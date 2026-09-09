"""Shared Pydantic configuration and validators for canonical contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field


def ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes and normalize aware ones to UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "timestamps must be timezone-aware (UTC)"
        raise ValueError(msg)
    return value.astimezone(UTC)


UtcDatetime = Annotated[datetime, AfterValidator(ensure_utc)]
UnitInterval = Annotated[float, Field(ge=0.0, le=1.0)]
NonNegativeFloat = Annotated[float, Field(ge=0.0)]


class FrozenModel(BaseModel):
    """Immutable value object; hashable so it can be used as a dict key."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ContractModel(BaseModel):
    """Record model; unknown fields are rejected and assignments are validated."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)
