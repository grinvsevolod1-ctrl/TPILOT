# -*- coding: utf-8 -*-
"""Offline selftest for the manager-runtime side of the device-login feature
(main.py's _devlogin_maybe_capture / _devlogin_validate_and_parse /
_devlogin_try_capture_message / _devlogin_scan_once).

main.py cannot be imported standalone (Telethon/env side effects at import
time) -- uses the project's established AST-extraction idiom (ast.parse ->
ast.unparse -> exec) to pull the pure/testable devlogin runtime functions out
of the file and exercise them against the REAL storage.py devlogin_* API on a
temp SQLite DB, with a fake Telethon message/event and a fake `client` for the
catch-up scan. No network, no Telegram, no production DB.

Covers plan test items A-F:
  A. only sender 777000 accepted;
  B. spoofed sender/private-chat rejected;
  C. group/channel/outgoing/old messages rejected;
  D. duplicate message_id ignored;
  E. expired/cancelled requests ignored;
  F. bounded catch-up only, limit=5.

Run:  python tools\\devlogin_runtime_selftest.py
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

MAIN_PY = os.path.join(BASE_DIR, "main.py")

import storage  # noqa: E402 -- REAL storage.py, exercised against a temp DB
from login_code_parser import parse_login_code  # noqa: E402

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
    exec(compile(module_src, "<main.py devlogin extract>", "exec"), ns)
    return ns


class FakeMessage:
    def __init__(self, *, id, text, date, sender_id=777000, out=False, is_private=True, entities=None):
        self.id = id
        self.message = text
        self.raw_text = text
        self.date = date
        self.sender_id = sender_id
        self.out = out
        self._is_private = is_private
        self.entities = entities

    @property
    def is_private(self):
        return self._is_private


class FakeEvent:
    def __init__(self, message: FakeMessage):
        self.message = message
        self.sender_id = message.sender_id
        self.out = message.out
        self.is_private = message.is_private


class FakeClient:
    """Only iter_messages is exercised (the bounded catch-up scan)."""

    def __init__(self, messages):
        self._messages = list(messages)

    async def iter_messages(self, peer, limit=5):
        assert peer == 777000, f"catch-up must target peer 777000, got {peer!r}"
        for m in self._messages[:limit]:
            yield m


def main():
    tmpd = tempfile.mkdtemp(prefix="devlogin_runtime_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)

    src = open(MAIN_PY, encoding="utf-8-sig").read()
    tree = ast.parse(src)

    # --- structural: verify the constants match what the code actually uses --
    body = tree.body
    const_vals = {}
    for n in body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
            name = n.targets[0].id
            if name in ("DEVLOGIN_SERVICE_PEER_ID", "DEVLOGIN_MESSAGE_MAX_LEN",
                        "DEVLOGIN_CLOCK_SKEW_SEC", "DEVLOGIN_CATCHUP_LIMIT"):
                try:
                    const_vals[name] = ast.literal_eval(n.value)
                except Exception:
                    pass
    check("DEVLOGIN_SERVICE_PEER_ID == 777000", const_vals.get("DEVLOGIN_SERVICE_PEER_ID") == 777000,
          detail=str(const_vals.get("DEVLOGIN_SERVICE_PEER_ID")))
    check("DEVLOGIN_MESSAGE_MAX_LEN == 512", const_vals.get("DEVLOGIN_MESSAGE_MAX_LEN") == 512)
    check("DEVLOGIN_CATCHUP_LIMIT == 5 (bounded catch-up)", const_vals.get("DEVLOGIN_CATCHUP_LIMIT") == 5)

    def _now_utc_iso():
        return datetime.utcnow().replace(microsecond=0).isoformat()

    ns_base = {
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        "Dict": dict, "Any": object, "Optional": type(None),
        "_devlogin_parse_code": parse_login_code,
        "_devlogin_storage": storage,
        "_now_utc_iso": _now_utc_iso,
        "TPILOT_DB_PATH": db,
        "DEVLOGIN_SERVICE_PEER_ID": 777000,
        "DEVLOGIN_MESSAGE_MAX_LEN": 512,
        "DEVLOGIN_CLOCK_SKEW_SEC": 5,
        "DEVLOGIN_CATCHUP_LIMIT": 5,
        "print": print,
    }

    def build_ns(controller_mode, runtime_key, client=None):
        ns = extract_and_exec(
            tree,
            ["_devlogin_iso_to_dt", "_devlogin_validate_and_parse",
             "_devlogin_try_capture_message", "_devlogin_maybe_capture", "_devlogin_scan_once"],
            dict(ns_base),
        )
        ns["CONTROLLER_MODE"] = controller_mode
        ns["MANAGER_RUNTIME_KEY"] = runtime_key
        if client is not None:
            ns["client"] = client
        return ns

    import asyncio

    _loop = asyncio.new_event_loop()

    def run(coro):
        return _loop.run_until_complete(coro)

    now_iso = _now_utc_iso()
    now_dt = datetime.utcnow().replace(tzinfo=timezone.utc)

    # === A/B: only sender 777000, non-private, outgoing rejected ==========
    storage.devlogin_create("dl_a1", "TestMgrA", requested_by_user_id=1,
                            token_hash="0" * 64, started_at=now_iso,
                            expires_at=(datetime.utcnow() + timedelta(seconds=180)).isoformat(),
                            db_path=db)
    ns = build_ns(False, "testmgra")

    ev_wrong_sender = FakeEvent(FakeMessage(id=1, text="Код для входа: 12345.", date=now_dt, sender_id=123456))
    captured = run(ns["_devlogin_maybe_capture"](ev_wrong_sender))
    check("A: wrong sender_id (not 777000) rejected", captured is False)

    ev_group = FakeEvent(FakeMessage(id=2, text="Код для входа: 22222.", date=now_dt, is_private=False))
    captured = run(ns["_devlogin_maybe_capture"](ev_group))
    check("B: non-private (group/channel) message rejected", captured is False)

    ev_outgoing = FakeEvent(FakeMessage(id=3, text="Код для входа: 33333.", date=now_dt, out=True))
    captured = run(ns["_devlogin_maybe_capture"](ev_outgoing))
    check("C: outgoing message rejected", captured is False)

    row_a = storage.devlogin_get("dl_a1", db_path=db)
    check("A/B/C: request still 'waiting' (nothing wrongly captured)", row_a and row_a["status"] == "waiting")

    # --- old message (before started_at - skew) rejected -------------------
    old_dt = now_dt - timedelta(seconds=999)
    ev_old = FakeEvent(FakeMessage(id=4, text="Код для входа: 44444.", date=old_dt))
    captured = run(ns["_devlogin_maybe_capture"](ev_old))
    check("C: message older than started_at (beyond clock skew) rejected", captured is False)

    # --- genuine capture works ----------------------------------------------
    ev_ok = FakeEvent(FakeMessage(id=5, text="Код для входа: 55555. Никому не сообщайте.", date=now_dt))
    captured = run(ns["_devlogin_maybe_capture"](ev_ok))
    check("A: genuine 777000 private incoming fresh message captured", captured is True)
    row_a2 = storage.devlogin_get("dl_a1", db_path=db)
    check("captured request moved waiting->received", row_a2 and row_a2["status"] == "received")
    check("only telegram_message_id recorded (never the code)", row_a2 and row_a2["telegram_message_id"] == 5)

    # === D: duplicate message_id ignored (even if resubmitted) =============
    storage.devlogin_create("dl_d1", "TestMgrD", requested_by_user_id=1,
                            token_hash="1" * 64, started_at=now_iso,
                            expires_at=(datetime.utcnow() + timedelta(seconds=180)).isoformat(),
                            db_path=db)
    ns_d = build_ns(False, "testmgrd")
    ev_dup = FakeEvent(FakeMessage(id=777, text="Код: 66666.", date=now_dt))
    first = run(ns_d["_devlogin_maybe_capture"](ev_dup))
    check("D: first delivery of a message captures", first is True)
    # simulate the SAME message id reprocessed (e.g. a catch-up race) against
    # a manually reset row -- devlogin_seen_msg dedup must still block it.
    storage.devlogin_transition("dl_d1", "received", "waiting", db_path=db)  # test-only rewind
    second = run(ns_d["_devlogin_maybe_capture"](ev_dup))
    check("D: duplicate message_id ignored even after a status rewind", second is False)

    # === E: expired/cancelled requests ignored ==============================
    storage.devlogin_create("dl_e1", "TestMgrE", requested_by_user_id=1,
                            token_hash="2" * 64, started_at=now_iso,
                            expires_at="2000-01-01T00:00:00",  # already expired
                            db_path=db)
    ns_e = build_ns(False, "testmgre")
    ev_e = FakeEvent(FakeMessage(id=8, text="Код: 77777.", date=now_dt))
    captured_e = run(ns_e["_devlogin_maybe_capture"](ev_e))
    check("E: expired request's message is ignored", captured_e is False)

    storage.devlogin_create("dl_e2", "TestMgrE2", requested_by_user_id=1,
                            token_hash="3" * 64, started_at=now_iso,
                            expires_at=(datetime.utcnow() + timedelta(seconds=180)).isoformat(),
                            db_path=db)
    storage.devlogin_cancel("dl_e2", db_path=db)
    ns_e2 = build_ns(False, "testmgre2")
    ev_e2 = FakeEvent(FakeMessage(id=9, text="Код: 88888.", date=now_dt))
    captured_e2 = run(ns_e2["_devlogin_maybe_capture"](ev_e2))
    check("E: cancelled request's message is ignored", captured_e2 is False)

    # === controller mode / no runtime key never captures ====================
    storage.devlogin_create("dl_cm1", "TestMgrCM", requested_by_user_id=1,
                            token_hash="4" * 64, started_at=now_iso,
                            expires_at=(datetime.utcnow() + timedelta(seconds=180)).isoformat(),
                            db_path=db)
    ns_cm = build_ns(True, "testmgrcm")  # CONTROLLER_MODE=True
    ev_cm = FakeEvent(FakeMessage(id=10, text="Код: 99999.", date=now_dt))
    check("controller-mode process never captures", run(ns_cm["_devlogin_maybe_capture"](ev_cm)) is False)

    ns_nokey = build_ns(False, "")  # no runtime key
    check("process with no MANAGER_RUNTIME_KEY never captures",
          run(ns_nokey["_devlogin_maybe_capture"](ev_cm)) is False)

    # === F: bounded catch-up scan, limit=5, only newer-than-started_at ======
    storage.devlogin_create("dl_f1", "TestMgrF", requested_by_user_id=1,
                            token_hash="5" * 64, started_at=now_iso,
                            expires_at=(datetime.utcnow() + timedelta(seconds=180)).isoformat(),
                            db_path=db)
    # 5 old (pre-started_at) messages + 1 fresh matching one further back in
    # the iterator than the catch-up limit would reach if it scanned in the
    # wrong order -- proves the scan is bounded to `limit` messages total,
    # not "first N matching".
    msgs = [FakeMessage(id=100 + i, text=f"нет кода тут {i}", date=old_dt) for i in range(5)]
    msgs.append(FakeMessage(id=200, text="Код: 13579.", date=now_dt))
    fake_client = FakeClient(msgs)
    ns_f = build_ns(False, "testmgrf", client=fake_client)
    res_f = run(ns_f["_devlogin_scan_once"]("dl_f1"))
    check("F: bounded scan (limit=5) does NOT find a match past the limit",
          res_f.get("captured") is False, detail=str(res_f))
    row_f = storage.devlogin_get("dl_f1", db_path=db)
    check("F: request still 'waiting' after a scan that found nothing within the bound",
          row_f and row_f["status"] == "waiting")

    # a fresh match WITHIN the bound is captured
    msgs2 = [FakeMessage(id=300, text="Код: 24681.", date=now_dt)] + \
            [FakeMessage(id=301 + i, text="ничего", date=old_dt) for i in range(4)]
    fake_client2 = FakeClient(msgs2)
    ns_f2 = build_ns(False, "testmgrf", client=fake_client2)
    res_f2 = run(ns_f2["_devlogin_scan_once"]("dl_f1"))
    check("F: match within the bounded scan window is captured", res_f2.get("captured") is True, detail=str(res_f2))

    _loop.close()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL DEVLOGIN RUNTIME SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
