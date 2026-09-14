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
from enum import IntEnum

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
    """N x (tid uint8, max_z float32, min_z float32), 9 bytes each."""
    POINT_CLOUD_3D = 1020
    """3D people counting compressed cloud: 5 float32 units, then N x 8-byte records."""
    PRESENCE_INDICATION = 1021
    """A single uint32 presence flag."""


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


TI_3D_PEOPLE_COUNTING = TiFirmwareProfile(name="ti-3d-people-counting")
TI_OOB_SDK3 = TiFirmwareProfile(name="ti-oob-sdk3")
