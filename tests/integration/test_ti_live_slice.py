"""The Phase 3 vertical slice without hardware:

fixture UART bytes -> TI parser -> native target -> PersonObservation -> FoundationPipeline
-> canonical PersonTrack -> Observatory snapshot, plus normalized capture and replay.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from radiowave.api.live import LiveObservatoryRun, LiveRuntime, allocate_capture_path
from radiowave.cli import main as cli_main
from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline
from radiowave.replay.reader import JsonlReplaySource
from tests.fixtures.ti_mmwave.builder import build_target_frame
from tests.unit.mmwave_ti.support import (
    RADAR,
    T0,
    FakeTiming,
    HoldOpenStream,
    live_config,
    wait_until,
)

FRAMES = 40


def walking_frames() -> list[bytes]:
    """One TI target (native id 3) walking from 3.5 m to 1.5 m in front of the radar."""
    return [
        build_target_frame(
            number,
            [
                {
                    "tid": 3,
                    "x": 0.2,
                    "y": 3.5 - 0.05 * number,
                    "z": 1.0,
                    "vx": 0.0,
                    "vy": -0.5,
                    "confidence": 0.85,
                }
            ],
        )
        for number in range(1, FRAMES + 1)
    ]


def make_run(tmp_path: Path, *, capture: bool, run_id: str = "run-0001") -> LiveObservatoryRun:
    config = live_config()
    frames = walking_frames()
    runtime = LiveRuntime(
        config=config,
        # Idle reads block: a timing-out idle port would run stale checks that advance
        # the auto-stepping fake clock a thread-timing-dependent number of times, and
        # two captures of the same bytes must be stamp-for-stamp identical.
        stream_factory=lambda: HoldOpenStream(frames, block_when_idle=True),
        timing=FakeTiming().timing(),
        capture_dir=tmp_path,
        autonomous=False,
    )
    run = LiveObservatoryRun(run_id, runtime, capture=capture)
    # Let the reader consume the whole fixture before the pipeline ticks, so the
    # fake clock stamps every frame deterministically (one clock read per frame).
    assert wait_until(lambda: run.session.queued == FRAMES)
    return run


def drive(run: LiveObservatoryRun, ticks: int = 3) -> None:
    for _ in range(ticks):
        with run.lock:
            run.tick()


def test_fixture_bytes_become_a_canonical_moving_track(tmp_path: Path) -> None:
    run = make_run(tmp_path, capture=False)
    try:
        drive(run)
        snapshot = run.snapshot()
        state = snapshot.state
        assert state.mode == "LIVE"
        assert state.scenario_id == "live"
        assert state.live is not None
        assert state.live.sensor_id == RADAR
        assert state.live.frames_parsed == FRAMES
        assert state.live.observations_emitted == FRAMES
        assert state.counters.accepted == FRAMES
        assert state.duration_s == state.time_s
        assert snapshot.timeline.ground_truth == []

        assert len(state.persons) == 1
        person = state.persons[0]
        assert person.track_id == "P0001"  # canonical, never "3"
        assert person.sensor_ids == [RADAR]
        # Radar at (6, 0) facing north: a target 0.2 m right / y ahead is at x=6.2, y=ahead.
        assert abs(person.x - 6.2) < 0.3
        assert 1.3 < person.y < 3.6
        assert len(person.trail) >= 2
        assert person.trail[0].y > person.trail[-1].y  # walked toward the radar
        assert person.sigma_m > 0.0
        assert 0.0 < person.confidence < 1.0
        assert [e.label for e in snapshot.events.events] == [
            "PERSON_TRACK_CREATED",
            "SESSION_OPENED",
        ]
    finally:
        run.close()


def test_replay_only_controls_are_refused_in_live_mode(tmp_path: Path) -> None:
    import pytest

    from radiowave.api.runs import LiveModeError

    run = make_run(tmp_path, capture=False)
    try:
        for control in (run.step, run.reset, lambda: run.advance(1.0), lambda: run.seek(0.0)):
            with pytest.raises(LiveModeError):
                control()
    finally:
        run.close()


def test_capture_replays_deterministically_without_hardware(tmp_path: Path) -> None:
    run = make_run(tmp_path, capture=True)
    drive(run)
    run.stop()
    assert run.finished
    assert run.capture_path is not None and run.capture_path.exists()

    entries = list(JsonlReplaySource(run.capture_path).entries())
    kinds = [entry.kind for entry in entries]
    assert kinds[0] == EntryKind.STORE_TWIN
    assert kinds[1] == EntryKind.PIPELINE_CONFIG
    assert kinds[-1] == EntryKind.RUN_END
    assert kinds.count(EntryKind.OBSERVATION) == FRAMES
    assert all(entry.format_version == 2 for entry in entries)
    observation = next(e for e in entries if e.kind == EntryKind.OBSERVATION)
    assert observation.sensor_id == RADAR
    assert observation.payload["metadata"]["native_track_id"] == "g1:t3"
    assert observation.payload["metadata"]["native_ti_track_id"] == 3
    assert observation.payload["metadata"]["native_frame_number"] == 1
    assert observation.payload["metadata"]["native_firmware_profile"] == "ti-3d-people-counting"

    # Replay through the pipeline reproduces the live canonical result exactly.
    live_tracks = run.pipeline.result().person_tracks
    replay = FoundationPipeline(StoreRegistry(live_config().store))
    result = replay.replay(entries)
    assert [t.track_id for t in result.person_tracks] == [t.track_id for t in live_tracks]
    for replayed, live in zip(result.person_tracks, live_tracks, strict=True):
        assert replayed.position == live.position
        assert replayed.observation_count == live.observation_count
        assert replayed.history == live.history

    # ...and the ordinary CLI replays it with no hardware and no special flags.
    assert cli_main(["replay", str(run.capture_path)]) == 0


def test_two_captures_of_the_same_stream_are_byte_identical(tmp_path: Path) -> None:
    first = make_run(tmp_path / "a", capture=True, run_id="run-0001")
    drive(first)
    first.stop()
    second = make_run(tmp_path / "b", capture=True, run_id="run-0002")
    drive(second)
    second.stop()
    assert first.capture_path is not None and second.capture_path is not None
    lines_a = [json.loads(line) for line in first.capture_path.read_text().splitlines()]
    lines_b = [json.loads(line) for line in second.capture_path.read_text().splitlines()]
    assert lines_a == lines_b


def test_recorded_entries_validate_as_a_v2_stream(tmp_path: Path) -> None:
    from radiowave.contracts.recording import validate_recording

    run = make_run(tmp_path, capture=True)
    drive(run)
    run.stop()
    assert run.capture_path is not None
    entries: list[RecordedEntry] = list(JsonlReplaySource(run.capture_path).entries())
    assert list(validate_recording(iter(entries))) == entries


def test_allocate_capture_path_never_overwrites_a_previous_experiment(tmp_path: Path) -> None:
    """Invariant 14: capture files never overwrite previous experiments.

    Two allocations that land on the same sensor, run id and (mocked) microsecond
    stamp must not resolve to the same file: the second gets a numeric suffix and the
    first file's contents stay untouched.
    """
    stamp = "20260914T101530.123456Z"
    first = allocate_capture_path(tmp_path, RADAR, "run-0042", stamp)
    first.write_text("first run's data\n")

    second = allocate_capture_path(tmp_path, RADAR, "run-0042", stamp)
    assert second != first
    assert first.read_text() == "first run's data\n"
    assert second.exists()

    # A capture from a different run in the same second gets its own file too.
    third = allocate_capture_path(tmp_path, RADAR, "run-0043", stamp)
    assert third not in (first, second)


def test_live_tick_uses_the_session_clock_not_a_regressing_wall_clock(tmp_path: Path) -> None:
    """Invariant 9: sensor observations and the pipeline clock share one ordered clock.

    Before the fix, ``tick`` advanced the pipeline with a wall-clock read taken
    independently of the session's own clock (the one that actually stamps every
    observation), so a host wall-clock regression could make ``now`` fall behind
    observations already stamped, producing spurious out-of-order drops. ``tick`` now
    reads both the drained batch and ``now`` from ``session.drain_with_clock()``
    alone, so a regressing wall clock fed to ``SessionTiming.utc_now`` (consumed only
    once, to anchor the session clock) has no effect on ticking at all.
    """
    fixture_frames = walking_frames()
    timing = FakeTiming(wall=[T0, T0 - timedelta(hours=1), T0 - timedelta(hours=2)]).timing()
    runtime = LiveRuntime(
        config=live_config(),
        stream_factory=lambda: HoldOpenStream(fixture_frames),
        timing=timing,
        capture_dir=tmp_path,
        autonomous=False,
    )
    run = LiveObservatoryRun("run-0001", runtime, capture=False)
    try:
        assert wait_until(lambda: run.session.queued == FRAMES)
        for _ in range(5):
            with run.lock:
                run.tick()
        assert run.pipeline.result().observations_out_of_order == 0
    finally:
        run.close()
