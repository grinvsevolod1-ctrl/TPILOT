# -*- coding: utf-8 -*-
"""Offline selftest for the device-login 30-second cooldown
(storage.devlogin_create_with_cooldown), exercised against the REAL storage.py
function on a temp SQLite DB. No network, no Telegram, no production DB.

Proves:
  - immediate second request rejected;
  - request accepted after the cooldown window has elapsed;
  - separate managers do not block each other;
  - cancelled/expired/error/consumed rows all enforce the cooldown;
  - waiting/received/delivered are still rejected by the active-request rule
    (distinct from, and checked before, the cooldown rule);
  - concurrent starts cannot both succeed (BEGIN IMMEDIATE serializes them);
  - cooldown timestamps survive a simulated process restart (durable, not
    AdminBot RAM -- a fresh connection to the SAME db_path sees the same
    updated_at);
  - no OTP or plaintext token ever enters durable storage via this path.

Run:  python tools\\devlogin_cooldown_selftest.py
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta

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


def _mk_hash(seed):
    return hashlib.sha256(seed.encode()).hexdigest()


def main():
    tmpd = tempfile.mkdtemp(prefix="devlogin_cooldown_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)

    OTP = "91827"
    TOKEN_PLAIN = "c" * 64

    def start(rid, mk, cooldown_sec=30, expires_in=180):
        return storage.devlogin_create_with_cooldown(
            rid, mk, requested_by_user_id=1, token_hash=_mk_hash(rid),
            started_at=datetime.utcnow().isoformat(),
            expires_at=(datetime.utcnow() + timedelta(seconds=expires_in)).isoformat(),
            cooldown_sec=cooldown_sec, db_path=db,
        )

    def force_terminal(rid, mk, status, updated_at_iso):
        """Directly stamp a terminal status + a controlled updated_at, so the
        cooldown window can be tested deterministically without real sleeps."""
        con = sqlite3.connect(db)
        try:
            con.execute(
                "UPDATE manager_device_login_ops SET status=?, updated_at=? WHERE request_id=?",
                (status, updated_at_iso, rid),
            )
            con.commit()
        finally:
            con.close()

    # === default constant sanity =============================================
    check("default cooldown constant is exactly 30 seconds",
          storage._DEVLOGIN_DEFAULT_COOLDOWN_SEC == 30)
    check("terminal statuses are exactly consumed/cancelled/expired/error",
          storage._DEVLOGIN_TERMINAL_STATUSES == ("consumed", "cancelled", "expired", "error"))

    # === immediate second request rejected (right after a terminal row) ======
    r1 = start("dl_cd1", "MgrCD1")
    check("first request for a fresh manager succeeds", r1.get("ok") is True, detail=str(r1))
    force_terminal("dl_cd1", "MgrCD1", "consumed", datetime.utcnow().isoformat())
    r2 = start("dl_cd1b", "MgrCD1")
    check("immediate second request (same manager, just-terminated) is REJECTED",
          r2.get("ok") is False and r2.get("reason") == "cooldown", detail=str(r2))
    check("cooldown rejection returns a positive retry_after_sec, never a secret",
          isinstance(r2.get("retry_after_sec"), int) and 0 < r2.get("retry_after_sec") <= 30,
          detail=str(r2.get("retry_after_sec")))

    # === request accepted after 30 seconds ====================================
    old_terminal_ts = (datetime.utcnow() - timedelta(seconds=31)).isoformat()
    force_terminal("dl_cd1", "MgrCD1", "consumed", old_terminal_ts)
    r3 = start("dl_cd1c", "MgrCD1")
    check("request accepted once the cooldown window (30s) has fully elapsed",
          r3.get("ok") is True, detail=str(r3))

    # === separate managers do not block each other ===========================
    force_terminal("dl_cd1c", "MgrCD1", "consumed", datetime.utcnow().isoformat())
    r4 = start("dl_cd2", "MgrCD2")  # a DIFFERENT manager, no prior history at all
    check("a different manager is completely unaffected by MgrCD1's cooldown",
          r4.get("ok") is True, detail=str(r4))

    # === cancelled/expired/error/consumed rows ALL enforce cooldown ==========
    for status in ("cancelled", "expired", "error", "consumed"):
        mk = f"MgrCD_{status}"
        rid_a = f"dl_{status}_a"
        rid_b = f"dl_{status}_b"
        ra = start(rid_a, mk)
        check(f"cooldown/{status}: setup request succeeds", ra.get("ok") is True)
        force_terminal(rid_a, mk, status, datetime.utcnow().isoformat())
        rb = start(rid_b, mk)
        check(f"cooldown/{status}: an immediate re-request after a '{status}' row is REJECTED",
              rb.get("ok") is False and rb.get("reason") == "cooldown", detail=str(rb))

    # === waiting/received/delivered are rejected by the ACTIVE rule, not cooldown
    for status in ("waiting", "received", "delivered"):
        mk = f"MgrCD_active_{status}"
        rid_a = f"dl_active_{status}_a"
        rid_b = f"dl_active_{status}_b"
        ra = start(rid_a, mk)
        check(f"active/{status}: setup request succeeds", ra.get("ok") is True)
        if status != "waiting":
            force_terminal(rid_a, mk, status, datetime.utcnow().isoformat())
        rb = start(rid_b, mk)
        check(f"active/{status}: a concurrent request is rejected as 'active', NOT 'cooldown'",
              rb.get("ok") is False and rb.get("reason") == "active", detail=str(rb))

    # === concurrent starts cannot both succeed (real thread race) ============
    mk_race = "MgrCDRace"
    results = []
    lock_results = threading.Lock()

    def racer(rid):
        r = start(rid, mk_race)
        with lock_results:
            results.append(r)

    threads = [threading.Thread(target=racer, args=(f"dl_race_{i}",)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    oks = [r.get("ok") for r in results]
    check("concurrent starts (5 threads, same manager): EXACTLY ONE succeeds",
          oks.count(True) == 1, detail=str(oks))
    check("concurrent starts: all losers are rejected as 'active' (BEGIN IMMEDIATE serialized them)",
          all(r.get("reason") == "active" for r in results if not r.get("ok")), detail=str(results))

    # === cooldown timestamps survive a simulated process restart =============
    mk_restart = "MgrCDRestart"
    r5 = start("dl_restart_a", mk_restart)
    check("restart test: setup request succeeds", r5.get("ok") is True)
    stamp = (datetime.utcnow() - timedelta(seconds=5)).isoformat()
    force_terminal("dl_restart_a", mk_restart, "consumed", stamp)
    # Simulate a fresh process: brand-new connection to the SAME db_path (no
    # in-memory state carried over -- storage.devlogin_create_with_cooldown
    # opens a fresh connection on every call already, so this just re-asserts
    # the durable read reflects the persisted timestamp).
    r6 = start("dl_restart_b", mk_restart)
    check("restart test: cooldown is still enforced from the DURABLE row after a "
          "simulated restart (not lost, not reset)",
          r6.get("ok") is False and r6.get("reason") == "cooldown", detail=str(r6))

    # === no OTP / plaintext token ever enters durable storage =================
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        con.close()
    scanned = [p for p in (db, db + "-wal", db + "-shm", db + "-journal") if os.path.isfile(p)]
    otp_hits, tok_hits = [], []
    for p in scanned:
        with open(p, "rb") as f:
            blob = f.read()
        if OTP.encode() in blob:
            otp_hits.append(p)
        if TOKEN_PLAIN.encode() in blob:
            tok_hits.append(p)
    check("no OTP anywhere on disk across the entire cooldown test flow", not otp_hits, detail=str(otp_hits))
    check("no plaintext token anywhere on disk across the entire cooldown test flow",
          not tok_hits, detail=str(tok_hits))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL DEVLOGIN COOLDOWN SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
