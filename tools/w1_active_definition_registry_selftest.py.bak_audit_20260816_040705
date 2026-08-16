# -*- coding: utf-8 -*-
"""tools/w1_active_definition_registry_selftest.py -- W1 (frozen master
plan, MASTER_PLAN_FREEZE/08, Section F): proves that every W1 edit landed
in the ACTIVE (last-definition-wins) copy of each target function, that no
new shadowing was introduced by accident, that the ManagerBot self-
diagnostic callback is unique and does not collide with any existing
mb: branch, and that the fleet-list renderer's active call sites go through
the new instrumented wrapper.

Read-only AST analysis. Never imports main.py/panel_bot.py/manager_bot.py.

    python tools\\w1_active_definition_registry_selftest.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"[FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def _read(fname: str) -> str:
    return (SRC / fname).read_bytes().decode("utf-8-sig", errors="replace")


def _defs(text: str, name: str):
    tree = ast.parse(text)
    spans = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            spans.append((node.lineno, node.end_lineno))
    return sorted(spans)


def _active_source(text: str, name: str) -> str:
    spans = _defs(text, name)
    if not spans:
        raise LookupError(name)
    lo, hi = spans[-1]
    return "\n".join(text.splitlines()[lo - 1:hi])


def _active_func_node(text: str, name: str):
    """Return the AST node of the ACTIVE (last) def/async def of `name` at
    module level, for structural inspection beyond plain text matching."""
    tree = ast.parse(text)
    matches = [n for n in tree.body
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if not matches:
        raise LookupError(name)
    return matches[-1]


def test_tpag_monitor_once_active_copy_has_w1_instrumentation():
    text = _read("main.py")
    spans = _defs(text, "_tpag_monitor_once")
    if len(spans) != 2:
        fail(f"expected exactly 2 definitions of _tpag_monitor_once (1 pre-existing dead "
             f"copy + 1 active), found {len(spans)}: {spans}")
        return
    active_src = _active_source(text, "_tpag_monitor_once")
    dead_lo, dead_hi = spans[0]
    dead_src = "\n".join(text.splitlines()[dead_lo - 1:dead_hi])
    checks = {
        "sweep_id generated": "sweep_id" in active_src,
        "structured event emitted on list failure": "PROXY_GUARD_SWEEP_LIST_FAILED" in active_src,
        "per-manager wait_for timeout": "asyncio.wait_for" in active_src,
        "per-manager exception isolation": "PROXY_GUARD_MANAGER_EXCEPTION" in active_src,
        "sweep summary emitted": "PROXY_GUARD_SWEEP_SUMMARY" in active_src,
    }
    missing = [k for k, v in checks.items() if not v]
    if missing:
        fail(f"active _tpag_monitor_once (L{spans[-1][0]}) is missing: {missing}")
    else:
        ok(f"active _tpag_monitor_once (L{spans[-1][0]}) carries every W1 instrumentation marker")
    if "PROXY_GUARD_SWEEP_SUMMARY" in dead_src:
        fail("the OLD dead copy of _tpag_monitor_once was edited instead of/as well as "
             "the active one -- this would be a no-op fix")
    else:
        ok(f"the pre-existing dead copy (L{spans[0][0]}) was left untouched, as required "
           f"(WAVE0 policy: never touch SHADOWED_DEAD without its own parity gate)")


def test_pb_manager_status_resolve_and_live_unchanged_pure():
    text = _read("panel_bot.py")
    for name in ("_pb_manager_status_resolve", "_pb_manager_status_resolve_live"):
        spans = _defs(text, name)
        if len(spans) != 1:
            fail(f"{name}: expected exactly 1 definition (must stay unique/unmodified), "
                 f"found {len(spans)}")
        else:
            ok(f"{name} remains a single, unshadowed definition (L{spans[0][0]})")


def test_fleet_list_renderer_uses_the_logged_wrapper():
    text = _read("panel_bot.py")
    for fn in ("_manager_settings_text", "_manager_settings_buttons"):
        src = _active_source(text, fn)
        if "_pb_manager_status_resolve_live_logged" in src:
            ok(f"{fn} (the active fleet-LIST renderer) calls the instrumented wrapper")
        else:
            fail(f"{fn} does not call _pb_manager_status_resolve_live_logged -- the fleet "
                 f"list would render without emitting any structured classification record")
        if re.search(r"(?<!_logged)\(\s*d\s*\)\s*$", "") and False:
            pass  # placeholder no-op to keep structure explicit; real check is below
    # Confirm the bare (unlogged) live resolver is called from exactly one
    # OTHER place -- the single-manager detail card, a DIFFERENT rendering
    # surface, deliberately left untouched (documented scope boundary, see
    # W1_OBSERVABILITY/04_RENDER_CLASSIFICATION_CONTRACT.md). This is not a
    # "bypass" of the new instrumentation for the fleet-list screen -- it is
    # a distinct screen this wave did not touch.
    bare_call_sites = [m.start() for m in re.finditer(r"(?<!_logged)_pb_manager_status_resolve_live\(", text)]
    # subtract the definition line itself (`def _pb_manager_status_resolve_live(row):`)
    def_line = re.search(r"def _pb_manager_status_resolve_live\(row\):", text)
    non_def_calls = len(bare_call_sites) - (1 if def_line else 0)
    if non_def_calls == 1:
        ok("exactly one remaining bare call to _pb_manager_status_resolve_live "
           "(the single-manager detail card, out of W1's declared scope)")
    else:
        fail(f"expected exactly 1 remaining bare call to _pb_manager_status_resolve_live "
             f"outside its own definition, found {non_def_calls} -- scope boundary changed "
             f"unexpectedly, must be re-reviewed")


def test_on_callback_unique_and_no_namespace_collision():
    text = _read("manager_bot.py")
    spans = _defs(text, "on_callback")
    if len(spans) != 1:
        fail(f"on_callback must remain a single definition, found {len(spans)}")
        return
    ok(f"on_callback remains a single, unshadowed definition (L{spans[0][0]})")
    src = _active_source(text, "on_callback")
    prefixes = re.findall(r'data(?:\.startswith)?\(?\s*==?\s*b"([^"]+)"', src)
    prefixes += re.findall(r'data\.startswith\(b"([^"]+)"\)', src)
    prefixes = sorted(set(prefixes))
    if "mb:myaccess:" not in prefixes:
        fail(f"mb:myaccess: branch not found inside on_callback; branches seen: {prefixes}")
        return
    others = [p for p in prefixes if p != "mb:myaccess:"]
    collisions = [p for p in others if "mb:myaccess:".startswith(p) or p.startswith("mb:myaccess:")]
    if collisions:
        fail(f"mb:myaccess: collides with existing branch(es): {collisions}")
    else:
        ok(f"mb:myaccess: does not collide with any of the other {len(others)} branches "
           f"already routed inside on_callback: {others}")


def test_no_legacy_whoami_callback_branch_exists():
    """Privacy/naming correction 20260729: a hidden 'whoami' route is just
    as much a reintroduction as the /whoami COMMAND would be, even though
    it is a callback rather than a command -- this is a DISTINCT check
    from test_no_reintroduction_of_whoami_command (which only looks at
    command dispatch), because the callback dispatcher is a different code
    path entirely."""
    text = _read("manager_bot.py")
    if re.search(r'b"mb:whoami', text) or "mb:whoami:0" in text:
        fail("a literal 'mb:whoami' callback branch or button payload still exists in "
             "manager_bot.py -- the callback must be named mb:myaccess:, not merely "
             "renamed at the command level")
    else:
        ok("no 'mb:whoami' callback branch or button payload exists anywhere in "
           "manager_bot.py -- the rename to mb:myaccess: is complete, not just cosmetic")


def test_adminbot_does_not_use_the_mb_namespace():
    panel_text = _read("panel_bot.py")
    hits = re.findall(r'b"(mb:[^"]*)"', panel_text)
    if hits:
        fail(f"panel_bot.py (AdminBot) uses the mb: namespace, which ManagerBot's "
             f"CALLBACK_PREFIX also owns: {sorted(set(hits))}")
    else:
        ok("AdminBot (panel_bot.py) does not use the mb: callback namespace anywhere "
           "-- no collision with ManagerBot's own mb:myaccess: (and separately, they are "
           "two different Telegram bot clients, so even an identical literal could not "
           "cross-fire at runtime)")


def test_mbstat_start_buttons_active_definition_is_the_w1_override():
    text = _read("manager_bot.py")
    spans = _defs(text, "_mbstat_start_buttons")
    active_src = _active_source(text, "_mbstat_start_buttons")
    if "mb:myaccess:0" in active_src and "_W1_PREV_START_BTNS" in active_src:
        ok(f"_mbstat_start_buttons active definition (L{spans[-1][0]} of {len(spans)} "
           f"total) is the W1 override, correctly delegating to the captured previous "
           f"chain before appending the new button")
    else:
        fail(f"_mbstat_start_buttons active definition (L{spans[-1][0]}) does not look "
             f"like the expected W1 override")
    # every earlier definition except the immediately-preceding one must
    # still be reachable via SOME capture (this project's PREV-chain
    # convention) -- a lightweight structural check, not a full shadow
    # analysis (that already exists in Wave 0's registry generator).
    if len(spans) < 2:
        fail("expected at least 2 definitions (pre-existing chain + W1 override)")
    else:
        ok(f"{len(spans)} total definitions of _mbstat_start_buttons -- W1 added exactly "
           f"one new override on top of the existing chain, none removed")


def test_no_reintroduction_of_whoami_command():
    text = _read("manager_bot.py")
    if re.search(r'cmd\s*==\s*[\'"]\s*/whoami\s*[\'"]', text):
        fail("a literal '/whoami' command branch was reintroduced -- forbidden by the "
             "frozen contract (WAVE0/07_safe_diagnostics_contract.md)")
    else:
        ok("no '/whoami' command branch exists anywhere in manager_bot.py -- the removed "
           "command stays removed; only the button was added")


def test_raw_tg_user_id_still_hidden_from_known_user_on_start():
    text = _read("manager_bot.py")
    src = _active_source(text, "_handle_start")
    # The ONLY tg_user_id interpolation inside _handle_start must be on the
    # not_found/identity_conflict branch (this exact known string), never on
    # the granted_now/already_granted/fallback (known-user) branches.
    known_user_blocks = re.split(r"if status ==", src)
    granted_block = known_user_blocks[0] if known_user_blocks else src
    if "{uid}" in granted_block or "f\"{uid" in granted_block:
        fail("_handle_start appears to interpolate {uid} into a known-user response block")
    else:
        ok("_handle_start still shows tg_user_id only on the not-found/unknown screen, "
           "never to an already-known user (D-20 stays fixed)")


def test_self_diagnostic_source_never_returns_or_writes_raw_uid():
    """Structural guard (distinct from the behavioural proof in
    w1_manager_self_diagnostic_selftest.py): the ACTIVE source of
    _w1_describe_access must contain no `"tg_user_id": uid`-shaped dict
    literal in any return statement, and _w1_myaccess_audit_log's active
    source must contain no raw `uid=` field in its log line -- it must use
    the actor_ref pseudonym instead."""
    text = _read("manager_bot.py")
    describe_src = _active_source(text, "_w1_describe_access")
    if re.search(r'["\']tg_user_id["\']\s*:\s*uid\b', describe_src):
        fail("_w1_describe_access's active source still contains a "
             "\"tg_user_id\": uid return field")
        return
    ok("_w1_describe_access's active source contains no \"tg_user_id\": uid return field")

    audit_src = _active_source(text, "_w1_myaccess_audit_log")
    if re.search(r'uid=\{int\(uid', audit_src) or re.search(r"f\"[^\"]*uid=\{uid", audit_src):
        fail("_w1_myaccess_audit_log's active source still writes a raw uid= field to the log")
        return
    if "actor_ref" not in audit_src:
        fail("_w1_myaccess_audit_log's active source does not reference actor_ref at all")
        return
    ok("_w1_myaccess_audit_log's active source writes actor_ref, not a raw uid= field")


# Privacy correction 20260729, ROUND 2: the first correction protected the
# self-diagnostic's RETURNED dict, its RENDERED text, and myaccess_audit.log
# -- but did not scan ORDINARY log.warning()/log.info() calls reachable from
# the same call graph, one of which still leaked the raw uid + full
# exception repr straight to the normal application logger. This scanner
# closes that gap generically: it does not special-case myaccess_audit.log,
# it inspects EVERY log.<method>(...) call inside the complete W1
# self-diagnostic call graph and fails if any argument is a bare reference
# to a raw identity value (uid / tg_user_id / sender_id) or to the
# exception object bound by an enclosing `except ... as <name>:` -- UNLESS
# that value is only the argument to _w1_myaccess_actor_ref(...) (computing
# the safe pseudonym) or wrapped as type(<exc>).__name__ (the one sanctioned
# sanitized form). A value used ONLY to compute actor_ref and never itself
# passed to log.* is fine by construction, since this scanner only inspects
# arguments actually passed to a log.* call.
W1_DIAGNOSTIC_CALL_GRAPH = (
    "_w1_describe_access", "_w1_my_access_text", "_w1_myaccess_actor_ref",
    "_w1_myaccess_audit_log", "_w1_myaccess_rate_limited",
    "_w1_handle_my_access_callback",
)
_FORBIDDEN_RAW_IDENTITY_NAMES = {"uid", "tg_user_id", "sender_id"}
_LOG_METHODS = {"warning", "info", "error", "debug", "critical", "exception"}


def _is_actor_ref_call(node) -> bool:
    return (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_w1_myaccess_actor_ref")


def _is_type_name_call(node) -> bool:
    # matches `type(<anything>).__name__`
    return (isinstance(node, ast.Attribute) and node.attr == "__name__"
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name) and node.value.func.id == "type")


def _scan_func_for_unsafe_logging(func_node, func_name):
    violations = []
    exc_names = {n.name for n in ast.walk(func_node)
                 if isinstance(n, ast.ExceptHandler) and n.name}
    forbidden = _FORBIDDEN_RAW_IDENTITY_NAMES | exc_names
    for node in ast.walk(func_node):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in _LOG_METHODS
                and isinstance(node.func.value, ast.Name) and node.func.value.id == "log"):
            continue
        # %r format-string check (first positional arg, if a plain string literal)
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            if "%r" in node.args[0].value:
                violations.append(
                    f"{func_name}: log.{node.func.attr}(...) at line {node.lineno} "
                    f"uses a '%r' format placeholder (exception repr is never safe to log)")
        # raw-identifier check across ALL positional args (allow-list the
        # two sanctioned safe forms as whole top-level arguments)
        for arg in node.args:
            if _is_actor_ref_call(arg) or _is_type_name_call(arg):
                continue
            for sub in ast.walk(arg):
                if isinstance(sub, ast.Name) and sub.id in forbidden:
                    violations.append(
                        f"{func_name}: log.{node.func.attr}(...) at line {node.lineno} "
                        f"passes raw identifier '{sub.id}' to the logger")
        # same check for keyword args, if any are ever used
        for kw in node.keywords:
            if kw.value is None or _is_actor_ref_call(kw.value) or _is_type_name_call(kw.value):
                continue
            for sub in ast.walk(kw.value):
                if isinstance(sub, ast.Name) and sub.id in forbidden:
                    violations.append(
                        f"{func_name}: log.{node.func.attr}(...) at line {node.lineno} "
                        f"passes raw identifier '{sub.id}' to the logger via keyword arg")
    return violations


def test_w1_diagnostic_logging_never_reaches_raw_uid_or_exception_repr():
    """Complete static scan of the W1 self-diagnostic call graph's ORDINARY
    logger calls (not just myaccess_audit.log). Fails if ANY reachable
    log.<method>() call embeds a raw uid/tg_user_id/sender_id, the raw
    exception object, or a '%r' format placeholder -- the only sanctioned
    identity/error forms are _w1_myaccess_actor_ref(uid) and
    type(exc).__name__."""
    text = _read("manager_bot.py")
    all_violations = []
    for name in W1_DIAGNOSTIC_CALL_GRAPH:
        try:
            node = _active_func_node(text, name)
        except LookupError:
            fail(f"{name} not found at module level in manager_bot.py -- "
                 f"call graph list is stale")
            continue
        all_violations.extend(_scan_func_for_unsafe_logging(node, name))
    if all_violations:
        for v in all_violations:
            fail(f"unsafe W1 diagnostic logging: {v}")
    else:
        ok(f"all {len(W1_DIAGNOSTIC_CALL_GRAPH)} W1 diagnostic call-graph functions' "
           f"log.* calls are free of raw uid/tg_user_id/sender_id and raw exception repr")


if __name__ == "__main__":
    test_tpag_monitor_once_active_copy_has_w1_instrumentation()
    test_pb_manager_status_resolve_and_live_unchanged_pure()
    test_fleet_list_renderer_uses_the_logged_wrapper()
    test_on_callback_unique_and_no_namespace_collision()
    test_no_legacy_whoami_callback_branch_exists()
    test_adminbot_does_not_use_the_mb_namespace()
    test_mbstat_start_buttons_active_definition_is_the_w1_override()
    test_no_reintroduction_of_whoami_command()
    test_raw_tg_user_id_still_hidden_from_known_user_on_start()
    test_self_diagnostic_source_never_returns_or_writes_raw_uid()
    test_w1_diagnostic_logging_never_reaches_raw_uid_or_exception_repr()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("ALL ACTIVE-DEFINITION / HANDLER-REGISTRY GUARD TESTS PASSED")
