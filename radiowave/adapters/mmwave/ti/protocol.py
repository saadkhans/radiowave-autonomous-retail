"""TI IWR6843-class mmWave UART packet layout (constants only, no parsing logic).

This is the public TI mmWave SDK 3.x / Industrial Toolbox "3D people counting" UART
output format as published in TI's demo user guides and the Industrial Visualizer
reference parser: an 8-byte magic word, a fixed 32-byte frame header, then a run of
type-length-value (TLV) records. The layouts below are what those parsers decode.

Firmware variance (older SDK 2.x length semantics, alternate target-record shapes,
packet padding) is isolated in :class:`TiFirmwareProfile` rather than scattered
through the parser. TI's native coordinate frame is NOT interpreted here — that is
the job of the adapter/normalization layer built on top of this package.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum, StrEnum

MAGIC_WORD = bytes([0x02, 0x01, 0x04, 0x03, 0x06, 0x05, 0x08, 0x07])

# version, total_packet_len, platform, frame_number, time_cpu_cycles,
# num_detected_objects, num_tlvs, subframe_number — immediately follows the magic word.
HEADER_STRUCT = struct.Struct("<8I")
FRAME_HEADER_BYTES = len(MAGIC_WORD) + HEADER_STRUCT.size  # 8 + 32 = 40

# type, length (payload bytes only unless the firmware profile says otherwise).
TLV_HEADER_STRUCT = struct.Struct("<II")


class TiTlvType(IntEnum):
    """TLV type ids as emitted by the TI demo firmwares this package targets."""

    DETECTED_POINTS = 1
    """OOB SDK 3.x: N x (x, y, z, velocity) float32 LE, 16 bytes each."""
    DETECTED_POINTS_SIDE_INFO = 7
    """OOB SDK 3.x: N x (snr int16, noise int16), units 0.1 dB, 4 bytes each."""
    TARGET_LIST_3D = 1010
    """3D people counting: N x target record; see :data:`TARGET_RECORD_LAYOUTS`."""
    TARGET_INDEX = 1011
    """N x uint8 target id per point; 253..255 are unassociated/noise codes."""
    TARGET_HEIGHT = 1012
    """N x (tid uint8, 3 pad bytes, max_z float32, min_z float32), 12 bytes each.

    The record is 12 bytes, not the 9 a hand-packed ``'<Bff'`` would produce: TI's C
    struct ``{uint8_t targetID; float maxZ; float minZ;}`` is padded by the compiler
    to 4-byte alignment, and TI's own reference visualizer reads it back with the
    native ``'B2f'`` layout (``struct.calcsize('B2f') == 12``). Decoding it as 9
    bytes rejects every real one-target height TLV on the divisibility check and
    takes the whole otherwise-valid frame — including its target list — with it.

    The id is the single byte at offset 0; bytes 1..3 are struct padding and are
    skipped, never folded into the id. Reading the field as a uint32 instead would
    be indistinguishable only if that padding were always zero, which nothing
    guarantees: the demo packs TLVs contiguously into a shared result buffer, so
    padding can carry stale bytes from a previous frame, and a uint32 read would
    then yield a silently wrong id where the byte read stays correct. Neither
    reading dominates the other outright — a genuine uint32 field carrying an id
    above 255 would truncate here — but for the id range this firmware is known to
    use the uint8 reading is correct in both cases, so it is the one pinned until
    real bytes settle it. Confirm the record against real bytes
    during bring-up (``docs/hardware/ti-iwr6843-first-bringup.md``) before anything
    consumes the id — today heights are carried for diagnostics only.
    """
    POINT_CLOUD_3D = 1020
    """3D people counting compressed cloud: 5 float32 units, then N x 8-byte records."""
    PRESENCE_INDICATION = 1021
    """A single uint32 presence flag."""


class TiTlvFamily(StrEnum):
    """Which of TI's two distinct UART TLV vocabularies a TLV type belongs to.

    The demo firmwares this package targets are not one protocol with optional
    fields — they are two separate vocabularies sharing one magic-word/header
    framing. The generic "out-of-box" (OOB) demo emits raw, untracked detections
    as types 1 and 7. The "3D people counting" demo emits tracked targets,
    compressed point cloud, and presence as types 1010-1021. A TLV type from one
    vocabulary showing up in a stream configured for the other means the wrong
    firmware image is flashed for the selected :class:`TiFirmwareProfile`, not
    merely an unrecognized TLV — see :data:`_TLV_TYPES_BY_FAMILY` and
    :func:`tlv_type_in_family`.
    """

    OOB = "oob"
    PEOPLE_COUNTING = "people_counting"


_TLV_TYPES_BY_FAMILY: dict[TiTlvFamily, frozenset[TiTlvType]] = {
    TiTlvFamily.OOB: frozenset({TiTlvType.DETECTED_POINTS, TiTlvType.DETECTED_POINTS_SIDE_INFO}),
    TiTlvFamily.PEOPLE_COUNTING: frozenset(
        {
            TiTlvType.TARGET_LIST_3D,
            TiTlvType.TARGET_INDEX,
            TiTlvType.TARGET_HEIGHT,
            TiTlvType.POINT_CLOUD_3D,
            TiTlvType.PRESENCE_INDICATION,
        }
    ),
}


def tlv_type_in_family(tlv_type: int, family: TiTlvFamily) -> bool:
    """Whether ``tlv_type`` belongs to the named :class:`TiTlvFamily`.

    Used to reject a *known* TLV type that belongs to a different firmware
    vocabulary than the one configured (e.g. an OOB-flashed sensor emitting type-1
    DETECTED_POINTS while the parser is configured for 3D people counting), so
    that case surfaces as a firmware/configuration error instead of a silently
    "healthy" frame with no targets. Types outside every family in
    :data:`_TLV_TYPES_BY_FAMILY` (i.e. unrecognized TLVs) are not covered by this
    check at all — callers must keep those on the existing tolerated path.
    """
    return tlv_type in _TLV_TYPES_BY_FAMILY.get(family, frozenset())


@dataclass(frozen=True, slots=True)
class TargetRecordLayout:
    """One TI target-record binary shape, selected by payload-length divisibility."""

    name: str
    struct: struct.Struct
    has_z: bool
    has_confidence: bool


# posX,posY,posZ,velX,velY,velZ,accX,accY,accZ float32; ec[16] float32; g float32;
# confidence float32.
_LAYOUT_3D_V2 = TargetRecordLayout(
    name="3d_v2", struct=struct.Struct("<I9f16fff"), has_z=True, has_confidence=True
)
# 9 float32 pos/vel/acc; ec[9] float32; g float32 (no confidence).
_LAYOUT_3D_V1 = TargetRecordLayout(
    name="3d_v1", struct=struct.Struct("<I9f9ff"), has_z=True, has_confidence=False
)
# posX,posY,velX,velY,accX,accY float32; ec[9] float32; g float32 (z, vz, az implied 0).
_LAYOUT_2D = TargetRecordLayout(
    name="2d", struct=struct.Struct("<I6f9ff"), has_z=False, has_confidence=False
)

TARGET_RECORD_LAYOUTS: tuple[TargetRecordLayout, ...] = (_LAYOUT_3D_V2, _LAYOUT_3D_V1, _LAYOUT_2D)
_LAYOUTS_BY_NAME = {layout.name: layout for layout in TARGET_RECORD_LAYOUTS}


def target_record_layout_by_name(name: str) -> TargetRecordLayout | None:
    """Look up one of :data:`TARGET_RECORD_LAYOUTS` by name, or ``None`` if unknown."""
    return _LAYOUTS_BY_NAME.get(name)


@dataclass(frozen=True, slots=True)
class TiFirmwareProfile:
    """Firmware-specific packet variance, isolated from the defensive parsing logic."""

    name: str
    target_record_layout: str | None = None
    """Force one of :data:`TARGET_RECORD_LAYOUTS` by name; ``None`` auto-detects."""
    tlv_length_includes_header: bool = False
    """SDK 2.x style lengths; the default (SDK 3.x) is payload-only lengths."""
    packet_alignment: int = 32
    """Packets are padded up to a multiple of this. Trailing bytes inside
    ``total_packet_len`` after the last TLV are padding and are counted, never parsed."""
    tlv_family: TiTlvFamily | None = None
    """Which :class:`TiTlvFamily` this firmware's TLV vocabulary is restricted to.

    ``None`` means unrestricted — kept as the default so ad-hoc profiles built in
    tests (or any firmware variant not yet classified into a family) are not
    silently made stricter than before. The two named profiles below pin a real
    family: a known TLV type from the *other* family showing up under one of them
    is a firmware/configuration mismatch and is rejected (see
    :func:`tlv_type_in_family` and its use in ``parser.py``), not decoded as if
    it belonged.
    """


TI_3D_PEOPLE_COUNTING = TiFirmwareProfile(
    name="ti-3d-people-counting", tlv_family=TiTlvFamily.PEOPLE_COUNTING
)
TI_OOB_SDK3 = TiFirmwareProfile(name="ti-oob-sdk3", tlv_family=TiTlvFamily.OOB)
