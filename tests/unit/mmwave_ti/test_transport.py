from __future__ import annotations

import pytest

from radiowave.adapters.mmwave.ti.transport import (
    ByteStreamClosed,
    ChunkedByteStream,
    MemoryByteStream,
)


def test_memory_byte_stream_serves_chunks_in_order() -> None:
    stream = MemoryByteStream([b"abc", b"de"])

    assert stream.read(10) == b"abc"
    assert stream.read(10) == b"de"


def test_memory_byte_stream_truncates_oversized_chunks_and_keeps_the_remainder() -> None:
    stream = MemoryByteStream([b"abcdef"])

    assert stream.read(3) == b"abc"
    assert stream.read(10) == b"def"


def test_memory_byte_stream_raises_closed_when_exhausted() -> None:
    stream = MemoryByteStream([b"a"])
    assert stream.read(10) == b"a"

    with pytest.raises(ByteStreamClosed):
        stream.read(10)


def test_memory_byte_stream_raises_fail_after_when_given() -> None:
    stream = MemoryByteStream([b"a"], fail_after=RuntimeError("device gone"))
    assert stream.read(10) == b"a"

    with pytest.raises(RuntimeError, match="device gone"):
        stream.read(10)


def test_memory_byte_stream_read_after_close_raises_closed() -> None:
    stream = MemoryByteStream([b"a"])
    stream.close()

    with pytest.raises(ByteStreamClosed):
        stream.read(10)


def test_chunked_byte_stream_serves_fixed_size_chunks_then_closes() -> None:
    stream = ChunkedByteStream(b"abcdefg", chunk_size=3)

    assert stream.read(100) == b"abc"
    assert stream.read(100) == b"def"
    assert stream.read(100) == b"g"
    with pytest.raises(ByteStreamClosed):
        stream.read(100)


def test_chunked_byte_stream_read_after_close_raises_closed() -> None:
    stream = ChunkedByteStream(b"ab", chunk_size=1)
    stream.close()

    with pytest.raises(ByteStreamClosed):
        stream.read(1)
