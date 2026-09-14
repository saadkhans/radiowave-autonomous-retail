"""TI IWR6843-class mmWave adapter. TI packet layout, TLV ids and native ids never
leave this package."""

from __future__ import annotations

from radiowave.adapters.mmwave.ti.adapter import TiTargetNormalizer
from radiowave.adapters.mmwave.ti.config import (
    TiAdapterConfig,
    TiCoordinateConvention,
    TiLiveConfig,
    TiObservationPolicy,
    TiReconnectPolicy,
    TiSerialConfig,
)
from radiowave.adapters.mmwave.ti.health import TiAdapterDiagnostics, TiStreamState
from radiowave.adapters.mmwave.ti.models import (
    TiFrame,
    TiPointCloudPoint,
    TiTarget,
    TiTargetHeight,
)
from radiowave.adapters.mmwave.ti.parser import TiFrameParser, TiParserLimits
from radiowave.adapters.mmwave.ti.protocol import (
    TI_3D_PEOPLE_COUNTING,
    TI_OOB_SDK3,
    TiFirmwareProfile,
)
from radiowave.adapters.mmwave.ti.session import TiLiveSession
from radiowave.adapters.mmwave.ti.transport import (
    ByteStream,
    ByteStreamClosed,
    ByteStreamError,
    ChunkedByteStream,
    MemoryByteStream,
)

__all__ = [
    "TI_3D_PEOPLE_COUNTING",
    "TI_OOB_SDK3",
    "ByteStream",
    "ByteStreamClosed",
    "ByteStreamError",
    "ChunkedByteStream",
    "MemoryByteStream",
    "TiAdapterConfig",
    "TiAdapterDiagnostics",
    "TiCoordinateConvention",
    "TiFirmwareProfile",
    "TiFrame",
    "TiFrameParser",
    "TiLiveConfig",
    "TiLiveSession",
    "TiObservationPolicy",
    "TiParserLimits",
    "TiPointCloudPoint",
    "TiReconnectPolicy",
    "TiSerialConfig",
    "TiStreamState",
    "TiTarget",
    "TiTargetHeight",
    "TiTargetNormalizer",
]
