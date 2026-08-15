# -*- coding: utf-8 -*-
"""tools/reserve_release_selftest.py -- offline self-test for the
"orphaned reserve release" patch (reserve_pair_release_for_primary +
manager-deletion integration + reserve picker orphan visibility).

Business rule under test:
* a reserve linked to an EXISTING primary stays unavailable in the picker;
* deleting a primary retires its linked reserve pairs (they become reusable);
* a legacy 'linked' pair whose primary manager row no longer exists must NOT
  hide the reserve from the picker;
* a reserve can never be attached to two primaries at once (PK reserve_key).

Techniques (matching the project's own tools/*_selftest.py conventions):
* storage.py IS importable -> real helpers exercised against a throwaway
  temporary SQLite file (never the real project DB);
* panel_bot.py / main.py are NOT importable (Telethon/env side effects) ->
  the picker function is AST-extracted and exec'd with fakes; the deletion
  handler is verified by static/AST evidence over its ACTIVE definition.

Pure/offline: no network, no Telegram, no real DB, no real API calls.

    python3.12 tools\\reserve_release_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aiosqlite
import storage
from manager_registry import normalize_manager_key

MAIN_PY = BASE_DIR / "main.py"
PANEL_BOT_PY = BASE_DIR / "panel_bot.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _selftest_db_guard(db_path: str, base_dir: Path, storage_mod) -> None:
    """Fail-fast production-path DB guard (2026-07-16, same technique as the
    other replacement selftests' own guard): refuses any db_path located
    under base_dir/db, and -- since this file explicitly threads db_path=
    through every storage.* call rather than relying on the module-level
    globals -- also defensively pins storage.DB_PATH/QUEUE_DB_PATH to the
    same temp path in case any newly-extracted dependency ever falls back
    to them instead of an explicit parameter."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    storage_mod.DB_PATH = db_path
    storage_mod.QUEUE_DB_PATH = db_path


def find_defs(path: Path, name: str) -> list:
    """All top-level def/async-def nodes with the given name (override stacks:
    the LAST one is the active binding)."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    return [n for n in tree.body if getattr(n, "name", None) == name]


def extract_and_exec(path: Path, names: set[str], extra_ns: dict) -> dict:
    """AST-extract the LAST (active) def of each requested name and exec into a
    seeded namespace -- same technique as the other tools/*_selftest.py files."""
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    picked: dict = {}
    for node in tree.body:
        if getattr(node, "name", None) in names:
            picked[node.name] = node  # later defs overwrite earlier -> last wins
    if set(picked) != names:
        raise AssertionError(f"expected {names}, found {set(picked)} in {path}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in names)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path.name}>", "exec"), ns)
    return ns


# ----------------------------------------------------------------------
# Fakes for the picker namespace
# ----------------------------------------------------------------------

class FakeButton:
    def __init__(self, text, data):
        self.text = str(text)
        self.data = data if isinstance(data, bytes) else str(data).encode()

    @classmethod
    def inline(cls, text, data=b""):
        return cls(text, data)


def picker_available_keys(rows_out) -> set:
    """Extract candidate reserve keys from the picker's button rows
    (attach buttons carry callback data rsv:pair:<pk>:<rk>)."""
    out = set()
    for row in rows_out:
        for btn in row:
            data = btn.data.decode("utf-8", errors="ignore")
            if data.startswith("rsv:pair:"):
                out.add(data.split(":", 3)[3])
    return out


def run_storage_checks(tmp_db: str) -> None:
    print("\n-- Storage: reserve_pair_release_for_primary (real helpers, temp SQLite) --")

    # Scenario: primaries A, B, C; reserves r1 (paired to A), r3 (paired to C).
    storage.ensure_reserve_tables(tmp_db)
    storage.ensure_reserve_tables(tmp_db)  # idempotent double-ensure
    storage.reserve_pair_set("primary_a", "reserve_r1", "src1", db_path=tmp_db)
    storage.reserve_pair_set("primary_c", "reserve_r3", "src1", db_path=tmp_db)

    pair = storage.reserve_pair_get_by_reserve("reserve_r1", db_path=tmp_db)
    check("1. linked reserve r1 is paired to primary A with status='linked'",
          bool(pair) and pair.get("primary_key") == "primary_a" and pair.get("status") == "linked", repr(pair))

    released = storage.reserve_pair_release_for_primary("primary_a", db_path=tmp_db)
    check("2a. release_for_primary(A) reports exactly 1 released pair", released == 1, repr(released))
    pair = storage.reserve_pair_get_by_reserve("reserve_r1", db_path=tmp_db)
    check("2b. r1's pair row is now status='retired' (row NOT deleted, created_at kept)",
          bool(pair) and pair.get("status") == "retired" and bool(pair.get("created_at")), repr(pair))

    # 3/4: released reserve becomes eligible + linkable to primary B.
    pair_b = storage.reserve_pair_set("primary_b", "reserve_r1", "src1", db_path=tmp_db)
    check("3/4. released r1 can be linked to primary B (status back to 'linked')",
          pair_b.get("primary_key") == "primary_b" and pair_b.get("status") == "linked", repr(pair_b))

    # 5: PK(reserve_key) forbids simultaneous attachment to A and B.
    storage.reserve_pair_set("primary_a", "reserve_r1", "src1", db_path=tmp_db)  # re-pair moves it
    con = sqlite3.connect(tmp_db)
    n_rows = con.execute("SELECT COUNT(*) FROM manager_reserve_pairs WHERE reserve_key='reserve_r1'").fetchone()[0]
    con.close()
    pair = storage.reserve_pair_get_by_reserve("reserve_r1", db_path=tmp_db)
    check("5. reserve_key uniqueness: re-pairing MOVES r1 (exactly 1 row, primary=A) -- never two active primaries",
          n_rows == 1 and pair.get("primary_key") == "primary_a", f"rows={n_rows} pair={pair!r}")

    # 6: repeated release is idempotent.
    first = storage.reserve_pair_release_for_primary("primary_a", db_path=tmp_db)
    second = storage.reserve_pair_release_for_primary("primary_a", db_path=tmp_db)
    check("6. repeated release is idempotent (1 then 0 affected rows)", first == 1 and second == 0,
          f"first={first} second={second}")

    # 7: unrelated primary C's pair is untouched by all of the above.
    pair_c = storage.reserve_pair_get_by_reserve("reserve_r3", db_path=tmp_db)
    check("7. unrelated pair r3->C is still 'linked' and unchanged",
          bool(pair_c) and pair_c.get("primary_key") == "primary_c" and pair_c.get("status") == "linked", repr(pair_c))

    # 8: a retired row remains valid and reusable.
    pair = storage.reserve_pair_set("primary_b", "reserve_r1", "src2", db_path=tmp_db)
    check("8. retired row is reusable: reserve_pair_set relinks r1 to B ('retired' -> 'linked')",
          pair.get("status") == "linked" and pair.get("primary_key") == "primary_b", repr(pair))

    # release with empty key is a safe no-op
    check("8b. release with empty key is a safe no-op returning 0",
          storage.reserve_pair_release_for_primary("", db_path=tmp_db) == 0)


def run_picker_checks(tmp_db: str) -> None:
    print("\n-- Picker: _reserve_pick_buttons (real function via AST, temp SQLite) --")

    # Authoritative managers registry (what /manager_delete_full hard-deletes from).
    # NOTE: 'arch_primary' EXISTS here but is hidden from the UI candidate pool below --
    # exactly the archived-primary case of check 12.
    con = sqlite3.connect(tmp_db)
    con.execute("CREATE TABLE IF NOT EXISTS managers(manager_key TEXT PRIMARY KEY, status TEXT DEFAULT 'active')")
    for mk, st in (("primary_new", "active"), ("primary_live", "active"),
                   ("arch_primary", "archived"), ("res_free", "active"),
                   ("res_live", "active"), ("res_orphan", "active"), ("res_arch", "active")):
        con.execute("INSERT OR REPLACE INTO managers(manager_key, status) VALUES(?,?)", (mk, st))
    con.commit()
    con.close()

    # Pair rows: res_live -> primary_live (alive), res_orphan -> ghost_primary (row
    # deleted long ago), res_arch -> arch_primary (archived but EXISTS).
    storage.reserve_pair_set("primary_live", "res_live", db_path=tmp_db)
    storage.reserve_pair_set("ghost_primary", "res_orphan", db_path=tmp_db)
    storage.reserve_pair_set("arch_primary", "res_arch", db_path=tmp_db)
    con = sqlite3.connect(tmp_db)
    con.execute("DELETE FROM managers WHERE manager_key='ghost_primary'")  # ensure ghost truly absent
    con.commit()
    con.close()

    # UI candidate pool (what _manager_rows_all offers): free + paired reserves; the
    # archived primary and closers are NOT in the pool (pre-existing UI rule).
    ui_pool = [
        {"manager_key": "res_free"},
        {"manager_key": "res_live"},
        {"manager_key": "res_orphan"},
        {"manager_key": "res_arch"},
        {"manager_key": "primary_live"},
    ]

    ns = extract_and_exec(
        PANEL_BOT_PY,
        {"_reserve_pick_buttons"},
        {
            "sqlite3": sqlite3,
            "TPILOT_DB_PATH": tmp_db,
            "normalize_manager_key": normalize_manager_key,
            "_source_links_map_sync": lambda: {},
            "_manager_rows_all": lambda: [dict(r) for r in ui_pool],
            "_manager_short_label": lambda r: str(r.get("manager_key") or ""),
            "Button": FakeButton,
            "List": list,
        },
    )
    pick = ns["_reserve_pick_buttons"]

    available = picker_available_keys(pick("primary_new"))
    check("9. reserve linked to an EXISTING primary stays unavailable", "res_live" not in available, repr(available))
    check("10. reserve whose linked primary row no longer exists IS available (orphan recovered)",
          "res_orphan" in available, repr(available))
    check("10b. a completely free manager is offered as before", "res_free" in available, repr(available))
    check("11. candidates outside the UI pool (archived/closer managers) are still NOT offered",
          "arch_primary" not in available, repr(available))
    check("12. primary hidden from the UI pool but PRESENT in managers (archived) is NOT treated as deleted -- its reserve stays excluded",
          "res_arch" not in available, repr(available))
    check("12b. the picker never offers the primary itself",
          "primary_new" not in picker_available_keys(pick("primary_new")))

    # Picker must not mutate DB state (read-only render).
    pair_after = storage.reserve_pair_get_by_reserve("res_orphan", db_path=tmp_db)
    check("12c. rendering the picker did NOT auto-retire the legacy orphan row (still 'linked')",
          bool(pair_after) and pair_after.get("status") == "linked", repr(pair_after))


def run_deletion_integration_checks() -> None:
    print("\n-- Deletion integration: _panel_manager_delete_full_command (static/AST) --")

    wrapper_defs = find_defs(MAIN_PY, "_panel_manager_delete_full_command")
    check("13a. _panel_manager_delete_full_command has exactly 1 definition (no override shadowing)",
          len(wrapper_defs) == 1, f"defs={len(wrapper_defs)}")
    wrapper_src = ast.unparse(wrapper_defs[-1]) if wrapper_defs else ""

    # STAGE 4 (2026-07-16): the actual delete logic (reserve-release ordering,
    # tombstone, the 7 DELETEs, etc.) was extracted out of the wrapper into
    # _manager_delete_full_core so Stage 4's commit engine can reuse it
    # without an admin password. The wrapper itself is now just a 4-line
    # password-parse-then-delegate shim -- checks 13b/14a-15 below therefore
    # inspect the CORE's body (where that logic actually lives now), not the
    # wrapper's, preserving their original verification intent unweakened.
    core_defs = find_defs(MAIN_PY, "_manager_delete_full_core")
    check("13a2. _manager_delete_full_core (the extracted shared delete logic) has exactly 1 definition",
          len(core_defs) == 1, f"defs={len(core_defs)}")
    src = ast.unparse(core_defs[-1]) if core_defs else ""

    check("13a3. the wrapper _panel_manager_delete_full_command's own body genuinely calls "
          "_manager_delete_full_core(key, ...) -- structural proof of delegation, not just two "
          "independently-existing functions",
          "_manager_delete_full_core(" in wrapper_src and "key" in wrapper_src, wrapper_src)
    check("13a4. the wrapper no longer contains the destructive delete logic itself "
          "(no raw 'DELETE FROM managers' in the wrapper -- it must live only in the core)",
          "DELETE FROM managers WHERE manager_key=?" not in wrapper_src, wrapper_src)

    check("13b. active deletion handler imports and calls reserve_pair_release_for_primary "
          "with the manager key as PRIMARY and TPILOT_DB_PATH",
          "reserve_pair_release_for_primary" in src
          and "_rsv_release_for_primary(key, db_path=TPILOT_DB_PATH)" in src)

    # 14 (POST-REVIEW ATOMICITY FIX): corrected ordering. Final review found the PRIOR
    # implementation released the reserve BEFORE the managers DELETE/commit -- a swallowed
    # delete (every statement in the loop is try/except:pass) could leave the primary
    # alive while its reserve was already retired and reassignable. This now asserts the
    # OPPOSITE, corrected ordering: delete/commit -> verify absence -> release.
    delete_pos = src.find("DELETE FROM managers WHERE manager_key=?")
    verify_pos = src.find("SELECT 1 FROM managers WHERE manager_key=?")
    release_pos = src.find("_rsv_release_for_primary(key")
    check("14a. the managers DELETE statement precedes the absence-verification query",
          0 < delete_pos < verify_pos, f"delete@{delete_pos} verify@{verify_pos}")
    check("14b. the absence-verification query precedes the reserve release call "
          "(corrected order: delete -> verify -> release)",
          0 < verify_pos < release_pos, f"verify@{verify_pos} release@{release_pos}")
    check("14c. verification is fail-closed by default (still_exists starts True; "
          "only becomes False on a positively confirmed absent row)",
          "still_exists = True" in src)
    check("14d. release is gated behind the still_exists check, not called unconditionally",
          "if still_exists:" in src and 0 <= src.find("if still_exists:") < release_pos)
    check("14e. the OLD (buggy) pre-delete-abort message is gone -- release no longer "
          "runs before, or aborts, the managers delete",
          "Удаление из реестра не выполнено, чтобы резерв не остался невидимо привязанным" not in src)
    check("14f. a still-existing manager after delete/commit produces an explicit "
          "'not confirmed' failure and explicitly states reserves were NOT released (no false success)",
          "не подтверждено" in src and "НЕ освобождались" in src)
    check("14g. a release failure AFTER confirmed deletion is reported without undoing the deletion",
          "Менеджер удалён, но не удалось очистить резервные метаданные" in src)

    check("15. existing deletion flow is preserved (stop process, onboarding cleanup, backup move, all 7 DELETEs, danger log)",
          all(marker in src for marker in (
              "_stop_manager_process(key",
              "manager_delete_onboarding_by_key(key)",
              "_deleted_managers_backup",
              "DELETE FROM manager_onboarding WHERE manager_key=?",
              "DELETE FROM manager_commands WHERE target_key=?",
              "DELETE FROM access_targets WHERE manager_key=?",
              "DELETE FROM manager_work_status WHERE manager_key=?",
              "DELETE FROM manager_source_links WHERE manager_key=?",
              "DELETE FROM manager_group_members WHERE manager_key=?",
              "DELETE FROM managers WHERE manager_key=?",
              "_log_manager_danger_action",
          )))


# ======================================================================
# BEHAVIORAL: the 3 critical states, executing the REAL active
# _panel_manager_delete_full_command (AST-extracted, exec'd with fakes for
# its Telethon/env-only dependencies -- everything DB-related runs for
# real against throwaway temp SQLite files, matching the technique the
# static checks above cannot provide: proof, not string matching).
# ======================================================================

class _FakeKyivNow:
    """Stand-in for _kyiv_now() -- only .strftime(...) is used by the handler."""

    def strftime(self, fmt: str) -> str:
        return "20260101_000000"


def _delete_cmd_namespace(tmp_db: str, base_dir: Path, captured_logs: list) -> dict:
    async def _fake_manager_get(k):
        return {"manager_key": k}

    def _fake_missing_text(k):
        return f"missing:{k}"

    def _fake_danger_parse_args(args, usage):
        parts = str(args or "").split()
        return (parts[0] if parts else "", "pw", True, "")

    async def _fake_stop_manager_process(k, silent=True):
        return None

    async def _fake_delete_onboarding(k):
        return None

    def _fake_build_manager_paths(base, k):
        # Points at a path that deliberately does NOT exist, so the handler's
        # `if root.exists():` runtime-folder-move branch is skipped -- that
        # pre-existing behavior is unrelated to the ordering bug under test.
        return {"root": str(Path(base) / "nonexistent_runtime_root")}

    async def _fake_log_action(*a, **kw):
        captured_logs.append(kw)

    return {
        "manager_get": _fake_manager_get,
        "_panel_manager_missing_text": _fake_missing_text,
        "_danger_parse_args": _fake_danger_parse_args,
        "_stop_manager_process": _fake_stop_manager_process,
        "manager_delete_onboarding_by_key": _fake_delete_onboarding,
        "BASE_DIR": base_dir,
        "build_manager_paths": _fake_build_manager_paths,
        "Path": Path,
        "shutil": shutil,
        "_kyiv_now": lambda: _FakeKyivNow(),
        "aiosqlite": aiosqlite,
        "TPILOT_DB_PATH": tmp_db,
        "_log_manager_danger_action": _fake_log_action,
        # DELETED MANAGER STATS RETENTION 20260711: the active _panel_manager_delete_full_command
        # now also writes a manager_stats_tombstone (real storage.py call, unstubbed --
        # exercises the real code) before the hard-delete, which needs _now_utc_iso().
        "_now_utc_iso": lambda: "2026-07-11T00:00:00",
    }


def run_deletion_behavioral_checks() -> None:
    print("\n-- Deletion integration: 3 critical states (REAL execution, AST-extracted active def) --")

    # STAGE 4 (2026-07-16): the actual delete logic now lives in
    # _manager_delete_full_core (extracted out of _panel_manager_delete_
    # full_command so Stage 4's commit engine can reuse it without an admin
    # password); the wrapper just parses/validates the password then
    # delegates. Both must be extracted into the SAME exec namespace -- they
    # share it as their __globals__, so every dependency _delete_cmd_
    # namespace() already provides (manager_get, _stop_manager_process,
    # BASE_DIR, build_manager_paths, aiosqlite, TPILOT_DB_PATH, etc.) is
    # visible to _manager_delete_full_core too without any new binding.
    ns = extract_and_exec(MAIN_PY, {"_panel_manager_delete_full_command", "_manager_delete_full_core"}, {})
    delete_cmd = ns["_panel_manager_delete_full_command"]

    work_dir = Path(tempfile.mkdtemp(prefix="reserve_selftest_"))
    try:
        # ---- State A: delete fails / manager row survives. Simulated with a REAL
        # SQLite BEFORE-DELETE trigger that aborts -- this exercises the actual
        # production try/except:pass swallow path with a genuine DB failure, not a
        # mocked one. ----
        db_a = str(work_dir / "state_a.db")
        con = sqlite3.connect(db_a)
        con.execute("CREATE TABLE managers(manager_key TEXT PRIMARY KEY)")
        con.execute("INSERT INTO managers(manager_key) VALUES('mgr_a')")
        con.execute(
            "CREATE TRIGGER block_delete BEFORE DELETE ON managers "
            "BEGIN SELECT RAISE(ABORT, 'simulated delete failure'); END;"
        )
        con.commit()
        con.close()
        storage.reserve_pair_set("mgr_a", "reserve_a", db_path=db_a)

        logs_a: list = []
        ns.update(_delete_cmd_namespace(db_a, work_dir, logs_a))
        result_a = asyncio.run(delete_cmd("mgr_a pw confirm"))

        con = sqlite3.connect(db_a)
        still_there = con.execute("SELECT 1 FROM managers WHERE manager_key='mgr_a'").fetchone()
        con.close()
        check("A1. simulated delete failure: managers row genuinely still present (real SQLite trigger fired, not mocked)",
              bool(still_there))
        check("A2. command reports the 'not confirmed' failure text, NOT the success text (no false success)",
              "не подтверждено" in result_a and "🗑 Менеджер полностью удалён" not in result_a, result_a)
        pair_a = storage.reserve_pair_get_by_reserve("reserve_a", db_path=db_a)
        check("A3. reserve release was NOT invoked: the pair is still 'linked' (not 'retired')",
              bool(pair_a) and pair_a.get("status") == "linked", repr(pair_a))
        check("A4. the danger-action log recorded an error result for this attempt",
              any(l.get("result") == "error" for l in logs_a), repr(logs_a))

        # ---- State B: delete succeeds, release succeeds. Uses the REAL (unpatched)
        # storage.reserve_pair_release_for_primary via the handler's own lazy import. ----
        db_b = str(work_dir / "state_b.db")
        con = sqlite3.connect(db_b)
        con.execute("CREATE TABLE managers(manager_key TEXT PRIMARY KEY)")
        con.execute("INSERT INTO managers(manager_key) VALUES('mgr_b')")
        con.commit()
        con.close()
        storage.reserve_pair_set("mgr_b", "reserve_b", db_path=db_b)

        logs_b: list = []
        ns.update(_delete_cmd_namespace(db_b, work_dir, logs_b))
        result_b = asyncio.run(delete_cmd("mgr_b pw confirm"))

        con = sqlite3.connect(db_b)
        gone_b = con.execute("SELECT 1 FROM managers WHERE manager_key='mgr_b'").fetchone()
        con.close()
        check("B1. manager row is genuinely deleted", gone_b is None)
        check("B2. command reports success", "🗑 Менеджер полностью удалён" in result_b, result_b)
        pair_b = storage.reserve_pair_get_by_reserve("reserve_b", db_path=db_b)
        check("B3. reserve pair became 'retired' -- release ran AFTER confirmed deletion",
              bool(pair_b) and pair_b.get("status") == "retired", repr(pair_b))
        check("B4. command result mentions the released-reserve count",
              "Освобождено резервных привязок" in result_b, result_b)

        # ---- State C: delete succeeds, release fails. storage.reserve_pair_release_for_primary
        # is monkeypatched to raise for exactly this call (the handler's lazy `from storage
        # import ...` resolves to the patched attribute at call time). ----
        db_c = str(work_dir / "state_c.db")
        con = sqlite3.connect(db_c)
        con.execute("CREATE TABLE managers(manager_key TEXT PRIMARY KEY)")
        con.execute("INSERT INTO managers(manager_key) VALUES('mgr_c')")
        con.commit()
        con.close()
        storage.reserve_pair_set("mgr_c", "reserve_c", db_path=db_c)

        logs_c: list = []
        ns.update(_delete_cmd_namespace(db_c, work_dir, logs_c))
        orig_release = storage.reserve_pair_release_for_primary

        def _poison_release(*a, **kw):
            raise RuntimeError("simulated reserve release failure")

        storage.reserve_pair_release_for_primary = _poison_release
        try:
            result_c = asyncio.run(delete_cmd("mgr_c pw confirm"))
        finally:
            storage.reserve_pair_release_for_primary = orig_release

        con = sqlite3.connect(db_c)
        gone_c = con.execute("SELECT 1 FROM managers WHERE manager_key='mgr_c'").fetchone()
        con.close()
        check("C1. manager row is STILL genuinely deleted despite the release failure (deletion is not undone/rolled back)",
              gone_c is None)
        check("C2. command reports manager deleted but reserve cleanup failed (not silently OK, not falsely blocked)",
              "🗑 Менеджер полностью удалён" in result_c and "не удалось очистить резервные метаданные" in result_c, result_c)
        pair_c = storage.reserve_pair_get_by_reserve("reserve_c", db_path=db_c)
        check("C3. the orphaned pair remains 'linked' in storage (release genuinely failed, row untouched)",
              bool(pair_c) and pair_c.get("status") == "linked", repr(pair_c))

        # End-to-end tie-in: the State-C orphan (still 'linked', primary now genuinely
        # absent) must be exposed by the REAL picker -- proving "linked orphan remains
        # safe because picker can expose linked orphan" is not just a claim.
        picker_ns = extract_and_exec(
            PANEL_BOT_PY, {"_reserve_pick_buttons"},
            {
                "sqlite3": sqlite3, "TPILOT_DB_PATH": db_c,
                "normalize_manager_key": normalize_manager_key,
                "_source_links_map_sync": lambda: {},
                "_manager_rows_all": lambda: [{"manager_key": "reserve_c"}, {"manager_key": "mgr_new"}],
                "_manager_short_label": lambda r: str(r.get("manager_key") or ""),
                "Button": FakeButton, "List": list,
            },
        )
        available_c = picker_available_keys(picker_ns["_reserve_pick_buttons"]("mgr_new"))
        check("C4. end-to-end: the State-C orphan (release failed, primary genuinely gone) IS available in the real picker",
              "reserve_c" in available_c, repr(available_c))
    finally:
        try:
            shutil.rmtree(work_dir, ignore_errors=True)
        except Exception:
            pass


def run_safety_checks(tmp_db: str) -> None:
    print("\n-- Safety / regression (static) --")

    storage_src = (BASE_DIR / "storage.py").read_text(encoding="utf-8-sig")
    helper_defs = find_defs(BASE_DIR / "storage.py", "reserve_pair_release_for_primary")
    helper_src = ast.unparse(helper_defs[-1]) if helper_defs else ""
    check("16a. new storage helper exists exactly once and contains no DELETE FROM / DROP SQL (UPDATE-only)",
          len(helper_defs) == 1 and "UPDATE manager_reserve_pairs" in helper_src
          and not re.search(r"\bDELETE\s+FROM\b|\bDROP\s+(TABLE|COLUMN|INDEX)\b", helper_src, re.IGNORECASE),
          helper_src[:200])
    check("16b. no schema-destructive SQL anywhere in storage.py (no DROP TABLE / DROP COLUMN)",
          "DROP TABLE" not in storage_src.upper() and "DROP COLUMN" not in storage_src.upper())

    picker_defs = find_defs(PANEL_BOT_PY, "_reserve_pick_buttons")
    picker_src = ast.unparse(picker_defs[-1]) if picker_defs else ""
    check("16c. picker remains read-only: no INSERT/UPDATE/DELETE in _reserve_pick_buttons",
          len(picker_defs) == 1 and not re.search(r"\b(INSERT|UPDATE|DELETE)\b", picker_src, re.IGNORECASE), picker_src[:200])

    # STAGE 4 (2026-07-16): the deletion handler's own logic now lives in
    # _manager_delete_full_core (the wrapper just delegates to it) -- check
    # both, since either could theoretically be where a session op would
    # appear.
    del_wrapper_src = ast.unparse(find_defs(MAIN_PY, "_panel_manager_delete_full_command")[-1])
    del_core_src = ast.unparse(find_defs(MAIN_PY, "_manager_delete_full_core")[-1])
    check("17. patch adds no session deletion/recreation (no .session ops, os.remove/unlink in helper/picker; deletion handler unchanged in that respect)",
          ".session" not in helper_src and ".session" not in picker_src
          and "os.remove" not in helper_src and "os.remove" not in picker_src
          and "os.remove" not in del_wrapper_src and "unlink" not in del_wrapper_src
          and "os.remove" not in del_core_src and "unlink" not in del_core_src)

    tmp_real = os.path.realpath(tmp_db)
    check("18. selftest used ONLY a temp SQLite file (never db/data_tpilot.db or db/data.db)",
          os.path.realpath(tempfile.gettempdir()) in tmp_real
          and "data_tpilot.db" not in tmp_real and os.sep + "db" + os.sep not in tmp_real, tmp_real)

    self_src = Path(__file__).read_text(encoding="utf-8-sig")
    check("19. selftest makes no network/API calls (no requests/telethon/urllib/anthropic imports)",
          not re.search(r"^\s*(import|from)\s+(requests|telethon|urllib|http\.client|anthropic)\b", self_src, re.MULTILINE))


def main() -> int:
    tmp_db = tempfile.mktemp(suffix="_reserve_selftest.db")
    _selftest_db_guard(tmp_db, BASE_DIR, storage)
    try:
        run_storage_checks(tmp_db)
        run_picker_checks(tmp_db)
        run_deletion_integration_checks()
        run_deletion_behavioral_checks()
        run_safety_checks(tmp_db)
    finally:
        try:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
        except Exception:
            pass

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    # check 20: nonzero exit on failure, clear SELFTEST OK on success -- enforced here.
    raise SystemExit(main())
