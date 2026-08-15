# -*- coding: utf-8 -*-
"""tools/tg_health_peerflood_recovery_selftest.py -- dedicated offline
self-test for the "PEERFLOOD RECOVERY 20260812" patch: LIMITED+PeerFlood no
longer permanently self-blocks TPilot's own outgoing sends.

This file complements (does not duplicate) the primitive-level coverage in
tools/tg_health_recovery_selftest.py (Groups B/C/D there prove the gate/
recovery/anti-flap primitives directly) and tools/manager_status_resolver_
selftest.py (the panel category resolver). This file's own job is the two
pieces of coverage neither of those already has:

  - LOOP-LEVEL behavior: does a REAL execution of the base bodies of
    _process_post_manual_followups_once / _process_profile_reminders_once
    (main.py) actually stop after the FIRST restriction failure in a tick
    (never attempting a second candidate), and never mark the lead/reminder
    trash for it? (matrix D, E below) -- this can only be proven by running
    the real function body against a scripted send stub, not by a static
    source-pattern check.
  - PANEL BADGE wiring: does the new _pb_tg_health_badge /
    _pb_manager_status_resolve_live_badged (panel_bot.py, P2) actually
    return the right badge for an active+running+proxy-ok+LIMITED manager,
    on top of the (already separately tested) category resolver? (matrix K)

Technique: same AST-extraction + exec() approach as every other
tools/*_selftest.py in this project (main.py/panel_bot.py cannot be
imported directly -- Telethon/env side effects at import time).

Matrix covered (owner's letter labels):
  A. LIMITED + PeerFlood -> gate allow, reason limited_probe_passthrough
  B. Successful real-send simulation -> LIMITED -> OK, incident resolved
  C. Repeated PeerFlood -> LIMITED remains/reopens, no permanent gate block
  D. Two PeerFlood candidate sends in same tick -> only first attempted,
     tick breaks
  E. Restriction failure -> lead NOT trash, profile reminder NOT trash
  F. Fast LIMITED->OK->LIMITED -> notification dedup preserved, no
     alert/recovery spam pair. CORRECTIVE 20260812b (finding B-1): this
     scenario was claimed here but had NO test before this correction --
     independent review measured the real gap (5 flap cycles -> 1 alert but
     5 recoveries) directly against storage.py. Now genuinely tested by
     test_f_flap_notification_dedup (real _tp_hg_notify_if_needed execution,
     real storage.py, temp sqlite) with a mutation proof
     (test_f_mutation_proof) showing the count reverts to the exact
     5-recoveries-for-5-flaps shape when the new guard is removed in-memory.
  G. BLOCKED -> deny
  H. manual_mark -> deny
  I. active FloodWait cooldown -> deny
  J. expired FloodWait -> existing behavior preserved
  K. active/running/proxy-ok + LIMITED -> runtime category ACTIVE, TG badge
  L. normal OK manager -> regression PASS
  M. def-count invariants

    python tools\\tg_health_peerflood_recovery_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    # Windows consoles often default to a non-UTF-8 codepage, which cannot
    # encode the Cyrillic/emoji text this module builds -- reconfigure
    # stdout/stderr so printing check() results never crashes the run.
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage  # noqa: E402  -- real module, no Telethon/env side effects at import time

FAILURES: List[str] = []


def check(label: str, condition: bool, detail: Any = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = BASE_DIR / "main.py"
MAIN_SRC = MAIN_PATH.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
PANEL_PATH = BASE_DIR / "panel_bot.py"
PANEL_SRC = PANEL_PATH.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")

MAIN_TREE = ast.parse(MAIN_SRC, filename=str(MAIN_PATH))
PANEL_TREE = ast.parse(PANEL_SRC, filename=str(PANEL_PATH))


def find_defs(tree: ast.AST, name: str) -> list:
    """Every top-level def/async-def/Assign node with this name, in source order."""
    out = []
    for n in getattr(tree, "body", []):
        nm = getattr(n, "name", None)
        if nm == name:
            out.append(n)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name:
            out.append(n)
    return out


def segment(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ast.unparse(node)


def last_def_source(tree: ast.AST, source: str, name: str) -> str:
    defs = find_defs(tree, name)
    if not defs:
        raise AssertionError(f"{name} not found")
    return segment(source, defs[-1])


def first_def_source(tree: ast.AST, source: str, name: str) -> str:
    defs = find_defs(tree, name)
    if not defs:
        raise AssertionError(f"{name} not found")
    return segment(source, defs[0])


# ======================================================================
# M. def-count invariants (checked first -- everything else assumes these).
# ======================================================================

def test_m_def_counts() -> None:
    print("\n-- M: def-count invariants --")
    gate_defs = find_defs(MAIN_TREE, "_tp_hg_send_allowed")
    send_defs = find_defs(MAIN_TREE, "_send_manager_private")
    send_ex_defs = find_defs(MAIN_TREE, "_send_manager_private_ex")
    resolve_defs = find_defs(PANEL_TREE, "_pb_manager_status_resolve")
    check("M1. _tp_hg_send_allowed has exactly 1 definition", len(gate_defs) == 1, len(gate_defs))
    check("M2. _send_manager_private has exactly 4 definitions (3 dead + 1 active thin wrapper)", len(send_defs) == 4, len(send_defs))
    check("M3. _send_manager_private_ex has exactly 1 definition (the real send body)", len(send_ex_defs) == 1, len(send_ex_defs))
    check("M4. panel_bot.py's _pb_manager_status_resolve has exactly 1 definition", len(resolve_defs) == 1, len(resolve_defs))


# ======================================================================
# A/B/C/G/H/I/J: gate + recovery primitives, executed for real via an
# isolated exec() of the active _tp_hg_send_allowed / _tp_hg_may_recover_
# on_send_success bodies (same technique tg_health_recovery_selftest.py
# uses for the gate alone -- this harness additionally exercises the
# recovery-eligibility helper together with it).
# ======================================================================

async def test_abc_gate_and_recovery_primitives() -> None:
    print("\n-- A/B/C/G/H/I/J: gate allow/deny + recovery-eligibility matrix --")
    gate_node = find_defs(MAIN_TREE, "_tp_hg_send_allowed")[-1]
    recover_node = find_defs(MAIN_TREE, "_tp_hg_may_recover_on_send_success")[-1]
    isolated = ast.Module(body=[gate_node, recover_node], type_ignores=[])
    ast.fix_missing_locations(isolated)

    current: Dict[str, Any] = {}

    async def row_loader(_key: str):
        return dict(current)

    def norm(value: str) -> str:
        return str(value or "").strip().lower()

    def family(*, status: str, error_class: str) -> str:
        if status == "blocked":
            return "blocked"
        value = str(error_class or "").lower()
        if "floodwait" in value:
            return "floodwait"
        return "peerflood"

    def parse_iso(value: str):
        value = str(value or "").strip()
        return datetime.fromisoformat(value) if value else None

    namespace: Dict[str, Any] = {
        "Tuple": tuple,
        "datetime": datetime,
        "TP_HG_STATUS_LIMITED": "limited",
        "TP_HG_STATUS_BLOCKED": "blocked",
        "_TP_HG_FAMILY_FLOODWAIT": "floodwait",
        "_TP_HG_FAMILY_PEERFLOOD": "peerflood",
        "_TP_HG_GATE_DENY_BLOCKED": "blocked",
        "_TP_HG_GATE_DENY_MANUAL_MARK": "manual_mark_active",
        "_TP_HG_GATE_DENY_FLOODWAIT_COOLDOWN": "floodwait_cooldown_active",
        "_TP_HG_GATE_DENY_LIMITED_RESTRICTION": "limited_restriction_active",
        "_TP_HG_GATE_ALLOW_LIMITED_PROBE": "limited_probe_passthrough",
        "_tp_hg_norm_key": norm,
        "_tp_hg_row": row_loader,
        "_tp_hg_restriction_family": family,
        "_tp_hg_parse_iso": parse_iso,
    }
    exec(compile(isolated, str(MAIN_PATH), "exec"), namespace, namespace)
    gate = namespace["_tp_hg_send_allowed"]
    may_recover = namespace["_tp_hg_may_recover_on_send_success"]

    # A. LIMITED + PeerFlood -> gate allow, reason limited_probe_passthrough.
    current.clear()
    current.update({"health_status": "limited", "error_class": "PeerFloodError", "error_source": "client_auto_send", "cooldown_until": ""})
    allowed, reason = await gate("darias", new_dialog=False)
    check("A. LIMITED+PeerFlood -> gate allow", allowed is True, allowed)
    check("A2. reason is limited_probe_passthrough", reason == "limited_probe_passthrough", reason)
    allowed_new, reason_new = await gate("darias", new_dialog=True)
    check("A3. same allow for new_dialog=True", allowed_new is True and reason_new == "limited_probe_passthrough", (allowed_new, reason_new))

    # B (primitive half). Recovery is eligible for the peerflood family
    # (the actual OK write + incident resolve is proven end-to-end in
    # tg_health_recovery_selftest.py's Group B/C -- this checks the
    # decision the send path relies on).
    recover_peerflood = await may_recover("darias")
    check("B. may_recover_on_send_success(peerflood LIMITED) -> True (eligible)", recover_peerflood is True, recover_peerflood)

    # C. Repeated PeerFlood -> still allow (no permanent internal deny even
    # after "many" observations -- the gate is stateless per call, so
    # calling it N times in a row while the row stays LIMITED must keep
    # returning allow every time, never flipping to a sticky deny).
    for i in range(5):
        allowed_i, reason_i = await gate("darias", new_dialog=False)
        check(f"C.{i}. repeated PeerFlood check #{i} still allows (no permanent internal deny)", allowed_i is True and reason_i == "limited_probe_passthrough", (allowed_i, reason_i))

    # G. BLOCKED -> deny, and never recoverable via an ordinary send.
    current.clear()
    current.update({"health_status": "blocked", "error_class": "AuthKeyUnregisteredError", "error_source": "client_auto_send", "cooldown_until": ""})
    allowed_blocked, reason_blocked = await gate("darias", new_dialog=False)
    check("G. BLOCKED -> gate deny", allowed_blocked is False and reason_blocked == "blocked", (allowed_blocked, reason_blocked))
    recover_blocked = await may_recover("darias")
    check("G2. BLOCKED is never recoverable via an ordinary send", recover_blocked is False, recover_blocked)

    # H. manual_mark -> deny, and never recoverable via an ordinary send
    # even though the family (peerflood) is otherwise allowed through.
    current.clear()
    current.update({"health_status": "limited", "error_class": "ManualLimited", "error_source": "manual_mark", "cooldown_until": ""})
    allowed_manual, reason_manual = await gate("darias", new_dialog=False)
    check("H. manual_mark -> gate deny", allowed_manual is False and reason_manual == "manual_mark_active", (allowed_manual, reason_manual))
    recover_manual = await may_recover("darias")
    check("H2. manual_mark is never recoverable via an ordinary send", recover_manual is False, recover_manual)

    # I. Active FloodWait cooldown -> deny.
    current.clear()
    current.update({
        "health_status": "limited", "error_class": "FloodWaitError", "error_source": "client_auto_send",
        "cooldown_until": (datetime.utcnow().replace(microsecond=0) + timedelta(minutes=30)).isoformat(),
    })
    allowed_fw, reason_fw = await gate("darias", new_dialog=False)
    check("I. active FloodWait cooldown -> gate deny", allowed_fw is False and reason_fw == "floodwait_cooldown_active", (allowed_fw, reason_fw))

    # J. Expired FloodWait -> allow (existing behavior preserved), and
    # recoverable via an ordinary send (unchanged -- floodwait always was).
    current["cooldown_until"] = (datetime.utcnow().replace(microsecond=0) - timedelta(minutes=1)).isoformat()
    allowed_fw2, reason_fw2 = await gate("darias", new_dialog=False)
    check("J. expired FloodWait -> gate allow (existing behavior preserved)", allowed_fw2 is True and reason_fw2 == "floodwait_cooldown_elapsed", (allowed_fw2, reason_fw2))
    recover_fw = await may_recover("darias")
    check("J2. FloodWait family is recoverable via an ordinary successful send", recover_fw is True, recover_fw)

    # L (primitive half). A normal OK manager -> allow, and status_ok reason
    # (regression: nothing above should have broken the plain non-LIMITED
    # path).
    current.clear()
    current.update({"health_status": "ok"})
    allowed_ok, reason_ok = await gate("darias", new_dialog=True)
    check("L. a normal OK manager -> gate allow, reason status_ok (regression)", allowed_ok is True and reason_ok == "status_ok", (allowed_ok, reason_ok))
    recover_ok = await may_recover("darias")
    check("L2. an OK manager is trivially 'recoverable' (nothing sticky to protect)", recover_ok is True, recover_ok)


# ======================================================================
# D/E: loop-level "one probe per tick" + "never trash on restriction" --
# real execution of the base bodies of _process_post_manual_followups_once
# and _process_profile_reminders_once (main.py), with two queued candidates
# and a scripted _send_manager_private_ex fake that fails the FIRST
# candidate with fail_kind="restriction". Only the base def carries the
# real logic (the second def in main.py is a content-refresh debounce
# wrapper that delegates to a captured reference to the base -- same
# convention tg_health_recovery_selftest.py's caller-matrix checks rely on).
# ======================================================================

async def test_de_post_followup_loop_stops_after_one_restriction() -> None:
    print("\n-- D/E: _process_post_manual_followups_once stops after one restriction, never trashes --")
    node = find_defs(MAIN_TREE, "_process_post_manual_followups_once")[0]
    isolated = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(isolated)

    send_calls: List[int] = []
    trash_calls: List[int] = []
    sent_calls: List[int] = []

    async def fake_send_ex(chat_id, text, **kw):
        send_calls.append(chat_id)
        if chat_id == 1001:
            return False, "restriction"
        # Should never be reached (candidate 2) -- if it is, the test below
        # will already have failed on the call-count assertion, but return
        # something sane rather than raising.
        return True, ""

    async def fake_gate_allowed_tick(mgr_key, *, new_dialog):
        return True, "limited_probe_passthrough"

    async def fake_window_counts(db_path, mgr_key, now_utc_dt):
        return 0, 0

    async def fake_candidates(db_path, *, today_local, limit):
        return [
            {"chat_id": 1001, "cycle_started_at_utc": "", "sent_text_keys": ""},
            {"chat_id": 1002, "cycle_started_at_utc": "", "sent_text_keys": ""},
        ]

    async def fake_lead_gate(mgr_key, chat_id, kind):
        return True, "ok"

    def fake_local_date_of(_raw):
        return None

    def fake_days_since(_cycle_local, _today_local):
        return 0

    async def fake_set_disabled(db_path, chat_id, flag, *, user_id, reason):
        pass

    def fake_v2_allowed(row):
        return True

    async def fake_ensure_plan(db_path, chat_id, row, today_local):
        return row

    def fake_due_slot(row, now_utc_dt, today_local):
        return "morning"

    def fake_choose_text(sent_keys, *, chat_id, date_key, slot):
        return "greeting", "hello"

    async def fake_mark_trash(db_path, chat_id, reason):
        trash_calls.append(chat_id)

    async def fake_mark_sent(db_path, chat_id, *, slot, today_local, template_key, text):
        sent_calls.append(chat_id)

    def fake_kyiv_now():
        return datetime.utcnow()

    def fake_followup_allowed_now(_now_local):
        return True

    def fake_norm_key(k):
        return str(k or "").strip().lower()

    ns: Dict[str, Any] = {
        "asyncio": asyncio, "random": __import__("random"), "datetime": datetime,
        "CONTROLLER_MODE": False, "MANAGER_RUNTIME_KEY": "mgrh_loop_test",
        "POST_MANUAL_FOLLOWUP_ENABLED": True,
        "_kyiv_now": fake_kyiv_now,
        "_tp_followup_allowed_now": fake_followup_allowed_now,
        "registry_normalize_manager_key": fake_norm_key,
        "_tp_hg_send_allowed": fake_gate_allowed_tick,
        "_post_followup_window_counts": fake_window_counts,
        "DB_PATH": "unused.db",
        "_fetch_post_followup_candidates": fake_candidates,
        "POST_MANUAL_FOLLOWUP_MAX_PER_HOUR": 20, "POST_MANUAL_FOLLOWUP_MAX_PER_DAY": 80,
        "_lead_automation_gate": fake_lead_gate,
        "_post_followup_local_date_of": fake_local_date_of,
        "_post_followup_days_since": fake_days_since,
        "POST_MANUAL_FOLLOWUP_MAX_DAYS": 3,
        "_set_post_followup_disabled": fake_set_disabled,
        "_post_followup_v2_allowed_for_row": fake_v2_allowed,
        "_post_followup_ensure_daily_plan": fake_ensure_plan,
        "_post_followup_due_slot": fake_due_slot,
        "choose_post_manual_followup_text": fake_choose_text,
        "_send_manager_private_ex": fake_send_ex,
        "_post_followup_mark_trash": fake_mark_trash,
        "_mark_post_followup_sent": fake_mark_sent,
        "POST_MANUAL_FOLLOWUP_BATCH_SLEEP_MIN_SEC": 0, "POST_MANUAL_FOLLOWUP_BATCH_SLEEP_MAX_SEC": 0,
        "_POST_FOLLOWUP_V2_SKIPPED_LOG_DONE": False,
        "_post_followup_v2_epoch_utc": lambda: datetime.utcnow(),
    }
    exec(compile(isolated, f"<{MAIN_PATH}:post_followup_loop>", "exec"), ns)
    fn = ns["_process_post_manual_followups_once"]

    sent = await fn()

    check("D. only the FIRST candidate is attempted (tick stops after one restriction)", send_calls == [1001], send_calls)
    check("D2. the tick returns 0 sent (nothing succeeded)", sent == 0, sent)
    check("E. the restriction-failed candidate is NOT marked trash", trash_calls == [], trash_calls)
    check("E2. nothing is marked sent", sent_calls == [], sent_calls)


async def test_de_profile_reminder_loop_stops_after_one_restriction() -> None:
    print("\n-- D/E: _process_profile_reminders_once stops after one restriction, never trashes --")
    node = find_defs(MAIN_TREE, "_process_profile_reminders_once")[0]
    isolated = ast.Module(body=[node], type_ignores=[])
    ast.fix_missing_locations(isolated)

    send_calls: List[int] = []
    trash_calls: List[int] = []
    state_set_calls: List[int] = []

    async def fake_send_ex(chat_id, text, **kw):
        send_calls.append(chat_id)
        if chat_id == 2001:
            return False, "restriction"
        return True, ""

    async def fake_gate_allowed_tick(mgr_key, *, new_dialog):
        return True, "limited_probe_passthrough"

    async def fake_reminders_allowed_now():
        return True

    async def fake_candidates(db_path, *, limit):
        return [
            {"chat_id": 2001},
            {"chat_id": 2002},
        ]

    async def fake_lead_gate(mgr_key, chat_id, kind):
        return True, "ok"

    async def fake_upsert_state(db_path, lead):
        return {"reminder_step": 0}

    def fake_due_text(lead, state, now_utc, today_local):
        return "reminder text"

    async def fake_mark_trash(db_path, chat_id, reason):
        trash_calls.append(chat_id)

    async def fake_set_state(db_path, chat_id, **fields):
        state_set_calls.append(chat_id)

    def fake_now_utc_iso():
        return "2026-08-12T00:00:00"

    def fake_kyiv_now():
        return datetime.utcnow()

    ns: Dict[str, Any] = {
        "asyncio": asyncio, "random": __import__("random"), "datetime": datetime,
        "CONTROLLER_MODE": False, "MANAGER_RUNTIME_KEY": "mgrh_loop_test2",
        "_profile_reminders_allowed_now": fake_reminders_allowed_now,
        "_tp_hg_send_allowed": fake_gate_allowed_tick,
        "_kyiv_now": fake_kyiv_now,
        "DB_PATH": "unused.db",
        "_fetch_reminder_candidates": fake_candidates,
        "_lead_automation_gate": fake_lead_gate,
        "_upsert_profile_reminder_state": fake_upsert_state,
        "_reminder_due_text": fake_due_text,
        "_send_manager_private_ex": fake_send_ex,
        "_mark_profile_reminder_trash": fake_mark_trash,
        "_set_profile_reminder_state": fake_set_state,
        "_now_utc_iso": fake_now_utc_iso,
        "PROFILE_REMINDER_BATCH_MIN_MINUTES": 0, "PROFILE_REMINDER_BATCH_MAX_MINUTES": 0,
    }
    exec(compile(isolated, f"<{MAIN_PATH}:profile_reminder_loop>", "exec"), ns)
    fn = ns["_process_profile_reminders_once"]

    sent = await fn()

    check("D3. only the FIRST candidate is attempted (profile reminders tick stops after one restriction)", send_calls == [2001], send_calls)
    check("D4. the tick returns 0 sent (nothing succeeded)", sent == 0, sent)
    check("E3. the restriction-failed reminder is NOT marked trash", trash_calls == [], trash_calls)
    check("E4. reminder state is not advanced for the failed candidate", state_set_calls == [], state_set_calls)


# ======================================================================
# K. PanelBot: active+running+proxy-ok + LIMITED -> runtime category stays
# 'ok' (ACTIVE), and a separate TG badge is shown. Real execution of the
# PURE _pb_manager_status_resolve (already covered by manager_status_
# resolver_selftest.py) PLUS the new P2 badge helpers, which nothing else
# tests yet.
# ======================================================================

def test_k_panel_badge_and_category() -> None:
    print("\n-- K: PanelBot runtime category stays ACTIVE + separate TG badge for LIMITED --")
    # Same dependency set as tools/manager_status_resolver_selftest.py's
    # PURE_NAMES -- _pb_manager_status_resolve calls _pb_proxy_effective_state
    # (2026-07-25 R1-R3 follow-up), which transitively needs the rest.
    names = {
        "_pb_manager_status_resolve", "_PB_STATUS_CATEGORIES",
        "_PB_STATUS_BANNED_MARKS", "_PB_STATUS_AUTH_MARKS",
        "_pb_proxy_guard_state", "_PB_PROXY_GUARD_FRESH_SEC", "_tpag_panel_v2_parse_dt",
        "_pb_proxy_ts_is_fresh", "_PB_PROXY_CLOCK_SKEW_TOLERANCE_SEC",
        "_pb_proxy_timestamp_malformed", "_pb_proxy_effective_state",
        "_pb_proxy_state_check_text", "_PB_PROXY_STATE_LABELS",
    }
    nodes = []
    seen = set()
    for n in PANEL_TREE.body:
        nm = getattr(n, "name", None)
        if nm in names:
            nodes.append(n)
            seen.add(nm)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names:
            nodes.append(n)
            seen.add(n.targets[0].id)
    missing = names - seen
    if missing:
        raise AssertionError(f"missing panel_bot.py names: {missing}")
    badge_node = find_defs(PANEL_TREE, "_pb_tg_health_badge")
    if not badge_node:
        raise AssertionError("_pb_tg_health_badge not found in panel_bot.py")
    nodes.append(badge_node[-1])

    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns: Dict[str, Any] = {"Dict": dict, "Any": object, "datetime": datetime, "timezone": timezone}
    exec(compile(module_src, f"<{PANEL_PATH}:status_badge>", "exec"), ns)
    resolve = ns["_pb_manager_status_resolve"]
    badge_fn = ns["_pb_tg_health_badge"]

    row = {
        "manager_key": "mgr01", "status": "active", "is_enabled": 1,
        "manual_stopped": 0, "auth_guard_state": "", "proxy_required": 0,
        "proxy_enabled": 0, "proxy_bypass_allowed": 0, "proxy_test_ok": 0,
    }
    tg_health_limited = {"health_status": "limited", "error_class": "PeerFloodError"}
    key, icon, label = resolve(row, tg_health=tg_health_limited, proc_running=True, session_exists=True)
    check("K. active+running+proxy-bypass+LIMITED resolves category 'ok' (ACTIVE), not 'warning'", key == "ok", (key, icon, label))
    check("K2. the icon is the green 'ok' icon", icon == ns["_PB_STATUS_CATEGORIES"]["ok"][0], icon)

    badge_limited = badge_fn(tg_health_limited)
    check("K3. the TG badge for LIMITED is the warning badge", badge_limited == "⚠️TG", badge_limited)

    tg_health_blocked = {"health_status": "blocked", "error_class": "SomeOtherBlockedReason"}
    badge_blocked = badge_fn(tg_health_blocked)
    check("K4. the TG badge for BLOCKED (non-ban/non-auth-marked) is the blocked badge", badge_blocked == "⛔TG", badge_blocked)

    tg_health_ok = {"health_status": "ok"}
    badge_ok = badge_fn(tg_health_ok)
    check("K5. the TG badge for OK is empty (no badge)", badge_ok == "", repr(badge_ok))
    badge_empty = badge_fn({})
    check("K6. the TG badge for no data is empty (no badge)", badge_empty == "", repr(badge_empty))

    # L (panel half): a plain OK manager, unaffected by any of this.
    key_ok, icon_ok, _label_ok = resolve(row, tg_health={"health_status": "ok"}, proc_running=True, session_exists=True)
    check("L3. a normal OK manager still resolves 'ok' (regression)", key_ok == "ok", (key_ok, icon_ok))


# ======================================================================
# F. Fast LIMITED->OK->LIMITED flap: notification dedup preserved on BOTH
# sides (problem alert AND recovery). CORRECTIVE 20260812b (finding B-1,
# independent review): this file's own docstring already claimed scenario F
# ("no alert/recovery spam pair") but had no test proving it -- the
# independent review measured the real gap directly against storage.py:
# 5 flap cycles produced exactly 1 alert but 5 separate "Telegram Health
# Recovery" notifications, because the recovery branch in
# _tp_hg_notify_if_needed had no dedup window of its own (any_was_notified
# alone stays True on every resolve of a preserve_notify_within_sec-kept
# incident). The fix gives the recovery message its own claim_notify-backed
# 6h window, keyed to a companion "recovery:<signature>" identity built from
# the SAME family/class/source/text the problem alert used.
#
# Real execution of the ACTIVE _tp_hg_notify_if_needed body (main.py) against
# the REAL storage.py functions (temp sqlite) -- _tp_hg_restriction_family/
# _tp_hg_incident_signature/_tp_hg_stable_error_text are extracted for real
# too, since the dedup identity's correctness depends on them; only the
# cosmetic text-building (_tp_hg_build_alert_text/_tp_hg_build_recovery_
# alert_text) and delivery (_tp_hg_insert_panel_notification/_tp_hg_send_
# work_status_notification/_tp_hg_bump_notify_meta) are stubbed to record
# calls -- their CONTENT is irrelevant to the dedup COUNT this proves, and
# the real alert-text builder pulls in a large unrelated dependency closure
# (_TP_HG_VERIFY_* marks, _hnv2_tg_health_family) already covered elsewhere.
# ======================================================================

def _build_notify_ns(db_path: str, *, mutate_remove_recovery_guard: bool = False) -> Tuple[Dict[str, Any], Dict[str, list]]:
    notify_node = find_defs(MAIN_TREE, "_tp_hg_notify_if_needed")[-1]
    fam_node = find_defs(MAIN_TREE, "_tp_hg_restriction_family")[-1]
    sig_node = find_defs(MAIN_TREE, "_tp_hg_incident_signature")[-1]
    stable_node = find_defs(MAIN_TREE, "_tp_hg_stable_error_text")[-1]
    notify_src = ast.unparse(notify_node)
    if mutate_remove_recovery_guard:
        # MUTATION PROOF: collapse the new per-signature recovery throttle
        # back to "always send if any_was_notified" -- the exact pre-fix
        # (B-1) behavior -- entirely in-memory, never touching main.py.
        marker = "if recovery_claim is not None:"
        assert marker in notify_src, "mutation anchor not found -- guard code changed shape, update this test"
        notify_src = notify_src.replace(marker, "if True:")
    other_src = "\n\n".join(ast.unparse(n) for n in (fam_node, sig_node, stable_node))
    module_src = other_src + "\n\n" + notify_src

    calls: Dict[str, list] = {"alerts": [], "recoveries": []}

    async def fake_insert_panel(title, body):
        if "Alert" in title:
            calls["alerts"].append(title)
        elif "Recovery" in title:
            calls["recoveries"].append(title)

    async def fake_work_status(body):
        pass

    async def fake_bump_meta(key, sig):
        pass

    def fake_alert_text(mk, row):
        return "alert-body"

    def fake_recovery_text(mk, row):
        return "recovery-body"

    def norm(v):
        return str(v or "").strip().lower()

    ns: Dict[str, Any] = {
        "re": __import__("re"),
        "TP_HG_STATUS_LIMITED": "limited", "TP_HG_STATUS_BLOCKED": "blocked",
        "TP_HG_STATUS_WARNING": "warning",
        "_TP_HG_FAMILY_FLOODWAIT": "floodwait", "_TP_HG_FAMILY_PEERFLOOD": "peerflood",
        "_TP_HG_FAMILY_BLOCKED": "blocked", "_TP_HG_FAMILY_WARNING": "warning_transient",
        "_TP_HG_FAMILY_UNKNOWN": "unknown",
        "TP_HG_NOTIFY_REPEAT_SEC": 6 * 60 * 60,
        "TPILOT_DB_PATH": db_path,
        "_tp_hg_norm_key": norm,
        "_tp_hg_build_alert_text": fake_alert_text,
        "_tp_hg_build_recovery_alert_text": fake_recovery_text,
        "_tp_hg_insert_panel_notification": fake_insert_panel,
        "_tp_hg_send_work_status_notification": fake_work_status,
        "_tp_hg_bump_notify_meta": fake_bump_meta,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:notify_flap>", "exec"), ns, ns)
    return ns, calls


async def _run_flap_cycles(ns: Dict[str, Any], key: str, cycles: int) -> None:
    notify = ns["_tp_hg_notify_if_needed"]
    limited_row = {
        "health_status": "limited", "error_class": "PeerFloodError",
        "error_source": "client_auto_send", "error_text": "PeerFloodError: too many requests",
    }
    ok_row = {"health_status": "ok"}
    old: Dict[str, Any] = {}
    for _ in range(cycles):
        await notify(key, limited_row, old)   # problem observed (alert branch)
        old = dict(limited_row)
        await notify(key, ok_row, old)        # genuine successful-send recovery
        old = dict(ok_row)


async def test_f_flap_notification_dedup() -> None:
    print("\n-- F: fast LIMITED->OK->LIMITED flap -- alert AND recovery both deduped (finding B-1) --")
    tmp = tempfile.mkdtemp(prefix="peerflood_flap_")
    try:
        db_path = str(Path(tmp) / "flap.db")
        ns, calls = _build_notify_ns(db_path)
        await _run_flap_cycles(ns, "flap_test_mgr", 5)
        check("F1. 5 rapid LIMITED->OK->LIMITED flap cycles produce exactly 1 problem alert", len(calls["alerts"]) == 1, calls["alerts"])
        check("F2. the SAME 5 flap cycles produce exactly 1 recovery notification, not 5 (finding B-1)", len(calls["recoveries"]) == 1, calls["recoveries"])

        # F3: a genuinely different restriction (BLOCKED) must not be
        # suppressed by the unrelated PeerFlood recovery-signature's history
        # -- independent identity, independent throttle.
        db_path2 = str(Path(tmp) / "flap_independent.db")
        ns2, calls2 = _build_notify_ns(db_path2)
        await _run_flap_cycles(ns2, "flap_independent_mgr", 3)
        notify2 = ns2["_tp_hg_notify_if_needed"]
        blocked_row = {"health_status": "blocked", "error_class": "AuthKeyUnregisteredError", "error_source": "client_auto_send", "error_text": "AuthKeyUnregisteredError"}
        ok_row = {"health_status": "ok"}
        old_ok = {"health_status": "ok"}
        await notify2("flap_independent_mgr", blocked_row, old_ok)
        await notify2("flap_independent_mgr", ok_row, blocked_row)
        check("F3. an independent BLOCKED incident still alerts (not suppressed by PeerFlood's recovery history)", len(calls2["alerts"]) == 2, calls2["alerts"])
        check("F4. the independent BLOCKED incident still recovers on its own (not suppressed either)", len(calls2["recoveries"]) == 2, calls2["recoveries"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def test_f_mutation_proof() -> None:
    print("\n-- F: mutation proof -- removing the recovery dedup guard MUST reproduce the B-1 spam --")
    tmp = tempfile.mkdtemp(prefix="peerflood_flap_mut_")
    try:
        db_path = str(Path(tmp) / "flap_mut.db")
        ns, calls = _build_notify_ns(db_path, mutate_remove_recovery_guard=True)
        await _run_flap_cycles(ns, "flap_mut_mgr", 5)
        check("F-mutation1. removing the recovery dedup guard DOES reproduce B-1 (recoveries > 1 -- the guard is load-bearing)", len(calls["recoveries"]) > 1, calls["recoveries"])
        check("F-mutation2. reproduces the EXACT 5-recoveries-for-5-flaps spam the independent review measured", len(calls["recoveries"]) == 5, calls["recoveries"])
        check("F-mutation3. the problem-alert side is unaffected by this mutation (still deduped to 1)", len(calls["alerts"]) == 1, calls["alerts"])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_m_def_counts()
    asyncio.run(test_abc_gate_and_recovery_primitives())
    asyncio.run(test_de_post_followup_loop_stops_after_one_restriction())
    asyncio.run(test_de_profile_reminder_loop_stops_after_one_restriction())
    asyncio.run(test_f_flap_notification_dedup())
    asyncio.run(test_f_mutation_proof())
    test_k_panel_badge_and_category()

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
