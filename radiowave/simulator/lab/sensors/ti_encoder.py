"""Production-safe TI UART packet encoder for the virtual store lab.

This is the mirror image of ``radiowave/adapters/mmwave/ti/parser.py``: where the
parser turns bytes into :class:`~radiowave.adapters.mmwave.ti.models.TiFrame`, this
module turns target data into the exact bytes a real IWR6843 running the "3D people
counting" demo firmware would put on its data UART. Every constant (magic word,
header/TLV struct layouts, target record layout, firmware profile) comes from
:mod:`radiowave.adapters.mmwave.ti.protocol` — the single source of truth the real
parser also reads from — so a protocol change only has to be made in one place.

Two surfaces are exposed, deliberately kept apart:

* The **happy-path API** (``encode_*``) can only ever build a packet the real
  :class:`~radiowave.adapters.mmwave.ti.parser.TiFrameParser` accepts. It has no
  "override" knobs — no way to lie about a length, a TLV count, or a total packet
  size — so a caller cannot emit a malformed packet by accident while emulating a
  healthy sensor.
* The **fault-injection API** (``CorruptionKind`` / ``build_corrupt_frame``) is the
  only place a malformed packet can be built, and it is built by explicitly naming
  which corruption is wanted. This is how the lab's noise/fault model (missed
  frames, bad transports, firmware bugs) gets test coverage without ever tempting
  the normal emulation path to grow the same footguns.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from enum import StrEnum

from radiowave.adapters.mmwave.ti.parser import TiParserLimits
from radiowave.adapters.mmwave.ti.protocol import (
    FRAME_HEADER_BYTES,
    HEADER_STRUCT,
    MAGIC_WORD,
    TI_3D_PEOPLE_COUNTING,
    TLV_HEADER_STRUCT,
    TargetRecordLayout,
    TiFirmwareProfile,
    TiTlvFamily,
    TiTlvType,
    target_record_layout_by_name,
    tlv_type_in_family,
)

# TI SDK 3.x "people counting" demo version/platform values as seen in captures and
# in the test fixture builder (tests/fixtures/ti_mmwave/builder.py); arbitrary but
# fixed so encoded frames look like a real board rather than all-zero placeholders.
_DEFAULT_VERSION = 0x03060000
_DEFAULT_PLATFORM = 0xA6843


def _require_target_layout(name: str) -> TargetRecordLayout:
    """Look up a named :data:`TARGET_RECORD_LAYOUTS` entry, or fail loudly.

    A plain module-level ``target_record_layout_by_name(...)`` call returns
    ``TargetRecordLayout | None``; wrapping it here gives every caller below a
    concretely-typed, never-``None`` layout without repeating the guard.
    """
    layout = target_record_layout_by_name(name)
    if layout is None:  # pragma: no cover - protocol.py guarantees this name exists
        msg = f"protocol.py no longer defines the {name!r} target record layout"
        raise AssertionError(msg)
    return layout


# Pinned per the mission brief: the emulator always encodes the "3d_v2" (with
# confidence) target record layout, matching TI's people-counting SDK 3.x output.
_TARGET_LAYOUT = _require_target_layout("3d_v2")

_DEFAULT_LIMITS = TiParserLimits()

# Every float a target record carries, in the order accepted by _TARGET_LAYOUT.struct:
# tid(I) x y z vx vy vz ax ay az(9f) error_covariance(16f) gating_gain(f) confidence(f).
_ERROR_COVARIANCE_LENGTH = 16


def _require_finite(label: str, values: Sequence[float]) -> None:
    for value in values:
        if not math.isfinite(value):
            msg = (
                f"{label} contains a non-finite value ({value!r}); the happy-path "
                "encoder never emits NaN/inf"
            )
            raise ValueError(msg)


def encode_target_record(
    *,
    native_track_id: int,
    x: float,
    y: float,
    z: float,
    vx: float,
    vy: float,
    vz: float,
    ax: float = 0.0,
    ay: float = 0.0,
    az: float = 0.0,
    error_covariance: Sequence[float] | None = None,
    gating_gain: float = 4.0,
    confidence: float = 0.9,
) -> bytes:
    """Encode one TI target record using the pinned ``"3d_v2"`` layout.

    ``native_track_id`` is the TI-native tracker id (see :mod:`ti_radar`'s
    ``VisibleTarget`` for why this must never be derived from a ground-truth id).
    All floats must be finite: a NaN/inf here would silently propagate through the
    real parser/normalizer, which is exactly what
    :data:`TiFrameRejectReason.NON_FINITE_VALUE` exists to catch, so the happy-path
    encoder refuses to build one at all.
    """
    ec = (
        list(error_covariance) if error_covariance is not None else [0.0] * _ERROR_COVARIANCE_LENGTH
    )
    if len(ec) != _ERROR_COVARIANCE_LENGTH:
        msg = (
            f"error_covariance must have {_ERROR_COVARIANCE_LENGTH} entries for the "
            f"3d_v2 layout, got {len(ec)}"
        )
        raise ValueError(msg)
    _require_finite(
        "target record", [x, y, z, vx, vy, vz, ax, ay, az, *ec, gating_gain, confidence]
    )
    return _TARGET_LAYOUT.struct.pack(
        native_track_id, x, y, z, vx, vy, vz, ax, ay, az, *ec, gating_gain, confidence
    )


def _tlv_type_of(tlv_bytes: bytes) -> int:
    """Peek the type field out of one already-encoded TLV's header."""
    tlv_type, _length = TLV_HEADER_STRUCT.unpack_from(tlv_bytes, 0)
    return int(tlv_type)


def encode_tlv(tlv_type: TiTlvType, payload: bytes) -> bytes:
    """Encode one people-counting-family TLV: an 8-byte ``(type, length)`` header
    plus ``payload`` (length is payload-only, matching SDK 3.x semantics).

    Restricted to the people-counting family (1010/1011/1012/1020/1021) — the same
    family :data:`~radiowave.adapters.mmwave.ti.protocol.TI_3D_PEOPLE_COUNTING` pins
    the real parser to. Emitting an OOB-family TLV (types 1/7) here would silently
    build a packet no real people-counting-profile sensor could ever produce; that
    is exactly what the fault-injection API's ``WRONG_FAMILY_TLV`` kind is for.
    """
    if not tlv_type_in_family(int(tlv_type), TiTlvFamily.PEOPLE_COUNTING):
        msg = (
            f"{tlv_type!r} is not a people-counting-family TLV; the happy-path encoder "
            "only emits 1010/1011/1012/1020/1021 (see CorruptionKind.WRONG_FAMILY_TLV "
            "for deliberately building the rejected case)"
        )
        raise ValueError(msg)
    return TLV_HEADER_STRUCT.pack(int(tlv_type), len(payload)) + payload


def encode_target_list_tlv(records: Sequence[bytes]) -> bytes:
    """Encode a TARGET_LIST_3D TLV from already-built :func:`encode_target_record` bytes."""
    if len(records) > _DEFAULT_LIMITS.max_targets:
        msg = (
            f"{len(records)} target records exceeds TiParserLimits.max_targets "
            f"({_DEFAULT_LIMITS.max_targets})"
        )
        raise ValueError(msg)
    return encode_tlv(TiTlvType.TARGET_LIST_3D, b"".join(records))


def _assemble_frame(
    *,
    frame_number: int,
    tlvs: Sequence[bytes],
    version: int,
    platform: int,
    time_cpu_cycles: int,
    num_detected_objects: int,
    subframe_number: int,
    packet_alignment: int,
    num_tlvs_override: int | None = None,
    total_packet_len_override: int | None = None,
    tail_bytes: bytes | None = None,
) -> bytes:
    """Shared framing logic: magic word + 32-byte header + TLVs + alignment tail.

    Private. The happy-path :func:`encode_frame` calls this with every override
    left at its safe default (``None``); only the fault-injection functions below
    pass an override, and only ever one at a time, to build one specific invalid
    packet a real parser is expected to reject.
    """
    tlv_bytes = b"".join(tlvs)
    body_len = FRAME_HEADER_BYTES + len(tlv_bytes)
    if tail_bytes is not None:
        padding = tail_bytes
    else:
        pad_len = 0
        if packet_alignment > 0 and body_len % packet_alignment != 0:
            pad_len = packet_alignment - (body_len % packet_alignment)
        padding = b"\x00" * pad_len
    total_packet_len = (
        total_packet_len_override
        if total_packet_len_override is not None
        else body_len + len(padding)
    )
    num_tlvs = num_tlvs_override if num_tlvs_override is not None else len(tlvs)
    header = HEADER_STRUCT.pack(
        version,
        total_packet_len,
        platform,
        frame_number,
        time_cpu_cycles,
        num_detected_objects,
        num_tlvs,
        subframe_number,
    )
    return MAGIC_WORD + header + tlv_bytes + padding


def encode_frame(
    *,
    frame_number: int,
    tlvs: Sequence[bytes],
    version: int = _DEFAULT_VERSION,
    platform: int = _DEFAULT_PLATFORM,
    time_cpu_cycles: int = 0,
    num_detected_objects: int = 0,
    subframe_number: int = 0,
    profile: TiFirmwareProfile = TI_3D_PEOPLE_COUNTING,
) -> bytes:
    """Encode one complete, valid TI UART frame: zero-byte alignment padding,
    ``total_packet_len``/``num_tlvs`` always consistent with the actual bytes, no
    duplicate decoded TLV types. This is the only frame-builder normal sensor
    emulation should call; it has no way to produce a packet the real
    :class:`~radiowave.adapters.mmwave.ti.parser.TiFrameParser` would reject.
    """
    if len(tlvs) > _DEFAULT_LIMITS.max_tlvs:
        msg = f"{len(tlvs)} TLVs exceeds TiParserLimits.max_tlvs ({_DEFAULT_LIMITS.max_tlvs})"
        raise ValueError(msg)
    seen_types: set[int] = set()
    for tlv in tlvs:
        tlv_type = _tlv_type_of(tlv)
        if tlv_type in seen_types:
            msg = (
                f"duplicate TLV type {tlv_type} passed to encode_frame; the real parser "
                "rejects a repeated decoded TLV type as DUPLICATE_TLV"
            )
            raise ValueError(msg)
        seen_types.add(tlv_type)
    return _assemble_frame(
        frame_number=frame_number,
        tlvs=tlvs,
        version=version,
        platform=platform,
        time_cpu_cycles=time_cpu_cycles,
        num_detected_objects=num_detected_objects,
        subframe_number=subframe_number,
        packet_alignment=profile.packet_alignment,
    )


# --------------------------------------------------------------------------------
# Fault injection: a separate, explicitly-named surface. Every function here is
# documented with the TiFrameRejectReason it is expected to trigger in the real
# parser (verified in tests/simulator/lab/test_ti_encoder.py); none of them are
# reachable from encode_frame/encode_tlv/encode_target_record above.
# --------------------------------------------------------------------------------


class CorruptionKind(StrEnum):
    """One deliberately-invalid packet shape, each mapped to a real
    :class:`~radiowave.adapters.mmwave.ti.parser.TiFrameRejectReason`."""

    BAD_ALIGNMENT = "bad_alignment"
    """total_packet_len not a multiple of the profile's packet_alignment ->
    BAD_PACKET_ALIGNMENT."""
    DUPLICATE_TLV = "duplicate_tlv"
    """Same decoded TLV type (TARGET_LIST_3D) emitted twice in one packet ->
    DUPLICATE_TLV."""
    WRONG_FAMILY_TLV = "wrong_family_tlv"
    """An OOB-family TLV (DETECTED_POINTS) under the people-counting profile ->
    TLV_NOT_IN_PROFILE."""
    MAGIC_WORD_IN_PADDING = "magic_word_in_padding"
    """The alignment tail contains the magic word instead of zero padding ->
    BAD_PACKET_PADDING."""
    TRUNCATED_TLV = "truncated_tlv"
    """num_tlvs claims one more TLV than actually fits before total_packet_len ->
    TRUNCATED_TLV."""
    NON_FINITE_TARGET = "non_finite_target"
    """A target record with a NaN position, built by hand (bypassing
    encode_target_record's finite check) -> NON_FINITE_VALUE."""


def build_corrupt_frame(
    kind: CorruptionKind,
    *,
    frame_number: int = 1,
    profile: TiFirmwareProfile = TI_3D_PEOPLE_COUNTING,
) -> bytes:
    """Build one packet the real ``TiFrameParser`` is expected to reject, per
    ``kind``. See :class:`CorruptionKind` for the exact reject reason each one maps
    to. This is the ONLY function in this module that can produce invalid bytes.
    """
    if kind is CorruptionKind.BAD_ALIGNMENT:
        tlv = encode_target_list_tlv([])
        # Any length not a multiple of packet_alignment (32) trips the check in
        # TiFrameParser.feed before the packet body is even parsed.
        return _assemble_frame(
            frame_number=frame_number,
            tlvs=[tlv],
            version=_DEFAULT_VERSION,
            platform=_DEFAULT_PLATFORM,
            time_cpu_cycles=0,
            num_detected_objects=0,
            subframe_number=0,
            packet_alignment=profile.packet_alignment,
            total_packet_len_override=FRAME_HEADER_BYTES + len(tlv) + 1,
        )

    if kind is CorruptionKind.DUPLICATE_TLV:
        record = encode_target_record(
            native_track_id=1, x=1.0, y=1.0, z=1.0, vx=0.0, vy=0.0, vz=0.0
        )
        tlv = encode_target_list_tlv([record])
        # Two TARGET_LIST_3D TLVs in one packet: ambiguous which one is "the"
        # target list, so the real parser rejects the whole packet rather than
        # picking a winner.
        return _assemble_frame(
            frame_number=frame_number,
            tlvs=[tlv, tlv],
            version=_DEFAULT_VERSION,
            platform=_DEFAULT_PLATFORM,
            time_cpu_cycles=0,
            num_detected_objects=0,
            subframe_number=0,
            packet_alignment=profile.packet_alignment,
        )

    if kind is CorruptionKind.WRONG_FAMILY_TLV:
        # Built with the raw TLV header pack, deliberately bypassing encode_tlv's
        # family guard: this is the one case that guard exists to make impossible
        # via the happy path.
        bad_tlv = TLV_HEADER_STRUCT.pack(int(TiTlvType.DETECTED_POINTS), 0)
        return _assemble_frame(
            frame_number=frame_number,
            tlvs=[bad_tlv],
            version=_DEFAULT_VERSION,
            platform=_DEFAULT_PLATFORM,
            time_cpu_cycles=0,
            num_detected_objects=0,
            subframe_number=0,
            packet_alignment=profile.packet_alignment,
        )

    if kind is CorruptionKind.MAGIC_WORD_IN_PADDING:
        tlv = encode_target_list_tlv([])
        body_len = FRAME_HEADER_BYTES + len(tlv)
        pad_len = profile.packet_alignment - (body_len % profile.packet_alignment)
        if pad_len == 0:
            pad_len = profile.packet_alignment
        # Fill the alignment tail with the magic word (padded out with zeros to the
        # required length): a legitimate tail can never contain it, so the real
        # parser treats this as the start of the next frame being swallowed.
        tail = (MAGIC_WORD + b"\x00" * pad_len)[:pad_len]
        return _assemble_frame(
            frame_number=frame_number,
            tlvs=[tlv],
            version=_DEFAULT_VERSION,
            platform=_DEFAULT_PLATFORM,
            time_cpu_cycles=0,
            num_detected_objects=0,
            subframe_number=0,
            packet_alignment=profile.packet_alignment,
            tail_bytes=tail,
        )

    if kind is CorruptionKind.TRUNCATED_TLV:
        # A real, fully valid TARGET_INDEX TLV that exactly fills the packet to a
        # 32-byte-aligned total_packet_len, combined with num_tlvs=2: the parser
        # decodes the first TLV fine, then tries to read a second TLV header that
        # was never written, running past total_packet_len.
        tlv = encode_tlv(TiTlvType.TARGET_INDEX, bytes(16))
        body_len = FRAME_HEADER_BYTES + len(tlv)
        if body_len % profile.packet_alignment != 0:  # pragma: no cover - 40 + 24 = 64
            msg = "TRUNCATED_TLV fixture no longer lands on an aligned body length"
            raise AssertionError(msg)
        return _assemble_frame(
            frame_number=frame_number,
            tlvs=[tlv],
            version=_DEFAULT_VERSION,
            platform=_DEFAULT_PLATFORM,
            time_cpu_cycles=0,
            num_detected_objects=0,
            subframe_number=0,
            packet_alignment=profile.packet_alignment,
            num_tlvs_override=2,
        )

    if kind is CorruptionKind.NON_FINITE_TARGET:
        # Packed by hand with struct directly, bypassing encode_target_record's
        # _require_finite guard on purpose.
        ec = [0.0] * _ERROR_COVARIANCE_LENGTH
        record = _TARGET_LAYOUT.struct.pack(
            1, math.nan, 1.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, *ec, 4.0, 0.9
        )
        tlv = encode_target_list_tlv([record])
        return _assemble_frame(
            frame_number=frame_number,
            tlvs=[tlv],
            version=_DEFAULT_VERSION,
            platform=_DEFAULT_PLATFORM,
            time_cpu_cycles=0,
            num_detected_objects=0,
            subframe_number=0,
            packet_alignment=profile.packet_alignment,
        )

    msg = f"unhandled CorruptionKind: {kind!r}"  # pragma: no cover - exhaustive above
    raise AssertionError(msg)
