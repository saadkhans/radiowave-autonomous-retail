from __future__ import annotations

import sys

import pytest

from radiowave.adapters.mmwave.ti.transport import ByteStreamError


def test_missing_pyserial_raises_a_clear_error(monkeypatch: pytest.MonkeyPatch) -> None:
    # Marking "serial" as None in sys.modules makes the import system raise
    # ImportError for it, regardless of whether pyserial is actually installed.
    monkeypatch.setitem(sys.modules, "serial", None)

    from radiowave.adapters.mmwave.ti.serial_transport import SerialByteStream

    with pytest.raises(ByteStreamError, match="hardware-ti"):
        SerialByteStream(port="COM3", baudrate=921_600, read_timeout_s=0.1)
