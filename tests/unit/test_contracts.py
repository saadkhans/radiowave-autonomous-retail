from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from radiowave.contracts import (
    EPC,
    NATIVE_ANTENNA_KEY,
    Box2D,
    ConfidenceThresholds,
    Item,
    ItemObservation,
    PersonObservation,
    Product,
    RetailEvent,
    RetailEventType,
    SpatialUncertainty,
    Store,
    Vector3D,
    Velocity,
    WorldCoordinate,
    make_event_id,
)
from radiowave.contracts.tracks import AssociationEvidence, CandidateScore
from radiowave.simulator.stores import (
    EPC_SHIRT_A,
    EPC_SHIRT_B,
    GTIN_BLACK_SHIRT_L,
    build_lab_store,
)
from tests.conftest import T0


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        PersonObservation(
            observation_id="o1",
            sensor_id="radar-north",
            timestamp=datetime(2026, 1, 1),
            confidence=0.9,
            coordinate=WorldCoordinate(x=1, y=2),
            uncertainty=SpatialUncertainty.isotropic(0.1),
        )


def test_aware_timestamp_normalized_to_utc() -> None:
    plus_three = timezone(timedelta(hours=3))
    obs = ItemObservation(
        observation_id="o1",
        sensor_id="rfid-f1",
        timestamp=datetime(2026, 1, 1, 3, 0, tzinfo=plus_three),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
    )
    assert obs.timestamp == datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    assert obs.timestamp.tzinfo == UTC


def test_epc_normalized_and_validated() -> None:
    assert EPC(value=" 3034f1a0000000000000a001 ").value == EPC_SHIRT_A
    with pytest.raises(ValidationError):
        EPC(value="not-hex")
    with pytest.raises(ValidationError):
        EPC(value="ABC")  # too short


def test_gtin_validated() -> None:
    Product(gtin="06281234567890", name="ok")
    with pytest.raises(ValidationError):
        Product(gtin="123", name="bad")


def test_identical_sku_units_have_distinct_epc_identity() -> None:
    a = Item(epc=EPC(value=EPC_SHIRT_A), gtin=GTIN_BLACK_SHIRT_L)
    b = Item(epc=EPC(value=EPC_SHIRT_B), gtin=GTIN_BLACK_SHIRT_L)
    assert a.gtin == b.gtin
    assert a.epc != b.epc
    assert a != b
    assert len({a.epc, b.epc}) == 2  # hashable and distinct as dict keys


def test_store_rejects_duplicate_epc_and_dangling_references() -> None:
    store = build_lab_store()
    with pytest.raises(ValidationError, match="duplicate epc"):
        Store.model_validate(
            {**store.model_dump(), "items": [i.model_dump() for i in store.items * 2]}
        )
    with pytest.raises(ValidationError, match="unknown fixture"):
        Store.model_validate(
            {
                **store.model_dump(),
                "items": [
                    {
                        "epc": {"value": EPC_SHIRT_A},
                        "gtin": GTIN_BLACK_SHIRT_L,
                        "home_fixture_id": "nope",
                    }
                ],
            }
        )


def test_native_fields_live_in_metadata_not_identity() -> None:
    obs = ItemObservation(
        observation_id="rfid:rfid-f1:1",
        sensor_id="rfid-f1",
        timestamp=T0,
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        metadata={NATIVE_ANTENNA_KEY: "3", "reader_serial": "IMP-000"},
    )
    assert obs.metadata[NATIVE_ANTENNA_KEY] == "3"
    assert not hasattr(obs, "antenna_id")
    assert obs.source_type == "RFID"
    with pytest.raises(ValidationError):
        ItemObservation.model_validate({**obs.model_dump(), "confidence": 1.5})


def test_geometry_primitives() -> None:
    v = Vector3D(x=3, y=4, z=0)
    assert v.norm == 5.0
    assert (v - Vector3D(x=3, y=4)).norm == 0.0
    assert Velocity(vx=3, vy=4).horizontal_speed == 5.0
    assert WorldCoordinate(x=0, y=0).horizontal_distance_to(WorldCoordinate(x=3, y=4, z=9)) == 5.0
    box = Box2D(min_x=0, min_y=0, max_x=2, max_y=2)
    assert box.contains(WorldCoordinate(x=1, y=1))
    assert box.distance_to(WorldCoordinate(x=5, y=1)) == 3.0
    with pytest.raises(ValidationError):
        Box2D(min_x=0, min_y=0, max_x=0, max_y=1)
    assert SpatialUncertainty.isotropic(0.5).horizontal_sigma == pytest.approx(0.5)


def _candidate(track_id: str, score: float) -> CandidateScore:
    evidence = AssociationEvidence(
        distance_score=score,
        distance_trend_score=score,
        velocity_score=score,
        temporal_score=score,
        co_motion_score=score,
        zone_score=score,
        vision_score=0.5,
        combined=score,
    )
    return CandidateScore(person_track_id=track_id, score=score, evidence=evidence)


def test_event_margin_and_deterministic_id() -> None:
    epc = EPC(value=EPC_SHIRT_A)
    event = RetailEvent(
        event_id=make_event_id(RetailEventType.PICK, epc, T0),
        event_type=RetailEventType.PICK,
        timestamp=T0,
        epc=epc,
        confidence=0.8,
        candidates=[_candidate("P0001", 0.8), _candidate("P0002", 0.3)],
    )
    assert event.margin == pytest.approx(0.5)
    assert event.model_copy(update={"candidates": [_candidate("P0001", 0.8)]}).margin == 1.0
    assert event.model_copy(update={"candidates": []}).margin == 1.0
    assert make_event_id(RetailEventType.PICK, epc, T0) == make_event_id(
        RetailEventType.PICK, epc, T0
    )
    assert make_event_id(RetailEventType.PICK, epc, T0) != make_event_id(
        RetailEventType.PUTBACK, epc, T0
    )


def test_confidence_thresholds_are_ordered() -> None:
    with pytest.raises(ValidationError):
        ConfidenceThresholds(commit_min_confidence=0.5, wait_min_confidence=0.6)
