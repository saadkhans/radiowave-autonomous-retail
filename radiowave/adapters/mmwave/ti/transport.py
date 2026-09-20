"""Byte-stream abstraction for the TI UART link.

No ``pyserial`` import here: this module defines the transport contract plus two
deterministic, in-memory implementations for offline tests and replay. The real
serial-port implementation lives in :mod:`radiowave.adapters.mmwave.ti.serial_transport`
so importing this module never requires the optional hardware dependency.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from typing import Protocol


class ByteStreamError(RuntimeError):
    """A transport failure: device gone, I/O error, or similar."""


class ByteStreamClosed(ByteStreamError):  # noqa: N818 -- name mandated by the transport contract
    """The stream is closed or at EOF; no more bytes will ever arrive."""


class ByteStream(Protocol):
    """Anything that can hand the parser raw bytes as they arrive."""

    def read(self, max_bytes: int) -> bytes:
        """Block up to the stream's own timeout and return what is available.

        Returns ``b""`` on a timeout (no data yet). Raises :class:`ByteStreamClosed`
        once the stream is closed or the device has disconnected.
        """
        ...

    def close(self) -> None: ...


class MemoryByteStream:
    """Serves a fixed sequence of byte chunks, ignoring nothing but respecting
    ``max_bytes``: an oversized chunk is truncated and the remainder is kept for the
    next read. Once exhausted it raises ``fail_after`` if given, else
    :class:`ByteStreamClosed`."""

    def __init__(self, chunks: Iterable[bytes], *, fail_after: BaseException | None = None) -> None:
        self._chunks: deque[bytes] = deque(chunks)
        self._fail_after = fail_after
        self._closed = False

    def read(self, max_bytes: int) -> bytes:
        if self._closed:
            raise ByteStreamClosed("stream already closed")
        if not self._chunks:
            self._closed = True
            if self._fail_after is not None:
                raise self._fail_after
            raise ByteStreamClosed("no more chunks")
        chunk = self._chunks.popleft()
        if len(chunk) > max_bytes:
            self._chunks.appendleft(chunk[max_bytes:])
            return chunk[:max_bytes]
        return chunk

    def close(self) -> None:
        self._closed = True


class ChunkedByteStream:
    """Serves a single byte string in fixed-size chunks (the last may be shorter),
    then raises :class:`ByteStreamClosed`."""

    def __init__(self, data: bytes, chunk_size: int) -> None:
        if chunk_size <= 0:
            msg = "chunk_size must be positive"
            raise ValueError(msg)
        self._data = data
        self._chunk_size = chunk_size
        self._offset = 0
        self._closed = False

    def read(self, max_bytes: int) -> bytes:
        if self._closed:
            raise ByteStreamClosed("stream already closed")
        if self._offset >= len(self._data):
            self._closed = True
            raise ByteStreamClosed("end of data")
        del max_bytes  # this stream's chunking is fixed by construction, not by the caller
        end = min(self._offset + self._chunk_size, len(self._data))
        chunk = self._data[self._offset : end]
        self._offset = end
        return chunk

    def close(self) -> None:
        self._closed = True
