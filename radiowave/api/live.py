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
from dataclasses import dataclass
from pathlib import Path

from radiowave.adapters.mmwave.ti.config import TiLiveConfig
from radiowave.adapters.mmwave.ti.health import TiAdapterDiagnostics
from radiowave.adapters.mmwave.ti.session import SessionTiming, StreamFactory, TiLiveSession
from radiowave.api.runs import LiveModeError, ObservatoryRun
from radiowave.api.viewmodels import (
    ObservatoryGroundTruth,
    ObservatoryLiveStatus,
    ObservatoryRunState,
    RunMode,
)
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


def allocate_capture_path(directory: Path, sensor_id: str, run_id: str, stamp: str) -> Path:
    """Reserve a unique capture file path and create it (empty) before anyone writes to it.

    Two live runs whose stamps collide (same sensor, same microsecond, or a test that
    mocks the clock) must never silently share a file: :class:`JsonlRecorder` opens its
    path with truncating ``"w"``, so handing it a path that already holds another run's
    data would erase that data (invariant 14: capture files never overwrite previous
    experiments). Exclusive creation (``"x"``) makes the check-then-open atomic; on a
    collision a bounded numeric suffix is tried instead of failing the run.
    """
    directory.mkdir(parents=True, exist_ok=True)
    base_name = f"live-{sensor_id}-{run_id}-{stamp}"
    for attempt in range(1000):
        name = f"{base_name}.jsonl" if attempt == 0 else f"{base_name}-{attempt}.jsonl"
        candidate = directory / name
        try:
            candidate.open("x").close()
        except FileExistsError:
            continue
        return candidate
    msg = f"could not allocate a unique capture path for {base_name!r} after 1000 attempts"
    raise RuntimeError(msg)


class LiveObservatoryRun(ObservatoryRun):
    mode: RunMode = "LIVE"

    def __init__(self, run_id: str, runtime: LiveRuntime, *, capture: bool) -> None:
        self._runtime = runtime
        self._init_common(run_id, runtime.config.store, runtime.pipeline_config)
        # Concrete today, but only the surface of
        # :class:`~radiowave.adapters.mmwave.base.LivePeopleSource` is used below
        # (architecture invariant 1). Annotating it as that protocol is what would
        # make mypy enforce the boundary instead of leaving it a convention; that
        # needs ``diagnostics()`` to return a vendor-neutral contract first, which
        # today is the TI-package ``TiAdapterDiagnostics``. See the protocol's
        # docstring for the deferred extraction.
        self.session = TiLiveSession(
            runtime.config,
            self.registry,
            stream_factory=runtime.stream_factory,
            timing=runtime.timing,
            scenario_id=None,
        )
        # Taken right after the session is constructed, before it starts reading: the
        # same ordered clock the session stamps every observation with (invariant 9),
        # so ``time_s`` (derived from ``epoch_at``) can never be ahead of or behind the
        # clock the pipeline advances on in ``tick``.
        self.epoch_at = self.session.clock_now()
        self.capture_path: Path | None = None
        recorder: Recorder | None = None
        if capture:
            stamp = self.epoch_at.strftime("%Y%m%dT%H%M%S.%fZ")
            self.capture_path = allocate_capture_path(
                runtime.capture_dir, runtime.config.adapter.sensor_id, run_id, stamp
            )
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
        # One diagnostics() read per snapshot: set at the top of ``_state`` so
        # ``observations_total`` and ``_live_status`` (both consumed from within it)
        # never observe two different instants of the session's counters.
        self._diagnostics_cache: TiAdapterDiagnostics | None = None
        # ``session.start()`` itself opens the raw capture file and creates the
        # reader thread, so it can raise after already allocating OS resources — not
        # just the driver thread's own creation below. Either failure means
        # ``__init__`` never returns, so ``build_live_exclusive`` never gets a
        # ``run`` reference to close: without this try/except, an already-open
        # recorder (and any partially opened capture) would leak. ``session.stop()``
        # is safe to call even on a session that never started or only partially
        # started (idempotent, never raises out of a clean state), but is still
        # guarded here — the same way the manager's own failure paths are — in case
        # it ever does.
        try:
            self.session.start()
            if runtime.autonomous:
                self._driver = threading.Thread(
                    target=self._drive, name=f"live-run-{run_id}", daemon=True
                )
                self._driver.start()
        except BaseException:
            try:
                self.session.stop()
            except Exception:
                log.exception(
                    "failed to stop live session for %s after construction failure", run_id
                )
            if self._recorder is not None:
                self._recorder.close()
            raise

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
        return self._diagnostics().observations_emitted

    def _diagnostics(self) -> TiAdapterDiagnostics:
        """The diagnostics read cached by ``_state`` for this snapshot, if any.

        Falls back to a fresh read for callers outside a ``_state`` build (e.g. a
        direct ``observations_total`` access), but within one ``_state`` call both
        ``observations_total`` and ``_live_status`` see the same instant.
        """
        if self._diagnostics_cache is not None:
            return self._diagnostics_cache
        return self.session.diagnostics()

    def _state(self) -> ObservatoryRunState:
        self._diagnostics_cache = self.session.diagnostics()
        try:
            return super()._state()
        finally:
            self._diagnostics_cache = None

    def _ground_truth_view(self) -> list[ObservatoryGroundTruth]:
        return []

    def _live_status(self) -> ObservatoryLiveStatus:
        diagnostics = self._diagnostics()
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
        # The queue is drained and ``now`` is read under the session's queue lock, so
        # every observation stamped before ``now`` is in this batch and the clock
        # advance below can never run ahead of an observation still being enqueued.
        # Both come from the session's one ordered clock (invariant 9): the pipeline
        # never advances on a clock different from the one that stamped the samples.
        observations, now = self.session.drain_with_clock()
        ingested = 0
        for observation in observations:
            try:
                if self.pipeline.ingest(observation):
                    ingested += 1
            except Exception:  # one bad sample must not stop the session
                self.ingest_errors += 1
                log.exception("live ingest failed for %s", observation.observation_id)
        self.pipeline.advance_to(now)
        self.time_s = max(self.time_s, round((now - self.epoch_at).total_seconds(), 3))
        # ``observations_cursor`` (read straight from ``self.cursor`` by ``_state``,
        # same as REPLAY) tracks how many observations the pipeline has accepted so
        # far, distinct from ``observations_total`` (everything the sensor emitted).
        self.cursor += ingested
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
        """Ask the session for a fresh connection generation.

        A user STOP never resurrects automatically (invariant 12): once this run is
        finalized (``stop()`` has set ``self.finished``) reconnect must not silently
        start a new reader thread, so it is refused with the same 409-style error
        replay-only controls use. While the run is still live, ``request_reconnect``
        can itself report ``False`` if the session had already been stopped from under
        it; that is refused the same way rather than pretending to succeed.
        """
        if self.finished:
            msg = "live run is stopped; start a new run"
            raise LiveModeError(msg)
        if not self.session.request_reconnect():
            msg = "live run is stopped; start a new run"
            raise LiveModeError(msg)
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
