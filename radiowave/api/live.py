"""LIVE Observatory runs: one real sensor feeding the unchanged Foundation pipeline.

A :class:`LiveObservatoryRun` is an :class:`ObservatoryRun` whose time is the wall
clock and whose observations arrive from a hardware session instead of a scenario.
Everything the frontend consumes (persons, items, carts, events, timeline) is produced
by the same projections as replay; only the driver differs:

* the sensor session (``TiLiveSession``) reads and normalizes on its own thread and
  queues store-frame ``PersonObservation`` values;
* one driver thread owns the pipeline: every ``tick`` drains the queue, ingests in
  arrival order, then ``advance_to(now)`` so fusion keeps stepping while nobody is in
  view. The run lock serializes ticks with API reads, so the pipeline is never
  mutated by two threads;
* optional capture writes the normalized stream through the ordinary
  :class:`JsonlRecorder`, so a live session is a format-v2 recording that replays
  with ``radiowave replay`` and no hardware.

Replay-only controls (step / advance / seek / reset) raise :class:`LiveModeError`.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from radiowave.adapters.mmwave.ti.config import TiLiveConfig
from radiowave.adapters.mmwave.ti.session import SessionTiming, StreamFactory, TiLiveSession
from radiowave.api.runs import LiveModeError, ObservatoryRun
from radiowave.api.viewmodels import ObservatoryGroundTruth, ObservatoryLiveStatus, RunMode
from radiowave.contracts.observations import SensorObservation
from radiowave.fusion.interfaces import Recorder
from radiowave.pipeline import FoundationPipeline, PipelineConfig
from radiowave.replay.recorder import JsonlRecorder

log = logging.getLogger(__name__)

LIVE_SCENARIO_ID = "live"
DEFAULT_CAPTURE_DIR = Path("data") / "captures"


def serial_support_available() -> bool:
    try:
        import serial  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class LiveRuntime:
    """What the API process needs to start live runs. Built once at startup from
    ``RADIOWAVE_TI_CONFIG``; tests inject a fixture stream factory and fake clocks."""

    config: TiLiveConfig
    config_path: str | None = None
    stream_factory: StreamFactory | None = None
    timing: SessionTiming | None = None
    capture_dir: Path = DEFAULT_CAPTURE_DIR
    tick_interval_s: float = 0.1
    pipeline_config: PipelineConfig | None = None
    #: When False the driver thread is not started; tests call ``tick()`` themselves.
    autonomous: bool = True

    @property
    def sensor_name(self) -> str | None:
        for sensor in self.config.store.sensors:
            if sensor.sensor_id == self.config.adapter.sensor_id:
                return sensor.name
        return None


def _utc_now(runtime: LiveRuntime) -> Callable[[], datetime]:
    if runtime.timing is not None:
        return runtime.timing.utc_now
    from datetime import UTC

    return lambda: datetime.now(UTC)


class LiveObservatoryRun(ObservatoryRun):
    mode: RunMode = "LIVE"

    def __init__(self, run_id: str, runtime: LiveRuntime, *, capture: bool) -> None:
        self._runtime = runtime
        self._utc_now = _utc_now(runtime)
        self._init_common(run_id, runtime.config.store, runtime.pipeline_config)
        self.epoch_at = self._utc_now()
        self.session = TiLiveSession(
            runtime.config,
            self.registry,
            stream_factory=runtime.stream_factory,
            timing=runtime.timing,
            scenario_id=None,
        )
        self.capture_path: Path | None = None
        recorder: Recorder | None = None
        if capture:
            stamp = self.epoch_at.strftime("%Y%m%dT%H%M%SZ")
            self.capture_path = (
                runtime.capture_dir / f"live-{runtime.config.adapter.sensor_id}-{stamp}.jsonl"
            )
            self.capture_path.parent.mkdir(parents=True, exist_ok=True)
            recorder = JsonlRecorder(self.capture_path)
        self._recorder = recorder
        self.pipeline = FoundationPipeline(
            self.registry, runtime.pipeline_config, scenario_id=None, recorder=recorder
        )
        self.ingest_errors = 0
        self._stop = threading.Event()
        self._driver: threading.Thread | None = None
        self.revision = 1
        self.epoch = 1
        self.session.start()
        if runtime.autonomous:
            self._driver = threading.Thread(
                target=self._drive, name=f"live-run-{run_id}", daemon=True
            )
            self._driver.start()

    # ---------------------------------------------------------------- identity
    @property
    def scenario_id(self) -> str:
        return LIVE_SCENARIO_ID

    @property
    def scenario_name(self) -> str:
        return self._runtime.sensor_name or self._runtime.config.adapter.sensor_id

    @property
    def seed(self) -> int:
        return 0

    @property
    def duration_s(self) -> float:
        return self.time_s  # the live edge

    @property
    def observations_total(self) -> int:
        return self.session.diagnostics().observations_emitted

    def _ground_truth_view(self) -> list[ObservatoryGroundTruth]:
        return []

    def _live_status(self) -> ObservatoryLiveStatus:
        diagnostics = self.session.diagnostics()
        return ObservatoryLiveStatus(
            sensor_id=diagnostics.sensor_id,
            sensor_name=self._runtime.sensor_name,
            state=diagnostics.state.value,
            message=diagnostics.message,
            generation=diagnostics.generation,
            frames_received=diagnostics.frames_received,
            frames_parsed=diagnostics.frames_parsed,
            frames_rejected=diagnostics.frames_rejected,
            frames_duplicate=diagnostics.frames_duplicate,
            observations_emitted=diagnostics.observations_emitted,
            observations_dropped_overflow=diagnostics.observations_dropped_overflow,
            reconnect_count=diagnostics.reconnect_count,
            last_frame_age_s=diagnostics.last_frame_age_s,
            frame_rate_hz=diagnostics.frame_rate_hz,
            observation_rate_hz=diagnostics.observation_rate_hz,
            capture_path=None if self.capture_path is None else str(self.capture_path),
            started_at=self.epoch_at.isoformat(),
        )

    # ----------------------------------------------------------------- driving
    def tick(self) -> int:
        """Drain the session queue into the pipeline and step fusion to now.

        Returns the number of observations ingested. Must be called under the run
        lock (the driver thread and ``apply`` both do).
        """
        if self.finished:
            return 0
        now = self._utc_now()
        ingested = 0
        for observation in self.session.drain():
            try:
                if self.pipeline.ingest(observation):
                    ingested += 1
            except Exception:  # one bad sample must not stop the session
                self.ingest_errors += 1
                log.exception("live ingest failed for %s", observation.observation_id)
        self.pipeline.advance_to(now)
        self.time_s = max(self.time_s, round((now - self.epoch_at).total_seconds(), 3))
        self.revision += 1
        return ingested

    def _drive(self) -> None:
        interval = self._runtime.tick_interval_s
        while not self._stop.wait(interval):
            try:
                with self._lock:
                    self.tick()
            except Exception:  # keep driving; the failure is logged
                log.exception("live driver tick failed for %s", self.run_id)

    def stop(self) -> None:
        """Stop the sensor and the driver, finalize the pipeline, close the capture."""
        with self._lock:
            if self.finished:
                return
            self._stop.set()
        driver = self._driver
        if driver is not None and driver is not threading.current_thread():
            driver.join(5.0)
        self.session.stop()
        with self._lock:
            if not self.finished:
                self.tick()  # last drained observations before the terminal evaluation
                self.pipeline.finish(advance=False)
                if self._recorder is not None:
                    self._recorder.close()
                self.finished = True
                self.revision += 1

    def reconnect(self) -> None:
        self.session.request_reconnect()
        with self._lock:
            self.revision += 1

    def close(self) -> None:
        self.stop()

    # ------------------------------------------------- replay-only controls
    def reset(self) -> None:
        if hasattr(self, "session"):
            msg = "reset is not available in LIVE mode; stop the run and start a new one"
            raise LiveModeError(msg)

    def advance(self, seconds: float) -> None:
        msg = "advance is not available in LIVE mode; time is the wall clock"
        raise LiveModeError(msg)

    def step(self) -> None:
        msg = "step is not available in LIVE mode; time is the wall clock"
        raise LiveModeError(msg)

    def seek(self, time_s: float) -> None:
        msg = "seek is not available in LIVE mode; replay a captured session instead"
        raise LiveModeError(msg)


def ingest_all(pipeline: FoundationPipeline, observations: list[SensorObservation]) -> int:
    """Small helper for headless capture: ingest in order, count acceptances."""
    return sum(1 for observation in observations if pipeline.ingest(observation))
