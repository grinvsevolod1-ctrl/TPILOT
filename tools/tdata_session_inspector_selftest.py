# -*- coding: utf-8 -*-
"""Offline selftest for tdata_import.session_inspector.

Builds throwaway Telethon-v7 `.session` SQLite files (valid + malformed
variants) in a temp dir and asserts the static validator's verdicts. No
network, no Telegram, no production data. Also asserts the auth_key bytes never
leak into the returned result.

Run:  python tools\\tdata_session_inspector_selftest.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tdata_import import session_inspector as si  # noqa: E402
from tdata_import.models import FailureClass  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def build_session(path, *, version=7, rows=(("2", "149.154.167.51", 443),),
                  auth_key=b"\xab" * si.AUTH_KEY_LEN, include_version=True,
                  include_sessions=True):
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS version (version integer primary key)")
        if include_sessions:
            con.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "dc_id integer primary key, server_address text, port integer,"
                " auth_key blob, takeout_id integer)"
            )
        con.execute("CREATE TABLE IF NOT EXISTS entities ("
                    "id integer primary key, hash integer not null, username text,"
                    " phone integer, name text, date integer)")
        if include_version and version is not None:
            con.execute("INSERT INTO version VALUES (?)", (version,))
        if include_sessions:
            for dc, addr, port in rows:
                con.execute("INSERT INTO sessions VALUES (?,?,?,?,NULL)",
                            (int(dc), addr, port, auth_key))
        con.commit()
    finally:
        con.close()
    return path


def main():
    d = tempfile.mkdtemp(prefix="tdi_insp_")

    # valid
    p = build_session(os.path.join(d, "ok.session"))
    r = si.inspect_session(p)
    check("valid v7 session accepted", r.valid is True, detail=r.reason)
    check("valid: schema/dc/port/authlen", r.schema_version == 7 and r.dc_id == 2
          and r.port == 443 and r.auth_key_len == si.AUTH_KEY_LEN)
    check("auth_key bytes never leak into result", "abab" not in str(r).lower())

    # non-sqlite
    ng = os.path.join(d, "garbage.session")
    open(ng, "wb").write(b"not a database at all " * 8)
    r = si.inspect_session(ng)
    check("non-sqlite -> session_corrupt", (not r.valid) and r.failure_class == FailureClass.SESSION_CORRUPT,
          detail=r.failure_class)

    # missing version table
    mv = os.path.join(d, "nover.session")
    build_session(mv, include_version=False)
    # remove version table entirely
    con = sqlite3.connect(mv); con.execute("DROP TABLE version"); con.commit(); con.close()
    r = si.inspect_session(mv)
    check("no version table -> schema_unsupported",
          (not r.valid) and r.failure_class == FailureClass.SESSION_SCHEMA_UNSUPPORTED, detail=r.failure_class)

    # wrong schema version
    wv = build_session(os.path.join(d, "v6.session"), version=6)
    r = si.inspect_session(wv)
    check("version != 7 -> schema_unsupported",
          (not r.valid) and r.failure_class == FailureClass.SESSION_SCHEMA_UNSUPPORTED, detail=r.failure_class)

    # zero session rows
    zr = build_session(os.path.join(d, "zero.session"), rows=())
    r = si.inspect_session(zr)
    check("zero sessions rows -> session_corrupt",
          (not r.valid) and r.failure_class == FailureClass.SESSION_CORRUPT, detail=r.failure_class)

    # two session rows
    tr = build_session(os.path.join(d, "two.session"),
                       rows=(("2", "149.154.167.51", 443), ("4", "149.154.167.91", 443)))
    r = si.inspect_session(tr)
    check("two sessions rows -> session_corrupt",
          (not r.valid) and r.failure_class == FailureClass.SESSION_CORRUPT, detail=r.failure_class)

    # bad auth_key length
    ba = build_session(os.path.join(d, "badkey.session"), auth_key=b"\x01" * 10)
    r = si.inspect_session(ba)
    check("short auth_key -> session_corrupt",
          (not r.valid) and r.failure_class == FailureClass.SESSION_CORRUPT, detail=r.failure_class)

    # implausible dc_id
    bd = build_session(os.path.join(d, "baddc.session"), rows=(("99", "1.2.3.4", 443),))
    r = si.inspect_session(bd)
    check("implausible dc_id -> session_corrupt",
          (not r.valid) and r.failure_class == FailureClass.SESSION_CORRUPT, detail=r.failure_class)

    # implausible port
    bp = build_session(os.path.join(d, "badport.session"), rows=(("2", "1.2.3.4", 0),))
    r = si.inspect_session(bp)
    check("implausible port -> session_corrupt",
          (not r.valid) and r.failure_class == FailureClass.SESSION_CORRUPT, detail=r.failure_class)

    # live-session collision
    lp = build_session(os.path.join(d, "live.session"))
    r = si.inspect_session(lp, known_session_paths=[lp])
    check("matches live manager session -> rejected",
          (not r.valid) and r.failure_class == FailureClass.SESSION_CORRUPT, detail=r.failure_class)

    # truncated / corrupt image
    cp = build_session(os.path.join(d, "trunc.session"))
    with open(cp, "r+b") as fh:
        fh.truncate(100)
    r = si.inspect_session(cp)
    check("truncated db -> session_corrupt", (not r.valid) and r.failure_class == FailureClass.SESSION_CORRUPT,
          detail=r.failure_class)

    # inspector does not mutate the candidate (mtime/size unchanged)
    stp = build_session(os.path.join(d, "immut.session"))
    before = os.stat(stp)
    si.inspect_session(stp)
    after = os.stat(stp)
    check("inspection does not mutate candidate",
          before.st_size == after.st_size and before.st_mtime == after.st_mtime)
    check("no -wal/-journal sidecar created by inspection",
          not os.path.exists(stp + "-wal") and not os.path.exists(stp + "-journal"))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL SESSION-INSPECTOR SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
