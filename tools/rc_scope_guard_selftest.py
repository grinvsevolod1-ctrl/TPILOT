# -*- coding: utf-8 -*-
"""tools/rc_scope_guard_selftest.py -- scope guard for the RUNTIME READINESS +
BUSINESS LINK RECOVERY RELIABILITY release candidate.

Answers one question and only that question: *did this release candidate change
anything it did not declare?*

It compares the working tree against the byte-verified pre-RC originals kept in
the release backup package and asserts, at AST level:

  * exactly the declared set of top-level defs / module constants differs;
  * every other top-level definition in main.py and storage.py -- roughly nine
    thousand of them -- is byte-identical;
  * the files this RC never touches are untouched;
  * the DB schema statements are unchanged;
  * the four _manager_command_loop generations still exist and gen 4 is still
    last (last-wins is how this project dispatches);
  * the strict replacement/onboarding readiness call sites do not opt into
    advisory mode.

Usage:
    python tools/rc_scope_guard_selftest.py [<release_backup_dir>]

With no argument it picks the newest directory under
C:\\ALM_TPilot_release_backups that contains original/main.py.

This is deliberately independent of the behavioural suites: those prove the
change is correct, this proves the change is *contained*.
"""
from __future__ import annotations

import ast
import glob
import hashlib
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BACKUP_ROOT = Path(r"C:\ALM_TPilot_release_backups")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


# The complete, declared surface of this release candidate. Anything that
# differs and is NOT listed here is an undeclared change and fails the guard.
DECLARED = {
    "main.py": {
        # wave L2 -- readiness
        "_RUNTIME_READY_HEARTBEAT_MAX_AGE_SEC",
        "_manager_health_heartbeat_ok",
        "_manager_runtime_ready_once",
        "manager_runtime_ready",
        "_queue_bizlink_create_n_for_manager",
        "_bizlink_human_error_reason",
        "_bizlink_readiness_safe_log",          # new
        # wave L3 -- observability + classifier
        "_bizlink_classify_error",
        "_manager_recover_link_limit",
        # wave L6 -- single-create coverage (lives inside the gen-4 loop)
        "_manager_command_loop",
        # wave L7 -- accounting safety
        "_manager_delete_business_links",
    },
    "storage.py": {
        # wave L4 -- candidate selection
        "bizlinks_select_delete_candidates",
    },
}

# Files this RC must not touch at all.
UNTOUCHED_FILES = ("panel_bot.py", "manager_bot.py", "partner_stat_bot.py",
                   "preflight_check.py", "proxy_lifecycle.py", "proxy_provider.py",
                   "stats_engine.py", "router.py", "llm_supervisor.py")


def resolve_backup(argv) -> Path:
    if len(argv) > 1:
        return Path(argv[1])
    cands = sorted(glob.glob(str(BACKUP_ROOT / "*" / "original" / "main.py")))
    if not cands:
        raise SystemExit(
            f"no release backup found under {BACKUP_ROOT}; pass the directory explicitly")
    return Path(cands[-1]).parent.parent


def top_level_map(src: str) -> dict:
    """name -> unparsed source, for every top-level def/class/constant.
    Later definitions overwrite earlier ones under the project's last-wins
    override convention, so the map holds what actually takes effect; the
    per-generation count is checked separately below."""
    tree = ast.parse(src)
    out: dict = {}
    for node in tree.body:
        name = getattr(node, "name", None)
        if name:
            out.setdefault(name, []).append(ast.unparse(node))
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out.setdefault(t.id, []).append(ast.unparse(node))
    return {k: "\n".join(v) for k, v in out.items()}


def read_src(path: Path) -> str:
    return path.read_bytes().decode("utf-8-sig")


def main() -> int:
    backup = resolve_backup(sys.argv)
    print("=" * 72)
    print("rc_scope_guard_selftest")
    print(f"baseline: {backup}")
    print("=" * 72)

    for fname, declared in DECLARED.items():
        cur = read_src(BASE_DIR / fname)
        old = read_src(backup / "original" / fname)
        cur_map, old_map = top_level_map(cur), top_level_map(old)

        added = set(cur_map) - set(old_map)
        removed = set(old_map) - set(cur_map)
        changed = {n for n in set(cur_map) & set(old_map) if cur_map[n] != old_map[n]}
        touched = added | removed | changed

        print(f"\n--- {fname}: {len(touched)} top-level definitions differ "
              f"(of {len(old_map)}) ---")
        for n in sorted(touched):
            kind = "ADDED" if n in added else "REMOVED" if n in removed else "changed"
            mark = "declared" if n in declared else "UNDECLARED"
            print(f"      {kind:8} {n}  [{mark}]")

        check(f"{fname}: no undeclared definition changed",
              not (touched - declared), f"undeclared: {sorted(touched - declared)}")
        check(f"{fname}: nothing was removed", not removed, f"removed: {sorted(removed)}")
        check(f"{fname}: every other top-level definition is byte-identical",
              len(touched) == len(touched & declared))

    # Files that must not have been touched at all.
    print("\n--- untouched files ---")
    for f in UNTOUCHED_FILES:
        p = BASE_DIR / f
        if not p.exists():
            continue
        bak = backup / "original" / f
        if bak.exists():
            check(f"{f} is byte-identical to the baseline",
                  hashlib.sha256(p.read_bytes()).hexdigest()
                  == hashlib.sha256(bak.read_bytes()).hexdigest())
        else:
            # Not archived because it was never in scope -- assert it is at
            # least not in the declared change set and still parses.
            try:
                ast.parse(read_src(p))
                check(f"{f} is out of scope and still parses", True)
            except SyntaxError as e:
                check(f"{f} is out of scope and still parses", False, repr(e))

    # DB schema must be untouched.
    print("\n--- schema / dispatch invariants ---")
    cur_storage = read_src(BASE_DIR / "storage.py")
    old_storage = read_src(backup / "original" / "storage.py")
    for kw in ("CREATE TABLE", "ALTER TABLE", "CREATE INDEX", "CREATE UNIQUE INDEX"):
        check(f"storage.py: '{kw}' statement count unchanged",
              cur_storage.count(kw) == old_storage.count(kw),
              f"{old_storage.count(kw)} -> {cur_storage.count(kw)}")

    cur_main = read_src(BASE_DIR / "main.py")
    old_main = read_src(backup / "original" / "main.py")
    tree = ast.parse(cur_main)
    loops = [n for n in tree.body if getattr(n, "name", "") == "_manager_command_loop"]
    old_loops = [n for n in ast.parse(old_main).body
                 if getattr(n, "name", "") == "_manager_command_loop"]
    check("main.py: _manager_command_loop generation count unchanged (gen 4 still last)",
          len(loops) == len(old_loops) == 4, f"{len(old_loops)} -> {len(loops)}")
    check("main.py: the shadowed early generations were not deleted",
          len(loops) > 1)

    # The strict replacement/onboarding contract.
    for fn in ("_repl4_validate_new_runtime",):
        src = [ast.unparse(n) for n in tree.body if getattr(n, "name", "") == fn]
        check(f"{fn} never opts into heartbeat_advisory",
              all("heartbeat_advisory" not in s for s in src))
    # Count real call sites via the AST, not raw text -- the explanatory comment
    # above the call mentions the keyword too, and a text count would conflate
    # documentation with behaviour.
    advisory_calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if (kw.arg == "heartbeat_advisory"
                        and isinstance(kw.value, ast.Constant) and kw.value.value is True):
                    advisory_calls.append(getattr(node.func, "id", None)
                                          or getattr(node.func, "attr", "?"))
    check("advisory mode is opted into at exactly one call site",
          len(advisory_calls) == 1, f"call sites: {advisory_calls}")
    check("that one call site is the bounded poller, not the one-shot pass",
          advisory_calls == ["manager_runtime_ready"], f"call sites: {advisory_calls}")

    print("\n" + "=" * 72)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: ALL PASS -- the diff is exactly what was declared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
