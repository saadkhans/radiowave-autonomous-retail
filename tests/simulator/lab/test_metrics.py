"""Scoring: predicted vs ground truth on a finished ``LabRunResult``.

Most tests here build a minimal, hand-constructed ``LabRunResult`` (no real
sensors, no real fusion) so the matching arithmetic can be pinned exactly.
The final section runs ``score_lab_run`` against the real catalog and asserts
only structural properties -- see its docstring for why baseline accuracy
numbers are never pinned.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from radiowave.cart.models import CartState
from radiowave.contracts.confidence import ConfidenceDecision, Decision
from radiowave.contracts.events import RetailEvent, RetailEventType
from radiowave.contracts.store import EPC
from radiowave.contracts.tracks import ItemTrack
from radiowave.pipeline import PipelineResult
from radiowave.simulator.lab.engine import LabRunResult, run_lab_scenario
from radiowave.simulator.lab.ground_truth import (
    GroundTruthEvent,
    GroundTruthEventType,
    GroundTruthLog,
)
from radiowave.simulator.lab.metrics import MatchPolicy, score_lab_run
from radiowave.simulator.lab.scenarios import load_scenario

_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


def _t(seconds: float) -> datetime:
    return _EPOCH + timedelta(seconds=seconds)


def _epc(value: str) -> EPC:
    return EPC(value=value)


def _retail_event(
    event_id: str,
    event_type: RetailEventType,
    epc: str,
    seconds: float,
    *,
    shopper_track_id: str | None = None,
    confidence: float = 0.9,
) -> RetailEvent:
    return RetailEvent(
        event_id=event_id,
        event_type=event_type,
        timestamp=_t(seconds),
        epc=_epc(epc),
        shopper_track_id=shopper_track_id,
        confidence=confidence,
    )


def _truth_event(
    event_type: GroundTruthEventType,
    epc: str,
    seconds: float,
    *,
    person: str | None = "GT-PERSON-001",
) -> GroundTruthEvent:
    return GroundTruthEvent(
        tick=int(seconds * 20),
        timestamp=_t(seconds),
        event_type=event_type,
        ground_truth_person_id=person,
        epc=epc,
    )


def _run_result(
    *,
    proposed: list[RetailEvent] | None = None,
    committed: list[RetailEvent] | None = None,
    decisions: list[ConfidenceDecision] | None = None,
    item_tracks: list[ItemTrack] | None = None,
    truth_events: list[GroundTruthEvent] | None = None,
    cart_state: CartState | None = None,
    scenario_id: str = "synthetic",
) -> LabRunResult:
    pipeline = PipelineResult(
        scenario_id=scenario_id,
        proposed_events=proposed or [],
        committed_events=committed or [],
        decisions=decisions or [],
        item_tracks=item_tracks or [],
        cart_state=cart_state or CartState(),
    )
    ground_truth = GroundTruthLog(events=truth_events or [])
    return LabRunResult(
        scenario_id=scenario_id,
        seed=1,
        ticks=200,
        pipeline=pipeline,
        ground_truth=ground_truth,
    )


# --------------------------------------------------------------------------- #
# Hand-built precision/recall
# --------------------------------------------------------------------------- #


def test_hand_built_case_has_exact_precision_and_recall() -> None:
    """One TP, one FP, one FN, spread over two EPCs -- pinned exactly.

    Truth: PICK epc1 @10s (found), PICK epc2 @20s (missed entirely).
    Predicted: PICK epc1 @10.5s (matches epc1's truth), PICK epc3 @15s (no truth
    at all for epc3 -- a pure false positive).
    """
    result = _run_result(
        proposed=[
            _retail_event("evt-1", RetailEventType.PICK, "0000000000000001", 10.5),
            _retail_event("evt-2", RetailEventType.PICK, "0000000000000003", 15.0),
        ],
        truth_events=[
            _truth_event(GroundTruthEventType.PICK, "0000000000000001", 10.0),
            _truth_event(GroundTruthEventType.PICK, "0000000000000002", 20.0),
        ],
    )
    report = score_lab_run(result, MatchPolicy(tolerance_s=2.0))
    pick = report.event_type(RetailEventType.PICK)

    assert pick.true_positives == 1
    assert pick.false_positives == 1
    assert pick.false_negatives == 1
    assert pick.precision == pytest.approx(0.5)
    assert pick.recall == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# Tolerance boundary
# --------------------------------------------------------------------------- #


def test_an_event_exactly_at_the_tolerance_boundary_matches() -> None:
    policy = MatchPolicy(tolerance_s=2.0)
    result = _run_result(
        proposed=[_retail_event("evt-1", RetailEventType.PICK, "0000000000000001", 12.0)],
        truth_events=[_truth_event(GroundTruthEventType.PICK, "0000000000000001", 10.0)],
    )
    pick = score_lab_run(result, policy).event_type(RetailEventType.PICK)
    assert pick.true_positives == 1
    assert pick.false_positives == 0
    assert pick.false_negatives == 0


def test_an_event_just_past_the_tolerance_boundary_does_not_match() -> None:
    policy = MatchPolicy(tolerance_s=2.0)
    result = _run_result(
        proposed=[_retail_event("evt-1", RetailEventType.PICK, "0000000000000001", 12.001)],
        truth_events=[_truth_event(GroundTruthEventType.PICK, "0000000000000001", 10.0)],
    )
    pick = score_lab_run(result, policy).event_type(RetailEventType.PICK)
    assert pick.true_positives == 0
    assert pick.false_positives == 1
    assert pick.false_negatives == 1


# --------------------------------------------------------------------------- #
# Greedy one-to-one matching
# --------------------------------------------------------------------------- #


def test_greedy_matching_is_genuinely_one_to_one() -> None:
    """Ten predicted PICKs of the same EPC, all within tolerance of one true PICK,
    must produce ONE true positive and NINE false positives -- not ten true
    positives. This is the property that would hide the baseline's known PICK
    over-proposal if the matcher let every prediction absorb the same truth."""
    predicted = [
        _retail_event(f"evt-{i}", RetailEventType.PICK, "0000000000000001", 9.0 + i * 0.1)
        for i in range(10)
    ]
    result = _run_result(
        proposed=predicted,
        truth_events=[_truth_event(GroundTruthEventType.PICK, "0000000000000001", 10.0)],
    )
    pick = score_lab_run(result, MatchPolicy(tolerance_s=2.0)).event_type(RetailEventType.PICK)
    assert pick.true_positives == 1
    assert pick.false_positives == 9
    assert pick.false_negatives == 0
    assert pick.precision == pytest.approx(0.1)
    assert pick.recall == pytest.approx(1.0)


# --------------------------------------------------------------------------- #
# EPC confusion
# --------------------------------------------------------------------------- #


def test_epc_confusion_is_detected_for_a_same_gtin_sibling() -> None:
    """The true unit (epc_true) is never proposed at all; instead a sibling EPC
    of the SAME GTIN is proposed nearby in time -- exactly the SKU/EPC collapse
    signature this metric exists to catch."""
    epc_true = "0000000000000001"
    epc_confused = "0000000000000002"
    epc_unrelated = "0000000000000009"
    gtin_shared = "00012345678905"
    gtin_other = "00098765432109"

    result = _run_result(
        proposed=[_retail_event("evt-1", RetailEventType.PICK, epc_confused, 10.3)],
        truth_events=[_truth_event(GroundTruthEventType.PICK, epc_true, 10.0)],
        item_tracks=[
            ItemTrack(epc=_epc(epc_true), gtin=gtin_shared),
            ItemTrack(epc=_epc(epc_confused), gtin=gtin_shared),
            ItemTrack(epc=_epc(epc_unrelated), gtin=gtin_other),
        ],
    )
    report = score_lab_run(result, MatchPolicy(tolerance_s=2.0))

    assert report.item_identity.confusion_count == 1
    confusion = report.item_identity.epc_confusions[0]
    assert confusion.event_type == RetailEventType.PICK
    assert confusion.true_epc == epc_true
    assert confusion.confused_epc == epc_confused
    assert confusion.gtin == gtin_shared
    assert report.item_identity.missed_epcs == (epc_true,)
    assert report.item_identity.correctly_tracked_epcs == 0
    assert report.item_identity.true_pick_epcs == 1


def test_no_confusion_is_reported_when_no_gtin_is_shared() -> None:
    """A false positive on an unrelated GTIN must never be reported as a
    confusion, however close in time it lands."""
    result = _run_result(
        proposed=[
            _retail_event("evt-1", RetailEventType.PICK, "0000000000000002", 10.1),
        ],
        truth_events=[_truth_event(GroundTruthEventType.PICK, "0000000000000001", 10.0)],
        item_tracks=[
            ItemTrack(epc=_epc("0000000000000001"), gtin="00012345678905"),
            ItemTrack(epc=_epc("0000000000000002"), gtin="00098765432109"),
        ],
    )
    report = score_lab_run(result, MatchPolicy(tolerance_s=2.0))
    assert report.item_identity.epc_confusions == ()


# --------------------------------------------------------------------------- #
# Divide-by-zero guards
# --------------------------------------------------------------------------- #


def test_precision_is_none_with_no_predictions_of_that_type() -> None:
    result = _run_result(
        truth_events=[_truth_event(GroundTruthEventType.PICK, "0000000000000001", 10.0)],
    )
    pick = score_lab_run(result).event_type(RetailEventType.PICK)
    assert pick.true_positives == 0
    assert pick.precision is None
    assert pick.recall == 0.0  # a defined (if bad) number: 0 true positives out of 1 true event


def test_recall_is_none_with_no_true_events_of_that_type() -> None:
    result = _run_result(
        proposed=[_retail_event("evt-1", RetailEventType.PICK, "0000000000000001", 10.0)],
    )
    pick = score_lab_run(result).event_type(RetailEventType.PICK)
    assert pick.false_positives == 1
    assert pick.recall is None
    assert pick.precision == 0.0


def test_both_are_none_with_no_predictions_and_no_truth() -> None:
    result = _run_result()
    for metrics in score_lab_run(result).event_type_metrics:
        assert metrics.precision is None
        assert metrics.recall is None
        assert metrics.true_positives == 0
        assert metrics.false_positives == 0
        assert metrics.false_negatives == 0


def test_cart_correctness_precision_and_recall_are_none_when_both_sets_are_empty() -> None:
    report = score_lab_run(_run_result())
    assert report.cart_correctness.precision is None
    assert report.cart_correctness.recall is None
    assert report.cart_correctness.true_epcs == ()
    assert report.cart_correctness.predicted_epcs == ()


def test_confidence_rates_are_none_with_no_decisions() -> None:
    confidence = score_lab_run(_run_result()).confidence
    assert confidence.commit_rate is None
    assert confidence.wait_rate is None
    assert confidence.review_rate is None
    assert confidence.commit_correctness is None


# --------------------------------------------------------------------------- #
# Confidence and cart correctness happy paths
# --------------------------------------------------------------------------- #


def test_confidence_counts_the_final_decision_per_episode_not_every_re_decision() -> None:
    """A WAIT episode re-decided three times, then a COMMIT episode decided once,
    must count as one WAIT and one COMMIT -- not four decisions."""
    decisions = [
        ConfidenceDecision(
            event_id="evt-wait",
            decision=Decision.WAIT,
            confidence=0.5,
            margin=0.1,
            evaluated_at=_t(t),
        )
        for t in (1.0, 2.0, 3.0)
    ]
    decisions.append(
        ConfidenceDecision(
            event_id="evt-commit",
            decision=Decision.COMMIT,
            confidence=0.9,
            margin=0.5,
            evaluated_at=_t(4.0),
        )
    )
    committed = [_retail_event("evt-commit", RetailEventType.PICK, "0000000000000001", 4.0)]
    result = _run_result(
        decisions=decisions,
        committed=committed,
        truth_events=[_truth_event(GroundTruthEventType.PICK, "0000000000000001", 4.1)],
    )
    confidence = score_lab_run(result, MatchPolicy(tolerance_s=2.0)).confidence
    assert confidence.commit_count == 1
    assert confidence.wait_count == 1
    assert confidence.review_count == 0
    assert confidence.commit_rate == pytest.approx(0.5)
    assert confidence.committed_total == 1
    assert confidence.committed_true_positives == 1
    assert confidence.commit_correctness == pytest.approx(1.0)


def test_cart_correctness_reports_missing_and_extra_epcs() -> None:
    from radiowave.cart.models import Cart, CartLine, CartStatus

    cart = Cart(
        cart_id="P0001",
        shopper_track_id="P0001",
        status=CartStatus.EXITED,
        lines={
            "0000000000000001": CartLine(
                epc=_epc("0000000000000001"), added_at=_t(5.0), source_event_id="evt-1"
            ),
            "0000000000000009": CartLine(
                epc=_epc("0000000000000009"), added_at=_t(6.0), source_event_id="evt-2"
            ),
        },
    )
    result = _run_result(
        cart_state=CartState(carts={"P0001": cart}, current_cart_ids={"P0001": "P0001"}),
        truth_events=[
            _truth_event(GroundTruthEventType.EXIT_WITH_ITEM, "0000000000000001", 20.0),
            _truth_event(GroundTruthEventType.EXIT_WITH_ITEM, "0000000000000002", 20.0),
        ],
    )
    cart_correctness = score_lab_run(result).cart_correctness
    assert cart_correctness.correct_epcs == ("0000000000000001",)
    assert cart_correctness.missing_epcs == ("0000000000000002",)
    assert cart_correctness.extra_epcs == ("0000000000000009",)
    assert cart_correctness.precision == pytest.approx(0.5)
    assert cart_correctness.recall == pytest.approx(0.5)


def test_person_tracking_reports_true_shopper_count_from_enter_events() -> None:
    result = _run_result(
        truth_events=[
            _truth_event(GroundTruthEventType.ENTER, epc="", seconds=0.0, person="GT-PERSON-001"),
            _truth_event(GroundTruthEventType.ENTER, epc="", seconds=1.0, person="GT-PERSON-002"),
        ]
    )
    person_tracking = score_lab_run(result).person_tracking
    assert person_tracking.true_shopper_count == 2
    assert person_tracking.predicted_track_count == 0


# --------------------------------------------------------------------------- #
# Real catalog: structural properties only, never pinned accuracy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("scenario_id", ["01_normal_purchase", "acceptance_60s"])
def test_score_lab_run_against_the_real_catalog_is_structurally_sound(scenario_id: str) -> None:
    """The report must populate and stay internally consistent on a real run.

    Deliberately NOT asserted: any specific precision/recall/confusion number --
    baseline fusion accuracy is a Phase-6 concern, and pinning today's values
    here would turn a measurement into a target (see ``test_engine.py``'s
    module docstring for the same discipline applied to the engine itself).
    """
    scenario = load_scenario(scenario_id)
    result = run_lab_scenario(scenario)
    report = score_lab_run(result)

    assert report.scenario_id == scenario_id
    assert report.seed == result.seed
    assert len(report.event_type_metrics) == 5
    for metrics in report.event_type_metrics:
        assert metrics.true_positives >= 0
        assert metrics.false_positives >= 0
        assert metrics.false_negatives >= 0
        if metrics.precision is not None:
            assert 0.0 <= metrics.precision <= 1.0
        if metrics.recall is not None:
            assert 0.0 <= metrics.recall <= 1.0

    assert report.person_tracking.true_shopper_count == len(scenario.shoppers)
    assert report.person_tracking.predicted_track_count >= 0

    assert report.item_identity.true_pick_epcs >= 0
    assert report.item_identity.correctly_tracked_epcs <= report.item_identity.true_pick_epcs
    assert len(report.item_identity.missed_epcs) == (
        report.item_identity.true_pick_epcs - report.item_identity.correctly_tracked_epcs
    )

    cc = report.cart_correctness
    assert set(cc.correct_epcs) <= set(cc.true_epcs)
    assert set(cc.correct_epcs) <= set(cc.predicted_epcs)
    assert set(cc.missing_epcs) == set(cc.true_epcs) - set(cc.predicted_epcs)
    assert set(cc.extra_epcs) == set(cc.predicted_epcs) - set(cc.true_epcs)

    confidence = report.confidence
    assert confidence.commit_count + confidence.wait_count + confidence.review_count >= 0
    assert confidence.committed_true_positives <= confidence.committed_total


def test_01_normal_purchase_has_exactly_one_true_pick() -> None:
    """A ground-truth-only structural fact from the scenario script itself, not
    a fusion accuracy pin: scenario 01 schedules exactly one PICK."""
    result = run_lab_scenario(load_scenario("01_normal_purchase"))
    report = score_lab_run(result)
    assert report.item_identity.true_pick_epcs == 1


def test_an_episode_is_represented_by_its_first_proposal_not_its_last() -> None:
    """Regression: the representative timestamp decides every match.

    Fusion re-proposes a waiting episode every step, so one episode arrives as many
    RetailEvents sharing an event_id. Scoring the LAST of them drags the
    representative arbitrarily far past the truth it correctly detected - an episode
    that waits 21 steps lands seconds late, falls outside tolerance, and is counted
    as a miss AND a false alarm. That produced a uniform zero-true-positive report
    across the whole catalog, which read as total algorithmic failure when the events
    had in fact been detected within ~1.3s. The first proposal is both the correct
    representative and what detection latency actually means.
    """
    epc = "0000000000000001"
    episode = [
        _retail_event("ep-1", RetailEventType.PICK, epc, 7.3),
        _retail_event("ep-1", RetailEventType.PICK, epc, 9.3),
        _retail_event("ep-1", RetailEventType.PICK, epc, 12.3),
    ]
    result = _run_result(
        proposed=episode,
        truth_events=[_truth_event(GroundTruthEventType.PICK, epc, 6.0)],
    )

    pick = score_lab_run(result, MatchPolicy(tolerance_s=2.0)).event_type(RetailEventType.PICK)
    # +1.3s is inside the tolerance; +12.3s would not be.
    assert pick.true_positives == 1
    assert pick.false_positives == 0
    assert pick.false_negatives == 0
