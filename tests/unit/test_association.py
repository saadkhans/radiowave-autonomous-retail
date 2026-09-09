from __future__ import annotations

import pytest

from radiowave.contracts import (
    EPC,
    NATIVE_TRACK_KEY,
    ItemObservation,
    PersonObservation,
    SpatialUncertainty,
    Velocity,
    VisionEvidence,
    VisionEvidenceKind,
    WorldCoordinate,
)
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.association import (
    AssociationScorer,
    CandidateLedger,
    assign_vision_to_nearest,
)
from radiowave.fusion.config import AssociationConfig, ItemTrackingConfig, PersonTrackingConfig
from radiowave.fusion.tracking import ItemTrackManager, PersonTrackManager
from radiowave.simulator.stores import EPC_SHIRT_A, FIXTURE_F1
from tests.conftest import at

ITEM_CFG = ItemTrackingConfig(smoothing_alpha=1.0, rest_init_reads=2)


def _person(track: str, t: float, x: float, y: float, vx: float, vy: float) -> PersonObservation:
    return PersonObservation(
        observation_id=f"mmwave:radar-north:{track}:{t:.2f}",
        sensor_id="radar-north",
        timestamp=at(t),
        confidence=0.9,
        coordinate=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(vx=vx, vy=vy),
        metadata={NATIVE_TRACK_KEY: track},
    )


def _item(t: float, x: float, y: float) -> ItemObservation:
    return ItemObservation(
        observation_id=f"rfid:rfid-f1:{t:.2f}",
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        coordinate=WorldCoordinate(x=x, y=y, z=0.9),
        uncertainty=SpatialUncertainty.isotropic(0.5),
    )


def _world(registry: StoreRegistry) -> tuple[PersonTrackManager, ItemTrackManager]:
    persons = PersonTrackManager(PersonTrackingConfig())
    items = ItemTrackManager(ITEM_CFG)
    items.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
    # Item rests on F1, then moves with the carrier "T1" walking south at 1 m/s.
    # "T2" stands 2 m east of the fixture the whole time.
    for i in range(40):
        t = i * 0.1
        if t < 1.0:
            items.ingest(_item(t, 4.0, 6.5))
            persons.ingest(_person("T1", t, 4.0, 5.4, 0.0, 0.0))
        else:
            y = 5.4 - (t - 1.0)
            items.ingest(_item(t, 4.25, y))
            persons.ingest(_person("T1", t, 4.0, y, 0.0, -1.0))
        persons.ingest(_person("T2", t, 6.0, 5.4, 0.0, 0.0))
    track = items.get(EPC(value=EPC_SHIRT_A))
    assert track is not None
    track.rest_position = WorldCoordinate(x=4.0, y=6.5, z=0.9)
    track.movement_start_at = at(1.0)
    return persons, items


def test_evidence_is_explainable_and_carrier_outscores_bystander(registry: StoreRegistry) -> None:
    persons, items = _world(registry)
    config = AssociationConfig()
    scorer = AssociationScorer(config, ITEM_CFG, registry, vision_enabled=False)
    ledger = CandidateLedger(config)
    item = items.get(EPC(value=EPC_SHIRT_A))
    assert item is not None
    carrier = persons.get("P0001")
    bystander = persons.get("P0002")
    assert carrier is not None and bystander is not None
    now = at(3.9)
    for _ in range(8):
        for person in (carrier, bystander):
            pair = ledger.pair(item.epc, person.track_id)
            evidence = scorer.score(item, person, pair, now, [], {})
            ledger.update(item.epc, person.track_id, evidence, now)
    ranking = ledger.ranking(item.epc, now, item.movement_start_at)
    assert [c.person_track_id for c in ranking.candidates] == ["P0001", "P0002"]
    top = ranking.candidates[0].evidence
    assert set(top.weights) == {
        "distance",
        "distance_trend",
        "velocity",
        "temporal",
        "co_motion",
        "zone",
    }
    weighted = sum(getattr(top, f"{k}_score") * w for k, w in top.weights.items())
    assert top.combined == pytest.approx(weighted / sum(top.weights.values()))
    assert top.features["distance_m"] < 0.5
    assert top.velocity_score > 0.8 and top.co_motion_score > 0.5 and top.zone_score == 1.0
    assert ranking.candidates[1].evidence.velocity_score == 0.0
    assert ranking.margin > 0.4


def test_vision_evidence_is_assigned_exclusively_to_nearest_person(registry: StoreRegistry) -> None:
    persons, _ = _world(registry)
    evidence = VisionEvidence(
        observation_id="vision:cam-main:0",
        sensor_id="cam-main",
        timestamp=at(1.0),
        confidence=0.85,
        kind=VisionEvidenceKind.REACH_INTO_FIXTURE,
        coordinate=WorldCoordinate(x=4.1, y=5.5),
        uncertainty=SpatialUncertainty.isotropic(0.3),
    )
    owner = assign_vision_to_nearest([evidence], persons.all, radius_m=3.0)
    assert owner == {"vision:cam-main:0": "P0001"}
    assert assign_vision_to_nearest([evidence], persons.all, radius_m=0.05) == {}


def test_vision_profile_only_applies_when_enabled(registry: StoreRegistry) -> None:
    persons, items = _world(registry)
    item = items.get(EPC(value=EPC_SHIRT_A))
    carrier = persons.get("P0001")
    assert item is not None and carrier is not None
    evidence = VisionEvidence(
        observation_id="v0",
        sensor_id="cam-main",
        timestamp=at(1.2),
        confidence=0.85,
        kind=VisionEvidenceKind.ITEM_IN_HAND,
        coordinate=WorldCoordinate(x=4.0, y=5.4),
        uncertainty=SpatialUncertainty.isotropic(0.3),
    )
    config = AssociationConfig()
    with_vision = AssociationScorer(config, ITEM_CFG, registry, vision_enabled=True)
    without = AssociationScorer(config, ITEM_CFG, registry, vision_enabled=False)
    ledger = CandidateLedger(config)
    pair = ledger.pair(item.epc, carrier.track_id)
    scored = with_vision.score(item, carrier, pair, at(3.9), [evidence], {"v0": "P0001"})
    assert scored.vision_score == 1.0 and "vision" in scored.weights
    plain = without.score(item, carrier, pair, at(3.9), [evidence], {"v0": "P0001"})
    assert "vision" not in plain.weights and plain.vision_score == 0.5
    other = with_vision.score(item, carrier, pair, at(3.9), [evidence], {"v0": "P0002"})
    assert other.vision_score == 0.1


def test_carry_profile_drops_pick_time_features(registry: StoreRegistry) -> None:
    persons, items = _world(registry)
    item = items.get(EPC(value=EPC_SHIRT_A))
    carrier = persons.get("P0001")
    assert item is not None and carrier is not None
    scorer = AssociationScorer(AssociationConfig(), ITEM_CFG, registry, vision_enabled=True)
    pair = CandidateLedger(AssociationConfig()).pair(item.epc, carrier.track_id)
    carry = scorer.score(item, carrier, pair, at(3.9), [], {}, carrier_committed=True)
    assert set(carry.weights) == {"distance", "distance_trend", "velocity", "co_motion"}
