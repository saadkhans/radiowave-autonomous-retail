"""Quantitative scoring of one :class:`~radiowave.simulator.lab.engine.LabRunResult`.

This module answers one question only: *what did fusion conclude, and how does that
compare to what the simulator recorded as having actually happened?* It is a pure,
read-only consumer of a finished run --

* every function here takes a ``LabRunResult`` (or pieces already extracted from
  one) and returns a value; nothing is mutated, nothing is fed back into
  ``radiowave.pipeline``, ``radiowave.fusion``, ``radiowave.cart`` or
  ``radiowave.confidence``, and nothing here is a new input those modules could
  ever see. Evaluation must stay strictly downstream of inference, the same
  discipline ``ground_truth.py`` enforces on the simulator side (see its
  docstring): if scoring code could influence the run being scored, it would be
  grading itself.
* no I/O, no global state, no randomness. Calling ``score_lab_run`` twice on the
  same ``LabRunResult`` with the same ``MatchPolicy`` always returns an equal
  report.

Two identity questions this module refuses to fabricate an answer to
----------------------------------------------------------------------
1. **Which canonical shopper track is which true shopper?** Canonical track ids
   (``P0001``, assigned by fusion) and ground-truth ids (``GT-PERSON-001``,
   assigned by the simulator) share no common namespace by design -- see
   ``test_canonical_person_ids_are_not_ground_truth_ids`` in
   ``tests/simulator/lab/test_engine.py``. Deriving that mapping soundly would
   require its own spatial/temporal association pass, which would mean
   re-implementing (and silently trusting) the very trajectory-association
   algorithm this module exists to grade. So `PersonTrackingMetrics` reports
   counts only, never a fragmentation ratio, and `CartCorrectnessMetrics`
   reports set-level (store-wide) EPC correctness rather than a per-shopper
   comparison. Both dataclasses document this in place; neither field is ever a
   guessed number standing in for one that cannot be soundly computed.
2. **Which physical unit is which, when two units share a GTIN?** This one is
   directly answerable, and is the highest-value metric this module computes:
   RFID reads (and every contract downstream of them) carry the literal EPC
   value, so a predicted event's ``epc`` and a true event's ``epc`` are directly
   comparable with no id gap at all. `ItemIdentityMetrics.epc_confusions`
   is what SKU/EPC collapse looks like from the outside: a true pick fusion
   missed, paired with a same-GTIN sibling EPC fusion falsely proposed instead.

Serialization
-------------
Every public type here is a frozen, ``slots``-based dataclass built only from
primitives (``str``, ``int``, ``float``, ``bool``, ``None``, ``tuple``,
``datetime``) and ``StrEnum`` members (which are themselves ``str``). A caller can
dump a `LabMetricsReport` with
``json.dumps(dataclasses.asdict(report), default=str)`` -- ``default=str`` is only
needed because ``datetime`` is not natively JSON-encodable; every enum field
already serializes as its plain string value.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from radiowave.contracts.confidence import Decision
from radiowave.contracts.events import RetailEvent, RetailEventType
from radiowave.pipeline import PipelineResult
from radiowave.simulator.lab.engine import LabRunResult
from radiowave.simulator.lab.ground_truth import GroundTruthEvent, GroundTruthEventType

#: The ``RetailEventType`` members this module scores. ``RetailEventType.CARRY``
#: is deliberately excluded even though fusion does propose and can commit it: it
#: names an ongoing state ("still being carried"), not a discrete happening with
#: one well-defined instant to match against, and scoring it here would silently
#: blend two different questions ("did X get picked" vs "was X observed mid-carry
#: at this particular tick"). Excluded by the evaluation's explicit scope.
SCORED_EVENT_TYPES: tuple[RetailEventType, ...] = (
    RetailEventType.PICK,
    RetailEventType.PUTBACK,
    RetailEventType.MISPLACE,
    RetailEventType.HANDOFF,
    RetailEventType.EXIT_WITH_ITEM,
)


@dataclass(frozen=True, slots=True)
class MatchPolicy:
    """How a predicted ``RetailEvent`` is paired with a true ``GroundTruthEvent``.

    A predicted event matches a true event when, and only when:

    1. the event types name the same physical happening (``RetailEventType.PICK``
       corresponds to ``GroundTruthEventType.PICK``, matched by enum *name* --
       see :func:`_truth_event_type`);
    2. the EPCs are identical (never gtin-equal -- see the module docstring); and
    3. the two timestamps are within ``tolerance_s`` of each other.

    Exact timestamp equality is never required: the simulated clock, sensor
    cadence and confidence-engine wait time all separate "the instant fusion
    proposed this" from "the instant it truly happened", so a hard equality check
    would fail almost every genuine match.

    ``tolerance_s`` is a documented *choice*, not a physical law. The 2.0 s
    default is a few multiples of the default pipeline step interval (0.25 s,
    see ``PipelineConfig.step_interval_s``) plus slack for the confidence
    engine's grace period before a low-confidence event may resolve -- enough
    room for a genuine detection to land without also admitting an unrelated
    episode of the same EPC minutes away. Callers that want to audit near-misses
    (widen it) or demand tighter timing (narrow it) should pass their own value;
    that is exactly why this is a parameter and not a constant baked into the
    matcher.
    """

    tolerance_s: float = 2.0


def _truth_event_type(event_type: RetailEventType) -> GroundTruthEventType:
    """The ``GroundTruthEventType`` naming the same physical happening.

    ``RetailEventType`` (what fusion infers) and ``GroundTruthEventType`` (what
    the simulator did) are deliberately separate vocabularies -- see both
    modules' docstrings -- but share member *names* for every physical event
    fusion can propose. Mapping by name keeps that correspondence explicit
    rather than duplicating it as a second, hand-maintained table that could
    drift from the enums it describes.
    """
    return GroundTruthEventType[event_type.name]


def _ratio(numerator: int, denominator: int) -> float | None:
    """``numerator / denominator``, or ``None`` when the question is undefined.

    A precision or recall with an empty denominator is not "perfect" (``1.0``)
    or "failing" (``0.0``) -- it is a question that was never asked this run
    (e.g. "PUTBACK recall" when the scenario contains no true PUTBACKs at all).
    Reporting ``None`` keeps that distinguishable from a genuine, measured 0 or 1.
    """
    return numerator / denominator if denominator > 0 else None


@dataclass(frozen=True, slots=True)
class _Match:
    """One matched (predicted, truth) pair. Internal to the matching algorithm;
    never part of the public, serializable report (see the module docstring)."""

    predicted: RetailEvent
    truth: GroundTruthEvent
    delta_s: float


def _dedup_by_event_id(events: list[RetailEvent]) -> list[RetailEvent]:
    """Collapse repeated re-proposals of one episode (shared ``event_id``) into a
    single representative: the EARLIEST proposal.

    Fusion re-proposes a still-WAITing episode every step until it commits,
    expires into REVIEW, or the underlying item state moves on (see
    ``FoundationPipeline._step``); those re-proposals are the *same* prediction
    made repeatedly, not independent ones. Collapsing them here, before
    matching, keeps the over-proposal defect this matcher exists to surface
    (see `_greedy_match`) about genuinely distinct episodes rather than an
    artifact of how many ticks each one happened to wait.

    Earliest, not latest, and the choice is load-bearing. The representative
    timestamp is what gets compared against the truth time, and the moment worth
    comparing is when the system FIRST concluded the event had happened - that is
    also what detection latency means. Taking the last re-proposal instead drags
    the representative arbitrarily far forward: an episode that waits 21 steps
    before committing ends up timestamped seconds after the truth it correctly
    detected, falls outside any sane tolerance, and is scored as a miss AND a
    false alarm. That produced a uniform zero-true-positive report across every
    scenario, which read as total algorithmic failure when the events were in
    fact detected within ~1.3s.
    """
    earliest: dict[str, RetailEvent] = {}
    for event in events:
        current = earliest.get(event.event_id)
        if current is None or event.timestamp < current.timestamp:
            earliest[event.event_id] = event
    return list(earliest.values())


def _greedy_match(
    predicted: list[RetailEvent],
    truth: list[GroundTruthEvent],
    tolerance_s: float,
) -> tuple[list[_Match], list[RetailEvent], list[GroundTruthEvent]]:
    """Greedy nearest-in-time one-to-one matching within one (event_type, epc) pool.

    Every (predicted, truth) pair within ``tolerance_s`` of each other is a match
    candidate. Candidates are consumed smallest-``|delta|`` first; once either
    side of a candidate is used, every other candidate touching it is skipped.

    This is a deliberately simple heuristic, not a globally optimal assignment
    (the Hungarian algorithm would minimize total ``|delta|`` across every pair
    simultaneously, and could occasionally prefer a different pairing than the
    greedy one chosen here). What it guarantees, and the only property this
    evaluation actually needs, is that **one true event can absorb at most one
    prediction**. Without that guarantee, an engine that proposes the same
    episode many times over -- the baseline's own known ~10x PICK
    over-proposal -- would show up as many true positives instead of one true
    positive and many false positives, hiding exactly the defect worth seeing.
    The bias this trades in for that guarantee: a true event always pairs with
    whichever candidate happens to be nearest in time first, even in the rare
    case a different, still-in-tolerance pairing would have produced a smaller
    total time error across the whole batch.
    """
    scored: list[tuple[float, int, int]] = []
    for pi, p in enumerate(predicted):
        for ti, t in enumerate(truth):
            delta = abs((p.timestamp - t.timestamp).total_seconds())
            if delta <= tolerance_s:
                scored.append((delta, pi, ti))
    scored.sort(key=lambda c: (c[0], c[1], c[2]))

    matched_p: set[int] = set()
    matched_t: set[int] = set()
    matches: list[_Match] = []
    for delta, pi, ti in scored:
        if pi in matched_p or ti in matched_t:
            continue
        matched_p.add(pi)
        matched_t.add(ti)
        matches.append(_Match(predicted=predicted[pi], truth=truth[ti], delta_s=delta))

    unmatched_p = [p for i, p in enumerate(predicted) if i not in matched_p]
    unmatched_t = [t for i, t in enumerate(truth) if i not in matched_t]
    return matches, unmatched_p, unmatched_t


_MatchResult = tuple[list[_Match], list[RetailEvent], list[GroundTruthEvent]]


def _match_by_type_and_epc(
    predicted: list[RetailEvent],
    truth_events: list[GroundTruthEvent],
    event_type: RetailEventType,
    tolerance_s: float,
) -> _MatchResult:
    """Match one event type, grouped by EPC first so a PICK of item A can never
    match a PICK of item B regardless of how close their timestamps are."""
    truth_type = _truth_event_type(event_type)
    preds = [e for e in predicted if e.event_type == event_type]
    truths = [e for e in truth_events if e.event_type == truth_type]

    epcs = {e.epc.value for e in preds} | {e.epc for e in truths if e.epc is not None}
    matches: list[_Match] = []
    unmatched_p: list[RetailEvent] = []
    unmatched_t: list[GroundTruthEvent] = []
    for epc in sorted(epcs):
        p_group = [e for e in preds if e.epc.value == epc]
        t_group = [e for e in truths if e.epc == epc]
        group_matches, group_up, group_ut = _greedy_match(p_group, t_group, tolerance_s)
        matches.extend(group_matches)
        unmatched_p.extend(group_up)
        unmatched_t.extend(group_ut)
    return matches, unmatched_p, unmatched_t


@dataclass(frozen=True, slots=True)
class EventTypeMetrics:
    """Precision/recall for one ``RetailEventType`` against ground truth.

    ``precision``/``recall`` are ``None`` -- never ``0.0`` or ``1.0`` -- when
    their denominator is zero; see `_ratio`.
    """

    event_type: RetailEventType
    true_positives: int
    false_positives: int
    false_negatives: int
    precision: float | None
    recall: float | None


def _event_type_metrics(
    predicted: list[RetailEvent],
    ground_truth_events: list[GroundTruthEvent],
    tolerance_s: float,
) -> tuple[dict[RetailEventType, _MatchResult], tuple[EventTypeMetrics, ...]]:
    by_type: dict[RetailEventType, _MatchResult] = {}
    metrics: list[EventTypeMetrics] = []
    for event_type in SCORED_EVENT_TYPES:
        result = _match_by_type_and_epc(predicted, ground_truth_events, event_type, tolerance_s)
        by_type[event_type] = result
        matches, unmatched_p, unmatched_t = result
        tp, fp, fn = len(matches), len(unmatched_p), len(unmatched_t)
        metrics.append(
            EventTypeMetrics(
                event_type=event_type,
                true_positives=tp,
                false_positives=fp,
                false_negatives=fn,
                precision=_ratio(tp, tp + fp),
                recall=_ratio(tp, tp + fn),
            )
        )
    return by_type, tuple(metrics)


@dataclass(frozen=True, slots=True)
class EpcConfusion:
    """A missed true event whose most plausible explanation is that fusion
    proposed the wrong physical unit of the *same product* nearby in time.

    This is the direct, externally-visible symptom of a SKU/EPC identity
    collapse: canonical item identity must stay keyed on EPC, never GTIN, and if
    that boundary leaks internally this is what it looks like from here.
    Detected as: a true event of some scored type/EPC that `_greedy_match` left
    unmatched (a false negative for that EPC), paired -- again by nearest-in-time,
    one-to-one, so one stray proposal cannot "explain away" more than one miss --
    with an unmatched *predicted* event of the same event type and the same GTIN
    but a *different* EPC.
    """

    event_type: RetailEventType
    true_epc: str
    confused_epc: str
    gtin: str
    true_timestamp: datetime
    predicted_timestamp: datetime
    delta_s: float


def _true_epc_pool(
    events: list[GroundTruthEvent], gtin_of: dict[str, str | None]
) -> list[tuple[int, GroundTruthEvent, str, str]]:
    """``(index, event, epc, gtin)`` for every event with a known EPC and GTIN."""
    pool: list[tuple[int, GroundTruthEvent, str, str]] = []
    for index, event in enumerate(events):
        epc = event.epc
        if epc is None:
            continue
        gtin = gtin_of.get(epc)
        if gtin is None:
            continue
        pool.append((index, event, epc, gtin))
    return pool


def _epc_confusions(
    by_type: dict[RetailEventType, _MatchResult],
    gtin_of: dict[str, str | None],
    tolerance_s: float,
) -> tuple[EpcConfusion, ...]:
    confusions: list[EpcConfusion] = []
    for event_type, (_matches, unmatched_p, unmatched_t) in by_type.items():
        true_pool = _true_epc_pool(unmatched_t, gtin_of)

        candidates: list[tuple[float, int, int]] = []
        for ti, t, true_epc, true_gtin in true_pool:
            for pi, p in enumerate(unmatched_p):
                pred_epc = p.epc.value
                if pred_epc == true_epc or gtin_of.get(pred_epc) != true_gtin:
                    continue
                delta = abs((p.timestamp - t.timestamp).total_seconds())
                if delta <= tolerance_s:
                    candidates.append((delta, ti, pi))
        candidates.sort(key=lambda c: (c[0], c[1], c[2]))

        true_by_index = {ti: (t, true_epc, true_gtin) for ti, t, true_epc, true_gtin in true_pool}
        used_t: set[int] = set()
        used_p: set[int] = set()
        for delta, ti, pi in candidates:
            if ti in used_t or pi in used_p:
                continue
            used_t.add(ti)
            used_p.add(pi)
            t, true_epc, true_gtin = true_by_index[ti]
            p = unmatched_p[pi]
            confusions.append(
                EpcConfusion(
                    event_type=event_type,
                    true_epc=true_epc,
                    confused_epc=p.epc.value,
                    gtin=true_gtin,
                    true_timestamp=t.timestamp,
                    predicted_timestamp=p.timestamp,
                    delta_s=delta,
                )
            )
    return tuple(confusions)


@dataclass(frozen=True, slots=True)
class ItemIdentityMetrics:
    """Per-item identity correctness, EPC by EPC, scoped to true PICK episodes
    (the point at which an item's lifecycle begins being tracked).

    Unlike shopper attribution (see `PersonTrackingMetrics`), EPC identity needs
    no cross-namespace mapping: a predicted event's ``epc`` and a true event's
    ``epc`` are the same literal value with no id gap to bridge, so "was the
    correct EPC tracked" is directly answerable rather than merely estimable.
    """

    true_pick_epcs: int
    correctly_tracked_epcs: int
    missed_epcs: tuple[str, ...]
    epc_confusions: tuple[EpcConfusion, ...]

    @property
    def confusion_count(self) -> int:
        return len(self.epc_confusions)


def _item_identity(
    pick_matches: list[_Match],
    pick_unmatched_truth: list[GroundTruthEvent],
    confusions: tuple[EpcConfusion, ...],
) -> ItemIdentityMetrics:
    tracked = sorted({m.truth.epc for m in pick_matches if m.truth.epc is not None})
    missed = sorted({t.epc for t in pick_unmatched_truth if t.epc is not None})
    return ItemIdentityMetrics(
        true_pick_epcs=len(tracked) + len(missed),
        correctly_tracked_epcs=len(tracked),
        missed_epcs=tuple(missed),
        epc_confusions=confusions,
    )


@dataclass(frozen=True, slots=True)
class PersonTrackingMetrics:
    """Shopper-count comparison only -- see the module docstring for why track
    *fragmentation* (canonical tracks per true shopper) is not reported here.

    Answering "which true shopper does this canonical track belong to" needs its
    own spatial/temporal association pass, which would mean re-implementing (and
    silently trusting) the very trajectory-association algorithm this evaluation
    exists to grade. Rather than invent that number unsoundly, only what is
    directly comparable without an id mapping is reported: how many distinct
    tracks and true shoppers each side has.
    """

    predicted_track_count: int
    true_shopper_count: int


@dataclass(frozen=True, slots=True)
class CartCorrectnessMetrics:
    """Store-wide (not per-shopper) EPC correctness of the final cart state.

    Per-shopper cart accuracy would need a canonical-cart -> true-shopper
    mapping, which hits the identical id-gap problem as `PersonTrackingMetrics`
    (a cart is keyed by canonical ``shopper_track_id``; ground truth by
    ``ground_truth_person_id``) -- there is no sound way to say "this cart
    belongs to that shopper" without re-deriving trajectory association
    ourselves. What IS directly comparable without inventing that mapping is the
    *set* of EPCs that should have left the store versus the set the final cart
    state says did, store-wide; that is what this reports.
    """

    true_epcs: tuple[str, ...]
    predicted_epcs: tuple[str, ...]
    correct_epcs: tuple[str, ...]
    missing_epcs: tuple[str, ...]
    extra_epcs: tuple[str, ...]
    precision: float | None
    recall: float | None


def _cart_correctness(
    pipeline_result: PipelineResult, ground_truth_events: list[GroundTruthEvent]
) -> CartCorrectnessMetrics:
    true_epcs = {
        e.epc
        for e in ground_truth_events
        if e.event_type == GroundTruthEventType.EXIT_WITH_ITEM and e.epc is not None
    }
    predicted_epcs = {
        epc for cart in pipeline_result.cart_state.carts.values() for epc in cart.epcs
    }
    correct = true_epcs & predicted_epcs
    missing = true_epcs - predicted_epcs
    extra = predicted_epcs - true_epcs
    return CartCorrectnessMetrics(
        true_epcs=tuple(sorted(true_epcs)),
        predicted_epcs=tuple(sorted(predicted_epcs)),
        correct_epcs=tuple(sorted(correct)),
        missing_epcs=tuple(sorted(missing)),
        extra_epcs=tuple(sorted(extra)),
        precision=_ratio(len(correct), len(predicted_epcs)),
        recall=_ratio(len(correct), len(true_epcs)),
    )


@dataclass(frozen=True, slots=True)
class ConfidenceMetrics:
    """Confidence-engine outcome counts, and correctness of what it chose to COMMIT.

    Counts are per distinct *episode* (``event_id``), taking each episode's
    final decision. A still-WAITing episode is re-decided every fusion step
    (see ``FoundationPipeline._step``), so counting every ``ConfidenceDecision``
    entry verbatim would count one pending episode dozens of times over and
    swamp the genuine COMMIT/REVIEW counts.
    """

    commit_count: int
    wait_count: int
    review_count: int
    commit_rate: float | None
    wait_rate: float | None
    review_rate: float | None
    committed_total: int
    committed_true_positives: int
    commit_correctness: float | None


def _total_true_positives(
    predicted: list[RetailEvent], ground_truth_events: list[GroundTruthEvent], tolerance_s: float
) -> int:
    total = 0
    for event_type in SCORED_EVENT_TYPES:
        matches, _unmatched_p, _unmatched_t = _match_by_type_and_epc(
            predicted, ground_truth_events, event_type, tolerance_s
        )
        total += len(matches)
    return total


def _confidence_metrics(
    pipeline_result: PipelineResult,
    ground_truth_events: list[GroundTruthEvent],
    tolerance_s: float,
) -> ConfidenceMetrics:
    final_decision: dict[str, Decision] = {}
    for decision in pipeline_result.decisions:  # chronological; last write wins
        final_decision[decision.event_id] = decision.decision
    total = len(final_decision)
    commit_count = sum(1 for d in final_decision.values() if d == Decision.COMMIT)
    wait_count = sum(1 for d in final_decision.values() if d == Decision.WAIT)
    review_count = sum(1 for d in final_decision.values() if d == Decision.REVIEW)

    committed_scored = [
        e for e in pipeline_result.committed_events if e.event_type in SCORED_EVENT_TYPES
    ]
    committed_tp = _total_true_positives(committed_scored, ground_truth_events, tolerance_s)

    return ConfidenceMetrics(
        commit_count=commit_count,
        wait_count=wait_count,
        review_count=review_count,
        commit_rate=_ratio(commit_count, total),
        wait_rate=_ratio(wait_count, total),
        review_rate=_ratio(review_count, total),
        committed_total=len(committed_scored),
        committed_true_positives=committed_tp,
        commit_correctness=_ratio(committed_tp, len(committed_scored)),
    )


@dataclass(frozen=True, slots=True)
class LabMetricsReport:
    """Everything this module can honestly say about one ``LabRunResult``.

    Built once, by :func:`score_lab_run`, from a finished run; see the module
    docstring for the invariants every field here upholds (pure, read-only,
    never fed back into the pipeline, no fabricated cross-namespace ids).
    """

    scenario_id: str
    seed: int
    policy: MatchPolicy
    event_type_metrics: tuple[EventTypeMetrics, ...]
    person_tracking: PersonTrackingMetrics
    item_identity: ItemIdentityMetrics
    cart_correctness: CartCorrectnessMetrics
    confidence: ConfidenceMetrics

    def event_type(self, event_type: RetailEventType) -> EventTypeMetrics:
        """Convenience lookup; raises if ``event_type`` is not scored (see
        `SCORED_EVENT_TYPES`)."""
        for metrics in self.event_type_metrics:
            if metrics.event_type == event_type:
                return metrics
        msg = f"{event_type} is not a scored event type"
        raise KeyError(msg)


def score_lab_run(result: LabRunResult, policy: MatchPolicy | None = None) -> LabMetricsReport:
    """Score one finished lab run against its own ground truth.

    Pure function: reads ``result`` only (see the module docstring), performs no
    I/O, and is safe to call any number of times on the same result.
    """
    policy = policy if policy is not None else MatchPolicy()
    pipeline_result = result.pipeline
    ground_truth_events = result.ground_truth.events

    # Known from the digital twin (every registry item is registered up front --
    # see ``BaselineFusionEngine.__init__``), not inferred, so this lookup is
    # exact for every EPC in the store regardless of whether fusion ever tracked
    # it during this run.
    gtin_of: dict[str, str | None] = {t.epc.value: t.gtin for t in pipeline_result.item_tracks}

    predicted = _dedup_by_event_id(
        [e for e in pipeline_result.proposed_events if e.event_type in SCORED_EVENT_TYPES]
    )
    by_type, event_type_metrics = _event_type_metrics(
        predicted, ground_truth_events, policy.tolerance_s
    )

    pick_matches, _pick_unmatched_p, pick_unmatched_t = by_type[RetailEventType.PICK]
    confusions = _epc_confusions(by_type, gtin_of, policy.tolerance_s)
    item_identity = _item_identity(pick_matches, pick_unmatched_t, confusions)

    true_shopper_count = len(
        {
            e.ground_truth_person_id
            for e in ground_truth_events
            if e.event_type == GroundTruthEventType.ENTER and e.ground_truth_person_id is not None
        }
    )
    person_tracking = PersonTrackingMetrics(
        predicted_track_count=len(pipeline_result.person_tracks),
        true_shopper_count=true_shopper_count,
    )

    cart_correctness = _cart_correctness(pipeline_result, ground_truth_events)
    confidence = _confidence_metrics(pipeline_result, ground_truth_events, policy.tolerance_s)

    return LabMetricsReport(
        scenario_id=result.scenario_id,
        seed=result.seed,
        policy=policy,
        event_type_metrics=event_type_metrics,
        person_tracking=person_tracking,
        item_identity=item_identity,
        cart_correctness=cart_correctness,
        confidence=confidence,
    )


__all__ = [
    "SCORED_EVENT_TYPES",
    "CartCorrectnessMetrics",
    "ConfidenceMetrics",
    "EpcConfusion",
    "EventTypeMetrics",
    "ItemIdentityMetrics",
    "LabMetricsReport",
    "MatchPolicy",
    "PersonTrackingMetrics",
    "score_lab_run",
]
