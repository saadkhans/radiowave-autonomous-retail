"""Regressions for the internal review of the Codex fix commits."""

from __future__ import annotations

from radiowave.contracts import (
    EPC,
    NATIVE_TRACK_KEY,
    ConfidenceThresholds,
    Decision,
    ItemObservation,
    ItemState,
    PersonObservation,
    RetailEvent,
    RetailEventType,
    SessionState,
    SpatialUncertainty,
    Velocity,
    WorldCoordinate,
    make_event_id,
)
from radiowave.contracts.recording import EntryKind
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import FusionConfig, ItemTrackingConfig, PersonTrackingConfig
from radiowave.fusion.state_machine import ItemStateMachine
from radiowave.fusion.tracking import ItemTrackManager, PersonTrackManager
from radiowave.pipeline import PipelineConfig
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import build_pipeline, run_scenario, scenario_observations
from radiowave.simulator.stores import EPC_SHIRT_A, FIXTURE_F1
from tests.conftest import at

ITEM_CFG = ItemTrackingConfig(smoothing_alpha=1.0, rest_init_reads=2, stale_after_s=1.5)


def _person(
    t: float, x: float, y: float, native: str | None = "T1", vx: float = 0.0, vy: float = 0.0
) -> PersonObservation:
    return PersonObservation(
        observation_id=f"mmwave:radar-north:{t:.3f}:{x}:{y}",
        sensor_id="radar-north",
        timestamp=at(t),
        confidence=0.9,
        coordinate=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(vx=vx, vy=vy),
        metadata={NATIVE_TRACK_KEY: native} if native is not None else {},
    )


def _item(t: float, x: float | None, y: float | None, zone: str | None = None) -> ItemObservation:
    coordinate = WorldCoordinate(x=x, y=y, z=0.9) if x is not None and y is not None else None
    return ItemObservation(
        observation_id=f"rfid:{zone or 'rfid-f1'}:{t:.3f}",
        sensor_id="rfid-exit" if zone == "exit" else "rfid-f1",
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        zone_id=zone or "zone-f1",
        coordinate=coordinate,
        uncertainty=SpatialUncertainty.isotropic(0.5) if coordinate else None,
    )


def test_native_id_survives_a_frame_gap_and_a_turn() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    manager.ingest(_person(0.0, 2.0, 4.0, vx=1.4))
    manager.ingest(_person(0.1, 2.14, 4.0, vx=1.4))
    # 0.9 s without frames (track still ACTIVE), shopper turned the corner meanwhile.
    resumed = manager.ingest(_person(1.0, 2.4, 5.4, vx=0.0, vy=1.4))
    assert resumed.track_id == "P0001"
    assert len(manager.all) == 1


def test_recycled_id_far_away_is_still_a_new_person() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    manager.ingest(_person(0.0, 2.0, 4.0))
    manager.ingest(_person(0.1, 2.0, 4.0))
    assert manager.ingest(_person(0.2, 9.0, 6.0)).track_id == "P0002"


def test_two_hintless_shoppers_in_one_frame_stay_separate() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    ids = set()
    for i in range(5):
        t = i * 0.1
        ids.add(manager.ingest(_person(t, 4.0, 5.0, native=None)).track_id)
        ids.add(manager.ingest(_person(t, 4.5, 5.0, native=None)).track_id)
    assert ids == {"P0001", "P0002"}
    assert manager.get("P0001").observation_count == 5  # type: ignore[union-attr]


def test_expired_stale_proposal_does_not_poison_the_next_episode(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.CARRIED
    item.movement_start_at = at(0.5)
    old_pick = RetailEvent(
        event_id=make_event_id(RetailEventType.PICK, epc, at(0.5)),
        event_type=RetailEventType.PICK,
        timestamp=at(1.0),
        epc=epc,
        confidence=0.5,
    )
    assert engine.is_active(old_pick)
    # A new episode starts (putback, then a fresh pick at another moment).
    item.movement_start_at = at(8.0)
    assert not engine.is_active(old_pick)
    engine.acknowledge(old_pick, committed=False)  # late REVIEW of the old proposal
    assert item.attribution_unresolved is False


def test_pending_one_shot_events_are_dropped_when_their_episode_ends() -> None:
    scenario = load_scenario("02")  # PICK then PUTBACK; a strict threshold keeps PUTBACK waiting
    config = PipelineConfig(
        thresholds=ConfidenceThresholds(commit_min_confidence=0.95, max_wait_seconds=60.0)
    )
    result = run_scenario(scenario, pipeline_config=config)
    assert result.committed_events == []
    assert all(e.event_type != RetailEventType.PUTBACK for e in result.review_events)
    # The PUTBACK was proposed once and re-decided while pending; it never became REVIEW.
    putbacks = [e for e in result.proposed_events if e.event_type == RetailEventType.PUTBACK]
    assert len(putbacks) == 1
    assert result.decisions_of(Decision.WAIT)


def test_committed_carrier_keeps_evidence_through_the_exit() -> None:
    result = run_scenario(load_scenario("04"))
    exit_event = next(
        e for e in result.committed_events if e.event_type == RetailEventType.EXIT_WITH_ITEM
    )
    assert [c.person_track_id for c in exit_event.candidates] == [exit_event.shopper_track_id]
    assert exit_event.candidates[0].score > 0.5


def test_dwell_timers_reset_after_a_blackout(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.CARRIED
    item.at_rest_since = at(0.9)
    engine._handoff_since[epc] = ("P0002", at(0.9))
    engine.step(at(3.0))  # no reads for 2 s: stale
    assert item.at_rest_since is None
    assert epc not in engine._handoff_since


def test_walking_past_the_door_does_not_reopen_a_session(registry: StoreRegistry) -> None:
    engine = BaselineFusionEngine(registry)
    for i in range(6):
        engine.ingest(_person(i * 0.1, 11.2, 4.0))
    engine.step(at(0.6))
    assert engine.sessions["P0001"].state == SessionState.EXITED
    for i in range(6, 12):  # keeps walking east, beyond the store floor
        engine.ingest(_person(i * 0.1, 11.2 + 0.2 * (i - 5), 4.0))
        engine.step(at(i * 0.1 + 0.05))
    assert engine.sessions["P0001"].state == SessionState.EXITED
    assert engine.session_history == []


def test_zone_only_exit_portal_reads_close_a_carried_episode(registry: StoreRegistry) -> None:
    machine = ItemStateMachine(FusionConfig().state_machine, ITEM_CFG, registry)
    manager = ItemTrackManager(ITEM_CFG)
    track = manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    for i in range(3):
        manager.ingest(_item(i * 0.1, 8.0, 4.0))
    track.state = ItemState.CARRIED
    track.rest_position = WorldCoordinate(x=3.8, y=6.5)
    manager.ingest(_item(5.0, None, None, zone="exit"))  # one stray portal read: not enough
    assert machine.evaluate(track, at(5.05), None, None) is None
    for i in range(1, 3):  # a portal burst of consecutive zone-only reads closes the episode
        manager.ingest(_item(5.0 + 0.1 * i, None, None, zone="exit"))
    transition = machine.evaluate(track, at(5.3), None, None)
    assert transition is not None and transition.to_state == ItemState.EXITED
    assert "exit zone" in transition.reason


def test_recording_has_one_retail_event_entry_per_proposal() -> None:
    scenario = load_scenario("02")

    class Capture:
        def __init__(self) -> None:
            self.entries: list[object] = []

        def record(self, entry: object) -> None:
            self.entries.append(entry)

        def close(self) -> None:
            return None

    recorder = Capture()
    result = build_pipeline(scenario, recorder=recorder).run(scenario_observations(scenario))
    recorded = [e for e in recorder.entries if e.kind == EntryKind.RETAIL_EVENT]
    assert len(recorded) == len(result.proposed_events)
    timestamps = [e.timestamp for e in recorder.entries]
    assert timestamps == sorted(timestamps)
