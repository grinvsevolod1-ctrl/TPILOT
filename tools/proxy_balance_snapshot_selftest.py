# -*- coding: utf-8 -*-
"""tools/proxy_balance_snapshot_selftest.py -- forward-fix Phase 2/4
(post-incident review 2026-07-26): the persisted Proxy-Seller balance
snapshot (proxy_balance_alert_state.balance_usd/balance_fetched_at/
balance_changed_at), its writer (storage.proxy_balance_snapshot_write,
called ONLY from main.py's existing scheduled _renewal_balance_alert_tick
on a successful fetch), and its readers (panel_bot.py's
_pb_persisted_proxy_balance/_pb_proxy_balance_value/_pb_cached_proxy_
balance_str/_pb_proxy_indicator_icon).

Layers tested:
  - storage.py: imported for real (no Telethon/env side effects there),
    exercised against a real temp sqlite file via asyncio.run.
  - main.py: _renewal_balance_alert_tick extracted for real via AST
    (main.py cannot be imported directly -- Telethon/env side effects at
    import), with ONLY _renewal_balance (the provider boundary) and
    _create_panel_notification (the Telegram-send boundary) faked.
  - panel_bot.py: the read-side helpers extracted for real via AST, with
    ONLY _connect_panel_db faked to point at the same temp sqlite file
    (same technique as tools/root_status_block_selftest.py test_7).

    python tools\\proxy_balance_snapshot_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402  -- real import, storage.py has no import-time side effects

FAILURES: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()
MAIN_TREE = ast.parse(MAIN_SRC)

PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()
PANEL_TREE = ast.parse(PANEL_SRC)


def _last_def(tree, name):
    node = None
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            node = n
    if node is None:
        raise AssertionError(f"no def {name} found")
    return node


def _last_assign(tree, name):
    node = None
    for n in tree.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name:
            node = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            node = n
    if node is None:
        raise AssertionError(f"no assign {name} found")
    return node


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="proxy_balance_snapshot_selftest_")
    os.close(fd)
    return path


def _read_row(db_path: str) -> dict:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        row = con.execute("SELECT * FROM proxy_balance_alert_state WHERE id=1").fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


# ======================================================================
# 1-3. storage.py's proxy_balance_snapshot_write, exercised for real
#      against a real temp sqlite file.
# ======================================================================

def test_1_first_success_persists() -> None:
    db_path = _make_temp_db()
    try:
        asyncio.run(storage.proxy_balance_snapshot_write(value=42.5, db_path=db_path))
        row = _read_row(db_path)
        check("1. first success persists balance_usd", row.get("balance_usd") == 42.5, row)
        check("1. first success sets balance_fetched_at", bool(row.get("balance_fetched_at")), row)
        check("1. first success sets balance_changed_at", bool(row.get("balance_changed_at")), row)
        check("1. fetched_at == changed_at on the very first write",
              row.get("balance_fetched_at") == row.get("balance_changed_at"), row)
    finally:
        os.unlink(db_path)


def test_2_same_value_updates_fetched_at_only() -> None:
    db_path = _make_temp_db()
    try:
        asyncio.run(storage.proxy_balance_snapshot_write(value=10.0, db_path=db_path))
        row1 = _read_row(db_path)
        time.sleep(1.1)  # cross a whole-second boundary (_now_iso() is second-resolution)
        asyncio.run(storage.proxy_balance_snapshot_write(value=10.0, db_path=db_path))
        row2 = _read_row(db_path)
        check("2. same value: balance_usd unchanged", row2["balance_usd"] == row1["balance_usd"], (row1, row2))
        check("2. same value: balance_changed_at UNCHANGED", row2["balance_changed_at"] == row1["balance_changed_at"], (row1, row2))
        check("2. same value: balance_fetched_at DOES advance", row2["balance_fetched_at"] != row1["balance_fetched_at"], (row1, row2))
        # Rounding: 10.001 and 10.004 both round to 10.00 -- must be treated as "same".
        time.sleep(1.1)
        asyncio.run(storage.proxy_balance_snapshot_write(value=10.001, db_path=db_path))
        row3 = _read_row(db_path)
        check("2. rounded-equal value (10.0 vs 10.001) still counts as unchanged",
              row3["balance_changed_at"] == row1["balance_changed_at"], (row1, row3))
    finally:
        os.unlink(db_path)


def test_3_changed_value_updates_changed_at() -> None:
    db_path = _make_temp_db()
    try:
        asyncio.run(storage.proxy_balance_snapshot_write(value=10.0, db_path=db_path))
        row1 = _read_row(db_path)
        time.sleep(1.1)
        asyncio.run(storage.proxy_balance_snapshot_write(value=15.0, db_path=db_path))
        row2 = _read_row(db_path)
        check("3. changed value: balance_usd updates", row2["balance_usd"] == 15.0, row2)
        check("3. changed value: balance_changed_at ADVANCES", row2["balance_changed_at"] != row1["balance_changed_at"], (row1, row2))
        check("3. changed value: balance_fetched_at ADVANCES", row2["balance_fetched_at"] != row1["balance_fetched_at"], (row1, row2))
    finally:
        os.unlink(db_path)


def test_4_migration_is_additive_and_reads_survive_missing_columns() -> None:
    """A pre-migration DB (only the original 3 columns) must not crash the
    reader -- _proxy_renewal_tables_ready's ADD COLUMN runs on the very
    next call, so this really only proves the read-side (proxy_balance_
    alert_state_get) tolerates a freshly-created table before any write."""
    db_path = _make_temp_db()
    try:
        state = asyncio.run(storage.proxy_balance_alert_state_get(db_path=db_path))
        check("4. fresh DB: balance_usd key present and None (no fetch yet)",
              "balance_usd" in state and state["balance_usd"] is None, state)
        check("4. fresh DB: below_threshold defaults to 0", int(state.get("below_threshold") or 0) == 0, state)
    finally:
        os.unlink(db_path)


def test_5_no_secrets_in_schema_or_writer() -> None:
    node = _last_def(ast.parse(open(str(BASE_DIR / "storage.py"), encoding="utf-8-sig").read()), "proxy_balance_snapshot_write")
    src = ast.unparse(node)
    for forbidden in ("password", "api_key", "api_id", "api_hash", "login", "token", "secret"):
        check(f"5. proxy_balance_snapshot_write never mentions {forbidden!r}", forbidden not in src.lower(), src[:200])


# ======================================================================
# 6-9. main.py's _renewal_balance_alert_tick, extracted for real (AST),
#      with ONLY the provider boundary (_renewal_balance) and the
#      Telegram-send boundary (_create_panel_notification) faked.
# ======================================================================

def build_tick_ns(db_path: str, *, balance_result, controller_mode: bool = True):
    nodes = [
        _last_assign(MAIN_TREE, "_PRENEW_BALANCE_ALERT_THRESHOLD_USD"),
        _last_assign(MAIN_TREE, "_PRENEW_BALANCE_ALERT_COOLDOWN_SEC"),
        _last_assign(MAIN_TREE, "_PRENEW_BALANCE_REFILL_URL"),
        _last_assign(MAIN_TREE, "_PRENEW_BALANCE_TICK_LOCK"),
        _last_def(MAIN_TREE, "_now_utc_iso"),
        _last_def(MAIN_TREE, "_renewal_balance_alert_tick"),
    ]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    notif_calls: list = []

    async def _fake_notif(kind, title, body):
        notif_calls.append((kind, title, body))

    async def _fake_balance():
        if isinstance(balance_result, Exception):
            raise balance_result
        return balance_result

    ns = {
        "asyncio": asyncio,
        "datetime": datetime,
        "CONTROLLER_MODE": controller_mode,
        "TPILOT_DB_PATH": db_path,
        "_renewal_balance": _fake_balance,
        "_create_panel_notification": _fake_notif,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:balance_tick>", "exec"), ns)
    ns["_notif_calls"] = notif_calls
    return ns


def test_6_tick_persists_on_success() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_tick_ns(db_path, balance_result=37.5)
        asyncio.run(ns["_renewal_balance_alert_tick"]())
        row = _read_row(db_path)
        check("6. tick success persists balance_usd", row.get("balance_usd") == 37.5, row)
        check("6. tick success sets balance_fetched_at", bool(row.get("balance_fetched_at")), row)
    finally:
        os.unlink(db_path)


def test_7_tick_failure_preserves_previous_value() -> None:
    """Mutation-relevant: the tick must NEVER call the snapshot writer when
    the provider read failed (balance is None) -- the row (and hence the
    root header) must show whatever the LAST successful fetch was."""
    db_path = _make_temp_db()
    try:
        ns1 = build_tick_ns(db_path, balance_result=22.0)
        asyncio.run(ns1["_renewal_balance_alert_tick"]())
        row_before = _read_row(db_path)
        check("7. [setup] first successful tick persisted 22.0", row_before.get("balance_usd") == 22.0, row_before)

        # Simulate a provider outage: _renewal_balance itself always fails
        # open to None on ANY exception (verified in main.py's real
        # _renewal_balance) -- this fake reproduces that exact contract.
        ns2 = build_tick_ns(db_path, balance_result=None)
        asyncio.run(ns2["_renewal_balance_alert_tick"]())
        row_after = _read_row(db_path)
        check("7. failed fetch (balance=None) PRESERVES the previous balance_usd",
              row_after.get("balance_usd") == 22.0, (row_before, row_after))
        check("7. failed fetch PRESERVES the previous balance_fetched_at (no phantom refresh)",
              row_after.get("balance_fetched_at") == row_before.get("balance_fetched_at"), (row_before, row_after))
        check("7. failed fetch never writes None/0 over a real value",
              row_after.get("balance_usd") not in (None, 0, 0.0), row_after)
    finally:
        os.unlink(db_path)


def test_8_timeout_preserves_previous_value() -> None:
    """A provider timeout surfaces through the SAME _renewal_balance ->
    None contract as any other read failure (verified: main.py's real
    _renewal_balance wraps the whole provider call in a bare except and
    always returns None on ANY exception, timeouts included) -- this test
    exercises that exact path via an exception-raising fake."""
    db_path = _make_temp_db()
    try:
        ns1 = build_tick_ns(db_path, balance_result=18.0)
        asyncio.run(ns1["_renewal_balance_alert_tick"]())
        row_before = _read_row(db_path)

        ns2 = build_tick_ns(db_path, balance_result=TimeoutError("simulated provider timeout"))
        asyncio.run(ns2["_renewal_balance_alert_tick"]())
        row_after = _read_row(db_path)
        check("8. simulated timeout PRESERVES the previous balance_usd",
              row_after.get("balance_usd") == row_before.get("balance_usd") == 18.0, (row_before, row_after))
    finally:
        os.unlink(db_path)


def test_9_restart_and_root_before_proxy_open() -> None:
    """'Restart' = a brand-new namespace/process never having called
    _prn_status_text (the Proxy-screen observer) -- the persisted snapshot
    must still be visible. Extracts panel_bot.py's REAL read-side helpers,
    faking ONLY _connect_panel_db (same technique as tools/root_status_
    block_selftest.py test_7)."""
    db_path = _make_temp_db()
    try:
        asyncio.run(storage.proxy_balance_snapshot_write(value=64.25, db_path=db_path))

        nodes = [
            _last_def(PANEL_TREE, "_pb_persisted_proxy_balance"),
            _last_def(PANEL_TREE, "_pb_proxy_balance_value"),
            _last_def(PANEL_TREE, "_pb_cached_proxy_balance_str"),
            _last_def(PANEL_TREE, "_pb_proxy_balance_low"),
            _last_def(PANEL_TREE, "_pb_proxy_indicator_icon"),
            _last_assign(PANEL_TREE, "_N531_PROXY_BALANCE_CACHE"),
        ]
        module_src = "\n\n".join(ast.unparse(n) for n in nodes)

        def _connect():
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            return con

        ns = {"Dict": dict, "Any": object, "_connect_panel_db": _connect}
        exec(compile(module_src, f"<{PANEL_PATH}:balance_read>", "exec"), ns)

        # This IS "restart": _N531_PROXY_BALANCE_CACHE (the in-memory
        # fallback) is freshly {"value": None} in this brand-new namespace
        # -- exactly what happens after an AdminBot process restart. No
        # call to _prn_status_text (the Proxy-screen observer) was ever
        # made in this namespace either -- "opening Proxy" never happened.
        check("9. fresh namespace: in-memory cache is None (proves this is NOT reading stale memory)",
              ns["_N531_PROXY_BALANCE_CACHE"]["value"] is None, ns["_N531_PROXY_BALANCE_CACHE"])

        rendered = ns["_pb_cached_proxy_balance_str"]()
        check("9. root balance string survives 'restart' AND 'Proxy never opened' -> '$64.25'",
              rendered == "$64.25", rendered)

        icon = ns["_pb_proxy_indicator_icon"]("🟢")
        check("9. icon reflects the KNOWN persisted balance (not the unknown-🟡 fallback)",
              icon == "🟢", icon)
    finally:
        os.unlink(db_path)


def test_10_failed_refresh_does_not_erase_root_value() -> None:
    """Same as test 7, but asserted at the panel_bot.py READ side -- proves
    the full loop (main.py writes only on success -> panel_bot.py reads
    the snapshot) keeps the root display intact across a failure."""
    db_path = _make_temp_db()
    try:
        asyncio.run(storage.proxy_balance_snapshot_write(value=5.5, db_path=db_path))
        ns_tick = build_tick_ns(db_path, balance_result=None)
        asyncio.run(ns_tick["_renewal_balance_alert_tick"]())  # failed fetch, no write

        nodes = [
            _last_def(PANEL_TREE, "_pb_persisted_proxy_balance"),
            _last_def(PANEL_TREE, "_pb_proxy_balance_value"),
            _last_def(PANEL_TREE, "_pb_cached_proxy_balance_str"),
            _last_assign(PANEL_TREE, "_N531_PROXY_BALANCE_CACHE"),
        ]
        module_src = "\n\n".join(ast.unparse(n) for n in nodes)

        def _connect():
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            return con

        ns = {"Dict": dict, "Any": object, "_connect_panel_db": _connect}
        exec(compile(module_src, f"<{PANEL_PATH}:balance_read2>", "exec"), ns)
        rendered = ns["_pb_cached_proxy_balance_str"]()
        check("10. root value survives a failed provider refresh -> still '$5.50'", rendered == "$5.50", rendered)
    finally:
        os.unlink(db_path)


def test_11_single_flight_lock() -> None:
    """Structural: the lock wraps the WHOLE tick body (fetch + persist +
    threshold logic), not just part of it. Behavioral: two concurrent
    tick invocations sharing the SAME lock instance never overlap their
    balance-fetch calls."""
    src = ast.unparse(_last_def(MAIN_TREE, "_renewal_balance_alert_tick"))
    check("11. [structural] tick references the shared lock", "_PRENEW_BALANCE_TICK_LOCK" in src, src[:200])

    node = _last_def(MAIN_TREE, "_renewal_balance_alert_tick")
    with_nodes = [n for n in ast.walk(node) if isinstance(n, ast.AsyncWith)]
    lock_with = None
    for w in with_nodes:
        for item in w.items:
            ctx = item.context_expr
            if isinstance(ctx, ast.Name) and ctx.id == "_PRENEW_BALANCE_TICK_LOCK":
                lock_with = w
    check("11. [structural] an 'async with _PRENEW_BALANCE_TICK_LOCK:' block exists", lock_with is not None, with_nodes)
    if lock_with is not None:
        body_src = "\n".join(ast.unparse(n) for n in lock_with.body)
        check("11. [structural] the lock's body contains the provider fetch", "_renewal_balance()" in body_src, body_src[:300])
        check("11. [structural] the lock's body contains the snapshot persist",
              "proxy_balance_snapshot_write" in body_src, body_src[:300])

    # Behavioral: real concurrency, real asyncio.Lock, one shared instance.
    db_path = _make_temp_db()
    try:
        concurrency = {"active": 0, "max_seen": 0}

        async def _slow_balance():
            concurrency["active"] += 1
            concurrency["max_seen"] = max(concurrency["max_seen"], concurrency["active"])
            await asyncio.sleep(0.05)
            concurrency["active"] -= 1
            return 9.0

        ns1 = build_tick_ns(db_path, balance_result=9.0)
        ns1["_renewal_balance"] = _slow_balance
        # Re-exec is unnecessary -- ns1 already has the function bound;
        # overwrite the fake balance fetcher it closes over is not
        # possible post-exec, so instead build ONE namespace and monkeypatch
        # its own global before invocation (exec'd module globals ARE ns1).
        ns1["_renewal_balance"] = _slow_balance

        async def _run_two():
            await asyncio.gather(
                ns1["_renewal_balance_alert_tick"](),
                ns1["_renewal_balance_alert_tick"](),
            )

        asyncio.run(_run_two())
        check("11. [behavioral] two concurrent ticks sharing one lock NEVER overlap (max concurrent fetches == 1)",
              concurrency["max_seen"] == 1, concurrency)
    finally:
        os.unlink(db_path)


def test_12_low_balance_anti_spam_unchanged() -> None:
    """Regression guard: the pre-existing below_threshold/cooldown/reset
    dedup logic must be byte-for-byte untouched by this phase (only NEW
    columns/behavior were added, nothing about the alert dedup changed)."""
    src = ast.unparse(_last_def(MAIN_TREE, "_renewal_balance_alert_tick"))
    for token in ("below_threshold", "last_alert_at", "_PRENEW_BALANCE_ALERT_COOLDOWN_SEC",
                  "proxy_balance_alert_state_set", "proxy_balance_alert_state_get"):
        check(f"12. tick still references {token!r} (alert dedup logic present, unremoved)", token in src, src[:200])
    threshold_src = ast.unparse(_last_assign(MAIN_TREE, "_PRENEW_BALANCE_ALERT_THRESHOLD_USD").value)
    cooldown_src = ast.unparse(_last_assign(MAIN_TREE, "_PRENEW_BALANCE_ALERT_COOLDOWN_SEC").value)
    check("12. alert threshold constant unchanged (20.0 USD)", threshold_src == "20.0", threshold_src)
    check("12. alert cooldown constant unchanged (24h == 24 * 3600)",
          eval(cooldown_src, {"__builtins__": {}}) == 24 * 3600, cooldown_src)


def test_13_no_secrets_persisted_rendered_or_logged() -> None:
    """The whole read/write path never touches provider credentials or
    proxy passwords -- neither in the SQL, nor in the rendered string."""
    def _code_only(node) -> str:
        """Unparsed source with the docstring (if any) excluded -- a
        docstring may legitimately DISCUSS credentials in prose (e.g. "no
        login/password ever leaves this function") without the CODE
        itself ever touching one."""
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        return "\n".join(ast.unparse(n) for n in body)

    write_src = _code_only(_last_def(ast.parse(open(str(BASE_DIR / "storage.py"), encoding="utf-8-sig").read()),
                                      "proxy_balance_snapshot_write"))
    tick_src = _code_only(_last_def(MAIN_TREE, "_renewal_balance_alert_tick"))
    read_src = _code_only(_last_def(PANEL_TREE, "_pb_cached_proxy_balance_str"))
    for label, src in (("storage writer", write_src), ("main.py tick", tick_src), ("panel_bot reader", read_src)):
        for forbidden in ("password", "api_key", "api_hash", "proxy_username", "login"):
            check(f"13. {label} never mentions {forbidden!r}", forbidden not in src.lower(), src[:200])


def main() -> int:
    test_1_first_success_persists()
    test_2_same_value_updates_fetched_at_only()
    test_3_changed_value_updates_changed_at()
    test_4_migration_is_additive_and_reads_survive_missing_columns()
    test_5_no_secrets_in_schema_or_writer()
    test_6_tick_persists_on_success()
    test_7_tick_failure_preserves_previous_value()
    test_8_timeout_preserves_previous_value()
    test_9_restart_and_root_before_proxy_open()
    test_10_failed_refresh_does_not_erase_root_value()
    test_11_single_flight_lock()
    test_12_low_balance_anti_spam_unchanged()
    test_13_no_secrets_persisted_rendered_or_logged()

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
