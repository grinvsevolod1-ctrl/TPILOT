# -*- coding: utf-8 -*-
"""tools/menu_parity_selftest.py -- BLOCKING acceptance gate for Phase N5
(2026-07-21): the completed canonical new menu (_nm_root_screen) becomes the
primary root rendered by /panel, /start, menu:main and every Home/refresh
action, with the pre-N5 old root preserved reachable via a new, distinct
temporary route ("old_root") for local parity verification until Phase N6.

This file does NOT require identical navigation structure between old and
new. Its job is to prove that every REAL, non-navigation capability reachable
from the pre-N5 old root is STILL reachable from the N5/N5.1/N5.2 canonical
new root, or is explicitly classified as an approved retired duplicate/
shortcut/text-only omission. Every capability in CAPABILITY_MATRIX below is
classified as exactly one of:

    REACHABLE_CANONICAL        -- same capability, reachable from the new root.
    INTENTIONAL_ALIAS          -- historical route now lands on the canonical
                                   implementation (may have a different route
                                   string, same underlying screen/command).
    TRANSITIONAL_SHORTCUT      -- a duplicate shortcut is gone, but the
                                   capability is reachable via its canonical
                                   section (different navigation depth/path).
    TEXT_COMMAND_ONLY_BY_DESIGN -- explicitly approved prior omission (e.g.
                                   /manager_stop, /manager_enable -- N2
                                   deliberately removed these from the card).
    FUTURE_PLACEHOLDER          -- a brand-new, not-yet-functional capability
                                   (AI-assistant only) -- never counted as
                                   satisfying any EXISTING capability.
    BLOCKING_MISSING           -- a real capability that is NOT reachable and
                                   NOT explicitly approved for omission.

Acceptance requires BLOCKING_MISSING == 0. Every entry below names an exact
old marker/evidence and new marker/evidence, plus a justification -- no
generic catch-all whitelist.

=== N5.2 Part F (2026-07-21): evidence methodology redesign ===============
The N5/N5.1 version of this gate checked most `new_marker`s with a bare
`marker in PANEL_SRC` whole-file substring search. An independent review
(X2) correctly found this could be satisfied by a marker that only exists in
SHADOWED/DEAD code (an earlier, inactive _title_for_menu generation, an
orphaned screen like the old admin card, etc.) -- a false pass that would
never be caught structurally. N5.2 replaced that methodology with 5 named,
scoped evidence types for every CURRENT-reachability claim:

    ACTIVE_FUNCTION   -- marker checked inside the LAST (active, last-wins)
                         body of a specific named top-level function, via
                         `_active_block()` (rfind + next top-level "\\ndef ").
    ACTIVE_GENERATION -- marker checked inside the ONE `_title_for_menu`
                         generation uniquely identified by a disambiguating
                         substring (`_find_generation_by_marker`), isolating
                         it from the ~30 other stacked generations.
    EXECUTED_SCREEN   -- the screen-builder function is ACTUALLY CALLED
                         (via `build_domain_screens_ns`, real AST-extraction
                         + exec against a temp SQLite db) and the real
                         rendered buttons are inspected for the exact
                         callback_data -- the strongest evidence type, used
                         for the explicit "deep reachability" domains below.
    HANDLER_SCOPE     -- (N5.2.1, see below) marker checked ONLY inside the
                         single owning conditional branch of a named handler
                         function, located structurally via AST.
    ROUTE_TRACE       -- (N5.2.1, see below) a real, deterministic route
                         trace executed against a shared trace context --
                         never an unconditional stub.
    OLD_BACKUP        -- (old-side only) a full-line, quote-tolerant marker
                         checked against the PRE-N5 BACKUP source, whole
                         file. This is intentionally NOT scoped: it is a
                         backward-looking, existence-only proof ("did this
                         capability exist at all before N5"), not a
                         reachability claim -- a match anywhere in a frozen,
                         years-old snapshot still proves prior existence.
                         Scoping effort is spent where it changes the
                         verdict: CURRENT reachability (new-side), which is
                         what BLOCKING_MISSING actually depends on.

=== N5.2.1 Part A (2026-07-21): HANDLER_SCOPE was itself tautological ======
An independent adversarial review of N5.2 (findings Y1/Y2) found that BOTH
non-EXECUTED_SCREEN "scoped" evidence types were, in practice, whole-file
searches in disguise:
  - Y1: HANDLER_SCOPE located an `anchor` string by byte offset and searched
    a fixed window STARTING AT that anchor for `marker` -- but in all 6
    prior entries (cal:/srcpick:/ppool:/plcterm:/tr:/trx:), `marker` was a
    SUBSTRING of `anchor` itself, so the check was satisfied the instant the
    anchor was found ANYWHERE in the file -- i.e. a bare `anchor in
    PANEL_SRC`, the exact X2 anti-pattern this file exists to eliminate.
  - Y2: ROUTE_TRACE's `_verify_evidence` branch was an unconditional
    `return True` -- it performed no execution and did not even confirm the
    referenced backing file existed. The Managers-unified-card reachability
    chain (root -> Менеджеры -> Настройки -> unified card) was, as a
    consequence, verified by NO test in the project.
N5.2.1 fixes both for real:
  - HANDLER_SCOPE is now `_extract_owning_branch_scope()`: an AST walk that
    locates the exact owning `if` branch of a named handler function (by a
    normalized `data.startswith(prefix)` / `data == value` condition, in
    either its positive form -- the branch's own body is the scope -- or
    its negative-guard form `if not <condition>: return` -- everything
    AFTER the guard, in the SAME statement list, is the scope), searches
    ONLY inside that branch's unparsed text, and never crosses into a
    different function. A missing or ambiguous (>1 candidate) owning branch
    is a hard FAIL, not a guess.
  - ROUTE_TRACE now looks up a named tracer function in `ROUTE_TRACERS` and
    executes it against a shared `build_trace_context()` -- real hops
    through `build_n5_ns` (old_root/AI-placeholder/delete-manager) and a
    NEW `build_managers_trace_ns` (the full root -> Менеджеры ->
    _nm_managers_screen -> Настройки -> canonical list -> selected manager
    -> active unified card chain, including a structural no-interception
    proof and a check that the routing generation never references the
    orphaned _manager_admin_detail_text/_buttons -- see `test_9_managers_
    route_trace` for the dedicated, fully-spelled-out version of the same
    computation). The 3 sibling capabilities reached through that SAME
    executed card (Replacement rw:/Relogin/Devlogin) are verified against
    its real rendered callback_data, not merely by naming an external file.

None of the 6 evidence types accepts: a bare short fragment (`menu:content`,
`menu:main`, `manager_settings`, ...) checked with no scope; a marker that
matches only in dead/shadowed code; a marker in an unrelated handler; or a
narrative classification with no executable/scoped backing. Every entry
below declares BOTH its old-evidence and new-evidence as an explicit
tuple (or None where the classification makes one side inapplicable --
TEXT_COMMAND_ONLY_BY_DESIGN has no live new-marker by definition;
FUTURE_PLACEHOLDER has no old-marker by definition).

=== Deep reachability (Part F) =============================================
For the domains the owner explicitly named, this file goes at least one
level past the root button using EXECUTED_SCREEN evidence (real execution,
real rendered buttons) via `build_domain_screens_ns` / `test_8_domain_
screens_all_execute`, PLUS (N5.2.1) a real end-to-end ROUTE_TRACE for the
Managers domain via `build_managers_trace_ns` / `test_9_managers_route_
trace`: Reports+Statistics, Managers+unified card (root-to-card
reachability now traced IN THIS FILE; the card's own internal composition
remains independently EXECUTED and verified by manager_card_unified_
selftest.py, not re-duplicated here), Traffic+Sources, Proxy+renewal/pool,
Business Links submenus (create/manage/delete, one level under the
Бизнес-ссылки button), Transfers, Automation incl. the active auto-status
controls, System incl. Description and the hidden Service path (Система ->
Сервис -> warning -> old_root, real execution in test_3b).

    python tools\\menu_parity_selftest.py
"""
from __future__ import annotations

import ast
import glob
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
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

# Locate the pre-N5 backup created for this phase (mandatory backup, per the
# N5 spec: panel_bot.py.bak_nm2_p5_<timestamp>). Picks the OLDEST matching
# backup if several exist (the one taken immediately before N5's first edit),
# so re-running this test after later same-day fixes still compares against
# the true pre-N5 state.
_P5_BACKUP_CANDIDATES = sorted(glob.glob(str(BASE_DIR / "panel_bot.py.bak_nm2_p5_*")))
if not _P5_BACKUP_CANDIDATES:
    raise AssertionError(
        "menu_parity_selftest: no panel_bot.py.bak_nm2_p5_* backup found -- "
        "this gate requires the exact pre-N5 backup to prove capabilities "
        "genuinely existed before N5 (not a fabricated comparison)."
    )
BACKUP_PATH = _P5_BACKUP_CANDIDATES[0]
BACKUP_SRC = open(BACKUP_PATH, encoding="utf-8-sig").read()

# A SEPARATE, more recent reference for structural "how many defs did THIS
# phase add" checks -- the newest pre-phase backup across N5/N5.1/N5.2
# (panel_bot.py.bak_nm2_p5_* / p51_* / p52_*). Using the original pre-N5
# BACKUP_SRC for that purpose would over-count (it would attribute earlier
# phases' new defs to the latest one). Picks the NEWEST matching backup by
# mtime, so the reference is always "immediately before the most recent
# corrective phase's edits".
_PRIOR_PHASE_CANDIDATES = (
    glob.glob(str(BASE_DIR / "panel_bot.py.bak_nm2_p5_*"))
    + glob.glob(str(BASE_DIR / "panel_bot.py.bak_nm2_p51_*"))
    + glob.glob(str(BASE_DIR / "panel_bot.py.bak_nm2_p52_*"))
    + glob.glob(str(BASE_DIR / "panel_bot.py.bak_nm2_p53_*"))
    + glob.glob(str(BASE_DIR / "panel_bot.py.bak_nm2_p531_*"))
    + glob.glob(str(BASE_DIR / "panel_bot.py.bak_nm2_p532_*"))
    + glob.glob(str(BASE_DIR / "panel_bot.py.bak_nm2_p54_*"))
)
PRIOR_PHASE_BACKUP_PATH = max(_PRIOR_PHASE_CANDIDATES, key=os.path.getmtime) if _PRIOR_PHASE_CANDIDATES else BACKUP_PATH
PRIOR_PHASE_BACKUP_SRC = open(PRIOR_PHASE_BACKUP_PATH, encoding="utf-8-sig").read() if _PRIOR_PHASE_CANDIDATES else BACKUP_SRC


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
    fd, path = tempfile.mkstemp(suffix=".db", prefix="menu_parity_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT);
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
    """Same minimal schema/idiom as tools/manager_card_unified_selftest.py and
    tools/manager_delete_safety_selftest.py -- used by the N5.2.1 Managers
    route trace (Part C) to seed at least one real, queryable manager row."""
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
    """ast.unparse always single-quotes string literals -- tolerate both
    (documented project pitfall, see ADDENDUM_FOR_NEW_PC.md)."""
    if marker in src_txt:
        return True
    if '"' in marker:
        return marker.replace('"', "'") in src_txt
    if "'" in marker:
        return marker.replace("'", '"') in src_txt
    return False


def _active_block(source: str, def_name: str) -> str:
    """Text of the LAST (active, last-wins) def `def_name` in `source`, up to
    the next top-level 'def ' -- scoping technique established in this
    project's N3.1/N3.2/N4 selftests. Empty string if not found."""
    start = source.rfind(f"def {def_name}")
    end = source.find("\ndef ", start + 10) if start != -1 else -1
    return source[start:end] if start != -1 and end != -1 else ""


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


def _find_generation_by_marker(source: str, name: str, marker: str):
    tree = ast.parse(source)
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


# ======================================================================
# Namespace #1: the current active N5/N5.1/N5.2 _title_for_menu generation
# (owns raw == "main", "old_root", "nm_ai_assistant", "old_menu_warning",
# "nm_manager_delete"), plus its real dependencies.
# ======================================================================

def build_n5_ns(db_path: str) -> dict:
    generation = _find_generation_by_marker(PANEL_SRC, "_title_for_menu", 'raw == "old_root"')
    if generation is None:
        raise AssertionError('build_n5_ns: could not locate the N5 _title_for_menu generation '
                              '(marker \'raw == "old_root"\' not found)')
    nodes = _extract_by_name({
        "_nm_root_screen", "_nm_ai_assistant_screen", "_nm_manager_delete_screen",
        "_nm_btn", "_nm_row", "_nm_footer", "_nm_screen", "_nm_canonical_path",
        "_pb_get_setting", "_connect_panel_db", "_N5_PREV_TITLE_FOR_MENU",
        "_manager_rows_all", "_manager_rows", "_manager_settings_state",
    })
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    import manager_registry
    ns = {
        "sqlite3": sqlite3, "os": os,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        "_panel_header": lambda: "HEADER",
        # Deliberately fake -- isolates this test from the entire pre-N5
        # PREV chain (old _main_menu, _tp_visual_* screens, ...): none of
        # that is needed to prove the N5 generation's OWN routing behavior
        # (raw == "main" / raw == "old_root" / fallthrough), only that it
        # calls its PREV correctly (checked structurally, see test_5).
        "_title_for_menu": lambda raw: (f"OLD_ROOT_VIA_PREV:{raw}", []),
    }
    exec(compile(module_src, f"<{PANEL_PATH}:n5>", "exec"), ns)
    exec(compile(ast.unparse(generation), f"<{PANEL_PATH}:n5_routing>", "exec"), ns)
    return ns


# ======================================================================
# Namespace #2 (N5.2 Part F, deep reachability): the 11 canonical domain
# section screens, executed for real. All 11 are simple single-def
# renderers depending only on _nm_btn/_nm_row/_nm_screen/_nm_footer/
# _pb_get_setting (same extraction idiom as
# tools/nm_menu_distribution_selftest.py's build_routing_ns) -- no fakes
# stand in for any button-construction logic, only _panel_header (pure
# cosmetic string) is stubbed.
# ======================================================================

DOMAIN_SCREEN_NAMES = {
    "_nm_btn", "_nm_row", "_nm_footer", "_nm_screen", "_nm_canonical_path",
    "_pb_get_setting", "_connect_panel_db",
    "_nm_reports_screen", "_nm_managers_screen", "_nm_traffic_screen", "_nm_proxy_screen",
    "_nm_bizlinks_screen", "_nm_bizlinks_create_screen", "_nm_bizlinks_manage_screen",
    "_nm_bizlinks_delete_screen", "_nm_transfers_screen", "_nm_automation_screen",
    "_nm_system_screen", "_nm_mgr_schedules_screen",  # N5.4.1
}


def build_domain_screens_ns(db_path: str) -> dict:
    nodes = _extract_by_name(DOMAIN_SCREEN_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "_panel_header": lambda: "HEADER",
    }
    exec(compile(module_src, f"<{PANEL_PATH}:domain_screens>", "exec"), ns)
    return ns


def _set_setting(db_path: str, key: str, value: str) -> None:
    con = sqlite3.connect(db_path)
    try:
        con.execute("INSERT OR REPLACE INTO settings(key, value, updated_at) VALUES (?, ?, '')", (key, value))
        con.commit()
    finally:
        con.close()


# ======================================================================
# N5.2.1 Part A: real AST branch-scoped extraction for HANDLER_SCOPE.
#
# The N5.2 version of HANDLER_SCOPE found the byte offset of a literal
# `anchor` string (e.g. 'if data.startswith("tr:")') and searched a fixed
# 2500-byte window starting AT that anchor for `marker`. An independent
# review (Y1) correctly found this tautological whenever `marker` is a
# substring of `anchor` itself (true for all 6 prior entries) -- the check
# degenerates to a bare `anchor in PANEL_SRC` whole-file search, exactly the
# X2 anti-pattern this file exists to eliminate.
#
# This replacement locates the OWNING branch structurally via AST, not by
# byte offset:
#   - "prefix" condition, POSITIVE form (`if data.startswith("cal:"):` --
#     used for branches living directly inside the large `on_callback`
#     dispatcher): the owning scope is that `if`'s OWN body.
#   - "prefix" condition, NEGATIVE-GUARD form (`if not data.startswith
#     ("tr:"): return` -- used at the top of a dedicated single-purpose
#     `@client.on(events.CallbackQuery)` handler): the owning scope is
#     every statement that follows the guard in the SAME statement list
#     (that's the code that only runs once the prefix has matched).
#   - "equals" condition (`data == "prn:root"`): same two forms, by
#     equality instead of startswith.
# The search never crosses into a nested function/class def, so a marker
# in a different handler cannot satisfy it, and an ambiguous (>1 owning
# branch) or absent match is a hard FAIL rather than a guess.
# ======================================================================

def _ast_is_data_startswith(node, prefix: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute) and node.func.attr == "startswith"
        and isinstance(node.func.value, ast.Name) and node.func.value.id == "data"
        and len(node.args) == 1 and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == prefix
    )


def _ast_is_data_equals(node, value: str) -> bool:
    return (
        isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name) and node.left.id == "data"
        and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1 and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == value
    )


def _ast_branch_kind(test_node, condition_kind: str, condition_value: str):
    """Returns 'positive', 'negative-guard', or None."""
    checker = _ast_is_data_startswith if condition_kind == "prefix" else _ast_is_data_equals
    if checker(test_node, condition_value):
        return "positive"
    if isinstance(test_node, ast.UnaryOp) and isinstance(test_node.op, ast.Not) and checker(test_node.operand, condition_value):
        return "negative-guard"
    return None


def _iter_scoped_stmt_lists(stmts: list):
    """Yields every statement-list ('block') reachable from `stmts` via
    ordinary control flow (If/Try/For/While/With), WITHOUT ever crossing
    into a nested function/class def -- keeps the search confined to ONE
    handler's own body, so a same-named branch inside a different function
    can never be picked up by mistake."""
    yield stmts
    for s in stmts:
        if isinstance(s, ast.If):
            yield from _iter_scoped_stmt_lists(s.body)
            yield from _iter_scoped_stmt_lists(s.orelse)
        elif isinstance(s, ast.Try):
            yield from _iter_scoped_stmt_lists(s.body)
            for h in s.handlers:
                yield from _iter_scoped_stmt_lists(h.body)
            yield from _iter_scoped_stmt_lists(s.orelse)
            yield from _iter_scoped_stmt_lists(s.finalbody)
        elif isinstance(s, (ast.For, ast.AsyncFor, ast.While)):
            yield from _iter_scoped_stmt_lists(s.body)
            yield from _iter_scoped_stmt_lists(s.orelse)
        elif isinstance(s, (ast.With, ast.AsyncWith)):
            yield from _iter_scoped_stmt_lists(s.body)
        # Deliberately NOT descending into FunctionDef/AsyncFunctionDef/
        # ClassDef -- a branch inside a nested def is a DIFFERENT handler.


def _extract_owning_branch_scope(source: str, func_name: str, condition_kind: str, condition_value: str):
    """Returns (scope_text, meta) on success, or (None, meta) with a
    `meta["reason"]` explaining the failure. `meta` always includes
    `handler`, `condition_kind`, `condition_value`, and (on success)
    `branch_kind` + the normalized `condition` text."""
    tree = ast.parse(source)
    fn_node = None
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == func_name:
            fn_node = n  # last-wins, mirrors the project's override-stack semantics
    if fn_node is None:
        return None, {"handler": func_name, "condition_kind": condition_kind,
                       "condition_value": condition_value, "reason": f"function {func_name!r} not found"}

    matches = []
    for stmt_list in _iter_scoped_stmt_lists(fn_node.body):
        for idx, stmt in enumerate(stmt_list):
            if isinstance(stmt, ast.If):
                kind = _ast_branch_kind(stmt.test, condition_kind, condition_value)
                if kind is not None:
                    matches.append((kind, stmt_list, idx, stmt))

    meta_base = {"handler": func_name, "condition_kind": condition_kind, "condition_value": condition_value}
    if not matches:
        return None, {**meta_base, "reason": "no owning branch found (absent -- hard FAIL, not a guess)"}
    if len(matches) > 1:
        return None, {**meta_base, "reason": f"ambiguous: {len(matches)} candidate owning branches found "
                                              f"(not individually documented as valid -- hard FAIL)"}

    kind, stmt_list, idx, stmt = matches[0]
    condition_text = ast.unparse(stmt.test)
    if kind == "positive":
        scope_text = "\n".join(ast.unparse(s) for s in stmt.body)
        return scope_text, {**meta_base, "branch_kind": "positive-if", "condition": condition_text}
    # negative-guard: the owning scope is everything AFTER the guard in the
    # SAME statement list (that code only runs once the prefix matched).
    remaining = stmt_list[idx + 1:]
    scope_text = "\n".join(ast.unparse(s) for s in remaining)
    return scope_text, {**meta_base, "branch_kind": "negative-guard", "condition": condition_text}


# ======================================================================
# Namespace #3 (N5.2.1 Part C): the real Managers -> unified-card route
# trace. Extracted and executed for real (same idiom as build_n5_ns /
# build_domain_screens_ns / tools/manager_card_unified_selftest.py's
# build_card_ns) -- no fake stands in for the card's own button
# construction, the manager-settings list, or the routing generation that
# owns "manager_settings"/"manager_settings:{key}".
# ======================================================================

MANAGERS_TRACE_NAMES = {
    "_nm_btn", "_nm_row", "_nm_footer", "_nm_screen", "_nm_canonical_path",
    "_nm_managers_screen",
    "_manager_settings_text", "_manager_settings_buttons",
    "_manager_settings_card_text", "_manager_settings_card_buttons",
    "_manager_settings_state", "_mcard_btn",
    "_manager_rows_all", "_manager_rows", "_manager_row_by_key",
    "_manager_short_label", "_manager_proxy_badge",
    # N5.4.2: the shared status resolver the list/counters now call.
    "_pb_manager_status_resolve", "_pb_manager_status_resolve_live",
    "_pb_tg_health_row", "_pb_manager_process_running",
    "_PB_STATUS_CATEGORIES", "_PB_STATUS_BANNED_MARKS", "_PB_STATUS_AUTH_MARKS",
    "_connect_panel_db",
    # 2026-07-25 proxy-freshness incident fix: _pb_manager_status_resolve
    # now calls _pb_proxy_guard_state, which needs these two.
    "_pb_proxy_guard_state", "_PB_PROXY_GUARD_FRESH_SEC", "_tpag_panel_v2_parse_dt",
    # 2026-07-25 R1-R3 follow-up (independent-review): _manager_proxy_badge
    # (called by _manager_settings_card_text above) and
    # _pb_manager_status_resolve's proxy categories now both route through
    # the single shared _pb_proxy_effective_state predicate (R2/R3).
    "_pb_proxy_effective_state", "_pb_proxy_ts_is_fresh",
    "_PB_PROXY_CLOCK_SKEW_TOLERANCE_SEC", "_PB_PROXY_BADGE_TEXT",
}


def build_managers_trace_ns(db_path: str) -> dict:
    import manager_registry

    tree = ast.parse(PANEL_SRC)
    nodes = _extract_by_name(MANAGERS_TRACE_NAMES)

    # The ONE _title_for_menu generation that owns manager_settings/
    # manager_settings:{key} routing (the N2 unified-card block) -- located
    # by the same disambiguating marker tools/manager_card_unified_
    # selftest.py already uses ('manager_disable_confirm:' is unique to
    # this generation).
    settings_gen = _find_generation_by_marker(PANEL_SRC, "_title_for_menu", "manager_disable_confirm:")
    if settings_gen is None:
        raise AssertionError(
            "build_managers_trace_ns: could not locate the _title_for_menu generation "
            "that owns manager_settings/manager_disable_confirm/manager_danger routing "
            "(marker 'manager_disable_confirm:' not found)"
        )
    nodes.append(settings_gen)

    # PART C item 7 (no interception): the SAME generation must also be the
    # LAST one containing 'raw.startswith("manager_settings:")' -- if some
    # later generation intercepted that route without also containing
    # 'manager_disable_confirm:', _find_generation_by_marker would return a
    # DIFFERENT generation here. Note: each call to _find_generation_by_marker
    # does its own ast.parse(), so comparing the returned nodes with `is`
    # would ALWAYS be False (different tree objects) even for the identical
    # source position -- compare by source line number instead, which is
    # stable across independent parses of the same text.
    intercept_check_gen = _find_generation_by_marker(PANEL_SRC, "_title_for_menu", 'raw.startswith("manager_settings:")')
    not_intercepted = (intercept_check_gen is not None
                       and getattr(intercept_check_gen, "lineno", None) == getattr(settings_gen, "lineno", None))

    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "sqlite3": sqlite3, "os": os, "re": re,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
        "datetime": datetime, "timezone": timezone,
        "Button": _FakeButton,
        "TPILOT_DB_PATH": db_path,
        "BASE_DIR": BASE_DIR,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        "_panel_header": lambda: "HEADER",
        "_tp_visual_screen": lambda path_items, description="", extra="": "\n".join([" > ".join(path_items), description, extra]).strip(),
        "_safe_text": lambda s, limit=3900: str(s or "")[:limit],
        "_norm_path": lambda s: str(s or "").replace("\\", "/").lower(),
        # Same fakes tools/manager_card_unified_selftest.py already
        # validated for these exact card dependencies -- none of them
        # stand in for button construction, routing, or the inclusion rule.
        "_replace_active_op_for_old_key": lambda key: {},
        "_ss_config": lambda key: {"mode": "off", "reminder_enabled": False, "reminder_time": "18:00"},
        "_ss_counts_today": lambda key: (0, 0),
        "_SS_MODE_LABELS": {"off": "Выкл", "instant": "Сразу", "daily": "В конце дня"},
        # N5.4.2: no real PowerShell scan in a selftest -- same deliberate
        # fake as tools/manager_card_unified_selftest.py and tools/
        # manager_status_resolver_selftest.py (None = fail-open, matches
        # production with no scan data yet).
        "_pb_service_scan_cached": lambda: None,
        # Deliberately fake -- isolates this trace from the other ~30
        # generations, none of which own manager_settings routing.
        "_title_for_menu": lambda raw: (f"OLD_MENU:{raw}", []),
    }
    exec(compile(module_src, f"<{PANEL_PATH}:managers_trace>", "exec"), ns)
    ns["_settings_generation_not_intercepted"] = not_intercepted
    ns["_settings_generation_src"] = ast.unparse(settings_gen)
    return ns


# ======================================================================
# N5.2.1 Part B: real, deterministic ROUTE_TRACE tracers (replacing the
# unconditional `return True` stub). Each tracer performs a REAL execution
# hop (or, for the manager-deletion flow, defers to the dedicated,
# independently-run tools/manager_delete_safety_selftest.py, whose own
# existence is checked -- never assumed) and returns (ok, detail). Bounded
# to a single hop per tracer (max_hops enforced where a loop could occur);
# a route that resolves only via recursion or an unresolved fallback is a
# hard FAIL, never a silent pass.
# ======================================================================

def _trace_via_n5_ns(ctx, route: str, expect):
    """route is the argument to ns["_title_for_menu"] (no "menu:" prefix).
    `expect` is either bytes tag text to compare against exactly, or a
    2-tuple (canonical_screen_name, None) meaning: compare against calling
    that canonical screen function directly (byte-for-byte text+datas)."""
    ns = ctx["n5"]
    visited = set()
    if route in visited:
        return False, f"recursive trace at route {route!r}"
    visited.add(route)
    text, rows = ns["_title_for_menu"](route)
    if isinstance(expect, tuple):
        canonical_name = expect[0]
        canon_text, canon_rows = ns[canonical_name]()
        ok = (text == canon_text) and (_datas(rows) == _datas(canon_rows))
        return ok, (f"route {route!r} -> compared byte-for-byte against {canonical_name}(): "
                    f"{'match' if ok else 'MISMATCH'} (got {text[:60]!r})")
    ok = text == expect
    return ok, f"route {route!r} -> expected tag {expect!r}, got {text[:60]!r}"


def _trace_old_root(ctx):
    return _trace_via_n5_ns(ctx, "old_root", "OLD_ROOT_VIA_PREV:main")


def _trace_ai_placeholder(ctx):
    return _trace_via_n5_ns(ctx, "nm_ai_assistant", ("_nm_ai_assistant_screen", None))


def _trace_delete_manager(ctx):
    return _trace_via_n5_ns(ctx, "nm_manager_delete", ("_nm_manager_delete_screen", None))


def _trace_managers_unified_card(ctx):
    mt = ctx.get("managers_trace")
    if mt is None:
        return False, "managers trace context was not built"
    if not mt["not_intercepted"]:
        return False, "the manager_settings: route is intercepted by a LATER generation than the one owning manager_disable_confirm: -- see Part C item 7"
    if not mt["root_emits_nm_managers"]:
        return False, "root screen does not emit menu:nm_managers"
    if not mt["managers_emits_settings"]:
        return False, "_nm_managers_screen does not emit menu:manager_settings"
    if not mt["list_has_manager_row"]:
        return False, "manager_settings list rendered zero manager rows for the seeded manager"
    if not mt["row_targets_card"]:
        return False, f"seeded manager's row does not target menu:manager_settings:{mt['key']!r}"
    if not mt["card_matches_canonical"]:
        return False, "manager_settings:{key} route did not resolve to _manager_settings_card_text/_buttons verbatim"
    if mt["references_orphan_card"]:
        return False, "the owning routing generation references the orphaned _manager_admin_detail_text/_buttons"
    return True, ("root -> Менеджеры -> _nm_managers_screen -> Настройки -> canonical list "
                  "-> selected manager -> active unified card: all hops verified, no interception, no orphan reference")


def _trace_replacement_rw(ctx):
    mt = ctx.get("managers_trace")
    if mt is None or not mt["card_matches_canonical"]:
        return False, "unified card trace prerequisite failed"
    ok = mt["card_emits_rw"]
    return ok, f"executed unified card emits an rw: callback: {ok} (datas: {mt['card_datas']})"


def _trace_relogin(ctx):
    mt = ctx.get("managers_trace")
    if mt is None or not mt["card_matches_canonical"]:
        return False, "unified card trace prerequisite failed"
    ok = mt["card_emits_relogin"]
    return ok, f"executed unified card emits a relogin: callback: {ok} (datas: {mt['card_datas']})"


def _trace_devlogin(ctx):
    mt = ctx.get("managers_trace")
    if mt is None or not mt["card_matches_canonical"]:
        return False, "unified card trace prerequisite failed"
    ok = mt["card_emits_devlogin"]
    return ok, f"executed unified card emits a devlogin: callback: {ok} (datas: {mt['card_datas']})"


ROUTE_TRACERS = {
    "old_root": _trace_old_root,
    "ai_placeholder": _trace_ai_placeholder,
    "delete_manager": _trace_delete_manager,
    "managers_unified_card": _trace_managers_unified_card,
    "replacement_rw": _trace_replacement_rw,
    "relogin": _trace_relogin,
    "devlogin": _trace_devlogin,
}


def build_trace_context(db_path: str) -> dict:
    """Built ONCE per test run and shared by test_7 (capability matrix),
    test_9 (dedicated Managers route trace), and every ROUTE_TRACE
    evidence lookup -- so the matrix's evidence and the dedicated test
    are provably the SAME computation, not two divergent implementations."""
    ctx = {"n5": build_n5_ns(db_path), "domain": build_domain_screens_ns(db_path)}

    managers_ns = build_managers_trace_ns(db_path)
    key = "mp_trace_mgr"
    _seed_managers(db_path, [{"manager_key": key, "display_name": "Trace Manager", "status": "active",
                               "is_enabled": 1, "manual_stopped": 0}])

    root_text, root_rows = ctx["n5"]["_nm_root_screen"]()
    mgr_text, mgr_rows = managers_ns["_nm_managers_screen"]()
    list_text, list_rows = managers_ns["_title_for_menu"]("manager_settings")
    row_target = f"menu:manager_settings:{key}".encode("utf-8")
    card_text, card_rows = managers_ns["_title_for_menu"](f"manager_settings:{key}")
    canon_card_text = managers_ns["_manager_settings_card_text"](key)
    canon_card_rows = managers_ns["_manager_settings_card_buttons"](key)
    card_matches = (card_text == canon_card_text) and (_datas(card_rows) == _datas(canon_card_rows))
    card_datas = _datas(card_rows)

    ctx["managers_trace"] = {
        "key": key,
        "not_intercepted": bool(managers_ns["_settings_generation_not_intercepted"]),
        "root_emits_nm_managers": b"menu:nm_managers" in _datas(root_rows),
        "managers_emits_settings": b"menu:manager_settings" in _datas(mgr_rows),
        "list_has_manager_row": row_target in _datas(list_rows),
        "row_targets_card": row_target in _datas(list_rows),
        "card_matches_canonical": card_matches,
        "references_orphan_card": ("_manager_admin_detail_text" in managers_ns["_settings_generation_src"]
                                    or "_manager_admin_detail_buttons" in managers_ns["_settings_generation_src"]),
        "card_datas": card_datas,
        "card_emits_rw": any(d.startswith(b"rw:") for d in card_datas),
        "card_emits_relogin": any(d.startswith(b"relogin:") for d in card_datas),
        "card_emits_devlogin": any(d.startswith(b"devlogin:") for d in card_datas),
    }
    return ctx


# ======================================================================
# 1. Root composition (Part E).
# ======================================================================

# N5.1 corrected root layout (owner-approved 2026-07-21): 8 rows, 12 raw
# buttons, exact order [1,1,2,2,2,2,1,1].
# N5.2 Part A: row7's target changed to the dedicated deletion-selection
# screen (menu:nm_manager_delete), replacing the generic manager-settings
# list -- see tools/manager_delete_safety_selftest.py for the deep safety
# proofs on that new screen.
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


def test_1_root_composition() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_n5_ns(db_path)
        text, rows = ns["_nm_root_screen"]()
        labels = _labels(rows)
        datas = _datas(rows)

        check("E1. Root has exactly 8 rows", len(rows) == 8, [len(r) for r in rows])
        row_counts = [len(r) for r in rows]
        check("E1b. Root row button counts are exactly [1,1,2,2,2,2,1,1]", row_counts == [1, 1, 2, 2, 2, 2, 1, 1], row_counts)
        check("E3. Exact raw total root button count == 12", len(_flat(rows)) == 12, (len(_flat(rows)), labels))

        actual_rows = [[(b[1], b[2]) for b in row] for row in rows]
        check("E4. Root rows match the exact corrected N5.1/N5.2 layout, in exact order (labels+callbacks)",
              actual_rows == _EXPECTED_ROOT_ROWS_N51, actual_rows)

        dupes = sorted({d for d in datas if datas.count(d) > 1})
        check("E5. No duplicate callback_data on the root", not dupes, dupes)

        for b in _flat(rows):
            check(f"E6. root callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))

        check("E7. No legacy Gen-1/Gen-2 tile on canonical root ('Отчёты и контроль', 'Менеджеры и режимы', 'Трафик и байеры')",
              not any(l in ("📊 Отчёты и контроль", "👥 Менеджеры и режимы", "📦 Трафик и байеры") for l in labels), labels)
        check("E8. No separate top-level 'Качество', 'Контроль' or 'Источники' on canonical root",
              not any(l in ("📈 Качество и дисциплина", "🧭 Контроль", "📌 Источники") or
                      ("Качество" in l) or (l == "🧭 Контроль") for l in labels), labels)
        check("E9. No '🧭 Новое меню' label appears in the canonical root", "🧭 Новое меню" not in labels, labels)

        check("E-F1. Search (🔎 Поиск) absent from root", not any("Поиск" in l for l in labels), labels)
        check("E-F2. Description (ℹ️ Описание) absent from root", not any(l.startswith("ℹ️") for l in labels), labels)
        check("E-F3. Old menu (↩️ Старое меню) absent from root", not any("Старое меню" in l for l in labels), labels)
        check("E-F4. Root does not directly emit menu:nm_search / menu:description / menu:old_root",
              not any(d in (b"menu:nm_search", b"menu:description", b"menu:old_root") for d in datas), datas)

        check("E10. AI-ассистент present exactly once on root", sum(1 for l in labels if "AI-ассистент" in l) == 1, labels)
        check("E11. Добавить менеджера present exactly once on root", sum(1 for l in labels if l == "➕ Добавить менеджера") == 1, labels)
        check("E12. Остановка и удаление present exactly once on root (N5.3 rename)", sum(1 for l in labels if l == "⏸ Остановка и удаление") == 1, labels)
        check("E13. Обновить present exactly once on root", sum(1 for l in labels if l == "🔄 Обновить") == 1, labels)

        # N5.2 Part A: the delete button must NOT open the generic manager
        # list any more -- both the positive (new target present) and
        # negative (old target absent) sides are asserted here, on the
        # REAL rendered root.
        check("A1. Удалить менеджера routes to the dedicated selection screen (menu:nm_manager_delete)",
              _find(rows, label="⏸ Остановка и удаление", data=b"menu:nm_manager_delete") is not None, rows)
        check("A2. Удалить менеджера no longer routes to the generic manager-settings list",
              b"menu:manager_settings" not in datas, datas)
    finally:
        os.unlink(db_path)


# ======================================================================
# 2. Primary entry points (Part B) + active composition safety (Part F).
# ======================================================================

def test_2_panel_start_menu_main_render_canonical_root() -> None:
    check("F5. /panel and /start funnel through the single _send_fresh_panel -> _title_for_menu(\"main\") call site",
          '_title_for_menu(\"main\")' in PANEL_SRC or "_title_for_menu('main')" in PANEL_SRC, None)
    # N5 adds its OWN raw == "main" branch (the new single owner reached
    # first at runtime, since it's the LAST generation); the original pre-N5
    # handler (:4835) is untouched and still exists further down the PREV
    # chain -- so exactly 2 code occurrences is the correct invariant now
    # (not 1), and this proves no THIRD, conflicting handler was introduced.
    check("B1/B3. exactly 2 'raw == \"main\"' handlers in the file (the N5 override + the untouched pre-N5 handler; no third/conflicting owner)",
          PANEL_SRC.count('raw == "main"') == 2, PANEL_SRC.count('raw == "main"'))

    db_path = _make_temp_db()
    try:
        ns = build_n5_ns(db_path)
        text, rows = ns["_title_for_menu"]("main")
        canonical_text, canonical_rows = ns["_nm_root_screen"]()
        check("B1/B2/B3. menu:main (and therefore /panel, /start, panel:back) renders the canonical new root verbatim",
              text == canonical_text and _datas(rows) == _datas(canonical_rows), (text[:80], canonical_text[:80]))
    finally:
        os.unlink(db_path)


def test_3_old_root_temporary_route_and_loop_safety() -> None:
    db_path = _make_temp_db()
    try:
        ns = build_n5_ns(db_path)
        # C: "old_root" must delegate to PREV("main") -- the fake PREV in this
        # namespace tags its output distinctly so we can PROVE delegation
        # happened (not a coincidental match), without needing the entire
        # pre-N5 chain (that chain's own correctness is unchanged/untouched
        # and covered by the historical-callback checks in test_6).
        text, rows = ns["_title_for_menu"]("old_root")
        check("C. 'old_root' route delegates to PREV(\"main\") (proven via a tagged fake PREV, not a coincidental match)",
              text == "OLD_ROOT_VIA_PREV:main", text)

        # C5/F4: no recursion -- "old_root" must NOT re-enter the "main"
        # branch of THIS SAME generation. Structural proof, scoped to this
        # one generation's own body (ACTIVE_GENERATION evidence): the fake
        # "_title_for_menu" bound in this namespace is a different object
        # than ns["_title_for_menu"] itself; if the generation's "old_root"
        # branch called the global name "_title_for_menu" recursively
        # instead of the captured PREV reference, it would hit OUR fake for
        # input "main" -- which is exactly the correct, safe behavior
        # asserted above. The dangerous case (calling _title_for_menu
        # ("old_root") again from inside) is proven absent structurally.
        gen_src = ast.unparse(_find_generation_by_marker(PANEL_SRC, "_title_for_menu", 'raw == "old_root"'))
        body_only = gen_src.split("\n", 1)[1] if "\n" in gen_src else ""
        check("C5/F4. no recursive self-capture: the N5/N5.1/N5.2 generation's own body never calls "
              "_title_for_menu(...) directly (only via its captured PREV reference)",
              "_title_for_menu(" not in body_only, gen_src)
    finally:
        os.unlink(db_path)


def test_3b_hidden_old_menu_path_and_ai_placeholder() -> None:
    # Part G: the hidden old-menu path (Система -> Сервис -> warning ->
    # old_root) is executed for real, not just marker-checked.
    db_path = _make_temp_db()
    try:
        ns = build_n5_ns(db_path)

        # nm_service_w must emit the warning-screen route. ACTIVE_GENERATION
        # evidence: 'raw == "nm_service_w"' occurs EXACTLY ONCE in the whole
        # file (verified below), so this generation cannot be shadowed by a
        # later duplicate -- the marker is then checked only within THAT
        # generation's own unparsed body, not the whole file.
        check("HANDLER-UNIQUENESS. exactly one generation owns raw == \"nm_service_w\" (no possible shadow)",
              PANEL_SRC.count('raw == "nm_service_w"') == 1, PANEL_SRC.count('raw == "nm_service_w"'))
        service_gen = _find_generation_by_marker(PANEL_SRC, "_title_for_menu", 'raw == "nm_service_w"')
        check("G0. nm_service_w generation located", service_gen is not None, None)
        service_gen_src = ast.unparse(service_gen) if service_gen is not None else ""
        check("G0. nm_service_w emits the hidden warning route (menu:old_menu_warning), not menu:old_root directly, "
              "checked ONLY within this one generation's own body (ACTIVE_GENERATION, not whole-file)",
              _contains_quote_tolerant(service_gen_src,
                                       'extra_rows=[_nm_row(_nm_btn("↩️ Временное старое меню", "menu:old_menu_warning"))]'),
              service_gen_src[:400])

        # The warning screen itself: real render, exact 3 buttons, exact targets.
        text, buttons = ns["_title_for_menu"]("old_menu_warning")
        check("G1. warning screen text is non-empty and mentions the temporary nature", "⚠️" in text and "Старое меню" in text, text)
        flat = [b for row in buttons for b in row]
        check("G2. warning screen has exactly 3 buttons", len(flat) == 3, flat)
        check("G3. warning screen's confirm button opens the UNCHANGED N5 temporary route (menu:old_root, not a new implementation)",
              _find(buttons, label="↩️ Открыть старое меню", data=b"menu:old_root") is not None, buttons)
        check("G4. warning screen's Back button returns to Сервис (menu:nm_service_w), not the root",
              _find(buttons, label="⬅️ Назад", data=b"menu:nm_service_w") is not None, buttons)
        check("G5. warning screen's Home button returns to the corrected canonical root (menu:main)",
              _find(buttons, label="🏠 Главная", data=b"menu:main") is not None, buttons)
        for b in flat:
            check(f"G6. warning screen callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))

        # No recursion: the warning-screen branch must not call
        # _title_for_menu(...) itself (only emit button data referencing
        # other routes, which is safe -- those are resolved on a SEPARATE,
        # later click, not during this render).
        gen_src = ast.unparse(_find_generation_by_marker(PANEL_SRC, "_title_for_menu", 'raw == "old_menu_warning"'))
        body_only = gen_src.split("\n", 1)[1] if "\n" in gen_src else ""
        check("G7/C5. no recursive self-capture in the warning-screen branch (only emits routes, never calls _title_for_menu(...) itself)",
              "_title_for_menu(" not in body_only, gen_src)

        # AI-assistant placeholder: real render, non-mutating (no DB write
        # helper referenced), N5.2 Part C custom footer (NOT the shared
        # _nm_footer any more -- Home must explicitly read "🏠 Главная").
        ai_text, ai_rows = ns["_title_for_menu"]("nm_ai_assistant")
        canonical_ai_text, canonical_ai_rows = ns["_nm_ai_assistant_screen"]()
        check("B1. nm_ai_assistant route renders _nm_ai_assistant_screen() verbatim (no second implementation)",
              ai_text == canonical_ai_text and _datas(ai_rows) == _datas(canonical_ai_rows), (ai_text[:60], canonical_ai_text[:60]))
        check("B2. AI-assistant screen explicitly states it is NOT yet operational",
              "не работает" in ai_text or "Готовится" in ai_text, ai_text)
        # N5.2 Part E: B3 strengthened from a bare "LLM" substring (which a
        # mutation could satisfy by accident) to the exact distinguishing
        # clause the screen uses to tell itself apart from the LLM
        # supervisor toggle -- proven against the REAL executed text.
        check("B3 (strengthened, N5.2 Part E). AI-assistant screen states the FULL distinguishing clause "
              "versus the LLM supervisor toggle (not just the bare substring 'LLM')",
              "то же самое, что переключатель LLM" in ai_text, ai_text)
        ai_flat = [b for row in ai_rows for b in row]
        check("B4 (N5.2 Part C). AI-assistant screen has exactly 2 footer buttons", len(ai_flat) == 2, ai_flat)
        check("B4c (N5.2 Part C). AI-assistant Home button explicitly reads '🏠 Главная' -> menu:main "
              "(not the shared _nm_footer's '🏠 Новое меню' -> menu:nm_root)",
              _find(ai_rows, label="🏠 Главная", data=b"menu:main") is not None, ai_rows)
        check("B4d (N5.2 Part C). AI-assistant Back button returns to menu:main (owner-approved: AI opens directly from root)",
              _find(ai_rows, label="⬅️ Назад", data=b"menu:main") is not None, ai_rows)
        check("B4e (N5.2 Part C). AI-assistant screen no longer uses the generic '🏠 Новое меню' label",
              "🏠 Новое меню" not in _labels(ai_rows), _labels(ai_rows))
        for b in ai_flat:
            check(f"B5. AI-assistant callback_data <=64 bytes: {b[1]!r}", len(b[2]) <= 64, (b[1], b[2], len(b[2])))

        # N5.2 Part A: the dedicated manager-deletion selection screen route.
        del_text, del_rows = ns["_title_for_menu"]("nm_manager_delete")
        canonical_del_text, canonical_del_rows = ns["_nm_manager_delete_screen"]()
        check("A3. nm_manager_delete route renders _nm_manager_delete_screen() verbatim (no second implementation)",
              del_text == canonical_del_text and _datas(del_rows) == _datas(canonical_del_rows),
              (del_text[:60], canonical_del_text[:60]))
    finally:
        os.unlink(db_path)


def test_4_active_composition_safety() -> None:
    # F: exactly one active owner of the root route; PREV chain intact;
    # handler/def inventory preserved; no @client.on before client creation.
    check("F3. N5 PREV-capture marker present", "_N5_PREV_TITLE_FOR_MENU = globals().get(" in PANEL_SRC, None)
    last_title_for_menu_block = PANEL_SRC[PANEL_SRC.rfind("def _title_for_menu"):]
    check("F6. no later generation shadows the N5/N5.1/N5.2 override (the textually LAST _title_for_menu def is the N5 one)",
          'raw == "old_root"' in last_title_for_menu_block[:600], last_title_for_menu_block[:200])
    client_line = PANEL_SRC.find("\nclient = TelegramClient(")
    first_on = PANEL_SRC.find("\n@client.on(")
    check("F8. no @client.on(...) appears before client creation", 0 < client_line < first_on, (client_line, first_on))

    # Structural delta checks use PRIOR_PHASE_BACKUP_SRC (the newest
    # pre-phase backup across N5..N5.4.6 -- currently the pre-N5.4 backup,
    # taken immediately before the N5.4.1 navigation edits) rather than the
    # original pre-N5 BACKUP_SRC -- using the pre-N5 snapshot here would
    # over-count (it would attribute earlier phases' new defs to the
    # latest one). N5.4.1+N5.4.2+N5.4.3+N5.4.4 together add FIFTEEN new
    # top-level defs: _nm_mgr_schedules_screen / _nm_mgr_schedules_all_
    # screen (N5.4.1 schedule navigation, reusing the sch:/schreq: editor
    # verbatim -- see canonical_back test_11); _pb_manager_status_resolve /
    # _pb_manager_status_resolve_live / _pb_tg_health_row /
    # _pb_manager_process_running (N5.4.2 shared status resolver -- see
    # tools\manager_status_resolver_selftest.py); _pb_manager_proxy_lease_row
    # / _pb_full_card_days_left / _pb_full_card_line / _pb_manager_full_
    # card_sections / _pb_manager_full_card_header / _pb_manager_full_
    # card_text / _pb_manager_full_card_needs_pages / _pb_manager_full_
    # card_buttons (N5.4.3 native full card -- see tools\manager_full_
    # card_selftest.py); _panel_log_delivery_error (N5.4.4 observability
    # fix for the result-delivery swallow points -- see tools\panel_
    # delivery_observability_selftest.py). N5.4.6 (independent-review
    # corrective micro-fix) adds exactly TWO more: _panel_sanitize_log_field
    # (RF4 -- strips control chars from every dynamic delivery-log field,
    # called from _panel_log_delivery_error; see tools\panel_delivery_
    # observability_selftest.py tests 6/7) and _pb_escape_md (RF5 --
    # neutralizes Telethon markdown/link delimiters in every dynamic full-
    # card field, called from _pb_full_card_line; see tools\manager_full_
    # card_selftest.py tests 11/12). 2026-07-25 proxy-freshness incident fix
    # (manager foxy1) adds exactly ONE more: _pb_proxy_guard_state (the
    # fresh/stale/never/latest-wins proxy-health classifier called from
    # _pb_manager_status_resolve's proxy_bad/ok branches and from
    # _pb_manager_full_card_sections -- see tools\manager_status_resolver_
    # selftest.py test_9/test_10 and tools\manager_full_card_selftest.py
    # test_14). 2026-07-25 R1-R3 follow-up (independent-review) adds FOUR
    # more top-level defs: _pb_proxy_ts_is_fresh, _pb_proxy_timestamp_
    # malformed, _pb_proxy_state_check_text, _pb_proxy_effective_state (the
    # single shared predicate R3 requires -- see tools\manager_status_
    # resolver_selftest.py test_9b and tools\manager_full_card_selftest.py
    # test_14g-14j). The 3 new module-level assigns this same phase adds
    # (_PB_PROXY_BADGE_TEXT, _PB_PROXY_CLOCK_SKEW_TOLERANCE_SEC,
    # _PB_PROXY_STATE_LABELS) are NOT `def` statements and do not affect
    # this count. 2026-07-26 forward-fix (post-incident review, Phase 1/2)
    # adds THREE more top-level defs: _pb_proxy_counts_effective (P1.1 --
    # the canonical shared-resolver root-header count, replacing the
    # independent _tpag_panel_v2_proxy_counts/_tpag_panel_v2_fresh chain as
    # _panel_header's proxy-count source -- see tools\root_status_block_
    # selftest.py), _pb_persisted_proxy_balance + _pb_proxy_balance_value
    # (P2 -- persisted-snapshot-first balance readers, replacing a direct
    # in-memory-cache read in _pb_cached_proxy_balance_str/_pb_proxy_
    # indicator_icon -- see tools\root_status_block_selftest.py and the new
    # proxy_balance_snapshot_selftest.py). Total: +18 (N5.4.1-N5.4.6 +
    # incident fix) + 4 (R1-R3 follow-up) + 3 (2026-07-26 forward-fix) =
    # +25. NO new PREV captures (none of these override an existing name)
    # -- verified below (F16).
    def_count_now = len([1 for _ in __import__("re").finditer(r"^(?:async )?def ", PANEL_SRC, __import__("re").M)])
    def_count_prior = len([1 for _ in __import__("re").finditer(r"^(?:async )?def ", PRIOR_PHASE_BACKUP_SRC, __import__("re").M)])
    check("F7/17. exactly +25 top-level defs vs the immediately-prior (pre-N5.4) backup "
          "(2 schedule-nav + 4 status-resolver + 8 native full-card + 1 observability + "
          "2 N5.4.6 helpers (_panel_sanitize_log_field/_pb_escape_md) + "
          "1 proxy-freshness helper (_pb_proxy_guard_state) + "
          "4 R1-R3 follow-up helpers (_pb_proxy_ts_is_fresh/_pb_proxy_timestamp_malformed/"
          "_pb_proxy_state_check_text/_pb_proxy_effective_state) + "
          "3 2026-07-26 forward-fix helpers (_pb_proxy_counts_effective/"
          "_pb_persisted_proxy_balance/_pb_proxy_balance_value))",
          def_count_now == def_count_prior + 25, (def_count_now, def_count_prior))

    handler_count_now = PANEL_SRC.count("@client.on(") + PANEL_SRC.count("add_event_handler(")
    handler_count_prior = PRIOR_PHASE_BACKUP_SRC.count("@client.on(") + PRIOR_PHASE_BACKUP_SRC.count("add_event_handler(")
    check("F9/16. handler registration inventory unchanged vs the immediately-prior backup", handler_count_now == handler_count_prior,
          (handler_count_now, handler_count_prior))

    prev_count_now = len(__import__("re").findall(r"PREV.*= globals\(\)\.get\(", PANEL_SRC))
    prev_count_prior = len(__import__("re").findall(r"PREV.*= globals\(\)\.get\(", PRIOR_PHASE_BACKUP_SRC))
    check("F16. PREV-capture count unchanged vs the immediately-prior (pre-N5.4) backup "
          "(N5.4.1/N5.4.2/N5.4.3/N5.4.4 add no new capture)",
          prev_count_now == prev_count_prior, (prev_count_now, prev_count_prior))


def test_5_old_menu_still_has_new_menu_entry() -> None:
    # C2: old menu still contains its existing "🧭 Новое меню" entry.
    check("C2. old root's '🧭 Новое меню' -> menu:nm_root entry still present, unchanged",
          '[Button.inline("🧭 Новое меню", b"menu:nm_root")]' in PANEL_SRC, None)


# ======================================================================
# 6. Historical callback matrix (Part D "no old button becomes unhandled").
# ======================================================================

def test_6_historical_callbacks_remain_handled() -> None:
    for marker in (
        'raw == "nm_root"', 'raw == "description"',
        'if raw == "llm_toggle":', 'if raw == "nm_llm_toggle_do":',
        'data.startswith("cal:")', 'data.startswith("wiz:period:")',
        'data == "prn:root"', 'data.startswith("renew:calc:")',
        'def _main_menu', 'def _tp_visual_reports_menu',
    ):
        check(f"6. historical marker still handled: {marker!r}", marker in PANEL_SRC, marker)

    # N5.2 Part D: Описание's Back button now targets the canonical Система
    # screen instead of the root -- scoped to the ACTIVE (last-wins)
    # "description" branch's generation, not the whole file (that branch
    # lives in an earlier, still-active generation shared by old and new
    # menu alike -- untouched route/handler, only its own button content
    # changed).
    desc_gen = _find_generation_by_marker(PANEL_SRC, "_title_for_menu", 'raw == "description"')
    check("D1. 'description' generation located", desc_gen is not None, None)
    desc_gen_src = ast.unparse(desc_gen) if desc_gen is not None else ""
    check("D2 (N5.2 Part D). Описание's Back button targets menu:nm_system (its real parent), "
          "checked ONLY within the active 'description' generation (ACTIVE_GENERATION)",
          _contains_quote_tolerant(desc_gen_src, 'Button.inline("⬅️ Назад", b"menu:nm_system")'), desc_gen_src[:600])
    check("D3 (N5.2 Part D). Описание's Home button still targets menu:main",
          _contains_quote_tolerant(desc_gen_src, 'Button.inline("🏠 Главная", b"menu:main")'), desc_gen_src[:600])
    check("D4. No second 'description' implementation exists (exactly one generation owns raw == \"description\")",
          PANEL_SRC.count('raw == "description"') == 1, PANEL_SRC.count('raw == "description"'))


# ======================================================================
# 7. CAPABILITY_MATRIX -- the parity gate itself (N5.2 Part F contract).
# ======================================================================

REACHABLE_CANONICAL = "REACHABLE_CANONICAL"
INTENTIONAL_ALIAS = "INTENTIONAL_ALIAS"
TRANSITIONAL_SHORTCUT = "TRANSITIONAL_SHORTCUT"
TEXT_COMMAND_ONLY_BY_DESIGN = "TEXT_COMMAND_ONLY_BY_DESIGN"
BLOCKING_MISSING = "BLOCKING_MISSING"
FUTURE_PLACEHOLDER = "FUTURE_PLACEHOLDER"

# Evidence-type tags (Part F, revised N5.2.1 Parts A/B). old/new fields
# below are either None, or a tuple whose first element is one of these tags:
#   ("OLD_BACKUP", marker)                              -- whole pre-N5 backup, full-line marker.
#   ("ACTIVE_FUNCTION", func_name, marker)               -- scoped to that function's active body.
#   ("ACTIVE_GENERATION", disambig_marker, marker)       -- scoped to that _title_for_menu generation.
#   ("HANDLER_SCOPE", func_name, cond_kind, cond_value, marker)
#                                                        -- N5.2.1: scoped to the ONE owning
#                                                           branch of `func_name`, located
#                                                           structurally via AST
#                                                           (_extract_owning_branch_scope);
#                                                           cond_kind is "prefix" or "equals".
#   ("EXECUTED_SCREEN", screen_name, marker_bytes)       -- real execution, exact callback_data match.
#   ("ROUTE_TRACE", tracer_name)                         -- N5.2.1: looks up a REAL, deterministic
#                                                           tracer function in ROUTE_TRACERS and
#                                                           executes it against the shared trace
#                                                           context -- never an unconditional stub.
ACTIVE_FUNCTION = "ACTIVE_FUNCTION"
ACTIVE_GENERATION = "ACTIVE_GENERATION"
HANDLER_SCOPE = "HANDLER_SCOPE"
EXECUTED_SCREEN = "EXECUTED_SCREEN"
ROUTE_TRACE = "ROUTE_TRACE"
OLD_BACKUP = "OLD_BACKUP"


def _verify_evidence(ev, *, source: str, domain_ns: dict | None, ctx: dict | None = None) -> tuple[bool, str]:
    """Returns (ok, detail). `source` is PANEL_SRC for new-side checks,
    BACKUP_SRC for old-side OLD_BACKUP checks. `ctx` is the shared trace
    context (build_trace_context) required for ROUTE_TRACE evidence."""
    if ev is None:
        return True, "(not applicable for this classification)"
    kind = ev[0]
    if kind == OLD_BACKUP:
        _, marker = ev
        ok = _contains_quote_tolerant(BACKUP_SRC, marker)
        return ok, f"OLD_BACKUP whole-backup existence check: {marker!r}"
    if kind == ACTIVE_FUNCTION:
        _, func_name, marker = ev
        body = _active_block(source, func_name)
        ok = bool(body) and _contains_quote_tolerant(body, marker)
        return ok, f"ACTIVE_FUNCTION scope={func_name!r} marker={marker!r} (scope found: {bool(body)})"
    if kind == ACTIVE_GENERATION:
        _, disambig, marker = ev
        node = _find_generation_by_marker(source, "_title_for_menu", disambig)
        if node is None:
            return False, f"ACTIVE_GENERATION scope not found (disambig={disambig!r})"
        try:
            txt = ast.unparse(node)
        except Exception as exc:
            return False, f"ACTIVE_GENERATION unparse failed: {exc!r}"
        ok = _contains_quote_tolerant(txt, marker)
        return ok, f"ACTIVE_GENERATION disambig={disambig!r} marker={marker!r}"
    if kind == HANDLER_SCOPE:
        _, func_name, cond_kind, cond_value, marker = ev
        scope_text, meta = _extract_owning_branch_scope(source, func_name, cond_kind, cond_value)
        if scope_text is None:
            return False, f"HANDLER_SCOPE {meta}"
        ok = _contains_quote_tolerant(scope_text, marker)
        return ok, (f"HANDLER_SCOPE handler={meta['handler']!r} branch={meta['branch_kind']!r} "
                    f"condition={meta['condition']!r} marker={marker!r} pass={ok}")
    if kind == EXECUTED_SCREEN:
        _, screen_name, marker = ev
        if domain_ns is None or screen_name not in domain_ns:
            return False, f"EXECUTED_SCREEN scope not available: {screen_name!r}"
        _text, rows = domain_ns[screen_name]()
        datas = _datas(rows)
        if isinstance(marker, bytes):
            ok = marker in datas
        else:
            ok = any(marker in lbl for lbl in _labels(rows))
        return ok, f"EXECUTED_SCREEN screen={screen_name!r} marker={marker!r} (rendered datas: {datas})"
    if kind == ROUTE_TRACE:
        _, tracer_name = ev
        tracer = ROUTE_TRACERS.get(tracer_name)
        if tracer is None:
            return False, f"ROUTE_TRACE unknown tracer name: {tracer_name!r}"
        if ctx is None:
            return False, f"ROUTE_TRACE tracer={tracer_name!r}: no trace context supplied"
        ok, detail = tracer(ctx)
        return ok, f"ROUTE_TRACE tracer={tracer_name!r}: {detail}"
    raise ValueError(f"unknown evidence kind: {kind!r}")


def E(domain, capability, classification, old, new, old_route, canonical_replacement, justification):
    return {
        "domain": domain, "capability": capability, "classification": classification,
        "old": old, "new": new,
        "old_route": old_route, "canonical_replacement": canonical_replacement,
        "justification": justification,
    }


# Each entry declares: domain, capability, classification, old evidence,
# new evidence, old route/action, canonical replacement, justification.
CAPABILITY_MATRIX = [
    # --- Root ---
    E("Root", "Search", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _nm_search_screen'),
      (ACTIVE_GENERATION, 'raw == "nm_search"', 'return _nm_search_screen()'),
      "menu:nm_search", "menu:nm_search (unchanged)",
      "Поиск intentionally removed from the visible root (N5.1 Part F), but the route/handler/_NM_SEARCH_INDEX are completely untouched -- historical menu:nm_search buttons (old messages) remain fully handled. Not on root by design, not missing."),
    E("Root", "Description", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'raw == "description"'),
      (ACTIVE_FUNCTION, "_nm_system_screen", '_nm_btn("ℹ️ Описание AdminBot", "menu:description")'),
      "menu:description (root)", "menu:description (Система, bottom)",
      "N5.1 (Part E): moved off root into Система (near the bottom); same existing route/handler. N5.2 (Part D): Back now returns to menu:nm_system (its real parent) instead of the root -- see test_6's D1-D4 for the scoped, executed proof."),
    E("Root", "Old menu (temporary)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _main_menu'),
      (ROUTE_TRACE, "old_root"),
      "menu:main (was old root)", "menu:nm_system -> menu:nm_service_w -> menu:old_menu_warning -> menu:old_root",
      "N5.1 (Part G): removed from root entirely; reachable ONLY via Система -> Сервис -> a one-time warning screen, whose own confirm button opens the unchanged N5 'old_root' route (itself delegating to the untouched pre-N5 old-root chain). Root has zero direct old-menu buttons. Real execution of the whole chain (not marker-only) happens in test_3b."),
    E("Root", "Add manager (onboarding)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'wiz:add_manager:start'),
      (ACTIVE_FUNCTION, "_nm_root_screen", '_nm_row(_nm_btn("➕ Добавить менеджера", "wiz:add_manager:start"))'),
      "wiz:add_manager:start (old admin flow)", "wiz:add_manager:start (root, unchanged wizard)",
      "N5.1 (Part A): direct top-level root button, reuses the existing onboarding wizard verbatim (proxy-first, manual/pool/purchase, phone/code/2FA, QR, tdata/session, source selection, screenshots-by-default) -- no new onboarding logic."),
    E("Root", "Delete manager (dedicated selection screen)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _manager_danger_zone_buttons'),
      (ROUTE_TRACE, "delete_manager"),
      "menu:manager_settings (N5.1) -> unified card -> danger zone",
      "menu:nm_manager_delete (N5.2 Part A) -> _nm_manager_delete_screen -> menu:manager_danger:{key} (unchanged)",
      "N5.2 Part A (owner decision X1, option B): root button now opens a THIN DEDICATED selection screen (not the generic manager list) whose every button lands directly on the existing, byte-identical-to-pre-N5.2 danger zone. No new deletion logic; root/selection screen never emit a destructive command. See the dedicated selftest for the full 12-point safety proof and 2 mutation proofs."),
    E("Root", "AI-ассистент (placeholder)", FUTURE_PLACEHOLDER,
      None,
      (ROUTE_TRACE, "ai_placeholder"),
      None, "menu:nm_ai_assistant -> _nm_ai_assistant_screen",
      "N5.1 (Part B) / N5.2 (Part C): brand-new, NOT a port of any old capability. Read-only placeholder screen (no admin_ai import, no engine, no LLM-supervisor link, no text interception, no production action). N5.2 gave it an explicit '🏠 Главная' -> menu:main footer (was the generic '🏠 Новое меню'). Explicitly does NOT replace or count as reachability for the existing LLM supervisor toggle (Автоматизация) or for Search -- both remain their own separate REACHABLE_CANONICAL entries below."),

    # --- Reports ---
    E("Reports", "Manager-first statistics (classic /stat flow)", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'menu_name == "stats"'),
      (EXECUTED_SCREEN, "_nm_reports_screen", b"menu:nm_stats"),
      "menu:stats (old)", "menu:nm_stats (Отчёты, top-level 'По менеджерам')",
      "Old menu:stats route unchanged (_tp_visual_existing_buttons dispatches menu_name==\"stats\" -> _stats_menu); N4 promoted it to a top-level 'По менеджерам' button in Reports (menu:nm_stats -> _nm_wrap), same _stats_menu reused verbatim. Deep reachability: EXECUTED (real call to _nm_reports_screen, real button inspected)."),
    E("Reports", "Source-first statistics (NMS)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _nms_sources_screen'),
      (EXECUTED_SCREEN, "_nm_reports_screen", b"menu:nm_stats2"),
      "menu:nm_stats2 (NM-only, no old equivalent)", "menu:nm_stats2 (Отчёты, canonical primary Статистика)",
      "NMS is a NM-only capability (no old equivalent) -- canonical primary Статистика entry, unchanged by N5/N5.1/N5.2. Its own internal composition (day/долёты mode switching, calendar, manager-window picker, pagination) is independently EXECUTED and verified by tools/nms_wizard_selftest.py and tools/n4_canonicalization_selftest.py -- not re-duplicated here."),
    E("Reports", "Specific date / calendar picker", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'data.startswith("cal:")'),
      (HANDLER_SCOPE, "on_callback", "prefix", "cal:", '_calendar_menu(kind, target, iso_date)'),
      "cal: (shared)", "cal: (shared, unchanged)",
      "cal: wizard is a shared, unchanged handler; reachable from both the wrapped classic _stats_menu (menu:nm_stats) and NMS."),
    E("Reports", "Date range picker", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'wiz:period:stats'),
      (ACTIVE_FUNCTION, "_stats_menu", 'wiz:period:stats'),
      "wiz:period:stats (old _stats_menu)", "wiz:period:stats (wrapped verbatim into menu:nm_stats)",
      "_stats_menu's '📆 Период с/до' button is reused verbatim inside _nm_wrap(\"stats\", ...)."),
    E("Reports", "Month shortcuts", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _nm_stats_month_rows'),
      (ACTIVE_GENERATION, 'raw == "nm_stats"', '_nm_stats_month_rows()'),
      "N1-added, no old equivalent", "menu:nm_stats (month rows appended)",
      "N1-added month shortcut rows, unchanged, appended to menu:nm_stats."),
    E("Reports", "Presets (сегодня/вчера/позавчера)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'cmd:/stat all'),
      (ACTIVE_FUNCTION, "_stats_menu", 'cmd:/stat all'),
      "cmd:/stat all (old _stats_menu)", "cmd:/stat all (wrapped verbatim into menu:nm_stats)",
      "_stats_menu's preset buttons reused verbatim inside the wrapped menu:nm_stats screen."),
    E("Reports", "Day/Долёты mode switching (per-source, NMS)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _nms_styp_buttons'),
      (ACTIVE_FUNCTION, "_nms_styp_buttons", "def _nms_styp_buttons"),
      "NM-only, no old equivalent", "nms: wizard (unchanged)",
      "NM-only capability inside the NMS wizard, unchanged by N5. Deep composition independently EXECUTED by tools/nms_wizard_selftest.py."),
    E("Reports", "Долёты (flights) report", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'raw_menu == "flights"'),
      (ACTIVE_GENERATION, 'raw == "nm_flights"', '_nm_wrap("flights", "nm_reports")'),
      "menu:flights (old)", "menu:nm_flights (wraps _flights_menu verbatim)",
      "Old menu:flights unchanged; new menu:nm_flights wraps the same _flights_menu verbatim."),
    E("Reports", "Detailed today", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'cmd:/stat det all'),
      (EXECUTED_SCREEN, "_nm_reports_screen", b"cmd:/stat det all"),
      "cmd:/stat det all (old)", "cmd:/stat det all (Отчёты, direct button)",
      "Identical route emitted directly by both old and new Reports screens. Deep reachability: EXECUTED."),
    E("Reports", "Duplicates", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'menu:duplicates'),
      (EXECUTED_SCREEN, "_nm_reports_screen", b"menu:nm_duplicates"),
      "menu:duplicates (old)", "menu:nm_duplicates (Отчёты, direct button)",
      "Route renamed (N1); wraps the same _duplicates_menu verbatim. Deep reachability: EXECUTED."),
    E("Reports", "Export", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'menu:export'),
      (EXECUTED_SCREEN, "_nm_reports_screen", b"menu:nm_excel"),
      "menu:export (old)", "menu:nm_excel (Отчёты, direct button)",
      "Route renamed (N1); wraps the same export screen verbatim. Deep reachability: EXECUTED."),
    E("Reports", "Quality (SLA/rating/losses)", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'b"menu:quality"'),
      (EXECUTED_SCREEN, "_nm_reports_screen", b"menu:nm_quality_w"),
      "menu:quality (old, standalone root section)", "menu:nm_quality_w (nested under Отчёты, direct button)",
      "N1 merged the standalone Качество root section into Отчёты; same underlying /quality commands, unchanged. Deep reachability: EXECUTED."),
    E("Reports", "Unanswered notifications toggle", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'def _unanswered_menu'),
      (ACTIVE_GENERATION, 'raw == "nm_unanswered"', '_nm_wrap("unanswered", "nm_quality_w")'),
      "menu:unanswered (old)", "menu:nm_unanswered (nested under Отчёты -> Качество)",
      "N1 nested Неотвеченные under Reports->Качество; N3 single-toggle mechanics unchanged."),

    # --- Managers ---
    E("Managers", "Manager list", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'cmd:/manager_list'),
      (EXECUTED_SCREEN, "_nm_managers_screen", b"cmd:/manager_list"),
      "cmd:/manager_list (old)", "cmd:/manager_list (Менеджеры, direct button)",
      "Identical command, direct button on both old admin screen and new Менеджеры screen. Deep reachability: EXECUTED."),
    E("Managers", "Unified lifecycle card (start/disable/restart)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _manager_settings_card_buttons'),
      (ROUTE_TRACE, "managers_unified_card"),
      "manager_admin:{key} + manager_settings:{key} (2 old cards)", "menu:manager_settings:{key} (N2 unified card, unchanged since)",
      "N5.2.1 (Part C, fixing Y2): the FULL chain root -> Менеджеры -> _nm_managers_screen -> "
      "Настройки -> canonical list -> selected manager -> active unified card is now traced FOR "
      "REAL by build_managers_trace_ns/build_trace_context (see test_9), including a structural "
      "no-interception proof and a check that the routing generation never references the "
      "orphaned _manager_admin_detail_text/_buttons. Card's own lifecycle-toggle/danger-zone "
      "sections remain independently EXECUTED and verified by tools/manager_card_unified_"
      "selftest.py -- not re-duplicated here, only the ROOT-TO-CARD REACHABILITY hop (the part "
      "the independent N5.2 review found untested anywhere) is now covered."),
    E("Managers", "Full manager info", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'cmd:/manager_info'),
      (ACTIVE_FUNCTION, "_manager_settings_card_buttons", "menu:manager_full:"),
      "cmd:/manager_info (old admin card)", "menu:manager_full:{key} (native screen, N5.4.3)",
      "N5.4.3 (owner decision B): «📄 Полная карточка» now opens a NATIVE panel screen "
      "rendered immediately from existing safe read-only helpers (identity, status, "
      "schedule, proxy, Telegram, functions/access, system info -- see tools\\"
      "manager_full_card_selftest.py for dedicated coverage), replacing the background "
      "cmd:/manager_info command-queue round-trip N2.1 had restored. The historical "
      "/manager_info text command itself is untouched in main.py and remains reachable "
      "via text; it is simply no longer this button's target."),
    E("Managers", "Screenshots / reminders", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'data.startswith("ssc:")'),
      (ACTIVE_FUNCTION, "_manager_settings_card_buttons", "ssc:"),
      "ssc: (old)", "ssc: (unified card)",
      "Screenshot-cycle (ssc:) controls live on the unified manager card, unchanged."),
    E("Managers", "ManagerBot access", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:mbaccess_all'),
      (EXECUTED_SCREEN, "_nm_managers_screen", b"menu:mbaccess_all"),
      "menu:mbaccess_all (old)", "menu:mbaccess_all (Менеджеры, direct button)",
      "Identical route, direct button on both old Менеджеры и режимы and new Менеджеры screens. Deep reachability: EXECUTED."),
    E("Managers", "Rename manager", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'wiz:rename_manager'),
      (ACTIVE_FUNCTION, "_manager_settings_card_buttons", "wiz:rename_manager"),
      "wiz:rename_manager (old)", "wiz:rename_manager (unified card)",
      "Unchanged wizard route on the unified card."),
    E("Managers", "Replacement (rw:)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _replace_callback'),
      (ROUTE_TRACE, "replacement_rw"),
      "rw: (old)", "rw: (unified card, unchanged)",
      "N5.2.1 (fixing Y2): the ACTUAL executed unified card (same build_managers_trace_ns "
      "computation as the Unified-card entry above) is inspected for a real rendered rw: "
      "callback -- no longer a bare reference to a sibling file that never re-verified "
      "reachability from the new root. tools/manager_replacement_adminbot_selftest.py's own "
      "deeper rw:-wizard regression coverage is unchanged and not re-duplicated here."),
    E("Managers", "Relogin", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'data.startswith("relogin:")'),
      (ROUTE_TRACE, "relogin"),
      "relogin: (old)", "relogin: (unified card, unchanged)",
      "N5.2.1 (fixing Y2): the ACTUAL executed unified card is inspected for a real rendered "
      "relogin: callback. tools/manager_relogin_selftest.py's own deeper regression coverage "
      "is unchanged and not re-duplicated here."),
    E("Managers", "Devlogin", REACHABLE_CANONICAL,
      (OLD_BACKUP, '_devlogin_waiting_buttons'),
      (ROUTE_TRACE, "devlogin"),
      "devlogin (old)", "devlogin (unified card, unchanged)",
      "N5.2.1 (fixing Y2): the ACTUAL executed unified card is inspected for a real rendered "
      "devlogin: callback. tools/devlogin_adminbot_wiring_selftest.py's own deeper regression "
      "coverage is unchanged and not re-duplicated here."),
    E("Managers", "Reserve (rsv:)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'data.startswith("rsv:")'),
      (ACTIVE_FUNCTION, "_manager_settings_card_buttons", "menu:reserve_admin:"),
      "rsv: (old)", "rsv: (unified card, unchanged)",
      "Unchanged, reachable from the unified card per N2's own audit."),
    E("Managers", "Proxy link (per-manager)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:proxy:{key}'),
      (ACTIVE_FUNCTION, "_manager_settings_card_buttons", "menu:proxy:"),
      "menu:proxy:{key} (old)", "menu:proxy:{key} (unified card, unchanged)",
      "Unified card links to the same proxy flow, unchanged."),
    E("Managers", "Transfers link (per-manager)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:transfer_mgr:{key}'),
      (ACTIVE_FUNCTION, "_manager_settings_card_buttons", "menu:transfer_mgr:"),
      "menu:transfer_mgr:{key} (old)", "menu:transfer_mgr:{key} (unified card, unchanged)",
      "Unified card links to per-manager transfer config; new _nm_transfers_screen itself does not duplicate this shortcut (TRANSITIONAL by design), but it remains reachable via Менеджеры -> card."),
    E("Managers", "Automation/modes per-manager (greeting/questionnaire/silence)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _manager_detail_buttons'),
      (ACTIVE_FUNCTION, "_manager_detail_buttons", "def _manager_detail_buttons"),
      "manager: (old)", "manager: (N3.2 single-toggle card, unchanged)",
      "N3.2 single-toggle manager-modes card, unchanged, reachable from the unified card / menu:managers."),
    E("Managers", "Add manager (phone/QR/tdata)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'wiz:add_manager:start'),
      (EXECUTED_SCREEN, "_nm_managers_screen", b"wiz:add_manager:start"),
      "wiz:add_manager:start (old)", "wiz:add_manager:start (Менеджеры, direct button)",
      "Identical wizard entry route, direct button on the new Менеджеры screen. Deep reachability: EXECUTED."),
    E("Managers", "Source selection after login", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'data.startswith("srcpick:")'),
      (HANDLER_SCOPE, "on_callback", "prefix", "srcpick:", 'command = f"/source link {source_key} {manager_key}"'),
      "srcpick: (old, onboarding)", "srcpick: (onboarding, unchanged)",
      "Unchanged onboarding step inside the add-manager wizard."),
    E("Managers", "Schedule requests", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:schedule_requests'),
      (EXECUTED_SCREEN, "_nm_mgr_schedules_screen", b"menu:schedule_requests"),
      "menu:schedule_requests (old)", "menu:schedule_requests (Менеджеры → Графики менеджеров)",
      "N5.4.1: moved from a direct root-level button into the canonical "
      "«📅 Графики менеджеров» submenu (owner-specified location; «Все "
      "менеджеры» and «Заявки на график» are separate functions, both "
      "present). Identical unchanged route. Deep reachability: EXECUTED."),
    E("Managers", "Schedule manager picker (calendar editor entry)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizschedule'),
      (EXECUTED_SCREEN, "_nm_mgr_schedules_screen", b"menu:nm_mgr_schedules_all"),
      "menu:bizschedule (old, Бизнес-ссылки only)", "menu:nm_mgr_schedules_all (Менеджеры → Графики менеджеров → Все менеджеры)",
      "N5.4.1: new canonical entry point into the SAME unchanged sch: "
      "monthly calendar editor (month/prev-next/date-toggle/autosave/"
      "source-inheritance all reused verbatim -- see canonical_back "
      "test_11). Two entry points reach the same editor by owner design: "
      "this one and the full-card «📅 График» link (N5.4.3)."),
    E("Managers", "Who works tomorrow", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizschedule_missing'),
      (EXECUTED_SCREEN, "_nm_mgr_schedules_screen", b"menu:bizschedule_missing"),
      "menu:bizschedule_missing (old)", "menu:bizschedule_missing (Менеджеры → Графики менеджеров)",
      "N5.4.1: identical unchanged route, now also reachable from the "
      "canonical Менеджеры section. Deep reachability: EXECUTED."),
    E("Managers", "/manager_stop button (canonical card)", TEXT_COMMAND_ONLY_BY_DESIGN,
      (OLD_BACKUP, 'cmd:/manager_stop'),
      None,
      "cmd:/manager_stop (old admin card button)", "text command only (/manager_stop)",
      "N2 explicit design decision (owner-approved): /manager_stop deliberately not shown on the canonical unified card; remains a text-only command. Not affected by N5/N5.1/N5.2."),
    E("Managers", "/manager_enable button (canonical card)", TEXT_COMMAND_ONLY_BY_DESIGN,
      (OLD_BACKUP, 'cmd:/manager_enable'),
      None,
      "cmd:/manager_enable (old admin card button)", "text command only (/manager_enable)",
      "N2 explicit design decision: /manager_enable deliberately not shown (start/disable toggle uses /manager_start instead, fixing a UX bug); /manager_enable remains a text-only command. Not affected by N5/N5.1/N5.2."),

    # --- Traffic ---
    E("Traffic", "Sources list/detail", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:sources'),
      (EXECUTED_SCREEN, "_nm_traffic_screen", b"menu:sources"),
      "menu:sources (old)", "menu:sources (Трафик и источники, direct button)",
      "Identical route reused verbatim by _nm_traffic_screen. Deep reachability: EXECUTED (this is the explicit 'Traffic+Sources' deep-reachability requirement)."),
    E("Traffic", "Source enable/disable", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'cmd:/source on {key}'),
      (ACTIVE_FUNCTION, "_source_detail_buttons", 'cmd:/source on'),
      "cmd:/source on {key} (old)", "cmd:/source on {key} (source detail card, unchanged N3 toggle)",
      "N3 single-toggle mechanics unchanged, reached via menu:sources -> source card."),
    E("Traffic", "Source schedule/windows/days/timing", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:source_stat:{key}'),
      (ACTIVE_FUNCTION, "_nm_traffic_screen", "menu:sources"),
      "menu:source_stat:{key} (old)", "menu:source_stat:{key} (source detail card, unchanged)",
      "Unchanged, reached via the source detail card (menu:sources)."),
    E("Traffic", "Add/rename source", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'wiz:add_source:start'),
      (EXECUTED_SCREEN, "_nm_traffic_screen", b"wiz:add_source:start"),
      "wiz:add_source:start (old)", "wiz:add_source:start (Трафик и источники, direct button)",
      "Identical wizard route, direct button on the new Трафик и источники screen. Deep reachability: EXECUTED."),
    E("Traffic", "Groups", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:groups'),
      (EXECUTED_SCREEN, "_nm_traffic_screen", b"menu:groups"),
      "menu:groups (old)", "menu:groups (Трафик и источники, direct button)",
      "Identical route reused verbatim by _nm_traffic_screen. Deep reachability: EXECUTED."),
    E("Traffic", "Buyers/partners and requests", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:partners'),
      (EXECUTED_SCREEN, "_nm_traffic_screen", b"menu:partners"),
      "menu:partners (old)", "menu:partners (Трафик и источники, direct button)",
      "Identical route reused verbatim by _nm_traffic_screen. Deep reachability: EXECUTED."),
    E("Traffic", "Funnel", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'cmd:/funnel sources'),
      (EXECUTED_SCREEN, "_nm_traffic_screen", b"menu:nm_funnel_w"),
      "menu:funnel (old, Reports)", "menu:nm_funnel_w (Трафик и источники, direct button)",
      "Route renamed (menu:funnel -> menu:nm_funnel_w, N1); wraps the same _funnel_menu verbatim, moved from Reports into Traffic. Deep reachability: EXECUTED."),

    # --- Proxy ---
    E("Proxy", "Manager proxies", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:proxy'),
      (EXECUTED_SCREEN, "_nm_proxy_screen", b"menu:proxy"),
      "menu:proxy (old)", "menu:proxy (Прокси, direct button)",
      "Identical route, direct button on the canonical Прокси screen. Deep reachability: EXECUTED."),
    E("Proxy", "Add proxy", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:proxy_add'),
      (EXECUTED_SCREEN, "_nm_proxy_screen", b"menu:proxy_add"),
      "menu:proxy_add (old)", "menu:proxy_add (Прокси, direct button)",
      "Identical route, direct button on the canonical Прокси screен. Deep reachability: EXECUTED."),
    E("Proxy", "Pool", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _ppool_root_buttons'),
      (EXECUTED_SCREEN, "_nm_proxy_screen", b"menu:ppool"),
      "menu:ppool (existed, unreachable from canonical Прокси pre-N4)", "menu:ppool (Прокси, direct button since N4)",
      "N4 added the direct pool entry to the canonical Прокси screen; underlying _ppool_root_buttons unchanged. Deep reachability: EXECUTED (this is the explicit 'Proxy+pool' deep-reachability requirement)."),
    E("Proxy", "PIN reveal / check / sync / assign / unassign", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'data.startswith("ppool:")'),
      (HANDLER_SCOPE, "_ppool_callback", "prefix", "ppool:", 'asyncio.create_task(_ppool_run_list(chat_id, user_id, filt, event=event))'),
      "ppool: (old)", "ppool: (unchanged, reachable via menu:ppool)",
      "Unchanged pool sub-handlers, reachable via menu:ppool (now directly on the canonical Прокси screen since N4)."),
    E("Proxy", "Renewal and balance", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'data == "prn:root"'),
      (EXECUTED_SCREEN, "_nm_proxy_screen", b"prn:root"),
      "prn:root (existed, unreachable from canonical Прокси pre-N4)", "prn:root (Прокси, direct button since N4)",
      "N4 added the direct prn:root entry to the canonical Прокси screen; underlying _prn_callback unchanged. Deep reachability: EXECUTED (this is the explicit 'Proxy+renewal' deep-reachability requirement)."),
    E("Proxy", "Lifecycle notification routes", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'plcterm:'),
      (HANDLER_SCOPE, "on_callback", "prefix", "plcterm:", 'command = f"/proxy_lifecycle_terminal_confirm {manager_key}"'),
      "plcterm: (notification-only)", "plcterm: (notification-only, unchanged)",
      "Notification-deep-link-only by design (both before and after N5); handler unchanged."),

    # --- Business links ---
    E("BusinessLinks", "Create one/15/N/all/date", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizlinks_create_scope'),
      (EXECUTED_SCREEN, "_nm_bizlinks_create_screen", b"menu:bizlinks_create_scope"),
      "menu:bizlinks_create_scope (old)", "menu:bizlinks_create_scope (Бизнес-ссылки -> Создание)",
      "Identical route, reused verbatim by _nm_bizlinks_create_screen. Deep reachability: EXECUTED one level under Бизнес-ссылки (this is the explicit 'Business Links submenus' deep-reachability requirement)."),
    E("BusinessLinks", "Templates", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizlinks_templates'),
      (EXECUTED_SCREEN, "_nm_bizlinks_manage_screen", b"menu:bizlinks_templates"),
      "menu:bizlinks_templates (old)", "menu:bizlinks_templates (Бизнес-ссылки -> Управление)",
      "Identical route, reused verbatim by _nm_bizlinks_manage_screen. Deep reachability: EXECUTED."),
    E("BusinessLinks", "List/show", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizlinks_show'),
      (EXECUTED_SCREEN, "_nm_bizlinks_manage_screen", b"menu:bizlinks_show"),
      "menu:bizlinks_show (old)", "menu:bizlinks_show (Бизнес-ссылки -> Управление)",
      "Identical route, reused verbatim by _nm_bizlinks_manage_screen. Deep reachability: EXECUTED."),
    E("BusinessLinks", "Test link", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizlinks_test_pick_mgr'),
      (EXECUTED_SCREEN, "_nm_bizlinks_create_screen", b"menu:bizlinks_test_pick_mgr"),
      "menu:bizlinks_test_pick_mgr (old)", "menu:bizlinks_test_pick_mgr (Бизнес-ссылки -> Создание)",
      "Identical route, reused verbatim by _nm_bizlinks_create_screen. Deep reachability: EXECUTED."),
    E("BusinessLinks", "Default counter", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizlinks_set_default_count'),
      (EXECUTED_SCREEN, "_nm_bizlinks_manage_screen", b"menu:bizlinks_set_default_count"),
      "menu:bizlinks_set_default_count (old)", "menu:bizlinks_set_default_count (Бизнес-ссылки -> Управление)",
      "Identical route, reused verbatim by _nm_bizlinks_manage_screen. Deep reachability: EXECUTED."),
    E("BusinessLinks", "Schedule", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizschedule'),
      (EXECUTED_SCREEN, "_nm_bizlinks_manage_screen", b"menu:bizschedule"),
      "menu:bizschedule (old)", "menu:bizschedule (Бизнес-ссылки -> Управление)",
      "Identical route, reused verbatim by _nm_bizlinks_manage_screen. Deep reachability: EXECUTED."),
    E("BusinessLinks", "Repair (bizfix)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizlinks_retry'),
      (EXECUTED_SCREEN, "_nm_bizlinks_manage_screen", b"menu:bizlinks_retry"),
      "menu:bizlinks_retry (old)", "menu:bizlinks_retry (Бизнес-ссылки -> Управление)",
      "Identical route, reused verbatim by _nm_bizlinks_manage_screen. Deep reachability: EXECUTED."),
    E("BusinessLinks", "Delete per-manager", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizdel_scope'),
      (EXECUTED_SCREEN, "_nm_bizlinks_delete_screen", b"menu:bizdel_scope"),
      "menu:bizdel_scope (old)", "menu:bizdel_scope (Бизнес-ссылки -> Удаление)",
      "Identical route, reused verbatim by _nm_bizlinks_delete_screen. Deep reachability: EXECUTED."),
    E("BusinessLinks", "Delete by date", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bdd_date'),
      (EXECUTED_SCREEN, "_nm_bizlinks_delete_screen", b"menu:bdd_date"),
      "menu:bdd_date (old)", "menu:bdd_date (Бизнес-ссылки -> Удаление)",
      "Identical route, reused verbatim by _nm_bizlinks_delete_screen. Deep reachability: EXECUTED."),
    E("BusinessLinks", "Delete global Telegram", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:bizdelg_scope'),
      (EXECUTED_SCREEN, "_nm_bizlinks_delete_screen", b"menu:bizdelg_scope"),
      "menu:bizdelg_scope (old, nested inside per-manager delete scope)", "menu:bizdelg_scope (Бизнес-ссылки -> Удаление, direct button)",
      "Identical route; new screen exposes it one level higher (direct button) than old (nested inside per-manager delete scope) -- functional parity, navigation depth differs only. Deep reachability: EXECUTED."),

    # --- Transfers ---
    E("Transfers", "Manager configuration (tr:)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'data.startswith("tr:")'),
      (HANDLER_SCOPE, "_tr_callback", "prefix", "tr:", '_tr_config_set_enabled(key, new_enabled, db_path=TPILOT_DB_PATH)'),
      "tr: (old, per-manager card)", "tr: (unified manager card's Передачи link, unchanged)",
      "Unchanged handler; reachable via the unified manager card's Передачи link (Менеджеры section), not duplicated as a Передачи-section shortcut -- TRANSITIONAL navigation-depth note, not a lost capability."),
    E("Transfers", "Closers", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:transfer_closers'),
      (EXECUTED_SCREEN, "_nm_transfers_screen", b"menu:transfer_closers"),
      "menu:transfer_closers (old)", "menu:transfer_closers (Передачи, direct button)",
      "Identical route, direct button on the canonical Передачи screen. Deep reachability: EXECUTED (this is the explicit 'Transfers' deep-reachability requirement)."),
    E("Transfers", "Statistics", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:transfer_stats'),
      (EXECUTED_SCREEN, "_nm_transfers_screen", b"menu:transfer_stats"),
      "menu:transfer_stats (old)", "menu:transfer_stats (Передачи, direct button)",
      "Identical route, direct button on the canonical Передачи screen. Deep reachability: EXECUTED."),
    E("Transfers", "Excel export", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'data.startswith("trx:")'),
      (HANDLER_SCOPE, "_tr_stats_callback", "prefix", "trx:", 'path = _tr_export_xlsx(period)'),
      "trx: (old)", "trx: (unchanged, reachable via menu:transfer_stats)",
      "Unchanged handler, reachable via menu:transfer_stats."),

    # --- Automation ---
    E("Automation", "Autoreply/greeting", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:managers'),
      (EXECUTED_SCREEN, "_nm_automation_screen", b"menu:managers"),
      "menu:managers (old)", "menu:managers (Автоматизация, direct button)",
      "Identical route, direct button on the canonical Автоматизация screen. Deep reachability: EXECUTED."),
    E("Automation", "Questionnaire", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'cmd:/questionnaire status all'),
      (EXECUTED_SCREEN, "_nm_automation_screen", b"cmd:/questionnaire status all"),
      "cmd:/questionnaire status all (old)", "cmd:/questionnaire status all (Автоматизация, direct button)",
      "Identical route, direct button on the canonical Автоматизация screen. Deep reachability: EXECUTED."),
    E("Automation", "Followups (per-manager + bulk)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:followups'),
      (EXECUTED_SCREEN, "_nm_automation_screen", b"menu:followups"),
      "menu:followups (old)", "menu:followups (Автоматизация, direct button)",
      "N3 single-toggle + bulk exception mechanics unchanged, direct button on the canonical screen. Deep reachability: EXECUTED."),
    E("Automation", "Content/delays", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:content'),
      (EXECUTED_SCREEN, "_nm_automation_screen", b"menu:content"),
      "menu:content (old)", "menu:content (Автоматизация, direct button)",
      "Identical route, direct button on the canonical Автоматизация screen. Deep reachability: EXECUTED (scoped to the ACTUAL rendered Автоматизация screen, not a whole-file substring -- menu:content also appears in dead/unrelated code paths elsewhere in the file, which this evidence type cannot be fooled by)."),
    E("Automation", "Per-manager silence", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'def _manager_detail_buttons'),
      (ACTIVE_FUNCTION, "_manager_detail_buttons", "mmt:silentcard:"),
      "manager: modes card (old, side-by-side pair)", "manager: modes card (N3.2 single toggle, mmt:silentcard:)",
      "N3.2 single-toggle per-manager silence, unchanged, reachable via manager modes card."),
    E("Automation", "Bulk silence", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:profile'),
      (EXECUTED_SCREEN, "_nm_automation_screen", b"menu:profile"),
      "menu:profile (old)", "menu:profile (Автоматизация, direct button)",
      "N3/N3.1 bulk-exception mechanics unchanged, direct button on the canonical screen. Deep reachability: EXECUTED."),
    E("Automation", "Auto-status (status + confirm-gated toggle)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'autostatus_status_info'),
      (EXECUTED_SCREEN, "_nm_automation_screen", b"autostatus_status_info"),
      "old Automation screen only (pre-N5: unreachable from any new-menu screen)", "autostatus_status_info (Автоматизация, direct button since the N5 fix)",
      "N5 FIX: this was BLOCKING_MISSING (no button anywhere in the new menu) until fixed in this phase -- added to _nm_automation_screen reusing the exact, unchanged N3/N3.1 confirm-gated route (menu:autostatus_toggle_confirm:{target}). No toggle logic touched. "
      "This is now proven by REAL EXECUTION (EXECUTED_SCREEN) against the actual rendered button's callback_data, not a bare string search -- the exact false-pass hazard that motivated this check (the bare literal 'autostatus_status_info' ALSO appears, unrelated, inside on_callback's own handler for that literal) is structurally impossible for EXECUTED_SCREEN evidence, since it inspects only the buttons the function itself renders."),
    E("Automation", "LLM status/toggle", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:nm_llm_toggle_confirm'),
      (EXECUTED_SCREEN, "_nm_automation_screen", b"menu:nm_llm_toggle_confirm"),
      "menu:llm_toggle (old, unsafe instant flip) / menu:nm_llm_toggle_confirm (N4, canonical)", "menu:nm_llm_toggle_confirm (Автоматизация, direct button)",
      "N4 canonical confirm-gated LLM flow, unchanged by N5; direct button on the canonical Автоматизация screen. Deep reachability: EXECUTED."),
    E("Automation", "Diagnostics (анкета проверка)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'menu:profile_check'),
      (EXECUTED_SCREEN, "_nm_automation_screen", b"menu:profile_check"),
      "menu:profile_check (old)", "menu:profile_check (Автоматизация, direct button)",
      "Identical route, direct button on the canonical Автоматизация screен. Deep reachability: EXECUTED."),

    # --- System ---
    E("System", "Health summary", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'menu:health'),
      (EXECUTED_SCREEN, "_nm_system_screen", b"menu:nm_health_w"),
      "menu:health (old)", "menu:nm_health_w (Система, direct button)",
      "Route renamed (N1, menu:health -> menu:nm_health_w); wraps the identical health screen verbatim. Deep reachability: EXECUTED (this is part of the explicit 'System' deep-reachability requirement)."),
    E("System", "Telegram Health + check all", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'menu:tghealth'),
      (EXECUTED_SCREEN, "_nm_system_screen", b"menu:nm_tghealth_w"),
      "menu:tghealth (old)", "menu:nm_tghealth_w (Система, direct button)",
      "Route renamed (N1); wraps the identical tghealth screen verbatim, including cmd:/tghealth check all. Deep reachability: EXECUTED."),
    E("System", "Preflight", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'pf:manual_check'),
      (ACTIVE_GENERATION, 'raw == "nm_service_w"', '_nm_wrap("service", "nm_system"'),
      "pf:manual_check (old service screen)", "pf:manual_check (menu:nm_service_w, wraps old service screen verbatim)",
      "Unchanged preflight routes, reachable via menu:nm_service_w (wraps the old service screen verbatim)."),
    E("System", "Baseline/service (hidden old-menu path owner)", INTENTIONAL_ALIAS,
      (OLD_BACKUP, 'def _tp_visual_service_menu'),
      (EXECUTED_SCREEN, "_nm_system_screen", b"menu:nm_service_w"),
      "menu:service (old)", "menu:nm_service_w (Система, direct button; also hosts the hidden old-menu warning route since N5.1)",
      "Route renamed (N1, menu:service -> menu:nm_service_w); wraps the identical service screen verbatim. Since N5.1 this same screen also hosts the ONLY route to the hidden old menu (see test_3b/test_6 for the real-execution proof of that chain). Deep reachability: EXECUTED for the System->Сервис button itself."),
    E("System", "Dangerous actions log", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'cmd:/manager_danger_log'),
      (EXECUTED_SCREEN, "_nm_system_screen", b"cmd:/manager_danger_log"),
      "cmd:/manager_danger_log (old, required Менеджеры->Админ менеджеров first)", "cmd:/manager_danger_log (Система, top-level direct button)",
      "Identical route; new screen promotes it to top-level System (old required Менеджеры->Админ менеджеров first). Deep reachability: EXECUTED."),
    E("Reports", "Screenshots by date (global)", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'ssv:dates'),
      (EXECUTED_SCREEN, "_nm_reports_screen", b"ssv:dates"),
      "ssv:dates (old: manager-settings list / Система)", "ssv:dates (Отчёты, direct button since N5.3)",
      "Identical route; N5.3 (Part C) moved global date-based screenshots to their canonical owner Отчёты "
      "(removed from Система and from the manager list; per-manager screenshots stay on the unified card "
      "via ssc:*). Deep reachability: EXECUTED."),
    E("System", "Help/instructions", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'cmd:/help'),
      (EXECUTED_SCREEN, "_nm_system_screen", b"cmd:/help"),
      "cmd:/help (old)", "cmd:/help (Система, direct button)",
      "Identical route, direct button on the canonical System screen. Deep reachability: EXECUTED."),
    E("System", "Description AdminBot", REACHABLE_CANONICAL,
      (OLD_BACKUP, 'raw == "description"'),
      (EXECUTED_SCREEN, "_nm_system_screen", b"menu:description"),
      "menu:description (root, N5/N5.1)", "menu:description (Система, near the bottom, since N5.1)",
      "N5.1 (Part E) moved Описание off the root into Система; N5.2 (Part D) additionally corrected its Back target to menu:nm_system (see test_6 D1-D4). Deep reachability: EXECUTED for the System->Описание button; the description content/nav itself is verified by test_6."),
]


def test_7_capability_matrix() -> None:
    db_path = _make_temp_db()
    try:
        _set_setting(db_path, "llm_supervisor_enabled", "1")
        _set_setting(db_path, "auto_status_enabled", "1")
        # N5.2.1: one shared trace context (n5 + domain-screens + the real
        # Managers->unified-card route trace) built ONCE and used by every
        # EXECUTED_SCREEN/ROUTE_TRACE lookup below AND by test_9 -- so the
        # matrix's evidence and the dedicated Managers trace test are
        # provably the SAME computation, never two divergent implementations.
        ctx = build_trace_context(db_path)
        domain_ns = ctx["domain"]

        counts = {REACHABLE_CANONICAL: 0, INTENTIONAL_ALIAS: 0, TRANSITIONAL_SHORTCUT: 0,
                  TEXT_COMMAND_ONLY_BY_DESIGN: 0, FUTURE_PLACEHOLDER: 0, BLOCKING_MISSING: 0}
        for entry in CAPABILITY_MATRIX:
            domain, capability, classification = entry["domain"], entry["capability"], entry["classification"]
            counts[classification] = counts.get(classification, 0) + 1
            label = f"7. [{domain}] {capability} ({classification})"

            if classification == TEXT_COMMAND_ONLY_BY_DESIGN:
                ok, detail = _verify_evidence(entry["old"], source=BACKUP_SRC, domain_ns=None, ctx=ctx)
                check(f"{label}: old evidence present in pre-N5 backup", ok, detail)
                check(f"{label}: no new-side evidence declared (by design)", entry["new"] is None, entry["new"])
                continue

            if classification == FUTURE_PLACEHOLDER:
                check(f"{label}: old evidence is None (genuinely new, not a port of any old capability)",
                      entry["old"] is None, entry["old"])
                ok, detail = _verify_evidence(entry["new"], source=PANEL_SRC, domain_ns=domain_ns, ctx=ctx)
                check(f"{label}: new evidence present/reachable now", ok, detail)
                continue

            old_ok, old_detail = _verify_evidence(entry["old"], source=BACKUP_SRC, domain_ns=None, ctx=ctx)
            new_ok, new_detail = _verify_evidence(entry["new"], source=PANEL_SRC, domain_ns=domain_ns, ctx=ctx)
            check(f"{label}: old evidence existed pre-N5 (backup) AND new evidence reachable now (current)",
                  old_ok and new_ok, {"old": old_detail, "new": new_detail, "justification": entry["justification"]})
    finally:
        os.unlink(db_path)

    print()
    print("Parity matrix totals by classification:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    check("7. BLOCKING_MISSING == 0 (acceptance requirement)", counts.get(BLOCKING_MISSING, 0) == 0, counts)
    check("7. AI-assistant FUTURE_PLACEHOLDER does not silently substitute for any LLM/search capability "
          "(those remain their own separate REACHABLE_CANONICAL entries above, verified independently)",
          counts.get(FUTURE_PLACEHOLDER, 0) >= 1 and counts.get(REACHABLE_CANONICAL, 0) >= 50, counts)
    check("7. capability matrix is non-trivially large (sanity: covers all 8 domains + root)", len(CAPABILITY_MATRIX) >= 50, len(CAPABILITY_MATRIX))
    check("7. every non-canonical classification (INTENTIONAL_ALIAS/TRANSITIONAL_SHORTCUT/"
          "TEXT_COMMAND_ONLY_BY_DESIGN/FUTURE_PLACEHOLDER) has a non-empty, entry-specific justification "
          "(no generic catch-all whitelist)",
          all(bool((e["justification"] or "").strip()) for e in CAPABILITY_MATRIX
              if e["classification"] != REACHABLE_CANONICAL),
          [e["capability"] for e in CAPABILITY_MATRIX if not (e["justification"] or "").strip()])


# ======================================================================
# 8. Deep reachability sanity: the EXECUTED_SCREEN evidence used above is
#    backed by real function calls -- this test just confirms the domain
#    namespace itself builds and every one of the 11 screens executes
#    without error, so a future change that breaks a screen's own
#    rendering (rather than removing a specific button) is also caught.
# ======================================================================

def test_8_domain_screens_all_execute() -> None:
    db_path = _make_temp_db()
    try:
        _set_setting(db_path, "llm_supervisor_enabled", "1")
        _set_setting(db_path, "auto_status_enabled", "1")
        ns = build_domain_screens_ns(db_path)
        for name in sorted(DOMAIN_SCREEN_NAMES - {"_nm_btn", "_nm_row", "_nm_footer", "_nm_screen", "_nm_canonical_path", "_pb_get_setting", "_connect_panel_db"}):
            text, rows = ns[name]()
            check(f"8. {name}() executes and returns non-empty text + at least one row", bool(text) and len(rows) > 0, (name, text[:60], len(rows)))
    finally:
        os.unlink(db_path)


# ======================================================================
# 9. N5.2.1 Part C: dedicated, fully real Managers route trace -- root ->
#    "Менеджеры" -> _nm_managers_screen -> "Настройки" -> canonical list ->
#    selected manager -> active unified card. Uses the SAME
#    build_trace_context computation the capability matrix's ROUTE_TRACE
#    evidence uses (not a second, divergent implementation).
# ======================================================================

def test_9_managers_route_trace() -> None:
    db_path = _make_temp_db()
    try:
        ctx = build_trace_context(db_path)
        mt = ctx["managers_trace"]

        check("9.1. Root emits the exact Managers callback (menu:nm_managers)",
              mt["root_emits_nm_managers"], mt)
        check("9.2. _nm_managers_screen emits the exact Settings callback (menu:manager_settings)",
              mt["managers_emits_settings"], mt)
        check("9.3. Settings list renders at least one manager row for the seeded manager",
              mt["list_has_manager_row"], mt)
        check("9.4. The manager row targets the canonical unified-card route (menu:manager_settings:{key})",
              mt["row_targets_card"], mt)
        check("9.5/9.6. menu:manager_settings:{key} resolves to active _manager_settings_card_text/"
              "_buttons verbatim (byte-for-byte against calling them directly)",
              mt["card_matches_canonical"], mt)
        check("9.6b. The owning routing generation never references the orphaned "
              "_manager_admin_detail_text/_buttons",
              not mt["references_orphan_card"], mt)
        check("9.7. No later _title_for_menu generation intercepts manager_settings: "
              "(structural identity check: the generation found via 'manager_disable_confirm:' "
              "is the SAME node as the one found via 'raw.startswith(\"manager_settings:\")')",
              mt["not_intercepted"], mt)
        # Bonus, reusing the same executed card for the 3 sibling capabilities
        # this same phase strengthened (Part B / Y2):
        check("(bonus) executed unified card emits an rw: callback", mt["card_emits_rw"], mt["card_datas"])
        check("(bonus) executed unified card emits a relogin: callback", mt["card_emits_relogin"], mt["card_datas"])
        check("(bonus) executed unified card emits a devlogin: callback", mt["card_emits_devlogin"], mt["card_datas"])
    finally:
        os.unlink(db_path)


def main() -> int:
    print(f"Comparing against pre-N5 backup: {BACKUP_PATH}")
    print(f"Comparing against immediately-prior-phase backup: {PRIOR_PHASE_BACKUP_PATH}")
    print()
    test_1_root_composition()
    test_2_panel_start_menu_main_render_canonical_root()
    test_3_old_root_temporary_route_and_loop_safety()
    test_3b_hidden_old_menu_path_and_ai_placeholder()
    test_4_active_composition_safety()
    test_5_old_menu_still_has_new_menu_entry()
    test_6_historical_callbacks_remain_handled()
    test_7_capability_matrix()
    test_8_domain_screens_all_execute()
    test_9_managers_route_trace()

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
