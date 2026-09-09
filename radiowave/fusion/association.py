"""Explainable shopper-item association.

For every (item, shopper) pair the scorer produces an
:class:`~radiowave.contracts.tracks.AssociationEvidence` with one score per
feature and the weighted combination. A :class:`CandidateLedger` accumulates
those scores over time (EMA) so a single noisy step cannot commit an assignment
and so that several shoppers remain ranked side by side.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime

from radiowave.contracts.geometry import Velocity, WorldCoordinate
from radiowave.contracts.observations import VisionEvidence, VisionEvidenceKind
from radiowave.contracts.store import EPC
from radiowave.contracts.tracks import AssociationEvidence, CandidateScore, InteractionCandidate
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.config import AssociationConfig, ItemTrackingConfig
from radiowave.fusion.tracking import ItemTrackState, PersonState

_INTERACTION_KINDS = {VisionEvidenceKind.REACH_INTO_FIXTURE, VisionEvidenceKind.ITEM_IN_HAND}


def _seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def _gaussian_score(distance: float, scale: float) -> float:
    return math.exp(-((distance / scale) ** 2))


@dataclass
class PairState:
    """Per (item, shopper) memory used by the ledger and the co-motion feature."""

    score: float = 0.0
    evidence: AssociationEvidence | None = None
    co_motion: deque[bool] = field(default_factory=deque)
    distance_history: deque[tuple[datetime, float]] = field(default_factory=deque)
    last_seen: datetime | None = None
    start_resolved_for: datetime | None = None
    start_person_position: WorldCoordinate | None = None

    def resolve_start(self, item: ItemTrackState, person: PersonState) -> None:
        """Cache where the shopper was when the item started moving (once per episode)."""
        if item.movement_start_at is None or self.start_resolved_for == item.movement_start_at:
            return
        self.start_resolved_for = item.movement_start_at
        self.start_person_position = person.position_at(item.movement_start_at)


class AssociationScorer:
    def __init__(
        self,
        config: AssociationConfig,
        item_config: ItemTrackingConfig,
        registry: StoreRegistry,
        vision_enabled: bool,
    ) -> None:
        self._cfg = config
        self._item_cfg = item_config
        self._registry = registry
        self._vision_enabled = vision_enabled

    def score(
        self,
        item: ItemTrackState,
        person: PersonState,
        pair: PairState,
        now: datetime,
        vision: list[VisionEvidence],
        vision_owner: dict[str, str],
        carrier_committed: bool = False,
    ) -> AssociationEvidence:
        """Score one pair.

        ``vision_owner`` maps a vision observation id to the person track it was
        assigned to (nearest person); ``carrier_committed`` selects the
        carry-time weight profile.
        """
        cfg = self._cfg
        profile = cfg.carry_weights if carrier_committed else cfg.pick_weights
        assert item.position is not None
        pair.resolve_start(item, person)
        distance = item.position.horizontal_distance_to(person.position)
        item_velocity = item.velocity(self._item_cfg.velocity_window_s)
        item_moving = item_velocity.horizontal_speed >= cfg.item_moving_speed_m_s
        person_moving = person.velocity.horizontal_speed >= cfg.moving_speed_m_s

        distance_score = _gaussian_score(distance, cfg.distance_scale_m)
        distance_trend_score = self._distance_trend(pair, distance, now, item_moving)
        velocity_score = self._velocity_similarity(item_velocity, person.velocity, item_moving)
        temporal_score, start_distance = self._temporal(item, pair)
        co_motion_score = self._co_motion(pair, distance, item_moving, person_moving)
        zone_score = self._zone(item, person, pair)
        vision_score, vision_hits = self._vision(item, person, vision, vision_owner)

        scores = {
            "distance": distance_score,
            "distance_trend": distance_trend_score,
            "velocity": velocity_score,
            "temporal": temporal_score,
            "co_motion": co_motion_score,
            "zone": zone_score,
        }
        weights = {k: v for k, v in profile.items() if k in scores}
        if self._vision_enabled and "vision" in profile:
            scores["vision"] = vision_score
            weights["vision"] = profile["vision"]
        total = sum(weights.values())
        combined = sum(scores[k] * w for k, w in weights.items()) / total if total else 0.0
        return AssociationEvidence(
            distance_score=distance_score,
            distance_trend_score=distance_trend_score,
            velocity_score=velocity_score,
            temporal_score=temporal_score,
            co_motion_score=co_motion_score,
            zone_score=zone_score,
            vision_score=vision_score,
            combined=_clamp(combined),
            weights=weights,
            features={
                "distance_m": distance,
                "item_speed_m_s": item_velocity.horizontal_speed,
                "person_speed_m_s": person.velocity.horizontal_speed,
                "distance_at_movement_start_m": start_distance,
                "vision_hits": float(vision_hits),
            },
        )

    # --- features -----------------------------------------------------------
    def _distance_trend(
        self, pair: PairState, distance: float, now: datetime, item_moving: bool
    ) -> float:
        pair.distance_history.append((now, distance))
        while len(pair.distance_history) > self._cfg.co_motion_window_steps:
            pair.distance_history.popleft()
        if not item_moving or len(pair.distance_history) < 2:
            return 0.5
        earliest = pair.distance_history[0][1]
        return _clamp(1.0 - abs(distance - earliest) / self._cfg.distance_scale_m)

    @staticmethod
    def _velocity_similarity(item: Velocity, person: Velocity, item_moving: bool) -> float:
        if not item_moving:
            return 0.5
        si, sp = item.horizontal_speed, person.horizontal_speed
        if sp <= 1e-6:
            return 0.0
        cosine = (item.vx * person.vx + item.vy * person.vy) / (si * sp)
        magnitude = 1.0 - min(1.0, abs(si - sp) / max(si, sp))
        return _clamp(max(0.0, cosine) * magnitude)

    def _temporal(self, item: ItemTrackState, pair: PairState) -> tuple[float, float]:
        """How close was this person to the item when it started moving?"""
        if item.movement_start_at is None or item.rest_position is None:
            return 0.5, float("nan")
        person_then = pair.start_person_position
        if person_then is None:
            return 0.0, float("nan")  # track did not exist yet when the item started moving
        distance = person_then.horizontal_distance_to(item.rest_position)
        return _gaussian_score(distance, self._cfg.distance_scale_m), distance

    def _co_motion(
        self, pair: PairState, distance: float, item_moving: bool, person_moving: bool
    ) -> float:
        together = distance <= self._cfg.co_motion_radius_m and item_moving and person_moving
        pair.co_motion.append(together)
        while len(pair.co_motion) > self._cfg.co_motion_window_steps:
            pair.co_motion.popleft()
        return sum(pair.co_motion) / len(pair.co_motion)

    def _zone(self, item: ItemTrackState, person: PersonState, pair: PairState) -> float:
        """Was the shopper at the departure fixture / zone when the item left it?"""
        if item.rest_position is None:
            return 0.5
        person_then = pair.start_person_position or person.position
        fixture = self._registry.fixture_at(
            item.rest_position, margin=self._cfg.fixture_proximity_m
        )
        if fixture is not None:
            if fixture.bounds.distance_to(person_then) <= self._cfg.fixture_proximity_m:
                return 1.0
            zone = self._registry.zone(fixture.zone_id)
            return 0.5 if zone.bounds.contains(person_then) else 0.0
        item_zone = self._registry.zone_at(item.rest_position)
        person_zone = self._registry.zone_at(person_then)
        if item_zone is None or person_zone is None:
            return 0.5
        return 1.0 if item_zone.zone_id == person_zone.zone_id else 0.0

    def _vision(
        self,
        item: ItemTrackState,
        person: PersonState,
        vision: list[VisionEvidence],
        vision_owner: dict[str, str],
    ) -> tuple[float, int]:
        """1.0 if interaction evidence at the departure point was assigned to this person,
        0.1 if it was assigned to someone else, 0.5 when there is no evidence."""
        if not self._vision_enabled or item.movement_start_at is None:
            return 0.5, 0
        relevant = [
            v
            for v in vision
            if v.kind in _INTERACTION_KINDS
            and abs(_seconds(v.timestamp, item.movement_start_at)) <= self._cfg.vision_window_s
            and item.rest_position is not None
            and v.coordinate.horizontal_distance_to(item.rest_position)
            <= self._cfg.candidate_radius_m
        ]
        if not relevant:
            return 0.5, 0
        hits = sum(1 for v in relevant if vision_owner.get(v.observation_id) == person.track_id)
        if hits:
            return 1.0, hits
        return 0.1, 0


def assign_vision_to_nearest(
    vision: list[VisionEvidence], persons: list[PersonState], radius_m: float
) -> dict[str, str]:
    """Exclusive assignment: each evidence goes to the single nearest person within radius."""
    owner: dict[str, str] = {}
    for evidence in vision:
        best_id: str | None = None
        best_distance = radius_m
        for person in persons:
            position = person.position_at(evidence.timestamp)
            if position is None:
                continue  # track did not exist yet; it cannot own earlier evidence
            distance = position.horizontal_distance_to(evidence.coordinate)
            if distance <= best_distance:
                best_id, best_distance = person.track_id, distance
        if best_id is not None:
            owner[evidence.observation_id] = best_id
    return owner


class CandidateLedger:
    """Ranked shopper candidates per item, smoothed over time."""

    def __init__(self, config: AssociationConfig) -> None:
        self._cfg = config
        self._pairs: dict[EPC, dict[str, PairState]] = {}

    def pair(self, epc: EPC, person_id: str) -> PairState:
        return self._pairs.setdefault(epc, {}).setdefault(person_id, PairState())

    def persons(self, epc: EPC) -> list[str]:
        return list(self._pairs.get(epc, {}))

    def update(
        self, epc: EPC, person_id: str, evidence: AssociationEvidence, now: datetime
    ) -> None:
        pair = self.pair(epc, person_id)
        a = self._cfg.ledger_alpha
        pair.score = pair.score * (1 - a) + evidence.combined * a
        pair.evidence = evidence
        pair.last_seen = now

    def prune(self, epc: EPC, person_id: str, distance: float) -> None:
        pairs = self._pairs.get(epc, {})
        pair = pairs.get(person_id)
        if pair and pair.score < self._cfg.prune_score and distance > self._cfg.prune_distance_m:
            del pairs[person_id]

    def drop(self, epc: EPC, person_id: str) -> None:
        """Forget a shopper that can no longer be a candidate (track ended)."""
        self._pairs.get(epc, {}).pop(person_id, None)

    def reset(self, epc: EPC) -> None:
        self._pairs.pop(epc, None)

    def ranking(
        self, epc: EPC, now: datetime, movement_start_at: datetime | None
    ) -> InteractionCandidate:
        pairs = self._pairs.get(epc, {})
        candidates = [
            CandidateScore(person_track_id=pid, score=_clamp(p.score), evidence=p.evidence)
            for pid, p in pairs.items()
            if p.evidence is not None
        ]
        candidates.sort(key=lambda c: (-c.score, c.person_track_id))
        return InteractionCandidate(
            epc=epc, timestamp=now, movement_start_at=movement_start_at, candidates=candidates
        )

    def score_of(self, epc: EPC, person_id: str) -> float:
        pair = self._pairs.get(epc, {}).get(person_id)
        return pair.score if pair else 0.0


def within_candidate_radius(
    item_position: WorldCoordinate, person: PersonState, radius: float
) -> bool:
    return item_position.horizontal_distance_to(person.position) <= radius
