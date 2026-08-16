# -*- coding: utf-8 -*-
"""tools/nm_menu_distribution_selftest.py -- offline self-test for the approved
nm_* ("Новое меню") function-distribution migration in panel_bot.py:

    1. 📤 Экспорт          -> 📊 Отчёты и контроль (nm_reports)
    2. 🔁 Дубликаты         -> 📊 Отчёты и контроль (nm_reports, unchanged)
    3. 📊 Воронка           -> 📦 Трафик и байеры (nm_traffic), removed from Reports
    4. 🤖 LLM: наблюдения   -> 🤖 Автоматизация (nm_automation), read-only
    5. 🤖 LLM: ВКЛ/ВЫКЛ     -> 🤖 Автоматизация, now gated by a confirm screen
    6. 🤝 Передачи          -> stays its own top-level nm_transfers, no dup in Reports
    7. 🔔 Неотвеченные      -> 📈 Качество и дисциплина (nm_quality_w), unchanged content

Corrective fix included here: the first pass left a second, unconfirmed path to the LLM
toggle in the new 🩺 Система screen (menu:llm_toggle, no confirm, landed on the OLD
reports_control). That button was removed from _nm_system_screen; the only new-menu path
to the toggle is now Автоматизация -> nm_llm_toggle_confirm/_do. Also removed the
redundant standalone "Долёты" entry from Reports -- Долёты is already a mode inside the
new 📊 Статистика wizard (nms:), so a second button into the old, heavier /flight screen
was a duplicate UX path, not a distinct function.

Phase N1 (approved NM2 migration, 2026-07-21) additions -- final 10-button root:
    11. 🧭 Контроль (standalone root section)            -> merged into 🛠 Система
        (nm_system). nm_health_w / nm_tghealth_w Back-target changed nm_control -> nm_system.
    12. 📌 Источники (standalone root section)            -> merged into 🚦 Трафик и
        источники (nm_traffic, relabeled from "🚦 Трафик"). Same targets reused
        (menu:sources, cmd:/source stats all, wiz:add_source:start).
    13. 📈 Качество и дисциплина (standalone root section) -> merged into 📊 Отчёты и
        контроль (nm_reports) as a "📈 Качество" button, same target menu:nm_quality_w.
        nm_quality_w Back-target changed nm_root -> nm_reports.
    14. Root reduced to the 10 approved canonical category buttons (Отчёты, Менеджеры,
        Трафик и источники, Прокси, Бизнес-ссылки, Передачи, Автоматизация, Система,
        Поиск, Описание) + 1 transitional "↩️ Старое меню" row (N1/N5 state; superseded
        below).
    _nm_control_screen and _nm_sources_screen stay fully defined and dispatchable in
    _title_for_menu (same orphan-by-root-removal pattern already established for
    nm_export/nm_flights before this migration) -- nothing removed, only reorganized.

    CURRENT root layout (N5.1, corrected 2026-07-21; see test_7d below): the N1/N5
    10+1 layout above was replaced by the owner-approved 8-row/12-button root:
    ➕ Добавить менеджера / 🤖 AI-ассистент / 8 canonical sections (📊 Отчёты, 👥
    Менеджеры, 🚦 Трафик и источники, 🌐 Прокси, 🔗 Бизнес-ссылки, 🤝 Передачи, ⚙️
    Автоматизация, 🛠 Система) / 🗑 Удалить менеджера / 🔄 Обновить. Поиск, Описание
    and Старое меню are NOT on the root any more -- Поиск stays a fully handled
    historical route (menu:nm_search); Описание moved into Система (near the bottom,
    menu:description unchanged); Старое меню moved into Система -> Сервис -> a
    warning screen (menu:old_menu_warning) that opens the unchanged temporary
    "old_root" route -- see menu_parity_selftest.py for that whole capability map.

    N5.2 Part A (2026-07-21): 🗑 Удалить менеджера now targets a dedicated selection
    screen (menu:nm_manager_delete, see panel_bot.py's _nm_manager_delete_screen)
    instead of the generic manager-settings list -- each manager button on that
    screen lands directly on the unchanged menu:manager_danger:{key} chain. See
    manager_delete_safety_selftest.py for the deep safety proofs on that screen.

Scope: panel_bot.py only. Old menu (_tp_visual_*, plain _main_menu/_title_for_menu
chain, /stat /export /duplicates /funnel /unanswered commands) is untouched -- this
migration only rewires which nm_* screen a function is reachable from and what its
nm Back-target is; the underlying calc/command functions are reused verbatim.

Techniques: panel_bot.py cannot be imported standalone (Telethon/env side effects at
import time) -- functions under test are extracted via ast.parse + ast.unparse +
exec() (same technique used throughout this project's tools/*_selftest.py files) and
run FOR REAL against a temporary SQLite file (never db/data_tpilot.db or db/data.db).

Special problem this file has to solve: panel_bot.py redefines _title_for_menu 31
times via an override-stack (only the LAST active definition matters at runtime).
Ordinary by-name AST extraction can't tell 31 same-named functions apart. This file
locates the ONE _title_for_menu generation that owns the nm_* routing (the one edited
for this migration) by a unique marker string ("nm_llm_toggle_do") that only appears
in that generation, and isolates it from the other 30 by injecting a FAKE prior
_title_for_menu (so _NM_PREV_TITLE_FOR_MENU captures the fake, not any of the other
30 generations) -- this is a deliberate, scoped isolation, not an oversight: the other
30 generations handle unrelated features (nms: stats wizard, proxy pool, autostatus,
baseline managers, ...) that this migration does not touch.

    python tools\\nm_menu_distribution_selftest.py
"""
from __future__ import annotations

import ast
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


PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()


class _FakeBtn:
    """Exposes BOTH attribute access (.text/.data, as real Telethon Button objects do --
    needed because production code like _nm_row_is_old_back uses getattr(btn, "data", ...))
    and tuple-style indexing/unpacking (used by this file's own _flat/_find/_labels/_datas
    helpers)."""
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


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="nm_menu_dist_selftest_")
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


# ======================================================================
# Extraction #1: the nm_* routing chain (root/reports/traffic/automation
# screens, the nm_wrap footer mechanism, the LLM toggle+confirm flow, and
# the ONE _title_for_menu generation that owns nm_* dispatch).
# ======================================================================

ROUTING_NAMES = {
    "_nm_btn", "_nm_row", "_nm_footer", "_nm_screen", "_nm_canonical_path",
    "_nm_row_is_old_back", "_nm_wrap",
    "_NM_OLD_BACK_CALLBACKS", "_NM_REFRESH_ONLY_CALLBACKS",
    "_nm_root_screen", "_nm_reports_screen", "_nm_traffic_screen", "_nm_automation_screen",
    "_nm_system_screen",
    "_nm_llm_status_buttons", "_nm_llm_toggle_confirm_text", "_nm_llm_toggle_confirm_buttons",
    "_NM_PREV_TITLE_FOR_MENU",
    "_pb_get_setting", "_pb_set_setting", "_connect_panel_db",
    "_LLM_STATUS_PERIOD_LABELS",
    # W3.2 TZ-5: _pb_set_setting's updated_at write now reuses the approved
    # _utc_now_iso() wrapper instead of a bare datetime.now() -- extract it too, or
    # the call raises NameError inside _pb_set_setting's own try/except (silently
    # swallowed, so the setting write never actually lands and every check that
    # depends on it flipping fails downstream instead of at the real cause).
    "_utc_now_iso",
}


def _find_edited_title_for_menu(tree: ast.Module, marker: str):
    """panel_bot.py defines _title_for_menu 31 times (override-stack). Locate the ONE
    generation whose body contains `marker` -- only the generation edited for this
    migration calls the new nm_llm_toggle_do route, so that string uniquely identifies it."""
    found = None
    for n in tree.body:
        if getattr(n, "name", None) == "_title_for_menu":
            try:
                src_txt = ast.unparse(n)
            except Exception:
                continue
            if marker in src_txt:
                found = n
    return found


def build_routing_ns(db_path: str, *, fake_old_screen=None, fake_llm_status_text=None) -> dict:
    import manager_registry
    from datetime import datetime as _dt

    tree = ast.parse(PANEL_SRC)
    nodes = []
    seen = set()
    for n in tree.body:
        nm = getattr(n, "name", None)
        if nm == "_title_for_menu":
            continue  # handled separately below (marker lookup)
        if nm and nm in ROUTING_NAMES:
            nodes.append(n)
            seen.add(nm)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in ROUTING_NAMES:
            nodes.append(n)
            seen.add(n.targets[0].id)
            continue

    missing = ROUTING_NAMES - seen
    if missing:
        raise AssertionError(f"build_routing_ns: expected {ROUTING_NAMES}, missing {missing}")

    edited = _find_edited_title_for_menu(tree, "nm_llm_toggle_do")
    if edited is None:
        raise AssertionError("build_routing_ns: could not locate the edited _title_for_menu "
                              "generation (marker 'nm_llm_toggle_do' not found) -- migration "
                              "may have been reverted or moved to a different generation")
    nodes.append(edited)

    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    def _default_fake_old_screen(route):
        route = str(route or "")
        return f"OLD_SCREEN:{route}", [
            [_FakeButton.inline(f"stub-content:{route}", f"cmd:/stub {route}")],
            [_FakeButton.inline("⬅️ Назад", b"menu:main")],
        ]

    def _default_fake_llm_status_text(period="today"):
        return f"LLM_STATUS_TEXT:{period}"

    ns = {
        "sqlite3": sqlite3,
        "os": os,
        "datetime": _dt,
        # utcnow refactor (2026-08-16): extracted panel_bot code reads the
        # clock through the module-level _pb_utc_now() seam (naive UTC).
        "_pb_utc_now": (lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).replace(tzinfo=None)),
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "_panel_header": lambda: "HEADER",
        # Captured by _NM_PREV_TITLE_FOR_MENU = globals().get("_title_for_menu") --
        # deliberately fake, isolating this test from the other 30 _title_for_menu
        # generations (nms: wizard, proxy pool, autostatus, baseline managers, ...),
        # none of which handle any nm_* route this migration touches.
        "_title_for_menu": fake_old_screen or _default_fake_old_screen,
        "_tp_llm_status_screen_text": fake_llm_status_text or _default_fake_llm_status_text,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:nm_routing>", "exec"), ns)
    return ns


# ======================================================================
# Extraction #2: the REAL legacy menu builders whose command/callback
# content must survive this migration byte-for-byte (requirement #9).
# ======================================================================

LEGACY_NAMES = {
    "_export_menu", "_duplicates_menu", "_funnel_menu", "_unanswered_menu", "_quality_menu",
    "_manager_rows", "_manager_hist_label", "_manager_short_label", "_section_button",
    "_today_iso", "_parse_iso_date", "_date_obj", "_clamp_iso_date", "_shift_iso_date",
    "_display_date", "_cmd_date", "_callback_data",
    # N3: _unanswered_menu now calls this new sync-read helper (and its own
    # dependencies) for its single dynamic toggle -- must be extracted
    # alongside it.
    "_pb_unanswered_notify_enabled", "_pb_get_setting", "_connect_panel_db",
}


def _extract_and_exec(names: set, extra_ns: dict) -> dict:
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
        raise AssertionError(f"expected {names}, missing {missing}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{PANEL_PATH}:legacy>", "exec"), ns)
    return ns


def build_legacy_ns(db_path: str) -> dict:
    import manager_registry
    from datetime import datetime as _dt, timedelta as _td
    return _extract_and_exec(
        LEGACY_NAMES,
        {
            "sqlite3": sqlite3,
            "os": os,
            "datetime": _dt,
            "timedelta": _td,
            "Button": _FakeButton,
            "TPILOT_DB_PATH": db_path,
            "normalize_manager_key": manager_registry.normalize_manager_key,
            "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        },
    )


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
# 1/2. New "Отчёты" (N1.1: renamed from "Отчёты и контроль" -- control
# functions live in Система) has Статистика / Дубликаты / Экспорт / Качество
# ======================================================================

def test_1_reports_has_stats_duplicates_export() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        text, rows = ns["_nm_reports_screen"]()
        check("1. Reports title uses the N1.1 name '📊 ОТЧЁТЫ' (control lives in Система)", "📊 ОТЧЁТЫ" in text, text)
        check("1. Reports title no longer contains 'И КОНТРОЛЬ' (N1.1 fix C1)", "И КОНТРОЛЬ" not in text, text)
        # N5.3.1: canonical breadcrumbs are «Главная → …» now (no «Новое
        # меню»/«Админ-бот» in nested canonical paths -- owner decision 13/14).
        check("1. Reports breadcrumb is 'Главная → Отчёты' without 'и контроль' (N1.1 C1 + N5.3.1)",
              "Главная → Отчёты" in text and "Отчёты и контроль" not in text and "Новое меню →" not in text, text)
        check("1. Reports has '📊 Статистика' -> menu:nm_stats2", _find(rows, label="📊 Статистика", data=b"menu:nm_stats2") is not None, rows)
        check("1. Reports has '🔁 Дубликаты' -> menu:nm_duplicates", _find(rows, label="🔁 Дубликаты", data=b"menu:nm_duplicates") is not None, rows)
        check("1. Reports has '📤 Экспорт' -> menu:nm_excel", _find(rows, label="📤 Экспорт", data=b"menu:nm_excel") is not None, rows)
        check("1. Reports has '📈 Качество' -> menu:nm_quality_w (N1 merge)", _find(rows, label="📈 Качество", data=b"menu:nm_quality_w") is not None, rows)
    finally:
        os.unlink(db_path)


def test_2_reports_does_not_have_funnel_llm_transfers_unanswered() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        _text, rows = ns["_nm_reports_screen"]()
        labels = " | ".join(_labels(rows))
        check("2. Reports no longer has Воронка", not any("Воронк" in l for l in _labels(rows)), labels)
        check("2. Reports no longer has any LLM entry", not any("LLM" in l for l in _labels(rows)), labels)
        check("2. Reports no longer has Передачи", not any("Передач" in l for l in _labels(rows)), labels)
        check("2. Reports no longer has Неотвеченные", not any("Неотвеч" in l for l in _labels(rows)), labels)
        datas = _datas(rows)
        check("2. Reports callback data has no nm_funnel_w/llm/transfer/unanswered route",
              not any(b"funnel" in d or b"llm" in d or b"transfer" in d or b"unanswered" in d for d in datas), datas)
    finally:
        os.unlink(db_path)


def test_2b_reports_has_no_standalone_flights_button() -> None:
    """Долёты is already a mode inside the new 📊 Статистика wizard (nms:) -- Reports
    must not additionally offer a duplicate entry into the old, heavier /flight screen."""
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        text, rows = ns["_nm_reports_screen"]()
        labels = " | ".join(_labels(rows))
        check("2b. Reports has no '🌙 Долёты' / '✈️ Долёты' button", not any("Долёт" in l for l in _labels(rows)), labels)
        check("2b. Reports callback data has no nm_flights route", not any(b"nm_flights" in d for d in _datas(rows)), _datas(rows))
        content_labels = [l for l in _labels(rows) if l not in ("⬅️ Назад", "🏠 Новое меню", "🔄 Обновить")]
        # N4 (2026-07-21): "👥 По менеджерам" added -- the classic manager-first
        # flow (menu:nm_stats, unchanged route/logic), promoted here from an
        # ambiguously-labeled "📦 Классическая статистика" fallback button that
        # used to be nested one level deeper inside the NMS source-picker
        # screen (see tools/n4_canonicalization_selftest.py for the dedicated
        # N4 regression coverage of that move).
        # N5.3 (Part C): «📷 Скрины по датам» (ssv:dates) moved here from
        # Система -- Отчёты is the canonical owner of global date-based
        # screenshots now.
        check("2b. Reports content buttons are exactly Статистика, По менеджерам, Детально сегодня, Дубликаты, Экспорт, Качество, Скрины по датам (N1+N4+N5.3; footer excluded)",
              sorted(content_labels) == sorted(["📊 Статистика", "👥 По менеджерам", "🔎 Детально сегодня", "🔁 Дубликаты", "📤 Экспорт", "📈 Качество", "📷 Скрины по датам"]),
              content_labels)
        check("2b. Reports emits ssv:dates (global screenshots owner, N5.3)",
              _find(rows, data=b"ssv:dates") is not None, rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 3. Traffic has exactly one Воронка
# ======================================================================

def test_3_traffic_has_exactly_one_funnel() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        text, rows = ns["_nm_traffic_screen"]()
        funnel_buttons = [b for b in _flat(rows) if "Воронк" in b[1]]
        check("3. Traffic has exactly one Воронка button", len(funnel_buttons) == 1, funnel_buttons)
        check("3. Traffic's Воронка uses the approved label '📊 Воронка'", funnel_buttons and funnel_buttons[0][1] == "📊 Воронка", funnel_buttons)
        check("3. Traffic's Воронка routes to menu:nm_funnel_w", funnel_buttons and funnel_buttons[0][2] == b"menu:nm_funnel_w", funnel_buttons)
        check("3. Traffic hint no longer claims funnel lives in Reports", "«📊 Отчёты»" not in text, text)
    finally:
        os.unlink(db_path)


# ======================================================================
# 3b. Phase N1: Источники merged into Трафик и источники (nm_traffic),
# same targets reused from the retired _nm_sources_screen.
# ======================================================================

def test_3b_traffic_has_sources_merged_in() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        text, rows = ns["_nm_traffic_screen"]()
        check("3b. Traffic screen title is '🚦 ТРАФИК И ИСТОЧНИКИ'", "ТРАФИК И ИСТОЧНИКИ" in text, text)
        check("3b. Traffic has '📌 Источники' -> menu:sources", _find(rows, label="📌 Источники", data=b"menu:sources") is not None, rows)
        check("3b. Traffic has '📊 Статистика источников' -> cmd:/source stats all",
              _find(rows, label="📊 Статистика источников", data=b"cmd:/source stats all") is not None, rows)
        check("3b. Traffic has '➕ Добавить источник' -> wiz:add_source:start",
              _find(rows, label="➕ Добавить источник", data=b"wiz:add_source:start") is not None, rows)
        check("3b. Traffic still has Байеры/Группы/Список байеров (unchanged)",
              _find(rows, label="🧑‍💼 Байеры и доступы", data=b"menu:partners") is not None
              and _find(rows, label="👥 Группы", data=b"menu:groups") is not None
              and _find(rows, label="📋 Список байеров", data=b"cmd:/partner list") is not None,
              rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 4/5. Automation has LLM: наблюдения (read-only) + dynamic toggle,
# toggle requires confirmation, cancel changes nothing, confirm changes
# only llm_supervisor_enabled, Back-after-action goes to nm_automation.
# ======================================================================

def test_4_automation_has_llm_observations_and_dynamic_toggle() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)

        ns["_pb_set_setting"]("llm_supervisor_enabled", "1")
        _text, rows_on = ns["_nm_automation_screen"]()
        check("4. Automation has '🤖 LLM: наблюдения' -> menu:nm_llm_status",
              _find(rows_on, label="🤖 LLM: наблюдения", data=b"menu:nm_llm_status") is not None, rows_on)
        check("4. Automation toggle shows ВКЛ state and routes to confirm screen",
              _find(rows_on, label="🟢 LLM: ВКЛ", data=b"menu:nm_llm_toggle_confirm") is not None, rows_on)

        ns["_pb_set_setting"]("llm_supervisor_enabled", "0")
        _text, rows_off = ns["_nm_automation_screen"]()
        check("4. Automation toggle shows ВЫКЛ state when setting is off",
              _find(rows_off, label="🔴 LLM: ВЫКЛ", data=b"menu:nm_llm_toggle_confirm") is not None, rows_off)
    finally:
        os.unlink(db_path)


def test_5_llm_toggle_requires_confirmation_and_mutates_only_its_own_key() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        title_for_menu = ns["_title_for_menu"]

        ns["_pb_set_setting"]("llm_supervisor_enabled", "1")
        ns["_pb_set_setting"]("unrelated_setting", "untouched")

        # Clicking the automation toggle button does NOT flip the setting by
        # itself -- it only opens a confirm screen (button data proven above);
        # rendering that confirm screen must also not mutate anything.
        confirm_text, confirm_rows = title_for_menu("nm_llm_toggle_confirm")
        check("5. Confirm screen mentions the CURRENT state (включён)", "включён" in confirm_text, confirm_text)
        # N3: the confirm button now encodes the TARGET action in the route
        # (not the previously observed state) -- current='1' -> target='0'.
        check("5. Confirm screen has '✅ Подтвердить' -> menu:nm_llm_toggle_do:0 (target-encoded)",
              _find(confirm_rows, label="✅ Подтвердить", data=b"menu:nm_llm_toggle_do:0") is not None, confirm_rows)
        check("5. Confirm screen has '❌ Отмена' -> menu:nm_automation",
              _find(confirm_rows, label="❌ Отмена", data=b"menu:nm_automation") is not None, confirm_rows)
        check("5. Merely rendering the confirm screen does not flip the setting",
              ns["_pb_get_setting"]("llm_supervisor_enabled") == "1", ns["_pb_get_setting"]("llm_supervisor_enabled"))

        # Simulate pressing "❌ Отмена": the button just routes to nm_automation,
        # no settings write happens as part of that route.
        _cancel_text, _cancel_rows = title_for_menu("nm_automation")
        check("5. Cancel path (nm_automation) leaves the setting unchanged",
              ns["_pb_get_setting"]("llm_supervisor_enabled") == "1", ns["_pb_get_setting"]("llm_supervisor_enabled"))
        check("5. Cancel path leaves the unrelated setting untouched too",
              ns["_pb_get_setting"]("unrelated_setting") == "untouched", ns["_pb_get_setting"]("unrelated_setting"))

        # N3: stale-state no-op -- if the setting somehow already matches the
        # confirm screen's encoded target by the time "do" is clicked (e.g. a
        # duplicate click, or someone else changed it), the target-encoded
        # route must NOT reapply the action; it must re-render with a notice.
        ns["_pb_set_setting"]("llm_supervisor_enabled", "0")  # already at the target='0' the confirm screen encoded
        stale_text, stale_rows = title_for_menu("nm_llm_toggle_do:0")
        check("5. Stale target-encoded do: (already at target) shows a notice and does not error",
              "уже изменилось" in stale_text, stale_text)
        check("5. Stale target-encoded do: leaves the setting exactly as-is (no redundant write)",
              ns["_pb_get_setting"]("llm_supervisor_enabled") == "0", ns["_pb_get_setting"]("llm_supervisor_enabled"))
        ns["_pb_set_setting"]("llm_supervisor_enabled", "1")  # restore for the rest of the test

        # Simulate pressing "✅ Подтвердить" (target-encoded): this is the
        # path that actually mutates.
        do_text, do_rows = title_for_menu("nm_llm_toggle_do:0")
        check("5. Confirming (target-encoded) flips llm_supervisor_enabled to '0'",
              ns["_pb_get_setting"]("llm_supervisor_enabled") == "0", ns["_pb_get_setting"]("llm_supervisor_enabled"))
        check("5. Confirming does not touch any other setting",
              ns["_pb_get_setting"]("unrelated_setting") == "untouched", ns["_pb_get_setting"]("unrelated_setting"))
        check("5. After confirming, Back/landing screen is nm_automation itself (not reports_control)",
              "АВТОМАТИЗАЦИЯ" in do_text, do_text)
        check("5. After confirming, the automation screen now shows ВЫКЛ",
              _find(do_rows, label="🔴 LLM: ВЫКЛ") is not None, do_rows)

        # Confirm it round-trips (toggle back on via the target-encoded route)
        # and never leaves a stray key.
        title_for_menu("nm_llm_toggle_do:1")
        check("5. Toggling again (target-encoded) flips back to '1'", ns["_pb_get_setting"]("llm_supervisor_enabled") == "1", None)

        # N3: the bare historical route (no target) is kept working verbatim
        # for any already-sent Telegram confirm screen -- unconditional flip.
        title_for_menu("nm_llm_toggle_do")
        check("5. Historical bare 'nm_llm_toggle_do' route still flips unconditionally (backward compat)",
              ns["_pb_get_setting"]("llm_supervisor_enabled") == "0", ns["_pb_get_setting"]("llm_supervisor_enabled"))
        title_for_menu("nm_llm_toggle_do")
        check("5. Historical bare route round-trips too", ns["_pb_get_setting"]("llm_supervisor_enabled") == "1", None)
    finally:
        os.unlink(db_path)


# ======================================================================
# 6. Качество и дисциплина has Неотвеченные (nm_quality_w). N1: its own
# canonical parent/Back-target is now nm_reports (merged into Отчёты),
# not nm_root.
# ======================================================================

def test_6_quality_screen_has_unanswered_with_correct_back() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        title_for_menu = ns["_title_for_menu"]

        text, rows = title_for_menu("nm_quality_w")
        check("6. Качество и дисциплина screen wraps the old quality content", text.startswith("OLD_SCREEN:quality"), text)
        check("6. Качество и дисциплина has '🔔 Неотвеченные' -> menu:nm_unanswered",
              _find(rows, label="🔔 Неотвеченные", data=b"menu:nm_unanswered") is not None, rows)
        check("6. Качество и дисциплина Back-target is nm_reports (N1: merged into Отчёты)",
              _find(rows, label="⬅️ Назад", data=b"menu:nm_reports") is not None, rows)
        check("6. Качество и дисциплина no longer Backs to nm_root directly",
              _find(rows, label="⬅️ Назад", data=b"menu:nm_root") is None, rows)
        check("6. The stale stub back button (menu:main) from the wrapped old screen was stripped",
              _find(rows, data=b"menu:main") is None, rows)

        u_text, u_rows = title_for_menu("nm_unanswered")
        check("6. Неотвеченные screen wraps the old unanswered content", u_text.startswith("OLD_SCREEN:unanswered"), u_text)
        check("6. Неотвеченные Back-target is nm_quality_w (not nm_reports)",
              _find(u_rows, label="⬅️ Назад", data=b"menu:nm_quality_w") is not None, u_rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 6b. N1: Health Summary / Telegram Health canonical parent/Back-target
# is now nm_system (merged Контроль -> Система), not nm_control.
# ======================================================================

def test_6b_health_and_tghealth_back_target_is_system() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        title_for_menu = ns["_title_for_menu"]

        h_text, h_rows = title_for_menu("nm_health_w")
        check("6b. Health Summary screen wraps the old health content", h_text.startswith("OLD_SCREEN:health"), h_text)
        check("6b. Health Summary Back-target is nm_system (N1: merged into Система)",
              _find(h_rows, label="⬅️ Назад", data=b"menu:nm_system") is not None, h_rows)
        check("6b. Health Summary no longer Backs to nm_control",
              _find(h_rows, label="⬅️ Назад", data=b"menu:nm_control") is None, h_rows)

        t_text, t_rows = title_for_menu("nm_tghealth_w")
        check("6b. Telegram Health screen wraps the old tghealth content", t_text.startswith("OLD_SCREEN:tghealth"), t_text)
        check("6b. Telegram Health Back-target is nm_system (N1: merged into Система)",
              _find(t_rows, label="⬅️ Назад", data=b"menu:nm_system") is not None, t_rows)
        check("6b. Telegram Health no longer Backs to nm_control",
              _find(t_rows, label="⬅️ Назад", data=b"menu:nm_control") is None, t_rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 7. Root keeps a single, separate 🤝 Передачи; no duplicate inside
# Reports (already proven by test_2, re-asserted here from the root side).
# ======================================================================

def test_7_root_has_single_transfers_section() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        text, rows = ns["_nm_root_screen"]()
        transfer_buttons = [b for b in _flat(rows) if "Передач" in b[1]]
        check("7. Root has exactly one Передачи entry", len(transfer_buttons) == 1, transfer_buttons)
        check("7. Root's Передачи routes to menu:nm_transfers", transfer_buttons and transfer_buttons[0][2] == b"menu:nm_transfers", transfer_buttons)
        check("7. Root no longer exposes a standalone top-level Export entry (moved into Reports)",
              _find(rows, data=b"menu:nm_export") is None, rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 7d. Phase N5.1 (2026-07-21): root corrected to the owner-approved
# 8-row/12-button layout -- Добавить менеджера, AI-ассистент, 8 canonical
# sections (Автоматизация relabeled ⚙️), Удалить менеджера, Обновить.
# Поиск/Описание/Старое меню removed from root entirely (still reachable
# elsewhere -- see menu_parity_selftest.py for that capability mapping).
#
# N5.2 Part A (2026-07-21): Удалить менеджера's target changed from the
# generic menu:manager_settings list to the dedicated selection screen
# menu:nm_manager_delete (see menu_parity_selftest.py / manager_delete_
# safety_selftest.py for the deep safety proofs on that new screen).
# ======================================================================

_EXPECTED_ROOT_ROWS_N51 = [
    [("➕ Добавить менеджера", b"wiz:add_manager:start")],
    [("🤖 AI-ассистент", b"menu:nm_ai_assistant")],
    [("📊 Отчёты", b"menu:nm_reports"), ("👥 Менеджеры", b"menu:nm_managers")],
    [("🚦 Трафик и источники", b"menu:nm_traffic"), ("🌐 Прокси", b"menu:nm_proxy")],
    [("🔗 Бизнес-ссылки", b"menu:nm_bizlinks"), ("🤝 Передачи", b"menu:nm_transfers")],
    [("⚙️ Автоматизация", b"menu:nm_automation"), ("🛠 Система", b"menu:nm_system")],
    [("⏸ Остановка и удаление", b"menu:nm_manager_delete")],
    [("🔄 Обновить", b"menu:main")],
]


def test_7d_root_has_exactly_the_corrected_n51_layout() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        text, rows = ns["_nm_root_screen"]()
        labels = _labels(rows)
        datas = _datas(rows)

        check("7d. Root has exactly 8 rows", len(rows) == 8, [len(r) for r in rows])
        row_counts = [len(r) for r in rows]
        check("7d. Root row button counts are exactly [1,1,2,2,2,2,1,1]",
              row_counts == [1, 1, 2, 2, 2, 2, 1, 1], row_counts)
        check("7d. Root has exactly 12 raw buttons total", len(_flat(rows)) == 12, len(_flat(rows)))

        actual_rows = [[(b[1], b[2]) for b in row] for row in rows]
        check("7d. Root rows match the exact corrected layout, in exact order (labels+callbacks)",
              actual_rows == _EXPECTED_ROOT_ROWS_N51, actual_rows)

        datas_no_noop = [d for d in datas if d != b"noop"]
        dupes = sorted({d for d in datas_no_noop if datas_no_noop.count(d) > 1})
        check("7d. Root has no duplicate callback_data", not dupes, dupes)

        for b in _flat(rows):
            check(f"7d. Root callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))

        check("7d. Search (🔎 Поиск) absent from root", not any("Поиск" in l for l in labels), labels)
        check("7d. Description (ℹ️ Описание) absent from root", not any(l.startswith("ℹ️") for l in labels), labels)
        check("7d. Old menu (↩️ Старое меню) absent from root", not any("Старое меню" in l for l in labels), labels)
        check("7d. AI-ассистент present exactly once", sum(1 for l in labels if "AI-ассистент" in l) == 1, labels)
        check("7d. Добавить менеджера present exactly once", sum(1 for l in labels if l == "➕ Добавить менеджера") == 1, labels)
        check("7d. Остановка и удаление present exactly once (N5.3 rename of Удалить менеджера)",
              sum(1 for l in labels if l == "⏸ Остановка и удаление") == 1, labels)
        check("7d. Обновить present exactly once", sum(1 for l in labels if l == "🔄 Обновить") == 1, labels)
        check("7d. Root no longer has a standalone '🧭 Контроль' button", not any("Контроль" in l for l in labels), labels)
        check("7d. Root no longer has a standalone '📌 Источники' button", not any(l == "📌 Источники" for l in labels), labels)
        check("7d. Root no longer has a standalone '📈 Качество и дисциплина' button",
              not any("Качество" in l for l in labels), labels)
        check("7d. Root callback data has no dangling nm_control/nm_sources route",
              not any(d in (b"menu:nm_control", b"menu:nm_sources") for d in datas), datas)
        check("7d. Root does not emit menu:nm_search / menu:description / menu:old_root directly",
              not any(d in (b"menu:nm_search", b"menu:description", b"menu:old_root") for d in datas), datas)
        check("7d. Удалить менеджера routes to the dedicated menu:nm_manager_delete screen, "
              "not the generic menu:manager_settings list",
              _find(rows, data=b"menu:nm_manager_delete") is not None
              and _find(rows, data=b"menu:manager_settings") is None,
              rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 7e. Phase N1: the retired _nm_control_screen / _nm_sources_screen
# builder functions stay fully defined and dispatchable in the routing
# table -- only unlinked from the root, nothing deleted (same pattern
# already established for nm_export/nm_flights before this migration).
# ======================================================================

def test_7e_retired_control_and_sources_screens_stay_defined_and_dispatchable() -> None:
    for marker in ("def _nm_control_screen", "def _nm_sources_screen",
                   'if raw == "nm_control":', 'if raw == "nm_sources":'):
        check(f"7e. retired NM screen still defined/dispatchable: {marker!r}", marker in PANEL_SRC, marker)


# ======================================================================
# 7b. Corrective fix: the new 🩺 Система screen must not offer a second,
# unconfirmed path to the LLM toggle (old menu:llm_toggle). The only new-
# menu path to the toggle is Автоматизация -> nm_llm_toggle_confirm/_do.
# ======================================================================

def test_7b_system_screen_has_no_llm_toggle_duplicate() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        text, rows = ns["_nm_system_screen"]()
        labels = " | ".join(_labels(rows))
        datas = _datas(rows)
        check("7b. System screen has no LLM-labeled button at all", not any("LLM" in l for l in _labels(rows)), labels)
        check("7b. System screen callback data never references llm_toggle", not any(b"llm_toggle" in d for d in datas), datas)
        check("7b. System screen callback data never references llm at all", not any(b"llm" in d for d in datas), datas)
        check("7b. System still has its other real entries (Сервис/Помощь/Инструкция)",
              _find(rows, label="🧰 Сервис", data=b"menu:nm_service_w") is not None
              and _find(rows, label="❓ Помощь", data=b"cmd:/help") is not None
              and _find(rows, label="📘 Инструкция", data=b"cmd:/instructions") is not None,
              rows)
        check("7b. System hint text no longer claims to host the LLM switch", "LLM-переключатель, сервис" not in text, text)
    finally:
        os.unlink(db_path)


def test_7c_no_nm_screen_anywhere_references_old_llm_toggle() -> None:
    """Exhaustive static sweep: EVERY top-level _nm_*_screen builder in panel_bot.py
    (not just the ones this migration directly touched) must not reference the old
    menu:llm_toggle callback. Static (source-text) rather than execution-based, so it
    also catches any nm screen this test suite doesn't otherwise extract/exercise."""
    tree = ast.parse(PANEL_SRC)
    nm_screen_fns = [n for n in tree.body
                      if isinstance(n, ast.FunctionDef) and str(getattr(n, "name", "")).startswith("_nm_")
                      and str(n.name).endswith("_screen")]
    check("7c. found a healthy number of nm_*_screen functions to sweep (sanity)", len(nm_screen_fns) >= 10, len(nm_screen_fns))
    offenders = []
    for n in nm_screen_fns:
        try:
            src_txt = ast.unparse(n)
        except Exception:
            continue
        if "menu:llm_toggle" in src_txt:
            offenders.append(n.name)
    check("7c. no _nm_*_screen function anywhere references the old menu:llm_toggle callback",
          not offenders, offenders)

    # Also sweep the one _title_for_menu generation that owns nm_* routing itself,
    # in case a route were wired to the old callback directly rather than through a
    # screen-builder function.
    edited = _find_edited_title_for_menu(tree, "nm_llm_toggle_do")
    check("7c. the nm_* routing table's active _title_for_menu generation was found for the sweep", edited is not None, None)
    if edited is not None:
        routing_src = ast.unparse(edited)
        check("7c. the nm_* routing table does not dispatch any route to menu:llm_toggle",
              "menu:llm_toggle" not in routing_src, None)
        check("7c. the nm_* routing table's only LLM-toggle paths are nm_llm_toggle_confirm/_do",
              "nm_llm_toggle_confirm" in routing_src and "nm_llm_toggle_do" in routing_src, None)


# ======================================================================
# 7f. Phase N1: Контроль merged into Система (nm_system), same targets
# reused from the retired _nm_control_screen.
# ======================================================================

def test_7f_system_has_control_merged_in() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_routing_ns(db_path)
        text, rows = ns["_nm_system_screen"]()
        check("7f. System screen title is '🛠 СИСТЕМА'", "СИСТЕМА" in text, text)
        check("7f. System has '🩺 Health Summary' -> menu:nm_health_w",
              _find(rows, label="🩺 Health Summary", data=b"menu:nm_health_w") is not None, rows)
        check("7f. System has '📡 Telegram Health' -> menu:nm_tghealth_w",
              _find(rows, label="📡 Telegram Health", data=b"menu:nm_tghealth_w") is not None, rows)
        check("7f. System has '🧨 Опасные действия' -> cmd:/manager_danger_log",
              _find(rows, label="🧨 Опасные действия", data=b"cmd:/manager_danger_log") is not None, rows)
        check("7f. System has '🔄 TG проверка всех' -> cmd:/tghealth check all",
              _find(rows, label="🔄 TG проверка всех", data=b"cmd:/tghealth check all") is not None, rows)
        # N5.3 (Part C): global screenshots moved to Отчёты -- Система must
        # NOT offer them any more (see test_2b for the positive side).
        check("7f. System does NOT have '📷 Скрины по датам' any more (moved to Отчёты, N5.3)",
              _find(rows, data=b"ssv:dates") is None, rows)
        check("7f. System still has Сервис/Помощь/Инструкция (unchanged)",
              _find(rows, label="🧰 Сервис", data=b"menu:nm_service_w") is not None
              and _find(rows, label="❓ Помощь", data=b"cmd:/help") is not None
              and _find(rows, label="📘 Инструкция", data=b"cmd:/instructions") is not None,
              rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 8. Old callbacks and old menu are NOT deleted.
# ======================================================================

def test_8_old_menu_not_deleted() -> None:
    markers = [
        "def _stats_menu", "def _flights_menu", "def _quality_menu", "def _funnel_menu",
        "def _duplicates_menu", "def _unanswered_menu", "def _export_menu",
        "_tp_visual_reports_menu", "menu:reports_control",
        'if raw == "llm_toggle":', "_tp_llm_status_buttons",
        'raw_menu == "flights"', "menu:flights",
        'if raw == "nm_flights":',  # the internal nm wrapper route stays defined (orphaned,
                                     # not deleted) even though no button links to it anymore
    ]
    for m in markers:
        check(f"8. old-menu marker still present: {m!r}", m in PANEL_SRC, m)
    # N4 fix (Part B, 2026-07-21): the OLD llm_toggle route used to flip
    # llm_supervisor_enabled immediately/unconditionally and return to
    # reports_control -- the exact unsafe pattern N3/N3.1 eliminated for the
    # canonical NM route. It no longer does that (route handler kept, per
    # requirement #4 -- "do not remove the old route handler" -- but its
    # BODY now safely delegates to the canonical confirm screen instead of
    # mutating state on click). See tools/n4_canonicalization_selftest.py
    # test_8/test_14 for the dedicated behavioral regression coverage
    # (proves no mutation on render + identical text/buttons to the
    # canonical menu:nm_llm_toggle_confirm, in one hop, no recursion).
    start = PANEL_SRC.find('if raw == "llm_toggle":')
    end = PANEL_SRC.find("\n    if ", start + 10) if start != -1 else -1
    llm_toggle_block = PANEL_SRC[start:end] if start != -1 and end != -1 else ""
    check("8. old llm_toggle route no longer performs the unsafe immediate/unconditional flip",
          "_pb_set_setting(" not in llm_toggle_block, llm_toggle_block)
    check("8. old llm_toggle route no longer returns directly to reports_control (that was the unsafe-flip's own re-render)",
          '_TPLLMF1_PREV_TITLE_FOR_MENU("reports_control")' not in llm_toggle_block, llm_toggle_block)
    check("8. old llm_toggle route now delegates to the canonical confirm screen (single hop, no new implementation)",
          "_nm_llm_toggle_confirm_text()" in llm_toggle_block and "_nm_llm_toggle_confirm_buttons()" in llm_toggle_block,
          llm_toggle_block)


# ======================================================================
# 9. Existing commands for export/duplicates/funnel/unanswered remain
# exactly the same -- run the REAL production menu builders.
# ======================================================================

def test_9_existing_commands_unchanged() -> None:
    db_path = _make_temp_db()
    _seed_managers(db_path, [{"manager_key": "mgr01", "display_name": "Manager 01"}])
    try:
        ns = build_legacy_ns(db_path)

        export_rows = ns["_export_menu"]()
        export_datas = _datas(export_rows)
        for expected in (b"cmd:/export all", b"cmd:/export flight all",
                          b"cmd:/export all 7d", b"cmd:/export all 31d",
                          b"cmd:/export flight all 7d", b"cmd:/export flight all 31d",
                          b"wiz:period:export", b"cmd:/export mgr01", b"cmd:/export flight mgr01"):
            check(f"9. /export menu still has {expected!r}", expected in export_datas, export_datas)

        dup_rows = ns["_duplicates_menu"]()
        dup_datas = _datas(dup_rows)
        for expected in (b"cmd:/duplicates today", b"cmd:/duplicates yesterday",
                          b"cmd:/duplicates 31d", b"cmd:/duplicates excel 31d"):
            check(f"9. /duplicates menu still has {expected!r}", expected in dup_datas, dup_datas)

        funnel_rows = ns["_funnel_menu"]()
        funnel_datas = _datas(funnel_rows)
        for expected in (b"cmd:/funnel sources", b"cmd:/funnel groups", b"cmd:/funnel links",
                          b"cmd:/funnel losses", b"cmd:/funnel best", b"cmd:/funnel help"):
            check(f"9. /funnel menu still has {expected!r}", expected in funnel_datas, funnel_datas)

        unans_rows = ns["_unanswered_menu"]()
        unans_datas = _datas(unans_rows)
        for expected in (b"cmd:/unanswered all", b"cmd:/unanswered mgr01",
                          b"cmd:/unanswered_notify status"):
            check(f"9. /unanswered menu still has {expected!r}", expected in unans_datas, unans_datas)
        # N3: the on/off pair collapsed into a single dynamic toggle -- both
        # commands still exist and are reachable, but exactly ONE is shown
        # at a time depending on the current persisted state (no settings
        # row in this temp DB -> default enabled -> "off" is the one shown).
        on_present = b"cmd:/unanswered_notify on" in unans_datas
        off_present = b"cmd:/unanswered_notify off" in unans_datas
        check("9. /unanswered menu shows exactly ONE of on/off (single dynamic toggle, not a pair)",
              on_present != off_present, (on_present, off_present))
        check("9. /unanswered menu (default-enabled state) shows the 'off' action",
              off_present and not on_present, unans_datas)

        quality_rows = ns["_quality_menu"]()
        quality_datas = _datas(quality_rows)
        for expected in (b"cmd:/quality all", b"cmd:/quality response", b"cmd:/quality sla",
                          b"cmd:/quality ranking", b"cmd:/quality losses"):
            check(f"9. /quality menu still has {expected!r}", expected in quality_datas, quality_datas)
    finally:
        os.unlink(db_path)

    # Transfer stats and LLM-status calc are untouched by this migration (not part of
    # ROUTING_NAMES/LEGACY_NAMES edits) -- confirm their source markers are intact.
    check("9. transfer stats route/command untouched (menu:transfer_stats)", "menu:transfer_stats" in PANEL_SRC, None)
    check("9. transfer Excel export untouched (trx:xlsx:)", "trx:xlsx:" in PANEL_SRC, None)
    check("9. LLM status calc function untouched (_tp_llm_status_screen_text still defined)",
          "def _tp_llm_status_screen_text" in PANEL_SRC, None)


def test_9b_new_llm_status_route_reuses_the_real_calc_function() -> None:
    """The new nm_llm_status route must call the SAME _tp_llm_status_screen_text (no
    duplicated calculation) and apply the SAME 'LLM выключен' warning-prefix logic as
    the old llm_status route. Proven by injecting a distinguishable fake calc function
    and checking it is actually invoked with the right period, plus checking the
    warning-prefix branch fires under the same condition as the old route."""
    db_path = _make_temp_db()
    try:
        calls = []

        def _fake_calc(period="today"):
            calls.append(period)
            return f"CALC:{period}"

        ns = build_routing_ns(db_path, fake_llm_status_text=_fake_calc)
        title_for_menu = ns["_title_for_menu"]

        ns["_pb_set_setting"]("llm_supervisor_enabled", "1")
        text_on, rows_on = title_for_menu("nm_llm_status:week")
        check("9b. nm_llm_status passes the period through to the real calc function", calls == ["week"], calls)
        check("9b. nm_llm_status shows the calc function's output verbatim", "CALC:week" in text_on, text_on)
        check("9b. nm_llm_status does not show the LLM-off warning when LLM is on", "выключен" not in text_on, text_on)
        check("9b. nm_llm_status buttons include a period switcher for week/month/today/yesterday",
              any(d.startswith(b"menu:nm_llm_status:") for d in _datas(rows_on)), rows_on)
        check("9b. nm_llm_status Back-target is nm_automation, not reports_control",
              _find(rows_on, label="⬅️ Назад", data=b"menu:nm_automation") is not None, rows_on)

        ns["_pb_set_setting"]("llm_supervisor_enabled", "0")
        text_off, _rows_off = title_for_menu("nm_llm_status")
        check("9b. nm_llm_status shows the LLM-off warning when LLM is off (same rule as old route)",
              "выключен" in text_off, text_off)
    finally:
        os.unlink(db_path)


# ======================================================================
# 10. The finished nms: statistics block is untouched by this migration.
# ======================================================================

def test_10_nms_block_untouched() -> None:
    for marker in ("def _nms_callback", "nms:pickmgr:", "_nms_render_mgrlist", "_nms_sources_screen"):
        check(f"10. nms: block marker still present: {marker!r}", marker in PANEL_SRC, marker)
    check("10. nms: manager-first entry point untouched", "nms:entry:mgr" in PANEL_SRC, None)


def main() -> int:
    test_1_reports_has_stats_duplicates_export()
    test_2_reports_does_not_have_funnel_llm_transfers_unanswered()
    test_2b_reports_has_no_standalone_flights_button()
    test_3_traffic_has_exactly_one_funnel()
    test_3b_traffic_has_sources_merged_in()
    test_4_automation_has_llm_observations_and_dynamic_toggle()
    test_5_llm_toggle_requires_confirmation_and_mutates_only_its_own_key()
    test_6_quality_screen_has_unanswered_with_correct_back()
    test_6b_health_and_tghealth_back_target_is_system()
    test_7_root_has_single_transfers_section()
    test_7b_system_screen_has_no_llm_toggle_duplicate()
    test_7c_no_nm_screen_anywhere_references_old_llm_toggle()
    test_7d_root_has_exactly_the_corrected_n51_layout()
    test_7e_retired_control_and_sources_screens_stay_defined_and_dispatchable()
    test_7f_system_has_control_merged_in()
    test_8_old_menu_not_deleted()
    test_9_existing_commands_unchanged()
    test_9b_new_llm_status_route_reuses_the_real_calc_function()
    test_10_nms_block_untouched()

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
