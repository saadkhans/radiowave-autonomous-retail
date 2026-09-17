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
from radiowave.simulator.lab import interactions
from radiowave.simulator.lab.ground_truth import GroundTruthLog
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
from radiowave.simulator.lab.world import VirtualWorld, WorldConfig


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
        self._rfid = RfidReaderEmulator(
            RfidReaderEmulatorConfig(
                antennas=antennas,
                noise=RfidReadNoiseConfig(
                    base_read_probability=reliability.read_probability,
                    missed_read_probability=reliability.missed_read_rate,
                    bleed_probability=reliability.bleed_rate,
                    # Lab runs need item LOCATION evidence, because Foundation's fusion
                    # is built around an RFID adapter supplying a zone-scale coordinate
                    # (its own synthetic reads carry ~0.5 m sigma). With no estimate at
                    # all the pipeline tracks items but can never infer that one moved,
                    # so every scenario proposes zero events - which is a starved
                    # simulator, not a working one. The blur stays honest at rack scale.
                    localization_enabled=True,
                ),
                seed=scenario.seed,
            )
        )

        self._pending = sorted(scenario.scheduled_interactions, key=lambda i: i.t)
        self._faults_applied: list[str] = []
        self._radar_restarts_done: set[float] = set()

    # ------------------------------------------------------------------ helpers
    def _sensor_of(self, modality: SourceType) -> Sensor:
        for sensor in self.scenario.store.sensors:
            if sensor.modality is modality:
                return sensor
        msg = f"scenario store has no {modality.value} sensor"
        raise ValueError(msg)

    def _elapsed_s(self) -> float:
        return self.world.tick / self.scenario.tick_hz

    def _fault_active(self, kind: FaultKind, now_s: float) -> bool:
        for fault in self.scenario.fault_injections:
            if fault.kind is kind and fault.start_t <= now_s < fault.end_t:
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
            actor = interactions.enter(world, log, entry, item.shopper_id, script.speed_mps)
            rest = [(w.x, w.y) for w in script.waypoints[1:]]
            if rest:
                from radiowave.simulator.lab import actors as _actors

                _actors.waypoint_walk(actor, rest, script.speed_mps)
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
        self._radar_bytes += len(data)
        observations: list[AnyObservation] = []
        for frame in self._parser.feed(data):
            observations.extend(self._ti_normalizer.frame_to_observations(frame, received_at=now))
        return observations

    def _sense_rfid(self, now: datetime, now_s: float) -> list[AnyObservation]:
        if self._fault_active(FaultKind.RFID_DROPOUT, now_s):
            return []
        tagged = [
            TaggedItemState(epc=state.epc, position=state.position, carrier_id=state.carrier_id)
            for state in self.world.items.values()
        ]
        return [self.observation_normalizer.item(read) for read in self._rfid.reads(tagged, now)]

    # ------------------------------------------------------------------- run
    def run(self) -> LabRunResult:
        self._radar_bytes = 0
        ingested = rejected = person_obs = item_obs = 0
        total_ticks = round(self.scenario.duration_s * self.scenario.tick_hz)

        for _ in range(total_ticks):
            now_s = self._elapsed_s()
            # Scripted actions land before the tick they are scheduled for, so the
            # sensors in this tick already see their physical consequences.
            while self._pending and self._pending[0].t <= now_s:
                self._apply_interaction(self._pending.pop(0))

            self.world.step()
            now = self.world.now

            observations = self._sense_radar(now, now_s) + self._sense_rfid(now, now_s)
            person_obs += sum(1 for o in observations if o.source_type is SourceType.MMWAVE)
            item_obs += sum(1 for o in observations if o.source_type is SourceType.RFID)

            # Stable ordering before ingest: the pipeline drops observations that move
            # time backwards, and an unordered batch would make that a coin flip rather
            # than a property of the simulated transport.
            for observation in sorted(
                observations, key=lambda o: (o.timestamp, o.source_type.value, o.observation_id)
            ):
                if self.pipeline.ingest(observation):
                    ingested += 1
                else:
                    rejected += 1
            self.pipeline.advance_to(now)

        stats = self._parser.stats
        return LabRunResult(
            scenario_id=self.scenario.scenario_id,
            seed=self.scenario.seed,
            ticks=total_ticks,
            pipeline=self.pipeline.finish(),
            ground_truth=self.ground_truth,
            radar_bytes=self._radar_bytes,
            frames_parsed=stats.frames_parsed,
            frames_rejected=stats.frames_rejected,
            person_observations=person_obs,
            item_observations=item_obs,
            observations_ingested=ingested,
            observations_rejected=rejected,
            faults_applied=list(self._faults_applied),
        )


def run_lab_scenario(
    scenario: LabScenario,
    *,
    pipeline_config: PipelineConfig | None = None,
    recorder: Recorder | None = None,
) -> LabRunResult:
    """One-call execution of a lab scenario. Deterministic for a given scenario+seed."""
    return LabEngine(scenario, pipeline_config=pipeline_config, recorder=recorder).run()
