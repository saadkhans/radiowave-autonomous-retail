"""Scenario definition: scripted ground truth that generators turn into sensor samples.

A scenario is fully described by data (waypoints, carries, dropouts, seed), so
it is deterministic, serialisable and can be recorded as ground truth next to
the synthetic observations it produced.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from pydantic import Field, model_validator

from radiowave.contracts._base import ContractModel, FrozenModel
from radiowave.contracts.events import RetailEventType
from radiowave.contracts.geometry import Velocity, WorldCoordinate
from radiowave.contracts.store import Store

SCENARIO_EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

#: Horizontal offset of a carried item from the carrier's tracked body point (meters).
CARRY_OFFSET = (0.25, 0.0)


class Waypoint(FrozenModel):
    t: float = Field(ge=0.0, description="Seconds since scenario start")
    x: float
    y: float


class ShopperScript(FrozenModel):
    """Piecewise-linear path. The shopper is present from the first to the last waypoint."""

    label: str = Field(min_length=1, description="Ground-truth label, never a system id")
    waypoints: list[Waypoint] = Field(min_length=1)

    @model_validator(mode="after")
    def _monotonic(self) -> ShopperScript:
        times = [w.t for w in self.waypoints]
        if any(b < a for a, b in pairwise(times)):
            msg = f"waypoints for {self.label} must be time-ordered"
            raise ValueError(msg)
        return self

    def present_at(self, t: float) -> bool:
        return self.waypoints[0].t <= t <= self.waypoints[-1].t

    def position_at(self, t: float) -> WorldCoordinate | None:
        if not self.present_at(t):
            return None
        points = self.waypoints
        for a, b in pairwise(points):
            if a.t <= t <= b.t:
                if b.t == a.t:
                    return WorldCoordinate(x=b.x, y=b.y)
                f = (t - a.t) / (b.t - a.t)
                return WorldCoordinate(x=a.x + f * (b.x - a.x), y=a.y + f * (b.y - a.y))
        last = points[-1]
        return WorldCoordinate(x=last.x, y=last.y)

    def velocity_at(self, t: float) -> Velocity:
        points = self.waypoints
        for a, b in pairwise(points):
            if a.t <= t < b.t and b.t > a.t:
                dt = b.t - a.t
                return Velocity(vx=(b.x - a.x) / dt, vy=(b.y - a.y) / dt)
        return Velocity()


class ItemPlacement(FrozenModel):
    epc: str
    x: float
    y: float
    z: float = 0.9


class Carry(FrozenModel):
    """Between ``start_t`` and ``end_t`` the item moves with the labelled shopper."""

    epc: str
    carrier_label: str
    start_t: float
    end_t: float | None = Field(default=None, description="None = carried until scenario end")


class Dropout(FrozenModel):
    """Suppress a sensor modality (optionally one sensor) for a time window."""

    start_t: float
    end_t: float
    sensor_id: str | None = None


class GroundTruthEvent(FrozenModel):
    t: float
    event_type: RetailEventType
    epc: str
    shopper_label: str | None = None
    counterpart_label: str | None = None


class Scenario(ContractModel):
    scenario_id: str = Field(min_length=1)
    name: str
    description: str = ""
    seed: int = 7
    duration_s: float = Field(gt=0.0)
    store: Store
    shoppers: list[ShopperScript] = Field(default_factory=list)
    placements: list[ItemPlacement] = Field(default_factory=list)
    carries: list[Carry] = Field(default_factory=list)
    radar_dropouts: list[Dropout] = Field(default_factory=list)
    rfid_dropouts: list[Dropout] = Field(default_factory=list)
    vision_enabled: bool = False
    expected_events: list[GroundTruthEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def _references(self) -> Scenario:
        labels = {s.label for s in self.shoppers}
        epcs = {p.epc for p in self.placements}
        store_epcs = {i.epc.value for i in self.store.items}
        for carry in self.carries:
            if carry.carrier_label not in labels:
                msg = f"carry references unknown shopper {carry.carrier_label}"
                raise ValueError(msg)
            if carry.epc not in epcs:
                msg = f"carry references unplaced item {carry.epc}"
                raise ValueError(msg)
        for epc in epcs:
            if epc not in store_epcs:
                msg = f"placement references EPC {epc} missing from the store twin"
                raise ValueError(msg)
        return self

    # --- time helpers ------------------------------------------------------
    def at(self, t: float) -> datetime:
        return SCENARIO_EPOCH + timedelta(seconds=t)

    def shopper(self, label: str) -> ShopperScript:
        for script in self.shoppers:
            if script.label == label:
                return script
        msg = f"unknown shopper label {label}"
        raise KeyError(msg)

    # --- ground truth ------------------------------------------------------
    def carrier_at(self, epc: str, t: float) -> str | None:
        for carry in self.carries:
            if carry.epc == epc and carry.start_t <= t and (carry.end_t is None or t < carry.end_t):
                return carry.carrier_label
        return None

    def item_position_at(self, epc: str, t: float) -> WorldCoordinate:
        """True item location: with its carrier, or wherever it was last set down."""
        placement = next(p for p in self.placements if p.epc == epc)
        position = WorldCoordinate(x=placement.x, y=placement.y, z=placement.z)
        for carry in sorted((c for c in self.carries if c.epc == epc), key=lambda c: c.start_t):
            if t < carry.start_t:
                break
            end = carry.end_t
            sample_t = t if end is None or t < end else end
            carrier = self.shopper(carry.carrier_label).position_at(sample_t)
            if carrier is not None:
                position = WorldCoordinate(
                    x=carrier.x + CARRY_OFFSET[0], y=carrier.y + CARRY_OFFSET[1], z=placement.z
                )
        return position

    def item_velocity_at(self, epc: str, t: float) -> Velocity:
        carrier = self.carrier_at(epc, t)
        return self.shopper(carrier).velocity_at(t) if carrier else Velocity()

    def radar_suppressed(self, sensor_id: str, t: float) -> bool:
        return _suppressed(self.radar_dropouts, sensor_id, t)

    def rfid_suppressed(self, sensor_id: str, t: float) -> bool:
        return _suppressed(self.rfid_dropouts, sensor_id, t)


def _suppressed(dropouts: list[Dropout], sensor_id: str, t: float) -> bool:
    return any(
        d.start_t <= t < d.end_t and (d.sensor_id is None or d.sensor_id == sensor_id)
        for d in dropouts
    )


def frames(duration_s: float, rate_hz: float, phase_s: float = 0.0) -> list[float]:
    """Deterministic sample times in seconds: ``phase, phase + 1/rate, ...`` up to duration."""
    period = 1.0 / rate_hz
    count = math.floor((duration_s - phase_s) / period) + 1
    return [round(phase_s + i * period, 6) for i in range(max(count, 0))]
