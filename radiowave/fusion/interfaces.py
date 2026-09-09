"""Core interfaces. Structural (Protocol) so implementations need no base class.

None of these interfaces depend on a message bus, database, or vendor SDK.
Streams are plain iterators of normalized contracts; state is queried via methods.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Protocol, runtime_checkable

from radiowave.contracts.confidence import ConfidenceDecision
from radiowave.contracts.events import CartEvent, RetailEvent
from radiowave.contracts.observations import (
    ItemObservation,
    PersonObservation,
    SensorObservation,
    VisionEvidence,
)
from radiowave.contracts.recording import RecordedEntry
from radiowave.contracts.store import EPC
from radiowave.contracts.tracks import InteractionCandidate, ItemTrack, PersonTrack


@runtime_checkable
class PeopleTracker(Protocol):
    """Yields normalized, store-frame person observations in timestamp order."""

    def observations(self) -> Iterator[PersonObservation]: ...


@runtime_checkable
class ItemTracker(Protocol):
    """Yields normalized item (EPC) observations in timestamp order."""

    def observations(self) -> Iterator[ItemObservation]: ...


@runtime_checkable
class VisionEvidenceProvider(Protocol):
    """Yields semantic vision evidence in timestamp order (may be empty)."""

    def evidence(self) -> Iterator[VisionEvidence]: ...


@runtime_checkable
class FusionEngine(Protocol):
    """Consumes normalized observations, maintains tracks, proposes retail events."""

    def ingest(self, observation: SensorObservation) -> None: ...

    def step(self, now: datetime) -> list[RetailEvent]: ...

    def person_tracks(self) -> list[PersonTrack]: ...

    def item_tracks(self) -> list[ItemTrack]: ...

    def candidates(self, epc: EPC) -> InteractionCandidate | None: ...

    def acknowledge(self, event: RetailEvent, committed: bool) -> None:
        """Feedback from the confidence/cart layer about a proposed event."""
        ...


@runtime_checkable
class ConfidenceEngine(Protocol):
    def decide(self, event: RetailEvent, now: datetime) -> ConfidenceDecision: ...


@runtime_checkable
class CartEngine(Protocol):
    def apply(self, event: RetailEvent) -> CartEvent: ...


@runtime_checkable
class Recorder(Protocol):
    def record(self, entry: RecordedEntry) -> None: ...

    def close(self) -> None: ...


@runtime_checkable
class ReplaySource(Protocol):
    def entries(self) -> Iterator[RecordedEntry]: ...
