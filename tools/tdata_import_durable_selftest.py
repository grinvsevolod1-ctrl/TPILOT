# -*- coding: utf-8 -*-
"""Offline selftest for the tdata/session-import durable operation layer.

Pure/offline: temp SQLite only, no network, no Telegram, no production DB, no
spend. Exercises storage.tdata_import_* (create/idempotency/one-active-per-
manager/CAS advance/branch/set_fields/claim/fail/cancel/complete/stale/cleanup)
and the tdata_import.models Stage/Status/FailureClass constants.

Run:  python tools\\tdata_import_durable_selftest.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import storage  # noqa: E402  (real storage layer, importable)
from manager_registry import build_manager_paths  # noqa: E402
from tdata_import import models  # noqa: E402  (pure)

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _guard_temp_db(db_path):
    """Never allow the selftest to touch a real project DB."""
    rp = os.path.realpath(db_path)
    tmp = os.path.realpath(tempfile.gettempdir())
    assert rp.startswith(tmp), f"db must live under tempdir, got {rp}"
    assert "data_tpilot.db" not in rp and os.sep + "db" + os.sep not in rp, rp


def main():
    tmpd = tempfile.mkdtemp(prefix="tdimport_durable_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)

    S = models.Stage
    ST = models.Status

    # --- create + idempotency -------------------------------------------
    row = storage.tdata_import_create("op1", "MgrOne", owner_user_id=7,
                                      source_key="src", expires_at="2999-01-01T00:00:00",
                                      db_path=db)
    check("create returns row", bool(row) and row.get("operation_id") == "op1")
    check("create initial status/stage", row and row["status"] == ST.CREATED and row["stage"] == S.CREATED)
    check("create normalizes manager_key", row and row["manager_key"] == "mgrone",
          detail=str(row.get("manager_key") if row else None))

    again = storage.tdata_import_create("op1", "MgrOne", db_path=db)
    check("create idempotent on operation_id", bool(again) and again["operation_id"] == "op1")

    # --- one active op per manager --------------------------------------
    dup = storage.tdata_import_create("op1b", "mgrone", db_path=db)
    check("second active op for same manager rejected (None)", dup is None)

    other = storage.tdata_import_create("op2", "MgrTwo", db_path=db)
    check("different manager can start its own op", bool(other))

    # --- CAS advance: valid edge, stale, invalid ------------------------
    ok = storage.tdata_import_advance_stage("op1", S.CREATED, S.MANAGER_PREPARED, db_path=db)
    check("advance created->manager_prepared", ok is True)
    moved = storage.tdata_import_get("op1", db_path=db)
    check("advance bumps status to processing", moved and moved["status"] == ST.PROCESSING)
    check("advance updates stage", moved and moved["stage"] == S.MANAGER_PREPARED)

    stale = storage.tdata_import_advance_stage("op1", S.CREATED, S.MANAGER_PREPARED, db_path=db)
    check("stale CAS (wrong from_stage) returns False", stale is False)

    raised = False
    try:
        storage.tdata_import_advance_stage("op1", S.MANAGER_PREPARED, S.PROXY_VERIFIED, db_path=db)
    except ValueError:
        raised = True
    check("invalid (skipping) transition raises ValueError", raised)

    raised2 = False
    try:
        storage.tdata_import_advance_stage("op1", S.MANAGER_PREPARED, S.MANAGER_PREPARED,
                                           fields={"tg_user_id": 5, "evil_col": 1}, db_path=db)
    except ValueError:
        raised2 = True
    check("forbidden field in advance raises ValueError", raised2)

    # --- walk the ready-session branch to runtime_running ---------------
    seq = [
        (S.MANAGER_PREPARED, S.SOURCE_ASSIGNED, {"source_key": "src1"}),
        (S.SOURCE_ASSIGNED, S.PROXY_ASSIGNED, {"proxy_lease_id": 42, "proxy_ref": "lease#42"}),
        (S.PROXY_ASSIGNED, S.PROXY_VERIFIED, {"proxy_verified_ip": "203.0.113.9"}),
        (S.PROXY_VERIFIED, S.ARCHIVE_UPLOADED, None),
        (S.ARCHIVE_UPLOADED, S.ARCHIVE_VALIDATED, None),
        (S.ARCHIVE_VALIDATED, S.SESSION_DETECTED, None),
        (S.SESSION_DETECTED, S.SESSION_SELECTED, {"import_method": models.ImportMethod.READY_SESSION}),
        (S.SESSION_SELECTED, S.SESSION_VALIDATED, None),
        (S.SESSION_VALIDATED, S.IDENTITY_CHECKING, None),
        (S.IDENTITY_CHECKING, S.IDENTITY_VERIFIED, {"tg_user_id": 12345, "username": "u", "masked_phone": "+49****789"}),
        (S.IDENTITY_VERIFIED, S.SESSION_INSTALLING, None),
        (S.SESSION_INSTALLING, S.SESSION_INSTALLED, None),
        (S.SESSION_INSTALLED, S.RUNTIME_STARTING, None),
        (S.RUNTIME_STARTING, S.RUNTIME_RUNNING, None),
    ]
    walk_ok = all(storage.tdata_import_advance_stage("op1", a, b, fields=f, db_path=db) for a, b, f in seq)
    check("full ready-session walk to runtime_running", walk_ok)
    fin = storage.tdata_import_get("op1", db_path=db)
    check("mutable fields persisted (ip/tg/method)",
          fin and fin["proxy_verified_ip"] == "203.0.113.9"
          and fin["tg_user_id"] == 12345 and fin["import_method"] == "ready_session")

    # --- complete guard --------------------------------------------------
    early = storage.tdata_import_complete("op2", db_path=db)  # op2 still at 'created'
    check("complete rejected before runtime_running", early is False)
    done = storage.tdata_import_complete("op1", result_json='{"ok":true}', db_path=db)
    check("complete at runtime_running succeeds", done is True)
    done_row = storage.tdata_import_get("op1", db_path=db)
    check("completed row status/stage", done_row and done_row["status"] == ST.DONE and done_row["stage"] == S.COMPLETED)

    # a completed op can no longer advance
    post = storage.tdata_import_advance_stage("op1", S.RUNTIME_RUNNING, S.COMPLETED, db_path=db)
    check("terminal op cannot advance", post is False)

    # after completion the manager slot frees -> a new op may start
    reop = storage.tdata_import_create("op1c", "mgrone", db_path=db)
    check("new op allowed after prior completed", bool(reop))

    # --- tdata branch on op2 --------------------------------------------
    storage.tdata_import_advance_stage("op2", S.CREATED, S.MANAGER_PREPARED, db_path=db)
    storage.tdata_import_advance_stage("op2", S.MANAGER_PREPARED, S.SOURCE_ASSIGNED, db_path=db)
    storage.tdata_import_advance_stage("op2", S.SOURCE_ASSIGNED, S.PROXY_ASSIGNED, db_path=db)
    storage.tdata_import_advance_stage("op2", S.PROXY_ASSIGNED, S.PROXY_VERIFIED, db_path=db)
    storage.tdata_import_advance_stage("op2", S.PROXY_VERIFIED, S.ARCHIVE_UPLOADED, db_path=db)
    storage.tdata_import_advance_stage("op2", S.ARCHIVE_UPLOADED, S.ARCHIVE_VALIDATED, db_path=db)
    storage.tdata_import_advance_stage("op2", S.ARCHIVE_VALIDATED, S.SESSION_DETECTED, db_path=db)
    br = storage.tdata_import_advance_stage("op2", S.SESSION_DETECTED, S.TDATA_CONVERTED,
                                            fields={"import_method": models.ImportMethod.TDATA_CONVERTED}, db_path=db)
    check("session_detected -> tdata_converted branch", br is True)

    # --- set_fields (no stage change) -----------------------------------
    sf = storage.tdata_import_set_fields("op2", {"proxy_verified_ip": "198.51.100.7"}, db_path=db)
    check("set_fields updates without stage change", sf is True)
    sf_row = storage.tdata_import_get("op2", db_path=db)
    check("set_fields value persisted, stage unchanged",
          sf_row and sf_row["proxy_verified_ip"] == "198.51.100.7" and sf_row["stage"] == S.TDATA_CONVERTED)

    # --- claim ownership -------------------------------------------------
    c1 = storage.tdata_import_claim("op2", "worker-A", db_path=db)
    c2 = storage.tdata_import_claim("op2", "worker-A", db_path=db)  # same worker re-claim ok
    c3 = storage.tdata_import_claim("op2", "worker-B", db_path=db)  # different worker denied
    check("claim by first worker", c1 is True)
    check("re-claim by same worker", c2 is True)
    check("claim by other worker denied", c3 is False)

    # --- fail / cancel ---------------------------------------------------
    failed = storage.tdata_import_fail("op2", error_class=models.FailureClass.TDATA_CONVERSION_FAILED,
                                       error_text="unknown tdata variant", db_path=db)
    check("fail moves active op to error", failed is True)
    frow = storage.tdata_import_get("op2", db_path=db)
    check("failed row records safe error_class",
          frow and frow["status"] == ST.ERROR and frow["error_class"] == "tdata_conversion_failed")
    check("failed error_class is a known safe class", frow and frow["error_class"] in models.FailureClass.ALL)
    refail = storage.tdata_import_fail("op2", error_class="x", db_path=db)
    check("cannot fail an already-terminal op", refail is False)

    cop = storage.tdata_import_create("op3", "MgrThree", db_path=db)
    cancelled = storage.tdata_import_cancel("op3", db_path=db)
    check("cancel active op", bool(cop) and cancelled is True)
    crow = storage.tdata_import_get("op3", db_path=db)
    check("cancelled status", crow and crow["status"] == ST.CANCELLED)

    # --- stale listing ---------------------------------------------------
    stale_op = storage.tdata_import_create("op4", "MgrFour", expires_at="2000-01-01T00:00:00", db_path=db)
    fresh_op = storage.tdata_import_create("op5", "MgrFive", expires_at="2999-01-01T00:00:00", db_path=db)
    stales = storage.tdata_import_list_stale("2020-06-01T00:00:00", db_path=db)
    stale_ids = {r["operation_id"] for r in stales}
    check("stale list includes past-expiry active op", "op4" in stale_ids and bool(stale_op))
    check("stale list excludes future-expiry op", "op5" not in stale_ids and bool(fresh_op))

    # --- cleanup marker --------------------------------------------------
    mc = storage.tdata_import_mark_cleanup("op4", db_path=db)
    check("mark_cleanup", mc is True)
    mcrow = storage.tdata_import_get("op4", db_path=db)
    check("cleanup_done flag set", mcrow and int(mcrow["cleanup_done"]) == 1)

    # --- managers.auth_profile (TPILOT AUTH PROFILE FIX 20260719) --------
    # This durable flag is what lets a runtime restart pick the same API/device
    # profile a direct-import manager was originally authorized under (see
    # main.py's _api_profile_for/_device_kwargs_for). Default 'project' must
    # cover ALL existing/phone/QR managers untouched; only a successful direct
    # import ever writes 'tdesktop' (via main.py's _tdimport_spawn_runtime,
    # BEFORE the runtime subprocess is spawned -- see the order-of-operations
    # test in tools\\tdata_import_adminbot_wiring_selftest.py). manager_get/
    # manager_add/manager_set_fields read the module-level storage.DB_PATH
    # global (not a db_path kwarg like the tdata_import_* functions above),
    # so it's rebound here to the same guarded temp db and restored after.
    _prev_db_path, _prev_queue_db_path = storage.DB_PATH, storage.QUEUE_DB_PATH
    storage.DB_PATH = db
    storage.QUEUE_DB_PATH = db
    try:
        async def _auth_profile_checks():
            paths = build_manager_paths(tmpd, "authprofiletest")
            await storage.manager_add(
                manager_key="authprofiletest", display_name="Auth Profile Test", phone="",
                status="new", session_path=paths["session_path"], db_path=paths["db_path"],
                workdir=paths["root"], log_path=paths["log_path"], is_enabled=0,
            )
            row = await storage.manager_get("authprofiletest")
            check("new manager row defaults auth_profile='project' (phone/QR/existing managers unaffected)",
                  row is not None and row.get("auth_profile") == "project",
                  detail=str(row.get("auth_profile") if row else None))

            await storage.manager_set_fields("authprofiletest", auth_profile="tdesktop")
            row2 = await storage.manager_get("authprofiletest")
            check("manager_set_fields writes auth_profile='tdesktop' for a direct-import manager",
                  row2 is not None and row2.get("auth_profile") == "tdesktop")

        asyncio.run(_auth_profile_checks())
    finally:
        storage.DB_PATH, storage.QUEUE_DB_PATH = _prev_db_path, _prev_queue_db_path

    # --- models sanity ---------------------------------------------------
    check("Stage.TRANSITIONS fork at session_detected",
          set(S.TRANSITIONS[S.SESSION_DETECTED]) == {S.SESSION_SELECTED, S.TDATA_CONVERTED})
    check("Stage.can_transition helper", S.can_transition(S.RUNTIME_RUNNING, S.COMPLETED)
          and not S.can_transition(S.CREATED, S.COMPLETED))
    check("storage stage table matches models order",
          tuple(storage._TDIMPORT_STAGES) == models.Stage.ORDER)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL DURABLE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
