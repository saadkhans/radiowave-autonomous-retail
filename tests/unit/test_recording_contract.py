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
from datetime import timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from radiowave.contracts import EPC, ItemObservation
from radiowave.contracts.recording import (
    CURRENT_RECORDING_FORMAT_VERSION,
    LEGACY_RECORDING_FORMAT_VERSION,
    RECORDING_FORMAT_VERSION,
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
from radiowave.simulator.runner import (
    build_pipeline,
    run_scenario,
    scenario_observation_stream,
    scenario_observations,
)
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


def _v2_only_entry(kind: EntryKind, format_version: int) -> RecordedEntry:
    """A well-formed entry of a v2-only kind (typed payload, sensor named for inputs)."""
    if kind == EntryKind.DUPLICATE_OBSERVATION:
        observation = _item_observation(t=0.0)
        return RecordedEntry(
            sequence=0,
            timestamp=at(0.0),
            kind=kind,
            source_type=observation.source_type,
            sensor_id=observation.sensor_id,
            format_version=format_version,  # type: ignore[arg-type]
            payload=observation.model_dump(mode="json"),
        )
    payload: dict[str, object] = (
        {"timestamp": at(0.0).isoformat()} if kind == EntryKind.CLOCK else {"advance": True}
    )
    return RecordedEntry(
        sequence=0,
        timestamp=at(0.0),
        kind=kind,
        format_version=format_version,  # type: ignore[arg-type]
        payload=payload,
    )


@pytest.mark.parametrize(
    "kind", [EntryKind.CLOCK, EntryKind.RUN_END, EntryKind.DUPLICATE_OBSERVATION]
)
def test_v2_clock_and_run_end_entries_validate_and_v1_rejects_them(kind: EntryKind) -> None:
    ok = _v2_only_entry(kind, 2)
    assert ok.format_version == 2 and ok.kind == kind

    with pytest.raises(ValidationError, match="requires recording format_version >= 2"):
        _v2_only_entry(kind, 1)


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
    # The reader reports the offending line and the reason as one RecordingError.
    with pytest.raises(RecordingError, match=r"line 1: .*unsupported recording format_version"):
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

    late = _item_observation(t=scenario.duration_s + 1.0)
    extra = RecordedEntry.from_observation(len(entries), late)
    assert extra.timestamp >= entries[-1].timestamp
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
        format_version=2,
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
    assert schema["properties"]["format_version"]["default"] == 1
    assert schema["properties"]["format_version"]["enum"] == [1, 2]
    assert {"CLOCK", "DUPLICATE_OBSERVATION", "RUN_END"} <= set(
        schema["$defs"]["EntryKind"]["enum"]
    )


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


# --------------------------------------------------------------------------- v1/v2 compatibility


def test_legacy_row_without_format_version_reads_as_v1(tmp_path: Path) -> None:
    raw = {
        "sequence": 0,
        "timestamp": at(0.0).isoformat(),
        "kind": "GROUND_TRUTH",
        "payload": {},
    }
    entry = RecordedEntry.model_validate(raw)
    assert entry.format_version == LEGACY_RECORDING_FORMAT_VERSION

    jsonl_path = tmp_path / "legacy_no_version.jsonl"
    jsonl_path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    entries = list(JsonlReplaySource(jsonl_path).entries())
    assert len(entries) == 1
    assert entries[0].format_version == 1


def test_legacy_stream_mixing_omitted_and_explicit_v1_is_valid(tmp_path: Path) -> None:
    rows = [
        {"sequence": 0, "timestamp": at(0.0).isoformat(), "kind": "GROUND_TRUTH", "payload": {}},
        {
            "sequence": 1,
            "timestamp": at(1.0).isoformat(),
            "kind": "GROUND_TRUTH",
            "payload": {},
            "format_version": 1,
        },
        {"sequence": 2, "timestamp": at(2.0).isoformat(), "kind": "GROUND_TRUTH", "payload": {}},
    ]
    entries = [RecordedEntry.model_validate(row) for row in rows]
    validated = list(validate_recording(entries))
    assert validated == entries
    assert all(entry.format_version == 1 for entry in validated)

    # A full legacy replay: strip format_version from every other row before writing.
    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)
    entries_v2 = recorder.entries
    v2_only = {EntryKind.CLOCK, EntryKind.RUN_END, EntryKind.DUPLICATE_OBSERVATION}
    v1_entries = [
        RecordedEntry(**{**entry.model_dump(), "format_version": 1})
        for entry in entries_v2
        if entry.kind not in v2_only
    ]
    assert v1_entries

    lines: list[str] = []
    for index, entry in enumerate(v1_entries):
        dumped = json.loads(entry.model_dump_json())
        if index % 2 == 1:
            dumped.pop("format_version", None)
        lines.append(json.dumps(dumped))
    jsonl_path = tmp_path / "legacy_mixed.jsonl"
    jsonl_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    read_back = list(JsonlReplaySource(jsonl_path).entries())
    assert all(entry.format_version == 1 for entry in read_back)

    replayed_result = build_pipeline(scenario).replay(read_back)
    legacy_result = build_pipeline(scenario).run(scenario_observation_stream(scenario))
    assert replayed_result.committed_events == legacy_result.committed_events
    assert replayed_result.cart_state == legacy_result.cart_state
    assert replayed_result.item_tracks == legacy_result.item_tracks
    assert replayed_result.steps == legacy_result.steps


def test_legacy_parquet_without_format_version_column_reads_as_v1(tmp_path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    from radiowave.replay.recorder import _PARQUET_COLUMNS

    columns = [c for c in _PARQUET_COLUMNS if c != "format_version"]
    rows = {
        "sequence": [0, 1],
        "timestamp": [at(0.0).isoformat(), at(1.0).isoformat()],
        "kind": ["STORE_TWIN", "GROUND_TRUTH"],
        "source_type": [None, None],
        "sensor_id": [None, None],
        "scenario_id": ["unit", "unit"],
        "payload_json": [json.dumps({}), json.dumps({})],
    }
    assert set(rows) == set(columns)
    table = pa.table(rows)
    path = tmp_path / "legacy_no_column.parquet"
    pq.write_table(table, path)

    entries = list(ParquetReplaySource(path).entries())
    assert len(entries) == 2
    assert all(entry.format_version == 1 for entry in entries)


def test_recording_format_version_alias_is_import_compatible() -> None:
    assert RECORDING_FORMAT_VERSION == CURRENT_RECORDING_FORMAT_VERSION == 2
    assert LEGACY_RECORDING_FORMAT_VERSION == 1


def test_every_new_writer_path_declares_v2(tmp_path: Path) -> None:
    scenario = load_scenario("05")
    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)
    entries = recorder.entries
    assert {entry.format_version for entry in entries} == {2}

    observation = _item_observation()
    from_observation_entry = RecordedEntry.from_observation(0, observation)
    assert from_observation_entry.format_version == 2

    payload_entry = InMemoryRecorder().record_payload(EntryKind.GROUND_TRUTH, at(0.0), {})
    assert payload_entry.format_version == 2

    jsonl_path = tmp_path / "writer_v2.jsonl"
    jsonl_recorder = JsonlRecorder(jsonl_path)
    for entry in entries:
        jsonl_recorder.record(entry)
    jsonl_recorder.close()
    jsonl_entries = list(open_replay_source(jsonl_path).entries())
    assert {entry.format_version for entry in jsonl_entries} == {2}

    parquet_path = tmp_path / "writer_v2.parquet"
    parquet_recorder = ParquetRecorder(parquet_path)
    for entry in entries:
        parquet_recorder.record(entry)
    parquet_recorder.close()
    parquet_entries = list(open_replay_source(parquet_path).entries())
    assert {entry.format_version for entry in parquet_entries} == {2}

    raw_lines = [line for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line]
    assert raw_lines
    assert all('"format_version":2' in line for line in raw_lines)


# --------------------------------------------------------------------------- RUN_END required (v2)


def _header_only_entries(scenario_id: str = "unit") -> list[RecordedEntry]:
    return [
        RecordedEntry(
            sequence=0,
            timestamp=at(0.0),
            kind=EntryKind.STORE_TWIN,
            scenario_id=scenario_id,
            format_version=2,
            payload={},
        ),
        RecordedEntry(
            sequence=1,
            timestamp=at(0.0),
            kind=EntryKind.PIPELINE_CONFIG,
            scenario_id=scenario_id,
            format_version=2,
            payload={},
        ),
    ]


def _case_header_only() -> tuple:
    scenario = load_scenario("01")
    return scenario, _header_only_entries(scenario.scenario_id)


def _case_header_plus_partial_observations() -> tuple:
    scenario = load_scenario("05")
    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)
    entries = recorder.entries
    observation_indices = [i for i, e in enumerate(entries) if e.kind == EntryKind.OBSERVATION]
    cutoff = observation_indices[len(observation_indices) // 2]
    return scenario, entries[: cutoff + 1]


def _case_truncated_after_last_clock() -> tuple:
    scenario = load_scenario("04")
    recorder = InMemoryRecorder()
    pipeline = build_pipeline(scenario, recorder=recorder)
    cutoff = scenario.at(scenario.duration_s - 4.0)
    for observation in scenario_observations(scenario):
        if observation.timestamp <= cutoff:
            pipeline.ingest(observation)
    pipeline.advance_to(scenario.at(scenario.duration_s))
    pipeline.finish(advance=False)
    entries = recorder.entries
    last_clock_index = max(i for i, e in enumerate(entries) if e.kind == EntryKind.CLOCK)
    return scenario, entries[: last_clock_index + 1]


def _case_missing_run_end() -> tuple:
    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)
    entries = recorder.entries
    assert entries[-1].kind == EntryKind.RUN_END
    return scenario, entries[:-1]


TRUNCATED_V2_CASES = {
    "header only": _case_header_only,
    "header plus partial observations": _case_header_plus_partial_observations,
    "truncated after last clock": _case_truncated_after_last_clock,
    "missing run end": _case_missing_run_end,
}


@pytest.mark.parametrize("case_name", list(TRUNCATED_V2_CASES), ids=list(TRUNCATED_V2_CASES))
def test_v2_stream_without_run_end_is_rejected(case_name: str, tmp_path: Path) -> None:
    scenario, entries = TRUNCATED_V2_CASES[case_name]()
    assert not any(entry.kind == EntryKind.RUN_END for entry in entries)

    with pytest.raises(RecordingError, match="ended without RUN_END"):
        list(validate_recording(entries))

    pipeline = build_pipeline(scenario)
    with pytest.raises(RecordingError, match="ended without RUN_END"):
        pipeline.replay(entries)
    assert pipeline.finished is False

    stem = case_name.replace(" ", "_")

    jsonl_path = tmp_path / f"{stem}.jsonl"
    jsonl_recorder = JsonlRecorder(jsonl_path)
    for entry in entries:
        jsonl_recorder.record(entry)
    jsonl_recorder.close()
    jsonl_entries = list(open_replay_source(jsonl_path).entries())
    with pytest.raises(RecordingError, match="ended without RUN_END"):
        list(validate_recording(jsonl_entries))
    jsonl_pipeline = build_pipeline(scenario)
    with pytest.raises(RecordingError, match="ended without RUN_END"):
        jsonl_pipeline.replay(jsonl_entries)
    assert jsonl_pipeline.finished is False

    parquet_path = tmp_path / f"{stem}.parquet"
    parquet_recorder = ParquetRecorder(parquet_path)
    for entry in entries:
        parquet_recorder.record(entry)
    parquet_recorder.close()
    parquet_entries = list(open_replay_source(parquet_path).entries())
    with pytest.raises(RecordingError, match="ended without RUN_END"):
        list(validate_recording(parquet_entries))
    parquet_pipeline = build_pipeline(scenario)
    with pytest.raises(RecordingError, match="ended without RUN_END"):
        parquet_pipeline.replay(parquet_entries)
    assert parquet_pipeline.finished is False


def test_v1_stream_without_run_end_is_complete() -> None:
    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    run_scenario(scenario, recorder=recorder)
    entries_v2 = recorder.entries
    v2_only = {EntryKind.CLOCK, EntryKind.RUN_END, EntryKind.DUPLICATE_OBSERVATION}
    v1_entries = [
        RecordedEntry(**{**entry.model_dump(), "format_version": 1})
        for entry in entries_v2
        if entry.kind not in v2_only
    ]
    assert not any(entry.kind == EntryKind.RUN_END for entry in v1_entries)
    # A v1 stream legitimately ends at EOF: no RUN_END required.
    assert list(validate_recording(v1_entries)) == v1_entries


# --------------------------------------------------------------------------- ordering


def _base_observation_entry(sequence: int, timestamp) -> RecordedEntry:
    observation = _item_observation(t=0.0)
    return RecordedEntry(
        sequence=sequence,
        timestamp=timestamp,
        kind=EntryKind.OBSERVATION,
        source_type=observation.source_type,
        sensor_id=observation.sensor_id,
        format_version=2,
        payload=observation.model_dump(mode="json"),
    )


def _run_end_entry(sequence: int, timestamp) -> RecordedEntry:
    return RecordedEntry(
        sequence=sequence,
        timestamp=timestamp,
        kind=EntryKind.RUN_END,
        format_version=2,
        payload={"advance": True},
    )


def _duplicate_entry(sequence: int, envelope_timestamp, payload_timestamp) -> RecordedEntry:
    observation = _item_observation(t=0.0).model_copy(update={"timestamp": payload_timestamp})
    return RecordedEntry(
        sequence=sequence,
        timestamp=envelope_timestamp,
        kind=EntryKind.DUPLICATE_OBSERVATION,
        source_type=observation.source_type,
        sensor_id=observation.sensor_id,
        format_version=2,
        payload=observation.model_dump(mode="json"),
    )


@pytest.mark.parametrize(
    ("build_entries", "expected_error"),
    [
        pytest.param(
            lambda: [
                _base_observation_entry(0, at(1.0)),
                _base_observation_entry(1, at(1.0)),
                _run_end_entry(2, at(1.0)),
            ],
            None,
            id="equal_timestamps_pass",
        ),
        pytest.param(
            lambda: [
                _base_observation_entry(0, at(1.0)),
                _base_observation_entry(1, at(2.0)),
                _run_end_entry(2, at(2.0)),
            ],
            None,
            id="increasing_timestamps_pass",
        ),
        pytest.param(
            lambda: [
                _base_observation_entry(0, at(2.0)),
                _base_observation_entry(1, at(1.0)),
            ],
            "never move backwards",
            id="timestamp_regression_fails",
        ),
        pytest.param(
            lambda: [
                _base_observation_entry(0, at(1.0)),
                _base_observation_entry(1, at(2.0)),
                _base_observation_entry(2, at(3.0)),
                _run_end_entry(3, at(3.0)),
            ],
            None,
            id="sequence_increasing_passes",
        ),
        pytest.param(
            lambda: [
                _base_observation_entry(0, at(1.0)),
                _base_observation_entry(0, at(2.0)),
            ],
            "strictly increase",
            id="duplicate_sequence_fails",
        ),
        pytest.param(
            lambda: [
                _base_observation_entry(1, at(1.0)),
                _base_observation_entry(0, at(2.0)),
            ],
            "strictly increase",
            id="sequence_regression_fails",
        ),
        pytest.param(
            lambda: [
                _base_observation_entry(0, at(1.0)),
                _duplicate_entry(1, at(2.0), at(2.0) + timedelta(seconds=60)),
                _run_end_entry(2, at(2.0)),
            ],
            None,
            id="duplicate_with_future_payload_timestamp_passes",
        ),
        pytest.param(
            lambda: [
                _base_observation_entry(0, at(1.0)),
                _duplicate_entry(1, at(2.0), at(0.0)),
                _run_end_entry(2, at(2.0)),
            ],
            None,
            id="duplicate_with_stale_payload_timestamp_passes",
        ),
    ],
)
def test_non_monotonic_streams_are_rejected(build_entries, expected_error: str | None) -> None:
    entries = build_entries()
    if expected_error is None:
        assert list(validate_recording(entries)) == entries
    else:
        with pytest.raises(RecordingError, match=expected_error):
            list(validate_recording(entries))


# --------------------------------------------------------------------------- typed control payloads


@pytest.mark.parametrize(
    "advance_value",
    ["false", "true", 0, 1, None],
    ids=["str-false", "str-true", "int-0", "int-1", "none"],
)
def test_run_end_advance_must_be_a_strict_boolean(advance_value: object) -> None:
    with pytest.raises(ValidationError, match="RUN_END payload"):
        RecordedEntry(
            sequence=0,
            timestamp=at(0.0),
            kind=EntryKind.RUN_END,
            format_version=2,
            payload={"advance": advance_value},
        )


def test_run_end_advance_missing_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="RUN_END payload"):
        RecordedEntry(
            sequence=0,
            timestamp=at(0.0),
            kind=EntryKind.RUN_END,
            format_version=2,
            payload={},
        )


def test_run_end_advance_extra_key_is_rejected() -> None:
    with pytest.raises(ValidationError, match="RUN_END payload"):
        RecordedEntry(
            sequence=0,
            timestamp=at(0.0),
            kind=EntryKind.RUN_END,
            format_version=2,
            payload={"advance": True, "extra": 1},
        )


@pytest.mark.parametrize("value", [True, False])
def test_run_end_advance_accepts_strict_booleans(value: bool) -> None:
    entry = RecordedEntry(
        sequence=0,
        timestamp=at(0.0),
        kind=EntryKind.RUN_END,
        format_version=2,
        payload={"advance": value},
    )
    assert entry.run_end_advance is value


def test_run_end_advance_string_false_via_jsonl_is_rejected(tmp_path: Path) -> None:
    raw = {
        "sequence": 0,
        "timestamp": at(0.0).isoformat(),
        "kind": "RUN_END",
        "format_version": 2,
        "payload": {"advance": "false"},
    }
    path = tmp_path / "bad_run_end.jsonl"
    path.write_text(json.dumps(raw) + "\n", encoding="utf-8")
    with pytest.raises(RecordingError, match="RUN_END payload"):
        list(JsonlReplaySource(path).entries())


def test_clock_payload_must_match_envelope() -> None:
    envelope = at(5.0)
    ok_same_form = RecordedEntry(
        sequence=0,
        timestamp=envelope,
        kind=EntryKind.CLOCK,
        format_version=2,
        payload={"timestamp": envelope.isoformat()},
    )
    assert ok_same_form.kind == EntryKind.CLOCK

    # An equivalent instant expressed with a "+02:00" offset form.
    tz_plus2 = timezone(timedelta(hours=2))
    offset_form = envelope.astimezone(tz_plus2).isoformat()
    ok_offset_form = RecordedEntry(
        sequence=1,
        timestamp=envelope,
        kind=EntryKind.CLOCK,
        format_version=2,
        payload={"timestamp": offset_form},
    )
    assert ok_offset_form.kind == EntryKind.CLOCK

    with pytest.raises(ValidationError, match="disagrees with the envelope"):
        RecordedEntry(
            sequence=2,
            timestamp=envelope,
            kind=EntryKind.CLOCK,
            format_version=2,
            payload={"timestamp": (envelope + timedelta(seconds=1)).isoformat()},
        )

    with pytest.raises(ValidationError, match="CLOCK payload"):
        RecordedEntry(
            sequence=3, timestamp=envelope, kind=EntryKind.CLOCK, format_version=2, payload={}
        )

    with pytest.raises(ValidationError, match="CLOCK payload"):
        RecordedEntry(
            sequence=4,
            timestamp=envelope,
            kind=EntryKind.CLOCK,
            format_version=2,
            payload={"timestamp": envelope.isoformat(), "extra": 1},
        )

    with pytest.raises(ValidationError, match="is invalid"):
        RecordedEntry(
            sequence=5,
            timestamp=envelope,
            kind=EntryKind.CLOCK,
            format_version=2,
            payload={"timestamp": "2026-01-01T00:00:05"},  # naive: rejected by ensure_utc
        )


@pytest.mark.parametrize("kind", [EntryKind.OBSERVATION, EntryKind.DUPLICATE_OBSERVATION])
@pytest.mark.parametrize("missing", ["source_type", "sensor_id"])
def test_input_entries_must_name_sensor_and_modality(kind: EntryKind, missing: str) -> None:
    observation = _item_observation()
    fields: dict[str, object] = {
        "sequence": 0,
        "timestamp": observation.timestamp,
        "kind": kind,
        "source_type": observation.source_type,
        "sensor_id": observation.sensor_id,
        "format_version": 2,
        "payload": observation.model_dump(mode="json"),
    }
    fields[missing] = None
    with pytest.raises(ValidationError, match="must name sensor and modality"):
        RecordedEntry(**fields)
