"""World ground truth -> TI-native target -> real TI UART bytes.

:class:`TiRadarEmulator` is the virtual counterpart of a physical IWR6843 board
running the "3D people counting" demo firmware. It never constructs
:class:`~radiowave.adapters.mmwave.ti.models.TiFrame`,
:class:`~radiowave.adapters.mmwave.base.NativeRadarSample`, or
:class:`~radiowave.contracts.observations.PersonObservation` directly — those are
what the REAL parser (:mod:`radiowave.adapters.mmwave.ti.parser`) and normalizer
(:mod:`radiowave.adapters.mmwave.ti.adapter`) produce from the bytes this module
writes, exactly as they would from a real board's UART. That is what makes this a
useful lab sensor rather than a shortcut: any bug in the real parsing/normalization
path is exercised the same way here as it would be against real hardware.

Owns no world/actor model. The only input this module understands about "what is
out there" is :class:`VisibleTarget` — a world-frame position/velocity plus a
ground-truth id used purely for this emulator's own internal bookkeeping
(continuity of its native track ids, FOV membership). The ground-truth id is never
written into the wire bytes: see the module docstring on :class:`VisibleTarget`.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from radiowave.adapters.mmwave.ti.config import TiCoordinateConvention
from radiowave.adapters.mmwave.ti.protocol import TI_3D_PEOPLE_COUNTING, TiFirmwareProfile
from radiowave.contracts.geometry import SensorCoordinate, Velocity, WorldCoordinate
from radiowave.contracts.store import SensorPose
from radiowave.digital_twin.geometry import RigidTransform
from radiowave.simulator.lab.sensors.ti_encoder import (
    encode_frame,
    encode_target_list_tlv,
    encode_target_record,
)

# A new, previously-unused per-modality seed offset, following the established
# convention in radiowave/simulator/generators.py (_RADAR_SEED_OFFSET=101,
# _RFID_SEED_OFFSET=202, _VISION_SEED_OFFSET=303). This emulator is a distinct
# sensor from the synthetic NativeRadarSample generator above, so it gets its own
# offset rather than reusing 101, keeping the two RNG streams independent even if
# both are ever seeded from the same scenario seed in one process.
_TI_RADAR_SEED_OFFSET = 404

_MAX_UINT32 = 2**32 - 1


@dataclass(frozen=True, slots=True)
class VisibleTarget:
    """One body the world hands the radar for one simulation tick.

    ``ground_truth_id`` (e.g. ``"GT-PERSON-001"``) is provenance for this
    emulator's own internal tracking bookkeeping ONLY (FOV membership, id
    continuity across frames). It is never encoded into the TI packet: the wire
    format has no field for it, and the emulator's own TI-native track ids are
    assigned independently (see :meth:`TiRadarEmulator._allocate_native_id`), the
    same way a real firmware's tracker assigns ids with no knowledge of who a
    person "really" is.
    """

    ground_truth_id: str
    position: WorldCoordinate
    velocity: Velocity


@dataclass(frozen=True, slots=True)
class TiRadarFovConfig:
    """Field of view a target must be inside (sensor frame) to be reported at all.

    ASSUMPTION, not a measured antenna pattern: the IWR6843 people-counting demo's
    real azimuth FOV varies by antenna config and firmware tuning. Both bounds here
    are deliberately kept inside :class:`~radiowave.adapters.mmwave.ti.config.TiObservationPolicy`'s
    independent, silent drop thresholds (``max_range_m=15.0``, checked on horizontal
    range; ``max_abs_height_m=4.0``) so a target this emulator decides is "visible"
    is never invisibly discarded one layer up in the normalizer with no signal back
    to the caller.
    """

    max_range_m: float = 12.0
    """ASSUMPTION: horizontal (sensor-frame hypot(x, y)) range limit, metres."""
    min_range_m: float = 0.0
    """ASSUMPTION: targets closer than this (near-field blind spot) are dropped."""
    horizontal_half_angle_rad: float = math.radians(60.0)
    """ASSUMPTION: azimuth half-angle from boresight a target must be inside."""


@dataclass(frozen=True, slots=True)
class TiRadarNoiseConfig:
    """All noise/fault knobs, seeded and explicit. Every field is an ASSUMED
    provenance value for lab testing, not a measurement of a real IWR6843 — see
    ``docs/experiments`` for the bring-up process that will eventually replace
    numbers like these with calibrated ones.
    """

    position_sigma_m: float = 0.05
    """ASSUMPTION: 1-sigma Gaussian noise added to each TI-native x/y/z, metres."""
    velocity_sigma_m_s: float = 0.05
    """ASSUMPTION: 1-sigma Gaussian noise added to each TI-native vx/vy/vz, m/s."""
    missed_frame_probability: float = 0.0
    """ASSUMPTION: probability a whole frame is produced by the firmware but lost
    in UART transit (emit() returns ``b""`` that tick). The frame counter and
    native-id bookkeeping still advance, mirroring a real dropped packet rather
    than a paused tracker."""
    target_dropout_probability: float = 0.0
    """ASSUMPTION: per-target, per-frame probability of a transient miss: the
    target is skipped in this frame's TLV but keeps its native id, so it resumes
    under the same id next frame (unlike a target leaving the FOV, which retires
    its id — see :meth:`TiRadarEmulator.emit`)."""
    false_target_rate: float = 0.0
    """ASSUMPTION: per-frame probability of one spurious clutter target with no
    corresponding :class:`VisibleTarget`, at a random in-FOV position."""
    native_id_switch_probability: float = 0.0
    """ASSUMPTION: per-track, per-frame probability the track's native id is
    deliberately retired and replaced mid-track, simulating a firmware
    track-loss/reacquire that a real tracker can do even while a person keeps
    walking continuously."""
    native_id_reuse_probability: float = 0.0
    """ASSUMPTION: probability a newly assigned native id (new track, or the
    replacement half of a native-id switch) is drawn from the small pool of ids
    recently retired by other tracks, rather than a never-before-used id — this is
    what lets the normalizer's stream-generation/native-id-hint scoping be
    exercised against a genuinely reused raw TI id, not just a monotonically
    growing one."""
    frame_jitter_max_skip: int = 0
    """ASSUMPTION: upper bound (inclusive) on an extra random jump added to the
    frame counter between ticks, simulating a firmware whose frame numbers are not
    exactly one apart. The counter is always strictly non-decreasing regardless of
    this value; only :meth:`TiRadarEmulator.reset_stream` can make it go backwards,
    to deliberately simulate a device restart."""


@dataclass(frozen=True, slots=True)
class TiRadarEmulatorConfig:
    """Everything one virtual TI radar needs: identity, pose, and its policies."""

    sensor_id: str
    pose: SensorPose
    coordinates: TiCoordinateConvention = field(default_factory=TiCoordinateConvention)
    fov: TiRadarFovConfig = field(default_factory=TiRadarFovConfig)
    noise: TiRadarNoiseConfig = field(default_factory=TiRadarNoiseConfig)
    profile: TiFirmwareProfile = TI_3D_PEOPLE_COUNTING
    seed: int = 0


def _inverse_convention(
    convention: TiCoordinateConvention,
    *,
    forward: float,
    left: float,
    up: float,
    sensor_height_m: float,
) -> tuple[float, float, float]:
    """The exact algebraic inverse of :meth:`TiCoordinateConvention.to_sensor_frame`.

    That method computes, from TI-native ``(x, y, z)``::

        forward, lateral = (y, x) if forward_axis == "y" else (x, y)
        left = -lateral if lateral_positive == "right" else lateral
        up = z - sensor_height_m if z_origin == "floor" else z

    Undone here in reverse order: recover ``lateral`` from ``left``, then ``x, y``
    from ``forward, lateral`` (undoing whichever axis swap ``forward_axis`` picked),
    then ``z`` from ``up``. ``sensor_height_m`` must be passed the same way the
    normalizer passes it: the sensor's mounted height for a position (so the floor
    reference shifts back to sensor-relative-then-native), and ``0.0`` for a
    velocity/acceleration direction (no origin to shift).
    """
    lateral = -left if convention.lateral_positive == "right" else left
    if convention.forward_axis == "y":
        x, y = lateral, forward
    else:
        x, y = forward, lateral
    z = up + sensor_height_m if convention.z_origin == "floor" else up
    return (x, y, z)


@dataclass(slots=True)
class _Track:
    """Internal, per-ground-truth-id bookkeeping. Never serialized."""

    native_id: int


class TiRadarEmulator:
    """Virtual IWR6843: world :class:`VisibleTarget` list -> real TI UART bytes.

    One instance owns one seeded RNG stream and one native-track-id space, exactly
    as one physical radar connection would. Call :meth:`emit` once per simulation
    tick; it returns the bytes a real board's data UART would have produced for
    that tick (zero bytes for a simulated missed frame).
    """

    def __init__(self, config: TiRadarEmulatorConfig) -> None:
        self._config = config
        self._rng = np.random.default_rng(config.seed + _TI_RADAR_SEED_OFFSET)
        self._transform = RigidTransform.from_pose(config.sensor_id, config.pose)
        self._frame_number = 0
        self._tracks: dict[str, _Track] = {}
        self._retired_ids: list[int] = []
        self._force_id_reuse = False
        self._next_native_id = 1

    @property
    def frame_number(self) -> int:
        """The frame number that will be used on the *next* :meth:`emit` call."""
        return self._frame_number

    def reset_stream(self, start_frame_number: int = 0) -> None:
        """Deliberately restart the stream: frame numbers resume from
        ``start_frame_number`` (which may be lower than the last emitted one).

        This is the ONLY way this emulator's frame counter can go backwards — every
        other path (including frame jitter) is strictly non-decreasing, because a
        decreasing frame number is what makes the real normalizer declare a new
        stream generation (see ``adapter.py``'s module docstring), silently
        changing native-id continuity. Native-id tracking state is intentionally
        NOT cleared here: generation scoping of native-id hints is the
        normalizer's job, not this emulator's.
        """
        self._frame_number = start_frame_number

    # ------------------------------------------------------------------ helpers

    def _in_fov(self, forward: float, left: float) -> bool:
        fov = self._config.fov
        range_m = math.hypot(forward, left)
        if not (fov.min_range_m <= range_m <= fov.max_range_m):
            return False
        angle = abs(math.atan2(left, forward))
        return angle <= fov.horizontal_half_angle_rad

    def set_forced_id_reuse(self, enabled: bool) -> None:
        """Force the next allocations to recycle a retired id, for a scheduled fault.

        A probability alone cannot express "reuse an id *now*", which is what a
        scenario wants when it schedules a NATIVE_ID_REUSE window at the moment one
        shopper leaves and another arrives. That exact sequencing is the case Phase
        3's generation scoping has to survive, so it must be schedulable rather than
        left to chance.
        """
        self._force_id_reuse = enabled

    def _allocate_native_id(self) -> int:
        noise = self._config.noise
        # The draw happens either way, even when reuse is forced, so that turning the
        # fault on does not shift the RNG stream underneath every other noise source.
        # Enabling a fault should change what the fault governs and nothing else,
        # otherwise two runs are incomparable for reasons unrelated to the fault.
        roll = self._rng.random()
        reuse = self._force_id_reuse or roll < noise.native_id_reuse_probability
        if self._retired_ids and reuse:
            index = int(self._rng.integers(0, len(self._retired_ids)))
            return self._retired_ids.pop(index)
        native_id = self._next_native_id
        self._next_native_id += 1
        return native_id

    def _retire(self, native_id: int) -> None:
        self._retired_ids.append(native_id)
        # A small pool, not an unbounded list: real trackers only have a handful of
        # ids in flight, and an unbounded pool would make "reuse" less and less
        # likely to land on a *recently* retired id, which is the scenario worth
        # testing (see TiRadarNoiseConfig.native_id_reuse_probability).
        del self._retired_ids[:-16]

    def _next_frame_number(self) -> int:
        """Advance and return the frame number for the packet about to be built."""
        current = self._frame_number
        step = 1
        if self._config.noise.frame_jitter_max_skip > 0:
            step += int(self._rng.integers(0, self._config.noise.frame_jitter_max_skip + 1))
        self._frame_number = min(current + step, _MAX_UINT32)
        return current

    def _native_from_sensor(
        self, sensor_point: SensorCoordinate, sensor_velocity: Velocity
    ) -> tuple[float, float, float, float, float, float]:
        """Sensor-frame position/velocity -> TI-native (x, y, z, vx, vy, vz).

        The exact inverse of :meth:`TiCoordinateConvention.to_sensor_frame`.
        Position uses this sensor's mounted height; velocity uses ``0.0``,
        mirroring how the real normalizer calls ``to_sensor_frame`` twice (see
        ``adapter.py``'s ``_sample_for``) — this must match that call exactly or
        the round trip in ``tests/simulator/lab/test_ti_radar.py`` cannot land
        back on the original world position.
        """
        convention = self._config.coordinates
        sensor_height_m = self._config.pose.position.z
        x, y, z = _inverse_convention(
            convention,
            forward=sensor_point.x,
            left=sensor_point.y,
            up=sensor_point.z,
            sensor_height_m=sensor_height_m,
        )
        vx, vy, vz = _inverse_convention(
            convention,
            forward=sensor_velocity.vx,
            left=sensor_velocity.vy,
            up=sensor_velocity.vz,
            sensor_height_m=0.0,
        )
        return (x, y, z, vx, vy, vz)

    def _false_target_native_xyz(self) -> tuple[float, float, float]:
        """A plausible in-FOV TI-native position for a spurious clutter target.

        Picked directly in the sensor's forward/left/up frame (random range inside
        the configured FOV, random azimuth inside the half-angle, a small random
        height near the floor) and only then run through the same inverse
        convention as a real target, so a false target is exactly as valid a TI
        record as a real one — a real radar's clutter would be too.
        """
        fov = self._config.fov
        range_m = float(self._rng.uniform(max(fov.min_range_m, 0.1), fov.max_range_m))
        half_angle = fov.horizontal_half_angle_rad
        angle = float(self._rng.uniform(-half_angle, half_angle))
        forward = range_m * math.cos(angle)
        left = range_m * math.sin(angle)
        up = float(self._rng.uniform(-0.5, 0.5))
        convention = self._config.coordinates
        return _inverse_convention(
            convention,
            forward=forward,
            left=left,
            up=up,
            sensor_height_m=self._config.pose.position.z,
        )

    # -------------------------------------------------------------------- emit

    def emit(self, targets: Sequence[VisibleTarget], sim_time: datetime) -> bytes:
        """One simulation tick: world targets -> zero or one encoded TI frame.

        Returns ``b""`` for a simulated missed frame (see
        ``TiRadarNoiseConfig.missed_frame_probability``); otherwise the bytes of
        exactly one :func:`~radiowave.simulator.lab.sensors.ti_encoder.encode_frame`
        packet, ready to be fed straight into a real
        :class:`~radiowave.adapters.mmwave.ti.parser.TiFrameParser`.
        """
        rng = self._rng
        noise = self._config.noise

        # Which ground-truth ids are inside the FOV this tick, computed once so it
        # can drive both "assign/keep a native id" and "retire ids that left the
        # FOV" without recomputing the transform per lookup.
        in_fov: dict[str, tuple[float, float, float, float, float, float]] = {}
        for visible in targets:
            # FOV membership is judged in the sensor frame (forward/left), not on
            # the TI-native axes, because the FOV is a physical antenna property of
            # the sensor, independent of how its firmware happens to report axes.
            sensor_point = self._transform.to_sensor(visible.position)
            if not self._in_fov(sensor_point.x, sensor_point.y):
                continue
            sensor_velocity = self._transform.velocity_to_sensor(visible.velocity)
            in_fov[visible.ground_truth_id] = self._native_from_sensor(
                sensor_point, sensor_velocity
            )

        # A track whose ground-truth id is no longer in the FOV has left: retire
        # its native id back to the reuse pool, exactly as a real tracker would
        # drop a target that walked out of range. This is unconditional (not
        # gated on any noise probability) because it is a real geometric fact, not
        # a simulated fault.
        for ground_truth_id in list(self._tracks):
            if ground_truth_id not in in_fov:
                self._retire(self._tracks.pop(ground_truth_id).native_id)

        records: list[bytes] = []
        for ground_truth_id, (x, y, z, vx, vy, vz) in in_fov.items():
            track = self._tracks.get(ground_truth_id)
            if track is None:
                track = _Track(native_id=self._allocate_native_id())
                self._tracks[ground_truth_id] = track
            elif rng.random() < noise.native_id_switch_probability:
                # Deliberate mid-track id churn: the person kept walking, but the
                # firmware's tracker lost and re-acquired them under a new id.
                self._retire(track.native_id)
                track.native_id = self._allocate_native_id()

            if rng.random() < noise.target_dropout_probability:
                # Transient miss: skip this frame's record, but keep the track (and
                # its native id) alive for next tick — see TiRadarNoiseConfig.
                continue

            records.append(
                encode_target_record(
                    native_track_id=track.native_id,
                    x=x + float(rng.normal(0.0, noise.position_sigma_m)),
                    y=y + float(rng.normal(0.0, noise.position_sigma_m)),
                    z=z + float(rng.normal(0.0, noise.position_sigma_m)),
                    vx=vx + float(rng.normal(0.0, noise.velocity_sigma_m_s)),
                    vy=vy + float(rng.normal(0.0, noise.velocity_sigma_m_s)),
                    vz=vz + float(rng.normal(0.0, noise.velocity_sigma_m_s)),
                )
            )

        if rng.random() < noise.false_target_rate:
            fx, fy, fz = self._false_target_native_xyz()
            records.append(
                encode_target_record(
                    native_track_id=self._allocate_native_id(),
                    x=fx,
                    y=fy,
                    z=fz,
                    vx=0.0,
                    vy=0.0,
                    vz=0.0,
                    confidence=0.3,
                )
            )

        frame_number = self._next_frame_number()

        if rng.random() < noise.missed_frame_probability:
            # The firmware "produced" this frame (the counter and all track/id
            # bookkeeping above already advanced) but it never reached the wire —
            # a dropped UART packet, not a paused tracker.
            return b""

        tlv = encode_target_list_tlv(records)
        time_cpu_cycles = int(sim_time.timestamp() * 1000) & _MAX_UINT32
        return encode_frame(
            frame_number=frame_number,
            tlvs=[tlv],
            time_cpu_cycles=time_cpu_cycles,
            num_detected_objects=len(records),
            subframe_number=0,
            profile=self._config.profile,
        )
