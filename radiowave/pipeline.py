"""End-to-end Foundation v0 pipeline: observations -> fusion -> confidence -> cart.

The pipeline is deterministic: observations are consumed in (timestamp, id)
order, fusion is stepped on a fixed simulated interval, and no wall-clock time
is involved. The same recording therefore always yields the same result.
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Iterator
from datetime import datetime, timedelta

from pydantic import Field

from radiowave.cart.engine import InMemoryCartEngine
from radiowave.cart.models import CartState
from radiowave.confidence.engine import ThresholdConfidenceEngine
from radiowave.contracts._base import ContractModel, FrozenModel
from radiowave.contracts.confidence import ConfidenceDecision, ConfidenceThresholds, Decision
from radiowave.contracts.events import CartEvent, RetailEvent
from radiowave.contracts.observations import AnyObservation, SensorObservation
from radiowave.contracts.recording import EntryKind
from radiowave.contracts.sessions import ShopperSession
from radiowave.contracts.tracks import ItemTrack, PersonTrack
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.baseline import BaselineFusionEngine
from radiowave.fusion.config import FusionConfig
from radiowave.ingestion.deduplication import ObservationDeduplicator
from radiowave.replay.recorder import InMemoryRecorder


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
    cart_events: list[CartEvent] = Field(default_factory=list)
    cart_state: CartState = Field(default_factory=CartState)
    person_tracks: list[PersonTrack] = Field(default_factory=list)
    item_tracks: list[ItemTrack] = Field(default_factory=list)
    sessions: list[ShopperSession] = Field(default_factory=list)
    observations_accepted: int = 0
    observations_dropped: int = 0
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
        recorder: InMemoryRecorder | None = None,
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
        self._steps = 0

    # ------------------------------------------------------------------
    def run(self, *streams: Iterable[AnyObservation]) -> PipelineResult:
        """Consume one or more timestamp-ordered observation streams to completion."""
        merged = heapq.merge(*(list(s) for s in streams), key=_order_key)
        step = timedelta(seconds=self.config.step_interval_s)
        next_step: datetime | None = None
        last_timestamp: datetime | None = None
        for observation in merged:
            if next_step is None:
                next_step = observation.timestamp + step
            while next_step is not None and observation.timestamp >= next_step:
                self._step(next_step)
                next_step = next_step + step
            self.ingest(observation)
            last_timestamp = observation.timestamp
        if last_timestamp is not None and next_step is not None:
            self._step(next_step)
        return self.result()

    def ingest(self, observation: SensorObservation) -> bool:
        if not self.dedup.accept(observation):
            return False
        if self.recorder is not None:
            self.recorder.record_observation(observation, self.scenario_id)
        self.fusion.ingest(observation)
        return True

    def _step(self, now: datetime) -> None:
        self._steps += 1
        for event in self.fusion.step(now):
            self._proposed.append(event)
            decision = self.confidence.decide(event, now)
            self._decisions.append(decision)
            if self.recorder is not None:
                self.recorder.record_payload(
                    EntryKind.RETAIL_EVENT, now, event.model_dump(mode="json"), self.scenario_id
                )
                self.recorder.record_payload(
                    EntryKind.DECISION, now, decision.model_dump(mode="json"), self.scenario_id
                )
            if decision.decision == Decision.COMMIT:
                cart_event = self.cart.apply(event)
                self._committed.append(event)
                self.fusion.acknowledge(event, committed=True)
                self.confidence.forget(event.event_id)
                if self.recorder is not None:
                    self.recorder.record_payload(
                        EntryKind.CART_EVENT,
                        now,
                        cart_event.model_dump(mode="json"),
                        self.scenario_id,
                    )
            elif decision.decision == Decision.REVIEW:
                self._review.append(event)
                self.fusion.acknowledge(event, committed=False)
                self.confidence.forget(event.event_id)

    def result(self) -> PipelineResult:
        return PipelineResult(
            scenario_id=self.scenario_id,
            proposed_events=list(self._proposed),
            decisions=list(self._decisions),
            committed_events=list(self._committed),
            review_events=list(self._review),
            cart_events=list(self.cart.cart_events),
            cart_state=self.cart.state,
            person_tracks=self.fusion.person_tracks(),
            item_tracks=self.fusion.item_tracks(),
            sessions=list(self.fusion.sessions.values()),
            observations_accepted=self.dedup.accepted,
            observations_dropped=self.dedup.dropped,
            steps=self._steps,
        )


def _order_key(observation: SensorObservation) -> tuple[datetime, str, str]:
    return (observation.timestamp, observation.source_type.value, observation.observation_id)


def merge_streams(*streams: Iterable[AnyObservation]) -> Iterator[AnyObservation]:
    yield from heapq.merge(*(list(s) for s in streams), key=_order_key)
