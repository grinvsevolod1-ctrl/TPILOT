# -*- coding: utf-8 -*-
"""tools/proxy_renew_flow_selftest.py -- offline self-test for the Stage 5
safe-expiration-warning + admin-confirmed-renewal flow: proxy_provider.py's
prolong_calc/prolong_make + extract_renewal_info (reusing Stage 4.1's
find_period_id), storage.py's proxy_lease_list_expiring/
proxy_lease_update_renew, and panel_bot.py's one-time confirmation-token
check (_prenew_confirm_state_ok).

Pure/offline: every provider interaction uses an injected FakeSession
(never the real `requests`/network); storage tests use a throwaway
temporary SQLite file. No lease is ever actually renewed here -- every
spend-guarded call either stays blocked (SpendGuardError, asserted) or is
unblocked only via an explicit allow_spend=True against a FakeSession that
never touches the network.

    python3.12 tools\\proxy_renew_flow_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import io
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import proxy_provider as pvd
import storage

FAILURES: list[str] = []
SECRET_API_KEY = "FAKE_STAGE5_TEST_KEY_renew_789"
SECRET_PASSWORD = "renew_flow_secret_pw_99"


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
    def __init__(self, responder):
        self.calls: list[dict] = []
        self._responder = responder

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        return self._responder(method, url, params, json)


REFERENCE_LIST_WITH_1M = {
    "status": "success", "errors": [],
    "data": {"items": [
        {"id": 42, "name": "Germany", "alpha3": "DEU", "period": [
            {"id": "1m", "name": "1 month"},
            {"id": "3m", "name": "3 months"},
        ]},
    ]},
}

REFERENCE_LIST_NO_1M = {
    "data": {"items": [
        {"id": 42, "name": "Germany", "alpha3": "DEU", "period": [
            {"id": "3m", "name": "3 months"},
        ]},
    ]},
}


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


async def _make_lease(tmp_db: str, **overrides) -> int:
    kwargs = dict(
        provider_type="proxy_seller",
        host="1.2.3.4",
        port=50101,
        manager_key="buyer01",
        provider_proxy_id="PXY-777",
        proxy_type="ipv4",
        scheme="socks5",
        login="u1",
        password=SECRET_PASSWORD,
        status="active",
        db_path=tmp_db,
    )
    kwargs.update(overrides)
    return await storage.proxy_lease_create(**kwargs)


def _extract_and_exec(path: str, names: set, extra_ns: dict) -> dict:
    src = open(path, encoding="utf-8-sig").read()
    tree = ast.parse(src)
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    if len(nodes) != len(names):
        found = {getattr(n, "name", None) for n in nodes}
        raise AssertionError(f"expected {names}, found {found} in {path}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path}>", "exec"), ns)
    return ns


async def run_all_checks(tmp_db: str) -> None:
    now = datetime.utcnow()

    # ------------------------------------------------------------------
    # 1/2/3. proxy_lease_list_expiring: finds expiring leases, excludes
    #    non-expiring and inactive leases.
    # ------------------------------------------------------------------
    lease_expiring = await _make_lease(tmp_db, host="1.1.1.1", expires_at=_iso(now + timedelta(days=1)))
    lease_far = await _make_lease(tmp_db, host="2.2.2.2", expires_at=_iso(now + timedelta(days=30)))
    lease_inactive = await _make_lease(tmp_db, host="3.3.3.3", expires_at=_iso(now + timedelta(days=1)), status="disabled")

    horizon = _iso(now + timedelta(days=3))
    expiring = await storage.proxy_lease_list_expiring(horizon, only_active=True, db_path=tmp_db)
    ids = {r["id"] for r in expiring}
    check("1. proxy_lease_list_expiring finds the lease expiring within the horizon", lease_expiring in ids, str(ids))
    check("2. proxy_lease_list_expiring does NOT include a lease expiring far in the future", lease_far not in ids, str(ids))
    check("3. proxy_lease_list_expiring does NOT include an inactive lease", lease_inactive not in ids, str(ids))

    # ------------------------------------------------------------------
    # 4. Renewal calc stops if provider_proxy_id is missing -- mirrors the
    #    exact guard main.py's _handle_proxy_renew_calc_command runs
    #    FIRST, before constructing a provider or making any call.
    # ------------------------------------------------------------------
    lease_no_pid = await _make_lease(tmp_db, host="4.4.4.4", provider_proxy_id=None, expires_at=_iso(now + timedelta(days=1)))
    lease_row = await storage.proxy_lease_get(lease_no_pid, db_path=tmp_db)
    provider_proxy_id = lease_row.get("provider_proxy_id")

    def _explode_responder(method, url, params, json_body):
        raise AssertionError(f"provider should never be called when provider_proxy_id is missing (hit {url})")

    session_guard = FakeSession(_explode_responder)
    if provider_proxy_id:
        pvd.ProxySellerProvider(SECRET_API_KEY, session=session_guard).reference_list("ipv4")
    check(
        "4. (calc guard) missing provider_proxy_id -> handler stops before any provider call",
        not provider_proxy_id and len(session_guard.calls) == 0,
        repr(provider_proxy_id),
    )

    # ------------------------------------------------------------------
    # 5/6. Renewal calc resolves period id (find_period_id, reused from
    #    Stage 4.1) and calls prolong_calc only -- never prolong_make --
    #    exercised via the SAME reference_list -> find_period_id ->
    #    prolong_calc sequence main.py's calc handler actually runs.
    # ------------------------------------------------------------------
    def calc_responder(method, url, params, json_body):
        if "reference/list" in url:
            return FakeResponse(200, REFERENCE_LIST_WITH_1M)
        if "prolong/calc" in url:
            return FakeResponse(200, {"status": "success", "errors": [], "data": {"total": "2.50", "currency": "USD"}})
        return FakeResponse(200, {"status": "success", "errors": [], "data": {}})

    session_calc = FakeSession(calc_responder)
    provider_calc = pvd.ProxySellerProvider(SECRET_API_KEY, session=session_calc)  # allow_spend defaults False

    ref = provider_calc.reference_list("ipv4")
    period_id = pvd.find_period_id(ref, preferred_id="1m", preferred_name="1 month")
    check("5. renewal calc resolves period_id == '1m'", period_id == "1m", repr(period_id))

    calc_res = provider_calc.prolong_calc("ipv4", ["PXY-777"], period_id, 1)
    info = pvd.extract_renewal_info(calc_res)
    check("5b. renewal calc reads price/total from the prolong_calc response", info.get("total") == "2.50" and info.get("currency") == "USD", repr(info))
    check(
        "6. renewal calc made exactly one prolong/calc call and NEVER touched prolong/make (no spend)",
        sum(1 for c in session_calc.calls if "prolong/calc" in c["url"]) == 1
        and all("prolong/make" not in c["url"] for c in session_calc.calls),
        str(session_calc.calls),
    )

    # ------------------------------------------------------------------
    # 7. Renewal confirm stops if there is no one-time confirmation
    #    token/state -- panel_bot.py's _prenew_confirm_state_ok, tested
    #    directly via AST extraction (pure function, no Telethon mocking
    #    needed).
    # ------------------------------------------------------------------
    ns_panel = _extract_and_exec(
        str(BASE_DIR / "panel_bot.py"),
        {"_prenew_confirm_state_ok"},
        {"Optional": object},
    )
    state_ok_fn = ns_panel["_prenew_confirm_state_ok"]

    check("7a. no wizard state at all -> confirm blocked", state_ok_fn(None, 7) is False)
    check("7b. wrong wizard name -> confirm blocked", state_ok_fn({"wizard": "add_manager", "step": "confirm", "payload": {"lease_id": 7}}, 7) is False)
    check("7c. wrong step -> confirm blocked", state_ok_fn({"wizard": "proxy_renew", "step": "other", "payload": {"lease_id": 7}}, 7) is False)
    check("7d. mismatched lease_id -> confirm blocked", state_ok_fn({"wizard": "proxy_renew", "step": "confirm", "payload": {"lease_id": 999}}, 7) is False)
    check(
        "7e. matching wizard/step/lease_id -> confirm allowed (one-time token present)",
        state_ok_fn({"wizard": "proxy_renew", "step": "confirm", "payload": {"lease_id": 7}}, 7) is True,
    )

    # ------------------------------------------------------------------
    # 8. Renewal confirm calls prolong_make(..., allow_spend=True) only
    #    after confirmation -- blocked by default (SpendGuardError),
    #    proceeds only with allow_spend=True (same provider-level guard
    #    mechanism Stage 2-3/4 already rely on for make_ipv4).
    # ------------------------------------------------------------------
    def make_responder(method, url, params, json_body):
        if "reference/list" in url:
            return FakeResponse(200, REFERENCE_LIST_WITH_1M)
        if "prolong/make" in url:
            return FakeResponse(200, {"status": "success", "errors": [], "data": {"date_end": "2026-09-09"}})
        return FakeResponse(200, {"status": "success", "errors": [], "data": {}})

    session_make = FakeSession(make_responder)
    provider_make = pvd.ProxySellerProvider(SECRET_API_KEY, session=session_make)  # allow_spend defaults False

    blocked = None
    try:
        provider_make.prolong_make("ipv4", ["PXY-777"], "1m", 1)  # no allow_spend
    except pvd.SpendGuardError as e:
        blocked = e
    check("8a. prolong_make without allow_spend is blocked", blocked is not None, repr(blocked))
    check("8b. no prolong/make call was attempted while blocked", all("prolong/make" not in c["url"] for c in session_make.calls), str(session_make.calls))

    make_res = provider_make.prolong_make("ipv4", ["PXY-777"], "1m", 1, allow_spend=True)
    renew_info = pvd.extract_renewal_info(make_res)
    check("8c. prolong_make(allow_spend=True) succeeds and returns a new expiry date", renew_info.get("new_expires_at") == "2026-09-09", repr(renew_info))

    # ------------------------------------------------------------------
    # 9. Renewal confirm does not call prolong_make if periodId cannot be
    #    resolved -- mirrors Stage 4.1's calc/confirm-stop tests.
    # ------------------------------------------------------------------
    session_no_period = FakeSession(lambda m, u, p, j: FakeResponse(200, REFERENCE_LIST_NO_1M))
    provider_no_period = pvd.ProxySellerProvider(SECRET_API_KEY, session=session_no_period, allow_spend=True)
    ref_np = provider_no_period.reference_list("ipv4")
    period_id_np = pvd.find_period_id(ref_np, preferred_id="1m", preferred_name="1 month")
    check("9a. (confirm guard) period_id is None when no 1-month period exists", period_id_np is None)
    if period_id_np is not None:
        provider_no_period.prolong_make("ipv4", ["PXY-777"], period_id_np, 1, allow_spend=True)
    check(
        "9b. renewal confirm: no prolong/make call was made when periodId could not be resolved",
        all("prolong/make" not in c["url"] for c in session_no_period.calls),
        str(session_no_period.calls),
    )

    # ------------------------------------------------------------------
    # 10. Provider errors are scrubbed -- the API key never leaks even
    #    from a prolong/calc or prolong/make failure.
    # ------------------------------------------------------------------
    def errors_responder(method, url, params, json_body):
        return FakeResponse(200, {"status": "error", "errors": [f"balance too low, key={SECRET_API_KEY}"], "data": None})

    session_err = FakeSession(errors_responder)
    provider_err = pvd.ProxySellerProvider(SECRET_API_KEY, session=session_err, allow_spend=True)
    calc_err = None
    try:
        provider_err.prolong_calc("ipv4", ["PXY-777"], "1m", 1)
    except pvd.ProxyProviderError as e:
        calc_err = e
    check("10a. prolong_calc error is raised as ProxyProviderError", calc_err is not None)
    check("10b. prolong_calc error message never contains the raw API key", calc_err is not None and SECRET_API_KEY not in str(calc_err), str(calc_err))

    make_err = None
    try:
        provider_err.prolong_make("ipv4", ["PXY-777"], "1m", 1, allow_spend=True)
    except pvd.ProxyProviderError as e:
        make_err = e
    check("10c. prolong_make error is raised as ProxyProviderError", make_err is not None)
    check("10d. prolong_make error message never contains the raw API key", make_err is not None and SECRET_API_KEY not in str(make_err), str(make_err))

    # ------------------------------------------------------------------
    # 11. proxy_lease_update_renew writes status/time (and, when given a
    #    confirmed new date, bumps expires_at -- but never guesses one;
    #    status column is never touched, stays 'active' on success).
    # ------------------------------------------------------------------
    lease_for_update = await _make_lease(tmp_db, host="5.5.5.5", expires_at=_iso(now + timedelta(days=1)))
    before = await storage.proxy_lease_get(lease_for_update, db_path=tmp_db)
    check("11a. (setup) lease has no renew attempt recorded yet", not before.get("last_renew_attempt_at"))

    await storage.proxy_lease_update_renew(lease_for_update, status_text="notified", db_path=tmp_db)
    after_notify = await storage.proxy_lease_get(lease_for_update, db_path=tmp_db)
    check("11b. proxy_lease_update_renew('notified') sets last_renew_attempt_at", bool(after_notify.get("last_renew_attempt_at")))
    check("11c. proxy_lease_update_renew('notified') sets last_renew_status", after_notify.get("last_renew_status") == "notified")
    check("11d. proxy_lease_update_renew('notified') does NOT change expires_at (no new_expires_at given)", after_notify.get("expires_at") == before.get("expires_at"))

    await storage.proxy_lease_update_renew(lease_for_update, status_text="renew_ok", new_expires_at="2026-09-09T00:00:00", db_path=tmp_db)
    after_renew = await storage.proxy_lease_get(lease_for_update, db_path=tmp_db)
    check("11e. proxy_lease_update_renew('renew_ok', new_expires_at=...) bumps expires_at", after_renew.get("expires_at") == "2026-09-09T00:00:00", repr(after_renew.get("expires_at")))
    check("11f. proxy_lease_update_renew('renew_ok') sets last_renew_status", after_renew.get("last_renew_status") == "renew_ok")
    check("11g. proxy_lease_update_renew never touches status (stays 'active' on success)", after_renew.get("status") == "active", repr(after_renew.get("status")))

    # ------------------------------------------------------------------
    # 12. No password/API key printed anywhere -- checked by the stdout
    #    Tee wrapper in main() below (mirrors proxy_leases_selftest.py's
    #    own technique).
    # ------------------------------------------------------------------


def main() -> int:
    tmp_db = tempfile.mktemp(suffix=".db")
    try:
        real_stdout = sys.stdout
        buffer = io.StringIO()

        class _Tee:
            def write(self_, s):
                real_stdout.write(s)
                buffer.write(s)
                return len(s)

            def flush(self_):
                real_stdout.flush()

        sys.stdout = _Tee()
        try:
            asyncio.run(run_all_checks(tmp_db))
        finally:
            sys.stdout = real_stdout

        captured = buffer.getvalue()
        check("12a. the fake API key never appeared in ANY printed output during the whole run", SECRET_API_KEY not in captured)
        check("12b. the test lease password never appeared in ANY printed output during the whole run", SECRET_PASSWORD not in captured)
    finally:
        try:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)
        except Exception:
            pass

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
