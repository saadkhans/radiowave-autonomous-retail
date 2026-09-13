"""Recording/replay matrix: invariants that must hold for every driver, scenario and format.

Invariants covered by this module:

1. Recording sequence is deterministic.
2. ``RUN_END`` is terminal (``entries[-1].kind == RUN_END``, exactly one, nothing after).
3. Nothing writes after finalization.
4. Explicit clock movement (``CLOCK`` entries) is replayable.
5. Duplicate inputs are replayable without affecting physical time.
6. Every accepted final observation is evaluated exactly once.
7. Replay never evaluates physical state past the run's advertised duration.
8. Recorded + replayed result == live result.

Two shared drivers are used throughout:

* ``_drive_like_observatory`` mimics ``ObservatoryRun.advance`` (fixed-size time chunks,
  ingest everything timestamped at or before the chunk boundary, then an explicit
  ``advance_to``), the way the Observatory API drives a run.
* ``run_scenario`` (imported from the simulator) drives a run in one batch, the way an
  ordinary offline run does.

Both must record a recording that replays, byte-for-byte outcome, to the same result.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from radiowave.contracts.observations import AnyObservation
from radiowave.contracts.recording import EntryKind, RecordedEntry, RecordingError
from radiowave.contracts.store import SourceType
from radiowave.pipeline import FoundationPipeline, PipelineResult
from radiowave.replay.reader import open_replay_source
from radiowave.replay.recorder import InMemoryRecorder, JsonlRecorder, ParquetRecorder
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import (
    build_pipeline,
    run_scenario,
    scenario_observation_stream,
    scenario_observations,
)
from radiowave.simulator.scenario import Scenario

# --------------------------------------------------------------------------- fingerprint


def _fingerprint(result: PipelineResult) -> dict[str, object]:
    """A replay-independent summary: two results with the same fingerprint agree on
    everything downstream logic can observe."""
    return {
        "observations_accepted": result.observations_accepted,
        "observations_dropped": result.observations_dropped,
        "steps": result.steps,
        "persons": [(p.track_id, p.state.value, p.observation_count) for p in result.person_tracks],
        "items": [(t.epc.value, t.state.value, t.carrier_track_id) for t in result.item_tracks],
        "sessions": [(s.session_id, s.state.value) for s in result.sessions],
        "committed": [
            (e.event_id, e.event_type.value, e.shopper_track_id) for e in result.committed_events
        ],
        "review": sorted(e.event_id for e in result.review_events),
        "pending": sorted(e.event_id for e in result.pending_events),
        "decisions": [
            (d.event_id, d.decision.value, round(d.confidence, 6)) for d in result.decisions
        ],
        "carts": {
            cart_id: (cart.status.value, cart.exited_at, sorted(cart.lines))
            for cart_id, cart in result.cart_state.carts.items()
        },
        "cart_sessions": dict(result.cart_sessions),
        "unresolved": sorted(
            (epc, item.reason, item.source_event_id)
            for epc, item in result.cart_state.unresolved.items()
        ),
    }


# --------------------------------------------------------------------------- drivers


def _drive_like_observatory(
    scenario: Scenario,
    recorder,
    *,
    chunk_s: float,
    stop_at_s: float | None = None,
    observations: list[AnyObservation] | None = None,
) -> tuple[PipelineResult, FoundationPipeline]:
    """Mimic ``ObservatoryRun.advance``: fixed-size chunks, ingest everything timestamped
    at or before the chunk boundary, then an explicit ``advance_to`` — up to
    ``scenario.duration_s`` (or ``stop_at_s``) — and finally ``finish(advance=False)``.

    Returns ``(result, pipeline)``; the pipeline is exposed because some invariants (the
    replay clock's last stepped position) have no public accessor.
    """
    pipeline = build_pipeline(scenario, recorder=recorder)
    stream = observations if observations is not None else scenario_observation_stream(scenario)
    limit = scenario.duration_s if stop_at_s is None else stop_at_s
    cursor = 0
    time_s = 0.0
    while time_s < limit:
        time_s = min(time_s + chunk_s, limit)
        target_at = scenario.at(time_s)
        while cursor < len(stream) and stream[cursor].timestamp <= target_at:
            pipeline.ingest(stream[cursor])
            cursor += 1
        pipeline.advance_to(target_at)
    return pipeline.finish(advance=False), pipeline


def _replay(scenario: Scenario, entries) -> PipelineResult:
    return build_pipeline(scenario).replay(entries)


def _stream_order_key(observation: AnyObservation) -> tuple:
    return (observation.timestamp, observation.source_type.value, observation.observation_id)


def _with_boundary_observation(
    scenario: Scenario, offset_s: float, suffix: str
) -> list[AnyObservation]:
    """The scenario's fed stream plus one extra person sample near the duration boundary.

    The template's own coordinate is nudged by a tiny amount so the extra sample is
    genuinely new evidence, not a byte-for-byte content duplicate of the reading it is
    based on (radar frames already land exactly on ``duration_s`` for these scenarios).
    """
    base = scenario_observation_stream(scenario)
    template = next(
        o for o in reversed(scenario_observations(scenario)) if o.source_type == SourceType.MMWAVE
    )
    nudged_coordinate = template.coordinate.model_copy(update={"x": template.coordinate.x + 0.001})
    extra = template.model_copy(
        update={
            "observation_id": f"{template.observation_id}-{suffix}",
            "timestamp": scenario.at(scenario.duration_s + offset_s),
            "coordinate": nudged_coordinate,
        }
    )
    return sorted([*base, extra], key=_stream_order_key)


# --------------------------------------------------------------------------- test A


def _case_ordinary_run() -> tuple[Scenario, InMemoryRecorder, PipelineResult]:
    scenario = load_scenario("05")
    recorder = InMemoryRecorder()
    live = run_scenario(scenario, recorder=recorder)
    return scenario, recorder, live


def _case_drive_aligned() -> tuple[Scenario, InMemoryRecorder, PipelineResult]:
    scenario = load_scenario("04")
    recorder = InMemoryRecorder()
    live, _pipeline = _drive_like_observatory(scenario, recorder, chunk_s=0.25)
    return scenario, recorder, live


def _case_drive_nonaligned() -> tuple[Scenario, InMemoryRecorder, PipelineResult]:
    scenario = load_scenario("04")
    recorder = InMemoryRecorder()
    live, _pipeline = _drive_like_observatory(scenario, recorder, chunk_s=0.3)
    return scenario, recorder, live


def _case_trailing_dropout() -> tuple[Scenario, InMemoryRecorder, PipelineResult]:
    scenario = load_scenario("04")
    recorder = InMemoryRecorder()
    pipeline = build_pipeline(scenario, recorder=recorder)
    cutoff = scenario.at(scenario.duration_s - 4.0)
    for observation in scenario_observations(scenario):
        if observation.timestamp <= cutoff:
            pipeline.ingest(observation)
    pipeline.advance_to(scenario.at(scenario.duration_s))
    live = pipeline.finish(advance=False)
    return scenario, recorder, live


def _case_scenario_10() -> tuple[Scenario, InMemoryRecorder, PipelineResult]:
    scenario = load_scenario("10")
    recorder = InMemoryRecorder()
    live = run_scenario(scenario, recorder=recorder)
    return scenario, recorder, live


def _case_nonstep_aligned_duration() -> tuple[Scenario, InMemoryRecorder, PipelineResult]:
    base = load_scenario("01")
    scenario = base.model_copy(update={"duration_s": base.duration_s + 0.1})
    recorder = InMemoryRecorder()
    live, _pipeline = _drive_like_observatory(scenario, recorder, chunk_s=0.25)
    return scenario, recorder, live


def _case_single_observation() -> tuple[Scenario, InMemoryRecorder, PipelineResult]:
    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    pipeline = build_pipeline(scenario, recorder=recorder)
    observations = scenario_observations(scenario)
    pipeline.ingest(observations[0])
    live = pipeline.finish()
    return scenario, recorder, live


def _case_zero_observations() -> tuple[Scenario, InMemoryRecorder, PipelineResult]:
    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    pipeline = build_pipeline(scenario, recorder=recorder)
    live = pipeline.finish()
    return scenario, recorder, live


CASE_BUILDERS: dict[str, Callable[[], tuple[Scenario, InMemoryRecorder, PipelineResult]]] = {
    "ordinary run": _case_ordinary_run,
    "observatory drive aligned": _case_drive_aligned,
    "observatory drive non-aligned chunk": _case_drive_nonaligned,
    "trailing dropout": _case_trailing_dropout,
    "scenario 10 duplicates": _case_scenario_10,
    "non-step-aligned duration": _case_nonstep_aligned_duration,
    "single observation then finish": _case_single_observation,
    "zero observations with scheduled ground truth": _case_zero_observations,
}


@pytest.mark.parametrize("case_name", list(CASE_BUILDERS), ids=list(CASE_BUILDERS))
def test_run_end_is_the_final_entry_matrix(case_name: str) -> None:
    """Invariants 1 (deterministic sequence), 2 (RUN_END terminal) and 8 (record+replay==live).

    Scenario 01 schedules a ground-truth entry (see ``build_pipeline``), so even the
    "zero observations" case ends up recording a header, that ground truth and a
    RUN_END; a genuinely empty recording (no header ever written) is not exercised by
    any case here and would only occur for a pipeline with no recorder, no observation
    and no scheduled entry at all.
    """
    scenario, recorder, live = CASE_BUILDERS[case_name]()
    entries = recorder.entries
    if not entries:
        return

    run_ends = [entry for entry in entries if entry.kind == EntryKind.RUN_END]
    assert len(run_ends) == 1
    assert entries[-1].kind == EntryKind.RUN_END
    timestamps = [entry.timestamp for entry in entries]
    assert timestamps == sorted(timestamps)
    assert all(entry.format_version == 2 for entry in entries)
    assert [entry.sequence for entry in entries] == list(range(len(entries)))

    replayed = _replay(scenario, entries)
    assert _fingerprint(replayed) == _fingerprint(live)
    assert replayed.steps == live.steps


# --------------------------------------------------------------------------- test B


def test_finish_is_idempotent_and_seals_the_recording() -> None:
    """Invariant 3: nothing writes after finalization."""
    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    pipeline = build_pipeline(scenario, recorder=recorder)
    for observation in scenario_observations(scenario):
        pipeline.ingest(observation)

    first = pipeline.finish()
    recorded_after_first = len(recorder.entries)
    second = pipeline.finish()
    assert second is first
    assert len(recorder.entries) == recorded_after_first
    assert pipeline.finished is True

    fresh_observation = scenario_observations(scenario)[0]
    with pytest.raises(RuntimeError, match="finished"):
        pipeline.ingest(fresh_observation)
    with pytest.raises(RuntimeError, match="finished"):
        pipeline.advance_to(scenario.at(scenario.duration_s))
    with pytest.raises(RuntimeError, match="finished"):
        pipeline.schedule_entry(scenario.at(0.0), EntryKind.GROUND_TRUTH, {})
    assert len(recorder.entries) == recorded_after_first


# --------------------------------------------------------------------------- test C


def _case_c_aligned():
    scenario = load_scenario("01")
    stream = scenario_observation_stream(scenario)
    recorder = InMemoryRecorder()
    live, pipeline = _drive_like_observatory(scenario, recorder, chunk_s=0.25, observations=stream)
    return scenario, recorder, live, pipeline, stream


def _case_c_nonaligned_duration():
    base = load_scenario("01")
    scenario = base.model_copy(update={"duration_s": base.duration_s + 0.1})
    stream = scenario_observation_stream(scenario)
    recorder = InMemoryRecorder()
    live, pipeline = _drive_like_observatory(scenario, recorder, chunk_s=0.25, observations=stream)
    return scenario, recorder, live, pipeline, stream


def _case_c_observation_at_duration():
    scenario = load_scenario("01")
    stream = _with_boundary_observation(scenario, 0.0, "at-duration")
    recorder = InMemoryRecorder()
    live, pipeline = _drive_like_observatory(scenario, recorder, chunk_s=0.25, observations=stream)
    return scenario, recorder, live, pipeline, stream


def _case_c_observation_before_duration():
    scenario = load_scenario("01")
    stream = _with_boundary_observation(scenario, -0.01, "before-duration")
    recorder = InMemoryRecorder()
    live, pipeline = _drive_like_observatory(scenario, recorder, chunk_s=0.25, observations=stream)
    return scenario, recorder, live, pipeline, stream


def _case_c_trailing_dropout():
    scenario = load_scenario("04")
    recorder = InMemoryRecorder()
    pipeline = build_pipeline(scenario, recorder=recorder)
    cutoff = scenario.at(scenario.duration_s - 4.0)
    fed = [o for o in scenario_observations(scenario) if o.timestamp <= cutoff]
    for observation in fed:
        pipeline.ingest(observation)
    pipeline.advance_to(scenario.at(scenario.duration_s))
    live = pipeline.finish(advance=False)
    return scenario, recorder, live, pipeline, fed


TERMINAL_CASES: dict[str, Callable[[], tuple]] = {
    "duration aligned to step": _case_c_aligned,
    "duration not aligned to step": _case_c_nonaligned_duration,
    "observation exactly at duration": _case_c_observation_at_duration,
    "observation immediately before duration": _case_c_observation_before_duration,
    "no observation in final interval": _case_c_trailing_dropout,
}


@pytest.mark.parametrize("case_name", list(TERMINAL_CASES), ids=list(TERMINAL_CASES))
def test_terminal_sample_is_evaluated_exactly_once_and_never_past_duration(case_name: str) -> None:
    """Invariants 6 (evaluated exactly once) and 7 (never evaluates past duration)."""
    scenario, recorder, live, pipeline, fed_stream = TERMINAL_CASES[case_name]()
    end = scenario.at(scenario.duration_s)

    assert all(d.evaluated_at <= end for d in live.decisions)
    assert all(e.timestamp <= end for e in live.committed_events)
    # pipeline._last_step_at: no public accessor for the replay clock's last stepped position.
    assert pipeline._last_step_at is None or pipeline._last_step_at <= end

    unique_fed_ids = {o.observation_id for o in fed_stream}
    assert live.observations_accepted == len(unique_fed_ids)

    replayed = _replay(scenario, recorder.entries)
    assert replayed.steps == live.steps
    assert _fingerprint(replayed) == _fingerprint(live)


# --------------------------------------------------------------------------- test D


@pytest.mark.parametrize("fmt", ["jsonl", "parquet"])
def test_scenario_10_duplicates_round_trip_through_deduplication(tmp_path: Path, fmt: str) -> None:
    """Invariant 5: duplicate inputs are replayable without affecting physical time."""
    scenario = load_scenario("10")
    path = tmp_path / f"scenario-10.{fmt}"
    recorder = JsonlRecorder(path) if fmt == "jsonl" else ParquetRecorder(path)
    live = run_scenario(scenario, recorder=recorder)
    recorder.close()

    entries = list(open_replay_source(path).entries())
    assert live.observations_dropped > 0
    duplicate_entries = [e for e in entries if e.kind == EntryKind.DUPLICATE_OBSERVATION]
    observation_entries = [e for e in entries if e.kind == EntryKind.OBSERVATION]
    assert len(duplicate_entries) == live.observations_dropped
    assert len(observation_entries) == live.observations_accepted

    replayed = _replay(scenario, entries)
    assert replayed.observations_dropped == live.observations_dropped
    assert _fingerprint(replayed) == _fingerprint(live)

    # A normal (non-duplicated) scenario never invents a duplicate entry.
    scenario_05 = load_scenario("05")
    recorder_05 = InMemoryRecorder()
    run_scenario(scenario_05, recorder=recorder_05)
    assert not any(e.kind == EntryKind.DUPLICATE_OBSERVATION for e in recorder_05.entries)


# --------------------------------------------------------------------------- test E


def test_duplicate_with_future_timestamp_cannot_advance_the_clock() -> None:
    """Invariant 5: a duplicate ingress attempt never mutates dedup state or fusion time,
    however its own timestamp compares to the clock."""
    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    pipeline = build_pipeline(scenario, recorder=recorder)
    observations = scenario_observations(scenario)
    for observation in observations[:5]:
        assert pipeline.ingest(observation)

    accepted = observations[2]
    future_duplicate = accepted.model_copy(
        update={"timestamp": accepted.timestamp + timedelta(seconds=30)}
    )
    dropped_before = pipeline.dedup.dropped
    last_timestamp_before = pipeline._last_timestamp
    steps_before = pipeline.result().steps

    assert pipeline.ingest(future_duplicate) is False
    assert pipeline.dedup.dropped == dropped_before + 1
    assert pipeline._last_timestamp == last_timestamp_before
    assert pipeline.result().steps == steps_before

    duplicate_entry = recorder.entries[-1]
    assert duplicate_entry.kind == EntryKind.DUPLICATE_OBSERVATION
    # Stamped with the clock, not with the (future) attempt's own timestamp.
    assert duplicate_entry.timestamp == last_timestamp_before
    assert duplicate_entry.payload["timestamp"] != duplicate_entry.timestamp.isoformat()

    live = pipeline.finish()
    replayed = _replay(scenario, recorder.entries)
    assert replayed.observations_dropped == live.observations_dropped
    assert replayed.steps == live.steps
    assert _fingerprint(replayed) == _fingerprint(live)

    # A content-duplicate (same content, fresh observation_id) is rejected the same way.
    recorder2 = InMemoryRecorder()
    pipeline2 = build_pipeline(scenario, recorder=recorder2)
    for observation in observations[:5]:
        pipeline2.ingest(observation)
    content_duplicate = observations[2].model_copy(update={"observation_id": "content-dup"})
    dropped_before2 = pipeline2.dedup.dropped
    assert pipeline2.ingest(content_duplicate) is False
    assert pipeline2.dedup.dropped == dropped_before2 + 1


# --------------------------------------------------------------------------- test F


def test_replay_refuses_a_duplicate_entry_whose_original_is_missing() -> None:
    """A DUPLICATE_OBSERVATION with no accepted original is not self-consistent."""
    scenario = load_scenario("10")
    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)
    entries: list[RecordedEntry] = list(recorder.entries)

    duplicate = next(e for e in entries if e.kind == EntryKind.DUPLICATE_OBSERVATION)
    original_id = duplicate.payload["observation_id"]
    tampered = [
        e
        for e in entries
        if not (e.kind == EntryKind.OBSERVATION and e.payload.get("observation_id") == original_id)
    ]

    with pytest.raises(RecordingError, match="not self-consistent"):
        build_pipeline(scenario).replay(tampered)


# --------------------------------------------------------------------------- test G


@pytest.mark.parametrize("fmt", ["jsonl", "parquet"])
def test_explicit_clock_advance_replays_identically_in_both_file_formats(
    tmp_path: Path, fmt: str
) -> None:
    """Invariant 4: explicit clock movement is replayable, in every persisted format."""
    scenario = load_scenario("04")
    path = tmp_path / f"scenario-04-dropout.{fmt}"
    recorder = JsonlRecorder(path) if fmt == "jsonl" else ParquetRecorder(path)
    pipeline = build_pipeline(scenario, recorder=recorder)
    cutoff = scenario.at(scenario.duration_s - 4.0)
    for observation in scenario_observations(scenario):
        if observation.timestamp <= cutoff:
            pipeline.ingest(observation)
    pipeline.advance_to(scenario.at(scenario.duration_s))
    live = pipeline.finish(advance=False)
    recorder.close()

    entries = list(open_replay_source(path).entries())
    clock_entries = [e for e in entries if e.kind == EntryKind.CLOCK]
    assert clock_entries and clock_entries[-1].timestamp == scenario.at(scenario.duration_s)
    run_end = [e for e in entries if e.kind == EntryKind.RUN_END]
    assert len(run_end) == 1 and run_end[0].payload == {"advance": False}

    replayed = _replay(scenario, entries)
    assert _fingerprint(replayed) == _fingerprint(live)


# --------------------------------------------------------------------------- test H


@pytest.mark.parametrize("driver", ["batch", "observatory"])
@pytest.mark.parametrize("scenario_id", ["01", "03", "04", "05", "06", "10"])
def test_live_vs_recorded_replay_matrix(scenario_id: str, driver: str) -> None:
    """Invariant 8: recorded + replayed result == live result, for every driver and scenario."""
    scenario = load_scenario(scenario_id)
    recorder = InMemoryRecorder()
    if driver == "batch":
        live = run_scenario(scenario, recorder=recorder)
    else:
        live, _pipeline = _drive_like_observatory(scenario, recorder, chunk_s=0.25)

    replayed = _replay(scenario, recorder.entries)
    assert _fingerprint(replayed) == _fingerprint(live)
