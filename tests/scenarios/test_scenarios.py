"""Deterministic end-to-end scenario tests. Every test asserts final state, not execution."""

from __future__ import annotations

from functools import cache

import pytest

from radiowave.cart.models import CartStatus
from radiowave.contracts import Decision, ItemState, RetailEventType, SessionState
from radiowave.contracts.tracks import PersonTrackState
from radiowave.pipeline import PipelineResult
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import run_observations, run_scenario, scenario_observations
from radiowave.simulator.stores import EPC_SHIRT_A, EPC_SHIRT_B, GTIN_BLACK_SHIRT_L

PICK, CARRY, PUTBACK = RetailEventType.PICK, RetailEventType.CARRY, RetailEventType.PUTBACK
MISPLACE, HANDOFF, EXIT = (
    RetailEventType.MISPLACE,
    RetailEventType.HANDOFF,
    RetailEventType.EXIT_WITH_ITEM,
)


@cache
def run(scenario_id: str) -> PipelineResult:
    return run_scenario(load_scenario(scenario_id))


def shoppers(result: PipelineResult) -> list[str]:
    """Canonical track ids in order of first appearance (A, B, ...)."""
    return [t.track_id for t in sorted(result.person_tracks, key=lambda t: t.created_at)]


def committed(result: PipelineResult) -> list[tuple[RetailEventType, str, str | None, str | None]]:
    return [
        (e.event_type, e.epc.value, e.shopper_track_id, e.counterpart_track_id)
        for e in result.committed_events
    ]


def test_scenario_01_single_shopper_picks_one_item() -> None:
    r = run("01")
    (a,) = shoppers(r)
    assert committed(r) == [(PICK, EPC_SHIRT_A, a, None), (CARRY, EPC_SHIRT_A, a, None)]
    assert r.cart_state.epcs_in(a) == {EPC_SHIRT_A}
    item = r.item(EPC_SHIRT_A)
    assert item.state == ItemState.CARRIED and item.carrier_track_id == a
    assert r.item(EPC_SHIRT_B).state == ItemState.ON_FIXTURE
    assert r.review_events == []
    pick = r.committed_events[0]
    assert pick.candidates and pick.candidates[0].evidence.weights  # evidence is exposed
    assert pick.confidence >= 0.75 and pick.fixture_id == "F1"


def test_scenario_02_putback_to_original_fixture() -> None:
    r = run("02")
    (a,) = shoppers(r)
    assert [c[0] for c in committed(r)] == [PICK, CARRY, PUTBACK]
    assert all(c[2] == a for c in committed(r))
    assert r.cart_state.epcs_in(a) == set()
    item = r.item(EPC_SHIRT_A)
    assert item.state == ItemState.ON_FIXTURE and item.carrier_track_id is None
    assert r.cart_state.unresolved == {}
    assert [s.state for s in r.sessions] == [SessionState.EXITED]


def test_scenario_03_misplace_at_wrong_fixture() -> None:
    r = run("03")
    (a,) = shoppers(r)
    assert [c[0] for c in committed(r)] == [PICK, CARRY, MISPLACE]
    misplace = r.committed_events[-1]
    assert misplace.shopper_track_id == a and misplace.fixture_id == "F2"
    assert r.cart_state.epcs_in(a) == set()
    item = r.item(EPC_SHIRT_A)
    assert item.state == ItemState.MISPLACED
    assert item.rest_position is not None and item.rest_position.x > 7.0


def test_scenario_04_exit_with_item_preserves_ownership() -> None:
    r = run("04")
    (a,) = shoppers(r)
    assert [c[0] for c in committed(r)] == [PICK, CARRY, EXIT]
    cart = r.cart_state.carts[a]
    assert cart.status == CartStatus.EXITED
    assert cart.lines[EPC_SHIRT_A].final_ownership_candidate is True
    assert r.item(EPC_SHIRT_A).state == ItemState.EXITED
    assert (
        r.sessions[0].state == SessionState.EXITED and r.sessions[0].exit_boundary_id == "exit-gate"
    )


def test_scenario_05_handoff_transfers_cart_line() -> None:
    r = run("05")
    a, b = shoppers(r)
    events = committed(r)
    assert events[0] == (PICK, EPC_SHIRT_A, a, None)
    assert (HANDOFF, EPC_SHIRT_A, a, b) in events
    assert r.cart_state.epcs_in(a) == set()
    assert r.cart_state.epcs_in(b) == {EPC_SHIRT_A}
    assert r.item(EPC_SHIRT_A).carrier_track_id == b
    handoff = next(e for e in r.committed_events if e.event_type == HANDOFF)
    assert handoff.margin >= 0.25 and handoff.candidates[0].person_track_id == b


def test_scenario_06_two_shoppers_same_fixture_waits_then_resolves() -> None:
    r = run("06")
    a, b = shoppers(r)
    picks = [e for e in r.committed_events if e.event_type == PICK]
    assert [(e.epc.value, e.shopper_track_id) for e in picks] == [(EPC_SHIRT_A, a)]
    assert r.decisions_of(Decision.WAIT)  # ambiguity was not committed prematurely
    assert r.cart_state.epcs_in(a) == {EPC_SHIRT_A}
    assert r.cart_state.epcs_in(b) == set()
    assert {c.person_track_id for c in picks[0].candidates} == {a, b}
    assert r.review_events == []


def test_scenario_07_identical_skus_stay_individually_tracked() -> None:
    r = run("07")
    a, b = shoppers(r)
    assert r.item(EPC_SHIRT_A).gtin == r.item(EPC_SHIRT_B).gtin == GTIN_BLACK_SHIRT_L
    assert r.cart_state.epcs_in(a) == {EPC_SHIRT_A}
    assert r.cart_state.epcs_in(b) == {EPC_SHIRT_B}
    assert r.item(EPC_SHIRT_A).carrier_track_id == a
    assert r.item(EPC_SHIRT_B).carrier_track_id == b
    assert r.review_events == [] and r.cart_state.unresolved == {}


def test_scenario_08_rfid_dropout_does_not_corrupt_state() -> None:
    r = run("08")
    (a,) = shoppers(r)
    assert [c[0] for c in committed(r)] == [PICK, CARRY]
    assert r.cart_state.epcs_in(a) == {EPC_SHIRT_A}
    assert r.item(EPC_SHIRT_A).state == ItemState.CARRIED
    assert r.observations_accepted < run("01").observations_accepted  # reads really were lost


def test_scenario_09_radar_dropout_reacquires_same_shopper() -> None:
    r = run("09")
    assert len(r.person_tracks) == 1
    (a,) = shoppers(r)
    assert r.person_tracks[0].state == PersonTrackState.ACTIVE
    assert r.person_tracks[0].observation_count < run("01").person_tracks[0].observation_count
    assert [c[0] for c in committed(r)] == [PICK, CARRY]
    assert r.cart_state.epcs_in(a) == {EPC_SHIRT_A}


def test_scenario_10_duplicate_observation_replay_is_idempotent() -> None:
    scenario = load_scenario("10")
    observations = scenario_observations(scenario)
    doubled = sorted(observations + observations, key=lambda o: (o.timestamp, o.observation_id))
    r = run_observations(scenario, doubled)
    baseline = run("01")
    assert r.observations_dropped == len(observations)
    assert r.observations_accepted == baseline.observations_accepted
    assert committed(r) == committed(baseline)
    (a,) = shoppers(r)
    assert r.cart_state.epcs_in(a) == {EPC_SHIRT_A}
    assert len(r.cart_state.carts[a].lines) == 1


def test_scenario_11_rfid_jitter_never_becomes_a_pick() -> None:
    r = run("11")
    assert r.committed_events == [] and r.proposed_events == []
    assert all(t.state == ItemState.ON_FIXTURE for t in r.item_tracks)
    assert r.cart_state.carts == {}


def test_scenario_12_ambiguous_pick_waits_and_never_misattributes() -> None:
    r = run("12")
    a, b = shoppers(r)
    assert r.committed_events == []
    assert r.cart_state.carts == {}
    assert len(r.decisions_of(Decision.WAIT)) > 10
    assert [d.decision for d in r.decisions][-1] == Decision.REVIEW
    proposal = r.proposed_events[-1]
    assert proposal.event_type == PICK and {c.person_track_id for c in proposal.candidates} == {
        a,
        b,
    }
    assert proposal.margin < 0.25
    assert r.item(EPC_SHIRT_A).state == ItemState.CARRIED
    assert r.item(EPC_SHIRT_A).carrier_track_id is None


def test_scenario_12v_vision_evidence_resolves_the_ambiguity() -> None:
    r = run("12v")
    a, b = shoppers(r)
    assert committed(r)[0] == (PICK, EPC_SHIRT_A, a, None)
    assert r.cart_state.epcs_in(a) == {EPC_SHIRT_A} and r.cart_state.epcs_in(b) == set()
    pick = r.committed_events[0]
    assert pick.candidates[0].evidence.vision_score == 1.0
    assert "vision" in pick.candidates[0].evidence.weights


def test_scenario_13_departed_companion_no_longer_blocks_attribution() -> None:
    r = run("13")
    a, b = shoppers(r)
    departed = next(t for t in r.person_tracks if t.track_id == b)
    assert departed.state == PersonTrackState.ENDED
    assert r.decisions_of(Decision.WAIT)  # ambiguous while both were present
    assert committed(r)[0] == (PICK, EPC_SHIRT_A, a, None)
    pick = r.committed_events[0]
    assert [c.person_track_id for c in pick.candidates] == [a]  # B dropped from the ledger
    assert r.cart_state.epcs_in(a) == {EPC_SHIRT_A}
    assert b not in r.cart_state.carts
    assert r.review_events == []


@pytest.mark.parametrize("scenario_id", ["01", "05", "12"])
def test_no_vendor_identifier_becomes_canonical_identity(scenario_id: str) -> None:
    r = run(scenario_id)
    for track in r.person_tracks:
        assert track.track_id.startswith("P") and "T" not in track.track_id[1:]
        assert len(track.contributing_sensor_ids) == 2  # both radars fed the same person
