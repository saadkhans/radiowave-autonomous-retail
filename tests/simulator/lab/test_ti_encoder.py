"""Encoder-level checks: real TiFrameParser accepts happy-path packets, and rejects
every deliberately-corrupt packet from the fault-injection surface with the exact
reason expected.
"""

from __future__ import annotations

import math

import pytest

from radiowave.adapters.mmwave.ti.parser import TiFrameParser, TiFrameRejectReason
from radiowave.adapters.mmwave.ti.protocol import TI_3D_PEOPLE_COUNTING, TiTlvType
from radiowave.simulator.lab.sensors.ti_encoder import (
    CorruptionKind,
    build_corrupt_frame,
    encode_frame,
    encode_target_list_tlv,
    encode_target_record,
    encode_tlv,
)


def _feed(frame_bytes: bytes) -> tuple[list, TiFrameParser]:
    parser = TiFrameParser()
    frames = parser.feed(frame_bytes)
    return frames, parser


# --------------------------------------------------------------------------- happy


def test_single_target_frame_is_accepted_and_round_trips_fields() -> None:
    record = encode_target_record(
        native_track_id=5, x=1.5, y=2.5, z=0.3, vx=0.1, vy=-0.2, vz=0.0, confidence=0.8
    )
    tlv = encode_target_list_tlv([record])
    frame_bytes = encode_frame(frame_number=3, tlvs=[tlv])

    frames, parser = _feed(frame_bytes)

    assert len(frames) == 1
    assert parser.stats.frames_rejected == 0
    frame = frames[0]
    assert frame.frame_number == 3
    assert frame.target_record_layout == "3d_v2"
    assert len(frame.targets) == 1
    target = frame.targets[0]
    assert target.native_track_id == 5
    assert target.x == pytest.approx(1.5)
    assert target.y == pytest.approx(2.5)
    assert target.z == pytest.approx(0.3)
    assert target.vx == pytest.approx(0.1)
    assert target.vy == pytest.approx(-0.2)
    assert target.confidence == pytest.approx(0.8)


def test_multi_target_frame_preserves_order() -> None:
    records = [
        encode_target_record(native_track_id=i, x=float(i), y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0)
        for i in range(1, 4)
    ]
    tlv = encode_target_list_tlv(records)
    frame_bytes = encode_frame(frame_number=10, tlvs=[tlv])

    frames, _parser = _feed(frame_bytes)

    assert len(frames) == 1
    assert [t.native_track_id for t in frames[0].targets] == [1, 2, 3]
    assert [t.x for t in frames[0].targets] == pytest.approx([1.0, 2.0, 3.0])


def test_zero_target_frame_is_accepted() -> None:
    tlv = encode_target_list_tlv([])
    frame_bytes = encode_frame(frame_number=1, tlvs=[tlv])

    frames, parser = _feed(frame_bytes)

    assert len(frames) == 1
    assert frames[0].targets == ()
    assert parser.stats.frames_rejected == 0


def test_total_packet_len_is_always_aligned() -> None:
    for target_count in range(0, 5):
        records = [
            encode_target_record(native_track_id=i, x=0.0, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0)
            for i in range(target_count)
        ]
        tlv = encode_target_list_tlv(records)
        frame_bytes = encode_frame(frame_number=1, tlvs=[tlv])
        total_packet_len = int.from_bytes(frame_bytes[12:16], "little")
        assert total_packet_len % TI_3D_PEOPLE_COUNTING.packet_alignment == 0
        assert total_packet_len == len(frame_bytes)


def test_trailing_padding_is_zero_bytes_and_bounded() -> None:
    tlv = encode_target_list_tlv([])
    frame_bytes = encode_frame(frame_number=1, tlvs=[tlv])
    # header is 40 bytes, tlv is 8 bytes (empty target list) -> body 48, padded to 64.
    tail = frame_bytes[48:]
    assert tail == b"\x00" * len(tail)
    assert len(tail) < 2 * TI_3D_PEOPLE_COUNTING.packet_alignment


def test_encode_target_record_rejects_non_finite_values() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        encode_target_record(native_track_id=1, x=math.nan, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0)


def test_encode_tlv_rejects_non_people_counting_family() -> None:
    with pytest.raises(ValueError, match="people-counting-family"):
        encode_tlv(TiTlvType.DETECTED_POINTS, b"")


def test_encode_frame_rejects_duplicate_tlv_type() -> None:
    tlv = encode_target_list_tlv([])
    with pytest.raises(ValueError, match="duplicate"):
        encode_frame(frame_number=1, tlvs=[tlv, tlv])


def test_encode_target_list_tlv_rejects_too_many_targets() -> None:
    record = encode_target_record(native_track_id=1, x=0.0, y=0.0, z=0.0, vx=0.0, vy=0.0, vz=0.0)
    with pytest.raises(ValueError, match="max_targets"):
        encode_target_list_tlv([record] * 65)


def test_encode_frame_rejects_too_many_tlvs() -> None:
    tlv = encode_tlv(TiTlvType.PRESENCE_INDICATION, (0).to_bytes(4, "little"))
    with pytest.raises(ValueError, match="max_tlvs"):
        encode_frame(frame_number=1, tlvs=[tlv] * 33)


# ------------------------------------------------------------------------- faults


@pytest.mark.parametrize(
    ("kind", "expected_reason"),
    [
        (CorruptionKind.BAD_ALIGNMENT, TiFrameRejectReason.BAD_PACKET_ALIGNMENT),
        (CorruptionKind.DUPLICATE_TLV, TiFrameRejectReason.DUPLICATE_TLV),
        (CorruptionKind.WRONG_FAMILY_TLV, TiFrameRejectReason.TLV_NOT_IN_PROFILE),
        (CorruptionKind.MAGIC_WORD_IN_PADDING, TiFrameRejectReason.BAD_PACKET_PADDING),
        (CorruptionKind.TRUNCATED_TLV, TiFrameRejectReason.TRUNCATED_TLV),
        (CorruptionKind.NON_FINITE_TARGET, TiFrameRejectReason.NON_FINITE_VALUE),
    ],
)
def test_corrupt_frame_is_rejected_with_expected_reason(kind, expected_reason) -> None:
    frame_bytes = build_corrupt_frame(kind, frame_number=1)

    frames, parser = _feed(frame_bytes)

    assert frames == []
    assert parser.stats.frames_rejected >= 1
    assert parser.stats.reject_reasons.get(expected_reason.value, 0) >= 1


def test_corrupt_frame_kinds_are_all_exercised() -> None:
    # Guards against a CorruptionKind being added without a matching parametrized
    # case above going stale silently.
    tested = {
        CorruptionKind.BAD_ALIGNMENT,
        CorruptionKind.DUPLICATE_TLV,
        CorruptionKind.WRONG_FAMILY_TLV,
        CorruptionKind.MAGIC_WORD_IN_PADDING,
        CorruptionKind.TRUNCATED_TLV,
        CorruptionKind.NON_FINITE_TARGET,
    }
    assert tested == set(CorruptionKind)
