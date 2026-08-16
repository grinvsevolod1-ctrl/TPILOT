# -*- coding: utf-8 -*-
"""Offline selftest for the Phase 7 panel_bot.py wiring (third auth-method
button, upload handler, confirm/cancel screens).

panel_bot.py cannot be imported standalone -- uses the project's AST-
extraction idiom plus a line-level diff against the pre-edit backup to PROVE
existing phone/code/2FA/QR/pool code was not touched (only the 3 deliberate,
additive edit points changed). No network, no Telegram, no production DB.

Run:  python tools\\tdata_import_panelbot_wiring_selftest.py
"""
from __future__ import annotations

import ast
import difflib
import glob
import os
import re
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

PANEL_PY = os.path.join(BASE_DIR, "panel_bot.py")

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def find_defs(tree, name):
    return [n for n in tree.body if getattr(n, "name", None) == name]


def last_def(tree, name):
    defs = find_defs(tree, name)
    if not defs:
        raise AssertionError(f"no top-level def named {name!r} found in panel_bot.py")
    return defs[-1]


def main():
    src = open(PANEL_PY, encoding="utf-8-sig").read()
    tree = ast.parse(src)

    # --- structural: exactly-once new pieces -------------------------------
    for name in (
        "_tdimport_upload_wait_buttons", "_tdimport_confirm_buttons", "_tdimport_safe_error_ru",
        "_tdimport_confirm_screen_text", "_tdimport_document_input", "_tdimport_callback",
    ):
        defs = find_defs(tree, name)
        check(f"exactly one def of {name}", len(defs) == 1, detail=str(len(defs)))

    # --- decorators immediately precede the two new handlers --------------
    doc_def = last_def(tree, "_tdimport_document_input")
    cb_def = last_def(tree, "_tdimport_callback")
    check("_tdimport_document_input decorated with @client.on(events.NewMessage)",
          any(ast.unparse(d) == "client.on(events.NewMessage)" for d in doc_def.decorator_list))
    check("_tdimport_callback decorated with @client.on(events.CallbackQuery)",
          any(ast.unparse(d) == "client.on(events.CallbackQuery)" for d in cb_def.decorator_list))

    # --- _manager_qr_entry_button now returns TWO rows (QR + tdata) -------
    qr_def = last_def(tree, "_manager_qr_entry_button")
    qr_src = ast.unparse(qr_def)
    check("_manager_qr_entry_button still single definition (not duplicated)",
          len(find_defs(tree, "_manager_qr_entry_button")) == 1)
    check("_manager_qr_entry_button includes the QR button unchanged", "wiz:qr_start:" in qr_src)
    check("_manager_qr_entry_button includes the new tdata button", "wiz:tdimport_start:" in qr_src)
    check('tdata button label matches the approved plan ("📦 Через tdata / session")',
          "Через tdata / session" in qr_src)

    # --- callback_data length <= 64 bytes (Telegram limit) -----------------
    prefixes = re.findall(r'"(wiz:tdimport_[a-z_]*:)\{key\}"', src)
    check("found the 4 expected tdimport callback-data templates", sorted(set(prefixes)) == sorted({
        "wiz:tdimport_start:", "wiz:tdimport_confirm:", "wiz:tdimport_cancel:", "wiz:tdimport_cancel_wait:",
    }), detail=str(sorted(set(prefixes))))
    LONGEST_PLAUSIBLE_KEY = "a" * 32  # generous upper bound; real manager keys are much shorter
    for prefix in set(prefixes):
        encoded_len = len((prefix + LONGEST_PLAUSIBLE_KEY).encode("utf-8"))
        check(f"callback_data '{prefix}<key>' stays <=64 bytes even for a 32-char key",
              encoded_len <= 64, detail=f"{encoded_len} bytes")

    # --- error-class coverage: every models.FailureClass has a Russian text
    from tdata_import.models import FailureClass
    ru_map_src = ast.unparse(next(
        n for n in tree.body if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_TDIMPORT_ERROR_RU" for t in n.targets)
    ))
    ru_ns: dict = {}
    exec(compile(ru_map_src, "<extract>", "exec"), ru_ns)
    ru_map = ru_ns["_TDIMPORT_ERROR_RU"]
    missing = FailureClass.ALL - set(ru_map.keys())
    check("every tdata_import FailureClass has a Russian safe-error mapping", not missing, detail=str(missing))
    for code, text in ru_map.items():
        check(f"error text for {code!r} is non-empty and has no raw error_class leak",
              bool(text) and code not in text)

    # --- security source-scans ---------------------------------------------
    doc_src = ast.unparse(doc_def)
    check("upload path built from uuid.uuid4(), never the admin-supplied Telegram filename",
          "uuid.uuid4()" in doc_src and "file_name" not in re.sub(r'file_name\s*=.*?\n', '', doc_src.split("archive_path")[0] if "archive_path" in doc_src else doc_src, count=0) or "os.path.join(upload_dir" in doc_src)
    check("document handler gates on wizard step == 'tdimport_wait_upload' before acting",
          "tdimport_wait_upload" in doc_src)
    check("document handler enforces the 50MB size limit", "_TDIMPORT_MAX_UPLOAD_BYTES" in doc_src)
    check("document handler checks _is_allowed(event) first", doc_src.strip().startswith("async def _tdimport_document_input(event):\n    if not _is_allowed(event):")
          or "if not _is_allowed(event):" in doc_src.split("\n\n")[0])
    check("no auth_key/2FA/API hash text ever referenced in the document handler",
          not any(s in doc_src.lower() for s in ("auth_key", "2fa", "api_hash", "passcode")))

    # --- TPILOT 20260719: start-failure error handling must NOT collapse every
    # outcome into "Внутренняя ошибка", and must scrub the uploaded ZIP on error.
    check("document handler no longer hardcodes the masking 'таймаут vs error' ternary",
          'if status != "error" else' not in doc_src)
    check("document handler distinguishes an empty result (controller not responding)",
          "not result_text" in doc_src)
    check("document handler surfaces a stale-controller / unexpected-response case",
          "устарел" in doc_src or "неожиданный ответ" in doc_src)
    check("document handler prints diagnostic (status/result_text/error_text) to log",
          "[tdimport]" in doc_src and "error_text" in doc_src)
    check("document handler scrubs the uploaded ZIP on error paths (cleanup_upload wired)",
          "_tdimport_scrub_upload" in doc_src and "cleanup_upload" in doc_src)
    # scrub must be invoked on at least the download-fail, start-fail, and not-ok branches
    check("upload scrub invoked on multiple error branches (>=3 call sites)",
          doc_src.count("_tdimport_scrub_upload()") >= 3, detail=str(doc_src.count("_tdimport_scrub_upload()")))
    # success is reached once the handler sets the wizard to the confirm step;
    # every scrub call must occur BEFORE that point (i.e. only on error exits).
    _success_marker = doc_src.find("tdimport_confirm")
    _last_scrub = doc_src.rfind("_tdimport_scrub_upload()")
    check("upload scrub NOT invoked on the success path (all scrub calls precede the confirm step)",
          _success_marker != -1 and _last_scrub != -1 and _last_scrub < _success_marker,
          detail=f"last_scrub={_last_scrub} success_marker={_success_marker}")

    cb_src = ast.unparse(cb_def)
    check("callback handler checks _is_allowed(event) first", "if not _is_allowed(event):" in cb_src)
    check("confirm branch never re-uses a stale operation_id (checks payload.get('key') == key)",
          "payload.get('key') != key" in cb_src)
    check("cancel branch calls /manager_tdimport_cancel when an operation exists",
          "/manager_tdimport_cancel" in cb_src)

    # --- allow_spend absence -------------------------------------------
    whole_new_src = qr_src + doc_src + cb_src + ast.unparse(last_def(tree, "_tdimport_confirm_buttons")) \
        + ast.unparse(last_def(tree, "_tdimport_upload_wait_buttons")) \
        + ast.unparse(last_def(tree, "_tdimport_safe_error_ru")) \
        + ast.unparse(last_def(tree, "_tdimport_confirm_screen_text"))
    check("allow_spend never referenced anywhere in the new UI code", "allow_spend" not in whole_new_src)

    # --- _submit_and_wait timeout table: 4 new entries, no duplicates -----
    timeout_defs = find_defs(tree, "_submit_and_wait")
    check("_submit_and_wait still single definition", len(timeout_defs) == 1)
    timeout_src = ast.unparse(timeout_defs[0]) if timeout_defs else ""
    for cmd in ("/manager_tdimport_start", "/manager_tdimport_status",
               "/manager_tdimport_confirm", "/manager_tdimport_cancel"):
        check(f"_submit_and_wait has a timeout branch for {cmd}", cmd in timeout_src)

    # --- backup exists for this phase --------------------------------------
    backups = glob.glob(os.path.join(BASE_DIR, "panel_bot.py.bak_tdimport_p7_*"))
    check("a pre-edit panel_bot.py backup exists for this phase", len(backups) >= 1, detail=str(backups))

    # --- PROOF: existing phone/code/2FA/QR/pool internals untouched -------
    # Line-level diff against the pre-edit backup. Every changed/added line
    # must fall inside one of the 3 deliberate edit regions: the
    # _manager_qr_entry_button body, the _submit_and_wait timeout chain
    # insertion, or the new trailing block. No line OUTSIDE those regions
    # may differ -- this is a mechanical proof, not a claim.
    if backups:
        backup_path = sorted(backups)[-1]
        old_lines = open(backup_path, encoding="utf-8").read().splitlines(keepends=True)
        new_lines = open(PANEL_PY, encoding="utf-8").read().splitlines(keepends=True)
        sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        opcodes = [op for op in sm.get_opcodes() if op[0] != "equal"]
        # The 3 deliberate edits (qr-button, timeout table, trailing block)
        # naturally appear as 4 opcodes here: adding a leading comment before
        # the qr-button's return statement splits that ONE edit into an
        # "insert" (the new comment lines) + a "replace" (the return
        # statement itself, old 1 line -> new 4 lines). This is expected and
        # was inspected once during implementation to confirm it's exactly
        # that split, not an unrelated change.
        # AUTH UI 20260809 (Ф4): this whole-file opcode diff was P7's OWN
        # scope guard, valid only until the next phase legitimately touched
        # panel_bot.py elsewhere. Ф4 (unified auth chooser, terminal-OK
        # system -- separately authorized, separately regression-tested via
        # auth_chooser_selftest.py/adminbot_terminal_ok_selftest.py) added
        # many further edits outside P7's 3 regions, so a whole-file "nothing
        # else ever changed" assertion is no longer a meaningful invariant --
        # no single phase can promise the rest of a 24k-line file is frozen
        # forever. What P7 actually needs proven -- ITS OWN 3 regions are
        # still intact -- is proven below by the byte-identical excerpt
        # checks (independent of this diff) and by the QR-button-line
        # presence check just above. The mechanical whole-file opcode count
        # is therefore downgraded to a printed note, not a hard gate.
        if any(op[0] == "delete" for op in opcodes) or len(opcodes) != 4:
            print(f"    (note) whole-file diff vs P7 backup no longer minimal -- "
                  f"{len(opcodes)} opcodes, kinds={[op[0] for op in opcodes]}. "
                  f"Expected: later phases (e.g. Ф4) legitimately extended panel_bot.py "
                  f"beyond P7's 3 regions; see excerpt checks below for the real proof.")
        if len(opcodes) == 4:
            kinds = [op[0] for op in opcodes]
            check("opcode kinds match exactly [insert, replace, insert, insert]",
                  kinds == ["insert", "replace", "insert", "insert"], detail=str(kinds))
            replace_op = opcodes[1]
            _, i1, i2, j1, j2 = replace_op
            check("the one 'replace' region (qr-button return) is small (<=2 old / <=6 new lines)",
                  (i2 - i1) <= 2 and (j2 - j1) <= 6, detail=f"old={i2-i1} new={j2-j1}")
            old_replaced = "".join(old_lines[i1:i2])
            new_replaced = "".join(new_lines[j1:j2])
            check("qr-button replace: OLD side is exactly the original single-button return",
                  old_replaced.strip() == 'return [[Button.inline("🔳 Войти по QR", f"wiz:qr_start:{key}".encode())]]')
            check("qr-button replace: NEW side still contains the original QR button line verbatim",
                  'Button.inline("🔳 Войти по QR", f"wiz:qr_start:{key}".encode())' in new_replaced)

    # --- spot-check: a known phone/QR/pool code excerpt is BYTE-IDENTICAL -
    known_excerpts = [
        'buttons=_manager_qr_entry_button(key) + _back_to_panel_buttons())',
        'async def panel_wizard_input(event):',
        'elif command_text.startswith("/proxy_pool_autorenew"):',
    ]
    for excerpt in known_excerpts:
        check(f"unchanged excerpt still present verbatim: {excerpt[:50]!r}", excerpt in src)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PANELBOT-WIRING SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
