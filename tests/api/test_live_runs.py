"""Live run API: availability, creation, LIVE snapshot shape, refused replay controls,
stop/reconnect, capture path, and one-live-run-at-a-time."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from radiowave.api.app import create_app
from radiowave.api.live import LiveObservatoryRun, LiveRuntime
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


def test_replay_run_state_defaults_to_replay_mode(live_client: TestClient) -> None:
    state = live_client.post("/api/runs", json={"scenario_id": "01"}).json()["state"]
    assert state["mode"] == "REPLAY"
    assert state["live"] is None
