"""Observatory SIM runs: the virtual store lab, watchable in the browser.

A SIM run is a :class:`~radiowave.api.runs.ObservatoryRun` whose observations come
from :class:`~radiowave.simulator.lab.engine.LabEngine` instead of a recorded stream
or a serial port. Every projection the Observatory already renders — person tracks,
item tracks, events, carts, sessions, confidence decisions — is reused unchanged;
there is no second map renderer and no second state contract.

**What this does and does not exercise.** The radar path is real: the engine encodes
TI UART bytes and parses them with the production ``TiFrameParser`` and
``TiTargetNormalizer`` before anything reaches fusion. What a SIM run deliberately
does NOT exercise is ``TiLiveSession`` — the transport/reconnect/queueing layer.
Driving that from a viewer would mean a reader thread advancing the world at thread
speed, which reintroduces exactly the nondeterminism the lab exists to avoid, and
that layer already has thorough Phase-3 tests of its own. So a SIM run is an honest
view of the simulation through the production *inference* path, not a rehearsal of
the serial transport.

**Labelling.** SIM runs report ``mode == "LIVE"`` so the existing live panels work,
but ``ObservatoryLiveStatus.simulated`` is set and carries the scenario and seed.
Clients must surface that: simulated and hardware runs render identically otherwise,
and quietly conflating them would let a demo be mistaken for a measurement.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from radiowave.api.runs import ObservatoryRun
from radiowave.api.viewmodels import (
    ObservatoryGroundTruth,
    ObservatoryLiveStatus,
    ObservatoryScenarioSummary,
    RunMode,
)
from radiowave.pipeline import PipelineConfig
from radiowave.simulator.lab.engine import LabEngine
from radiowave.simulator.lab.scenarios import (
    SCENARIOS,
    FaultKind,
    LabScenario,
    load_scenario,
)
from radiowave.simulator.lab.world import LAB_WORLD_EPOCH


@dataclass(frozen=True)
class SimRuntime:
    """What the API process needs to start SIM runs.

    Unlike ``LiveRuntime`` this needs no hardware configuration at all, which is the
    whole point: the lab is always available, on any machine, with no board attached.
    """

    #: Default scenario offered when a request does not name one.
    default_scenario_id: str = "acceptance_60s"
    pipeline_config: PipelineConfig | None = None
    #: Simulated seconds advanced per Observatory poll. The browser polls on a wall
    #: clock, but the simulation's own clock only moves here, so what you watch is
    #: paced by polling while the run itself stays a pure function of its seed.
    seconds_per_advance: float = 0.5
    available: list[str] = field(default_factory=lambda: sorted(SCENARIOS))


class SimObservatoryRun(ObservatoryRun):
    """A lab scenario, stepped incrementally and rendered as a live-style run."""

    mode: RunMode = "LIVE"

    def __init__(
        self,
        run_id: str,
        scenario: LabScenario,
        *,
        runtime: SimRuntime,
        pipeline_config: PipelineConfig | None = None,
    ) -> None:
        self.lab_scenario = scenario
        self._runtime = runtime
        self._engine = LabEngine(scenario, pipeline_config=pipeline_config)
        self._init_common(run_id, scenario.store, pipeline_config)
        # The base class projects state straight off ``self.pipeline``; point it at
        # the engine's so no observation can reach the UI without having gone through
        # the engine's sensor path first.
        self.pipeline = self._engine.pipeline
        self.epoch_at = self._engine.world.now
        self.revision = 1

    # ---------------------------------------------------------------- identity
    @property
    def scenario_id(self) -> str:
        return self.lab_scenario.scenario_id

    @property
    def scenario_name(self) -> str:
        return self.lab_scenario.name

    @property
    def seed(self) -> int:
        return self.lab_scenario.seed

    @property
    def duration_s(self) -> float:
        return self.lab_scenario.duration_s

    @property
    def observations_total(self) -> int:
        return self._engine._person_obs + self._engine._item_obs

    # ------------------------------------------------------------------ control
    def advance(self, seconds: float) -> None:
        """Step the simulation forward by ``seconds`` of SIMULATED time.

        Overridden wholesale rather than reusing the replay implementation: a replay
        run walks a pre-computed observation list, whereas here the observations do
        not exist until the world has been stepped and the sensors have looked at it.
        """
        with self._lock:
            if self.finished or seconds <= 0:
                return
            ticks = max(1, round(seconds * self.lab_scenario.tick_hz))
            for _ in range(ticks):
                if self._engine.exhausted:
                    break
                self._engine.step_once()
            self.time_s = round(self._engine.world.tick / self.lab_scenario.tick_hz, 6)
            self.cursor = self._engine._ingested
            self.revision += 1
            if self._engine.exhausted:
                self.pipeline.finish(advance=False)
                self.finished = True

    def step(self) -> None:
        self.advance(self._runtime.seconds_per_advance)

    def reset(self) -> None:
        """Rebuild the run from the same scenario and seed — bit-identical by design."""
        with self._lock:
            self._engine = LabEngine(self.lab_scenario, pipeline_config=self.config)
            self.pipeline = self._engine.pipeline
            self.epoch_at = self._engine.world.now
            self.cursor = 0
            self.time_s = 0.0
            self.finished = False
            self.revision += 1
            self.epoch += 1

    def stop(self) -> None:
        """Finish the simulation early and leave the run readable.

        Required, not optional: every client stops the active run before replacing it,
        so without this a simulated run could be started and then never switched away
        from. There is no transport to close — stopping simply means the world stops
        advancing and the pipeline is finalized where it stands.
        """
        with self._lock:
            if self.finished:
                return
            self.pipeline.finish(advance=False)
            self.finished = True
            self.revision += 1

    def close(self) -> None:
        """A SIM run holds no OS resources — no port, no thread, no file."""

    # -------------------------------------------------------------- projections
    def _ground_truth_view(self) -> list[ObservatoryGroundTruth]:
        """The simulator's private truth, exposed for DISPLAY only.

        This is the one place ground truth is allowed out, and it goes to the
        operator's screen, never into the pipeline: ``LabEngine`` builds its
        observations without consulting the log, so what you see overlaid here had no
        influence on what fusion concluded. That is exactly what makes the overlay
        useful — it is the answer key held up *beside* the answer, not folded into it.
        """
        start = LAB_WORLD_EPOCH
        return [
            ObservatoryGroundTruth(
                t_s=round((event.timestamp - start).total_seconds(), 3),
                event_type=event.event_type.value,
                epc=event.epc or "",
                shopper_label=event.ground_truth_person_id,
                counterpart_label=event.counterpart_person_id,
            )
            for event in self._engine.ground_truth.events
        ]

    def _live_status(self) -> ObservatoryLiveStatus | None:
        stats = self._engine._parser.stats
        radar_sensor_id = self._engine._radar_sensor_id
        elapsed = max(self.time_s, 1e-9)
        return ObservatoryLiveStatus(
            sensor_id=radar_sensor_id,
            sensor_name=f"Virtual TI radar ({self.lab_scenario.scenario_id})",
            state="STREAMING" if not self.finished else "DISCONNECTED",
            message=None if not self.finished else "simulation complete",
            generation=self._engine._ti_normalizer.generation,
            frames_received=stats.frames_parsed + stats.frames_rejected,
            frames_parsed=stats.frames_parsed,
            frames_rejected=stats.frames_rejected,
            frames_duplicate=self._engine._ti_normalizer.counters.frames_duplicate,
            observations_emitted=self._engine._person_obs,
            observations_dropped_overflow=0,
            reconnect_count=max(0, self._engine._ti_normalizer.generation - 1),
            last_frame_age_s=0.0,
            frame_rate_hz=round(stats.frames_parsed / elapsed, 2),
            observation_rate_hz=round(self._engine._person_obs / elapsed, 2),
            capture_path=None,
            started_at=self.epoch_at.isoformat(),
            simulated=True,
            simulated_scenario_id=self.lab_scenario.scenario_id,
            simulated_seed=self.lab_scenario.seed,
        )


def sim_scenario_summaries() -> list[ObservatoryScenarioSummary]:
    """The lab catalog, projected into the summary the Observatory already renders.

    ``ground_truth`` is populated from the scenario's EXPECTED truth rather than a
    run's actual truth: this is a catalog listing, so it describes what a scenario is
    meant to demonstrate before anyone runs it. It is display metadata and never
    reaches a pipeline.
    """
    summaries: list[ObservatoryScenarioSummary] = []
    for scenario_id in sorted(SCENARIOS):
        scenario = load_scenario(scenario_id)
        summaries.append(
            ObservatoryScenarioSummary(
                scenario_id=scenario.scenario_id,
                name=scenario.name,
                description=scenario.description,
                duration_s=scenario.duration_s,
                seed=scenario.seed,
                shopper_count=len(scenario.shoppers),
                item_count=len(scenario.store.items),
                # The lab has no camera: selective CV is Phase 7, and claiming vision
                # here would advertise evidence the simulator cannot produce.
                vision_enabled=False,
                radar_dropouts=sum(
                    1
                    for f in scenario.fault_injections
                    if f.kind in (FaultKind.RADAR_DROPOUT, FaultKind.RADAR_RESTART)
                ),
                rfid_dropouts=sum(
                    1 for f in scenario.fault_injections if f.kind is FaultKind.RFID_DROPOUT
                ),
                ground_truth=[
                    ObservatoryGroundTruth(
                        t_s=event.t,
                        event_type=event.event_type.value,
                        epc=event.epc,
                        shopper_label=event.shopper_id,
                        counterpart_label=event.counterpart_shopper_id,
                    )
                    for event in scenario.expected_truth.events
                ],
            )
        )
    return summaries


def build_sim_run(
    run_id: str,
    runtime: SimRuntime,
    *,
    scenario_id: str | None = None,
    seed: int | None = None,
) -> SimObservatoryRun:
    scenario = load_scenario(scenario_id or runtime.default_scenario_id)
    if seed is not None and seed != scenario.seed:
        # A different seed is a different noise realization of the SAME physical
        # scenario - that separation is the lab's core property, so it is offered.
        scenario = scenario.model_copy(update={"seed": seed})
    return SimObservatoryRun(
        run_id, scenario, runtime=runtime, pipeline_config=runtime.pipeline_config
    )


__all__ = [
    "SimObservatoryRun",
    "SimRuntime",
    "build_sim_run",
    "sim_scenario_summaries",
]
