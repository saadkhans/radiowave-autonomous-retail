"""Deterministic run control: create, step, advance, seek, reset, inspect."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from radiowave.api.runs import ObservatoryRun, RunManager, store_view
from radiowave.api.viewmodels import (
    AdvanceRequest,
    ObservatoryEventPage,
    ObservatoryRunState,
    ObservatorySnapshot,
    ObservatoryStore,
    ObservatoryTimeline,
    RunCreateRequest,
    SeekRequest,
)
from radiowave.simulator.library import SCENARIOS

router = APIRouter(tags=["runs"])


def _manager(request: Request) -> RunManager:
    manager: RunManager = request.app.state.runs
    return manager


def _run(request: Request, run_id: str) -> ObservatoryRun:
    run = _manager(request).get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")
    return run


@router.get("/runs", response_model=list[str])
def list_runs(request: Request) -> list[str]:
    return _manager(request).ids()


@router.post("/runs", response_model=ObservatorySnapshot, status_code=201)
def create_run(request: Request, body: RunCreateRequest) -> ObservatorySnapshot:
    """Create a run and return its initial snapshot so the client binds to it atomically."""
    if body.scenario_id not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario {body.scenario_id!r}")
    return _manager(request).create(body.scenario_id, body.seed).snapshot()


@router.delete("/runs/{run_id}", status_code=204)
def delete_run(request: Request, run_id: str) -> None:
    if not _manager(request).delete(run_id):
        raise HTTPException(status_code=404, detail=f"unknown run {run_id!r}")


@router.get("/runs/{run_id}/state", response_model=ObservatoryRunState)
def get_state(request: Request, run_id: str) -> ObservatoryRunState:
    return _run(request, run_id).state()


@router.get("/runs/{run_id}/snapshot", response_model=ObservatorySnapshot)
def get_snapshot(request: Request, run_id: str) -> ObservatorySnapshot:
    """State, events and timeline at one revision, captured under the run's lock."""
    return _run(request, run_id).snapshot()


@router.get("/runs/{run_id}/store", response_model=ObservatoryStore)
def get_store(request: Request, run_id: str) -> ObservatoryStore:
    return store_view(_run(request, run_id).scenario.store)


@router.post("/runs/{run_id}/reset", response_model=ObservatorySnapshot)
def reset_run(request: Request, run_id: str) -> ObservatorySnapshot:
    run = _run(request, run_id)
    return run.apply(run.reset)


@router.post("/runs/{run_id}/step", response_model=ObservatorySnapshot)
def step_run(request: Request, run_id: str) -> ObservatorySnapshot:
    run = _run(request, run_id)
    return run.apply(run.step)


@router.post("/runs/{run_id}/advance", response_model=ObservatorySnapshot)
def advance_run(request: Request, run_id: str, body: AdvanceRequest) -> ObservatorySnapshot:
    run = _run(request, run_id)
    return run.apply(lambda: run.advance(body.seconds))


@router.post("/runs/{run_id}/seek", response_model=ObservatorySnapshot)
def seek_run(request: Request, run_id: str, body: SeekRequest) -> ObservatorySnapshot:
    run = _run(request, run_id)
    return run.apply(lambda: run.seek(body.time_s))


@router.get("/runs/{run_id}/events", response_model=ObservatoryEventPage)
def get_events(
    request: Request,
    run_id: str,
    since: int = Query(default=0, ge=0, description="First sequence number to return"),
    limit: int = Query(default=500, ge=1, le=5000),
) -> ObservatoryEventPage:
    run = _run(request, run_id)
    events = run.events()
    page = events[since : since + limit]
    return ObservatoryEventPage(
        run_id=run_id, events=page, next_seq=since + len(page), total=len(events)
    )


@router.get("/runs/{run_id}/timeline", response_model=ObservatoryTimeline)
def get_timeline(request: Request, run_id: str) -> ObservatoryTimeline:
    return _run(request, run_id).timeline()
