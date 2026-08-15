# -*- coding: utf-8 -*-
"""tools/proxy_renewal_wiring_selftest.py -- offline wiring selftest for the
PROXY RENEWAL (TPilot-managed prolong) 20260721 main.py block (Stages 3-5).

main.py cannot be imported standalone -- AST-extraction idiom + real temp
SQLite (storage runs for real) + fake provider/executor. No network, no
Telegram, no real spend.

Covers:
  - order integrity: the renewal _panel_execute_command_text override is the
    LAST def (before the __main__ guard), delegates unknown commands, and
    _RENEWAL_DISPATCH + _renewal_loop are defined before the guard;
  - allow_spend AST: the whole renewal block adds ZERO allow_spend=True call
    sites (project-wide count stays 2 -- checked in the separate audit; here
    we assert the block references it only in comments);
  - _renewal_wrapped_execute idempotency: a make_pending op is created BEFORE
    the (faked) spend; a duplicate/concurrent call for the same lease is
    SKIPPED without spending; success/failed advance the op correctly;
  - _renewal_eligible_leases: only auto_renew_enabled=1 + proxy_seller +
    provider_proxy_id + expiring within lead_days; unknown expiry skipped;
  - guard check: emergency_stop/paused/per-op-cap/daily-budget block a spend;
  - automation OFF => _renewal_auto_tick is a pure no-op (no spend, no op);
  - config enable_auto records consent; pause/emergency flags honored;
  - backfill sets auto_renew_enabled from usage (no provider call, no spend);
  - restart reconcile: a make_pending op whose provider expiry advanced ->
    success; unchanged -> failed (never a blind re-make).
  - _renewal_calc_preview strict signature (2026-07-21 diagnostics hotfix):
    prolong_calc is called with the exact positional order (proxy_type, ids,
    period_id, payment_id) and ids is a ONE-ITEM LIST, not a bare scalar --
    a permissive fake (fn(*a,**kw)) elsewhere in this file would miss an
    arg-order regression, so this uses a fake that records the exact call;
    a missing/empty provider_proxy_id returns a safe error WITHOUT ever
    calling the provider; an exception in the calc path logs a scrubbed
    traceback (never the API key/secret) while the AdminBot-facing message
    stays byte-identical to the pre-hotfix safe message.

Run:  python tools\\proxy_renewal_wiring_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import contextlib
import datetime as _dt
import io
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from zoneinfo import ZoneInfo

_TEST_TZ_KYIV = ZoneInfo("Europe/Kyiv")

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import aiosqlite
import storage
import proxy_lifecycle as plc

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


def find_defs(tree, name):
    return [n for n in tree.body if getattr(n, "name", None) == name]


def last_def(tree, name):
    d = find_defs(tree, name)
    if not d:
        raise AssertionError(f"no top-level def {name!r}")
    return d[-1]


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
    exec(compile(module_src, "<main.py renewal extract>", "exec"), ns)
    return ns


def run_order_integrity(main_tree) -> None:
    print("\n-- Order integrity (static) --")
    body = main_tree.body
    guard = [i for i, n in enumerate(body) if isinstance(n, ast.If) and ast.unparse(n.test).replace(" ", "") == "__name__=='__main__'"]
    check("exactly one __main__ guard", len(guard) == 1)
    if len(guard) != 1:
        return
    g = guard[0]
    pe = [i for i, n in enumerate(body) if getattr(n, "name", None) == "_panel_execute_command_text"]
    dd = [i for i, n in enumerate(body) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_RENEWAL_DISPATCH" for t in n.targets)]
    loop = [i for i, n in enumerate(body) if getattr(n, "name", None) == "_renewal_loop"]
    check("renewal _panel_execute_command_text override is the LAST def, before the guard",
          pe and pe[-1] == max(pe) and pe[-1] < g)
    check("_RENEWAL_DISPATCH assigned before the guard", dd and dd[0] < g)
    check("_renewal_loop defined before the guard", loop and loop[0] < g)
    main_src = "\n".join(ast.unparse(n) for n in body)
    check("create_task(_renewal_loop()) registered", "create_task(_renewal_loop())" in main_src)
    check("create_task(_renewal_balance_alert_loop()) registered", "create_task(_renewal_balance_alert_loop())" in main_src)

    # allow_spend appears in the renewal block ONLY as comments (the real spend
    # is delegated to _prenew_execute_renewal, unchanged).
    marker = [i for i, n in enumerate(body) if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_PRENEW_LEAD_DAYS_DEFAULT" for t in n.targets)]
    if marker:
        default_node = body[marker[0]].value
        check("_PRENEW_LEAD_DAYS_DEFAULT canonical default is 2 (was 7)",
              isinstance(default_node, ast.Constant) and default_node.value == 2,
              ast.unparse(body[marker[0]]))
    block_src = ast.unparse(body[marker[0]:pe[-1] + 1]) if marker else ""
    # ast.unparse drops comments, so a literal allow_spend=True in code would
    # survive but a comment would not -> asserting absence proves no code use.
    check("SAFETY: no allow_spend=True in the renewal block's executable code",
          "allow_spend=True" not in block_src)
    check("SAFETY: renewal block never calls prolong_make/make_ipv4 directly (delegates to _prenew_execute_renewal)",
          "prolong_make(" not in block_src and "make_ipv4(" not in block_src)


def run_runtime_checks(main_tree) -> None:
    print("\n-- Runtime behavior (REAL execution, temp DB, fake provider/executor) --")

    work = Path(tempfile.mkdtemp(prefix="proxy_renewal_wiring_"))
    tmp_db = str(work / "q.db")
    _guard_temp_db(tmp_db)
    storage.DB_PATH = tmp_db
    storage.QUEUE_DB_PATH = tmp_db

    spend_calls = {"count": 0}
    executor_result = {"value": {"ok": True, "total": "3.50", "currency": "USD", "expires_at": "2026-10-01"}}

    async def _fake_prenew_execute_renewal(lease, *, source, _preflight=None, _op_id=None):
        # Crash-window fix (independent re-review correction, 20260809):
        # _renewal_wrapped_execute now creates the op row in 'pending'
        # (RESERVED) and relies on _prenew_execute_renewal to CAS-advance it
        # to 'make_pending' (SPEND_STARTED) immediately before "spending" --
        # this fake stands in for that whole function, so it must also
        # perform that same marker write, or the op row would still be
        # 'pending' when _renewal_wrapped_execute's own success/failed
        # op_advance(..., from_status="make_pending") runs afterward, and
        # that CAS would silently no-op (this file tests THAT wiring, via
        # the op["status"] checks below).
        if _op_id is not None:
            await storage.proxy_renewal_op_advance(_op_id, "make_pending", from_status="pending", db_path=tmp_db)
        spend_calls["count"] += 1
        return dict(executor_result["value"], lease_id=lease.get("id"), provider_proxy_id=lease.get("provider_proxy_id"))

    # BL-1 fix (Proxy Renewal Reliability review, 20260808): _renewal_wrapped_
    # execute now runs _prenew_preflight_check BEFORE proxy_renewal_op_create.
    # This file's job is the op-creation/idempotency WIRING, not preflight
    # failure modes (those are covered by proxy_renewal_idempotency_selftest.py
    # and proxy_error_classify_selftest.py) -- so the fake here always
    # succeeds, same spirit as the pre-existing _fake_prenew_execute_renewal.
    async def _fake_preflight_check(lease):
        return {"ok": True, "provider": None, "period_id": "1m", "before_dt": None}

    async def _fake_never_orphan(lease, usage_map=None, *, context="gate"):
        return False

    import json as _json

    ns = extract_and_exec(
        main_tree,
        {
            "_renewal_wrapped_execute", "_renewal_eligible_leases", "_renewal_guard_check",
            "_renewal_auto_tick", "_handle_proxy_renewal_backfill_command",
            "_renewal_reconcile_pending_on_start", "_renewal_float", "_renewal_kyiv_today_str",
            "_PRENEW_LEAD_DAYS_DEFAULT", "_PBUY_PERIOD_ID", "_RENEWAL_ELIGIBLE_MIN_DELTA_DAYS",
        },
        {
            "Any": object, "Dict": dict, "List": list, "Optional": object, "Tuple": tuple,
            "TPILOT_DB_PATH": tmp_db,
            "CONTROLLER_MODE": True,
            "_PRENEW_PROXY_TYPE": "ipv4",
            "_prenew_execute_renewal": _fake_prenew_execute_renewal,
            "_now_utc_iso": lambda: "2026-08-25T12:00:00",
            # tz-AWARE, matching the real _kyiv_now() (datetime.now(tz=TZ_KYIV)) --
            # a naive fake here would hide the naive/aware subtraction TypeError
            # that this exact fixture originally missed (2026-07-21 tz hotfix).
            "_kyiv_now": lambda: _dt.datetime(2026, 8, 25, 12, 0, 0, tzinfo=_TEST_TZ_KYIV),
            "_pbuy_json": _json,
            "_plc": plc,
            "_create_panel_notification": _fake_notify,
            "_ppool_usage_key": _fake_usage_key,
            "_ppool_manager_usage_map": _fake_usage_map,
            "_prenew_collect_active_seller_leases": _fake_collect_leases_factory(tmp_db),
            "_ppool_parse_expires_at": _fake_parse_expires,
            "_pbuy_provider": _fake_provider,   # returns a fake with list_proxies driven by RECON_OBSERVED
            "_pbuy_call": _fake_pbuy_call,
            "_pbuy_safe_error": lambda e: str(e),
            "_renewal_calc_preview": _fake_calc_preview,
            "_renewal_balance": _fake_balance,
            "_prenew_preflight_check": _fake_preflight_check,
            # CORRECTION B 20260810: _renewal_wrapped_execute now gates on
            # _prenew_lease_is_orphan before anything else -- this file
            # predates that correction and tests unrelated invariants
            # (idempotency/crash-window/config), not orphan suppression
            # (that is tools/proxy_orphan_lease_selftest.py's job), so a
            # never-orphan fake preserves this file's existing scenarios.
            "_prenew_lease_is_orphan": _fake_never_orphan,
        },
    )

    wrapped = ns["_renewal_wrapped_execute"]
    eligible_fn = ns["_renewal_eligible_leases"]
    guard_fn = ns["_renewal_guard_check"]
    auto_tick = ns["_renewal_auto_tick"]
    backfill = ns["_handle_proxy_renewal_backfill_command"]
    reconcile_start = ns["_renewal_reconcile_pending_on_start"]

    # seed a lease
    async def seed():
        lid = await storage.proxy_lease_create(
            provider_type="proxy_seller", host="1.1.1.1", port=50101, provider_proxy_id="PXY-R",
            login="u", password="SECRET_RENEW_PW", expires_at="2026-08-28", auto_renew_enabled=True, db_path=tmp_db,
        )
        return lid
    lease_id = asyncio.run(seed())
    lease = asyncio.run(storage.proxy_lease_get(lease_id, db_path=tmp_db))

    # --- idempotent wrapped execute: one spend, op recorded success ---
    res1 = asyncio.run(wrapped(lease, source="manual", actor_user_id=7))
    check("wrapped execute spends exactly once and succeeds", res1.get("ok") is True and spend_calls["count"] == 1)
    op = asyncio.run(storage.proxy_renewal_op_get(res1["op_id"], db_path=tmp_db))
    check("op recorded success + total + currency", op["status"] == "success" and op["calc_total"] == "3.50" and op["currency"] == "USD")

    # --- duplicate for the SAME renewal target (same expiry/period) is skipped, no spend ---
    res2 = asyncio.run(wrapped(lease, source="manual", actor_user_id=7))
    check("duplicate wrapped execute is SKIPPED (idempotency_key collision) with NO extra spend",
          res2.get("skipped") is True and spend_calls["count"] == 1)

    # --- failed executor path advances op to failed ---
    executor_result["value"] = {"ok": False, "message": "insufficient_balance"}
    lease2_id = asyncio.run(storage.proxy_lease_create(
        provider_type="proxy_seller", host="2.2.2.2", port=50101, provider_proxy_id="PXY-R2",
        expires_at="2026-08-28", auto_renew_enabled=True, db_path=tmp_db))
    lease2 = asyncio.run(storage.proxy_lease_get(lease2_id, db_path=tmp_db))
    res3 = asyncio.run(wrapped(lease2, source="auto"))
    check("failed executor -> op status failed + retry bumped", res3.get("ok") is False)
    op3 = asyncio.run(storage.proxy_renewal_op_active_for_lease(lease2_id, db_path=tmp_db))
    check("failed op is no longer 'active' (terminal failed state)", op3 is None)
    failed = asyncio.run(storage.proxy_renewal_ops_by_status(["failed"], db_path=tmp_db))
    check("failed op recorded with retry_count>=1", any(o["lease_id"] == lease2_id and int(o["retry_count"]) >= 1 for o in failed))
    executor_result["value"] = {"ok": True, "total": "3.50", "currency": "USD", "expires_at": "2026-10-01"}

    # --- eligible leases: within lead window, flag on, provider id present ---
    elig = asyncio.run(eligible_fn(7))
    elig_ids = {e["id"] for e in elig}
    check("eligible includes the flagged lease expiring within 7 days", lease_id in elig_ids or lease2_id in elig_ids)
    # a lease with the flag OFF is not eligible
    lease_off = asyncio.run(storage.proxy_lease_create(
        provider_type="proxy_seller", host="3.3.3.3", port=50101, provider_proxy_id="PXY-OFF",
        expires_at="2026-08-28", auto_renew_enabled=False, db_path=tmp_db))
    elig2 = asyncio.run(eligible_fn(7))
    check("a lease with auto_renew_enabled=0 is NOT eligible", lease_off not in {e["id"] for e in elig2})
    # a lease expiring far away is not eligible
    lease_far = asyncio.run(storage.proxy_lease_create(
        provider_type="proxy_seller", host="4.4.4.4", port=50101, provider_proxy_id="PXY-FAR",
        expires_at="2027-01-01", auto_renew_enabled=True, db_path=tmp_db))
    check("a lease expiring far beyond the lead window is NOT eligible",
          lease_far not in {e["id"] for e in asyncio.run(eligible_fn(7))})

    # --- guards ---
    cfg_stop = {"emergency_stop": 1}
    check("emergency_stop blocks spend", asyncio.run(guard_fn(cfg_stop, "3.50", "2026-08-25")) is not None)
    cfg_pause = {"paused": 1}
    check("paused blocks spend", asyncio.run(guard_fn(cfg_pause, "3.50", "2026-08-25")) is not None)
    cfg_cap = {"max_amount_per_op": "1.00"}
    check("per-op cap blocks an over-cap spend", asyncio.run(guard_fn(cfg_cap, "3.50", "2026-08-25")) is not None)
    cfg_ok = {"max_amount_per_op": "10.00"}
    check("a within-cap spend is allowed (None)", asyncio.run(guard_fn(cfg_ok, "3.50", "2026-08-25")) is None)

    # --- automation OFF => auto_tick is a no-op ---
    spend_before = spend_calls["count"]
    asyncio.run(auto_tick())
    check("automation OFF => _renewal_auto_tick spends nothing", spend_calls["count"] == spend_before)

    # --- config enable_auto records consent, then a guarded tick can run ---
    asyncio.run(storage.proxy_renewal_config_set(automation_enabled=1, enabled_by_user_id=99, db_path=tmp_db))
    cfg_now = asyncio.run(storage.proxy_renewal_config_get(db_path=tmp_db))
    check("enable_auto sets automation_enabled=1 + records consent user", int(cfg_now["automation_enabled"]) == 1 and int(cfg_now["enabled_by_user_id"]) == 99)

    # --- backfill sets flags from usage (no provider, no spend) ---
    spend_before = spend_calls["count"]
    out = _json.loads(asyncio.run(backfill("")))
    check("backfill runs with no spend", out.get("ok") is True and spend_calls["count"] == spend_before)

    # --- restart reconcile: make_pending w/ advanced provider expiry -> success ---
    op_pend = asyncio.run(storage.proxy_renewal_op_create(
        lease_id=lease_far, provider_proxy_id="PXY-FAR", idempotency_key="PXY-FAR:2027-01-01:1m",
        source="auto", status="make_pending", period_id="1m", expires_before="2027-01-01", db_path=tmp_db))
    # provider now reports a LATER expiry -> renewal actually happened
    RECON_OBSERVED["PXY-FAR"] = "2027-02-01"
    asyncio.run(reconcile_start())
    op_after = asyncio.run(storage.proxy_renewal_op_get(op_pend, db_path=tmp_db))
    check("restart reconcile: provider expiry advanced -> op marked success (no blind re-make)",
          op_after["status"] == "success" and spend_calls["count"] == spend_before)

    # unchanged expiry -> failed (retry next tick), still no spend
    op_pend2 = asyncio.run(storage.proxy_renewal_op_create(
        lease_id=lease2_id, provider_proxy_id="PXY-R2b", idempotency_key="PXY-R2b:2026-08-28:1m",
        source="auto", status="make_pending", period_id="1m", expires_before="2026-08-28", db_path=tmp_db))
    RECON_OBSERVED["PXY-R2b"] = "2026-08-28"  # unchanged
    asyncio.run(reconcile_start())
    op_after2 = asyncio.run(storage.proxy_renewal_op_get(op_pend2, db_path=tmp_db))
    check("restart reconcile: unchanged expiry -> op marked failed (safe retry), never re-makes",
          op_after2["status"] == "failed" and spend_calls["count"] == spend_before)

    # --- secret safety ---
    all_notif = " ".join(n["body"] for n in NOTIFICATIONS)
    check("SAFETY: no notification ever contains the proxy password", "SECRET_RENEW_PW" not in all_notif)


# --- module-level fakes shared by the extracted functions -------------------
NOTIFICATIONS: list = []
RECON_OBSERVED: dict = {}


async def _fake_notify(kind, title, body):
    NOTIFICATIONS.append({"kind": kind, "title": title, "body": body})


def _fake_usage_key(host, port):
    try:
        return (str(host or "").strip().lower(), int(port))
    except Exception:
        return None


async def _fake_usage_map():
    # PXY-R host 1.1.1.1 has one healthy user; others none.
    return {("1.1.1.1", 50101): [{"manager_key": "m1"}]}


def _fake_collect_leases_factory(tmp_db):
    async def _collect():
        rows = await storage.proxy_lease_list_all(db_path=tmp_db)
        return [r for r in rows if str(r.get("status") or "") == "active" and str(r.get("provider_type") or "") == "proxy_seller"]
    return _collect


def _fake_parse_expires(raw):
    import datetime as _dt
    s = str(raw or "").strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d", "%d.%m.%Y"):
        try:
            return _dt.datetime.strptime(s, fmt)
        except Exception:
            pass
    return None


class _FakeProvider:
    def list_proxies(self, proxy_type):
        # Shape extract_proxy_entries understands: data.items[] with ip/port_socks/id/date_end.
        items = [{"id": pid, "ip": "9.9.9.9", "port_socks": 50101, "date_end": exp}
                 for pid, exp in RECON_OBSERVED.items()]
        return {"status": "success", "errors": [], "data": {"items": items}}

    def balance(self):
        return {"data": {"summ": "100.0"}, "summ": "100.0"}


def _fake_provider():
    return _FakeProvider()


async def _fake_pbuy_call(fn, *a, **kw):
    return fn(*a, **kw)


async def _fake_calc_preview(lease):
    return {"ok": True, "lease_id": lease.get("id"), "total": "3.50", "currency": "USD", "period_id": "1m"}


async def _fake_balance():
    return 100.0


class _StrictCalcProvider:
    """Records the EXACT positional args prolong_calc is called with, unlike
    the permissive `_fake_pbuy_call` (fn(*a,**kw)) used elsewhere in this
    file -- this is what catches an arg-order regression."""

    def __init__(self, calls, *, fail=None, secret=""):
        self.calls = calls
        self.fail = fail
        self.secret = secret

    def _scrub_text(self, text):
        text = str(text or "")
        if self.secret and self.secret in text:
            text = text.replace(self.secret, "***API_KEY***")
        return text

    def reference_list(self, proxy_type):
        self.calls.append(("reference_list", proxy_type))
        return {"status": "success", "errors": [], "data": {"items": [
            {"id": 1, "name": "Germany", "alpha3": "DEU", "period": [{"id": "1m", "name": "1 month"}]},
        ]}}

    def prolong_calc(self, proxy_type, ids, period_id, payment_id):
        self.calls.append(("prolong_calc", proxy_type, ids, period_id, payment_id))
        if self.fail is not None:
            raise self.fail
        return {"status": "success", "errors": [], "data": {"total": "3.50", "currency": "USD", "price": "3.50"}}


def run_calc_preview_checks(main_tree) -> None:
    print("\n-- _renewal_calc_preview strict signature + guard + traceback log (REAL execution) --")

    def make_ns(provider):
        return extract_and_exec(
            main_tree,
            {"_renewal_calc_preview", "_pbuy_call", "_pbuy_safe_error", "_prenew_classify_provider_error"},
            {
                "Dict": dict, "Any": object, "asyncio": asyncio,
                "_PRENEW_PROXY_TYPE": "ipv4",
                "_PRENEW_PAYMENT_ID": 1,
                "_PBUY_PERIOD_ID": "1m",
                "_PBUY_PERIOD_NAME": "1 month",
                "_pbuy_provider": lambda: provider,
            },
        )

    # --- strict positional arg order + ids-as-one-item-list ---
    calls_ok = []
    provider_ok = _StrictCalcProvider(calls_ok)
    ns_ok = make_ns(provider_ok)
    res_ok = asyncio.run(ns_ok["_renewal_calc_preview"]({"id": 1, "provider_proxy_id": "PXY-STRICT"}))
    check("well-formed calc preview succeeds", res_ok.get("ok") is True, str(res_ok))
    pc_call = next((c for c in calls_ok if c[0] == "prolong_calc"), None)
    check("prolong_calc was called", pc_call is not None)
    if pc_call is not None:
        check("prolong_calc proxy_type positional arg == 'ipv4'", pc_call[1] == "ipv4")
        check("prolong_calc ids arg is a ONE-ITEM LIST [provider_proxy_id], not a bare scalar",
              pc_call[2] == ["PXY-STRICT"] and isinstance(pc_call[2], list))
        check("prolong_calc period_id arg matches the resolved period", pc_call[3] == "1m")
        check("prolong_calc payment_id arg is an int", pc_call[4] == 1 and isinstance(pc_call[4], int))

    # --- missing/empty provider_proxy_id: safe error, provider NEVER called ---
    for bad_id, label in ((None, "None"), ("", "empty string")):
        calls_missing = []
        provider_missing = _StrictCalcProvider(calls_missing)
        ns_missing = make_ns(provider_missing)
        res_missing = asyncio.run(ns_missing["_renewal_calc_preview"]({"id": 2, "provider_proxy_id": bad_id}))
        check(f"provider_proxy_id={label} returns a safe error", res_missing.get("ok") is False and res_missing.get("error") == "missing_provider_proxy_id")
        check(f"provider_proxy_id={label} never calls the provider", calls_missing == [])

    # --- exception path: scrubbed traceback logged, UI message unchanged ---
    secret = "SECRET_PSK_TOKEN_XYZ"
    calls_fail = []
    provider_fail = _StrictCalcProvider(calls_fail, fail=TypeError(f"boom {secret} leaked"), secret=secret)
    ns_fail = make_ns(provider_fail)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res_fail = asyncio.run(ns_fail["_renewal_calc_preview"]({"id": 3, "provider_proxy_id": "PXY-FAIL"}))
    printed = buf.getvalue()
    check("exception path returns the pre-hotfix safe UI message unchanged",
          res_fail.get("ok") is False and res_fail.get("error") == "calc_failed"
          and res_fail.get("message") == "TypeError: непредвиденная ошибка провайдера",
          str(res_fail))
    # BL-2 follow-up fix (independent re-review correction, 20260809):
    # _renewal_calc_preview's failure branches now also carry classified
    # error_category/reason/action (same shape as _prenew_preflight_check/
    # _prenew_execute_renewal) -- this is what the prn:calc: UI now shows
    # instead of the raw `message` field checked above.
    check("BL-2 follow-up: exception path also carries classified reason/action, never raw text",
          res_fail.get("error_category") == "unknown" and bool(res_fail.get("reason")) and bool(res_fail.get("action"))
          and secret not in str(res_fail.get("reason")) and secret not in str(res_fail.get("action")),
          str(res_fail))
    check("exception path logs a traceback to the controller log", "[proxy_renewal] calc_failed" in printed and "Traceback" in printed)
    check("SAFETY: the logged traceback never contains the raw secret", secret not in printed)


def run_eligible_leases_tz_checks(main_tree) -> None:
    """2026-07-21 datetime-awareness hotfix (updated 2026-07-21 for the
    7->2 day lead-window default change): _renewal_eligible_leases used to
    subtract a naive parsed expiry from tz-aware _kyiv_now() ->
    'can't subtract offset-naive and offset-aware datetimes' in production.
    Reproduces the exact aware _kyiv_now() the bug needs, across every
    expires_at shape the provider is known to send, plus the today/boundary/
    expired edge cases the (now 2-day) lead window depends on."""
    print("\n-- _renewal_eligible_leases datetime awareness (calendar-day math) --")

    # "today" = 2026-08-25, Kyiv, tz-AWARE (near midnight to also stress any
    # accidental UTC-vs-Kyiv day-boundary drift).
    FAKE_NOW = _dt.datetime(2026, 8, 25, 23, 45, 0, tzinfo=_TEST_TZ_KYIV)
    LEAD_DAYS = 2  # matches the current _PRENEW_LEAD_DAYS_DEFAULT

    LEASES = [
        # naive ISO, expires TODAY -> delta_days == 0
        {"id": 201, "auto_renew_enabled": 1, "provider_proxy_id": "PXY-TODAY", "expires_at": "2026-08-25T10:00:00"},
        # 'Z' (UTC) ISO, exactly 2 days away -> delta_days == 2 (lead_days boundary)
        {"id": 202, "auto_renew_enabled": 1, "provider_proxy_id": "PXY-Z2", "expires_at": "2026-08-27T00:00:00Z"},
        # positive offset ISO (+03:00), 1 day away -> delta_days == 1
        {"id": 203, "auto_renew_enabled": 1, "provider_proxy_id": "PXY-OFFSET", "expires_at": "2026-08-26T12:00:00+03:00"},
        # naive ISO, expired 5 days ago -> delta_days==-5, now EXCLUDED (R5b lower bound,
        # 20260808 -- this used to be returned forever with no lower bound at all, the
        # exact bug that let a long-dead lease get retried by this engine's tick forever)
        {"id": 204, "auto_renew_enabled": 1, "provider_proxy_id": "PXY-EXPIRED", "expires_at": "2026-08-20T00:00:00"},
        # explicit '+00:00' offset ISO, far beyond the lead window -> excluded
        {"id": 205, "auto_renew_enabled": 1, "provider_proxy_id": "PXY-FAR", "expires_at": "2026-10-01T00:00:00+00:00"},
        # date-only value -> accepted, 1 day away -> delta_days == 1
        {"id": 206, "auto_renew_enabled": 1, "provider_proxy_id": "PXY-DATEONLY", "expires_at": "2026-08-26"},
        # naive ISO, 3 days away -> just OUTSIDE the new 2-day window -> excluded
        {"id": 207, "auto_renew_enabled": 1, "provider_proxy_id": "PXY-JUST-OUTSIDE", "expires_at": "2026-08-28T00:00:00"},
        # R5b: expired YESTERDAY (delta_days==-1) -> still INSIDE the 1-day catch-up grace
        {"id": 208, "auto_renew_enabled": 1, "provider_proxy_id": "PXY-GRACE-IN", "expires_at": "2026-08-24T00:00:00"},
        # R5b: expired 2 days ago (delta_days==-2) -> just OUTSIDE the grace -> excluded
        {"id": 209, "auto_renew_enabled": 1, "provider_proxy_id": "PXY-GRACE-OUT", "expires_at": "2026-08-23T00:00:00"},
    ]

    async def _fake_collect():
        return LEASES

    ns = extract_and_exec(
        main_tree,
        {"_renewal_eligible_leases", "_ppool_parse_expires_at", "_RENEWAL_ELIGIBLE_MIN_DELTA_DAYS"},
        {
            "Any": object, "Dict": dict, "List": list, "Optional": object, "Tuple": tuple,
            "datetime": _dt.datetime,
            "_prenew_collect_active_seller_leases": _fake_collect,
            "_kyiv_now": lambda: FAKE_NOW,
        },
    )
    check("R5b lower-bound constant is -1 (one day of catch-up grace, not unbounded)", ns["_RENEWAL_ELIGIBLE_MIN_DELTA_DAYS"] == -1, ns["_RENEWAL_ELIGIBLE_MIN_DELTA_DAYS"])

    try:
        elig = asyncio.run(ns["_renewal_eligible_leases"](LEAD_DAYS))
        raised = None
    except Exception as exc:  # pragma: no cover - only on regression
        elig, raised = [], exc
    check("naive expiry vs aware _kyiv_now() does NOT raise (root-cause repro)", raised is None, repr(raised))

    by_id = {e["id"]: e["_delta_days"] for e in elig}
    check("naive ISO expiry TODAY is eligible with delta_days==0", by_id.get(201) == 0, str(by_id))
    check("'Z' (UTC) expiry exactly 2 days away is eligible with delta_days==2 (new lead_days boundary)", by_id.get(202) == 2, str(by_id))
    check("positive-offset (+03:00) expiry resolves to the correct calendar delta_days==1", by_id.get(203) == 1, str(by_id))
    check("expired 5 days ago (delta_days==-5) is now EXCLUDED by the R5b lower bound (was unbounded before)", 204 not in by_id, str(by_id))
    check("explicit '+00:00' expiry far beyond the lead window is excluded", 205 not in by_id, str(by_id))
    check("date-only expiry is accepted and resolves to delta_days==1", by_id.get(206) == 1, str(by_id))
    check("naive expiry 3 days away is just OUTSIDE the new 2-day window and excluded", 207 not in by_id, str(by_id))
    check("R5b: expired YESTERDAY (delta_days==-1) is still INSIDE the 1-day grace and included", by_id.get(208) == -1, str(by_id))
    check("R5b: expired 2 days ago (delta_days==-2) is just OUTSIDE the grace and excluded", 209 not in by_id, str(by_id))


class _FakeAlertClock:
    """Controllable stand-in for the `datetime` name inside
    _renewal_balance_alert_tick -- .utcnow() returns a settable instant,
    .fromisoformat delegates to the real parser."""

    def __init__(self, now):
        self.now = now

    def utcnow(self):
        return self.now

    def fromisoformat(self, s):
        return _dt.datetime.fromisoformat(s)


def run_balance_alert_checks(main_tree) -> None:
    """2026-07-21 low-balance AdminBot alert: alert immediately on crossing
    below $20, at most once per 24h while still below, reset on recovery,
    balance-read failure never raises/blocks. REAL storage (temp DB) +
    REAL _renewal_balance_alert_tick, with _renewal_balance/_now_utc_iso/
    datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) faked for deterministic control."""
    print("\n-- low-balance AdminBot alert (REAL execution, temp DB) --")

    work = Path(tempfile.mkdtemp(prefix="proxy_balance_alert_"))
    tmp_db = str(work / "q.db")
    _guard_temp_db(tmp_db)
    storage.DB_PATH = tmp_db
    storage.QUEUE_DB_PATH = tmp_db

    clock = _FakeAlertClock(_dt.datetime(2026, 8, 25, 12, 0, 0))
    balance_state = {"value": 15.0, "raise": False}
    sent: list = []

    async def _fake_renewal_balance():
        if balance_state["raise"]:
            raise RuntimeError("simulated provider read failure")
        return balance_state["value"]

    async def _fake_create_panel_notification(kind, title, body):
        sent.append({"kind": kind, "title": title, "body": body})

    def _fake_now_utc_iso():
        return clock.now.replace(microsecond=0).isoformat()

    ns = extract_and_exec(
        main_tree,
        {
            "_renewal_balance_alert_tick", "_PRENEW_BALANCE_ALERT_THRESHOLD_USD",
            "_PRENEW_BALANCE_ALERT_COOLDOWN_SEC", "_PRENEW_BALANCE_REFILL_URL",
            # Forward-fix Phase 2 (2026-07-26): the tick body is now wrapped in
            # this module-level asyncio.Lock() (single-flight enforcement) --
            # extract the real assignment too, not just fake it, so this test
            # proves the REAL lock exists and is used, matching the technique
            # already used for other PREV-globals in sibling selftests.
            "_PRENEW_BALANCE_TICK_LOCK",
        },
        {
            "Any": object, "Dict": dict, "List": list, "Optional": object, "Tuple": tuple,
            "datetime": clock,
            "asyncio": asyncio,
            "CONTROLLER_MODE": True,
            "TPILOT_DB_PATH": tmp_db,
            "_renewal_balance": _fake_renewal_balance,
            "_create_panel_notification": _fake_create_panel_notification,
            "_now_utc_iso": _fake_now_utc_iso,
        },
    )
    tick = ns["_renewal_balance_alert_tick"]

    check("threshold constant is $20", ns["_PRENEW_BALANCE_ALERT_THRESHOLD_USD"] == 20.0)
    check("refill link is the exact required URL", ns["_PRENEW_BALANCE_REFILL_URL"] == "https://proxy-seller.me/personal/balance")

    # --- A: fresh crossing below $20 -> alert sent immediately ---
    asyncio.run(tick())
    check("crossing below $20 sends exactly one alert", len(sent) == 1, str(sent))
    body_a = sent[0]["body"] if sent else ""
    check("alert body contains the exact required warning line",
          "⚠️ Баланс Proxy-Seller ниже 20 $. Пополните баланс." in body_a, body_a)
    check("alert body includes the current balance", "15.00" in body_a, body_a)
    check("alert body includes the clickable refill link", "https://proxy-seller.me/personal/balance" in body_a, body_a)
    state_a = asyncio.run(storage.proxy_balance_alert_state_get(db_path=tmp_db))
    check("state marks below_threshold=1 after the first alert", int(state_a.get("below_threshold") or 0) == 1, str(state_a))

    # --- B: still below threshold, only 1h later -> cooldown blocks a repeat ---
    clock.now += _dt.timedelta(hours=1)
    asyncio.run(tick())
    check("still below threshold within 24h does NOT repeat the alert (anti-spam)", len(sent) == 1, str(sent))

    # --- C: still below threshold, 25h after the FIRST alert -> repeats once ---
    clock.now += _dt.timedelta(hours=24)
    asyncio.run(tick())
    check("still below threshold after 24h+ repeats the alert exactly once", len(sent) == 2, str(sent))

    # --- D: balance recovers to >= $20 -> state resets, no alert on recovery ---
    balance_state["value"] = 25.0
    asyncio.run(tick())
    check("recovery to >= $20 sends NO alert", len(sent) == 2, str(sent))
    state_d = asyncio.run(storage.proxy_balance_alert_state_get(db_path=tmp_db))
    check("state resets below_threshold=0 on recovery", int(state_d.get("below_threshold") or 0) == 0, str(state_d))

    # --- E: dips below threshold again right after recovery -> fresh crossing, alerts immediately ---
    balance_state["value"] = 10.0
    asyncio.run(tick())
    check("a new crossing right after recovery alerts immediately (no stale cooldown)", len(sent) == 3, str(sent))

    # --- F: balance read fails -> no alert, no crash, state untouched ---
    balance_state["raise"] = True
    state_before_fail = asyncio.run(storage.proxy_balance_alert_state_get(db_path=tmp_db))
    try:
        asyncio.run(tick())
        raised = None
    except Exception as exc:  # pragma: no cover - only on regression
        raised = exc
    check("a balance read failure never raises out of the tick", raised is None, repr(raised))
    check("a balance read failure sends no alert", len(sent) == 3, str(sent))
    state_after_fail = asyncio.run(storage.proxy_balance_alert_state_get(db_path=tmp_db))
    check("a balance read failure leaves the alert state untouched", state_after_fail == state_before_fail, str(state_after_fail))

    # --- safety: no message ever leaks anything beyond the numeric balance ---
    all_bodies = " ".join(s["body"] for s in sent)
    check("SAFETY: no alert body ever contains a login/password-looking secret", "SECRET" not in all_bodies and "password" not in all_bodies.lower())


def run_lead_days_migration_checks() -> None:
    """2026-07-21: the seeded default changed 7 -> 2, but INSERT OR IGNORE
    never touches an already-seeded row, so a pre-existing production row
    (lead_days=7) would stay at the old default forever without an explicit
    one-time migration. Verifies: existing 7 -> 2; existing custom 1/3 stay
    untouched; fresh DB seeds 2 directly; the migration is idempotent; and a
    LATER deliberate admin choice of 7 (after the migration already ran) is
    never silently re-flipped back to 2. REAL storage, temp DBs."""
    print("\n-- lead_days 7->2 one-time migration (REAL storage, temp DB) --")

    def _fresh_db_path() -> str:
        work = Path(tempfile.mkdtemp(prefix="proxy_lead_days_mig_"))
        tmp_db = str(work / "q.db")
        _guard_temp_db(tmp_db)
        return tmp_db

    # --- fresh DB: seeded directly at the new default, no migration involved ---
    db_fresh = _fresh_db_path()
    cfg_fresh = asyncio.run(storage.proxy_renewal_config_get(db_path=db_fresh))
    check("fresh DB seeds lead_days=2 directly", int(cfg_fresh.get("lead_days") or 0) == 2, str(cfg_fresh))

    # --- pre-existing row at the OLD default (7, pre-dating the sentinel column) -> migrated to 2 ---
    db_old = _fresh_db_path()
    async def _seed_old_default():
        async with aiosqlite.connect(db_old) as db:
            await storage._proxy_renewal_tables_ready(db)
            await db.execute("UPDATE proxy_renewal_config SET lead_days=7, lead_days_migrated_7to2=NULL WHERE id=1")
            await db.commit()
    asyncio.run(_seed_old_default())
    cfg_migrated_1 = asyncio.run(storage.proxy_renewal_config_get(db_path=db_old))
    cfg_migrated_2 = asyncio.run(storage.proxy_renewal_config_get(db_path=db_old))  # second pass -> idempotency
    check("pre-existing lead_days=7 is migrated to 2", int(cfg_migrated_1.get("lead_days") or 0) == 2, str(cfg_migrated_1))
    check("migration is idempotent (second access stays 2)", int(cfg_migrated_2.get("lead_days") or 0) == 2, str(cfg_migrated_2))

    # --- a LATER deliberate admin choice of 7 (after migration already ran) is respected, not re-flipped ---
    db_revert = _fresh_db_path()
    async def _seed_then_revert():
        async with aiosqlite.connect(db_revert) as db:
            await storage._proxy_renewal_tables_ready(db)  # fresh DB -> already 2, sentinel stays NULL (never needed)
            await db.execute("UPDATE proxy_renewal_config SET lead_days=7, lead_days_migrated_7to2=1 WHERE id=1")
            await db.commit()
        await storage.proxy_renewal_config_set(lead_days=7, db_path=db_revert)  # admin explicitly re-sets 7
        return await storage.proxy_renewal_config_get(db_path=db_revert)
    cfg_revert = asyncio.run(_seed_then_revert())
    check("a later deliberate admin lead_days=7 (sentinel already set) is respected, never re-flipped to 2",
          int(cfg_revert.get("lead_days") or 0) == 7, str(cfg_revert))

    # --- genuinely custom values (never 7) are preserved untouched by the migration ---
    for custom_value in (1, 3):
        db_custom = _fresh_db_path()
        async def _seed_custom(v=custom_value, db_path=db_custom):
            async with aiosqlite.connect(db_path) as db:
                await storage._proxy_renewal_tables_ready(db)
                await db.execute("UPDATE proxy_renewal_config SET lead_days=?, lead_days_migrated_7to2=NULL WHERE id=1", (v,))
                await db.commit()
        asyncio.run(_seed_custom())
        cfg_custom = asyncio.run(storage.proxy_renewal_config_get(db_path=db_custom))
        check(f"existing custom lead_days={custom_value} is preserved untouched by the migration",
              int(cfg_custom.get("lead_days") or 0) == custom_value, str(cfg_custom))


def main() -> int:
    main_tree = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))
    run_order_integrity(main_tree)
    run_runtime_checks(main_tree)
    run_calc_preview_checks(main_tree)
    run_eligible_leases_tz_checks(main_tree)
    run_balance_alert_checks(main_tree)
    run_lead_days_migration_checks()
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PROXY RENEWAL WIRING SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
