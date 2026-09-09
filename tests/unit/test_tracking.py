from __future__ import annotations

from radiowave.contracts import (
    EPC,
    NATIVE_TRACK_KEY,
    ItemObservation,
    PersonObservation,
    PersonTrackState,
    SpatialUncertainty,
    Velocity,
    WorldCoordinate,
)
from radiowave.fusion.config import ItemTrackingConfig, PersonTrackingConfig
from radiowave.fusion.tracking import ItemTrackManager, PersonTrackManager
from radiowave.simulator.stores import EPC_SHIRT_A
from tests.conftest import at


def _person(
    sensor: str, native: str, t: float, x: float, y: float, vx: float = 0.0
) -> PersonObservation:
    return PersonObservation(
        observation_id=f"mmwave:{sensor}:{t}:{native}",
        sensor_id=sensor,
        timestamp=at(t),
        confidence=0.9,
        coordinate=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.08),
        velocity=Velocity(vx=vx),
        metadata={NATIVE_TRACK_KEY: native},
    )


def test_two_sensors_observing_one_person_share_a_canonical_track() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    a = manager.ingest(_person("radar-north", "T1", 0.0, 2.0, 2.0))
    b = manager.ingest(_person("radar-south", "T9", 0.05, 2.05, 1.95))
    assert a.track_id == b.track_id == "P0001"
    assert sorted(a.sensor_last_update) == ["radar-north", "radar-south"]
    assert "T1" not in a.track_id and "T9" not in a.track_id


def test_one_sensor_cannot_feed_two_native_tracks_into_one_person() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    first = manager.ingest(_person("radar-north", "T1", 0.0, 2.0, 2.0))
    second = manager.ingest(_person("radar-north", "T2", 0.0, 2.5, 2.0))  # 0.5 m apart
    assert first.track_id != second.track_id
    assert [t.track_id for t in manager.all] == ["P0001", "P0002"]


def test_dropout_reacquires_the_same_canonical_track() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    for i in range(10):
        manager.ingest(_person("radar-north", "T1", i * 0.1, 1.0 + i * 0.1, 2.0, vx=1.0))
    manager.step(at(2.5))  # 1.6 s silence -> LOST
    lost = manager.get("P0001")
    assert lost is not None and lost.state == PersonTrackState.LOST
    # A new native id appears where a 1 m/s walker would now be.
    reacquired = manager.ingest(_person("radar-north", "T4", 2.6, 1.9 + 1.7, 2.0, vx=1.0))
    assert reacquired.track_id == "P0001"
    assert reacquired.state == PersonTrackState.ACTIVE
    assert len(manager.all) == 1


def test_track_ends_after_long_silence_and_new_person_gets_new_id() -> None:
    manager = PersonTrackManager(PersonTrackingConfig())
    manager.ingest(_person("radar-north", "T1", 0.0, 1.0, 2.0))
    manager.step(at(20.0))
    assert manager.get("P0001") is not None
    assert manager.get("P0001").state == PersonTrackState.ENDED  # type: ignore[union-attr]
    later = manager.ingest(_person("radar-north", "T1", 20.5, 1.0, 2.0))
    assert later.track_id == "P0002"


def _item(t: float, x: float, y: float) -> ItemObservation:
    return ItemObservation(
        observation_id=f"rfid:rfid-f1:{t}",
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        coordinate=WorldCoordinate(x=x, y=y, z=0.9),
        uncertainty=SpatialUncertainty.isotropic(0.5),
    )


def test_item_estimate_is_smoothed_and_rest_position_needs_initial_reads() -> None:
    config = ItemTrackingConfig(smoothing_alpha=0.5, rest_init_reads=3)
    manager = ItemTrackManager(config)
    track = manager.ingest(_item(0.0, 0.0, 0.0))
    assert track.rest_position is None
    manager.ingest(_item(0.1, 2.0, 0.0))
    assert track.position is not None and track.position.x == 1.0  # EMA, not raw
    manager.ingest(_item(0.2, 2.0, 0.0))
    assert track.rest_position is not None
    assert track.uncertainty is not None and track.uncertainty.sigma_x == 0.5


def test_item_smoother_resets_after_long_gap() -> None:
    config = ItemTrackingConfig(smoothing_alpha=0.25, reset_after_s=3.0)
    manager = ItemTrackManager(config)
    manager.ingest(_item(0.0, 0.0, 0.0))
    manager.ingest(_item(0.5, 0.0, 0.0))
    track = manager.ingest(_item(4.0, 5.0, 5.0))
    assert track.position is not None and (track.position.x, track.position.y) == (5.0, 5.0)
    assert track.is_stale(at(6.0), stale_after_s=1.5)
    assert not track.is_stale(at(4.5), stale_after_s=1.5)


def test_half_window_velocity_and_rest_displacement() -> None:
    manager = ItemTrackManager(ItemTrackingConfig(smoothing_alpha=1.0, history_length=100))
    for i in range(20):
        manager.ingest(_item(i * 0.1, i * 0.1, 0.0))  # 1 m/s along x
    track = manager.get(EPC(value=EPC_SHIRT_A))
    assert track is not None
    assert abs(track.velocity(1.5).vx - 1.0) < 0.05
    assert track.rest_displacement(1.5) is not None and track.rest_displacement(1.5) > 0.5
    still = ItemTrackManager(ItemTrackingConfig(smoothing_alpha=1.0))
    for i in range(20):
        still.ingest(_item(i * 0.1, 1.0, 1.0))
    rest = still.get(EPC(value=EPC_SHIRT_A))
    assert rest is not None and rest.rest_displacement(1.5) == 0.0
