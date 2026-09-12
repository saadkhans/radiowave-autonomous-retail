"""Vision evidence adapter contract.

Vision in this platform is *selective and semantic*: a provider reports that an
interaction happened at a location, never who a person is. No frames, crops,
embeddings or biometric descriptors cross this boundary.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from pydantic import Field

from radiowave.contracts._base import FrozenModel, UnitInterval, UtcDatetime
from radiowave.contracts.geometry import SensorCoordinate
from radiowave.contracts.observations import VisionEvidenceKind


class NativeVisionDetection(FrozenModel):
    sensor_id: str = Field(min_length=1)
    sequence: int = Field(ge=0)
    timestamp: UtcDatetime
    kind: VisionEvidenceKind
    position: SensorCoordinate = Field(description="Interaction location in the camera frame")
    confidence: UnitInterval
    sigma_m: float = Field(ge=0.0)


class VisionSource(Protocol):
    def detections(self) -> Iterator[NativeVisionDetection]: ...
