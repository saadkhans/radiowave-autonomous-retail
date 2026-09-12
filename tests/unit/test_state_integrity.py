"""State-integrity regressions: frozen-evidence attribution, unobserved episodes,
stale-carrier exits, vision gating, duplicate-timestamp evidence and cart terminality.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from radiowave.adapters.rfid.base import NativeRfidRead
from radiowave.cart.engine import InMemoryCartEngine
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
    SessionState,
    SpatialUncertainty,
    Velocity,
    VisionEvidence,
    VisionEvidenceKind,
    WorldCoordinate,
    make_event_id,
)
from radiowave.contracts.tracks import AssociationEvidence
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.association import AssociationScorer, PairState
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import AssociationConfig, ItemTrackingConfig, PersonTrackingConfig
from radiowave.fusion.tracking import ItemTrackManager, ItemTrackState, PersonState
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.pipeline import FoundationPipeline
from radiowave.replay.recorder import InMemoryRecorder
from radiowave.simulator.stores import EPC_SHIRT_A, EPC_SHIRT_B, FIXTURE_F1
from tests.conftest import at

ITEM_CFG = ItemTrackingConfig(smoothing_alpha=1.0, rest_init_reads=2, stale_after_s=1.5)


def _item(
    t: float, x: float, y: float, sensor: str = "rfid-f1", zone: str = "zone-f1"
) -> ItemObservation:
    return ItemObservation(
        observation_id=f"rfid:{sensor}:{t:.3f}",
        sensor_id=sensor,
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        zone_id=zone,
        coordinate=WorldCoordinate(x=x, y=y, z=0.9),
        uncertainty=SpatialUncertainty.isotropic(0.5),
    )


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


def _evidence(score: float) -> AssociationEvidence:
    return AssociationEvidence(
        distance_score=score,
        distance_trend_score=score,
        velocity_score=score,
        temporal_score=score,
        co_motion_score=score,
        zone_score=score,
        vision_score=0.5,
        combined=score,
    )


# ---------------------------------------------------------------------------------
# FIX 1 - a LOST shopper's frozen ledger score must not lead attribution.


def test_lost_shoppers_frozen_score_does_not_lead_attribution(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 3.8, 5.6, native="T1", sensor="radar-north"))
        engine.ingest(_person(i * 0.1, 5.2, 5.6, native="T2", sensor="radar-south"))
    engine.step(at(1.0))
    assert {p.track_id for p in engine.persons.all} == {"P0001", "P0002"}

    # Seed the ledger directly: P0002 leads, P0001 trails.
    for _ in range(10):
        engine.ledger.update(epc, "P0002", _evidence(0.9), at(1.0))
        engine.ledger.update(epc, "P0001", _evidence(0.6), at(1.0))

    # Advance time with only P0001 producing fresh samples; P0002 goes silent and LOST
    # (lost_after_s defaults to 1.0 s), but stays inside the re-acquisition window.
    for i in range(11, 30):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 3.8, 5.6, native="T1", sensor="radar-north"))
        engine.step(at(i * 0.1 + 0.05))
    p2 = engine.persons.get("P0002")
    assert p2 is not None and p2.state == PersonTrackState.LOST

    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.CARRIED
    item.carrier_track_id = None
    item.movement_start_at = at(2.5)

    events = engine.step(at(3.0))
    pick = next(e for e in events if e.event_type == RetailEventType.PICK)
    assert pick.shopper_track_id == "P0001"
    assert "P0002" not in {c.person_track_id for c in pick.candidates}
    # The pair itself is retained (not dropped) so re-acquisition would restore it.
    assert "P0002" in engine.ledger.persons(epc)
    assert engine.ledger.score_of(epc, "P0002") > 0.8


# ---------------------------------------------------------------------------------
# FIX 2 - a localization gap on a CARRIED item with no committed carrier ends the episode.


def test_break_continuity_ends_an_uncommitted_carry() -> None:
    manager = ItemTrackManager(ITEM_CFG)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 4.0, 6.5))
    track.state = ItemState.CARRIED
    track.carrier_track_id = None
    track.movement_start_at = at(0.5)
    manager.ingest(_item(30.0, 8.0, 6.5))  # gap far beyond reset_after_s (3.0 s default)
    assert track.state == ItemState.UNKNOWN
    assert track.movement_start_at is None
    assert track.rest_position is None


def test_break_continuity_keeps_a_committed_carry() -> None:
    manager = ItemTrackManager(ITEM_CFG)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 4.0, 6.5))
    track.state = ItemState.CARRIED
    track.carrier_track_id = "P0001"
    track.movement_start_at = at(0.5)
    manager.ingest(_item(30.0, 8.0, 6.5))
    assert track.state == ItemState.CARRIED
    assert track.movement_start_at == at(0.5)


def test_break_continuity_clears_review_suppression_for_an_uncommitted_carry() -> None:
    manager = ItemTrackManager(ITEM_CFG)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 4.0, 6.5))
    track.state = ItemState.CARRIED
    track.carrier_track_id = None
    track.attribution_unresolved = True
    track.carry_announced = True
    manager.ingest(_item(30.0, 8.0, 6.5))  # gap far beyond reset_after_s (3.0 s default)
    assert track.state == ItemState.UNKNOWN
    assert track.attribution_unresolved is False
    assert track.carry_announced is False


def test_break_continuity_keeps_suppression_flags_for_a_committed_carry() -> None:
    manager = ItemTrackManager(ITEM_CFG)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 4.0, 6.5))
    track.state = ItemState.CARRIED
    track.carrier_track_id = "P0001"
    track.attribution_unresolved = True
    track.carry_announced = True
    manager.ingest(_item(30.0, 8.0, 6.5))
    assert track.state == ItemState.CARRIED
    assert track.attribution_unresolved is True
    assert track.carry_announced is True


def test_engine_pending_pick_stops_being_active_after_an_uncommitted_gap(
    registry: StoreRegistry,
) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.CARRIED
    item.carrier_track_id = None
    item.movement_start_at = at(0.5)
    event = RetailEvent(
        event_id=make_event_id(RetailEventType.PICK, epc, item.movement_start_at),
        event_type=RetailEventType.PICK,
        timestamp=at(0.5),
        epc=epc,
        shopper_track_id=None,
        confidence=0.5,
    )
    assert engine.is_active(event) is True
    engine.ingest(_item(30.0, 8.0, 6.5))  # localization gap with no committed carrier
    assert engine.is_active(event) is False


# ---------------------------------------------------------------------------------
# FIX 3 - the zone-only portal exit must not use a stale (LOST) carrier position.


def test_zone_only_exit_ignores_a_lost_carriers_stale_position(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 11.2, 4.0, native="T1"))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.CARRIED
    item.carrier_track_id = "P0001"
    item.rest_position = WorldCoordinate(x=3.8, y=6.5, z=0.9)

    engine.step(at(3.0))  # no further P0001 samples: > lost_after_s (1.0 s) -> LOST
    person = engine.persons.get("P0001")
    assert person is not None and person.state == PersonTrackState.LOST

    engine.ingest(
        ItemObservation(
            observation_id="rfid:exit:zoneonly",
            sensor_id="rfid-exit",
            timestamp=at(3.1),
            confidence=0.8,
            epc=epc,
            zone_id="exit",
            coordinate=None,
            uncertainty=None,
        )
    )
    events = engine.step(at(3.2))
    assert not any(e.event_type == RetailEventType.EXIT_WITH_ITEM for e in events)
    assert item.state == ItemState.CARRIED


def test_zone_only_exit_uses_an_active_carriers_current_position(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 11.2, 4.0, native="T1"))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.CARRIED
    item.carrier_track_id = "P0001"
    item.rest_position = WorldCoordinate(x=3.8, y=6.5, z=0.9)

    for i in range(10, 31):  # keep the carrier ACTIVE through the door
        engine.ingest(_person(i * 0.1, 11.2, 4.0, native="T1"))
    engine.ingest(
        ItemObservation(
            observation_id="rfid:exit:zoneonly",
            sensor_id="rfid-exit",
            timestamp=at(3.1),
            confidence=0.8,
            epc=epc,
            zone_id="exit",
            coordinate=None,
            uncertainty=None,
        )
    )
    events = engine.step(at(3.2))
    person = engine.persons.get("P0001")
    assert person is not None and person.state == PersonTrackState.ACTIVE
    assert item.state == ItemState.EXITED or any(
        e.event_type == RetailEventType.EXIT_WITH_ITEM for e in events
    )


# ---------------------------------------------------------------------------------
# FIX 4 - vision samples only count as new evidence when vision is enabled.


def _score_delta(registry: StoreRegistry, vision_enabled: bool) -> tuple[float, float]:
    engine = BaselineFusionEngine(registry, vision_enabled=vision_enabled)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 3.8, 5.6, native="T1"))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.INTERACTION_CANDIDATE
    item.movement_start_at = at(0.5)
    engine.step(at(1.05))
    before = engine.ledger.score_of(epc, "P0001")
    engine.ingest(
        VisionEvidence(
            observation_id="vision:1",
            sensor_id="cam-main",
            timestamp=at(1.1),
            confidence=0.9,
            kind=VisionEvidenceKind.REACH_INTO_FIXTURE,
            coordinate=WorldCoordinate(x=3.8, y=6.5, z=1.2),
            uncertainty=SpatialUncertainty.isotropic(0.2),
        )
    )
    engine.step(at(1.15))  # no new item/person sample: only vision arrived
    after = engine.ledger.score_of(epc, "P0001")
    return before, after


def test_vision_sample_is_not_new_evidence_when_vision_disabled(registry: StoreRegistry) -> None:
    before, after = _score_delta(registry, vision_enabled=False)
    assert before == after


def test_vision_sample_is_new_evidence_when_vision_enabled(registry: StoreRegistry) -> None:
    before, after = _score_delta(registry, vision_enabled=True)
    assert before != after


# ---------------------------------------------------------------------------------
# FIX 5 - duplicate same-timestamp samples must not over-weight the evidence windows.


def test_duplicate_same_timestamp_scoring_does_not_double_count_windows(
    registry: StoreRegistry,
) -> None:
    scorer = AssociationScorer(
        AssociationConfig(), ItemTrackingConfig(), registry, vision_enabled=False
    )
    item = ItemTrackState(epc=EPC(value=EPC_SHIRT_A), gtin=None, home_fixture_id=FIXTURE_F1)
    item.position = WorldCoordinate(x=3.8, y=6.5, z=0.9)
    item.rest_position = WorldCoordinate(x=3.8, y=6.5, z=0.9)
    person = PersonState(
        track_id="P0001",
        created_at=at(0.0),
        updated_at=at(0.0),
        position=WorldCoordinate(x=3.8, y=5.4, z=1.0),
        velocity=Velocity(),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        confidence=0.9,
    )
    pair = PairState()
    scorer.score(item, person, pair, at(1.0), [], {})
    scorer.score(item, person, pair, at(1.0), [], {})
    assert len(pair.co_motion) == 1
    assert len(pair.distance_history) == 1


# ---------------------------------------------------------------------------------
# FIX 6 - a lost-at-the-door cart with an exit line and a newer line must still close.


def test_close_carts_with_exit_candidates_closes_despite_a_newer_line() -> None:
    cart = InMemoryCartEngine()
    epc_a = EPC(value=EPC_SHIRT_A)
    epc_b = EPC(value=EPC_SHIRT_B)
    cart.apply(
        RetailEvent(
            event_id=make_event_id(RetailEventType.PICK, epc_a, at(1.0), "P0001"),
            event_type=RetailEventType.PICK,
            timestamp=at(1.0),
            epc=epc_a,
            shopper_track_id="P0001",
            confidence=0.9,
        )
    )
    cart.apply(
        RetailEvent(
            event_id=make_event_id(RetailEventType.EXIT_WITH_ITEM, epc_a, at(5.0), "P0001"),
            event_type=RetailEventType.EXIT_WITH_ITEM,
            timestamp=at(5.0),
            epc=epc_a,
            shopper_track_id="P0001",
            confidence=0.9,
        )
    )
    cart.apply(
        RetailEvent(
            event_id=make_event_id(RetailEventType.PICK, epc_b, at(10.0), "P0001"),
            event_type=RetailEventType.PICK,
            timestamp=at(10.0),
            epc=epc_b,
            shopper_track_id="P0001",
            confidence=0.9,
        )
    )
    (open_cart,) = cart.state.carts.values()
    assert open_cart.status == CartStatus.OPEN

    cart.close_carts_with_exit_candidates(shopper_gone=lambda _: True)

    (closed_cart,) = cart.state.carts.values()
    assert closed_cart.status == CartStatus.EXITED
    assert closed_cart.exited_at == at(10.0)


# ---------------------------------------------------------------------------------
# B1 - only vision relevant to THIS item counts as new evidence (not the global counter).


def test_vision_far_from_this_item_does_not_count_as_new_evidence(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry, vision_enabled=True)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 3.8, 5.6, native="T1"))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.INTERACTION_CANDIDATE
    item.movement_start_at = at(0.5)
    engine.step(at(1.05))
    before = engine.ledger.score_of(epc, "P0001")

    engine.ingest(
        VisionEvidence(
            observation_id="vision:far",
            sensor_id="cam-main",
            timestamp=at(1.1),
            confidence=0.9,
            kind=VisionEvidenceKind.REACH_INTO_FIXTURE,
            coordinate=WorldCoordinate(x=10.0, y=1.0, z=1.2),
            uncertainty=SpatialUncertainty.isotropic(0.2),
        )
    )
    engine.step(at(1.15))  # no new item/person sample, vision is nowhere near this item
    after_far = engine.ledger.score_of(epc, "P0001")
    assert after_far == before

    engine.ingest(
        VisionEvidence(
            observation_id="vision:near",
            sensor_id="cam-main",
            timestamp=at(1.2),
            confidence=0.9,
            kind=VisionEvidenceKind.REACH_INTO_FIXTURE,
            coordinate=WorldCoordinate(x=3.8, y=6.5, z=1.2),
            uncertainty=SpatialUncertainty.isotropic(0.2),
        )
    )
    engine.step(at(1.25))  # relevant: at this item's rest position, within the window
    after_near = engine.ledger.score_of(epc, "P0001")
    assert after_near != after_far


# ---------------------------------------------------------------------------------
# B2 - observations stamped with another scenario's id must not leak into intake.


def test_foreign_scenario_observations_are_rejected_at_intake(registry: StoreRegistry) -> None:
    recorder = InMemoryRecorder()
    pipeline = FoundationPipeline(registry, scenario_id="01", recorder=recorder)
    foreign = _item(0.0, 3.8, 6.5).model_copy(
        update={"scenario_id": "02", "observation_id": "foreign:1"}
    )
    assert pipeline.ingest(foreign) is False
    assert pipeline.observations_rejected_foreign_scenario == 1
    assert recorder.entries == []  # nothing recorded for a rejected observation

    unstamped = _item(0.1, 3.8, 6.5).model_copy(
        update={"scenario_id": None, "observation_id": "unstamped:1"}
    )
    assert pipeline.ingest(unstamped) is True
    result = pipeline.finish()
    assert result.observations_rejected_foreign_scenario == 1
    assert result.observations_accepted == 1


# ---------------------------------------------------------------------------------
# B3 - a re-entered shopper's new cart must not be closed by their OLD session's sweep.


def test_a_reentered_shoppers_new_cart_is_not_closed_by_a_stale_session_sweep(
    registry: StoreRegistry,
) -> None:
    pipeline = FoundationPipeline(registry)
    epc_a = EPC(value=EPC_SHIRT_A)
    epc_b = EPC(value=EPC_SHIRT_B)

    for i in range(10):  # shopper on the sales floor: first session S-P0001 opens
        pipeline.fusion.ingest(_person(i * 0.1, 6.0, 4.0, native="T1"))
    pipeline.fusion.step(at(1.0))

    pick_a = RetailEvent(
        event_id=make_event_id(RetailEventType.PICK, epc_a, at(0.5), "P0001"),
        event_type=RetailEventType.PICK,
        timestamp=at(0.5),
        epc=epc_a,
        shopper_track_id="P0001",
        confidence=0.9,
    )
    pipeline.cart.apply(pick_a)
    pipeline._track_cart_session(pick_a)
    assert pipeline._cart_session["P0001"] == "S-P0001"

    for i in range(10, 20):  # walks gradually into the exit boundary: S-P0001 EXITED
        x = 6.0 + (11.25 - 6.0) * (i - 9) / 10
        pipeline.fusion.ingest(_person(i * 0.1, x, 4.0, native="T1"))
        pipeline.fusion.step(at(i * 0.1 + 0.05))
    assert pipeline.fusion.sessions["P0001"].state == SessionState.EXITED
    pipeline._close_carts_of_ended_sessions(at(2.0))
    assert pipeline.cart.state.carts["P0001"].status == CartStatus.EXITED

    for i in range(20, 32):  # walks back onto the sales floor: a fresh session opens
        x = 11.25 - (11.25 - 6.0) * (i - 19) / 12
        pipeline.fusion.ingest(_person(i * 0.1, x, 4.0, native="T1"))
        pipeline.fusion.step(at(i * 0.1 + 0.05))
    assert pipeline.fusion.sessions["P0001"].session_id == "S-P0001-2"
    assert pipeline.fusion.sessions["P0001"].state == SessionState.ACTIVE

    pick_b = RetailEvent(
        event_id=make_event_id(RetailEventType.PICK, epc_b, at(3.5), "P0001"),
        event_type=RetailEventType.PICK,
        timestamp=at(3.5),
        epc=epc_b,
        shopper_track_id="P0001",
        confidence=0.9,
    )
    pipeline.cart.apply(pick_b)
    pipeline._track_cart_session(pick_b)
    assert "P0001#2" in pipeline.cart.state.carts
    assert pipeline._cart_session["P0001#2"] == "S-P0001-2"

    # Sweep again (as a normal fusion step would): the OLD, now-superseded S-P0001
    # session is still EXITED in session_history, but must never touch P0001#2.
    for _ in range(3):
        pipeline._close_carts_of_ended_sessions(at(3.6))

    assert pipeline.cart.state.carts["P0001#2"].status == CartStatus.OPEN
    assert set(pipeline.cart.state.carts["P0001#2"].lines) == {epc_b.value}
    assert pipeline.cart.state.carts["P0001"].status == CartStatus.EXITED
    assert set(pipeline.cart.state.carts["P0001"].lines) == {epc_a.value}


def test_a_reentered_shoppers_cart_is_not_closed_by_an_emptyhanded_old_session(
    registry: StoreRegistry,
) -> None:
    """An archived session that never had a cart must not fall back to closing the
    shopper's CURRENT cart just because that cart is unmapped-to-IT; it may only close a
    cart that has no session mapping at all."""
    pipeline = FoundationPipeline(registry)
    epc = EPC(value=EPC_SHIRT_A)

    for i in range(10):  # shopper enters and stands on the sales floor: S-P0001 opens
        pipeline.fusion.ingest(_person(i * 0.1, 6.0, 4.0, native="T1"))
    pipeline.fusion.step(at(1.0))
    assert pipeline.fusion.sessions["P0001"].session_id == "S-P0001"

    for i in range(10, 20):  # walks into the exit boundary empty-handed: S-P0001 EXITED
        x = 6.0 + (11.25 - 6.0) * (i - 9) / 10
        pipeline.fusion.ingest(_person(i * 0.1, x, 4.0, native="T1"))
        pipeline.fusion.step(at(i * 0.1 + 0.05))
    assert pipeline.fusion.sessions["P0001"].state == SessionState.EXITED
    pipeline._close_carts_of_ended_sessions(at(2.0))
    assert "P0001" not in pipeline.cart.state.carts  # never had a cart to begin with

    for i in range(20, 32):  # walks back onto the sales floor: a fresh session opens
        x = 11.25 - (11.25 - 6.0) * (i - 19) / 12
        pipeline.fusion.ingest(_person(i * 0.1, x, 4.0, native="T1"))
        pipeline.fusion.step(at(i * 0.1 + 0.05))
    assert pipeline.fusion.sessions["P0001"].session_id == "S-P0001-2"
    assert pipeline.fusion.sessions["P0001"].state == SessionState.ACTIVE

    # Commit a PICK through the normal (_decide()) bookkeeping path.
    pick = RetailEvent(
        event_id=make_event_id(RetailEventType.PICK, epc, at(3.5), "P0001"),
        event_type=RetailEventType.PICK,
        timestamp=at(3.5),
        epc=epc,
        shopper_track_id="P0001",
        confidence=0.9,
    )
    pipeline.cart.apply(pick)
    pipeline._track_cart_session(pick)
    assert "P0001" in pipeline.cart.state.carts  # first lifecycle: no earlier cart existed
    assert pipeline._cart_session["P0001"] == "S-P0001-2"

    # Sweep again (as normal fusion steps would): the archived, empty-handed S-P0001
    # session must never fall back to closing the shopper's current (mapped) cart.
    for _ in range(2):
        pipeline._step(at(3.6))

    assert pipeline.cart.state.carts["P0001"].status == CartStatus.OPEN
    assert pipeline._cart_session["P0001"] == "S-P0001-2"


# ---------------------------------------------------------------------------------
# B4 - a localized RFID read takes its zone from the world-frame estimate, not the
# reading antenna's own pose (only a zone-only read falls back to the antenna's zone).


def test_localized_read_takes_its_zone_from_the_estimate_not_the_antenna(
    registry: StoreRegistry,
) -> None:
    # rfid-exit's own antenna sits in the exit zone, but this read's estimate maps to a
    # point inside zone-f1: the resulting zone must be zone-f1, not the antenna's exit.
    estimate = registry.transform("rfid-exit").to_sensor(WorldCoordinate(x=4.0, y=6.5, z=0.9))
    read = NativeRfidRead(
        sensor_id="rfid-exit",
        sequence=0,
        timestamp=at(0),
        epc_hex=EPC_SHIRT_A,
        antenna_port="5",
        rssi_dbm=-55.0,
        confidence=0.8,
        estimate=estimate,
        estimate_sigma_m=0.5,
    )
    obs = ObservationNormalizer(registry).item(read)
    assert obs.coordinate is not None
    assert obs.zone_id == "zone-f1"

    zone_only = NativeRfidRead(
        sensor_id="rfid-exit",
        sequence=1,
        timestamp=at(0.1),
        epc_hex=EPC_SHIRT_A,
        antenna_port="5",
        rssi_dbm=-60.0,
        confidence=0.6,
    )
    obs_zone_only = ObservationNormalizer(registry).item(zone_only)
    assert obs_zone_only.coordinate is None
    assert obs_zone_only.zone_id == "exit"


# ---------------------------------------------------------------------------------
# B5 - PersonTrackingConfig.prediction_horizon_s must not be negative.


def test_negative_prediction_horizon_is_rejected() -> None:
    with pytest.raises(ValidationError):
        PersonTrackingConfig(prediction_horizon_s=-1.0)


# ---------------------------------------------------------------------------------
# B6 - `radiowave.cli` replay streams a recording instead of reading it twice.


def test_cli_replay_reads_a_jsonl_recording_exactly_once(tmp_path, monkeypatch) -> None:
    from radiowave.cli import main
    from radiowave.replay.reader import JsonlReplaySource
    from radiowave.replay.recorder import JsonlRecorder
    from radiowave.simulator.library import load_scenario
    from radiowave.simulator.runner import run_scenario

    scenario = load_scenario("01")
    path = tmp_path / "scenario-01.jsonl"
    recorder = JsonlRecorder(path)
    run_scenario(scenario, recorder=recorder)
    recorder.close()

    calls = {"n": 0}
    original_entries = JsonlReplaySource.entries

    def counting_entries(self):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        yield from original_entries(self)

    monkeypatch.setattr(JsonlReplaySource, "entries", counting_entries)
    assert main(["replay", str(path)]) == 0
    assert calls["n"] == 1


def test_cli_replay_rejects_when_every_observation_is_a_foreign_scenario(tmp_path) -> None:
    from radiowave.cli import main
    from radiowave.replay.recorder import JsonlRecorder
    from radiowave.simulator.library import load_scenario
    from radiowave.simulator.runner import run_scenario

    scenario = load_scenario("01")
    path = tmp_path / "scenario-01.jsonl"
    recorder = JsonlRecorder(path)
    run_scenario(scenario, recorder=recorder)  # observations carry scenario_id "01"
    recorder.close()

    # --scenario 02 makes the pipeline's own scenario_id "02": every recorded observation
    # is rejected as foreign, so this must exit 1 just like the unknown-sensor case.
    assert main(["replay", str(path), "--scenario", "02"]) == 1
