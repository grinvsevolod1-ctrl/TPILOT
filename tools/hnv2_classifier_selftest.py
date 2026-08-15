# -*- coding: utf-8 -*-
"""tools/hnv2_classifier_selftest.py -- offline self-test for the Health
Notification System V2 root-cause classifier (main.py, the "TPILOT HEALTH
NOTIFICATION V2 20260807" appended block): `_hnv2_classify_root_cause`,
`_hnv2_collect_evidence`, `_hnv2_signature`, and their dependencies.

main.py cannot be imported directly (Telethon/env side effects at import
time) -- these names are extracted for REAL via ast.parse + ast.unparse +
exec(), the same technique every other tools/*_selftest.py in this project
uses. `_hnv2_process_state` is extracted too, but its one real subprocess
dependency is replaced with a FAKE module (no real PowerShell/Get-CimInstance
calls -- this offline test never spawns a real process or queries real
process state).

Covers approved-plan selftest scenarios:
  1 (PeerFlood still fail-closed -- the protected def-count invariants),
  2 (FloodWait cooldown / D12 mislabel correction),
  5 (auth outranks recovery_flap),
  6 (stale evidence never produces a false OK),
plus the signature-stability table (plan section K.3) and the D11 proxy
start-block fix (rule 6's process_exited+degraded/blocked branch).
"""
from __future__ import annotations

import ast
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

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

REAL_NAMES = {
    # HNV2 constants
    "HNV2_ENABLED", "HNV2_ACCOUNT_MAX_AGE_SEC", "HNV2_PROXY_MAX_AGE_SEC",
    "HNV2_STUCK_STARTING_SEC", "HNV2_CONNECTED_MAX_AGE_SEC", "HNV2_EXIT_EVIDENCE_MAX_AGE_SEC",
    "HNV2_STABLE_SEC", "HNV2_STABLE_SEC_SQLITE_LOCK", "HNV2_SPAWN_VERIFY_DELAY_SEC",
    "HNV2_READY_PROBE_MIN_INTERVAL_SEC", "HNV2_DAILY_PROBLEM_CAP",
    "HNV2_UNKNOWN_CONFIRM_SEC", "HNV2_UNKNOWN_MIN_OBSERVATIONS",
    # HNV2 family constants
    "HNV2_FAMILY_SESSION_UNAUTHORIZED", "HNV2_FAMILY_ACCOUNT_BLOCKED",
    "HNV2_FAMILY_PEERFLOOD", "HNV2_FAMILY_FLOODWAIT", "HNV2_FAMILY_PROXY_AUTH_FAILED",
    "HNV2_FAMILY_PROXY_UNAVAILABLE", "HNV2_FAMILY_SQLITE_LOCK", "HNV2_FAMILY_NETWORK_TIMEOUT",
    "HNV2_FAMILY_WORKER_STUCK_STARTING", "HNV2_FAMILY_WORKER_CRASH", "HNV2_FAMILY_RECOVERY_FLAP",
    "HNV2_FAMILY_HEALTH_MISSING_STALE", "HNV2_FAMILY_OK", "HNV2_FAMILY_UNKNOWN",
    # HNV2 marker tuples
    "_HNV2_SESSION_MARKS", "_HNV2_ACCOUNT_BLOCKED_MARKS", "_HNV2_PROXY_AUTH_MARKS",
    "_HNV2_NETWORK_TIMEOUT_MARKS", "_HNV2_SQLITE_LOCK_MARKS", "_HNV2_SQLITE_MALFORMED_MARKS",
    "_HNV2_FLOODWAIT_TEXT_MARKS",
    # HNV2 functions under test
    "_hnv2_process_state", "_hnv2_collect_evidence", "_hnv2_result",
    "_hnv2_classify_root_cause", "_hnv2_normalize_sig_component", "_hnv2_signature",
    # Real dependencies these need
    "TP_HG_CHECK_INTERVAL_SEC", "TPAG_V2_MONITOR_INTERVAL_SEC",
    "TP_HG_STATUS_OK", "TP_HG_STATUS_WARNING", "TP_HG_STATUS_LIMITED",
    "TP_HG_STATUS_BLOCKED", "TP_HG_STATUS_UNKNOWN",
    "_tp_hg_parse_iso", "_tp_hg_restriction_family", "_tp_hg_stable_error_text",
    "_TP_HG_FAMILY_PEERFLOOD", "_TP_HG_FAMILY_FLOODWAIT", "_TP_HG_FAMILY_BLOCKED",
    "_TP_HG_FAMILY_WARNING", "_TP_HG_FAMILY_UNKNOWN",
    "HEALTH_AGG_RECOVERY_FLAP_THRESHOLD",
    # PeerFlood fail-closed protected names (def-count proof, scenario 1)
    "_tp_hg_send_allowed", "_send_manager_private",
    "_process_profile_reminders_once", "_process_post_manual_followups_once",
    "_health_incident_handle", "_health_incident_notify", "_m212a_write_start_status",
}


def _extract_by_names(src: str, names: set) -> list:
    tree = ast.parse(src)
    nodes = []
    seen = set()
    for n in tree.body:
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
        raise AssertionError(f"expected {names}, missing {missing}")
    return nodes


class _FakeSubprocessResult:
    def __init__(self, returncode: int, stdout: str):
        self.returncode = returncode
        self.stdout = stdout


class _FakeSubprocessModule:
    """Replaces the real `subprocess` module inside the exec namespace so
    `_hnv2_process_state` never spawns a real PowerShell/pgrep process.
    `run_calls` records every invocation; `next_result`/`next_exception`
    control what the next call returns/raises."""
    def __init__(self):
        self.run_calls: list = []
        self.next_result: Optional[_FakeSubprocessResult] = None
        self.next_exception: Optional[Exception] = None

    def run(self, args, **kwargs):
        self.run_calls.append((args, kwargs))
        if self.next_exception is not None:
            exc, self.next_exception = self.next_exception, None
            raise exc
        if self.next_result is not None:
            res, self.next_result = self.next_result, None
            return res
        return _FakeSubprocessResult(0, "")


def build_ns():
    import manager_registry
    import re as _re
    import os as _os

    fake_subprocess = _FakeSubprocessModule()
    nodes = _extract_by_names(MAIN_SRC, REAL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "os": _os, "re": _re, "sys": sys, "subprocess": fake_subprocess,
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "Any": Any, "Optional": Optional,
        "BASE_DIR": BASE_DIR,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:hnv2_classifier>", "exec"), ns)
    ns["__fake_subprocess__"] = fake_subprocess
    return ns


NS = build_ns()
classify = NS["_hnv2_classify_root_cause"]
collect = NS["_hnv2_collect_evidence"]
signature = NS["_hnv2_signature"]


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


NOW = datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None)


def ev(mrow=None, hrow=None, ss=None, proc="running", flap_new=0, last_verify="none"):
    return collect("mgr_x", mrow or {}, hrow or {}, ss or {}, proc, flap_new, last_verify)


# ======================================================================
# Scenario 1: PeerFlood remains fail-closed -- protected def-count
# invariants, re-verified from the SAME extraction this file itself uses
# (proves the classifier's own extraction didn't inadvertently shadow or
# duplicate a protected name).
# ======================================================================

def test_scenario_1_peerflood_protected_invariants():
    print("\n-- Scenario 1: PeerFlood fail-closed protected invariants --")
    defs = Counter(n.name for n in MAIN_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)))
    for name, expected in (
        # OVERRIDE CLEANUP 20260815: _send_manager_private 4 -> 3 (one dead
        # shadowed def removed; active chain unchanged).
        ("_tp_hg_send_allowed", 1), ("_send_manager_private", 3),
        ("_process_profile_reminders_once", 2), ("_process_post_manual_followups_once", 2),
        ("_health_incident_handle", 1), ("_health_incident_notify", 1),
        ("_m212a_write_start_status", 1),
        ("_manager_recovery_classify", 1), ("_manager_recovery_open_auth_incident", 1),
        ("_manager_recovery_resolve_auth_incident", 1),
    ):
        check(f"1. {name} defined exactly {expected} time(s)", defs.get(name, 0) == expected, defs.get(name, 0))

    src = MAIN_SRC
    check("1. 'gate_error_fail_open' never returns anywhere in main.py", "gate_error_fail_open" not in src, None)
    check("1. 'existing_dialog_allowed' never returns anywhere in main.py", "existing_dialog_allowed" not in src, None)
    check("1. '_TP_HG_GATE_DENY_LIMITED_RESTRICTION' is present (R2 fail-closed constant)", "_TP_HG_GATE_DENY_LIMITED_RESTRICTION" in src, None)

    # HNV2 must not define any new stacked override of the protected names.
    hnv2_start = src.index("TPILOT HEALTH NOTIFICATION V2 20260807 START")
    hnv2_end = src.index("TPILOT HEALTH NOTIFICATION V2 20260807 END")
    hnv2_block = src[hnv2_start:hnv2_end]
    for protected in (
        "_tp_hg_send_allowed", "_send_manager_private", "_process_profile_reminders_once",
        "_process_post_manual_followups_once", "_health_incident_handle", "_health_incident_notify",
        "_manager_recovery_classify", "_manager_recovery_open_auth_incident",
        "_manager_recovery_resolve_auth_incident", "_m212a_write_start_status",
    ):
        check(f"1. HNV2 block does not define {protected}", f"def {protected}(" not in hnv2_block, None)


# ======================================================================
# Scenario 2: FloodWait cooldown -- deferred to TP_HG, never independently
# alerted/actioned by HNV2. Plus the D12 mislabel correction (a startup
# FloodWait, filed by _m212a_classify_exit_reason as reason_class=
# 'auth_session', must classify as floodwait -- not session_unauthorized).
# ======================================================================

def test_scenario_2_floodwait_deferred_and_d12():
    print("\n-- Scenario 2: FloodWait deferred to TP_HG + D12 mislabel correction --")
    hrow = {"health_status": "limited", "error_class": "FloodWaitError", "cooldown_until": _iso(NOW + timedelta(minutes=30))}
    r = classify(ev(hrow=hrow))
    check("2a. FloodWait classifies as family=floodwait", r["family"] == "floodwait", r)
    check("2b. floodwait is never alertable by HNV2 (TP_HG owns it)", r["alertable"] is False, r)
    check("2c. floodwait is explicitly deferred_to='tg_health'", r["deferred_to"] == "tg_health", r)

    # D12: a FloodWaitError-derived exit, mislabeled 'auth_session' by
    # _m212a_classify_exit_reason (main.py, unchanged/not-to-be-touched per
    # the approved plan), must NOT be classified session_unauthorized.
    #
    # F-1/F-2 correction (post-independent-review): the ORIGINAL fixture here
    # supplied only last_exit_reason_class/last_exit_error and OMITTED the
    # CURRENT-tick reason_class/error fields entirely -- but _m212a_write_
    # start_status ALWAYS writes both current AND sticky fields together on
    # a phase='exited' write (main.py _m212a_write_start_status: 'reason_class'/
    # 'error' hold the CURRENT write's kwargs; 'last_exit_reason_class'/
    # 'last_exit_error' are set from the SAME kwargs whenever phase=='exited').
    # A start_status.json shaped like the old fixture (sticky fields set,
    # current fields absent) cannot actually occur at the exact tick right
    # after an exit -- it can only occur strictly LATER, after a respawn has
    # already reset the current fields to ''. Testing ONLY that later shape
    # let a classifier bug survive: the ORIGINAL rule-1 branch-2 guard checked
    # `"floodwait" not in ss_reason_class` -- but ss_reason_class is always one
    # of a small fixed label set ('auth_session' etc.) and NEVER itself
    # contains the word "floodwait", so that guard was a no-op and this
    # fixture never exercised it. Independent Opus 5 review reproduced this
    # with the exact-at-exit shape (current + sticky both set) and got
    # family=session_unauthorized. Also: real Telethon FloodWaitError.__str__
    # is "A wait of {seconds} seconds is required (caused by ...)" -- it does
    # NOT contain the literal word "floodwait" at all, so a fixture using the
    # literal string "FloodWaitError(seconds=3600)" (which DOES contain
    # "floodwait") was itself unrealistic and would not have caught a
    # classifier that only matched that one substring. This block now covers
    # all four production-realistic shapes.

    # D12-A: exact-at-exit shape -- current reason_class/error freshly
    # written, sticky last_exit_* mirrors them (this is what start_status.json
    # actually looks like the moment the 'exited' write happens).
    ss_d12a = {"phase": "exited", "reason_class": "auth_session",
               "error": "A wait of 3600 seconds is required (caused by SendCodeRequest)",
               "last_exit_reason_class": "auth_session",
               "last_exit_error": "A wait of 3600 seconds is required (caused by SendCodeRequest)",
               "last_exit_at": _iso(NOW)}
    r2a = classify(ev(ss=ss_d12a))
    check("2d-A. D12 exact-at-exit shape (current+sticky both set, real Telethon message text) classifies as floodwait, NOT session_unauthorized",
          r2a["family"] == "floodwait", r2a)
    check("2e-A. D12-A is also non-alertable / deferred to tg_health", r2a["alertable"] is False and r2a["deferred_to"] == "tg_health", r2a)

    # D12-B: post-respawn steady state -- current fields reset to '' by the
    # next 'starting' write, only sticky last_exit_* survives (D1).
    ss_d12b = {"phase": "starting", "reason_class": "", "error": "",
               "last_exit_reason_class": "auth_session",
               "last_exit_error": "A wait of 3600 seconds is required (caused by SendCodeRequest)",
               "last_exit_at": _iso(NOW - timedelta(minutes=5)), "updated_at": _iso(NOW - timedelta(seconds=5))}
    r2b = classify(ev(ss=ss_d12b, proc="running"))
    check("2d-B. D12 post-respawn steady state (sticky only, current reset) classifies as floodwait, NOT session_unauthorized",
          r2b["family"] == "floodwait", r2b)
    check("2e-B. D12-B is also non-alertable / deferred to tg_health", r2b["alertable"] is False and r2b["deferred_to"] == "tg_health", r2b)

    # D12-C: dynamic seconds value + "(caused by ...)" variant must not
    # defeat the match (the number and the RPC name are volatile).
    ss_d12c = {"phase": "exited", "reason_class": "auth_session",
               "error": "A wait of 91234 seconds is required (caused by GetUsersRequest)",
               "last_exit_reason_class": "auth_session",
               "last_exit_error": "A wait of 91234 seconds is required (caused by GetUsersRequest)",
               "last_exit_at": _iso(NOW)}
    r2c = classify(ev(ss=ss_d12c))
    check("2d-C. D12 dynamic seconds/caused-by variant still classifies as floodwait",
          r2c["family"] == "floodwait", r2c)

    # D12-D: the literal exception-repr style text ("FloodWaitError(seconds=N)")
    # some log call sites may still produce -- must also still match.
    ss_d12d = {"phase": "exited", "reason_class": "auth_session",
               "error": "FloodWaitError(seconds=1800)",
               "last_exit_reason_class": "auth_session",
               "last_exit_error": "FloodWaitError(seconds=1800)",
               "last_exit_at": _iso(NOW)}
    r2d = classify(ev(ss=ss_d12d))
    check("2d-D. D12 literal FloodWaitError(seconds=N) repr-style text still classifies as floodwait",
          r2d["family"] == "floodwait", r2d)

    # Peerflood: also deferred, never alertable.
    hrow_pf = {"health_status": "limited", "error_class": "PeerFloodError"}
    r3 = classify(ev(hrow=hrow_pf))
    check("2f. PeerFlood classifies as family=telegram_limited_peerflood", r3["family"] == "telegram_limited_peerflood", r3)
    check("2g. peerflood is never alertable by HNV2", r3["alertable"] is False and r3["deferred_to"] == "tg_health", r3)

    # Genuine session_unauthorized (no floodwait text anywhere) must NOT be
    # reclassified as floodwait by the D12 exclusion -- and this fixture now
    # ALSO carries the current-tick fields (the realistic exact-at-exit
    # shape), so a classifier that regressed to "always floodwait" would be
    # caught here too.
    ss_real = {"phase": "exited", "reason_class": "session_unauthorized", "error": "no phone number set",
               "last_exit_reason_class": "session_unauthorized",
               "last_exit_error": "no phone number set", "last_exit_at": _iso(NOW)}
    r4 = classify(ev(ss=ss_real))
    check("2h. a genuine session_unauthorized exit (no floodwait text, exact-at-exit shape) is NOT reclassified as floodwait", r4["family"] == "session_unauthorized", r4)

    # Negative control: session_unauthorized must still beat recovery_flap
    # even with the F-1 fix in place (guards against an over-broad fix that
    # accidentally also swallows genuine auth failures under flap pressure).
    ss_flap = {"phase": "exited", "last_exit_reason_class": "session_unauthorized",
               "last_exit_error": "", "last_exit_at": _iso(NOW)}
    r5 = classify(ev(ss=ss_flap, proc="absent", flap_new=5, last_verify="failed"))
    check("2i. genuine session_unauthorized still outranks recovery_flap after the F-1 fix", r5["family"] == "session_unauthorized", r5)


# ======================================================================
# Scenario 5: auth outranks recovery_flap. Even with a raging flap
# (flap_new=5, last_verify='failed'), an authoritative session_unauthorized
# signal wins.
# ======================================================================

def test_scenario_5_auth_outranks_flap():
    print("\n-- Scenario 5: session_unauthorized outranks recovery_flap --")
    ss = {"phase": "starting", "last_exit_reason_class": "session_unauthorized",
          "last_exit_error": "no phone number set", "last_exit_at": _iso(NOW)}
    r = classify(ev(ss=ss, flap_new=5, last_verify="failed"))
    check("5a. family is session_unauthorized despite flap_new=5", r["family"] == "session_unauthorized", r)
    check("5b. signature is hv2:session_unauthorized:session_unauthorized", signature(r["family"], r["evidence_key"]) == "hv2:session_unauthorized:session_unauthorized", (r, signature(r["family"], r["evidence_key"])))

    # Control: the SAME flap evidence with no competing cause DOES produce
    # recovery_flap (proves rule 11 is reachable, not dead code).
    r2 = classify(ev(flap_new=5, last_verify="failed"))
    check("5c. control: with no competing authoritative cause, flap_new=5 does classify as recovery_flap", r2["family"] == "recovery_flap", r2)

    # And last_verify='ok' must suppress recovery_flap even with flap_new
    # over threshold (D2/D9 -- a verified spawn is transient churn, not a
    # flap).
    r3 = classify(ev(flap_new=5, last_verify="ok"))
    check("5d. last_verify=='ok' suppresses recovery_flap even with flap_new over threshold", r3["family"] != "recovery_flap", r3)


# ======================================================================
# Scenario 6: stale evidence never produces a false OK (D4).
# ======================================================================

def test_scenario_6_stale_never_ok():
    print("\n-- Scenario 6: stale evidence never yields a false OK --")
    stale_hrow = {"health_status": "ok", "last_check_at": _iso(NOW - timedelta(seconds=4000))}
    stale_mrow = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW - timedelta(seconds=4000))}
    r = classify(ev(mrow=stale_mrow, hrow=stale_hrow))
    check("6a. both sources stale (4000s > 3600s/1200s thresholds) -> health_missing_stale", r["family"] == "health_missing_stale", r)
    check("6b. evidence_key == 'both' when both sources are stale", r["evidence_key"] == "both", r)

    fresh_hrow = {"health_status": "ok", "last_check_at": _iso(NOW - timedelta(seconds=600))}
    fresh_mrow = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW - timedelta(seconds=600))}
    r2 = classify(ev(mrow=fresh_mrow, hrow=fresh_hrow))
    check("6c. both sources fresh (600s) -> ok", r2["family"] == "ok", r2)
    check("6d. ok family is never alertable", r2["alertable"] is False, r2)

    # Only the account side stale -> evidence_key == 'account'.
    r3 = classify(ev(mrow=fresh_mrow, hrow=stale_hrow))
    check("6e. only account stale -> health_missing_stale, evidence_key=='account'", r3["family"] == "health_missing_stale" and r3["evidence_key"] == "account", r3)

    # Only the proxy side stale -> evidence_key == 'proxy'.
    r4 = classify(ev(mrow=stale_mrow, hrow=fresh_hrow))
    check("6f. only proxy stale -> health_missing_stale, evidence_key=='proxy'", r4["family"] == "health_missing_stale" and r4["evidence_key"] == "proxy", r4)

    # A sticky negative (blocked) at ANY age must never be read as ok --
    # rule 2 (account_blocked, never age-gated) must outrank rule 12/13
    # entirely regardless of how stale last_check_at is.
    ancient_blocked_hrow = {"health_status": "blocked", "error_class": "UserDeactivatedBanError",
                             "last_check_at": _iso(NOW - timedelta(days=30))}
    r5 = classify(ev(hrow=ancient_blocked_hrow))
    check("6g. a sticky BLOCKED negative at any age is never read as OK or as stale-unknown", r5["family"] == "account_blocked", r5)

    # No data at all -> health_missing_stale (not silently OK, not a
    # fabricated crash).
    r6 = classify(ev(mrow={}, hrow={}))
    check("6h. completely absent evidence -> health_missing_stale, never OK", r6["family"] == "health_missing_stale" and r6["evidence_key"] == "both", r6)


# ======================================================================
# D11: proxy start-block respawn-loop fix -- process_exited +
# blocked/degraded proxy classifies as proxy_unavailable, not a bare
# worker_crash (which would leave it eligible for the generic recovery
# respawn path that manufactured the original flap).
# ======================================================================

def test_d11_proxy_start_block():
    print("\n-- D11: proxy start-block respawn-loop fix --")
    ss = {"phase": "exited", "last_exit_reason_class": "process_exited", "last_exit_at": _iso(NOW)}
    r = classify(ev(mrow={"auth_guard_state": "blocked"}, ss=ss))
    check("D11a. process_exited + auth_guard_state=blocked -> proxy_unavailable, not worker_crash", r["family"] == "proxy_unavailable", r)
    r2 = classify(ev(mrow={"auth_guard_state": "degraded"}, ss=ss))
    check("D11b. process_exited + auth_guard_state=degraded -> proxy_unavailable, not worker_crash", r2["family"] == "proxy_unavailable", r2)

    # Control: process_exited with a HEALTHY proxy genuinely IS worker_crash.
    r3 = classify(ev(mrow={"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW)}, ss=ss, proc="absent"))
    check("D11c. control: process_exited + healthy proxy -> worker_crash (D11 fix does not over-suppress)", r3["family"] == "worker_crash", r3)


# ======================================================================
# worker_stuck_starting (D5) + the phase='running' never-age-gated
# invariant.
# ======================================================================

def test_worker_stuck_starting_and_running_invariant():
    print("\n-- D5: worker_stuck_starting + phase='running' never age-gated --")
    ss_stuck = {"phase": "starting", "updated_at": _iso(NOW - timedelta(seconds=400))}
    r = classify(ev(ss=ss_stuck, proc="running"))
    check("D5a. phase='starting' for 400s (>=300s threshold) with a live process -> worker_stuck_starting", r["family"] == "worker_stuck_starting", r)

    ss_not_yet = {"phase": "starting", "updated_at": _iso(NOW - timedelta(seconds=100))}
    r2 = classify(ev(ss=ss_not_yet, proc="running"))
    check("D5b. phase='starting' for only 100s (<300s) does NOT yet classify as stuck", r2["family"] != "worker_stuck_starting", r2)

    # phase='running', arbitrarily old updated_at -- must NEVER be
    # age-gated (the documented invariant this rule deliberately excludes).
    ss_running_old = {"phase": "running", "updated_at": _iso(NOW - timedelta(days=10))}
    r3 = classify(ev(ss=ss_running_old, proc="running", mrow={"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW)}, hrow={"health_status": "ok", "last_check_at": _iso(NOW)}))
    check("D5c. phase='running' 10 days old is NEVER classified as stuck (never age-gated)", r3["family"] != "worker_stuck_starting", r3)


# ======================================================================
# worker_crash + probe_error must NEVER become worker_crash.
# ======================================================================

def test_worker_crash_and_probe_error_safety():
    print("\n-- worker_crash rules + probe_error safety --")
    ss_exited = {"phase": "exited", "last_exit_reason_class": "unknown", "last_exit_at": _iso(NOW)}
    r = classify(ev(ss=ss_exited, proc="absent"))
    check("wc-a. proc=absent + phase=exited + reason=unknown -> worker_crash", r["family"] == "worker_crash", r)

    ss_no_exit = {"phase": "running", "updated_at": _iso(NOW)}
    r2 = classify(ev(ss=ss_no_exit, proc="absent"))
    check("wc-b. proc=absent + phase=running (no exit record) -> worker_crash, detail=no_exit_record", r2["family"] == "worker_crash" and r2["detail"] == "no_exit_record", r2)

    # The critical safety property: probe_error can NEVER produce
    # worker_crash, regardless of what the OTHER evidence looks like.
    # Uses deliberately ambiguous hrow/mrow (health_status='warning' is
    # neither 'ok' nor 'blocked'/'limited'/empty, auth_guard_state is
    # fresh-'ok') so this evidence bundle would otherwise fall through
    # every other rule -- isolating proc=='probe_error' as the one
    # variable under test, rather than accidentally exercising rule 12
    # (health_missing_stale, which empty hrow/mrow would trigger
    # regardless of proc) or rule 13 (ok, which a fresh 'ok' hrow would
    # trigger regardless of proc).
    ambiguous_hrow = {"health_status": "warning", "last_check_at": _iso(NOW)}
    ambiguous_mrow = {"auth_guard_state": "ok", "auth_guard_last_ok_at": _iso(NOW)}
    r3 = classify(ev(mrow=ambiguous_mrow, hrow=ambiguous_hrow, ss=ss_exited, proc="probe_error"))
    check("wc-c. proc=probe_error NEVER classifies as worker_crash (falls to unknown)", r3["family"] != "worker_crash", r3)
    check("wc-d. proc=probe_error with otherwise-ambiguous evidence -> family=unknown, confidence=none", r3["family"] == "unknown" and r3["confidence"] == "none", r3)


# ======================================================================
# sqlite_lock vs malformed-DB exclusion.
# ======================================================================

def test_sqlite_lock_vs_malformed():
    print("\n-- sqlite_lock family + malformed-DB exclusion --")
    ss_lock = {"phase": "exited", "last_exit_error": "sqlite3.OperationalError: database is locked", "last_exit_at": _iso(NOW)}
    r = classify(ev(ss=ss_lock))
    check("lock-a. 'database is locked' in last_exit_error -> sqlite_lock", r["family"] == "sqlite_lock", r)

    ss_malformed = {"phase": "exited", "last_exit_reason_class": "unknown",
                     "last_exit_error": "sqlite3.DatabaseError: database disk image is malformed", "last_exit_at": _iso(NOW)}
    r2 = classify(ev(ss=ss_malformed, proc="absent"))
    check("lock-b. 'malformed' text is EXCLUDED from sqlite_lock even though it could otherwise match", r2["family"] != "sqlite_lock", r2)
    check("lock-c. malformed DB falls through to worker_crash with detail='db_malformed'", r2["family"] == "worker_crash" and r2["detail"] == "db_malformed", r2)


# ======================================================================
# Signature stability table (plan section K.3): the same underlying
# failure, observed across different PID/respawn_seq/attempt_number/
# timestamp/cooldown_until/variable exception text, must produce a
# byte-identical signature.
# ======================================================================

def test_signature_stability():
    print("\n-- Signature stability across PID/retry/timestamp/cooldown/exception-text variation --")
    base_sig = None
    variants = [
        {"last_exit_error": "AuthKeyUnregisteredError (caused by SendMessageRequest)"},
        {"last_exit_error": "AuthKeyUnregisteredError (caused by ImportContactsRequest)"},
        {"last_exit_error": "AuthKeyUnregisteredError 12345"},
        {"last_exit_error": "AuthKeyUnregisteredError 999999999"},
    ]
    sigs = []
    for i, extra in enumerate(variants):
        ss = {
            "phase": "exited",
            "last_exit_reason_class": "session_unauthorized",
            "last_exit_at": _iso(NOW),
        }
        ss.update(extra)
        r = classify(ev(ss=ss))
        sig = signature(r["family"], r["evidence_key"])
        sigs.append(sig)
    check("sig-a. all 4 variants (different PID-equivalent free text) produce the SAME signature",
          len(set(sigs)) == 1, sigs)

    # Different families/evidence_keys MUST produce different signatures.
    r_pf = classify(ev(hrow={"health_status": "limited", "error_class": "PeerFloodError"}))
    r_su = classify(ev(ss={"phase": "exited", "last_exit_reason_class": "session_unauthorized", "last_exit_at": _iso(NOW)}))
    check("sig-b. distinct root causes produce distinct signatures",
          signature(r_pf["family"], r_pf["evidence_key"]) != signature(r_su["family"], r_su["evidence_key"]),
          (r_pf, r_su))

    # Signature never exceeds the 80-byte cap (SQLite PK + 64-byte
    # callback_data budget headroom).
    long_sig = signature("session_unauthorized", "x" * 200)
    check("sig-c. signature is capped at 80 bytes even with pathological input", len(long_sig.encode("utf-8")) <= 80, len(long_sig.encode("utf-8")))
    check("sig-d. signature always starts with the 'hv2:' namespace", long_sig.startswith("hv2:"), long_sig)


# ======================================================================
# Classifier never raises -- pathological/garbage evidence dicts must
# always degrade to 'unknown', never propagate an exception.
# ======================================================================

def test_classifier_never_raises():
    print("\n-- Classifier robustness: never raises on garbage input --")
    garbage_inputs = [
        {}, {"mrow": None, "hrow": None, "ss": None, "proc": None, "flap_new": "not_a_number", "last_verify": None},
        {"mrow": {"auth_guard_state": 12345}, "hrow": {"health_status": object()}, "ss": {}, "proc": "running", "flap_new": 0, "last_verify": "none"},
    ]
    for i, g in enumerate(garbage_inputs):
        try:
            r = classify(g)
            ok = isinstance(r, dict) and "family" in r
        except Exception as e:
            ok = False
            r = repr(e)
        check(f"robust-{i}. garbage input #{i} never raises, always returns a dict with 'family'", ok, r)


def main() -> int:
    test_scenario_1_peerflood_protected_invariants()
    test_scenario_2_floodwait_deferred_and_d12()
    test_scenario_5_auth_outranks_flap()
    test_scenario_6_stale_never_ok()
    test_d11_proxy_start_block()
    test_worker_stuck_starting_and_running_invariant()
    test_worker_crash_and_probe_error_safety()
    test_sqlite_lock_vs_malformed()
    test_signature_stability()
    test_classifier_never_raises()

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
