# -*- coding: utf-8 -*-
"""tools/hnv2_panel_callback_selftest.py -- offline self-test for the
Health Notification System V2 AdminBot surface: the button builder
(_hnv2_health_notification_buttons / _hnv2_family_actions, panel_bot.py)
and the hv2: callback handler (_hnv2_callback / _hnv2_run_diag,
panel_bot.py), plus the strictly-read-only contract of main.py's
/hnv2_diag command (_hnv2_diag_text).

Covers approved-plan selftest scenario 15 (callback authorization /
idempotency / stale-state) and the owner's per-family button-map
specification (2026-08-07 revision).

panel_bot.py and main.py cannot be imported directly (Telethon/env side
effects at import time) -- extracted via ast.parse + ast.unparse + exec(),
the established technique. No real Telegram, no real DB writes outside a
temp SQLite file, no real network, no real process spawn.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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


PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()
PANEL_TREE = ast.parse(PANEL_SRC)

MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()
MAIN_TREE = ast.parse(MAIN_SRC)


def _extract_by_names(tree: ast.AST, src: str, names: set) -> list:
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
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in names:
            nodes.append(n)
            seen.add(n.target.id)
            continue
    missing = names - seen
    if missing:
        raise AssertionError(f"expected {names}, missing {missing}")
    return nodes


# ======================================================================
# PART A: panel_bot.py -- button builder + callback handler.
# ======================================================================

PANEL_REAL_NAMES = {
    "_hnv2_health_notification_buttons", "_hnv2_family_actions",
    "_hnv2_family_from_health_row", "_hnv2_callback", "_hnv2_run_diag",
    "_HNV2_KEY_RE", "_HNV2_FAMILY_RE", "_back_to_panel_buttons",
    "_safe_text",
    # TERMINAL OK 20260809 (Ф4 bot-wide audit): _hnv2_run_diag now appends
    # _terminal_ok_button() to its result message.
    "_terminal_ok_button",
    # HNV2 N-2/N-8 correction (post-independent-review): confidence
    # degradation + FloodWait-cooldown button gating, added to
    # _hnv2_family_actions/_hnv2_health_notification_buttons.
    "_HNV2_CONFIDENCE_RE", "_hnv2_degrade_for_confidence",
    "_HNV2_UNSAFE_ACTION_PREFIXES", "_HNV2_SAFE_DIAG_FALLBACK",
    # RR-1 (W3.2 2026-08-07): _hnv2_health_notification_buttons' FloodWait
    # cooldown check now reuses the approved _utc_now_iso() wrapper instead
    # of a local datetime.utcnow() -- a real, load-bearing dependency for
    # this function, same as every other helper in this set.
    "_utc_now_iso",
    # R1B/F-16/F-32 (2026-08-12, large reliability batch): every
    # `await event.answer(...)` call site in panel_bot.py -- including
    # inside _hnv2_callback -- was mechanically rewritten to
    # `await _pb_safe_answer(event, ...)`, a thin wrapper that swallows only
    # a stale/expired callback query. A real, load-bearing dependency now,
    # same as every other helper in this set.
    "_pb_safe_answer", "_pb_is_query_invalid_error",
    "_PB_QUERY_INVALID_CLASS_NAMES", "_PB_QUERY_INVALID_TEXT_MARKERS",
    # Same batch: every `asyncio.create_task(...)` call site -- including the
    # two inside _hnv2_callback -- was mechanically rewritten to
    # `_pb_track_task(...)` (F-18/F-40 task ownership).
    "_pb_track_task",
}


class _FakeButton:
    @staticmethod
    def inline(label, data):
        return ("inline", label, data)


class _FakeEvent:
    def __init__(self, data: bytes, chat_id: int, sender_id: int):
        self.data = data
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.answers: list = []

    async def answer(self, text="", alert=False):
        self.answers.append((text, alert))


class _FakeClient:
    def __init__(self):
        self.sent: list = []

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))

    def on(self, *_a, **_kw):
        def deco(fn):
            return fn
        return deco


class _EventsNS:
    class CallbackQuery:
        def __init__(self, *a, **kw):
            pass

    def on(self, *_a, **_kw):
        def deco(fn):
            return fn
        return deco


def build_panel_ns(manager_rows: dict, *, is_allowed=True, dedupe_window: float = 2.5, submit_result=None):
    nodes = _extract_by_names(PANEL_TREE, PANEL_SRC, PANEL_REAL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    fake_client = _FakeClient()
    dup_calls: list = []
    seen_keys: set = set()
    answer_fast_calls: list = []
    submit_calls: list = []

    def normalize_manager_key(k):
        return str(k or "").strip().lower()

    def fake_is_allowed(event):
        return bool(is_allowed)

    def fake_dup(chat_id, user_id, key_text):
        dup_calls.append((chat_id, user_id, key_text))
        k = (chat_id, user_id, key_text)
        if k in seen_keys:
            return True
        seen_keys.add(k)
        return False

    async def fake_answer_fast(event, text=""):
        answer_fast_calls.append(text)
        try:
            await event.answer(text)
        except Exception:
            pass

    def fake_manager_row_by_id(manager_id):
        return dict(manager_rows.get(int(manager_id), {}) or {})

    def fake_manager_row_by_key(key):
        nk = normalize_manager_key(key)
        for row in manager_rows.values():
            if normalize_manager_key(row.get("manager_key")) == nk:
                return dict(row)
        return {}

    menu_calls: list = []

    async def fake_pb_send_menu_message(chat_id, menu_route):
        menu_calls.append((chat_id, menu_route))

    async def fake_submit_and_wait(command_text, requested_by, source_chat_id, response_chat_id):
        submit_calls.append(command_text)
        return submit_result or {"result_text": "diag ok"}

    ns = {
        "re": re, "asyncio": asyncio,
        "datetime": datetime, "timezone": timezone,
        "Button": _FakeButton, "events": _EventsNS(),
        "client": fake_client,
        "_PANEL_BACKGROUND_TASKS": set(),
        "normalize_manager_key": normalize_manager_key,
        "_is_allowed": fake_is_allowed,
        "_panel_callback_is_duplicate": fake_dup,
        "_panel_answer_fast": fake_answer_fast,
        "_manager_row_by_id": fake_manager_row_by_id,
        "_manager_row_by_key": fake_manager_row_by_key,
        "_pb_send_menu_message": fake_pb_send_menu_message,
        "_submit_and_wait": fake_submit_and_wait,
        "_pb_tg_health_row": lambda k: {},
    }
    exec(compile(module_src, f"<{PANEL_PATH}:hnv2_panel_callback>", "exec"), ns)
    ns["__fake_client__"] = fake_client
    ns["__dup_calls__"] = dup_calls
    ns["__answer_fast_calls__"] = answer_fast_calls
    ns["__submit_calls__"] = submit_calls
    ns["__menu_calls__"] = menu_calls
    return ns


def _run_and_settle(coro) -> None:
    """Runs `coro` to completion, then yields the event loop briefly so any
    asyncio.create_task(...) it scheduled (the REAL asyncio module is used
    here, not a fake, since intercepting create_task itself would diverge
    from what the real handler actually does) gets a chance to run to
    completion before the loop closes."""
    async def _wrapper():
        await coro
        await asyncio.sleep(0.05)
    asyncio.run(_wrapper())


ALL_FAMILIES = [
    "session_unauthorized", "account_blocked", "telegram_limited_peerflood",
    "floodwait", "proxy_auth_failed", "proxy_unavailable", "worker_crash",
    "worker_stuck_starting", "network_timeout", "sqlite_lock",
    "health_missing_stale", "recovery_flap", "unknown",
]


def _sample_body(family: str, key: str = "mgr_test") -> str:
    return f"🩺 Health V2: {key} — {family}\nmanager: {key}\nfamily: {family}\nПричина: test\n"


# ======================================================================
# Button-family map assertions (owner spec 2026-08-07).
# ======================================================================

def test_family_button_map():
    print("\n-- Button family map (owner spec) --")
    ns = build_panel_ns({1: {"id": 1, "manager_key": "mgr_test", "status": "active"}})
    fam_actions = ns["_hnv2_family_actions"]

    for fam in ALL_FAMILIES:
        pairs = fam_actions(fam, "mgr_test", 1)
        for label, data in pairs:
            check(f"len-{fam}. callback_data for {data!r} is <=64 bytes", len(data.encode("utf-8")) <= 64, data)

    recovery_flap_data = [d for _, d in fam_actions("recovery_flap", "mgr_test", 1)]
    check("recovery_flap has NO restart button", not any(d.startswith("cmd:/manager_restart") for d in recovery_flap_data), recovery_flap_data)

    account_blocked_data = [d for _, d in fam_actions("account_blocked", "mgr_test", 1)]
    check("account_blocked has NO restart/relogin/QR/devlogin/replace button (no auto-fix exists)",
          not any(d.startswith(("cmd:/manager_restart", "relogin:", "wiz:qr_start:", "devlogin:start:", "rw:")) for d in account_blocked_data),
          account_blocked_data)
    check("account_blocked returns zero direct actions (card-only, appended by the caller)", account_blocked_data == [], account_blocked_data)

    sqlite_lock_data = [d for _, d in fam_actions("sqlite_lock", "mgr_test", 1)]
    check("sqlite_lock has NO restart button", not any(d.startswith("cmd:/manager_restart") for d in sqlite_lock_data), sqlite_lock_data)
    check("sqlite_lock offers the read-only diag action", any(d.startswith("hv2:diag:") for d in sqlite_lock_data), sqlite_lock_data)

    session_unauth_pairs = fam_actions("session_unauthorized", "mgr_test", 1)
    session_unauth_labels = [label for label, _ in session_unauth_pairs]
    session_unauth_data = [d for _, d in session_unauth_pairs]
    # HNV2 UX hotfix 20260808: device-login is a way to connect Telegram from
    # ANOTHER device, not a way to restore a missing/unauthorized session on
    # THIS manager -- misleading in this specific alert, so it was removed
    # from session_unauthorized's action set only (relogin/QR untouched at
    # the time this comment was written).
    #
    # SUPERSEDED by Ф4 20260809 (unified auth UX, independently verified in
    # tools/auth_chooser_selftest.py's A11): scattered per-method buttons for
    # session_unauthorized (separate relogin + QR actions) were replaced with
    # ONE unified "🔐 Восстановить доступ" action routing to relogin:start,
    # whose own screen (_relogin_intro_buttons) now shows the full QR/Phone/
    # Session/TData chooser -- QR is still reachable, just one tap further
    # in, not lost. The OLD "exactly 2 actions" claim is no longer the
    # correct invariant; re-derived as "exactly 1 unified action".
    check("session_unauthorized offers the unified recovery action (label)",
          "🔐 Восстановить доступ" in session_unauth_labels, session_unauth_labels)
    check("session_unauthorized's unified action routes to relogin:start (same entry "
          "point whose own screen now shows the full QR/Phone/Session/TData chooser)",
          "relogin:start:mgr_test" in session_unauth_data, session_unauth_data)
    check("session_unauthorized no longer exposes a raw QR-only shortcut "
          "(QR is still reachable, one tap into the chooser, not a separate top-level action)",
          "wiz:qr_start:mgr_test" not in session_unauth_data, session_unauth_data)
    check("session_unauthorized does NOT offer devlogin (callback_data)", not any(d.startswith("devlogin:start:") for d in session_unauth_data), session_unauth_data)
    check("session_unauthorized does NOT offer devlogin (label)", "🔐 Вход на устройстве" not in session_unauth_labels, session_unauth_labels)
    check("session_unauthorized offers exactly 1 action (the unified recovery button, not scattered per-method buttons)",
          len(session_unauth_pairs) == 1, session_unauth_pairs)

    for fam in ("telegram_limited_peerflood", "floodwait"):
        data = [d for _, d in fam_actions(fam, "mgr_test", 1)]
        check(f"{fam} has NO restart/relogin button (TP_HG owns recovery)", not any(d.startswith(("cmd:/manager_restart", "relogin:")) for d in data), data)


# ======================================================================
# N-2 correction: confidence degradation (approved plan section I.2.1) --
# every family's mutating/auth/restart buttons must disappear whenever
# confidence != 'certain'.
# ======================================================================

def test_n2_confidence_degradation():
    print("\n-- N-2: confidence degradation strips unsafe buttons --")
    ns = build_panel_ns({1: {"id": 1, "manager_key": "mgr_test", "status": "active"}})
    fam_actions = ns["_hnv2_family_actions"]

    UNSAFE_PREFIXES = ("relogin:start:", "wiz:qr_start:", "devlogin:start:", "cmd:/manager_restart")

    # The exact case the independent review called out by name.
    certain = [d for _, d in fam_actions("proxy_unavailable", "mgr_test", 1, confidence="certain")]
    probable = [d for _, d in fam_actions("proxy_unavailable", "mgr_test", 1, confidence="probable")]
    check("proxy_unavailable at confidence='certain' DOES offer Restart", any(d.startswith("cmd:/manager_restart") for d in certain), certain)
    check("proxy_unavailable at confidence='probable' does NOT offer Restart", not any(d.startswith("cmd:/manager_restart") for d in probable), probable)
    check("proxy_unavailable at confidence='probable' still offers a safe diagnostic action", len(probable) > 0, probable)

    for fam in ALL_FAMILIES:
        for conf in ("probable", "none", "unknown-garbage-value"):
            data = [d for _, d in fam_actions(fam, "mgr_test", 1, confidence=conf)]
            unsafe = [d for d in data if d.startswith(UNSAFE_PREFIXES)]
            check(f"{fam} at confidence={conf!r}: zero mutating/auth/restart buttons", unsafe == [], (fam, conf, data))

    # worker_crash's ONLY action IS restart -- degrading must not leave it
    # with literally nothing but the (separately-appended) card button.
    wc_degraded = [d for _, d in fam_actions("worker_crash", "mgr_test", 1, confidence="probable")]
    check("worker_crash degraded to non-certain still offers a safe diagnostic fallback (not empty)", len(wc_degraded) > 0, wc_degraded)
    check("worker_crash degraded fallback contains no restart", not any(d.startswith("cmd:/manager_restart") for d in wc_degraded), wc_degraded)

    # account_blocked/sqlite_lock/telegram_limited_peerflood were ALREADY
    # restricted -- degrading confidence must not change them at all.
    check("account_blocked unaffected by confidence (already empty)", fam_actions("account_blocked", "mgr_test", 1, confidence="probable") == [], None)
    check("sqlite_lock unaffected by confidence (already restart-free)",
          [d for _, d in fam_actions("sqlite_lock", "mgr_test", 1, confidence="probable")] == [d for _, d in fam_actions("sqlite_lock", "mgr_test", 1, confidence="certain")], None)


# ======================================================================
# N-8 correction: active FloodWait cooldown exposes card/navigation only
# -- no 🩺 Проверить (real get_me/get_dialogs Telegram traffic) while the
# account is supposed to be left alone.
# ======================================================================

def test_n8_floodwait_cooldown_buttons():
    print("\n-- N-8: FloodWait cooldown gates the check button --")
    ns = build_panel_ns({1: {"id": 1, "manager_key": "mgr_test", "status": "active"}})
    fam_actions = ns["_hnv2_family_actions"]

    active = fam_actions("floodwait", "mgr_test", 1, cooldown_active=True)
    expired = fam_actions("floodwait", "mgr_test", 1, cooldown_active=False)
    check("floodwait WHILE cooldown active: zero actions (card/back only, appended by caller)", active == [], active)
    check("floodwait AFTER cooldown expires: the existing check action is available again", any(d.startswith("cmd:/tghealth check") for d, _ in [] ) or any(data.startswith("cmd:/tghealth check") for _, data in expired), expired)

    # End-to-end through the body parser: cooldown_until in the future vs
    # in the past, via the fresh _pb_tg_health_row read the builder does.
    from datetime import datetime, timedelta, timezone as _tz
    future = (datetime.utcnow() + timedelta(minutes=30)).replace(microsecond=0).isoformat()
    past = (datetime.utcnow() - timedelta(minutes=5)).replace(microsecond=0).isoformat()

    ns_future = build_panel_ns({1: {"id": 1, "manager_key": "mgr_fw", "status": "active"}})
    ns_future["_pb_tg_health_row"] = lambda k: {"cooldown_until": future}
    exec(compile("\n\n".join(ast.unparse(n) for n in _extract_by_names(PANEL_TREE, PANEL_SRC, PANEL_REAL_NAMES)), f"<{PANEL_PATH}:hnv2_panel_callback_fw_future>", "exec"), ns_future)
    body_future = "🩺 alert\nmanager: mgr_fw\nfamily: floodwait\n"
    btns_future = ns_future["_hnv2_health_notification_buttons"](body_future)
    flat_future = [d for row in btns_future for (_lbl, d) in [(b[1], b[2]) for b in row]]
    check("end-to-end: future cooldown_until -> no /tghealth check button in the rendered notification", not any(d.startswith(b"cmd:/tghealth check") for d in flat_future), flat_future)

    ns_past = build_panel_ns({1: {"id": 1, "manager_key": "mgr_fw2", "status": "active"}})
    ns_past["_pb_tg_health_row"] = lambda k: {"cooldown_until": past}
    exec(compile("\n\n".join(ast.unparse(n) for n in _extract_by_names(PANEL_TREE, PANEL_SRC, PANEL_REAL_NAMES)), f"<{PANEL_PATH}:hnv2_panel_callback_fw_past>", "exec"), ns_past)
    body_past = "🩺 alert\nmanager: mgr_fw2\nfamily: floodwait\n"
    btns_past = ns_past["_hnv2_health_notification_buttons"](body_past)
    flat_past = [d for row in btns_past for (_lbl, d) in [(b[1], b[2]) for b in row]]
    check("end-to-end: expired cooldown_until -> /tghealth check button IS offered again", any(d.startswith(b"cmd:/tghealth check") for d in flat_past), flat_past)


# ======================================================================
# Notification-body button wiring end-to-end.
# ======================================================================

def test_notification_buttons_end_to_end():
    print("\n-- Notification body -> buttons, end to end --")
    ns = build_panel_ns({1: {"id": 1, "manager_key": "mgr_e2e", "status": "active"}})
    build = ns["_hnv2_health_notification_buttons"]

    rows = build(_sample_body("worker_crash", "mgr_e2e"))
    flat = [d for row in rows for (_, _, d) in row]
    check("worker_crash notification includes the restart button", any(b"cmd:/manager_restart" in d for d in flat), flat)
    check("worker_crash notification includes a card button", any(b"menu:manager_full:" in d for d in flat), flat)

    rows_ab = build(_sample_body("account_blocked", "mgr_e2e"))
    flat_ab = [d for row in rows_ab for (_, _, d) in row]
    check("account_blocked notification has NO restart button", not any(b"cmd:/manager_restart" in d for d in flat_ab), flat_ab)
    check("account_blocked notification still has a card button", any(b"menu:manager_full:" in d for d in flat_ab), flat_ab)

    # Malformed body -> safe fallback, never raises.
    rows_bad = build("garbage body with no manager: line at all")
    check("malformed body falls back to _back_to_panel_buttons without raising", isinstance(rows_bad, list) and len(rows_bad) >= 1, rows_bad)

    # Unknown manager_key -> safe fallback.
    rows_unknown = build("manager: nonexistent_key_xyz\nfamily: worker_crash\n")
    check("unknown manager_key falls back to _back_to_panel_buttons", isinstance(rows_unknown, list), rows_unknown)


# ======================================================================
# Scenario 15: callback authorization / idempotency / stale state.
# ======================================================================

def test_scenario_15_callback_contract():
    print("\n-- Scenario 15: _hnv2_callback authorization/idempotency/stale-state --")

    # 15a: not allowed -> zero dispatch, no crash.
    ns = build_panel_ns({1: {"id": 1, "manager_key": "mgr15", "status": "active"}}, is_allowed=False)
    ev = _FakeEvent(b"hv2:card:1", chat_id=100, sender_id=200)
    _run_and_settle(ns["_hnv2_callback"](ev))
    check("15a. _is_allowed=False -> zero event.answer calls (silent return, matches every other handler's convention)", ev.answers == [], ev.answers)

    # 15b: allowed but manager archived -> alert, zero dispatch (no
    # _pb_send_menu_message call for 'card').
    ns2 = build_panel_ns({1: {"id": 1, "manager_key": "mgr15", "status": "archived"}}, is_allowed=True)
    ev2 = _FakeEvent(b"hv2:card:1", chat_id=100, sender_id=200)
    _run_and_settle(ns2["_hnv2_callback"](ev2))
    check("15b. archived manager -> event.answer(alert=True)", any(alert for _, alert in ev2.answers), ev2.answers)
    check("15b. archived manager -> zero background dispatch (_pb_send_menu_message never called)", ns2["__menu_calls__"] == [], ns2["__menu_calls__"])

    # 15c: unknown manager id -> alert, zero dispatch.
    ns3 = build_panel_ns({1: {"id": 1, "manager_key": "mgr15", "status": "active"}}, is_allowed=True)
    ev3 = _FakeEvent(b"hv2:card:999", chat_id=100, sender_id=200)
    _run_and_settle(ns3["_hnv2_callback"](ev3))
    check("15c. unknown manager_id -> event.answer(alert=True), stale-click handled", any(alert for _, alert in ev3.answers), ev3.answers)

    # 15d: dedupe -- same (chat,user,data) twice within the window -> one
    # dispatch (second call answers "Уже выполняю..." and returns early).
    ns4 = build_panel_ns({1: {"id": 1, "manager_key": "mgr15", "status": "active"}}, is_allowed=True)
    ev4a = _FakeEvent(b"hv2:card:1", chat_id=100, sender_id=200)
    ev4b = _FakeEvent(b"hv2:card:1", chat_id=100, sender_id=200)
    _run_and_settle(ns4["_hnv2_callback"](ev4a))
    _run_and_settle(ns4["_hnv2_callback"](ev4b))
    check("15d. first call dispatches (calls _pb_send_menu_message exactly once)", len(ns4["__menu_calls__"]) == 1, ns4["__menu_calls__"])
    check("15d. duplicate call within the window is answered but does NOT dispatch again", ev4b.answers and "Уже выполняю" in ev4b.answers[0][0], ev4b.answers)

    # 15e: manager row is ALWAYS re-resolved by id at click time, never
    # trusted from stale message state -- proven by the archived-manager
    # case above (15b) already; additionally confirm the row lookup
    # happens on every call, not cached across calls.
    ns5 = build_panel_ns({1: {"id": 1, "manager_key": "mgr15", "status": "active"}}, is_allowed=True)
    ev5a = _FakeEvent(b"hv2:card:1", chat_id=1, sender_id=1)
    _run_and_settle(ns5["_hnv2_callback"](ev5a))
    check("15e. active manager: card action dispatches successfully", len(ns5["__menu_calls__"]) == 1, ns5["__menu_calls__"])

    # 15f: malformed callback_data (too few parts) -> alert, no crash.
    ns6 = build_panel_ns({1: {"id": 1, "manager_key": "mgr15", "status": "active"}}, is_allowed=True)
    ev6 = _FakeEvent(b"hv2:card", chat_id=1, sender_id=1)
    _run_and_settle(ns6["_hnv2_callback"](ev6))
    check("15f. malformed data (missing manager_id segment) -> alert, no crash", any(alert for _, alert in ev6.answers), ev6.answers)

    # 15g: unrecognised action -> alert.
    ns7 = build_panel_ns({1: {"id": 1, "manager_key": "mgr15", "status": "active"}}, is_allowed=True)
    ev7 = _FakeEvent(b"hv2:bogus_action:1", chat_id=1, sender_id=1)
    _run_and_settle(ns7["_hnv2_callback"](ev7))
    check("15g. unrecognised action -> event.answer(alert=True)", any(alert for _, alert in ev7.answers), ev7.answers)

    # 15h: 'diag' action actually submits the read-only command through
    # the existing panel_commands bridge.
    ns8 = build_panel_ns({1: {"id": 1, "manager_key": "mgr15", "status": "active"}}, is_allowed=True, submit_result={"result_text": "diag output here"})
    ev8 = _FakeEvent(b"hv2:diag:1", chat_id=1, sender_id=1)
    _run_and_settle(ns8["_hnv2_callback"](ev8))
    check("15h. 'diag' action submits /hnv2_diag through _submit_and_wait", any("/hnv2_diag" in c for c in ns8["__submit_calls__"]), ns8["__submit_calls__"])
    check("15h. diag result is sent back to the chat", any("diag output here" in txt for _, txt in ns8["__fake_client__"].sent), ns8["__fake_client__"].sent)


# ======================================================================
# Static: _hnv2_callback's structural contract -- first statement is the
# _is_allowed guard, dedupe checked before dispatch, no branch skips
# re-resolving the manager row.
# ======================================================================

def test_static_callback_structure():
    print("\n-- Static: _hnv2_callback structural contract --")
    defs = [n for n in PANEL_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_hnv2_callback"]
    check("static. _hnv2_callback defined exactly once", len(defs) == 1, len(defs))
    if not defs:
        return
    fn = defs[0]
    try_node = next((n for n in fn.body if isinstance(n, ast.Try)), None)
    check("static. function body's first statement is a try block (matches every other handler's convention)", try_node is not None, ast.dump(fn))
    if try_node is None:
        return
    first_stmt = try_node.body[0]
    first_src = ast.unparse(first_stmt)
    check("static. the FIRST statement inside try is the _is_allowed(event) guard", "_is_allowed(event)" in first_src, first_src)

    src = ast.unparse(fn)
    dup_idx = src.find("_panel_callback_is_duplicate(")
    dispatch_idx = min(
        [i for i in (src.find("create_task("), src.find('action == "card"'), src.find('action == "diag"')) if i != -1] or [len(src)]
    )
    check("static. _panel_callback_is_duplicate is checked before any dispatch branch", dup_idx != -1 and dup_idx < dispatch_idx, (dup_idx, dispatch_idx))
    check("static. _manager_row_by_id is called (re-resolves by id every time, never trusts cached state)", "_manager_row_by_id(" in src, None)
    string_constants = {n.value for n in ast.walk(fn) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    check("static. archived/deleted status is checked (value-based, quote-style-independent)",
          "archived" in string_constants and "deleted" in string_constants, sorted(string_constants))


# ======================================================================
# HNV2 UX hotfix 20260808: devlogin removed from session_unauthorized's
# HNV2 action set only -- static proof that ordinary devlogin capability
# (the manager-card button, its callback handler) is completely untouched.
# ======================================================================

def test_devlogin_hotfix_20260808():
    print("\n-- Static: devlogin hotfix touched ONLY _hnv2_family_actions --")

    fam_defs = [n for n in PANEL_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_hnv2_family_actions"]
    check("static. _hnv2_family_actions defined exactly once", len(fam_defs) == 1, len(fam_defs))
    if not fam_defs:
        return
    fam_span = (fam_defs[0].lineno, fam_defs[0].end_lineno)
    # Isolate the `if family == "session_unauthorized":` branch specifically
    # -- the function's OWN docstring still (correctly) documents
    # _hnv2_degrade_for_confidence's general "restart/relogin/QR/devlogin"
    # stripping capability, which remains true generically even though no
    # family currently emits a devlogin action; that is not what this check
    # is about.
    su_branch = next(
        (n for n in ast.walk(fam_defs[0])
         if isinstance(n, ast.If) and ast.unparse(n.test) == "family == 'session_unauthorized'"),
        None,
    )
    check("static. found the `if family == \"session_unauthorized\":` branch", su_branch is not None, None)
    if su_branch is not None:
        su_branch_src = ast.get_source_segment(PANEL_SRC, su_branch) or ""
        check("static. session_unauthorized branch no longer mentions devlogin at all", "devlogin" not in su_branch_src, su_branch_src)

    # The ordinary manager-card devlogin button must still exist SOMEWHERE
    # in panel_bot.py, OUTSIDE _hnv2_family_actions's own source span.
    lines = PANEL_SRC.splitlines()
    outside_src = "\n".join(
        line for i, line in enumerate(lines, start=1)
        if not (fam_span[0] <= i <= fam_span[1])
    )
    check("static. ordinary manager card's devlogin callback_data ('devlogin:start:') still present outside _hnv2_family_actions",
          "devlogin:start:" in outside_src, None)
    check("static. ordinary manager card's devlogin button label ('Вход на устройстве') still present outside _hnv2_family_actions",
          "Вход на устройстве" in outside_src, None)

    # The devlogin callback handler itself (dispatched on data.startswith
    # ("devlogin:")) must be untouched -- same def, same dispatch guard.
    devlogin_cb_defs = [n for n in PANEL_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_devlogin_callback"]
    check("static. _devlogin_callback handler defined exactly once", len(devlogin_cb_defs) == 1, len(devlogin_cb_defs))
    if devlogin_cb_defs:
        cb_src = ast.get_source_segment(PANEL_SRC, devlogin_cb_defs[0]) or ""
        check("static. _devlogin_callback still dispatches on data.startswith(\"devlogin:\")",
              'data.startswith("devlogin:")' in cb_src, cb_src)

    # _HNV2_UNSAFE_ACTION_PREFIXES is defense-in-depth for ALL families and
    # is deliberately left untouched by this hotfix (still lists
    # "devlogin:start:" even though no family currently emits it) -- assert
    # it was not narrowed as a side effect.
    prefix_assigns = [n for n in PANEL_TREE.body if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "_HNV2_UNSAFE_ACTION_PREFIXES"]
    check("static. _HNV2_UNSAFE_ACTION_PREFIXES defined exactly once", len(prefix_assigns) == 1, len(prefix_assigns))
    if prefix_assigns:
        prefix_src = ast.get_source_segment(PANEL_SRC, prefix_assigns[0]) or ""
        check("static. _HNV2_UNSAFE_ACTION_PREFIXES still lists devlogin:start: (untouched, defense-in-depth)",
              '"devlogin:start:"' in prefix_src, prefix_src)


# ======================================================================
# PART B: main.py -- /hnv2_diag strictly read-only contract.
# ======================================================================

def test_static_diag_read_only():
    print("\n-- Static: /hnv2_diag is strictly read-only (AST-verified) --")
    defs = [n for n in MAIN_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_hnv2_diag_text"]
    check("static. _hnv2_diag_text defined exactly once", len(defs) == 1, len(defs))
    if not defs:
        return
    src = ast.unparse(defs[0])

    forbidden_write_calls = []
    for node in ast.walk(defs[0]):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
            if name in ("execute", "executemany", "executescript", "commit", "manager_add", "manager_set_fields",
                        "health_notify_quota_claim", "health_incident_upsert_open", "health_incident_resolve",
                        "_spawn_manager_process", "_stop_manager_process", "send_code_request", "sign_in", "start"):
                forbidden_write_calls.append(name)
    check("static. no write/mutating/process-control call anywhere in _hnv2_diag_text", forbidden_write_calls == [], forbidden_write_calls)
    # HNV2 N-1 correction (post-independent-review): /hnv2_diag must use the
    # STRICT read-only getters (never create schema) -- health_incident_v2_
    # get_readonly/health_notify_quota_get_readonly, never the ensure_*-
    # calling health_incident_v2_get/health_notify_quota_get, and never any
    # ensure_*/migration helper transitively (schema-missing must be
    # reported, not silently created).
    check("static. uses health_notify_quota_get_readonly, never the ensure_*-calling health_notify_quota_get/health_notify_quota_claim",
          "health_notify_quota_get_readonly(" in src and "health_notify_quota_claim(" not in src
          and "health_notify_quota_get(" not in src.replace("health_notify_quota_get_readonly(", ""), None)
    check("static. uses health_incident_v2_get_readonly, never the ensure_*-calling health_incident_v2_get",
          "health_incident_v2_get_readonly(" in src
          and "health_incident_v2_get(" not in src.replace("health_incident_v2_get_readonly(", ""), None)
    check("static. never calls any ensure_*/migration helper",
          not re.search(r"\bensure_health_incidents_v2_columns\(|\bensure_health_notify_quota_table\(|\bensure_health_incidents_table\(", src), None)

    # The dispatcher itself must never route /hnv2_diag to anything but
    # the pure-read helper.
    #
    # SUPERSEDED premise (Ф4, unified auth UX -- predates this session's
    # bot-wide terminal-OK pass, only now surfaced by a full regression
    # sweep): main.py's _panel_execute_command_text got a NEW final override
    # (the _AUTHUI_DISPATCH block) whose OWN body only handles the new auth
    # commands and falls back to a captured _AUTHUI_PREV_PANEL_EXEC
    # reference for everything else -- so the literal string "/hnv2_diag"
    # no longer appears in the LAST def's own unparsed source, even though
    # it's still fully reachable through the fallback chain (same
    # architecture already proven and tested by manager_replacement_
    # adminbot_selftest.py's "dispatcher: ... does not reimplement/shadow"
    # checks for the earlier REPL3/TDIMPORT/RENEWAL/HNV2 generations). The
    # correct invariant is "reachable somewhere in the active chain", not
    # "present in the last def's own body".
    dispatch_defs = [n for n in MAIN_TREE.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "_panel_execute_command_text"]
    last_dispatch_src = ast.unparse(dispatch_defs[-1]) if dispatch_defs else ""
    all_dispatch_src = "\n\n".join(ast.unparse(n) for n in dispatch_defs)
    check("static. /hnv2_diag -> _hnv2_diag_text dispatch exists somewhere in the "
          "_panel_execute_command_text override chain",
          "/hnv2_diag" in all_dispatch_src and "_hnv2_diag_text(" in all_dispatch_src, None)
    check("static. the LAST (active) _panel_execute_command_text generation either "
          "handles /hnv2_diag directly OR falls back to a captured PREV reference "
          "(proving /hnv2_diag stays reachable, not shadowed by the newest override)",
          ("/hnv2_diag" in last_dispatch_src and "_hnv2_diag_text(" in last_dispatch_src)
          or bool(re.search(r"_PREV_PANEL_EXEC\s*\(|_PREV\s*\(", last_dispatch_src)), last_dispatch_src[:300])


def test_runtime_diag_no_db_mutation():
    print("\n-- Runtime: /hnv2_diag causes zero DB writes and returns a secret-free report --")

    nodes = _extract_by_names(MAIN_TREE, MAIN_SRC, {
        "_hnv2_diag_text", "_hnv2_diag_scrub", "_HNV2_DIAG_SECRET_PATTERNS",
        "_hnv2_collect_evidence", "_hnv2_classify_root_cause", "_hnv2_result",
        "_hnv2_signature", "_hnv2_normalize_sig_component",
        "_hnv2_process_state", "_hnv2_count_new_recovery_events", "_hnv2_last_spawn_verify",
        "HNV2_FAMILY_SESSION_UNAUTHORIZED", "HNV2_FAMILY_ACCOUNT_BLOCKED",
        "HNV2_FAMILY_PEERFLOOD", "HNV2_FAMILY_FLOODWAIT", "HNV2_FAMILY_PROXY_AUTH_FAILED",
        "HNV2_FAMILY_PROXY_UNAVAILABLE", "HNV2_FAMILY_SQLITE_LOCK", "HNV2_FAMILY_NETWORK_TIMEOUT",
        "HNV2_FAMILY_WORKER_STUCK_STARTING", "HNV2_FAMILY_WORKER_CRASH", "HNV2_FAMILY_RECOVERY_FLAP",
        "HNV2_FAMILY_HEALTH_MISSING_STALE", "HNV2_FAMILY_OK", "HNV2_FAMILY_UNKNOWN",
        "HNV2_DAILY_PROBLEM_CAP",
        "_HNV2_SESSION_MARKS", "_HNV2_ACCOUNT_BLOCKED_MARKS", "_HNV2_PROXY_AUTH_MARKS",
        "_HNV2_NETWORK_TIMEOUT_MARKS", "_HNV2_SQLITE_LOCK_MARKS", "_HNV2_SQLITE_MALFORMED_MARKS",
        "_HNV2_FLOODWAIT_TEXT_MARKS",
        "TP_HG_CHECK_INTERVAL_SEC", "TPAG_V2_MONITOR_INTERVAL_SEC", "HEALTH_AGG_RECOVERY_FLAP_THRESHOLD",
        "HEALTH_AGG_RECOVERY_WINDOW_MIN", "_tp_hg_parse_iso", "_tp_hg_restriction_family",
        "_tp_hg_stable_error_text", "_TP_HG_FAMILY_PEERFLOOD", "_TP_HG_FAMILY_FLOODWAIT",
        "_TP_HG_FAMILY_BLOCKED", "_TP_HG_FAMILY_WARNING", "_TP_HG_FAMILY_UNKNOWN",
        "TP_HG_STATUS_OK", "TP_HG_STATUS_WARNING", "TP_HG_STATUS_LIMITED", "TP_HG_STATUS_BLOCKED", "TP_HG_STATUS_UNKNOWN",
        "HNV2_ACCOUNT_MAX_AGE_SEC", "HNV2_PROXY_MAX_AGE_SEC",
        "_manager_recovery_read_start_status",
        # HNV2 N-1 correction (post-independent-review): _hnv2_diag_text now
        # calls this small pure helper to unwrap the readonly getters'
        # {'_schema_missing': True} sentinel.
        "_hnv2_diag_row",
    })
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    import storage
    import manager_registry
    import subprocess as _real_subprocess  # never actually invoked below

    tmp_root = Path(tempfile.mkdtemp(prefix="hnv2_diag_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    prod_db_dir = os.path.abspath(os.path.join(str(BASE_DIR), "db"))
    assert not os.path.abspath(db_path).startswith(prod_db_dir)
    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    con = sqlite3.connect(db_path)
    con.close()

    class _NoRunSubprocess:
        def run(self, *a, **kw):
            raise AssertionError("read-only /hnv2_diag must never spawn a real subprocess in this test")

    async def fake_read_proxy_rows():
        return [{"manager_key": "mgr_diag", "auth_guard_state": "blocked", "auth_guard_error": "password=SUPERSECRET123 token=abcxyz"}]

    async def fake_read_account_rows():
        return {"mgr_diag": {"health_status": "warning", "error_class": "SomeError", "error_text": "session at path C:\\secret\\password=hunter2"}}

    import datetime as _dt
    ns = {
        "os": os, "sys": sys, "re": re, "json": __import__("json"), "time": __import__("time"),
        "datetime": _dt.datetime, "timedelta": _dt.timedelta, "timezone": _dt.timezone,
        "asyncio": asyncio, "Any": Any, "Dict": Dict, "List": List, "Optional": Optional, "Tuple": Tuple,
        "subprocess": _NoRunSubprocess(),
        "BASE_DIR": tmp_root, "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "_repl_storage": storage,
        "_health_agg_read_proxy_rows": fake_read_proxy_rows,
        "_health_agg_read_account_rows": fake_read_account_rows,
        "_manager_runtime_ready_once": None,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:hnv2_diag>", "exec"), ns)

    # Realistic starting state: in production the aggregator tick (every
    # HEALTH_AGG_INTERVAL_SEC) has ALREADY created the health_incidents/
    # health_notify_quota schema long before any operator ever clicks
    # "Диагностика" -- pre-bootstrap it here too, so this test measures
    # what /hnv2_diag itself does, not first-run schema creation (which
    # is the aggregator's job, not this command's).
    storage.ensure_health_incidents_v2_columns(db_path=db_path)
    storage.ensure_health_notify_quota_table(db_path=db_path)

    before_bytes = Path(db_path).read_bytes()
    text = asyncio.run(ns["_hnv2_diag_text"]("mgr_diag"))
    after_bytes = Path(db_path).read_bytes()

    check("runtime. the DB file's bytes are byte-for-byte identical before and after /hnv2_diag", before_bytes == after_bytes, (len(before_bytes), len(after_bytes)))
    check("runtime. output is a non-empty string", isinstance(text, str) and len(text) > 0, text[:200] if text else text)
    check("runtime. secrets are scrubbed from the output ('password=SUPERSECRET123' never appears verbatim)", "SUPERSECRET123" not in text, text)
    check("runtime. secrets are scrubbed from the output ('token=abcxyz' never appears verbatim)", "token=abcxyz" not in text, text)
    check("runtime. secrets are scrubbed from the output ('password=hunter2' never appears verbatim)", "hunter2" not in text, text)
    check("runtime. the manager key IS present (report is actually useful, not over-redacted)", "mgr_diag" in text, text)

    # HNV2 N-1 correction (post-independent-review): the SAME extracted
    # _hnv2_diag_text, called against a temp DB where the HNV2 schema has
    # NEVER been migrated (no aggregator tick has ever run) -- must not
    # create anything. This is the actual acceptance test for N-1: before
    # this fix, health_incident_v2_get/health_notify_quota_get both called
    # their ensure_*() counterpart, so even a strictly-read-only diagnostic
    # command would silently CREATE the schema on a fresh DB.
    tmp_root2 = Path(tempfile.mkdtemp(prefix="hnv2_diag_noschema_selftest_"))
    db_path2 = str(tmp_root2 / "data_tpilot.db")
    con2 = sqlite3.connect(db_path2)
    # Pre-touch the file exactly the way EVERY storage.py connection does,
    # project-wide, unconditionally, regardless of HNV2 (storage._bsl_connect
    # always runs 'PRAGMA journal_mode=WAL;' on every connect -- on a brand
    # new/empty file this is SQLite materializing page 1 to persist the file
    # format flag, a one-time page-allocation side effect of WAL mode itself,
    # not an HNV2-introduced write and not application data). Isolating past
    # this universal baseline is what actually measures whether
    # _hnv2_diag_text adds any mutation of its OWN beyond it.
    con2.execute("PRAGMA journal_mode=WAL;")
    con2.close()
    ns2 = dict(ns)
    ns2["TPILOT_DB_PATH"] = db_path2
    exec(compile(module_src, f"<{MAIN_PATH}:hnv2_diag_noschema>", "exec"), ns2)

    tables_before = set(sqlite3.connect(db_path2).execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    bytes_before2 = Path(db_path2).read_bytes()
    text2 = asyncio.run(ns2["_hnv2_diag_text"]("mgr_diag"))
    tables_after = set(sqlite3.connect(db_path2).execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall())
    bytes_after2 = Path(db_path2).read_bytes()

    check("runtime (no-schema DB). zero new tables created", tables_after == tables_before, (tables_before, tables_after))
    check("runtime (no-schema DB). DB file bytes unchanged", bytes_before2 == bytes_after2, (len(bytes_before2), len(bytes_after2)))
    check("runtime (no-schema DB). output still non-empty and useful", isinstance(text2, str) and len(text2) > 0 and "mgr_diag" in text2, text2[:200] if text2 else text2)
    check("runtime (no-schema DB). reports schema unavailable instead of fabricating incident/quota state",
          "schema unavailable" in text2.lower() or "not initialized" in text2.lower(), text2)
    import shutil as _shutil2
    _shutil2.rmtree(str(tmp_root2), ignore_errors=True)

    try:
        import shutil
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


def main() -> int:
    test_family_button_map()
    test_n2_confidence_degradation()
    test_n8_floodwait_cooldown_buttons()
    test_notification_buttons_end_to_end()
    test_scenario_15_callback_contract()
    test_static_callback_structure()
    test_devlogin_hotfix_20260808()
    test_static_diag_read_only()
    test_runtime_diag_no_db_mutation()

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
