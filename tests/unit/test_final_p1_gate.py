"""Final P1 gate: duplicates must never advance pipeline time, a localized zone must
agree with its coordinate, and reacquisition dead reckoning must be capped.
"""

from __future__ import annotations

from radiowave.contracts import (
    EPC,
    NATIVE_TRACK_KEY,
    ItemObservation,
    ItemState,
    PersonObservation,
    PersonTrackState,
    RetailEventType,
    SessionState,
    SpatialUncertainty,
    Velocity,
    WorldCoordinate,
)
from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.ingestion.deduplication import ObservationDeduplicator
from radiowave.pipeline import FoundationPipeline, PipelineConfig
from radiowave.replay.recorder import JsonlRecorder
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import build_pipeline, scenario_observations
from radiowave.simulator.stores import EPC_SHIRT_A, build_lab_store
from tests.conftest import at


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


# ---------------------------------------------------------------------------------
# P1 #1 - duplicates must not advance pipeline time.


def test_duplicate_with_later_timestamp_does_not_advance_fusion_time(
    registry: StoreRegistry,
) -> None:
    pipeline = FoundationPipeline(registry)
    first = PersonObservation(
        observation_id="obs-001",
        sensor_id="radar-north",
        timestamp=at(0.0),
        confidence=0.9,
        coordinate=WorldCoordinate(x=2.0, y=4.0, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(),
        metadata={NATIVE_TRACK_KEY: "T1"},
    )
    assert pipeline.ingest(first) is True
    steps_before = pipeline._steps
    track_id = pipeline.fusion.persons.all[0].track_id

    duplicate = first.model_copy(update={"timestamp": at(10.0)})
    assert pipeline.ingest(duplicate) is False
    assert pipeline._steps == steps_before
    track = pipeline.fusion.persons.get(track_id)
    assert track is not None and track.state == PersonTrackState.ACTIVE
    assert pipeline._last_timestamp == at(0.0)

    second = first.model_copy(
        update={
            "observation_id": "obs-002",
            "timestamp": at(1.0),
            "coordinate": WorldCoordinate(x=2.1, y=4.0, z=1.0),
        }
    )
    assert pipeline.ingest(second) is True
    result = pipeline.finish()
    assert result.observations_accepted == 2
    assert result.observations_dropped == 1
    assert result.observations_out_of_order == 0
    person_track = next(t for t in result.person_tracks if t.track_id == track_id)
    assert person_track.state != PersonTrackState.ENDED


def test_duplicate_cannot_expire_a_pending_wait_or_close_a_session() -> None:
    scenario = load_scenario("12")
    pipeline = build_pipeline(scenario)
    observations = [o for o in scenario_observations(scenario) if o.timestamp <= at(12.0)]
    last_accepted = None
    for observation in observations:
        if pipeline.ingest(observation):
            last_accepted = observation
    assert pipeline._pending, "expected a PICK proposal pending in WAIT by t=12s"
    assert last_accepted is not None

    pending_before = set(pipeline._pending)
    steps_before = pipeline._steps
    decisions_before = len(pipeline._decisions)
    sessions_before = {s.session_id: s.state for s in pipeline.fusion.sessions.values()}
    cart_before = pipeline.cart.state.model_dump()

    stale = last_accepted.model_copy(update={"timestamp": at(60.0)})
    assert pipeline.ingest(stale) is False

    assert set(pipeline._pending) == pending_before
    assert pipeline._steps == steps_before
    assert len(pipeline._decisions) == decisions_before
    assert {s.session_id: s.state for s in pipeline.fusion.sessions.values()} == sessions_before
    assert pipeline.cart.state.model_dump() == cart_before
    for session in sessions_before.values():
        assert session == SessionState.ACTIVE


def test_dedup_peek_does_not_mutate_and_out_of_order_new_input_is_not_counted_accepted(
    registry: StoreRegistry,
) -> None:
    dedup = ObservationDeduplicator()
    obs = _item(0.0, 4.0, 6.5)
    assert dedup.is_duplicate(obs) is False
    assert (dedup.accepted, dedup.dropped) == (0, 0)
    dedup.commit(obs)
    assert dedup.is_duplicate(obs) is True
    assert dedup.accepted == 1

    pipeline = FoundationPipeline(registry)
    newer = _item(5.0, 4.0, 6.5)
    assert pipeline.ingest(newer) is True
    older = _item(4.0, 4.0, 6.5)  # genuinely new content, but an older timestamp
    assert pipeline.ingest(older) is False
    assert pipeline.observations_out_of_order == 1
    assert pipeline.dedup.accepted == 1
    assert pipeline.dedup.dropped == 0

    assert pipeline.ingest(older) is False  # replayed again: still out-of-order, not duplicate
    assert pipeline.observations_out_of_order == 2
    assert pipeline.dedup.accepted == 1
    assert pipeline.dedup.dropped == 0


# ---------------------------------------------------------------------------------
# P1 #2 - a localized zone must agree with the coordinate.


def test_contradictory_zone_and_coordinate_are_rejected_before_any_mutation(
    registry: StoreRegistry,
) -> None:
    pipeline = FoundationPipeline(registry)
    obs = _item(0.0, 6.0, 4.0, sensor="rfid-floor", zone="exit")
    assert pipeline.ingest(obs) is False
    result = pipeline.finish()
    assert result.observations_rejected_spatially_inconsistent == 1
    assert result.observations_accepted == 0
    assert pipeline._steps == 0
    item_track = result.item(EPC_SHIRT_A)
    assert item_track.observation_count == 0
    assert item_track.last_seen_at is None


def test_three_contradictory_exit_reads_never_exit_a_carried_item(
    registry: StoreRegistry,
) -> None:
    pipeline = FoundationPipeline(registry)
    for i in range(6):
        assert pipeline.ingest(_item(i * 0.1, 3.8, 6.5)) is True
    item = pipeline.fusion.items.get(EPC(value=EPC_SHIRT_A))
    assert item is not None
    item.state = ItemState.CARRIED
    item.movement_start_at = at(0.3)

    for t in (1.0, 1.1, 1.2):
        obs = _item(t, 6.0, 4.0, sensor="rfid-floor", zone="exit")
        assert pipeline.ingest(obs) is False

    result = pipeline.finish()
    assert result.observations_rejected_spatially_inconsistent == 3
    assert not any(e.event_type == RetailEventType.EXIT_WITH_ITEM for e in result.proposed_events)
    assert not any(e.event_type == RetailEventType.EXIT_WITH_ITEM for e in result.committed_events)
    assert item.state == ItemState.CARRIED


def test_coordinate_inside_its_named_exit_zone_is_accepted(registry: StoreRegistry) -> None:
    pipeline = FoundationPipeline(registry)
    obs = _item(0.0, 11.2, 4.0, sensor="rfid-exit", zone="exit")
    assert pipeline.ingest(obs) is True


def test_coordinate_inside_an_enclosing_zone_is_accepted(registry: StoreRegistry) -> None:
    pipeline = FoundationPipeline(registry)
    obs = _item(0.0, 3.8, 6.5, zone="floor")  # inside zone-f1 and the enclosing SALES_FLOOR
    assert pipeline.ingest(obs) is True


def test_zone_only_portal_read_is_unaffected_by_the_spatial_check(registry: StoreRegistry) -> None:
    pipeline = FoundationPipeline(registry)
    obs = ItemObservation(
        observation_id="rfid:rfid-exit:zoneonly",
        sensor_id="rfid-exit",
        timestamp=at(0.0),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        zone_id="exit",
        coordinate=None,
        uncertainty=None,
    )
    assert pipeline.ingest(obs) is True


def test_unknown_zone_id_with_a_coordinate_is_rejected(registry: StoreRegistry) -> None:
    pipeline = FoundationPipeline(registry)
    obs = _item(0.0, 6.0, 4.0, sensor="rfid-floor", zone="vendor-cell-42")
    assert pipeline.ingest(obs) is False
    result = pipeline.finish()
    assert result.observations_rejected_spatially_inconsistent == 1


def test_cli_replay_rejects_when_every_observation_is_spatially_inconsistent(tmp_path) -> None:
    from radiowave.cli import main

    # A recording built by hand (not through the pipeline, which would reject these
    # observations at intake and never record them): every observation is a localized
    # read at (6.0, 4.0) claiming the "exit" zone, which does not contain that point.
    registry = StoreRegistry(build_lab_store())
    config = PipelineConfig()
    path = tmp_path / "spatially_inconsistent.jsonl"
    recorder = JsonlRecorder(path)
    header_timestamp = at(0.0)
    recorder.record(
        RecordedEntry(
            sequence=0,
            timestamp=header_timestamp,
            kind=EntryKind.STORE_TWIN,
            payload=registry.store.model_dump(mode="json"),
        )
    )
    recorder.record(
        RecordedEntry(
            sequence=1,
            timestamp=header_timestamp,
            kind=EntryKind.PIPELINE_CONFIG,
            payload=config.model_dump(mode="json"),
        )
    )
    for i in range(3):
        obs = _item(i * 0.1, 6.0, 4.0, sensor="rfid-floor", zone="exit")
        recorder.record(RecordedEntry.from_observation(2 + i, obs))
    recorder.close()

    assert main(["replay", str(path)]) == 1


# ---------------------------------------------------------------------------------
# Combined cross-P1 regression: invalid, duplicate and contradictory input never
# mutates physical state.


def test_invalid_duplicate_and_contradictory_input_never_mutates_physical_state(
    registry: StoreRegistry,
) -> None:
    pipeline = FoundationPipeline(registry, scenario_id="unit")
    last_accepted = None
    for i in range(11):  # a real shopper standing near the F1 fixture, t = 0.0 .. 1.0
        obs = _person(i * 0.1, 3.8, 5.4, native="T1", sensor="radar-north")
        assert pipeline.ingest(obs) is True
        last_accepted = obs
    for i in range(6):  # the item resting at (3.8, 6.5), genuine reads, t = 1.0 .. 1.5
        obs = _item(1.0 + i * 0.1, 3.8, 6.5)
        assert pipeline.ingest(obs) is True
        last_accepted = obs
    assert last_accepted is not None

    item = pipeline.fusion.items.get(EPC(value=EPC_SHIRT_A))
    assert item is not None

    def snapshot() -> dict:
        return {
            "steps": pipeline._steps,
            "person_tracks": {
                t.track_id: {
                    "state": t.state,
                    "position": t.position,
                    "velocity": t.velocity,
                    "observation_count": t.observation_count,
                }
                for t in pipeline.fusion.persons.all
            },
            "session_states": {s.session_id: s.state for s in pipeline.fusion.sessions.values()},
            "cart_state": pipeline.cart.state.model_dump(),
            "proposed": len(pipeline._proposed),
            "pending": len(pipeline._pending),
            "item_state": item.state,
            "item_carrier": item.carrier_track_id,
            "item_position": item.position,
            "item_observation_count": item.observation_count,
            "item_localized_count": item.localized_count,
            "item_history_len": len(item.history),
        }

    before = snapshot()
    assert before["session_states"]  # the shopper has an ACTIVE session
    assert all(state == SessionState.ACTIVE for state in before["session_states"].values())

    # 1. a duplicate of the last accepted observation, timestamped 30 s later.
    duplicate = last_accepted.model_copy(update={"timestamp": at(31.5)})
    assert pipeline.ingest(duplicate) is False

    # 2. a foreign scenario_id.
    foreign = _item(1.5, 3.8, 6.5).model_copy(
        update={"scenario_id": "other", "observation_id": "foreign:1"}
    )
    assert pipeline.ingest(foreign) is False

    # 3. an unknown sensor.
    unknown_sensor = _item(1.5, 3.8, 6.5, sensor="rfid-nonexistent")
    assert pipeline.ingest(unknown_sensor) is False

    # 4. a person observation from an RFID sensor (modality mismatch).
    modality_mismatch = PersonObservation(
        observation_id="mismatch:1",
        sensor_id="rfid-f1",
        timestamp=at(1.5),
        confidence=0.9,
        coordinate=WorldCoordinate(x=3.8, y=5.4, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(),
    )
    assert pipeline.ingest(modality_mismatch) is False

    # 5-8. four contradictory localized exit reads.
    for t in (1.51, 1.52, 1.53, 1.54):
        contradictory = _item(t, 6.0, 4.0, sensor="rfid-floor", zone="exit")
        assert pipeline.ingest(contradictory) is False

    # 9. a low-confidence displaced item read: accepted by intake, ignored by tracking.
    # Same timestamp as the last accepted observation (1.5 s), so it is neither
    # out-of-order nor able to advance fusion time past what the snapshot already covers.
    low_confidence = _item(1.5, 8.0, 6.5, sensor="rfid-f2", zone="zone-f2").model_copy(
        update={"confidence": 0.0}
    )
    assert pipeline.ingest(low_confidence) is True

    # 10. a genuinely new observation older than _last_timestamp (still 1.5 s).
    stale_new = _item(1.45, 3.8, 6.5)
    assert pipeline.ingest(stale_new) is False

    after = snapshot()
    assert after == before
    proposed_types = {e.event_type for e in pipeline._proposed}
    assert not proposed_types & {
        RetailEventType.PICK,
        RetailEventType.CARRY,
        RetailEventType.PUTBACK,
        RetailEventType.MISPLACE,
        RetailEventType.HANDOFF,
        RetailEventType.EXIT_WITH_ITEM,
    }
    assert all(s.state == SessionState.ACTIVE for s in pipeline.fusion.sessions.values())

    result = pipeline.finish()
    assert result.observations_dropped == 1
    assert result.observations_rejected_foreign_scenario == 1
    assert result.observations_rejected_unknown_sensor == 2
    assert result.observations_rejected_spatially_inconsistent == 4
    assert result.observations_out_of_order == 1
    assert result.observations_rejected_low_confidence == 1
