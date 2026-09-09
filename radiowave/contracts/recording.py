"""Recorder / replay entry contract.

A recording is an ordered sequence of :class:`RecordedEntry` values. Each entry
preserves the timestamp, entry kind, sensor, the full normalized payload
(including store-frame coordinates, confidence and retained native metadata)
and the scenario identifier, so any experiment can be replayed without hardware.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import Field

from radiowave.contracts._base import ContractModel, UtcDatetime
from radiowave.contracts.observations import (
    AnyObservation,
    ItemObservation,
    PersonObservation,
    SensorObservation,
    VisionEvidence,
)
from radiowave.contracts.store import SourceType

RECORDING_FORMAT_VERSION = 1


class EntryKind(StrEnum):
    OBSERVATION = "OBSERVATION"
    RETAIL_EVENT = "RETAIL_EVENT"
    DECISION = "DECISION"
    CART_EVENT = "CART_EVENT"
    GROUND_TRUTH = "GROUND_TRUTH"
    STORE_TWIN = "STORE_TWIN"


class RecordedEntry(ContractModel):
    sequence: int = Field(ge=0)
    timestamp: UtcDatetime
    kind: EntryKind
    source_type: SourceType | None = None
    sensor_id: str | None = None
    scenario_id: str | None = None
    format_version: int = RECORDING_FORMAT_VERSION
    payload: dict[str, Any]

    @classmethod
    def from_observation(
        cls, sequence: int, observation: SensorObservation, scenario_id: str | None = None
    ) -> RecordedEntry:
        return cls(
            sequence=sequence,
            timestamp=observation.timestamp,
            kind=EntryKind.OBSERVATION,
            source_type=observation.source_type,
            sensor_id=observation.sensor_id,
            scenario_id=scenario_id or observation.scenario_id,
            payload=observation.model_dump(mode="json"),
        )

    def to_observation(self) -> AnyObservation:
        if self.kind != EntryKind.OBSERVATION or self.source_type is None:
            msg = f"entry {self.sequence} is not an observation"
            raise ValueError(msg)
        if self.source_type == SourceType.MMWAVE:
            return PersonObservation.model_validate(self.payload)
        if self.source_type == SourceType.RFID:
            return ItemObservation.model_validate(self.payload)
        return VisionEvidence.model_validate(self.payload)
