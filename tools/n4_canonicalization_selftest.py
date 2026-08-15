# -*- coding: utf-8 -*-
"""tools/n4_canonicalization_selftest.py -- offline self-test for the approved
Phase N4 "canonicalize the remaining parallel menu entry points" migration in
panel_bot.py (2026-07-21):

  A. Statistics: the canonical Reports screen (_nm_reports_screen) has exactly
     ONE primary "Статистика" entry (menu:nm_stats2, NMS/source-first,
     unchanged), plus a new, clearly-named top-level "👥 По менеджерам"
     sibling reusing the EXISTING classic manager-first flow verbatim
     (menu:nm_stats -> _nm_wrap("stats", ...), no stats-engine/command logic
     duplicated). The ambiguously-labeled "📦 Классическая статистика"
     fallback button was removed from inside the NMS source-picker screen
     (_nms_sources_screen) -- same underlying route (menu:nm_stats) is still
     fully defined/dispatchable, just reachable via the new top-level entry
     instead of a jargon-labeled button one level deeper.
  B. LLM: the old bare "menu:llm_toggle" route (still emitted by the
     still-live old-menu Reports screen) no longer flips llm_supervisor_enabled
     immediately/unconditionally -- it now renders the exact SAME canonical,
     target-encoded, stale-safe confirm screen as menu:nm_llm_toggle_confirm
     (_nm_llm_toggle_confirm_text/_buttons, unchanged, reused verbatim -- no
     second toggle implementation). Rendering the confirm screen never
     mutates the setting.
  C. Proxy: the canonical Прокси screen (_nm_proxy_screen) now directly
     exposes Пул (menu:ppool) and Продление и баланс (prn:root) -- both
     reuse existing, already-live routes/handlers verbatim. No canonical NM
     screen touched by N4 emits renew:/prenew (those stay notification-
     deep-link-only, in the untouched _prenew_callback block).

Scope: panel_bot.py only (root Reports/Proxy screens + the old bare
llm_toggle route + search-index entries). Old menu screens (_tp_visual_*),
NMS wizard internals, proxy-pool/renewal business logic, and the classic
_stats_menu()/_stat_command chain are all untouched -- N4 only re-wires
which screen a capability is reachable from and fixes one unsafe route.

Techniques: panel_bot.py cannot be imported standalone (Telethon/env side
effects at import time) -- functions under test are extracted via
ast.parse + ast.unparse + exec() (same technique used throughout this
project's tools/*_selftest.py files) and run FOR REAL against a temporary
SQLite file (never db/data_tpilot.db or db/data.db).

_title_for_menu is defined ~30+ times via an override-stack (only the LAST
active definition matters at runtime). The old "menu:llm_toggle" handler
lives in ONE specific generation, isolated here by the marker
'raw == "llm_toggle"' (verified unique in the file -- the canonical NM LLM
generation only ever uses the longer 'nm_llm_toggle_do'/'nm_llm_toggle_confirm'
strings, never the bare quoted 'llm_toggle'), the same generation-isolation
idiom established in tools/nm_menu_distribution_selftest.py.

    python tools\\n4_canonicalization_selftest.py
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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="n4_canonicalization_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE settings(
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


def _set_setting(db_path: str, key: str, value: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES(?,?,datetime('now'))", (key, value))
        con.commit()
    finally:
        con.close()


def _read_setting(db_path: str, key: str) -> str:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return str(row[0] or "") if row else ""
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
    """ast.unparse always single-quotes string literals -- tolerate both
    (documented project pitfall, see ADDENDUM_FOR_NEW_PC.md)."""
    if marker in src_txt:
        return True
    if '"' in marker:
        return marker.replace('"', "'") in src_txt
    if "'" in marker:
        return marker.replace("'", '"') in src_txt
    return False


def _find_generation_by_marker(name: str, marker: str):
    """Locate the LAST def `name` whose unparsed source contains `marker`
    (quote-tolerant) -- same idiom as tools/nm_menu_distribution_selftest.py's
    _find_edited_title_for_menu, generalized to any target def name."""
    tree = ast.parse(PANEL_SRC)
    found = None
    for n in tree.body:
        if getattr(n, "name", None) == name:
            try:
                src_txt = ast.unparse(n)
            except Exception:
                continue
            if _contains_quote_tolerant(src_txt, marker):
                found = n
    return found


# ======================================================================
# Namespace builders.
# ======================================================================

def build_reports_ns(db_path: str) -> dict:
    """_nm_reports_screen (Part A) + _nms_sources_screen (Part A cleanup) --
    both single-def, safe to extract directly by name."""
    nodes = _extract_by_name({
        "_nm_reports_screen", "_nms_sources_screen", "_nms_sources",
        "_nm_screen", "_nm_canonical_path", "_nm_btn", "_nm_row", "_nm_footer", "_connect_panel_db",
    })
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "_panel_header": lambda: "HEADER",
    }
    exec(compile(module_src, f"<{PANEL_PATH}:reports>", "exec"), ns)
    return ns


def build_proxy_ns(db_path: str) -> dict:
    """_nm_proxy_screen (Part C) -- single-def, safe to extract directly."""
    nodes = _extract_by_name({
        "_nm_proxy_screen", "_nm_screen", "_nm_canonical_path", "_nm_btn", "_nm_row", "_nm_footer",
    })
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "_panel_header": lambda: "HEADER",
    }
    exec(compile(module_src, f"<{PANEL_PATH}:proxy>", "exec"), ns)
    return ns


def build_llm_toggle_ns(db_path: str) -> dict:
    """The ONE _title_for_menu generation that owns the old bare
    "llm_toggle" route (Part B), isolated by marker 'raw == "llm_toggle"'
    (verified unique -- the canonical NM generation only ever spells the
    longer nm_llm_toggle_do/nm_llm_toggle_confirm strings), plus its real
    dependencies (_nm_llm_toggle_confirm_text/_buttons -- the exact
    canonical functions it must now delegate to, _pb_get_setting/
    _pb_set_setting for real state, _connect_panel_db for the temp DB)."""
    generation = _find_generation_by_marker("_title_for_menu", 'raw == "llm_toggle"')
    if generation is None:
        raise AssertionError('build_llm_toggle_ns: could not locate the _title_for_menu '
                              'generation handling raw == "llm_toggle" -- marker not found')
    nodes = _extract_by_name({
        "_nm_llm_toggle_confirm_text", "_nm_llm_toggle_confirm_buttons",
        "_pb_get_setting", "_pb_set_setting", "_connect_panel_db",
    })
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "datetime": __import__("datetime").datetime,
        "_panel_header": lambda: "HEADER",
        # Deliberately fake -- isolates this test from every other
        # _title_for_menu generation (nms:, autostatus, ppool, ...), none of
        # which handle "llm_toggle".
        "_title_for_menu": lambda raw: (f"OLD_MENU:{raw}", []),
    }
    exec(compile(module_src, f"<{PANEL_PATH}:llm_toggle>", "exec"), ns)
    exec(compile(ast.unparse(generation), f"<{PANEL_PATH}:llm_toggle_routing>", "exec"), ns)
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


def _datas(rows) -> list:
    return [b[2] for b in _flat(rows)]


def _labels(rows) -> list:
    return [b[1] for b in _flat(rows)]


def _active_block(def_name: str) -> str:
    """Text of the LAST (active, last-wins) def `def_name`, up to the next
    top-level 'def ' -- same rfind-based scoping used elsewhere in this
    project's N3.1/N3.2 selftests, so a scan is never fooled by an earlier
    shadowed generation of the same name."""
    start = PANEL_SRC.rfind(f"def {def_name}")
    end = PANEL_SRC.find("\ndef ", start + 10) if start != -1 else -1
    return PANEL_SRC[start:end] if start != -1 and end != -1 else ""


# ======================================================================
# 1-10. Part A -- canonical Statistics.
# ======================================================================

def test_1_reports_has_exactly_one_primary_statistics_entry() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_reports_ns(db_path)
        text, rows = ns["_nm_reports_screen"]()
        stats_buttons = [b for b in _flat(rows) if b[1] == "📊 Статистика"]
        check("1/9. canonical Reports has exactly ONE '📊 Статистика' button", len(stats_buttons) == 1, stats_buttons)
        check("2. primary Статистика opens NMS (menu:nm_stats2)",
              _find(rows, label="📊 Статистика", data=b"menu:nm_stats2") is not None, rows)
    finally:
        os.unlink(db_path)


def test_2_manager_first_reachable_as_po_menedzheram() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_reports_ns(db_path)
        _text, rows = ns["_nm_reports_screen"]()
        check("3. 'По менеджерам' reachable from Reports, routes to the existing classic flow (menu:nm_stats, unchanged)",
              _find(rows, label="👥 По менеджерам", data=b"menu:nm_stats") is not None, rows)
        # 6. the classic route/handler itself is untouched -- still defined and dispatchable.
        # N4.1 fix (review W3): label corrected to match what's actually
        # asserted (was 'raw == "stats"', a leftover from before the marker
        # was fixed to the real variable name used at this call site).
        check("6. classic stats route still defined/dispatchable: 'menu_name == \"stats\"'",
              'menu_name == "stats"' in PANEL_SRC, None)
        check("6. classic stats route still wrapped for NM (menu:nm_stats -> _nm_wrap(\"stats\", ...))",
              '_nm_wrap("stats", "nm_reports"' in PANEL_SRC, None)
    finally:
        os.unlink(db_path)


def test_3_old_stats_callbacks_remain_handled() -> None:
    # 4/7/8. Old menu:stats / menu:nm_stats historical callbacks remain
    # handled -- not blindly redirected (which would destroy the valid
    # manager-first subflow), just still-live, unchanged routes.
    for marker in ('menu_name == "stats"', 'raw == "nm_stats"'):
        check(f"4/7/8. historical stats route still handled: {marker!r}", marker in PANEL_SRC, marker)


def test_4_no_ambiguous_duplicate_stats_labels() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_reports_ns(db_path)
        _text, reports_rows = ns["_nm_reports_screen"]()
        _text2, nms_rows = ns["_nms_sources_screen"]()
        all_labels = _labels(reports_rows) + _labels(nms_rows)
        for ambiguous in ("Статистика 2", "Новая статистика", "Классическая статистика", "📦 Классическая статистика"):
            check(f"5/10. no ambiguous duplicate label {ambiguous!r} on canonical Reports/NMS-root screens",
                  ambiguous not in all_labels, all_labels)
        # The old ambiguous button's route (menu:nm_stats) must still be
        # reachable from SOMEWHERE (it moved, it was not deleted).
        check("10. menu:nm_stats route still reachable from the canonical Reports screen after the rename/move",
              _find(reports_rows, data=b"menu:nm_stats") is not None, reports_rows)
    finally:
        os.unlink(db_path)


def test_5_nms_manager_first_subflow_still_present() -> None:
    # The NMS-native manager-first entry (nms:entry:mgr, a DIFFERENT,
    # source-window-aware capability from the classic flow) must remain
    # intact -- N4 did not remove or rename this route, only the ambiguous
    # classic-flow fallback button that used to sit next to it.
    db_path = _make_temp_db()
    try:
        ns = build_reports_ns(db_path)
        _text, nms_rows = ns["_nms_sources_screen"]()
        check("nms:entry:mgr manager-first subflow still present inside NMS root",
              _find(nms_rows, data=b"nms:entry:mgr") is not None, nms_rows)
    finally:
        os.unlink(db_path)


# ======================================================================
# 6-9. Part B -- canonical LLM supervisor.
# ======================================================================

def test_6_automation_screen_has_exactly_one_llm_action() -> None:
    check("6. canonical Automation LLM row present (unchanged)",
          '"🤖 LLM: наблюдения"' in PANEL_SRC and '"menu:nm_llm_toggle_confirm"' in PANEL_SRC, None)
    # Exactly one *action* route (nm_llm_toggle_confirm); the dashboard
    # button (nm_llm_status) is a read-only view, not a second action.
    automation_block = _active_block("_nm_automation_screen")
    action_hits = automation_block.count("nm_llm_toggle_confirm")
    check("6. exactly one LLM action route in the active Automation screen",
          action_hits == 1, (action_hits, automation_block[:400]))


def test_7_no_active_canonical_screen_emits_unsafe_llm_flip() -> None:
    # 7/9. No active canonical NM screen may emit a route that performs an
    # immediate unconditional flip -- specifically, the canonical Automation
    # screen must route through the confirm screen (menu:nm_llm_toggle_confirm),
    # never the bare unsafe "menu:llm_toggle".
    automation_block = _active_block("_nm_automation_screen")
    check("9. active canonical Automation screen does not emit the bare unsafe 'menu:llm_toggle'",
          "menu:llm_toggle" not in automation_block, automation_block[:400])
    proxy_block = _active_block("_nm_proxy_screen")
    reports_block = _active_block("_nm_reports_screen")
    for name, block in (("Proxy", proxy_block), ("Reports", reports_block)):
        check(f"9. active canonical {name} screen does not emit 'menu:llm_toggle'",
              "menu:llm_toggle" not in block, block[:200])


def test_8_historical_llm_toggle_redirects_to_canonical_confirm() -> None:
    # 3/8. Historical menu:llm_toggle buttons must safely render the SAME
    # canonical confirm screen as menu:nm_llm_toggle_confirm -- built from
    # FRESH current state, and must NEVER mutate the setting just by being
    # rendered (clicking the historical button must be as safe as clicking
    # the canonical one).
    db_path = _make_temp_db()
    try:
        for seed in ("1", "0", ""):
            if seed:
                _set_setting(db_path, "llm_supervisor_enabled", seed)
            ns = build_llm_toggle_ns(db_path)
            before = _read_setting(db_path, "llm_supervisor_enabled")
            old_text, old_buttons = ns["_title_for_menu"]("llm_toggle")
            after = _read_setting(db_path, "llm_supervisor_enabled")
            check(f"8. historical menu:llm_toggle (seed={seed!r}) does NOT mutate the setting on render",
                  before == after, (before, after))
            canonical_text = ns["_nm_llm_toggle_confirm_text"]()
            canonical_buttons = ns["_nm_llm_toggle_confirm_buttons"]()
            check(f"3/8. historical menu:llm_toggle (seed={seed!r}) renders the IDENTICAL canonical confirm text",
                  old_text == canonical_text, (old_text, canonical_text))
            check(f"3/8. historical menu:llm_toggle (seed={seed!r}) renders the IDENTICAL canonical confirm buttons",
                  _datas(old_buttons) == _datas(canonical_buttons), (_datas(old_buttons), _datas(canonical_buttons)))
            # Target is encoded correctly relative to fresh current state.
            is_on = (before != "0") if before else True  # default-on per canonical text logic (current != "0")
            confirm_btn = _find(old_buttons, label="✅ Подтвердить")
            check(f"6 (N3 preserved). historical confirm (seed={seed!r}) target-encodes the OPPOSITE of current state",
                  confirm_btn is not None and confirm_btn[2] == (b"menu:nm_llm_toggle_do:0" if is_on else b"menu:nm_llm_toggle_do:1"),
                  (confirm_btn, is_on))
    finally:
        os.unlink(db_path)


def test_9_bare_nm_llm_toggle_do_and_old_route_handler_preserved() -> None:
    # 4/10. Do not remove the old route handler; historical bare
    # nm_llm_toggle_do compatibility (defined in N3) remains untouched.
    check('4. old "llm_toggle" route handler still present (not removed)',
          'if raw == "llm_toggle":' in PANEL_SRC, None)
    check("10. historical bare 'nm_llm_toggle_do' route still present/handled",
          'if raw == "nm_llm_toggle_do":' in PANEL_SRC, None)
    check("7. LLM dashboard/status screen route still present",
          'raw == "nm_llm_status"' in PANEL_SRC, None)


# ======================================================================
# 10-14. Part C -- canonical Proxy renewal.
# ======================================================================

def test_10_proxy_screen_emits_prn_root() -> None:
    ns = build_proxy_ns("")
    _text, rows = ns["_nm_proxy_screen"]()
    check("10. canonical Proxy section emits prn:root", _find(rows, data=b"prn:root") is not None, rows)
    check("10. canonical Proxy section emits menu:ppool (pool)", _find(rows, data=b"menu:ppool") is not None, rows)


def test_11_no_active_canonical_menu_emits_renew_or_prenew() -> None:
    for name in ("_nm_proxy_screen", "_nm_reports_screen", "_nm_automation_screen", "_nm_root_screen"):
        block = _active_block(name)
        check(f"11. active {name} does not emit 'renew:'", '"renew:' not in block and "'renew:" not in block, block[:200])
        check(f"11. active {name} does not emit 'prenew'", "prenew" not in block, block[:200])


def test_12_notification_deeplink_handlers_for_renew_prenew_present() -> None:
    for marker in (
        'data.startswith("renew:calc:")', 'data.startswith("renew:confirm:")',
        'data.startswith("renew:cancel:")', 'data.startswith("renew:defer:")',
        "_prenew_notification_buttons", "_prenew_autorenew_failed_buttons", "_prenew_callback",
    ):
        check(f"12. notification/deep-link handler still present: {marker!r}", marker in PANEL_SRC, marker)


def test_13_prn_subroutes_present() -> None:
    for marker in ('data == "prn:root"', 'data == "prn:due"', 'data == "prn:errors"', 'data == "prn:config"'):
        check(f"13. prn: subroute still present: {marker!r}", marker in PANEL_SRC, marker)


def test_14_no_redirect_loop() -> None:
    # The old llm_toggle handler must delegate to the canonical confirm
    # functions directly (a single hop), never re-invoke _title_for_menu on
    # itself or on "llm_toggle" again -- structurally impossible to loop.
    start = PANEL_SRC.find('if raw == "llm_toggle":')
    end = PANEL_SRC.find("\n    if ", start + 10) if start != -1 else -1
    block = PANEL_SRC[start:end] if start != -1 and end != -1 else ""
    check("14. no redirect loop: llm_toggle handler does not call _title_for_menu recursively",
          "_title_for_menu(" not in block, block)
    check("14. llm_toggle handler delegates in one hop to the canonical confirm functions",
          "_nm_llm_toggle_confirm_text()" in block and "_nm_llm_toggle_confirm_buttons()" in block, block)


# ======================================================================
# 15-17. Callback safety audits.
# ======================================================================

def test_15_callback_length() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_reports_ns(db_path)
        _text, rows = ns["_nm_reports_screen"]()
        for b in _flat(rows):
            check(f"15. Reports callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
        _text2, nms_rows = ns["_nms_sources_screen"]()
        for b in _flat(nms_rows):
            check(f"15. NMS-root callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
    finally:
        os.unlink(db_path)
    ns2 = build_proxy_ns("")
    _text3, proxy_rows = ns2["_nm_proxy_screen"]()
    for b in _flat(proxy_rows):
        check(f"15. Proxy callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))


def _content_rows(rows: list) -> list:
    """Drop the standard _nm_footer row (last row: Back + Home) -- on a
    top-level section screen (default parent="nm_root") both legitimately
    point at the SAME menu:nm_root target, which is by-design NM footer
    behavior on every top-level screen (see test_2b's "footer excluded"
    convention in tools/nm_menu_distribution_selftest.py), not a duplicate
    action button. This helper isolates the screen's own content rows."""
    return rows[:-1] if rows else rows


def test_16_no_duplicate_callback_data_on_touched_screens() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_reports_ns(db_path)
        for screen_name, screen_fn in (("Reports", ns["_nm_reports_screen"]), ("NMS-root", ns["_nms_sources_screen"])):
            _text, rows = screen_fn()
            datas = [d for d in _datas(_content_rows(rows)) if d != b"noop"]
            dupes = sorted({d for d in datas if datas.count(d) > 1})
            check(f"16. {screen_name}: no duplicate callback_data (content rows, footer excluded)", not dupes, dupes)
    finally:
        os.unlink(db_path)
    ns2 = build_proxy_ns("")
    _text2, proxy_rows = ns2["_nm_proxy_screen"]()
    datas2 = [d for d in _datas(_content_rows(proxy_rows)) if d != b"noop"]
    dupes2 = sorted({d for d in datas2 if datas2.count(d) > 1})
    check("16. Proxy: no duplicate callback_data (content rows, footer excluded)", not dupes2, dupes2)


def test_17_handler_prev_inventory_sanity() -> None:
    for marker in (
        "def _nm_reports_screen", "def _nm_proxy_screen", "def _nms_sources_screen",
        "def _nm_llm_toggle_confirm_text", "def _nm_llm_toggle_confirm_buttons",
    ):
        check(f"17. N4 marker present: {marker!r}", marker in PANEL_SRC, marker)
    client_on_count = PANEL_SRC.count("@client.on(")
    check("17. @client.on(...) registration count is a healthy positive number (sanity, N4 adds none)",
          client_on_count > 50, client_on_count)


# ======================================================================
# 18. Exact raw button counts (N4.1 fix W2) -- a stray extra button that
# isn't a duplicate and isn't one of the pinned labels would pass every
# other check in this file; these exact counts close that gap.
# ======================================================================

def test_18_exact_raw_button_counts() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_reports_ns(db_path)

        # A. _nm_reports_screen -- deterministic (no DB-dependent content).
        # Rows: [Статистика, По менеджерам] + [Детально сегодня] +
        # [Дубликаты, Экспорт] + [Качество, Скрины по датам (N5.3 Part C)]
        # = 2+1+2+2 = 7 content buttons, + standard 1-row footer
        # [Назад, Главная] = 2 -> 9 total.
        _text, reports_rows = ns["_nm_reports_screen"]()
        check("18A. Reports: exact total raw button count == 9 (2+1+2+2 content + 2 footer)",
              len(_flat(reports_rows)) == 9, (len(_flat(reports_rows)), _labels(reports_rows)))

        # B. _nms_sources_screen -- fixed seed: zero traffic_sources rows
        # (temp DB has no traffic_sources table at all -- _nms_sources()
        # fails open to []), so the source-first loop renders nothing real
        # and the deterministic "Источники не найдены" placeholder fires
        # instead. Rows: [Источники не найдены] (nms:noop placeholder,
        # stands in for the empty source-first list) + [По менеджерам (по
        # окну источника)] (manager-window subflow) + standard 1-row footer
        # [Назад, Главная] = 1+1+2 = 4 total.
        _text2, nms_rows = ns["_nms_sources_screen"]()
        check("18B. NMS-root (0 sources): exact total raw button count == 4 (1 placeholder + 1 manager-window + 2 footer)",
              len(_flat(nms_rows)) == 4, (len(_flat(nms_rows)), _labels(nms_rows)))
    finally:
        os.unlink(db_path)

    # C. _nm_proxy_screen -- deterministic (no DB-dependent content).
    # Rows: [Прокси менеджеров, Добавить прокси] + [Пул прокси, Продление и
    # баланс] = 2+2 = 4 content buttons, + standard 1-row footer
    # [Назад, Главная] = 2 -> 6 total.
    ns2 = build_proxy_ns("")
    _text3, proxy_rows = ns2["_nm_proxy_screen"]()
    check("18C. Proxy: exact total raw button count == 6 (2+2 content + 2 footer)",
          len(_flat(proxy_rows)) == 6, (len(_flat(proxy_rows)), _labels(proxy_rows)))


def main() -> int:
    test_1_reports_has_exactly_one_primary_statistics_entry()
    test_2_manager_first_reachable_as_po_menedzheram()
    test_3_old_stats_callbacks_remain_handled()
    test_4_no_ambiguous_duplicate_stats_labels()
    test_5_nms_manager_first_subflow_still_present()
    test_6_automation_screen_has_exactly_one_llm_action()
    test_7_no_active_canonical_screen_emits_unsafe_llm_flip()
    test_8_historical_llm_toggle_redirects_to_canonical_confirm()
    test_9_bare_nm_llm_toggle_do_and_old_route_handler_preserved()
    test_10_proxy_screen_emits_prn_root()
    test_11_no_active_canonical_menu_emits_renew_or_prenew()
    test_12_notification_deeplink_handlers_for_renew_prenew_present()
    test_13_prn_subroutes_present()
    test_14_no_redirect_loop()
    test_15_callback_length()
    test_16_no_duplicate_callback_data_on_touched_screens()
    test_17_handler_prev_inventory_sanity()
    test_18_exact_raw_button_counts()

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
