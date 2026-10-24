"""Deterministic fixed-timestep world for the Phase-4 virtual store lab.

``VirtualWorld`` is the physical substrate everything else in ``lab/`` sits on
top of: a tick-driven clock, a table of shopper actor states, and a table of
item states. It is intentionally dumb -- ``step()`` only advances the clock,
moves actors along whatever path they have been given (see ``actors.py``),
and keeps carried items glued to their carrier. It knows nothing about
"picking" or "retail events"; those live in ``interactions.py``.

Determinism contract
---------------------
* Simulated time is derived *only* from ``tick`` (``LAB_WORLD_EPOCH + tick /
  tick_hz``). Nothing in this module calls ``datetime.now()`` or
  ``time.time()``. Two ``VirtualWorld`` instances built with the same
  ``WorldConfig`` and driven through the same sequence of calls produce
  byte-identical state, on any machine, at any wall-clock time.
* The only source of randomness is ``VirtualWorld.rng``, seeded from
  ``WorldConfig.seed``. Callers that want noise (e.g. jitter in a scripted
  path) must draw from this generator, never from an unseeded global.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import numpy as np

from radiowave.contracts.geometry import Vector3D, Velocity, WorldCoordinate
from radiowave.contracts.store import Store
from radiowave.simulator.lab.store import build_virtual_lab_store

#: Epoch for the virtual lab world, analogous to SCENARIO_EPOCH in
#: radiowave/simulator/scenario.py. Arbitrary but fixed so timestamps are
#: reproducible and always timezone-aware UTC.
LAB_WORLD_EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)

#: Horizontal offset of a carried item from its carrier's tracked body point
#: (meters), mirroring scenario.py's CARRY_OFFSET. Applied every tick so a
#: carried item's position is *continuously* derived from its carrier rather
#: than snapshotted once at pick time.
CARRY_OFFSET = (0.25, 0.0)


class ActorMotionState(StrEnum):
    """Vocabulary for ShopperActorState.state. Simulator-internal, not a wire contract."""

    OFFSTAGE = "OFFSTAGE"
    ENTERING = "ENTERING"
    WALKING = "WALKING"
    APPROACHING = "APPROACHING"
    STOPPED = "STOPPED"
    TURNING = "TURNING"
    EXITING = "EXITING"
    EXITED = "EXITED"


@dataclass
class ShopperActorState:
    """Mutable per-tick truth of one simulated shopper.

    ``ground_truth_person_id`` is simulator-private provenance (e.g.
    ``"GT-PERSON-001"``). It must never be used as, or leak into, canonical
    track identity -- Foundation independently derives P0001/P0002/... from
    sensor evidence. Nothing downstream of the sensor generators may read
    this field.
    """

    ground_truth_person_id: str
    position: WorldCoordinate
    velocity: Velocity = field(default_factory=Velocity)
    heading: float = 0.0
    state: ActorMotionState = ActorMotionState.OFFSTAGE
    carried_epcs: tuple[str, ...] = field(default_factory=tuple)
    present: bool = False

    # --- deterministic path-following state, driven by actors.py ----------
    planned_path: tuple[WorldCoordinate, ...] = field(default_factory=tuple)
    path_index: int = 0
    speed_mps: float = 0.0


@dataclass
class ItemState:
    """Mutable per-tick physical truth of one item.

    ``carrier_id`` is ``None`` while the item rests on ``current_fixture_id``;
    once carried, position is derived from the carrier every tick (see
    ``step()``) rather than being an independent variable.
    """

    epc: str
    gtin: str
    home_fixture_id: str | None
    position: WorldCoordinate
    current_fixture_id: str | None
    carrier_id: str | None = None


@dataclass
class WorldConfig:
    """Fixed-timestep configuration. ``tick_hz`` and ``seed`` fully determine cadence/noise."""

    tick_hz: float = 20.0
    seed: int = 7

    @property
    def dt(self) -> float:
        """Seconds advanced per ``step()`` call."""
        return 1.0 / self.tick_hz


class VirtualWorld:
    """Deterministic fixed-timestep simulation of the Phase-4 lab store."""

    def __init__(self, config: WorldConfig | None = None, store: Store | None = None) -> None:
        self.config = config or WorldConfig()
        self.store = store or build_virtual_lab_store()
        self.tick: int = 0
        # Single seeded RNG for the whole world; every consumer of randomness
        # (actors, generators built on top of this world) must draw from it
        # so a given seed reproduces the entire run deterministically.
        self.rng: np.random.Generator = np.random.default_rng(self.config.seed)
        self.shoppers: dict[str, ShopperActorState] = {}
        self.items: dict[str, ItemState] = self._initial_item_states()

    def _initial_item_states(self) -> dict[str, ItemState]:
        fixtures_by_id = {f.fixture_id: f for f in self.store.fixtures}
        states: dict[str, ItemState] = {}
        for item in self.store.items:
            fixture_id = item.home_fixture_id
            fixture = fixtures_by_id[fixture_id] if fixture_id else None
            position = fixture.center if fixture is not None else WorldCoordinate(x=0.0, y=0.0)
            states[item.epc.value] = ItemState(
                epc=item.epc.value,
                gtin=item.gtin,
                home_fixture_id=fixture_id,
                position=position,
                current_fixture_id=fixture_id,
            )
        return states

    @property
    def now(self) -> datetime:
        """Simulated wall-clock time, derived only from the tick count."""
        return LAB_WORLD_EPOCH + timedelta(seconds=self.tick * self.config.dt)

    def add_shopper(
        self, ground_truth_person_id: str, position: WorldCoordinate
    ) -> ShopperActorState:
        if ground_truth_person_id in self.shoppers:
            msg = f"shopper {ground_truth_person_id} already exists in this world"
            raise ValueError(msg)
        actor = ShopperActorState(
            ground_truth_person_id=ground_truth_person_id,
            position=position,
            present=True,
            state=ActorMotionState.ENTERING,
        )
        self.shoppers[ground_truth_person_id] = actor
        return actor

    def step(self) -> None:
        """Advance exactly one tick: move actors along their planned paths, then
        re-derive carried item positions from their carriers. Deterministic given
        the current state -- no randomness, no wall-clock reads."""
        dt = self.config.dt
        for actor in self.shoppers.values():
            if actor.present:
                _advance_along_path(actor, dt)
        for item in self.items.values():
            if item.carrier_id is not None:
                carrier = self.shoppers[item.carrier_id]
                item.position = carrier.position.displaced(
                    Vector3D(x=CARRY_OFFSET[0], y=CARRY_OFFSET[1], z=0.0)
                )
        self.tick += 1


def _advance_along_path(actor: ShopperActorState, dt: float) -> None:
    """Move ``actor`` toward the next waypoint(s) in its planned path by ``speed_mps * dt``.

    Handles the case where the per-tick travel budget overshoots the next
    waypoint (e.g. a low ``tick_hz`` with a fast shopper) by consuming the
    remaining budget against subsequent waypoints, so motion stays on the
    scripted polyline regardless of timestep size.
    """
    if not actor.planned_path or actor.path_index >= len(actor.planned_path):
        actor.velocity = Velocity()
        return

    budget = actor.speed_mps * dt
    position = actor.position
    while budget > 0.0 and actor.path_index < len(actor.planned_path):
        target = actor.planned_path[actor.path_index]
        dx, dy = target.x - position.x, target.y - position.y
        distance = (dx**2 + dy**2) ** 0.5
        if distance <= budget or distance == 0.0:
            position = target
            budget -= distance
            actor.path_index += 1
        else:
            fraction = budget / distance
            position = WorldCoordinate(x=position.x + dx * fraction, y=position.y + dy * fraction)
            budget = 0.0

    if actor.speed_mps > 0.0:
        dx, dy = position.x - actor.position.x, position.y - actor.position.y
        actor.velocity = Velocity(vx=dx / dt, vy=dy / dt)
        if dx != 0.0 or dy != 0.0:
            actor.heading = math.atan2(dy, dx)
    actor.position = position

    if actor.path_index >= len(actor.planned_path):
        actor.velocity = Velocity()
        if actor.state in (
            ActorMotionState.WALKING,
            ActorMotionState.ENTERING,
            ActorMotionState.APPROACHING,
        ):
            actor.state = ActorMotionState.STOPPED
        elif actor.state == ActorMotionState.EXITING:
            actor.state = ActorMotionState.EXITED
            actor.present = False
