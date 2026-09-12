"""Confidence decisions and their configuration."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from radiowave.contracts._base import ContractModel, FrozenModel, UnitInterval, UtcDatetime


class Decision(StrEnum):
    COMMIT = "COMMIT"
    WAIT = "WAIT"
    REVIEW = "REVIEW"


class ConfidenceThresholds(FrozenModel):
    """Single source of truth for confidence thresholds. No magic numbers elsewhere."""

    commit_min_confidence: UnitInterval = Field(
        default=0.75, description="Confidence at or above this may COMMIT"
    )
    wait_min_confidence: UnitInterval = Field(
        default=0.40, description="Confidence at or above this (but below commit) WAITs"
    )
    commit_min_margin: UnitInterval = Field(
        default=0.25, description="Minimum gap between best and runner-up candidate to COMMIT"
    )
    max_wait_seconds: float = Field(
        default=20.0,
        ge=0.0,
        description="After waiting this long an unresolved event goes to REVIEW",
    )
    review_grace_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description="Low confidence goes to REVIEW only after this grace period, so a new "
        "episode can still accumulate evidence",
    )

    @model_validator(mode="after")
    def _ordered(self) -> ConfidenceThresholds:
        if self.wait_min_confidence > self.commit_min_confidence:
            msg = "wait_min_confidence must not exceed commit_min_confidence"
            raise ValueError(msg)
        return self


class ConfidenceDecision(ContractModel):
    event_id: str
    decision: Decision
    confidence: UnitInterval
    margin: float
    evaluated_at: UtcDatetime
    waited_seconds: float = Field(default=0.0, ge=0.0)
    reason: str = ""
