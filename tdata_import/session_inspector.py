# -*- coding: utf-8 -*-
"""Static, offline validation of a candidate Telethon `.session` (SQLite).

Runs BEFORE any Telegram network use. It opens the file strictly read-only and
immutable (never mutates the candidate or its sidecars), runs PRAGMA
quick_check, and checks the Telethon schema (accepts the known versions 7 and 8 --
7 for the pinned telethon==1.42.0, 8 from 1.44 on) and the single `sessions` row
(dc_id / server_address / port / auth_key length).

What this can prove offline: the file is a well-formed Telethon session DB of a
known schema with a plausibly-sized auth_key for a plausible DC. What it CANNOT
prove (needs the network, via identity_probe through the verified proxy): that
the auth_key is still authorized / not revoked, and the real account identity.

SECURITY: never returns, logs, or stores the auth_key bytes or any row content
beyond safe structural facts (schema version, dc_id, server address, port,
auth_key *length*).
"""
from __future__ import annotations

import os
import sqlite3
from typing import Iterable, Optional

from .models import FailureClass, SessionCandidate

# The Telethon session schemas this project accepts.
#
# STAGE 3: this used to be a bare `7` matching telethon==1.42.0, and the constant could
# drift from the installed library SILENTLY. Telethon 1.44 bumped CURRENT_VERSION to 8,
# so on a host that resolved a newer Telethon every prepared account was rejected with
# "unsupported schema version 8 (need 7)" -- a total offline-import outage whose message
# blames the operator's session file rather than the dependency. (That is exactly how it
# surfaced: 17 selftest failures, one root cause.)
#
# Deliberately an ALLOW-SET of known Telethon schemas, not a single version:
#   - Pinning one version rejects the other half of the real world. Live sessions on
#     disk are v7 while a newer Telethon writes v8, and BOTH are legitimate inputs --
#     tracking only the installed library would reject every existing session, which is
#     a worse outage than the one being fixed.
#   - Membership stays EXACT (never ">= 7"): an unknown future schema must still be
#     refused, because session installation is the one place this project must not guess
#     -- a wrong .session install is unrecoverable without re-login.
# Add a version here only after confirming the layout this module reads (the single
# `sessions` row: dc_id / server_address / port / auth_key) is unchanged in it.
SESSION_SCHEMA_VERSIONS = frozenset({7, 8})
# Preferred version for messages and for callers that need a single number; the schema
# the installed Telethon writes, when that is one we know.
try:  # pragma: no cover - trivial import shim
    from telethon.sessions.sqlite import CURRENT_VERSION as _TELETHON_SCHEMA_VERSION
    _lib = int(_TELETHON_SCHEMA_VERSION)
except Exception:  # Telethon absent (pure-offline tooling) or API moved
    _lib = 7
SESSION_SCHEMA_VERSION = _lib if _lib in SESSION_SCHEMA_VERSIONS else max(SESSION_SCHEMA_VERSIONS)
# A Telegram MTProto auth_key is 256 bytes.
AUTH_KEY_LEN = 256
VALID_DC_IDS = frozenset({1, 2, 3, 4, 5})
PLAUSIBLE_PORTS = frozenset({80, 443, 8080, 5222})


def _fail(path: str, origin: str, failure_class: str, reason: str) -> SessionCandidate:
    return SessionCandidate(path=path, origin=origin, valid=False,
                            failure_class=failure_class, reason=reason)


def _same_file(a: str, b: str) -> bool:
    try:
        return os.path.realpath(a).lower() == os.path.realpath(b).lower()
    except Exception:  # noqa: BLE001
        return False


def inspect_session(path: str, *, origin: str = "ready",
                    known_session_paths: Optional[Iterable[str]] = None) -> SessionCandidate:
    """Statically validate the `.session` at `path`. Never raises for a bad
    candidate -- returns a SessionCandidate with valid=False + failure_class."""
    if not path or not os.path.isfile(path):
        return _fail(path, origin, FailureClass.SESSION_CORRUPT, "file not found")
    try:
        if os.path.getsize(path) <= 0:
            return _fail(path, origin, FailureClass.SESSION_CORRUPT, "empty file")
    except OSError:
        return _fail(path, origin, FailureClass.SESSION_CORRUPT, "unreadable file")

    # Reject reusing a session file currently owned by another live manager.
    for known in (known_session_paths or ()):
        if known and _same_file(path, known):
            return _fail(path, origin, FailureClass.SESSION_CORRUPT,
                         "candidate matches a live manager session path")

    uri = f"file:{_as_uri_path(path)}?mode=ro&immutable=1"
    con = None
    try:
        con = sqlite3.connect(uri, uri=True, timeout=5)
        # quick_check surfaces a malformed image without mutating anything.
        try:
            qc = con.execute("PRAGMA quick_check;").fetchone()
        except sqlite3.DatabaseError:
            return _fail(path, origin, FailureClass.SESSION_CORRUPT, "quick_check failed")
        if not qc or str(qc[0]).lower() != "ok":
            return _fail(path, origin, FailureClass.SESSION_CORRUPT, "integrity quick_check not ok")

        tables = {
            str(r[0]).lower()
            for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "version" not in tables:
            return _fail(path, origin, FailureClass.SESSION_SCHEMA_UNSUPPORTED,
                         "no version table (not a Telethon session)")
        if "sessions" not in tables:
            return _fail(path, origin, FailureClass.SESSION_SCHEMA_UNSUPPORTED,
                         "no sessions table")

        vrow = con.execute("SELECT version FROM version").fetchone()
        version = int(vrow[0]) if vrow and vrow[0] is not None else None
        if version not in SESSION_SCHEMA_VERSIONS:
            known = ", ".join(str(v) for v in sorted(SESSION_SCHEMA_VERSIONS))
            return _fail(path, origin, FailureClass.SESSION_SCHEMA_UNSUPPORTED,
                         f"unsupported schema version {version!r} (need one of {known})")

        srows = con.execute(
            "SELECT dc_id, server_address, port, auth_key FROM sessions"
        ).fetchall()
        if len(srows) != 1:
            return _fail(path, origin, FailureClass.SESSION_CORRUPT,
                         f"expected exactly one sessions row, found {len(srows)}")
        dc_id, server_address, port, auth_key = srows[0]

        auth_len = len(auth_key) if isinstance(auth_key, (bytes, bytearray)) else 0
        if auth_len != AUTH_KEY_LEN:
            return _fail(path, origin, FailureClass.SESSION_CORRUPT,
                         "auth_key missing or wrong length")
        try:
            dc_int = int(dc_id)
        except (TypeError, ValueError):
            dc_int = -1
        if dc_int not in VALID_DC_IDS:
            return _fail(path, origin, FailureClass.SESSION_CORRUPT, "implausible dc_id")
        try:
            port_int = int(port)
        except (TypeError, ValueError):
            port_int = -1
        if not (0 < port_int < 65536):
            return _fail(path, origin, FailureClass.SESSION_CORRUPT, "implausible port")
        addr = str(server_address or "").strip()
        if not addr or len(addr) > 255:
            return _fail(path, origin, FailureClass.SESSION_CORRUPT, "implausible server address")

        return SessionCandidate(
            path=path, origin=origin, valid=True,
            schema_version=version, dc_id=dc_int,
            server_address=addr, port=port_int, auth_key_len=auth_len,
            reason="ok",
        )
    except sqlite3.DatabaseError:
        return _fail(path, origin, FailureClass.SESSION_CORRUPT, "not a valid sqlite database")
    finally:
        if con is not None:
            try:
                con.close()
            except Exception:  # noqa: BLE001
                pass


def _as_uri_path(path: str) -> str:
    """Build the file: URI path component. On Windows an absolute path needs a
    leading slash and forward slashes; spaces/# are percent-encoded minimally."""
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return p.replace("%", "%25").replace("#", "%23").replace("?", "%3f")
