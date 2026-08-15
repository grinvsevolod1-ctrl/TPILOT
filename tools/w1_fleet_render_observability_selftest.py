# -*- coding: utf-8 -*-
"""tools/w1_fleet_render_observability_selftest.py -- W1 (frozen master
plan, MASTER_PLAN_FREEZE/08): proves the fleet-renderer classification
logging added in panel_bot.py (D-26/I-25) without a real database, real
Telegram bot or real proxy checks.

panel_bot.py cannot be imported directly (module-level side effects, same
restriction documented in Wave 0's active-implementation registry). The
REAL _pb_manager_status_resolve, _pb_proxy_effective_state,
_w1_render_reason_code and _w1_render_classification_record are extracted
from the actual source via ast.parse+unparse+exec (project convention).

Covers:
  - reason codes distinguish bypass / not-running / stale / never-checked
    (test C10's core, unblocked assertion);
  - a synthetic fleet where every manager is genuinely healthy produces
    zero UNKNOWN-equivalent reason codes (no fabricated selective failure);
  - a synthetic fully-stale fleet produces STALE_RESULT for every manager,
    never a selective pattern (ties to the DISPROVEN 5/6-lock explanation --
    the mechanism cannot manufacture a partial result from a uniform input);
  - the structured record never contains proxy credentials, phone numbers,
    usernames or a raw personal identifier beyond the safe manager_key;
  - mutation proof: omitting a required field, or silently mapping
    'unknown' to a code that does not distinguish stale/never, must be
    caught.

    python tools\\w1_fleet_render_observability_selftest.py
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta

BASE_DIR_REAL = __import__("pathlib").Path(__file__).resolve().parent.parent
PANEL_BOT_PY = BASE_DIR_REAL / "panel_bot.py"

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
        "_W1_RENDER_REASON_BY_CATEGORY", "_w1_render_reason_code",
        "_w1_result_age_seconds", "_w1_render_classification_record",
    })
    mod = ast.Module(body=nodes, type_ignores=[])
    ast.fix_missing_locations(mod)

    # _tpag_panel_v2_parse_dt is the shared timestamp parser these functions
    # depend on -- a small, faithful, independent reimplementation (ISO
    # 8601, naive UTC) is used here rather than pulling in more of the real
    # module, since its own contract (parse or return None) is simple and
    # stable.
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

    ns = {
        "__name__": "w1_test_ns",
        "datetime": datetime,
        "ZoneInfo": __import__("zoneinfo").ZoneInfo,
        "_tpag_panel_v2_parse_dt": fake_parse_dt,
        "normalize_manager_key": (lambda k: str(k or "").strip().lower()),
        "_PB_STATUS_CATEGORIES": {
            "banned": ("🚫", "banned", "Banned"),
            "unauthorized": ("🔐", "unauth", "Unauthorized"),
            "proxy_bad": ("🌐", "proxy_bad", "Proxy bad"),
            "process_down": ("🔴", "down", "Process down"),
            "starting": ("🔄", "starting", "Starting"),
            "stopped": ("⏸️", "stopped", "Stopped"),
            "warning": ("🟡", "warn", "Warning"),
            "ok": ("🟢", "ok", "OK"),
            "disabled": ("⚪", "disabled", "Disabled"),
            "unknown": ("❓", "unknown", "Unknown"),
        },
    }
    exec(compile(mod, "<w1_extract:fleet_render>", "exec"), ns)
    return ns


def _row(**kw):
    base = {
        "manager_key": "mgr_test", "status": "active", "is_enabled": 1,
        "manual_stopped": 0, "proxy_required": 1, "proxy_enabled": 1,
        "proxy_bypass_allowed": 0, "proxy_host": "10.0.0.1", "proxy_port": "1080",
        "proxy_username": "u", "proxy_password": "p",
        "auth_guard_state": "", "auth_guard_last_ok_at": "", "auth_guard_last_bad_at": "",
    }
    base.update(kw)
    return base


def test_reason_codes_distinguish_bypass_not_running_stale_never():
    ns = build_namespace()
    resolve = ns["_pb_manager_status_resolve"]
    build_record = ns["_w1_render_classification_record"]
    now = datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0)
    fresh = (now).isoformat()
    stale = (now - timedelta(seconds=ns["_PB_PROXY_GUARD_FRESH_SEC"] + 120)).isoformat()

    cases = {
        "GREEN_FRESH": _row(auth_guard_state="ok", auth_guard_last_ok_at=fresh),
        "BYPASS_CONFIGURED": _row(proxy_bypass_allowed=1, proxy_required=0),
        "PROCESS_NOT_RUNNING": _row(auth_guard_state="ok", auth_guard_last_ok_at=fresh),
        "STALE_RESULT": _row(auth_guard_state="ok", auth_guard_last_ok_at=stale),
        "NEVER_CHECKED": _row(auth_guard_state=""),
    }
    proc_running_by_case = {"PROCESS_NOT_RUNNING": False}

    all_good = True
    for expected_code, row in cases.items():
        proc_running = proc_running_by_case.get(expected_code, True)
        cat, icon, _label = resolve(row, tg_health={}, proc_running=proc_running, session_exists=None)
        rec = build_record(row, tg_health={}, proc_running=proc_running, session_exists=None,
                           category=cat, icon=icon, operation_id="test-op")
        if rec["reason_code"] != expected_code:
            fail(f"case {expected_code}: got reason_code={rec['reason_code']!r} category={cat!r}")
            all_good = False
    if all_good:
        ok("reason codes correctly distinguish bypass/not-running/stale/never-checked")


def test_all_healthy_fleet_has_zero_unknown():
    ns = build_namespace()
    resolve = ns["_pb_manager_status_resolve"]
    build_record = ns["_w1_render_classification_record"]
    now_iso = datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat()
    fleet = [_row(manager_key=f"mgr_{i:02d}", auth_guard_state="ok", auth_guard_last_ok_at=now_iso)
             for i in range(16)]
    codes = []
    for row in fleet:
        cat, icon, _label = resolve(row, tg_health={}, proc_running=True, session_exists=None)
        rec = build_record(row, tg_health={}, proc_running=True, session_exists=None,
                           category=cat, icon=icon, operation_id="test-op-healthy")
        codes.append(rec["reason_code"])
    bad = [c for c in codes if c in ("STALE_RESULT", "NEVER_CHECKED", "DATA_UNAVAILABLE")]
    if bad:
        fail(f"a fully healthy fleet produced non-green reason codes: {bad}")
    else:
        ok("a fully healthy 16-manager fleet produces zero fabricated stale/unknown codes")


def test_fully_stale_fleet_is_uniform_never_selective():
    """Ties to the frozen plan's DISPROVEN finding: a lock/staleness
    mechanism is all-or-nothing, so a fully-stale input must NEVER produce
    a partial/selective pattern of reason codes."""
    ns = build_namespace()
    resolve = ns["_pb_manager_status_resolve"]
    build_record = ns["_w1_render_classification_record"]
    stale_iso = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(seconds=ns["_PB_PROXY_GUARD_FRESH_SEC"] + 300)).isoformat()
    fleet = [_row(manager_key=f"mgr_{i:02d}", auth_guard_state="ok", auth_guard_last_ok_at=stale_iso)
             for i in range(11)]
    codes = set()
    for row in fleet:
        cat, icon, _label = resolve(row, tg_health={}, proc_running=True, session_exists=None)
        rec = build_record(row, tg_health={}, proc_running=True, session_exists=None,
                           category=cat, icon=icon, operation_id="test-op-stale")
        codes.add(rec["reason_code"])
    if codes != {"STALE_RESULT"}:
        fail(f"a uniformly stale fleet produced non-uniform reason codes: {codes}")
    else:
        ok("a uniformly stale 11-manager fleet produces STALE_RESULT for every manager, "
           "never a selective pattern")


def test_no_credentials_or_personal_data_in_record():
    ns = build_namespace()
    resolve = ns["_pb_manager_status_resolve"]
    build_record = ns["_w1_render_classification_record"]
    row = _row(display_name="Ivan Ivanov", telegram_username="ivan_secret",
              session_path=r"C:\ALM_TPilot\sessions\mgr_test.session")
    cat, icon, _label = resolve(row, tg_health={}, proc_running=True, session_exists=None)
    rec = build_record(row, tg_health={}, proc_running=True, session_exists=None,
                       category=cat, icon=icon, operation_id="test-op-priv")
    import json
    blob = json.dumps(rec)
    forbidden = ["10.0.0.1", "1080", "proxy_password", "Ivan Ivanov", "ivan_secret",
                 "sessions", ".session"]
    leaked = [f for f in forbidden if f in blob]
    if leaked:
        fail(f"structured record leaked forbidden values: {leaked}")
    else:
        ok("structured record contains no credentials, names, usernames or session paths")
    required = {"manager_key", "source", "configured_mode", "process_running_state",
                "last_successful_check_at", "result_age_seconds", "freshness_threshold_seconds",
                "raw_guard_state", "final_classification", "reason_code", "operation_id",
                "timestamp_utc", "timestamp_kyiv"}
    missing = required - set(rec)
    if missing:
        fail(f"structured record missing required fields: {sorted(missing)}")
    else:
        ok("structured record contains every required field")


def test_mutation_proof_reason_code_collapse_is_caught():
    """Mutation proof (Section G-20): if 'unknown' were mapped to a single
    code regardless of stale/never (i.e. the distinction were removed),
    the STALE_RESULT/NEVER_CHECKED test above would fail to distinguish
    them -- proving this suite is sensitive to that specific regression."""
    ns = build_namespace()
    reason_fn = ns["_w1_render_reason_code"]
    stale_code = reason_fn("unknown", "stale")
    never_code = reason_fn("unknown", "never")
    if stale_code == never_code:
        fail("mutation-proof regression: 'stale' and 'never' collapse to the same reason code")
    else:
        ok(f"'unknown' category still distinguishes stale ({stale_code}) from never ({never_code})")


if __name__ == "__main__":
    test_reason_codes_distinguish_bypass_not_running_stale_never()
    test_all_healthy_fleet_has_zero_unknown()
    test_fully_stale_fleet_is_uniform_never_selective()
    test_no_credentials_or_personal_data_in_record()
    test_mutation_proof_reason_code_collapse_is_caught()
    print()
    if FAILURES:
        print(f"FAILED ({len(FAILURES)}):")
        for f in FAILURES:
            print("  -", f)
        sys.exit(1)
    print("ALL FLEET RENDER OBSERVABILITY TESTS PASSED")
