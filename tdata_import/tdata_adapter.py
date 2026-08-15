# -*- coding: utf-8 -*-
"""Public adapter: Telegram Desktop tdata -> a fresh Telethon v7 `.session`.

Fallback path only (owner decision #4: a ready `.session` always has priority
over this). Uses the vendored, from-scratch tdata reader
(tdata_import.vendor.tdesktop -- see its NOTICE for exact provenance) to
extract (dc_id, user_id, auth_key) with ZERO Telegram network activity, then
writes those into a brand-new Telethon SQLiteSession using Telethon's OWN
session class (not hand-rolled SQL) so the on-disk schema is guaranteed to
match this project's installed telethon==1.42.0 exactly. The result is then
run through session_inspector.py -- the SAME static validator used for a
ready `.session` -- so both import paths converge on one validation gate.

This is the first module in the package that imports telethon (SQLiteSession
needs it to build a real session file); nothing here calls .connect() or any
other network method.
"""
from __future__ import annotations

import os

from telethon.crypto import AuthKey
from telethon.sessions import SQLiteSession

from . import session_inspector
from .errors import MultipleAccounts, SessionInstallFailed, TdataConversionFailed, TdataPasscodeRequired
from .models import SessionCandidate
from .vendor.tdesktop import reader as tdata_reader
from .vendor.tdesktop.dcs import address_for_dc


def _remove_session_and_sidecars(path: str) -> None:
    """Remove a stale scratch session file + its Telethon sidecar before a
    fresh write. Only ever called on a path THIS module owns (a temp working
    file), never on a live manager session."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = path + suffix
        if os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass


def convert_tdata_to_session(tdata_dir: str, dest_session_path: str,
                             *, known_session_paths=None) -> SessionCandidate:
    """Convert the single account found under `tdata_dir` into a fresh
    Telethon session at `dest_session_path` (a scratch/working path owned by
    the caller -- NOT a live manager session path).

    Raises:
        TdataPasscodeRequired -- tdata is protected by a Local Passcode.
        MultipleAccounts      -- more than one loadable account slot in tdata.
        TdataConversionFailed -- any other structural read failure.
        SessionInstallFailed  -- could not write the scratch session file.

    Returns a SessionCandidate with valid=True and origin="tdata_converted"
    on success (already passed through session_inspector). Never raises for a
    successfully-produced-but-invalid session -- that would be an internal
    contradiction (this module wrote it), so it's treated as a hard failure
    instead (SessionInstallFailed), not returned as valid=False.
    """
    try:
        accounts = tdata_reader.read_tdata_accounts(tdata_dir)
    except tdata_reader.TdataPasscodeRequired as exc:
        raise TdataPasscodeRequired(str(exc))
    except tdata_reader.TdataReadError as exc:
        raise TdataConversionFailed(str(exc))

    if len(accounts) > 1:
        raise MultipleAccounts(f"tdata contains {len(accounts)} loadable accounts")
    account = accounts[0]

    try:
        server_address, port = address_for_dc(account.dc_id)
    except ValueError as exc:
        raise TdataConversionFailed(str(exc))

    _remove_session_and_sidecars(dest_session_path)
    os.makedirs(os.path.dirname(dest_session_path) or ".", exist_ok=True)

    try:
        session = SQLiteSession(dest_session_path)
        try:
            session.set_dc(account.dc_id, server_address, port)
            session.auth_key = AuthKey(account.auth_key)
            session.save()
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001
        raise SessionInstallFailed(f"could not write converted session: {type(exc).__name__}")

    candidate = session_inspector.inspect_session(
        dest_session_path, origin="tdata_converted", known_session_paths=known_session_paths
    )
    if not candidate.valid:
        # We just wrote this file ourselves with a known-good v7 schema and a
        # real 256-byte key -- a failure here means something is wrong with
        # THIS host's environment (e.g. disk/sqlite oddity), not with the
        # source tdata. Treat as an install failure, not a conversion verdict.
        raise SessionInstallFailed(
            f"converted session failed self-validation: {candidate.failure_class or candidate.reason}"
        )
    return candidate
