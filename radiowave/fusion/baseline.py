"""Explainable baseline fusion engine (v0).

Responsibilities
----------------
* keep canonical person and item tracks up to date from normalized observations;
* run the per-item retail state machine;
* keep a ranked, explainable candidate ledger for every moving item;
* propose retail events with their evidence, re-proposing PICK/HANDOFF while
  the confidence layer is still waiting, and never committing on its own.

No ML, no vendor logic, no raw sensor coordinates.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime

from radiowave.contracts.events import RetailEvent, RetailEventType, make_event_id
from radiowave.contracts.observations import (
    ItemObservation,
    PersonObservation,
    SensorObservation,
    VisionEvidence,
)
from radiowave.contracts.sessions import SessionState, ShopperSession
from radiowave.contracts.store import EPC, BoundaryKind
from radiowave.contracts.tracks import (
    CandidateScore,
    InteractionCandidate,
    ItemState,
    ItemTrack,
    PersonTrack,
    PersonTrackState,
)
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.association import (
    AssociationScorer,
    CandidateLedger,
    assign_vision_to_nearest,
)
from radiowave.fusion.config import FusionConfig
from radiowave.fusion.state_machine import ItemStateMachine, Transition
from radiowave.fusion.tracking import (
    ItemTrackManager,
    ItemTrackState,
    PersonState,
    PersonTrackManager,
)

_VISION_BUFFER = 400


def _seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


class BaselineFusionEngine:
    def __init__(
        self,
        registry: StoreRegistry,
        config: FusionConfig | None = None,
        vision_enabled: bool = False,
        scenario_id: str | None = None,
    ) -> None:
        self.config = config or FusionConfig()
        self.registry = registry
        self.scenario_id = scenario_id
        self.persons = PersonTrackManager(self.config.person)
        self.items = ItemTrackManager(self.config.item)
        self.state_machine = ItemStateMachine(self.config.state_machine, self.config.item, registry)
        self.scorer = AssociationScorer(
            self.config.association, self.config.item, registry, vision_enabled
        )
        self.ledger = CandidateLedger(self.config.association)
        self.transitions: list[Transition] = []
        self.sessions: dict[str, ShopperSession] = {}
        self.session_history: list[ShopperSession] = []  # exited/abandoned, in order
        self._session_counter: dict[str, int] = {}
        self._vision: deque[VisionEvidence] = deque(maxlen=_VISION_BUFFER)
        self._handoff_since: dict[EPC, tuple[str, datetime]] = {}
        self._committed_ids: set[str] = set()
        self._rejected_ids: set[str] = set()
        self._last_step: datetime | None = None
        for item in registry.items:
            self.items.register(item.epc, item.gtin, registry.home_fixture_id(item.epc))

    # ------------------------------------------------------------------ intake
    def ingest(self, observation: SensorObservation) -> None:
        if isinstance(observation, PersonObservation):
            self.persons.ingest(observation)
        elif isinstance(observation, ItemObservation):
            track = self.items.ingest(observation)
            if track.gtin is None:
                twin_item = self.registry.item(track.epc)
                if twin_item is not None:
                    track.gtin = twin_item.gtin
                    track.home_fixture_id = self.registry.home_fixture_id(track.epc)
        elif isinstance(observation, VisionEvidence):
            self._vision.append(observation)

    # ------------------------------------------------------------------ queries
    def person_tracks(self) -> list[PersonTrack]:
        return self.persons.snapshots()

    def item_tracks(self) -> list[ItemTrack]:
        return self.items.snapshots()

    def candidates(self, epc: EPC) -> InteractionCandidate | None:
        item = self.items.get(epc)
        if item is None or not self.ledger.persons(epc):
            return None
        now = self._last_step or item.last_seen_at
        if now is None:
            return None
        return self.ledger.ranking(epc, now, item.movement_start_at)

    def acknowledge(self, event: RetailEvent, committed: bool) -> None:
        item = self.items.get(event.epc)
        if committed:
            self._committed_ids.add(event.event_id)
            if item is None:
                return
            if event.event_type == RetailEventType.PICK:
                item.carrier_track_id = event.shopper_track_id
                item.attribution_unresolved = False
            elif event.event_type == RetailEventType.HANDOFF:
                item.carrier_track_id = event.counterpart_track_id
                self._handoff_since.pop(event.epc, None)
        else:
            self._rejected_ids.add(event.event_id)
            if item is not None and event.event_type in (
                RetailEventType.PICK,
                RetailEventType.HANDOFF,
            ):
                item.attribution_unresolved = True
                self._handoff_since.pop(event.epc, None)

    # ------------------------------------------------------------------ stepping
    def step(self, now: datetime) -> list[RetailEvent]:
        self._last_step = now
        self.persons.step(now)
        self._update_sessions(now)
        events: list[RetailEvent] = []
        for item in self.items.all:
            events.extend(self._step_item(item, now))
        return events

    def _step_item(self, item: ItemTrackState, now: datetime) -> list[RetailEvent]:
        events: list[RetailEvent] = []
        if item.state == ItemState.UNKNOWN and item.rest_position is not None:
            # First classification of a freshly seen item against the twin.
            item.state = self.state_machine.classify_rest(item, item.rest_position)
            item.rest_state = item.state
            item.state_since = now
        carrier = self.persons.get(item.carrier_track_id) if item.carrier_track_id else None
        transition = self.state_machine.evaluate(
            item,
            now,
            carrier.position if carrier else None,
            self._nearest_person_m(item, now),
        )
        if transition is not None:
            self.transitions.append(transition)
            events.extend(self._events_for_transition(item, transition, now))
        if item.is_stale(now, self.config.item.stale_after_s):
            return events  # no fresh reads: never score or attribute against a cached position
        if item.state in (ItemState.INTERACTION_CANDIDATE, ItemState.CARRIED):
            self._score_candidates(item, now)
        if item.state == ItemState.CARRIED:
            events.extend(self._attribution_events(item, now))
        return events

    def _nearest_person_m(self, item: ItemTrackState, now: datetime) -> float | None:
        """Distance from the item to the closest ACTIVE or LOST person track.

        LOST tracks are dead-reckoned for at most ``prediction_horizon_s`` so a
        long-gone shopper cannot hold or release a rest decision from a ghost position.
        """
        if item.position is None:
            return None
        horizon = self.config.person.prediction_horizon_s
        distances = [
            item.position.horizontal_distance_to(person.predicted_position(now, horizon))
            for person in self.persons.all
            if person.state != PersonTrackState.ENDED
        ]
        return min(distances) if distances else None

    # ------------------------------------------------------------------ scoring
    def _score_candidates(self, item: ItemTrackState, now: datetime) -> None:
        if item.position is None:
            return
        cfg = self.config.association
        vision = [v for v in self._vision if _seconds(now, v.timestamp) <= 30.0]
        vision_owner = assign_vision_to_nearest(vision, self.persons.all, cfg.vision_match_radius_m)
        considered = set(self.ledger.persons(item.epc))
        for active in self.persons.active:
            if item.position.horizontal_distance_to(active.position) <= cfg.candidate_radius_m:
                considered.add(active.track_id)
        for person_id in sorted(considered):
            person = self.persons.get(person_id)
            if (
                person is None
                or person.state == PersonTrackState.ENDED
                or not self._in_store(person)
            ):
                # Departed or exited shoppers cannot be candidates nor block others.
                self.ledger.drop(item.epc, person_id)
                continue
            pair = self.ledger.pair(item.epc, person_id)
            evidence = self.scorer.score(
                item,
                person,
                pair,
                now,
                vision,
                vision_owner,
                carrier_committed=item.carrier_track_id is not None,
            )
            self.ledger.update(item.epc, person_id, evidence, now)
            distance = item.position.horizontal_distance_to(person.position)
            self.ledger.prune(item.epc, person_id, distance)

    # ------------------------------------------------------------------ events
    def _events_for_transition(
        self, item: ItemTrackState, transition: Transition, now: datetime
    ) -> list[RetailEvent]:
        to = transition.to_state
        frm = transition.from_state
        if frm == ItemState.INTERACTION_CANDIDATE and to != ItemState.CARRIED:
            # Abandoned interaction (jitter/timeout): its evidence must not seed the next one.
            self.ledger.reset(item.epc)
            self._handoff_since.pop(item.epc, None)
            return []
        if frm != ItemState.CARRIED:
            return []
        if to == ItemState.ON_FIXTURE:
            event_type = RetailEventType.PUTBACK
        elif to == ItemState.MISPLACED:
            event_type = RetailEventType.MISPLACE
        elif to == ItemState.EXITED:
            event_type = RetailEventType.EXIT_WITH_ITEM
        else:
            return []
        event = self._physical_event(item, event_type, transition, now)
        self.ledger.reset(item.epc)
        self._handoff_since.pop(item.epc, None)
        if to != ItemState.EXITED:
            item.carrier_track_id = None
        return [event]

    def _physical_event(
        self,
        item: ItemTrackState,
        event_type: RetailEventType,
        transition: Transition,
        now: datetime,
    ) -> RetailEvent:
        carrier_id = item.carrier_track_id
        candidates: list[CandidateScore] = []
        if carrier_id is not None:
            ranking = self.ledger.ranking(item.epc, now, item.movement_start_at)
            candidates = [c for c in ranking.candidates if c.person_track_id == carrier_id]
        fixture = (
            self.registry.fixture_at(item.position, margin=self.config.state_machine.home_radius_m)
            if item.position
            else None
        )
        zone = self.registry.zone_at(item.position) if item.position else None
        return RetailEvent(
            event_id=make_event_id(event_type, item.epc, now, carrier_id),
            event_type=event_type,
            timestamp=now,
            epc=item.epc,
            shopper_track_id=carrier_id,
            fixture_id=fixture.fixture_id if fixture else None,
            zone_id=zone.zone_id if zone else None,
            confidence=self.config.state_machine.physical_event_confidence,
            candidates=candidates,
            reason=transition.reason,
            scenario_id=self.scenario_id,
        )

    def _attribution_events(self, item: ItemTrackState, now: datetime) -> list[RetailEvent]:
        events: list[RetailEvent] = []
        ranking = self.ledger.ranking(item.epc, now, item.movement_start_at)
        if item.carrier_track_id is None:
            if item.attribution_unresolved or item.movement_start_at is None:
                return events
            event_id = make_event_id(RetailEventType.PICK, item.epc, item.movement_start_at)
            if event_id in self._committed_ids or event_id in self._rejected_ids:
                return events
            top = ranking.top
            home = self.registry.fixture(item.home_fixture_id) if item.home_fixture_id else None
            events.append(
                RetailEvent(
                    event_id=event_id,
                    event_type=RetailEventType.PICK,
                    timestamp=now,
                    epc=item.epc,
                    shopper_track_id=top.person_track_id if top else None,
                    fixture_id=home.fixture_id if home else None,
                    zone_id=home.zone_id if home else None,
                    confidence=top.score if top else 0.0,
                    candidates=ranking.candidates,
                    reason=(
                        f"item CARRIED since {item.movement_start_at.isoformat()}; "
                        f"{len(ranking.candidates)} shopper candidate(s) ranked by evidence"
                    ),
                    scenario_id=self.scenario_id,
                )
            )
            return events
        events.extend(self._carry_event(item, now, ranking))
        events.extend(self._handoff_event(item, now, ranking))
        return events

    def _carry_event(
        self, item: ItemTrackState, now: datetime, ranking: InteractionCandidate
    ) -> list[RetailEvent]:
        if item.carry_announced or item.movement_start_at is None or item.carrier_track_id is None:
            return []
        if _seconds(now, item.movement_start_at) < self.config.carry_confirm_s:
            return []
        item.carry_announced = True
        carrier = [c for c in ranking.candidates if c.person_track_id == item.carrier_track_id]
        zone = self.registry.zone_at(item.position) if item.position else None
        return [
            RetailEvent(
                event_id=make_event_id(
                    RetailEventType.CARRY, item.epc, item.movement_start_at, item.carrier_track_id
                ),
                event_type=RetailEventType.CARRY,
                timestamp=now,
                epc=item.epc,
                shopper_track_id=item.carrier_track_id,
                zone_id=zone.zone_id if zone else None,
                confidence=carrier[0].score
                if carrier
                else self.config.state_machine.physical_event_confidence,
                candidates=carrier,
                reason=f"carried by committed shopper for >= {self.config.carry_confirm_s} s",
                scenario_id=self.scenario_id,
            )
        ]

    def _handoff_event(
        self, item: ItemTrackState, now: datetime, ranking: InteractionCandidate
    ) -> list[RetailEvent]:
        carrier_id = item.carrier_track_id
        top = ranking.top
        if carrier_id is None or top is None or top.person_track_id == carrier_id:
            self._handoff_since.pop(item.epc, None)
            return []
        carrier_score = self.ledger.score_of(item.epc, carrier_id)
        if top.score - carrier_score < self.config.handoff_min_margin:
            self._handoff_since.pop(item.epc, None)
            return []
        since = self._handoff_since.get(item.epc)
        if since is None or since[0] != top.person_track_id:
            self._handoff_since[item.epc] = (top.person_track_id, now)
            return []
        if _seconds(now, since[1]) < self.config.handoff_confirm_s:
            return []
        event_id = make_event_id(RetailEventType.HANDOFF, item.epc, since[1], top.person_track_id)
        if event_id in self._committed_ids or event_id in self._rejected_ids:
            return []
        zone = self.registry.zone_at(item.position) if item.position else None
        return [
            RetailEvent(
                event_id=event_id,
                event_type=RetailEventType.HANDOFF,
                timestamp=now,
                epc=item.epc,
                shopper_track_id=carrier_id,
                counterpart_track_id=top.person_track_id,
                zone_id=zone.zone_id if zone else None,
                confidence=top.score,
                candidates=ranking.candidates,
                reason=(
                    f"{top.person_track_id} out-scored committed carrier {carrier_id} by "
                    f"{top.score - carrier_score:.2f} for {_seconds(now, since[1]):.1f} s"
                ),
                scenario_id=self.scenario_id,
            )
        ]

    # ------------------------------------------------------------------ sessions
    def _in_store(self, person: PersonState) -> bool:
        """A shopper with an ACTIVE session; exited sessions receive no attribution."""
        session = self.sessions.get(person.track_id)
        return session is not None and session.state == SessionState.ACTIVE

    def _update_sessions(self, now: datetime) -> None:
        for person in self.persons.all:
            session = self.sessions.get(person.track_id)
            if session is None:
                session = self._open_session(person, person.created_at)
            if session.state == SessionState.EXITED:
                # A track that steps back out of the exit boundary starts a fresh session;
                # the exited one keeps its cart for settlement.
                if person.state == PersonTrackState.ACTIVE and not self.registry.in_exit_boundary(
                    person.position
                ):
                    self._open_session(person, now)
                continue
            if session.state != SessionState.ACTIVE:
                continue
            if person.state == PersonTrackState.ACTIVE and self.registry.in_exit_boundary(
                person.position
            ):
                session.state = SessionState.EXITED
                session.exited_at = now
                session.exit_boundary_id = self._boundary_id(person, entry=False)
            elif person.state == PersonTrackState.ENDED:
                session.state = SessionState.ABANDONED
                session.exited_at = now

    def _open_session(self, person: PersonState, entered_at: datetime) -> ShopperSession:
        self._session_counter[person.track_id] = self._session_counter.get(person.track_id, 0) + 1
        ordinal = self._session_counter[person.track_id]
        suffix = "" if ordinal == 1 else f"-{ordinal}"
        session = ShopperSession(
            session_id=f"S-{person.track_id}{suffix}",
            person_track_id=person.track_id,
            entered_at=entered_at,
            entry_boundary_id=self._boundary_id(person, entry=True),
        )
        previous = self.sessions.get(person.track_id)
        if previous is not None:
            self.session_history.append(previous)
        self.sessions[person.track_id] = session
        person.session_id = session.session_id
        return session

    def _boundary_id(self, person: PersonState, entry: bool) -> str | None:
        kind = BoundaryKind.ENTRY if entry else BoundaryKind.EXIT
        boundary = self.registry.boundary_at(person.position, kind)
        return boundary.boundary_id if boundary else None
