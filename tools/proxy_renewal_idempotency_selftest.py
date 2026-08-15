# -*- coding: utf-8 -*-
"""tools/proxy_renewal_idempotency_selftest.py -- end-to-end selftest for
the BL-1 fix (Proxy Renewal Reliability independent review, 20260808):
proxy_renewal_ops.idempotency_key must only ever be consumed AFTER the safe
PRE-SPEND preflight (provider configured / reference_list / period lookup)
has succeeded. Before this fix, _renewal_wrapped_execute created the op row
(and therefore burned the idempotency_key forever, since it is a project-
wide UNIQUE index, not scoped to 'active' status) BEFORE running any of
those checks -- a transient/config failure that never even reached
prolong_make permanently blocked every future retry for that lease's
current pre-renewal expiry.

The fix moves _prenew_preflight_check (provider configured / reference_list
/ period lookup -- all safe reads, no spend) to run BEFORE
proxy_renewal_op_create inside _renewal_wrapped_execute. A preflight
failure now returns immediately with no op row created at all, so the SAME
lease/expiry can be retried indefinitely. Only once preflight succeeds does
op_create run, immediately followed by the real prolong_make attempt --
from that point on the idempotency_key is permanently consumed for this
(provider_proxy_id, pre-renewal expiry date, period) triple, so ANY outcome
reached after that point (success, provider error, or an unverified/
transport-ambiguous result) can never silently re-spend for the same
renewal cycle.

main.py cannot be imported standalone -- AST-extraction idiom (same as the
other proxy_*_selftest.py files here) + a real temp SQLite DB (storage runs
for real: proxy_renewal_op_create/_renewal_wrapped_execute do real CAS
inserts against it) + a fake ProxySellerProvider the test fully controls.
No network, no Telegram, no real provider writes.

Required cases (task spec Fix 2 + MUST-PASS PROOFS A-C):
  A. reference_list_failed BEFORE spend: first wrapped_execute call spends
     0 times (structured failure, no op row created); after the provider
     recovers, the very next call for the SAME lease/expiry spends exactly
     once.
  B. period_not_found BEFORE spend: same shape, period lookup recovers.
  C. provider not configured BEFORE spend: same shape, provider becomes
     configured.
  D. prolong_make WAS actually called and the outcome is unverified
     (transport-ambiguous / not confirmed by the post-spend refresh) ->
     every subsequent wrapped_execute call for the SAME pre-renewal expiry
     spends ZERO additional times (idempotency_key already consumed),
     never re-attempts prolong_make.
  E. same starting point as D, but modelling a process restart between the
     unverified attempt and the retry (a fresh _renewal_wrapped_execute
     call against the same durable on-disk DB state, no in-memory state
     carried over) -- spend count stays at 1.
  F. two concurrent callers for the SAME lease/expiry -> total spend <= 1
     (the CAS unique-index race is decided by SQLite, not by this test).

Run:  python tools\\proxy_renewal_idempotency_selftest.py
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
    exec(compile(module_src, "<main.py renewal-idempotency extract>", "exec"), ns)
    return ns


class FakeProvider:
    """In-memory Proxy-Seller stand-in, fully test-controlled. Never
    touches the network. Tracks prolong_make call count so tests can
    assert an exact spend count."""

    def __init__(self):
        self.prolong_make_calls = 0
        self.reference_list_raises: Exception | None = None
        self.period_available = True
        self.make_raises: Exception | None = None
        # Default make_response carries an ADVANCED date_end so a
        # successful call confirms directly (no refresh round-trip needed)
        # unless a specific case overrides it.
        self.make_response = {"data": {"total": "1.8", "currency": "USD", "date_end": "2026-09-08"}}
        self.list_proxies_calls = 0
        self._list_responses: list[list[dict]] = []

    def reference_list(self, proxy_type):
        if self.reference_list_raises is not None:
            raise self.reference_list_raises
        if self.period_available:
            return {"data": [{"id": "1m", "name": "1 month"}]}
        return {"data": []}

    def prolong_make(self, proxy_type, ids, period_id, payment_id, *, allow_spend=False):
        assert allow_spend is True, "prolong_make must only ever be called with allow_spend=True"
        self.prolong_make_calls += 1
        if self.make_raises is not None:
            raise self.make_raises
        return dict(self.make_response)

    def list_proxies(self, proxy_type, **kwargs):
        self.list_proxies_calls += 1
        if self._list_responses:
            return {"data": self._list_responses.pop(0)}
        return {"data": []}

    def queue_list_response(self, entries: list[dict]) -> None:
        self._list_responses.append(entries)


class _TransportError(Exception):
    kind = "transport"


async def _make_lease(tmp_db: str, **overrides) -> int:
    kwargs = dict(
        provider_type="proxy_seller", host="1.2.3.4", port=50101, manager_key="mgr_a",
        provider_proxy_id="PXY-idem-1", proxy_type="ipv4", scheme="socks5",
        login="u1", password="pw1", status="active", db_path=tmp_db,
        expires_at="2026-08-08",
    )
    kwargs.update(overrides)
    return await storage.proxy_lease_create(**kwargs)


async def main_async() -> int:
    main_tree = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))

    fake_provider_holder: dict = {}

    def _fake_pbuy_provider():
        return fake_provider_holder.get("instance")

    async def _fake_pbuy_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    async def _fake_never_orphan(lease, usage_map=None, *, context="gate"):
        return False

    ns = extract_and_exec(
        main_tree,
        {
            "_prenew_preflight_check",
            "_prenew_execute_renewal",
            "_renewal_wrapped_execute",
            "_prenew_verify_renewal_via_refresh",
            "_prenew_find_provider_entry",
            "_prenew_classify_provider_error",
            "_ppool_parse_expires_at",
            "_pbuy_safe_error",
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
            # CORRECTION B 20260810: see the identical comment in
            # proxy_renewal_wiring_selftest.py -- this file predates the
            # orphan gate and tests unrelated idempotency invariants.
            "_prenew_lease_is_orphan": _fake_never_orphan,
        },
    )
    wrapped_execute = ns["_renewal_wrapped_execute"]

    with tempfile.TemporaryDirectory(prefix="tpilot_renewal_idem_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        _guard_temp_db(tmp_db)
        ns["TPILOT_DB_PATH"] = tmp_db

        # ------------------------------------------------------------------
        # CASE A: reference_list_failed BEFORE spend -- freely retryable.
        # ------------------------------------------------------------------
        lease_a_id = await _make_lease(tmp_db, provider_proxy_id="PXY-A", host="10.0.0.1", port=51001, expires_at="2026-08-08")
        lease_a = await storage.proxy_lease_get(lease_a_id, db_path=tmp_db)

        provider_a = FakeProvider()
        provider_a.reference_list_raises = _TransportError("simulated network timeout")
        fake_provider_holder["instance"] = provider_a

        res_a1 = await wrapped_execute(lease_a, source="autorenew")
        check("A1. first call (reference_list fails) is ok=False", res_a1.get("ok") is False, res_a1)
        check("A2. first call error is reference_list_failed", res_a1.get("error") == "reference_list_failed", res_a1)
        check("A3. first call never spent (prolong_make not called)", provider_a.prolong_make_calls == 0, provider_a.prolong_make_calls)
        check("A4. first call is NOT marked skipped (it's a real precheck failure, not a dedup hit)", not res_a1.get("skipped"), res_a1)
        op_a1 = await storage.proxy_renewal_op_active_for_lease(lease_a_id, db_path=tmp_db)
        check("A5. NO op row was created for the pre-spend failure (idempotency_key never burned)", op_a1 is None, op_a1)

        # recovery: provider fixed -> the SAME lease/expiry retries and spends exactly once
        provider_a.reference_list_raises = None
        res_a2 = await wrapped_execute(lease_a, source="autorenew")
        check("A6. retry after recovery succeeds", res_a2.get("ok") is True, res_a2)
        check("A7. retry after recovery spends EXACTLY ONCE", provider_a.prolong_make_calls == 1, provider_a.prolong_make_calls)

        # a THIRD call for the same (now-renewed) lease is a normal same-cycle
        # dedup hit (expires_at changed after A6, so this only proves the op
        # bookkeeping is consistent, not a new spend).
        res_a3 = await wrapped_execute(lease_a, source="autorenew")
        check("A8. a third call after a real success does not spend again", provider_a.prolong_make_calls == 1, provider_a.prolong_make_calls)

        # ------------------------------------------------------------------
        # CASE B: period_not_found BEFORE spend -- freely retryable.
        # ------------------------------------------------------------------
        lease_b_id = await _make_lease(tmp_db, provider_proxy_id="PXY-B", host="10.0.0.2", port=51002, expires_at="2026-08-08")
        lease_b = await storage.proxy_lease_get(lease_b_id, db_path=tmp_db)

        provider_b = FakeProvider()
        provider_b.period_available = False
        fake_provider_holder["instance"] = provider_b

        res_b1 = await wrapped_execute(lease_b, source="autorenew")
        check("B1. first call (period not found) is ok=False", res_b1.get("ok") is False, res_b1)
        check("B2. first call error is period_not_found", res_b1.get("error") == "period_not_found", res_b1)
        check("B3. first call never spent", provider_b.prolong_make_calls == 0, provider_b.prolong_make_calls)
        op_b1 = await storage.proxy_renewal_op_active_for_lease(lease_b_id, db_path=tmp_db)
        check("B4. NO op row was created for the pre-spend failure", op_b1 is None, op_b1)

        provider_b.period_available = True
        res_b2 = await wrapped_execute(lease_b, source="autorenew")
        check("B5. retry after the period appears succeeds", res_b2.get("ok") is True, res_b2)
        check("B6. retry after recovery spends EXACTLY ONCE", provider_b.prolong_make_calls == 1, provider_b.prolong_make_calls)

        # ------------------------------------------------------------------
        # CASE C: provider not configured BEFORE spend -- freely retryable.
        # ------------------------------------------------------------------
        lease_c_id = await _make_lease(tmp_db, provider_proxy_id="PXY-C", host="10.0.0.3", port=51003, expires_at="2026-08-08")
        lease_c = await storage.proxy_lease_get(lease_c_id, db_path=tmp_db)

        fake_provider_holder["instance"] = None  # not configured

        res_c1 = await wrapped_execute(lease_c, source="autorenew")
        check("C1. first call (provider not configured) is ok=False", res_c1.get("ok") is False, res_c1)
        check("C2. first call error is not_configured", res_c1.get("error") == "not_configured", res_c1)
        op_c1 = await storage.proxy_renewal_op_active_for_lease(lease_c_id, db_path=tmp_db)
        check("C3. NO op row was created for the pre-spend failure", op_c1 is None, op_c1)

        provider_c = FakeProvider()
        fake_provider_holder["instance"] = provider_c
        res_c2 = await wrapped_execute(lease_c, source="autorenew")
        check("C4. retry after the provider becomes configured succeeds", res_c2.get("ok") is True, res_c2)
        check("C5. retry after recovery spends EXACTLY ONCE", provider_c.prolong_make_calls == 1, provider_c.prolong_make_calls)

        # ------------------------------------------------------------------
        # CASE D: prolong_make WAS called, outcome unverified (transport-
        # ambiguous, never confirmed) -> every retry spends ZERO more times.
        # ------------------------------------------------------------------
        lease_d_id = await _make_lease(tmp_db, provider_proxy_id="PXY-D", host="10.0.0.4", port=51004, expires_at="2026-08-08")
        lease_d = await storage.proxy_lease_get(lease_d_id, db_path=tmp_db)

        provider_d = FakeProvider()
        provider_d.make_response = {"data": {"total": "1.8", "currency": "USD"}}  # no date_end -> forces refresh
        provider_d.queue_list_response([])  # refresh finds nothing -> stays unverified
        fake_provider_holder["instance"] = provider_d

        res_d1 = await wrapped_execute(lease_d, source="autorenew")
        check("D1. first call reaches the spend and comes back unverified", res_d1.get("ok") is False and res_d1.get("error") == "unverified", res_d1)
        check("D2. prolong_make WAS called exactly once", provider_d.prolong_make_calls == 1, provider_d.prolong_make_calls)
        op_d1 = await storage.proxy_renewal_op_active_for_lease(lease_d_id, db_path=tmp_db)
        check("D3. the op row is no longer 'active' (terminal, but its idempotency_key stays consumed forever)", op_d1 is None, op_d1)
        lease_d_after1 = await storage.proxy_lease_get(lease_d_id, db_path=tmp_db)
        check("D4. expires_at is left untouched after an unverified outcome", lease_d_after1.get("expires_at") == "2026-08-08", lease_d_after1)

        # retry #1 for the SAME (unchanged) pre-renewal expiry -> blocked by
        # the now-consumed idempotency_key, zero additional spend.
        provider_d.queue_list_response([{"id": "PXY-D", "ip": "10.0.0.4", "port_socks": 51004, "date_end": "2026-09-08", "order_id": "ORD-D"}])
        res_d2 = await wrapped_execute(lease_d, source="autorenew")
        check("D5. retry #1 for the SAME expiry is SKIPPED (idempotency_key collision)", res_d2.get("skipped") is True, res_d2)
        check("D6. retry #1 spends ZERO additional times", provider_d.prolong_make_calls == 1, provider_d.prolong_make_calls)

        # ------------------------------------------------------------------
        # CASE E: same starting point as D, but a FRESH wrapped_execute call
        # against the same durable DB state (models a process restart --
        # no in-process state survives, only what's on disk).
        # ------------------------------------------------------------------
        res_e = await wrapped_execute(lease_d, source="autorenew")  # a brand-new call, same durable DB
        check("E1. post-restart retry for the SAME expiry is still SKIPPED", res_e.get("skipped") is True, res_e)
        check("E2. post-restart retry spends ZERO additional times (total stays 1)", provider_d.prolong_make_calls == 1, provider_d.prolong_make_calls)

        # ------------------------------------------------------------------
        # CASE F: two concurrent callers for the SAME lease/expiry -> total
        # spend <= 1. The CAS unique-index race is decided by SQLite itself.
        # ------------------------------------------------------------------
        lease_f_id = await _make_lease(tmp_db, provider_proxy_id="PXY-F", host="10.0.0.5", port=51005, expires_at="2026-08-08")
        lease_f = await storage.proxy_lease_get(lease_f_id, db_path=tmp_db)

        provider_f = FakeProvider()
        fake_provider_holder["instance"] = provider_f

        results_f = await asyncio.gather(
            wrapped_execute(lease_f, source="manual"),
            wrapped_execute(lease_f, source="autorenew"),
        )
        ok_count = sum(1 for r in results_f if r.get("ok"))
        skipped_count = sum(1 for r in results_f if r.get("skipped"))
        check("F1. exactly one concurrent caller succeeds", ok_count == 1, results_f)
        check("F2. exactly one concurrent caller is skipped", skipped_count == 1, results_f)
        check("F3. total spend across both concurrent callers is exactly 1 (never 2)", provider_f.prolong_make_calls == 1, provider_f.prolong_make_calls)

    if FAILURES:
        print(f"\nFAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("\n" + "=" * 70)
    print("SELFTEST OK: all checks passed.")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
