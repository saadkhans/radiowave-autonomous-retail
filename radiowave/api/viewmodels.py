"""Observatory view-model contracts.

These are the ONLY shapes the frontend consumes. They are built from the
Foundation contracts but deliberately flattened: times are seconds since the
scenario epoch, coordinates are store-frame meters, and identities are the
canonical ones (person track id, session id, EPC, GTIN). Sensor ids appear
only as provenance.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from radiowave.simulator.scenario import SCENARIO_EPOCH


def seconds_since_epoch(when: datetime | None) -> float | None:
    if when is None:
        return None
    return round((when - SCENARIO_EPOCH).total_seconds(), 3)


class ViewModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ObservatoryBounds(ViewModel):
    min_x: float
    min_y: float
    max_x: float
    max_y: float


class ObservatoryZone(ViewModel):
    zone_id: str
    name: str
    kind: str
    bounds: ObservatoryBounds


class ObservatoryFixture(ViewModel):
    fixture_id: str
    zone_id: str
    name: str
    bounds: ObservatoryBounds


class ObservatoryBoundary(ViewModel):
    boundary_id: str
    kind: str
    bounds: ObservatoryBounds


class ObservatorySensor(ViewModel):
    """Provenance only. Never a business identity."""

    sensor_id: str
    modality: str
    x: float
    y: float
    z: float
    yaw: float
    name: str | None = None


class ObservatoryProduct(ViewModel):
    gtin: str
    name: str
    sku: str | None = None
    home_fixture_id: str | None = None


class ObservatoryCatalogItem(ViewModel):
    epc: str
    short_epc: str
    gtin: str
    product_name: str
    home_fixture_id: str | None = None


class ObservatoryStore(ViewModel):
    store_id: str
    name: str
    units: str = "m"
    floor: ObservatoryBounds
    zones: list[ObservatoryZone]
    fixtures: list[ObservatoryFixture]
    boundaries: list[ObservatoryBoundary]
    sensors: list[ObservatorySensor]
    products: list[ObservatoryProduct]
    items: list[ObservatoryCatalogItem]


class ObservatoryPoint(ViewModel):
    t_s: float
    x: float
    y: float


class ObservatoryFeature(ViewModel):
    name: str
    score: float | None = Field(default=None, description="None when the engine did not use it")
    weight: float | None = None


class ObservatoryCandidate(ViewModel):
    track_id: str
    score: float
    features: list[ObservatoryFeature]
    raw_features: dict[str, float] = Field(default_factory=dict)


class ObservatoryDecision(ViewModel):
    event_id: str
    event_type: str
    decision: Literal["COMMIT", "WAIT", "REVIEW"]
    confidence: float
    margin: float
    waited_s: float
    reason: str
    at_s: float


class ObservatoryPerson(ViewModel):
    track_id: str
    session_id: str | None
    state: str
    x: float
    y: float
    vx: float
    vy: float
    speed: float
    heading_deg: float | None
    confidence: float
    sigma_m: float
    observation_count: int
    created_s: float
    updated_s: float
    sensor_ids: list[str] = Field(default_factory=list, description="Provenance only")
    cart_id: str | None = None
    carried_epcs: list[str] = Field(default_factory=list)
    trail: list[ObservatoryPoint] = Field(default_factory=list)


class ObservatoryItem(ViewModel):
    epc: str
    short_epc: str
    gtin: str | None
    product_name: str | None
    sku: str | None
    home_fixture_id: str | None
    state: str
    state_since_s: float | None
    x: float | None
    y: float | None
    sigma_m: float | None
    zone_id: str | None
    carrier_track_id: str | None
    movement_start_s: float | None
    last_seen_s: float | None
    observation_count: int
    candidates: list[ObservatoryCandidate] = Field(default_factory=list)
    pending: ObservatoryDecision | None = None
    trail: list[ObservatoryPoint] = Field(default_factory=list)


class ObservatoryCartLine(ViewModel):
    epc: str
    short_epc: str
    gtin: str | None
    product_name: str | None
    added_s: float
    final_ownership_candidate: bool
    exit_event_s: float | None


class ObservatoryCart(ViewModel):
    cart_id: str
    shopper_track_id: str
    session_id: str | None
    status: str
    exited_s: float | None
    lines: list[ObservatoryCartLine]


class ObservatorySession(ViewModel):
    session_id: str
    track_id: str
    state: str
    entered_s: float
    exited_s: float | None
    entry_boundary_id: str | None
    exit_boundary_id: str | None


class ObservatoryEvent(ViewModel):
    seq: int
    t_s: float
    kind: Literal["RETAIL_EVENT", "ITEM_TRANSITION", "PERSON_TRACK", "SESSION"]
    label: str
    epc: str | None = None
    shopper_track_id: str | None = None
    counterpart_track_id: str | None = None
    confidence: float | None = None
    margin: float | None = None
    decision: Literal["COMMIT", "WAIT", "REVIEW"] | None = None
    reason: str = ""
    event_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None


class ObservatoryCounters(ViewModel):
    accepted: int
    dropped_duplicates: int
    out_of_order: int
    rejected_unknown_sensor: int
    rejected_low_confidence: int
    rejected_foreign_scenario: int
    rejected_spatially_inconsistent: int


class ObservatoryGroundTruth(ViewModel):
    t_s: float
    event_type: str
    epc: str
    shopper_label: str | None
    counterpart_label: str | None


class ObservatoryScenarioSummary(ViewModel):
    scenario_id: str
    name: str
    description: str
    duration_s: float
    seed: int
    shopper_count: int
    item_count: int
    vision_enabled: bool
    radar_dropouts: int
    rfid_dropouts: int
    ground_truth: list[ObservatoryGroundTruth]


class ObservatoryScenarioDetail(ObservatoryScenarioSummary):
    store: ObservatoryStore


class ObservatoryRunState(ViewModel):
    run_id: str
    scenario_id: str
    scenario_name: str
    seed: int
    time_s: float
    duration_s: float
    step_interval_s: float
    steps: int
    finished: bool
    observations_cursor: int
    observations_total: int
    events_total: int
    persons: list[ObservatoryPerson]
    items: list[ObservatoryItem]
    carts: list[ObservatoryCart]
    sessions: list[ObservatorySession]
    counters: ObservatoryCounters


class ObservatoryTimelineMarker(ViewModel):
    t_s: float
    label: str
    epc: str | None = None
    shopper_track_id: str | None = None
    decision: str | None = None


class ObservatoryTimeline(ViewModel):
    run_id: str
    time_s: float
    duration_s: float
    markers: list[ObservatoryTimelineMarker]
    ground_truth: list[ObservatoryGroundTruth]


class RunCreateRequest(ViewModel):
    scenario_id: str
    seed: int | None = None


class AdvanceRequest(ViewModel):
    seconds: float = Field(gt=0.0, le=3600.0)


class SeekRequest(ViewModel):
    time_s: float = Field(ge=0.0)


class ObservatoryEventPage(ViewModel):
    run_id: str
    events: list[ObservatoryEvent]
    next_seq: int
    total: int
