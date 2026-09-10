"""Normalized sensor observations.

Every observation carries: ``observation_id``, ``sensor_id``, ``timestamp`` (UTC),
``source_type``, ``confidence`` and, where applicable, a store-frame ``coordinate``
with explicit ``uncertainty``.

Sensor-native values (vendor track ids, antenna ports, native coordinates, SNR)
are retained in ``metadata`` for debugging and replay only. They are never used
as canonical shopper or item identity; that mapping is owned by fusion.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from radiowave.contracts._base import ContractModel, UnitInterval, UtcDatetime
from radiowave.contracts.geometry import SpatialUncertainty, Velocity, WorldCoordinate
from radiowave.contracts.store import EPC, SourceType

#: Well-known metadata keys for retained native fields.
NATIVE_TRACK_KEY = "native_track_id"
NATIVE_ANTENNA_KEY = "native_antenna"
NATIVE_POSITION_KEY = "native_position"


def _require_json(value: Any, path: str) -> None:
    if value is None or isinstance(value, bool | int | float | str):
        return
    if isinstance(value, list):
        for index, element in enumerate(value):
            _require_json(element, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, element in value.items():
            if not isinstance(key, str):
                msg = f"{path}: keys must be strings"
                raise ValueError(msg)
            _require_json(element, f"{path}.{key}")
        return
    msg = f"{path}: {type(value).__name__} is not a JSON value"
    raise ValueError(msg)


class SensorObservation(ContractModel):
    """Base class for all normalized observations."""

    observation_id: str = Field(min_length=1)
    sensor_id: str = Field(min_length=1)
    timestamp: UtcDatetime
    source_type: SourceType
    confidence: UnitInterval
    coordinate: WorldCoordinate | None = None
    uncertainty: SpatialUncertainty | None = None
    scenario_id: str | None = None
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Retained native fields; JSON-compatible values only so every observation "
        "is recordable",
    )

    @field_validator("metadata")
    @classmethod
    def _json_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        _require_json(value, "metadata")
        return value

    @model_validator(mode="after")
    def _location_is_qualified(self) -> SensorObservation:
        if (self.coordinate is None) != (self.uncertainty is None):
            msg = "coordinate and uncertainty must be given together (a location without "
            msg += "a stated accuracy is not evidence)"
            raise ValueError(msg)
        return self


class PersonObservation(SensorObservation):
    """Anonymous body/person point from a people-tracking sensor (mmWave in v0).

    No biometric or identifying information is ever attached.
    """

    source_type: Literal[SourceType.MMWAVE] = SourceType.MMWAVE
    coordinate: WorldCoordinate
    uncertainty: SpatialUncertainty
    velocity: Velocity | None = None


class ItemObservation(SensorObservation):
    """One RFID read (or read aggregate) of a specific EPC.

    ``coordinate``/``uncertainty`` are the adapter's *location estimate* at the
    configured accuracy; ``zone_id`` is the coarse read zone. Either may be absent.
    """

    source_type: Literal[SourceType.RFID] = SourceType.RFID
    epc: EPC
    zone_id: str | None = None
    rssi_dbm: float | None = None
    phase_rad: float | None = Field(default=None, ge=-6.3, le=6.3)
    read_rate_hz: float | None = Field(default=None, ge=0.0)


class VisionEvidenceKind(StrEnum):
    PERSON_AT_FIXTURE = "PERSON_AT_FIXTURE"
    REACH_INTO_FIXTURE = "REACH_INTO_FIXTURE"
    ITEM_IN_HAND = "ITEM_IN_HAND"
    ITEM_PLACED = "ITEM_PLACED"


class VisionEvidence(SensorObservation):
    """Semantic (non-biometric) evidence from a vision provider.

    ``coordinate`` is the world position of the observed person/interaction.
    Vision never identifies people; it only says *something happened here*.
    """

    source_type: Literal[SourceType.VISION] = SourceType.VISION
    kind: VisionEvidenceKind
    coordinate: WorldCoordinate
    fixture_id: str | None = None
    zone_id: str | None = None


class SensorHealthStatus(StrEnum):
    OK = "OK"
    DEGRADED = "DEGRADED"
    OFFLINE = "OFFLINE"


class SensorHealth(ContractModel):
    sensor_id: str = Field(min_length=1)
    timestamp: UtcDatetime
    status: SensorHealthStatus
    last_observation_at: UtcDatetime | None = None
    message: str | None = None


AnyObservation = PersonObservation | ItemObservation | VisionEvidence
