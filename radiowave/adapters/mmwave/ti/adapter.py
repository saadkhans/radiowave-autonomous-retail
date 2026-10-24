"""TI target list -> canonical person observations.

This is the vendor boundary. A parsed :class:`TiFrame` (TI target ids, TI axes, TI
frame counters) goes in; store-frame :class:`PersonObservation` values come out, via
the same :class:`NativeRadarSample` contract and :class:`ObservationNormalizer` that
the synthetic radar uses. Nothing downstream of this module knows the radar is a TI.

Policies implemented here (see ``config.py`` and the Phase 3 doc):

* **Timestamps.** The observation timestamp is the host's timezone-aware UTC receive
  time of the frame. TI frame numbers and CPU cycle counters are not a synchronized
  clock and are retained as native metadata only.
* **Native ids are hints, scoped to the stream generation.** ``native_track_id``
  passed to :class:`NativeRadarSample` (and so ``NATIVE_TRACK_KEY`` in the
  observation) is ``f"g{generation}:t{tid}"``, never the raw TI tracker id alone.
  A TI tracker id is only unique within one connection to the device; after a
  restart or reconnect the firmware can hand the same small integer to a
  completely different person, and fusion keys its continuity hint cache by
  this string, so an unscoped id would let a recycled TI id inherit a stale
  canonical track (``P0001``, its cart, its session) once it drifts back within
  the spatial gate. Scoping the hint by generation makes that impossible: a
  reused raw id after a restart always produces a different hint string, so
  fusion treats it as a fresh continuity claim subject to the normal spatial
  gates. Fusion assigns the canonical ``P0001`` identity and owns continuity
  and re-acquisition; it never sees the raw TI id. The raw TI id is preserved,
  unscoped, as provenance only under ``NATIVE_TI_TRACK_ID_KEY``.
* **Stream generations.** A frame number that goes backwards means the device
  restarted; the adapter opens a new *stream generation* so ``(generation,
  frame, subframe)`` stays a unique key and the native-id hint namespace above
  is scoped to it. Reconnects also open a new generation. A frame+subframe
  pair seen twice in one generation (UART re-read, buffered bytes after
  reconnect) is dropped and counted; two different subframes sharing one frame
  number are neither a restart nor a duplicate.
* **Limitation: restart detection is backwards-motion-only.** A device restart
  whose frame counter resumes at an equal or higher value (no reset, or
  wrap-around) is not recognized as a new generation, and if its
  ``(generation, frame, subframe)`` key collides with an entry still in the
  seen-frame cache that frame is treated as a duplicate rather than as a fresh
  stream. A reconnect, by contrast, always opens a new generation regardless
  of the frame number it resumes at.
* **Coordinates.** TI axes are mapped into the sensor frame explicitly
  (:meth:`TiCoordinateConvention.to_sensor_frame`); the store twin's pose does the
  rest through the existing rigid-transform path.
* **Uncertainty / confidence.** Conservative configured baselines; the firmware's
  confidence level is used only when present and inside ``[0, 1]``, and is capped.
"""

from __future__ import annotations

import math
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from radiowave.adapters.mmwave.base import NativeRadarSample
from radiowave.adapters.mmwave.ti.config import TiAdapterConfig
from radiowave.adapters.mmwave.ti.models import TiFrame, TiTarget
from radiowave.contracts._base import ensure_utc
from radiowave.contracts.geometry import SensorCoordinate, Velocity
from radiowave.contracts.observations import PersonObservation
from radiowave.contracts.store import SourceType
from radiowave.digital_twin.registry import StoreRegistry
from radiowave.ingestion.normalization import ObservationNormalizer

#: Metadata keys written by this adapter (all prefixed ``native_`` except the
#: confidence provenance). They are debug/replay provenance, never identity.
NATIVE_FRAME_NUMBER_KEY = "native_frame_number"
NATIVE_STREAM_GENERATION_KEY = "native_stream_generation"
NATIVE_FIRMWARE_PROFILE_KEY = "native_firmware_profile"
NATIVE_TARGET_LAYOUT_KEY = "native_target_layout"
NATIVE_TIME_CPU_CYCLES_KEY = "native_time_cpu_cycles"
NATIVE_CONFIDENCE_KEY = "native_confidence"
NATIVE_TI_POSITION_KEY = "native_ti_position"
#: Raw TI tracker id (an int), unscoped, kept as provenance only. Never used as a
#: continuity hint: see the module docstring. Compare with ``NATIVE_TRACK_KEY``
#: (in the observation metadata), which carries the generation-scoped hint string
#: that fusion actually keys continuity on.
NATIVE_TI_TRACK_ID_KEY = "native_ti_track_id"
CONFIDENCE_SOURCE_KEY = "confidence_source"

_SEEN_FRAMES_CAPACITY = 4096


@dataclass(slots=True)
class TiNormalizerCounters:
    """Diagnostics for the normalization stage (per adapter instance)."""

    frames_seen: int = 0
    frames_duplicate: int = 0
    frames_restart: int = 0
    generation: int = 1
    targets_seen: int = 0
    targets_dropped_implausible: int = 0
    observations_emitted: int = 0
    confidence_from_firmware: int = 0
    confidence_from_baseline: int = 0
    last_frame_number: int | None = None
    last_subframe_number: int | None = None
    last_frame_at: datetime | None = None
    restart_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "frames_seen": self.frames_seen,
            "frames_duplicate": self.frames_duplicate,
            "frames_restart": self.frames_restart,
            "generation": self.generation,
            "targets_seen": self.targets_seen,
            "targets_dropped_implausible": self.targets_dropped_implausible,
            "observations_emitted": self.observations_emitted,
            "confidence_from_firmware": self.confidence_from_firmware,
            "confidence_from_baseline": self.confidence_from_baseline,
            "last_frame_number": self.last_frame_number,
            "last_subframe_number": self.last_subframe_number,
            "last_frame_at": None if self.last_frame_at is None else self.last_frame_at.isoformat(),
        }


class TiTargetNormalizer:
    """Turns parsed TI frames into canonical person observations for one sensor."""

    def __init__(
        self,
        config: TiAdapterConfig,
        registry: StoreRegistry,
        *,
        clock: Callable[[], datetime] | None = None,
        scenario_id: str | None = None,
    ) -> None:
        sensor = registry.sensor(config.sensor_id)
        if sensor.modality != SourceType.MMWAVE:
            msg = (
                f"sensor {config.sensor_id!r} is registered as {sensor.modality.value}, "
                "the TI adapter needs an MMWAVE sensor"
            )
            raise ValueError(msg)
        self._config = config
        self._sensor_height_m = sensor.pose.position.z
        self._normalizer = ObservationNormalizer(registry, scenario_id)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sequence = 0
        self._seen: OrderedDict[tuple[int, int, int], None] = OrderedDict()
        self.counters = TiNormalizerCounters()

    @property
    def sensor_id(self) -> str:
        return self._config.sensor_id

    @property
    def generation(self) -> int:
        return self.counters.generation

    def new_generation(self, reason: str) -> int:
        """Open a new stream generation (device restart or transport reconnect)."""
        self.counters.generation += 1
        self.counters.last_frame_number = None
        self.counters.last_subframe_number = None
        self.counters.restart_reasons.append(reason)
        del self.counters.restart_reasons[:-16]
        return self.counters.generation

    # ------------------------------------------------------------------ frames

    def _admit_frame(self, frame: TiFrame) -> bool:
        """Apply the restart and duplicate rules; False means drop the frame.

        Restart detection compares frame numbers only (tracked once per frame, not
        per subframe): two subframes sharing one frame number are not a restart.
        Deduplication keys on ``(generation, frame_number, subframe_number)`` so a
        genuine repeated subframe is dropped, but distinct subframes of the same
        frame number are both admitted.
        """
        counters = self.counters
        counters.frames_seen += 1
        last = counters.last_frame_number
        if last is not None and frame.frame_number < last:
            counters.frames_restart += 1
            self.new_generation(f"frame number went backwards ({last} -> {frame.frame_number})")
        key = (counters.generation, frame.frame_number, frame.subframe_number)
        if key in self._seen:
            counters.frames_duplicate += 1
            return False
        self._seen[key] = None
        while len(self._seen) > _SEEN_FRAMES_CAPACITY:
            self._seen.popitem(last=False)
        counters.last_frame_number = frame.frame_number
        counters.last_subframe_number = frame.subframe_number
        return True

    def frame_to_samples(
        self, frame: TiFrame, received_at: datetime | None = None
    ) -> list[NativeRadarSample]:
        """Sensor-frame samples for one frame (empty for a duplicate or target-less frame)."""
        timestamp = ensure_utc(received_at) if received_at is not None else self._clock()
        timestamp = ensure_utc(timestamp)
        if not self._admit_frame(frame):
            return []
        self.counters.last_frame_at = timestamp
        samples: list[NativeRadarSample] = []
        for target in frame.targets:
            self.counters.targets_seen += 1
            sample = self._sample_for(frame, target, timestamp)
            if sample is None:
                self.counters.targets_dropped_implausible += 1
                continue
            samples.append(sample)
        return samples

    def frame_to_observations(
        self, frame: TiFrame, received_at: datetime | None = None
    ) -> list[PersonObservation]:
        """Store-frame observations for one frame, through the shared normalizer."""
        observations = [
            self._normalizer.person(sample) for sample in self.frame_to_samples(frame, received_at)
        ]
        self.counters.observations_emitted += len(observations)
        return observations

    # ----------------------------------------------------------------- targets

    def _confidence(self, target: TiTarget) -> tuple[float, str]:
        policy = self._config.observation
        raw = target.confidence
        if (
            policy.use_firmware_confidence
            and raw is not None
            and math.isfinite(raw)
            and 0.0 <= raw <= 1.0
        ):
            self.counters.confidence_from_firmware += 1
            return (min(raw, policy.confidence_ceiling), "firmware")
        self.counters.confidence_from_baseline += 1
        return (policy.baseline_confidence, "baseline")

    def _sample_for(
        self, frame: TiFrame, target: TiTarget, timestamp: datetime
    ) -> NativeRadarSample | None:
        config = self._config
        convention = config.coordinates
        x, y, z = convention.to_sensor_frame(
            target.x, target.y, target.z, sensor_height_m=self._sensor_height_m
        )
        vx, vy, vz = convention.to_sensor_frame(
            target.vx, target.vy, target.vz, sensor_height_m=0.0
        )
        values = (x, y, z, vx, vy, vz)
        if not all(math.isfinite(value) for value in values):
            return None
        policy = config.observation
        if math.hypot(x, y) > policy.max_range_m or abs(z) > policy.max_abs_height_m:
            return None
        confidence, source = self._confidence(target)
        # Scoped to the stream generation: see the module docstring and
        # NATIVE_TI_TRACK_ID_KEY for why the raw TI id alone must never be the hint.
        native_hint = f"g{self.counters.generation}:t{target.native_track_id}"
        metadata: dict[str, Any] = {
            NATIVE_FRAME_NUMBER_KEY: frame.frame_number,
            NATIVE_STREAM_GENERATION_KEY: self.counters.generation,
            NATIVE_FIRMWARE_PROFILE_KEY: config.firmware_profile,
            NATIVE_TARGET_LAYOUT_KEY: frame.target_record_layout,
            NATIVE_TIME_CPU_CYCLES_KEY: frame.time_cpu_cycles,
            NATIVE_CONFIDENCE_KEY: (
                target.confidence
                if target.confidence is not None and math.isfinite(target.confidence)
                else None
            ),
            NATIVE_TI_POSITION_KEY: {"x": target.x, "y": target.y, "z": target.z},
            NATIVE_TI_TRACK_ID_KEY: target.native_track_id,
            CONFIDENCE_SOURCE_KEY: source,
        }
        self._sequence += 1
        return NativeRadarSample(
            sensor_id=config.sensor_id,
            sequence=self._sequence,
            timestamp=timestamp,
            native_track_id=native_hint,
            position=SensorCoordinate(x=x, y=y, z=z, frame_id=config.sensor_id),
            velocity=Velocity(vx=vx, vy=vy, vz=vz),
            track_confidence=confidence,
            sigma_m=policy.baseline_sigma_m,
            metadata=metadata,
        )
