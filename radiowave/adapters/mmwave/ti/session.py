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
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
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
from radiowave.contracts._base import ensure_utc
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
            clock=self.clock_now,
            scenario_id=scenario_id,
        )
        self._parser = TiFrameParser(
            firmware_profile_for(config.adapter), parser_limits_for(config.adapter)
        )
        self._queue: deque[PersonObservation] = deque(maxlen=queue_capacity)
        self._lock = threading.Lock()
        self._lifecycle = threading.Lock()
        self._stop = threading.Event()
        self._stopped = False
        self._reconnect_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._stream: ByteStream | None = None
        self._diagnostics = TiAdapterDiagnostics(sensor_id=config.adapter.sensor_id)
        self._last_frame_monotonic: float | None = None
        self._frame_rate = RateMeter()
        self._observation_rate = RateMeter()
        self._capture: RawByteCapture | None = None
        self._connections = 0
        self._connection_had_frame = False
        # One ordered pipeline clock per session; see ``clock_now``. Anchored lazily
        # (before the reader ever stamps an observation) rather than here, so tests can
        # inject the exact wall/monotonic reading the anchor is taken from.
        self._clock_lock = threading.Lock()
        self._clock_anchor_utc: datetime | None = None
        self._clock_anchor_monotonic: float | None = None
        self._clock_last_elapsed: float = 0.0

    # --------------------------------------------------------------- lifecycle

    @property
    def sensor_id(self) -> str:
        return self._adapter_config.sensor_id

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        with self._lifecycle:
            if self.running:
                return
            self._stopped = False
            self._stop.clear()
            raw = self._adapter_config.raw_capture
            opened_capture = False
            if raw.path is not None and self._capture is None:
                self._capture = RawByteCapture(Path(raw.path), raw.max_bytes)
                opened_capture = True
            try:
                self._thread = threading.Thread(
                    target=self._run, name=f"ti-mmwave-{self.sensor_id}", daemon=True
                )
                self._thread.start()
            except BaseException:
                # Thread creation/start failed (thread exhaustion, OS limits): no
                # reader exists to ever close the capture handle just opened above, so
                # close it here rather than leaking an open file. Never close a capture
                # a previous, already-succeeded start() owns.
                if opened_capture and self._capture is not None:
                    try:
                        self._capture.close()
                    except Exception:
                        # A cleanup failure (e.g. OSError flushing a full disk) must
                        # never replace the original thread-start error being
                        # propagated below; only log it. Catching Exception, not
                        # BaseException, so a KeyboardInterrupt during cleanup is
                        # still delivered rather than swallowed.
                        log.exception(
                            "failed to close raw capture for %s during start() cleanup",
                            self.sensor_id,
                        )
                    self._capture = None
                self._thread = None
                # Back to the stopped state this start() was entered from: the flag
                # was cleared above on the assumption a reader would exist, and none
                # does. Leaving it false would let a caller that swallows this error
                # go on to request_reconnect() and run a reader with raw capture
                # silently disabled (the handle it would write to was just closed).
                self._stopped = True
                raise

    def stop(self, timeout_s: float = 5.0) -> bool:
        """Stop reading, close the transport and join the thread. True if it exited.

        A stopped session never resurrects itself: :meth:`request_reconnect` is a no-op
        until an explicit :meth:`start` call clears the stopped flag again.
        """
        with self._lifecycle:
            self._stopped = True
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

    def request_reconnect(self) -> bool:
        """Ask the session to drop its current connection and open a new generation.

        If the reader thread is alive, this just signals it (existing behaviour): the
        thread notices, closes the stream and reconnects on its own. If the thread has
        already exited (ERROR after exhausted retries, or reconnect disabled) nothing
        would otherwise consume the request, so a fresh reader thread is started with a
        clean failure budget instead. A user ``stop()`` is never overridden: a stopped
        session returns False and starts nothing until an explicit ``start()``. The
        lifecycle lock makes concurrent callers agree on exactly one outcome: never two
        reader threads for one session.
        """
        with self._lifecycle:
            if self._stopped:
                return False
            thread = self._thread
            if thread is not None and thread.is_alive():
                self._reconnect_requested.set()
                self._close_stream()
                return True
            # The reader already exited (ERROR, or reconnect disabled) or the session
            # was never started: bring up a fresh reader rather than leaving the
            # session stuck until an unrelated stop()/start() cycle.
            self._reconnect_requested.clear()
            self._stop.clear()
            with self._lock:
                self._diagnostics.state = TiStreamState.CONNECTING
                self._diagnostics.message = None
            self._thread = threading.Thread(
                target=self._run, name=f"ti-mmwave-{self.sensor_id}", daemon=True
            )
            self._thread.start()
            return True

    def drain(self, max_items: int | None = None) -> list[PersonObservation]:
        """Observations in arrival order; the queue is emptied (or reduced by max_items).

        Diagnostics/tests only: unlike :meth:`drain_with_clock` this does not read the
        session clock atomically with the drain, so a consumer that advances a
        downstream pipeline's clock from a separately-read clock value can be overtaken
        by an observation still on its way into the queue.
        """
        with self._lock:
            return self._drain_locked(max_items)

    def drain_with_clock(self) -> tuple[list[PersonObservation], datetime]:
        """Drain the queue and read :meth:`clock_now` in one critical section.

        This is the canonical consumer primitive: the reader stamps, normalizes and
        enqueues each observation under the same queue lock, so every observation
        stamped before the returned instant is in the returned batch. A consumer that
        advances its pipeline clock to that instant can therefore never be overtaken by
        an older observation still on its way into the queue (which the pipeline would
        otherwise have to drop as out-of-order).
        """
        with self._lock:
            items = self._drain_locked(None)
            now = self.clock_now()
        return items, now

    def observations(self) -> Iterator[PersonObservation]:
        """Drain the queue and yield observations in arrival order.

        This exists so :class:`TiLiveSession` structurally satisfies
        :class:`radiowave.fusion.interfaces.PeopleTracker` (a vendor-neutral,
        substitutable boundary; see architecture invariant 1). It is built on
        :meth:`drain`, so — like ``drain`` and unlike :meth:`drain_with_clock` — it
        does NOT read :meth:`clock_now` atomically with the drain. A consumer that
        also advances a downstream pipeline's clock (the live Observatory driver, or
        anything else stepping fusion forward) MUST use :meth:`drain_with_clock`
        instead, or it can advance its clock past an observation still on its way
        into the queue.
        """
        yield from self.drain()

    def clock_now(self) -> datetime:
        """The session's one ordered live clock, shared by every stamped observation and
        by :meth:`drain_with_clock`.

        Anchored lazily, at the first call (before the reader stamps its first
        observation), to a real UTC instant; every later value is that anchor plus
        elapsed monotonic time, so host wall-clock changes (NTP steps, DST, an operator
        adjusting the system clock) can never move it backwards within the session. A
        reconnect does not re-anchor: the clock keeps advancing across generations.
        Device frame numbers and TI CPU cycles stay metadata only; they are never a
        source of UTC time.
        """
        with self._clock_lock:
            return self._clock_at_locked(self._timing.monotonic())

    def _clock_at_locked(self, mono: float) -> datetime:
        """Advance the clock from an already-sampled monotonic reading.

        Caller holds ``self._clock_lock``. Split out from :meth:`clock_now` so
        ``_handle_bytes`` can reuse the single monotonic reading it already takes for
        staleness/rate tracking as the same instant used for the receive stamp, instead
        of sampling the monotonic clock twice for what must be one ordered instant.
        """
        if self._clock_anchor_monotonic is None:
            self._clock_anchor_utc = ensure_utc(self._timing.utc_now())
            self._clock_anchor_monotonic = mono
        elapsed = mono - self._clock_anchor_monotonic
        if elapsed < self._clock_last_elapsed:
            # Monotonic must never regress within a process, but clamp defensively
            # rather than ever let the pipeline clock move backwards.
            elapsed = self._clock_last_elapsed
        self._clock_last_elapsed = elapsed
        anchor = self._clock_anchor_utc
        assert anchor is not None  # set on the branch above if it wasn't already
        return anchor + timedelta(seconds=elapsed)

    def _drain_locked(self, max_items: int | None) -> list[PersonObservation]:
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
            self._connection_had_frame = False
            self._set_state(TiStreamState.CONNECTING, "waiting for the first frame")
            reason, had_healthy_frame = self._read_until_failure(stream)
            self._close_stream()
            if self._stop.is_set():
                return
            if had_healthy_frame:
                # ``failures`` counts CONSECUTIVE failures: a connection that produced
                # at least one valid parsed frame was healthy, so the next outage starts
                # a fresh retry budget instead of compounding onto failures from a
                # connection that actually worked.
                failures = 0
            if reason is None:
                failures = 0  # an explicit reconnect request is not a failure
                continue
            failures += 1
            if not self._should_retry(failures, reason):
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

    def _read_until_failure(self, stream: ByteStream) -> tuple[str | None, bool]:
        """Read loop for one connection.

        Returns ``(reason, had_healthy_frame)``: ``reason`` is a failure reason, or
        ``None`` for a requested reconnect / stop; ``had_healthy_frame`` is True once
        this connection parsed at least one valid frame (see C1 in the session's
        consecutive-failure budget in :meth:`_run`).
        """
        chunk = self._config.serial.read_chunk_bytes
        while not self._stop.is_set() and not self._reconnect_requested.is_set():
            try:
                data = stream.read(chunk)
            except ByteStreamClosed as exc:
                if self._stop.is_set() or self._reconnect_requested.is_set():
                    # request_reconnect()/stop() closed the stream to wake a reader
                    # blocked inside stream.read(); that close is the operator's own
                    # pending request, not a transport failure (see module docstring
                    # and request_reconnect's docstring for the wake-up contract). A
                    # spontaneous close with no pending request still falls through
                    # below and counts against the failure budget as before.
                    return None, self._connection_had_frame
                reason = f"stream closed: {exc}" if str(exc) else "stream closed"
                return reason, self._connection_had_frame
            except ByteStreamError as exc:
                if self._stop.is_set() or self._reconnect_requested.is_set():
                    # Some transports (e.g. SerialByteStream, when the port's
                    # ``is_open`` flag has not yet flipped at the moment of the
                    # exception) can surface a request_reconnect()/stop()-triggered
                    # close as a plain ByteStreamError rather than ByteStreamClosed.
                    # Same benign-close reasoning as above.
                    return None, self._connection_had_frame
                return f"transport error: {exc}", self._connection_had_frame
            except Exception as exc:  # a driver bug must not kill the API
                log.exception("unexpected error reading %s", self.sensor_id)
                return f"unexpected error: {type(exc).__name__}", self._connection_had_frame
            if not data:
                with self._lock:
                    self._refresh_stale(self._timing.monotonic())
                continue
            self._handle_bytes(data)
        return None, self._connection_had_frame

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
        self._connection_had_frame = True
        # Stamp, normalize and enqueue under the queue lock: the receive time is the
        # canonical observation timestamp, and a consumer draining the queue reads its
        # own clock under this same lock (``drain_with_clock``), so an observation can
        # never carry a stamp older than a clock position the consumer already applied.
        with self._lock:
            now = self._timing.monotonic()
            # One shared monotonic reading feeds both the pipeline clock (so observation
            # and consumer clock are the same ordered clock) and staleness/rate
            # tracking below, instead of sampling the monotonic clock twice for what
            # must be one instant.
            with self._clock_lock:
                received_at = self._clock_at_locked(now)
            emitted: list[PersonObservation] = []
            for frame in frames:
                try:
                    emitted.extend(self._normalizer.frame_to_observations(frame, received_at))
                except Exception:  # one bad frame must not stop the stream
                    log.exception("normalization failure on %s frame %s", self.sensor_id, frame)
            self._last_frame_monotonic = now
            self._frame_rate.tick(now, len(frames))
            self._observation_rate.tick(now, len(emitted))
            for observation in emitted:
                if len(self._queue) == self._queue.maxlen:
                    self._diagnostics.observations_dropped_overflow += 1
                self._queue.append(observation)
            self._diagnostics.state = TiStreamState.STREAMING
            self._diagnostics.message = None
