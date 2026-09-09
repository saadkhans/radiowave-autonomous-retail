"""Digital store twin contracts: coordinate frame, zones, fixtures, sensors, products, items.

Identity rules
--------------
* :class:`Product` is a *commercial type* identified by GTIN (and optionally an
  internal SKU). Many physical units share one product.
* :class:`Item` is a *single physical unit* identified by its :class:`EPC`.
  Two items of the same GTIN are still two different items.
* :class:`Sensor` ids are ours. Whatever the vendor calls the device lives in
  ``vendor_metadata`` and is never used as a business identifier.
"""

from __future__ import annotations

import math
import re
from enum import StrEnum

from pydantic import Field, field_validator, model_validator

from radiowave.contracts._base import ContractModel, FrozenModel
from radiowave.contracts.geometry import STORE_FRAME_ID, WorldCoordinate

_EPC_HEX = re.compile(r"^[0-9A-F]{8,64}$")
_GTIN_DIGITS = re.compile(r"^\d{8}$|^\d{12,14}$")


class EPC(FrozenModel):
    """Electronic Product Code of exactly one physical merchandise unit.

    Stored as uppercase hexadecimal (EPC binary encoding, 32-256 bits).
    """

    value: str

    @field_validator("value")
    @classmethod
    def _normalize(cls, value: str) -> str:
        cleaned = value.strip().upper()
        if not _EPC_HEX.match(cleaned):
            msg = f"EPC must be 8-64 uppercase hex characters, got {value!r}"
            raise ValueError(msg)
        return cleaned

    def __str__(self) -> str:
        return self.value


class Product(FrozenModel):
    """Commercial product type (SKU / GTIN). Never a physical object."""

    gtin: str = Field(description="GTIN-8/12/13/14 digits")
    name: str
    sku: str | None = None
    home_fixture_id: str | None = Field(
        default=None, description="Fixture where units of this product are normally displayed"
    )

    @field_validator("gtin")
    @classmethod
    def _check_gtin(cls, value: str) -> str:
        if not _GTIN_DIGITS.match(value):
            msg = f"GTIN must be 8, 12, 13 or 14 digits, got {value!r}"
            raise ValueError(msg)
        return value


class Item(FrozenModel):
    """One physical, uniquely tagged merchandise unit."""

    epc: EPC
    gtin: str
    home_fixture_id: str | None = Field(
        default=None,
        description="Fixture this unit was placed on (overrides the product home fixture)",
    )


class CoordinateFrame(FrozenModel):
    """The shared store frame. Right-handed, meters, z up, origin documented in text."""

    frame_id: str = STORE_FRAME_ID
    description: str = "Store floor plan frame: origin at a documented corner, x east, y north"
    units: str = "m"


class Box2D(FrozenModel):
    """Axis-aligned rectangle on the floor plane, in store-frame meters."""

    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @model_validator(mode="after")
    def _check_extent(self) -> Box2D:
        if self.max_x <= self.min_x or self.max_y <= self.min_y:
            msg = "Box2D must have positive width and height"
            raise ValueError(msg)
        return self

    def contains(self, point: WorldCoordinate, margin: float = 0.0) -> bool:
        return (
            self.min_x - margin <= point.x <= self.max_x + margin
            and self.min_y - margin <= point.y <= self.max_y + margin
        )

    @property
    def center(self) -> WorldCoordinate:
        return WorldCoordinate(x=(self.min_x + self.max_x) / 2, y=(self.min_y + self.max_y) / 2)

    def distance_to(self, point: WorldCoordinate) -> float:
        """Horizontal distance from point to the nearest edge (0 if inside)."""
        dx = max(self.min_x - point.x, 0.0, point.x - self.max_x)
        dy = max(self.min_y - point.y, 0.0, point.y - self.max_y)
        return math.hypot(dx, dy)


class ZoneKind(StrEnum):
    SALES_FLOOR = "SALES_FLOOR"
    FIXTURE = "FIXTURE"
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    FITTING_ROOM = "FITTING_ROOM"
    BACK_OF_HOUSE = "BACK_OF_HOUSE"


class Zone(FrozenModel):
    zone_id: str = Field(min_length=1)
    name: str
    kind: ZoneKind
    bounds: Box2D


class Fixture(FrozenModel):
    """A display fixture (rack, table, shelf) where products have a home."""

    fixture_id: str = Field(min_length=1)
    zone_id: str
    name: str
    bounds: Box2D

    @property
    def center(self) -> WorldCoordinate:
        return self.bounds.center


class BoundaryKind(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class Boundary(FrozenModel):
    """Entry or exit boundary; crossing into the box counts as crossing the boundary."""

    boundary_id: str = Field(min_length=1)
    kind: BoundaryKind
    bounds: Box2D


class SensorPose(FrozenModel):
    """Rigid pose of a sensor in the store frame.

    ``position`` is the sensor origin in store coordinates (meters).
    Orientation is intrinsic yaw (about z), pitch (about y), roll (about x) in radians.
    """

    position: WorldCoordinate
    yaw: float = 0.0
    pitch: float = 0.0
    roll: float = 0.0


class SourceType(StrEnum):
    """Sensing modality of an observation or sensor."""

    MMWAVE = "MMWAVE"
    RFID = "RFID"
    VISION = "VISION"


class Sensor(FrozenModel):
    """A physical sensing device. ``sensor_id`` is ours; vendor naming stays in metadata."""

    sensor_id: str = Field(min_length=1)
    modality: SourceType
    pose: SensorPose
    name: str | None = None
    vendor_metadata: dict[str, str] = Field(default_factory=dict)


class Store(ContractModel):
    """Minimal, extensible digital twin of one store."""

    store_id: str = Field(min_length=1)
    name: str
    frame: CoordinateFrame = Field(default_factory=CoordinateFrame)
    floor_bounds: Box2D
    zones: list[Zone] = Field(default_factory=list)
    fixtures: list[Fixture] = Field(default_factory=list)
    boundaries: list[Boundary] = Field(default_factory=list)
    sensors: list[Sensor] = Field(default_factory=list)
    products: list[Product] = Field(default_factory=list)
    items: list[Item] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_references(self) -> Store:
        zone_ids = {z.zone_id for z in self.zones}
        fixture_ids = {f.fixture_id for f in self.fixtures}
        product_gtins = {p.gtin for p in self.products}
        _require_unique("zone_id", [z.zone_id for z in self.zones])
        _require_unique("fixture_id", [f.fixture_id for f in self.fixtures])
        _require_unique("sensor_id", [s.sensor_id for s in self.sensors])
        _require_unique("boundary_id", [b.boundary_id for b in self.boundaries])
        _require_unique("gtin", [p.gtin for p in self.products])
        _require_unique("epc", [i.epc.value for i in self.items])
        for fixture in self.fixtures:
            if fixture.zone_id not in zone_ids:
                msg = f"fixture {fixture.fixture_id} references unknown zone {fixture.zone_id}"
                raise ValueError(msg)
        for product in self.products:
            if product.home_fixture_id and product.home_fixture_id not in fixture_ids:
                msg = f"product {product.gtin} references unknown fixture {product.home_fixture_id}"
                raise ValueError(msg)
        for item in self.items:
            if item.gtin not in product_gtins:
                msg = f"item {item.epc} references unknown product {item.gtin}"
                raise ValueError(msg)
            if item.home_fixture_id and item.home_fixture_id not in fixture_ids:
                msg = f"item {item.epc} references unknown fixture {item.home_fixture_id}"
                raise ValueError(msg)
        return self


def _require_unique(label: str, values: list[str]) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            msg = f"duplicate {label}: {value}"
            raise ValueError(msg)
        seen.add(value)
