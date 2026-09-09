"""Recorders: in-memory, JSONL and Parquet.

Every recorder receives :class:`~radiowave.contracts.recording.RecordedEntry`
values and persists them losslessly, so a recording made from mock sensors today
and from real sensors later can be replayed through the same pipeline.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from radiowave.contracts.observations import SensorObservation
from radiowave.contracts.recording import EntryKind, RecordedEntry

_PARQUET_COLUMNS = (
    "sequence",
    "timestamp",
    "kind",
    "source_type",
    "sensor_id",
    "scenario_id",
    "format_version",
    "payload_json",
)


class InMemoryRecorder:
    def __init__(self) -> None:
        self.entries: list[RecordedEntry] = []
        self._sequence = 0

    def record(self, entry: RecordedEntry) -> None:
        self.entries.append(entry)

    def record_observation(
        self, observation: SensorObservation, scenario_id: str | None = None
    ) -> RecordedEntry:
        entry = RecordedEntry.from_observation(self._sequence, observation, scenario_id)
        self._sequence += 1
        self.record(entry)
        return entry

    def record_payload(
        self,
        kind: EntryKind,
        timestamp: datetime,
        payload: dict[str, Any],
        scenario_id: str | None = None,
        sensor_id: str | None = None,
    ) -> RecordedEntry:
        entry = RecordedEntry(
            sequence=self._sequence,
            timestamp=timestamp,
            kind=kind,
            sensor_id=sensor_id,
            scenario_id=scenario_id,
            payload=payload,
        )
        self._sequence += 1
        self.record(entry)
        return entry

    def close(self) -> None:
        return None


class JsonlRecorder(InMemoryRecorder):
    """One JSON object per line; streamed to disk as entries arrive."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("w", encoding="utf-8")

    def record(self, entry: RecordedEntry) -> None:
        self._handle.write(entry.model_dump_json())
        self._handle.write("\n")

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()


class ParquetRecorder(InMemoryRecorder):
    """Buffers entries and writes a single Parquet file on close."""

    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def close(self) -> None:
        import pyarrow as pa
        import pyarrow.parquet as pq

        rows: dict[str, list[Any]] = {column: [] for column in _PARQUET_COLUMNS}
        for entry in self.entries:
            rows["sequence"].append(entry.sequence)
            rows["timestamp"].append(entry.timestamp.isoformat())
            rows["kind"].append(entry.kind.value)
            rows["source_type"].append(entry.source_type.value if entry.source_type else None)
            rows["sensor_id"].append(entry.sensor_id)
            rows["scenario_id"].append(entry.scenario_id)
            rows["format_version"].append(entry.format_version)
            rows["payload_json"].append(json.dumps(entry.payload, sort_keys=True))
        table = pa.table(rows)
        pq.write_table(table, self.path)
