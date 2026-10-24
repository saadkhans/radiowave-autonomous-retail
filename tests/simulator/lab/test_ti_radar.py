"""Acceptance test for the virtual store lab's TI radar: world position -> emitted
TI UART bytes -> the REAL TiFrameParser -> the REAL TiTargetNormalizer must land
back on (approximately) the original world position, for several sensor poses and
at least one non-default coordinate convention, so a hardcoded-axes bug cannot pass.

Test world points are generated FROM a chosen sensor-frame offset (forward, left,
up), via the real ``RigidTransform``, rather than picked by hand in world
coordinates: that is the only way to guarantee a point lands inside the radar's
FOV (a function of the sensor-frame angle/range) regardless of a pose's yaw/pitch,
without duplicating the trigonometry the emulator itself does.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest

from radiowave.adapters.mmwave.ti.adapter import NATIVE_TI_TRACK_ID_KEY, TiTargetNormalizer
from radiowave.adapters.mmwave.ti.config import TiAdapterConfig, TiCoordinateConvention
from radiowave.adapters.mmwave.ti.parser import TiFrameParser
from radiowave.contracts.geometry import SensorCoordinate, Velocity, WorldCoordinate
from radiowave.contracts.store import Box2D, Sensor, SensorPose, SourceType, Store
from radiowave.digital_twin.geometry import RigidTransform
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.simulator.lab.sensors.ti_radar import (
    TiRadarEmulator,
    TiRadarEmulatorConfig,
    TiRadarFovConfig,
    TiRadarNoiseConfig,
    VisibleTarget,
)

SENSOR_ID = "radar-lab-01"
T0 = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
ZERO_NOISE = TiRadarNoiseConfig(position_sigma_m=0.0, velocity_sigma_m_s=0.0)


def _store(
    *, x: float, y: float, z: float, yaw: float = 0.0, pitch: float = 0.0, roll: float = 0.0
) -> Store:
    return Store(
        store_id="virtual-store-lab",
        name="Virtual Store Lab",
        floor_bounds=Box2D(min_x=-30.0, min_y=-30.0, max_x=30.0, max_y=30.0),
        sensors=[
            Sensor(
                sensor_id=SENSOR_ID,
                modality=SourceType.MMWAVE,
                pose=SensorPose(
                    position=WorldCoordinate(x=x, y=y, z=z), yaw=yaw, pitch=pitch, roll=roll
                ),
            )
        ],
    )


def _rig(
    *,
    x: float,
    y: float,
    z: float,
    yaw: float = 0.0,
    pitch: float = 0.0,
    roll: float = 0.0,
    coordinates: TiCoordinateConvention | None = None,
    noise: TiRadarNoiseConfig | None = None,
    fov: TiRadarFovConfig | None = None,
    seed: int = 42,
) -> tuple[TiRadarEmulator, TiFrameParser, TiTargetNormalizer, Store]:
    """One sensor pose, wired up identically on the emulator side and the real
    parser/normalizer side, sharing the same coordinate convention and pose."""
    store = _store(x=x, y=y, z=z, yaw=yaw, pitch=pitch, roll=roll)
    registry = StoreRegistry(store)
    coords = coordinates or TiCoordinateConvention()
    normalizer = TiTargetNormalizer(
        TiAdapterConfig(sensor_id=SENSOR_ID, coordinates=coords), registry, clock=lambda: T0
    )
    parser = TiFrameParser()
    emulator = TiRadarEmulator(
        TiRadarEmulatorConfig(
            sensor_id=SENSOR_ID,
            pose=store.sensors[0].pose,
            coordinates=coords,
            fov=fov or TiRadarFovConfig(),
            noise=noise if noise is not None else TiRadarNoiseConfig(),
            seed=seed,
        )
    )
    return emulator, parser, normalizer, store


def _world_point_ahead(store: Store, *, forward: float, left: float, up: float) -> WorldCoordinate:
    """A world point at the given sensor-frame offset from ``store``'s one sensor.

    Guarantees FOV membership can be reasoned about directly in sensor-frame terms
    (``forward``/``left`` -> range and azimuth), independent of the pose's yaw/pitch.
    """
    pose = store.sensors[0].pose
    transform = RigidTransform.from_pose(SENSOR_ID, pose)
    return transform.to_world(SensorCoordinate(x=forward, y=left, z=up, frame_id=SENSOR_ID))


def _round_trip(
    emulator: TiRadarEmulator,
    parser: TiFrameParser,
    normalizer: TiTargetNormalizer,
    *,
    world_point: WorldCoordinate,
    world_velocity: Velocity | None = None,
    ground_truth_id: str = "GT-PERSON-001",
):
    target = VisibleTarget(
        ground_truth_id=ground_truth_id,
        position=world_point,
        velocity=world_velocity or Velocity(),
    )
    frame_bytes = emulator.emit([target], T0)
    frames = parser.feed(frame_bytes)
    assert parser.stats.frames_rejected == 0
    assert len(frames) == 1
    observations = normalizer.frame_to_observations(frames[0], received_at=T0)
    return observations


# --------------------------------------------------------------- pose/point fixtures

# (sensor x, y, z, yaw_rad, pitch_rad) — non-zero yaw, offset positions, a
# realistic mount height, and one pitched mount, per the mission brief.
SENSOR_POSES = [
    pytest.param(0.0, 0.0, 2.4, 0.0, 0.0, id="origin-no-yaw"),
    pytest.param(5.0, -3.0, 2.7, math.pi / 2, 0.0, id="offset-yaw-90"),
    pytest.param(-4.0, 6.0, 3.0, math.radians(37.0), 0.0, id="offset-yaw-37"),
    pytest.param(2.0, 2.0, 2.2, math.pi, 0.0, id="yaw-180-realistic-mount"),
    pytest.param(0.0, 0.0, 2.5, math.radians(15.0), math.radians(-10.0), id="pitched-mount"),
]

# Sensor-frame (forward, left, up) offsets used to build the actual test target: all
# comfortably inside TiRadarFovConfig's defaults (max_range_m=12, half_angle=60deg).
TARGET_OFFSET = (5.0, 2.0, -1.2)

NON_DEFAULT_CONVENTION = TiCoordinateConvention(
    forward_axis="x", lateral_positive="left", z_origin="sensor"
)


@pytest.mark.parametrize(("sx", "sy", "sz", "yaw", "pitch"), SENSOR_POSES)
def test_round_trip_exact_without_noise_default_convention(sx, sy, sz, yaw, pitch) -> None:
    emulator, parser, normalizer, store = _rig(
        x=sx, y=sy, z=sz, yaw=yaw, pitch=pitch, noise=ZERO_NOISE
    )
    world_point = _world_point_ahead(
        store, forward=TARGET_OFFSET[0], left=TARGET_OFFSET[1], up=TARGET_OFFSET[2]
    )
    observations = _round_trip(emulator, parser, normalizer, world_point=world_point)

    assert len(observations) == 1
    coord = observations[0].coordinate
    assert coord.x == pytest.approx(world_point.x, abs=1e-6)
    assert coord.y == pytest.approx(world_point.y, abs=1e-6)
    assert coord.z == pytest.approx(world_point.z, abs=1e-6)


@pytest.mark.parametrize(("sx", "sy", "sz", "yaw", "pitch"), SENSOR_POSES)
def test_round_trip_exact_without_noise_non_default_convention(sx, sy, sz, yaw, pitch) -> None:
    emulator, parser, normalizer, store = _rig(
        x=sx,
        y=sy,
        z=sz,
        yaw=yaw,
        pitch=pitch,
        coordinates=NON_DEFAULT_CONVENTION,
        noise=ZERO_NOISE,
    )
    world_point = _world_point_ahead(
        store, forward=TARGET_OFFSET[0], left=TARGET_OFFSET[1], up=TARGET_OFFSET[2]
    )
    observations = _round_trip(emulator, parser, normalizer, world_point=world_point)

    assert len(observations) == 1
    coord = observations[0].coordinate
    assert coord.x == pytest.approx(world_point.x, abs=1e-6)
    assert coord.y == pytest.approx(world_point.y, abs=1e-6)
    assert coord.z == pytest.approx(world_point.z, abs=1e-6)


def test_round_trip_bounded_with_noise() -> None:
    sigma = 0.05
    noise = TiRadarNoiseConfig(position_sigma_m=sigma, velocity_sigma_m_s=sigma)
    emulator, parser, normalizer, store = _rig(
        x=1.0, y=-2.0, z=2.6, yaw=math.radians(20.0), noise=noise
    )
    world_point = _world_point_ahead(store, forward=4.0, left=-1.0, up=-1.6)

    observations = _round_trip(emulator, parser, normalizer, world_point=world_point)

    assert len(observations) == 1
    coord = observations[0].coordinate
    # 8 sigma is an extremely generous bound (essentially never trips for a
    # correctly-implemented Gaussian) while still catching a gross axis/offset bug.
    bound = 8 * sigma
    assert coord.x == pytest.approx(world_point.x, abs=bound)
    assert coord.y == pytest.approx(world_point.y, abs=bound)
    assert coord.z == pytest.approx(world_point.z, abs=bound)


def test_velocity_round_trips_too() -> None:
    emulator, parser, normalizer, store = _rig(
        x=0.0, y=0.0, z=2.4, yaw=math.pi / 4, noise=ZERO_NOISE
    )
    world_point = _world_point_ahead(store, forward=5.0, left=1.0, up=-1.4)
    world_velocity = Velocity(vx=0.5, vy=-0.3, vz=0.0)

    observations = _round_trip(
        emulator, parser, normalizer, world_point=world_point, world_velocity=world_velocity
    )

    assert len(observations) == 1
    velocity = observations[0].velocity
    assert velocity is not None
    assert velocity.vx == pytest.approx(world_velocity.vx, abs=1e-6)
    assert velocity.vy == pytest.approx(world_velocity.vy, abs=1e-6)
    assert velocity.vz == pytest.approx(world_velocity.vz, abs=1e-6)


# ------------------------------------------------------------------------------ FOV


def test_target_outside_max_range_is_excluded_not_errored() -> None:
    fov = TiRadarFovConfig(max_range_m=5.0)
    emulator, parser, normalizer, store = _rig(x=0.0, y=0.0, z=2.4, fov=fov)
    # 20m straight ahead: outside the 5m FOV range limit but dead on boresight.
    far_point = _world_point_ahead(store, forward=20.0, left=0.0, up=-1.4)

    target = VisibleTarget(ground_truth_id="GT-FAR", position=far_point, velocity=Velocity())
    frame_bytes = emulator.emit([target], T0)
    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].targets == ()
    assert normalizer.frame_to_observations(frames[0], received_at=T0) == []


def test_target_outside_horizontal_half_angle_is_excluded() -> None:
    fov = TiRadarFovConfig(max_range_m=20.0, horizontal_half_angle_rad=math.radians(10.0))
    emulator, parser, _normalizer, store = _rig(x=0.0, y=0.0, z=2.4, fov=fov)
    # Directly to the sensor's side (90 degrees off boresight): outside a 10 degree
    # half-angle FOV even though well within range.
    side_point = _world_point_ahead(store, forward=0.0, left=5.0, up=-1.4)

    target = VisibleTarget(ground_truth_id="GT-SIDE", position=side_point, velocity=Velocity())
    frame_bytes = emulator.emit([target], T0)
    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].targets == ()


# -------------------------------------------------------------------- native ids


def test_native_id_is_reused_after_a_target_leaves_and_a_new_one_appears() -> None:
    noise = TiRadarNoiseConfig(native_id_reuse_probability=1.0)
    emulator, parser, normalizer, store = _rig(x=0.0, y=0.0, z=2.4, noise=noise)

    leaving_point = _world_point_ahead(store, forward=3.0, left=0.5, up=-1.0)
    leaving = VisibleTarget(
        ground_truth_id="GT-LEAVING", position=leaving_point, velocity=Velocity()
    )
    frame_bytes = emulator.emit([leaving], T0)
    frames = parser.feed(frame_bytes)
    assert len(frames) == 1
    assert len(frames[0].targets) == 1
    first_native_id = frames[0].targets[0].native_track_id

    # The leaving target is gone from the world entirely next tick: its native id
    # is retired.
    frame_bytes = emulator.emit([], T0)
    frames = parser.feed(frame_bytes)
    assert len(frames) == 1
    assert frames[0].targets == ()

    # A brand-new target appears; with native_id_reuse_probability=1.0 it must be
    # assigned the retired id rather than a fresh one.
    new_point = _world_point_ahead(store, forward=4.0, left=-0.5, up=-1.0)
    new_target = VisibleTarget(ground_truth_id="GT-NEW", position=new_point, velocity=Velocity())
    frame_bytes = emulator.emit([new_target], T0)
    frames = parser.feed(frame_bytes)
    assert len(frames) == 1
    assert len(frames[0].targets) == 1
    assert frames[0].targets[0].native_track_id == first_native_id

    observations = normalizer.frame_to_observations(frames[0], received_at=T0)
    assert observations[0].metadata[NATIVE_TI_TRACK_ID_KEY] == first_native_id


def test_native_id_is_never_derived_from_ground_truth_id() -> None:
    emulator, parser, _normalizer, store = _rig(x=0.0, y=0.0, z=2.4)
    world_point = _world_point_ahead(store, forward=3.0, left=0.0, up=-1.0)
    target = VisibleTarget(
        ground_truth_id="GT-PERSON-999999", position=world_point, velocity=Velocity()
    )
    frame_bytes = emulator.emit([target], T0)
    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert len(frames[0].targets) == 1
    native_id = frames[0].targets[0].native_track_id
    assert native_id == 1  # first-ever allocation, independent of the huge gt id
    assert "GT-PERSON-999999" not in frame_bytes.decode("latin-1")


# -------------------------------------------------------------------- determinism


def test_same_seed_produces_identical_bytes() -> None:
    noise = TiRadarNoiseConfig(position_sigma_m=0.05, velocity_sigma_m_s=0.05)
    emulator_a, _p1, _n1, store_a = _rig(x=0.0, y=0.0, z=2.4, noise=noise, seed=7)
    emulator_b, _p2, _n2, _store_b = _rig(x=0.0, y=0.0, z=2.4, noise=noise, seed=7)
    world_point = _world_point_ahead(store_a, forward=4.0, left=1.0, up=-1.0)
    target = VisibleTarget(ground_truth_id="GT-1", position=world_point, velocity=Velocity())

    bytes_a = emulator_a.emit([target], T0)
    bytes_b = emulator_b.emit([target], T0)

    assert bytes_a == bytes_b
    assert bytes_a != b""


def test_different_seed_produces_different_bytes() -> None:
    noise = TiRadarNoiseConfig(position_sigma_m=0.05, velocity_sigma_m_s=0.05)
    emulator_a, _p1, _n1, store_a = _rig(x=0.0, y=0.0, z=2.4, noise=noise, seed=7)
    emulator_b, _p2, _n2, _store_b = _rig(x=0.0, y=0.0, z=2.4, noise=noise, seed=99)
    world_point = _world_point_ahead(store_a, forward=4.0, left=1.0, up=-1.0)
    target = VisibleTarget(ground_truth_id="GT-1", position=world_point, velocity=Velocity())

    bytes_a = emulator_a.emit([target], T0)
    bytes_b = emulator_b.emit([target], T0)

    assert bytes_a != bytes_b


# ------------------------------------------------------------------ frame numbering


def test_frame_numbers_are_strictly_non_decreasing_until_deliberate_reset() -> None:
    emulator, parser, _normalizer, store = _rig(
        x=0.0, y=0.0, z=2.4, noise=TiRadarNoiseConfig(frame_jitter_max_skip=3), seed=3
    )
    world_point = _world_point_ahead(store, forward=4.0, left=1.0, up=-1.0)
    target = VisibleTarget(ground_truth_id="GT-1", position=world_point, velocity=Velocity())
    seen_numbers = []
    for _ in range(10):
        frame_bytes = emulator.emit([target], T0)
        if not frame_bytes:
            continue
        frames = parser.feed(frame_bytes)
        seen_numbers.extend(f.frame_number for f in frames)

    assert seen_numbers == sorted(seen_numbers)
    assert len(set(seen_numbers)) == len(seen_numbers)  # jitter never collides here

    emulator.reset_stream(start_frame_number=0)
    assert emulator.frame_number == 0
