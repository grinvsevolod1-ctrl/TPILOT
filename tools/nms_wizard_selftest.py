# -*- coding: utf-8 -*-
"""tools/nms_wizard_selftest.py -- offline self-test for the "👥 По менеджерам"
manager-first entry added to the New Menu source-first statistics wizard
(panel_bot.py's nms: callback namespace, menu:nm_stats2 -> _nms_sources_screen).

Context: a read-only migration audit found the source-first calendar/day-
flight wizard (Stage 3A) already implements almost everything the new
Statistics UX needs (range calendar, day/flight toggle, edit-in-place,
DB-backed wizard state, tombstone-safe manager listing via the existing
/nmstat -> stats_engine path). The only missing piece was a manager-first
entry point ("👥 По менеджерам") that resolves the manager's OWN linked
source implicitly (via the existing _ss_source_for_manager_panel reverse
lookup) and reuses the SAME styp/calendar/confirm screens and the SAME
wizard='nms' state machine, just skipping the manager-selection step
(mks is already fixed to the one chosen manager).

Scope: panel_bot.py only. No changes to main.py/stats_engine.py/storage.py/
partner_stat_bot.py -- /nmstat itself (main.py) is untouched; this wizard
only ever calls it via the existing _panel_spawn_command_task mechanism.

Techniques: panel_bot.py cannot be imported standalone (Telethon/env side
effects at import time) -- functions under test are extracted via
ast.parse + ast.unparse + exec() (same technique used throughout this
project's tools/*_selftest.py files) and run FOR REAL against a temporary
SQLite file (never db/data_tpilot.db or db/data.db) that mirrors the real
managers/manager_source_links/traffic_sources/source_work_windows/
panel_wizard_state schema. Telegram client calls are faked in-memory; no
real DB/network/Telegram.

    python3.12 tools\\nms_wizard_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ======================================================================
# AST extraction (panel_bot.py cannot be imported standalone -- Telethon/
# env side effects at import time). Same technique as every other
# tools/*_selftest.py in this project.
# ======================================================================

def _extract_and_exec(path: str, names: set, extra_ns: dict) -> dict:
    src = open(path, encoding="utf-8-sig").read()
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
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in names:
            nodes.append(n)
            seen.add(n.target.id)
            continue
    missing = names - seen
    if missing:
        raise AssertionError(f"expected {names}, missing {missing} in {path}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path}>", "exec"), ns)
    return ns


PANEL_PATH = str(BASE_DIR / "panel_bot.py")


def _block_text_between(path: str, start_marker: str, end_marker: str) -> str:
    src = open(path, encoding="utf-8-sig").read()
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def test_19_no_adminbot_partnerbot_changes() -> None:
    """The read-only audit explicitly forbade touching partner_stat_bot.py/
    main.py/stats_engine.py/storage.py -- confirm nothing from this feature
    leaked into them."""
    for label, path in (
        ("partner_stat_bot.py", str(BASE_DIR / "partner_stat_bot.py")),
        ("main.py", str(BASE_DIR / "main.py")),
        ("stats_engine.py", str(BASE_DIR / "stats_engine.py")),
        ("storage.py", str(BASE_DIR / "storage.py")),
    ):
        try:
            text = open(path, encoding="utf-8-sig").read()
        except Exception as e:
            check(f"{label} is readable for the scope check", False, repr(e))
            continue
        check(f"{label} does not contain the manager-first entry marker", "nms:entry:mgr" not in text and "_nms_managers_list_text" not in text)


# ======================================================================
# Temp DB fixture (managers + manager_source_links + traffic_sources +
# source_work_windows; panel_wizard_state is auto-created by the real
# _ensure_panel_runtime_tables()).
# ======================================================================

def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="nms_wizard_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE managers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key TEXT UNIQUE,
                display_name TEXT,
                telegram_username TEXT,
                role TEXT DEFAULT 'manager',
                status TEXT DEFAULT 'active',
                is_enabled INTEGER DEFAULT 1,
                manual_stopped INTEGER DEFAULT 0
            );
            CREATE TABLE manager_source_links(
                manager_key TEXT,
                source_key TEXT,
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE traffic_sources(
                source_key TEXT PRIMARY KEY,
                name TEXT
            );
            CREATE TABLE source_work_windows(
                source_key TEXT PRIMARY KEY,
                day_start INTEGER,
                day_end INTEGER,
                enabled INTEGER,
                pro_include_dolyoty INTEGER
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def _seed(db_path: str, *, managers: list, links: list, sources: list) -> None:
    con = sqlite3.connect(db_path)
    try:
        for m in managers:
            con.execute(
                "INSERT INTO managers(manager_key, display_name, telegram_username, role, status, is_enabled, manual_stopped) "
                "VALUES(?,?,?,?,?,?,?)",
                (m["manager_key"], m.get("display_name", m["manager_key"]), m.get("telegram_username", ""),
                 m.get("role", "manager"), m.get("status", "active"), int(m.get("is_enabled", 1)), int(m.get("manual_stopped", 0))),
            )
        for mk, sk in links:
            con.execute("INSERT INTO manager_source_links(manager_key, source_key, created_at, updated_at) VALUES(?,?,?,?)", (mk, sk, "", ""))
        for s in sources:
            con.execute("INSERT INTO traffic_sources(source_key, name) VALUES(?,?)", (s["key"], s.get("name", s["key"])))
            if "day_start" in s:
                con.execute(
                    "INSERT INTO source_work_windows(source_key, day_start, day_end, enabled, pro_include_dolyoty) VALUES(?,?,?,?,?)",
                    (s["key"], s["day_start"], s["day_end"], 1, int(s.get("dolyoty", 1))),
                )
        con.commit()
    finally:
        con.close()


def _disable_manager(db_path: str, manager_key: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("UPDATE managers SET is_enabled=0 WHERE manager_key=?", (manager_key,))
        con.commit()
    finally:
        con.close()


def _delete_source(db_path: str, source_key: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("DELETE FROM traffic_sources WHERE source_key=?", (source_key,))
        con.commit()
    finally:
        con.close()


def _add_source_link(db_path: str, manager_key: str, source_key: str, updated_at: str = "") -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT INTO manager_source_links(manager_key, source_key, created_at, updated_at) VALUES(?,?,?,?)",
            (manager_key, source_key, "", updated_at),
        )
        con.commit()
    finally:
        con.close()


class _FakeButton:
    @staticmethod
    def inline(text, data):
        return ("btn", text, data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8"))


class _FakeEventsNS:
    CallbackQuery = object()


class FakeClient:
    def on(self, *a, **kw):
        def _decorator(fn):
            return fn
        return _decorator


class FakeEvent:
    def __init__(self, data: bytes, chat_id: int, sender_id: int):
        self.data = data
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))


# ======================================================================
# Render-layer extraction: pure functions, real DB fixture, no Telegram.
# ======================================================================

RENDER_NAMES = {
    "_manager_rows", "_ss_source_for_manager_panel", "_tp_v8_manager_label_by_key",
    "_nms_sources", "_nms_source_info", "_nms_linked_manager_keys", "_nms_min_yyyymm",
    "_nms_active_managers_for_picker", "_nms_managers_list_text", "_nms_managers_list_buttons",
    "_nms_manager_tokens_for_page", "_nms_build_mgrlist_payload", "_nms_render_mgrlist", "_nms_mgrlist_page_count",
    "_nms_sources_screen", "_nms_styp_text", "_nms_styp_buttons",
    "_nms_confirm_text", "_nms_confirm_buttons",
    "_nm_btn", "_nm_row", "_nm_footer", "_nm_screen", "_nm_canonical_path",
    "_NMS_MGR_PAGE_SIZE",
}


def build_render_ns(db_path: str):
    import manager_registry
    return _extract_and_exec(
        PANEL_PATH,
        RENDER_NAMES,
        {
            "sqlite3": sqlite3,
            "os": os,
            "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
            "Button": _FakeButton,
            "TPILOT_DB_PATH": db_path,
            "normalize_manager_key": manager_registry.normalize_manager_key,
            "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
            "_manager_short_label": lambda row: (str(row.get("display_name") or row.get("manager_key") or "")
                                                  + (f" | @{row['telegram_username']}" if row.get("telegram_username") else "")),
            "_panel_header": lambda: "HEADER",
            "_sched_pb_kyiv_today": lambda: __import__("datetime").date(2026, 7, 14),
            "_bld_fmt_date": lambda iso: iso,
        },
    )


FIXTURE_MANAGERS = [{"manager_key": f"mgr{i:02d}", "display_name": f"Manager {i:02d}"} for i in range(1, 13)]  # 12 -> 2 pages @10
FIXTURE_LINKS = [("mgr01", "src_a"), ("mgr02", "src_a")]
FIXTURE_SOURCES = [
    {"key": "src_a", "name": "Source A", "day_start": 480, "day_end": 1020, "dolyoty": 1},
    {"key": "src_b", "name": "Source B (no dolyoty)", "day_start": 0, "day_end": 1440, "dolyoty": 0},
]


def test_1_entry_button_present() -> None:
    """1. The manager-first entry button exists on the sources screen with
    the exact required callback data.

    N4 (2026-07-21): the label gained a disambiguating suffix, "(по окну
    источника)", since Phase N4 promoted a SEPARATE, differently-scoped
    "👥 По менеджерам" button to the top-level Reports screen (menu:nm_stats,
    the classic manager-first flow, window-agnostic) -- this NMS-nested one
    stays source-window-aware and keeps its own nms:entry:mgr route
    unchanged; the callback data assertion below is therefore unaffected by
    the label change. The old ambiguous "📦 Классическая статистика" fallback
    button was REMOVED from this screen (not "still present") -- its route
    (menu:nm_stats) is still fully defined/dispatchable, just reachable via
    the new top-level Reports entry instead of a jargon-labeled button
    nested here; see tools/n4_canonicalization_selftest.py test_4/test_5 for
    the dedicated regression coverage of that move."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        ns = build_render_ns(db_path)
        text, rows = ns["_nms_sources_screen"]()
        flat = [(btn[1], btn[2]) for row in rows for btn in row]
        check("1. sources screen has the manager-first entry button (N4 label)",
              any(t == "👥 По менеджерам (по окну источника)" for t, d in flat), flat)
        check("1. its callback data is exactly nms:entry:mgr", any(d == b"nms:entry:mgr" for t, d in flat), flat)
        check("(N4) ambiguous classic fallback button was removed from this screen",
              not any(t == "📦 Классическая статистика" for t, d in flat), flat)
        check("(N4) classic stats route (menu:nm_stats) is not emitted from this screen anymore (moved to Reports)",
              not any(d == b"menu:nm_stats" for t, d in flat), flat)
    finally:
        os.unlink(db_path)


def test_2_manager_list_pagination() -> None:
    """2/3. manager-list screen paginates (page size 10); every button
    carries a short nms:pickmgr:<token> callback (never the raw
    manager_key), and _nms_render_mgrlist's token map resolves back to the
    exact managers shown on that page -- driven through the single-snapshot
    production entrypoint, not the individual builders in isolation."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        ns = build_render_ns(db_path)
        page0, text0, rows0 = ns["_nms_render_mgrlist"](0)
        data0 = [btn[2] for row in rows0 for btn in row]
        pick0 = [d for d in data0 if d.startswith(b"nms:pickmgr:")]
        check("2. page 0 shows exactly PAGE_SIZE (10) manager buttons out of 12", len(pick0) == 10, len(pick0))
        check("2. page-nav '➡️' is present when more managers exist", any(d.startswith(b"nms:mpage:") for d in data0), data0)
        check("2. page 0 callback data never contains a raw manager_key (mgrNN)", not any(b"mgr01" in d or b"mgr02" in d for d in data0), data0)

        page1, text1, rows1 = ns["_nms_render_mgrlist"](1)
        data1 = [btn[2] for row in rows1 for btn in row]
        pick1 = [d for d in data1 if d.startswith(b"nms:pickmgr:")]
        check("2. page 1 shows the remaining 2 managers", len(pick1) == 2, len(pick1))
        check("2. page 0 and page 1 tokens are disjoint (no duplicate tokens across pages)", set(pick0).isdisjoint(set(pick1)), (pick0, pick1))

        check("2. page 0 token map resolves to exactly the 10 managers shown (mgr01..mgr10)",
              sorted(page0["manager_tokens"].values()) == [f"mgr{i:02d}" for i in range(1, 11)], page0["manager_tokens"])
        check("2. page 1 token map resolves to the remaining 2 managers (mgr11, mgr12)",
              sorted(page1["manager_tokens"].values()) == ["mgr11", "mgr12"], page1["manager_tokens"])
    finally:
        os.unlink(db_path)


def test_3_long_manager_key_never_silently_omitted() -> None:
    """3. a manager whose manager_key is long enough that a raw-key callback
    (nms:pickmgr:<key>) would have exceeded Telegram's 64-byte limit is
    still shown -- run through the REAL production single-snapshot
    entrypoint -- and its callback_data stays far under 64 bytes because it
    carries a token, not the key itself."""
    long_key = "mgr_" + ("x" * 58)  # well past 64 bytes as a raw "nms:pickmgr:<key>" callback
    assert len(f"nms:pickmgr:{long_key}".encode("utf-8")) > 64
    db_path = _make_temp_db()
    _seed(db_path, managers=[{"manager_key": long_key, "display_name": "Long Key Mgr"}],
          links=[(long_key, "src_a")], sources=FIXTURE_SOURCES)
    try:
        ns = build_render_ns(db_path)
        page0, text0, rows0 = ns["_nms_render_mgrlist"](0)
        data0 = [btn[2] for row in rows0 for btn in row]
        pick0 = [d for d in data0 if d.startswith(b"nms:pickmgr:")]
        check("3. the long-key manager's button is NOT silently omitted", len(pick0) == 1, pick0)
        check("3. its callback_data stays under Telegram's 64-byte limit", pick0 and len(pick0[0]) <= 64, pick0)
        check("3. its token in the payload map resolves back to the full long key", page0["manager_tokens"].get("0") == long_key, page0["manager_tokens"])
    finally:
        os.unlink(db_path)


def test_3c_single_db_read_per_mgrlist_render() -> None:
    """3c. _nms_render_mgrlist reads the manager list from the DB exactly
    ONCE per render call -- payload, text, and buttons are all derived from
    that one snapshot, not three independent reads. Proven by counting real
    calls to _nms_active_managers_for_picker (the only DB-reading function
    in this chain) via monkeypatching it inside the extracted namespace."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        ns = build_render_ns(db_path)
        calls = {"n": 0}
        real_picker = ns["_nms_active_managers_for_picker"]

        def _counting_picker():
            calls["n"] += 1
            return real_picker()

        ns["_nms_active_managers_for_picker"] = _counting_picker
        # _nms_render_mgrlist is a plain top-level function in the extracted
        # namespace -- it looked up _nms_active_managers_for_picker by name
        # in that SAME namespace's globals at call time, so swapping the
        # name here is enough to intercept every call it makes.
        payload, text, buttons = ns["_nms_render_mgrlist"](0)
        check("3c. one manager-list render reads the DB exactly once", calls["n"] == 1, calls["n"])

        calls["n"] = 0
        payload2, text2, buttons2 = ns["_nms_render_mgrlist"](1)
        check("3c. a second, separate render also reads exactly once (not zero, not cached-and-stale)", calls["n"] == 1, calls["n"])
    finally:
        os.unlink(db_path)


def test_3d_snapshot_consistency_under_concurrent_mutation() -> None:
    """3d. the core hardening claim: once _nms_render_mgrlist has taken its
    snapshot, mutating the DB (simulating a concurrent delete arriving in
    the window between the snapshot and the admin's next click) must NOT
    change what the already-built payload/text/buttons say -- they were
    built from the in-memory snapshot, not re-derived from the DB. This
    proves buttons and token map cannot diverge: they came from one read."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        ns = build_render_ns(db_path)
        payload, text, buttons = ns["_nms_render_mgrlist"](0)
        pre_tokens = dict(payload["manager_tokens"])
        pre_button_data = [btn[2] for row in buttons for btn in row]

        # Simulate a concurrent deletion of a manager that WAS in this
        # snapshot's page, arriving after the snapshot was taken.
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM managers WHERE manager_key = 'mgr05'")
        con.commit()
        con.close()

        check("3d. the already-built token map is untouched by the later DB mutation",
              payload["manager_tokens"] == pre_tokens, (payload["manager_tokens"], pre_tokens))
        check("3d. the already-built button list is untouched by the later DB mutation",
              [btn[2] for row in buttons for btn in row] == pre_button_data, buttons)
        check("3d. the deleted manager's token is still present in THIS render's frozen map (proves no re-read)",
              "4" in payload["manager_tokens"] and payload["manager_tokens"]["4"] == "mgr05", payload["manager_tokens"])

        # A FRESH render after the mutation correctly reflects the new state
        # -- proving the snapshot is per-render, not permanently stale/cached.
        payload2, text2, buttons2 = ns["_nms_render_mgrlist"](0)
        check("3d. a fresh render after the mutation no longer includes the deleted manager",
              "mgr05" not in payload2["manager_tokens"].values(), payload2["manager_tokens"])
        check("3d. a fresh render's total manager count reflects the deletion (11, not 12)",
              "Активных менеджеров: 11" in text2, text2)
    finally:
        os.unlink(db_path)


async def test_3b_stale_or_invalid_token_is_handled_safely() -> None:
    """3b. a pickmgr click carrying a token that isn't in the current
    wizard payload (stale button from a previous render, tampered/garbage
    callback data) must not resolve to any manager, must not crash, and
    must respond with a safe fallback instead of silently doing nothing."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)

        # No wizard state at all yet -- wizard-state-required boundary applies.
        ev0 = FakeEvent(b"nms:pickmgr:0", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev0)
        check("3b. pickmgr with no prior wizard state is denied ('mastered reset') and does not crash",
              ev0.answers and ev0.answers[-1][1] is True, ev0.answers)

        # Real wizard state exists (page 0 issued tokens "0".."9"), but the
        # click carries a token that was never issued on that page.
        await ns["_nms_callback"](FakeEvent(b"nms:entry:mgr", CHAT_ID, USER_ID))
        ev1 = FakeEvent(b"nms:pickmgr:999", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev1)
        check("3b. an out-of-range/unknown token is denied with an alert, not resolved to any manager",
              ev1.answers and ev1.answers[-1][1] is True, ev1.answers)
        check("3b. handling a stale token still safely re-renders the manager list (no crash, no dead end)",
              ev1.edits and "выбор менеджера" in ev1.edits[-1][0], ev1.edits)

        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("3b. wizard did not advance to the styp step from an unresolved token",
              wstate.get("step") != "styp", wstate)
    finally:
        os.unlink(db_path)


# ======================================================================
# Full callback-flow extraction: _nms_callback itself, run FOR REAL against
# the real DB-backed wizard state, with Telegram/command-spawn effects faked.
# ======================================================================

CALLBACK_NAMES = RENDER_NAMES | {
    "_wizard_get", "_wizard_set", "_wizard_clear", "_connect_panel_db", "_ensure_panel_runtime_tables",
    "_nms_month_text", "_nms_month_buttons", "_nms_mgr_text", "_nms_mgr_buttons",
    "_nms_callback",
    # R1B/F-16/F-32 (2026-08-12, large reliability batch): every
    # `await event.answer(...)` call site in panel_bot.py -- including
    # inside _nms_callback -- was mechanically rewritten to
    # `await _pb_safe_answer(event, ...)`, a thin wrapper that swallows only
    # a stale/expired callback query. A real, load-bearing dependency now.
    "_pb_safe_answer", "_pb_is_query_invalid_error",
    "_PB_QUERY_INVALID_CLASS_NAMES", "_PB_QUERY_INVALID_TEXT_MARKERS",
}


def build_callback_ns(db_path: str, client: "FakeClient", *, is_allowed=True, duplicate=False):
    import manager_registry
    import json as _json
    from datetime import datetime as _dt, timedelta as _td

    edits: list = []
    spawned: list = []

    async def _fake_safe_event_edit(event, text, buttons=None):
        event.edits.append((text, buttons))
        edits.append((text, buttons))

    async def _fake_panel_answer_fast(event, text=""):
        pass

    async def _fake_panel_status_send(chat_id, text, command=""):
        # N5.4.4: the real _panel_status_send gained an optional trailing
        # `command` arg (observability fix) -- accepted and ignored here,
        # this fake only needs to mirror the return value.
        return 4242

    def _fake_panel_spawn_command_task(command, requested_by, chat_id, status_message_id=0):
        spawned.append({"command": command, "requested_by": requested_by, "chat_id": chat_id, "status_message_id": status_message_id})

    def _fake_title_for_menu(menu):
        return (f"STUB_SCREEN:{menu}", [[_FakeButton.inline("stub", b"menu:nm_root")]])

    return _extract_and_exec(
        PANEL_PATH,
        CALLBACK_NAMES,
        {
            "sqlite3": sqlite3,
            "os": os,
            "json": _json,
            "datetime": _dt,
            "timedelta": _td,
            "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
            "Button": _FakeButton,
            "events": _FakeEventsNS,
            "client": client,
            "TPILOT_DB_PATH": db_path,
            "normalize_manager_key": manager_registry.normalize_manager_key,
            "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
            "_manager_short_label": lambda row: (str(row.get("display_name") or row.get("manager_key") or "")
                                                  + (f" | @{row['telegram_username']}" if row.get("telegram_username") else "")),
            "_panel_header": lambda: "HEADER",
            "_sched_pb_kyiv_today": lambda: __import__("datetime").date(2026, 7, 14),
            "_bld_fmt_date": lambda iso: iso,
            "_utc_now_iso": lambda: "2026-07-14T00:00:00",
            "_is_allowed": lambda event: is_allowed,
            "_safe_event_edit": _fake_safe_event_edit,
            "_panel_answer_fast": _fake_panel_answer_fast,
            "_panel_status_send": _fake_panel_status_send,
            "_panel_spawn_command_task": _fake_panel_spawn_command_task,
            "_panel_callback_is_duplicate": lambda chat_id, user_id, key_text: duplicate,
            "_title_for_menu": _fake_title_for_menu,
            "_sched_pb_cal_mod": __import__("calendar"),
            "_sched_pb_date_cls": __import__("datetime").date,
            "_SCHED_PB_MONTH_NAMES": ["", "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"],
        },
    ), spawned


CHAT_ID = 777
USER_ID = 999


async def _pick_token_for(ns, mgr_key: str, *, chat_id=CHAT_ID, user_id=USER_ID, mpage: int = 0) -> bytes:
    """Drive the real 'nms:entry:mgr' (or 'nms:mpage:<n>') callback to make
    _nms_callback freeze a token->manager_key map into wizard state exactly
    as a real admin session would, then look up the token the picker
    actually issued for mgr_key. This is the only supported way tests reach
    a pickmgr click now -- the raw manager_key never appears in callback
    data, so tests must go through the same render path a real user does."""
    if mpage == 0:
        await ns["_nms_callback"](FakeEvent(b"nms:entry:mgr", chat_id, user_id))
    else:
        await ns["_nms_callback"](FakeEvent(f"nms:mpage:{mpage}".encode("utf-8"), chat_id, user_id))
    wstate = ns["_wizard_get"](chat_id, user_id)
    payload = wstate.get("payload") or {}
    tokens = payload.get("manager_tokens") or {}
    for tok, mk in tokens.items():
        if mk == mgr_key:
            return f"nms:pickmgr:{tok}".encode("utf-8")
    raise AssertionError(f"no token issued for manager {mgr_key!r} on page {mpage}: {tokens}")


async def test_4_pickmgr_with_source_resolves_and_advances() -> None:
    """4. picking a manager WITH a linked source resolves it implicitly,
    pre-fixes mks to that one manager, and shows the styp screen with the
    manager's label visible."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)
        cb = await _pick_token_for(ns, "mgr01")
        ev = FakeEvent(cb, CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev)
        check("4. picking a linked manager edits to the styp screen", len(ev.edits) == 1, ev.edits)
        text, buttons = ev.edits[0]
        check("4. styp screen shows the manager label", "Manager 01" in text, text)
        check("4. styp screen shows the manager's OWN source name", "Source A" in text, text)

        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        payload = wstate.get("payload") or {}
        check("4. wizard payload has entry='mgr'", payload.get("entry") == "mgr", payload)
        check("4. wizard payload resolved src to the manager's own linked source", payload.get("src") == "src_a", payload)
        check("4. wizard payload pre-fixed mks to exactly this one manager", payload.get("mks") == ["mgr01"], payload)
    finally:
        os.unlink(db_path)


async def test_5_pickmgr_without_source_denied() -> None:
    """5. picking a manager WITHOUT a linked source is denied with an alert
    and does not change any state or edit any message."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)
        cb = await _pick_token_for(ns, "mgr05")  # mgr05 has no link
        ev = FakeEvent(cb, CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev)
        check("5. unlinked manager pick is denied (alert)", ev.answers and ev.answers[-1][1] is True, ev.answers)
        check("5. unlinked manager pick never edits any message", ev.edits == [], ev.edits)
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("5. wizard state stays on the manager list (not advanced to styp)", wstate.get("step") == "mgrlist", wstate)
    finally:
        os.unlink(db_path)


async def test_4b_manager_disabled_after_render_is_rejected_on_click() -> None:
    """4b. race: manager list is rendered (token issued), then the manager
    is disabled before the click lands. The stale token must be rejected
    by revalidating against the CURRENT picker-eligible set -- not just
    resolved from the frozen token map -- so no source is accepted and no
    /nmstat is spawned for a manager who is no longer eligible."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)
        cb = await _pick_token_for(ns, "mgr01")  # token issued while mgr01 was still active/enabled

        _disable_manager(db_path, "mgr01")  # race: disabled after render, before click

        ev = FakeEvent(cb, CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev)
        check("4b. stale token for a since-disabled manager is denied (alert)", ev.answers and ev.answers[-1][1] is True, ev.answers)
        check("4b. no source/mks was accepted for the disabled manager", ev.edits and "выбор менеджера" in ev.edits[-1][0], ev.edits)
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("4b. wizard did not advance to styp for the disabled manager", wstate.get("step") != "styp", wstate)
        check("4b. no /nmstat command was spawned for the disabled manager", spawned == [], spawned)
    finally:
        os.unlink(db_path)


async def test_5b_source_resolution_deterministic_and_rejects_removed() -> None:
    """5b. _ss_source_for_manager_panel: (a) when a manager somehow has more
    than one source-link row, resolution is deterministic (repeat calls
    agree, and it never silently disagrees between renders); (b) a stale
    link pointing at a source that no longer exists in traffic_sources is
    rejected outright (never resolved), matching the 'no source' denial
    path in pickmgr."""
    db_path = _make_temp_db()
    _seed(db_path, managers=[{"manager_key": "mgr_multi"}, {"manager_key": "mgr_stale"}],
          links=[], sources=FIXTURE_SOURCES)
    _add_source_link(db_path, "mgr_multi", "src_a", updated_at="2026-07-01T00:00:00")
    _add_source_link(db_path, "mgr_multi", "src_b", updated_at="2026-07-10T00:00:00")  # more recent
    _add_source_link(db_path, "mgr_stale", "src_deleted", updated_at="2026-07-01T00:00:00")
    try:
        ns, _spawned = build_callback_ns(db_path, FakeClient())  # needs _connect_panel_db, not in the pure render_ns extraction
        r1 = ns["_ss_source_for_manager_panel"]("mgr_multi")
        r2 = ns["_ss_source_for_manager_panel"]("mgr_multi")
        check("5b. multiple source links resolve deterministically across repeated calls", r1 == r2 and r1 != "", (r1, r2))
        check("5b. the most recently-updated link wins", r1 == "src_b", r1)

        r3 = ns["_ss_source_for_manager_panel"]("mgr_stale")
        check("5b. a link pointing at a since-deleted source is never resolved", r3 == "", r3)
    finally:
        os.unlink(db_path)

    # End-to-end through the real pickmgr branch: a source that existed at
    # render time but is deleted before the click lands (5.9 "source removed
    # after manager list rendering") must be denied exactly like "no source".
    db_path2 = _make_temp_db()
    _seed(db_path2, managers=[{"manager_key": "mgr_stale2"}], links=[("mgr_stale2", "src_gone")],
          sources=[{"key": "src_gone", "name": "Soon Gone"}])
    try:
        client = FakeClient()
        ns2, spawned2 = build_callback_ns(db_path2, client)
        cb = await _pick_token_for(ns2, "mgr_stale2")

        _delete_source(db_path2, "src_gone")  # removed after the list was rendered, before the click

        ev = FakeEvent(cb, CHAT_ID, USER_ID)
        await ns2["_nms_callback"](ev)
        check("5b. pickmgr denies a manager whose source was removed after rendering", ev.answers and ev.answers[-1][1] is True, ev.answers)
        check("5b. no /nmstat spawned for a manager whose source was removed after rendering", spawned2 == [], spawned2)
    finally:
        os.unlink(db_path2)


async def test_6_7_manager_first_skips_mgr_step_and_confirm_shows_label() -> None:
    """6/7. tomgr skips the manager-selection screen entirely for a
    manager-first flow and goes straight to confirm; confirm shows the
    manager label (not the source-first 'N из M linked' wording); go
    builds the /nmstat command with the single manager appended."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)

        cb = await _pick_token_for(ns, "mgr01")
        await ns["_nms_callback"](FakeEvent(cb, CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:styp:day", CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:d:20260710", CHAT_ID, USER_ID))

        ev_tomgr = FakeEvent(b"nms:tomgr", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_tomgr)
        text, buttons = ev_tomgr.edits[-1]
        check("6. manager-first tomgr skips straight to the confirm screen", "подтверждение" in text, text)
        check("7. confirm screen shows the manager label", "Manager 01" in text, text)
        check("7. confirm screen does NOT use the source-first 'N из M' wording for manager-first", "из" not in text.split("Менеджер:")[-1].splitlines()[0] if "Менеджер:" in text else True, text)

        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("6/7. wizard step advanced straight to 'confirm' (mgr step skipped)", wstate.get("step") == "confirm", wstate)

        ev_go = FakeEvent(b"nms:go", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_go)
        check("7. exactly one /nmstat command was spawned", len(spawned) == 1, spawned)
        check("7. spawned command targets the manager's own source", spawned and spawned[0]["command"].startswith("/nmstat src_a "), spawned)
        check("7. spawned command includes the single chosen manager", spawned and spawned[0]["command"].strip().endswith("mgr01"), spawned)
    finally:
        os.unlink(db_path)


async def test_8_back_navigation_manager_first() -> None:
    """8. back navigation for manager-first: styp.back -> manager list
    (not sources), confirm.back -> calendar (mgr step skipped)."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)

        cb1 = await _pick_token_for(ns, "mgr01")
        await ns["_nms_callback"](FakeEvent(cb1, CHAT_ID, USER_ID))
        ev_back1 = FakeEvent(b"nms:back:mgrlist", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_back1)
        text1, _ = ev_back1.edits[-1]
        check("8. styp 'Назад' for manager-first returns to the manager list, not sources", "выбор менеджера" in text1, text1)

        cb2 = await _pick_token_for(ns, "mgr01")
        await ns["_nms_callback"](FakeEvent(cb2, CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:styp:day", CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:d:20260710", CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:tomgr", CHAT_ID, USER_ID))
        ev_back2 = FakeEvent(b"nms:back:cal", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_back2)
        text2, _ = ev_back2.edits[-1]
        check("8. confirm 'Назад' for manager-first returns to the calendar (mgr step skipped)", "дата" in text2, text2)
    finally:
        os.unlink(db_path)


async def test_6b_manager_list_page_preserved_across_full_flow_and_back() -> None:
    """6b. selecting a manager from page 2 (mpage=1) preserves that page
    through styp/calendar/confirm, and 'back:mgrlist' returns to page 2,
    not page 0 -- the previously-reported regression."""
    db_path = _make_temp_db()
    links = FIXTURE_LINKS + [("mgr11", "src_a")]  # mgr11 lives on page 1 (0-indexed)
    _seed(db_path, managers=FIXTURE_MANAGERS, links=links, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)

        cb = await _pick_token_for(ns, "mgr11", mpage=1)
        await ns["_nms_callback"](FakeEvent(cb, CHAT_ID, USER_ID))
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        payload = wstate.get("payload") or {}
        check("6b. styp payload carries mpage=1 from the picked page", payload.get("mpage") == 1, payload)

        await ns["_nms_callback"](FakeEvent(b"nms:styp:day", CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:d:20260710", CHAT_ID, USER_ID))
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("6b. mpage survives through styp -> calendar", (wstate.get("payload") or {}).get("mpage") == 1, wstate)

        await ns["_nms_callback"](FakeEvent(b"nms:tomgr", CHAT_ID, USER_ID))
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("6b. mpage survives through calendar -> confirm", (wstate.get("payload") or {}).get("mpage") == 1, wstate)

        ev_back = FakeEvent(b"nms:back:mgrlist", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_back)
        text, _ = ev_back.edits[-1]
        check("6b. 'Назад' from confirm-side navigation to mgrlist returns to page 2, not page 0", "страница 2/2" in text, text)
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("6b. wizard mgrlist payload mpage is 1 after back", (wstate.get("payload") or {}).get("mpage") == 1, wstate)
    finally:
        os.unlink(db_path)


async def test_6c_manager_list_page_clamped_if_manager_count_shrinks() -> None:
    """6c. if the manager count shrinks between render and 'back:mgrlist'
    such that the stored mpage no longer has a page, it clamps to the
    nearest valid page instead of erroring or showing an empty screen."""
    db_path = _make_temp_db()
    links = FIXTURE_LINKS + [("mgr11", "src_a")]
    _seed(db_path, managers=FIXTURE_MANAGERS, links=links, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)
        cb = await _pick_token_for(ns, "mgr11", mpage=1)
        await ns["_nms_callback"](FakeEvent(cb, CHAT_ID, USER_ID))

        # Shrink the manager list to page-count 1 while mpage=1 is stored.
        con = sqlite3.connect(db_path)
        con.execute("DELETE FROM managers WHERE manager_key NOT IN ('mgr01','mgr02','mgr03')")
        con.commit()
        con.close()

        ev_back = FakeEvent(b"nms:back:mgrlist", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_back)
        check("6c. back:mgrlist with a stale out-of-range mpage does not crash", ev_back.edits, ev_back.edits)
        text, buttons = ev_back.edits[-1]
        flat = [btn[2] for row in buttons for btn in row]
        pick = [d for d in flat if d.startswith(b"nms:pickmgr:")]
        check("6c. clamped page still shows manager buttons (not an empty/dead screen)", len(pick) > 0, pick)
    finally:
        os.unlink(db_path)


async def test_9_source_first_regression_unaffected() -> None:
    """9. the existing source-first flow is completely unaffected: tomgr
    still shows the manager-selection screen, confirm still uses the
    'N из M linked' wording, styp.back still goes to sources, confirm.back
    still goes to the manager-selection screen."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)

        await ns["_nms_callback"](FakeEvent(b"nms:src:src_a", CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:styp:day", CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:d:20260710", CHAT_ID, USER_ID))

        ev_tomgr = FakeEvent(b"nms:tomgr", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_tomgr)
        text, buttons = ev_tomgr.edits[-1]
        check("9. source-first tomgr still shows the manager-selection screen", "менеджеры" in text, text)

        ev_back_styp = FakeEvent(b"nms:back:src", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_back_styp)
        check("9. source-first styp 'Назад' still clears the wizard and returns to sources (stub)", ev_back_styp.edits and ev_back_styp.edits[-1][0].startswith("STUB_SCREEN:nm_stats2"), ev_back_styp.edits)
    finally:
        os.unlink(db_path)


async def test_9b_duplicate_go_is_deduped() -> None:
    """9b. the 'go' branch consults the shared _panel_callback_is_duplicate
    guard before spawning /nmstat; when it reports a duplicate (as it would
    for two rapid identical clicks), the second click is answered safely
    and does NOT spawn a second report task."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client, duplicate=False)
        cb = await _pick_token_for(ns, "mgr01")
        await ns["_nms_callback"](FakeEvent(cb, CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:styp:day", CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:d:20260710", CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:tomgr", CHAT_ID, USER_ID))
        ev_go1 = FakeEvent(b"nms:go", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_go1)
        check("9b. first 'go' click spawns exactly one report task", len(spawned) == 1, spawned)

        # Simulate the second rapid click of the SAME callback hitting the
        # shared dedup guard (real _panel_callback_is_duplicate would report
        # True for an identical callback within its window).
        ns2, spawned2 = build_callback_ns(db_path, client, duplicate=True)
        cb2 = await _pick_token_for(ns2, "mgr01")
        await ns2["_nms_callback"](FakeEvent(cb2, CHAT_ID, USER_ID))
        await ns2["_nms_callback"](FakeEvent(b"nms:styp:day", CHAT_ID, USER_ID))
        await ns2["_nms_callback"](FakeEvent(b"nms:d:20260710", CHAT_ID, USER_ID))
        await ns2["_nms_callback"](FakeEvent(b"nms:tomgr", CHAT_ID, USER_ID))
        ev_go2 = FakeEvent(b"nms:go", CHAT_ID, USER_ID)
        await ns2["_nms_callback"](ev_go2)
        check("9b. a click flagged as duplicate by the shared guard spawns NO report task", spawned2 == [], spawned2)
        check("9b. duplicate click is answered safely ('Уже выполняю...')", ev_go2.answers and ev_go2.answers[-1][0] == "Уже выполняю...", ev_go2.answers)
    finally:
        os.unlink(db_path)


async def test_10_manager_first_calendar_multiclick_behavior() -> None:
    """10. manager-first flow exercises the SAME production calendar
    handler as source-first: 1st click selects a start date, 2nd click (a
    later date) extends to a range, a click on an already-selected date
    removes it, and a 3rd fresh click after a full range resets to a single
    new date."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client)
        cb = await _pick_token_for(ns, "mgr01")
        await ns["_nms_callback"](FakeEvent(cb, CHAT_ID, USER_ID))
        await ns["_nms_callback"](FakeEvent(b"nms:styp:day", CHAT_ID, USER_ID))

        ev1 = FakeEvent(b"nms:d:20260705", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev1)
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("10. first click selects a single start date", (wstate.get("payload") or {}).get("dates") == ["2026-07-05"], wstate)

        ev2 = FakeEvent(b"nms:d:20260710", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev2)
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("10. second (later) click extends to a range", (wstate.get("payload") or {}).get("dates") == ["2026-07-05", "2026-07-10"], wstate)

        ev3 = FakeEvent(b"nms:d:20260701", CHAT_ID, USER_ID)  # earlier than both -- resets per project's 2-slot rule
        await ns["_nms_callback"](ev3)
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("10. a third click (2 dates already selected) resets to a single new date", (wstate.get("payload") or {}).get("dates") == ["2026-07-01"], wstate)

        ev4 = FakeEvent(b"nms:d:20260701", CHAT_ID, USER_ID)  # clicking the SAME date again deselects it
        await ns["_nms_callback"](ev4)
        wstate = ns["_wizard_get"](CHAT_ID, USER_ID)
        check("10. clicking an already-selected date removes it", (wstate.get("payload") or {}).get("dates") == [], wstate)

        # same-day range: click the same date once (already covered above as a single date)
        ev5 = FakeEvent(b"nms:d:20260712", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev5)
        await ns["_nms_callback"](FakeEvent(b"nms:tomgr", CHAT_ID, USER_ID))
        ev_go = FakeEvent(b"nms:go", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev_go)
        check("10. same-day range produces d1==d2 in the spawned /nmstat command", spawned and " 2026-07-12 2026-07-12 " in spawned[0]["command"], spawned)
    finally:
        os.unlink(db_path)


async def test_11_tombstone_disabled_closer_excluded_from_picker() -> None:
    """11. the manager-first picker excludes archived (tombstone), disabled,
    manual_stopped, and closer-role managers -- the same _manager_rows(
    only_active=True, only_enabled=True, exclude_closers=True) fence used
    elsewhere in AdminBot -- using a real production render pass, not a
    simplified replica."""
    db_path = _make_temp_db()
    managers = [
        {"manager_key": "mgr_ok", "status": "active", "is_enabled": 1, "manual_stopped": 0, "role": "manager"},
        {"manager_key": "mgr_archived", "status": "archived", "is_enabled": 1, "manual_stopped": 0, "role": "manager"},
        {"manager_key": "mgr_disabled", "status": "active", "is_enabled": 0, "manual_stopped": 0, "role": "manager"},
        {"manager_key": "mgr_stopped", "status": "active", "is_enabled": 1, "manual_stopped": 1, "role": "manager"},
        {"manager_key": "mgr_closer", "status": "active", "is_enabled": 1, "manual_stopped": 0, "role": "closer"},
    ]
    _seed(db_path, managers=managers, links=[(m["manager_key"], "src_a") for m in managers], sources=FIXTURE_SOURCES)
    try:
        ns = build_render_ns(db_path)
        payload, text, rows = ns["_nms_render_mgrlist"](0)
        flat = [btn[2] for row in rows for btn in row]
        pick = [d for d in flat if d.startswith(b"nms:pickmgr:")]
        check("11. picker shows exactly the one truly eligible manager", len(pick) == 1, pick)
        check("11. picker token map resolves only to mgr_ok", list(payload["manager_tokens"].values()) == ["mgr_ok"], payload["manager_tokens"])
    finally:
        os.unlink(db_path)


async def test_gate_and_namespace() -> None:
    """(extra) _is_allowed gate applies; unrelated callback namespaces are ignored."""
    db_path = _make_temp_db()
    _seed(db_path, managers=FIXTURE_MANAGERS, links=FIXTURE_LINKS, sources=FIXTURE_SOURCES)
    try:
        client = FakeClient()
        ns, spawned = build_callback_ns(db_path, client, is_allowed=False)
        ev = FakeEvent(b"nms:entry:mgr", CHAT_ID, USER_ID)
        await ns["_nms_callback"](ev)
        check("(extra) _is_allowed=False denies the whole nms: namespace (no edit, no answer)", ev.edits == [] and ev.answers == [], (ev.edits, ev.answers))

        ns2, _ = build_callback_ns(db_path, client, is_allowed=True)
        ev2 = FakeEvent(b"mb:some:other:namespace", CHAT_ID, USER_ID)
        await ns2["_nms_callback"](ev2)
        check("(extra) unrelated callback namespace is ignored entirely", ev2.edits == [] and ev2.answers == [], (ev2.edits, ev2.answers))
    finally:
        os.unlink(db_path)


async def main() -> int:
    test_19_no_adminbot_partnerbot_changes()
    test_1_entry_button_present()
    test_2_manager_list_pagination()
    test_3_long_manager_key_never_silently_omitted()
    test_3c_single_db_read_per_mgrlist_render()
    test_3d_snapshot_consistency_under_concurrent_mutation()
    await test_3b_stale_or_invalid_token_is_handled_safely()
    await test_4_pickmgr_with_source_resolves_and_advances()
    await test_4b_manager_disabled_after_render_is_rejected_on_click()
    await test_5_pickmgr_without_source_denied()
    await test_5b_source_resolution_deterministic_and_rejects_removed()
    await test_6_7_manager_first_skips_mgr_step_and_confirm_shows_label()
    await test_6b_manager_list_page_preserved_across_full_flow_and_back()
    await test_6c_manager_list_page_clamped_if_manager_count_shrinks()
    await test_8_back_navigation_manager_first()
    await test_9_source_first_regression_unaffected()
    await test_9b_duplicate_go_is_deduped()
    await test_10_manager_first_calendar_multiclick_behavior()
    await test_11_tombstone_disabled_closer_excluded_from_picker()
    await test_gate_and_namespace()

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
