"""Live radar session: one reader thread, one bounded queue, no broker.

``TiLiveSession`` owns the transport, the parser and the normalizer for ONE radar.
A daemon thread reads bytes, parses frames, normalizes them into store-frame
:class:`PersonObservation` values and places them on a bounded queue. Whoever owns
the pipeline (the live Observatory run, the probe command, a capture script) drains
that queue from its own thread, so ``FoundationPipeline`` is only ever mutated by one
thread. The queue drops the OLDEST observations when the consumer falls behind and
counts every drop; the reader never blocks on the consumer and never grows memory.

Failure policy: a transport error marks the stream DISCONNECTED (or ERROR when
reconnection is disabled/exhausted), never raises out of the thread, never emits a
fabricated observation, and reconnects with bounded exponential backoff. Every
(re)connection opens a new stream generation so buffered or re-read frames from the
previous connection can never collide with new ones.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

from radiowave.adapters.mmwave.ti.adapter import TiTargetNormalizer
from radiowave.adapters.mmwave.ti.config import TiAdapterConfig, TiLiveConfig, TiSerialConfig
from radiowave.adapters.mmwave.ti.health import RateMeter, TiAdapterDiagnostics, TiStreamState
from radiowave.adapters.mmwave.ti.parser import TiFrameParser, TiParserLimits
from radiowave.adapters.mmwave.ti.protocol import (
    TI_3D_PEOPLE_COUNTING,
    TI_OOB_SDK3,
    TiFirmwareProfile,
)
from radiowave.adapters.mmwave.ti.transport import ByteStream, ByteStreamClosed, ByteStreamError
from radiowave.contracts.observations import PersonObservation
from radiowave.digital_twin.registry import StoreRegistry

log = logging.getLogger(__name__)

StreamFactory = Callable[[], ByteStream]


def firmware_profile_for(config: TiAdapterConfig) -> TiFirmwareProfile:
    base = TI_OOB_SDK3 if config.firmware_profile == "ti-oob-sdk3" else TI_3D_PEOPLE_COUNTING
    if config.target_record_layout is None:
        return base
    return TiFirmwareProfile(
        name=base.name,
        target_record_layout=config.target_record_layout,
        tlv_length_includes_header=base.tlv_length_includes_header,
        packet_alignment=base.packet_alignment,
    )


def parser_limits_for(config: TiAdapterConfig) -> TiParserLimits:
    limits = config.parser_limits
    return TiParserLimits(
        max_packet_bytes=limits.max_packet_bytes,
        max_tlvs=limits.max_tlvs,
        max_targets=limits.max_targets,
        max_points=limits.max_points,
        max_buffer_bytes=limits.max_buffer_bytes,
    )


def serial_stream_factory(serial: TiSerialConfig) -> StreamFactory:
    """Factory opening the configured data UART; import of pyserial is deferred to the call."""

    def open_stream() -> ByteStream:
        from radiowave.adapters.mmwave.ti.serial_transport import SerialByteStream

        return SerialByteStream(
            serial.data_port, serial.data_baud_rate, read_timeout_s=serial.read_timeout_s
        )

    return open_stream


class RawByteCapture:
    """Opt-in raw UART capture with a hard size cap (parser debugging only)."""

    def __init__(self, path: Path, max_bytes: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("wb")
        self._remaining = max_bytes
        self._lock = threading.Lock()
        self.path = path
        self.truncated = False

    def write(self, data: bytes) -> None:
        # The reader thread writes while stop() may close from another thread: the
        # lock makes a late write a no-op instead of an exception out of the reader.
        with self._lock:
            if self._handle.closed or self._remaining <= 0:
                self.truncated = self.truncated or self._remaining <= 0
                return
            chunk = data[: self._remaining]
            self._handle.write(chunk)
            self._remaining -= len(chunk)
            if len(chunk) < len(data):
                self.truncated = True

    def close(self) -> None:
        with self._lock:
            self._handle.close()


@dataclass(frozen=True, slots=True)
class SessionTiming:
    """Injectable clocks so tests run without wall time."""

    utc_now: Callable[[], datetime]
    monotonic: Callable[[], float]
    wait: Callable[[threading.Event, float], bool]


def _default_timing() -> SessionTiming:
    return SessionTiming(
        utc_now=lambda: datetime.now(UTC),
        monotonic=time.monotonic,
        wait=lambda event, seconds: event.wait(seconds),
    )


class TiLiveSession:
    """Reader thread + bounded observation queue for one TI radar."""

    def __init__(
        self,
        config: TiLiveConfig,
        registry: StoreRegistry,
        *,
        stream_factory: StreamFactory | None = None,
        timing: SessionTiming | None = None,
        queue_capacity: int = 4096,
        scenario_id: str | None = None,
    ) -> None:
        self._config = config
        self._adapter_config = config.adapter
        self._registry = registry
        self._stream_factory = stream_factory or serial_stream_factory(config.serial)
        self._timing = timing or _default_timing()
        self._normalizer = TiTargetNormalizer(
            config.adapter,
            registry,
            clock=self._timing.utc_now,
            scenario_id=scenario_id,
        )
        self._parser = TiFrameParser(
            firmware_profile_for(config.adapter), parser_limits_for(config.adapter)
        )
        self._queue: deque[PersonObservation] = deque(maxlen=queue_capacity)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reconnect_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream: ByteStream | None = None
        self._diagnostics = TiAdapterDiagnostics(sensor_id=config.adapter.sensor_id)
        self._last_frame_monotonic: float | None = None
        self._frame_rate = RateMeter()
        self._observation_rate = RateMeter()
        self._capture: RawByteCapture | None = None
        self._connections = 0

    # --------------------------------------------------------------- lifecycle

    @property
    def sensor_id(self) -> str:
        return self._adapter_config.sensor_id

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        raw = self._adapter_config.raw_capture
        if raw.path is not None and self._capture is None:
            self._capture = RawByteCapture(Path(raw.path), raw.max_bytes)
        self._thread = threading.Thread(
            target=self._run, name=f"ti-mmwave-{self.sensor_id}", daemon=True
        )
        self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Stop reading, close the transport and join the thread. True if it exited."""
        self._stop.set()
        self._close_stream()
        thread = self._thread
        if thread is not None:
            thread.join(timeout_s)
            exited = not thread.is_alive()
        else:
            exited = True
        if self._capture is not None:
            self._capture.close()
            self._capture = None
        with self._lock:
            if self._diagnostics.state not in (TiStreamState.ERROR,):
                self._diagnostics.state = TiStreamState.DISCONNECTED
                self._diagnostics.message = "stopped"
        return exited

    def request_reconnect(self) -> None:
        """Ask the reader to drop the current connection and open a new generation."""
        self._reconnect_requested.set()
        self._close_stream()

    def drain(self, max_items: int | None = None) -> list[PersonObservation]:
        """Observations in arrival order; the queue is emptied (or reduced by max_items)."""
        with self._lock:
            if max_items is None or max_items >= len(self._queue):
                items = list(self._queue)
                self._queue.clear()
            else:
                items = [self._queue.popleft() for _ in range(max_items)]
        return items

    @property
    def queued(self) -> int:
        with self._lock:
            return len(self._queue)

    def diagnostics(self) -> TiAdapterDiagnostics:
        """Consistent snapshot; safe to call from any thread."""
        now = self._timing.monotonic()
        with self._lock:
            self._refresh_stale(now)
            stats = self._parser.stats
            counters = self._normalizer.counters
            diagnostics = self._diagnostics
            diagnostics.bytes_received = stats.bytes_received
            diagnostics.frames_parsed = stats.frames_parsed
            diagnostics.frames_rejected = stats.frames_rejected
            diagnostics.frames_received = stats.frames_parsed + stats.frames_rejected
            diagnostics.unknown_tlv_count = stats.unknown_tlv_count
            diagnostics.resyncs = stats.resyncs
            diagnostics.reject_reasons = dict(stats.reject_reasons)
            diagnostics.frames_duplicate = counters.frames_duplicate
            diagnostics.generation = counters.generation
            diagnostics.targets_emitted = (
                counters.targets_seen - counters.targets_dropped_implausible
            )
            diagnostics.observations_emitted = counters.observations_emitted
            diagnostics.last_frame_number = counters.last_frame_number
            diagnostics.last_frame_at = counters.last_frame_at
            diagnostics.last_frame_age_s = (
                None
                if self._last_frame_monotonic is None
                else max(0.0, now - self._last_frame_monotonic)
            )
            diagnostics.frame_rate_hz = self._frame_rate.rate_hz
            diagnostics.observation_rate_hz = self._observation_rate.rate_hz
            return replace(diagnostics, reject_reasons=dict(diagnostics.reject_reasons))

    # ---------------------------------------------------------------- internals

    def _set_state(self, state: TiStreamState, message: str | None = None) -> None:
        with self._lock:
            self._diagnostics.state = state
            self._diagnostics.message = message

    def _refresh_stale(self, now: float) -> None:
        # Caller holds the lock.
        if self._diagnostics.state not in (TiStreamState.STREAMING, TiStreamState.STALE):
            return
        stale_after = self._adapter_config.observation.stale_after_s
        if self._last_frame_monotonic is None or now - self._last_frame_monotonic > stale_after:
            self._diagnostics.state = TiStreamState.STALE
            self._diagnostics.message = f"no frame for more than {stale_after:g} s"
        else:
            self._diagnostics.state = TiStreamState.STREAMING
            self._diagnostics.message = None

    def _close_stream(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.close()
            except Exception:  # closing a dead port must never propagate
                log.debug("ignoring error while closing %s stream", self.sensor_id, exc_info=True)

    def _backoff(self, attempt: int) -> float:
        policy = self._adapter_config.reconnect
        return float(min(policy.initial_delay_s * (2.0 ** max(0, attempt - 1)), policy.max_delay_s))

    def _run(self) -> None:
        failures = 0
        while not self._stop.is_set():
            self._set_state(TiStreamState.CONNECTING, self._config.serial.data_port)
            try:
                stream = self._stream_factory()
            except ByteStreamError as exc:
                failures += 1
                if not self._should_retry(failures, f"open failed: {exc}"):
                    return
                if self._timing.wait(self._stop, self._backoff(failures)):
                    return
                continue
            self._stream = stream
            self._connections += 1
            if self._connections > 1:
                self._normalizer.new_generation("reconnect")
                with self._lock:
                    self._diagnostics.reconnect_count += 1
            self._parser.reset()
            self._frame_rate.reset()
            self._observation_rate.reset()
            self._reconnect_requested.clear()
            self._set_state(TiStreamState.CONNECTING, "waiting for the first frame")
            outcome = self._read_until_failure(stream)
            self._close_stream()
            if self._stop.is_set():
                return
            if outcome is None:
                failures = 0  # an explicit reconnect request is not a failure
                continue
            failures += 1
            if not self._should_retry(failures, outcome):
                return
            if self._timing.wait(self._stop, self._backoff(failures)):
                return
        self._set_state(TiStreamState.DISCONNECTED, "stopped")

    def _should_retry(self, failures: int, reason: str) -> bool:
        policy = self._adapter_config.reconnect
        if not policy.enabled:
            self._set_state(TiStreamState.ERROR, f"{reason} (reconnect disabled)")
            return False
        if failures > policy.max_attempts:
            self._set_state(
                TiStreamState.ERROR, f"{reason} (gave up after {policy.max_attempts} attempts)"
            )
            return False
        self._set_state(TiStreamState.DISCONNECTED, f"{reason}; retry {failures}")
        return True

    def _read_until_failure(self, stream: ByteStream) -> str | None:
        """Read loop for one connection. Returns a failure reason, or None for a requested
        reconnect / stop."""
        chunk = self._config.serial.read_chunk_bytes
        while not self._stop.is_set() and not self._reconnect_requested.is_set():
            try:
                data = stream.read(chunk)
            except ByteStreamClosed as exc:
                return f"stream closed: {exc}" if str(exc) else "stream closed"
            except ByteStreamError as exc:
                return f"transport error: {exc}"
            except Exception as exc:  # a driver bug must not kill the API
                log.exception("unexpected error reading %s", self.sensor_id)
                return f"unexpected error: {type(exc).__name__}"
            if not data:
                with self._lock:
                    self._refresh_stale(self._timing.monotonic())
                continue
            self._handle_bytes(data)
        return None

    def _handle_bytes(self, data: bytes) -> None:
        try:
            if self._capture is not None:
                self._capture.write(data)
            frames = self._parser.feed(data)
        except Exception:  # parser bugs are counted, never fatal
            log.exception("parser failure on %s; resetting parser", self.sensor_id)
            self._parser.reset()
            return
        if not frames:
            return
        received_at = self._timing.utc_now()
        now = self._timing.monotonic()
        emitted: list[PersonObservation] = []
        for frame in frames:
            try:
                emitted.extend(self._normalizer.frame_to_observations(frame, received_at))
            except Exception:  # one bad frame must not stop the stream
                log.exception("normalization failure on %s frame %s", self.sensor_id, frame)
        with self._lock:
            self._last_frame_monotonic = now
            self._frame_rate.tick(now, len(frames))
            self._observation_rate.tick(now, len(emitted))
            for observation in emitted:
                if len(self._queue) == self._queue.maxlen:
                    self._diagnostics.observations_dropped_overflow += 1
                self._queue.append(observation)
            self._diagnostics.state = TiStreamState.STREAMING
            self._diagnostics.message = None
