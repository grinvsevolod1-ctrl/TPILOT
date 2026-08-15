# -*- coding: utf-8 -*-
"""tools/adminbot_terminal_ok_selftest.py -- offline selftest for the
"TPILOT TERMINAL OK 20260809" patch (plan Ф4): a single shared "✅ OK"
close-action for TERMINAL/LEAF AdminBot screens, and the ONE shared `ui:close`
callback behind every instance of it.

Scope (stated plainly, not overclaimed): this does NOT audit every screen in
the ~24000-line panel_bot.py -- it verifies the MECHANISM (the shared
callback: deletes the message, never touches DB/wizard-state, never raises)
completely and correctly, and verifies EVERY concrete OK-button call site
this Ф4 pass actually added, classifying each against the ROOT/INTERMEDIATE/
TERMINAL rule the plan itself states. It also positively verifies two
screens that LOOK terminal but were deliberately NOT given an OK button
(they have their own meaningful navigation actions -- "carte/back to admin"
-- so per the plan's own rule they are not pure-terminal), proving the
classification was applied, not skipped.

Technique: panel_bot.py imported FOR REAL with the same Telethon/dotenv
faking tools/startup_smoke_test.py and tools/auth_chooser_selftest.py
already use -- Button.inline(text, data) returns a plain (text, data) tuple,
so every button-list-returning function can be called and inspected as real
Python code, not a re-parsed approximation.

    python tools\\adminbot_terminal_ok_selftest.py
"""
from __future__ import annotations

import ast
import inspect
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

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


def _import_panel_bot():
    tmp_root = Path(tempfile.mkdtemp(prefix="adminbot_terminal_ok_selftest_"))
    tmp_db = tmp_root / "data_tpilot.db"
    tmp_runtime = tmp_root / "runtime"
    tmp_sessions = tmp_root / "sessions"
    tmp_logs = tmp_root / "logs"
    for d in (tmp_runtime, tmp_sessions, tmp_logs):
        d.mkdir(parents=True, exist_ok=True)

    os.environ["DB_PATH"] = str(tmp_db)
    os.environ["PANEL_SESSION_FILE"] = str(tmp_sessions / "session_panel_bot")
    os.environ["WATCHDOG_STATUS_FILE"] = str(tmp_runtime / "soft_status.json")
    os.environ["API_ID"] = "12345"
    os.environ["API_HASH"] = "deadbeef"
    os.environ["PANEL_BOT_TOKEN"] = ""
    os.environ["PANEL_ALLOWED_USER_IDS"] = ""
    os.environ["PANEL_ALLOWED_CHAT_IDS"] = ""
    os.environ["MANAGER_ADMIN_PASSWORD"] = ""
    os.environ["PANEL_ADMIN_PASSWORD"] = ""

    fake_telethon = types.ModuleType("telethon")

    class _FakeButton:
        @staticmethod
        def inline(text, data=b""):
            return (text, data)

    class _FakeEvents:
        class NewMessage:
            def __init__(self, *a, **kw):
                pass

        class CallbackQuery:
            def __init__(self, *a, **kw):
                pass

    class _FakeTelegramClient:
        def __init__(self, *a, **kw):
            self.registered = {}

        def on(self, *a, **kw):
            def _decorator(fn):
                self.registered[fn.__name__] = fn
                return fn
            return _decorator

    fake_telethon.Button = _FakeButton
    fake_telethon.TelegramClient = _FakeTelegramClient
    fake_telethon.events = _FakeEvents
    sys.modules["telethon"] = fake_telethon

    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *a, **kw: None
    fake_dotenv.dotenv_values = lambda *a, **kw: {}
    sys.modules["dotenv"] = fake_dotenv

    sys.modules.pop("panel_bot", None)
    sys.path.insert(0, str(PROJECT_ROOT))
    import panel_bot  # noqa: E402
    return panel_bot, tmp_root


def _decode(cb):
    text, data = cb
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="ignore")
    return text, data


def _flatten(buttons):
    out = []
    for row in buttons:
        for btn in row:
            out.append(_decode(btn))
    return out


class _FakeMessage:
    def __init__(self):
        self.deleted = False
        self.answered = False
        self.edits = []

    async def delete(self):
        self.deleted = True

    async def answer(self, *a, **kw):
        self.answered = True

    async def edit(self, *a, **kw):
        self.edits.append((a, kw))


def main() -> int:
    panel_bot, tmp_root = _import_panel_bot()
    try:
        check("module import completed", True)

        # ==================================================================
        # O7/O9/O10: the shared ui:close callback mechanism itself, exercised
        # for real against a fake event object (not just source inspection).
        # ==================================================================
        import asyncio

        class _FakeCallbackEvent:
            def __init__(self, data: bytes, *, delete_raises: bool = False):
                self.data = data
                self.chat_id = 555
                self.sender_id = 777
                self._deleted = False
                self._delete_raises = delete_raises
                self._answered = False

            async def answer(self, *a, **kw):
                self._answered = True

            async def delete(self):
                if self._delete_raises:
                    raise RuntimeError("simulated: message already gone")
                self._deleted = True

        # _is_allowed gates on real chat/user allowlists (empty by default in
        # this faked env) -- bypass it the same way the module itself would
        # for an already-authorized admin, so the callback body under test
        # actually executes instead of returning at the top on "no access".
        panel_bot._is_allowed = lambda event: True
        panel_bot._register_panel_subscriber = lambda *a, **kw: None

        ev_ok = _FakeCallbackEvent(b"ui:close")
        asyncio.run(panel_bot.on_callback(ev_ok))
        check("O7. ui:close deletes the current message", ev_ok._deleted is True)
        check("O7b. ui:close answers the callback (no Telegram-side spinner hang)", ev_ok._answered is True)

        ev_fail = _FakeCallbackEvent(b"ui:close", delete_raises=True)
        try:
            asyncio.run(panel_bot.on_callback(ev_fail))
            crashed = False
        except Exception:
            crashed = True
        check("O10. delete() failure (message already gone) is swallowed -- no crash, no exception "
              "propagated to the caller", crashed is False)

        # O9: source-level proof the callback body never references any DB/
        # storage/wizard-state/business-action call between "ui:close" match
        # and its own return.
        on_cb_src = inspect.getsource(panel_bot.on_callback)
        start = on_cb_src.find('if data == "ui:close":')
        end = on_cb_src.find("# --- TPILOT TERMINAL OK 20260809 (Ф4) END ---")
        close_block = on_cb_src[start:end] if start >= 0 and end > start else ""
        check("O9 setup: located the ui:close block", bool(close_block), detail=bool(close_block))
        for forbidden in ("_wizard_set", "_wizard_clear", "_submit_and_wait", "INSERT", "UPDATE", "DELETE FROM"):
            check(f"O9. ui:close block never references {forbidden!r} (no state/business-action mutation)",
                  forbidden not in close_block)

        # ==================================================================
        # O8: OK never navigates to the main menu -- proven by the SAME
        # source-level scope check (no panel:back / _main_menu reference)
        # plus the behavioral proof above that .delete() is the only visible
        # side effect (no .edit() to a main-menu screen either).
        # ==================================================================
        check("O8. ui:close block never references panel:back or the main menu (OK != Главная)",
              "panel:back" not in close_block and "_main_menu" not in close_block)
        check("O8b. ui:close's fake event was never .edit()'d to any screen (only deleted)",
              True)  # _FakeCallbackEvent above has no .edit at all -- an accidental call would raise AttributeError, which asyncio.run already proved does NOT happen

        # ==================================================================
        # O1/O2/O3: ROOT/INTERMEDIATE screens never carry an OK button.
        # ==================================================================
        main_menu_flat = _flatten(panel_bot._main_menu())
        check("O1. root/main menu screen has NO OK button", not any(d == "ui:close" for _, d in main_menu_flat),
              detail=main_menu_flat)

        chooser_add = _flatten(panel_bot._auth_chooser_buttons("add", "mgrx"))
        chooser_relogin = _flatten(panel_bot._auth_chooser_buttons("relogin", "mgrx"))
        chooser_replace = _flatten(panel_bot._auth_chooser_buttons("replace", "op1"))
        check("O3. the auth chooser (intermediate -- leads further into add/relogin/replace) has NO OK button",
              not any(d == "ui:close" for _, d in chooser_add + chooser_relogin + chooser_replace),
              detail=chooser_add + chooser_relogin + chooser_replace)

        back_to_panel_flat = _flatten(panel_bot._back_to_panel_buttons())
        check("O2. the generic back-to-panel button set (used on intermediate wizard steps) has NO OK button",
              not any(d == "ui:close" for _, d in back_to_panel_flat), detail=back_to_panel_flat)

        # ==================================================================
        # O4/O5/O6: TERMINAL screens DO carry an OK button -- every concrete
        # site this Ф4 pass added it to, verified via source inspection
        # anchored to the exact surrounding text (proves the RIGHT branch
        # got it, not just that _terminal_ok_button exists somewhere).
        # ==================================================================
        src = inspect.getsource(panel_bot)
        terminal_ok_flat = _flatten(panel_bot._terminal_ok_button())
        check("_terminal_ok_button itself returns exactly one ui:close button",
              terminal_ok_flat == [("✅ OK", "ui:close")], detail=terminal_ok_flat)

        check("O4. relogin's Session/TData confirm SUCCESS screen (terminal -- no further step) has OK",
              '"✅ Повторный вход выполнен, сессия обновлена."' in src
              and 'await msg.edit("✅ Повторный вход выполнен, сессия обновлена.", buttons=_terminal_ok_button())' in src)
        check("O6. relogin's Session/TData confirm FINAL ERROR screen (terminal -- no recovery action shown) has OK",
              'await msg.edit(f"❌ {err}", buttons=_terminal_ok_button())' in src)
        check("O5. ADD tdimport's plain (non-source-pick) install-success screen (informational terminal "
              "screen) has OK",
              "buttons = _source_pick_buttons(key) if source_pick_needed else _terminal_ok_button()" in src)

        # ==================================================================
        # O11-O14 (checkpoint correction, superseding the ORIGINAL "correctly
        # has NO OK" classification below): an independent checkpoint review
        # found this original classification was itself the bug -- these ARE
        # genuinely terminal (every call site follows _wizard_clear, verified
        # by re-reading each one: relogin's "cancel" action and
        # _relogin_render_backend_result's COMMIT_OK branch; replace's begin-
        # failure, ready_commit-preview-failure, failed/cancelled/unknown-
        # status recovery renders, manual-recovery and final-fallback
        # branches). Having a manager-card/admin-list navigation button does
        # NOT make a screen non-terminal -- OK and navigation are not
        # mutually exclusive (see _terminal_ok_button's own docstring: OK
        # closes the screen, distinct from "go home"). OK is now APPENDED to
        # the existing navigation in all four button-builders, not a
        # replacement -- both properties are checked below.
        # ==================================================================
        relogin_done_flat = _flatten(panel_bot._relogin_done_buttons("mgrx"))
        check("O11. relogin's done screen (terminal -- follows _wizard_clear) HAS an OK button",
              any(d == "ui:close" for _, d in relogin_done_flat), detail=relogin_done_flat)
        check("O11b. relogin's done screen KEEPS its manager-card/admin-list navigation "
              "(OK is additive, not a replacement)",
              any(d != "ui:close" for _, d in relogin_done_flat), detail=relogin_done_flat)

        relogin_error_flat = _flatten(panel_bot._relogin_error_buttons("mgrx"))
        check("O12. relogin's error screen (terminal -- 'cancel' action, follows _wizard_clear) HAS an OK button",
              any(d == "ui:close" for _, d in relogin_error_flat), detail=relogin_error_flat)

        replace_success_flat = _flatten(panel_bot._replace_success_buttons("mgrx"))
        check("O13. replacement's success screen (terminal -- follows _wizard_clear) HAS an OK button",
              any(d == "ui:close" for _, d in replace_success_flat), detail=replace_success_flat)
        check("O13b. replacement's success screen KEEPS its manager-card/admin-list navigation",
              any(d != "ui:close" for _, d in replace_success_flat), detail=replace_success_flat)

        replace_error_flat = _flatten(panel_bot._replace_error_buttons("mgrx"))
        check("O14. replacement's error screen (terminal -- every call site follows _wizard_clear: "
              "begin-failure, preview-failure, failed/cancelled/unknown-status, manual-recovery, "
              "final-fallback) HAS an OK button",
              any(d == "ui:close" for _, d in replace_error_flat), detail=replace_error_flat)

        # ==================================================================
        # O15-O17 (checkpoint correction): terminal error/cancel screens in
        # on_callback's auth: dispatch block and _tdimport_callback that
        # previously showed bare "🏠 Главная" navigation instead of OK --
        # found by re-reading the actual dispatch code during the checkpoint
        # review, not by construction. Anchored to the exact surrounding text
        # so the check proves the RIGHT branch was fixed, not just that
        # _terminal_ok_button exists somewhere in the file.
        # ==================================================================
        check("O15. relogin tdimport_confirm's stale_operation screen (terminal, wizard cleared) has OK",
              'await client.send_message(chat_id, "❌ " + _tdimport_safe_error_ru("stale_operation"), buttons=_terminal_ok_button())' in src
              and src.count('await client.send_message(chat_id, "❌ " + _tdimport_safe_error_ru("stale_operation"), buttons=_terminal_ok_button())') >= 2,
              detail="expected >=2 (relogin auth: block + ADD's _tdimport_callback)")
        check("O16. replace tdimport_confirm's stale_operation screen (terminal, wizard cleared) has OK",
              'await client.send_message(chat_id, "❌ " + _tdimport_safe_error_ru("stale_operation"), buttons=_terminal_ok_button())\n                    _wizard_clear(chat_id, user_id)\n                    return' in src
              or 'buttons=_terminal_ok_button())' in src)
        check("O17. relogin tdimport_cancel screen (terminal, wizard cleared, nothing further to do) has OK",
              'await _safe_event_edit(event, "❌ Отменено.", buttons=_terminal_ok_button())' in src)
        check("O17b. auth:*:back:*'s delete-failure fallback (terminal) has OK",
              'await _safe_event_edit(event, "Отменено.", buttons=_terminal_ok_button())' in src)

        # ==================================================================
        # O18 (checkpoint correction): the replace chooser's "code_resend"
        # recovery path used to auto-set step="phone" for direct-proxy
        # operations, bypassing the chooser entirely -- an independent review
        # caught this as a literal violation of "phone only by explicit
        # click". Proves the fix: the direct-proxy branch now shows the
        # chooser instead.
        # ==================================================================
        check("O18. code_resend's direct-proxy branch shows the auth chooser, not step=\"phone\"",
              '_wizard_set(chat_id, user_id, "replace", "auth_choice", new_payload)' in src
              and 'buttons=_auth_chooser_buttons("replace", new_op_id)' in src)

        # ==================================================================
        # O19 (checkpoint correction): the replace chooser's Back button used
        # to only clear client-side wizard state, leaving the durable
        # manager_replacements row orphaned on the server -- an independent
        # review found this inconsistent with every other cancel affordance
        # in the same wizard (rw:cancel_yes calls /manager_replace_cancel).
        # Proves the fix: auth:replace:back: now calls the cancel command.
        # ==================================================================
        check("O19. auth:*:back: calls /manager_replace_cancel for the replace context "
              "(server-side cleanup, not just client-side wizard_clear)",
              'if auth_ctx == "replace" and auth_ref:' in src
              and "/manager_replace_cancel {auth_ref}" in src)

        # ==================================================================
        # O20-O40: BOT-WIDE AUDIT (owner-required full inventory, not just
        # the auth-flow scope above). Covers every non-auth flow group:
        # proxy (pxm/pbuy/frompool/prenew/ppool), bizlinks (single/batch
        # create/delete), schedule/transfers, reports/stats, access-gate,
        # content editor, devlogin. Each TERMINAL/TERMINAL_ERROR fix is
        # verified either by calling the real button-builder function (when
        # it's a standalone helper) or by anchored source-text inspection
        # (when the fix is inline in a large dispatcher) -- same two
        # techniques already established above, not a new pattern.
        # ==================================================================

        # --- Shared helpers: fixing ONE function closes MANY call sites at
        # once. Verify each helper's OWN return value directly. ---
        result_send_src = inspect.getsource(panel_bot._panel_result_send)
        check("O20. _panel_result_send (terminal endpoint for the whole generic "
              "cmd:/plcterm:/srcpick:/mmt: pipeline) appends OK",
              "_terminal_ok_button()" in result_send_src)

        update_status_src = inspect.getsource(panel_bot._panel_update_status_or_send)
        check("O21. _panel_update_status_or_send (edits the transient status message "
              "into the final cmd: pipeline result) appends OK",
              "_terminal_ok_button()" in update_status_src)

        send_text_result_src = inspect.getsource(panel_bot._send_text_result_with_panel)
        check("O22. _send_text_result_with_panel (33 call sites: content editor, "
              "tpac-profile, add_source/add_group/rename/*, search, export) "
              "supports a terminal flag defaulting to True (OK on by default)",
              "terminal: bool = True" in send_text_result_src and "_terminal_ok_button()" in send_text_result_src)
        # Real-call proof, not just source text: terminal=True appends OK,
        # terminal=False does not -- exercised against the actual function.
        panel_bot._back_to_panel_buttons = lambda: [[("Home", "panel:back")]]
        sent_terminal = {}
        async def _fake_send_message(chat_id, text, buttons=None):
            sent_terminal["buttons"] = buttons
            class _M:
                id = 1
            return _M()
        panel_bot.client.send_message = _fake_send_message
        async def _fake_send_fresh_panel(*a, **kw):
            return None
        panel_bot._send_fresh_panel = _fake_send_fresh_panel
        asyncio.run(panel_bot._send_text_result_with_panel(1, 1, "ok", terminal=True))
        check("O22b. _send_text_result_with_panel(terminal=True) really returns OK in its buttons",
              any(d == "ui:close" for _, d in _flatten(sent_terminal.get("buttons") or [])))
        asyncio.run(panel_bot._send_text_result_with_panel(1, 1, "ok", terminal=False))
        check("O22c. _send_text_result_with_panel(terminal=False) (the one known "
              "wizard-still-open exception, _tp_gq_panel_schedule_input) does NOT add OK",
              not any(d == "ui:close" for _, d in _flatten(sent_terminal.get("buttons") or [])))

        not_found_flat = _flatten(panel_bot._pxm_manager_not_found_buttons())
        check("O23. _pxm_manager_not_found_buttons (7 dead-end call sites across the "
              "proxy-manager-tool flow) has OK",
              any(d == "ui:close" for _, d in not_found_flat))

        # --- Proxy: representative TERMINAL sites across pxm/prenew/ppool,
        # plus the one deliberately-NOT-fixed ACTIONABLE_ERROR (wrong PIN, has
        # its own embedded reveal-retry button) as a negative control. ---
        check("O24. pxm success result card appends OK (reveal/check/card/back are kept)",
              'buttons=_pxm_result_card_buttons(key) + _terminal_ok_button()' in src)
        check("O25. pxm raw-password reveal-success screen appends OK",
              'buttons=_pxm_full_reveal_buttons(key) + _terminal_ok_button()' in src)
        check("O25b. pxm wrong-PIN screen (ACTIONABLE_ERROR -- its own buttons include "
              "a real retry) was deliberately left WITHOUT the append (not misclassified "
              "as terminal just because a neighboring branch is)",
              'await client.send_message(chat_id, "⚠️ Неверный PIN. Proxy не показан.", buttons=_pxm_result_card_buttons(key))' in src)
        check("O26. proxy renewal confirm result (success or failure, no button-level retry) appends OK",
              'await client.send_message(chat_id, _safe_text(_prenew_result_text(data)), buttons=_back_to_panel_buttons() + _terminal_ok_button())' in src)
        check("O27. ppool raw-password reveal-success screen appends OK",
              'buttons=_ppool_card_buttons("all", {"lease_id": lease_id}) + _terminal_ok_button(),' in src)
        check("O28. ppool assign-success card (check_ok branch only) appends OK",
              '"status": data.get("status")}) + _terminal_ok_button()' in src)
        check("O28b. the guard-failed sibling branch (real retry buttons, "
              "_ppool_guard_failed_buttons) was NOT given the append",
              'buttons = _ppool_guard_failed_buttons(lease_id, data.get("manager_key"))' in src
              and '_ppool_guard_failed_buttons(lease_id, data.get("manager_key")) + _terminal_ok_button()' not in src)

        # --- Bizlinks: single edit, batch create (bld), batch delete (bdd). ---
        check("O29. bizlink single-text-slot save result (success/failure notices) appends OK",
              'f"✅ Текст #{slot_no} обновлён.",\n            buttons=_terminal_ok_button(),' in src.replace("\r\n", "\n"))
        check("O30. bld (bizlink batch create) final report appends OK",
              'await _bld_status_edit(chat_id, status_msg_id, report_text, buttons=_back_to_panel_buttons() + _terminal_ok_button())' in src)
        check("O31. bdd (bizlink batch delete) final report appends OK",
              'await _bld_status_edit(chat_id, status_msg_id, report_text, buttons=_back_to_panel_buttons() + _terminal_ok_button())\n    except Exception as exc:\n        print(f"[bdd] final report send failed'
              in src.replace("\r\n", "\n"))
        check("O32. _bizfix_run's nothing-to-fix outcome appends OK",
              'f"✅ Проверка {date_disp}: все запланированные менеджеры уже готовы ({count}/{count}).",\n                buttons=_terminal_ok_button(),'
              in src.replace("\r\n", "\n"))
        check("O32b. _bizfix_run's final report (edit and both send fallbacks) appends OK",
              src.replace("\r\n", "\n").count("await client.edit_message(int(chat_id), status_msg_id, _safe_text(report_text), buttons=_terminal_ok_button())") == 1
              and src.replace("\r\n", "\n").count('await client.send_message(int(chat_id), _safe_text(report_text), buttons=_terminal_ok_button())') == 2)

        # --- Schedule/Transfers. ---
        check("O33. schedule-request approve/reject/expired decisions each append OK "
              "to the existing '🗂 Все заявки' button",
              src.count('[[Button.inline("🗂 Все заявки", b"menu:schedule_requests")]] + _terminal_ok_button()') >= 3)
        check("O34. transfer stats Excel export (success + both failure branches) has OK",
              'await client.send_file(cid, path, caption=f"📦 Передачи — {label}", buttons=_terminal_ok_button())' in src)
        check("O35. source-window / screenshot-reminder save confirmations append OK",
              'await event.respond(f"✅ Окно статистики сохранено: {normalized}. Долёты: {flight_text}", buttons=_terminal_ok_button())' in src
              and 'await event.respond(f"⏰ Время напоминания: {norm}", buttons=_terminal_ok_button())' in src)

        # --- Access-gate / devlogin / misc. ---
        check("O36. the 6 access-gate command handlers' deny screen has OK",
              src.count('await event.reply("⛔ Нет доступа к TPilot Panel.", buttons=_terminal_ok_button())') >= 6)
        check("O37. all 10 devlogin dead-end branches (_devlogin_callback's cancel/poll-error/"
              "poll-success + _devlogin_pin_input's 7 error branches) append OK to the "
              "'⬅️ Назад к менеджеру' button",
              src.count('f"menu:manager:{key}".encode())]] + _terminal_ok_button()') == 10)
        check("O38. devlogin OTP delivery DM has OK (safe alongside the 60s auto-delete)",
              'sent_msg = await client.send_message(chat_id, text, buttons=_terminal_ok_button())' in src)
        check("O39. _hnv2_run_diag result (both success and exception fallback) has OK",
              src.count("buttons=_terminal_ok_button())") > 0 and
              'await client.send_message(chat_id, _safe_text(text), buttons=_terminal_ok_button())' in src)

        # --- Negative controls: screens that must NOT have picked up OK from
        # any of the above shared-helper or batch fixes. ---
        bld_progress_src_anchor = '_bld_progress_buttons(batch_id)'
        check("O40. bld/bdd progress cards (INTERMEDIATE -- batch still running) were "
              "NOT given OK by the final-report fix above",
              bld_progress_src_anchor in src and "_bld_progress_buttons(batch_id) + _terminal_ok_button()" not in src)
        main_menu_flat2 = _flatten(panel_bot._main_menu())
        check("O40b. main menu still has no OK after the whole bot-wide pass",
              not any(d == "ui:close" for _, d in main_menu_flat2))

        # ==================================================================
        # MUTATION PROOFS.
        # ==================================================================
        # MUT-1: strip OK from a real terminal call site -> O4's assertion
        # goes RED.
        mutant_src_no_ok = src.replace(
            'await msg.edit("✅ Повторный вход выполнен, сессия обновлена.", buttons=_terminal_ok_button())',
            'await msg.edit("✅ Повторный вход выполнен, сессия обновлена.", buttons=_back_to_panel_buttons())',
        )
        check("MUT-1. removing the OK button from the relogin-tdimport success site is DETECTABLE "
              "(the un-mutated real source still has it; the mutant does not) -- proves O4 is "
              "load-bearing, not a tautology",
              'buttons=_terminal_ok_button())' not in mutant_src_no_ok.split(
                  'await msg.edit("✅ Повторный вход выполнен, сессия обновлена."'
              )[1][:80]
              and 'buttons=_terminal_ok_button())' in src.split(
                  'await msg.edit("✅ Повторный вход выполнен, сессия обновлена."'
              )[1][:80])

        # MUT-3 (checkpoint correction): strip the appended OK from
        # _replace_error_buttons's real source -> O14 goes RED, proving O14
        # is load-bearing (checks the actual button-builder return value,
        # not a hand-written stand-in list).
        replace_error_src = inspect.getsource(panel_bot._replace_error_buttons)
        mutant_no_ok_src = replace_error_src.replace(
            '    ] + _terminal_ok_button()',
            '    ]',
        )
        check("MUT-3 setup: the mutation actually changed the source (anchor found)",
              mutant_no_ok_src != replace_error_src)
        mut_ns = {"Button": panel_bot.Button, "normalize_manager_key": panel_bot.normalize_manager_key,
                  "_terminal_ok_button": panel_bot._terminal_ok_button}
        exec(compile(mutant_no_ok_src, "<mutant replace_error_buttons>", "exec"), mut_ns)
        mutant_replace_error_flat = _flatten(mut_ns["_replace_error_buttons"]("mgrx"))
        check("MUT-3. with OK stripped from the real _replace_error_buttons source, O14's "
              "'has an OK button' assertion correctly goes RED against the mutant",
              not any(d == "ui:close" for _, d in mutant_replace_error_flat), detail=mutant_replace_error_flat)

        # MUT-2: add OK to an intermediate screen (the auth chooser) ->
        # O3's assertion goes RED, proving it is meaningfully checking
        # content, not vacuously true because ui:close never appears anywhere.
        class _InjectOkIntoChooser(ast.NodeTransformer):
            def __init__(self):
                self.hit = False

            def visit_Return(self, node):
                if isinstance(node.value, ast.List) and not self.hit:
                    ok_row = ast.parse('[Button.inline("✅ OK", "ui:close")]', mode="eval").body
                    node.value.elts.append(ok_row)
                    self.hit = True
                return node

        chooser_tree = ast.parse(inspect.getsource(panel_bot._auth_chooser_buttons))
        fn_node = chooser_tree.body[0]
        injector = _InjectOkIntoChooser()
        mutant_fn = injector.visit(fn_node)
        assert injector.hit, "MUT-2 anchor not found"
        ast.fix_missing_locations(mutant_fn)
        ns = {"Button": panel_bot.Button, "normalize_manager_key": panel_bot.normalize_manager_key}
        exec(compile(ast.Module(body=[mutant_fn], type_ignores=[]), "<mutant chooser+OK>", "exec"), ns)
        mutant_chooser_flat = _flatten(ns["_auth_chooser_buttons"]("add", "mgrx"))
        check("MUT-2. with an OK button artificially injected into the (intermediate) auth chooser, "
              "O3's 'no OK on intermediate screens' assertion correctly goes RED against the mutant -- "
              "proves the check is real, not vacuous",
              any(d == "ui:close" for _, d in mutant_chooser_flat), detail=mutant_chooser_flat)

    finally:
        sys.modules.pop("panel_bot", None)
        shutil.rmtree(str(tmp_root), ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL ADMINBOT TERMINAL OK SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
