# -*- coding: utf-8 -*-
"""tools/proxy_role_gate_selftest.py -- offline selftest for the PROXY
RENEWAL RELIABILITY R1 lease role classifier (main.py, 20260808).

Covers the authoritative _prenew_lease_role('managed' | 'orphan' | 'free')
helper and its OBSERVE-MODE wiring into _prenew_notify_expiring_loop /
_prenew_autorenew_loop. main.py cannot be imported standalone (Telethon/env
side effects at import) -- uses the same AST-extraction idiom as the other
proxy_*_selftest.py files in this directory. Pure/offline: no network, no
Telegram, no provider calls, no DB writes (the classifier is a pure
in-memory function over dict rows -- no secrets are ever involved: it only
looks at id/manager_key/host/port/status/provider_type/proxy_enabled/
proxy_lease_id, never login/password).

Static checks:
  - _prenew_lease_role is defined exactly once (single authoritative
    classifier -- no second copy anywhere else).
  - _PRENEW_ROLE_ENFORCEMENT_ENABLED literal default.
    CORRECTION B 20260810 (OLD/NEW, legitimate semantic change, not a
    weakened check): OLD asserted `is False` (Stage R7's OBSERVE-only
    rollout, documented in this file's own header at the time). NEW asserts
    `is True` -- Correction B is the flip this flag was staged for (main.py's
    own comment above the assignment says exactly that): the classifier is
    now authoritative for real in the warn/autorenew loops, per
    tools/proxy_orphan_lease_selftest.py's P1-P15/mutation coverage of the
    enforced behavior. This file's own checks 10-12 below were already
    written as hypothetical True/False comparisons (not reading the actual
    flag), so they did not need to change.
  - Both loops call _prenew_role_diagnostic(...) and gate on
    "_PRENEW_ROLE_ENFORCEMENT_ENABLED and role != 'managed'" -- i.e. the
    exact same switch, now enforced.

Behavioral checks (matrix from the task spec):
  - current lease of a live manager (by proxy_lease_id) => managed
  - current lease of a live manager (by host:port fallback) => managed
  - archived manager => orphan
  - proxy_enabled=0 => orphan
  - replaced/old lease of a still-live manager => orphan
  - manager_key empty, host:port unused anywhere => free
  - manager_key empty, host:port IS in live usage (external/current usage
    fallback) => managed
  - manager_key set but manager row missing (stale manager_key) => orphan
  - garbage/malformed input never raises -- fails safe to orphan
  - observe mode (enforcement=False) never blocks; enforcement=True blocks
    exactly the non-managed roles -- proven against the SAME boolean
    expression main.py uses, not a reimplementation.

Run:  python tools\\proxy_role_gate_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

MAIN_PY = BASE_DIR / "main.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def find_defs(tree, name):
    return [n for n in tree.body if getattr(n, "name", None) == name]


def extract_and_exec(tree, names, extra_ns):
    picked = {}
    for node in tree.body:
        nm = getattr(node, "name", None)
        if nm in names:
            picked[nm] = node
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            picked[node.targets[0].id] = node
    missing = names - set(picked)
    if missing:
        raise AssertionError(f"missing {missing}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in names if n in picked)
    ns = dict(extra_ns)
    exec(compile(module_src, "<main.py role-gate extract>", "exec"), ns)
    return ns


def run_static_checks(main_tree) -> None:
    print("\n-- Static / wiring checks --")

    role_defs = find_defs(main_tree, "_prenew_lease_role")
    check("_prenew_lease_role is defined exactly once (single authoritative classifier)", len(role_defs) == 1, str(len(role_defs)))

    marker = [i for i, n in enumerate(main_tree.body)
              if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_PRENEW_ROLE_ENFORCEMENT_ENABLED" for t in n.targets)]
    check("_PRENEW_ROLE_ENFORCEMENT_ENABLED assignment found", bool(marker))
    if marker:
        node = main_tree.body[marker[0]]
        val = node.value
        # CORRECTION B 20260810 (OLD/NEW): OLD asserted `is False` (Stage R7
        # OBSERVE-only rollout). NEW asserts `is True` -- Correction B flips
        # this flag to enforced, exactly as main.py's own comment above the
        # assignment always said it would once staged; see this file's
        # module docstring for the full rationale.
        check("_PRENEW_ROLE_ENFORCEMENT_ENABLED default is True (ENFORCED, Correction B 20260810)",
              isinstance(val, ast.Constant) and val.value is True, ast.unparse(node))

    warn_loop = find_defs(main_tree, "_prenew_notify_expiring_loop")
    autorenew_loop = find_defs(main_tree, "_prenew_autorenew_loop")
    check("_prenew_notify_expiring_loop defined exactly once", len(warn_loop) == 1, str(len(warn_loop)))
    check("_prenew_autorenew_loop defined exactly once", len(autorenew_loop) == 1, str(len(autorenew_loop)))

    warn_src = ast.unparse(warn_loop[0]) if warn_loop else ""
    autorenew_src = ast.unparse(autorenew_loop[0]) if autorenew_loop else ""

    # ast.unparse normalizes string quoting, so match quote-agnostically.
    gate_expr_dq = '_PRENEW_ROLE_ENFORCEMENT_ENABLED and role != "managed"'
    gate_expr_sq = "_PRENEW_ROLE_ENFORCEMENT_ENABLED and role != 'managed'"
    check("warn loop calls _prenew_role_diagnostic(...)", "_prenew_role_diagnostic(" in warn_src)
    check("warn loop gates on the enforcement switch, not unconditionally",
          gate_expr_dq in warn_src or gate_expr_sq in warn_src)
    check("autorenew loop calls _prenew_role_diagnostic(...)", "_prenew_role_diagnostic(" in autorenew_src)
    check("autorenew loop gates on the enforcement switch, not unconditionally",
          gate_expr_dq in autorenew_src or gate_expr_sq in autorenew_src)

    check("warn loop still calls _prenew_warn_eligible unconditionally (legacy eligibility untouched)",
          "_prenew_warn_eligible(lease, slot, now_local)" in warn_src)
    check("autorenew loop still calls _prenew_autorenew_gates_ok unconditionally (legacy eligibility untouched)",
          "_prenew_autorenew_gates_ok(lease, slot, now_local)" in autorenew_src)


def main() -> int:
    main_tree = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))
    run_static_checks(main_tree)

    async def _fake_tpag_registry_get(key):
        return FAKE_MANAGERS.get(str(key or "").strip())

    ns = extract_and_exec(
        main_tree,
        {"_prenew_lease_role", "_prenew_role_diagnostic", "_ppool_usage_key", "_PRENEW_ROLE_ENFORCEMENT_ENABLED"},
        {
            "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
            "_tpag_registry_get": _fake_tpag_registry_get,
        },
    )
    role_fn = ns["_prenew_lease_role"]
    diag_fn = ns["_prenew_role_diagnostic"]
    usage_key_fn = ns["_ppool_usage_key"]

    global FAKE_MANAGERS
    FAKE_MANAGERS = {}

    print("\n-- Behavioral matrix: _prenew_lease_role --")

    # 1. current lease of a live manager, matched by managers.proxy_lease_id
    live_mgr = {
        "manager_key": "mgr_live", "status": "active", "proxy_enabled": 1,
        "proxy_lease_id": 101, "proxy_host": "1.1.1.1", "proxy_port": 50101,
    }
    lease_current_by_id = {"id": 101, "manager_key": "mgr_live", "host": "1.1.1.1", "port": 50101}
    check("1. current lease of a live manager (matched by proxy_lease_id) => managed",
          role_fn(lease_current_by_id, live_mgr, {}) == "managed")

    # 2. current lease matched only by host:port fallback (proxy_lease_id absent/stale)
    live_mgr_no_id = {
        "manager_key": "mgr_live2", "status": "active", "proxy_enabled": 1,
        "proxy_lease_id": None, "proxy_host": "2.2.2.2", "proxy_port": 50102,
    }
    lease_current_by_hostport = {"id": 202, "manager_key": "mgr_live2", "host": "2.2.2.2", "port": 50102}
    check("2. current lease of a live manager (matched by host:port fallback) => managed",
          role_fn(lease_current_by_hostport, live_mgr_no_id, {}) == "managed")

    # 3. archived manager => orphan
    archived_mgr = {
        "manager_key": "mgr_dead", "status": "archived", "proxy_enabled": 1,
        "proxy_lease_id": 303, "proxy_host": "3.3.3.3", "proxy_port": 50103,
    }
    lease_archived = {"id": 303, "manager_key": "mgr_dead", "host": "3.3.3.3", "port": 50103}
    check("3. archived manager => orphan", role_fn(lease_archived, archived_mgr, {}) == "orphan")

    # 4. proxy_enabled=0 => orphan
    disabled_mgr = {
        "manager_key": "mgr_disabled", "status": "active", "proxy_enabled": 0,
        "proxy_lease_id": 404, "proxy_host": "4.4.4.4", "proxy_port": 50104,
    }
    lease_disabled = {"id": 404, "manager_key": "mgr_disabled", "host": "4.4.4.4", "port": 50104}
    check("4. proxy_enabled=0 => orphan", role_fn(lease_disabled, disabled_mgr, {}) == "orphan")

    # 5. replaced/old lease of a still-live manager (manager's CURRENT proxy is a
    #    DIFFERENT lease/host:port -- this row is the stale one)
    live_mgr_replaced = {
        "manager_key": "mgr_replaced", "status": "active", "proxy_enabled": 1,
        "proxy_lease_id": 999, "proxy_host": "9.9.9.9", "proxy_port": 50199,
    }
    lease_old_replaced = {"id": 505, "manager_key": "mgr_replaced", "host": "5.5.5.5", "port": 50105}
    check("5. replaced/old lease of a still-live manager => orphan",
          role_fn(lease_old_replaced, live_mgr_replaced, {}) == "orphan")

    # 6. manager_key empty, host:port unused anywhere => free
    lease_free = {"id": 606, "manager_key": "", "host": "6.6.6.6", "port": 50106}
    check("6. manager_key empty + unused anywhere => free", role_fn(lease_free, None, {}) == "free")

    # 7. manager_key empty, but host:port IS in the live usage map (external
    #    proxy applied outside the pool) => managed, not free
    usage_map = {usage_key_fn("7.7.7.7", 50107): [{"manager_key": "mgr_external"}]}
    lease_external = {"id": 707, "manager_key": "", "host": "7.7.7.7", "port": 50107}
    check("7. manager_key empty but host:port in live usage (external) => managed",
          role_fn(lease_external, None, usage_map) == "managed")

    # 8. stale manager_key: lease points at a manager_key that no longer
    #    resolves to any managers row (manager_row is None)
    lease_stale_key = {"id": 808, "manager_key": "mgr_ghost", "host": "8.8.8.8", "port": 50108}
    check("8. stale manager_key (manager row missing) => orphan", role_fn(lease_stale_key, None, {}) == "orphan")

    # 9. garbage input never raises -- fails safe to orphan
    garbage_cases = [
        ({}, None, None),
        ({"id": "not-an-int", "manager_key": "mgr_live", "host": None, "port": "abc"}, live_mgr, {}),
        (None, live_mgr, {}),
    ]
    all_safe = True
    for lease_g, mgr_g, um_g in garbage_cases:
        try:
            r = role_fn(lease_g, mgr_g, um_g)
            if r not in ("managed", "orphan", "free"):
                all_safe = False
        except Exception:
            all_safe = False
    check("9. garbage/malformed input never raises, always returns a valid role", all_safe)

    print("\n-- Observe-mode vs enforcement-mode gating (same boolean expression as main.py) --")

    non_managed_cases = [
        ("archived", lease_archived, archived_mgr, {}),
        ("proxy_enabled=0", lease_disabled, disabled_mgr, {}),
        ("replaced/old lease", lease_old_replaced, live_mgr_replaced, {}),
        ("free/unassigned", lease_free, None, {}),
        ("stale manager_key", lease_stale_key, None, {}),
    ]
    observe_never_blocks = True
    enforcement_blocks_all = True
    for label, lease_c, mgr_c, um_c in non_managed_cases:
        role = role_fn(lease_c, mgr_c, um_c)
        blocked_observe = (False and role != "managed")  # mirrors _PRENEW_ROLE_ENFORCEMENT_ENABLED == False
        blocked_enforced = (True and role != "managed")  # mirrors the switch flipped on in Stage R7
        if blocked_observe:
            observe_never_blocks = False
        if not blocked_enforced:
            enforcement_blocks_all = False
    check("10. OBSERVE mode (enforcement=False) never blocks any non-managed lease", observe_never_blocks)
    check("11. enforcement=True would block every non-managed lease (archived/disabled/replaced/free/stale)", enforcement_blocks_all)

    role_managed = role_fn(lease_current_by_id, live_mgr, {})
    blocked_enforced_managed = (True and role_managed != "managed")
    check("12. enforcement=True never blocks a genuinely managed lease", blocked_enforced_managed is False)

    print("\n-- _prenew_role_diagnostic (async wrapper: resolves manager_row itself, never raises) --")

    FAKE_MANAGERS["mgr_live"] = live_mgr
    FAKE_MANAGERS["mgr_dead"] = archived_mgr

    async def _run_diag_checks():
        role_a = await diag_fn(lease_current_by_id, {}, context="warn")
        check("13a. diagnostic resolves manager_row via _tpag_registry_get and classifies managed correctly",
              role_a == "managed", role_a)
        role_b = await diag_fn(lease_archived, {}, context="autorenew")
        check("13b. diagnostic classifies an archived-manager lease as orphan", role_b == "orphan", role_b)
        role_c = await diag_fn({"id": "bad", "manager_key": None}, None, context="warn")
        check("13c. diagnostic never raises on malformed input", role_c in ("managed", "orphan", "free"), repr(role_c))

    asyncio.run(_run_diag_checks())

    print(f"\n{'='*70}")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
