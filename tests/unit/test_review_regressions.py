"""Regressions for review findings: each test pins one previously wrong behaviour."""

from __future__ import annotations

import pytest

from radiowave.contracts import (
    EPC,
    NATIVE_TRACK_KEY,
    ConfidenceThresholds,
    Decision,
    ItemObservation,
    ItemState,
    PersonObservation,
    PersonTrackState,
    RetailEventType,
    SpatialUncertainty,
    Velocity,
    VisionEvidence,
    VisionEvidenceKind,
    WorldCoordinate,
)
from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.association import assign_vision_to_nearest
from radiowave.fusion.config import (
    AssociationConfig,
    ItemTrackingConfig,
    PersonTrackingConfig,
    StateMachineConfig,
)
from radiowave.fusion.state_machine import ItemStateMachine
from radiowave.fusion.tracking import ItemTrackManager, PersonTrackManager
from radiowave.ingestion.deduplication import ObservationDeduplicator
from radiowave.pipeline import FoundationPipeline, PipelineConfig
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import build_pipeline, run_scenario, scenario_observations
from radiowave.simulator.stores import EPC_SHIRT_A, FIXTURE_F1
from tests.conftest import at


def _person(
    sensor: str, t: float, x: float, y: float, native: str | None = "T1"
) -> PersonObservation:
    metadata = {NATIVE_TRACK_KEY: native} if native is not None else {}
    return PersonObservation(
        observation_id=f"mmwave:{sensor}:{t:.2f}:{native}",
        sensor_id=sensor,
        timestamp=at(t),
        confidence=0.9,
        coordinate=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(),
        metadata=metadata,
    )


def _item(t: float, x: float, y: float) -> ItemObservation:
    return ItemObservation(
        observation_id=f"rfid:rfid-f1:{t:.2f}",
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        coordinate=WorldCoordinate(x=x, y=y, z=0.9),
        uncertainty=SpatialUncertainty.isotropic(0.5),
    )


def test_recycled_native_track_id_far_away_is_a_new_person() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    first = manager.ingest(_person("radar-north", 0.0, 2.0, 2.0, "T1"))
    manager.ingest(_person("radar-north", 0.1, 2.0, 2.0, "T1"))
    other = manager.ingest(_person("radar-north", 0.2, 9.0, 6.0, "T1"))  # vendor reused T1
    assert other.track_id != first.track_id
    assert manager.get(first.track_id) is not None
    assert manager.get(first.track_id).observation_count == 2  # type: ignore[union-attr]


def test_observations_without_native_hint_keep_one_track() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    ids = {
        manager.ingest(_person("radar-north", i * 0.1, 2.0 + i * 0.05, 2.0, None)).track_id
        for i in range(10)
    }
    assert ids == {"P0001"}
    assert manager.get("P0001").observation_count == 10  # type: ignore[union-attr]


def test_vision_evidence_is_not_assigned_to_tracks_created_later() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    manager.ingest(_person("radar-north", 5.0, 4.0, 5.4, "T1"))  # arrives after the evidence
    evidence = VisionEvidence(
        observation_id="v0",
        sensor_id="cam-main",
        timestamp=at(1.0),
        confidence=0.85,
        kind=VisionEvidenceKind.REACH_INTO_FIXTURE,
        coordinate=WorldCoordinate(x=4.0, y=5.4),
        uncertainty=SpatialUncertainty.isotropic(0.3),
    )
    assert assign_vision_to_nearest([evidence], manager.all, radius_m=3.0) == {}


def test_dedup_keeps_distinct_vision_kinds_at_same_place_and_time() -> None:
    dedup = ObservationDeduplicator()
    base = dict(
        sensor_id="cam-main",
        timestamp=at(1.0),
        confidence=0.85,
        coordinate=WorldCoordinate(x=4.0, y=5.4),
        uncertainty=SpatialUncertainty.isotropic(0.3),
    )
    reach = VisionEvidence(observation_id="a", kind=VisionEvidenceKind.REACH_INTO_FIXTURE, **base)
    in_hand = VisionEvidence(observation_id="b", kind=VisionEvidenceKind.ITEM_IN_HAND, **base)
    assert dedup.accept(reach) and dedup.accept(in_hand)
    assert not dedup.accept(reach.model_copy(update={"observation_id": "c"}))


def test_movement_confirmation_needs_fresh_reads(registry: StoreRegistry) -> None:
    item_cfg = ItemTrackingConfig(smoothing_alpha=1.0, rest_init_reads=2, stale_after_s=5.0)
    machine = ItemStateMachine(
        StateMachineConfig(movement_threshold_m=0.6, movement_confirm_reads=3), item_cfg, registry
    )
    manager = ItemTrackManager(item_cfg)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 4.0, 6.5))
    track.state = track.rest_state = ItemState.ON_FIXTURE
    manager.ingest(_item(0.3, 5.5, 6.5))  # one displaced read, then radar-only steps
    for step in range(1, 6):
        assert machine.evaluate(track, at(0.3 + 0.25 * step), None, None) is None
    assert track.state == ItemState.ON_FIXTURE
    for i in range(1, 3):  # two more genuine reads complete the confirmation
        manager.ingest(_item(0.3 + 0.1 * i, 5.5, 6.5))
        transition = machine.evaluate(track, at(0.3 + 0.1 * i), None, None)
    assert transition is not None and transition.to_state == ItemState.INTERACTION_CANDIDATE


def test_association_config_rejects_zero_distance_scale() -> None:
    with pytest.raises(ValueError):
        AssociationConfig(distance_scale_m=0.0)


def test_stale_items_are_not_scored_or_attributed() -> None:
    scenario = load_scenario("08")  # RFID dropout 9-12 s while carried
    result = run_scenario(scenario)
    gap_proposals = [e for e in result.proposed_events if at(10.6) <= e.timestamp <= at(12.0)]
    assert gap_proposals == []
    assert result.cart_state.epcs_in("P0001") == {EPC_SHIRT_A}


def test_one_shot_events_waiting_on_confidence_reach_review() -> None:
    scenario = load_scenario("02")  # PICK then PUTBACK at 0.9 physical confidence
    config = PipelineConfig(
        thresholds=ConfidenceThresholds(commit_min_confidence=0.95, max_wait_seconds=3.0)
    )
    result = run_scenario(scenario, pipeline_config=config)
    assert result.committed_events == []
    reviewed = {e.event_type for e in result.review_events}
    assert RetailEventType.PUTBACK in reviewed or RetailEventType.PICK in reviewed
    assert result.pending_events == []
    assert result.decisions_of(Decision.WAIT)


class _MinimalRecorder:
    """Implements only the Recorder protocol (record/close)."""

    def __init__(self) -> None:
        self.entries: list[RecordedEntry] = []
        self.closed = False

    def record(self, entry: RecordedEntry) -> None:
        self.entries.append(entry)

    def close(self) -> None:
        self.closed = True


def test_pipeline_uses_only_the_recorder_protocol_and_records_chronologically() -> None:
    scenario = load_scenario("02")
    recorder = _MinimalRecorder()
    pipeline = build_pipeline(scenario, recorder=recorder)
    pipeline.run(scenario_observations(scenario))
    assert recorder.entries, "nothing recorded"
    timestamps = [e.timestamp for e in recorder.entries]
    assert timestamps == sorted(timestamps)
    assert [e.sequence for e in recorder.entries] == list(range(len(recorder.entries)))
    truths = [e for e in recorder.entries if e.kind == EntryKind.GROUND_TRUTH]
    assert len(truths) == len(scenario.expected_events)
    first_truth_index = recorder.entries.index(truths[0])
    assert any(e.kind == EntryKind.OBSERVATION for e in recorder.entries[:first_truth_index])


def test_departed_shopper_state_is_ended_not_lost_forever() -> None:
    result = run_scenario(load_scenario("13"))
    states = {t.track_id: t.state for t in result.person_tracks}
    assert PersonTrackState.ENDED in states.values()
    pipeline = FoundationPipeline(StoreRegistry(load_scenario("13").store))
    assert pipeline.result().steps == 0
