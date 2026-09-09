from __future__ import annotations

import math

import pytest

from radiowave.adapters.mmwave.base import NativeRadarSample
from radiowave.contracts import (
    STORE_FRAME_ID,
    SensorCoordinate,
    SensorPose,
    Velocity,
    WorldCoordinate,
)
from radiowave.digital_twin.geometry import RigidTransform, rotation_matrix
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.simulator.stores import RADAR_NORTH
from tests.conftest import T0


def test_yaw_rotation_maps_sensor_x_to_world_y() -> None:
    pose = SensorPose(position=WorldCoordinate(x=0, y=0, z=0), yaw=math.pi / 2)
    transform = RigidTransform.from_pose("s", pose)
    world = transform.to_world(SensorCoordinate(x=1, y=0, z=0, frame_id="s"))
    assert world.x == pytest.approx(0.0, abs=1e-9)
    assert world.y == pytest.approx(1.0)
    assert world.frame_id == STORE_FRAME_ID


def test_translation_and_rotation_compose() -> None:
    pose = SensorPose(position=WorldCoordinate(x=6, y=8, z=2.4), yaw=-math.pi / 2)
    transform = RigidTransform.from_pose("radar", pose)
    # A point 4 m straight ahead of a radar looking south (-y) lands 4 m south of it.
    world = transform.to_world(SensorCoordinate(x=4, y=0, z=0, frame_id="radar"))
    assert (world.x, world.y, world.z) == pytest.approx((6.0, 4.0, 2.4))


def test_roundtrip_for_every_lab_sensor(registry: StoreRegistry) -> None:
    target = WorldCoordinate(x=3.3, y=5.1, z=1.0)
    for sensor in registry.sensors:
        transform = registry.transform(sensor.sensor_id)
        back = transform.to_world(transform.to_sensor(target))
        assert (back.x, back.y, back.z) == pytest.approx((3.3, 5.1, 1.0), abs=1e-9)


def test_frame_mismatch_is_rejected() -> None:
    transform = RigidTransform.from_pose("a", SensorPose(position=WorldCoordinate(x=0, y=0)))
    with pytest.raises(ValueError, match="frame"):
        transform.to_world(SensorCoordinate(x=1, y=1, frame_id="b"))
    with pytest.raises(ValueError, match="frame"):
        transform.to_sensor(WorldCoordinate(x=1, y=1, frame_id="other"))


def test_inverse_and_compose_give_identity() -> None:
    pose = SensorPose(position=WorldCoordinate(x=1, y=2, z=3), yaw=0.4, pitch=-0.2, roll=0.1)
    transform = RigidTransform.from_pose("s", pose)
    identity = transform.inverse().compose(transform)
    x, y, z = identity.apply_xyz(0.7, -0.3, 2.0)
    assert (x, y, z) == pytest.approx((0.7, -0.3, 2.0), abs=1e-9)
    assert rotation_matrix(0, 0, 0).tolist() == [[1, 0, 0], [0, 1, 0], [0, 0, 1]]


def test_velocity_rotates_without_translation() -> None:
    pose = SensorPose(position=WorldCoordinate(x=10, y=10), yaw=math.pi)
    transform = RigidTransform.from_pose("s", pose)
    velocity = transform.velocity_to_world(Velocity(vx=1.0, vy=0.0))
    assert velocity.vx == pytest.approx(-1.0)
    assert velocity.vy == pytest.approx(0.0, abs=1e-9)


def test_normalizer_emits_store_frame_and_keeps_native_position(registry: StoreRegistry) -> None:
    sample = NativeRadarSample(
        sensor_id=RADAR_NORTH,
        sequence=0,
        timestamp=T0,
        native_track_id="T7",
        position=SensorCoordinate(x=4.0, y=0.0, z=-1.4, frame_id=RADAR_NORTH),
        velocity=Velocity(vx=1.0, vy=0.0),
        track_confidence=0.9,
        sigma_m=0.08,
    )
    obs = ObservationNormalizer(registry, "unit").person(sample)
    assert obs.coordinate.frame_id == STORE_FRAME_ID
    assert (obs.coordinate.x, obs.coordinate.y, obs.coordinate.z) == pytest.approx((6.0, 4.0, 1.0))
    assert obs.velocity is not None and obs.velocity.vy == pytest.approx(-1.0)
    assert obs.metadata["native_track_id"] == "T7"
    assert obs.metadata["native_position"]["frame_id"] == RADAR_NORTH
    assert obs.observation_id.startswith("mmwave:radar-north:")
    assert obs.scenario_id == "unit"
