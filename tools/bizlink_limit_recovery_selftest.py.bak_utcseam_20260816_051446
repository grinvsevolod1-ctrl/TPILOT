# -*- coding: utf-8 -*-
"""tools/bizlink_limit_recovery_selftest.py -- offline selftest for the
RUNTIME READINESS + BUSINESS LINK RECOVERY RELIABILITY release candidate,
waves L3-L7 (business-link tg_limit recovery).

Scope. The batch recovery path is NOT broken -- a read-only production audit
found 95 `bizlink_delete_audit` rows with mode='tg_limit_auto', 86 of them
`deleted_ok`, the most recent succeeding on 2026-08-04T16:55:50 (vinchi,
deleted_count=15, retry completed). What this selftest pins down are the
reliability defects found alongside it:

  G7  the broad `except Exception` in _manager_recover_link_limit collapses any
      internal failure into reason="exception" with deleted=0 and prints
      NOTHING; since 2026-08-01 a W3TimezoneError from _kyiv_now() is
      deliberately routed here, so a tzdata fault would kill auto-deletion
      silently. Fail-closed is correct; invisible is not.
  G8  the bare "too many" token in _bizlink_classify_error would classify
      PeerFloodError('Too many requests') as tg_limit and kick off a pointless
      deletion sweep. CHATLINKS_TOO_MUCH must stay tg_limit.
  G3/G4 bizlinks_select_delete_candidates applies `LIMIT count` BEFORE main.py's
      `target_date < today(Kyiv)` eligibility filter, and the recovery loop
      re-issues the identical query with no OFFSET. If the 15 oldest-by-
      created_at rows are all ineligible, Layer 1 yields nothing and breaks out
      immediately. It also orders by created_at while the business notion of
      "oldest" is target_date, and an empty created_at sorts LAST (sentinel
      '9'), i.e. is treated as newest.
  G5  recovered_link_limit latches regardless of outcome, so a batch whose 15
      oldest slugs are all dead on Telegram (CHATLINK_SLUG_EXPIRED) stops for
      good -- observed once: audit id 17, mihha, 2026-07-05, deleted 0 /
      failed 15.
  G6  deleted_slugs.append(slug) runs BEFORE _bizlink_delete_safe_log inside the
      same try, so a raising logger would put one slug in both deleted_slugs
      and failed_slugs and flip ok to False.
  G1  the bizlink_create_one branch has no recovery at all.

Invariants that must stay green throughout: today/future/current-target_date
links are never auto-deleted, foreign titles are never touched, expired slugs
never count as freed capacity, exactly one create retry per slot, no unbounded
loop, W3TimezoneError stays fail-closed, and `by_date`/`last_n` selection is
unchanged.

Technique: main.py cannot be imported standalone, so every function under test
is extracted via ast.parse + ast.unparse + exec(), the idiom already used by
tools/bizlink_delete_result_selftest.py. storage.py IS importable and is used
for real against a throwaway SQLite file, so the candidate-selection SQL is
exercised as written rather than re-implemented.

No Telegram. No production DB.
"""
from __future__ import annotations

import ast
import asyncio
import io
import contextlib
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()

import storage as _storage  # noqa: E402  (real module, throwaway DB)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES: list = []
PENDING: list = []

TODAY = "2026-08-05"          # fixed "today" in Kyiv for every scenario
CUR_TARGET = "2026-08-06"     # the batch currently being created


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def check_post_fix(label: str, condition: bool, detail: str = "",
                   *, unavailable: bool = False) -> None:
    if unavailable:
        print(f"[PEND] {label}  (waiting for its wave)")
        PENDING.append(label)
        return
    check(label, condition, detail)


# ----------------------------------------------------------------------
# AST extraction (same contract as tools/bizlink_delete_result_selftest.py)
# ----------------------------------------------------------------------

_PARSE_CACHE: dict = {}


def _cached_parse(src: str):
    cached = _PARSE_CACHE.get(id(src))
    if cached is not None and cached[0] is src:
        return cached[1]
    tree = ast.parse(src)
    _PARSE_CACHE[id(src)] = (src, tree)
    return tree


def _top_level_node(src: str, name: str):
    tree = _cached_parse(src)
    nodes = [n for n in tree.body if getattr(n, "name", None) == name]
    if not nodes:
        raise AssertionError(f"{name} not found as a top-level def")
    return nodes[-1]  # last-wins, per this project's override convention


class MutationAnchorError(AssertionError):
    """Anchor missing or ambiguous -- must never be mistaken for a caught
    mutation."""


def _apply_subs(src: str, subs) -> str:
    if subs and isinstance(subs[0], str):
        subs = [subs]
    for old, new in subs:
        count = src.count(old)
        if count == 0:
            raise MutationAnchorError(f"mutation anchor not found: {old!r}")
        if count > 1:
            raise MutationAnchorError(
                f"mutation anchor is not unique ({count} occurrences): {old!r}")
        src = src.replace(old, new, 1)
    return src


RECOVERY_FNS = ("_bizlink_slug_from_url", "_bizlink_classify_error",
                "_bizlink_delete_safe_sanitize", "_bizlink_delete_safe_log",
                "_manager_delete_business_links", "_manager_recover_link_limit")


# ----------------------------------------------------------------------
# Fakes
# ----------------------------------------------------------------------

class FakeDelReq:
    """Stand-in for telethon's DeleteBusinessChatLinkRequest."""

    def __init__(self, slug):
        self.slug = slug


class FloodWaitError(Exception):
    def __init__(self, seconds=0):
        super().__init__(f"flood wait {seconds}")
        self.seconds = seconds


class BadRequestError(Exception):
    """Stand-in for telethon BadRequestError -- only repr() text matters."""


class PeerFloodError(Exception):
    pass


class NoSleep:
    """asyncio shim: the real code sleeps 3 s between deletes, which would make
    this suite take minutes. Only sleep() is neutralised; CancelledError keeps
    its real identity so the `except asyncio.CancelledError: raise` path is
    still exercised faithfully."""
    CancelledError = asyncio.CancelledError

    @staticmethod
    async def sleep(_secs):
        return None


class FakeTelegram:
    """Scripted Telegram side. `behaviors` maps slug -> None (deleted) or an
    Exception instance to raise. Records every slug actually sent."""

    def __init__(self, behaviors: Optional[dict] = None, default=None):
        self.behaviors = dict(behaviors or {})
        self.default = default
        self.deleted: List[str] = []
        self.attempts: List[str] = []

    async def __call__(self, req):
        slug = getattr(req, "slug", "")
        self.attempts.append(slug)
        exc = self.behaviors.get(slug, self.default)
        if exc is not None:
            raise exc
        self.deleted.append(slug)
        return {"ok": True}


def make_db(rows: List[dict]) -> str:
    """Throwaway DB seeded with explicit bizlinks rows.

    row keys: manager_key, target_date, slot_no, slug, status, created_at.
    Values are written verbatim so a test can express an empty created_at or an
    empty target_date -- exactly the cases the production selector mishandles.
    """
    fd, path = tempfile.mkstemp(prefix="tpilot_bizrec_", suffix=".db",
                                dir=tempfile.gettempdir())
    os.close(fd)
    _storage.ensure_bizlink_tables(path)
    _storage.ensure_bizlink_delete_tables(path)
    # bizlinks has UNIQUE(manager_key, target_date, slot_no) and CHECK(slot_no
    # BETWEEN 1 AND 15). Slots are assigned per (manager_key, target_date) group
    # so a test can just declare the rows it needs without hand-numbering them.
    con = sqlite3.connect(path)
    seen: dict = {}
    try:
        for r in rows:
            mk = r.get("manager_key", "mgr")
            td = r.get("target_date", "")
            slot = seen.get((mk, td), 0) + 1
            seen[(mk, td)] = slot
            if slot > 15:
                raise AssertionError(
                    f"more than 15 rows for ({mk}, {td}): the real schema caps a "
                    f"manager/date at 15 slots -- spread the fixture over more dates")
            con.execute(
                "INSERT INTO bizlinks(manager_key, target_date, slot_no, title,"
                " message_text, link_url, slug, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (mk, td, slot,
                 r.get("title", f"TPilot {td} #{slot:02d}"),
                 "msg", f"https://t.me/m/{r.get('slug','')}", r.get("slug", ""),
                 r.get("status", "created"), r.get("created_at", ""),
                 r.get("created_at", "")),
            )
        con.commit()
    finally:
        con.close()
    return path


def build_env(db_path: str, *, telegram: FakeTelegram,
              tg_list_result: Optional[dict] = None,
              kyiv_exc: Optional[Exception] = None,
              del_available: bool = True) -> dict:
    """Namespace for the extracted recovery functions."""

    def _kyiv_now():
        if kyiv_exc is not None:
            raise kyiv_exc
        return datetime.fromisoformat(TODAY + "T12:00:00")

    async def _manager_list_business_links(payload):
        if tg_list_result is None:
            return {"ok": False, "links": [], "count": 0, "error": "not stubbed"}
        return dict(tg_list_result)

    return {
        "asyncio": NoSleep,
        "datetime": datetime,
        "timedelta": timedelta,
        "re": re,
        "Any": Any, "Dict": Dict, "List": List,
        "Optional": Optional, "Tuple": Tuple,
        "TPILOT_DB_PATH": db_path,
        "client": telegram,
        "FloodWaitError": FloodWaitError,
        "_M213D3A_TG_DEL_AVAILABLE": bool(del_available),
        "_M213D3A_DelBizLinkReq": FakeDelReq,
        "_bsd3a_select_candidates": _storage.bizlinks_select_delete_candidates,
        "_bsd3a_mark_deleted": _storage.bizlink_mark_deleted,
        "_bsd3a_mark_delete_failed": _storage.bizlink_mark_delete_failed,
        "_bsd3a_audit_add": _storage.bizlink_delete_audit_add,
        "_kyiv_now": _kyiv_now,
        "_manager_list_business_links": _manager_list_business_links,
    }


def load_recovery(env: dict, mutations: Optional[dict] = None) -> dict:
    parts = []
    for nm in RECOVERY_FNS:
        src = ast.unparse(_top_level_node(MAIN_SRC, nm))
        if mutations and nm in mutations:
            src = _apply_subs(src, mutations[nm])
        parts.append(src)
    ns = dict(env)
    exec(compile("\n\n".join(parts), "<main.py:bizlink_recovery>", "exec"), ns)
    return ns


def recover(ns, *, manager_key="mgr", needed=1, target_date=CUR_TARGET):
    return asyncio.run(ns["_manager_recover_link_limit"](
        manager_key, target_date, needed=needed))


def slugs_of(db_path: str, status: str = "created") -> set:
    con = sqlite3.connect(db_path)
    try:
        return {r[0] for r in con.execute(
            "SELECT slug FROM bizlinks WHERE status=?", (status,))}
    finally:
        con.close()


# ----------------------------------------------------------------------
# Confirmed-defect checks
# ----------------------------------------------------------------------

def run_defect_checks() -> None:
    print("\n--- confirmed-defect checks ---")

    # G8: classifier must not treat a generic flood phrase as the link cap.
    env = build_env(make_db([]), telegram=FakeTelegram())
    ns = load_recovery(env)
    cls = ns["_bizlink_classify_error"]
    check("G8a CHATLINKS_TOO_MUCH stays tg_limit",
          cls(BadRequestError("RPCError 400: CHATLINKS_TOO_MUCH (caused by "
                              "CreateBusinessChatLinkRequest)")) == "tg_limit")
    check("G8b PeerFloodError('Too many requests') is NOT tg_limit",
          cls(PeerFloodError("Too many requests")) != "tg_limit",
          f"got {cls(PeerFloodError('Too many requests'))!r} -- would trigger a "
          f"pointless deletion sweep")

    # G7: an internal failure must leave a visible, sanitized trace.
    # W3TimezoneError stays fail-closed (0 deletions) -- that is the invariant;
    # what is missing today is any log line at all.
    db = make_db([{"slug": "old1", "target_date": "2026-07-01",
                   "created_at": "2026-07-01T10:00:00"}])
    tg = FakeTelegram()
    ns = load_recovery(build_env(db, telegram=tg,
                                 kyiv_exc=RuntimeError("w3: cannot load timezone")))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = recover(ns)
    out = buf.getvalue()
    check("G7a W3 timezone failure stays fail-closed (0 deletions)",
          res.get("reason") == "exception" and int(res.get("deleted") or 0) == 0
          and len(tg.deleted) == 0, repr(res))
    check("G7b the swallowed recovery exception is logged",
          "bizlink" in out.lower() and "exception" in out.lower(),
          f"nothing printed; only a 500-char audit column would carry it. stdout={out!r}")
    check("G7c the recovery log carries no raw secrets",
          "https://" not in out and ".session" not in out, f"stdout={out!r}")

    # G3/G4: LIMIT is applied before the date filter, and the loop cannot page
    # deeper past ineligible rows. 15 future-dated rows (created earliest) hide
    # one genuinely old, deletable row.
    rows = [{"slug": f"future{i}", "target_date": "2026-08-31",
             "created_at": f"2026-08-01T10:00:{i:02d}", "slot_no": i}
            for i in range(1, 16)]
    rows.append({"slug": "reallyold", "target_date": "2026-06-01",
                 "created_at": "2026-08-02T10:00:00", "slot_no": 15})
    db = make_db(rows)
    tg = FakeTelegram()
    ns = load_recovery(build_env(db, telegram=tg))
    res = recover(ns, needed=1)
    check("G3 an eligible old link behind a full LIMIT window is still found",
          tg.deleted == ["reallyold"],
          f"deleted={tg.deleted} reason={res.get('reason')!r}: the 15 newest-by-"
          f"business-date rows filled the LIMIT window and hid the only "
          f"deletable link")

    # G4: "oldest" must mean oldest business date, not oldest row-creation time.
    rows = [
        {"slug": "newerdate", "target_date": "2026-08-01", "created_at": "2026-07-01T00:00:00", "slot_no": 1},
        {"slug": "olderdate", "target_date": "2026-07-01", "created_at": "2026-07-20T00:00:00", "slot_no": 2},
    ]
    db = make_db(rows)
    tg = FakeTelegram()
    ns = load_recovery(build_env(db, telegram=tg))
    recover(ns, needed=1)
    check("G4a oldest means oldest target_date, not oldest created_at",
          tg.deleted[:1] == ["olderdate"], f"deleted={tg.deleted}")

    # G4b: a row with an empty created_at must be treated as oldest, not newest.
    rows = [{"slug": f"pad{i}", "target_date": "2026-07-10",
             "created_at": f"2026-07-10T00:00:{i:02d}"}
            for i in range(1, 15)]
    rows.append({"slug": "nodate", "target_date": "2026-07-10", "created_at": ""})
    db = make_db(rows)
    tg = FakeTelegram()
    ns = load_recovery(build_env(db, telegram=tg))
    recover(ns, needed=1)
    check("G4b empty created_at sorts as oldest, not newest",
          tg.deleted[:1] == ["nodate"], f"deleted={tg.deleted}")

    # G5: 15 oldest slugs all dead on Telegram -> a bounded second pass must
    # reach the live one behind them. Expired slugs never count as freed.
    expired = BadRequestError("RPCError 400: CHATLINK_SLUG_EXPIRED (caused by "
                              "DeleteBusinessChatLinkRequest)")
    rows = [{"slug": f"dead{i}", "target_date": "2026-07-01",
             "created_at": f"2026-07-01T00:00:{i:02d}", "slot_no": i}
            for i in range(1, 16)]
    rows.append({"slug": "alive", "target_date": "2026-07-02",
                 "created_at": "2026-07-02T00:00:00", "slot_no": 15})
    db = make_db(rows)
    tg = FakeTelegram(behaviors={f"dead{i}": expired for i in range(1, 16)})
    ns = load_recovery(build_env(db, telegram=tg))
    res = recover(ns, needed=1)
    check("G5 a bounded second pass reaches a live link behind 15 dead slugs",
          int(res.get("deleted") or 0) == 1 and "alive" in tg.deleted,
          f"deleted={res.get('deleted')} expired={res.get('expired_cleaned')} "
          f"reason={res.get('reason')!r}")

    # G6: a raising logger must never turn a successful delete into a failure.
    db = make_db([{"slug": "old1", "target_date": "2026-07-01",
                   "created_at": "2026-07-01T00:00:00"}])
    tg = FakeTelegram()
    ns = load_recovery(
        build_env(db, telegram=tg),
        # Fault injected ONLY on the success path, so the test isolates
        # "a logger fault must not corrupt a successful delete" from the
        # separate question of a logger fault inside the error handler.
        mutations={"_bizlink_delete_safe_log":
                   ("try:", "if result == 'deleted':\n        raise RuntimeError('logger down')\n    try:")})
    out = asyncio.run(ns["_manager_delete_business_links"]({
        "manager_key": "mgr", "mode": "oldest_n_auto", "target_date": CUR_TARGET,
        "slugs": ["old1"], "delay_between_deletes_sec": 1,
    }))
    both = set(out.get("deleted_slugs") or []) & set(out.get("failed_slugs") or [])
    check("G6 a logger fault never turns a real delete into a failed one",
          int(out.get("deleted") or 0) == 1 and int(out.get("failed") or 0) == 0
          and not both,
          f"deleted={out.get('deleted')} failed={out.get('failed')} both={both}")

    # G1: single-link creation must recover on tg_limit too.
    one_src = ast.unparse(_top_level_node(MAIN_SRC, "_manager_command_loop"))
    branch = one_src.split("bizlink_create_one")[-1].split("elif command ==")[0]
    # Look for the CALL, not the bare name: the callable(globals().get(...))
    # guard mentions the name as a string and would mask a removed call.
    check("G1 the bizlink_create_one path recovers on tg_limit",
          "_manager_recover_link_limit(" in branch,
          "the single-create branch has no recovery: on tg_limit it marks the "
          "slot failed and returns, without deleting anything or retrying")


# ----------------------------------------------------------------------
# Invariants -- green before AND after every wave
# ----------------------------------------------------------------------

def run_invariant_checks() -> None:
    print("\n--- invariants ---")

    # No limit reached -> recovery is simply never invoked by the batch path.
    batch_src = ast.unparse(_top_level_node(MAIN_SRC, "_manager_create_15_business_links"))
    check("I0 recovery is gated on error_class == 'tg_limit'",
          "'tg_limit'" in batch_src and "recovered_link_limit" in batch_src)

    # Today / future / current target date are never auto-deleted.
    rows = [
        {"slug": "today", "target_date": TODAY, "created_at": "2026-01-01T00:00:00", "slot_no": 1},
        {"slug": "future", "target_date": "2026-09-09", "created_at": "2026-01-01T00:00:01", "slot_no": 2},
        {"slug": "curtarget", "target_date": CUR_TARGET, "created_at": "2026-01-01T00:00:02", "slot_no": 3},
    ]
    db = make_db(rows)
    tg = FakeTelegram()
    ns = load_recovery(build_env(db, telegram=tg))
    res = recover(ns, needed=5)
    check("I1 today / future / current-target links are never deleted",
          tg.deleted == [] and int(res.get("deleted") or 0) == 0, f"deleted={tg.deleted}")

    # Empty target_date is never a candidate.
    db = make_db([{"slug": "nodate", "target_date": "", "created_at": "2026-01-01T00:00:00"}])
    tg = FakeTelegram()
    ns = load_recovery(build_env(db, telegram=tg))
    recover(ns, needed=5)
    check("I2 a row with an empty target_date is never a candidate",
          tg.deleted == [], f"deleted={tg.deleted}")

    # Expired-only sweep must not report freed capacity.
    expired = BadRequestError("RPCError 400: CHATLINK_SLUG_EXPIRED (caused by "
                              "DeleteBusinessChatLinkRequest)")
    db = make_db([{"slug": "d1", "target_date": "2026-07-01", "created_at": "2026-07-01T00:00:00"}])
    tg = FakeTelegram(behaviors={"d1": expired})
    ns = load_recovery(build_env(db, telegram=tg))
    res = recover(ns, needed=1)
    check("I3 expired slugs never count as freed capacity",
          int(res.get("deleted") or 0) == 0 and int(res.get("expired_cleaned") or 0) == 1
          and not res.get("ok"), repr(res))
    check("I3b an expired slug is soft-deleted so oldest_n stops re-selecting it",
          "d1" not in slugs_of(db, "created"),
          f"still selectable: {slugs_of(db, 'created')}")

    # FloodWait stops the sweep immediately.
    db = make_db([{"slug": f"o{i}", "target_date": "2026-07-01",
                   "created_at": f"2026-07-01T00:00:{i:02d}", "slot_no": i}
                  for i in range(1, 6)])
    tg = FakeTelegram(behaviors={"o1": FloodWaitError(42)})
    ns = load_recovery(build_env(db, telegram=tg))
    res = recover(ns, needed=5)
    check("I4 FloodWait stops the sweep immediately",
          res.get("reason") == "flood_wait" and len(tg.attempts) == 1, repr(res))

    # Layer 2 protects foreign titles.
    db = make_db([])
    tg = FakeTelegram()
    ns = load_recovery(build_env(
        db, telegram=tg,
        tg_list_result={"ok": True, "links": [
            {"slug": "foreign1", "title": "My own link", "url": "", "views": 0},
            {"slug": "foreign2", "title": "", "url": "", "views": 0},
            {"slug": "tp_old", "title": "TPilot 2026-07-01 #01", "url": "", "views": 0},
            {"slug": "tp_today", "title": f"TPilot {TODAY} #01", "url": "", "views": 0},
        ]}))
    res = recover(ns, needed=1)
    check("I5 Layer 2 deletes only old TPilot-titled links",
          tg.deleted == ["tp_old"], f"deleted={tg.deleted}")

    # Telegram list returning an unexpected object shape must not crash.
    db = make_db([])
    tg = FakeTelegram()
    ns = load_recovery(build_env(
        db, telegram=tg,
        tg_list_result={"ok": True, "links": [{"slug": "x"}, {"title": "TPilot 2026-07-01 #01"}]}))
    res = recover(ns, needed=1)
    check("I6 a new/partial Telegram link shape never raises",
          isinstance(res, dict) and not res.get("ok") and tg.deleted == [], repr(res))

    # Bounded: expired slugs are soft-deleted, so every batch DOES surface a
    # fresh window of candidates -- the only thing stopping an endless sweep is
    # the MAX_BATCHES / max_viewed pair. needed=5 gives delete_count=15 and
    # max_viewed=45, so a correct implementation attempts at most 45 of the 59.
    expired_all = BadRequestError("RPCError 400: CHATLINK_SLUG_EXPIRED (caused by "
                                  "DeleteBusinessChatLinkRequest)")
    rows = [{"slug": f"x{i}", "target_date": f"2026-06-{(i // 15) + 1:02d}",
             "created_at": f"2026-06-01T00:{i // 60:02d}:{i % 60:02d}"}
            for i in range(1, 60)]
    db = make_db(rows)
    tg = FakeTelegram(default=expired_all)
    ns = load_recovery(build_env(db, telegram=tg))
    res = recover(ns, needed=5)
    check("I7 the sweep is bounded even when every batch surfaces fresh candidates",
          len(tg.attempts) <= 45 and isinstance(res, dict),
          f"attempts={len(tg.attempts)} (cap is MAX_BATCHES*delete_count = 45)")

    # by_date / last_n selection modes are untouched by wave L4.
    rows = [
        {"slug": "a", "target_date": "2026-07-01", "created_at": "2026-07-01T00:00:00", "slot_no": 1},
        {"slug": "b", "target_date": "2026-07-02", "created_at": "2026-07-02T00:00:00", "slot_no": 2},
    ]
    db = make_db(rows)
    by_date = _storage.bizlinks_select_delete_candidates("mgr", "by_date",
                                                         target_date="2026-07-01", db_path=db)
    last_n = _storage.bizlinks_select_delete_candidates("mgr", "last_n", count=1, db_path=db)
    check("I8 by_date selection is unchanged",
          [r["slug"] for r in by_date] == ["a"], repr([r["slug"] for r in by_date]))
    check("I9 last_n still returns the newest row",
          [r["slug"] for r in last_n] == ["b"], repr([r["slug"] for r in last_n]))

    # Exactly one create retry per slot, and only when real capacity was freed.
    check("I10 create retry happens only when real links were deleted",
          "if recovery_deleted > 0:" in batch_src, "retry must not run on expired-only")
    check("I11 exactly one create retry per slot",
          batch_src.count("retry_res = await _manager_create_one_business_link") == 1)

    # No DB transaction may be held across a Telegram await.
    rec_src = ast.unparse(_top_level_node(MAIN_SRC, "_manager_recover_link_limit"))
    check("I12 no open DB connection is held across a Telegram await",
          "con." not in rec_src and "sqlite3" not in rec_src,
          "candidate selection must finish and close before the first delete")


# ----------------------------------------------------------------------
# Mutation controls
# ----------------------------------------------------------------------

def _m_red(label: str, fn, *, anchored_in_fix: bool = False) -> None:
    try:
        caught = not bool(fn())
    except MutationAnchorError as e:
        if anchored_in_fix:
            print(f"[PEND] {label}  (anchor lands with its wave)")
            PENDING.append(label)
            return
        print(f"[FAIL] {label}  mutation anchor drifted: {e}")
        FAILURES.append(label)
        return
    except Exception:
        caught = True
    if caught:
        print(f"[OK]   {label} (mutation caught)")
    else:
        print(f"[FAIL] {label}  mutation NOT caught")
        FAILURES.append(label)


def run_mutation_controls() -> None:
    print("\n--- mutation controls ---")

    rows = [
        {"slug": "old", "target_date": "2026-07-01", "created_at": "2026-07-01T00:00:00", "slot_no": 1},
        {"slug": "today", "target_date": TODAY, "created_at": "2026-07-02T00:00:00", "slot_no": 2},
        {"slug": "curtarget", "target_date": CUR_TARGET, "created_at": "2026-07-03T00:00:00", "slot_no": 3},
    ]

    # After wave L4 the eligibility date is enforced twice: once in SQL
    # (before_target_date, applied before LIMIT) and once in the Python filter
    # that also carries the current_target_date exclusion. That redundancy is
    # deliberate, and it means a control which removes only ONE layer proves
    # nothing about the guard -- the other layer absorbs it. So each guard gets
    # two controls: a redundancy check (single layer removed => still safe) and
    # a real mutation control (both layers removed => must be caught).
    SQL_LAYER = ("before_target_date=today_iso_r5", "before_target_date=None")
    PY_LAYER_OFF = ("str(c.get('target_date') or '') < today_iso_r5", "True")
    PY_LAYER_LTE = ("str(c.get('target_date') or '') < today_iso_r5",
                    "str(c.get('target_date') or '') <= today_iso_r5")

    def _sweep(muts):
        db = make_db(rows)
        tg = FakeTelegram()
        ns = load_recovery(build_env(db, telegram=tg),
                           mutations={"_manager_recover_link_limit": muts})
        recover(ns, needed=5)
        return set(tg.deleted)

    check("R1 removing only the Python date filter is absorbed by the SQL layer",
          _sweep([PY_LAYER_OFF]) <= {"old"}, "defense-in-depth broken")
    check("R2 removing only the SQL date bound is absorbed by the Python filter",
          _sweep([SQL_LAYER]) <= {"old"}, "defense-in-depth broken")

    _m_red("M7 date guard removed from BOTH layers",
           lambda: _sweep([SQL_LAYER, PY_LAYER_OFF]) <= {"old"})
    _m_red("M8 '<' relaxed to '<=' in BOTH layers",
           lambda: "today" not in _sweep([SQL_LAYER, PY_LAYER_LTE]))

    # M9: candidate ordering reversed -- newest deleted first.
    def m9():
        db = make_db([
            {"slug": "older", "target_date": "2026-07-01", "created_at": "2026-07-01T00:00:00", "slot_no": 1},
            {"slug": "newer", "target_date": "2026-07-20", "created_at": "2026-07-20T00:00:00", "slot_no": 2},
        ])
        tg = FakeTelegram()
        ns = load_recovery(build_env(db, telegram=tg), mutations={
            "_manager_recover_link_limit": ("candidates.sort(key=lambda c: (",
                                            "candidates.sort(reverse=True, key=lambda c: (")})
        recover(ns, needed=1)
        return tg.deleted[:1] == ["older"]
    _m_red("M9 candidate ordering reversed", m9)

    # M11: the one-recovery-per-batch latch removed.
    def m11():
        src = ast.unparse(_top_level_node(MAIN_SRC, "_manager_create_15_business_links"))
        mutated = _apply_subs(src, ("and (not recovered_link_limit)", ""))
        # Inspect only the `if` test line itself -- the assignment on the next
        # line also mentions recovered_link_limit and would mask the removal.
        guard_line = mutated.split("res_error_class ==")[1].split("\n")[0]
        return "recovered_link_limit" in guard_line
    _m_red("M11 recovered_link_limit latch removed", m11)

    # M12: BOTH iteration caps removed. MAX_BATCHES and max_viewed bind at the
    # same point by construction (max_viewed = delete_count*3, MAX_BATCHES = 3),
    # so a control that lifts only one of them proves nothing -- this lifts both.
    def m12():
        expired_all = BadRequestError("RPCError 400: CHATLINK_SLUG_EXPIRED (caused by "
                                      "DeleteBusinessChatLinkRequest)")
        db = make_db([{"slug": f"x{i}", "target_date": f"2026-06-{(i // 15) + 1:02d}",
                       "created_at": f"2026-06-01T00:{i // 60:02d}:{i % 60:02d}"}
                      for i in range(1, 60)])
        tg = FakeTelegram(default=expired_all)
        ns = load_recovery(build_env(db, telegram=tg), mutations={
            "_manager_recover_link_limit": [("MAX_BATCHES = 3", "MAX_BATCHES = 10000"),
                                            ("max_viewed = delete_count * 3",
                                             "max_viewed = delete_count * 10000")]})
        recover(ns, needed=5)
        return len(tg.attempts) <= 45
    _m_red("M12 iteration caps removed", m12)

    # M13: the broad "too many" token restored.
    def m13():
        ns = load_recovery(build_env(make_db([]), telegram=FakeTelegram()), mutations={
            "_bizlink_classify_error": ("'businesschatlinks'", "'businesschatlinks', 'too many'")})
        return ns["_bizlink_classify_error"](PeerFloodError("Too many requests")) != "tg_limit"
    _m_red("M13 broad 'too many' token restored", m13, anchored_in_fix=True)

    # M14: an expired slug allowed to count as freed capacity.
    def m14():
        expired = BadRequestError("RPCError 400: CHATLINK_SLUG_EXPIRED (caused by "
                                  "DeleteBusinessChatLinkRequest)")
        db = make_db([{"slug": "d1", "target_date": "2026-07-01",
                       "created_at": "2026-07-01T00:00:00"}])
        tg = FakeTelegram(behaviors={"d1": expired})
        ns = load_recovery(build_env(db, telegram=tg), mutations={
            "_manager_recover_link_limit":
                ("real_deleted += int(result.get('deleted') or 0)",
                 "real_deleted += int(result.get('deleted') or 0) + int(result.get('expired_cleaned') or 0)")})
        res = recover(ns, needed=1)
        return int(res.get("deleted") or 0) == 0
    _m_red("M14 expired slug counted as freed capacity", m14)

    # M10: the create retry is allowed even when nothing was actually freed.
    def m10():
        src = ast.unparse(_top_level_node(MAIN_SRC, "_manager_create_15_business_links"))
        mutated = _apply_subs(src, ("if recovery_deleted > 0:", "if True:"))
        return "if recovery_deleted > 0:" in mutated
    _m_red("M10 retry allowed when deleted=0", m10)

    # M15: wave L6 reverted -- the single-create branch loses its recovery.
    def m15():
        src = ast.unparse(_top_level_node(MAIN_SRC, "_manager_command_loop"))
        mutated = _apply_subs(src, ("await _manager_recover_link_limit(",
                                    "await _noop_recover("))
        branch = mutated.split("bizlink_create_one")[-1].split("elif command ==")[0]
        return "_manager_recover_link_limit(" in branch
    _m_red("M15 single-create recovery removed", m15)

    # M16: wave L7 reverted -- the success-path log is no longer isolated from
    # the delete result, so a logger fault can reclassify a real deletion.
    def m16():
        db = make_db([{"slug": "old1", "target_date": "2026-07-01",
                       "created_at": "2026-07-01T00:00:00"}])
        tg = FakeTelegram()
        ns = load_recovery(build_env(db, telegram=tg), mutations={
            "_bizlink_delete_safe_log":
                ("try:", "if result == 'deleted':\n        raise RuntimeError('logger down')\n    try:"),
            "_manager_delete_business_links":
                ("try:\n                _bizlink_delete_safe_log('delete', 'deleted', "
                 "manager_key=manager_key, slug=slug)\n            except Exception:\n                pass",
                 "_bizlink_delete_safe_log('delete', 'deleted', manager_key=manager_key, slug=slug)")})
        out = asyncio.run(ns["_manager_delete_business_links"]({
            "manager_key": "mgr", "mode": "oldest_n_auto", "target_date": CUR_TARGET,
            "slugs": ["old1"], "delay_between_deletes_sec": 1}))
        both = set(out.get("deleted_slugs") or []) & set(out.get("failed_slugs") or [])
        return int(out.get("failed") or 0) == 0 and not both
    _m_red("M16 success-path log no longer isolated from the delete result", m16)


def main() -> int:
    print("=" * 72)
    print("bizlink_limit_recovery_selftest -- waves L3-L7")
    print("=" * 72)
    run_defect_checks()
    run_invariant_checks()
    run_mutation_controls()
    print("\n" + "=" * 72)
    if PENDING:
        print(f"PENDING: {len(PENDING)}")
        for p in PENDING:
            print(f"  ~ {p}")
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} FAILED")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    if PENDING:
        print("RESULT: no failures, but some waves are not applied yet")
        return 2
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
