"""CLI replay error handling: a malformed recording is refused with one readable
``SystemExit("invalid recording: ...")`` line, never a traceback or a half-printed
summary, in both the ordinary and ``--step`` replay modes; a well-formed v1 or v2
recording still replays and prints the usual summary.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from radiowave.cli import main
from radiowave.contracts import EPC, ItemObservation, SpatialUncertainty, WorldCoordinate
from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import PipelineConfig
from radiowave.simulator.stores import EPC_SHIRT_A, build_lab_store
from tests.conftest import at


def _item(
    t: float, x: float, y: float, sensor: str = "rfid-f1", zone: str = "zone-f1"
) -> ItemObservation:
    return ItemObservation(
        observation_id=f"rfid:{sensor}:{t:.3f}",
        sensor_id=sensor,
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        zone_id=zone,
        coordinate=WorldCoordinate(x=x, y=y, z=0.9),
        uncertainty=SpatialUncertainty.isotropic(0.5),
    )


def _dump(entries: list[RecordedEntry]) -> list[dict]:
    return [json.loads(entry.model_dump_json()) for entry in entries]


def _write(path: Path, dicts: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(d) for d in dicts) + "\n", encoding="utf-8")


def _baseline_entries() -> list[RecordedEntry]:
    """A small, well-formed v2 recording: header, two observations, a CLOCK entry and
    RUN_END, so every kind that carries a format-contract rule is represented."""
    registry = StoreRegistry(build_lab_store())
    config = PipelineConfig()
    obs0 = _item(0.0, 4.0, 6.5)
    obs1 = _item(0.1, 4.0, 6.5)
    clock_at = at(0.2)
    return [
        RecordedEntry(
            sequence=0,
            timestamp=at(0.0),
            kind=EntryKind.STORE_TWIN,
            format_version=2,
            payload=registry.store.model_dump(mode="json"),
        ),
        RecordedEntry(
            sequence=1,
            timestamp=at(0.0),
            kind=EntryKind.PIPELINE_CONFIG,
            format_version=2,
            payload=config.model_dump(mode="json"),
        ),
        RecordedEntry.from_observation(2, obs0),
        RecordedEntry.from_observation(3, obs1),
        RecordedEntry(
            sequence=4,
            timestamp=clock_at,
            kind=EntryKind.CLOCK,
            format_version=2,
            payload={"timestamp": clock_at.isoformat()},
        ),
        RecordedEntry(
            sequence=5,
            timestamp=clock_at,
            kind=EntryKind.RUN_END,
            format_version=2,
            payload={"advance": True},
        ),
    ]


def _baseline_dicts() -> list[dict]:
    return _dump(_baseline_entries())


# --------------------------------------------------------------------------- malformed cases


def _make_unsupported_version(path: Path) -> None:
    dicts = _baseline_dicts()
    dicts[0]["format_version"] = 3
    _write(path, dicts)


def _make_mixed_versions(path: Path) -> None:
    dicts = _baseline_dicts()
    dicts[0]["format_version"] = 1
    dicts[1]["format_version"] = 1
    _write(path, dicts)


def _make_missing_run_end(path: Path) -> None:
    dicts = _baseline_dicts()
    _write(path, dicts[:-1])


def _make_timestamp_regression(path: Path) -> None:
    dicts = _baseline_dicts()
    observation_indices = [i for i, d in enumerate(dicts) if d["kind"] == "OBSERVATION"]
    # The second observation's envelope moves earlier than the first's.
    earlier = at(-10.0).isoformat()
    dicts[observation_indices[1]]["timestamp"] = earlier
    _write(path, dicts)


def _make_sequence_regression(path: Path) -> None:
    dicts = _baseline_dicts()
    observation_indices = [i for i, d in enumerate(dicts) if d["kind"] == "OBSERVATION"]
    dicts[observation_indices[1]]["sequence"] = dicts[observation_indices[0]]["sequence"]
    _write(path, dicts)


def _make_run_end_advance_string_false(path: Path) -> None:
    dicts = _baseline_dicts()
    dicts[-1]["payload"] = {"advance": "false"}
    _write(path, dicts)


def _make_clock_disagrees_with_envelope(path: Path) -> None:
    dicts = _baseline_dicts()
    clock_index = next(i for i, d in enumerate(dicts) if d["kind"] == "CLOCK")
    disagreeing = (at(0.2) + timedelta(seconds=1)).isoformat()
    dicts[clock_index]["payload"] = {"timestamp": disagreeing}
    _write(path, dicts)


def _make_corrupt_observation_payload(path: Path) -> None:
    dicts = _baseline_dicts()
    first = next(d for d in dicts if d["kind"] == "OBSERVATION")
    first["payload"]["coordinate"]["x"] = "not-a-number"
    _write(path, dicts)


def _make_garbage_observation_payload(path: Path) -> None:
    dicts = _baseline_dicts()
    first = next(d for d in dicts if d["kind"] == "OBSERVATION")
    first["payload"] = {"garbage": 1}
    _write(path, dicts)


def _make_corrupt_jsonl_line(path: Path) -> None:
    dicts = _baseline_dicts()
    lines = [json.dumps(d) for d in dicts]
    lines[2] = lines[2][:-5]  # a truncated line: invalid JSON
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_garbage_store_twin_payload(path: Path) -> None:
    dicts = _baseline_dicts()
    next(d for d in dicts if d["kind"] == "STORE_TWIN")["payload"] = {"zones": "nope"}
    _write(path, dicts)


def _make_garbage_pipeline_config_payload(path: Path) -> None:
    dicts = _baseline_dicts()
    config = next(d for d in dicts if d["kind"] == "PIPELINE_CONFIG")
    config["payload"] = {"step_interval_s": "fast"}
    _write(path, dicts)


MALFORMED_CASES = {
    "garbage store twin payload": _make_garbage_store_twin_payload,
    "garbage pipeline config payload": _make_garbage_pipeline_config_payload,
    "corrupt observation payload": _make_corrupt_observation_payload,
    "garbage observation payload": _make_garbage_observation_payload,
    "corrupt jsonl line": _make_corrupt_jsonl_line,
    "unsupported version": _make_unsupported_version,
    "mixed versions": _make_mixed_versions,
    "v2 missing run end": _make_missing_run_end,
    "timestamp regression": _make_timestamp_regression,
    "sequence regression": _make_sequence_regression,
    "run end advance string false": _make_run_end_advance_string_false,
    "clock disagrees with envelope": _make_clock_disagrees_with_envelope,
}


@pytest.mark.parametrize("case_name", list(MALFORMED_CASES), ids=list(MALFORMED_CASES))
def test_cli_replay_rejects_malformed_recordings(case_name: str, tmp_path: Path, capsys) -> None:
    path = tmp_path / f"{case_name.replace(' ', '_')}.jsonl"
    MALFORMED_CASES[case_name](path)

    with pytest.raises(SystemExit) as exc_info:
        main(["replay", str(path)])

    message = str(exc_info.value.code)
    assert message.startswith("invalid recording:")
    assert len(message) > len("invalid recording:")

    captured = capsys.readouterr()
    # Never a half-printed summary: the summary always starts with this line.
    assert "scenario        :" not in captured.out


@pytest.mark.parametrize(
    "case_name",
    ["unsupported version", "v2 missing run end", "timestamp regression"],
)
def test_cli_replay_step_mode_rejects_malformed_recordings(
    case_name: str, tmp_path: Path, monkeypatch, capsys
) -> None:
    path = tmp_path / f"step_{case_name.replace(' ', '_')}.jsonl"
    MALFORMED_CASES[case_name](path)

    # Enough blank "keep going" answers to release every observation in the tiny
    # baseline recording; a malformed header is refused before stdin is ever read.
    monkeypatch.setattr(sys, "stdin", io.StringIO("\n" * 20))

    with pytest.raises(SystemExit) as exc_info:
        main(["replay", str(path), "--step"])

    message = str(exc_info.value.code)
    assert message.startswith("invalid recording:")

    captured = capsys.readouterr()
    assert "scenario        :" not in captured.out


# --------------------------------------------------------------------------- positive controls


def _write_parquet(path: Path, rows: dict[str, list]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    pq.write_table(pa.table(rows), path)


def _baseline_parquet_rows() -> dict[str, list]:
    rows: dict[str, list] = {
        column: []
        for column in (
            "sequence",
            "timestamp",
            "kind",
            "source_type",
            "sensor_id",
            "scenario_id",
            "format_version",
            "payload_json",
        )
    }
    for entry in _baseline_entries():
        rows["sequence"].append(entry.sequence)
        rows["timestamp"].append(entry.timestamp.isoformat())
        rows["kind"].append(entry.kind.value)
        rows["source_type"].append(entry.source_type.value if entry.source_type else None)
        rows["sensor_id"].append(entry.sensor_id)
        rows["scenario_id"].append(entry.scenario_id)
        rows["format_version"].append(entry.format_version)
        rows["payload_json"].append(json.dumps(entry.payload, sort_keys=True))
    return rows


def _make_parquet_corrupt_payload_json(path: Path) -> None:
    rows = _baseline_parquet_rows()
    rows["payload_json"][2] = "{not json"
    _write_parquet(path, rows)


def _make_parquet_missing_column(path: Path) -> None:
    rows = _baseline_parquet_rows()
    del rows["sensor_id"]
    _write_parquet(path, rows)


def _make_not_a_parquet_file(path: Path) -> None:
    path.write_bytes(b"this is not a parquet file")


PARQUET_MALFORMED_CASES = {
    "parquet corrupt payload json": _make_parquet_corrupt_payload_json,
    "parquet missing column": _make_parquet_missing_column,
    "not a parquet file": _make_not_a_parquet_file,
}


@pytest.mark.parametrize(
    "case_name", list(PARQUET_MALFORMED_CASES), ids=list(PARQUET_MALFORMED_CASES)
)
def test_cli_replay_rejects_malformed_parquet_recordings(
    case_name: str, tmp_path: Path, capsys
) -> None:
    path = tmp_path / f"{case_name.replace(' ', '_')}.parquet"
    PARQUET_MALFORMED_CASES[case_name](path)
    with pytest.raises(SystemExit) as excinfo:
        main(["replay", str(path)])
    assert str(excinfo.value.code).startswith("invalid recording:")
    assert "duplicates dropped" not in capsys.readouterr().out


def test_cli_replay_accepts_a_valid_v2_recording(tmp_path: Path, capsys) -> None:
    path = tmp_path / "valid_v2.jsonl"
    _write(path, _baseline_dicts())

    exit_code = main(["replay", str(path)])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "scenario        :" in captured.out
    assert "duplicates dropped" in captured.out


def test_cli_replay_accepts_a_valid_v1_recording(tmp_path: Path, capsys) -> None:
    # A v1 recording predates CLOCK/RUN_END and legitimately ends at EOF.
    entries = _baseline_entries()
    v1_only = [e for e in entries if e.kind not in (EntryKind.CLOCK, EntryKind.RUN_END)]
    dicts = [{**d, "format_version": 1} for d in _dump(v1_only)]
    path = tmp_path / "valid_v1.jsonl"
    _write(path, dicts)

    exit_code = main(["replay", str(path)])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "scenario        :" in captured.out
    assert "duplicates dropped" in captured.out
