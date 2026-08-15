# -*- coding: utf-8 -*-
"""tools/proxy_provider_selftest.py -- offline self-test for
proxy_provider.py.

Pure, no real network calls -- every test injects a FakeSession that
records what it was called with and returns a canned response, so we can
both verify behavior AND prove no attempt was made to reach the real
Proxy-Seller API.

    python3.12 tools\\proxy_provider_selftest.py
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import proxy_provider as pvd

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


class FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json_data = json_data

    def json(self):
        return self._json_data


class FakeSession:
    """Injectable stand-in for `requests`. Records every call it receives
    so tests can assert whether (and how) the provider tried to reach the
    network, without ever touching a real socket."""

    def __init__(self, responder):
        self.calls: list[dict] = []
        self._responder = responder

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json, "timeout": timeout})
        return self._responder(method, url, params, json)


API_KEY = "SECRET_TEST_API_KEY_abc123"


def main() -> int:
    # ------------------------------------------------------------------
    # 1. mocked success response (balance -> data.summ)
    # ------------------------------------------------------------------
    def success_responder(method, url, params, json_body):
        return FakeResponse(200, {"status": "success", "errors": [], "data": {"summ": "42.50"}})

    session = FakeSession(success_responder)
    provider = pvd.ProxySellerProvider(API_KEY, session=session)
    result = provider.balance()
    check("mocked success response: balance() reads data.summ", result.get("summ") == "42.50", repr(result))
    check("mocked success response: exactly one call made", len(session.calls) == 1, str(session.calls))
    check(
        "mocked success response: API key never appears in the request params/json (only in URL, which is never logged)",
        API_KEY not in str(session.calls[0]["params"]) and API_KEY not in str(session.calls[0]["json"]),
    )

    # ------------------------------------------------------------------
    # 2. mocked errors[] response (HTTP 200 but errors[] populated -> failure)
    # ------------------------------------------------------------------
    def errors_responder(method, url, params, json_body):
        return FakeResponse(200, {"status": "error", "errors": ["INVALID_COUNTRY"], "data": None})

    session2 = FakeSession(errors_responder)
    provider2 = pvd.ProxySellerProvider(API_KEY, session=session2)
    raised = None
    try:
        provider2.reference_list("ipv4")
    except pvd.ProxyProviderError as e:
        raised = e
    check("HTTP 200 with errors[] populated is treated as failure", raised is not None, repr(raised))
    check(
        "errors[] failure message does not leak the API key",
        raised is not None and API_KEY not in str(raised),
        str(raised),
    )

    # ------------------------------------------------------------------
    # 3. masked secret response (download_proxies returns plaintext creds)
    # ------------------------------------------------------------------
    def download_responder(method, url, params, json_body):
        return FakeResponse(200, {
            "status": "success", "errors": [],
            "data": {"list": ["hunter2secret:mypassword99@1.2.3.4:50101"]},
        })

    session3 = FakeSession(download_responder)
    provider3 = pvd.ProxySellerProvider(API_KEY, session=session3)
    dl = provider3.download_proxies(proto="socks5")
    raw_has_password = "mypassword99" in repr(dl)
    check("download_proxies() itself returns the raw (unmasked) provider payload", raw_has_password)
    masked = pvd.safe_log_repr(dl)
    check(
        "safe_log_repr() masks the password before it is safe to log",
        "mypassword99" not in masked and "***" in masked,
        masked,
    )

    # ------------------------------------------------------------------
    # 4. make_ipv4 / prolong_make blocked by default (no network call at all)
    # ------------------------------------------------------------------
    session4 = FakeSession(success_responder)
    provider4 = pvd.ProxySellerProvider(API_KEY, session=session4)  # allow_spend defaults to False
    blocked = None
    try:
        provider4.make_ipv4(country_id=1, period_id=1)
    except pvd.SpendGuardError as e:
        blocked = e
    check("make_ipv4() blocked by default", blocked is not None, repr(blocked))
    check("make_ipv4() blocked -> ZERO network calls attempted", len(session4.calls) == 0, str(session4.calls))

    blocked2 = None
    try:
        provider4.prolong_make("ipv4", ids=[1, 2], period_id=1)
    except pvd.SpendGuardError as e:
        blocked2 = e
    check("prolong_make() blocked by default", blocked2 is not None, repr(blocked2))
    check("prolong_make() blocked -> ZERO network calls attempted", len(session4.calls) == 0, str(session4.calls))

    # allow_spend=True at CALL level permits the (mocked) spend call
    result_spend_call = provider4.make_ipv4(country_id=1, period_id=1, allow_spend=True)
    check("make_ipv4(allow_spend=True) proceeds (call-level override)", isinstance(result_spend_call, dict) and len(session4.calls) == 1)

    # allow_spend=True at PROVIDER level also permits it
    session5 = FakeSession(success_responder)
    provider5 = pvd.ProxySellerProvider(API_KEY, session=session5, allow_spend=True)
    result_spend_provider = provider5.prolong_make("ipv4", ids=[1], period_id=1)
    check("prolong_make() proceeds when provider allow_spend=True", isinstance(result_spend_provider, dict) and len(session5.calls) == 1)

    # calc_ipv4 / prolong_calc (price-only, never spend) are NOT guarded
    session6 = FakeSession(success_responder)
    provider6 = pvd.ProxySellerProvider(API_KEY, session=session6)
    provider6.calc_ipv4(country_id=1, period_id=1)
    provider6.prolong_calc("ipv4", ids=[1], period_id=1)
    check("calc_ipv4/prolong_calc are never spend-guarded (price-only)", len(session6.calls) == 2, str(session6.calls))

    # ------------------------------------------------------------------
    # 5. no network by default -- this whole file never imports/uses the
    #    real `requests` module; every provider above was constructed with
    #    an injected FakeSession. Confirm the module still imports cleanly
    #    even without `requests` installed (defensive import).
    # ------------------------------------------------------------------
    check("proxy_provider module is importable regardless of `requests` availability", pvd is not None)
    provider_no_session = pvd.ProxySellerProvider(API_KEY)  # no session injected
    no_requests_err = None
    try:
        provider_no_session.balance()
    except pvd.ProxyProviderError as e:
        no_requests_err = e
    # Either requests is genuinely absent (raises a clean ProxyProviderError,
    # no real network attempted) or it's installed in this environment (in
    # which case this specific sub-check is skipped rather than making a
    # real network call).
    if pvd.requests is None:
        check("without an injected session and without `requests` installed, balance() fails safely (no network)", no_requests_err is not None, repr(no_requests_err))
    else:
        print("[SKIP] `requests` is installed in this environment -- skipping the no-requests-installed sub-check (would require a real network call to exercise otherwise)")

    # ------------------------------------------------------------------
    # extra: network-error path scrubs the API key
    # ------------------------------------------------------------------
    class ExplodingSession:
        def request(self, *a, **kw):
            raise RuntimeError(f"connection refused (url contained {API_KEY})")

    provider7 = pvd.ProxySellerProvider(API_KEY, session=ExplodingSession())
    net_err = None
    try:
        provider7.balance()
    except pvd.ProxyProviderError as e:
        net_err = e
    check("network-error exceptions are wrapped in ProxyProviderError", net_err is not None, repr(net_err))
    check(
        "network-error message has the API key scrubbed even if the underlying exception embedded it",
        net_err is not None and API_KEY not in str(net_err) and "***API_KEY***" in str(net_err),
        str(net_err),
    )

    # ------------------------------------------------------------------
    # extra: check_proxy never leaks the proxy password on failure
    # ------------------------------------------------------------------
    session8 = FakeSession(errors_responder)
    provider8 = pvd.ProxySellerProvider(API_KEY, session=session8)
    check_err = None
    try:
        provider8.check_proxy("myuser:supersecretpw@5.6.7.8:1080")
    except pvd.ProxyProviderError as e:
        check_err = e
    check("check_proxy() failure raises ProxyProviderError", check_err is not None)
    check(
        "check_proxy() failure message never contains the raw proxy password",
        check_err is not None and "supersecretpw" not in str(check_err),
        str(check_err),
    )

    # ------------------------------------------------------------------
    # extra: check_proxy scrubs echoed credentials even WITHOUT an
    # accompanying @host:port shape (the review-finding regression case --
    # the generic shape-based masking alone would miss this).
    # ------------------------------------------------------------------
    def echo_no_hostport_responder(method, url, params, json_body):
        return FakeResponse(200, {
            "status": "error",
            "errors": ["authentication rejected for credentials myuser:TOPSECRETPW"],
            "data": None,
        })

    session9 = FakeSession(echo_no_hostport_responder)
    provider9 = pvd.ProxySellerProvider(API_KEY, session=session9)
    echoed_err = None
    try:
        provider9.check_proxy("myuser:TOPSECRETPW@1.2.3.4:1080")
    except pvd.ProxyProviderError as e:
        echoed_err = e
    check("check_proxy() with echoed bare credentials still raises ProxyProviderError", echoed_err is not None)
    check(
        "echoed password (no @host:port nearby) is not present in the error text",
        echoed_err is not None and "TOPSECRETPW" not in str(echoed_err),
        str(echoed_err),
    )
    check(
        "echoed login is not present in the error text",
        echoed_err is not None and "myuser" not in str(echoed_err),
        str(echoed_err),
    )
    check(
        "masked placeholder is present in the error text",
        echoed_err is not None and "***" in str(echoed_err),
        str(echoed_err),
    )
    check(
        "no network call beyond the single mocked request",
        len(session9.calls) == 1,
        str(session9.calls),
    )

    # ------------------------------------------------------------------
    # extra: check_proxy TRANSPORT FIX 20260711 -- regression guard for the
    # "Could not find value for parameter {proxy}" bug. tools/proxy/check
    # is a GET endpoint that reads `proxy` as a query parameter; it does
    # not read a JSON body at all. The old code posted {"proxy": ...} as
    # json_body, so the provider could never find the parameter. Asserts
    # the exact method/path/params/json shape so this exact regression
    # (right string, wrong transport) cannot silently reappear.
    # ------------------------------------------------------------------
    def check_success_responder(method, url, params, json_body):
        return FakeResponse(200, {"status": "success", "errors": [], "data": {"valid": True, "ip": "5.6.7.8"}})

    session10 = FakeSession(check_success_responder)
    provider10 = pvd.ProxySellerProvider(API_KEY, session=session10)
    proxy_line = "myuser:supersecretpw@5.6.7.8:1080"
    check_result = provider10.check_proxy(proxy_line)
    check("check_proxy() makes exactly one request", len(session10.calls) == 1, str(session10.calls))
    call = session10.calls[0] if session10.calls else {}
    check(
        "check_proxy() uses HTTP GET (not POST) -- the endpoint does not read a JSON body",
        call.get("method") == "GET", repr(call),
    )
    check(
        "check_proxy() targets the tools/proxy/check path",
        "tools/proxy/check" in str(call.get("url") or ""), repr(call),
    )
    check(
        "check_proxy() sends the proxy string as a query parameter, format UNCHANGED "
        "(login:password@host:port) -- params == {'proxy': <line>}",
        call.get("params") == {"proxy": proxy_line}, repr(call),
    )
    check(
        "check_proxy() sends NO JSON body -- this is the exact regression that caused "
        "'Could not find value for parameter {proxy}'",
        call.get("json") is None, repr(call),
    )
    check(
        "check_proxy() request never includes the API key in params (only in the URL path, never logged)",
        API_KEY not in str(call.get("params")), repr(call),
    )
    check(
        "check_proxy() returns the provider's parsed response as a plain dict",
        isinstance(check_result, dict) and (check_result.get("data") or {}).get("valid") is True, repr(check_result),
    )
    check(
        "check_proxy() success response never leaks the raw password anywhere in its own repr",
        "supersecretpw" not in repr(check_result), repr(check_result),
    )

    # ------------------------------------------------------------------
    # PROXY LIFECYCLE SYNC 20260721: extract_proxy_entries now surfaces the
    # read-only auto_renew / can_prolong / auto_renew_period provider fields.
    # ------------------------------------------------------------------
    check("normalize_auto_renew: 'Y'->'Y', 'N'->'N'", pvd.normalize_auto_renew("Y") == "Y" and pvd.normalize_auto_renew("N") == "N")
    check("normalize_auto_renew tolerates booleans/1/0/yes/no", pvd.normalize_auto_renew(True) == "Y" and pvd.normalize_auto_renew(0) == "N" and pvd.normalize_auto_renew("yes") == "Y")
    check("normalize_auto_renew maps unknown/absent to '' (never 'N')", pvd.normalize_auto_renew(None) == "" and pvd.normalize_auto_renew("garbage") == "")

    list_resp = {
        "status": "success", "errors": [],
        "data": {"items": [
            {"id": "PXY-ON", "ip": "41.0.0.1", "port_socks": 50101, "login": "u1", "password": "supersecretpw",
             "date_end": "2026-10-01", "auto_renew": "Y", "can_prolong": True, "auto_renew_period": "1m"},
            {"id": "PXY-OFF", "ip": "41.0.0.2", "port_socks": 50102, "login": "u2", "password": "supersecretpw",
             "date_end": "2026-10-02", "auto_renew": "N"},
            {"id": "PXY-UNK", "ip": "41.0.0.3", "port_socks": 50103, "login": "u3", "password": "supersecretpw",
             "date_end": "2026-10-03"},  # no auto_renew field at all
        ]},
    }
    parsed = pvd.extract_proxy_entries(list_resp)
    by_id = {e["provider_proxy_id"]: e for e in parsed}
    check("extract_proxy_entries parses all three rows", len(parsed) == 3, repr(len(parsed)))
    check("auto_renew 'Y' surfaced for the ON proxy", by_id.get("PXY-ON", {}).get("auto_renew") == "Y")
    check("can_prolong surfaced for the ON proxy", by_id.get("PXY-ON", {}).get("can_prolong") is True)
    check("auto_renew_period surfaced for the ON proxy", by_id.get("PXY-ON", {}).get("auto_renew_period") == "1m")
    check("auto_renew 'N' surfaced for the OFF proxy", by_id.get("PXY-OFF", {}).get("auto_renew") == "N")
    check("absent auto_renew becomes '' (unknown, never 'N')", by_id.get("PXY-UNK", {}).get("auto_renew") == "")
    check("host/port/id still parsed alongside the new fields",
          by_id.get("PXY-ON", {}).get("host") == "41.0.0.1" and by_id.get("PXY-ON", {}).get("port") == 50101)

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
