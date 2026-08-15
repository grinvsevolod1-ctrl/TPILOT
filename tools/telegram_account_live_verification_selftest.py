# -*- coding: utf-8 -*-
"""tools/telegram_account_live_verification_selftest.py -- offline selftest
for "CORRECTION A 20260810": TPilot must never declare a manager's Telegram
account "заблокирован/деактивирован" on the strength of a single suspicious
RPC error about an ARBITRARY peer/operation (e.g. InputUserDeactivatedError
raised while sending to a lead whose OWN account was deactivated) without
first independently verifying, via a live client.get_me() on the manager's
OWN already-authenticated session, that the manager's account itself is
actually the one with the problem.

Technique: main.py cannot be imported standalone (Telethon/env side effects
at import time) -- the real _tp_hg_verify_account_live/_tp_hg_set_from_
exception/_tp_hg_classify_exception/_tp_hg_me_health_problem/_tp_hg_me_flags
functions (and their real dependency constants) are extracted via
ast.parse + ast.unparse + exec(), the same technique every other main.py-
testing selftest in this project uses (see tools/qr_auth_context_selftest.py
for the established pattern). _tp_hg_update_status itself is faked (a plain
list-recording stub) -- Correction A is about the CLASSIFICATION decision
(what status/error_class gets written), not the DB/notification plumbing,
which is already covered by tools/health_incident_selftest.py and friends.

    python tools\\telegram_account_live_verification_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(PROJECT_ROOT / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()
PANEL_PATH = str(PROJECT_ROOT / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()

# ======================================================================
# Fake Telegram exception classes -- telethon is not installed locally.
# Only the CLASS NAME matters (the real classifiers match on
# type(exc).__name__ + str(exc), never isinstance/import-based), so a
# plain Exception subclass named identically to the real Telethon class
# is fully sufficient and proves the classifiers really are import-free.
# ======================================================================

class InputUserDeactivatedError(Exception):
    pass


class UserDeactivatedError(Exception):
    """Raised by real Telethon when the CALLING account's own session is
    used against a Telegram-deactivated account."""
    pass


class UserDeactivatedBanError(Exception):
    """Raised by real Telethon when the calling account is BANNED."""
    pass


class AuthKeyUnregisteredError(Exception):
    pass


class SessionRevokedError(Exception):
    pass


class PhoneNumberBannedError(Exception):
    pass


class SocksProxyConnectionError(Exception):
    pass


class TimeoutError_(Exception):  # avoid shadowing builtins.TimeoutError
    pass


class SomeUnrecognisedTelegramError(Exception):
    """Deliberately matches NONE of the mark lists -- proves fail-closed
    behavior on truly ambiguous evidence."""
    pass


TP_HG_NAMES = {
    "TP_HG_STATUS_OK", "TP_HG_STATUS_WARNING", "TP_HG_STATUS_LIMITED",
    "TP_HG_STATUS_BLOCKED", "TP_HG_STATUS_UNKNOWN",
    "_TP_HG_VERIFY_BANNED_MARKS", "_TP_HG_VERIFY_DEACTIVATED_MARKS",
    "_TP_HG_VERIFY_AUTH_MARKS", "_TP_HG_VERIFY_NETWORK_MARKS",
    "TP_HG_VERIFY_ACCOUNT_LIVE", "TP_HG_VERIFY_AUTH_SESSION_PROBLEM",
    "TP_HG_VERIFY_IDENTITY_MISMATCH", "TP_HG_VERIFY_ACCOUNT_DEACTIVATED_CONFIRMED",
    "TP_HG_VERIFY_ACCOUNT_BANNED_CONFIRMED", "TP_HG_VERIFY_NETWORK_OR_PROXY_PROBLEM",
    "TP_HG_VERIFY_UNKNOWN",
    "_tp_hg_error_text", "_tp_hg_norm_key", "_tp_hg_me_flags",
    "_tp_hg_me_health_problem", "_tp_hg_classify_exception",
    "_tp_hg_verify_account_live", "_tp_hg_set_from_exception",
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


class FakeMe:
    def __init__(self, *, id=0, username=None, first_name="Test", last_name="",
                 deleted=False, restricted=False, restriction_reason=None):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.deleted = deleted
        self.restricted = restricted
        self.restriction_reason = restriction_reason


class FakeClient:
    """script controls get_me()'s outcome:
    {"get_me": "ok", "me": FakeMe(...)}                -> returns the FakeMe
    {"get_me": "raise", "exc": SomeException(...)}     -> raises exc
    {"get_me": "empty"}                                -> returns None
    """

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    async def get_me(self):
        self.calls.append("get_me")
        mode = self.script.get("get_me", "ok")
        if mode == "raise":
            raise self.script["exc"]
        if mode == "empty":
            return None
        return self.script.get("me") or FakeMe(id=999001)


def build_ns(*, manager_runtime_key: str, controller_mode: bool, client_script: dict,
             stored_tg_user_id: int) -> dict:
    nodes = _extract_by_names(MAIN_SRC, TP_HG_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    update_status_calls = []

    async def fake_tp_hg_update_status(manager_key, *, status, error_class="", error_text="",
                                        error_source="", action_required="", allow_recover=False,
                                        cooldown_seconds=0):
        row = {
            "manager_key": manager_key, "status": status, "error_class": error_class,
            "error_text": error_text, "error_source": error_source, "action_required": action_required,
        }
        update_status_calls.append(row)
        return row

    fake_client = FakeClient(client_script)

    def fake_get_manager_row_from_db_sync(db_path, key):
        return {"tg_user_id": stored_tg_user_id}

    ns = {
        "Any": object, "Dict": dict, "Optional": None,
        "client": fake_client,
        "MANAGER_RUNTIME_KEY": manager_runtime_key,
        "CONTROLLER_MODE": controller_mode,
        "TPILOT_DB_PATH": ":memory:",
        "registry_normalize_manager_key": lambda k: str(k or "").strip().lower(),
        "get_manager_row_from_db_sync": fake_get_manager_row_from_db_sync,
        "_tp_hg_update_status": fake_tp_hg_update_status,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:tp_hg_account_live>", "exec"), ns)
    ns["__update_status_calls__"] = update_status_calls
    ns["__fake_client__"] = fake_client
    return ns


# ======================================================================
# Alert-text builder (_tp_hg_build_alert_text) -- requirement #15: the
# admin-facing health alert must not say "забанен" for AUTH_SESSION_PROBLEM
# or IDENTITY_MISMATCH, and must not suggest a plain relogin for a
# CONFIRMED ban (action_required already forbids that). Extracted for
# real; only its label/lookup dependencies (which need a live DB/manager
# registry) are faked -- their exact wording doesn't matter to these
# checks, only the title/action lines the real function composes around them.
# ======================================================================
ALERT_TEXT_NAMES = TP_HG_NAMES | {"_tp_hg_build_alert_text"}


def build_alert_ns() -> dict:
    nodes = _extract_by_names(MAIN_SRC, ALERT_TEXT_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "Any": object, "Dict": dict, "Optional": None, "re": __import__("re"),
        "_kyiv_now": lambda: __import__("datetime").datetime(2026, 8, 10, 12, 0, 0),
        "_tp_hg_status_label": lambda st: f"label({st})",
        "_tp_hg_manager_label_from_key": lambda k: k,
        "_hnv2_tg_health_family": lambda st, ec: "family",
        "_tp_hg_username_from_key": lambda k: "@" + k,
        "_tp_hg_norm_key": lambda k: str(k or "").strip().lower(),
    }
    exec(compile(module_src, f"<{MAIN_PATH}:tp_hg_alert_text>", "exec"), ns)
    return ns


# ======================================================================
# Downstream reclassification safety (checkpoint requirement #10): a row
# _tp_hg_set_from_exception writes for AUTH_SESSION_PROBLEM/IDENTITY_
# MISMATCH must not be re-classified as a ban by the OTHER two independent
# classifiers that also inspect manager_telegram_health -- HNV2's
# _hnv2_classify_root_cause (main.py) and panel_bot's
# _pb_manager_status_resolve. Both are extracted for REAL (not
# reimplemented) and fed the ACTUAL rows _tp_hg_set_from_exception wrote in
# H4/H5/H8/H9 above, so this proves the real end-to-end pipeline, not just
# tuple-membership reasoning.
# ======================================================================
HNV2_CLASSIFY_NAMES = {
    "HEALTH_AGG_RECOVERY_FLAP_THRESHOLD", "HNV2_ACCOUNT_MAX_AGE_SEC", "HNV2_CONNECTED_MAX_AGE_SEC",
    "HNV2_EXIT_EVIDENCE_MAX_AGE_SEC", "HNV2_FAMILY_ACCOUNT_BLOCKED", "HNV2_FAMILY_FLOODWAIT",
    "HNV2_FAMILY_HEALTH_MISSING_STALE", "HNV2_FAMILY_NETWORK_TIMEOUT", "HNV2_FAMILY_OK",
    "HNV2_FAMILY_PEERFLOOD", "HNV2_FAMILY_PROXY_AUTH_FAILED", "HNV2_FAMILY_PROXY_UNAVAILABLE",
    "HNV2_FAMILY_RECOVERY_FLAP", "HNV2_FAMILY_SESSION_UNAUTHORIZED", "HNV2_FAMILY_SQLITE_LOCK",
    "HNV2_FAMILY_UNKNOWN", "HNV2_FAMILY_WORKER_CRASH", "HNV2_FAMILY_WORKER_STUCK_STARTING",
    "HNV2_PROXY_MAX_AGE_SEC", "HNV2_STUCK_STARTING_SEC",
    "_HNV2_ACCOUNT_BLOCKED_MARKS", "_HNV2_FLOODWAIT_TEXT_MARKS", "_HNV2_NETWORK_TIMEOUT_MARKS",
    "_HNV2_PROXY_AUTH_MARKS", "_HNV2_SESSION_MARKS", "_HNV2_SQLITE_LOCK_MARKS", "_HNV2_SQLITE_MALFORMED_MARKS",
    "_TP_HG_FAMILY_FLOODWAIT", "_TP_HG_FAMILY_PEERFLOOD", "_TP_HG_FAMILY_BLOCKED", "_TP_HG_FAMILY_WARNING",
    "_TP_HG_FAMILY_UNKNOWN", "TP_HG_STATUS_BLOCKED", "TP_HG_STATUS_LIMITED", "TP_HG_STATUS_WARNING",
    "TP_HG_CHECK_INTERVAL_SEC", "TPAG_V2_MONITOR_INTERVAL_SEC",
    "_hnv2_result", "_tp_hg_restriction_family", "_hnv2_classify_root_cause",
}


def build_hnv2_classify_ns() -> dict:
    nodes = _extract_by_names(MAIN_SRC, HNV2_CLASSIFY_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {"Any": object, "Dict": dict, "Optional": None, "os": __import__("os")}
    exec(compile(module_src, f"<{MAIN_PATH}:hnv2_classify>", "exec"), ns)
    return ns


def hnv2_ev_for(hrow: dict) -> dict:
    return {
        "mrow": {}, "hrow": hrow, "ss": {}, "proc": "probe_error",
        "flap_new": 0, "last_verify": "none",
        "age_account": None, "age_proxy": None, "age_ss": None, "age_exit": None,
    }


PB_STATUS_RESOLVE_NAMES = {
    "_PB_STATUS_AUTH_MARKS", "_PB_STATUS_BANNED_MARKS", "_PB_STATUS_CATEGORIES",
    "_pb_manager_status_resolve",
}


def build_pb_status_ns() -> dict:
    nodes = _extract_by_names(PANEL_SRC, PB_STATUS_RESOLVE_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "Any": object, "Dict": dict, "Optional": None,
        # Proxy state is orthogonal to ban/auth classification -- a neutral
        # value that never matches ("not_configured", "broken") keeps
        # category 3 (proxy_bad) from masking the ban/auth category under test.
        "_pb_proxy_effective_state": lambda row: "healthy",
    }
    exec(compile(module_src, f"<{PANEL_PATH}:pb_status_resolve>", "exec"), ns)
    return ns


MANAGER_KEY = "mgr_accountlive"
STORED_TG_ID = 700500


def main() -> int:
    # ==================================================================
    # H1: InputUserDeactivatedError (about a PEER, e.g. client_auto_send)
    # + get_me() succeeds with the SAME id -> ACCOUNT_LIVE, NOT BANNED.
    # This is the exact regression scenario from the owner's spec (section
    # 9): a manager whose own account is fine must never be shown
    # "Забанен/ограничен Telegram" just because sending to some other lead
    # failed with a deactivated-shaped error.
    # ==================================================================
    ns1 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "ok", "me": FakeMe(id=STORED_TG_ID, username=None)},
        stored_tg_user_id=STORED_TG_ID,
    )
    r1 = asyncio.run(ns1["_tp_hg_set_from_exception"](
        MANAGER_KEY, InputUserDeactivatedError("The user has been deleted/deactivated"),
        source="client_auto_send",
    ))
    calls1 = ns1["__update_status_calls__"]
    check("H1. exactly one manager_telegram_health write occurred", len(calls1) == 1, calls1)
    check("H1. status is NOT 'blocked' after live verification (ACCOUNT_LIVE)",
          calls1[-1]["status"] != ns1["TP_HG_STATUS_BLOCKED"], calls1[-1])
    check("H1. error_class does not carry a ban/deactivation marker",
          not any(m in calls1[-1]["error_class"].lower() for m in ("banned", "deactivated")), calls1[-1])
    check("H1. no Russian 'заблокирован'/'забанен' wording anywhere in the written row",
          "заблокирован" not in calls1[-1]["error_text"].lower() and "забанен" not in calls1[-1]["error_text"].lower()
          and "заблокирован" not in calls1[-1]["action_required"].lower() and "забанен" not in calls1[-1]["action_required"].lower(),
          calls1[-1])
    check("H1. get_me was actually called (real verification happened, not skipped)",
          "get_me" in ns1["__fake_client__"].calls, ns1["__fake_client__"].calls)

    # ==================================================================
    # H2/H3: username changed / username None, id unchanged -> LIVE.
    # Direct call to _tp_hg_verify_account_live (the identity anchor is
    # tg_user_id ONLY -- username must never be consulted).
    # ==================================================================
    ns2 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "ok", "me": FakeMe(id=STORED_TG_ID, username="brand_new_name")},
        stored_tg_user_id=STORED_TG_ID,
    )
    v2 = asyncio.run(ns2["_tp_hg_verify_account_live"](MANAGER_KEY, source="test"))
    check("H2. username changed (id unchanged) -> ACCOUNT_LIVE", v2.get("verify_class") == ns2["TP_HG_VERIFY_ACCOUNT_LIVE"], v2)

    ns3 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "ok", "me": FakeMe(id=STORED_TG_ID, username=None)},
        stored_tg_user_id=STORED_TG_ID,
    )
    v3 = asyncio.run(ns3["_tp_hg_verify_account_live"](MANAGER_KEY, source="test"))
    check("H3. username is None (id unchanged) -> ACCOUNT_LIVE", v3.get("verify_class") == ns3["TP_HG_VERIFY_ACCOUNT_LIVE"], v3)

    # ==================================================================
    # H4: verification's OWN get_me() raises a session/auth exception ->
    # AUTH_SESSION_PROBLEM, NOT BANNED.
    # ==================================================================
    ns4 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "raise", "exc": AuthKeyUnregisteredError("The key is not registered in the system")},
        stored_tg_user_id=STORED_TG_ID,
    )
    r4 = asyncio.run(ns4["_tp_hg_set_from_exception"](
        MANAGER_KEY, InputUserDeactivatedError("some peer error"), source="client_auto_send",
    ))
    calls4 = ns4["__update_status_calls__"]
    check("H4. status stays 'blocked' (sends genuinely can't succeed)", calls4[-1]["status"] == ns4["TP_HG_STATUS_BLOCKED"], calls4[-1])
    check("H4. action_required tells the admin to RELOGIN, not that the account is banned",
          "перезайти" in calls4[-1]["action_required"].lower() and "забанен" not in calls4[-1]["action_required"].lower(), calls4[-1])
    check("H4. error_class carries an auth-session marker, not a ban marker",
          "authkeyunregistered" in calls4[-1]["error_class"].lower(), calls4[-1])

    # ==================================================================
    # H5: verification succeeds but me.id != stored tg_user_id ->
    # IDENTITY_MISMATCH, NOT BANNED, identity never silently rewritten.
    # ==================================================================
    ns5 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "ok", "me": FakeMe(id=STORED_TG_ID + 12345, username="someoneelse")},
        stored_tg_user_id=STORED_TG_ID,
    )
    r5 = asyncio.run(ns5["_tp_hg_set_from_exception"](
        MANAGER_KEY, InputUserDeactivatedError("some peer error"), source="client_auto_send",
    ))
    calls5 = ns5["__update_status_calls__"]
    check("H5. error_class is exactly 'telegram_identity_mismatch'", calls5[-1]["error_class"] == "telegram_identity_mismatch", calls5[-1])
    check("H5. error_text carries both the stored and live ids", str(STORED_TG_ID) in calls5[-1]["error_text"] and str(STORED_TG_ID + 12345) in calls5[-1]["error_text"], calls5[-1])
    check("H5. action_required explicitly says identity is NOT rewritten automatically",
          "автоматически" in calls5[-1]["action_required"].lower(), calls5[-1])
    check("H5. no ban wording in the written row", "забанен" not in calls5[-1]["error_text"].lower() and "забанен" not in calls5[-1]["action_required"].lower(), calls5[-1])

    # ==================================================================
    # H6: verification's OWN get_me() fails with a proxy/network error ->
    # NETWORK_OR_PROXY_PROBLEM, NOT BANNED (fail toward "don't know").
    # ==================================================================
    ns6 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "raise", "exc": SocksProxyConnectionError("Connection to the SOCKS5 proxy timed out")},
        stored_tg_user_id=STORED_TG_ID,
    )
    r6 = asyncio.run(ns6["_tp_hg_set_from_exception"](
        MANAGER_KEY, InputUserDeactivatedError("some peer error"), source="client_auto_send",
    ))
    calls6 = ns6["__update_status_calls__"]
    check("H6. status is downgraded to 'warning', never left as a confirmed ban", calls6[-1]["status"] == ns6["TP_HG_STATUS_WARNING"], calls6[-1])
    check("H6. no ban wording anywhere in the written row",
          "забанен" not in calls6[-1]["error_text"].lower() and "заблокирован" not in calls6[-1]["action_required"].lower(), calls6[-1])

    # ==================================================================
    # H7: verification is genuinely inconclusive (unrecognised exception,
    # matches none of the mark lists) -> UNKNOWN/VERIFY_FAILED, NOT BANNED.
    # ==================================================================
    ns7 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "raise", "exc": SomeUnrecognisedTelegramError("totally novel Telegram error text")},
        stored_tg_user_id=STORED_TG_ID,
    )
    r7 = asyncio.run(ns7["_tp_hg_set_from_exception"](
        MANAGER_KEY, InputUserDeactivatedError("some peer error"), source="client_auto_send",
    ))
    calls7 = ns7["__update_status_calls__"]
    check("H7. status is downgraded to 'warning' on genuinely ambiguous verification (fail-closed)",
          calls7[-1]["status"] == ns7["TP_HG_STATUS_WARNING"], calls7[-1])
    check("H7. action_required promises a retry, never claims a confirmed ban",
          "повторная проверка" in calls7[-1]["action_required"].lower(), calls7[-1])

    # H7b: get_me() returns an empty result (no exception, but no user
    # object either) -- same fail-closed contract.
    ns7b = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "empty"},
        stored_tg_user_id=STORED_TG_ID,
    )
    r7b = asyncio.run(ns7b["_tp_hg_set_from_exception"](
        MANAGER_KEY, InputUserDeactivatedError("some peer error"), source="client_auto_send",
    ))
    calls7b = ns7b["__update_status_calls__"]
    check("H7b. empty get_me() result also fails closed to 'warning', never 'blocked'-as-ban",
          calls7b[-1]["status"] == ns7b["TP_HG_STATUS_WARNING"], calls7b[-1])

    # ==================================================================
    # H8: verification's OWN get_me() raises with DEACTIVATED evidence
    # (about the manager's OWN account, not a peer) -> DEACTIVATED_CONFIRMED.
    # ==================================================================
    ns8 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "raise", "exc": UserDeactivatedError("The user has been deleted/deactivated")},
        stored_tg_user_id=STORED_TG_ID,
    )
    r8 = asyncio.run(ns8["_tp_hg_set_from_exception"](
        MANAGER_KEY, InputUserDeactivatedError("some peer error"), source="client_auto_send",
    ))
    calls8 = ns8["__update_status_calls__"]
    check("H8. status is 'blocked' (confirmed by direct first-party evidence)", calls8[-1]["status"] == ns8["TP_HG_STATUS_BLOCKED"], calls8[-1])
    check("H8. action_required says NOT to restart/relogin, but to replace the account",
          "не перезапускать" in calls8[-1]["action_required"].lower() and "заменить аккаунт" in calls8[-1]["action_required"].lower(), calls8[-1])
    check("H8. error_class carries a deactivation marker", "userdeactivated" in calls8[-1]["error_class"].lower(), calls8[-1])

    # ==================================================================
    # H9: verification's OWN get_me() raises with BANNED evidence ->
    # BANNED_CONFIRMED.
    # ==================================================================
    ns9 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "raise", "exc": UserDeactivatedBanError("The user has been banned")},
        stored_tg_user_id=STORED_TG_ID,
    )
    r9 = asyncio.run(ns9["_tp_hg_set_from_exception"](
        MANAGER_KEY, InputUserDeactivatedError("some peer error"), source="client_auto_send",
    ))
    calls9 = ns9["__update_status_calls__"]
    check("H9. status is 'blocked' (confirmed ban)", calls9[-1]["status"] == ns9["TP_HG_STATUS_BLOCKED"], calls9[-1])
    check("H9. error_class carries a ban marker", "userdeactivatedban" in calls9[-1]["error_class"].lower(), calls9[-1])
    check("H9. action_required says NOT to restart/relogin, but to replace the account",
          "не перезапускать" in calls9[-1]["action_required"].lower(), calls9[-1])

    # ==================================================================
    # UX1-UX4 (requirement #15): the admin-facing health ALERT text built
    # from H4/H5/H8/H9's written rows must not misdescribe what happened.
    # ==================================================================
    ns_alert = build_alert_ns()
    build_alert = ns_alert["_tp_hg_build_alert_text"]

    text4 = build_alert(MANAGER_KEY, calls4[-1])
    check("UX1. AUTH_SESSION_PROBLEM alert never says 'забанен'", "забанен" not in text4.lower(), text4)
    check("UX1. AUTH_SESSION_PROBLEM alert says a relogin/recovery is needed", "перезайти" in text4.lower() or "перезаход" in text4.lower() or "не авторизован" in text4.lower(), text4)

    text5 = build_alert(MANAGER_KEY, calls5[-1])
    check("UX2. IDENTITY_MISMATCH alert never says 'забанен'", "забанен" not in text5.lower(), text5)
    check("UX2. IDENTITY_MISMATCH alert mentions the id mismatch, not a generic ban", "не совпадает" in text5.lower(), text5)

    text8 = build_alert(MANAGER_KEY, calls8[-1])
    check("UX3. DEACTIVATED_CONFIRMED alert does NOT suggest reconnecting via PHONE/CODE/PASS "
          "(action_required already forbids relogin -- the alert must not contradict it)",
          "phone / code / pass" not in text8.lower(), text8)

    text9 = build_alert(MANAGER_KEY, calls9[-1])
    check("UX4. BANNED_CONFIRMED alert still shows the ban wording (only CONFIRMED bans do)", "забанен" in text9.lower(), text9)
    check("UX4. BANNED_CONFIRMED alert does NOT suggest reconnecting via PHONE/CODE/PASS",
          "phone / code / pass" not in text9.lower(), text9)

    # ==================================================================
    # DR1-DR4 (checkpoint requirement #10): feed the ACTUAL rows written for
    # H4/H5/H8/H9 into the REAL, independently-authored downstream
    # classifiers (HNV2's _hnv2_classify_root_cause, panel_bot's
    # _pb_manager_status_resolve) and prove neither reclassifies an
    # AUTH_SESSION_PROBLEM/IDENTITY_MISMATCH row as a ban.
    # ==================================================================
    ns_hnv2 = build_hnv2_classify_ns()
    hnv2_classify = ns_hnv2["_hnv2_classify_root_cause"]
    ns_pb = build_pb_status_ns()
    pb_resolve = ns_pb["_pb_manager_status_resolve"]

    def hrow_of(call_row):
        return {"health_status": call_row["status"], "error_class": call_row["error_class"], "error_text": call_row["error_text"]}

    r4_hnv2 = hnv2_classify(hnv2_ev_for(hrow_of(calls4[-1])))
    check("DR1. HNV2 classifies the real H4 (AUTH_SESSION_PROBLEM) row as session_unauthorized, not account_blocked",
          r4_hnv2.get("family") == ns_hnv2["HNV2_FAMILY_SESSION_UNAUTHORIZED"], r4_hnv2)
    r4_pb = pb_resolve({}, tg_health=hrow_of(calls4[-1]))
    check("DR1b. panel_bot resolves the real H4 row to category 'unauthorized', never 'banned'", r4_pb[0] == "unauthorized", r4_pb)

    r5_hnv2 = hnv2_classify(hnv2_ev_for(hrow_of(calls5[-1])))
    check("DR2. HNV2 classifies the real H5 (IDENTITY_MISMATCH) row as session_unauthorized, not account_blocked",
          r5_hnv2.get("family") == ns_hnv2["HNV2_FAMILY_SESSION_UNAUTHORIZED"], r5_hnv2)
    r5_pb = pb_resolve({}, tg_health=hrow_of(calls5[-1]))
    check("DR2b. panel_bot resolves the real H5 row to category 'unauthorized', never 'banned'", r5_pb[0] == "unauthorized", r5_pb)

    r8_hnv2 = hnv2_classify(hnv2_ev_for(hrow_of(calls8[-1])))
    check("DR3. HNV2 classifies the real H8 (DEACTIVATED_CONFIRMED) row as account_blocked with 'certain' confidence",
          r8_hnv2.get("family") == ns_hnv2["HNV2_FAMILY_ACCOUNT_BLOCKED"] and r8_hnv2.get("confidence") == "certain", r8_hnv2)
    r8_pb = pb_resolve({}, tg_health=hrow_of(calls8[-1]))
    check("DR3b. panel_bot resolves the real H8 row to category 'banned'", r8_pb[0] == "banned", r8_pb)

    r9_hnv2 = hnv2_classify(hnv2_ev_for(hrow_of(calls9[-1])))
    check("DR4. HNV2 classifies the real H9 (BANNED_CONFIRMED) row as account_blocked with 'certain' confidence",
          r9_hnv2.get("family") == ns_hnv2["HNV2_FAMILY_ACCOUNT_BLOCKED"] and r9_hnv2.get("confidence") == "certain", r9_hnv2)
    r9_pb = pb_resolve({}, tg_health=hrow_of(calls9[-1]))
    check("DR4b. panel_bot resolves the real H9 row to category 'banned'", r9_pb[0] == "banned", r9_pb)

    # ==================================================================
    # H10: no extra verification call is made when the original exception
    # does NOT classify as blocked in the first place (e.g. a plain
    # FloodWait) -- proves this correction costs zero extra Telegram
    # requests for the common, non-suspicious paths.
    # ==================================================================
    ns10 = build_ns(
        manager_runtime_key=MANAGER_KEY, controller_mode=False,
        client_script={"get_me": "raise", "exc": RuntimeError("get_me must NOT be called for this scenario")},
        stored_tg_user_id=STORED_TG_ID,
    )
    r10 = asyncio.run(ns10["_tp_hg_set_from_exception"](
        MANAGER_KEY, TimeoutError_("Connection timed out"), source="client_auto_send",
    ))
    check("H10. no verification get_me() call is made for a non-blocked classification (zero extra requests)",
          "get_me" not in ns10["__fake_client__"].calls, ns10["__fake_client__"].calls)

    # ==================================================================
    # H11: CONTROLLER_MODE / empty MANAGER_RUNTIME_KEY -- there is no
    # manager client to verify with, verification must be skipped safely
    # (not crash, not hang) rather than attempted.
    # ==================================================================
    ns11 = build_ns(
        manager_runtime_key="", controller_mode=True,
        client_script={"get_me": "raise", "exc": RuntimeError("must not be called in controller mode")},
        stored_tg_user_id=STORED_TG_ID,
    )
    r11 = asyncio.run(ns11["_tp_hg_set_from_exception"](
        MANAGER_KEY, InputUserDeactivatedError("some peer error"), source="client_auto_send",
    ))
    check("H11. controller-mode/empty-runtime-key path never calls get_me (no manager client exists there)",
          "get_me" not in ns11["__fake_client__"].calls, ns11["__fake_client__"].calls)

    # ==================================================================
    # MUTATION PROOFS -- each strips out exactly the safety property under
    # test and proves the corresponding H-check goes RED against the
    # mutant, proving the check is load-bearing, not a tautology.
    # ==================================================================
    nodes_mut = _extract_by_names(MAIN_SRC, TP_HG_NAMES)
    real_src = "\n\n".join(ast.unparse(n) for n in nodes_mut)

    # MUT-1: revert to the OLD direct mapping (classify_exception's BLOCKED
    # verdict written straight through, no verification at all) -> H1 must
    # go RED (a live account gets wrongly marked blocked again).
    anchor = "if str(d.get('status') or '') == TP_HG_STATUS_BLOCKED and MANAGER_RUNTIME_KEY and (not CONTROLLER_MODE):"
    check("MUT-1 setup: anchor found in the real extracted source", anchor in real_src)
    mut1_src = real_src.replace(anchor, "if False:  # MUTATION: verification disabled")
    check("MUT-1 setup: mutation actually changed the source", mut1_src != real_src)
    ns_m1 = build_ns(manager_runtime_key=MANAGER_KEY, controller_mode=False,
                      client_script={"get_me": "ok", "me": FakeMe(id=STORED_TG_ID)}, stored_tg_user_id=STORED_TG_ID)
    exec(compile(mut1_src, "<mutant no-verification>", "exec"), ns_m1)
    r_m1 = asyncio.run(ns_m1["_tp_hg_set_from_exception"](MANAGER_KEY, InputUserDeactivatedError("peer error"), source="client_auto_send"))
    mut1_calls = ns_m1["__update_status_calls__"]
    check("MUT-1. with verification disabled, H1's live account is WRONGLY marked 'blocked' -- "
          "proves the verification gate in _tp_hg_set_from_exception is load-bearing, not a tautology",
          mut1_calls[-1]["status"] == ns_m1["TP_HG_STATUS_BLOCKED"], mut1_calls[-1])

    # MUT-2: make _tp_hg_verify_account_live treat a missing/empty username
    # as ban evidence -> H3 (username=None, id unchanged) must go RED.
    anchor2 = "if stored_tg_id and stored_tg_id != live_tg_id:"
    check("MUT-2 setup: anchor found in the real extracted source", anchor2 in real_src)
    mut2_src = real_src.replace(
        anchor2,
        'if not getattr(me, "username", None):\n'
        '        return {"verify_class": TP_HG_VERIFY_ACCOUNT_BANNED_CONFIRMED, "error_class": "banned", "error_text": "MUTATION: username absent treated as ban"}\n'
        "    if stored_tg_id and stored_tg_id != live_tg_id:",
    )
    check("MUT-2 setup: mutation actually changed the source", mut2_src != real_src)
    ns_m2 = build_ns(manager_runtime_key=MANAGER_KEY, controller_mode=False,
                      client_script={"get_me": "ok", "me": FakeMe(id=STORED_TG_ID, username=None)}, stored_tg_user_id=STORED_TG_ID)
    exec(compile(mut2_src, "<mutant username-as-ban>", "exec"), ns_m2)
    v_m2 = asyncio.run(ns_m2["_tp_hg_verify_account_live"](MANAGER_KEY, source="test"))
    check("MUT-2. with 'username absent = ban' injected, H3's username-None-but-live account is "
          "WRONGLY classified as a confirmed ban -- proves H3 (username is never a ban signal) is load-bearing",
          v_m2.get("verify_class") == ns_m2["TP_HG_VERIFY_ACCOUNT_BANNED_CONFIRMED"], v_m2)

    # MUT-3: on verification failure, default to a confirmed ban instead of
    # failing closed -> H7 must go RED.
    anchor3 = "'status': TP_HG_STATUS_WARNING, 'error_class': str(verify.get('error_class') or 'VerificationInconclusive')"
    check("MUT-3 setup: anchor found in the real extracted source", anchor3 in real_src)
    mut3_src = real_src.replace(
        anchor3,
        "'status': TP_HG_STATUS_BLOCKED, 'error_class': str(verify.get('error_class') or 'VerificationInconclusive')",
    )
    check("MUT-3 setup: mutation actually changed the source", mut3_src != real_src)
    ns_m3 = build_ns(manager_runtime_key=MANAGER_KEY, controller_mode=False,
                      client_script={"get_me": "raise", "exc": SomeUnrecognisedTelegramError("novel error")}, stored_tg_user_id=STORED_TG_ID)
    exec(compile(mut3_src, "<mutant default-to-banned>", "exec"), ns_m3)
    r_m3 = asyncio.run(ns_m3["_tp_hg_set_from_exception"](MANAGER_KEY, InputUserDeactivatedError("peer error"), source="client_auto_send"))
    mut3_calls = ns_m3["__update_status_calls__"]
    check("MUT-3. with 'default to banned on inconclusive verification' injected, H7's genuinely-"
          "ambiguous case is WRONGLY marked 'blocked' -- proves the fail-closed default in H7 is load-bearing",
          mut3_calls[-1]["status"] == ns_m3["TP_HG_STATUS_BLOCKED"], mut3_calls[-1])

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL TELEGRAM ACCOUNT LIVE VERIFICATION SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
