# -*- coding: utf-8 -*-
"""tools/proxy_error_classify_selftest.py -- offline selftest for the PROXY
RENEWAL RELIABILITY R4 provider error classification (proxy_provider.py +
main.py, 20260808).

Covers:
  - proxy_provider.ProxyProviderError.kind: set correctly at every raise
    site (transport / provider_errors / http / response_shape / auth /
    spend_guard), and preserved through check_proxy()'s re-raise.
  - proxy_provider._request(): an envelope with an explicit status=error/
    fail/failed is now ALSO treated as failure even with an empty errors[]
    (HTTP 200 is not business success by itself) -- while a MISSING or
    unrecognized status field changes nothing (no false positives).
  - main.py._prenew_classify_provider_error(): maps a provider/spend-tail
    failure to one of the 10 RU categories from the task spec (ip_not_found,
    insufficient_balance, provider_auth, rate_limit, network_timeout,
    invalid_period, provider_unavailable, not_configured, unverified,
    unknown), using the exact production example text
    ("provider returned errors[] on prolong/make/ipv4: [{'message': 'IP not
    found', 'code': 0, 'customData': None}]") among the cases.
  - main.py._prenew_execute_renewal(): every failure branch (not_configured/
    reference_list_failed/period_not_found/make_failed/unverified) now
    carries a category/reason/action -- and NONE of the raw provider
    structure (errors[]/code/customData/dict-repr/traceback/API key) ever
    appears in reason/action, only in the internal `message` field.

main.py cannot be imported standalone -- AST-extraction idiom (same as the
other proxy_*_selftest.py files here). No network, no Telegram, no real
provider writes.

Run:  python tools\\proxy_error_classify_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import storage
import proxy_provider
from proxy_provider import ProxyProviderError, SpendGuardError

MAIN_PY = BASE_DIR / "main.py"

FAILURES: list[str] = []
RAW_MARKERS = ("errors[", "customData", "provider returned", "'code':", "{'message'", "Traceback")


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


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
    exec(compile(module_src, "<main.py error-classify extract>", "exec"), ns)
    return ns


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.calls = 0

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls += 1
        return self._response


def run_provider_kind_checks() -> None:
    print("\n-- proxy_provider.ProxyProviderError.kind (structural classification) --")

    # transport: no requests lib, no session
    orig_requests = proxy_provider.requests
    try:
        proxy_provider.requests = None
        p = proxy_provider.ProxySellerProvider("FAKE_KEY", session=None)
        try:
            p.balance()
            check("1a. no session + no requests -> raises", False)
        except ProxyProviderError as e:
            check("1a. no session + no requests -> kind=transport", e.kind == "transport", e.kind)
    finally:
        proxy_provider.requests = orig_requests

    # transport: session.request raises
    class _RaisingSession:
        def request(self, *a, **kw):
            raise ConnectionError("simulated network failure")

    p2 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=_RaisingSession())
    try:
        p2.balance()
        check("1b. network exception -> raises", False)
    except ProxyProviderError as e:
        check("1b. network exception -> kind=transport", e.kind == "transport", e.kind)

    # response_shape: non-JSON
    class _BadJsonResponse:
        status_code = 200

        def json(self):
            raise ValueError("not json")

    class _BadJsonSession:
        def request(self, *a, **kw):
            return _BadJsonResponse()

    p3 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=_BadJsonSession())
    try:
        p3.balance()
        check("2. non-JSON response -> raises", False)
    except ProxyProviderError as e:
        check("2. non-JSON response -> kind=response_shape", e.kind == "response_shape", e.kind)

    # provider_errors: errors[] non-empty, HTTP 200
    p4 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=FakeSession(FakeResponse(200, {"status": "error", "errors": ["IP not found"], "data": None})))
    try:
        p4.balance()
        check("3. errors[] non-empty -> raises", False)
    except ProxyProviderError as e:
        check("3. errors[] non-empty -> kind=provider_errors", e.kind == "provider_errors", e.kind)

    # R4: envelope status=error with EMPTY errors[] and HTTP 200 -- the gap
    # explicitly called out in the forensic audit ("HTTP 200 does not mean
    # business success").
    p5 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=FakeSession(FakeResponse(200, {"status": "error", "errors": [], "data": None})))
    try:
        p5.balance()
        check("4a. status=error + empty errors[] + HTTP 200 -> STILL raises (envelope status is authoritative)", False)
    except ProxyProviderError as e:
        check("4a. status=error + empty errors[] + HTTP 200 -> raises", True)
        check("4b. status=error + empty errors[] -> kind=provider_errors", e.kind == "provider_errors", e.kind)

    # R4 no-false-positive: MISSING status field, empty errors[], HTTP 200 -> success
    p6 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=FakeSession(FakeResponse(200, {"data": {"summ": "12.34"}})))
    try:
        out = p6.balance()
        check("5. missing status field + empty errors[] -> still treated as success (no false positive)", out.get("summ") == "12.34", out)
    except ProxyProviderError as e:
        check("5. missing status field + empty errors[] -> still treated as success (no false positive)", False, str(e))

    # R4 no-false-positive: status="success", empty errors[], HTTP 200 -> success
    p7 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=FakeSession(FakeResponse(200, {"status": "success", "errors": [], "data": {"summ": "5.00"}})))
    try:
        out = p7.balance()
        check("6. status=success + empty errors[] -> success (unchanged happy path)", out.get("summ") == "5.00", out)
    except ProxyProviderError as e:
        check("6. status=success + empty errors[] -> success (unchanged happy path)", False, str(e))

    # auth: HTTP 401/403, no errors[]
    p8 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=FakeSession(FakeResponse(401, {"status": "success", "errors": [], "data": None})))
    try:
        p8.balance()
        check("7a. HTTP 401 -> raises", False)
    except ProxyProviderError as e:
        check("7a. HTTP 401 with no errors[] -> kind=auth", e.kind == "auth", e.kind)

    p9 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=FakeSession(FakeResponse(403, {"status": "success", "errors": [], "data": None})))
    try:
        p9.balance()
        check("7b. HTTP 403 -> raises", False)
    except ProxyProviderError as e:
        check("7b. HTTP 403 with no errors[] -> kind=auth", e.kind == "auth", e.kind)

    # http: some other 4xx/5xx with no errors[]
    p10 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=FakeSession(FakeResponse(500, {"status": "success", "errors": [], "data": None})))
    try:
        p10.balance()
        check("8. HTTP 500 with no errors[] -> raises", False)
    except ProxyProviderError as e:
        check("8. HTTP 500 with no errors[] -> kind=http", e.kind == "http", e.kind)

    # spend_guard: SpendGuardError always self-tags, no network call
    guard_session = FakeSession(FakeResponse(200, {"status": "success", "errors": [], "data": {}}))
    p11 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=guard_session, allow_spend=False)
    try:
        p11.make_ipv4(1, "1m", allow_spend=False)
        check("9a. make_ipv4 without allow_spend -> raises", False)
    except SpendGuardError as e:
        check("9a. make_ipv4 without allow_spend -> kind=spend_guard", e.kind == "spend_guard", e.kind)
        check("9b. make_ipv4 without allow_spend -> ZERO network calls", guard_session.calls == 0, str(guard_session.calls))

    # check_proxy() re-raise preserves kind
    p12 = proxy_provider.ProxySellerProvider("FAKE_KEY", session=FakeSession(FakeResponse(200, {"status": "error", "errors": ["Could not find value for parameter {proxy}"], "data": None})))
    try:
        p12.check_proxy("u:p@1.2.3.4:1080")
        check("10. check_proxy failure -> raises", False)
    except ProxyProviderError as e:
        check("10. check_proxy() re-raise PRESERVES the original kind (provider_errors)", e.kind == "provider_errors", e.kind)


def main() -> int:
    run_provider_kind_checks()

    main_tree = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))

    ns = extract_and_exec(
        main_tree,
        {"_prenew_classify_provider_error"},
        {"Any": object, "Dict": dict, "Optional": object},
    )
    classify_fn = ns["_prenew_classify_provider_error"]

    print("\n-- main.py._prenew_classify_provider_error: category mapping --")

    # The EXACT production example from the task/forensic audit.
    production_text = "provider returned errors[] on prolong/make/ipv4: [{'message': 'IP not found', 'code': 0, 'customData': None}]"
    r = classify_fn(error_code="make_failed", message=production_text, kind="provider_errors")
    check("11a. production 'IP not found' text classifies as ip_not_found", r["category"] == "ip_not_found", r)
    check("11b. ip_not_found reason is the exact spec RU text", r["reason"] == "Провайдер больше не находит этот прокси.", r)
    check("11c. ip_not_found action is the exact spec RU text", r["action"] == "Проверьте прокси и при необходимости замените его.", r)
    for marker in RAW_MARKERS:
        check(f"11d. classified output never contains raw marker {marker!r}", marker not in r["reason"] and marker not in r["action"], r)

    cases = [
        ("insufficient_balance", "balance too low, key=SECRET123", None),
        ("insufficient_balance", "insufficient funds on account", None),
        ("insufficient_balance", "недостаточно средств на балансе", None),
        ("provider_auth", "authentication rejected for credentials", "auth"),
        ("provider_auth", "HTTP 401 from balance/get", None),
        ("provider_auth", "HTTP 403 from balance/get", None),
        ("rate_limit", "HTTP 429 from proxy/list/ipv4", None),
        ("rate_limit", "too many requests, slow down", None),
        ("network_timeout", "network error calling balance/get: TimeoutError: timed out", "transport"),
        ("network_timeout", "connection refused", "transport"),
        ("provider_unavailable", "HTTP 502 from order/make", "http"),
        ("provider_unavailable", "provider unavailable right now", None),
        ("invalid_period", "requested period is invalid", None),
        ("unknown", "something completely unrecognizable happened", None),
    ]
    for expected_cat, text, kind in cases:
        r = classify_fn(message=text, kind=kind)
        check(f"12. {text[:40]!r} -> {expected_cat}", r["category"] == expected_cat, r)

    r_not_configured = classify_fn(error_code="not_configured", message="Proxy-Seller API key не настроен")
    check("13. error_code=not_configured short-circuits to not_configured", r_not_configured["category"] == "not_configured", r_not_configured)

    r_period = classify_fn(error_code="period_not_found", message="Период 1 month (1m) не найден в справочнике провайдера.")
    check("14. error_code=period_not_found short-circuits to invalid_period", r_period["category"] == "invalid_period", r_period)

    r_unverified = classify_fn(error_code="unverified", message="Списание могло пройти, но новый срок действия не подтверждён провайдером.")
    check("15. error_code=unverified short-circuits to unverified", r_unverified["category"] == "unverified", r_unverified)
    check("15b. unverified action tells the admin to sync + check (matches spec)", "синхронизируйте" in r_unverified["action"].lower(), r_unverified)

    check("16. garbage input never raises, always falls back to unknown", classify_fn(message=None, kind=None, error_code=None)["category"] == "unknown")

    print("\n-- All 10 spec categories are reachable and produce non-empty RU text --")
    all_categories = {
        "ip_not_found", "insufficient_balance", "provider_auth", "rate_limit", "network_timeout",
        "invalid_period", "provider_unavailable", "not_configured", "unverified", "unknown",
    }
    reached = set()
    reached.add(classify_fn(error_code="not_configured")["category"])
    reached.add(classify_fn(error_code="unverified")["category"])
    reached.add(classify_fn(error_code="period_not_found")["category"])
    reached.add(classify_fn(message="ip not found")["category"])
    reached.add(classify_fn(message="insufficient balance")["category"])
    reached.add(classify_fn(message="unauthorized", kind="auth")["category"])
    reached.add(classify_fn(message="429 too many requests")["category"])
    reached.add(classify_fn(message="timeout", kind="transport")["category"])
    reached.add(classify_fn(message="HTTP 503", kind="http")["category"])
    reached.add(classify_fn(message="nonsense")["category"])
    check("17. every one of the 10 spec categories is reachable", reached == all_categories, sorted(reached))
    for cat, text, kind in [
        ("ip_not_found", "proxy not found", None), ("insufficient_balance", "not enough balance", None),
        ("provider_auth", "access denied", None), ("rate_limit", "rate limit", None),
        ("network_timeout", "timed out", None), ("invalid_period", "invalid period", None),
        ("provider_unavailable", "service unavailable", None), ("not_configured", "", None),
        ("unverified", "", None), ("unknown", "??", None),
    ]:
        r = classify_fn(error_code=cat if cat in ("not_configured", "unverified") else "", message=text)
        check(f"18. category {cat!r} has non-empty reason/action", bool(r["reason"]) and bool(r["action"]), r)

    print("\n-- main.py._prenew_execute_renewal: every failure branch carries category/reason/action --")

    ns2 = extract_and_exec(
        main_tree,
        {
            "_prenew_preflight_check", "_prenew_execute_renewal", "_prenew_verify_renewal_via_refresh", "_prenew_find_provider_entry",
            "_prenew_classify_provider_error", "_ppool_parse_expires_at", "_pbuy_safe_error",
        },
        {
            "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
            "datetime": datetime, "asyncio": asyncio,
            "_pbuy_provider": lambda: None,  # not_configured branch
            "_pbuy_call": None,
            "_PRENEW_PROXY_TYPE": "ipv4", "_PRENEW_PAYMENT_ID": 1,
            "_PBUY_PERIOD_ID": "1m", "_PBUY_PERIOD_NAME": "1 month",
            "_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS": 1, "_PRENEW_VERIFY_REFRESH_BACKOFF_SEC": 0.0,
        },
    )
    execute_renewal_fn = ns2["_prenew_execute_renewal"]

    async def _run_not_configured_check():
        result = await execute_renewal_fn({"id": 1, "expires_at": "2026-08-08", "provider_proxy_id": "PXY-1"}, source="test")
        check("19a. not_configured branch is ok=False", result.get("ok") is False, result)
        check("19b. not_configured branch carries category=not_configured", result.get("error_category") == "not_configured", result)
        check("19c. not_configured branch carries non-empty reason/action", bool(result.get("reason")) and bool(result.get("action")), result)

    asyncio.run(_run_not_configured_check())

    with tempfile.TemporaryDirectory(prefix="tpilot_errclassify_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        rp = os.path.realpath(tmp_db)
        assert os.path.realpath(tempfile.gettempdir()) in rp and "data_tpilot.db" not in rp

        async def _run_make_failed_check():
            lease_id = await storage.proxy_lease_create(
                provider_type="proxy_seller", host="1.1.1.1", port=50101, provider_proxy_id="PXY-ERR-1",
                manager_key=None, status="active", expires_at="2026-08-08", db_path=tmp_db,
            )
            lease = await storage.proxy_lease_get(lease_id, db_path=tmp_db)

            class _FakeProvider:
                def reference_list(self, proxy_type):
                    return {"data": [{"id": "1m", "name": "1 month"}]}

                def prolong_make(self, proxy_type, ids, period_id, payment_id, *, allow_spend=False):
                    raise ProxyProviderError(
                        "provider returned errors[] on prolong/make/ipv4: [{'message': 'IP not found', 'code': 0, 'customData': None}]",
                        kind="provider_errors",
                    )

            fake = _FakeProvider()

            async def _fake_pbuy_call(fn, *a, **kw):
                return fn(*a, **kw)

            ns3 = extract_and_exec(
                main_tree,
                {
                    "_prenew_preflight_check", "_prenew_execute_renewal", "_prenew_verify_renewal_via_refresh", "_prenew_find_provider_entry",
                    "_prenew_classify_provider_error", "_ppool_parse_expires_at", "_pbuy_safe_error",
                },
                {
                    "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
                    "datetime": datetime, "asyncio": asyncio,
                    "_pbuy_provider": lambda: fake,
                    "_pbuy_call": _fake_pbuy_call,
                    "_PRENEW_PROXY_TYPE": "ipv4", "_PRENEW_PAYMENT_ID": 1,
                    "_PBUY_PERIOD_ID": "1m", "_PBUY_PERIOD_NAME": "1 month",
                    "_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS": 1, "_PRENEW_VERIFY_REFRESH_BACKOFF_SEC": 0.0,
                    "TPILOT_DB_PATH": tmp_db,
                },
            )
            result = await ns3["_prenew_execute_renewal"](lease, source="test")
            check("20a. make_failed (production IP-not-found text) -> ok=False", result.get("ok") is False, result)
            check("20b. make_failed classifies as ip_not_found", result.get("error_category") == "ip_not_found", result)
            check("20c. reason is the clean RU spec text, not the raw provider structure", result.get("reason") == "Провайдер больше не находит этот прокси.", result)
            check("20d. action is the clean RU spec text", result.get("action") == "Проверьте прокси и при необходимости замените его.", result)
            for marker in RAW_MARKERS:
                check(f"20e. reason/action never leak raw marker {marker!r}", marker not in str(result.get("reason")) and marker not in str(result.get("action")), result)
            check("20f. internal 'message' field (technical/log-only) still carries the raw scrubbed text", "IP not found" in str(result.get("message")), result)

        asyncio.run(_run_make_failed_check())

    print(f"\n{'='*70}")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
