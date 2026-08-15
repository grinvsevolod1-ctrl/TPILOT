# -*- coding: utf-8 -*-
"""tools/identity_consumers_selftest.py -- offline regression test for the
"TPILOT IDENTITY RECONCILE 20260809" patch (plan Ф2 / H8): PartnerBot and
ManagerBot surfaces that used to render a manager's IDENTITY (display name +
Telegram username) from a frozen historical snapshot instead of the LIVE
`managers` table.

Confirmed defect (see the audit that drove this patch): partner_lead_events.
manager_display_name/manager_username are written ONCE when the event row is
created and never touched again by any rename-propagation job -- so after a
Telegram rename (production case: manager_key=ira) PartnerBot's live lead
cards and duplicate cards kept showing the OLD name/username forever, even
though PartnerBot's OWN stats surfaces already read the live `managers`
table. The analogous defect existed in ManagerBot's duplicate card, which
read identity out of the frozen `manager_bot_events.payload_json` snapshot.

This file covers the two PartnerBot active surfaces named in the plan:
  - _partner_manager_label_from_event  (partner_stat_bot.py)
  - _fmt_lead_notification             (partner_stat_bot.py, ACTIVE def --
    this file has 3 stacked definitions; "last wins")
and the one ManagerBot surface:
  - _format_duplicate_card             (manager_bot.py)

Technique (same convention as every other tools/*_selftest.py in this
project): partner_stat_bot.py and manager_bot.py both construct a real
TelegramClient(...) at MODULE SCOPE (partner_stat_bot.py line ~46) and load
.env at import time, so importing either module directly is unsafe for an
offline test -- no existing tools/*_selftest.py does it. The functions under
test (plus their pure, side-effect-free dependencies) are extracted via
ast.parse + ast.unparse + exec() instead. The live-identity lookups
(_current_manager_identity / _sched_manager_label_mb) run for REAL against a
temp SQLite `managers` table -- never faked -- proving the actual SQL,
not a mock of it.

Mutation proof (fault-injection at the exact dependency boundary the fix
introduced, not text/AST surgery on the call sites -- see each scenario's
own comment for why this is equivalent to literally reverting to direct
snapshot/payload reads): binds a fake dependency that always defers to the
snapshot/payload, and asserts the "current identity" checks flip to FAIL,
proving they are load-bearing.

Never: real Telegram network, real filesystem writes outside a temp dir,
production DB/runtime/session/log access, any UPDATE against a
partner_lead_events-shaped table (this file never even creates one --
_partner_manager_label_from_event/_fmt_lead_notification only ever receive
plain dict rows, proving by construction that no historical table write is
possible from these code paths).

    python tools\\identity_consumers_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


PARTNER_PATH = str(BASE_DIR / "partner_stat_bot.py")
MANAGER_BOT_PATH = str(BASE_DIR / "manager_bot.py")
PARTNER_SRC = open(PARTNER_PATH, encoding="utf-8-sig").read()
MANAGER_BOT_SRC = open(MANAGER_BOT_PATH, encoding="utf-8-sig").read()


def _extract_by_names(src: str, names: set):
    """Returns {name: node} for every top-level (Async)FunctionDef whose name
    is in `names`, taking the LAST definition per name (this project's own
    override-stacking convention -- see CLAUDE.md; _fmt_lead_notification has
    3 stacked defs, everything else here has exactly 1). Raises if any
    requested name is missing."""
    tree = ast.parse(src)
    found: Dict[str, Any] = {}
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names:
            found[n.name] = n
    missing = names - set(found.keys())
    if missing:
        raise AssertionError(f"expected {names}, missing {missing}")
    return found


def _unparse_all(nodes) -> str:
    import copy
    parts = []
    for n in nodes:
        n = copy.deepcopy(n)
        ast.fix_missing_locations(n)
        parts.append(ast.unparse(n))
    return "\n\n".join(parts)


def _selftest_db_guard(db_path: str) -> None:
    prod_db_dir = str((BASE_DIR / "db").resolve())
    target = str(Path(db_path).resolve())
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"


def make_temp_env(prefix: str):
    tmp_root = Path(tempfile.mkdtemp(prefix=prefix))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


async def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


# ======================================================================
# PART A -- PartnerBot: _partner_manager_label_from_event / _fmt_lead_notification
# ======================================================================

PARTNER_NAMES = {
    "_norm_key",
    "_lead_dt",
    "_current_manager_identity",
    "_partner_current_or_snapshot_identity",
    "_partner_manager_label_from_event",
    "_fmt_lead_notification",
}


def build_partner_ns(db_path: str, *, fake_resolver=None):
    """Extracts the PartnerBot identity-resolution chain and both confirmed
    active consumer surfaces. If `fake_resolver` is given, it REPLACES
    _partner_current_or_snapshot_identity in the namespace -- fault-injecting
    at the exact dependency boundary the fix introduced (see MUT-PARTNER
    below) instead of mutating source text."""
    import storage as _storage

    nodes = _extract_by_names(PARTNER_SRC, PARTNER_NAMES)
    src = _unparse_all(nodes.values())

    def _kyiv_now():
        return _storage.w3_now()

    ns: Dict[str, Any] = {
        "sqlite3": __import__("sqlite3"),
        "time": __import__("time"),
        "re": __import__("re"),
        "os": os,
        "datetime": datetime,
        "timezone": timezone,
        "TZ": _storage.w3_tz(),
        "_kyiv_now": _kyiv_now,
        "TPILOT_DB_PATH": db_path,
        "_CURRENT_MGR_IDENTITY_CACHE": {},
        "_CURRENT_MGR_IDENTITY_TTL_SEC": 60.0,
        "print": print,
        "Dict": Dict, "Any": Any, "Optional": Optional, "Tuple": Tuple,
    }
    exec(compile(src, f"<{PARTNER_PATH}:identity-consumers>", "exec"), ns)
    if fake_resolver is not None:
        ns["_partner_current_or_snapshot_identity"] = fake_resolver
    return ns


async def seed_partner_managers_table(db_path: str):
    """Real managers row via the real storage module (same schema
    _current_manager_identity's raw SELECT expects)."""
    import storage as _storage

    _selftest_db_guard(db_path)
    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    await _storage.manager_add(
        manager_key="ira", display_name="Ира", phone="", status="active",
        session_path="", db_path=db_path, workdir="", log_path="",
    )
    await _storage.manager_set_fields("ira", telegram_username="ira_tg_t")
    return _storage


STALE_SNAPSHOT_ROW = {
    "manager_key": "ira",
    "manager_display_name": "pNpLqhW",
    "manager_username": "",
    "first_seen_utc": "2026-08-01T10:00:00+00:00",
    "chat_id": 123456,
    "username": "leaduser",
    "full_name": "Lead Full Name",
    "phone": "",
}

GHOST_SNAPSHOT_ROW = {
    "manager_key": "ghost_deleted_manager",
    "manager_display_name": "OldGhostName",
    "manager_username": "old_ghost_user",
    "first_seen_utc": "2026-08-01T10:00:00+00:00",
    "chat_id": 1,
}


async def test_partner_current_identity_label_from_event():
    print("\n-- PartnerBot A1: _partner_manager_label_from_event prefers LIVE identity --")
    tmp_root, db_path = make_temp_env("idcons_partner_a1_")
    try:
        await seed_partner_managers_table(db_path)
        ns = build_partner_ns(db_path)
        label = ns["_partner_manager_label_from_event"](STALE_SNAPSHOT_ROW)
        check("A1. label uses CURRENT identity, not the frozen snapshot", label == "Ира | @ira_tg_t", label)
        check("A2. row dict itself is not mutated by the call", STALE_SNAPSHOT_ROW["manager_display_name"] == "pNpLqhW", STALE_SNAPSHOT_ROW)
    finally:
        await cleanup_env(tmp_root)


async def test_partner_current_identity_fmt_lead_notification():
    print("\n-- PartnerBot A3: _fmt_lead_notification (ACTIVE def) prefers LIVE identity --")
    tmp_root, db_path = make_temp_env("idcons_partner_a3_")
    try:
        await seed_partner_managers_table(db_path)
        ns = build_partner_ns(db_path)
        buyer = {"can_view_contacts": 0}
        text = ns["_fmt_lead_notification"](STALE_SNAPSHOT_ROW, buyer)
        check("A3. rendered card shows CURRENT identity 'Ира | @ira_tg_t'", "Ира | @ira_tg_t" in text, text)
        check("A4. rendered card does NOT show the stale snapshot name", "pNpLqhW" not in text, text)
    finally:
        await cleanup_env(tmp_root)


async def test_partner_fallback_when_manager_row_missing():
    print("\n-- PartnerBot A5: manager row absent -> fallback to historical snapshot --")
    tmp_root, db_path = make_temp_env("idcons_partner_a5_")
    try:
        # No managers row seeded at all -- db has the schema (created lazily by
        # _current_manager_identity's own SELECT against an empty/new sqlite file)
        # but zero rows.
        import storage as _storage
        _selftest_db_guard(db_path)
        _storage.DB_PATH = db_path
        _storage.QUEUE_DB_PATH = db_path
        # Ensure the managers table exists (empty) so the SELECT doesn't hit a
        # missing-table error, same as production after init_db() has run once.
        await _storage.manager_add(
            manager_key="someone_else", display_name="X", phone="", status="active",
            session_path="", db_path=db_path, workdir="", log_path="",
        )
        ns = build_partner_ns(db_path)
        label = ns["_partner_manager_label_from_event"](GHOST_SNAPSHOT_ROW)
        check(
            "A5. deleted-manager fallback renders the historical snapshot, not a blank/'_'",
            label == "OldGhostName | @old_ghost_user",
            label,
        )
    finally:
        await cleanup_env(tmp_root)


async def test_partner_never_touches_history_table():
    print("\n-- PartnerBot A6: no historical-table write is even possible --")
    tmp_root, db_path = make_temp_env("idcons_partner_a6_")
    try:
        await seed_partner_managers_table(db_path)
        ns = build_partner_ns(db_path)
        # Both consumer surfaces take a plain dict `row` -- they never receive a
        # DB handle/cursor, so structurally there is no way for them to execute an
        # UPDATE against partner_lead_events. Call both and confirm the passed-in
        # row objects are byte-for-byte unchanged afterwards (the only mutation
        # surface a plain-dict-in/str-out function could possibly have).
        before = dict(STALE_SNAPSHOT_ROW)
        ns["_partner_manager_label_from_event"](STALE_SNAPSHOT_ROW)
        ns["_fmt_lead_notification"](STALE_SNAPSHOT_ROW, {"can_view_contacts": 1})
        check("A6. snapshot row dict unchanged after both calls (no in-place mutation, a fortiori no DB UPDATE)",
              STALE_SNAPSHOT_ROW == before, (before, STALE_SNAPSHOT_ROW))
    finally:
        await cleanup_env(tmp_root)


class _FakeTime:
    """F2 CHECKPOINT (2026-08-09): deterministic stand-in for the `time` module
    inside the exec'd PartnerBot namespace -- controls what time.monotonic()
    returns so TTL-cache expiry can be tested exactly (no real sleep, no
    flakiness), by directly replacing ns["time"] after build_partner_ns (the
    exec'd _current_manager_identity resolves `time` via its own __globals__ =
    the ns dict, at CALL time, so a post-exec reassignment still takes effect --
    same technique already used for fake_resolver overrides above)."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def monotonic(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


class _CountingSqlite3:
    """Wraps the real sqlite3 module, counting .connect() calls -- used to prove
    a cache hit does NOT open a second connection (i.e. genuinely didn't re-query),
    distinct from merely asserting the returned VALUE looks right."""

    def __init__(self, real_module):
        self._real = real_module
        self.connect_calls = 0

    def connect(self, *a, **kw):
        self.connect_calls += 1
        return self._real.connect(*a, **kw)

    def __getattr__(self, name):
        return getattr(self._real, name)


async def test_partner_cache_isolation_between_managers():
    print("\n-- PartnerBot A7: TTL cache never confuses two different manager_key's identities --")
    tmp_root, db_path = make_temp_env("idcons_partner_cache_iso_")
    try:
        import storage as _storage
        _selftest_db_guard(db_path)
        _storage.DB_PATH = db_path
        _storage.QUEUE_DB_PATH = db_path
        await _storage.manager_add(manager_key="mgrx", display_name="ИмяX", phone="", status="active",
                                    session_path="", db_path=db_path, workdir="", log_path="")
        await _storage.manager_set_fields("mgrx", telegram_username="user_x")
        await _storage.manager_add(manager_key="mgry", display_name="ИмяY", phone="", status="active",
                                    session_path="", db_path=db_path, workdir="", log_path="")
        await _storage.manager_set_fields("mgry", telegram_username="user_y")

        ns = build_partner_ns(db_path)
        resolve = ns["_current_manager_identity"]

        rx = resolve("mgrx")
        ry = resolve("mgry")
        check("A7-1. mgrx resolves to its OWN identity", rx == ("ИмяX", "user_x"), rx)
        check("A7-2. mgry resolves to its OWN identity (not mgrx's)", ry == ("ИмяY", "user_y"), ry)

        cache = ns["_CURRENT_MGR_IDENTITY_CACHE"]
        check("A7-3. cache holds exactly 2 independent entries, one per manager_key",
              set(cache.keys()) == {"mgrx", "mgry"}, cache)

        # Re-resolve both from cache (well within TTL) -- values must still be
        # correctly attributed, not swapped.
        rx2 = resolve("mgrx")
        ry2 = resolve("mgry")
        check("A7-4. cached re-resolve of mgrx is still mgrx's identity (not swapped with mgry)", rx2 == rx, (rx, rx2))
        check("A7-5. cached re-resolve of mgry is still mgry's identity (not swapped with mgrx)", ry2 == ry, (ry, ry2))
    finally:
        await cleanup_env(tmp_root)


async def test_partner_cache_key_is_normalized():
    print("\n-- PartnerBot A8: cache key is the NORMALIZED manager_key (case-insensitive hit) --")
    tmp_root, db_path = make_temp_env("idcons_partner_cache_norm_")
    try:
        import storage as _storage
        _selftest_db_guard(db_path)
        _storage.DB_PATH = db_path
        _storage.QUEUE_DB_PATH = db_path
        await _storage.manager_add(manager_key="mixedkey", display_name="ИмяMixed", phone="", status="active",
                                    session_path="", db_path=db_path, workdir="", log_path="")
        await _storage.manager_set_fields("mixedkey", telegram_username="mixed_u")

        ns = build_partner_ns(db_path)
        counting = _CountingSqlite3(ns["sqlite3"])
        ns["sqlite3"] = counting
        resolve = ns["_current_manager_identity"]

        r1 = resolve("MIXEDKEY")
        check("A8-1. uppercase lookup resolves correctly", r1 == ("ИмяMixed", "mixed_u"), r1)
        check("A8-2. one real DB connection made for the cold lookup", counting.connect_calls == 1, counting.connect_calls)

        r2 = resolve("mixedkey")
        check("A8-3. lowercase lookup for the SAME manager returns the identical cached value", r2 == r1, (r1, r2))
        check("A8-4. NO second DB connection -- proves the lowercase lookup hit the SAME normalized cache entry, "
              "not a fresh query", counting.connect_calls == 1, counting.connect_calls)

        cache = ns["_CURRENT_MGR_IDENTITY_CACHE"]
        check("A8-5. exactly ONE cache entry exists (both spellings normalized to the same key)", len(cache) == 1, cache)
    finally:
        await cleanup_env(tmp_root)


async def test_partner_ttl_staleness_window():
    print("\n-- PartnerBot A9: a rename is visible only after the TTL window elapses --")
    tmp_root, db_path = make_temp_env("idcons_partner_ttl_")
    try:
        import storage as _storage
        _selftest_db_guard(db_path)
        _storage.DB_PATH = db_path
        _storage.QUEUE_DB_PATH = db_path
        await _storage.manager_add(manager_key="ttlmgr", display_name="Old Name", phone="", status="active",
                                    session_path="", db_path=db_path, workdir="", log_path="")
        await _storage.manager_set_fields("ttlmgr", telegram_username="old_user")

        ns = build_partner_ns(db_path)
        fake_time = _FakeTime(start=1000.0)
        ns["time"] = fake_time
        resolve = ns["_current_manager_identity"]

        r1 = resolve("ttlmgr")
        check("A9-1. initial (cold) resolve returns the current DB value", r1 == ("Old Name", "old_user"), r1)

        # Simulate a Telegram rename landing in the DB AFTER the cache was
        # populated (e.g. the periodic reconcile ran on a different process).
        await _storage.manager_set_fields("ttlmgr", display_name="New Name", telegram_username="new_user")

        fake_time.advance(1.0)  # well within the 60s TTL
        r2 = resolve("ttlmgr")
        check("A9-2. within the TTL window, the STALE cached value is still returned (expected caching behavior)",
              r2 == r1, (r1, r2))

        fake_time.advance(ns["_CURRENT_MGR_IDENTITY_TTL_SEC"] + 1.0)  # past the TTL
        r3 = resolve("ttlmgr")
        check("A9-3. once the TTL has elapsed, the FRESH (renamed) value becomes visible",
              r3 == ("New Name", "new_user"), r3)
    finally:
        await cleanup_env(tmp_root)


async def test_partner_mutation_proof_revert_to_snapshot():
    print("\n-- MUT-PARTNER: fault-inject the pre-fix (snapshot-only) resolver --")
    tmp_root, db_path = make_temp_env("idcons_partner_mut_")
    try:
        await seed_partner_managers_table(db_path)

        # This IS the pre-fix behavior, structurally: _partner_current_or_snapshot_identity
        # replaced exactly this inline snapshot-read logic at both call sites (see the
        # function's own module docstring in partner_stat_bot.py). Binding this fake in
        # place of the real resolver is equivalent to reverting the fix at both consumer
        # surfaces simultaneously, without duplicating/hand-copying their own logic.
        def pre_fix_resolver(manager_key, snapshot_display, snapshot_username):
            return str(snapshot_display or "").strip(), str(snapshot_username or "").strip().lstrip("@")

        ns = build_partner_ns(db_path, fake_resolver=pre_fix_resolver)
        label = ns["_partner_manager_label_from_event"](STALE_SNAPSHOT_ROW)
        text = ns["_fmt_lead_notification"](STALE_SNAPSHOT_ROW, {"can_view_contacts": 0})

        check(
            "MUT-PARTNER. with the pre-fix snapshot-only resolver, the STALE name reappears "
            "(proves A1/A3's 'current identity' assertions are load-bearing, not tautologies)",
            label == "pNpLqhW" and "pNpLqhW" in text and "Ира" not in text,
            (label, text),
        )
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# PART B -- ManagerBot: _format_duplicate_card
# ======================================================================

MANAGER_BOT_NAMES = {
    "_norm_key",
    "_connect",
    "_decode_payload",
    "_sched_manager_label_mb",
    "_format_duplicate_card",
}


def build_managerbot_ns(db_path: str, *, fake_label_resolver=None):
    """Extracts the ManagerBot duplicate-card chain. If `fake_label_resolver`
    is given, it REPLACES _sched_manager_label_mb -- fault-injecting at the
    exact dependency boundary the fix introduced (see MUT-MANAGERBOT below)."""
    nodes = _extract_by_names(MANAGER_BOT_SRC, MANAGER_BOT_NAMES)
    src = _unparse_all(nodes.values())
    ns: Dict[str, Any] = {
        "sqlite3": __import__("sqlite3"),
        "json": __import__("json"),
        "Path": Path,
        "TPILOT_DB_PATH": db_path,
        "Dict": Dict, "Any": Any, "List": List,
    }
    exec(compile(src, f"<{MANAGER_BOT_PATH}:identity-consumers>", "exec"), ns)
    if fake_label_resolver is not None:
        ns["_sched_manager_label_mb"] = fake_label_resolver
    return ns


async def seed_managerbot_managers_table(db_path: str):
    import storage as _storage

    _selftest_db_guard(db_path)
    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    await _storage.manager_add(
        manager_key="ira", display_name="Ира", phone="", status="active",
        session_path="", db_path=db_path, workdir="", log_path="",
    )
    await _storage.manager_set_fields("ira", telegram_username="ira_tg_t")
    await _storage.manager_add(
        manager_key="oldmgr", display_name="OldMgrCurrent", phone="", status="active",
        session_path="", db_path=db_path, workdir="", log_path="",
    )
    await _storage.manager_set_fields("oldmgr", telegram_username="oldmgr_current_user")
    return _storage


def _stale_event_row(**payload_overrides) -> Dict[str, Any]:
    import json as _json
    payload = {
        "manager_key": "ira",
        "manager_display_name": "pNpLqhW",
        "manager_username": "",
        "previous_manager_key": "oldmgr",
        "previous_manager_display_name": "StaleOldMgrName",
        "previous_manager_username": "stale_oldmgr_user",
        "full_name": "Duplicate Lead",
        "username": "dupleaduser",
        "phone": "",
        "chat_id": 999,
        "current_seen_utc": "2026-08-09T10:00:00",
        "first_seen_global_at": "2026-08-01T10:00:00",
        "previous_seen_utc": "2026-08-01T10:00:00",
        "status_text": "не считается в оплату",
    }
    payload.update(payload_overrides)
    return {"payload_json": _json.dumps(payload)}


async def test_managerbot_duplicate_card_current_identity():
    print("\n-- ManagerBot B1: _format_duplicate_card prefers LIVE identity for current + previous manager --")
    tmp_root, db_path = make_temp_env("idcons_mb_b1_")
    try:
        await seed_managerbot_managers_table(db_path)
        ns = build_managerbot_ns(db_path)
        text = ns["_format_duplicate_card"](_stale_event_row())

        # NOTE: _sched_manager_label_mb formats "{name} / @{username}" (slash, not the
        # " | @" pipe used elsewhere) -- an already-known separator inconsistency across
        # this project's UI surfaces (out of scope to fix here; see plan DEFERRED list),
        # not a defect in this patch.
        check("B1. current manager shows LIVE identity 'Ира / @ira_tg_t'", "Ира / @ira_tg_t" in text, text)
        check("B2. current manager does NOT show the stale payload name 'pNpLqhW'", "pNpLqhW" not in text, text)
        check("B3. previous manager shows LIVE identity 'OldMgrCurrent / @oldmgr_current_user'",
              "OldMgrCurrent / @oldmgr_current_user" in text, text)
        check("B4. previous manager does NOT show the stale payload name 'StaleOldMgrName'",
              "StaleOldMgrName" not in text, text)
    finally:
        await cleanup_env(tmp_root)


async def test_managerbot_duplicate_card_fallback_when_row_missing():
    print("\n-- ManagerBot B5: manager row absent -> fallback to payload snapshot --")
    tmp_root, db_path = make_temp_env("idcons_mb_b5_")
    try:
        import storage as _storage
        _selftest_db_guard(db_path)
        _storage.DB_PATH = db_path
        _storage.QUEUE_DB_PATH = db_path
        await _storage.manager_add(
            manager_key="someone_else", display_name="X", phone="", status="active",
            session_path="", db_path=db_path, workdir="", log_path="",
        )
        ns = build_managerbot_ns(db_path)
        row = _stale_event_row(
            manager_key="ghost_key", manager_display_name="GhostPayloadName", manager_username="ghost_user",
            previous_manager_key="", previous_manager_display_name="", previous_manager_username="",
        )
        text = ns["_format_duplicate_card"](row)
        check(
            "B5. deleted-manager fallback renders the payload snapshot, not a blank/'-'",
            "GhostPayloadName | @ghost_user" in text,
            text,
        )
    finally:
        await cleanup_env(tmp_root)


async def test_managerbot_duplicate_card_mixed_fallback_both_directions():
    print("\n-- ManagerBot B6/B7: current live + previous deleted, and the REVERSE --")

    # --- B6: current manager EXISTS (live managers row), previous manager DELETED ---
    tmp_root, db_path = make_temp_env("idcons_mb_b6_")
    try:
        import storage as _storage
        _selftest_db_guard(db_path)
        _storage.DB_PATH = db_path
        _storage.QUEUE_DB_PATH = db_path
        await _storage.manager_add(manager_key="ira", display_name="Ира", phone="", status="active",
                                    session_path="", db_path=db_path, workdir="", log_path="")
        await _storage.manager_set_fields("ira", telegram_username="ira_tg_t")
        # "oldmgr" (previous_manager_key in _stale_event_row's default payload) is
        # deliberately NEVER created here -- it no longer has a managers row.
        ns = build_managerbot_ns(db_path)
        text = ns["_format_duplicate_card"](_stale_event_row())

        check("B6-1. current manager (row exists) shows LIVE identity 'Ира / @ira_tg_t'", "Ира / @ira_tg_t" in text, text)
        check("B6-2. current manager does NOT show the stale payload name 'pNpLqhW'", "pNpLqhW" not in text, text)
        check("B6-3. previous manager (row gone) falls back to the historical payload 'StaleOldMgrName | @stale_oldmgr_user'",
              "StaleOldMgrName | @stale_oldmgr_user" in text, text)
    finally:
        await cleanup_env(tmp_root)

    # --- B7: REVERSE -- current manager DELETED, previous manager EXISTS (live) ---
    tmp_root, db_path = make_temp_env("idcons_mb_b7_")
    try:
        import storage as _storage
        _selftest_db_guard(db_path)
        _storage.DB_PATH = db_path
        _storage.QUEUE_DB_PATH = db_path
        # "ira" (current manager_key) is deliberately NEVER created -- gone.
        await _storage.manager_add(manager_key="oldmgr", display_name="OldMgrCurrent", phone="", status="active",
                                    session_path="", db_path=db_path, workdir="", log_path="")
        await _storage.manager_set_fields("oldmgr", telegram_username="oldmgr_current_user")
        ns = build_managerbot_ns(db_path)
        text = ns["_format_duplicate_card"](_stale_event_row())

        check("B7-1. current manager (row gone) falls back to the stale payload 'pNpLqhW' (no live row to prefer)",
              "pNpLqhW" in text, text)
        check("B7-2. previous manager (row exists) shows LIVE identity 'OldMgrCurrent / @oldmgr_current_user'",
              "OldMgrCurrent / @oldmgr_current_user" in text, text)
        check("B7-3. previous manager does NOT show the stale payload name 'StaleOldMgrName'",
              "StaleOldMgrName" not in text, text)
    finally:
        await cleanup_env(tmp_root)


async def test_managerbot_mutation_proof_revert_to_payload():
    print("\n-- MUT-MANAGERBOT: fault-inject a lookup that never improves on the bare key --")
    tmp_root, db_path = make_temp_env("idcons_mb_mut_")
    try:
        await seed_managerbot_managers_table(db_path)

        # Faking _sched_manager_label_mb to always return the bare key reproduces the
        # PRE-FIX code path exactly: _format_duplicate_card's own guard is
        # `if cur_current and cur_current != cur_key:` -- when the lookup can never beat
        # the bare key, that guard never fires, and control falls through to the
        # payload-only branches unchanged from before this patch.
        def always_bare_key(mk: str) -> str:
            return mk

        ns = build_managerbot_ns(db_path, fake_label_resolver=always_bare_key)
        text = ns["_format_duplicate_card"](_stale_event_row())

        check(
            "MUT-MANAGERBOT. with the live lookup neutralized, the STALE payload names "
            "reappear (proves B1/B3's 'current identity' assertions are load-bearing)",
            "pNpLqhW" in text and "StaleOldMgrName" in text and "Ира | @ira_tg_t" not in text,
            text,
        )
    finally:
        await cleanup_env(tmp_root)


async def main() -> int:
    await test_partner_current_identity_label_from_event()
    await test_partner_current_identity_fmt_lead_notification()
    await test_partner_fallback_when_manager_row_missing()
    await test_partner_never_touches_history_table()
    await test_partner_cache_isolation_between_managers()
    await test_partner_cache_key_is_normalized()
    await test_partner_ttl_staleness_window()
    await test_partner_mutation_proof_revert_to_snapshot()

    await test_managerbot_duplicate_card_current_identity()
    await test_managerbot_duplicate_card_fallback_when_row_missing()
    await test_managerbot_duplicate_card_mixed_fallback_both_directions()
    await test_managerbot_mutation_proof_revert_to_payload()

    print()
    if FAILURES:
        print(f"RESULT: FAIL ({len(FAILURES)} failing check(s)): {FAILURES}")
        return 1
    print("RESULT: PASS (all checks green)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
