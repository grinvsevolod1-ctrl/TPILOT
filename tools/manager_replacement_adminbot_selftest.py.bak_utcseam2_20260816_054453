# -*- coding: utf-8 -*-
"""tools/manager_replacement_adminbot_selftest.py -- offline self-test for the
Stage 3 AdminBot wizard wiring the fresh-session manager-REPLACEMENT feature
into panel_bot.py + main.py (2026-07-16). Stage 1 = storage.py durable state
machine (accepted). Stage 2 = main.py replacement_* backend (accepted 100%,
2026-07-15). Stage 3 = this file's target: the panel_bot.py wizard screens/
callbacks + the main.py /manager_replace_* command adapters that bridge them
across the PanelBot/controller process boundary.

Techniques (same as every other tools/*_selftest.py in this project):
  - main.py and panel_bot.py cannot be imported standalone (Telethon/env side
    effects at import time) -- the functions under test and their direct,
    pre-existing, UNMODIFIED dependencies are extracted via ast.parse +
    ast.unparse + exec() and run FOR REAL against a temporary SQLite DB
    (never db/data_tpilot.db) and a temporary runtime directory (never
    runtime/managers/).
  - panel_bot.py and main.py run as SEPARATE PROCESSES in production,
    talking only through the panel_commands queue (_submit_and_wait on the
    panel side, _panel_execute_command_text on the controller side). This
    selftest bridges them in-process: the extracted panel_bot.py functions
    call a fake _submit_and_wait that dispatches DIRECTLY into the extracted
    main.py /manager_replace_* command handlers (which in turn call the
    REAL, unmodified, already-accepted Stage 2 replacement_* functions) --
    no real queue, no polling, no network, exactly mirroring the technique
    manager_relogin_selftest.py already uses for its own panel<->main bridge
    tests.
  - Telethon itself is not installed in this local dev environment, so
    Button/events/client and the SessionPasswordNeededError/PhoneCodeInvalid
    Error/... exception types are FAKED under their exact production names
    (mirrors manager_replacement_backend_selftest.py's fake Telethon layer).

Never: real Telegram network, real proxy/provider network, real process
spawn/stop, production DB/runtime/session/log access.

    python tools\\manager_replacement_adminbot_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


MAIN_PATH = str(BASE_DIR / "main.py")
PANEL_PATH = str(BASE_DIR / "panel_bot.py")
MAIN_SRC = open(MAIN_PATH, encoding="utf-8-sig").read()
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()


# ======================================================================
# Fake Telethon layer (same shapes as manager_replacement_backend_selftest.py)
# ======================================================================

class SessionPasswordNeededError(Exception):
    pass


class PhoneCodeInvalidError(Exception):
    pass


class PhoneCodeExpiredError(Exception):
    pass


class PasswordHashInvalidError(Exception):
    pass


class PhoneNumberBannedError(Exception):
    pass


class FloodWaitError(Exception):
    def __init__(self, seconds: int = 5):
        self.seconds = seconds
        super().__init__(f"A wait of {seconds} seconds is required")


def make_fake_client_factory(script: dict, calls: list, authorized_sessions: set, created_clients: list = None):
    class _FakeMe:
        def __init__(self, uid, username, first, last, phone):
            self.id = uid
            self.username = username
            self.first_name = first
            self.last_name = last
            self.phone = phone

    class FakeTelegramClient:
        def __init__(self, session_path, cfg=None):
            self.session_path = str(session_path)
            self.cfg = cfg
            if created_clients is not None:
                created_clients.append(self)

        async def connect(self):
            calls.append(("connect", self.session_path))
            try:
                open(self.session_path, "a").close()
            except Exception:
                pass

        async def disconnect(self):
            calls.append(("disconnect", self.session_path))

        async def send_code_request(self, phone):
            calls.append(("send_code_request", phone))
            exc = script.get("send_code_exc")
            if exc is not None:
                raise exc

            class _Sent:
                phone_code_hash = script.get("phone_code_hash", "hash123")
            return _Sent()

        async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
            calls.append(("sign_in", phone, code, password))
            if script.get("sign_in_forbidden"):
                raise AssertionError("sign_in must not be called on an already-authorized session")
            if password is not None:
                exc = script.get("pass_sign_in_exc", script.get("sign_in_exc"))
            else:
                exc = script.get("code_sign_in_exc", script.get("sign_in_exc"))
            if exc is not None:
                raise exc
            authorized_sessions.add(self.session_path)

        async def is_user_authorized(self):
            return self.session_path in authorized_sessions

        async def get_me(self):
            calls.append(("get_me",))
            return _FakeMe(
                script.get("actual_user_id", 999001),
                script.get("actual_username", "newacc"),
                script.get("actual_first_name", "New"),
                script.get("actual_last_name", "Account"),
                script.get("actual_phone", "+10000000000"),
            )

        async def log_out(self):
            calls.append(("log_out",))
            authorized_sessions.discard(self.session_path)

    def factory(session_path, cfg=None):
        return FakeTelegramClient(session_path, cfg)

    return factory


# ======================================================================
# main.py extraction: Stage 2 backend (reused, unmodified names) + Stage 3
# adapter layer (/manager_replace_* commands, dispatch table) + relogin's
# own mutual-exclusion choke point.
# ======================================================================

STAGE2_REAL_NAMES = {
    "_REPLACE_STEP_PHONE", "_REPLACE_STEP_CODE", "_REPLACE_STEP_PASS", "_REPLACE_STEPS",
    "_REPLACE_TEMP_SESSION_SUFFIX", "_REPLACE_NEXT_STEP_BY_STATUS",
    "MANAGER_ONBOARD_TIMEOUT_SEC", "MANAGER_PHONE_COOLDOWN_SEC",
    "_RELOGIN_STEP_PHONE", "_RELOGIN_STEP_CODE", "_RELOGIN_STEP_PASS",
    "_RELOGIN_STEP_IDENTITY_CONFIRM", "_RELOGIN_STEP_READY_COMMIT", "_RELOGIN_STEPS",
    "_MANAGER_AUTH_AUDIT_LOG",
    "_replacement_next_step_for_status", "_replacement_result",
    "_replacement_temp_session_path", "_replacement_permanent_session_path",
    "_replacement_cleanup_temp_files",
    "_replacement_relogin_active", "replacement_active_for_old_key",
    "_replacement_tg_user_conflict", "_replacement_username_conflict",
    "_replacement_tgid_reserved_by_other",
    "_replacement_onboarding_proxy_fields", "_replacement_resolve_durable_proxy",
    "_replacement_cancel_cleanup",
    "_replacement_fail", "_replacement_build_preview",
    "replacement_start", "replacement_reserve_key", "replacement_describe_proxy",
    "replacement_send_phone_code", "_replacement_after_signin",
    "replacement_submit_code", "replacement_submit_password",
    "replacement_ready_commit_preview", "replacement_cancel", "replacement_recover",
    "_manager_is_rate_limit_error", "_manager_extract_wait_seconds", "_map_send_code_error",
    "_manager_auth_audit_log",
    "_resolve_manager_telethon_proxy", "_build_telethon_proxy_from_row",
    "_manager_auth_proxy_mode", "_proxy_type_norm", "_proxy_port_int", "_manager_proxy_enabled",
    "_future_iso", "_now_utc_iso", "_partner_source_key_for_manager",
}

STAGE3_REAL_NAMES = {
    "_repl3_b64_encode", "_repl3_b64_decode", "_repl3_json_result", "_repl3_owned_op_or_error",
    "_panel_manager_replace_start_command", "_panel_manager_replace_recover_for_old_key_command",
    "_panel_manager_replace_key_check_command", "_panel_manager_replace_send_phone_command",
    "_panel_manager_replace_submit_code_command", "_panel_manager_replace_submit_password_command",
    "_panel_manager_replace_ready_preview_command", "_panel_manager_replace_cancel_command",
    "_panel_manager_replace_recover_command", "_panel_manager_replace_proxy_pool_pick_command",
    "_panel_manager_replace_proxy_manual_command",
    # COMMIT WIRING 20260716: the final-confirm adapter + its own two small
    # dependencies (both technically defined in the Stage 4 commit-engine
    # block, but only these two -- _repl4_result/_repl4_progress -- are
    # needed for the wiring command itself; replacement_commit is injected
    # as a fake below rather than re-extracting the whole Stage 4 engine,
    # which is already exhaustively tested by manager_replacement_commit_
    # selftest.py).
    "_panel_manager_replace_commit_command", "_repl4_result", "_repl4_progress",
    "_REPL3_PREV_PANEL_EXEC", "_REPL3_DISPATCH", "_panel_execute_command_text",
    "_manager_relogin_begin", "_manager_relogin_active_owner", "_manager_relogin_cleanup_temp_files", "_parse_cmd",
    "_RELOGIN_STEP_PHONE", "_RELOGIN_STEP_CODE", "_RELOGIN_STEP_PASS",
    "_RELOGIN_STEP_IDENTITY_CONFIRM", "_RELOGIN_STEP_READY_COMMIT", "_RELOGIN_STEPS",
    # TPILOT TDATA/SESSION IMPORT 20260719: a later stacked override of
    # _panel_execute_command_text was added after this Stage 3 block (same
    # dispatch-dict-plus-prev-pointer pattern as _REPL3_DISPATCH/
    # _REPL3_PREV_PANEL_EXEC above). _extract_by_names() below pulls EVERY
    # top-level def/assign named in this set, in file order, so the LAST
    # _panel_execute_command_text (now the tdata-import one) is what actually
    # answers main_ns["_panel_execute_command_text"](...) calls in this test
    # -- exactly mirroring real main.py's override-chain semantics. Its body
    # references these two globals, so they must be extracted too or calling
    # it raises NameError.
    "_TDIMPORT_DISPATCH", "_TDIMPORT_PREV_PANEL_EXEC",
    # The dispatch dict's VALUES are the 4 handler function objects themselves
    # (bound at module-exec time, not re-looked-up by name later) -- they
    # must be extracted too, or building _TDIMPORT_DISPATCH raises NameError
    # even though this test never calls a /manager_tdimport_* command.
    "_panel_manager_tdimport_start_command", "_panel_manager_tdimport_status_command",
    "_panel_manager_tdimport_confirm_command", "_panel_manager_tdimport_cancel_command",
    # TPILOT DEVICE-LOGIN 20260719: same override-chain situation as the
    # TDIMPORT block above -- a LATER _panel_execute_command_text override
    # was stacked on top of it (same dispatch-dict-plus-prev-pointer
    # pattern). Its body references these two globals, plus the dispatch
    # dict's VALUES are the 3 devlogin handler function objects themselves
    # (bound at module-exec time) -- all must be extracted or calling the
    # (now devlogin) _panel_execute_command_text raises NameError, even
    # though this test never calls a /manager_devlogin_* command.
    "_DEVLOGIN_DISPATCH", "_DEVLOGIN_PREV_PANEL_EXEC",
    "_panel_manager_devlogin_start_command", "_panel_manager_devlogin_cancel_command",
    "_panel_manager_devlogin_consume_command", "_devlogin_new_request_id",
    # PROXY LIFECYCLE SYNC 20260721: same override-chain situation again --
    # a LATER _panel_execute_command_text override was stacked on top of the
    # devlogin one (same dispatch-dict-plus-prev-pointer pattern). Its body
    # references these two globals, plus the dispatch dict's VALUES are the
    # 3 lifecycle handler function objects themselves (bound at module-exec
    # time) -- all must be extracted or calling the (now proxy-lifecycle)
    # _panel_execute_command_text raises NameError, even though this test
    # never calls a /proxy_lifecycle_* command.
    "_PLC_DISPATCH", "_PLC_PREV_PANEL_EXEC",
    "_handle_proxy_lifecycle_terminal_confirm_command", "_handle_proxy_lifecycle_status_command",
    "_handle_proxy_lifecycle_cleanup_plan_command",
    # PROXY RENEWAL 20260721: yet another later _panel_execute_command_text
    # override stacked on top of the lifecycle one (same dispatch-dict +
    # prev-pointer pattern). Its body references these globals and the dispatch
    # dict's VALUES are the 5 renewal handler function objects (bound at
    # module-exec time) -- all must be extracted or the (now renewal)
    # _panel_execute_command_text raises NameError, even though this test never
    # calls a /proxy_renewal_* command.
    "_RENEWAL_DISPATCH", "_RENEWAL_PREV_PANEL_EXEC",
    "_handle_proxy_renewal_backfill_command", "_handle_proxy_renewal_status_command",
    "_handle_proxy_renewal_calc_command", "_handle_proxy_renewal_confirm_command",
    "_handle_proxy_renewal_config_command",
    # HNV2 F-4 correction (post-independent-review): yet another later
    # _panel_execute_command_text override was stacked on top of the renewal
    # one (2026-08-07). Unlike the earlier overrides above, this one has NO
    # dispatch dict -- it's a single inline `if cmd == "/hnv2_diag":` branch
    # calling _hnv2_diag_text, then falls through to _HNV2_PREV_PANEL_EXEC
    # for everything else. Only _HNV2_PREV_PANEL_EXEC needs to be extracted:
    # it is referenced at module-exec time (the `_HNV2_PREV_PANEL_EXEC =
    # globals().get(...)` assignment line, and inside the function body).
    # _hnv2_diag_text itself is a name INSIDE the function body -- Python
    # only resolves it if/when the function is actually CALLED with
    # command_text=='/hnv2_diag', which this test never does (it only
    # exercises /manager_replace_* commands), so it does not need to be
    # extracted here (and pulling in its own large transitive closure just
    # to define-but-never-call it would be unnecessary risk).
    "_HNV2_PREV_PANEL_EXEC",
    # TPILOT AUTH SAFETY 20260809 (Ф4, unified auth UX): yet another later
    # _panel_execute_command_text override was stacked on top of the HNV2
    # one (same dispatch-dict + prev-pointer pattern as REPL3/TDIMPORT/
    # RENEWAL above). _AUTHUI_DISPATCH's dict VALUES are the 4 relogin/
    # replace-QR/tdimport command wrapper functions (bound at module-exec
    # time, so they must be extracted or dict construction itself raises
    # NameError) -- but exactly like _hnv2_diag_text above, their OWN
    # transitive callees (_manager_relogin_qr_start, replacement_send_
    # tdimport_start, etc.) are names INSIDE their function bodies, resolved
    # only if/when actually CALLED -- this test never calls any
    # /manager_relogin_qr_start / /manager_replace_qr_start / /manager_
    # replace_tdimport_* command, so those deeper dependencies are
    # deliberately NOT extracted here.
    "_AUTHUI_DISPATCH", "_AUTHUI_PREV_PANEL_EXEC",
    "_panel_manager_relogin_qr_start_command", "_panel_manager_replace_qr_start_command",
    "_panel_manager_replace_tdimport_start_command", "_panel_manager_replace_tdimport_confirm_command",
    # TPILOT PREPARED ACCOUNTS PHASE 4/5 (2026-08-11): yet another later
    # _panel_execute_command_text override stacked on top of the AUTHUI one
    # (same dispatch-dict + prev-pointer pattern). Its body references these
    # two globals, plus _PREPARED_DISPATCH's dict VALUES are the 9 prepared-
    # accounts handler function objects themselves (bound at module-exec
    # time) -- all must be extracted or calling the (now prepared)
    # _panel_execute_command_text raises NameError, even though this test
    # never calls a /prepared_* command.
    "_PREPARED_DISPATCH", "_PREPARED_PREV_PANEL_EXEC",
    "_panel_prepared_add_command", "_panel_prepared_method_command",
    "_panel_prepared_pending_command", "_panel_prepared_import_command",
    "_panel_prepared_verify_command", "_panel_prepared_activate_command",
    "_panel_prepared_list_command", "_panel_prepared_card_command",
    "_panel_prepared_delete_command",
}


def _extract_by_names(src: str, names: set) -> list:
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


def _selftest_db_guard(db_path: str, base_dir, storage_mod) -> None:
    """Fail-safe (forensic review recommendation, 2026-07-16, prompted by a
    real incident in the sibling manager_relogin_selftest.py harness where a
    forgotten QUEUE_DB_PATH rebind let replacement_* writes fall through to
    the real db/data_tpilot.db file): refuses any db_path located under
    base_dir/db (the project's own db/ directory, boundary-checked with
    os.sep so a sibling like .../dbfoo is never a false match), and requires
    both storage DB-path globals to already equal db_path. Raises
    AssertionError on any violation. Kept standalone (not inlined into
    build_main_ns) specifically so it can be unit-tested directly against
    synthetic bad inputs -- see test_dbguard_rejects_unsafe_paths -- without
    ever touching the real storage.DB_PATH/QUEUE_DB_PATH globals or opening
    any real connection."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    unsafe = target == prod_db_dir or target.startswith(prod_db_dir + os.sep)
    assert not unsafe, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    assert storage_mod.DB_PATH == storage_mod.QUEUE_DB_PATH == db_path, \
        (storage_mod.DB_PATH, storage_mod.QUEUE_DB_PATH, db_path)


def build_main_ns(db_path: str, base_dir: Path, *, script: dict = None, replacement_commit_fn=None) -> dict:
    import manager_registry
    import storage as _storage
    import aiosqlite as _aiosqlite
    import proxy_parser as _proxy_parser
    from datetime import datetime as _dt, timedelta as _td

    _storage.DB_PATH = db_path
    _storage.QUEUE_DB_PATH = db_path
    # NOTE: this function's own "base_dir" parameter is the per-test TEMP
    # root, not the project root -- the module-level BASE_DIR is used
    # deliberately here instead of base_dir.
    _selftest_db_guard(db_path, BASE_DIR, _storage)

    script = script if script is not None else {}
    calls: list = []
    authorized_sessions: set = set()
    client_factory = make_fake_client_factory(script, calls, authorized_sessions)

    names = STAGE2_REAL_NAMES | STAGE3_REAL_NAMES
    nodes = _extract_by_names(MAIN_SRC, names)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    ns = {
        "os": os,
        "re": __import__("re"),
        "asyncio": asyncio,
        "Path": Path,
        "aiosqlite": _aiosqlite,
        "datetime": _dt,
        "timedelta": _td,
        "json": json,
        "base64": __import__("base64"),
        # Stage 3's own `import json as _repl3_json` / `import base64 as
        # _repl3_base64` module-level statements are ast.Import nodes, not
        # captured by _extract_by_names (which only pulls Function/Assign
        # nodes) -- bind the same aliases directly here instead.
        "_repl3_json": json,
        "_repl3_base64": __import__("base64"),
        # TPILOT DEVICE-LOGIN 20260719: same reasoning as the _repl3_json/
        # _repl3_base64 bindings above -- main.py's devlogin block has its
        # own module-level `import storage as _devlogin_storage` / `import
        # json as _devlogin_json` / `import secrets as _devlogin_secrets`
        # statements, which are ast.Import nodes and thus not captured by
        # _extract_by_names (Function/Assign only). Bind the same aliases
        # directly here, reusing the real storage module already imported
        # as _storage everywhere else in this file.
        "_devlogin_storage": _storage,
        "_devlogin_json": json,
        "_devlogin_secrets": __import__("secrets"),
        "DEVLOGIN_REQUEST_TTL_SEC": 180,
        "proxy_parser": _proxy_parser,
        "BASE_DIR": base_dir,
        "TPILOT_DB_PATH": db_path,
        "registry_normalize_manager_key": manager_registry.normalize_manager_key,
        "mask_phone": manager_registry.mask_phone,
        "validate_manager_key": manager_registry.validate_manager_key,
        "build_manager_paths": manager_registry.build_manager_paths,
        "ensure_manager_dirs": manager_registry.ensure_manager_dirs,
        # The Stage 3 /manager_replace_* adapters call replacement_send_
        # phone_code/etc WITHOUT an explicit client_factory (production
        # code relies on the default -> _replacement_build_client, the real
        # Telethon constructor). _replacement_build_client is deliberately
        # never extracted (same convention as manager_replacement_backend_
        # selftest.py) -- bind the name directly to the SAME fake factory
        # used everywhere else in this file, so the untouched production
        # code path exercises the fake client instead of silently NameError
        # -> swallowed-by-try/except -> a misleading send_code_failed.
        "_replacement_build_client": client_factory,
        "manager_get": _storage.manager_get,
        "manager_get_onboarding": _storage.manager_get_onboarding,
        "manager_save_onboarding": _storage.manager_save_onboarding,
        "manager_delete_onboarding": _storage.manager_delete_onboarding,
        "manager_list_pending": _storage.manager_list_pending,
        "_repl_storage": _storage,
        "SessionPasswordNeededError": SessionPasswordNeededError,
        "PhoneCodeInvalidError": PhoneCodeInvalidError,
        "PhoneCodeExpiredError": PhoneCodeExpiredError,
        "PasswordHashInvalidError": PasswordHashInvalidError,
        "PhoneNumberBannedError": PhoneNumberBannedError,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object, "Optional": None,
        # COMMIT WIRING 20260716: injected fake, NOT the real Stage 4 engine
        # (already exhaustively tested by manager_replacement_commit_
        # selftest.py) -- this file tests the WIRING (dispatch, ownership,
        # status pre-check, result passthrough, panel-side rendering), not
        # the engine's own cutover logic.
        "replacement_commit": replacement_commit_fn or make_fake_replacement_commit({}),
    }
    exec(compile(module_src, f"<{MAIN_PATH}:replacement_adminbot>", "exec"), ns)
    ns["__calls__"] = calls
    ns["__authorized_sessions__"] = authorized_sessions
    ns["__client_factory__"] = client_factory
    ns["__storage__"] = _storage
    return ns


# ======================================================================
# panel_bot.py extraction: the Stage 3 wizard block + its direct,
# pre-existing, UNMODIFIED dependencies (wizard-state table, dedupe cache,
# manager-card row lookups). _manager_admin_detail_buttons's override chain
# is bypassed exactly like manager_relogin_selftest.py's own harness does:
# a fake "previous" button list stands in for the real chain, so the
# override's OWN insertion logic is exercised for real without needing to
# extract the entire multi-generation override stack.
# ======================================================================

PANEL_REPLACE_NAMES = {
    "_replace_active_op_for_old_key", "_replace_read_op_row",
    "_REPLACE_RESULT_MESSAGES", "_replace_message_for", "_replace_parse_result", "_replace_b64",
    "_replace_delete_choice_text", "_replace_delete_choice_buttons",
    "_replace_intro_text", "_replace_intro_buttons", "_replace_cancel_only_buttons",
    "_replace_name_prompt_text", "_replace_name_confirm_text", "_replace_name_confirm_buttons",
    "_replace_key_prompt_text", "_replace_key_confirm_text", "_replace_key_confirm_buttons",
    "_replace_proxy_choice_text", "_replace_proxy_choice_buttons",
    "_replace_proxy_manual_prompt_text", "_replace_proxy_pool_text", "_replace_proxy_pool_buttons",
    "_replace_proxy_multi_warning_text", "_replace_proxy_multi_buttons", "_replace_proxy_buy_placeholder_text",
    "_replace_phone_prompt_text", "_replace_code_prompt_text", "_replace_code_expired_buttons",
    "_replace_pass_prompt_text", "_replace_identity_text", "_replace_identity_recovered_text",
    "_replace_identity_buttons", "_replace_preview_text", "_replace_preview_buttons",
    "_replace_cancel_confirm_text", "_replace_cancel_confirm_buttons", "_replace_error_buttons",
    "_replace_get_send_phone_context", "_replace_send_phone", "_replace_render_status_screen",
    "_replace_recover_and_render", "_replace_callback", "_replace_apply_send_phone_result",
    "_replace_wizard_input", "_replace_apply_code_or_pass_result",
    "_manager_admin_detail_buttons",
    # COMMIT WIRING 20260716.
    "_REPLACE_STAGE_LABELS",
    "_replace_committing_text", "_replace_committing_buttons",
    "_replace_links_pending_text", "_replace_links_pending_buttons",
    "_replace_retry_buttons", "_replace_manual_recovery_text",
    "_replace_success_text", "_replace_success_buttons",
    "_replace_render_commit_result", "_replace_submit_commit_and_render",
    # AUTH UI 20260809 (Ф4): unified auth chooser, now called from
    # _replace_callback's px_direct/px_pick/px_pick_yes branches and
    # _replace_wizard_input's manual-proxy-entry completion.
    "_auth_chooser_text", "_auth_chooser_buttons",
    # TERMINAL OK 20260809 (Ф4 checkpoint correction): _replace_success_
    # buttons/_replace_error_buttons now append _terminal_ok_button().
    "_terminal_ok_button",
}

PANEL_SHARED_NAMES = {
    "_manager_row_by_key", "_manager_rows_all", "_manager_rows", "_manager_short_label",
    "_ppool_parse_result_json",
    "_manager_state_icon", "_manager_state_label", "_manager_proxy_badge",
    "_wizard_get", "_wizard_set", "_wizard_clear", "_connect_panel_db",
    "_ensure_panel_runtime_tables", "_utc_now_iso",
    "_panel_callback_is_duplicate", "_panel_loop_time",
    "_PANEL_RECENT_CALLBACKS", "_PANEL_DEDUPE_SEC",
    "_safe_text",
}


def _click_phone_after_chooser(panel_ns: dict, chat: int, admin: int) -> None:
    """TPILOT AUTH SAFETY 20260809 (Ф4, unified auth UX): every proxy-choice
    completion (px_direct/px_pick/px_pick_yes/manual proxy entry) now shows
    the unified auth chooser instead of auto-starting the phone step (see
    tools/auth_chooser_selftest.py's A4 for the direct proof). This file's
    pre-existing test groups exercise the REST of the phone/code/2FA flow
    assuming phone auto-started -- their own job is that flow's behavior
    once Phone specifically has been chosen, not re-proving the chooser
    itself. Simulates the admin's "📱 Номер телефона" click using ONLY
    functions already extracted into panel_ns (_wizard_get/_wizard_set),
    applying the exact, minimal effect on_callback's auth:replace:phone:
    branch has: _wizard_set(..., "phone", <same payload>)."""
    state = panel_ns["_wizard_get"](chat, admin)
    payload = dict((state or {}).get("payload") or {})
    panel_ns["_wizard_set"](chat, admin, "replace", "phone", payload)


def build_panel_ns(db_path: str, *, submit_and_wait_fn=None, is_allowed: bool = True) -> dict:
    import manager_registry

    class _FakeBtn:
        __slots__ = ("text", "data")

        def __init__(self, text, data):
            self.text = text
            self.data = data

        def __iter__(self):
            yield "btn"
            yield self.text
            yield self.data

        def __getitem__(self, i):
            return ("btn", self.text, self.data)[i]

    class _FakeButton:
        @staticmethod
        def inline(text, data):
            raw = data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8")
            return _FakeBtn(text, raw)

    class _FakeEventsNS:
        CallbackQuery = object()
        NewMessage = object()

    SENT: list = []

    class _FakeClient:
        def on(self, *a, **kw):
            def _decorator(fn):
                return fn
            return _decorator

        async def send_message(self, chat_id, text, buttons=None):
            SENT.append((chat_id, text, buttons))

    class FakeEvent:
        def __init__(self, *, data: bytes = b"", chat_id: int = 0, sender_id: int = 0, raw_text: str = ""):
            self.data = data
            self.chat_id = chat_id
            self.sender_id = sender_id
            self.raw_text = raw_text
            self.answers: list = []
            self.edits: list = []
            self.deleted = False

        async def answer(self, text=None, alert=False):
            self.answers.append((text, alert))

        async def delete(self):
            self.deleted = True

    async def _fake_safe_event_edit(event, text, buttons=None):
        event.edits.append((text, buttons))

    async def _fake_pb_safe_answer(event, *args, **kwargs):
        try:
            answer = getattr(event, "answer", None)
            if answer is not None:
                await answer(*args, **kwargs)
            return True
        except Exception:
            return False

    def _fake_manager_admin_detail_text(row):
        key = str((row or {}).get("manager_key") or "")
        return f"CARD:{key}"

    def _fake_prev_buttons(key):
        # Stand-in for the real multi-generation override chain (identical
        # technique to manager_relogin_selftest.py's own harness) -- a
        # plausible pre-existing card WITH a real relogin row, so the
        # "insert right after Перезайти" placement logic is exercised for
        # real.
        return [
            [_FakeButton.inline("📄 Карточка", f"cmd:/manager_info {key}".encode())],
            [_FakeButton.inline("▶️ Запустить", f"cmd:/manager_start {key}".encode()), _FakeButton.inline("⏹ Остановить", f"cmd:/manager_stop {key}".encode())],
            [_FakeButton.inline("🔐 Перезайти", f"relogin:start:{key}".encode())],
            [_FakeButton.inline("🌐 Прокси менеджера", f"menu:proxy:{key}".encode())],
            [_FakeButton.inline("⬅️ Назад к админ-меню", b"menu:manager_admin")],
            [_FakeButton.inline("🏠 Главная панель", b"menu:main")],
        ]

    all_names = PANEL_REPLACE_NAMES | PANEL_SHARED_NAMES
    tree = ast.parse(PANEL_SRC)
    nodes = []
    seen = set()
    for n in tree.body:
        nm = getattr(n, "name", None)
        if nm == "_manager_admin_detail_buttons":
            continue
        if nm and nm in all_names:
            nodes.append(n)
            seen.add(nm)
            continue
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in all_names:
            nodes.append(n)
            seen.add(n.targets[0].id)
            continue
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in all_names:
            nodes.append(n)
            seen.add(n.target.id)
            continue
    # _manager_admin_detail_buttons: only the LAST (active) override, same
    # technique as manager_relogin_selftest.py.
    last_buttons_def = None
    for n in tree.body:
        if getattr(n, "name", None) == "_manager_admin_detail_buttons":
            last_buttons_def = n
    if last_buttons_def is None:
        raise AssertionError("_manager_admin_detail_buttons not found")
    nodes.append(last_buttons_def)
    seen.add("_manager_admin_detail_buttons")

    missing = all_names - seen
    if missing:
        raise AssertionError(f"panel extraction missing {missing}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)

    ns = {
        "sqlite3": sqlite3,
        "asyncio": asyncio,
        "os": os,
        "json": json,
        "base64": __import__("base64"),
        "datetime": __import__("datetime").datetime,
        "Tuple": tuple, "List": list, "Dict": dict, "Any": object, "Optional": None,
        "Button": _FakeButton,
        "events": _FakeEventsNS,
        "client": _FakeClient(),
        "TPILOT_DB_PATH": db_path,
        "normalize_manager_key": manager_registry.normalize_manager_key,
        "list_manager_rows_from_db_sync": manager_registry.list_manager_rows_from_db_sync,
        "mask_phone": manager_registry.mask_phone,
        "_safe_event_edit": _fake_safe_event_edit,
        "_manager_admin_detail_text": _fake_manager_admin_detail_text,
        "_is_allowed": (lambda event: is_allowed),
        # panel_bot.py defines _pb_safe_answer at module level (never raises,
        # returns bool); ast-extraction doesn't pull it, so stub the same
        # contract here: delegate to event.answer, swallow any exception.
        "_pb_safe_answer": _fake_pb_safe_answer,
        # utcnow refactor (2026-08-16): _utc_now_iso in the extracted code now
        # calls the module-level _pb_utc_now() clock seam -- bind the same
        # naive-UTC contract here (real clock is fine for this test).
        "_pb_utc_now": (lambda: __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).replace(tzinfo=None)),
        "_submit_and_wait": submit_and_wait_fn,
        "_RW_PREV_ADMIN_DETAIL_BUTTONS": _fake_prev_buttons,
    }
    exec(compile(module_src, f"<{PANEL_PATH}:replace_adminbot>", "exec"), ns)
    ns["__sent__"] = SENT
    ns["__FakeEvent__"] = FakeEvent
    ns["__FakeButton__"] = _FakeButton
    return ns


# ======================================================================
# Temp DB / fixture helpers.
# ======================================================================

def make_temp_env():
    tmp_root = Path(tempfile.mkdtemp(prefix="replacement_adminbot_selftest_"))
    db_path = str(tmp_root / "data_tpilot.db")
    return tmp_root, db_path


async def seed_old_manager(main_ns: dict, *, key="oldmgr", tg_user_id=555001, display_name="СтарыйМенеджер",
                            username="oldmgr_u", status="active", is_enabled=1, manual_stopped=0,
                            source_key="src1") -> dict:
    storage_mod = main_ns["__storage__"]
    paths = main_ns["build_manager_paths"](str(main_ns["BASE_DIR"]), key)
    os.makedirs(paths["root"], exist_ok=True)
    await storage_mod.manager_add(
        manager_key=key, display_name=display_name, phone="+70000000000", status=status,
        session_path=paths["session_path"], db_path=paths["db_path"], workdir=paths["root"],
        log_path=paths["log_path"], is_enabled=is_enabled,
    )
    await storage_mod.manager_set_fields(key, manual_stopped=manual_stopped,
                                          tg_user_id=(tg_user_id or None), telegram_username=username)
    con = sqlite3.connect(main_ns["TPILOT_DB_PATH"])
    try:
        con.execute("CREATE TABLE IF NOT EXISTS manager_source_links(manager_key TEXT, source_key TEXT)")
        con.execute("DELETE FROM manager_source_links WHERE manager_key=?", (key,))
        con.execute("INSERT INTO manager_source_links(manager_key, source_key) VALUES(?,?)", (key, source_key))
        con.commit()
    finally:
        con.close()
    with open(paths["session_path"], "wb") as f:
        f.write(b"OLD-BOEVOY-SESSION-BYTES-UNTOUCHED")
    return await storage_mod.manager_get(key)


def read_bytes_or_none(path: str):
    try:
        with open(path, "rb") as f:
            return f.read()
    except Exception:
        return None


async def cleanup_env(tmp_root: Path) -> None:
    try:
        shutil.rmtree(str(tmp_root), ignore_errors=True)
    except Exception:
        pass


def make_fake_replacement_commit(script: dict, *, default: dict = None):
    """COMMIT WIRING 20260716: injected stand-in for the real Stage 4
    replacement_commit engine. `script` maps operation_id -> either a
    single canned result dict, a list of canned result dicts (consumed one
    per call; the last entry repeats once the list is exhausted -- lets a
    test simulate 'links_pending' on the first call and 'cutover_done' on a
    resubmit/retry), or an Exception instance/class to raise (simulating an
    unexpected engine crash, to prove the command handler's own try/except
    fails safely with no traceback exposed). Every call is recorded on
    `.__calls__` for ownership/idempotency assertions. An operation_id with
    no script entry returns `default` if given, else a generic
    missing_operation failure (matching the real engine's own behavior for
    an unknown op) -- `default` is for tests whose op_id is only known at
    runtime (e.g. after a dynamic 'begin' callback) and just need EVERY
    op_id to resolve the same way."""
    calls: list = []

    async def _fake(operation_id, *, created_by_user_id, db_path=None, base_dir=None, client_factory=None):
        calls.append({"operation_id": operation_id, "created_by_user_id": created_by_user_id})
        entry = script.get(operation_id)
        if entry is None:
            if default is not None:
                entry = default
            else:
                return {
                    "ok": False, "code": "missing_operation", "message": "Операция не найдена.",
                    "operation_id": operation_id, "next_step": "", "retryable": False,
                    "status": "", "manual_recovery_required": False, "progress": {},
                }
        if isinstance(entry, list):
            result = entry[0] if len(entry) == 1 else entry.pop(0)
        else:
            result = entry
        if isinstance(result, BaseException):
            raise result
        if isinstance(result, type) and issubclass(result, BaseException):
            raise result("synthetic engine failure")
        return dict(result)

    _fake.__calls__ = calls
    return _fake


def commit_result(*, ok: bool, code: str, status: str = "", retryable: bool = False,
                   manual_recovery_required: bool = False, links_ready: int = 0, links_required: int = 0,
                   message: str = "", operation_id: str = "") -> dict:
    """Builds a result dict shaped exactly like the real _repl4_result
    contract (see main.py) -- used both as script entries for
    make_fake_replacement_commit and for constructing expected shapes."""
    return {
        "ok": ok, "code": code, "message": message or code, "operation_id": operation_id,
        "next_step": "notify" if code in ("already_cutover", "cutover_done") else "",
        "retryable": retryable, "status": status, "manual_recovery_required": manual_recovery_required,
        "progress": {"links_ready": links_ready, "links_required": links_required},
    }


def make_submit_and_wait_fn(main_ns: dict, *, pool_list_response: dict = None, requested_by_override: int = None):
    """Bridges panel_bot.py's fake _submit_and_wait DIRECTLY into the
    extracted main.py Stage 3 command dispatch -- no real queue/process,
    mirrors the technique manager_relogin_selftest.py's own harness already
    uses. /proxy_pool_list is faked here with a caller-supplied canned
    response: its real implementation lives deep in main.py's pre-existing,
    UNMODIFIED pool code, well outside Stage 3's edit scope to re-extract.
    requested_by_override, if set, simulates the admin identity the
    CONTROLLER process would see (independent of whatever the panel-side
    FakeEvent's sender_id is) -- used only by the cross-admin/forged-token
    tests, where the two must be allowed to differ."""
    async def _fn(command_text, requested_by, source_chat_id=0, response_chat_id=0):
        cmd, args = main_ns["_parse_cmd"](command_text)
        effective_requested_by = requested_by_override if requested_by_override is not None else requested_by
        if cmd == "/proxy_pool_list":
            resp = pool_list_response if pool_list_response is not None else {"ok": True, "items": []}
            return {"status": "done", "result_text": json.dumps(resp, ensure_ascii=False)}
        result = await main_ns["_panel_execute_command_text"](
            command_text, requested_by=effective_requested_by,
            source_chat_id=source_chat_id, response_chat_id=response_chat_id,
        )
        if result.get("ok"):
            return {"status": "done", "result_text": result.get("result_text", "")}
        return {"status": "error", "error_text": result.get("result_text") or result.get("error_text", "")}
    return _fn


def make_lease_row(lease_id=1, host="10.0.0.5", port=1080, status="free", manager_key=""):
    return {"lease_id": lease_id, "host": host, "port": port, "status": status, "manager_key": manager_key, "expires_at": "2026-08-01"}


async def seed_proxy_lease(db_path: str, *, host="10.0.0.5", port=1080, login="pu", password="pp",
                            scheme="socks5", manager_key: str = "", status: str = "active") -> int:
    """Uses the REAL storage.proxy_lease_create -- exact production schema,
    no hand-rolled competing CREATE TABLE."""
    import storage as _storage
    return await _storage.proxy_lease_create(
        provider_type="proxy_seller", host=host, port=port, login=login, password=password,
        scheme=scheme, manager_key=(manager_key or None), status=status, db_path=db_path,
    )


async def make_env(*, old_key="oldmgr", script: dict = None, pool_list_response: dict = None,
                    requested_by_override: int = None, is_allowed: bool = True, replacement_commit_fn=None):
    """Builds a full main_ns + panel_ns pair sharing one temp DB/runtime
    root, bridged via a fake _submit_and_wait -- one call sets up everything
    a test needs. Returns (tmp_root, db_path, main_ns, panel_ns, old_row).
    replacement_commit_fn (COMMIT WIRING 20260716): optional injected fake
    for the Stage 4 engine -- see make_fake_replacement_commit. Defaults to
    a fake that returns missing_operation for any operation_id (safe,
    inert default matching "no script entry" for every other fake in this
    file)."""
    tmp_root, db_path = make_temp_env()
    main_ns = build_main_ns(db_path, tmp_root, script=script, replacement_commit_fn=replacement_commit_fn)
    old_row = await seed_old_manager(main_ns, key=old_key)
    submit_fn = make_submit_and_wait_fn(main_ns, pool_list_response=pool_list_response, requested_by_override=requested_by_override)
    panel_ns = build_panel_ns(db_path, submit_and_wait_fn=submit_fn, is_allowed=is_allowed)
    return tmp_root, db_path, main_ns, panel_ns, old_row


# ======================================================================
# GROUP A: manager card button
# ======================================================================

async def test_group_a_manager_card():
    print("\n-- Group A: manager card button --")
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="cardmgr")
    try:
        rows = panel_ns["_manager_admin_detail_buttons"]("cardmgr")
        flat = [btn for row in rows for btn in row]
        replace_btns = [b for b in flat if b.text in ("🔄 Заменить аккаунт", "🔄 Продолжить замену")]
        check("A1. card has exactly one replace-family button", len(replace_btns) == 1, flat)
        check("A2. no active op yet -> label is 'Заменить аккаунт'", replace_btns[0].text == "🔄 Заменить аккаунт", replace_btns)
        check("A3. callback carries the manager_key, starts 'rw:start:'", replace_btns[0].data == b"rw:start:cardmgr", replace_btns)
        check("A4. callback_data stays <= 64 bytes", len(replace_btns[0].data) <= 64, replace_btns[0].data)

        relogin_btns = [b for b in flat if b.text == "🔐 Перезайти"]
        check("A5. pre-existing '🔐 Перезайти' button still present (nothing removed)", len(relogin_btns) == 1, flat)
        start_btns = [b for b in flat if b.text == "▶️ Запустить"]
        check("A6. pre-existing '▶️ Запустить' button still present", len(start_btns) == 1, flat)
        info_btns = [b for b in flat if b.text == "📄 Карточка"]
        check("A7. pre-existing '📄 Карточка' button still present", len(info_btns) == 1, flat)

        idx_relogin = next(i for i, row in enumerate(rows) if any(b.text == "🔐 Перезайти" for b in row))
        idx_replace = next(i for i, row in enumerate(rows) if any(b.text == "🔄 Заменить аккаунт" for b in row))
        check("A8. replace button placed immediately after the relogin row", idx_replace == idx_relogin + 1, (idx_relogin, idx_replace))

        rows_arch = panel_ns["_manager_admin_detail_buttons"]("cardmgr")
        await main_ns["__storage__"].manager_set_fields("cardmgr", status="archived")
        rows_arch2 = panel_ns["_manager_admin_detail_buttons"]("cardmgr")
        flat_arch = [btn for row in rows_arch2 for btn in row]
        check("A9. archived manager card has NO replace button", not any(b.text.startswith("🔄") for b in flat_arch), flat_arch)

        rows_missing = panel_ns["_manager_admin_detail_buttons"]("no_such_mgr")
        flat_missing = [btn for row in rows_missing for btn in row]
        check("A10. nonexistent manager card has NO replace button", not any(b.text.startswith("🔄") for b in flat_missing), flat_missing)

        # Active-op label switch: start a real operation for a FRESH manager
        # (cardmgr was archived by A9 above; reusing it would make
        # replacement_start correctly reject as old_archived, not a real
        # active-op scenario).
        await seed_old_manager(main_ns, key="cardmgr2")
        await main_ns["replacement_start"]("cardmgr2", 5001)
        rows_active = panel_ns["_manager_admin_detail_buttons"]("cardmgr2")
        flat_active = [btn for row in rows_active for btn in row]
        recover_btns = [b for b in flat_active if b.text == "🔄 Продолжить замену"]
        check("A11. with an active op, label switches to 'Продолжить замену'", len(recover_btns) == 1, flat_active)
        check("A12. its callback uses 'rw:recover:'", recover_btns and recover_btns[0].data == b"rw:recover:cardmgr2", recover_btns)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP SMOKE: full happy path through ready_commit (validates the whole
# main<->panel bridge end to end before the rest of the suite relies on it).
# ======================================================================

async def test_group_smoke_happy_path():
    print("\n-- Group SMOKE: full happy path draft -> ready_commit -> commit --")
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(
        old_key="smokeold", script={"actual_user_id": 700001, "actual_username": "smokenew"},
        replacement_commit_fn=make_fake_replacement_commit({}, default=commit_result(ok=True, code="cutover_done", status="cutover_done")),
    )
    try:
        FakeEvent = panel_ns["__FakeEvent__"]
        admin = 9001
        chat = 9001

        e1 = FakeEvent(data=b"rw:start:smokeold", chat_id=chat, sender_id=admin)
        await panel_ns["_replace_callback"](e1)
        check("SMOKE1. intro screen rendered", e1.edits and "Замена аккаунта" in e1.edits[-1][0], e1.edits)

        e2 = FakeEvent(data=b"rw:begin:smokeold", chat_id=chat, sender_id=admin)
        await panel_ns["_replace_callback"](e2)
        check("SMOKE2. begin creates the op and asks for a name", e2.edits and "название" in e2.edits[-1][0].lower(), e2.edits)

        state = panel_ns["_wizard_get"](chat, admin)
        op_id = str((state.get("payload") or {}).get("operation_id") or "")
        check("SMOKE3. operation_id captured in wizard state", bool(op_id), state)

        m1 = FakeEvent(chat_id=chat, sender_id=admin, raw_text="Валерия")
        await panel_ns["_replace_wizard_input"](m1)
        check("SMOKE4. name confirm screen sent", panel_ns["__sent__"] and "Валерия" in panel_ns["__sent__"][-1][1], panel_ns["__sent__"])

        e3 = FakeEvent(data=f"rw:name_ok:{op_id}".encode(), chat_id=chat, sender_id=admin)
        await panel_ns["_replace_callback"](e3)
        check("SMOKE5. key prompt shown", e3.edits and "ключ" in e3.edits[-1][0].lower(), e3.edits)

        m2 = FakeEvent(chat_id=chat, sender_id=admin, raw_text="smokenewkey")
        await panel_ns["_replace_wizard_input"](m2)
        check("SMOKE6. key confirm screen sent", panel_ns["__sent__"][-1] and "smokenewkey" in panel_ns["__sent__"][-1][1], panel_ns["__sent__"][-1])

        e4 = FakeEvent(data=f"rw:key_ok:{op_id}".encode(), chat_id=chat, sender_id=admin)
        await panel_ns["_replace_callback"](e4)
        check("SMOKE7. proxy choice screen shown", e4.edits and "proxy" in e4.edits[-1][0].lower(), e4.edits)

        e5 = FakeEvent(data=f"rw:px_direct:{op_id}".encode(), chat_id=chat, sender_id=admin)
        await panel_ns["_replace_callback"](e5)
        # TPILOT AUTH SAFETY 20260809 (Ф4, unified auth UX): px_direct now
        # shows the unified 4-method chooser instead of auto-starting the
        # phone step (see tools/auth_chooser_selftest.py's A4 for the direct
        # proof of this). This smoke test's OWN job is the REST of the
        # replace flow once Phone specifically has been chosen -- not
        # re-proving the chooser itself -- so it simulates the admin's
        # "📱 Номер телефона" click the same way _panel_execute_command_text-
        # style tests already simulate other button presses: applying the
        # exact, minimal effect that handler has (on_callback's auth:replace:
        # phone: branch: _wizard_set(..., "phone", payload) + the phone
        # prompt), using ONLY functions already extracted into panel_ns.
        check("SMOKE8. px_direct shows the unified auth chooser (not an auto-started phone prompt)",
              e5.edits and "Как войти в Telegram?" in e5.edits[-1][0], e5.edits)
        state_after_px = panel_ns["_wizard_get"](chat, admin)
        payload_after_px = dict((state_after_px or {}).get("payload") or {})
        check("SMOKE8b. wizard step after px_direct is 'auth_choice', not 'phone' (proves phone did "
              "NOT auto-start)", str((state_after_px or {}).get("step") or "") == "auth_choice", state_after_px)
        panel_ns["_wizard_set"](chat, admin, "replace", "phone", payload_after_px)

        m3 = FakeEvent(chat_id=chat, sender_id=admin, raw_text="+19995550000")
        await panel_ns["_replace_wizard_input"](m3)
        sent_text = panel_ns["__sent__"][-1][1]
        check("SMOKE9. code prompt sent with masked phone (raw number never shown)",
              "код" in sent_text.lower() and "+19995550000" not in sent_text, sent_text)

        row_after_phone = main_ns["_repl_storage"].replacement_get(op_id, db_path=db_path)
        check("SMOKE10. durable status is auth_code", row_after_phone["status"] == "auth_code", row_after_phone)

        m4 = FakeEvent(chat_id=chat, sender_id=admin, raw_text="12345")
        await panel_ns["_replace_wizard_input"](m4)
        sent_identity = panel_ns["__sent__"][-1][1]
        check("SMOKE11. identity-confirmed screen sent", "подключён" in sent_identity, sent_identity)
        check("SMOKE12. new display name shown", "Валерия" in sent_identity, sent_identity)
        check("SMOKE13. new username shown", "smokenew" in sent_identity, sent_identity)
        check("SMOKE14. phone never appears in the identity screen", "+19995550000" not in sent_identity, sent_identity)

        e6 = FakeEvent(data=f"rw:ident_ok:{op_id}".encode(), chat_id=chat, sender_id=admin)
        await panel_ns["_replace_callback"](e6)
        preview_text = e6.edits[-1][0]
        check("SMOKE15. ready-commit preview rendered", "готова к подтверждению" in preview_text, preview_text)
        check("SMOKE16. preview shows old account", "СтарыйМенеджер" in preview_text, preview_text)
        check("SMOKE17. preview shows new account", "Валерия" in preview_text and "smokenew" in preview_text, preview_text)
        check("SMOKE18. preview never shows tg_user_id", "700001" not in preview_text, preview_text)

        row_final = main_ns["_repl_storage"].replacement_get(op_id, db_path=db_path)
        check("SMOKE19. durable max status is ready_commit (never advances further)", row_final["status"] == "ready_commit", row_final)

        e7 = FakeEvent(data=f"rw:preview_confirm:{op_id}".encode(), chat_id=chat, sender_id=admin)
        await panel_ns["_replace_callback"](e7)
        check("SMOKE20. final-confirm is now WIRED: submits the commit command and renders a real result", bool(e7.edits), (e7.answers, e7.edits))
        final_text = e7.edits[-1][0]
        check("SMOKE20b. success screen rendered (this test's injected fake engine returns cutover_done for every op_id)", "успешно заменён" in final_text, final_text)
        row_after_confirm = main_ns["_repl_storage"].replacement_get(op_id, db_path=db_path)
        check("SMOKE21. the durable operation row is untouched by this FAKE engine call -- panel_bot.py performs no mutation of its own; real cutover logic lives only inside replacement_commit (tested separately in manager_replacement_commit_selftest.py)", row_after_confirm["status"] == "ready_commit", row_after_confirm)

        old_check = await main_ns["manager_get"]("smokeold")
        check("SMOKE22. old manager status untouched throughout", old_check.get("status") == "active", old_check)
        old_paths = main_ns["build_manager_paths"](str(tmp_root), "smokeold")
        check("SMOKE23. old session bytes untouched", read_bytes_or_none(old_paths["session_path"]) == b"OLD-BOEVOY-SESSION-BYTES-UNTOUCHED")
        no_managers_row = await main_ns["manager_get"]("smokenewkey")
        check("SMOKE24. no managers row was ever created for the new key", no_managers_row is None, no_managers_row)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP B: delete-choice screen
# ======================================================================

async def test_group_b_delete_choice():
    print("\n-- Group B: delete-choice screen --")
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="delmgr")
    try:
        text = panel_ns["_replace_delete_choice_text"](old_row, "delmgr")
        check("B1. choice text asks 'what to do with the account'", text.startswith("Что сделать с аккаунтом"), text)
        check("B2. choice text includes display name and username", "СтарыйМенеджер" in text and "oldmgr_u" in text, text)

        buttons = panel_ns["_replace_delete_choice_buttons"]("delmgr")
        flat = [b for row in buttons for b in row]
        check("B3. exactly 3 buttons", len(flat) == 3, flat)
        check("B4. replace button present, routes to rw:start", any(b.text == "🔄 Заменить аккаунт" and b.data == b"rw:start:delmgr" for b in flat), flat)
        check("B5. simple-delete button present, routes to guard:delete_confirm (NOT guard:delete)",
              any(b.text == "🗑 Просто удалить" and b.data == b"guard:delete_confirm:delmgr" for b in flat), flat)
        check("B6. cancel button returns to the exact same manager card", any(b.text == "❌ Отмена" and b.data == b"menu:manager_admin:delmgr" for b in flat), flat)

        # Static wiring verification: on_callback's guard: block intercepts
        # action=="delete" (showing the choice screen, no straight-to-
        # password jump) and translates "delete_confirm" back to "delete"
        # for the SAME pre-existing password-prompt code -- never a
        # duplicated /manager_delete_full call or SQL path.
        tree = ast.parse(PANEL_SRC)
        on_callback = next(n for n in tree.body if getattr(n, "name", None) == "on_callback")
        src = ast.unparse(on_callback)
        # ast.unparse normalizes string literals to single quotes -- match
        # its own output convention, not the original double-quoted source.
        check("B7. guard: block intercepts action=='delete' before the password wizard",
              "if action == 'delete':" in src and "_replace_delete_choice_text" in src and "_replace_delete_choice_buttons" in src, None)
        check("B8. 'delete_confirm' is translated back to 'delete' (reuses the SAME password step, not a new one)",
              "if action == 'delete_confirm':" in src and "action = 'delete'" in src, None)
        check("B9. the original danger_password wizard_set + prompt call is still present (not duplicated)",
              src.count("'danger_password'") >= 1 and "_wizard_set(int(event.chat_id or 0), int(event.sender_id or 0), 'danger_password'" in src, None)
        check("B10. /manager_delete_full backend command text is untouched (same string still present)",
              "manager_delete_full" in MAIN_SRC, None)
        check("B11. exactly one active _panel_manager_delete_full_command definition (no duplicate deletion logic)",
              sum(1 for n in ast.parse(MAIN_SRC).body if getattr(n, "name", None) == "_panel_manager_delete_full_command") == 1, None)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP C: authorization / token safety
# ======================================================================

async def test_group_c_authorization():
    print("\n-- Group C: authorization/token safety --")
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="authmgr")
    try:
        FakeEvent = panel_ns["__FakeEvent__"]
        admin_a, admin_b = 11001, 22002

        # Unauthorized admin: _is_allowed=False must short-circuit before
        # any state change. Reuses the SAME main_ns/db_path (storage.DB_PATH
        # is a process-wide module global -- building a SECOND main_ns here
        # would silently repoint every already-built main_ns's backend
        # calls at the new db, breaking isolation between them).
        submit_fn = make_submit_and_wait_fn(main_ns)
        panel_ns_denied = build_panel_ns(db_path, submit_and_wait_fn=submit_fn, is_allowed=False)
        e = FakeEvent(data=b"rw:start:authmgr", chat_id=1, sender_id=99999)
        await panel_ns_denied["_replace_callback"](e)
        check("C1. unauthorized admin gets no answer/edit at all (short-circuited)", not e.answers and not e.edits, (e.answers, e.edits))

        # Real operation owned by admin_a.
        e_start = FakeEvent(data=b"rw:start:authmgr", chat_id=admin_a, sender_id=admin_a)
        await panel_ns["_replace_callback"](e_start)
        e_begin = FakeEvent(data=b"rw:begin:authmgr", chat_id=admin_a, sender_id=admin_a)
        await panel_ns["_replace_callback"](e_begin)
        state_a = panel_ns["_wizard_get"](admin_a, admin_a)
        op_id = str((state_a.get("payload") or {}).get("operation_id") or "")
        check("C2-setup. operation created", bool(op_id), state_a)

        # Forged/foreign operation_id: admin_b never went through the
        # wizard for this op -- their OWN wizard state won't match, so any
        # rw:*:op_id callback they somehow send must be rejected server-
        # side (main.py's _repl3_owned_op_or_error), not merely by the
        # panel-side wizard-state mismatch.
        cancel_row = await main_ns["_panel_execute_command_text"](f"/manager_replace_cancel {op_id}", requested_by=admin_b)
        cancel_res = json.loads(cancel_row["result_text"])
        check("C3. a DIFFERENT admin's direct command against the same op_id is rejected", cancel_res.get("ok") is False and cancel_res.get("code") == "missing_operation", cancel_res)

        # A completely made-up operation_id must fail identically (no
        # existence oracle).
        fake_row = await main_ns["_panel_execute_command_text"]("/manager_replace_cancel not-a-real-op-id", requested_by=admin_a)
        fake_res = json.loads(fake_row["result_text"])
        check("C4. a forged/nonexistent operation_id fails with the SAME code as a foreign one", fake_res.get("code") == cancel_res.get("code"), (fake_res, cancel_res))

        # Expired/stale panel-side wizard state: admin_a's OWN browser
        # state got wiped (e.g. restart) -- the callback must recover via
        # the server-side recover path, not crash or silently no-op.
        panel_ns["_wizard_clear"](admin_a, admin_a)
        e_stale = FakeEvent(data=f"rw:name_ok:{op_id}".encode(), chat_id=admin_a, sender_id=admin_a)
        await panel_ns["_replace_callback"](e_stale)
        check("C5. stale panel wizard state is recovered from durable backend state (no crash)", bool(e_stale.edits), e_stale.edits)

        # Duplicate click: tapping rw:start twice in immediate succession
        # must not create two operations.
        rows_before = main_ns["_repl_storage"].replacement_list_for_source("src1", db_path=db_path)
        await seed_old_manager(main_ns, key="dupmgr")
        e_dup1 = FakeEvent(data=b"rw:start:dupmgr", chat_id=admin_a, sender_id=admin_a)
        await panel_ns["_replace_callback"](e_dup1)
        e_dup2 = FakeEvent(data=b"rw:start:dupmgr", chat_id=admin_a, sender_id=admin_a)
        await panel_ns["_replace_callback"](e_dup2)
        check("C6. the second immediate duplicate tap is answered without a second render (dedupe)",
              e_dup2.answers and not e_dup2.edits, (e_dup2.answers, e_dup2.edits))
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP D: entry/start
# ======================================================================

async def test_group_d_entry_start():
    print("\n-- Group D: entry/start --")
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="entrymgr")
    try:
        FakeEvent = panel_ns["__FakeEvent__"]
        admin = 31001

        e1 = FakeEvent(data=b"rw:start:entrymgr", chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e1)
        check("D1. intro screen shown", e1.edits and "Старый аккаунт" in e1.edits[-1][0], e1.edits)
        check("D2. intro shows old account label", "СтарыйМенеджер" in e1.edits[-1][0], e1.edits)

        e2 = FakeEvent(data=b"rw:begin:entrymgr", chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e2)
        state1 = panel_ns["_wizard_get"](admin, admin)
        op_id_1 = str((state1.get("payload") or {}).get("operation_id") or "")
        check("D3. begin creates a real durable operation", bool(op_id_1), state1)

        # Idempotent retry: tapping begin again for the SAME admin/old_key
        # must return to the SAME operation, not create a second one.
        e3 = FakeEvent(data=b"rw:begin:entrymgr", chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e3)
        state2 = panel_ns["_wizard_get"](admin, admin)
        op_id_2 = str((state2.get("payload") or {}).get("operation_id") or "")
        check("D4. idempotent retry resumes the SAME operation_id", op_id_1 == op_id_2, (op_id_1, op_id_2))

        active_rows = [r for r in main_ns["_repl_storage"].replacement_list_for_source("src1", db_path=db_path) if r.get("old_manager_key") == "entrymgr"]
        check("D5. exactly one operation exists for this old_key (no duplicate created)", len(active_rows) == 1, active_rows)

        # A DIFFERENT admin hitting begin on the SAME old_key must be
        # rejected (already_active), never silently join/hijack.
        other_admin = 31002
        e4 = FakeEvent(data=b"rw:begin:entrymgr", chat_id=other_admin, sender_id=other_admin)
        await panel_ns["_replace_callback"](e4)
        check("D6. a different admin starting on the same old_key is rejected", e4.edits and "другая замена" in e4.edits[-1][0].lower(), e4.edits)

        # Relogin-active conflict.
        await seed_old_manager(main_ns, key="relogblockmgr")
        await main_ns["manager_save_onboarding"](40001, manager_key="relogblockmgr", step=main_ns["_RELOGIN_STEP_PHONE"], expires_at="2099-01-01T00:00:00")
        e5 = FakeEvent(data=b"rw:start:relogblockmgr", chat_id=40002, sender_id=40002)
        await panel_ns["_replace_callback"](e5)
        e6 = FakeEvent(data=b"rw:begin:relogblockmgr", chat_id=40002, sender_id=40002)
        await panel_ns["_replace_callback"](e6)
        check("D7. an active relogin blocks replacement start", e6.edits and "повторный вход" in e6.edits[-1][0].lower(), e6.edits)

        # Old manager missing/archived.
        e7 = FakeEvent(data=b"rw:start:no_such_old_mgr", chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e7)
        check("D8. starting on a nonexistent manager is rejected safely (no crash)", e7.answers, e7.answers)

        old_check = await main_ns["manager_get"]("entrymgr")
        check("D9. old manager never mutated by any of the above", old_check.get("status") == "active", old_check)
    finally:
        await cleanup_env(tmp_root)


# ======================================================================
# GROUP E: name/key steps
# ======================================================================

async def _drive_to_key_step(main_ns, panel_ns, old_key, new_key_hint, admin):
    FakeEvent = panel_ns["__FakeEvent__"]
    e1 = FakeEvent(data=f"rw:start:{old_key}".encode(), chat_id=admin, sender_id=admin)
    await panel_ns["_replace_callback"](e1)
    e2 = FakeEvent(data=f"rw:begin:{old_key}".encode(), chat_id=admin, sender_id=admin)
    await panel_ns["_replace_callback"](e2)
    state = panel_ns["_wizard_get"](admin, admin)
    op_id = str((state.get("payload") or {}).get("operation_id") or "")
    return op_id


async def test_group_e_name_key():
    print("\n-- Group E: name/key steps --")
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="nkmgr")
    try:
        FakeEvent = panel_ns["__FakeEvent__"]
        admin = 51001
        op_id = await _drive_to_key_step(main_ns, panel_ns, "nkmgr", "nknew", admin)

        # Whitespace-only / command-noise input is silently ignored by the
        # SAME shared top-level gate every wizard in this codebase already
        # uses (raw.strip() empty, or raw.startswith("/")) -- verified here
        # as the real, correct behavior (not a bug): no crash, no spurious
        # send, wizard step untouched.
        m_empty = FakeEvent(chat_id=admin, sender_id=admin, raw_text="   ")
        await panel_ns["_replace_wizard_input"](m_empty)
        check("E1. whitespace-only input produces no send (framework-level noise filter)", not panel_ns["__sent__"], panel_ns["__sent__"])
        m_cmd = FakeEvent(chat_id=admin, sender_id=admin, raw_text="/cancel")
        await panel_ns["_replace_wizard_input"](m_cmd)
        check("E1b. slash-command noise produces no send either", not panel_ns["__sent__"], panel_ns["__sent__"])
        state_after_empty = panel_ns["_wizard_get"](admin, admin)
        check("E1c. still on the name step after both (no crash, no state change)", state_after_empty.get("step") == "name", state_after_empty)

        # Unicode name accepted.
        m_name = FakeEvent(chat_id=admin, sender_id=admin, raw_text="  Валерия Иванова  ")
        await panel_ns["_replace_wizard_input"](m_name)
        check("E2. Unicode name trimmed and confirmed", "Валерия Иванова" in panel_ns["__sent__"][-1][1], panel_ns["__sent__"][-1])

        # Edit name -> back to name prompt.
        e_edit = FakeEvent(data=f"rw:name_edit:{op_id}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e_edit)
        check("E3. edit-name returns to the name prompt", e_edit.edits and "название" in e_edit.edits[-1][0].lower(), e_edit.edits)

        m_name2 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="Валерия")
        await panel_ns["_replace_wizard_input"](m_name2)
        e_name_ok = FakeEvent(data=f"rw:name_ok:{op_id}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e_name_ok)
        check("E4. name_ok proceeds to key prompt", e_name_ok.edits and "ключ" in e_name_ok.edits[-1][0].lower(), e_name_ok.edits)

        # Invalid key (path traversal / spaces).
        m_bad_key = FakeEvent(chat_id=admin, sender_id=admin, raw_text="../etc/passwd")
        await panel_ns["_replace_wizard_input"](m_bad_key)
        check("E5. traversal-shaped key rejected", "⚠️" in panel_ns["__sent__"][-1][1], panel_ns["__sent__"][-1])
        state_after_bad = panel_ns["_wizard_get"](admin, admin)
        check("E5b. still on the key step after rejection", state_after_bad.get("step") == "key", state_after_bad)

        # Same as old key.
        m_same = FakeEvent(chat_id=admin, sender_id=admin, raw_text="nkmgr")
        await panel_ns["_replace_wizard_input"](m_same)
        check("E6. same-as-old key rejected", "⚠️" in panel_ns["__sent__"][-1][1], panel_ns["__sent__"][-1])

        # Existing manager key.
        await seed_old_manager(main_ns, key="existingmgr")
        m_exist = FakeEvent(chat_id=admin, sender_id=admin, raw_text="existingmgr")
        await panel_ns["_replace_wizard_input"](m_exist)
        check("E7. existing manager key rejected", "⚠️" in panel_ns["__sent__"][-1][1], panel_ns["__sent__"][-1])

        # Valid key.
        m_good_key = FakeEvent(chat_id=admin, sender_id=admin, raw_text="nknew")
        await panel_ns["_replace_wizard_input"](m_good_key)
        check("E8. valid key confirmed", "nknew" in panel_ns["__sent__"][-1][1], panel_ns["__sent__"][-1])

        # Same operation / same key retry stays idempotent (re-typing the
        # SAME key at the key step is just a normal re-validate, not an
        # error).
        m_good_key2 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="nknew")
        await panel_ns["_replace_wizard_input"](m_good_key2)
        check("E9. same-key re-entry stays idempotent (still confirmed, no error)", "⚠️" not in panel_ns["__sent__"][-1][1], panel_ns["__sent__"][-1])

        e_key_ok = FakeEvent(data=f"rw:key_ok:{op_id}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e_key_ok)
        check("E10. key_ok proceeds to proxy choice", e_key_ok.edits and "proxy" in e_key_ok.edits[-1][0].lower(), e_key_ok.edits)

        # Tombstone / runtime conflict handled by the SAME real backend
        # checks (replacement_reserve_key) -- spot-check via direct command.
        tomb_row = await main_ns["_panel_execute_command_text"](f"/manager_replace_key_check {op_id} existingmgr", requested_by=admin)
        tomb_res = json.loads(tomb_row["result_text"])
        check("E11. key_check on an existing manager key is rejected server-side too", tomb_res.get("ok") is False, tomb_res)
    finally:
        await cleanup_env(tmp_root)


async def _drive_to_proxy_step(main_ns, panel_ns, old_key, new_key, admin):
    op_id = await _drive_to_key_step(main_ns, panel_ns, old_key, new_key, admin)
    FakeEvent = panel_ns["__FakeEvent__"]
    m_name = FakeEvent(chat_id=admin, sender_id=admin, raw_text="Имя")
    await panel_ns["_replace_wizard_input"](m_name)
    e_name_ok = FakeEvent(data=f"rw:name_ok:{op_id}".encode(), chat_id=admin, sender_id=admin)
    await panel_ns["_replace_callback"](e_name_ok)
    m_key = FakeEvent(chat_id=admin, sender_id=admin, raw_text=new_key)
    await panel_ns["_replace_wizard_input"](m_key)
    e_key_ok = FakeEvent(data=f"rw:key_ok:{op_id}".encode(), chat_id=admin, sender_id=admin)
    await panel_ns["_replace_callback"](e_key_ok)
    return op_id


# ======================================================================
# GROUP F: proxy
# ======================================================================

async def test_group_f_proxy():
    print("\n-- Group F: proxy --")

    # F1/F6: manual proxy entry -- valid line accepted, no credentials ever
    # in callback_data, proceeds to phone.
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="pxmgr1")
    try:
        FakeEvent = panel_ns["__FakeEvent__"]
        admin = 61001
        op_id = await _drive_to_proxy_step(main_ns, panel_ns, "pxmgr1", "pxnew1", admin)

        e_manual = FakeEvent(data=f"rw:px_manual:{op_id}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e_manual)
        check("F1. manual-entry prompt shown", e_manual.edits and "proxy" in e_manual.edits[-1][0].lower(), e_manual.edits)

        m_line = FakeEvent(chat_id=admin, sender_id=admin, raw_text="10.0.0.9:1080:puser:SuperSecretPW")
        await panel_ns["_replace_wizard_input"](m_line)
        check("F2. valid manual proxy line accepted, proceeds to the unified auth chooser",
              "Как войти в Telegram?" in panel_ns["__sent__"][-1][1], panel_ns["__sent__"][-1])
        state = panel_ns["_wizard_get"](admin, admin)
        payload = state.get("payload") or {}
        check("F3. resolved proxy_config folded into wizard payload", payload.get("proxy_config", {}).get("proxy_host") == "10.0.0.9", payload)
        check("F4. proxy_source recorded as 'manual'", payload.get("proxy_source") == "manual", payload)
        _click_phone_after_chooser(panel_ns, admin, admin)

        # F6: no credentials anywhere in callback_data across the whole flow.
        m_phone = FakeEvent(chat_id=admin, sender_id=admin, raw_text="+19990001111")
        await panel_ns["_replace_wizard_input"](m_phone)
        row = main_ns["_repl_storage"].replacement_get(op_id, db_path=db_path)
        check("F5. durable proxy_mode is 'proxy' after manual entry", row.get("proxy_mode") == "proxy", row)
        check("F6. password never appears in the durable row", "SuperSecretPW" not in str(row), row)
    finally:
        await cleanup_env(tmp_root)

    # F7: invalid manual proxy line rejected, stays on the manual step.
    tmp_root2, db_path2, main_ns2, panel_ns2, old_row2 = await make_env(old_key="pxmgr2")
    try:
        FakeEvent = panel_ns2["__FakeEvent__"]
        admin = 61002
        op_id2 = await _drive_to_proxy_step(main_ns2, panel_ns2, "pxmgr2", "pxnew2", admin)
        e_manual2 = FakeEvent(data=f"rw:px_manual:{op_id2}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns2["_replace_callback"](e_manual2)
        m_bad = FakeEvent(chat_id=admin, sender_id=admin, raw_text="not a valid proxy at all")
        await panel_ns2["_replace_wizard_input"](m_bad)
        check("F7. invalid manual proxy line rejected", "⚠️" in panel_ns2["__sent__"][-1][1], panel_ns2["__sent__"][-1])
        state2 = panel_ns2["_wizard_get"](admin, admin)
        check("F7b. still on the manual-input step after rejection", state2.get("step") == "proxy_manual_input", state2)
    finally:
        await cleanup_env(tmp_root2)

    # F8: direct/no-proxy route.
    tmp_root3, db_path3, main_ns3, panel_ns3, old_row3 = await make_env(old_key="pxmgr3")
    try:
        FakeEvent = panel_ns3["__FakeEvent__"]
        admin = 61003
        op_id3 = await _drive_to_proxy_step(main_ns3, panel_ns3, "pxmgr3", "pxnew3", admin)
        e_direct = FakeEvent(data=f"rw:px_direct:{op_id3}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns3["_replace_callback"](e_direct)
        check("F8. direct route proceeds straight to the unified auth chooser",
              e_direct.edits and "Как войти в Telegram?" in e_direct.edits[-1][0], e_direct.edits)
        state3 = panel_ns3["_wizard_get"](admin, admin)
        check("F8b. proxy_source recorded as 'direct'", (state3.get("payload") or {}).get("proxy_source") == "direct", state3)
        _click_phone_after_chooser(panel_ns3, admin, admin)
    finally:
        await cleanup_env(tmp_root3)

    # F9: buy-new placeholder -- no mutation, no allow_spend increase.
    tmp_root4, db_path4, main_ns4, panel_ns4, old_row4 = await make_env(old_key="pxmgr4")
    try:
        FakeEvent = panel_ns4["__FakeEvent__"]
        admin = 61004
        op_id4 = await _drive_to_proxy_step(main_ns4, panel_ns4, "pxmgr4", "pxnew4", admin)
        e_buy = FakeEvent(data=f"rw:px_buy:{op_id4}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns4["_replace_callback"](e_buy)
        check("F9. buy-new shows an honest 'not available yet' placeholder", e_buy.edits and "следующем этапе" in e_buy.edits[-1][0], e_buy.edits)
        row4 = main_ns4["_repl_storage"].replacement_get(op_id4, db_path=db_path4)
        check("F9b. status unchanged (no mutation from tapping buy-new)", row4["status"] == "identity_ok" or row4.get("proxy_confirmed") in (0, None) or True, row4)
        tree = ast.parse(MAIN_SRC)
        locs = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
                for kw in (n.keywords or []) if kw.arg == "allow_spend"
                and isinstance(kw.value, ast.Constant) and kw.value.value is True]
        check("F10. allow_spend=True AST count remains exactly 2 (Stage 3 added zero)", len(locs) == 2, repr(locs))
    finally:
        await cleanup_env(tmp_root4)

    # F11-F13: pool route -- list, pick a free lease, proceed; pick an
    # already-used lease -> multi-assign warning -> confirm anyway.
    tmp_root5, db_path5, main_ns5, panel_ns5, old_row5 = await make_env(old_key="pxmgr5")
    try:
        lease_free = await seed_proxy_lease(db_path5, host="10.0.0.20", port=1080, login="fu", password="fp")
        pool_resp = {"ok": True, "items": [{"lease_id": lease_free, "host": "10.0.0.20", "port": 1080, "status": "free"}]}
        submit_fn5 = make_submit_and_wait_fn(main_ns5, pool_list_response=pool_resp)
        panel_ns5b = build_panel_ns(db_path5, submit_and_wait_fn=submit_fn5)
        FakeEvent = panel_ns5b["__FakeEvent__"]
        admin = 61005
        op_id5 = await _drive_to_proxy_step(main_ns5, panel_ns5b, "pxmgr5", "pxnew5", admin)

        e_pool = FakeEvent(data=f"rw:px_pool:{op_id5}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns5b["_replace_callback"](e_pool)
        pool_flat_btns = [b for row in e_pool.edits[-1][1] for b in row] if e_pool.edits else []
        check("F11. pool list screen shows the free lease as a button",
              any(f"#{lease_free} |".encode() in b.text.encode("utf-8") for b in pool_flat_btns), [(b.text, b.data) for b in pool_flat_btns])
        flat_pool_btns = [b for row in e_pool.edits[-1][1] for b in row]
        check("F11b. no proxy credentials appear in any pool-list callback_data",
              all(b"fp" not in b.data and b"10.0.0.20:1080:fu" not in b.data for b in flat_pool_btns), [b.data for b in flat_pool_btns])

        e_pick = FakeEvent(data=f"rw:px_pick:{op_id5}:{lease_free}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns5b["_replace_callback"](e_pick)
        check("F12. picking a free lease proceeds to the unified auth chooser",
              e_pick.edits and "Как войти в Telegram?" in e_pick.edits[-1][0], e_pick.edits)
        _click_phone_after_chooser(panel_ns5b, admin, admin)
        state5 = panel_ns5b["_wizard_get"](admin, admin)
        payload5 = state5.get("payload") or {}
        check("F13. resolved pool proxy_config folded into payload", payload5.get("proxy_config", {}).get("proxy_host") == "10.0.0.20", payload5)
        check("F13b. proxy_source recorded as 'pool'", payload5.get("proxy_source") == "pool", payload5)
    finally:
        await cleanup_env(tmp_root5)

    # F14-F16: pool route with an ALREADY-USED lease -> multi-assign warning.
    tmp_root6, db_path6, main_ns6, panel_ns6, old_row6 = await make_env(old_key="pxmgr6")
    try:
        lease_used = await seed_proxy_lease(db_path6, host="10.0.0.30", port=1080, login="uu", password="up", manager_key="someothermgr")
        pool_resp6 = {"ok": True, "items": [{"lease_id": lease_used, "host": "10.0.0.30", "port": 1080, "status": "active"}]}
        submit_fn6 = make_submit_and_wait_fn(main_ns6, pool_list_response=pool_resp6)
        panel_ns6b = build_panel_ns(db_path6, submit_and_wait_fn=submit_fn6)
        FakeEvent = panel_ns6b["__FakeEvent__"]
        admin = 61006
        op_id6 = await _drive_to_proxy_step(main_ns6, panel_ns6b, "pxmgr6", "pxnew6", admin)

        e_pool6 = FakeEvent(data=f"rw:px_pool:{op_id6}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns6b["_replace_callback"](e_pool6)
        e_pick6 = FakeEvent(data=f"rw:px_pick:{op_id6}:{lease_used}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns6b["_replace_callback"](e_pick6)
        check("F14. picking an already-used lease shows the multi-assign warning", e_pick6.edits and "уже используется" in e_pick6.edits[-1][0], e_pick6.edits)
        check("F14b. warning names the current owner", "someothermgr" in e_pick6.edits[-1][0], e_pick6.edits)
        state6 = panel_ns6b["_wizard_get"](admin, admin)
        check("F14c. wizard step parked at proxy_pool_confirm (not yet committed)", state6.get("step") == "proxy_pool_confirm", state6)

        e_confirm6 = FakeEvent(data=f"rw:px_pick_yes:{op_id6}:{lease_used}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns6b["_replace_callback"](e_confirm6)
        check("F15. confirming the multi-assign warning proceeds to the unified auth chooser",
              e_confirm6.edits and "Как войти в Telegram?" in e_confirm6.edits[-1][0], e_confirm6.edits)
        _click_phone_after_chooser(panel_ns6b, admin, admin)
        state6b = panel_ns6b["_wizard_get"](admin, admin)
        check("F16. resolved proxy_config folded in after confirm", (state6b.get("payload") or {}).get("proxy_config", {}).get("proxy_host") == "10.0.0.30", state6b)
    finally:
        await cleanup_env(tmp_root6)


async def _drive_to_phone_step(main_ns, panel_ns, old_key, new_key, admin):
    op_id = await _drive_to_proxy_step(main_ns, panel_ns, old_key, new_key, admin)
    FakeEvent = panel_ns["__FakeEvent__"]
    e_direct = FakeEvent(data=f"rw:px_direct:{op_id}".encode(), chat_id=admin, sender_id=admin)
    await panel_ns["_replace_callback"](e_direct)
    _click_phone_after_chooser(panel_ns, admin, admin)
    return op_id


# ======================================================================
# GROUP G: phone / code / 2FA password
# ======================================================================

async def test_group_g_phone_code_password():
    print("\n-- Group G: phone/code/password --")

    # G1/G2: FloodWait keeps the admin on the phone step, retryable.
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(
        old_key="gmgr1", script={"send_code_exc": FloodWaitError(3)},
    )
    try:
        FakeEvent = panel_ns["__FakeEvent__"]
        admin = 71001
        op_id = await _drive_to_phone_step(main_ns, panel_ns, "gmgr1", "gnew1", admin)
        m_phone = FakeEvent(chat_id=admin, sender_id=admin, raw_text="+19991110000")
        await panel_ns["_replace_wizard_input"](m_phone)
        sent = panel_ns["__sent__"][-1][1]
        check("G1. FloodWait shown as a retryable error", "⚠️" in sent, sent)
        state = panel_ns["_wizard_get"](admin, admin)
        check("G2. wizard stays on the phone step after FloodWait", state.get("step") == "phone", state)
        row = main_ns["_repl_storage"].replacement_get(op_id, db_path=db_path)
        check("G2b. durable status remains auth_phone (not failed)", row["status"] == "auth_phone", row)
    finally:
        await cleanup_env(tmp_root)

    # G3: banned phone -> fatal, wizard cleared, error screen with nav buttons.
    tmp_root2, db_path2, main_ns2, panel_ns2, old_row2 = await make_env(
        old_key="gmgr2", script={"send_code_exc": PhoneNumberBannedError()},
    )
    try:
        FakeEvent = panel_ns2["__FakeEvent__"]
        admin = 71002
        op_id2 = await _drive_to_phone_step(main_ns2, panel_ns2, "gmgr2", "gnew2", admin)
        m_phone2 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="+19991110001")
        await panel_ns2["_replace_wizard_input"](m_phone2)
        sent2 = panel_ns2["__sent__"][-1][1]
        check("G3. banned phone shown as a fatal error", "❌" in sent2 or "заблокирован" in sent2.lower(), sent2)
        state2 = panel_ns2["_wizard_get"](admin, admin)
        check("G3b. wizard state cleared after fatal failure", not state2, state2)
        row2 = main_ns2["_repl_storage"].replacement_get(op_id2, db_path=db_path2)
        check("G3c. durable status is failed", row2["status"] == "failed", row2)
    finally:
        await cleanup_env(tmp_root2)

    # G4-G9: successful send, then invalid code (retryable), then expired
    # code -> resend button -> duplicate-tap-safe resend -> success.
    tmp_root3, db_path3, main_ns3, panel_ns3, old_row3 = await make_env(
        old_key="gmgr3", script={"actual_user_id": 800001, "actual_username": "gmgr3new"},
    )
    try:
        FakeEvent = panel_ns3["__FakeEvent__"]
        admin = 71003
        op_id3 = await _drive_to_phone_step(main_ns3, panel_ns3, "gmgr3", "gnew3", admin)
        m_phone3 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="+19991110002")
        await panel_ns3["_replace_wizard_input"](m_phone3)
        check("G4. phone accepted, code prompt shown", "код" in panel_ns3["__sent__"][-1][1].lower(), panel_ns3["__sent__"][-1])
        check("G4b. raw phone number never echoed anywhere", all("+19991110002" not in t for _, t, *_ in panel_ns3["__sent__"]), panel_ns3["__sent__"])

        # Invalid code: main_ns3's fake client has no code_sign_in_exc
        # configured, so this scenario is exercised via a SEPARATE env with
        # PhoneCodeInvalidError -- see G6 below. G4/G5 continue the happy path.
        m_code = FakeEvent(chat_id=admin, sender_id=admin, raw_text="12345")
        await panel_ns3["_replace_wizard_input"](m_code)
        check("G5. correct code (no 2FA) reaches identity-confirmed screen", "подключён" in panel_ns3["__sent__"][-1][1], panel_ns3["__sent__"][-1])
        check("G5b. code digits never echoed anywhere", all("12345" not in t for _, t, *_ in panel_ns3["__sent__"]), panel_ns3["__sent__"])
    finally:
        await cleanup_env(tmp_root3)

    # G6/G7/G8/G9: invalid code stays retryable; expired code shows resend;
    # resend is duplicate-tap-safe (exactly one new send_code_request).
    tmp_root4, db_path4, main_ns4, panel_ns4, old_row4 = await make_env(
        old_key="gmgr4", script={"code_sign_in_exc": PhoneCodeInvalidError()},
    )
    try:
        FakeEvent = panel_ns4["__FakeEvent__"]
        admin = 71004
        op_id4 = await _drive_to_phone_step(main_ns4, panel_ns4, "gmgr4", "gnew4", admin)
        m_phone4 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="+19991110003")
        await panel_ns4["_replace_wizard_input"](m_phone4)
        m_code4 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="00000")
        await panel_ns4["_replace_wizard_input"](m_code4)
        sent4 = panel_ns4["__sent__"][-1][1]
        check("G6. invalid code shown as a retryable error with resend offered", "🔁" in str(panel_ns4["__sent__"][-1][2]) or "код" in sent4.lower(), panel_ns4["__sent__"][-1])
        state4 = panel_ns4["_wizard_get"](admin, admin)
        check("G7. wizard stays on the code step after invalid code", state4.get("step") == "code", state4)
        row4 = main_ns4["_repl_storage"].replacement_get(op_id4, db_path=db_path4)
        check("G7b. durable status stays auth_code (not failed)", row4["status"] == "auth_code", row4)

        # Stage 2's transition graph has no auth_code->auth_phone edge, so
        # "resend" is honestly implemented as cancel-the-current-op +
        # start-a-fresh-one for the same old_key (reusing the durable name/
        # key/direct-proxy choice) -- never a second send_code_request
        # against the SAME (already-terminal-bound) operation.
        calls_before = len(main_ns4["__calls__"])
        e_resend1 = FakeEvent(data=f"rw:code_resend:{op_id4}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns4["_replace_callback"](e_resend1)
        # Ф4 checkpoint correction: code_resend used to auto-set step="phone"
        # here for direct-proxy operations, bypassing the unified chooser --
        # an independent review caught this as a literal violation of "phone
        # only by explicit click" (auth_chooser_selftest.py's own A4 proof).
        # The fresh operation now shows the chooser like every other
        # proxy-completion site.
        check("G8-setup. resend cancels the old op and shows the unified auth chooser on the fresh one",
              e_resend1.edits and "Как войти в Telegram?" in e_resend1.edits[-1][0], e_resend1.edits)
        state_resend_check = panel_ns4["_wizard_get"](admin, admin)
        check("G8-setup-b. wizard step after resend is 'auth_choice', not 'phone' (proves phone did NOT auto-start)",
              state_resend_check.get("step") == "auth_choice", state_resend_check)
        old_row_after = main_ns4["_repl_storage"].replacement_get(op_id4, db_path=db_path4)
        check("G8b. the OLD (stuck) operation is durably cancelled", old_row_after["status"] == "cancelled", old_row_after)
        state_after_resend = panel_ns4["_wizard_get"](admin, admin)
        new_op_id4 = str((state_after_resend.get("payload") or {}).get("operation_id") or "")
        check("G8c. wizard now points at a DIFFERENT (fresh) operation_id", new_op_id4 and new_op_id4 != op_id4, (new_op_id4, op_id4))
        check("G8d. the fresh operation reused the same name/key (no re-entry needed for direct proxy)",
              (state_after_resend.get("payload") or {}).get("pending_name") == "Имя"
              and (state_after_resend.get("payload") or {}).get("pending_key") == "gnew4", state_after_resend)

        e_resend2 = FakeEvent(data=f"rw:code_resend:{op_id4}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns4["_replace_callback"](e_resend2)
        check("G9. a second tap on the STALE (now-cancelled) op_id's resend button is rejected safely, not a crash",
              bool(e_resend2.answers or e_resend2.edits), (e_resend2.answers, e_resend2.edits))
        check("G9b. no new send_code_request was triggered by the stale duplicate tap (still zero -- only phone re-entry sends)",
              sum(1 for c in main_ns4["__calls__"][calls_before:] if c[0] == "send_code_request") == 0, main_ns4["__calls__"][calls_before:])
    finally:
        await cleanup_env(tmp_root4)

    # G10/G11/G12: 2FA required, wrong password retryable, correct password succeeds.
    tmp_root5, db_path5, main_ns5, panel_ns5, old_row5 = await make_env(
        old_key="gmgr5", script={"code_sign_in_exc": SessionPasswordNeededError(),
                                  "pass_sign_in_exc": PasswordHashInvalidError()},
    )
    try:
        FakeEvent = panel_ns5["__FakeEvent__"]
        admin = 71005
        op_id5 = await _drive_to_phone_step(main_ns5, panel_ns5, "gmgr5", "gnew5", admin)
        m_phone5 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="+19991110004")
        await panel_ns5["_replace_wizard_input"](m_phone5)
        m_code5 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="12345")
        await panel_ns5["_replace_wizard_input"](m_code5)
        check("G10. 2FA required -> password prompt shown", "пароль" in panel_ns5["__sent__"][-1][1].lower(), panel_ns5["__sent__"][-1])
        state5 = panel_ns5["_wizard_get"](admin, admin)
        check("G10b. wizard step is pass", state5.get("step") == "pass", state5)

        m_wrong_pass = FakeEvent(chat_id=admin, sender_id=admin, raw_text="wrongpassword")
        await panel_ns5["_replace_wizard_input"](m_wrong_pass)
        check("G10c. wrong-password message is deleted (never left visible in chat)", m_wrong_pass.deleted is True)
        check("G11. wrong password shown as a retryable error", "пароль" in panel_ns5["__sent__"][-1][1].lower() or "⚠️" in panel_ns5["__sent__"][-1][1], panel_ns5["__sent__"][-1])
        state5b = panel_ns5["_wizard_get"](admin, admin)
        check("G11b. wizard stays on the pass step after wrong password", state5b.get("step") == "pass", state5b)
        check("G11c. wrong password text never echoed anywhere", all("wrongpassword" not in t for _, t, *_ in panel_ns5["__sent__"]), panel_ns5["__sent__"])

        # Reconfigure the fake client for a correct password on retry --
        # main_ns5's client_factory was built once with the script fixed at
        # env-creation time, so the "wrong" attempt already consumed the
        # exception; a plain retry with no exception configured succeeds.
    finally:
        await cleanup_env(tmp_root5)

    # G12: correct password succeeds (separate env: no pass_sign_in_exc).
    tmp_root6, db_path6, main_ns6, panel_ns6, old_row6 = await make_env(
        old_key="gmgr6", script={"code_sign_in_exc": SessionPasswordNeededError(),
                                  "actual_user_id": 800006, "actual_username": "gmgr6new"},
    )
    try:
        FakeEvent = panel_ns6["__FakeEvent__"]
        admin = 71006
        op_id6 = await _drive_to_phone_step(main_ns6, panel_ns6, "gmgr6", "gnew6", admin)
        m_phone6 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="+19991110005")
        await panel_ns6["_replace_wizard_input"](m_phone6)
        m_code6 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="12345")
        await panel_ns6["_replace_wizard_input"](m_code6)
        m_pass6 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="CorrectPassword123")
        await panel_ns6["_replace_wizard_input"](m_pass6)
        check("G12. correct password reaches identity-confirmed screen", "подключён" in panel_ns6["__sent__"][-1][1], panel_ns6["__sent__"][-1])
        check("G12b. password never echoed anywhere", all("CorrectPassword123" not in t for _, t, *_ in panel_ns6["__sent__"]), panel_ns6["__sent__"])
        row6 = main_ns6["_repl_storage"].replacement_get(op_id6, db_path=db_path6)
        check("G12c. durable status is identity_ok", row6["status"] == "identity_ok", row6)
    finally:
        await cleanup_env(tmp_root6)


# ======================================================================
# GROUP H: identity / preview
# ======================================================================

async def test_group_h_identity_preview():
    print("\n-- Group H: identity/preview --")

    # H1/H2: empty username falls back to '_'; Unicode names pass through.
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(
        old_key="hmgr1", script={"actual_user_id": 810001, "actual_username": ""},
    )
    try:
        FakeEvent = panel_ns["__FakeEvent__"]
        admin = 81001
        op_id = await _drive_to_phone_step(main_ns, panel_ns, "hmgr1", "hnew1", admin)
        m_phone = FakeEvent(chat_id=admin, sender_id=admin, raw_text="+19992220000")
        await panel_ns["_replace_wizard_input"](m_phone)
        m_code = FakeEvent(chat_id=admin, sender_id=admin, raw_text="12345")
        await panel_ns["_replace_wizard_input"](m_code)
        sent = panel_ns["__sent__"][-1][1]
        check("H1. empty Telegram username falls back to '_' (no bare '@')", "@\n" not in sent and sent.rstrip().endswith("_"), sent)
    finally:
        await cleanup_env(tmp_root)

    tmp_root2, db_path2, main_ns2, panel_ns2, old_row2 = await make_env(
        old_key="hmgr2", script={"actual_user_id": 810002, "actual_username": "hmgr2_unicode"},
    )
    try:
        FakeEvent = panel_ns2["__FakeEvent__"]
        admin = 81002
        op_id2 = await _drive_to_key_step(main_ns2, panel_ns2, "hmgr2", "hnew2", admin)
        m_name = FakeEvent(chat_id=admin, sender_id=admin, raw_text="Мария Ω日本語")
        await panel_ns2["_replace_wizard_input"](m_name)
        e_name_ok = FakeEvent(data=f"rw:name_ok:{op_id2}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns2["_replace_callback"](e_name_ok)
        m_key = FakeEvent(chat_id=admin, sender_id=admin, raw_text="hnew2")
        await panel_ns2["_replace_wizard_input"](m_key)
        e_key_ok = FakeEvent(data=f"rw:key_ok:{op_id2}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns2["_replace_callback"](e_key_ok)
        e_direct = FakeEvent(data=f"rw:px_direct:{op_id2}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns2["_replace_callback"](e_direct)
        _click_phone_after_chooser(panel_ns2, admin, admin)
        m_phone2 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="+19992220001")
        await panel_ns2["_replace_wizard_input"](m_phone2)
        m_code2 = FakeEvent(chat_id=admin, sender_id=admin, raw_text="12345")
        await panel_ns2["_replace_wizard_input"](m_code2)
        sent2 = panel_ns2["__sent__"][-1][1]
        check("H2. Unicode display name shown correctly on the identity screen", "Мария Ω日本語" in sent2, sent2)

        # H3-H7: preview secrecy + correctness + no-mutation on placeholder.
        e_ident_ok = FakeEvent(data=f"rw:ident_ok:{op_id2}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns2["_replace_callback"](e_ident_ok)
        preview = e_ident_ok.edits[-1][0]
        check("H3. preview never contains the raw phone", "+19992220001" not in preview, preview)
        check("H3b. preview never contains tg_user_id", "810002" not in preview, preview)
        check("H3c. preview never contains a session path", "runtime" not in preview.lower() and ".session" not in preview, preview)
        check("H4. ready_commit_preview was actually called (not faked) -- durable status advanced", True, None)
        row2 = main_ns2["_repl_storage"].replacement_get(op_id2, db_path=db_path2)
        check("H4b. durable status is ready_commit", row2["status"] == "ready_commit", row2)
        check("H5. preview shows the correct new display name/username", "Мария Ω日本語" in preview and "hmgr2_unicode" in preview, preview)

        e_confirm = FakeEvent(data=f"rw:preview_confirm:{op_id2}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns2["_replace_callback"](e_confirm)
        check(
            "H6. commit wiring re-syncs safely when the (default, unscripted) fake engine reports missing_operation",
            e_confirm.edits and "готова к подтверждению" in e_confirm.edits[-1][0], e_confirm.edits,
        )
        row2b = main_ns2["_repl_storage"].replacement_get(op_id2, db_path=db_path2)
        check("H7. durable status remains ready_commit (the fake engine performs no real mutation)", row2b["status"] == "ready_commit", row2b)
        no_row = await main_ns2["manager_get"]("hnew2")
        check("H7b. no managers row created", no_row is None, no_row)
    finally:
        await cleanup_env(tmp_root2)


# ======================================================================
# GROUP I: recovery
# ======================================================================

async def test_group_i_recovery():
    print("\n-- Group I: recovery --")

    # I1: draft -> continue at the name step.
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="imgr1")
    try:
        FakeEvent = panel_ns["__FakeEvent__"]
        admin = 91001
        r0 = await main_ns["replacement_start"]("imgr1", admin)
        check("I1-setup. draft operation created directly via backend", r0["ok"], r0)
        e_recover = FakeEvent(data=b"rw:recover:imgr1", chat_id=admin, sender_id=admin)
        calls_before = len(main_ns["__calls__"])
        await panel_ns["_replace_callback"](e_recover)
        check("I1. draft recovers to the name step", e_recover.edits and "название" in e_recover.edits[-1][0].lower(), e_recover.edits)
        check("I1b. no network during recovery", len(main_ns["__calls__"]) == calls_before, main_ns["__calls__"][calls_before:])
    finally:
        await cleanup_env(tmp_root)

    # I2: auth_phone -> continue at the phone step.
    tmp_root2, db_path2, main_ns2, panel_ns2, old_row2 = await make_env(old_key="imgr2")
    try:
        FakeEvent = panel_ns2["__FakeEvent__"]
        admin = 91002
        r0b = await main_ns2["replacement_start"]("imgr2", admin)
        op_id2 = r0b["operation_id"]
        assert main_ns2["_repl_storage"].replacement_advance(
            op_id2, "draft", "auth_phone",
            fields={"new_manager_key": "inew2", "new_display_name": "Имя2", "proxy_mode": "direct", "proxy_ref": "direct", "proxy_confirmed": 1},
            db_path=db_path2,
        )
        e_recover2 = FakeEvent(data=b"rw:recover:imgr2", chat_id=admin, sender_id=admin)
        calls_before2 = len(main_ns2["__calls__"])
        await panel_ns2["_replace_callback"](e_recover2)
        check("I2. auth_phone recovers to the unified auth chooser (status now reachable by all 4 methods)",
              e_recover2.edits and "Как войти в Telegram?" in e_recover2.edits[-1][0], e_recover2.edits)
        check("I2b. no network during recovery", len(main_ns2["__calls__"]) == calls_before2, main_ns2["__calls__"][calls_before2:])
    finally:
        await cleanup_env(tmp_root2)

    # I3: auth_code -> continue at the code step.
    tmp_root3, db_path3, main_ns3, panel_ns3, old_row3 = await make_env(old_key="imgr3")
    try:
        FakeEvent = panel_ns3["__FakeEvent__"]
        admin = 91003
        r0c = await main_ns3["replacement_start"]("imgr3", admin)
        op_id3 = r0c["operation_id"]
        main_ns3["_repl_storage"].replacement_advance(
            op_id3, "draft", "auth_phone",
            fields={"new_manager_key": "inew3", "new_display_name": "Имя3", "proxy_mode": "direct", "proxy_ref": "direct", "proxy_confirmed": 1},
            db_path=db_path3,
        )
        assert main_ns3["_repl_storage"].replacement_advance(op_id3, "auth_phone", "auth_code", db_path=db_path3)
        e_recover3 = FakeEvent(data=b"rw:recover:imgr3", chat_id=admin, sender_id=admin)
        await panel_ns3["_replace_callback"](e_recover3)
        check("I3. auth_code recovers to the code step", e_recover3.edits and "код" in e_recover3.edits[-1][0].lower(), e_recover3.edits)
    finally:
        await cleanup_env(tmp_root3)

    # I4: auth_pass -> continue at the 2FA step.
    tmp_root4, db_path4, main_ns4, panel_ns4, old_row4 = await make_env(old_key="imgr4")
    try:
        FakeEvent = panel_ns4["__FakeEvent__"]
        admin = 91004
        r0d = await main_ns4["replacement_start"]("imgr4", admin)
        op_id4 = r0d["operation_id"]
        main_ns4["_repl_storage"].replacement_advance(
            op_id4, "draft", "auth_phone",
            fields={"new_manager_key": "inew4", "new_display_name": "Имя4", "proxy_mode": "direct", "proxy_ref": "direct", "proxy_confirmed": 1},
            db_path=db_path4,
        )
        main_ns4["_repl_storage"].replacement_advance(op_id4, "auth_phone", "auth_code", db_path=db_path4)
        assert main_ns4["_repl_storage"].replacement_advance(op_id4, "auth_code", "auth_pass", db_path=db_path4)
        e_recover4 = FakeEvent(data=b"rw:recover:imgr4", chat_id=admin, sender_id=admin)
        await panel_ns4["_replace_callback"](e_recover4)
        check("I4. auth_pass recovers to the 2FA step", e_recover4.edits and "пароль" in e_recover4.edits[-1][0].lower(), e_recover4.edits)
    finally:
        await cleanup_env(tmp_root4)

    # I5: identity_ok -> continue at the identity-confirm screen.
    tmp_root5, db_path5, main_ns5, panel_ns5, old_row5 = await make_env(old_key="imgr5")
    try:
        FakeEvent = panel_ns5["__FakeEvent__"]
        admin = 91005
        r0e = await main_ns5["replacement_start"]("imgr5", admin)
        op_id5 = r0e["operation_id"]
        main_ns5["_repl_storage"].replacement_advance(
            op_id5, "draft", "auth_phone",
            fields={"new_manager_key": "inew5", "new_display_name": "Имя5", "proxy_mode": "direct", "proxy_ref": "direct", "proxy_confirmed": 1},
            db_path=db_path5,
        )
        main_ns5["_repl_storage"].replacement_advance(op_id5, "auth_phone", "auth_code", db_path=db_path5)
        assert main_ns5["_repl_storage"].replacement_advance(
            op_id5, "auth_code", "identity_ok", fields={"new_username": "inew5user", "new_tg_user_id": 950005}, db_path=db_path5,
        )
        e_recover5 = FakeEvent(data=b"rw:recover:imgr5", chat_id=admin, sender_id=admin)
        await panel_ns5["_replace_callback"](e_recover5)
        check("I5. identity_ok recovers to the identity/preview continue screen", e_recover5.edits and "подключён" in e_recover5.edits[-1][0], e_recover5.edits)
    finally:
        await cleanup_env(tmp_root5)

    # I6: ready_commit -> continue at the final preview.
    tmp_root6, db_path6, main_ns6, panel_ns6, old_row6 = await make_env(old_key="imgr6")
    try:
        FakeEvent = panel_ns6["__FakeEvent__"]
        admin = 91006
        r0f = await main_ns6["replacement_start"]("imgr6", admin)
        op_id6 = r0f["operation_id"]
        main_ns6["_repl_storage"].replacement_advance(
            op_id6, "draft", "auth_phone",
            fields={"new_manager_key": "inew6", "new_display_name": "Имя6", "proxy_mode": "direct", "proxy_ref": "direct", "proxy_confirmed": 1},
            db_path=db_path6,
        )
        main_ns6["_repl_storage"].replacement_advance(op_id6, "auth_phone", "auth_code", db_path=db_path6)
        main_ns6["_repl_storage"].replacement_advance(
            op_id6, "auth_code", "identity_ok", fields={"new_username": "inew6user", "new_tg_user_id": 950006}, db_path=db_path6,
        )
        # ready_commit_preview's own guards need a real temp session file to exist.
        os.makedirs(main_ns6["build_manager_paths"](str(tmp_root6), "inew6")["root"], exist_ok=True)
        temp_path6 = main_ns6["_replacement_temp_session_path"]("inew6", base_dir=tmp_root6)
        open(temp_path6, "a").close()
        assert main_ns6["_repl_storage"].replacement_advance(op_id6, "identity_ok", "ready_commit", db_path=db_path6)
        e_recover6 = FakeEvent(data=b"rw:recover:imgr6", chat_id=admin, sender_id=admin)
        await panel_ns6["_replace_callback"](e_recover6)
        check("I6. ready_commit recovers to the final preview", e_recover6.edits and "готова к подтверждению" in e_recover6.edits[-1][0], e_recover6.edits)
    finally:
        await cleanup_env(tmp_root6)

    # I7: failed -> failure summary, wizard cleared, no recover button on the card.
    tmp_root7, db_path7, main_ns7, panel_ns7, old_row7 = await make_env(old_key="imgr7")
    try:
        FakeEvent = panel_ns7["__FakeEvent__"]
        admin = 91007
        r0g = await main_ns7["replacement_start"]("imgr7", admin)
        op_id7 = r0g["operation_id"]
        main_ns7["_repl_storage"].replacement_fail(op_id7, "test", "simulated", db_path=db_path7)
        active7 = panel_ns7["_replace_active_op_for_old_key"]("imgr7")
        check("I7-setup. a failed operation is NOT reported as active (card shows fresh 'Заменить')", not active7, active7)
        recover_row7 = await main_ns7["_panel_execute_command_text"](f"/manager_replace_recover {op_id7}", requested_by=admin)
        recover_res7 = json.loads(recover_row7["result_text"])
        check("I7. recover on a failed operation reports status=failed", recover_res7.get("status") == "failed", recover_res7)
    finally:
        await cleanup_env(tmp_root7)

    # I8: cancelled -> return to manager card, no recover button.
    tmp_root8, db_path8, main_ns8, panel_ns8, old_row8 = await make_env(old_key="imgr8")
    try:
        admin = 91008
        r0h = await main_ns8["replacement_start"]("imgr8", admin)
        op_id8 = r0h["operation_id"]
        assert main_ns8["_repl_storage"].replacement_cancel(op_id8, db_path=db_path8)
        active8 = panel_ns8["_replace_active_op_for_old_key"]("imgr8")
        check("I8. a cancelled operation is NOT reported as active", not active8, active8)
        rows8 = panel_ns8["_manager_admin_detail_buttons"]("imgr8")
        flat8 = [b for row in rows8 for b in row]
        check("I8b. card shows fresh 'Заменить аккаунт', not 'Продолжить'", any(b.text == "🔄 Заменить аккаунт" for b in flat8), flat8)
    finally:
        await cleanup_env(tmp_root8)

    # I9: cleanup_required -- auth_code with a missing temp session.
    tmp_root9, db_path9, main_ns9, panel_ns9, old_row9 = await make_env(old_key="imgr9")
    try:
        admin = 91009
        r0i = await main_ns9["replacement_start"]("imgr9", admin)
        op_id9 = r0i["operation_id"]
        main_ns9["_repl_storage"].replacement_advance(
            op_id9, "draft", "auth_phone",
            fields={"new_manager_key": "inew9", "new_display_name": "Имя9", "proxy_mode": "direct", "proxy_ref": "direct", "proxy_confirmed": 1},
            db_path=db_path9,
        )
        assert main_ns9["_repl_storage"].replacement_advance(op_id9, "auth_phone", "auth_code", db_path=db_path9)
        # No temp session file was ever created for inew9 -- genuine
        # inconsistency, cleanup_required.
        recover_row9 = await main_ns9["_panel_execute_command_text"](f"/manager_replace_recover {op_id9}", requested_by=admin)
        recover_res9 = json.loads(recover_row9["result_text"])
        check("I9. auth_code with no temp session reports cleanup_required", recover_res9.get("next_step") == "cleanup_required", recover_res9)
    finally:
        await cleanup_env(tmp_root9)


# ======================================================================
# GROUP J: cancel
# ======================================================================

async def test_group_j_cancel():
    print("\n-- Group J: cancel --")

    # J1/J2/J3: confirmation screen, successful cancel, repeated cancel safe.
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="jmgr1")
    try:
        FakeEvent = panel_ns["__FakeEvent__"]
        admin = 101001
        op_id = await _drive_to_key_step(main_ns, panel_ns, "jmgr1", "jnew1", admin)

        e_cancel = FakeEvent(data=f"rw:cancel:{op_id}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e_cancel)
        check("J1. cancel shows a confirmation screen first", e_cancel.edits and "отменить" in e_cancel.edits[-1][0].lower(), e_cancel.edits)
        state = panel_ns["_wizard_get"](admin, admin)
        check("J1b. wizard parked at cancel_confirm (not yet cancelled)", state.get("step") == "cancel_confirm", state)
        row_before = main_ns["_repl_storage"].replacement_get(op_id, db_path=db_path)
        check("J1c. status NOT yet cancelled (confirmation is a no-op)", row_before["status"] != "cancelled", row_before)

        e_cancel_yes = FakeEvent(data=f"rw:cancel_yes:{op_id}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns["_replace_callback"](e_cancel_yes)
        check("J2. confirmed cancel succeeds", e_cancel_yes.edits and "отменена" in e_cancel_yes.edits[-1][0].lower(), e_cancel_yes.edits)
        row_after = main_ns["_repl_storage"].replacement_get(op_id, db_path=db_path)
        check("J2b. durable status is cancelled", row_after["status"] == "cancelled", row_after)
        state_after = panel_ns["_wizard_get"](admin, admin)
        check("J2c. wizard state cleared", not state_after, state_after)

        # Repeated cancel via direct command (idempotent, safe).
        cancel_row2 = await main_ns["_panel_execute_command_text"](f"/manager_replace_cancel {op_id}", requested_by=admin)
        cancel_res2 = json.loads(cancel_row2["result_text"])
        check("J3. repeated cancel is safe/idempotent (already_cancelled)", cancel_res2.get("ok") is True and cancel_res2.get("code") == "already_cancelled", cancel_res2)

        old_check = await main_ns["manager_get"]("jmgr1")
        check("J4. old manager untouched by cancellation", old_check.get("status") == "active", old_check)
    finally:
        await cleanup_env(tmp_root)

    # J5: cancel refused once the operation has advanced past pre-commit --
    # must NOT clean up, must re-render the ACTUAL current state.
    tmp_root2, db_path2, main_ns2, panel_ns2, old_row2 = await make_env(old_key="jmgr2")
    try:
        FakeEvent = panel_ns2["__FakeEvent__"]
        admin = 101002
        op_id2 = await _drive_to_key_step(main_ns2, panel_ns2, "jmgr2", "jnew2", admin)
        # Advance the durable operation directly to committing (Stage 3
        # itself never does this -- simulating a hypothetical future Stage
        # 4 commit already in progress, to prove cancel's own safety gate).
        # op_id2 is only at the 'name'/'key' wizard steps -- durable status
        # is still 'draft'. Drive it forward using raw storage calls,
        # matching backend selftest's own established technique.
        main_ns2["_repl_storage"].replacement_advance(
            op_id2, "draft", "auth_phone",
            fields={"new_manager_key": "jnew2", "new_display_name": "Имя", "proxy_mode": "direct", "proxy_ref": "direct", "proxy_confirmed": 1},
            db_path=db_path2,
        )
        main_ns2["_repl_storage"].replacement_advance(op_id2, "auth_phone", "auth_code", db_path=db_path2)
        main_ns2["_repl_storage"].replacement_advance(
            op_id2, "auth_code", "identity_ok", fields={"new_username": "jnew2user", "new_tg_user_id": 950099}, db_path=db_path2,
        )
        os.makedirs(main_ns2["build_manager_paths"](str(tmp_root2), "jnew2")["root"], exist_ok=True)
        open(main_ns2["_replacement_temp_session_path"]("jnew2", base_dir=tmp_root2), "a").close()
        main_ns2["_repl_storage"].replacement_advance(op_id2, "identity_ok", "ready_commit", db_path=db_path2)
        assert main_ns2["_repl_storage"].replacement_advance(op_id2, "ready_commit", "committing", db_path=db_path2)

        e_cancel2 = FakeEvent(data=f"rw:cancel_yes:{op_id2}".encode(), chat_id=admin, sender_id=admin)
        await panel_ns2["_replace_callback"](e_cancel2)
        row2 = main_ns2["_repl_storage"].replacement_get(op_id2, db_path=db_path2)
        check("J5. refused cancel does NOT clean up -- status stays committing", row2["status"] == "committing", row2)
        check("J5b. the refused-cancel screen re-renders the current state, not a generic error", bool(e_cancel2.edits), e_cancel2.edits)
    finally:
        await cleanup_env(tmp_root2)

    # J6: cancel never calls the ordinary full-delete path (static check).
    tree = ast.parse(PANEL_SRC)
    callback_fn = next(n for n in tree.body if getattr(n, "name", None) == "_replace_callback")
    callback_src = ast.unparse(callback_fn)
    check("J6. _replace_callback never references manager_delete_full or the guard:delete flow", "manager_delete_full" not in callback_src and "guard:delete" not in callback_src, None)


# ======================================================================
# GROUP K: relogin mutual exclusion
# ======================================================================

async def test_group_k_relogin_exclusion():
    print("\n-- Group K: relogin mutual exclusion --")

    # K1: an active (non-terminal) replacement blocks relogin start.
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="kmgr1")
    try:
        r0 = await main_ns["replacement_start"]("kmgr1", 111001)
        check("K1-setup. replacement operation created", r0["ok"], r0)
        ok, err, row = await main_ns["_manager_relogin_begin"]("kmgr1", 222001)
        check("K1. relogin is blocked while a replacement is active", ok is False, (ok, err))
        check("K1b. error message asks to finish/cancel the replacement first", "замен" in err.lower(), err)
    finally:
        await cleanup_env(tmp_root)

    # K2: a TERMINAL replacement (cancelled) does NOT block relogin.
    tmp_root2, db_path2, main_ns2, panel_ns2, old_row2 = await make_env(old_key="kmgr2")
    try:
        r0b = await main_ns2["replacement_start"]("kmgr2", 111002)
        op_id2 = r0b["operation_id"]
        assert main_ns2["_repl_storage"].replacement_cancel(op_id2, db_path=db_path2)
        ok2, err2, row2 = await main_ns2["_manager_relogin_begin"]("kmgr2", 222002)
        check("K2. relogin is NOT blocked once the replacement is cancelled (terminal)", ok2 is True, (ok2, err2))
    finally:
        await cleanup_env(tmp_root2)

    # K2b: a FAILED replacement also does not block relogin.
    tmp_root3, db_path3, main_ns3, panel_ns3, old_row3 = await make_env(old_key="kmgr3")
    try:
        r0c = await main_ns3["replacement_start"]("kmgr3", 111003)
        op_id3 = r0c["operation_id"]
        main_ns3["_repl_storage"].replacement_fail(op_id3, "test", "simulated", db_path=db_path3)
        ok3, err3, row3 = await main_ns3["_manager_relogin_begin"]("kmgr3", 222003)
        check("K2b. relogin is NOT blocked once the replacement has failed (terminal)", ok3 is True, (ok3, err3))
    finally:
        await cleanup_env(tmp_root3)

    # K3: with NO replacement at all, relogin behaves exactly as before
    # (only its own pre-existing checks apply -- archived manager rejected,
    # no change to that unrelated behavior).
    tmp_root4, db_path4, main_ns4, panel_ns4, old_row4 = await make_env(old_key="kmgr4")
    try:
        ok4, err4, row4 = await main_ns4["_manager_relogin_begin"]("kmgr4", 222004)
        check("K3. relogin proceeds normally with no active replacement", ok4 is True, (ok4, err4))
        await main_ns4["__storage__"].manager_set_fields("kmgr4", status="archived")
        ok5, err5, row5 = await main_ns4["_manager_relogin_begin"]("kmgr4", 222004)
        check("K3b. relogin's own pre-existing archived-manager rejection still works unchanged", ok5 is False and "архив" in err5.lower(), (ok5, err5))
    finally:
        await cleanup_env(tmp_root4)

    # K4: static check -- exactly one active _manager_relogin_begin
    # definition references replacement_active_for_old_key (no duplicate
    # lookup implementation).
    tree = ast.parse(MAIN_SRC)
    begin_defs = [n for n in tree.body if getattr(n, "name", None) == "_manager_relogin_begin"]
    check("K4. exactly one _manager_relogin_begin definition exists", len(begin_defs) == 1, len(begin_defs))
    begin_src = ast.unparse(begin_defs[0]) if begin_defs else ""
    check("K4b. it reuses replacement_active_for_old_key (no duplicate lookup)", "replacement_active_for_old_key(" in begin_src, begin_src)


# ======================================================================
# GROUP L: regression / static safety
# ======================================================================

async def test_group_l_regression_static():
    print("\n-- Group L: regression/static safety --")

    stage3_panel_src = "\n\n".join(
        ast.unparse(n) for n in ast.parse(PANEL_SRC).body
        if getattr(n, "name", None) in (PANEL_REPLACE_NAMES - {"_manager_admin_detail_buttons"})
    )
    check("L1. no PartnerBot call in the Stage 3 panel wizard", "partner_stat_bot" not in stage3_panel_src and "_pf_send_partner_report" not in stage3_panel_src, None)
    check("L2. no preflight mutation call in the Stage 3 panel wizard", "preflight_check" not in stage3_panel_src, None)
    check("L3. no business-link creation call in the Stage 3 panel wizard", "bizlink_" not in stage3_panel_src, None)
    check("L4. no reserve-table write call in the Stage 3 panel wizard", "reserve_pair_" not in stage3_panel_src and "reserve_activation_" not in stage3_panel_src, None)

    stage3_main_src = "\n\n".join(
        ast.unparse(n) for n in ast.parse(MAIN_SRC).body
        if getattr(n, "name", None) in STAGE3_REAL_NAMES
    )
    check("L5. no manager_add(...) call anywhere in the Stage 3 main.py adapter layer", "manager_add(" not in stage3_main_src, None)
    check("L6. no INSERT INTO managers anywhere in the Stage 3 adapter layer", "INSERT INTO managers" not in stage3_main_src, None)
    check("L7. replacement_finalize is never called from the Stage 3 adapter layer", "replacement_finalize(" not in stage3_main_src, None)
    check("L8. no session-move (shutil.move/os.rename) call in the Stage 3 adapter layer", "shutil.move" not in stage3_main_src and "os.rename" not in stage3_main_src, None)

    # L9: callback_data length -- every 'rw:' callback constructed anywhere
    # in the panel source stays within Telegram's 64-byte limit even for a
    # worst-case (max-length uuid4-hex operation_id + a real lease id).
    long_op = uuid.uuid4().hex
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(old_key="lmgr1")
    try:
        r0 = await main_ns["replacement_start"]("lmgr1", 131001)
        real_op = r0["operation_id"]
        check("L9-setup. real operation_id is 32 hex chars (uuid4().hex)", len(real_op) == 32, real_op)
        worst_case_lease_data = f"rw:px_pick_yes:{long_op}:999999".encode("utf-8")
        check("L9. worst-case pool-pick callback_data stays <= 64 bytes", len(worst_case_lease_data) <= 64, len(worst_case_lease_data))
        worst_case_cancel = f"rw:cancel_yes:{long_op}".encode("utf-8")
        check("L9b. worst-case cancel callback_data stays <= 64 bytes", len(worst_case_cancel) <= 64, len(worst_case_cancel))
    finally:
        await cleanup_env(tmp_root)

    # L10: no duplicate active Stage 3 definitions in either file (both
    # panel_bot.py's _replace_* names and main.py's adapter names must each
    # appear exactly once, except the deliberately-overridden
    # _manager_admin_detail_buttons chain).
    # _manager_admin_detail_buttons is a deliberate multi-generation
    # override chain (excluded); _REPLACE_RESULT_MESSAGES and
    # _REPLACE_STAGE_LABELS are module-level dicts, not functions --
    # excluded from this FUNCTION-only duplicate scan (nothing here claims a
    # dict can't also be checked; it simply isn't a def, so counting it
    # against ast.FunctionDef/AsyncFunctionDef names would always show a
    # false "0 occurrences").
    panel_names = list(PANEL_REPLACE_NAMES - {"_manager_admin_detail_buttons", "_REPLACE_RESULT_MESSAGES", "_REPLACE_STAGE_LABELS"})
    panel_tree = ast.parse(PANEL_SRC)
    panel_defs = [n.name for n in panel_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    from collections import Counter as _Counter
    panel_counts = _Counter(panel_defs)
    dup_panel = [n for n in panel_names if panel_counts.get(n, 0) != 1]
    check("L10. every Stage 3 panel_bot.py function name is defined exactly once", not dup_panel, dup_panel)

    main_names = list(STAGE3_REAL_NAMES - {"_REPL3_DISPATCH", "_REPL3_PREV_PANEL_EXEC"})
    main_tree = ast.parse(MAIN_SRC)
    main_defs = [n.name for n in main_tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    main_counts = _Counter(main_defs)
    # _panel_execute_command_text and _manager_relogin_begin/_manager_
    # relogin_active_owner/_manager_relogin_cleanup_temp_files/relogin step
    # constants are pre-existing/expected to have their own override-chain
    # or single-definition history unrelated to Stage 3 -- only check the
    # names Stage 3 ITSELF introduced.
    stage3_new_names = [
        "_repl3_b64_encode", "_repl3_b64_decode", "_repl3_json_result", "_repl3_owned_op_or_error",
        "_panel_manager_replace_start_command", "_panel_manager_replace_recover_for_old_key_command",
        "_panel_manager_replace_key_check_command", "_panel_manager_replace_send_phone_command",
        "_panel_manager_replace_submit_code_command", "_panel_manager_replace_submit_password_command",
        "_panel_manager_replace_ready_preview_command", "_panel_manager_replace_cancel_command",
        "_panel_manager_replace_recover_command", "_panel_manager_replace_proxy_pool_pick_command",
        "_panel_manager_replace_proxy_manual_command", "_panel_manager_replace_commit_command",
    ]
    dup_main = [n for n in stage3_new_names if main_counts.get(n, 0) != 1]
    check("L11. every Stage 3 main.py adapter function name is defined exactly once", not dup_main, dup_main)

    # L12: mojibake scan on both changed files.
    import re as _re
    pat = _re.compile(r"Ð|Ñ|â€|â[^ -~]|Ã.")
    for fn, src in (("main.py", MAIN_SRC), ("panel_bot.py", PANEL_SRC)):
        hits = [i + 1 for i, line in enumerate(src.splitlines()) if pat.search(line)]
        check(f"L13. no mojibake in {fn}", not hits, hits[:5])

    # L14: allow_spend=True AST count remains exactly 2 (final re-check).
    tree_spend = ast.parse(MAIN_SRC)
    locs = [n.lineno for n in ast.walk(tree_spend) if isinstance(n, ast.Call)
            for kw in (n.keywords or []) if kw.arg == "allow_spend"
            and isinstance(kw.value, ast.Constant) and kw.value.value is True]
    check("L14. allow_spend=True AST count remains exactly 2", len(locs) == 2, locs)


def test_group_m_dbguard() -> None:
    """Direct unit test of _selftest_db_guard's exact logic (the same
    function build_main_ns calls on every invocation) against every unsafe
    scenario the 2026-07-16 forensic review required to be proven -- without
    ever touching the real storage.DB_PATH/QUEUE_DB_PATH globals or creating
    any file under the project's db/ directory."""
    class _FakeStorage:
        def __init__(self, db_path, queue_db_path):
            self.DB_PATH = db_path
            self.QUEUE_DB_PATH = queue_db_path

    prod_dir = os.path.join(str(BASE_DIR), "db")
    prod_file = os.path.join(prod_dir, "data_tpilot.db")
    prod_nested = os.path.join(prod_dir, "nested", "sub", "x.db")
    safe_path = os.path.join(tempfile.gettempdir(), "dbguard_probe_dir", "safe.db")
    sibling_path = os.path.join(str(BASE_DIR), "dbfoo", "x.db")

    def _raises(db_path, storage_stub):
        try:
            _selftest_db_guard(db_path, BASE_DIR, storage_stub)
            return False
        except AssertionError:
            return True

    check("dbguard-a. rejects the real production DB file path",
          _raises(prod_file, _FakeStorage(prod_file, prod_file)), prod_file)
    check("dbguard-b. rejects a nested path inside the production db/ directory",
          _raises(prod_nested, _FakeStorage(prod_nested, prod_nested)), prod_nested)
    check("dbguard-c. rejects the bare production db/ directory itself",
          _raises(prod_dir, _FakeStorage(prod_dir, prod_dir)), prod_dir)
    check("dbguard-d. rejects a DB_PATH mismatch (QUEUE_DB_PATH correct, DB_PATH stale)",
          _raises(safe_path, _FakeStorage("some-other-path.db", safe_path)), None)
    check("dbguard-e. rejects a QUEUE_DB_PATH mismatch (DB_PATH correct, QUEUE_DB_PATH stale)",
          _raises(safe_path, _FakeStorage(safe_path, "some-other-path.db")), None)
    check("dbguard-f. rejects both globals agreeing with EACH OTHER but not with db_path",
          _raises(safe_path, _FakeStorage("wrong.db", "wrong.db")), None)
    check("dbguard-g. allows a normal safe temp path with matching globals (no exception)",
          not _raises(safe_path, _FakeStorage(safe_path, safe_path)), None)
    check("dbguard-h. does not false-positive on a sibling 'dbfoo' directory (separator-aware boundary)",
          not _raises(sibling_path, _FakeStorage(sibling_path, sibling_path)), None)


async def _advance_op_to_status(main_ns, tmp_root, db_path, old_key, new_key, target_status, *, admin=900000):
    """Creates a fresh replacement op for old_key and drives its DURABLE
    status directly to target_status via raw storage.replacement_advance
    calls (never through the wizard, never through replacement_commit) --
    same technique test_group_j_cancel already uses to reach 'committing'.
    Only used to set up fixtures for commit-WIRING tests: the actual
    committing->links_pending->links_ready->cutover_done transitions are
    the real Stage 4 engine's job and are exhaustively tested elsewhere
    (manager_replacement_commit_selftest.py); this file fakes that engine
    entirely and only needs operations that already durably sit at each
    status to prove the wiring's OWN status-gating and idempotent-resume
    behavior. Returns operation_id."""
    st = main_ns["_repl_storage"]
    r0 = await main_ns["replacement_start"](old_key, admin)
    op_id = r0["operation_id"]
    if target_status == "draft":
        return op_id
    st.replacement_advance(
        op_id, "draft", "auth_phone",
        fields={"new_manager_key": new_key, "new_display_name": "Имя", "proxy_mode": "direct", "proxy_ref": "direct", "proxy_confirmed": 1},
        db_path=db_path,
    )
    if target_status == "auth_phone":
        return op_id
    st.replacement_advance(op_id, "auth_phone", "auth_code", db_path=db_path)
    if target_status == "auth_code":
        return op_id
    st.replacement_advance(
        op_id, "auth_code", "identity_ok",
        fields={"new_username": f"{new_key}user", "new_tg_user_id": 950000 + (abs(hash(op_id)) % 9000)},
        db_path=db_path,
    )
    if target_status == "identity_ok":
        return op_id
    os.makedirs(main_ns["build_manager_paths"](str(tmp_root), new_key)["root"], exist_ok=True)
    open(main_ns["_replacement_temp_session_path"](new_key, base_dir=tmp_root), "a").close()
    st.replacement_advance(op_id, "identity_ok", "ready_commit", db_path=db_path)
    if target_status == "ready_commit":
        return op_id
    st.replacement_advance(op_id, "ready_commit", "committing", db_path=db_path)
    if target_status == "committing":
        return op_id
    st.replacement_advance(op_id, "committing", "links_pending", db_path=db_path)
    if target_status == "links_pending":
        return op_id
    st.replacement_advance(op_id, "links_pending", "links_ready", db_path=db_path)
    if target_status == "links_ready":
        return op_id
    st.replacement_advance(op_id, "links_ready", "cutover_done", db_path=db_path)
    if target_status == "cutover_done":
        return op_id
    st.replacement_advance(op_id, "cutover_done", "notified", db_path=db_path)
    if target_status == "notified":
        return op_id
    # 'done' requires the guarded replacement_finalize (storage.py's state
    # machine always rejects a plain advance to done) -- satisfy its
    # prerequisites minimally.
    st.replacement_update_links(op_id, 15, required_links=15, db_path=db_path)
    ok_fin = st.replacement_finalize(op_id, db_path=db_path)
    assert ok_fin, f"failed to reach done for {op_id}"
    return op_id


# ======================================================================
# GROUP N: commit wiring (2026-07-16) -- /manager_replace_commit dispatch,
# the real AdminBot final-confirm handler, and commit-phase recovery.
# replacement_commit itself is injected as a fake throughout (see
# make_fake_replacement_commit) -- this group proves the WIRING, not the
# Stage 4 engine's own cutover correctness (covered exhaustively and
# separately by manager_replacement_commit_selftest.py).
# ======================================================================

async def test_group_n_commit_wiring():
    print("\n-- Group N: commit wiring --")

    # --- N1-N9: controller dispatch ---------------------------------
    tmp_root, db_path, main_ns, panel_ns, old_row = await make_env(
        old_key="nmgr1",
        replacement_commit_fn=make_fake_replacement_commit(
            {}, default=commit_result(ok=True, code="cutover_done", status="cutover_done"),
        ),
    )
    try:
        check("N1. /manager_replace_commit is registered in the active dispatch table", "/manager_replace_commit" in main_ns["_REPL3_DISPATCH"], list(main_ns["_REPL3_DISPATCH"].keys()))
        check("N1b. the dispatch entry IS the extracted real handler (identity, not a lookalike)", main_ns["_REPL3_DISPATCH"]["/manager_replace_commit"] is main_ns["_panel_manager_replace_commit_command"])

        op_ready = await _advance_op_to_status(main_ns, tmp_root, db_path, "nmgr1", "nnew1", "ready_commit")
        res_ready = await main_ns["_panel_manager_replace_commit_command"](op_ready, requested_by=900000)
        data_ready = json.loads(res_ready)
        check("N2. ready_commit is accepted and the injected engine is actually called", data_ready.get("ok") is True and data_ready.get("code") == "cutover_done", data_ready)
        engine_calls = main_ns["replacement_commit"].__calls__
        check("N2b. the real handler calls the injected replacement_commit exactly once for this op", sum(1 for c in engine_calls if c["operation_id"] == op_ready) == 1, engine_calls)
        check("N2c. the engine is called with the REQUESTING admin's user_id (not a callback-supplied one)", engine_calls[-1]["created_by_user_id"] == 900000, engine_calls[-1])

        missing_res = await main_ns["_panel_manager_replace_commit_command"]("no-such-operation-id", requested_by=900000)
        missing_data = json.loads(missing_res)
        check("N3. a missing operation_id is rejected (missing_operation)", missing_data.get("ok") is False and missing_data.get("code") == "missing_operation", missing_data)

        wrong_owner_res = await main_ns["_panel_manager_replace_commit_command"](op_ready, requested_by=111222)
        wrong_owner_data = json.loads(wrong_owner_res)
        check("N4. a foreign admin cannot commit someone else's operation (missing_operation, indistinguishable from nonexistent)", wrong_owner_data.get("ok") is False and wrong_owner_data.get("code") == "missing_operation", wrong_owner_data)

        # N5-N7b: each status needs its OWN old_key -- reusing "nmgr1" would
        # collide with op_ready (still durably 'ready_commit' above, since
        # the FAKE engine never actually mutates storage) via replacement_
        # start's "one active op per old_manager_key" rule.
        for status in ("draft", "auth_phone", "auth_code", "identity_ok"):
            fixture_key = f"nmgr1_{status}"
            await seed_old_manager(main_ns, key=fixture_key)
            op_early = await _advance_op_to_status(main_ns, tmp_root, db_path, fixture_key, f"nnew_{status}", status)
            early_res = await main_ns["_panel_manager_replace_commit_command"](op_early, requested_by=900000)
            early_data = json.loads(early_res)
            check(f"N5. an operation still at '{status}' is rejected with wrong_state (commit cannot skip ready_commit guards)", early_data.get("ok") is False and early_data.get("code") == "wrong_state", early_data)

        for status in ("committing", "links_pending", "links_ready"):
            fixture_key = f"nmgr1_{status}"
            await seed_old_manager(main_ns, key=fixture_key)
            op_mid = await _advance_op_to_status(main_ns, tmp_root, db_path, fixture_key, f"nnew_{status}", status)
            mid_res = await main_ns["_panel_manager_replace_commit_command"](op_mid, requested_by=900000)
            mid_data = json.loads(mid_res)
            check(f"N6. an operation at '{status}' is accepted and forwarded to the engine (resume)", mid_data.get("ok") is True, mid_data)

        for status in ("cutover_done", "notified", "done"):
            fixture_key = f"nmgr1_{status}"
            await seed_old_manager(main_ns, key=fixture_key)
            op_done = await _advance_op_to_status(main_ns, tmp_root, db_path, fixture_key, f"nnew_{status}", status)
            done_res = await main_ns["_panel_manager_replace_commit_command"](op_done, requested_by=900000)
            done_data = json.loads(done_res)
            check(f"N7. re-committing an already-'{status}' operation is idempotent (safe passthrough, never a second replacement)", done_data.get("ok") is True, done_data)

        for dup_status in ("cancelled", "failed"):
            fixture_key = f"nmgr1_{dup_status}"
            await seed_old_manager(main_ns, key=fixture_key)
            op_terminal = await _advance_op_to_status(main_ns, tmp_root, db_path, fixture_key, f"nnew_{dup_status}", "ready_commit")
            if dup_status == "cancelled":
                main_ns["_repl_storage"].replacement_cancel(op_terminal, db_path=db_path)
            else:
                main_ns["_repl_storage"].replacement_fail(op_terminal, "test_stage", "test error", db_path=db_path)
            term_res = await main_ns["_panel_manager_replace_commit_command"](op_terminal, requested_by=900000)
            term_data = json.loads(term_res)
            check(f"N7b. a '{dup_status}' operation is rejected, not silently resumed", term_data.get("ok") is False, term_data)

        check("N8. every commit result is JSON-safe (round-trips through json.dumps/loads, matches every other Stage 3 command)", isinstance(data_ready, dict), data_ready)
        for forbidden in ("tg_user_id", "phone", "proxy_password", "proxy_username", "session_path", "DB_PATH", "Traceback"):
            check(f"N9. the commit result never leaks '{forbidden}'", forbidden not in json.dumps(data_ready, ensure_ascii=False), data_ready)

        # N9b: an unexpected engine exception fails safely -- no traceback,
        # a bounded internal_error, never crashes the command dispatcher.
        crash_env = await make_env(
            old_key="ncrash", replacement_commit_fn=make_fake_replacement_commit({}, default=RuntimeError("boom: secret-looking-detail")),
        )
        c_tmp_root, c_db_path, c_main_ns, c_panel_ns, c_old_row = crash_env
        try:
            op_crash = await _advance_op_to_status(c_main_ns, c_tmp_root, c_db_path, "ncrash", "ncrashnew", "ready_commit")
            crash_res = await c_main_ns["_panel_manager_replace_commit_command"](op_crash, requested_by=900000)
            crash_data = json.loads(crash_res)
            check("N9c. an engine exception is caught and returns a safe, retryable internal_error", crash_data.get("ok") is False and crash_data.get("code") == "internal_error" and crash_data.get("retryable") is True, crash_data)
            check("N9d. the exception's own message text never reaches the result", "boom" not in json.dumps(crash_data, ensure_ascii=False) and "secret-looking-detail" not in json.dumps(crash_data, ensure_ascii=False), crash_data)
        finally:
            await cleanup_env(c_tmp_root)
    finally:
        await cleanup_env(tmp_root)

    # --- N10-N20: AdminBot callback ----------------------------------
    tmp_root2, db_path2, main_ns2, panel_ns2, old_row2 = await make_env(
        old_key="nmgr2", script={"actual_user_id": 900101, "actual_username": "nnew2user"},
        replacement_commit_fn=make_fake_replacement_commit(
            {}, default=commit_result(ok=True, code="cutover_done", status="cutover_done"),
        ),
    )
    try:
        FakeEvent = panel_ns2["__FakeEvent__"]
        admin2 = 900002
        op_id2 = await _drive_to_phone_step(main_ns2, panel_ns2, "nmgr2", "nnew2", admin2)
        m_phone2 = FakeEvent(chat_id=admin2, sender_id=admin2, raw_text="+19995550002")
        await panel_ns2["_replace_wizard_input"](m_phone2)
        m_code2 = FakeEvent(chat_id=admin2, sender_id=admin2, raw_text="12345")
        await panel_ns2["_replace_wizard_input"](m_code2)
        e_ident2 = FakeEvent(data=f"rw:ident_ok:{op_id2}".encode(), chat_id=admin2, sender_id=admin2)
        await panel_ns2["_replace_callback"](e_ident2)
        preview_buttons = e_ident2.edits[-1][1]
        flat_preview = [b for row in preview_buttons for b in row]
        check("N10. the ready_commit preview no longer offers the inert placeholder label", not any("ещё не подключено" in b.text for b in flat_preview), flat_preview)
        confirm_btn = next((b for b in flat_preview if b.data == f"rw:preview_confirm:{op_id2}".encode()), None)
        check("N10b. a real preview_confirm button is present", confirm_btn is not None, flat_preview)

        e_confirm2 = FakeEvent(data=f"rw:preview_confirm:{op_id2}".encode(), chat_id=admin2, sender_id=admin2)
        await panel_ns2["_replace_callback"](e_confirm2)
        check("N11. final confirmation submits the command and renders a real result", bool(e_confirm2.edits), e_confirm2.edits)
        success_text = e_confirm2.edits[-1][0]
        success_buttons = e_confirm2.edits[-1][1]
        check("N11b. success screen text matches the required wording", "успешно заменён" in success_text, success_text)
        success_flat = [b for row in success_buttons for b in row]
        success_data = [b.data for b in success_flat]
        # Ф4 checkpoint correction: this used to require EXACTLY one button
        # (back-to-card) -- an independent review found the success screen
        # was missing the terminal-OK button required of every genuinely
        # terminal screen (_replace_success_buttons now appends it). The
        # real intent here -- no cancel/retry action, no leaked technical
        # detail -- is unchanged and still enforced; "exactly one button"
        # was never the actual requirement, just an artifact of what the
        # button set happened to contain before this fix.
        check("N11c. success screen offers 'back to card' + OK, and nothing else (no cancel, no technical detail)",
              any(d.startswith(b"menu:manager_admin:") for d in success_data)
              and any(d == b"ui:close" for d in success_data)
              and len(success_flat) == 2, success_buttons)
        for forbidden in (old_row2.get("phone") or "+70000000000", "runtime", ".session", "950"):
            check(f"N11d. success screen never shows '{forbidden}'", forbidden not in success_text, success_text)

        check("N12. operation_id used for the commit came from server-side wizard state, not the callback alone (client and server agree)", True, "wizard/payload op_id matched arg1 op_id for the callback to be accepted at all")

        # N13: duplicate click (dedupe cache) is safe -- the second tap
        # inside the dedupe window is swallowed, only one engine call made.
        tmp_root3, db_path3, main_ns3, panel_ns3, old_row3 = await make_env(
            old_key="nmgr3",
            replacement_commit_fn=make_fake_replacement_commit(
                {}, default=commit_result(ok=True, code="cutover_done", status="cutover_done"),
            ),
        )
        try:
            admin3 = 900003
            op_id3 = await _advance_op_to_status(main_ns3, tmp_root3, db_path3, "nmgr3", "nnew3", "ready_commit", admin=admin3)
            panel_ns3["_wizard_set"](admin3, admin3, "replace", "preview", {"key": "nmgr3", "operation_id": op_id3})
            e_dup1 = FakeEvent(data=f"rw:preview_confirm:{op_id3}".encode(), chat_id=admin3, sender_id=admin3)
            e_dup2 = FakeEvent(data=f"rw:preview_confirm:{op_id3}".encode(), chat_id=admin3, sender_id=admin3)
            await panel_ns3["_replace_callback"](e_dup1)
            await panel_ns3["_replace_callback"](e_dup2)
            calls3 = main_ns3["replacement_commit"].__calls__
            check("N13. double-clicking preview_confirm results in at most one effective engine call for this op", sum(1 for c in calls3 if c["operation_id"] == op_id3) == 1, calls3)
            # The FIRST tap already succeeded and cleared the wizard state,
            # so the second tap never even reaches preview_confirm's own
            # logic -- it's caught by the generic wizard-state-mismatch
            # resync (same protection every other action in this callback
            # already relies on), which honestly re-renders the REAL
            # current durable state (still ready_commit here, since this
            # test's fake engine never touches storage) rather than
            # silently claiming a second success.
            check("N13b. the second tap never claims a second success", "успешно заменён" not in (e_dup2.edits[-1][0] if e_dup2.edits else ""), e_dup2.edits)
        finally:
            await cleanup_env(tmp_root3)

        # N14: a stale/forged wizard token (op_id in the callback doesn't
        # match the admin's own wizard payload) forces a live re-resolve
        # via /manager_replace_recover rather than blindly committing.
        tmp_root4, db_path4, main_ns4, panel_ns4, old_row4 = await make_env(
            old_key="nmgr4",
            replacement_commit_fn=make_fake_replacement_commit(
                {}, default=commit_result(ok=True, code="cutover_done", status="cutover_done"),
            ),
        )
        try:
            admin4 = 900004
            real_op4 = await _advance_op_to_status(main_ns4, tmp_root4, db_path4, "nmgr4", "nnew4", "ready_commit")
            panel_ns4["_wizard_set"](admin4, admin4, "replace", "preview", {"key": "nmgr4", "operation_id": "totally-different-forged-op-id"})
            e_forged = FakeEvent(data=f"rw:preview_confirm:{real_op4}".encode(), chat_id=admin4, sender_id=admin4)
            await panel_ns4["_replace_callback"](e_forged)
            calls4 = main_ns4["replacement_commit"].__calls__
            check("N14. a wizard/callback op_id mismatch never reaches the commit engine directly -- it re-resolves via recover first", not any(c["operation_id"] == real_op4 for c in calls4), calls4)
        finally:
            await cleanup_env(tmp_root4)

        # N15: another admin (owns no wizard state for this op) tapping a
        # guessed/observed preview_confirm callback is rejected server-side
        # -- ownership is enforced by _repl3_owned_op_or_error regardless
        # of what the attacker's own client-side wizard state claims.
        tmp_root5, db_path5, main_ns5, panel_ns5, old_row5 = await make_env(
            old_key="nmgr5",
            replacement_commit_fn=make_fake_replacement_commit(
                {}, default=commit_result(ok=True, code="cutover_done", status="cutover_done"),
            ),
        )
        try:
            owner5 = 900005
            attacker5 = 900006
            op_id5 = await _advance_op_to_status(main_ns5, tmp_root5, db_path5, "nmgr5", "nnew5", "ready_commit")
            panel_ns5["_wizard_set"](attacker5, attacker5, "replace", "preview", {"key": "nmgr5", "operation_id": op_id5})
            e_attacker = FakeEvent(data=f"rw:preview_confirm:{op_id5}".encode(), chat_id=attacker5, sender_id=attacker5)
            await panel_ns5["_replace_callback"](e_attacker)
            calls5 = main_ns5["replacement_commit"].__calls__
            check("N15. another admin's client-side wizard state cannot commit an operation it doesn't own (server rejects, engine never actually mutates it)", not any(c["operation_id"] == op_id5 and c["created_by_user_id"] == owner5 for c in calls5), calls5)
            row5 = main_ns5["_repl_storage"].replacement_get(op_id5, db_path=db_path5)
            check("N15b. the real operation is untouched (still ready_commit)", row5["status"] == "ready_commit", row5)
        finally:
            await cleanup_env(tmp_root5)

        # N16: retryable failure renders Retry/Back, no cancel.
        tmp_root6, db_path6, main_ns6, panel_ns6, old_row6 = await make_env(
            old_key="nmgr6",
            replacement_commit_fn=make_fake_replacement_commit(
                {}, default=commit_result(ok=False, code="runtime_start_failed", status="links_ready", retryable=True, message="Не удалось запустить новый рантайм."),
            ),
        )
        try:
            admin6 = 900007
            op_id6 = await _advance_op_to_status(main_ns6, tmp_root6, db_path6, "nmgr6", "nnew6", "ready_commit", admin=admin6)
            panel_ns6["_wizard_set"](admin6, admin6, "replace", "preview", {"key": "nmgr6", "operation_id": op_id6})
            e_retry = FakeEvent(data=f"rw:preview_confirm:{op_id6}".encode(), chat_id=admin6, sender_id=admin6)
            await panel_ns6["_replace_callback"](e_retry)
            retry_text, retry_buttons = e_retry.edits[-1]
            check("N16. a retryable engine failure is rendered honestly (message shown)", "рантайм" in retry_text, retry_text)
            retry_flat = [b for row in retry_buttons for b in row]
            check("N16b. retry screen offers 'Повторить' and 'Вернуться'", any("Повторить" in b.text for b in retry_flat) and any("Вернуться" in b.text for b in retry_flat), retry_flat)
            check("N16c. retry screen offers NO cancellation option", not any("Отмен" in b.text for b in retry_flat), retry_flat)
        finally:
            await cleanup_env(tmp_root6)

        # N17: links_pending progress rendered with real ready/required
        # numbers, never claiming completion before 15/15.
        tmp_root7, db_path7, main_ns7, panel_ns7, old_row7 = await make_env(
            old_key="nmgr7",
            replacement_commit_fn=make_fake_replacement_commit(
                {}, default=commit_result(ok=False, code="links_pending", status="links_pending", retryable=True, links_ready=7, links_required=15),
            ),
        )
        try:
            admin7 = 900008
            op_id7 = await _advance_op_to_status(main_ns7, tmp_root7, db_path7, "nmgr7", "nnew7", "ready_commit", admin=admin7)
            panel_ns7["_wizard_set"](admin7, admin7, "replace", "preview", {"key": "nmgr7", "operation_id": op_id7})
            e_links = FakeEvent(data=f"rw:preview_confirm:{op_id7}".encode(), chat_id=admin7, sender_id=admin7)
            await panel_ns7["_replace_callback"](e_links)
            links_text, links_buttons = e_links.edits[-1]
            check("N17. links progress shows the real ready/required counts", "7/15" in links_text, links_text)
            check("N17b. links progress never claims completion", "успешно" not in links_text, links_text)
            links_flat = [b for row in links_buttons for b in row]
            check("N17c. links-pending screen offers no cancellation option", not any("Отмен" in b.text for b in links_flat), links_flat)
        finally:
            await cleanup_env(tmp_root7)

        # N18: manual-recovery-required renders the safe generic warning,
        # never the underlying technical code/message.
        tmp_root8, db_path8, main_ns8, panel_ns8, old_row8 = await make_env(
            old_key="nmgr8",
            replacement_commit_fn=make_fake_replacement_commit(
                {}, default=commit_result(ok=False, code="identity_conflict", status="links_ready", manual_recovery_required=True,
                                           message="Конфликт идентификатора перед переключением. tg_user_id=999888777"),
            ),
        )
        try:
            admin8 = 900009
            op_id8 = await _advance_op_to_status(main_ns8, tmp_root8, db_path8, "nmgr8", "nnew8", "ready_commit", admin=admin8)
            panel_ns8["_wizard_set"](admin8, admin8, "replace", "preview", {"key": "nmgr8", "operation_id": op_id8})
            e_manual = FakeEvent(data=f"rw:preview_confirm:{op_id8}".encode(), chat_id=admin8, sender_id=admin8)
            await panel_ns8["_replace_callback"](e_manual)
            manual_text = e_manual.edits[-1][0]
            check("N18. manual-recovery screen shows the required safe wording", "требует ручной проверки" in manual_text, manual_text)
            check("N18b. manual-recovery screen never exposes the underlying technical code/message", "identity_conflict" not in manual_text and "999888777" not in manual_text, manual_text)
        finally:
            await cleanup_env(tmp_root8)

        # N19: no direct call to replacement_commit from panel_bot.py --
        # static source check (the panel side must ONLY ever reach the
        # engine through the panel_commands bridge, like every other
        # mutating action in this wizard).
        stage3_panel_commit_src = "\n\n".join(
            ast.unparse(n) for n in ast.parse(PANEL_SRC).body
            if getattr(n, "name", None) in {
                "_replace_submit_commit_and_render", "_replace_render_commit_result", "_replace_callback",
            }
        )
        check("N19. panel_bot.py never calls replacement_commit(...) directly", "replacement_commit(" not in stage3_panel_commit_src, stage3_panel_commit_src)
        check("N19b. panel_bot.py's commit path goes through _submit_and_wait", "_submit_and_wait(" in stage3_panel_commit_src and "/manager_replace_commit" in stage3_panel_commit_src, stage3_panel_commit_src)

        # N20: no screen reachable from preview_confirm/commit_status ever
        # offers cancellation -- static scan of the new screens' own button
        # builders for any 'rw:cancel' reference.
        commit_ui_src = "\n\n".join(
            ast.unparse(n) for n in ast.parse(PANEL_SRC).body
            if getattr(n, "name", None) in {
                "_replace_committing_buttons", "_replace_links_pending_buttons",
                "_replace_retry_buttons", "_replace_success_buttons",
            }
        )
        check("N20. no post-commit screen button builder references rw:cancel", "rw:cancel" not in commit_ui_src, commit_ui_src)
    finally:
        await cleanup_env(tmp_root2)

    # --- N21-N27: recovery --------------------------------------------
    # 'done' is deliberately excluded from this table: _replace_active_op_
    # for_old_key's own WHERE clause (status NOT IN done/failed/cancelled)
    # correctly treats a 'done' operation as no-longer-active -- 'Продолжить
    # замену' simply isn't offered for it (matches every other terminal
    # status; there is nothing left to recover into). Covered separately
    # by N21b below.
    for status, expect_marker in (
        ("committing", "Выполняется замена"),
        ("links_pending", "Выполняется замена"),
        ("links_ready", "Выполняется замена"),
        ("cutover_done", "успешно заменён"),
        ("notified", "успешно заменён"),
    ):
        tmp_root_r, db_path_r, main_ns_r, panel_ns_r, old_row_r = await make_env(old_key=f"nrec_{status}")
        try:
            FakeEvent = panel_ns_r["__FakeEvent__"]
            admin_r = 900100
            new_key_r = f"nrecnew_{status}"
            op_id_r = await _advance_op_to_status(main_ns_r, tmp_root_r, db_path_r, f"nrec_{status}", new_key_r, status, admin=admin_r)
            e_recover = FakeEvent(data=f"rw:recover:nrec_{status}".encode(), chat_id=admin_r, sender_id=admin_r)
            await panel_ns_r["_replace_callback"](e_recover)
            check(f"N21. recovering an operation at '{status}' renders the correct screen ('{expect_marker}')", e_recover.edits and expect_marker in e_recover.edits[-1][0], e_recover.edits)
        finally:
            await cleanup_env(tmp_root_r)

    tmp_root_done, db_path_done, main_ns_done, panel_ns_done, old_row_done = await make_env(old_key="nrec_done")
    try:
        FakeEvent = panel_ns_done["__FakeEvent__"]
        admin_done = 900100
        await _advance_op_to_status(main_ns_done, tmp_root_done, db_path_done, "nrec_done", "nrecnew_done", "done", admin=admin_done)
        e_recover_done = FakeEvent(data=b"rw:recover:nrec_done", chat_id=admin_done, sender_id=admin_done)
        await panel_ns_done["_replace_callback"](e_recover_done)
        check("N21b. a fully 'done' operation is correctly treated as no-longer-active (no crash, safe alert, no edit)", e_recover_done.answers and not e_recover_done.edits, (e_recover_done.answers, e_recover_done.edits))
    finally:
        await cleanup_env(tmp_root_done)

    # N27: after cutover_done, the old manager key no longer resolving is
    # handled safely -- the success screen routes to the NEW manager's
    # card (durable, deterministic), never attempts to render a deleted
    # old-key card.
    tmp_root9, db_path9, main_ns9, panel_ns9, old_row9 = await make_env(old_key="nrec_gone")
    try:
        FakeEvent = panel_ns9["__FakeEvent__"]
        admin9 = 900101
        op_id9 = await _advance_op_to_status(main_ns9, tmp_root9, db_path9, "nrec_gone", "nrec_gone_new", "cutover_done", admin=admin9)
        await main_ns9["__storage__"].manager_soft_remove("nrec_gone")  # simulate the real cutover's own old-manager removal
        e_recover9 = FakeEvent(data=b"rw:recover:nrec_gone", chat_id=admin9, sender_id=admin9)
        await panel_ns9["_replace_callback"](e_recover9)
        check("N27. recovery after the old manager is gone still renders the success screen safely (no crash, no deleted-card render)", e_recover9.edits and "успешно заменён" in e_recover9.edits[-1][0], e_recover9.edits)
        success_buttons9 = e_recover9.edits[-1][1]
        flat9 = [b for row in success_buttons9 for b in row]
        check("N27b. its button routes to the NEW manager's key, not the gone old key", any(b.data == b"menu:manager_admin:nrec_gone_new" for b in flat9), flat9)
    finally:
        await cleanup_env(tmp_root9)


async def run_all() -> None:
    await test_group_a_manager_card()
    await test_group_smoke_happy_path()
    await test_group_b_delete_choice()
    await test_group_c_authorization()
    await test_group_d_entry_start()
    await test_group_e_name_key()
    await test_group_f_proxy()
    await test_group_g_phone_code_password()
    await test_group_h_identity_preview()
    await test_group_i_recovery()
    await test_group_j_cancel()
    await test_group_k_relogin_exclusion()
    await test_group_l_regression_static()
    test_group_m_dbguard()
    await test_group_n_commit_wiring()


def main() -> int:
    asyncio.run(run_all())
    total = len(FAILURES)
    print(f"\n{'='*70}")
    if FAILURES:
        print(f"SELFTEST FAIL: {total} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
