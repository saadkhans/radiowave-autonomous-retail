"""The twelve Foundation v0 scenarios, expressed as scripted ground truth.

Shopper labels (``A``, ``B``) are ground truth only; the system never sees them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from radiowave.contracts.events import RetailEventType as E
from radiowave.simulator.scenario import (
    Carry,
    Dropout,
    GroundTruthEvent,
    ItemPlacement,
    Scenario,
    ShopperScript,
    Waypoint,
)
from radiowave.simulator.stores import (
    ENTRY_POINT,
    EPC_HEADPHONES_A,
    EPC_SHIRT_A,
    EPC_SHIRT_B,
    EPC_SHOE_A,
    EXIT_POINT,
    F1_APPROACH,
    F2_APPROACH,
    F3_APPROACH,
    build_lab_store,
)

MID_FLOOR = (8.0, 4.0)
WEST_FLOOR = (2.5, 4.0)


def _wp(t: float, xy: tuple[float, float]) -> Waypoint:
    return Waypoint(t=t, x=xy[0], y=xy[1])


def _shopper(label: str, *points: tuple[float, tuple[float, float]]) -> ShopperScript:
    return ShopperScript(label=label, waypoints=[_wp(t, xy) for t, xy in points])


def _placements() -> list[ItemPlacement]:
    return [
        ItemPlacement(epc=EPC_SHIRT_A, x=3.8, y=6.5),
        ItemPlacement(epc=EPC_SHIRT_B, x=4.2, y=6.5),
        ItemPlacement(epc=EPC_SHOE_A, x=8.0, y=6.5),
        ItemPlacement(epc=EPC_HEADPHONES_A, x=4.0, y=1.5),
    ]


def _scenario(
    scenario_id: str, name: str, description: str, duration_s: float, **kw: Any
) -> Scenario:
    return Scenario(
        scenario_id=scenario_id,
        name=name,
        description=description,
        duration_s=duration_s,
        store=build_lab_store(),
        placements=_placements(),
        **kw,
    )


def scenario_01_single_pick() -> Scenario:
    return _scenario(
        "01",
        "one shopper picks one item",
        "A enters, walks to F1, picks shirt A and carries it to the middle of the floor.",
        18.0,
        shoppers=[
            _shopper(
                "A",
                (0, ENTRY_POINT),
                (4, F1_APPROACH),
                (7, F1_APPROACH),
                (12, MID_FLOOR),
                (18, MID_FLOOR),
            )
        ],
        carries=[Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.0)],
        expected_events=[
            GroundTruthEvent(t=6.0, event_type=E.PICK, epc=EPC_SHIRT_A, shopper_label="A")
        ],
    )


def scenario_02_putback() -> Scenario:
    return _scenario(
        "02",
        "shopper returns item to original fixture",
        "A picks shirt A at F1, walks away, returns to F1 and puts it back, then leaves.",
        32.0,
        shoppers=[
            _shopper(
                "A",
                (0, ENTRY_POINT),
                (4, F1_APPROACH),
                (7, F1_APPROACH),
                (12, MID_FLOOR),
                (18, F1_APPROACH),
                (22, F1_APPROACH),
                (30, EXIT_POINT),
                (32, EXIT_POINT),
            )
        ],
        carries=[Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.0, end_t=18.5)],
        expected_events=[
            GroundTruthEvent(t=6.0, event_type=E.PICK, epc=EPC_SHIRT_A, shopper_label="A"),
            GroundTruthEvent(t=18.5, event_type=E.PUTBACK, epc=EPC_SHIRT_A, shopper_label="A"),
        ],
    )


def scenario_03_misplace() -> Scenario:
    return _scenario(
        "03",
        "shopper returns item to incorrect fixture",
        "A picks shirt A at F1 and sets it down at F2, then leaves.",
        26.0,
        shoppers=[
            _shopper(
                "A",
                (0, ENTRY_POINT),
                (4, F1_APPROACH),
                (7, F1_APPROACH),
                (12, F2_APPROACH),
                (16, F2_APPROACH),
                (24, EXIT_POINT),
                (26, EXIT_POINT),
            )
        ],
        carries=[Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.0, end_t=12.5)],
        expected_events=[
            GroundTruthEvent(t=6.0, event_type=E.PICK, epc=EPC_SHIRT_A, shopper_label="A"),
            GroundTruthEvent(t=12.5, event_type=E.MISPLACE, epc=EPC_SHIRT_A, shopper_label="A"),
        ],
    )


def scenario_04_exit_with_item() -> Scenario:
    return _scenario(
        "04",
        "shopper exits carrying item",
        "A picks shirt A at F1 and walks out through the exit boundary.",
        18.0,
        shoppers=[
            _shopper(
                "A",
                (0, ENTRY_POINT),
                (4, F1_APPROACH),
                (7, F1_APPROACH),
                (15, EXIT_POINT),
                (18, EXIT_POINT),
            )
        ],
        carries=[Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.0)],
        expected_events=[
            GroundTruthEvent(t=6.0, event_type=E.PICK, epc=EPC_SHIRT_A, shopper_label="A"),
            GroundTruthEvent(
                t=15.0, event_type=E.EXIT_WITH_ITEM, epc=EPC_SHIRT_A, shopper_label="A"
            ),
        ],
    )


def scenario_05_handoff() -> Scenario:
    meet_a = (6.1, 4.0)
    meet_b = (6.9, 4.0)
    return _scenario(
        "05",
        "shopper A hands item to shopper B",
        "A picks shirt A, meets B mid-floor and hands it over; A leaves to F3, B walks on with it.",
        26.0,
        shoppers=[
            _shopper(
                "A",
                (0, ENTRY_POINT),
                (4, F1_APPROACH),
                (7, F1_APPROACH),
                (10, meet_a),
                (13, meet_a),
                (18, F3_APPROACH),
                (26, F3_APPROACH),
            ),
            _shopper(
                "B",
                (1, ENTRY_POINT),
                (8, meet_b),
                (16, meet_b),
                (19, (9.5, 4.0)),
                (21, (9.5, 5.3)),
                (26, (9.5, 5.3)),
            ),
        ],
        carries=[
            Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.0, end_t=11.5),
            Carry(epc=EPC_SHIRT_A, carrier_label="B", start_t=11.5),
        ],
        expected_events=[
            GroundTruthEvent(t=6.0, event_type=E.PICK, epc=EPC_SHIRT_A, shopper_label="A"),
            GroundTruthEvent(
                t=11.5,
                event_type=E.HANDOFF,
                epc=EPC_SHIRT_A,
                shopper_label="A",
                counterpart_label="B",
            ),
        ],
    )


def scenario_06_two_shoppers_same_fixture() -> Scenario:
    left = (3.6, 5.3)
    right = (4.4, 5.3)
    return _scenario(
        "06",
        "two shoppers approach the same fixture",
        "A and B both stand at F1; A picks shirt A and walks off; B lingers then leaves to F3.",
        20.0,
        shoppers=[
            _shopper("A", (0, ENTRY_POINT), (4, left), (8, left), (13, MID_FLOOR), (20, MID_FLOOR)),
            _shopper(
                "B",
                (0.5, ENTRY_POINT),
                (4.5, right),
                (10, right),
                (14, F3_APPROACH),
                (20, F3_APPROACH),
            ),
        ],
        carries=[Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.5)],
        expected_events=[
            GroundTruthEvent(t=6.5, event_type=E.PICK, epc=EPC_SHIRT_A, shopper_label="A")
        ],
    )


def scenario_07_identical_skus_different_epcs() -> Scenario:
    left = (3.6, 5.3)
    right = (4.4, 5.3)
    return _scenario(
        "07",
        "two identical SKU units have different EPCs",
        "Shirt A and shirt B share a GTIN. A takes shirt A, B takes shirt B, they walk apart.",
        20.0,
        shoppers=[
            _shopper("A", (0, ENTRY_POINT), (4, left), (8, left), (13, MID_FLOOR), (20, MID_FLOOR)),
            _shopper(
                "B",
                (0.5, ENTRY_POINT),
                (4.5, right),
                (8.5, right),
                (13, F3_APPROACH),
                (20, F3_APPROACH),
            ),
        ],
        carries=[
            Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.5),
            Carry(epc=EPC_SHIRT_B, carrier_label="B", start_t=7.0),
        ],
        expected_events=[
            GroundTruthEvent(t=6.5, event_type=E.PICK, epc=EPC_SHIRT_A, shopper_label="A"),
            GroundTruthEvent(t=7.0, event_type=E.PICK, epc=EPC_SHIRT_B, shopper_label="B"),
        ],
    )


def scenario_08_rfid_dropout() -> Scenario:
    base = scenario_01_single_pick()
    update = {
        "scenario_id": "08",
        "name": "temporary RFID observation loss",
        "description": "Scenario 01 with all RFID reads suppressed for 3 s mid-carry.",
        "rfid_dropouts": [Dropout(start_t=9.0, end_t=12.0)],
    }
    return Scenario.model_validate({**base.model_dump(), **update})


def scenario_09_radar_dropout() -> Scenario:
    base = scenario_01_single_pick()
    update = {
        "scenario_id": "09",
        "name": "temporary radar observation loss",
        "description": "Scenario 01 with both radars suppressed for 2 s mid-carry.",
        "radar_dropouts": [Dropout(start_t=9.0, end_t=11.0)],
    }
    return Scenario.model_validate({**base.model_dump(), **update})


def scenario_10_duplicate_replay() -> Scenario:
    base = scenario_01_single_pick()
    update = {
        "scenario_id": "10",
        "name": "duplicate observation replay",
        "description": "Scenario 01 whose observation stream is fed twice.",
        "duplicate_observation_stream": True,
    }
    return Scenario.model_validate({**base.model_dump(), **update})


def scenario_11_rfid_jitter_no_pickup() -> Scenario:
    return _scenario(
        "11",
        "small RFID jitter without a true pickup",
        "A browses past F1 without touching anything; RFID estimates jitter at configured noise.",
        20.0,
        shoppers=[
            _shopper(
                "A",
                (0, ENTRY_POINT),
                (5, (4.0, 4.6)),
                (9, (4.0, 4.6)),
                (14, MID_FLOOR),
                (20, MID_FLOOR),
            )
        ],
        carries=[],
        expected_events=[],
    )


def scenario_12_ambiguous_pick() -> Scenario:
    left = (3.6, 5.3)
    right = (4.6, 5.3)
    return _scenario(
        "12",
        "ambiguous two-shopper pickup should WAIT",
        "A and B stand at F1 side by side; A picks shirt A; they walk and stand together "
        "throughout.",
        34.0,
        shoppers=[
            _shopper(
                "A", (0, ENTRY_POINT), (4, left), (8, left), (13, (7.6, 4.0)), (34, (7.6, 4.0))
            ),
            _shopper(
                "B",
                (0.3, ENTRY_POINT),
                (4.3, right),
                (8, right),
                (13, (8.6, 4.0)),
                (34, (8.6, 4.0)),
            ),
        ],
        carries=[Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.5)],
        expected_events=[
            GroundTruthEvent(t=6.5, event_type=E.PICK, epc=EPC_SHIRT_A, shopper_label="A")
        ],
    )


def scenario_12v_ambiguous_pick_with_vision() -> Scenario:
    base = scenario_12_ambiguous_pick()
    update = {
        "scenario_id": "12v",
        "name": "ambiguous pickup resolved by vision evidence",
        "description": "Scenario 12 with selective vision evidence enabled at the fixture.",
        "vision_enabled": True,
    }
    return Scenario.model_validate({**base.model_dump(), **update})


def scenario_13_companion_leaves_store() -> Scenario:
    left = (3.6, 5.3)
    right = (4.6, 5.3)
    return _scenario(
        "13",
        "co-located companion leaves the store",
        "A and B stand at F1 and walk together after A picks shirt A; B exits the store, "
        "then A walks on alone and must be attributed the item.",
        34.0,
        shoppers=[
            _shopper(
                "A",
                (0, ENTRY_POINT),
                (4, left),
                (8, left),
                (13, (7.6, 4.0)),
                (24, (7.6, 4.0)),
                (29, F3_APPROACH),
                (34, F3_APPROACH),
            ),
            _shopper(
                "B",
                (0.3, ENTRY_POINT),
                (4.3, right),
                (8, right),
                (13, (8.6, 4.0)),
                (16, EXIT_POINT),
            ),
        ],
        carries=[Carry(epc=EPC_SHIRT_A, carrier_label="A", start_t=6.5)],
        expected_events=[
            GroundTruthEvent(t=6.5, event_type=E.PICK, epc=EPC_SHIRT_A, shopper_label="A")
        ],
    )


SCENARIOS: dict[str, Callable[[], Scenario]] = {
    "01": scenario_01_single_pick,
    "02": scenario_02_putback,
    "03": scenario_03_misplace,
    "04": scenario_04_exit_with_item,
    "05": scenario_05_handoff,
    "06": scenario_06_two_shoppers_same_fixture,
    "07": scenario_07_identical_skus_different_epcs,
    "08": scenario_08_rfid_dropout,
    "09": scenario_09_radar_dropout,
    "10": scenario_10_duplicate_replay,
    "11": scenario_11_rfid_jitter_no_pickup,
    "12": scenario_12_ambiguous_pick,
    "12v": scenario_12v_ambiguous_pick_with_vision,
    "13": scenario_13_companion_leaves_store,
}


def load_scenario(scenario_id: str) -> Scenario:
    try:
        return SCENARIOS[scenario_id]()
    except KeyError as exc:
        msg = f"unknown scenario {scenario_id!r}; known: {', '.join(SCENARIOS)}"
        raise KeyError(msg) from exc
