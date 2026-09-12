"""Anonymous shopper sessions.

A session is the lifetime of one shopper track inside the store, from entry to
exit. It carries no identity, payment or biometric information; settlement
linkage is explicitly out of scope for Foundation v0.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from radiowave.contracts._base import ContractModel, UtcDatetime


class SessionState(StrEnum):
    ACTIVE = "ACTIVE"
    EXITED = "EXITED"
    ABANDONED = "ABANDONED"


class ShopperSession(ContractModel):
    session_id: str = Field(min_length=1)
    person_track_id: str = Field(min_length=1)
    state: SessionState = SessionState.ACTIVE
    entered_at: UtcDatetime
    exited_at: UtcDatetime | None = None
    entry_boundary_id: str | None = None
    exit_boundary_id: str | None = None
