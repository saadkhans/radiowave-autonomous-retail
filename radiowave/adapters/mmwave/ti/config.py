"""Configuration for the TI IWR6843 adapter.

Everything a lab needs to describe one radar lives here: which serial ports to open,
which firmware output format to expect, how TI's reported axes map onto Radiowave's
sensor frame, and the conservative uncertainty/confidence baselines the adapter
applies until real calibration data exists.

The radar's *pose* is not configured here. It is part of the store twin
(:class:`~radiowave.contracts.store.Sensor`), exactly like the synthetic radars, and
:class:`TiLiveConfig` bundles a twin with one adapter so a single JSON file describes
a lab setup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, model_validator

from radiowave.contracts._base import FrozenModel
from radiowave.contracts.store import SourceType, Store

FirmwareProfileName = Literal["ti-3d-people-counting", "ti-oob-sdk3"]
TargetRecordLayoutName = Literal["3d_v2", "3d_v1", "2d"]


class TiSerialConfig(FrozenModel):
    """Serial ports of one IWR6843-class board.

    TI demo firmware exposes two UARTs: a configuration/CLI port (115200 baud by
    default) and a data port (921600 baud by default). Phase 3 only reads the data
    port; the chip is configured externally (see the hardware guide), so ``cli_port``
    is optional and retained only for later automation.
    """

    data_port: str = Field(
        min_length=1, description="Data UART device, e.g. COM6 (Windows) or /dev/ttyUSB1"
    )
    data_baud_rate: int = Field(default=921_600, gt=0)
    cli_port: str | None = Field(
        default=None, description="Configuration UART, e.g. COM5 or /dev/ttyUSB0 (optional)"
    )
    cli_baud_rate: int = Field(default=115_200, gt=0)
    read_timeout_s: float = Field(
        default=0.5, gt=0.0, le=30.0, description="Blocking read timeout; also the stop latency"
    )
    read_chunk_bytes: int = Field(default=4096, ge=64, le=65_536)


class TiCoordinateConvention(FrozenModel):
    """How the firmware's reported axes map onto Radiowave's sensor frame.

    Radiowave's sensor frame (see ``digital_twin.geometry``) is right-handed with
    ``x`` along the boresight (forward), ``y`` to the sensor's left and ``z`` up, with
    its origin at the sensor itself; the store twin's :class:`SensorPose` then places
    it in the world.

    The defaults describe the TI 3D people counting demo output: ``X`` lateral
    (positive to the sensor's right when looking out along the boresight), ``Y``
    forward (range direction), ``Z`` vertical up, metres, with ``Z = 0`` on the floor
    when the chip's ``sensorPosition`` height is configured. Every one of these
    assumptions must be verified on first bring-up (walk left/right, walk away): the
    experiment document lists the checks. A firmware that reports height relative to
    the sensor instead of the floor sets ``z_origin = "sensor"``.
    """

    forward_axis: Literal["x", "y"] = Field(
        default="y", description="Which reported axis points away from the radar"
    )
    lateral_positive: Literal["right", "left"] = Field(
        default="right",
        description="Sign of the reported lateral axis, seen from behind the radar",
    )
    z_origin: Literal["floor", "sensor"] = Field(
        default="floor", description="Where the reported Z axis has its zero"
    )
    units: Literal["m"] = Field(default="m", description="Metres only; no conversion")

    def to_sensor_frame(
        self, x: float, y: float, z: float, *, sensor_height_m: float
    ) -> tuple[float, float, float]:
        """Map one reported TI point/velocity component triple into the sensor frame.

        ``sensor_height_m`` is only used when ``z_origin`` is ``floor`` (positions);
        callers pass ``0.0`` for velocities and other pure directions.
        """
        forward, lateral = (y, x) if self.forward_axis == "y" else (x, y)
        left = -lateral if self.lateral_positive == "right" else lateral
        up = z - sensor_height_m if self.z_origin == "floor" else z
        return (forward, left, up)


class TiObservationPolicy(FrozenModel):
    """Baselines applied to every emitted observation until the radar is calibrated.

    These are adapter/configuration-level numbers, not measured characteristics. The
    IWR6843 target list carries floating-point coordinates and an error covariance,
    but neither has been validated against ground truth in this repository yet, so
    the adapter reports a deliberately conservative isotropic sigma and never claims
    a confidence of 1.0. ``docs/experiments`` defines the measurements that will
    replace these values.
    """

    baseline_sigma_m: float = Field(
        default=0.35,
        gt=0.0,
        le=5.0,
        description="1-sigma position uncertainty (metres) reported for every target",
    )
    baseline_confidence: Annotated[float, Field(ge=0.0, lt=1.0)] = Field(
        default=0.6,
        description="Confidence used when the firmware provides no usable quality value",
    )
    use_firmware_confidence: bool = Field(
        default=True,
        description="Use the target list's confidence level when present and inside [0, 1]",
    )
    confidence_ceiling: Annotated[float, Field(ge=0.0, lt=1.0)] = Field(
        default=0.9,
        description="Upper bound on any firmware-derived confidence; a radar track is never "
        "certain evidence of a person",
    )
    max_range_m: float = Field(
        default=15.0,
        gt=0.0,
        description="Targets reported farther than this (horizontal, sensor frame) are "
        "dropped as implausible and counted",
    )
    max_abs_height_m: float = Field(
        default=4.0,
        gt=0.0,
        description="Targets whose sensor-frame |z| exceeds this are dropped and counted",
    )
    stale_after_s: float = Field(
        default=2.0,
        gt=0.0,
        description="Seconds without a parsed frame before the stream is reported STALE",
    )

    @model_validator(mode="after")
    def _confidence_bounds_ordered(self) -> TiObservationPolicy:
        if self.baseline_confidence > self.confidence_ceiling:
            msg = "baseline_confidence must be <= confidence_ceiling"
            raise ValueError(msg)
        return self


class TiReconnectPolicy(FrozenModel):
    """Bounded, backed-off reconnection after a transport failure."""

    enabled: bool = True
    initial_delay_s: float = Field(default=1.0, gt=0.0, le=60.0)
    max_delay_s: float = Field(default=10.0, gt=0.0, le=300.0)
    max_attempts: int = Field(
        default=20, ge=1, description="Consecutive failed attempts before giving up (ERROR)"
    )

    @model_validator(mode="after")
    def _ordered(self) -> TiReconnectPolicy:
        if self.max_delay_s < self.initial_delay_s:
            msg = "max_delay_s must be >= initial_delay_s"
            raise ValueError(msg)
        return self


class TiRawCaptureConfig(FrozenModel):
    """Optional raw UART capture for parser debugging. Off unless a path is given.

    Files land under the git-ignored ``data/captures/`` tree by convention and are
    size-capped; they are never required for normal operation or replay.
    """

    path: str | None = Field(default=None, description="Destination file (created/truncated)")
    max_bytes: int = Field(default=64 * 1024 * 1024, ge=1024)


class TiParserLimitsConfig(FrozenModel):
    """Bounds that keep a corrupt or hostile byte stream from growing memory."""

    max_packet_bytes: int = Field(default=65_536, ge=64, le=1_048_576)
    max_tlvs: int = Field(default=32, ge=1, le=256)
    max_targets: int = Field(default=64, ge=1, le=1024)
    max_points: int = Field(default=4096, ge=1, le=65_536)
    max_buffer_bytes: int = Field(default=262_144, ge=1024, le=8_388_608)

    @model_validator(mode="after")
    def _packet_fits_in_buffer(self) -> TiParserLimitsConfig:
        if self.max_packet_bytes > self.max_buffer_bytes:
            msg = (
                f"max_packet_bytes ({self.max_packet_bytes}) must be <= max_buffer_bytes "
                f"({self.max_buffer_bytes}); a packet larger than the buffer could never complete"
            )
            raise ValueError(msg)
        return self


class TiAdapterConfig(FrozenModel):
    """Everything about one radar except its serial ports and its pose."""

    sensor_id: str = Field(min_length=1, description="Radiowave sensor id from the store twin")
    firmware_profile: FirmwareProfileName = "ti-3d-people-counting"
    target_record_layout: TargetRecordLayoutName | None = Field(
        default=None, description="Force a target record layout; None auto-detects by length"
    )
    coordinates: TiCoordinateConvention = Field(default_factory=TiCoordinateConvention)
    observation: TiObservationPolicy = Field(default_factory=TiObservationPolicy)
    reconnect: TiReconnectPolicy = Field(default_factory=TiReconnectPolicy)
    raw_capture: TiRawCaptureConfig = Field(default_factory=TiRawCaptureConfig)
    parser_limits: TiParserLimitsConfig = Field(default_factory=TiParserLimitsConfig)


class TiLiveConfig(FrozenModel):
    """One lab setup: the store twin (with the radar's measured pose), ports and policy."""

    store: Store
    serial: TiSerialConfig
    adapter: TiAdapterConfig

    @model_validator(mode="after")
    def _sensor_is_in_twin(self) -> TiLiveConfig:
        for sensor in self.store.sensors:
            if sensor.sensor_id == self.adapter.sensor_id:
                if sensor.modality != SourceType.MMWAVE:
                    msg = (
                        f"sensor {sensor.sensor_id!r} is registered as {sensor.modality.value}, "
                        "the TI adapter needs an MMWAVE sensor"
                    )
                    raise ValueError(msg)
                return self
        msg = f"adapter.sensor_id {self.adapter.sensor_id!r} is not a sensor of the store twin"
        raise ValueError(msg)

    @classmethod
    def load(cls, path: str | Path) -> TiLiveConfig:
        with Path(path).open(encoding="utf-8") as handle:
            return cls.model_validate(json.load(handle))
