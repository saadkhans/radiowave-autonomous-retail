"""Regressions for the fifth Codex review round."""

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
    RetailEventType,
    SpatialUncertainty,
    Velocity,
    WorldCoordinate,
)
from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import FusionConfig, ItemTrackingConfig, StateMachineConfig
from radiowave.fusion.state_machine import ItemStateMachine
from radiowave.fusion.tracking import ItemTrackManager
from radiowave.pipeline import FoundationPipeline, PipelineConfig
from radiowave.replay.clock import SimulatedClock
from radiowave.replay.recorder import InMemoryRecorder
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import build_pipeline, run_scenario, scenario_observations
from radiowave.simulator.scenario import Carry, Dropout, GroundTruthEvent, Scenario
from radiowave.simulator.stores import EPC_SHIRT_A, EPC_SHIRT_B, FIXTURE_F1
from tests.conftest import T0, at

ITEM_CFG = ItemTrackingConfig(smoothing_alpha=1.0, rest_init_reads=2, stale_after_s=1.5)


def _item(t: float, x: float | None, y: float | None, zone: str = "zone-f1") -> ItemObservation:
    coordinate = WorldCoordinate(x=x, y=y, z=0.9) if x is not None and y is not None else None
    return ItemObservation(
        observation_id=f"rfid:rfid-f1:{t:.3f}:{zone}",
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        zone_id=zone,
        coordinate=coordinate,
        uncertainty=SpatialUncertainty.isotropic(0.5) if coordinate else None,
    )


def _person(t: float, x: float, y: float, sensor: str = "radar-north") -> PersonObservation:
    return PersonObservation(
        observation_id=f"mmwave:{sensor}:{t:.3f}",
        sensor_id=sensor,
        timestamp=at(t),
        confidence=0.9,
        coordinate=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(),
        metadata={NATIVE_TRACK_KEY: "T1"},
    )


def test_item_only_exit_never_closes_a_tracked_shoppers_cart_at_finish(
    registry: StoreRegistry,
) -> None:
    pipeline = FoundationPipeline(registry)
    engine = pipeline.fusion
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):  # shopper stays mid-store, tracked, while the item alone reaches the door
        pipeline.ingest(_person(i * 0.1, 6.0, 4.0))
        pipeline.ingest(_item(i * 0.1, 3.8, 6.5))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.CARRIED
    item.carrier_track_id = "P0001"
    from radiowave.contracts import RetailEvent, make_event_id

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
    result = pipeline.finish()
    (cart,) = result.cart_state.carts.values()
    assert cart.status == CartStatus.OPEN  # shopper is still tracked inside
    assert cart.lines[EPC_SHIRT_A].exit_event_at == at(0.9)


def test_lost_shopper_cart_is_stamped_with_the_exit_event_time() -> None:
    scenario = load_scenario("04").model_copy(
        update={"radar_dropouts": [Dropout(start_t=10.0, end_t=18.0)]}
    )
    result = run_scenario(scenario)
    exit_event = next(
        e for e in result.committed_events if e.event_type == RetailEventType.EXIT_WITH_ITEM
    )
    (cart,) = result.cart_state.carts.values()
    assert cart.status == CartStatus.EXITED
    assert cart.exited_at == exit_event.timestamp
    assert cart.exited_at > cart.lines[EPC_SHIRT_A].added_at


def test_candidate_scores_do_not_move_on_empty_steps(registry: StoreRegistry) -> None:
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
    first = engine.ledger.score_of(epc, "P0001")
    assert first > 0.0
    for step in range(1, 5):  # radar and RFID silent: elapsed ticks are not evidence
        engine.step(at(1.05 + 0.1 * step))
    assert engine.ledger.score_of(epc, "P0001") == first
    engine.ingest(_person(1.5, 3.8, 5.4))  # one new shopper sample moves it again
    engine.step(at(1.55))
    assert engine.ledger.score_of(epc, "P0001") != first


def test_vision_mode_is_recorded_and_restored() -> None:
    scenario = load_scenario("12v")
    recorder = InMemoryRecorder()
    build_pipeline(scenario, recorder=recorder).run(scenario_observations(scenario)[:50])
    header = next(e for e in recorder.entries if e.kind == EntryKind.PIPELINE_CONFIG)
    assert PipelineConfig.model_validate(header.payload).vision_enabled is True
    plain = load_scenario("01")
    recorder = InMemoryRecorder()
    build_pipeline(plain, recorder=recorder).run(scenario_observations(plain)[:50])
    header = next(e for e in recorder.entries if e.kind == EntryKind.PIPELINE_CONFIG)
    assert PipelineConfig.model_validate(header.payload).vision_enabled is False


def test_carrier_exit_needs_a_fresh_localized_read(registry: StoreRegistry) -> None:
    machine = ItemStateMachine(FusionConfig().state_machine, ITEM_CFG, registry)
    manager = ItemTrackManager(ITEM_CFG)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 8.0, 4.0))
    track.state = ItemState.CARRIED
    track.rest_position = WorldCoordinate(x=3.8, y=6.5)
    manager.ingest(_item(10.0, None, None, zone="zone-f2"))  # coarse read, position is 10 s old
    carrier_at_door = WorldCoordinate(x=11.2, y=4.0)
    assert machine.evaluate(track, at(10.1), carrier_at_door, None) is None
    assert track.state == ItemState.CARRIED


def test_expired_lost_tracks_do_not_block_rest(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(5):
        engine.ingest(_person(i * 0.1, 3.9, 6.4))
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
    engine.step(at(3.0))  # shopper silent for 2.6 s: LOST, beyond the 1 s horizon
    person = engine.persons.get("P0001")
    assert person is not None and person.state == PersonTrackState.LOST
    item = engine.items.get(epc)
    assert item is not None
    assert engine._nearest_person_m(item, at(3.0)) is None


def test_recording_envelope_must_agree_with_payload() -> None:
    observation = _item(1.0, 3.8, 6.5)
    entry = RecordedEntry.from_observation(0, observation)
    assert entry.to_observation() == observation
    tampered = entry.model_copy(update={"timestamp": at(5.0)})
    with pytest.raises(ValueError, match="envelope"):
        tampered.to_observation()


def test_config_and_clock_bounds() -> None:
    with pytest.raises(ValidationError):
        StateMachineConfig(physical_event_confidence=2.0)
    clock = SimulatedClock(T0)
    with pytest.raises(ValueError):
        clock.advance(-1.0)


def test_scenario_rejects_bad_dropouts_labels_truth_and_carries() -> None:
    base = load_scenario("01")
    data = base.model_dump()
    with pytest.raises(ValidationError, match="unknown sensor"):
        Scenario.model_validate(
            {**data, "radar_dropouts": [Dropout(start_t=1, end_t=2, sensor_id="nope").model_dump()]}
        )
    with pytest.raises(ValidationError, match="not a MMWAVE sensor"):
        Scenario.model_validate(
            {
                **data,
                "radar_dropouts": [Dropout(start_t=1, end_t=2, sensor_id="rfid-f1").model_dump()],
            }
        )
    with pytest.raises(ValidationError, match="labels must be unique"):
        Scenario.model_validate({**data, "shoppers": [base.shoppers[0].model_dump()] * 2})
    late_truth = GroundTruthEvent(t=99.0, event_type=RetailEventType.PICK, epc=EPC_SHIRT_A)
    with pytest.raises(ValidationError, match="after the scenario ends"):
        Scenario.model_validate({**data, "expected_events": [late_truth.model_dump()]})
    with pytest.raises(ValidationError):
        GroundTruthEvent(t=-1.0, event_type=RetailEventType.PICK, epc=EPC_SHIRT_A)
    outside = Carry(epc=EPC_SHIRT_B, carrier_label="A", start_t=0.0, end_t=17.0)
    data_short = {
        **data,
        "shoppers": [
            {
                "label": "A",
                "waypoints": [{"t": 2.0, "x": 1.0, "y": 4.0}, {"t": 10.0, "x": 4.0, "y": 5.3}],
            }
        ],
    }
    with pytest.raises(ValidationError, match="outside A's presence"):
        Scenario.model_validate({**data_short, "carries": [outside.model_dump()]})


def test_intake_rejects_unknown_or_mismatched_sensors(registry: StoreRegistry) -> None:
    pipeline = FoundationPipeline(registry)
    assert pipeline.ingest(_person(0.0, 2.0, 4.0, sensor="ghost-radar")) is False
    assert pipeline.ingest(_person(0.1, 2.0, 4.0, sensor="rfid-f1")) is False
    assert pipeline.ingest(_person(0.2, 2.0, 4.0)) is True
    result = pipeline.finish()
    assert result.observations_rejected_unknown_sensor == 2
    assert result.observations_accepted == 1
