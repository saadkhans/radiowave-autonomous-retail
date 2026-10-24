"""Deterministic shopper motion primitives for the virtual store lab.

Everything here is a *pure mutation* of a ``ShopperActorState`` already held
by a ``VirtualWorld``: build a planned path, set a speed, flip a motion
state. Actual position integration happens once per tick inside
``VirtualWorld.step()`` -- this module never advances the clock or moves an
actor itself, so a scripted plan is fully determined by the scenario (which
waypoints, which fixtures, which speeds) and the world's fixed timestep, not
by wall-clock timing or game physics.

``ground_truth_person_id`` values handed to these functions (e.g.
``"GT-PERSON-001"``) are simulator-private provenance for later evaluation.
They must never be surfaced to, or used as, canonical track identity --
Foundation independently derives P0001/P0002/P0003 from noisy sensor
evidence, never from this label.
"""

from __future__ import annotations

from radiowave.contracts.geometry import WorldCoordinate
from radiowave.simulator.lab.world import ActorMotionState, ShopperActorState, VirtualWorld

#: Default deterministic walking speed for scripted shoppers (m/s).
DEFAULT_SPEED_MPS = 1.2


def _next_person_id(world: VirtualWorld) -> str:
    """Deterministic default id: GT-PERSON-001, -002, ... in spawn order."""
    return f"GT-PERSON-{len(world.shoppers) + 1:03d}"


def jitter(
    world: VirtualWorld, point: tuple[float, float], sigma: float = 0.03
) -> tuple[float, float]:
    """Draw a small, seed-determined offset from ``world.rng`` and apply it to ``point``.

    This is how a scripted path becomes "fully determined by scenario + seed"
    rather than exactly repeating the same waypoint every run: callers that
    want natural variation (e.g. slightly different standing spots in front
    of a fixture) draw it from the world's single seeded generator, so the
    same seed always reproduces the same jitter and a different seed always
    reproduces a different (but still deterministic) one.
    """
    dx, dy = world.rng.normal(0.0, sigma, size=2)
    return (point[0] + float(dx), point[1] + float(dy))


def enter(
    world: VirtualWorld,
    entry_point: tuple[float, float],
    ground_truth_person_id: str | None = None,
    speed_mps: float = DEFAULT_SPEED_MPS,
) -> ShopperActorState:
    """Spawn a shopper at ``entry_point`` and mark it present, walking-ready.

    This only creates physical presence in the world; it does not itself
    imply an ENTER ground-truth event -- that is recorded by
    ``interactions.enter`` when the caller wants both effects together.
    """
    person_id = ground_truth_person_id or _next_person_id(world)
    actor = world.add_shopper(person_id, WorldCoordinate(x=entry_point[0], y=entry_point[1]))
    actor.speed_mps = speed_mps
    actor.state = ActorMotionState.ENTERING
    return actor


def waypoint_walk(
    actor: ShopperActorState,
    waypoints: list[tuple[float, float]],
    speed_mps: float = DEFAULT_SPEED_MPS,
) -> None:
    """Walk a fully scripted polyline. Deterministic: same waypoints -> same path every run."""
    actor.planned_path = tuple(WorldCoordinate(x=x, y=y) for x, y in waypoints)
    actor.path_index = 0
    actor.speed_mps = speed_mps
    actor.state = ActorMotionState.WALKING


def hold_velocity(actor: ShopperActorState, target: tuple[float, float], speed_mps: float) -> None:
    """Constant-velocity straight-line walk to a single target point."""
    waypoint_walk(actor, [target], speed_mps)


def stop(actor: ShopperActorState) -> None:
    """Cancel any planned path; the actor holds its current position."""
    actor.planned_path = ()
    actor.path_index = 0
    actor.speed_mps = 0.0
    actor.state = ActorMotionState.STOPPED


def turn(actor: ShopperActorState, heading: float) -> None:
    """Instantaneous, deterministic reorientation without translation (no path change)."""
    actor.heading = heading
    actor.state = ActorMotionState.TURNING


def approach_fixture(
    world: VirtualWorld,
    actor: ShopperActorState,
    fixture_id: str,
    approach_point: tuple[float, float] | None = None,
    speed_mps: float = DEFAULT_SPEED_MPS,
) -> None:
    """Walk to a point in front of ``fixture_id``.

    Callers should normally pass the store's named ``*_APPROACH`` constant
    (see ``lab/store.py``) so the stopping point is a realistic standing
    distance from the fixture rather than its exact center.
    """
    if approach_point is None:
        fixture = next(f for f in world.store.fixtures if f.fixture_id == fixture_id)
        approach_point = (fixture.center.x, fixture.center.y)
    waypoint_walk(actor, [approach_point], speed_mps)
    actor.state = ActorMotionState.APPROACHING


def leave_fixture(
    actor: ShopperActorState, target: tuple[float, float], speed_mps: float = DEFAULT_SPEED_MPS
) -> None:
    """Walk away from whatever fixture the actor is currently standing at."""
    waypoint_walk(actor, [target], speed_mps)


def exit_store(
    actor: ShopperActorState, exit_point: tuple[float, float], speed_mps: float = DEFAULT_SPEED_MPS
) -> None:
    """Walk to ``exit_point``; ``VirtualWorld.step()`` marks the actor departed on arrival."""
    waypoint_walk(actor, [exit_point], speed_mps)
    actor.state = ActorMotionState.EXITING
