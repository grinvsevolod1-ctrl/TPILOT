# -*- coding: utf-8 -*-
"""Minimal read-only Qt QDataStream-compatible byte reader.

Reimplements (stdlib only, no PyQt5) exactly the primitives the tdata read
path uses: big-endian (network byte order) fixed-width integers, the
length-prefixed QByteArray wire format, and raw unprefixed reads. This is the
documented Qt QDataStream binary format (https://doc.qt.io/qt-5/datastreamformat.html),
not upstream-proprietary logic -- see NOTICE item 1.
"""
from __future__ import annotations

import struct

# QByteArray null-length sentinel (Qt writes 0xFFFFFFFF for a null/None array,
# distinct from a present-but-empty array which is a length of 0).
_QBYTEARRAY_NULL = 0xFFFFFFFF


class QDataStreamReadError(Exception):
    """Raised when the buffer is exhausted or malformed relative to the
    expected Qt QDataStream wire format. Never wraps credential bytes in the
    message -- only offsets/lengths."""


class QDataReader:
    """Sequential big-endian reader over an in-memory buffer."""

    def __init__(self, data: bytes):
        self._data = bytes(data)
        self._pos = 0

    def __len__(self) -> int:
        return len(self._data)

    @property
    def pos(self) -> int:
        return self._pos

    def at_end(self) -> bool:
        return self._pos >= len(self._data)

    def remaining(self) -> int:
        return len(self._data) - self._pos

    def _take(self, n: int) -> bytes:
        if n < 0 or self._pos + n > len(self._data):
            raise QDataStreamReadError(
                f"buffer underrun: need {n} bytes at offset {self._pos}, have {self.remaining()}"
            )
        chunk = self._data[self._pos:self._pos + n]
        self._pos += n
        return chunk

    def read_int32(self) -> int:
        return struct.unpack(">i", self._take(4))[0]

    def read_uint32(self) -> int:
        return struct.unpack(">I", self._take(4))[0]

    def read_int64(self) -> int:
        return struct.unpack(">q", self._take(8))[0]

    def read_uint64(self) -> int:
        return struct.unpack(">Q", self._take(8))[0]

    def read_raw(self, n: int) -> bytes:
        return self._take(n)

    def read_bytearray(self) -> bytes:
        """Qt `operator>>(QDataStream&, QByteArray&)`: 4-byte BE uint32 length
        prefix (0xFFFFFFFF = null -> empty bytes here), then that many raw
        bytes."""
        length = self.read_uint32()
        if length == _QBYTEARRAY_NULL:
            return b""
        return self._take(length)
