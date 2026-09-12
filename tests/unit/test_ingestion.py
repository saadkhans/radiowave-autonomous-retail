from __future__ import annotations

from radiowave.adapters.rfid.base import NativeRfidRead
from radiowave.contracts import EPC, NATIVE_ANTENNA_KEY, ItemObservation, WorldCoordinate
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.ingestion.deduplication import ObservationDeduplicator
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.simulator.stores import EPC_SHIRT_A
from tests.conftest import at


def _obs(observation_id: str, t: float, epc: str = EPC_SHIRT_A) -> ItemObservation:
    return ItemObservation(
        observation_id=observation_id,
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=epc),
    )


def test_duplicate_ids_and_duplicate_content_are_dropped() -> None:
    dedup = ObservationDeduplicator()
    assert dedup.accept(_obs("a", 1.0)) is True
    assert dedup.accept(_obs("a", 1.0)) is False  # same id
    assert dedup.accept(_obs("b", 1.0)) is False  # same content, re-issued id
    assert dedup.accept(_obs("c", 1.25)) is True  # genuinely new
    assert (dedup.accepted, dedup.dropped) == (2, 2)


def test_dedup_capacity_is_bounded() -> None:
    dedup = ObservationDeduplicator(capacity=4)
    for i in range(10):
        assert dedup.accept(_obs(f"o{i}", i)) is True
    assert dedup.accept(_obs("o0", 0)) is True  # evicted, so accepted again (bounded memory)


def test_rfid_normalization_assigns_zone_from_the_estimate_and_keeps_native_port(
    registry: StoreRegistry,
) -> None:
    # A localized read's zone comes from its world-frame estimate, not the antenna's own
    # pose: pick an estimate that lands squarely inside zone-f1's bounds.
    estimate = registry.transform("rfid-f1").to_sensor(WorldCoordinate(x=4.0, y=6.5, z=0.9))
    read = NativeRfidRead(
        sensor_id="rfid-f1",
        sequence=3,
        timestamp=at(0),
        epc_hex=EPC_SHIRT_A.lower(),
        antenna_port="1",
        rssi_dbm=-52.0,
        phase_rad=0.3,
        read_rate_hz=4.0,
        confidence=0.85,
        estimate=estimate,
        estimate_sigma_m=0.5,
    )
    obs = ObservationNormalizer(registry, "unit").item(read)
    assert obs.epc.value == EPC_SHIRT_A
    assert obs.zone_id == "zone-f1"
    assert obs.metadata[NATIVE_ANTENNA_KEY] == "1"
    assert obs.uncertainty is not None and obs.uncertainty.sigma_x == 0.5
    assert obs.coordinate is not None and obs.coordinate.frame_id == "store"
    assert obs.observation_id == "rfid:rfid-f1:3"


def test_rfid_read_without_estimate_has_zone_only(registry: StoreRegistry) -> None:
    read = NativeRfidRead(
        sensor_id="rfid-exit",
        sequence=0,
        timestamp=at(0),
        epc_hex=EPC_SHIRT_A,
        antenna_port="5",
        rssi_dbm=-60.0,
        confidence=0.6,
    )
    obs = ObservationNormalizer(registry).item(read)
    assert obs.coordinate is None and obs.uncertainty is None
    assert obs.zone_id == "exit"
