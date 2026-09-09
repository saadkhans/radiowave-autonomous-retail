"""Retail events (fusion output) and cart events (cart mutations).

``RetailEvent.event_id`` is deterministic for a given physical episode so that a
replayed or re-proposed event is recognisable as the *same* event; cart
mutations key their idempotency on it.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from enum import StrEnum

from pydantic import Field

from radiowave.contracts._base import ContractModel, UnitInterval, UtcDatetime
from radiowave.contracts.store import EPC
from radiowave.contracts.tracks import CandidateScore


class RetailEventType(StrEnum):
    PICK = "PICK"
    CARRY = "CARRY"
    PUTBACK = "PUTBACK"
    MISPLACE = "MISPLACE"
    HANDOFF = "HANDOFF"
    EXIT_WITH_ITEM = "EXIT_WITH_ITEM"


class RetailEvent(ContractModel):
    """A shopper-item event proposed by fusion, with its full evidence."""

    event_id: str = Field(min_length=1)
    event_type: RetailEventType
    timestamp: UtcDatetime
    epc: EPC
    shopper_track_id: str | None = Field(
        default=None, description="Best-supported shopper for this event, if any"
    )
    counterpart_track_id: str | None = Field(
        default=None, description="Receiving shopper for HANDOFF"
    )
    fixture_id: str | None = None
    zone_id: str | None = None
    confidence: UnitInterval
    candidates: list[CandidateScore] = Field(
        default_factory=list, description="Ranked shopper candidates with evidence"
    )
    reason: str = Field(default="", description="Human-readable transition explanation")
    scenario_id: str | None = None

    @property
    def margin(self) -> float:
        """Gap between best and runner-up candidate; 1.0 when there is nothing to disambiguate."""
        if len(self.candidates) < 2:
            return 1.0
        return self.candidates[0].score - self.candidates[1].score


def make_event_id(
    event_type: RetailEventType,
    epc: EPC,
    episode_start: datetime,
    shopper_track_id: str | None = None,
) -> str:
    """Deterministic id: same episode (type, item, start time, shopper) -> same id."""
    key = f"{event_type}|{epc}|{episode_start.isoformat()}|{shopper_track_id or ''}"
    return f"evt_{hashlib.sha1(key.encode()).hexdigest()[:16]}"


class CartEventType(StrEnum):
    ADD = "ADD"
    REMOVE = "REMOVE"
    TRANSFER = "TRANSFER"
    UNRESOLVED = "UNRESOLVED"
    EXIT_HOLD = "EXIT_HOLD"
    NOOP = "NOOP"
    NOOP_DUPLICATE = "NOOP_DUPLICATE"


class CartEvent(ContractModel):
    """Result of applying one RetailEvent to the cart engine."""

    cart_event_id: str = Field(min_length=1)
    cart_event_type: CartEventType
    timestamp: UtcDatetime
    epc: EPC
    cart_id: str | None = None
    from_cart_id: str | None = None
    source_event_id: str
    note: str = ""
