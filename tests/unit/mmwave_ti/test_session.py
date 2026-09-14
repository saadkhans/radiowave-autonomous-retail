"""TiLiveSession: reader thread, bounded queue, health, disconnect/reconnect, shutdown."""

from __future__ import annotations

from dataclasses import replace
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
        observations, now = session.drain_with_clock(timing.utc_now)
        assert len(observations) == 4
        assert all(observation.timestamp <= now for observation in observations)
        assert session.queued == 0
        # An empty drain still yields a usable instant for the consumer's clock advance.
        again, later = session.drain_with_clock(timing.utc_now)
        assert again == [] and later >= now
    finally:
        assert session.stop()
