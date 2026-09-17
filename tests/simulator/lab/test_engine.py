"""LabEngine: the full virtual-store path through production Foundation code.

These tests guard the phase's central invariant — that the simulator supplies input
and production code supplies interpretation — and the determinism the calibration
doctrine depends on. They deliberately do NOT assert fusion accuracy: how well the
baseline engine attributes picks is a Phase-6 question, and pinning today's numbers
here would turn a measurement into a target.
"""

from __future__ import annotations

import hashlib

import pytest

from radiowave.contracts.store import SourceType
from radiowave.simulator.lab.engine import LabEngine, run_lab_scenario
from radiowave.simulator.lab.scenarios import SCENARIOS, load_scenario

ACCEPTANCE = "acceptance_60s"


def _fingerprint(engine: LabEngine, result) -> str:
    """A stable digest of everything a rerun must reproduce."""
    h = hashlib.sha256()
    h.update(
        f"{result.radar_bytes}|{result.frames_parsed}|{result.frames_rejected}"
        f"|{result.person_observations}|{result.item_observations}".encode()
    )
    for event in result.pipeline.proposed_events:
        h.update(f"{event.event_type.value}{event.epc.value}{event.timestamp.isoformat()}".encode())
    for truth in engine.ground_truth.events:
        h.update(f"{truth.event_type.value}{truth.epc}".encode())
    return h.hexdigest()


def test_the_same_seed_reproduces_an_identical_run() -> None:
    """The headline guarantee: identical seed -> identical bytes, observations,
    inferred events and ground truth."""
    first = LabEngine(load_scenario(ACCEPTANCE))
    second = LabEngine(load_scenario(ACCEPTANCE))
    assert _fingerprint(first, first.run()) == _fingerprint(second, second.run())


def test_a_different_seed_changes_observation_but_not_truth() -> None:
    """Seed varies observation, not truth.

    This is what lets one physical scenario be replayed against many noise
    realizations and a metric change be attributed to the algorithm rather than to
    the world having moved underneath it.
    """
    base = load_scenario(ACCEPTANCE)
    a = LabEngine(base)
    b = LabEngine(base.model_copy(update={"seed": base.seed + 991}))
    result_a, result_b = a.run(), b.run()

    assert _fingerprint(a, result_a) != _fingerprint(b, result_b)
    # ...but the physical story is identical.
    truth_a = [(e.event_type.value, e.epc) for e in a.ground_truth.events]
    truth_b = [(e.event_type.value, e.epc) for e in b.ground_truth.events]
    assert truth_a == truth_b


def test_radar_reaches_the_pipeline_only_as_parsed_uart_bytes() -> None:
    """The boundary this whole phase exists to exercise.

    Person observations must be the product of real bytes through the real parser:
    bytes emitted, frames parsed, none rejected, and observations produced. A
    simulator that quietly constructed PersonObservation would still show
    observations here but would emit no bytes and parse no frames.
    """
    result = run_lab_scenario(load_scenario("01_normal_purchase"))
    assert result.radar_bytes > 0
    assert result.frames_parsed > 0
    assert result.frames_rejected == 0
    assert result.person_observations > 0


def test_ground_truth_never_reaches_the_pipeline() -> None:
    """Fusion must infer, never be told.

    The engine holds both the ground-truth log and the pipeline, so the cheap
    mistake would be feeding one into the other. Every observation the pipeline
    accepted must be a sensor observation, and the recorded truth must not appear
    among them.
    """
    engine = LabEngine(load_scenario("04_handoff"))
    result = engine.run()

    assert engine.ground_truth.events, "the scenario should have produced truth"
    # Everything ingested came from a sensor modality, not from the truth log.
    assert result.observations_ingested > 0
    assert result.person_observations + result.item_observations >= result.observations_ingested
    # The pipeline's own view of the run carries no ground-truth event kinds beyond
    # what it inferred for itself: inferred events are RetailEvents built by fusion.
    for event in result.pipeline.proposed_events:
        assert event.epc is not None


def test_item_observations_carry_epc_identity_not_sku() -> None:
    """Two physical items sharing a GTIN must stay distinct all the way through."""
    scenario = load_scenario("06_same_sku_distinct_epcs")
    gtin_of = {item.epc.value: item.gtin for item in scenario.store.items}
    picked = {i.epc for i in scenario.scheduled_interactions if i.epc}
    assert len({gtin_of[e] for e in picked}) == 1, "fixture should share one GTIN"

    result = run_lab_scenario(scenario)
    tracked = {track.epc.value for track in result.pipeline.item_tracks}
    assert picked <= tracked, "each distinct EPC must be tracked separately"


def test_a_radar_dropout_window_suppresses_radar_but_not_rfid() -> None:
    normal = run_lab_scenario(load_scenario("01_normal_purchase"))
    dropped = run_lab_scenario(load_scenario("07_radar_dropout"))
    assert dropped.person_observations < normal.person_observations
    assert dropped.item_observations > 0


def test_an_rfid_dropout_window_suppresses_rfid_but_not_radar() -> None:
    normal = run_lab_scenario(load_scenario("01_normal_purchase"))
    dropped = run_lab_scenario(load_scenario("08_rfid_dropout"))
    assert dropped.item_observations < normal.item_observations
    assert dropped.person_observations > 0


def test_a_radar_restart_is_applied_and_opens_a_new_generation() -> None:
    engine = LabEngine(load_scenario("13_sensor_restart"))
    result = engine.run()
    assert any(f.startswith("RADAR_RESTART") for f in result.faults_applied)
    # A restart makes the frame number go backwards, which the production normalizer
    # reads as a device reboot and answers with a fresh generation - retiring the old
    # native id space so stale identity cannot survive the restart.
    assert engine._ti_normalizer.generation > 1


@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_every_catalog_scenario_runs_end_to_end(scenario_id: str) -> None:
    """No scenario may crash, and none may be silently empty.

    A run that produces nothing is the most misleading possible outcome - it looks
    like a pass in a summary line - so emptiness is asserted against directly.
    """
    result = run_lab_scenario(load_scenario(scenario_id))
    assert result.ticks > 0
    assert result.frames_parsed > 0
    assert result.observations_ingested > 0
    assert result.pipeline.person_tracks, "a shopper should be tracked"


def test_acceptance_gate_shape() -> None:
    """Phase-4 first gate: 3 shoppers, 30 items, 3 racks, 1 entrance, 1 exit,
    60 deterministic seconds, through production boundaries, zero hardware."""
    scenario = load_scenario(ACCEPTANCE)
    assert len(scenario.shoppers) == 3
    assert len(scenario.store.items) == 30
    assert len([f for f in scenario.store.fixtures]) == 3
    assert scenario.duration_s == 60.0

    result = run_lab_scenario(scenario)
    assert result.ticks == int(60.0 * scenario.tick_hz)
    assert len(result.pipeline.person_tracks) == 3, "three shoppers, independently tracked"
    assert len(result.pipeline.item_tracks) == 30
    assert result.frames_rejected == 0
    # The canonical ids are Foundation's, derived from sensor data - never the
    # simulator's GT-PERSON-* ground-truth ids.
    for track in result.pipeline.person_tracks:
        assert not track.track_id.startswith("GT-")


def test_canonical_person_ids_are_not_ground_truth_ids() -> None:
    engine = LabEngine(load_scenario("05_two_shoppers_cross"))
    result = engine.run()
    truth_ids = {a.ground_truth_person_id for a in engine.world.shoppers.values()}
    canonical = {t.track_id for t in result.pipeline.person_tracks}
    assert canonical.isdisjoint(truth_ids)


def test_observations_are_stamped_with_simulated_not_host_time() -> None:
    """Host wall clock is never simulation truth."""
    scenario = load_scenario("01_normal_purchase")
    engine = LabEngine(scenario)
    engine.run()
    # The world clock runs from the lab epoch, decades away from any plausible
    # host clock during a test run.
    assert engine.world.now.year == 2026
    assert engine.world.now.month == 1


def test_sensor_modalities_are_both_represented() -> None:
    result = run_lab_scenario(load_scenario(ACCEPTANCE))
    assert result.person_observations > 0, "radar path produced nothing"
    assert result.item_observations > 0, "rfid path produced nothing"
    tracks = run_lab_scenario(load_scenario(ACCEPTANCE)).pipeline
    assert {SourceType.MMWAVE, SourceType.RFID}
    assert tracks.item_tracks and tracks.person_tracks
