# -*- coding: utf-8 -*-
"""tools/catchup_managerbot_card_selftest.py -- offline self-test for the
"downtime catch-up enqueues ManagerBot lead cards" fix in main.py.

Under test: _tp_catchup_enqueue_manager_card, executed for real against a
throwaway temporary SQLite DB, together with the real lead_card writers it
depends on (_mb_insert_event / _mb_ensure_events_table / _mb_build_event_key /
_mb_build_payload). main.py is NOT importable (Telethon/env side effects at
import time), so these functions are AST-extracted and exec'd into a namespace
seeded with fakes for their module globals -- the same technique used by
tools/reserve_release_selftest.py and tools/auto_status_disable_selftest.py.

Everything DB-related runs for real (real aiosqlite, real INSERT OR IGNORE);
only the Telethon/env/log boundaries are faked. No network, no Telegram, no
production DB, no external APIs.

    python3.12 tools\\catchup_managerbot_card_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import datetime as _dt
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aiosqlite

MAIN_PY = BASE_DIR / "main.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def extract_and_exec(path: Path, names: set[str], extra_ns: dict) -> dict:
    """AST-extract the LAST (active) def of each requested name and exec into a
    seeded namespace -- same technique as the other tools/*_selftest.py files."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    picked: dict = {}
    for node in tree.body:
        if getattr(node, "name", None) in names:
            picked[node.name] = node  # later defs overwrite earlier -> last wins
    missing = names - set(picked)
    if missing:
        raise AssertionError(f"could not find {missing} as top-level defs in {path}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in sorted(picked))
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path.name}>", "exec"), ns)
    return ns


class _FakeKyiv:
    """Stand-in for _kyiv_now(): only .date().isoformat() is used."""

    def date(self):
        return _dt.date(2026, 7, 11)


def _build_ns(tmp_db: str, log_lines: list):
    def _fake_catchup_log(manager_key, msg):
        log_lines.append((str(manager_key), str(msg)))

    ns = extract_and_exec(
        MAIN_PY,
        {
            "_tp_catchup_enqueue_manager_card",
            "_mb_insert_event",
            "_mb_ensure_events_table",
            "_mb_build_event_key",
            "_mb_build_payload",
            "_mb_current_lead_date",
            "_mb_now_iso",
        },
        {
            "_mb_aiosqlite": aiosqlite,
            "TPILOT_DB_PATH": tmp_db,
            "MANAGER_RUNTIME_KEY": "mgr1",
            "_mb_json": json,
            "_mb_datetime": _dt.datetime,
            "_kyiv_now": lambda: _FakeKyiv(),
            "_tp_catchup_log": _fake_catchup_log,
            "_MB_EVENTS_READY": False,
            "Dict": dict,
            "Any": object,
        },
    )
    return ns


def _events(tmp_db: str, event_type: str = "") -> list:
    con = sqlite3.connect(tmp_db)
    con.row_factory = sqlite3.Row
    try:
        if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='manager_bot_events'").fetchone():
            return []
        if event_type:
            rows = con.execute("SELECT * FROM manager_bot_events WHERE event_type=?", (event_type,)).fetchall()
        else:
            rows = con.execute("SELECT * FROM manager_bot_events").fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def _countable_row(chat_id: int = 111, row_id: int = 5001) -> dict:
    return {
        "id": row_id, "manager_key": "mgr1", "chat_id": chat_id, "lead_date": "2026-07-11",
        "lead_countable": 1, "contact_kind": "new", "status": "pending", "quality_bucket": "",
        "username": "leaduser", "full_name": "Lead User", "phone": "",
    }


def run_checks(tmp_db: str) -> None:
    log_lines: list = []
    ns = _build_ns(tmp_db, log_lines)
    enqueue = ns["_tp_catchup_enqueue_manager_card"]

    # ------------------------------------------------------------------
    # 1. A catch-up-created countable daily_leads row creates exactly ONE
    #    manager_bot_events lead_card.
    # ------------------------------------------------------------------
    asyncio.run(enqueue("mgr1", 111, _countable_row()))
    cards = _events(tmp_db, "lead_card")
    check("1. countable catch-up lead creates exactly one lead_card event", len(cards) == 1, repr(cards))
    check("1b. the lead_card event_key is lead_card:<mk>:<date>:<chat>",
          cards and cards[0]["event_key"] == "lead_card:mgr1:2026-07-11:111", repr(cards[0]["event_key"] if cards else None))
    check("1c. event_type is 'lead_card' and manager_key/chat_id/lead_date are set",
          cards and cards[0]["event_type"] == "lead_card" and cards[0]["manager_key"] == "mgr1"
          and cards[0]["chat_id"] == 111 and cards[0]["lead_date"] == "2026-07-11")
    check("1d. a card_enqueue_ok line was logged with the identifying fields",
          any("card_enqueue_ok" in m and "chat_id=111" in m and "event_key=lead_card:mgr1:2026-07-11:111" in m
              for _k, m in log_lines), repr(log_lines))

    # ------------------------------------------------------------------
    # 2. A second catch-up run over the SAME lead does not duplicate the card.
    # ------------------------------------------------------------------
    first_id = cards[0]["id"]
    first_created = cards[0]["created_at"]
    log_lines.clear()
    asyncio.run(enqueue("mgr1", 111, _countable_row()))
    cards2 = _events(tmp_db, "lead_card")
    check("2. second catch-up run does NOT create a duplicate lead_card (still exactly one)", len(cards2) == 1, repr(cards2))
    check("2b. the existing lead_card row is untouched (same id + created_at -- INSERT OR IGNORE)",
          cards2 and cards2[0]["id"] == first_id and cards2[0]["created_at"] == first_created)
    check("2c. the repeat run logged card_enqueue_skipped_existing (not a second _ok)",
          any("card_enqueue_skipped_existing" in m for _k, m in log_lines)
          and not any("card_enqueue_ok" in m for _k, m in log_lines), repr(log_lines))

    # ------------------------------------------------------------------
    # 3. A pre-existing lead_card (e.g. created live before the outage) is left
    #    untouched when catch-up encounters the same lead.
    # ------------------------------------------------------------------
    con = sqlite3.connect(tmp_db)
    con.execute(
        "UPDATE manager_bot_events SET daily_lead_id=9999, payload_json='LIVE_ORIGINAL' "
        "WHERE event_key='lead_card:mgr1:2026-07-11:111'"
    )
    con.commit()
    con.close()
    log_lines.clear()
    asyncio.run(enqueue("mgr1", 111, _countable_row()))
    con = sqlite3.connect(tmp_db)
    con.row_factory = sqlite3.Row
    row = dict(con.execute("SELECT * FROM manager_bot_events WHERE event_key='lead_card:mgr1:2026-07-11:111'").fetchone())
    con.close()
    check("3. an existing lead_card is NOT overwritten by catch-up (payload_json + daily_lead_id preserved)",
          row["payload_json"] == "LIVE_ORIGINAL" and row["daily_lead_id"] == 9999, repr(row))

    # ------------------------------------------------------------------
    # 4. Catch-up NEVER creates a duplicate_card, and a non-countable row
    #    (duplicate/returning/old_baseline => lead_countable=0) enqueues nothing.
    # ------------------------------------------------------------------
    all_events_before = _events(tmp_db)
    log_lines.clear()
    noncountable = {
        "id": 0, "manager_key": "mgr1", "chat_id": 222, "lead_date": "2026-07-11",
        "lead_countable": 0, "contact_kind": "duplicate", "dedupe_reason": "seen_before_other_manager",
    }
    asyncio.run(enqueue("mgr1", 222, noncountable))
    also_countable_but_no_id = dict(_countable_row(chat_id=333, row_id=0))  # countable flag but no persisted id
    asyncio.run(enqueue("mgr1", 333, also_countable_but_no_id))
    all_events_after = _events(tmp_db)
    check("4. a non-countable (duplicate) row enqueues NOTHING (no lead_card, no new event)",
          len(all_events_after) == len(all_events_before), f"{len(all_events_before)} -> {len(all_events_after)}")
    check("4b. a 'countable' flag without a persisted id>0 also enqueues nothing (guards against pseudo-rows)",
          not any(int(e.get("chat_id") or 0) == 333 for e in all_events_after))
    check("4c. catch-up NEVER created a duplicate_card event",
          len(_events(tmp_db, "duplicate_card")) == 0, repr(_events(tmp_db, "duplicate_card")))
    check("4d. no log line was emitted for the non-applicable non-countable rows (silent skip)",
          not any("card_enqueue" in m for _k, m in log_lines), repr(log_lines))

    # ------------------------------------------------------------------
    # 5. Manual-status / override tables are never touched by the enqueue.
    # ------------------------------------------------------------------
    con = sqlite3.connect(tmp_db)
    con.execute("CREATE TABLE lead_status_overrides(chat_id INTEGER, manager_key TEXT, status TEXT, bucket TEXT)")
    con.execute("INSERT INTO lead_status_overrides VALUES(111,'mgr1','liquid','liquid')")
    con.commit()
    con.close()
    before_ovr = _dump(tmp_db, "lead_status_overrides")
    asyncio.run(enqueue("mgr1", 111, _countable_row()))  # same lead again
    after_ovr = _dump(tmp_db, "lead_status_overrides")
    check("5. lead_status_overrides is completely unmodified by the card enqueue (manual status authority preserved)",
          before_ovr == after_ovr, f"{before_ovr!r} != {after_ovr!r}")


def _dump(tmp_db: str, table: str) -> list:
    con = sqlite3.connect(tmp_db)
    try:
        return con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    finally:
        con.close()


def run_safety_checks(tmp_db: str) -> None:
    print("\n-- Safety --")
    real = os.path.realpath(tmp_db)
    check("6. selftest used ONLY a temp SQLite file (never db/data_tpilot.db or db/data.db)",
          os.path.realpath(tempfile.gettempdir()) in real
          and "data_tpilot.db" not in real and (os.sep + "db" + os.sep) not in real, real)
    self_src = Path(__file__).read_text(encoding="utf-8-sig")
    import re
    check("7. selftest makes no network/Telethon/API calls",
          not re.search(r"^\s*(import|from)\s+(requests|telethon|urllib|http\.client|anthropic)\b", self_src, re.MULTILINE))


def main() -> int:
    tmp_db = tempfile.mktemp(suffix="_catchup_card_selftest.db")
    try:
        print("-- Catch-up ManagerBot lead-card enqueue (real funcs via AST, temp SQLite) --")
        run_checks(tmp_db)
        run_safety_checks(tmp_db)
    finally:
        try:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
        except Exception:
            pass

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
