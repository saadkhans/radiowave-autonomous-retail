"""Synthetic lab store used by all Foundation v0 scenarios.

Layout (meters, store frame, origin bottom-left, x east, y north)::

    y=8 +----------------------------------------------+
        |        [F1 zone]           [F2 zone]         |
        |         F1 table            F2 table         |
    y=5 |ENTRY                                    EXIT |
    y=3 |                                              |
        |        [F3 zone]                             |
        |         F3 table                             |
    y=0 +----------------------------------------------+
        x=0                                          x=12

Sensors are deliberately mounted with non-trivial poses so every synthetic
sample passes through a real frame transform before fusion sees it.
"""

from __future__ import annotations

import math

from radiowave.contracts.geometry import WorldCoordinate
from radiowave.contracts.store import (
    EPC,
    Boundary,
    BoundaryKind,
    Box2D,
    Fixture,
    Item,
    Product,
    Sensor,
    SensorPose,
    SourceType,
    Store,
    Zone,
    ZoneKind,
)

RADAR_NORTH = "radar-north"
RADAR_SOUTH = "radar-south"
CAMERA_MAIN = "cam-main"
RFID_READ_POINTS = ("rfid-f1", "rfid-f2", "rfid-f3", "rfid-floor", "rfid-exit")

GTIN_BLACK_SHIRT_L = "06281234567890"
GTIN_RUNNING_SHOE_42 = "06281234567906"
GTIN_HEADPHONES = "06281234567913"

EPC_SHIRT_A = "3034F1A0000000000000A001"
EPC_SHIRT_B = "3034F1A0000000000000A002"
EPC_SHOE_A = "3034F1B0000000000000B001"
EPC_HEADPHONES_A = "3034F1C0000000000000C001"

FIXTURE_F1 = "F1"
FIXTURE_F2 = "F2"
FIXTURE_F3 = "F3"

F1_APPROACH = (4.0, 5.3)
F2_APPROACH = (8.0, 5.3)
F3_APPROACH = (4.0, 2.7)
ENTRY_POINT = (0.75, 4.0)
EXIT_POINT = (11.25, 4.0)


def _read_point(sensor_id: str, x: float, y: float, port: str) -> Sensor:
    return Sensor(
        sensor_id=sensor_id,
        modality=SourceType.RFID,
        pose=SensorPose(position=WorldCoordinate(x=x, y=y, z=2.6), yaw=0.0, pitch=-math.pi / 2),
        name=f"RFID read point {sensor_id}",
        vendor_metadata={"reader": "mock-reader-1", "port": port},
    )


def build_lab_store() -> Store:
    zones = [
        Zone(
            zone_id="entry",
            name="Entry",
            kind=ZoneKind.ENTRY,
            bounds=Box2D(min_x=0.0, min_y=3.0, max_x=1.5, max_y=5.0),
        ),
        Zone(
            zone_id="exit",
            name="Exit",
            kind=ZoneKind.EXIT,
            bounds=Box2D(min_x=10.5, min_y=3.0, max_x=12.0, max_y=5.0),
        ),
        Zone(
            zone_id="floor",
            name="Sales floor",
            kind=ZoneKind.SALES_FLOOR,
            bounds=Box2D(min_x=0.0, min_y=0.0, max_x=12.0, max_y=8.0),
        ),
        Zone(
            zone_id="zone-f1",
            name="Apparel wall",
            kind=ZoneKind.FIXTURE,
            bounds=Box2D(min_x=2.5, min_y=5.0, max_x=5.5, max_y=8.0),
        ),
        Zone(
            zone_id="zone-f2",
            name="Footwear",
            kind=ZoneKind.FIXTURE,
            bounds=Box2D(min_x=6.5, min_y=5.0, max_x=9.5, max_y=8.0),
        ),
        Zone(
            zone_id="zone-f3",
            name="Electronics",
            kind=ZoneKind.FIXTURE,
            bounds=Box2D(min_x=2.5, min_y=0.0, max_x=5.5, max_y=3.0),
        ),
    ]
    fixtures = [
        Fixture(
            fixture_id=FIXTURE_F1,
            zone_id="zone-f1",
            name="Apparel table",
            bounds=Box2D(min_x=3.5, min_y=6.0, max_x=4.5, max_y=7.0),
        ),
        Fixture(
            fixture_id=FIXTURE_F2,
            zone_id="zone-f2",
            name="Footwear table",
            bounds=Box2D(min_x=7.5, min_y=6.0, max_x=8.5, max_y=7.0),
        ),
        Fixture(
            fixture_id=FIXTURE_F3,
            zone_id="zone-f3",
            name="Electronics table",
            bounds=Box2D(min_x=3.5, min_y=1.0, max_x=4.5, max_y=2.0),
        ),
    ]
    boundaries = [
        Boundary(
            boundary_id="entry-gate",
            kind=BoundaryKind.ENTRY,
            bounds=Box2D(min_x=0.0, min_y=3.0, max_x=1.5, max_y=5.0),
        ),
        Boundary(
            boundary_id="exit-gate",
            kind=BoundaryKind.EXIT,
            bounds=Box2D(min_x=10.5, min_y=3.0, max_x=12.0, max_y=5.0),
        ),
    ]
    sensors = [
        Sensor(
            sensor_id=RADAR_NORTH,
            modality=SourceType.MMWAVE,
            pose=SensorPose(position=WorldCoordinate(x=6.0, y=8.0, z=2.4), yaw=-math.pi / 2),
            name="mmWave node, north wall, looking south",
            vendor_metadata={"kind": "mock-60ghz"},
        ),
        Sensor(
            sensor_id=RADAR_SOUTH,
            modality=SourceType.MMWAVE,
            pose=SensorPose(position=WorldCoordinate(x=6.0, y=0.0, z=2.4), yaw=math.pi / 2),
            name="mmWave node, south wall, looking north",
            vendor_metadata={"kind": "mock-60ghz"},
        ),
        _read_point("rfid-f1", 4.0, 6.5, "1"),
        _read_point("rfid-f2", 8.0, 6.5, "2"),
        _read_point("rfid-f3", 4.0, 1.5, "3"),
        _read_point("rfid-floor", 6.0, 4.0, "4"),
        _read_point("rfid-exit", 11.25, 4.0, "5"),
        Sensor(
            sensor_id=CAMERA_MAIN,
            modality=SourceType.VISION,
            pose=SensorPose(position=WorldCoordinate(x=6.0, y=8.0, z=3.0), yaw=-math.pi / 2),
            name="Ground-truth camera (semantic evidence only)",
            vendor_metadata={"kind": "mock-camera"},
        ),
    ]
    products = [
        Product(
            gtin=GTIN_BLACK_SHIRT_L,
            sku="SHIRT-BLK-L",
            name="Black shirt, large",
            home_fixture_id=FIXTURE_F1,
        ),
        Product(
            gtin=GTIN_RUNNING_SHOE_42,
            sku="SHOE-RUN-42",
            name="Running shoe 42",
            home_fixture_id=FIXTURE_F2,
        ),
        Product(
            gtin=GTIN_HEADPHONES,
            sku="HP-ANC-1",
            name="Noise-cancelling headphones",
            home_fixture_id=FIXTURE_F3,
        ),
    ]
    items = [
        Item(epc=EPC(value=EPC_SHIRT_A), gtin=GTIN_BLACK_SHIRT_L),
        Item(epc=EPC(value=EPC_SHIRT_B), gtin=GTIN_BLACK_SHIRT_L),
        Item(epc=EPC(value=EPC_SHOE_A), gtin=GTIN_RUNNING_SHOE_42),
        Item(epc=EPC(value=EPC_HEADPHONES_A), gtin=GTIN_HEADPHONES),
    ]
    return Store(
        store_id="lab-01",
        name="Foundation v0 synthetic lab store",
        floor_bounds=Box2D(min_x=0.0, min_y=0.0, max_x=12.0, max_y=8.0),
        zones=zones,
        fixtures=fixtures,
        boundaries=boundaries,
        sensors=sensors,
        products=products,
        items=items,
    )
