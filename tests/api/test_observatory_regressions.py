"""Observatory API regressions from the first Codex review round (PR #2)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from radiowave.api.app import create_app
from radiowave.api.runs import _cart_sessions, _decisions_with_proposals
from radiowave.cart.models import Cart, CartState, CartStatus
from radiowave.contracts.confidence import ConfidenceDecision, Decision
from radiowave.contracts.events import RetailEvent, RetailEventType
from radiowave.contracts.sessions import SessionState, ShopperSession
from radiowave.contracts.store import EPC
from radiowave.pipeline import PipelineResult
from radiowave.simulator.scenario import SCENARIO_EPOCH

EPC_A = "3034F1A0000000000000A001"


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _create(client: TestClient, scenario_id: str = "01", **body: object) -> dict:
    response = client.post("/api/runs", json={"scenario_id": scenario_id, **body})
    assert response.status_code == 201, response.text
    return response.json()


def _strip_run_id(state: dict) -> dict:
    """Replay state without per-run metadata (id, mutation counter)."""
    return {key: value for key, value in state.items() if key not in {"run_id", "revision"}}


def _at(seconds: float):
    return SCENARIO_EPOCH + timedelta(seconds=seconds)


# --- P1: finalization stays within the advertised duration ---------------------


def test_finished_run_never_evaluates_past_its_duration(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    state = client.post(f"/api/runs/{run_id}/advance", json={"seconds": 3600}).json()["state"]
    assert state["finished"] is True
    duration = state["duration_s"]
    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
    assert events and max(event["t_s"] for event in events) <= duration
    timeline = client.get(f"/api/runs/{run_id}/timeline").json()
    assert all(marker["t_s"] <= duration for marker in timeline["markers"])
    for item in state["items"]:
        for key in ("state_since_s", "last_seen_s", "movement_start_s"):
            assert item[key] is None or item[key] <= duration
        if item["decision"] is not None:
            assert item["decision"]["at_s"] <= duration
    for person in state["persons"]:
        assert person["updated_s"] <= duration
    # The engine stepped exactly through the duration and not one interval further.
    assert state["steps"] <= duration / state["step_interval_s"] + 1


def test_finished_state_matches_incremental_state_at_duration(client: TestClient) -> None:
    first = _create(client)["run_id"]
    second = _create(client)["run_id"]
    finished = client.post(f"/api/runs/{first}/advance", json={"seconds": 3600}).json()["state"]
    duration = finished["duration_s"]
    for _ in range(int(duration / 2)):
        stepped = client.post(f"/api/runs/{second}/advance", json={"seconds": 2}).json()["state"]
    assert stepped["finished"] is True
    assert _strip_run_id(finished) == _strip_run_id(stepped)


# --- P1: decisions carry the proposal that was current when judged --------------


def test_decision_pairing_uses_the_proposal_active_at_evaluation() -> None:
    def proposal(seconds: float, shopper: str) -> RetailEvent:
        return RetailEvent(
            event_id="pick-1",
            event_type=RetailEventType.PICK,
            timestamp=_at(seconds),
            epc=EPC(value=EPC_A),
            shopper_track_id=shopper,
            confidence=0.6,
        )

    def decision(seconds: float) -> ConfidenceDecision:
        return ConfidenceDecision(
            event_id="pick-1",
            decision=Decision.WAIT,
            confidence=0.6,
            margin=0.1,
            waited_seconds=0.0,
            evaluated_at=_at(seconds),
        )

    result = PipelineResult(
        proposed_events=[proposal(8.0, "P0001"), proposal(9.0, "P0002")],
        decisions=[decision(8.0), decision(8.5), decision(9.0)],
    )
    paired = _decisions_with_proposals(result)
    assert [event.shopper_track_id for _, event in paired] == ["P0001", "P0001", "P0002"]


def test_wait_rows_keep_their_contemporaneous_attribution(client: TestClient) -> None:
    run = _create(client, "12")
    observatory_run = client.app.state.runs.get(run["run_id"])  # type: ignore[attr-defined]
    observatory_run.advance(3600)
    result = observatory_run.pipeline.result()
    paired = _decisions_with_proposals(result)
    assert len(paired) == len(result.decisions)
    proposals_by_id: dict[str, list[RetailEvent]] = {}
    for event in result.proposed_events:
        proposals_by_id.setdefault(event.event_id, []).append(event)
    # Scenario 12 re-proposes the same PICK id many times while waiting.
    assert any(len(events) > 1 for events in proposals_by_id.values())
    for decision, event in paired:
        assert event.event_id == decision.event_id
        assert event.timestamp <= decision.evaluated_at
        later = [
            e
            for e in proposals_by_id[event.event_id]
            if event.timestamp < e.timestamp <= decision.evaluated_at
        ]
        assert later == []


# --- P2: scenario 10 is fed twice ------------------------------------------------


def test_scenario_10_feeds_duplicates_and_drops_them(client: TestClient) -> None:
    baseline = _create(client, "01")
    doubled = _create(client, "10")
    assert doubled["observations_total"] == 2 * baseline["observations_total"]
    state = client.post(f"/api/runs/{doubled['run_id']}/advance", json={"seconds": 3600}).json()[
        "state"
    ]
    assert state["counters"]["dropped_duplicates"] == baseline["observations_total"]
    assert state["counters"]["accepted"] == baseline["observations_total"]
    reference = client.post(
        f"/api/runs/{baseline['run_id']}/advance", json={"seconds": 3600}
    ).json()["state"]
    assert [c["lines"] for c in state["carts"]] == [c["lines"] for c in reference["carts"]]


# --- P2: settled decisions stay visible on the item --------------------------------


def test_item_decision_survives_commit_and_review(client: TestClient) -> None:
    run = _create(client, "01")
    state = client.post(f"/api/runs/{run['run_id']}/advance", json={"seconds": 8.5}).json()["state"]
    carried = next(item for item in state["items"] if item["state"] == "CARRIED")
    assert carried["decision"] is not None
    assert carried["decision"]["decision"] == "COMMIT"
    assert carried["decision"]["event_type"] == "PICK"
    assert carried["decision"]["confidence"] >= 0.75

    ambiguous = _create(client, "12")
    end = client.post(f"/api/runs/{ambiguous['run_id']}/advance", json={"seconds": 3600}).json()[
        "state"
    ]
    reviewed = next(item for item in end["items"] if item["short_epc"] == "00A001")
    assert reviewed["decision"] is not None
    assert reviewed["decision"]["decision"] == "REVIEW"
    assert "waited" in reviewed["decision"]["reason"]


# --- P2: carts follow their own session -------------------------------------------


def test_carts_follow_their_own_session() -> None:
    def session(session_id: str, start: float, end: float | None) -> ShopperSession:
        return ShopperSession(
            session_id=session_id,
            person_track_id="P0001",
            state=SessionState.EXITED if end is not None else SessionState.ACTIVE,
            entered_at=_at(start),
            exited_at=None if end is None else _at(end),
        )

    first = Cart(cart_id="P0001", shopper_track_id="P0001", status=CartStatus.EXITED)
    second = Cart(cart_id="P0001#1", shopper_track_id="P0001", status=CartStatus.OPEN)
    result = PipelineResult(
        cart_state=CartState(
            carts={first.cart_id: first, second.cart_id: second},
            current_cart_ids={"P0001": second.cart_id},
        ),
        sessions=[session("S-P0001", 0.0, 10.0), session("S-P0001#1", 12.0, None)],
        cart_sessions={"P0001": "S-P0001"},
    )
    assert _cart_sessions(result) == {"P0001": "S-P0001", "P0001#1": "S-P0001#1"}


def test_pipeline_cart_session_mapping_is_exposed(client: TestClient) -> None:
    run = _create(client, "04")
    state = client.post(f"/api/runs/{run['run_id']}/advance", json={"seconds": 3600}).json()[
        "state"
    ]
    sessions = {s["session_id"]: s for s in state["sessions"]}
    for cart in state["carts"]:
        assert cart["session_id"] in sessions
        assert sessions[cart["session_id"]]["track_id"] == cart["shopper_track_id"]


# --- P2: seed validation ------------------------------------------------------------


def test_out_of_range_seed_is_a_validation_error(client: TestClient) -> None:
    for seed in (-1000, 2**40):
        response = client.post("/api/runs", json={"scenario_id": "01", "seed": seed})
        assert response.status_code == 422, seed
    ok = _create(client, "01", seed=11)
    assert ok["seed"] == 11


# --- P2: per-run serialization ----------------------------------------------------


def test_concurrent_steps_on_one_run_are_serialized(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: client.post(f"/api/runs/{run_id}/step"), range(40)))
    state = client.get(f"/api/runs/{run_id}/state").json()
    assert state["time_s"] == pytest.approx(40 * state["step_interval_s"])
    reference = _create(client)["run_id"]
    expected = client.post(
        f"/api/runs/{reference}/advance", json={"seconds": 40 * state["step_interval_s"]}
    ).json()["state"]
    assert _strip_run_id(state) == _strip_run_id(expected)


# --- round 2: boundary samples are evaluated; mutation + snapshot are atomic ---------


def test_finish_without_advance_evaluates_input_after_the_last_step() -> None:
    from radiowave.simulator.library import load_scenario
    from radiowave.simulator.runner import build_pipeline, scenario_observations

    scenario = load_scenario("01")
    pipeline = build_pipeline(scenario)
    end = _at(scenario.duration_s)
    for observation in scenario_observations(scenario):
        if observation.timestamp <= end:
            pipeline.ingest(observation)
    pipeline.advance_to(end)
    steps_before = pipeline.result().steps
    # Samples stamped exactly on the last step boundary were ingested after that step.
    assert pipeline._ingested_since_step is True
    result = pipeline.finish(advance=False)
    assert result.steps == steps_before + 1
    assert pipeline._last_step_at == end
    # Idempotent: a second finish evaluates nothing further.
    assert pipeline.finish(advance=False).steps == steps_before + 1


def test_finish_without_advance_is_a_noop_when_nothing_is_pending() -> None:
    from radiowave.simulator.library import load_scenario
    from radiowave.simulator.runner import build_pipeline

    pipeline = build_pipeline(load_scenario("01"))
    assert pipeline.finish(advance=False).steps == 0


def test_partial_final_interval_is_evaluated(client: TestClient) -> None:
    run = _create(client, "01")
    run_id = run["run_id"]
    interval = run["step_interval_s"]
    # Stop 0.1 s short of a step boundary so the last interval is partial, then finish.
    client.post(f"/api/runs/{run_id}/advance", json={"seconds": run["duration_s"] - interval / 2})
    state = client.post(f"/api/runs/{run_id}/advance", json={"seconds": 3600}).json()["state"]
    assert state["finished"] is True
    reference = client.post(
        f"/api/runs/{_create(client, '01')['run_id']}/advance", json={"seconds": 3600}
    ).json()["state"]
    assert _strip_run_id(state) == _strip_run_id(reference)


def test_each_mutation_returns_its_own_snapshot(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    interval = run["step_interval_s"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(lambda _: client.post(f"/api/runs/{run_id}/step"), range(40)))
    times = sorted(round(response.json()["state"]["time_s"], 6) for response in responses)
    assert times == [round((i + 1) * interval, 6) for i in range(40)]


# --- round 3: atomic snapshot, canonical scenario stream, UTC clock, listing lock ------


def test_snapshot_is_one_consistent_revision(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    assert run["revision"] >= 1
    advanced = client.post(f"/api/runs/{run_id}/advance", json={"seconds": 9}).json()["state"]
    assert advanced["revision"] == run["revision"] + 1
    snapshot = client.get(f"/api/runs/{run_id}/snapshot").json()
    assert snapshot["state"] == client.get(f"/api/runs/{run_id}/state").json()
    assert snapshot["events"]["events"] == client.get(f"/api/runs/{run_id}/events").json()["events"]
    assert snapshot["timeline"] == client.get(f"/api/runs/{run_id}/timeline").json()
    assert snapshot["state"]["revision"] == advanced["revision"]
    assert snapshot["events"]["total"] == snapshot["state"]["events_total"]
    assert client.get("/api/runs/run-9999/snapshot").status_code == 404


def test_run_scenario_feeds_scenario_10_twice() -> None:
    from radiowave.simulator.library import load_scenario
    from radiowave.simulator.runner import run_scenario

    doubled = run_scenario(load_scenario("10"))
    baseline = run_scenario(load_scenario("01"))
    assert doubled.observations_dropped == baseline.observations_accepted
    assert doubled.observations_accepted == baseline.observations_accepted
    assert [e.event_type for e in doubled.committed_events] == [
        e.event_type for e in baseline.committed_events
    ]


def test_advance_to_rejects_naive_and_normalizes_offsets() -> None:
    from datetime import datetime, timezone

    from radiowave.simulator.library import load_scenario
    from radiowave.simulator.runner import build_pipeline

    pipeline = build_pipeline(load_scenario("01"))
    with pytest.raises(ValueError, match="timezone-aware"):
        pipeline.advance_to(datetime(2026, 1, 1, 0, 0, 5))
    plus_two = timezone(timedelta(hours=2))
    pipeline.advance_to(datetime(2026, 1, 1, 2, 0, 5, tzinfo=plus_two))
    assert pipeline._last_timestamp == _at(5.0)
    assert pipeline._last_timestamp.tzinfo is not None


def test_listing_runs_while_creating_and_deleting_never_fails(client: TestClient) -> None:
    def churn(index: int) -> int:
        if index % 3 == 0:
            return client.get("/api/runs").status_code
        created = client.post("/api/runs", json={"scenario_id": "01"})
        if index % 3 == 2:
            client.delete(f"/api/runs/{created.json()['run_id']}")
        return created.status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        codes = list(pool.map(churn, range(60)))
    assert set(codes) <= {200, 201}


# --- round 4: mutations return their own snapshot; settled items keep their episode ---


def test_mutation_response_is_its_own_complete_snapshot(client: TestClient) -> None:
    run = _create(client)
    run_id = run["run_id"]
    response = client.post(f"/api/runs/{run_id}/advance", json={"seconds": 9}).json()
    assert set(response) == {"state", "events", "timeline"}
    assert response["state"]["revision"] == run["revision"] + 1
    assert response["events"]["total"] == response["state"]["events_total"]
    assert response["timeline"]["time_s"] == response["state"]["time_s"]
    assert response == client.get(f"/api/runs/{run_id}/snapshot").json()
    for op, body in (("step", None), ("seek", {"time_s": 3}), ("reset", None)):
        result = client.post(f"/api/runs/{run_id}/{op}", json=body).json()
        assert set(result) == {"state", "events", "timeline"}


def test_misplaced_item_trail_is_trimmed_to_its_movement_episode(client: TestClient) -> None:
    run = _create(client, "03")
    end = client.post(f"/api/runs/{run['run_id']}/advance", json={"seconds": 3600}).json()["state"]
    misplaced = [item for item in end["items"] if item["state"] == "MISPLACED"]
    assert misplaced, "scenario 03 must leave an item MISPLACED"
    for item in misplaced:
        assert item["movement_start_s"] is None
        assert item["episode_start_s"] is not None
        assert item["trail"]
        assert all(point["t_s"] >= item["episode_start_s"] for point in item["trail"])
    # Items that never left their fixture carry no episode and their full trail.
    resting = [item for item in end["items"] if item["state"] == "ON_FIXTURE"]
    assert resting and all(item["episode_start_s"] is None for item in resting)
