"""Device/stream health for one TI radar, and its projection onto the canonical contract.

The adapter keeps a richer, vendor-neutral state machine than
:class:`~radiowave.contracts.observations.SensorHealth` exposes (which only knows
OK / DEGRADED / OFFLINE); :meth:`TiAdapterDiagnostics.to_sensor_health` maps onto it so
every consumer can use the existing contract, while the detailed counters stay
available for the probe command and the live API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from radiowave.contracts.observations import SensorHealth, SensorHealthStatus


class TiStreamState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    STREAMING = "STREAMING"
    STALE = "STALE"
    ERROR = "ERROR"


_CANONICAL_STATUS: dict[TiStreamState, SensorHealthStatus] = {
    TiStreamState.STREAMING: SensorHealthStatus.OK,
    TiStreamState.STALE: SensorHealthStatus.DEGRADED,
    TiStreamState.CONNECTING: SensorHealthStatus.OFFLINE,
    TiStreamState.DISCONNECTED: SensorHealthStatus.OFFLINE,
    TiStreamState.ERROR: SensorHealthStatus.OFFLINE,
}


@dataclass(slots=True)
class TiAdapterDiagnostics:
    """Point-in-time snapshot of one radar stream. Plain data, safe to serialize."""

    sensor_id: str
    state: TiStreamState = TiStreamState.DISCONNECTED
    message: str | None = None
    generation: int = 1
    bytes_received: int = 0
    frames_received: int = 0
    frames_parsed: int = 0
    frames_rejected: int = 0
    frames_duplicate: int = 0
    unknown_tlv_count: int = 0
    resyncs: int = 0
    targets_emitted: int = 0
    observations_emitted: int = 0
    observations_dropped_overflow: int = 0
    reconnect_count: int = 0
    last_frame_number: int | None = None
    last_frame_at: datetime | None = None
    last_frame_age_s: float | None = None
    observation_rate_hz: float | None = None
    frame_rate_hz: float | None = None
    reject_reasons: dict[str, int] = field(default_factory=dict)

    def to_sensor_health(self, now: datetime) -> SensorHealth:
        message = self.state.value
        if self.message is not None:
            message = f"{message}: {self.message}"
        return SensorHealth(
            sensor_id=self.sensor_id,
            timestamp=now,
            status=_CANONICAL_STATUS[self.state],
            last_observation_at=self.last_frame_at,
            message=message,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "sensor_id": self.sensor_id,
            "state": self.state.value,
            "message": self.message,
            "generation": self.generation,
            "bytes_received": self.bytes_received,
            "frames_received": self.frames_received,
            "frames_parsed": self.frames_parsed,
            "frames_rejected": self.frames_rejected,
            "frames_duplicate": self.frames_duplicate,
            "unknown_tlv_count": self.unknown_tlv_count,
            "resyncs": self.resyncs,
            "targets_emitted": self.targets_emitted,
            "observations_emitted": self.observations_emitted,
            "observations_dropped_overflow": self.observations_dropped_overflow,
            "reconnect_count": self.reconnect_count,
            "last_frame_number": self.last_frame_number,
            "last_frame_at": None if self.last_frame_at is None else self.last_frame_at.isoformat(),
            "last_frame_age_s": self.last_frame_age_s,
            "observation_rate_hz": self.observation_rate_hz,
            "frame_rate_hz": self.frame_rate_hz,
            "reject_reasons": dict(self.reject_reasons),
        }


class RateMeter:
    """Exponential moving rate estimate over event timestamps (monotonic seconds)."""

    def __init__(self, alpha: float = 0.2) -> None:
        self._alpha = alpha
        self._last: float | None = None
        self.rate_hz: float | None = None

    def tick(self, now_monotonic: float, count: int = 1) -> None:
        if count <= 0:
            return
        if self._last is not None:
            interval = now_monotonic - self._last
            if interval > 0.0:
                instantaneous = count / interval
                self.rate_hz = (
                    instantaneous
                    if self.rate_hz is None
                    else (1.0 - self._alpha) * self.rate_hz + self._alpha * instantaneous
                )
        self._last = now_monotonic

    def reset(self) -> None:
        self._last = None
        self.rate_hz = None
