"""mmWave people-tracking adapter contract.

A concrete adapter (TI, Infineon, anything else) only has to produce
:class:`NativeRadarSample` values in its *own* sensor frame. Normalization into
the store frame and canonical track identity happen outside the adapter, so
no vendor detail leaks past this module.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from pydantic import Field, field_validator

from radiowave.contracts._base import FrozenModel, UnitInterval, UtcDatetime
from radiowave.contracts.geometry import SensorCoordinate, Velocity
from radiowave.contracts.observations import PersonObservation


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
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Vendor-neutral native provenance (frame counters, stream generation, "
        "firmware mode, raw native values) carried into the observation's metadata; JSON "
        "values only, never used as identity",
    )

    @field_validator("metadata")
    @classmethod
    def _json_only(cls, value: dict[str, Any]) -> dict[str, Any]:
        # Same rule as SensorObservation.metadata: every sample must be recordable.
        from radiowave.contracts.observations import _require_json

        _require_json(value, "metadata")
        return value


class MmWaveSource(Protocol):
    """Anything that yields radar samples in timestamp order."""

    def samples(self) -> Iterator[NativeRadarSample]: ...


@runtime_checkable
class LivePeopleSource(Protocol):
    """Lifecycle surface a live consumer needs from any radar people source.

    Vendor-neutral counterpart to :class:`MmWaveSource`: covers exactly the methods
    a live driver (``radiowave.api.live.LiveObservatoryRun``) calls on its sensor
    session, so a hardware adapter is substitutable with a mock/replay people source
    without leaking vendor-specific types past this boundary (architecture
    invariant 1: vendor-neutral, replaceable adapters).

    Deferred: this documents the boundary rather than enforcing it. Annotating
    ``LiveObservatoryRun.session`` against this protocol is what would make mypy
    check it, but ``diagnostics()`` is typed ``object`` here because its real
    return, ``TiAdapterDiagnostics``, lives in the TI package — importing it would
    put a vendor type back in this module. Its *content* is already vendor-neutral
    (a radar stream state machine plus counters), so the fix is to extract it into
    a shared diagnostics contract that both this protocol and the TI adapter use,
    then tighten this signature and annotate the consumer. That touches every
    diagnostics consumer and test, so it belongs in its own change.
    """

    @property
    def sensor_id(self) -> str: ...

    @property
    def running(self) -> bool: ...

    def start(self) -> None: ...

    def stop(self, timeout_s: float = 5.0) -> bool: ...

    def request_reconnect(self) -> bool: ...

    def clock_now(self) -> datetime: ...

    def drain_with_clock(self) -> tuple[list[PersonObservation], datetime]: ...

    def diagnostics(self) -> object: ...
