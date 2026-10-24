"""Deterministic execution of a :class:`LabScenario` through the production stack.

This module is the one place where the virtual store is wired to real Foundation
code, so it is also the one place where the phase's central invariant can be lost.
That invariant, stated once here:

    The simulator produces INPUT. Production code produces INTERPRETATION.

Concretely, per tick:

    world truth
      -> virtual TI radar -> real UART bytes -> real ``TiFrameParser``
                          -> real ``TiTargetNormalizer`` -> ``PersonObservation``
      -> virtual RFID     -> ``NativeRfidRead`` -> real ``ObservationNormalizer``
                          -> ``ItemObservation``
      -> ``FoundationPipeline.ingest``

Nothing here hands the pipeline a fact it could not have inferred from a sensor.
``GroundTruthLog`` is carried alongside the run and returned for evaluation, but it
is never read by the pipeline, fusion, confidence or the cart engine — if it were,
we would be scoring an answer key rather than an algorithm.

**Determinism.** This runner is the authoritative reproducibility oracle for Phase 4:
it is synchronous, single-threaded, and stamps every observation with *simulated*
time (``received_at=<sim clock>``), so the host wall clock and thread scheduling
cannot influence a result. The live-Observatory path (a ``stream_factory`` feeding
``TiLiveSession``) consumes the same bytes but batches them on a reader thread, so it
is deliberately NOT the determinism guarantee — it exists to satisfy the
"through the live Observatory" acceptance gate, and that distinction is documented
rather than papered over.

The seed varies observation, not truth: scripted motion is seed-invariant unless
jitter is deliberately applied, so one physical scenario can be replayed against many
noise realizations and a metric change is attributable to the algorithm.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from radiowave.adapters.mmwave.ti.adapter import TiTargetNormalizer
from radiowave.adapters.mmwave.ti.config import TiAdapterConfig
from radiowave.adapters.mmwave.ti.parser import TiFrameParser
from radiowave.adapters.mmwave.ti.session import firmware_profile_for, parser_limits_for
from radiowave.contracts.observations import AnyObservation
from radiowave.contracts.store import Sensor, SourceType
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.interfaces import Recorder
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.pipeline import FoundationPipeline, PipelineConfig, PipelineResult
from radiowave.simulator.lab import actors, interactions
from radiowave.simulator.lab.ground_truth import GroundTruthEventType, GroundTruthLog
from radiowave.simulator.lab.scenarios import (
    FaultKind,
    InteractionVerb,
    LabScenario,
    ScheduledInteraction,
)
from radiowave.simulator.lab.sensors.rfid import (
    RfidAntennaZoneConfig,
    RfidReaderEmulator,
    RfidReaderEmulatorConfig,
    RfidReadNoiseConfig,
    TaggedItemState,
)
from radiowave.simulator.lab.sensors.ti_radar import (
    TiRadarEmulator,
    TiRadarEmulatorConfig,
    VisibleTarget,
)
from radiowave.simulator.lab.world import (
    ActorMotionState,
    ItemState,
    VirtualWorld,
    WorldConfig,
)


@dataclass(frozen=True, slots=True)
class LabRunResult:
    """Everything a caller needs to evaluate a run — truth and prediction side by side.

    They are deliberately separate attributes rather than a merged view: the pipeline
    result is what the system concluded, ``ground_truth`` is what actually happened,
    and keeping them apart is what makes an honest comparison possible.
    """

    scenario_id: str
    seed: int
    ticks: int
    pipeline: PipelineResult
    ground_truth: GroundTruthLog
    #: Counts kept for sanity checks and metrics; cheap, and they make a silently
    #: empty run (the most misleading possible outcome) obvious immediately.
    radar_bytes: int = 0
    frames_parsed: int = 0
    frames_rejected: int = 0
    person_observations: int = 0
    item_observations: int = 0
    observations_ingested: int = 0
    observations_rejected: int = 0
    faults_applied: list[str] = field(default_factory=list)


class LabEngine:
    """Runs one :class:`LabScenario` end to end, synchronously and deterministically."""

    def __init__(
        self,
        scenario: LabScenario,
        *,
        pipeline_config: PipelineConfig | None = None,
        recorder: Recorder | None = None,
    ) -> None:
        self.scenario = scenario
        self.registry = StoreRegistry(scenario.store)
        self.world = VirtualWorld(
            WorldConfig(tick_hz=scenario.tick_hz, seed=scenario.seed), scenario.store
        )
        self.ground_truth = GroundTruthLog()

        # --- the real Foundation stack, unmodified ---------------------------------
        self.pipeline = FoundationPipeline(
            self.registry, pipeline_config, scenario_id=scenario.scenario_id, recorder=recorder
        )
        self.observation_normalizer = ObservationNormalizer(
            self.registry, scenario_id=scenario.scenario_id
        )

        # --- the radar boundary ----------------------------------------------------
        radar_sensor = self._sensor_of(SourceType.MMWAVE)
        self._radar_sensor_id = radar_sensor.sensor_id
        adapter_config = TiAdapterConfig(sensor_id=radar_sensor.sensor_id)
        # Parser and normalizer are the production classes, configured exactly as the
        # live session configures them (``firmware_profile_for``/``parser_limits_for``),
        # so the simulator cannot accidentally run a laxer parser than hardware would.
        self._parser = TiFrameParser(
            profile=firmware_profile_for(adapter_config),
            limits=parser_limits_for(adapter_config),
        )
        self._ti_normalizer = TiTargetNormalizer(
            adapter_config, self.registry, scenario_id=scenario.scenario_id
        )
        self._radar = TiRadarEmulator(
            TiRadarEmulatorConfig(
                sensor_id=radar_sensor.sensor_id,
                pose=radar_sensor.pose,
                coordinates=adapter_config.coordinates,
                noise=scenario.sensors.radar_noise,
                fov=scenario.sensors.radar_fov,
                seed=scenario.seed,
            )
        )

        # --- the RFID boundary -----------------------------------------------------
        antennas = [
            RfidAntennaZoneConfig(
                sensor_id=sensor.sensor_id,
                position=sensor.pose.position,
                pose=sensor.pose,
                antenna_port=sensor.vendor_metadata.get("port", sensor.sensor_id),
            )
            for sensor in scenario.store.sensors
            if sensor.modality is SourceType.RFID
        ]
        reliability = scenario.sensors.rfid
        reliability_noise = RfidReadNoiseConfig(
            base_read_probability=reliability.read_probability,
            missed_read_probability=reliability.missed_read_rate,
            bleed_probability=reliability.bleed_rate,
            # Lab runs need item LOCATION evidence, because Foundation's fusion is
            # built around an RFID adapter supplying a zone-scale coordinate (its own
            # synthetic reads carry ~0.5 m sigma). With no estimate at all the pipeline
            # tracks items but can never infer that one moved, so every scenario
            # proposes zero events - a starved simulator, not a working one. The blur
            # stays honest at rack scale.
            localization_enabled=True,
        )
        self._rfid = RfidReaderEmulator(
            RfidReaderEmulatorConfig(
                antennas=antennas, noise=reliability_noise, seed=scenario.seed
            )
        )

        # The advertised reader cadence, used to pace polling (see _sense_rfid).
        self._rfid_rate_hz = float(reliability_noise.read_rate_hz)
        self._pending = sorted(scenario.scheduled_interactions, key=lambda i: i.t)
        self._faults_applied: list[str] = []
        self._radar_restarts_done: set[float] = set()
        self._id_reuse_forced = False
        self._pending_waypoints: dict[str, list[tuple[float, tuple[float, float]]]] = {}
        self._departed: set[str] = set()
        self._next_rfid_poll_s = 0.0
        self._last_frame_at_s = 0.0
        # Run counters live on the instance so a caller can step the simulation
        # incrementally (the Observatory SIM run) and still get a coherent result.
        self._radar_bytes = 0
        self._person_obs = 0
        self._item_obs = 0
        self._ingested = 0
        self._rejected = 0

    # ------------------------------------------------------------------ helpers
    def _sensor_of(self, modality: SourceType) -> Sensor:
        for sensor in self.scenario.store.sensors:
            if sensor.modality is modality:
                return sensor
        msg = f"scenario store has no {modality.value} sensor"
        raise ValueError(msg)

    def _elapsed_s(self) -> float:
        return self.world.tick / self.scenario.tick_hz

    def _fault_active(self, kind: FaultKind, now_s: float, sensor_id: str | None = None) -> bool:
        """Is this fault active now, for this sensor?

        ``sensor_id`` matters: a fault that names one read point must darken only
        that read point. Ignoring it turned ``08_rfid_dropout`` - declared against a
        single rack antenna - into a store-wide RFID blackout, so the scenario
        measured something far more severe than the outage it describes.
        """
        for fault in self.scenario.fault_injections:
            if fault.kind is not kind or not (fault.start_t <= now_s < fault.end_t):
                continue
            if fault.sensor_id is None or sensor_id is None or fault.sensor_id == sensor_id:
                return True
        return False

    def _apply_interaction(self, item: ScheduledInteraction) -> None:
        """Execute one scripted action against PHYSICAL world state.

        Note what this does NOT do: it never emits a retail event into the pipeline.
        A PICK moves an item from a rack into a shopper's hands and is recorded in the
        ground-truth log; whether the system notices is entirely up to fusion reading
        noisy sensor output. That asymmetry is the whole point of the exercise.
        """
        world, log = self.world, self.ground_truth
        script = next(s for s in self.scenario.shoppers if s.shopper_id == item.shopper_id)
        verb = item.verb
        if verb is InteractionVerb.ENTER:
            entry = (script.waypoints[0].x, script.waypoints[0].y)
            interactions.enter(world, log, entry, item.shopper_id, script.speed_mps)
            # Later waypoints are QUEUED against their own ``t`` rather than loaded
            # now. Loading the whole route at entry made the shopper sprint it
            # immediately, and the next APPROACH_FIXTURE then overwrote whatever was
            # left - so the shopper stood at the rack for the rest of the run instead
            # of carrying the item along the declared trajectory, and the radar and
            # RFID evidence described a route the scenario never asked for.
            self._pending_waypoints[item.shopper_id] = [
                (w.t, (w.x, w.y)) for w in script.waypoints[1:]
            ]
        elif verb is InteractionVerb.APPROACH_FIXTURE:
            assert item.fixture_id is not None  # guaranteed by LabScenario validation
            interactions.approach_fixture(
                world, log, item.shopper_id, item.fixture_id, speed_mps=script.speed_mps
            )
        elif verb is InteractionVerb.PICK:
            assert item.epc is not None
            interactions.pick(world, log, item.epc, item.shopper_id)
        elif verb is InteractionVerb.CARRY:
            assert item.epc is not None
            interactions.carry(world, log, item.epc)
        elif verb is InteractionVerb.PUTBACK:
            assert item.epc is not None
            interactions.putback(world, log, item.epc, item.fixture_id)
        elif verb is InteractionVerb.MISPLACE:
            assert item.epc is not None and item.fixture_id is not None
            interactions.misplace(world, log, item.epc, item.fixture_id)
        elif verb is InteractionVerb.HANDOFF:
            assert item.epc is not None and item.counterpart_shopper_id is not None
            interactions.handoff(world, log, item.epc, item.counterpart_shopper_id)
        elif verb is InteractionVerb.EXIT:
            last = script.waypoints[-1]
            interactions.exit_store(
                world, log, item.shopper_id, (last.x, last.y), script.speed_mps
            )

    def _release_due_waypoints(self, now_s: float) -> None:
        """Hand a shopper the next scripted waypoint once its time has come."""
        for shopper_id, queued in self._pending_waypoints.items():
            actor = self.world.shoppers.get(shopper_id)
            if actor is None or not queued:
                continue
            # Once a shopper is leaving, the scripted route is over. Releasing a
            # later waypoint would call waypoint_walk and reset the actor out of
            # EXITING back to WALKING, so arrival at the door would register as an
            # ordinary stop and the departure - and with it EXIT_WITH_ITEM truth -
            # would never be recorded at all.
            if actor.state in (ActorMotionState.EXITING, ActorMotionState.EXITED):
                continue
            due = [point for when, point in queued if when <= now_s]
            if due:
                self._pending_waypoints[shopper_id] = [
                    (when, point) for when, point in queued if when > now_s
                ]
                actors.waypoint_walk(actor, due, actor.speed_mps)

    def _record_departures(self) -> None:
        """Record EXIT / EXIT_WITH_ITEM at the moment the boundary is actually crossed.

        ``exit_store`` only starts the walk; the shopper stays present - and visible to
        both sensors - until they arrive. Truth is therefore stamped here, on the
        present -> departed transition, so the answer key agrees with when the evidence
        could first have supported it.
        """
        for shopper_id, actor in self.world.shoppers.items():
            if actor.present or shopper_id in self._departed:
                continue
            self._departed.add(shopper_id)
            self.ground_truth.record_event(
                self.world, GroundTruthEventType.EXIT, ground_truth_person_id=shopper_id
            )
            for epc in actor.carried_epcs:
                self.ground_truth.record_event(
                    self.world,
                    GroundTruthEventType.EXIT_WITH_ITEM,
                    ground_truth_person_id=shopper_id,
                    epc=epc,
                )

    # --------------------------------------------------------------- sensing
    def _sense_radar(self, now: datetime, now_s: float) -> list[AnyObservation]:
        """World truth -> TI bytes -> REAL parser -> REAL normalizer.

        The bytes are genuinely serialized and re-parsed rather than short-circuited.
        That costs a little time per tick and buys the thing this phase exists for:
        the production parser, its bounds checks, its reject reasons and its
        generation/identity semantics are all exercised before hardware exists.
        """
        if self._fault_active(FaultKind.RADAR_DROPOUT, now_s):
            return []

        # A scheduled restart resets the stream once, which makes the frame number go
        # backwards exactly as a real device reboot does; the normalizer notices and
        # opens a new generation, retiring the old native-id space.
        for fault in self.scenario.fault_injections:
            if (
                fault.kind is FaultKind.RADAR_RESTART
                and fault.start_t <= now_s
                and fault.start_t not in self._radar_restarts_done
            ):
                self._radar.reset_stream()
                self._parser.reset()
                self._radar_restarts_done.add(fault.start_t)
                self._faults_applied.append(f"RADAR_RESTART@{fault.start_t}")

        # A scheduled NATIVE_ID_REUSE window makes the radar recycle a retired native
        # id rather than waiting for chance: the case worth exercising is a departing
        # shopper's id being handed to an arriving one, which must NOT let stale
        # identity survive - Phase 3's generation scoping is what stops it.
        reuse_now = self._fault_active(FaultKind.NATIVE_ID_REUSE, now_s)
        if reuse_now != self._id_reuse_forced:
            self._radar.set_forced_id_reuse(reuse_now)
            self._id_reuse_forced = reuse_now
            if reuse_now:
                self._faults_applied.append(f"NATIVE_ID_REUSE@{now_s}")

        targets = [
            VisibleTarget(
                ground_truth_id=actor.ground_truth_person_id,
                position=actor.position,
                velocity=actor.velocity,
            )
            for actor in self.world.shoppers.values()
            if actor.present
        ]
        data = self._radar.emit(targets, now)
        if data:
            self._last_frame_at_s = now_s
        self._radar_bytes += len(data)
        observations: list[AnyObservation] = []
        for frame in self._parser.feed(data):
            observations.extend(self._ti_normalizer.frame_to_observations(frame, received_at=now))
        return observations

    def _sense_rfid(self, now: datetime, now_s: float) -> list[AnyObservation]:
        # Poll at the reader's CONFIGURED cadence, not once per world tick. At the
        # catalog's 20 Hz tick and a 2 Hz reader, polling every tick produced ten
        # times the independent localized estimates the reader claims to deliver -
        # each one a fresh blur of the true position, so averaging them recovered far
        # more certainty than the advertised rate can justify. That silently made the
        # evidence better than the hardware assumption it is meant to stand in for.
        if now_s + 1e-9 < self._next_rfid_poll_s:
            return []
        rate_hz = self._rfid_rate_hz
        self._next_rfid_poll_s = now_s + (1.0 / rate_hz if rate_hz > 0 else 0.0)

        tagged = [
            TaggedItemState(epc=state.epc, position=state.position, carrier_id=state.carrier_id)
            for state in self.world.items.values()
            if self._still_in_store(state)
        ]
        return [
            self.observation_normalizer.item(read)
            for read in self._rfid.reads(tagged, now)
            # Filtered per read rather than per poll: only the named antenna goes
            # dark, so the rest of the reader keeps working as the scenario declares.
            if not self._fault_active(FaultKind.RFID_DROPOUT, now_s, read.sensor_id)
        ]

    def _still_in_store(self, state: ItemState) -> bool:
        """False once an item's carrier has physically left the building.

        A tag that has gone out of the door cannot keep answering antennas inside it.
        Leaving departed merchandise in the poll kept generating in-store evidence for
        items that had left, which corrupts exactly the exit and cart evaluation the
        long scenarios exist to measure.
        """
        if state.carrier_id is None:
            return True
        carrier = self.world.shoppers.get(state.carrier_id)
        return carrier is None or carrier.present

    # ------------------------------------------------------------------- run
    @property
    def total_ticks(self) -> int:
        return round(self.scenario.duration_s * self.scenario.tick_hz)

    @property
    def radar_suppressed(self) -> bool:
        """True while a configured RADAR_DROPOUT window is open (for live status)."""
        return self._fault_active(FaultKind.RADAR_DROPOUT, self._elapsed_s())

    @property
    def last_frame_at_s(self) -> float:
        """Elapsed simulated seconds at the last emitted radar frame."""
        return self._last_frame_at_s

    @property
    def exhausted(self) -> bool:
        return self.world.tick >= self.total_ticks

    def step_once(self) -> int:
        """Advance exactly one simulated tick and ingest what the sensors saw.

        Split out of :meth:`run` so a live viewer can drive the same simulation
        incrementally without a second code path: the Observatory SIM run calls this,
        and ``run`` simply calls it in a loop. Both therefore exercise identical
        sensing, ordering and ingest logic, and cannot drift apart.

        Returns the number of observations the pipeline accepted this tick.
        """
        now_s = self._elapsed_s()
        # Scripted actions land before the tick they are scheduled for, so the
        # sensors in this tick already see their physical consequences.
        while self._pending and self._pending[0].t <= now_s:
            self._apply_interaction(self._pending.pop(0))

        self._release_due_waypoints(now_s)
        self.world.step()
        now = self.world.now
        self._record_departures()
        # Sample continuous truth every tick. Without this the trajectory maps the
        # log advertises stay empty, so track continuity, position error, co-motion
        # and ID switches cannot be evaluated against physical truth at all.
        self.ground_truth.snapshot(self.world)

        observations = self._sense_radar(now, now_s) + self._sense_rfid(now, now_s)
        self._person_obs += sum(1 for o in observations if o.source_type is SourceType.MMWAVE)
        self._item_obs += sum(1 for o in observations if o.source_type is SourceType.RFID)

        # Stable ordering before ingest: the pipeline drops observations that move
        # time backwards, and an unordered batch would make that a coin flip rather
        # than a property of the simulated transport.
        accepted = 0
        for observation in sorted(
            observations, key=lambda o: (o.timestamp, o.source_type.value, o.observation_id)
        ):
            if self.pipeline.ingest(observation):
                self._ingested += 1
                accepted += 1
            else:
                self._rejected += 1
        self.pipeline.advance_to(now)
        return accepted

    def result(self) -> LabRunResult:
        """Finalize the run and return truth and prediction side by side.

        Callers that need live state mid-run (the Observatory SIM run) read the
        pipeline through ``ObservatoryRun``'s own state rendering instead; finishing
        here would close the pipeline underneath them.
        """
        stats = self._parser.stats
        pipeline_result = self.pipeline.finish()
        return LabRunResult(
            scenario_id=self.scenario.scenario_id,
            seed=self.scenario.seed,
            ticks=self.world.tick,
            pipeline=pipeline_result,
            ground_truth=self.ground_truth,
            radar_bytes=self._radar_bytes,
            frames_parsed=stats.frames_parsed,
            frames_rejected=stats.frames_rejected,
            person_observations=self._person_obs,
            item_observations=self._item_obs,
            observations_ingested=self._ingested,
            observations_rejected=self._rejected,
            faults_applied=list(self._faults_applied),
        )

    def run(self) -> LabRunResult:
        while not self.exhausted:
            self.step_once()
        # Validation accepts an interaction at exactly ``duration_s``, but the loop
        # above stops before a tick with ``now_s == duration_s`` ever runs, so such an
        # action would vanish from both physical state and truth. A validated
        # timestamp must never silently disappear.
        while self._pending and self._pending[0].t <= self.scenario.duration_s:
            self._apply_interaction(self._pending.pop(0))
        self._record_departures()
        return self.result()


def run_lab_scenario(
    scenario: LabScenario,
    *,
    pipeline_config: PipelineConfig | None = None,
    recorder: Recorder | None = None,
) -> LabRunResult:
    """One-call execution of a lab scenario. Deterministic for a given scenario+seed."""
    return LabEngine(scenario, pipeline_config=pipeline_config, recorder=recorder).run()
