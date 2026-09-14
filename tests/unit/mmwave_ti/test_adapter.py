"""TI target list -> canonical PersonObservation: golden normalization, rotations,
timestamps, native-id policy, uncertainty/confidence policy, duplicates and restarts."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from radiowave.adapters.mmwave.ti.adapter import (
    CONFIDENCE_SOURCE_KEY,
    NATIVE_CONFIDENCE_KEY,
    NATIVE_FIRMWARE_PROFILE_KEY,
    NATIVE_FRAME_NUMBER_KEY,
    NATIVE_STREAM_GENERATION_KEY,
    NATIVE_TARGET_LAYOUT_KEY,
    NATIVE_TI_POSITION_KEY,
    TiTargetNormalizer,
)
from radiowave.adapters.mmwave.ti.config import TiCoordinateConvention, TiObservationPolicy
from radiowave.adapters.mmwave.ti.models import TiFrame, TiTarget
from radiowave.adapters.mmwave.ti.parser import TiFrameParser
from radiowave.contracts.observations import (
    NATIVE_POSITION_KEY,
    NATIVE_TRACK_KEY,
    PersonObservation,
)
from radiowave.contracts.store import SourceType
from radiowave.digital_twin.registry import StoreRegistry
from tests.fixtures.ti_mmwave.builder import build_target_frame
from tests.unit.mmwave_ti.support import RADAR, T0, adapter_config, store_with_radar

TOL = 1e-6


def target(
    tid: int = 3,
    x: float = 1.0,
    y: float = 3.0,
    z: float = 1.0,
    vx: float = 0.1,
    vy: float = 0.5,
    vz: float = 0.0,
    confidence: float | None = 0.9,
) -> TiTarget:
    return TiTarget(
        native_track_id=tid,
        x=x,
        y=y,
        z=z,
        vx=vx,
        vy=vy,
        vz=vz,
        ax=0.0,
        ay=0.0,
        az=0.0,
        error_covariance=(),
        gating_gain=4.0,
        confidence=confidence,
    )


def frame(number: int, *targets: TiTarget, cycles: int = 12345) -> TiFrame:
    return TiFrame(
        version=0x03060000,
        total_packet_len=0,
        platform=0xA6843,
        frame_number=number,
        time_cpu_cycles=cycles,
        num_detected_objects=0,
        num_tlvs=1,
        subframe_number=0,
        targets=tuple(targets),
        points=(),
        target_indices=(),
        heights=(),
        presence=None,
        unknown_tlvs=(),
        target_record_layout="3d_v2",
        padding_bytes=0,
    )


def normalizer(
    *, x: float = 0.0, y: float = 0.0, z: float = 2.4, yaw: float = 0.0, **overrides: object
) -> TiTargetNormalizer:
    store = store_with_radar(x=x, y=y, z=z, yaw=yaw)
    return TiTargetNormalizer(adapter_config(**overrides), StoreRegistry(store), clock=lambda: T0)


def one(observations: list[PersonObservation]) -> PersonObservation:
    assert len(observations) == 1
    return observations[0]


# ------------------------------------------------------------------ golden


def test_golden_ti_target_to_person_observation() -> None:
    """A TI target 3 m ahead and 1 m to the right of a radar on the south wall facing
    north (pose (6, 0, 2.4), yaw +90 deg) is a person at world (7, 3, 1)."""
    norm = normalizer(x=6.0, y=0.0, z=2.4, yaw=math.pi / 2)
    observation = one(norm.frame_to_observations(frame(17, target()), received_at=T0))

    assert observation.sensor_id == RADAR
    assert observation.source_type == SourceType.MMWAVE
    assert observation.timestamp == T0
    assert observation.timestamp.tzinfo is not None
    assert observation.coordinate.x == pytest.approx(7.0, abs=TOL)
    assert observation.coordinate.y == pytest.approx(3.0, abs=TOL)
    assert observation.coordinate.z == pytest.approx(1.0, abs=TOL)
    assert observation.velocity is not None
    assert observation.velocity.vx == pytest.approx(0.1, abs=TOL)
    assert observation.velocity.vy == pytest.approx(0.5, abs=TOL)
    assert observation.velocity.vz == pytest.approx(0.0, abs=TOL)
    assert observation.uncertainty.sigma_x == pytest.approx(0.35)
    assert observation.uncertainty.sigma_y == pytest.approx(0.35)
    assert observation.confidence == pytest.approx(0.9)
    assert observation.observation_id == f"mmwave:{RADAR}:1"
    assert observation.scenario_id is None

    metadata = observation.metadata
    assert metadata[NATIVE_TRACK_KEY] == "3"
    assert metadata[NATIVE_FRAME_NUMBER_KEY] == 17
    assert metadata[NATIVE_STREAM_GENERATION_KEY] == 1
    assert metadata[NATIVE_FIRMWARE_PROFILE_KEY] == "ti-3d-people-counting"
    assert metadata[NATIVE_TARGET_LAYOUT_KEY] == "3d_v2"
    assert metadata["native_time_cpu_cycles"] == 12345
    assert metadata[NATIVE_CONFIDENCE_KEY] == pytest.approx(0.9)
    assert metadata[CONFIDENCE_SOURCE_KEY] == "firmware"
    assert metadata[NATIVE_TI_POSITION_KEY] == {"x": 1.0, "y": 3.0, "z": 1.0}
    # The sensor-frame position (x forward, y left, z relative to the sensor).
    native = metadata[NATIVE_POSITION_KEY]
    assert native["frame_id"] == RADAR
    assert native["x"] == pytest.approx(3.0, abs=TOL)
    assert native["y"] == pytest.approx(-1.0, abs=TOL)
    assert native["z"] == pytest.approx(1.0 - 2.4, abs=TOL)


def test_golden_matches_the_parser_output_end_to_end() -> None:
    """Bytes from the fixture builder decode to the same observation as the DTO path."""
    parser = TiFrameParser()
    frames = parser.feed(
        build_target_frame(
            17, [{"tid": 3, "x": 1.0, "y": 3.0, "z": 1.0, "vx": 0.1, "vy": 0.5, "confidence": 0.9}]
        )
    )
    assert len(frames) == 1
    norm = normalizer(x=6.0, y=0.0, z=2.4, yaw=math.pi / 2)
    observation = one(norm.frame_to_observations(frames[0], received_at=T0))
    assert (observation.coordinate.x, observation.coordinate.y) == pytest.approx((7.0, 3.0))
    assert observation.metadata[NATIVE_TRACK_KEY] == "3"


# --------------------------------------------------------------- rotations


@pytest.mark.parametrize(
    ("pose", "ti_xyz", "expected_world"),
    [
        # yaw 0: radar at the origin facing +x; TI forward (+y) becomes world +x,
        # TI right (+x) becomes world -y.
        (dict(x=0.0, y=0.0, z=2.4, yaw=0.0), (1.0, 3.0, 1.0), (3.0, -1.0, 1.0)),
        # yaw 90: facing +y; right becomes +x.
        (dict(x=0.0, y=0.0, z=2.4, yaw=math.pi / 2), (1.0, 3.0, 1.0), (1.0, 3.0, 1.0)),
        # translated, yaw 0.
        (dict(x=4.0, y=2.0, z=2.4, yaw=0.0), (1.0, 3.0, 1.0), (7.0, 1.0, 1.0)),
        # negative native x (target to the left), yaw 0.
        (dict(x=0.0, y=0.0, z=2.4, yaw=0.0), (-1.0, 3.0, 1.0), (3.0, 1.0, 1.0)),
        # yaw 180: facing -x; forward becomes -x, right becomes +y.
        (dict(x=0.0, y=0.0, z=2.4, yaw=math.pi), (1.0, 3.0, 1.0), (-3.0, 1.0, 1.0)),
        # height: TI z is above the floor, so the world z is the TI z whatever the mount.
        (dict(x=0.0, y=0.0, z=3.0, yaw=0.0), (0.0, 2.0, 1.2), (2.0, 0.0, 1.2)),
    ],
)
def test_sensor_pose_transforms_ti_axes_into_world(
    pose: dict[str, float], ti_xyz: tuple[float, float, float], expected_world: tuple[float, ...]
) -> None:
    norm = normalizer(**pose)
    ti_target = target(x=ti_xyz[0], y=ti_xyz[1], z=ti_xyz[2])
    observation = one(norm.frame_to_observations(frame(1, ti_target), T0))
    assert observation.coordinate.x == pytest.approx(expected_world[0], abs=TOL)
    assert observation.coordinate.y == pytest.approx(expected_world[1], abs=TOL)
    assert observation.coordinate.z == pytest.approx(expected_world[2], abs=TOL)


def test_velocity_rotates_but_never_translates() -> None:
    norm = normalizer(x=5.0, y=5.0, z=2.4, yaw=math.pi / 2)
    observation = one(norm.frame_to_observations(frame(1, target(vx=1.0, vy=0.0)), T0))
    assert observation.velocity is not None
    # TI +x (right) with the radar facing north (+y) is world +x.
    assert observation.velocity.vx == pytest.approx(1.0, abs=TOL)
    assert observation.velocity.vy == pytest.approx(0.0, abs=TOL)


def test_z_origin_sensor_convention_skips_the_height_offset() -> None:
    norm = normalizer(z=2.4, coordinates=TiCoordinateConvention(z_origin="sensor"))
    observation = one(norm.frame_to_observations(frame(1, target(z=-1.0)), T0))
    assert observation.coordinate.z == pytest.approx(1.4, abs=TOL)


def test_forward_axis_x_and_lateral_left_conventions() -> None:
    norm = normalizer(coordinates=TiCoordinateConvention(forward_axis="x", lateral_positive="left"))
    observation = one(norm.frame_to_observations(frame(1, target(x=3.0, y=1.0)), T0))
    assert observation.coordinate.x == pytest.approx(3.0, abs=TOL)
    assert observation.coordinate.y == pytest.approx(1.0, abs=TOL)


# -------------------------------------------------------------- timestamps


def test_timestamp_is_host_utc_receive_time_not_device_cycles() -> None:
    norm = normalizer()
    observation = one(norm.frame_to_observations(frame(1, target(), cycles=999_999), T0))
    assert observation.timestamp == T0
    assert observation.metadata["native_time_cpu_cycles"] == 999_999


def test_clock_is_used_when_no_receive_time_is_given() -> None:
    later = T0 + timedelta(seconds=5)
    store = store_with_radar()
    norm = TiTargetNormalizer(adapter_config(), StoreRegistry(store), clock=lambda: later)
    observation = one(norm.frame_to_observations(frame(1, target())))
    assert observation.timestamp == later


def test_naive_receive_time_is_rejected() -> None:
    norm = normalizer()
    with pytest.raises(ValueError, match="timezone-aware"):
        norm.frame_to_observations(frame(1, target()), datetime(2026, 1, 1))


def test_non_utc_receive_time_is_normalized_to_utc() -> None:
    from datetime import timezone

    plus_three = datetime(2026, 3, 1, 15, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    norm = normalizer()
    observation = one(norm.frame_to_observations(frame(1, target()), plus_three))
    assert observation.timestamp == T0
    assert observation.timestamp.tzinfo == UTC


# ------------------------------------------------ uncertainty / confidence


def test_uncertainty_is_the_configured_baseline_not_zero() -> None:
    norm = normalizer(observation=TiObservationPolicy(baseline_sigma_m=0.5))
    observation = one(norm.frame_to_observations(frame(1, target()), T0))
    assert observation.uncertainty.sigma_x == pytest.approx(0.5)
    assert observation.uncertainty.sigma_x > 0.0


def test_firmware_confidence_is_capped_never_one() -> None:
    norm = normalizer()
    observation = one(norm.frame_to_observations(frame(1, target(confidence=1.0)), T0))
    assert observation.confidence == pytest.approx(0.9)
    assert observation.metadata[CONFIDENCE_SOURCE_KEY] == "firmware"
    assert norm.counters.confidence_from_firmware == 1


@pytest.mark.parametrize("raw", [None, 1.7, -0.2, float("nan")])
def test_missing_or_unusable_firmware_confidence_falls_back_to_baseline(
    raw: float | None,
) -> None:
    norm = normalizer()
    observation = one(norm.frame_to_observations(frame(1, target(confidence=raw)), T0))
    assert observation.confidence == pytest.approx(0.6)
    assert observation.metadata[CONFIDENCE_SOURCE_KEY] == "baseline"
    assert norm.counters.confidence_from_baseline == 1


def test_firmware_confidence_can_be_disabled() -> None:
    norm = normalizer(observation=TiObservationPolicy(use_firmware_confidence=False))
    observation = one(norm.frame_to_observations(frame(1, target(confidence=0.8)), T0))
    assert observation.confidence == pytest.approx(0.6)


# ---------------------------------------------------- native ids / dedup


def test_native_track_id_is_metadata_only_and_a_string() -> None:
    norm = normalizer()
    observation = one(norm.frame_to_observations(frame(1, target(tid=42)), T0))
    assert observation.metadata[NATIVE_TRACK_KEY] == "42"
    assert "42" not in observation.observation_id


def test_sequence_numbers_are_unique_across_frames_and_targets() -> None:
    norm = normalizer()
    first = norm.frame_to_observations(frame(1, target(tid=1), target(tid=2)), T0)
    second = norm.frame_to_observations(frame(2, target(tid=1)), T0)
    ids = [o.observation_id for o in first + second]
    assert ids == [f"mmwave:{RADAR}:1", f"mmwave:{RADAR}:2", f"mmwave:{RADAR}:3"]


def test_duplicate_frame_number_in_one_generation_is_dropped() -> None:
    norm = normalizer()
    assert len(norm.frame_to_observations(frame(7, target()), T0)) == 1
    assert norm.frame_to_observations(frame(7, target()), T0) == []
    assert norm.counters.frames_duplicate == 1
    assert norm.counters.frames_seen == 2


def test_backwards_frame_number_opens_a_new_generation_and_is_not_a_duplicate() -> None:
    norm = normalizer()
    norm.frame_to_observations(frame(500, target()), T0)
    observation = one(norm.frame_to_observations(frame(3, target()), T0))
    assert norm.counters.frames_restart == 1
    assert norm.generation == 2
    assert observation.metadata[NATIVE_STREAM_GENERATION_KEY] == 2
    # The same frame number again in the new generation IS a duplicate...
    assert norm.frame_to_observations(frame(3, target()), T0) == []
    # ...but frame 500 from the previous generation is a fresh frame now.
    assert len(norm.frame_to_observations(frame(500, target()), T0)) == 1


def test_explicit_reconnect_generation_resets_the_last_frame_number() -> None:
    norm = normalizer()
    norm.frame_to_observations(frame(10, target()), T0)
    norm.new_generation("reconnect")
    assert len(norm.frame_to_observations(frame(10, target()), T0)) == 1
    assert norm.counters.frames_restart == 0
    assert norm.generation == 2


# ----------------------------------------------------------- fail safe


def test_implausible_targets_are_dropped_and_counted() -> None:
    norm = normalizer(observation=TiObservationPolicy(max_range_m=5.0, max_abs_height_m=3.0))
    observations = norm.frame_to_observations(
        frame(1, target(tid=1, y=50.0), target(tid=2, z=9.0), target(tid=3, y=2.0)), T0
    )
    assert [o.metadata[NATIVE_TRACK_KEY] for o in observations] == ["3"]
    assert norm.counters.targets_dropped_implausible == 2
    assert norm.counters.targets_seen == 3


def test_target_less_frame_emits_nothing_but_counts() -> None:
    norm = normalizer()
    assert norm.frame_to_observations(frame(1), T0) == []
    assert norm.counters.frames_seen == 1
    assert norm.counters.last_frame_number == 1


def test_every_observation_is_finite_and_recordable() -> None:
    norm = normalizer(x=6.0, y=0.0, z=2.4, yaw=math.pi / 2)
    for number in range(1, 20):
        for observation in norm.frame_to_observations(
            frame(number, target(x=0.3 * number, y=2.0 + 0.1 * number)), T0
        ):
            assert math.isfinite(observation.coordinate.x)
            assert math.isfinite(observation.coordinate.y)
            observation.model_dump_json()  # JSON-safe metadata


def test_wrong_modality_sensor_is_refused() -> None:
    store = store_with_radar()
    rfid = store.sensors[0].model_copy(update={"modality": SourceType.RFID})
    store = store.model_copy(update={"sensors": [rfid]})
    with pytest.raises(ValueError, match="MMWAVE"):
        TiTargetNormalizer(adapter_config(), StoreRegistry(store))
