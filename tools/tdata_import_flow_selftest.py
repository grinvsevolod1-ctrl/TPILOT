# -*- coding: utf-8 -*-
"""Offline selftest for tdata_import.service -- the full orchestrated state
machine (start_import / get_status / confirm_install / cancel_import).

Pure/offline: temp SQLite only (DB-guarded), a fully synthetic Telethon v7
`.session` built in-process (no real Telegram data), fake proxy prober, fake
Telegram client, fake runtime spawn/readiness callables. NO real network, NO
real Telegram/proxy calls, NO spend. Proves:
  - direct-mode is rejected BEFORE any Telegram client is ever constructed;
  - proxy_missing / proxy_unavailable stop the flow before Telegram use;
  - session_unauthorized / duplicate-account detection work;
  - one active operation per manager (concurrent_operation);
  - confirm_install only proceeds from identity_verified (durable-restart-safe:
    a second, independent "process" -- a fresh get_status()/confirm_install()
    call sequence -- can resume purely from the DB row);
  - a failed runtime start rolls back the atomically-installed session file
    exactly to its prior state;
  - cancel_import cleans up and is idempotent-safe against re-cancel.

Run:  python tools\\tdata_import_flow_selftest.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import storage  # noqa: E402
from tdata_import import proxy_gate, service  # noqa: E402
from tdata_import.errors import ProxyUnavailable  # noqa: E402

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


def _build_synthetic_session(path, *, dc_id=2, addr="149.154.167.51", port=443):
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE version (version integer primary key)")
        con.execute("CREATE TABLE sessions (dc_id integer primary key, server_address text,"
                    " port integer, auth_key blob, takeout_id integer)")
        con.execute("CREATE TABLE entities (id integer primary key, hash integer not null,"
                    " username text, phone integer, name text, date integer)")
        con.execute("INSERT INTO version VALUES (7)")
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,NULL)", (dc_id, addr, port, b"\xab" * 256))
        con.commit()
    finally:
        con.close()


def _zip_session_archive(dest_zip, session_path):
    with zipfile.ZipFile(dest_zip, "w") as zf:
        zf.write(session_path, arcname="account/imported.session")


class FakeUser:
    def __init__(self, *, id, username="testuser", first_name="Test", last_name="User",
                phone="79001234567", bot=False, deleted=False):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.phone = phone
        self.bot = bot
        self.deleted = deleted


class FakeClient:
    """Matches identity_probe.TelegramClientLike. Records disconnect() calls
    and never opens a real socket."""

    def __init__(self, *, authorized=True, me=None):
        self._authorized = authorized
        self._me = me
        self.disconnected = False

    async def is_user_authorized(self):
        return self._authorized

    async def get_me(self):
        return self._me

    async def disconnect(self):
        self.disconnected = True


def _read_session_auth_key(session_path):
    """Read the raw auth_key blob straight from a Telethon v7 sqlite session
    file (bypassing session_inspector/Telethon entirely) -- used ONLY by
    _make_client_factory's own defensive verification below, so it must stay
    independent of the code paths it is checking."""
    con = sqlite3.connect(session_path)
    try:
        row = con.execute("SELECT auth_key FROM sessions LIMIT 1").fetchone()
    finally:
        con.close()
    return row[0] if row else None


def _make_client_factory(client, call_log):
    # PROBE-WIRING FIX 20260719 (regression guard): this factory now MUST
    # accept the session_path service.py passes in (the real
    # session_source_path it validated/converted -- see service.py's
    # start_import), matching the corrected ClientFactoryFn contract
    # (Callable[[str], Awaitable[Any]], was Callable[[], Awaitable[Any]]).
    #
    # It also independently re-opens that EXACT file and asserts it carries a
    # real 256-byte auth_key BEFORE returning the (fake) client -- this is a
    # deliberate regression guard against the original wiring bug's failure
    # shape: main.py used to hand start_import's client_factory a hardcoded,
    # never-written `work_root/probe.session`, and a naive fake client whose
    # get_me() just returns a canned FakeUser would have kept "passing" even
    # though the REAL client would have opened an empty session and gotten
    # get_me()->None->session_unauthorized. By verifying the auth_key
    # ourselves here, every existing/new test using this factory fails loudly
    # if start_import ever again hands the factory an empty/decoy/wrong path,
    # regardless of what any FakeClient.get_me() is hardcoded to return.
    async def factory(session_path):
        call_log.append(session_path)
        key = _read_session_auth_key(session_path)
        if not key or len(key) != 256:
            raise AssertionError(
                f"client_factory received a session with no valid 256-byte "
                f"auth_key: {session_path!r} (got {len(key) if key else 0} bytes) "
                f"-- this is the exact shape of the original probe-wiring bug"
            )
        return client
    return factory


def _fake_prober_ok(ip="203.0.113.7"):
    def prober(host, port, username, password, rdns):
        return proxy_gate.ProxyVerification(ip=ip, method="socks5h" if rdns else "socks5")
    return prober


def _fake_prober_always_fails(host, port, username, password, rdns):
    raise ProxyUnavailable("simulated proxy down")


PROXY_ROW_OK = {
    "proxy_mode": "proxy", "proxy_lease_id": 1, "proxy_host": "10.0.0.1", "proxy_port": 1080,
    "proxy_username": "u", "proxy_password": "p",
}
PROXY_ROW_DIRECT = {"proxy_mode": "direct", "proxy_host": "", "proxy_port": 0}
PROXY_ROW_MISSING = {"proxy_mode": "proxy", "proxy_host": "", "proxy_port": 0}


def main():
    tmpd = tempfile.mkdtemp(prefix="tdimport_flow_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)

    # --- build one shared synthetic "ready session" archive ---------------
    sess_dir = os.path.join(tmpd, "src")
    os.makedirs(sess_dir, exist_ok=True)
    sess_path = os.path.join(sess_dir, "imported.session")
    _build_synthetic_session(sess_path)

    def fresh_archive(name):
        p = os.path.join(tmpd, name)
        _zip_session_archive(p, sess_path)
        return p

    async def run():
        # === 1. Happy path: full start -> confirm -> runtime_running ======
        op1 = "op-happy-1"
        mgr1 = "testmgrhappy"
        work_root1 = os.path.join(tmpd, "work1")
        client_calls = []
        client = FakeClient(me=FakeUser(id=555000111))
        spawned, waited, stopped = [], [], []

        async def wait_ready_ok(key):
            waited.append(key)
            return True

        r1 = await service.start_import(
            operation_id=op1, manager_key=mgr1, archive_path=fresh_archive("a1.zip"),
            work_root=work_root1, proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(client, client_calls),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("happy: start_import ok", r1.get("ok") is True, detail=str(r1))
        check("happy: stage is identity_verified", r1.get("stage") == "identity_verified", detail=str(r1.get("stage")))
        check("happy: tg_user_id recorded", r1.get("tg_user_id") == 555000111)
        check("happy: masked_phone recorded, not full", r1.get("masked_phone", "").count("*") > 0)
        check("happy: client_factory called exactly once", len(client_calls) == 1, detail=str(client_calls))
        check("happy: client disconnected after probe", client.disconnected is True)

        # === 1b. READY SESSION PATH WIRING (TPILOT PROBE-WIRING FIX 20260719) ==
        # This is the regression test for the proven live defect: the client
        # factory must receive the REAL extracted ready-session path --
        # session_inspector's own validated candidate -- never a separate/blank
        # decoy file such as the old hardcoded `work_root/probe.session`.
        received_path_1b = client_calls[0]
        check("1b. client_factory received a real, existing file (not a decoy)",
              os.path.isfile(received_path_1b), detail=received_path_1b)
        check("1b. received path is the extracted ready .session (basename preserved)",
              os.path.basename(received_path_1b) == "imported.session", detail=received_path_1b)
        check("1b. received path lives under this operation's work_root",
              os.path.realpath(received_path_1b).startswith(os.path.realpath(work_root1) + os.sep),
              detail=received_path_1b)
        check("1b. received path is NOT the old hardcoded probe.session name",
              os.path.basename(received_path_1b) != "probe.session", detail=received_path_1b)
        check("1b. received file carries the real 256-byte auth_key (not empty)",
              _read_session_auth_key(received_path_1b) == b"\xab" * 256)
        check("1b. no probe.session was created anywhere under work_root",
              not any(fn == "probe.session" for _r, _d, fs in os.walk(work_root1) for fn in fs))

        st1 = service.get_status(operation_id=op1, db_path=db)
        check("happy: get_status matches start_import result", st1.get("stage") == "identity_verified")

        final_path_holder = {}

        def spawn_ok(key):
            spawned.append(key)

        base_dir1 = tmpd  # manager_registry.build_manager_paths(base_dir, key) -> base_dir/runtime/managers/<key>/

        r1c = await service.confirm_install(
            operation_id=op1, base_dir=base_dir1, proxy_row=PROXY_ROW_OK,
            spawn_runtime=spawn_ok, wait_runtime_ready=wait_ready_ok,
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("happy: confirm_install ok", r1c.get("ok") is True, detail=str(r1c))
        check("happy: final status done/completed", r1c.get("status") == "done" and r1c.get("stage") == "completed")
        check("happy: spawn_runtime called", spawned == [mgr1])
        check("happy: wait_runtime_ready called", waited == [mgr1])

        from manager_registry import build_manager_paths
        installed_path = build_manager_paths(base_dir1, mgr1)["session_path"]
        check("happy: session file physically installed", os.path.isfile(installed_path))
        check("happy: work_root cleaned up after completion", not os.path.isdir(work_root1))

        # === 1c. TDATA PATH WIRING (TPILOT PROBE-WIRING FIX 20260719) =========
        # Same regression guard as 1b, but for the tdata-conversion branch. A
        # real tdata fixture isn't available offline (that layer has its own
        # dedicated tools\tdata_converter_selftest.py, gated on a real fixture
        # supplied by the owner); here only the vendored crypto/parsing entry
        # point (tdata_reader.read_tdata_accounts) is monkeypatched with a fake
        # account, so the REAL convert_tdata_to_session -> session_inspector ->
        # client_factory wiring still runs end-to-end and is what's under test.
        from tdata_import import tdata_adapter as _tdata_adapter_mod
        from tdata_import.vendor.tdesktop.reader import TdataAccount as _TdataAccount

        op1c = "op-tdata-1c"
        mgr1c = "testmgrtdata"
        work_root1c = os.path.join(tmpd, "work1c")
        tdata_src_dir = os.path.join(tmpd, "tdata_src_1c", "tdata")
        os.makedirs(tdata_src_dir, exist_ok=True)
        with open(os.path.join(tdata_src_dir, "key_datas"), "wb") as f:
            f.write(b"\x00" * 16)  # content is irrelevant -- the reader is monkeypatched below

        def _tdata_1c_archive():
            p = os.path.join(tmpd, "a1c.zip")
            with zipfile.ZipFile(p, "w") as zf:
                zf.write(os.path.join(tdata_src_dir, "key_datas"), arcname="tdata/key_datas")
            return p

        fake_auth_key_1c = os.urandom(256)
        _orig_read_accounts = _tdata_adapter_mod.tdata_reader.read_tdata_accounts

        def _fake_read_accounts(tdata_dir):
            return [_TdataAccount(index=0, user_id=555000333, dc_id=2, auth_key=fake_auth_key_1c)]

        _tdata_adapter_mod.tdata_reader.read_tdata_accounts = _fake_read_accounts
        try:
            client_calls_1c = []
            client_1c = FakeClient(me=FakeUser(id=555000333))
            r1c_start = await service.start_import(
                operation_id=op1c, manager_key=mgr1c, archive_path=_tdata_1c_archive(),
                work_root=work_root1c, proxy_row=PROXY_ROW_OK,
                client_factory=_make_client_factory(client_1c, client_calls_1c),
                proxy_prober=_fake_prober_ok(), db_path=db,
            )
        finally:
            _tdata_adapter_mod.tdata_reader.read_tdata_accounts = _orig_read_accounts

        check("1c. tdata start_import ok", r1c_start.get("ok") is True, detail=str(r1c_start))
        expected_converted_path = os.path.join(work_root1c, "converted", "session.session")
        check("1c. client_factory received exactly work_root\\converted\\session.session",
              client_calls_1c == [expected_converted_path], detail=str(client_calls_1c))
        check("1c. converted session file carries the real auth_key (verified independently)",
              _read_session_auth_key(expected_converted_path) == fake_auth_key_1c)
        con_1c = sqlite3.connect(expected_converted_path)
        try:
            version_1c = con_1c.execute("SELECT version FROM version LIMIT 1").fetchone()[0]
        finally:
            con_1c.close()
        check("1c. converted session schema version == 7", version_1c == 7, detail=str(version_1c))
        check("1c. no probe.session was created anywhere under work_root",
              not any(fn == "probe.session" for _r, _d, fs in os.walk(work_root1c) for fn in fs))

        # === 2. Direct mode rejected BEFORE any Telegram client ===========
        op2 = "op-direct-2"
        client_calls2 = []
        r2 = await service.start_import(
            operation_id=op2, manager_key="testmgrdirect", archive_path=fresh_archive("a2.zip"),
            work_root=os.path.join(tmpd, "work2"), proxy_row=PROXY_ROW_DIRECT,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=1)), client_calls2),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("direct-mode: rejected", r2.get("ok") is False and r2.get("error_class") == "direct_connection_blocked",
              detail=str(r2))
        check("direct-mode: NO Telegram client was ever constructed", client_calls2 == [])

        # === 3. proxy_missing (host/port empty) ============================
        op3 = "op-proxymissing-3"
        r3 = await service.start_import(
            operation_id=op3, manager_key="testmgrproxymissing", archive_path=fresh_archive("a3.zip"),
            work_root=os.path.join(tmpd, "work3"), proxy_row=PROXY_ROW_MISSING,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=1)), []),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("proxy_missing: rejected", r3.get("ok") is False and r3.get("error_class") == "proxy_missing", detail=str(r3))

        # === 4. proxy_unavailable (prober always fails) -> no client use ==
        op4 = "op-proxydown-4"
        client_calls4 = []
        r4 = await service.start_import(
            operation_id=op4, manager_key="testmgrproxydown", archive_path=fresh_archive("a4.zip"),
            work_root=os.path.join(tmpd, "work4"), proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=1)), client_calls4),
            proxy_prober=_fake_prober_always_fails, db_path=db,
        )
        check("proxy_unavailable: rejected", r4.get("ok") is False and r4.get("error_class") == "proxy_unavailable",
              detail=str(r4))
        check("proxy_unavailable: NO Telegram client was ever constructed", client_calls4 == [])

        # === 5. session_unauthorized ========================================
        op5 = "op-unauth-5"
        unauth_client = FakeClient(authorized=False)
        r5 = await service.start_import(
            operation_id=op5, manager_key="testmgrunauth", archive_path=fresh_archive("a5.zip"),
            work_root=os.path.join(tmpd, "work5"), proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(unauth_client, []),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("session_unauthorized: rejected", r5.get("ok") is False and r5.get("error_class") == "session_unauthorized",
              detail=str(r5))

        # === 6. duplicate_telegram_account ================================
        op6 = "op-dup-6"

        def dup_check(tg_id):
            return "some_other_manager"

        r6 = await service.start_import(
            operation_id=op6, manager_key="testmgrdup", archive_path=fresh_archive("a6.zip"),
            work_root=os.path.join(tmpd, "work6"), proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=999)), []),
            proxy_prober=_fake_prober_ok(), duplicate_check=dup_check, db_path=db,
        )
        check("duplicate account: rejected", r6.get("ok") is False and r6.get("error_class") == "duplicate_telegram_account",
              detail=str(r6))

        # === 7. one active operation per manager (concurrent_operation) ===
        op7a, op7b = "op-conc-7a", "op-conc-7b"
        mgr7 = "testmgrconcurrent"
        work_root7 = os.path.join(tmpd, "work7")
        r7a = await service.start_import(
            operation_id=op7a, manager_key=mgr7, archive_path=fresh_archive("a7a.zip"),
            work_root=work_root7, proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=7001)), []),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("concurrent: first op for manager succeeds", r7a.get("ok") is True, detail=str(r7a))
        r7b = await service.start_import(
            operation_id=op7b, manager_key=mgr7, archive_path=fresh_archive("a7b.zip"),
            work_root=os.path.join(tmpd, "work7b"), proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=7002)), []),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("concurrent: second op for SAME manager rejected", r7b.get("ok") is False
              and r7b.get("error_class") == "concurrent_operation", detail=str(r7b))

        # === 8. confirm_install on wrong stage (never started) =============
        r8 = await service.confirm_install(
            operation_id="op-never-existed-8", base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=lambda k: None, wait_runtime_ready=wait_ready_ok,
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("confirm on unknown operation: rejected", r8.get("ok") is False and r8.get("error_class") == "stale_operation",
              detail=str(r8))

        # === 9. runtime failure -> rollback to prior state =================
        op9 = "op-runtimefail-9"
        mgr9 = "testmgrruntimefail"
        work_root9 = os.path.join(tmpd, "work9")
        # Pre-seed an existing "prior" session at the manager's final path to
        # prove rollback restores it exactly (byte-for-byte).
        from manager_registry import build_manager_paths
        prior_paths = build_manager_paths(tmpd, mgr9)
        os.makedirs(prior_paths["root"], exist_ok=True)
        prior_content = b"PRIOR-SESSION-BYTES-MUST-SURVIVE-ROLLBACK"
        with open(prior_paths["session_path"], "wb") as fh:
            fh.write(prior_content)

        r9 = await service.start_import(
            operation_id=op9, manager_key=mgr9, archive_path=fresh_archive("a9.zip"),
            work_root=work_root9, proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=9001)), []),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("runtimefail: start_import ok", r9.get("ok") is True, detail=str(r9))

        stopped9 = []

        async def wait_ready_fail(key):
            return False

        def stop_runtime9(key):
            stopped9.append(key)

        r9c = await service.confirm_install(
            operation_id=op9, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=lambda k: spawned.append(k), wait_runtime_ready=wait_ready_fail,
            stop_runtime=stop_runtime9, proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("runtimefail: confirm_install reports failure", r9c.get("ok") is False
              and r9c.get("error_class") == "runtime_failed", detail=str(r9c))
        check("runtimefail: stop_runtime invoked", stopped9 == [mgr9])
        with open(prior_paths["session_path"], "rb") as fh:
            restored = fh.read()
        check("runtimefail: PRIOR session restored byte-for-byte after rollback", restored == prior_content)

        # === 10. durable restart simulation: resume purely from the DB ====
        op10 = "op-restart-10"
        mgr10 = "testmgrrestart"
        r10 = await service.start_import(
            operation_id=op10, manager_key=mgr10, archive_path=fresh_archive("a10.zip"),
            work_root=os.path.join(tmpd, "work10"), proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=10001)), []),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("restart: start ok", r10.get("ok") is True)
        # Simulate "controller restart": nothing in memory carries over --
        # only a fresh DB read (get_status) informs the next step.
        resumed = service.get_status(operation_id=op10, db_path=db)
        check("restart: status recoverable purely from DB", resumed.get("stage") == "identity_verified")
        r10c = await service.confirm_install(
            operation_id=op10, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=lambda k: None, wait_runtime_ready=wait_ready_ok,
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("restart: confirm after simulated restart succeeds", r10c.get("ok") is True, detail=str(r10c))

        # === 11. cancel_import ==============================================
        op11 = "op-cancel-11"
        work_root11 = os.path.join(tmpd, "work11")
        r11 = await service.start_import(
            operation_id=op11, manager_key="testmgrcancel", archive_path=fresh_archive("a11.zip"),
            work_root=work_root11, proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=11001)), []),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("cancel: start ok", r11.get("ok") is True)
        c11 = service.cancel_import(operation_id=op11, db_path=db)
        check("cancel: cancel_import ok", c11.get("ok") is True and c11.get("status") == "cancelled", detail=str(c11))
        c11_again = service.cancel_import(operation_id=op11, db_path=db)
        check("cancel: re-cancel of already-terminal op rejected (idempotent-safe)",
              c11_again.get("ok") is False and c11_again.get("error_class") == "concurrent_operation")

        # freed slot: a NEW operation for the same manager key is now allowed
        r11b = await service.start_import(
            operation_id="op-cancel-11b", manager_key="testmgrcancel", archive_path=fresh_archive("a11b.zip"),
            work_root=os.path.join(tmpd, "work11b"), proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=11002)), []),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("cancel: manager slot freed after cancel, new op allowed", r11b.get("ok") is True, detail=str(r11b))

        # === 12. credential-material cleanup (uploaded ZIP + work_root) =====
        # Uses the REAL panel layout: runtime\tdata_import\_uploads\<token>\archive.zip
        # so cleanup_upload removes the whole per-upload token dir.
        def _uploaded_archive(token):
            upl = os.path.join(tmpd, "runtime", "tdata_import", "_uploads", token)
            os.makedirs(upl, exist_ok=True)
            p = os.path.join(upl, "archive.zip")
            _zip_session_archive(p, sess_path)
            return p, upl

        # 12a. SUCCESS: uploaded ZIP is scrubbed right after extraction; the
        # extracted work_root persists (confirm needs it) until confirm/cancel.
        up_ok, up_ok_dir = _uploaded_archive("tok-success")
        work_root12 = os.path.join(tmpd, "work12")
        r12 = await service.start_import(
            operation_id="op-clean-12", manager_key="testmgrclean12", archive_path=up_ok,
            work_root=work_root12, proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=12001)), []),
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("cleanup: start ok", r12.get("ok") is True)
        check("cleanup: uploaded ZIP token dir removed right after extraction (success)",
              not os.path.exists(up_ok_dir))
        check("cleanup: work_root still present after successful start (confirm needs it)",
              os.path.isdir(work_root12))
        # cancel scrubs the extracted work_root too
        service.cancel_import(operation_id="op-clean-12", db_path=db)
        check("cleanup: work_root removed after cancel", not os.path.isdir(work_root12))

        # 12b. FAILURE (proxy_unavailable happens BEFORE extraction): uploaded
        # ZIP and work_root must both be scrubbed by the except path.
        up_fail_pre, up_fail_pre_dir = _uploaded_archive("tok-fail-pre")
        work_root13 = os.path.join(tmpd, "work13")
        r13 = await service.start_import(
            operation_id="op-clean-13", manager_key="testmgrclean13", archive_path=up_fail_pre,
            work_root=work_root13, proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=13001)), []),
            proxy_prober=_fake_prober_always_fails, db_path=db,
        )
        check("cleanup: pre-extraction failure returns error", r13.get("ok") is False)
        check("cleanup: uploaded ZIP removed on pre-extraction failure", not os.path.exists(up_fail_pre_dir))
        check("cleanup: work_root removed on pre-extraction failure", not os.path.isdir(work_root13))

        # 12c. FAILURE AFTER extraction (identity mismatch via duplicate): the
        # extracted session material (credential) under work_root must be gone.
        up_fail_post, up_fail_post_dir = _uploaded_archive("tok-fail-post")
        work_root14 = os.path.join(tmpd, "work14")
        r14 = await service.start_import(
            operation_id="op-clean-14", manager_key="testmgrclean14", archive_path=up_fail_post,
            work_root=work_root14, proxy_row=PROXY_ROW_OK,
            client_factory=_make_client_factory(FakeClient(me=FakeUser(id=14001)), []),
            proxy_prober=_fake_prober_ok(),
            duplicate_check=lambda tg: "other_mgr", db_path=db,
        )
        check("cleanup: post-extraction failure (duplicate) returns error",
              r14.get("ok") is False and r14.get("error_class") == "duplicate_telegram_account", detail=str(r14))
        check("cleanup: uploaded ZIP removed on post-extraction failure", not os.path.exists(up_fail_post_dir))
        check("cleanup: extracted work_root (credential material) removed on post-extraction failure",
              not os.path.isdir(work_root14))

        # === 12d. cleanup_upload anchoring (defence-in-depth) ==============
        from tdata_import import cleanup as _cleanup
        # a dir whose parent is literally "_uploads" but NOT under a
        # tdata_import/_uploads structure must NOT be rmtree'd.
        rogue = os.path.join(tmpd, "somewhere", "_uploads", "x")
        os.makedirs(rogue, exist_ok=True)
        with open(os.path.join(rogue, "keep.txt"), "wb") as fh:
            fh.write(b"must survive")
        _cleanup.cleanup_upload(os.path.join(rogue, "archive.zip"))
        check("cleanup_upload refuses to rmtree a non-tdata_import _uploads dir",
              os.path.isdir(rogue) and os.path.isfile(os.path.join(rogue, "keep.txt")))

        # === 13. stale-sweep: abandoned op scrubbed + manager lock released =
        import json as _json

        def _make_workroot(name, payload=b"AUTHKEYMATERIAL"):
            wr = os.path.join(tmpd, name)
            os.makedirs(os.path.join(wr, "converted"), exist_ok=True)
            with open(os.path.join(wr, "converted", "session.session"), "wb") as fh:
                fh.write(payload)
            return wr

        # 13a. abandoned op at manager_prepared (<= identity_verified), past
        # expiry -> swept: status error/stale_operation, work_root scrubbed,
        # manager slot freed.
        sweep_wr = _make_workroot("sweep_wr_stale")
        storage.tdata_import_create("op-stale-13", "testmgrstale",
                                    expires_at="2000-01-01T00:00:00", db_path=db)
        storage.tdata_import_advance_stage("op-stale-13", "created", "manager_prepared",
                                           fields={"result_json": _json.dumps({"work_root": sweep_wr})}, db_path=db)
        # a second manager slot is held while the op is active
        blocked = storage.tdata_import_create("op-stale-13-dup", "testmgrstale", db_path=db)
        check("sweep: manager locked while stale op still active", blocked is None)

        n_swept = service.sweep_stale(now_iso="2020-01-01T00:00:00", db_path=db)
        check("sweep: at least one stale op swept", n_swept >= 1, detail=str(n_swept))
        srow = storage.tdata_import_get("op-stale-13", db_path=db)
        check("sweep: stale op moved to error/stale_operation",
              srow and srow["status"] == "error" and srow["error_class"] == "stale_operation", detail=str(srow))
        check("sweep: abandoned work_root (credential material) scrubbed", not os.path.isdir(sweep_wr))
        reop = storage.tdata_import_create("op-stale-13b", "testmgrstale", db_path=db)
        check("sweep: manager slot freed after sweep (re-import now possible)", bool(reop))

        # 13b. op mid-install (stage session_installing) past expiry is NOT
        # swept -- its session may already be on disk.
        walk_wr = _make_workroot("sweep_wr_installing")
        storage.tdata_import_create("op-installing-13c", "testmgrinstalling",
                                    expires_at="2000-01-01T00:00:00", db_path=db)
        chain = ["created", "manager_prepared", "source_assigned", "proxy_assigned", "proxy_verified",
                 "archive_uploaded", "archive_validated", "session_detected", "session_selected",
                 "session_validated", "identity_checking", "identity_verified", "session_installing"]
        for a, b in zip(chain, chain[1:]):
            storage.tdata_import_advance_stage("op-installing-13c", a, b,
                                               fields={"result_json": _json.dumps({"work_root": walk_wr})} if b == "session_installing" else None,
                                               db_path=db)
        n_swept2 = service.sweep_stale(now_iso="2020-01-01T00:00:00", db_path=db)
        irow = storage.tdata_import_get("op-installing-13c", db_path=db)
        check("sweep: mid-install op NOT swept (still processing)",
              irow and irow["status"] == "processing" and irow["stage"] == "session_installing", detail=str(irow))
        check("sweep: mid-install op work_root preserved (session may be installed)", os.path.isdir(walk_wr))

        # 13c. non-stale op (future expiry) is never swept.
        fut_wr = _make_workroot("sweep_wr_future")
        storage.tdata_import_create("op-future-13d", "testmgrfuture",
                                    expires_at="2999-01-01T00:00:00", db_path=db)
        storage.tdata_import_advance_stage("op-future-13d", "created", "manager_prepared",
                                           fields={"result_json": _json.dumps({"work_root": fut_wr})}, db_path=db)
        service.sweep_stale(now_iso="2020-01-01T00:00:00", db_path=db)
        frow2 = storage.tdata_import_get("op-future-13d", db_path=db)
        check("sweep: future-expiry op untouched", frow2 and frow2["status"] == "processing")
        check("sweep: future-expiry op work_root preserved", os.path.isdir(fut_wr))

        # === 14. TIMEOUT HARDENING (TPILOT 20260719) =======================
        # Real-default sanity check FIRST (before any monkeypatching below):
        # the configured worst-case identity leg must stay safely under
        # AdminBot's own panel-command timeout, or start_import could still
        # outlive it even with the new bounds in place.
        check("configured identity-leg timeout budget (client+probe+disconnect) "
              "stays below the AdminBot panel-command timeout",
              service.TDIMPORT_IDENTITY_LEG_MAX_TIMEOUT_SEC < service.TDIMPORT_ADMINBOT_PANEL_TIMEOUT_SEC,
              detail=f"{service.TDIMPORT_IDENTITY_LEG_MAX_TIMEOUT_SEC} vs {service.TDIMPORT_ADMINBOT_PANEL_TIMEOUT_SEC}")

        # Deterministic offline tests using tiny monkey-patched deadlines --
        # runs in well under a second instead of actually waiting out the
        # real 20/25/5s defaults. Restored in `finally` so no other test in
        # this file (or a re-run of this same suite) is affected.
        _orig_connect_to = service.TDIMPORT_CLIENT_CONNECT_TIMEOUT_SEC
        _orig_probe_to = service.TDIMPORT_IDENTITY_PROBE_TIMEOUT_SEC
        _orig_disc_to = service.TDIMPORT_DISCONNECT_TIMEOUT_SEC
        service.TDIMPORT_CLIENT_CONNECT_TIMEOUT_SEC = 0.05
        service.TDIMPORT_IDENTITY_PROBE_TIMEOUT_SEC = 0.05
        service.TDIMPORT_DISCONNECT_TIMEOUT_SEC = 0.05
        try:
            def _no_pending_extra_tasks(label):
                extra = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
                check(f"no orphan task remains after {label}", extra == [], detail=str(extra))

            async def _hanging_client_factory(session_path):
                await asyncio.sleep(999)
                return None  # pragma: no cover -- never reached, wait_for cancels first

            class HangingGetMeClient:
                def __init__(self):
                    self.disconnected = False

                async def get_me(self):
                    await asyncio.sleep(999)

                async def disconnect(self):
                    self.disconnected = True

            class HangingDisconnectClient:
                def __init__(self, me):
                    self._me = me
                    self.disconnect_called = False

                async def get_me(self):
                    return self._me

                async def disconnect(self):
                    self.disconnect_called = True
                    await asyncio.sleep(999)

            class GenericRpcErrorClient:
                async def get_me(self):
                    raise RuntimeError("simulated ServerError-like RPC failure")

                async def disconnect(self):
                    pass

            # --- 14a. client_factory never returns -------------------------
            work_root_14a = os.path.join(tmpd, "work14a")
            t0 = time.monotonic()
            r14a = await service.start_import(
                operation_id="op-timeout-14a", manager_key="testmgrtimeouta",
                archive_path=fresh_archive("a14a.zip"), work_root=work_root_14a,
                proxy_row=PROXY_ROW_OK, client_factory=_hanging_client_factory,
                proxy_prober=_fake_prober_ok(), db_path=db,
            )
            elapsed_14a = time.monotonic() - t0
            check("14a. hung client_factory -> operation becomes error", r14a.get("ok") is False, detail=str(r14a))
            check("14a. error_class is runtime_failed", r14a.get("error_class") == "runtime_failed", detail=str(r14a))
            check("14a. safe error text, no secrets", "timed out" in (r14a.get("error_text") or ""))
            check("14a. bounded by the tiny timeout, not the real 999s hang", elapsed_14a < 5)
            check("14a. work_root cleaned up", not os.path.isdir(work_root_14a))
            check("14a. no probe.session leaked (old-defect artifact)",
                  not os.path.isfile(os.path.join(work_root_14a, "probe.session")))
            row_14a = storage.tdata_import_get("op-timeout-14a", db_path=db)
            check("14a. cleanup_done is truthful (work_root really gone)",
                  row_14a and int(row_14a.get("cleanup_done") or 0) == 1, detail=str(row_14a))
            reop_14a = storage.tdata_import_create("op-timeout-14a-retry", "testmgrtimeouta", db_path=db)
            check("14a. manager slot released, retry allowed", bool(reop_14a))
            _no_pending_extra_tasks("14a (hung client_factory)")

            # --- 14a2. client_factory raises immediately (not a hang) ------
            work_root_14a2 = os.path.join(tmpd, "work14a2")

            async def _raising_client_factory(session_path):
                raise ConnectionError("simulated dead proxy socket")

            r14a2 = await service.start_import(
                operation_id="op-timeout-14a2", manager_key="testmgrtimeouta2",
                archive_path=fresh_archive("a14a2.zip"), work_root=work_root_14a2,
                proxy_row=PROXY_ROW_OK, client_factory=_raising_client_factory,
                proxy_prober=_fake_prober_ok(), db_path=db,
            )
            check("14a2. client_factory exception -> operation becomes error", r14a2.get("ok") is False, detail=str(r14a2))
            check("14a2. error_class is runtime_failed", r14a2.get("error_class") == "runtime_failed", detail=str(r14a2))
            check("14a2. work_root cleaned up", not os.path.isdir(work_root_14a2))
            row_14a2 = storage.tdata_import_get("op-timeout-14a2", db_path=db)
            check("14a2. cleanup_done is truthful", row_14a2 and int(row_14a2.get("cleanup_done") or 0) == 1)
            reop_14a2 = storage.tdata_import_create("op-timeout-14a2-retry", "testmgrtimeouta2", db_path=db)
            check("14a2. manager slot released, retry allowed", bool(reop_14a2))

            # --- 14b. client connects, get_me never returns -----------------
            work_root_14b = os.path.join(tmpd, "work14b")
            client_14b = HangingGetMeClient()
            t0 = time.monotonic()
            r14b = await service.start_import(
                operation_id="op-timeout-14b", manager_key="testmgrtimeoutb",
                archive_path=fresh_archive("a14b.zip"), work_root=work_root_14b,
                proxy_row=PROXY_ROW_OK, client_factory=_make_client_factory(client_14b, []),
                proxy_prober=_fake_prober_ok(), db_path=db,
            )
            elapsed_14b = time.monotonic() - t0
            check("14b. hung get_me -> operation becomes error", r14b.get("ok") is False, detail=str(r14b))
            check("14b. error_class is runtime_failed", r14b.get("error_class") == "runtime_failed", detail=str(r14b))
            check("14b. safe error text, no secrets", "timed out" in (r14b.get("error_text") or ""))
            check("14b. disconnect was still attempted despite the hang", client_14b.disconnected is True)
            check("14b. bounded by the tiny timeout, not the real 999s hang", elapsed_14b < 5)
            check("14b. work_root cleaned up", not os.path.isdir(work_root_14b))
            check("14b. no probe.session leaked (old-defect artifact)",
                  not os.path.isfile(os.path.join(work_root_14b, "probe.session")))
            row_14b = storage.tdata_import_get("op-timeout-14b", db_path=db)
            check("14b. cleanup_done is truthful (work_root really gone)",
                  row_14b and int(row_14b.get("cleanup_done") or 0) == 1, detail=str(row_14b))
            reop_14b = storage.tdata_import_create("op-timeout-14b-retry", "testmgrtimeoutb", db_path=db)
            check("14b. manager slot released, retry allowed", bool(reop_14b))
            _no_pending_extra_tasks("14b (hung get_me)")

            # --- 14c. get_me succeeds, disconnect never returns -------------
            work_root_14c = os.path.join(tmpd, "work14c")
            client_14c = HangingDisconnectClient(FakeUser(id=555000222))
            t0 = time.monotonic()
            r14c = await service.start_import(
                operation_id="op-timeout-14c", manager_key="testmgrtimeoutc",
                archive_path=fresh_archive("a14c.zip"), work_root=work_root_14c,
                proxy_row=PROXY_ROW_OK, client_factory=_make_client_factory(client_14c, []),
                proxy_prober=_fake_prober_ok(), db_path=db,
            )
            elapsed_14c = time.monotonic() - t0
            check("14c. successful identity result preserved despite disconnect hang",
                  r14c.get("ok") is True and r14c.get("tg_user_id") == 555000222, detail=str(r14c))
            check("14c. disconnect timeout did not hang the operation", elapsed_14c < 5)
            check("14c. disconnect was attempted (even though it never completed)", client_14c.disconnect_called is True)
            _no_pending_extra_tasks("14c (hung disconnect)")

            # --- 14d. get_me returns None -> still session_unauthorized (unchanged
            # by the timeout hardening, even with tiny deadlines active) --------
            work_root_14d = os.path.join(tmpd, "work14d")
            r14d = await service.start_import(
                operation_id="op-timeout-14d", manager_key="testmgrtimeoutd",
                archive_path=fresh_archive("a14d.zip"), work_root=work_root_14d,
                proxy_row=PROXY_ROW_OK, client_factory=_make_client_factory(FakeClient(authorized=False), []),
                proxy_prober=_fake_prober_ok(), db_path=db,
            )
            check("14d. get_me-None still classified session_unauthorized (not runtime_failed)",
                  r14d.get("ok") is False and r14d.get("error_class") == "session_unauthorized", detail=str(r14d))

            # --- 14e. get_me raises a generic/ServerError-like RPC exception ->
            # runtime_failed, NOT session_unauthorized -----------------------
            work_root_14e = os.path.join(tmpd, "work14e")
            r14e = await service.start_import(
                operation_id="op-timeout-14e", manager_key="testmgrtimeoute",
                archive_path=fresh_archive("a14e.zip"), work_root=work_root_14e,
                proxy_row=PROXY_ROW_OK, client_factory=_make_client_factory(GenericRpcErrorClient(), []),
                proxy_prober=_fake_prober_ok(), db_path=db,
            )
            check("14e. generic RPC-like error -> runtime_failed, not session_unauthorized",
                  r14e.get("ok") is False and r14e.get("error_class") == "runtime_failed", detail=str(r14e))
            check("14e. work_root cleaned up", not os.path.isdir(work_root_14e))
            row_14e = storage.tdata_import_get("op-timeout-14e", db_path=db)
            check("14e. cleanup_done is truthful", row_14e and int(row_14e.get("cleanup_done") or 0) == 1)
            reop_14e = storage.tdata_import_create("op-timeout-14e-retry", "testmgrtimeoute", db_path=db)
            check("14e. manager slot released, retry allowed", bool(reop_14e))

            # === TEST C: EMPTY SESSION DEFENSE (does not rely only on get_me) ==
            # Independent proof that _make_client_factory's own verification (used
            # throughout this file) actually fires -- i.e. it is not a no-op --
            # by handing it a real, on-disk, EMPTY-auth_key session (the exact
            # shape of the original probe.session bug) alongside a FakeClient
            # whose get_me() is hardcoded to SUCCEED. A naive test relying only on
            # get_me() would pass here; this one must fail loudly instead.
            empty_session_path = os.path.join(tmpd, "empty_defense_check.session")
            _build_synthetic_session_schema_only = sqlite3.connect(empty_session_path)
            try:
                _build_synthetic_session_schema_only.execute("CREATE TABLE version (version integer primary key)")
                _build_synthetic_session_schema_only.execute(
                    "CREATE TABLE sessions (dc_id integer primary key, server_address text,"
                    " port integer, auth_key blob, takeout_id integer)")
                _build_synthetic_session_schema_only.execute("INSERT INTO version VALUES (7)")
                _build_synthetic_session_schema_only.execute(
                    "INSERT INTO sessions VALUES (?,?,?,?,NULL)", (2, "149.154.167.51", 443, b""))
                _build_synthetic_session_schema_only.commit()
            finally:
                _build_synthetic_session_schema_only.close()
            defense_factory = _make_client_factory(FakeClient(me=FakeUser(id=999999999)), [])
            defense_triggered = False
            try:
                await defense_factory(empty_session_path)
            except AssertionError:
                defense_triggered = True
            check("C. empty-auth_key session is rejected by the factory's own verification "
                  "even though get_me() would have succeeded", defense_triggered)
        finally:
            service.TDIMPORT_CLIENT_CONNECT_TIMEOUT_SEC = _orig_connect_to
            service.TDIMPORT_IDENTITY_PROBE_TIMEOUT_SEC = _orig_probe_to
            service.TDIMPORT_DISCONNECT_TIMEOUT_SEC = _orig_disc_to

        # === TEST E: CANCEL DURING IDENTITY_CHECKING (mid-flight persisted paths)
        # Simulates the exact scenario the early-persistence fix (service.py's
        # start_import) protects against: a controller crash/admin-cancel while
        # status=processing, stage=identity_checking, with work_root/
        # session_source_path already durable but identity never completed.
        # Proves cancel_import finds and scrubs the REAL on-disk credential
        # files (not a silent no-op on an empty result_json) and only marks
        # cleanup_done=1 because the deletion actually succeeded.
        import json as _json

        op_e = "op-cancel-e"
        mgr_e = "testmgrcancele"
        work_root_e = os.path.join(tmpd, "work_e")
        session_src_e = os.path.join(work_root_e, "converted", "session.session")
        os.makedirs(os.path.dirname(session_src_e), exist_ok=True)
        _build_synthetic_session(session_src_e)

        storage.tdata_import_create(op_e, mgr_e, db_path=db)
        chain_e = ["created", "manager_prepared", "source_assigned", "proxy_assigned", "proxy_verified",
                   "archive_uploaded", "archive_validated", "session_detected", "tdata_converted",
                   "session_validated", "identity_checking"]
        for a, b in zip(chain_e, chain_e[1:]):
            fields = {"result_json": _json.dumps({
                "work_root": work_root_e, "session_source_path": session_src_e,
            })} if b == "identity_checking" else None
            storage.tdata_import_advance_stage(op_e, a, b, fields=fields, db_path=db)

        row_e_before = storage.tdata_import_get(op_e, db_path=db)
        check("E. setup: op reached identity_checking with persisted paths",
              row_e_before and row_e_before["stage"] == "identity_checking"
              and row_e_before["status"] == "processing", detail=str(row_e_before))
        check("E. setup: credential file really exists before cancel", os.path.isfile(session_src_e))

        r_e = service.cancel_import(operation_id=op_e, db_path=db)
        check("E. cancel_import ok", r_e.get("ok") is True, detail=str(r_e))
        row_e_after = storage.tdata_import_get(op_e, db_path=db)
        check("E. status becomes cancelled", row_e_after and row_e_after["status"] == "cancelled", detail=str(row_e_after))
        check("E. work_root removed", not os.path.isdir(work_root_e))
        check("E. converted session removed", not os.path.isfile(session_src_e))
        check("E. cleanup_done=1 ONLY because deletion actually succeeded",
              row_e_after and int(row_e_after.get("cleanup_done") or 0) == 1, detail=str(row_e_after))
        reop_e = storage.tdata_import_create("op-cancel-e-retry", mgr_e, db_path=db)
        check("E. manager slot released, retry allowed", bool(reop_e))

        # === TEST F: STALE SWEEP AT IDENTITY_CHECKING (mid-flight persisted paths)
        op_f = "op-stale-f"
        mgr_f = "testmgrstalef"
        work_root_f = os.path.join(tmpd, "work_f")
        session_src_f = os.path.join(work_root_f, "converted", "session.session")
        os.makedirs(os.path.dirname(session_src_f), exist_ok=True)
        _build_synthetic_session(session_src_f)

        storage.tdata_import_create(op_f, mgr_f, expires_at="2000-01-01T00:00:00", db_path=db)
        chain_f = ["created", "manager_prepared", "source_assigned", "proxy_assigned", "proxy_verified",
                   "archive_uploaded", "archive_validated", "session_detected", "tdata_converted",
                   "session_validated", "identity_checking"]
        for a, b in zip(chain_f, chain_f[1:]):
            fields = {"result_json": _json.dumps({
                "work_root": work_root_f, "session_source_path": session_src_f,
            })} if b == "identity_checking" else None
            storage.tdata_import_advance_stage(op_f, a, b, fields=fields, db_path=db)

        check("F. setup: credential file really exists before sweep", os.path.isfile(session_src_f))
        n_swept_f = service.sweep_stale(now_iso="2020-01-01T00:00:00", db_path=db)
        check("F. at least one operation swept", n_swept_f >= 1, detail=str(n_swept_f))
        row_f = storage.tdata_import_get(op_f, db_path=db)
        check("F. status/error_class -> error/stale_operation",
              row_f and row_f["status"] == "error" and row_f["error_class"] == "stale_operation", detail=str(row_f))
        check("F. work_root removed", not os.path.isdir(work_root_f))
        check("F. converted session removed", not os.path.isfile(session_src_f))
        check("F. cleanup_done=1 ONLY because deletion actually succeeded",
              row_f and int(row_f.get("cleanup_done") or 0) == 1, detail=str(row_f))
        reop_f = storage.tdata_import_create("op-stale-f-retry", mgr_f, db_path=db)
        check("F. manager slot released, retry allowed", bool(reop_f))

    asyncio.run(run())

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL FLOW SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
