"""Indexed, query-friendly view over a :class:`~radiowave.contracts.store.Store`."""

from __future__ import annotations

from radiowave.contracts.geometry import WorldCoordinate
from radiowave.contracts.store import (
    EPC,
    Boundary,
    BoundaryKind,
    Fixture,
    Item,
    Product,
    Sensor,
    Store,
    Zone,
    ZoneKind,
)
from radiowave.digital_twin.geometry import RigidTransform


class StoreRegistry:
    """Read-only lookups and spatial queries over the digital twin."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._zones = {z.zone_id: z for z in store.zones}
        self._fixtures = {f.fixture_id: f for f in store.fixtures}
        self._sensors = {s.sensor_id: s for s in store.sensors}
        self._boundaries = {b.boundary_id: b for b in store.boundaries}
        self._products = {p.gtin: p for p in store.products}
        self._items = {i.epc: i for i in store.items}
        self._transforms = {
            s.sensor_id: RigidTransform.from_pose(s.sensor_id, s.pose) for s in store.sensors
        }

    # --- identity lookups -------------------------------------------------
    def zone(self, zone_id: str) -> Zone:
        return self._zones[zone_id]

    def zone_or_none(self, zone_id: str | None) -> Zone | None:
        return self._zones.get(zone_id) if zone_id else None

    def fixture(self, fixture_id: str) -> Fixture:
        return self._fixtures[fixture_id]

    def sensor(self, sensor_id: str) -> Sensor:
        return self._sensors[sensor_id]

    def product(self, gtin: str) -> Product:
        return self._products[gtin]

    def item(self, epc: EPC) -> Item | None:
        return self._items.get(epc)

    @property
    def items(self) -> list[Item]:
        return list(self._items.values())

    @property
    def sensors(self) -> list[Sensor]:
        return list(self._sensors.values())

    @property
    def fixtures(self) -> list[Fixture]:
        return list(self._fixtures.values())

    def transform(self, sensor_id: str) -> RigidTransform:
        """Sensor-native frame -> store frame transform for ``sensor_id``."""
        return self._transforms[sensor_id]

    def home_fixture_id(self, epc: EPC) -> str | None:
        item = self._items.get(epc)
        if item is None:
            return None
        if item.home_fixture_id:
            return item.home_fixture_id
        product = self._products.get(item.gtin)
        return product.home_fixture_id if product else None

    def home_fixture(self, epc: EPC) -> Fixture | None:
        fixture_id = self.home_fixture_id(epc)
        return self._fixtures.get(fixture_id) if fixture_id else None

    # --- spatial queries --------------------------------------------------
    def zone_at(self, point: WorldCoordinate, kinds: set[ZoneKind] | None = None) -> Zone | None:
        """Smallest zone containing the point (fixture zones before floor zones)."""
        matches = [
            z
            for z in self._zones.values()
            if z.bounds.contains(point) and (kinds is None or z.kind in kinds)
        ]
        if not matches:
            return None
        return min(matches, key=_zone_area)

    def fixture_at(self, point: WorldCoordinate, margin: float = 0.0) -> Fixture | None:
        for fixture in self._fixtures.values():
            if fixture.bounds.contains(point, margin=margin):
                return fixture
        return None

    def nearest_fixture(self, point: WorldCoordinate) -> tuple[Fixture, float] | None:
        if not self._fixtures:
            return None
        best = min(self._fixtures.values(), key=lambda f: f.bounds.distance_to(point))
        return best, best.bounds.distance_to(point)

    def boundary_at(self, point: WorldCoordinate, kind: BoundaryKind) -> Boundary | None:
        for boundary in self._boundaries.values():
            if boundary.kind == kind and boundary.bounds.contains(point):
                return boundary
        return None

    def in_exit_boundary(self, point: WorldCoordinate) -> bool:
        return self.boundary_at(point, BoundaryKind.EXIT) is not None

    def in_entry_boundary(self, point: WorldCoordinate) -> bool:
        return self.boundary_at(point, BoundaryKind.ENTRY) is not None


def _zone_area(zone: Zone) -> float:
    return (zone.bounds.max_x - zone.bounds.min_x) * (zone.bounds.max_y - zone.bounds.min_y)
