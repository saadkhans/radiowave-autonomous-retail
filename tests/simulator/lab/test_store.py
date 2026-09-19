"""Contract-level checks on the Phase-4 virtual lab store twin."""

from __future__ import annotations

from collections import Counter

from radiowave.contracts.store import SourceType
from radiowave.simulator.lab.store import (
    ALL_EPCS,
    EPCS_RACK_A,
    EPCS_RACK_B,
    EPCS_RACK_C,
    MMWAVE_SENSOR_ID,
    RACK_A,
    RACK_B,
    RACK_C,
    RFID_READ_POINT_IDS,
    build_virtual_lab_store,
)
from radiowave.simulator.stores import build_lab_store


def test_store_passes_contract_validators() -> None:
    # Store's own model_validator raises on any invalid reference; just building
    # it successfully is the assertion.
    store = build_virtual_lab_store()
    assert store.store_id == "virtual-lab-01"


def test_exactly_thirty_items_ten_per_rack() -> None:
    store = build_virtual_lab_store()
    assert len(store.items) == 30
    counts = Counter(item.home_fixture_id for item in store.items)
    assert counts == {RACK_A: 10, RACK_B: 10, RACK_C: 10}


def test_all_epcs_unique() -> None:
    store = build_virtual_lab_store()
    epcs = [item.epc.value for item in store.items]
    assert len(epcs) == len(set(epcs))
    assert set(epcs) == set(ALL_EPCS)


def test_at_least_one_gtin_shared_by_multiple_epcs() -> None:
    store = build_virtual_lab_store()
    gtin_counts = Counter(item.gtin for item in store.items)
    shared = {gtin: count for gtin, count in gtin_counts.items() if count > 1}
    assert shared, "expected at least one GTIN shared by multiple physical EPCs"
    # Every product on every rack is shared by design: physical identity (EPC)
    # is deliberately more granular than commercial identity (GTIN/SKU).
    assert len(shared) == len(gtin_counts)


def test_epc_and_gtin_are_visibly_different_identities() -> None:
    """The reviewer-visible property: many EPCs, few GTINs, and no item's EPC equals a GTIN."""
    store = build_virtual_lab_store()
    gtins = {p.gtin for p in store.products}
    epcs = {i.epc.value for i in store.items}
    assert len(epcs) > len(gtins)
    assert epcs.isdisjoint(gtins)


def test_rack_epc_lists_match_store_items() -> None:
    store = build_virtual_lab_store()
    by_fixture: dict[str, set[str]] = {}
    for item in store.items:
        by_fixture.setdefault(item.home_fixture_id, set()).add(item.epc.value)
    assert by_fixture[RACK_A] == set(EPCS_RACK_A)
    assert by_fixture[RACK_B] == set(EPCS_RACK_B)
    assert by_fixture[RACK_C] == set(EPCS_RACK_C)


def test_has_one_mmwave_sensor_with_non_trivial_pose() -> None:
    store = build_virtual_lab_store()
    radars = [s for s in store.sensors if s.modality == SourceType.MMWAVE]
    assert len(radars) == 1
    radar = radars[0]
    assert radar.sensor_id == MMWAVE_SENSOR_ID
    assert radar.pose.yaw != 0.0
    assert 2.4 <= radar.pose.position.z <= 2.6


def test_all_racks_and_boundaries_within_radar_range() -> None:
    store = build_virtual_lab_store()
    radar = next(s for s in store.sensors if s.modality == SourceType.MMWAVE)
    max_range_m = 15.0
    points = [f.center for f in store.fixtures] + [b.bounds.center for b in store.boundaries]
    for point in points:
        distance = radar.pose.position.horizontal_distance_to(point)
        assert distance <= max_range_m


def test_rfid_read_points_cover_every_rack_plus_exit() -> None:
    store = build_virtual_lab_store()
    rfid_sensors = [s for s in store.sensors if s.modality == SourceType.RFID]
    assert {s.sensor_id for s in rfid_sensors} == set(RFID_READ_POINT_IDS)
    assert len(rfid_sensors) >= len(store.fixtures) + 1
    # Vendor labels live only in metadata, never as an identity.
    for sensor in rfid_sensors:
        assert "reader" in sensor.vendor_metadata
        assert sensor.sensor_id not in sensor.vendor_metadata.values()


def test_does_not_touch_existing_foundation_v0_store() -> None:
    """Phase 4 has its own twin; the 14 existing scenarios must be unaffected."""
    lab_store = build_lab_store()
    virtual_store = build_virtual_lab_store()
    assert lab_store.store_id != virtual_store.store_id
    assert lab_store.floor_bounds != virtual_store.floor_bounds
