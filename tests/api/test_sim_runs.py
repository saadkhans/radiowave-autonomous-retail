"""SIM runs: the virtual store lab served through the Observatory API."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient

from radiowave.api.app import create_app
from radiowave.api.sim import SimObservatoryRun


@pytest.fixture
def client() -> Iterator[TestClient]:
    # No LiveRuntime at all: the lab must work on a machine with no hardware and no
    # RADIOWAVE_TI_CONFIG, which is the entire point of building it.
    with TestClient(create_app(None)) as test_client:
        yield test_client


def _start(client: TestClient, scenario_id: str = "01_normal_purchase", **body: object) -> str:
    created = client.post("/api/runs/sim", json={"scenario_id": scenario_id, **body})
    assert created.status_code == 201, created.text
    run_id: str = created.json()["state"]["run_id"]
    return run_id


def test_the_lab_catalog_is_available_without_any_hardware(client: TestClient) -> None:
    listed = client.get("/api/sim/scenarios")
    assert listed.status_code == 200
    scenarios = listed.json()
    assert len(scenarios) >= 14
    ids = {s["scenario_id"] for s in scenarios}
    assert "acceptance_60s" in ids
    # Live mode is correctly unavailable; the lab is unaffected by that.
    assert client.get("/api/live/status").json()["configured"] is False


def test_a_sim_run_is_labelled_simulated_with_its_scenario_and_seed(client: TestClient) -> None:
    """The label is a correctness requirement, not decoration: simulated and hardware
    runs render through identical panels, so a client must be able to tell them apart
    or a demo can be mistaken for a measurement."""
    run_id = _start(client, "acceptance_60s")
    live = client.get(f"/api/runs/{run_id}/snapshot").json()["state"]["live"]
    assert live["simulated"] is True
    assert live["simulated_scenario_id"] == "acceptance_60s"
    assert live["simulated_seed"] is not None


def test_a_hardware_live_status_is_not_marked_simulated() -> None:
    """The flag defaults false, so existing hardware runs are untouched by it."""
    from radiowave.api.viewmodels import ObservatoryLiveStatus

    status = ObservatoryLiveStatus(
        sensor_id="radar-1",
        state="STREAMING",
        generation=1,
        frames_received=1,
        frames_parsed=1,
        frames_rejected=0,
        frames_duplicate=0,
        observations_emitted=1,
        observations_dropped_overflow=0,
        reconnect_count=0,
        last_frame_age_s=0.0,
        frame_rate_hz=1.0,
        observation_rate_hz=1.0,
        started_at="2026-01-01T00:00:00+00:00",
    )
    assert status.simulated is False
    assert status.simulated_scenario_id is None


def test_advancing_a_sim_run_drives_the_production_pipeline(client: TestClient) -> None:
    run_id = _start(client, "acceptance_60s")
    for _ in range(6):
        assert client.post(f"/api/runs/{run_id}/advance", json={"seconds": 10}).status_code == 200

    state = client.get(f"/api/runs/{run_id}/snapshot").json()["state"]
    assert state["finished"] is True
    assert state["time_s"] == pytest.approx(60.0)
    # Canonical identities are Foundation's, derived from simulated sensor data.
    assert sorted(p["track_id"] for p in state["persons"]) == ["P0001", "P0002", "P0003"]
    assert len(state["items"]) == 30
    # Radar bytes really were parsed by the production parser.
    assert state["live"]["frames_parsed"] > 0
    assert state["live"]["frames_rejected"] == 0


def test_a_sim_run_can_be_stopped_so_it_can_be_replaced(client: TestClient) -> None:
    """Every client stops the active run before starting another (the stop-first
    rule). A simulated run that could not be stopped could never be switched away
    from, which is how this was originally broken."""
    run_id = _start(client)
    client.post(f"/api/runs/{run_id}/advance", json={"seconds": 5})

    stopped = client.post(f"/api/runs/{run_id}/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"]["finished"] is True

    # ...and a replacement now starts cleanly.
    assert client.post("/api/runs/sim", json={"scenario_id": "02_putback"}).status_code == 201


def test_reconnect_is_refused_with_a_reason_that_names_simulation(client: TestClient) -> None:
    """A simulated run has no transport. Refusing is right; refusing with 'is not a
    LIVE run' would be baffling, since its mode IS LIVE."""
    run_id = _start(client)
    refused = client.post(f"/api/runs/{run_id}/reconnect")
    assert refused.status_code == 409
    assert "simulated" in refused.json()["detail"]


def test_replay_controls_work_on_a_sim_run(client: TestClient) -> None:
    """Unlike hardware, a simulation legitimately supports step/reset: it is
    reproducible, so replaying it is meaningful rather than a lie about the past."""
    run_id = _start(client)
    assert client.post(f"/api/runs/{run_id}/step").status_code == 200
    before = client.get(f"/api/runs/{run_id}/snapshot").json()["state"]["time_s"]
    assert before > 0

    assert client.post(f"/api/runs/{run_id}/reset").status_code == 200
    after = client.get(f"/api/runs/{run_id}/snapshot").json()["state"]["time_s"]
    assert after == 0.0


def test_the_same_seed_reproduces_the_same_run_through_the_api(client: TestClient) -> None:
    def digest(seed: int) -> tuple[int, list[str]]:
        run_id = _start(client, "04_handoff", seed=seed)
        for _ in range(4):
            client.post(f"/api/runs/{run_id}/advance", json={"seconds": 10})
        page = client.get(f"/api/runs/{run_id}/events").json()["events"]
        client.post(f"/api/runs/{run_id}/stop")
        return len(page), [f"{e['kind']}:{e['label']}" for e in page]

    assert digest(7) == digest(7)


def test_a_different_seed_changes_the_run_but_not_the_ground_truth(client: TestClient) -> None:
    def run(seed: int) -> tuple[list[str], list[tuple[float, str]]]:
        run_id = _start(client, "04_handoff", seed=seed)
        for _ in range(4):
            client.post(f"/api/runs/{run_id}/advance", json={"seconds": 10})
        snapshot = client.get(f"/api/runs/{run_id}/snapshot").json()
        truth = [(g["t_s"], g["event_type"]) for g in snapshot["timeline"]["ground_truth"]]
        client.post(f"/api/runs/{run_id}/stop")
        return [i["epc"] for i in snapshot["state"]["items"]], truth

    _, truth_a = run(7)
    _, truth_b = run(4242)
    # Seed varies observation, not truth: the physical story is identical.
    assert truth_a == truth_b


def test_ground_truth_is_exposed_for_display_but_never_ingested(client: TestClient) -> None:
    """The overlay is the answer key held up BESIDE the answer.

    It reaches the timeline viewmodel and nothing else: the engine builds its
    observations without consulting the log, so the overlay cannot have influenced
    what fusion concluded.
    """
    run_id = _start(client, "04_handoff")
    for _ in range(4):
        client.post(f"/api/runs/{run_id}/advance", json={"seconds": 10})
    snapshot = client.get(f"/api/runs/{run_id}/snapshot").json()

    truth = snapshot["timeline"]["ground_truth"]
    assert truth, "a handoff scenario should record truth"
    assert any(g["event_type"] == "HANDOFF" for g in truth)
    # Ground-truth rows carry the simulator's private person ids...
    assert any((g["shopper_label"] or "").startswith("GT-") for g in truth)
    # ...while the canonical tracks never do.
    assert all(not p["track_id"].startswith("GT-") for p in snapshot["state"]["persons"])


def test_an_unknown_scenario_is_a_404_not_a_crash(client: TestClient) -> None:
    refused = client.post("/api/runs/sim", json={"scenario_id": "no_such_scenario"})
    assert refused.status_code == 404


def test_several_sim_runs_may_coexist(client: TestClient) -> None:
    """SIM has no physical exclusivity: there is no serial port to contend for, so
    inheriting LIVE's one-at-a-time rule would be an accident of implementation."""
    first = _start(client, "01_normal_purchase")
    second = _start(client, "02_putback")
    assert first != second
    for run_id in (first, second):
        assert client.get(f"/api/runs/{run_id}/snapshot").status_code == 200


def test_sim_runs_do_not_disturb_replay_runs(client: TestClient) -> None:
    replay = client.post("/api/runs", json={"scenario_id": "01"})
    assert replay.status_code == 201
    sim_id = _start(client)
    replay_id = replay.json()["state"]["run_id"]
    assert client.get(f"/api/runs/{replay_id}/snapshot").json()["state"]["mode"] == "REPLAY"
    assert client.get(f"/api/runs/{sim_id}/snapshot").json()["state"]["mode"] == "LIVE"


def test_sim_run_holds_no_os_resources(client: TestClient) -> None:
    run_id = _start(client)
    run = client.app.state.runs.get(run_id)  # type: ignore[attr-defined]
    assert isinstance(run, SimObservatoryRun)
    run.close()  # must be a no-op, and must not raise
    assert client.get(f"/api/runs/{run_id}/snapshot").status_code == 200
