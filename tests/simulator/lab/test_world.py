"""Determinism and clock-derivation checks for the fixed-timestep virtual world."""

from __future__ import annotations

from datetime import UTC

from radiowave.simulator.lab import actors, interactions
from radiowave.simulator.lab.ground_truth import GroundTruthLog
from radiowave.simulator.lab.store import (
    ENTRY_POINT,
    EPCS_RACK_A,
    EPCS_RACK_B,
    EPCS_RACK_C,
    EXIT_POINT,
    RACK_A,
    RACK_A_APPROACH,
    RACK_B,
    RACK_B_APPROACH,
    RACK_C,
    RACK_C_APPROACH,
)
from radiowave.simulator.lab.world import LAB_WORLD_EPOCH, VirtualWorld, WorldConfig

_RACKS = (
    (RACK_A, RACK_A_APPROACH, EPCS_RACK_A[0]),
    (RACK_B, RACK_B_APPROACH, EPCS_RACK_B[0]),
    (RACK_C, RACK_C_APPROACH, EPCS_RACK_C[0]),
)


def _run(seed: int, ticks_per_leg: int = 60) -> tuple[VirtualWorld, GroundTruthLog]:
    """Three shoppers enter, each approach a different rack with seed-jittered standing
    spots, pick one item, and exit. Fully driven by tick count only."""
    world = VirtualWorld(WorldConfig(tick_hz=20.0, seed=seed))
    log = GroundTruthLog()
    shoppers = [
        interactions.enter(world, log, ENTRY_POINT, f"GT-PERSON-{i + 1:03d}") for i in range(3)
    ]
    for shopper, (fixture_id, approach, _epc) in zip(shoppers, _RACKS, strict=True):
        jittered = actors.jitter(world, approach, sigma=0.05)
        interactions.approach_fixture(
            world, log, shopper.ground_truth_person_id, fixture_id, jittered
        )
    for _ in range(ticks_per_leg):
        world.step()
        log.snapshot(world)
    for shopper, (_fixture_id, _approach, epc) in zip(shoppers, _RACKS, strict=True):
        interactions.pick(world, log, epc, shopper.ground_truth_person_id)
    for shopper in shoppers:
        interactions.exit_store(world, log, shopper.ground_truth_person_id, EXIT_POINT)
    for _ in range(ticks_per_leg * 3):
        world.step()
        log.snapshot(world)
    return world, log


def _fingerprint(world: VirtualWorld, log: GroundTruthLog) -> tuple:
    """A hashable/comparable summary of everything that should be reproducible."""
    shopper_positions = tuple(
        (pid, round(actor.position.x, 9), round(actor.position.y, 9), actor.present)
        for pid, actor in sorted(world.shoppers.items())
    )
    item_positions = tuple(
        (epc, round(item.position.x, 9), round(item.position.y, 9), item.carrier_id)
        for epc, item in sorted(world.items.items())
    )
    events = tuple((e.tick, e.event_type, e.ground_truth_person_id, e.epc) for e in log.events)
    # Endpoints converge regardless of seed (everyone walks to the same fixed exit
    # point), so the seed-dependent jitter only shows up mid-path; include the
    # sampled trajectories (built from per-tick snapshots) to capture that.
    trajectories = tuple(
        (pid, tuple((round(s.position.x, 9), round(s.position.y, 9)) for s in samples))
        for pid, samples in sorted(log.shopper_trajectories.items())
    )
    return (world.tick, shopper_positions, item_positions, events, trajectories)


def test_same_seed_produces_identical_world_evolution_and_log() -> None:
    world_a, log_a = _run(seed=42)
    world_b, log_b = _run(seed=42)
    assert _fingerprint(world_a, log_a) == _fingerprint(world_b, log_b)


def test_different_seed_produces_different_evolution() -> None:
    world_a, log_a = _run(seed=42)
    world_b, log_b = _run(seed=99)
    assert _fingerprint(world_a, log_a) != _fingerprint(world_b, log_b)


def test_step_advances_exactly_one_tick() -> None:
    world = VirtualWorld(WorldConfig(tick_hz=20.0, seed=1))
    assert world.tick == 0
    world.step()
    assert world.tick == 1
    world.step()
    assert world.tick == 2


def test_simulated_clock_is_derived_purely_from_tick_count() -> None:
    world = VirtualWorld(WorldConfig(tick_hz=20.0, seed=1))
    assert world.now == LAB_WORLD_EPOCH
    for _ in range(20):
        world.step()
    # 20 ticks at 20 Hz = exactly 1 simulated second, regardless of how long this
    # test actually took to run on the host machine.
    assert (world.now - LAB_WORLD_EPOCH).total_seconds() == 1.0
    assert world.now.tzinfo is UTC


def test_carried_item_tracks_its_carrier_every_tick() -> None:
    world = VirtualWorld(WorldConfig(tick_hz=20.0, seed=3))
    log = GroundTruthLog()
    shopper = interactions.enter(world, log, ENTRY_POINT, "GT-PERSON-001")
    interactions.pick(world, log, EPCS_RACK_A[0], shopper.ground_truth_person_id)
    actors.waypoint_walk(shopper, [RACK_B_APPROACH, RACK_C_APPROACH])
    for _ in range(200):
        world.step()
        item = world.items[EPCS_RACK_A[0]]
        # The item's position must always equal carrier position + carry offset;
        # it is derived, never an independent variable, while carried.
        assert abs(item.position.x - (shopper.position.x + 0.25)) < 1e-9
        assert abs(item.position.y - shopper.position.y) < 1e-9
