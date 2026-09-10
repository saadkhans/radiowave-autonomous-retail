"""Regressions for the sixth Codex review round."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from radiowave.cart.models import CartStatus
from radiowave.contracts import (
    EPC,
    NATIVE_TRACK_KEY,
    ItemObservation,
    ItemState,
    PersonObservation,
    PersonTrackState,
    RetailEvent,
    RetailEventType,
    SpatialUncertainty,
    Velocity,
    WorldCoordinate,
    make_event_id,
)
from radiowave.contracts.tracks import AssociationEvidence, CandidateScore
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import ItemTrackingConfig
from radiowave.fusion.tracking import ItemTrackManager
from radiowave.pipeline import FoundationPipeline
from radiowave.simulator.library import load_scenario
from radiowave.simulator.scenario import Carry, GroundTruthEvent, Scenario
from radiowave.simulator.stores import EPC_SHIRT_A, FIXTURE_F1
from tests.conftest import at

ITEM_CFG = ItemTrackingConfig(smoothing_alpha=1.0, rest_init_reads=2, stale_after_s=1.5)


def _item(t: float, x: float, y: float) -> ItemObservation:
    return ItemObservation(
        observation_id=f"rfid:rfid-f1:{t:.3f}",
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        zone_id="zone-f1",
        coordinate=WorldCoordinate(x=x, y=y, z=0.9),
        uncertainty=SpatialUncertainty.isotropic(0.5),
    )


def _person(t: float, x: float, y: float, native: str = "T1") -> PersonObservation:
    return PersonObservation(
        observation_id=f"mmwave:radar-north:{t:.3f}:{native}",
        sensor_id="radar-north",
        timestamp=at(t),
        confidence=0.9,
        coordinate=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(),
        metadata={NATIVE_TRACK_KEY: native},
    )


def _cand(track: str, score: float) -> CandidateScore:
    ev = AssociationEvidence(
        distance_score=score,
        distance_trend_score=score,
        velocity_score=score,
        temporal_score=score,
        co_motion_score=score,
        zone_score=score,
        vision_score=0.5,
        combined=score,
    )
    return CandidateScore(person_track_id=track, score=score, evidence=ev)


def test_unsorted_or_duplicate_candidate_rankings_are_rejected() -> None:
    epc = EPC(value=EPC_SHIRT_A)
    with pytest.raises(ValidationError, match="descending"):
        RetailEvent(
            event_id="e",
            event_type=RetailEventType.PICK,
            timestamp=at(0),
            epc=epc,
            confidence=0.9,
            candidates=[_cand("P1", 0.9), _cand("P2", 0.1), _cand("P3", 0.89)],
        )
    with pytest.raises(ValidationError, match="more than once"):
        RetailEvent(
            event_id="e",
            event_type=RetailEventType.PICK,
            timestamp=at(0),
            epc=epc,
            confidence=0.9,
            candidates=[_cand("P1", 0.9), _cand("P1", 0.5)],
        )
    event = RetailEvent(
        event_id="e",
        event_type=RetailEventType.PICK,
        timestamp=at(0),
        epc=epc,
        confidence=0.9,
        candidates=[_cand("P1", 0.9), _cand("P2", 0.89), _cand("P3", 0.1)],
    )
    assert event.margin == pytest.approx(0.01)


def test_metadata_must_be_json_values() -> None:
    with pytest.raises(ValidationError, match="not a JSON value"):
        _item(0.0, 1.0, 1.0).model_copy(update={"metadata": {"native": object()}}).model_validate(
            {**_item(0.0, 1.0, 1.0).model_dump(), "metadata": {"native": object()}}
        )
    ok = ItemObservation.model_validate(
        {**_item(0.0, 1.0, 1.0).model_dump(), "metadata": {"a": [1, 2.5, "x", None, {"b": True}]}}
    )
    assert ok.metadata["a"][4] == {"b": True}


def test_lost_inside_the_store_at_finish_keeps_the_cart_open(registry: StoreRegistry) -> None:
    pipeline = FoundationPipeline(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):  # shopper mid-store, then radar goes silent while an exit line exists
        pipeline.ingest(_person(i * 0.1, 6.0, 4.0))
        pipeline.ingest(_item(i * 0.1, 3.8, 6.5))
    pipeline.cart.apply(
        RetailEvent(
            event_id=make_event_id(RetailEventType.PICK, epc, at(0.5), "P0001"),
            event_type=RetailEventType.PICK,
            timestamp=at(0.5),
            epc=epc,
            shopper_track_id="P0001",
            confidence=0.9,
        )
    )
    pipeline.cart.apply(
        RetailEvent(
            event_id=make_event_id(RetailEventType.EXIT_WITH_ITEM, epc, at(0.9), "P0001"),
            event_type=RetailEventType.EXIT_WITH_ITEM,
            timestamp=at(0.9),
            epc=epc,
            shopper_track_id="P0001",
            confidence=0.9,
        )
    )
    pipeline.ingest(_item(3.0, 3.8, 6.5))  # time passes; the radar track is now LOST mid-store
    result = pipeline.finish()
    person = next(p for p in result.person_tracks if p.track_id == "P0001")
    assert person.state == PersonTrackState.LOST
    (cart,) = result.cart_state.carts.values()
    assert cart.status == CartStatus.OPEN


def test_lost_shoppers_are_not_rescored_from_item_reads(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 3.8, 5.4))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.INTERACTION_CANDIDATE
    item.movement_start_at = at(0.5)
    engine.step(at(1.05))
    for i in range(11, 25):  # radar silent, RFID keeps reading: the shopper becomes LOST
        engine.ingest(_item(i * 0.1, 5.0 + 0.05 * (i - 10), 6.0))  # clearly away from rest
        engine.step(at(i * 0.1 + 0.05))
    person = engine.persons.get("P0001")
    assert person is not None and person.state == PersonTrackState.LOST
    before = engine.ledger.score_of(epc, "P0001")
    assert before > 0.0
    for i in range(25, 40):  # still LOST: fresh item reads must not move a frozen shopper's score
        engine.ingest(_item(i * 0.1, 5.0 + 0.05 * (i - 10), 6.0))
        engine.step(at(i * 0.1 + 0.05))
    assert engine.ledger.score_of(epc, "P0001") == before
    for i in range(40, 110):  # beyond the re-acquisition window: dropped from the ledger
        engine.ingest(_item(i * 0.1, 7.0, 6.0))
        engine.step(at(i * 0.1 + 0.05))
    assert "P0001" not in engine.ledger.persons(epc)


def test_long_localization_gap_is_not_observed_motion(registry: StoreRegistry) -> None:
    manager = ItemTrackManager(ITEM_CFG)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 4.0, 6.5))
    track.state = track.rest_state = ItemState.ON_FIXTURE
    assert track.rest_position is not None
    manager.ingest(_item(30.0, 8.0, 6.5))  # reappears 4 m away after a 30 s blackout
    assert track.continuity_lost is True
    assert track.state == ItemState.UNKNOWN and track.rest_position is None
    assert track.movement_start_at is None and track.reads_beyond_threshold == 0
    manager.ingest(_item(30.1, 8.0, 6.5))
    assert track.rest_position is not None and track.rest_position.x == 8.0
    engine = BaselineFusionEngine(registry)
    for i in range(3):
        engine.ingest(_item(i * 0.1, 4.0, 6.5))
    engine.step(at(0.3))
    for i in range(8):  # default rest_init_reads: the rest position re-initializes here
        engine.ingest(_item(30.0 + 0.1 * i, 8.0, 6.5))
    events = engine.step(at(30.8))
    assert events == []
    relocated = engine.items.get(EPC(value=EPC_SHIRT_A))
    assert relocated is not None and relocated.state == ItemState.MISPLACED


def test_scenario_rejects_overlapping_carries_and_handoff_without_counterpart() -> None:
    base = load_scenario("01")
    data = base.model_dump()
    overlapping = [
        Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.0, end_t=10.0).model_dump(),
        Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=8.0).model_dump(),
    ]
    with pytest.raises(ValidationError, match="overlap"):
        Scenario.model_validate({**data, "carries": overlapping})
    handoff = GroundTruthEvent(
        t=6.0, event_type=RetailEventType.HANDOFF, epc=EPC_SHIRT_A, shopper_label="A"
    )
    with pytest.raises(ValidationError, match="counterpart_label"):
        Scenario.model_validate({**data, "expected_events": [handoff.model_dump()]})


def test_negative_durations_are_rejected() -> None:
    with pytest.raises(ValidationError):
        ItemTrackingConfig(stale_after_s=-1.0)
