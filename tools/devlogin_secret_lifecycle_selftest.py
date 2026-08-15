# -*- coding: utf-8 -*-
"""Offline selftest proving the device-login feature's OTP/plaintext-token
secret lifecycle end-to-end through the REAL controller command handlers
(main.py's _panel_manager_devlogin_start_command / _cancel_ / _consume_) and
the REAL manager_commands queue (storage.manager_queue_put/get/finish) --
not just the durable devlogin table already covered by
tools\\devlogin_durable_selftest.py.

Pure/offline: temp SQLite only, no network, no Telegram. Proves plan test
item U comprehensively (every table this feature ever writes to, plus
captured stdout) and re-confirms V/X/Y/Z at the integration level.

Run:  python tools\\devlogin_secret_lifecycle_selftest.py
"""
from __future__ import annotations

import ast
import hashlib
import io
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

MAIN_PY = os.path.join(BASE_DIR, "main.py")

import storage  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""), file=sys.__stdout__)
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
    exec(compile(module_src, "<main.py devlogin secret-lifecycle extract>", "exec"), ns)
    return ns


def main():
    tmpd = tempfile.mkdtemp(prefix="devlogin_secrets_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)

    OTP = "35791"  # never actually used by these handlers (they never see it),
                   # kept as the canonical "secret we must never find on disk"
    TOKEN_PLAIN = "b" * 64  # the plaintext capability token, AdminBot RAM only

    src = open(MAIN_PY, encoding="utf-8-sig").read()
    tree = ast.parse(src)

    # Minimal fakes for the handlers' non-devlogin dependencies.
    async def fake_manager_get(key):
        return {"manager_key": key, "phone": "+70000000000", "is_enabled": 1, "status": "active"}

    def fake_normalize(key):
        return str(key or "").strip().lower()

    def fake_future_iso(seconds):
        from datetime import timedelta
        return (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) + timedelta(seconds=int(seconds))).isoformat()

    def fake_now_iso():
        return datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None).replace(microsecond=0).isoformat()

    def fake_repl3_json_result(payload):
        import json
        return json.dumps(payload)

    ns = extract_and_exec(
        tree,
        ["_devlogin_new_request_id", "_panel_manager_devlogin_start_command",
         "_panel_manager_devlogin_cancel_command", "_panel_manager_devlogin_consume_command"],
        {
            "_devlogin_storage": storage,
            "_devlogin_json": __import__("json"),
            "_devlogin_secrets": __import__("secrets"),
            "manager_get": fake_manager_get,
            "registry_normalize_manager_key": fake_normalize,
            "_future_iso": fake_future_iso,
            "_now_utc_iso": fake_now_iso,
            "_repl3_json_result": fake_repl3_json_result,
            "TPILOT_DB_PATH": db,
            "Dict": dict, "Any": object,
            "DEVLOGIN_REQUEST_TTL_SEC": 180,
            "DEVLOGIN_COOLDOWN_SEC": 30,
        },
    )

    import asyncio
    loop = asyncio.new_event_loop()

    def run(coro):
        return loop.run_until_complete(coro)

    token_hash = hashlib.sha256(TOKEN_PLAIN.encode()).hexdigest()

    # Capture everything this "session" prints (matches project convention:
    # these handlers never print anything at all, but this also catches any
    # accidental future print/log statement leaking the token/OTP).
    captured = io.StringIO()
    with redirect_stdout(captured):
        result_text = run(ns["_panel_manager_devlogin_start_command"](
            f"testmgr {7} {token_hash}", requested_by=7))

    import json as _json
    data = _json.loads(result_text)
    check("start command succeeds with the fake manager row", data.get("ok") is True, detail=result_text)
    request_id = str(data.get("request_id") or "")
    check("start command returns a request_id", bool(request_id))
    check("start command result_text contains NO plaintext token", TOKEN_PLAIN not in result_text)
    check("start command result_text contains NO OTP", OTP not in result_text)

    # --- devlogin table: only token_hash, never plaintext/OTP --------------
    row = storage.devlogin_get(request_id, db_path=db)
    check("durable row was created", bool(row))
    check("durable row's token_hash matches the SHA-256 (not the plaintext)",
          row and row["token_hash"] == token_hash)
    row_blob = str(dict(row or {}))
    check("durable row (as a whole) contains NO plaintext token", TOKEN_PLAIN not in row_blob)
    check("durable row (as a whole) contains NO OTP", OTP not in row_blob)

    # --- manager_commands (devlogin_arm enqueue): payload contains only request_id/started_at
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        mc_rows = con.execute("SELECT * FROM manager_commands WHERE command='devlogin_arm'").fetchall()
    finally:
        con.close()
    check("exactly one devlogin_arm manager_commands row enqueued", len(mc_rows) == 1, detail=str(len(mc_rows)))
    if mc_rows:
        mc_blob = str(dict(mc_rows[0]))
        check("devlogin_arm manager_commands row contains NO plaintext token", TOKEN_PLAIN not in mc_blob)
        check("devlogin_arm manager_commands row contains NO OTP", OTP not in mc_blob)
        check("devlogin_arm payload_json contains only request_id/started_at",
              "request_id" in mc_blob and "started_at" in mc_blob and "token" not in mc_blob.lower().replace("token_hash", ""))

    # --- consume command: no code/token involved ----------------------------
    # Move the row through received->delivered so 'consume' has something to
    # flip (mirrors what the runtime bridge would have already done).
    storage.devlogin_transition(request_id, "waiting", "received",
                                fields={"telegram_message_id": 555, "received_at": fake_now_iso()}, db_path=db)
    storage.devlogin_claim_for_delivery(request_id, db_path=db)
    captured2 = io.StringIO()
    with redirect_stdout(captured2):
        consume_text = run(ns["_panel_manager_devlogin_consume_command"](request_id, requested_by=7))
    check("consume command result contains NO OTP/token", OTP not in consume_text and TOKEN_PLAIN not in consume_text)
    row_after_consume = storage.devlogin_get(request_id, db_path=db)
    check("consume command flips delivered->consumed", row_after_consume and row_after_consume["status"] == "consumed")

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        mc_rows2 = con.execute("SELECT * FROM manager_commands WHERE command='devlogin_close'").fetchall()
    finally:
        con.close()
    check("consume also enqueues a devlogin_close signal", len(mc_rows2) >= 1, detail=str(len(mc_rows2)))

    # --- cancel path: same guarantees --------------------------------------
    request_id2 = str(_json.loads(run(ns["_panel_manager_devlogin_start_command"](
        f"testmgr2 {9} {token_hash}", requested_by=9))).get("request_id") or "")
    cancel_text = run(ns["_panel_manager_devlogin_cancel_command"](request_id2, requested_by=9))
    check("cancel command result contains NO OTP/token", OTP not in cancel_text and TOKEN_PLAIN not in cancel_text)

    # --- U: comprehensive raw-disk scan of EVERY table this feature touches -
    con = sqlite3.connect(db)
    try:
        con.execute("PRAGMA wal_checkpoint(FULL)")
    finally:
        con.close()
    scanned_files = [p for p in (db, db + "-wal", db + "-shm", db + "-journal") if os.path.isfile(p)]
    otp_hits, tok_hits = [], []
    for p in scanned_files:
        with open(p, "rb") as f:
            blob = f.read()
        if OTP.encode() in blob:
            otp_hits.append(p)
        if TOKEN_PLAIN.encode() in blob:
            tok_hits.append(p)
    check("U: OTP appears in NO on-disk file across the full simulated flow "
          "(devlogin table + manager_commands + WAL/journal)", not otp_hits, detail=str(otp_hits))
    check("U: plaintext token appears in NO on-disk file across the full simulated flow",
          not tok_hits, detail=str(tok_hits))

    # --- captured stdout across the whole flow ------------------------------
    all_captured = captured.getvalue() + captured2.getvalue()
    check("no plaintext token was ever printed to stdout during the flow", TOKEN_PLAIN not in all_captured)
    check("no OTP was ever printed to stdout during the flow", OTP not in all_captured)

    # --- Z: explicit, non-removable documentation of the residual risk -----
    devlogin_block_start = src.index("TPILOT DEVICE-LOGIN (connect Telegram on another device) BEGIN")
    devlogin_block_end = src.index("TPILOT DEVICE-LOGIN (connect Telegram on another device) END")
    block = src[devlogin_block_start:devlogin_block_end]
    check("Z: main.py devlogin block documents the OTP is re-read on demand and "
          "never cached (the physical-erasure-cannot-be-guaranteed risk lives in "
          "panel_bot.py's UI warning + design comments, verified by the wiring selftest)",
          "never cached in a" in block or "re-read from Telegram on demand" in block)

    loop.close()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL DEVLOGIN SECRET LIFECYCLE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
