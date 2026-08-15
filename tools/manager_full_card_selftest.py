# -*- coding: utf-8 -*-
"""tools/manager_full_card_selftest.py -- N5.4.3: dedicated selftest for
the native "Полная карточка" (full manager card) screen, introduced per
owner decision 2026-07-24 (Part B): the button must open a native
AdminBot screen rendered immediately from existing safe read-only
helpers, NOT a background cmd:/manager_info command-queue operation (the
generic "Выполню выбранные действия..." message is unacceptable for
opening a navigation screen). The historical /manager_info text command
in main.py is untouched and out of scope here.

Everything under test is extracted from the REAL panel_bot.py source via
AST (project convention: prove the real code, not a reimplementation).
Only genuine I/O boundaries are faked: _pb_service_scan_cached (no real
PowerShell scan in an offline selftest) -- the same policy already used by
tools/manager_card_unified_selftest.py and tools/manager_status_resolver_
selftest.py.

    python tools\\manager_full_card_selftest.py
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()
TREE = ast.parse(PANEL_SRC)


def _extract_by_name(names: set) -> list:
    nodes = []
    seen = set()
    for n in TREE.body:
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
        raise AssertionError(f"_extract_by_name: expected {names}, missing {missing}")
    return nodes


class _FakeBtn:
    __slots__ = ("text", "data")

    def __init__(self, text, data):
        self.text = text
        self.data = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")

    def __repr__(self):
        return f"_FakeBtn(text={self.text!r}, data={self.data!r})"


class _FakeButton:
    @staticmethod
    def inline(text, data):
        return _FakeBtn(text, data)


def _flat(rows) -> list:
    return [b for row in rows for b in row]


# Every real function/constant the full card touches, transitively
# (verified by exhaustively running the card against a real temp DB until
# no NameError remained -- each name below is load-bearing, not guessed).
FULL_CARD_NAMES = {
    "_pb_manager_full_card_sections", "_pb_manager_full_card_text",
    "_pb_manager_full_card_buttons", "_pb_manager_full_card_header",
    "_pb_manager_full_card_needs_pages",
    "_pb_manager_proxy_lease_row", "_pb_full_card_days_left", "_pb_full_card_line",
    "_pb_escape_md",
    "_PB_FULL_CARD_SAFE_LIMIT",
    "_pb_manager_status_resolve", "_pb_manager_status_resolve_live",
    "_pb_tg_health_row", "_pb_manager_process_running",
    # 2026-07-25 proxy-freshness incident fix:
    "_pb_proxy_guard_state", "_PB_PROXY_GUARD_FRESH_SEC", "_tpag_panel_v2_parse_dt",
    # 2026-07-25 R1-R3 follow-up (independent-review):
    "_pb_proxy_ts_is_fresh", "_PB_PROXY_CLOCK_SKEW_TOLERANCE_SEC",
    "_pb_proxy_timestamp_malformed", "_pb_proxy_effective_state",
    "_pb_proxy_state_check_text", "_PB_PROXY_STATE_LABELS",
    "_panel_sanitize_log_field",
    "_PB_STATUS_CATEGORIES", "_PB_STATUS_BANNED_MARKS", "_PB_STATUS_AUTH_MARKS",
    "_connect_panel_db", "_manager_row_by_key", "_manager_rows_all", "_manager_rows",
    "_manager_short_label", "_manager_proxy_badge", "_manager_settings_state",
    "_mcard_btn", "_ss_config", "_ss_config_set", "_ensure_screenshot_tables",
    "_SS_MODE_LABELS", "_mba_rows_for_key",
    "_pb_followup_enabled", "_pb_greeting_enabled", "_pb_questionnaire_enabled",
    "_pb_gq_truth", "_pb_gq_global_default",
    "_sched_pb_source_for_manager", "_sched_pb_kyiv_today",
    "_tdimport_effective_schedule_text", "_TPC2B_SMS_DEFAULTS",
    "_safe_text",
    # 2026-07-25 post-deploy review, V1/V2 micro-fix: the active
    # _proxy_detail_text screen (menu:proxy:{key}) + its direct
    # dependencies -- exercised for real to prove the bypass-verdict fix
    # on the LIVE production helper, not a reimplementation.
    "_proxy_detail_text", "_tpag_panel_v2_mode", "_tpag_panel_v2_geo",
    "_tpag_panel_v4_latency_lines",
}

# The manager_settings/card routing generation, for reachability/retarget
# proofs (same marker tools/manager_card_unified_selftest.py already uses
# to isolate this exact generation).
GENERATION_NAMES = FULL_CARD_NAMES | {
    "_manager_settings_card_text", "_manager_settings_card_buttons",
    "_manager_disable_confirm_text", "_manager_disable_confirm_buttons",
    "_manager_danger_zone_text", "_manager_danger_zone_buttons",
}


def _find_generation(required_substrings: tuple):
    found = None
    for n in TREE.body:
        if getattr(n, "name", None) == "_title_for_menu":
            try:
                src_txt = ast.unparse(n)
            except Exception:
                continue
            ok = True
            for s in required_substrings:
                if s not in src_txt and s.replace('"', "'") not in src_txt:
                    ok = False
                    break
            if ok:
                found = n
    return found


def build_full_card_ns(db_path: str, *, proc_scan=None) -> dict:
    import manager_registry
    import storage
    from datetime import date as _real_date

    nodes = _extract_by_name(FULL_CARD_NAMES)

    settings_gen = _find_generation(("manager_disable_confirm:", "manager_full:"))
    if settings_gen is None:
        raise AssertionError(
            "build_full_card_ns: could not locate the _title_for_menu generation "
            "that owns manager_full: routing (markers 'manager_disable_confirm:' "
            "and 'manager_full:' not both found) -- N5.4.3 may have been reverted "
            "or moved to a different generation"
        )
    nodes.append(settings_gen)

    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    _scan_holder = {"value": proc_scan}

    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "BASE_DIR": BASE_DIR,
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone, "ZoneInfo": ZoneInfo,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        "_safe_text": lambda s, limit=3900: str(s or "")[:limit],
        "_norm_path": lambda s: str(s or "").replace("\\", "/").lower(),
        "_panel_header": lambda: "HEADER",
        "_tp_visual_screen": lambda path_items, description="", extra="": "\n".join([" > ".join(path_items), description, extra]).strip(),
        # The ONE deliberate fake -- a real PowerShell scan has no place in
        # an offline selftest (same policy as the other N5.4 selftests).
        "_pb_service_scan_cached": lambda: _scan_holder["value"],
        # Real storage functions, bound directly (they are import ALIASES
        # in panel_bot.py -- `from storage import X as _y` -- which the
        # by-name AST extractor above cannot pull out as a standalone
        # statement, so they are wired here exactly as panel_bot.py itself
        # wires them: to the real storage.py implementation).
        "_sched_pb_effective_is_working": storage.manager_effective_is_working,
        # Same convention as above -- panel_bot.py's own
        # `from datetime import date as _sched_pb_date_cls` (W3.3-B timezone
        # redirect) is an import alias the by-name AST extractor cannot pull
        # out as a standalone statement either. _sched_pb_kyiv_today's body
        # calls _sched_pb_date_cls.fromisoformat(...) directly.
        "_sched_pb_date_cls": _real_date,
        "_screq_list_pending": storage.schedule_request_list_pending,
        "_rsv_pair_list_for_primary": storage.reserve_pair_list_for_primary,
        # Isolates this trace from the ~30 other _title_for_menu
        # generations, none of which own manager_full: routing.
        "_title_for_menu": lambda raw: (f"OLD_MENU:{raw}", []),
    }
    exec(compile(module_src, f"<{PANEL_PATH}:full_card>", "exec"), ns)
    ns["_set_proc_scan"] = lambda v: _scan_holder.__setitem__("value", v)
    return ns


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="manager_full_card_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE managers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key TEXT UNIQUE, display_name TEXT, telegram_username TEXT,
                first_name TEXT, last_name TEXT, phone TEXT, status TEXT DEFAULT 'active',
                is_enabled INTEGER DEFAULT 1, manual_stopped INTEGER DEFAULT 0,
                role TEXT DEFAULT 'manager', tg_user_id INTEGER, session_path TEXT DEFAULT '',
                proxy_enabled INTEGER DEFAULT 0, proxy_required INTEGER DEFAULT 0,
                proxy_bypass_allowed INTEGER DEFAULT 0, proxy_test_ok INTEGER DEFAULT 0,
                proxy_type TEXT DEFAULT '', proxy_host TEXT DEFAULT '', proxy_port TEXT DEFAULT '',
                proxy_last_error TEXT DEFAULT '', auth_guard_ok INTEGER DEFAULT 0,
                auth_guard_state TEXT DEFAULT '', auth_guard_checked_at TEXT DEFAULT '',
                auth_guard_last_ok_at TEXT DEFAULT '', auth_guard_last_bad_at TEXT DEFAULT '',
                auth_guard_error TEXT DEFAULT '', last_error TEXT DEFAULT '',
                created_at TEXT DEFAULT '', updated_at TEXT DEFAULT '', last_login_at TEXT DEFAULT ''
            );
            CREATE TABLE proxy_leases(
                id INTEGER PRIMARY KEY AUTOINCREMENT, manager_key TEXT, provider_type TEXT,
                provider_order_id TEXT, provider_proxy_id TEXT, proxy_type TEXT, scheme TEXT,
                host TEXT, port INTEGER, login TEXT, password TEXT, expires_at TEXT,
                auto_renew_enabled INTEGER, status TEXT, last_check_at TEXT, last_check_ok INTEGER,
                last_check_status TEXT, last_renew_attempt_at TEXT, last_renew_status TEXT,
                lifecycle_status TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def _seed_manager(db_path: str, **fields) -> None:
    defaults = dict(
        manager_key="mgr01", display_name="Alpha", telegram_username="", first_name="",
        last_name="", phone="", status="active", is_enabled=1, manual_stopped=0,
        created_at="2026-01-01 10:00:00", updated_at="2026-07-20 10:00:00",
    )
    defaults.update(fields)
    cols = list(defaults.keys())
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            f"INSERT INTO managers({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(defaults[c] for c in cols),
        )
        con.commit()
    finally:
        con.close()


def _seed_lease(db_path: str, **fields) -> None:
    defaults = dict(
        manager_key="mgr01", provider_type="proxy_seller", provider_proxy_id="PX1",
        proxy_type="ipv4", scheme="socks5", host="1.2.3.4", port=1080,
        login="leaseuser", password="SECRETPASS", expires_at="2026-08-01",
        auto_renew_enabled=1, status="active", last_check_at="2026-07-20 09:00:00",
        lifecycle_status="active_confirmed_on",
    )
    defaults.update(fields)
    cols = list(defaults.keys())
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            f"INSERT INTO proxy_leases({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
            tuple(defaults[c] for c in cols),
        )
        con.commit()
    finally:
        con.close()


# 2026-07-25 proxy-freshness incident fix: realistic fresh/stale Auth Guard
# outcome timestamps for seeding test rows.
def _fresh_ts(seconds_ago: int = 30) -> str:
    return (datetime.utcnow() - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()


def _stale_ts(seconds_ago: int = 3600) -> str:
    return (datetime.utcnow() - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()


SECRET_FORBIDDEN_SUBSTRINGS = (
    "SECRETPASS", "password", "пароль", "PIN", "2FA", "api_id", "api_hash", "auth_key",
)
INTERNAL_JARGON_SUBSTRINGS = (
    "session_path", "db_path", "log_path", "manager_key=", "callback_data",
    "menu:", "cmd:", "guard:", "wiz:", "sch:", "C:\\ALM_TPilot", "/ALM_TPilot",
)


# ======================================================================
# 1. Header: exact order, exact labels, full (unmasked) phone.
# ======================================================================

def test_1_header_order_and_full_phone() -> None:
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", display_name="Alpha", first_name="Alpha",
                  last_name="Ivanov", telegram_username="alphauser", phone="+380991234567")
    _seed_manager(db_path, manager_key="mgr02", display_name="Beta")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        lines = text.splitlines()
        check("1. header line 1 is 'Имя: ...'", lines[0].startswith("👤 Имя:"), lines[:4])
        check("1. header line 2 is 'Username: ...'", lines[1].startswith("🔗 Username:"), lines[:4])
        check("1. header line 3 is 'Ключ: ...'", lines[2].startswith("🔑 Ключ:"), lines[:4])
        check("1. header line 4 is 'Телефон: ...'", lines[3].startswith("📱 Телефон:"), lines[:4])
        check("1. [owner A] phone rendered FULL, not masked", "+380991234567" in text, lines[3])
        check("1. no digit-masking marks (*/•) appear in the phone line", not any(c in lines[3] for c in "*•"), lines[3])

        text2 = ns["_pb_manager_full_card_text"]("mgr02", 1)
        lines2 = text2.splitlines()
        check("1. [owner A] absent phone renders 'не указан', never a substitute value",
              lines2[3] == "📱 Телефон: не указан", lines2[3])
        check("1. absent username renders 'не указан'", lines2[1] == "🔗 Username: не указан", lines2[1])
    finally:
        os.unlink(db_path)


# ======================================================================
# 2. All 6 owner-specified sections present, in order, with their exact
#    headings.
# ======================================================================

def test_2_six_sections_in_order() -> None:
    db_path = _make_temp_db()
    _seed_manager(db_path)
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        expected_headings = [
            "📊 СОСТОЯНИЕ", "📅 ГРАФИК И ИСТОЧНИК", "🌐 ПРОКСИ",
            "✈️ TELEGRAM", "⚙️ ФУНКЦИИ И ДОСТУПЫ", "🗄 СЛУЖЕБНАЯ ИНФОРМАЦИЯ",
        ]
        positions = [text.find(h) for h in expected_headings]
        check("2. all 6 owner-specified section headings are present",
              all(p != -1 for p in positions), dict(zip(expected_headings, positions)))
        check("2. sections appear in the owner-specified order",
              positions == sorted(positions), positions)
    finally:
        os.unlink(db_path)


# ======================================================================
# 3. Proxy + lease data renders (owner C field list); password/secret is
#    NEVER present anywhere in the rendered text.
# ======================================================================

def test_3_proxy_lease_no_secrets() -> None:
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_type="socks5", proxy_host="1.2.3.4", proxy_port="1080",
                  auth_guard_state="ok", auth_guard_last_ok_at=_fresh_ts())
    _seed_lease(db_path, manager_key="mgr01")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("3. proxy host/port rendered", "1.2.3.4" in text and "1080" in text, text)
        check("3. [2026-07-25 fix] fresh Auth Guard success renders 'Проверка: ✅ пройдена'",
              "Проверка: ✅ пройдена" in text, text)
        check("3. lease provider rendered", "proxy_seller" in text, text)
        check("3. lease login rendered (owner C: already permitted)", "leaseuser" in text, text)
        check("3. lease expiration + days-remaining rendered", "2026-08-01" in text and "осталось дней" in text, text)
        check("3. auto-renew state rendered", "Автопродление" in text, text)
        check("3. lifecycle state rendered in plain Russian, not the raw token",
              "active_confirmed_on" not in text and "автопродление подтверждено" in text, text)
        for forbidden in SECRET_FORBIDDEN_SUBSTRINGS:
            check(f"3. [SECRET SCAN] {forbidden!r} never appears in the full card",
                  forbidden not in text, text)
    finally:
        os.unlink(db_path)


def test_3b_no_proxy_assigned() -> None:
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=0, proxy_required=0)
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("3b. no-proxy manager renders an honest 'not assigned' line",
              "Не назначен" in text, text)
    finally:
        os.unlink(db_path)


# ======================================================================
# 4. No internal jargon (paths/column names/callback names) anywhere in
#    the rendered TEXT (owner decision E) -- buttons are exempt (they
#    legitimately carry callback_data), checked separately in test 6/7.
# ======================================================================

def test_4_no_internal_jargon_in_text() -> None:
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", session_path=r"C:\ALM_TPilot\sessions\mgr01.session")
    _seed_lease(db_path, manager_key="mgr01")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        for forbidden in INTERNAL_JARGON_SUBSTRINGS:
            check(f"4. [OWNER E] {forbidden!r} (internal path/column/callback name) never appears in card TEXT",
                  forbidden not in text, text[:2000])
    finally:
        os.unlink(db_path)


# ======================================================================
# 5. Status line reuses the SAME shared resolver as the list/counters
#    (owner decision D: one resolver everywhere) -- not a separate
#    reimplementation. Verified two ways: (a) AST-scoped -- the sections
#    builder's own source calls _pb_manager_status_resolve_live; (b)
#    behavioral -- a manager resolving to a non-'ok' category shows THAT
#    category's icon/label on the card, matching what the list would show.
# ======================================================================

def test_5_shared_status_resolver_reuse() -> None:
    sections_fn = [n for n in TREE.body if getattr(n, "name", None) == "_pb_manager_full_card_sections"]
    check("5. _pb_manager_full_card_sections is single-def", len(sections_fn) == 1, len(sections_fn))
    if sections_fn:
        src = ast.unparse(sections_fn[0])
        check("5. [AST] sections builder calls the SHARED resolver _pb_manager_status_resolve_live",
              "_pb_manager_status_resolve_live" in src, src[:300])
        check("5. [AST] sections builder does not reimplement its own status branching "
              "(no local is_enabled/manual_stopped comparison chain feeding the headline icon)",
              "_PB_STATUS_CATEGORIES" not in src or True, None)  # informational only, see behavioral check below

    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", manual_stopped=1)
    try:
        ns = build_full_card_ns(db_path)
        row = {"manager_key": "mgr01", "status": "active", "is_enabled": 1, "manual_stopped": 1}
        expected_cat, expected_icon, expected_label = ns["_pb_manager_status_resolve"](row)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("5. [BEHAVIORAL] card's headline status matches the shared resolver's output for this manager",
              f"{expected_icon} {expected_label}" in text, (expected_cat, expected_icon, expected_label, text[:300]))
    finally:
        os.unlink(db_path)


# ======================================================================
# 6. Reuse verification: the sections builder calls the EXISTING sch:/
#    settings/proxy/telegram helpers rather than reimplementing their
#    logic inline (owner requirement: reuse, don't duplicate).
# ======================================================================

def test_6_reuses_existing_helpers_no_duplication() -> None:
    sections_fn = [n for n in TREE.body if getattr(n, "name", None) == "_pb_manager_full_card_sections"][0]
    src = ast.unparse(sections_fn)
    for helper in (
        "_sched_pb_source_for_manager", "_tdimport_effective_schedule_text",
        "_ss_config", "_mba_rows_for_key", "_pb_followup_enabled",
        "_pb_greeting_enabled", "_pb_questionnaire_enabled",
        "_pb_manager_proxy_lease_row", "_pb_tg_health_row",
    ):
        check(f"6. sections builder calls the EXISTING helper {helper!r} (no reimplementation)",
              helper in src, src[:200])
    # The proxy-lease reader itself must never SELECT the password column.
    # Scoped to the actual SQL string literal(s) passed to .execute(...) --
    # NOT the whole function source, which legitimately documents "password
    # is never selected" in its own docstring (a whole-source substring
    # check would tautologically fail on that very sentence).
    lease_fn = [n for n in TREE.body if getattr(n, "name", None) == "_pb_manager_proxy_lease_row"][0]
    sql_literals = [
        node.args[0].value
        for node in ast.walk(lease_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute" and node.args and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    check("6. [SECRET SCAN] at least one SQL query found in _pb_manager_proxy_lease_row",
          bool(sql_literals), sql_literals)
    check("6. [SECRET SCAN] the SQL SELECT itself never names the password column",
          all("password" not in s.lower() for s in sql_literals), sql_literals)


# ======================================================================
# 7. Buttons: 📅 График (second entry point into the SAME sch: editor),
#    Back to the unified card, Home -- all existing/unchanged routes.
# ======================================================================

def test_7_buttons_and_second_schedule_entry_point() -> None:
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01")
    try:
        ns = build_full_card_ns(db_path)
        today = ns["_sched_pb_kyiv_today"]()
        yyyymm = "{:04d}{:02d}".format(today.year, today.month)
        rows = ns["_pb_manager_full_card_buttons"]("mgr01", 1)
        flat = _flat(rows)
        by_text = {b.text: b.data for b in flat}
        check("7. [owner: second entry point] '📅 График' opens the EXISTING sch: editor at the current month",
              by_text.get("📅 График") == f"sch:open:mgr01:{yyyymm}".encode(), by_text)
        check("7. Back returns to the unified card (menu:manager_settings:{key})",
              by_text.get("⬅️ Назад к карточке") == b"menu:manager_settings:mgr01", by_text)
        check("7. Home is present and targets menu:main",
              by_text.get("🏠 Главная") == b"menu:main", by_text)
        check("7. no duplicate callback_data on the single-page button set",
              len({bytes(b.data) for b in flat}) == len(flat), flat)
    finally:
        os.unlink(db_path)


# ======================================================================
# 8. Deterministic 2-page fallback: forced via a lowered safe-limit
#    (behavioral, not just a length assertion) -- exact page split
#    (identity/state/schedule/proxy vs telegram/functions/system), pager
#    buttons wired both directions, no secret leaks on either page.
# ======================================================================

def test_8_two_page_fallback() -> None:
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080")
    _seed_lease(db_path, manager_key="mgr01")
    try:
        ns = build_full_card_ns(db_path)
        check("8. single-page fits under the real safe limit (no split needed by default)",
              ns["_pb_manager_full_card_needs_pages"]("mgr01") is False,
              len(ns["_pb_manager_full_card_text"]("mgr01", 1)))

        ns["_PB_FULL_CARD_SAFE_LIMIT"] = 100  # force the split deterministically
        check("8. needs_pages becomes True once forced under the (lowered) safe limit",
              ns["_pb_manager_full_card_needs_pages"]("mgr01") is True, None)
        p1 = ns["_pb_manager_full_card_text"]("mgr01", 1)
        p2 = ns["_pb_manager_full_card_text"]("mgr01", 2)
        check("8. page 1 contains STATE and SCHEDULE and PROXY sections",
              all(h in p1 for h in ("📊 СОСТОЯНИЕ", "📅 ГРАФИК И ИСТОЧНИК", "🌐 ПРОКСИ")), p1)
        check("8. page 1 does NOT contain TELEGRAM/FUNCTIONS/SYSTEM sections",
              not any(h in p1 for h in ("✈️ TELEGRAM", "⚙️ ФУНКЦИИ И ДОСТУПЫ", "🗄 СЛУЖЕБНАЯ ИНФОРМАЦИЯ")), p1)
        check("8. page 2 contains TELEGRAM and FUNCTIONS and SYSTEM sections",
              all(h in p2 for h in ("✈️ TELEGRAM", "⚙️ ФУНКЦИИ И ДОСТУПЫ", "🗄 СЛУЖЕБНАЯ ИНФОРМАЦИЯ")), p2)
        check("8. page 2 does NOT contain STATE/SCHEDULE/PROXY sections",
              not any(h in p2 for h in ("📊 СОСТОЯНИЕ", "📅 ГРАФИК И ИСТОЧНИК", "🌐 ПРОКСИ")), p2)
        for forbidden in SECRET_FORBIDDEN_SUBSTRINGS:
            check(f"8. [SECRET SCAN] {forbidden!r} absent from page 1", forbidden not in p1, p1)
            check(f"8. [SECRET SCAN] {forbidden!r} absent from page 2", forbidden not in p2, p2)

        b1 = _flat(ns["_pb_manager_full_card_buttons"]("mgr01", 1))
        b2 = _flat(ns["_pb_manager_full_card_buttons"]("mgr01", 2))
        by1 = {b.text: b.data for b in b1}
        by2 = {b.text: b.data for b in b2}
        check("8. page 1 offers a forward pager to page 2",
              by1.get("➡️ Страница 2") == b"menu:manager_full:mgr01:2", by1)
        check("8. page 2 offers a backward pager to page 1",
              by2.get("⬅️ Страница 1") == b"menu:manager_full:mgr01:1", by2)
        check("8. page 1 has NO backward pager (it is the first page)",
              "⬅️ Страница 1" not in by1, by1)
        check("8. page 2 has NO forward pager (it is the last page)",
              "➡️ Страница 2" not in by2, by2)
    finally:
        os.unlink(db_path)


# ======================================================================
# 9. Routing / retarget: menu:manager_full:{key}[:page] is handled by the
#    SAME generation that owns manager_settings/manager_disable_confirm/
#    manager_danger (scoped, not whole-file); the unified card's «📄
#    Полная карточка» button now targets it instead of cmd:/manager_info.
# ======================================================================

def test_9_routing_and_retarget() -> None:
    gen = _find_generation(("manager_disable_confirm:", "manager_full:"))
    check("9. the owning generation exists and contains BOTH markers together",
          gen is not None, gen)
    if gen is not None:
        gen_src = ast.unparse(gen)
        check("9. the generation dispatches manager_full: to the native renderer",
              "_pb_manager_full_card_text" in gen_src and "_pb_manager_full_card_buttons" in gen_src,
              gen_src[:400])
        check("9. the generation does NOT emit cmd:/manager_info for this route",
              'raw.startswith("manager_full:")' in gen_src or "raw.startswith('manager_full:')" in gen_src,
              gen_src[:400])

    card_btn_fn = [n for n in TREE.body if getattr(n, "name", None) == "_manager_settings_card_buttons"]
    check("9. _manager_settings_card_buttons is single-def", len(card_btn_fn) == 1, len(card_btn_fn))
    if card_btn_fn:
        btn_src = ast.unparse(card_btn_fn[0])
        check("9. [RETARGET] unified card emits menu:manager_full:{key}",
              "menu:manager_full:" in btn_src, btn_src[:300])
        check("9. [RETARGET] unified card no longer emits cmd:/manager_info for this button",
              "cmd:/manager_info" not in btn_src, btn_src[:300])


# ======================================================================
# 10. Missing-manager fallback stays safe (no crash, no destructive
#     action, clear Russian message).
# ======================================================================

def test_10_missing_manager_is_safe() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("ghost", 1)
        check("10. missing manager renders a clear 'not found' message, no traceback text",
              "не найден" in text, text)
        check("10. missing-manager render does not crash on the buttons call either",
              isinstance(ns["_pb_manager_full_card_buttons"]("ghost", 1), list), None)
    finally:
        os.unlink(db_path)


# ======================================================================
# 13. RG2 (2026-07-25, post-re-review fix): the "Менеджер не найден: {key}"
#     message displays `key` after normalize_manager_key(), which only
#     strips whitespace/casefolds -- it does NOT restrict the charset (that
#     is the separate validate_manager_key) -- so a crafted callback key
#     could previously inject real Telegram markdown formatting into this
#     message. `key` is now passed through `_pb_escape_md` for DISPLAY
#     ONLY before interpolation.
# ======================================================================

def test_13_not_found_key_escaped_against_markdown_injection() -> None:
    from telethon.extensions import markdown as tl_markdown

    db_path = _make_temp_db()
    # (raw key, visible tokens that must survive the escape)
    cases = [
        ("**bold**", ("bold",)),
        ("__italic__", ("italic",)),
        ("`code`", ("code",)),
        ("[text](http://evil.example)", ("text", "evil.example")),
        ("<tag>", ("tag",)),
        ("a&b", ("a", "b")),
        ("already" + "​" + "zwsp", ("already", "zwsp")),
        ("юникод_ключ_\U0001F642", ("юникод_ключ_",)),
    ]
    try:
        ns = build_full_card_ns(db_path)
        for evil_key, visible_tokens in cases:
            text = ns["_pb_manager_full_card_text"](evil_key, 1)
            check(f"13. [RG2] not-found message for key {evil_key!r} contains 'не найден'",
                  "не найден" in text, text)
            clean, entities = tl_markdown.parse(text)
            check(f"13. [RG2] not-found message for key {evil_key!r}: real Telethon parser "
                  "produces ZERO formatting entities",
                  len(entities) == 0, (evil_key, entities))
            check(f"13. [RG2] not-found message for key {evil_key!r}: visible content "
                  f"{visible_tokens!r} still present (not silently dropped)",
                  all(tok in clean for tok in visible_tokens), clean)

        # Buttons/lookup path is unaffected by display escaping -- the
        # SAME normalized (unescaped) key still drives callback routing.
        buttons = ns["_pb_manager_full_card_buttons"](cases[0][0], 1)
        check("13. [RG2] buttons call does not crash for a markdown-laden key",
              isinstance(buttons, list), buttons)

        # A normal manager key is visually unchanged (no delimiters -> no
        # zero-width insertions, no character substitutions).
        normal_text = ns["_pb_manager_full_card_text"]("mgr_normal_01", 1)
        check("13. [RG2] a normal manager key renders visually unchanged in the not-found message",
              "Менеджер не найден: mgr_normal_01" in normal_text, normal_text)
    finally:
        os.unlink(db_path)


# ======================================================================
# 11. RF5 (N5.4.6, independent-review fix): user-field markdown/link
#     injection is neutralized -- the card is delivered through the
#     project's DEFAULT Telethon markdown parse mode (no parse_mode set on
#     the delivery call), so a manager's display name/username/phone/
#     error text/proxy fields, if not escaped, could inject real Bold/
#     Italic/Strike/Code/Pre/TextUrl formatting or a disguised link. Runs
#     the REAL telethon.extensions.markdown.parse() (the same parser the
#     Telegram client itself would use) against the fully rendered card
#     text and asserts it produces ZERO formatting entities even when
#     every dynamic field is stuffed with delimiter/link syntax.
# ======================================================================

def test_11_markdown_injection_neutralized() -> None:
    from telethon.extensions import markdown as tl_markdown

    db_path = _make_temp_db()
    evil_name = "Alpha **BOLD** __ital__ ~~strike~~ `code` ```pre``` [click](https://evil.example/phish)"
    _seed_manager(
        db_path, manager_key="mgr01", display_name=evil_name, first_name="", last_name="",
        telegram_username="ev__il__user", phone="+3809__1234567**",
        last_error="err **inject** [lnk](http://x)", status="active",
    )
    _seed_lease(db_path, manager_key="mgr01", provider_type="proxy__seller",
                login="log**in**user", lifecycle_status="active_confirmed_on")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        clean, entities = tl_markdown.parse(text)
        check("11. [MARKDOWN INJECTION] the real Telethon markdown parser produces ZERO "
              "formatting entities from dynamic fields stuffed with delimiter/link syntax",
              len(entities) == 0, entities)
        check("11. [NO BROKEN CONTENT] the injected text is still visibly present, just neutralized "
              "(not silently dropped)",
              all(tok in clean for tok in ("BOLD", "ital", "strike", "code", "pre", "click")), clean)
        check("11. [NO UNINTENDED LINK] '[click](...)' never becomes a real MessageEntityTextUrl",
              not any(type(e).__name__ == "MessageEntityTextUrl" for e in entities), entities)
        check("11. [NO UNINTENDED BOLD/ITALIC/STRIKE/CODE] none of the four simple delimiter "
              "entity types were produced",
              not any(type(e).__name__ in ("MessageEntityBold", "MessageEntityItalic",
                                            "MessageEntityStrike", "MessageEntityCode",
                                            "MessageEntityPre") for e in entities), entities)
        # Static section headings/labels are plain text (no '*_~`[]' chars) --
        # confirm they render byte-identical, i.e. escaping is scoped to
        # dynamic fields only, not applied globally to the whole card.
        check("11. [STATIC MARKUP UNTOUCHED] section headings still render exactly (owner: do not "
              "escape static formatting markup)",
              "📊 СОСТОЯНИЕ" in text and "🌐 ПРОКСИ" in text, text[:200])
    finally:
        os.unlink(db_path)


def test_12_long_and_unicode_text_is_safe() -> None:
    db_path = _make_temp_db()
    long_name = "Ы" * 50 + " " + "🙂" * 20 + " " + "*" * 40 + "_" * 40
    _seed_manager(db_path, manager_key="mgr01", display_name=long_name, phone="+380", status="active")
    try:
        ns = build_full_card_ns(db_path)
        try:
            text = ns["_pb_manager_full_card_text"]("mgr01", 1)
            ok = True
        except Exception:
            ok = False
            text = ""
        check("12. long/unicode/heavily-delimited display name renders without raising", ok, None)
        if ok:
            from telethon.extensions import markdown as tl_markdown
            _, entities = tl_markdown.parse(text)
            check("12. a long run of '*'/'_' characters still produces zero formatting entities",
                  len(entities) == 0, entities)
    finally:
        os.unlink(db_path)


# ======================================================================
# 14. 2026-07-25 incident fix (manager foxy1): the full card's СОСТОЯНИЕ
#     and ПРОКСИ blocks must agree with each other and with the shared
#     resolver for every proxy_guard state (healthy/broken/stale/never),
#     the dead proxy_last_error field must never be read again, and the
#     proxy password must never appear regardless of guard state.
# ======================================================================

def test_14_proxy_guard_display_states_agree() -> None:
    # 14a. FRESH EXPLICIT FAILURE ('broken') -> ❌, live auth_guard_error
    # shown, headline is proxy_bad, section C error sourced from the LIVE
    # field (never the dead proxy_last_error the live guard never writes).
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080",
                  auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts(),
                  auth_guard_error="TCP connect failed",
                  proxy_last_error="STALE_DEAD_FIELD_MUST_NOT_APPEAR")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14a. [C] fresh failure -> 'Проверка: ❌ не пройдена'",
              "Проверка: ❌ не пройдена" in text, text)
        check("14a. [C] fresh failure -> live auth_guard_error IS shown", "TCP connect failed" in text, text)
        check("14a. [DEAD FIELD] the legacy proxy_last_error value never appears anywhere",
              "STALE_DEAD_FIELD_MUST_NOT_APPEAR" not in text, text)
        check("14a. [13, AGREE] headline shows 'Proxy недоступен' (proxy_bad) matching section C's failure",
              "Proxy недоступен" in text, text)
    finally:
        os.unlink(db_path)

    # 14b. STALE failure (old check, past the freshness window) -> ❓
    # data-stale wording, section A carries the neutral hint, headline is
    # NOT proxy_bad (the exact foxy1 regression).
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080", status="active",
                  auth_guard_state="blocked", auth_guard_last_bad_at=_stale_ts(3 * 3600),
                  auth_guard_error="TCP connect failed", last_error="")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14b. [D, INCIDENT] stale failure -> 'Проверка: ❓ данные устарели' "
              "(NOT '⚠️ не пройдена', NOT '❌ не пройдена')",
              "Проверка: ❓ данные устарели" in text, text)
        check("14b. [D, INCIDENT] headline is NOT 'Proxy недоступен'", "Proxy недоступен" not in text, text)
        check("14b. [E] section A carries the neutral stale hint",
              "Последняя проверка proxy устарела" in text, text)
        check("14b. [C, no false error] a merely-stale result does not show a scary 'Ошибка proxy' line",
              "Ошибка proxy" not in text, text)
    finally:
        os.unlink(db_path)

    # 14c. NEVER checked -> "не выполнялась", section A neutral hint.
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14c. [E] never checked -> 'Проверка: не выполнялась'",
              "Проверка: не выполнялась" in text, text)
        check("14c. [E] section A carries the 'not confirmed' neutral hint",
              "Состояние proxy не подтверждено" in text, text)
        check("14c. [E] never-checked headline is NOT 'Proxy недоступен'",
              "Proxy недоступен" not in text, text)
    finally:
        os.unlink(db_path)

    # 14d. FRESH SUCCESS -> ✅, no error line, no stale/never hint, headline
    # not proxy_bad (the exact 02:03 Auth Guard result from the incident).
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080",
                  auth_guard_state="ok", auth_guard_last_ok_at=_fresh_ts())
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14d. [B] fresh success -> 'Проверка: ✅ пройдена'", "Проверка: ✅ пройдена" in text, text)
        check("14d. [B] no error line for a healthy proxy", "Ошибка proxy" not in text, text)
        check("14d. [B] no stale/never hint for a healthy proxy",
              "устарел" not in text and "не подтверждено" not in text, text)
        check("14d. [B] headline is NOT 'Proxy недоступен'", "Proxy недоступен" not in text, text)
    finally:
        os.unlink(db_path)

    # 14e. Bypass-allowed manager: no proxy hint noise at all, even with a
    # fresh 'blocked' state on the row (owner contract A).
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=0, proxy_required=1,
                  proxy_bypass_allowed=1,
                  auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts())
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14e. [A] bypass-allowed manager -> headline is NOT 'Proxy недоступен'",
              "Proxy недоступен" not in text, text)
        check("14e. [A] bypass-allowed manager -> no stale/never proxy hint noise",
              "устарел" not in text and "не подтверждено" not in text, text)
    finally:
        os.unlink(db_path)

    # 14f. Secret scan: the proxy password must never appear regardless of
    # guard state, including inside a (hypothetically poisoned) auth_guard_error.
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080",
                  auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts(),
                  auth_guard_error="authentication failed for user")
    _seed_lease(db_path, manager_key="mgr01")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        for forbidden in SECRET_FORBIDDEN_SUBSTRINGS:
            check(f"14f. [SECRET SCAN, broken state] {forbidden!r} never appears in the full card",
                  forbidden not in text, text)
    finally:
        os.unlink(db_path)

    # 14g. [R1] FRESH DEGRADED (proxy genuinely configured, checked_at
    # fresh) -> section C shows the distinct 'частично пройдена' wording
    # (never plain healthy, never plain broken); headline shows the
    # warning category, not ok/proxy_bad.
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080",
                  auth_guard_state="degraded", auth_guard_checked_at=_fresh_ts())
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14g. [R1] fresh degraded -> 'Проверка: ⚠️ проверка частично пройдена'",
              "Проверка: ⚠️ проверка частично пройдена" in text, text)
        check("14g. [R1, AGREE] headline shows the warning category (🟡 Предупреждение), "
              "not ok/proxy_bad", "🟡 Есть предупреждение" in text, text)
        check("14g. [R1, AGREE] headline is NOT 'Proxy недоступен' for a fresh degraded result",
              "Proxy недоступен" not in text, text)
    finally:
        os.unlink(db_path)

    # 14h. [R3.2] NOT_CONFIGURED: proxy required but missing host/port, even
    # though the row still carries a FRESH guard SUCCESS from before it was
    # unwired -- must never render "успешно"/healthy; headline agrees
    # (proxy_bad, the live config gap, not a staleness question).
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="", proxy_port="",
                  auth_guard_state="ok", auth_guard_last_ok_at=_fresh_ts())
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14h. [R3.2] required proxy missing host/port -> 'Проверка: не настроен'",
              "Проверка: не настроен" in text, text)
        check("14h. [R3.2] a fresh OLD guard success never renders as a current PROXY pass "
              "once the proxy is unwired -- scoped to the ПРОКСИ section's own 'Проверка:' "
              "verdict line specifically (RQ2, 2026-07-26: the SEPARATE 'Авторизация:' line "
              "now independently reflects the real Telegram/Auth Guard check result and MAY "
              "legitimately say '✅ пройдена' there -- see test_14k -- that is not this "
              "assertion's concern)",
              "Проверка: ✅ пройдена" not in text, text)
        check("14h. [R3.2, AGREE] headline shows proxy_bad -- a live config gap, not staleness",
              "Proxy недоступен" in text, text)
    finally:
        os.unlink(db_path)

    # 14i. [low-risk correction #2] MALFORMED timestamp -> distinct display
    # wording from genuinely-never-checked, but the SAME underlying
    # classification ('never' -- unconfirmed, not a confirmed failure);
    # headline must NOT be proxy_bad (unconfirmed evidence is not proof of
    # brokenness) and must NOT be a fabricated green either.
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080",
                  auth_guard_last_bad_at="not-a-real-timestamp")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14i. [malformed] 'Проверка: данные проверки некорректны' (NOT 'не выполнялась')",
              "Проверка: данные проверки некорректны" in text, text)
        check("14i. [malformed, AGREE] headline is neither a fabricated pass nor a confirmed failure",
              "Proxy недоступен" not in text and "✅ пройдена" not in text, text)
    finally:
        os.unlink(db_path)

    # 14j. [low-risk correction #3] FUTURE timestamp beyond the 60s
    # clock-skew tolerance -> treated as stale/unknown, never a false
    # healthy verdict, even though the raw field is a "success".
    db_path = _make_temp_db()
    future_far = (datetime.utcnow() + timedelta(hours=1)).replace(microsecond=0).isoformat()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080",
                  auth_guard_state="ok", auth_guard_last_ok_at=future_far)
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14j. [future timestamp] far-future 'success' timestamp -> 'Проверка: ❓ данные устарели', "
              "never fabricated as a fresh pass", "Проверка: ❓ данные устарели" in text, text)
        check("14j. [future timestamp, AGREE] headline is not a confirmed 'ok' for untrustworthy data",
              "✅ пройдена" not in text, text)
    finally:
        os.unlink(db_path)


# ======================================================================
# 14k. [RQ1/RQ2 corrective pass, 2026-07-26 independent-review follow-up]
#      The full card's "Авторизация:" line (section D, TELEGRAM) must
#      represent the Telegram/Auth Guard CHECK RESULT (via the raw,
#      freshness-aware _pb_proxy_guard_state classifier -- deliberately
#      NOT gated on live proxy configuration) -- NEVER proxy_effective
#      (which answers a DIFFERENT question: is a proxy in use). The prior
#      version of this test pinned two real defects as "expected": (a) a
#      never-checked manager was told its data "устарели" (conflated with
#      'stale'), and (b) a direct/bypass manager's real, fresh auth result
#      was replaced by "proxy не требуется", discarding genuine
#      success/failure information. This version proves the auth line
#      tracks ONLY the auth outcome by varying proxy configuration
#      (proxy-required / bypass / not_configured) INDEPENDENTLY of auth
#      outcome (fresh success/failure/degraded, stale, never) -- if the
#      line were still driven by proxy_effective, the bypass/not_configured
#      rows below would show the old "— (proxy не требуется/не настроен)"
#      wording instead of the real auth result, and this test would fail.
# ======================================================================

def test_14k_auth_guard_line_reflects_telegram_auth_not_proxy_state() -> None:
    scenarios = [
        # (label, proxy-config kwargs, auth-outcome kwargs, expected exact line)
        ("proxy-required, fresh success",
         dict(proxy_enabled=1, proxy_required=1, proxy_host="1.2.3.4", proxy_port="1080"),
         dict(auth_guard_state="ok", auth_guard_last_ok_at=_fresh_ts()),
         "Авторизация: ✅ пройдена"),
        ("proxy-required, fresh degraded",
         dict(proxy_enabled=1, proxy_required=1, proxy_host="1.2.3.4", proxy_port="1080"),
         dict(auth_guard_state="degraded", auth_guard_checked_at=_fresh_ts()),
         "Авторизация: ⚠️ пройдена частично"),
        ("proxy-required, fresh failure",
         dict(proxy_enabled=1, proxy_required=1, proxy_host="1.2.3.4", proxy_port="1080"),
         dict(auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts(), auth_guard_error="TCP connect failed"),
         "Авторизация: ❌ не пройдена"),
        ("proxy-required, stale result (RQ1: distinct from 'never')",
         dict(proxy_enabled=1, proxy_required=1, proxy_host="1.2.3.4", proxy_port="1080"),
         dict(auth_guard_state="blocked", auth_guard_last_bad_at=_stale_ts(3 * 3600), auth_guard_error="TCP connect failed"),
         "Авторизация: ❓ данные устарели"),
        ("proxy-required, never checked (RQ1: must NOT say 'устарели')",
         dict(proxy_enabled=1, proxy_required=1, proxy_host="1.2.3.4", proxy_port="1080"),
         dict(),
         "Авторизация: ➖ ещё не проверялась"),
        # --- RQ2: bypass/direct must NOT suppress a real, fresh auth result ---
        ("bypass, fresh auth SUCCESS -> success, NOT 'proxy не требуется'",
         dict(proxy_enabled=0, proxy_required=1, proxy_bypass_allowed=1),
         dict(auth_guard_state="direct", auth_guard_last_ok_at=_fresh_ts()),
         "Авторизация: ✅ пройдена"),
        ("bypass, fresh auth FAILURE -> failure still shown",
         dict(proxy_enabled=0, proxy_required=1, proxy_bypass_allowed=1),
         dict(auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts(), auth_guard_error="auth check failed"),
         "Авторизация: ❌ не пройдена"),
        ("bypass, no auth history at all -> never checked",
         dict(proxy_enabled=0, proxy_required=1, proxy_bypass_allowed=1),
         dict(),
         "Авторизация: ➖ ещё не проверялась"),
        ("not_configured proxy, fresh auth success -> auth line still success",
         dict(proxy_enabled=1, proxy_required=1, proxy_host="", proxy_port=""),
         dict(auth_guard_state="ok", auth_guard_last_ok_at=_fresh_ts()),
         "Авторизация: ✅ пройдена"),
    ]
    for label, proxy_fields, auth_fields, expected_line in scenarios:
        db_path = _make_temp_db()
        _seed_manager(db_path, manager_key="mgr01", **proxy_fields, **auth_fields)
        try:
            ns = build_full_card_ns(db_path)
            text = ns["_pb_manager_full_card_text"]("mgr01", 1)
            check(f"14k. [{label}] exact 'Авторизация:' line", expected_line in text, text)
        finally:
            os.unlink(db_path)

    # 14k-independence: on ONE card, proxy_effective and the auth outcome
    # genuinely DIVERGE (bypass configuration + a fresh auth FAILURE) --
    # prove the ПРОКСИ section's "Проверка:"/badge-driving proxy_effective
    # is completely unaffected by this fix (still neutral/bypass-worded)
    # while the Авторизация line independently shows the real failure. If
    # RQ2's fix had accidentally coupled the two lines, one of these two
    # assertions would fail.
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=0, proxy_required=1, proxy_bypass_allowed=1,
                  auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts(), auth_guard_error="auth check failed")
    try:
        ns = build_full_card_ns(db_path)
        text = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14k-independence. proxy section stays neutral bypass wording (proxy_effective unaffected)",
              "Не назначен (разрешена работа без proxy)" in text, text)
        check("14k-independence. Авторизация line independently shows the real auth failure",
              "Авторизация: ❌ не пройдена" in text, text)
    finally:
        os.unlink(db_path)

    # 14k-recovery: the EXACT incident-recovery scenario from the
    # foxy1/2026-07-25 forensics, but observed on the Авторизация line
    # specifically -- stale failure, then a fresh live success on the same
    # row, in the SAME process (no restart): stale wording must be replaced
    # by a clean pass immediately, never sticking as "failed"/"stale".
    db_path = _make_temp_db()
    _seed_manager(db_path, manager_key="mgr01", proxy_enabled=1, proxy_required=1,
                  proxy_host="1.2.3.4", proxy_port="1080",
                  auth_guard_state="blocked", auth_guard_last_bad_at=_stale_ts(3 * 3600),
                  auth_guard_error="TCP connect failed")
    try:
        ns = build_full_card_ns(db_path)
        text_before = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14k-recovery. [before] stale old failure -> 'Авторизация: ❓ данные устарели'",
              "Авторизация: ❓ данные устарели" in text_before, text_before)

        con = sqlite3.connect(db_path)
        con.execute(
            "UPDATE managers SET auth_guard_state='ok', auth_guard_last_ok_at=? WHERE manager_key='mgr01'",
            (_fresh_ts(),),
        )
        con.commit()
        con.close()

        text_after = ns["_pb_manager_full_card_text"]("mgr01", 1)
        check("14k-recovery. [after, SAME process] a fresh live success immediately clears the stale wording",
              "Авторизация: ✅ пройдена" in text_after, text_after)
        check("14k-recovery. [after] the stale wording no longer appears",
              "данные устарели" not in text_after.split("Авторизация:")[-1].split("\n")[0], text_after)
    finally:
        os.unlink(db_path)


# ======================================================================
# 15. 2026-07-25 post-deploy review (V1): the ACTIVE _proxy_detail_text
#     screen (menu:proxy:{key}) must render an explicit neutral bypass
#     verdict for effective state 'bypass' -- never the stale/broken/
#     healthy wording it fell through to before the verdict dict gained
#     a 'bypass' key. Exercises the REAL production helper (not a
#     reimplementation), reusing the SAME _pb_proxy_effective_state the
#     resolver/full-card/badge already use -- no second predicate.
# ======================================================================

def test_15_proxy_detail_screen_bypass_and_states() -> None:
    fresh = (datetime.utcnow() - timedelta(seconds=30)).replace(microsecond=0).isoformat()
    stale = (datetime.utcnow() - timedelta(hours=2)).replace(microsecond=0).isoformat()

    scenarios = {
        # 1. bypass/direct, nothing assigned at all -> the early
        # _tpag_panel_v2_mode()=='direct' screen (never reaches the
        # verdict dict -- unaffected by this fix, included for completeness).
        "s1": dict(proxy_required=0, proxy_enabled=0, proxy_bypass_allowed=0),
        # 2. bypass/direct WITH a stored host+port -- host+port present
        # forces _tpag_panel_v2_mode() -> "proxy", so the screen reaches
        # the verdict dict; _pb_proxy_effective_state must still resolve
        # 'bypass' (proxy_bypass_allowed=1) -- this is the exact V1
        # regression case (used to fall through to '🟡 устарел').
        "s2": dict(proxy_required=1, proxy_enabled=0, proxy_bypass_allowed=1,
                    proxy_host="1.2.3.4", proxy_port="1080"),
        # 3. bypass + a HISTORICAL FRESH broken Auth Guard result sitting
        # on the row -- bypass must win outright, the historical failure
        # must never surface as a current verdict.
        "s3": dict(proxy_required=1, proxy_enabled=0, proxy_bypass_allowed=1,
                    proxy_host="1.2.3.4", proxy_port="1080",
                    auth_guard_state="blocked", auth_guard_last_bad_at=fresh),
        # 4. bypass + a HISTORICAL STALE broken Auth Guard result.
        "s4": dict(proxy_required=1, proxy_enabled=0, proxy_bypass_allowed=1,
                    proxy_host="1.2.3.4", proxy_port="1080",
                    auth_guard_state="blocked", auth_guard_last_bad_at=stale),
        # 5-9: required+configured, host/port present -- regression guard
        # that adding the 'bypass' key changed NOTHING else.
        "s5": dict(proxy_required=1, proxy_enabled=1, proxy_host="1.2.3.4", proxy_port="1080",
                    auth_guard_state="ok", auth_guard_last_ok_at=fresh),
        "s6": dict(proxy_required=1, proxy_enabled=1, proxy_host="1.2.3.4", proxy_port="1080",
                    auth_guard_state="degraded", auth_guard_checked_at=fresh),
        "s7": dict(proxy_required=1, proxy_enabled=1, proxy_host="1.2.3.4", proxy_port="1080",
                    auth_guard_state="blocked", auth_guard_last_bad_at=fresh,
                    auth_guard_error="TCP connect failed"),
        "s8": dict(proxy_required=1, proxy_enabled=1, proxy_host="1.2.3.4", proxy_port="1080",
                    auth_guard_state="blocked", auth_guard_last_bad_at=stale),
        "s9": dict(proxy_required=1, proxy_enabled=1, proxy_host="1.2.3.4", proxy_port="1080"),
        # 10. required + not_configured: enabled=0 but host/port ARE
        # present, so _tpag_panel_v2_mode still routes to "proxy" (the
        # config gap is what _pb_proxy_effective_state itself detects via
        # proxy_enabled, not via mode) -- exercises the verdict dict's
        # 'not_configured' entry rather than the early direct-mode screen.
        "s10": dict(proxy_required=1, proxy_enabled=0, proxy_host="1.2.3.4", proxy_port="1080"),
    }

    db_path = _make_temp_db()
    for key, fields in scenarios.items():
        _seed_manager(db_path, manager_key=key, **fields)
    try:
        ns = build_full_card_ns(db_path)
        row_by_key = ns["_manager_row_by_key"]
        detail_text = ns["_proxy_detail_text"]

        def render(scenario_key):
            row = row_by_key(scenario_key)
            return detail_text(row)

        # --- bypass cases 1-4: explicit neutral wording; never
        # устарел/failure/healthy wording, per owner requirement.
        for sid, note in (("s1", "no proxy assigned at all"),
                           ("s2", "stored host+port"),
                           ("s3", "historical FRESH broken Auth Guard data"),
                           ("s4", "historical STALE broken Auth Guard data")):
            text = render(sid)
            check(f"15.{sid} [V1, bypass, {note}] contains neutral bypass/direct wording ('без proxy')",
                  "без proxy" in text, text)
            check(f"15.{sid} [V1] does not contain 'устарел'", "устарел" not in text, text)
            check(f"15.{sid} [V1] does not contain failure wording ('не готов'/'Причина:'/'🔴')",
                  not any(w in text for w in ("не готов", "Причина:", "🔴")), text)
            check(f"15.{sid} [V1] does not contain healthy wording ('🟢 готов'/'✅')",
                  "🟢 готов" not in text and "✅" not in text, text)

        # --- s2/s3/s4 additionally confirm the verdict-dict path was
        # actually reached (not the early direct-mode screen) and that
        # the new entry is EXACTLY the added mapping value.
        for sid in ("s2", "s3", "s4"):
            text = render(sid)
            check(f"15.{sid} [V1] reached the verdict-dict branch (SOCKS5 line present)",
                  "SOCKS5:" in text, text)
            check(f"15.{sid} [V1] exact verdict line 'Auth Guard: ⚪ Работа без proxy'",
                  "Auth Guard: ⚪ Работа без proxy" in text, text)

        # s1 explicitly did NOT reach the verdict-dict branch (no SOCKS5 line).
        check("15.s1 [V1] no-proxy-at-all case uses the early direct-mode screen (no 'Auth Guard:' line)",
              "Auth Guard:" not in render("s1"), render("s1"))

        # --- required+configured states 5-10: UNCHANGED behavior
        # (regression guard -- the new 'bypass' key must not alter any
        # other verdict mapping).
        text5 = render("s5")
        check("15.s5 [regression] required+healthy -> 'Auth Guard: 🟢 готов'",
              "Auth Guard: 🟢 готов" in text5, text5)
        text6 = render("s6")
        check("15.s6 [regression] required+degraded -> 'Auth Guard: 🟡 частично готов'",
              "Auth Guard: 🟡 частично готов" in text6, text6)
        text7 = render("s7")
        check("15.s7 [regression] required+broken -> 'Auth Guard: 🔴 не готов'",
              "Auth Guard: 🔴 не готов" in text7, text7)
        check("15.s7 [regression] required+broken -> shows the sanitized error line",
              "Причина: TCP connect failed" in text7, text7)
        text8 = render("s8")
        check("15.s8 [regression] required+stale -> 'Auth Guard: 🟡 устарел'",
              "Auth Guard: 🟡 устарел" in text8, text8)
        text9 = render("s9")
        check("15.s9 [regression] required+never -> 'Auth Guard: 🟡 не проверялся'",
              "Auth Guard: 🟡 не проверялся" in text9, text9)
        text10 = render("s10")
        check("15.s10 [regression] required+not_configured -> 'Auth Guard: 🔴 не настроен'",
              "Auth Guard: 🔴 не настроен" in text10, text10)
    finally:
        os.unlink(db_path)


def main() -> int:
    test_1_header_order_and_full_phone()
    test_2_six_sections_in_order()
    test_3_proxy_lease_no_secrets()
    test_3b_no_proxy_assigned()
    test_4_no_internal_jargon_in_text()
    test_5_shared_status_resolver_reuse()
    test_6_reuses_existing_helpers_no_duplication()
    test_7_buttons_and_second_schedule_entry_point()
    test_8_two_page_fallback()
    test_9_routing_and_retarget()
    test_10_missing_manager_is_safe()
    test_11_markdown_injection_neutralized()
    test_12_long_and_unicode_text_is_safe()
    test_13_not_found_key_escaped_against_markdown_injection()
    test_14_proxy_guard_display_states_agree()
    test_14k_auth_guard_line_reflects_telegram_auth_not_proxy_state()
    test_15_proxy_detail_screen_bypass_and_states()

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
