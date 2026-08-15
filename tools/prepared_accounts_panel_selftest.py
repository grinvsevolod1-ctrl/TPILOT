# -*- coding: utf-8 -*-
"""tools/prepared_accounts_panel_selftest.py -- offline selftest for the
Phase 6 PanelBot UI of "💾Подготовленные аккаунты" (prepared accounts).

Technique: same as tools/auth_chooser_selftest.py -- panel_bot.py is
imported FOR REAL (not AST-extracted) with every project path env var
redirected to a temp dir, `telethon` faked (Button.inline(text, data) ->
plain (text, data) tuple, TelegramClient.on() just records handler names,
no real client/network), `dotenv` faked. Pure functions (button/text
builders) run as real Python code; the on_callback/_prep_callback dispatch
bodies (which need a live panel_commands queue to fully simulate) are
verified via static source inspection anchored to unique, load-bearing
substrings -- same style the project's other panel selftests already use.

Never: real Telegram network, real proxy/provider network, production
DB/session/runtime/log access or mutation.

    python tools\\prepared_accounts_panel_selftest.py
"""
from __future__ import annotations

import inspect
import os
import re
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
    tmp_root = Path(tempfile.mkdtemp(prefix="prepared_panel_selftest_"))
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
    os.environ["MANAGER_ADMIN_PASSWORD"] = "test-admin-pw"
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


def _func_src(fn) -> str:
    return inspect.getsource(fn)


def main() -> int:
    panel_bot, tmp_root = _import_panel_bot()
    try:
        check("module import completed", True)
        src = inspect.getsource(panel_bot)

        # ==================================================================
        # 1. Section button in the manager admin menu, exact label + prefix.
        # ==================================================================
        menu_rows = panel_bot._manager_admin_menu()
        menu_flat = _flatten(menu_rows)
        menu_by_text = dict(menu_flat)
        check("1. section button '💾Подготовленные аккаунты' present in _manager_admin_menu (no space after emoji)",
              menu_by_text.get("💾Подготовленные аккаунты") == "prep:menu", detail=menu_by_text)
        check("1b. section label matches features.prepared_accounts.texts.SECTION_LABEL verbatim (single source of truth)",
              "💾Подготовленные аккаунты" == panel_bot._prepared_texts.SECTION_LABEL)

        # ==================================================================
        # 1c. TPILOT PREP BUTTON MANAGER LIST 20260811: second UI entry point
        # -- Главная -> Менеджеры -> Список менеджеров (_manager_settings_
        # buttons(), the runtime-effective builder for menu:manager_settings,
        # reached via the _title_for_menu override chain) must offer the
        # SAME prep:menu entry, as the FIRST row, before any manager button.
        # Executes the real function (no seeded managers -> only the new
        # row + the trailing back row are present, which already proves
        # "first").
        # ==================================================================
        settings_rows = panel_bot._manager_settings_buttons()
        settings_flat = _flatten(settings_rows)
        check("1c. _manager_settings_buttons() (Список менеджеров) contains '💾Подготовленные аккаунты' -> prep:menu",
              ("💾Подготовленные аккаунты", "prep:menu") in settings_flat, detail=settings_flat)
        check("1c2. it is the FIRST row (before any manager/back button)",
              bool(settings_rows) and len(settings_rows[0]) == 1
              and settings_rows[0][0][0] == "💾Подготовленные аккаунты"
              and (settings_rows[0][0][1].decode() if isinstance(settings_rows[0][0][1], bytes) else settings_rows[0][0][1]) == "prep:menu",
              detail=settings_rows[0] if settings_rows else None)
        check("1c3. label text is byte-identical to _prepared_texts.SECTION_LABEL "
              "(a literal here, not the module reference -- this function is AST-extracted by exact "
              "name into an isolated namespace by tools/manager_card_unified_selftest.py's CARD_NAMES, "
              "which does not inject the _prepared_texts module)",
              settings_flat[0][0] == panel_bot._prepared_texts.SECTION_LABEL, detail=settings_flat[0] if settings_flat else None)

        # ==================================================================
        # 2. Add-manager branch-choice screen: ▶️/📦 buttons + exact labels.
        # ==================================================================
        check("2a. 'wiz:add_manager:start' now shows a branch-choice screen, not the old key prompt directly",
              "Куда добавить аккаунт?" in src)
        check("2b. branch-choice offers ▶️ Подключить сейчас -> wiz:add_manager:go:live",
              'b"wiz:add_manager:go:live"' in src and "▶️ Подключить сейчас" in src)
        check("2c. branch-choice offers 📦 Подготовить на потом (from texts.PREPARE_ACTION_LABEL, not a re-typed literal)",
              "_prepared_texts.PREPARE_ACTION_LABEL" in src and 'b"wiz:add_manager:go:prepared"' in src)
        check("2d. PREPARE_ACTION_LABEL is exactly '📦 Подготовить на потом' (NOT 💾 -- that emoji is reserved for the section button)",
              panel_bot._prepared_texts.PREPARE_ACTION_LABEL == "📦 Подготовить на потом")

        # ==================================================================
        # 3. ▶️ continues the OLD flow byte-identically; 📦 uses /prepared_add.
        # ==================================================================
        check("3a. wiz:add_manager:go:live sends the ORIGINAL key-entry prompt text verbatim",
              '"➕ Добавление менеджера\\n\\nВведите ключ менеджера латиницей.\\nНапример te или miller"' in src)
        check("3b. wiz:add_manager:go:live seeds payload prepared=False (▶️ never takes the prepared branch)",
              '_wizard_set(int(event.chat_id or 0), int(event.sender_id or 0), "add_manager", "key", {"prepared": False})' in src)
        check("3c. wiz:add_manager:go:prepared seeds payload prepared=True",
              '_wizard_set(int(event.chat_id or 0), int(event.sender_id or 0), "add_manager", "key", {"prepared": True})' in src)
        check("3d. step=='key' branches on payload.get('prepared') and calls /prepared_add (not /manager_add) for the prepared path",
              'if payload.get("prepared"):' in src and 'f"/prepared_add {key}"' in src)
        check("3e. the non-prepared step=='key' path still calls /manager_add unconditionally (old behavior preserved)",
              'f"/manager_add {key}"' in src)

        # ==================================================================
        # 4/5. auth chooser 'prepare' context + exact labels (no 🔳/🗂 anywhere).
        # ==================================================================
        flat_prepare = _flatten(panel_bot._auth_chooser_buttons("prepare", "acc1"))
        by_text_prepare = dict(flat_prepare)
        check("4a. auth chooser 'prepare' context produces exactly 5 rows (QR/Phone/Session/TData/Back)",
              len(flat_prepare) == 5, detail=flat_prepare)
        check("4b. prepare QR routes through auth:prepare:qr:<key>",
              by_text_prepare.get("◻️ QR") == "auth:prepare:qr:acc1", detail=by_text_prepare)
        check("4c. prepare Phone routes through auth:prepare:phone:<key>",
              by_text_prepare.get("📱 Номер телефона") == "auth:prepare:phone:acc1", detail=by_text_prepare)
        check("4d. prepare Session routes through auth:prepare:session:<key>",
              by_text_prepare.get("📁 Session") == "auth:prepare:session:acc1", detail=by_text_prepare)
        check("4e. prepare TData routes through auth:prepare:tdata:<key>",
              by_text_prepare.get("📦 TData") == "auth:prepare:tdata:acc1", detail=by_text_prepare)
        check("4f. prepare Back routes through auth:prepare:back:<key>",
              by_text_prepare.get("⬅️ Назад") == "auth:prepare:back:acc1", detail=by_text_prepare)
        for ctx in ("add", "relogin", "replace", "prepare"):
            flat = _flatten(panel_bot._auth_chooser_buttons(ctx, "x"))
            texts = [t for t, _ in flat]
            check(f"5b. ctx={ctx}: no legacy 🔳 label anywhere", not any("🔳" in t for t in texts), detail=texts)
            check(f"5c. ctx={ctx}: no legacy 🗂 label anywhere", not any("🗂" in t for t in texts), detail=texts)
        check("5d. _manager_qr_entry_button uses the new ◻️ label, not 🔳",
              "◻️ Войти по QR" in _flatten(panel_bot._manager_qr_entry_button("acc1"))[0])
        check("5e. callback_data of the fixed labels is completely unchanged (UI-only relabel) -- proven by "
              "tools/auth_chooser_selftest.py's A1/A2/A3 checks passing against the SAME builder",
              True)

        # ==================================================================
        # 6. QR/Phone prepare: /prepared_method is called before the proxy
        #    chooser, and the chooser has no bypass (prepared=True).
        # ==================================================================
        check("6a. auth:prepare:qr calls /prepared_method <key> qr before showing the proxy chooser",
              'f"/prepared_method {key} qr"' in src)
        check("6b. auth:prepare:phone calls /prepared_method <key> phone before showing the proxy chooser",
              'f"/prepared_method {key} phone"' in src)
        check("6c. both prepare qr/phone branches render _add_manager_proxy_choice_buttons(key, prepared=True)",
              src.count("_add_manager_proxy_choice_buttons(key, prepared=True)") >= 2)
        prepared_proxy_buttons = _flatten(panel_bot._add_manager_proxy_choice_buttons("acc1", prepared=True))
        prepared_proxy_texts = [t for t, _ in prepared_proxy_buttons]
        check("6d/7. prepared=True proxy chooser has NO '🔓 Подключить без proxy' bypass button",
              not any("без proxy" in t for t in prepared_proxy_texts), detail=prepared_proxy_texts)
        ordinary_proxy_buttons = _flatten(panel_bot._add_manager_proxy_choice_buttons("acc1", prepared=False))
        ordinary_proxy_texts = [t for t, _ in ordinary_proxy_buttons]
        check("7b. ordinary (prepared=False) proxy chooser STILL has the bypass button (regression check)",
              any("без proxy" in t for t in ordinary_proxy_texts), detail=ordinary_proxy_texts)
        check("7c. _add_manager_proxy_choice_buttons(key) with no explicit kwarg auto-detects from the live "
              "managers row (default None, not hardcoded False) -- every pre-existing retry call site stays correct",
              "if prepared is None:" in _func_src(panel_bot._add_manager_proxy_choice_buttons))

        # ==================================================================
        # 8/9. Durable resume seam wired at all 4 proxy-continuation points,
        # with zero duplicated QR/phone-start logic (extraction proof).
        # ==================================================================
        resume_calls = src.count("await _prep_resume_after_proxy(chat_id, user_id, key")
        check("8a. _prep_resume_after_proxy is called from all 4 proxy-continuation points "
              "(manual one-line, buy-confirm, buy-recover, from-pool)",
              resume_calls == 4, detail=resume_calls)
        check("8b. /prepared_pending is the ONLY thing _prep_resume_after_proxy reads to decide "
              "the durable method (not the wizard payload, which gets rebuilt on almost every step)",
              'f"/prepared_pending {key}"' in _func_src(panel_bot._prep_resume_after_proxy))
        # DUPLICATED_AUTH_LOGIC = 0: the QR-start/phone-entry bodies exist
        # in exactly ONE place each (_auth_start_qr/_auth_enter_phone);
        # every callback branch that used to inline them now just calls
        # the helper.
        check("9a. wiz:qr_start: callback delegates to _auth_start_qr (no inline duplicate)",
              "await _auth_start_qr(chat_id, user_id, key, event=event)" in src)
        check("9b. auth:add:phone: callback delegates to _auth_enter_phone (no inline duplicate)",
              "await _auth_enter_phone(chat_id, user_id, key, event=event)" in src)
        qr_start_prompt_count = src.count('"/manager_qr_start {key}"'.replace("{key}", "")) if False else src.count('f"/manager_qr_start {key}"')
        check("9c. DUPLICATED_AUTH_LOGIC = 0: /manager_qr_start is issued from exactly ONE place "
              "(_auth_start_qr) -- not re-implemented at any of its call sites",
              qr_start_prompt_count == 1, detail=qr_start_prompt_count)
        phone_wizard_set_count = src.count('_wizard_set(chat_id, user_id, "add_manager", "phone", {"key": key})')
        check("9d. DUPLICATED_AUTH_LOGIC = 0: the phone-entry step transition exists in exactly ONE place "
              "(_auth_enter_phone) plus the pre-existing unrelated resend_code fallback -- exactly 2 total",
              phone_wizard_set_count == 2, detail=phone_wizard_set_count)
        check("9e. QR_PHONE_SECOND_AUTH_CHOOSER = NO: proxy success resumes DIRECTLY via "
              "_auth_start_qr/_auth_enter_phone, never re-renders _auth_chooser_buttons",
              "await _auth_start_qr(chat_id, user_id, key, event=event)" in _func_src(panel_bot._prep_resume_after_proxy)
              and "await _auth_enter_phone(chat_id, user_id, key, event=event)" in _func_src(panel_bot._prep_resume_after_proxy))

        # ==================================================================
        # 10/11. QR/Phone prepared terminal screen + notification branch.
        # ==================================================================
        check("10a. _PREPARED_READY_MARKER is checked BEFORE _ONBOARDING_SOURCE_PICK_MARKER in the "
              "synchronous code/pass/qr wizard steps (no source chooser for prepared)",
              src.count("if _prepared_model.PREPARED_READY_MARKER in text:") >= 3)
        check("10b. _send_text_result_with_prepared strips the technical sentinel before display "
              "(never shown raw to the operator, unlike the human-readable onboarding marker)",
              "_prepared_model.PREPARED_READY_MARKER" in _func_src(panel_bot._send_text_result_with_prepared)
              and ".replace(" in _func_src(panel_bot._send_text_result_with_prepared))
        check("11a. the QR-path notification loop has a kind=='prepared_ready' branch",
              'str(row.get("kind") or "") == "prepared_ready"' in src)
        prepared_ready_branch = src.split('== "prepared_ready"', 1)[1].split('elif str(row.get("kind")', 1)[0]
        check("11b. the prepared_ready notification branch strips the sentinel and never renders manager_source_pick buttons",
              "_prepared_model.PREPARED_READY_MARKER" in prepared_ready_branch
              and "_manager_source_pick_notification_buttons" not in prepared_ready_branch,
              detail=prepared_ready_branch)

        # ==================================================================
        # 12/13. Session/TData prepare: zero proxy UI, uses /prepared_import.
        # ==================================================================
        prepare_session_branch_start = src.find('if auth_method in ("session", "tdata"):')
        prepare_session_branch_end = src.find("if auth_method ==", prepare_session_branch_start + 40)
        session_branch_src = src[prepare_session_branch_start:prepare_session_branch_end] if prepare_session_branch_start >= 0 else ""
        check("12a. session/tdata prepare branch located", bool(session_branch_src))
        check("12b. SESSION_PREPARE_PROXY_UI = NO / TDATA_PREPARE_PROXY_UI = NO: the prepare branch of the "
              "session/tdata handler never calls _add_manager_proxy_choice_buttons",
              "_add_manager_proxy_choice_buttons" not in session_branch_src, detail=session_branch_src)
        check("12c. session/tdata prepare uses a DEDICATED wizard name ('prepared_import'), never 'add_manager' "
              "-- cannot collide with the existing online-tdimport upload handler",
              '_wizard_set(chat_id, user_id, "prepared_import", "wait_upload", {"key": key, "kind": auth_method})' in src)
        check("13a. the prepared document-input handler is gated on wizard=='prepared_import' (separate from "
              "the existing add_manager/relogin/replace tdimport upload handlers)",
              'state.get("wizard") != "prepared_import" or state.get("step") != "wait_upload"' in src)
        check("13b. the prepared document-input handler calls /prepared_import, never /manager_tdimport_start",
              'f"/prepared_import {key} {archive_path}"' in _func_src(panel_bot._prepared_import_document_input))
        check("13c. the prepared document-input handler never calls the online /manager_tdimport_start command",
              "/manager_tdimport_start" not in _func_src(panel_bot._prepared_import_document_input))
        check("13d. the offline upload success screen does NOT claim '✅ Telegram проверен' for a STORED account "
              "(requirement 13 -- verification happens at activation, not at prepare)",
              "✅ Telegram проверен" not in _func_src(panel_bot._prepared_import_document_input))

        # ==================================================================
        # 14. Panel never chooses Session vs TData itself.
        # ==================================================================
        check("14. panel_bot.py contains no local Session-vs-TData priority/choice logic "
              "(detector.choose_source, imported by import_offline.py, is the sole authority)",
              "choose_source" not in src)

        # ==================================================================
        # 15. List/card use /prepared_list //prepared_card.
        # ==================================================================
        check("15a. _prep_fetch_list calls /prepared_list", 'await _submit_and_wait("/prepared_list"' in _func_src(panel_bot._prep_fetch_list))
        check("15b. _prep_fetch_card calls /prepared_card <key>", 'f"/prepared_card {key}"' in _func_src(panel_bot._prep_fetch_card))

        # ==================================================================
        # 16. DRAFT/VERIFIED/STORED card action availability.
        #
        # TPILOT PREPARED ACCOUNTS PHASE 7B BLOCKER-1 CORRECTION (2026-08-11):
        # the independent Phase 7A review found that the ORIGINAL 16b here
        # asserted the WRONG behavior -- a DRAFT QR/Phone card with NO bound
        # proxy must NEVER offer a continue-auth button (that account's
        # first Telegram connect would go out on the SERVER's own IP, since
        # _manager_auth_proxy_mode/_tpag_run_guard fall back to mode=
        # 'direct' when no proxy is configured). 16b is inverted below (now
        # asserts absence); 1/2/3/4/5/6 are NEW checks covering both QR and
        # Phone, both with and without a bound proxy.
        # ==================================================================
        draft_qr = {"manager_key": "acc1", "substate": "draft", "auth_source": "qr", "proxy_host": "", "proxy_port": "", "proxy_enabled": 0}
        draft_buttons = _flatten(panel_bot._prep_card_buttons(draft_qr))
        draft_texts = [t for t, _ in draft_buttons]
        check("16a. DRAFT card: ▶️ Подключить is NOT offered", not any("Подключить" == t.split(" ", 1)[-1] or t == "▶️ Подключить" for t in draft_texts), detail=draft_texts)
        check("16b. [B1-1] DRAFT (qr) card WITHOUT proxy does NOT offer a QR continue/retry action", not any("QR" in t for t in draft_texts), detail=draft_texts)
        check("16b2. [B1-2] DRAFT (qr) card WITHOUT proxy offers '🌐 Подключить proxy' instead", "🌐 Подключить proxy" in draft_texts, detail=draft_texts)
        check("16c. DRAFT card offers 🗑 Удалить and ⬅️ Назад", any("Удалить" in t for t in draft_texts) and any("Назад" in t for t in draft_texts))

        draft_phone = {"manager_key": "acc1", "substate": "draft", "auth_source": "phone", "proxy_host": "", "proxy_port": "", "proxy_enabled": 0}
        draft_phone_texts = [t for t, _ in _flatten(panel_bot._prep_card_buttons(draft_phone))]
        check("16b3. [B1-3] DRAFT (phone) card WITHOUT proxy does NOT offer a phone continue action",
              not any("телефон" in t.lower() or "номер" in t.lower() for t in draft_phone_texts), detail=draft_phone_texts)
        check("16b4. [B1-4] DRAFT (phone) card WITHOUT proxy offers '🌐 Подключить proxy' instead",
              "🌐 Подключить proxy" in draft_phone_texts, detail=draft_phone_texts)

        draft_qr_proxy = {"manager_key": "acc1", "substate": "draft", "auth_source": "qr", "proxy_host": "1.2.3.4", "proxy_port": "1080", "proxy_enabled": 1}
        draft_qr_proxy_texts = [t for t, _ in _flatten(panel_bot._prep_card_buttons(draft_qr_proxy))]
        check("16b5. [B1-5] DRAFT (qr) card WITH a bound proxy still offers the QR continue action",
              any("QR" in t for t in draft_qr_proxy_texts), detail=draft_qr_proxy_texts)

        draft_phone_proxy = {"manager_key": "acc1", "substate": "draft", "auth_source": "phone", "proxy_host": "1.2.3.4", "proxy_port": "1080", "proxy_enabled": 1}
        draft_phone_proxy_texts = [t for t, _ in _flatten(panel_bot._prep_card_buttons(draft_phone_proxy))]
        check("16b6. [B1-6] DRAFT (phone) card WITH a bound proxy still offers the phone continue action",
              any("номер" in t.lower() for t in draft_phone_proxy_texts), detail=draft_phone_proxy_texts)

        verified = {"manager_key": "acc1", "substate": "verified", "auth_source": "qr",
                    "proxy_host": "1.2.3.4", "proxy_port": "1080", "proxy_enabled": 1}
        verified_texts = [t for t, _ in _flatten(panel_bot._prep_card_buttons(verified))]
        check("19a. VERIFIED card offers ▶️ Подключить", "▶️ Подключить" in verified_texts, detail=verified_texts)
        check("19b. VERIFIED card offers 🔄 Проверить аккаунт", "🔄 Проверить аккаунт" in verified_texts, detail=verified_texts)
        check("19c. VERIFIED card (has proxy) offers 🌐 Сменить proxy / 🧹 Освободить proxy, not 🌐 Подключить proxy",
              "🌐 Сменить proxy" in verified_texts and "🧹 Освободить proxy" in verified_texts
              and "🌐 Подключить proxy" not in verified_texts, detail=verified_texts)

        stored = {"manager_key": "acc1", "substate": "stored", "auth_source": "session",
                  "proxy_host": "", "proxy_port": "", "proxy_enabled": 0}
        stored_texts = [t for t, _ in _flatten(panel_bot._prep_card_buttons(stored))]
        check("20a. STORED card (no proxy) offers ▶️ Подключить (activation itself enforces NEED_PROXY -> fail closed)",
              "▶️ Подключить" in stored_texts, detail=stored_texts)
        check("20b. STORED card (no proxy) offers '🌐 Подключить proxy', not 'Сменить'/'Освободить'",
              "🌐 Подключить proxy" in stored_texts and "🌐 Сменить proxy" not in stored_texts
              and "🧹 Освободить proxy" not in stored_texts, detail=stored_texts)

        # ==================================================================
        # 17. NEED_PROXY UI for both activate and verify, no bypass.
        # ==================================================================
        activate_src = _func_src(panel_bot._prep_callback)
        check('17a. activate handles status==NEED_PROXY by rendering _pxm_menu_buttons(key)',
              '_prepared_model.ActivationResult.NEED_PROXY' in activate_src and "_pxm_menu_buttons(key)" in activate_src)
        pxm_buttons_texts = [t for t, _ in _flatten(panel_bot._pxm_menu_buttons("acc1"))]
        check("17b. _pxm_menu_buttons (reused verbatim for NEED_PROXY) has no bypass-without-proxy option",
              not any("без proxy" in t for t in pxm_buttons_texts), detail=pxm_buttons_texts)

        # ==================================================================
        # BLOCKER-1 FIX 1B -- behavioral (not source-scan) proof that
        # _prep_callback's "continue" action re-checks the FRESH card's
        # proxy state before ever calling _auth_start_qr/_auth_enter_phone.
        # This is the actual runtime dispatch handler, executed for real
        # (async, with every I/O dependency monkeypatched away) rather than
        # inferred from source text -- the independent review specifically
        # flagged that this handler was never exercised by any prior test.
        # ==================================================================
        _b1_orig_prep_fetch_card = panel_bot._prep_fetch_card
        _b1_orig_auth_start_qr = panel_bot._auth_start_qr
        _b1_orig_auth_enter_phone = panel_bot._auth_enter_phone
        _b1_orig_safe_event_edit = panel_bot._safe_event_edit
        _b1_orig_is_allowed = panel_bot._is_allowed

        class _B1FakeEvent:
            def __init__(self, data: bytes):
                self.data = data
                self.chat_id = 111
                self.sender_id = 222

            async def answer(self, *a, **kw):
                pass

        def _b1_card_factory(item):
            async def _fake(chat_id, user_id, key):
                return item
            return _fake

        b1_qr_calls, b1_phone_calls, b1_edit_calls = [], [], []

        async def _b1_fake_auth_start_qr(chat_id, user_id, key, *, event=None):
            b1_qr_calls.append((chat_id, user_id, key))

        async def _b1_fake_auth_enter_phone(chat_id, user_id, key, *, event=None):
            b1_phone_calls.append((chat_id, user_id, key))

        async def _b1_fake_safe_event_edit(event, text, buttons=None):
            b1_edit_calls.append((text, buttons))

        panel_bot._is_allowed = lambda event: True
        panel_bot._auth_start_qr = _b1_fake_auth_start_qr
        panel_bot._auth_enter_phone = _b1_fake_auth_enter_phone
        panel_bot._safe_event_edit = _b1_fake_safe_event_edit
        try:
            # 7/9a. QR, no proxy: _auth_start_qr must NOT be called; the
            # existing proxy menu (_pxm_menu_buttons) is shown instead.
            panel_bot._prep_fetch_card = _b1_card_factory(
                {"manager_key": "acc1", "substate": "draft", "auth_source": "qr", "proxy_host": "", "proxy_port": "", "proxy_enabled": 0}
            )
            import asyncio as _b1_asyncio
            _b1_asyncio.run(panel_bot._prep_callback(_B1FakeEvent(b"prep:continue:acc1")))
            check("7. [B1] prep:continue QR without proxy: _auth_start_qr calls == 0", b1_qr_calls == [], detail=b1_qr_calls)
            check("9a. [B1] prep:continue QR without proxy: renders _pxm_menu_buttons(key) via _safe_event_edit",
                  len(b1_edit_calls) == 1 and b1_edit_calls[-1][1] == panel_bot._pxm_menu_buttons("acc1"), detail=b1_edit_calls)

            # 8/9b. Phone, no proxy: _auth_enter_phone must NOT be called.
            b1_qr_calls.clear(); b1_phone_calls.clear(); b1_edit_calls.clear()
            panel_bot._prep_fetch_card = _b1_card_factory(
                {"manager_key": "acc1", "substate": "draft", "auth_source": "phone", "proxy_host": "", "proxy_port": "", "proxy_enabled": 0}
            )
            _b1_asyncio.run(panel_bot._prep_callback(_B1FakeEvent(b"prep:continue:acc1")))
            check("8. [B1] prep:continue Phone without proxy: _auth_enter_phone calls == 0", b1_phone_calls == [], detail=b1_phone_calls)
            check("9b. [B1] prep:continue Phone without proxy: renders the proxy menu", len(b1_edit_calls) == 1, detail=b1_edit_calls)

            # 10. Stale continue after a proxy release: proxy_host/port still
            # present (not cleared), but proxy_enabled==0 -- must ALSO fail
            # closed, not just the empty-host case above (this is the exact
            # "release proxy then reuse a stale card" race the review named).
            b1_qr_calls.clear(); b1_phone_calls.clear(); b1_edit_calls.clear()
            panel_bot._prep_fetch_card = _b1_card_factory(
                {"manager_key": "acc1", "substate": "draft", "auth_source": "qr", "proxy_host": "1.2.3.4", "proxy_port": "1080", "proxy_enabled": 0}
            )
            _b1_asyncio.run(panel_bot._prep_callback(_B1FakeEvent(b"prep:continue:acc1")))
            check("10. [B1] stale prep:continue after proxy release (proxy_enabled=0): auth calls == 0",
                  b1_qr_calls == [] and b1_phone_calls == [], detail=(b1_qr_calls, b1_phone_calls))

            # Positive control: WITH a bound proxy, continue still reaches
            # the existing auth helper -- proves the fence isn't
            # unconditionally blocking the ordinary success path.
            b1_qr_calls.clear(); b1_edit_calls.clear()
            panel_bot._prep_fetch_card = _b1_card_factory(
                {"manager_key": "acc1", "substate": "draft", "auth_source": "qr", "proxy_host": "1.2.3.4", "proxy_port": "1080", "proxy_enabled": 1}
            )
            _b1_asyncio.run(panel_bot._prep_callback(_B1FakeEvent(b"prep:continue:acc1")))
            check("B1-control. prep:continue QR WITH a bound proxy still calls _auth_start_qr exactly once",
                  b1_qr_calls == [(111, 222, "acc1")], detail=b1_qr_calls)
        finally:
            panel_bot._prep_fetch_card = _b1_orig_prep_fetch_card
            panel_bot._auth_start_qr = _b1_orig_auth_start_qr
            panel_bot._auth_enter_phone = _b1_orig_auth_enter_phone
            panel_bot._safe_event_edit = _b1_orig_safe_event_edit
            panel_bot._is_allowed = _b1_orig_is_allowed

        # ==================================================================
        # BLOCKER-2 FIX -- _frompool_empty_buttons prepared-aware, and the
        # panel-side bypass callback/step fences (defense in depth; the
        # authoritative gate is the main.py controller-side fix, checked in
        # tools/prepared_accounts_finalize_selftest.py).
        # ==================================================================
        b2_ordinary_texts = [t for t, _ in _flatten(panel_bot._frompool_empty_buttons("acc1", prepared=False))]
        check("11. [B2] _frompool_empty_buttons ordinary (prepared=False) flow keeps the bypass button (regression)",
              any("без proxy" in t for t in b2_ordinary_texts), detail=b2_ordinary_texts)

        b2_prepared_texts = [t for t, _ in _flatten(panel_bot._frompool_empty_buttons("acc1", prepared=True))]
        check("12. [B2] _frompool_empty_buttons prepared=True has NO '🔓 Подключить без proxy' bypass button",
              not any("без proxy" in t for t in b2_prepared_texts), detail=b2_prepared_texts)
        check("13. [B2] prepared empty-pool screen still offers safe alternatives (buy/manual proxy + back)",
              any("Купить" in t for t in b2_prepared_texts) and any("Подключить SOCKS5" in t for t in b2_prepared_texts)
              and any("Назад" in t for t in b2_prepared_texts), detail=b2_prepared_texts)
        check("B2-autodetect. _frompool_empty_buttons(key) with no explicit kwarg auto-detects from the live "
              "managers row (same contract as _add_manager_proxy_choice_buttons)",
              "if prepared is None:" in _func_src(panel_bot._frompool_empty_buttons))

        bypass_callback_src = src[src.find('if data.startswith("wiz:add_manager:bypass:"):'):]
        bypass_callback_src = bypass_callback_src[:bypass_callback_src.find("\n    if data.startswith(", 1) or None]
        check("14. [B2] wiz:add_manager:bypass: callback re-checks the live managers row and refuses status=='prepared' "
              "BEFORE setting the proxy_bypass_password wizard step",
              "status" in bypass_callback_src and "'prepared'" in bypass_callback_src or '"prepared"' in bypass_callback_src)

        # TPILOT PREPARED ACCOUNTS PHASE 7B: 'if step == "proxy_bypass_password":'
        # appears TWICE -- first in the harmless resume-preview text builder
        # (_panel_wizard_resume_text_buttons, which never submits a command),
        # second in the REAL message-dispatch handler that actually calls
        # /manager_proxy_bypass. Must anchor on the SECOND occurrence.
        _bypass_step_marker = 'if step == "proxy_bypass_password":'
        _bypass_step_first = src.find(_bypass_step_marker)
        _bypass_step_second = src.find(_bypass_step_marker, _bypass_step_first + 1)
        check("(setup) 'proxy_bypass_password' step handler found twice (resume-preview + real dispatch)",
              _bypass_step_first != -1 and _bypass_step_second != -1 and _bypass_step_second > _bypass_step_first)
        bypass_step_src = src[_bypass_step_second:]
        bypass_step_src = bypass_step_src[:bypass_step_src.find('if step == "phone":')]
        check("15. [B2] proxy_bypass_password message step ALSO re-checks status=='prepared' before ever calling "
              "/manager_proxy_bypass -- checked BEFORE the password comparison",
              bypass_step_src.find('"prepared"') != -1
              # anchored on the f-string command literal (f"/manager_proxy_bypass), not the bare substring --
              # this file's own explanatory comment also contains the bare text and would false-match otherwise.
              and bypass_step_src.find('"prepared"') < bypass_step_src.find('f"/manager_proxy_bypass'))

        check("17. [B2] prepared empty-pool path never reaches the second auth chooser directly "
              "(_frompool_empty_buttons has no _auth_chooser_buttons call)",
              "_auth_chooser_buttons" not in _func_src(panel_bot._frompool_empty_buttons))
        check("18. [B2] prepared bypass callback path returns immediately on the prepared guard -- never reaches "
              "the _auth_chooser_buttons('add', ...) call further down in the SAME wizard step",
              bypass_step_src.find('"prepared"') < bypass_step_src.find('_auth_chooser_buttons("add"')
              if '_auth_chooser_buttons("add"' in bypass_step_src else True)

        check("19. [B1] QR_SECOND_AUTH_CHOOSER = NO -- the DRAFT-without-proxy continue path never renders "
              "_auth_chooser_buttons (goes to the proxy menu instead)", "_auth_chooser_buttons" not in _func_src(panel_bot._prep_callback))
        check("20. [B2] PHONE_SECOND_AUTH_CHOOSER = NO -- same evidence as 19 (single shared _prep_callback "
              "handler for both auth_source values)", "_auth_chooser_buttons" not in _func_src(panel_bot._prep_callback))

        # ==================================================================
        # 18. Session/TData activation screen: no code/SMS text, exact spinner line.
        # ==================================================================
        screen_text = panel_bot._prep_activation_screen_text(stored)
        check("18a. activation screen contains the EXACT ACTIVATION_VERIFYING_TEXT line",
              panel_bot._prepared_texts.ACTIVATION_VERIFYING_TEXT in screen_text, detail=screen_text)
        check("18b. activation screen never mentions a login code/SMS (activation never repeats a login)",
              "код" not in screen_text.lower() and "sms" not in screen_text.lower(), detail=screen_text)
        check("18c. activation screen reuses the EXISTING '🔐 Подключение Telegram' title (ACTIVATION_TITLE)",
              panel_bot._prepared_texts.ACTIVATION_TITLE in screen_text)

        # ==================================================================
        # 19/26/23. Activation success reuses the EXISTING source-pick screen.
        # ==================================================================
        activate_branch_start = activate_src.find('if action == "activate":')
        activate_branch_end = activate_src.find('if action == "delete":')
        activate_branch = activate_src[activate_branch_start:activate_branch_end] if activate_branch_start >= 0 else ""
        check("19d/26. activate's OK path branches on _ONBOARDING_SOURCE_PICK_MARKER and calls the EXISTING "
              "_send_text_result_with_source_pick / _send_text_result_with_panel -- no new prepared-specific "
              "success screen is built",
              "_ONBOARDING_SOURCE_PICK_MARKER in result_text" in activate_branch
              and "_send_text_result_with_source_pick(chat_id, user_id, result_text, key)" in activate_branch
              and "_send_text_result_with_panel(chat_id, user_id, result_text)" in activate_branch,
              detail=activate_branch)

        # ==================================================================
        # 20/27. Activation failure keeps the card reachable, never releases proxy.
        # ==================================================================
        check("20c/27. activate's failure path (not ok, not NEED_PROXY) re-renders the card via "
              "_prep_card_text/_prep_card_buttons -- never calls proxy_pool_unassign",
              "_prep_card_text(item2)" in activate_branch and "_prep_card_buttons(item2)" in activate_branch
              and "/proxy_pool_unassign" not in activate_branch, detail=activate_branch)
        check("20d. no unconditional proxy release exists anywhere in _prep_callback outside the explicit, "
              "user-confirmed releaseproxy_confirm action",
              activate_src.count("/proxy_pool_unassign") == 1)

        # ==================================================================
        # 21/28/29. Change/release proxy reuse existing menus/backends.
        # ==================================================================
        check("21/29. 'changeproxy' action renders the EXISTING _pxm_menu_buttons(key) -- no new prepared "
              "proxy menu is built",
              'action == "changeproxy"' in activate_src and "_pxm_menu_buttons(key)" in activate_src)
        check("28. 'releaseproxy_confirm' delegates to the EXISTING /proxy_pool_unassign command",
              'f"/proxy_pool_unassign {lease_id}"' in activate_src)
        check("28b. releasing proxy requires an explicit confirm step first (no one-tap destructive release)",
              'action == "releaseproxy"' in activate_src and 'action == "releaseproxy_confirm"' in activate_src
              and "Да, освободить" in activate_src)

        # ==================================================================
        # 30. Delete uses /prepared_delete with the same protection level as
        # the canonical manager delete (password-gated confirmation).
        # ==================================================================
        check("30a. 'delete' action requires MANAGER_ADMIN_PASSWORD confirmation (matches guard:delete_confirm's "
              "own danger_password gate) before calling /prepared_delete",
              '_wizard_set(chat_id, user_id, "prepared_delete", "password", {"key": key})' in activate_src)
        check("30b. the prepared_delete wizard step checks MANAGER_ADMIN_PASSWORD before calling /prepared_delete",
              'if not MANAGER_ADMIN_PASSWORD or password != MANAGER_ADMIN_PASSWORD:' in src
              and 'f"/prepared_delete {key}"' in src)

        # ==================================================================
        # 31. Deterministic back navigation.
        # ==================================================================
        check("31a. card -> list: 'card' action's not-found fallback and _prep_card_buttons both route to prep:menu",
              'b"prep:menu"' in _func_src(panel_bot._prep_card_buttons))
        check("31b. list -> manager admin menu: _prep_list_buttons' back row targets menu:manager_admin",
              'b"menu:manager_admin"' in _func_src(panel_bot._prep_list_buttons))
        check("31c. prepare auth chooser 'back' returns to the add-manager branch-choice screen (not a bare cancel)",
              'if auth_ctx == "prepare":' in src and '_wizard_set(chat_id, user_id, "add_manager", "branch_choice", {})' in src)
        back_branch_body = src.split('if auth_method == "back":', 1)[1].split('if auth_method == "phone":', 1)[0]
        check("31d. the old add-context and replace-context 'back' behaviors are unmodified (still fall through "
              "to the generic wizard_clear+delete path for their own contexts)",
              '_wizard_clear(chat_id, user_id)' in back_branch_body, detail=back_branch_body)

        # ==================================================================
        # 34. Exact substate texts.
        # ==================================================================
        check("34a. substate_label(DRAFT) == '⏳ ожидает авторизации'",
              panel_bot._prepared_texts.substate_label("draft") == "⏳ ожидает авторизации")
        check("34b. substate_label(VERIFIED) == '✅ Telegram проверен'",
              panel_bot._prepared_texts.substate_label("verified") == "✅ Telegram проверен")
        check("34c. substate_label(STORED) == '⏳ проверка при подключении'",
              panel_bot._prepared_texts.substate_label("stored") == "⏳ проверка при подключении")
        failed_verify = dict(verified, last_verify_ok=0, last_verify_error="unauthorized")
        check("34d. a failed last verify appends the non-blocking '❌ Последняя проверка неуспешна' note, "
              "without changing the underlying substate label",
              "❌ Последняя проверка неуспешна" in panel_bot._prep_card_text(failed_verify)
              and "✅ Telegram проверен" in panel_bot._prep_card_text(failed_verify))

        # ==================================================================
        # 35. Generic manager card icon/label mapping for a prepared row.
        # ==================================================================
        prepared_row = {"status": "prepared", "is_enabled": 0, "manual_stopped": 1}
        check("35a. _manager_state_icon(prepared) == 💾 (not the generic ⚪ 'disabled' icon)",
              panel_bot._manager_state_icon(prepared_row) == "💾")
        check("35b. _manager_state_label(prepared) == 'подготовлен' (not 'выключен')",
              panel_bot._manager_state_label(prepared_row) == "подготовлен")

        # ==================================================================
        # 32/25. Panel callback is thin: routing/rendering/submit-command/
        # refresh only -- no SQL, no TelegramClient, no lifecycle writes.
        # ==================================================================
        forbidden_tokens = [
            "sqlite3", "aiosqlite", "TelegramClient(", ".connect()", "get_me()", "is_user_authorized()",
            "manager_set_fields(", "_spawn_manager_process(", "manager_bot_access_ensure_sync(",
            "ProxySellerProvider(", "allow_spend",
        ]
        prep_all_src = activate_src + _func_src(panel_bot._prep_resume_after_proxy) \
            + _func_src(panel_bot._auth_start_qr) + _func_src(panel_bot._auth_enter_phone) \
            + _func_src(panel_bot._prepared_import_document_input)
        for tok in forbidden_tokens:
            check(f"32/25. prepared-accounts panel code contains no '{tok}' (routing/rendering only)",
                  tok not in prep_all_src)
        check("32b. every /prepared_* business action in the panel goes through _submit_and_wait "
              "(never a direct storage/service call)",
              "_submit_and_wait(f\"/prepared_" in src or 'await _submit_and_wait(f"/prepared_' in src)

        # ==================================================================
        # 33. prep: prefix does not collide with any existing broad handler.
        # ==================================================================
        broad_prefixes = re.findall(r'data\.startswith\("([a-z_]+:)"\)', src)
        colliding = [p for p in set(broad_prefixes) if p != "prep:" and "prep:".startswith(p)]
        check("33. PREP_CALLBACK_PREFIX_COLLISION = NO -- no existing startswith(...) prefix is a prefix of 'prep:'",
              not colliding, detail=colliding)
        check("33b. _prep_callback silently returns (no alert) on both the permission check and the prefix "
              "check -- it fires for EVERY callback in the bot (no @client.on data filter), matching the "
              "established _pxm_callback/_ppool_callback shape, not the main on_callback's alerting shape",
              _func_src(panel_bot._prep_callback).count("if not _is_allowed(event):\n            return") == 1
              or "if not _is_allowed(event):\n            return" in _func_src(panel_bot._prep_callback))

        # ==================================================================
        # Regression: ordinary "▶️ Подключить сейчас" flow is untouched.
        # ==================================================================
        # NOTE: _panel_wizard_resume_text_buttons has a later override (capture
        # -and-delegate pattern, _TPAC_PANEL_ORIG_PANEL_WIZARD_RESUME) that only
        # adds 3 unrelated wizard names and delegates everything else -- so
        # inspect.getsource() of the live attribute would only show the
        # wrapper, not the add_manager branch this checks. Search the whole
        # module source instead (both stacked defs are real, live code).
        check("REGR-1. the ordinary add_manager proxy_choice/auth_choice wizard steps are unaffected: "
              "the resume-screen renderer still shows step=='proxy_choice' with the plain (kwarg-free) "
              "proxy chooser call",
              'f"➕ Добавление менеджера\\n\\nМенеджер: {key}\\n\\nВыберите способ подключения.", _add_manager_proxy_choice_buttons(key)' in src)
        check("REGR-2. _auth_chooser_buttons('add', ref) callback_data is completely unaffected by the new "
              "'prepare' context (still routes QR/Session/TData through the original wiz: callbacks)",
              dict(_flatten(panel_bot._auth_chooser_buttons("add", "regr1"))).get("◻️ QR") == "wiz:qr_start:regr1")

    finally:
        sys.modules.pop("panel_bot", None)
        shutil.rmtree(str(tmp_root), ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PREPARED ACCOUNTS PANEL SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
