"""RAIN UHF RFID adapter contract.

A read point is one antenna (or one localization cell) registered in the twin
as a ``Sensor`` of modality RFID. Readers with several antennas expose several
read points; the reader/port naming stays in ``Sensor.vendor_metadata`` and the
sample's ``antenna_port`` is retained only as observation metadata.

If a vendor provides its own location estimate it is reported in the read
point's frame with the vendor's stated accuracy. Mock adapters must report the
*configured* accuracy, never a better one.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from pydantic import Field

from radiowave.contracts._base import FrozenModel, UnitInterval, UtcDatetime
from radiowave.contracts.geometry import SensorCoordinate


class NativeRfidRead(FrozenModel):
    """One EPC read (or short read aggregate) from one read point."""

    sensor_id: str = Field(min_length=1, description="Read-point sensor id (ours)")
    sequence: int = Field(ge=0)
    timestamp: UtcDatetime
    epc_hex: str = Field(min_length=8)
    antenna_port: str = Field(description="Vendor antenna/port label; metadata only")
    rssi_dbm: float
    phase_rad: float | None = None
    read_rate_hz: float | None = Field(default=None, ge=0.0)
    confidence: UnitInterval
    estimate: SensorCoordinate | None = Field(
        default=None, description="Location estimate in the read-point frame, if localizing"
    )
    estimate_sigma_m: float | None = Field(
        default=None, ge=0.0, description="1-sigma accuracy of the estimate"
    )


class RfidSource(Protocol):
    def reads(self) -> Iterator[NativeRfidRead]: ...
