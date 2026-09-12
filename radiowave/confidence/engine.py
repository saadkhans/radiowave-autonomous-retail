"""Threshold-based confidence engine: COMMIT / WAIT / REVIEW.

All thresholds come from :class:`~radiowave.contracts.confidence.ConfidenceThresholds`.
The engine remembers when it first saw each event id so that WAIT can expire into
REVIEW after ``max_wait_seconds``.
"""

from __future__ import annotations

from datetime import datetime

from radiowave.contracts.confidence import ConfidenceDecision, ConfidenceThresholds, Decision
from radiowave.contracts.events import RetailEvent


class ThresholdConfidenceEngine:
    def __init__(self, thresholds: ConfidenceThresholds | None = None) -> None:
        self.thresholds = thresholds or ConfidenceThresholds()
        self._first_seen: dict[str, datetime] = {}

    def decide(self, event: RetailEvent, now: datetime) -> ConfidenceDecision:
        t = self.thresholds
        first = self._first_seen.setdefault(event.event_id, now)
        waited = max(0.0, (now - first).total_seconds())
        margin = event.margin
        if event.confidence >= t.commit_min_confidence and margin >= t.commit_min_margin:
            decision, reason = (
                Decision.COMMIT,
                (
                    f"confidence {event.confidence:.2f} >= {t.commit_min_confidence} and "
                    f"margin {margin:.2f} >= {t.commit_min_margin}"
                ),
            )
        elif waited > t.max_wait_seconds:
            decision, reason = (
                Decision.REVIEW,
                (
                    f"waited {waited:.1f} s > {t.max_wait_seconds} s without reaching commit "
                    f"thresholds "
                    f"(confidence {event.confidence:.2f}, margin {margin:.2f})"
                ),
            )
        elif event.confidence >= t.wait_min_confidence or waited <= t.review_grace_seconds:
            decision, reason = (
                Decision.WAIT,
                (
                    f"confidence {event.confidence:.2f} below commit or margin {margin:.2f} "
                    f"< {t.commit_min_margin}; gathering more evidence ({waited:.1f} s)"
                ),
            )
        else:
            decision, reason = (
                Decision.REVIEW,
                (
                    f"confidence {event.confidence:.2f} < {t.wait_min_confidence} after "
                    f"{waited:.1f} s grace; unresolved"
                ),
            )
        return ConfidenceDecision(
            event_id=event.event_id,
            decision=decision,
            confidence=event.confidence,
            margin=margin,
            evaluated_at=now,
            waited_seconds=waited,
            reason=reason,
        )

    def forget(self, event_id: str) -> None:
        self._first_seen.pop(event_id, None)
