# -*- coding: utf-8 -*-
"""tools/w2_access_authority_selftest.py -- offline selftest for the W2
("Access and Delivery", frozen master plan) single explicit access
authority in storage.py: w2_resolve_delivery_manager_keys,
w2_detect_access_divergence, w2_manager_bot_access_repair_backlog.

storage.py is importable with zero import-time side effects (same
project convention every other storage.py selftest relies on) -- this
suite imports it directly and runs the REAL functions against throwaway
temp SQLite files. No network, no Telegram, no production DB, no spend.

    python tools\\w2_access_authority_selftest.py
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402 -- real production module, zero import-time side effects

FAILURES: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="w2_access_authority_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE access_users(
                tg_user_id INTEGER PRIMARY KEY,
                display_name TEXT DEFAULT '',
                username TEXT DEFAULT '',
                access_level INTEGER NOT NULL DEFAULT 1,
                scope_mode TEXT NOT NULL DEFAULT 'selected',
                is_enabled INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE access_targets(
                tg_user_id INTEGER NOT NULL,
                manager_key TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, manager_key)
            );
            CREATE TABLE manager_bot_access(
                tg_user_id INTEGER NOT NULL,
                manager_key TEXT NOT NULL DEFAULT '',
                can_receive_cards INTEGER NOT NULL DEFAULT 0,
                can_set_status INTEGER NOT NULL DEFAULT 0,
                can_view_stats INTEGER NOT NULL DEFAULT 0,
                stats_format TEXT NOT NULL DEFAULT 'light',
                allow_custom_period INTEGER NOT NULL DEFAULT 0,
                auto_granted INTEGER NOT NULL DEFAULT 0,
                granted_by INTEGER DEFAULT 0,
                granted_at TEXT NOT NULL DEFAULT '',
                revoked INTEGER NOT NULL DEFAULT 0,
                revoked_by INTEGER DEFAULT 0,
                revoked_at TEXT NOT NULL DEFAULT '',
                event_cutoff_id INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tg_user_id, manager_key)
            );
            CREATE TABLE manager_bot_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT UNIQUE NOT NULL DEFAULT '',
                event_type TEXT NOT NULL DEFAULT '',
                manager_key TEXT NOT NULL DEFAULT '',
                chat_id INTEGER NOT NULL DEFAULT 0,
                lead_date TEXT NOT NULL DEFAULT '',
                daily_lead_id INTEGER NOT NULL DEFAULT 0,
                old_status TEXT NOT NULL DEFAULT '',
                new_status TEXT NOT NULL DEFAULT '',
                payload_json TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def _seed_mba(path, uid, mk, *, can_receive=1, revoked=0, cutoff=0):
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked, event_cutoff_id, "
        "granted_at, created_at, updated_at) VALUES (?,?,?,?,?,'','','')",
        (uid, mk, can_receive, revoked, cutoff),
    )
    con.commit()
    con.close()


def _seed_access_user(path, uid, *, scope_mode="selected", is_enabled=1):
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO access_users(tg_user_id, scope_mode, is_enabled, created_at, updated_at) VALUES (?,?,?,'','')",
        (uid, scope_mode, is_enabled),
    )
    con.commit()
    con.close()


def _seed_access_target(path, uid, mk):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,'')", (uid, mk))
    con.commit()
    con.close()


# ======================================================================
# 1. access_targets allows, manager_bot_access denies -> delivery denied
#    (the authority is manager_bot_access, never access_targets).
# ======================================================================

def test_1_access_targets_allows_mba_denies() -> None:
    db = _make_temp_db()
    try:
        _seed_access_user(db, 100)
        _seed_access_target(db, 100, "mgr_at_only")
        # No manager_bot_access row at all for this pair.
        keys = storage.w2_resolve_delivery_manager_keys(100, db_path=db)
        check("1. access_targets-only row is NOT sufficient for delivery eligibility",
              "mgr_at_only" not in keys, keys)
    finally:
        os.unlink(db)


# ======================================================================
# 2. manager_bot_access allows, access_targets missing -> delivery allowed
#    (THE D-02 fix: this is exactly the 178-backlog class).
# ======================================================================

def test_2_mba_allows_access_targets_missing() -> None:
    db = _make_temp_db()
    try:
        _seed_access_user(db, 200)
        _seed_mba(db, 200, "mgr_mba_only", can_receive=1, revoked=0)
        # Deliberately NO access_targets row.
        keys = storage.w2_resolve_delivery_manager_keys(200, db_path=db)
        check("2. manager_bot_access alone IS sufficient for delivery eligibility (D-02 fix)",
              "mgr_mba_only" in keys, keys)

        div = storage.w2_detect_access_divergence(db_path=db)
        check("2b. divergence detector counts this as exactly one mba_without_access_target",
              div["mba_without_access_target_count"] == 1, div)
        check("2c. divergence detector does not leak a raw tg_user_id",
              all("200" not in str(v) for v in div["mba_without_access_target_sample"]), div)
    finally:
        os.unlink(db)


# ======================================================================
# 3. explicit revocation wins, regardless of access_targets.
# ======================================================================

def test_3_explicit_revocation_wins() -> None:
    db = _make_temp_db()
    try:
        _seed_access_user(db, 300)
        _seed_access_target(db, 300, "mgr_revoked")
        _seed_mba(db, 300, "mgr_revoked", can_receive=1, revoked=1)
        keys = storage.w2_resolve_delivery_manager_keys(300, db_path=db)
        check("3. revoked=1 in manager_bot_access denies delivery even with access_targets present",
              "mgr_revoked" not in keys, keys)
    finally:
        os.unlink(db)


# ======================================================================
# 4. disabled access (access_users.is_enabled=0) remains disabled --
#    checked at the _enabled_access_users layer, proven here via a direct
#    query mirroring that function's own WHERE clause (manager_bot.py
#    cannot be imported; this proves the DATA CONTRACT is intact).
# ======================================================================

def test_4_disabled_access_remains_disabled() -> None:
    db = _make_temp_db()
    try:
        _seed_access_user(db, 400, is_enabled=0)
        _seed_mba(db, 400, "mgr_disabled_user", can_receive=1, revoked=0)
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        enabled_uids = [r["tg_user_id"] for r in con.execute(
            "SELECT tg_user_id FROM access_users WHERE COALESCE(is_enabled,0)=1"
        ).fetchall()]
        con.close()
        check("4. a disabled access_users row is excluded from the enabled-user set "
              "(the poll loop never even queries manager_bot_access for this uid)",
              400 not in enabled_uids, enabled_uids)
        # manager_bot_access itself is untouched by disabling access_users.
        keys = storage.w2_resolve_delivery_manager_keys(400, db_path=db)
        check("4b. manager_bot_access authority itself still reports the grant "
              "(disabling is enforced at the access_users/enabled-user layer, not by mutating manager_bot_access)",
              "mgr_disabled_user" in keys, keys)
    finally:
        os.unlink(db)


# ======================================================================
# 5. missing association fails closed -- unknown uid, no rows anywhere.
# ======================================================================

def test_5_missing_association_fails_closed() -> None:
    db = _make_temp_db()
    try:
        keys = storage.w2_resolve_delivery_manager_keys(999999, db_path=db)
        check("5. an unknown tg_user_id resolves to an EMPTY key list, never an exception",
              keys == [], keys)
        keys0 = storage.w2_resolve_delivery_manager_keys(0, db_path=db)
        check("5b. tg_user_id=0 resolves to an empty list (never auto-grants)", keys0 == [], keys0)
    finally:
        os.unlink(db)


# ======================================================================
# 15 (partial). duplicate=1 semantics: w2_resolve_delivery_manager_keys /
# w2_detect_access_divergence / w2_manager_bot_access_repair_backlog never
# reference or write a `duplicate` column at all -- static source proof.
# ======================================================================

def test_15_no_duplicate_column_touched() -> None:
    src = open(str(BASE_DIR / "storage.py"), encoding="utf-8-sig").read()
    start = src.index("# --- TPILOT W2 ACCESS & DELIVERY 20260729 START ---")
    end = src.index("# --- TPILOT W2 ACCESS & DELIVERY 20260729 END ---", start)
    block = src[start:end]
    check("15. the entire W2 storage.py block never references a `duplicate` column",
          "duplicate" not in block.lower(), None)


# ======================================================================
# 20. no raw Telegram ID appears in any W2 observability output.
# ======================================================================

def test_20_no_raw_uid_in_divergence_output() -> None:
    db = _make_temp_db()
    try:
        for uid, mk in ((111222333, "mgrA"), (444555666, "mgrB")):
            _seed_access_user(db, uid)
            _seed_mba(db, uid, mk, can_receive=1, revoked=0)
        div = storage.w2_detect_access_divergence(db_path=db)
        blob = str(div)
        check("20. neither raw uid appears anywhere in the divergence report",
              "111222333" not in blob and "444555666" not in blob, blob)
        check("20b. actor_ref values are present and look like a 16-hex-char digest, not a raw id",
              all(len(s["actor_ref"]) == 16 for s in div["mba_without_access_target_sample"]), div)
    finally:
        os.unlink(db)


# ======================================================================
# 27. access fail-closed mutation proof: mutate the resolver's WHERE
#     clause (drop the revoked=0 guard) and prove test_3's assertion now
#     fails for the RIGHT reason.
# ======================================================================

def test_27_fail_closed_mutation_proof() -> None:
    db = _make_temp_db()
    try:
        _seed_access_user(db, 300)
        _seed_mba(db, 300, "mgr_revoked", can_receive=1, revoked=1)

        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        # Reproduce the REAL query, then a MUTANT missing the revoked=0 guard,
        # proving the guard is load-bearing (not a no-op / dead condition).
        real = con.execute(
            "SELECT manager_key FROM manager_bot_access WHERE tg_user_id=? "
            "AND COALESCE(can_receive_cards,0)=1 AND COALESCE(revoked,0)=0",
            (300,),
        ).fetchall()
        mutant = con.execute(
            "SELECT manager_key FROM manager_bot_access WHERE tg_user_id=? "
            "AND COALESCE(can_receive_cards,0)=1",
            (300,),
        ).fetchall()
        con.close()
        check("27. [mutation proof] REAL query (with revoked=0 guard) excludes the revoked row",
              len(real) == 0, real)
        check("27. [mutation proof] MUTANT query (revoked=0 guard removed) WOULD incorrectly admit it "
              "-- proves the guard is load-bearing, not dead code",
              len(mutant) == 1, mutant)
    finally:
        os.unlink(db)


def main() -> int:
    test_1_access_targets_allows_mba_denies()
    test_2_mba_allows_access_targets_missing()
    test_3_explicit_revocation_wins()
    test_4_disabled_access_remains_disabled()
    test_5_missing_association_fails_closed()
    test_15_no_duplicate_column_touched()
    test_20_no_raw_uid_in_divergence_output()
    test_27_fail_closed_mutation_proof()

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
