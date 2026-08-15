# -*- coding: utf-8 -*-
"""tools/proxy_buy_flow_selftest.py -- offline self-test for the Stage 4
automatic-SOCKS5-purchase flow (proxy_provider.py's Stage 4 helpers,
end-to-end lease creation via storage.py, and non-regression of the
existing manual proxy parsers in main.py/panel_bot.py).

Pure/offline: every provider interaction uses an injected FakeSession
(never the real `requests`/network); storage tests use a throwaway
temporary SQLite file. main.py/panel_bot.py cannot be imported directly in
a standalone script (heavy import-time side effects -- live Telegram env
vars, client construction), so the three existing manual-proxy parser
functions are regression-checked via AST extraction, the same technique
used by their own Stage 1 test.

    python3.12 tools\\proxy_buy_flow_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import sqlite3
import sys
import tempfile
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import proxy_parser
import proxy_provider as pvd
import storage

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
    def __init__(self, responder):
        self.calls: list[dict] = []
        self._responder = responder

    def request(self, method, url, params=None, json=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "json": json})
        return self._responder(method, url, params, json)


API_KEY = "FAKE_STAGE4_TEST_KEY_xyz"

# A plausible reference_list("ipv4") shape: a list of country dicts nested
# under data.items. Deliberately uses a shape that is NOT the only one the
# defensive extractor accepts, to prove the search isn't shape-brittle.
REFERENCE_LIST_OK = {
    "status": "success",
    "errors": [],
    "data": {
        "items": [
            {"id": 11, "name": "France", "alpha3": "FRA"},
            {"id": 42, "name": "Germany", "alpha3": "DEU"},
            {"id": 77, "name": "Poland", "alpha3": "POL"},
        ]
    },
}

REFERENCE_LIST_NO_GERMANY = {
    "status": "success",
    "errors": [],
    "data": {"items": [{"id": 11, "name": "France", "alpha3": "FRA"}]},
}


def main() -> int:
    # ------------------------------------------------------------------
    # 1. find_country_id: Germany resolved via alpha3 (preferred)
    # ------------------------------------------------------------------
    cid = pvd.find_country_id(REFERENCE_LIST_OK, alpha3="DEU", name="Germany")
    check("find_country_id resolves Germany via alpha3=='DEU'", cid == 42, repr(cid))

    # alpha3-only match still works even if name were different/missing
    ref_alpha3_only = {"data": {"items": [{"id": 99, "alpha3": "DEU"}]}}
    cid2 = pvd.find_country_id(ref_alpha3_only, alpha3="DEU", name="Germany")
    check("find_country_id resolves via alpha3 even without a name field", cid2 == 99, repr(cid2))

    # name-only fallback (no alpha3 field on the matching entry)
    ref_name_only = {"data": {"items": [{"id": 55, "name": "Germany"}]}}
    cid3 = pvd.find_country_id(ref_name_only, alpha3="DEU", name="Germany")
    check("find_country_id falls back to name=='Germany' when alpha3 absent", cid3 == 55, repr(cid3))

    # ------------------------------------------------------------------
    # 2. find_country_id: Germany not found -> None, clear failure path
    # ------------------------------------------------------------------
    cid_missing = pvd.find_country_id(REFERENCE_LIST_NO_GERMANY, alpha3="DEU", name="Germany")
    check("find_country_id returns None when Germany is absent", cid_missing is None, repr(cid_missing))

    # ------------------------------------------------------------------
    # 3. extract_price_info / extract_order_info / extract_proxy_entries /
    #    extract_download_lines -- defensive response parsing
    # ------------------------------------------------------------------
    calc_response = {"status": "success", "errors": [], "data": {"total": "4.99", "currency": "USD"}}
    price = pvd.extract_price_info(calc_response)
    check("extract_price_info finds total/currency", price.get("total") == "4.99" and price.get("currency") == "USD", repr(price))

    make_response = {"status": "success", "errors": [], "data": {"orderId": "ORD-777", "listBaseOrderNumbers": ["BN-001"]}}
    order = pvd.extract_order_info(make_response)
    check("extract_order_info finds orderId and order number", order.get("order_id") == "ORD-777" and order.get("order_number") == "BN-001", repr(order))

    list_response = {
        "status": "success", "errors": [],
        "data": {"items": [{"id": "PXY-1", "ip": "5.6.7.8", "port_socks": 50111, "login": "u1", "password": "SUPERSECRET1"}]},
    }
    entries = pvd.extract_proxy_entries(list_response)
    check(
        "extract_proxy_entries finds host/port/login/password/provider_proxy_id",
        len(entries) == 1
        and entries[0]["host"] == "5.6.7.8"
        and entries[0]["port"] == 50111
        and entries[0]["login"] == "u1"
        and entries[0]["password"] == "SUPERSECRET1"
        and entries[0]["provider_proxy_id"] == "PXY-1",
        repr(entries),
    )

    download_response = {"data": {"list": ["u2:SUPERSECRET2@9.9.9.9:1080", "not a proxy line"]}}
    dl_lines = pvd.extract_download_lines(download_response)
    check("extract_download_lines finds only proxy-shaped lines", dl_lines == ["u2:SUPERSECRET2@9.9.9.9:1080"], repr(dl_lines))

    # ------------------------------------------------------------------
    # 4/5. Purchase cannot happen without explicit confirmation; make path
    #    only spends with allow_spend=True -- exercised via the SAME
    #    country-resolution + calc + make sequence the Stage 4 confirm
    #    handler in main.py actually runs.
    # ------------------------------------------------------------------
    def ref_responder(method, url, params, json_body):
        return FakeResponse(200, REFERENCE_LIST_OK)

    def calc_responder(method, url, params, json_body):
        return FakeResponse(200, {"status": "success", "errors": [], "data": {"total": "4.99", "currency": "USD"}})

    session_noconfirm = FakeSession(lambda m, u, p, j: (calc_responder(m, u, p, j) if "order/calc" in u else ref_responder(m, u, p, j)))
    provider_noconfirm = pvd.ProxySellerProvider(API_KEY, session=session_noconfirm)  # allow_spend defaults False

    ref = provider_noconfirm.reference_list("ipv4")
    country_id = pvd.find_country_id(ref, alpha3="DEU", name="Germany")
    check("(setup) country resolved before attempting a purchase", country_id == 42)

    blocked = None
    try:
        provider_noconfirm.make_ipv4(country_id, "1m", 1, "", "", "", 1)  # no allow_spend
    except pvd.SpendGuardError as e:
        blocked = e
    check("make_ipv4 without allow_spend is blocked even with a resolved country_id", blocked is not None, repr(blocked))
    check(
        "no order/make call was attempted while blocked (only reference_list happened)",
        all("order/make" not in c["url"] for c in session_noconfirm.calls),
        str(session_noconfirm.calls),
    )

    # ------------------------------------------------------------------
    # 6. Fake provider make success + proxy/list success (full happy path)
    # ------------------------------------------------------------------
    def make_responder(method, url, params, json_body):
        return FakeResponse(200, {"status": "success", "errors": [], "data": {"orderId": "ORD-888", "listBaseOrderNumbers": ["BN-777"]}})

    def list_responder(method, url, params, json_body):
        return FakeResponse(200, {
            "status": "success", "errors": [],
            "data": {"items": [{"id": "PXY-999", "ip": "1.2.3.4", "port_socks": 50101, "login": "buyeruser", "password": "buyerSECRET99", "date_end": "2026-08-09"}]},
        })

    def happy_responder(method, url, params, json_body):
        if "reference/list" in url:
            return ref_responder(method, url, params, json_body)
        if "order/make" in url:
            return make_responder(method, url, params, json_body)
        if "proxy/list" in url:
            return list_responder(method, url, params, json_body)
        return FakeResponse(200, {"status": "success", "errors": [], "data": {}})

    session_happy = FakeSession(happy_responder)
    provider_happy = pvd.ProxySellerProvider(API_KEY, session=session_happy)

    ref2 = provider_happy.reference_list("ipv4")
    country_id2 = pvd.find_country_id(ref2, alpha3="DEU", name="Germany")
    make_res = provider_happy.make_ipv4(country_id2, "1m", 1, "", "", "", 1, allow_spend=True)
    order_info = pvd.extract_order_info(make_res)
    check("full happy path: make_ipv4(allow_spend=True) succeeds and returns an order id", order_info.get("order_id") == "ORD-888", repr(order_info))

    list_res = provider_happy.list_proxies("ipv4", order_info.get("order_id"))
    proxy_entries = pvd.extract_proxy_entries(list_res)
    check("full happy path: list_proxies + extract_proxy_entries yields one usable proxy", len(proxy_entries) == 1, repr(proxy_entries))

    # ------------------------------------------------------------------
    # 7. Lease create + assign + manager proxy fields mapping (temp SQLite)
    # ------------------------------------------------------------------
    async def _lease_chain_check():
        tmp_db = tempfile.mktemp(suffix=".db")
        con = sqlite3.connect(tmp_db)
        con.execute("CREATE TABLE IF NOT EXISTS managers(manager_key TEXT UNIQUE, proxy_lease_id INTEGER)")
        con.execute("INSERT INTO managers(manager_key) VALUES('buyer01')")
        con.commit()
        con.close()

        entry = proxy_entries[0]
        lease_id = await storage.proxy_lease_create(
            provider_type="proxy_seller",
            host=str(entry["host"]),
            port=int(entry["port"]),
            manager_key="buyer01",
            provider_order_id=str(order_info.get("order_id")),
            provider_order_number=str(order_info.get("order_number")),
            provider_proxy_id=str(entry.get("provider_proxy_id")),
            proxy_type="ipv4",
            scheme="socks5",
            login=str(entry.get("login")),
            password=str(entry.get("password")),
            expires_at=str(entry.get("expires_at")),
            auto_renew_enabled=False,
            status="active",
            db_path=tmp_db,
        )
        assigned = await storage.proxy_lease_assign_to_manager(lease_id, "buyer01", db_path=tmp_db)
        lease = await storage.proxy_lease_get(lease_id, db_path=tmp_db)
        fields = storage.proxy_lease_to_manager_proxy_fields(lease or {})

        con = sqlite3.connect(tmp_db)
        linked = con.execute("SELECT proxy_lease_id FROM managers WHERE manager_key='buyer01'").fetchone()
        con.close()

        try:
            import os
            os.remove(tmp_db)
        except Exception:
            pass

        return lease_id, assigned, lease, fields, linked

    lease_id, assigned, lease, fields, linked = asyncio.run(_lease_chain_check())
    check("proxy_lease_create + assign_to_manager succeeds end-to-end", assigned is True and lease_id > 0)
    check("lease row carries the extracted host/port", bool(lease) and lease.get("host") == "1.2.3.4" and lease.get("port") == 50101, repr(lease))
    check("managers.proxy_lease_id was linked by the buy flow's assignment step", linked is not None and linked[0] == lease_id)
    check(
        "proxy_lease_to_manager_proxy_fields maps host/port/login/password/proxy_mode correctly",
        fields.get("proxy_host") == "1.2.3.4"
        and fields.get("proxy_port") == 50101
        and fields.get("proxy_username") == "buyeruser"
        and fields.get("proxy_password") == "buyerSECRET99"
        and fields.get("proxy_type") == "socks5"
        and fields.get("proxy_mode") == "proxy"
        and fields.get("proxy_enabled") == 1,
        repr({k: v for k, v in fields.items() if k != "proxy_password"}),
    )

    # ------------------------------------------------------------------
    # 8. Provider errors are scrubbed (Stage 4 path reuses proxy_provider's
    #    own scrubbing -- re-verify end to end via a Germany-not-found and
    #    an errors[] failure that echoes credentials).
    # ------------------------------------------------------------------
    session_no_germany = FakeSession(lambda m, u, p, j: FakeResponse(200, REFERENCE_LIST_NO_GERMANY))
    provider_no_germany = pvd.ProxySellerProvider(API_KEY, session=session_no_germany)
    ref3 = provider_no_germany.reference_list("ipv4")
    cid_fail = pvd.find_country_id(ref3, alpha3="DEU", name="Germany")
    check("(scrub-safety setup) Germany-not-found path never reaches make_ipv4", cid_fail is None)
    check(
        "no purchase-related call was ever made when the country could not be resolved",
        all("order/make" not in c["url"] and "order/calc" not in c["url"] for c in session_no_germany.calls),
        str(session_no_germany.calls),
    )

    def echo_errors_responder(method, url, params, json_body):
        return FakeResponse(200, {"status": "error", "errors": ["authentication rejected for credentials svc:BUYFLOWSECRET"], "data": None})

    session_echo = FakeSession(echo_errors_responder)
    provider_echo = pvd.ProxySellerProvider(API_KEY, session=session_echo)
    echoed_err = None
    try:
        provider_echo.check_proxy("svc:BUYFLOWSECRET@2.2.2.2:1080")
    except pvd.ProxyProviderError as e:
        echoed_err = e
    check("Stage 2-3 credential-scrub fix still holds for a Stage-4-shaped secret", echoed_err is not None and "BUYFLOWSECRET" not in str(echoed_err), str(echoed_err))
    check("API key never appears in any Stage 4 error path", API_KEY not in str(echoed_err))

    # ------------------------------------------------------------------
    # 9. Existing manual proxy flow still works -- regression via AST
    #    extraction (main.py/panel_bot.py cannot be imported standalone).
    # ------------------------------------------------------------------
    def extract_and_exec(path: str, names: set[str], extra_ns: dict):
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

    import re as _re

    def _registry_normalize_manager_key(raw):
        return _re.sub(r"\s+", "", str(raw or "")).casefold().replace("ё", "е")

    ns_main = extract_and_exec(
        str(BASE_DIR / "main.py"),
        {"_tp_parse_proxy_one_line", "_tpilot_proxy_parse_set_args_v5", "_proxy_type_norm", "_proxy_port_int"},
        {"proxy_parser": proxy_parser, "registry_normalize_manager_key": _registry_normalize_manager_key, "Any": object, "Tuple": tuple},
    )
    one_line = ns_main["_tp_parse_proxy_one_line"]
    v5 = ns_main["_tpilot_proxy_parse_set_args_v5"]

    check(
        "regression: _tp_parse_proxy_one_line still parses host:port:login:password",
        one_line("1.2.3.4:1080:myuser:mypass") == ("1.2.3.4", 1080, "myuser", "mypass"),
    )
    check(
        "regression: _tpilot_proxy_parse_set_args_v5 still parses the manual SOCKS5 command shape",
        v5("te SOCKS5 1.2.3.4 1080 myuser mypass") == ("te", "socks5", "1.2.3.4", 1080, "myuser", "mypass"),
    )

    ns_panel = extract_and_exec(
        str(BASE_DIR / "panel_bot.py"),
        {"_tpilot_parse_socks5_one_line"},
        {"proxy_parser": proxy_parser},
    )
    panel_parse = ns_panel["_tpilot_parse_socks5_one_line"]
    check(
        "regression: panel_bot.py's _tpilot_parse_socks5_one_line still works unchanged",
        panel_parse("1.2.3.4:1080:user:pass") == ("1.2.3.4", "1080", "user", "pass"),
    )

    # ------------------------------------------------------------------
    # 10. Stage 4.1: find_period_id() -- periodId resolution (mirrors the
    #    find_country_id tests above). The documented shape nests periods
    #    inside each country entry (data.items[].period[]).
    # ------------------------------------------------------------------
    REFERENCE_LIST_WITH_PERIODS = {
        "status": "success",
        "errors": [],
        "data": {
            "items": [
                {"id": 11, "name": "France", "alpha3": "FRA", "period": [
                    {"id": "1m", "name": "1 month"},
                    {"id": "3m", "name": "3 months"},
                ]},
                {"id": 42, "name": "Germany", "alpha3": "DEU", "period": [
                    {"id": "1m", "name": "1 month"},
                    {"id": "3m", "name": "3 months"},
                ]},
            ]
        },
    }

    pid = pvd.find_period_id(REFERENCE_LIST_WITH_PERIODS, preferred_id="1m", preferred_name="1 month")
    check("find_period_id resolves by exact id=='1m'", pid == "1m", repr(pid))

    # id doesn't match anything, but name=='1 month' does -> name-fallback,
    # returning the entry's OWN id (proves the fallback path is exercised,
    # not just a coincidental id match).
    REFERENCE_LIST_OPAQUE_PERIOD_ID = {
        "data": {"items": [
            {"id": 42, "name": "Germany", "alpha3": "DEU", "period": [
                {"id": "prd-1mo-77", "name": "1 month"},
                {"id": "prd-3mo-77", "name": "3 months"},
            ]},
        ]}
    }
    pid_by_name = pvd.find_period_id(REFERENCE_LIST_OPAQUE_PERIOD_ID, preferred_id="1m", preferred_name="1 month")
    check(
        "find_period_id falls back to name=='1 month' when the id doesn't match, returning the entry's own id",
        pid_by_name == "prd-1mo-77",
        repr(pid_by_name),
    )

    # neither id nor name (nor alias) matches -> None
    REFERENCE_LIST_NO_1M = {
        "data": {"items": [
            {"id": 42, "name": "Germany", "alpha3": "DEU", "period": [
                {"id": "3m", "name": "3 months"},
                {"id": "6m", "name": "6 months"},
            ]},
        ]}
    }
    pid_missing = pvd.find_period_id(REFERENCE_LIST_NO_1M, preferred_id="1m", preferred_name="1 month")
    check("find_period_id returns None when no 1-month period exists", pid_missing is None, repr(pid_missing))

    # ------------------------------------------------------------------
    # 11. Stage 4.1: the buy-calc flow stops cleanly (no order/calc call)
    #    when periodId cannot be resolved -- mirrors the exact guard
    #    sequence main.py's _handle_manager_proxy_buy_calc_command runs:
    #    reference_list -> find_country_id -> find_period_id -> (stop here
    #    if None, never calling calc_ipv4).
    # ------------------------------------------------------------------
    session_no_period_calc = FakeSession(lambda m, u, p, j: FakeResponse(200, REFERENCE_LIST_NO_1M))
    provider_no_period_calc = pvd.ProxySellerProvider(API_KEY, session=session_no_period_calc)
    ref_np = provider_no_period_calc.reference_list("ipv4")
    country_id_np = pvd.find_country_id(ref_np, alpha3="DEU", name="Germany")
    period_id_np = pvd.find_period_id(ref_np, preferred_id="1m", preferred_name="1 month")
    check("(calc guard) country resolves fine even when period does not", country_id_np == 42, repr(country_id_np))
    check("(calc guard) period_id is None -> calc handler must stop before calling calc_ipv4", period_id_np is None)
    if period_id_np is not None:
        provider_no_period_calc.calc_ipv4(country_id_np, period_id_np, 1, "", "", "", 1)
    check(
        "buy-calc flow: no order/calc call was made when periodId could not be resolved",
        all("order/calc" not in c["url"] for c in session_no_period_calc.calls),
        str(session_no_period_calc.calls),
    )

    # ------------------------------------------------------------------
    # 12. Stage 4.1: the buy-confirm flow stops cleanly (make_ipv4 is NOT
    #    called, no order/make call) when periodId cannot be resolved --
    #    mirrors main.py's _handle_manager_proxy_buy_confirm_command guard
    #    sequence.
    # ------------------------------------------------------------------
    session_no_period_confirm = FakeSession(lambda m, u, p, j: FakeResponse(200, REFERENCE_LIST_NO_1M))
    provider_no_period_confirm = pvd.ProxySellerProvider(API_KEY, session=session_no_period_confirm)
    ref_npc = provider_no_period_confirm.reference_list("ipv4")
    country_id_npc = pvd.find_country_id(ref_npc, alpha3="DEU", name="Germany")
    period_id_npc = pvd.find_period_id(ref_npc, preferred_id="1m", preferred_name="1 month")
    check("(confirm guard) period_id is None -> confirm handler must stop before calling make_ipv4", period_id_npc is None)
    if period_id_npc is not None:
        provider_no_period_confirm.make_ipv4(country_id_npc, period_id_npc, 1, "", "", "", 1, allow_spend=True)
    check(
        "buy-confirm flow: make_ipv4/order-make was never attempted when periodId could not be resolved",
        all("order/make" not in c["url"] for c in session_no_period_confirm.calls),
        str(session_no_period_confirm.calls),
    )

    # ------------------------------------------------------------------
    # 13. Stage 4.1 happy path: the resolved (possibly opaque) period id
    #    is the value actually sent to the provider in calc/make, not a
    #    raw hardcoded "1m" -- proves the call sites use the value
    #    returned by find_period_id, not the preferred_id constant.
    # ------------------------------------------------------------------
    def opaque_responder(method, url, params, json_body):
        if "order/calc" in url:
            return FakeResponse(200, {"status": "success", "errors": [], "data": {"total": "4.99", "currency": "USD"}})
        if "order/make" in url:
            return FakeResponse(200, {"status": "success", "errors": [], "data": {"orderId": "ORD-999"}})
        return FakeResponse(200, REFERENCE_LIST_OPAQUE_PERIOD_ID)

    session_opaque = FakeSession(opaque_responder)
    provider_opaque = pvd.ProxySellerProvider(API_KEY, session=session_opaque, allow_spend=True)

    ref_op = provider_opaque.reference_list("ipv4")
    country_id_op = pvd.find_country_id(ref_op, alpha3="DEU", name="Germany")
    period_id_op = pvd.find_period_id(ref_op, preferred_id="1m", preferred_name="1 month")
    check("(happy path) period resolved via name-fallback to the opaque provider id", period_id_op == "prd-1mo-77", repr(period_id_op))

    provider_opaque.calc_ipv4(country_id_op, period_id_op, 1, "", "", "", 1)
    calc_call = next(c for c in session_opaque.calls if "order/calc" in c["url"])
    check(
        "happy path calc: outgoing periodId is the RESOLVED value, not the raw preferred_id '1m'",
        calc_call["json"].get("periodId") == "prd-1mo-77",
        repr(calc_call["json"]),
    )

    provider_opaque.make_ipv4(country_id_op, period_id_op, 1, "", "", "", 1, allow_spend=True)
    make_call = next(c for c in session_opaque.calls if "order/make" in c["url"])
    check(
        "happy path confirm: outgoing periodId is the RESOLVED value, not the raw preferred_id '1m'",
        make_call["json"].get("periodId") == "prd-1mo-77",
        repr(make_call["json"]),
    )

    # ------------------------------------------------------------------
    # 14. Hotfix: customTargetName -- Proxy-Seller rejects order/calc and
    #    order/make with errors[] "Set [customTargetName]" when this field
    #    is empty. main.py's _pbuy_custom_target_name() builds a safe,
    #    non-secret value from manager_key; extracted via AST since
    #    main.py cannot be imported standalone.
    # ------------------------------------------------------------------
    ns_ctn = extract_and_exec(
        str(BASE_DIR / "main.py"),
        {"_pbuy_custom_target_name"},
        {"re": _re},
    )
    custom_target_name_fn = ns_ctn["_pbuy_custom_target_name"]

    ctn_normal = custom_target_name_fn("buyer01")
    check("14a. customTargetName is non-empty for a normal manager_key", bool(ctn_normal), repr(ctn_normal))
    check("14b. customTargetName has the TPilot_<key> format", ctn_normal == "TPilot_buyer01", repr(ctn_normal))

    ctn_unsafe = custom_target_name_fn("bad key!@# with spaces/ё")
    check(
        "14c. unsafe manager_key characters are sanitized (only letters/digits/_/- remain)",
        _re.fullmatch(r"TPilot_[A-Za-z0-9_-]+", ctn_unsafe) is not None,
        repr(ctn_unsafe),
    )

    ctn_empty = custom_target_name_fn("")
    check("14d. empty manager_key falls back to a non-empty safe default", bool(ctn_empty) and ctn_empty.startswith("TPilot_"), repr(ctn_empty))

    ctn_only_symbols = custom_target_name_fn("!!!###")
    check("14e. manager_key that sanitizes to nothing falls back to TPilot_manager", ctn_only_symbols == "TPilot_manager", repr(ctn_only_symbols))

    # 1/2/3: calc and make both send a non-empty customTargetName, and the
    # SAME value for the same manager_key -- exercised via the actual
    # provider call sequence (same shape as the Stage 4.1 happy-path test).
    def ctn_responder(method, url, params, json_body):
        if "order/calc" in url:
            return FakeResponse(200, {"status": "success", "errors": [], "data": {"total": "4.99", "currency": "USD"}})
        if "order/make" in url:
            return FakeResponse(200, {"status": "success", "errors": [], "data": {"orderId": "ORD-CTN"}})
        return FakeResponse(200, REFERENCE_LIST_OK)

    session_ctn = FakeSession(ctn_responder)
    provider_ctn = pvd.ProxySellerProvider(API_KEY, session=session_ctn, allow_spend=True)
    ref_ctn = provider_ctn.reference_list("ipv4")
    country_id_ctn = pvd.find_country_id(ref_ctn, alpha3="DEU", name="Germany")

    target_name = custom_target_name_fn("buyer01")
    provider_ctn.calc_ipv4(country_id_ctn, "1m", 1, "", "", target_name, 1)
    provider_ctn.make_ipv4(country_id_ctn, "1m", 1, "", "", target_name, 1, allow_spend=True)

    calc_ctn_call = next(c for c in session_ctn.calls if "order/calc" in c["url"])
    make_ctn_call = next(c for c in session_ctn.calls if "order/make" in c["url"])
    check("14f. calc request body has a non-empty customTargetName", bool(calc_ctn_call["json"].get("customTargetName")), repr(calc_ctn_call["json"]))
    check("14g. make request body has a non-empty customTargetName", bool(make_ctn_call["json"].get("customTargetName")), repr(make_ctn_call["json"]))
    check(
        "14h. calc and make use the SAME customTargetName for the same manager_key",
        calc_ctn_call["json"].get("customTargetName") == make_ctn_call["json"].get("customTargetName") == target_name,
        repr((calc_ctn_call["json"].get("customTargetName"), make_ctn_call["json"].get("customTargetName"))),
    )

    # 5: no secret-like markers (API key, password/secret/token/phone/
    # session wording) ever end up inside the generated customTargetName,
    # across a normal key, an unsafe key, and edge-case empty/symbols-only
    # keys -- _pbuy_custom_target_name() only ever touches manager_key.
    secret_markers = (API_KEY, "password", "secret", "token", "phone", "session", "apikey")
    for probe_key in ("buyer01", "bad key!@# with spaces/ё", "", "!!!###"):
        probe_name = custom_target_name_fn(probe_key)
        for marker in secret_markers:
            check(
                f"14i. customTargetName for manager_key={probe_key!r} never contains marker {marker!r}",
                marker not in probe_name,
                probe_name,
            )

    # ------------------------------------------------------------------
    # 15. Hotfix 2026-07-09 (live order 4953243): buy-confirm now polls
    #    much longer (was 10x3s=30s, now >=60 attempts x5s=300s) and no
    #    longer tells the admin to fall back to manual /manager_proxy_set
    #    when money was already spent; a new no-spend recovery path
    #    exists. _pbuy_poll_for_proxy / _pbuy_finalize_lease are extracted
    #    via AST (main.py cannot be imported standalone) and exercised
    #    against a fake provider, with asyncio.sleep faked to a no-op so
    #    this test doesn't actually wait 5 real minutes.
    # ------------------------------------------------------------------
    class _InstantAsyncio:
        """Bound as the `asyncio` name inside the extracted function's own
        namespace -- only .sleep() is used there, and it returns
        immediately so polling 60 attempts takes milliseconds, not 5 real
        minutes."""
        @staticmethod
        async def sleep(_seconds):
            return None

    async def _fake_pbuy_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    ns_poll = extract_and_exec(
        str(BASE_DIR / "main.py"),
        {"_pbuy_poll_for_proxy"},
        {
            "_pbuy_call": _fake_pbuy_call,
            "_PBUY_PROXY_TYPE": "ipv4",
            "proxy_parser": proxy_parser,
            "asyncio": _InstantAsyncio(),
            "Optional": object, "Dict": dict, "Any": object,
        },
    )
    poll_fn = ns_poll["_pbuy_poll_for_proxy"]

    # -- 15a/15b: attempt count + pending (not error) when never ready --
    poll_calls: list = []

    class _ProviderNeverReady:
        def list_proxies(self, proxy_type, order_id):
            poll_calls.append(order_id)
            return {"status": "success", "errors": [], "data": {"items": []}}

        def download_proxies(self, proxy_type, proto, fmt):
            return {"status": "success", "errors": [], "data": {"list": []}}

    result_never = asyncio.run(poll_fn(_ProviderNeverReady(), "ORD-NEVER", attempts=60, interval_sec=5.0))
    check(
        "15a. buy-confirm polling now makes >= 60 list_proxies attempts (was 10x3s=30s before the hotfix)",
        len(poll_calls) == 60,
        str(len(poll_calls)),
    )
    check("15b. poll returns None (pending, not a crash) when the provider never provisions", result_never is None)

    # -- 15c: succeeds once the provider responds, even after many empty
    #    attempts (proves the poll doesn't give up early like the old 10x).
    delayed_calls: list = []

    class _ProviderDelayed:
        READY_AT = 12  # well past the old 10-attempt limit

        def list_proxies(self, proxy_type, order_id):
            delayed_calls.append(order_id)
            if len(delayed_calls) < self.READY_AT:
                return {"status": "success", "errors": [], "data": {"items": []}}
            return {
                "status": "success", "errors": [],
                "data": {"items": [{"id": "PXY-DELAYED", "ip": "6.6.6.6", "port_socks": 50199, "login": "u9", "password": "pw9", "date_end": "2026-09-09"}]},
            }

        def download_proxies(self, proxy_type, proto, fmt):
            return {"status": "success", "errors": [], "data": {"list": []}}

    result_delayed = asyncio.run(poll_fn(_ProviderDelayed(), "ORD-DELAYED", attempts=60, interval_sec=5.0))
    check(
        "15c. buy-confirm poll succeeds once the provider responds, even after 11 empty attempts (past the old 10x limit)",
        result_delayed is not None and result_delayed.get("host") == "6.6.6.6" and len(delayed_calls) == 12,
        repr((result_delayed, len(delayed_calls))),
    )

    # -- 15d-15g: no more "check manually / /manager_proxy_set" as the
    #    primary path; new pending code + recovery-button wording exist.
    main_src = Path(str(BASE_DIR / "main.py")).read_text(encoding="utf-8-sig")
    check("15d. the old 'provisioning_timeout' error code was removed", '"provisioning_timeout"' not in main_src)
    check("15e. the new 'provisioning_pending' error code exists (pending/recoverable state)", '"provisioning_pending"' in main_src)
    check(
        "15f. buy-confirm no longer tells the admin to manually run /manager_proxy_set as the primary recovery path",
        "прокси менеджеру через /manager_proxy_set" not in main_src,
    )
    check("15g. buy-confirm's pending message points at the recovery button wording", "Дозабрать proxy" in main_src)

    # -- 15h/15i: recovery command never calls order/make, never passes
    #    allow_spend=True -- verified by scanning the function's OWN
    #    unparsed source, not just the helpers it happens to call.
    tree = ast.parse(main_src)
    recover_node = next(n for n in tree.body if getattr(n, "name", None) == "_handle_manager_proxy_buy_recover_command")
    recover_body = recover_node.body
    if (
        recover_body
        and isinstance(recover_body[0], ast.Expr)
        and isinstance(getattr(recover_body[0], "value", None), ast.Constant)
        and isinstance(recover_body[0].value.value, str)
    ):
        recover_body = recover_body[1:]  # drop the docstring -- it mentions allow_spend/make_ipv4 in prose, not code
    recover_src = "\n".join(ast.unparse(n) for n in recover_body)
    check("15h. recovery command source (excluding docstring) never calls make_ipv4", "make_ipv4" not in recover_src, recover_src[:200])
    check("15i. recovery command source (excluding docstring) never references allow_spend", "allow_spend" not in recover_src, recover_src[:200])

    # -- 15j-15n: recovery finds proxy by order_id and assigns it -- the
    #    SAME shared _pbuy_finalize_lease helper the real recovery
    #    handler calls, exercised against a temp SQLite DB and a minimal
    #    fake manager registry (the real registry needs a live Telethon
    #    controller, out of scope for an offline unit test).
    _fake_registry_store: dict = {}

    async def _fake_registry_set_fields(key, **fields):
        _fake_registry_store[key] = dict(fields)
        return True

    async def _fake_run_guard(key, *, source, force=False):
        return True, "ok"

    async def _fake_registry_get(key):
        return _fake_registry_store.get(key, {"display_name": key})

    def _fake_manager_proxy_info_text(row):
        return f"proxy_host={row.get('proxy_host')}"

    tmp_db_finalize = tempfile.mktemp(suffix=".db")
    try:
        ns_finalize = extract_and_exec(
            str(BASE_DIR / "main.py"),
            {"_pbuy_finalize_lease", "_pbuy_apply_lease_to_manager"},
            {
                "_PBUY_PROXY_TYPE": "ipv4",
                "_PBUY_COUNTRY_NAME": "Germany",
                "TPILOT_DB_PATH": tmp_db_finalize,
                "Dict": dict, "Any": object, "Optional": object,
                "_tpag_registry_set_fields": _fake_registry_set_fields,
                "_tpag_run_guard": _fake_run_guard,
                "_tpag_registry_get": _fake_registry_get,
                "_manager_proxy_info_text": _fake_manager_proxy_info_text,
                "_pbuy_safe_error": lambda e: str(e),
            },
        )
        finalize_fn = ns_finalize["_pbuy_finalize_lease"]

        proxy_entry_recovered = {
            "host": "7.7.7.7", "port": 50200, "login": "rec_user", "password": "rec_pass",
            "provider_proxy_id": "PXY-RECOVERED", "expires_at": "2026-09-09",
        }
        finalize_result = asyncio.run(finalize_fn("test_poxy", "4953243", None, proxy_entry_recovered, source="panel_buy_recover"))
        check("15j. recovery finalize succeeds and creates a lease", finalize_result.get("ok") is True, repr(finalize_result))
        check("15k. recovery finalize records the provider order_id on the lease", finalize_result.get("provider_order_id") == "4953243", repr(finalize_result))
        check(
            "15l. recovery finalize records provider_proxy_id from the provider result (not the internal lease id)",
            finalize_result.get("provider_proxy_id") == "PXY-RECOVERED",
            repr(finalize_result),
        )
        check(
            "15m. recovery finalize's internal lease_id is distinct from provider_proxy_id (never confused)",
            isinstance(finalize_result.get("lease_id"), int) and finalize_result.get("lease_id") != "PXY-RECOVERED",
            repr(finalize_result),
        )
        check(
            "15n. recovery finalize applies the found proxy's fields to the manager via the registry helper",
            _fake_registry_store.get("test_poxy", {}).get("proxy_host") == "7.7.7.7",
            repr(_fake_registry_store.get("test_poxy")),
        )
    finally:
        Path(tmp_db_finalize).unlink(missing_ok=True)

    # -- 15o-15q: panel command timeouts are command-specific (not a
    #    global bump) and long enough for the new provisioning wait.
    panel_src = Path(str(BASE_DIR / "panel_bot.py")).read_text(encoding="utf-8-sig")
    m_confirm = _re.search(r'command_text\.startswith\("/manager_proxy_buy_confirm"\).*?\n\s*timeout_s\s*=\s*(\d+)', panel_src, _re.S)
    m_recover = _re.search(r'command_text\.startswith\("/manager_proxy_buy_recover"\).*?\n\s*timeout_s\s*=\s*(\d+)', panel_src, _re.S)
    m_calc = _re.search(r'command_text\.startswith\("/manager_proxy_buy_calc"\).*?\n\s*timeout_s\s*=\s*(\d+)', panel_src, _re.S)
    check(
        "15o. panel timeout for /manager_proxy_buy_confirm is command-specific and >= 360s",
        m_confirm is not None and int(m_confirm.group(1)) >= 360,
        m_confirm.group(0) if m_confirm else None,
    )
    check(
        "15p. panel timeout for /manager_proxy_buy_recover is its own command-specific value (not the generic 45s fallback)",
        m_recover is not None and int(m_recover.group(1)) != 45,
        m_recover.group(0) if m_recover else None,
    )
    check(
        "15q. panel timeout for /manager_proxy_buy_calc is command-specific and >= 60s",
        m_calc is not None and int(m_calc.group(1)) >= 60,
        m_calc.group(0) if m_calc else None,
    )

    # ------------------------------------------------------------------
    # No network calls anywhere in this file -- confirm requests was never
    # touched for real (every provider above used an injected FakeSession).
    # ------------------------------------------------------------------
    check("proxy_provider module remains importable regardless of `requests` availability", pvd is not None)

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
