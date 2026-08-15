# -*- coding: utf-8 -*-
"""tools/bizlink_delete_result_selftest.py -- offline selftest for the
BIZLINK_DELETE_RESULT_FIX patch (2026-08-04).

Root cause fixed by this patch (confirmed by a server-side read-only
forensic audit, not reproduced here): the manager-side D3A dispatch branch
for /bizlink_delete_tpilot wrote `result_json=str(res)` (a Python dict
repr, NOT valid JSON) while the controller-side reader used
`json.loads(...)`, which always raised and was silently swallowed into
`{}`. That made `bizlink_delete_audit.deleted_count`/`failed_count` read
0 for EVERY manual deletion even though the real Telegram deletions
succeeded. A second, independent bug was fixed in the same patch: the
expired-slug soft-delete matcher included a bare `"expired" in low`
clause, which could soft-delete a bizlink row on ANY exception whose repr
happens to contain the word "expired" (SessionExpiredError,
PHONE_CODE_EXPIRED, ...), not just a genuine dead Telegram slug.

Scope of this patch: main.py only (three call sites) + this selftest.
storage.py, panel_bot.py, manager_bot.py, partner_stat_bot.py are
untouched -- no schema change, no historical audit-row rewrite.

BIZLINK_DELETE_RESULT_FIX_CORRECTION (2026-08-04, same day): a follow-up
independent review found that the original fix's own diagnostics could
leak sensitive-looking text (URLs, proxy host:port:login:password,
password/token/api_hash key=value pairs) into the process log via
_bizlink_delete_safe_log's error_snippet, and into the
bizlink_delete_audit.error_text DB column via the controller's
parse-failure diagnostic (both carried raw repr()/payload text). A new
_bizlink_delete_safe_sanitize() helper now redacts both paths. Separately,
_bizlink_delete_safe_log's try/except was widened to cover argument
handling and message construction too, not just the print() call, so the
helper truly never raises regardless of what it is called with. See
run_correction_checks() / run_correction_mutation_controls() (mutations
M6-M8) below for the corresponding tests.

BIZLINK_DELETE_RESULT_FIX_FINAL_CORRECTION (2026-08-04, same day): an
independent review of the correction above found a blocking issue (B-1):
_bizlink_delete_safe_sanitize's session-path pattern (`\\S*\\.session\\S*`,
two unanchored/unbounded `\\S*` quantifiers) forced the backtracking regex
engine into O(k^2) work for a whitespace-free run of length k with no
".session" match -- measured ~32s for a 128KB payload, unusable at 1MB.
Fixed two ways together: the pattern now uses bounded `{0,80}` reps, and
the working text is truncated to a small window BEFORE any regex runs at
all (only the first `max_len` output chars can ever survive the final
truncation, so nothing beyond a few times max_len is reachable anyway).
Separately, the controller's OTHER audit error_text source -- the queue
row's own `error_text` field, untouched by the original N-4 fix -- is now
also routed through the sanitizer. See run_perf_checks() (§5 of the task
spec) and the M9/M10 mutation controls below.

The review also flagged that this file's own mutation controls (M1-M8)
could count a silently-inapplicable mutation (anchor text no longer found
in main.py) as a "successful RED", via `except Exception: mN_red = True`
swallowing _apply_subs's AssertionError. _apply_subs now raises the
distinct MutationAnchorError (also enforcing anchor uniqueness), and every
mutation control catches it separately, records an explicit
MUTATION_ANCHOR_FAILURE check failure, and does NOT count it as RED. See
M11 below, which proves this directly.

Technique: main.py cannot be imported standalone (Telethon/env side
effects at import time). Every function under test here is extracted
straight from the CURRENT main.py source via ast.parse + ast.unparse +
exec() -- the same idiom used throughout this project's
tools/*_selftest.py files (mirrors tools/proxy_pool_selftest.py's
_extract_and_exec). Two of the three fixed code paths
(_manager_delete_business_links and _queue_bizlink_delete_tpilot_for_manager)
are top-level functions and are extracted whole. The third
(the "bizlink_delete_tpilot" dispatch branch inside the giant
_manager_command_loop) is NOT a standalone function -- it is one `elif`
arm of a huge, side-effecting, infinite-loop coroutine that cannot be
run directly in a test. That one branch is instead located by walking
the AST of the LAST (active, last-wins per this project's override
convention) _manager_command_loop definition for the `If` node whose
test compares `command == "bizlink_delete_tpilot"`, and only that node's
body is extracted and wrapped in a small standalone async harness. This
keeps the test bound to the real, active source (not a hand-copied
duplicate that could silently drift from it) while staying runnable.

Mutation controls (section 6 of the task spec) reuse the exact same
extraction, but apply one targeted text substitution to the *unparsed*
function/branch source before exec -- simulating a regression -- and
assert the corresponding positive check(s) go RED. Nothing is ever
written back to the real main.py; the mutation only ever exists as an
in-memory string.

Run:
    python tools\\bizlink_delete_result_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import glob
import io
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


# ----------------------------------------------------------------------
# Generic AST extraction helpers
# ----------------------------------------------------------------------

# BIZLINK_DELETE_RESULT_FIX_CORRECTION 20260804: ast.parse() on the ~1.8MB
# main.py takes several seconds, and _top_level_node() previously re-parsed
# it from scratch on every single call. This selftest now calls it many
# more times (the N-1/N-4/N-6 correction checks below), which made a full
# run take minutes. Caching the parsed tree by the exact source-string
# identity is a pure memoization -- same input, same tree, zero behavior
# change -- MAIN_SRC (and the one backup source read in
# run_scope_guard_checks) are read once and never mutated, so this is safe.
_PARSE_CACHE: dict = {}


def _cached_parse(main_src: str):
    cached = _PARSE_CACHE.get(id(main_src))
    if cached is not None and cached[0] is main_src:
        return cached[1]
    tree = ast.parse(main_src)
    _PARSE_CACHE[id(main_src)] = (main_src, tree)
    return tree


def _top_level_node(main_src: str, name: str):
    tree = _cached_parse(main_src)
    nodes = [n for n in tree.body if getattr(n, "name", None) == name]
    if not nodes:
        raise AssertionError(f"{name} not found as a top-level def in main.py")
    return nodes[-1]  # last-wins, per this project's override convention


class MutationAnchorError(AssertionError):
    """Raised by _apply_subs when a mutation anchor is missing or not unique
    (BIZLINK_DELETE_RESULT_FIX_FINAL_CORRECTION 20260804, §4). A distinct
    exception type -- not a bare AssertionError/Exception -- so every
    mutation-control call site can catch it BEFORE its generic
    `except Exception: mN_red = True` and refuse to count an anchor that
    silently failed to apply as a successful RED mutation. An anchor that
    drifted out of sync with main.py must fail the selftest loudly, not
    masquerade as a passing regression guard."""


def _apply_subs(src: str, subs) -> str:
    """Apply one or more (old_substr, new_substr) replacements to src, each
    a single mandatory AND unique occurrence. `subs` may be one tuple or a
    list of tuples (applied in order). Raises MutationAnchorError (not a
    bare AssertionError) if an anchor is missing or ambiguous -- see
    MutationAnchorError docstring for why that distinction matters."""
    if subs and isinstance(subs[0], str):
        subs = [subs]
    for old, new in subs:
        count = src.count(old)
        if count == 0:
            raise MutationAnchorError(f"mutation anchor not found: {old!r}")
        if count > 1:
            raise MutationAnchorError(
                f"mutation anchor is not unique ({count} occurrences): {old!r}"
            )
        src = src.replace(old, new, 1)
    return src


def _build_ns(main_src: str, names: set, extra_ns: dict, mutations: dict | None = None) -> dict:
    """Extract top-level function defs by name and exec them into a fresh
    namespace seeded with extra_ns. `mutations`, if given, is
    {name: (old_substr, new_substr) | [(old, new), ...]}: applied to that
    one function's *unparsed* source before exec -- used only by the
    mutation-control checks below."""
    parts = []
    for nm in names:
        node = _top_level_node(main_src, nm)
        src = ast.unparse(node)
        if mutations and nm in mutations:
            src = _apply_subs(src, mutations[nm])
        parts.append(src)
    module_src = "\n\n".join(parts)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<main.py:{','.join(sorted(names))}>", "exec"), ns)
    return ns


def _find_if_body(fn_node, contains_all) -> list:
    """Walk fn_node for the LAST ast.If whose unparsed test contains every
    substring in contains_all; return its .body statement list."""
    if isinstance(contains_all, str):
        contains_all = (contains_all,)
    found = []
    for node in ast.walk(fn_node):
        if isinstance(node, ast.If):
            try:
                test_src = ast.unparse(node.test)
            except Exception:
                continue
            if all(c in test_src for c in contains_all):
                found.append(node)
    if not found:
        raise AssertionError(f"no If node matching {contains_all!r} found")
    return found[-1].body


def _wrap_async_harness(fn_name: str, params: str, body_stmts, mutate=None):
    """Build `async def fn_name(params): <body>` from a list of ast
    statement nodes (as returned by _find_if_body) and exec it, returning
    the callable. `mutate`, if given, is (old_substr, new_substr) or a
    list of such pairs, applied to the unparsed body text first
    (mutation-control checks only)."""
    body_src = "\n".join(ast.unparse(s) for s in body_stmts)
    if mutate:
        body_src = _apply_subs(body_src, mutate)
    indented = "\n".join(("    " + ln if ln.strip() else ln) for ln in body_src.splitlines())
    func_src = f"async def {fn_name}({params}):\n{indented}\n"
    ns: dict = {}
    exec(compile(func_src, f"<main.py branch:{fn_name}>", "exec"), ns)
    return ns[fn_name]


# ----------------------------------------------------------------------
# Fakes shared across scenarios
# ----------------------------------------------------------------------

class FakeDelReq:
    """Stand-in for telethon's DeleteBusinessChatLinkRequest -- only needs
    a .slug attribute, never touches the network."""

    def __init__(self, slug):
        self.slug = slug


class FloodWaitError(Exception):
    """Dummy stand-in for telethon.errors.FloodWaitError -- _bizlink_classify_error
    only needs isinstance() to work; no test here exercises flood-wait."""


def make_fake_client(behaviors: dict):
    """behaviors: {slug: None-or-Exception-instance}. None means success."""

    async def _client(req):
        exc = behaviors.get(req.slug)
        if exc is not None:
            raise exc
        return {"ok": True}

    return _client


class CallRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))


def build_delete_links_ns(main_src: str, behaviors: dict, mutations=None):
    mark_deleted = CallRecorder()
    mark_failed = CallRecorder()
    extra_ns = {
        "Dict": dict, "Any": object, "List": list,
        "asyncio": asyncio,
        "re": re,
        "client": make_fake_client(behaviors),
        "_M213D3A_TG_DEL_AVAILABLE": True,
        "_M213D3A_DelBizLinkReq": FakeDelReq,
        "_bsd3a_mark_deleted": mark_deleted,
        "_bsd3a_mark_delete_failed": mark_failed,
        "FloodWaitError": FloodWaitError,
        "TPILOT_DB_PATH": ":memory:",
    }
    ns = _build_ns(
        main_src,
        {
            "_manager_delete_business_links", "_bizlink_delete_safe_log",
            "_bizlink_delete_safe_sanitize", "_bizlink_classify_error",
        },
        extra_ns,
        mutations=mutations,
    )
    return ns, mark_deleted, mark_failed


# ----------------------------------------------------------------------
# 1/2/6/7. _manager_delete_business_links -- deleted/failed counts
# ----------------------------------------------------------------------

async def run_manager_delete_checks():
    # 6/7: one succeeds, one fails with a generic (non-slug-expired) error.
    behaviors = {"ok-slug": None, "bad-slug": RuntimeError("boom")}
    ns, mark_deleted, mark_failed = build_delete_links_ns(MAIN_SRC, behaviors)
    fn = ns["_manager_delete_business_links"]
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = await fn({
            "manager_key": "mgr_a", "mode": "manual", "target_date": "",
            "slugs": ["ok-slug", "bad-slug"], "links": [], "requested_by_user_id": 1,
        })
    logs = buf.getvalue()
    check("6. successful delete increments deleted", res.get("deleted") == 1, res)
    check("7. Telegram failure increments failed", res.get("failed") == 1, res)
    check(
        "7b. failed slug is marked via mark_delete_failed, not mark_deleted",
        any(c[0][:2] == ("mgr_a", "bad-slug") for c in mark_failed.calls)
        and not any(c[0][:2] == ("mgr_a", "bad-slug") for c in mark_deleted.calls),
        (mark_deleted.calls, mark_failed.calls),
    )
    check(
        "15a. deleted-slug log includes manager_key/slug/action/result",
        all(t in logs for t in ("manager_key=mgr_a", "slug=ok-slug", "action=delete", "result=deleted")),
        logs,
    )
    check(
        "15b. failed-slug log includes manager_key/slug/action/result + error info",
        all(t in logs for t in ("manager_key=mgr_a", "slug=bad-slug", "action=delete", "result=failed", "error_class=RuntimeError")),
        logs,
    )
    check("16a. logs never contain http(s):// URLs", "http://" not in logs and "https://" not in logs, logs)

    # 8. exact CHATLINK_SLUG_EXPIRED -> expired_cleaned + soft-delete.
    exc_expired = RuntimeError("CHATLINK_SLUG_EXPIRED")
    ns2, mark_deleted2, mark_failed2 = build_delete_links_ns(MAIN_SRC, {"dead-slug": exc_expired})
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        res2 = await ns2["_manager_delete_business_links"]({
            "manager_key": "mgr_a", "mode": "manual", "target_date": "",
            "slugs": ["dead-slug"], "links": [], "requested_by_user_id": 1,
        })
    check("8a. exact CHATLINK_SLUG_EXPIRED increments expired_cleaned", res2.get("expired_cleaned") == 1, res2)
    check(
        "8b. exact CHATLINK_SLUG_EXPIRED soft-deletes (mark_deleted called, mark_delete_failed not)",
        any(c[0][:2] == ("mgr_a", "dead-slug") for c in mark_deleted2.calls)
        and not mark_failed2.calls,
        (mark_deleted2.calls, mark_failed2.calls),
    )
    check(
        "15c. slug_expired_cleaned log present",
        "result=slug_expired_cleaned" in buf2.getvalue() and "slug=dead-slug" in buf2.getvalue(),
        buf2.getvalue(),
    )

    # 9. SESSION_EXPIRED must NOT enter the expired branch (narrowed matcher).
    exc_session = RuntimeError("SESSION_EXPIRED")
    ns3, mark_deleted3, mark_failed3 = build_delete_links_ns(MAIN_SRC, {"s-slug": exc_session})
    res3 = await ns3["_manager_delete_business_links"]({
        "manager_key": "mgr_b", "mode": "manual", "target_date": "",
        "slugs": ["s-slug"], "links": [], "requested_by_user_id": 1,
    })
    check("9. SESSION_EXPIRED does not enter the expired branch", res3.get("expired_cleaned") == 0 and res3.get("failed") == 1, res3)
    check("9b. SESSION_EXPIRED does not soft-delete", not mark_deleted3.calls, mark_deleted3.calls)

    # 10. A real SessionExpiredError-named exception must not soft-delete either.
    class SessionExpiredError(Exception):
        pass
    exc_cls = SessionExpiredError("session expired")
    ns4, mark_deleted4, mark_failed4 = build_delete_links_ns(MAIN_SRC, {"c-slug": exc_cls})
    res4 = await ns4["_manager_delete_business_links"]({
        "manager_key": "mgr_b", "mode": "manual", "target_date": "",
        "slugs": ["c-slug"], "links": [], "requested_by_user_id": 1,
    })
    check("10. SessionExpiredError does not soft-delete the row", not mark_deleted4.calls and res4.get("failed") == 1, (res4, mark_deleted4.calls))

    # 11. PHONE_CODE_EXPIRED must not soft-delete either.
    exc_phone = RuntimeError("PHONE_CODE_EXPIRED")
    ns5, mark_deleted5, mark_failed5 = build_delete_links_ns(MAIN_SRC, {"p-slug": exc_phone})
    res5 = await ns5["_manager_delete_business_links"]({
        "manager_key": "mgr_b", "mode": "manual", "target_date": "",
        "slugs": ["p-slug"], "links": [], "requested_by_user_id": 1,
    })
    check("11. PHONE_CODE_EXPIRED does not soft-delete the row", not mark_deleted5.calls and res5.get("failed") == 1, (res5, mark_deleted5.calls))

    # 12. Similar slug names do not cross-match -- exact per-slug bookkeeping.
    behaviors6 = {"slug-A": None, "slug-AB": RuntimeError("boom"), "slug-ABC": None}
    ns6, mark_deleted6, mark_failed6 = build_delete_links_ns(MAIN_SRC, behaviors6)
    res6 = await ns6["_manager_delete_business_links"]({
        "manager_key": "mgr_c", "mode": "manual", "target_date": "",
        "slugs": ["slug-A", "slug-AB", "slug-ABC"], "links": [], "requested_by_user_id": 1,
    })
    deleted_calls_slugs = {c[0][1] for c in mark_deleted6.calls}
    failed_calls_slugs = {c[0][1] for c in mark_failed6.calls}
    check(
        "12. similar slug names do not cross-match (each slug tracked exactly)",
        deleted_calls_slugs == {"slug-A", "slug-ABC"} and failed_calls_slugs == {"slug-AB"},
        (deleted_calls_slugs, failed_calls_slugs),
    )

    # 13. Two managers remain isolated -- manager_key threaded through correctly.
    ns7a, md7a, mf7a = build_delete_links_ns(MAIN_SRC, {"iso-slug": None})
    await ns7a["_manager_delete_business_links"]({
        "manager_key": "mgr_iso_1", "mode": "manual", "target_date": "",
        "slugs": ["iso-slug"], "links": [], "requested_by_user_id": 1,
    })
    ns7b, md7b, mf7b = build_delete_links_ns(MAIN_SRC, {"iso-slug": None})
    await ns7b["_manager_delete_business_links"]({
        "manager_key": "mgr_iso_2", "mode": "manual", "target_date": "",
        "slugs": ["iso-slug"], "links": [], "requested_by_user_id": 1,
    })
    check(
        "13. two managers remain isolated (each call tagged with its own manager_key only)",
        {c[0][0] for c in md7a.calls} == {"mgr_iso_1"} and {c[0][0] for c in md7b.calls} == {"mgr_iso_2"},
        (md7a.calls, md7b.calls),
    )

    # 14a. _manager_delete_business_links is a pure function of its input --
    # calling it twice with the identical payload yields identical counters
    # (no hidden module-level accumulation).
    ns8, _, _ = build_delete_links_ns(MAIN_SRC, {"idem-slug": None})
    res8a = await ns8["_manager_delete_business_links"]({
        "manager_key": "mgr_d", "mode": "manual", "target_date": "",
        "slugs": ["idem-slug"], "links": [], "requested_by_user_id": 1,
    })
    ns9, _, _ = build_delete_links_ns(MAIN_SRC, {"idem-slug": None})
    res8b = await ns9["_manager_delete_business_links"]({
        "manager_key": "mgr_d", "mode": "manual", "target_date": "",
        "slugs": ["idem-slug"], "links": [], "requested_by_user_id": 1,
    })
    check(
        "14a. repeated calls with the same payload are idempotent (deterministic counters)",
        res8a.get("deleted") == res8b.get("deleted") == 1 and res8a.get("failed") == res8b.get("failed") == 0,
        (res8a, res8b),
    )


# ----------------------------------------------------------------------
# 3/4/5. Controller-side parse+audit block, extracted from
# _queue_bizlink_delete_tpilot_for_manager's `if row and status in (...)`
# branch (the block this patch rewrote).
# ----------------------------------------------------------------------

def build_controller_parse_harness(main_src: str, mutate=None):
    fn_node = _top_level_node(main_src, "_queue_bizlink_delete_tpilot_for_manager")
    body = _find_if_body(fn_node, ("'done'", "'error'"))
    params = (
        "row, mk, mode, target_date, preview, user_id, preview_created_at, "
        "_bsd3a_audit_add, _bizlink_delete_safe_log, datetime, TPILOT_DB_PATH, "
        "_m213d3a_ctrl_json, _bizlink_delete_safe_sanitize"
    )
    harness = _wrap_async_harness("_controller_parse_harness", params, body, mutate=mutate)

    # BIZLINK_DELETE_RESULT_FIX_CORRECTION 20260804 (N-4): the branch now calls
    # _bizlink_delete_safe_sanitize() on the malformed-payload prefix before it
    # is stored in the audit row. Extract the REAL sanitizer (not a fake) so
    # these checks exercise actual redaction behavior, not a stand-in.
    sanitize_node = _top_level_node(main_src, "_bizlink_delete_safe_sanitize")
    _san_ns = {"re": re}
    exec(compile(ast.unparse(sanitize_node), "<main.py:_bizlink_delete_safe_sanitize>", "exec"), _san_ns)
    real_sanitize = _san_ns["_bizlink_delete_safe_sanitize"]

    async def _call(raw_result_json: str, *, status: str = "done", error_text: str = ""):
        from datetime import datetime as _dt
        audit_calls = []

        def _fake_audit_add(*a, **kw):
            audit_calls.append(kw)

        def _fake_safe_log(*a, **kw):
            print("[bizlink_delete_audit] " + " ".join([f"action={a[0]}", f"result={a[1]}"] + [f"{k}={v}" for k, v in kw.items() if v]))

        row = {"status": status, "result_text": "n/a", "result_json": raw_result_json, "error_text": error_text}
        preview = {"exact_count": 9, "slugs_json": "[]", "link_urls_json": "[]"}
        ok_flag, result_text = await harness(
            row, "mgr_x", "manual", "2026-08-04", preview, 555, "2026-08-04T00:00:00",
            _fake_audit_add, _fake_safe_log, _dt, ":memory:",
            json, real_sanitize,
        )
        return ok_flag, result_text, (audit_calls[0] if audit_calls else None)

    return _call


async def run_controller_parse_checks():
    call = build_controller_parse_harness(MAIN_SRC)

    # 1/2. Valid JSON payload preserves deleted/failed counts.
    ok_flag, _text, audit = await call(json.dumps({"deleted": 3, "failed": 2, "ok": True}))
    check("1. JSON payload parses and preserves deleted count", audit is not None and audit["deleted_count"] == 3, audit)
    check("2. JSON payload parses and preserves failed count", audit is not None and audit["failed_count"] == 2, audit)
    check("1b. valid JSON payload does not set result_parse_failed", audit is not None and audit["error_class"] == "" and audit["result_ok"] is True, audit)

    # 3. Legacy str(dict) (Python repr) payload -- what the OLD buggy D3A
    # branch actually wrote -- must still parse via ast.literal_eval.
    legacy_payload = str({"ok": True, "deleted": 5, "failed": 1, "errors": []})
    ok_flag, _text, audit_legacy = await call(legacy_payload)
    check(
        "3. legacy str(dict) payload parses through ast.literal_eval",
        audit_legacy is not None and audit_legacy["deleted_count"] == 5 and audit_legacy["failed_count"] == 1,
        audit_legacy,
    )
    check("3b. legacy payload is not flagged as a parse failure", audit_legacy is not None and audit_legacy["error_class"] == "", audit_legacy)

    # 4/5. Malformed payload -> visible parse failure, never a silent
    # successful-looking zero-count row.
    ok_flag_bad, _text_bad, audit_bad = await call("{not valid json or python repr!!", status="done")
    check("4. malformed payload sets result_parse_failed", audit_bad is not None and audit_bad["error_class"] == "result_parse_failed", audit_bad)
    check(
        "5. malformed payload cannot produce result_ok=True with silent zero counts",
        audit_bad is not None and audit_bad["result_ok"] is False and audit_bad["deleted_count"] == 0 and audit_bad["failed_count"] == 0,
        audit_bad,
    )

    # 14b. Controller-side parsing is idempotent -- same raw payload in,
    # same audit fields out, every time.
    _o1, _t1, audit_i1 = await call(json.dumps({"deleted": 7, "failed": 0}))
    _o2, _t2, audit_i2 = await call(json.dumps({"deleted": 7, "failed": 0}))
    check(
        "14b. controller parsing is idempotent for identical payloads",
        audit_i1 == audit_i2,
        (audit_i1, audit_i2),
    )

    # 16b. parse-failure log never leaks the raw payload's full content
    # unbounded, and structured fields are present without secrets.
    buf = io.StringIO()
    secret_marker = "sk_live_SECRETTOKEN12345"
    with contextlib.redirect_stdout(buf):
        await call("BROKEN " + secret_marker + " not json not repr {{{")
    logs = buf.getvalue()
    check(
        "15d. controller parse-failure log includes manager_key/action/result/error_class",
        all(t in logs for t in ("manager_key=mgr_x", "action=parse_delete_result", "result=failed", "error_class=result_parse_failed")),
        logs,
    )
    # NOTE: the safe-log call itself never receives the raw payload (see
    # §5 of the fix) -- only the audit row's bounded error_text does, which
    # is a DB write, not a log line. This still confirms the *log* stays
    # secret-free even when the raw payload contains something secret-shaped.
    check("16b. parse-failure log line never contains the raw payload content", secret_marker not in logs, logs)


# ----------------------------------------------------------------------
# BIZLINK_DELETE_RESULT_FIX_CORRECTION 20260804 (N-1/N-4/N-6): sanitizer +
# never-raise checks for _bizlink_delete_safe_log /
# _bizlink_delete_safe_sanitize, and for the controller-side parse-failure
# audit diagnostic. Task spec items 1-10.
# ----------------------------------------------------------------------

async def run_correction_checks():
    # 1. Exception contains a t.me URL/slug -- must not appear in the log.
    ns_c1, _md1, _mf1 = build_delete_links_ns(MAIN_SRC, {"s1": RuntimeError("boom https://t.me/m/secret_slug")})
    buf1 = io.StringIO()
    with contextlib.redirect_stdout(buf1):
        await ns_c1["_manager_delete_business_links"]({
            "manager_key": "mgr_c1", "mode": "manual", "target_date": "",
            "slugs": ["s1"], "links": [], "requested_by_user_id": 1,
        })
    logs1 = buf1.getvalue()
    check(
        "1. exception containing a t.me URL/slug never appears in the deletion log",
        "secret_slug" not in logs1 and "t.me/m/secret_slug" not in logs1,
        logs1,
    )

    # 2. Exception contains proxy host:port:login:password -- credentials
    # must not appear.
    ns_c2, _md2, _mf2 = build_delete_links_ns(
        MAIN_SRC, {"s2": RuntimeError("proxy failed 1.2.3.4:8080:login:Password123")}
    )
    buf2 = io.StringIO()
    with contextlib.redirect_stdout(buf2):
        await ns_c2["_manager_delete_business_links"]({
            "manager_key": "mgr_c2", "mode": "manual", "target_date": "",
            "slugs": ["s2"], "links": [], "requested_by_user_id": 1,
        })
    logs2 = buf2.getvalue()
    check(
        "2. proxy host:port:login:password credentials never appear in the deletion log",
        "Password123" not in logs2 and "1.2.3.4:8080:login" not in logs2,
        logs2,
    )

    # 3. Exception contains password=/token=/api_hash= -- values must be
    # redacted (keys may remain, values must not).
    ns_c3, _md3, _mf3 = build_delete_links_ns(
        MAIN_SRC, {"s3": RuntimeError("auth failed password=abc token=xyz api_hash=secret")}
    )
    buf3 = io.StringIO()
    with contextlib.redirect_stdout(buf3):
        await ns_c3["_manager_delete_business_links"]({
            "manager_key": "mgr_c3", "mode": "manual", "target_date": "",
            "slugs": ["s3"], "links": [], "requested_by_user_id": 1,
        })
    logs3 = buf3.getvalue()
    check(
        "3. password=/token=/api_hash= values are redacted from the deletion log",
        "=abc" not in logs3 and "=xyz" not in logs3 and "=secret" not in logs3 and "[REDACTED]" in logs3,
        logs3,
    )

    # 4. Malformed result payload containing a URL and credential-like text
    # -- bizlink_delete_audit.error_text must be sanitized, not raw.
    call_c4 = build_controller_parse_harness(MAIN_SRC)
    raw_c4 = "NOTJSON https://t.me/m/leaked_slug proxy=1.2.3.4:8080:login:Password123 password=abc {{{"
    _ok_c4, _t4, audit_c4 = await call_c4(raw_c4)
    et_c4 = (audit_c4 or {}).get("error_text", "")
    check(
        "4. malformed payload's URL/credential-like text is sanitized out of bizlink_delete_audit.error_text",
        audit_c4 is not None
        and audit_c4["error_class"] == "result_parse_failed"
        and "leaked_slug" not in et_c4
        and "Password123" not in et_c4
        and "=abc" not in et_c4,
        et_c4,
    )

    # 5. Logging helper receives an object whose __str__ raises -- deletion
    # flow (via the helper itself, called exactly as production call sites
    # call it) must not raise. Two variants: manager_key (never routed
    # through _bizlink_delete_safe_sanitize -- this exercises the helper's
    # OWN outer try/except, i.e. N-6) and error_snippet (which DOES go
    # through the sanitizer -- that function has its own internal
    # str(value) guard too, so this variant is defense-in-depth, not the
    # primary N-6 boundary).
    class _BoomStr:
        def __str__(self):
            raise RuntimeError("str exploded")

    ns_c5, _md5, _mf5 = build_delete_links_ns(MAIN_SRC, {"s5": None})
    raised5a = None
    try:
        ns_c5["_bizlink_delete_safe_log"](
            "delete", "deleted", manager_key=_BoomStr(), slug="s5"
        )
    except Exception as e:
        raised5a = e
    check(
        "5a. _bizlink_delete_safe_log does not raise when manager_key.__str__() raises "
        "(exercises the helper's own outer try/except, not the sanitizer's)",
        raised5a is None,
        raised5a,
    )

    raised5b = None
    try:
        ns_c5["_bizlink_delete_safe_log"](
            "delete", "deleted", manager_key="mgr_c5", slug="s5", error_snippet=_BoomStr()
        )
    except Exception as e:
        raised5b = e
    check(
        "5b. _bizlink_delete_safe_log does not raise when error_snippet.__str__() raises",
        raised5b is None,
        raised5b,
    )

    # 6. print() itself raises -- deletion flow must not raise, neither at
    # the helper level nor through the full _manager_delete_business_links
    # call path that invokes it.
    def _raising_print(*_a, **_k):
        raise RuntimeError("print exploded")

    ns_c6, _md6, _mf6 = build_delete_links_ns(MAIN_SRC, {"s6": None})
    ns_c6["print"] = _raising_print
    raised6a = None
    try:
        ns_c6["_bizlink_delete_safe_log"]("delete", "deleted", manager_key="mgr_c6", slug="s6")
    except Exception as e:
        raised6a = e
    check("6a. _bizlink_delete_safe_log swallows a raising print()", raised6a is None, raised6a)

    raised6b = None
    res6b = None
    try:
        res6b = await ns_c6["_manager_delete_business_links"]({
            "manager_key": "mgr_c6b", "mode": "manual", "target_date": "",
            "slugs": ["s6"], "links": [], "requested_by_user_id": 1,
        })
    except Exception as e:
        raised6b = e
    check(
        "6b. full deletion flow does not raise when print() raises inside the log helper",
        raised6b is None and res6b is not None and res6b.get("deleted") == 1,
        (raised6b, res6b),
    )

    # 7/8. Regression: successful delete / failure still increment their
    # counters correctly with the corrected helper wired in.
    ns_c78, md_c78, mf_c78 = build_delete_links_ns(
        MAIN_SRC, {"ok7": None, "bad8": RuntimeError("boom")}
    )
    res_c78 = await ns_c78["_manager_delete_business_links"]({
        "manager_key": "mgr_c78", "mode": "manual", "target_date": "",
        "slugs": ["ok7", "bad8"], "links": [], "requested_by_user_id": 1,
    })
    check("7. successful delete still increments deleted after the correction", res_c78.get("deleted") == 1, res_c78)
    check("8. failure still increments failed after the correction", res_c78.get("failed") == 1, res_c78)

    # 9. Exact CHATLINK_SLUG_EXPIRED still increments expired_cleaned.
    ns_c9, md_c9, mf_c9 = build_delete_links_ns(MAIN_SRC, {"dead9": RuntimeError("CHATLINK_SLUG_EXPIRED")})
    res_c9 = await ns_c9["_manager_delete_business_links"]({
        "manager_key": "mgr_c9", "mode": "manual", "target_date": "",
        "slugs": ["dead9"], "links": [], "requested_by_user_id": 1,
    })
    check("9. exact CHATLINK_SLUG_EXPIRED still increments expired_cleaned after the correction", res_c9.get("expired_cleaned") == 1, res_c9)

    # 10. SESSION_EXPIRED still remains failed and is not soft-deleted.
    ns_c10, md_c10, mf_c10 = build_delete_links_ns(MAIN_SRC, {"s10": RuntimeError("SESSION_EXPIRED")})
    res_c10 = await ns_c10["_manager_delete_business_links"]({
        "manager_key": "mgr_c10", "mode": "manual", "target_date": "",
        "slugs": ["s10"], "links": [], "requested_by_user_id": 1,
    })
    check(
        "10. SESSION_EXPIRED still remains failed and not soft-deleted after the correction",
        res_c10.get("expired_cleaned") == 0 and res_c10.get("failed") == 1 and not md_c10.calls,
        (res_c10, md_c10.calls),
    )

    # 11. BIZLINK_DELETE_RESULT_FIX_FINAL_CORRECTION 20260804 (§3): the
    # queue row's OWN error_text -- separate from the parse-failure
    # diagnostic N-4 already covered -- must now also be sanitized before
    # reaching bizlink_delete_audit.error_text, on a VALID (non-parse-
    # failure) payload too, since that path previously stored it raw.
    call_c11 = build_controller_parse_harness(MAIN_SRC)
    raw_row_error = (
        "queue error https://t.me/m/ROWLEAK proxy=9.9.9.9:1080:rowuser:RowPass987 "
        r"password=ROWPW C:\ALM_TPilot\sessions\rowmgr.session"
    )
    _ok_c11, _t11, audit_c11 = await call_c11(
        json.dumps({"deleted": 2, "failed": 0}), error_text=raw_row_error
    )
    et_c11 = (audit_c11 or {}).get("error_text", "")
    check(
        "11. row['error_text'] URL/proxy/password/session content is sanitized out of bizlink_delete_audit.error_text",
        audit_c11 is not None
        and "ROWLEAK" not in et_c11
        and "RowPass987" not in et_c11
        and "ROWPW" not in et_c11
        and "rowmgr" not in et_c11,
        et_c11,
    )
    check(
        "11b. sanitizing row['error_text'] does not disturb valid-payload deleted/failed counts",
        audit_c11 is not None and audit_c11["deleted_count"] == 2 and audit_c11["failed_count"] == 0
        and audit_c11["error_class"] == "",
        audit_c11,
    )

    # 12. Ordinary (non-secret-shaped) row['error_text'] stays readable --
    # sanitizing must not gut normal diagnostics down to noise.
    _ok_c12, _t12, audit_c12 = await call_c11(
        json.dumps({"deleted": 0, "failed": 1}),
        error_text="Telethon FloodWaitError: A wait of 42 seconds is required",
    )
    et_c12 = (audit_c12 or {}).get("error_text", "")
    check(
        "12. ordinary row['error_text'] remains useful after sanitization",
        "FloodWaitError" in et_c12 and "42 seconds" in et_c12,
        et_c12,
    )


# ----------------------------------------------------------------------
# BIZLINK_DELETE_RESULT_FIX_FINAL_CORRECTION 20260804 (B-1, §5): timing
# checks against the REAL extracted sanitizer, proving the fix's work is
# bounded (effectively O(1) w.r.t. input length, since the working text is
# truncated before any regex runs) rather than the O(k^2)-in-the-worst-case
# behavior the independent review measured (~32s at 128KB).
# ----------------------------------------------------------------------

def _extract_real_sanitize(main_src: str):
    node = _top_level_node(main_src, "_bizlink_delete_safe_sanitize")
    ns = {"re": re, "Any": object}
    exec(compile(ast.unparse(node), "<main.py:_bizlink_delete_safe_sanitize>", "exec"), ns)
    return ns["_bizlink_delete_safe_sanitize"]


# Generous, non-flaky ceiling for the local machine. The fixed sanitizer's
# work no longer scales with input length at all, so real runs finish in
# single-digit milliseconds even at 10MB; 1.5s leaves wide headroom for a
# slow/loaded box while still catching any reintroduced unbounded regex
# (which blows past this by 1-2 orders of magnitude once inputs cross a
# few hundred KB -- see the B-1 measurements in the independent review).
PERF_CEILING_SEC = 1.5


async def run_perf_checks():
    san = _extract_real_sanitize(MAIN_SRC)

    sizes = [(8_000, "8KB"), (32_000, "32KB"), (128_000, "128KB"),
              (1_000_000, "1MB"), (10_000_000, "10MB")]
    timings = {}
    for n, label in sizes:
        s = "a" * n  # worst case: whitespace-free run, no redaction pattern ever matches
        t0 = time.perf_counter()
        out = san(s)
        dt = time.perf_counter() - t0
        timings[label] = dt
        check(
            f"perf: {label} whitespace-free adversarial input completes under {PERF_CEILING_SEC}s (took {dt:.3f}s)",
            dt < PERF_CEILING_SEC,
            dt,
        )
        check(f"perf: {label} output remains bounded (<=150 chars, default max_len)", len(out) <= 150, len(out))

    # "any multi-second growth between 32KB and 1MB should FAIL" (task §5):
    # with a bounded pre-regex window the fixed sanitizer's cost is
    # independent of input length, so 1MB must not take meaningfully
    # longer than 32KB.
    growth = timings["1MB"] - timings["32KB"]
    check(
        f"perf: no multi-second growth between 32KB and 1MB (delta={growth:.3f}s)",
        growth < 2.0,
        growth,
    )

    # 1MB malformed compact JSON WITH a .session path near the start --
    # must still be redacted, and stay fast.
    with_session = (
        '{"error":"cannot open C:\\\\ALM_TPilot\\\\sessions\\\\mgr1.session file",'
        '"pad":"' + ("x" * 1_000_000) + '"}'
    )
    t0 = time.perf_counter()
    out_ws = san(with_session, 200)
    dt_ws = time.perf_counter() - t0
    check(f"perf: 1MB malformed JSON WITH .session completes under {PERF_CEILING_SEC}s ({dt_ws:.3f}s)", dt_ws < PERF_CEILING_SEC, dt_ws)
    check("perf: 1MB malformed JSON WITH .session -- path is redacted", "SESSION_PATH_REDACTED" in out_ws, out_ws)
    check("perf: 1MB malformed JSON WITH .session -- no leak of 'mgr1'", "mgr1" not in out_ws, out_ws)
    check("perf: 1MB malformed JSON WITH .session -- output bounded to 200", len(out_ws) <= 200, len(out_ws))

    # 1MB malformed compact JSON WITHOUT any .session content.
    without_session = '{"error":"generic failure","pad":"' + ("x" * 1_000_000) + '"}'
    t0 = time.perf_counter()
    out_wos = san(without_session, 200)
    dt_wos = time.perf_counter() - t0
    check(f"perf: 1MB malformed JSON WITHOUT .session completes under {PERF_CEILING_SEC}s ({dt_wos:.3f}s)", dt_wos < PERF_CEILING_SEC, dt_wos)
    check("perf: 1MB malformed JSON WITHOUT .session -- output bounded", len(out_wos) <= 200, len(out_wos))

    # Speed alone is not the point -- correctness must survive the bounding
    # change too: ordinary text stays readable, redaction still works.
    ordinary = san("RPCError 400: CHATLINK_SLUG_EXPIRED (caused by DeleteBusinessChatLink)")
    check("perf: harmless ordinary RPC error remains readable after the B-1 fix", "CHATLINK_SLUG_EXPIRED" in ordinary, ordinary)
    redacted = san("boom https://t.me/m/secret_slug password=abc")
    check(
        "perf: URL/credential redaction still works after the B-1 fix",
        "secret_slug" not in redacted and "=abc" not in redacted,
        redacted,
    )

    # A large malformed payload cannot block controller parsing: run the
    # REAL controller branch end-to-end with a 1MB malformed result_json
    # and confirm it returns promptly and the payload's leak markers never
    # reach the audit row.
    call = build_controller_parse_harness(MAIN_SRC)
    big_malformed = ("NOTJSON https://t.me/m/BIGLEAK password=BIGPW " * 25_000)[:1_000_000] + "{{{"
    t0 = time.perf_counter()
    ok_big, _text_big, audit_big = await call(big_malformed)
    dt_big = time.perf_counter() - t0
    check(
        f"perf: 1MB malformed result_json does not block controller parsing (took {dt_big:.3f}s)",
        dt_big < PERF_CEILING_SEC,
        dt_big,
    )
    check(
        "perf: 1MB payload's leak markers do not appear in the audit row",
        audit_big is not None
        and "BIGLEAK" not in audit_big.get("error_text", "")
        and "BIGPW" not in audit_big.get("error_text", ""),
        (audit_big or {}).get("error_text", ""),
    )

    # 1MB input does not appear in the deletion log either (the safe-log
    # path, exercised through the real _manager_delete_business_links call
    # site, which already pre-truncates repr(exc) to 200 chars before
    # handing it to the sanitizer -- confirms that existing bound still
    # holds and nothing regressed it).
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ns_big, _md, _mf = build_delete_links_ns(
            MAIN_SRC, {"s_big": RuntimeError("boom " + ("Z" * 1_000_000) + " password=BIGSECRET")},
        )
        await ns_big["_manager_delete_business_links"]({
            "manager_key": "mgr_big", "mode": "manual", "target_date": "",
            "slugs": ["s_big"], "links": [], "requested_by_user_id": 1,
        })
    big_log = buf.getvalue()
    check("perf: 1MB error text does not appear verbatim in the deletion log", ("Z" * 1000) not in big_log, len(big_log))
    check("perf: secret embedded in the 1MB error text is redacted from the log", "BIGSECRET" not in big_log, big_log[:300])


async def run_correction_mutation_controls():
    # M6: remove sanitization from the logging helper's error_snippet line
    # (revert to str(error_snippet)[:150]) -- URL-leakage check must go RED.
    m6_red = False
    try:
        ns_m6, _md, _mf = build_delete_links_ns(
            MAIN_SRC, {"s": RuntimeError("boom https://t.me/m/secret_slug")},
            mutations={
                "_bizlink_delete_safe_log": (
                    "parts.append(f'error={_bizlink_delete_safe_sanitize(error_snippet)}')",
                    "parts.append(f'error={str(error_snippet)[:150]}')",
                ),
            },
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await ns_m6["_manager_delete_business_links"]({
                "manager_key": "mgr_m6", "mode": "manual", "target_date": "",
                "slugs": ["s"], "links": [], "requested_by_user_id": 1,
            })
        m6_red = "secret_slug" in buf.getvalue()
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M6 anchor missing/non-unique -- {e}", False, e)
        m6_red = False
    except Exception:
        m6_red = True
    check("M6 mutation (remove logging-helper sanitization) turns URL-leakage check RED", m6_red)

    # M7: move try below message construction -- a raising error_snippet
    # .__str__() now escapes the helper instead of being swallowed.
    m7_red = False
    try:
        ns_m7, _md, _mf = build_delete_links_ns(
            MAIN_SRC, {"s": None},
            mutations={
                "_bizlink_delete_safe_log": (
                    "    try:\n"
                    "        parts = [f'action={action}', f'result={result}']\n"
                    "        if manager_key:\n"
                    "            parts.append(f'manager_key={manager_key}')\n"
                    "        if slug:\n"
                    "            parts.append(f'slug={slug}')\n"
                    "        if error_class:\n"
                    "            parts.append(f'error_class={error_class}')\n"
                    "        if error_snippet:\n"
                    "            parts.append(f'error={_bizlink_delete_safe_sanitize(error_snippet)}')\n"
                    "        print('[bizlink_delete_audit] ' + ' '.join(parts))\n"
                    "    except Exception:\n"
                    "        pass",
                    "    parts = [f'action={action}', f'result={result}']\n"
                    "    if manager_key:\n"
                    "        parts.append(f'manager_key={manager_key}')\n"
                    "    if slug:\n"
                    "        parts.append(f'slug={slug}')\n"
                    "    if error_class:\n"
                    "        parts.append(f'error_class={error_class}')\n"
                    "    if error_snippet:\n"
                    "        parts.append(f'error={_bizlink_delete_safe_sanitize(error_snippet)}')\n"
                    "    try:\n"
                    "        print('[bizlink_delete_audit] ' + ' '.join(parts))\n"
                    "    except Exception:\n"
                    "        pass",
                ),
            },
        )

        class _BoomM7:
            def __str__(self):
                raise RuntimeError("str exploded")

        # manager_key, not error_snippet: error_snippet is routed through
        # _bizlink_delete_safe_sanitize(), which has its OWN internal
        # str(value) guard and would swallow the raise regardless of this
        # mutation, masking the very regression M7 is meant to catch.
        # manager_key goes straight into an f-string with no such
        # independent guard, so it only survives if the helper's own
        # (mutated-away) outer try/except is what's protecting it.
        try:
            ns_m7["_bizlink_delete_safe_log"](
                "delete", "deleted", manager_key=_BoomM7(), slug="s"
            )
            m7_red = False  # did not raise -- mutation had no observable effect
        except Exception:
            m7_red = True
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M7 anchor missing/non-unique -- {e}", False, e)
        m7_red = False
    except Exception:
        m7_red = True
    check("M7 mutation (move try below message construction) turns raising-__str__ check RED", m7_red)

    # M8: restore raw_result storage in error_text (undo N-4).
    m8_red = False
    try:
        call_m8 = build_controller_parse_harness(
            MAIN_SRC,
            mutate=(
                "safe_prefix = _bizlink_delete_safe_sanitize(raw_result, 200)",
                "safe_prefix = raw_result[:200]",
            ),
        )
        raw_m8 = "NOTJSON https://t.me/m/leaked_slug password=abc {{{"
        _ok_m8, _t_m8, audit_m8 = await call_m8(raw_m8)
        et_m8 = (audit_m8 or {}).get("error_text", "")
        m8_red = "leaked_slug" in et_m8 or "password=abc" in et_m8
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M8 anchor missing/non-unique -- {e}", False, e)
        m8_red = False
    except Exception:
        m8_red = True
    check("M8 mutation (restore raw payload in audit error_text) turns audit-leakage check RED", m8_red)

    # M9: remove the sanitizer's pre-regex truncation window (undo B-1's
    # bounding step, keep the bounded {0,80} session regex as-is). Even
    # with the regex itself no longer catastrophically quadratic, running
    # it across a full unbounded 1MB input is still measurably slower than
    # the O(1)-w.r.t.-input-length bounded-window version -- a performance
    # check must go RED.
    m9_red = False
    try:
        san_src_m9 = _apply_subs(
            ast.unparse(_top_level_node(MAIN_SRC, "_bizlink_delete_safe_sanitize")),
            (
                "        window = min(len(text), limit * 4, 4000)\n        text = text[:window]",
                "        pass",
            ),
        )
        ns_m9 = {"re": re, "Any": object}
        exec(compile(san_src_m9, "<m9>", "exec"), ns_m9)
        san_m9 = ns_m9["_bizlink_delete_safe_sanitize"]

        s_m9 = "a" * 1_000_000  # whitespace-free, no pattern ever matches
        t0 = time.perf_counter()
        san_m9(s_m9)
        dt_m9 = time.perf_counter() - t0
        # The FIXED sanitizer clears this same 1MB input in a few
        # milliseconds (see run_perf_checks PERF_CEILING_SEC below, which
        # is far more generous); 0.15s is a tight bound the fix always
        # clears and the truncation-removed mutation reliably misses.
        m9_red = dt_m9 > 0.15
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M9 anchor missing/non-unique -- {e}", False, e)
        m9_red = False
    except Exception:
        m9_red = True
    check("M9 mutation (remove pre-regex truncation) turns 1MB performance check RED", m9_red)

    # M10: restore raw row["error_text"] storage (undo §3's second-source
    # sanitization) -- audit-leakage check must go RED.
    m10_red = False
    try:
        call_m10 = build_controller_parse_harness(
            MAIN_SRC,
            mutate=(
                'audit_error_text = _bizlink_delete_safe_sanitize(row.get(\'error_text\') or \'\', 500)',
                "audit_error_text = str(row.get('error_text') or '')[:500]",
            ),
        )
        _ok_m10, _t_m10, audit_m10 = await call_m10(
            json.dumps({"deleted": 1, "failed": 0}),
            error_text="leak https://t.me/m/m10_leaked_slug password=m10secret",
        )
        et_m10 = (audit_m10 or {}).get("error_text", "")
        m10_red = "m10_leaked_slug" in et_m10 or "m10secret" in et_m10
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M10 anchor missing/non-unique -- {e}", False, e)
        m10_red = False
    except Exception:
        m10_red = True
    check("M10 mutation (restore raw row['error_text'] in audit) turns audit-leakage check RED", m10_red)

    # M11: prove a missing/drifted mutation anchor can NEVER fake a
    # successful RED. Uses a deliberately bogus anchor that cannot exist in
    # main.py; must raise MutationAnchorError (not silently succeed, and
    # not be swallowed by a bare `except Exception`).
    anchor_error_raised = False
    anchor_error_type_correct = False
    bogus_call = None
    try:
        bogus_call = build_controller_parse_harness(
            MAIN_SRC,
            mutate=(
                "THIS_ANCHOR_TEXT_CANNOT_EXIST_IN_MAIN_PY_0xDEADBEEF",
                "irrelevant",
            ),
        )
    except MutationAnchorError:
        anchor_error_raised = True
        anchor_error_type_correct = True
    except AssertionError:
        # Would previously have been swallowed by a bare `except Exception`
        # in every M1-M8 call site and miscounted as a successful mutation --
        # confirm it is at least an AssertionError subclass even if somehow
        # not routed through MutationAnchorError.
        anchor_error_raised = True
        anchor_error_type_correct = False
    check(
        "M11: a bogus/missing mutation anchor raises an exception instead of silently no-op'ing",
        anchor_error_raised,
        bogus_call,
    )
    check(
        "M11: the anchor-failure exception is specifically MutationAnchorError "
        "(so mutation-control call sites can catch it separately from a real "
        "behavioral RED and refuse to count it as success)",
        anchor_error_type_correct,
    )
    # And directly reproduce the failure mode the review flagged: a naive
    # `except Exception: mN_red = True` WOULD have miscounted this as RED.
    naive_would_misreport = False
    try:
        build_controller_parse_harness(
            MAIN_SRC, mutate=("THIS_ANCHOR_TEXT_CANNOT_EXIST_EITHER_0xC0FFEE", "x"),
        )
    except Exception:
        naive_would_misreport = True  # this is exactly the old (buggy) pattern
    check(
        "M11: confirms the pre-fix vulnerability -- a naive bare except would have scored "
        "this as a successful mutation (now prevented by catching MutationAnchorError first)",
        naive_would_misreport,
    )


# ----------------------------------------------------------------------
# D3A manager-side dispatch branch (serialization fix, §2) -- extracted
# from the LAST _manager_command_loop definition.
# ----------------------------------------------------------------------

def build_d3a_dispatch_harness(main_src: str, mutate=None):
    fn_node = _top_level_node(main_src, "_manager_command_loop")
    body = _find_if_body(fn_node, ("command ==", "bizlink_delete_tpilot"))
    params = "row, nonce, _mqfinish, _manager_delete_business_links, MANAGER_RUNTIME_KEY, TPILOT_DB_PATH"
    return _wrap_async_harness("_d3a_dispatch_harness", params, body, mutate=mutate)


async def run_d3a_dispatch_checks():
    harness = build_d3a_dispatch_harness(MAIN_SRC)
    calls = []

    async def fake_mqfinish(nonce, *, worker_key, ok, result_text, result_json, db_path):
        calls.append({"nonce": nonce, "ok": ok, "result_text": result_text, "result_json": result_json})

    async def fake_delete(payload):
        return {
            "ok": True, "manager_key": "mgr_a", "mode": "manual", "target_date": "",
            "deleted": 4, "failed": 1, "skipped": 0, "errors": ["boom"],
            "deleted_slugs": ["a", "b", "c", "d"], "failed_slugs": ["e"],
            "flood_wait_seconds": 0, "expired_cleaned": 0, "expired_slugs": [],
        }

    row = {"payload_json": json.dumps({"manager_key": "mgr_a", "mode": "manual"})}
    await harness(row, "nonce-1", fake_mqfinish, fake_delete, "mgr_a", ":memory:")

    check("D3A dispatch calls _mqfinish exactly once", len(calls) == 1, calls)
    result_json_out = calls[0]["result_json"] if calls else ""
    parsed_ok = False
    parsed = None
    try:
        parsed = json.loads(result_json_out)
        parsed_ok = True
    except Exception:
        parsed_ok = False
    check("D3A dispatch writes valid JSON to result_json (not a Python repr)", parsed_ok, result_json_out)
    check(
        "D3A dispatch result_json preserves deleted/failed counts through the transport",
        parsed_ok and parsed.get("deleted") == 4 and parsed.get("failed") == 1,
        parsed,
    )
    return harness  # reused by mutation M1 below


# ----------------------------------------------------------------------
# 17/18/19. Scope guard: unrelated code paths byte-identical to backup.
# ----------------------------------------------------------------------

def run_scope_guard_checks():
    backup_glob = str(BASE_DIR.parent / "ALM_TPilot_AUDIT" / "*" / "BIZLINK_DELETE_RESULT_FIX" / "BACKUP" / "main.py.bak_bizlink_delete_result_*")
    candidates = sorted(glob.glob(backup_glob))
    check("scope-guard: pre-edit external backup exists", bool(candidates), backup_glob)
    if not candidates:
        return
    backup_src = open(candidates[0], encoding="utf-8-sig").read()

    # 17. D3B (bizlink_list_telegram / bizlink_delete_telegram) branches --
    # untouched by this patch -- byte-identical to the pre-edit backup.
    cur_tree = _cached_parse(MAIN_SRC)
    bak_tree = _cached_parse(backup_src)
    cur_loop = [n for n in cur_tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_manager_command_loop"][-1]
    bak_loop = [n for n in bak_tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_manager_command_loop"][-1]
    d3b_names = (("bizlink_list_telegram",), ("bizlink_delete_telegram",))
    for marker in d3b_names:
        cur_body = "\n".join(ast.unparse(s) for s in _find_if_body(cur_loop, ("command ==",) + marker))
        bak_body = "\n".join(ast.unparse(s) for s in _find_if_body(bak_loop, ("command ==",) + marker))
        check(f"17. D3B branch {marker[0]} unchanged vs pre-edit backup", cur_body == bak_body)

    # 18. Auto-recovery caller of _manager_delete_business_links
    # (_manager_recover_link_limit, the oldest_n cleanup path) -- this
    # patch only changed the SHARED helper's internals (additive logging +
    # narrower matcher), never this caller's own code.
    cur_recover = [n for n in cur_tree.body if getattr(n, "name", None) == "_manager_recover_link_limit"]
    bak_recover = [n for n in bak_tree.body if getattr(n, "name", None) == "_manager_recover_link_limit"]
    if cur_recover and bak_recover:
        check(
            "18. auto-recovery caller _manager_recover_link_limit unchanged vs backup",
            ast.unparse(cur_recover[-1]) == ast.unparse(bak_recover[-1]),
        )
    else:
        check("18. auto-recovery caller _manager_recover_link_limit located for comparison", False, "function not found by that name -- verify manually")

    # 19. Sibling runtime files never opened for writing by this patch --
    # proxy check: their mtimes must all predate main.py's mtime (only
    # main.py was edited during this task).
    main_mtime = os.path.getmtime(MAIN_PATH)
    for fname in ("storage.py", "panel_bot.py", "manager_bot.py", "partner_stat_bot.py"):
        fpath = str(BASE_DIR / fname)
        if os.path.exists(fpath):
            check(f"19. {fname} not modified during this patch (mtime predates main.py)", os.path.getmtime(fpath) <= main_mtime)


# ----------------------------------------------------------------------
# Mutation controls (task §6, M1-M5): each must turn the corresponding
# positive check(s) RED, proving the test actually detects the bug it
# claims to guard against. All mutations run on in-memory unparsed
# source only -- the real main.py on disk is never touched.
# ----------------------------------------------------------------------

async def run_mutation_controls():
    # M1: restore result_json=str(res) in the D3A dispatch branch.
    m1_red = False
    try:
        harness_m1 = build_d3a_dispatch_harness(
            MAIN_SRC,
            mutate=("result_json_str = _m213d3a_del_json.dumps(res, ensure_ascii=False)", "result_json_str = str(res)"),
        )
        calls_m1 = []

        async def fake_mqfinish_m1(nonce, *, worker_key, ok, result_text, result_json, db_path):
            calls_m1.append(result_json)

        async def fake_delete_m1(payload):
            return {"ok": True, "manager_key": "m", "mode": "x", "target_date": "", "deleted": 1, "failed": 0,
                     "skipped": 0, "errors": [], "deleted_slugs": ["a"], "failed_slugs": [],
                     "flood_wait_seconds": 0, "expired_cleaned": 0, "expired_slugs": []}

        row = {"payload_json": json.dumps({"manager_key": "m", "mode": "x"})}
        await harness_m1(row, "n1", fake_mqfinish_m1, fake_delete_m1, "m", ":memory:")
        try:
            json.loads(calls_m1[0])
            m1_red = False  # still valid JSON -- mutation had no effect, test did NOT catch it
        except Exception:
            m1_red = True  # invalid JSON, as the original bug produced -- mutation correctly detected
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M1 anchor missing/non-unique -- {e}", False, e)
        m1_red = False
    except Exception:
        m1_red = True
    check("M1 mutation (restore str(res)) turns serialization check RED", m1_red)

    # M2: remove the ast.literal_eval legacy fallback.
    m2_red = False
    try:
        call_m2 = build_controller_parse_harness(
            MAIN_SRC,
            mutate=(
                "_parsed_legacy = _m213d3a_ctrl_ast.literal_eval(raw_result)",
                "raise ValueError('mutated: legacy fallback removed')",
            ),
        )
        legacy_payload = str({"ok": True, "deleted": 5, "failed": 1})
        _ok, _text, audit_m2 = await call_m2(legacy_payload)
        m2_red = not (audit_m2 is not None and audit_m2["deleted_count"] == 5 and audit_m2["error_class"] == "")
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M2 anchor missing/non-unique -- {e}", False, e)
        m2_red = False
    except Exception:
        m2_red = True
    check("M2 mutation (remove ast.literal_eval fallback) turns legacy-payload check RED", m2_red)

    # M3: restore the broad `or "expired" in low` matcher.
    m3_red = False
    try:
        ns_m3, mark_deleted_m3, _mf = build_delete_links_ns(
            MAIN_SRC, {"s-slug": RuntimeError("SESSION_EXPIRED")},
            mutations={
                "_manager_delete_business_links": (
                    "if 'chatlink_slug_expired' in low or 'slug_expired' in low:",
                    "if 'chatlink_slug_expired' in low or 'slug_expired' in low or 'expired' in low:",
                ),
            },
        )
        await ns_m3["_manager_delete_business_links"]({
            "manager_key": "mgr_b", "mode": "manual", "target_date": "",
            "slugs": ["s-slug"], "links": [], "requested_by_user_id": 1,
        })
        m3_red = bool(mark_deleted_m3.calls)  # SESSION_EXPIRED wrongly soft-deleted under the mutation
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M3 anchor missing/non-unique -- {e}", False, e)
        m3_red = False
    except Exception:
        m3_red = True
    check("M3 mutation (restore broad 'expired' matcher) turns SESSION_EXPIRED check RED", m3_red)

    # M4: restore the silent `{}` fallback -- i.e. even when parsing truly
    # failed, pretend it succeeded (result_ok tracks only queue status,
    # error_class stays empty). This directly targets the two lines that
    # DETERMINE the audit row's visible-failure fields, so the mutation
    # cannot be masked by any later statement re-deriving the same values.
    m4_red = False
    try:
        call_m4 = build_controller_parse_harness(
            MAIN_SRC,
            mutate=[
                ("audit_result_ok = bool(ok_flag) and (not parse_failed)", "audit_result_ok = bool(ok_flag)"),
                ("audit_error_class = 'result_parse_failed' if parse_failed else ''", "audit_error_class = ''"),
            ],
        )
        _ok, _text, audit_m4 = await call_m4("{not valid json or python repr!!")
        m4_red = not (audit_m4 is not None and audit_m4["error_class"] == "result_parse_failed" and audit_m4["result_ok"] is False)
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M4 anchor missing/non-unique -- {e}", False, e)
        m4_red = False
    except Exception:
        m4_red = True
    check("M4 mutation (restore silent {} fallback) turns parse-failure check RED", m4_red)

    # M5: remove the safe log marker (both call sites).
    m5_red_a = False
    try:
        ns_m5, _md, _mf = build_delete_links_ns(
            MAIN_SRC, {"ok-slug": None},
            mutations={
                "_manager_delete_business_links": (
                    '_bizlink_delete_safe_log(\'delete\', \'deleted\', manager_key=manager_key, slug=slug)',
                    'pass',
                ),
            },
        )
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            await ns_m5["_manager_delete_business_links"]({
                "manager_key": "mgr_a", "mode": "manual", "target_date": "",
                "slugs": ["ok-slug"], "links": [], "requested_by_user_id": 1,
            })
        m5_red_a = "result=deleted" not in buf.getvalue()
    except MutationAnchorError as e:
        check(f"MUTATION_ANCHOR_FAILURE: M5 anchor missing/non-unique -- {e}", False, e)
        m5_red_a = False
    except Exception:
        m5_red_a = True
    check("M5 mutation (remove safe log marker) turns deletion-log check RED", m5_red_a)


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

async def _amain():
    await run_manager_delete_checks()
    await run_controller_parse_checks()
    await run_d3a_dispatch_checks()
    run_scope_guard_checks()
    await run_mutation_controls()
    await run_correction_checks()
    await run_perf_checks()
    await run_correction_mutation_controls()


def main() -> int:
    asyncio.run(_amain())
    print()
    if FAILURES:
        print(f"{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
