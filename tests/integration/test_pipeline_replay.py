"""Record -> replay determinism: a recording replays to the same result without the simulator."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from radiowave.contracts.observations import AnyObservation
from radiowave.contracts.recording import EntryKind
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline, PipelineResult
from radiowave.replay.reader import observations_from, open_replay_source
from radiowave.replay.recorder import JsonlRecorder, ParquetRecorder
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import run_observations, run_scenario, scenario_observations


def _fingerprint(result: PipelineResult) -> dict[str, object]:
    return {
        "committed": [
            (e.event_id, e.event_type.value, e.shopper_track_id, round(e.confidence, 6))
            for e in result.committed_events
        ],
        "decisions": [
            (d.event_id, d.decision.value, round(d.confidence, 6)) for d in result.decisions
        ],
        "carts": {c: sorted(cart.lines) for c, cart in result.cart_state.carts.items()},
        "items": [(t.epc.value, t.state.value, t.carrier_track_id) for t in result.item_tracks],
        "persons": [(p.track_id, p.observation_count) for p in result.person_tracks],
    }


def test_same_seed_is_bit_for_bit_deterministic() -> None:
    scenario = load_scenario("06")
    first = scenario_observations(scenario)
    second = scenario_observations(scenario)
    assert [o.model_dump() for o in first] == [o.model_dump() for o in second]
    assert _fingerprint(run_scenario(scenario)) == _fingerprint(run_scenario(scenario))


def test_different_seed_changes_observations_but_not_the_outcome() -> None:
    scenario = load_scenario("01")
    reseeded = scenario.model_copy(update={"seed": scenario.seed + 1})
    a = scenario_observations(scenario)
    b = scenario_observations(reseeded)
    assert [o.model_dump() for o in a] != [o.model_dump() for o in b]
    result = run_scenario(reseeded)
    assert [e.event_type.value for e in result.committed_events][:1] == ["PICK"]
    assert result.cart_state.epcs_in(result.committed_events[0].shopper_track_id or "") == {
        scenario.carries[0].epc
    }


def test_step_by_step_ingest_matches_batch_run() -> None:
    scenario = load_scenario("06")
    observations = scenario_observations(scenario)
    registry = StoreRegistry(scenario.store)
    stepped = FoundationPipeline(registry, scenario_id=scenario.scenario_id)
    for observation in observations:
        stepped.ingest(observation)
    assert _fingerprint(stepped.finish()) == _fingerprint(run_observations(scenario, observations))
    assert stepped.result().steps > 0


def test_paced_replay_consumes_lazily() -> None:
    scenario = load_scenario("01")
    observations = scenario_observations(scenario)
    seen: list[int] = []

    def counting() -> Iterator[AnyObservation]:
        for index, observation in enumerate(observations):
            seen.append(index)
            yield observation

    pipeline = FoundationPipeline(StoreRegistry(scenario.store), scenario_id="01")
    stream = counting()
    pipeline.ingest(next(stream))
    assert seen == [0]  # nothing beyond the first observation has been pulled yet
    pipeline.run(stream)
    assert seen[-1] == len(observations) - 1


@pytest.mark.parametrize("fmt", ["jsonl", "parquet"])
def test_recording_replays_to_identical_result(tmp_path: Path, fmt: str) -> None:
    scenario = load_scenario("05")
    path = tmp_path / f"scenario-05.{fmt}"
    recorder = JsonlRecorder(path) if fmt == "jsonl" else ParquetRecorder(path)
    live = run_scenario(scenario, recorder=recorder)
    recorder.close()

    entries = list(open_replay_source(path).entries())
    kinds = {e.kind for e in entries}
    assert {
        EntryKind.OBSERVATION,
        EntryKind.RETAIL_EVENT,
        EntryKind.DECISION,
        EntryKind.CART_EVENT,
        EntryKind.GROUND_TRUTH,
    } <= kinds
    observation_entries = [e for e in entries if e.kind == EntryKind.OBSERVATION]
    assert len(observation_entries) == live.observations_accepted
    sample = observation_entries[0]
    assert sample.scenario_id == "05" and sample.sensor_id and sample.source_type
    assert "coordinate" in sample.payload and "metadata" in sample.payload
    assert "confidence" in sample.payload

    replayed = run_observations(scenario, observations_from(entries))
    assert _fingerprint(replayed) == _fingerprint(live)
