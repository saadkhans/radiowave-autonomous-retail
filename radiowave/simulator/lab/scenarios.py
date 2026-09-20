"""Declarative Phase-4 lab scenarios: pure data describing what a run should do.

This module deliberately mirrors two conventions from the Foundation v0 simulator:
:mod:`radiowave.simulator.scenario` (the Pydantic scenario model / ``model_validator``
style) and :mod:`radiowave.simulator.library` (the factory-function + ``SCENARIOS``
dict + ``load_scenario`` catalog pattern). Read both before changing this file.

Scope boundary -- read carefully
---------------------------------
This module builds the declarative scenario *data model* and the *catalog* of
scenario definitions for the Phase-4 virtual store lab. It does NOT build the
execution engine that wires a :class:`~radiowave.simulator.lab.world.VirtualWorld`
to sensor emulators and the pipeline -- that is owned separately. Every model here
is pure, side-effect-free data:

* Constructing a :class:`LabScenario` never touches ``VirtualWorld``, any sensor
  emulator, or the pipeline. There is no ``run()`` method on anything in this file.
* Scheduled interactions name an :class:`InteractionVerb` (``ENTER``,
  ``APPROACH_FIXTURE``, ``PICK``, ``CARRY``, ``PUTBACK``, ``MISPLACE``, ``HANDOFF``,
  ``EXIT``) plus the shopper/EPC/fixture ids involved; they do not call
  ``radiowave.simulator.lab.interactions`` functions. An execution engine built on
  top of this data is expected to interpret each scheduled interaction against a
  live ``VirtualWorld`` by calling those functions itself.
* Sensor configuration is described as data: radar knobs reuse
  :class:`~radiowave.simulator.lab.sensors.ti_radar.TiRadarNoiseConfig` and
  :class:`~radiowave.simulator.lab.sensors.ti_radar.TiRadarFovConfig` (themselves
  plain, already-frozen dataclasses with no execution behaviour attached), while
  RFID reliability is described by this module's own
  :class:`RfidReliabilityConfig` rather than importing
  ``radiowave.simulator.lab.sensors.rfid`` -- that module is a concurrently
  developed execution component with its own API, and this file (and every
  scenario built from it) must stay importable and constructible independent of
  it.

Because everything here is inert data, invalid scenarios must fail loudly at
*construction* time (inside a ``model_validator``), the same discipline
``radiowave.simulator.scenario.Scenario`` uses: a scenario that passes validation
but describes something physically impossible (a pick of an already-carried item,
a handoff to someone not in the store, ...) would otherwise silently produce a
misleading run much later, when it is far harder to trace back to a scenario
authoring mistake.
"""

from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
from itertools import pairwise
from typing import Any

from pydantic import Field, model_validator

from radiowave.contracts._base import ContractModel, FrozenModel, UnitInterval
from radiowave.contracts.events import RetailEventType
from radiowave.contracts.store import SourceType, Store
from radiowave.simulator.lab.sensors.ti_radar import TiRadarFovConfig, TiRadarNoiseConfig
from radiowave.simulator.lab.store import (
    ENTRY_POINT,
    EPCS_RACK_A,
    EPCS_RACK_B,
    EPCS_RACK_C,
    EXIT_POINT,
    MMWAVE_SENSOR_ID,
    RACK_A,
    RACK_A_APPROACH,
    RACK_B,
    RACK_B_APPROACH,
    RACK_C,
    RACK_C_APPROACH,
    RFID_RACK_A_ID,
    build_virtual_lab_store,
)

#: Default deterministic walking speed (m/s), mirroring
#: ``radiowave.simulator.lab.actors.DEFAULT_SPEED_MPS``. Duplicated here (rather
#: than imported) because this module must stay import-independent of the
#: execution engine (``world.py`` / ``actors.py`` / ``interactions.py``) -- see the
#: module docstring.
DEFAULT_SPEED_MPS = 1.2

#: Wall-clock tolerance (seconds) used when comparing a scheduled interaction's
#: time against a shopper's scripted presence window. Scenario waypoints and
#: interaction schedules are independently authored floats; this absorbs benign
#: floating point drift without silently accepting a real timing mistake (an
#: interaction seconds outside a shopper's presence still fails).
_TIME_EPSILON = 1e-6

#: A central floor point away from every rack, reused by several catalog
#: scenarios below as a meeting/crossing point.
MID_FLOOR = (4.0, 3.0)


class InteractionVerb(StrEnum):
    """Ground-truth action vocabulary a scheduled interaction can name.

    Deliberately a separate enum from ``radiowave.contracts.events.RetailEventType``
    (the system-*inferred* outcome vocabulary fusion emits, e.g. no ``ENTER``) and
    from ``radiowave.simulator.lab.ground_truth.GroundTruthEventType`` (the
    execution engine's private append-only log): this one is what a *scenario
    author* schedules before any run happens, so it must not import, or be
    imported by, the execution engine -- see the module docstring.
    """

    ENTER = "ENTER"
    APPROACH_FIXTURE = "APPROACH_FIXTURE"
    PICK = "PICK"
    CARRY = "CARRY"
    PUTBACK = "PUTBACK"
    MISPLACE = "MISPLACE"
    HANDOFF = "HANDOFF"
    EXIT = "EXIT"


class FaultKind(StrEnum):
    """Vocabulary for scheduled sensor fault windows (see :class:`FaultInjection`)."""

    RADAR_DROPOUT = "RADAR_DROPOUT"
    RFID_DROPOUT = "RFID_DROPOUT"
    RADAR_RESTART = "RADAR_RESTART"
    NATIVE_ID_REUSE = "NATIVE_ID_REUSE"


_FAULT_MODALITY = {
    FaultKind.RADAR_DROPOUT: SourceType.MMWAVE,
    FaultKind.RADAR_RESTART: SourceType.MMWAVE,
    FaultKind.NATIVE_ID_REUSE: SourceType.MMWAVE,
    FaultKind.RFID_DROPOUT: SourceType.RFID,
}


class LabWaypoint(FrozenModel):
    """One (time, position) sample of a scripted path. Mirrors ``scenario.Waypoint``."""

    t: float = Field(ge=0.0, description="Seconds since scenario start")
    x: float
    y: float


class LabShopperScript(FrozenModel):
    """A ground-truth shopper's scripted presence: when, where, how fast.

    ``shopper_id`` is ground truth only (e.g. ``"GT-PERSON-001"``, matching
    ``radiowave.simulator.lab.actors``'s id convention) and must never be
    surfaced to, or used as, canonical track identity.
    """

    shopper_id: str = Field(min_length=1)
    entry_t: float = Field(ge=0.0, description="Seconds since scenario start this shopper spawns")
    waypoints: list[LabWaypoint] = Field(min_length=1)
    speed_mps: float = Field(gt=0.0, default=DEFAULT_SPEED_MPS)

    @model_validator(mode="after")
    def _time_ordered_and_starts_at_entry(self) -> LabShopperScript:
        times = [w.t for w in self.waypoints]
        if any(b < a for a, b in pairwise(times)):
            msg = f"waypoints for {self.shopper_id} must be time-ordered"
            raise ValueError(msg)
        if abs(self.waypoints[0].t - self.entry_t) > _TIME_EPSILON:
            msg = (
                f"{self.shopper_id}'s first waypoint ({self.waypoints[0].t} s) must equal "
                f"entry_t ({self.entry_t} s)"
            )
            raise ValueError(msg)
        return self

    @property
    def exit_t(self) -> float:
        """Last instant this shopper is scripted to be present."""
        return self.waypoints[-1].t

    def present_at(self, t: float) -> bool:
        return self.entry_t - _TIME_EPSILON <= t <= self.exit_t + _TIME_EPSILON


class ScheduledInteraction(FrozenModel):
    """One scripted action: ``shopper_id`` does ``verb`` to an EPC/fixture at ``t``.

    Pure data -- see the module docstring. Which fields are meaningful depends on
    ``verb``:

    * ``ENTER`` / ``EXIT``: only ``shopper_id`` and ``t`` matter.
    * ``APPROACH_FIXTURE``: needs ``fixture_id``.
    * ``PICK`` / ``CARRY``: needs ``epc`` (the fixture is whichever one the item is
      currently resting on; the scenario need not repeat it).
    * ``PUTBACK``: needs ``epc``; ``fixture_id`` defaults to the item's home
      fixture when omitted, mirroring ``interactions.putback``.
    * ``MISPLACE``: needs ``epc`` and a ``fixture_id`` that is NOT the item's home
      fixture.
    * ``HANDOFF``: needs ``epc`` and ``counterpart_shopper_id`` (the receiver);
      ``shopper_id`` is the giver, who must currently be carrying the item.
    """

    t: float = Field(ge=0.0)
    verb: InteractionVerb
    shopper_id: str = Field(min_length=1)
    epc: str | None = None
    fixture_id: str | None = None
    counterpart_shopper_id: str | None = None


class RfidReliabilityConfig(FrozenModel):
    """RFID reader reliability knobs, described as data only.

    Deliberately does NOT import ``radiowave.simulator.lab.sensors.rfid``: see the
    module docstring for why this model, and every scenario built from it, must
    stay constructible independent of that concurrently developed module.
    """

    read_probability: UnitInterval = Field(
        default=0.95,
        description="Probability a tag physically inside a read point's zone is read this cycle",
    )
    missed_read_rate: UnitInterval = Field(
        default=0.05,
        description="Rate of a genuinely present tag producing no read at all this cycle",
    )
    bleed_rate: UnitInterval = Field(
        default=0.02,
        description="Probability a read point reports a tag actually resting at a neighboring "
        "zone (antenna cross-talk / bleed)",
    )
    false_read_rate: UnitInterval = Field(
        default=0.0, description="Probability of a spurious read with no corresponding tag"
    )
    restart_time_s: float = Field(
        default=0.0,
        ge=0.0,
        description="Time a reader takes to resume reads after a restart/outage",
    )


class LabSensorConfig(FrozenModel):
    """Sensor settings for one lab run, referenced as data only -- see module docstring."""

    radar_noise: TiRadarNoiseConfig = Field(default_factory=TiRadarNoiseConfig)
    radar_fov: TiRadarFovConfig = Field(default_factory=TiRadarFovConfig)
    rfid: RfidReliabilityConfig = Field(default_factory=RfidReliabilityConfig)


class FaultInjection(FrozenModel):
    """A scheduled sensor fault window.

    Pure data: an execution engine decides how to realize each ``kind`` against
    the emulators it owns (e.g. suppressing RFID reads for the window, calling
    ``TiRadarEmulator.reset_stream`` at ``start_t``, forcing a native-id reuse
    between ``start_t`` and ``end_t``) -- this model only describes when and
    where, never how.
    """

    kind: FaultKind
    start_t: float = Field(ge=0.0)
    end_t: float = Field(ge=0.0)
    sensor_id: str | None = Field(
        default=None, description="Specific sensor affected; None means every sensor of this kind"
    )
    note: str = ""

    @model_validator(mode="after")
    def _ordered(self) -> FaultInjection:
        if self.end_t < self.start_t:
            msg = f"fault {self.kind} ends ({self.end_t} s) before it starts ({self.start_t} s)"
            raise ValueError(msg)
        return self


class ExpectedEvent(FrozenModel):
    """One retail event a correct fusion run should emit, for later evaluation.

    Uses ``RetailEventType`` (the system-inferred outcome vocabulary), not
    ``InteractionVerb`` (the scripted-cause vocabulary): this is the expected
    *effect* of the scheduled interactions above, not a restatement of them.
    """

    t: float = Field(ge=0.0)
    event_type: RetailEventType
    epc: str
    shopper_id: str | None = None
    counterpart_shopper_id: str | None = None


class ExpectedExit(FrozenModel):
    """What one shopper should be leaving the store with."""

    shopper_id: str = Field(min_length=1)
    epcs: tuple[str, ...] = Field(default_factory=tuple)


class ExpectedTruth(FrozenModel):
    """Ground-truth outcomes a correct run should produce.

    Kept separate from ``scheduled_interactions`` (the causes) so evaluation code
    can compare "what fusion inferred" against "what should have resulted"
    without re-deriving it from the schedule every time.
    """

    exits: tuple[ExpectedExit, ...] = Field(default_factory=tuple)
    events: tuple[ExpectedEvent, ...] = Field(default_factory=tuple)


class LabScenario(ContractModel):
    """A Phase-4 lab scenario: fully declarative, no execution coupling.

    Deterministic and serialisable, mirroring
    ``radiowave.simulator.scenario.Scenario`` at the top level: everything needed
    to run and evaluate a lab episode is data on this model. See the module
    docstring for the pure-data contract this type must uphold.
    """

    scenario_id: str = Field(min_length=1)
    name: str
    description: str = ""
    seed: int = 7
    duration_s: float = Field(gt=0.0)
    tick_hz: float = Field(gt=0.0, default=20.0)
    store: Store
    shoppers: list[LabShopperScript] = Field(default_factory=list)
    scheduled_interactions: list[ScheduledInteraction] = Field(default_factory=list)
    sensors: LabSensorConfig = Field(default_factory=LabSensorConfig)
    fault_injections: list[FaultInjection] = Field(default_factory=list)
    expected_truth: ExpectedTruth = Field(default_factory=ExpectedTruth)

    @model_validator(mode="after")
    def _validate(self) -> LabScenario:
        self._check_shoppers()
        self._check_faults()
        self._replay_schedule()
        self._check_expected_truth()
        return self

    # --- validation helpers -------------------------------------------------
    def _check_shoppers(self) -> None:
        ids = [s.shopper_id for s in self.shoppers]
        if len(ids) != len(set(ids)):
            msg = "shopper ids must be unique"
            raise ValueError(msg)
        for shopper in self.shoppers:
            if shopper.exit_t > self.duration_s:
                msg = (
                    f"{shopper.shopper_id} is scripted present until {shopper.exit_t} s, "
                    f"after the scenario ends ({self.duration_s} s)"
                )
                raise ValueError(msg)

    def _check_faults(self) -> None:
        sensor_modality = {s.sensor_id: s.modality for s in self.store.sensors}
        for fault in self.fault_injections:
            if fault.end_t > self.duration_s:
                msg = f"fault {fault.kind} ends ({fault.end_t} s) after the scenario ends"
                raise ValueError(msg)
            if fault.sensor_id is None:
                continue
            if fault.sensor_id not in sensor_modality:
                msg = f"fault references unknown sensor {fault.sensor_id}"
                raise ValueError(msg)
            expected_modality = _FAULT_MODALITY[fault.kind]
            if sensor_modality[fault.sensor_id] != expected_modality:
                msg = (
                    f"fault {fault.kind} sensor {fault.sensor_id} is not a "
                    f"{expected_modality} sensor"
                )
                raise ValueError(msg)

    def _replay_schedule(self) -> None:
        """Chronologically replay every scheduled interaction against a minimal
        carry/presence model, enforcing every cross-reference and physical
        consistency rule a scenario author can get wrong. This never touches
        ``VirtualWorld``: it is a pure bookkeeping pass over this model's own
        data, so an invalid scenario fails loudly here rather than silently
        producing a misleading run later.
        """
        shopper_ids = {s.shopper_id for s in self.shoppers}
        fixture_ids = {f.fixture_id for f in self.store.fixtures}
        home_fixture_of = {i.epc.value: i.home_fixture_id for i in self.store.items}
        store_epcs = set(home_fixture_of)
        presence = {s.shopper_id: (s.entry_t, s.exit_t) for s in self.shoppers}

        carrier_of: dict[str, str | None] = {epc: None for epc in store_epcs}
        entered: set[str] = set()
        exited: set[str] = set()

        # Stable sort by (t, original position) so two interactions scheduled at
        # exactly the same instant are still resolved in a deterministic,
        # author-controlled order rather than an arbitrary one.
        ordered = sorted(
            enumerate(self.scheduled_interactions), key=lambda pair: (pair[1].t, pair[0])
        )
        for _, action in ordered:
            if action.t > self.duration_s:
                msg = f"interaction {action.verb} at {action.t} s is after the scenario ends"
                raise ValueError(msg)
            if action.shopper_id not in shopper_ids:
                msg = f"interaction references unknown shopper {action.shopper_id}"
                raise ValueError(msg)
            entry_t, exit_t = presence[action.shopper_id]
            if not (entry_t - _TIME_EPSILON <= action.t <= exit_t + _TIME_EPSILON):
                msg = (
                    f"{action.shopper_id} is not present at {action.t} s "
                    f"(present {entry_t}-{exit_t} s)"
                )
                raise ValueError(msg)

            if action.verb == InteractionVerb.ENTER:
                if action.shopper_id in entered:
                    msg = f"{action.shopper_id} has more than one ENTER"
                    raise ValueError(msg)
                entered.add(action.shopper_id)
            elif action.verb == InteractionVerb.EXIT:
                if action.shopper_id in exited:
                    msg = f"{action.shopper_id} has more than one EXIT"
                    raise ValueError(msg)
                exited.add(action.shopper_id)
            elif action.verb == InteractionVerb.APPROACH_FIXTURE:
                if action.fixture_id is None or action.fixture_id not in fixture_ids:
                    msg = f"interaction references unknown fixture {action.fixture_id!r}"
                    raise ValueError(msg)
            elif action.verb in (InteractionVerb.PICK, InteractionVerb.CARRY):
                epc = _require_known_epc(action, store_epcs)
                if action.verb == InteractionVerb.PICK:
                    if carrier_of[epc] is not None:
                        msg = (
                            f"PICK of {epc} at {action.t} s: already carried by "
                            f"{carrier_of[epc]}"
                        )
                        raise ValueError(msg)
                    carrier_of[epc] = action.shopper_id
                elif carrier_of[epc] != action.shopper_id:
                    # CARRY: an ongoing-carry sample, no state change of its own.
                    msg = (
                        f"CARRY of {epc} by {action.shopper_id} at {action.t} s: not "
                        "currently carried by them"
                    )
                    raise ValueError(msg)
            elif action.verb in (InteractionVerb.PUTBACK, InteractionVerb.MISPLACE):
                epc = _require_known_epc(action, store_epcs)
                if carrier_of[epc] != action.shopper_id:
                    msg = (
                        f"{action.verb} of {epc} by {action.shopper_id} at {action.t} s: not "
                        "currently carried by them"
                    )
                    raise ValueError(msg)
                target_fixture = action.fixture_id or home_fixture_of[epc]
                if target_fixture is None or target_fixture not in fixture_ids:
                    msg = f"interaction references unknown fixture {action.fixture_id!r}"
                    raise ValueError(msg)
                if (
                    action.verb == InteractionVerb.MISPLACE
                    and target_fixture == home_fixture_of[epc]
                ):
                    msg = f"MISPLACE of {epc} targets its own home fixture {target_fixture}"
                    raise ValueError(msg)
                carrier_of[epc] = None
            elif action.verb == InteractionVerb.HANDOFF:
                epc = _require_known_epc(action, store_epcs)
                if carrier_of[epc] != action.shopper_id:
                    msg = (
                        f"HANDOFF of {epc} by {action.shopper_id} at {action.t} s: not "
                        "currently carried by them"
                    )
                    raise ValueError(msg)
                counterpart = action.counterpart_shopper_id
                if counterpart is None:
                    msg = "a HANDOFF interaction needs a counterpart_shopper_id"
                    raise ValueError(msg)
                if counterpart == action.shopper_id:
                    msg = f"{action.shopper_id} cannot hand {epc} off to themselves"
                    raise ValueError(msg)
                if counterpart not in shopper_ids:
                    msg = f"HANDOFF references unknown shopper {counterpart}"
                    raise ValueError(msg)
                c_entry, c_exit = presence[counterpart]
                if not (c_entry - _TIME_EPSILON <= action.t <= c_exit + _TIME_EPSILON):
                    msg = f"{counterpart} is not present at {action.t} s to receive {epc}"
                    raise ValueError(msg)
                carrier_of[epc] = counterpart

    def _check_expected_truth(self) -> None:
        shopper_ids = {s.shopper_id for s in self.shoppers}
        store_epcs = {i.epc.value for i in self.store.items}
        for exit_truth in self.expected_truth.exits:
            if exit_truth.shopper_id not in shopper_ids:
                msg = f"expected_truth references unknown shopper {exit_truth.shopper_id}"
                raise ValueError(msg)
            for epc in exit_truth.epcs:
                if epc not in store_epcs:
                    msg = f"expected_truth references unknown EPC {epc}"
                    raise ValueError(msg)
        for event in self.expected_truth.events:
            if event.t > self.duration_s:
                msg = f"expected_truth event at {event.t} s is after the scenario ends"
                raise ValueError(msg)
            if event.epc not in store_epcs:
                msg = f"expected_truth event references unknown EPC {event.epc}"
                raise ValueError(msg)
            for label in (event.shopper_id, event.counterpart_shopper_id):
                if label is not None and label not in shopper_ids:
                    msg = f"expected_truth event references unknown shopper {label}"
                    raise ValueError(msg)
            if event.event_type == RetailEventType.HANDOFF and event.counterpart_shopper_id is None:
                msg = "a HANDOFF expected_truth event needs a counterpart_shopper_id"
                raise ValueError(msg)

    # --- lookups --------------------------------------------------------------
    def shopper(self, shopper_id: str) -> LabShopperScript:
        for script in self.shoppers:
            if script.shopper_id == shopper_id:
                return script
        msg = f"unknown shopper {shopper_id}"
        raise KeyError(msg)


def _require_known_epc(action: ScheduledInteraction, store_epcs: set[str]) -> str:
    if action.epc is None:
        msg = f"{action.verb} needs an epc"
        raise ValueError(msg)
    if action.epc not in store_epcs:
        msg = f"interaction references unknown EPC {action.epc}"
        raise ValueError(msg)
    return action.epc


# =============================================================================
# The catalog: thirteen named scenarios plus the acceptance-gate scenario.
# =============================================================================


def _wp(t: float, xy: tuple[float, float]) -> LabWaypoint:
    return LabWaypoint(t=t, x=xy[0], y=xy[1])


def _shopper(
    shopper_id: str,
    *points: tuple[float, tuple[float, float]],
    speed_mps: float = DEFAULT_SPEED_MPS,
) -> LabShopperScript:
    """Build a ``LabShopperScript`` from ``(t, (x, y))`` pairs; the first pair is entry."""
    return LabShopperScript(
        shopper_id=shopper_id,
        entry_t=points[0][0],
        waypoints=[_wp(t, xy) for t, xy in points],
        speed_mps=speed_mps,
    )


def _lab_scenario(
    scenario_id: str, name: str, description: str, duration_s: float, **kw: Any
) -> LabScenario:
    return LabScenario(
        scenario_id=scenario_id,
        name=name,
        description=description,
        duration_s=duration_s,
        store=build_virtual_lab_store(),
        **kw,
    )


def scenario_01_normal_purchase() -> LabScenario:
    epc = EPCS_RACK_A[0]
    return _lab_scenario(
        "01_normal_purchase",
        "one shopper, one pick, one exit",
        "A enters, walks straight to Rack A, picks one item and exits carrying it.",
        24.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (8, RACK_A_APPROACH),
                (14, MID_FLOOR),
                (20, EXIT_POINT),
                (22, EXIT_POINT),
            )
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc
            ),
            ScheduledInteraction(t=22.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
        ],
        expected_truth=ExpectedTruth(
            exits=(ExpectedExit(shopper_id="GT-PERSON-001", epcs=(epc,)),),
            events=(
                ExpectedEvent(
                    t=6.0, event_type=RetailEventType.PICK, epc=epc, shopper_id="GT-PERSON-001"
                ),
                ExpectedEvent(
                    t=20.0,
                    event_type=RetailEventType.EXIT_WITH_ITEM,
                    epc=epc,
                    shopper_id="GT-PERSON-001",
                ),
            ),
        ),
    )


def scenario_02_putback() -> LabScenario:
    epc = EPCS_RACK_A[0]
    return _lab_scenario(
        "02_putback",
        "shopper returns the item to its home fixture",
        "A picks an item at Rack A, walks off, comes back and puts it back, then leaves "
        "empty-handed.",
        30.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (8, RACK_A_APPROACH),
                (14, MID_FLOOR),
                (18, RACK_A_APPROACH),
                (21, RACK_A_APPROACH),
                (26, EXIT_POINT),
                (28, EXIT_POINT),
            )
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc
            ),
            ScheduledInteraction(
                t=19.0, verb=InteractionVerb.PUTBACK, shopper_id="GT-PERSON-001", epc=epc
            ),
            ScheduledInteraction(t=28.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
        ],
        expected_truth=ExpectedTruth(
            exits=(ExpectedExit(shopper_id="GT-PERSON-001", epcs=()),),
            events=(
                ExpectedEvent(
                    t=6.0, event_type=RetailEventType.PICK, epc=epc, shopper_id="GT-PERSON-001"
                ),
                ExpectedEvent(
                    t=19.0,
                    event_type=RetailEventType.PUTBACK,
                    epc=epc,
                    shopper_id="GT-PERSON-001",
                ),
            ),
        ),
    )


def scenario_03_misplace() -> LabScenario:
    epc = EPCS_RACK_A[0]
    return _lab_scenario(
        "03_misplace",
        "shopper sets the item down on the wrong rack",
        "A picks an item at Rack A and sets it down at Rack B -- a true misplace, not a putback.",
        26.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (8, RACK_A_APPROACH),
                (14, RACK_B_APPROACH),
                (17, RACK_B_APPROACH),
                (22, EXIT_POINT),
                (24, EXIT_POINT),
            )
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc
            ),
            ScheduledInteraction(
                t=15.0,
                verb=InteractionVerb.MISPLACE,
                shopper_id="GT-PERSON-001",
                epc=epc,
                fixture_id=RACK_B,
            ),
            ScheduledInteraction(t=24.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
        ],
        expected_truth=ExpectedTruth(
            exits=(ExpectedExit(shopper_id="GT-PERSON-001", epcs=()),),
            events=(
                ExpectedEvent(
                    t=6.0, event_type=RetailEventType.PICK, epc=epc, shopper_id="GT-PERSON-001"
                ),
                ExpectedEvent(
                    t=15.0,
                    event_type=RetailEventType.MISPLACE,
                    epc=epc,
                    shopper_id="GT-PERSON-001",
                ),
            ),
        ),
    )


def scenario_04_handoff() -> LabScenario:
    epc = EPCS_RACK_A[0]
    return _lab_scenario(
        "04_handoff",
        "shopper A hands the item to shopper B mid-floor",
        "A picks an item at Rack A, meets B mid-floor and hands it over; B carries it out, A "
        "leaves empty-handed.",
        32.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (8, RACK_A_APPROACH),
                (12, MID_FLOOR),
                (16, MID_FLOOR),
                (24, EXIT_POINT),
                (26, EXIT_POINT),
            ),
            _shopper(
                "GT-PERSON-002",
                (1, ENTRY_POINT),
                (14, MID_FLOOR),
                (16, MID_FLOOR),
                (24, EXIT_POINT),
                (30, EXIT_POINT),
            ),
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(t=1.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-002"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc
            ),
            ScheduledInteraction(
                t=15.0,
                verb=InteractionVerb.HANDOFF,
                shopper_id="GT-PERSON-001",
                epc=epc,
                counterpart_shopper_id="GT-PERSON-002",
            ),
            ScheduledInteraction(t=26.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(t=30.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-002"),
        ],
        expected_truth=ExpectedTruth(
            exits=(
                ExpectedExit(shopper_id="GT-PERSON-001", epcs=()),
                ExpectedExit(shopper_id="GT-PERSON-002", epcs=(epc,)),
            ),
            events=(
                ExpectedEvent(
                    t=6.0, event_type=RetailEventType.PICK, epc=epc, shopper_id="GT-PERSON-001"
                ),
                ExpectedEvent(
                    t=15.0,
                    event_type=RetailEventType.HANDOFF,
                    epc=epc,
                    shopper_id="GT-PERSON-001",
                    counterpart_shopper_id="GT-PERSON-002",
                ),
                ExpectedEvent(
                    t=24.0,
                    event_type=RetailEventType.EXIT_WITH_ITEM,
                    epc=epc,
                    shopper_id="GT-PERSON-002",
                ),
            ),
        ),
    )


def scenario_05_two_shoppers_cross() -> LabScenario:
    """Two shoppers enter almost together and walk to opposite racks, their paths
    overlapping near mid-floor at overlapping times -- a trajectory-association
    stress case with no handoff at all (contrast with 04)."""
    epc_a = EPCS_RACK_A[0]
    epc_b = EPCS_RACK_B[0]
    return _lab_scenario(
        "05_two_shoppers_cross",
        "two shoppers' trajectories cross mid-floor, unrelated items",
        "A and B enter together and cross paths heading to opposite racks; each picks their own "
        "item and leaves; they never interact.",
        22.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (6, RACK_B_APPROACH),
                (9, RACK_B_APPROACH),
                (16, EXIT_POINT),
                (18, EXIT_POINT),
            ),
            _shopper(
                "GT-PERSON-002",
                (0.5, ENTRY_POINT),
                (7, RACK_A_APPROACH),
                (10, RACK_A_APPROACH),
                (17, EXIT_POINT),
                (19, EXIT_POINT),
            ),
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(t=0.5, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-002"),
            ScheduledInteraction(
                t=6.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_B,
            ),
            ScheduledInteraction(
                t=7.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-002",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=7.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc_b
            ),
            ScheduledInteraction(
                t=8.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-002", epc=epc_a
            ),
            ScheduledInteraction(t=18.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(t=19.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-002"),
        ],
        expected_truth=ExpectedTruth(
            exits=(
                ExpectedExit(shopper_id="GT-PERSON-001", epcs=(epc_b,)),
                ExpectedExit(shopper_id="GT-PERSON-002", epcs=(epc_a,)),
            ),
            events=(
                ExpectedEvent(
                    t=7.0, event_type=RetailEventType.PICK, epc=epc_b, shopper_id="GT-PERSON-001"
                ),
                ExpectedEvent(
                    t=8.0, event_type=RetailEventType.PICK, epc=epc_a, shopper_id="GT-PERSON-002"
                ),
            ),
        ),
    )


def scenario_06_same_sku_distinct_epcs() -> LabScenario:
    """The phase's headline property: two physically distinct EPCs that share one
    GTIN. A SKU-collapsing bug (treating the GTIN as canonical identity) would
    make this look like one item bought twice, or one item teleporting between
    two shoppers; a correct fusion pipeline must keep the two units distinct."""
    epc_a, epc_b = EPCS_RACK_A[0], EPCS_RACK_A[1]
    return _lab_scenario(
        "06_same_sku_distinct_epcs",
        "two units of the same product, two different physical items",
        "A and B, at well-separated times, each pick a distinct EPC of the same GTIN off Rack A "
        "and leave with their own unit.",
        40.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (8, RACK_A_APPROACH),
                (14, EXIT_POINT),
                (16, EXIT_POINT),
            ),
            _shopper(
                "GT-PERSON-002",
                (18, ENTRY_POINT),
                (23, RACK_A_APPROACH),
                (26, RACK_A_APPROACH),
                (32, EXIT_POINT),
                (34, EXIT_POINT),
            ),
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc_a
            ),
            ScheduledInteraction(t=16.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(t=18.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-002"),
            ScheduledInteraction(
                t=23.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-002",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=24.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-002", epc=epc_b
            ),
            ScheduledInteraction(t=34.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-002"),
        ],
        expected_truth=ExpectedTruth(
            exits=(
                ExpectedExit(shopper_id="GT-PERSON-001", epcs=(epc_a,)),
                ExpectedExit(shopper_id="GT-PERSON-002", epcs=(epc_b,)),
            ),
            events=(
                ExpectedEvent(
                    t=6.0, event_type=RetailEventType.PICK, epc=epc_a, shopper_id="GT-PERSON-001"
                ),
                ExpectedEvent(
                    t=24.0,
                    event_type=RetailEventType.PICK,
                    epc=epc_b,
                    shopper_id="GT-PERSON-002",
                ),
            ),
        ),
    )


def scenario_07_radar_dropout() -> LabScenario:
    base = scenario_01_normal_purchase()
    update = {
        "scenario_id": "07_radar_dropout",
        "name": "temporary radar observation loss mid-carry",
        "description": "Scenario 01 with the lab mmWave radar suppressed for 3 s while the item "
        "is being carried.",
        "fault_injections": [
            FaultInjection(
                kind=FaultKind.RADAR_DROPOUT,
                start_t=9.0,
                end_t=12.0,
                sensor_id=MMWAVE_SENSOR_ID,
                note="radar blind spot while GT-PERSON-001 walks from Rack A to mid-floor",
            )
        ],
    }
    return LabScenario.model_validate({**base.model_dump(), **update})


def scenario_08_rfid_dropout() -> LabScenario:
    base = scenario_01_normal_purchase()
    update = {
        "scenario_id": "08_rfid_dropout",
        "name": "temporary RFID read-point outage at the pick fixture",
        "description": "Scenario 01 with Rack A's RFID read point suppressed for 3 s spanning the "
        "pick.",
        "fault_injections": [
            FaultInjection(
                kind=FaultKind.RFID_DROPOUT,
                start_t=5.0,
                end_t=8.0,
                sensor_id=RFID_RACK_A_ID,
                note="reader outage spanning the PICK at t=6",
            )
        ],
    }
    return LabScenario.model_validate({**base.model_dump(), **update})


def scenario_09_native_id_reuse() -> LabScenario:
    """A schedules and leaves early (its radar-native id retires when it leaves
    the mmWave FOV); B enters after A has left and is expected to be assigned a
    native id drawn from that same recently-retired pool. This is what exercises
    the normalizer's stream-generation / native-id-hint scoping against a
    genuinely reused raw TI id rather than a monotonically growing one."""
    epc = EPCS_RACK_A[0]
    return _lab_scenario(
        "09_native_id_reuse",
        "a retired radar-native id is later reassigned to a new shopper",
        "A enters, picks an item and leaves quickly; B enters afterward and is expected to reuse "
        "A's just-retired mmWave native id.",
        36.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (7, RACK_A_APPROACH),
                (12, EXIT_POINT),
                (14, EXIT_POINT),
            ),
            _shopper(
                "GT-PERSON-002",
                (20, ENTRY_POINT),
                (25, RACK_B_APPROACH),
                (27, RACK_B_APPROACH),
                (32, EXIT_POINT),
                (34, EXIT_POINT),
            ),
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc
            ),
            ScheduledInteraction(t=14.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(t=20.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-002"),
            ScheduledInteraction(t=34.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-002"),
        ],
        fault_injections=[
            FaultInjection(
                kind=FaultKind.NATIVE_ID_REUSE,
                start_t=14.0,
                end_t=20.0,
                sensor_id=MMWAVE_SENSOR_ID,
                note="GT-PERSON-001's retired native id should be reassigned to GT-PERSON-002",
            )
        ],
        expected_truth=ExpectedTruth(
            exits=(
                ExpectedExit(shopper_id="GT-PERSON-001", epcs=(epc,)),
                ExpectedExit(shopper_id="GT-PERSON-002", epcs=()),
            ),
            events=(
                ExpectedEvent(
                    t=6.0, event_type=RetailEventType.PICK, epc=epc, shopper_id="GT-PERSON-001"
                ),
            ),
        ),
    )


def scenario_10_crowded_rack_ambiguity() -> LabScenario:
    """Three shoppers approach Rack A at overlapping times with only one PICK
    recorded: attribution genuinely is ambiguous among the three candidates. This
    is expected to stress fusion, and a WAIT/low-confidence outcome is an
    acceptable (even desirable) result here -- see the fixture-crowding
    discussion in the top-level scenario 12."""
    epc = EPCS_RACK_A[0]
    return _lab_scenario(
        "10_crowded_rack_ambiguity",
        "three shoppers crowd one rack at the same time",
        "A, B and C all stand at Rack A during an overlapping window; only one of them actually "
        "picks an item, so attribution among the three is genuinely ambiguous.",
        30.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (14, RACK_A_APPROACH),
                (20, EXIT_POINT),
                (22, EXIT_POINT),
            ),
            _shopper(
                "GT-PERSON-002",
                (0.3, ENTRY_POINT),
                (5.3, RACK_A_APPROACH),
                (14, RACK_A_APPROACH),
                (21, EXIT_POINT),
                (23, EXIT_POINT),
            ),
            _shopper(
                "GT-PERSON-003",
                (0.6, ENTRY_POINT),
                (5.6, RACK_A_APPROACH),
                (14, RACK_A_APPROACH),
                (22, EXIT_POINT),
                (24, EXIT_POINT),
            ),
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(t=0.3, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-002"),
            ScheduledInteraction(t=0.6, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-003"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=5.3,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-002",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=5.6,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-003",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=9.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-002", epc=epc
            ),
            ScheduledInteraction(t=22.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(t=23.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-002"),
            ScheduledInteraction(t=24.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-003"),
        ],
        expected_truth=ExpectedTruth(
            exits=(
                ExpectedExit(shopper_id="GT-PERSON-001", epcs=()),
                ExpectedExit(shopper_id="GT-PERSON-002", epcs=(epc,)),
                ExpectedExit(shopper_id="GT-PERSON-003", epcs=()),
            ),
            events=(
                ExpectedEvent(
                    t=9.0, event_type=RetailEventType.PICK, epc=epc, shopper_id="GT-PERSON-002"
                ),
            ),
        ),
    )


def scenario_11_multiple_items() -> LabScenario:
    epcs = (EPCS_RACK_A[0], EPCS_RACK_A[4], EPCS_RACK_C[0])
    return _lab_scenario(
        "11_multiple_items",
        "one shopper picks several items across racks",
        "A visits Rack A twice for two different products and Rack C once, leaving with three "
        "items.",
        34.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (9, RACK_A_APPROACH),
                (16, RACK_C_APPROACH),
                (19, RACK_C_APPROACH),
                (26, EXIT_POINT),
                (28, EXIT_POINT),
            )
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epcs[0]
            ),
            ScheduledInteraction(
                t=7.5, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epcs[1]
            ),
            ScheduledInteraction(
                t=16.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_C,
            ),
            ScheduledInteraction(
                t=17.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epcs[2]
            ),
            ScheduledInteraction(t=28.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
        ],
        expected_truth=ExpectedTruth(
            exits=(ExpectedExit(shopper_id="GT-PERSON-001", epcs=epcs),),
            events=tuple(
                ExpectedEvent(
                    t=t, event_type=RetailEventType.PICK, epc=e, shopper_id="GT-PERSON-001"
                )
                for t, e in ((6.0, epcs[0]), (7.5, epcs[1]), (17.0, epcs[2]))
            ),
        ),
    )


def scenario_12_exit_with_item() -> LabScenario:
    """A picks an item at Rack A, then browses Rack B and Rack C without picking
    anything there, and only then exits still carrying the original item. This
    exercises that browsing unrelated fixtures while carrying an item must not be
    mistaken for a putback/misplace at those fixtures -- distinct from scenario
    01's direct route."""
    epc = EPCS_RACK_A[0]
    return _lab_scenario(
        "12_exit_with_item",
        "carried item survives browsing at unrelated fixtures before exit",
        "A picks an item at Rack A, browses Rack B and Rack C without picking anything else, then "
        "exits the store still carrying the original item.",
        40.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (7, RACK_A_APPROACH),
                (13, RACK_B_APPROACH),
                (16, RACK_B_APPROACH),
                (22, RACK_C_APPROACH),
                (25, RACK_C_APPROACH),
                (32, EXIT_POINT),
                (34, EXIT_POINT),
            )
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc
            ),
            ScheduledInteraction(
                t=13.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_B,
            ),
            ScheduledInteraction(
                t=22.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_C,
            ),
            ScheduledInteraction(t=34.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
        ],
        expected_truth=ExpectedTruth(
            exits=(ExpectedExit(shopper_id="GT-PERSON-001", epcs=(epc,)),),
            events=(
                ExpectedEvent(
                    t=6.0, event_type=RetailEventType.PICK, epc=epc, shopper_id="GT-PERSON-001"
                ),
                ExpectedEvent(
                    t=32.0,
                    event_type=RetailEventType.EXIT_WITH_ITEM,
                    epc=epc,
                    shopper_id="GT-PERSON-001",
                ),
            ),
        ),
    )


def scenario_13_sensor_restart() -> LabScenario:
    base = scenario_01_normal_purchase()
    update = {
        "scenario_id": "13_sensor_restart",
        "name": "radar restarts mid-run",
        "description": "Scenario 01 with the lab mmWave radar restarting (frame-counter reset) "
        "while the item is being carried.",
        "fault_injections": [
            FaultInjection(
                kind=FaultKind.RADAR_RESTART,
                start_t=10.0,
                end_t=10.5,
                sensor_id=MMWAVE_SENSOR_ID,
                note="brief restart; the frame counter resumes from a lower value, forcing a new "
                "stream generation",
            )
        ],
    }
    return LabScenario.model_validate({**base.model_dump(), **update})


def scenario_acceptance_60s() -> LabScenario:
    """The Phase-4 acceptance-gate scenario: 3 shoppers, all 30 items across the
    3 racks, 1 entrance, 1 exit, 60 simulated seconds. Each shopper picks and
    leaves with at least one item; the run also contains at least one putback
    and one handoff, so every headline behaviour is exercised in a single run."""
    epc_a0, epc_a1 = EPCS_RACK_A[0], EPCS_RACK_A[1]
    epc_b0 = EPCS_RACK_B[0]
    epc_c0, epc_c1 = EPCS_RACK_C[0], EPCS_RACK_C[1]
    return _lab_scenario(
        "acceptance_60s",
        "Phase-4 acceptance gate: 3 shoppers, 30 items, 3 racks, 60 s",
        "A picks two items at Rack A and puts one back; B picks one item at Rack B; C picks two "
        "items at Rack C, hands one off to A mid-floor, and keeps the other. Every shopper leaves "
        "with at least one item.",
        60.0,
        shoppers=[
            _shopper(
                "GT-PERSON-001",
                (0, ENTRY_POINT),
                (5, RACK_A_APPROACH),
                (16, RACK_A_APPROACH),
                (20, MID_FLOOR),
                (24, MID_FLOOR),
                (45, EXIT_POINT),
                (48, EXIT_POINT),
            ),
            _shopper(
                "GT-PERSON-002",
                (2, ENTRY_POINT),
                (9, RACK_B_APPROACH),
                (11, RACK_B_APPROACH),
                (30, EXIT_POINT),
                (32, EXIT_POINT),
            ),
            _shopper(
                "GT-PERSON-003",
                (4, ENTRY_POINT),
                (12, RACK_C_APPROACH),
                (16, RACK_C_APPROACH),
                (22, MID_FLOOR),
                (24, MID_FLOOR),
                (40, EXIT_POINT),
                (42, EXIT_POINT),
            ),
        ],
        scheduled_interactions=[
            ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ScheduledInteraction(t=2.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-002"),
            ScheduledInteraction(t=4.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-003"),
            ScheduledInteraction(
                t=5.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-001",
                fixture_id=RACK_A,
            ),
            ScheduledInteraction(
                t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc_a0
            ),
            ScheduledInteraction(
                t=7.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=epc_a1
            ),
            ScheduledInteraction(
                t=15.0, verb=InteractionVerb.PUTBACK, shopper_id="GT-PERSON-001", epc=epc_a1
            ),
            ScheduledInteraction(
                t=9.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-002",
                fixture_id=RACK_B,
            ),
            ScheduledInteraction(
                t=10.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-002", epc=epc_b0
            ),
            ScheduledInteraction(
                t=12.0,
                verb=InteractionVerb.APPROACH_FIXTURE,
                shopper_id="GT-PERSON-003",
                fixture_id=RACK_C,
            ),
            ScheduledInteraction(
                t=13.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-003", epc=epc_c0
            ),
            ScheduledInteraction(
                t=14.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-003", epc=epc_c1
            ),
            ScheduledInteraction(
                t=22.0,
                verb=InteractionVerb.HANDOFF,
                shopper_id="GT-PERSON-003",
                epc=epc_c1,
                counterpart_shopper_id="GT-PERSON-001",
            ),
            ScheduledInteraction(t=32.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-002"),
            ScheduledInteraction(t=42.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-003"),
            ScheduledInteraction(t=48.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001"),
        ],
        expected_truth=ExpectedTruth(
            exits=(
                ExpectedExit(shopper_id="GT-PERSON-001", epcs=(epc_a0, epc_c1)),
                ExpectedExit(shopper_id="GT-PERSON-002", epcs=(epc_b0,)),
                ExpectedExit(shopper_id="GT-PERSON-003", epcs=(epc_c0,)),
            ),
            events=(
                ExpectedEvent(
                    t=6.0, event_type=RetailEventType.PICK, epc=epc_a0, shopper_id="GT-PERSON-001"
                ),
                ExpectedEvent(
                    t=7.0, event_type=RetailEventType.PICK, epc=epc_a1, shopper_id="GT-PERSON-001"
                ),
                ExpectedEvent(
                    t=15.0,
                    event_type=RetailEventType.PUTBACK,
                    epc=epc_a1,
                    shopper_id="GT-PERSON-001",
                ),
                ExpectedEvent(
                    t=10.0, event_type=RetailEventType.PICK, epc=epc_b0, shopper_id="GT-PERSON-002"
                ),
                ExpectedEvent(
                    t=13.0, event_type=RetailEventType.PICK, epc=epc_c0, shopper_id="GT-PERSON-003"
                ),
                ExpectedEvent(
                    t=14.0, event_type=RetailEventType.PICK, epc=epc_c1, shopper_id="GT-PERSON-003"
                ),
                ExpectedEvent(
                    t=22.0,
                    event_type=RetailEventType.HANDOFF,
                    epc=epc_c1,
                    shopper_id="GT-PERSON-003",
                    counterpart_shopper_id="GT-PERSON-001",
                ),
                ExpectedEvent(
                    t=45.0,
                    event_type=RetailEventType.EXIT_WITH_ITEM,
                    epc=epc_a0,
                    shopper_id="GT-PERSON-001",
                ),
                ExpectedEvent(
                    t=45.0,
                    event_type=RetailEventType.EXIT_WITH_ITEM,
                    epc=epc_c1,
                    shopper_id="GT-PERSON-001",
                ),
                ExpectedEvent(
                    t=30.0,
                    event_type=RetailEventType.EXIT_WITH_ITEM,
                    epc=epc_b0,
                    shopper_id="GT-PERSON-002",
                ),
                ExpectedEvent(
                    t=40.0,
                    event_type=RetailEventType.EXIT_WITH_ITEM,
                    epc=epc_c0,
                    shopper_id="GT-PERSON-003",
                ),
            ),
        ),
    )


SCENARIOS: dict[str, Callable[[], LabScenario]] = {
    "01_normal_purchase": scenario_01_normal_purchase,
    "02_putback": scenario_02_putback,
    "03_misplace": scenario_03_misplace,
    "04_handoff": scenario_04_handoff,
    "05_two_shoppers_cross": scenario_05_two_shoppers_cross,
    "06_same_sku_distinct_epcs": scenario_06_same_sku_distinct_epcs,
    "07_radar_dropout": scenario_07_radar_dropout,
    "08_rfid_dropout": scenario_08_rfid_dropout,
    "09_native_id_reuse": scenario_09_native_id_reuse,
    "10_crowded_rack_ambiguity": scenario_10_crowded_rack_ambiguity,
    "11_multiple_items": scenario_11_multiple_items,
    "12_exit_with_item": scenario_12_exit_with_item,
    "13_sensor_restart": scenario_13_sensor_restart,
    "acceptance_60s": scenario_acceptance_60s,
}


def load_scenario(scenario_id: str) -> LabScenario:
    try:
        return SCENARIOS[scenario_id]()
    except KeyError as exc:
        msg = f"unknown lab scenario {scenario_id!r}; known: {', '.join(SCENARIOS)}"
        raise KeyError(msg) from exc
