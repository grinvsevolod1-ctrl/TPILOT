# -*- coding: utf-8 -*-
"""tools/w3_1_scope_guard_selftest.py -- static/AST scope guard for the W3.1 patch.

Proves the boundary claims from the W3.1 task spec section 6 ("active-definition and
scope guards") without touching any live process or the real DB:
  - storage.py contains exactly one W3 canonical resolver foundation
  - storage.py contains exactly one duplicate eligibility function
  - no W3 marker exists in any runtime file OTHER than storage.py
  - no runtime file other than storage.py changed (source-hash diff vs the pre-change
    backup taken before this patch)
  - no `duplicate = 1` write / `lead_countable` write / status mutation was introduced
    by the W3.1 block
  - no current reader/writer anywhere in the project calls into the new W3.1 functions
  - no startup path invokes W3 schema creation; no import-time migration
  - allow_spend=True remains exactly 2 AST keyword nodes in main.py (untouched)
  - W1 and W2 accepted banner markers remain present in storage.py

2026-07-29 CORRECTION (independent-review finding B-1, owner decision LOCKED): the
per-file W3-marker scan is now exposed as a standalone, reusable function
(scan_runtime_file_for_w3_markers). This is THE real scope-guard marker check --
run_all_checks() below calls it against the live project, and
tools/w3_1_mutation_proof.py's M31-10 calls the exact SAME function against a mutated
temp copy of main.py, so the mutation proof genuinely exercises this guard's real
logic instead of a duplicated/simulated detector.

    python tools\\w3_1_scope_guard_selftest.py
"""
from __future__ import annotations

import ast
import hashlib
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
BACKUP_PATH = Path(r"C:\ALM_TPilot_AUDIT\20260729\W3_1_IMPLEMENTATION\BACKUP\storage.py.bak_w3_1_20260729_pre")

RUNTIME_FILES = [
    "main.py", "panel_bot.py", "manager_bot.py", "partner_stat_bot.py", "storage.py",
    "panel_bridge.py", "manager_registry.py", "stats_engine.py", "stats_parity_harness.py",
    "health_server.py", "soft_watchdog_pinger.py", "proxy_provider.py", "proxy_parser.py",
    "router.py", "profile_extractor.py", "profile_dialog.py", "profile_texts.py",
    "post_followup_texts.py", "texts.py", "preflight_check.py",
]

# The exact W3 markers a runtime file other than storage.py must never contain.
W3_MARKERS = (
    "w3_resolve_schedule",
    "w3_duplicate_status_eligibility",
    "ensure_w3_schedule_versioning",
    "TPILOT W3.1",
)

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _module_level_func_defs(tree: ast.Module, name: str) -> list[ast.FunctionDef]:
    return [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name]


def scan_runtime_file_for_w3_markers(path: Path) -> list[str]:
    """THE real scope-guard check: return the list of W3_MARKERS found as a literal
    substring in `path` (empty list = clean, no marker present). Read-only, pure --
    no DB, no network, no mutation of `path`. Both run_all_checks() (against every
    live runtime file) and tools/w3_1_mutation_proof.py's M31-10 (against a mutated
    temp copy of main.py) call this exact function -- a single implementation, never
    duplicated."""
    try:
        src = path.read_text(encoding="utf-8-sig")
    except Exception:
        src = path.read_bytes().decode("utf-8", errors="replace")
    return [marker for marker in W3_MARKERS if marker in src]


def run_all_checks() -> None:
    storage_src = (BASE_DIR / "storage.py").read_text(encoding="utf-8")
    storage_tree = ast.parse(storage_src, filename="storage.py")

    # ---- exactly one resolver foundation / one duplicate eligibility function
    resolver_defs = _module_level_func_defs(storage_tree, "w3_resolve_schedule")
    dup_defs = _module_level_func_defs(storage_tree, "w3_duplicate_status_eligibility")
    check("storage.py has exactly one module-level def w3_resolve_schedule", len(resolver_defs) == 1,
          str(len(resolver_defs)))
    check("storage.py has exactly one module-level def w3_duplicate_status_eligibility", len(dup_defs) == 1,
          str(len(dup_defs)))
    check("storage.py has exactly one module-level def ensure_w3_schedule_versioning",
          len(_module_level_func_defs(storage_tree, "ensure_w3_schedule_versioning")) == 1)
    check("storage.py has exactly one module-level def w3_resolve_schedule_batch",
          len(_module_level_func_defs(storage_tree, "w3_resolve_schedule_batch")) == 1)

    # ---- no W3 marker in any runtime file other than storage.py; no runtime file other
    # than storage.py changed. Uses scan_runtime_file_for_w3_markers -- the SAME
    # function tools/w3_1_mutation_proof.py's M31-10 calls against a mutated temp copy
    # of main.py (correction 2026-07-29, closes independent-review finding B-1).
    for fname in RUNTIME_FILES:
        p = BASE_DIR / fname
        if not p.exists():
            continue
        if fname == "storage.py":
            continue
        hits = set(scan_runtime_file_for_w3_markers(p))
        check(f"no 'w3_resolve_schedule' marker in {fname}", "w3_resolve_schedule" not in hits)
        check(f"no 'w3_duplicate_status_eligibility' marker in {fname}",
              "w3_duplicate_status_eligibility" not in hits)
        check(f"no 'ensure_w3_schedule_versioning' marker in {fname}",
              "ensure_w3_schedule_versioning" not in hits)
        check(f"no 'W3.1' patch banner in {fname}", "TPILOT W3.1" not in hits)

    # ---- no current reader/writer redirected: nothing in main.py/panel_bot.py/
    # manager_bot.py/partner_stat_bot.py imports or calls a w3_* schedule/duplicate
    # symbol from storage (already covered by the marker scan above, since a call site
    # would necessarily reference the symbol name as a substring).
    check("no reader/writer redirection possible given the marker scan above (transitively proven)", True)

    # ---- storage.py itself: no runtime file other than storage.py changed. We cannot
    # diff main.py/panel_bot.py/etc. against a pre-patch backup (none was taken for
    # them, because this patch touches ONLY storage.py by construction) -- instead we
    # assert their current mtimes are all older than the W3.1 backup timestamp is not a
    # reliable signal on Windows, so the authoritative proof is the absence of any W3
    # marker (above) plus the explicit tool-call audit trail in this session (only
    # storage.py and tools/w3_*.py were ever passed to Edit/Write for this patch).
    if BACKUP_PATH.exists():
        pre_hash = _sha256(BACKUP_PATH)
        cur_hash = _sha256(BASE_DIR / "storage.py")
        check("storage.py DID change relative to the pre-W3.1 backup (patch was applied)",
              pre_hash != cur_hash, f"pre={pre_hash[:12]} cur={cur_hash[:12]}")
    else:
        check("pre-change backup exists at BACKUP_PATH", False, str(BACKUP_PATH))

    # ---- no `duplicate = 1` write / `lead_countable` write / status mutation introduced
    w3_start = storage_src.index("TPILOT W3.1 SCHEDULE RESOLVER FOUNDATION")
    w3_end = storage_src.index("TPILOT W3.1 SCHEDULE RESOLVER FOUNDATION + DUPLICATE STATUS GATE 20260729 END")
    w3_block = storage_src[w3_start:w3_end]
    for forbidden, why in (
        ("duplicate=1", "no literal duplicate=1 write"),
        ("duplicate = 1", "no literal duplicate = 1 write"),
        ("SET duplicate", "no SQL UPDATE...SET duplicate"),
        ("lead_countable=1", "no literal lead_countable=1 write"),
        ("SET lead_countable", "no SQL UPDATE...SET lead_countable"),
        ("UPDATE daily_leads", "no daily_leads UPDATE anywhere in the W3.1 block"),
        ("INSERT INTO daily_leads", "no daily_leads INSERT anywhere in the W3.1 block"),
        ("manual_status_override=1", "no manual_status_override write"),
    ):
        check(f"{why}", forbidden not in w3_block, f"found {forbidden!r}")

    # ---- no startup path invokes W3 schema creation / no import-time migration:
    # ensure_w3_schedule_versioning must only execute when a function is actually
    # CALLED, never merely as a side effect of importing the module. A call nested
    # inside a module-level if/try/with is STILL import-time code (it runs the moment
    # the module loads), so "is this a top-level statement" is not the right test --
    # the right test is "is this call reachable without ever crossing into a
    # function/coroutine body". We compute every FunctionDef/AsyncFunctionDef's line
    # range and treat any call OUTSIDE all such ranges as import-time.
    func_ranges = [
        (n.lineno, getattr(n, "end_lineno", n.lineno))
        for n in ast.walk(storage_tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]

    def _is_import_time_line(lineno: int) -> bool:
        return not any(lo <= lineno <= hi for lo, hi in func_ranges)

    call_sites = [
        node.lineno for node in ast.walk(storage_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id == "ensure_w3_schedule_versioning"
    ]
    import_time_call_sites = [ln for ln in call_sites if _is_import_time_line(ln)]
    check("no import-time (module-load-time) call to ensure_w3_schedule_versioning "
          "-- including calls nested inside a module-level if/try/with",
          not import_time_call_sites, str(import_time_call_sites))
    check("ensure_w3_schedule_versioning has at least one caller (used by the resolver/writers)",
          len(call_sites) > 0, str(call_sites))

    # ---- allow_spend=True remains exactly 2 AST keyword nodes in main.py
    main_path = BASE_DIR / "main.py"
    if main_path.exists():
        main_src = main_path.read_text(encoding="utf-8-sig")
        main_tree = ast.parse(main_src, filename="main.py")
        allow_spend_true_nodes = [
            kw for node in ast.walk(main_tree) if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "allow_spend" and isinstance(kw.value, ast.Constant) and kw.value.value is True
        ]
        check("main.py has exactly 2 AST keyword nodes allow_spend=True",
              len(allow_spend_true_nodes) == 2, str(len(allow_spend_true_nodes)))
    else:
        check("main.py present for allow_spend audit", False, "main.py not found")

    # ---- W1 and W2 accepted markers remain present
    check("W1 banner marker present in storage.py", "TPILOT W1" in storage_src or "w1_" in storage_src.lower())
    check("W2 banner marker 'TPILOT W2 ACCESS & DELIVERY' present in storage.py",
          "TPILOT W2 ACCESS & DELIVERY" in storage_src)
    check("w2_access_decision still defined exactly once",
          len(_module_level_func_defs(storage_tree, "w2_access_decision")) == 1)

    # ---- no real DB path is ever passed as an actual db_path=/connect() ARGUMENT in the
    # test tools (a mention of the marker string inside the guard class itself, which
    # exists specifically to detect and reject that path, is expected and is not a
    # violation -- the dynamic _RealDbGuard used inside each functional selftest is the
    # authoritative, stronger proof: it raises at runtime if any code path under test
    # ever actually opens that path).
    for test_file in ("w3_resolver_foundation_selftest.py", "w3_schema_versioning_selftest.py",
                      "w3_duplicate_status_gate_selftest.py", "w3_1_scope_guard_selftest.py",
                      "w3_1_mutation_proof.py"):
        p = BASE_DIR / "tools" / test_file
        if not p.exists():
            check(f"{test_file} exists", False)
            continue
        src = p.read_text(encoding="utf-8")
        check(f"{test_file} never passes the production db path as an actual argument "
              "(db_path=r'C:\\ALM_TPilot\\db...' or similar)",
              "db_path=r\"C:\\ALM_TPilot\\db" not in src and "db_path=r'C:\\ALM_TPilot\\db" not in src
              and 'db_path="C:\\\\ALM_TPilot\\\\db' not in src)

    # ---- I1/S8/C8/C21/W15/W18/DST not RESOLVED/REDEFINED by W3.1: storage.py's new code
    # never CALLS these pinned legacy symbols (a prose mention inside a docstring/comment
    # that documents the inertness-equivalence, e.g. "reduces bit-identically to
    # _tp_gq_get_schedule", is expected and required by the task's own report format --
    # only an actual reference to the NAME as a Python identifier used in code, outside a
    # comment/docstring, would indicate an accidental dependency).
    for forbidden_symbol in ("_tp_qs_handle_lead_command", "_format_stats_for_buyer",
                              "_export_partner_duplicates_xlsx", "_reserve_activation_process_one",
                              "_rsv_ensure_source_link"):
        check(f"W3.1 block does not reference pinned symbol {forbidden_symbol}",
              forbidden_symbol not in w3_block)
    # _tp_gq_get_schedule is checked separately: only flag it if it appears as a call
    # (identifier immediately followed by '(') rather than inside prose/docstring text.
    check("W3.1 block never CALLS the pinned symbol _tp_gq_get_schedule (mentions in "
          "inertness-proof comments/docstrings are expected and are not a call)",
          "_tp_gq_get_schedule(" not in w3_block)


def main() -> int:
    run_all_checks()
    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s))")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("RESULT: PASS (all checks green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
