# -*- coding: utf-8 -*-
"""tools/manager_status_resolver_selftest.py -- N5.4.2: dedicated selftest
for the shared deterministic manager-status resolver (panel_bot.py's
_pb_manager_status_resolve / _pb_manager_status_resolve_live), introduced
per owner decision 2026-07-24 (Part D) to fix the "15 total / 12 active /
2 stopped / 0 disabled" defect: the OLD three-predicate counters could
leave a manager (e.g. status='new', mid-onboarding) unclassified by any
bucket while the list still showed it 🟢. This resolver assigns EXACTLY
one of 10 mutually-exclusive categories to every manager, in the owner's
mandated priority order, and never fabricates a signal it wasn't given.

Two layers are tested:
  - the PURE classifier _pb_manager_status_resolve(row, tg_health=,
    proc_running=, session_exists=) -- no I/O, exercised directly with
    synthetic inputs covering every category and priority interaction;
  - the I/O wrapper _pb_manager_status_resolve_live(row) -- extracted for
    REAL together with _pb_tg_health_row/_pb_manager_process_running/
    _connect_panel_db (project convention: prefer the real helper), with
    the ONE deliberate fake being _pb_service_scan_cached (a real
    PowerShell process scan has no place in an offline selftest).

    python tools\\manager_status_resolver_selftest.py
"""
from __future__ import annotations

import ast
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()
TREE = ast.parse(PANEL_SRC)


def _extract_by_name(names: set) -> list:
    nodes = []
    seen = set()
    for n in TREE.body:
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


PURE_NAMES = {
    "_pb_manager_status_resolve", "_PB_STATUS_CATEGORIES",
    "_PB_STATUS_BANNED_MARKS", "_PB_STATUS_AUTH_MARKS",
    # 2026-07-25 proxy-freshness incident fix:
    "_pb_proxy_guard_state", "_PB_PROXY_GUARD_FRESH_SEC", "_tpag_panel_v2_parse_dt",
    # 2026-07-25 R1-R3 follow-up (independent-review):
    "_pb_proxy_ts_is_fresh", "_PB_PROXY_CLOCK_SKEW_TOLERANCE_SEC",
    "_pb_proxy_timestamp_malformed", "_pb_proxy_effective_state",
    "_pb_proxy_state_check_text", "_PB_PROXY_STATE_LABELS",
}

LIVE_NAMES = {
    "_pb_manager_status_resolve_live", "_pb_tg_health_row",
    "_pb_manager_process_running", "_connect_panel_db",
}


def build_pure_ns() -> dict:
    """The pure classifier + its category table, extracted for real, with
    zero I/O dependencies -- exec'd standalone. `datetime`/`timezone` are
    provided because _pb_proxy_guard_state (2026-07-25 fix) does its own
    (pure, no-network) timestamp-freshness math."""
    nodes = _extract_by_name(PURE_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {"Dict": dict, "Any": object, "datetime": datetime, "timezone": timezone}
    exec(compile(module_src, f"<{PANEL_PATH}:status_pure>", "exec"), ns)
    return ns


def build_live_ns(db_path: str, proc_scan=None) -> dict:
    """The I/O wrapper + its real dependencies, with the ONE deliberate
    fake (_pb_service_scan_cached -- no real PowerShell scan in a
    selftest). normalize_manager_key is the real manager_registry
    function (project convention)."""
    import manager_registry

    nodes = _extract_by_name(PURE_NAMES) + _extract_by_name(LIVE_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    _scan_holder = {"value": proc_scan}

    ns = {
        "sqlite3": sqlite3,
        "os": os,
        "re": re,
        "Dict": dict, "Any": object,
        "datetime": datetime, "timezone": timezone,
        "TPILOT_DB_PATH": db_path,
        "BASE_DIR": BASE_DIR,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "_norm_path": lambda s: str(s or "").replace("\\", "/").lower(),
        "_pb_service_scan_cached": lambda: _scan_holder["value"],
    }
    exec(compile(module_src, f"<{PANEL_PATH}:status_live>", "exec"), ns)
    ns["_set_proc_scan"] = lambda v: _scan_holder.__setitem__("value", v)
    return ns


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="manager_status_resolver_selftest_")
    os.close(fd)
    con = sqlite3.connect(path)
    try:
        con.executescript(
            """
            CREATE TABLE managers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key TEXT UNIQUE
            );
            """
        )
        con.commit()
    finally:
        con.close()
    return path


BASE_ROW = {
    "manager_key": "mgr01", "status": "active", "is_enabled": 1,
    "manual_stopped": 0, "auth_guard_state": "", "proxy_required": 0,
    "proxy_enabled": 0, "proxy_bypass_allowed": 0, "proxy_test_ok": 0,
    # 2026-07-25 R1-R3 follow-up: a genuinely-configured proxy needs
    # host+port for _pb_proxy_effective_state to reach the guard-state
    # branch at all (R3.2 checks host/port BEFORE any guard timestamp) --
    # default to a plausible configured host/port so every existing
    # proxy_required=1/proxy_enabled=1 row in this file exercises the
    # guard-state path it was written to test, not 'not_configured'.
    # Tests that specifically need the not_configured/missing-host-port
    # path override these to "" explicitly (see test_9b.15).
    "proxy_host": "203.0.113.10", "proxy_port": "1080",
}


def _row(**over):
    d = dict(BASE_ROW)
    d.update(over)
    return d


# 2026-07-25 proxy-freshness incident fix: realistic fresh/stale Auth Guard
# outcome timestamps for seeding test rows (_pb_proxy_guard_state reads
# auth_guard_last_ok_at / auth_guard_last_bad_at, gated by
# _PB_PROXY_GUARD_FRESH_SEC == 600s).
def _fresh_ts(seconds_ago: int = 30) -> str:
    return (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()


def _stale_ts(seconds_ago: int = 3600) -> str:
    return (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()


# ======================================================================
# 1. Every one of the 10 categories is individually reachable with a
#    realistic combination of signals (exact category_key + icon).
# ======================================================================

def test_1_all_ten_categories_reachable(ns) -> None:
    resolve = ns["_pb_manager_status_resolve"]
    cases = [
        ("banned", "🚫", _row(), {"tg_health": {"health_status": "blocked", "error_class": "UserDeactivatedBanError"}}),
        ("unauthorized", "🔐", _row(), {"session_exists": False}),
        ("unauthorized", "🔐", _row(), {"tg_health": {"health_status": "blocked", "error_class": "AuthKeyUnregisteredError"}}),
        ("proxy_bad", "🌐", _row(proxy_required=1, proxy_enabled=0), {}),
        # 2026-07-25 fix: proxy_bad now requires a FRESH, trusted explicit
        # Auth Guard failure -- a bare stale proxy_test_ok=0 (the dead flag
        # the live guard never writes) is deliberately no longer sufficient
        # on its own; see test_9 below for the stale-failure -> NOT
        # proxy_bad regression coverage.
        ("proxy_bad", "🌐", _row(proxy_required=1, proxy_enabled=1,
                                  auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts()), {}),
        ("proxy_bad", "🌐", _row(proxy_required=1, proxy_enabled=1,
                                  auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts(),
                                  auth_guard_last_ok_at=_stale_ts()), {}),
        ("process_down", "🔴", _row(), {"proc_running": False}),
        ("starting", "🔄", _row(status="new"), {}),
        ("stopped", "⏸️", _row(manual_stopped=1), {}),
        # 2026-07-25 R1 follow-up: a bare auth_guard_state="degraded" no
        # longer means anything on its own -- a manager with no proxy
        # required/enabled classifies as 'bypass' first (R3.1) and never
        # even reaches _pb_proxy_guard_state, so 'degraded' must be
        # exercised on a row that actually has a proxy configured, WITH a
        # fresh auth_guard_checked_at (R1: degraded is only trusted while
        # fresh).
        ("warning", "🟡", _row(proxy_required=1, proxy_enabled=1,
                                auth_guard_state="degraded", auth_guard_checked_at=_fresh_ts()), {}),
        ("warning", "🟡", _row(), {"tg_health": {"health_status": "warning"}}),
        ("ok", "🟢", _row(), {"proc_running": True}),
        # PEERFLOOD RECOVERY 20260812 (P1): LIMITED (PeerFlood/FloodWait) no
        # longer downgrades the runtime icon -- _tp_hg_send_allowed does not
        # block ordinary sends for it, so an active+running+proxy-ok manager
        # stays 'ok'/🟢; only a genuinely fresh degraded proxy or a plain
        # 'warning' Telegram Health status (both exercised above) still
        # route to 'warning'.
        ("ok", "🟢", _row(), {"tg_health": {"health_status": "limited"}, "proc_running": True}),
        ("disabled", "⚪", _row(is_enabled=0), {}),
        ("disabled", "⚪", _row(status="disabled"), {}),
        ("unknown", "❓", _row(status="pending_review", is_enabled=1, manual_stopped=0), {"tg_health": None}),
    ]
    seen_categories = set()
    for expected_key, expected_icon, row, kwargs in cases:
        key, icon, label = resolve(row, **kwargs)
        check(f"1. {expected_key} reachable: row={row.get('status')}/{kwargs} -> {expected_key}",
              key == expected_key and icon == expected_icon, (key, icon, label))
        seen_categories.add(expected_key)
    all_keys = set(ns["_PB_STATUS_CATEGORIES"].keys())
    check("1. all 10 owner-mandated categories are exercised above",
          seen_categories == all_keys, (seen_categories, all_keys))
    check("1. exactly 10 categories exist in the table", len(all_keys) == 10, all_keys)


# ======================================================================
# 2. Priority order: when signals for MULTIPLE categories are present at
#    once, the HIGHER-priority (earlier in the owner's list) one wins.
# ======================================================================

def test_2_priority_order(ns) -> None:
    resolve = ns["_pb_manager_status_resolve"]

    # banned (1) beats unauthorized (2): both marker families could match
    # health_status='blocked' text, but only the ban-family marker is
    # present, so it must resolve to banned, not fall through.
    key, _, _ = resolve(_row(), tg_health={"health_status": "blocked", "error_class": "PhoneNumberBannedError"},
                         session_exists=False)
    check("2. banned (1) beats unauthorized (2) when only ban-family markers match",
          key == "banned", key)

    # unauthorized (2) beats proxy_bad (3): a manager with a missing
    # session AND a broken proxy must show the more urgent unauthorized.
    key, _, _ = resolve(_row(proxy_required=1, proxy_enabled=0), session_exists=False)
    check("2. unauthorized (2) beats proxy_bad (3)", key == "unauthorized", key)

    # proxy_bad (3) beats process_down (4): a manager whose proxy is
    # broken AND whose process is down must show proxy_bad first (the
    # proxy failure is the more actionable root cause).
    key, _, _ = resolve(_row(proxy_required=1, proxy_enabled=0), proc_running=False)
    check("2. proxy_bad (3) beats process_down (4)", key == "proxy_bad", key)

    # process_down (4) beats starting (5): status='new' with a config that
    # otherwise looks "should be running" -- process_down's guard requires
    # status=='active', so a genuinely 'new' manager cannot trigger it;
    # verify starting wins cleanly when status='new' and nothing else fires.
    key, _, _ = resolve(_row(status="new"), proc_running=False)
    check("2. status='new' cannot trigger process_down (guarded to status=='active') -> starting wins",
          key == "starting", key)

    # starting (5) beats stopped (6): an onboarding manager that also has
    # manual_stopped=1 set (edge case) still reads as starting.
    key, _, _ = resolve(_row(status="new", manual_stopped=1))
    check("2. starting (5) beats stopped (6)", key == "starting", key)

    # stopped (6) beats warning (7): a manually-stopped manager with a
    # FRESH degraded auth-guard reading (proxy actually configured -- see
    # test_1's R1 note above) still shows stopped (operator action already
    # explains the state).
    key, _, _ = resolve(_row(manual_stopped=1, proxy_required=1, proxy_enabled=1,
                              auth_guard_state="degraded", auth_guard_checked_at=_fresh_ts()))
    check("2. stopped (6) beats warning (7)", key == "stopped", key)

    # warning (7) beats ok (8): a running, enabled, active manager with a
    # FRESH degraded guard reading (proxy configured) must NOT show a plain
    # green.
    key, _, _ = resolve(_row(proxy_required=1, proxy_enabled=1,
                              auth_guard_state="degraded", auth_guard_checked_at=_fresh_ts()),
                         proc_running=True)
    check("2. warning (7) beats ok (8)", key == "warning", key)

    # ok (8) beats disabled (9): disabled only fires when genuinely
    # disabled -- an enabled+active+healthy manager is never miscast.
    key, _, _ = resolve(_row(), proc_running=True)
    check("2. a genuinely healthy manager resolves to ok, not disabled",
          key == "ok", key)


# ======================================================================
# 3. Mutual exclusivity + the sum invariant (owner's exact requirement):
#    sum(all displayed status categories) == total displayed managers.
#    Includes the EXACT regression scenario from the owner's report
#    (15 total / 12 active / 2 stopped / 0 disabled = 14, one manager
#    unclassified -- the manager was status='new').
# ======================================================================

def test_3_mutual_exclusivity_and_sum_invariant(ns) -> None:
    resolve = ns["_pb_manager_status_resolve"]
    categories = ns["_PB_STATUS_CATEGORIES"]

    def _classify_batch(rows):
        counts = {k: 0 for k in categories}
        for r in rows:
            key, _icon, _label = resolve(r)
            check(f"3. {key!r} is a real category key for manager {r.get('manager_key')}",
                  key in categories, key)
            counts[key] += 1
        return counts

    # 3a. Synthetic reproduction of the owner's exact defect: 12 active,
    # 2 manually-stopped, 0 disabled, 1 status='new' (the manager the OLD
    # three-predicate counters silently dropped) = 15 total.
    rows = (
        [_row(manager_key=f"a{i}") for i in range(12)]
        + [_row(manager_key=f"s{i}", manual_stopped=1) for i in range(2)]
        + [_row(manager_key="onboarding1", status="new", is_enabled=1, manual_stopped=0)]
    )
    check("3a. synthetic batch has exactly 15 managers (owner's reported total)", len(rows) == 15, len(rows))
    counts = _classify_batch(rows)
    check("3a. [FIX 15 vs 14] sum(all categories) == total managers (was 14 before N5.4.2)",
          sum(counts.values()) == len(rows), (counts, len(rows)))
    check("3a. the onboarding manager lands in 'starting', not silently dropped",
          counts["starting"] == 1, counts)
    check("3a. 12 managers classify as 'ok'", counts["ok"] == 12, counts)
    check("3a. 2 managers classify as 'stopped'", counts["stopped"] == 2, counts)
    check("3a. 0 managers classify as 'disabled' (matches the owner's report)", counts["disabled"] == 0, counts)

    # 3b. Broader randomized-shape coverage: every row still gets EXACTLY
    # one category regardless of combination (mutual exclusivity holds
    # for messy real-world combinations, not just the clean cases above).
    messy_rows = [
        _row(manager_key="m1", is_enabled=0, manual_stopped=1),  # disabled wins over stopped? no: stopped(6) checked before disabled(9), and manual_stopped=1 -> "stopped"
        _row(manager_key="m2", status="disabled", manual_stopped=0, is_enabled=1),
        _row(manager_key="m3", status="", is_enabled=0, manual_stopped=0),
        _row(manager_key="m4", proxy_required=1, proxy_bypass_allowed=1, proxy_enabled=0),  # bypass exempts proxy_bad
        _row(manager_key="m5", auth_guard_state="direct"),  # unrecognized state -> falls through to ok
    ]
    counts2 = _classify_batch(messy_rows)
    check("3b. messy-combination batch: sum(categories) == total managers",
          sum(counts2.values()) == len(messy_rows), (counts2, len(messy_rows)))
    check("3b. proxy_bypass_allowed=1 exempts a manager from proxy_bad even though proxy_required=1",
          resolve(messy_rows[3])[0] != "proxy_bad", resolve(messy_rows[3]))
    check("3b. auth_guard_state='direct' (bypass mode) does not trigger proxy_bad/warning",
          resolve(messy_rows[4])[0] not in ("proxy_bad", "warning"), resolve(messy_rows[4]))


# ======================================================================
# 4. "Do not fabricate unavailable signals": None/missing inputs must
#    NEVER produce a false positive category.
# ======================================================================

def test_4_no_fabrication(ns) -> None:
    resolve = ns["_pb_manager_status_resolve"]

    # No session_path ever configured (empty/never-set, e.g. a freshly
    # added manager still in onboarding) must NOT read as "unauthorized" --
    # only a session that WAS configured and is now missing should.
    key, _, _ = resolve(_row(status="new"), session_exists=None)
    check("4. session_exists=None (never configured) does not fabricate 'unauthorized'",
          key != "unauthorized", key)

    # No process-scan data at all (proc_running=None) must not fabricate
    # a red 'process_down' -- absence of evidence is not evidence of a
    # stopped process.
    key, _, _ = resolve(_row())
    check("4. proc_running=None (no scan data) does not fabricate 'process_down'",
          key != "process_down", key)

    # tg_health=None (no row/table) must not fabricate 'warning' or
    # 'banned'/'unauthorized'.
    key, _, _ = resolve(_row(), tg_health=None)
    check("4. tg_health=None does not fabricate any Telegram-derived category",
          key not in ("banned", "unauthorized", "warning"), key)

    # An empty tg_health dict (row exists but has no recognizable
    # health_status) behaves identically to no data.
    key, _, _ = resolve(_row(), tg_health={})
    check("4. tg_health={} behaves like no data", key == "ok", key)


# ======================================================================
# 5. _pb_manager_process_running: same detection rule as
#    _scan_process_state's manager_processes map, reading the shared
#    cached scan list (no new PowerShell call).
# ======================================================================

def test_5_process_running_detection(live_ns) -> None:
    fn = live_ns["_pb_manager_process_running"]
    check("5. no scan data (None) -> None (unknown, not a false negative)",
          fn(None, "mgr01") is None, fn(None, "mgr01"))
    procs = [
        {"cmd": f"{BASE_DIR}/venv/Scripts/python.exe {BASE_DIR}/main.py --manager mgr01"},
        {"cmd": f"{BASE_DIR}/venv/Scripts/python.exe {BASE_DIR}/main.py --manager mgr02"},
        {"cmd": "C:/some/other/tool.exe --manager mgr01"},  # outside BASE_DIR -- must not count
    ]
    check("5. mgr01 process found in the shared scan -> True", fn(procs, "mgr01") is True, fn(procs, "mgr01"))
    check("5. mgr03 (not in scan) -> False", fn(procs, "mgr03") is False, fn(procs, "mgr03"))
    check("5. empty/invalid manager_key -> None", fn(procs, "") is None, fn(procs, ""))


# ======================================================================
# 6. _pb_tg_health_row: pure read-only lookup, fail-open on any error
#    (missing table, no row) -- never raises, never writes.
# ======================================================================

def test_6_tg_health_row_fail_open(live_ns) -> None:
    fn = live_ns["_pb_tg_health_row"]
    check("6. missing table -> {} (fail-open, no crash)", fn("mgr01") == {}, fn("mgr01"))
    check("6. empty manager_key -> {}", fn("") == {}, fn(""))


# ======================================================================
# 7. _pb_manager_status_resolve_live: full I/O wrapper wiring -- reads
#    the shared cached scan (fake) + the real (empty) tg_health lookup +
#    session-file existence, and calls the pure resolver.
# ======================================================================

def test_7_live_wrapper_wiring(db_path: str) -> None:
    ns = build_live_ns(db_path)
    resolve_live = ns["_pb_manager_status_resolve_live"]

    key, icon, _label = resolve_live(_row(manager_key="mgrX"))
    check("7. live wrapper with no scan data resolves 'ok' (no fabricated process_down)",
          key == "ok" and icon == "🟢", (key, icon))

    ns["_set_proc_scan"]([])
    key, _icon, _label = resolve_live(_row(manager_key="mgrX", status="active"))
    check("7. live wrapper WITH scan data but manager absent from it resolves 'process_down'",
          key == "process_down", key)

    ns["_set_proc_scan"]([{"cmd": f"{BASE_DIR}/main.py --manager mgrx"}])
    key, _icon, _label = resolve_live(_row(manager_key="mgrX", status="active"))
    check("7. live wrapper WITH the manager present in the scan resolves 'ok'",
          key == "ok", key)

    # Session-file wiring: a configured-but-missing session file resolves
    # to unauthorized; a never-configured one does not.
    key, _, _ = resolve_live(_row(manager_key="mgrX", session_path="/definitely/not/a/real/path.session"))
    check("7. live wrapper: configured-but-missing session file -> unauthorized",
          key == "unauthorized", key)
    key, _, _ = resolve_live(_row(manager_key="mgrX", session_path=""))
    check("7. live wrapper: never-configured session_path -> not fabricated as unauthorized",
          key != "unauthorized", key)


# ======================================================================
# 8. N5.4.6 (RF1, independent-review fix): _get_python_processes' OWN
#    contract -- a LIST (possibly empty) means a genuinely completed,
#    trustworthy scan; None means the scan itself could not be trusted
#    (subprocess failure/timeout/non-zero-with-no-output/malformed JSON).
#    Before the fix EVERY failure path returned [], which downstream
#    (_pb_manager_process_running) read as a CONFIRMED negative -> every
#    enabled manager showed a false process_down 🔴 whenever the
#    PowerShell scan merely hiccuped. This runs the REAL function with
#    subprocess.run faked at the boundary -- no real PowerShell/`ps`
#    process spawned.
# ======================================================================

class _FakeCompletedProcess:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def build_scan_ns(*, run_result=None, run_raises=None, os_name="nt"):
    """Extracts the REAL _get_python_processes, faking only subprocess.run
    (and os.name, to exercise both the Windows/PowerShell and the POSIX
    `ps aux` branches) -- `run_result` is a _FakeCompletedProcess to
    return, `run_raises` is an exception instance to raise instead."""
    import json as real_json
    import subprocess as real_subprocess

    nodes = _extract_by_name({"_get_python_processes"})
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    class _FakeSubprocess:
        @staticmethod
        def run(*args, **kwargs):
            if run_raises is not None:
                raise run_raises
            return run_result

        TimeoutExpired = real_subprocess.TimeoutExpired

    class _FakeOS:
        name = os_name
        sep = os.sep

    ns = {
        "subprocess": _FakeSubprocess(),
        "os": _FakeOS(),
        "json": real_json,
        "BASE_DIR": BASE_DIR,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:scan_contract>", "exec"), ns)
    return ns


def test_8_real_process_scan_contract() -> None:
    fn_name = "_get_python_processes"

    # 8a. Genuine match: a real python process line is present -> a
    # trustworthy, non-empty list.
    ns = build_scan_ns(run_result=_FakeCompletedProcess(
        stdout='[{"ProcessId": 111, "ParentProcessId": 1, "Name": "python.exe", '
               '"CommandLine": "python main.py --manager mgr01"}]',
        returncode=0))
    result = ns[fn_name]()
    check("8a. [MATCH] a successful scan with real output returns a non-empty LIST",
          isinstance(result, list) and len(result) == 1, result)
    check("8a. the returned row carries the command line", "mgr01" in (result[0].get("cmd") or ""), result)

    # 8b. Genuine no-match: PowerShell ran cleanly (returncode 0) and found
    # nothing -- a real, trustworthy, CONFIRMED empty result, not unknown.
    ns = build_scan_ns(run_result=_FakeCompletedProcess(stdout="", returncode=0))
    result = ns[fn_name]()
    check("8b. [NO-MATCH, confirmed] a clean successful scan with zero matches returns [] "
          "(a real confirmed-empty result, not None)",
          result == [], result)

    # 8c. Failure: the subprocess call itself raises (spawn error / OS-level
    # failure) -- MUST be None (unknown), never [].
    ns = build_scan_ns(run_raises=OSError("simulated: powershell.exe not found"))
    result = ns[fn_name]()
    check("8c. [FAILURE] subprocess.run raising OSError -> None (unknown, NEVER a confirmed [])",
          result is None, result)

    # 8d. Timeout: subprocess.run raising TimeoutExpired -- MUST be None.
    ns = build_scan_ns(run_raises=__import__("subprocess").TimeoutExpired(cmd="powershell", timeout=10))
    result = ns[fn_name]()
    check("8d. [TIMEOUT] subprocess.run timing out -> None (unknown, NEVER a confirmed [])",
          result is None, result)

    # 8e. Non-zero exit with no usable stdout -- errored run, MUST be None
    # (this is the exact class of bug RF1 fixed: this used to silently
    # become [] and read downstream as "confirmed nobody is running").
    ns = build_scan_ns(run_result=_FakeCompletedProcess(stdout="", returncode=1))
    result = ns[fn_name]()
    check("8e. [RF1 REGRESSION CASE] non-zero exit with empty stdout -> None, NOT [] "
          "(this exact case used to cause a false mass process_down 🔴)",
          result is None, result)

    # 8f. Malformed/unparseable stdout (garbage instead of JSON) -- MUST be
    # None, never crash and never silently become [].
    ns = build_scan_ns(run_result=_FakeCompletedProcess(stdout="not-json-at-all{{{", returncode=0))
    result = ns[fn_name]()
    check("8f. [MALFORMED] unparseable stdout -> None (unknown), function does not raise",
          result is None, result)

    # 8g. Non-Windows (POSIX `ps aux`) branch: same None-on-failure contract.
    ns = build_scan_ns(os_name="posix", run_result=_FakeCompletedProcess(
        stdout="user 123 0.0 0.1 python main.py --manager mgr01\n", returncode=0))
    result = ns[fn_name]()
    check("8g. [POSIX, match] `ps aux` branch returns a trustworthy list on a clean run",
          isinstance(result, list) and len(result) == 1, result)

    ns = build_scan_ns(os_name="posix", run_raises=OSError("simulated: ps not found"))
    result = ns[fn_name]()
    check("8g. [POSIX, failure] `ps aux` branch also returns None (not []) when the scan itself fails",
          result is None, result)

    # 8h. End-to-end proof that a genuine scan FAILURE (None) never gets
    # silently reclassified as a confirmed process_down by the downstream
    # detector -- ties the contract fix directly to the resolver's own
    # documented fail-open guarantee (this is the exact chain the owner's
    # original incident report exercised: PowerShell scan -> caller ->
    # displayed status).
    scope_db = _make_temp_db()
    try:
        proc_running_fn = build_live_ns(scope_db)["_pb_manager_process_running"]
        check("8h. [END-TO-END] a None scan result (as now correctly returned on failure) "
              "resolves to unknown (None) in _pb_manager_process_running, never False",
              proc_running_fn(None, "mgr01") is None, proc_running_fn(None, "mgr01"))
    finally:
        os.unlink(scope_db)


# ======================================================================
# 9. 2026-07-25 incident fix (manager foxy1): _pb_proxy_guard_state --
#    fresh/stale/never/latest-wins semantics, direct/bypass exemption, the
#    resolver's new proxy_bad/ok wiring, runtime-vs-proxy separation, and
#    the category-sum invariant across a mixed proxy-freshness batch.
# ======================================================================

def test_9_proxy_guard_freshness_and_latest_wins(ns) -> None:
    guard = ns["_pb_proxy_guard_state"]
    resolve = ns["_pb_manager_status_resolve"]

    # 1. [A] Direct/bypass manager -> never proxy_bad, even with a fresh
    # 'blocked' state sitting in the row (bypass wins outright).
    row = _row(proxy_required=1, proxy_bypass_allowed=1, auth_guard_state="blocked",
               auth_guard_last_bad_at=_fresh_ts())
    key, _icon, _label = resolve(row, proc_running=True)
    check("9.1 [A] bypass-allowed manager never resolves proxy_bad even with a fresh 'blocked' state",
          key != "proxy_bad", key)

    # 2. [B] Assigned proxy, fresh success -> healthy, category 'ok' reachable.
    row = _row(proxy_required=1, proxy_enabled=1, auth_guard_state="ok",
               auth_guard_last_ok_at=_fresh_ts())
    check("9.2 [B] fresh success -> guard state 'healthy'", guard(row) == "healthy", guard(row))
    key, _icon, _label = resolve(row, proc_running=True)
    check("9.2 [B] fresh success -> resolver does not return proxy_bad", key != "proxy_bad", key)
    check("9.2 [B] fresh success + otherwise-healthy manager -> resolves 'ok'", key == "ok", key)

    # 3. [C] Assigned proxy, fresh EXPLICIT failure -> proxy_bad, timestamp
    # is present on the row for display.
    row = _row(proxy_required=1, proxy_enabled=1, auth_guard_state="blocked",
               auth_guard_last_bad_at=_fresh_ts())
    check("9.3 [C] fresh failure -> guard state 'broken'", guard(row) == "broken", guard(row))
    key, _icon, _label = resolve(row)
    check("9.3 [C] fresh failure -> resolver returns proxy_bad", key == "proxy_bad", key)

    # 4. [D, THE REPORTED INCIDENT] Assigned proxy, OLD/stale failure (1h)
    # -> NOT proxy_bad. This is the exact foxy1 defect: a check from hours
    # ago must not remain authoritative forever.
    row = _row(proxy_required=1, proxy_enabled=1, auth_guard_state="blocked",
               auth_guard_last_bad_at=_stale_ts(3600))
    check("9.4 [D, INCIDENT] stale (1h old) failure -> guard state 'stale', NOT 'broken'",
          guard(row) == "stale", guard(row))
    key, _icon, _label = resolve(row, proc_running=True)
    check("9.4 [D, INCIDENT] stale failure -> resolver does NOT return proxy_bad",
          key != "proxy_bad", key)

    # 5. [F] Old failure followed by a NEWER success -> success wins.
    row = _row(proxy_required=1, proxy_enabled=1,
               auth_guard_last_bad_at=_stale_ts(3600), auth_guard_last_ok_at=_fresh_ts())
    check("9.5 [F] old failure + newer success -> guard state 'healthy'",
          guard(row) == "healthy", guard(row))
    key, _icon, _label = resolve(row, proc_running=True)
    check("9.5 [F] old failure + newer success -> resolves 'ok', not proxy_bad",
          key == "ok" and key != "proxy_bad", key)

    # 6. [F] Newer failure AFTER an older success -> newer failure wins.
    row = _row(proxy_required=1, proxy_enabled=1,
               auth_guard_last_ok_at=_stale_ts(3600), auth_guard_last_bad_at=_fresh_ts())
    check("9.6 [F] newer failure + older success -> guard state 'broken'",
          guard(row) == "broken", guard(row))
    key, _icon, _label = resolve(row)
    check("9.6 [F] newer failure + older success -> resolves proxy_bad", key == "proxy_bad", key)

    # 7. [E] Missing timestamps entirely -> 'never', not a fabricated failure.
    row = _row(proxy_required=1, proxy_enabled=1)
    check("9.7 [E] no auth_guard_last_ok_at/_last_bad_at at all -> guard state 'never'",
          guard(row) == "never", guard(row))
    key, _icon, _label = resolve(row, proc_running=True)
    check("9.7 [E] never-checked proxy -> resolver does NOT return proxy_bad",
          key != "proxy_bad", key)

    # 8. Malformed timestamps -> treated as absent via the SAME safe parser
    # already used elsewhere in the project (_tpag_panel_v2_parse_dt) --
    # never an exception.
    row = _row(proxy_required=1, proxy_enabled=1,
               auth_guard_last_bad_at="not-a-real-timestamp", auth_guard_last_ok_at="")
    try:
        state = guard(row)
        ok_no_raise = True
    except Exception as exc:
        state, ok_no_raise = f"<raised {exc!r}>", False
    check("9.8 [malformed] malformed timestamp does not raise", ok_no_raise, state)
    check("9.8 [malformed] malformed timestamp -> guard state 'never' (parsed as absent)",
          state == "never", state)

    # 9. Auth Guard proxy SUCCESS + manager runtime timeout ("Менеджер не
    # ответил или рантайм не запущен") -- proxy must stay healthy; the
    # runtime problem is a SEPARATE signal (managers.last_error / proc_
    # running), never folded into proxy_guard.
    row = _row(proxy_required=1, proxy_enabled=1, auth_guard_state="ok",
               auth_guard_last_ok_at=_fresh_ts(),
               last_error="Менеджер не ответил или рантайм не запущен")
    check("9.9 [separation] runtime-timeout last_error does not affect guard state",
          guard(row) == "healthy", guard(row))
    key, _icon, _label = resolve(row, proc_running=False)
    check("9.9 [separation] runtime timeout (proc_running=False) resolves process_down, "
          "NOT proxy_bad -- proxy stays healthy, the runtime issue is classified separately",
          key == "process_down", key)

    # 12. Category totals still sum exactly to the manager count across a
    # realistic mixed batch of proxy-freshness states (extends test_3's
    # sum-invariant with the new dimension this fix introduces).
    categories = ns["_PB_STATUS_CATEGORIES"]
    mixed_rows = [
        _row(manager_key="p1", proxy_required=1, proxy_enabled=1, auth_guard_last_ok_at=_fresh_ts()),
        _row(manager_key="p2", proxy_required=1, proxy_enabled=1, auth_guard_last_bad_at=_fresh_ts()),
        _row(manager_key="p3", proxy_required=1, proxy_enabled=1, auth_guard_last_bad_at=_stale_ts()),
        _row(manager_key="p4", proxy_required=1, proxy_enabled=1),
        _row(manager_key="p5", proxy_required=1, proxy_bypass_allowed=1),
        _row(manager_key="p6", proxy_required=0),
    ]
    counts = {k: 0 for k in categories}
    for r in mixed_rows:
        k, _i, _l = resolve(r, proc_running=True)
        check(f"9.12 {k!r} is a real category key for manager {r.get('manager_key')}", k in categories, k)
        counts[k] += 1
    check("9.12 [counters] sum(all categories) == total managers for a mixed proxy-freshness batch",
          sum(counts.values()) == len(mixed_rows), (counts, len(mixed_rows)))
    check("9.12 exactly one manager (p2, fresh explicit failure) classifies as proxy_bad",
          counts.get("proxy_bad", 0) == 1, counts)


# ======================================================================
# 9b. 2026-07-25 R1-R3 follow-up (independent-review): the new shared
#     predicate _pb_proxy_effective_state (bypass/not_configured/healthy/
#     degraded/broken/stale/never), the owner's 15-minute TTL contract
#     (_PB_PROXY_GUARD_FRESH_SEC), the 60s clock-skew tolerance, the strict
#     tie-break, the malformed-vs-never display distinction, and the
#     degraded-freshness gate. Covers the owner's numbered scenarios
#     1-15 and 18-19 (12/16/17/20-22 are full-card/badge/live-rerender
#     scenarios, covered in manager_full_card_selftest.py /
#     manager_card_unified_selftest.py).
# ======================================================================

def test_9b_effective_state_and_ttl_contract(ns) -> None:
    guard = ns["_pb_proxy_guard_state"]
    effective = ns["_pb_proxy_effective_state"]
    check_text = ns["_pb_proxy_state_check_text"]
    malformed = ns["_pb_proxy_timestamp_malformed"]
    resolve = ns["_pb_manager_status_resolve"]
    fresh_gate = ns["_pb_proxy_ts_is_fresh"]

    # --- owner scenario 1: fresh healthy required proxy ---
    row = _row(proxy_required=1, proxy_enabled=1, proxy_host="1.2.3.4", proxy_port="1080",
               auth_guard_state="ok", auth_guard_last_ok_at=_fresh_ts())
    check("9b.1 fresh healthy required proxy -> effective 'healthy'", effective(row) == "healthy", effective(row))
    key, _, _ = resolve(row, proc_running=True)
    check("9b.1 fresh healthy required proxy -> resolver 'ok'", key == "ok", key)

    # --- owner scenario 2: fresh degraded required proxy ---
    row = _row(proxy_required=1, proxy_enabled=1,
               auth_guard_state="degraded", auth_guard_checked_at=_fresh_ts())
    check("9b.2 fresh degraded required proxy -> effective 'degraded'", effective(row) == "degraded", effective(row))
    key, _, _ = resolve(row, proc_running=True)
    check("9b.2 fresh degraded required proxy -> resolver 'warning'", key == "warning", key)

    # --- owner scenario 3: stale degraded -> stale, NOT healthy (R1's core
    # requirement -- a stale degraded snapshot must never silently read as
    # confirmed-healthy).
    row = _row(proxy_required=1, proxy_enabled=1,
               auth_guard_state="degraded", auth_guard_checked_at=_stale_ts(3600))
    check("9b.3 [R1] stale degraded -> effective 'stale', never 'healthy'/'degraded'",
          effective(row) == "stale", effective(row))
    key, _, _ = resolve(row, proc_running=True)
    check("9b.3 [R1] stale degraded -> resolver does not fabricate 'ok' or 'warning'",
          key not in ("ok", "warning"), key)

    # --- owner scenario 3b: degraded but auth_guard_checked_at missing
    # entirely -- same as stale (the soft-fail branch always refreshes
    # checked_at; a missing value cannot be trusted).
    row = _row(proxy_required=1, proxy_enabled=1, auth_guard_state="degraded")
    check("9b.3b degraded with no checked_at at all -> 'stale' (never trusted)",
          effective(row) == "stale", effective(row))

    # --- owner scenario 4: fresh broken required proxy ---
    row = _row(proxy_required=1, proxy_enabled=1,
               auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts())
    check("9b.4 fresh broken required proxy -> effective 'broken'", effective(row) == "broken", effective(row))
    key, _, _ = resolve(row)
    check("9b.4 fresh broken required proxy -> resolver 'proxy_bad'", key == "proxy_bad", key)

    # --- owner scenario 5: stale broken -> neutral 'stale', not 'broken' ---
    row = _row(proxy_required=1, proxy_enabled=1,
               auth_guard_state="blocked", auth_guard_last_bad_at=_stale_ts(3600))
    check("9b.5 stale broken -> effective 'stale', not 'broken'", effective(row) == "stale", effective(row))
    key, _, _ = resolve(row, proc_running=True)
    check("9b.5 stale broken -> resolver does not return proxy_bad", key != "proxy_bad", key)

    # --- owner scenario 6: never checked ---
    row = _row(proxy_required=1, proxy_enabled=1)
    check("9b.6 never-checked required proxy -> effective 'never'", effective(row) == "never", effective(row))
    check("9b.6 'never' display text -> 'не выполнялась' (no raw timestamp present)",
          check_text("never", row) == "не выполнялась", check_text("never", row))

    # --- owner scenario 7: malformed timestamp -- distinct display wording
    # from genuinely-never-checked, but SAME underlying classification
    # ('never' -- neither is trustworthy evidence).
    row = _row(proxy_required=1, proxy_enabled=1, auth_guard_last_bad_at="not-a-timestamp")
    check("9b.7 malformed timestamp -> effective 'never' (classification unchanged)",
          effective(row) == "never", effective(row))
    check("9b.7 malformed timestamp IS detected by _pb_proxy_timestamp_malformed",
          malformed(row) is True, malformed(row))
    check("9b.7 malformed timestamp display text -> 'данные проверки некорректны', NOT 'не выполнялась'",
          check_text("never", row) == "данные проверки некорректны", check_text("never", row))
    row_clean = _row(proxy_required=1, proxy_enabled=1)
    check("9b.7 genuinely-absent timestamp is NOT flagged malformed",
          malformed(row_clean) is False, malformed(row_clean))

    # --- owner scenario 8: future timestamp beyond the 60s clock-skew
    # tolerance -> treated as invalid/stale, never a false-healthy.
    future_far = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) + timedelta(seconds=3600)).replace(microsecond=0).isoformat()
    row = _row(proxy_required=1, proxy_enabled=1, auth_guard_state="ok", auth_guard_last_ok_at=future_far)
    check("9b.8 far-future timestamp (1h ahead) -> effective 'stale', not fabricated 'healthy'",
          effective(row) == "stale", effective(row))
    # ...but a small forward skew WITHIN the 60s tolerance is still trusted.
    future_near = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) + timedelta(seconds=30)).replace(microsecond=0).isoformat()
    row = _row(proxy_required=1, proxy_enabled=1, auth_guard_state="ok", auth_guard_last_ok_at=future_near)
    check("9b.8 near-future timestamp (30s ahead, within 60s skew tolerance) -> effective 'healthy'",
          effective(row) == "healthy", effective(row))

    # --- owner scenario 9: equal success/failure timestamps -> conservative
    # deterministic result (failure wins on an exact tie, strict '>').
    tie_ts = _fresh_ts(45)
    row = _row(proxy_required=1, proxy_enabled=1, auth_guard_last_ok_at=tie_ts, auth_guard_last_bad_at=tie_ts)
    check("9b.9 [tie-break] exact-second tie -> effective 'broken' (failure wins, never masked by success)",
          effective(row) == "broken", effective(row))

    # --- owner scenario 10/11: latest-timestamp-wins (already covered in
    # test_9, re-verified here through the effective-state predicate). ---
    row = _row(proxy_required=1, proxy_enabled=1,
               auth_guard_last_bad_at=_stale_ts(3600), auth_guard_last_ok_at=_fresh_ts())
    check("9b.10 older failure + newer success -> effective 'healthy'", effective(row) == "healthy", effective(row))
    row = _row(proxy_required=1, proxy_enabled=1,
               auth_guard_last_ok_at=_stale_ts(3600), auth_guard_last_bad_at=_fresh_ts())
    check("9b.11 older success + newer failure -> effective 'broken'", effective(row) == "broken", effective(row))

    # --- owner scenario 12: direct/bypass with no proxy at all. ---
    row = _row(proxy_required=0, proxy_enabled=0)
    check("9b.12 no proxy required/enabled at all -> effective 'bypass'", effective(row) == "bypass", effective(row))
    key, _, _ = resolve(row, proc_running=True)
    check("9b.12 bypass with no proxy -> resolver never proxy_bad", key != "proxy_bad", key)

    # --- owner scenario 13: bypass with a HISTORICAL broken guard result
    # must not show a current effective failure (bypass wins outright,
    # regardless of what an old guard row happens to contain).
    row = _row(proxy_required=1, proxy_bypass_allowed=1, auth_guard_state="blocked",
               auth_guard_last_bad_at=_fresh_ts())
    check("9b.13 [R3.1] bypass-allowed with a fresh 'blocked' guard row still resolves 'bypass', "
          "never a current failure verdict", effective(row) == "bypass", effective(row))
    key, _, _ = resolve(row)
    check("9b.13 bypass never resolves proxy_bad even with a fresh broken guard row",
          key != "proxy_bad", key)

    # --- owner scenario 14: required proxy disabled/unassigned PLUS a
    # fresh OLD guard success must NOT show success (R3.2: a live config
    # gap is checked before any guard timestamp).
    row = _row(proxy_required=1, proxy_enabled=0, auth_guard_state="ok", auth_guard_last_ok_at=_fresh_ts())
    check("9b.14 [R3.2] required-but-disabled proxy with a fresh guard SUCCESS still -> 'not_configured'",
          effective(row) == "not_configured", effective(row))
    key, _, _ = resolve(row)
    check("9b.14 required-but-disabled proxy -> resolver proxy_bad despite a fresh guard success",
          key == "proxy_bad", key)

    # --- owner scenario 15: required proxy missing host/port is not
    # healthy, even with enabled=1 and a fresh guard success.
    row = _row(proxy_required=1, proxy_enabled=1, proxy_host="", proxy_port="",
               auth_guard_state="ok", auth_guard_last_ok_at=_fresh_ts())
    check("9b.15 required proxy enabled but missing host/port -> 'not_configured', not 'healthy'",
          effective(row) == "not_configured", effective(row))

    # --- R3.4: proxy_required=0 but proxy_enabled=1 (actually configured,
    # just not policy-mandatory) is NOT bypass -- its real connectivity is
    # checked like any other configured proxy.
    row = _row(proxy_required=0, proxy_enabled=1, proxy_host="1.2.3.4", proxy_port="1080",
               auth_guard_state="blocked", auth_guard_last_bad_at=_fresh_ts())
    check("9b.R3.4 proxy_required=0 but proxy_enabled=1 with a fresh failure -> effective 'broken', "
          "NOT silently 'bypass'", effective(row) == "broken", effective(row))

    # --- owner scenario 18/19: counters -- 12 managers with STALE legacy
    # failures (the exact foxy1-incident shape, now expressed through the
    # shared predicate) produce 0 proxy_bad.
    categories = ns["_PB_STATUS_CATEGORIES"]
    stale_batch = [
        _row(manager_key=f"legacy{i}", proxy_required=1, proxy_enabled=1,
             auth_guard_state="blocked", auth_guard_last_bad_at=_stale_ts(3600 + i))
        for i in range(12)
    ]
    counts = {k: 0 for k in categories}
    for r in stale_batch:
        k, _i, _l = resolve(r, proc_running=True)
        counts[k] += 1
    check("9b.19 [FOXY1 INCIDENT SHAPE] 12 managers with stale (1h+) legacy failures -> 0 proxy_bad",
          counts.get("proxy_bad", 0) == 0, counts)
    check("9b.19 sum(categories) == 12 for the stale-legacy-failure batch",
          sum(counts.values()) == 12, (counts, 12))

    # --- structural: the 15-minute TTL is the SAME named constant used by
    # both the freshness check and the badge/full-card wiring -- no second
    # magic number.
    fresh_sec = ns["_PB_PROXY_GUARD_FRESH_SEC"]
    check("9b.[TTL] _PB_PROXY_GUARD_FRESH_SEC is the owner-mandated 15 minutes (900s)",
          fresh_sec == 900, fresh_sec)
    now = datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0)
    just_inside = now - timedelta(seconds=fresh_sec - 1)
    just_outside = now - timedelta(seconds=fresh_sec + 1)
    check("9b.[TTL boundary] a timestamp 1s inside the 900s window is fresh",
          fresh_gate(just_inside, now) is True, (just_inside, now))
    check("9b.[TTL boundary] a timestamp 1s outside the 900s window is stale",
          fresh_gate(just_outside, now) is False, (just_outside, now))

    # --- HARD-CODED 10-15 minute range check (deliberately does NOT read
    # fresh_sec back from ns -- a hardcoded 10-minute TTL regression must
    # bite THIS specific check, not just the constant-value assertion
    # above, since a result in this exact range is real Auth Guard monitor
    # territory: one full 5-minute cadence past the old, insufficient
    # 10-minute window).
    row_12min = _row(proxy_required=1, proxy_enabled=1, auth_guard_state="ok",
                      auth_guard_last_ok_at=_fresh_ts(12 * 60))
    check("9b.[TTL, 10-15min range] a 12-minute-old success is trusted (fresh) under the "
          "owner's 15-minute contract -- would be wrongly 'stale' under the old 10-minute window",
          effective(row_12min) == "healthy", effective(row_12min))


# ======================================================================
# 10. No cache on the proxy/manager-status classification path (owner
#     requirement: reopening the list/card must show a refreshed status
#     without waiting for a TTL or a service restart), and a persistence-
#     shape contract test proving the resolver correctly reads exactly the
#     field set main.py's live Auth Guard success path writes
#     (_tpag_v4_save_guard + _tpag_stability_set_fields, main.py ~13233 /
#     ~17105 -- verbatim: auth_guard_ok=1, auth_guard_checked_at=<now>,
#     auth_guard_state='ok', auth_guard_last_ok_at=<now>).
# ======================================================================

def test_10_no_cache_and_persistence_contract() -> None:
    # No @lru_cache / memoization decorator anywhere on the classification
    # path -- structural proof, not just "no cache observed in this run".
    for name in ("_pb_proxy_guard_state", "_pb_manager_status_resolve",
                 "_pb_manager_status_resolve_live"):
        defs = [n for n in TREE.body if getattr(n, "name", None) == name]
        check(f"10. [NO CACHE] {name} has no decorator (structurally uncached)",
              defs and not defs[-1].decorator_list, [ast.dump(d) for d in (defs[-1].decorator_list if defs else [])])

    pure_ns = build_pure_ns()
    resolve = pure_ns["_pb_manager_status_resolve"]

    # Simulates the BEFORE/AFTER of a real Auth Guard run on the SAME row:
    # first the OLD stale failure (what foxy1 looked like at 23:04), then
    # exactly the field set main.py writes on success (what it looked like
    # at 02:03) -- two independent calls, same process, no restart, no
    # cache in between -- the second call must immediately reflect the new
    # state.
    stale_broken_row = _row(
        manager_key="foxy1", proxy_required=1, proxy_enabled=1,
        auth_guard_state="blocked", auth_guard_last_bad_at=_stale_ts(3 * 3600),
    )
    key_before, _icon, _label = resolve(stale_broken_row, proc_running=True)
    check("10. [PRE-STATE] stale old failure alone does not resolve proxy_bad "
          "(sanity for the before/after pair below)", key_before != "proxy_bad", key_before)

    # Exact field set main.py's success path writes (contract check: field
    # NAMES match _tpag_v4_save_guard's fields dict + _tpag_stability_set_
    # fields's success call verbatim).
    guard_success_fields = {
        "auth_guard_ok": 1,
        "auth_guard_checked_at": _fresh_ts(),
        "auth_guard_state": "ok",
        "auth_guard_last_ok_at": _fresh_ts(),
        "auth_guard_notified_bad": 0,
        "auth_guard_error": "",
    }
    refreshed_row = dict(stale_broken_row)
    refreshed_row.update(guard_success_fields)
    key_after, _icon, _label = resolve(refreshed_row, proc_running=True)
    check("10. [PERSISTENCE CONTRACT + NO CACHE] applying exactly the field set the live "
          "Auth Guard success path writes flips the SAME manager to 'ok' on the very next "
          "call -- no restart, no stale cache in between",
          key_after == "ok", key_after)
    check("10. [NO CACHE] the two calls are genuinely independent -- different results from "
          "the same pure function for the same manager_key, no memoized answer reused",
          key_before != key_after, (key_before, key_after))


def main() -> int:
    pure_ns = build_pure_ns()
    test_1_all_ten_categories_reachable(pure_ns)
    test_2_priority_order(pure_ns)
    test_3_mutual_exclusivity_and_sum_invariant(pure_ns)
    test_4_no_fabrication(pure_ns)
    test_9_proxy_guard_freshness_and_latest_wins(pure_ns)
    test_9b_effective_state_and_ttl_contract(pure_ns)

    db_path = _make_temp_db()
    try:
        live_ns = build_live_ns(db_path)
        test_5_process_running_detection(live_ns)
        test_6_tg_health_row_fail_open(live_ns)
        test_7_live_wrapper_wiring(db_path)
    finally:
        os.unlink(db_path)

    test_8_real_process_scan_contract()
    test_10_no_cache_and_persistence_contract()

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
