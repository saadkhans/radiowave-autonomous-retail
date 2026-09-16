"""Live run API: availability, creation, LIVE snapshot shape, refused replay controls,
stop/reconnect, capture path, and one-live-run-at-a-time."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from radiowave.adapters.mmwave.ti.session import TiLiveSession
from radiowave.api.app import create_app
from radiowave.api.live import LiveObservatoryRun, LiveRuntime
from radiowave.api.runs import LiveRunBusyError
from radiowave.replay.recorder import JsonlRecorder
from tests.fixtures.ti_mmwave.builder import build_target_frame
from tests.unit.mmwave_ti.support import (
    RADAR,
    FakeTiming,
    HoldOpenStream,
    live_config,
    wait_until,
)

FRAMES = 12


def frames() -> list[bytes]:
    return [
        build_target_frame(n, [{"tid": 5, "x": 0.0, "y": 3.0 - 0.1 * n, "z": 1.0}])
        for n in range(1, FRAMES + 1)
    ]


@pytest.fixture
def live_client(tmp_path: Path) -> Iterator[TestClient]:
    runtime = LiveRuntime(
        config=live_config(),
        config_path="configs/examples/ti-iwr6843-lab.json",
        stream_factory=lambda: HoldOpenStream(frames()),
        timing=FakeTiming().timing(),
        capture_dir=tmp_path,
        autonomous=False,
    )
    with TestClient(create_app(runtime)) as client:
        yield client


def _run(client: TestClient, run_id: str) -> LiveObservatoryRun:
    run = client.app.state.runs.get(run_id)  # type: ignore[attr-defined]
    assert isinstance(run, LiveObservatoryRun)
    return run


def _drive(client: TestClient, run_id: str) -> None:
    run = _run(client, run_id)
    assert wait_until(lambda: run.session.queued == FRAMES)
    for _ in range(3):
        with run.lock:
            run.tick()


def test_status_without_configuration_explains_why(tmp_path: Path) -> None:
    with TestClient(create_app(None)) as client:
        status = client.get("/api/live/status").json()
        assert status["configured"] is False
        assert "RADIOWAVE_TI_CONFIG" in status["reason"]
        assert status["active_run_id"] is None
        response = client.post("/api/runs/live", json={"capture": False})
        assert response.status_code == 409
        # Replay keeps working without any sensor.
        assert client.post("/api/runs", json={"scenario_id": "01"}).status_code == 201


def test_status_with_configuration(live_client: TestClient) -> None:
    status = live_client.get("/api/live/status").json()
    assert status["configured"] is True
    assert status["sensor_id"] == RADAR
    assert status["data_port"] == "TEST-PORT"
    assert status["reason"] is None
    assert status["serial_support"] is True  # a fixture stream stands in for pyserial


def test_live_run_snapshot_has_live_mode_and_canonical_person(live_client: TestClient) -> None:
    created = live_client.post("/api/runs/live", json={"capture": False})
    assert created.status_code == 201
    initial = created.json()
    run_id = initial["state"]["run_id"]
    assert initial["state"]["mode"] == "LIVE"
    assert initial["state"]["revision"] == 1
    assert initial["state"]["live"]["sensor_id"] == RADAR
    assert initial["state"]["persons"] == []

    _drive(live_client, run_id)
    snapshot = live_client.get(f"/api/runs/{run_id}/snapshot").json()
    state = snapshot["state"]
    assert state["mode"] == "LIVE"
    assert state["revision"] > 1
    assert state["live"]["frames_parsed"] == FRAMES
    assert state["live"]["state"] in {"STREAMING", "STALE", "DISCONNECTED", "ERROR"}
    assert [p["track_id"] for p in state["persons"]] == ["P0001"]
    assert state["persons"][0]["sensor_ids"] == [RADAR]
    assert "5" not in {p["track_id"] for p in state["persons"]}
    assert snapshot["timeline"]["ground_truth"] == []
    assert live_client.get("/api/live/status").json()["active_run_id"] == run_id
    store = live_client.get(f"/api/runs/{run_id}/store").json()
    assert [s["sensor_id"] for s in store["sensors"]] == [RADAR]


def test_live_observations_cursor_tracks_pipeline_ingestion(live_client: TestClient) -> None:
    created = live_client.post("/api/runs/live", json={"capture": False}).json()
    run_id = created["state"]["run_id"]
    assert created["state"]["observations_cursor"] == 0

    _drive(live_client, run_id)
    snapshot = live_client.get(f"/api/runs/{run_id}/snapshot").json()
    state = snapshot["state"]
    # Every fixture frame carries one target and none duplicate, so all FRAMES
    # observations are accepted by the pipeline.
    assert state["observations_cursor"] == FRAMES
    assert state["observations_total"] >= FRAMES


def test_live_snapshot_reads_session_diagnostics_once(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = live_client.post("/api/runs/live", json={}).json()["state"]["run_id"]
    run = _run(live_client, run_id)
    _drive(live_client, run_id)

    calls = {"count": 0}
    original = run.session.diagnostics

    def counting_diagnostics() -> object:
        calls["count"] += 1
        return original()

    monkeypatch.setattr(run.session, "diagnostics", counting_diagnostics)

    run.snapshot()
    assert calls["count"] == 1

    run.snapshot()
    assert calls["count"] == 2


def test_replay_controls_are_409_in_live_mode(live_client: TestClient) -> None:
    run_id = live_client.post("/api/runs/live", json={}).json()["state"]["run_id"]
    assert live_client.post(f"/api/runs/{run_id}/step").status_code == 409
    assert live_client.post(f"/api/runs/{run_id}/reset").status_code == 409
    assert live_client.post(f"/api/runs/{run_id}/advance", json={"seconds": 1}).status_code == 409
    assert live_client.post(f"/api/runs/{run_id}/seek", json={"time_s": 0}).status_code == 409
    # And live controls are refused on a replay run.
    replay_id = live_client.post("/api/runs", json={"scenario_id": "01"}).json()["state"]["run_id"]
    assert live_client.post(f"/api/runs/{replay_id}/stop").status_code == 409
    assert live_client.post(f"/api/runs/{replay_id}/reconnect").status_code == 409


def test_one_live_run_at_a_time_until_stopped(live_client: TestClient) -> None:
    first = live_client.post("/api/runs/live", json={}).json()["state"]["run_id"]
    blocked = live_client.post("/api/runs/live", json={})
    assert blocked.status_code == 409
    assert first in blocked.json()["detail"]
    stopped = live_client.post(f"/api/runs/{first}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"]["finished"] is True
    assert live_client.get("/api/live/status").json()["active_run_id"] is None
    assert live_client.post("/api/runs/live", json={}).status_code == 201


def test_stop_route_holds_the_sensor_busy_until_session_actually_stops(
    live_client: TestClient,
) -> None:
    """The stop route does not have the same slot-release defect as ``delete``:
    ``LiveObservatoryRun.stop()`` only sets ``self.finished`` (which is what makes
    the run stop counting as "active") after the session has actually stopped, so
    the sensor stays reported busy for the whole teardown, never just after it."""
    run_id = live_client.post("/api/runs/live", json={}).json()["state"]["run_id"]
    run = _run(live_client, run_id)

    entered = threading.Event()
    release = threading.Event()
    original_session_stop = run.session.stop

    def blocking_session_stop(timeout_s: float = 5.0) -> bool:
        entered.set()
        assert release.wait(5.0)
        return original_session_stop(timeout_s)

    run.session.stop = blocking_session_stop  # type: ignore[method-assign]

    results: dict[str, Any] = {}

    def do_stop() -> None:
        results["status"] = live_client.post(f"/api/runs/{run_id}/stop").status_code

    thread = threading.Thread(target=do_stop)
    thread.start()
    try:
        assert wait_until(lambda: entered.is_set())
        blocked = live_client.post("/api/runs/live", json={})
        assert blocked.status_code == 409
    finally:
        release.set()
        thread.join(5.0)

    assert results["status"] == 200
    retry = live_client.post("/api/runs/live", json={})
    assert retry.status_code == 201


def test_stop_finalizes_capture_and_reconnect_bumps_generation(
    live_client: TestClient, tmp_path: Path
) -> None:
    created = live_client.post("/api/runs/live", json={"capture": True}).json()
    run_id = created["state"]["run_id"]
    capture_path = created["state"]["live"]["capture_path"]
    assert capture_path is not None and capture_path.startswith(str(tmp_path))
    _drive(live_client, run_id)
    reconnected = live_client.post(f"/api/runs/{run_id}/reconnect")
    assert reconnected.status_code == 200
    run = _run(live_client, run_id)
    assert wait_until(lambda: run.session.diagnostics().generation >= 2)
    stopped = live_client.post(f"/api/runs/{run_id}/stop").json()
    assert stopped["state"]["finished"] is True
    lines = Path(capture_path).read_text().splitlines()
    assert '"kind":"RUN_END"' in lines[-1].replace(" ", "")
    # The run stays readable after stop, and a second stop is harmless.
    assert live_client.get(f"/api/runs/{run_id}/state").status_code == 200
    assert live_client.post(f"/api/runs/{run_id}/stop").status_code == 200


def test_delete_stops_the_session(live_client: TestClient) -> None:
    run_id = live_client.post("/api/runs/live", json={}).json()["state"]["run_id"]
    run = _run(live_client, run_id)
    assert live_client.delete(f"/api/runs/{run_id}").status_code == 204
    assert wait_until(lambda: not run.session.running)
    assert run.finished


def test_delete_holds_the_live_slot_until_close_completes(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 15's teardown-side twin of ``test_concurrent_admission_exactly_one_run_starts``:
    ``delete()`` must not free the slot until ``run.close()`` (which joins the
    session's reader thread) has actually returned, or a concurrent admission could
    open a second session against the same UART while the old one is still alive."""
    run_id = live_client.post("/api/runs/live", json={}).json()["state"]["run_id"]

    entered = threading.Event()
    release = threading.Event()
    original_stop = LiveObservatoryRun.stop

    def blocking_stop(self: LiveObservatoryRun) -> None:
        entered.set()
        assert release.wait(5.0)
        original_stop(self)

    monkeypatch.setattr(LiveObservatoryRun, "stop", blocking_stop)

    results: dict[str, Any] = {}

    def do_delete() -> None:
        results["status"] = live_client.delete(f"/api/runs/{run_id}").status_code

    thread = threading.Thread(target=do_delete)
    thread.start()
    try:
        assert wait_until(lambda: entered.is_set())
        blocked = live_client.post("/api/runs/live", json={})
        assert blocked.status_code == 409
        assert "stopping" in blocked.json()["detail"]
    finally:
        release.set()
        thread.join(5.0)

    assert results["status"] == 204
    retry = live_client.post("/api/runs/live", json={})
    assert retry.status_code == 201


def test_availability_reports_a_closing_live_run_as_unavailable(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = live_client.post("/api/runs/live", json={}).json()["state"]["run_id"]

    entered = threading.Event()
    release = threading.Event()
    original_stop = LiveObservatoryRun.stop

    def blocking_stop(self: LiveObservatoryRun) -> None:
        entered.set()
        assert release.wait(5.0)
        original_stop(self)

    monkeypatch.setattr(LiveObservatoryRun, "stop", blocking_stop)

    thread = threading.Thread(target=lambda: live_client.delete(f"/api/runs/{run_id}"))
    thread.start()
    try:
        assert wait_until(lambda: entered.is_set())
        status = live_client.get("/api/live/status").json()
        assert status["reason"] is not None
        assert "stopping" in status["reason"]
    finally:
        release.set()
        thread.join(5.0)


def test_a_close_that_raises_still_releases_the_live_slot(
    live_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = live_client.post("/api/runs/live", json={}).json()["state"]["run_id"]
    original_stop = LiveObservatoryRun.stop

    def raising_stop(self: LiveObservatoryRun) -> None:
        original_stop(self)  # really stop the session first, so nothing leaks
        raise RuntimeError("boom-close")

    monkeypatch.setattr(LiveObservatoryRun, "stop", raising_stop)

    with pytest.raises(RuntimeError, match="boom-close"):
        live_client.delete(f"/api/runs/{run_id}")

    manager = live_client.app.state.runs  # type: ignore[attr-defined]
    assert manager.closing_live_run_id() is None

    retry = live_client.post("/api/runs/live", json={})
    assert retry.status_code == 201


def test_live_run_construction_closes_the_recorder_when_session_start_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``TiLiveSession.start()`` itself opens the raw capture file and creates the
    reader thread, so it can raise after already allocating OS resources — the
    comment this fix replaced wrongly claimed only the driver thread's own creation
    could still fail at that point. A failure here must close the already-open
    recorder and release the live slot instead of leaking."""
    runtime = LiveRuntime(
        config=live_config(),
        stream_factory=lambda: HoldOpenStream(frames()),
        timing=FakeTiming().timing(),
        capture_dir=tmp_path,
        autonomous=False,
    )
    recorders: list[JsonlRecorder] = []
    original_recorder_init = JsonlRecorder.__init__

    def capturing_init(self: JsonlRecorder, *args: Any, **kwargs: Any) -> None:
        original_recorder_init(self, *args, **kwargs)
        recorders.append(self)

    monkeypatch.setattr(JsonlRecorder, "__init__", capturing_init)

    original_start = TiLiveSession.start

    def failing_start(self: TiLiveSession) -> None:
        raise RuntimeError("boom-session-start")

    monkeypatch.setattr(TiLiveSession, "start", failing_start)

    with TestClient(create_app(runtime)) as client:
        with pytest.raises(RuntimeError, match="boom-session-start"):
            client.post("/api/runs/live", json={"capture": True})

        assert len(recorders) == 1
        assert recorders[0]._handle.closed

        manager = client.app.state.runs  # type: ignore[attr-defined]
        assert manager.live_run_ids() == []
        assert manager.live_reservation() is None

        status = client.get("/api/live/status").json()
        assert status["reason"] is None
        assert status["active_run_id"] is None

        monkeypatch.setattr(TiLiveSession, "start", original_start)
        retry = client.post("/api/runs/live", json={"capture": False})
        assert retry.status_code == 201


def test_replay_run_state_defaults_to_replay_mode(live_client: TestClient) -> None:
    state = live_client.post("/api/runs", json={"scenario_id": "01"}).json()["state"]
    assert state["mode"] == "REPLAY"
    assert state["live"] is None


def test_reconnect_after_stop_is_refused(live_client: TestClient) -> None:
    """Invariant 12: a user STOP never resurrects automatically."""
    run_id = live_client.post("/api/runs/live", json={}).json()["state"]["run_id"]
    assert live_client.post(f"/api/runs/{run_id}/stop").status_code == 200
    response = live_client.post(f"/api/runs/{run_id}/reconnect")
    assert response.status_code == 409
    assert "stopped" in response.json()["detail"]


def test_concurrent_admission_exactly_one_run_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 15: one physical sensor has at most one live owner, including during
    creation. Two racing ``POST /runs/live`` requests must admit exactly one run: the
    manager reserves the slot before the (slow) factory call, so a second request that
    arrives while the first is still constructing its session is refused immediately,
    and ``GET /api/live/status`` explains why while the reservation is held.
    """
    entered = threading.Event()
    release = threading.Event()
    stream_calls = {"n": 0}

    def counting_stream_factory() -> HoldOpenStream:
        stream_calls["n"] += 1
        return HoldOpenStream(frames())

    runtime = LiveRuntime(
        config=live_config(),
        stream_factory=counting_stream_factory,
        timing=FakeTiming().timing(),
        capture_dir=tmp_path,
        autonomous=False,
    )
    with TestClient(create_app(runtime)) as client:
        original_snapshot = LiveObservatoryRun.snapshot
        calls = {"n": 0}

        def blocking_snapshot(self: LiveObservatoryRun) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                entered.set()
                assert release.wait(5.0)
            return original_snapshot(self)

        monkeypatch.setattr(LiveObservatoryRun, "snapshot", blocking_snapshot)

        results: dict[str, Any] = {}

        def start_first() -> None:
            results["first"] = client.post("/api/runs/live", json={"capture": False})

        thread = threading.Thread(target=start_first)
        thread.start()
        try:
            assert wait_until(lambda: entered.is_set())

            status = client.get("/api/live/status").json()
            assert status["reason"] is not None
            assert "starting" in status["reason"]

            blocked = client.post("/api/runs/live", json={"capture": False})
            assert blocked.status_code == 409
        finally:
            release.set()
            thread.join(5.0)

        first_response = results["first"]
        assert first_response.status_code == 201
        assert stream_calls["n"] == 1  # exactly one session/stream was ever opened


def test_partial_live_start_failure_releases_the_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 16: partial live creation failure releases every resource and
    reservation. If the run's own initial snapshot fails after the session has
    already started, the session must be stopped, the capture closed, the run must
    never be published, and the slot must be free for a fresh start."""
    runtime = LiveRuntime(
        config=live_config(),
        stream_factory=lambda: HoldOpenStream(frames()),
        timing=FakeTiming().timing(),
        capture_dir=tmp_path,
        autonomous=False,
    )
    with TestClient(create_app(runtime)) as client:
        original_snapshot = LiveObservatoryRun.snapshot
        calls = {"n": 0}
        failed_runs: list[LiveObservatoryRun] = []

        def failing_snapshot(self: LiveObservatoryRun) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                failed_runs.append(self)
                raise RuntimeError("boom")
            return original_snapshot(self)

        monkeypatch.setattr(LiveObservatoryRun, "snapshot", failing_snapshot)

        with pytest.raises(RuntimeError, match="boom"):
            client.post("/api/runs/live", json={"capture": True})

        manager = client.app.state.runs  # type: ignore[attr-defined]
        assert manager.live_run_ids() == []
        assert manager.live_reservation() is None

        failed_run = failed_runs[0]
        assert not failed_run.session.running
        assert not any(
            t.name.startswith("ti-mmwave-") and t.is_alive() for t in threading.enumerate()
        )
        assert failed_run.capture_path is not None
        assert failed_run._recorder is not None
        assert failed_run._recorder._handle.closed

        status = client.get("/api/live/status").json()
        assert status["reason"] is None
        assert status["active_run_id"] is None

        retry = client.post("/api/runs/live", json={"capture": False})
        assert retry.status_code == 201


def test_close_all_stops_a_run_still_under_construction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invariant 16 / shutdown never leaves a reader thread: a run whose ``factory()``
    has already returned (session started) but whose initial snapshot is still in
    flight is not yet in ``_runs``, so ``close_all()`` must reach it through
    ``RunManager._live_constructing`` — otherwise shutdown would return with the
    sensor's reader thread still running, and the admission would go on to publish a
    run after the manager was already closed."""
    runtime = LiveRuntime(
        config=live_config(),
        stream_factory=lambda: HoldOpenStream(frames()),
        timing=FakeTiming().timing(),
        capture_dir=tmp_path,
        autonomous=False,
    )
    entered = threading.Event()
    release = threading.Event()
    original_snapshot = LiveObservatoryRun.snapshot
    captured: dict[str, LiveObservatoryRun] = {}

    def blocking_snapshot(self: LiveObservatoryRun) -> Any:
        captured["run"] = self
        entered.set()
        assert release.wait(5.0)
        return original_snapshot(self)

    monkeypatch.setattr(LiveObservatoryRun, "snapshot", blocking_snapshot)

    with TestClient(create_app(runtime)) as client:
        manager = client.app.state.runs  # type: ignore[attr-defined]
        results: dict[str, Any] = {}

        def build() -> None:
            try:
                manager.build_live_exclusive(
                    lambda run_id: LiveObservatoryRun(run_id, runtime, capture=False)
                )
            except Exception as exc:
                results["error"] = exc

        thread = threading.Thread(target=build)
        thread.start()
        try:
            assert wait_until(lambda: entered.is_set())
            manager.close_all()
        finally:
            release.set()
            thread.join(5.0)

        assert isinstance(results.get("error"), LiveRunBusyError)
        assert not captured["run"].session.running
        assert manager.live_run_ids() == []
        assert manager.live_reservation() is None
        assert not any(
            t.name.startswith("ti-mmwave-") and t.is_alive() for t in threading.enumerate()
        )


def test_driver_thread_start_failure_stops_session_and_releases_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If anything after ``session.start()`` raises inside ``LiveObservatoryRun.__init__``
    (here, the driver thread's own creation), the session must already be stopped
    before the exception propagates out of the constructor: ``build_live_exclusive``
    never gets a ``run`` reference to close in that case, so without this guard the
    started session (and its reader thread) would leak. The reservation must still be
    released so a subsequent admission succeeds."""
    runtime = LiveRuntime(
        config=live_config(),
        stream_factory=lambda: HoldOpenStream(frames()),
        timing=FakeTiming().timing(),
        capture_dir=tmp_path,
        autonomous=True,
    )
    original_start = threading.Thread.start
    calls = {"n": 0}

    def failing_start(self: threading.Thread) -> None:
        if self.name.startswith("live-run-"):
            calls["n"] += 1
            raise RuntimeError("boom-driver-thread")
        return original_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)

    with TestClient(create_app(runtime)) as client:
        manager = client.app.state.runs  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError, match="boom-driver-thread"):
            manager.build_live_exclusive(
                lambda run_id: LiveObservatoryRun(run_id, runtime, capture=False)
            )
        assert calls["n"] == 1
        assert manager.live_run_ids() == []
        assert manager.live_reservation() is None
        assert not any(
            t.name.startswith("ti-mmwave-") and t.is_alive() for t in threading.enumerate()
        )

        monkeypatch.setattr(threading.Thread, "start", original_start)
        run, _snapshot = manager.build_live_exclusive(
            lambda run_id: LiveObservatoryRun(run_id, runtime, capture=False)
        )
        assert isinstance(run, LiveObservatoryRun)
        run.close()
