# -*- coding: utf-8 -*-
"""tdata "TDF" file container: read-only.

Ported from `Storage.ReadFile` (NOTICE item 2): 4-byte magic b"TDF$", 4-byte
little-endian version, payload, trailing 16-byte MD5 checksum over
(payload || size4LE || version4LE || magic); filename suffix try-order
"s","1","0" (TDesktop keeps up to 3 rotating copies of a file for crash
safety -- "s" is the primary/latest).

Read-only: this module never writes tdata files (the plan's fallback is
one-directional: tdata -> Telethon session, never the reverse).
"""
from __future__ import annotations

import hashlib
import os
from typing import Optional

TDF_MAGIC = b"TDF$"
_SUFFIX_TRY_ORDER = ("s", "1", "0")


class TdfFileError(Exception):
    """Missing / bad magic / bad checksum. Message never includes file
    contents, only the file's base name and a short reason."""


def _find_existing(base_path: str, file_name: str) -> Optional[str]:
    for suffix in _SUFFIX_TRY_ORDER:
        candidate = os.path.join(base_path, file_name + suffix)
        if os.path.isfile(candidate):
            return candidate
    return None


def read_tdf(base_path: str, file_name: str) -> bytes:
    """Read and integrity-check a TDF container, return its payload bytes
    (magic/version/checksum stripped). Tries the "s"/"1"/"0" suffixes in
    order and returns the first structurally valid one found."""
    last_error: Optional[Exception] = None
    tried_any = False
    for suffix in _SUFFIX_TRY_ORDER:
        path = os.path.join(base_path, file_name + suffix)
        if not os.path.isfile(path):
            continue
        tried_any = True
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            last_error = TdfFileError(f"could not read {file_name}{suffix}: {type(exc).__name__}")
            continue

        if len(raw) < 8 + 16:
            last_error = TdfFileError(f"{file_name}{suffix} too small to be a TDF container")
            continue
        magic = raw[:4]
        if magic != TDF_MAGIC:
            last_error = TdfFileError(f"{file_name}{suffix} has invalid TDF magic")
            continue
        version = int.from_bytes(raw[4:8], "little")
        body = raw[8:]
        data_len = len(body) - 16
        if data_len < 0:
            last_error = TdfFileError(f"{file_name}{suffix} truncated")
            continue
        payload = body[:data_len]
        stored_md5 = body[data_len:]

        check = hashlib.md5(
            payload + data_len.to_bytes(4, "little") + version.to_bytes(4, "little") + magic
        ).digest()
        if check != stored_md5:
            last_error = TdfFileError(f"{file_name}{suffix} checksum mismatch")
            continue

        return payload

    if not tried_any:
        raise TdfFileError(f"{file_name} not found (tried suffixes {_SUFFIX_TRY_ORDER!r})")
    raise last_error or TdfFileError(f"{file_name}: no valid TDF copy found")


def file_exists(base_path: str, file_name: str) -> bool:
    return _find_existing(base_path, file_name) is not None
