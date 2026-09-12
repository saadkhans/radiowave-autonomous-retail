from __future__ import annotations

from pathlib import Path

import pytest

from radiowave.contracts import (
    EPC,
    ItemObservation,
    PersonObservation,
    SpatialUncertainty,
    Velocity,
    WorldCoordinate,
)
from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.replay.clock import ReplayPacer, SimulatedClock
from radiowave.replay.reader import (
    InMemoryReplaySource,
    JsonlReplaySource,
    ParquetReplaySource,
    ReplayPlayer,
    observations_from,
    open_replay_source,
)
from radiowave.replay.recorder import InMemoryRecorder, JsonlRecorder, ParquetRecorder
from radiowave.simulator.stores import EPC_SHIRT_A
from tests.conftest import T0, at


def _observations() -> list[PersonObservation | ItemObservation]:
    return [
        PersonObservation(
            observation_id="mmwave:radar-north:0",
            sensor_id="radar-north",
            timestamp=at(0.0),
            confidence=0.9,
            coordinate=WorldCoordinate(x=1.0, y=2.0, z=1.0),
            uncertainty=SpatialUncertainty.isotropic(0.08),
            velocity=Velocity(vx=0.5),
            scenario_id="unit",
            metadata={"native_track_id": "T1", "snr_db": 17.5},
        ),
        ItemObservation(
            observation_id="rfid:rfid-f1:0",
            sensor_id="rfid-f1",
            timestamp=at(0.1),
            confidence=0.8,
            epc=EPC(value=EPC_SHIRT_A),
            zone_id="zone-f1",
            rssi_dbm=-51.2,
            phase_rad=1.1,
            read_rate_hz=4.0,
            coordinate=WorldCoordinate(x=3.9, y=6.4, z=0.9),
            uncertainty=SpatialUncertainty.isotropic(0.5),
            scenario_id="unit",
            metadata={"native_antenna": "1"},
        ),
    ]


def _entries(observations: list[PersonObservation | ItemObservation]) -> list[RecordedEntry]:
    return [RecordedEntry.from_observation(i, o) for i, o in enumerate(observations)]


def test_recorded_entry_preserves_required_fields() -> None:
    entry = _entries(_observations())[1]
    assert entry.kind == EntryKind.OBSERVATION
    assert entry.source_type == "RFID"
    assert entry.sensor_id == "rfid-f1"
    assert entry.scenario_id == "unit"
    assert entry.payload["coordinate"] == {"x": 3.9, "y": 6.4, "z": 0.9, "frame_id": "store"}
    assert entry.payload["metadata"] == {"native_antenna": "1"}
    assert entry.payload["confidence"] == 0.8
    assert entry.to_observation() == _observations()[1]


@pytest.mark.parametrize("fmt", ["jsonl", "parquet"])
def test_file_recorders_round_trip(tmp_path: Path, fmt: str) -> None:
    path = tmp_path / f"rec.{fmt}"
    recorder = JsonlRecorder(path) if fmt == "jsonl" else ParquetRecorder(path)
    originals = _observations()
    for observation in originals:
        recorder.record_observation(observation)
    recorder.record_payload(EntryKind.GROUND_TRUTH, at(5.0), {"note": "truth"}, "unit")
    recorder.close()
    source = open_replay_source(path)
    assert isinstance(source, JsonlReplaySource if fmt == "jsonl" else ParquetReplaySource)
    entries = list(source.entries())
    assert [e.sequence for e in entries] == [0, 1, 2]
    assert list(observations_from(entries)) == originals
    assert entries[2].kind == EntryKind.GROUND_TRUTH and entries[2].payload == {"note": "truth"}


def test_in_memory_source_orders_by_timestamp() -> None:
    entries = _entries(_observations())
    source = InMemoryReplaySource(reversed(entries))
    assert [e.sequence for e in source.entries()] == [0, 1]


def test_pacer_rates_with_injected_sleep() -> None:
    sleeps: list[float] = []
    fast = ReplayPacer(rate=0.0, sleep=sleeps.append)
    for entry in _entries(_observations()):
        fast.wait_for(entry.timestamp)
    assert sleeps == []
    accelerated = ReplayPacer(rate=2.0, sleep=sleeps.append)
    for t in (0.0, 1.0, 3.0):
        accelerated.wait_for(at(t))
    assert sleeps == pytest.approx([0.5, 1.0])
    assert accelerated.total_waited_s == pytest.approx(1.5)
    with pytest.raises(ValueError):
        ReplayPacer(rate=-1)


def test_player_step_by_step_is_deterministic() -> None:
    recorder = InMemoryRecorder()
    for observation in _observations():
        recorder.record_observation(observation)
    player = iter(ReplayPlayer(InMemoryReplaySource(recorder.entries)))
    first = next(player)
    second = next(player)
    assert (first.sequence, second.sequence) == (0, 1)
    with pytest.raises(StopIteration):
        next(player)


def test_simulated_clock_only_moves_forward() -> None:
    clock = SimulatedClock(T0)
    assert clock.advance(1.5) == at(1.5)
    clock.set(at(2.0))
    with pytest.raises(ValueError):
        clock.set(at(1.0))
