# -*- coding: utf-8 -*-
"""Deterministic per-account data-name -> on-disk basename derivation.

Ported from `Storage.ComposeDataString` / `ComputeDataNameKey` / `ToFilePart`
(NOTICE item 6). Verified byte-exact against the real authorized test fixture:
compute_data_name_key("data") -> to_file_part(...) == "D877F783D5D3EF8C",
matching that fixture's actual tdata account-folder name.
"""
from __future__ import annotations

import hashlib

_HEX_DIGITS = "0123456789ABCDEF"


def compose_data_string(data_name: str, index: int) -> str:
    """account index 0 -> data_name unchanged; index>0 -> data_name + "#<index+1>".
    Also strips any literal '#' already in data_name, matching upstream."""
    result = str(data_name or "").replace("#", "")
    if index > 0:
        result += f"#{index + 1}"
    return result


def compute_data_name_key(data_name: str) -> int:
    """md5(name) read as a little-endian 128-bit integer."""
    digest = hashlib.md5(data_name.encode("utf-8")).digest()
    return int.from_bytes(digest, "little")


def to_file_part(val: int) -> str:
    """16 hex characters, low-nibble-first (i.e. NOT standard big-endian hex
    encoding) -- consumes only the low 64 bits of `val`."""
    out = []
    for _ in range(16):
        v = val & 0xF
        out.append(_HEX_DIGITS[v])
        val >>= 4
    return "".join(out)


def account_basename(index: int, *, key_file: str = "data") -> str:
    """The on-disk basename (before the ReadFile "s"/"1"/"0" suffix try-order)
    for the account at `index` under a tdata root with the given key_file
    ("data" is TDesktop's default and this project's only supported value)."""
    data_name = compose_data_string(key_file, index)
    return to_file_part(compute_data_name_key(data_name))
