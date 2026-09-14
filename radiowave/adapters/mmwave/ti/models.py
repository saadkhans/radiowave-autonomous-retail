"""Parsed TI mmWave frame DTOs.

These are adapter-private value objects, not canonical contracts, so plain frozen
dataclasses are used instead of :class:`~radiowave.contracts._base.FrozenModel`.
Everything here is still in the TI sensor's native frame; normalization into store
coordinates happens outside this package.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TiTarget:
    """One tracked target record, in the sensor's native frame."""

    native_track_id: int
    x: float
    y: float
    z: float
    vx: float
    vy: float
    vz: float
    ax: float
    ay: float
    az: float
    error_covariance: tuple[float, ...]
    gating_gain: float
    confidence: float | None


@dataclass(frozen=True, slots=True)
class TiPointCloudPoint:
    """One point-cloud point, in the sensor's native Cartesian frame.

    Spherical-to-Cartesian conversion follows the TI convention (+azimuth -> +x):
    ``x = r * sin(az) * cos(el)``, ``y = r * cos(az) * cos(el)``, ``z = r * sin(el)``.
    """

    x: float
    y: float
    z: float
    range_m: float
    azimuth_rad: float
    elevation_rad: float
    doppler_mps: float
    snr: float


@dataclass(frozen=True, slots=True)
class TiTargetHeight:
    """Per-target height envelope reported alongside the 3D target list."""

    native_track_id: int
    max_z: float
    min_z: float


@dataclass(frozen=True, slots=True)
class TiFrame:
    """One fully decoded TI UART frame."""

    version: int
    total_packet_len: int
    platform: int
    frame_number: int
    time_cpu_cycles: int
    num_detected_objects: int
    num_tlvs: int
    subframe_number: int
    targets: tuple[TiTarget, ...]
    points: tuple[TiPointCloudPoint, ...]
    target_indices: tuple[int, ...]
    heights: tuple[TiTargetHeight, ...]
    presence: int | None
    unknown_tlvs: tuple[tuple[int, int], ...]
    """(type, length) for each TLV type this parser does not decode; payload is dropped."""
    target_record_layout: str | None
    """Name of the :data:`~radiowave.adapters.mmwave.ti.protocol.TARGET_RECORD_LAYOUTS`
    entry used to decode ``targets``, or ``None`` if no target-list TLV was present."""
    padding_bytes: int
