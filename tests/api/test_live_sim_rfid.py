"""Operator-driven simulated RFID for LIVE runs (``radiowave.api.live_rfid``).

The bench is tested three ways: its own rules (who a pick attaches to, put back,
leaving the store), end to end with the production pipeline on a scripted walk (a
real PICK and EXIT_WITH_ITEM come out of fusion), and through the HTTP routes of a
live run fed by a fixture radar stream.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from radiowave.adapters.mmwave.ti.config import TiLiveConfig
from radiowave.api.live_rfid import (
    CARRIER_GONE_GRACE_S,
    BenchItemState,
    OperatorRfidBench,
    OperatorRfidError,
)
from radiowave.contracts.events import RetailEventType
from radiowave.contracts.geometry import SpatialUncertainty, WorldCoordinate
from radiowave.contracts.observations import PersonObservation
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
from radiowave.contracts.tracks import PersonTrack, PersonTrackState
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.ingestion.normalization import ObservationNormalizer
from radiowave.pipeline import FoundationPipeline, merge_streams
from tests.unit.mmwave_ti.support import RADAR, T0, live_config

SIM_RFID = "rfid-sim-01"
SHIRT = "E2801160600002000000A001"
SHELF = Box2D(min_x=0.5, min_y=2.0, max_x=1.5, max_y=3.0)  # centre (1.0, 2.5)
# The shelf's zone includes the floor a shopper stands on to reach it (0.6 m all round),
# the same convention as the Virtual Store Lab's racks.
SHELF_ZONE = Box2D(min_x=0.0, min_y=1.4, max_x=2.1, max_y=3.6)
EXIT = Box2D(min_x=5.0, min_y=0.0, max_x=6.0, max_y=1.5)


def lab_store(*, simulated: bool = True) -> Store:
    return Store(
        store_id="lab-sim-rfid",
        name="Lab with a simulated RFID read point",
        floor_bounds=Box2D(min_x=0.0, min_y=0.0, max_x=6.0, max_y=6.0),
        zones=[
            Zone(
                zone_id="floor",
                name="Floor",
                kind=ZoneKind.SALES_FLOOR,
                bounds=Box2D(min_x=0.0, min_y=0.0, max_x=6.0, max_y=6.0),
            ),
            Zone(zone_id="shelf-a-zone", name="Shelf A", kind=ZoneKind.FIXTURE, bounds=SHELF_ZONE),
            Zone(zone_id="exit-zone", name="Exit", kind=ZoneKind.EXIT, bounds=EXIT),
        ],
        fixtures=[
            Fixture(fixture_id="shelf-a", zone_id="shelf-a-zone", name="Shelf A", bounds=SHELF)
        ],
        boundaries=[Boundary(boundary_id="door", kind=BoundaryKind.EXIT, bounds=EXIT)],
        sensors=[
            Sensor(
                sensor_id=RADAR,
                modality=SourceType.MMWAVE,
                pose=SensorPose(position=WorldCoordinate(x=3.0, y=0.0, z=2.4), yaw=math.pi / 2),
            ),
            Sensor(
                sensor_id=SIM_RFID,
                modality=SourceType.RFID,
                pose=SensorPose(position=WorldCoordinate(x=3.0, y=3.0, z=2.5)),
                name="Simulated RFID (operator bench)",
                vendor_metadata=({"simulated": "true", "max_range_m": "8.0"} if simulated else {}),
            ),
        ],
        products=[Product(gtin="4006381333931", name="Folded shirt", home_fixture_id="shelf-a")],
        items=[Item(epc=EPC(value=SHIRT), gtin="4006381333931")],
    )


def person(
    track_id: str, x: float, y: float, state: PersonTrackState = PersonTrackState.ACTIVE
) -> PersonTrack:
    return PersonTrack(
        track_id=track_id,
        state=state,
        created_at=T0,
        updated_at=T0,
        position=WorldCoordinate(x=x, y=y, z=1.0),
        uncertainty=SpatialUncertainty.isotropic(0.35),
        confidence=0.9,
    )


# ------------------------------------------------------------------ bench rules
def test_example_config_enables_the_bench() -> None:
    path = Path(__file__).resolve().parents[2] / "configs/examples/ti-iwr6843-lab-sim-rfid.json"
    bench = OperatorRfidBench(StoreRegistry(TiLiveConfig.load(path).store))
    assert bench.enabled
    assert {v.home_fixture_id for v in bench.items()} == {"shelf-a", "shelf-b"}


def test_bench_is_inert_without_a_simulated_read_point() -> None:
    assert not OperatorRfidBench(StoreRegistry(lab_store(simulated=False))).enabled
    assert OperatorRfidBench(StoreRegistry(lab_store())).enabled


def test_pick_attaches_to_the_nearest_active_person() -> None:
    bench = OperatorRfidBench(StoreRegistry(lab_store()))
    people = [
        person("P0001", 4.0, 4.0),
        person("P0002", 1.2, 2.0),
        person("P0003", 1.0, 2.4, PersonTrackState.LOST),  # nearest, but not active
    ]
    assert bench.pick(SHIRT, people) == "P0002"
    (view,) = bench.items()
    assert view.state == BenchItemState.CARRIED
    assert view.carrier_track_id == "P0002"
    with pytest.raises(OperatorRfidError, match="not on its shelf"):
        bench.pick(SHIRT, people)
    bench.put_back(SHIRT)
    assert bench.items()[0].state == BenchItemState.ON_SHELF


def test_pick_is_refused_when_nobody_is_near_the_shelf() -> None:
    bench = OperatorRfidBench(StoreRegistry(lab_store()))
    with pytest.raises(OperatorRfidError, match="nobody is being tracked"):
        bench.pick(SHIRT, [])
    with pytest.raises(OperatorRfidError, match="nobody is near shelf-a"):
        bench.pick(SHIRT, [person("P0001", 5.5, 5.5)])
    with pytest.raises(OperatorRfidError, match="unknown item"):
        bench.pick("E28011606000020000000000", [person("P0001", 1.0, 2.5)])


def test_carried_item_follows_its_carrier_then_leaves_with_them() -> None:
    registry = StoreRegistry(lab_store())
    bench = OperatorRfidBench(registry, noise=None)
    normalizer = ObservationNormalizer(registry)
    bench.pick(SHIRT, [person("P0001", 1.0, 2.2)])
    # Carrier walks to (4.0, 1.0): localized reads move with them (0.5 m blur).
    xs = []
    for k in range(40):
        now = T0 + timedelta(seconds=0.5 * k)
        for read in bench.reads(now, [person("P0001", 4.0, 1.0)]):
            obs = normalizer.item(read)
            assert obs.coordinate is not None
            xs.append(obs.coordinate.x)
    assert xs, "the virtual reader produced no reads"
    assert abs(sum(xs) / len(xs) - 4.0) < 0.5
    # Carrier's track ends: the item answers for the grace period, then is gone.
    gone_from = T0 + timedelta(seconds=30)
    bench.reads(gone_from, [person("P0001", 4.0, 1.0, PersonTrackState.ENDED)])
    later = gone_from + timedelta(seconds=CARRIER_GONE_GRACE_S + 1.0)
    assert bench.reads(later, []) == []
    assert bench.items()[0].state == BenchItemState.GONE


def test_reads_are_stamped_with_the_given_clock_and_paced() -> None:
    bench = OperatorRfidBench(StoreRegistry(lab_store()), poll_interval_s=0.5)
    first = bench.reads(T0, [])
    assert all(r.timestamp == T0 for r in first)
    assert bench.reads(T0 + timedelta(seconds=0.2), []) == []  # not due yet


# ------------------------------------------------------- end to end with fusion
def _walk(t: float) -> tuple[float, float] | None:
    """Stand by the shelf, walk to the exit, stand there, then leave view."""
    start, end = (1.0, 2.0), (5.5, 0.7)
    if t < 8.0:
        return start
    if t < 18.0:
        f = (t - 8.0) / 10.0
        return start[0] + f * (end[0] - start[0]), start[1] + f * (end[1] - start[1])
    if t < 22.0:
        return end
    return None


def test_pick_and_exit_come_out_of_the_production_pipeline() -> None:
    registry = StoreRegistry(lab_store())
    bench = OperatorRfidBench(registry)
    normalizer = ObservationNormalizer(registry)
    pipeline = FoundationPipeline(registry)
    picked_by = None
    for step in range(int(36.0 / 0.1)):
        t = step * 0.1
        now = T0 + timedelta(seconds=t)
        radar = []
        position = _walk(t)
        if position is not None:
            radar.append(
                PersonObservation(
                    observation_id=f"mmwave:{RADAR}:{step}",
                    sensor_id=RADAR,
                    timestamp=now,
                    confidence=0.9,
                    coordinate=WorldCoordinate(x=position[0], y=position[1], z=1.0),
                    uncertainty=SpatialUncertainty.isotropic(0.35),
                )
            )
        if step == 80:  # t = 8 s: the operator presses "pick up"
            picked_by = bench.pick(SHIRT, pipeline.fusion.person_tracks())
        rfid = [normalizer.item(r) for r in bench.reads(now, pipeline.fusion.person_tracks())]
        for observation in merge_streams(radar, rfid):
            pipeline.ingest(observation)
        pipeline.advance_to(now)
    result = pipeline.finish(advance=False)
    assert picked_by == "P0001"
    events = {(e.event_type, e.shopper_track_id) for e in result.committed_events}
    assert (RetailEventType.PICK, "P0001") in events
    assert (RetailEventType.CARRY, "P0001") in events
    assert (RetailEventType.EXIT_WITH_ITEM, "P0001") in events


# ------------------------------------------------------------- HTTP, live run
pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from radiowave.api.app import create_app  # noqa: E402
from radiowave.api.live import LiveRuntime  # noqa: E402
from tests.fixtures.ti_mmwave.builder import build_target_frame  # noqa: E402
from tests.unit.mmwave_ti.support import FakeTiming, HoldOpenStream  # noqa: E402


def _client(tmp_path: Path, config: TiLiveConfig) -> TestClient:
    # Radar at (3, 0) facing +y: TI (x, y) = world (3 + x, y). Target near the shelf.
    frames = [
        build_target_frame(n, [{"tid": 1, "x": -2.0, "y": 2.2, "z": 1.0}]) for n in range(1, 13)
    ]
    runtime = LiveRuntime(
        config=config,
        stream_factory=lambda: HoldOpenStream(frames),
        timing=FakeTiming().timing(),
        capture_dir=tmp_path,
        autonomous=False,
    )
    return TestClient(create_app(runtime))


@pytest.fixture
def sim_rfid_client(tmp_path: Path) -> Iterator[TestClient]:
    config = live_config(lab_store())
    with _client(tmp_path, config) as client:
        yield client


def test_routes_pick_and_put_back_on_a_live_run(sim_rfid_client: TestClient) -> None:
    from tests.unit.mmwave_ti.support import wait_until

    run_id = sim_rfid_client.post("/api/runs/live", json={"capture": False}).json()["state"][
        "run_id"
    ]
    bench = sim_rfid_client.get(f"/api/runs/{run_id}/sim-rfid").json()
    assert bench["enabled"] is True
    assert bench["attribution_simulated"] is True
    assert [(i["epc"], i["state"], i["home_fixture_name"]) for i in bench["items"]] == [
        (SHIRT, "ON_SHELF", "Shelf A")
    ]
    # Before the radar has seen anyone, a pick is refused with a reason.
    refused = sim_rfid_client.post(
        f"/api/runs/{run_id}/sim-rfid", json={"action": "pick", "epc": SHIRT}
    )
    assert refused.status_code == 409
    assert "nobody" in refused.json()["detail"]

    run = sim_rfid_client.app.state.runs.get(run_id)  # type: ignore[attr-defined]
    assert wait_until(lambda: run.session.queued == 12)
    with run.lock:
        run.tick()
    picked = sim_rfid_client.post(
        f"/api/runs/{run_id}/sim-rfid", json={"action": "pick", "epc": SHIRT}
    )
    assert picked.status_code == 200
    (item,) = picked.json()["items"]
    assert item["state"] == "CARRIED"
    assert item["carrier_track_id"] == "P0001"
    back = sim_rfid_client.post(
        f"/api/runs/{run_id}/sim-rfid", json={"action": "put_back", "epc": SHIRT}
    )
    assert back.json()["items"][0]["state"] == "ON_SHELF"


def test_routes_explain_a_store_without_simulated_rfid(tmp_path: Path) -> None:
    with _client(tmp_path, live_config(lab_store(simulated=False))) as client:
        run_id = client.post("/api/runs/live", json={"capture": False}).json()["state"]["run_id"]
        bench = client.get(f"/api/runs/{run_id}/sim-rfid").json()
        assert bench["enabled"] is False
        assert "simulated RFID" in bench["reason"]
        response = client.post(
            f"/api/runs/{run_id}/sim-rfid", json={"action": "pick", "epc": SHIRT}
        )
        assert response.status_code == 409


def test_live_tick_ingests_marked_bench_reads_and_the_capture_replays(tmp_path: Path) -> None:
    """Through the real LiveObservatoryRun.tick: bench reads are ingested, every one is
    marked simulated in the recording, and the capture replays the same way twice."""
    from radiowave.api.live import LiveObservatoryRun
    from radiowave.api.live_rfid import SIMULATED_METADATA_KEY, SIMULATED_METADATA_VALUE
    from radiowave.replay.reader import JsonlReplaySource
    from tests.unit.mmwave_ti.support import wait_until

    frames = [
        build_target_frame(n, [{"tid": 1, "x": -2.0, "y": 2.2, "z": 1.0}]) for n in range(1, 13)
    ]
    runtime = LiveRuntime(
        config=live_config(lab_store()),
        stream_factory=lambda: HoldOpenStream(frames),
        timing=FakeTiming().timing(),
        capture_dir=tmp_path,
        autonomous=False,
    )
    run = LiveObservatoryRun("live-test", runtime, capture=True)
    assert wait_until(lambda: run.session.queued == 12)
    with run.lock:
        run.tick()
    assert run.rfid_pick(SHIRT) == "P0001"
    for _ in range(20):
        with run.lock:
            run.tick()
    run.stop()
    assert run.capture_path is not None

    entries = list(JsonlReplaySource(run.capture_path).entries())
    rfid = [
        e for e in entries if e.kind.value == "OBSERVATION" and e.source_type == SourceType.RFID
    ]
    assert rfid, "no simulated RFID reads were ingested by the live tick"
    assert all(
        e.payload["metadata"][SIMULATED_METADATA_KEY] == SIMULATED_METADATA_VALUE for e in rfid
    )

    def replay() -> str:
        from radiowave.contracts.recording import validate_recording

        twin = next(e for e in entries if e.kind.value == "STORE_TWIN")
        pipeline = FoundationPipeline(StoreRegistry(Store.model_validate(twin.payload)))
        source = JsonlReplaySource(run.capture_path)  # type: ignore[arg-type]
        result = pipeline.replay(validate_recording(source.entries()))
        assert result.observations_accepted > len(rfid) - 1
        return result.model_dump_json()

    assert replay() == replay()
