"""Live sensor runs: availability, start, stop, reconnect.

The live run reuses every ``/runs/{id}/...`` read route; only creation and the two
hardware controls live here. One live run at a time: the radar has one serial port.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from radiowave.api.live import LiveObservatoryRun, LiveRuntime, serial_support_available
from radiowave.api.runs import LiveModeError, LiveRunBusyError, RunManager
from radiowave.api.viewmodels import (
    LiveRunCreateRequest,
    ObservatoryLiveAvailability,
    ObservatorySnapshot,
)

router = APIRouter(tags=["live"])


def _manager(request: Request) -> RunManager:
    manager: RunManager = request.app.state.runs
    return manager


def _runtime(request: Request) -> LiveRuntime | None:
    runtime: LiveRuntime | None = getattr(request.app.state, "live", None)
    return runtime


def availability(request: Request) -> ObservatoryLiveAvailability:
    manager = _manager(request)
    runtime = _runtime(request)
    active = manager.live_run_ids()
    active_id = active[0] if active else None
    # A reservation (another admission still constructing its session) makes the
    # sensor unavailable too, even though no run is published yet: the manager is
    # authoritative about the slot, this is only a friendlier pre-check message.
    starting = manager.live_reservation() is not None
    # Likewise, a run whose ``delete()`` has already removed it from the published
    # run list but whose ``close()`` has not finished stopping the sensor yet: still
    # unavailable, with a distinct reason from "starting".
    closing = manager.closing_live_run_id() is not None
    if runtime is None:
        return ObservatoryLiveAvailability(
            configured=False,
            serial_support=serial_support_available(),
            active_run_id=active_id,
            reason="no live sensor configured: set RADIOWAVE_TI_CONFIG to a TiLiveConfig JSON",
        )
    serial_ok = serial_support_available() or runtime.stream_factory is not None
    reason = None
    if not serial_ok:
        reason = "TI serial support not installed; install radiowave-autonomous-retail[hardware-ti]"
    elif active_id is not None:
        reason = f"live run {active_id} is already using the sensor; stop it first"
    elif starting:
        reason = "a live run is starting; try again shortly"
    elif closing:
        reason = "a live run is stopping; try again shortly"
    return ObservatoryLiveAvailability(
        configured=True,
        config_path=runtime.config_path,
        sensor_id=runtime.config.adapter.sensor_id,
        sensor_name=runtime.sensor_name,
        data_port=runtime.config.serial.data_port,
        serial_support=serial_ok,
        active_run_id=active_id,
        reason=reason,
    )


@router.get("/live/status", response_model=ObservatoryLiveAvailability)
def live_status(request: Request) -> ObservatoryLiveAvailability:
    return availability(request)


@router.post("/runs/live", response_model=ObservatorySnapshot, status_code=201)
def create_live_run(request: Request, body: LiveRunCreateRequest) -> ObservatorySnapshot:
    """Connect the configured sensor and return the run's initial snapshot.

    The ``availability()`` check here is only a friendly pre-check (a clearer message
    for the common case, and a fast 409 without touching hardware when nothing is
    configured); the manager's reservation in ``build_live_exclusive`` is what
    actually makes admission exclusive when two requests race.
    """
    status = availability(request)
    if status.reason is not None:
        raise HTTPException(status_code=409, detail=status.reason)
    runtime = _runtime(request)
    assert runtime is not None  # availability() guarantees it
    try:
        _, snapshot = _manager(request).build_live_exclusive(
            lambda run_id: LiveObservatoryRun(run_id, runtime, capture=body.capture)
        )
    except LiveRunBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return snapshot


def _live_run(request: Request, run_id: str) -> LiveObservatoryRun:
    run = _manager(request).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    if not isinstance(run, LiveObservatoryRun):
        raise HTTPException(status_code=409, detail=f"run {run_id!r} is not a LIVE run")
    return run


@router.post("/runs/{run_id}/stop", response_model=ObservatorySnapshot)
def stop_live_run(request: Request, run_id: str) -> ObservatorySnapshot:
    """Stop reading, finalize the pipeline and close the capture; the run stays readable."""
    run = _live_run(request, run_id)
    run.stop()  # joins the reader/driver threads; takes the lock itself
    return run.snapshot()


@router.post("/runs/{run_id}/reconnect", response_model=ObservatorySnapshot)
def reconnect_live_run(request: Request, run_id: str) -> ObservatorySnapshot:
    run = _live_run(request, run_id)
    try:
        return run.apply(run.reconnect)
    except LiveModeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
