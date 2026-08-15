# -*- coding: utf-8 -*-
"""tools/proxy_renewal_retry_policy_selftest.py -- end-to-end selftest for
the proxy-renewal post-spend money-safety policy, on top of the
already-closed crash-window fix (see proxy_renewal_crash_window_selftest.py)
and idempotency fix (see proxy_renewal_idempotency_selftest.py).

HISTORY (two corrections on 20260809, same day):

  1st pass (BLOCKER-1 retry-policy fix): before this, ANY failure after
  prolong_make was called (timeout, HTTP 5xx, malformed response, OR a
  structurally-provable business rejection like "insufficient balance"/
  "IP not found"/an auth reject) advanced the proxy_renewal_ops row to the
  SAME terminal 'failed' status, and since proxy_renewal_ops_idem_idx is a
  GLOBAL unique index (not scoped to active statuses), that idempotency_key
  stayed burned FOREVER -- even once the admin fixed the reported cause, the
  next attempt for the SAME (provider_proxy_id, expires_before, period)
  triple silently hit the collision and returned {skipped: True}, with both
  _prenew_autorenew_one and _renewal_auto_tick returning on that `skipped`
  result WITHOUT ever telling the admin ("silent death").

  2nd pass (THIS file, money-safety correction -- independent adversarial
  re-review, 20260809): the 1st pass's fix additionally freed the
  idempotency_key whenever the prolong_make except-block's error kind was
  PROVIDER_ERRORS or AUTH, reasoning that proxy_provider.py's own
  errors[]-before-HTTP-status check made those kinds structural proof the
  spend never happened. That reasoning was FALSIFIED: proxy_provider.py's
  ProxySellerProvider._request() checks errors[] BEFORE the HTTP status
  code on EVERY response body, regardless of what that status code is --
  so an HTTP 500/502/429 response whose JSON body ALSO happens to carry a
  non-empty errors[] list (an entirely ordinary shape for a provider-side
  failure) produces the EXACT SAME kind=PROVIDER_ERRORS a clean pre-billing
  business rejection would. ProxyProviderError carries no HTTP status code
  and no separate "request_not_sent"/"spend_not_attempted" signal to tell
  these apart. kind alone was therefore NOT sufficient structural proof of
  no-spend, and the "provably no-spend" branch (main.py
  _renewal_wrapped_execute's spend_provably_absent handling) plus its
  storage helper (proxy_renewal_op_delete_no_spend_failed) have been
  REMOVED entirely.

CURRENT (safe) POLICY, verified by this file:
  - Pre-spend (reference_list/period lookup failure, a local guard block) --
    no op row is ever created, freely retryable, zero spend.
  - Once prolong_make has been CALLED (the op is 'make_pending'), ANY
    outcome other than a confirmed success is AMBIGUOUS, with NO exception
    for error kind or human-facing category: the op advances to terminal
    'failed', proxy_renewal_ops_idem_idx keeps the idempotency_key burned,
    and no automatic OR manual second prolong_make can ever happen for that
    exact (provider_proxy_id, expires_before, period) triple. The ONLY way
    to unblock a future cycle is a later provider-refresh reconcile
    (_renewal_reconcile_pending_on_start / the periodic
    _plc_reconcile_loop) observing the provider's own expiry has actually
    advanced -- which changes expires_at and therefore mints a NEW
    idempotency_key.
  - The already-closed silent-death fix is preserved: a lease durably stuck
    behind a 'failed' op (error='blocked_unresolved') is never silently
    skipped -- both engines fall through to the classified/deduped
    notification machinery, and the alert never offers a spend-capable
    button.

Cases (task spec, CASE A-N):
  A. HTTP 500 + JSON errors[] after prolong_make -> AMBIGUOUS, no retry.
  B. HTTP 502 + errors[] -> AMBIGUOUS, no retry.
  C. HTTP 429 + errors[] -> AMBIGUOUS, no retry.
  D. HTTP 401/403 after prolong_make -> AMBIGUOUS, no retry (kind=AUTH is
     NOT treated as proof of no-spend either).
  E. Transport timeout after the request was sent -> AMBIGUOUS.
  F. Connection reset after the request was sent -> AMBIGUOUS.
  G. Malformed/non-JSON response after the request was sent -> AMBIGUOUS.
  H. Pre-spend reference_list failure -> retry allowed, spends exactly once
     once the cause is fixed.
  I. Local guard failure (emergency_stop) -> retry allowed once cleared,
     zero spend while blocked.
  J. Ambiguous lease + a subsequent manual attempt -> spend count stays 1.
  K. Ambiguous lease + a subsequent engine-A (_prenew_autorenew_one)
     attempt -> spend count stays 1.
  L. Ambiguous lease + a subsequent engine-B (source='auto') attempt ->
     spend count stays 1.
  M. Crash-recovery reconcile confirms the provider actually renewed ->
     op 'success' AND lease.expires_at advances; the new expires_at mints a
     fresh idempotency_key for the next cycle.
  N. Reconcile observes provider expiry UNCHANGED -> op stays/becomes
     'failed', no second spend, and the durably-stuck lease still produces
     an actionable alert (silent death stays closed).

Section 10 (parser-grounded proof): cases A-G above do NOT hand-construct
`ProxyProviderError(kind=...)` -- they drive a REAL `proxy_provider.
ProxySellerProvider` instance through a fake `session` object that returns
raw (status_code, json_body) pairs, so `kind` is computed by production
`_request()` parsing logic, never asserted by the test. This directly
proves the case Opus's review flagged: an HTTP 5xx response whose body
contains errors[] is classified identically (kind=PROVIDER_ERRORS) to a
pre-billing rejection, and confirms the current code path treats BOTH as
ambiguous (no special-cased retry for either).

main.py cannot be imported standalone -- AST-extraction idiom (same as
proxy_renewal_crash_window_selftest.py) + a real temp SQLite DB (storage
runs for real). No network, no Telegram, no real provider writes.

Run:  python tools\\proxy_renewal_retry_policy_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import datetime
import os
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage
from proxy_provider import (
    ProxyProviderError,
    ProxySellerProvider,
    PROXY_ERROR_KIND_TRANSPORT,
    PROXY_ERROR_KIND_PROVIDER_ERRORS,
    PROXY_ERROR_KIND_AUTH,
)

MAIN_PY = BASE_DIR / "main.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def _guard_temp_db(db_path: str) -> None:
    rp = os.path.realpath(db_path)
    tmp = os.path.realpath(tempfile.gettempdir())
    assert rp.startswith(tmp), f"db must live under tempdir, got {rp}"
    assert "data_tpilot.db" not in rp and os.sep + "db" + os.sep not in rp, rp


def extract_and_exec(tree, names, extra_ns):
    picked = {}
    for node in tree.body:
        nm = getattr(node, "name", None)
        if nm in names:
            picked[nm] = node
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name) and node.targets[0].id in names:
            picked[node.targets[0].id] = node
    missing = names - set(picked)
    if missing:
        raise AssertionError(f"missing {missing}")
    module_src = "\n\n".join(ast.unparse(picked[n]) for n in names if n in picked)
    ns = dict(extra_ns)
    exec(compile(module_src, "<main.py retry-policy extract>", "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Parser-grounded fakes (Section 10): drive the REAL ProxySellerProvider._http/
# _request through a fake `session`, so `kind` is whatever production parsing
# logic computes from a raw (status_code, json_body) pair -- never hand-set.
# ---------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status_code, json_data=None, malformed=False):
        self.status_code = status_code
        self._json_data = json_data
        self._malformed = malformed

    def json(self):
        if self._malformed:
            raise ValueError("simulated malformed/non-JSON response body")
        return self._json_data


class FakeSession:
    """Queues canned (status_code, json_body) responses or raw exceptions,
    in call order, and hands them back through the SAME `.request(method,
    url, params=, json=, timeout=)` shape ProxySellerProvider._http expects.
    Tracks every URL requested so tests can prove EXACTLY how many times
    prolong/make was actually called."""

    def __init__(self):
        self._queue: list[tuple] = []
        self.calls: list[str] = []

    def queue_response(self, status_code, json_data):
        self._queue.append(("response", status_code, json_data))

    def queue_raise(self, exc: Exception) -> None:
        self._queue.append(("raise", exc, None))

    def queue_malformed(self, status_code) -> None:
        self._queue.append(("malformed", status_code, None))

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append(url)
        if not self._queue:
            raise RuntimeError(f"FakeSession: no queued response for {method} {url}")
        kind, a, b = self._queue.pop(0)
        if kind == "raise":
            raise a
        if kind == "malformed":
            return FakeResponse(a, malformed=True)
        return FakeResponse(a, b)

    def prolong_make_call_count(self) -> int:
        return sum(1 for u in self.calls if "prolong/make" in u)

    def reference_list_call_count(self) -> int:
        return sum(1 for u in self.calls if "reference/list" in u)


_REF_OK = {"data": [{"id": "1m", "name": "1 month"}]}
_CALC_OK = {"data": {"total": "1.8", "currency": "USD", "price": "1.8"}}
_MAKE_OK = {"data": {"total": "1.8", "currency": "USD", "date_end": "2026-09-08"}}


def _real_provider(session: FakeSession, api_key: str = "fake-nospend-test-key") -> ProxySellerProvider:
    return ProxySellerProvider(api_key, allow_spend=True, session=session)


# ---------------------------------------------------------------------------
# Simple duck-typed provider stand-in for bookkeeping-only scenarios
# (reconcile's list_proxies-based provider-refresh) that are unrelated to
# the kind-classification hazard this file targets -- these do not need to
# go through the real HTTP parsing layer.
# ---------------------------------------------------------------------------

class ReconcileFakeProvider:
    def __init__(self):
        self.prolong_make_calls = 0
        self._list_responses: list[list[dict]] = []
        self.list_proxies_calls = 0

    def list_proxies(self, proxy_type, **kwargs):
        self.list_proxies_calls += 1
        if self._list_responses:
            return {"data": self._list_responses.pop(0)}
        raise RuntimeError("no queued list_proxies response")

    def queue_list_response(self, entries: list[dict]) -> None:
        self._list_responses.append(entries)


async def _make_lease(tmp_db: str, **overrides) -> int:
    kwargs = dict(
        provider_type="proxy_seller", host="1.2.3.4", port=50101, manager_key="mgr_a",
        provider_proxy_id="PXY-retry-1", proxy_type="ipv4", scheme="socks5",
        login="u1", password="pw1", status="active", db_path=tmp_db,
        expires_at="2026-08-08",
    )
    kwargs.update(overrides)
    return await storage.proxy_lease_create(**kwargs)


async def main_async() -> int:
    main_tree = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))

    fake_provider_holder: dict = {}
    notifications: list[dict] = []

    def _fake_pbuy_provider():
        return fake_provider_holder.get("instance")

    async def _fake_pbuy_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _fake_create_panel_notification(kind, title, body):
        notifications.append({"kind": kind, "title": title, "body": body})

    async def _fake_tpag_registry_get(manager_key):
        return None  # falls back to manager_key / 'не назначен' -- no registry dependency needed

    # CORRECTION B 20260810: _renewal_wrapped_execute (and _prenew_autorenew_one)
    # now gate on _prenew_lease_is_orphan before anything else. This file predates
    # that correction and tests the RETRY/SPEND policy, not orphan suppression
    # (that is tools/proxy_orphan_lease_selftest.py's job), so a never-orphan fake
    # preserves every existing scenario here -- identical treatment to
    # tools/proxy_renewal_wiring_selftest.py, which received this same injection
    # when Correction B landed while this file was missed.
    async def _fake_never_orphan(lease, usage_map=None, *, context="gate"):
        return False

    ns = extract_and_exec(
        main_tree,
        {
            "_prenew_preflight_check",
            "_prenew_execute_renewal",
            "_renewal_wrapped_execute",
            "_renewal_reconcile_pending_on_start",
            "_renewal_guard_check",
            "_renewal_calc_preview",
            "_renewal_float",
            "_renewal_kyiv_today_str",
            "_handle_proxy_renewal_confirm_command",
            "_prenew_verify_renewal_via_refresh",
            "_prenew_find_provider_entry",
            "_prenew_classify_provider_error",
            "_ppool_parse_expires_at",
            "_pbuy_safe_error",
            "_prenew_autorenew_one",
            "_renewal_auto_tick_report_outcome",
            "_prenew_manager_identity_line",
            "_prenew_format_expires_display",
        },
        {
            "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
            "datetime": datetime.datetime, "asyncio": asyncio,
            "_pbuy_provider": _fake_pbuy_provider,
            "_pbuy_call": _fake_pbuy_call,
            "_PRENEW_PROXY_TYPE": "ipv4",
            "_PRENEW_PAYMENT_ID": 1,
            "_PBUY_PERIOD_ID": "1m",
            "_PBUY_PERIOD_NAME": "1 month",
            "_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS": 1,
            "_PRENEW_VERIFY_REFRESH_BACKOFF_SEC": 0.0,
            "_now_utc_iso": lambda: "2026-08-08T12:00:00",
            "_kyiv_now": lambda: datetime.datetime(2026, 8, 8, 12, 0, 0),
            "CONTROLLER_MODE": True,
            "_pbuy_json": __import__("json"),
            "_create_panel_notification": _fake_create_panel_notification,
            "_tpag_registry_get": _fake_tpag_registry_get,
            "_prenew_lease_is_orphan": _fake_never_orphan,
        },
    )
    wrapped_execute = ns["_renewal_wrapped_execute"]
    execute_renewal = ns["_prenew_execute_renewal"]
    reconcile_fn = ns["_renewal_reconcile_pending_on_start"]
    confirm_cmd = ns["_handle_proxy_renewal_confirm_command"]
    autorenew_one = ns["_prenew_autorenew_one"]

    with tempfile.TemporaryDirectory(prefix="tpilot_renewal_retry_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        _guard_temp_db(tmp_db)
        ns["TPILOT_DB_PATH"] = tmp_db

        async def _assert_ambiguous_after_spend(label_prefix, lease_id, session, expected_kind=None):
            """Shared assertions for CASE A-G: prolong_make was attempted
            exactly once, the op landed terminal 'failed' (row still
            exists, NOT deleted), no active/in-flight op remains, and an
            immediate retry is skipped as 'blocked_unresolved' WITHOUT any
            new prolong_make call."""
            active = await storage.proxy_renewal_op_active_for_lease(lease_id, db_path=tmp_db)
            check(f"{label_prefix}.2 op is NOT in the active-op set (terminal, not in-flight)", active is None, active)
            ops_failed = await storage.proxy_renewal_ops_by_status(["failed"], db_path=tmp_db)
            row = next((o for o in ops_failed if o.get("lease_id") == lease_id), None)
            check(f"{label_prefix}.3 op landed in terminal 'failed' (row still exists, NOT freed/deleted)", row is not None, ops_failed)
            check(f"{label_prefix}.4 prolong_make was attempted exactly once", session.prolong_make_call_count() == 1, session.calls)

            # retry: preflight's reference_list is safe to call again, but
            # op_create must collide on the burned idempotency_key BEFORE
            # _prenew_execute_renewal (and therefore prolong_make) is ever
            # reached again.
            session.queue_response(200, _REF_OK)
            retry_res = await wrapped_execute(await storage.proxy_lease_get(lease_id, db_path=tmp_db), source="autorenew")
            check(f"{label_prefix}.5 immediate retry is skipped, blocked_unresolved (no special-cased retry for {expected_kind})",
                  retry_res.get("skipped") is True and retry_res.get("error") == "blocked_unresolved", retry_res)
            check(f"{label_prefix}.6 retry spent ZERO additional times", session.prolong_make_call_count() == 1, session.calls)

        # ------------------------------------------------------------------
        # CASE A: HTTP 500 + JSON errors[] after prolong_make -- parser-
        # grounded proof that kind=PROVIDER_ERRORS is NOT proof of no-spend.
        # ------------------------------------------------------------------
        lease_a_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-A", host="10.3.0.1", port=54001, expires_at="2026-08-08")
        lease_a = await storage.proxy_lease_get(lease_a_id, db_path=tmp_db)
        session_a = FakeSession()
        session_a.queue_response(200, _REF_OK)
        session_a.queue_response(500, {"errors": [{"message": "internal error", "code": 0}]})
        fake_provider_holder["instance"] = _real_provider(session_a)

        res_a = await wrapped_execute(lease_a, source="autorenew")
        check("A1. HTTP 500 + errors[] after prolong_make returns ok=False (spend attempted, ambiguous)",
              res_a.get("ok") is False and not res_a.get("skipped"), res_a)
        check("A1b. make_failed result no longer carries a spend_provably_absent field (removed hazard)",
              "spend_provably_absent" not in res_a, res_a)
        await _assert_ambiguous_after_spend("A", lease_a_id, session_a, expected_kind=PROXY_ERROR_KIND_PROVIDER_ERRORS)

        # ------------------------------------------------------------------
        # CASE B: HTTP 502 + errors[].
        # ------------------------------------------------------------------
        lease_b_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-B", host="10.3.0.2", port=54002, expires_at="2026-08-08")
        lease_b = await storage.proxy_lease_get(lease_b_id, db_path=tmp_db)
        session_b = FakeSession()
        session_b.queue_response(200, _REF_OK)
        session_b.queue_response(502, {"errors": [{"message": "bad gateway"}]})
        fake_provider_holder["instance"] = _real_provider(session_b)

        res_b = await wrapped_execute(lease_b, source="autorenew")
        check("B1. HTTP 502 + errors[] returns ok=False", res_b.get("ok") is False and not res_b.get("skipped"), res_b)
        await _assert_ambiguous_after_spend("B", lease_b_id, session_b, expected_kind=PROXY_ERROR_KIND_PROVIDER_ERRORS)

        # ------------------------------------------------------------------
        # CASE C: HTTP 429 + errors[].
        # ------------------------------------------------------------------
        lease_c_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-C", host="10.3.0.3", port=54003, expires_at="2026-08-08")
        lease_c = await storage.proxy_lease_get(lease_c_id, db_path=tmp_db)
        session_c = FakeSession()
        session_c.queue_response(200, _REF_OK)
        session_c.queue_response(429, {"errors": [{"message": "too many requests"}]})
        fake_provider_holder["instance"] = _real_provider(session_c)

        res_c = await wrapped_execute(lease_c, source="autorenew")
        check("C1. HTTP 429 + errors[] returns ok=False", res_c.get("ok") is False and not res_c.get("skipped"), res_c)
        await _assert_ambiguous_after_spend("C", lease_c_id, session_c, expected_kind=PROXY_ERROR_KIND_PROVIDER_ERRORS)

        # ------------------------------------------------------------------
        # CASE D: HTTP 401/403 after prolong_make -- kind=AUTH is ALSO not
        # treated as proof of no-spend. D2 additionally proves errors[]
        # wins over the HTTP status even when the status is 401 (still
        # PROVIDER_ERRORS, not AUTH) -- the same conflation as CASE A-C.
        # ------------------------------------------------------------------
        lease_d_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-D", host="10.3.0.4", port=54004, expires_at="2026-08-08")
        lease_d = await storage.proxy_lease_get(lease_d_id, db_path=tmp_db)
        session_d = FakeSession()
        session_d.queue_response(200, _REF_OK)
        session_d.queue_response(401, {})
        fake_provider_holder["instance"] = _real_provider(session_d)

        res_d = await wrapped_execute(lease_d, source="autorenew")
        check("D1. HTTP 401 (no errors[]) after prolong_make returns ok=False", res_d.get("ok") is False and not res_d.get("skipped"), res_d)
        await _assert_ambiguous_after_spend("D", lease_d_id, session_d, expected_kind=PROXY_ERROR_KIND_AUTH)

        # D2: standalone _request() proof that errors[] is checked BEFORE
        # the HTTP status -- a 401 whose body ALSO carries errors[] must
        # still classify as PROVIDER_ERRORS, not AUTH (money-safety
        # implication: neither kind is trustworthy on its own).
        session_d2 = FakeSession()
        session_d2.queue_response(401, {"errors": [{"message": "unauthorized (explicit envelope error too)"}]})
        provider_d2 = _real_provider(session_d2)
        try:
            provider_d2.prolong_make("ipv4", ["PXY-D2"], "1m", 1, allow_spend=True)
            check("D2. expected ProxyProviderError, none raised", False)
        except ProxyProviderError as e:
            check("D2. HTTP 401 + errors[] classifies as PROVIDER_ERRORS (errors[] wins over status), proving kind alone can't disambiguate auth-vs-business failures",
                  e.kind == PROXY_ERROR_KIND_PROVIDER_ERRORS, e.kind)

        # ------------------------------------------------------------------
        # CASE E: transport timeout AFTER the request was sent.
        # ------------------------------------------------------------------
        lease_e_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-E", host="10.3.0.5", port=54005, expires_at="2026-08-08")
        lease_e = await storage.proxy_lease_get(lease_e_id, db_path=tmp_db)
        session_e = FakeSession()
        session_e.queue_response(200, _REF_OK)
        session_e.queue_raise(TimeoutError("simulated timeout after request sent"))
        fake_provider_holder["instance"] = _real_provider(session_e)

        res_e = await wrapped_execute(lease_e, source="autorenew")
        check("E1. transport timeout after prolong_make returns ok=False", res_e.get("ok") is False and not res_e.get("skipped"), res_e)
        await _assert_ambiguous_after_spend("E", lease_e_id, session_e, expected_kind=PROXY_ERROR_KIND_TRANSPORT)

        # ------------------------------------------------------------------
        # CASE F: connection reset AFTER the request was sent.
        # ------------------------------------------------------------------
        lease_f_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-F", host="10.3.0.6", port=54006, expires_at="2026-08-08")
        lease_f = await storage.proxy_lease_get(lease_f_id, db_path=tmp_db)
        session_f = FakeSession()
        session_f.queue_response(200, _REF_OK)
        session_f.queue_raise(ConnectionResetError("simulated connection reset after request sent"))
        fake_provider_holder["instance"] = _real_provider(session_f)

        res_f = await wrapped_execute(lease_f, source="autorenew")
        check("F1. connection reset after prolong_make returns ok=False", res_f.get("ok") is False and not res_f.get("skipped"), res_f)
        await _assert_ambiguous_after_spend("F", lease_f_id, session_f, expected_kind=PROXY_ERROR_KIND_TRANSPORT)

        # ------------------------------------------------------------------
        # CASE G: malformed/non-JSON response AFTER the request was sent.
        # ------------------------------------------------------------------
        lease_g_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-G", host="10.3.0.7", port=54007, expires_at="2026-08-08")
        lease_g = await storage.proxy_lease_get(lease_g_id, db_path=tmp_db)
        session_g = FakeSession()
        session_g.queue_response(200, _REF_OK)
        session_g.queue_malformed(200)
        fake_provider_holder["instance"] = _real_provider(session_g)

        res_g = await wrapped_execute(lease_g, source="autorenew")
        check("G1. malformed response after prolong_make returns ok=False", res_g.get("ok") is False and not res_g.get("skipped"), res_g)
        await _assert_ambiguous_after_spend("G", lease_g_id, session_g, expected_kind="response_shape")

        # ------------------------------------------------------------------
        # CASE H: pre-spend reference_list failure -- freely retryable,
        # spends exactly once after the cause is fixed.
        # ------------------------------------------------------------------
        lease_h_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-H", host="10.3.0.8", port=54008, expires_at="2026-08-08")
        lease_h = await storage.proxy_lease_get(lease_h_id, db_path=tmp_db)
        session_h = FakeSession()
        session_h.queue_raise(ProxyProviderError("network error calling reference/list/ipv4", kind=PROXY_ERROR_KIND_TRANSPORT))
        fake_provider_holder["instance"] = _real_provider(session_h)

        res_h1 = await wrapped_execute(lease_h, source="autorenew")
        check("H1. pre-spend reference_list failure returns ok=False without spending", res_h1.get("ok") is False and not res_h1.get("skipped"), res_h1)
        check("H2. pre-spend failure never called prolong_make", session_h.prolong_make_call_count() == 0, session_h.calls)
        active_h = await storage.proxy_renewal_op_active_for_lease(lease_h_id, db_path=tmp_db)
        check("H3. pre-spend failure never created an op row (idempotency_key never burned)", active_h is None, active_h)

        session_h.queue_response(200, _REF_OK)
        session_h.queue_response(200, {"data": {"total": "1.8", "currency": "USD", "date_end": "2026-09-08"}})
        res_h2 = await wrapped_execute(lease_h, source="autorenew")
        check("H4. retry after the cause is fixed succeeds", res_h2.get("ok") is True, res_h2)
        check("H5. total spend across the whole sequence is exactly 1", session_h.prolong_make_call_count() == 1, session_h.calls)

        # ------------------------------------------------------------------
        # CASE I: local guard failure (emergency_stop) -- blocked BEFORE any
        # op row / provider call; retry allowed once cleared.
        # ------------------------------------------------------------------
        lease_i_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-I", host="10.3.0.9", port=54009, expires_at="2026-08-08")
        await storage.proxy_renewal_config_set(emergency_stop=1, db_path=tmp_db)
        session_i = FakeSession()
        # confirm_cmd's own no-spend calc PREVIEW runs BEFORE the guard
        # check regardless of guard state, and it itself does reference_list
        # (period lookup) THEN prolong_calc -- neither ever spends -- so
        # queue clean responses for both rather than let them error out.
        session_i.queue_response(200, _REF_OK)
        session_i.queue_response(200, _CALC_OK)
        fake_provider_holder["instance"] = _real_provider(session_i)

        res_i1 = await confirm_cmd(f"{lease_i_id}")
        import json as _json
        data_i1 = _json.loads(res_i1)
        check("I1. guard-blocked confirm returns ok=False, error=blocked", data_i1.get("ok") is False and data_i1.get("error") == "blocked", data_i1)
        check("I2. guard block never reaches wrapped_execute's own preflight or prolong_make (only the no-spend calc preview ran)",
              len(session_i.calls) == 2 and session_i.prolong_make_call_count() == 0, session_i.calls)
        active_i = await storage.proxy_renewal_op_active_for_lease(lease_i_id, db_path=tmp_db)
        check("I3. guard block never created an op row", active_i is None, active_i)

        await storage.proxy_renewal_config_set(emergency_stop=0, db_path=tmp_db)
        session_i.queue_response(200, _REF_OK)   # calc preview's reference_list
        session_i.queue_response(200, _CALC_OK)  # calc preview's prolong_calc
        session_i.queue_response(200, _REF_OK)   # wrapped_execute's own preflight
        session_i.queue_response(200, _MAKE_OK)  # the actual spend
        res_i2 = await confirm_cmd(f"{lease_i_id}")
        data_i2 = _json.loads(res_i2)
        check("I4. after clearing the guard, confirm succeeds", data_i2.get("ok") is True, data_i2)
        check("I5. exactly one spend once the guard is cleared", session_i.prolong_make_call_count() == 1, session_i.calls)

        # ------------------------------------------------------------------
        # CASE J/K/L: an ambiguous (post-spend) lease stays blocked no
        # matter which entry point attempts it next -- manual, engine A
        # (_prenew_autorenew_one), engine B (source='auto'). Reuses CASE B's
        # already-ambiguous lease_b (HTTP 502 + errors[]).
        # ------------------------------------------------------------------
        # confirm_cmd: calc preview (reference_list + prolong_calc, no-spend)
        # + wrapped_execute's OWN preflight (a second, separate
        # reference_list) -- all three run before op_create collides on the
        # burned idempotency_key; prolong_make is never reached. Re-point
        # the fake provider back at lease_b's own session (CASE H/I above
        # swapped it to their own sessions).
        fake_provider_holder["instance"] = _real_provider(session_b)
        session_b.queue_response(200, _REF_OK)
        session_b.queue_response(200, _CALC_OK)
        session_b.queue_response(200, _REF_OK)
        res_j = await confirm_cmd(f"{lease_b_id}")
        data_j = _json.loads(res_j)
        check("J1. manual confirm on the ambiguous lease is also blocked", data_j.get("ok") is False, data_j)
        check("J2. manual confirm spends ZERO additional times", session_b.prolong_make_call_count() == 1, session_b.calls)

        lease_b_fresh = await storage.proxy_lease_get(lease_b_id, db_path=tmp_db)
        notifications.clear()
        # _prenew_autorenew_one: same calc-preview-then-preflight shape as
        # confirm_cmd above.
        session_b.queue_response(200, _REF_OK)
        session_b.queue_response(200, _CALC_OK)
        session_b.queue_response(200, _REF_OK)
        await autorenew_one(lease_b_fresh)
        check("K1. engine A (_prenew_autorenew_one) on the ambiguous lease spends ZERO additional times", session_b.prolong_make_call_count() == 1, session_b.calls)
        check("K2. engine A's attempt on a durably-blocked lease is NOT silent (alert produced)", len(notifications) >= 1, notifications)

        # wrapped_execute called directly (engine B's own spend path, no
        # calc preview at this layer) -- only its own preflight
        # reference_list runs before op_create collides.
        session_b.queue_response(200, _REF_OK)
        res_l = await wrapped_execute(lease_b_fresh, source="auto")
        check("L1. engine B (source='auto') on the ambiguous lease is skipped/blocked", res_l.get("skipped") is True and res_l.get("error") == "blocked_unresolved", res_l)
        check("L2. engine B spends ZERO additional times", session_b.prolong_make_call_count() == 1, session_b.calls)

        # ------------------------------------------------------------------
        # CASE M: crash-recovery reconcile confirms the provider actually
        # renewed -- op advances to 'success' AND lease.expires_at actually
        # advances, minting a fresh idempotency_key for the next cycle.
        # ------------------------------------------------------------------
        lease_m_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-M", host="10.3.0.10", port=54010, expires_at="2026-08-08")
        idem_m = storage.proxy_renewal_idempotency_key("PXY-CASE-M", "2026-08-08", "1m")
        op_m_id = await storage.proxy_renewal_op_create(
            lease_id=lease_m_id, provider_proxy_id="PXY-CASE-M", idempotency_key=idem_m,
            source="autorenew", status="pending", period_id="1m",
            expires_before="2026-08-08", db_path=tmp_db,
        )
        await storage.proxy_renewal_op_advance(op_m_id, "make_pending", from_status="pending", db_path=tmp_db)
        provider_m = ReconcileFakeProvider()
        provider_m.queue_list_response([{"id": "PXY-CASE-M", "ip": "10.3.0.10", "port_socks": 54010, "date_end": "2026-09-08", "order_id": "ORD-M"}])
        fake_provider_holder["instance"] = provider_m
        await reconcile_fn()

        op_m_after = await storage.proxy_renewal_op_get(op_m_id, db_path=tmp_db)
        check("M1. op advances to 'success' from provider evidence", op_m_after is not None and op_m_after["status"] == "success", op_m_after)
        lease_m_after = await storage.proxy_lease_get(lease_m_id, db_path=tmp_db)
        check("M2. lease.expires_at ACTUALLY advances to the confirmed date",
              lease_m_after is not None and str(lease_m_after.get("expires_at")) == "2026-09-08", lease_m_after)
        check("M3. reconcile NEVER calls prolong_make even when confirming success", provider_m.prolong_make_calls == 0, provider_m.prolong_make_calls)

        idem_m_new = storage.proxy_renewal_idempotency_key("PXY-CASE-M", "2026-09-08", "1m")
        op_m_new = await storage.proxy_renewal_op_create(
            lease_id=lease_m_id, provider_proxy_id="PXY-CASE-M", idempotency_key=idem_m_new,
            source="autorenew", status="pending", period_id="1m",
            expires_before="2026-09-08", db_path=tmp_db,
        )
        check("M4. the NEW expires_at produces a fresh, uncollided idempotency_key", op_m_new is not None, op_m_new)

        # ------------------------------------------------------------------
        # CASE N: reconcile observes provider expiry UNCHANGED -- stays
        # blocked, no second spend, and the durably-stuck lease still
        # produces an actionable alert (silent death stays closed).
        # ------------------------------------------------------------------
        lease_n_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-N", host="10.3.0.11", port=54011, expires_at="2026-08-08")
        idem_n = storage.proxy_renewal_idempotency_key("PXY-CASE-N", "2026-08-08", "1m")
        op_n_id = await storage.proxy_renewal_op_create(
            lease_id=lease_n_id, provider_proxy_id="PXY-CASE-N", idempotency_key=idem_n,
            source="autorenew", status="pending", period_id="1m",
            expires_before="2026-08-08", db_path=tmp_db,
        )
        await storage.proxy_renewal_op_advance(op_n_id, "make_pending", from_status="pending", db_path=tmp_db)
        provider_n = ReconcileFakeProvider()
        provider_n.queue_list_response([{"id": "PXY-CASE-N", "ip": "10.3.0.11", "port_socks": 54011, "date_end": "2026-08-08", "order_id": "ORD-N"}])
        fake_provider_holder["instance"] = provider_n
        await reconcile_fn()
        op_n_after = await storage.proxy_renewal_op_get(op_n_id, db_path=tmp_db)
        check("N1. unchanged provider expiry -> op marked terminal 'failed', not re-attempted", op_n_after is not None and op_n_after["status"] == "failed", op_n_after)
        check("N2. no spend during reconcile itself (read-only)", provider_n.prolong_make_calls == 0, provider_n.prolong_make_calls)

        # Supplementary: reconcile's OWN provider-list call failing must
        # also leave state UNCHANGED, no second spend (distinct from N,
        # which is reconcile successfully observing an unchanged expiry).
        lease_n2_id = await _make_lease(tmp_db, provider_proxy_id="PXY-CASE-N2", host="10.3.0.12", port=54012, expires_at="2026-08-08")
        idem_n2 = storage.proxy_renewal_idempotency_key("PXY-CASE-N2", "2026-08-08", "1m")
        op_n2_id = await storage.proxy_renewal_op_create(
            lease_id=lease_n2_id, provider_proxy_id="PXY-CASE-N2", idempotency_key=idem_n2,
            source="autorenew", status="pending", period_id="1m",
            expires_before="2026-08-08", db_path=tmp_db,
        )
        await storage.proxy_renewal_op_advance(op_n2_id, "make_pending", from_status="pending", db_path=tmp_db)
        provider_n2 = ReconcileFakeProvider()  # no queued list response -> list_proxies raises
        fake_provider_holder["instance"] = provider_n2
        await reconcile_fn()
        op_n2_after = await storage.proxy_renewal_op_get(op_n2_id, db_path=tmp_db)
        check("N3. a failed provider-list reconcile leaves the op status UNCHANGED ('make_pending')",
              op_n2_after is not None and op_n2_after["status"] == "make_pending", op_n2_after)
        check("N4. no spend happened during the failed reconcile attempt", provider_n2.prolong_make_calls == 0, provider_n2.prolong_make_calls)

        # N5: the durably-stuck lease from N1 still produces exactly one
        # actionable alert (silent death stays closed for the AMBIGUOUS
        # bucket, not just for the removed provably-safe one).
        lease_n_fresh = await storage.proxy_lease_get(lease_n_id, db_path=tmp_db)
        notifications.clear()
        session_n = FakeSession()
        session_n.queue_response(200, _REF_OK)   # calc preview's reference_list
        session_n.queue_response(200, _CALC_OK)  # calc preview's prolong_calc
        session_n.queue_response(200, _REF_OK)   # wrapped_execute's own preflight
        fake_provider_holder["instance"] = _real_provider(session_n)
        await autorenew_one(lease_n_fresh)
        check("N5. reconcile-confirmed-unchanged lease still produces an actionable alert (no silent death)", len(notifications) == 1, notifications)
        if notifications:
            body_n5 = notifications[0]["body"]
            check("N6. that alert offers no spend-callback reference", "renew:confirm" not in body_n5 and "prn:confirm" not in body_n5, body_n5)

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S):")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("\nALL PROXY RENEWAL RETRY-POLICY (MONEY-SAFETY) SELFTESTS PASSED")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
