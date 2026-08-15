# -*- coding: utf-8 -*-
"""Durable orchestrator for the tdata/session import operation.

Public surface matches the planned controller commands 1:1:
    start_import()    <- /manager_tdimport_start
    get_status()      <- /manager_tdimport_status
    confirm_install() <- /manager_tdimport_confirm
    cancel_import()   <- /manager_tdimport_cancel

Every network-touching or process-management dependency (proxy prober,
Telegram client, runtime spawn/readiness-wait) is INJECTED by the caller
(main.py, which owns those real primitives and cannot itself be imported by
this package) -- this module never imports telethon.TelegramClient
construction logic or main.py. That keeps the full state machine, including
its failure/rollback paths, testable offline with fakes.

None of these functions ever raises to the caller: every exception is caught,
mapped to a safe error_class via storage.tdata_import_fail(), and returned as
part of the result dict. Callers should serialize the returned dict straight
into a command result (matching the project's `result_text` JSON convention).
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Awaitable, Callable, Dict, Optional

import storage

from . import archive, cleanup, detector, identity_probe, installer, proxy_gate, tdata_adapter
from .errors import RuntimeFailed, TdataImportError
from .models import FailureClass, Stage, Status

# TPILOT TDIMPORT TIMEOUT HARDENING 20260719: without a deadline, a hung Telegram/proxy
# socket (dead connection, black-holed proxy) leaves the identity-probe leg of
# start_import running forever -- AdminBot's own _submit_and_wait gives up on the panel
# command after ~75s and reports a timeout to the admin, but the controller's coroutine
# (and the DB row: status='processing', stage='identity_checking') stays alive/frozen
# indefinitely, permanently holding the one-active-op-per-manager lock. These three
# bounded deadlines cover the ENTIRE network-touching leg (client connect -> identity
# probe -> disconnect); their sum is kept safely below the ~75s panel-command budget so
# start_import always finishes (success OR a clean runtime_failed) before AdminBot's own
# timeout fires. Env-overridable for operational tuning; a non-numeric/empty/non-positive
# override silently falls back to the safe default rather than crashing the controller.
def _tdimport_timeout_env(name: str, default: float) -> float:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


TDIMPORT_CLIENT_CONNECT_TIMEOUT_SEC = _tdimport_timeout_env("TDIMPORT_CLIENT_CONNECT_TIMEOUT_SEC", 20.0)
TDIMPORT_IDENTITY_PROBE_TIMEOUT_SEC = _tdimport_timeout_env("TDIMPORT_IDENTITY_PROBE_TIMEOUT_SEC", 25.0)
TDIMPORT_DISCONNECT_TIMEOUT_SEC = _tdimport_timeout_env("TDIMPORT_DISCONNECT_TIMEOUT_SEC", 5.0)
# Reference budget this trio must stay under (AdminBot's own panel-command timeout);
# used only by the regression test below to catch a future config drift, not enforced
# at runtime (the individual per-leg deadlines are what actually bound execution).
TDIMPORT_ADMINBOT_PANEL_TIMEOUT_SEC = 75.0
TDIMPORT_IDENTITY_LEG_MAX_TIMEOUT_SEC = (
    TDIMPORT_CLIENT_CONNECT_TIMEOUT_SEC + TDIMPORT_IDENTITY_PROBE_TIMEOUT_SEC + TDIMPORT_DISCONNECT_TIMEOUT_SEC
)

# Stages at/below which a stale operation may be safely swept: nothing has
# been installed onto a manager's live session yet (session install happens
# only in confirm_install, at stage >= session_installing). An op mid-install
# / cutover is NEVER auto-swept -- it either completes or fails inside its own
# synchronous confirm command, and its session may already be on disk.
_SWEEPABLE_STAGES = frozenset(Stage.ORDER[: Stage.ORDER.index(Stage.IDENTITY_VERIFIED) + 1])

ClientFactoryFn = Callable[[str], Awaitable[Any]]
# spawn_runtime / stop_runtime / is_runtime_running may be sync OR async
# (main.py's real _spawn_manager_process/_stop_manager_process/
# _manager_process_running are async; offline selftests use plain sync
# fakes) -- _maybe_await() below accepts either.
SpawnRuntimeFn = Callable[[str], Any]
WaitRuntimeReadyFn = Callable[[str], Awaitable[bool]]
StopRuntimeFn = Callable[[str], Any]
# TPILOT AUTH SAFETY 20260809 (Ф3, D-INSTALL): returns whether manager_key's
# runtime process is CURRENTLY running -- used both before install (decide
# whether a stop is needed at all) and after issuing stop_runtime (confirm it
# actually took effect before touching the live session file).
IsRuntimeRunningFn = Callable[[str], Any]


async def _maybe_await(value: Any) -> Any:
    if hasattr(value, "__await__"):
        return await value
    return value


def _safe_error_text(exc: Exception) -> str:
    """Never let exception text carry credential material -- our own
    TdataImportError subclasses already keep messages generic; anything else
    (a bug, an OS error) is reduced to just its type name."""
    if isinstance(exc, TdataImportError):
        return str(exc)
    return f"internal error: {type(exc).__name__}"


def _error_class_of(exc: Exception) -> str:
    if isinstance(exc, TdataImportError):
        return exc.safe_class()
    return FailureClass.RUNTIME_FAILED


def _fail(operation_id: str, exc: Exception, *, stage: Optional[str] = None, db_path=None) -> Dict[str, Any]:
    error_class = _error_class_of(exc)
    error_text = _safe_error_text(exc)
    storage.tdata_import_fail(operation_id, error_class=error_class, error_text=error_text, stage=stage, db_path=db_path)
    return {"ok": False, "operation_id": operation_id, "error_class": error_class, "error_text": error_text}


def _merge_result_json(operation_id: str, patch: Dict[str, Any], *, db_path=None) -> Dict[str, Any]:
    row = storage.tdata_import_get(operation_id, db_path=db_path)
    current = {}
    if row and row.get("result_json"):
        try:
            current = json.loads(row["result_json"])
        except (ValueError, TypeError):
            current = {}
    current.update(patch)
    return current


def _safe_status_dict(row: Dict[str, Any]) -> Dict[str, Any]:
    """Project a raw tdata_import_ops row down to fields safe to hand back to
    AdminBot / a panel_commands result. Never includes result_json verbatim
    (it may hold local filesystem paths -- not secret, but not needed by the
    UI either) beyond the few whitelisted display fields."""
    extra = {}
    if row.get("result_json"):
        try:
            extra = json.loads(row["result_json"])
        except (ValueError, TypeError):
            extra = {}
    return {
        "ok": True,
        "operation_id": row.get("operation_id"),
        "manager_key": row.get("manager_key"),
        "status": row.get("status"),
        "stage": row.get("stage"),
        "import_method": row.get("import_method") or "",
        "tg_user_id": row.get("tg_user_id"),
        "username": row.get("username") or "",
        "masked_phone": row.get("masked_phone") or "",
        "display_name": row.get("display_name") or "",
        "proxy_verified_ip": row.get("proxy_verified_ip") or "",
        "dc_id": extra.get("dc_id"),
        "error_class": row.get("error_class") or "",
        "error_text": row.get("error_text") or "",
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


async def start_import(
    *,
    operation_id: str,
    manager_key: str,
    archive_path: str,
    work_root: str,
    proxy_row: Dict[str, Any],
    client_factory: ClientFactoryFn,
    owner_user_id: Optional[int] = None,
    source_key: str = "",
    expires_at: str = "",
    known_session_paths=None,
    proxy_prober=None,
    duplicate_check=None,
    reserved_check=None,
    db_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Run everything up to (and including) identity verification. Nothing
    installed yet -- fully reversible on any failure here (no manager session
    file has been touched; only scratch files under `work_root`)."""
    row = storage.tdata_import_create(
        operation_id, manager_key, owner_user_id=owner_user_id, source_key=source_key,
        expires_at=expires_at, db_path=db_path,
    )
    if row is None:
        from .errors import ConcurrentOperation
        return _fail(operation_id, ConcurrentOperation(
            f"another active tdata-import operation already exists for manager {manager_key!r}"
        ), db_path=db_path)
    if row.get("operation_id") != operation_id:
        # Shouldn't happen (create() only returns a mismatched row on a real
        # bug), but never silently proceed on the wrong operation.
        from .errors import ConcurrentOperation
        return _fail(operation_id, ConcurrentOperation("operation_id collision"), db_path=db_path)

    stage = Stage.CREATED
    try:
        os.makedirs(work_root, exist_ok=True)

        # TPILOT PROBE-WIRING FIX 20260719: persist work_root into result_json as
        # soon as the operation owns it -- BEFORE any network activity -- so that
        # cancel_import/sweep_stale (separate calls that only ever see this DB
        # row, never this function's local variables) can always find and scrub
        # the real on-disk credential-material directory, even if the controller
        # crashes or an admin cancels mid-flight. Without this, a cancel/sweep
        # landing before this point would see an empty result_json, silently
        # no-op cleanup on a None path, and still mark cleanup_done=1 -- exactly
        # the proven live defect (files remained on disk despite cleanup_done=1).
        early_merged = _merge_result_json(operation_id, {"work_root": work_root}, db_path=db_path)
        for from_stage, to_stage, fields in (
            (Stage.CREATED, Stage.MANAGER_PREPARED, {"result_json": json.dumps(early_merged)}),
            (Stage.MANAGER_PREPARED, Stage.SOURCE_ASSIGNED, {"source_key": source_key} if source_key else None),
        ):
            stage = to_stage
            storage.tdata_import_advance_stage(operation_id, from_stage, to_stage, fields=fields, db_path=db_path)

        proxy_ref = str(proxy_row.get("proxy_lease_id") or proxy_row.get("proxy_host") or "")
        stage = Stage.PROXY_ASSIGNED
        storage.tdata_import_advance_stage(
            operation_id, Stage.SOURCE_ASSIGNED, Stage.PROXY_ASSIGNED,
            fields={"proxy_lease_id": proxy_row.get("proxy_lease_id"), "proxy_ref": proxy_ref},
            db_path=db_path,
        )

        proxy_gate.assert_proxy_mode_allowed(proxy_row.get("proxy_mode") or "")
        verification = proxy_gate.verify_proxy(
            proxy_row.get("proxy_host") or "", proxy_row.get("proxy_port") or 0,
            proxy_row.get("proxy_username") or "", proxy_row.get("proxy_password") or "",
            prober=proxy_prober,
        )
        stage = Stage.PROXY_VERIFIED
        storage.tdata_import_advance_stage(
            operation_id, Stage.PROXY_ASSIGNED, Stage.PROXY_VERIFIED,
            fields={"proxy_verified_ip": verification.ip}, db_path=db_path,
        )

        stage = Stage.ARCHIVE_UPLOADED
        storage.tdata_import_advance_stage(operation_id, Stage.PROXY_VERIFIED, Stage.ARCHIVE_UPLOADED, db_path=db_path)

        res = archive.extract_archive(archive_path, work_root)
        # The uploaded ZIP is credential material and is fully consumed by the
        # extraction above -- delete it immediately (owner rule: "archive =
        # password"), regardless of what happens next.
        cleanup.cleanup_upload(archive_path)
        stage = Stage.ARCHIVE_VALIDATED
        storage.tdata_import_advance_stage(operation_id, Stage.ARCHIVE_UPLOADED, Stage.ARCHIVE_VALIDATED, db_path=db_path)

        inv = detector.inventory(res["extracted_dir"])
        kind, path = detector.choose_source(inv)
        stage = Stage.SESSION_DETECTED
        storage.tdata_import_advance_stage(operation_id, Stage.ARCHIVE_VALIDATED, Stage.SESSION_DETECTED, db_path=db_path)

        dc_id = None
        server_address = None
        if kind == "ready_session":
            from . import session_inspector
            candidate = session_inspector.inspect_session(path, origin="ready", known_session_paths=known_session_paths)
            if not candidate.valid:
                from .errors import from_code
                raise from_code(candidate.failure_class, candidate.reason)
            session_source_path = path
            import_method = "ready_session"
            dc_id, server_address = candidate.dc_id, candidate.server_address
            stage = Stage.SESSION_SELECTED
            # TPILOT PROBE-WIRING FIX 20260719: durably persist session_source_path
            # (the REAL candidate that session_inspector just validated) immediately
            # -- before proceeding to identity_checking/client_factory/probe_identity
            # -- so an interruption at any later point can locate and scrub it. This
            # is also, unrelated to cleanup, the SAME path now threaded into
            # client_factory(session_source_path) below (no separate/decoy probe file).
            selected_merged = _merge_result_json(operation_id, {
                "session_source_path": session_source_path, "work_root": work_root,
                "dc_id": dc_id, "server_address": server_address,
            }, db_path=db_path)
            storage.tdata_import_advance_stage(
                operation_id, Stage.SESSION_DETECTED, Stage.SESSION_SELECTED,
                fields={"import_method": import_method, "result_json": json.dumps(selected_merged)}, db_path=db_path,
            )
        else:
            dest = os.path.join(work_root, "converted", "session.session")
            candidate = tdata_adapter.convert_tdata_to_session(path, dest, known_session_paths=known_session_paths)
            session_source_path = dest
            import_method = "tdata_converted"
            dc_id, server_address = candidate.dc_id, candidate.server_address
            stage = Stage.TDATA_CONVERTED
            # TPILOT PROBE-WIRING FIX 20260719: see the matching comment in the
            # ready_session branch above -- same early-persistence rationale.
            converted_merged = _merge_result_json(operation_id, {
                "session_source_path": session_source_path, "work_root": work_root,
                "dc_id": dc_id, "server_address": server_address,
            }, db_path=db_path)
            storage.tdata_import_advance_stage(
                operation_id, Stage.SESSION_DETECTED, Stage.TDATA_CONVERTED,
                fields={"import_method": import_method, "result_json": json.dumps(converted_merged)}, db_path=db_path,
            )

        prior_stage = Stage.SESSION_SELECTED if import_method == "ready_session" else Stage.TDATA_CONVERTED
        stage = Stage.SESSION_VALIDATED
        storage.tdata_import_advance_stage(operation_id, prior_stage, Stage.SESSION_VALIDATED, db_path=db_path)

        stage = Stage.IDENTITY_CHECKING
        storage.tdata_import_advance_stage(operation_id, Stage.SESSION_VALIDATED, Stage.IDENTITY_CHECKING, db_path=db_path)

        # TIMEOUT HARDENING 20260719: client creation/connect is the first network-
        # touching step of this leg -- a dead proxy socket must not hang start_import
        # forever. asyncio.wait_for cancels and awaits the inner coroutine on timeout
        # (no orphan task), and re-raising as RuntimeFailed keeps the existing
        # generic-Exception except-block below doing its normal cleanup+_fail path,
        # with a short, secret-free message (never proxy password/API hash/auth_key).
        #
        # PROBE-WIRING FIX 20260719: client_factory receives session_source_path --
        # the SAME file session_inspector/tdata_adapter just validated (the ready
        # .session's extracted path, or work_root/converted/session.session) --
        # never a separate/blank decoy file. Previously main.py hardcoded an
        # unrelated `work_root/probe.session` that was never written to, so
        # Telethon opened an empty session and silently minted a fresh,
        # unauthorized auth_key: connect succeeded, but get_me() then always
        # returned None -> session_unauthorized, regardless of whether the
        # source tdata/.session was actually valid.
        try:
            client = await asyncio.wait_for(
                client_factory(session_source_path), timeout=TDIMPORT_CLIENT_CONNECT_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            raise RuntimeFailed("client connection timed out") from None
        try:
            # SessionUnauthorized (get_me() returned None) and any other RPCError
            # raised BY probe_identity itself propagate through wait_for untouched --
            # only a genuine deadline expiry is caught here and reclassified. This is
            # what keeps get_me()-is-None -> session_unauthorized unchanged while a
            # hang -> runtime_failed.
            try:
                meta = await asyncio.wait_for(
                    identity_probe.probe_identity(
                        client, operation_id=operation_id,
                        duplicate_check=duplicate_check, reserved_check=reserved_check,
                    ),
                    timeout=TDIMPORT_IDENTITY_PROBE_TIMEOUT_SEC,
                )
            except asyncio.TimeoutError:
                raise RuntimeFailed("identity probe timed out") from None
        finally:
            # A disconnect that never returns must not hang cleanup or overwrite an
            # already-obtained successful result -- bounded and swallowed exactly
            # like the pre-existing disconnect exception handling (asyncio.TimeoutError
            # IS an Exception subclass, so the same bare `except Exception: pass`
            # below already covers both a raised error AND a bounded timeout).
            disconnect = getattr(client, "disconnect", None)
            if disconnect is not None:
                try:
                    result = disconnect()
                    if hasattr(result, "__await__"):
                        await asyncio.wait_for(result, timeout=TDIMPORT_DISCONNECT_TIMEOUT_SEC)
                except Exception:  # noqa: BLE001
                    pass

        stage = Stage.IDENTITY_VERIFIED
        merged = _merge_result_json(operation_id, {
            "session_source_path": session_source_path,
            "work_root": work_root,
            "dc_id": dc_id,
            "server_address": server_address,
        }, db_path=db_path)
        storage.tdata_import_advance_stage(
            operation_id, Stage.IDENTITY_CHECKING, Stage.IDENTITY_VERIFIED,
            fields={
                "tg_user_id": meta.tg_user_id, "username": meta.username,
                "masked_phone": meta.masked_phone, "display_name": meta.display_name,
                "identity_verified_at": storage._now_iso(),
                "result_json": json.dumps(merged),
            },
            db_path=db_path,
        )

        row = storage.tdata_import_get(operation_id, db_path=db_path)
        return _safe_status_dict(row)

    except Exception as exc:  # noqa: BLE001
        # On ANY failure during start_import, no manager session was touched
        # (install happens only in confirm_install), so it is always safe to
        # scrub every scratch artifact now: the extracted archive contents
        # (a ready .session, or a tdata-converted session holding a real
        # auth_key -- both credential material) under work_root, plus the
        # uploaded ZIP if extraction never reached the cleanup point above.
        # TRUTHFUL CLEANUP FIX 20260719: cleanup_done must mean the known
        # artifacts are ACTUALLY gone -- only mark it when both removals report
        # success (never-existed counts as success; a failed delete does not).
        upload_clean = cleanup.cleanup_upload(archive_path)
        work_root_clean = cleanup.cleanup_work_root(work_root)
        result = _fail(operation_id, exc, stage=stage, db_path=db_path)
        if upload_clean and work_root_clean:
            storage.tdata_import_mark_cleanup(operation_id, db_path=db_path)
        return result


def get_status(*, operation_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    row = storage.tdata_import_get(operation_id, db_path=db_path)
    if not row:
        from .errors import StaleOperation
        return _fail(operation_id, StaleOperation("operation not found"), db_path=db_path)
    return _safe_status_dict(row)


async def confirm_install(
    *,
    operation_id: str,
    base_dir: str,
    proxy_row: Dict[str, Any],
    spawn_runtime: SpawnRuntimeFn,
    wait_runtime_ready: WaitRuntimeReadyFn,
    stop_runtime: Optional[StopRuntimeFn] = None,
    is_runtime_running: Optional[IsRuntimeRunningFn] = None,
    proxy_prober=None,
    backup_dir: Optional[str] = None,
    db_path: Optional[str] = None,
    stop_confirm_max_attempts: int = 5,
    stop_confirm_poll_sec: float = 1.0,
) -> Dict[str, Any]:
    """Cross the point of no return: re-verify the proxy fresh, STOP the
    manager's existing runtime if one is running and CONFIRM it actually
    stopped, atomically install the validated session, start the runtime, and
    wait for it to confirm readiness. Any failure here rolls back the session
    install (and best-effort stops a partially-started runtime) -- the
    operation moves to status='error' but the target manager is left exactly
    as it was before this call.

    TPILOT AUTH SAFETY 20260809 (Ф3, D-INSTALL): a manager can already have a
    live, authorized session before this operation (relogin/replace an
    EXISTING manager's account via tdata/.session import, not just fresh
    add-manager onboarding). On Windows the live .session SQLite file can be
    held open by that manager's own running process -- installer.install_
    session's os.replace() is not safe/reliable against an open handle. This
    function now:
      1. checks is_runtime_running(manager_key) BEFORE touching anything;
      2. if running, calls stop_runtime(manager_key) and then POLLS
         is_runtime_running again (stop_confirm_max_attempts times, every
         stop_confirm_poll_sec) until it confirms the process is gone;
      3. if the stop cannot be confirmed within that budget (or stop_runtime
         itself raises, or is_runtime_running is running=True but no
         stop_runtime was supplied), fails closed BEFORE installer.
         install_session is ever called -- the live session is never touched;
      4. records whether WE confirmed the stop (stop_confirmed_by_us) so a
         later rollback restarts the runtime ONLY when it is safe to do so
         (we are certain it was running and that we are the ones who stopped
         it) -- a manager that was already stopped before this operation is
         never spawned by a failed rollback.
    is_runtime_running=None (the default) preserves the EXACT prior behavior
    for callers that do not pass it (no stop-before-install, no confirm, no
    rollback-restart) -- existing callers/tests are unaffected until they
    opt in."""
    row = storage.tdata_import_get(operation_id, db_path=db_path)
    if not row:
        from .errors import StaleOperation
        return _fail(operation_id, StaleOperation("operation not found"), db_path=db_path)
    if row.get("status") != Status.PROCESSING or row.get("stage") != Stage.IDENTITY_VERIFIED:
        from .errors import ConcurrentOperation
        return _fail(operation_id, ConcurrentOperation(
            f"operation not ready to confirm (status={row.get('status')!r}, stage={row.get('stage')!r})"
        ), stage=row.get("stage"), db_path=db_path)

    manager_key = row["manager_key"]
    extra = {}
    if row.get("result_json"):
        try:
            extra = json.loads(row["result_json"])
        except (ValueError, TypeError):
            extra = {}
    session_source_path = extra.get("session_source_path")
    work_root = extra.get("work_root")

    stage = Stage.IDENTITY_VERIFIED
    install_result = None
    stop_confirmed_by_us = False
    try:
        if not session_source_path or not os.path.isfile(session_source_path):
            from .errors import SessionInstallFailed
            raise SessionInstallFailed("validated session scratch file is missing (stale operation?)")

        # Defense in depth: re-verify the proxy is STILL up right before the
        # point of no return (time may have passed waiting for admin confirm).
        proxy_gate.assert_proxy_mode_allowed(proxy_row.get("proxy_mode") or "")
        proxy_gate.verify_proxy(
            proxy_row.get("proxy_host") or "", proxy_row.get("proxy_port") or 0,
            proxy_row.get("proxy_username") or "", proxy_row.get("proxy_password") or "",
            prober=proxy_prober,
        )

        from manager_registry import build_manager_paths
        paths = build_manager_paths(base_dir, manager_key)
        final_path = paths["session_path"]

        # TPILOT AUTH SAFETY 20260809 (Ф3, D-INSTALL): stop-before-install,
        # fail closed if the stop cannot be confirmed. Deliberately placed
        # AFTER the proxy re-verify (no point disrupting a running manager
        # for an operation that is about to fail anyway) and BEFORE any
        # session_installing stage transition/install attempt.
        was_runtime_running = False
        if is_runtime_running is not None:
            try:
                was_runtime_running = bool(await _maybe_await(is_runtime_running(manager_key)))
            except Exception:  # noqa: BLE001
                was_runtime_running = False
        if was_runtime_running:
            if stop_runtime is None:
                from .errors import RuntimeFailed
                raise RuntimeFailed("manager runtime is running and no stop_runtime was provided")
            try:
                await _maybe_await(stop_runtime(manager_key))
            except Exception as e:  # noqa: BLE001
                from .errors import RuntimeFailed
                raise RuntimeFailed(f"failed to stop existing manager runtime: {type(e).__name__}") from e
            for _ in range(max(1, int(stop_confirm_max_attempts))):
                try:
                    still_running = bool(await _maybe_await(is_runtime_running(manager_key)))
                except Exception:  # noqa: BLE001
                    still_running = True  # unknown state -> treat as still running, fail closed
                if not still_running:
                    stop_confirmed_by_us = True
                    break
                await asyncio.sleep(max(0.0, float(stop_confirm_poll_sec)))
            if not stop_confirmed_by_us:
                from .errors import RuntimeFailed
                raise RuntimeFailed("could not confirm manager runtime stopped before install")

        stage = Stage.SESSION_INSTALLING
        storage.tdata_import_advance_stage(operation_id, Stage.IDENTITY_VERIFIED, Stage.SESSION_INSTALLING, db_path=db_path)

        install_result = installer.install_session(session_source_path, final_path, backup_dir=backup_dir)

        stage = Stage.SESSION_INSTALLED
        storage.tdata_import_advance_stage(operation_id, Stage.SESSION_INSTALLING, Stage.SESSION_INSTALLED, db_path=db_path)

        stage = Stage.RUNTIME_STARTING
        storage.tdata_import_advance_stage(operation_id, Stage.SESSION_INSTALLED, Stage.RUNTIME_STARTING, db_path=db_path)
        await _maybe_await(spawn_runtime(manager_key))

        ready = await wait_runtime_ready(manager_key)
        if not ready:
            from .errors import RuntimeFailed
            raise RuntimeFailed("runtime did not confirm readiness in time")

        stage = Stage.RUNTIME_RUNNING
        storage.tdata_import_advance_stage(operation_id, Stage.RUNTIME_STARTING, Stage.RUNTIME_RUNNING, db_path=db_path)

        storage.tdata_import_complete(operation_id, result_json=json.dumps(extra), db_path=db_path)

        if work_root:
            if cleanup.cleanup_work_root(work_root):
                storage.tdata_import_mark_cleanup(operation_id, db_path=db_path)

        row = storage.tdata_import_get(operation_id, db_path=db_path)
        return _safe_status_dict(row)

    except Exception as exc:  # noqa: BLE001
        # TPILOT AUTH SAFETY 20260809 (Ф3, D-INSTALL): rollback restores the OLD
        # session (installer.rollback_install, unchanged) and restarts the
        # runtime -- but ONLY when stop_confirmed_by_us is True, i.e. we are
        # certain the runtime was running before this call AND that we are the
        # ones who stopped it (never spawns a manager that was already stopped
        # on purpose, and never spawns when the stop itself could not be
        # confirmed -- see the raise above, which is reached with
        # stop_confirmed_by_us still False and install_result still None, so
        # NEITHER branch below fires in that case: fail closed, nothing touched,
        # nothing (re)started).
        rollback_note = ""
        if install_result is not None and stage in (Stage.SESSION_INSTALLED, Stage.RUNTIME_STARTING):
            if stop_runtime is not None:
                try:
                    await _maybe_await(stop_runtime(manager_key))
                except Exception:  # noqa: BLE001
                    pass
            installer.rollback_install(install_result)
            if stop_confirmed_by_us:
                try:
                    await _maybe_await(spawn_runtime(manager_key))
                except Exception as respawn_exc:  # noqa: BLE001
                    rollback_note = f" (rollback restart also failed: {type(respawn_exc).__name__})"
        elif stop_confirmed_by_us and install_result is None:
            # Stop was confirmed, but something failed before install_session
            # was ever attempted (e.g. the durable stage-advance write itself
            # raised) -- nothing was installed, so there is nothing to roll
            # back at the session-file level, but the runtime we stopped must
            # still be restored.
            try:
                await _maybe_await(spawn_runtime(manager_key))
            except Exception as respawn_exc:  # noqa: BLE001
                rollback_note = f" (rollback restart also failed: {type(respawn_exc).__name__})"
        result = _fail(operation_id, exc, stage=stage, db_path=db_path)
        if rollback_note:
            result["error_text"] = str(result.get("error_text") or "") + rollback_note
        return result


def cancel_import(*, operation_id: str, db_path: Optional[str] = None) -> Dict[str, Any]:
    row = storage.tdata_import_get(operation_id, db_path=db_path)
    if not row:
        from .errors import StaleOperation
        return _fail(operation_id, StaleOperation("operation not found"), db_path=db_path)
    if row.get("status") not in (Status.CREATED, Status.PROCESSING):
        from .errors import ConcurrentOperation
        return _fail(operation_id, ConcurrentOperation(
            f"operation already terminal (status={row.get('status')!r})"
        ), db_path=db_path)

    extra = {}
    if row.get("result_json"):
        try:
            extra = json.loads(row["result_json"])
        except (ValueError, TypeError):
            extra = {}

    ok = storage.tdata_import_cancel(operation_id, db_path=db_path)
    # TRUTHFUL CLEANUP FIX 20260719: only claim cleanup_done=1 when the known
    # credential artifacts are verifiably gone (never-existed counts as gone;
    # a failed delete does not) -- matches the pattern confirm_install's own
    # success path already uses (cleanup.cleanup_work_root(...) gates the mark).
    work_root_clean = cleanup.cleanup_work_root(extra.get("work_root"))
    scratch_clean = cleanup.cleanup_scratch_session(extra.get("session_source_path"))
    if work_root_clean and scratch_clean:
        storage.tdata_import_mark_cleanup(operation_id, db_path=db_path)

    row = storage.tdata_import_get(operation_id, db_path=db_path)
    result = _safe_status_dict(row) if row else {"operation_id": operation_id}
    result["ok"] = bool(ok)
    return result


def sweep_stale(*, now_iso: Optional[str] = None, db_path: Optional[str] = None) -> int:
    """Controller-side hygiene sweep (pure/offline-testable -- no telethon, no
    network). Finds expired, still-active import operations that are at/below
    identity_verified (awaiting the admin's confirm tap -- NEVER one currently
    installing/cutting over) and:
      1. scrubs their credential-material scratch dirs (extracted .session /
         tdata-converted session under work_root, plus any scratch session);
      2. flips them to status='error' (error_class='stale_operation'), which
         releases the one-active-per-manager unique-index lock so the manager
         can be re-imported.

    Without this, an abandoned/interrupted import (server restart or admin
    walking away between start_import and confirm_install) would leave a
    decrypted full-account session on disk indefinitely AND permanently block
    that manager from ever being re-imported. Returns the number swept.

    now_iso defaults to storage's UTC clock (matches how expires_at is set)."""
    now = now_iso or storage._now_iso()
    swept = 0
    for row in storage.tdata_import_list_stale(now, db_path=db_path):
        if str(row.get("stage") or "") not in _SWEEPABLE_STAGES:
            continue  # mid-install / cutover: never auto-swept
        extra: Dict[str, Any] = {}
        if row.get("result_json"):
            try:
                extra = json.loads(row["result_json"])
            except (ValueError, TypeError):
                extra = {}
        # TRUTHFUL CLEANUP FIX 20260719: the sweep itself (moving the op to
        # error/stale_operation, releasing the manager slot) is independent of
        # whether file deletion actually succeeded -- swept still counts the op
        # as swept either way -- but cleanup_done must only be set when both
        # removals report success, same rule as cancel_import/start_import above.
        work_root_clean = cleanup.cleanup_work_root(extra.get("work_root"))
        scratch_clean = cleanup.cleanup_scratch_session(extra.get("session_source_path"))
        if storage.tdata_import_fail(
            row["operation_id"], error_class=FailureClass.STALE_OPERATION,
            error_text="operation expired before confirmation", db_path=db_path,
        ):
            if work_root_clean and scratch_clean:
                storage.tdata_import_mark_cleanup(row["operation_id"], db_path=db_path)
            swept += 1
    return swept
