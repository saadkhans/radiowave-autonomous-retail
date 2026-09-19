"""Ground-truth actions: ENTER, APPROACH_FIXTURE, PICK, CARRY, PUTBACK, MISPLACE,
HANDOFF, EXIT.

Every function here does exactly two things, in order:

1. Mutate PHYSICAL state on a ``VirtualWorld`` (attach/detach an item from a
   shopper or fixture, move a shopper's plan, etc).
2. Record what just happened into a ``GroundTruthLog`` -- for later
   evaluation only.

CRITICAL: this module never emits anything into ``radiowave.pipeline`` or
``radiowave.fusion``. Foundation must infer PICK/PUTBACK/etc from noisy
sensor observations generated from the physical state these functions
produce; it never sees the ground-truth log these functions write to. See
``ground_truth.py`` for why that log is structurally a dead end for fusion.
"""

from __future__ import annotations

from radiowave.contracts.geometry import Vector3D, WorldCoordinate
from radiowave.simulator.lab import actors
from radiowave.simulator.lab.ground_truth import GroundTruthEventType, GroundTruthLog
from radiowave.simulator.lab.world import CARRY_OFFSET, ShopperActorState, VirtualWorld


def _carry_position(world: VirtualWorld, shopper: ShopperActorState) -> WorldCoordinate:
    """Where a carried item rides relative to its carrier.

    ``world`` is unused today but kept so every helper in this module takes the same
    (world, ...) shape as the public interactions; a carry offset that later depends on
    store geometry or tick rate would need it.
    """
    return shopper.position.displaced(Vector3D(x=CARRY_OFFSET[0], y=CARRY_OFFSET[1], z=0.0))


def enter(
    world: VirtualWorld,
    log: GroundTruthLog,
    entry_point: tuple[float, float],
    ground_truth_person_id: str | None = None,
    speed_mps: float = actors.DEFAULT_SPEED_MPS,
) -> ShopperActorState:
    """Physically spawn a shopper at the entry, and record the ENTER event."""
    actor = actors.enter(world, entry_point, ground_truth_person_id, speed_mps)
    log.record_event(
        world, GroundTruthEventType.ENTER, ground_truth_person_id=actor.ground_truth_person_id
    )
    return actor


def approach_fixture(
    world: VirtualWorld,
    log: GroundTruthLog,
    ground_truth_person_id: str,
    fixture_id: str,
    approach_point: tuple[float, float] | None = None,
    speed_mps: float = actors.DEFAULT_SPEED_MPS,
) -> None:
    """Physically plan a walk to ``fixture_id``, and record the APPROACH_FIXTURE event."""
    actor = world.shoppers[ground_truth_person_id]
    actors.approach_fixture(world, actor, fixture_id, approach_point, speed_mps)
    log.record_event(
        world,
        GroundTruthEventType.APPROACH_FIXTURE,
        ground_truth_person_id=ground_truth_person_id,
        fixture_id=fixture_id,
    )


def pick(world: VirtualWorld, log: GroundTruthLog, epc: str, ground_truth_person_id: str) -> None:
    """Detach an item from its fixture and attach it to a shopper.

    The item's position becomes derived from the carrier (see
    ``VirtualWorld.step``); it stops resting on any fixture.
    """
    item = world.items[epc]
    if item.carrier_id is not None:
        msg = f"item {epc} is already carried by {item.carrier_id}, cannot pick again"
        raise ValueError(msg)
    shopper = world.shoppers[ground_truth_person_id]
    item.carrier_id = ground_truth_person_id
    item.current_fixture_id = None
    item.position = _carry_position(world, shopper)
    shopper.carried_epcs = (*shopper.carried_epcs, epc)
    log.record_event(
        world, GroundTruthEventType.PICK, ground_truth_person_id=ground_truth_person_id, epc=epc
    )


def carry(world: VirtualWorld, log: GroundTruthLog, epc: str) -> None:
    """Record an ongoing-carry sample for an already-picked item (no state change)."""
    item = world.items[epc]
    if item.carrier_id is None:
        msg = f"item {epc} is not currently carried; call pick() first"
        raise ValueError(msg)
    log.record_event(
        world, GroundTruthEventType.CARRY, ground_truth_person_id=item.carrier_id, epc=epc
    )


def putback(
    world: VirtualWorld, log: GroundTruthLog, epc: str, fixture_id: str | None = None
) -> None:
    """Detach a carried item from its shopper and rest it on a fixture (default: its home)."""
    item = world.items[epc]
    if item.carrier_id is None:
        msg = f"item {epc} is not carried, nothing to put back"
        raise ValueError(msg)
    shopper_id = item.carrier_id
    target_fixture_id = fixture_id or item.home_fixture_id
    if target_fixture_id is None:
        msg = f"item {epc} has no home fixture and no fixture_id was given"
        raise ValueError(msg)
    fixture = next(f for f in world.store.fixtures if f.fixture_id == target_fixture_id)
    item.carrier_id = None
    item.current_fixture_id = target_fixture_id
    item.position = fixture.center
    shopper = world.shoppers[shopper_id]
    shopper.carried_epcs = tuple(e for e in shopper.carried_epcs if e != epc)
    log.record_event(
        world,
        GroundTruthEventType.PUTBACK,
        ground_truth_person_id=shopper_id,
        epc=epc,
        fixture_id=target_fixture_id,
    )


def misplace(world: VirtualWorld, log: GroundTruthLog, epc: str, wrong_fixture_id: str) -> None:
    """Like ``putback``, but onto a fixture that is NOT the item's home (a true misplace)."""
    item = world.items[epc]
    if wrong_fixture_id == item.home_fixture_id:
        msg = (
            f"misplace target {wrong_fixture_id} equals {epc}'s home fixture; "
            "use putback() for a correct return"
        )
        raise ValueError(msg)
    if item.carrier_id is None:
        msg = f"item {epc} is not carried, nothing to misplace"
        raise ValueError(msg)
    shopper_id = item.carrier_id
    fixture = next(f for f in world.store.fixtures if f.fixture_id == wrong_fixture_id)
    item.carrier_id = None
    item.current_fixture_id = wrong_fixture_id
    item.position = fixture.center
    shopper = world.shoppers[shopper_id]
    shopper.carried_epcs = tuple(e for e in shopper.carried_epcs if e != epc)
    log.record_event(
        world,
        GroundTruthEventType.MISPLACE,
        ground_truth_person_id=shopper_id,
        epc=epc,
        fixture_id=wrong_fixture_id,
    )


def handoff(
    world: VirtualWorld, log: GroundTruthLog, epc: str, to_ground_truth_person_id: str
) -> None:
    """Transfer carry of an item from its current shopper to another, mid-floor."""
    item = world.items[epc]
    from_person_id = item.carrier_id
    if from_person_id is None:
        msg = f"item {epc} is not carried, nothing to hand off"
        raise ValueError(msg)
    if to_ground_truth_person_id == from_person_id:
        msg = f"cannot hand off {epc} from {from_person_id} to itself"
        raise ValueError(msg)
    from_shopper = world.shoppers[from_person_id]
    to_shopper = world.shoppers[to_ground_truth_person_id]
    item.carrier_id = to_ground_truth_person_id
    item.position = _carry_position(world, to_shopper)
    from_shopper.carried_epcs = tuple(e for e in from_shopper.carried_epcs if e != epc)
    to_shopper.carried_epcs = (*to_shopper.carried_epcs, epc)
    log.record_event(
        world,
        GroundTruthEventType.HANDOFF,
        ground_truth_person_id=to_ground_truth_person_id,
        counterpart_person_id=from_person_id,
        epc=epc,
    )


def exit_store(
    world: VirtualWorld,
    log: GroundTruthLog,
    ground_truth_person_id: str,
    exit_point: tuple[float, float],
    speed_mps: float = actors.DEFAULT_SPEED_MPS,
) -> None:
    """Physically plan a walk to the exit, and record EXIT plus EXIT_WITH_ITEM per carried item.

    ``VirtualWorld.step()`` marks the actor departed (``present = False``)
    once it physically arrives at ``exit_point``; this call only schedules
    the walk and logs the ground-truth intent/evidence at the moment of
    departure being triggered.
    """
    actor = world.shoppers[ground_truth_person_id]
    carried = actor.carried_epcs
    actors.exit_store(actor, exit_point, speed_mps)
    log.record_event(
        world, GroundTruthEventType.EXIT, ground_truth_person_id=ground_truth_person_id
    )
    for epc in carried:
        log.record_event(
            world,
            GroundTruthEventType.EXIT_WITH_ITEM,
            ground_truth_person_id=ground_truth_person_id,
            epc=epc,
        )


__all__ = [
    "approach_fixture",
    "carry",
    "enter",
    "exit_store",
    "handoff",
    "misplace",
    "pick",
    "putback",
]
