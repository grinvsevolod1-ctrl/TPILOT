# -*- coding: utf-8 -*-
"""Orchestrates the tdata read chain: key_data -> localKey -> account index
list -> per-account MTP AuthKey. Ported from `TDesktop.__loadFromTData` +
`StorageAccount.readMtpData` + `Account._setMtpAuthorization` (NOTICE items
7-8). No Telegram network connection anywhere in this module.

SECURITY: never logs auth_key/localKey/passcode bytes. Returned TdataAccount
objects carry the raw auth_key (needed to build the Telethon session) --
callers must not print/log it; only session_inspector's auth_key_len and this
package's SafeMeta may cross an I/O boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from . import container, naming
from .crypto import TdataDecryptError, decrypt_local, create_local_key
from .qtstream import QDataReader, QDataStreamReadError

_MTP_AUTHORIZATION_BLOCK_ID = 75  # dbi.MtpAuthorization
_WIDE_IDS_TAG_USER = -1
_WIDE_IDS_TAG_DC = -1


class TdataPasscodeRequired(Exception):
    """The tdata's key_data is protected by a non-empty Local Passcode. This
    project never collects or stores a passcode (owner decision) -- surfaced
    as a distinct signal so the adapter maps it to
    models.FailureClass.TDATA_PASSCODE_REQUIRED."""


class TdataReadError(Exception):
    """Any other structural failure (missing file, bad checksum, no account,
    no matching auth key, ...). Message is always generic."""


@dataclass
class TdataAccount:
    index: int
    user_id: int
    dc_id: int
    auth_key: bytes  # 256 bytes; SECURITY: never log/print this


def _read_key_data(base_path: str, *, key_file: str = "data") -> tuple:
    """Returns (local_key: bytes[256], account_indices: List[int], active_index: int)."""
    try:
        payload = container.read_tdf(base_path, "key_" + key_file)
    except container.TdfFileError as exc:
        raise TdataReadError(f"could not read key_data: {exc}")

    try:
        r = QDataReader(payload)
        salt = r.read_bytearray()
        key_encrypted = r.read_bytearray()
        info_encrypted = r.read_bytearray()
    except QDataStreamReadError as exc:
        raise TdataReadError(f"key_data malformed: {exc}")

    passcode_key = create_local_key(salt, b"")  # this project never collects a passcode
    try:
        key_inner = decrypt_local(key_encrypted, passcode_key)
    except TdataDecryptError:
        # Empty passcode failed to open the outer layer -> the tdata is
        # protected by a real Local Passcode we don't have and don't collect.
        raise TdataPasscodeRequired("tdata is protected by a Local Passcode")

    if len(key_inner) < 256:
        raise TdataReadError("key_data inner payload too short for local key")
    local_key = key_inner[:256]

    try:
        info_data = decrypt_local(info_encrypted, local_key)
    except TdataDecryptError as exc:
        raise TdataReadError(f"could not decrypt account index list: {exc}")

    try:
        ir = QDataReader(info_data)
        count = ir.read_int32()
        if count <= 0:
            raise TdataReadError("account index list is empty")
        indices = [ir.read_int32() for _ in range(count)]
        active_index = 0
        if not ir.at_end():
            active_index = ir.read_int32()
    except QDataStreamReadError as exc:
        raise TdataReadError(f"account index list malformed: {exc}")

    return local_key, indices, active_index


def _read_account_auth(base_path: str, local_key: bytes, index: int) -> TdataAccount:
    basename = naming.account_basename(index)
    try:
        outer_payload = container.read_tdf(base_path, basename)
    except container.TdfFileError as exc:
        raise TdataReadError(f"could not read account data for index {index}: {exc}")

    try:
        outer = QDataReader(outer_payload)
        encrypted = outer.read_bytearray()
    except QDataStreamReadError as exc:
        raise TdataReadError(f"account container malformed for index {index}: {exc}")

    try:
        inner = decrypt_local(encrypted, local_key)
    except TdataDecryptError as exc:
        raise TdataReadError(f"could not decrypt account data for index {index}: {exc}")

    try:
        ir = QDataReader(inner)
        block_id = ir.read_int32()
        if block_id != _MTP_AUTHORIZATION_BLOCK_ID:
            raise TdataReadError(f"unexpected block id {block_id!r} for index {index}")
        serialized = ir.read_bytearray()
    except QDataStreamReadError as exc:
        raise TdataReadError(f"account mtp container malformed for index {index}: {exc}")

    return _parse_mtp_authorization(serialized, index)


def _parse_mtp_authorization(serialized: bytes, index: int) -> TdataAccount:
    try:
        sr = QDataReader(serialized)
        user_id = sr.read_int32()
        main_dc_id = sr.read_int32()
        if user_id == _WIDE_IDS_TAG_USER and main_dc_id == _WIDE_IDS_TAG_DC:
            user_id = sr.read_uint64()
            main_dc_id = sr.read_int32()

        def read_keys():
            count = sr.read_int32()
            keys = []
            for _ in range(count):
                dc_id = sr.read_int32()
                key_bytes = sr.read_raw(256)
                keys.append((dc_id, key_bytes))
            return keys

        mtp_keys = read_keys()
        _mtp_keys_to_destroy = read_keys()  # consumed for stream sanity, never used
    except QDataStreamReadError as exc:
        raise TdataReadError(f"mtp authorization malformed for index {index}: {exc}")

    for dc_id, key_bytes in mtp_keys:
        if dc_id == main_dc_id:
            return TdataAccount(index=index, user_id=int(user_id), dc_id=int(dc_id), auth_key=key_bytes)

    raise TdataReadError(f"no auth key matching main dc for index {index}")


def read_tdata_accounts(base_path: str, *, key_file: str = "data") -> List[TdataAccount]:
    """Read every loadable account under a tdata root (`base_path`).

    Passcode is never accepted/collected -- a passcode-protected tdata raises
    TdataPasscodeRequired. Individual account slots that fail to parse (e.g. a
    stale/half-written slot) are skipped, matching upstream's own
    try/except-per-account loop; if NONE load, TdataReadError is raised.
    """
    local_key, indices, _active_index = _read_key_data(base_path, key_file=key_file)

    accounts: List[TdataAccount] = []
    last_error: Optional[Exception] = None
    for idx in indices:
        try:
            accounts.append(_read_account_auth(base_path, local_key, idx))
        except TdataReadError as exc:
            last_error = exc
            continue

    if not accounts:
        raise last_error or TdataReadError("no account could be loaded from tdata")
    return accounts
