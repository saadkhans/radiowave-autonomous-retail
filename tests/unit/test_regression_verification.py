"""Regression verification pass: replay fidelity, cart/exit semantics, low-confidence

intake, HANDOFF invalidation at the pipeline level, and unknown-zone robustness.

Each test targets exact final state (item tracks, cart state, decision sequence, event
ids) rather than mere execution, per the areas A-E of the verification brief. Areas F
(physical uniqueness) and G (temporal validation) are already pinned exactly by the
existing suite (see the module docstring notes below each area) and are not duplicated
here.
"""

from __future__ import annotations

from radiowave.cart.engine import InMemoryCartEngine
from radiowave.cart.models import CartStatus
from radiowave.contracts import (
    EPC,
    ConfidenceThresholds,
    ItemObservation,
    ItemState,
    RetailEvent,
    RetailEventType,
    SpatialUncertainty,
    Store,
    WorldCoordinate,
    make_event_id,
)
from radiowave.contracts.recording import EntryKind
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import FusionConfig, StateMachineConfig
from radiowave.pipeline import FoundationPipeline, PipelineConfig
from radiowave.replay.reader import JsonlReplaySource, observations_from
from radiowave.replay.recorder import InMemoryRecorder, JsonlRecorder
from radiowave.simulator.library import load_scenario
from radiowave.simulator.runner import build_pipeline, scenario_observations
from radiowave.simulator.stores import EPC_SHIRT_A, EPC_SHIRT_B, GTIN_BLACK_SHIRT_L
from tests.conftest import at


def _item(
    t: float,
    x: float | None,
    y: float | None,
    confidence: float = 0.8,
    zone: str = "zone-f1",
) -> ItemObservation:
    coordinate = WorldCoordinate(x=x, y=y, z=0.9) if x is not None and y is not None else None
    return ItemObservation(
        observation_id=f"rfid:{zone}:{t:.3f}:{confidence}",
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=confidence,
        epc=EPC(value=EPC_SHIRT_A),
        zone_id=zone,
        coordinate=coordinate,
        uncertainty=SpatialUncertainty.isotropic(0.5) if coordinate else None,
    )


def _cart_event(
    kind: RetailEventType,
    epc: EPC,
    shopper: str | None,
    t: float,
    counterpart: str | None = None,
) -> RetailEvent:
    return RetailEvent(
        event_id=make_event_id(kind, epc, at(t), shopper),
        event_type=kind,
        timestamp=at(t),
        epc=epc,
        shopper_track_id=shopper,
        counterpart_track_id=counterpart,
        confidence=0.9,
    )


# ---------------------------------------------------------------------------------
# A. Replay configuration fidelity
#
# Existing coverage: test_codex_round4.py::
# test_recording_carries_the_pipeline_config_and_replays_with_it
# (varies only commit_min_confidence, compares committed event ids and cart_state) and
# ::test_header_is_written_even_without_observations (zero *observations*, not zero
# *accepted* observations, but a scenario with ground truth already scheduled).
# Neither varies step_interval_s and fusion thresholds together, nor asserts item-track
# final state or the full decision+event_id sequence, nor drives the "zero accepted"
# case through a scenario/pipeline with no ground truth scheduled. New tests below.
# ---------------------------------------------------------------------------------


def test_replay_with_nondefault_config_reproduces_item_state_cart_state_and_decisions(
    tmp_path,
) -> None:
    scenario = load_scenario("05")  # PICK then HANDOFF: exercises attribution + confidence
    config = PipelineConfig(
        step_interval_s=0.5,
        thresholds=ConfidenceThresholds(commit_min_confidence=0.6, commit_min_margin=0.1),
        fusion=FusionConfig(state_machine=StateMachineConfig(carry_displacement_m=1.2)),
    )
    path = tmp_path / "nondefault.jsonl"
    recorder = JsonlRecorder(path)
    live = build_pipeline(scenario, config, recorder=recorder).run(scenario_observations(scenario))
    recorder.close()

    entries = list(JsonlReplaySource(path).entries())
    assert [e.kind for e in entries[:2]] == [EntryKind.STORE_TWIN, EntryKind.PIPELINE_CONFIG]
    recorded_config = PipelineConfig.model_validate(entries[1].payload)
    assert recorded_config == config
    recorded_store = Store.model_validate(entries[0].payload)

    # Replay from the recorded STORE_TWIN + PIPELINE_CONFIG + observations only (never the
    # scenario object), matching how a real replay-from-recording would be constructed.
    replay_pipeline = FoundationPipeline(
        StoreRegistry(recorded_store), recorded_config, scenario_id=scenario.scenario_id
    )
    replayed = replay_pipeline.run(observations_from(entries))

    assert [e.event_id for e in replayed.committed_events] == [
        e.event_id for e in live.committed_events
    ]
    assert len(live.committed_events) > 0  # the scenario actually produced commits to compare
    assert [(d.event_id, d.decision) for d in replayed.decisions] == [
        (d.event_id, d.decision) for d in live.decisions
    ]
    assert replayed.cart_state == live.cart_state
    assert [t.model_dump() for t in replayed.item_tracks] == [
        t.model_dump() for t in live.item_tracks
    ]


def test_zero_accepted_observations_with_scheduled_ground_truth_still_writes_header() -> None:
    """The literal case from the brief: observations arrive but none are accepted, while
    ground truth *is* scheduled (scenario 04 carries expected_events)."""
    scenario = load_scenario("04")
    recorder = InMemoryRecorder()
    pipeline = build_pipeline(scenario, recorder=recorder)
    bad_sensor_obs = _item(0.0, 4.0, 6.5, zone="zone-f1").model_copy(
        update={"sensor_id": "not-a-real-sensor", "observation_id": "bad:1"}
    )
    assert pipeline.ingest(bad_sensor_obs) is False
    result = pipeline.finish()
    assert result.observations_accepted == 0
    kinds = [e.kind for e in recorder.entries]
    assert kinds[:2] == [EntryKind.STORE_TWIN, EntryKind.PIPELINE_CONFIG]
    assert EntryKind.GROUND_TRUTH in kinds
    timestamps = [e.timestamp for e in recorder.entries]
    assert timestamps == sorted(timestamps)


def test_zero_accepted_observations_without_ground_truth_still_writes_header(
    registry: StoreRegistry,
) -> None:
    """DEFECT reproduction.

    File: radiowave/pipeline.py, method FoundationPipeline._ensure_header (called only
    from ingest(), after a successful dedup accept, and from finish(), only when
    self._scheduled is non-empty).

    Scenario: a FoundationPipeline with a recorder, no scenario/ground truth scheduled,
    fed exactly one observation whose sensor is unknown to the twin (rejected before
    dedup, so dedup.accept() -- and therefore _ensure_header() -- is never reached during
    ingest()); finish() then also skips _ensure_header() because self._scheduled is empty.

    Expected (per pipeline.py's own docstring: "the header is stamped no later than the
    first entry" and the verification brief: "a run with zero accepted observations still
    writes STORE_TWIN + PIPELINE_CONFIG"): recorder.entries starts with
    [STORE_TWIN, PIPELINE_CONFIG] regardless of whether any observation was accepted or
    any ground truth was scheduled.

    Actual: recorder.entries is completely empty -- the header is silently never written.
    """
    recorder = InMemoryRecorder()
    pipeline = FoundationPipeline(registry, recorder=recorder)  # no scenario_id -> no GT scheduled
    bad_sensor_obs = _item(0.0, 4.0, 6.5, zone="zone-f1").model_copy(
        update={"sensor_id": "not-a-real-sensor", "observation_id": "bad:1"}
    )
    assert pipeline.ingest(bad_sensor_obs) is False
    result = pipeline.finish()
    assert result.observations_accepted == 0
    kinds = [e.kind for e in recorder.entries]
    assert kinds[:2] == [EntryKind.STORE_TWIN, EntryKind.PIPELINE_CONFIG]


# ---------------------------------------------------------------------------------
# B. Item exit vs shopper cart exit
#
# Existing coverage: test_cart.py::test_exit_preserves_final_ownership_candidate_without_settlement
# (single-line exit, then close_cart), test_codex_round4.py::
# test_item_exit_freezes_the_line_but_only_a_session_exit_closes_the_cart (pipeline/scenario
# 04) and ::test_exit_line_after_a_closed_cart_opens_a_new_lifecycle (new lifecycle after
# close), test_codex_round6.py::test_lost_inside_the_store_at_finish_keeps_the_cart_open.
# None of these covers a *second, untouched line coexisting with an exited line in the same
# still-open cart*, a *later PICK landing in that same open cart*, and the *closed cart's
# lines being immutable to further events for the same shopper*, all in one narrative.
# ---------------------------------------------------------------------------------


def test_item_exit_freezes_only_its_own_line_until_session_close_and_closed_cart_is_immutable() -> (
    None
):
    engine = InMemoryCartEngine({EPC_SHIRT_A: GTIN_BLACK_SHIRT_L, EPC_SHIRT_B: GTIN_BLACK_SHIRT_L})
    epc_a = EPC(value=EPC_SHIRT_A)
    epc_b = EPC(value=EPC_SHIRT_B)
    epc_c = EPC(value="3034F1B0000000000000B002")
    epc_d = EPC(value="3034F1B0000000000000B003")

    engine.apply(_cart_event(RetailEventType.PICK, epc_a, "P0001", 1.0))
    engine.apply(_cart_event(RetailEventType.PICK, epc_b, "P0001", 2.0))
    engine.apply(_cart_event(RetailEventType.EXIT_WITH_ITEM, epc_a, "P0001", 5.0))

    cart = engine.state.carts["P0001"]
    assert cart.status == CartStatus.OPEN  # EXIT_WITH_ITEM alone never closes the cart
    assert cart.lines[EPC_SHIRT_A].final_ownership_candidate is True
    assert cart.lines[EPC_SHIRT_A].exit_event_at == at(5.0)
    assert cart.lines[EPC_SHIRT_B].final_ownership_candidate is False
    assert cart.lines[EPC_SHIRT_B].exit_event_at is None

    # A later PICK for the same shopper lands in the SAME (still open) cart.
    engine.apply(_cart_event(RetailEventType.PICK, epc_c, "P0001", 6.0))
    assert engine.state.carts["P0001"] is cart
    assert cart.epcs == {EPC_SHIRT_A, EPC_SHIRT_B, epc_c.value}
    assert cart.status == CartStatus.OPEN

    # Only close_cart (the shopper's session ending) freezes it.
    engine.close_cart("P0001", at(9.0))
    assert cart.status == CartStatus.EXITED
    assert cart.exited_at == at(9.0)
    frozen_snapshot = cart.model_dump()

    # A further event for the same shopper track opens a brand-new lifecycle and never
    # mutates the closed cart's lines.
    engine.apply(_cart_event(RetailEventType.PICK, epc_d, "P0001", 10.0))
    assert cart.model_dump() == frozen_snapshot
    assert "P0001#2" in engine.state.carts
    assert engine.state.carts["P0001#2"].epcs == {epc_d.value}
    assert engine.state.current_cart_ids["P0001"] == "P0001#2"


# ---------------------------------------------------------------------------------
# C. Low-confidence input
#
# Existing coverage: test_codex_round4.py::test_low_confidence_reads_never_touch_physical_state
# (ItemTrackManager + ItemStateMachine directly),
# ::test_low_confidence_person_observations_are_ignored,
# ::test_rejected_read_never_creates_item_identity (PersonTrackManager.ingest /
# ItemTrackManager.ingest return None; ItemStateMachine.evaluate returns None on
# low-confidence-only reads). None of
# these drives BaselineFusionEngine: no test asserts the candidate ledger is unmoved and no
# PICK/HANDOFF/EXIT_WITH_ITEM proposals appear when only low-confidence samples arrive.
# ---------------------------------------------------------------------------------


def _weak_person(t: float, x: float, y: float):
    from radiowave.contracts import NATIVE_TRACK_KEY, PersonObservation, Velocity

    return PersonObservation(
        observation_id=f"mmwave:radar-north:{t:.3f}:weak",
        sensor_id="radar-north",
        timestamp=at(t),
        confidence=0.05,
        coordinate=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(),
        metadata={NATIVE_TRACK_KEY: "T1"},
    )


def _strong_person(t: float, x: float, y: float):
    from radiowave.contracts import NATIVE_TRACK_KEY, PersonObservation, Velocity

    return PersonObservation(
        observation_id=f"mmwave:radar-north:{t:.3f}:strong",
        sensor_id="radar-north",
        timestamp=at(t),
        confidence=0.9,
        coordinate=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(),
        metadata={NATIVE_TRACK_KEY: "T1"},
    )


def test_low_confidence_only_samples_never_move_the_ledger_or_propose_events(
    registry: StoreRegistry,
) -> None:
    engine = BaselineFusionEngine(registry)
    epc = EPC(value=EPC_SHIRT_A)
    for i in range(10):
        engine.ingest(_item(i * 0.1, 3.8, 6.5))
        engine.ingest(_strong_person(i * 0.1, 3.8, 5.4))
    engine.step(at(1.0))
    item = engine.items.get(epc)
    assert item is not None
    item.state = ItemState.INTERACTION_CANDIDATE
    item.movement_start_at = at(0.5)
    events = engine.step(at(1.05))
    assert events == []
    before_score = engine.ledger.score_of(epc, "P0001")
    assert before_score > 0.0
    item_localized_before = item.localized_count
    person = engine.persons.get("P0001")
    assert person is not None
    person_observations_before = person.observation_count

    for i in range(1, 9):
        now = at(1.05 + 0.1 * i)
        t = 1.05 + 0.1 * i
        engine.ingest(_item(t, 5.0, 6.5, confidence=0.05))  # clearly displaced, but too weak
        engine.ingest(_weak_person(t, 5.0, 6.5))
        events = engine.step(now)
        assert events == []  # no PICK/HANDOFF/EXIT_WITH_ITEM arises from weak reads alone

    assert item.localized_count == item_localized_before  # weak reads never registered
    assert person.observation_count == person_observations_before
    assert engine.ledger.score_of(epc, "P0001") == before_score  # ledger frozen
    assert item.state == ItemState.INTERACTION_CANDIDATE  # state machine never advanced
    assert item.position is not None and item.position.x < 4.5  # never actually displaced
    assert engine.items.rejected_low_confidence == 8
    assert engine.persons.rejected_low_confidence == 8


# ---------------------------------------------------------------------------------
# D. HANDOFF invalidation via BaselineFusionEngine.is_active
#
# Existing coverage: test_codex_round4.py::
# test_handoff_proposal_is_inactive_once_the_receiver_changes
# pins is_active() itself at the engine level. test_self_review.py::
# test_pending_one_shot_events_are_dropped_when_their_episode_ends pins the pipeline-level
# drop mechanism for a one-shot PUTBACK proposal. Neither combines the two: a pending WAIT
# HANDOFF at the FoundationPipeline level whose is_active() later turns False. New test below
# drives FoundationPipeline._step with a stub fusion engine so the transition is deterministic.
# ---------------------------------------------------------------------------------


class _StubFusionEngine:
    """Minimal stand-in for BaselineFusionEngine, controlling exactly what FoundationPipeline
    needs from ``self.fusion`` inside ``_step``."""

    def __init__(self) -> None:
        from types import SimpleNamespace

        self.session_history: list = []
        self.sessions: dict = {}
        self._events_by_step: dict = {}
        self.is_active_result = True
        self.acknowledged: list[tuple[str, bool]] = []
        self.persons = SimpleNamespace(rejected_low_confidence=0)
        self.items = SimpleNamespace(rejected_low_confidence=0)

    def step(self, now):
        return self._events_by_step.pop(now, [])

    def is_active(self, event) -> bool:
        return self.is_active_result

    def acknowledge(self, event, committed: bool) -> None:
        self.acknowledged.append((event.event_id, committed))

    def person_tracks(self):
        return []

    def item_tracks(self):
        return []


def test_pending_handoff_is_dropped_without_review_when_is_active_turns_false(
    registry: StoreRegistry,
) -> None:
    config = PipelineConfig()  # default thresholds: wait_min=0.40, commit_min=0.75
    pipeline = FoundationPipeline(registry, config)
    stub = _StubFusionEngine()
    pipeline.fusion = stub

    epc = EPC(value=EPC_SHIRT_A)
    handoff = RetailEvent(
        event_id=make_event_id(RetailEventType.HANDOFF, epc, at(1.0), "P0002"),
        event_type=RetailEventType.HANDOFF,
        timestamp=at(1.0),
        epc=epc,
        shopper_track_id="P0001",
        counterpart_track_id="P0002",
        confidence=0.5,  # below commit, above wait_min -> WAIT, never COMMIT/REVIEW here
    )
    stub._events_by_step[at(1.0)] = [handoff]

    pipeline._step(at(1.0))
    assert handoff.event_id in pipeline._pending
    assert pipeline._decisions[-1].event_id == handoff.event_id
    assert pipeline._decisions[-1].decision.value == "WAIT"
    decisions_before = len(pipeline._decisions)

    forgotten: list[str] = []
    original_forget = pipeline.confidence.forget

    def _spy_forget(event_id: str) -> None:
        forgotten.append(event_id)
        original_forget(event_id)

    pipeline.confidence.forget = _spy_forget  # type: ignore[method-assign]
    stub.is_active_result = False  # the receiver changed: the proposal no longer applies

    pipeline._step(at(1.5))

    assert handoff.event_id not in pipeline._pending
    assert forgotten == [handoff.event_id]
    # Dropped, not decided: no new ConfidenceDecision was recorded for it.
    assert len(pipeline._decisions) == decisions_before

    result = pipeline.result()
    assert result.pending_events == []
    assert all(e.event_id != handoff.event_id for e in result.review_events)
    assert all(e.event_id != handoff.event_id for e in result.committed_events)


# ---------------------------------------------------------------------------------
# E. Unknown zones
#
# Existing coverage: test_codex_round4.py::test_unknown_zone_ids_are_not_exit_evidence pins
# ItemTrackManager + ItemStateMachine directly against an unknown zone_id. No existing test
# drives this through FoundationPipeline.ingest/finish nor through a JSONL record -> replay
# round trip.
# ---------------------------------------------------------------------------------


def test_unknown_zone_reads_flow_through_the_pipeline_and_a_jsonl_replay_round_trip(
    tmp_path, registry: StoreRegistry
) -> None:
    assert registry.zone_or_none("vendor-cell-42") is None  # unknown zone -> None, no KeyError

    scenario = load_scenario("01")
    path = tmp_path / "unknown_zone.jsonl"
    recorder = JsonlRecorder(path)
    pipeline = build_pipeline(scenario, recorder=recorder)
    epc = EPC(value=EPC_SHIRT_A)

    for i in range(10):  # establish a real rest position from a known zone
        pipeline.ingest(_item(i * 0.1, 3.8, 6.5))

    item = pipeline.fusion.items.get(epc)
    assert item is not None and item.rest_position is not None
    item.state = ItemState.CARRIED
    item.carrier_track_id = None

    for i in range(10, 40):  # zone-only reads from a zone the twin has never heard of
        pipeline.ingest(_item(i * 0.1, None, None, zone="vendor-cell-42"))

    result = pipeline.finish()
    recorder.close()

    assert all(e.event_type != RetailEventType.EXIT_WITH_ITEM for e in result.proposed_events)
    assert result.committed(RetailEventType.EXIT_WITH_ITEM) == []
    replayed_item_track = result.item(EPC_SHIRT_A)
    assert replayed_item_track.zone_id == "vendor-cell-42"
    assert replayed_item_track.state != ItemState.EXITED

    # JSONL round trip: read back, rebuild the pipeline from the recorded twin/config only.
    entries = list(JsonlReplaySource(path).entries())
    store = Store.model_validate(next(e.payload for e in entries if e.kind == EntryKind.STORE_TWIN))
    config = PipelineConfig.model_validate(
        next(e.payload for e in entries if e.kind == EntryKind.PIPELINE_CONFIG)
    )
    replay_pipeline = FoundationPipeline(
        StoreRegistry(store), config, scenario_id=scenario.scenario_id
    )
    replayed = replay_pipeline.run(observations_from(entries))  # must not raise

    assert all(e.event_type != RetailEventType.EXIT_WITH_ITEM for e in replayed.proposed_events)
    assert replayed.committed(RetailEventType.EXIT_WITH_ITEM) == []
