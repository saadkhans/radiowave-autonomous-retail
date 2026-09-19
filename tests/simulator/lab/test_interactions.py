"""Ground-truth action tests: PICK/PUTBACK/MISPLACE/HANDOFF mutate physical state
correctly, and a carried item's position tracks its carrier."""

from __future__ import annotations

import pytest

from radiowave.simulator.lab import interactions
from radiowave.simulator.lab.ground_truth import GroundTruthEventType, GroundTruthLog
from radiowave.simulator.lab.store import (
    ENTRY_POINT,
    EPCS_RACK_A,
    EPCS_RACK_B,
    RACK_A,
    RACK_B,
)
from radiowave.simulator.lab.world import VirtualWorld, WorldConfig


def _world_with_shopper(
    person_id: str = "GT-PERSON-001",
) -> tuple[VirtualWorld, GroundTruthLog, str]:
    world = VirtualWorld(WorldConfig(seed=5))
    log = GroundTruthLog()
    interactions.enter(world, log, ENTRY_POINT, person_id)
    return world, log, person_id


def test_pick_attaches_item_to_shopper_and_detaches_from_fixture() -> None:
    world, log, person_id = _world_with_shopper()
    epc = EPCS_RACK_A[0]
    home_fixture_position = world.items[epc].position

    interactions.pick(world, log, epc, person_id)

    item = world.items[epc]
    shopper = world.shoppers[person_id]
    assert item.carrier_id == person_id
    assert item.current_fixture_id is None
    assert epc in shopper.carried_epcs
    # It moved off the fixture onto the shopper's carry offset immediately.
    assert item.position != home_fixture_position
    assert log.events_of(GroundTruthEventType.PICK)[-1].epc == epc


def test_pick_twice_without_putback_raises() -> None:
    world, log, person_id = _world_with_shopper()
    epc = EPCS_RACK_A[0]
    interactions.pick(world, log, epc, person_id)
    with pytest.raises(ValueError):
        interactions.pick(world, log, epc, person_id)


def test_carried_item_position_tracks_carrier_across_ticks() -> None:
    world, log, person_id = _world_with_shopper()
    epc = EPCS_RACK_A[0]
    interactions.pick(world, log, epc, person_id)
    shopper = world.shoppers[person_id]

    from radiowave.simulator.lab import actors

    actors.waypoint_walk(shopper, [(5.0, 4.5)])
    for _ in range(50):
        world.step()

    item = world.items[epc]
    assert abs(item.position.x - (shopper.position.x + 0.25)) < 1e-9
    assert abs(item.position.y - shopper.position.y) < 1e-9


def test_putback_detaches_item_and_rests_it_on_home_fixture_by_default() -> None:
    world, log, person_id = _world_with_shopper()
    epc = EPCS_RACK_A[0]
    interactions.pick(world, log, epc, person_id)

    interactions.putback(world, log, epc)

    item = world.items[epc]
    shopper = world.shoppers[person_id]
    assert item.carrier_id is None
    assert item.current_fixture_id == RACK_A
    assert epc not in shopper.carried_epcs
    assert log.events_of(GroundTruthEventType.PUTBACK)[-1].fixture_id == RACK_A


def test_putback_without_carrying_raises() -> None:
    world, log, _person_id = _world_with_shopper()
    with pytest.raises(ValueError):
        interactions.putback(world, log, EPCS_RACK_A[0])


def test_misplace_rests_item_on_a_different_fixture_than_home() -> None:
    world, log, person_id = _world_with_shopper()
    epc = EPCS_RACK_A[0]
    interactions.pick(world, log, epc, person_id)

    interactions.misplace(world, log, epc, RACK_B)

    item = world.items[epc]
    assert item.carrier_id is None
    assert item.current_fixture_id == RACK_B
    assert item.home_fixture_id == RACK_A
    assert log.events_of(GroundTruthEventType.MISPLACE)[-1].fixture_id == RACK_B


def test_misplace_onto_home_fixture_is_rejected() -> None:
    world, log, person_id = _world_with_shopper()
    epc = EPCS_RACK_A[0]
    interactions.pick(world, log, epc, person_id)
    with pytest.raises(ValueError):
        interactions.misplace(world, log, epc, RACK_A)


def test_handoff_transfers_carrier_and_carried_epcs() -> None:
    world = VirtualWorld(WorldConfig(seed=5))
    log = GroundTruthLog()
    giver = interactions.enter(world, log, ENTRY_POINT, "GT-PERSON-001")
    receiver = interactions.enter(world, log, (1.0, 3.5), "GT-PERSON-002")
    epc = EPCS_RACK_B[0]
    interactions.pick(world, log, epc, giver.ground_truth_person_id)

    interactions.handoff(world, log, epc, receiver.ground_truth_person_id)

    item = world.items[epc]
    assert item.carrier_id == receiver.ground_truth_person_id
    assert epc not in giver.carried_epcs
    assert epc in receiver.carried_epcs
    handoff_event = log.events_of(GroundTruthEventType.HANDOFF)[-1]
    assert handoff_event.ground_truth_person_id == receiver.ground_truth_person_id
    assert handoff_event.counterpart_person_id == giver.ground_truth_person_id
    # After handoff, the item must track the *new* carrier, not the old one.
    assert abs(item.position.x - (receiver.position.x + 0.25)) < 1e-9


def test_handoff_without_carrying_raises() -> None:
    world = VirtualWorld(WorldConfig(seed=5))
    log = GroundTruthLog()
    # The giver is placed in the world for realism but never bound: this test is
    # about the receiver having nothing handed to it.
    interactions.enter(world, log, ENTRY_POINT, "GT-PERSON-001")
    receiver = interactions.enter(world, log, (1.0, 3.5), "GT-PERSON-002")
    with pytest.raises(ValueError):
        interactions.handoff(world, log, EPCS_RACK_B[0], receiver.ground_truth_person_id)


def test_exit_logs_exit_with_item_for_every_carried_epc() -> None:
    world, log, person_id = _world_with_shopper()
    epc_a, epc_b = EPCS_RACK_A[0], EPCS_RACK_A[1]
    interactions.pick(world, log, epc_a, person_id)
    interactions.pick(world, log, epc_b, person_id)

    interactions.exit_store(world, log, person_id, (7.3, 3.0))

    exit_with_item_epcs = {e.epc for e in log.events_of(GroundTruthEventType.EXIT_WITH_ITEM)}
    assert exit_with_item_epcs == {epc_a, epc_b}
    assert len(log.events_of(GroundTruthEventType.EXIT)) == 1


def test_ground_truth_log_never_imports_pipeline_or_fusion() -> None:
    """The log must be structurally unreachable from the pipeline: no fusion import here."""
    import ast

    import radiowave.simulator.lab.ground_truth as gt_module

    source = gt_module.__file__
    with open(source, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=source)

    forbidden_prefixes = ("radiowave.pipeline", "radiowave.fusion", "radiowave.adapters")
    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    for module_name in imported_modules:
        assert not module_name.startswith(forbidden_prefixes), (
            f"ground_truth.py must never import {module_name}; it would make the "
            "ground-truth log reachable from fusion"
        )
