from __future__ import annotations

import pytest

from radiowave.confidence.engine import ThresholdConfidenceEngine
from radiowave.contracts import (
    EPC,
    ConfidenceThresholds,
    Decision,
    RetailEvent,
    RetailEventType,
)
from radiowave.contracts.tracks import AssociationEvidence, CandidateScore
from radiowave.simulator.stores import EPC_SHIRT_A
from tests.conftest import at

THRESHOLDS = ConfidenceThresholds(
    commit_min_confidence=0.75,
    wait_min_confidence=0.4,
    commit_min_margin=0.25,
    max_wait_seconds=5.0,
    review_grace_seconds=1.0,
)


def _cand(track: str, score: float) -> CandidateScore:
    ev = AssociationEvidence(
        distance_score=score,
        distance_trend_score=score,
        velocity_score=score,
        temporal_score=score,
        co_motion_score=score,
        zone_score=score,
        vision_score=0.5,
        combined=score,
    )
    return CandidateScore(person_track_id=track, score=score, evidence=ev)


def _event(confidence: float, scores: list[float], event_id: str = "evt_x") -> RetailEvent:
    return RetailEvent(
        event_id=event_id,
        event_type=RetailEventType.PICK,
        timestamp=at(0),
        confidence=confidence,
        epc=EPC(value=EPC_SHIRT_A),
        candidates=[_cand(f"P{i}", s) for i, s in enumerate(scores)],
    )


def test_high_confidence_with_clear_margin_commits() -> None:
    engine = ThresholdConfidenceEngine(THRESHOLDS)
    decision = engine.decide(_event(0.9, [0.9, 0.3]), at(0))
    assert decision.decision == Decision.COMMIT
    assert decision.margin == pytest.approx(0.6)


def test_ambiguous_candidates_wait_then_review_after_max_wait() -> None:
    engine = ThresholdConfidenceEngine(THRESHOLDS)
    event = _event(0.9, [0.9, 0.8])
    assert engine.decide(event, at(0)).decision == Decision.WAIT
    assert engine.decide(event, at(4.9)).decision == Decision.WAIT
    late = engine.decide(event, at(5.1))
    assert late.decision == Decision.REVIEW
    assert late.waited_seconds == 5.1


def test_medium_confidence_waits_and_low_confidence_reviews_after_grace() -> None:
    engine = ThresholdConfidenceEngine(THRESHOLDS)
    assert engine.decide(_event(0.5, [0.5], "medium"), at(0)).decision == Decision.WAIT
    low = _event(0.2, [0.2], "low")
    assert engine.decide(low, at(0)).decision == Decision.WAIT  # inside grace period
    assert engine.decide(low, at(1.5)).decision == Decision.REVIEW


def test_single_candidate_needs_only_confidence() -> None:
    engine = ThresholdConfidenceEngine(THRESHOLDS)
    assert engine.decide(_event(0.8, [0.8]), at(0)).decision == Decision.COMMIT
    assert engine.decide(_event(0.8, []), at(0)).decision == Decision.COMMIT


def test_thresholds_are_configuration_not_constants() -> None:
    lenient = ThresholdConfidenceEngine(
        ConfidenceThresholds(
            commit_min_confidence=0.5, wait_min_confidence=0.2, commit_min_margin=0.05
        )
    )
    assert lenient.decide(_event(0.55, [0.55, 0.45]), at(0)).decision == Decision.COMMIT
    strict = ThresholdConfidenceEngine(THRESHOLDS)
    assert strict.decide(_event(0.55, [0.55, 0.45]), at(0)).decision == Decision.WAIT
