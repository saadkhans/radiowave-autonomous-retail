"""Regressions for the third Codex review round (cart lifecycles, contract validators)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from radiowave.adapters.rfid.base import NativeRfidRead
from radiowave.cart.engine import InMemoryCartEngine
from radiowave.cart.models import CartStatus
from radiowave.contracts import (
    EPC,
    CartEventType,
    ItemObservation,
    ItemState,
    RetailEvent,
    RetailEventType,
    SensorCoordinate,
    SpatialUncertainty,
    WorldCoordinate,
    make_event_id,
)
from radiowave.contracts.recording import RecordedEntry
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.config import AssociationConfig, ItemTrackingConfig, StateMachineConfig
from radiowave.fusion.state_machine import ItemStateMachine
from radiowave.fusion.tracking import ItemTrackState
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.simulator.library import load_scenario
from radiowave.simulator.scenario import Carry, Scenario
from radiowave.simulator.stores import EPC_SHIRT_A, EPC_SHIRT_B
from tests.conftest import at

A = EPC(value=EPC_SHIRT_A)
B = EPC(value=EPC_SHIRT_B)


def _event(kind: RetailEventType, epc: EPC, shopper: str, t: float) -> RetailEvent:
    return RetailEvent(
        event_id=make_event_id(kind, epc, at(t), shopper),
        event_type=kind,
        timestamp=at(t),
        epc=epc,
        shopper_track_id=shopper,
        confidence=0.9,
    )


def test_reentry_after_exit_opens_a_new_cart_lifecycle() -> None:
    engine = InMemoryCartEngine()
    engine.apply(_event(RetailEventType.PICK, A, "P0001", 1.0))
    engine.apply(_event(RetailEventType.EXIT_WITH_ITEM, A, "P0001", 5.0))
    engine.close_cart("P0001", at(5.0))  # session exited
    result = engine.apply(_event(RetailEventType.PICK, B, "P0001", 30.0))
    assert result.cart_event_type == CartEventType.ADD
    assert result.cart_id == "P0001#2"
    exited = engine.state.carts["P0001"]
    assert exited.status == CartStatus.EXITED and exited.epcs == {EPC_SHIRT_A}
    assert exited.lines[EPC_SHIRT_A].final_ownership_candidate is True
    fresh = engine.state.carts["P0001#2"]
    assert fresh.status == CartStatus.OPEN and fresh.epcs == {EPC_SHIRT_B}
    assert engine.state.epcs_in("P0001") == {EPC_SHIRT_B}
    # A second exit freezes the second lifecycle without touching the first.
    engine.apply(_event(RetailEventType.EXIT_WITH_ITEM, B, "P0001", 40.0))
    engine.close_cart("P0001", at(40.0))
    assert engine.state.carts["P0001"].epcs == {EPC_SHIRT_A}
    assert engine.state.carts["P0001#2"].status == CartStatus.EXITED


def test_item_observation_location_needs_uncertainty() -> None:
    with pytest.raises(ValidationError, match="together"):
        ItemObservation(
            observation_id="o",
            sensor_id="rfid-f1",
            timestamp=at(0),
            confidence=0.8,
            epc=A,
            coordinate=WorldCoordinate(x=1.0, y=1.0),
        )
    with pytest.raises(ValidationError, match="together"):
        ItemObservation(
            observation_id="o",
            sensor_id="rfid-f1",
            timestamp=at(0),
            confidence=0.8,
            epc=A,
            uncertainty=SpatialUncertainty.isotropic(0.5),
        )


def test_non_finite_coordinates_are_rejected() -> None:
    with pytest.raises(ValidationError):
        WorldCoordinate(x=float("nan"), y=1.0)
    with pytest.raises(ValidationError):
        SpatialUncertainty.isotropic(float("inf"))


def test_unknown_recording_format_version_is_rejected() -> None:
    # Version 2 became the current format with Observatory v0 (CLOCK / RUN_END /
    # DUPLICATE_OBSERVATION); a version this reader does not understand is still refused.
    entry = RecordedEntry(sequence=0, timestamp=at(0), kind="GROUND_TRUTH", payload={})
    with pytest.raises(ValidationError, match="unsupported recording format_version 3"):
        RecordedEntry.model_validate({**entry.model_dump(mode="json"), "format_version": 3})


def test_reversed_or_out_of_range_carries_are_rejected() -> None:
    with pytest.raises(ValidationError, match="before it starts"):
        Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=5.0, end_t=2.0)
    with pytest.raises(ValidationError):
        Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=-1.0)
    scenario = load_scenario("01")
    late = Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=99.0)
    with pytest.raises(ValidationError, match="after the scenario ends"):
        Scenario.model_validate({**scenario.model_dump(), "carries": [late.model_dump()]})


def test_association_weight_profiles_are_validated() -> None:
    with pytest.raises(ValidationError, match="unknown features"):
        AssociationConfig(pick_weights={"distance": 1.0, "haircut": 0.5})
    with pytest.raises(ValidationError, match="non-negative"):
        AssociationConfig(carry_weights={"distance": 1.0, "velocity": -0.9})
    with pytest.raises(ValidationError, match="positive total"):
        AssociationConfig(carry_weights={"distance": 0.0})


def test_normalizer_refuses_samples_wired_to_the_wrong_modality(registry: StoreRegistry) -> None:
    read = NativeRfidRead(
        sensor_id="radar-north",
        sequence=0,
        timestamp=at(0),
        epc_hex=EPC_SHIRT_A,
        antenna_port="1",
        rssi_dbm=-50.0,
        confidence=0.8,
        estimate=SensorCoordinate(x=0.1, y=0.1, z=1.0, frame_id="radar-north"),
        estimate_sigma_m=0.5,
    )
    with pytest.raises(ValueError, match="registered as MMWAVE"):
        ObservationNormalizer(registry).item(read)


def test_items_without_a_home_never_rest_on_fixture(registry: StoreRegistry) -> None:
    machine = ItemStateMachine(StateMachineConfig(), ItemTrackingConfig(), registry)
    homeless = ItemTrackState(epc=A, gtin=None, home_fixture_id=None)
    assert machine.classify_rest(homeless, WorldCoordinate(x=4.0, y=6.5)) == ItemState.MISPLACED
    homed = ItemTrackState(epc=A, gtin=None, home_fixture_id="F1")
    assert machine.classify_rest(homed, WorldCoordinate(x=4.0, y=6.5)) == ItemState.ON_FIXTURE
