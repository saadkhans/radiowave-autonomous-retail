"""In-process deterministic runs of the Foundation pipeline for the Observatory.

A run owns one scenario, its pre-generated observation stream and one
:class:`FoundationPipeline`. The UI drives simulated time through
``advance``/``step``/``seek``; no wall clock is involved, so the same sequence
of calls always yields the same state.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import timedelta
from threading import Lock, RLock

from radiowave.api.viewmodels import (
    ObservatoryBoundary,
    ObservatoryBounds,
    ObservatoryCandidate,
    ObservatoryCart,
    ObservatoryCartLine,
    ObservatoryCatalogItem,
    ObservatoryCounters,
    ObservatoryDecision,
    ObservatoryEvent,
    ObservatoryFeature,
    ObservatoryFixture,
    ObservatoryGroundTruth,
    ObservatoryItem,
    ObservatoryPerson,
    ObservatoryPoint,
    ObservatoryProduct,
    ObservatoryRunState,
    ObservatoryScenarioDetail,
    ObservatoryScenarioSummary,
    ObservatorySensor,
    ObservatorySession,
    ObservatoryStore,
    ObservatoryTimeline,
    ObservatoryTimelineMarker,
    ObservatoryZone,
    seconds_since_epoch,
)
from radiowave.cart.models import Cart
from radiowave.contracts.confidence import ConfidenceDecision
from radiowave.contracts.events import RetailEvent
from radiowave.contracts.sessions import ShopperSession
from radiowave.contracts.store import EPC, Box2D, Store
from radiowave.contracts.tracks import (
    CandidateScore,
    ItemState,
    ItemTrack,
    PersonTrack,
    TrackPoint,
)
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline, PipelineConfig, PipelineResult
from radiowave.simulator.library import SCENARIOS, load_scenario
from radiowave.simulator.runner import build_pipeline, scenario_observation_stream
from radiowave.simulator.scenario import SCENARIO_EPOCH, Scenario

TRAIL_POINTS = 40
FEATURE_ORDER = (
    "distance",
    "distance_trend",
    "velocity",
    "temporal",
    "co_motion",
    "zone",
    "vision",
)


def short_epc(epc: str) -> str:
    return epc[-6:]


def _seconds(when: object) -> float:
    value = seconds_since_epoch(when)  # type: ignore[arg-type]
    return value if value is not None else 0.0


# ---------------------------------------------------------------------------
# store / scenario view models
# ---------------------------------------------------------------------------
def _bounds(box: Box2D) -> ObservatoryBounds:
    return ObservatoryBounds(
        min_x=box.min_x,
        min_y=box.min_y,
        max_x=box.max_x,
        max_y=box.max_y,
    )


def store_view(store: Store) -> ObservatoryStore:
    products = {p.gtin: p for p in store.products}
    return ObservatoryStore(
        store_id=store.store_id,
        name=store.name,
        units=store.frame.units,
        floor=_bounds(store.floor_bounds),
        zones=[
            ObservatoryZone(
                zone_id=z.zone_id, name=z.name, kind=z.kind.value, bounds=_bounds(z.bounds)
            )
            for z in store.zones
        ],
        fixtures=[
            ObservatoryFixture(
                fixture_id=f.fixture_id, zone_id=f.zone_id, name=f.name, bounds=_bounds(f.bounds)
            )
            for f in store.fixtures
        ],
        boundaries=[
            ObservatoryBoundary(
                boundary_id=b.boundary_id, kind=b.kind.value, bounds=_bounds(b.bounds)
            )
            for b in store.boundaries
        ],
        sensors=[
            ObservatorySensor(
                sensor_id=s.sensor_id,
                modality=s.modality.value,
                x=s.pose.position.x,
                y=s.pose.position.y,
                z=s.pose.position.z,
                yaw=s.pose.yaw,
                name=s.name,
            )
            for s in store.sensors
        ],
        products=[
            ObservatoryProduct(
                gtin=p.gtin, name=p.name, sku=p.sku, home_fixture_id=p.home_fixture_id
            )
            for p in store.products
        ],
        items=[
            ObservatoryCatalogItem(
                epc=i.epc.value,
                short_epc=short_epc(i.epc.value),
                gtin=i.gtin,
                product_name=products[i.gtin].name if i.gtin in products else i.gtin,
                home_fixture_id=i.home_fixture_id
                or (products[i.gtin].home_fixture_id if i.gtin in products else None),
            )
            for i in store.items
        ],
    )


def _ground_truth(scenario: Scenario) -> list[ObservatoryGroundTruth]:
    return [
        ObservatoryGroundTruth(
            t_s=truth.t,
            event_type=truth.event_type.value,
            epc=truth.epc,
            shopper_label=truth.shopper_label,
            counterpart_label=truth.counterpart_label,
        )
        for truth in scenario.expected_events
    ]


def scenario_summary(scenario: Scenario) -> ObservatoryScenarioSummary:
    return ObservatoryScenarioSummary(
        scenario_id=scenario.scenario_id,
        name=scenario.name,
        description=scenario.description,
        duration_s=scenario.duration_s,
        seed=scenario.seed,
        shopper_count=len(scenario.shoppers),
        item_count=len(scenario.placements),
        vision_enabled=scenario.vision_enabled,
        radar_dropouts=len(scenario.radar_dropouts),
        rfid_dropouts=len(scenario.rfid_dropouts),
        ground_truth=_ground_truth(scenario),
    )


def scenario_detail(scenario: Scenario) -> ObservatoryScenarioDetail:
    summary = scenario_summary(scenario)
    return ObservatoryScenarioDetail(**summary.model_dump(), store=store_view(scenario.store))


def list_scenarios() -> list[ObservatoryScenarioSummary]:
    return [scenario_summary(load_scenario(scenario_id)) for scenario_id in SCENARIOS]


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
class ObservatoryRun:
    def __init__(
        self,
        run_id: str,
        scenario: Scenario,
        pipeline_config: PipelineConfig | None = None,
    ) -> None:
        self.run_id = run_id
        self.scenario = scenario
        self.config = pipeline_config
        self.registry = StoreRegistry(scenario.store)
        # What the scenario is *fed* (scenario 10 replays every observation twice).
        self.observations = scenario_observation_stream(scenario)
        # FastAPI runs sync handlers on worker threads; every mutation and every
        # snapshot of one run is serialized so replay stays deterministic.
        self._lock = RLock()
        self._catalog = {i.epc.value: i for i in scenario.store.items}
        self._products = {p.gtin: p for p in scenario.store.products}
        self.pipeline: FoundationPipeline = build_pipeline(scenario, pipeline_config)
        self.cursor = 0
        self.time_s = 0.0
        self.finished = False
        self.reset()

    # ----------------------------------------------------------------- control
    def reset(self) -> None:
        with self._lock:
            self.pipeline = build_pipeline(self.scenario, self.config)
            self.cursor = 0
            self.time_s = 0.0
            self.finished = False

    @property
    def step_interval_s(self) -> float:
        return self.pipeline.config.step_interval_s

    @property
    def duration_s(self) -> float:
        return self.scenario.duration_s

    def advance(self, seconds: float) -> None:
        """Move simulated time forward, feeding every observation stamped up to the target."""
        with self._lock:
            if self.finished or seconds <= 0:
                return
            target = min(self.time_s + seconds, self.duration_s)
            target_at = SCENARIO_EPOCH + timedelta(seconds=target)
            while self.cursor < len(self.observations):
                observation = self.observations[self.cursor]
                if observation.timestamp > target_at:
                    break
                self.pipeline.ingest(observation)
                self.cursor += 1
            self.pipeline.advance_to(target_at)
            self.time_s = round(target, 6)
            if self.time_s >= self.duration_s and self.cursor >= len(self.observations):
                # The clock already stepped through the duration; finalize carts and
                # sessions without evaluating anything past the advertised end.
                self.pipeline.finish(advance=False)
                self.finished = True

    def step(self) -> None:
        self.advance(self.step_interval_s)

    def apply(self, operation: Callable[[], None]) -> ObservatoryRunState:
        """Run one mutation and snapshot the result under the same lock.

        A response must describe the request that produced it, never a concurrent
        client's reset or seek that slipped in between the mutation and the snapshot.
        """
        with self._lock:
            operation()
            return self._state()

    def seek(self, time_s: float) -> None:
        """Deterministic scrub: rebuild from zero when moving backwards."""
        with self._lock:
            target = max(0.0, min(time_s, self.duration_s))
            if target < self.time_s or (self.finished and target < self.duration_s):
                self.reset()
            if target > self.time_s:
                self.advance(target - self.time_s)

    # ----------------------------------------------------------------- views
    def state(self) -> ObservatoryRunState:
        with self._lock:
            return self._state()

    def _state(self) -> ObservatoryRunState:
        result = self.pipeline.result()
        decisions = _decisions_with_proposals(result)
        cart_sessions = _cart_sessions(result)
        carts = [
            self._cart_view(c, cart_sessions.get(c.cart_id))
            for c in result.cart_state.carts.values()
        ]
        cart_by_track = {
            cart.shopper_track_id: cart.cart_id
            for cart in carts
            if cart.cart_id == result.cart_state.current_cart_ids.get(cart.shopper_track_id)
        }
        carried: dict[str, list[str]] = {}
        for item in result.item_tracks:
            if item.carrier_track_id and item.state == ItemState.CARRIED:
                carried.setdefault(item.carrier_track_id, []).append(item.epc.value)
        persons = [self._person_view(p, cart_by_track, carried) for p in result.person_tracks]
        items = [self._item_view(i, decisions) for i in result.item_tracks]
        return ObservatoryRunState(
            run_id=self.run_id,
            scenario_id=self.scenario.scenario_id,
            scenario_name=self.scenario.name,
            seed=self.scenario.seed,
            time_s=self.time_s,
            duration_s=self.duration_s,
            step_interval_s=self.step_interval_s,
            steps=result.steps,
            finished=self.finished,
            observations_cursor=self.cursor,
            observations_total=len(self.observations),
            events_total=len(self._events()),
            persons=persons,
            items=items,
            carts=carts,
            sessions=[self._session_view(s) for s in result.sessions],
            counters=ObservatoryCounters(
                accepted=result.observations_accepted,
                dropped_duplicates=result.observations_dropped,
                out_of_order=result.observations_out_of_order,
                rejected_unknown_sensor=result.observations_rejected_unknown_sensor,
                rejected_low_confidence=result.observations_rejected_low_confidence,
                rejected_foreign_scenario=result.observations_rejected_foreign_scenario,
                rejected_spatially_inconsistent=result.observations_rejected_spatially_inconsistent,
            ),
        )

    def events(self) -> list[ObservatoryEvent]:
        with self._lock:
            return self._events()

    def _events(self) -> list[ObservatoryEvent]:
        """Unified chronological stream: track creation, item transitions, decisions, sessions."""
        result = self.pipeline.result()
        rows: list[tuple[float, int, ObservatoryEvent]] = []
        order = 0

        def add(event: ObservatoryEvent) -> None:
            nonlocal order
            rows.append((event.t_s, order, event))
            order += 1

        for person in result.person_tracks:
            add(
                ObservatoryEvent(
                    seq=0,
                    t_s=_seconds(person.created_at),
                    kind="PERSON_TRACK",
                    label="PERSON_TRACK_CREATED",
                    shopper_track_id=person.track_id,
                    confidence=person.confidence,
                )
            )
        for transition in self.pipeline.fusion.transitions:
            add(
                ObservatoryEvent(
                    seq=0,
                    t_s=_seconds(transition.timestamp),
                    kind="ITEM_TRANSITION",
                    label=_transition_label(transition.from_state, transition.to_state),
                    epc=transition.epc,
                    reason=transition.reason,
                    from_state=transition.from_state.value,
                    to_state=transition.to_state.value,
                )
            )
        for decision, event in _decisions_with_proposals(result):
            add(
                ObservatoryEvent(
                    seq=0,
                    t_s=_seconds(decision.evaluated_at),
                    kind="RETAIL_EVENT",
                    label=event.event_type.value,
                    epc=event.epc.value,
                    shopper_track_id=event.shopper_track_id,
                    counterpart_track_id=event.counterpart_track_id,
                    confidence=decision.confidence,
                    margin=decision.margin,
                    decision=decision.decision.value,
                    reason=decision.reason if decision.reason else event.reason,
                    event_id=event.event_id,
                )
            )
        for session in result.sessions:
            add(
                ObservatoryEvent(
                    seq=0,
                    t_s=_seconds(session.entered_at),
                    kind="SESSION",
                    label="SESSION_OPENED",
                    shopper_track_id=session.person_track_id,
                    reason=session.session_id,
                )
            )
            if session.exited_at is not None:
                add(
                    ObservatoryEvent(
                        seq=0,
                        t_s=_seconds(session.exited_at),
                        kind="SESSION",
                        label=f"SESSION_{session.state.value}",
                        shopper_track_id=session.person_track_id,
                        reason=session.session_id,
                    )
                )
        rows.sort(key=lambda row: (row[0], row[1]))
        return [event.model_copy(update={"seq": index}) for index, (_, _, event) in enumerate(rows)]

    def timeline(self) -> ObservatoryTimeline:
        with self._lock:
            return self._timeline()

    def _timeline(self) -> ObservatoryTimeline:
        markers = [
            ObservatoryTimelineMarker(
                t_s=e.t_s,
                label=e.label,
                epc=e.epc,
                shopper_track_id=e.shopper_track_id,
                decision=e.decision,
            )
            for e in self.events()
            if e.kind == "RETAIL_EVENT" and e.decision == "COMMIT"
        ]
        return ObservatoryTimeline(
            run_id=self.run_id,
            time_s=self.time_s,
            duration_s=self.duration_s,
            markers=markers,
            ground_truth=_ground_truth(self.scenario),
        )

    # ----------------------------------------------------------------- helpers
    def _trail(self, history: list[TrackPoint]) -> list[ObservatoryPoint]:
        recent = history[-TRAIL_POINTS:]
        return [
            ObservatoryPoint(t_s=_seconds(p.timestamp), x=p.coordinate.x, y=p.coordinate.y)
            for p in recent
        ]

    def _person_view(
        self,
        person: PersonTrack,
        cart_by_track: dict[str, str],
        carried: dict[str, list[str]],
    ) -> ObservatoryPerson:
        speed = person.velocity.horizontal_speed
        heading = (
            math.degrees(math.atan2(person.velocity.vy, person.velocity.vx))
            if speed > 0.05
            else None
        )
        return ObservatoryPerson(
            track_id=person.track_id,
            session_id=person.session_id,
            state=person.state.value,
            x=person.position.x,
            y=person.position.y,
            vx=person.velocity.vx,
            vy=person.velocity.vy,
            speed=speed,
            heading_deg=heading,
            confidence=person.confidence,
            sigma_m=person.uncertainty.horizontal_sigma,
            observation_count=person.observation_count,
            created_s=_seconds(person.created_at),
            updated_s=_seconds(person.updated_at),
            sensor_ids=list(person.contributing_sensor_ids),
            cart_id=cart_by_track.get(person.track_id),
            carried_epcs=sorted(carried.get(person.track_id, [])),
            trail=self._trail(person.history),
        )

    def _candidates(self, epc: EPC) -> list[ObservatoryCandidate]:
        ranking = self.pipeline.fusion.candidates(epc)
        if ranking is None:
            return []
        return [_candidate_view(c) for c in ranking.candidates]

    def _item_view(
        self,
        item: ItemTrack,
        decisions: list[tuple[ConfidenceDecision, RetailEvent]],
    ) -> ObservatoryItem:
        catalog = self._catalog.get(item.epc.value)
        gtin = item.gtin or (catalog.gtin if catalog else None)
        product = self._products.get(gtin) if gtin else None
        decision_view: ObservatoryDecision | None = None
        latest = _episode_decision(item, decisions)
        if latest is not None:
            decision, event = latest
            decision_view = ObservatoryDecision(
                event_id=event.event_id,
                event_type=event.event_type.value,
                decision=decision.decision.value,
                confidence=decision.confidence,
                margin=decision.margin,
                waited_s=decision.waited_seconds,
                reason=decision.reason,
                at_s=_seconds(decision.evaluated_at),
            )
        return ObservatoryItem(
            epc=item.epc.value,
            short_epc=short_epc(item.epc.value),
            gtin=gtin,
            product_name=product.name if product else None,
            sku=product.sku if product else None,
            home_fixture_id=item.home_fixture_id,
            state=item.state.value,
            state_since_s=seconds_since_epoch(item.state_since),
            x=item.position.x if item.position else None,
            y=item.position.y if item.position else None,
            sigma_m=item.uncertainty.horizontal_sigma if item.uncertainty else None,
            zone_id=item.zone_id,
            carrier_track_id=item.carrier_track_id,
            movement_start_s=seconds_since_epoch(item.movement_start_at),
            last_seen_s=seconds_since_epoch(item.last_seen_at),
            observation_count=item.observation_count,
            candidates=self._candidates(item.epc),
            decision=decision_view,
            trail=self._trail(item.history),
        )

    def _cart_view(self, cart: Cart, session_id: str | None) -> ObservatoryCart:
        lines = []
        for line in cart.lines.values():
            product = self._products.get(line.gtin) if line.gtin else None
            lines.append(
                ObservatoryCartLine(
                    epc=line.epc.value,
                    short_epc=short_epc(line.epc.value),
                    gtin=line.gtin,
                    product_name=product.name if product else None,
                    added_s=_seconds(line.added_at),
                    final_ownership_candidate=line.final_ownership_candidate,
                    exit_event_s=seconds_since_epoch(line.exit_event_at),
                )
            )
        lines.sort(key=lambda line: line.added_s)
        return ObservatoryCart(
            cart_id=cart.cart_id,
            shopper_track_id=cart.shopper_track_id,
            session_id=session_id,
            status=cart.status.value,
            exited_s=seconds_since_epoch(cart.exited_at),
            lines=lines,
        )

    @staticmethod
    def _session_view(session: ShopperSession) -> ObservatorySession:
        return ObservatorySession(
            session_id=session.session_id,
            track_id=session.person_track_id,
            state=session.state.value,
            entered_s=_seconds(session.entered_at),
            exited_s=seconds_since_epoch(session.exited_at),
            entry_boundary_id=session.entry_boundary_id,
            exit_boundary_id=session.exit_boundary_id,
        )


def _decisions_with_proposals(
    result: PipelineResult,
) -> list[tuple[ConfidenceDecision, RetailEvent]]:
    """Pair every decision with the proposal that was current when it was evaluated.

    Fusion re-proposes a deterministic event id while it WAITs, possibly with a
    different top shopper as evidence evolves; a decision must be shown with the
    attribution it actually judged, never with the final one.
    """
    proposals: dict[str, list[RetailEvent]] = {}
    for event in result.proposed_events:
        proposals.setdefault(event.event_id, []).append(event)
    paired: list[tuple[ConfidenceDecision, RetailEvent]] = []
    for decision in result.decisions:
        current: RetailEvent | None = None
        for event in proposals.get(decision.event_id, []):
            if event.timestamp <= decision.evaluated_at:
                current = event
            else:
                break
        if current is not None:
            paired.append((decision, current))
    return paired


def _episode_decision(
    item: ItemTrack, decisions: list[tuple[ConfidenceDecision, RetailEvent]]
) -> tuple[ConfidenceDecision, RetailEvent] | None:
    """Latest decision about this EPC within its current episode (movement or rest)."""
    since = item.movement_start_at or item.state_since
    latest: tuple[ConfidenceDecision, RetailEvent] | None = None
    for decision, event in decisions:
        if event.epc != item.epc:
            continue
        if since is not None and event.timestamp < since:
            continue
        if latest is None or decision.evaluated_at >= latest[0].evaluated_at:
            latest = (decision, event)
    return latest


def _cart_sessions(result: PipelineResult) -> dict[str, str]:
    """Cart id -> session id.

    The pipeline records the session each cart was committed under. A cart that has
    not committed anything yet (or one from a lifecycle the pipeline never mapped)
    falls back to the shopper's session that was open when the cart was current: the
    n-th cart of a track belongs to the n-th session of that track.
    """
    mapping = dict(result.cart_sessions)
    by_track: dict[str, list[ShopperSession]] = {}
    for session in result.sessions:
        by_track.setdefault(session.person_track_id, []).append(session)
    for sessions in by_track.values():
        sessions.sort(key=lambda s: s.entered_at)
    carts_by_track: dict[str, list[Cart]] = {}
    for cart in result.cart_state.carts.values():
        carts_by_track.setdefault(cart.shopper_track_id, []).append(cart)
    for track_id, carts in carts_by_track.items():
        carts.sort(key=_cart_ordinal)
        sessions = by_track.get(track_id, [])
        for index, cart in enumerate(carts):
            if cart.cart_id in mapping:
                continue
            if index < len(sessions):
                mapping[cart.cart_id] = sessions[index].session_id
            elif sessions:
                mapping[cart.cart_id] = sessions[-1].session_id
    return mapping


def _cart_ordinal(cart: Cart) -> int:
    """'<track>' is lifecycle 0, '<track>#<n>' is lifecycle n."""
    _, _, suffix = cart.cart_id.partition("#")
    return int(suffix) if suffix.isdigit() else 0


def _candidate_view(candidate: CandidateScore) -> ObservatoryCandidate:
    evidence = candidate.evidence
    scores = {
        "distance": evidence.distance_score,
        "distance_trend": evidence.distance_trend_score,
        "velocity": evidence.velocity_score,
        "temporal": evidence.temporal_score,
        "co_motion": evidence.co_motion_score,
        "zone": evidence.zone_score,
        "vision": evidence.vision_score,
    }
    features = [
        ObservatoryFeature(
            name=name,
            score=scores[name] if name in evidence.weights else None,
            weight=evidence.weights.get(name),
        )
        for name in FEATURE_ORDER
    ]
    return ObservatoryCandidate(
        track_id=candidate.person_track_id,
        score=candidate.score,
        features=features,
        raw_features=dict(evidence.features),
    )


def _transition_label(from_state: ItemState, to_state: ItemState) -> str:
    if to_state == ItemState.INTERACTION_CANDIDATE:
        return "INTERACTION_CANDIDATE"
    if to_state == ItemState.CARRIED:
        return "ITEM_MOVEMENT"
    if to_state == ItemState.EXITED:
        return "ITEM_EXITED"
    if from_state == ItemState.INTERACTION_CANDIDATE:
        return "ITEM_JITTER_REVERTED"
    return f"ITEM_{to_state.value}"


class RunManager:
    """Owns all in-process runs. Run ids are sequential so tests stay deterministic."""

    def __init__(self) -> None:
        self._runs: dict[str, ObservatoryRun] = {}
        self._counter = 0
        self._lock = Lock()

    def create(self, scenario_id: str, seed: int | None = None) -> ObservatoryRun:
        scenario = load_scenario(scenario_id)
        if seed is not None:
            scenario = scenario.model_copy(update={"seed": seed})
        with self._lock:
            self._counter += 1
            run_id = f"run-{self._counter:04d}"
            run = ObservatoryRun(run_id, scenario)
            self._runs[run_id] = run
        return run

    def get(self, run_id: str) -> ObservatoryRun | None:
        return self._runs.get(run_id)

    def delete(self, run_id: str) -> bool:
        with self._lock:
            return self._runs.pop(run_id, None) is not None

    def ids(self) -> list[str]:
        return sorted(self._runs)
