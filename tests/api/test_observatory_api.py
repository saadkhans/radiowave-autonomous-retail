"""Observatory API tests: scenario listing, run lifecycle, determinism, isolation."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from radiowave.api.app import create_app
from radiowave.simulator.library import SCENARIOS


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _create(client: TestClient, scenario_id: str = "01") -> dict:
    response = client.post("/api/runs", json={"scenario_id": scenario_id})
    assert response.status_code == 201, response.text
    return response.json()


def _strip_run_id(state: dict) -> dict:
    return {key: value for key, value in state.items() if key != "run_id"}


def test_health(client: TestClient) -> None:
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["engine"] == "foundation-v0"


def test_list_scenarios_matches_library(client: TestClient) -> None:
    body = client.get("/api/scenarios").json()
    ids = [entry["scenario_id"] for entry in body]
    assert ids == list(SCENARIOS)
    assert "12v" in ids
    for entry in body:
        assert entry["description"]
        assert entry["duration_s"] > 0


def test_scenario_detail_carries_store_twin(client: TestClient) -> None:
    body = client.get("/api/scenarios/01").json()
    store = body["store"]
    floor = store["floor"]
    assert floor["max_x"] > floor["min_x"] and floor["max_y"] > floor["min_y"]
    assert store["units"] == "m"
    assert store["zones"] and store["fixtures"] and store["boundaries"]
    kinds = {boundary["kind"] for boundary in store["boundaries"]}
    assert kinds == {"ENTRY", "EXIT"}
    assert all("sensor_id" in sensor for sensor in store["sensors"])


def test_unknown_scenario_is_404(client: TestClient) -> None:
    assert client.get("/api/scenarios/99").status_code == 404
    response = client.post("/api/runs", json={"scenario_id": "99"})
    assert response.status_code == 404


def test_unknown_run_is_404(client: TestClient) -> None:
    assert client.get("/api/runs/run-9999/state").status_code == 404
    assert client.post("/api/runs/run-9999/step").status_code == 404
    assert client.post("/api/runs/run-9999/advance", json={"seconds": 1}).status_code == 404


def test_create_run_starts_at_time_zero(client: TestClient) -> None:
    run = _create(client)
    assert run["run_id"] == "run-0001"
    assert run["scenario_id"] == "01"
    assert run["time_s"] == 0.0
    assert run["finished"] is False
    assert run["persons"] == []
    assert run["observations_total"] > 0
    # Nothing has been observed yet: every catalog EPC is UNKNOWN and unlocalized.
    assert all(item["state"] == "UNKNOWN" and item["x"] is None for item in run["items"])


def test_step_advances_by_one_interval(client: TestClient) -> None:
    run = _create(client)
    stepped = client.post(f"/api/runs/{run['run_id']}/step").json()
    assert stepped["time_s"] == pytest.approx(run["step_interval_s"])
    assert stepped["steps"] >= 1


def test_advance_produces_scenario_01_pick_and_cart(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    state = client.post(f"/api/runs/{run_id}/advance", json={"seconds": 9}).json()
    assert state["time_s"] == pytest.approx(9.0)
    assert len(state["persons"]) == 1
    person = state["persons"][0]
    assert person["state"] == "ACTIVE"
    assert person["trail"]
    assert person["session_id"]
    item = next(entry for entry in state["items"] if entry["state"] == "CARRIED")
    assert item["carrier_track_id"] == person["track_id"]
    assert item["candidates"][0]["track_id"] == person["track_id"]
    assert item["candidates"][0]["features"]
    cart = next(entry for entry in state["carts"] if entry["status"] == "OPEN")
    assert cart["shopper_track_id"] == person["track_id"]
    assert [line["epc"] for line in cart["lines"]] == [item["epc"]]

    events = client.get(f"/api/runs/{run_id}/events").json()
    labels = [event["label"] for event in events["events"]]
    assert "PERSON_TRACK_CREATED" in labels
    assert "SESSION_OPENED" in labels
    assert "INTERACTION_CANDIDATE" in labels
    committed = [
        event
        for event in events["events"]
        if event["label"] == "PICK" and event["decision"] == "COMMIT"
    ]
    assert committed and committed[0]["epc"] == item["epc"]
    assert committed[0]["confidence"] is not None


def test_advance_to_end_finishes_run(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    state = client.post(f"/api/runs/{run_id}/advance", json={"seconds": 3600}).json()
    assert state["finished"] is True
    assert state["time_s"] == pytest.approx(state["duration_s"])
    assert state["observations_cursor"] == state["observations_total"]
    timeline = client.get(f"/api/runs/{run_id}/timeline").json()
    labels = {marker["label"] for marker in timeline["markers"]}
    assert "PICK" in labels
    assert timeline["ground_truth"]


def test_advance_rejects_non_positive_seconds(client: TestClient) -> None:
    run = _create(client)
    response = client.post(f"/api/runs/{run['run_id']}/advance", json={"seconds": 0})
    assert response.status_code == 422


def test_reset_returns_to_initial_state(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    client.post(f"/api/runs/{run_id}/advance", json={"seconds": 9})
    reset = client.post(f"/api/runs/{run_id}/reset").json()
    assert _strip_run_id(reset) == _strip_run_id(run)
    events = client.get(f"/api/runs/{run_id}/events").json()
    assert events["events"] == []


def test_replay_is_deterministic_across_runs_and_step_sizes(client: TestClient) -> None:
    first = _create(client)["run_id"]
    second = _create(client)["run_id"]
    assert first != second
    state_a = client.post(f"/api/runs/{first}/advance", json={"seconds": 12}).json()
    for _ in range(6):
        state_b = client.post(f"/api/runs/{second}/advance", json={"seconds": 2}).json()
    assert _strip_run_id(state_a) == _strip_run_id(state_b)
    events_a = client.get(f"/api/runs/{first}/events").json()["events"]
    events_b = client.get(f"/api/runs/{second}/events").json()["events"]
    assert events_a == events_b


def test_seek_backwards_replays_from_start(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    client.post(f"/api/runs/{run_id}/advance", json={"seconds": 12})
    sought = client.post(f"/api/runs/{run_id}/seek", json={"time_s": 5}).json()
    assert sought["time_s"] == pytest.approx(5.0)
    assert all(item["state"] == "ON_FIXTURE" for item in sought["items"])
    fresh = _create(client)["run_id"]
    direct = client.post(f"/api/runs/{fresh}/advance", json={"seconds": 5}).json()
    assert _strip_run_id(sought) == _strip_run_id(direct)


def test_runs_are_isolated(client: TestClient) -> None:
    first = _create(client, "01")["run_id"]
    second = _create(client, "02")["run_id"]
    client.post(f"/api/runs/{first}/advance", json={"seconds": 9})
    untouched = client.get(f"/api/runs/{second}/state").json()
    assert untouched["time_s"] == 0.0
    assert untouched["scenario_id"] == "02"
    assert untouched["persons"] == []
    listing = client.get("/api/runs").json()
    assert set(listing) == {first, second}
    assert client.delete(f"/api/runs/{first}").status_code == 204
    assert client.get(f"/api/runs/{first}/state").status_code == 404


def test_events_pagination_uses_since_cursor(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    client.post(f"/api/runs/{run_id}/advance", json={"seconds": 9})
    page = client.get(f"/api/runs/{run_id}/events", params={"limit": 2}).json()
    assert len(page["events"]) == 2
    rest = client.get(f"/api/runs/{run_id}/events", params={"since": page["next_seq"]}).json()
    assert rest["events"][0]["seq"] == page["next_seq"]
    assert len(page["events"]) + len(rest["events"]) == page["total"]


def test_view_models_use_canonical_identities(client: TestClient) -> None:
    run = _create(client)
    state = client.post(f"/api/runs/{run['run_id']}/advance", json={"seconds": 9}).json()
    for person in state["persons"]:
        assert person["track_id"].startswith("P")
        assert isinstance(person["sensor_ids"], list)
    for item in state["items"]:
        assert len(item["epc"]) >= 6
        assert item["short_epc"] == item["epc"][-6:]
        assert item["gtin"]


@pytest.mark.parametrize("scenario_id", list(SCENARIOS))
def test_every_scenario_runs_to_completion(client: TestClient, scenario_id: str) -> None:
    run = _create(client, scenario_id)
    state = client.post(f"/api/runs/{run['run_id']}/advance", json={"seconds": 3600}).json()
    assert state["finished"] is True
    events = client.get(f"/api/runs/{run['run_id']}/events").json()
    assert events["total"] == len(events["events"])


def test_scenario_12_shows_ambiguity_and_12v_resolves(client: TestClient) -> None:
    ambiguous = _create(client, "12")["run_id"]
    resolved = _create(client, "12v")["run_id"]
    client.post(f"/api/runs/{ambiguous}/advance", json={"seconds": 3600})
    client.post(f"/api/runs/{resolved}/advance", json={"seconds": 3600})
    ambiguous_events = client.get(f"/api/runs/{ambiguous}/events").json()["events"]
    resolved_events = client.get(f"/api/runs/{resolved}/events").json()["events"]
    ambiguous_decisions = {
        event["decision"] for event in ambiguous_events if event["kind"] == "RETAIL_EVENT"
    }
    assert ambiguous_decisions & {"WAIT", "REVIEW"}
    resolved_picks = [
        event
        for event in resolved_events
        if event["label"] == "PICK" and event["decision"] == "COMMIT"
    ]
    assert resolved_picks
