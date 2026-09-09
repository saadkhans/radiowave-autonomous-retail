"""Geometric primitives. All distances are meters, all speeds are meters/second.

Two coordinate types are deliberately distinct:

* :class:`SensorCoordinate` - a point expressed in one sensor's native frame.
* :class:`WorldCoordinate` - a point in the shared store frame.

Business logic (fusion, cart, events) only ever consumes ``WorldCoordinate``.
The digital twin converts between the two using a sensor pose.
"""

from __future__ import annotations

import math

from pydantic import Field

from radiowave.contracts._base import FrozenModel, NonNegativeFloat

STORE_FRAME_ID = "store"


class Vector3D(FrozenModel):
    """Generic 3-vector in meters (or any consistent unit)."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def __add__(self, other: Vector3D) -> Vector3D:
        return Vector3D(x=self.x + other.x, y=self.y + other.y, z=self.z + other.z)

    def __sub__(self, other: Vector3D) -> Vector3D:
        return Vector3D(x=self.x - other.x, y=self.y - other.y, z=self.z - other.z)

    def scale(self, factor: float) -> Vector3D:
        return Vector3D(x=self.x * factor, y=self.y * factor, z=self.z * factor)

    def dot(self, other: Vector3D) -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    @property
    def norm(self) -> float:
        return math.sqrt(self.dot(self))

    @property
    def horizontal_norm(self) -> float:
        return math.hypot(self.x, self.y)

    def as_tuple(self) -> tuple[float, float, float]:
        return (self.x, self.y, self.z)


class _Point(FrozenModel):
    x: float
    y: float
    z: float = 0.0

    def distance_to(self, other: _Point) -> float:
        return math.sqrt(
            (self.x - other.x) ** 2 + (self.y - other.y) ** 2 + (self.z - other.z) ** 2
        )

    def horizontal_distance_to(self, other: _Point) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)

    def as_vector(self) -> Vector3D:
        return Vector3D(x=self.x, y=self.y, z=self.z)


class WorldCoordinate(_Point):
    """Point in the shared store frame (meters)."""

    frame_id: str = STORE_FRAME_ID

    def displaced(self, delta: Vector3D) -> WorldCoordinate:
        return WorldCoordinate(
            x=self.x + delta.x, y=self.y + delta.y, z=self.z + delta.z, frame_id=self.frame_id
        )


class SensorCoordinate(_Point):
    """Point in a sensor-native frame identified by ``frame_id`` (usually the sensor id)."""

    frame_id: str = Field(min_length=1)


class Velocity(FrozenModel):
    """Velocity in meters/second, expressed in the store frame unless stated otherwise."""

    vx: float = 0.0
    vy: float = 0.0
    vz: float = 0.0

    @property
    def speed(self) -> float:
        return math.sqrt(self.vx**2 + self.vy**2 + self.vz**2)

    @property
    def horizontal_speed(self) -> float:
        return math.hypot(self.vx, self.vy)

    def as_vector(self) -> Vector3D:
        return Vector3D(x=self.vx, y=self.vy, z=self.vz)

    @classmethod
    def from_vector(cls, vector: Vector3D) -> Velocity:
        return cls(vx=vector.x, vy=vector.y, vz=vector.z)


class SpatialUncertainty(FrozenModel):
    """One-sigma positional uncertainty per axis, in meters."""

    sigma_x: NonNegativeFloat
    sigma_y: NonNegativeFloat
    sigma_z: NonNegativeFloat = 0.0

    @classmethod
    def isotropic(cls, sigma: float, sigma_z: float | None = None) -> SpatialUncertainty:
        return cls(sigma_x=sigma, sigma_y=sigma, sigma_z=sigma if sigma_z is None else sigma_z)

    @property
    def horizontal_sigma(self) -> float:
        """Root-mean-square horizontal sigma; a scalar summary for gating."""
        return math.sqrt((self.sigma_x**2 + self.sigma_y**2) / 2.0)
