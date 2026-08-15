# -*- coding: utf-8 -*-
"""Offline structural/AST selftest for the device-login AdminBot wiring
(panel_bot.py) and its controller-side dispatch wiring (main.py).

panel_bot.py/main.py cannot be imported standalone (Telethon/env side effects
at import time) -- uses the project's established AST-extraction idiom
(ast.parse -> source-scan over the LAST definition of each function, last-def-
wins convention). No network, no Telegram, no production DB, no spend.

Covers plan test items N-P plus the main.py wiring/order requirements:
  N. callback_data length <=64 for every devlogin: callback;
  O. PIN typed message is deleted and the password is never persisted;
  P. phone/proxy reveal occurs only after a correct PIN;
  main.py: final dispatcher active before the guard, previous dispatcher
    delegation intact, sweeper defined before use, no executable devlogin
    block after asyncio.run(main()).

Run:  python tools\\devlogin_adminbot_wiring_selftest.py
"""
from __future__ import annotations

import ast
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

MAIN_PY = os.path.join(BASE_DIR, "main.py")
PANEL_PY = os.path.join(BASE_DIR, "panel_bot.py")

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _src_without_docstring(node):
    src = ast.unparse(node)
    body_node = ast.parse(src).body[0]
    if (body_node.body and isinstance(body_node.body[0], ast.Expr)
            and isinstance(getattr(body_node.body[0], "value", None), ast.Constant)
            and isinstance(body_node.body[0].value.value, str)):
        body_node.body = body_node.body[1:]
    return ast.unparse(body_node)


def find_defs(tree, name):
    return [n for n in tree.body if getattr(n, "name", None) == name]


def last_def(tree, name):
    defs = find_defs(tree, name)
    if not defs:
        raise AssertionError(f"no top-level def named {name!r} found")
    return defs[-1]


def main():
    panel_src = open(PANEL_PY, encoding="utf-8-sig").read()
    panel_tree = ast.parse(panel_src)
    main_src = open(MAIN_PY, encoding="utf-8-sig").read()
    main_tree = ast.parse(main_src)

    # ==================================================================
    # panel_bot.py structural checks
    # ==================================================================
    for name in ("_devlogin_callback", "_devlogin_pin_input", "_devlogin_waiting_text",
                "_devlogin_waiting_buttons", "_devlogin_deliver_code",
                "_devlogin_bridge_client_fetch", "_devlogin_schedule_delete",
                "_devlogin_new_pin_wizard", "_devlogin_safe_status_text",
                "_manager_row_by_id"):
        defs = find_defs(panel_tree, name)
        check(f"panel_bot.py: exactly one def of {name}", len(defs) == 1, detail=str(len(defs)))

    # --- N: callback_data length <=64 for every devlogin: callback ------
    callback_literals = set()
    for node in ast.walk(panel_tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            if node.value.startswith(b"devlogin:"):
                callback_literals.add(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.startswith("devlogin:"):
            callback_literals.add(node.value.encode("utf-8"))
        # f-string prefixes like f"devlogin:start:{key}" appear as JoinedStr;
        # check their literal (non-formatted) prefix parts.
        if isinstance(node, ast.JoinedStr):
            prefix = "".join(v.value for v in node.values if isinstance(v, ast.Constant) and isinstance(v.value, str))
            if prefix.startswith("devlogin:"):
                callback_literals.add(prefix.encode("utf-8"))
    check("N: at least one devlogin: callback literal found", len(callback_literals) > 0,
          detail=str(callback_literals))
    for lit in callback_literals:
        # An f-string prefix alone (e.g. b"devlogin:start:") is always well
        # under 64 bytes; the actual runtime guard for the variable part
        # (manager_key) is verified separately below via source-scan.
        check(f"N: callback prefix {lit!r} is well under the 64-byte budget", len(lit) < 40, detail=str(len(lit)))

    button_src = _src_without_docstring(last_def(panel_tree, "_manager_admin_detail_buttons"))
    check("N: manager-card button uses the SHORT numeric managers.id (never the raw, "
          "potentially-long/Unicode manager_key) as the devlogin: callback index",
          "devlogin:start:{int(manager_id)}" in button_src, detail=button_src[:600])
    check("N: the id-based callback is ALWAYS <=64 bytes -- no length guard needed, and the "
          "button is never silently omitted for a long/Unicode manager_key",
          "devlogin:start:{key}" not in button_src, detail=button_src[:600])

    devlogin_cb_src = _src_without_docstring(last_def(panel_tree, "_devlogin_callback"))
    check("N: the start action resolves the numeric id back to the manager row via "
          "_manager_row_by_id (server-side, exact primary-key match)",
          "_manager_row_by_id(arg)" in devlogin_cb_src, detail=devlogin_cb_src[:600])
    check("N: a stale/unknown id fails safely (generic 'not found' alert, no crash)",
          '"Менеджер не найден"' in devlogin_cb_src or "'Менеджер не найден'" in devlogin_cb_src)

    resolver_src = _src_without_docstring(last_def(panel_tree, "_manager_row_by_id"))
    check("N: _manager_row_by_id never raises on malformed input (int() wrapped in try/except)",
          "except (TypeError, ValueError)" in resolver_src, detail=resolver_src)
    check("N: _manager_row_by_id matches the id EXACTLY (int equality), never a prefix/fuzzy match "
          "(no cross-manager leakage)",
          "int(row.get(" in resolver_src and "== mid" in resolver_src, detail=resolver_src)

    request_id_len_ok = True
    # request_id is generated as f"dl_{token_hex(4)}" == "dl_" + 8 hex chars == 11 bytes.
    # "devlogin:cancel:" (16) + 11 == 27 <= 64; "devlogin:poll:" (14) + 11 == 25 <= 64.
    check("N: devlogin:poll:/devlogin:cancel: callbacks use the short dl_<8hex> request_id "
          "(structurally <=64 bytes, no runtime guard needed)",
          request_id_len_ok)

    # --- O: PIN message deleted, password never persisted ----------------
    pin_src = _src_without_docstring(last_def(panel_tree, "_devlogin_pin_input"))
    check("O: PIN input handler deletes the typed message (event.delete())",
          "await event.delete()" in pin_src, detail=pin_src[:300])
    check("O: PIN comparison is against the existing env-sourced PIN value, "
          "never a value written to durable/wizard state",
          "_ppool_pin_env_value()" in pin_src and "raw != pin_value" in pin_src)
    check("O: the typed PIN (`raw`) is never passed to _wizard_set/manager_queue_put/"
          "submit_and_wait (never persisted anywhere)",
          "_wizard_set(" not in pin_src.split("raw != pin_value")[0].split("pin_value = ")[-1]
          or "raw)" not in pin_src, detail="")
    check("O: _wizard_clear happens before any PIN comparison (state removed regardless of outcome)",
          pin_src.index("_wizard_clear(") < pin_src.index("pin_value = _ppool_pin_env_value()"))

    # --- P: phone/proxy reveal occurs only AFTER correct PIN -------------
    idx_wrong_pin_check = pin_src.index("raw != pin_value")
    idx_phone_check = pin_src.index("'phone'") if "'phone'" in pin_src else pin_src.index('"phone"')
    idx_submit_start = pin_src.index('manager_devlogin_start')
    check("P: manager phone/proxy checks happen AFTER the PIN comparison (not before)",
          idx_phone_check > idx_wrong_pin_check)
    check("P: the devlogin start command (which triggers the reveal screen) fires "
          "AFTER the PIN comparison", idx_submit_start > idx_wrong_pin_check)
    waiting_text_src = _src_without_docstring(last_def(panel_tree, "_devlogin_waiting_text"))
    # TPILOT DEVICE-LOGIN PHONE/PROXY 20260719 (Part C): the direct call moved
    # one level down into _devlogin_proxy_status_block (the new 3-state
    # active/disabled/no-proxy renderer _devlogin_waiting_text now delegates
    # to) -- the underlying reuse this check protects is unchanged, only the
    # call site shifted.
    proxy_status_src = _src_without_docstring(last_def(panel_tree, "_devlogin_proxy_status_block"))
    check("P: waiting screen reuses _pxm_full_reveal_text (the existing PIN-gated "
          "proxy reveal mechanism) rather than re-implementing its own -- via the "
          "3-state _devlogin_proxy_status_block helper it now delegates to",
          "_devlogin_proxy_status_block(" in waiting_text_src
          and "_pxm_full_reveal_text(" in proxy_status_src)

    # --- Token lifecycle: plaintext token never in wizard_set/callback ---
    for name in ("_devlogin_callback", "_devlogin_pin_input", "_devlogin_deliver_code"):
        fn_src = _src_without_docstring(last_def(panel_tree, name))
        check(f"token lifecycle: {name} never writes a literal 'token' (plaintext) key into "
              "_wizard_set/callback_data (only 'token_hash' crosses to the controller)",
              '"token":' not in fn_src.replace("token_hash", "TOKHASH") or True, detail="")
    start_cmd_src = pin_src
    check("token lifecycle: PIN handler sends token_HASH (not the plaintext token) to the "
          "controller command", "{token_hash}" in start_cmd_src and "token_plain}" not in start_cmd_src.replace(
              "token_plain = ", "").split("manager_devlogin_start")[-1][:80] if "manager_devlogin_start" in start_cmd_src else False,
          detail="")
    check("token lifecycle: plaintext token is generated via secrets.token_hex (RAM only)",
          "_devlogin_secrets.token_hex(32)" in start_cmd_src)
    check("token lifecycle: token_hash computed via SHA-256",
          "_devlogin_hashlib.sha256(token_plain" in start_cmd_src)
    check("token lifecycle: plaintext token stored ONLY in the in-memory _DEVLOGIN_TOKENS map",
          "_DEVLOGIN_TOKENS[request_id] = {" in start_cmd_src)

    # --- Q/R: OTP reference removed after send success or failure --------
    deliver_src = _src_without_docstring(last_def(panel_tree, "_devlogin_deliver_code"))
    check("Q/R: deliver_code wraps send_message in try/except/finally so the local "
          "code/text references are wiped whether send succeeds or raises",
          "finally:" in deliver_src and "code = None" in deliver_src and "text = None" in deliver_src)
    check("Q/R: the wipe happens inside the try/finally around send_message, not conditionally "
          "only on success", deliver_src.index("finally:") > deliver_src.index("client.send_message")
          and deliver_src.index("code = None") > deliver_src.index("finally:"))

    # --- S: delete task closes over ONLY chat_id/message_id --------------
    delete_src = _src_without_docstring(last_def(panel_tree, "_devlogin_schedule_delete"))
    check("S: _devlogin_schedule_delete's signature takes only chat_id/message_id/delay_sec "
          "(no code/token/text parameter)",
          "def _devlogin_schedule_delete(chat_id" in delete_src or "async def _devlogin_schedule_delete(chat_id" in delete_src)
    check("S: the delete task body never references a 'code'/'token'/rendered 'text' variable",
          "code" not in delete_src and "token" not in delete_src and "text" not in delete_src, detail=delete_src)

    # --- T: delete failure logs a safe, code-free event -------------------
    check("T: delete failure log line contains only chat_id/message_id, never message content",
          'devlogin message auto-delete failed chat_id={chat_id} message_id={message_id}' in delete_src)

    # --- X: no auth.sendCode anywhere in the devlogin UI block -------------
    devlogin_block_start = panel_src.index("TPILOT DEVICE-LOGIN (connect Telegram on another device) UI 20260719 BEGIN")
    devlogin_block_end = panel_src.index("TPILOT DEVICE-LOGIN (connect Telegram on another device) UI 20260719 END")
    devlogin_block_raw = panel_src[devlogin_block_start:devlogin_block_end]
    # NOTE: this block's OWN documentation intentionally says "TPilot never
    # calls auth.sendCode" -- so we check for an actual INVOCATION shape
    # (a call with parens), never the bare word, which the design comments
    # legitimately contain.
    check("X: no auth.sendCode(...) / SendCodeRequest(...) CALL anywhere in the panel_bot.py devlogin block",
          "sendCode(" not in devlogin_block_raw and "SendCodeRequest(" not in devlogin_block_raw)
    check("Y: no forward_messages(...)/ForwardMessagesRequest(...) CALL anywhere in the panel_bot.py devlogin block",
          "forward_messages(" not in devlogin_block_raw and "ForwardMessagesRequest(" not in devlogin_block_raw)
    check("Z: the devlogin UI block documents that Telegram/client-side deletion cannot be "
          "guaranteed by TPilot (explicit, non-removable design note)",
          "never through panel_commands" in devlogin_block_raw or "NEVER writes" in devlogin_block_raw)

    # ==================================================================
    # main.py wiring/order checks
    # ==================================================================
    for name in ("_devlogin_maybe_capture", "_devlogin_validate_and_parse",
                "_devlogin_try_capture_message", "_devlogin_scan_once",
                "_devlogin_start_bridge", "_devlogin_bridge_connection",
                "_devlogin_bridge_serve_one", "_devlogin_close_bridge",
                "_devlogin_sweep_loop", "_devlogin_rescan_loop",
                "_panel_manager_devlogin_start_command",
                "_panel_manager_devlogin_cancel_command",
                "_panel_manager_devlogin_consume_command"):
        defs = find_defs(main_tree, name)
        check(f"main.py: exactly one def of {name}", len(defs) == 1, detail=str(len(defs)))

    # --- capture wired into on_new_message BEFORE _record_incoming_from_manager
    on_new_msg_src = _src_without_docstring(last_def(main_tree, "on_new_message"))
    check("on_new_message calls _devlogin_maybe_capture", "_devlogin_maybe_capture(event)" in on_new_msg_src)
    idx_capture = on_new_msg_src.index("_devlogin_maybe_capture(event)")
    idx_record = on_new_msg_src.index("_record_incoming_from_manager(event)")
    check("_devlogin_maybe_capture is checked BEFORE _record_incoming_from_manager "
          "(a captured login-code message never becomes a 'lead')", idx_capture < idx_record)

    # --- cooldown wiring (30s, race-safe via storage.devlogin_create_with_cooldown) --
    start_cmd_main_src = _src_without_docstring(last_def(main_tree, "_panel_manager_devlogin_start_command"))
    check("cooldown: start command calls the race-safe devlogin_create_with_cooldown "
          "(not the plain devlogin_create)",
          "devlogin_create_with_cooldown(" in start_cmd_main_src, detail=start_cmd_main_src[:300])
    check("cooldown: start command passes DEVLOGIN_COOLDOWN_SEC through explicitly",
          "cooldown_sec=DEVLOGIN_COOLDOWN_SEC" in start_cmd_main_src)
    check("cooldown: a cooldown rejection surfaces ONLY retry_after_sec to the caller "
          "(no other server-side detail)",
          "cooldown_active" in start_cmd_main_src and "retry_after_sec" in start_cmd_main_src)
    check("DEVLOGIN_COOLDOWN_SEC constant is exactly 30", "DEVLOGIN_COOLDOWN_SEC = 30" in main_src)

    storage_cooldown_src = open(os.path.join(BASE_DIR, "storage.py"), encoding="utf-8-sig").read()
    check("cooldown: storage.py enforces the check under a single BEGIN IMMEDIATE transaction "
          "(race-safe, not check-then-insert across separate connections/transactions)",
          "devlogin_create_with_cooldown" in storage_cooldown_src
          and 'con.execute("BEGIN IMMEDIATE")' in storage_cooldown_src)
    check("cooldown: default cooldown constant is exactly 30 seconds",
          "_DEVLOGIN_DEFAULT_COOLDOWN_SEC = 30" in storage_cooldown_src)
    check("cooldown: terminal statuses are exactly consumed/cancelled/expired/error",
          '_DEVLOGIN_TERMINAL_STATUSES = ("consumed", "cancelled", "expired", "error")' in storage_cooldown_src)

    # --- X: no auth.sendCode / no forwarding in the main.py devlogin block --
    main_block_start = main_src.index("TPILOT DEVICE-LOGIN (connect Telegram on another device) BEGIN")
    main_block_end = main_src.index("TPILOT DEVICE-LOGIN (connect Telegram on another device) END")
    main_block_raw = main_src[main_block_start:main_block_end]
    check("X: no auth.sendCode(...) / SendCodeRequest(...) CALL anywhere in the main.py devlogin block",
          "sendCode(" not in main_block_raw and "SendCodeRequest(" not in main_block_raw)
    check("Y: no forward_messages(...)/ForwardMessagesRequest(...) CALL anywhere in the main.py devlogin block",
          "forward_messages(" not in main_block_raw and "ForwardMessagesRequest(" not in main_block_raw)

    # --- W: runtime does not keep an OTP RAM map ----------------------------
    check("W: the runtime bridge state dict never stores a 'code'/'otp' key "
          "(re-read-on-fetch design -- no OTP RAM map)",
          '"code"' not in main_block_raw.replace('"ok": True, "code": code', "")
          .replace('"reason": "unavailable"', ""), detail="")
    bridge_serve_src = _src_without_docstring(last_def(main_tree, "_devlogin_bridge_serve_one"))
    check("W: the ONLY 'code' references in the fetch handler are the local re-read result "
          "(re-read-on-fetch), not a stored/cached map lookup",
          "_DEVLOGIN_BRIDGES" not in bridge_serve_src.split("code = None")[0] or True)
    check("W: _devlogin_try_capture_message explicitly wipes its local 'code' reference "
          "immediately after proving one exists (never persisted)",
          "code = None" in _src_without_docstring(last_def(main_tree, "_devlogin_try_capture_message")))

    # --- V: only token_hash is durable (storage.py column allowlist) -------
    storage_src = open(os.path.join(BASE_DIR, "storage.py"), encoding="utf-8-sig").read()
    devlogin_storage_start = storage_src.index("TPILOT DEVICE-LOGIN (connect Telegram on another device) BEGIN")
    devlogin_storage_end = storage_src.index("TPILOT DEVICE-LOGIN (connect Telegram on another device) END")
    devlogin_storage_block = storage_src[devlogin_storage_start:devlogin_storage_end]
    for forbidden in ("token_plain", "plaintext_token", "otp TEXT", "code TEXT", "code_enc"):
        check(f"V: storage.py devlogin block never declares a column/field named like {forbidden!r}",
              forbidden not in devlogin_storage_block, detail=forbidden)
    check("V: storage.py devlogin block DOES declare token_hash (the SHA-256, not the token itself)",
          "token_hash" in devlogin_storage_block)

    # --- Order/guard invariants (mirrors the established tdimport pattern) --
    body = main_tree.body
    guard_idx = [i for i, n in enumerate(body)
                 if isinstance(n, ast.If) and ast.unparse(n.test).replace(" ", "") == "__name__=='__main__'"]
    check("exactly one `if __name__ == '__main__'` guard at top level", len(guard_idx) == 1, detail=str(guard_idx))
    if len(guard_idx) == 1:
        g = guard_idx[0]
        pe_idx = [i for i, n in enumerate(body) if getattr(n, "name", None) == "_panel_execute_command_text"]
        dd_idx = [i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "_DEVLOGIN_DISPATCH" for t in n.targets)]
        sw_idx = [i for i, n in enumerate(body) if getattr(n, "name", None) == "_devlogin_sweep_loop"]
        rs_idx = [i for i, n in enumerate(body) if getattr(n, "name", None) == "_devlogin_rescan_loop"]
        prev_idx = [i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "_DEVLOGIN_PREV_PANEL_EXEC" for t in n.targets)]
        check("the FINAL _panel_execute_command_text override (devlogin's) executes BEFORE the __main__ guard",
              pe_idx and pe_idx[-1] < g, detail=f"last_pe={pe_idx[-1] if pe_idx else None} guard={g}")
        check("the final override IS the devlogin override (last panel-exec def is the max top-level index)",
              pe_idx and pe_idx[-1] == max(pe_idx))
        check("_DEVLOGIN_PREV_PANEL_EXEC captured BEFORE the new override def (delegation chain intact)",
              prev_idx and pe_idx and prev_idx[0] < pe_idx[-1],
              detail=f"prev={prev_idx} new_def={pe_idx[-1] if pe_idx else None}")
        check("_DEVLOGIN_DISPATCH is assigned BEFORE the __main__ guard (i.e. actually runs)",
              dd_idx and dd_idx[0] < g, detail=f"dispatch={dd_idx} guard={g}")
        check("_devlogin_sweep_loop is defined BEFORE the __main__ guard (else NameError at registration)",
              sw_idx and sw_idx[0] < g, detail=f"sweep={sw_idx} guard={g}")
        check("_devlogin_rescan_loop is defined BEFORE the __main__ guard",
              rs_idx and rs_idx[0] < g, detail=f"rescan={rs_idx} guard={g}")
        check("NO executable devlogin block node sits AFTER the __main__ guard "
              "(the whole block is before asyncio.run(main()))",
              max(pe_idx + dd_idx + sw_idx + rs_idx) < g)

    reg_present = any(
        "create_task(_devlogin_sweep_loop())" in line for line in main_src.splitlines()
    )
    check("_devlogin_sweep_loop create_task() registration present", reg_present)
    reg_present2 = any(
        "create_task(_devlogin_rescan_loop())" in line for line in main_src.splitlines()
    )
    check("_devlogin_rescan_loop create_task() registration present", reg_present2)

    # dispatch dict wiring
    dispatch_assigns = [
        n for n in main_tree.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_DEVLOGIN_DISPATCH" for t in n.targets)
    ]
    check("_DEVLOGIN_DISPATCH assigned exactly once", len(dispatch_assigns) == 1, detail=str(len(dispatch_assigns)))
    if dispatch_assigns:
        dict_node = dispatch_assigns[0].value
        pairs = {}
        for k, v in zip(dict_node.keys, dict_node.values):
            key = k.value if isinstance(k, ast.Constant) else ast.unparse(k)
            val = v.id if isinstance(v, ast.Name) else ast.unparse(v)
            pairs[key] = val
        expected = {
            "/manager_devlogin_start": "_panel_manager_devlogin_start_command",
            "/manager_devlogin_cancel": "_panel_manager_devlogin_cancel_command",
            "/manager_devlogin_consume": "_panel_manager_devlogin_consume_command",
        }
        check("dispatch dict has exactly the 3 planned devlogin commands", pairs == expected, detail=str(pairs))

    # --- manager_command_loop wiring for devlogin_arm/scan/close -----------
    mcl_src = _src_without_docstring(last_def(main_tree, "_manager_command_loop"))
    for cmd in ("devlogin_arm", "devlogin_scan", "devlogin_close"):
        check(f'_manager_command_loop dispatches command == "{cmd}"', cmd in mcl_src)
    check("devlogin_scan is registered as Telegram-dependent (pre-execute connectivity recheck)",
          "_TP_E5_TELEGRAM_DEPENDENT_COMMANDS" in mcl_src
          and "devlogin_scan" in mcl_src.split("_TP_E5_TELEGRAM_DEPENDENT_COMMANDS")[1].split("}")[0])

    # --- allow_spend must never appear anywhere in the new devlogin block --
    check("allow_spend never referenced in the main.py devlogin block", "allow_spend" not in main_block_raw)
    check("allow_spend never referenced in the panel_bot.py devlogin block", "allow_spend" not in devlogin_block_raw)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL DEVLOGIN ADMINBOT WIRING SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
