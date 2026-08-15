# -*- coding: utf-8 -*-
"""tools/proxy_renew_verify_selftest.py -- offline selftest for the PROXY
RENEWAL RELIABILITY R2 verify-after-renew logic in main.py (20260808).

Covers _prenew_execute_renewal's post-spend confirmation: a prolong/make
response that doesn't raise is no longer treated as proof of success. If
the response itself doesn't carry a usable newer date_end, a bounded
READ-ONLY list_proxies refresh is used to confirm the provider's OWN state
actually advanced before expires_at is ever written. If it can't be
confirmed, the outcome is 'renew_unverified' and expires_at is left
untouched -- no guessing, no second spend for the same cycle.

main.py cannot be imported standalone -- AST-extraction idiom (same as the
other proxy_*_selftest.py files here) + a real temp SQLite DB (storage runs
for real) + a fake ProxySellerProvider. No network, no Telegram, no real
provider writes -- prolong_make is a Python fake that never touches the
network.

Required cases (task spec):
  1. make without date + refresh shows a LATER date => success, expires_at bumped.
  2. make without date + refresh shows the SAME date => unverified, expires_at untouched.
  3. a repeated attempt after unverified spends exactly once total
     (idempotency_key stays pinned to the unchanged pre-renewal expiry).
  4. provider_proxy_id changed after renewal => lease's identity resynced
     in place (no orphan duplicate row).
  5. provider takes >1 refresh attempt to show the new date (delayed
     consistency) => confirmed within the bounded attempt count, still
     exactly one prolong/make call.

Run:  python tools\\proxy_renew_verify_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import datetime
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


def find_defs(tree, name):
    return [n for n in tree.body if getattr(n, "name", None) == name]


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
    exec(compile(module_src, "<main.py renew-verify extract>", "exec"), ns)
    return ns


class FakeProvider:
    """In-memory Proxy-Seller stand-in. prolong_make NEVER touches the
    network -- it's a plain Python method the test controls. Tracks call
    counts so tests can assert 'spend exactly once'."""

    def __init__(self):
        self.prolong_make_calls = 0
        self.list_proxies_calls = 0
        # scripted list_proxies() responses, consumed in order
        self._list_responses: list[list[dict]] = []
        # what prolong_make's response should look like this call
        self.make_response = {"data": {"total": "1.8", "currency": "USD"}}  # no date_end -> forces refresh path
        self.make_raises: Exception | None = None

    def reference_list(self, proxy_type):
        return {"data": [{"id": "1m", "name": "1 month"}]}

    def prolong_make(self, proxy_type, ids, period_id, payment_id, *, allow_spend=False):
        assert allow_spend is True, "prolong_make must only ever be called with allow_spend=True"
        self.prolong_make_calls += 1
        if self.make_raises is not None:
            raise self.make_raises
        return dict(self.make_response)

    def list_proxies(self, proxy_type, **kwargs):
        self.list_proxies_calls += 1
        if self._list_responses:
            entries = self._list_responses.pop(0)
        elif self._list_responses == [] and hasattr(self, "_last_response"):
            entries = self._last_response
        else:
            entries = []
        self._last_response = entries
        return {"data": entries}

    def queue_list_response(self, entries: list[dict]) -> None:
        self._list_responses.append(entries)


def _entry(pid: str, host: str, port: int, expires_at: str) -> dict:
    return {"id": pid, "ip": host, "port_socks": port, "date_end": expires_at, "order_id": f"ORD-{pid}"}


async def _make_lease(tmp_db: str, **overrides) -> int:
    kwargs = dict(
        provider_type="proxy_seller", host="1.2.3.4", port=50101, manager_key="mgr_a",
        provider_proxy_id="PXY-1", proxy_type="ipv4", scheme="socks5",
        login="u1", password="pw1", status="active", db_path=tmp_db,
        expires_at="2026-08-08",
    )
    kwargs.update(overrides)
    return await storage.proxy_lease_create(**kwargs)


async def main_async() -> int:
    main_tree = ast.parse(MAIN_PY.read_text(encoding="utf-8-sig"))

    fake_provider_holder: dict = {}

    def _fake_pbuy_provider():
        return fake_provider_holder["instance"]

    async def _fake_pbuy_call(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    ns = extract_and_exec(
        main_tree,
        {
            "_prenew_preflight_check",
            "_prenew_execute_renewal",
            "_prenew_verify_renewal_via_refresh",
            "_prenew_find_provider_entry",
            "_prenew_classify_provider_error",
            "_ppool_parse_expires_at",
            "_pbuy_safe_error",
        },
        {
            "Any": object, "Dict": dict, "Optional": object, "Tuple": tuple, "List": list,
            "datetime": datetime, "asyncio": asyncio,
            "_pbuy_provider": _fake_pbuy_provider,
            "_pbuy_call": _fake_pbuy_call,
            "_PRENEW_PROXY_TYPE": "ipv4",
            "_PRENEW_PAYMENT_ID": 1,
            "_PBUY_PERIOD_ID": "1m",
            "_PBUY_PERIOD_NAME": "1 month",
            # fast-test overrides: NOT extracted from main.py (so they don't
            # get shadowed back to the real 60s/2-attempt production values)
            "_PRENEW_VERIFY_MAX_REFRESH_ATTEMPTS": 2,
            "_PRENEW_VERIFY_REFRESH_BACKOFF_SEC": 0.0,
        },
    )
    execute_renewal_fn = ns["_prenew_execute_renewal"]
    parse_fn = ns["_ppool_parse_expires_at"]

    with tempfile.TemporaryDirectory(prefix="tpilot_renew_verify_") as tmp:
        tmp_db = os.path.join(tmp, "test.db")
        _guard_temp_db(tmp_db)

        # ------------------------------------------------------------------
        # Case 1: make response has NO date_end, but a read-only refresh
        # shows the expiry genuinely advanced -> CONFIRMED success.
        # ------------------------------------------------------------------
        lease_id_1 = await _make_lease(tmp_db, provider_proxy_id="PXY-1", host="1.1.1.1", port=50101, expires_at="2026-08-08")
        lease_1 = await storage.proxy_lease_get(lease_id_1, db_path=tmp_db)

        provider1 = FakeProvider()
        provider1.make_response = {"data": {"total": "1.8", "currency": "USD"}}  # no date_end
        provider1.queue_list_response([_entry("PXY-1", "1.1.1.1", 50101, "2026-09-08")])  # advanced
        fake_provider_holder["instance"] = provider1
        ns["TPILOT_DB_PATH"] = tmp_db

        result1 = await execute_renewal_fn(lease_1, source="autorenew")
        check("1a. make w/o date + refresh advanced => ok=True", result1.get("ok") is True, repr(result1))
        check("1b. confirmed new expires_at reflects the REFRESH value, not a guess",
              result1.get("expires_at") == "2026-09-08", repr(result1))
        check("1c. exactly one prolong/make call (single spend)", provider1.prolong_make_calls == 1, str(provider1.prolong_make_calls))
        lease_1_after = await storage.proxy_lease_get(lease_id_1, db_path=tmp_db)
        check("1d. storage.expires_at was actually bumped", lease_1_after.get("expires_at") == "2026-09-08", repr(lease_1_after))

        # ------------------------------------------------------------------
        # Case 2: make response has NO date_end, refresh shows the SAME
        # (unchanged) date -> UNVERIFIED, expires_at left untouched.
        # ------------------------------------------------------------------
        lease_id_2 = await _make_lease(tmp_db, provider_proxy_id="PXY-2", host="2.2.2.2", port=50102, expires_at="2026-08-08")
        lease_2 = await storage.proxy_lease_get(lease_id_2, db_path=tmp_db)

        provider2 = FakeProvider()
        provider2.make_response = {"data": {"total": "1.8", "currency": "USD"}}
        provider2.queue_list_response([_entry("PXY-2", "2.2.2.2", 50102, "2026-08-08")])  # UNCHANGED
        provider2.queue_list_response([_entry("PXY-2", "2.2.2.2", 50102, "2026-08-08")])  # still unchanged on retry
        fake_provider_holder["instance"] = provider2

        result2 = await execute_renewal_fn(lease_2, source="autorenew")
        check("2a. make w/o date + refresh unchanged => ok=False, error=unverified", result2.get("ok") is False and result2.get("error") == "unverified", repr(result2))
        check("2b. unverified message never claims success", "не подтвержд" in str(result2.get("message") or ""), repr(result2))
        lease_2_after = await storage.proxy_lease_get(lease_id_2, db_path=tmp_db)
        check("2c. expires_at is left UNTOUCHED on unverified (never guessed)", lease_2_after.get("expires_at") == "2026-08-08", repr(lease_2_after))
        check("2d. last_renew_status records 'renew_unverified'", "renew_unverified" in str(lease_2_after.get("last_renew_status") or ""), repr(lease_2_after))
        check("2e. bounded refresh: exactly 2 list_proxies calls (max_attempts), not unbounded", provider2.list_proxies_calls == 2, str(provider2.list_proxies_calls))
        check("2f. exactly one prolong/make call even though unverified", provider2.prolong_make_calls == 1, str(provider2.prolong_make_calls))

        # ------------------------------------------------------------------
        # Case 3: a REPEATED renewal attempt after an unverified outcome for
        # the SAME (provider_proxy_id, pre-renewal expiry, period) must not
        # spend again -- proven via _renewal_wrapped_execute's idempotency
        # (op_create returns None for a colliding idempotency_key). This
        # selftest calls _prenew_execute_renewal directly (the raw spend
        # tail, which itself has no idempotency of its own -- that lives one
        # layer up in _renewal_wrapped_execute per R3), so here we prove the
        # PRECONDITION that makes that guarantee possible: expires_at (and
        # therefore the idempotency key, which is derived from it) stays
        # IDENTICAL after an unverified attempt, so a second wrapped call
        # would compute the SAME idempotency_key and be rejected before ever
        # reaching this function again.
        # ------------------------------------------------------------------
        idem_before = storage.proxy_renewal_idempotency_key(lease_2.get("provider_proxy_id"), lease_2.get("expires_at"), "1m")
        idem_after = storage.proxy_renewal_idempotency_key(lease_2_after.get("provider_proxy_id"), lease_2_after.get("expires_at"), "1m")
        check("3. idempotency key is UNCHANGED after an unverified attempt (blocks a same-cycle re-spend upstream)",
              idem_before == idem_after, f"{idem_before!r} vs {idem_after!r}")

        # ------------------------------------------------------------------
        # Case 4: provider_proxy_id ROTATES on renewal. The make response has
        # no date_end, so refresh kicks in; refresh can't find the OLD id but
        # matches by host:port and returns a NEW id with a later date -> the
        # lease's provider identity is resynced IN PLACE (same lease id, no
        # orphan duplicate row created).
        # ------------------------------------------------------------------
        lease_id_4 = await _make_lease(tmp_db, provider_proxy_id="PXY-OLD", host="4.4.4.4", port=50104, expires_at="2026-08-08")
        lease_4 = await storage.proxy_lease_get(lease_id_4, db_path=tmp_db)

        provider4 = FakeProvider()
        provider4.make_response = {"data": {"total": "1.8", "currency": "USD"}}
        # NOTE: the returned entry uses a NEW id ("PXY-NEW"), matched by host:port fallback
        provider4.queue_list_response([_entry("PXY-NEW", "4.4.4.4", 50104, "2026-09-08")])
        fake_provider_holder["instance"] = provider4

        result4 = await execute_renewal_fn(lease_4, source="autorenew")
        check("4a. rotated-id renewal still confirms success (matched by host:port)", result4.get("ok") is True, repr(result4))
        check("4b. result reflects the NEW provider_proxy_id", result4.get("provider_proxy_id") == "PXY-NEW", repr(result4))
        lease_4_after = await storage.proxy_lease_get(lease_id_4, db_path=tmp_db)
        check("4c. lease row's provider_proxy_id was resynced IN PLACE", lease_4_after.get("provider_proxy_id") == "PXY-NEW", repr(lease_4_after))
        check("4d. lease row's expires_at was bumped to the confirmed date", lease_4_after.get("expires_at") == "2026-09-08", repr(lease_4_after))
        check("4e. lease row's manager_key untouched by the identity resync", lease_4_after.get("manager_key") == "mgr_a", repr(lease_4_after))
        all_leases_4 = await storage.proxy_lease_list_all(db_path=tmp_db)
        matching_new_id = [l for l in all_leases_4 if l.get("provider_proxy_id") == "PXY-NEW"]
        check("4f. no orphan duplicate row was created for the new id (still exactly one row)", len(matching_new_id) == 1, str(len(matching_new_id)))

        # ------------------------------------------------------------------
        # Case 5: provider delayed consistency -- the FIRST refresh read
        # still shows the OLD date (provider hasn't caught up yet), the
        # SECOND (bounded) attempt shows the advanced date -> confirmed
        # within the attempt bound, still only ONE prolong/make call.
        # ------------------------------------------------------------------
        lease_id_5 = await _make_lease(tmp_db, provider_proxy_id="PXY-5", host="5.5.5.5", port=50105, expires_at="2026-08-08")
        lease_5 = await storage.proxy_lease_get(lease_id_5, db_path=tmp_db)

        provider5 = FakeProvider()
        provider5.make_response = {"data": {"total": "1.8", "currency": "USD"}}
        provider5.queue_list_response([_entry("PXY-5", "5.5.5.5", 50105, "2026-08-08")])  # attempt 1: stale
        provider5.queue_list_response([_entry("PXY-5", "5.5.5.5", 50105, "2026-09-08")])  # attempt 2: advanced
        fake_provider_holder["instance"] = provider5

        result5 = await execute_renewal_fn(lease_5, source="autorenew")
        check("5a. delayed consistency: confirmed within the bounded attempt count", result5.get("ok") is True, repr(result5))
        check("5b. delayed consistency: exactly 2 refresh reads used (not more, not fewer)", provider5.list_proxies_calls == 2, str(provider5.list_proxies_calls))
        check("5c. delayed consistency: still exactly ONE prolong/make call (no second spend while waiting)", provider5.prolong_make_calls == 1, str(provider5.prolong_make_calls))

        # ------------------------------------------------------------------
        # Extra: make response DOES carry a usable newer date directly ->
        # confirmed WITHOUT ever calling list_proxies (no unnecessary refresh
        # when the make response is already trustworthy).
        # ------------------------------------------------------------------
        lease_id_6 = await _make_lease(tmp_db, provider_proxy_id="PXY-6", host="6.6.6.6", port=50106, expires_at="2026-08-08")
        lease_6 = await storage.proxy_lease_get(lease_id_6, db_path=tmp_db)
        provider6 = FakeProvider()
        provider6.make_response = {"data": {"date_end": "2026-09-08", "total": "1.8", "currency": "USD"}}
        fake_provider_holder["instance"] = provider6
        result6 = await execute_renewal_fn(lease_6, source="autorenew")
        check("6a. make response WITH a usable newer date confirms directly", result6.get("ok") is True, repr(result6))
        check("6b. no refresh needed when the make response is already trustworthy", provider6.list_proxies_calls == 0, str(provider6.list_proxies_calls))

        # ------------------------------------------------------------------
        # Extra: make response repeats the SAME date (not advanced) with no
        # separate date_end trust -- must still go through refresh, not
        # blindly accept an unchanged "date_end" as success (the exact
        # production "Было==Стало" bug).
        # ------------------------------------------------------------------
        lease_id_7 = await _make_lease(tmp_db, provider_proxy_id="PXY-7", host="7.7.7.7", port=50107, expires_at="2026-08-08")
        lease_7 = await storage.proxy_lease_get(lease_id_7, db_path=tmp_db)
        provider7 = FakeProvider()
        provider7.make_response = {"data": {"date_end": "2026-08-08", "total": "1.8", "currency": "USD"}}  # SAME as before
        provider7.queue_list_response([_entry("PXY-7", "7.7.7.7", 50107, "2026-08-08")])
        provider7.queue_list_response([_entry("PXY-7", "7.7.7.7", 50107, "2026-08-08")])
        fake_provider_holder["instance"] = provider7
        result7 = await execute_renewal_fn(lease_7, source="autorenew")
        check("7. make response claims the SAME (unadvanced) date -> NOT accepted as success (production 'Было==Стало' bug)",
              result7.get("ok") is False and result7.get("error") == "unverified", repr(result7))
        lease_7_after = await storage.proxy_lease_get(lease_id_7, db_path=tmp_db)
        check("7b. expires_at untouched when the claimed date didn't actually advance", lease_7_after.get("expires_at") == "2026-08-08", repr(lease_7_after))

    print(f"\n{'='*70}")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
