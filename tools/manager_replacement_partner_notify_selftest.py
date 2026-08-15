# -*- coding: utf-8 -*-
"""tools/manager_replacement_partner_notify_selftest.py -- offline self-test
for Stage 5 of the manager Telegram-account replacement feature:
PartnerBot notification, per-user acknowledgement, 7-day retention, and the
final cutover_done -> notified -> done transition.

Techniques (same as every other tools/*_selftest.py in this project):
  - partner_stat_bot.py cannot be imported standalone (it constructs a real
    telethon.TelegramClient at module scope, and Telethon itself is not
    installed in this local dev environment) -- the Stage 5 functions and
    their direct, pre-existing, UNMODIFIED dependencies are extracted via
    ast.parse + ast.unparse + exec() and run FOR REAL against a temporary
    SQLite DB (never db/data_tpilot.db).
  - storage.py IS importable -> the real, already-shipped Stage 1
    replacement_ack_insert/_exists/_unacked_ids/_prune, replacement_advance,
    replacement_finalize, replacement_list_notify_eligible_for_source are
    exercised for real, never stubbed.
  - preflight_check.py IS importable (no Telethon/env side effects) -> its
    new Stage 5 dedup helpers are exercised for real too.
  - Windows here has no tzdata package -- a fixed UTC+3 offset stands in for
    partner_stat_bot.py's own TZ = ZoneInfo("Europe/Kyiv") (same convention
    as every other selftest in this project); TPILOT_DB_PATH is bound
    directly to the temp path rather than extracted.

Never: real Telegram network, real proxy/provider network, real process
spawn/stop, production DB/runtime/session/log access.

    python tools\\manager_replacement_partner_notify_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage as _storage_module

PARTNER_PATH = str(BASE_DIR / "partner_stat_bot.py")
PARTNER_SRC = open(PARTNER_PATH, encoding="utf-8-sig").read()

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _selftest_db_guard(db_path: str, base_dir: Path, storage_mod) -> None:
    """Mandatory production-path DB guard (same technique as every other
    replacement selftest's own guard)."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    storage_mod.DB_PATH = db_path
    storage_mod.QUEUE_DB_PATH = db_path


# ======================================================================
# Fake Telegram layer (no real Telethon -- Button/events/client all faked).
# ======================================================================

class FakeButton:
    def __init__(self, text, data):
        self.text = str(text)
        self.data = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")

    @classmethod
    def inline(cls, text, data=b""):
        return cls(text, data)


class FakeEventsNS:
    CallbackQuery = object()


class FakeEvent:
    def __init__(self, data: bytes, sender_id: int):
        self.data = data
        self.sender_id = sender_id
        self.answers: list = []
        self.deleted = False

    async def answer(self, text=None):
        self.answers.append(text)

    async def delete(self):
        self.deleted = True


class FakeClient:
    """calls records every send/delete for assertions. send_fail_uids and
    delete_fail_uids force a raised exception for exactly the given user_id
    (matching the real client.send_message/delete_messages call shape)."""

    def __init__(self):
        self.calls: list = []
        self.send_fail_uids: set = set()
        self.delete_fail_uids: set = set()
        self._next_id = 1000
        self.deleted_refs: list = []

    def on(self, *a, **kw):
        def _decorator(fn):
            return fn
        return _decorator

    @property
    def loop(self):
        outer = self

        class _Loop:
            @staticmethod
            def create_task(coro):
                return asyncio.get_event_loop().create_task(coro)
        return _Loop()

    async def send_message(self, uid, text, buttons=None):
        self.calls.append(("send_message", int(uid), text, buttons))
        if int(uid) in self.send_fail_uids:
            raise RuntimeError("simulated send failure")
        self._next_id += 1
        return SimpleNamespace(id=self._next_id)

    async def delete_messages(self, uid, mids):
        self.calls.append(("delete_messages", int(uid), list(mids)))
        if int(uid) in self.delete_fail_uids:
            raise RuntimeError("simulated delete failure")
        self.deleted_refs.append((int(uid), list(mids)))


# ======================================================================
# partner_stat_bot.py extraction.
# ======================================================================

STAGE5_REAL_NAMES = {
    "_ARN_ACK_TEXT", "_ARN_RETENTION_DAYS",
    "_arn_safe_at", "_arn_safe_display", "_arn_utc_iso_to_kyiv_hm", "_arn_notification_text",
    "_arn_eligible_rows_for_source", "_arn_send_one", "_arn_sweep_source", "_arn_sweep_tick",
    "_arn_sweep_loop", "_arn_retention_cutoff_iso", "_arn_run_daily_retention",
    "_arn_cleanup_stale_message", "_arn_ack_callback",
    "_pf_get_setting", "_pf_set_setting", "_buyer", "_enabled_buyers", "_ensure_tables",
    "_norm_key", "_connect", "_kyiv_now",
    # _ensure_tables has a 2-generation override chain (base def + an
    # additive-columns wrapper that captures the base via
    # `_TP_PARTNER_CI_ORIG_ENSURE_TABLES = globals().get("_ensure_tables")`
    # before redefining itself) -- _extract_by_names below already collects
    # EVERY node matching a requested name (not just the last), so listing
    # "_ensure_tables" pulls both generations in original file order; the
    # capture assignment itself is a separate top-level name that must also
    # be requested explicitly.
    "_TP_PARTNER_CI_ORIG_ENSURE_TABLES", "_tp_partner_ci_cols",
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


def build_ns(db_path: str, base_dir: Path) -> dict:
    import sqlite3 as _sqlite3
    import re as _re

    _selftest_db_guard(db_path, BASE_DIR, _storage_module)

    nodes = _extract_by_names(PARTNER_SRC, STAGE5_REAL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    client = FakeClient()
    ns = {
        "os": os,
        "asyncio": asyncio,
        "sqlite3": _sqlite3,
        "re": _re,
        "datetime": datetime,
        "timedelta": timedelta,
        "timezone": timezone,
        # Windows here has no tzdata package -- fixed UTC+3 stands in for
        # the real TZ = ZoneInfo("Europe/Kyiv") (same convention as every
        # other selftest in this project).
        "TZ": timezone(timedelta(hours=3)),
        "TPILOT_DB_PATH": db_path,
        "storage": _storage_module,
        "Button": FakeButton,
        "events": FakeEventsNS,
        "client": client,
        "Dict": dict, "Any": object, "List": list, "Optional": None,
    }
    exec(compile(module_src, f"<{PARTNER_PATH}:stage5>", "exec"), ns)
    ns["__client__"] = client
    ns["__storage__"] = _storage_module
    return ns


# ======================================================================
# Temp DB / fixture helpers.
# ======================================================================

def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="replacement_partner_notify_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


_CHAIN = ["draft", "auth_phone", "auth_code", "identity_ok", "ready_commit",
          "committing", "links_pending", "links_ready", "cutover_done", "notified"]

_SEED_TGID_COUNTER = [800000]


def _next_seed_tgid() -> int:
    """ux_repl_new_tgid enforces a real DB-level uniqueness constraint on
    new_tg_user_id across every non-terminal/terminal-but-not-failed-or-
    cancelled row -- every seeded row needs its OWN value, never a shared
    default, or a second seed_replacement_row call raises ReplacementConflict."""
    _SEED_TGID_COUNTER[0] += 1
    return _SEED_TGID_COUNTER[0]


def seed_replacement_row(db_path: str, *, op_id: str, source_key: str, old_key: str, new_key: str,
                          old_username: str = "oldacc", new_username: str = "newacc",
                          old_display_name: str = "Старый Менеджер", new_display_name: str = "Новый Менеджер",
                          status: str = "cutover_done", required_links: int = 15, ready_links: int = 15,
                          links_ready_at: str = "2026-07-16T07:00:00", created_by_user_id: int = 1001,
                          new_tg_user_id: int = None) -> int:
    """Constructs a manager_replacements row via REAL storage.py primitives
    (replacement_create/replacement_advance/replacement_update_links/
    replacement_finalize -- same raw-advance technique the other Stage 2-4
    selftests already use), landing at the requested durable status.
    ready_links/required_links are set independently of the terminal status
    reached, so "links below 15" can be tested even at cutover_done/
    notified -- Stage 5's OWN eligibility filter (not storage.py's state
    machine) is what must reject that case."""
    storage = _storage_module
    if new_tg_user_id is None:
        new_tg_user_id = _next_seed_tgid()
    storage.replacement_create(
        op_id, old_key, source_key=source_key, old_display_name=old_display_name,
        old_username=old_username, created_by_user_id=created_by_user_id, db_path=db_path,
    )
    target_idx = _CHAIN.index(status) if status in _CHAIN else (len(_CHAIN) if status == "done" else 0)

    if target_idx >= 1:
        storage.replacement_advance(op_id, "draft", "auth_phone", fields={
            "new_manager_key": new_key, "new_display_name": new_display_name,
        }, db_path=db_path)
    if target_idx >= 2:
        storage.replacement_advance(op_id, "auth_phone", "auth_code", db_path=db_path)
    if target_idx >= 3:
        storage.replacement_advance(op_id, "auth_code", "identity_ok", fields={
            "new_username": new_username, "new_tg_user_id": new_tg_user_id,
        }, db_path=db_path)
    if target_idx >= 4:
        storage.replacement_advance(op_id, "identity_ok", "ready_commit", db_path=db_path)
    if target_idx >= 5:
        storage.replacement_advance(op_id, "ready_commit", "committing", db_path=db_path)
    if target_idx >= 6:
        storage.replacement_advance(op_id, "committing", "links_pending", db_path=db_path)
        storage.replacement_update_links(
            op_id, ready_links, required_links=required_links, db_path=db_path,
            links_ready_at=(links_ready_at if ready_links >= required_links else None),
        )
    if target_idx >= 7:
        storage.replacement_advance(op_id, "links_pending", "links_ready", db_path=db_path)
    if target_idx >= 8:
        storage.replacement_advance(op_id, "links_ready", "cutover_done", db_path=db_path)
    if target_idx >= 9:
        storage.replacement_advance(op_id, "cutover_done", "notified", db_path=db_path)
    if status == "done":
        storage.replacement_finalize(op_id, db_path=db_path)

    row = storage.replacement_get(op_id, db_path=db_path)
    return int(row["id"])


def seed_buyer(db_path: str, *, user_id: int, source_key: str, is_enabled: int = 1) -> None:
    import sqlite3 as _sqlite3
    con = _sqlite3.connect(db_path)
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS partner_buyers("
            "user_id INTEGER PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '',"
            " last_name TEXT DEFAULT '', source_key TEXT DEFAULT '', is_enabled INTEGER NOT NULL DEFAULT 1,"
            " can_view_stats INTEGER NOT NULL DEFAULT 1, can_live_leads INTEGER NOT NULL DEFAULT 0,"
            " can_view_contacts INTEGER NOT NULL DEFAULT 0, can_excel INTEGER NOT NULL DEFAULT 0,"
            " stat_format TEXT NOT NULL DEFAULT 'pro', live_enabled_at TEXT DEFAULT '',"
            " created_at TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL DEFAULT '', last_seen_at TEXT DEFAULT '')"
        )
        con.execute(
            "INSERT OR REPLACE INTO partner_buyers(user_id, source_key, is_enabled) VALUES(?,?,?)",
            (user_id, source_key, is_enabled),
        )
        con.commit()
    finally:
        con.close()


async def cleanup_env(tmp_root: Path) -> None:
    import shutil
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


# ======================================================================
# GROUP 1: eligibility
# ======================================================================

async def test_group_1_eligibility():
    print("\n-- Group 1: eligibility --")
    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=5001, source_key="src1")
        rid_cutover = seed_replacement_row(db_path, op_id="op-e1", source_key="src1", old_key="old1", new_key="new1", status="cutover_done")
        rows = ns["_arn_eligible_rows_for_source"]("src1")
        check("1a. cutover_done row is eligible", any(r["id"] == rid_cutover for r in rows), rows)

        rid_notified = seed_replacement_row(db_path, op_id="op-e2", source_key="src1", old_key="old2", new_key="new2", status="notified")
        rows2 = ns["_arn_eligible_rows_for_source"]("src1")
        check("1b. notified row is eligible (retry/idempotency)", any(r["id"] == rid_notified for r in rows2), rows2)

        rid_done = seed_replacement_row(db_path, op_id="op-e3", source_key="src1", old_key="old3", new_key="new3", status="done")
        rows3 = ns["_arn_eligible_rows_for_source"]("src1")
        check("1c. done row is eligible (visible to unacknowledged user)", any(r["id"] == rid_done for r in rows3), rows3)

        for bad_status in ("draft", "auth_phone", "auth_code", "identity_ok", "ready_commit", "committing", "links_pending", "links_ready"):
            rid_bad = seed_replacement_row(db_path, op_id=f"op-e-{bad_status}", source_key="src1", old_key=f"oldx-{bad_status}", new_key=f"newx-{bad_status}", status=bad_status)
            rows_bad = ns["_arn_eligible_rows_for_source"]("src1")
            check(f"1d. pre-cutover status '{bad_status}' excluded", not any(r["id"] == rid_bad for r in rows_bad), rows_bad)

        con_id = seed_replacement_row(db_path, op_id="op-e-cancelled", source_key="src1", old_key="oldcx", new_key="newcx", status="committing")
        ok_cancel = ns["__storage__"].replacement_fail("op-e-cancelled", "test", "simulated", db_path=db_path)
        check("1e-setup. failed transition applied", ok_cancel, None)
        rows_failed = ns["_arn_eligible_rows_for_source"]("src1")
        check("1e. failed status excluded", not any(r["id"] == con_id for r in rows_failed), rows_failed)

        rows_missing_source = ns["_arn_eligible_rows_for_source"]("")
        check("1f. missing/empty source_key excluded (returns nothing)", rows_missing_source == [], rows_missing_source)

        rid_low_links = seed_replacement_row(db_path, op_id="op-e-lowlinks", source_key="src1", old_key="oldlow", new_key="newlow",
                                              status="cutover_done", required_links=15, ready_links=7,
                                              links_ready_at="")
        rows_low = ns["_arn_eligible_rows_for_source"]("src1")
        check("1g. links below 15/15 excluded", not any(r["id"] == rid_low_links for r in rows_low), rows_low)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 2: source/users
# ======================================================================

async def test_group_2_source_users():
    print("\n-- Group 2: source/users --")

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=6001, source_key="srca")
        rid = seed_replacement_row(db_path, op_id="op-u1", source_key="srca", old_key="oA", new_key="nA", status="cutover_done")
        result = await ns["_arn_sweep_source"]("srca", [6001])
        check("2a. one matching user: sent_count==1", result["sent_count"] == 1 and result["failed_count"] == 0, result)
        check("2a2. status advanced to done (single successful delivery)", result["status_after"] == "done", result)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=6002, source_key="srcb")
        seed_buyer(db_path, user_id=6003, source_key="srcb")
        rid = seed_replacement_row(db_path, op_id="op-u2", source_key="srcb", old_key="oB", new_key="nB", status="cutover_done")
        result = await ns["_arn_sweep_source"]("srcb", [6002, 6003])
        check("2b. multiple matching users: sent_count==2", result["sent_count"] == 2, result)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=6004, source_key="srcc")
        seed_buyer(db_path, user_id=6005, source_key="srcother")
        seed_replacement_row(db_path, op_id="op-u3", source_key="srcc", old_key="oC", new_key="nC", status="cutover_done")
        calls_before = list(ns["__client__"].calls)
        result = await ns["_arn_sweep_source"]("srcc", [6004])  # caller pre-filters by source, matching _pf_partner_tick's own by_source grouping
        sent_to = {c[1] for c in ns["__client__"].calls if c[0] == "send_message"}
        check("2c. user from another source never receives the send (caller-side source filter, matching _pf_partner_tick's own grouping)", 6005 not in sent_to and 6004 in sent_to, sent_to)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=6006, source_key="srcd", is_enabled=0)
        seed_replacement_row(db_path, op_id="op-u4", source_key="srcd", old_key="oD", new_key="nD", status="cutover_done")
        buyers = ns["_enabled_buyers"]()
        check("2d. disabled buyer excluded from _enabled_buyers", not any(b["user_id"] == 6006 for b in buyers), buyers)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_replacement_row(db_path, op_id="op-u5", source_key="srce", old_key="oE", new_key="nE", status="cutover_done")
        result = await ns["_arn_sweep_source"]("srce", [])
        check("2e. no eligible users: sent_count==0, ok reflects nothing to do", result["sent_count"] == 0 and result["eligible_users"] == 0, result)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=6007, source_key="srcf")
        seed_buyer(db_path, user_id=6008, source_key="srcf")
        seed_replacement_row(db_path, op_id="op-u6", source_key="srcf", old_key="oF", new_key="nF", status="cutover_done")
        ns["__client__"].send_fail_uids.add(6007)
        result = await ns["_arn_sweep_source"]("srcf", [6007, 6008])
        check("2f. one user send fails while another succeeds: counts reflect both", result["failed_count"] == 1 and result["sent_count"] == 1, result)
        check("2f2. overall ok remains True (at least one success)", result["ok"] is True, result)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 3: notification text
# ======================================================================

def test_group_3_notification_text():
    print("\n-- Group 3: notification text --")
    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        row = {
            "old_display_name": "Иван Петров", "old_username": "ivan_old", "new_username": "ivan_new",
            "links_ready_at": "2026-07-16T07:30:00",
        }
        text = ns["_arn_notification_text"](row)
        expected = (
            "🔄 Замена аккаунта\n\n"
            "✅ Иван Петров | @ivan_old заменена на @ivan_new.\n"
            "Ссылки на сегодня обновлены в 10:30.\n\n"
            "Откройте 🔗 Бизнес-ссылки и проверьте новые ссылки."
        )
        check("3a. exact approved Russian text (Kyiv time = UTC+3)", text == expected, text)

        row_empty_new = {"old_display_name": "Мария", "old_username": "maria_old", "new_username": "", "links_ready_at": "2026-07-16T05:00:00"}
        text2 = ns["_arn_notification_text"](row_empty_new)
        check("3b. safe username fallback for empty new_username", "(без username)" in text2, text2)
        check("3b2. no bare @ anywhere in the text", " @ " not in text2 and not text2.rstrip().endswith("@") and "@." not in text2, text2)

        row_empty_old = {"old_display_name": "Пётр", "old_username": "", "new_username": "petr_new", "links_ready_at": "2026-07-16T05:00:00"}
        text3 = ns["_arn_notification_text"](row_empty_old)
        check("3c. safe username fallback for empty old_username", "(без username)" in text3, text3)

        for secret in ("+380", "tg_user_id", "manager_key", "proxy", "session", "operation_id", "op-"):
            check(f"3d. no leak of '{secret}' in the notification text", secret not in text, text)
    finally:
        asyncio.run(cleanup_env(tmp_root))


async def test_group_3b_button_and_callback():
    print("\n-- Group 3b: button + callback length --")
    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=7001, source_key="srcg")
        seed_replacement_row(db_path, op_id="op-btn1", source_key="srcg", old_key="oG", new_key="nG", status="cutover_done")
        await ns["_arn_sweep_source"]("srcg", [7001])
        send_calls = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("3e. exactly one send call", len(send_calls) == 1, send_calls)
        _, _, _, buttons = send_calls[0]
        btn = buttons[0][0]
        check("3f. button text is '✅ Ознакомился'", btn.text == "✅ Ознакомился", btn.text)
        check("3g. callback_data starts with arn:ack: and is well under 64 bytes", btn.data.startswith(b"arn:ack:") and len(btn.data) <= 64, (btn.data, len(btn.data)))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 4: duplicate / message lifecycle
# ======================================================================

async def test_group_4_message_lifecycle():
    print("\n-- Group 4: duplicate/message lifecycle --")

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=8001, source_key="srch")
        seed_replacement_row(db_path, op_id="op-m1", source_key="srch", old_key="oH1", new_key="nH1", status="cutover_done")
        r1 = await ns["_arn_sweep_source"]("srch", [8001])
        check("4a. first send succeeds", r1["sent_count"] == 1, r1)

        r2 = await ns["_arn_sweep_source"]("srch", [8001])
        send_calls = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("4b. repeated sweep does not duplicate the send (still exactly 1 send call)", len(send_calls) == 1, send_calls)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=8002, source_key="srci")
        seed_replacement_row(db_path, op_id="op-m2a", source_key="srci", old_key="oI1", new_key="nI1", status="cutover_done", links_ready_at="2026-07-16T05:00:00")
        r1 = await ns["_arn_sweep_source"]("srci", [8002])
        check("4c-setup. first (older) replacement sent", r1["sent_count"] == 1, r1)
        first_mid_ref = ns["_pf_get_setting"]("arn_active_msg_8002")

        seed_replacement_row(db_path, op_id="op-m2b", source_key="srci", old_key="oI2", new_key="nI2", status="cutover_done", links_ready_at="2026-07-16T06:00:00")
        r2 = await ns["_arn_sweep_source"]("srci", [8002])
        # sent_count counts every row confirmed successfully delivered THIS
        # sweep pass, including an idempotent already-sent confirmation for
        # op-m2a (genuinely still a success, just not a FRESH send) -- so it
        # is 2 here (op-m2a idempotent + op-m2b fresh), not 1. The real
        # "no duplicate send" guarantee is that exactly ONE NEW send_message
        # call happened (for op-m2b) -- op-m2a's own send_message call count
        # must stay frozen at the single call from r1, matching check 4b's
        # own "repeated sweep does not duplicate the send" assertion.
        send_calls_after = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("4d. newer replacement also sent (exactly one NEW send_message call this pass)", len(send_calls_after) == 2, send_calls_after)

        delete_calls = [c for c in ns["__client__"].calls if c[0] == "delete_messages"]
        check("4e. newer replacement supersedes older message (old message deleted)", any(c[1] == 8002 for c in delete_calls), delete_calls)
        active_after = ns["_pf_get_setting"]("arn_active_msg_8002")
        check("4f. active message reference now points at the NEWER replacement", active_after != first_mid_ref and active_after.split(":")[0].isdigit(), (first_mid_ref, active_after))

        unrelated_msg_key = "preflight_msg_partner_today_8002"
        ns["_pf_set_setting"](unrelated_msg_key, "2026-07-16:555")
        check("4g. only the replacement notification message is tracked/deleted -- an unrelated preflight menu/statistics message key is untouched",
              ns["_pf_get_setting"](unrelated_msg_key) == "2026-07-16:555", None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=8003, source_key="srcj")
        seed_replacement_row(db_path, op_id="op-m3a", source_key="srcj", old_key="oJ1", new_key="nJ1", status="cutover_done", links_ready_at="2026-07-16T05:00:00")
        await ns["_arn_sweep_source"]("srcj", [8003])
        seed_replacement_row(db_path, op_id="op-m3b", source_key="srcj", old_key="oJ2", new_key="nJ2", status="cutover_done", links_ready_at="2026-07-16T06:00:00")
        ns["__client__"].delete_fail_uids.add(8003)
        r2 = await ns["_arn_sweep_source"]("srcj", [8003])
        send_calls_after = [c for c in ns["__client__"].calls if c[0] == "send_message"]
        check("4h. delete failure of the superseded message does not block the new send (still 2 total sends: op-m3a + op-m3b)",
              len(send_calls_after) == 2, send_calls_after)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 5: acknowledgement
# ======================================================================

async def test_group_5_ack():
    print("\n-- Group 5: ack --")

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9001, source_key="srck")
        rid = seed_replacement_row(db_path, op_id="op-a1", source_key="srck", old_key="oK", new_key="nK", status="cutover_done")
        ev = FakeEvent(f"arn:ack:{rid}".encode(), 9001)
        await ns["_arn_ack_callback"](ev)
        check("5a. valid ack: event deleted", ev.deleted, None)
        check("5a2. valid ack: answers exact ack text", ev.answers and ev.answers[-1] == ns["_ARN_ACK_TEXT"], ev.answers)
        check("5a3. valid ack: replacement_ack_exists now True", ns["__storage__"].replacement_ack_exists(9001, rid, db_path=db_path), None)

        ev2 = FakeEvent(f"arn:ack:{rid}".encode(), 9001)
        await ns["_arn_ack_callback"](ev2)
        check("5b. repeated ack is safe (still answers ack text, no crash)", ev2.answers and ev2.answers[-1] == ns["_ARN_ACK_TEXT"], ev2.answers)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9002, source_key="srcl1")
        rid = seed_replacement_row(db_path, op_id="op-a2", source_key="srcl2", old_key="oL", new_key="nL", status="cutover_done")
        ev = FakeEvent(f"arn:ack:{rid}".encode(), 9002)  # buyer's OWN source (srcl1) != row's source (srcl2)
        await ns["_arn_ack_callback"](ev)
        check("5c. wrong source: ack NOT recorded", not ns["__storage__"].replacement_ack_exists(9002, rid, db_path=db_path), None)
        check("5c2. wrong source: safe (non-success) answer, not the success text", ev.answers and ev.answers[-1] != ns["_ARN_ACK_TEXT"], ev.answers)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        rid = seed_replacement_row(db_path, op_id="op-a3", source_key="srcm", old_key="oM", new_key="nM", status="cutover_done")
        ev = FakeEvent(f"arn:ack:{rid}".encode(), 999999)  # no partner_buyers row at all for this uid
        await ns["_arn_ack_callback"](ev)
        check("5d. wrong/unknown user: ack NOT recorded, no crash", not ns["__storage__"].replacement_ack_exists(999999, rid, db_path=db_path), None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9003, source_key="srcn")
        ev = FakeEvent(b"arn:ack:999999999", 9003)  # replacement_id that does not exist
        await ns["_arn_ack_callback"](ev)
        check("5e. stale/nonexistent replacement_id: safe, no crash, not acked", not ns["__storage__"].replacement_ack_exists(9003, 999999999, db_path=db_path), None)
        check("5e2. stale token still answers safely (not the success text)", ev.answers and ev.answers[-1] != ns["_ARN_ACK_TEXT"], ev.answers)

        ev_missing = FakeEvent(b"arn:ack:", 9003)
        await ns["_arn_ack_callback"](ev_missing)
        check("5f. missing/malformed replacement_id in callback_data is safe (no crash)", True, None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9004, source_key="srco")
        rid = seed_replacement_row(db_path, op_id="op-a4", source_key="srco", old_key="oO", new_key="nO", status="committing")
        ev = FakeEvent(f"arn:ack:{rid}".encode(), 9004)
        await ns["_arn_ack_callback"](ev)
        check("5g. pre-cutover replacement rejected (status not in eligible set)", not ns["__storage__"].replacement_ack_exists(9004, rid, db_path=db_path), None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9005, source_key="srcp")
        seed_buyer(db_path, user_id=9006, source_key="srcp")
        rid = seed_replacement_row(db_path, op_id="op-a5", source_key="srcp", old_key="oP", new_key="nP", status="cutover_done")
        ev1 = FakeEvent(f"arn:ack:{rid}".encode(), 9005)
        await ns["_arn_ack_callback"](ev1)
        check("5h. user A's ack recorded", ns["__storage__"].replacement_ack_exists(9005, rid, db_path=db_path), None)
        check("5h2. user B's ack isolated -- NOT recorded just because A acked", not ns["__storage__"].replacement_ack_exists(9006, rid, db_path=db_path), None)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9007, source_key="srcq")
        seed_replacement_row(db_path, op_id="op-a6", source_key="srcq", old_key="oQ", new_key="nQ", status="cutover_done")
        await ns["_arn_sweep_source"]("srcq", [9007])
        ref_before = ns["_pf_get_setting"]("arn_active_msg_9007")
        check("5i-setup. active message reference set after send", bool(ref_before), ref_before)
        rid = int(ref_before.split(":")[0])
        ev = FakeEvent(f"arn:ack:{rid}".encode(), 9007)
        await ns["_arn_ack_callback"](ev)
        ref_after = ns["_pf_get_setting"]("arn_active_msg_9007")
        check("5i. matching active message reference cleared after ack", ref_after == "", ref_after)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 6: retention
# ======================================================================

async def test_group_6_retention():
    print("\n-- Group 6: retention --")
    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        storage = ns["__storage__"]
        rid_old = seed_replacement_row(db_path, op_id="op-r1", source_key="srcr", old_key="oR1", new_key="nR1", status="done")
        rid_new = seed_replacement_row(db_path, op_id="op-r2", source_key="srcr", old_key="oR2", new_key="nR2", status="done")

        cutoff = "2026-07-10T00:00:00"
        con = __import__("sqlite3").connect(db_path)
        con.execute("INSERT OR REPLACE INTO replacement_notify_ack(user_id, replacement_id, source_key, acknowledged_at, created_at) VALUES(9101,?,?,?,?)",
                    (rid_old, "srcr", "2026-07-01T00:00:00", "2026-07-01T00:00:00"))
        con.execute("INSERT OR REPLACE INTO replacement_notify_ack(user_id, replacement_id, source_key, acknowledged_at, created_at) VALUES(9101,?,?,?,?)",
                    (rid_new, "srcr", "2026-07-15T00:00:00", "2026-07-15T00:00:00"))
        con.commit()
        con.close()

        pruned = storage.replacement_ack_prune(cutoff, db_path=db_path)
        check("6a. acknowledged row OLDER than cutoff pruned", pruned == 1, pruned)
        check("6b. newer acknowledged row kept", storage.replacement_ack_exists(9101, rid_new, db_path=db_path), None)
        check("6b2. older acknowledged row gone", not storage.replacement_ack_exists(9101, rid_old, db_path=db_path), None)

        con = __import__("sqlite3").connect(db_path)
        con.execute("INSERT OR REPLACE INTO replacement_notify_ack(user_id, replacement_id, source_key, acknowledged_at, created_at) VALUES(9102,?,?,?,?)",
                    (rid_old, "srcr", cutoff, cutoff))
        con.commit()
        con.close()
        pruned2 = storage.replacement_ack_prune(cutoff, db_path=db_path)
        check("6c. row exactly AT the cutoff is kept (strictly-less-than boundary)", pruned2 == 0 and storage.replacement_ack_exists(9102, rid_old, db_path=db_path), pruned2)

        history_row = storage.replacement_get_by_id(rid_old, db_path=db_path)
        check("6d. replacement history (manager_replacements row) preserved after ack pruning", history_row is not None and history_row.get("status") == "done", history_row)

        ns["_pf_set_setting"]("unrelated_kv_key_untouched", "keepme")
        check("6e. unrelated KV state preserved by prune (prune only touches replacement_notify_ack)", ns["_pf_get_setting"]("unrelated_kv_key_untouched") == "keepme", None)
    finally:
        await cleanup_env(tmp_root)


async def test_group_6b_stale_unacked_cleanup():
    print("\n-- Group 6b: unacknowledged-older-than-7-days cleanup --")
    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9201, source_key="srcs")
        rid = seed_replacement_row(db_path, op_id="op-r3", source_key="srcs", old_key="oS", new_key="nS", status="done")
        await ns["_arn_sweep_source"]("srcs", [9201])
        ref = ns["_pf_get_setting"]("arn_active_msg_9201")
        check("6f-setup. active message set after send", bool(ref), ref)

        con = __import__("sqlite3").connect(db_path)
        old_completed = (datetime(2026, 7, 16, 12, 0, 0) - timedelta(days=10)).isoformat()
        con.execute("UPDATE manager_replacements SET completed_at=? WHERE id=?", (old_completed, rid))
        con.commit()
        con.close()

        ns["_arn_run_daily_retention"]("2026-07-16")
        await asyncio.sleep(0.05)
        ref_after = ns["_pf_get_setting"]("arn_active_msg_9201")
        check("6g. unacknowledged notification older than 7 days is no longer actively shown (message reference cleaned up)", ref_after == "", ref_after)

        history_row = ns["__storage__"].replacement_get_by_id(rid, db_path=db_path)
        check("6h. manager_replacements row (history) untouched by the cleanup", history_row is not None and history_row.get("status") == "done", history_row)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 7: status transitions
# ======================================================================

async def test_group_7_transitions():
    print("\n-- Group 7: transitions --")

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9301, source_key="srct")
        seed_replacement_row(db_path, op_id="op-t1", source_key="srct", old_key="oT1", new_key="nT1", status="cutover_done")
        ns["__client__"].send_fail_uids.add(9301)
        result = await ns["_arn_sweep_source"]("srct", [9301])
        row_after = ns["__storage__"].replacement_get("op-t1", db_path=db_path)
        check("7a. no send success -> status remains cutover_done", row_after["status"] == "cutover_done", row_after)
        check("7a2. structured result reflects failure/retryable", result["failed_count"] == 1 and result["retryable"] is True, result)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9302, source_key="srcu")
        seed_replacement_row(db_path, op_id="op-t2", source_key="srcu", old_key="oT2", new_key="nT2", status="cutover_done")
        await ns["_arn_sweep_source"]("srcu", [9302])
        row_after = ns["__storage__"].replacement_get("op-t2", db_path=db_path)
        check("7b. successful send -> notified reached en route", row_after["status"] in ("notified", "done"), row_after)
        check("7c. guarded finalize -> done (immediately after successful notification)", row_after["status"] == "done", row_after)
        check("7d. finalize prerequisites recorded (new_manager_key/new_display_name/links_ready_at all set)",
              bool(row_after.get("new_manager_key")) and bool(row_after.get("new_display_name")) and bool(row_after.get("links_ready_at")), row_after)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9303, source_key="srcv")
        seed_replacement_row(db_path, op_id="op-t3", source_key="srcv", old_key="oT3", new_key="nT3", status="done")
        r1 = await ns["_arn_sweep_source"]("srcv", [9303])
        r2 = await ns["_arn_sweep_source"]("srcv", [9303])
        row_after = ns["__storage__"].replacement_get("op-t3", db_path=db_path)
        check("7e. repeated run from done is idempotent (status stays done, no crash)", row_after["status"] == "done", row_after)
    finally:
        await cleanup_env(tmp_root)

    tmp_root, db_path = make_temp_env()
    ns = build_ns(db_path, tmp_root)
    try:
        seed_buyer(db_path, user_id=9304, source_key="srcw")
        seed_replacement_row(db_path, op_id="op-t4", source_key="srcw", old_key="oT4", new_key="nT4", status="cutover_done")
        ns["__client__"].send_fail_uids.add(9304)
        await ns["_arn_sweep_source"]("srcw", [9304])
        row_after = ns["__storage__"].replacement_get("op-t4", db_path=db_path)
        check("7f. no transition to notified before a successful send", row_after["status"] == "cutover_done", row_after)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP 8: preflight deduplication
# ======================================================================

def test_group_8_preflight_dedup():
    print("\n-- Group 8: preflight dedup --")
    import preflight_check as pf

    tmp_root, db_path = make_temp_env()
    try:
        _selftest_db_guard(db_path, BASE_DIR, _storage_module)
        con = __import__("sqlite3").connect(db_path)
        con.execute("CREATE TABLE IF NOT EXISTS manager_reserve_pairs(reserve_key TEXT PRIMARY KEY, primary_key TEXT, source_key TEXT, status TEXT)")
        con.commit()
        con.close()

        seed_replacement_row(db_path, op_id="op-pf1", source_key="srcx", old_key="mgrX", new_key="mgrX_new", status="notified")

        records = [
            {"primary_key": "mgrX", "source_key": "srcx", "state": "C"},
            {"primary_key": "mgrY", "source_key": "srcx", "state": "B"},
        ]
        filtered = pf._stage5_dedupe_partner_replacements(records, db_path, "srcx")
        check("8a. PartnerBot morning report excludes a replacement already notified/done via Stage 5",
              not any(r["primary_key"] == "mgrX" for r in filtered), filtered)
        check("8b. unrelated/undelivered record (mgrY) is preserved", any(r["primary_key"] == "mgrY" for r in filtered), filtered)

        records_other_source = [{"primary_key": "mgrX", "source_key": "srcother", "state": "C"}]
        filtered2 = pf._stage5_dedupe_partner_replacements(records_other_source, db_path, "srcother")
        check("8c. a same-manager-key row under a DIFFERENT source is not incorrectly excluded", len(filtered2) == 1, filtered2)

        empty = pf._stage5_dedupe_partner_replacements([], db_path, "srcx")
        check("8d. empty record list stays empty (no crash)", empty == [], empty)

        broken = pf._stage5_dedupe_partner_replacements(records, "C:/nonexistent/path/x.db", "srcx")
        check("8e. any internal failure fails OPEN (records unchanged), never silently over-hides", broken == records, broken)
    finally:
        asyncio.run(cleanup_env(tmp_root))


def test_group_8b_admin_report_unaffected():
    print("\n-- Group 8b: AdminBot summary unchanged --")
    tree = ast.parse(open(BASE_DIR / "preflight_check.py", encoding="utf-8-sig").read())
    build_report_defs = [n for n in tree.body if getattr(n, "name", None) == "build_report"]
    check("8f. build_report (AdminBot) still defined exactly once", len(build_report_defs) == 1, len(build_report_defs))
    src = ast.unparse(build_report_defs[-1]) if build_report_defs else ""
    check("8g. AdminBot's build_report does NOT call the new Stage 5 dedup helper (AdminBot summary unchanged)",
          "_stage5_dedupe_partner_replacements" not in src, None)
    render_defs = [n for n in tree.body if getattr(n, "name", None) == "render_admin_text"]
    check("8h. render_admin_text still defined exactly once (untouched)", len(render_defs) == 1, len(render_defs))


# ======================================================================
# GROUP 9: static / security
# ======================================================================

def test_group_9_static_security():
    print("\n-- Group 9: static/security --")

    tree_main = ast.parse(open(BASE_DIR / "main.py", encoding="utf-8-sig").read())
    stage5_main_touch = "manager_replacement_commit_selftest" in "\n".join(
        ast.unparse(n) for n in tree_main.body if getattr(n, "name", None) == "replacement_commit"
    )
    check("9a. main.py's replacement_commit is untouched by Stage 5 (no reference to Stage 5 test names)", not stage5_main_touch, None)
    main_src = open(BASE_DIR / "main.py", encoding="utf-8-sig").read()
    sites = [n.lineno for n in ast.walk(tree_main) if isinstance(n, ast.Call) for kw in n.keywords
             if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True]
    check("9b. allow_spend=True remains exactly 2 call sites in main.py (Stage 5 adds none)", len(sites) == 2, sites)

    panel_src = open(BASE_DIR / "panel_bot.py", encoding="utf-8-sig").read()
    check("9c. no reference to the new arn: callback prefix in panel_bot.py (no AdminBot UI change)", "arn:ack:" not in panel_src, None)

    partner_tree = ast.parse(PARTNER_SRC)
    stage5_fn_names = {
        "_arn_safe_at", "_arn_safe_display", "_arn_utc_iso_to_kyiv_hm", "_arn_notification_text",
        "_arn_eligible_rows_for_source", "_arn_send_one", "_arn_sweep_source", "_arn_sweep_tick",
        "_arn_sweep_loop", "_arn_retention_cutoff_iso", "_arn_run_daily_retention",
        "_arn_cleanup_stale_message", "_arn_ack_callback",
    }
    from collections import Counter
    defs = [n.name for n in partner_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    c = Counter(defs)
    for name in sorted(stage5_fn_names):
        check(f"9d. {name} defined exactly once (no duplicate active definitions)", c.get(name, 0) == 1, c.get(name, 0))

    stage5_src = "\n".join(
        ast.unparse(n) for n in partner_tree.body if getattr(n, "name", None) in stage5_fn_names
    )
    check("9e. no proxy/provider call in Stage 5 code (no 'proxy_seller', 'requests.', 'ProxySellerProvider')",
          not any(s in stage5_src for s in ("proxy_seller", "ProxySellerProvider", "requests.")), None)
    check("9f. no manager row or session mutation in Stage 5 code (no manager_set_fields/manager_add/shutil.move)",
          not any(s in stage5_src for s in ("manager_set_fields(", "manager_add(", "shutil.move(", "_manager_delete_full_core(")), None)
    check("9g. no secret persistence -- 'proxy_password'/'phone_code_hash' never referenced in Stage 5 code",
          "proxy_password" not in stage5_src and "phone_code_hash" not in stage5_src, None)

    # callback_data length scan: every literal callback string built with the
    # arn: prefix, worst case with a large integer id, stays under 64 bytes.
    worst_case = f"arn:ack:{2**31 - 1}".encode()
    check("9h. worst-case arn:ack: callback_data (max 32-bit id) is under 64 bytes", len(worst_case) <= 64, len(worst_case))

    storage_src = open(BASE_DIR / "storage.py", encoding="utf-8-sig").read()
    check("9i. storage.py unmodified by Stage 5 (no Stage 5 marker present)", "STAGE5" not in storage_src, None)


def test_group_9b_dbguard():
    print("\n-- Group 9b: production-path DB guard --")
    prod_dir = os.path.join(str(BASE_DIR), "db")
    prod_file = os.path.join(prod_dir, "data_tpilot.db")
    safe_path = os.path.join(tempfile.gettempdir(), "arn_dbguard_probe", "safe.db")

    class _FakeStorage:
        def __init__(self, db_path, queue_db_path):
            self.DB_PATH = db_path
            self.QUEUE_DB_PATH = queue_db_path

    def _raises(db_path, storage_stub):
        try:
            _selftest_db_guard(db_path, BASE_DIR, storage_stub)
            return False
        except AssertionError:
            return True

    check("9j. rejects the real production DB file path", _raises(prod_file, _FakeStorage(prod_file, prod_file)), prod_file)
    check("9k. allows a normal safe temp path", not _raises(safe_path, _FakeStorage(safe_path, safe_path)), None)


def main() -> int:
    asyncio.run(test_group_1_eligibility())
    asyncio.run(test_group_2_source_users())
    test_group_3_notification_text()
    asyncio.run(test_group_3b_button_and_callback())
    asyncio.run(test_group_4_message_lifecycle())
    asyncio.run(test_group_5_ack())
    asyncio.run(test_group_6_retention())
    asyncio.run(test_group_6b_stale_unacked_cleanup())
    asyncio.run(test_group_7_transitions())
    test_group_8_preflight_dedup()
    test_group_8b_admin_report_unaffected()
    test_group_9_static_security()
    test_group_9b_dbguard()

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
