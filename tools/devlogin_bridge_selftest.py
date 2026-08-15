# -*- coding: utf-8 -*-
"""Offline selftest for the device-login LOOPBACK BRIDGE (main.py's
_devlogin_start_bridge / _devlogin_bridge_connection / _devlogin_bridge_serve_one
/ _devlogin_close_bridge).

Runs a REAL asyncio.start_server on 127.0.0.1 (in-process, ephemeral port) and
REAL asyncio.open_connection clients against it -- no Telegram, no proxy
network, no production DB. The 777000 message re-read is faked via a minimal
`client.get_messages` stub (the bridge itself does not know/care that it's
fake) so the ATOMIC one-shot claim/protocol/binding logic under test is the
REAL production code, extracted via AST from main.py.

Covers plan test items G-M:
  G. actual socket binds exactly 127.0.0.1;
  H. malformed/oversized requests rejected generically;
  I. wrong token rejected via the hmac.compare_digest path;
  J. two concurrent valid fetches: exactly one receives the OTP;
  K. second fetch after claim never receives the OTP;
  L. simulated AdminBot crash after claim -> loss, not duplicate delivery
     (modeled as: the CAS winner is decided before any I/O with the client,
     so a client that vanishes after triggering the claim never gets a
     second chance -- proven by K/J already; this test additionally proves
     the row is left in 'delivered', not silently reverted/re-openable).
  M. unauthorized admin cannot obtain the code (wrong token = same as I;
     proven end-to-end here rather than only at the storage layer).

Run:  python tools\\devlogin_bridge_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import hashlib
import hmac
import json
import os
import struct
import sys
import tempfile
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

MAIN_PY = os.path.join(BASE_DIR, "main.py")

import storage  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _guard_temp_db(db_path):
    rp = os.path.realpath(db_path)
    tmp = os.path.realpath(tempfile.gettempdir())
    assert rp.startswith(tmp), f"db must live under tempdir, got {rp}"
    assert "data_tpilot.db" not in rp and os.sep + "db" + os.sep not in rp, rp


def find_defs(tree, name):
    return [n for n in tree.body if getattr(n, "name", None) == name]


def last_def(tree, name):
    defs = find_defs(tree, name)
    if not defs:
        raise AssertionError(f"no top-level def named {name!r} found in main.py")
    return defs[-1]


def extract_and_exec(tree, names, extra_ns):
    nodes = [last_def(tree, n) for n in names]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, "<main.py devlogin bridge extract>", "exec"), ns)
    return ns


class FakeTgMessage:
    def __init__(self, text):
        self.message = text
        self.entities = None


class FakeClient:
    """Only get_messages(peer, ids=...) is exercised (the re-read-on-fetch
    step). Records call count so we can prove it is called AT MOST ONCE per
    successful claim (never per losing connection)."""

    def __init__(self, code_text):
        self._code_text = code_text
        self.get_messages_calls = 0

    async def get_messages(self, peer, ids=None):
        self.get_messages_calls += 1
        assert peer == 777000
        return FakeTgMessage(self._code_text)


def main():
    tmpd = tempfile.mkdtemp(prefix="devlogin_bridge_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)

    src = open(MAIN_PY, encoding="utf-8-sig").read()
    tree = ast.parse(src)

    ns_base = {
        "TPILOT_DB_PATH": db,
        "asyncio": asyncio, "struct": struct, "json": json,
        "_devlogin_hashlib": hashlib, "_devlogin_hmac": hmac,
        "_devlogin_json": json, "_devlogin_struct": struct,
        "_devlogin_storage": storage,
        "_devlogin_parse_code": __import__("login_code_parser").parse_login_code,
        "_now_utc_iso": lambda: datetime.utcnow().replace(microsecond=0).isoformat(),
        "Dict": dict, "Any": object, "Optional": type(None),
        "print": print,
        "DEVLOGIN_BRIDGE_IO_TIMEOUT_SEC": 2.0,
        "DEVLOGIN_BRIDGE_MAX_REQUEST_BYTES": 256,
        "DEVLOGIN_BRIDGE_MAX_RESPONSE_BYTES": 64,
        "DEVLOGIN_BRIDGE_MAX_CONNECTIONS": 4,
        "DEVLOGIN_SERVICE_PEER_ID": 777000,
    }

    def build_bridge_ns(client):
        ns = extract_and_exec(
            tree,
            ["_devlogin_bridge_read_frame", "_devlogin_bridge_write_frame",
             "_devlogin_bridge_reject", "_devlogin_bridge_serve_one",
             "_devlogin_bridge_connection", "_devlogin_start_bridge", "_devlogin_close_bridge"],
            dict(ns_base),
        )
        ns["client"] = client
        ns["_DEVLOGIN_BRIDGES"] = {}
        ns["CONTROLLER_MODE"] = False
        ns["MANAGER_RUNTIME_KEY"] = "testmgr"
        return ns

    loop = asyncio.new_event_loop()

    def run(coro):
        return loop.run_until_complete(coro)

    async def send_frame(writer, obj):
        body = json.dumps(obj).encode("utf-8")
        writer.write(struct.pack(">I", len(body)) + body)
        await writer.drain()

    async def send_raw(writer, raw_bytes):
        writer.write(raw_bytes)
        await writer.drain()

    async def read_frame(reader):
        header = await reader.readexactly(4)
        (length,) = struct.unpack(">I", header)
        body = await reader.readexactly(length)
        return json.loads(body.decode("utf-8"))

    def make_request(request_id, manager_key, token_hash, message_id, expires_in=180):
        started_at = datetime.utcnow().isoformat()
        expires_at = (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat()
        storage.devlogin_create(request_id, manager_key, requested_by_user_id=1,
                                token_hash=token_hash, started_at=started_at,
                                expires_at=expires_at, db_path=db)
        storage.devlogin_transition(request_id, "waiting", "received",
                                    fields={"telegram_message_id": message_id,
                                            "received_at": started_at}, db_path=db)

    # === G: actual socket binds exactly 127.0.0.1 ===========================
    fake_client_g = FakeClient("Код: 11111.")
    ns_g = build_bridge_ns(fake_client_g)
    run(ns_g["_devlogin_start_bridge"]("dl_bind_check", "testmgr"))
    state_g = ns_g["_DEVLOGIN_BRIDGES"].get("dl_bind_check")
    check("G: bridge state created", bool(state_g))
    server_g = state_g.get("server") if state_g else None
    check("G: server actually started", server_g is not None)
    if server_g is not None:
        sockname = server_g.sockets[0].getsockname()
        check("G: actual bound socket address is 127.0.0.1", sockname[0] == "127.0.0.1", detail=str(sockname))
    run(ns_g["_devlogin_close_bridge"]("dl_bind_check"))
    check("bridge state removed after close", "dl_bind_check" not in ns_g["_DEVLOGIN_BRIDGES"])

    # === Full happy-path harness reused by H-M ==============================
    def setup_request(label_suffix, code_text="Код: 24680."):
        rid = f"dl_test_{label_suffix}"
        mk = f"testmgr_{label_suffix}"
        token_plain = "a" * 64
        token_hash = hashlib.sha256(token_plain.encode()).hexdigest()
        make_request(rid, mk, token_hash, message_id=42)
        fake_client = FakeClient(code_text)
        ns = build_bridge_ns(fake_client)
        run(ns["_devlogin_start_bridge"](rid, mk))
        state = ns["_DEVLOGIN_BRIDGES"][rid]
        port = state["server"].sockets[0].getsockname()[1]
        return rid, mk, token_plain, token_hash, ns, fake_client, port

    async def one_fetch(port, rid, token):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await send_frame(writer, {"request_id": rid, "token": token})
        resp = await read_frame(reader)
        writer.close()
        return resp

    async def one_fetch_raw(port, raw_bytes):
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        await send_raw(writer, raw_bytes)
        try:
            resp = await asyncio.wait_for(read_frame(reader), timeout=2.0)
        except Exception:
            resp = None
        writer.close()
        return resp

    # === H: malformed / oversized requests rejected generically =============
    rid_h, mk_h, tok_h, hash_h, ns_h, client_h, port_h = setup_request("h")
    resp_malformed = run(one_fetch_raw(port_h, b"\x00\x00\x00\x05notjs"))
    check("H: malformed (invalid JSON) request rejected generically",
          resp_malformed is not None and resp_malformed.get("ok") is False, detail=str(resp_malformed))
    oversized_body = json.dumps({"request_id": rid_h, "token": "x" * 500}).encode()
    resp_oversized = run(one_fetch_raw(port_h, struct.pack(">I", len(oversized_body)) + oversized_body))
    check("H: oversized request rejected generically",
          resp_oversized is not None and resp_oversized.get("ok") is False, detail=str(resp_oversized))
    row_h = storage.devlogin_get(rid_h, db_path=db)
    check("H: malformed/oversized requests never advanced status",
          row_h and row_h["status"] == "received", detail=str(row_h))
    run(ns_h["_devlogin_close_bridge"](rid_h))

    # === I / M: wrong token rejected via hmac.compare_digest path ===========
    rid_i, mk_i, tok_i, hash_i, ns_i, client_i, port_i = setup_request("i")
    resp_wrong = run(one_fetch(port_i, rid_i, "0" * 64))  # well-formed but WRONG token
    check("I/M: wrong token rejected (unauthorized fetch never gets the code)",
          resp_wrong.get("ok") is False, detail=str(resp_wrong))
    check("I: get_messages was NEVER called for a rejected/unauthorized fetch",
          client_i.get_messages_calls == 0)
    row_i = storage.devlogin_get(rid_i, db_path=db)
    check("I: wrong-token attempt never advanced status past 'received'",
          row_i and row_i["status"] == "received")
    resp_right = run(one_fetch(port_i, rid_i, tok_i))
    check("I: the CORRECT token afterwards still succeeds", resp_right.get("ok") is True, detail=str(resp_right))
    run(ns_i["_devlogin_close_bridge"](rid_i))

    # === J / K: two concurrent valid fetches -> exactly one wins ============
    rid_j, mk_j, tok_j, hash_j, ns_j, client_j, port_j = setup_request("j")

    async def concurrent_pair():
        return await asyncio.gather(
            one_fetch(port_j, rid_j, tok_j),
            one_fetch(port_j, rid_j, tok_j),
        )

    resp1, resp2 = run(concurrent_pair())
    oks = [r.get("ok") for r in (resp1, resp2)]
    check("J: exactly one of two concurrent valid fetches receives the OTP",
          oks.count(True) == 1 and oks.count(False) == 1, detail=str(oks))
    winner = resp1 if resp1.get("ok") else resp2
    check("J: the winner's code is exactly 5 digits", isinstance(winner.get("code"), str) and len(winner["code"]) == 5)
    row_j = storage.devlogin_get(rid_j, db_path=db)
    check("J: request status is 'delivered' after the winning claim",
          row_j and row_j["status"] == "delivered")
    check("J: get_messages (re-read) called exactly once, never by the loser",
          client_j.get_messages_calls == 1, detail=str(client_j.get_messages_calls))

    # === K: a THIRD fetch after the claim never receives the code ===========
    resp3 = run(one_fetch(port_j, rid_j, tok_j))
    check("K: a fetch AFTER the claim never receives the OTP", resp3.get("ok") is False, detail=str(resp3))
    check("K: get_messages still called exactly once (K did not trigger a re-read)",
          client_j.get_messages_calls == 1)

    # === L: row stays 'delivered' (not silently reverted/reopened) ==========
    row_j2 = storage.devlogin_get(rid_j, db_path=db)
    check("L: post-claim row remains 'delivered' -- a vanished requester loses "
          "the code rather than it becoming re-claimable", row_j2 and row_j2["status"] == "delivered")
    run(ns_j["_devlogin_close_bridge"](rid_j))

    loop.close()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL DEVLOGIN BRIDGE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
