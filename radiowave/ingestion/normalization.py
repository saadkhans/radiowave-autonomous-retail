"""Turn adapter-native samples into canonical store-frame observations.

This is the only place where sensor frames are converted. Fusion never sees a
:class:`~radiowave.contracts.geometry.SensorCoordinate`.
"""

from __future__ import annotations

from typing import Any

from radiowave.adapters.mmwave.base import NativeRadarSample
from radiowave.adapters.rfid.base import NativeRfidRead
from radiowave.adapters.vision.base import NativeVisionDetection
from radiowave.contracts.geometry import SpatialUncertainty
from radiowave.contracts.observations import (
    NATIVE_ANTENNA_KEY,
    NATIVE_POSITION_KEY,
    NATIVE_TRACK_KEY,
    ItemObservation,
    PersonObservation,
    VisionEvidence,
)
from radiowave.contracts.store import EPC, Sensor, SourceType, ZoneKind
from radiowave.digital_twin.registry import StoreRegistry


class ObservationNormalizer:
    def __init__(self, registry: StoreRegistry, scenario_id: str | None = None) -> None:
        self._registry = registry
        self._scenario_id = scenario_id

    def _sensor(self, sensor_id: str, expected: SourceType) -> Sensor:
        """The registered sensor, refusing samples whose modality does not match its wiring."""
        sensor = self._registry.sensor(sensor_id)
        if sensor.modality != expected:
            msg = (
                f"sensor {sensor_id!r} is registered as {sensor.modality.value}, "
                f"but a {expected.value} sample referenced it"
            )
            raise ValueError(msg)
        return sensor

    def person(self, sample: NativeRadarSample) -> PersonObservation:
        self._sensor(sample.sensor_id, SourceType.MMWAVE)
        transform = self._registry.transform(sample.sensor_id)
        coordinate = transform.to_world(sample.position)
        velocity = (
            transform.velocity_to_world(sample.velocity) if sample.velocity is not None else None
        )
        metadata: dict[str, Any] = {
            NATIVE_TRACK_KEY: sample.native_track_id,
            NATIVE_POSITION_KEY: sample.position.model_dump(),
        }
        if sample.snr_db is not None:
            metadata["snr_db"] = sample.snr_db
        return PersonObservation(
            observation_id=f"mmwave:{sample.sensor_id}:{sample.sequence}",
            sensor_id=sample.sensor_id,
            timestamp=sample.timestamp,
            confidence=sample.track_confidence,
            coordinate=coordinate,
            uncertainty=SpatialUncertainty.isotropic(sample.sigma_m),
            velocity=velocity,
            scenario_id=self._scenario_id,
            metadata=metadata,
        )

    def item(self, read: NativeRfidRead) -> ItemObservation:
        sensor = self._sensor(read.sensor_id, SourceType.RFID)
        transform = self._registry.transform(read.sensor_id)
        coordinate = None
        uncertainty = None
        if read.estimate is not None and read.estimate_sigma_m is not None:
            coordinate = transform.to_world(read.estimate)
            uncertainty = SpatialUncertainty.isotropic(read.estimate_sigma_m)
        # A localized read takes its zone from the actual world-frame estimate; only a
        # zone-only read (no estimate) falls back to the read point's own pose.
        zone_point = coordinate if coordinate is not None else sensor.pose.position
        zone = self._registry.zone_at(
            zone_point, kinds={ZoneKind.FIXTURE, ZoneKind.SALES_FLOOR, ZoneKind.EXIT}
        )
        metadata: dict[str, Any] = {NATIVE_ANTENNA_KEY: read.antenna_port}
        if read.estimate is not None:
            metadata[NATIVE_POSITION_KEY] = read.estimate.model_dump()
        return ItemObservation(
            observation_id=f"rfid:{read.sensor_id}:{read.sequence}",
            sensor_id=read.sensor_id,
            timestamp=read.timestamp,
            confidence=read.confidence,
            epc=EPC(value=read.epc_hex),
            zone_id=zone.zone_id if zone else None,
            rssi_dbm=read.rssi_dbm,
            phase_rad=read.phase_rad,
            read_rate_hz=read.read_rate_hz,
            coordinate=coordinate,
            uncertainty=uncertainty,
            scenario_id=self._scenario_id,
            metadata=metadata,
        )

    def vision(self, detection: NativeVisionDetection) -> VisionEvidence:
        self._sensor(detection.sensor_id, SourceType.VISION)
        transform = self._registry.transform(detection.sensor_id)
        coordinate = transform.to_world(detection.position)
        fixture = self._registry.fixture_at(coordinate, margin=0.5)
        zone = self._registry.zone_at(coordinate)
        return VisionEvidence(
            observation_id=f"vision:{detection.sensor_id}:{detection.sequence}",
            sensor_id=detection.sensor_id,
            timestamp=detection.timestamp,
            confidence=detection.confidence,
            kind=detection.kind,
            coordinate=coordinate,
            uncertainty=SpatialUncertainty.isotropic(detection.sigma_m),
            fixture_id=fixture.fixture_id if fixture else None,
            zone_id=zone.zone_id if zone else None,
            scenario_id=self._scenario_id,
            metadata={NATIVE_POSITION_KEY: detection.position.model_dump()},
        )
