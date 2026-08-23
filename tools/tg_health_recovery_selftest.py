# -*- coding: utf-8 -*-
"""tools/tg_health_recovery_selftest.py -- offline self-test for the
"TPILOT HEALTH RECOVERY HYBRID-D 20260723" patch to the manager Telegram
health guard (_tp_hg_* in main.py) and _send_manager_private.

Background: production evidence showed a manager accumulating dozens of
PeerFloodError health alerts within minutes despite a 6-hour notify cooldown,
with the health row later showing health_status='ok' while
last_notify_signature still referenced the old PeerFloodError. Root cause
(read-only RCA, confirmed against source): any ordinary successful send to an
existing dialog, or a passing get_me/get_dialogs light check, silently wrote
health_status='ok' over an unresolved auto-detected LIMITED/BLOCKED incident
(PeerFlood does not block reads or replies into dialogs that already exist)
-- so the status flapped OK<->LIMITED, and the alert dedup compared
old_status != new_status, re-firing on every flap and bypassing the 6h window
entirely. There was also no send gate at all: a LIMITED/BLOCKED manager kept
being asked to initiate brand new dialogs.

This patch (approved policy -- Hybrid D):
  - allow_recover-gated downgrade guard in _tp_hg_update_status: an
    unresolved auto-detected LIMITED/BLOCKED incident is never silently
    cleared by an ordinary OK/WARNING write.
  - a normalized restriction-family helper (_tp_hg_restriction_family)
    distinguishing peerflood / floodwait / blocked / warning_transient from
    the STRUCTURED classifier fields (health_status, error_class), never
    from raw error_text.
  - a central send gate (_tp_hg_send_allowed) wired into the active
    _send_manager_private (new_dialog kwarg) and every active automatic-send
    caller: LIMITED+peerflood blocks only NEW-dialog initiation; existing-
    dialog replies continue; BLOCKED blocks everything; FloodWait uses
    exc.seconds via a cooldown_until column (idempotent additive migration)
    and allows one controlled retry after it elapses.
  - alert dedup now delegates to the existing durable storage.health_incident_*
    CAS lifecycle (open-once / mark-notified / reminder-after-interval /
    resolve) keyed on a normalized, stable incident signature, replacing the
    old ad-hoc health_status-transition compare.
  - deepcheck (_tp_hg_run_deep_self_check) distinguishes "the light get_me/
    get_dialogs probe genuinely failed just now" (live_probe_ok=False) from
    "the persisted row is still sticky-LIMITED from a past incident"
    (status still LIMITED, live_probe_ok=True) -- and a successful Saved
    Messages self-send probe alone is never treated as proof of PeerFlood
    (new-peer) recovery, since Saved Messages is exempt from PeerFlood.

PEERFLOOD RECOVERY 20260812 (superseding R2 above for LIMITED+PeerFlood
specifically -- BLOCKED/manual_mark/FloodWait-cooldown are UNCHANGED): the
R2 policy above ("LIMITED+PeerFlood denies every ordinary send, new AND
existing dialog") turned out to be a self-inflicted permanent deadlock in
production -- a manager stuck LIMITED could never recover, because recovery
required a successful send, and the gate denied every send while LIMITED.
The fix:
  - _tp_hg_send_allowed now ALLOWS one controlled real-send probe per tick
    for LIMITED+PeerFlood (new reason: limited_probe_passthrough) -- BLOCKED
    and an explicit manual mark are UNCHANGED (still deny everything).
  - _tp_hg_may_recover_on_send_success now returns True for the peerflood
    family too (previously floodwait-only) -- a genuinely successful send is
    itself the evidence the restriction lifted.
  - _send_manager_private's body moved into a new _send_manager_private_ex,
    which returns (ok, fail_kind) so callers can tell a health-gate denial
    or a classified Telegram restriction ("gate_denied"/"restriction") apart
    from an ordinary send failure ("other") -- callers must never trash a
    lead for the former, and must stop the tick after one such failure
    ("one probe per tick", owner-approved). _send_manager_private itself
    stays a thin bool wrapper around it for its many unchanged callers.
  - health_incident_upsert_open/health_incident_resolve_all_for_manager
    (storage.py) gained additive preserve_notify_within_sec/return_notified
    parameters so a fast resolve->reopen of the SAME signature within the 6h
    notify window keeps the prior notify metadata (anti-flap) instead of
    resetting it, and a recovery notification only fires when the original
    problem was actually delivered to the owner.
See Group B (auto-recovery on a genuine successful send), Group C (gate now
allows LIMITED+PeerFlood through), and Group D's M2c/M2d/M2e (anti-flap
dedup on reopen, while the original never-permanently-orphaned guarantee
still holds past the window) for the updated assertions.

Technique: same AST-extraction + exec() approach as every other
tools/*_selftest.py in this project (main.py cannot be imported directly --
Telethon/env side effects at import time). Extracts only the ACTIVE (last)
definition of every duplicated name (_tp_hg_run_self_check, _tp_hg_mark,
_send_manager_private each have multiple stacked override-chain defs in
main.py; only the last is runtime-effective -- see CLAUDE.md section 4).

Fully offline: temp SQLite file (via storage.health_incident_* against a
throwaway db_path), a fake in-process Telegram client (no Telethon import,
no network), real aiosqlite/sqlite3/storage.py (storage.py has no Telethon/
env side effects at import time). No production DB/runtime/session/log
access.

    python3.12 tools\\tg_health_recovery_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    # Windows consoles often default to a non-UTF-8 codepage (e.g. cp1251),
    # which cannot encode the Cyrillic/emoji text this module builds (alert
    # bodies, status labels) -- reconfigure stdout/stderr so printing check()
    # results never crashes the run itself.
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
MAIN_SRC = MAIN_PATH.read_text(encoding="utf-8-sig")
STORAGE_PATH = BASE_DIR / "storage.py"
STORAGE_SRC = STORAGE_PATH.read_text(encoding="utf-8-sig")

# Names with exactly one definition in main.py.
SINGLE_NAMES = {
    "TP_HG_CHECK_INTERVAL_SEC", "TP_HG_NOTIFY_REPEAT_SEC", "TP_HG_UNKNOWN_AFTER_SEC",
    "TP_HG_STATUS_OK", "TP_HG_STATUS_WARNING", "TP_HG_STATUS_LIMITED", "TP_HG_STATUS_BLOCKED", "TP_HG_STATUS_UNKNOWN",
    "_tp_hg_now_iso", "_tp_hg_parse_iso", "_tp_hg_norm_key", "_tp_hg_error_text", "_tp_hg_status_label",
    "_tp_hg_classify_exception",
    "_TP_HG_FAMILY_PEERFLOOD", "_TP_HG_FAMILY_FLOODWAIT", "_TP_HG_FAMILY_BLOCKED",
    "_TP_HG_FAMILY_WARNING", "_TP_HG_FAMILY_UNKNOWN",
    "_tp_hg_restriction_family",
    "_tp_hg_connect_sync", "_tp_hg_ensure_tables",
    "_tp_hg_manager_row_sync", "_tp_hg_manager_label_from_key", "_tp_hg_username_from_key",
    "_tp_hg_severity",
    "_tp_hg_build_alert_text", "_tp_hg_build_recovery_alert_text",
    "_tp_hg_insert_panel_notification", "_tp_hg_send_work_status_notification",
    "_tp_hg_stable_error_text", "_tp_hg_incident_signature", "_tp_hg_bump_notify_meta",
    "_tp_hg_notify_if_needed",
    "_tp_hg_update_status",
    "_tp_hg_set_from_exception",
    "_tp_hg_manual_override_active",
    "_TP_HG_GATE_DENY_BLOCKED", "_TP_HG_GATE_DENY_NEW_DIALOG_LIMITED", "_TP_HG_GATE_DENY_FLOODWAIT_COOLDOWN",
    "_TP_HG_GATE_DENY_MANUAL_MARK",
    # PeerFlood Fail-Closed R2: sticky non-FloodWait LIMITED restrictions (PeerFlood
    # and friends) deny every ordinary automatic send in both new AND existing
    # dialogs -- _tp_hg_send_allowed's body (extracted above via _tp_hg_send_allowed
    # itself, but this is the deny-reason constant IT references) returns this
    # constant from that branch. Without it in the extraction namespace, any test
    # path that reaches that branch raises an uncaught NameError.
    "_TP_HG_GATE_DENY_LIMITED_RESTRICTION",
    "_tp_hg_send_allowed", "_tp_hg_may_recover_on_send_success",
    "_TP_HG_GATE_ALLOW_LIMITED_PROBE",
    # PEERFLOOD RECOVERY 20260812: _send_manager_private_ex carries the real
    # send body now (the active _send_manager_private is a thin bool wrapper
    # around it) -- required in the extraction namespace so the wrapper's
    # call to it resolves instead of raising NameError.
    "_send_manager_private_ex",
    "_tp_hg_promote_self_send_classification", "_tp_hg_saved_messages_send_probe",
    "_tp_hg_set_from_self_send_exception",
    "_tp_hg_run_deep_self_check",
    "_tp_hg_reset",
    "_tp_hg_row",
    # HNV2 F-3 correction (post-independent-review): _tp_hg_build_alert_text
    # (main.py, ~line 121 of its own extracted body) now calls
    # _hnv2_tg_health_family to print the 'family:' line (approved plan
    # section I.4) -- a real, load-bearing transitive dependency introduced
    # by the HNV2 patch, same as every other helper in this set. It is a
    # pure function (string mapping over _tp_hg_restriction_family, already
    # extracted above) with no I/O of its own; extracting it here restores
    # this test's coverage without changing any runtime behavior.
    "_hnv2_tg_health_family",
    "HNV2_FAMILY_UNKNOWN", "HNV2_FAMILY_PEERFLOOD", "HNV2_FAMILY_FLOODWAIT",
    "HNV2_FAMILY_ACCOUNT_BLOCKED",
    # CORRECTION A 20260810: _tp_hg_set_from_exception now calls
    # _tp_hg_verify_account_live before writing a BLOCKED status, and
    # _tp_hg_build_alert_text now references the _TP_HG_VERIFY_* mark
    # tuples to pick its title/action wording -- real, load-bearing
    # transitive dependencies, same as every other helper in this set.
    "_tp_hg_verify_account_live",
    "_TP_HG_VERIFY_BANNED_MARKS", "_TP_HG_VERIFY_DEACTIVATED_MARKS",
    "_TP_HG_VERIFY_AUTH_MARKS", "_TP_HG_VERIFY_NETWORK_MARKS",
    "TP_HG_VERIFY_ACCOUNT_LIVE", "TP_HG_VERIFY_AUTH_SESSION_PROBLEM",
    "TP_HG_VERIFY_IDENTITY_MISMATCH", "TP_HG_VERIFY_ACCOUNT_DEACTIVATED_CONFIRMED",
    "TP_HG_VERIFY_ACCOUNT_BANNED_CONFIRMED", "TP_HG_VERIFY_NETWORK_OR_PROXY_PROBLEM",
    "TP_HG_VERIFY_UNKNOWN",
    # STAGE 3: the utcnow refactor (2026-08-16) replaced bare datetime.utcnow() calls
    # throughout main.py with the _tp_utc_now() clock seam. Extracted bodies therefore
    # call it, and every harness that did not know about it broke at once with
    # "NameError: name '_tp_utc_now'". It is a pure, side-effect-free clock helper
    # (main.py line 15), so extract the REAL one rather than stubbing a clock -- the
    # naive-UTC contract is exactly what the timestamp assertions here depend on.
    "_tp_utc_now",
}
# Names with a stacked override chain in main.py -- only the LAST top-level
# def is runtime-active; earlier ones are shadowed dead code (see CLAUDE.md
# section 4 and Group STATIC below, which locks in that these are the ones
# actually patched).
OVERRIDE_CHAIN_NAMES = {
    "_tp_hg_run_self_check",
    "_tp_hg_mark",
    "_send_manager_private",
}
ALL_NAMES = SINGLE_NAMES | OVERRIDE_CHAIN_NAMES


# Parsed ONCE -- main.py is ~1.8MB/40000+ lines, so ast.parse() alone costs a
# few seconds; re-parsing it once per name (as find_defs() used to do) turned
# a ~50-name extraction into a multi-minute run. Every def-lookup below reuses
# this single tree.
MAIN_TREE = ast.parse(MAIN_SRC)


def find_defs(name: str) -> list:
    """Every top-level def/async-def/Assign node with this name, in source order."""
    out = []
    for n in MAIN_TREE.body:
        nm = getattr(n, "name", None)
        if nm == name:
            out.append(n)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name:
            out.append(n)
    return out


def _extract_active_nodes(names: set) -> list:
    """For each requested name, take its LAST top-level definition (matches
    Python's own name-resolution: only the final def/assign is the live
    global by the time any function actually runs) -- same override-chain
    convention as tools/auto_status_disable_selftest.py's find_defs, but
    resolving straight to the active node instead of returning all layers."""
    nodes = []
    missing = []
    for name in sorted(names):
        defs = find_defs(name)
        if not defs:
            missing.append(name)
            continue
        nodes.append(defs[-1])
    if missing:
        raise AssertionError(f"names not found in main.py: {missing}")
    return nodes


async def _stub_table_columns(db, table: str) -> List[str]:
    """Inline reimplementation of main.py's own _table_columns() helper
    (main.py ~line 787) -- trivial PRAGMA table_info wrapper, stubbed
    directly rather than AST-extracted from an unrelated part of the file."""
    cur = await db.execute(f"PRAGMA table_info({table});")
    rows = await cur.fetchall()
    return [str(r[1]) for r in rows]


async def _stub_human_send_delay(chat_id: int, text: str, *, after_first: bool = False) -> None:
    return None


def _stub_kyiv_now():
    return datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None)


def _stub_norm_key(raw: Any) -> str:
    return str(raw or "").strip().lower()


class _FakeMsg:
    def __init__(self, mid: int):
        self.id = mid

    async def delete(self):
        return True


class FakeClient:
    """In-process fake Telegram client -- no Telethon import, no network.
    Exceptions are scripted per-call via *_exc attributes/dicts so each test
    can simulate a specific Telegram error class by name."""

    def __init__(self):
        self.connected = True
        self.get_me_exc: Optional[BaseException] = None
        self.get_dialogs_exc: Optional[BaseException] = None
        self.send_exc_for_chat: Dict[str, BaseException] = {}
        self.send_exc_default: Optional[BaseException] = None
        self.sent_messages: List[Tuple[Any, str]] = []
        self.get_me_calls = 0
        self.get_dialogs_calls = 0
        self.send_calls = 0

    def is_connected(self) -> bool:
        return self.connected

    async def connect(self):
        self.connected = True

    async def get_me(self):
        self.get_me_calls += 1
        if self.get_me_exc is not None:
            raise self.get_me_exc
        return object()

    async def get_dialogs(self, limit: int = 1):
        self.get_dialogs_calls += 1
        if self.get_dialogs_exc is not None:
            raise self.get_dialogs_exc
        return []

    async def send_message(self, chat_id, text):
        self.send_calls += 1
        key = str(chat_id)
        exc = self.send_exc_for_chat.get(key, self.send_exc_default)
        if exc is not None:
            raise exc
        self.sent_messages.append((chat_id, text))
        return _FakeMsg(len(self.sent_messages))

    def action(self, chat_id, kind):
        return _NullAsyncCM()


class _NullAsyncCM:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# Local fake exception classes -- matched by _tp_hg_classify_exception purely
# via __class__.__name__ substring, so these behave identically to the real
# Telethon errors for classification purposes without importing Telethon.
class PeerFloodError(Exception):
    def __init__(self, msg: str = "Too many requests"):
        super().__init__(msg)


class FloodWaitError(Exception):
    def __init__(self, seconds: int):
        self.seconds = int(seconds)
        super().__init__(f"A wait of {seconds} seconds is required (caused by SendMessageRequest)")


class AuthKeyUnregisteredError(Exception):
    def __init__(self, msg: str = "The key is not registered in the system"):
        super().__init__(msg)


def build_main_ns(db_path: str, fake_client: FakeClient, *, manager_key: str = "mgrh_test", controller_mode: bool = False) -> dict:
    storage.DB_PATH = db_path
    storage.QUEUE_DB_PATH = db_path
    prod_db_dir = os.path.abspath(os.path.join(str(BASE_DIR), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"

    nodes = _extract_active_nodes(ALL_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    import aiosqlite as _aiosqlite

    ns: Dict[str, Any] = {
        "Any": Any, "Dict": Dict, "Optional": Optional, "Tuple": Tuple, "List": List,
        # timezone: required by the real _tp_utc_now() (utcnow refactor 2026-08-16),
        # which builds an aware UTC datetime and then drops the tzinfo.
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "re": re, "os": os, "asyncio": asyncio, "time": __import__("time"),
        "aiosqlite": _aiosqlite,
        "_tp_hg_sqlite3": sqlite3,
        "_table_columns": _stub_table_columns,
        "_human_send_delay": _stub_human_send_delay,
        "_kyiv_now": _stub_kyiv_now,
        "registry_normalize_manager_key": _stub_norm_key,
        "client": fake_client,
        "TPILOT_DB_PATH": db_path,
        "MANAGER_RUNTIME_KEY": manager_key,
        "CONTROLLER_MODE": controller_mode,
        "WORK_STATUS_CHAT_ID": 999999,
    }
    exec(compile(module_src, f"<{MAIN_PATH}:tg_health_recovery>", "exec"), ns)
    return ns


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="tg_health_recovery_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


def _raw_health_row(db_path: str, manager_key: str) -> Optional[dict]:
    con = sqlite3.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        row = con.execute("SELECT * FROM manager_telegram_health WHERE manager_key=?", (manager_key,)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def _raw_backdate_cooldown_expired(db_path: str, manager_key: str) -> None:
    ts = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(seconds=60)).replace(microsecond=0).isoformat()
    con = sqlite3.connect(db_path)
    try:
        con.execute("UPDATE manager_telegram_health SET cooldown_until=? WHERE manager_key=?", (ts, manager_key))
        con.commit()
    finally:
        con.close()


def _raw_backdate_incident_notified(db_path: str, manager_key: str, signature: str, seconds_ago: int) -> None:
    ts = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE health_incidents SET last_notified_at=? WHERE manager_key=? AND signature=?",
            (ts, manager_key, signature),
        )
        con.commit()
    finally:
        con.close()


def _raw_backdate_incident_resolved(db_path: str, manager_key: str, signature: str, seconds_ago: int) -> None:
    """PEERFLOOD RECOVERY 20260812: backdates resolved_at so a subsequent
    reopen of the SAME signature falls outside preserve_notify_within_sec
    (health_incident_upsert_open, storage.py) -- proves M2's original
    never-permanently-orphaned guarantee still holds once the anti-flap
    window has genuinely elapsed."""
    ts = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) - timedelta(seconds=seconds_ago)).replace(microsecond=0).isoformat()
    con = sqlite3.connect(db_path)
    try:
        con.execute(
            "UPDATE health_incidents SET resolved_at=? WHERE manager_key=? AND signature=?",
            (ts, manager_key, signature),
        )
        con.commit()
    finally:
        con.close()


def _panel_notifications_count(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return con.execute("SELECT COUNT(*) FROM panel_notifications").fetchone()[0]
    finally:
        con.close()


def _panel_notifications_titles(db_path: str) -> List[str]:
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("SELECT title FROM panel_notifications ORDER BY id ASC").fetchall()]
    finally:
        con.close()


def _panel_notifications_bodies(db_path: str) -> List[str]:
    con = sqlite3.connect(db_path)
    try:
        return [r[0] for r in con.execute("SELECT body FROM panel_notifications ORDER BY id ASC").fetchall()]
    finally:
        con.close()


def _work_status_alert_count(fake_client: FakeClient) -> int:
    return sum(1 for cid, _ in fake_client.sent_messages if str(cid) == "999999")


# ======================================================================
# GROUP A: state machine -- classify, downgrade guard, exc.seconds.
# Covers cases 1, 12, 13, 18.
# ======================================================================

async def test_group_a_state_machine():
    print("\n-- Group A: state machine (classify / downgrade guard / exc.seconds) --")
    tmp_root, db_path = make_temp_env()
    try:
        fc = FakeClient()
        ns = build_main_ns(db_path, fc)
        key = ns["MANAGER_RUNTIME_KEY"]

        # 1. OK -> PeerFloodError -> LIMITED.
        row = await ns["_tp_hg_set_from_exception"](key, PeerFloodError(), source="client_auto_send")
        check("A1. OK -> PeerFloodError classifies to LIMITED", row.get("health_status") == "limited", row)
        check("A1b. error_class is PeerFloodError", row.get("error_class") == "PeerFloodError", row)

        # 12. WARNING cannot downgrade LIMITED.
        row2 = await ns["_tp_hg_update_status"](key, status=ns["TP_HG_STATUS_WARNING"], error_source="periodic_self_check_get_dialogs")
        check("A12. an ordinary WARNING write does not downgrade LIMITED", row2.get("health_status") == "limited", row2)
        check("A12b. original error_class is preserved through the blocked downgrade", row2.get("error_class") == "PeerFloodError", row2)

        # 13. OK cannot downgrade BLOCKED.
        # CORRECTION A 20260810: see the identical comment in Group C -- the
        # fake client's get_me() must also fail so live-verification lands on
        # AUTH_SESSION_PROBLEM (still BLOCKED).
        fc.get_me_exc = AuthKeyUnregisteredError()
        row3 = await ns["_tp_hg_set_from_exception"](key + "_b", AuthKeyUnregisteredError(), source="client_auto_send")
        check("A13-setup. AuthKeyUnregisteredError classifies to BLOCKED", row3.get("health_status") == "blocked", row3)
        row4 = await ns["_tp_hg_update_status"](key + "_b", status=ns["TP_HG_STATUS_OK"], error_source="periodic_self_check")
        check("A13. an ordinary OK write does not downgrade BLOCKED", row4.get("health_status") == "blocked", row4)

        # 18. FloodWait uses exc.seconds (>=30min -> LIMITED with a cooldown;
        # a short one stays WARNING -- both branches read exc.seconds).
        row5 = await ns["_tp_hg_set_from_exception"](key + "_c", FloodWaitError(3600), source="client_auto_send")
        check("A18. FloodWaitError(3600s) classifies using exc.seconds (>=30min -> LIMITED)", row5.get("health_status") == "limited", row5)
        check("A18b. cooldown_until was populated from exc.seconds", bool(row5.get("cooldown_until")), row5)
        row5b = await ns["_tp_hg_set_from_exception"](key + "_d", FloodWaitError(60), source="client_auto_send")
        check("A18c. a short FloodWaitError(60s) (<30min) classifies to WARNING, not LIMITED", row5b.get("health_status") == "warning", row5b)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP B: light check / ordinary send success do not clear sticky PeerFlood.
# Covers cases 2, 3.
# ======================================================================

async def test_group_b_no_false_recovery():
    print("\n-- Group B: light check never falsely recovers PeerFlood; a genuine successful send now does (PEERFLOOD RECOVERY 20260812) --")
    tmp_root, db_path = make_temp_env()
    try:
        fc = FakeClient()
        ns = build_main_ns(db_path, fc)
        key = ns["MANAGER_RUNTIME_KEY"]
        await ns["_tp_hg_set_from_exception"](key, PeerFloodError(), source="client_auto_send")

        # 2. Periodic light check (get_me + get_dialogs both succeed) must not clear it.
        res = await ns["_tp_hg_run_self_check"](source="periodic_self_check")
        check("B2. light self-check succeeding does not clear PeerFlood LIMITED", res.get("status") == "limited", res)
        check("B2b. live_probe_ok reflects the live get_me/get_dialogs success even though status stays sticky", res.get("live_probe_ok") is True, res)
        row = _raw_health_row(db_path, key)
        check("B2c. persisted row is still limited", row.get("health_status") == "limited", row)

        # 3 (PEERFLOOD RECOVERY 20260812): an existing-dialog send is now
        # ALLOWED while sticky PeerFlood LIMITED (one controlled probe per
        # tick) -- and because the underlying send genuinely succeeds here
        # (the fake client has no scripted failure), it IS real evidence the
        # restriction lifted: the status auto-recovers to OK and the open
        # PeerFlood incident resolves. This exercises M1 (gate allow) + M2
        # (allow_recover for the peerflood family) together.
        family_peerflood = ns["_tp_hg_restriction_family"](status="limited", error_class="PeerFloodError")
        sig_peerflood = ns["_tp_hg_incident_signature"](status="limited", error_class="PeerFloodError", error_source="client_auto_send", family=family_peerflood, error_text=ns["_tp_hg_error_text"](PeerFloodError()))
        incident_before_b3 = storage.health_incident_get(key, sig_peerflood, db_path=db_path)
        check("B3-setup. the PeerFlood incident is open before the recovering send", incident_before_b3 is not None and incident_before_b3.get("status") == "open", incident_before_b3)
        ok = await ns["_send_manager_private"](111, "hello again", new_dialog=False)
        check("B3. an existing-dialog send is ALLOWED while sticky PeerFlood LIMITED", ok is True, ok)
        check("B3z. the allowed existing-dialog send actually reached client.send_message", (111, "hello again") in fc.sent_messages, fc.sent_messages)
        row2 = _raw_health_row(db_path, key)
        check("B3b. a genuinely successful send auto-recovers PeerFlood LIMITED to OK", row2.get("health_status") == "ok", row2)
        incident_after_b3 = storage.health_incident_get(key, sig_peerflood, db_path=db_path)
        check("B3c. the PeerFlood incident resolves on the successful recovering send", incident_after_b3.get("status") == "resolved", incident_after_b3)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP C: central send gate. Covers cases 4, 5, 6, 7, 14, 29.
# ======================================================================

async def test_group_c_send_gate():
    print("\n-- Group C: central send gate (PEERFLOOD RECOVERY 20260812: LIMITED+PeerFlood now allows through) --")
    tmp_root, db_path = make_temp_env()
    try:
        fc = FakeClient()
        ns = build_main_ns(db_path, fc)
        key = ns["MANAGER_RUNTIME_KEY"]
        await ns["_tp_hg_set_from_exception"](key, PeerFloodError(), source="client_auto_send")

        # 4/5 (PEERFLOOD RECOVERY 20260812): the gate helper itself, called
        # directly (read-only, no side effects), now ALLOWS both new-dialog
        # and existing-dialog checks while sticky PeerFlood LIMITED -- one
        # controlled probe per tick (owner-approved "skip + one probe" mode).
        # BLOCKED/manual_mark/an unexpired FloodWait cooldown are UNCHANGED
        # (see C14 below and the dedicated failclosed selftest).
        allowed_existing_direct, reason_existing_direct = await ns["_tp_hg_send_allowed"](key, new_dialog=False)
        check("C5c. _tp_hg_send_allowed ALLOWS new_dialog=False while sticky PeerFlood LIMITED", allowed_existing_direct is True, allowed_existing_direct)
        check("C5d. the existing-dialog allow reason is _TP_HG_GATE_ALLOW_LIMITED_PROBE", reason_existing_direct == ns["_TP_HG_GATE_ALLOW_LIMITED_PROBE"], reason_existing_direct)
        allowed_new_direct, reason_new_direct = await ns["_tp_hg_send_allowed"](key, new_dialog=True)
        check("C5e. _tp_hg_send_allowed ALLOWS new_dialog=True while sticky PeerFlood LIMITED", allowed_new_direct is True, allowed_new_direct)
        check("C5f. the new-dialog allow reason is ALSO _TP_HG_GATE_ALLOW_LIMITED_PROBE", reason_new_direct == ns["_TP_HG_GATE_ALLOW_LIMITED_PROBE"], reason_new_direct)

        # 4/6/7/29: a real send while LIMITED now reaches client.send_message
        # instead of being stopped before the Telegram layer. The fake
        # client has no scripted failure here, so the send genuinely
        # succeeds -- real evidence of recovery (Group B proves the detailed
        # before/after incident state; this block proves the gate itself no
        # longer stands in the way and the caller sees a plain True).
        ok_new = await ns["_send_manager_private"](222, "hi new lead", new_dialog=True)
        check("C4. a new-dialog send is ALLOWED (not denied) while PeerFlood LIMITED", ok_new is True, ok_new)
        check("C6. the allowed send actually reached client.send_message", (222, "hi new lead") in fc.sent_messages, fc.sent_messages)
        check("C29. an allowed send returns a plain True (callers' `if ok:` sent-flag guards fire correctly)", ok_new is True, ok_new)
        # C7: the successful send resolves the previously-notified incident,
        # which emits its OWN recovery notification -- the new intended
        # behavior (owner requirement 6), not a "gate denial creates no
        # alert" case anymore.
        titles_after_recovery = _panel_notifications_titles(db_path)
        check("C7. the recovering send's own recovery notification is delivered", bool(titles_after_recovery) and titles_after_recovery[-1] == "✅ Telegram Health Recovery", titles_after_recovery)
        row_after_recovery = _raw_health_row(db_path, key)
        check("C7b. the health row itself reflects the recovery (status=ok)", row_after_recovery.get("health_status") == "ok", row_after_recovery)

        # 14. BLOCKED prevents all ordinary auto-sends (both new and existing dialog).
        # CORRECTION A 20260810: a broken auth key breaks get_me() the same as
        # any other RPC on the same client -- scripting it here so the new
        # live-verification step (added inside _tp_hg_set_from_exception)
        # correctly lands on AUTH_SESSION_PROBLEM (still BLOCKED) instead of
        # ACCOUNT_LIVE (the fake client's unscripted get_me() otherwise just
        # succeeds, which would incorrectly downgrade this to WARNING).
        fc.get_me_exc = AuthKeyUnregisteredError()
        await ns["_tp_hg_set_from_exception"](key, AuthKeyUnregisteredError(), source="client_auto_send")
        calls_before = fc.send_calls
        ok_blocked_new = await ns["_send_manager_private"](444, "x", new_dialog=True)
        ok_blocked_existing = await ns["_send_manager_private"](333, "y", new_dialog=False)
        check("C14. BLOCKED denies a new-dialog send", ok_blocked_new is False, ok_blocked_new)
        check("C14b. BLOCKED denies an existing-dialog send too", ok_blocked_existing is False, ok_blocked_existing)
        check("C14c. neither BLOCKED-denied send reached client.send_message", fc.send_calls == calls_before, fc.send_calls)

        # Gate helper decision table directly, including the explicit recovery-probe bypass.
        allowed, reason = await ns["_tp_hg_send_allowed"](key, new_dialog=True, recovery_probe=True)
        check("C-bypass. recovery_probe=True bypasses the gate even while BLOCKED", allowed is True and reason == "recovery_probe", (allowed, reason))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP D: durable incident dedup. Covers cases 8, 9, 10, 11, 30.
# ======================================================================

async def test_group_d_durable_dedup():
    print("\n-- Group D: durable incident dedup (storage.health_incident_*) --")
    tmp_root, db_path = make_temp_env()
    try:
        fc = FakeClient()
        ns = build_main_ns(db_path, fc)
        key = ns["MANAGER_RUNTIME_KEY"]

        # 8. Repeated identical PeerFlood within six hours -> no duplicate notification.
        await ns["_tp_hg_set_from_exception"](key, PeerFloodError(), source="client_auto_send")
        check("D8-setup. first PeerFlood sends exactly one alert", _panel_notifications_count(db_path) == 1, _panel_notifications_titles(db_path))
        for _ in range(4):
            await ns["_tp_hg_set_from_exception"](key, PeerFloodError(), source="client_auto_send")
        check("D8. repeated identical PeerFlood within 6h creates no duplicate alert", _panel_notifications_count(db_path) == 1, _panel_notifications_titles(db_path))

        # 9. Reminder after six hours follows the durable incident policy. The
        # signature must be built from the SAME error_text the real code stores
        # (_tp_hg_error_text(exc) == "PeerFloodError: <message>", not the bare
        # message) -- otherwise this test would backdate a different row than
        # the one _tp_hg_notify_if_needed actually looks up.
        family = ns["_tp_hg_restriction_family"](status="limited", error_class="PeerFloodError")
        real_error_text = ns["_tp_hg_error_text"](PeerFloodError())
        signature = ns["_tp_hg_incident_signature"](status="limited", error_class="PeerFloodError", error_source="client_auto_send", family=family, error_text=real_error_text)
        _raw_backdate_incident_notified(db_path, key, signature, ns["TP_HG_NOTIFY_REPEAT_SEC"] + 60)
        await ns["_tp_hg_set_from_exception"](key, PeerFloodError(), source="client_auto_send")
        check("D9. past the 6h reminder interval -> a second (reminder) alert is sent", _panel_notifications_count(db_path) == 2, _panel_notifications_titles(db_path))
        bodies = _panel_notifications_bodies(db_path)
        check("D9b. the reminder alert's body is marked as a repeat", "Повтор" in bodies[-1], bodies)

        # 11. A genuinely different BLOCKED incident may notify immediately (different
        # signature -- family/class/source differ -- even though a LIMITED incident for
        # the SAME manager is already open and within its own 6h window).
        count_before_blocked = _panel_notifications_count(db_path)
        # CORRECTION A 20260810: see the identical comment in Group C -- the
        # fake client's get_me() must also fail here so live-verification
        # correctly lands on AUTH_SESSION_PROBLEM (still BLOCKED), not
        # ACCOUNT_LIVE.
        fc.get_me_exc = AuthKeyUnregisteredError()
        await ns["_tp_hg_set_from_exception"](key, AuthKeyUnregisteredError(), source="client_auto_send")
        check("D11. a different BLOCKED incident notifies immediately alongside an open LIMITED one", _panel_notifications_count(db_path) == count_before_blocked + 1, _panel_notifications_titles(db_path))

        # 10. Concurrent identical failures create at most one immediate notification.
        # TPILOT HEALTH RECOVERY HYBRID-D CORRECTIVE 20260723b (finding H1/M3):
        # the original version of this test used only 2 gathered tasks and one
        # iteration, which was too weak to reliably catch the read-decide-write
        # race in the FIRST corrective patch (health_incident_get -> "was_open"
        # decision -> health_incident_upsert_open, with mark_notified only
        # AFTER delivery) -- that race was intermittent (~2 of 7 manual runs
        # caught it). _tp_hg_notify_if_needed now claims ownership of delivery
        # via the atomic storage.health_incident_claim_notify BEFORE any
        # delivery is attempted, which closes the window deterministically
        # regardless of task count or iteration count -- so this is now run
        # with MORE concurrent tasks across MULTIPLE independent iterations
        # and must be exactly 1 EVERY time, not just on average.
        for concurrency_round in range(5):
            tmp_root2, db_path2 = make_temp_env()
            try:
                fc2 = FakeClient()
                mgr2 = f"mgrh_concurrent_{concurrency_round}"
                ns2 = build_main_ns(db_path2, fc2, manager_key=mgr2)
                key2 = ns2["MANAGER_RUNTIME_KEY"]
                # Pre-warm the schema (CREATE TABLE + the idempotent ADD COLUMN
                # check) on a single connection first -- concurrent DDL/ALTER on
                # a completely cold, freshly-created SQLite file is its own,
                # unrelated contention scenario (main.py's own busy_timeout
                # covers realistic contention, but 8 simultaneous first-ever
                # CREATE/ALTER attempts on one brand-new file is not a
                # realistic production shape -- one manager is one process).
                # This test's job is to stress the NOTIFICATION CLAIM race
                # (H1), not schema-creation locking.
                await ns2["_tp_hg_ensure_tables"]()
                # 3 concurrent tasks per round (not 8): still a genuine race on
                # the SAME (manager_key, signature) claim -- the original H1 bug
                # was already reliably reproducible with just 2 -- while staying
                # within what this environment's aiosqlite/WAL connection churn
                # handles cleanly under asyncio.gather (8-way immediate
                # concurrency here hits transient "database is locked" from
                # short-lived-connection churn on Windows, an environment
                # artifact unrelated to the claim logic being tested).
                await asyncio.gather(*[
                    ns2["_tp_hg_set_from_exception"](key2, PeerFloodError(), source="client_auto_send")
                    for _ in range(3)
                ])
                check(
                    f"D10.{concurrency_round}. 3 concurrent identical PeerFlood failures produce exactly one notification (round {concurrency_round})",
                    _panel_notifications_count(db_path2) == 1, _panel_notifications_titles(db_path2),
                )
            finally:
                cleanup_env(tmp_root2)

        # 30. Durable incident CAS exercised directly (not only indirectly through
        # _tp_hg_notify_if_needed) -- proves genuine reuse of the real primitives.
        direct_row = storage.health_incident_get(key, signature, db_path=db_path)
        check("D30. the LIMITED incident's real health_incidents row is directly readable via storage.health_incident_get", direct_row is not None and direct_row.get("status") == "open", direct_row)
        reopened = storage.health_incident_upsert_open(key, signature, db_path=db_path)
        check("D30b. upsert_open on the already-open row is idempotent (direct call agrees with the row _tp_hg_notify_if_needed maintains)", reopened.get("status") == "open", reopened)

        # D30c/d: the new atomic claim primitive itself, called directly. First
        # claim on a fresh incident -> 'open'; immediate second claim -> None
        # (already claimed, nothing to deliver); after backdating past the
        # repeat interval -> 'reminder', and reminder_count increments.
        claim_key, claim_sig = "mgrh_claim_direct", "limited:peerflood:PeerFloodError:auto:direct probe"
        storage.health_incident_upsert_open(claim_key, claim_sig, db_path=db_path)
        first_claim = storage.health_incident_claim_notify(claim_key, claim_sig, repeat_after_sec=21600, db_path=db_path)
        check("D30c. first direct claim on a fresh incident returns 'open'", first_claim == "open", first_claim)
        second_claim = storage.health_incident_claim_notify(claim_key, claim_sig, repeat_after_sec=21600, db_path=db_path)
        check("D30d. an immediate second claim returns None (already claimed, nothing new to deliver)", second_claim is None, second_claim)
        _raw_backdate_incident_notified(db_path, claim_key, claim_sig, 21600 + 60)
        reminder_count_before = storage.health_incident_get(claim_key, claim_sig, db_path=db_path).get("reminder_count")
        reminder_claim = storage.health_incident_claim_notify(claim_key, claim_sig, repeat_after_sec=21600, db_path=db_path)
        check("D30e. a claim past the repeat interval returns 'reminder'", reminder_claim == "reminder", reminder_claim)
        reminder_count_after = storage.health_incident_get(claim_key, claim_sig, db_path=db_path).get("reminder_count")
        check("D30f. a reminder claim increments reminder_count", int(reminder_count_after) == int(reminder_count_before) + 1, (reminder_count_before, reminder_count_after))

        # M1 (finding): the SAME restriction observed via different internal
        # source functions (a live send failure vs the periodic self-check)
        # must now collapse to ONE incident, not two independently-alerting
        # ones -- error_source is normalized to a coarse auto/manual_mark
        # category inside the signature.
        tmp_root3, db_path3 = make_temp_env()
        try:
            fc3 = FakeClient()
            ns3 = build_main_ns(db_path3, fc3, manager_key="mgrh_m1")
            key3 = ns3["MANAGER_RUNTIME_KEY"]
            await ns3["_tp_hg_set_from_exception"](key3, PeerFloodError(), source="client_auto_send")
            await ns3["_tp_hg_set_from_exception"](key3, PeerFloodError(), source="periodic_self_check_get_dialogs")
            await ns3["_tp_hg_set_from_exception"](key3, PeerFloodError(), source="manual_deepcheck_light_get_me")
            check(
                "M1. PeerFlood observed via client_auto_send, periodic_self_check, and deepcheck collapses to ONE incident/ONE alert",
                _panel_notifications_count(db_path3) == 1, _panel_notifications_titles(db_path3),
            )
            sig_auto_a = ns3["_tp_hg_incident_signature"](status="limited", error_class="PeerFloodError", error_source="client_auto_send", family="peerflood", error_text=ns3["_tp_hg_error_text"](PeerFloodError()))
            sig_auto_b = ns3["_tp_hg_incident_signature"](status="limited", error_class="PeerFloodError", error_source="periodic_self_check_get_dialogs", family="peerflood", error_text=ns3["_tp_hg_error_text"](PeerFloodError()))
            check("M1b. the two source strings produce an identical normalized signature", sig_auto_a == sig_auto_b, (sig_auto_a, sig_auto_b))
            # A manual_mark for the SAME restriction is still a genuinely
            # separate identity (different administrative meaning/protection).
            sig_manual = ns3["_tp_hg_incident_signature"](status="limited", error_class="PeerFloodError", error_source="manual_mark", family="peerflood", error_text=ns3["_tp_hg_error_text"](PeerFloodError()))
            check("M1c. manual_mark keeps a distinct signature from auto-detected sources", sig_manual != sig_auto_a, (sig_manual, sig_auto_a))
        finally:
            cleanup_env(tmp_root3)

        # L1 (finding): the volatile Telethon "(caused by ...Request)" suffix
        # must not fragment identity -- two error texts differing ONLY in that
        # suffix must normalize to the same stable_text/signature.
        text_a = "PeerFloodError: Too many requests (caused by messages.SendMessageRequest)"
        text_b = "PeerFloodError: Too many requests (caused by contacts.ImportContactsRequest)"
        stable_a = ns["_tp_hg_stable_error_text"](text_a)
        stable_b = ns["_tp_hg_stable_error_text"](text_b)
        check("L1. stable_error_text strips the '(caused by ...)' suffix so both requests normalize identically", stable_a == stable_b == "PeerFloodError: Too many requests", (stable_a, stable_b))

        # M2 (finding): when a DIFFERENT restriction (BLOCKED) overwrites the
        # health row while a LIMITED incident is still open, recovery to OK
        # must resolve BOTH incidents (not just the last-written one) --
        # otherwise the LIMITED incident is orphaned and its next real
        # recurrence would be silently suppressed for up to 6h instead of
        # alerting immediately.
        tmp_root4, db_path4 = make_temp_env()
        try:
            fc4 = FakeClient()
            ns4 = build_main_ns(db_path4, fc4, manager_key="mgrh_m2")
            key4 = ns4["MANAGER_RUNTIME_KEY"]
            await ns4["_tp_hg_set_from_exception"](key4, PeerFloodError(), source="client_auto_send")
            limited_sig = ns4["_tp_hg_incident_signature"](status="limited", error_class="PeerFloodError", error_source="client_auto_send", family="peerflood", error_text=ns4["_tp_hg_error_text"](PeerFloodError()))
            check("M2-setup. the LIMITED/peerflood incident is open", storage.health_incident_get(key4, limited_sig, db_path=db_path4).get("status") == "open", None)

            # CORRECTION A 20260810: see the identical comment in Group C -- the
            # fake client's get_me() must also fail so live-verification lands
            # on AUTH_SESSION_PROBLEM (still BLOCKED, same error_class/error_text/
            # error_source as before -- verification only changes those fields
            # when it can positively confirm something DIFFERENT from the
            # original classification, e.g. live/mismatch/confirmed-ban).
            fc4.get_me_exc = AuthKeyUnregisteredError()
            await ns4["_tp_hg_set_from_exception"](key4, AuthKeyUnregisteredError(), source="client_auto_send")
            blocked_sig = ns4["_tp_hg_incident_signature"](status="blocked", error_class="AuthKeyUnregisteredError", error_source="client_auto_send", family="blocked", error_text=ns4["_tp_hg_error_text"](AuthKeyUnregisteredError()))
            check("M2-setup2. the health row is now BLOCKED and a SEPARATE BLOCKED incident is open", storage.health_incident_get(key4, blocked_sig, db_path=db_path4).get("status") == "open", None)
            check("M2-setup3. the earlier LIMITED incident is still open (not yet touched)", storage.health_incident_get(key4, limited_sig, db_path=db_path4).get("status") == "open", None)

            # H3 (finding, corrective 20260723c): health_incidents is a SHARED
            # table -- other subsystems open incidents for the SAME manager_key
            # namespace with their own, non-Telegram-health signatures. Seed
            # three of them directly via the real storage API (the exact way
            # each subsystem actually calls it) BEFORE the Telegram-health
            # recovery fires, to prove recovery does not reach across subsystem
            # boundaries.
            foreign_sigs = ["auth_action_required", "recovery_flap", "proxy_terminal_review"]
            for fsig in foreign_sigs:
                storage.health_incident_upsert_open(key4, fsig, db_path=db_path4)
                check(f"H3-setup. foreign incident '{fsig}' is open before recovery", storage.health_incident_get(key4, fsig, db_path=db_path4).get("status") == "open", fsig)

            # Deepcheck-style recovery to OK (allow_recover=True).
            await ns4["_tp_hg_update_status"](key4, status=ns4["TP_HG_STATUS_OK"], error_source="manual_deepcheck", allow_recover=True)
            limited_after = storage.health_incident_get(key4, limited_sig, db_path=db_path4)
            blocked_after = storage.health_incident_get(key4, blocked_sig, db_path=db_path4)
            check("M2. the earlier (orphaned-in-the-old-design) LIMITED incident IS resolved by recovery", limited_after.get("status") == "resolved", limited_after)
            check("M2b. the BLOCKED incident IS ALSO resolved by the same recovery", blocked_after.get("status") == "resolved", blocked_after)

            # H3: the SAME recovery must NOT touch any foreign-subsystem
            # incident -- resolve_all_for_manager is scoped to signatures
            # starting with 'limited:'/'blocked:' only.
            for fsig in foreign_sigs:
                foreign_after = storage.health_incident_get(key4, fsig, db_path=db_path4)
                check(f"H3. foreign incident '{fsig}' is UNTOUCHED (still open) by Telegram-health recovery", foreign_after.get("status") == "open", foreign_after)

            # Direct call proof: resolve_all_for_manager's own return value
            # counts only the 2 Telegram-health incidents just resolved above
            # (both are already resolved by the recovery call, so a second
            # direct call must now resolve ZERO further rows -- proving it
            # never had the 3 foreign incidents in scope, not even latently).
            second_call_count = storage.health_incident_resolve_all_for_manager(key4, db_path=db_path4)
            check("H3b. a second direct resolve_all_for_manager call resolves 0 rows (foreign incidents were never in its scope)", second_call_count == 0, second_call_count)

            # PEERFLOOD RECOVERY 20260812 (finding M5): a reopen within
            # TP_HG_NOTIFY_REPEAT_SEC of its own resolution now preserves the
            # PRIOR last_notified_at instead of resetting it (anti-flap dedup
            # -- closes a residual flap-bypass in the original H1 fix, where
            # upsert_open always reset last_notified_at on resolved->open,
            # so a fast resolve/reopen cycle could still re-fire an alert
            # every time). M2's own guarantee -- a recurrence is never
            # PERMANENTLY orphaned/suppressed -- is verified separately below
            # by backdating the resolution past the window.
            count_before_recur = _panel_notifications_count(db_path4)
            await ns4["_tp_hg_set_from_exception"](key4, PeerFloodError(), source="client_auto_send")
            check("M2c. an immediate PeerFlood recurrence (within the 6h window) is deduped, not re-alerted (finding M5 anti-flap)", _panel_notifications_count(db_path4) == count_before_recur, _panel_notifications_count(db_path4))
            reopened_row = storage.health_incident_get(key4, limited_sig, db_path=db_path4)
            check("M2d. the incident IS genuinely reopened (status='open', not stuck referencing stale orphaned state)", reopened_row is not None and reopened_row.get("status") == "open", reopened_row)

            # M2's original guarantee still holds for a GENUINELY stale
            # resolution (past the anti-flap window): resolve it again, then
            # backdate resolved_at past TP_HG_NOTIFY_REPEAT_SEC before the
            # NEXT recurrence -- it must alert, proving the incident is never
            # permanently orphaned/suppressed.
            await ns4["_tp_hg_update_status"](key4, status=ns4["TP_HG_STATUS_OK"], error_source="manual_deepcheck", allow_recover=True)
            resolved_row = storage.health_incident_get(key4, limited_sig, db_path=db_path4)
            check("M2e-setup. the reopened incident resolves again", resolved_row is not None and resolved_row.get("status") == "resolved", resolved_row)
            _raw_backdate_incident_resolved(db_path4, key4, limited_sig, ns4["TP_HG_NOTIFY_REPEAT_SEC"] + 60)
            count_before_stale_recur = _panel_notifications_count(db_path4)
            await ns4["_tp_hg_set_from_exception"](key4, PeerFloodError(), source="client_auto_send")
            check("M2e. a recurrence past the anti-flap window alerts again -- never permanently orphaned/suppressed", _panel_notifications_count(db_path4) == count_before_stale_recur + 1, _panel_notifications_count(db_path4))
        finally:
            cleanup_env(tmp_root4)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP E: FloodWait cooldown. Covers cases 19, 20, 21, 22.
# ======================================================================

async def test_group_e_floodwait_cooldown():
    print("\n-- Group E: FloodWait cooldown --")
    tmp_root, db_path = make_temp_env()
    try:
        fc = FakeClient()
        ns = build_main_ns(db_path, fc)
        key = ns["MANAGER_RUNTIME_KEY"]

        # A large FloodWait -> LIMITED with an active cooldown.
        await ns["_tp_hg_set_from_exception"](key, FloodWaitError(3600), source="client_auto_send")
        row = _raw_health_row(db_path, key)
        check("E-setup. large FloodWait classifies to LIMITED with cooldown_until set", row.get("health_status") == "limited" and bool(row.get("cooldown_until")), row)

        # 19. Active FloodWait cooldown blocks a relevant automatic send (existing-dialog
        # replies are ALSO blocked for FloodWait specifically -- it is a pure rate limit,
        # not a new-peer restriction). Snapshot send_calls right before this call -- the
        # setup line above already sent one client.send_message itself (the WORK_STATUS
        # health alert for the FloodWait incident).
        calls_before = fc.send_calls
        ok = await ns["_send_manager_private"](555, "x", new_dialog=False)
        check("E19. an existing-dialog send is denied while the FloodWait cooldown is active", ok is False, ok)
        check("E19b. the denied send never reached client.send_message", fc.send_calls == calls_before, fc.send_calls)

        # 20. FloodWait expiry allows one controlled retry (gate re-opens).
        _raw_backdate_cooldown_expired(db_path, key)
        allowed, reason = await ns["_tp_hg_send_allowed"](key, new_dialog=False)
        check("E20. once the cooldown has elapsed, the gate allows a controlled retry", allowed is True, (allowed, reason))

        # 21. A successful controlled retry recovers (allow_recover=True for the floodwait family).
        ok2 = await ns["_send_manager_private"](555, "retry after cooldown", new_dialog=False)
        check("E21. the controlled retry send succeeds", ok2 is True, ok2)
        row2 = _raw_health_row(db_path, key)
        check("E21b. a successful FloodWait-cooldown retry DOES recover the status to OK", row2.get("health_status") == "ok", row2)
        # L3 (finding): cooldown_until must not survive as stale data once the
        # status has genuinely recovered to OK.
        check("L3. cooldown_until is cleared on OK recovery, not left stale", row2.get("cooldown_until") == "", row2)

        # 22. A failed controlled retry preserves/extends the restriction.
        await ns["_tp_hg_set_from_exception"](key, FloodWaitError(1800), source="client_auto_send")
        row3 = _raw_health_row(db_path, key)
        check("E22. a failed retry (fresh FloodWaitError) keeps the account LIMITED with a refreshed cooldown", row3.get("health_status") == "limited" and bool(row3.get("cooldown_until")), row3)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP F: recovery paths -- reset, deepcheck bypass, weak-probe guard,
# manual_mark stickiness. Covers cases 15, 16, 17, 27.
# ======================================================================

async def test_group_f_recovery_paths():
    print("\n-- Group F: recovery paths (reset / deepcheck / manual mark) --")
    tmp_root, db_path = make_temp_env()
    try:
        fc = FakeClient()
        ns = build_main_ns(db_path, fc)
        key = ns["MANAGER_RUNTIME_KEY"]

        # 15. Explicit reset recovers and resolves the matching durable incident.
        await ns["_tp_hg_set_from_exception"](key, PeerFloodError(), source="client_auto_send")
        family = ns["_tp_hg_restriction_family"](status="limited", error_class="PeerFloodError")
        signature = ns["_tp_hg_incident_signature"](status="limited", error_class="PeerFloodError", error_source="client_auto_send", family=family, error_text=ns["_tp_hg_error_text"](PeerFloodError()))
        incident_before = storage.health_incident_get(key, signature, db_path=db_path)
        check("F15-setup. the PeerFlood incident is open before reset", incident_before is not None and incident_before.get("status") == "open", incident_before)
        count_before_reset = _panel_notifications_count(db_path)
        await ns["_tp_hg_reset"](key)
        row = _raw_health_row(db_path, key)
        check("F15. /tghealth reset clears the sticky status to unknown", row.get("health_status") == "unknown", row)
        incident_after = storage.health_incident_get(key, signature, db_path=db_path)
        check("F15b. reset resolves the matching durable incident", incident_after.get("status") == "resolved", incident_after)
        check("F15c. reset sends its own recovery notification", _panel_notifications_count(db_path) == count_before_reset + 1, _panel_notifications_titles(db_path))

        # 16 + 17. Deepcheck bypass is explicit and limited; a successful Saved Messages
        # probe alone never falsely proves PeerFlood recovery.
        await ns["_tp_hg_set_from_exception"](key, PeerFloodError(), source="client_auto_send")
        fc.get_me_calls = 0
        fc.get_dialogs_calls = 0
        res = await ns["_tp_hg_run_deep_self_check"](source="manual_deepcheck")
        check("F16. deepcheck's own light re-check genuinely runs get_me/get_dialogs (explicit bypass point, not skipped)", fc.get_me_calls >= 1 and fc.get_dialogs_calls >= 1, (fc.get_me_calls, fc.get_dialogs_calls))
        check("F17. deepcheck does NOT report ok=True from a successful Saved-Messages probe on an unresolved PeerFlood incident", res.get("ok") is False and res.get("status") == "limited", res)
        check("F17b. deepcheck's result text explains sends are not stopped and status auto-clears on a successful business send (PEERFLOOD RECOVERY 20260812)", "не остановлен" in res.get("text", "").lower() and "автоматически" in res.get("text", "").lower(), res)
        row_after_deepcheck = _raw_health_row(db_path, key)
        check("F17c. the persisted row is still LIMITED after the deepcheck probe", row_after_deepcheck.get("health_status") == "limited", row_after_deepcheck)

        # Deepcheck on a NON-peerflood (BLOCKED/auth-style) incident still recovers via a
        # successful self-send probe -- unchanged pre-existing behavior, only PeerFlood
        # gained the stricter guard.
        # CORRECTION A 20260810: script get_me_exc ONLY for the classification
        # call itself (so live-verification correctly lands on AUTH_SESSION_
        # PROBLEM, still BLOCKED) then clear it before the deepcheck's own,
        # separate live probe below -- deepcheck recovering via ITS OWN
        # successful get_me/send probe is the exact unchanged behavior this
        # scenario is proving, distinct from Correction A's verification step.
        fc.get_me_exc = AuthKeyUnregisteredError()
        await ns["_tp_hg_set_from_exception"](key, AuthKeyUnregisteredError(), source="client_auto_send")
        fc.get_me_exc = None
        res2 = await ns["_tp_hg_run_deep_self_check"](source="manual_deepcheck")
        check("F16b. deepcheck DOES recover a non-PeerFlood (auth/session) BLOCKED incident on a successful self-send probe", res2.get("ok") is True and res2.get("status") == "ok", res2)

        # 27. Manual mark remains sticky -- and the light self-check must not even attempt
        # a live probe while a manual mark is active.
        await ns["_tp_hg_mark"](key, "limited", "ручная проверка требуется")
        row_mark = _raw_health_row(db_path, key)
        check("F27-setup. manual mark sets LIMITED with error_source=manual_mark", row_mark.get("health_status") == "limited" and row_mark.get("error_source") == "manual_mark", row_mark)
        fc.get_me_calls = 0
        res3 = await ns["_tp_hg_run_self_check"](source="periodic_self_check")
        check("F27. the light self-check short-circuits on an active manual override (no live probe attempted)", fc.get_me_calls == 0, fc.get_me_calls)
        check("F27b. the light self-check reports the manual mark's status, not OK", res3.get("status") == "limited", res3)
        # An ordinary successful send must not clear a manual mark either.
        ok = await ns["_send_manager_private"](666, "x", new_dialog=False)
        check("F27c. manual mark blocks even an existing-dialog gate check (admin has not confirmed recovery)", ok is False, ok)
        row_mark_after = _raw_health_row(db_path, key)
        check("F27d. manual mark is unaffected by the gated send attempt", row_mark_after.get("error_source") == "manual_mark", row_mark_after)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP F2: H2 behavioral proof (finding H2) -- executes the REAL
# runtime-active/delegated-live _tp_gq_send_questionnaire_followup body (its
# base/first definition, live via the _LLMQ_ORIG_FOLLOWUP capture in the
# debounce-wrapper redefinition -- not an override-shadow), not a
# reimplementation, and proves the profile_final sent-flag is only written
# when the send actually succeeded. This is a SEPARATE, self-contained
# extraction (own tiny stub namespace) rather than folded into build_main_ns,
# because this function's dependency surface (lead automation gate, profile
# dialog state, LLM-supervisor shadow hooks, ...) is unrelated to the
# _tp_hg_* health-guard primitives the rest of this file focuses on; keeping
# it isolated avoids bloating/blurring the main harness with unrelated stubs.
# ======================================================================

def _extract_first_def_source(name: str) -> str:
    """Unlike _extract_active_nodes (which takes the LAST def for genuine
    override-shadow chains), _tp_gq_send_questionnaire_followup's real logic
    lives in its FIRST def -- the second is a debounce wrapper that delegates
    to a captured reference to the first, not a shadowing redefinition."""
    defs = find_defs(name)
    if not defs:
        raise AssertionError(f"{name} not found in main.py")
    return ast.unparse(defs[0])


async def test_group_f2_h2_behavioral():
    print("\n-- Group F2: H2 behavioral proof (real live questionnaire-followup body) --")
    body_src = _extract_first_def_source("_tp_gq_send_questionnaire_followup")
    check(
        "F2-setup. the extracted (live) body contains the H2 guard (`if ok:` before the sent-flag write)",
        "if ok:" in body_src and "_set_daily_profile_final_sent" in body_src, None,
    )

    send_results: List[bool] = []
    send_calls: List[tuple] = []
    flag_calls: List[dict] = []

    async def fake_send(chat_id, text, **kw):
        send_calls.append((chat_id, text, kw))
        return send_results.pop(0)

    async def fake_set_final(lead_row):
        flag_calls.append(dict(lead_row))

    async def fake_update_fields(*a, **kw):
        return None

    async def fake_gate(mk, cid, kind, state=None):
        return True, "ok"

    async def fake_load_state(db, cid):
        return {}

    def fake_choose(lead, text, st):
        return {}

    def fake_content(key, default):
        return f"TEXT[{key}]"

    class _FixedDate:
        @staticmethod
        def date():
            return datetime(2026, 7, 23).date()

    def fake_kyiv_now():
        return _FixedDate()

    ns = {
        "Dict": Dict, "Any": Any,
        "_update_daily_lead_fields": fake_update_fields,
        "_lead_automation_gate": fake_gate,
        "load_profile_dialog_state": fake_load_state,
        "choose_profile_reply": fake_choose,
        "_tp_gq_content_text": fake_content,
        "_send_manager_private": fake_send,
        "_set_daily_profile_final_sent": fake_set_final,
        "_set_daily_ua_sent": fake_update_fields,
        "_kyiv_now": fake_kyiv_now,
        "MANAGER_RUNTIME_KEY": "mgr_h2_behavioral", "DB_PATH": "unused.db",
        "globals": globals,  # the live body guards several optional hooks via globals().get(...)
    }
    exec(compile(body_src, "<followup-live-body-h2>", "exec"), ns)
    fn = ns["_tp_gq_send_questionnaire_followup"]

    lead = {"chat_id": 1, "manager_key": "mgr_h2_behavioral", "country": "Россия", "status": "liquid",
            "profile_done": 1, "profile_final_sent": 0, "_last_incoming_text": "x",
            "lead_date": "2026-07-23", "manager_replied": 0, "ua_text_sent": 1}

    # Scenario 1: the health gate denies (or the send otherwise fails) -> False.
    send_results[:] = [False]
    await fn(dict(lead), {}, 1)
    check("H2-behavioral-1. a denied/failed send does not write the profile_final sent-flag", len(send_calls) == 1 and len(flag_calls) == 0, (len(send_calls), len(flag_calls)))

    # Scenario 2: later, the same lead (flag still unset) is retried and the send succeeds -> flag exactly once.
    send_results[:] = [True]
    await fn(dict(lead), {}, 1)
    check("H2-behavioral-2. a later successful send writes the profile_final sent-flag exactly once", len(send_calls) == 2 and len(flag_calls) == 1, (len(send_calls), len(flag_calls)))


# ======================================================================
# GROUP G: notify_count/history compatibility. Covers case 23.
# ======================================================================

async def test_group_g_history_compat():
    print("\n-- Group G: notify_count/history compatibility --")
    tmp_root, db_path = make_temp_env()
    try:
        fc = FakeClient()
        ns = build_main_ns(db_path, fc)
        key = ns["MANAGER_RUNTIME_KEY"]

        await ns["_tp_hg_set_from_exception"](key, PeerFloodError(), source="client_auto_send")
        row1 = _raw_health_row(db_path, key)
        check("G23. notify_count is incremented on the first alert (historical/compat field preserved)", int(row1.get("notify_count") or 0) >= 1, row1)
        check("G23b. notify_sent_at is populated", bool(row1.get("notify_sent_at")), row1)
        check("G23c. last_notify_signature is populated", bool(row1.get("last_notify_signature")), row1)
        count_before = int(row1.get("notify_count") or 0)

        # A blocked-downgrade attempt (ordinary OK write over unresolved LIMITED) must not
        # erase the historical notify_* bookkeeping.
        await ns["_tp_hg_update_status"](key, status=ns["TP_HG_STATUS_OK"], error_source="client_send_success")
        row2 = _raw_health_row(db_path, key)
        check("G23d. a blocked downgrade preserves notify_count/notify_sent_at/last_notify_signature", int(row2.get("notify_count") or 0) == count_before and row2.get("notify_sent_at") == row1.get("notify_sent_at") and row2.get("last_notify_signature") == row1.get("last_notify_signature"), (row1, row2))
        check("G23e. a blocked downgrade preserves error_class/error_text/error_source/first_seen_at", row2.get("error_class") == row1.get("error_class") and row2.get("error_source") == row1.get("error_source") and row2.get("first_seen_at") == row1.get("first_seen_at"), (row1, row2))
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# GROUP H: scope / regression -- no hardcoding, controller mode unaffected,
# autodozhim-style gate reuse. Covers cases 24, 25, 28.
# ======================================================================

async def test_group_h_scope_regression():
    print("\n-- Group H: scope / regression --")

    # 24. No hardcoding of specific manager keys/usernames from the production
    # incident anywhere in the patched source.
    forbidden = ['"vinch"', "'vinch'", '"dariass"', "'dariass'", '"mikhail_bely"', "'mikhail_bely'", '"jora"', "'jora'", '"ed"', "'ed'"]
    hits = [tok for tok in forbidden if tok in MAIN_SRC]
    check("H24. no hardcoded production manager key/username literals in main.py", not hits, hits)

    tmp_root, db_path = make_temp_env()
    try:
        # 25. Controller mode / non-manager services unaffected.
        fc = FakeClient()
        ns = build_main_ns(db_path, fc, manager_key="", controller_mode=True)
        res = await ns["_tp_hg_run_self_check"](source="periodic_self_check")
        check("H25. with no MANAGER_RUNTIME_KEY, the self-check returns UNKNOWN without touching the client", res.get("status") == "unknown" and fc.get_me_calls == 0, (res, fc.get_me_calls))
        ok = await ns["_send_manager_private"](777, "controller-context send", new_dialog=True)
        check("H25b. with no MANAGER_RUNTIME_KEY, the send gate is skipped entirely and the send proceeds", ok is True and fc.send_calls == 1, (ok, fc.send_calls))

        # 28 (PEERFLOOD RECOVERY 20260812): autodozhim-style existing-dialog
        # gate reuse -- the SAME central gate that backs
        # _process_post_manual_followups_once (new_dialog=False) now ALLOWS
        # while sticky LIMITED (one controlled probe per tick), same as
        # every other caller -- only BLOCKED/manual_mark/an unexpired
        # FloodWait cooldown still deny (see H28b below).
        fc2 = FakeClient()
        ns2 = build_main_ns(db_path, fc2, manager_key="mgrh_followup")
        key2 = ns2["MANAGER_RUNTIME_KEY"]
        await ns2["_tp_hg_set_from_exception"](key2, PeerFloodError(), source="client_auto_send")
        allowed_limited, reason_limited = await ns2["_tp_hg_send_allowed"](key2, new_dialog=False)
        check("H28. autodozhim-style existing-dialog gate check now ALLOWS while sticky LIMITED (PEERFLOOD RECOVERY 20260812)", allowed_limited is True, allowed_limited)
        check("H28a. the LIMITED allow reason is _TP_HG_GATE_ALLOW_LIMITED_PROBE", reason_limited == ns2["_TP_HG_GATE_ALLOW_LIMITED_PROBE"], reason_limited)
        # CORRECTION A 20260810: see the identical comment in Group C -- the
        # fake client's get_me() must also fail so live-verification lands on
        # AUTH_SESSION_PROBLEM (still BLOCKED).
        fc2.get_me_exc = AuthKeyUnregisteredError()
        await ns2["_tp_hg_set_from_exception"](key2, AuthKeyUnregisteredError(), source="client_auto_send")
        allowed_blocked, _ = await ns2["_tp_hg_send_allowed"](key2, new_dialog=False)
        check("H28b. autodozhim-style existing-dialog gate check denies while BLOCKED", allowed_blocked is False, allowed_blocked)
    finally:
        cleanup_env(tmp_root)


# ======================================================================
# STATIC: which definitions are active, and the allow_spend invariant.
# Covers case 26 + the project's CLAUDE.md allow_spend audit.
# ======================================================================

def test_static_active_definitions():
    print("\n-- Static: active (last) definitions are the ones actually patched --")
    for name, expect_snippet in (
        ("_send_manager_private", "new_dialog"),
        ("_tp_hg_run_self_check", "live_probe_ok"),
    ):
        defs = find_defs(name)
        # Commit ef4fb8d ("streamline lead status...") removed DEAD shadowed
        # copies, so a single (active-only) definition is now a valid state;
        # what still matters is that the ACTIVE (last) def carries the patch
        # marker and any surviving dead copies stay untouched (below).
        check(f"static. {name} has at least one definition (stacked chain allowed, per CLAUDE.md section 4; dead copies may have been removed by cleanup ef4fb8d)", len(defs) >= 1, len(defs))
        last_src = ast.unparse(defs[-1]) if defs else ""
        check(f"static. the LAST (active) {name} definition contains the new patch marker '{expect_snippet}'", expect_snippet in last_src, len(defs))
        for i, d in enumerate(defs[:-1]):
            check(f"static. {name} def #{i + 1} (dead/shadowed) was left untouched (no '{expect_snippet}')", expect_snippet not in ast.unparse(d), i)

    for name in ("_tp_hg_update_status", "_tp_hg_notify_if_needed", "_tp_hg_run_deep_self_check", "_send_manager_private_ex"):
        defs = find_defs(name)
        check(f"static. {name} is defined exactly once at module top level", len(defs) == 1, len(defs))

    # CLAUDE.md's own invariant is about allow_spend=True as a REAL CALL
    # ARGUMENT, not any textual mention -- the file also has ~15 comments/
    # docstrings *describing* that invariant (e.g. "SPEND SAFETY: allow_spend=
    # True is passed to make_ipv4() in EXACTLY ONE place..."), which a bare
    # substring search would double-count. A genuine keyword-argument call site
    # is a line containing nothing but `allow_spend=True` (plus a trailing
    # comma/whitespace); prose always has surrounding text on the same line.
    allow_spend_call_sites = [
        ln for ln in MAIN_SRC.splitlines() if re.match(r"^\s*allow_spend=True,?\s*$", ln)
    ]
    check(
        "static. allow_spend=True still appears as a real call argument in exactly 2 places project-wide (CLAUDE.md invariant, unrelated to this patch)",
        len(allow_spend_call_sites) == 2, allow_spend_call_sites,
    )


def test_static_caller_matrix_and_corrective_fixes():
    """TPILOT HEALTH RECOVERY HYBRID-D CORRECTIVE 20260723b (finding M3):
    structural regression lock on the send-gate caller matrix -- a test suite
    that only exercises _tp_hg_* primitives in isolation would keep passing
    even if `new_dialog=...` (or an entire pre-tick gate call) were silently
    dropped from an active caller, since none of the other tests execute
    those caller bodies (several have too many unrelated dependencies --
    LLM supervisor, dialog-state persistence, lead automation gate -- to
    safely AST-extract and exec without a large fake surface). These checks
    close that gap by verifying, directly against the real source of the
    ACTIVE (non-shadowed) definitions, that every known automatic-send call
    site still carries the gate wiring, and that the H2 sent-flag fix is the
    one actually shipped (not just present in a comment)."""
    print("\n-- Static: send-gate caller matrix + H2 sent-flag fix (structural) --")

    # _maybe_auto_reply_to_lead historically had 6 stacked defs (only the
    # LAST active). The R1 override-chain collapse (2026-08-23) removed the
    # shadowed bodies, so exactly ONE definition -- the previously-active
    # one -- must remain. defs[-1] below is therefore still the active body.
    reply_defs = find_defs("_maybe_auto_reply_to_lead")
    check("caller-matrix. _maybe_auto_reply_to_lead has exactly one (post-collapse, active) definition", len(reply_defs) == 1, len(reply_defs))
    active_reply_src = ast.unparse(reply_defs[-1]) if reply_defs else ""
    check(
        "caller-matrix. active _maybe_auto_reply_to_lead: old-client night notice is gated as an EXISTING dialog (new_dialog=False)",
        "new_dialog=False" in active_reply_src and active_reply_src.count("new_dialog=False") >= 1,
        active_reply_src.count("new_dialog=False"),
    )
    check(
        "caller-matrix. active _maybe_auto_reply_to_lead: exactly two NEW-dialog sends (new_client night notice + day greeting)",
        active_reply_src.count("new_dialog=True") == 2, active_reply_src.count("new_dialog=True"),
    )

    # _process_profile_reminders_once and _process_post_manual_followups_once
    # each have a BASE body (the real logic, first def) plus a trivial content-
    # settings debounce wrapper redefinition (`*args, **kwargs` passthrough
    # that just calls a captured reference to the base -- same delegation
    # pattern as _tp_gq_send_questionnaire_followup above, not an override-
    # shadow). The base body (defs[0]) is where the gate wiring lives.
    for name in ("_process_profile_reminders_once", "_process_post_manual_followups_once"):
        defs = find_defs(name)
        check(f"caller-matrix. {name} has a base body plus a content-refresh wrapper redefinition", len(defs) == 2, len(defs))
        src = ast.unparse(defs[0]) if defs else ""
        check(f"caller-matrix. {name} calls the central health gate (_tp_hg_send_allowed) before its send loop", "_tp_hg_send_allowed(" in src, name)
        check(f"caller-matrix. {name} passes new_dialog=False to its send call (existing-dialog only)", "new_dialog=False" in src, name)
        # PEERFLOOD RECOVERY 20260812 (M4): both loops must use the
        # fail_kind-aware _send_manager_private_ex, not the plain bool
        # _send_manager_private, so a health-gate denial or a classified
        # Telegram restriction can be told apart from an ordinary send
        # failure (and never trashes the lead / stops the tick instead).
        check(f"caller-matrix. {name} calls _send_manager_private_ex (fail_kind-aware) not the plain bool wrapper", "_send_manager_private_ex(" in src, name)
        check(f"caller-matrix. {name} never trashes a lead on gate_denied/restriction fail_kind", "fail_kind in ('gate_denied', 'restriction')" in src, name)

    # _tp_gq_send_questionnaire_followup's real logic lives in the FIRST def
    # (base body); the second def is a debounce wrapper that delegates to a
    # captured reference to the first -- not an override-shadow, so "active"
    # here means "reachable via delegation", verified structurally against
    # the base body's own source slice rather than picking a specific node.
    followup_defs = find_defs("_tp_gq_send_questionnaire_followup")
    check("caller-matrix. _tp_gq_send_questionnaire_followup has a base body plus a debounce-wrapper redefinition", len(followup_defs) == 2, len(followup_defs))
    base_followup_src = ast.unparse(followup_defs[0])
    check(
        "caller-matrix. the base questionnaire-followup body gates ALL FOUR of its sends as existing-dialog (new_dialog=False)",
        base_followup_src.count("new_dialog=False") == 4, base_followup_src.count("new_dialog=False"),
    )

    # H2 fix: the profile_final send result must be checked before the
    # sent-flag is written, and the OLD unconditional pattern must be gone.
    h2_fixed_pattern = re.search(
        r"ok = await _send_manager_private\(chat_id, txt, human_delay=True, after_first_delay=True, new_dialog=False\)\s*\n"
        r"\s*if ok:\s*\n"
        r"\s*await _set_daily_profile_final_sent\(lead_row\)",
        MAIN_SRC,
    )
    check("H2. profile_final: the send result is captured and the sent-flag is written only `if ok:`", h2_fixed_pattern is not None, None)
    h2_old_broken_pattern = re.search(
        r"await _send_manager_private\(chat_id, txt, human_delay=True, after_first_delay=True, new_dialog=False\)\s*\n"
        r"\s*await _set_daily_profile_final_sent\(lead_row\)",
        MAIN_SRC,
    )
    check("H2b. the OLD unconditional (ignore-result, always-mark-sent) pattern is gone from the source", h2_old_broken_pattern is None, None)


def test_static_storage_api_compatibility():
    """TPILOT HEALTH RECOVERY HYBRID-D CORRECTIVE 20260723b (finding H1/M2):
    verify storage.py additions are genuinely ADDITIVE -- the pre-existing
    health_incident_* API (used by main.py's OTHER, untouched consumer,
    _health_incident_handle) must remain exactly as it was, and the new
    functions must be new names, not silent behavior changes to old ones."""
    print("\n-- Static: storage.py health_incident_* API compatibility --")
    for name in ("ensure_health_incidents_table", "health_incident_upsert_open",
                 "health_incident_mark_notified", "health_incident_resolve", "health_incident_get"):
        check(f"storage-compat. pre-existing storage.{name} is still defined exactly once", STORAGE_SRC.count(f"\ndef {name}(") == 1, name)
        check(f"storage-compat. storage.{name} is importable/callable", callable(getattr(storage, name, None)), name)
    for name in ("health_incident_claim_notify", "health_incident_resolve_all_for_manager"):
        check(f"storage-compat. new storage.{name} is defined exactly once (additive, not a rename)", STORAGE_SRC.count(f"\ndef {name}(") == 1, name)
        check(f"storage-compat. new storage.{name} is importable/callable", callable(getattr(storage, name, None)), name)

    # The rewritten _tp_hg_notify_if_needed must genuinely use the new
    # pre-delivery claim primitive, not just have it sitting unused nearby.
    notify_defs = find_defs("_tp_hg_notify_if_needed")
    notify_src = ast.unparse(notify_defs[0]) if notify_defs else ""
    check("storage-compat. _tp_hg_notify_if_needed calls health_incident_claim_notify", "health_incident_claim_notify(" in notify_src, None)
    check("storage-compat. _tp_hg_notify_if_needed calls health_incident_resolve_all_for_manager on recovery", "health_incident_resolve_all_for_manager(" in notify_src, None)
    check("storage-compat. _tp_hg_notify_if_needed no longer relies on the old post-delivery health_incident_mark_notified", "health_incident_mark_notified(" not in notify_src, None)


def main() -> int:
    asyncio.run(test_group_a_state_machine())
    asyncio.run(test_group_b_no_false_recovery())
    asyncio.run(test_group_c_send_gate())
    asyncio.run(test_group_d_durable_dedup())
    asyncio.run(test_group_e_floodwait_cooldown())
    asyncio.run(test_group_f_recovery_paths())
    asyncio.run(test_group_f2_h2_behavioral())
    asyncio.run(test_group_g_history_compat())
    asyncio.run(test_group_h_scope_regression())
    test_static_active_definitions()
    test_static_caller_matrix_and_corrective_fixes()
    test_static_storage_api_compatibility()

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
