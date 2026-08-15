# -*- coding: utf-8 -*-
"""tools/hnv2_notify_delivery_selftest.py -- offline self-test for the
Health Notification System V2 D9 fix: panel_notifications delivery
durability. Covers approved-plan selftest scenario 17: a row stranded in
'running' longer than max_age_sec is reaped back to 'new', and
_finish_panel_notification on a row already reaped to 'new' is a no-op
(CAS on status='running' prevents a zombie delivery attempt from
overwriting a fresh pickup's own eventual outcome).

panel_bot.py cannot be imported directly (Telethon/env side effects at
import time) -- _finish_panel_notification and
_reap_stale_running_panel_notifications are extracted for REAL via
ast.parse + ast.unparse + exec(), the established technique, and run
against a real temp SQLite file (never the production DB).
"""
from __future__ import annotations

import ast
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()
PANEL_TREE = ast.parse(PANEL_SRC)

REAL_NAMES = {
    "_finish_panel_notification", "_reap_stale_running_panel_notifications",
    "_connect_panel_db", "_ensure_panel_runtime_tables", "_utc_now_iso",
    "_take_panel_notifications",
}


def _extract_by_names(src: str, names: set) -> list:
    tree = ast.parse(src)
    nodes = []
    seen = set()
    for n in tree.body:
        nm = getattr(n, "name", None)
        if nm and nm in names:
            nodes.append(n)
            seen.add(nm)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names:
            nodes.append(n)
            seen.add(n.targets[0].id)
            continue
    missing = names - seen
    if missing:
        raise AssertionError(f"expected {names}, missing {missing}")
    return nodes


def _selftest_db_guard(db_path: str) -> None:
    prod_db_dir = os.path.abspath(os.path.join(str(BASE_DIR), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"


def build_ns(db_path: str) -> dict:
    _selftest_db_guard(db_path)
    nodes = _extract_by_names(PANEL_SRC, REAL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "os": os, "sqlite3": sqlite3,
        "datetime": datetime, "timedelta": timedelta,
        "TPILOT_DB_PATH": db_path,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:hnv2_notify_delivery>", "exec"), ns)
    return ns


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="hnv2_notify_delivery_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


def _insert_notification(db_path: str, *, status: str, updated_at: str, kind: str = "health_agg") -> int:
    con = sqlite3.connect(db_path)
    try:
        cur = con.execute(
            "INSERT INTO panel_notifications(kind,title,body,status,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (kind, "t", "b", status, updated_at, updated_at),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def _row(db_path: str, notification_id: int) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        r = con.execute("SELECT * FROM panel_notifications WHERE id=?", (notification_id,)).fetchone()
        return dict(r) if r else {}
    finally:
        con.close()


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


NOW = datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None)


# ======================================================================
# Scenario 17a: a row stranded in 'running' longer than max_age_sec is
# reaped back to 'new'.
# ======================================================================

def test_reap_stale_running():
    print("\n-- Reaper: stale 'running' row returns to 'new' --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(db_path)
        ns["_ensure_panel_runtime_tables"]()

        stale_id = _insert_notification(db_path, status="running", updated_at=_iso(NOW - timedelta(seconds=700)))
        fresh_id = _insert_notification(db_path, status="running", updated_at=_iso(NOW - timedelta(seconds=30)))
        new_id = _insert_notification(db_path, status="new", updated_at=_iso(NOW))
        done_id = _insert_notification(db_path, status="done", updated_at=_iso(NOW - timedelta(seconds=700)))

        n = ns["_reap_stale_running_panel_notifications"](max_age_sec=600)
        check("17a-1. reaper reports exactly one row reaped", n == 1, n)

        check("17a-2. the stale (700s) running row is returned to 'new'", _row(db_path, stale_id).get("status") == "new", _row(db_path, stale_id))
        check("17a-3. the fresh (30s) running row is left untouched (still 'running')", _row(db_path, fresh_id).get("status") == "running", _row(db_path, fresh_id))
        check("17a-4. an already-'new' row is untouched", _row(db_path, new_id).get("status") == "new", _row(db_path, new_id))
        check("17a-5. an already-'done' row is untouched", _row(db_path, done_id).get("status") == "done", _row(db_path, done_id))

        # Idempotent: running it again on the same (now-clean) state finds nothing.
        n2 = ns["_reap_stale_running_panel_notifications"](max_age_sec=600)
        check("17a-6. a second reap pass finds nothing left to reap", n2 == 0, n2)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario 17b: _finish_panel_notification on a row already reaped to
# 'new' is a no-op (CAS on status='running').
# ======================================================================

def test_finish_after_reap_is_noop():
    print("\n-- _finish_panel_notification after reap: no-op (CAS on status='running') --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(db_path)
        ns["_ensure_panel_runtime_tables"]()

        # Simulate: PanelBot claims a row (status='running'), then "crashes"
        # before finishing it -- the row goes stale.
        nid = _insert_notification(db_path, status="running", updated_at=_iso(NOW - timedelta(seconds=700)))

        # The reaper runs (as it does at the top of every loop iteration)
        # and returns it to 'new' BEFORE the original (zombie) delivery
        # attempt gets around to calling _finish_panel_notification.
        reaped = ns["_reap_stale_running_panel_notifications"](max_age_sec=600)
        check("17b-1. the row was reaped to 'new'", reaped == 1 and _row(db_path, nid).get("status") == "new", _row(db_path, nid))

        before = _row(db_path, nid)
        # The ORIGINAL (zombie) call site now finally gets around to
        # calling _finish_panel_notification('done') -- this MUST be a
        # no-op: the row is 'new' again (or will be picked up fresh by
        # _take_panel_notifications), not 'running', so the CAS in
        # _finish_panel_notification must not match it.
        ns["_finish_panel_notification"](nid, ok=True)
        after = _row(db_path, nid)
        check("17b-2. _finish_panel_notification on a reaped ('new') row is a no-op -- status unchanged", after.get("status") == before.get("status") == "new", (before, after))
        check("17b-3. updated_at is unchanged by the no-op call", after.get("updated_at") == before.get("updated_at"), (before, after))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Scenario: normal delivery (the common case) still works exactly as
# before -- _finish_panel_notification on a genuinely 'running' row (the
# expected state right after _take_panel_notifications claimed it)
# transitions it to 'done'/'error' as usual.
# ======================================================================

def test_normal_finish_still_works():
    print("\n-- Normal delivery path: _finish_panel_notification on a genuinely 'running' row still works --")
    tmp_root, db_path = make_temp_env()
    try:
        ns = build_ns(db_path)
        ns["_ensure_panel_runtime_tables"]()

        nid_ok = _insert_notification(db_path, status="running", updated_at=_iso(NOW))
        nid_err = _insert_notification(db_path, status="running", updated_at=_iso(NOW))

        ns["_finish_panel_notification"](nid_ok, ok=True)
        ns["_finish_panel_notification"](nid_err, ok=False)

        check("normal-1. ok=True transitions running -> done", _row(db_path, nid_ok).get("status") == "done", _row(db_path, nid_ok))
        check("normal-2. ok=False transitions running -> error", _row(db_path, nid_err).get("status") == "error", _row(db_path, nid_err))

        # Calling finish AGAIN on an already-'done' row must also be a
        # no-op (CAS on status='running' -- 'done' no longer matches).
        before = _row(db_path, nid_ok)
        ns["_finish_panel_notification"](nid_ok, ok=False)  # attempt to flip it to 'error'
        after = _row(db_path, nid_ok)
        check("normal-3. calling finish twice never flips an already-finished row (CAS prevents double-finish)", after.get("status") == before.get("status") == "done", (before, after))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# Static: the reaper is wired into _panel_notification_loop, and
# _finish_panel_notification's UPDATE carries the CAS.
# ======================================================================

def test_static_wiring():
    print("\n-- Static: reaper wiring + CAS presence --")
    defs = [n for n in PANEL_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_reap_stale_running_panel_notifications"]
    check("static. _reap_stale_running_panel_notifications defined exactly once", len(defs) == 1, len(defs))

    finish_defs = [n for n in PANEL_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_finish_panel_notification"]
    check("static. _finish_panel_notification defined exactly once", len(finish_defs) == 1, len(finish_defs))
    if finish_defs:
        src = ast.unparse(finish_defs[0])
        check("static. _finish_panel_notification's UPDATE carries the status='running' CAS", "status='running'" in src, src)

    loop_defs = [n for n in PANEL_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_panel_notification_loop"]
    check("static. _panel_notification_loop defined exactly once (unchanged, no new override)", len(loop_defs) == 1, len(loop_defs))
    if loop_defs:
        loop_src = ast.unparse(loop_defs[0])
        check("static. _panel_notification_loop calls the reaper", "_reap_stale_running_panel_notifications(" in loop_src, None)
        reap_idx = loop_src.find("_reap_stale_running_panel_notifications(")
        take_idx = loop_src.find("_take_panel_notifications(")
        check("static. the reaper runs BEFORE _take_panel_notifications each iteration", reap_idx != -1 and take_idx != -1 and reap_idx < take_idx, (reap_idx, take_idx))


def main() -> int:
    test_reap_stale_running()
    test_finish_after_reap_is_noop()
    test_normal_finish_still_works()
    test_static_wiring()

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
