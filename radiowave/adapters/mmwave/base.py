"""mmWave people-tracking adapter contract.

A concrete adapter (TI, Infineon, anything else) only has to produce
:class:`NativeRadarSample` values in its *own* sensor frame. Normalization into
the store frame and canonical track identity happen outside the adapter, so
no vendor detail leaks past this module.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from pydantic import Field

from radiowave.contracts._base import FrozenModel, UnitInterval, UtcDatetime
from radiowave.contracts.geometry import SensorCoordinate, Velocity


class NativeRadarSample(FrozenModel):
    """One tracked-point sample as reported by a radar, in the radar's frame."""

    sensor_id: str = Field(min_length=1, description="Our sensor id, not the vendor's")
    sequence: int = Field(ge=0, description="Monotonic per-sensor sample counter")
    timestamp: UtcDatetime
    native_track_id: str = Field(
        min_length=1, description="Vendor track number; continuity hint only"
    )
    position: SensorCoordinate
    velocity: Velocity | None = Field(default=None, description="In the sensor frame")
    track_confidence: UnitInterval
    sigma_m: float = Field(ge=0.0, description="Reported 1-sigma position accuracy")
    snr_db: float | None = None


class MmWaveSource(Protocol):
    """Anything that yields radar samples in timestamp order."""

    def samples(self) -> Iterator[NativeRadarSample]: ...
