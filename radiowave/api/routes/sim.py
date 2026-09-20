"""Virtual store lab runs: list scenarios, start a simulated run.

Only creation and discovery live here; a SIM run reuses every ``/runs/{id}/...``
read and control route unchanged, which is the point — the Observatory renders a
simulated store through exactly the panels it renders a real one through.

Unlike LIVE, SIM has no physical exclusivity: there is no serial port to contend
for, so several simulated runs may exist at once. Inheriting LIVE's one-at-a-time
rule would have been an accident of implementation rather than a real constraint.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from radiowave.api.runs import RunManager
from radiowave.api.sim import SimRuntime, build_sim_run, sim_scenario_summaries
from radiowave.api.viewmodels import (
    ObservatoryScenarioSummary,
    ObservatorySnapshot,
    SimRunCreateRequest,
)

router = APIRouter(tags=["sim"])


def _manager(request: Request) -> RunManager:
    manager: RunManager = request.app.state.runs
    return manager


def _runtime(request: Request) -> SimRuntime:
    runtime: SimRuntime | None = getattr(request.app.state, "sim", None)
    if runtime is None:  # pragma: no cover - the app always configures one
        runtime = SimRuntime()
    return runtime


@router.get("/sim/scenarios", response_model=list[ObservatoryScenarioSummary])
def sim_scenarios() -> list[ObservatoryScenarioSummary]:
    """The lab catalog. Always available — the simulator needs no hardware."""
    return sim_scenario_summaries()


@router.post("/runs/sim", response_model=ObservatorySnapshot, status_code=201)
def create_sim_run(request: Request, body: SimRunCreateRequest) -> ObservatorySnapshot:
    manager = _manager(request)
    runtime = _runtime(request)
    try:
        run, snapshot = manager.build_with(
            lambda run_id: build_sim_run(
                run_id, runtime, scenario_id=body.scenario_id, seed=body.seed
            )
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    del run
    return snapshot
