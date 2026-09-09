"""Rigid transforms between sensor-native frames and the store frame.

A :class:`RigidTransform` maps points from a *source* frame to a *target* frame:
``p_target = R @ p_source + t``. Sensor poses are expressed as the transform
from the sensor frame into the store frame, so ``world = pose.apply(sensor_point)``
and ``sensor_point = pose.inverse().apply(world)``.

Rotation convention: intrinsic Z-Y-X (yaw about z, then pitch about y, then roll
about x), right-handed, radians. This is enough for Foundation v0; a full
calibration model (lens/antenna intrinsics, time offsets) can extend this class
without changing its callers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.typing import NDArray

from radiowave.contracts.geometry import (
    STORE_FRAME_ID,
    SensorCoordinate,
    Vector3D,
    Velocity,
    WorldCoordinate,
)
from radiowave.contracts.store import SensorPose

FloatArray = NDArray[np.float64]


def rotation_matrix(yaw: float, pitch: float, roll: float) -> FloatArray:
    """Rotation matrix for intrinsic yaw (z), pitch (y), roll (x)."""
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cr, sr = math.cos(roll), math.sin(roll)
    rz = np.array([[cy, -sy, 0.0], [sy, cy, 0.0], [0.0, 0.0, 1.0]])
    ry = np.array([[cp, 0.0, sp], [0.0, 1.0, 0.0], [-sp, 0.0, cp]])
    rx = np.array([[1.0, 0.0, 0.0], [0.0, cr, -sr], [0.0, sr, cr]])
    result: FloatArray = rz @ ry @ rx
    return result


@dataclass(frozen=True)
class RigidTransform:
    """Maps ``source_frame`` coordinates into ``target_frame`` coordinates."""

    source_frame: str
    target_frame: str
    rotation: FloatArray
    translation: FloatArray

    @classmethod
    def identity(cls, frame: str = STORE_FRAME_ID) -> RigidTransform:
        return cls(frame, frame, np.eye(3), np.zeros(3))

    @classmethod
    def from_pose(cls, sensor_id: str, pose: SensorPose) -> RigidTransform:
        """Transform from a sensor's native frame into the store frame."""
        rotation = rotation_matrix(pose.yaw, pose.pitch, pose.roll)
        translation = np.array([pose.position.x, pose.position.y, pose.position.z])
        return cls(sensor_id, pose.position.frame_id, rotation, translation)

    def inverse(self) -> RigidTransform:
        r_inv = self.rotation.T
        return RigidTransform(
            self.target_frame, self.source_frame, r_inv, -r_inv @ self.translation
        )

    def compose(self, inner: RigidTransform) -> RigidTransform:
        """Return ``self ∘ inner``: apply ``inner`` first, then ``self``."""
        if inner.target_frame != self.source_frame:
            msg = f"cannot compose: {inner.target_frame} != {self.source_frame}"
            raise ValueError(msg)
        return RigidTransform(
            inner.source_frame,
            self.target_frame,
            self.rotation @ inner.rotation,
            self.rotation @ inner.translation + self.translation,
        )

    def apply_xyz(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        out = self.rotation @ np.array([x, y, z]) + self.translation
        return (float(out[0]), float(out[1]), float(out[2]))

    def rotate_xyz(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        """Rotate a direction/velocity vector without translating it."""
        out = self.rotation @ np.array([x, y, z])
        return (float(out[0]), float(out[1]), float(out[2]))

    def to_world(self, point: SensorCoordinate) -> WorldCoordinate:
        if point.frame_id != self.source_frame:
            msg = f"point is in frame {point.frame_id!r}, transform expects {self.source_frame!r}"
            raise ValueError(msg)
        x, y, z = self.apply_xyz(point.x, point.y, point.z)
        return WorldCoordinate(x=x, y=y, z=z, frame_id=self.target_frame)

    def to_sensor(self, point: WorldCoordinate) -> SensorCoordinate:
        if point.frame_id != self.target_frame:
            msg = f"point is in frame {point.frame_id!r}, transform expects {self.target_frame!r}"
            raise ValueError(msg)
        x, y, z = self.inverse().apply_xyz(point.x, point.y, point.z)
        return SensorCoordinate(x=x, y=y, z=z, frame_id=self.source_frame)

    def velocity_to_world(self, velocity: Velocity) -> Velocity:
        vx, vy, vz = self.rotate_xyz(velocity.vx, velocity.vy, velocity.vz)
        return Velocity(vx=vx, vy=vy, vz=vz)

    def velocity_to_sensor(self, velocity: Velocity) -> Velocity:
        vx, vy, vz = self.inverse().rotate_xyz(velocity.vx, velocity.vy, velocity.vz)
        return Velocity(vx=vx, vy=vy, vz=vz)

    def vector_to_world(self, vector: Vector3D) -> Vector3D:
        x, y, z = self.rotate_xyz(vector.x, vector.y, vector.z)
        return Vector3D(x=x, y=y, z=z)
