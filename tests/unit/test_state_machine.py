from __future__ import annotations

from radiowave.contracts import EPC, ItemObservation, ItemState, SpatialUncertainty, WorldCoordinate
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.config import ItemTrackingConfig, StateMachineConfig
from radiowave.fusion.state_machine import ItemStateMachine, Transition
from radiowave.fusion.tracking import ItemTrackManager, ItemTrackState
from radiowave.simulator.stores import EPC_SHIRT_A, FIXTURE_F1
from tests.conftest import at

SM = StateMachineConfig(
    movement_threshold_m=0.6,
    movement_confirm_steps=3,
    carry_displacement_m=1.5,
    carry_min_duration_s=1.0,
    rest_window_s=2.0,
    rest_displacement_m=0.3,
    rest_confirm_s=0.5,
    home_radius_m=1.2,
    hold_radius_m=1.0,
)
ITEM_CFG = ItemTrackingConfig(smoothing_alpha=1.0, rest_init_reads=2, stale_after_s=1.5)


def _obs(t: float, x: float, y: float) -> ItemObservation:
    return ItemObservation(
        observation_id=f"rfid:rfid-f1:{t:.2f}",
        sensor_id="rfid-f1",
        timestamp=at(t),
        confidence=0.8,
        epc=EPC(value=EPC_SHIRT_A),
        coordinate=WorldCoordinate(x=x, y=y, z=0.9),
        uncertainty=SpatialUncertainty.isotropic(0.5),
    )


class Harness:
    def __init__(self, registry: StoreRegistry) -> None:
        self.manager = ItemTrackManager(ITEM_CFG)
        self.machine = ItemStateMachine(SM, ITEM_CFG, registry)
        self.track: ItemTrackState = self.manager.register(EPC(value=EPC_SHIRT_A), None, FIXTURE_F1)
        self.transitions: list[Transition] = []
        self.t = 0.0

    def feed(
        self,
        seconds: float,
        x: float,
        y: float,
        nearest_person: float | None = None,
        carrier: WorldCoordinate | None = None,
        rate_hz: float = 10.0,
    ) -> None:
        steps = int(seconds * rate_hz)
        for _ in range(steps):
            self.t += 1.0 / rate_hz
            self.manager.ingest(_obs(self.t, x, y))
            if self.track.state == ItemState.UNKNOWN and self.track.rest_position is not None:
                self.track.state = self.machine.classify_rest(self.track, self.track.rest_position)
                self.track.rest_state = self.track.state
            transition = self.machine.evaluate(self.track, at(self.t), carrier, nearest_person)
            if transition is not None:
                self.transitions.append(transition)

    @property
    def states(self) -> list[tuple[ItemState, ItemState]]:
        return [(tr.from_state, tr.to_state) for tr in self.transitions]


def test_initial_rest_away_from_home_is_misplaced(registry: StoreRegistry) -> None:
    h = Harness(registry)
    h.feed(1.0, 8.0, 6.5)  # shirt whose home is F1 first seen on F2
    assert h.track.state == ItemState.MISPLACED
    assert h.track.rest_state == ItemState.MISPLACED


def test_small_displacement_is_jitter_not_pick(registry: StoreRegistry) -> None:
    h = Harness(registry)
    h.feed(1.0, 4.0, 6.5)
    assert h.track.state == ItemState.ON_FIXTURE
    h.feed(2.0, 4.4, 6.5)  # 0.4 m < threshold
    assert h.states == []
    assert h.track.state == ItemState.ON_FIXTURE


def test_movement_becomes_candidate_then_reverts_without_carry(registry: StoreRegistry) -> None:
    h = Harness(registry)
    h.feed(1.0, 4.0, 6.5)
    h.feed(0.5, 4.9, 6.5)  # 0.9 m: candidate after 3 confirming points
    assert h.states == [(ItemState.ON_FIXTURE, ItemState.INTERACTION_CANDIDATE)]
    assert h.track.movement_start_at is not None
    h.feed(0.5, 4.1, 6.5)  # back within threshold -> jitter, no event
    assert h.states[-1] == (ItemState.INTERACTION_CANDIDATE, ItemState.ON_FIXTURE)
    assert "jitter" in h.transitions[-1].reason
    assert h.track.movement_start_at is None


def test_sustained_displacement_is_carried_then_putback_near_home(registry: StoreRegistry) -> None:
    h = Harness(registry)
    h.feed(1.0, 4.0, 6.5)
    h.feed(1.5, 6.0, 4.0)  # 3 m away for 1.5 s -> CARRIED
    assert (ItemState.INTERACTION_CANDIDATE, ItemState.CARRIED) in h.states
    h.feed(3.0, 4.2, 5.4, nearest_person=0.3)  # back near F1 but a person is right there
    assert h.track.state == ItemState.CARRIED  # possibly still held: deferred
    h.feed(3.0, 4.2, 5.4, nearest_person=2.5)  # person stepped away
    assert h.states[-1] == (ItemState.CARRIED, ItemState.ON_FIXTURE)
    assert h.transitions[-1].measurements["home_distance_m"] < SM.home_radius_m
    assert h.track.rest_position is not None and abs(h.track.rest_position.x - 4.2) < 1e-9


def test_rest_far_from_home_is_misplaced(registry: StoreRegistry) -> None:
    h = Harness(registry)
    h.feed(1.0, 4.0, 6.5)
    h.feed(1.5, 8.0, 5.3)
    h.feed(3.0, 8.0, 5.3, nearest_person=None)
    assert h.states[-1] == (ItemState.CARRIED, ItemState.MISPLACED)
    h.feed(0.5, 9.0, 5.3)  # moving again from the misplaced spot restarts the cycle
    assert h.states[-1] == (ItemState.MISPLACED, ItemState.INTERACTION_CANDIDATE)


def test_exit_boundary_and_stale_items(registry: StoreRegistry) -> None:
    h = Harness(registry)
    h.feed(1.0, 4.0, 6.5)
    h.feed(1.5, 8.0, 5.3)
    h.feed(0.5, 11.2, 4.0)
    assert h.states[-1] == (ItemState.CARRIED, ItemState.EXITED)
    stale = h.machine.evaluate(h.track, at(h.t + 10), None, None)
    assert stale is None


def test_stale_item_never_transitions(registry: StoreRegistry) -> None:
    h = Harness(registry)
    h.feed(1.0, 4.0, 6.5)
    h.feed(1.5, 8.0, 5.3)
    assert h.track.state == ItemState.CARRIED
    assert h.machine.evaluate(h.track, at(h.t + 5.0), None, None) is None
    assert h.track.state == ItemState.CARRIED
