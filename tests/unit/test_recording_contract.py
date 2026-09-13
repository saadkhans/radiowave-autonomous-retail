"""Recording format contract (v1 -> v2 evolution of ``radiowave.contracts.recording``).

Invariants covered:

* new recordings declare the format they actually use (every writer emits
  ``CURRENT_RECORDING_FORMAT_VERSION``, currently 2);
* v1 recordings remain readable (a Foundation v0 recording with no v2-only entries
  still validates, round-trips through every recorder/reader and replays byte-for-byte
  the same as the legacy default-finish behaviour);
* future unsupported versions fail safely (an integer outside
  ``SUPPORTED_RECORDING_VERSIONS``, or a non-int such as ``True``, is rejected with a
  clear error at construction time and on read);
* RUN_END is terminal (nothing may follow it in a recording, and a single recording
  never mixes format versions).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from radiowave.contracts import EPC, ItemObservation
from radiowave.contracts.recording import (
    EntryKind,
    RecordedEntry,
    RecordingError,
    validate_recording,
)
from radiowave.replay.reader import (
    InMemoryReplaySource,
    JsonlReplaySource,
    ParquetReplaySource,
    open_replay_source,
)
from radiowave.replay.recorder import InMemoryRecorder, JsonlRecorder, ParquetRecorder
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import build_pipeline, run_scenario, scenario_observation_stream
from radiowave.simulator.stores import EPC_SHIRT_A
from tests.conftest import at

REPO_ROOT = Path(__file__).resolve().parents[2]


def _item_observation(
    *, observation_id: str = "rfid:rfid-f1:0", sensor_id: str = "rfid-f1", t: float = 0.0
) -> ItemObservation:
    return ItemObservation(
        observation_id=observation_id,
        sensor_id=sensor_id,
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        scenario_id="unit",
    )


def test_new_recordings_are_written_as_format_version_2(tmp_path: Path) -> None:
    scenario = load_scenario("05")

    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)
    assert recorder.entries
    assert all(entry.format_version == 2 for entry in recorder.entries)

    jsonl_path = tmp_path / "scenario05.jsonl"
    jsonl_recorder = JsonlRecorder(jsonl_path)
    run_scenario(scenario, recorder=jsonl_recorder)
    jsonl_recorder.close()
    jsonl_entries = list(open_replay_source(jsonl_path).entries())
    assert jsonl_entries
    assert all(entry.format_version == 2 for entry in jsonl_entries)

    parquet_path = tmp_path / "scenario05.parquet"
    parquet_recorder = ParquetRecorder(parquet_path)
    run_scenario(scenario, recorder=parquet_recorder)
    parquet_recorder.close()
    parquet_entries = list(open_replay_source(parquet_path).entries())
    assert parquet_entries
    assert all(entry.format_version == 2 for entry in parquet_entries)


def test_v1_recording_loads_and_replays_with_legacy_finish(tmp_path: Path) -> None:
    scenario = load_scenario("01")

    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)
    entries_v2 = recorder.entries
    assert any(entry.kind == EntryKind.RUN_END for entry in entries_v2)

    v2_only = {EntryKind.CLOCK, EntryKind.RUN_END, EntryKind.DUPLICATE_OBSERVATION}
    v1_entries = [
        RecordedEntry(**{**entry.model_dump(), "format_version": 1})
        for entry in entries_v2
        if entry.kind not in v2_only
    ]
    assert v1_entries
    assert all(entry.kind not in v2_only for entry in v1_entries)

    # The stream-level contract accepts this recording as-is.
    assert list(validate_recording(v1_entries)) == v1_entries

    jsonl_path = tmp_path / "scenario01_v1.jsonl"
    jsonl_recorder = JsonlRecorder(jsonl_path)
    for entry in v1_entries:
        jsonl_recorder.record(entry)
    jsonl_recorder.close()
    read_back_jsonl = list(JsonlReplaySource(jsonl_path).entries())
    assert all(entry.format_version == 1 for entry in read_back_jsonl)

    parquet_path = tmp_path / "scenario01_v1.parquet"
    parquet_recorder = ParquetRecorder(parquet_path)
    for entry in v1_entries:
        parquet_recorder.record(entry)
    parquet_recorder.close()
    read_back_parquet = list(ParquetReplaySource(parquet_path).entries())
    assert all(entry.format_version == 1 for entry in read_back_parquet)

    replayed_result = build_pipeline(scenario).replay(read_back_jsonl)
    legacy_result = build_pipeline(scenario).run(scenario_observation_stream(scenario))

    assert replayed_result.committed_events == legacy_result.committed_events
    assert replayed_result.cart_state == legacy_result.cart_state
    assert replayed_result.item_tracks == legacy_result.item_tracks
    assert replayed_result.steps == legacy_result.steps


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (EntryKind.CLOCK, {"timestamp": at(0.0).isoformat()}),
        (EntryKind.RUN_END, {"advance": True}),
        (EntryKind.DUPLICATE_OBSERVATION, {}),
    ],
)
def test_v2_clock_and_run_end_entries_validate_and_v1_rejects_them(
    kind: EntryKind, payload: dict[str, object]
) -> None:
    ok = RecordedEntry(sequence=0, timestamp=at(0.0), kind=kind, format_version=2, payload=payload)
    assert ok.format_version == 2 and ok.kind == kind

    with pytest.raises(ValidationError, match="requires recording format_version >= 2"):
        RecordedEntry(sequence=0, timestamp=at(0.0), kind=kind, format_version=1, payload=payload)


@pytest.mark.parametrize("bad_version", [3, 0])
def test_unsupported_future_version_fails_explicitly(bad_version: int, tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="unsupported recording format_version"):
        RecordedEntry(
            sequence=0,
            timestamp=at(0.0),
            kind=EntryKind.GROUND_TRUTH,
            format_version=bad_version,
            payload={},
        )

    with pytest.raises(ValidationError, match="must be an integer"):
        RecordedEntry(
            sequence=0,
            timestamp=at(0.0),
            kind=EntryKind.GROUND_TRUTH,
            format_version=True,
            payload={},
        )

    raw = {
        "sequence": 0,
        "timestamp": at(0.0).isoformat(),
        "kind": "GROUND_TRUTH",
        "source_type": None,
        "sensor_id": None,
        "scenario_id": None,
        "format_version": 3,
        "payload": {},
    }
    jsonl_path = tmp_path / "future_version.jsonl"
    jsonl_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="unsupported recording format_version"):
        list(JsonlReplaySource(jsonl_path).entries())


def test_mixed_version_stream_is_rejected() -> None:
    observation = _item_observation()
    entries = [
        RecordedEntry.from_observation(0, observation),
        RecordedEntry(
            sequence=1,
            timestamp=at(1.0),
            kind=EntryKind.GROUND_TRUTH,
            format_version=1,
            payload={},
        ),
    ]

    with pytest.raises(RecordingError, match="mixes format versions"):
        list(validate_recording(entries))

    scenario = load_scenario("01")
    with pytest.raises(RecordingError, match="mixes format versions"):
        build_pipeline(scenario).replay(entries)


def test_entries_after_run_end_are_rejected() -> None:
    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)
    entries = recorder.entries
    assert entries[-1].kind == EntryKind.RUN_END

    extra = RecordedEntry(
        sequence=len(entries),
        timestamp=entries[-1].timestamp + timedelta(seconds=1),
        kind=EntryKind.OBSERVATION,
        payload={},
    )
    entries_with_extra = [*entries, extra]

    with pytest.raises(RecordingError, match="follows RUN_END"):
        list(validate_recording(entries_with_extra))

    with pytest.raises(RecordingError, match="follows RUN_END"):
        build_pipeline(scenario).replay(entries_with_extra)


def test_duplicate_entry_to_observation_tolerates_clock_stamp_but_checks_envelope() -> None:
    observation = _item_observation(t=2.0)
    envelope_stamp = at(5.0)  # the pipeline clock had moved on by the time this was rejected
    duplicate_entry = RecordedEntry(
        sequence=0,
        timestamp=envelope_stamp,
        kind=EntryKind.DUPLICATE_OBSERVATION,
        source_type=observation.source_type,
        sensor_id=observation.sensor_id,
        scenario_id=observation.scenario_id,
        payload=observation.model_dump(mode="json"),
    )
    recovered = duplicate_entry.to_observation()
    assert recovered == observation
    assert recovered.timestamp == observation.timestamp
    assert recovered.timestamp != envelope_stamp

    mismatched_sensor = duplicate_entry.model_copy(update={"sensor_id": "some-other-sensor"})
    with pytest.raises(ValueError, match="disagrees with its envelope"):
        mismatched_sensor.to_observation()

    observation_entry = RecordedEntry(
        sequence=1,
        timestamp=envelope_stamp,
        kind=EntryKind.OBSERVATION,
        source_type=observation.source_type,
        sensor_id=observation.sensor_id,
        scenario_id=observation.scenario_id,
        payload=observation.model_dump(mode="json"),
    )
    with pytest.raises(ValueError, match="disagrees with its envelope"):
        observation_entry.to_observation()


def test_jsonl_and_parquet_preserve_v2_kinds_and_version(tmp_path: Path) -> None:
    scenario = load_scenario("10")

    jsonl_path = tmp_path / "scenario10.jsonl"
    jsonl_recorder = JsonlRecorder(jsonl_path)
    run_scenario(scenario, recorder=jsonl_recorder)
    jsonl_recorder.close()

    parquet_path = tmp_path / "scenario10.parquet"
    parquet_recorder = ParquetRecorder(parquet_path)
    run_scenario(scenario, recorder=parquet_recorder)
    parquet_recorder.close()

    jsonl_entries = list(JsonlReplaySource(jsonl_path).entries())
    parquet_entries = list(ParquetReplaySource(parquet_path).entries())

    jsonl_kinds = {entry.kind for entry in jsonl_entries}
    assert EntryKind.DUPLICATE_OBSERVATION in jsonl_kinds
    assert EntryKind.RUN_END in jsonl_kinds
    assert all(entry.format_version == 2 for entry in jsonl_entries)
    assert all(entry.format_version == 2 for entry in parquet_entries)

    jsonl_dump = [entry.model_dump(mode="json") for entry in jsonl_entries]
    parquet_dump = [entry.model_dump(mode="json") for entry in parquet_entries]
    assert jsonl_dump == parquet_dump

    assert jsonl_entries[-1].kind == EntryKind.RUN_END
    assert parquet_entries[-1].kind == EntryKind.RUN_END


def test_exported_schema_matches_recording_contract() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/export_schemas.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    schema_path = REPO_ROOT / "schemas" / "json" / "recorded_entry.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    assert schema["properties"]["format_version"]["enum"] == [1, 2]
    assert "DUPLICATE_OBSERVATION" in schema["$defs"]["EntryKind"]["enum"]


def test_in_memory_source_order_keeps_run_end_last_and_duplicates_after_their_original() -> None:
    scenario = load_scenario("10")
    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)

    ordered = list(InMemoryReplaySource(recorder.entries).entries())
    assert ordered[-1].kind == EntryKind.RUN_END

    first_observation_at: dict[str, int] = {}
    for index, entry in enumerate(ordered):
        if entry.kind == EntryKind.OBSERVATION:
            first_observation_at.setdefault(entry.payload["observation_id"], index)

    duplicate_count = 0
    for index, entry in enumerate(ordered):
        if entry.kind == EntryKind.DUPLICATE_OBSERVATION:
            duplicate_count += 1
            original_index = first_observation_at[entry.payload["observation_id"]]
            assert original_index < index
    assert duplicate_count > 0
