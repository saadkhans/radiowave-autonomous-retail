"""Regression for the Observatory person-view cart projection (Codex finding,
``radiowave/api/runs.py`` ``ObservatoryRun._state()``).

A person's ``cart_id`` must be the OPEN cart belonging to their CURRENT session,
never a stale cart inherited from a previous session of the same reused canonical
track id. See ``tests/unit/test_state_integrity.py::
test_a_reentered_shoppers_new_cart_is_not_closed_by_a_stale_session_sweep`` for the
pipeline-level scenario this test drives through the Observatory view layer.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient

from radiowave.api.app import create_app
from radiowave.api.runs import ObservatoryRun
from radiowave.contracts import (
    EPC,
    NATIVE_TRACK_KEY,
    PersonObservation,
    RetailEvent,
    RetailEventType,
    SpatialUncertainty,
    Velocity,
    WorldCoordinate,
    make_event_id,
)
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline
from radiowave.simulator.library import load_scenario
from radiowave.simulator.stores import EPC_SHIRT_A, EPC_SHIRT_B
from tests.conftest import at


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _create(client: TestClient, scenario_id: str = "01", **body: object) -> dict:
    response = client.post("/api/runs", json={"scenario_id": scenario_id, **body})
    assert response.status_code == 201, response.text
    return response.json()["state"]


def _person(
    t: float, x: float, y: float, native: str = "T1", sensor: str = "radar-north"
) -> PersonObservation:
    return PersonObservation(
        observation_id=f"mmwave:{sensor}:{t:.3f}:{native}",
        sensor_id=sensor,
        timestamp=at(t),
        confidence=0.9,
        coordinate=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(),
        metadata={NATIVE_TRACK_KEY: native},
    )


def _pick(epc: EPC, t: float, shopper_track_id: str = "P0001") -> RetailEvent:
    return RetailEvent(
        event_id=make_event_id(RetailEventType.PICK, epc, at(t), shopper_track_id),
        event_type=RetailEventType.PICK,
        timestamp=at(t),
        epc=epc,
        shopper_track_id=shopper_track_id,
        confidence=0.9,
    )


def _build_run(registry: StoreRegistry) -> tuple[ObservatoryRun, FoundationPipeline]:
    """An ObservatoryRun wrapping a hand-driven pipeline on the shared lab registry.

    The scenario passed to the constructor only supplies catalog/product metadata
    for cosmetic fields (product names); the pipeline itself is substituted so the
    test can drive fusion/cart/session transitions directly, exactly as
    ``test_state_integrity.py`` does.
    """
    run = ObservatoryRun("run-cart-projection", load_scenario("01"))
    pipeline = FoundationPipeline(registry)
    run.pipeline = pipeline
    return run, pipeline


def test_reentered_shoppers_stale_cart_never_leaks_into_the_new_session(
    registry: StoreRegistry,
) -> None:
    run, pipeline = _build_run(registry)
    epc_a = EPC(value=EPC_SHIRT_A)
    epc_b = EPC(value=EPC_SHIRT_B)

    # Session 1 opens and picks item A: cart1 is OPEN under session 1.
    for i in range(10):
        pipeline.fusion.ingest(_person(i * 0.1, 6.0, 4.0, native="T1"))
    pipeline.fusion.step(at(1.0))
    pick_a = _pick(epc_a, 0.5)
    pipeline.cart.apply(pick_a)
    pipeline._track_cart_session(pick_a)

    state = run.state()
    persons = {p.track_id: p for p in state.persons}
    assert persons["P0001"].session_id == "S-P0001"
    assert persons["P0001"].cart_id == "P0001"

    # Walk gradually into the exit boundary: session 1 EXITED, cart1 EXITED, but
    # still present in state.carts with its own session mapping.
    for i in range(10, 20):
        x = 6.0 + (11.25 - 6.0) * (i - 9) / 10
        pipeline.fusion.ingest(_person(i * 0.1, x, 4.0, native="T1"))
        pipeline.fusion.step(at(i * 0.1 + 0.05))
    pipeline._close_carts_of_ended_sessions(at(2.0))

    state = run.state()
    persons = {p.track_id: p for p in state.persons}
    carts = {c.cart_id: c for c in state.carts}
    assert persons["P0001"].session_id == "S-P0001"
    assert persons["P0001"].cart_id is None
    assert carts["P0001"].status == "EXITED"
    assert carts["P0001"].session_id == "S-P0001"

    # Walk back onto the sales floor: fusion opens session 2 immediately, ahead of
    # any new cart-affecting commit. The person must show no cart, never cart1.
    for i in range(20, 32):
        x = 11.25 - (11.25 - 6.0) * (i - 19) / 12
        pipeline.fusion.ingest(_person(i * 0.1, x, 4.0, native="T1"))
        pipeline.fusion.step(at(i * 0.1 + 0.05))

    state = run.state()
    persons = {p.track_id: p for p in state.persons}
    assert persons["P0001"].session_id == "S-P0001-2"
    assert pipeline.fusion.sessions["P0001"].state.value == "ACTIVE"
    assert persons["P0001"].cart_id is None
    assert persons["P0001"].cart_id != "P0001"

    # First cart-affecting commit of session 2 mints cart2 (<track>#2): the person
    # now shows cart2, never the stale, EXITED cart1.
    pick_b = _pick(epc_b, 3.5)
    pipeline.cart.apply(pick_b)
    pipeline._track_cart_session(pick_b)

    state = run.state()
    persons = {p.track_id: p for p in state.persons}
    carts = {c.cart_id: c for c in state.carts}
    assert "P0001#2" in carts
    assert persons["P0001"].cart_id == "P0001#2"
    assert persons["P0001"].cart_id != "P0001"

    # Both lifecycles remain visible with their own, distinct session mapping.
    assert carts["P0001"].status == "EXITED"
    assert carts["P0001"].session_id == "S-P0001"
    assert carts["P0001#2"].status == "OPEN"
    assert carts["P0001#2"].session_id == "S-P0001-2"


def test_exited_shopper_with_committed_item_has_no_cart_but_keeps_its_cart_record(
    client: TestClient,
) -> None:
    """Scenario 04: shopper A picks an item and exits with it. Once the session is
    EXITED, the person view must show no current cart even though the (now EXITED)
    cart remains in state.carts."""
    run = _create(client, "04")
    run_id = run["run_id"]
    state = client.post(f"/api/runs/{run_id}/advance", json={"seconds": 3600}).json()["state"]
    assert state["finished"] is True

    sessions_by_id = {s["session_id"]: s for s in state["sessions"]}
    exited_carts = [c for c in state["carts"] if c["status"] == "EXITED"]
    assert exited_carts, "scenario 04 is expected to produce an exited cart"
    cart = exited_carts[0]
    assert sessions_by_id[cart["session_id"]]["state"] == "EXITED"

    person = next(p for p in state["persons"] if p["track_id"] == cart["shopper_track_id"])
    assert person["session_id"] == cart["session_id"]
    assert person["cart_id"] is None
