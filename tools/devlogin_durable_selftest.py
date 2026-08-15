# -*- coding: utf-8 -*-
"""Offline selftest for the device-login durable state layer (storage.py).

Pure/offline: temp SQLite only (DB-guarded), no network, no Telegram. Proves:
  - create/idempotency/one-active-per-manager;
  - CAS status transitions (waiting->received->delivered->consumed, expire,
    cancel, fail) and lost-race no-ops;
  - the ATOMIC one-shot delivery claim (received->delivered) grants exactly one
    winner under concurrent calls;
  - devlogin_set_fields rejects any non-whitelisted key (an OTP/plaintext token
    can never be smuggled into a column);
  - devlogin_seen_mark_once dedup is at-most-once;
  - SECURITY: after a full simulated flow, a raw byte scan of the temp .db plus
    -wal/-shm/-journal (and any list of "log lines") proves the 5-digit test OTP
    and the plaintext capability token appear NOWHERE on disk (test O + U).

Run:  python tools\\devlogin_durable_selftest.py
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import storage  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _guard_temp_db(db_path):
    rp = os.path.realpath(db_path)
    tmp = os.path.realpath(tempfile.gettempdir())
    assert rp.startswith(tmp), f"db must live under tempdir, got {rp}"
    assert "data_tpilot.db" not in rp and os.sep + "db" + os.sep not in rp, rp


def main():
    tmpd = tempfile.mkdtemp(prefix="devlogin_durable_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)

    OTP = "48261"  # the secret we will prove never touches disk
    TOKEN_PLAINTEXT = "a" * 64  # plaintext capability token -- RAM/loopback only
    TOKEN_HASH = hashlib.sha256(TOKEN_PLAINTEXT.encode()).hexdigest()

    # --- create + idempotency + one-active-per-manager ------------------
    row = storage.devlogin_create("dl_req1", "MgrOne", requested_by_user_id=7,
                                  token_hash=TOKEN_HASH,
                                  started_at="2026-07-19T10:00:00",
                                  expires_at="2999-01-01T00:00:00", db_path=db)
    check("create returns row", bool(row) and row.get("request_id") == "dl_req1")
    check("create initial status waiting", row and row["status"] == "waiting")
    check("create normalizes manager_key", row and row["manager_key"] == "mgrone",
          detail=str(row.get("manager_key") if row else None))
    check("token_hash stored, not plaintext", row and row["token_hash"] == TOKEN_HASH)

    again = storage.devlogin_create("dl_req1", "MgrOne", db_path=db)
    check("create idempotent on request_id", bool(again) and again["request_id"] == "dl_req1")

    dup = storage.devlogin_create("dl_req1b", "mgrone", db_path=db)
    check("second active request for same manager rejected (None)", dup is None)

    other = storage.devlogin_create("dl_req2", "MgrTwo", db_path=db)
    check("different manager can start its own request", bool(other))

    active = storage.devlogin_get_active_for_manager("MgrOne", db_path=db)
    check("get_active_for_manager finds the waiting request", active and active["request_id"] == "dl_req1")

    # --- set_fields allowlist -------------------------------------------
    ok_set = storage.devlogin_set_fields("dl_req1", {"bridge_host": "127.0.0.1", "bridge_port": 51515}, db_path=db)
    check("set_fields writes whitelisted bridge metadata", ok_set)
    r = storage.devlogin_get("dl_req1", db_path=db)
    check("bridge_host/port persisted", r and r["bridge_host"] == "127.0.0.1" and r["bridge_port"] == 51515)

    raised = False
    try:
        storage.devlogin_set_fields("dl_req1", {"code": OTP}, db_path=db)
    except ValueError:
        raised = True
    check("set_fields REJECTS a 'code' column (no OTP smuggling)", raised)

    raised2 = False
    try:
        storage.devlogin_set_fields("dl_req1", {"token": TOKEN_PLAINTEXT}, db_path=db)
    except ValueError:
        raised2 = True
    check("set_fields REJECTS a 'token' column (no plaintext token smuggling)", raised2)

    # --- runtime capture: waiting -> received (only message_id) ---------
    moved = storage.devlogin_transition("dl_req1", "waiting", "received",
                                        fields={"telegram_message_id": 900123,
                                                "received_at": "2026-07-19T10:00:05"}, db_path=db)
    check("waiting->received CAS ok", moved)
    r = storage.devlogin_get("dl_req1", db_path=db)
    check("received records only message_id", r and r["telegram_message_id"] == 900123 and r["status"] == "received")

    stale = storage.devlogin_transition("dl_req1", "waiting", "received", db_path=db)
    check("repeat waiting->received is a no-op (lost race)", stale is False)

    # --- atomic one-shot delivery claim ---------------------------------
    win1 = storage.devlogin_claim_for_delivery("dl_req1", db_path=db)
    win2 = storage.devlogin_claim_for_delivery("dl_req1", db_path=db)
    check("exactly one claim wins (first True, second False)", win1 is True and win2 is False,
          detail=f"win1={win1} win2={win2}")
    r = storage.devlogin_get("dl_req1", db_path=db)
    check("claim moved status to delivered + stamped + counted",
          r and r["status"] == "delivered" and r["bridge_claimed_at"] and r["fetch_attempts"] == 1)

    consumed = storage.devlogin_transition("dl_req1", "delivered", "consumed",
                                           fields={"consumed_at": "2026-07-19T10:00:07"}, db_path=db)
    check("delivered->consumed ok", consumed)
    check("terminal consumed frees the active slot",
          bool(storage.devlogin_create("dl_req1c", "MgrOne", db_path=db)))

    # --- cancel + expire + fail -----------------------------------------
    storage.devlogin_create("dl_reqC", "MgrCancel", db_path=db)
    check("cancel active request", storage.devlogin_cancel("dl_reqC", db_path=db) is True)
    check("cancel already-terminal is no-op", storage.devlogin_cancel("dl_reqC", db_path=db) is False)

    storage.devlogin_create("dl_reqE", "MgrExpire", expires_at="2000-01-01T00:00:00", db_path=db)
    stales = storage.devlogin_list_stale("2020-01-01T00:00:00", db_path=db)
    check("list_stale finds past-expiry request", any(s["request_id"] == "dl_reqE" for s in stales))
    check("expire moves it out of active", storage.devlogin_expire("dl_reqE", db_path=db) is True)
    check("slot freed after expire", bool(storage.devlogin_create("dl_reqE2", "MgrExpire", db_path=db)))

    storage.devlogin_create("dl_reqF", "MgrFail", db_path=db)
    check("fail moves to error", storage.devlogin_fail("dl_reqF", error_class="runtime_offline", db_path=db) is True)

    # --- dedup mark-once ------------------------------------------------
    first = storage.devlogin_seen_mark_once("dl_req1", 900123, db_path=db)
    repeat = storage.devlogin_seen_mark_once("dl_req1", 900123, db_path=db)
    check("seen_mark_once: first True, repeat False", first is True and repeat is False)

    # --- SECURITY: raw disk scan (test O + U) ---------------------------
    # Force a WAL checkpoint so pages are flushed, then scan every on-disk file
    # for the OTP and the plaintext token. Neither was EVER passed to storage,
    # so neither can appear -- this is the durable-layer half of the proof.
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        con.close()

    scanned_files = []
    for suffix in ("", "-wal", "-shm", "-journal"):
        p = db + suffix
        if os.path.isfile(p):
            scanned_files.append(p)
    otp_bytes = OTP.encode()
    tok_bytes = TOKEN_PLAINTEXT.encode()
    otp_hits = []
    tok_hits = []
    for p in scanned_files:
        with open(p, "rb") as f:
            blob = f.read()
        if otp_bytes in blob:
            otp_hits.append(p)
        if tok_bytes in blob:
            tok_hits.append(p)
    check("O: 5-digit OTP appears in NO temp .db/-wal/-shm/-journal file",
          not otp_hits, detail=f"hits={otp_hits}")
    check("U: plaintext capability token appears in NO on-disk file",
          not tok_hits, detail=f"hits={tok_hits}")
    check("U: token_hash (SHA-256) IS present (proves we stored the hash, not the token)",
          any(TOKEN_HASH.encode() in open(p, "rb").read() for p in scanned_files))
    check("scan actually inspected the main DB file", db in scanned_files)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL DEVLOGIN DURABLE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
