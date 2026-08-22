# -*- coding: utf-8 -*-
"""tools/identity_sync_ira_selftest.py -- offline regression test for the
"TPILOT IDENTITY RECONCILE 20260809" patch (plan Ф2 / H7): the periodic
Telegram identity reconcile (_identity_reconcile_from_me, main.py) that
refreshes a manager's Telegram identity in the CENTRAL managers row on every
20-min health self-check, not just once at process start.

Named after the confirmed production case: manager_key=ira had a working
Telethon session that already knew tg_user_id=7267762183 / first_name='Ира' /
username='ira_tg_t', while the central `managers` row was stuck with
display_name='Ira', first_name='pNpLqhW', telegram_username='' -- because
identity was only ever synced once, at manager-runtime process start, and
never again for the lifetime of the process.

Technique (same convention as every other tools/*_selftest.py in this
project, e.g. manager_runtime_start_selftest.py): main.py and storage.py both
have module-scope side effects (Telethon client construction / real DB
paths) that make them unsafe to import directly, so the two functions under
test are extracted via ast.parse + ast.unparse + exec() instead.
registry_normalize_manager_key / get_manager_row_from_db_sync
(manager_registry.py -- pure functions, no side effects at import) and
aiosqlite / _manager_table_ready / _now_iso (storage.py's own real
dependencies) are bound for REAL against a temp SQLite DB -- never faked,
matching manager_runtime_start_selftest.py's own convention of exercising
real DB primitives rather than mocking them.

Mutation proofs (structural AST transforms applied to a freshly-parsed,
independent copy of the node -- the real .py files on disk are never
touched) prove each assertion below is actually falsifiable, not a
tautology:
  - MUT-A: strip the ` WHERE manager_key=?` clause + its parameter binding
    from manager_sync_telegram_profile_in_db -- the "other manager
    untouched" assertions (scenario 7/8) must flip to FAIL against this
    mutant (an UPDATE with no WHERE clause touches every row).
  - MUT-B: force `if current_source != 'manual':` to always be True in
    manager_sync_telegram_profile_in_db -- the "manual display_name
    preserved" assertion (scenario 5) must flip to FAIL against this
    mutant.
  - MUT-C: force the tg_user_id-mismatch guard in
    _identity_reconcile_from_me to never trigger -- the "mismatch causes no
    write" assertion (scenario 7) must flip to FAIL against this mutant.
  - MUT-D: force the no-op guard in _identity_reconcile_from_me to never
    trigger -- the "noop causes no write / no updated_at bump" assertion
    (scenario 8) must flip to FAIL against this mutant.

Never: real Telegram network, real filesystem writes outside a temp dir,
production DB/runtime/session/log access.

    python tools\\identity_sync_ira_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import copy
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
STORAGE_PATH = str(BASE_DIR / "storage.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()
STORAGE_SRC = open(STORAGE_PATH, encoding="utf-8-sig").read()


def _find_func_node(src: str, name: str):
    """Returns the LAST top-level (Async)FunctionDef named `name` in `src` --
    "last wins" matches this project's own override-stacking convention
    (see CLAUDE.md); both functions this file extracts currently have
    exactly one top-level definition each. Re-parses `src` fresh on every
    call, so callers always get an independent, unmutated tree."""
    tree = ast.parse(src)
    found = None
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            found = n
    if found is None:
        raise AssertionError(f"{name!r} not found as a top-level def in source")
    return found


def _unparse(node) -> str:
    node = copy.deepcopy(node)
    ast.fix_missing_locations(node)
    return ast.unparse(node)


class _IfGuardKiller(ast.NodeTransformer):
    """Finds the first `if` statement (AST visitation order) whose unparsed
    test condition contains `needle`, and replaces its test with the constant
    `replacement` -- neutralizing that guard. Structural (operates on AST
    nodes, not text), so it is immune to ast.unparse formatting differences.
    Raises via the caller's `hit` check if no matching `if` is found, so a
    future rewording of the guard doesn't silently produce a no-op mutant."""

    def __init__(self, needle: str, replacement: bool):
        self.needle = needle
        self.replacement = replacement
        self.hit = False

    def visit_If(self, node):
        self.generic_visit(node)
        if not self.hit and self.needle in ast.unparse(node.test):
            node.test = ast.Constant(value=self.replacement)
            self.hit = True
        return node


class _WhereScopeStripper(ast.NodeTransformer):
    """Structural mutant of manager_sync_telegram_profile_in_db: removes the
    ' WHERE manager_key=?' suffix from the UPDATE statement's sql string
    AND the matching `vals.append(key)` parameter binding -- simulating a
    lost manager_key scope (an UPDATE that would touch every row instead of
    one). Operates on AST nodes, not text."""

    def __init__(self):
        self.hit_sql = False
        self.hit_append = False

    def visit_Assign(self, node):
        self.generic_visit(node)
        if (
            len(node.targets) == 1 and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "sql" and isinstance(node.value, ast.BinOp)
            and isinstance(node.value.right, ast.Constant) and isinstance(node.value.right.value, str)
            and "WHERE manager_key" in node.value.right.value
        ):
            node.value = node.value.left  # drop " + \" WHERE manager_key=?\""
            self.hit_sql = True
        return node

    def visit_Expr(self, node):
        self.generic_visit(node)
        v = node.value
        if (
            isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute) and v.func.attr == "append"
            and isinstance(v.func.value, ast.Name) and v.func.value.id == "vals"
            and len(v.args) == 1 and isinstance(v.args[0], ast.Name) and v.args[0].id == "key"
        ):
            self.hit_append = True
            return None  # NodeTransformer contract: None deletes this stmt from its body list
        return node


def _selftest_db_guard(db_path: str) -> None:
    """Mandatory production-path DB guard, same convention as every other
    tools/*_selftest.py in this project: refuses any db_path under
    base_dir/db."""
    prod_db_dir = str((BASE_DIR / "db").resolve())
    target = str(Path(db_path).resolve())
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"


def build_reconcile_fn(*, sync_fn, mutate_if_needle: Optional[str] = None, mutate_replacement: Optional[bool] = None):
    """Extracts _identity_reconcile_from_me from main.py, optionally
    mutating ONE named `if` guard, and returns the exec'd coroutine function
    bound to registry_normalize_manager_key / get_manager_row_from_db_sync
    (real, manager_registry.py) and manager_sync_telegram_profile_in_db
    (whichever `sync_fn` the caller passes -- real or a storage-level
    mutant)."""
    import manager_registry

    node = _find_func_node(MAIN_SRC, "_identity_reconcile_from_me")
    if mutate_if_needle is not None:
        killer = _IfGuardKiller(mutate_if_needle, mutate_replacement)
        node = killer.visit(node)
        if not killer.hit:
            raise AssertionError(f"mutation anchor not found: {mutate_if_needle!r}")
    src = _unparse(node)
    ns: Dict[str, Any] = {
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "get_manager_row_from_db_sync": manager_registry.get_manager_row_from_db_sync,
        "manager_sync_telegram_profile_in_db": sync_fn,
        "print": print,
        "Dict": Dict,
        "Any": Any,
    }
    exec(compile(src, f"<{MAIN_PATH}:_identity_reconcile_from_me>", "exec"), ns)
    return ns["_identity_reconcile_from_me"]


def build_sync_fn(*, mutate: Optional[str] = None):
    """Extracts manager_sync_telegram_profile_in_db from storage.py,
    optionally applying a named structural mutation
    ("strip_where" | "disable_manual_guard"), and returns the exec'd
    coroutine function bound to REAL aiosqlite / _manager_table_ready /
    _now_iso (storage.py's own real dependencies -- never faked)."""
    import aiosqlite as _aiosqlite
    import storage as _storage

    node = _find_func_node(STORAGE_SRC, "manager_sync_telegram_profile_in_db")
    if mutate == "strip_where":
        stripper = _WhereScopeStripper()
        node = stripper.visit(node)
        if not (stripper.hit_sql and stripper.hit_append):
            raise AssertionError(
                f"WHERE-scope mutation anchors not found (sql={stripper.hit_sql} append={stripper.hit_append})"
            )
    elif mutate == "disable_manual_guard":
        killer = _IfGuardKiller("!= 'manual'", True)
        node = killer.visit(node)
        if not killer.hit:
            raise AssertionError("manual-guard mutation anchor not found")
    elif mutate is not None:
        raise AssertionError(f"unknown mutation: {mutate!r}")
    src = _unparse(node)
    ns: Dict[str, Any] = {
        "aiosqlite": _aiosqlite,
        "_manager_table_ready": _storage._manager_table_ready,
        "_now_iso": _storage._now_iso,
        # STAGE 3: manager_sync_telegram_profile_in_db was later refactored to open its
        # connection through storage._db_conn() instead of aiosqlite.connect() directly.
        # It is a REAL storage.py dependency, bound here exactly like the two above --
        # never faked, so the extracted function still exercises the production
        # connection/PRAGMA path (and still honours db_path, which the tests assert).
        "_db_conn": _storage._db_conn,
        "Dict": Dict,
        "Any": Any,
        "Optional": Optional,
    }
    exec(compile(src, f"<{STORAGE_PATH}:manager_sync_telegram_profile_in_db>", "exec"), ns)
    return ns["manager_sync_telegram_profile_in_db"]


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="idsync_ira_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


async def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


class FakeMe:
    def __init__(self, *, id, username, first_name, last_name):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name


ME_IRA = FakeMe(id=7267762183, username="ira_tg_t", first_name="Ира", last_name="")


def _counting_wrapper(real_fn, counter: list):
    """F2 CHECKPOINT (2026-08-09): a deterministic call-counter around the REAL
    canonical write function, so "zero writes" claims (mismatch / noop) rest on an
    exact invocation count rather than on updated_at, whose 1-second resolution
    (storage.py's _now_iso truncates microseconds) can produce a false negative --
    two calls inside the same wall-clock second look identical even if a real write
    happened. counter[0] is incremented on every call, BEFORE delegating to the real
    function, so a call that raises still counts (a write attempt, not just a
    successful one)."""
    async def wrapped(*args, **kwargs):
        counter[0] += 1
        return await real_fn(*args, **kwargs)
    return wrapped


async def seed_env(*, ira_source: str):
    """Fresh temp DB with two managers: `ira` (the regression case, seeded
    with the exact confirmed-stale production values) and a control manager
    `other`, used throughout to prove no cross-manager write ever happens."""
    import storage as _storage

    tmp_root, db_path = make_temp_env()
    _selftest_db_guard(db_path)
    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path

    await _storage.manager_add(
        manager_key="ira", display_name="Ira", phone="", status="active",
        session_path="", db_path=db_path, workdir="", log_path="",
    )
    await _storage.manager_set_fields(
        "ira", tg_user_id=7267762183, telegram_username="", first_name="pNpLqhW",
        last_name="", display_name_source=ira_source,
    )
    await _storage.manager_add(
        manager_key="other", display_name="OtherName", phone="", status="active",
        session_path="", db_path=db_path, workdir="", log_path="",
    )
    await _storage.manager_set_fields(
        "other", tg_user_id=555000111, telegram_username="otherguy", first_name="Other",
        last_name="Guy", display_name_source="auto",
    )
    return tmp_root, db_path, _storage


# ======================================================================
# Scenarios 1-4: display_name_source='auto' -- full happy-path sync.
# ======================================================================

async def test_1_auto_source_full_sync():
    print("\n-- Scenarios 1-4: display_name_source='auto', full sync --")
    tmp_root, db_path, storage_mod = await seed_env(ira_source="auto")
    try:
        sync_fn = storage_mod.manager_sync_telegram_profile_in_db
        reconcile = build_reconcile_fn(sync_fn=sync_fn)
        before = await storage_mod.manager_get("ira")

        result = await reconcile(ME_IRA, manager_key="ira", db_path=db_path)
        after = await storage_mod.manager_get("ira")

        check("1. action == synced", result.get("action") == "synced", result)
        check("2. username -> ira_tg_t", after.get("telegram_username") == "ira_tg_t", after)
        check("3. first_name -> Ира", after.get("first_name") == "Ира", after)
        check("4. last_name correct (empty)", (after.get("last_name") or "") == "", after)
        check(
            "5. display_name_source='auto' -> display_name updates to Ира",
            after.get("display_name") == "Ира" and after.get("display_name_source") == "auto",
            after,
        )
        check(
            "6. updated_at changed", after.get("updated_at") != before.get("updated_at"),
            (before.get("updated_at"), after.get("updated_at")),
        )
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# Scenario 5: display_name_source='manual' -- display_name untouched,
# username/first/last still update.
# ======================================================================

async def test_5_manual_source_display_name_preserved():
    print("\n-- Scenario 5: display_name_source='manual' --")
    tmp_root, db_path, storage_mod = await seed_env(ira_source="manual")
    try:
        sync_fn = storage_mod.manager_sync_telegram_profile_in_db
        reconcile = build_reconcile_fn(sync_fn=sync_fn)

        result = await reconcile(ME_IRA, manager_key="ira", db_path=db_path)
        after = await storage_mod.manager_get("ira")

        check("7. action == synced", result.get("action") == "synced", result)
        check("8. display_name NOT changed (still 'Ira')", after.get("display_name") == "Ira", after)
        check("9. display_name_source stays 'manual'", after.get("display_name_source") == "manual", after)
        check("10. username still updates to ira_tg_t", after.get("telegram_username") == "ira_tg_t", after)
        check("11. first_name still updates to Ира", after.get("first_name") == "Ира", after)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# Scenario 6: stale username cleared when the live username is empty/None.
# ======================================================================

async def test_6_empty_live_username_clears_stale():
    print("\n-- Scenario 6: empty live username clears a stale stored username --")
    tmp_root, db_path, storage_mod = await seed_env(ira_source="auto")
    try:
        await storage_mod.manager_set_fields("ira", telegram_username="old_stale_name")
        sync_fn = storage_mod.manager_sync_telegram_profile_in_db
        reconcile = build_reconcile_fn(sync_fn=sync_fn)

        me_no_username = FakeMe(id=7267762183, username=None, first_name="Ира", last_name="")
        result = await reconcile(me_no_username, manager_key="ira", db_path=db_path)
        after = await storage_mod.manager_get("ira")

        check("12. action == synced", result.get("action") == "synced", result)
        check("13. stale username cleared to ''", (after.get("telegram_username") or "") == "", after)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# Scenario 7: tg_user_id mismatch -- no write, action identity_mismatch,
# `other` manager untouched.
# ======================================================================

async def test_7_identity_mismatch_no_write():
    print("\n-- Scenario 7: stored tg_user_id != live id --")
    tmp_root, db_path, storage_mod = await seed_env(ira_source="auto")
    try:
        write_calls = [0]
        sync_fn = _counting_wrapper(storage_mod.manager_sync_telegram_profile_in_db, write_calls)
        reconcile = build_reconcile_fn(sync_fn=sync_fn)
        before_ira = await storage_mod.manager_get("ira")
        before_other = await storage_mod.manager_get("other")

        me_wrong_id = FakeMe(id=999999999, username="ira_tg_t", first_name="Ира", last_name="")
        result = await reconcile(me_wrong_id, manager_key="ira", db_path=db_path)
        after_ira = await storage_mod.manager_get("ira")
        after_other = await storage_mod.manager_get("other")

        check("14. action == identity_mismatch", result.get("action") == "identity_mismatch", result)
        check("14b. canonical write function invoked EXACTLY 0 times (deterministic counter, not a timestamp inference)",
              write_calls[0] == 0, write_calls[0])
        check("15. ira row completely unchanged", after_ira == before_ira, (before_ira, after_ira))
        check("16. other manager untouched", after_other == before_other, (before_other, after_other))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# Scenarios 8-9: repeated sync of identical data -> noop, updated_at
# unchanged, `other` still untouched.
# ======================================================================

async def test_8_repeat_sync_is_noop():
    print("\n-- Scenarios 8-9: repeated identical sync -> noop --")
    tmp_root, db_path, storage_mod = await seed_env(ira_source="auto")
    try:
        write_calls = [0]
        sync_fn = _counting_wrapper(storage_mod.manager_sync_telegram_profile_in_db, write_calls)
        reconcile = build_reconcile_fn(sync_fn=sync_fn)

        r1 = await reconcile(ME_IRA, manager_key="ira", db_path=db_path)
        mid = await storage_mod.manager_get("ira")
        before_other = await storage_mod.manager_get("other")
        check("17b. canonical write function invoked EXACTLY 1 time on the real (non-noop) first sync",
              write_calls[0] == 1, write_calls[0])

        r2 = await reconcile(ME_IRA, manager_key="ira", db_path=db_path)
        after = await storage_mod.manager_get("ira")
        after_other = await storage_mod.manager_get("other")

        check("17. first call action == synced", r1.get("action") == "synced", r1)
        check("18. second call action == noop", r2.get("action") == "noop", r2)
        check(
            "18b. canonical write function STILL invoked exactly 1 time in total -- the noop call added ZERO "
            "invocations (deterministic counter, not a timestamp inference)",
            write_calls[0] == 1, write_calls[0],
        )
        check(
            "19. updated_at unchanged across the noop call",
            after.get("updated_at") == mid.get("updated_at"),
            (mid.get("updated_at"), after.get("updated_at")),
        )
        check("20. other manager untouched across both calls", after_other == before_other, (before_other, after_other))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# MUT-A: strip WHERE manager_key=? from the REAL storage function's AST --
# `other` MUST get clobbered.
# ======================================================================

async def test_mutA_where_scope_stripped():
    print("\n-- MUT-A: WHERE manager_key=? scoping removed (structural AST mutant) --")
    tmp_root, db_path, storage_mod = await seed_env(ira_source="auto")
    try:
        mutant_sync = build_sync_fn(mutate="strip_where")
        reconcile = build_reconcile_fn(sync_fn=mutant_sync)
        before_other = await storage_mod.manager_get("other")

        await reconcile(ME_IRA, manager_key="ira", db_path=db_path)
        after_other = await storage_mod.manager_get("other")

        check(
            "MUT-A. mutant WHERE-less UPDATE clobbers `other` (proves the real "
            "WHERE manager_key=? scoping exercised in scenario 7/8 is load-bearing, not a tautology)",
            after_other != before_other,
            (before_other, after_other),
        )
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# MUT-B: disable the manual-source guard in the REAL storage function's AST
# -- a 'manual' display_name MUST get overwritten.
# ======================================================================

async def test_mutB_manual_guard_disabled():
    print("\n-- MUT-B: display_name_source='manual' guard disabled (structural AST mutant) --")
    tmp_root, db_path, storage_mod = await seed_env(ira_source="manual")
    try:
        mutant_sync = build_sync_fn(mutate="disable_manual_guard")
        reconcile = build_reconcile_fn(sync_fn=mutant_sync)

        await reconcile(ME_IRA, manager_key="ira", db_path=db_path)
        after = await storage_mod.manager_get("ira")

        check(
            "MUT-B. mutant with the manual guard disabled overwrites display_name "
            "(proves scenario 5's 'manual -> preserved' assertion is load-bearing)",
            after.get("display_name") != "Ira",
            after,
        )
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# MUT-C: disable the tg_user_id-mismatch guard in _identity_reconcile_from_me
# -- a mismatched call MUST write instead of refusing.
# ======================================================================

async def test_mutC_mismatch_guard_disabled():
    print("\n-- MUT-C: tg_user_id mismatch guard disabled (structural AST mutant) --")
    tmp_root, db_path, storage_mod = await seed_env(ira_source="auto")
    try:
        sync_fn = storage_mod.manager_sync_telegram_profile_in_db
        mutant_reconcile = build_reconcile_fn(
            sync_fn=sync_fn, mutate_if_needle="!= live_tg_id", mutate_replacement=False,
        )

        me_wrong_id = FakeMe(id=999999999, username="hijack", first_name="Hijack", last_name="")
        result = await mutant_reconcile(me_wrong_id, manager_key="ira", db_path=db_path)
        after = await storage_mod.manager_get("ira")

        check(
            "MUT-C. mutant with the mismatch guard disabled writes anyway "
            "(proves scenario 7's 'mismatch -> no write' assertion is load-bearing)",
            result.get("action") != "identity_mismatch" and after.get("telegram_username") == "hijack",
            (result, after),
        )
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# MUT-D: disable the no-op guard in _identity_reconcile_from_me -- a fully
# unchanged second call MUST still write (bump updated_at) instead of
# skipping.
# ======================================================================

async def test_mutD_noop_guard_disabled():
    print("\n-- MUT-D: no-op guard disabled (structural AST mutant) --")
    tmp_root, db_path, storage_mod = await seed_env(ira_source="auto")
    try:
        sync_fn = storage_mod.manager_sync_telegram_profile_in_db
        mutant_reconcile = build_reconcile_fn(
            sync_fn=sync_fn, mutate_if_needle="stored_last_name == live_last_name", mutate_replacement=False,
        )

        await mutant_reconcile(ME_IRA, manager_key="ira", db_path=db_path)
        result2 = await mutant_reconcile(ME_IRA, manager_key="ira", db_path=db_path)

        # Deliberately NOT comparing updated_at here: _now_iso() (storage.py) truncates
        # to whole seconds, so two calls milliseconds apart can legitimately produce an
        # IDENTICAL timestamp even though a real write happened -- a false negative, not
        # evidence of no write. `action` is the reliable, timing-independent signal: my
        # own _identity_reconcile_from_me only ever returns {"action": "synced", ...}
        # AFTER actually awaiting manager_sync_telegram_profile_in_db(...) -- the mutated
        # no-op guard being defeated is what lets a fully-identical second call reach
        # that write path and report "synced" instead of short-circuiting to "noop".
        check(
            "MUT-D. mutant with the no-op guard disabled reaches the write path "
            "(action='synced') on a fully-identical second call, instead of short-circuiting "
            "to 'noop' (proves scenario 8's 'noop -> no write' assertion is load-bearing)",
            result2.get("action") == "synced",
            result2,
        )
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# F2 CHECKPOINT (2026-08-09): central DB path routing -- the reconcile must
# write ONLY to the explicit db_path it is given, NEVER to storage.DB_PATH
# (which, inside a real manager runtime process, points at the PER-MANAGER
# database -- main.py overwrites os.environ["DB_PATH"] before storage is
# imported, see main.py ~line 124).
# ======================================================================

async def test_central_db_path_routing():
    print("\n-- Central DB path routing: explicit db_path wins over a storage.DB_PATH decoy --")
    tmp_root, central_db_path, storage_mod = await seed_env(ira_source="auto")
    decoy_root = None
    try:
        # Simulate the real hazard: point storage.DB_PATH at a DECOY db (standing
        # in for a per-manager database) with a DIFFERENT, deliberately-wrong row
        # for the SAME manager_key -- if _identity_reconcile_from_me ever fell back
        # to the module-level storage.DB_PATH instead of its own explicit db_path
        # parameter, this decoy is what it would corrupt.
        decoy_root, decoy_db_path = make_temp_env()
        storage_mod.DB_PATH = decoy_db_path
        storage_mod.QUEUE_DB_PATH = decoy_db_path
        await storage_mod.manager_add(
            manager_key="ira", display_name="DECOY-MUST-NEVER-BE-WRITTEN", phone="", status="active",
            session_path="", db_path=decoy_db_path, workdir="", log_path="",
        )
        await storage_mod.manager_set_fields("ira", tg_user_id=7267762183, telegram_username="decoy_stale_name")
        decoy_before = await storage_mod.manager_get("ira")  # reads via storage.DB_PATH == decoy right now

        sync_fn = storage_mod.manager_sync_telegram_profile_in_db
        reconcile = build_reconcile_fn(sync_fn=sync_fn)
        # storage.DB_PATH is STILL the decoy at this exact call -- the function
        # under test must not consult it at all, only the explicit kwarg below.
        result = await reconcile(ME_IRA, manager_key="ira", db_path=central_db_path)

        storage_mod.DB_PATH = central_db_path
        storage_mod.QUEUE_DB_PATH = central_db_path
        central_after = await storage_mod.manager_get("ira")

        storage_mod.DB_PATH = decoy_db_path
        storage_mod.QUEUE_DB_PATH = decoy_db_path
        decoy_after = await storage_mod.manager_get("ira")

        check("DBPATH-1. action == synced (the CENTRAL row was genuinely updated)", result.get("action") == "synced", result)
        check("DBPATH-2. CENTRAL db_path row received the real Telegram username",
              central_after.get("telegram_username") == "ira_tg_t", central_after)
        check("DBPATH-3. DECOY db (simulating storage.DB_PATH / a per-manager DB) is COMPLETELY untouched",
              decoy_after == decoy_before, (decoy_before, decoy_after))
    finally:
        await cleanup_env(tmp_root)
        if decoy_root is not None:
            await cleanup_env(decoy_root)


# ======================================================================
# F2 CHECKPOINT (2026-08-09): static proof that the ACTUAL call site inside
# main.py's active _tp_hg_run_self_check passes db_path=TPILOT_DB_PATH
# literally -- the behavioral test above proves the FUNCTION honours
# whatever db_path it is given; this proves the CALL SITE hands it the right
# one. Includes a textual mutation proof so the pattern itself is shown to be
# falsifiable, not a vacuous substring that would match anything.
# ======================================================================

_REQUIRED_CALL_SITE_PATTERN = "_identity_reconcile_from_me(me, manager_key=MANAGER_RUNTIME_KEY, db_path=TPILOT_DB_PATH)"


def test_call_site_passes_explicit_central_db_path():
    print("\n-- Static: _tp_hg_run_self_check call site passes explicit TPILOT_DB_PATH --")
    node = _find_func_node(MAIN_SRC, "_tp_hg_run_self_check")
    src = _unparse(node)

    check(
        "callsite-1. the reconcile call site passes db_path=TPILOT_DB_PATH literally "
        "(not storage.DB_PATH, not a hardcoded path, not omitted)",
        _REQUIRED_CALL_SITE_PATTERN in src,
        src,
    )

    wrong_variants = {
        "wrong-global (storage.DB_PATH instead of TPILOT_DB_PATH)":
            src.replace("db_path=TPILOT_DB_PATH", "db_path=DB_PATH"),
        "missing (db_path omitted entirely)":
            src.replace(_REQUIRED_CALL_SITE_PATTERN, "_identity_reconcile_from_me(me, manager_key=MANAGER_RUNTIME_KEY)"),
    }
    for label, wrong_src in wrong_variants.items():
        check(
            f"callsite-mut. a mutated call site ({label}) does NOT match the required pattern "
            "(proves callsite-1 is falsifiable, not vacuous)",
            _REQUIRED_CALL_SITE_PATTERN not in wrong_src,
            wrong_src,
        )


async def main() -> int:
    await test_1_auto_source_full_sync()
    await test_5_manual_source_display_name_preserved()
    await test_6_empty_live_username_clears_stale()
    await test_7_identity_mismatch_no_write()
    await test_8_repeat_sync_is_noop()
    await test_central_db_path_routing()
    test_call_site_passes_explicit_central_db_path()
    await test_mutA_where_scope_stripped()
    await test_mutB_manual_guard_disabled()
    await test_mutC_mismatch_guard_disabled()
    await test_mutD_noop_guard_disabled()

    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s)): {FAILURES}")
        return 1
    print("RESULT: PASS (all checks green)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
