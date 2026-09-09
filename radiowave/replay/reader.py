"""Replay sources: in-memory, JSONL and Parquet, plus a paced player."""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from pathlib import Path

from radiowave.contracts.observations import AnyObservation
from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.replay.clock import ReplayPacer


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
            for line in handle:
                line = line.strip()
                if line:
                    yield RecordedEntry.model_validate_json(line)


class ParquetReplaySource:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def entries(self) -> Iterator[RecordedEntry]:
        import pyarrow.parquet as pq

        table = pq.read_table(self.path)
        for row in table.to_pylist():
            yield RecordedEntry(
                sequence=row["sequence"],
                timestamp=row["timestamp"],
                kind=row["kind"],
                source_type=row["source_type"],
                sensor_id=row["sensor_id"],
                scenario_id=row["scenario_id"],
                format_version=row["format_version"],
                payload=json.loads(row["payload_json"]),
            )


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
