"""Shopper (person) tracks, item tracks and shopper-item association candidates."""

from __future__ import annotations

from enum import StrEnum
from itertools import pairwise

from pydantic import Field, field_validator

from radiowave.contracts._base import ContractModel, FrozenModel, UnitInterval, UtcDatetime
from radiowave.contracts.geometry import SpatialUncertainty, Velocity, WorldCoordinate
from radiowave.contracts.store import EPC


class TrackPoint(FrozenModel):
    timestamp: UtcDatetime
    coordinate: WorldCoordinate
    velocity: Velocity | None = None


class PersonTrackState(StrEnum):
    ACTIVE = "ACTIVE"
    LOST = "LOST"
    ENDED = "ENDED"


class PersonTrack(ContractModel):
    """Canonical anonymous shopper track.

    ``track_id`` is assigned by fusion and is independent of any sensor's native
    track numbering. Several sensors (or re-acquisitions after dropout) can feed one track.
    """

    track_id: str = Field(min_length=1)
    state: PersonTrackState = PersonTrackState.ACTIVE
    created_at: UtcDatetime
    updated_at: UtcDatetime
    position: WorldCoordinate
    velocity: Velocity = Field(default_factory=Velocity)
    uncertainty: SpatialUncertainty
    confidence: UnitInterval
    observation_count: int = Field(default=0, ge=0)
    contributing_sensor_ids: list[str] = Field(default_factory=list)
    session_id: str | None = None
    history: list[TrackPoint] = Field(default_factory=list)


class ItemState(StrEnum):
    """Physical state of one EPC as inferred by the retail state machine."""

    ON_FIXTURE = "ON_FIXTURE"
    INTERACTION_CANDIDATE = "INTERACTION_CANDIDATE"
    CARRIED = "CARRIED"
    MISPLACED = "MISPLACED"
    EXITED = "EXITED"
    UNKNOWN = "UNKNOWN"


class ItemTrack(ContractModel):
    """Canonical track of one physical item, keyed by EPC."""

    epc: EPC
    gtin: str | None = None
    state: ItemState = ItemState.UNKNOWN
    state_since: UtcDatetime | None = None
    home_fixture_id: str | None = None
    position: WorldCoordinate | None = Field(
        default=None, description="Smoothed location estimate at configured RFID accuracy"
    )
    uncertainty: SpatialUncertainty | None = None
    zone_id: str | None = None
    rest_position: WorldCoordinate | None = Field(
        default=None, description="Where the item last came to rest"
    )
    movement_start_at: UtcDatetime | None = None
    last_seen_at: UtcDatetime | None = None
    carrier_track_id: str | None = Field(
        default=None, description="Committed shopper track carrying this item, if any"
    )
    observation_count: int = Field(default=0, ge=0)
    history: list[TrackPoint] = Field(default_factory=list)


class AssociationEvidence(FrozenModel):
    """Explainable per-feature scores for one shopper-item pairing.

    All scores are in [0, 1]; ``combined`` is the weighted mean using ``weights``.
    ``features`` carries the raw measurements the scores were derived from.
    """

    distance_score: UnitInterval
    distance_trend_score: UnitInterval
    velocity_score: UnitInterval
    temporal_score: UnitInterval
    co_motion_score: UnitInterval
    zone_score: UnitInterval
    vision_score: UnitInterval
    combined: UnitInterval
    weights: dict[str, float] = Field(default_factory=dict)
    features: dict[str, float] = Field(default_factory=dict)


class CandidateScore(FrozenModel):
    person_track_id: str
    score: UnitInterval
    evidence: AssociationEvidence


def validate_ranking(candidates: list[CandidateScore]) -> list[CandidateScore]:
    """A ranking lists each shopper once, in descending score order."""
    ids = [c.person_track_id for c in candidates]
    if len(ids) != len(set(ids)):
        msg = "candidate ranking lists a shopper more than once"
        raise ValueError(msg)
    scores = [c.score for c in candidates]
    if any(later > earlier for earlier, later in pairwise(scores)):
        msg = "candidate ranking must be sorted by descending score"
        raise ValueError(msg)
    return candidates


def ranking_margin(candidates: list[CandidateScore]) -> float:
    """Best minus runner-up score of a ranking; 1.0 with fewer than two candidates."""
    if len(candidates) < 2:
        return 1.0
    ordered = sorted((c.score for c in candidates), reverse=True)
    return ordered[0] - ordered[1]


class InteractionCandidate(ContractModel):
    """Current ranked shopper candidates for one moving item."""

    epc: EPC
    timestamp: UtcDatetime
    movement_start_at: UtcDatetime | None = None
    candidates: list[CandidateScore] = Field(default_factory=list)

    @field_validator("candidates")
    @classmethod
    def _ranked(cls, candidates: list[CandidateScore]) -> list[CandidateScore]:
        return validate_ranking(candidates)

    @property
    def top(self) -> CandidateScore | None:
        return self.candidates[0] if self.candidates else None

    @property
    def margin(self) -> float:
        """Score gap between best and second-best candidate (1.0 if fewer than two)."""
        return ranking_margin(self.candidates)
