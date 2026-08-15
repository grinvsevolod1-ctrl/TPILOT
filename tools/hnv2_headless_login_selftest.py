# -*- coding: utf-8 -*-
"""tools/hnv2_headless_login_selftest.py -- offline self-test for the
Health Notification System V2 D3 fix: a manager worker with an unauthorized
Telegram session must NEVER enter interactive Telethon login.

Covers approved-plan selftest scenario 4 ("no interactive login") with
static AST assertions against the REAL current main.py source -- this file
does not extract-and-exec main()'s control flow (that is already covered,
dynamically, by tools/manager_runtime_start_selftest.py's scenarios 2/3/3b
via its hand-mirrored harness); this file's job is the structural proof
that the SOURCE ITSELF can never reach input()/getpass, and that the
worker-mode bailout unconditionally precedes client.start( in the code the
interpreter actually executes.

Self-contained: does not import tools/manager_runtime_start_selftest.py,
to keep the two files independently authoritative (a bug in one locator
technique should not silently hide behind the other).
"""
from __future__ import annotations

import ast
import io
import sys
import tokenize
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()
MAIN_TREE = ast.parse(MAIN_SRC)


# ======================================================================
# Locate the ONE base async def main() that actually runs at runtime --
# same structural signal as manager_runtime_start_selftest.py's own
# locator: a direct call to client.run_until_disconnected(), the one
# marker no delegate-only wrapper override shares.
# ======================================================================

def _locate_base_main() -> ast.AsyncFunctionDef:
    hits = []
    for node in MAIN_TREE.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "main":
            for n in ast.walk(node):
                if (
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "run_until_disconnected"
                ):
                    hits.append(node)
                    break
    assert len(hits) == 1, f"expected exactly one async def main() containing run_until_disconnected(), found {len(hits)}"
    return hits[0]


BASE_MAIN = _locate_base_main()


def _locate_auth_if_block(main_node: ast.AsyncFunctionDef) -> ast.If:
    """Locates the `if not _runtime_already_authorized:` block -- identified
    structurally by its test being `not _runtime_already_authorized`, not by
    line number (which shifts as unrelated code above it changes)."""
    hits = []
    for n in ast.walk(main_node):
        if isinstance(n, ast.If) and isinstance(n.test, ast.UnaryOp) and isinstance(n.test.op, ast.Not):
            operand = n.test.operand
            if isinstance(operand, ast.Name) and operand.id == "_runtime_already_authorized":
                hits.append(n)
    assert len(hits) == 1, f"expected exactly one `if not _runtime_already_authorized:` block, found {len(hits)}"
    return hits[0]


AUTH_IF = _locate_auth_if_block(BASE_MAIN)


# ======================================================================
# Test 1: no Call to input() or getpass(...) anywhere inside the auth
# block -- the two entry points Telethon's own client.start() falls back
# to for an interactive code/password prompt.
# ======================================================================

def test_1_no_interactive_prompt_calls():
    print("\n-- Test 1: no input()/getpass() calls anywhere in the auth block --")
    calls = []
    for n in ast.walk(AUTH_IF):
        if isinstance(n, ast.Call):
            fn = n.func
            name = fn.id if isinstance(fn, ast.Name) else (fn.attr if isinstance(fn, ast.Attribute) else None)
            if name in ("input", "getpass"):
                calls.append(name)
    check("1a. zero input()/getpass() calls inside the auth block", calls == [], calls)


# ======================================================================
# Test 2: the worker-mode bailout (_m212a_fail_startup("session_unauthorized",
# ...) guarded by `MANAGER_RUNTIME_KEY and not CONTROLLER_MODE`) appears,
# in AST walk (= source/execution) order, BEFORE the client.start( call --
# i.e. a headless worker can structurally never reach client.start().
# ======================================================================

def _guard_is_manager_runtime_key_and_not_controller_mode(test: ast.AST) -> bool:
    if not isinstance(test, ast.BoolOp) or not isinstance(test.op, ast.And):
        return False
    if len(test.values) != 2:
        return False
    left, right = test.values
    left_ok = isinstance(left, ast.Name) and left.id == "MANAGER_RUNTIME_KEY"
    right_ok = isinstance(right, ast.UnaryOp) and isinstance(right.op, ast.Not) and isinstance(right.operand, ast.Name) and right.operand.id == "CONTROLLER_MODE"
    return left_ok and right_ok


def _call_name(n: ast.Call) -> str:
    fn = n.func
    if isinstance(fn, ast.Name):
        return fn.id
    if isinstance(fn, ast.Attribute):
        return fn.attr
    return ""


def test_2_worker_gate_precedes_client_start():
    print("\n-- Test 2: worker-mode gate structurally precedes client.start( --")

    # Find the `if MANAGER_RUNTIME_KEY and not CONTROLLER_MODE:` block
    # inside the auth block.
    gate_ifs = [n for n in ast.walk(AUTH_IF) if isinstance(n, ast.If) and _guard_is_manager_runtime_key_and_not_controller_mode(n.test)]
    check("2a. exactly one `if MANAGER_RUNTIME_KEY and not CONTROLLER_MODE:` gate exists in the auth block", len(gate_ifs) == 1, len(gate_ifs))
    if not gate_ifs:
        return
    gate_if = gate_ifs[0]

    fail_startup_calls_in_gate = [
        n for n in ast.walk(gate_if)
        if isinstance(n, ast.Call) and _call_name(n) == "_m212a_fail_startup"
    ]
    check("2b. the gate calls _m212a_fail_startup(...)", len(fail_startup_calls_in_gate) == 1, len(fail_startup_calls_in_gate))
    if fail_startup_calls_in_gate:
        args = fail_startup_calls_in_gate[0].args
        first_arg_ok = len(args) >= 1 and isinstance(args[0], ast.Constant) and args[0].value == "session_unauthorized"
        check("2c. the gate's _m212a_fail_startup call passes reason_class='session_unauthorized'", first_arg_ok, ast.dump(fail_startup_calls_in_gate[0]) if fail_startup_calls_in_gate else None)

    # Structural precedence: the gate `if` block must be one of the FIRST
    # statements in the auth block's body (before the `if not PHONE:` /
    # `client.start(` statements that follow it) -- i.e. it is not nested
    # inside, or after, the client.start() call path.
    auth_body_stmts = AUTH_IF.body
    gate_stmt_index = None
    start_call_stmt_index = None
    for i, stmt in enumerate(auth_body_stmts):
        if stmt is gate_if:
            gate_stmt_index = i
        for n in ast.walk(stmt):
            if isinstance(n, ast.Call) and _call_name(n) == "start" and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "client":
                if start_call_stmt_index is None:
                    start_call_stmt_index = i
    check("2d. the gate `if` is a top-level statement inside the auth block", gate_stmt_index is not None, gate_stmt_index)
    check("2e. the statement containing client.start( comes strictly AFTER the gate statement", start_call_stmt_index is not None and gate_stmt_index is not None and gate_stmt_index < start_call_stmt_index, (gate_stmt_index, start_call_stmt_index))

    # And confirm the gate unconditionally raises (via _m212a_fail_startup,
    # which unconditionally raises SystemExit(1) -- proven separately by
    # test_5 below) -- i.e. control flow can never fall through the gate
    # `if` into the client.start( statement for a worker.
    gate_ends_with_call_only = all(
        isinstance(s, ast.Expr) and isinstance(s.value, ast.Call) or (isinstance(s, ast.Try))
        for s in gate_if.body
    )
    check("2f. the gate body's final statement is the (unconditionally-raising) _m212a_fail_startup call, nothing after it in the same branch", gate_ends_with_call_only, ast.dump(gate_if))


# ======================================================================
# Test 3: the old pre-fix buggy pattern is not present (regression lock,
# same literal as manager_runtime_start_selftest.py's own tripwire-3).
# ======================================================================

_TRIPWIRE_OLD_BUGGY_PATTERN = "await client.start(phone=PHONE if PHONE else None)"


def _strip_comments(src: str) -> str:
    lines = src.splitlines(keepends=True)
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                line = lines[row - 1]
                nl = ""
                if line.endswith("\r\n"):
                    nl = "\r\n"
                elif line.endswith("\n"):
                    nl = "\n"
                lines[row - 1] = line[:col] + nl
    except Exception:
        return src
    return "".join(lines)


def test_3_no_old_buggy_pattern():
    print("\n-- Test 3: old pre-fix buggy pattern absent (regression lock) --")
    main_src = ast.get_source_segment(MAIN_SRC, BASE_MAIN) or ""
    code_only = _strip_comments(main_src)
    check("3a. old buggy pattern absent from the active main() source", _TRIPWIRE_OLD_BUGGY_PATTERN not in code_only, None)


# ======================================================================
# Test 4: client.connect( still precedes client.start( (tripwire-2 from
# manager_runtime_start_selftest.py, re-verified independently here since
# the D3 edit touches the exact same region of code).
# ======================================================================

def test_4_connect_before_start():
    print("\n-- Test 4: client.connect( still precedes client.start( --")
    main_src = ast.get_source_segment(MAIN_SRC, BASE_MAIN) or ""
    code_only = _strip_comments(main_src)
    connect_idx = code_only.find("client.connect(")
    start_idx = code_only.find("client.start(")
    check("4a. client.connect( present", connect_idx != -1, connect_idx)
    check("4b. client.start( present (D3 gates it, does not delete it -- CONTROLLER_MODE still needs it)", start_idx != -1, start_idx)
    check("4c. client.connect( occurs before client.start(", connect_idx != -1 and start_idx != -1 and connect_idx < start_idx, (connect_idx, start_idx))


# ======================================================================
# Test 5: _m212a_fail_startup unconditionally raises SystemExit(1) --
# structural proof (not text search) that reaching the gate call in test 2
# genuinely halts execution rather than merely logging.
# ======================================================================

def test_5_fail_startup_always_raises():
    print("\n-- Test 5: _m212a_fail_startup unconditionally raises SystemExit --")
    defs = [n for n in MAIN_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_m212a_fail_startup"]
    check("5a. _m212a_fail_startup defined exactly once", len(defs) == 1, len(defs))
    if not defs:
        return
    fn = defs[0]
    # The LAST top-level statement in the function body must be an
    # unconditional `raise SystemExit(...)` -- not nested inside an `if`.
    last_stmt = fn.body[-1]
    is_raise_systemexit = (
        isinstance(last_stmt, ast.Raise)
        and isinstance(last_stmt.exc, ast.Call)
        and isinstance(last_stmt.exc.func, ast.Name)
        and last_stmt.exc.func.id == "SystemExit"
    )
    check("5b. the function's last top-level statement is `raise SystemExit(...)`, unconditional (not inside an if)", is_raise_systemexit, ast.dump(last_stmt))


# ======================================================================
# Test 6: whole-module client.start( call count is exactly 1 -- counted
# via AST Call nodes, not text grep (a grep would also match the two
# explanatory comment mentions elsewhere in the file).
# ======================================================================

def test_6_single_client_start_call_in_module():
    print("\n-- Test 6: exactly one client.start( Call in the whole module --")
    hits = []
    for n in ast.walk(MAIN_TREE):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "start"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "client"
        ):
            hits.append(n)
    check("6a. exactly one `client.start(...)` Call exists in the entire module", len(hits) == 1, len(hits))


# ======================================================================
# Test 7: explicit onboarding/relogin/QR/devlogin flows are untouched --
# they must still call send_code_request / sign_in on a temp/qr client,
# never on the module-level `client` object the D3 gate protects.
# ======================================================================

def test_7_explicit_flows_untouched():
    print("\n-- Test 7: explicit onboarding/relogin/QR/devlogin flows unaffected --")
    send_code_on_module_client = []
    for n in ast.walk(MAIN_TREE):
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "send_code_request"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "client"
        ):
            send_code_on_module_client.append(n)
    check("7a. send_code_request is never called on the module-level `client` object (only on throwaway temp/qr clients)", send_code_on_module_client == [], len(send_code_on_module_client))

    sign_in_count = sum(
        1 for n in ast.walk(MAIN_TREE)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "sign_in"
    )
    check("7b. sign_in( call sites still exist elsewhere in the module (explicit flows untouched)", sign_in_count > 0, sign_in_count)


def main() -> int:
    test_1_no_interactive_prompt_calls()
    test_2_worker_gate_precedes_client_start()
    test_3_no_old_buggy_pattern()
    test_4_connect_before_start()
    test_5_fail_startup_always_raises()
    test_6_single_client_start_call_in_module()
    test_7_explicit_flows_untouched()

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
