"""Shared helpers for the TI adapter tests: stores with one radar, fake clocks, streams."""

from __future__ import annotations

import math
import threading
from datetime import UTC, datetime, timedelta

from radiowave.adapters.mmwave.ti.config import (
    TiAdapterConfig,
    TiLiveConfig,
    TiReconnectPolicy,
    TiSerialConfig,
)
from radiowave.adapters.mmwave.ti.session import SessionTiming
from radiowave.adapters.mmwave.ti.transport import ByteStreamClosed
from radiowave.contracts.geometry import WorldCoordinate
from radiowave.contracts.store import Box2D, Sensor, SensorPose, SourceType, Store

T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
RADAR = "radar-ti-01"


def store_with_radar(
    *,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 2.4,
    yaw: float = 0.0,
    sensor_id: str = RADAR,
    name: str | None = "TI IWR6843 lab radar",
) -> Store:
    return Store(
        store_id="lab-ti",
        name="TI lab",
        floor_bounds=Box2D(min_x=-10.0, min_y=-10.0, max_x=20.0, max_y=20.0),
        sensors=[
            Sensor(
                sensor_id=sensor_id,
                modality=SourceType.MMWAVE,
                pose=SensorPose(position=WorldCoordinate(x=x, y=y, z=z), yaw=yaw),
                name=name,
            )
        ],
    )


def adapter_config(sensor_id: str = RADAR, **overrides: object) -> TiAdapterConfig:
    return TiAdapterConfig(sensor_id=sensor_id, **overrides)  # type: ignore[arg-type]


def live_config(
    store: Store | None = None,
    *,
    sensor_id: str = RADAR,
    reconnect: TiReconnectPolicy | None = None,
    **adapter_overrides: object,
) -> TiLiveConfig:
    policy = reconnect or TiReconnectPolicy(
        enabled=True, initial_delay_s=0.001, max_delay_s=0.002, max_attempts=2
    )
    return TiLiveConfig(
        store=store or store_with_radar(x=6.0, y=0.0, z=2.4, yaw=math.pi / 2),
        serial=TiSerialConfig(data_port="TEST-PORT", read_timeout_s=0.05, read_chunk_bytes=256),
        adapter=adapter_config(sensor_id, reconnect=policy, **adapter_overrides),
    )


class FakeTiming:
    """Deterministic clocks: every ``utc_now``/``monotonic`` call advances by ``step_s``;
    ``wait`` never sleeps (it just reports whether the event is set)."""

    def __init__(self, start: datetime = T0, step_s: float = 0.1) -> None:
        self._start = start
        self._step = step_s
        self._utc_calls = 0
        self._monotonic_calls = 0
        self._lock = threading.Lock()

    def utc_now(self) -> datetime:
        # Its own counter: the reader thread's stale checks (monotonic only) must not
        # perturb the UTC stamps, or captures would not be reproducible.
        with self._lock:
            value = self._start + timedelta(seconds=self._utc_calls * self._step)
            self._utc_calls += 1
            return value

    def monotonic(self) -> float:
        with self._lock:
            value = self._monotonic_calls * self._step
            self._monotonic_calls += 1
            return value

    def wait(self, event: threading.Event, _seconds: float) -> bool:
        return event.is_set()

    def timing(self) -> SessionTiming:
        return SessionTiming(utc_now=self.utc_now, monotonic=self.monotonic, wait=self.wait)


class HoldOpenStream:
    """Serves chunks, then reports timeouts (``b""``) until closed, like an idle serial port."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self._closed = threading.Event()
        self.reads = 0

    def read(self, max_bytes: int) -> bytes:
        self.reads += 1
        if self._closed.is_set():
            raise ByteStreamClosed("closed")
        if self._chunks:
            chunk = self._chunks.pop(0)
            if len(chunk) > max_bytes:
                self._chunks.insert(0, chunk[max_bytes:])
                return chunk[:max_bytes]
            return chunk
        self._closed.wait(0.005)
        return b""

    def close(self) -> None:
        self._closed.set()


def wait_until(predicate: object, timeout_s: float = 5.0) -> bool:
    """Poll a zero-arg callable with real time; True once it returns truthy."""
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.005)
    return bool(predicate())  # type: ignore[operator]
