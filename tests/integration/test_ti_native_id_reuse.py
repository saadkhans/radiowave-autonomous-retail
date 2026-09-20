"""TI native track ids are recyclable, generation-scoped hints, never global identity.

Invariants exercised end to end through :class:`~radiowave.pipeline.FoundationPipeline`:

5. Native TI track ids are never globally stable identity.
6. Reconnect/restart creates a new native hint namespace.
7. Frame identity is generation + frame + subframe.

These tests build the pipeline directly (no serial transport, no session runner): TI
frame DTOs go through :class:`TiTargetNormalizer` to observations, which are fed to
:meth:`FoundationPipeline.ingest`/``advance_to`` on an explicit clock, so everything is
deterministic and runs in well under a second.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from radiowave.adapters.mmwave.ti.adapter import NATIVE_TI_TRACK_ID_KEY, TiTargetNormalizer
from radiowave.adapters.mmwave.ti.models import TiFrame, TiTarget
from radiowave.contracts.observations import NATIVE_TRACK_KEY, PersonObservation
from radiowave.contracts.tracks import PersonTrackState
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.pipeline import FoundationPipeline
from tests.unit.mmwave_ti.support import RADAR, adapter_config, store_with_radar

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _target(tid: int, y: float, x: float = 0.0, z: float = 1.0) -> TiTarget:
    """A stationary-ish TI target; ``y`` (forward, default convention) sets world x."""
    return TiTarget(
        native_track_id=tid,
        x=x,
        y=y,
        z=z,
        vx=0.0,
        vy=0.0,
        vz=0.0,
        ax=0.0,
        ay=0.0,
        az=0.0,
        error_covariance=(),
        gating_gain=4.0,
        confidence=0.9,
    )


def _frame(number: int, ti_target: TiTarget) -> TiFrame:
    return TiFrame(
        version=0x03060000,
        total_packet_len=0,
        platform=0xA6843,
        frame_number=number,
        time_cpu_cycles=0,
        num_detected_objects=1,
        num_tlvs=1,
        subframe_number=0,
        targets=(ti_target,),
        points=(),
        target_indices=(),
        heights=(),
        presence=None,
        unknown_tlvs=(),
        target_record_layout="3d_v2",
        padding_bytes=0,
    )


def _build_pipeline() -> tuple[FoundationPipeline, TiTargetNormalizer]:
    # Radar at the origin facing world +x (yaw 0, default TI convention), so a TI
    # target directly ahead (x=0, forward-only) lands at world (y, 0, z).
    store = store_with_radar(x=0.0, y=0.0, z=2.4, yaw=0.0)
    registry = StoreRegistry(store)
    normalizer = TiTargetNormalizer(adapter_config(), registry, clock=lambda: T0)
    pipeline = FoundationPipeline(registry)
    return pipeline, normalizer


def _ingest(
    pipeline: FoundationPipeline,
    normalizer: TiTargetNormalizer,
    frame_number: int,
    tid: int,
    y: float,
    at: datetime,
) -> list[PersonObservation]:
    observations = normalizer.frame_to_observations(_frame(frame_number, _target(tid, y)), at)
    for observation in observations:
        pipeline.ingest(observation)
    return observations


def test_recycled_native_id_far_after_restart_creates_a_new_track() -> None:
    """Invariants 5 & 6: raw TI id 3 walks at world (3, 0, *) for a few frames, creating
    canonical P0001 and session S-P0001. A reconnect (``new_generation``) then hands the
    *same* raw id 3 to a person 6 m away (well beyond ``reacquire_base_radius_m`` even
    with the growth term), inside the re-acquisition window. The recycled id must not
    resurrect P0001: a fresh canonical P0002 is created instead."""
    pipeline, normalizer = _build_pipeline()
    for i in range(4):
        _ingest(pipeline, normalizer, i + 1, 3, 3.0, T0 + timedelta(seconds=0.1 * i))

    result = pipeline.result()
    assert [t.track_id for t in result.person_tracks] == ["P0001"]
    assert [s.session_id for s in result.sessions] == ["S-P0001"]

    normalizer.new_generation("reconnect")
    new_hint = f"g{normalizer.generation}:t3"
    # The new generation's hint namespace starts empty: nothing maps to it yet, and in
    # particular nothing maps it to the old canonical track before it is ever observed.
    native_map_before = dict(pipeline.fusion.persons._native_map)
    assert (RADAR, new_hint) not in native_map_before

    far_at = T0 + timedelta(seconds=2.0)
    observations = _ingest(pipeline, normalizer, 100, 3, 9.0, far_at)  # world (9, 0, *): 6 m away
    assert len(observations) == 1
    assert observations[0].metadata[NATIVE_TRACK_KEY] == new_hint
    assert observations[0].metadata[NATIVE_TI_TRACK_ID_KEY] == 3

    pipeline.finish()
    result = pipeline.result()
    assert sorted(t.track_id for t in result.person_tracks) == ["P0001", "P0002"]
    assert sorted(s.session_id for s in result.sessions) == ["S-P0001", "S-P0002"]
    assert pipeline.fusion.persons._native_map[(RADAR, new_hint)] == "P0002"


def test_native_id_continuity_holds_within_one_generation() -> None:
    """Control: with no restart, the same raw TI id continuing near its own track keeps
    hint continuity working exactly as before (invariant 5 is about identity, not about
    disabling the hint altogether)."""
    pipeline, normalizer = _build_pipeline()
    for i in range(8):
        _ingest(pipeline, normalizer, i + 1, 3, 3.0 - 0.05 * i, T0 + timedelta(seconds=0.1 * i))

    result = pipeline.result()
    assert [t.track_id for t in result.person_tracks] == ["P0001"]
    assert result.person_tracks[0].observation_count == 8


def test_recycled_id_just_inside_the_growing_continuity_gate_does_not_inherit_the_track() -> None:
    """Invariants 5 & 6, the precise regression this fix closes: an *unscoped* native-id
    hint keeps growing its re-association radius with time-since-update even while the
    old track is still ACTIVE (``PersonTrackManager._gate_radius`` with
    ``continuity=True``: ``merge_radius_m + reacquire_growth_m_per_s * silent_for``),
    which is wider than the flat ``merge_radius_m`` a hint-less/fresh claim must pass.

    After an 0.8 s gap the buggy unscoped continuity radius would be
    ``0.7 + 1.2 * 0.8 = 1.66`` m -- wide enough to swallow a recycled id reappearing
    1.2 m away -- while the general (no-hint) gate only allows 0.7 m. With the hint
    scoped to the new generation, the recycled id gets no continuity allowance at all
    and must pass the same 0.7 m gate as anyone else, so it does not inherit P0001.
    """
    pipeline, normalizer = _build_pipeline()
    for i in range(4):
        _ingest(pipeline, normalizer, i + 1, 3, 3.0, T0 + timedelta(seconds=0.1 * i))
    last_update_at = T0 + timedelta(seconds=0.3)

    track = pipeline.fusion.persons.get("P0001")
    assert track is not None
    assert track.state == PersonTrackState.ACTIVE

    normalizer.new_generation("reconnect")
    recycled_at = last_update_at + timedelta(seconds=0.8)
    observations = _ingest(pipeline, normalizer, 100, 3, 4.2, recycled_at)  # world (4.2,0,*): 1.2 m
    assert observations[0].metadata[NATIVE_TRACK_KEY] == f"g{normalizer.generation}:t3"

    result = pipeline.result()
    assert sorted(t.track_id for t in result.person_tracks) == ["P0001", "P0002"]
    original = pipeline.fusion.persons.get("P0001")
    assert original is not None
    assert original.state == PersonTrackState.ACTIVE  # the recycled id never touched it
    assert original.observation_count == 4
