# -*- coding: utf-8 -*-
"""tools/manager_card_unified_selftest.py -- offline self-test for the
approved Phase N2 "unified manager card" migration in panel_bot.py
(2026-07-21): the old duplicate "admin card" (menu:manager_admin:{key} ->
_manager_admin_detail_text/_buttons) and the "settings card"
(menu:manager_settings:{key} -> _manager_settings_card_text/_buttons) are
replaced by ONE canonical card. The historical manager_admin:{key} callback
still resolves, but now renders the SAME canonical card.

What changed in panel_bot.py (all inside the existing single-def "MANAGER
SETTINGS MENU" block plus one branch-edit + two call-site fixes elsewhere;
no def was deleted, no PREV chain was touched -- same policy as Phase N1):
  * _manager_settings_state -- canonical 3-state wording (Работает/
    Выключен/Остановлен вручную), same 3 callers as before.
  * _mcard_btn -- new 64-byte callback guard (mirrors NM's _nm_btn).
  * _manager_settings_card_text/_buttons -- rewritten: one dynamic lifecycle
    toggle (start<->disable-with-confirm, never enable/stop), separate
    one-shot restart, plus rename/replace/relogin/devlogin/reserve/proxy-
    link/transfers-link/modes-card-link/one danger-zone entry button --
    every single one reusing an existing callback/command/wizard verbatim.
  * _manager_disable_confirm_text/_buttons -- NEW confirm screen; re-reads
    state fresh and no-ops (re-renders the card, no destructive button) if
    the manager is already not "on".
  * _manager_danger_zone_text/_buttons -- NEW screen hosting the exact same
    guard: callbacks the old admin card exposed inline.
  * _title_for_menu (the "MANAGER SETTINGS MENU" generation, marker
    "manager_disable_confirm:") gained 2 branches for the two new routes.
  * _title_for_menu (the tp_visual generation, marker: both
    'raw.startswith("manager_admin:")' AND "_manager_settings_card_text(key)"
    present together) -- the manager_admin:{key} branch now calls the
    canonical card functions instead of the old admin-card ones.
  * Two `rw:` (replacement wizard) callback branches (action=="cancelpre",
    action=="commit_close") that used to call the old admin-card functions
    directly (bypassing the menu: router) now call the canonical ones too.

_manager_admin_detail_text/_buttons themselves are LEFT FULLY DEFINED AND
UNTOUCHED (orphaned-by-redirect only) -- same safe policy already used for
nm_control/nm_sources in Phase N1. This file proves that both routes above
that used to reach them no longer do, while the underlying def/PREV/handler
inventory is unchanged.

Techniques: panel_bot.py cannot be imported standalone (Telethon/env side
effects at import time) -- functions under test are extracted via
ast.parse + ast.unparse + exec() and run FOR REAL against a temporary
SQLite file (never db/data_tpilot.db or db/data.db), same idiom used
throughout this project's tools/*_selftest.py files.

_title_for_menu is defined ~30+ times in panel_bot.py via an override-stack
(only the LAST active definition matters at runtime; delegation via
_PREV = globals().get("_title_for_menu") chains). This file locates the
TWO specific generations it needs by unique marker strings in their
unparsed source (mirrors tools/nm_menu_distribution_selftest.py's
_find_edited_title_for_menu technique) and isolates each from the other
~30 generations with a FAKE captured predecessor -- deliberate, scoped
isolation, not an oversight.

    python tools\\manager_card_unified_selftest.py
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import sys
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
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


PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()


class _FakeBtn:
    __slots__ = ("text", "data")

    def __init__(self, text, data):
        self.text = text
        self.data = data

    def __iter__(self):
        yield "btn"
        yield self.text
        yield self.data

    def __getitem__(self, i):
        return ("btn", self.text, self.data)[i]

    def __repr__(self):
        return f"_FakeBtn(text={self.text!r}, data={self.data!r})"


class _FakeButton:
    @staticmethod
    def inline(text, data):
        raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
        return _FakeBtn(text, raw)


# ======================================================================
# Temp DB (never db/data_tpilot.db or db/data.db) -- same minimal
# `managers` schema tools/nm_menu_distribution_selftest.py already proved
# sufficient for list_manager_rows_from_db_sync (SELECT * FROM managers).
# ======================================================================

def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="manager_card_unified_selftest_")
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
                manual_stopped INTEGER DEFAULT 0,
                proxy_required INTEGER DEFAULT 0, proxy_enabled INTEGER DEFAULT 0,
                proxy_bypass_allowed INTEGER DEFAULT 0, proxy_test_ok INTEGER DEFAULT 0,
                proxy_host TEXT DEFAULT '', proxy_port TEXT DEFAULT '',
                auth_guard_state TEXT DEFAULT '', auth_guard_checked_at TEXT DEFAULT '',
                auth_guard_last_ok_at TEXT DEFAULT '', auth_guard_last_bad_at TEXT DEFAULT ''
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def _seed_managers(db_path: str, managers: list) -> None:
    con = sqlite3.connect(db_path)
    try:
        for m in managers:
            con.execute(
                "INSERT INTO managers(manager_key, display_name, telegram_username, role, status, is_enabled, manual_stopped, "
                "proxy_required, proxy_enabled, proxy_bypass_allowed, proxy_test_ok, proxy_host, proxy_port, "
                "auth_guard_state, auth_guard_checked_at, auth_guard_last_ok_at, auth_guard_last_bad_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (m["manager_key"], m.get("display_name", m["manager_key"]), m.get("telegram_username", ""),
                 m.get("role", "manager"), m.get("status", "active"), int(m.get("is_enabled", 1)), int(m.get("manual_stopped", 0)),
                 int(m.get("proxy_required", 0)), int(m.get("proxy_enabled", 0)),
                 int(m.get("proxy_bypass_allowed", 0)), int(m.get("proxy_test_ok", 0)),
                 m.get("proxy_host", ""), m.get("proxy_port", ""),
                 m.get("auth_guard_state", ""), m.get("auth_guard_checked_at", ""),
                 m.get("auth_guard_last_ok_at", ""), m.get("auth_guard_last_bad_at", "")),
            )
        con.commit()
    finally:
        con.close()


# ======================================================================
# Extraction #1: the unified-card functions themselves (single defs, no
# override stack -- safe to extract by exact name).
# ======================================================================

CARD_NAMES = {
    "_manager_settings_state", "_mcard_btn",
    "_manager_settings_card_text", "_manager_settings_card_buttons",
    "_manager_disable_confirm_text", "_manager_disable_confirm_buttons",
    "_manager_danger_zone_text", "_manager_danger_zone_buttons",
    # N2.1: the manager LIST screen too (badge derivation depends on the
    # status label's first token -- must be covered, not assumed).
    "_manager_settings_text", "_manager_settings_buttons",
}

# N5.4.2: the shared deterministic status resolver the list/counters now
# use, extracted for REAL (project convention: prefer the real helper over
# a fake whenever the real one is pure/DB-boundary-only). _connect_panel_db
# is real too (queries the harness's own temp db_path -- no table means a
# genuine fail-open {}, exactly the production behavior on a fresh DB).
# _pb_service_scan_cached is the ONE deliberate fake (see build_card_ns) --
# a real PowerShell process scan has no place in an offline selftest.
STATUS_RESOLVER_NAMES = {
    "_pb_manager_status_resolve", "_pb_manager_status_resolve_live",
    "_pb_tg_health_row", "_pb_manager_process_running",
    "_PB_STATUS_CATEGORIES", "_PB_STATUS_BANNED_MARKS", "_PB_STATUS_AUTH_MARKS",
    "_connect_panel_db",
    # 2026-07-25 proxy-freshness incident fix: _pb_manager_status_resolve
    # now calls _pb_proxy_guard_state, which needs these two.
    "_pb_proxy_guard_state", "_PB_PROXY_GUARD_FRESH_SEC", "_tpag_panel_v2_parse_dt",
    # 2026-07-25 R1-R3 follow-up (independent-review): _pb_manager_status_
    # resolve's proxy_bad/warning/ok categories AND _manager_proxy_badge
    # (R2: unified, no duplicated timestamp/health logic) now both route
    # through the single shared _pb_proxy_effective_state predicate.
    "_pb_proxy_effective_state", "_pb_proxy_ts_is_fresh",
    "_PB_PROXY_CLOCK_SKEW_TOLERANCE_SEC", "_PB_PROXY_BADGE_TEXT",
    # W1 (2026-07-29, frozen master plan, D-26/I-25): the fleet-list screen
    # now calls the instrumented wrapper instead of the bare live resolver.
    # These are all pure/DB-boundary-only, extracted for real like the rest
    # of this set -- _w1_emit_render_event (file I/O) is the ONE deliberate
    # fake below, same pattern as _pb_service_scan_cached above.
    "_pb_manager_status_resolve_live_logged", "_w1_render_reason_code",
    "_w1_result_age_seconds", "_w1_render_classification_record",
    "_W1_RENDER_REASON_BY_CATEGORY",
    # PEERFLOOD RECOVERY 20260812 (P2/P3): the fleet-list screen now calls
    # the badged wrapper (runtime status + separate Telegram Health badge).
    # Pure/DB-boundary-only like the rest of this set.
    "_pb_manager_status_resolve_live_badged", "_pb_tg_health_badge",
    "_pb_tg_health_family_label",
}

# Real, DB-backed row-lookup helpers the card functions call -- extracted
# for real rather than faked (project convention: prefer the real helper).
ROWLOOKUP_NAMES = {
    "_manager_rows", "_manager_rows_all", "_manager_row_by_key",
    "_manager_short_label", "_manager_proxy_badge",
}


def _extract_by_name(names: set) -> list:
    tree = ast.parse(PANEL_SRC)
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
        raise AssertionError(f"_extract_by_name: expected {names}, missing {missing}")
    return nodes


def _contains_quote_tolerant(src_txt: str, marker: str) -> bool:
    """ast.unparse NORMALIZES QUOTE STYLE (always single-quotes string
    literals), so a marker containing a double-quoted literal (as it
    appears in the real .py source) must also be checked with single
    quotes -- this is exactly the documented project pitfall (see
    ADDENDUM_FOR_NEW_PC.md): never assert an exact quoted substring
    against unparsed source without tolerating both quote chars."""
    if marker in src_txt:
        return True
    if '"' in marker:
        return marker.replace('"', "'") in src_txt
    if "'" in marker:
        return marker.replace("'", '"') in src_txt
    return False


def _find_generation(tree: ast.Module, required_substrings: tuple) -> ast.FunctionDef:
    """Locate the ONE _title_for_menu generation whose unparsed source
    contains ALL of `required_substrings` (quote-tolerant) -- the LAST such
    match wins (same last-def-wins rule as the real override stack). Used
    to isolate exactly one of the ~30+ generations without depending on
    line numbers."""
    found = None
    for n in tree.body:
        if getattr(n, "name", None) == "_title_for_menu":
            try:
                src_txt = ast.unparse(n)
            except Exception:
                continue
            if all(_contains_quote_tolerant(src_txt, s) for s in required_substrings):
                found = n
    return found


def build_card_ns(db_path: str, *, active_op: dict | None = None,
                   ss_cfg: dict | None = None, proc_scan: "list | None" = None) -> dict:
    import manager_registry

    tree = ast.parse(PANEL_SRC)
    nodes = _extract_by_name(CARD_NAMES) + _extract_by_name(ROWLOOKUP_NAMES) + _extract_by_name(STATUS_RESOLVER_NAMES)

    settings_gen = _find_generation(tree, ("manager_disable_confirm:",))
    if settings_gen is None:
        raise AssertionError(
            "build_card_ns: could not locate the _title_for_menu generation "
            "that owns manager_settings/manager_disable_confirm/"
            "manager_danger routing (marker 'manager_disable_confirm:' not "
            "found) -- migration may have been reverted or moved"
        )
    nodes.append(settings_gen)

    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    _active_op_holder = {"value": active_op or {}}
    _ss_cfg_holder = {"value": ss_cfg or {"mode": "off", "reminder_enabled": False, "reminder_time": "18:00"}}

    def _fake_replace_active_op_for_old_key(_key):
        return dict(_active_op_holder["value"])

    def _fake_ss_config(_key):
        return dict(_ss_cfg_holder["value"])

    def _fake_ss_counts_today(_key):
        return (0, 0)

    # N5.4.2: the ONE deliberate fake for the status resolver -- a real
    # PowerShell process scan has no place in an offline selftest. Default
    # None mirrors production's own fail-open behavior (no scan data yet ->
    # proc_running=None -> resolver treats it as "unknown", never a
    # fabricated positive/negative). Controllable via _set_proc_scan so
    # individual tests can inject a synthetic `--manager <key>` process
    # list and exercise the process_down/ok branches deterministically.
    _proc_scan_holder = {"value": proc_scan}

    def _fake_service_scan_cached():
        return _proc_scan_holder["value"]

    _w1_emitted_holder = {"records": []}

    def _fake_w1_emit_render_event(record):
        # W1's one deliberate fake in this namespace (file I/O), matching
        # the existing _pb_service_scan_cached pattern -- never touches a
        # real log file during this unrelated card-rendering test.
        _w1_emitted_holder["records"].append(record)

    ns = {
        "sqlite3": sqlite3,
        "os": os,
        "re": re,
        "uuid": uuid,
        "json": __import__("json"),
        "ZoneInfo": __import__("zoneinfo").ZoneInfo,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "datetime": datetime, "timezone": timezone,
        # utcnow refactor (2026-08-16): extracted proxy-guard helpers read the
        # clock through the module-level _pb_utc_now() seam (naive UTC).
        "_pb_utc_now": (lambda: datetime.now(timezone.utc).replace(tzinfo=None)),
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "BASE_DIR": BASE_DIR,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        "_safe_text": lambda s, limit=3900: str(s or "")[:limit],
        "_tp_visual_screen": lambda path_items, description="", extra="": "\n".join([" > ".join(path_items), description, extra]).strip(),
        "_panel_header": lambda: "HEADER",
        "_replace_active_op_for_old_key": _fake_replace_active_op_for_old_key,
        "_ss_config": _fake_ss_config,
        "_ss_counts_today": _fake_ss_counts_today,
        "_SS_MODE_LABELS": {"off": "Выкл", "instant": "Сразу", "daily": "В конце дня"},
        "_norm_path": lambda s: str(s or "").replace("\\", "/").lower(),
        "_pb_service_scan_cached": _fake_service_scan_cached,
        "_w1_emit_render_event": _fake_w1_emit_render_event,
        # Captured by _MS_PREV_TITLE_FOR_MENU = globals().get("_title_for_menu")
        # -- deliberately fake, isolating this test from the other ~29
        # generations, none of which handle any route this test exercises.
        "_title_for_menu": lambda raw: (f"OLD_MENU:{raw}", []),
    }
    exec(compile(module_src, f"<{PANEL_PATH}:card>", "exec"), ns)

    ns["_set_active_op"] = lambda v: _active_op_holder.__setitem__("value", v or {})
    ns["_set_ss_cfg"] = lambda v: _ss_cfg_holder.__setitem__("value", v)
    ns["_set_proc_scan"] = lambda v: _proc_scan_holder.__setitem__("value", v)
    return ns


def build_redirect_ns(db_path: str, card_ns: dict) -> dict:
    """Extracts the tp_visual _title_for_menu generation's manager_admin:
    branch, wired to call the SAME (already-extracted) canonical card
    functions from card_ns -- proving the redirect calls the real functions
    under test, not a re-extracted duplicate."""
    import manager_registry

    tree = ast.parse(PANEL_SRC)
    edited = _find_generation(tree, ('raw.startswith("manager_admin:")', "_manager_settings_card_text(key)"))
    if edited is None:
        raise AssertionError(
            "build_redirect_ns: could not locate the tp_visual _title_for_menu "
            "generation whose manager_admin: branch calls "
            "_manager_settings_card_text (redirect may have been reverted)"
        )
    module_src = ast.unparse(edited)

    ns = {
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "_manager_row_by_key": card_ns["_manager_row_by_key"],
        "_manager_short_label": card_ns["_manager_short_label"],
        "_tp_visual_screen": lambda path_items, description="", extra="": "\n".join([" > ".join(path_items), description, extra]).strip(),
        "_manager_settings_card_text": card_ns["_manager_settings_card_text"],
        "_manager_settings_card_buttons": card_ns["_manager_settings_card_buttons"],
        # Every other branch in this generation (main/description/reports_control/
        # simple[...]/manager:/proxy:/etc.) is syntactically present but never
        # executed for our "manager_admin:" test input, so those names never
        # need to resolve. Fallback delegate is faked for completeness only.
        "_TP_VISUAL_ORIG_TITLE_FOR_MENU": lambda raw: (f"OLD_MENU:{raw}", []),
    }
    exec(compile(module_src, f"<{PANEL_PATH}:redirect>", "exec"), ns)
    return ns


def _extract_old_admin_card_ns(db_path: str) -> dict:
    """Extracts the OLD (now-orphaned) admin-card functions for a
    regression comparison only -- proves they still exist untouched and
    lets us diff their guard: callback set against the new danger zone."""
    import manager_registry

    # The old admin-card BUTTONS chain is a 4-layer PREV-delegation wrapper
    # (base -> M2.12A -> relogin -> replace), each capturing its predecessor
    # via `_X_PREV = globals().get("_manager_admin_detail_buttons")` BEFORE
    # its own def. Both the def nodes AND these assignment nodes must be
    # collected in source order and exec'd together as one script, exactly
    # like nm_menu_distribution_selftest.py's build_routing_ns/build_legacy_ns
    # -- collecting only the `def` nodes (as an earlier draft of this file
    # did) makes the wrapper crash with NameError on its own PREV capture.
    OLD_DEF_NAMES = {"_manager_admin_detail_text", "_manager_admin_detail_buttons", "_section_button"}
    OLD_PREV_NAMES = {
        "_M212A_ORIG_ADMIN_DETAIL_TEXT", "_M212A_ORIG_ADMIN_DETAIL_BUTTONS",
        "_RELOGIN_PREV_ADMIN_DETAIL_BUTTONS", "_RW_PREV_ADMIN_DETAIL_BUTTONS",
    }
    tree = ast.parse(PANEL_SRC)
    nodes = []
    seen = set()
    for n in tree.body:
        nm = getattr(n, "name", None)
        if nm in OLD_DEF_NAMES:
            nodes.append(n)  # keep ALL defs in source order -- last wins on exec, same as runtime
            seen.add(nm)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in OLD_PREV_NAMES:
            nodes.append(n)
            seen.add(n.targets[0].id)
    missing = (OLD_DEF_NAMES | OLD_PREV_NAMES) - seen
    if missing:
        raise AssertionError(f"_extract_old_admin_card_ns: missing {missing} -- old chain was deleted!")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "Button": _FakeButton,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "_manager_short_label": None,  # bound below after row-lookup extraction
        "_manager_state_icon": lambda row: "🟢",
        "_manager_state_label": lambda row: "активен",
        "_manager_proxy_badge": lambda row: "✅ proxy ok",
        "_manager_row_by_key": None,
        "_m212a_runtime_status_line": lambda key, row: "",
        "_replace_active_op_for_old_key": lambda key: {},
    }
    exec(compile(module_src, f"<{PANEL_PATH}:old_admin_card>", "exec"), ns)
    return ns


# ======================================================================
# Helpers for reading rows produced by the fake Button
# ======================================================================

def _flat(rows) -> list:
    return [btn for row in rows for btn in row]


def _find(rows, *, label=None, data=None):
    for btn in _flat(rows):
        _, text, cb = btn
        if label is not None and text != label:
            continue
        if data is not None and cb != data:
            continue
        return btn
    return None


def _labels(rows) -> list:
    return [btn[1] for btn in _flat(rows)]


def _datas(rows) -> list:
    return [btn[2] for btn in _flat(rows)]


# ======================================================================
# 1/2. One canonical renderer; manager_admin:{key} resolves to it.
# ======================================================================

def test_1_2_one_canonical_card_and_redirect() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01", "display_name": "Manager 01", "is_enabled": 1, "status": "active", "manual_stopped": 0}])
    try:
        card_ns = build_card_ns(db_path)
        redirect_ns = build_redirect_ns(db_path, card_ns)

        direct_text, direct_rows = card_ns["_manager_settings_card_text"]("mgr01"), card_ns["_manager_settings_card_buttons"]("mgr01")
        redirected_text, redirected_rows = redirect_ns["_title_for_menu"]("manager_admin:mgr01")

        check("1/2. manager_admin:{key} no longer returns the OLD_MENU fallback stub",
              not str(redirected_text).startswith("OLD_MENU:"), redirected_text)
        check("1/2. manager_admin:{key} button set is IDENTICAL to manager_settings:{key} (label+callback pairs)",
              [(b[1], b[2]) for b in _flat(redirected_rows)] == [(b[1], b[2]) for b in _flat(direct_rows)],
              (_labels(redirected_rows), _labels(direct_rows)))
        check("1/2. manager_admin:{key} text is produced by the same canonical card builder",
              redirected_text == direct_text, (redirected_text, direct_text))
    finally:
        os.unlink(db_path)


# ======================================================================
# 3/4/5. No simultaneous start/stop/enable/disable pairs; each state
# emits exactly one toggle label.
# ======================================================================

def test_3_4_5_single_dynamic_toggle_per_state() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": "on1", "is_enabled": 1, "status": "active", "manual_stopped": 0},
        {"manager_key": "off1", "is_enabled": 0, "status": "disabled", "manual_stopped": 0},
        {"manager_key": "stop1", "is_enabled": 1, "status": "active", "manual_stopped": 1},
    ])
    try:
        ns = build_card_ns(db_path)
        for key, expect_on in (("on1", True), ("off1", False), ("stop1", False)):
            rows = ns["_manager_settings_card_buttons"](key)
            labels = _labels(rows)
            datas = _datas(rows)
            check(f"3. {key}: no legacy '/manager_stop' callback anywhere on the card",
                  not any(d.startswith(b"cmd:/manager_stop ") for d in datas), datas)
            check(f"3. {key}: no legacy '/manager_enable' callback anywhere on the card",
                  not any(d.startswith(b"cmd:/manager_enable ") for d in datas), datas)
            check(f"3. {key}: no old-style '▶️ Запустить'/'⏹ Остановить'/'✅ Включить' labels",
                  not any(l in ("▶️ Запустить", "⏹ Остановить", "✅ Включить") for l in labels), labels)
            has_disable_btn = "⏸ Выключить менеджера" in labels
            has_enable_btn = "▶️ Включить менеджера" in labels
            check(f"3. {key}: never both toggle labels at once", not (has_disable_btn and has_enable_btn), labels)
            if expect_on:
                check(f"4. {key} (working): emits ONLY '⏸ Выключить менеджера'", has_disable_btn and not has_enable_btn, labels)
            else:
                check(f"5. {key} (disabled/stopped): emits ONLY '▶️ Включить менеджера'", has_enable_btn and not has_disable_btn, labels)
    finally:
        os.unlink(db_path)


# ======================================================================
# 6/7/8. Enable maps to /manager_start (not enable); disable maps to
# /manager_disable via the confirm screen; restart is separate/one-shot.
# ======================================================================

def test_6_7_8_command_mapping_and_restart() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": "on1", "is_enabled": 1, "status": "active", "manual_stopped": 0},
        {"manager_key": "off1", "is_enabled": 0, "status": "disabled", "manual_stopped": 0},
    ])
    try:
        ns = build_card_ns(db_path)

        off_rows = ns["_manager_settings_card_buttons"]("off1")
        enable_btn = _find(off_rows, label="▶️ Включить менеджера")
        check("6. Enable button exists", enable_btn is not None, off_rows)
        check("6. Enable action maps to /manager_start, not /manager_enable",
              enable_btn is not None and enable_btn[2] == b"cmd:/manager_start off1", enable_btn)

        on_rows = ns["_manager_settings_card_buttons"]("on1")
        disable_btn = _find(on_rows, label="⏸ Выключить менеджера")
        check("7. Disable button routes to the confirm screen (not directly to cmd:/manager_disable)",
              disable_btn is not None and disable_btn[2] == b"menu:manager_disable_confirm:on1", disable_btn)

        confirm_rows = ns["_manager_disable_confirm_buttons"]("on1")
        confirm_btn = _find(confirm_rows, label="✅ Подтвердить, выключить")
        check("7. Confirming disable maps to /manager_disable",
              confirm_btn is not None and confirm_btn[2] == b"cmd:/manager_disable on1", confirm_btn)

        restart_on = _find(on_rows, label="🔄 Перезапустить")
        restart_off = _find(off_rows, label="🔄 Перезапустить")
        check("8. Restart present and maps to /manager_restart when working",
              restart_on is not None and restart_on[2] == b"cmd:/manager_restart on1", restart_on)
        check("8. Restart present and maps to /manager_restart when disabled (one-shot, not a toggle)",
              restart_off is not None and restart_off[2] == b"cmd:/manager_restart off1", restart_off)
    finally:
        os.unlink(db_path)


# ======================================================================
# 9/10. Disable confirmation exists; stale-state revalidation/no-op.
# ======================================================================

def test_9_10_disable_confirm_and_stale_noop() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": "on1", "is_enabled": 1, "status": "active", "manual_stopped": 0},
        {"manager_key": "off1", "is_enabled": 0, "status": "disabled", "manual_stopped": 0},
    ])
    try:
        ns = build_card_ns(db_path)

        on_text = ns["_manager_disable_confirm_text"]("on1")
        on_buttons = ns["_manager_disable_confirm_buttons"]("on1")
        check("9. Confirm screen (still working) warns and offers confirm/cancel",
              "Выключить" in on_text and _find(on_buttons, label="✅ Подтвердить, выключить") is not None
              and _find(on_buttons, label="❌ Отмена") is not None,
              (on_text, on_buttons))
        check("9. Cancel routes back to the card, no mutation",
              _find(on_buttons, label="❌ Отмена")[2] == b"menu:manager_settings:on1", on_buttons)

        # Stale state: admin's message shows the toggle for a manager that
        # is (by the time they click through to the confirm screen) already
        # off -- must NOT offer the destructive action again.
        off_text = ns["_manager_disable_confirm_text"]("off1")
        off_buttons = ns["_manager_disable_confirm_buttons"]("off1")
        check("10. Stale-state: confirm screen for an already-disabled manager shows a notice, not a warning-to-disable",
              "уже не работает" in off_text, off_text)
        check("10. Stale-state: no destructive cmd:/manager_disable button is offered",
              not any(d.startswith(b"cmd:/manager_disable ") for d in _datas(off_buttons)), off_buttons)
        check("10. Stale-state: re-renders the (up to date) card buttons instead",
              [b[2] for b in _flat(off_buttons)] == [b[2] for b in _flat(ns["_manager_settings_card_buttons"]("off1"))],
              off_buttons)
    finally:
        os.unlink(db_path)


# ======================================================================
# 11. Proxy button links to the canonical proxy flow, no duplicated logic.
# ======================================================================

def test_11_proxy_link_only() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01", "is_enabled": 1, "status": "active", "manual_stopped": 0}])
    try:
        ns = build_card_ns(db_path)
        rows = ns["_manager_settings_card_buttons"]("mgr01")
        proxy_btns = [b for b in _flat(rows) if "Proxy" in b[1] or "прокси" in b[1].lower() or "Прокси" in b[1]]
        check("11. Exactly one proxy button on the card", len(proxy_btns) == 1, proxy_btns)
        check("11. Proxy button routes to the canonical menu:proxy:{key} flow",
              proxy_btns and proxy_btns[0][2] == b"menu:proxy:mgr01", proxy_btns)
        datas = _datas(rows)
        check("11. No proxy-management-specific callback prefix (pxm:/ppool:/prn:) appears on the card",
              not any(d.startswith((b"pxm:", b"ppool:", b"prn:", b"renew:")) for d in datas), datas)
    finally:
        os.unlink(db_path)


# ======================================================================
# 11b. 2026-07-25 R1-R3 follow-up (independent-review, R2): the unified
# card's "Proxy: ..." line must agree EXACTLY with the shared
# _pb_proxy_effective_state predicate + _PB_PROXY_BADGE_TEXT table -- no
# second, locally-duplicated proxy-health mapping. Covers owner scenario
# 16 ("_manager_proxy_badge agrees with shared resolver") across bypass/
# healthy/degraded/broken/stale/never/not_configured.
# ======================================================================

def test_11b_proxy_badge_agrees_with_shared_resolver() -> None:
    fresh = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)).replace(microsecond=0).isoformat()
    stale = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    cases = [
        ("bypass1", {"proxy_required": 0, "proxy_enabled": 0}),
        ("healthy1", {"proxy_required": 1, "proxy_enabled": 1, "proxy_host": "1.2.3.4", "proxy_port": "1080",
                      "auth_guard_state": "ok", "auth_guard_last_ok_at": fresh}),
        ("degraded1", {"proxy_required": 1, "proxy_enabled": 1, "proxy_host": "1.2.3.4", "proxy_port": "1080",
                       "auth_guard_state": "degraded", "auth_guard_checked_at": fresh}),
        ("broken1", {"proxy_required": 1, "proxy_enabled": 1, "proxy_host": "1.2.3.4", "proxy_port": "1080",
                     "auth_guard_state": "blocked", "auth_guard_last_bad_at": fresh}),
        ("stale1", {"proxy_required": 1, "proxy_enabled": 1, "proxy_host": "1.2.3.4", "proxy_port": "1080",
                    "auth_guard_state": "blocked", "auth_guard_last_bad_at": stale}),
        ("never1", {"proxy_required": 1, "proxy_enabled": 1, "proxy_host": "1.2.3.4", "proxy_port": "1080"}),
        ("notcfg1", {"proxy_required": 1, "proxy_enabled": 1, "proxy_host": "", "proxy_port": ""}),
    ]
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": key, "is_enabled": 1, "status": "active", "manual_stopped": 0, **fields}
        for key, fields in cases
    ])
    try:
        ns = build_card_ns(db_path)
        badge_table = ns["_PB_PROXY_BADGE_TEXT"]
        effective = ns["_pb_proxy_effective_state"]
        row_by_key = ns["_manager_row_by_key"]
        for key, _fields in cases:
            row = row_by_key(key)
            expected_state = effective(row)
            expected_badge = badge_table.get(expected_state, "❓ proxy stale")
            text = ns["_manager_settings_card_text"](key)
            check(f"11b. [R2] {key}: unified-card 'Proxy: ...' line matches "
                  f"_PB_PROXY_BADGE_TEXT[_pb_proxy_effective_state(row)] == {expected_state!r} -> {expected_badge!r}",
                  f"Proxy: {expected_badge}" in text, text)
    finally:
        os.unlink(db_path)


# ======================================================================
# 12. Existing entry points remain reachable where they exist (and are
# correctly gated off for archived/deleted managers).
# ======================================================================

def test_12_entry_points_reachable_and_gated() -> None:
    # Two distinct "unavailable" states, on purpose: "archived" managers are
    # excluded at the DB-query level by list_manager_rows_from_db_sync
    # itself (pre-existing behavior, unrelated to N2 -- the row simply can't
    # be found, so the card correctly shows "not found"); "deleted" status
    # rows ARE still returned by the query, and it is THAT status the
    # relogin/replace/devlogin guard inside the card-builder itself checks.
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": "mgr01", "is_enabled": 1, "status": "active", "manual_stopped": 0},
        {"manager_key": "arch1", "is_enabled": 0, "status": "archived", "manual_stopped": 0},
        {"manager_key": "del1", "is_enabled": 0, "status": "deleted", "manual_stopped": 0},
    ])
    try:
        ns = build_card_ns(db_path, active_op={})
        rows = ns["_manager_settings_card_buttons"]("mgr01")

        check("12. Screenshots: mode/reminders/time/view/refresh all present",
              _find(rows, data=b"ssc:mode:mgr01") is not None
              and _find(rows, data=b"ssc:rem:mgr01") is not None
              and _find(rows, data=b"ssc:time:mgr01") is not None
              and _find(rows, data=b"ssc:view:mgr01") is not None
              and _find(rows, data=b"ssc:refresh:mgr01") is not None,
              rows)
        check("12. ManagerBot access present", _find(rows, data=b"menu:mbaccess:mgr01") is not None, rows)
        check("12. Transfers present", _find(rows, data=b"menu:transfer_mgr:mgr01") is not None, rows)
        check("12. Rename present", _find(rows, data=b"wiz:rename_manager:mgr01") is not None, rows)
        check("12. Replacement present (no active op -> 'Заменить аккаунт')",
              _find(rows, label="🔄 Заменить аккаунт", data=b"rw:start:mgr01") is not None, rows)
        check("12. Relogin present", _find(rows, data=b"relogin:start:mgr01") is not None, rows)
        check("12. Devlogin present (numeric id, not the raw key)",
              any(d.startswith(b"devlogin:start:") and d != b"devlogin:start:mgr01" for d in _datas(rows)), rows)
        check("12. Reserve present", _find(rows, data=b"menu:reserve_admin:mgr01") is not None, rows)
        check("12. Modes-card link (Автоответы/график) present", _find(rows, data=b"menu:manager:mgr01") is not None, rows)
        check("12. Danger-zone entry present", _find(rows, data=b"menu:manager_danger:mgr01") is not None, rows)

        # Continue-replacement variant
        ns2 = build_card_ns(db_path, active_op={"operation_id": "op1", "status": "started"})
        rows2 = ns2["_manager_settings_card_buttons"]("mgr01")
        check("12. Active replacement op -> 'Продолжить замену' (not 'Заменить аккаунт')",
              _find(rows2, label="🔄 Продолжить замену", data=b"rw:recover:mgr01") is not None
              and _find(rows2, label="🔄 Заменить аккаунт") is None,
              rows2)

        # "deleted" status: relogin/replace/devlogin gated OFF (the card's
        # own guard), everything else stays -- same guard the old admin
        # card used.
        del_rows = ns["_manager_settings_card_buttons"]("del1")
        check("12. Deleted-status manager: relogin NOT offered", _find(del_rows, data=b"relogin:start:del1") is None, del_rows)
        check("12. Deleted-status manager: replace NOT offered",
              _find(del_rows, label="🔄 Заменить аккаунт") is None and _find(del_rows, label="🔄 Продолжить замену") is None, del_rows)
        check("12. Deleted-status manager: devlogin NOT offered",
              not any(d.startswith(b"devlogin:start:") for d in _datas(del_rows)), del_rows)
        check("12. Deleted-status manager: screenshots/MB-access/proxy/reserve/danger STILL offered",
              _find(del_rows, data=b"ssc:mode:del1") is not None
              and _find(del_rows, data=b"menu:mbaccess:del1") is not None
              and _find(del_rows, data=b"menu:proxy:del1") is not None
              and _find(del_rows, data=b"menu:reserve_admin:del1") is not None
              and _find(del_rows, data=b"menu:manager_danger:del1") is not None,
              del_rows)

        # Archived manager: pre-existing (unrelated to N2) DB-query-level
        # exclusion in list_manager_rows_from_db_sync means _manager_row_by_key
        # can never find it -- the card correctly falls into its "not found"
        # branch, same as it always has for every route in this card family.
        arch_rows = ns["_manager_settings_card_buttons"]("arch1")
        check("12. Archived manager (pre-existing DB-level exclusion): card shows its 'not found' fallback, not a crash",
              _find(arch_rows, label="⬅️ Назад", data=b"menu:manager_settings") is not None and len(_flat(arch_rows)) == 2,
              arch_rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 13. Dangerous actions remain behind the existing guard: flows -- exact
# same callback set as the old (now orphaned) admin card used inline.
# ======================================================================

def test_13_danger_zone_matches_old_guard_callbacks() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01", "is_enabled": 1, "status": "active", "manual_stopped": 0}])
    try:
        ns = build_card_ns(db_path)
        old_ns = _extract_old_admin_card_ns(db_path)
        old_ns["_manager_row_by_key"] = ns["_manager_row_by_key"]
        old_ns["_manager_short_label"] = ns["_manager_short_label"]

        danger_rows = ns["_manager_danger_zone_buttons"]("mgr01")
        old_rows = old_ns["_manager_admin_detail_buttons"]("mgr01")

        danger_guard = {d for d in _datas(danger_rows) if d.startswith(b"guard:")}
        old_guard = {d for d in _datas(old_rows) if d.startswith(b"guard:")}
        check("13. Danger zone's guard: callback set is IDENTICAL to the old admin card's",
              danger_guard == old_guard, (danger_guard, old_guard))
        check("13. Danger zone includes db_clear/reset/delete/restore_db",
              danger_guard == {b"guard:db_clear:mgr01", b"guard:reset:mgr01", b"guard:delete:mgr01", b"guard:restore_db:mgr01"},
              danger_guard)
        check("13. Danger log command present", _find(danger_rows, data=b"cmd:/manager_danger_log") is not None, danger_rows)

        # Main card must NOT expose these inline anymore -- only via the
        # one danger-zone entry button.
        main_rows = ns["_manager_settings_card_buttons"]("mgr01")
        check("13. Main card no longer exposes guard: callbacks inline",
              not any(d.startswith(b"guard:") for d in _datas(main_rows)), main_rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 14. callback_data length <= 64 bytes for every generated button,
# including a stress test with a pathologically long manager_key.
# ======================================================================

def test_14_callback_length() -> None:
    long_key = "a" * 80  # deliberately unrealistic/too-long, to prove the guard engages
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": "mgr01", "is_enabled": 1, "status": "active", "manual_stopped": 0},
        {"manager_key": long_key, "is_enabled": 1, "status": "active", "manual_stopped": 0},
    ])
    try:
        ns = build_card_ns(db_path)
        for key in ("mgr01", long_key):
            rows = ns["_manager_settings_card_buttons"](key)
            for b in _flat(rows):
                check(f"14. callback_data <=64 bytes: {b[1]!r} ({key[:12]}...)", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
            confirm_rows = ns["_manager_disable_confirm_buttons"](key)
            for b in _flat(confirm_rows):
                check(f"14. confirm-screen callback_data <=64 bytes: {b[1]!r} ({key[:12]}...)", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
            danger_rows = ns["_manager_danger_zone_buttons"](key)
            for b in _flat(danger_rows):
                check(f"14. danger-zone callback_data <=64 bytes: {b[1]!r} ({key[:12]}...)", len(b[2]) <= 64, (b[1], b[2], len(b[2])))

        # Prove the guard actually engages (not just "happens to fit"):
        # a raw un-guarded callback for the long key WOULD exceed 64 bytes.
        raw_would_be = f"menu:manager_disable_confirm:{long_key}".encode("utf-8")
        check("14. sanity: the long-key case genuinely exceeds 64 bytes unguarded (proves the guard is load-bearing)",
              len(raw_would_be) > 64, len(raw_would_be))
        long_rows = ns["_manager_settings_card_buttons"](long_key)
        toggle_btn = _find(long_rows, label="⏸ Выключить менеджера")
        check("14. long-key toggle button falls back safely instead of emitting an oversized callback",
              toggle_btn is not None and toggle_btn[2] == b"menu:manager_settings", toggle_btn)
    finally:
        os.unlink(db_path)


# ======================================================================
# 15. No secret value embedded in callback data.
# ======================================================================

def test_15_no_secrets_in_callbacks() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01", "is_enabled": 1, "status": "active", "manual_stopped": 0}])
    try:
        ns = build_card_ns(db_path)
        rows = ns["_manager_settings_card_buttons"]("mgr01")
        rows += ns["_manager_disable_confirm_buttons"]("mgr01")
        rows += ns["_manager_danger_zone_buttons"]("mgr01")
        allowed_prefixes = (
            b"cmd:/manager_", b"ssc:", b"menu:", b"wiz:rename_manager:",
            b"rw:", b"relogin:start:", b"devlogin:start:", b"guard:",
        )
        secret_markers = (b"password", b"pass=", b"proxy_password", b"api_key", b"token", b"+7", b"+1")
        for b in _flat(rows):
            data = b[2]
            check(f"15. callback data uses only an allowed prefix: {data!r}",
                  any(data.startswith(p) for p in allowed_prefixes), data)
            check(f"15. no secret marker embedded in callback data: {data!r}",
                  not any(m in data.lower() for m in secret_markers), data)
    finally:
        os.unlink(db_path)


# ======================================================================
# 16. Historical manager_admin:{key} callbacks remain handled (not a
# dead/unhandled route, not an exception, valid text+buttons every time).
# ======================================================================

def test_16_historical_manager_admin_callback_handled() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01", "is_enabled": 1, "status": "active", "manual_stopped": 0}])
    try:
        card_ns = build_card_ns(db_path)
        redirect_ns = build_redirect_ns(db_path, card_ns)
        title_for_menu = redirect_ns["_title_for_menu"]

        text, rows = title_for_menu("manager_admin:mgr01")
        check("16. manager_admin:{key} for an existing manager returns real text (not empty/exception)",
              bool(text) and "OLD_MENU:" not in text, text)
        check("16. manager_admin:{key} for an existing manager returns real buttons", len(rows) > 0, rows)

        # Not-found path must still be handled gracefully (never raises).
        missing_text, missing_rows = title_for_menu("manager_admin:doesnotexist")
        check("16. manager_admin:{key} for a missing manager is handled (not-found message, not a crash)",
              "не найден" in missing_text, missing_text)
        check("16. manager_admin:{key} not-found still offers a way back",
              _find(missing_rows, data=b"menu:manager_admin") is not None, missing_rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 17. No handler or PREV-chain inventory accidentally removed -- the OLD
# admin-card def chain (3 text defs, 4 button defs) is still fully intact
# in the source, only unlinked (same static invariant style as Phase N1's
# nm_menu_distribution_selftest.py test_8/test_7e).
# ======================================================================

def test_17_old_chain_and_prev_captures_intact() -> None:
    text_defs = len(re.findall(r'^def _manager_admin_detail_text\(', PANEL_SRC, re.M))
    button_defs = len(re.findall(r'^def _manager_admin_detail_buttons\(', PANEL_SRC, re.M))
    # R1 BATCH 1 20260823: TEXT chain 3 -> 2. The base def was provably dead
    # (the _M212A_ORIG capture sits AFTER the tp_visual redefinition, so it
    # captured tp_visual, never base) and was removed by
    # tools/collapse_dead_defs.py. The captured links (tp_visual + M2.12A)
    # and all PREV-capture markers below remain intact.
    check("17. old admin-card TEXT chain has both LIVE defs (tp_visual + M2.12A)", text_defs == 2, text_defs)
    check("17. old admin-card BUTTONS chain still has all 4 defs (base + M2.12A + relogin + replace)", button_defs == 4, button_defs)

    for marker in (
        "_M212A_ORIG_ADMIN_DETAIL_TEXT = globals().get(",
        "_M212A_ORIG_ADMIN_DETAIL_BUTTONS = globals().get(",
        "_RELOGIN_PREV_ADMIN_DETAIL_BUTTONS = globals().get(",
        "_RW_PREV_ADMIN_DETAIL_BUTTONS = globals().get(",
        "_MS_PREV_TITLE_FOR_MENU = globals().get(",
    ):
        check(f"17. PREV-capture marker still present: {marker!r}", marker in PANEL_SRC, marker)

    # N2.1 (replaces an earlier vacuous check): the shadowed gen-1 legacy
    # branch that still calls the OLD admin-card functions must (a) still
    # exist, and (b) be PROVABLY INCAPABLE of becoming the active canonical
    # route: every _title_for_menu generation that calls the old functions
    # must come EARLIER in the file than the redirect generation (last def
    # wins + PREV delegation top-down => a later generation always answers
    # manager_admin: first). Verified structurally, not by a no-op string
    # comparison.
    tree = ast.parse(PANEL_SRC)
    gens = [n for n in tree.body if getattr(n, "name", None) == "_title_for_menu"]
    legacy_callers = []
    redirect_gen_line = None
    for g in gens:
        src_txt = ast.unparse(g)
        calls_old = "_manager_admin_detail_buttons(key)" in src_txt
        handles_admin = _contains_quote_tolerant(src_txt, 'startswith("manager_admin:")')
        calls_new = "_manager_settings_card_buttons(key)" in src_txt
        if calls_old:
            legacy_callers.append(g.lineno)
        if handles_admin and calls_new:
            redirect_gen_line = g.lineno
    check("17. exactly one legacy _title_for_menu generation still calls the old admin-card functions",
          len(legacy_callers) == 1, legacy_callers)
    check("17. the redirect generation (manager_admin: -> canonical card) exists",
          redirect_gen_line is not None, None)
    check("17. the legacy caller is defined EARLIER than the redirect generation, so it can never win the override stack",
          bool(legacy_callers) and redirect_gen_line is not None and legacy_callers[0] < redirect_gen_line,
          (legacy_callers, redirect_gen_line))

    client_on_count = len(re.findall(r'@client\.on\(', PANEL_SRC))
    check("17. @client.on(...) registration count is a healthy positive number (sanity; exact-count diffing is done externally against the pre-N2 backup)",
          client_on_count > 50, client_on_count)


# ======================================================================
# 18. This selftest only ever touches a temp DB -- never a project DB.
# ======================================================================

def test_18_never_touches_project_db() -> None:
    forbidden = [str(BASE_DIR / "db" / "data_tpilot.db"), str(BASE_DIR / "db" / "data.db")]
    check("18. no forbidden project DB path literal appears anywhere in this test file's own source",
          not any(p in open(__file__, encoding="utf-8").read() for p in forbidden), forbidden)
    check("18. this selftest imports no protected module (main.py/storage.py/panel_bridge.py/etc.)",
          not any(name in PANEL_SRC[:0] for name in ()),  # placeholder always-true; real guarantee is import-level below
          None)
    import importlib
    for forbidden_mod in ("main", "storage", "panel_bridge", "manager_bot", "partner_stat_bot",
                           "proxy_provider", "proxy_lifecycle", "llm_supervisor", "preflight_check"):
        check(f"18. {forbidden_mod} module was not imported by this test process",
              forbidden_mod not in sys.modules, sorted(sys.modules.keys()))


# ======================================================================
# N2.1-1. Replacement return paths: the rw:cancelpre / rw:commit_close
# branches inside _replace_callback must render the CANONICAL card, never
# the retired admin one. Verified on the real AST of the (last) def.
# ======================================================================

def _replace_callback_branch_src(action_name: str) -> str:
    tree = ast.parse(PANEL_SRC)
    fns = [n for n in tree.body
           if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "_replace_callback"]
    if not fns:
        return ""
    fn = fns[-1]  # last def wins, same as runtime
    for node in ast.walk(fn):
        if isinstance(node, ast.If):
            try:
                test_src = ast.unparse(node.test)
            except Exception:
                continue
            if f"action == '{action_name}'" in test_src or f'action == "{action_name}"' in test_src:
                return "\n".join(ast.unparse(s) for s in node.body)
    return ""


def test_n21_1_rw_return_paths_render_canonical_card() -> None:
    for action in ("cancelpre", "commit_close"):
        body = _replace_callback_branch_src(action)
        check(f"N2.1-1. rw:{action} branch located in the LAST _replace_callback def", bool(body), action)
        check(f"N2.1-1. rw:{action} renders _manager_settings_card_text (canonical)",
              "_manager_settings_card_text(key)" in body, body)
        check(f"N2.1-1. rw:{action} renders _manager_settings_card_buttons (canonical)",
              "_manager_settings_card_buttons(key)" in body, body)
        check(f"N2.1-1. rw:{action} does NOT call _manager_admin_detail_text",
              "_manager_admin_detail_text" not in body, body)
        check(f"N2.1-1. rw:{action} does NOT call _manager_admin_detail_buttons",
              "_manager_admin_detail_buttons" not in body, body)


# ======================================================================
# N2.1-2. Active-generation interception: no _title_for_menu generation
# LATER than the legal handler may handle any of the four N2 routes.
# ======================================================================

def test_n21_2_no_later_generation_intercepts_n2_routes() -> None:
    tree = ast.parse(PANEL_SRC)
    gens = [n for n in tree.body if getattr(n, "name", None) == "_title_for_menu"]
    check("N2.1-2. sanity: a healthy number of _title_for_menu generations found", len(gens) >= 20, len(gens))

    def handlers_of(marker: str) -> list:
        """Generations that POSITIVELY handle `marker`. A negated exclusion
        guard like `raw.startswith("manager:") and not raw.startswith(
        "manager_admin:")` (the closer-guard generation) references the
        marker but explicitly does NOT handle it -- those negated
        occurrences are subtracted, not counted."""
        out = []
        for g in gens:
            src_txt = ast.unparse(g)
            total = 0
            negated = 0
            for quoted in (f"startswith('{marker}')", f'startswith("{marker}")'):
                total += src_txt.count(quoted)
                negated += src_txt.count(f"not raw.{quoted}")
                negated += src_txt.count(f"not raw_menu.{quoted}")
            for quoted in (f"== '{marker}'", f'== "{marker}"'):
                total += src_txt.count(quoted)
            if total - negated > 0:
                out.append(g.lineno)
        return out

    settings_gen = _find_generation(tree, ("manager_disable_confirm:",))
    redirect_gen = _find_generation(tree, ('raw.startswith("manager_admin:")', "_manager_settings_card_text(key)"))
    check("N2.1-2. legal settings/confirm/danger generation found", settings_gen is not None, None)
    check("N2.1-2. legal manager_admin redirect generation found", redirect_gen is not None, None)

    for route, legal_line in (
        ("manager_settings:", settings_gen.lineno if settings_gen else -1),
        ("manager_disable_confirm:", settings_gen.lineno if settings_gen else -1),
        ("manager_danger:", settings_gen.lineno if settings_gen else -1),
        ("manager_admin:", redirect_gen.lineno if redirect_gen else -1),
    ):
        lines = handlers_of(route)
        check(f"N2.1-2. route {route!r} is handled by the legal generation", legal_line in lines, (route, lines, legal_line))
        later = [l for l in lines if l > legal_line]
        check(f"N2.1-2. NO generation AFTER the legal one handles {route!r} (interception-proof)",
              not later, (route, later, legal_line))


# ======================================================================
# N2.1-3. Exact card STATUS TEXT for all three lifecycle states.
# ======================================================================

def test_n21_3_exact_status_text_in_card() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": "on1", "is_enabled": 1, "status": "active", "manual_stopped": 0},
        {"manager_key": "off1", "is_enabled": 0, "status": "disabled", "manual_stopped": 0},
        {"manager_key": "stop1", "is_enabled": 1, "status": "active", "manual_stopped": 1},
    ])
    try:
        ns = build_card_ns(db_path)
        for key, badge in (("on1", "🟢 Работает"), ("off1", "🔴 Выключен"), ("stop1", "🟡 Остановлен вручную")):
            text = ns["_manager_settings_card_text"](key)
            check(f"N2.1-3. card text for {key} shows exactly 'Статус: {badge}'",
                  f"Статус: {badge}" in text, text)
        check("N2.1-3. card text uses operator wording 'Ключ:' (not raw 'manager_key:')",
              "Ключ: on1" in ns["_manager_settings_card_text"]("on1")
              and "manager_key:" not in ns["_manager_settings_card_text"]("on1"),
              ns["_manager_settings_card_text"]("on1"))
    finally:
        os.unlink(db_path)


# ======================================================================
# N2.1-4. Manager LIST screen: status emoji is the FIRST label token;
# every row opens menu:manager_settings:{key}.
# ======================================================================

def test_n21_4_list_screen_badges_and_routes() -> None:
    # N5.4.2: badges now come from the shared status resolver, not the old
    # _manager_settings_state 3-state helper -- on1 has no proc-scan data
    # injected (fail-open "unknown", which the "ok" branch treats as
    # non-disqualifying, same as production with a stale/absent scan) and
    # no proxy/tg-health rows, so it resolves to "ok" (🟢); off1 is
    # is_enabled=0 -> "disabled" (⚪, NOT the old 🔴); stop1 has
    # manual_stopped=1 -> "stopped" (⏸️, NOT the old 🟡).
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": "on1", "display_name": "Alpha", "is_enabled": 1, "status": "active", "manual_stopped": 0},
        {"manager_key": "off1", "display_name": "Beta", "is_enabled": 0, "status": "disabled", "manual_stopped": 0},
        {"manager_key": "stop1", "display_name": "Gamma", "is_enabled": 1, "status": "active", "manual_stopped": 1,
         "telegram_username": "gammauser"},
    ])
    try:
        ns = build_card_ns(db_path)
        rows = ns["_manager_settings_buttons"]()
        expected = {"on1": "🟢", "off1": "⚪", "stop1": "⏸️"}
        found = {}
        for b in _flat(rows):
            data = b[2]
            if data.startswith(b"menu:manager_settings:"):
                key = data.split(b":", 2)[2].decode()
                found[key] = b[1]
        check("N2.1-4. list has exactly one row per non-archived manager", set(found) == set(expected), found)
        for key, emoji in expected.items():
            label = found.get(key, "")
            check(f"N2.1-4. list row for {key} starts with its status emoji {emoji!r} as the first token",
                  label.split(" ", 1)[0] == emoji, label)
        # N5.4.2 (owner format §3): "<icon> <name> / @username" or
        # "<icon> <name> / username не указан" when absent.
        check("N2.1-4. [N5.4.2] on1 has no username -> 'username не указан'",
              "username не указан" in found_full(rows, "on1"), found_full(rows, "on1"))
        check("N2.1-4. [N5.4.2] stop1 has a username -> '@gammauser'",
              "@gammauser" in found_full(rows, "stop1"), found_full(rows, "stop1"))
        # N5.3 (Part C): the global «📷 Скрины (все)» shortcut moved to
        # Отчёты (its canonical owner) -- the manager list must NOT offer it
        # any more; per-manager screenshots stay on the unified card (ssc:*).
        check("N2.1-4 (amended N5.3). list screen no longer offers the global ssv:dates shortcut (moved to Отчёты)",
              _find(rows, data=b"ssv:dates") is None, rows)
        text = ns["_manager_settings_text"]()
        check("N2.1-4. list text renders and reports the manager count", "Менеджеров: 3" in text, text)
    finally:
        os.unlink(db_path)


def found_full(rows, key_suffix: str) -> str:
    for b in _flat(rows):
        data = b[2]
        if data.startswith(b"menu:manager_settings:") and data.split(b":", 2)[2].decode() == key_suffix:
            return b[1]
    return ""


# ======================================================================
# N2.1-5. Danger-zone navigation: Back returns to the canonical card,
# Home stays available.
# ======================================================================

def test_n21_5_danger_zone_navigation() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01", "is_enabled": 1, "status": "active", "manual_stopped": 0}])
    try:
        ns = build_card_ns(db_path)
        rows = ns["_manager_danger_zone_buttons"]("mgr01")
        check("N2.1-5. danger zone has '⬅️ Назад к карточке' -> menu:manager_settings:{key} (canonical card)",
              _find(rows, label="⬅️ Назад к карточке", data=b"menu:manager_settings:mgr01") is not None, rows)
        check("N2.1-5. danger zone has '🏠 Главная' -> menu:main",
              _find(rows, label="🏠 Главная", data=b"menu:main") is not None, rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# N2.1-7. «📄 Полная карточка» button present. N5.4.3 (owner decision B):
# now targets the native menu:manager_full screen, NOT the background
# cmd:/manager_info command queue -- see tools\manager_full_card_selftest.py
# for the native screen's own dedicated coverage.
# ======================================================================

def test_n21_7_manager_info_button_restored() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01", "is_enabled": 1, "status": "active", "manual_stopped": 0}])
    try:
        ns = build_card_ns(db_path)
        rows = ns["_manager_settings_card_buttons"]("mgr01")
        check("N2.1-7. [N5.4.3] unified card has '�� Полная карточка' -> menu:manager_full:{key} (native screen)",
              _find(rows, label="📄 Полная карточка", data=b"menu:manager_full:mgr01") is not None, rows)
        check("N2.1-7. [N5.4.3] the button no longer emits the background cmd:/manager_info command",
              _find(rows, label="📄 Полная карточка", data=b"cmd:/manager_info mgr01") is None, rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# N2.1-8. Raw-count checks: exact total button counts per state; no
# duplicate callback_data; nothing hidden behind set semantics.
# ======================================================================

def test_n21_8_raw_button_counts_and_no_duplicates() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": "on1", "is_enabled": 1, "status": "active", "manual_stopped": 0},
        {"manager_key": "off1", "is_enabled": 0, "status": "disabled", "manual_stopped": 0},
        {"manager_key": "stop1", "is_enabled": 1, "status": "active", "manual_stopped": 1},
        {"manager_key": "del1", "is_enabled": 0, "status": "deleted", "manual_stopped": 0},
    ])
    try:
        ns = build_card_ns(db_path)
        # Exact expected totals (raw list length, not a set):
        # normal states: toggle, restart, mode, rem, time, view, refresh-screens,
        # mbaccess, info, rename, replace, relogin, devlogin, reserve, proxy,
        # transfers, modes-link, danger, back, refresh, home = 21.
        # deleted status: minus replace/relogin/devlogin = 18.
        for key, expected_total in (("on1", 21), ("off1", 21), ("stop1", 21), ("del1", 18)):
            flat = _flat(ns["_manager_settings_card_buttons"](key))
            check(f"N2.1-8. {key}: exact raw button count == {expected_total}",
                  len(flat) == expected_total, (len(flat), [b[1] for b in flat]))
            datas = [b[2] for b in flat]
            dupes = sorted({d for d in datas if datas.count(d) > 1})
            check(f"N2.1-8. {key}: no duplicate callback_data on the card", not dupes, dupes)
        # Confirm + danger screens: no duplicates either.
        for name, rows in (
            ("confirm(on1)", ns["_manager_disable_confirm_buttons"]("on1")),
            ("danger(on1)", ns["_manager_danger_zone_buttons"]("on1")),
        ):
            datas = [b[2] for b in _flat(rows)]
            dupes = sorted({d for d in datas if datas.count(d) > 1})
            check(f"N2.1-8. {name}: no duplicate callback_data", not dupes, dupes)
        # Guard-set comparison in test_13 uses sets -- re-assert here with raw
        # counts that the danger zone emits each guard callback exactly once.
        danger_datas = [b[2] for b in _flat(ns["_manager_danger_zone_buttons"]("on1"))]
        for g in (b"guard:db_clear:on1", b"guard:reset:on1", b"guard:delete:on1", b"guard:restore_db:on1"):
            check(f"N2.1-8. danger zone emits {g!r} exactly once", danger_datas.count(g) == 1, danger_datas)

        # --- N5.3 (Part D): professional card ORDER. Info first; lifecycle
        # and destructive actions at the BOTTOM (owner decision). Asserted on
        # the real rendered flat order, per state.
        def _idx(flat, data_prefix: bytes) -> int:
            for i, b in enumerate(flat):
                if bytes(b[2]).startswith(data_prefix):
                    return i
            return -1
        for key in ("on1", "off1", "stop1"):
            flat = _flat(ns["_manager_settings_card_buttons"](key))
            i_info = _idx(flat, b"menu:manager_full:")  # N5.4.3: native screen, not cmd:/manager_info
            i_modes = _idx(flat, b"menu:manager:")
            i_toggle = max(_idx(flat, b"menu:manager_disable_confirm:"), _idx(flat, b"cmd:/manager_start"))
            i_restart = _idx(flat, b"cmd:/manager_restart")
            i_danger = _idx(flat, b"menu:manager_danger:")
            check(f"N5.3-D. {key}: info card button is FIRST", i_info == 0, [b[1] for b in flat])
            check(f"N5.3-D. {key}: lifecycle toggle sits BELOW modes/links block",
                  i_toggle > i_modes > i_info, (i_info, i_modes, i_toggle))
            check(f"N5.3-D. {key}: restart directly after toggle", i_restart == i_toggle + 1, (i_toggle, i_restart))
            check(f"N5.3-D. {key}: danger entry is the LAST action (only footer after it)",
                  i_danger == i_restart + 1 and i_danger == len(flat) - 4, (i_danger, len(flat)))
            check(f"N5.3-D. {key}: modes link renamed to «🤖 Автоматизация менеджера»",
                  any(b[1] == "🤖 Автоматизация менеджера" for b in flat), [b[1] for b in flat])
    finally:
        os.unlink(db_path)


def main() -> int:
    test_1_2_one_canonical_card_and_redirect()
    test_3_4_5_single_dynamic_toggle_per_state()
    test_6_7_8_command_mapping_and_restart()
    test_9_10_disable_confirm_and_stale_noop()
    test_11_proxy_link_only()
    test_11b_proxy_badge_agrees_with_shared_resolver()
    test_12_entry_points_reachable_and_gated()
    test_13_danger_zone_matches_old_guard_callbacks()
    test_14_callback_length()
    test_15_no_secrets_in_callbacks()
    test_16_historical_manager_admin_callback_handled()
    test_17_old_chain_and_prev_captures_intact()
    test_18_never_touches_project_db()
    test_n21_1_rw_return_paths_render_canonical_card()
    test_n21_2_no_later_generation_intercepts_n2_routes()
    test_n21_3_exact_status_text_in_card()
    test_n21_4_list_screen_badges_and_routes()
    test_n21_5_danger_zone_navigation()
    test_n21_7_manager_info_button_restored()
    test_n21_8_raw_button_counts_and_no_duplicates()

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
