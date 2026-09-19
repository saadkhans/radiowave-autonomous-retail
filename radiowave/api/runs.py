"""In-process deterministic runs of the Foundation pipeline for the Observatory.

A run owns one scenario, its pre-generated observation stream and one
:class:`FoundationPipeline`. The UI drives simulated time through
``advance``/``step``/``seek``; no wall clock is involved, so the same sequence
of calls always yields the same state.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from datetime import datetime, timedelta
from threading import Condition, Lock, RLock
from typing import Protocol, runtime_checkable

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
    ObservatoryEventPage,
    ObservatoryFeature,
    ObservatoryFixture,
    ObservatoryGroundTruth,
    ObservatoryItem,
    ObservatoryLiveStatus,
    ObservatoryPerson,
    ObservatoryPoint,
    ObservatoryProduct,
    ObservatoryRunState,
    ObservatoryScenarioDetail,
    ObservatoryScenarioSummary,
    ObservatorySensor,
    ObservatorySession,
    ObservatorySnapshot,
    ObservatoryStore,
    ObservatoryTimeline,
    ObservatoryTimelineMarker,
    ObservatoryUnresolved,
    ObservatoryZone,
    RunMode,
    seconds_since_epoch,
)
from radiowave.cart.models import Cart, CartStatus, UnresolvedItem
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

log = logging.getLogger(__name__)

_RESTING = frozenset({ItemState.ON_FIXTURE, ItemState.MISPLACED, ItemState.UNKNOWN})
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


def _seconds(when: object, epoch: datetime = SCENARIO_EPOCH) -> float:
    value = seconds_since_epoch(when, epoch)  # type: ignore[arg-type]
    return value if value is not None else 0.0


class LiveModeError(RuntimeError):
    """A replay-only control (step/advance/seek/reset) was requested on a LIVE run."""


class LiveRunBusyError(RuntimeError):
    """A live run is already active, or another admission is in flight.

    One physical sensor has at most one live owner, including during creation
    (invariant 15): this is raised both when a LIVE run is already streaming and
    while another ``POST /runs/live`` is still constructing one.
    """


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
@runtime_checkable
class StoppableRun(Protocol):
    """A run that can be told to finish and then still be read.

    Capability, not class: both a hardware LIVE run and a simulated one can be
    stopped, and ``/runs/{id}/stop`` cares about that rather than about which
    concrete class is behind it. Checking the class instead is what made a simulated
    run unstoppable - and therefore impossible to switch away from, because every
    replacement path stops the active run first.
    """

    mode: RunMode

    def stop(self) -> None: ...

    def snapshot(self) -> ObservatorySnapshot: ...


class ObservatoryRun:
    """A deterministic REPLAY run over one scenario.

    The scenario-specific parts (identity, duration, observation stream, ground truth)
    are isolated behind small properties so a LIVE run (``radiowave.api.live``) can
    reuse every projection, the lock discipline and the snapshot contract unchanged.
    """

    mode: RunMode = "REPLAY"

    def __init__(
        self,
        run_id: str,
        scenario: Scenario,
        pipeline_config: PipelineConfig | None = None,
    ) -> None:
        self.scenario = scenario
        # What the scenario is *fed* (scenario 10 replays every observation twice).
        self.observations = scenario_observation_stream(scenario)
        self._init_common(run_id, scenario.store, pipeline_config)
        self.pipeline: FoundationPipeline = build_pipeline(scenario, pipeline_config)
        self.reset()

    def _init_common(
        self, run_id: str, store: Store, pipeline_config: PipelineConfig | None
    ) -> None:
        self.run_id = run_id
        self.config = pipeline_config
        self.store = store
        self.registry = StoreRegistry(store)
        # FastAPI runs sync handlers on worker threads; every mutation and every
        # snapshot of one run is serialized so replay stays deterministic.
        self._lock = RLock()
        self._catalog = {i.epc.value: i for i in store.items}
        self._products = {p.gtin: p for p in store.products}
        # View-model times are seconds after this instant.
        self.epoch_at: datetime = SCENARIO_EPOCH
        self.cursor = 0
        self.time_s = 0.0
        self.finished = False
        self.revision = 0
        self.epoch = 0

    # ---------------------------------------------------------------- identity
    @property
    def scenario_id(self) -> str:
        return self.scenario.scenario_id

    @property
    def scenario_name(self) -> str:
        return self.scenario.name

    @property
    def seed(self) -> int:
        return self.scenario.seed

    @property
    def observations_total(self) -> int:
        return len(self.observations)

    def _ground_truth_view(self) -> list[ObservatoryGroundTruth]:
        return _ground_truth(self.scenario)

    def _live_status(self) -> ObservatoryLiveStatus | None:
        return None

    def _t(self, when: object) -> float:
        return _seconds(when, self.epoch_at)

    def _t_opt(self, when: datetime | None) -> float | None:
        return seconds_since_epoch(when, self.epoch_at)

    def close(self) -> None:
        """Release external resources; a replay run holds none."""

    # ----------------------------------------------------------------- control
    def reset(self) -> None:
        with self._lock:
            self.pipeline = build_pipeline(self.scenario, self.config)
            self.cursor = 0
            self.time_s = 0.0
            self.finished = False
            self.revision += 1
            self.epoch += 1  # sequence numbers restart: a new replay build

    @property
    def lock(self) -> RLock:
        """The run's re-entrant lock, for callers that read several fields together."""
        return self._lock

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
            self.revision += 1
            if self.time_s >= self.duration_s and self.cursor >= len(self.observations):
                # The clock already stepped through the duration; finalize carts and
                # sessions without evaluating anything past the advertised end.
                self.pipeline.finish(advance=False)
                self.finished = True

    def step(self) -> None:
        self.advance(self.step_interval_s)

    def snapshot(self) -> ObservatorySnapshot:
        """State, full event stream and timeline captured under one lock."""
        with self._lock:
            events = self._events()
            return ObservatorySnapshot(
                state=self._state(),
                events=ObservatoryEventPage(
                    run_id=self.run_id,
                    epoch=self.epoch,
                    events=events,
                    next_seq=len(events),
                    total=len(events),
                ),
                timeline=self._timeline(),
            )

    def apply(self, operation: Callable[[], None]) -> ObservatorySnapshot:
        """Run one mutation and capture the complete snapshot under the same lock.

        A response must describe the request that produced it, never a concurrent
        client's reset or seek that slipped in between the mutation and the snapshot.
        """
        with self._lock:
            operation()
            return self.snapshot()

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
        session_by_track = {p.track_id: p.session_id for p in result.person_tracks}
        cart_by_track = {
            cart.shopper_track_id: cart.cart_id
            for cart in result.cart_state.carts.values()
            if cart.status == CartStatus.OPEN
            and cart_sessions.get(cart.cart_id) == session_by_track.get(cart.shopper_track_id)
        }
        carried: dict[str, list[str]] = {}
        for item in result.item_tracks:
            if item.carrier_track_id and item.state == ItemState.CARRIED:
                carried.setdefault(item.carrier_track_id, []).append(item.epc.value)
        persons = [self._person_view(p, cart_by_track, carried) for p in result.person_tracks]
        episode_starts = self._episode_starts()
        items = [
            self._item_view(i, decisions, episode_starts.get(i.epc.value))
            for i in result.item_tracks
        ]
        return ObservatoryRunState(
            run_id=self.run_id,
            revision=self.revision,
            epoch=self.epoch,
            mode=self.mode,
            scenario_id=self.scenario_id,
            scenario_name=self.scenario_name,
            seed=self.seed,
            time_s=self.time_s,
            duration_s=self.duration_s,
            step_interval_s=self.step_interval_s,
            steps=result.steps,
            finished=self.finished,
            observations_cursor=self.cursor,
            observations_total=self.observations_total,
            events_total=len(self._events()),
            live=self._live_status(),
            persons=persons,
            items=items,
            carts=carts,
            unresolved=[self._unresolved_view(u) for u in result.cart_state.unresolved.values()],
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
                    t_s=self._t(person.created_at),
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
                    t_s=self._t(transition.timestamp),
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
                    t_s=self._t(decision.evaluated_at),
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
                    t_s=self._t(session.entered_at),
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
                        t_s=self._t(session.exited_at),
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
            # Settled adjudications only: COMMIT and terminal REVIEW, never the
            # repetitive intermediate WAIT rows.
            if e.kind == "RETAIL_EVENT" and e.decision in ("COMMIT", "REVIEW")
        ]
        return ObservatoryTimeline(
            run_id=self.run_id,
            time_s=self.time_s,
            duration_s=self.duration_s,
            markers=markers,
            ground_truth=self._ground_truth_view(),
        )

    # ----------------------------------------------------------------- helpers
    def _trail(self, history: list[TrackPoint]) -> list[ObservatoryPoint]:
        recent = history[-TRAIL_POINTS:]
        return [
            ObservatoryPoint(t_s=self._t(p.timestamp), x=p.coordinate.x, y=p.coordinate.y)
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
            created_s=self._t(person.created_at),
            updated_s=self._t(person.updated_at),
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

    def _episode_starts(self) -> dict[str, datetime]:
        """EPC -> when it last left its fixture, from the state machine's transition log.

        ``ItemTrack.movement_start_at`` is cleared once the item settles (MISPLACED, or
        back ON_FIXTURE), so the trail of a settled item needs the transition log to be
        trimmed to its movement episode.
        """
        starts: dict[str, datetime] = {}
        for transition in self.pipeline.fusion.transitions:
            # Leaving any resting state (ON_FIXTURE or MISPLACED) starts a movement
            # episode; the initial UNKNOWN -> rest classification is not a departure.
            if transition.from_state in _RESTING and transition.to_state not in _RESTING:
                starts[transition.epc] = transition.timestamp
        return starts

    def _item_view(
        self,
        item: ItemTrack,
        decisions: list[tuple[ConfidenceDecision, RetailEvent]],
        left_fixture_at: datetime | None,
    ) -> ObservatoryItem:
        # The state machine keeps the true start of the latest movement episode even
        # after the item settles; the transition log is the fallback.
        tracked = self.pipeline.fusion.items.get(item.epc)
        remembered = tracked.last_movement_start_at if tracked is not None else None
        episode_start = item.movement_start_at or remembered or left_fixture_at
        history = item.history
        if episode_start is not None and item.state not in (
            ItemState.ON_FIXTURE,
            ItemState.UNKNOWN,
        ):
            history = [p for p in history if p.timestamp >= episode_start]
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
                at_s=self._t(decision.evaluated_at),
            )
        return ObservatoryItem(
            epc=item.epc.value,
            short_epc=short_epc(item.epc.value),
            gtin=gtin,
            product_name=product.name if product else None,
            sku=product.sku if product else None,
            home_fixture_id=item.home_fixture_id,
            state=item.state.value,
            state_since_s=self._t_opt(item.state_since),
            x=item.position.x if item.position else None,
            y=item.position.y if item.position else None,
            sigma_m=item.uncertainty.horizontal_sigma if item.uncertainty else None,
            zone_id=item.zone_id,
            carrier_track_id=item.carrier_track_id,
            movement_start_s=self._t_opt(item.movement_start_at),
            episode_start_s=self._t_opt(episode_start),
            last_seen_s=self._t_opt(item.last_seen_at),
            observation_count=item.observation_count,
            candidates=self._candidates(item.epc),
            decision=decision_view,
            trail=self._trail(history),
        )

    def _unresolved_view(self, entry: UnresolvedItem) -> ObservatoryUnresolved:
        catalog = self._catalog.get(entry.epc.value)
        gtin = catalog.gtin if catalog else None
        product = self._products.get(gtin) if gtin else None
        return ObservatoryUnresolved(
            epc=entry.epc.value,
            short_epc=short_epc(entry.epc.value),
            gtin=gtin,
            product_name=product.name if product else None,
            reason=entry.reason,
            source_event_id=entry.source_event_id,
            t_s=self._t(entry.timestamp),
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
                    added_s=self._t(line.added_at),
                    final_ownership_candidate=line.final_ownership_candidate,
                    exit_event_s=self._t_opt(line.exit_event_at),
                )
            )
        lines.sort(key=lambda line: line.added_s)
        return ObservatoryCart(
            cart_id=cart.cart_id,
            shopper_track_id=cart.shopper_track_id,
            session_id=session_id,
            status=cart.status.value,
            exited_s=self._t_opt(cart.exited_at),
            lines=lines,
        )

    def _session_view(self, session: ShopperSession) -> ObservatorySession:
        return ObservatorySession(
            session_id=session.session_id,
            track_id=session.person_track_id,
            state=session.state.value,
            entered_s=self._t(session.entered_at),
            exited_s=self._t_opt(session.exited_at),
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


_SHUTDOWN_SETTLE_TIMEOUT_S = 10.0
"""How long ``close_all`` waits for an in-flight LIVE admission to settle."""


class RunManager:
    """Owns all in-process runs. Run ids are sequential so tests stay deterministic."""

    def __init__(self) -> None:
        self._runs: dict[str, ObservatoryRun] = {}
        self._counter = 0
        self._lock = Lock()
        # The run id currently being constructed for a LIVE admission, if any; see
        # ``build_live_exclusive``. Held only between reserving the slot and either
        # publishing the run or releasing the slot on failure.
        self._live_reservation: str | None = None
        # The run object itself, once ``factory()`` has returned but before it is
        # published to ``_runs``. Without this, a run whose construction is still in
        # progress (session started, driver thread up, snapshot in flight) is
        # invisible to ``close_all()``: shutdown would return with the reader thread
        # still running, and the admission would go on to publish a run after the
        # manager was already closed (invariant 16).
        self._live_constructing: ObservatoryRun | None = None
        # The run id of a LIVE run whose ``delete()`` has already popped it from
        # ``_runs`` but whose ``run.close()`` has not returned yet. The physical
        # sensor is not free until close() actually joins the reader thread and
        # tears down the transport, so this keeps the slot held for the whole
        # teardown window (invariant 15) even though the run is no longer
        # discoverable via ``get``/``ids``. See ``delete``.
        self._closing_live_run_id: str | None = None
        # Set when a deleted LIVE run's teardown could NOT free the sensor (its reader
        # thread never exited). Unlike ``_closing_live_run_id`` this never clears: it
        # marks the slot as permanently held so the operator is told to restart rather
        # than to retry. See ``delete``.
        self._stuck_live_run_id: str | None = None
        # Signalled whenever ``_live_reservation`` is released. ``close_all`` waits on
        # it so shutdown cannot return while an admission is still inside
        # ``factory(run_id)`` — at that point ``_live_constructing`` is still None even
        # though the constructor may already have started its sensor thread, so
        # shutdown would otherwise promise "no reader thread survives" while one was
        # being created behind its back (invariant 16).
        self._reservation_settled = Condition(self._lock)
        # Set by ``close_all()``; once true no new LIVE admission may start, and any
        # admission already past that point must close its run instead of publishing.
        self._closed = False

    def _build(
        self, scenario_id: str, seed: int | None = None
    ) -> tuple[ObservatoryRun, ObservatorySnapshot]:
        """Allocate a run and capture its initial snapshot before publishing it.

        A concurrent client that discovers or predicts a run id (``GET /runs``, sequential
        ids) must never be able to step/seek/reset it before its t=0 snapshot has been
        captured: that would let ``POST /runs`` observe a non-initial revision despite its
        atomic-initial-snapshot contract. The run is only added to ``_runs`` (making it
        visible to ``get``/``ids``, and therefore mutable by anyone else) after its
        snapshot has already been read, so no other caller can ever reach it first. The
        manager lock itself is only held for the id allocation and for the publish step,
        never across ``run.snapshot()``: that call takes the run's own lock and can run
        concurrently with unrelated ``get``/``ids`` calls on other runs. A ``delete`` of
        the predicted id inside that window simply reports 404 (nothing to delete yet);
        the guarantee is about mutation of the run's state, not about its id being
        unguessable.
        """
        scenario = load_scenario(scenario_id)
        if seed is not None:
            scenario = scenario.model_copy(update={"seed": seed})
        return self.build_with(lambda run_id: ObservatoryRun(run_id, scenario))

    def build_with(
        self, factory: Callable[[str], ObservatoryRun]
    ) -> tuple[ObservatoryRun, ObservatorySnapshot]:
        """Allocate an id, build the run, snapshot it, then publish it (see ``_build``).

        Shared by replay and live runs so both keep the atomic-initial-snapshot contract.
        """
        with self._lock:
            self._counter += 1
            run_id = f"run-{self._counter:04d}"
        run = factory(run_id)
        snapshot = run.snapshot()  # captured before anyone can see the run
        with self._lock:
            self._runs[run_id] = run  # published only now
        return run, snapshot

    def create(self, scenario_id: str, seed: int | None = None) -> ObservatoryRun:
        return self._build(scenario_id, seed)[0]

    def create_with_snapshot(
        self, scenario_id: str, seed: int | None = None
    ) -> ObservatorySnapshot:
        """Create a run and return its initial snapshot; the run is discoverable only after."""
        return self._build(scenario_id, seed)[1]

    def build_live_exclusive(
        self, factory: Callable[[str], ObservatoryRun]
    ) -> tuple[ObservatoryRun, ObservatorySnapshot]:
        """Atomically admit at most one LIVE run: reserve the slot, build outside the
        lock, then publish (see ``build_with``) — or release the slot on any failure.

        ``build_with`` alone leaves a race for LIVE runs: two concurrent callers can
        both pass an ``availability()`` pre-check and both start a
        ``TiLiveSession`` for the same physical sensor before either is published
        (invariant 15). The reservation closes that window: it is taken under the
        manager lock *before* the (slow, thread-starting) factory call and is only
        ever cleared after the run is published or immediately on failure, so a
        partial construction failure never leaves the sensor permanently
        unavailable (invariant 16). The manager lock itself is still never held
        across ``factory()``/``run.snapshot()``, exactly as in ``build_with``.
        """
        with self._lock:
            if self._closed:
                msg = "manager is shutting down"
                raise LiveRunBusyError(msg)
            active = self._active_live_run_id_locked()
            if active is not None:
                if active == self._closing_live_run_id:
                    # Deleted, but its teardown has not released the sensor. If the
                    # reader thread refused to exit, the slot is held deliberately and
                    # for good (see ``delete``) -- telling the operator to "try again
                    # shortly" would be a lie, since retrying can never succeed.
                    if self._stuck_live_run_id == active:
                        msg = (
                            f"live run {active} could not release the sensor; its "
                            "reader thread did not exit, so the sensor stays reserved "
                            "until the API process is restarted"
                        )
                    else:
                        msg = (
                            f"live run {active} is still releasing the sensor; "
                            "try again shortly"
                        )
                else:
                    msg = f"live run {active} is already using the sensor; stop it first"
                raise LiveRunBusyError(msg)
            if self._live_reservation is not None:
                msg = "a live run is starting; try again shortly"
                raise LiveRunBusyError(msg)
            self._counter += 1
            run_id = f"run-{self._counter:04d}"
            self._live_reservation = run_id
        run: ObservatoryRun | None = None
        try:
            run = factory(run_id)
            # Visible to ``close_all()`` from the instant construction finishes, even
            # though it is not yet in ``_runs``: a shutdown racing the snapshot below
            # must still be able to stop it.
            with self._lock:
                self._live_constructing = run
                # Wakes a ``close_all`` waiting for exactly this: the factory has
                # returned, so the run (and any sensor thread it started) is now
                # reachable from shutdown.
                self._reservation_settled.notify_all()
            snapshot = run.snapshot()  # captured before anyone can see the run
        except BaseException:
            if run is not None:
                try:
                    run.close()
                except Exception:
                    log.exception("failed to release live run %s after admission failure", run_id)
            with self._lock:
                self._live_reservation = None
                self._live_constructing = None
                self._reservation_settled.notify_all()
            raise
        with self._lock:
            if self._closed:
                # Shutdown happened while the snapshot was in flight: never publish a
                # run after the manager has promised no reader thread survives it.
                # The reservation is deliberately still held here and released only
                # after ``run.close()`` below, so a ``close_all`` waiting on it cannot
                # return while this run's reader thread is still being torn down.
                shutting_down = True
            else:
                self._runs[run_id] = run  # published only now
                self._live_reservation = None
                self._live_constructing = None
                self._reservation_settled.notify_all()
                shutting_down = False
        if shutting_down:
            try:
                run.close()
            except Exception:
                log.exception("failed to release live run %s after manager shutdown", run_id)
            finally:
                with self._lock:
                    self._live_reservation = None
                    self._live_constructing = None
                    self._reservation_settled.notify_all()
            msg = "manager is shutting down"
            raise LiveRunBusyError(msg)
        return run, snapshot

    def live_reservation(self) -> str | None:
        """The run id currently being constructed for a LIVE admission, if any."""
        with self._lock:
            return self._live_reservation

    def closing_live_run_id(self) -> str | None:
        """The run id currently being torn down by ``delete()``, if any.

        Distinct from ``live_reservation`` (construction, before a run exists) and
        from a published-but-not-yet-finished run in ``_runs``: this covers the
        window after ``delete()`` has removed the run from ``_runs`` but before its
        ``close()`` has actually stopped the sensor. See ``delete``.
        """
        with self._lock:
            return self._closing_live_run_id

    def get(self, run_id: str) -> ObservatoryRun | None:
        with self._lock:
            return self._runs.get(run_id)

    def _active_live_run_id_locked(self) -> str | None:
        """First LIVE run that is not finished ("active" = not stopped); caller holds the lock.

        Also reports a run whose ``delete()`` has already popped it from ``_runs``
        but whose ``close()`` has not returned yet (see ``_closing_live_run_id``):
        the physical sensor is not free until then, so admission must still refuse a
        concurrent ``build_live_exclusive`` for that whole window.
        """
        if self._closing_live_run_id is not None:
            return self._closing_live_run_id
        for run_id, run in sorted(self._runs.items()):
            if run.mode == "LIVE" and not run.finished:
                return run_id
        return None

    def live_run_ids(self) -> list[str]:
        with self._lock:
            return sorted(
                run_id
                for run_id, run in self._runs.items()
                if run.mode == "LIVE" and not run.finished
            )

    def delete(self, run_id: str) -> bool:
        """Remove a run and release its resources.

        For a LIVE run that is still active, the slot must stay held for the whole
        teardown (invariant 15): popping from ``_runs`` and then closing outside the
        lock (deliberately, so a live run's thread joins never block the manager
        lock) would otherwise leave a window where ``_active_live_run_id_locked``
        reports no active run and no reservation while the old reader thread, serial
        stream and raw capture are still alive — long enough for a concurrent
        ``POST /runs/live`` to pass admission and open a second session against the
        same UART. ``_closing_live_run_id`` closes that window: set before the run is
        popped, cleared only after ``run.close()`` returns (in a ``finally``, so a
        ``close()`` that raises still releases the slot rather than sticking it
        forever).
        """
        with self._lock:
            run = self._runs.pop(run_id, None)
            if run is None:
                return False
            closing = run.mode == "LIVE" and not run.finished
            if closing:
                self._closing_live_run_id = run_id
        try:
            run.close()  # outside the manager lock: a live run joins its threads here
        finally:
            if closing:
                with self._lock:
                    # Release the slot only if the run actually finished. A LIVE run
                    # whose ``stop()`` could not join its reader thread leaves
                    # ``finished`` False on purpose: that thread may still be holding
                    # the UART and enqueueing, so the slot stays held rather than
                    # letting a second session open the same port. The run is already
                    # out of ``_runs``, so this is the only thing still standing
                    # between a stuck reader and a duplicate session; it is a
                    # deliberate fail-closed, and it means the sensor stays
                    # unavailable until the process is restarted. Preferring that to
                    # two readers on one UART is the whole point of invariant 15.
                    if run.finished:
                        self._closing_live_run_id = None
                    else:
                        self._stuck_live_run_id = run_id
                        log.error(
                            "live run %s did not finish stopping; keeping the sensor "
                            "reserved rather than admitting another session",
                            run_id,
                        )
        return True

    def close_all(self) -> None:
        """Stop every run (live sessions included); used on application shutdown.

        Also stops a LIVE run still under construction (``factory()`` has returned
        but the admission has not published it yet) and refuses any further
        admissions from now on, so shutdown never leaves a reader thread behind
        (invariant 16) even when it races ``build_live_exclusive``.
        """
        with self._lock:
            self._closed = True
            # The blind spot is an admission still INSIDE ``factory(run_id)``: it
            # holds a reservation, ``_live_constructing`` is not set yet, and its
            # constructor may already have started a sensor thread — so shutdown would
            # return promising no reader thread survives while one was being created
            # behind its back. Wait only for that window to close. Once
            # ``_live_constructing`` is set the run is reachable from here and the
            # existing path handles it, so waiting further (e.g. on an in-flight
            # ``snapshot()``) would be pointless and could stall shutdown. Bounded,
            # because a wedged constructor must not hang process shutdown for ever; if
            # it expires we carry on and say so rather than blocking.
            if not self._reservation_settled.wait_for(
                lambda: self._live_reservation is None or self._live_constructing is not None,
                timeout=_SHUTDOWN_SETTLE_TIMEOUT_S,
            ):
                log.error(
                    "live admission %s did not settle within %.0fs; shutting down "
                    "without it",
                    self._live_reservation,
                    _SHUTDOWN_SETTLE_TIMEOUT_S,
                )
            runs = list(self._runs.values())
            self._runs.clear()
            constructing = self._live_constructing
            self._live_constructing = None
        for run in runs:
            run.close()
        if constructing is not None:
            constructing.close()

    def ids(self) -> list[str]:
        with self._lock:
            return sorted(self._runs)
