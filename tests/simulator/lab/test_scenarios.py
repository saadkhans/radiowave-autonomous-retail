"""Tests for the declarative Phase-4 lab scenario model and catalog.

Every test in this file constructs :class:`~radiowave.simulator.lab.scenarios.LabScenario`
objects directly -- never a ``VirtualWorld``, sensor emulator, or pipeline -- to keep
these tests honest about the "pure data" contract the module docstring promises.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from radiowave.contracts.events import RetailEventType
from radiowave.simulator.lab.scenarios import (
    SCENARIOS,
    ExpectedEvent,
    ExpectedExit,
    ExpectedTruth,
    FaultInjection,
    FaultKind,
    InteractionVerb,
    LabScenario,
    LabShopperScript,
    LabWaypoint,
    ScheduledInteraction,
    load_scenario,
)
from radiowave.simulator.lab.store import (
    ENTRY_POINT,
    EPCS_RACK_A,
    EPCS_RACK_B,
    EXIT_POINT,
    RACK_A,
    RACK_A_APPROACH,
    build_virtual_lab_store,
)

EPC_A0, EPC_A1 = EPCS_RACK_A[0], EPCS_RACK_A[1]
EPC_B0 = EPCS_RACK_B[0]


def _one_shopper_scenario(**kw: object) -> LabScenario:
    """Minimal, otherwise-valid single-shopper scenario for validator unit tests."""
    defaults: dict[str, object] = {
        "scenario_id": "test",
        "name": "test",
        "duration_s": 20.0,
        "store": build_virtual_lab_store(),
        "shoppers": [
            LabShopperScript(
                shopper_id="GT-PERSON-001",
                entry_t=0.0,
                waypoints=[
                    LabWaypoint(t=0.0, x=ENTRY_POINT[0], y=ENTRY_POINT[1]),
                    LabWaypoint(t=5.0, x=RACK_A_APPROACH[0], y=RACK_A_APPROACH[1]),
                    LabWaypoint(t=15.0, x=EXIT_POINT[0], y=EXIT_POINT[1]),
                ],
            )
        ],
    }
    defaults.update(kw)
    return LabScenario(**defaults)  # type: ignore[arg-type]


# --- pure-data contract ------------------------------------------------------


def test_scenarios_are_pure_data_no_world_or_sensor_import() -> None:
    """Constructing every catalog scenario must not require a world, sensors, or a
    pipeline -- importing this test module at all is itself part of the proof,
    since ``radiowave.simulator.lab.scenarios`` never imports
    ``radiowave.simulator.lab.world``, ``interactions``, ``actors``, or
    ``radiowave.pipeline``."""
    import radiowave.simulator.lab.scenarios as scenarios_module

    assert "radiowave.simulator.lab.world" not in _transitive_import_names(scenarios_module)
    assert "radiowave.simulator.lab.interactions" not in _transitive_import_names(
        scenarios_module
    )
    assert "radiowave.simulator.lab.actors" not in _transitive_import_names(scenarios_module)
    assert "radiowave.simulator.lab.sensors.rfid" not in _transitive_import_names(
        scenarios_module
    )
    assert "radiowave.pipeline" not in _transitive_import_names(scenarios_module)


def _transitive_import_names(module: object) -> set[str]:
    import dis

    source_names: set[str] = set()
    code = module.__loader__.get_code(module.__name__)  # type: ignore[union-attr]
    for instr in dis.get_instructions(code):
        if instr.opname == "IMPORT_NAME":
            source_names.add(instr.argval)
    return source_names


def test_constructing_every_catalog_scenario_needs_no_world() -> None:
    for scenario_id in SCENARIOS:
        scenario = load_scenario(scenario_id)
        assert isinstance(scenario, LabScenario)


# --- catalog: loads, validates, and has its named property -------------------


def test_all_thirteen_named_scenarios_plus_acceptance_are_registered() -> None:
    expected_ids = {
        "01_normal_purchase",
        "02_putback",
        "03_misplace",
        "04_handoff",
        "05_two_shoppers_cross",
        "06_same_sku_distinct_epcs",
        "07_radar_dropout",
        "08_rfid_dropout",
        "09_native_id_reuse",
        "10_crowded_rack_ambiguity",
        "11_multiple_items",
        "12_exit_with_item",
        "13_sensor_restart",
        "acceptance_60s",
    }
    assert set(SCENARIOS) == expected_ids


def test_01_normal_purchase_single_pick_single_exit() -> None:
    s = load_scenario("01_normal_purchase")
    picks = [a for a in s.scheduled_interactions if a.verb == InteractionVerb.PICK]
    assert len(picks) == 1
    assert len(s.shoppers) == 1
    (exit_truth,) = s.expected_truth.exits
    assert len(exit_truth.epcs) == 1


def test_02_putback_leaves_empty_handed() -> None:
    s = load_scenario("02_putback")
    verbs = [a.verb for a in s.scheduled_interactions]
    assert InteractionVerb.PICK in verbs
    assert InteractionVerb.PUTBACK in verbs
    (exit_truth,) = s.expected_truth.exits
    assert exit_truth.epcs == ()


def test_03_misplace_targets_a_different_fixture_than_home() -> None:
    s = load_scenario("03_misplace")
    misplace = next(a for a in s.scheduled_interactions if a.verb == InteractionVerb.MISPLACE)
    home_fixture = next(i.home_fixture_id for i in s.store.items if i.epc.value == misplace.epc)
    assert misplace.fixture_id is not None
    assert misplace.fixture_id != home_fixture


def test_04_handoff_transfers_the_item_between_two_shoppers() -> None:
    s = load_scenario("04_handoff")
    handoff = next(a for a in s.scheduled_interactions if a.verb == InteractionVerb.HANDOFF)
    assert handoff.counterpart_shopper_id is not None
    assert handoff.shopper_id != handoff.counterpart_shopper_id
    giver_exit = next(e for e in s.expected_truth.exits if e.shopper_id == handoff.shopper_id)
    receiver_exit = next(
        e for e in s.expected_truth.exits if e.shopper_id == handoff.counterpart_shopper_id
    )
    assert handoff.epc not in giver_exit.epcs
    assert handoff.epc in receiver_exit.epcs


def test_05_two_shoppers_cross_without_any_handoff() -> None:
    s = load_scenario("05_two_shoppers_cross")
    assert len(s.shoppers) == 2
    assert all(a.verb != InteractionVerb.HANDOFF for a in s.scheduled_interactions)
    # Each shopper leaves with their own, distinct item.
    epcs = [e.epcs for e in s.expected_truth.exits]
    assert all(len(e) == 1 for e in epcs)
    assert epcs[0] != epcs[1]


def test_06_same_sku_distinct_epcs_really_share_a_gtin() -> None:
    s = load_scenario("06_same_sku_distinct_epcs")
    picks = [a for a in s.scheduled_interactions if a.verb == InteractionVerb.PICK]
    assert len(picks) == 2
    epc_values = {a.epc for a in picks}
    assert len(epc_values) == 2, "the whole point is two DISTINCT physical EPCs"
    gtins = {i.epc.value: i.gtin for i in s.store.items}
    picked_gtins = {gtins[epc] for epc in epc_values if epc is not None}
    assert len(picked_gtins) == 1, "the two distinct EPCs must share one GTIN"


def test_07_radar_dropout_schedules_a_radar_fault() -> None:
    s = load_scenario("07_radar_dropout")
    assert len(s.fault_injections) == 1
    (fault,) = s.fault_injections
    assert fault.kind == FaultKind.RADAR_DROPOUT


def test_08_rfid_dropout_schedules_an_rfid_fault() -> None:
    s = load_scenario("08_rfid_dropout")
    assert len(s.fault_injections) == 1
    (fault,) = s.fault_injections
    assert fault.kind == FaultKind.RFID_DROPOUT


def test_09_native_id_reuse_schedules_a_retire_then_reassign_window() -> None:
    s = load_scenario("09_native_id_reuse")
    (fault,) = s.fault_injections
    assert fault.kind == FaultKind.NATIVE_ID_REUSE
    # The reuse window opens when the first shopper leaves and closes only after
    # the second shopper has entered -- a genuine "retired, then later
    # reassigned" window, not an instantaneous or degenerate one.
    first_leaves = s.shopper("GT-PERSON-001").exit_t
    second_enters = s.shopper("GT-PERSON-002").entry_t
    assert fault.start_t >= first_leaves
    assert fault.end_t <= second_enters
    assert fault.end_t > fault.start_t


def test_10_crowded_rack_ambiguity_has_multiple_shoppers_at_one_fixture_at_once() -> None:
    s = load_scenario("10_crowded_rack_ambiguity")
    approaches = [a for a in s.scheduled_interactions if a.verb == InteractionVerb.APPROACH_FIXTURE]
    assert len({a.fixture_id for a in approaches}) == 1, "all approaches target one rack"
    assert len(approaches) >= 3, "at least three shoppers approach it"
    # Their presence windows genuinely overlap, not just sequentially visit.
    windows = [
        (s.shopper(a.shopper_id).entry_t, s.shopper(a.shopper_id).exit_t) for a in approaches
    ]
    overlap_start = max(w[0] for w in windows)
    overlap_end = min(w[1] for w in windows)
    assert overlap_end > overlap_start


def test_11_multiple_items_shopper_leaves_with_several_items() -> None:
    s = load_scenario("11_multiple_items")
    (exit_truth,) = s.expected_truth.exits
    assert len(exit_truth.epcs) >= 3
    picks = [a for a in s.scheduled_interactions if a.verb == InteractionVerb.PICK]
    assert len(picks) == len(exit_truth.epcs)


def test_12_exit_with_item_browses_other_fixtures_before_leaving() -> None:
    s = load_scenario("12_exit_with_item")
    pick = next(a for a in s.scheduled_interactions if a.verb == InteractionVerb.PICK)
    approaches_after_pick = [
        a
        for a in s.scheduled_interactions
        if a.verb == InteractionVerb.APPROACH_FIXTURE and a.t > pick.t
    ]
    # Browses at least one fixture other than the one the item was picked from,
    # with no PUTBACK/MISPLACE at all -- the item is still carried at EXIT.
    assert any(a.fixture_id != RACK_A for a in approaches_after_pick)
    assert not any(
        a.verb in (InteractionVerb.PUTBACK, InteractionVerb.MISPLACE)
        for a in s.scheduled_interactions
    )
    (exit_truth,) = s.expected_truth.exits
    assert pick.epc in exit_truth.epcs


def test_13_sensor_restart_schedules_a_radar_restart_fault() -> None:
    s = load_scenario("13_sensor_restart")
    (fault,) = s.fault_injections
    assert fault.kind == FaultKind.RADAR_RESTART


def test_acceptance_60s_matches_the_phase4_acceptance_gate_shape() -> None:
    s = load_scenario("acceptance_60s")
    assert s.duration_s == 60.0
    assert len(s.shoppers) == 3
    assert len(s.store.items) == 30
    assert len({i.home_fixture_id for i in s.store.items}) == 3
    assert len(s.store.boundaries) == 2  # one entry, one exit

    verbs = [a.verb for a in s.scheduled_interactions]
    assert verbs.count(InteractionVerb.PUTBACK) >= 1
    assert verbs.count(InteractionVerb.HANDOFF) >= 1

    # Every shopper picks at least one item and leaves with at least one item.
    picking_shoppers = {
        a.shopper_id for a in s.scheduled_interactions if a.verb == InteractionVerb.PICK
    }
    assert picking_shoppers == {sh.shopper_id for sh in s.shoppers}
    for exit_truth in s.expected_truth.exits:
        assert len(exit_truth.epcs) >= 1


# --- validation: fails loudly at construction ---------------------------------


def test_unknown_epc_in_pick_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown EPC"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=6.0,
                    verb=InteractionVerb.PICK,
                    shopper_id="GT-PERSON-001",
                    epc="NOT-A-REAL-EPC",
                )
            ]
        )


def test_unknown_fixture_in_approach_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown fixture"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=5.0,
                    verb=InteractionVerb.APPROACH_FIXTURE,
                    shopper_id="GT-PERSON-001",
                    fixture_id="RACK-NOWHERE",
                )
            ]
        )


def test_unknown_shopper_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown shopper"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=1.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-999"
                )
            ]
        )


def test_interaction_before_shopper_enters_rejected() -> None:
    with pytest.raises(ValidationError, match="not present"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=EPC_A0
                )
            ],
            shoppers=[
                LabShopperScript(
                    shopper_id="GT-PERSON-001",
                    entry_t=10.0,
                    waypoints=[
                        LabWaypoint(t=10.0, x=ENTRY_POINT[0], y=ENTRY_POINT[1]),
                        LabWaypoint(t=15.0, x=EXIT_POINT[0], y=EXIT_POINT[1]),
                    ],
                )
            ],
        )


def test_interaction_after_shopper_exits_rejected() -> None:
    with pytest.raises(ValidationError, match="not present"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(t=18.0, verb=InteractionVerb.EXIT, shopper_id="GT-PERSON-001")
            ],
            shoppers=[
                LabShopperScript(
                    shopper_id="GT-PERSON-001",
                    entry_t=0.0,
                    waypoints=[
                        LabWaypoint(t=0.0, x=ENTRY_POINT[0], y=ENTRY_POINT[1]),
                        LabWaypoint(t=15.0, x=EXIT_POINT[0], y=EXIT_POINT[1]),
                    ],
                )
            ],
        )


def test_pick_of_already_carried_item_rejected() -> None:
    with pytest.raises(ValidationError, match="already carried"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=EPC_A0
                ),
                ScheduledInteraction(
                    t=7.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=EPC_A0
                ),
            ]
        )


def test_putback_by_non_carrier_rejected() -> None:
    with pytest.raises(ValidationError, match="not currently carried"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=6.0, verb=InteractionVerb.PUTBACK, shopper_id="GT-PERSON-001", epc=EPC_A0
                )
            ]
        )


def test_handoff_by_non_carrier_rejected() -> None:
    two_shopper_kwargs: dict[str, object] = {
        "shoppers": [
            LabShopperScript(
                shopper_id="GT-PERSON-001",
                entry_t=0.0,
                waypoints=[
                    LabWaypoint(t=0.0, x=ENTRY_POINT[0], y=ENTRY_POINT[1]),
                    LabWaypoint(t=15.0, x=EXIT_POINT[0], y=EXIT_POINT[1]),
                ],
            ),
            LabShopperScript(
                shopper_id="GT-PERSON-002",
                entry_t=0.0,
                waypoints=[
                    LabWaypoint(t=0.0, x=ENTRY_POINT[0], y=ENTRY_POINT[1]),
                    LabWaypoint(t=15.0, x=EXIT_POINT[0], y=EXIT_POINT[1]),
                ],
            ),
        ]
    }
    with pytest.raises(ValidationError, match="not currently carried"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=6.0,
                    verb=InteractionVerb.HANDOFF,
                    shopper_id="GT-PERSON-001",
                    epc=EPC_A0,
                    counterpart_shopper_id="GT-PERSON-002",
                )
            ],
            **two_shopper_kwargs,
        )


def test_misplace_targeting_home_fixture_rejected() -> None:
    home_fixture = next(
        i.home_fixture_id for i in build_virtual_lab_store().items if i.epc.value == EPC_A0
    )
    assert home_fixture == RACK_A
    with pytest.raises(ValidationError, match="own home fixture"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=EPC_A0
                ),
                ScheduledInteraction(
                    t=7.0,
                    verb=InteractionVerb.MISPLACE,
                    shopper_id="GT-PERSON-001",
                    epc=EPC_A0,
                    fixture_id=RACK_A,
                ),
            ]
        )


def test_overlapping_carry_via_double_pick_without_intervening_release_rejected() -> None:
    """A second shopper cannot pick an item still held by the first -- an
    'overlapping/contradictory schedule' caught by the chronological replay."""
    two_shopper_kwargs: dict[str, object] = {
        "shoppers": [
            LabShopperScript(
                shopper_id="GT-PERSON-001",
                entry_t=0.0,
                waypoints=[
                    LabWaypoint(t=0.0, x=ENTRY_POINT[0], y=ENTRY_POINT[1]),
                    LabWaypoint(t=15.0, x=EXIT_POINT[0], y=EXIT_POINT[1]),
                ],
            ),
            LabShopperScript(
                shopper_id="GT-PERSON-002",
                entry_t=0.0,
                waypoints=[
                    LabWaypoint(t=0.0, x=ENTRY_POINT[0], y=ENTRY_POINT[1]),
                    LabWaypoint(t=15.0, x=EXIT_POINT[0], y=EXIT_POINT[1]),
                ],
            ),
        ]
    }
    with pytest.raises(ValidationError, match="already carried"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=EPC_A0
                ),
                ScheduledInteraction(
                    t=7.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-002", epc=EPC_A0
                ),
            ],
            **two_shopper_kwargs,
        )


def test_duplicate_enter_rejected() -> None:
    with pytest.raises(ValidationError, match="more than one ENTER"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(t=0.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
                ScheduledInteraction(t=1.0, verb=InteractionVerb.ENTER, shopper_id="GT-PERSON-001"),
            ]
        )


def test_handoff_to_self_rejected() -> None:
    with pytest.raises(ValidationError, match="cannot hand"):
        _one_shopper_scenario(
            scheduled_interactions=[
                ScheduledInteraction(
                    t=6.0, verb=InteractionVerb.PICK, shopper_id="GT-PERSON-001", epc=EPC_A0
                ),
                ScheduledInteraction(
                    t=7.0,
                    verb=InteractionVerb.HANDOFF,
                    shopper_id="GT-PERSON-001",
                    epc=EPC_A0,
                    counterpart_shopper_id="GT-PERSON-001",
                ),
            ]
        )


def test_fault_injection_bad_ordering_rejected() -> None:
    with pytest.raises(ValidationError, match="ends"):
        FaultInjection(kind=FaultKind.RADAR_DROPOUT, start_t=10.0, end_t=5.0)


def test_fault_injection_wrong_modality_sensor_rejected() -> None:
    from radiowave.simulator.lab.store import RFID_RACK_A_ID

    with pytest.raises(ValidationError, match="not a"):
        _one_shopper_scenario(
            fault_injections=[
                FaultInjection(
                    kind=FaultKind.RADAR_DROPOUT, start_t=1.0, end_t=2.0, sensor_id=RFID_RACK_A_ID
                )
            ]
        )


def test_fault_injection_unknown_sensor_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown sensor"):
        _one_shopper_scenario(
            fault_injections=[
                FaultInjection(
                    kind=FaultKind.RADAR_DROPOUT, start_t=1.0, end_t=2.0, sensor_id="not-a-sensor"
                )
            ]
        )


def test_expected_truth_unknown_epc_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown EPC"):
        _one_shopper_scenario(
            expected_truth=ExpectedTruth(
                exits=(ExpectedExit(shopper_id="GT-PERSON-001", epcs=("NOT-A-REAL-EPC",)),)
            )
        )


def test_expected_truth_handoff_without_counterpart_rejected() -> None:
    with pytest.raises(ValidationError, match="HANDOFF"):
        _one_shopper_scenario(
            expected_truth=ExpectedTruth(
                events=(
                    ExpectedEvent(
                        t=1.0,
                        event_type=RetailEventType.HANDOFF,
                        epc=EPC_A0,
                        shopper_id="GT-PERSON-001",
                    ),
                )
            )
        )


def test_shopper_present_past_scenario_duration_rejected() -> None:
    with pytest.raises(ValidationError, match="after the scenario ends"):
        _one_shopper_scenario(
            duration_s=10.0,
            shoppers=[
                LabShopperScript(
                    shopper_id="GT-PERSON-001",
                    entry_t=0.0,
                    waypoints=[
                        LabWaypoint(t=0.0, x=ENTRY_POINT[0], y=ENTRY_POINT[1]),
                        LabWaypoint(t=15.0, x=EXIT_POINT[0], y=EXIT_POINT[1]),
                    ],
                )
            ],
        )


def test_shopper_script_waypoints_must_start_at_entry_t() -> None:
    with pytest.raises(ValidationError, match="entry_t"):
        LabShopperScript(
            shopper_id="GT-PERSON-001",
            entry_t=5.0,
            waypoints=[LabWaypoint(t=0.0, x=0.0, y=0.0)],
        )


def test_shopper_script_waypoints_must_be_time_ordered() -> None:
    with pytest.raises(ValidationError, match="time-ordered"):
        LabShopperScript(
            shopper_id="GT-PERSON-001",
            entry_t=2.0,
            waypoints=[LabWaypoint(t=2.0, x=0.0, y=0.0), LabWaypoint(t=1.0, x=1.0, y=1.0)],
        )


# --- load_scenario error message ----------------------------------------------


def test_load_scenario_unknown_id_lists_known_ids() -> None:
    with pytest.raises(KeyError) as exc_info:
        load_scenario("does-not-exist")
    message = str(exc_info.value)
    assert "does-not-exist" in message
    for scenario_id in SCENARIOS:
        assert scenario_id in message
