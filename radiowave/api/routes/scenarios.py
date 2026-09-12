"""Read-only scenario catalog: summaries, details and the store digital twin."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from radiowave.api.runs import list_scenarios, scenario_detail, store_view
from radiowave.api.viewmodels import (
    ObservatoryScenarioDetail,
    ObservatoryScenarioSummary,
    ObservatoryStore,
)
from radiowave.simulator.library import SCENARIOS, load_scenario

router = APIRouter(tags=["scenarios"])


def _load(scenario_id: str) -> ObservatoryScenarioDetail:
    if scenario_id not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario {scenario_id!r}")
    return scenario_detail(load_scenario(scenario_id))


@router.get("/scenarios", response_model=list[ObservatoryScenarioSummary])
def get_scenarios() -> list[ObservatoryScenarioSummary]:
    return list_scenarios()


@router.get("/scenarios/{scenario_id}", response_model=ObservatoryScenarioDetail)
def get_scenario(scenario_id: str) -> ObservatoryScenarioDetail:
    return _load(scenario_id)


@router.get("/scenarios/{scenario_id}/store", response_model=ObservatoryStore)
def get_scenario_store(scenario_id: str) -> ObservatoryStore:
    if scenario_id not in SCENARIOS:
        raise HTTPException(status_code=404, detail=f"unknown scenario {scenario_id!r}")
    return store_view(load_scenario(scenario_id).store)
