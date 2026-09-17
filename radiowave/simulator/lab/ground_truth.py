"""The private ground-truth log. NOT FOR FUSION.

================================================================================
THIS MODULE IS A SINK, NEVER A SOURCE. Nothing in ``radiowave.pipeline``,
``radiowave.fusion``, ``radiowave.adapters``, or ``radiowave.digital_twin``
imports it, and it must stay that way. ``GroundTruthLog`` exists solely so
test/evaluation code can compare what fusion *inferred* against what
physically happened in the simulator. If the pipeline could read this log we
would be grading an answer key instead of testing the inference algorithm.
================================================================================

``interactions.py`` is the only writer (it mutates physical world state and
records what it just did here, in the same call). Test and evaluation code
is the only reader. There is no contract, adapter, or observation type that
carries a ``GroundTruthEvent`` -- on purpose.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from radiowave.contracts.geometry import Velocity, WorldCoordinate
from radiowave.simulator.lab.world import VirtualWorld


class GroundTruthEventType(StrEnum):
    """Vocabulary for GroundTruthLog entries.

    Deliberately a separate enum from ``radiowave.contracts.events.RetailEventType``:
    this one describes what the simulator *did* to physical state (including
    ENTER/APPROACH_FIXTURE, which fusion never emits as retail events), not
    what fusion *inferred* from noisy sensor evidence.
    """

    ENTER = "ENTER"
    APPROACH_FIXTURE = "APPROACH_FIXTURE"
    PICK = "PICK"
    CARRY = "CARRY"
    PUTBACK = "PUTBACK"
    MISPLACE = "MISPLACE"
    HANDOFF = "HANDOFF"
    EXIT = "EXIT"
    EXIT_WITH_ITEM = "EXIT_WITH_ITEM"


@dataclass(frozen=True)
class GroundTruthEvent:
    """One physical happening, simulated-time stamped. Simulator-private ids only."""

    tick: int
    timestamp: datetime
    event_type: GroundTruthEventType
    ground_truth_person_id: str | None = None
    counterpart_person_id: str | None = None
    epc: str | None = None
    fixture_id: str | None = None


@dataclass(frozen=True)
class TrajectorySample:
    tick: int
    timestamp: datetime
    position: WorldCoordinate
    velocity: Velocity = field(default_factory=Velocity)


@dataclass(frozen=True)
class ItemTrajectorySample:
    tick: int
    timestamp: datetime
    position: WorldCoordinate
    holder: str | None  # ground_truth_person_id currently carrying, or None if resting on a fixture


@dataclass
class GroundTruthLog:
    """Append-only record of what truly happened in one ``VirtualWorld`` run."""

    events: list[GroundTruthEvent] = field(default_factory=list)
    shopper_trajectories: dict[str, list[TrajectorySample]] = field(default_factory=dict)
    item_trajectories: dict[str, list[ItemTrajectorySample]] = field(default_factory=dict)

    def record_event(
        self,
        world: VirtualWorld,
        event_type: GroundTruthEventType,
        *,
        ground_truth_person_id: str | None = None,
        counterpart_person_id: str | None = None,
        epc: str | None = None,
        fixture_id: str | None = None,
    ) -> GroundTruthEvent:
        event = GroundTruthEvent(
            tick=world.tick,
            timestamp=world.now,
            event_type=event_type,
            ground_truth_person_id=ground_truth_person_id,
            counterpart_person_id=counterpart_person_id,
            epc=epc,
            fixture_id=fixture_id,
        )
        self.events.append(event)
        return event

    def snapshot(self, world: VirtualWorld) -> None:
        """Sample every present shopper's and every item's true state at the current tick.

        Call once per tick from the test/scenario driver -- never from
        ``VirtualWorld`` itself, which must stay ignorant of this log -- to
        build continuous ground-truth trajectories alongside the discrete
        events recorded by ``interactions.py``.
        """
        for person_id, actor in world.shoppers.items():
            if not actor.present:
                continue
            self.shopper_trajectories.setdefault(person_id, []).append(
                TrajectorySample(
                    tick=world.tick,
                    timestamp=world.now,
                    position=actor.position,
                    velocity=actor.velocity,
                )
            )
        for epc, item in world.items.items():
            self.item_trajectories.setdefault(epc, []).append(
                ItemTrajectorySample(
                    tick=world.tick,
                    timestamp=world.now,
                    position=item.position,
                    holder=item.carrier_id,
                )
            )

    def events_of(self, event_type: GroundTruthEventType) -> list[GroundTruthEvent]:
        return [e for e in self.events if e.event_type == event_type]
