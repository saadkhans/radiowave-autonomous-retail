"""TiLiveSession: reader thread, bounded queue, health, disconnect/reconnect, shutdown."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from radiowave.adapters.mmwave.ti.config import TiRawCaptureConfig, TiReconnectPolicy
from radiowave.adapters.mmwave.ti.health import TiStreamState
from radiowave.adapters.mmwave.ti.session import TiLiveSession
from radiowave.adapters.mmwave.ti.transport import (
    ByteStream,
    ByteStreamError,
    ChunkedByteStream,
    MemoryByteStream,
)
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline
from tests.fixtures.ti_mmwave.builder import build_target_frame
from tests.unit.mmwave_ti.support import FakeTiming, HoldOpenStream, live_config, wait_until


def frames(count: int, start: int = 1) -> list[bytes]:
    return [
        build_target_frame(number, [{"tid": 3, "x": 0.0, "y": 2.0 + 0.05 * number, "z": 1.0}])
        for number in range(start, start + count)
    ]


def session_for(
    streams: list[ByteStream],
    *,
    timing: FakeTiming | None = None,
    queue_capacity: int = 4096,
    reconnect: TiReconnectPolicy | None = None,
    **adapter_overrides: object,
) -> TiLiveSession:
    config = live_config(reconnect=reconnect, **adapter_overrides)
    pending = list(streams)

    def factory() -> ByteStream:
        if not pending:
            raise ByteStreamError("port unavailable")
        return pending.pop(0)

    return TiLiveSession(
        config,
        StoreRegistry(config.store),
        stream_factory=factory,
        timing=(timing or FakeTiming()).timing(),
        queue_capacity=queue_capacity,
    )


def test_frames_from_the_stream_become_queued_observations() -> None:
    stream = HoldOpenStream(frames(5))
    session = session_for([stream])
    session.start()
    try:
        assert wait_until(lambda: session.queued == 5)
        observations = session.drain()
        assert [o.metadata["native_frame_number"] for o in observations] == [1, 2, 3, 4, 5]
        assert session.queued == 0
        diagnostics = session.diagnostics()
        assert diagnostics.state == TiStreamState.STREAMING
        assert diagnostics.frames_parsed == 5
        assert diagnostics.observations_emitted == 5
        assert diagnostics.last_frame_number == 5
        assert diagnostics.generation == 1
    finally:
        assert session.stop()


def test_frames_split_across_reads_are_reassembled() -> None:
    data = b"".join(frames(4))
    stream = ChunkedByteStream(data, chunk_size=7)
    session = session_for([stream], reconnect=TiReconnectPolicy(enabled=False))
    session.start()
    try:
        assert wait_until(lambda: session.queued == 4)
    finally:
        session.stop()


def test_disconnect_marks_the_stream_and_reconnects_in_a_new_generation() -> None:
    first = MemoryByteStream(frames(2))  # raises ByteStreamClosed when exhausted
    second = HoldOpenStream(frames(2, start=1))  # frame numbers restart after reconnect
    session = session_for([first, second])
    session.start()
    try:
        assert wait_until(lambda: session.queued == 4)
        observations = session.drain()
        assert [o.metadata["native_stream_generation"] for o in observations] == [1, 1, 2, 2]
        diagnostics = session.diagnostics()
        assert diagnostics.reconnect_count == 1
        assert diagnostics.generation == 2
        assert diagnostics.frames_duplicate == 0
        assert diagnostics.state == TiStreamState.STREAMING
    finally:
        session.stop()


def test_reconnect_disabled_ends_in_error_without_crashing() -> None:
    session = session_for([MemoryByteStream(frames(1))], reconnect=TiReconnectPolicy(enabled=False))
    session.start()
    try:
        assert wait_until(lambda: session.diagnostics().state == TiStreamState.ERROR)
        assert wait_until(lambda: not session.running)
        assert session.queued == 1
        assert "reconnect disabled" in (session.diagnostics().message or "")
    finally:
        session.stop()


def test_bounded_retries_give_up_with_error_state() -> None:
    session = session_for(
        [], reconnect=TiReconnectPolicy(initial_delay_s=0.001, max_delay_s=0.001, max_attempts=3)
    )
    session.start()
    try:
        assert wait_until(lambda: session.diagnostics().state == TiStreamState.ERROR)
        assert "gave up after 3 attempts" in (session.diagnostics().message or "")
        assert session.queued == 0
    finally:
        session.stop()


def test_transport_error_mid_stream_does_not_lose_earlier_frames() -> None:
    boom = MemoryByteStream(frames(3), fail_after=ByteStreamError("USB gone"))
    session = session_for([boom], reconnect=TiReconnectPolicy(enabled=False))
    session.start()
    try:
        assert wait_until(lambda: session.diagnostics().state == TiStreamState.ERROR)
        assert session.queued == 3
        assert "USB gone" in (session.diagnostics().message or "")
    finally:
        session.stop()


def test_queue_is_bounded_and_overflow_is_counted() -> None:
    session = session_for([HoldOpenStream(frames(10))], queue_capacity=4)
    session.start()
    try:
        assert wait_until(lambda: session.diagnostics().observations_dropped_overflow == 6)
        assert session.queued == 4
        observations = session.drain()
        # Oldest dropped, newest kept.
        assert [o.metadata["native_frame_number"] for o in observations] == [7, 8, 9, 10]
    finally:
        session.stop()


def test_stop_joins_the_thread_and_closes_the_stream() -> None:
    stream = HoldOpenStream(frames(1))
    session = session_for([stream])
    session.start()
    assert wait_until(lambda: session.queued == 1)
    assert session.stop(timeout_s=2.0)
    assert not session.running
    assert session.diagnostics().state == TiStreamState.DISCONNECTED
    with pytest.raises(Exception):  # noqa: B017 - the port is closed for good
        stream.read(1)
    # No more observations after stop.
    session.drain()
    assert session.queued == 0


def test_stream_goes_stale_when_frames_stop() -> None:
    timing = FakeTiming(step_s=1.0)  # each clock read is a full second later
    session = session_for(
        [HoldOpenStream(frames(1))], timing=timing, observation={"stale_after_s": 2.0}
    )
    session.start()
    try:
        assert wait_until(lambda: session.queued == 1)
        assert wait_until(lambda: session.diagnostics().state == TiStreamState.STALE)
        assert "no frame" in (session.diagnostics().message or "")
    finally:
        session.stop()


def test_garbage_on_the_wire_is_counted_and_survived() -> None:
    noisy = [b"\xff" * 300, frames(1)[0], b"\x00\x01\x02" * 50, frames(1, start=2)[0]]
    session = session_for([HoldOpenStream(noisy)])
    session.start()
    try:
        assert wait_until(lambda: session.queued == 2)
        diagnostics = session.diagnostics()
        assert diagnostics.frames_parsed == 2
        assert diagnostics.resyncs >= 1
    finally:
        session.stop()


def test_request_reconnect_opens_a_new_generation_on_demand() -> None:
    first = HoldOpenStream(frames(1))
    second = HoldOpenStream(frames(1))
    session = session_for([first, second])
    session.start()
    try:
        assert wait_until(lambda: session.queued == 1)
        session.request_reconnect()
        assert wait_until(lambda: session.queued == 2)
        assert session.diagnostics().generation == 2
        assert session.diagnostics().reconnect_count == 1
    finally:
        session.stop()


def test_raw_capture_is_opt_in_and_size_capped(tmp_path: Path) -> None:
    path = tmp_path / "captures" / "raw.bin"
    data = frames(12)
    assert sum(len(chunk) for chunk in data) > 1024
    session = session_for(
        [HoldOpenStream(data)],
        raw_capture=TiRawCaptureConfig(path=str(path), max_bytes=1024),
    )
    session.start()
    try:
        assert wait_until(lambda: session.queued == 12)
    finally:
        session.stop()
    assert path.exists()
    assert path.stat().st_size == 1024  # capped, never grows past the configured limit


def test_canonical_health_projection() -> None:
    session = session_for([HoldOpenStream(frames(1))])
    session.start()
    try:
        assert wait_until(lambda: session.queued == 1)
        from radiowave.contracts.observations import SensorHealthStatus
        from tests.unit.mmwave_ti.support import T0

        health = session.diagnostics().to_sensor_health(T0)
        assert health.sensor_id == session.sensor_id
        assert health.status == SensorHealthStatus.OK
        assert health.last_observation_at is not None
    finally:
        session.stop()


def test_receive_time_is_stamped_under_the_queue_lock() -> None:
    """The canonical timestamp is taken inside the same critical section that enqueues
    the observation, so a consumer using ``drain_with_clock`` cannot observe a clock
    instant that an observation still on its way into the queue predates."""
    timing = FakeTiming()
    session = session_for([HoldOpenStream(frames(3))], timing=timing)
    locked_at_stamp: list[bool] = []
    original_utc_now = session._timing.utc_now

    def stamping_clock():  # type: ignore[no-untyped-def]
        locked_at_stamp.append(session._lock.locked())
        return original_utc_now()

    session._timing = replace(session._timing, utc_now=stamping_clock)
    session.start()
    try:
        assert wait_until(lambda: session.queued == 3)
    finally:
        assert session.stop()
    assert locked_at_stamp and all(locked_at_stamp)


def test_drain_with_clock_never_returns_an_instant_older_than_a_drained_observation() -> None:
    timing = FakeTiming()
    session = session_for([HoldOpenStream(frames(4))], timing=timing)
    session.start()
    try:
        assert wait_until(lambda: session.queued == 4)
        observations, now = session.drain_with_clock()
        assert len(observations) == 4
        assert all(observation.timestamp <= now for observation in observations)
        assert session.queued == 0
        # An empty drain still yields a usable instant for the consumer's clock advance.
        again, later = session.drain_with_clock()
        assert again == [] and later >= now
    finally:
        assert session.stop()


# --------------------------------------------------------------------------- invariant 8


def test_canonical_timestamps_never_go_backwards_when_host_wall_time_does() -> None:
    """Invariant 8: canonical live timestamps never go backwards because host wall time
    changes. ``clock_now()`` is anchored to UTC only once (lazily, at first use) and is
    then driven purely by the monotonic clock, so a host wall-clock regression seen
    after that anchor is never used."""
    wall = [
        datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 3, 1, 12, 0, 1, tzinfo=UTC),
        datetime(2026, 3, 1, 11, 59, 55, tzinfo=UTC),  # a backward NTP-style step
    ]
    timing = FakeTiming(wall=wall, monotonic=[100.0, 101.0, 102.0])
    session = session_for([HoldOpenStream(frames(3))], timing=timing)
    session.start()
    try:
        assert wait_until(lambda: session.queued == 3)
        observations = session.drain()
        stamps = [o.timestamp for o in observations]
        assert stamps == [
            datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
            datetime(2026, 3, 1, 12, 0, 1, tzinfo=UTC),
            datetime(2026, 3, 1, 12, 0, 2, tzinfo=UTC),
        ]
        assert stamps == sorted(stamps)  # strictly non-decreasing; no 11:59:55 anywhere
        now = session.clock_now()
        assert now >= stamps[-1]
    finally:
        assert session.stop()


# --------------------------------------------------------------------------- invariant 9


def test_observation_and_pipeline_clock_are_the_same_ordered_clock() -> None:
    """Invariant 9: sensor observations and the pipeline clock use the same ordered
    clock. A consumer that ingests a drained batch and then advances a
    ``FoundationPipeline`` to ``drain_with_clock``'s instant never has its own
    observations rejected as out of order, even across a host wall-clock regression."""
    wall = [
        datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
        datetime(2026, 3, 1, 12, 0, 1, tzinfo=UTC),
        datetime(2026, 3, 1, 11, 59, 50, tzinfo=UTC),  # a backward NTP-style step
    ]
    timing = FakeTiming(wall=wall, monotonic=[100.0, 101.0, 102.0])
    config = live_config()
    pending: list[ByteStream] = [HoldOpenStream(frames(3))]

    def factory() -> ByteStream:
        if not pending:
            raise ByteStreamError("no more streams")
        return pending.pop(0)

    session = TiLiveSession(
        config, StoreRegistry(config.store), stream_factory=factory, timing=timing.timing()
    )
    pipeline = FoundationPipeline(StoreRegistry(config.store))
    session.start()
    accepted = 0
    try:
        # The stream delivers all three frames almost immediately (no per-frame
        # pacing), so wait for all of them, then drain/ingest/advance in a loop: later
        # iterations simply see an empty batch and a clock that has not moved, which
        # must be just as harmless as draining mid-stream.
        assert wait_until(lambda: session.queued == 3)
        for _ in range(3):
            observations, now = session.drain_with_clock()
            for observation in observations:
                assert pipeline.ingest(observation)
                accepted += 1
            pipeline.advance_to(now)
    finally:
        assert session.stop()
    assert accepted == 3
    result = pipeline.result()
    assert result.observations_out_of_order == 0


# --------------------------------------------------------------------------- invariant 10 (C1)


def test_a_healthy_connection_resets_the_consecutive_failure_budget() -> None:
    """Invariant 10 / C1: a successful sensor connection resets consecutive retry
    failures. Two failed opens, then a connection that emits a valid frame before
    dropping, must report the *next* outage as retry 1 (not 3) and must not exhaust a
    ``max_attempts=2`` budget that the pre-health failures would otherwise have used up.
    """
    healthy_stream = MemoryByteStream(frames(1))  # emits one frame, then raises closed
    attempts = 0
    healthy_seen = threading.Event()

    def factory() -> ByteStream:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise ByteStreamError("port busy")
        healthy_seen.set()
        return healthy_stream

    def wait(event: threading.Event, _seconds: float) -> bool:
        # Before the healthy connection: never really sleep (matches FakeTiming).
        # After it drops: block on the real stop event so the test has a window to
        # observe the "retry 1" state before the reader tries to reconnect again.
        if healthy_seen.is_set():
            return event.wait(2.0)
        return event.is_set()

    fake = FakeTiming()
    timing = replace(fake.timing(), wait=wait)
    config = live_config(
        reconnect=TiReconnectPolicy(
            enabled=True, initial_delay_s=0.001, max_delay_s=0.001, max_attempts=2
        )
    )
    session = TiLiveSession(
        config, StoreRegistry(config.store), stream_factory=factory, timing=timing
    )
    session.start()
    try:
        assert wait_until(lambda: session.queued == 1)
        assert wait_until(lambda: "retry 1" in (session.diagnostics().message or ""), timeout_s=2.0)
        assert session.diagnostics().state != TiStreamState.ERROR
    finally:
        assert session.stop()


# --------------------------------------------------------------------------- invariant 11/12 (C2)


def test_reconnect_from_error_restarts_a_dead_reader() -> None:
    """Invariant 11: reconnect from ERROR actually restarts a dead reader."""
    pending: list[ByteStream] = []

    def factory() -> ByteStream:
        if not pending:
            raise ByteStreamError("port unavailable")
        return pending.pop(0)

    config = live_config(
        reconnect=TiReconnectPolicy(
            enabled=True, initial_delay_s=0.001, max_delay_s=0.001, max_attempts=1
        )
    )
    session = TiLiveSession(
        config, StoreRegistry(config.store), stream_factory=factory, timing=FakeTiming().timing()
    )
    session.start()
    try:
        assert wait_until(lambda: session.diagnostics().state == TiStreamState.ERROR)
        assert wait_until(lambda: not session.running)
        pending.append(HoldOpenStream(frames(1)))
        assert session.request_reconnect() is True
        assert wait_until(lambda: session.running)
        assert wait_until(lambda: session.queued == 1)
        assert session.diagnostics().state == TiStreamState.STREAMING
    finally:
        session.stop()


def test_reconnect_after_error_with_a_prior_connection_opens_a_new_generation() -> None:
    """C2: a restarted reader still opens a new stream generation via the existing
    ``_connections > 1`` rule, because ``_connections`` persists across restarts."""
    pending: list[ByteStream] = [MemoryByteStream(frames(1))]

    def factory() -> ByteStream:
        if not pending:
            raise ByteStreamError("port gone")
        return pending.pop(0)

    config = live_config(
        reconnect=TiReconnectPolicy(
            enabled=True, initial_delay_s=0.001, max_delay_s=0.001, max_attempts=1
        )
    )
    session = TiLiveSession(
        config, StoreRegistry(config.store), stream_factory=factory, timing=FakeTiming().timing()
    )
    session.start()
    try:
        assert wait_until(lambda: session.diagnostics().state == TiStreamState.ERROR)
        assert session.diagnostics().generation == 1
        pending.append(HoldOpenStream(frames(1, start=1)))
        assert session.request_reconnect() is True
        assert wait_until(lambda: session.queued >= 1)
        observations = session.drain()
        assert observations[-1].metadata["native_stream_generation"] == 2
        assert session.diagnostics().generation == 2
    finally:
        session.stop()


def test_reconnect_after_stop_is_rejected_and_never_resurrects() -> None:
    """Invariant 12: user STOP never resurrects automatically."""
    session = session_for([HoldOpenStream(frames(1))])
    session.start()
    try:
        assert wait_until(lambda: session.queued == 1)
    finally:
        assert session.stop()
    assert session.request_reconnect() is False
    assert not session.running
    assert not any(t.name == f"ti-mmwave-{session.sensor_id}" for t in threading.enumerate())


def test_concurrent_reconnect_requests_spawn_exactly_one_reader() -> None:
    """C2: a reconnect race (multiple concurrent callers) never spawns two threads."""
    available = False

    def factory() -> ByteStream:
        # A fresh, never-failing stream every time the port is "available": some of
        # the racing callers below force the eventual sole reader through more than
        # one reconnect cycle (closing the stream it just opened), so the factory must
        # never run out rather than accidentally driving the session into ERROR.
        if not available:
            raise ByteStreamError("port unavailable")
        return HoldOpenStream(frames(1))

    config = live_config(
        reconnect=TiReconnectPolicy(
            enabled=True, initial_delay_s=0.001, max_delay_s=0.001, max_attempts=1
        )
    )
    session = TiLiveSession(
        config, StoreRegistry(config.store), stream_factory=factory, timing=FakeTiming().timing()
    )
    session.start()
    try:
        assert wait_until(lambda: session.diagnostics().state == TiStreamState.ERROR)
        assert wait_until(lambda: not session.running)
        available = True
        barrier = threading.Barrier(4)
        results: list[bool] = []
        results_lock = threading.Lock()

        def call_reconnect() -> None:
            barrier.wait(timeout=2.0)
            outcome = session.request_reconnect()
            with results_lock:
                results.append(outcome)

        callers = [threading.Thread(target=call_reconnect) for _ in range(4)]
        for caller in callers:
            caller.start()
        for caller in callers:
            caller.join(timeout=2.0)
        assert len(results) == 4
        assert all(results)
        # At least one reconnect cycle got far enough to deliver a frame; how many of
        # the racing calls each forced yet another cycle first is not deterministic.
        assert wait_until(lambda: session.queued >= 1)
        ti_threads = [
            t for t in threading.enumerate() if t.name == f"ti-mmwave-{session.sensor_id}"
        ]
        assert len(ti_threads) == 1
    finally:
        session.stop()


def test_no_zombie_threads_after_stop() -> None:
    session = session_for([HoldOpenStream(frames(1))])
    session.start()
    try:
        assert wait_until(lambda: session.queued == 1)
    finally:
        assert session.stop(timeout_s=2.0)
    assert session.running is False
    assert not any(t.name == f"ti-mmwave-{session.sensor_id}" for t in threading.enumerate())
