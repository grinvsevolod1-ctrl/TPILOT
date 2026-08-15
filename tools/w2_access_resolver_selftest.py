# -*- coding: utf-8 -*-
"""tools/w2_access_resolver_selftest.py -- offline selftest for the W2
REVISION Blocker 2 fix: storage.w2_access_decision, the single resolved
access-authority decision manager_bot.py's _poll_loop now consumes
directly (no more union of independent candidate-key lookups).

storage.py is directly importable (zero import-time side effects).

Pure/offline: no network, no Telegram, no production DB, no spend.

    python tools\\w2_access_resolver_selftest.py
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

import storage  # noqa: E402

FAILURES: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="w2_access_resolver_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE access_users(
                tg_user_id INTEGER PRIMARY KEY, scope_mode TEXT NOT NULL DEFAULT 'selected',
                is_enabled INTEGER NOT NULL DEFAULT 1
            );
            CREATE TABLE access_targets(
                tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(tg_user_id, manager_key)
            );
            CREATE TABLE manager_bot_access(
                tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL DEFAULT '',
                can_receive_cards INTEGER NOT NULL DEFAULT 0, revoked INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (tg_user_id, manager_key)
            );
            CREATE TABLE managers(
                manager_key TEXT PRIMARY KEY, status TEXT NOT NULL DEFAULT 'active', is_enabled INTEGER NOT NULL DEFAULT 1
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def _seed(db, *, uid, scope_mode="selected", is_enabled=1, mba=None, at=None, managers=None):
    con = sqlite3.connect(db)
    con.execute("INSERT OR REPLACE INTO access_users(tg_user_id, scope_mode, is_enabled) VALUES (?,?,?)",
                (uid, scope_mode, is_enabled))
    for mk, can_receive, revoked in (mba or []):
        con.execute("INSERT INTO manager_bot_access(tg_user_id, manager_key, can_receive_cards, revoked) VALUES (?,?,?,?)",
                    (uid, mk, can_receive, revoked))
    for mk in (at or []):
        con.execute("INSERT INTO access_targets(tg_user_id, manager_key, created_at) VALUES (?,?,'')", (uid, mk))
    for mk in (managers or []):
        con.execute("INSERT OR IGNORE INTO managers(manager_key, status, is_enabled) VALUES (?,'active',1)", (mk,))
    con.commit()
    con.close()


# ======================================================================
# access_targets allow + manager_bot_access deny (revoked) -> no delivery.
# ======================================================================

def test_access_targets_allow_mba_deny_no_delivery() -> None:
    db = _make_db()
    try:
        _seed(db, uid=100, mba=[("mgr1", 1, 1)], at=["mgr1"], managers=["mgr1"])
        decision = storage.w2_access_decision(100, manager_key="mgr1", db_path=db)
        check("access_targets allows + manager_bot_access revoked -> status='revoked', no delivery",
              decision["status"] == "revoked" and decision["delivery_manager_keys"] == [], decision)
        decision_all = storage.w2_access_decision(100, db_path=db)
        check("(unscoped) revoked key never appears in delivery_manager_keys",
              "mgr1" not in decision_all["delivery_manager_keys"], decision_all)
    finally:
        os.unlink(db)


# ======================================================================
# access_targets allow + manager_bot_access missing entirely -> no delivery.
# ======================================================================

def test_access_targets_allow_mba_missing_no_delivery() -> None:
    db = _make_db()
    try:
        _seed(db, uid=101, at=["mgr2"], managers=["mgr2"])
        decision = storage.w2_access_decision(101, manager_key="mgr2", db_path=db)
        check("access_targets allows + no manager_bot_access row -> status='no_manager_association', no delivery",
              decision["status"] == "no_manager_association" and decision["delivery_manager_keys"] == [], decision)
        check("but the key IS still in ui_manager_keys (legacy UI menu case)",
              "mgr2" in decision["ui_manager_keys"], decision)
    finally:
        os.unlink(db)


# ======================================================================
# manager_bot_access allow + access_targets missing -> deterministic
# approved result (THE D-02 fix).
# ======================================================================

def test_mba_allow_access_targets_missing_deterministic_approved() -> None:
    db = _make_db()
    try:
        _seed(db, uid=102, mba=[("mgr3", 1, 0)], managers=["mgr3"])
        decision = storage.w2_access_decision(102, manager_key="mgr3", db_path=db)
        check("manager_bot_access allows, access_targets missing -> status='allowed', deterministic delivery grant",
              decision["status"] == "allowed" and decision["delivery_manager_keys"] == ["mgr3"], decision)
        decision_unscoped = storage.w2_access_decision(102, db_path=db)
        check("(unscoped) status='allowed', delivery_manager_keys=['mgr3'] -- D-02 backlog class recovered",
              decision_unscoped["status"] == "allowed" and decision_unscoped["delivery_manager_keys"] == ["mgr3"],
              decision_unscoped)
    finally:
        os.unlink(db)


# ======================================================================
# Explicit revoked -> deny (unscoped call too).
# ======================================================================

def test_explicit_revoked_deny() -> None:
    db = _make_db()
    try:
        _seed(db, uid=103, mba=[("mgr4", 1, 1)], at=["mgr4"], managers=["mgr4"])
        decision = storage.w2_access_decision(103, db_path=db)
        check("revoked key excluded from delivery_manager_keys even unscoped",
              decision["delivery_manager_keys"] == [], decision)
    finally:
        os.unlink(db)


# ======================================================================
# Disabled -> deny, REGARDLESS of manager_bot_access state.
# ======================================================================

def test_disabled_deny_regardless_of_mba() -> None:
    db = _make_db()
    try:
        _seed(db, uid=104, is_enabled=0, mba=[("mgr5", 1, 0)], managers=["mgr5"])
        decision = storage.w2_access_decision(104, db_path=db)
        check("disabled access_users -> status='disabled', delivery_manager_keys=[] "
              "even though manager_bot_access itself is active/non-revoked",
              decision["status"] == "disabled" and decision["delivery_manager_keys"] == [], decision)
        check("no raw uid in the decision output", "104" not in str(decision.get("actor_ref")), decision)
    finally:
        os.unlink(db)


# ======================================================================
# Legacy non-manager UI case (B10) remains functional: ui_manager_keys
# includes the key, delivery_manager_keys does not.
# ======================================================================

def test_legacy_non_manager_ui_case_functional_without_delivery() -> None:
    db = _make_db()
    try:
        _seed(db, uid=105, at=["mgr6"], managers=["mgr6"])  # B10: approved via access_targets only
        decision = storage.w2_access_decision(105, db_path=db)
        check("B10 case: ui_manager_keys includes the key (menu stays functional)",
              "mgr6" in decision["ui_manager_keys"], decision)
        check("B10 case: delivery_manager_keys does NOT include it (never silently grants cards)",
              "mgr6" not in decision["delivery_manager_keys"], decision)
        check("B10 case status is 'no_manager_association', not 'missing' (distinguishable: this uid IS known)",
              decision["status"] == "no_manager_association", decision)
    finally:
        os.unlink(db)


def test_scope_all_supervisor_case() -> None:
    """B11: scope_mode='all' -- ui_manager_keys is every manager, no
    access_targets row is required or read for this branch."""
    db = _make_db()
    try:
        _seed(db, uid=106, scope_mode="all", managers=["mgrA", "mgrB"])
        decision = storage.w2_access_decision(106, db_path=db)
        check("scope='all': ui_manager_keys includes every manager", set(decision["ui_manager_keys"]) == {"mgrA", "mgrB"}, decision)
        check("scope='all': delivery_manager_keys is empty (no manager_bot_access rows granted)",
              decision["delivery_manager_keys"] == [], decision)
        check("scope='all': conflict is never flagged for this branch (access_targets not applicable)",
              decision["conflict"] is False, decision)
    finally:
        os.unlink(db)


# ======================================================================
# Conflict is visible but never auto-repaired (zero writes).
# ======================================================================

def test_conflict_visible_not_auto_repaired() -> None:
    db = _make_db()
    try:
        _seed(db, uid=107, mba=[("mgr7", 1, 0)], managers=["mgr7"])  # mba only, no access_targets
        before_at = None
        con = sqlite3.connect(db)
        before_at = con.execute("SELECT COUNT(*) FROM access_targets").fetchone()[0]
        con.close()

        decision = storage.w2_access_decision(107, db_path=db)
        check("conflict flag is True (mba active, access_targets missing, scope='selected')",
              decision["conflict"] is True, decision)

        con = sqlite3.connect(db)
        after_at = con.execute("SELECT COUNT(*) FROM access_targets").fetchone()[0]
        after_mba_revoked = con.execute("SELECT revoked FROM manager_bot_access WHERE tg_user_id=107 AND manager_key='mgr7'").fetchone()[0]
        con.close()
        check("computing the decision performed ZERO writes to access_targets", after_at == before_at, (before_at, after_at))
        check("computing the decision never touched manager_bot_access.revoked", after_mba_revoked == 0, after_mba_revoked)
    finally:
        os.unlink(db)


def test_missing_association_fails_closed() -> None:
    db = _make_db()
    try:
        decision = storage.w2_access_decision(999999, db_path=db)
        check("completely unknown uid -> status='missing', delivery_manager_keys=[]",
              decision["status"] == "missing" and decision["delivery_manager_keys"] == [], decision)
        decision0 = storage.w2_access_decision(0, db_path=db)
        check("uid=0 -> status='missing', never an exception", decision0["status"] == "missing", decision0)
    finally:
        os.unlink(db)


def test_deleted_manager_status() -> None:
    db = _make_db()
    try:
        _seed(db, uid=108, at=["mgr_gone"])  # no managers row for mgr_gone at all
        decision = storage.w2_access_decision(108, manager_key="mgr_gone", db_path=db)
        check("querying a manager_key with no `managers` row at all -> status='deleted_manager'",
              decision["status"] == "deleted_manager", decision)
        check("deleted_manager -> delivery_manager_keys empty", decision["delivery_manager_keys"] == [], decision)
    finally:
        os.unlink(db)


def main() -> int:
    test_access_targets_allow_mba_deny_no_delivery()
    test_access_targets_allow_mba_missing_no_delivery()
    test_mba_allow_access_targets_missing_deterministic_approved()
    test_explicit_revoked_deny()
    test_disabled_deny_regardless_of_mba()
    test_legacy_non_manager_ui_case_functional_without_delivery()
    test_scope_all_supervisor_case()
    test_conflict_visible_not_auto_repaired()
    test_missing_association_fails_closed()
    test_deleted_manager_status()

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
