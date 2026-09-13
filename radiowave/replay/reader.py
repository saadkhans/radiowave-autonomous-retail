"""Replay sources: in-memory, JSONL and Parquet, plus a paced player."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from pydantic import ValidationError

from radiowave.contracts.observations import AnyObservation
from radiowave.contracts.recording import EntryKind, RecordedEntry, RecordingError
from radiowave.replay.clock import ReplayPacer

# Every column a recording must carry; ``format_version`` is optional (absent = v1).
_REQUIRED_PARQUET_COLUMNS = frozenset(
    {"sequence", "timestamp", "kind", "source_type", "sensor_id", "scenario_id", "payload_json"}
)


class InMemoryReplaySource:
    def __init__(self, entries: Iterable[RecordedEntry]) -> None:
        self._entries = list(entries)

    def entries(self) -> Iterator[RecordedEntry]:
        yield from sorted(self._entries, key=lambda e: (e.timestamp, e.sequence))


class JsonlReplaySource:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def entries(self) -> Iterator[RecordedEntry]:
        with self.path.open("r", encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield RecordedEntry.model_validate_json(line)
                except ValidationError as exc:
                    msg = f"{self.path} line {number}: {_first_error(exc)}"
                    raise RecordingError(msg) from exc


class ParquetReplaySource:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def entries(self) -> Iterator[RecordedEntry]:
        import pyarrow.parquet as pq

        try:
            table = pq.read_table(self.path)
        except Exception as exc:  # pyarrow raises ArrowInvalid / OSError subclasses
            msg = f"{self.path}: not a readable Parquet recording: {exc}"
            raise RecordingError(msg) from exc
        missing = _REQUIRED_PARQUET_COLUMNS.difference(table.column_names)
        if missing:
            msg = f"{self.path}: recording is missing columns {sorted(missing)}"
            raise RecordingError(msg)
        for index, row in enumerate(table.to_pylist()):
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, ValueError) as exc:
                msg = f"{self.path} row {index}: payload_json is not valid JSON: {exc}"
                raise RecordingError(msg) from exc
            if not isinstance(payload, dict):
                msg = f"{self.path} row {index}: payload_json must encode an object"
                raise RecordingError(msg)
            fields = {
                "sequence": row["sequence"],
                "timestamp": row["timestamp"],
                "kind": row["kind"],
                "source_type": row["source_type"],
                "sensor_id": row["sensor_id"],
                "scenario_id": row["scenario_id"],
                "payload": payload,
            }
            # A legacy file written without the column, or a null cell, is v1 (the read
            # default); an explicit value is validated like any other.
            if row.get("format_version") is not None:
                fields["format_version"] = row["format_version"]
            try:
                yield RecordedEntry.model_validate(fields)
            except ValidationError as exc:
                msg = f"{self.path} row {index}: {_first_error(exc)}"
                raise RecordingError(msg) from exc


def _first_error(exc: ValidationError) -> str:
    """The first validation message, without pydantic's multi-line report."""
    errors = exc.errors()
    if not errors:
        return str(exc)
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid"))
    return f"{location}: {message}" if location else message


def open_replay_source(path: str | Path) -> JsonlReplaySource | ParquetReplaySource:
    path = Path(path)
    if path.suffix.lower() == ".parquet":
        return ParquetReplaySource(path)
    return JsonlReplaySource(path)


def observations_from(entries: Iterable[RecordedEntry]) -> Iterator[AnyObservation]:
    for entry in entries:
        if entry.kind == EntryKind.OBSERVATION:
            yield entry.to_observation()


class ReplayPlayer:
    """Iterates a source at a given rate. ``rate=0`` streams without waiting.

    Step mode: iterate with ``next()`` and the caller decides when the next
    entry is released; no wall-clock pacing is applied.
    """

    def __init__(
        self,
        source: InMemoryReplaySource | JsonlReplaySource | ParquetReplaySource,
        pacer: ReplayPacer | None = None,
    ) -> None:
        self._source = source
        self._pacer = pacer

    def __iter__(self) -> Iterator[RecordedEntry]:
        for entry in self._source.entries():
            if self._pacer is not None:
                self._pacer.wait_for(entry.timestamp)
            yield entry
