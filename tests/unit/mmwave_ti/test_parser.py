from __future__ import annotations

import math
import random
import struct

import pytest

from radiowave.adapters.mmwave.ti.models import TiFrame
from radiowave.adapters.mmwave.ti.parser import (
    TiFrameParser,
    TiFrameRejectReason,
    TiParserLimits,
)
from radiowave.adapters.mmwave.ti.protocol import (
    FRAME_HEADER_BYTES,
    MAGIC_WORD,
    TI_3D_PEOPLE_COUNTING,
    TI_OOB_SDK3,
    TiFirmwareProfile,
    TiTlvType,
)
from tests.fixtures.ti_mmwave.builder import (
    build_frame,
    build_point_cloud_tlv,
    build_target_frame,
    build_target_record,
    build_tlv,
)


def test_single_target_frame_decodes_all_fields_and_counts_padding() -> None:
    record = build_target_record(
        tid=7, x=1.0, y=2.0, z=0.5, vx=0.1, vy=-0.2, vz=0.0, confidence=0.75
    )
    tlv = build_tlv(TiTlvType.TARGET_LIST_3D, record)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv], pad_to=64)

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    frame = frames[0]
    assert frame.frame_number == 1
    assert frame.target_record_layout == "3d_v2"
    assert frame.total_packet_len == len(frame_bytes)
    assert frame.padding_bytes == 32
    assert len(frame.targets) == 1
    target = frame.targets[0]
    assert target.native_track_id == 7
    assert target.x == pytest.approx(1.0)
    assert target.y == pytest.approx(2.0)
    assert target.z == pytest.approx(0.5)
    assert target.vx == pytest.approx(0.1)
    assert target.vy == pytest.approx(-0.2)
    assert target.confidence == pytest.approx(0.75)
    assert len(target.error_covariance) == 16


def test_multi_target_frame_preserves_order_and_all_records() -> None:
    frame_bytes = build_target_frame(
        frame_number=2,
        targets=[
            {"tid": 1, "x": 1.0},
            {"tid": 2, "x": 2.0},
            {"tid": 3, "x": 3.0},
        ],
    )

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    targets = frames[0].targets
    assert [t.native_track_id for t in targets] == [1, 2, 3]
    assert [t.x for t in targets] == pytest.approx([1.0, 2.0, 3.0])


def test_point_cloud_target_index_height_and_presence_frame() -> None:
    cloud_tlv = build_tlv(
        TiTlvType.POINT_CLOUD_3D,
        build_point_cloud_tlv([(0, 0, 100, 500, 200), (0, 50, -100, 1000, 150)]),
    )
    index_tlv = build_tlv(TiTlvType.TARGET_INDEX, bytes([0, 1]))
    height_tlv = build_tlv(TiTlvType.TARGET_HEIGHT, struct.pack("<B3xff", 0, 1.8, 0.1))
    presence_tlv = build_tlv(TiTlvType.PRESENCE_INDICATION, struct.pack("<I", 1))
    frame_bytes = build_frame(frame_number=3, tlvs=[cloud_tlv, index_tlv, height_tlv, presence_tlv])

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    frame = frames[0]
    assert frame.presence == 1
    assert frame.target_indices == (0, 1)
    assert len(frame.heights) == 1
    assert frame.heights[0].native_track_id == 0
    assert frame.heights[0].max_z == pytest.approx(1.8)
    assert frame.heights[0].min_z == pytest.approx(0.1)
    assert len(frame.points) == 2
    first, second = frame.points
    assert first.range_m == pytest.approx(5.0)
    assert first.azimuth_rad == pytest.approx(0.0)
    assert first.x == pytest.approx(0.0, abs=1e-9)
    assert first.y == pytest.approx(5.0)
    assert first.doppler_mps == pytest.approx(1.0)
    assert first.snr == pytest.approx(200.0)
    assert second.range_m == pytest.approx(10.0)
    assert second.azimuth_rad == pytest.approx(0.5)
    assert second.x == pytest.approx(10.0 * math.sin(0.5) * math.cos(0.0))
    assert second.y == pytest.approx(10.0 * math.cos(0.5) * math.cos(0.0))
    assert second.doppler_mps == pytest.approx(-1.0)
    assert second.snr == pytest.approx(150.0)


def test_frame_split_across_two_chunks_is_parsed_once_complete() -> None:
    frame_bytes = build_target_frame(frame_number=4, targets=[{"tid": 1}])
    parser = TiFrameParser()
    split = len(frame_bytes) // 2

    assert parser.feed(frame_bytes[:split]) == []
    frames = parser.feed(frame_bytes[split:])

    assert len(frames) == 1
    assert frames[0].frame_number == 4


def test_frame_fed_one_byte_at_a_time_matches_single_feed() -> None:
    frame_bytes = build_target_frame(frame_number=5, targets=[{"tid": 2, "x": 3.0}])
    whole = TiFrameParser().feed(frame_bytes)

    parser = TiFrameParser()
    collected: list[TiFrame] = []
    for i in range(len(frame_bytes)):
        collected.extend(parser.feed(frame_bytes[i : i + 1]))

    assert collected == whole


def test_two_frames_in_one_chunk_are_returned_in_order() -> None:
    frame_a = build_target_frame(frame_number=10, targets=[{"tid": 1}])
    frame_b = build_target_frame(frame_number=11, targets=[{"tid": 2}])

    frames = TiFrameParser().feed(frame_a + frame_b)

    assert [f.frame_number for f in frames] == [10, 11]


def test_garbage_prefix_before_a_valid_frame_is_discarded_and_counted() -> None:
    frame_bytes = build_target_frame(frame_number=6, targets=[{"tid": 1}])
    garbage = b"\xff" * 17
    parser = TiFrameParser()

    frames = parser.feed(garbage + frame_bytes)

    assert len(frames) == 1
    assert frames[0].frame_number == 6
    assert parser.stats.bytes_discarded == len(garbage)
    assert parser.stats.resyncs >= 1


def test_garbage_interleaved_between_two_valid_frames_is_skipped() -> None:
    frame_a = build_target_frame(frame_number=20, targets=[{"tid": 1}])
    frame_b = build_target_frame(frame_number=21, targets=[{"tid": 2}])
    garbage = b"\x11" * 23
    parser = TiFrameParser()

    frames = parser.feed(frame_a + garbage + frame_b)

    assert [f.frame_number for f in frames] == [20, 21]
    assert parser.stats.bytes_discarded == len(garbage)


def test_unknown_tlv_is_retained_and_rest_of_frame_still_parsed() -> None:
    known_tlv = build_tlv(
        TiTlvType.TARGET_LIST_3D,
        build_target_record(tid=9, x=0.0, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0),
    )
    unknown_tlv = build_tlv(9999, b"\x01\x02\x03\x04")
    frame_bytes = build_frame(frame_number=7, tlvs=[known_tlv, unknown_tlv])
    parser = TiFrameParser()

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    frame = frames[0]
    assert len(frame.targets) == 1
    assert frame.unknown_tlvs == ((9999, 4),)
    assert parser.stats.unknown_tlv_count == 1


def test_total_packet_len_smaller_than_header_is_rejected_and_resyncs() -> None:
    bad_tlv = build_tlv(
        TiTlvType.TARGET_LIST_3D,
        build_target_record(tid=1, x=0.0, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0),
    )
    bad_frame = build_frame(frame_number=7, tlvs=[bad_tlv], total_len_override=10)
    good_frame = build_target_frame(frame_number=8, targets=[{"tid": 2}])
    parser = TiFrameParser()

    frames = parser.feed(bad_frame + good_frame)

    assert [f.frame_number for f in frames] == [8]
    assert parser.stats.frames_rejected >= 1
    assert parser.stats.reject_reasons.get(TiFrameRejectReason.BAD_PACKET_LENGTH.value, 0) >= 1
    assert parser.stats.resyncs >= 1


def test_packet_too_large_is_rejected_without_waiting_for_more_bytes() -> None:
    limits = TiParserLimits(max_packet_bytes=200)
    tlv = build_tlv(
        TiTlvType.TARGET_LIST_3D,
        build_target_record(tid=1, x=0.0, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0),
    )
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv], total_len_override=100_000)
    parser = TiFrameParser(limits=limits)

    # Only the 40-byte header is fed: nowhere near total_len_override bytes.
    frames = parser.feed(frame_bytes[:40])

    assert frames == []
    assert parser.stats.frames_rejected == 1
    assert parser.stats.reject_reasons[TiFrameRejectReason.PACKET_TOO_LARGE.value] == 1


def test_tlv_length_overrun_is_rejected_and_next_frame_parsed() -> None:
    bad_tlv = build_tlv(TiTlvType.PRESENCE_INDICATION, struct.pack("<I", 1), length_override=999)
    bad_frame = build_frame(frame_number=30, tlvs=[bad_tlv])
    good_frame = build_target_frame(frame_number=31, targets=[{"tid": 1}])
    parser = TiFrameParser()

    frames = parser.feed(bad_frame + good_frame)

    assert [f.frame_number for f in frames] == [31]
    assert parser.stats.reject_reasons.get(TiFrameRejectReason.TLV_OVERRUN.value, 0) >= 1


def test_truncated_frame_is_recovered_when_a_complete_frame_follows() -> None:
    truncated = build_target_frame(frame_number=40, targets=[{"tid": 1}])[:20]
    good_frame = build_target_frame(frame_number=41, targets=[{"tid": 2}])
    parser = TiFrameParser()

    first = parser.feed(truncated)
    assert first == []

    frames = parser.feed(good_frame)

    assert [f.frame_number for f in frames] == [41]
    assert parser.stats.bytes_discarded > 0 or parser.stats.frames_rejected > 0


def test_num_tlvs_exceeding_limit_is_rejected() -> None:
    limits = TiParserLimits(max_tlvs=1)
    tlv = build_tlv(TiTlvType.PRESENCE_INDICATION, struct.pack("<I", 1))
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv, tlv])
    parser = TiFrameParser(limits=limits)

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.TOO_MANY_TLVS.value] == 1


def test_too_many_targets_is_rejected() -> None:
    limits = TiParserLimits(max_targets=2)
    frame_bytes = build_target_frame(frame_number=1, targets=[{"tid": i} for i in range(3)])
    parser = TiFrameParser(limits=limits)

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.TOO_MANY_TARGETS.value] == 1


def test_non_finite_target_value_rejects_the_whole_frame() -> None:
    frame_bytes = build_target_frame(frame_number=1, targets=[{"tid": 1, "x": math.nan}])
    parser = TiFrameParser()

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.NON_FINITE_VALUE.value] == 1


def test_duplicate_frame_numbers_are_both_returned_deduplication_is_not_the_parsers_job() -> None:
    frame_a = build_target_frame(frame_number=99, targets=[{"tid": 1}])
    frame_b = build_target_frame(frame_number=99, targets=[{"tid": 2}])

    frames = TiFrameParser().feed(frame_a + frame_b)

    assert [f.frame_number for f in frames] == [99, 99]


def test_empty_target_list_tlv_yields_zero_targets() -> None:
    tlv = build_tlv(TiTlvType.TARGET_LIST_3D, b"")
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].targets == ()


def test_2d_layout_is_auto_detected_from_payload_length() -> None:
    record = build_target_record(tid=1, x=1.0, y=2.0, z=0.0, vx=0.0, vy=0.0, vz=0.0, layout="2d")
    tlv = build_tlv(TiTlvType.TARGET_LIST_3D, record)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    frame = frames[0]
    assert frame.target_record_layout == "2d"
    assert frame.targets[0].z == 0.0
    assert frame.targets[0].vz == 0.0
    assert frame.targets[0].confidence is None
    assert len(frame.targets[0].error_covariance) == 9


def test_3d_v1_layout_is_auto_detected_from_payload_length() -> None:
    record = build_target_record(tid=1, x=1.0, y=2.0, z=3.0, vx=0.0, vy=0.0, vz=0.0, layout="3d_v1")
    tlv = build_tlv(TiTlvType.TARGET_LIST_3D, record)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    frame = frames[0]
    assert frame.target_record_layout == "3d_v1"
    assert frame.targets[0].confidence is None
    assert len(frame.targets[0].error_covariance) == 9


def test_forced_target_record_layout_mismatch_is_rejected() -> None:
    record = build_target_record(tid=1, x=0.0, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0, layout="2d")
    tlv = build_tlv(TiTlvType.TARGET_LIST_3D, record)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    profile = TiFirmwareProfile(name="forced-3d-v2", target_record_layout="3d_v2")
    parser = TiFrameParser(profile=profile)

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.BAD_TARGET_RECORD.value] == 1


def test_explicit_target_record_layout_is_accepted() -> None:
    """Invariant: a packet that matches the configured firmware profile's pinned
    layout is decoded exactly with it, never re-derived by length-guessing."""
    record = build_target_record(tid=1, x=1.0, y=2.0, z=3.0, vx=0.0, vy=0.0, vz=0.0, layout="3d_v1")
    tlv = build_tlv(TiTlvType.TARGET_LIST_3D, record)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    profile = TiFirmwareProfile(name="forced-3d-v1", target_record_layout="3d_v1")
    parser = TiFrameParser(profile=profile)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].target_record_layout == "3d_v1"
    assert len(frames[0].targets) == 1


def test_ambiguous_target_record_length_is_rejected_when_layout_unset() -> None:
    """Invariant: ambiguous sensor bytes never become canonical observations.

    560 bytes is simultaneously 7 records of 3d_v1 (80 B) and 5 records of 3d_v2
    (112 B); with no pinned layout the parser must refuse to guess.
    """
    records = b"".join(
        build_target_record(tid=i, x=float(i), y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0, layout="3d_v1")
        for i in range(7)
    )
    assert len(records) == 560
    tlv = build_tlv(TiTlvType.TARGET_LIST_3D, records)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    parser = TiFrameParser()

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.frames_parsed == 0
    assert parser.stats.reject_reasons[TiFrameRejectReason.AMBIGUOUS_TARGET_RECORD.value] == 1


def test_ambiguous_length_decodes_correctly_once_layout_is_pinned() -> None:
    """Same 560-byte payload as above, but pinning the layout removes the ambiguity
    entirely and decodes all 7 records."""
    records = b"".join(
        build_target_record(tid=i, x=float(i), y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0, layout="3d_v1")
        for i in range(7)
    )
    tlv = build_tlv(TiTlvType.TARGET_LIST_3D, records)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    profile = TiFirmwareProfile(name="forced-3d-v1", target_record_layout="3d_v1")
    parser = TiFrameParser(profile=profile)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].target_record_layout == "3d_v1"
    assert len(frames[0].targets) == 7
    assert [t.native_track_id for t in frames[0].targets] == list(range(7))


def test_parser_continues_with_next_valid_frame_after_an_ambiguous_one() -> None:
    records = b"".join(
        build_target_record(tid=i, x=float(i), y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0, layout="3d_v1")
        for i in range(7)
    )
    ambiguous_tlv = build_tlv(TiTlvType.TARGET_LIST_3D, records)
    ambiguous_frame = build_frame(frame_number=50, tlvs=[ambiguous_tlv])
    good_frame = build_target_frame(frame_number=51, targets=[{"tid": 1}])
    parser = TiFrameParser()

    frames = parser.feed(ambiguous_frame + good_frame)

    assert [f.frame_number for f in frames] == [51]
    assert parser.stats.reject_reasons[TiFrameRejectReason.AMBIGUOUS_TARGET_RECORD.value] == 1


def test_tlv_length_includes_header_profile_variant_decodes_correctly() -> None:
    profile = TiFirmwareProfile(name="sdk2-style", tlv_length_includes_header=True)
    payload = struct.pack("<I", 1)
    tlv = build_tlv(TiTlvType.PRESENCE_INDICATION, payload, length_override=len(payload) + 8)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    parser = TiFrameParser(profile=profile)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].presence == 1


def test_oob_detected_points_tlv_decodes_to_point_cloud_points() -> None:
    payload = struct.pack("<4f", 1.0, 2.0, 0.0, 0.5)  # x, y, z, velocity
    tlv = build_tlv(TiTlvType.DETECTED_POINTS, payload)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    parser = TiFrameParser(profile=TI_OOB_SDK3)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    points = frames[0].points
    assert len(points) == 1
    point = points[0]
    assert point.x == pytest.approx(1.0)
    assert point.y == pytest.approx(2.0)
    assert point.z == pytest.approx(0.0)
    assert point.doppler_mps == pytest.approx(0.5)
    assert point.range_m == pytest.approx(math.sqrt(1.0**2 + 2.0**2))
    assert point.snr == pytest.approx(0.0)  # no side-info TLV present


def test_aligned_total_packet_len_is_accepted() -> None:
    frame_bytes = build_target_frame(frame_number=1, targets=[{"tid": 1}])
    assert len(frame_bytes) % TI_3D_PEOPLE_COUNTING.packet_alignment == 0
    parser = TiFrameParser()

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].frame_number == 1


def test_misaligned_total_packet_len_is_rejected_and_next_frame_still_parses() -> None:
    """Invariant: a packet that violates the configured firmware profile (here, its
    packet alignment) is rejected before consuming future frames."""
    tlv = build_tlv(
        TiTlvType.TARGET_LIST_3D,
        build_target_record(tid=1, x=0.0, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0),
    )
    bad_frame = build_frame(frame_number=70, tlvs=[tlv], total_len_override=45)
    good_frame = build_target_frame(frame_number=71, targets=[{"tid": 2}])
    parser = TiFrameParser()

    frames = parser.feed(bad_frame + good_frame)

    assert [f.frame_number for f in frames] == [71]
    assert parser.stats.frames_parsed == 1
    assert parser.stats.reject_reasons[TiFrameRejectReason.BAD_PACKET_ALIGNMENT.value] >= 1


# POINT_CLOUD_3D (people-counting family) and DETECTED_POINTS (OOB family) never
# appear together in one real packet once TiFrameRejectReason.TLV_NOT_IN_PROFILE
# is enforced (see the TLV-family tests further down) — no shipped profile permits
# both. The two tests below only exercise the *combined-bound arithmetic* itself
# (points from two different TLVs summing past the limit together), which is a
# property of the parser's bookkeeping independent of firmware-family policy, so
# they deliberately use an ad-hoc profile with no family restriction
# (``tlv_family=None``) rather than either named real-world profile.
_UNRESTRICTED_FAMILY_TEST_PROFILE = TiFirmwareProfile(name="test-any-family")


def test_combined_cloud_and_oob_points_over_limit_is_rejected() -> None:
    """Invariant: parser bounds apply to the final combined outputs, not individual
    TLVs only — 5 + 5 each fit ``max_points`` alone but not together."""
    limits = TiParserLimits(max_points=8)
    cloud_tlv = build_tlv(
        TiTlvType.POINT_CLOUD_3D,
        build_point_cloud_tlv([(0, 0, 10, 100, 50)] * 5),
    )
    oob_tlv = build_tlv(TiTlvType.DETECTED_POINTS, struct.pack("<4f", 1.0, 1.0, 0.0, 0.0) * 5)
    frame_bytes = build_frame(frame_number=1, tlvs=[cloud_tlv, oob_tlv])
    parser = TiFrameParser(profile=_UNRESTRICTED_FAMILY_TEST_PROFILE, limits=limits)

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.TOO_MANY_POINTS.value] == 1


def test_combined_cloud_and_oob_points_at_limit_is_accepted() -> None:
    limits = TiParserLimits(max_points=8)
    cloud_tlv = build_tlv(
        TiTlvType.POINT_CLOUD_3D,
        build_point_cloud_tlv([(0, 0, 10, 100, 50)] * 4),
    )
    oob_tlv = build_tlv(TiTlvType.DETECTED_POINTS, struct.pack("<4f", 1.0, 1.0, 0.0, 0.0) * 4)
    frame_bytes = build_frame(frame_number=1, tlvs=[cloud_tlv, oob_tlv])
    parser = TiFrameParser(profile=_UNRESTRICTED_FAMILY_TEST_PROFILE, limits=limits)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert len(frames[0].points) == 8


def test_endless_garbage_never_raises_and_stays_within_buffer_limit() -> None:
    limits = TiParserLimits()
    parser = TiFrameParser(limits=limits)
    rng = random.Random(0)
    remaining = 1024 * 1024
    chunk_size = 4096

    while remaining > 0:
        parser.feed(rng.randbytes(chunk_size))
        assert parser.buffered_bytes <= limits.max_buffer_bytes
        remaining -= chunk_size


def test_reset_clears_the_buffer() -> None:
    parser = TiFrameParser()
    parser.feed(b"\x00" * 10)
    assert parser.buffered_bytes > 0

    parser.reset()

    assert parser.buffered_bytes == 0


def test_duplicate_target_list_tlv_in_one_packet_is_rejected_with_no_frame_emitted() -> None:
    """Invariant: a TLV type repeated within one packet is ambiguous and must be
    rejected, never resolved by picking a winner."""
    first = build_tlv(
        TiTlvType.TARGET_LIST_3D,
        build_target_record(tid=1, x=0.0, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0),
    )
    second = build_tlv(
        TiTlvType.TARGET_LIST_3D,
        build_target_record(tid=2, x=1.0, y=1.0, z=0.0, vx=0.0, vy=0.0, vz=0.0),
    )
    frame_bytes = build_frame(frame_number=1, tlvs=[first, second])
    parser = TiFrameParser()

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.DUPLICATE_TLV.value] == 1


def test_duplicate_unknown_tlv_type_in_one_packet_still_parses() -> None:
    """Unknown TLV types are never decoded into a combined output, so a repeat of
    one is merely listed twice rather than rejected."""
    unknown_a = build_tlv(9999, b"\x01\x02\x03\x04")
    unknown_b = build_tlv(9999, b"\x05\x06\x07\x08")
    frame_bytes = build_frame(frame_number=1, tlvs=[unknown_a, unknown_b])
    parser = TiFrameParser()

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].unknown_tlvs == ((9999, 4), (9999, 4))


def test_target_height_uses_the_ti_twelve_byte_record() -> None:
    """The real TI wire format for TARGET_HEIGHT (TLV 1012) is 12 bytes per record,
    matching TI's native 'B2f' alignment rather than a hand-packed 9-byte record: a
    uint8 id, three bytes of C struct padding, then two float32s. The parser reads the
    id from offset 0 and skips the padding, so this proves it decodes TI's own byte
    layout unchanged."""
    height_tlv = build_tlv(TiTlvType.TARGET_HEIGHT, struct.pack("<B3xff", 3, 1.9, 0.2))
    frame_bytes = build_frame(frame_number=1, tlvs=[height_tlv])

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    assert len(frames[0].heights) == 1
    assert frames[0].heights[0].native_track_id == 3
    assert frames[0].heights[0].max_z == pytest.approx(1.9)
    assert frames[0].heights[0].min_z == pytest.approx(0.2)


def test_nine_byte_target_height_payload_is_rejected() -> None:
    """A legacy/incorrect 9-byte-per-record payload does not satisfy the real
    12-byte alignment and must be rejected outright, never silently decoded."""
    bad_height_tlv = build_tlv(TiTlvType.TARGET_HEIGHT, struct.pack("<Bff", 0, 1.8, 0.1))
    frame_bytes = build_frame(frame_number=1, tlvs=[bad_height_tlv])
    parser = TiFrameParser()

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.BAD_TLV_LENGTH.value] == 1


def test_target_height_id_ignores_dirty_struct_padding() -> None:
    """The id is the byte at offset 0; bytes 1..3 are C struct padding and must be
    skipped, never folded into the id.

    TI's demo packs TLVs contiguously into a shared result buffer, so those padding
    bytes can carry stale data from an earlier frame rather than zeros. Reading the
    field as a uint32 would turn that into a silently wrong track id; this pins that
    a dirty-padding record still decodes the same id as a clean one.
    """
    dirty = struct.pack("<B3sff", 7, b"\xde\xad\xbe", 1.7, 0.4)
    assert len(dirty) == 12
    height_tlv = build_tlv(TiTlvType.TARGET_HEIGHT, dirty)
    frame_bytes = build_frame(frame_number=1, tlvs=[height_tlv])

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].heights[0].native_track_id == 7
    assert frames[0].heights[0].max_z == pytest.approx(1.7)
    assert frames[0].heights[0].min_z == pytest.approx(0.4)


def test_multi_record_target_height_decodes_at_correct_offsets() -> None:
    payload = struct.pack("<B3xff", 0, 1.8, 0.1) + struct.pack("<B3xff", 1, 2.2, 0.3)
    assert len(payload) == 24
    height_tlv = build_tlv(TiTlvType.TARGET_HEIGHT, payload)
    frame_bytes = build_frame(frame_number=1, tlvs=[height_tlv])

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    heights = frames[0].heights
    assert len(heights) == 2
    assert heights[0].native_track_id == 0
    assert heights[0].max_z == pytest.approx(1.8)
    assert heights[0].min_z == pytest.approx(0.1)
    assert heights[1].native_track_id == 1
    assert heights[1].max_z == pytest.approx(2.2)
    assert heights[1].min_z == pytest.approx(0.3)


def test_frame_with_target_list_and_target_height_tlv_yields_targets() -> None:
    """Regression for the P1 bug: a wrong 9-byte height record size caused a valid
    12-byte height payload to fail its modulo check and reject the whole frame,
    including an otherwise-valid target list. With the correct 12-byte record size
    both TLVs decode together."""
    target_tlv = build_tlv(
        TiTlvType.TARGET_LIST_3D,
        build_target_record(tid=5, x=1.0, y=2.0, z=0.0, vx=0.0, vy=0.0, vz=0.0),
    )
    height_tlv = build_tlv(TiTlvType.TARGET_HEIGHT, struct.pack("<B3xff", 5, 1.8, 0.1))
    frame_bytes = build_frame(frame_number=1, tlvs=[target_tlv, height_tlv])

    frames = TiFrameParser().feed(frame_bytes)

    assert len(frames) == 1
    assert len(frames[0].targets) == 1
    assert frames[0].targets[0].native_track_id == 5
    assert len(frames[0].heights) == 1
    assert frames[0].heights[0].native_track_id == 5


def test_target_index_over_max_points_is_rejected() -> None:
    limits = TiParserLimits(max_points=4)
    index_tlv = build_tlv(TiTlvType.TARGET_INDEX, bytes(range(5)))
    frame_bytes = build_frame(frame_number=1, tlvs=[index_tlv])
    parser = TiFrameParser(limits=limits)

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.TOO_MANY_POINTS.value] == 1


def test_target_index_at_max_points_is_accepted() -> None:
    limits = TiParserLimits(max_points=4)
    index_tlv = build_tlv(TiTlvType.TARGET_INDEX, bytes(range(4)))
    frame_bytes = build_frame(frame_number=1, tlvs=[index_tlv])
    parser = TiFrameParser(limits=limits)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].target_indices == (0, 1, 2, 3)


def test_parser_recovers_after_an_oversized_target_index() -> None:
    limits = TiParserLimits(max_points=4)
    oversized_index_tlv = build_tlv(TiTlvType.TARGET_INDEX, bytes(range(5)))
    bad_frame = build_frame(frame_number=80, tlvs=[oversized_index_tlv])
    good_frame = build_target_frame(frame_number=81, targets=[{"tid": 1}])
    parser = TiFrameParser(limits=limits)

    frames = parser.feed(bad_frame + good_frame)

    assert [f.frame_number for f in frames] == [81]
    assert parser.stats.reject_reasons[TiFrameRejectReason.TOO_MANY_POINTS.value] == 1


def test_target_height_over_max_targets_is_rejected() -> None:
    limits = TiParserLimits(max_targets=4)
    payload = b"".join(struct.pack("<B3xff", i, 1.8, 0.1) for i in range(5))
    height_tlv = build_tlv(TiTlvType.TARGET_HEIGHT, payload)
    frame_bytes = build_frame(frame_number=1, tlvs=[height_tlv])
    parser = TiFrameParser(limits=limits)

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.TOO_MANY_TARGETS.value] == 1


def test_target_height_at_max_targets_is_accepted() -> None:
    limits = TiParserLimits(max_targets=4)
    payload = b"".join(struct.pack("<B3xff", i, 1.8, 0.1) for i in range(4))
    height_tlv = build_tlv(TiTlvType.TARGET_HEIGHT, payload)
    frame_bytes = build_frame(frame_number=1, tlvs=[height_tlv])
    parser = TiFrameParser(limits=limits)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert len(frames[0].heights) == 4


def test_parser_recovers_after_an_oversized_target_height() -> None:
    limits = TiParserLimits(max_targets=4)
    payload = b"".join(struct.pack("<B3xff", i, 1.8, 0.1) for i in range(5))
    oversized_height_tlv = build_tlv(TiTlvType.TARGET_HEIGHT, payload)
    bad_frame = build_frame(frame_number=90, tlvs=[oversized_height_tlv])
    good_frame = build_target_frame(frame_number=91, targets=[{"tid": 1}])
    parser = TiFrameParser(limits=limits)

    frames = parser.feed(bad_frame + good_frame)

    assert [f.frame_number for f in frames] == [91]
    assert parser.stats.reject_reasons[TiFrameRejectReason.TOO_MANY_TARGETS.value] == 1


def test_next_valid_frame_after_a_duplicate_tlv_packet_is_parsed() -> None:
    first = build_tlv(
        TiTlvType.TARGET_LIST_3D,
        build_target_record(tid=1, x=0.0, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0),
    )
    second = build_tlv(
        TiTlvType.TARGET_LIST_3D,
        build_target_record(tid=2, x=1.0, y=1.0, z=0.0, vx=0.0, vy=0.0, vz=0.0),
    )
    bad_frame = build_frame(frame_number=60, tlvs=[first, second])
    good_frame = build_target_frame(frame_number=61, targets=[{"tid": 3}])
    parser = TiFrameParser()

    frames = parser.feed(bad_frame + good_frame)

    assert [f.frame_number for f in frames] == [61]
    assert parser.stats.reject_reasons[TiFrameRejectReason.DUPLICATE_TLV.value] == 1


# --- side-info record bound + cross-check (P2 fix 1) -------------------------------


def test_side_info_record_count_over_max_points_is_rejected() -> None:
    """A side-info payload's record count must be bounded before it is ever
    materialized into a list — a small (or absent) DETECTED_POINTS TLV alongside
    a thousands-strong side-info TLV must not bypass max_points."""
    limits = TiParserLimits(max_points=4)
    side_info_payload = struct.pack("<2h", 0, 0) * 5  # 5 records > max_points=4
    tlv = build_tlv(TiTlvType.DETECTED_POINTS_SIDE_INFO, side_info_payload)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    parser = TiFrameParser(profile=TI_OOB_SDK3, limits=limits)

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.TOO_MANY_POINTS.value] == 1


def test_side_info_record_count_at_max_points_is_accepted() -> None:
    limits = TiParserLimits(max_points=4)
    detected_payload = struct.pack("<4f", 1.0, 1.0, 0.0, 0.0) * 4
    side_info_payload = struct.pack("<2h", 50, 5) * 4
    detected_tlv = build_tlv(TiTlvType.DETECTED_POINTS, detected_payload)
    side_info_tlv = build_tlv(TiTlvType.DETECTED_POINTS_SIDE_INFO, side_info_payload)
    frame_bytes = build_frame(frame_number=1, tlvs=[detected_tlv, side_info_tlv])
    parser = TiFrameParser(profile=TI_OOB_SDK3, limits=limits)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert len(frames[0].points) == 4
    assert frames[0].points[0].snr == pytest.approx(5.0)


def test_mismatched_side_info_and_detected_point_counts_is_rejected() -> None:
    """Invariant: when both TLVs are present, a record-count mismatch means the
    packet is corrupt — neither array is "authoritative" over the other."""
    detected_payload = struct.pack("<4f", 1.0, 1.0, 0.0, 0.0) * 2
    side_info_payload = struct.pack("<2h", 0, 0) * 3
    detected_tlv = build_tlv(TiTlvType.DETECTED_POINTS, detected_payload)
    side_info_tlv = build_tlv(TiTlvType.DETECTED_POINTS_SIDE_INFO, side_info_payload)
    frame_bytes = build_frame(frame_number=1, tlvs=[detected_tlv, side_info_tlv])
    parser = TiFrameParser(profile=TI_OOB_SDK3)

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.BAD_TLV_LENGTH.value] == 1


def test_parser_recovers_after_an_oversized_side_info_tlv() -> None:
    limits = TiParserLimits(max_points=4)
    oversized_side_info_tlv = build_tlv(
        TiTlvType.DETECTED_POINTS_SIDE_INFO, struct.pack("<2h", 0, 0) * 5
    )
    bad_frame = build_frame(frame_number=120, tlvs=[oversized_side_info_tlv])
    good_frame = build_frame(
        frame_number=121,
        tlvs=[build_tlv(TiTlvType.DETECTED_POINTS, struct.pack("<4f", 1.0, 1.0, 0.0, 0.0))],
    )
    parser = TiFrameParser(profile=TI_OOB_SDK3, limits=limits)

    frames = parser.feed(bad_frame + good_frame)

    assert [f.frame_number for f in frames] == [121]
    assert parser.stats.reject_reasons[TiFrameRejectReason.TOO_MANY_POINTS.value] == 1


# --- packet tail / padding validation (P2 fix 2) ------------------------------------


def test_packet_tail_containing_embedded_magic_word_is_rejected_and_next_frame_recovers() -> None:
    """Regression: a corrupted total_packet_len whose tail swallows the next
    packet's magic word must not be accepted as ordinary padding — proving the
    actual data-loss defect requires showing the following genuinely valid frame
    is still recovered from the buffer afterwards, not merely that this one is
    rejected."""
    tlv = build_tlv(TiTlvType.PRESENCE_INDICATION, struct.pack("<I", 1))
    body_len = FRAME_HEADER_BYTES + len(tlv)
    tail = MAGIC_WORD + b"\x00" * 4  # embeds a full magic word inside the "padding"
    declared_total_len = body_len + len(tail)
    assert declared_total_len % TI_3D_PEOPLE_COUNTING.packet_alignment == 0
    frame_a = (
        build_frame(frame_number=100, tlvs=[tlv], total_len_override=declared_total_len, pad_to=0)
        + tail
    )
    good_frame = build_target_frame(frame_number=101, targets=[{"tid": 1}])
    parser = TiFrameParser()

    frames = parser.feed(frame_a + good_frame)

    assert [f.frame_number for f in frames] == [101]
    assert parser.stats.reject_reasons[TiFrameRejectReason.BAD_PACKET_PADDING.value] >= 1


def test_over_long_packet_tail_is_rejected() -> None:
    """A tail far larger than any plausible alignment padding is corruption, not
    filler, and must be rejected even with no embedded magic word inside it."""
    tlv = build_tlv(TiTlvType.PRESENCE_INDICATION, struct.pack("<I", 1))
    body_len = FRAME_HEADER_BYTES + len(tlv)  # 52
    tail_len = 76  # >= 2 * packet_alignment (64): implausibly long for real padding
    declared_total_len = body_len + tail_len
    assert declared_total_len % TI_3D_PEOPLE_COUNTING.packet_alignment == 0
    frame_bytes = (
        build_frame(frame_number=1, tlvs=[tlv], total_len_override=declared_total_len, pad_to=0)
        + b"\x00" * tail_len
    )
    parser = TiFrameParser()

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.BAD_PACKET_PADDING.value] == 1


def test_small_alignment_padding_tail_still_parses() -> None:
    """Ordinary alignment-rounding padding, well under the corruption-sized bound
    introduced above, is legitimate and must still parse."""
    tlv = build_tlv(TiTlvType.PRESENCE_INDICATION, struct.pack("<I", 1))
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    parser = TiFrameParser()

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].padding_bytes == 12
    assert frames[0].presence == 1


# --- TLV-type / firmware-profile family enforcement (P2 fix 3) ---------------------


def test_oob_detected_points_tlv_under_people_counting_profile_is_rejected() -> None:
    """Regression: an OOB-flashed sensor configured with the people-counting
    profile decodes type-1 DETECTED_POINTS as if it belonged, leaving
    frame.targets empty with no observation ever emitted while the session still
    reports healthy parsed frames. This must surface as a firmware/configuration
    error instead."""
    payload = struct.pack("<4f", 1.0, 2.0, 0.0, 0.5)
    tlv = build_tlv(TiTlvType.DETECTED_POINTS, payload)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    parser = TiFrameParser(profile=TI_3D_PEOPLE_COUNTING)

    frames = parser.feed(frame_bytes)

    assert frames == []
    assert parser.stats.reject_reasons[TiFrameRejectReason.TLV_NOT_IN_PROFILE.value] == 1


def test_oob_detected_points_tlv_under_oob_profile_still_parses() -> None:
    """The same TLV type is exactly the OOB profile's own vocabulary and must
    keep parsing normally under it."""
    payload = struct.pack("<4f", 1.0, 2.0, 0.0, 0.5)
    tlv = build_tlv(TiTlvType.DETECTED_POINTS, payload)
    frame_bytes = build_frame(frame_number=1, tlvs=[tlv])
    parser = TiFrameParser(profile=TI_OOB_SDK3)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert len(frames[0].points) == 1


def test_unknown_tlv_type_is_tolerated_under_people_counting_profile() -> None:
    """The family check must never reach genuinely unrecognized TLV types — they
    keep their existing tolerated (listed-only, never decoded) behaviour."""
    unknown_tlv = build_tlv(9999, b"\x01\x02\x03\x04")
    frame_bytes = build_frame(frame_number=1, tlvs=[unknown_tlv])
    parser = TiFrameParser(profile=TI_3D_PEOPLE_COUNTING)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].unknown_tlvs == ((9999, 4),)


def test_unknown_tlv_type_is_tolerated_under_oob_profile() -> None:
    unknown_tlv = build_tlv(9999, b"\x01\x02\x03\x04")
    frame_bytes = build_frame(frame_number=1, tlvs=[unknown_tlv])
    parser = TiFrameParser(profile=TI_OOB_SDK3)

    frames = parser.feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].unknown_tlvs == ((9999, 4),)
