"""All tunable thresholds for fusion v0 live here, never inline in the algorithms."""

from __future__ import annotations

import math

from pydantic import Field, model_validator

from radiowave.contracts._base import FrozenModel


class PersonTrackingConfig(FrozenModel):
    merge_radius_m: float = Field(
        default=0.7,
        description="A new native track within this distance of an existing canonical track "
        "(observed by a different sensor) is the same person",
    )
    lost_after_s: float = Field(default=1.0, description="No observation for this long -> LOST")
    reacquire_window_s: float = Field(
        default=6.0, description="A LOST track may be re-acquired within this window"
    )
    reacquire_base_radius_m: float = Field(default=0.8)
    reacquire_growth_m_per_s: float = Field(
        default=1.2, description="Re-acquisition gate grows by this much per second lost"
    )
    end_after_s: float = Field(default=8.0, description="LOST for this long -> ENDED")
    same_sensor_exclusion_s: float = Field(
        default=0.3,
        description="A canonical track updated by sensor X within this window cannot absorb "
        "a second native track from sensor X",
    )
    velocity_window_s: float = Field(default=0.6)
    prediction_horizon_s: float = Field(
        default=1.0,
        description="Maximum dead-reckoning horizon when a LOST track's position is needed",
    )
    history_length: int = Field(
        default=600, ge=2, description="Points kept per track (~30 s at 20 Hz)"
    )


class ItemTrackingConfig(FrozenModel):
    smoothing_alpha: float = Field(
        default=0.25, gt=0.0, le=1.0, description="EMA factor on RFID location estimates"
    )
    stale_after_s: float = Field(
        default=1.5, description="No reads for this long -> position is stale, no motion inference"
    )
    reset_after_s: float = Field(
        default=3.0, description="After a gap this long the smoother restarts on the next read"
    )
    velocity_window_s: float = Field(
        default=1.5, description="Window for the half-mean velocity estimate"
    )
    rest_init_reads: int = Field(
        default=8, ge=1, description="Reads needed before the initial rest position is trusted"
    )
    history_length: int = Field(default=400, ge=4)


class StateMachineConfig(FrozenModel):
    movement_threshold_m: float = Field(
        default=0.75, description="Displacement from rest to become INTERACTION_CANDIDATE"
    )
    movement_confirm_reads: int = Field(
        default=4,
        ge=1,
        description="Consecutive evaluations, each backed by at least one new RFID read, with "
        "the smoothed estimate beyond the threshold before INTERACTION_CANDIDATE",
    )
    carry_displacement_m: float = Field(
        default=1.5, description="Displacement from rest to become CARRIED"
    )
    carry_min_duration_s: float = Field(default=1.0)
    home_zone_margin_m: float = Field(
        default=0.5, description="Leaving the home fixture bounds by this margin also implies carry"
    )
    candidate_timeout_s: float = Field(
        default=8.0, description="INTERACTION_CANDIDATE not progressing reverts after this"
    )
    rest_window_s: float = Field(
        default=2.5, description="Window over which a carried item must stop moving"
    )
    rest_displacement_m: float = Field(
        default=0.4,
        description="Half-window mean displacement below this (for the whole window) = at rest",
    )
    rest_confirm_s: float = Field(
        default=1.0, description="At-rest condition must hold this long before PUTBACK/MISPLACE"
    )
    hold_radius_m: float = Field(
        default=1.0,
        description="A resting item with any tracked person closer than this may still be held "
        "in hand, so PUTBACK/MISPLACE is deferred until they step away",
    )
    home_radius_m: float = Field(
        default=1.2, description="Rest within this distance of the home fixture bounds = PUTBACK"
    )
    exit_item_radius_m: float = Field(
        default=1.5, description="Item within this distance of an exiting carrier leaves with them"
    )
    physical_event_confidence: float = Field(
        default=0.9, description="Confidence for rest/exit facts backed by fresh reads"
    )


ASSOCIATION_FEATURES = frozenset(
    {"distance", "distance_trend", "velocity", "temporal", "co_motion", "zone", "vision"}
)


class AssociationConfig(FrozenModel):
    candidate_radius_m: float = Field(
        default=2.5,
        gt=0.0,
        description="Persons within this distance of the item become candidates",
    )
    distance_scale_m: float = Field(default=1.5, gt=0.0)
    co_motion_radius_m: float = Field(default=1.3, gt=0.0)
    co_motion_window_steps: int = Field(default=12, ge=1)
    moving_speed_m_s: float = Field(default=0.3)
    item_moving_speed_m_s: float = Field(default=0.2)
    fixture_proximity_m: float = Field(default=1.0)
    vision_match_radius_m: float = Field(default=0.7)
    vision_min_confidence: float = Field(
        default=0.6,
        ge=0.0,
        le=1.0,
        description="Vision evidence below this confidence is ignored entirely",
    )
    vision_window_s: float = Field(default=2.0)
    ledger_alpha: float = Field(default=0.3, gt=0.0, le=1.0)
    prune_score: float = Field(default=0.02)
    prune_distance_m: float = Field(default=6.0)
    pick_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "distance": 0.25,
            "distance_trend": 0.10,
            "velocity": 0.15,
            "temporal": 0.15,
            "co_motion": 0.20,
            "zone": 0.10,
            "vision": 0.30,
        },
        description="Feature weights while attributing a PICK (who took it off the fixture)",
    )
    carry_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "distance": 0.35,
            "distance_trend": 0.15,
            "velocity": 0.20,
            "co_motion": 0.30,
        },
        description="Feature weights once a carrier is committed (who has it now / HANDOFF)",
    )

    @model_validator(mode="after")
    def _weights_are_sane(self) -> AssociationConfig:
        for name, profile in (
            ("pick_weights", self.pick_weights),
            ("carry_weights", self.carry_weights),
        ):
            unknown = set(profile) - ASSOCIATION_FEATURES
            if unknown:
                msg = f"{name} has unknown features {sorted(unknown)}"
                raise ValueError(msg)
            if any(not math.isfinite(w) or w < 0.0 for w in profile.values()):
                msg = f"{name} must contain finite, non-negative weights"
                raise ValueError(msg)
            if sum(profile.values()) <= 0.0:
                msg = f"{name} must have a strictly positive total weight"
                raise ValueError(msg)
        return self


class FusionConfig(FrozenModel):
    person: PersonTrackingConfig = Field(default_factory=PersonTrackingConfig)
    item: ItemTrackingConfig = Field(default_factory=ItemTrackingConfig)
    state_machine: StateMachineConfig = Field(default_factory=StateMachineConfig)
    association: AssociationConfig = Field(default_factory=AssociationConfig)
    handoff_min_margin: float = Field(default=0.2)
    handoff_confirm_s: float = Field(default=2.0)
    carry_confirm_s: float = Field(default=3.0)
