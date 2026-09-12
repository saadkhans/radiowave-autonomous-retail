"""Regressions for the second Codex review round (evidence freshness, frames, sessions)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from radiowave.adapters.rfid.base import NativeRfidRead
from radiowave.contracts import (
    EPC,
    NATIVE_TRACK_KEY,
    ItemObservation,
    ItemState,
    PersonObservation,
    SensorCoordinate,
    SessionState,
    SpatialUncertainty,
    Velocity,
    VisionEvidence,
    VisionEvidenceKind,
    WorldCoordinate,
)
from radiowave.contracts.recording import EntryKind
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.association import AssociationScorer, CandidateLedger
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import AssociationConfig, ItemTrackingConfig, StateMachineConfig
from radiowave.fusion.state_machine import ItemStateMachine
from radiowave.fusion.tracking import ItemTrackManager
from radiowave.pipeline import FoundationPipeline
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import build_pipeline, scenario_observations
from radiowave.simulator.stores import EPC_SHIRT_A, FIXTURE_F1
from tests.conftest import at

ITEM_CFG = ItemTrackingConfig(smoothing_alpha=1.0, rest_init_reads=2, stale_after_s=1.5)


def _item(t: float, x: float | None, y: float | None) -> ItemObservation:
    coordinate = WorldCoordinate(x=x, y=y, z=0.9) if x is not None and y is not None else None
    return ItemObservation(
        observation_id=f"rfid:rfid-f1:{t:.3f}",
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        zone_id="zone-f1",
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


def _candidate_item(registry: StoreRegistry) -> tuple[ItemStateMachine, ItemTrackManager]:
    machine = ItemStateMachine(
        StateMachineConfig(
            movement_threshold_m=0.6,
            movement_confirm_reads=2,
            carry_displacement_m=1.5,
            carry_min_duration_s=1.0,
        ),
        ITEM_CFG,
        registry,
    )
    manager = ItemTrackManager(ITEM_CFG)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 4.0, 6.5))
    track.state = track.rest_state = ItemState.ON_FIXTURE
    for i in range(3, 6):  # 2.5 m away on three fresh reads -> INTERACTION_CANDIDATE
        manager.ingest(_item(i * 0.1, 6.5, 6.5))
        machine.evaluate(track, at(i * 0.1), None, None)
    assert track.state == ItemState.INTERACTION_CANDIDATE
    return machine, manager


def test_carry_promotion_needs_a_fresh_localized_read(registry: StoreRegistry) -> None:
    machine, manager = _candidate_item(registry)
    track = manager.get(EPC(value=EPC_SHIRT_A))
    assert track is not None
    for step in range(1, 6):  # radar-only steps inside the stale window: no promotion
        assert machine.evaluate(track, at(0.5 + 0.25 * step), None, None) is None
    assert track.state == ItemState.INTERACTION_CANDIDATE
    manager.ingest(_item(1.8, 6.5, 6.5))  # one fresh read after carry_min_duration_s
    transition = machine.evaluate(track, at(1.8), None, None)
    assert transition is not None and transition.to_state == ItemState.CARRIED


def test_zone_only_reads_do_not_refresh_location_evidence(registry: StoreRegistry) -> None:
    machine, manager = _candidate_item(registry)
    track = manager.get(EPC(value=EPC_SHIRT_A))
    assert track is not None
    for step in range(1, 12):  # coordinate-less reads for 2.75 s: identity only
        manager.ingest(_item(0.5 + 0.25 * step, None, None))
        assert machine.evaluate(track, at(0.5 + 0.25 * step), None, None) is None
    assert track.is_stale(at(3.25), stale_after_s=1.5)
    assert track.observation_count > track.localized_count
    assert track.state == ItemState.INTERACTION_CANDIDATE


def test_abandoned_interaction_clears_candidate_ledger(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 3.8, 5.4))
    engine.step(at(1.0))
    for i in range(10, 16):  # item drifts 1.4 m for a while, shopper is next to it
        engine.ingest(_item(i * 0.1, 5.2, 6.5))
        engine.ingest(_person(i * 0.1, 3.8, 5.4))
        engine.step(at(i * 0.1 + 0.05))
    item = engine.items.get(epc)
    assert item is not None and item.state == ItemState.INTERACTION_CANDIDATE
    assert engine.ledger.persons(epc) == ["P0001"]
    for i in range(16, 26):  # back within threshold -> jitter revert
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 3.8, 5.4))
        engine.step(at(i * 0.1 + 0.05))
    assert item.state == ItemState.ON_FIXTURE
    assert engine.ledger.persons(epc) == []


def test_world_coordinate_rejects_foreign_frames() -> None:
    with pytest.raises(ValidationError, match="store"):
        WorldCoordinate(x=1.0, y=2.0, frame_id="radar-north")
    assert WorldCoordinate(x=1.0, y=2.0).frame_id == "store"


def test_low_confidence_vision_evidence_is_ignored(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry, vision_enabled=True)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_person(i * 0.1, 3.8, 5.4))
    epc = EPC(value=EPC_SHIRT_A)
    item = engine.items.get(epc)
    assert item is not None
    item.movement_start_at = at(0.5)
    person = engine.persons.get("P0001")
    assert person is not None
    scorer = AssociationScorer(AssociationConfig(), ITEM_CFG, registry, vision_enabled=True)
    pair = CandidateLedger(AssociationConfig()).pair(epc, "P0001")

    def evidence(confidence: float) -> VisionEvidence:
        return VisionEvidence(
            observation_id=f"v{confidence}",
            sensor_id="cam-main",
            timestamp=at(0.5),
            confidence=confidence,
            kind=VisionEvidenceKind.REACH_INTO_FIXTURE,
            coordinate=WorldCoordinate(x=3.8, y=5.4),
            uncertainty=SpatialUncertainty.isotropic(0.3),
        )

    weak = scorer.score(item, person, pair, at(1.0), [evidence(0.2)], {"v0.2": "P0001"})
    strong = scorer.score(item, person, pair, at(1.0), [evidence(0.9)], {"v0.9": "P0001"})
    assert weak.vision_score == 0.5  # ignored, neutral
    assert strong.vision_score == 1.0


def test_rfid_estimate_requires_accuracy() -> None:
    with pytest.raises(ValidationError, match="accuracy"):
        NativeRfidRead(
            sensor_id="rfid-f1",
            sequence=0,
            timestamp=at(0),
            epc_hex=EPC_SHIRT_A,
            antenna_port="1",
            rssi_dbm=-50.0,
            confidence=0.8,
            estimate=SensorCoordinate(x=0.1, y=0.1, z=1.0, frame_id="rfid-f1"),
        )


def test_out_of_order_observations_are_dropped_and_counted(registry: StoreRegistry) -> None:
    pipeline = FoundationPipeline(registry)
    assert pipeline.ingest(_person(1.0, 2.0, 4.0)) is True
    assert pipeline.ingest(_person(0.5, 2.0, 4.0)) is False
    result = pipeline.finish()
    assert result.observations_out_of_order == 1
    assert result.observations_accepted == 1


def test_recording_starts_with_the_store_twin() -> None:
    scenario = load_scenario("01")

    class Capture:
        def __init__(self) -> None:
            self.entries: list[object] = []

        def record(self, entry: object) -> None:
            self.entries.append(entry)

        def close(self) -> None:
            return None

    recorder = Capture()
    build_pipeline(scenario, recorder=recorder).run(scenario_observations(scenario)[:5])
    first = recorder.entries[0]
    assert first.kind == EntryKind.STORE_TWIN
    assert first.payload["store_id"] == scenario.store.store_id


def test_exited_session_gets_no_attribution_and_reentry_opens_a_new_session(
    registry: StoreRegistry,
) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):  # shopper stands inside the exit boundary, item is "carried" nearby
        engine.ingest(_person(i * 0.1, 11.2, 4.0))
        engine.ingest(_item(i * 0.1, 11.4, 4.0))
    engine.step(at(1.0))
    assert engine.sessions["P0001"].state == SessionState.EXITED
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.CARRIED
    item.movement_start_at = at(0.5)
    engine.ingest(_item(1.1, 11.4, 4.0))
    events = engine.step(at(1.2))
    assert engine.ledger.persons(epc) == []
    assert all(e.shopper_track_id is None for e in events)
    for i in range(12, 24):  # walks back into the store: a fresh session opens
        engine.ingest(_person(i * 0.1, 11.2 - 0.2 * (i - 11), 4.0))
        engine.step(at(i * 0.1 + 0.05))
    assert engine.sessions["P0001"].state == SessionState.ACTIVE
    assert engine.sessions["P0001"].session_id == "S-P0001-2"
    assert [s.session_id for s in engine.session_history] == ["S-P0001"]
