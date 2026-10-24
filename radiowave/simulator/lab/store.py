"""Phase-4 virtual lab store: the reference mini-store for the deterministic world sim.

Layout (meters, store frame, origin bottom-left, x east, y north)::

    y=6 +----------------------------------------------+
        |  [zone-rack-a]        [zone-rack-b]           |
        |   RACK-A                RACK-B                |
    y=4 |                                                |
        |ENTRY                                     EXIT |
    y=2 |                                                |
        |          [zone-rack-c]                        |
        |           RACK-C                              |
    y=0 +----------------------------------------------+
        x=0                                            x=8

This store is deliberately separate from ``radiowave/simulator/stores.py``
(``build_lab_store`` / the "Foundation v0" 12x8m store used by the fourteen
existing scenarios). Phase 4 gets its own twin so the existing scenario suite
never sees a layout change. Do not merge these two builders.

The single most important property of this store is that physical identity
(EPC) and commercial identity (GTIN/SKU) are visibly different: several
distinct EPCs share one GTIN, so a reviewer can see at a glance that "four
units of the same t-shirt" are four different physical objects.
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

# --- rack (fixture) ids --------------------------------------------------
RACK_A = "RACK-A"
RACK_B = "RACK-B"
RACK_C = "RACK-C"

# --- zone ids --------------------------------------------------------------
ZONE_ENTRY = "lab-entry"
ZONE_EXIT = "lab-exit"
ZONE_FLOOR = "lab-floor"
ZONE_RACK_A = "zone-rack-a"
ZONE_RACK_B = "zone-rack-b"
ZONE_RACK_C = "zone-rack-c"

# --- boundary ids ------------------------------------------------------------
BOUNDARY_ENTRY = "lab-entry-gate"
BOUNDARY_EXIT = "lab-exit-gate"

# --- sensor ids --------------------------------------------------------------
MMWAVE_SENSOR_ID = "lab-mmwave-01"
RFID_RACK_A_ID = "lab-rfid-rack-a"
RFID_RACK_B_ID = "lab-rfid-rack-b"
RFID_RACK_C_ID = "lab-rfid-rack-c"
RFID_EXIT_ID = "lab-rfid-exit"
RFID_READ_POINT_IDS = (RFID_RACK_A_ID, RFID_RACK_B_ID, RFID_RACK_C_ID, RFID_EXIT_ID)

# --- named waypoints, mirroring stores.py's *_APPROACH constants -----------
# Reused by actors.py for deterministic path planning and by tests.
ENTRY_POINT = (0.7, 3.0)
EXIT_POINT = (7.3, 3.0)
RACK_A_APPROACH = (2.4, 4.2)
RACK_B_APPROACH = (5.6, 4.2)
RACK_C_APPROACH = (4.0, 1.8)

# --- GTIN/SKU catalog --------------------------------------------------------
# Three products per rack. Each product is a *commercial type*; several
# distinct physical EPCs below are instances of the same product, which is
# the whole point of separating Item (EPC) from Product (GTIN/SKU).
GTIN_TSHIRT_BLACK_M = "06284000000010"
GTIN_TSHIRT_WHITE_M = "06284000000011"
GTIN_JACKET_DENIM_32 = "06284000000012"
GTIN_SNEAKER_RUN_42 = "06284000000020"
GTIN_SNEAKER_RUN_44 = "06284000000021"
GTIN_SANDAL_40 = "06284000000022"
GTIN_HEADPHONES_ANC = "06284000000030"
GTIN_SPEAKER_MINI = "06284000000031"
GTIN_CHARGER_USBC = "06284000000032"

SKU_TSHIRT_BLACK_M = "TSHIRT-BLACK-M"
SKU_TSHIRT_WHITE_M = "TSHIRT-WHITE-M"
SKU_JACKET_DENIM_32 = "JACKET-DENIM-32"
SKU_SNEAKER_RUN_42 = "SNEAKER-RUN-42"
SKU_SNEAKER_RUN_44 = "SNEAKER-RUN-44"
SKU_SANDAL_40 = "SANDAL-40"
SKU_HEADPHONES_ANC = "HEADPHONES-ANC"
SKU_SPEAKER_MINI = "SPEAKER-MINI"
SKU_CHARGER_USBC = "CHARGER-USBC"


def _epc(rack_letter: str, index: int) -> str:
    """Deterministic 24-hex-char EPC: ``3034F1`` + rack letter + zero pad + 4-digit index."""
    return f"3034F1{rack_letter}{0:013d}{index:04d}"


# 10 uniquely identified physical items per rack (30 total). Within each rack,
# the first 4 EPCs share one GTIN, the next 3 share a second GTIN, and the
# final 3 share a third GTIN -- so GTIN sharing is visible on every rack, not
# just once in the whole store.
EPCS_RACK_A = tuple(_epc("A", i) for i in range(1, 11))
EPCS_RACK_B = tuple(_epc("B", i) for i in range(1, 11))
EPCS_RACK_C = tuple(_epc("C", i) for i in range(1, 11))
ALL_EPCS = EPCS_RACK_A + EPCS_RACK_B + EPCS_RACK_C


def _read_point(sensor_id: str, x: float, y: float, port: str) -> Sensor:
    """RFID logical antenna zone, mounted straight down like stores.py's read points."""
    return Sensor(
        sensor_id=sensor_id,
        modality=SourceType.RFID,
        pose=SensorPose(position=WorldCoordinate(x=x, y=y, z=2.6), yaw=0.0, pitch=-math.pi / 2),
        name=f"RFID read point {sensor_id}",
        vendor_metadata={"reader": "mock-reader-lab", "port": port},
    )


def build_virtual_lab_store() -> Store:
    """Build the Phase-4 reference mini-store (~8m x 6m). Deterministic, no randomness."""
    zones = [
        Zone(
            zone_id=ZONE_ENTRY,
            name="Entry",
            kind=ZoneKind.ENTRY,
            bounds=Box2D(min_x=0.0, min_y=2.2, max_x=1.4, max_y=3.8),
        ),
        Zone(
            zone_id=ZONE_EXIT,
            name="Exit",
            kind=ZoneKind.EXIT,
            bounds=Box2D(min_x=6.6, min_y=2.2, max_x=8.0, max_y=3.8),
        ),
        Zone(
            zone_id=ZONE_FLOOR,
            name="Sales floor",
            kind=ZoneKind.SALES_FLOOR,
            bounds=Box2D(min_x=0.0, min_y=0.0, max_x=8.0, max_y=6.0),
        ),
        Zone(
            zone_id=ZONE_RACK_A,
            name="Rack A zone",
            kind=ZoneKind.FIXTURE,
            bounds=Box2D(min_x=1.2, min_y=4.0, max_x=3.6, max_y=6.0),
        ),
        Zone(
            zone_id=ZONE_RACK_B,
            name="Rack B zone",
            kind=ZoneKind.FIXTURE,
            bounds=Box2D(min_x=4.4, min_y=4.0, max_x=6.8, max_y=6.0),
        ),
        Zone(
            zone_id=ZONE_RACK_C,
            name="Rack C zone",
            kind=ZoneKind.FIXTURE,
            bounds=Box2D(min_x=2.8, min_y=0.0, max_x=5.2, max_y=2.0),
        ),
    ]
    fixtures = [
        Fixture(
            fixture_id=RACK_A,
            zone_id=ZONE_RACK_A,
            name="Rack A",
            bounds=Box2D(min_x=1.8, min_y=4.6, max_x=3.0, max_y=5.4),
        ),
        Fixture(
            fixture_id=RACK_B,
            zone_id=ZONE_RACK_B,
            name="Rack B",
            bounds=Box2D(min_x=5.0, min_y=4.6, max_x=6.2, max_y=5.4),
        ),
        Fixture(
            fixture_id=RACK_C,
            zone_id=ZONE_RACK_C,
            name="Rack C",
            bounds=Box2D(min_x=3.4, min_y=0.6, max_x=4.6, max_y=1.4),
        ),
    ]
    boundaries = [
        Boundary(
            boundary_id=BOUNDARY_ENTRY,
            kind=BoundaryKind.ENTRY,
            bounds=Box2D(min_x=0.0, min_y=2.2, max_x=1.4, max_y=3.8),
        ),
        Boundary(
            boundary_id=BOUNDARY_EXIT,
            kind=BoundaryKind.EXIT,
            bounds=Box2D(min_x=6.6, min_y=2.2, max_x=8.0, max_y=3.8),
        ),
    ]
    # Single mmWave radar with a non-trivial (non-axis-aligned) pose, mounted near
    # the entry-side corner and aimed at the store center so every rack and both
    # the entry and exit boundaries fall comfortably inside its ~15m range.
    mmwave_position = WorldCoordinate(x=0.4, y=5.7, z=2.5)
    store_center = (4.0, 3.0)
    mmwave_yaw = math.atan2(
        store_center[1] - mmwave_position.y, store_center[0] - mmwave_position.x
    )
    sensors = [
        Sensor(
            sensor_id=MMWAVE_SENSOR_ID,
            modality=SourceType.MMWAVE,
            pose=SensorPose(
                position=mmwave_position,
                yaw=mmwave_yaw,
                pitch=-math.radians(15),
            ),
            name="mmWave node, NW corner, aimed at store center",
            vendor_metadata={"kind": "mock-60ghz"},
        ),
        _read_point(RFID_RACK_A_ID, RACK_A_APPROACH[0], 5.0, "1"),
        _read_point(RFID_RACK_B_ID, RACK_B_APPROACH[0], 5.0, "2"),
        _read_point(RFID_RACK_C_ID, RACK_C_APPROACH[0], 1.0, "3"),
        _read_point(RFID_EXIT_ID, EXIT_POINT[0], EXIT_POINT[1], "4"),
    ]
    products = [
        Product(
            gtin=GTIN_TSHIRT_BLACK_M,
            sku=SKU_TSHIRT_BLACK_M,
            name="T-shirt, black, M",
            home_fixture_id=RACK_A,
        ),
        Product(
            gtin=GTIN_TSHIRT_WHITE_M,
            sku=SKU_TSHIRT_WHITE_M,
            name="T-shirt, white, M",
            home_fixture_id=RACK_A,
        ),
        Product(
            gtin=GTIN_JACKET_DENIM_32,
            sku=SKU_JACKET_DENIM_32,
            name="Denim jacket, 32",
            home_fixture_id=RACK_A,
        ),
        Product(
            gtin=GTIN_SNEAKER_RUN_42,
            sku=SKU_SNEAKER_RUN_42,
            name="Running sneaker, 42",
            home_fixture_id=RACK_B,
        ),
        Product(
            gtin=GTIN_SNEAKER_RUN_44,
            sku=SKU_SNEAKER_RUN_44,
            name="Running sneaker, 44",
            home_fixture_id=RACK_B,
        ),
        Product(
            gtin=GTIN_SANDAL_40,
            sku=SKU_SANDAL_40,
            name="Sandal, 40",
            home_fixture_id=RACK_B,
        ),
        Product(
            gtin=GTIN_HEADPHONES_ANC,
            sku=SKU_HEADPHONES_ANC,
            name="Noise-cancelling headphones",
            home_fixture_id=RACK_C,
        ),
        Product(
            gtin=GTIN_SPEAKER_MINI,
            sku=SKU_SPEAKER_MINI,
            name="Mini bluetooth speaker",
            home_fixture_id=RACK_C,
        ),
        Product(
            gtin=GTIN_CHARGER_USBC,
            sku=SKU_CHARGER_USBC,
            name="USB-C charger",
            home_fixture_id=RACK_C,
        ),
    ]
    # Per rack: EPCs 1-4 -> first GTIN, 5-7 -> second GTIN, 8-10 -> third GTIN.
    # This is the visible SKU/EPC split: e.g. EPCS_RACK_A[0..3] are four distinct
    # physical t-shirts that are all GTIN_TSHIRT_BLACK_M.
    rack_gtin_plan = {
        RACK_A: (EPCS_RACK_A, (GTIN_TSHIRT_BLACK_M, GTIN_TSHIRT_WHITE_M, GTIN_JACKET_DENIM_32)),
        RACK_B: (EPCS_RACK_B, (GTIN_SNEAKER_RUN_42, GTIN_SNEAKER_RUN_44, GTIN_SANDAL_40)),
        RACK_C: (EPCS_RACK_C, (GTIN_HEADPHONES_ANC, GTIN_SPEAKER_MINI, GTIN_CHARGER_USBC)),
    }
    items: list[Item] = []
    for fixture_id, (epcs, gtins) in rack_gtin_plan.items():
        # 4 units of the first GTIN, 3 of the second, 3 of the third: 10 total.
        gtin_for_index = [gtins[0]] * 4 + [gtins[1]] * 3 + [gtins[2]] * 3
        for epc_value, gtin in zip(epcs, gtin_for_index, strict=True):
            items.append(Item(epc=EPC(value=epc_value), gtin=gtin, home_fixture_id=fixture_id))

    return Store(
        store_id="virtual-lab-01",
        name="Phase 4 virtual autonomous-retail lab store",
        floor_bounds=Box2D(min_x=0.0, min_y=0.0, max_x=8.0, max_y=6.0),
        zones=zones,
        fixtures=fixtures,
        boundaries=boundaries,
        sensors=sensors,
        products=products,
        items=items,
    )
