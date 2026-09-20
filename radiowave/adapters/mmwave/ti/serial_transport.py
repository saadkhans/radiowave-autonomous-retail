"""Real-hardware serial transport for the TI IWR6843 UART data port.

``pyserial`` is an optional dependency (the ``hardware-ti`` extra): CI, replay and
the Observatory never open a serial port, so the import is deferred to
construction and this module is safe to import without pyserial installed.
"""

from __future__ import annotations

from typing import Protocol

from radiowave.adapters.mmwave.ti.transport import ByteStreamClosed, ByteStreamError

_MISSING_DEPENDENCY_MESSAGE = (
    "TI serial support not installed; install radiowave-autonomous-retail[hardware-ti] (pyserial)"
)


class _SerialPort(Protocol):
    """The slice of ``serial.Serial`` this transport relies on."""

    @property
    def is_open(self) -> bool: ...
    def read(self, size: int) -> bytes: ...
    def close(self) -> None: ...


class SerialByteStream:
    """Reads raw bytes from a TI EVM's UART data port via pyserial."""

    def __init__(self, port: str, baudrate: int, read_timeout_s: float) -> None:
        try:
            import serial  # type: ignore[import-untyped]
        except ImportError as exc:
            raise ByteStreamError(_MISSING_DEPENDENCY_MESSAGE) from exc

        self._exception_type: type[Exception] = serial.SerialException
        try:
            port_obj: _SerialPort = serial.Serial(
                port=port, baudrate=baudrate, timeout=read_timeout_s
            )
        except (serial.SerialException, OSError) as exc:
            raise ByteStreamError(str(exc)) from exc
        self._serial: _SerialPort = port_obj

    def read(self, max_bytes: int) -> bytes:
        try:
            return self._serial.read(max_bytes)
        except (self._exception_type, OSError) as exc:
            if not self._serial.is_open:
                raise ByteStreamClosed(str(exc)) from exc
            raise ByteStreamError(str(exc)) from exc

    def close(self) -> None:
        if self._serial.is_open:
            self._serial.close()
