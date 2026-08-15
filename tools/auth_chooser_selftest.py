# -*- coding: utf-8 -*-
"""tools/auth_chooser_selftest.py -- offline selftest for the
"TPILOT AUTH SAFETY 20260809" patch (plan Ф4, unified auth UX): the shared
4-method (QR/Phone/Session/TData) auth chooser and its wiring into
add/relogin/replace/HNV2.

Technique: panel_bot.py is imported FOR REAL (not AST-extracted) using the
SAME environment-faking technique tools/startup_smoke_test.py already
established and proved safe -- every project path env var redirected to a
temp directory BEFORE import, `telethon` faked (Button.inline(text, data)
returns a plain (text, data) tuple, TelegramClient.on() just records handler
names, no real client/network), `dotenv` faked so the real .env.TPilot is
never read. This lets the chooser/button-builder functions (pure, no I/O)
run as REAL Python code, not a re-parsed approximation -- while the deeper
on_callback dispatch (which needs a live panel_commands queue to fully
simulate) is instead verified via static source inspection of the function
body actually imported, anchored to unique, load-bearing substrings.

Never: real Telegram network, real proxy/provider network, production
DB/session/runtime/log access or mutation.

    python tools\\auth_chooser_selftest.py
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
    tmp_root = Path(tempfile.mkdtemp(prefix="auth_chooser_selftest_"))
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
    """(text, data) -> (text, decoded_str) -- data may be bytes or str
    depending on how the button was built."""
    text, data = cb
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="ignore")
    return text, data


def _flatten(buttons):
    """List-of-rows -> flat list of (text, data_str)."""
    out = []
    for row in buttons:
        for btn in row:
            out.append(_decode(btn))
    return out


def main() -> int:
    panel_bot, tmp_root = _import_panel_bot()
    try:
        check("module import completed", True)

        src = inspect.getsource(panel_bot)

        # ==================================================================
        # A1/A2/A3: chooser has all 4 methods for add/relogin/replace.
        # ==================================================================
        for ctx, ref in (("add", "mgrtest"), ("relogin", "mgrtest"), ("replace", "op-id-123")):
            flat = _flatten(panel_bot._auth_chooser_buttons(ctx, ref))
            texts = [t for t, _ in flat]
            check(f"A{'123'[('add','relogin','replace').index(ctx)]}. {ctx} chooser has exactly 4 auth methods + Back "
                  "(QR/Phone/Session/TData/Back)",
                  len(flat) == 5, detail=flat)
            check(f"A. {ctx} chooser includes QR", any("QR" in t for t in texts), detail=texts)
            check(f"A. {ctx} chooser includes Phone (Номер телефона)", any("телефон" in t.lower() for t in texts), detail=texts)
            check(f"A. {ctx} chooser includes Session", any("Session" in t for t in texts), detail=texts)
            check(f"A. {ctx} chooser includes TData", any("TData" in t for t in texts), detail=texts)
            check(f"A. {ctx} chooser includes Back (Назад)", any("Назад" in t for t in texts), detail=texts)
            check(f"A. {ctx} chooser's ref ({ref}) appears in every non-add-QR/session/tdata callback_data",
                  all(ref in d or ctx == "add" for _, d in flat if "Назад" in _ or "телефон" in _.lower()),
                  detail=flat)

        # ADD reuses the EXISTING, already-safe onboarding callbacks verbatim.
        flat_add = _flatten(panel_bot._auth_chooser_buttons("add", "mgrtest"))
        by_text_add = dict(flat_add)
        check("A1. add's QR button reuses the EXISTING wiz:qr_start callback (zero new QR code path)",
              by_text_add.get("◻️ QR") == "wiz:qr_start:mgrtest", detail=by_text_add)
        check("A1. add's Session/TData buttons reuse the EXISTING wiz:tdimport_start callback",
              by_text_add.get("📁 Session") == "wiz:tdimport_start:mgrtest"
              and by_text_add.get("📦 TData") == "wiz:tdimport_start:mgrtest", detail=by_text_add)

        # relogin/replace route through the new auth:<ctx>:<method>:<ref> namespace.
        flat_relogin = _flatten(panel_bot._auth_chooser_buttons("relogin", "mgrq"))
        by_text_relogin = dict(flat_relogin)
        check("A2. relogin's QR button routes through auth:relogin:qr:<key>",
              by_text_relogin.get("◻️ QR") == "auth:relogin:qr:mgrq", detail=by_text_relogin)
        check("A2. relogin's Phone button reuses the EXISTING relogin:phone:<key> callback",
              by_text_relogin.get("📱 Номер телефона") == "relogin:phone:mgrq", detail=by_text_relogin)
        check("A2. relogin's Back button reuses the EXISTING relogin:cancel:<key> (real server-side cleanup)",
              by_text_relogin.get("⬅️ Назад") == "relogin:cancel:mgrq", detail=by_text_relogin)

        flat_replace = _flatten(panel_bot._auth_chooser_buttons("replace", "op-777"))
        by_text_replace = dict(flat_replace)
        check("A3. replace's QR button routes through auth:replace:qr:<op_id>",
              by_text_replace.get("◻️ QR") == "auth:replace:qr:op-777", detail=by_text_replace)
        check("A3. replace's Phone button routes through auth:replace:phone:<op_id>",
              by_text_replace.get("📱 Номер телефона") == "auth:replace:phone:op-777", detail=by_text_replace)
        check("A3. replace's Session/TData route through auth:replace:session|tdata:<op_id>",
              by_text_replace.get("📁 Session") == "auth:replace:session:op-777"
              and by_text_replace.get("📦 TData") == "auth:replace:tdata:op-777", detail=by_text_replace)

        # ==================================================================
        # A4: phone step is never set except via an explicit Phone-button
        # press (or an already-mid-phone-flow retry). Verified two ways:
        # (a) source inspection -- every surviving 'first transition after
        #     setup' call site now sets "auth_choice", never "phone";
        # (b) the auth: dispatch's own phone branch is the ONLY new code
        #     path that sets step="phone" for a context that didn't already
        #     have its own pre-existing phone-step mechanism.
        # ==================================================================
        first_transition_markers = [
            'success:\n            _wizard_set(chat_id, user_id, "add_manager", "auth_choice"',
        ]
        check('A4. the ADD proxy-one-line success site now sets step="auth_choice", not "phone"',
              '_wizard_set(chat_id, user_id, "add_manager", "auth_choice", {"key": key})' in src)
        check('A4. the REPLACE px_direct (direct proxy) site now sets step="auth_choice", not "phone"',
              'payload["proxy_source"] = "direct"\n            _wizard_set(chat_id, user_id, "replace", "auth_choice", payload)' in src)
        check('A4. the REPLACE proxy-pool-pick success site now sets step="auth_choice", not "phone"',
              'payload["proxy_source"] = "pool"\n            _wizard_set(chat_id, user_id, "replace", "auth_choice", payload)' in src)
        check('A4. the REPLACE manual-proxy-entry success site now sets step="auth_choice", not "phone"',
              'payload["proxy_source"] = "manual"\n            _wizard_set(chat_id, user_id, "replace", "auth_choice", payload)' in src)
        # Exactly 2 legitimate surviving sites: (1) the auth: dispatch's own
        # Phone-button handler (the intended, only NEW entry into step=
        # "phone"), and (2) the pre-existing wiz:resend_code: lost-payload
        # recovery fallback -- already mid-phone-flow (a QR-entry recovery
        # case, not a first transition), deliberately left untouched.
        phone_step_count = src.count('_wizard_set(chat_id, user_id, "add_manager", "phone", {"key": key})')
        check("A4. exactly the 2 expected surviving 'set step=phone' sites remain for add_manager "
              "(the new Phone-button handler + the pre-existing resend_code lost-payload fallback) -- "
              "no OTHER first-transition site still auto-starts phone",
              phone_step_count == 2, detail=phone_step_count)

        # ==================================================================
        # A5/A6/A7: QR routing per context.
        # ==================================================================
        check("A5. QR for context=add still opens on the manager's own final session path "
              "(_manager_qr_start_command unchanged -- proven structurally in the F3 checkpoint diff, "
              "re-confirmed here: the callback_data is literally the pre-existing wiz:qr_start)",
              by_text_add.get("◻️ QR") == "wiz:qr_start:mgrtest")
        check("A6. QR for context=relogin calls /manager_relogin_qr_start (the Ф3 relogin QR backend)",
              "/manager_relogin_qr_start {key}" in src)
        check("A7. QR for context=replace calls /manager_replace_qr_start (the Ф3 replace QR backend)",
              "/manager_replace_qr_start {op_id}" in src)
        check("A6/A7. relogin and replace QR dispatch never cross-call each other's start command",
              "/manager_relogin_qr_start {op_id}" not in src and "/manager_replace_qr_start {key}" not in src)

        # ==================================================================
        # A8/A9: Session/TData intent routes to the tdimport upload path.
        # ==================================================================
        check("A8. Session intent (relogin) sets wizard step 'tdimport_wait_upload' with kind='session'",
              '_wizard_set(chat_id, user_id, "relogin", "tdimport_wait_upload", {"key": key, "kind": auth_method})' in src)
        check("A9. TData intent (replace) sets wizard step 'tdimport_wait_upload' carrying kind in payload",
              'payload["kind"] = auth_method\n                _wizard_set(chat_id, user_id, "replace", "tdimport_wait_upload", payload)' in src)
        check("A8/A9. relogin Session/TData reuses the EXISTING /manager_tdimport_start command "
              "(no new backend for relogin+tdata -- Ф3's D-INSTALL already proved it safe for an "
              "existing manager)",
              '"/manager_tdimport_start {key} {archive_path}"' in src)
        check("A8/A9. replace Session/TData routes through the NEW /manager_replace_tdimport_start "
              "(temp-path backend built in this Ф4 pass)",
              "/manager_replace_tdimport_start {op_id}" in src)

        # ==================================================================
        # A10: Back from the chooser clears wizard state (never leaves a
        # stale step that would misinterpret the next text message as phone).
        # ==================================================================
        chosen = inspect.getsource(panel_bot.on_callback)
        back_branch_start = chosen.find('if auth_method == "back":')
        back_branch_end = chosen.find('if auth_method == "phone":')
        back_branch = chosen[back_branch_start:back_branch_end] if back_branch_start >= 0 and back_branch_end > back_branch_start else ""
        check("A10. the auth: 'back' branch was found in on_callback", bool(back_branch), detail=bool(back_branch))
        check("A10. Back calls _wizard_clear BEFORE doing anything else (no stale step survives)",
              "_wizard_clear(chat_id, user_id)" in back_branch, detail=back_branch)
        # A10b (Ф4 checkpoint correction): unlike add (no durable per-
        # operation row) and relogin (whose chooser routes Back through the
        # pre-existing relogin:cancel: callback, which already does real
        # cleanup -- see A5/A6's wiring), replace's chooser DOES route Back
        # through this generic handler, and a replace operation is a durable
        # manager_replacements row. An independent review found this was
        # previously skipped, leaving a pre-commit operation orphaned on the
        # server. Proves the fix: for auth_ctx=="replace", the back branch
        # now calls /manager_replace_cancel before clearing wizard state.
        check("A10b. Back cancels the durable replacement operation server-side for the replace context "
              "(not just client-side wizard_clear)",
              'auth_ctx == "replace" and auth_ref' in back_branch and "/manager_replace_cancel {auth_ref}" in back_branch,
              detail=back_branch)

        # ==================================================================
        # A11: HNV2 session_unauthorized -> single unified "Восстановить
        # доступ" action -> relogin:start (the SAME entry point whose own
        # intro screen now shows the full chooser, per A2).
        # ==================================================================
        actions = panel_bot._hnv2_family_actions("session_unauthorized", "mgrhnv2", 1, confidence="certain")
        check("A11. HNV2 session_unauthorized offers EXACTLY one action (not scattered per-method buttons)",
              len(actions) == 1, detail=actions)
        if actions:
            label, cb = actions[0]
            check("A11. HNV2's single action is labeled 'Восстановить доступ'", "Восстановить доступ" in label, detail=label)
            check("A11. HNV2's single action routes to relogin:start:<key> (unified chooser entry point)",
                  cb == "relogin:start:mgrhnv2", detail=cb)
        check("A11. _relogin_intro_buttons (relogin:start's own screen) now returns the unified chooser, "
              "not a phone-only intro",
              panel_bot._relogin_intro_buttons("mgrhnv2") == panel_bot._auth_chooser_buttons("relogin", "mgrhnv2"))

        # ==================================================================
        # MUTATION PROOF: strip a method row out of _auth_chooser_buttons ->
        # A1/A2/A3's "exactly 4 methods" assertion goes RED, proving it is
        # load-bearing (not vacuously true because a 5-row list always has
        # 5 items regardless of content).
        # ==================================================================
        tree = ast.parse(src if False else inspect.getsource(panel_bot._auth_chooser_buttons))
        # (re-parse the function's OWN real source directly, not the whole module)
        fn_node = tree.body[0]
        assert isinstance(fn_node, ast.FunctionDef)

        class _DropSessionRow(ast.NodeTransformer):
            def __init__(self):
                self.hit = False

            def visit_Return(self, node):
                if isinstance(node.value, ast.List) and not self.hit:
                    new_elts = []
                    for elt in node.value.elts:
                        if isinstance(elt, ast.List) and elt.elts:
                            inner = elt.elts[0]
                            if (isinstance(inner, ast.Call) and inner.args
                                    and isinstance(inner.args[0], ast.Constant)
                                    and "Session" in str(inner.args[0].value)):
                                self.hit = True
                                continue
                        new_elts.append(elt)
                    node.value.elts = new_elts
                return node

        stripper = _DropSessionRow()
        mutant = stripper.visit(fn_node)
        assert stripper.hit, "MUT-1 anchor (Session row) not found"
        ast.fix_missing_locations(mutant)
        ns = {"Button": panel_bot.Button, "normalize_manager_key": panel_bot.normalize_manager_key}
        exec(compile(ast.Module(body=[mutant], type_ignores=[]), "<mutant _auth_chooser_buttons>", "exec"), ns)
        mutant_flat = _flatten(ns["_auth_chooser_buttons"]("add", "mgrtest"))
        check("MUT-1. with the Session row stripped from _auth_chooser_buttons, the chooser now has "
              "only 4 rows (not 5) and is MISSING Session -- proves A1-A3's 'exactly 4 methods + Back' "
              "assertion is load-bearing, not a tautology",
              len(mutant_flat) == 4 and not any("Session" in t for t, _ in mutant_flat), detail=mutant_flat)

        # MUTATION: swap relogin/replace QR context in the source (simulates
        # the exact real bug class the Ф3 checkpoint already caught once for
        # 2FA) -> proves A6/A7's "never cross-call" assertion is meaningful.
        swapped_src = src.replace(
            '"/manager_relogin_qr_start {key}"', '"/manager_replace_qr_start {key}"',
        )
        check("MUT-2. a hypothetical swap of relogin's QR command string for replace's WOULD be caught "
              "by A6/A7 (the un-swapped, real source does NOT contain this string -- confirms the "
              "check actually inspects content, not just presence of *a* command string)",
              "/manager_replace_qr_start {key}" in swapped_src and "/manager_replace_qr_start {key}" not in src)

    finally:
        sys.modules.pop("panel_bot", None)
        shutil.rmtree(str(tmp_root), ignore_errors=True)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL AUTH CHOOSER SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
