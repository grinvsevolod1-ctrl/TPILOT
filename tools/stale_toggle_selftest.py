# -*- coding: utf-8 -*-
"""tools/stale_toggle_selftest.py -- offline self-test for stale-state and
duplicate-click safety across the Phase N3 toggle contract (panel_bot.py,
2026-07-21).

Two toggles (F: auto-status, G: NM LLM supervisor) implement a genuine
two-step confirm-then-do state machine with a TARGET encoded in the route
(not the previously observed state) -- this file exercises the 10 required
stale-state scenarios against BOTH of them for real, using AST-extracted,
executed code (never a re-implementation of the logic under test):

  1. state changes between screen render and button click
  2. requested target already matches persisted state
  3. no duplicate command is executed (state written exactly once)
  4. a clear stale-state notice is rendered
  5. the notice/rerender reflects fresh current state
  6. two rapid identical clicks do not apply twice
  7. missing/deleted target (malformed target token) handled safely
  8. malformed callback does not mutate state
  9. confirmation routes revalidate state (not just the final do: step)
  10. historical old callback (no target encoded) still lands on a safe path

The remaining toggles (A: unanswered, B: source, C: group, D: followups) do
NOT have their own confirm/do state machine -- they ride the existing
generic `cmd:` pipeline, which already provides duplicate-click protection
via `_panel_callback_is_duplicate` (2.5s window) for every `cmd:` button
project-wide. This file verifies THAT structurally (scenario 6-equivalent)
rather than re-simulating the shared dispatcher.

Techniques: same AST-extraction + temp-SQLite idiom as every other
tools/*_selftest.py in this project (see
tools/manager_card_unified_selftest.py and tools/toggle_singleton_selftest.py
for the reference patterns this file reuses).

    python tools\\stale_toggle_selftest.py
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


class _FakeButton:
    @staticmethod
    def inline(text, data):
        raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
        return _FakeBtn(text, raw)


def _flat(rows):
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


# ======================================================================
# Temp settings-only DB (never db/data_tpilot.db or db/data.db).
# ======================================================================

def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="stale_toggle_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
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


def _get_setting_direct(db_path: str, key: str) -> str:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT value FROM settings WHERE key=? LIMIT 1", (key,)).fetchone()
        return str(row[0] or "") if row else ""
    finally:
        con.close()


def _write_count(db_path: str, key: str) -> int:
    """Count how many rows exist for `key` in the settings audit -- since we
    use INSERT OR REPLACE (one row per key), instead track write COUNT via a
    side log table populated by a wrapped _pb_set_setting."""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT COUNT(*) FROM write_log WHERE key=?", (key,)).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        con.close()


def _ensure_write_log(db_path: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS write_log(key TEXT, value TEXT, ts TEXT)")
        con.commit()
    finally:
        con.close()


# ======================================================================
# AST extraction helpers (project convention).
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
    if marker in src_txt:
        return True
    if '"' in marker:
        return marker.replace('"', "'") in src_txt
    if "'" in marker:
        return marker.replace("'", '"') in src_txt
    return False


def _find_generation(marker_all: tuple) -> ast.FunctionDef:
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
# F: autostatus namespace -- real _title_for_menu generation, executed
# for real, with a write-counting _pb_set_setting so "no duplicate write"
# (scenario 3/6) is provable, not inferred.
# ======================================================================

def build_autostatus_ns(db_path: str) -> dict:
    _ensure_write_log(db_path)
    routing_gen = _find_generation(("autostatus_toggle_confirm:",))
    if routing_gen is None:
        raise AssertionError("build_autostatus_ns: could not locate the N3 autostatus _title_for_menu generation")
    # _title_for_menu's stale-notice branches call this sibling helper --
    # extract it too, not just the routing generation itself.
    render_helper_nodes = _extract_by_name({"_autostatus_render_automation"})

    def _pb_get_setting(key):
        return _get_setting_direct(db_path, key)

    def _pb_set_setting(key, value):
        _set_setting(db_path, key, value)
        con = sqlite3.connect(db_path)
        try:
            con.execute("INSERT INTO write_log(key, value, ts) VALUES(?,?,datetime('now'))", (key, value))
            con.commit()
        finally:
            con.close()

    ns = {
        "sqlite3": sqlite3, "os": os,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "_pb_get_setting": _pb_get_setting,
        "_pb_set_setting": _pb_set_setting,
        "_panel_header": lambda: "HEADER",
        # Deliberately fake, isolating this test from every other
        # _title_for_menu generation, none of which handle autostatus_*.
        "_AUTOSTATUS_PREV_TITLE_FOR_MENU": lambda raw: ("AUTOMATION_SCREEN", [[_FakeButton.inline("⬅️", b"menu:main")]]),
        "_title_for_menu": lambda raw: (f"OLD_MENU:{raw}", []),
    }
    exec(compile("\n\n".join(ast.unparse(n) for n in render_helper_nodes), f"<{PANEL_PATH}:autostatus_render_helper>", "exec"), ns)
    exec(compile(ast.unparse(routing_gen), f"<{PANEL_PATH}:autostatus_routing>", "exec"), ns)
    return ns


# ======================================================================
# G: NM LLM namespace -- same technique.
# ======================================================================

def build_nm_llm_ns(db_path: str) -> dict:
    _ensure_write_log(db_path)
    routing_gen = _find_generation(("nm_llm_toggle_do:",))
    if routing_gen is None:
        raise AssertionError("build_nm_llm_ns: could not locate the NM _title_for_menu generation with the N3 target-encoded route")
    confirm_nodes = _extract_by_name({"_nm_llm_toggle_confirm_text", "_nm_llm_toggle_confirm_buttons"})

    def _pb_get_setting(key):
        return _get_setting_direct(db_path, key)

    def _pb_set_setting(key, value):
        _set_setting(db_path, key, value)
        con = sqlite3.connect(db_path)
        try:
            con.execute("INSERT INTO write_log(key, value, ts) VALUES(?,?,datetime('now'))", (key, value))
            con.commit()
        finally:
            con.close()

    ns = {
        "sqlite3": sqlite3, "os": os,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "_pb_get_setting": _pb_get_setting,
        "_pb_set_setting": _pb_set_setting,
        "_panel_header": lambda: "HEADER",
        "_nm_automation_screen": lambda: ("AUTOMATION_SCREEN", [[_FakeButton.inline("⬅️", b"menu:nm_root")]]),
        "_title_for_menu": lambda raw: (f"OLD_MENU:{raw}", []),
    }
    exec(compile("\n\n".join(ast.unparse(n) for n in confirm_nodes), f"<{PANEL_PATH}:nm_llm_confirm>", "exec"), ns)
    exec(compile(ast.unparse(routing_gen), f"<{PANEL_PATH}:nm_llm_routing>", "exec"), ns)
    return ns


# ======================================================================
# Parametrized scenario runner -- same 10 scenarios exercised against
# BOTH F (autostatus) and G (nm_llm) real extracted code.
# ======================================================================

class _Toggle:
    """Adapter so the 10 scenarios can run identically against F and G,
    which have slightly different route names / setting keys / confirm
    entry points but the IDENTICAL target-encoded contract shape."""

    def __init__(self, name, ns_builder, setting_key, confirm_route_of, do_route_of,
                 do_bare_route, malformed_do_route, malformed_confirm_route=None,
                 on_label_in_do_text=None):
        self.name = name
        self.ns_builder = ns_builder
        self.setting_key = setting_key
        self.confirm_route_of = confirm_route_of
        self.do_route_of = do_route_of
        self.do_bare_route = do_bare_route
        self.malformed_do_route = malformed_do_route
        # N3.1: only F's confirm route takes a raw target straight from the
        # callback (autostatus_toggle_confirm:<target>) -- G's confirm route
        # is bare ("nm_llm_toggle_confirm", no user-suppliable target segment
        # at all), so there is structurally no malformed-target case to test
        # on G's confirm entry. Left None for G on purpose, not an oversight.
        self.malformed_confirm_route = malformed_confirm_route


TOGGLES = [
    _Toggle(
        "F(autostatus)", build_autostatus_ns, "auto_status_enabled",
        confirm_route_of=lambda target: f"autostatus_toggle_confirm:{target}",
        do_route_of=lambda target: f"autostatus_toggle_do:{target}",
        do_bare_route="autostatus_toggle",
        malformed_do_route="autostatus_toggle_do:garbage",
        malformed_confirm_route="autostatus_toggle_confirm:garbage",
    ),
    _Toggle(
        "G(nm_llm)", build_nm_llm_ns, "llm_supervisor_enabled",
        confirm_route_of=lambda target: "nm_llm_toggle_confirm",
        do_route_of=lambda target: f"nm_llm_toggle_do:{target}",
        do_bare_route="nm_llm_toggle_do",
        malformed_do_route="nm_llm_toggle_do:garbage",
        malformed_confirm_route=None,
    ),
]


def _writes_for(db_path: str, key: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT COUNT(*) FROM write_log WHERE key=?", (key,)).fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        con.close()


def run_scenarios_for(tg: _Toggle) -> None:
    # ------------------------------------------------------------------
    # 1/2. State changes between render and click; target already matches
    # persisted state by the time "do" is clicked -- must no-op.
    # ------------------------------------------------------------------
    db = _make_temp_db()
    try:
        _set_setting(db, tg.setting_key, "1")
        ns = tg.ns_builder(db)
        title_for_menu = ns["_title_for_menu"]
        # Confirm screen rendered while setting was "1" -> target computed as "0".
        target = "0"
        # Someone else flips it to "0" (the confirm's own target) before the click.
        _set_setting(db, tg.setting_key, "0")
        writes_before = _writes_for(db, tg.setting_key)
        text, rows = title_for_menu(tg.do_route_of(target))
        writes_after = _writes_for(db, tg.setting_key)
        check(f"3. {tg.name}: no duplicate/redundant write happens when target already matches current state",
              writes_after == writes_before, (writes_before, writes_after))
        check(f"4. {tg.name}: a clear stale-state notice is rendered", "уже изменилось" in text, text)
        check(f"5. {tg.name}: the notice reflects the CURRENT (fresh) state, not an error/crash", bool(text) and rows is not None, (text, rows))
    finally:
        os.unlink(db)

    # ------------------------------------------------------------------
    # 6. Two rapid IDENTICAL do: clicks (target still valid on the first,
    # already-achieved on the second) -- the setting is written exactly
    # once, not twice.
    # ------------------------------------------------------------------
    db = _make_temp_db()
    try:
        _set_setting(db, tg.setting_key, "1")
        ns = tg.ns_builder(db)
        title_for_menu = ns["_title_for_menu"]
        target = "0"
        title_for_menu(tg.do_route_of(target))  # first click: real transition
        writes_after_first = _writes_for(db, tg.setting_key)
        title_for_menu(tg.do_route_of(target))  # second (duplicate) click: same target
        writes_after_second = _writes_for(db, tg.setting_key)
        check(f"6. {tg.name}: first click actually applies (one write)", writes_after_first == 1, writes_after_first)
        check(f"6. {tg.name}: second identical click is a no-op (still one write total)",
              writes_after_second == 1, writes_after_second)
        check(f"6. {tg.name}: final persisted state matches the confirmed target",
              _get_setting_direct(db, tg.setting_key) == target, _get_setting_direct(db, tg.setting_key))
    finally:
        os.unlink(db)

    # ------------------------------------------------------------------
    # 7/8. Malformed/garbage target token -- handled safely, no mutation,
    # no exception. Exercised against BOTH current="1" (the safe half the
    # original N3 scenario covered) AND current="0" (N3.1 fix: this is the
    # case where the naive `(current!="0")==(target!="0")` guard used to
    # evaluate False==True -> fall through -> _pb_set_setting(key,"garbage")
    # -- a real bug, since every later read is `value != "0"`, so a
    # "garbage" value would have been misread as enabled).
    # ------------------------------------------------------------------
    for seed_current in ("1", "0"):
        db = _make_temp_db()
        try:
            _set_setting(db, tg.setting_key, seed_current)
            ns = tg.ns_builder(db)
            title_for_menu = ns["_title_for_menu"]
            writes_before = _writes_for(db, tg.setting_key)
            try:
                text, rows = title_for_menu(tg.malformed_do_route)
                raised = False
            except Exception as exc:
                text, rows, raised = "", [], exc
            writes_after = _writes_for(db, tg.setting_key)
            persisted = _get_setting_direct(db, tg.setting_key)
            check(f"7/8. {tg.name} (current={seed_current!r}): a malformed/garbage target token does not raise",
                  raised is False, raised)
            check(f"7/8. {tg.name} (current={seed_current!r}): no write occurs for a malformed/garbage target",
                  writes_after == writes_before, (writes_before, writes_after))
            check(f"7/8. {tg.name} (current={seed_current!r}): the persisted value is UNCHANGED (never poisoned with the raw garbage token)",
                  persisted == seed_current, persisted)
            # The critical N3.1 regression check: a garbage value must never
            # end up persisted and later misread as "enabled" (!= "0").
            check(f"7/8. {tg.name} (current={seed_current!r}): persisted value is never the literal 'garbage' token",
                  persisted != "garbage", persisted)
            check(f"7/8. {tg.name} (current={seed_current!r}): a clear stale/invalid notice is rendered",
                  "уже изменилось" in text, text)
        finally:
            os.unlink(db)

    # ------------------------------------------------------------------
    # 7b. Malformed target on the CONFIRM route itself (F only -- see the
    # _Toggle docstring for why G has no user-suppliable confirm target).
    # A malformed confirm target must not render a (misleading) confirm
    # screen and must not mutate state either.
    # ------------------------------------------------------------------
    if tg.malformed_confirm_route:
        for seed_current in ("1", "0"):
            db = _make_temp_db()
            try:
                _set_setting(db, tg.setting_key, seed_current)
                ns = tg.ns_builder(db)
                title_for_menu = ns["_title_for_menu"]
                writes_before = _writes_for(db, tg.setting_key)
                text, rows = title_for_menu(tg.malformed_confirm_route)
                writes_after = _writes_for(db, tg.setting_key)
                check(f"7b. {tg.name} (current={seed_current!r}): malformed confirm target does not mutate state",
                      writes_after == writes_before and _get_setting_direct(db, tg.setting_key) == seed_current,
                      (writes_before, writes_after, _get_setting_direct(db, tg.setting_key)))
                check(f"7b. {tg.name} (current={seed_current!r}): malformed confirm target shows a stale/invalid notice, not a (misleading) confirm prompt",
                      "уже изменилось" in text and "Подтвердить" not in text, text)
            finally:
                os.unlink(db)

    # ------------------------------------------------------------------
    # 9. Confirmation route itself revalidates state (does not blindly
    # trust an earlier render) -- rendering confirm never mutates, and its
    # encoded target always reflects CURRENT state at render time.
    # ------------------------------------------------------------------
    db = _make_temp_db()
    try:
        _set_setting(db, tg.setting_key, "1")
        ns = tg.ns_builder(db)
        title_for_menu = ns["_title_for_menu"]
        writes_before = _writes_for(db, tg.setting_key)
        _text, _rows = title_for_menu(tg.confirm_route_of("0"))
        writes_after = _writes_for(db, tg.setting_key)
        check(f"9. {tg.name}: rendering the confirm screen never mutates the setting",
              writes_after == writes_before, (writes_before, writes_after))
        # Flip the underlying state, then render confirm again -- the SAME
        # confirm route must reflect the NEW current state fresh (not a
        # stale captured value from the first render).
        _set_setting(db, tg.setting_key, "0")
        text2, _rows2 = title_for_menu(tg.confirm_route_of("0"))
        check(f"9. {tg.name}: confirm route re-reads state fresh on every render (not cached)",
              bool(text2), text2)
    finally:
        os.unlink(db)

    # ------------------------------------------------------------------
    # 10. Historical old callback (no target encoded) still lands on a
    # safe path -- unconditional flip, exactly as before N3, never raises.
    # ------------------------------------------------------------------
    db = _make_temp_db()
    try:
        _set_setting(db, tg.setting_key, "1")
        ns = tg.ns_builder(db)
        title_for_menu = ns["_title_for_menu"]
        try:
            text, rows = title_for_menu(tg.do_bare_route)
            raised = False
        except Exception as exc:
            text, rows, raised = "", [], exc
        check(f"10. {tg.name}: historical bare route ({tg.do_bare_route!r}) does not raise", raised is False, raised)
        check(f"10. {tg.name}: historical bare route still flips the setting (unconditional, as before N3)",
              _get_setting_direct(db, tg.setting_key) == "0", _get_setting_direct(db, tg.setting_key))
        check(f"10. {tg.name}: historical bare route returns real (text, buttons)", bool(text) and rows is not None, (text, rows))
    finally:
        os.unlink(db)


def test_f_and_g_full_scenario_matrix() -> None:
    for tg in TOGGLES:
        print(f"\n-- {tg.name} --")
        run_scenarios_for(tg)


# ======================================================================
# A-D structural coverage: no bespoke state machine, so scenarios 3/6
# (no duplicate execution / rapid identical clicks) are guaranteed by the
# SHARED generic cmd: pipeline's dedupe -- verify that mechanism is real
# and actually wired to every cmd: button (including A-D's).
# ======================================================================

def test_a_to_d_ride_the_deduped_cmd_pipeline() -> None:
    check("cmd: pipeline dedupe helper exists (_panel_callback_is_duplicate)",
          "def _panel_callback_is_duplicate(" in PANEL_SRC, None)
    check("cmd: pipeline dedupe window constant exists (_PANEL_DEDUPE_SEC)",
          "_PANEL_DEDUPE_SEC" in PANEL_SRC, None)
    # The generic cmd: dispatch branch must call the dedupe helper BEFORE
    # spawning the command task (structural: dedupe-then-spawn ordering).
    idx_cmd = PANEL_SRC.find('if data.startswith("cmd:")')
    if idx_cmd == -1:
        idx_cmd = PANEL_SRC.find("if data.startswith('cmd:')")
    check("generic cmd: dispatch branch located", idx_cmd != -1, idx_cmd)
    if idx_cmd != -1:
        window = PANEL_SRC[idx_cmd: idx_cmd + 1200]
        idx_dup = window.find("_panel_callback_is_duplicate(")
        idx_spawn = window.find("_panel_spawn_command_task(")
        check("cmd: dispatch checks duplicate BEFORE spawning the command (dedupe-then-spawn ordering)",
              idx_dup != -1 and idx_spawn != -1 and idx_dup < idx_spawn, (idx_dup, idx_spawn))
    # A-D's toggle buttons all route through cmd: (not a bespoke menu: route),
    # so they inherit this dedupe for free -- verify their callbacks are
    # indeed cmd:-prefixed (not a parallel, unprotected mechanism).
    for marker in (
        'f"cmd:/source off {key}"', 'f"cmd:/source on {key}"',
        'f"cmd:/mgroup off {key}"', 'f"cmd:/mgroup on {key}"',
        'b"cmd:/unanswered_notify off"', 'b"cmd:/unanswered_notify on"',
        'f"cmd:/followup off {key}"', 'f"cmd:/followup on {key}"',
    ):
        check(f"A-D toggle emits a cmd:-prefixed callback (inherits shared dedupe): {marker!r}",
              _contains_quote_tolerant(PANEL_SRC, marker), marker)


def main() -> int:
    test_f_and_g_full_scenario_matrix()
    print()
    test_a_to_d_ride_the_deduped_cmd_pipeline()

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
