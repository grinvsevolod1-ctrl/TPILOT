# -*- coding: utf-8 -*-
"""tools/hnv2_quota_selftest.py -- offline self-test for the Health
Notification System V2 storage-layer primitives added in storage.py under
the "TPILOT HEALTH NOTIFICATION V2 20260807" block:

  - ensure_health_incidents_v2_columns (additive migration, does not
    disturb the pre-existing health_incidents lifecycle)
  - ensure_health_notify_quota_table
  - health_notify_quota_claim / health_notify_quota_refund (the atomic
    per-(manager_key, signature, kyiv_date) daily-cap CAS)
  - health_incident_v2_get / _observe / _close_silent / _set_state /
    _set_watermark / any_open_v2

storage.py is directly importable (no Telethon/env side effects) -- these
are plain sync `def`s using raw sqlite3, exercised FOR REAL against a temp
SQLite file, same convention as tools/health_incident_selftest.py.

Covers approved-plan selftest scenarios 7 (max 3/day), 8 (4th observation
silent), 11 (restart doesn't reset quota), plus a real two-thread
concurrency proof that the CAS grants exactly one winner.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402

FAILURES: list = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _selftest_db_guard(db_path: str) -> None:
    """Mandatory production-path DB guard, same convention as every other
    tools/*_selftest.py in this project."""
    prod_db_dir = os.path.abspath(os.path.join(str(BASE_DIR), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="hnv2_quota_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    _selftest_db_guard(db_path)
    return tmp_root, db_path


def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


# ======================================================================
# Group 0: table creation is idempotent and does not disturb the
# pre-existing health_incidents lifecycle (health_incident_selftest.py's
# own storage-level checks are the authority on THAT lifecycle -- here we
# only prove the V2 migration coexists safely).
# ======================================================================

def test_group_0_migration_safety():
    print("\n-- Group 0: additive migration safety --")
    tmp_root, db_path = make_temp_env()
    try:
        # Pre-existing legacy incident, created via the ORIGINAL (pre-V2)
        # storage API, exactly as health_incident_selftest.py exercises it.
        # Uses the 'limited:' namespace deliberately -- that is the ONE
        # namespace health_incident_resolve_all_for_manager actually sweeps
        # (its filter is `signature LIKE 'limited:%' OR 'blocked:%'`,
        # storage.py ~3991 -- 'recovery_flap'/'no_health_data'/'hv2:*' are
        # intentionally excluded from that sweep, see 0e below).
        storage.health_incident_upsert_open("mgr_legacy", "limited:peerflood:pf", db_path=db_path)
        before = storage.health_incident_get("mgr_legacy", "limited:peerflood:pf", db_path=db_path)
        check("0a. legacy incident created via pre-V2 API", before is not None and before.get("status") == "open", before)

        # Running the V2 migration must not touch the legacy row's existing
        # columns, and must add the new columns with their DEFAULTs.
        storage.ensure_health_incidents_v2_columns(db_path=db_path)
        after = storage.health_incident_get("mgr_legacy", "limited:peerflood:pf", db_path=db_path)
        check("0b. legacy row status/opened_at unchanged by V2 migration",
              after.get("status") == before.get("status") and after.get("opened_at") == before.get("opened_at"),
              (before, after))
        check("0c. V2 columns present with DEFAULT values on the legacy row",
              after.get("observed_count") == 0 and after.get("family") == "" and after.get("rearm_watermark") == "",
              after)

        # Running it again (e.g. a second controller start) must be a no-op,
        # not raise, not duplicate columns.
        storage.ensure_health_incidents_v2_columns(db_path=db_path)
        storage.ensure_health_incidents_v2_columns(db_path=db_path)
        con = sqlite3.connect(db_path)
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(health_incidents)").fetchall()]
        finally:
            con.close()
        check("0d. re-running the migration is idempotent (no duplicate columns)",
              cols.count("family") == 1 and cols.count("rearm_watermark") == 1, cols)

        # health_incident_resolve_all_for_manager's existing LIKE filter
        # must remain untouched by anything V2 does here.
        n = storage.health_incident_resolve_all_for_manager("mgr_legacy", db_path=db_path)
        check("0e. legacy resolve-all-for-manager still works after V2 migration", n == 1, n)

        # And an hv2:* incident for the SAME manager must NOT be swept by
        # that same call -- this is the load-bearing isolation guarantee
        # storage.py's docstring (3963-3978) promises and the approved plan
        # relies on (K.3): a Telegram-health recovery can never close a V2
        # incident.
        storage.health_incident_upsert_open("mgr_legacy", "hv2:worker_crash:process_exited", db_path=db_path)
        n2 = storage.health_incident_resolve_all_for_manager("mgr_legacy", db_path=db_path)
        check("0f. resolve-all-for-manager does NOT sweep an 'hv2:*' incident", n2 == 0, n2)
        still_open = storage.health_incident_get("mgr_legacy", "hv2:worker_crash:process_exited", db_path=db_path)
        check("0g. the hv2:* incident remains open after the legacy sweep", still_open.get("status") == "open", still_open)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Group 1: health_notify_quota_claim -- basic CAS semantics.
# ======================================================================

def test_group_1_quota_basic():
    print("\n-- Group 1: quota claim basic CAS --")
    tmp_root, db_path = make_temp_env()
    try:
        day = "2026-08-07"
        g1, n1 = storage.health_notify_quota_claim("mgr1", "hv2:worker_crash:process_exited", day, max_per_day=3, db_path=db_path)
        check("1a. first claim of the day is granted", g1 is True and n1 == 1, (g1, n1))
        g2, n2 = storage.health_notify_quota_claim("mgr1", "hv2:worker_crash:process_exited", day, max_per_day=3, db_path=db_path)
        check("1b. second claim is granted, count advances to 2", g2 is True and n2 == 2, (g2, n2))
        row = storage.health_notify_quota_claim  # noop, keep name in scope
        con = sqlite3.connect(db_path)
        try:
            r = con.execute(
                "SELECT first_sent_at, last_sent_at FROM health_notify_quota WHERE manager_key=? AND signature=? AND kyiv_date=?",
                ("mgr1", "hv2:worker_crash:process_exited", day),
            ).fetchone()
        finally:
            con.close()
        check("1c. first_sent_at set once and never overwritten by later claims", bool(r[0]) and r[0] <= r[1], r)

        # Bad-arg guards never grant.
        g_bad, n_bad = storage.health_notify_quota_claim("", "sig", day, db_path=db_path)
        check("1d. empty manager_key never grants", g_bad is False and n_bad == -1, (g_bad, n_bad))
        g_bad2, n_bad2 = storage.health_notify_quota_claim("mgr1", "sig", day, max_per_day=0, db_path=db_path)
        check("1e. max_per_day<=0 never grants", g_bad2 is False and n_bad2 == -1, (g_bad2, n_bad2))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario 7 + 8: max 3 problem notifications per Kyiv calendar day,
# 4th observation silent, quota state byte-identical after denial.
# ======================================================================

def test_group_2_scenario_7_8_daily_cap():
    print("\n-- Group 2: scenario 7/8 -- max 3/day, 4th silent --")
    tmp_root, db_path = make_temp_env()
    try:
        day = "2026-08-07"
        mk, sig = "mgr7", "hv2:session_unauthorized:authkeyunregistered"
        results = [storage.health_notify_quota_claim(mk, sig, day, max_per_day=3, db_path=db_path) for _ in range(3)]
        check("7a. first three claims of the day are all granted",
              all(g for g, _ in results) and [n for _, n in results] == [1, 2, 3], results)

        con = sqlite3.connect(db_path)
        try:
            before = dict(zip(
                ("sent_count", "first_sent_at", "last_sent_at"),
                con.execute(
                    "SELECT sent_count, first_sent_at, last_sent_at FROM health_notify_quota WHERE manager_key=? AND signature=? AND kyiv_date=?",
                    (mk, sig, day),
                ).fetchone(),
            ))
        finally:
            con.close()

        g4, n4 = storage.health_notify_quota_claim(mk, sig, day, max_per_day=3, db_path=db_path)
        check("8a. fourth observation is silently denied", g4 is False, (g4, n4))
        check("8b. denial reports the current sent_count (3), not an incremented one", n4 == 3, n4)

        con = sqlite3.connect(db_path)
        try:
            after = dict(zip(
                ("sent_count", "first_sent_at", "last_sent_at"),
                con.execute(
                    "SELECT sent_count, first_sent_at, last_sent_at FROM health_notify_quota WHERE manager_key=? AND signature=? AND kyiv_date=?",
                    (mk, sig, day),
                ).fetchone(),
            ))
        finally:
            con.close()
        check("8c. quota row is byte-identical after a denied claim (no mutation on denial)", before == after, (before, after))

        # A 5th, 6th... attempt stays denied and never grows sent_count.
        for _ in range(3):
            g, n = storage.health_notify_quota_claim(mk, sig, day, max_per_day=3, db_path=db_path)
            check(f"8d. repeated denial stays denied at sent_count=3", g is False and n == 3, (g, n))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario: recovery notifications never call the quota claim (this is
# enforced by main.py's aggregator, not storage.py -- here we just prove
# the refund primitive, which is the OTHER half of the quota contract).
# ======================================================================

def test_group_3_refund():
    print("\n-- Group 3: quota refund on lost incident-claim race --")
    tmp_root, db_path = make_temp_env()
    try:
        day = "2026-08-07"
        mk, sig = "mgr_refund", "hv2:proxy_unavailable:unreachable"
        g1, n1 = storage.health_notify_quota_claim(mk, sig, day, max_per_day=3, db_path=db_path)
        check("3a. claim granted before refund", g1 is True and n1 == 1, (g1, n1))
        refunded = storage.health_notify_quota_refund(mk, sig, day, db_path=db_path)
        check("3b. refund succeeds and reports True", refunded is True)
        con = sqlite3.connect(db_path)
        try:
            row = con.execute(
                "SELECT sent_count FROM health_notify_quota WHERE manager_key=? AND signature=? AND kyiv_date=?",
                (mk, sig, day),
            ).fetchone()
        finally:
            con.close()
        check("3c. sent_count back to 0 after refund", row[0] == 0, row)

        # Refund never drops below zero.
        refunded2 = storage.health_notify_quota_refund(mk, sig, day, db_path=db_path)
        check("3d. refunding an already-zero counter returns False (never negative)", refunded2 is False)
        con = sqlite3.connect(db_path)
        try:
            row2 = con.execute(
                "SELECT sent_count FROM health_notify_quota WHERE manager_key=? AND signature=? AND kyiv_date=?",
                (mk, sig, day),
            ).fetchone()
        finally:
            con.close()
        check("3e. sent_count never goes negative", row2[0] == 0, row2)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario 10: distinct root causes have independent budgets -- same
# manager, two different signatures, one exhausted does not block the
# other.
# ======================================================================

def test_group_4_scenario_10_independent_causes():
    print("\n-- Group 4: scenario 10 -- distinct causes independently alertable --")
    tmp_root, db_path = make_temp_env()
    try:
        day = "2026-08-07"
        mk = "mgr10"
        sig_a = "hv2:proxy_unavailable:unreachable"
        sig_b = "hv2:session_unauthorized:authkeyunregistered"
        for _ in range(3):
            storage.health_notify_quota_claim(mk, sig_a, day, max_per_day=3, db_path=db_path)
        g_a4, n_a4 = storage.health_notify_quota_claim(mk, sig_a, day, max_per_day=3, db_path=db_path)
        check("10a. sig_a is exhausted at 3/3", g_a4 is False and n_a4 == 3, (g_a4, n_a4))

        g_b1, n_b1 = storage.health_notify_quota_claim(mk, sig_b, day, max_per_day=3, db_path=db_path)
        check("10b. sig_b (different root cause) is independently alertable despite sig_a's exhaustion",
              g_b1 is True and n_b1 == 1, (g_b1, n_b1))

        con = sqlite3.connect(db_path)
        try:
            n_rows = con.execute("SELECT COUNT(*) FROM health_notify_quota WHERE manager_key=?", (mk,)).fetchone()[0]
        finally:
            con.close()
        check("10c. exactly two independent quota rows exist for this manager (no global 3-alert cap)", n_rows == 2, n_rows)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario 11: restart doesn't reset quota. storage.py functions hold NO
# in-process cache -- every call opens a fresh sqlite3 connection and
# closes it. The durable state is entirely on disk. We prove this by
# dropping every Python-level reference/module state that could
# theoretically cache something and re-verifying purely from a brand new
# connection, plus by deleting and re-importing the storage module itself
# (the strongest available proxy for "the whole process restarted" without
# actually spawning a new interpreter).
# ======================================================================

def test_group_5_scenario_11_restart_persistence():
    print("\n-- Group 5: scenario 11 -- controller restart does not reset quota --")
    tmp_root, db_path = make_temp_env()
    try:
        day = "2026-08-07"
        mk, sig = "mgr11", "hv2:worker_crash:unknown"
        for _ in range(3):
            g, n = storage.health_notify_quota_claim(mk, sig, day, max_per_day=3, db_path=db_path)
            check(f"11-setup. claim {n} granted", g is True)

        # Simulate a controller restart: remove the storage module from
        # sys.modules and re-import it fresh, so any accidental module-level
        # cache (there is none by design, but this proves it) cannot survive.
        del sys.modules["storage"]
        import importlib
        fresh_storage = importlib.import_module("storage")
        check("11a. storage module re-imported fresh (simulated restart)", fresh_storage is not storage)

        g4, n4 = fresh_storage.health_notify_quota_claim(mk, sig, day, max_per_day=3, db_path=db_path)
        check("11b. the 4th claim is STILL denied after simulated restart (quota persisted on disk)", g4 is False and n4 == 3, (g4, n4))

        globals()["storage"] = fresh_storage
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Concurrency proof: two threads racing for the last slot -- exactly one
# must win, final count must be exactly the cap, never more.
# ======================================================================

def test_group_6_concurrency():
    print("\n-- Group 6: concurrent claim -- exactly one winner for the last slot --")
    tmp_root, db_path = make_temp_env()
    try:
        day = "2026-08-07"
        mk, sig = "mgr_race", "hv2:network_timeout:timeout"
        # Pre-seed at 2/3 so exactly one slot remains.
        storage.health_notify_quota_claim(mk, sig, day, max_per_day=3, db_path=db_path)
        storage.health_notify_quota_claim(mk, sig, day, max_per_day=3, db_path=db_path)

        results: list = []
        lock = threading.Lock()

        def _worker():
            g, n = storage.health_notify_quota_claim(mk, sig, day, max_per_day=3, db_path=db_path)
            with lock:
                results.append((g, n))

        threads = [threading.Thread(target=_worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        grants = [g for g, _ in results if g]
        check("6a. exactly one of 8 concurrent callers wins the last slot", len(grants) == 1, results)

        con = sqlite3.connect(db_path)
        try:
            final = con.execute(
                "SELECT sent_count FROM health_notify_quota WHERE manager_key=? AND signature=? AND kyiv_date=?",
                (mk, sig, day),
            ).fetchone()[0]
        finally:
            con.close()
        check("6b. final sent_count is exactly the cap (3), never exceeded", final == 3, final)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Group 7: health_incident_v2_* helper functions -- observe / close_silent
# / set_state / set_watermark / any_open_v2. Pure storage-layer sanity;
# the full state-machine orchestration is exercised by
# hnv2_lifecycle_selftest.py (Phase 6) against main.py's aggregator.
# ======================================================================

def test_group_7_v2_helpers():
    print("\n-- Group 7: health_incident_v2_* helper primitives --")
    tmp_root, db_path = make_temp_env()
    try:
        mk, sig = "mgr_v2", "hv2:worker_crash:process_exited"

        row = storage.health_incident_v2_observe(mk, sig, family="worker_crash", detail="d1", confirm_sec=180, db_path=db_path)
        check("7a. first observation creates status='observed', observed_count=1", row.get("status") == "observed" and row.get("observed_count") == 1, row)
        first_due = row.get("confirm_due_at")

        row2 = storage.health_incident_v2_observe(mk, sig, family="worker_crash", detail="d2", confirm_sec=180, db_path=db_path)
        check("7b. repeated same-family observation bumps observed_count without resetting confirm_due_at",
              row2.get("observed_count") == 2 and row2.get("confirm_due_at") == first_due, (row, row2))
        check("7c. detail is refreshed on repeat observation", row2.get("detail") == "d2", row2)

        # A different family at the SAME signature is not realistic (family
        # is embedded in the signature by construction in main.py), but the
        # storage primitive itself must still treat a family change as a
        # fresh observation defensively.
        row3 = storage.health_incident_v2_observe(mk, sig, family="network_timeout", detail="d3", confirm_sec=60, db_path=db_path)
        check("7d. family change resets observed_count to 1 (fresh observation)", row3.get("observed_count") == 1 and row3.get("family") == "network_timeout", row3)

        closed = storage.health_incident_v2_close_silent(mk, sig, db_path=db_path)
        check("7e. close_silent succeeds on an 'observed' row", closed is True)
        after_close = storage.health_incident_v2_get(mk, sig, db_path=db_path)
        check("7f. closed row is now status='resolved'", after_close.get("status") == "resolved", after_close)

        # close_silent must never touch an OPEN incident (that must go
        # through the normal resolve path so a recovery notification fires).
        storage.health_incident_upsert_open(mk, "hv2:sqlite_lock:locked", db_path=db_path)
        closed_open = storage.health_incident_v2_close_silent(mk, "hv2:sqlite_lock:locked", db_path=db_path)
        check("7g. close_silent is a no-op (False) against an OPEN incident", closed_open is False)
        still_open = storage.health_incident_v2_get(mk, "hv2:sqlite_lock:locked", db_path=db_path)
        check("7h. the OPEN incident is untouched by close_silent", still_open.get("status") == "open", still_open)

    finally:
        cleanup_env(tmp_root)


def test_group_7b_set_state_and_watermark():
    print("\n-- Group 7b: set_state + set_watermark + any_open_v2 --")
    tmp_root, db_path = make_temp_env()
    try:
        mk, sig = "mgr_v2b", "hv2:sqlite_lock:locked"
        wm_mk = "mgr_v2b_wm"  # separate manager: keeps the watermark/any_open_v2
        # isolation checks below from cross-contaminating with `sig`, which
        # this test deliberately leaves open through 7m/7n/7o.
        storage.health_incident_upsert_open(mk, sig, db_path=db_path)

        ok = storage.health_incident_v2_set_state(mk, sig, status="recovery_pending", db_path=db_path, recovery_pending_since="2026-08-07T10:00:00")
        check("7j. set_state transitions OPEN -> recovery_pending", ok is True)
        row = storage.health_incident_v2_get(mk, sig, db_path=db_path)
        check("7k. recovery_pending_since persisted", row.get("status") == "recovery_pending" and row.get("recovery_pending_since") == "2026-08-07T10:00:00", row)

        ok2 = storage.health_incident_v2_set_state(mk, sig, status="open", db_path=db_path, recovery_pending_since="")
        check("7l. set_state transitions back recovery_pending -> open (re-flap) without creating a new row", ok2 is True)
        con = sqlite3.connect(db_path)
        try:
            n = con.execute("SELECT COUNT(*) FROM health_incidents WHERE manager_key=? AND signature=?", (mk, sig)).fetchone()[0]
        finally:
            con.close()
        check("7m. exactly one row exists for this (manager_key, signature) throughout", n == 1, n)

        # set_state never creates a row for a signature that doesn't exist.
        ghost = storage.health_incident_v2_set_state(mk, "hv2:ghost:ghost", status="open", db_path=db_path)
        check("7n. set_state on a non-existent row returns False, creates nothing", ghost is False)
        ghost_row = storage.health_incident_v2_get(mk, "hv2:ghost:ghost", db_path=db_path)
        check("7o. no ghost row was created", ghost_row is None, ghost_row)

        # set_watermark: bootstrap creates a resolved row if none exists.
        # Uses wm_mk (a distinct manager_key) from here on, deliberately --
        # `mk` still has an open `sig` incident from 7j-7o above, which
        # would make any_open_v2 checks below pass for the wrong reason.
        wm_sig = "hv2:recovery_flap:flap"
        storage.health_incident_v2_set_watermark(wm_mk, wm_sig, '{"ts": "2026-08-07T00:00:00", "n": 0}', db_path=db_path)
        wm_row = storage.health_incident_v2_get(wm_mk, wm_sig, db_path=db_path)
        check("7p. set_watermark bootstraps a resolved row when none existed", wm_row is not None and wm_row.get("status") == "resolved", wm_row)
        check("7q. watermark value persisted", wm_row.get("rearm_watermark") == '{"ts": "2026-08-07T00:00:00", "n": 0}', wm_row)

        # set_watermark on an existing row only updates the watermark field.
        storage.health_incident_upsert_open(wm_mk, wm_sig, db_path=db_path)
        storage.health_incident_v2_set_watermark(wm_mk, wm_sig, '{"ts": "2026-08-07T12:00:00", "n": 2}', db_path=db_path)
        wm_row2 = storage.health_incident_v2_get(wm_mk, wm_sig, db_path=db_path)
        check("7r. set_watermark on an OPEN row updates watermark without touching status", wm_row2.get("status") == "open" and "12:00:00" in wm_row2.get("rearm_watermark", ""), wm_row2)

        # any_open_v2
        check("7s. any_open_v2 True while an hv2:* incident is open", storage.health_incident_any_open_v2(wm_mk, db_path=db_path) is True)
        storage.health_incident_resolve(wm_mk, wm_sig, db_path=db_path)
        check("7t. any_open_v2 False once resolved", storage.health_incident_any_open_v2(wm_mk, db_path=db_path) is False)

        # any_open_v2 isolation: a non-hv2 (legacy) open incident must not
        # count.
        storage.health_incident_upsert_open(wm_mk, "recovery_flap", db_path=db_path)
        check("7u. any_open_v2 ignores non-'hv2:' signatures (legacy recovery_flap does not count)", storage.health_incident_any_open_v2(wm_mk, db_path=db_path) is False)

        # And confirm manager isolation the other direction: `mk`'s still-
        # open `sig` incident must not leak into wm_mk's view either.
        check("7v. any_open_v2 is per-manager -- mk's open incident does not affect wm_mk", storage.health_incident_any_open_v2(wm_mk, db_path=db_path) is False)
    finally:
        cleanup_env(tmp_root)


def main() -> int:
    test_group_0_migration_safety()
    test_group_1_quota_basic()
    test_group_2_scenario_7_8_daily_cap()
    test_group_3_refund()
    test_group_4_scenario_10_independent_causes()
    test_group_5_scenario_11_restart_persistence()
    test_group_6_concurrency()
    test_group_7_v2_helpers()
    test_group_7b_set_state_and_watermark()

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
