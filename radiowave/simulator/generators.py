"""Deterministic synthetic sensor generators.

Each generator turns scenario ground truth into *native* samples in the sensor's
own frame, with explicit configured noise. Determinism: every generator owns a
``numpy`` generator seeded from ``scenario.seed`` plus a fixed per-modality
offset, and samples are emitted in a canonical order.

The RFID generator reports location estimates at exactly the configured
``rfid_sigma_m``; it never pretends to be more accurate than that.
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import numpy as np
from pydantic import Field

from radiowave.adapters.mmwave.base import NativeRadarSample
from radiowave.adapters.rfid.base import NativeRfidRead
from radiowave.adapters.vision.base import NativeVisionDetection
from radiowave.contracts._base import FrozenModel
from radiowave.contracts.geometry import Velocity, WorldCoordinate
from radiowave.contracts.observations import VisionEvidenceKind
from radiowave.contracts.store import SourceType
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.simulator.scenario import Scenario, frames

_RADAR_SEED_OFFSET = 101
_RFID_SEED_OFFSET = 202
_VISION_SEED_OFFSET = 303


class GeneratorConfig(FrozenModel):
    radar_rate_hz: float = Field(default=10.0, gt=0.0)
    radar_sigma_m: float = Field(default=0.08, ge=0.0)
    radar_velocity_sigma_m_s: float = Field(default=0.1, ge=0.0)
    radar_max_range_m: float = Field(default=14.0, gt=0.0)
    radar_track_confidence: float = Field(default=0.9, ge=0.0, le=1.0)
    rfid_rate_hz: float = Field(default=4.0, gt=0.0, description="Per read point")
    rfid_max_range_m: float = Field(default=8.0, gt=0.0)
    rfid_sigma_m: float = Field(
        default=0.5, ge=0.0, description="Configured 1-sigma accuracy of location estimates"
    )
    rfid_rssi_sigma_db: float = Field(default=2.0, ge=0.0)
    rfid_min_read_probability: float = Field(default=0.1, ge=0.0, le=1.0)
    vision_sigma_m: float = Field(default=0.3, ge=0.0)
    vision_confidence: float = Field(default=0.85, ge=0.0, le=1.0)
    vision_max_range_m: float = Field(default=10.0, gt=0.0)


class RadarGenerator:
    """Per-radar point samples for every present shopper, in the radar frame."""

    def __init__(
        self, scenario: Scenario, registry: StoreRegistry, config: GeneratorConfig | None = None
    ) -> None:
        self.scenario = scenario
        self.registry = registry
        self.config = config or GeneratorConfig()

    def samples(self) -> Iterator[NativeRadarSample]:
        cfg = self.config
        rng = np.random.default_rng(self.scenario.seed + _RADAR_SEED_OFFSET)
        radars = [s for s in self.registry.sensors if s.modality == SourceType.MMWAVE]
        output: list[NativeRadarSample] = []
        for radar in radars:
            transform = self.registry.transform(radar.sensor_id)
            sequence = 0
            native_counter = 0
            native_ids: dict[str, str] = {}
            visible_last_frame: set[str] = set()
            for t in frames(self.scenario.duration_s, cfg.radar_rate_hz):
                visible: set[str] = set()
                if self.scenario.radar_suppressed(radar.sensor_id, t):
                    visible_last_frame = visible
                    continue
                for shopper in self.scenario.shoppers:
                    truth = shopper.position_at(t)
                    if truth is None:
                        continue
                    body = WorldCoordinate(x=truth.x, y=truth.y, z=1.0)
                    if body.distance_to(radar.pose.position) > cfg.radar_max_range_m:
                        continue
                    visible.add(shopper.label)
                    if shopper.label not in visible_last_frame or shopper.label not in native_ids:
                        native_counter += 1
                        native_ids[shopper.label] = f"T{native_counter}"
                    noisy = WorldCoordinate(
                        x=body.x + rng.normal(0.0, cfg.radar_sigma_m),
                        y=body.y + rng.normal(0.0, cfg.radar_sigma_m),
                        z=body.z + rng.normal(0.0, cfg.radar_sigma_m),
                    )
                    truth_velocity = shopper.velocity_at(t)
                    noisy_velocity = Velocity(
                        vx=truth_velocity.vx + rng.normal(0.0, cfg.radar_velocity_sigma_m_s),
                        vy=truth_velocity.vy + rng.normal(0.0, cfg.radar_velocity_sigma_m_s),
                        vz=0.0,
                    )
                    output.append(
                        NativeRadarSample(
                            sensor_id=radar.sensor_id,
                            sequence=sequence,
                            timestamp=self.scenario.at(t),
                            native_track_id=native_ids[shopper.label],
                            position=transform.to_sensor(noisy),
                            velocity=transform.velocity_to_sensor(noisy_velocity),
                            track_confidence=cfg.radar_track_confidence,
                            sigma_m=cfg.radar_sigma_m,
                            snr_db=float(rng.uniform(12.0, 25.0)),
                        )
                    )
                    sequence += 1
                visible_last_frame = visible
        output.sort(key=lambda s: (s.timestamp, s.sensor_id, s.sequence))
        yield from output


class RfidGenerator:
    """Per-read-point EPC reads with RSSI, phase and a configured-accuracy estimate."""

    def __init__(
        self, scenario: Scenario, registry: StoreRegistry, config: GeneratorConfig | None = None
    ) -> None:
        self.scenario = scenario
        self.registry = registry
        self.config = config or GeneratorConfig()

    def reads(self) -> Iterator[NativeRfidRead]:
        cfg = self.config
        rng = np.random.default_rng(self.scenario.seed + _RFID_SEED_OFFSET)
        read_points = [s for s in self.registry.sensors if s.modality == SourceType.RFID]
        period = 1.0 / cfg.rfid_rate_hz
        output: list[NativeRfidRead] = []
        for index, read_point in enumerate(read_points):
            transform = self.registry.transform(read_point.sensor_id)
            phase_offset = (index / max(len(read_points), 1)) * period
            sequence = 0
            for t in frames(self.scenario.duration_s, cfg.rfid_rate_hz, phase_s=phase_offset):
                if self.scenario.rfid_suppressed(read_point.sensor_id, t):
                    continue
                for placement in self.scenario.placements:
                    truth = self.scenario.item_position_at(placement.epc, t)
                    distance = truth.distance_to(read_point.pose.position)
                    if distance > cfg.rfid_max_range_m:
                        continue
                    probability = max(
                        cfg.rfid_min_read_probability, 1.0 - distance / cfg.rfid_max_range_m
                    )
                    if rng.random() > probability:
                        continue
                    rssi = -40.0 - 20.0 * math.log10(max(distance, 0.5))
                    rssi += float(rng.normal(0.0, cfg.rfid_rssi_sigma_db))
                    estimate = WorldCoordinate(
                        x=truth.x + rng.normal(0.0, cfg.rfid_sigma_m),
                        y=truth.y + rng.normal(0.0, cfg.rfid_sigma_m),
                        z=truth.z,
                    )
                    output.append(
                        NativeRfidRead(
                            sensor_id=read_point.sensor_id,
                            sequence=sequence,
                            timestamp=self.scenario.at(t),
                            epc_hex=placement.epc,
                            antenna_port=read_point.vendor_metadata.get("port", "0"),
                            rssi_dbm=round(rssi, 2),
                            phase_rad=float(rng.uniform(-math.pi, math.pi)),
                            read_rate_hz=cfg.rfid_rate_hz,
                            confidence=max(0.3, min(0.95, 0.9 - 0.05 * distance)),
                            estimate=transform.to_sensor(estimate),
                            estimate_sigma_m=cfg.rfid_sigma_m,
                        )
                    )
                    sequence += 1
        output.sort(key=lambda r: (r.timestamp, r.sensor_id, r.sequence))
        yield from output


class VisionGenerator:
    """Semantic interaction evidence around ground-truth pick and put-down moments."""

    def __init__(
        self, scenario: Scenario, registry: StoreRegistry, config: GeneratorConfig | None = None
    ) -> None:
        self.scenario = scenario
        self.registry = registry
        self.config = config or GeneratorConfig()

    def detections(self) -> Iterator[NativeVisionDetection]:
        if not self.scenario.vision_enabled:
            return
        cfg = self.config
        rng = np.random.default_rng(self.scenario.seed + _VISION_SEED_OFFSET)
        cameras = [s for s in self.registry.sensors if s.modality == SourceType.VISION]
        output: list[NativeVisionDetection] = []
        moments: list[tuple[float, str, VisionEvidenceKind]] = []
        for carry in self.scenario.carries:
            moments.append(
                (carry.start_t, carry.carrier_label, VisionEvidenceKind.REACH_INTO_FIXTURE)
            )
            moments.append(
                (carry.start_t + 0.5, carry.carrier_label, VisionEvidenceKind.ITEM_IN_HAND)
            )
            if carry.end_t is not None:
                moments.append((carry.end_t, carry.carrier_label, VisionEvidenceKind.ITEM_PLACED))
        moments.sort()
        for camera in cameras:
            transform = self.registry.transform(camera.sensor_id)
            sequence = 0
            for t, label, kind in moments:
                if t > self.scenario.duration_s:
                    continue
                truth = self.scenario.shopper(label).position_at(t)
                if truth is None:
                    continue
                body = WorldCoordinate(x=truth.x, y=truth.y, z=1.0)
                if body.distance_to(camera.pose.position) > cfg.vision_max_range_m:
                    continue
                noisy = WorldCoordinate(
                    x=body.x + rng.normal(0.0, cfg.vision_sigma_m),
                    y=body.y + rng.normal(0.0, cfg.vision_sigma_m),
                    z=body.z,
                )
                output.append(
                    NativeVisionDetection(
                        sensor_id=camera.sensor_id,
                        sequence=sequence,
                        timestamp=self.scenario.at(t),
                        kind=kind,
                        position=transform.to_sensor(noisy),
                        confidence=cfg.vision_confidence,
                        sigma_m=cfg.vision_sigma_m,
                    )
                )
                sequence += 1
        output.sort(key=lambda d: (d.timestamp, d.sensor_id, d.sequence))
        yield from output
