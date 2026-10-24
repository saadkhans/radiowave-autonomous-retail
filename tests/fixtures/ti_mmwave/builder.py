"""Deterministic, programmatic TI UART packet builders for tests.

No binary blobs: every fixture is assembled from the same structs the parser
itself uses, so a change to the wire format only has to be made in one place.
"""

from __future__ import annotations

import struct

from radiowave.adapters.mmwave.ti.protocol import (
    FRAME_HEADER_BYTES,
    HEADER_STRUCT,
    MAGIC_WORD,
    TLV_HEADER_STRUCT,
    TiTlvType,
    target_record_layout_by_name,
)

_ERROR_COVARIANCE_LENGTH = {"3d_v2": 16, "3d_v1": 9, "2d": 9}


def build_target_record(
    tid: int,
    x: float,
    y: float,
    z: float,
    vx: float,
    vy: float,
    vz: float,
    ax: float = 0.0,
    ay: float = 0.0,
    az: float = 0.0,
    layout: str = "3d_v2",
    confidence: float = 0.9,
) -> bytes:
    """Encode one TI target record for the named layout (``3d_v2``, ``3d_v1``, ``2d``)."""
    record_layout = target_record_layout_by_name(layout)
    if record_layout is None:
        msg = f"unknown target record layout: {layout!r}"
        raise ValueError(msg)
    ec = [0.01 * (i + 1) for i in range(_ERROR_COVARIANCE_LENGTH[layout])]
    gating_gain = 4.0
    if layout == "2d":
        return record_layout.struct.pack(tid, x, y, vx, vy, ax, ay, *ec, gating_gain)
    if layout == "3d_v1":
        return record_layout.struct.pack(tid, x, y, z, vx, vy, vz, ax, ay, az, *ec, gating_gain)
    return record_layout.struct.pack(
        tid, x, y, z, vx, vy, vz, ax, ay, az, *ec, gating_gain, confidence
    )


def build_tlv(tlv_type: int, payload: bytes, *, length_override: int | None = None) -> bytes:
    """Encode one TLV: an 8-byte (type, length) header plus its payload.

    ``length_override`` lets a test declare a length independent of the actual
    payload bytes, to exercise the parser's bounds checks.
    """
    length = length_override if length_override is not None else len(payload)
    return TLV_HEADER_STRUCT.pack(tlv_type, length) + payload


def build_frame(
    *,
    frame_number: int,
    tlvs: list[bytes],
    version: int = 0x03060000,
    platform: int = 0xA6843,
    time_cpu_cycles: int = 0,
    num_detected_objects: int = 0,
    subframe_number: int = 0,
    num_tlvs_override: int | None = None,
    total_len_override: int | None = None,
    pad_to: int = 32,
) -> bytes:
    """Encode one full TI UART frame: magic word, header, TLVs, then alignment padding."""
    tlv_bytes = b"".join(tlvs)
    body_len = FRAME_HEADER_BYTES + len(tlv_bytes)
    padding = 0
    if pad_to > 0 and body_len % pad_to != 0:
        padding = pad_to - (body_len % pad_to)
    total_packet_len = total_len_override if total_len_override is not None else body_len + padding
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
    return MAGIC_WORD + header + tlv_bytes + (b"\x00" * padding)


def build_point_cloud_tlv(
    points: list[tuple[int, int, int, int, int]],
    units: tuple[float, float, float, float, float] = (0.01, 0.01, 0.01, 0.01, 1.0),
) -> bytes:
    """Encode a POINT_CLOUD_3D TLV payload: 5 float32 units then N 8-byte records.

    Each point is ``(elevation, azimuth, doppler, range, snr)`` in raw integer units
    (elevation/azimuth int8, doppler int16, range/snr uint16); ``units`` gives the
    float32 scale factor for each column, in the same order.
    """
    payload = struct.pack("<5f", *units)
    for elevation, azimuth, doppler, rng, snr in points:
        payload += struct.pack("<bbhHH", elevation, azimuth, doppler, rng, snr)
    return payload


def build_target_frame(frame_number: int, targets: list[dict[str, float | int | str]]) -> bytes:
    """Convenience: one frame containing a single TARGET_LIST_3D TLV."""
    records = b"".join(
        build_target_record(
            tid=int(t["tid"]),
            x=float(t.get("x", 0.0)),
            y=float(t.get("y", 0.0)),
            z=float(t.get("z", 0.0)),
            vx=float(t.get("vx", 0.0)),
            vy=float(t.get("vy", 0.0)),
            vz=float(t.get("vz", 0.0)),
            ax=float(t.get("ax", 0.0)),
            ay=float(t.get("ay", 0.0)),
            az=float(t.get("az", 0.0)),
            layout=str(t.get("layout", "3d_v2")),
            confidence=float(t.get("confidence", 0.9)),
        )
        for t in targets
    )
    tlv = build_tlv(TiTlvType.TARGET_LIST_3D, records)
    return build_frame(frame_number=frame_number, tlvs=[tlv])
