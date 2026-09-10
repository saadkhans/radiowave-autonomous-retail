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
from radiowave.contracts.sessions import ShopperSession
from radiowave.contracts.store import SourceType
from radiowave.contracts.tracks import ItemTrack, PersonTrack
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import FusionConfig
from radiowave.fusion.interfaces import Recorder
from radiowave.ingestion.deduplication import ObservationDeduplicator


class PipelineConfig(FrozenModel):
    step_interval_s: float = Field(default=0.25, gt=0.0)
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
    observations_accepted: int = 0
    observations_dropped: int = 0
    observations_out_of_order: int = 0
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
        vision_enabled: bool = False,
        scenario_id: str | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.registry = registry
        self.scenario_id = scenario_id
        self.fusion = BaselineFusionEngine(
            registry, self.config.fusion, vision_enabled=vision_enabled, scenario_id=scenario_id
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
        self._sequence = 0
        self._scheduled: list[tuple[datetime, int, EntryKind, dict[str, Any]]] = []
        self._last_timestamp: datetime | None = None
        self._twin_recorded = False
        self.observations_out_of_order = 0

    # ------------------------------------------------------------------ recording
    def schedule_entry(self, timestamp: datetime, kind: EntryKind, payload: dict[str, Any]) -> None:
        """Queue a non-observation entry (e.g. ground truth) to be recorded in timestamp order."""
        heapq.heappush(self._scheduled, (timestamp, len(self._scheduled), kind, payload))

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
        if self._last_timestamp is not None and observation.timestamp < self._last_timestamp:
            # Fusion time never moves backwards; late samples are dropped, not replayed.
            self.observations_out_of_order += 1
            return False
        self._last_timestamp = observation.timestamp
        self._advance_to(observation.timestamp)
        if not self.dedup.accept(observation):
            return False
        if not self._twin_recorded:
            # The recording carries the twin it was produced against, so a replay never
            # has to guess which store the coordinates, zones and home fixtures refer to.
            # It is stamped no later than anything else so the file stays chronological.
            first = observation.timestamp
            if self._scheduled and self._scheduled[0][0] < first:
                first = self._scheduled[0][0]
            self._record(first, EntryKind.STORE_TWIN, self.registry.store.model_dump(mode="json"))
            self._twin_recorded = True
        self._flush_scheduled(observation.timestamp)
        self._record(
            observation.timestamp,
            EntryKind.OBSERVATION,
            observation.model_dump(mode="json"),
            source_type=observation.source_type,
            sensor_id=observation.sensor_id,
        )
        self.fusion.ingest(observation)
        return True

    def finish(self) -> PipelineResult:
        """Run one final fusion step after the last observation and return the result."""
        if self._next_step is not None:
            self._step(self._next_step)
            self._next_step = None
        self._flush_scheduled(None)
        return self.result()

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
            observations_accepted=self.dedup.accepted,
            observations_dropped=self.dedup.dropped,
            observations_out_of_order=self.observations_out_of_order,
            steps=self._steps,
        )


def _order_key(observation: SensorObservation) -> tuple[datetime, str, str]:
    return (observation.timestamp, observation.source_type.value, observation.observation_id)


def merge_streams(*streams: Iterable[AnyObservation]) -> Iterator[AnyObservation]:
    """Lazily merge timestamp-ordered streams into one timestamp-ordered stream."""
    yield from heapq.merge(*streams, key=_order_key)
