"""Regressions for the fourth Codex review round (replay config, cart closure, read confidence)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from radiowave.cart.models import CartStatus
from radiowave.contracts import (
    EPC,
    NATIVE_TRACK_KEY,
    ConfidenceThresholds,
    CoordinateFrame,
    ItemObservation,
    ItemState,
    PersonObservation,
    RetailEvent,
    RetailEventType,
    SpatialUncertainty,
    Velocity,
    WorldCoordinate,
    make_event_id,
)
from radiowave.contracts.recording import EntryKind
from radiowave.contracts.tracks import AssociationEvidence
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import FusionConfig, ItemTrackingConfig, PersonTrackingConfig
from radiowave.fusion.state_machine import ItemStateMachine
from radiowave.fusion.tracking import ItemTrackManager, PersonTrackManager
from radiowave.pipeline import FoundationPipeline, PipelineConfig
from radiowave.replay.recorder import InMemoryRecorder
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import build_pipeline, run_scenario, scenario_observations
from radiowave.simulator.scenario import Dropout, ItemPlacement, Scenario
from radiowave.simulator.stores import EPC_SHIRT_A, EPC_SHIRT_B, FIXTURE_F1
from tests.conftest import at

ITEM_CFG = ItemTrackingConfig(smoothing_alpha=1.0, rest_init_reads=2, stale_after_s=1.5)


def _item(
    t: float, x: float, y: float, confidence: float = 0.8, zone: str = "zone-f1"
) -> ItemObservation:
    return ItemObservation(
        observation_id=f"rfid:{zone}:{t:.3f}:{confidence}",
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=confidence,
        epc=EPC(value=EPC_SHIRT_A),
        zone_id=zone,
        coordinate=WorldCoordinate(x=x, y=y, z=0.9),
        uncertainty=SpatialUncertainty.isotropic(0.5),
    )


def test_recording_carries_the_pipeline_config_and_replays_with_it() -> None:
    scenario = load_scenario("01")
    config = PipelineConfig(thresholds=ConfidenceThresholds(commit_min_confidence=0.6))
    recorder = InMemoryRecorder()
    build_pipeline(scenario, config, recorder).run(scenario_observations(scenario))
    header = [e.kind for e in recorder.entries[:2]]
    assert header == [EntryKind.STORE_TWIN, EntryKind.PIPELINE_CONFIG]
    recorded = PipelineConfig.model_validate(recorder.entries[1].payload)
    assert recorded == config
    replayed = build_pipeline(scenario, recorded).run(scenario_observations(scenario))
    live = build_pipeline(scenario, config).run(scenario_observations(scenario))
    assert [e.event_id for e in replayed.committed_events] == [
        e.event_id for e in live.committed_events
    ]
    assert replayed.cart_state == live.cart_state


def test_header_is_written_even_without_observations() -> None:
    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    build_pipeline(scenario, recorder=recorder).finish()
    kinds = [e.kind for e in recorder.entries]
    assert kinds[:2] == [EntryKind.STORE_TWIN, EntryKind.PIPELINE_CONFIG]
    assert EntryKind.GROUND_TRUTH in kinds
    timestamps = [e.timestamp for e in recorder.entries]
    assert timestamps == sorted(timestamps)


def test_item_exit_freezes_the_line_but_only_a_session_exit_closes_the_cart() -> None:
    scenario = load_scenario("04")
    result = run_scenario(scenario)
    (cart,) = result.cart_state.carts.values()
    assert cart.status == CartStatus.EXITED  # the shopper's session exited
    assert cart.lines[EPC_SHIRT_A].final_ownership_candidate is True
    session = next(s for s in result.sessions if s.person_track_id == cart.shopper_track_id)
    assert session.state.value == "EXITED"
    assert cart.exited_at == session.exited_at  # the session end stamps the cart
    exit_event = next(
        e for e in result.committed_events if e.event_type == RetailEventType.EXIT_WITH_ITEM
    )
    assert cart.lines[EPC_SHIRT_A].exit_event_at == exit_event.timestamp


def test_low_confidence_reads_never_touch_physical_state(registry: StoreRegistry) -> None:
    manager = ItemTrackManager(ITEM_CFG)
    machine = ItemStateMachine(FusionConfig().state_machine, ITEM_CFG, registry)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 4.0, 6.5))
    track.state = track.rest_state = ItemState.ON_FIXTURE
    for i in range(3, 10):  # zero-confidence displaced reads
        manager.ingest(_item(i * 0.1, 8.0, 6.5, confidence=0.0))
        assert machine.evaluate(track, at(i * 0.1), None, None) is None
    assert track.state == ItemState.ON_FIXTURE
    assert track.position is not None and track.position.x == 4.0
    assert manager.rejected_low_confidence == 7
    track.state = ItemState.CARRIED
    for i in range(10, 14):  # zero-confidence exit-zone reads do not close the episode
        manager.ingest(_item(i * 0.1, 11.2, 4.0, confidence=0.0, zone="exit"))
    assert machine.evaluate(track, at(1.4), None, None) is None
    assert track.state == ItemState.CARRIED


def test_low_confidence_person_observations_are_ignored() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    weak = PersonObservation(
        observation_id="w",
        sensor_id="radar-north",
        timestamp=at(0),
        confidence=0.05,
        coordinate=WorldCoordinate(x=1.0, y=1.0, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(),
        metadata={NATIVE_TRACK_KEY: "T1"},
    )
    assert manager.ingest(weak) is None
    assert manager.all == [] and manager.rejected_low_confidence == 1


def test_handoff_proposal_is_inactive_once_the_receiver_changes(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.CARRIED
    item.carrier_track_id = "P0001"

    def evidence(score: float) -> AssociationEvidence:
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

    for _ in range(10):  # receiver P0002 leads the ledger
        engine.ledger.update(epc, "P0002", evidence(0.8), at(1.0))
        engine.ledger.update(epc, "P0001", evidence(0.2), at(1.0))
    proposal = RetailEvent(
        event_id=make_event_id(RetailEventType.HANDOFF, epc, at(0.5), "P0002"),
        event_type=RetailEventType.HANDOFF,
        timestamp=at(1.0),
        epc=epc,
        shopper_track_id="P0001",
        counterpart_track_id="P0002",
        confidence=0.6,
    )
    assert engine.is_active(proposal)
    for _ in range(10):  # another shopper takes the lead
        engine.ledger.update(epc, "P0003", evidence(0.95), at(1.2))
    assert not engine.is_active(proposal)
    engine.ledger.drop(epc, "P0003")
    engine.ledger.drop(epc, "P0002")  # the receiver left the store
    assert not engine.is_active(proposal)


def test_unknown_zone_ids_are_not_exit_evidence(registry: StoreRegistry) -> None:
    manager = ItemTrackManager(ITEM_CFG)
    machine = ItemStateMachine(FusionConfig().state_machine, ITEM_CFG, registry)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 8.0, 4.0))
    track.state = ItemState.CARRIED
    track.rest_position = WorldCoordinate(x=3.8, y=6.5)
    for i in range(3, 7):
        manager.ingest(_item(i * 0.1, 8.0, 4.0, zone="vendor-cell-42"))
    assert machine.evaluate(track, at(0.7), None, None) is None  # no KeyError, no exit
    assert track.state == ItemState.CARRIED


def test_reversed_dropouts_and_duplicate_placements_are_rejected() -> None:
    with pytest.raises(ValidationError, match="before it starts"):
        Dropout(start_t=5.0, end_t=2.0)
    with pytest.raises(ValidationError):
        Dropout(start_t=-1.0, end_t=2.0)
    scenario = load_scenario("01")
    doubled = [p.model_dump() for p in scenario.placements] + [
        ItemPlacement(epc=EPC_SHIRT_A, x=9.0, y=1.0).model_dump()
    ]
    with pytest.raises(ValidationError, match="placed once"):
        Scenario.model_validate({**scenario.model_dump(), "placements": doubled})
    assert EPC_SHIRT_B in {p.epc for p in scenario.placements}


def test_store_frame_units_are_meters_only() -> None:
    with pytest.raises(ValidationError):
        CoordinateFrame(units="ft")
    assert CoordinateFrame().units == "m"


def test_replay_without_recorded_config_is_refused(tmp_path) -> None:
    from radiowave.cli import main

    scenario = load_scenario("01")
    recorder = InMemoryRecorder()
    build_pipeline(scenario, recorder=recorder).run(scenario_observations(scenario)[:20])
    path = tmp_path / "rec.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for entry in recorder.entries:
            if entry.kind != EntryKind.PIPELINE_CONFIG:
                handle.write(entry.model_dump_json() + "\n")
    with pytest.raises(SystemExit, match="PIPELINE_CONFIG"):
        main(["replay", str(path)])
    config_path = tmp_path / "config.json"
    config_path.write_text(PipelineConfig().model_dump_json(), encoding="utf-8")
    assert main(["replay", str(path), "--config", str(config_path)]) == 0


def test_pipeline_result_is_unaffected_by_header_entries() -> None:
    scenario = load_scenario("01")
    observations = scenario_observations(scenario)
    plain = build_pipeline(scenario).run(observations)
    recorded = build_pipeline(scenario, recorder=InMemoryRecorder()).run(observations)
    assert plain.model_dump() == recorded.model_dump()
    assert FoundationPipeline(StoreRegistry(scenario.store)).finish().steps == 0


def test_lost_track_at_the_door_still_freezes_the_cart() -> None:
    scenario = load_scenario("04").model_copy(
        update={"radar_dropouts": [Dropout(start_t=10.0, end_t=18.0)]}
    )
    result = run_scenario(scenario)
    assert [e.event_type for e in result.committed_events][-1] == RetailEventType.EXIT_WITH_ITEM
    (cart,) = result.cart_state.carts.values()
    assert cart.status == CartStatus.EXITED and cart.exited_at is not None


def test_exit_line_after_a_closed_cart_opens_a_new_lifecycle() -> None:
    from radiowave.cart.engine import InMemoryCartEngine

    engine = InMemoryCartEngine()
    engine.apply(
        RetailEvent(
            event_id=make_event_id(RetailEventType.PICK, EPC(value=EPC_SHIRT_A), at(1), "P0001"),
            event_type=RetailEventType.PICK,
            timestamp=at(1),
            epc=EPC(value=EPC_SHIRT_A),
            shopper_track_id="P0001",
            confidence=0.9,
        )
    )
    engine.close_cart("P0001", at(5))
    engine.apply(
        RetailEvent(
            event_id=make_event_id(
                RetailEventType.EXIT_WITH_ITEM, EPC(value=EPC_SHIRT_B), at(30), "P0001"
            ),
            event_type=RetailEventType.EXIT_WITH_ITEM,
            timestamp=at(30),
            epc=EPC(value=EPC_SHIRT_B),
            shopper_track_id="P0001",
            confidence=0.9,
        )
    )
    assert engine.state.carts["P0001"].epcs == {EPC_SHIRT_A}
    assert engine.state.carts["P0001#2"].epcs == {EPC_SHIRT_B}
    engine.close_carts_with_exit_candidates(shopper_gone=lambda _: True)
    assert engine.state.carts["P0001#2"].status == CartStatus.EXITED
    assert engine.state.carts["P0001#2"].exited_at == at(30)


def test_rejected_read_never_creates_item_identity(registry: StoreRegistry) -> None:
    manager = ItemTrackManager(ITEM_CFG)
    junk = _item(0.0, 1.0, 1.0, confidence=0.0).model_copy(
        update={"epc": EPC(value="3034F1A0000000000000FFFF")}
    )
    assert manager.ingest(junk) is None
    assert manager.all == [] and manager.rejected_low_confidence == 1
