"""Transparent per-item retail state machine.

States and transitions (all thresholds in :class:`StateMachineConfig`)::

    ON_FIXTURE / MISPLACED
        --(displacement > movement_threshold on N consecutive fresh reads)--> INTERACTION_CANDIDATE
    INTERACTION_CANDIDATE
        --(displacement > carry_displacement or left home zone, sustained)--> CARRIED
        --(back at rest / timeout)--> previous rest state          (jitter, no event)
    CARRIED
        --(at rest near home fixture)--> ON_FIXTURE                 PUTBACK
        --(at rest elsewhere)--> MISPLACED                          MISPLACE
        --(inside exit boundary, or with exiting carrier)--> EXITED EXIT_WITH_ITEM

HANDOFF does not change the physical state (still CARRIED); it is an
attribution change decided by the fusion engine from the candidate ledger.

Every transition carries a human-readable reason and the measurements that
triggered it, so the behaviour can be audited from a replay.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from radiowave.contracts.geometry import WorldCoordinate
from radiowave.contracts.store import ZoneKind
from radiowave.contracts.tracks import ItemState
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.fusion.config import ItemTrackingConfig, StateMachineConfig
from radiowave.fusion.tracking import ItemTrackState


@dataclass(frozen=True)
class Transition:
    epc: str
    from_state: ItemState
    to_state: ItemState
    timestamp: datetime
    reason: str
    measurements: dict[str, float] = field(default_factory=dict)


def _seconds(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds()


class ItemStateMachine:
    def __init__(
        self,
        config: StateMachineConfig,
        item_config: ItemTrackingConfig,
        registry: StoreRegistry,
    ) -> None:
        self._cfg = config
        self._item_cfg = item_config
        self._registry = registry

    # ------------------------------------------------------------------
    def evaluate(
        self,
        item: ItemTrackState,
        now: datetime,
        carrier_position: WorldCoordinate | None,
        nearest_person_m: float | None = None,
    ) -> Transition | None:
        """Advance one item by at most one transition.

        Stale items never transition, and every evaluation must be backed by at
        least one localized read that arrived since the previous evaluation.

        ``nearest_person_m`` is the distance to the closest tracked person (None if
        nobody is tracked); a resting item with someone within ``hold_radius_m``
        may still be in hand, so rest is not declared until they step away.
        """
        if item.position is None or item.rest_position is None:
            return None
        if item.state == ItemState.EXITED:
            return None
        if item.state == ItemState.CARRIED:
            # Leaving the store is zone/carrier evidence, not motion evidence: an exit
            # portal that only reports zone-level reads must still close the episode.
            exited = self._exit_transition(item, now, carrier_position)
            if exited is not None:
                return exited
        if item.is_stale(now, self._item_cfg.stale_after_s):
            return None
        if not item.has_fresh_evidence():
            return None  # a cached position is never new evidence for any transition
        item.localized_at_last_evaluation = item.localized_count
        if item.state in (ItemState.ON_FIXTURE, ItemState.MISPLACED, ItemState.UNKNOWN):
            return self._from_rest(item, now)
        if item.state == ItemState.INTERACTION_CANDIDATE:
            return self._from_candidate(item, now)
        if item.state == ItemState.CARRIED:
            return self._from_carried(item, now, carrier_position, nearest_person_m)
        return None

    def classify_rest(self, item: ItemTrackState, position: WorldCoordinate) -> ItemState:
        """ON_FIXTURE if the position is within the home radius of the home fixture.

        An item with no configured home can never be "correctly returned", so its
        resting places are MISPLACED (an exception to review), never ON_FIXTURE.
        """
        home = self._registry.fixture(item.home_fixture_id) if item.home_fixture_id else None
        if home is None:
            return ItemState.MISPLACED
        if home.bounds.distance_to(position) <= self._cfg.home_radius_m:
            return ItemState.ON_FIXTURE
        return ItemState.MISPLACED

    # ------------------------------------------------------------------
    def _from_rest(self, item: ItemTrackState, now: datetime) -> Transition | None:
        displacement = item.displacement_from_rest()
        if displacement > self._cfg.movement_threshold_m:
            item.reads_beyond_threshold += 1
        else:
            item.reads_beyond_threshold = 0
            return None
        if item.reads_beyond_threshold < self._cfg.movement_confirm_reads:
            return None
        start = self._movement_start(item)
        return self._apply(
            item,
            ItemState.INTERACTION_CANDIDATE,
            now,
            reason=(
                f"smoothed position moved {displacement:.2f} m from rest for "
                f"{item.reads_beyond_threshold} consecutive steps with fresh reads "
                f"(threshold {self._cfg.movement_threshold_m} m)"
            ),
            measurements={"displacement_m": displacement},
            movement_start_at=start,
        )

    def _from_candidate(self, item: ItemTrackState, now: datetime) -> Transition | None:
        cfg = self._cfg
        displacement = item.displacement_from_rest()
        moving_for = _seconds(now, item.movement_start_at) if item.movement_start_at else 0.0
        left_home = self._left_home_zone(item)
        if (displacement > cfg.carry_displacement_m or left_home) and (
            moving_for >= cfg.carry_min_duration_s
        ):
            return self._apply(
                item,
                ItemState.CARRIED,
                now,
                reason=(
                    f"displacement {displacement:.2f} m (carry threshold "
                    f"{cfg.carry_displacement_m} m), left home zone={left_home}, "
                    f"moving for {moving_for:.1f} s"
                ),
                measurements={"displacement_m": displacement, "moving_for_s": moving_for},
            )
        if displacement <= cfg.movement_threshold_m:
            item.reads_beyond_threshold = 0
            return self._apply(
                item,
                item.rest_state,
                now,
                reason=f"returned within {cfg.movement_threshold_m} m of rest; treated as jitter",
                measurements={"displacement_m": displacement},
                clear_movement=True,
            )
        if moving_for > cfg.candidate_timeout_s:
            item.reads_beyond_threshold = 0
            return self._apply(
                item,
                item.rest_state,
                now,
                reason=f"candidate timed out after {moving_for:.1f} s without carry evidence",
                measurements={"displacement_m": displacement, "moving_for_s": moving_for},
                clear_movement=True,
            )
        return None

    def _from_carried(
        self,
        item: ItemTrackState,
        now: datetime,
        carrier_position: WorldCoordinate | None,
        nearest_person_m: float | None,
    ) -> Transition | None:
        cfg = self._cfg
        assert item.position is not None
        displacement = item.rest_displacement(cfg.rest_window_s)
        possibly_held = nearest_person_m is not None and nearest_person_m <= cfg.hold_radius_m
        if (
            displacement is not None
            and displacement < cfg.rest_displacement_m
            and not possibly_held
        ):
            if item.at_rest_since is None:
                item.at_rest_since = now
            resting_for = _seconds(now, item.at_rest_since)
            if resting_for >= cfg.rest_confirm_s:
                nearest_text = (
                    f"{nearest_person_m:.2f} m" if nearest_person_m is not None else "none tracked"
                )
                new_state = self.classify_rest(item, item.position)
                home = (
                    self._registry.fixture(item.home_fixture_id) if item.home_fixture_id else None
                )
                home_distance = home.bounds.distance_to(item.position) if home else float("nan")
                return self._apply(
                    item,
                    new_state,
                    now,
                    reason=(
                        f"estimate drifted only {displacement:.2f} m over the last "
                        f"{cfg.rest_window_s} s (rest threshold {cfg.rest_displacement_m} m); "
                        f"nearest person {nearest_text} (hold radius {cfg.hold_radius_m} m); "
                        f"{home_distance:.2f} m from home fixture "
                        f"(home radius {cfg.home_radius_m} m)"
                    ),
                    measurements={
                        "rest_displacement_m": displacement,
                        "resting_for_s": resting_for,
                        "home_distance_m": home_distance,
                        "nearest_person_m": (
                            nearest_person_m if nearest_person_m is not None else float("nan")
                        ),
                    },
                    new_rest=True,
                )
        else:
            item.at_rest_since = None
        return None

    def _exit_transition(
        self, item: ItemTrackState, now: datetime, carrier_position: WorldCoordinate | None
    ) -> Transition | None:
        """EXITED when any recent read (localized or zone-only) places the item at the exit."""
        cfg = self._cfg
        if (
            item.last_seen_at is None
            or _seconds(now, item.last_seen_at) > self._item_cfg.stale_after_s
        ):
            return None
        recent = list(item.zone_history)[-cfg.exit_zone_confirm_reads :]
        if len(recent) == cfg.exit_zone_confirm_reads and all(
            z is not None and self._registry.zone(z).kind == ZoneKind.EXIT for z in recent
        ):
            return self._apply(
                item,
                ItemState.EXITED,
                now,
                reason=(
                    f"{cfg.exit_zone_confirm_reads} consecutive reads from exit zone "
                    f"{recent[-1]} (portal burst)"
                ),
                measurements={"exit_zone_reads": float(len(recent))},
            )
        if item.position is not None and self._registry.in_exit_boundary(item.position):
            return self._apply(
                item,
                ItemState.EXITED,
                now,
                reason="item location estimate inside exit boundary",
                measurements={},
            )
        if (
            carrier_position is not None
            and item.position is not None
            and self._registry.in_exit_boundary(carrier_position)
        ):
            gap = item.position.horizontal_distance_to(carrier_position)
            if gap <= cfg.exit_item_radius_m:
                return self._apply(
                    item,
                    ItemState.EXITED,
                    now,
                    reason=(
                        f"carrier inside exit boundary with item {gap:.2f} m away "
                        f"(radius {cfg.exit_item_radius_m} m)"
                    ),
                    measurements={"carrier_gap_m": gap},
                )
        return None

    # ------------------------------------------------------------------
    def _left_home_zone(self, item: ItemTrackState) -> bool:
        if item.position is None or item.home_fixture_id is None:
            return False
        fixture = self._registry.fixture(item.home_fixture_id)
        zone = self._registry.zone(fixture.zone_id)
        return not zone.bounds.contains(item.position, margin=self._cfg.home_zone_margin_m)

    def _movement_start(self, item: ItemTrackState) -> datetime:
        """Timestamp of the first history point that left the rest neighbourhood."""
        assert item.rest_position is not None
        threshold = self._cfg.movement_threshold_m
        start = item.history[-1].timestamp if item.history else item.last_seen_at
        for point in reversed(item.history):
            if point.coordinate.horizontal_distance_to(item.rest_position) <= threshold:
                break
            start = point.timestamp
        assert start is not None
        return start

    def _apply(
        self,
        item: ItemTrackState,
        to_state: ItemState,
        now: datetime,
        reason: str,
        measurements: dict[str, float],
        movement_start_at: datetime | None = None,
        clear_movement: bool = False,
        new_rest: bool = False,
    ) -> Transition:
        transition = Transition(
            epc=item.epc.value,
            from_state=item.state,
            to_state=to_state,
            timestamp=now,
            reason=reason,
            measurements=measurements,
        )
        item.state = to_state
        item.state_since = now
        item.at_rest_since = None
        if movement_start_at is not None:
            item.movement_start_at = movement_start_at
        if clear_movement:
            item.movement_start_at = None
        if new_rest:
            item.rest_position = item.position
            item.rest_state = to_state
            item.reads_beyond_threshold = 0
            item.movement_start_at = None
            item.attribution_unresolved = False
            item.carry_announced = False
        return transition
