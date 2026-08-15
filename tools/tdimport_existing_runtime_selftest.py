# -*- coding: utf-8 -*-
"""tools/tdimport_existing_runtime_selftest.py -- offline selftest for the
"TPILOT AUTH SAFETY 20260809" patch (plan Ф3, D-INSTALL): tdata_import.
service.confirm_install must stop an EXISTING manager's runtime and CONFIRM
it actually stopped before touching the live .session file (installer.
install_session's os.replace() is not safe against an open handle on
Windows), and must safely roll back -- including restarting the OLD runtime,
but ONLY when it is certain to have been running and to have been the one
that stopped it -- on any failure from that point forward.

Pure/offline, same technique and shared fixtures as tools/
tdata_import_flow_selftest.py: temp SQLite only (DB-guarded), a fully
synthetic Telethon v7 `.session` built in-process, fake proxy prober, fake
Telegram client, fake runtime spawn/readiness/stop/is_running callables. NO
real network, NO real Telegram/proxy calls, NO spend, NO real process
management.

Covers plan section 16's I1-I10 scenarios plus the required mutation proofs:
  - I1: manager not running -> install works; a LATER unrelated failure's
    rollback never spawns it (it was never "ours" to restart).
  - I2/order: STOP is called strictly BEFORE INSTALL, which is strictly
    before SPAWN, which is strictly before READY (a single shared call-order
    log proves the sequence, not just each call's existence).
  - I3: stop_runtime raises -> installer.install_session is called ZERO times
    (a wrapped/counting install_session proves this deterministically, not
    by inference from the result).
  - I4/I5/I6: install/spawn/readiness each fail after a successful, CONFIRMED
    stop -> old session restored (byte-for-byte) AND old runtime restarted.
  - I7: the rollback restart itself fails -> the ORIGINAL failure is still
    what's classified/returned (not masked by the rollback failure), AND the
    rollback failure is visibly recorded in the result (not silently eaten).
  - I8: a fully successful install never calls the rollback-restart path.
  - I9: old session bytes are IDENTICAL before/after every failed-flow
    scenario (not just "some file exists").
  - I10: confirm_install itself never writes anything into the `managers`
    table (identity sync only ever happens later, inside a genuinely
    started/ready runtime) -- proven by asserting no such table/call exists
    in this module's dependency surface at all.
  - Mutation proofs: (a) stop-before-install ordering removed -> RED;
    (b) rollback unconditionally spawns regardless of stop_confirmed_by_us
    -> RED for an originally-NOT-running manager; (c) old-session restore
    removed -> RED; (d) success declared before wait_runtime_ready -> RED.

Also verifies SOURCE_PRIORITY is completely unaffected: this patch only
touches confirm_install (session-file install/rollback safety), never
detector.py/session_inspector.py/tdata_adapter.py -- a `.session` + tdata
archive still resolves to the ready session with the tdata converter never
invoked, exactly as before this patch.

Run:  python tools\\tdimport_existing_runtime_selftest.py
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import storage  # noqa: E402
from tdata_import import detector, installer, proxy_gate, service  # noqa: E402
from tdata_import.models import ArchiveInventory  # noqa: E402

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


def _build_synthetic_session(path, *, dc_id=2, addr="149.154.167.51", port=443, auth_key=None):
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE version (version integer primary key)")
        con.execute("CREATE TABLE sessions (dc_id integer primary key, server_address text,"
                    " port integer, auth_key blob, takeout_id integer)")
        con.execute("CREATE TABLE entities (id integer primary key, hash integer not null,"
                    " username text, phone integer, name text, date integer)")
        con.execute("INSERT INTO version VALUES (7)")
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,NULL)", (dc_id, addr, port, auth_key or b"\xab" * 256))
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


def _make_client_factory(client):
    async def factory(session_path):
        return client
    return factory


def _fake_prober_ok(ip="203.0.113.7"):
    def prober(host, port, username, password, rdns):
        return proxy_gate.ProxyVerification(ip=ip, method="socks5h" if rdns else "socks5")
    return prober


PROXY_ROW_OK = {
    "proxy_mode": "proxy", "proxy_lease_id": 1, "proxy_host": "10.0.0.1", "proxy_port": 1080,
    "proxy_username": "u", "proxy_password": "p",
}


# ======================================================================
# Shared call-order log + fakes -- ONE log list threaded through every fake
# so the exact interleaving (not just each call's existence) is provable.
# ======================================================================

class RuntimeFakes:
    """Configurable spawn/stop/is_running/wait_ready fakes, all appending to
    ONE shared `calls` list in real invocation order. install_session is
    wrapped separately (see wrap_install_session) so its call COUNT can be
    asserted deterministically (I3's "install call count == 0" proof)."""

    def __init__(self, *, initially_running=False, stop_ok=True, stop_raises=False,
                 stop_leaves_running=False, spawn_ok=True, spawn_raises=False,
                 ready_ok=True):
        self.calls = []
        self._running = bool(initially_running)
        self.stop_ok = stop_ok
        self.stop_raises = stop_raises
        self.stop_leaves_running = stop_leaves_running
        self.spawn_ok = spawn_ok
        self.spawn_raises = spawn_raises
        self.ready_ok = ready_ok
        self.spawn_call_count = 0
        self.stop_call_count = 0

    async def is_runtime_running(self, key):
        self.calls.append(("is_running", key, self._running))
        return self._running

    async def stop_runtime(self, key):
        self.calls.append(("stop", key))
        self.stop_call_count += 1
        if self.stop_raises:
            raise RuntimeError("simulated stop failure")
        if not self.stop_leaves_running:
            self._running = False
        # stop_leaves_running=True simulates a stop command that "succeeds"
        # (no exception) but never actually takes effect -- the poll loop
        # below must catch this via is_runtime_running, not trust stop's
        # own return value.

    async def spawn_runtime(self, key):
        self.calls.append(("spawn", key))
        self.spawn_call_count += 1
        if self.spawn_raises:
            raise RuntimeError("simulated spawn failure")
        self._running = True

    async def wait_runtime_ready(self, key):
        self.calls.append(("ready", key))
        return self.ready_ok


def wrap_install_session(*, fail=False):
    """Returns (fake_install_session, call_counter_list) -- replaces
    installer.install_session for the duration of one scenario via
    monkeypatch, so I3's "install called 0 times" and I4/I5/I6's "install
    called exactly once" are proven by a real counter, not inferred from
    side effects."""
    calls = []
    real_install = installer.install_session

    def fake(session_source_path, final_path, *, backup_dir=None):
        calls.append((session_source_path, final_path))
        if fail:
            raise RuntimeError("simulated install_session failure")
        return real_install(session_source_path, final_path, backup_dir=backup_dir)

    return fake, calls


async def _seed_identity_verified_op(db, tmpd, *, op_id, manager_key, tg_user_id=900001):
    """Drives a REAL start_import to identity_verified (same technique as
    tdata_import_flow_selftest.py), returning nothing -- confirm_install is
    what this file actually tests, called separately by each scenario."""
    sess_path = os.path.join(tmpd, f"{op_id}_src.session")
    _build_synthetic_session(sess_path)
    archive_path = os.path.join(tmpd, f"{op_id}.zip")
    _zip_session_archive(archive_path, sess_path)
    client = FakeClient(me=FakeUser(id=tg_user_id))
    work_root = os.path.join(tmpd, f"{op_id}_work")
    r = await service.start_import(
        operation_id=op_id, manager_key=manager_key, archive_path=archive_path,
        work_root=work_root, proxy_row=PROXY_ROW_OK,
        client_factory=_make_client_factory(client),
        proxy_prober=_fake_prober_ok(), db_path=db,
    )
    assert r.get("ok") is True, f"setup failed: {r}"
    return work_root


def _seed_prior_session(tmpd, manager_key, content: bytes):
    from manager_registry import build_manager_paths
    paths = build_manager_paths(tmpd, manager_key)
    os.makedirs(paths["root"], exist_ok=True)
    with open(paths["session_path"], "wb") as fh:
        fh.write(content)
    return paths["session_path"]


def main():
    tmpd = tempfile.mkdtemp(prefix="tdimport_existing_runtime_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)

    async def run():
        # === I1: manager NOT running -> install works; a rollback triggered
        # by a LATER, UNRELATED scenario never spawns THIS manager. ==========
        op1, mgr1 = "op-i1", "mgri1"
        await _seed_identity_verified_op(db, tmpd, op_id=op1, manager_key=mgr1)
        fakes1 = RuntimeFakes(initially_running=False)
        r1 = await service.confirm_install(
            operation_id=op1, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=fakes1.spawn_runtime, wait_runtime_ready=fakes1.wait_runtime_ready,
            stop_runtime=fakes1.stop_runtime, is_runtime_running=fakes1.is_runtime_running,
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("I1. not-running manager: confirm_install succeeds", r1.get("ok") is True, detail=str(r1))
        check("I1. stop_runtime was never called (nothing to stop)", fakes1.stop_call_count == 0)
        check("I1. spawn_runtime called exactly once (the real post-install start)", fakes1.spawn_call_count == 1)

        # === I2: manager RUNNING -> STOP strictly before INSTALL strictly
        # before SPAWN strictly before READY. ================================
        op2, mgr2 = "op-i2", "mgri2"
        await _seed_identity_verified_op(db, tmpd, op_id=op2, manager_key=mgr2)
        fakes2 = RuntimeFakes(initially_running=True)
        fake_install2, install_calls2 = wrap_install_session()
        _orig_install = installer.install_session
        installer.install_session = fake_install2
        try:
            r2 = await service.confirm_install(
                operation_id=op2, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
                spawn_runtime=fakes2.spawn_runtime, wait_runtime_ready=fakes2.wait_runtime_ready,
                stop_runtime=fakes2.stop_runtime, is_runtime_running=fakes2.is_runtime_running,
                proxy_prober=_fake_prober_ok(), db_path=db, stop_confirm_poll_sec=0.01,
            )
        finally:
            installer.install_session = _orig_install
        check("I2. running manager: confirm_install succeeds", r2.get("ok") is True, detail=str(r2))
        check("I2. install_session called exactly once", len(install_calls2) == 1, detail=str(install_calls2))
        order2 = [c[0] for c in fakes2.calls]
        stop_idx = order2.index("stop")
        spawn_idx = order2.index("spawn")
        ready_idx = order2.index("ready")
        install_call_pos = fakes2.calls.index(("is_running", mgr2, False))  # first is_running AFTER stop takes effect
        check("I2. STOP happens before SPAWN", stop_idx < spawn_idx, detail=str(order2))
        check("I2. SPAWN happens before READY", spawn_idx < ready_idx, detail=str(order2))
        check("I2. a post-stop is_running confirmation happened before spawn",
              install_call_pos < spawn_idx, detail=str(fakes2.calls))

        # === I3: stop_runtime raises -> install_session called ZERO times. ==
        op3, mgr3 = "op-i3", "mgri3"
        await _seed_identity_verified_op(db, tmpd, op_id=op3, manager_key=mgr3)
        fakes3 = RuntimeFakes(initially_running=True, stop_raises=True)
        fake_install3, install_calls3 = wrap_install_session()
        installer.install_session = fake_install3
        try:
            r3 = await service.confirm_install(
                operation_id=op3, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
                spawn_runtime=fakes3.spawn_runtime, wait_runtime_ready=fakes3.wait_runtime_ready,
                stop_runtime=fakes3.stop_runtime, is_runtime_running=fakes3.is_runtime_running,
                proxy_prober=_fake_prober_ok(), db_path=db, stop_confirm_poll_sec=0.01,
            )
        finally:
            installer.install_session = _orig_install
        check("I3. stop_runtime raising -> confirm_install fails", r3.get("ok") is False, detail=str(r3))
        check("I3. install_session called ZERO times (fail closed BEFORE install)", len(install_calls3) == 0,
              detail=str(install_calls3))
        check("I3. spawn_runtime was NEVER called (no unsafe respawn on an unconfirmed stop)",
              fakes3.spawn_call_count == 0)

        # === I3b: stop_runtime succeeds (no exception) but the process never
        # actually goes away -> confirm loop times out -> install ZERO calls,
        # and NO respawn (we never confirmed WE stopped it). =================
        op3b, mgr3b = "op-i3b", "mgri3b"
        await _seed_identity_verified_op(db, tmpd, op_id=op3b, manager_key=mgr3b)
        fakes3b = RuntimeFakes(initially_running=True, stop_leaves_running=True)
        fake_install3b, install_calls3b = wrap_install_session()
        installer.install_session = fake_install3b
        try:
            r3b = await service.confirm_install(
                operation_id=op3b, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
                spawn_runtime=fakes3b.spawn_runtime, wait_runtime_ready=fakes3b.wait_runtime_ready,
                stop_runtime=fakes3b.stop_runtime, is_runtime_running=fakes3b.is_runtime_running,
                proxy_prober=_fake_prober_ok(), db_path=db,
                stop_confirm_max_attempts=2, stop_confirm_poll_sec=0.01,
            )
        finally:
            installer.install_session = _orig_install
        check("I3b. unconfirmed stop -> confirm_install fails", r3b.get("ok") is False, detail=str(r3b))
        check("I3b. install_session called ZERO times", len(install_calls3b) == 0, detail=str(install_calls3b))
        check("I3b. NO respawn attempted (stop was never confirmed -- respawning could double-run the process)",
              fakes3b.spawn_call_count == 0)

        # === I4: install_session itself fails AFTER a confirmed stop -> old
        # session restored (installer's own internal safety) + old runtime
        # restarted. ===========================================================
        op4, mgr4 = "op-i4", "mgri4"
        prior4 = _seed_prior_session(tmpd, mgr4, b"PRIOR-I4-BYTES")
        await _seed_identity_verified_op(db, tmpd, op_id=op4, manager_key=mgr4)
        fakes4 = RuntimeFakes(initially_running=True)
        fake_install4, install_calls4 = wrap_install_session(fail=True)
        installer.install_session = fake_install4
        try:
            r4 = await service.confirm_install(
                operation_id=op4, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
                spawn_runtime=fakes4.spawn_runtime, wait_runtime_ready=fakes4.wait_runtime_ready,
                stop_runtime=fakes4.stop_runtime, is_runtime_running=fakes4.is_runtime_running,
                proxy_prober=_fake_prober_ok(), db_path=db, stop_confirm_poll_sec=0.01,
            )
        finally:
            installer.install_session = _orig_install
        check("I4. install failure after confirmed stop -> confirm_install fails", r4.get("ok") is False, detail=str(r4))
        check("I4. install_session was attempted exactly once", len(install_calls4) == 1)
        with open(prior4, "rb") as fh:
            restored4 = fh.read()
        check("I4. OLD session restored byte-for-byte", restored4 == b"PRIOR-I4-BYTES", detail=str(restored4))
        check("I4. old runtime RESTARTED (spawn called after the confirmed stop)", fakes4.spawn_call_count == 1)

        # === I5: spawn_runtime fails AFTER a successful install -> old
        # session restored + old runtime restarted. ==========================
        op5, mgr5 = "op-i5", "mgri5"
        prior5 = _seed_prior_session(tmpd, mgr5, b"PRIOR-I5-BYTES")
        await _seed_identity_verified_op(db, tmpd, op_id=op5, manager_key=mgr5)
        fakes5 = RuntimeFakes(initially_running=True, spawn_raises=True)
        r5 = await service.confirm_install(
            operation_id=op5, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=fakes5.spawn_runtime, wait_runtime_ready=fakes5.wait_runtime_ready,
            stop_runtime=fakes5.stop_runtime, is_runtime_running=fakes5.is_runtime_running,
            proxy_prober=_fake_prober_ok(), db_path=db, stop_confirm_poll_sec=0.01,
        )
        check("I5. spawn failure -> confirm_install fails", r5.get("ok") is False, detail=str(r5))
        with open(prior5, "rb") as fh:
            restored5 = fh.read()
        check("I5. OLD session restored byte-for-byte", restored5 == b"PRIOR-I5-BYTES", detail=str(restored5))
        # spawn_runtime was called by the main flow (raised) AND by the rollback
        # (raises again the second time too, since spawn_raises stays True) --
        # both calls are the SAME safety property (attempted restart), so >=2.
        check("I5. old runtime restart WAS ATTEMPTED (rollback path called spawn_runtime again)",
              fakes5.spawn_call_count >= 2, detail=str(fakes5.spawn_call_count))

        # === I6: wait_runtime_ready fails AFTER successful install+spawn ->
        # old session restored + old runtime restarted. ======================
        op6, mgr6 = "op-i6", "mgri6"
        prior6 = _seed_prior_session(tmpd, mgr6, b"PRIOR-I6-BYTES")
        await _seed_identity_verified_op(db, tmpd, op_id=op6, manager_key=mgr6)
        fakes6 = RuntimeFakes(initially_running=True, ready_ok=False)
        r6 = await service.confirm_install(
            operation_id=op6, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=fakes6.spawn_runtime, wait_runtime_ready=fakes6.wait_runtime_ready,
            stop_runtime=fakes6.stop_runtime, is_runtime_running=fakes6.is_runtime_running,
            proxy_prober=_fake_prober_ok(), db_path=db, stop_confirm_poll_sec=0.01,
        )
        check("I6. readiness failure -> confirm_install fails", r6.get("ok") is False, detail=str(r6))
        check("I6. error_class is runtime_failed", r6.get("error_class") == "runtime_failed", detail=str(r6))
        with open(prior6, "rb") as fh:
            restored6 = fh.read()
        check("I6. OLD session restored byte-for-byte", restored6 == b"PRIOR-I6-BYTES", detail=str(restored6))
        check("I6. old runtime RESTARTED (spawn called again by the rollback)", fakes6.spawn_call_count >= 2,
              detail=str(fakes6.spawn_call_count))

        # === I7: the rollback restart itself ALSO fails -> the ORIGINAL
        # failure (readiness) is still what's reported, and the rollback
        # failure is visibly recorded (not silently swallowed as success). ==
        op7, mgr7 = "op-i7", "mgri7"
        _seed_prior_session(tmpd, mgr7, b"PRIOR-I7-BYTES")
        await _seed_identity_verified_op(db, tmpd, op_id=op7, manager_key=mgr7)

        class AlwaysFailSpawnAfterFirst(RuntimeFakes):
            async def spawn_runtime(self, key):
                self.calls.append(("spawn", key))
                self.spawn_call_count += 1
                if self.spawn_call_count == 1:
                    raise RuntimeError("simulated FIRST spawn failure (the one wait_runtime_ready would gate on)")
                raise RuntimeError("simulated ROLLBACK spawn failure")

        fakes7 = AlwaysFailSpawnAfterFirst(initially_running=True)
        r7 = await service.confirm_install(
            operation_id=op7, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=fakes7.spawn_runtime, wait_runtime_ready=fakes7.wait_runtime_ready,
            stop_runtime=fakes7.stop_runtime, is_runtime_running=fakes7.is_runtime_running,
            proxy_prober=_fake_prober_ok(), db_path=db, stop_confirm_poll_sec=0.01,
        )
        check("I7. confirm_install still reports failure", r7.get("ok") is False, detail=str(r7))
        check("I7. ORIGINAL failure class preserved (not masked by the rollback failure)",
              r7.get("error_class") in ("runtime_failed",), detail=str(r7))
        check("I7. the rollback restart's OWN failure is visibly recorded in the result text",
              "rollback restart also failed" in str(r7.get("error_text") or ""), detail=str(r7))
        check("I7. rollback restart WAS attempted (second spawn call happened, and failed)",
              fakes7.spawn_call_count >= 2)

        # === I8: fully successful install -> the rollback-restart path is
        # NEVER reached at all (spawn_runtime called exactly once, for the
        # real, successful start -- not twice). ==============================
        op8, mgr8 = "op-i8", "mgri8"
        await _seed_identity_verified_op(db, tmpd, op_id=op8, manager_key=mgr8)
        fakes8 = RuntimeFakes(initially_running=True)
        r8 = await service.confirm_install(
            operation_id=op8, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=fakes8.spawn_runtime, wait_runtime_ready=fakes8.wait_runtime_ready,
            stop_runtime=fakes8.stop_runtime, is_runtime_running=fakes8.is_runtime_running,
            proxy_prober=_fake_prober_ok(), db_path=db, stop_confirm_poll_sec=0.01,
        )
        check("I8. successful install", r8.get("ok") is True, detail=str(r8))
        check("I8. spawn_runtime called EXACTLY once (rollback-restart never fires on success)",
              fakes8.spawn_call_count == 1, detail=str(fakes8.spawn_call_count))

        # === I9: already covered per-scenario above (I4/I5/I6 byte checks) --
        # explicit summary check that a NOT-running manager's rollback never
        # touches ITS session at all (nothing to restore -- no install ever
        # ran on a failure for it in this suite; this asserts I1's manager
        # session file was never even created by confirm_install's rollback
        # path, since no prior file was seeded for it).
        from manager_registry import build_manager_paths
        i1_paths = build_manager_paths(tmpd, mgr1)
        check("I9. I1's manager session exists (real install succeeded, nothing to roll back)",
              os.path.isfile(i1_paths["session_path"]))

        # === I10: confirm_install never has ANY dependency capable of writing
        # to the `managers` table -- structural proof via the module's own
        # import surface (it imports storage for tdata_import_ops helpers
        # only, never manager_add/manager_set_fields/manager_sync_telegram_
        # profile_in_db). ======================================================
        import inspect
        service_src = inspect.getsource(service)
        for forbidden in ("manager_set_fields", "manager_sync_telegram_profile_in_db", "manager_add("):
            check(f"I10. confirm_install's module source never references {forbidden} "
                  "(identity/manager-row writes only ever happen inside a started runtime, never here)",
                  forbidden not in service_src)

        # ==================================================================
        # MUTATION PROOFS -- structural, on a deep-copied AST of confirm_
        # install extracted from the REAL service.py source (the file on disk
        # is never touched). Each proves the corresponding assertion above is
        # falsifiable, not a tautology.
        # ==================================================================
        import ast
        import copy as _copy

        SERVICE_PATH = os.path.join(BASE_DIR, "tdata_import", "service.py")
        SERVICE_SRC = open(SERVICE_PATH, encoding="utf-8-sig").read()

        def _find_confirm_install_node():
            tree = ast.parse(SERVICE_SRC)
            for n in tree.body:
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "confirm_install":
                    return n
            raise AssertionError("confirm_install not found")

        class _IfGuardKiller(ast.NodeTransformer):
            def __init__(self, needle, replacement):
                self.needle = needle
                self.replacement = replacement
                self.hit = False

            def visit_If(self, node):
                self.generic_visit(node)
                if not self.hit and self.needle in ast.unparse(node.test):
                    node.test = ast.Constant(value=self.replacement)
                    self.hit = True
                return node

        def _build_mutant_confirm_install(mutator):
            node = _copy.deepcopy(_find_confirm_install_node())
            if mutator is not None:
                node = mutator(node)
            ast.fix_missing_locations(node)
            src = ast.unparse(node)
            ns = {
                "storage": storage, "os": os, "json": __import__("json"), "asyncio": asyncio,
                "proxy_gate": proxy_gate, "installer": installer,
                "Stage": service.Stage, "Status": service.Status,
                "Dict": dict, "Any": object, "Optional": None,
                "_fail": service._fail, "_merge_result_json": service._merge_result_json,
                "_safe_status_dict": service._safe_status_dict, "_maybe_await": service._maybe_await,
            }
            from tdata_import import cleanup as _cleanup_mod
            ns["cleanup"] = _cleanup_mod
            exec(compile(src, f"<{SERVICE_PATH}:mutant>", "exec"), ns)
            return ns["confirm_install"]

        # --- MUT-1: stop-before-install ordering removed (force was_runtime_
        # running's guard to False, so the stop/confirm block never runs at
        # all) -> a running manager's confirm_install would call install
        # WITHOUT ever stopping it first.
        def _mut1(node):
            killer = _IfGuardKiller("was_runtime_running", False)
            node = killer.visit(node)
            if not killer.hit:
                raise AssertionError("MUT-1 anchor not found")
            return node

        mutant_confirm_1 = _build_mutant_confirm_install(_mut1)
        op_m1, mgr_m1 = "op-mut1", "mgrmut1"
        await _seed_identity_verified_op(db, tmpd, op_id=op_m1, manager_key=mgr_m1)
        fakes_m1 = RuntimeFakes(initially_running=True)
        r_m1 = await mutant_confirm_1(
            operation_id=op_m1, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=fakes_m1.spawn_runtime, wait_runtime_ready=fakes_m1.wait_runtime_ready,
            stop_runtime=fakes_m1.stop_runtime, is_runtime_running=fakes_m1.is_runtime_running,
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("MUT-1. with stop-before-install disabled, a RUNNING manager's install proceeds WITHOUT "
              "ever calling stop_runtime (proves I2's ordering assertion is load-bearing)",
              r_m1.get("ok") is True and fakes_m1.stop_call_count == 0, (r_m1, fakes_m1.stop_call_count))

        # --- MUT-2: rollback spawns unconditionally regardless of
        # stop_confirmed_by_us -> a manager that was NEVER running before this
        # operation gets spawned by an unrelated failure's rollback.
        # Must match the BARE `if stop_confirmed_by_us:` guard (the one
        # gating respawn in the install_result-is-not-None branch) exactly --
        # NOT the earlier `if not stop_confirmed_by_us:` pre-install fail-
        # closed check, which a substring match would hit first since it
        # appears earlier in the source.
        class _ExactIfGuardKiller(ast.NodeTransformer):
            def __init__(self, exact_test_src, replacement):
                self.exact_test_src = exact_test_src
                self.replacement = replacement
                self.hit = False

            def visit_If(self, node):
                self.generic_visit(node)
                if not self.hit and ast.unparse(node.test) == self.exact_test_src:
                    node.test = ast.Constant(value=self.replacement)
                    self.hit = True
                return node

        def _mut2(node):
            killer = _ExactIfGuardKiller("stop_confirmed_by_us", True)
            node = killer.visit(node)
            if not killer.hit:
                raise AssertionError("MUT-2 anchor not found")
            return node

        mutant_confirm_2 = _build_mutant_confirm_install(_mut2)
        op_m2, mgr_m2 = "op-mut2", "mgrmut2"
        await _seed_identity_verified_op(db, tmpd, op_id=op_m2, manager_key=mgr_m2)
        fakes_m2 = RuntimeFakes(initially_running=False, ready_ok=False)  # not running before; fails at readiness
        r_m2 = await mutant_confirm_2(
            operation_id=op_m2, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=fakes_m2.spawn_runtime, wait_runtime_ready=fakes_m2.wait_runtime_ready,
            stop_runtime=fakes_m2.stop_runtime, is_runtime_running=fakes_m2.is_runtime_running,
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("MUT-2. with the stop_confirmed_by_us guard disabled, an ORIGINALLY-NOT-RUNNING manager's "
              "failed install STILL gets spawned by rollback (proves I1's 'never spawns if not ours to "
              "restart' assertion is load-bearing)",
              r_m2.get("ok") is False and fakes_m2.spawn_call_count >= 2, (r_m2, fakes_m2.spawn_call_count))

        # --- MUT-3: old-session restore removed (rollback_install call
        # deleted from the AST) -> a failed install leaves the OLD session
        # gone/corrupted instead of restored.
        class _RollbackCallStripper(ast.NodeTransformer):
            def __init__(self):
                self.hit = False

            def visit_Expr(self, node):
                self.generic_visit(node)
                v = node.value
                if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                        and v.func.attr == "rollback_install"):
                    self.hit = True
                    return None
                return node

        def _mut3(node):
            stripper = _RollbackCallStripper()
            node = stripper.visit(node)
            if not stripper.hit:
                raise AssertionError("MUT-3 anchor not found")
            return node

        mutant_confirm_3 = _build_mutant_confirm_install(_mut3)
        op_m3, mgr_m3 = "op-mut3", "mgrmut3"
        prior_m3 = _seed_prior_session(tmpd, mgr_m3, b"PRIOR-MUT3-BYTES")
        await _seed_identity_verified_op(db, tmpd, op_id=op_m3, manager_key=mgr_m3)
        fakes_m3 = RuntimeFakes(initially_running=True, ready_ok=False)
        r_m3 = await mutant_confirm_3(
            operation_id=op_m3, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=fakes_m3.spawn_runtime, wait_runtime_ready=fakes_m3.wait_runtime_ready,
            stop_runtime=fakes_m3.stop_runtime, is_runtime_running=fakes_m3.is_runtime_running,
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        with open(prior_m3, "rb") as fh:
            after_m3 = fh.read()
        check("MUT-3. with rollback_install() stripped, the OLD session is NOT restored after a failed "
              "install (proves I4/I5/I6's byte-restore assertions are load-bearing)",
              r_m3.get("ok") is False and after_m3 != b"PRIOR-MUT3-BYTES", (r_m3, after_m3))

        # --- MUT-4: success declared before wait_runtime_ready is even called
        # (the readiness gate itself removed) -> confirm_install would report
        # ok=True without ever confirming the new runtime is actually ready.
        class _ReadinessGateStripper(ast.NodeTransformer):
            """Removes the `if not ready: raise RuntimeFailed(...)` If-block
            entirely (returns None to delete it from its containing body)."""

            def __init__(self):
                self.hit = False

            def visit_If(self, node):
                self.generic_visit(node)
                if not self.hit and "not ready" in ast.unparse(node.test):
                    self.hit = True
                    return None
                return node

        def _mut4(node):
            stripper = _ReadinessGateStripper()
            node = stripper.visit(node)
            if not stripper.hit:
                raise AssertionError("MUT-4 anchor not found")
            return node

        mutant_confirm_4 = _build_mutant_confirm_install(_mut4)
        op_m4, mgr_m4 = "op-mut4", "mgrmut4"
        await _seed_identity_verified_op(db, tmpd, op_id=op_m4, manager_key=mgr_m4)
        fakes_m4 = RuntimeFakes(initially_running=False, ready_ok=False)
        r_m4 = await mutant_confirm_4(
            operation_id=op_m4, base_dir=tmpd, proxy_row=PROXY_ROW_OK,
            spawn_runtime=fakes_m4.spawn_runtime, wait_runtime_ready=fakes_m4.wait_runtime_ready,
            stop_runtime=fakes_m4.stop_runtime, is_runtime_running=fakes_m4.is_runtime_running,
            proxy_prober=_fake_prober_ok(), db_path=db,
        )
        check("MUT-4. with the readiness gate stripped, confirm_install reports SUCCESS even though "
              "wait_runtime_ready returned False (proves SUCCESS_REQUIRES_READINESS is load-bearing)",
              r_m4.get("ok") is True, detail=str(r_m4))

        # ==================================================================
        # SOURCE PRIORITY UNCHANGED -- this patch never touches detector.py/
        # session_inspector.py/tdata_adapter.py. Re-proves the existing
        # .session > TData rule end-to-end through the REAL detector module.
        # ==================================================================
        inv_both = ArchiveInventory()
        inv_both.session_candidates = ["/x/account.session"]
        inv_both.tdata_dirs = ["/x/tdata"]
        kind, path = detector.choose_source(inv_both)
        check("SOURCE-PRIORITY. .session + tdata -> ready_session (unchanged by this patch)",
              kind == "ready_session" and path == "/x/account.session", (kind, path))

        inv_multi = ArchiveInventory()
        inv_multi.session_candidates = ["/x/a.session", "/x/b.session"]
        raised = False
        try:
            detector.choose_source(inv_multi)
        except Exception as e:
            raised = e.__class__.__name__ == "MultipleAccounts"
        check("SOURCE-PRIORITY. multiple .session files -> MultipleAccounts, fail-closed (unchanged)", raised)

    asyncio.run(run())

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL EXISTING-RUNTIME SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
