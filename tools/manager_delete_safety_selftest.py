# -*- coding: utf-8 -*-
"""tools/manager_delete_safety_selftest.py -- offline self-test for the N5.2
Part A dedicated manager-deletion selection screen in panel_bot.py
(2026-07-21): the canonical root's "🗑 Удалить менеджера" button no longer
opens the generic manager-settings list (menu:manager_settings) -- it now
opens a thin, purpose-built selector (menu:nm_manager_delete ->
_nm_manager_delete_screen) whose ONLY job is to let the operator pick a
manager and land DIRECTLY on the existing, completely unmodified canonical
danger zone (menu:manager_danger:{key} -> _manager_danger_zone_text/_buttons,
still gated behind the unchanged guard:db_clear/reset/delete/restore_db
password+backup wizard).

This file exists because deletion-adjacent UI deserves its own dedicated,
maximally scoped proof, separate from menu_parity_selftest.py's broader
capability matrix (per the N5.2 spec's allowance for "one new focused
selftest ... if that produces stronger independent coverage"). Every check
below is either:
  - a real EXECUTION of _nm_manager_delete_screen() against a temp SQLite
    database (never db/data_tpilot.db), inspecting the actual rendered
    buttons, or
  - a scoped, quote-tolerant source check against the LAST (active,
    last-wins) body of a specific named function/generation -- never a
    bare whole-file substring search.

Product principles verified (owner's explicit list, N5.2):
  - no new deletion business logic is introduced by this screen;
  - the root button and the selection screen never emit a destructive
    command (/manager_delete_full, /manager_remove, /manager_reset,
    /manager_db_clear, /manager_db_restore) or a guard: callback directly;
  - nothing is written to the DB by this screen;
  - the password/backup guard flow (_manager_danger_zone_buttons) is
    byte-identical to its pre-N5.2 state.

Techniques: panel_bot.py cannot be imported standalone (Telethon/env side
effects at import time) -- functions under test are extracted via
ast.parse + ast.unparse + exec() and run for real against a temp SQLite
file, same idiom used throughout this project's tools/*_selftest.py files
(mirrors tools/manager_card_unified_selftest.py's build_card_ns).

    python tools\\manager_delete_safety_selftest.py
"""
from __future__ import annotations

import ast
import glob
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

# Pre-N5.2 backup (mandatory backup for this phase): used only to prove the
# danger-zone guard chain is byte-identical to its state before this screen
# was added -- i.e. Part A touched navigation only, never the guard logic.
_P52_BACKUP_CANDIDATES = sorted(glob.glob(str(BASE_DIR / "panel_bot.py.bak_nm2_p52_*")))
PRE_N52_BACKUP_SRC = (
    open(_P52_BACKUP_CANDIDATES[0], encoding="utf-8-sig").read() if _P52_BACKUP_CANDIDATES else None
)


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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="manager_delete_safety_selftest_")
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


def _contains_quote_tolerant(src_txt: str, marker: str) -> bool:
    if marker in src_txt:
        return True
    if '"' in marker:
        return marker.replace('"', "'") in src_txt
    if "'" in marker:
        return marker.replace("'", '"') in src_txt
    return False


def _active_block(source: str, def_name: str) -> str:
    """Text of the LAST (active, last-wins) top-level def `def_name` in
    `source`, up to the next top-level 'def ' -- same scoping technique
    established in N3.1/N3.2/N4/N5/N5.1's selftests."""
    start = source.rfind(f"def {def_name}")
    end = source.find("\ndef ", start + 10) if start != -1 else -1
    return source[start:end] if start != -1 and end != -1 else ""


DELETE_SCREEN_NAMES = {
    "_nm_manager_delete_screen", "_nm_mgr_decision_screen", "_nm_btn", "_nm_row",
    "_manager_settings_state",
}
ROWLOOKUP_NAMES = {"_manager_rows", "_manager_rows_all", "_manager_row_by_key"}
# The dedicated screen must NOT need any of the danger-zone functions itself
# (it only emits their route as a string) -- extracted separately below only
# to prove the guard chain those routes land on is unmodified.
DANGER_ZONE_NAMES = {"_manager_danger_zone_text", "_manager_danger_zone_buttons", "_mcard_btn"}


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


def build_delete_screen_ns(db_path: str) -> dict:
    import manager_registry

    nodes = _extract_by_name(DELETE_SCREEN_NAMES) + _extract_by_name(ROWLOOKUP_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        "_panel_header": lambda: "HEADER",
    }
    exec(compile(module_src, f"<{PANEL_PATH}:manager_delete>", "exec"), ns)
    return ns


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
    return [b[1] for b in _flat(rows)]


def _datas(rows) -> list:
    return [b[2] for b in _flat(rows)]


_DESTRUCTIVE_MARKERS = (
    b"cmd:/manager_delete_full", b"cmd:/manager_remove", b"cmd:/manager_reset",
    b"cmd:/manager_db_clear", b"cmd:/manager_db_restore",
)


# ======================================================================
# 1. Root button opens ONLY the selection screen; no destructive command
#    on the root callback itself (Part B items 1-2).
# ======================================================================

ROOT_SCREEN_NAMES = {"_nm_root_screen", "_nm_btn", "_nm_row"}


def build_root_screen_ns() -> dict:
    nodes = _extract_by_name(ROOT_SCREEN_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {"Button": _FakeButton, "Tuple": tuple, "List": list, "_panel_header": lambda: "HEADER"}
    exec(compile(module_src, f"<{PANEL_PATH}:root_screen>", "exec"), ns)
    return ns


def test_1_root_button_opens_only_selection_screen() -> None:
    # N5.3 (Part E): the root button was professionally renamed
    # «⏸ Остановка и удаление» (both meanings preserved); route unchanged.
    root_block = _active_block(PANEL_SRC, "_nm_root_screen")
    check("1. Root's Остановка и удаление targets the dedicated screen (full-line construction, "
          "not a bare fragment)",
          _contains_quote_tolerant(root_block, '_nm_btn("⏸ Остановка и удаление", "menu:nm_manager_delete")'),
          root_block[:400])
    check("1. Root no longer targets the generic manager-settings list for delete",
          '"menu:manager_settings")' not in root_block, root_block[:400])

    # Real execution, not a string search: find the actual button object for
    # this label and inspect its raw callback_data -- proves the root
    # callback itself is a plain navigation hop, never a destructive command
    # or a guard: callback.
    ns = build_root_screen_ns()
    _, root_rows = ns["_nm_root_screen"]()
    delete_btn = _find(root_rows, label="⏸ Остановка и удаление")
    check("2. The root's stop/delete button exists and carries a plain menu: route "
          "(never a destructive command or guard:)",
          delete_btn is not None and delete_btn[2] == b"menu:nm_manager_delete", delete_btn)


# ======================================================================
# 2. The selection screen itself: real execution, inspect actual buttons
#    (Part B items 3, 4, 8, 9, 10).
# ======================================================================

def test_2_selection_screen_execution() -> None:
    db_path = _make_temp_db()
    try:
        _seed_managers(db_path, [
            {"manager_key": "mgr_one", "display_name": "Manager One", "status": "active", "is_enabled": 1, "manual_stopped": 0},
            {"manager_key": "mgr_two", "display_name": "Manager Two", "status": "active", "is_enabled": 0, "manual_stopped": 1},
            {"manager_key": "mgr_arch", "display_name": "Archived One", "status": "archived", "is_enabled": 1, "manual_stopped": 0},
        ])
        ns = build_delete_screen_ns(db_path)
        text, rows = ns["_nm_manager_delete_screen"]()
        labels = _labels(rows)
        datas = _datas(rows)

        check("3. Selection screen never emits a destructive command or a guard: callback directly",
              not any(d.startswith(m) for d in datas for m in _DESTRUCTIVE_MARKERS) and not any(d.startswith(b"guard:") for d in datas),
              datas)

        # N5.3 (Part E): manager rows now open the thin two-option DECISION
        # screen (menu:nm_mgr_decision:{key}), not the danger zone directly.
        check("4. mgr_one (active) has a button landing on the decision screen (menu:nm_mgr_decision:mgr_one)",
              _find(rows, data=b"menu:nm_mgr_decision:mgr_one") is not None, rows)
        check("4. mgr_two (stopped) has a button landing on the decision screen (menu:nm_mgr_decision:mgr_two)",
              _find(rows, data=b"menu:nm_mgr_decision:mgr_two") is not None, rows)
        check("4. Archived manager is excluded from the selection list (same rule as _manager_settings_buttons)",
              not any(b"mgr_arch" in d for d in datas), rows)
        check("4b. Selection screen no longer links straight into the danger zone (N5.3)",
              not any(d.startswith(b"menu:manager_danger:") for d in datas), datas)

        # The footer's Back and Home intentionally share the same target
        # (menu:main, per owner spec) -- excluded from dup-detection, same
        # documented exception already used for other top-level NM screens
        # (see N4 review note W4 / nm_menu_distribution_selftest.py). Only
        # the per-manager decision routes must be unique.
        manager_datas = [d for d in datas if d.startswith(b"menu:nm_mgr_decision:")]
        check("10. No duplicate callback_data among the per-manager decision buttons",
              len(manager_datas) == len(set(manager_datas)), manager_datas)

        check("navigation: Back returns to menu:main", _find(rows, label="⬅️ Назад", data=b"menu:main") is not None, rows)
        check("navigation: Home returns to menu:main", _find(rows, label="🏠 Главная", data=b"menu:main") is not None, rows)

        for b in _flat(rows):
            check(f"9. callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))

        check("text explains nothing happens immediately (stop-or-delete choice next)",
              "ОСТАНОВКА И УДАЛЕНИЕ" in text and "сразу" in text, text[:200])
    finally:
        os.unlink(db_path)


# ======================================================================
# 2b. N5.3 (Part E): the DECISION screen itself -- real execution. Exactly
#     two direction buttons (stop OR start, + full deletion) plus Back/Home;
#     the deletion direction is the EXISTING guard:delete: protected entry
#     (password+backup flow); no other danger-zone action is offered here.
# ======================================================================

def test_2b_decision_screen_execution() -> None:
    db_path = _make_temp_db()
    try:
        _seed_managers(db_path, [
            {"manager_key": "mgr_on", "display_name": "Running Mgr", "status": "active", "is_enabled": 1, "manual_stopped": 0},
            {"manager_key": "mgr_off", "display_name": "Stopped Mgr", "status": "active", "is_enabled": 0, "manual_stopped": 1},
        ])
        ns = build_delete_screen_ns(db_path)

        # Running manager: safe stop (confirm-gated existing flow) + deletion.
        text_on, rows_on = ns["_nm_mgr_decision_screen"]("mgr_on")
        datas_on = _datas(rows_on)
        check("E1. decision(on): offers the confirm-gated STOP (menu:manager_disable_confirm:), never a bare stop command",
              _find(rows_on, label="⏸ Остановить менеджера", data=b"menu:manager_disable_confirm:mgr_on") is not None, rows_on)
        check("E2. decision(on): offers full deletion via the EXISTING protected guard entry (guard:delete:)",
              _find(rows_on, label="🗑 Полное удаление", data=b"guard:delete:mgr_on") is not None, rows_on)
        check("E3. decision(on): exactly 4 buttons total (stop + delete + Back + Home) -- никакой danger-zone коллекции",
              len(_flat(rows_on)) == 4, _flat(rows_on))
        check("E4. decision(on): no db_clear/reset/restore guard actions offered here",
              not any(d.startswith((b"guard:db_clear", b"guard:reset", b"guard:restore_db")) for d in datas_on), datas_on)
        check("E5. decision(on): never emits a direct destructive command",
              not any(d.startswith(m) for d in datas_on for m in _DESTRUCTIVE_MARKERS), datas_on)
        check("E6. decision(on): Back -> selection screen, Home -> menu:main",
              _find(rows_on, label="⬅️ Назад", data=b"menu:nm_manager_delete") is not None
              and _find(rows_on, label="🏠 Главная", data=b"menu:main") is not None, rows_on)

        # Stopped manager: start action replaces stop; deletion unchanged.
        text_off, rows_off = ns["_nm_mgr_decision_screen"]("mgr_off")
        check("E7. decision(off): offers START via cmd:/manager_start (same as the unified card; NOT /manager_enable)",
              _find(rows_off, label="▶️ Включить менеджера", data=b"cmd:/manager_start mgr_off") is not None, rows_off)
        check("E8. decision(off): deletion entry unchanged (guard:delete:)",
              _find(rows_off, data=b"guard:delete:mgr_off") is not None, rows_off)

        # Missing manager: safe fallback with navigation only.
        text_gone, rows_gone = ns["_nm_mgr_decision_screen"]("no_such_mgr")
        check("E9. decision(missing): no action buttons, only Back/Home",
              len(_flat(rows_gone)) == 2 and _find(rows_gone, data=b"menu:nm_manager_delete") is not None, rows_gone)

        for b in _flat(rows_on) + _flat(rows_off) + _flat(rows_gone):
            check(f"E10. decision callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
    finally:
        os.unlink(db_path)


def test_3_empty_state_is_safe() -> None:
    # Part B item 8: empty state renders a clear message, no per-manager
    # buttons, Back/Home still present.
    db_path = _make_temp_db()
    try:
        ns = build_delete_screen_ns(db_path)
        text, rows = ns["_nm_manager_delete_screen"]()
        flat = _flat(rows)
        check("8. Empty state: text clearly says no managers were found", "не найдены" in text, text)
        check("8. Empty state: exactly the Back+Home footer row, zero manager buttons", len(flat) == 2, flat)
        check("8. Empty state: Back is still present", _find(rows, label="⬅️ Назад", data=b"menu:main") is not None, rows)
        check("8. Empty state: Home is still present", _find(rows, label="🏠 Главная", data=b"menu:main") is not None, rows)
    finally:
        os.unlink(db_path)


def test_4_long_key_callback_guard() -> None:
    # Part B item 9 (dedicated case): a pathologically long manager_key must
    # not blow the 64-byte Telegram callback_data limit -- _nm_btn's guard
    # degrades it to a safe non-destructive fallback (menu:nm_root) rather
    # than crashing or silently truncating into a colliding route.
    db_path = _make_temp_db()
    try:
        long_key = "m" * 60
        _seed_managers(db_path, [
            {"manager_key": long_key, "display_name": "Long Key Manager", "status": "active", "is_enabled": 1, "manual_stopped": 0},
        ])
        ns = build_delete_screen_ns(db_path)
        text, rows = ns["_nm_manager_delete_screen"]()
        for b in _flat(rows):
            check(f"9b. long-key callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))
        oversize_route = f"menu:nm_mgr_decision:{long_key}".encode("utf-8")
        check("9b. the oversize route itself never actually appears as callback_data (guard degraded it)",
              not any(d == oversize_route for d in _datas(rows)), _datas(rows))
    finally:
        os.unlink(db_path)


# ======================================================================
# 5. No new deletion business logic; danger-zone guard chain untouched
#    (Part B items 6, 7).
# ======================================================================

def test_5_no_new_deletion_logic_and_guard_chain_untouched() -> None:
    # Scoped to the function's CODE only (comments legitimately document the
    # unchanged guard chain by name -- "guard:db_clear" etc. -- so a bare
    # whole-block substring check on "guard:" would false-fail on its own
    # docstring/comment; the AST body strips comments, giving a precise
    # check of what the function actually EMITS).
    tree = ast.parse(PANEL_SRC)
    fn_node = None
    for n in tree.body:
        if getattr(n, "name", None) == "_nm_manager_delete_screen":
            fn_node = n  # last one wins, same as runtime
    if fn_node is None:
        raise AssertionError("could not locate _nm_manager_delete_screen in panel_bot.py")
    screen_code_only = ast.unparse(fn_node)
    check("6/7. Selection screen's CODE (comments excluded) never references _submit_and_wait, "
          "guard:, or any destructive command literal",
          "_submit_and_wait" not in screen_code_only
          and "guard:" not in screen_code_only
          and not any(m.decode() in screen_code_only for m in _DESTRUCTIVE_MARKERS),
          screen_code_only)
    screen_block = _active_block(PANEL_SRC, "_nm_manager_delete_screen")
    check("7. Selection screen defines no SQL write (no INSERT/UPDATE/DELETE/execute( in its own body)",
          not any(tok in screen_block for tok in ("INSERT INTO", "UPDATE ", "DELETE FROM", ".execute(", ".commit(")),
          screen_block)

    # N5.3: the decision screen's own CODE must also stay a pure navigator --
    # its ONLY guard-flavored emission is the single existing protected entry
    # guard:delete:{key} (Part E), never a direct destructive command, never
    # _submit_and_wait, never SQL writes.
    dec_node = None
    for n in tree.body:
        if getattr(n, "name", None) == "_nm_mgr_decision_screen":
            dec_node = n
    if dec_node is None:
        raise AssertionError("could not locate _nm_mgr_decision_screen in panel_bot.py")
    dec_code = ast.unparse(dec_node)
    check("N5.3-E. Decision screen's CODE never references _submit_and_wait or a destructive command literal",
          "_submit_and_wait" not in dec_code
          and not any(m.decode() in dec_code for m in _DESTRUCTIVE_MARKERS),
          dec_code)
    check("N5.3-E. Decision screen's only guard emission is the protected guard:delete: entry",
          "guard:delete:" in dec_code
          and "guard:db_clear" not in dec_code
          and "guard:reset" not in dec_code
          and "guard:restore_db" not in dec_code,
          dec_code)
    check("N5.3-E. Decision screen defines no SQL write",
          not any(tok in dec_code for tok in ("INSERT INTO", "UPDATE ", "DELETE FROM", ".execute(", ".commit(")),
          dec_code)

    if PRE_N52_BACKUP_SRC is not None:
        danger_now = "\n\n".join(_active_block(PANEL_SRC, n) for n in ("_manager_danger_zone_text", "_manager_danger_zone_buttons"))
        danger_prior = "\n\n".join(_active_block(PRE_N52_BACKUP_SRC, n) for n in ("_manager_danger_zone_text", "_manager_danger_zone_buttons"))
        check("5. _manager_danger_zone_text/_buttons are BYTE-IDENTICAL to their pre-N5.2 state "
              "(Part A only changed navigation into this screen, never the guard logic itself)",
              danger_now == danger_prior and bool(danger_now), None)
    else:
        # GIT MIGRATION 20260815: the pre-N5.2 backup was a one-time artifact
        # of the pre-git file-backup workflow and never entered version
        # control. When absent (any machine other than the original dev PC),
        # the byte-identity comparison is SKIPPED -- git history is now the
        # authoritative record. When present, it still runs unchanged.
        print("[SKIP] 5. pre-N5.2 backup not present (pre-git artifact) -- "
              "byte-identity check skipped; git history is authoritative")

    danger_buttons_block = _active_block(PANEL_SRC, "_manager_danger_zone_buttons")
    for marker in ("guard:db_clear:", "guard:reset:", "guard:delete:", "guard:restore_db:"):
        check(f"danger zone still emits {marker!r} (unchanged password+backup guard flow)",
              marker in danger_buttons_block, danger_buttons_block[:200])


def main() -> int:
    test_1_root_button_opens_only_selection_screen()
    test_2_selection_screen_execution()
    test_2b_decision_screen_execution()
    test_3_empty_state_is_safe()
    test_4_long_key_callback_guard()
    test_5_no_new_deletion_logic_and_guard_chain_untouched()

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
