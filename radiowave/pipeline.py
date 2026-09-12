"""End-to-end Foundation v0 pipeline: observations -> fusion -> confidence -> cart.

The pipeline is deterministic: observations are consumed in (timestamp, id)
order, fusion is stepped on a fixed simulated interval, and no wall-clock time
is involved. The same recording therefore always yields the same result.

Recording goes through the :class:`~radiowave.fusion.interfaces.Recorder`
protocol only (``record(entry)`` / ``close()``); entries are written in
timestamp order with a monotonic sequence number.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta
from typing import Any

from pydantic import Field

from radiowave.cart.engine import InMemoryCartEngine
from radiowave.cart.models import CartState
from radiowave.confidence.engine import ThresholdConfidenceEngine
from radiowave.contracts._base import ContractModel, FrozenModel
from radiowave.contracts.confidence import ConfidenceDecision, ConfidenceThresholds, Decision
from radiowave.contracts.events import CartEvent, RetailEvent
from radiowave.contracts.observations import AnyObservation, SensorObservation
from radiowave.contracts.recording import EntryKind, RecordedEntry
from radiowave.contracts.sessions import SessionState, ShopperSession
from radiowave.contracts.store import SourceType
from radiowave.contracts.tracks import ItemTrack, PersonTrack, PersonTrackState
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import FusionConfig
from radiowave.fusion.interfaces import Recorder
from radiowave.ingestion.deduplication import ObservationDeduplicator


class PipelineConfig(FrozenModel):
    step_interval_s: float = Field(default=0.25, gt=0.0)
    vision_enabled: bool = Field(
        default=False,
        description="Whether a vision provider is wired in; changes association weight "
        "normalization, so it is recorded and restored on replay",
    )
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    thresholds: ConfidenceThresholds = Field(default_factory=ConfidenceThresholds)


class PipelineResult(ContractModel):
    scenario_id: str | None = None
    proposed_events: list[RetailEvent] = Field(
        default_factory=list, description="Every proposal, including re-proposals while waiting"
    )
    decisions: list[ConfidenceDecision] = Field(default_factory=list)
    committed_events: list[RetailEvent] = Field(default_factory=list)
    review_events: list[RetailEvent] = Field(default_factory=list)
    pending_events: list[RetailEvent] = Field(
        default_factory=list, description="Events still in WAIT when the run ended"
    )
    cart_events: list[CartEvent] = Field(default_factory=list)
    cart_state: CartState = Field(default_factory=CartState)
    person_tracks: list[PersonTrack] = Field(default_factory=list)
    item_tracks: list[ItemTrack] = Field(default_factory=list)
    sessions: list[ShopperSession] = Field(default_factory=list)
    cart_sessions: dict[str, str] = Field(
        default_factory=dict, description="Cart id -> session id the cart was committed under"
    )
    observations_accepted: int = 0
    observations_dropped: int = 0
    observations_out_of_order: int = 0
    observations_rejected_unknown_sensor: int = Field(
        default=0, description="Observations whose sensor is unknown or of another modality"
    )
    observations_rejected_low_confidence: int = Field(
        default=0, description="Observations ignored by tracking for low reported confidence"
    )
    observations_rejected_foreign_scenario: int = Field(
        default=0,
        description="Observations stamped with a different scenario_id than this pipeline's own",
    )
    observations_rejected_spatially_inconsistent: int = Field(
        default=0,
        description="Observations whose coordinate falls outside the zone they claim to be in",
    )
    steps: int = 0

    def committed(self, event_type: str | None = None) -> list[RetailEvent]:
        return [
            e for e in self.committed_events if event_type is None or e.event_type == event_type
        ]

    def decisions_of(self, decision: Decision) -> list[ConfidenceDecision]:
        return [d for d in self.decisions if d.decision == decision]

    def item(self, epc_value: str) -> ItemTrack:
        return next(t for t in self.item_tracks if t.epc.value == epc_value)


class FoundationPipeline:
    def __init__(
        self,
        registry: StoreRegistry,
        config: PipelineConfig | None = None,
        scenario_id: str | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.registry = registry
        self.scenario_id = scenario_id
        self.fusion = BaselineFusionEngine(
            registry,
            self.config.fusion,
            vision_enabled=self.config.vision_enabled,
            scenario_id=scenario_id,
        )
        self.confidence = ThresholdConfidenceEngine(self.config.thresholds)
        self.cart = InMemoryCartEngine({i.epc.value: i.gtin for i in registry.items})
        self.dedup = ObservationDeduplicator()
        self.recorder = recorder
        self._proposed: list[RetailEvent] = []
        self._decisions: list[ConfidenceDecision] = []
        self._committed: list[RetailEvent] = []
        self._review: list[RetailEvent] = []
        self._pending: dict[str, RetailEvent] = {}
        self._steps = 0
        self._next_step: datetime | None = None
        self._last_step_at: datetime | None = None
        self._ingested_since_step = False
        self._sequence = 0
        self._scheduled: list[tuple[datetime, int, EntryKind, dict[str, Any]]] = []
        self._last_timestamp: datetime | None = None
        self._twin_recorded = False
        self._schedule_sequence = 0
        self.observations_out_of_order = 0
        self.observations_rejected_unknown_sensor = 0
        self.observations_rejected_foreign_scenario = 0
        self.observations_rejected_spatially_inconsistent = 0
        self._first_input_at: datetime | None = None
        self._cart_session: dict[str, str] = {}  # cart_id -> session_id it was committed under

    # ------------------------------------------------------------------ recording
    def schedule_entry(self, timestamp: datetime, kind: EntryKind, payload: dict[str, Any]) -> None:
        """Queue a non-observation entry (e.g. ground truth) to be recorded in timestamp order."""
        self._schedule_sequence += 1
        heapq.heappush(self._scheduled, (timestamp, self._schedule_sequence, kind, payload))

    def _record(
        self,
        timestamp: datetime,
        kind: EntryKind,
        payload: dict[str, Any],
        source_type: SourceType | None = None,
        sensor_id: str | None = None,
    ) -> None:
        if self.recorder is None:
            return
        self.recorder.record(
            RecordedEntry(
                sequence=self._sequence,
                timestamp=timestamp,
                kind=kind,
                source_type=source_type,
                sensor_id=sensor_id,
                scenario_id=self.scenario_id,
                payload=payload,
            )
        )
        self._sequence += 1

    def _flush_scheduled(self, up_to: datetime | None) -> None:
        while self._scheduled and (up_to is None or self._scheduled[0][0] <= up_to):
            timestamp, _, kind, payload = heapq.heappop(self._scheduled)
            self._record(timestamp, kind, payload)

    # ------------------------------------------------------------------ intake
    def run(self, *streams: Iterable[AnyObservation]) -> PipelineResult:
        """Consume one or more timestamp-ordered observation streams to completion.

        Streams are merged lazily so a paced replay source is consumed at its own rate.
        """
        merged = streams[0] if len(streams) == 1 else heapq.merge(*streams, key=_order_key)
        for observation in merged:
            self.ingest(observation)
        return self.finish()

    def ingest(self, observation: SensorObservation) -> bool:
        """Feed one observation, stepping fusion for every step boundary it crosses.

        Observations must arrive in timestamp order; this is the single entry point
        used by batch runs, paced replay and step-by-step replay alike.
        """
        if self._first_input_at is None:
            self._first_input_at = observation.timestamp
        if (
            self.scenario_id is not None
            and observation.scenario_id is not None
            and observation.scenario_id != self.scenario_id
        ):
            # A merged/misrouted stream must not leak another scenario's samples in.
            self.observations_rejected_foreign_scenario += 1
            return False
        if not self._sensor_matches(observation):
            self.observations_rejected_unknown_sensor += 1
            return False
        if not self._spatially_consistent(observation):
            self.observations_rejected_spatially_inconsistent += 1
            return False
        if self.dedup.is_duplicate(observation):
            # A pure peek: dropping a duplicate must never mutate the dedup cache or
            # advance fusion time, however stale or futuristic its timestamp is.
            self.dedup.dropped += 1
            return False
        if self._last_timestamp is not None and observation.timestamp < self._last_timestamp:
            # Fusion time never moves backwards; late samples are dropped, not replayed
            # (and never committed to the dedup cache, so a later replay of the same
            # late sample is still counted out-of-order, not duplicate).
            self.observations_out_of_order += 1
            return False
        self._last_timestamp = observation.timestamp
        self._advance_to(observation.timestamp)
        self.dedup.commit(observation)
        self._ensure_header(observation.timestamp)
        self._flush_scheduled(observation.timestamp)
        self._record(
            observation.timestamp,
            EntryKind.OBSERVATION,
            observation.model_dump(mode="json"),
            source_type=observation.source_type,
            sensor_id=observation.sensor_id,
        )
        self.fusion.ingest(observation)
        self._ingested_since_step = True
        return True

    def _spatially_consistent(self, observation: SensorObservation) -> bool:
        """A localized read's coordinate must fall inside its own claimed zone.

        Zone-only evidence (no coordinate) and observations with no zone at all
        (e.g. person observations) are unaffected; an unknown zone id paired with a
        coordinate is rejected rather than silently ignored.
        """
        zone_id = getattr(observation, "zone_id", None)
        if zone_id is None or observation.coordinate is None:
            return True
        zone = self.registry.zone_or_none(zone_id)
        if zone is None:
            return False
        return zone.bounds.contains(observation.coordinate)

    def _lost_at_an_exit(self, track_id: str) -> bool:
        """The shopper's track stopped inside an exit boundary (not merely anywhere)."""
        person = self.fusion.persons.get(track_id)
        if person is None or person.state == PersonTrackState.ACTIVE:
            return False
        return self.registry.in_exit_boundary(person.position)

    def _sensor_matches(self, observation: SensorObservation) -> bool:
        """The observation's sensor must exist in the twin with the same modality."""
        try:
            sensor = self.registry.sensor(observation.sensor_id)
        except KeyError:
            return False
        return sensor.modality == observation.source_type

    def finish(self, *, advance: bool = True) -> PipelineResult:
        """Finalize the run and return the result.

        By default one last fusion step runs after the last observation. A driver that
        has already stepped the clock through the end of the run (``advance_to``) passes
        ``advance=False``: nothing is evaluated past the advertised duration, but input
        that arrived after the last step (a sample stamped exactly on the end, or a final
        partial interval) is still evaluated once, at the clock's current time.
        """
        if self._next_step is not None:
            if advance:
                self._step(self._next_step)
            elif self._last_timestamp is not None and (
                self._ingested_since_step
                or self._last_step_at is None
                or self._last_step_at < self._last_timestamp
            ):
                self._step(self._last_timestamp)
            self._next_step = None
        stamp = self._scheduled[0][0] if self._scheduled else self._first_input_at
        if stamp is not None:
            self._ensure_header(stamp)
        self._flush_scheduled(None)
        if self._last_timestamp is not None:
            self._close_carts_of_ended_sessions(self._last_timestamp)
        # A shopper whose track was lost at the door never reaches a terminal session;
        # a cart that holds an exit candidate line is frozen at the end only when that
        # shopper is no longer observed, never while they are still inside the store.
        self.cart.close_carts_with_exit_candidates(
            shopper_gone=self._lost_at_an_exit,
        )
        return self.result()

    def _ensure_header(self, timestamp: datetime) -> None:
        """Write the twin and the effective configuration once, before anything else.

        A replay must never guess which store the coordinates refer to nor which
        thresholds produced the recorded decisions. The header is stamped no later
        than the first entry so the file stays chronological.
        """
        if self._twin_recorded:
            return
        first = timestamp
        if self._scheduled and self._scheduled[0][0] < first:
            first = self._scheduled[0][0]
        self._record(first, EntryKind.STORE_TWIN, self.registry.store.model_dump(mode="json"))
        self._record(first, EntryKind.PIPELINE_CONFIG, self.config.model_dump(mode="json"))
        self._twin_recorded = True

    def advance_to(self, timestamp: datetime) -> None:
        """Run fusion steps up to ``timestamp`` without any observation.

        Lets an interactive driver (replay console, tests) move simulated time through
        an observation-free interval such as a sensor dropout. Time never moves backwards.
        """
        if self._last_timestamp is not None and timestamp < self._last_timestamp:
            return
        self._last_timestamp = timestamp
        self._advance_to(timestamp)

    def _advance_to(self, timestamp: datetime) -> None:
        step = timedelta(seconds=self.config.step_interval_s)
        if self._next_step is None:
            self._next_step = timestamp + step
            return
        while timestamp >= self._next_step:
            self._step(self._next_step)
            self._next_step = self._next_step + step

    # ------------------------------------------------------------------ stepping
    def _step(self, now: datetime) -> None:
        self._steps += 1
        self._last_step_at = now
        self._ingested_since_step = False
        self._flush_scheduled(now)
        proposed_now: set[str] = set()
        for event in self.fusion.step(now):
            proposed_now.add(event.event_id)
            self._proposed.append(event)
            self._decide(event, now, proposed=True)
        # One-shot proposals (CARRY, PUTBACK, MISPLACE, EXIT_WITH_ITEM) are not re-proposed by
        # fusion; keep re-evaluating them while the confidence engine says WAIT so they
        # eventually COMMIT or expire into REVIEW instead of silently vanishing. A pending
        # proposal whose episode has since ended is dropped, never decided late.
        for event_id, event in list(self._pending.items()):
            if event_id in proposed_now:
                continue
            if not self.fusion.is_active(event):
                self._pending.pop(event_id, None)
                self.confidence.forget(event_id)
                continue
            self._decide(event, now, proposed=False)
        self._close_carts_of_ended_sessions(now)

    def _close_carts_of_ended_sessions(self, now: datetime) -> None:
        """A cart closes when its shopper's session ends (exit or abandonment), never
        because one EPC crossed the exit boundary while the shopper is still inside, and
        never because a *different*, later session of the same shopper track ended: a cart
        closes only for the session it was actually committed under (``_cart_session``).
        close_cart_by_id is idempotent, so the sweep is repeated every step."""
        for session in [*self.fusion.session_history, *self.fusion.sessions.values()]:
            cart_ids = [
                cart_id
                for cart_id, session_id in self._cart_session.items()
                if session_id == session.session_id
            ]
            if session.state == SessionState.EXITED:
                stamp = session.exited_at or now
                if cart_ids:
                    for cart_id in cart_ids:
                        self.cart.close_cart_by_id(cart_id, stamp)
                else:
                    # No cart was ever mapped to THIS session. The shopper's CURRENT cart
                    # may belong to a different (later, possibly still-active) session, so
                    # it may only be closed here when it, too, carries no mapping at all
                    # (a legacy cart from a caller that bypassed _decide() entirely).
                    cart = self.cart.state.current_cart(session.person_track_id)
                    if cart is not None and cart.cart_id not in self._cart_session:
                        self.cart.close_cart_by_id(cart.cart_id, stamp)
            elif session.state == SessionState.ABANDONED:
                # The track vanished; an item-level exit event, when consistent with the
                # cart, is the best evidence of when the merchandise left.
                fallback = session.exited_at or now
                if cart_ids:
                    for cart_id in cart_ids:
                        self.cart.close_cart_by_id(
                            cart_id, self.cart.exit_stamp_or_by_id(cart_id, fallback)
                        )
                else:
                    cart = self.cart.state.current_cart(session.person_track_id)
                    if cart is not None and cart.cart_id not in self._cart_session:
                        self.cart.close_cart_by_id(
                            cart.cart_id, self.cart.exit_stamp_or_by_id(cart.cart_id, fallback)
                        )

    def _track_cart_session(self, event: RetailEvent) -> None:
        """Remember which session a committed event's cart belongs to.

        Called once a cart-affecting event actually commits, for every shopper id the
        event names; a cart keeps its first mapping (a cart is not shared between
        sessions, so a later commit into the same cart must not overwrite it).
        """
        for track_id in (event.shopper_track_id, event.counterpart_track_id):
            if track_id is None:
                continue
            cart = self.cart.state.current_cart(track_id)
            if cart is None or cart.cart_id in self._cart_session:
                continue
            session = self.fusion.sessions.get(track_id)
            if session is not None:
                self._cart_session[cart.cart_id] = session.session_id

    def _decide(self, event: RetailEvent, now: datetime, proposed: bool) -> None:
        decision = self.confidence.decide(event, now)
        self._decisions.append(decision)
        if proposed:
            self._record(now, EntryKind.RETAIL_EVENT, event.model_dump(mode="json"))
        self._record(now, EntryKind.DECISION, decision.model_dump(mode="json"))
        if decision.decision == Decision.COMMIT:
            cart_event = self.cart.apply(event)
            self._committed.append(event)
            self._pending.pop(event.event_id, None)
            self.fusion.acknowledge(event, committed=True)
            self.confidence.forget(event.event_id)
            self._record(now, EntryKind.CART_EVENT, cart_event.model_dump(mode="json"))
            self._track_cart_session(event)
        elif decision.decision == Decision.REVIEW:
            self._review.append(event)
            self._pending.pop(event.event_id, None)
            self.fusion.acknowledge(event, committed=False)
            self.confidence.forget(event.event_id)
        else:
            self._pending[event.event_id] = event

    def result(self) -> PipelineResult:
        return PipelineResult(
            scenario_id=self.scenario_id,
            proposed_events=list(self._proposed),
            decisions=list(self._decisions),
            committed_events=list(self._committed),
            review_events=list(self._review),
            pending_events=list(self._pending.values()),
            cart_events=list(self.cart.cart_events),
            cart_state=self.cart.state.model_copy(deep=True),
            person_tracks=self.fusion.person_tracks(),
            item_tracks=self.fusion.item_tracks(),
            sessions=[*self.fusion.session_history, *self.fusion.sessions.values()],
            cart_sessions=dict(self._cart_session),
            observations_accepted=self.dedup.accepted,
            observations_dropped=self.dedup.dropped,
            observations_out_of_order=self.observations_out_of_order,
            observations_rejected_unknown_sensor=self.observations_rejected_unknown_sensor,
            observations_rejected_foreign_scenario=self.observations_rejected_foreign_scenario,
            observations_rejected_spatially_inconsistent=(
                self.observations_rejected_spatially_inconsistent
            ),
            observations_rejected_low_confidence=(
                self.fusion.persons.rejected_low_confidence
                + self.fusion.items.rejected_low_confidence
            ),
            steps=self._steps,
        )


def _order_key(observation: SensorObservation) -> tuple[datetime, str, str]:
    return (observation.timestamp, observation.source_type.value, observation.observation_id)


def merge_streams(*streams: Iterable[AnyObservation]) -> Iterator[AnyObservation]:
    """Lazily merge timestamp-ordered streams into one timestamp-ordered stream."""
    yield from heapq.merge(*streams, key=_order_key)
