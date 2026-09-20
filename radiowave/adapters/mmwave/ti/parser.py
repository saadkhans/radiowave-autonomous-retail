"""Defensive, feed-based TI UART frame parser.

Never raises on malformed input — only a programming error should raise. Garbage,
truncation, and out-of-range lengths are all handled by discarding bytes and
resynchronizing on the next magic word. There are no threads here: :meth:`feed`
is a pure function of the bytes handed to it plus the parser's own buffer.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass, field
from enum import StrEnum

from radiowave.adapters.mmwave.ti.models import TiFrame, TiPointCloudPoint, TiTarget, TiTargetHeight
from radiowave.adapters.mmwave.ti.protocol import (
    FRAME_HEADER_BYTES,
    HEADER_STRUCT,
    MAGIC_WORD,
    TARGET_RECORD_LAYOUTS,
    TI_3D_PEOPLE_COUNTING,
    TLV_HEADER_STRUCT,
    TargetRecordLayout,
    TiFirmwareProfile,
    TiTlvType,
    target_record_layout_by_name,
    tlv_type_in_family,
)


class TiFrameRejectReason(StrEnum):
    BAD_PACKET_LENGTH = "BAD_PACKET_LENGTH"
    PACKET_TOO_LARGE = "PACKET_TOO_LARGE"
    TOO_MANY_TLVS = "TOO_MANY_TLVS"
    TRUNCATED_TLV = "TRUNCATED_TLV"
    TLV_OVERRUN = "TLV_OVERRUN"
    BAD_TLV_LENGTH = "BAD_TLV_LENGTH"
    BAD_TARGET_RECORD = "BAD_TARGET_RECORD"
    AMBIGUOUS_TARGET_RECORD = "AMBIGUOUS_TARGET_RECORD"
    TOO_MANY_TARGETS = "TOO_MANY_TARGETS"
    TOO_MANY_POINTS = "TOO_MANY_POINTS"
    NON_FINITE_VALUE = "NON_FINITE_VALUE"
    BAD_PACKET_ALIGNMENT = "BAD_PACKET_ALIGNMENT"
    DUPLICATE_TLV = "DUPLICATE_TLV"
    BAD_PACKET_PADDING = "BAD_PACKET_PADDING"
    TLV_NOT_IN_PROFILE = "TLV_NOT_IN_PROFILE"


@dataclass(frozen=True, slots=True)
class TiParserLimits:
    """Defensive caps. A stream that exceeds any of these is corrupt or hostile,
    never merely "a big frame"."""

    max_packet_bytes: int = 65_536
    max_tlvs: int = 32
    max_targets: int = 64
    max_points: int = 4096
    max_buffer_bytes: int = 262_144


@dataclass(slots=True)
class TiParserStats:
    """Running counters for observability. ``reject_reasons`` behaves like a
    ``Counter`` keyed by :class:`TiFrameRejectReason` value."""

    bytes_received: int = 0
    bytes_discarded: int = 0
    frames_parsed: int = 0
    frames_rejected: int = 0
    resyncs: int = 0
    unknown_tlv_count: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)


class _FrameRejectedError(Exception):
    """Internal signal: the packet under construction is invalid.

    Caught by :meth:`TiFrameParser.feed`, which does the accounting and resync;
    never escapes this module.
    """

    def __init__(self, reason: TiFrameRejectReason) -> None:
        super().__init__(reason.value)
        self.reason = reason


# --- fixed-size TLV record shapes not covered by TARGET_RECORD_LAYOUTS -------------

_DETECTED_POINT_STRUCT = struct.Struct("<4f")  # x, y, z, velocity (OOB SDK 3.x)
_SIDE_INFO_STRUCT = struct.Struct("<2h")  # snr, noise; units 0.1 dB
_TARGET_HEIGHT_STRUCT = struct.Struct("<B3xff")  # tid, 3 pad bytes, max_z, min_z
_POINT_CLOUD_UNITS_STRUCT = struct.Struct("<5f")  # elevation, azimuth, doppler, range, snr units
_POINT_CLOUD_RECORD_STRUCT = struct.Struct("<bbhHH")  # elevation, azimuth, doppler, range, snr
_PRESENCE_STRUCT = struct.Struct("<I")

# TLV types this module actually decodes into combined frame outputs. A repeat of
# any of these within one packet is ambiguous (which decode wins?) and the whole
# packet is rejected rather than resolved by picking one; unknown types are not
# decoded at all and may repeat freely.
_DECODED_TLV_TYPES = frozenset(
    {
        TiTlvType.TARGET_LIST_3D,
        TiTlvType.TARGET_INDEX,
        TiTlvType.TARGET_HEIGHT,
        TiTlvType.POINT_CLOUD_3D,
        TiTlvType.PRESENCE_INDICATION,
        TiTlvType.DETECTED_POINTS,
        TiTlvType.DETECTED_POINTS_SIDE_INFO,
    }
)


def _select_target_layout(
    payload_len: int, profile: TiFirmwareProfile
) -> TargetRecordLayout | None:
    """Pick the target-record layout for a TARGET_LIST_3D payload, or ``None`` for an
    unambiguous empty (0-length) target list.

    A 0-length payload is compatible with every layout (0 records either way), so it
    is never ambiguous and carries no layout. Otherwise, when the firmware profile
    does not pin a layout, every :data:`TARGET_RECORD_LAYOUTS` entry whose record size
    evenly divides ``payload_len`` is a candidate; more than one candidate means the
    bytes are genuinely ambiguous and must be rejected rather than guessed.
    """
    if payload_len == 0:
        return None
    if profile.target_record_layout is not None:
        layout = target_record_layout_by_name(profile.target_record_layout)
        if layout is None or payload_len % layout.struct.size != 0:
            raise _FrameRejectedError(TiFrameRejectReason.BAD_TARGET_RECORD)
        return layout
    compatible = [
        layout for layout in TARGET_RECORD_LAYOUTS if payload_len % layout.struct.size == 0
    ]
    if len(compatible) == 1:
        return compatible[0]
    if len(compatible) == 0:
        raise _FrameRejectedError(TiFrameRejectReason.BAD_TARGET_RECORD)
    raise _FrameRejectedError(TiFrameRejectReason.AMBIGUOUS_TARGET_RECORD)


def _require_finite(values: tuple[float, ...]) -> None:
    if not all(math.isfinite(value) for value in values):
        raise _FrameRejectedError(TiFrameRejectReason.NON_FINITE_VALUE)


def _decode_targets(payload: bytes, layout: TargetRecordLayout) -> tuple[TiTarget, ...]:
    targets: list[TiTarget] = []
    for record in layout.struct.iter_unpack(payload):
        if layout.name == "2d":
            tid, x, y, vx, vy, ax, ay, *tail = record
            z = vz = az = 0.0
            error_covariance = tuple(tail[:9])
            gating_gain = tail[9]
            confidence = None
        elif layout.name == "3d_v1":
            tid, x, y, z, vx, vy, vz, ax, ay, az, *tail = record
            error_covariance = tuple(tail[:9])
            gating_gain = tail[9]
            confidence = None
        else:  # "3d_v2"
            tid, x, y, z, vx, vy, vz, ax, ay, az, *tail = record
            error_covariance = tuple(tail[:16])
            gating_gain = tail[16]
            confidence = tail[17]

        checked = (x, y, z, vx, vy, vz, ax, ay, az, *error_covariance, gating_gain)
        _require_finite(checked if confidence is None else (*checked, confidence))
        targets.append(
            TiTarget(
                native_track_id=tid,
                x=x,
                y=y,
                z=z,
                vx=vx,
                vy=vy,
                vz=vz,
                ax=ax,
                ay=ay,
                az=az,
                error_covariance=error_covariance,
                gating_gain=gating_gain,
                confidence=confidence,
            )
        )
    return tuple(targets)


def _decode_oob_points(
    xyzv_payload: bytes, side_info_payload: bytes | None
) -> tuple[TiPointCloudPoint, ...]:
    side_info = (
        list(_SIDE_INFO_STRUCT.iter_unpack(side_info_payload))
        if side_info_payload is not None
        else []
    )
    points: list[TiPointCloudPoint] = []
    for index, (x, y, z, velocity) in enumerate(_DETECTED_POINT_STRUCT.iter_unpack(xyzv_payload)):
        range_m = math.sqrt(x * x + y * y + z * z)
        azimuth_rad = math.atan2(x, y)
        elevation_rad = math.asin(z / range_m) if range_m > 0.0 else 0.0
        snr = side_info[index][0] * 0.1 if index < len(side_info) else 0.0
        _require_finite((x, y, z, range_m, azimuth_rad, elevation_rad, velocity, snr))
        points.append(
            TiPointCloudPoint(
                x=x,
                y=y,
                z=z,
                range_m=range_m,
                azimuth_rad=azimuth_rad,
                elevation_rad=elevation_rad,
                doppler_mps=velocity,
                snr=snr,
            )
        )
    return tuple(points)


def _decode_point_cloud(payload: bytes) -> tuple[TiPointCloudPoint, ...]:
    elevation_unit, azimuth_unit, doppler_unit, range_unit, snr_unit = (
        _POINT_CLOUD_UNITS_STRUCT.unpack_from(payload, 0)
    )
    points: list[TiPointCloudPoint] = []
    body = payload[_POINT_CLOUD_UNITS_STRUCT.size :]
    for (
        elevation_raw,
        azimuth_raw,
        doppler_raw,
        range_raw,
        snr_raw,
    ) in _POINT_CLOUD_RECORD_STRUCT.iter_unpack(body):
        elevation_rad = elevation_raw * elevation_unit
        azimuth_rad = azimuth_raw * azimuth_unit
        doppler_mps = doppler_raw * doppler_unit
        range_m = range_raw * range_unit
        snr = snr_raw * snr_unit
        x = range_m * math.sin(azimuth_rad) * math.cos(elevation_rad)
        y = range_m * math.cos(azimuth_rad) * math.cos(elevation_rad)
        z = range_m * math.sin(elevation_rad)
        _require_finite((x, y, z, range_m, azimuth_rad, elevation_rad, doppler_mps, snr))
        points.append(
            TiPointCloudPoint(
                x=x,
                y=y,
                z=z,
                range_m=range_m,
                azimuth_rad=azimuth_rad,
                elevation_rad=elevation_rad,
                doppler_mps=doppler_mps,
                snr=snr,
            )
        )
    return tuple(points)


def _decode_heights(payload: bytes) -> tuple[TiTargetHeight, ...]:
    heights: list[TiTargetHeight] = []
    for tid, max_z, min_z in _TARGET_HEIGHT_STRUCT.iter_unpack(payload):
        _require_finite((max_z, min_z))
        heights.append(TiTargetHeight(native_track_id=tid, max_z=max_z, min_z=min_z))
    return tuple(heights)


def _parse_packet(
    packet: bytes,
    header: tuple[int, ...],
    profile: TiFirmwareProfile,
    limits: TiParserLimits,
) -> TiFrame:
    (
        version,
        total_packet_len,
        platform,
        frame_number,
        time_cpu_cycles,
        num_detected_objects,
        num_tlvs,
        subframe_number,
    ) = header

    targets: tuple[TiTarget, ...] = ()
    layout_name: str | None = None
    cloud_points: tuple[TiPointCloudPoint, ...] = ()
    pending_detected_points: bytes | None = None
    pending_side_info: bytes | None = None
    target_indices: tuple[int, ...] = ()
    heights: tuple[TiTargetHeight, ...] = ()
    presence: int | None = None
    unknown_tlvs: list[tuple[int, int]] = []
    seen: set[int] = set()

    offset = FRAME_HEADER_BYTES
    for _ in range(num_tlvs):
        if offset + TLV_HEADER_STRUCT.size > total_packet_len:
            raise _FrameRejectedError(TiFrameRejectReason.TRUNCATED_TLV)
        tlv_type, declared_length = TLV_HEADER_STRUCT.unpack_from(packet, offset)
        offset += TLV_HEADER_STRUCT.size

        length = declared_length
        if profile.tlv_length_includes_header:
            length -= TLV_HEADER_STRUCT.size
        if length < 0:
            raise _FrameRejectedError(TiFrameRejectReason.BAD_TLV_LENGTH)
        if offset + length > total_packet_len:
            raise _FrameRejectedError(TiFrameRejectReason.TLV_OVERRUN)
        payload = packet[offset : offset + length]

        # A TLV type repeated within one packet is ambiguous, not something to
        # resolve by picking a winner: reject the whole packet. Unknown TLV types
        # are exempt — they are merely listed, never decoded into a combined output.
        is_decoded_type = tlv_type in _DECODED_TLV_TYPES
        if is_decoded_type:
            if tlv_type in seen:
                raise _FrameRejectedError(TiFrameRejectReason.DUPLICATE_TLV)
            seen.add(tlv_type)
            # A known TLV type that does not belong to the configured firmware
            # profile's TLV family means the wrong firmware image is flashed for
            # the selected profile — e.g. an OOB-flashed sensor configured as
            # ti-3d-people-counting emits type-1 DETECTED_POINTS, which that
            # profile's layout never expects. Left unchecked this "successfully"
            # decodes into an empty/irrelevant frame field, so the session looks
            # healthy while producing nothing. Unknown types are not in
            # _DECODED_TLV_TYPES at all and are unaffected — they keep their
            # existing tolerated (listed-only, never decoded) behaviour.
            if profile.tlv_family is not None and not tlv_type_in_family(
                tlv_type, profile.tlv_family
            ):
                raise _FrameRejectedError(TiFrameRejectReason.TLV_NOT_IN_PROFILE)

        if tlv_type == TiTlvType.DETECTED_POINTS:
            if length % _DETECTED_POINT_STRUCT.size != 0:
                raise _FrameRejectedError(TiFrameRejectReason.BAD_TLV_LENGTH)
            pending_detected_points = payload
        elif tlv_type == TiTlvType.DETECTED_POINTS_SIDE_INFO:
            if length % _SIDE_INFO_STRUCT.size != 0:
                raise _FrameRejectedError(TiFrameRejectReason.BAD_TLV_LENGTH)
            # Bound the record count here, before _decode_oob_points ever
            # materializes a Python list from this payload. The 4-byte
            # divisibility check above says nothing about how many records that
            # is: a packet can declare a single detected point (or none at all)
            # while its side-info payload carries thousands of records, which
            # would otherwise bypass limits.max_points entirely until the whole
            # oversized list has already been built.
            if length // _SIDE_INFO_STRUCT.size > limits.max_points:
                raise _FrameRejectedError(TiFrameRejectReason.TOO_MANY_POINTS)
            pending_side_info = payload
        elif tlv_type == TiTlvType.TARGET_LIST_3D:
            layout = _select_target_layout(length, profile)
            if layout is None:
                layout_name = None
                targets = ()
            else:
                layout_name = layout.name
                targets = _decode_targets(payload, layout)
                if len(targets) > limits.max_targets:
                    raise _FrameRejectedError(TiFrameRejectReason.TOO_MANY_TARGETS)
        elif tlv_type == TiTlvType.TARGET_INDEX:
            if len(payload) > limits.max_points:
                raise _FrameRejectedError(TiFrameRejectReason.TOO_MANY_POINTS)
            target_indices = tuple(payload)
        elif tlv_type == TiTlvType.TARGET_HEIGHT:
            if length % _TARGET_HEIGHT_STRUCT.size != 0:
                raise _FrameRejectedError(TiFrameRejectReason.BAD_TLV_LENGTH)
            # One height record is emitted per target, so it carries the same
            # cardinality as TARGET_LIST_3D and is bounded the same way — before
            # materializing the tuple, not after.
            if length // _TARGET_HEIGHT_STRUCT.size > limits.max_targets:
                raise _FrameRejectedError(TiFrameRejectReason.TOO_MANY_TARGETS)
            heights = _decode_heights(payload)
        elif tlv_type == TiTlvType.POINT_CLOUD_3D:
            body_len = length - _POINT_CLOUD_UNITS_STRUCT.size
            if body_len < 0 or body_len % _POINT_CLOUD_RECORD_STRUCT.size != 0:
                raise _FrameRejectedError(TiFrameRejectReason.BAD_TLV_LENGTH)
            cloud_points = _decode_point_cloud(payload)
            if len(cloud_points) > limits.max_points:
                raise _FrameRejectedError(TiFrameRejectReason.TOO_MANY_POINTS)
        elif tlv_type == TiTlvType.PRESENCE_INDICATION:
            if length != _PRESENCE_STRUCT.size:
                raise _FrameRejectedError(TiFrameRejectReason.BAD_TLV_LENGTH)
            (presence,) = _PRESENCE_STRUCT.unpack_from(payload)
        else:
            unknown_tlvs.append((tlv_type, length))

        offset += length

    # TLV order within a packet is not guaranteed, so DETECTED_POINTS_SIDE_INFO can
    # arrive before or after DETECTED_POINTS: this cross-check can only run here,
    # once both counts are known, not inline in the loop above where one of the two
    # may still be unseen. A mismatch between the two counts means the packet is
    # corrupt — one array being shorter or longer than the other is not a case
    # where either one is "authoritative" and the other ignorable.
    if pending_detected_points is not None and pending_side_info is not None:
        detected_count = len(pending_detected_points) // _DETECTED_POINT_STRUCT.size
        side_info_count = len(pending_side_info) // _SIDE_INFO_STRUCT.size
        if detected_count != side_info_count:
            raise _FrameRejectedError(TiFrameRejectReason.BAD_TLV_LENGTH)

    # A corrupted total_packet_len that still passes every per-TLV bound check above
    # (offsets, lengths, alignment) can extend past the declared TLVs into bytes that
    # are not padding at all — most dangerously, the next packet's magic word and
    # header. Unvalidated, that both reports a damaged frame as successfully parsed
    # AND silently consumes (and thus drops) the start of the next valid frame from
    # the buffer. Validate the tail before accepting it as padding, not after.
    tail = packet[offset:total_packet_len]
    # A legitimate alignment tail can never contain the frame magic word; its
    # presence anywhere in the tail is conclusive proof this is the next frame's
    # start being swallowed, not filler bytes.
    if MAGIC_WORD in tail:
        raise _FrameRejectedError(TiFrameRejectReason.BAD_PACKET_PADDING)
    if profile.packet_alignment > 1:
        # Padding exists only to round total_packet_len up to the profile's
        # alignment, so it is bounded by that alignment — except when the
        # unpadded body length is already itself aligned, in which case the
        # smallest possible *nonzero* padding is one full alignment unit (this
        # happens legitimately, e.g. when a producer pads to a coarser multiple
        # than the protocol strictly requires). Two or more full alignment units
        # of trailing bytes is implausible for any legitimate rounding and is
        # exactly the size of corruption this check exists to catch.
        max_plausible_tail = 2 * profile.packet_alignment
        if len(tail) >= max_plausible_tail:
            raise _FrameRejectedError(TiFrameRejectReason.BAD_PACKET_PADDING)

    oob_points: tuple[TiPointCloudPoint, ...] = ()
    if pending_detected_points is not None:
        oob_points = _decode_oob_points(pending_detected_points, pending_side_info)
        if len(oob_points) > limits.max_points:
            raise _FrameRejectedError(TiFrameRejectReason.TOO_MANY_POINTS)

    # The bound applies to the combined cloud actually emitted on the frame, not just
    # each contributing TLV in isolation: two TLVs individually within limits can
    # still exceed it together.
    if len(cloud_points) + len(oob_points) > limits.max_points:
        raise _FrameRejectedError(TiFrameRejectReason.TOO_MANY_POINTS)

    return TiFrame(
        version=version,
        total_packet_len=total_packet_len,
        platform=platform,
        frame_number=frame_number,
        time_cpu_cycles=time_cpu_cycles,
        num_detected_objects=num_detected_objects,
        num_tlvs=num_tlvs,
        subframe_number=subframe_number,
        targets=targets,
        points=cloud_points + oob_points,
        target_indices=target_indices,
        heights=heights,
        presence=presence,
        unknown_tlvs=tuple(unknown_tlvs),
        target_record_layout=layout_name,
        padding_bytes=total_packet_len - offset,
    )


class TiFrameParser:
    """Buffers raw UART bytes and yields fully decoded, validated
    :class:`~radiowave.adapters.mmwave.ti.models.TiFrame` objects.

    Feed it bytes in any chunking (a whole frame, a partial frame, several frames,
    one byte at a time) and it produces the same frames either way.
    """

    def __init__(
        self,
        profile: TiFirmwareProfile = TI_3D_PEOPLE_COUNTING,
        limits: TiParserLimits | None = None,
    ) -> None:
        self._profile = profile
        self._limits = limits if limits is not None else TiParserLimits()
        self._buffer = bytearray()
        self.stats = TiParserStats()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        """Drop buffered bytes, e.g. after a reconnect."""
        self._buffer.clear()

    def _consume(self, count: int) -> None:
        del self._buffer[:count]

    def _discard(self, count: int, *, resync: bool) -> None:
        if count <= 0:
            return
        del self._buffer[:count]
        self.stats.bytes_discarded += count
        if resync:
            self.stats.resyncs += 1

    def _reject(self, reason: TiFrameRejectReason) -> None:
        self.stats.frames_rejected += 1
        self.stats.reject_reasons[reason.value] = self.stats.reject_reasons.get(reason.value, 0) + 1

    def feed(self, data: bytes) -> list[TiFrame]:
        self._buffer.extend(data)
        self.stats.bytes_received += len(data)
        frames: list[TiFrame] = []

        while True:
            magic_index = self._buffer.find(MAGIC_WORD)
            if magic_index == -1:
                # A magic word could straddle this feed and the next: keep the tail
                # that might be its prefix, discard the rest.
                keep_from = max(0, len(self._buffer) - (len(MAGIC_WORD) - 1))
                self._discard(keep_from, resync=keep_from > 0)
                break
            if magic_index > 0:
                self._discard(magic_index, resync=True)

            if len(self._buffer) < FRAME_HEADER_BYTES:
                break  # partial header: wait for more bytes

            header = HEADER_STRUCT.unpack_from(self._buffer, len(MAGIC_WORD))
            total_packet_len = header[1]
            num_tlvs = header[6]

            if total_packet_len < FRAME_HEADER_BYTES:
                self._reject(TiFrameRejectReason.BAD_PACKET_LENGTH)
                self._discard(1, resync=True)
                continue
            if total_packet_len > self._limits.max_packet_bytes:
                self._reject(TiFrameRejectReason.PACKET_TOO_LARGE)
                self._discard(1, resync=True)
                continue
            if num_tlvs > self._limits.max_tlvs:
                self._reject(TiFrameRejectReason.TOO_MANY_TLVS)
                self._discard(1, resync=True)
                continue
            if (
                self._profile.packet_alignment > 1
                and total_packet_len % self._profile.packet_alignment != 0
            ):
                self._reject(TiFrameRejectReason.BAD_PACKET_ALIGNMENT)
                self._discard(1, resync=True)
                continue

            if len(self._buffer) < total_packet_len:
                break  # partial packet: wait (bounded by max_packet_bytes above)

            packet = bytes(self._buffer[:total_packet_len])
            try:
                frame = _parse_packet(packet, header, self._profile, self._limits)
            except _FrameRejectedError as exc:
                self._reject(exc.reason)
                self._discard(1, resync=True)
                continue

            frames.append(frame)
            self.stats.frames_parsed += 1
            self.stats.unknown_tlv_count += len(frame.unknown_tlvs)
            self._consume(total_packet_len)

        if len(self._buffer) > self._limits.max_buffer_bytes:
            overflow = len(self._buffer) - self._limits.max_buffer_bytes
            self._discard(overflow, resync=True)

        return frames
