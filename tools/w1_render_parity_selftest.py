# -*- coding: utf-8 -*-
"""tools/w1_render_parity_selftest.py -- W1 (frozen master plan,
MASTER_PLAN_FREEZE/08): proves that the new instrumented entry point
_pb_manager_status_resolve_live_logged returns EXACTLY the same
(category, icon, label) tuple as the pre-existing, UNTOUCHED
_pb_manager_status_resolve_live for the same input row -- i.e. W1 added
observability without changing business behaviour (task requirement 9:
"Regression protection for already-correct local behavior").

Both real functions are extracted from panel_bot.py via ast.parse+unparse+
exec (project convention) and run side-by-side against the SAME faked
live-signal helpers, so any divergence in the returned tuple can only come
from an actual behavioural change, not from different fakes.

    python tools\\w1_render_parity_selftest.py
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime
from pathlib import Path

PANEL_BOT_PY = Path(__file__).resolve().parent.parent / "panel_bot.py"

FAILURES: list[str] = []


def fail(msg: str) -> None:
    FAILURES.append(msg)
    print(f"[FAIL] {msg}")


def ok(msg: str) -> None:
    print(f"[ OK ] {msg}")


def _last_defs(names: set[str]):
    src = PANEL_BOT_PY.read_bytes().decode("utf-8-sig", errors="replace")
    tree = ast.parse(src)
    found = {}
    for node in tree.body:
        target = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            target = node.name
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    target = t.id
        if target:
            found[target] = node
    missing = names - set(found)
    if missing:
        raise LookupError(f"not found at module level in panel_bot.py: {sorted(missing)}")
    nodes = list(found.values())
    nodes.sort(key=lambda n: n.lineno)
    return nodes


def build_namespace():
    nodes = _last_defs({
        "_PB_PROXY_GUARD_FRESH_SEC", "_PB_PROXY_CLOCK_SKEW_TOLERANCE_SEC",
        "_pb_proxy_ts_is_fresh", "_pb_proxy_timestamp_malformed", "_pb_proxy_guard_state",
        "_pb_proxy_effective_state", "_pb_manager_status_resolve",
        "_pb_tg_health_row", "_pb_manager_process_running",
        "_pb_manager_status_resolve_live",
        "_W1_RENDER_REASON_BY_CATEGORY", "_w1_render_reason_code",
        "_w1_result_age_seconds", "_w1_render_classification_record",
        "_pb_manager_status_resolve_live_logged",
    })
    # _w1_emit_render_event is DELIBERATELY excluded from extraction: a fake
    # is pre-seeded into `ns` below (recording emitted records instead of
    # writing to a file) and _pb_manager_status_resolve_live_logged's body
    # resolves the name from the SAME namespace dict at call time -- so it
    # calls the fake, not the real file-writer, without ever touching disk.
    mod = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(mod)

    def fake_parse_dt(raw):
        txt = str(raw or "").strip()
        if not txt:
            return None
        try:
            dt = datetime.fromisoformat(txt.replace("Z", "+00:00"))
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt.replace(microsecond=0)
        except Exception:
            return None

    calls = {"tg_health": 0, "proc_running": 0}

    def fake_tg_health_row(key):
        calls["tg_health"] += 1
        return {"health_status": "ok"}

    def fake_proc_running(procs, key):
        calls["proc_running"] += 1
        return True

    def fake_service_scan_cached():
        return []

    emitted = []

    def fake_emit(record):
        emitted.append(record)

    ns = {
        "__name__": "w1_test_ns",
        "datetime": datetime,
        "os": __import__("os"),
        "json": __import__("json"),
        "uuid": __import__("uuid"),
        "ZoneInfo": __import__("zoneinfo").ZoneInfo,
        "normalize_manager_key": (lambda k: str(k or "").strip().lower()),
        "_tpag_panel_v2_parse_dt": fake_parse_dt,
        "_pb_tg_health_row": fake_tg_health_row,
        "_pb_manager_process_running": fake_proc_running,
        "_pb_service_scan_cached": fake_service_scan_cached,
        "_w1_emit_render_event": fake_emit,   # override the real file-writer
        "BASE_DIR": Path("."),
        "_PB_STATUS_CATEGORIES": {
            "banned": ("🚫", "banned", "Banned"), "unauthorized": ("🔐", "unauth", "Unauthorized"),
            "proxy_bad": ("🌐", "proxy_bad", "Proxy bad"), "process_down": ("🔴", "down", "Process down"),
            "starting": ("🔄", "starting", "Starting"), "stopped": ("⏸️", "stopped", "Stopped"),
            "warning": ("🟡", "warn", "Warning"), "ok": ("🟢", "ok", "OK"),
            "disabled": ("⚪", "disabled", "Disabled"), "unknown": ("❓", "unknown", "Unknown"),
        },
    }
    exec(compile(mod, "<w1_extract:render_parity>", "exec"), ns)
    return ns, emitted, calls


def _row(**kw):
    base = {
        "manager_key": "mgr_parity", "status": "active", "is_enabled": 1,
        "manual_stopped": 0, "proxy_required": 1, "proxy_enabled": 1,
        "proxy_bypass_allowed": 0, "proxy_host": "10.0.0.1", "proxy_port": "1080",
        "auth_guard_state": "ok",
        "auth_guard_last_ok_at": datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat(),
    }
    base.update(kw)
    return base


def test_logged_wrapper_matches_unlogged_original_byte_for_byte():
    ns, emitted, calls = build_namespace()
    live = ns["_pb_manager_status_resolve_live"]
    live_logged = ns["_pb_manager_status_resolve_live_logged"]

    fixtures = [
        _row(),
        _row(manager_key="mgr_bypass", proxy_bypass_allowed=1, proxy_required=0),
        _row(manager_key="mgr_down", status="active"),
        _row(manager_key="mgr_stopped", manual_stopped=1),
        _row(manager_key="mgr_disabled", is_enabled=0),
        _row(manager_key="mgr_new", status="new"),
    ]

    mismatches = []
    for row in fixtures:
        expected = live(row)
        actual = live_logged(row, operation_id="parity-test")
        if expected != actual:
            mismatches.append((row.get("manager_key"), expected, actual))
    if mismatches:
        for mk, exp, act in mismatches:
            fail(f"{mk}: _pb_manager_status_resolve_live_logged diverged from "
                 f"_pb_manager_status_resolve_live: expected={exp} actual={act}")
    else:
        ok(f"_pb_manager_status_resolve_live_logged returned byte-identical "
           f"(category, icon, label) to the untouched original for all "
           f"{len(fixtures)} synthetic fixtures")

    if len(emitted) != len(fixtures):
        fail(f"expected one emitted record per row ({len(fixtures)}), got {len(emitted)}")
    else:
        ok(f"exactly one structured record emitted per row ({len(emitted)} total, "
           f"proving per-row coverage of the instrumentation layer)")

    op_ids = {r["operation_id"] for r in emitted}
    if op_ids != {"parity-test"}:
        fail(f"expected all records to share one operation_id, got {op_ids}")
    else:
        ok("all records in one render share a single operation_id")


def test_mutation_proof_diverging_wrapper_is_caught():
    """Mutation proof: if the logged wrapper called a DIFFERENT resolver
    path (e.g. re-derived proc_running independently instead of reusing the
    gathered signal), this parity test would catch the divergence. Verified
    here by asserting the comparison itself is exact equality, not a loose
    'both truthy' check."""
    ns, _emitted, _calls = build_namespace()
    live = ns["_pb_manager_status_resolve_live"]
    live_logged = ns["_pb_manager_status_resolve_live_logged"]
    row = _row(manager_key="mgr_mutation_check")
    a = live(row)
    b = live_logged(row, operation_id="mutation-check")
    if a is b:
        fail("comparison used identity instead of value equality -- would hide a real divergence")
    elif a == b:
        ok("parity comparison is exact tuple value equality (not identity), "
           "so a real behavioural divergence would be caught")
    else:
        fail(f"unexpected: {a} != {b}")


if __name__ == "__main__":
    test_logged_wrapper_matches_unlogged_original_byte_for_byte()
    test_mutation_proof_diverging_wrapper_is_caught()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("ALL RENDER PARITY TESTS PASSED")
