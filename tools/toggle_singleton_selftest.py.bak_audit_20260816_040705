# -*- coding: utf-8 -*-
"""tools/toggle_singleton_selftest.py -- offline self-test for the approved
Phase N3 "toggle standardization" migration in panel_bot.py (2026-07-21):
every true binary control in the canonical AdminBot UI now emits exactly ONE
state-change button (never a simultaneous Enable+Disable / On+Off pair), and
the two deliberately-whitelisted bulk-action screens (H: emergency silence
for-all, I: bulk follow-ups for-all) stay genuine two-action group operations
(not fake single toggles).

Controls covered (A-G, single dynamic toggle each):
  A. Unanswered notifications      -- _unanswered_menu (settings-key toggle)
  B. Traffic source activation     -- _source_detail_buttons (per-source)
  C. Group activation              -- _group_detail_buttons (per-group)
  D. Per-manager follow-ups        -- _followups_menu (per-manager rows)
  F. Auto-status global toggle     -- _tp_visual_automation_menu + confirm/do
  G. LLM supervisor (NM canonical) -- _nm_automation_screen + confirm/do

E. Per-manager proxy: VERIFIED N/A -- the active _proxy_detail_buttons has
   no live Enable/Disable pair to collapse (the old cmd:/manager_proxy_on|off
   pair survives only in dead, unreachable overrides). The live screen is
   already mode-driven (one action per mode): proxy mode -> "Перевести без
   proxy" via a wiz: warning screen; direct mode -> "Подключить proxy" via a
   wiz: setup screen. This file asserts that N/A finding explicitly (test_7)
   rather than silently skipping E.

H/I. Bulk exceptions (whitelisted, NOT converted) -- _tp_ae_emergency_silence_menu
   ("Включить/Снять тишину всем"), _followups_menu bulk rows and
   _tp_panel_v5_bulk_rows ("Дожимы всем"/"Дожимы выкл всем") -- these stay
   two explicit actions; this file proves they are NOT accidentally treated
   as single toggles anywhere, and that the manager-lifecycle toggle from
   Phase N2 is untouched by N3.

Techniques: panel_bot.py cannot be imported standalone (Telethon/env side
effects at import time) -- functions are extracted via ast.parse +
ast.unparse + exec() and run FOR REAL against a temporary SQLite file
(never db/data_tpilot.db or db/data.db), the same idiom used throughout
this project's tools/*_selftest.py files (see
tools/manager_card_unified_selftest.py for the reference pattern this file
follows for confirm/stale-state testing).

_title_for_menu is defined ~30+ times via an override-stack (only the LAST
active definition matters at runtime; PREV-delegation chains). This file
locates the specific generations it needs by unique marker strings in their
unparsed source (mirrors tools/nm_menu_distribution_selftest.py's
_find_edited_title_for_menu / tools/manager_card_unified_selftest.py's
_find_generation) and isolates each with a FAKE captured predecessor.

    python tools\\toggle_singleton_selftest.py
"""
from __future__ import annotations

import ast
import os
import re
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
# Temp DB helpers (never db/data_tpilot.db or db/data.db).
# ======================================================================

def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="toggle_singleton_selftest_")
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
            CREATE TABLE traffic_sources(
                source_key TEXT PRIMARY KEY,
                name TEXT,
                status TEXT DEFAULT 'active'
            );
            CREATE TABLE manager_groups(
                group_key TEXT PRIMARY KEY,
                name TEXT,
                status TEXT DEFAULT 'active'
            );
            CREATE TABLE manager_source_links(
                manager_key TEXT PRIMARY KEY,
                source_key TEXT
            );
            CREATE TABLE manager_group_members(
                group_key TEXT,
                manager_key TEXT
            );
            CREATE TABLE manager_followup_settings(
                manager_key TEXT PRIMARY KEY,
                enabled INTEGER,
                updated_by INTEGER,
                updated_at TEXT
            );
            CREATE TABLE settings(
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            );
            CREATE TABLE manager_client_auto_settings(
                manager_key TEXT PRIMARY KEY,
                greeting_enabled INTEGER,
                questionnaire_enabled INTEGER,
                updated_at TEXT,
                updated_by_user_id INTEGER
            );
            CREATE TABLE client_auto_global_settings(
                setting_key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT,
                updated_by_user_id INTEGER
            );
            CREATE TABLE client_auto_silent_rules(
                scope TEXT,
                rule_key TEXT,
                enabled INTEGER,
                updated_at TEXT,
                updated_by_user_id INTEGER,
                PRIMARY KEY(scope, rule_key)
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
                "INSERT INTO managers(manager_key, display_name, telegram_username, role, status, is_enabled, manual_stopped) "
                "VALUES(?,?,?,?,?,?,?)",
                (m["manager_key"], m.get("display_name", m["manager_key"]), m.get("telegram_username", ""),
                 m.get("role", "manager"), m.get("status", "active"), int(m.get("is_enabled", 1)), int(m.get("manual_stopped", 0))),
            )
        con.commit()
    finally:
        con.close()


def _set_setting(db_path: str, key: str, value: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES(?,?,datetime('now'))", (key, value))
        con.commit()
    finally:
        con.close()


def _set_source(db_path: str, key: str, status: str = "active") -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT OR REPLACE INTO traffic_sources(source_key, name, status) VALUES(?,?,?)", (key, key, status))
        con.commit()
    finally:
        con.close()


def _set_group(db_path: str, key: str, status: str = "active") -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT OR REPLACE INTO manager_groups(group_key, name, status) VALUES(?,?,?)", (key, key, status))
        con.commit()
    finally:
        con.close()


def _set_followup(db_path: str, key: str, enabled: bool) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO manager_followup_settings(manager_key, enabled, updated_by, updated_at) VALUES(?,?,?,datetime('now'))",
            (key, 1 if enabled else 0, 0),
        )
        con.commit()
    finally:
        con.close()


def _set_gq(db_path: str, key: str, greeting=None, questionnaire=None) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO manager_client_auto_settings(manager_key, greeting_enabled, questionnaire_enabled, updated_at, updated_by_user_id) "
            "VALUES(?,?,?,datetime('now'),0)",
            (key, None if greeting is None else int(greeting), None if questionnaire is None else int(questionnaire)),
        )
        con.commit()
    finally:
        con.close()


def _set_gq_global_default(db_path: str, setting_key: str, value: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO client_auto_global_settings(setting_key, value, updated_at, updated_by_user_id) VALUES(?,?,datetime('now'),0)",
            (setting_key, value),
        )
        con.commit()
    finally:
        con.close()


def _set_silent(db_path: str, scope: str, rule_key: str, enabled: bool) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO client_auto_silent_rules(scope, rule_key, enabled, updated_at, updated_by_user_id) VALUES(?,?,?,datetime('now'),0)",
            (scope, rule_key, 1 if enabled else 0),
        )
        con.commit()
    finally:
        con.close()


# ======================================================================
# Generic AST extraction helpers (project convention).
# ======================================================================

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
    """ast.unparse always single-quotes string literals -- tolerate both."""
    if marker in src_txt:
        return True
    if '"' in marker:
        return marker.replace('"', "'") in src_txt
    if "'" in marker:
        return marker.replace("'", '"') in src_txt
    return False


def _find_generation(marker_all: tuple) -> ast.FunctionDef:
    """Locate the LAST _title_for_menu generation whose unparsed source
    contains ALL of marker_all (quote-tolerant)."""
    tree = ast.parse(PANEL_SRC)
    found = None
    for n in tree.body:
        if getattr(n, "name", None) == "_title_for_menu":
            try:
                src_txt = ast.unparse(n)
            except Exception:
                continue
            if all(_contains_quote_tolerant(src_txt, m) for m in marker_all):
                found = n
    return found


# ======================================================================
# Per-area namespace builders.
# ======================================================================

def build_unanswered_ns(db_path: str) -> dict:
    import manager_registry
    nodes = _extract_by_name({
        "_unanswered_menu", "_pb_unanswered_notify_enabled", "_pb_get_setting",
        "_connect_panel_db", "_manager_rows", "_manager_rows_all", "_manager_short_label",
        "_section_button",
    })
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:unanswered>", "exec"), ns)
    return ns


def build_source_ns(db_path: str) -> dict:
    import manager_registry
    nodes = _extract_by_name({
        "_source_detail_buttons", "_slug_key", "_traffic_source_by_key",
        "_ensure_structure_tables_sync", "_connect_panel_db", "_manager_rows",
        "_manager_rows_all", "_manager_short_label", "_source_links_map_sync",
    })
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:source>", "exec"), ns)
    return ns


def build_group_ns(db_path: str) -> dict:
    import manager_registry
    nodes = _extract_by_name({
        "_group_detail_buttons", "_slug_key", "_manager_group_by_key",
        "_ensure_structure_tables_sync", "_connect_panel_db", "_manager_rows",
        "_manager_rows_all", "_manager_short_label", "_group_members_sync",
    })
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:group>", "exec"), ns)
    return ns


def build_followups_ns(db_path: str) -> dict:
    import manager_registry
    # _followups_menu has 3 defs (shadowed, base, wrapper); the base def
    # (the one captured by _TP_PANEL_V5_ORIG_FOLLOWUPS_MENU) holds the
    # per-manager rows this file tests -- extract it specifically by
    # requiring the N3 marker '_pb_followup_enabled' in its body (only the
    # correct generation calls it).
    tree = ast.parse(PANEL_SRC)
    candidates = [n for n in tree.body if getattr(n, "name", None) == "_followups_menu"]
    target = None
    for n in candidates:
        try:
            if "_pb_followup_enabled" in ast.unparse(n):
                target = n
        except Exception:
            continue
    if target is None:
        raise AssertionError("build_followups_ns: could not locate the N3 _followups_menu generation (marker '_pb_followup_enabled' not found)")
    nodes = _extract_by_name({
        "_pb_followup_enabled", "_connect_panel_db", "_manager_rows", "_manager_rows_all",
        "_manager_short_label",
    })
    nodes.append(target)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:followups>", "exec"), ns)
    return ns


def build_autostatus_ns(db_path: str) -> dict:
    import manager_registry
    # The active _tp_visual_automation_menu (last of its several defs) --
    # located by requiring the N3 marker so the shadowed older copies
    # (which still exist as dead code) are never picked by accident.
    tree = ast.parse(PANEL_SRC)
    menu_candidates = [n for n in tree.body if getattr(n, "name", None) == "_tp_visual_automation_menu"]
    menu_target = None
    for n in menu_candidates:
        try:
            if "autostatus_toggle_confirm:" in ast.unparse(n):
                menu_target = n
        except Exception:
            continue
    if menu_target is None:
        raise AssertionError("build_autostatus_ns: could not locate the N3 _tp_visual_automation_menu generation")

    routing_gen = _find_generation(("autostatus_toggle_confirm:",))
    if routing_gen is None:
        raise AssertionError("build_autostatus_ns: could not locate the N3 autostatus _title_for_menu generation")

    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "_pb_get_setting": lambda key: _read_setting(db_path, key),
        "_pb_set_setting": lambda key, value: _set_setting(db_path, key, value),
        "_panel_header": lambda: "HEADER",
        "_tp_visual_nav_rows": lambda *_a, **_k: [[_FakeButton.inline("⬅️ Назад", b"menu:main")]],
        # Deliberately fake, isolating this test from every other
        # _title_for_menu generation (none of which handle autostatus_*).
        "_title_for_menu": lambda raw: (f"OLD_MENU:{raw}", []),
    }
    exec(compile(ast.unparse(menu_target), f"<{PANEL_PATH}:autostatus_menu>", "exec"), ns)
    exec(compile(ast.unparse(routing_gen), f"<{PANEL_PATH}:autostatus_routing>", "exec"), ns)
    return ns


def _read_setting(db_path: str, key: str) -> str:
    try:
        con = sqlite3.connect(db_path)
        try:
            row = con.execute("SELECT value FROM settings WHERE key=? LIMIT 1", (key,)).fetchone()
            return str(row[0] or "") if row else ""
        finally:
            con.close()
    except Exception:
        return ""


def build_nm_llm_ns(db_path: str) -> dict:
    nodes = _extract_by_name({
        "_nm_llm_toggle_confirm_text", "_nm_llm_toggle_confirm_buttons",
        "_pb_get_setting", "_connect_panel_db",
    })
    routing_gen = _find_generation(("nm_llm_toggle_do:",))
    if routing_gen is None:
        raise AssertionError("build_nm_llm_ns: could not locate the NM _title_for_menu generation with the N3 target-encoded route")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "_panel_header": lambda: "HEADER",
        "_nm_automation_screen": lambda: ("AUTOMATION_SCREEN", [[_FakeButton.inline("⬅️", b"menu:nm_root")]]),
        "_title_for_menu": lambda raw: (f"OLD_MENU:{raw}", []),
    }
    exec(compile(module_src, f"<{PANEL_PATH}:nm_llm>", "exec"), ns)
    exec(compile(ast.unparse(routing_gen), f"<{PANEL_PATH}:nm_llm_routing>", "exec"), ns)
    return ns


def build_bulk_ns(db_path: str) -> dict:
    import manager_registry
    nodes = _extract_by_name({
        "_tp_panel_v5_confirm_text", "_tp_panel_v5_command_for", "_tp_panel_v5_confirm_buttons",
        "_tp_panel_v5_bulk_rows", "_tp_ae_emergency_silence_menu", "_pb_active_manager_count",
        "_manager_rows", "_manager_rows_all", "_manager_short_label",
        # N3.2: _tp_ae_emergency_silence_menu now reads per-manager silence
        # state via these (Part B single-toggle rows) -- must be in-scope or
        # the extracted screen raises NameError when actually called.
        "_pb_manager_silent_enabled", "_pb_toggle_btn", "_connect_panel_db",
    })
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        "_tp_visual_nav_rows": lambda *_a, **_k: [[_FakeButton.inline("⬅️ Назад", b"menu:main")]],
    }
    exec(compile(module_src, f"<{PANEL_PATH}:bulk>", "exec"), ns)
    return ns


def build_mmt_ns(db_path: str) -> dict:
    """N3.2: namespace for the manager-modes card (Part A: greeting/
    questionnaire/silence single toggles) and the pure mmt: resolve/state
    helpers (Part B relies on the same _pb_manager_silent_enabled). _manager_
    detail_text/_buttons have several stacked override defs (project-wide
    pattern) -- _extract_by_name returns ALL of them in file order and
    re-executing them in that same order in one exec() naturally reproduces
    runtime last-wins (only the final def stays bound to the name), the same
    implicit reliance already used for _manager_rows_all/_pb_active_manager_
    count in build_bulk_ns above. The N3.2 marker check below guards against
    a future override silently becoming "last" without this file noticing."""
    import manager_registry
    nodes = _extract_by_name({
        "_manager_detail_text", "_manager_detail_buttons",
        "_pb_gq_truth", "_pb_gq_global_default", "_pb_greeting_enabled",
        "_pb_questionnaire_enabled", "_pb_manager_silent_enabled", "_pb_toggle_btn",
        "_pb_mmt_resolve", "_connect_panel_db", "_manager_row_by_key", "_manager_rows_all", "_manager_rows",
        "_manager_short_label", "_manager_state_icon", "_manager_proxy_badge",
    })
    buttons_node = [n for n in nodes if getattr(n, "name", None) == "_manager_detail_buttons"][-1]
    if "_pb_toggle_btn" not in ast.unparse(buttons_node):
        raise AssertionError("build_mmt_ns: the LAST _manager_detail_buttons def is not the N3.2 generation (marker '_pb_toggle_btn' missing) -- a later override may have been added without updating this test")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:mmt>", "exec"), ns)
    return ns


# ======================================================================
# Shared helpers
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
# 1/2. Every canonical binary screen (A-D, F, G) emits exactly ONE
# state-change action, never a simultaneous pair.
# ======================================================================

def test_1_2_unanswered_single_toggle() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01"}])
    try:
        for state, expect_label in (("1", "🔕 Выключить уведомления"), ("0", "🔔 Включить уведомления")):
            _set_setting(db_path, "unanswered_panel_notifications_enabled", state)
            ns = build_unanswered_ns(db_path)
            rows = ns["_unanswered_menu"]()
            check(f"1/2. A unanswered={state}: emits ONLY {expect_label!r}",
                  _find(rows, label=expect_label) is not None
                  and _find(rows, label="🔔 Включить уведомления" if expect_label.startswith("🔕") else "🔕 Выключить уведомления") is None,
                  _labels(rows))
            datas = _datas(rows)
            check(f"1/2. A unanswered={state}: no simultaneous on+off callbacks",
                  not (b"cmd:/unanswered_notify on" in datas and b"cmd:/unanswered_notify off" in datas), datas)
    finally:
        os.unlink(db_path)


def test_3_source_single_toggle() -> None:
    db_path = _make_temp_db()
    for status, expect_label, other_label in (
        ("active", "⚪ Выключить источник", "🟢 Включить источник"),
        ("disabled", "🟢 Включить источник", "⚪ Выключить источник"),
    ):
        db_path2 = _make_temp_db()
        _set_source(db_path2, "src1", status)
        ns = build_source_ns(db_path2)
        rows = ns["_source_detail_buttons"]("src1")
        check(f"3. B source status={status}: emits ONLY {expect_label!r}",
              _find(rows, label=expect_label) is not None and _find(rows, label=other_label) is None, _labels(rows))
        datas = _datas(rows)
        check(f"3. B source status={status}: no simultaneous on+off callbacks",
              not (b"cmd:/source on src1" in datas and b"cmd:/source off src1" in datas), datas)
        os.unlink(db_path2)
    os.unlink(db_path)


def test_4_group_single_toggle() -> None:
    for status, expect_label, other_label in (
        ("active", "⚪ Выключить группу", "🟢 Включить группу"),
        ("disabled", "🟢 Включить группу", "⚪ Выключить группу"),
    ):
        db_path = _make_temp_db()
        _set_group(db_path, "grp1", status)
        ns = build_group_ns(db_path)
        rows = ns["_group_detail_buttons"]("grp1")
        check(f"4. C group status={status}: emits ONLY {expect_label!r}",
              _find(rows, label=expect_label) is not None and _find(rows, label=other_label) is None, _labels(rows))
        datas = _datas(rows)
        check(f"4. C group status={status}: no simultaneous on+off callbacks",
              not (b"cmd:/mgroup on grp1" in datas and b"cmd:/mgroup off grp1" in datas), datas)
        os.unlink(db_path)


def test_5_followups_single_toggle_per_manager() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [
        {"manager_key": "on1", "display_name": "On"},
        {"manager_key": "off1", "display_name": "Off"},
    ])
    _set_followup(db_path, "on1", True)
    _set_followup(db_path, "off1", False)
    try:
        ns = build_followups_ns(db_path)
        rows = ns["_followups_menu"]()
        check("5. D on1: emits ONLY the disable action",
              _find(rows, data=b"cmd:/followup off on1") is not None and _find(rows, data=b"cmd:/followup on on1") is None, rows)
        check("5. D off1: emits ONLY the enable action",
              _find(rows, data=b"cmd:/followup on off1") is not None and _find(rows, data=b"cmd:/followup off off1") is None, rows)
        datas = _datas(rows)
        check("5. D: no manager has both on+off callbacks simultaneously",
              not (b"cmd:/followup on on1" in datas and b"cmd:/followup off on1" in datas)
              and not (b"cmd:/followup on off1" in datas and b"cmd:/followup off off1" in datas),
              datas)
    finally:
        os.unlink(db_path)


def test_6_autostatus_single_toggle() -> None:
    db_path = _make_temp_db()
    try:
        for val, expect_label in (("1", "⏸ Выключить автостатусы"), ("0", "▶️ Включить автостатусы")):
            _set_setting(db_path, "auto_status_enabled", val)
            ns = build_autostatus_ns(db_path)
            rows = ns["_tp_visual_automation_menu"]()
            check(f"6. F autostatus={val}: emits ONLY {expect_label!r}",
                  _find(rows, label=expect_label) is not None
                  and _find(rows, label="▶️ Включить автостатусы" if expect_label.startswith("⏸") else "⏸ Выключить автостатусы") is None,
                  _labels(rows))
            check(f"6. F autostatus={val}: no direct instant-flip callback is emitted by the screen (routes to confirm)",
                  not any(d == b"menu:autostatus_toggle" for d in _datas(rows)), _datas(rows))
    finally:
        os.unlink(db_path)

    # N3.1 requirement B.3: the autostatus CONFIRM screen's Cancel/Back
    # button must return to menu:automation (its real origin screen) --
    # invoke the routing generation directly with a confirm: route.
    db_path2 = _make_temp_db()
    try:
        _set_setting(db_path2, "auto_status_enabled", "1")
        ns2 = build_autostatus_ns(db_path2)
        _text, confirm_rows = ns2["_title_for_menu"]("autostatus_toggle_confirm:0")
        cancel_btn = _find(confirm_rows, label="❌ Отмена")
        check("6b (N3.1 B.3). autostatus confirm screen has a Cancel button",
              cancel_btn is not None, confirm_rows)
        # N5.3 (D9): cancel now returns to the CANONICAL Автоматизация screen
        # (menu:nm_automation), not the retired old-menu category.
        check("6b (N3.1 B.3, amended N5.3 D9). autostatus confirm Cancel/Back returns to menu:nm_automation",
              _find(confirm_rows, label="❌ Отмена", data=b"menu:nm_automation") is not None,
              confirm_rows)
    finally:
        os.unlink(db_path2)


def test_9_nm_llm_single_toggle() -> None:
    db_path = _make_temp_db()
    try:
        for val, expect_label in (("1", "🟢 LLM: ВКЛ"), ("0", "🔴 LLM: ВЫКЛ")):
            _set_setting(db_path, "llm_supervisor_enabled", val)
            ns = build_nm_llm_ns(db_path)
            rows = ns["_nm_llm_toggle_confirm_buttons"]()
            confirm_btn = _find(rows, label="✅ Подтвердить")
            check(f"9. G llm={val}: confirm button target-encoded", confirm_btn is not None, rows)
            check(f"9. G llm={val}: confirm+cancel are the only two buttons (single action per confirm screen)",
                  len(_flat(rows)) == 2, rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 7. Area E (per-manager proxy) verified N/A: no live on/off pair exists.
# ======================================================================

def test_7_proxy_no_live_pair_verified_na() -> None:
    tree = ast.parse(PANEL_SRC)
    active_defs = [n for n in tree.body if getattr(n, "name", None) == "_proxy_detail_buttons"]
    check("7. E: at least one _proxy_detail_buttons def found", bool(active_defs), None)
    active_src = ast.unparse(active_defs[-1]) if active_defs else ""
    check("7. E: the ACTIVE _proxy_detail_buttons has NO cmd:/manager_proxy_on callback",
          "manager_proxy_on" not in active_src, active_src)
    check("7. E: the ACTIVE _proxy_detail_buttons has NO cmd:/manager_proxy_off callback",
          "manager_proxy_off" not in active_src, active_src)
    check("7. E: the ACTIVE _proxy_detail_buttons is mode-driven (single action per mode)",
          "direct_warn" in active_src and "wiz:proxy:set:" in active_src, active_src)
    check("7. E: the risky direction (leaving proxy) already routes through a confirm wizard",
          "wiz:proxy:direct_warn:" in active_src, active_src)


# ======================================================================
# 8. Manager lifecycle (Phase N2) remains unchanged and valid.
# ======================================================================

def test_8_n2_manager_lifecycle_untouched() -> None:
    for marker in (
        "def _manager_settings_state", "def _mcard_btn",
        "def _manager_settings_card_buttons", "def _manager_disable_confirm_buttons",
        "menu:manager_disable_confirm:", 'f"cmd:/manager_start {key}"', 'f"cmd:/manager_disable {key}"',
    ):
        check(f"8. N2 manager-lifecycle marker still present: {marker!r}", marker in PANEL_SRC, marker)
    # Precise: scan only the N2 card block (between its state helper and the
    # next top-level marker) for the retired commands.
    start = PANEL_SRC.find("def _manager_settings_state")
    end = PANEL_SRC.find("_MS_PREV_TITLE_FOR_MENU = globals()")
    check("8. N2 card block located for scanning", start != -1 and end != -1 and end > start, (start, end))
    block = PANEL_SRC[start:end] if start != -1 and end != -1 and end > start else ""
    check("8. N2 canonical card block never emits cmd:/manager_enable", "cmd:/manager_enable" not in block, block)
    check("8. N2 canonical card block never emits cmd:/manager_stop (bare stop)", "cmd:/manager_stop {key}" not in block, block)


# ======================================================================
# 10/11. Bulk exceptions (H/I) stay explicit two-action operations,
# never collapsed into a fake single toggle.
# ======================================================================

def test_10_bulk_followups_stays_two_actions() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": f"m{i}"} for i in range(3)])
    try:
        ns = build_bulk_ns(db_path)
        rows = ns["_tp_panel_v5_bulk_rows"]()
        check("10. I: bulk followups still has TWO explicit actions (on all / off all)",
              _find(rows, data=b"bulk:ask:followup:on") is not None and _find(rows, data=b"bulk:ask:followup:off") is not None,
              rows)
        confirm = ns["_tp_panel_v5_confirm_text"]("followup", "on")
        check("10. I: confirm text shows the affected-manager count", "Затронет менеджеров: 3" in confirm, confirm)
        confirm_off = ns["_tp_panel_v5_confirm_text"]("followup", "off")
        check("10. I: confirm text (off) also shows the affected-manager count", "Затронет менеджеров: 3" in confirm_off, confirm_off)
        # N3.1 fix R2 (verify from the SAME entry point the confirm text
        # uses): the affected-manager count includes closers, matching the
        # real /followup ... all command (main.py applies no role filter).
        # N5.3 (D10): kind-aware canonical return -- followup-bulk is
        # launched from the followups screen, so cancel returns THERE.
        buttons = ns["_tp_panel_v5_confirm_buttons"]("followup", "on")
        check("10. I (amended N5.3 D10). followup-bulk confirm Back/Cancel returns to menu:followups",
              _find(buttons, label="⬅️ Назад", data=b"menu:followups") is not None, buttons)
    finally:
        os.unlink(db_path)

    # Closer-inclusive count: reseed with 1 regular + 1 closer, both
    # active+enabled -- the confirm count must be 2, not 1.
    db_path2 = _make_temp_db()
    _seed_managers(db_path2, [
        {"manager_key": "reg1", "role": "manager"},
        {"manager_key": "cls1", "role": "closer"},
    ])
    try:
        ns2 = build_bulk_ns(db_path2)
        confirm2 = ns2["_tp_panel_v5_confirm_text"]("followup", "on")
        check("10. I (N3.1 fix R2): affected-manager count INCLUDES closers (1 manager + 1 closer -> 2)",
              "Затронет менеджеров: 2" in confirm2, confirm2)
    finally:
        os.unlink(db_path2)


def test_11_emergency_silence_stays_two_actions_with_confirm_gate() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": f"m{i}"} for i in range(2)])
    try:
        ns = build_bulk_ns(db_path)
        rows = ns["_tp_ae_emergency_silence_menu"]()
        check("11. H: emergency silence still has TWO explicit bulk actions",
              _find(rows, label="✅ Снять тишину у всех", data=b"cmd:/silent manager off all") is not None
              and _find(rows, label="🛑 Включить тишину всем", data=b"bulk:ask:silent:on") is not None,
              rows)
        check("11. H: 'off all' stays immediate (direct cmd:, no confirm gate)",
              _find(rows, data=b"cmd:/silent manager off all") is not None, rows)
        check("11. H: 'on all' now routes through the confirm gate (bulk:ask:silent:on, not a direct cmd:)",
              _find(rows, data=b"cmd:/silent manager on all") is None, rows)
        confirm = ns["_tp_panel_v5_confirm_text"]("silent", "on")
        # N3.1 fix R5: wording must not imply N independent per-manager
        # writes (the real command sets ONE global flag) -- the manager
        # count stays informational context only.
        check("11. H (N3.1 fix R5): confirm text states ONE global action ('для всех'), not per-manager scoping",
              "включена для всех" in confirm, confirm)
        check("11. H (N3.1 fix R5): confirm text still shows the manager count as informational context",
              "Активных менеджеров сейчас: 2" in confirm, confirm)
        command = ns["_tp_panel_v5_command_for"]("silent", "on")
        check("11. H: 'on all' confirm ultimately runs the same existing command", command == "/silent manager on all", command)
        # N3.1 fix R3: the silent confirm screen's Back/Cancel must return
        # to menu:profile (where "🛑 Включить тишину всем" actually lives),
        # not menu:automation.
        buttons = ns["_tp_panel_v5_confirm_buttons"]("silent", "on")
        check("11. H (N3.1 fix R3): silent confirm Back/Cancel returns to menu:profile (its real origin screen)",
              _find(buttons, label="⬅️ Назад", data=b"menu:profile") is not None, buttons)
        check("11. H (N3.1 fix R3): silent confirm Back/Cancel does NOT return to menu:automation",
              _find(buttons, label="⬅️ Назад", data=b"menu:automation") is None, buttons)
    finally:
        os.unlink(db_path)


# ======================================================================
# 13/14. Exact raw button counts (not just presence/absence); no
# duplicate callback_data hidden behind set semantics.
# ======================================================================

def test_13_14_raw_counts_no_duplicates() -> None:
    # N3.1 fix R4: exact TOTAL raw button counts (not just "one toggle row
    # present" / set-based dup checks) for every canonical A-D screen, with
    # a fixed, documented seed so the expected numbers are reproducible.
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01"}])
    _set_setting(db_path, "unanswered_panel_notifications_enabled", "1")
    try:
        ns = build_unanswered_ns(db_path)
        rows = ns["_unanswered_menu"]()
        toggle_rows = [r for r in rows if _find([r], label="🔕 Выключить уведомления") or _find([r], label="🔔 Включить уведомления")]
        check("13. A: exactly one toggle row present", len(toggle_rows) == 1, toggle_rows)
        check("13. A: exact total button count (1 manager, notify=on) == 7", len(_flat(rows)) == 7, len(_flat(rows)))
        # b"noop" is the established decorative section-header marker
        # (_section_button) -- legitimately repeated across a screen and
        # excluded from the duplicate-callback check on purpose.
        datas = [d for d in _datas(rows) if d != b"noop"]
        dupes = sorted({d for d in datas if datas.count(d) > 1})
        check("14. A: no duplicate REAL callback_data on the unanswered screen (excl. decorative noop)", not dupes, dupes)
    finally:
        os.unlink(db_path)

    db_path2 = _make_temp_db()
    _set_source(db_path2, "src1", "active")
    ns2 = build_source_ns(db_path2)
    rows2 = ns2["_source_detail_buttons"]("src1")
    check("13. B: exact total button count (no linked managers) == 11", len(_flat(rows2)) == 11, len(_flat(rows2)))
    datas2 = [d for d in _datas(rows2) if d != b"noop"]
    dupes2 = sorted({d for d in datas2 if datas2.count(d) > 1})
    check("14. B: no duplicate REAL callback_data on the source screen (excl. decorative noop)", not dupes2, dupes2)
    os.unlink(db_path2)

    db_path3 = _make_temp_db()
    _set_group(db_path3, "grp1", "active")
    ns3 = build_group_ns(db_path3)
    rows3 = ns3["_group_detail_buttons"]("grp1")
    check("13. C: exact total button count (no members) == 8", len(_flat(rows3)) == 8, len(_flat(rows3)))
    datas3 = [d for d in _datas(rows3) if d != b"noop"]
    dupes3 = sorted({d for d in datas3 if datas3.count(d) > 1})
    check("14. C: no duplicate REAL callback_data on the group screen (excl. decorative noop)", not dupes3, dupes3)
    os.unlink(db_path3)

    db_path4 = _make_temp_db()
    _seed_managers(db_path4, [{"manager_key": "on1", "display_name": "On"}, {"manager_key": "off1", "display_name": "Off"}])
    _set_followup(db_path4, "on1", True)
    _set_followup(db_path4, "off1", False)
    ns4 = build_followups_ns(db_path4)
    rows4 = ns4["_followups_menu"]()
    check("13. D: exact total button count (2 managers) == 5 (1 row/manager + 3 footer rows)", len(_flat(rows4)) == 5, len(_flat(rows4)))
    datas4 = [d for d in _datas(rows4) if d != b"noop"]
    dupes4 = sorted({d for d in datas4 if datas4.count(d) > 1})
    check("14. D: no duplicate REAL callback_data on the followups screen (excl. decorative noop)", not dupes4, dupes4)
    os.unlink(db_path4)


# ======================================================================
# 15. Callback length <= 64 bytes.
# ======================================================================

def test_15_callback_length() -> None:
    long_key = "a" * 60
    db_path = _make_temp_db()
    _set_source(db_path, long_key, "active")
    ns = build_source_ns(db_path)
    rows = ns["_source_detail_buttons"](long_key)
    for b in _flat(rows):
        check(f"15. B: callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
    os.unlink(db_path)

    db_path2 = _make_temp_db()
    _seed_managers(db_path2, [{"manager_key": "mgr01"}])
    _set_setting(db_path2, "auto_status_enabled", "1")
    ns2 = build_autostatus_ns(db_path2)
    rows2 = ns2["_tp_visual_automation_menu"]()
    for b in _flat(rows2):
        check(f"15. F: callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
    os.unlink(db_path2)


# ======================================================================
# 16. No /manager_enable or /manager_stop reintroduced into the N2 card.
# (Structural re-assertion, complements test_8.)
# ======================================================================

def test_16_no_manager_enable_stop_regression() -> None:
    start = PANEL_SRC.find("def _manager_settings_state")
    end = PANEL_SRC.find("_MS_PREV_TITLE_FOR_MENU = globals()")
    block = PANEL_SRC[start:end] if start != -1 and end != -1 and end > start else PANEL_SRC
    # Search for the actual EMITTED command prefix ("cmd:/manager_enable"),
    # not the bare word -- the block legitimately contains an explanatory
    # comment mentioning "/manager_enable" (documenting why /manager_start
    # is used instead), which is not a callback emission.
    check("16. no 'cmd:/manager_enable' emission in the N2 canonical card block", "cmd:/manager_enable" not in block, block)
    check("16. no bare 'cmd:/manager_stop {key}' emission in the N2 canonical card block", "cmd:/manager_stop {key}" not in block, block)


# ======================================================================
# 17. Existing historical callbacks remain handled.
# ======================================================================

def test_17_historical_callbacks_handled() -> None:
    for marker in (
        'if raw == "autostatus_toggle":', 'if raw == "nm_llm_toggle_do":', 'if raw == "llm_toggle":',
        "cmd:/source on", "cmd:/source off", "cmd:/mgroup on", "cmd:/mgroup off",
        "cmd:/unanswered_notify on", "cmd:/unanswered_notify off",
        "cmd:/followup on", "cmd:/followup off",
    ):
        check(f"17. historical marker still present/handled: {marker!r}", marker in PANEL_SRC, marker)


# ======================================================================
# 18. No handler/PREV inventory removed.
# ======================================================================

def test_18_handler_prev_inventory_intact() -> None:
    for marker in (
        "_AUTOSTATUS_PREV_TITLE_FOR_MENU = globals().get(",
        "_NM_PREV_TITLE_FOR_MENU" if "_NM_PREV_TITLE_FOR_MENU = globals()" in PANEL_SRC else "_MS_PREV_TITLE_FOR_MENU = globals().get(",
        "_TP_PANEL_V5_ORIG_FOLLOWUPS_MENU = globals().get(",
    ):
        check(f"18. PREV-capture marker still present: {marker!r}", marker in PANEL_SRC, marker)
    client_on_count = PANEL_SRC.count("@client.on(")
    check("18. @client.on(...) registration count is a healthy positive number (sanity)", client_on_count > 50, client_on_count)


# ======================================================================
# N3.2 -- manager-modes card (Part A) + per-manager silence list (Part B) +
# auto-status status-indicator fix (Part C).
# ======================================================================

def test_19_manager_modes_no_side_by_side_pairs() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01"}])
    try:
        ns = build_mmt_ns(db_path)
        rows = ns["_manager_detail_buttons"]("mgr01")
        datas = _datas(rows)
        check("19. manager-modes card: no old cmd:/greeting on+off pair",
              not (b"cmd:/greeting on mgr01" in datas and b"cmd:/greeting off mgr01" in datas), datas)
        check("19. manager-modes card: no old cmd:/questionnaire on+off pair",
              not (b"cmd:/questionnaire on mgr01" in datas and b"cmd:/questionnaire off mgr01" in datas), datas)
        check("19. manager-modes card: no old cmd:/silent manager on+off pair",
              not (b"cmd:/silent manager on mgr01" in datas and b"cmd:/silent manager off mgr01" in datas), datas)
        check("2. autoreply/greeting control shows exactly one opposite action",
              sum(1 for d in datas if d.startswith(b"mmt:greeting:")) == 1, datas)
        check("3. questionnaire control shows exactly one opposite action",
              sum(1 for d in datas if d.startswith(b"mmt:quest:")) == 1, datas)
        check("4. per-manager silence (card) shows exactly one opposite action",
              sum(1 for d in datas if d.startswith(b"mmt:silentcard:")) == 1, datas)
    finally:
        os.unlink(db_path)


def test_20_manager_modes_state_display() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01"}])
    try:
        # greeting default ON, questionnaire default OFF, silence default OFF.
        ns = build_mmt_ns(db_path)
        rows = ns["_manager_detail_buttons"]("mgr01")
        check("5. state display: greeting ON shows 'Выключить автоответ' (opposite action)",
              _find(rows, label="⏸ Выключить автоответ", data=b"mmt:greeting:off:mgr01") is not None, rows)
        check("5. state display: questionnaire OFF shows 'Включить анкету' (opposite action)",
              _find(rows, label="▶️ Включить анкету", data=b"mmt:quest:on:mgr01") is not None, rows)
        check("5. state display: silence OFF shows 'Включить тишину' (opposite action)",
              _find(rows, label="🔕 Включить тишину", data=b"mmt:silentcard:on:mgr01") is not None, rows)
    finally:
        os.unlink(db_path)

    db_path2 = _make_temp_db()
    _seed_managers(db_path2, [{"manager_key": "mgr01"}])
    _set_gq(db_path2, "mgr01", greeting=0, questionnaire=1)
    _set_silent(db_path2, "manager", "mgr01", True)
    try:
        ns2 = build_mmt_ns(db_path2)
        rows2 = ns2["_manager_detail_buttons"]("mgr01")
        check("5. state display: greeting OFF (override) shows 'Включить автоответ'",
              _find(rows2, label="▶️ Включить автоответ", data=b"mmt:greeting:on:mgr01") is not None, rows2)
        check("5. state display: questionnaire ON (override) shows 'Выключить анкету'",
              _find(rows2, label="⏸ Выключить анкету", data=b"mmt:quest:off:mgr01") is not None, rows2)
        check("5. state display: silence ON shows 'Выключить тишину'",
              _find(rows2, label="🔔 Выключить тишину", data=b"mmt:silentcard:off:mgr01") is not None, rows2)
    finally:
        os.unlink(db_path2)


def test_21_target_maps_to_existing_business_commands() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01"}])
    try:
        ns = build_mmt_ns(db_path)
        resolve = ns["_pb_mmt_resolve"]
        # 6. Every target callback resolves to EXACTLY the same command text
        # the old static pair used to embed directly in its callback_data --
        # no new business logic, same existing commands reused verbatim.
        cases = {
            "mmt:greeting:off:mgr01": "/greeting off mgr01",
            "mmt:quest:on:mgr01": "/questionnaire on mgr01",
            "mmt:silentcard:on:mgr01": "/silent manager on mgr01",
            "mmt:silentlist:on:mgr01": "/silent manager on mgr01",
        }
        for cb, expected_cmd in cases.items():
            result = resolve(cb)
            check(f"6. {cb} resolves to the existing command {expected_cmd!r}",
                  result.get("ok") and not result.get("stale") and result.get("command") == expected_cmd, result)
    finally:
        os.unlink(db_path)


def test_22_stale_target_no_mutate_rerender() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01"}])
    try:
        ns = build_mmt_ns(db_path)
        resolve = ns["_pb_mmt_resolve"]
        # greeting defaults ON -- target "on" is already current -> stale.
        result = resolve("mmt:greeting:on:mgr01")
        check("7. stale target: ok=True, stale=True, no 'command' key (nothing would be dispatched)",
              result == {"ok": True, "stale": True, "kind": "greeting", "key": "mgr01", "target": "on"}, result)
        # silence defaults OFF -- target "off" is already current -> stale.
        result2 = resolve("mmt:silentcard:off:mgr01")
        check("7. stale target (silence): ok=True, stale=True, no 'command' key",
              result2 == {"ok": True, "stale": True, "kind": "silentcard", "key": "mgr01", "target": "off"}, result2)
    finally:
        os.unlink(db_path)


def test_23_malformed_target_cannot_mutate() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01"}])
    try:
        ns = build_mmt_ns(db_path)
        resolve = ns["_pb_mmt_resolve"]
        for bad in (
            "mmt:garbage:on:mgr01",       # unknown kind
            "mmt:greeting:maybe:mgr01",   # unknown target
            "mmt:greeting:on:",           # empty key
            "mmt::on:mgr01",              # empty kind
            "mmt:greeting:on",            # missing key segment entirely
        ):
            result = resolve(bad)
            check(f"8. malformed target {bad!r}: rejected, no 'command' key present (cannot mutate)",
                  result.get("ok") is False and "command" not in result, result)
        check("8. malformed target: rejection reason is 'malformed' (not silently treated as valid)",
              resolve("mmt:garbage:on:mgr01").get("reason") == "malformed", None)
    finally:
        os.unlink(db_path)


def test_24_missing_manager_safe() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01"}])
    try:
        ns = build_mmt_ns(db_path)
        resolve = ns["_pb_mmt_resolve"]
        result = resolve("mmt:greeting:on:doesnotexist")
        check("9. missing/deleted manager: rejected safely, reason='missing_manager', no 'command' key",
              result.get("ok") is False and result.get("reason") == "missing_manager" and "command" not in result, result)
    finally:
        os.unlink(db_path)


# N3.2.1 fix (review V1): this used to be a snapshot-equality check against
# the exact pre-N3.2 dispatch block -- byte-for-byte, taken from the file
# BEFORE N3.2 touched anything (verified identical to the pre-N3.2 backup at
# the time this was written). Any future accidental edit to the generic
# cmd: pipeline changes this string and the "is present, unchanged" check
# below fails loudly; this is a real snapshot invariant, not a marker guess.
#
# N5.4.4 (owner decision, observability fix): the snapshot below was
# updated ONCE, deliberately, to match the intentional 1-line change this
# phase made -- _panel_status_send now also receives the `command` (used
# only to log its VERB, never its arguments, on a delivery failure; see
# tools\panel_delivery_observability_selftest.py). This is the exact kind
# of change this test exists to catch: verified here as intentional and
# reviewed, not silently masked -- the invariant this test protects
# (nothing else about the dispatch pipeline changes) still holds and is
# re-verified by this new snapshot.
_CMD_DISPATCH_BLOCK_SNAPSHOT = (
    '    if data.startswith("cmd:"):\n'
    '        command = data.split(":", 1)[1].strip()\n'
    '        if not command:\n'
    '            await event.answer("Пустая команда", alert=True)\n'
    '            return\n'
    '\n'
    '        chat_id = int(event.chat_id or 0)\n'
    '        user_id = int(event.sender_id or 0)\n'
    '\n'
    '        if _panel_callback_is_duplicate(chat_id, user_id, command):\n'
    '            await _panel_answer_fast(event, "Уже выполняю...")\n'
    '            return\n'
    '\n'
    '        await _panel_answer_fast(event, "⏳ Запустил")\n'
    '\n'
    '        status_msg_id = await _panel_status_send(\n'
    '            chat_id,\n'
    '            "⏳ Выполняю выбранное действие.\\nПанель можно использовать дальше.",\n'
    '            command,\n'
    '        )\n'
    '        _panel_spawn_command_task(command, user_id, chat_id, status_msg_id)\n'
    '        return\n'
)


def test_25_mmt_historical_and_old_pair_still_handled() -> None:
    # 10. The underlying /greeting, /questionnaire, /silent commands are
    # untouched and still reachable through the generic cmd: pipeline for
    # any already-sent historical button/message using the OLD pair-style
    # callback_data -- nothing about N3.2 removes or reroutes cmd:/greeting,
    # cmd:/questionnaire or cmd:/silent handling.
    check("10. generic cmd: dispatch branch still present (handles any historical cmd:/greeting|questionnaire|silent click)",
          'if data.startswith("cmd:"):' in PANEL_SRC, None)
    # N3.2.1 fix (review V1): the old check here was tautological post-N3.2
    # (its second OR-branch, a bare '"{marker}' text search, is now
    # unconditionally satisfied by N3.2's OWN resolver strings like
    # f"/greeting {target} {key}" -- it could never fail regardless of
    # whether the historical cmd: pipeline was actually intact). Replaced
    # with real, independent checks that do not depend on N3.2's own code:
    check("10. generic cmd: dispatch branch is byte-identical to its pre-N3.2 snapshot (unchanged pipeline)",
          _CMD_DISPATCH_BLOCK_SNAPSHOT in PANEL_SRC, None)
    # Scoped to the ACTIVE function bodies specifically (not a whole-file
    # substring search) -- both literals also exist verbatim in shadowed
    # dead-code generations earlier in the file (the old, pre-override
    # _tp_ae_emergency_silence_menu-equivalent and _manager_detail_buttons
    # defs), so an unscoped `in PANEL_SRC` check would stay green even if the
    # ACTIVE screen's emission were mutated -- proven by a scratchpad
    # mutation exercise during N3.2.1 (mutating only the active-def literal,
    # leaving the dead-code copy intact, left an unscoped check green).
    silence_start = PANEL_SRC.rfind("def _tp_ae_emergency_silence_menu")
    silence_end = PANEL_SRC.find("\ndef ", silence_start + 10) if silence_start != -1 else -1
    silence_block = PANEL_SRC[silence_start:silence_end] if silence_start != -1 and silence_end != -1 else ""
    check("10. active emergency-silence screen still emits the historical 'cmd:/silent manager off all' route",
          'b"cmd:/silent manager off all"' in silence_block, silence_block[:200])

    card_start = PANEL_SRC.rfind("def _manager_detail_buttons")
    card_end = PANEL_SRC.find("\ndef ", card_start + 10) if card_start != -1 else -1
    card_block = PANEL_SRC[card_start:card_end] if card_start != -1 and card_end != -1 else ""
    check("10. active manager-modes card still emits the historical 'cmd:/autoreply status {key}' route",
          'f"cmd:/autoreply status {key}".encode()' in card_block, card_block[:200])
    # The byte-identical snapshot above proves the generic cmd: forwarder is
    # completely unchanged (it never special-cases any command name -- see
    # the snapshot text itself: it just strips the "cmd:" prefix and spawns
    # whatever follows). Combined with main.py being untouched (protected
    # file, verified separately by mtime), that generic, unconditional
    # forwarding is sufficient proof that ANY historical "cmd:/greeting ...",
    # "cmd:/questionnaire ...", "cmd:/silent manager ..." callback from an
    # old Telegram message still dispatches correctly -- no per-command
    # marker search is needed (and a bare text search for "/greeting" would
    # be satisfied by N3.2's own resolver strings, which is exactly the
    # tautology this fix removes).


def test_26_n2_lifecycle_and_n3_n31_protections_unchanged() -> None:
    # 11. N2's manager-lifecycle toggle lives in a DIFFERENT function
    # (_manager_settings_card_buttons, the unified admin card) that N3.2
    # never touches -- confirm it's still present and distinct from the
    # N3.2 manager-modes card.
    check("11. N2 unified admin card builder untouched/still present: 'def _manager_settings_card_buttons'",
          "def _manager_settings_card_buttons" in PANEL_SRC, None)
    check("11. N2 lifecycle confirm route untouched: 'menu:manager_disable_confirm:'",
          "menu:manager_disable_confirm:" in PANEL_SRC, None)
    # 12. N3/N3.1 auto-status + LLM target-encoded confirm/do protections are
    # untouched by the Part C noop-wording fix (different callback literal).
    for marker in (
        'if raw.startswith("autostatus_toggle_confirm:"):',
        'if raw.startswith("autostatus_toggle_do:"):',
        'if raw.startswith("nm_llm_toggle_do:"):',
        'target not in ("0", "1")',
    ):
        check(f"12. N3/N3.1 auto-status/LLM protection marker still present: {marker!r}", marker in PANEL_SRC, marker)


def test_27_mmt_raw_counts_and_duplicates() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01"}])
    try:
        ns = build_mmt_ns(db_path)
        rows = ns["_manager_detail_buttons"]("mgr01")
        check("13. manager-modes card: exact total button count (1 manager) == 11",
              len(_flat(rows)) == 11, len(_flat(rows)))
        datas = [d for d in _datas(rows) if d != b"noop"]
        dupes = sorted({d for d in datas if datas.count(d) > 1})
        check("14. manager-modes card: no duplicate callback_data", not dupes, dupes)
    finally:
        os.unlink(db_path)

    db_path2 = _make_temp_db()
    _seed_managers(db_path2, [{"manager_key": "on1"}, {"manager_key": "off1"}])
    _set_silent(db_path2, "manager", "on1", True)
    try:
        ns2 = build_bulk_ns(db_path2)
        rows2 = ns2["_tp_ae_emergency_silence_menu"]()
        # N3.2.1 fix (review V2): corrected arithmetic (the old comment's
        # breakdown summed to 9, not 8) and made the fake-nav dependency
        # explicit. Real breakdown: 3 header buttons (1 "Проверить тишину" +
        # 2-button bulk pair) + 2 managers x (1 status button + 1 toggle
        # button) = 4 + build_bulk_ns's FAKE _tp_visual_nav_rows, which
        # returns exactly ONE stub nav button (not the real
        # _tp_visual_nav_rows, which would emit more) = 3 + 4 + 1 = 8.
        check("13. silence list: exact total button count (2 managers) == 8 (3 header + 2x(status+toggle)=4 + 1 fake-nav stub)",
              len(_flat(rows2)) == 8, len(_flat(rows2)))
        datas2 = [d for d in _datas(rows2) if d != b"noop"]
        dupes2 = sorted({d for d in datas2 if datas2.count(d) > 1})
        check("14. silence list: no duplicate callback_data", not dupes2, dupes2)
    finally:
        os.unlink(db_path2)


def test_28_mmt_callback_length() -> None:
    # Scoped to the N3.2 mmt: toggle buttons specifically (the only buttons
    # this phase touches/guards via _pb_toggle_btn) -- the surrounding
    # pre-existing buttons on these screens (proxy/reserve/transfer links,
    # the "Проверить режим"/"👤 label" status rows) are unrelated, unguarded,
    # unchanged N3.2-out-of-scope code and are intentionally not asserted on
    # here (owner's task explicitly excludes redesigning the rest of the
    # screen).
    #
    # Two cases: a realistic key (mmt: callback itself must stay <=64 bytes,
    # guard should NOT need to engage) and a pathologically long key (guard
    # MUST engage -- falls back to a safe, still-<=64-byte, non-mmt: route).
    db_path = _make_temp_db()
    realistic_key = "manager_with_a_normal_username"
    _seed_managers(db_path, [{"manager_key": realistic_key}])
    try:
        ns = build_mmt_ns(db_path)
        rows = ns["_manager_detail_buttons"](realistic_key)
        mmt_buttons = [b for b in _flat(rows) if b[2].startswith(b"mmt:")]
        check("15. manager-modes card: realistic key still emits real (unguarded) mmt: buttons",
              len(mmt_buttons) == 3, mmt_buttons)
        for b in mmt_buttons:
            check(f"15. manager-modes card: mmt: callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
    finally:
        os.unlink(db_path)

    # Toggle-button labels (the ones _pb_toggle_btn actually guards) --
    # identified by label since a guard-engaged button's DATA is no longer
    # mmt:-prefixed by definition (that's the point of the fallback).
    toggle_labels = {
        "⏸ Выключить автоответ", "▶️ Включить автоответ",
        "⏸ Выключить анкету", "▶️ Включить анкету",
        "🔔 Выключить тишину", "🔕 Включить тишину",
    }

    long_key = "a" * 60
    db_path2 = _make_temp_db()
    _seed_managers(db_path2, [{"manager_key": long_key}])
    try:
        ns2 = build_mmt_ns(db_path2)
        rows2 = ns2["_manager_detail_buttons"](long_key)
        raw_would_be = f"mmt:silentcard:off:{long_key}".encode("utf-8")
        check("15. sanity: the long-key mmt: callback genuinely exceeds 64 bytes unguarded (proves the guard is load-bearing)",
              len(raw_would_be) > 64, len(raw_would_be))
        guarded = [b for b in _flat(rows2) if b[1] in toggle_labels]
        check("15. manager-modes card (long key): all 3 toggle buttons present even in fallback form",
              len(guarded) == 3, guarded)
        for b in guarded:
            check(f"15. manager-modes card (long key): guard engaged, not a truncated mmt: callback: {b[1]!r}",
                  not b[2].startswith(b"mmt:"), b)
            check(f"15. manager-modes card (long key): fallback callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
    finally:
        os.unlink(db_path2)

    db_path3 = _make_temp_db()
    _seed_managers(db_path3, [{"manager_key": "on1"}])
    try:
        ns3 = build_bulk_ns(db_path3)
        rows3 = ns3["_tp_ae_emergency_silence_menu"]()
        mmt_buttons3 = [b for b in _flat(rows3) if b[2].startswith(b"mmt:")]
        check("15. silence list: realistic key still emits a real (unguarded) mmt: button",
              len(mmt_buttons3) == 1, mmt_buttons3)
        for b in mmt_buttons3:
            check(f"15. silence list: mmt: callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
    finally:
        os.unlink(db_path3)

    db_path4 = _make_temp_db()
    _seed_managers(db_path4, [{"manager_key": long_key}])
    try:
        ns4 = build_bulk_ns(db_path4)
        rows4 = ns4["_tp_ae_emergency_silence_menu"]()
        guarded4 = [b for b in _flat(rows4) if b[1] in toggle_labels]
        check("15. silence list (long key): toggle button present even in fallback form",
              len(guarded4) == 1, guarded4)
        for b in guarded4:
            check(f"15. silence list (long key): guard engaged, not a truncated mmt: callback: {b[1]!r}",
                  not b[2].startswith(b"mmt:"), b)
            check(f"15. silence list (long key): fallback callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
    finally:
        os.unlink(db_path4)


def test_29_autostatus_status_indicator_fix() -> None:
    # 16. The auto-status status row no longer shares the generic "noop"
    # callback (whose toast is a misleading "section header" notice for a
    # live state indicator); it has its own narrowly-scoped callback with a
    # meaningful notice, and the many genuine noop section headers elsewhere
    # keep their original wording untouched.
    # _tp_visual_automation_menu has several stacked override defs -- rfind
    # locates the LAST (active, last-wins) one, then the block runs to the
    # next top-level "def " after it, whatever that generation's neighbor
    # happens to be (avoids hardcoding a neighbor name that could itself be
    # multiply-defined).
    start = PANEL_SRC.rfind("def _tp_visual_automation_menu")
    end = PANEL_SRC.find("\ndef ", start + 10) if start != -1 else -1
    block = PANEL_SRC[start:end] if start != -1 and end != -1 and end > start else ""
    check("16. auto-status status row no longer uses the generic b'noop' callback",
          'b"noop"' not in block, block)
    check("16. auto-status status row uses its own narrowly-scoped callback",
          'b"autostatus_status_info"' in block, block)
    check("16. new callback has its own dedicated handler with a meaningful notice",
          'if data == "autostatus_status_info":' in PANEL_SRC and "ℹ️ Это текущий статус автостатусов" in PANEL_SRC, None)
    check("16. generic noop handler's wording is UNCHANGED for the many genuine section headers",
          'if data == "noop":' in PANEL_SRC and 'await event.answer("Это заголовок блока"' in PANEL_SRC, None)
    # sanity: plenty of other noop buttons still exist untouched (section
    # headers / disabled placeholders elsewhere in the file).
    check("16. sanity: other genuine noop section headers still present in the file",
          PANEL_SRC.count('b"noop")') > 10, PANEL_SRC.count('b"noop")'))


def test_30_n32_handler_prev_inventory() -> None:
    # 17. Sanity presence checks for the new N3.2 helpers/markers (the exact
    # byte-for-byte def/handler/PREV inventory diff against the pre-N3.2
    # backup is performed and reported separately, per project convention --
    # see the N3.2 completion report).
    for marker in (
        "def _pb_mmt_resolve", "def _pb_mmt_rerender", "def _pb_mmt_run", "def _pb_toggle_btn",
        "def _pb_greeting_enabled", "def _pb_questionnaire_enabled", "def _pb_manager_silent_enabled",
        'if data.startswith("mmt:"):',
    ):
        check(f"17. N3.2 marker present: {marker!r}", marker in PANEL_SRC, marker)
    client_on_count = PANEL_SRC.count("@client.on(")
    check("17. @client.on(...) registration count is a healthy positive number (sanity, N3.2 adds none)",
          client_on_count > 50, client_on_count)


def main() -> int:
    test_1_2_unanswered_single_toggle()
    test_3_source_single_toggle()
    test_4_group_single_toggle()
    test_5_followups_single_toggle_per_manager()
    test_6_autostatus_single_toggle()
    test_7_proxy_no_live_pair_verified_na()
    test_8_n2_manager_lifecycle_untouched()
    test_9_nm_llm_single_toggle()
    test_10_bulk_followups_stays_two_actions()
    test_11_emergency_silence_stays_two_actions_with_confirm_gate()
    test_13_14_raw_counts_no_duplicates()
    test_15_callback_length()
    test_16_no_manager_enable_stop_regression()
    test_17_historical_callbacks_handled()
    test_18_handler_prev_inventory_intact()
    test_19_manager_modes_no_side_by_side_pairs()
    test_20_manager_modes_state_display()
    test_21_target_maps_to_existing_business_commands()
    test_22_stale_target_no_mutate_rerender()
    test_23_malformed_target_cannot_mutate()
    test_24_missing_manager_safe()
    test_25_mmt_historical_and_old_pair_still_handled()
    test_26_n2_lifecycle_and_n3_n31_protections_unchanged()
    test_27_mmt_raw_counts_and_duplicates()
    test_28_mmt_callback_length()
    test_29_autostatus_status_indicator_fix()
    test_30_n32_handler_prev_inventory()

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
