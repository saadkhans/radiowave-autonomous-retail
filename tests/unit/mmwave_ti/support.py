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
from radiowave.adapters.mmwave.ti.transport import ByteStreamClosed, ByteStreamError
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
    ``wait`` never sleeps (it just reports whether the event is set).

    Tests that need to inject an exact wall/monotonic sequence (e.g. a host wall-clock
    regression) pass ``wall=[...]``/``monotonic=[...]``: each queued value is consumed,
    in order, before falling back to the default per-call stepping above. The same
    queues can be fed incrementally with ``set_wall``/``advance_monotonic``.
    """

    def __init__(
        self,
        start: datetime = T0,
        step_s: float = 0.1,
        *,
        wall: list[datetime] | None = None,
        monotonic: list[float] | None = None,
    ) -> None:
        self._start = start
        self._step = step_s
        self._utc_calls = 0
        self._monotonic_calls = 0
        self._lock = threading.Lock()
        self._wall_queue: list[datetime] = list(wall) if wall else []
        self._monotonic_queue: list[float] = list(monotonic) if monotonic else []

    def utc_now(self) -> datetime:
        # Its own counter: the reader thread's stale checks (monotonic only) must not
        # perturb the UTC stamps, or captures would not be reproducible.
        with self._lock:
            if self._wall_queue:
                return self._wall_queue.pop(0)
            value = self._start + timedelta(seconds=self._utc_calls * self._step)
            self._utc_calls += 1
            return value

    def monotonic(self) -> float:
        with self._lock:
            if self._monotonic_queue:
                return self._monotonic_queue.pop(0)
            value = self._monotonic_calls * self._step
            self._monotonic_calls += 1
            return value

    def wait(self, event: threading.Event, _seconds: float) -> bool:
        return event.is_set()

    def set_wall(self, dt: datetime) -> None:
        """Queue an explicit wall-clock value to be returned by the next ``utc_now``."""
        with self._lock:
            self._wall_queue.append(dt)

    def advance_monotonic(self, seconds: float) -> None:
        """Queue an explicit monotonic value to be returned by the next ``monotonic``."""
        with self._lock:
            self._monotonic_queue.append(seconds)

    def timing(self) -> SessionTiming:
        return SessionTiming(utc_now=self.utc_now, monotonic=self.monotonic, wait=self.wait)


class HoldOpenStream:
    """Serves chunks, then reports timeouts (``b""``) until closed, like an idle serial port.

    With ``block_when_idle=True`` an idle read blocks until ``close()`` instead of timing
    out. Each timeout triggers a stale check that reads the (auto-stepping) fake monotonic
    clock, so a test comparing two runs stamp for stamp uses the blocking form: the number
    of idle timeouts before the consumer ticks depends on thread timing, not on the input.

    With ``error_on_close=True`` a read after ``close()`` raises ``ByteStreamError``
    instead of ``ByteStreamClosed``. That models a real transport (``SerialByteStream``
    whose ``is_open`` flag has not flipped yet at the moment of the exception) surfacing
    an operator-requested close as a plain transport error.
    """

    def __init__(
        self,
        chunks: list[bytes],
        *,
        block_when_idle: bool = False,
        error_on_close: bool = False,
    ) -> None:
        self._chunks = list(chunks)
        self._closed = threading.Event()
        self._block_when_idle = block_when_idle
        self._error_on_close = error_on_close
        self.reads = 0

    def _closed_error(self) -> Exception:
        return ByteStreamError("closed") if self._error_on_close else ByteStreamClosed("closed")

    def read(self, max_bytes: int) -> bytes:
        self.reads += 1
        if self._closed.is_set():
            raise self._closed_error()
        if self._chunks:
            chunk = self._chunks.pop(0)
            if len(chunk) > max_bytes:
                self._chunks.insert(0, chunk[max_bytes:])
                return chunk[:max_bytes]
            return chunk
        if self._block_when_idle:
            self._closed.wait()
            raise self._closed_error()
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
