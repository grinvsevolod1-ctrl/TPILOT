# -*- coding: utf-8 -*-
"""tools/replacement_tdimport_selftest.py -- offline selftest for the
"TPILOT AUTH SAFETY 20260809" patch (plan Ф4): Session/TData entry method for
account REPLACEMENT. New orchestration, built from entirely existing,
unmodified primitives:
  - tdata_import.service.start_import (identity verification only, real
    detector/session_inspector/tdata_adapter/identity_probe pipeline, nothing
    installed to any live/permanent path);
  - installer.install_session (the SAME atomic install-with-rollback
    installer.py already uses, targeting the replacement TEMP path instead
    of a manager's final path);
  - the SAME identity-conflict guards (_replacement_tg_user_conflict /
    _replacement_username_conflict / _replacement_tgid_reserved_by_other /
    same-as-old check) _replacement_after_signin already uses for QR/phone;
  - the SAME auth_phone -> identity_ok transition edge already proven safe
    in the Ф3 independent checkpoint (STORAGE-* section of qr_auth_context_
    selftest.py).

Pure/offline, same technique as tools/tdata_import_flow_selftest.py (real
synthetic Telethon-shaped session file, real archive/detector/session_
inspector pipeline, only the Telegram CLIENT itself faked) combined with
tools/qr_auth_context_selftest.py's REPL_BACKEND_NAMES-style real extraction
of the replacement machinery. Never real Telegram/proxy network, never
production DB/session/runtime.

    python tools\\replacement_tdimport_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

FAILURES = []


def check(label, condition, detail=""):
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()


# ======================================================================
# Synthetic session/archive builders -- identical technique to
# tools/tdata_import_flow_selftest.py.
# ======================================================================

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
    def __init__(self, *, id, username="newacc", first_name="New", last_name="Acc", phone="79990000001", bot=False, deleted=False):
        self.id = id
        self.username = username
        self.first_name = first_name
        self.last_name = last_name
        self.phone = phone
        self.bot = bot
        self.deleted = deleted


class FakeClient:
    def __init__(self, *, me):
        self._me = me
        self.disconnected = False

    async def get_me(self):
        return self._me

    async def disconnect(self):
        self.disconnected = True


def _make_client_factory(me):
    calls = []

    async def factory(session_path):
        calls.append(session_path)
        return FakeClient(me=me)

    return factory, calls


def _fake_prober_ok(ip="203.0.113.9"):
    from tdata_import import proxy_gate

    def prober(host, port, username, password, rdns):
        return proxy_gate.ProxyVerification(ip=ip, method="socks5h" if rdns else "socks5")

    return prober


# tdata/session import requires a proxy-configured manager (proxy_gate.
# assert_proxy_mode_allowed forbids direct connections for this flow --
# pre-existing, unmodified policy, not something Ф4 introduces).
PROXY_CONFIG_OK = {
    "proxy_enabled": 1, "proxy_type": "socks5", "proxy_host": "10.0.0.1",
    "proxy_port": 1080, "proxy_username": "u", "proxy_password": "p",
}


REPL_BACKEND_NAMES = {
    "_REPLACE_STEP_PHONE", "_REPLACE_STEP_CODE", "_REPLACE_STEP_PASS", "_REPLACE_STEPS",
    "_REPLACE_TEMP_SESSION_SUFFIX", "_REPLACE_NEXT_STEP_BY_STATUS",
    "MANAGER_ONBOARD_TIMEOUT_SEC", "MANAGER_PHONE_COOLDOWN_SEC",
    "_MANAGER_AUTH_AUDIT_LOG",
    "_replacement_next_step_for_status", "_replacement_result",
    "_replacement_temp_session_path", "_replacement_permanent_session_path",
    "_replacement_cleanup_temp_files",
    "_replacement_relogin_active", "replacement_active_for_old_key",
    "_replacement_onboarding_proxy_fields", "_replacement_resolve_durable_proxy",
    "_replacement_tg_user_conflict", "_replacement_username_conflict",
    "_replacement_tgid_reserved_by_other", "_replacement_fail",
    "replacement_start", "replacement_reserve_key", "replacement_describe_proxy",
    "_manager_auth_audit_log",
    "_resolve_manager_telethon_proxy", "_build_telethon_proxy_from_row",
    "_manager_auth_proxy_mode", "_proxy_type_norm", "_proxy_port_int", "_manager_proxy_enabled",
    "_future_iso", "_now_utc_iso", "_partner_source_key_for_manager",
    "_tdimport_new_operation_id", "_tdimport_real_prober",
    "replacement_send_tdimport_start", "replacement_confirm_tdimport_install",
}


def _extract_by_names(src, names):
    tree = ast.parse(src)
    nodes = []
    seen = set()
    for n in tree.body:
        nm = getattr(n, "name", None)
        if nm and nm in names:
            nodes.append(n)
            seen.add(nm)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names:
            nodes.append(n)
            seen.add(n.targets[0].id)
            continue
    missing = names - seen
    if missing:
        raise AssertionError(f"expected {names}, missing {missing}")
    return nodes


def _selftest_db_guard(db_path, base_dir, storage_mod):
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


def build_ns(db_path, base_dir, *, client_factory_holder):
    import manager_registry
    import storage as _storage
    import aiosqlite as _aiosqlite
    from tdata_import import service as _tdimport_service_mod

    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    nodes = _extract_by_names(MAIN_SRC, REPL_BACKEND_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    async def fake_tdimport_client_factory_for(row, session_path):
        factory = client_factory_holder["factory"]
        return await factory(session_path)

    ns = {
        "os": os, "asyncio": asyncio, "aiosqlite": _aiosqlite, "Path": Path,
        "datetime": datetime, "timedelta": timedelta, "timezone": timezone,
        # utcnow refactor (2026-08-16): extracted main.py code reads the
        # clock through the module-level _tp_utc_now() seam (naive UTC).
        "_tp_utc_now": (lambda: datetime.now(timezone.utc).replace(tzinfo=None)),
        "Optional": None, "Dict": dict, "Any": object,
        "BASE_DIR": base_dir, "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "validate_manager_key": manager_registry.validate_manager_key,
        "build_manager_paths": manager_registry.build_manager_paths,
        "ensure_manager_dirs": manager_registry.ensure_manager_dirs,
        "manager_get": _storage.manager_get,
        "manager_add": _storage.manager_add,
        "manager_set_fields": _storage.manager_set_fields,
        "_repl_storage": _storage,
        "_tdimport_service": _tdimport_service_mod,
        "_tdimport_client_factory_for": fake_tdimport_client_factory_for,
        "json": __import__("json"),
    }
    exec(compile(module_src, f"<{MAIN_PATH}:repl_tdimport>", "exec"), ns)
    ns["__storage__"] = _storage
    return ns


def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="replacement_tdimport_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


async def seed_manager(ns, *, key, tg_user_id, display_name, username):
    storage_mod = ns["__storage__"]
    paths = ns["build_manager_paths"](str(ns["BASE_DIR"]), key)
    os.makedirs(paths["root"], exist_ok=True)
    await storage_mod.manager_add(
        manager_key=key, display_name=display_name, phone="+70000000000", status="active",
        session_path=paths["session_path"], db_path=paths["db_path"], workdir=paths["root"],
        log_path=paths["log_path"], is_enabled=1,
    )
    await storage_mod.manager_set_fields(key, tg_user_id=(tg_user_id or None), telegram_username=username)
    con = sqlite3.connect(ns["TPILOT_DB_PATH"])
    try:
        con.execute("CREATE TABLE IF NOT EXISTS manager_source_links(manager_key TEXT, source_key TEXT)")
        con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES(?,?)", (key, "src1"))
        con.commit()
    finally:
        con.close()


def _build_archive(tmp_root, tag, *, auth_key=None):
    sess_path = os.path.join(str(tmp_root), f"{tag}.session")
    _build_synthetic_session(sess_path, auth_key=auth_key)
    archive_path = os.path.join(str(tmp_root), f"{tag}.zip")
    _zip_session_archive(archive_path, sess_path)
    return archive_path


def main():
    async def run():
        # === Q1: happy path -- new identity, direct proxy. ===================
        tmp1, db1 = make_temp_env()
        holder1 = {"factory": None}
        ns1 = build_ns(db1, tmp1, client_factory_holder=holder1)
        await seed_manager(ns1, key="mgrq1old", tg_user_id=601001, display_name="Old1", username="old1_u")
        with open(ns1["_replacement_permanent_session_path"]("mgrq1old"), "wb") as fh:
            fh.write(b"OLD-LIVE-Q1-BYTES")
        me1 = FakeUser(id=602001, username="new1_u")
        factory1, calls1 = _make_client_factory(me1)
        holder1["factory"] = factory1

        op1 = await ns1["replacement_start"]("mgrq1old", 1, db_path=db1)
        assert op1.get("ok"), op1
        op_id1 = str(op1.get("operation_id"))
        archive1 = _build_archive(tmp1, "q1")

        r1 = await ns1["replacement_send_tdimport_start"](op_id1, "New One", "mgrq1new", PROXY_CONFIG_OK, archive1, db_path=db1, proxy_prober=_fake_prober_ok())
        check("Q1. send_tdimport_start reaches identity_verified", r1.get("ok") is True, detail=r1)
        check("Q1. client factory was actually invoked (real detector/session_inspector pipeline ran)",
              len(calls1) == 1, detail=calls1)
        op_row1 = ns1["_repl_storage"].replacement_get(op_id1, db_path=db1)
        check("Q1. operation status is 'auth_phone' after send_tdimport_start (not yet identity_ok)",
              str(op_row1.get("status")) == "auth_phone", detail=op_row1)

        live_path_old1 = ns1["_replacement_permanent_session_path"]("mgrq1old")
        with open(live_path_old1, "rb") as fh:
            old_live_before = fh.read()

        tdi_op1 = str(r1.get("tdimport_operation_id"))
        r1c = await ns1["replacement_confirm_tdimport_install"](op_id1, tdi_op1, db_path=db1)
        check("Q1. confirm_tdimport_install succeeds", r1c.get("ok") is True, detail=r1c)
        op_row1b = ns1["_repl_storage"].replacement_get(op_id1, db_path=db1)
        check("Q1. operation status advanced to identity_ok", str(op_row1b.get("status")) == "identity_ok", detail=op_row1b)
        check("Q1. new_tg_user_id persisted correctly", int(op_row1b.get("new_tg_user_id") or 0) == 602001, detail=op_row1b)
        check("Q1. new_username persisted correctly", str(op_row1b.get("new_username") or "") == "new1_u", detail=op_row1b)

        temp_path1 = ns1["_replacement_temp_session_path"]("mgrq1new")
        check("Q1. session installed at the TEMP replacement path (not the final/permanent path)",
              os.path.exists(temp_path1))
        perm_path1 = ns1["_replacement_permanent_session_path"]("mgrq1new")
        check("Q1. NO session was installed at the new manager's permanent path (that only happens "
              "later, at cutover)", not os.path.exists(perm_path1))
        with open(live_path_old1, "rb") as fh:
            old_live_after = fh.read()
        check("Q1. OLD manager's live session bytes are COMPLETELY UNCHANGED", old_live_after == old_live_before,
              detail=(old_live_before, old_live_after))

        # === Q2: idempotent retry -- a second confirm call after identity_ok
        # short-circuits cleanly. ===============================================
        r1d = await ns1["replacement_confirm_tdimport_install"](op_id1, tdi_op1, db_path=db1)
        check("Q2. second confirm call after identity_ok short-circuits (idempotent)",
              r1d.get("ok") is True and r1d.get("code") == "identity_verified", detail=r1d)

        # === Q3: same-as-old identity -> rejected. tdata_import's OWN
        # duplicate_check (tg_user_id already belongs to an existing manager,
        # which the OLD manager being replaced still is) catches this at the
        # SEND step already -- an even stronger/earlier gate than the
        # confirm-step guard alone would give. Proves this real, unmodified
        # first-layer check is genuinely wired in (not just present in
        # source), and that nothing is EVER installed anywhere for a
        # same-as-old attempt.
        tmp3, db3 = make_temp_env()
        holder3 = {"factory": None}
        ns3 = build_ns(db3, tmp3, client_factory_holder=holder3)
        await seed_manager(ns3, key="mgrq3old", tg_user_id=603001, display_name="Old3", username="old3_u")
        me3 = FakeUser(id=603001, username="old3_u")  # SAME id as the old manager
        factory3, _ = _make_client_factory(me3)
        holder3["factory"] = factory3
        op3 = await ns3["replacement_start"]("mgrq3old", 1, db_path=db3)
        op_id3 = str(op3.get("operation_id"))
        archive3 = _build_archive(tmp3, "q3", auth_key=b"\xcd" * 256)
        r3 = await ns3["replacement_send_tdimport_start"](op_id3, "New Three", "mgrq3new", PROXY_CONFIG_OK, archive3, db_path=db3, proxy_prober=_fake_prober_ok())
        check("Q3. same-as-old identity is REJECTED (caught by tdata_import's own "
              "duplicate_check at the send/verify step)",
              r3.get("ok") is False and r3.get("tdimport_error_class") == "duplicate_telegram_account", detail=r3)
        temp_path3 = ns3["_replacement_temp_session_path"]("mgrq3new")
        check("Q3. NO temp session installed for a rejected identity", not os.path.exists(temp_path3))
        op_row3 = ns3["_repl_storage"].replacement_get(op_id3, db_path=db3)
        check("Q3. operation never advanced to identity_ok", str(op_row3.get("status")) != "identity_ok", detail=op_row3)

        # === Q4: tg_user_id already belongs to ANOTHER existing manager --
        # same first-layer gate as Q3, different conflicting party. ============
        tmp4, db4 = make_temp_env()
        holder4 = {"factory": None}
        ns4 = build_ns(db4, tmp4, client_factory_holder=holder4)
        await seed_manager(ns4, key="mgrq4old", tg_user_id=604001, display_name="Old4", username="old4_u")
        await seed_manager(ns4, key="mgrq4other", tg_user_id=604999, display_name="Other4", username="other4_u")
        me4 = FakeUser(id=604999, username="other4_u")  # belongs to mgrq4other
        factory4, _ = _make_client_factory(me4)
        holder4["factory"] = factory4
        op4 = await ns4["replacement_start"]("mgrq4old", 1, db_path=db4)
        op_id4 = str(op4.get("operation_id"))
        archive4 = _build_archive(tmp4, "q4", auth_key=b"\xef" * 256)
        r4 = await ns4["replacement_send_tdimport_start"](op_id4, "New Four", "mgrq4new", PROXY_CONFIG_OK, archive4, db_path=db4, proxy_prober=_fake_prober_ok())
        check("Q4. tg_user_id conflict with ANOTHER existing manager is REJECTED at send step",
              r4.get("ok") is False and r4.get("tdimport_error_class") == "duplicate_telegram_account", detail=r4)
        temp_path4 = ns4["_replacement_temp_session_path"]("mgrq4new")
        check("Q4. NO temp session installed for a conflicting identity", not os.path.exists(temp_path4))

        # === Q4b: the SECOND-LAYER guard's unique value -- a tg_user_id
        # reserved by ANOTHER in-flight REPLACEMENT operation (manager_
        # replacements.new_tg_user_id). tdata_import's own reserved_check
        # only looks at OTHER tdata_import_ops, never manager_replacements --
        # so this conflict is INVISIBLE to the first layer and can only be
        # caught by confirm_tdimport_install's own _replacement_tgid_
        # reserved_by_other call (the same function _replacement_after_
        # signin already relies on for QR/phone). Proves the second layer is
        # not redundant with the first.
        tmp4b, db4b = make_temp_env()
        holder4b = {"factory": None}
        ns4b = build_ns(db4b, tmp4b, client_factory_holder=holder4b)
        await seed_manager(ns4b, key="mgrq4bold", tg_user_id=604101, display_name="Old4b", username="old4bold_u")
        await seed_manager(ns4b, key="mgrq4bother_old", tg_user_id=604102, display_name="Old4bOther", username="old4bother_u")
        # A second, unrelated, still-in-flight replacement operation that has
        # already reserved tg_user_id=604999 (e.g. via the QR path) -- reused
        # directly via replacement_advance, the SAME durable primitive every
        # real caller uses, not a hand-rolled raw SQL write.
        other_op = await ns4b["replacement_start"]("mgrq4bother_old", 1, db_path=db4b)
        other_op_id = str(other_op.get("operation_id"))
        reserved_other = await ns4b["replacement_reserve_key"](other_op_id, "mgrq4bothernew", db_path=db4b, base_dir=tmp4b)
        assert reserved_other.get("ok"), reserved_other
        ns4b["_repl_storage"].replacement_advance(
            other_op_id, "draft", "auth_phone", stage="qr_requested",
            fields={"new_manager_key": "mgrq4bothernew", "new_display_name": "Other", "proxy_mode": "direct",
                    "proxy_ref": "direct", "proxy_confirmed": 1},
            db_path=db4b,
        )
        ns4b["_repl_storage"].replacement_advance(
            other_op_id, "auth_phone", "identity_ok", stage="identity_verified",
            fields={"new_username": "reserved_elsewhere_u", "new_tg_user_id": 604999}, db_path=db4b,
        )

        me4b = FakeUser(id=604999, username="reserved_elsewhere_u")  # SAME id reserved by other_op
        factory4b, _ = _make_client_factory(me4b)
        holder4b["factory"] = factory4b
        op4b = await ns4b["replacement_start"]("mgrq4bold", 1, db_path=db4b)
        op_id4b = str(op4b.get("operation_id"))
        archive4b = _build_archive(tmp4b, "q4b", auth_key=b"\x11" * 256)
        r4b = await ns4b["replacement_send_tdimport_start"](op_id4b, "New Four B", "mgrq4bnew", PROXY_CONFIG_OK, archive4b, db_path=db4b, proxy_prober=_fake_prober_ok())
        check("Q4b setup: send step SUCCEEDS (first layer does NOT see cross-subsystem "
              "replacement reservations)", r4b.get("ok") is True, detail=r4b)
        if r4b.get("ok"):
            tdi_op4b = str(r4b.get("tdimport_operation_id"))
            r4bc = await ns4b["replacement_confirm_tdimport_install"](op_id4b, tdi_op4b, db_path=db4b)
            check("Q4b. confirm step's SECOND-LAYER guard catches the cross-operation "
                  "reservation conflict the first layer missed",
                  r4bc.get("ok") is False and r4bc.get("code") == "identity_conflict", detail=r4bc)
            temp_path4b = ns4b["_replacement_temp_session_path"]("mgrq4bnew")
            check("Q4b. NO temp session installed despite the first layer's false pass",
                  not os.path.exists(temp_path4b))

        # === Q4c (Ф4 checkpoint correction): cross-operation tdimport_
        # operation_id binding. Two INDEPENDENT, non-conflicting replacement
        # operations each successfully complete send_tdimport_start (distinct
        # identities -- neither guard layer has anything to catch here).
        # Feeding operation A's own confirm call operation B's
        # tdimport_operation_id must be rejected: the identity is genuinely
        # verified and non-duplicate, so without an explicit ownership check
        # this would silently attach the WRONG (but individually valid)
        # identity to operation A. Proves the fix added after the checkpoint
        # review actually blocks this, not just the duplicate-identity case
        # Q4/Q4b already cover. ===================================================
        tmp4c, db4c = make_temp_env()
        holder4c = {"factory": None}
        ns4c = build_ns(db4c, tmp4c, client_factory_holder=holder4c)
        await seed_manager(ns4c, key="mgrq4cold_a", tg_user_id=604201, display_name="Old4cA", username="old4ca_u")
        await seed_manager(ns4c, key="mgrq4cold_b", tg_user_id=604202, display_name="Old4cB", username="old4cb_u")
        opA = await ns4c["replacement_start"]("mgrq4cold_a", 1, db_path=db4c)
        op_idA = str(opA.get("operation_id"))
        opB = await ns4c["replacement_start"]("mgrq4cold_b", 1, db_path=db4c)
        op_idB = str(opB.get("operation_id"))

        meA = FakeUser(id=604301, username="new_a_u")
        factoryA, _ = _make_client_factory(meA)
        holder4c["factory"] = factoryA
        archiveA = _build_archive(tmp4c, "q4c_a", auth_key=b"\x22" * 256)
        rA = await ns4c["replacement_send_tdimport_start"](op_idA, "New A", "mgrq4cnew_a", PROXY_CONFIG_OK, archiveA, db_path=db4c, proxy_prober=_fake_prober_ok())
        check("Q4c setup: operation A's own send step succeeds", rA.get("ok") is True, detail=rA)

        meB = FakeUser(id=604302, username="new_b_u")
        factoryB, _ = _make_client_factory(meB)
        holder4c["factory"] = factoryB
        archiveB = _build_archive(tmp4c, "q4c_b", auth_key=b"\x33" * 256)
        rB = await ns4c["replacement_send_tdimport_start"](op_idB, "New B", "mgrq4cnew_b", PROXY_CONFIG_OK, archiveB, db_path=db4c, proxy_prober=_fake_prober_ok())
        check("Q4c setup: operation B's own send step succeeds", rB.get("ok") is True, detail=rB)

        if rA.get("ok") and rB.get("ok"):
            tdi_opB = str(rB.get("tdimport_operation_id"))
            r_cross = await ns4c["replacement_confirm_tdimport_install"](op_idA, tdi_opB, db_path=db4c)
            check("Q4c. confirming operation A with operation B's tdimport_operation_id is REJECTED (tdimport_mismatch)",
                  r_cross.get("ok") is False and r_cross.get("code") == "tdimport_mismatch", detail=r_cross)
            temp_pathA = ns4c["_replacement_temp_session_path"]("mgrq4cnew_a")
            check("Q4c. NO temp session installed for operation A from the mismatched cross-operation attempt",
                  not os.path.exists(temp_pathA))
            op_rowA = ns4c["_repl_storage"].replacement_get(op_idA, db_path=db4c)
            check("Q4c. operation A never advanced to identity_ok from the mismatched attempt",
                  str(op_rowA.get("status")) != "identity_ok", detail=op_rowA)
            # positive control: operation A confirming with its OWN tdimport_operation_id still works
            tdi_opA = str(rA.get("tdimport_operation_id"))
            r_ownA = await ns4c["replacement_confirm_tdimport_install"](op_idA, tdi_opA, db_path=db4c)
            check("Q4c. positive control: operation A confirming with its OWN tdimport_operation_id succeeds",
                  r_ownA.get("ok") is True, detail=r_ownA)

        # === Q5: confirm called before send_tdimport_start's identity
        # verification actually completed (bogus/unknown tdimport_operation_id)
        # -> fails closed. ========================================================
        tmp5, db5 = make_temp_env()
        holder5 = {"factory": None}
        ns5 = build_ns(db5, tmp5, client_factory_holder=holder5)
        await seed_manager(ns5, key="mgrq5old", tg_user_id=605001, display_name="Old5", username="old5_u")
        op5 = await ns5["replacement_start"]("mgrq5old", 1, db_path=db5)
        op_id5 = str(op5.get("operation_id"))
        r5c = await ns5["replacement_confirm_tdimport_install"](op_id5, "nonexistent-tdi-op", db_path=db5)
        check("Q5. confirm with an unverified/unknown tdimport_operation_id fails closed (not_verified)",
              r5c.get("ok") is False and r5c.get("code") == "not_verified", detail=r5c)
        op_row5 = ns5["_repl_storage"].replacement_get(op_id5, db_path=db5)
        check("Q5. operation status untouched by the failed-closed confirm attempt "
              "(never advanced to identity_ok)", str(op_row5.get("status")) != "identity_ok", detail=op_row5)

    asyncio.run(run())

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL REPLACEMENT TDIMPORT SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
