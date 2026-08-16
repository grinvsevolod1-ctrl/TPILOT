# -*- coding: utf-8 -*-
"""tools/panel_delivery_observability_selftest.py -- N5.4.4: dedicated
selftest for the result-delivery observability fix, introduced per owner
decision 2026-07-24 (Part 5/6). The N5.4 audit established two CONFIRMED
code facts about the "Полная карточка" background-command incident:
  - CONFIRMED DESIGN DEFECT: the button was routed through the generic
    cmd: background-command queue (fixed separately in N5.4.3, native
    screen);
  - CONFIRMED OBSERVABILITY DEFECT: _panel_result_send/_panel_update_
    status_or_send silently swallowed EVERY delivery exception
    (`except Exception: pass`) with zero trace anywhere -- a failed
    delivery could leave the "⏳ Выполняю выбранное действие..." status
    message in place indefinitely with no signal in any log.
This phase fixes ONLY the observability defect: failures are now logged
to a dedicated file (same pattern as the pre-existing _panel_log_slow /
_PANEL_SLOW_LOG_FILE), sanitized (command ARGUMENTS -- which can carry a
password/PIN -- are never logged, only the leading verb), and the
existing delivery/fallback CONTROL FLOW is unchanged (a failed edit still
falls back to a fresh send; a failed send still returns/no-ops exactly as
before -- this phase adds a side channel, it does not change what the
admin sees or what the caller does next).

Everything under test is extracted from the REAL panel_bot.py source via
AST. The only fakes are the Telegram client itself (send_message/
edit_message) -- no real Telegram calls in an offline selftest -- and the
log file path (redirected to a temp file so nothing is written under the
protected logs\\ directory during this selftest).

    python tools\\panel_delivery_observability_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list[str] = []


def check(label: str, condition: bool, detail="") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PANEL_SRC = open(PANEL_PATH, encoding="utf-8-sig").read()
TREE = ast.parse(PANEL_SRC)

DELIVERY_NAMES = {
    "_panel_status_send", "_panel_result_send", "_panel_update_status_or_send",
    "_panel_log_delivery_error", "_panel_sanitize_log_field", "_PANEL_DELIVERY_LOG_FILE",
    # W3.2 TZ-6: _panel_log_delivery_error's log line now builds its timestamp via
    # the approved _utc_now_iso() wrapper instead of a bare datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) --
    # extract it too, or the call raises NameError inside the try/except that
    # _panel_log_delivery_error itself swallows (silently dropping every log line
    # this whole suite exists to assert on).
    "_utc_now_iso",
    # utcnow refactor (2026-08-16): _utc_now_iso now delegates to the
    # module-level _pb_utc_now() clock seam -- extract it too, for the same
    # reason as _utc_now_iso itself (a NameError inside the swallowed
    # try/except would silently drop every log line under test).
    "_pb_utc_now",
}


def _extract_by_name(names: set) -> list:
    nodes = []
    seen = set()
    for n in TREE.body:
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
        raise AssertionError(f"_extract_by_name: expected {names}, missing {missing}")
    return nodes


class _FakeMsg:
    id = 999


class _FakeClient:
    """No real Telegram I/O -- controllable pass/fail per call, records
    every successful send/edit so control-flow can be asserted."""

    def __init__(self, fail_send: bool = False, fail_edit: bool = False,
                 fail_message: str = "simulated network failure"):
        self.fail_send = fail_send
        self.fail_edit = fail_edit
        self.fail_message = fail_message
        self.sent: list = []
        self.edited: list = []

    async def send_message(self, chat_id, text, buttons=None):
        if self.fail_send:
            raise RuntimeError(self.fail_message)
        self.sent.append((chat_id, text))
        return _FakeMsg()

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        if self.fail_edit:
            raise RuntimeError("simulated MessageIdInvalidError")
        self.edited.append((chat_id, message_id, text))


class _UnwritablePath:
    """N5.4.6 (RF2): a minimal stand-in for pathlib.Path exposing exactly
    the two operations _panel_log_delivery_error uses -- `.parent.mkdir()`
    and `.open()` -- both of which ALWAYS raise. A genuine, deterministic,
    platform-independent "cannot write here" failure that never touches
    any real filesystem path (unlike the retired version of this test,
    which used a relative fake path that Windows resolved to a WRITABLE
    nested subdirectory of the project -- mkdir silently succeeded there,
    the log was genuinely written, and a stray file was left inside the
    real project tree on every run)."""

    @property
    def parent(self):
        return self

    def mkdir(self, *args, **kwargs):
        raise OSError("simulated: cannot create log directory (unwritable path)")

    def open(self, *args, **kwargs):
        raise OSError("simulated: cannot open log file (unwritable path)")


def build_delivery_ns(log_path: str, *, fail_send: bool = False, fail_edit: bool = False,
                      log_path_obj=None, fail_message: str = "simulated network failure") -> dict:
    nodes = _extract_by_name(DELIVERY_NAMES)
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "os": os,
        "datetime": datetime,
        # utcnow refactor (2026-08-16): the extracted _pb_utc_now() seam
        # calls datetime.now(timezone.utc) -- timezone must be in the ns.
        "timezone": __import__("datetime").timezone,
        "BASE_DIR": BASE_DIR,
        "_safe_text": lambda s, limit=3900: str(s or "")[:limit],
        "_back_to_panel_buttons": lambda: [],
        # TERMINAL OK 20260809 (Ф4 bot-wide audit): _panel_result_send/
        # _panel_update_status_or_send now also call _terminal_ok_button() --
        # this test only cares about logging behavior, so an empty-list
        # stand-in (same style as _back_to_panel_buttons above) is sufficient.
        "_terminal_ok_button": lambda: [],
        "client": _FakeClient(fail_send=fail_send, fail_edit=fail_edit, fail_message=fail_message),
    }
    exec(compile(module_src, f"<{PANEL_PATH}:delivery>", "exec"), ns)
    # Redirect the log file to a temp path (or an in-memory unwritable
    # fake -- see test_4) -- nothing written under the real (protected)
    # logs\ directory during this selftest.
    ns["_PANEL_DELIVERY_LOG_FILE"] = log_path_obj if log_path_obj is not None else Path(log_path)
    return ns


def _read_log_or_fail(log_path: str, label_prefix: str) -> str:
    """N5.4.6 (RF3): read the delivery-error log for an assertion, but
    never let a MISSING file crash the suite with an unrelated
    FileNotFoundError -- a missing file where this test expected one is
    ITSELF the product regression under test (e.g. mutation M6, reverting
    the observability fix), so it must surface as a clean, labeled [FAIL]
    on the intended semantic assertion, not abort the whole run and hide
    every check after it (this is exactly what used to happen: test_2's
    unconditional read_text() masked test_5's AST-scope guard)."""
    p = Path(log_path)
    if not p.exists():
        check(f"{label_prefix} delivery-error log file was created", False,
              f"log file does not exist: {log_path}")
        return ""
    return p.read_text(encoding="utf-8")


# ======================================================================
# 1. Success path: no log entry, unchanged delivered text/behavior.
# ======================================================================

def test_1_success_no_log_noise() -> None:
    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    os.unlink(log_path)  # absent until first failure -- must not exist yet
    try:
        ns = build_delivery_ns(log_path)
        asyncio.run(ns["_panel_result_send"](111, "Готово", "/manager_info mgr01"))
        check("1. successful delivery writes NOTHING to the delivery-error log",
              not Path(log_path).exists(), None)
        check("1. the message was actually delivered (unchanged behavior)",
              ns["client"].sent == [(111, "Готово")], ns["client"].sent)
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


# ======================================================================
# 2. Failure path (_panel_result_send): CONFIRMED OBSERVABILITY DEFECT
#    fixed -- a log line IS written; sanitized (verb only, no arguments --
#    a password/PIN argument must never appear).
# ======================================================================

def test_2_result_send_failure_is_logged_and_sanitized() -> None:
    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    os.unlink(log_path)
    try:
        ns = build_delivery_ns(log_path, fail_send=True)
        asyncio.run(ns["_panel_result_send"](222, "oops", "/manager_reset mgr01 SUPERSECRET confirm"))
        check("2. [FIX] a failed delivery now writes a log line (was silently swallowed before N5.4.4)",
              Path(log_path).exists(), None)
        content = _read_log_or_fail(log_path, "2.")
        check("2. log line identifies the stage (result_send)", "result_send" in content, content)
        check("2. log line carries the chat id", "chat=222" in content, content)
        check("2. log line carries the exception class", "RuntimeError" in content, content)
        check("2. [SANITIZED] the command's leading verb IS logged (operationally useful)",
              "/manager_reset" in content, content)
        check("2. [SANITIZED] the command's ARGUMENTS are NEVER logged (a password/PIN can be an argument)",
              "SUPERSECRET" not in content and "mgr01" not in content and "confirm" not in content, content)
        check("2. the swallow itself is unchanged -- the coroutine still completes without raising",
              True, None)  # asyncio.run above would have raised if it propagated
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


def test_2b_status_send_failure_logged_returns_zero() -> None:
    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    os.unlink(log_path)
    try:
        ns = build_delivery_ns(log_path, fail_send=True)
        result = asyncio.run(ns["_panel_status_send"](333, "⏳ ...", "/proxy_pool_check mgr02"))
        check("2b. [UNCHANGED BEHAVIOR] failed status_send still returns 0 (same as pre-N5.4.4)",
              result == 0, result)
        content = _read_log_or_fail(log_path, "2b.")
        check("2b. [FIX] failure is now logged (stage=status_send)", "status_send" in content, content)
        check("2b. [SANITIZED] only the verb is logged", "/proxy_pool_check" in content and "mgr02" not in content, content)
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


# ======================================================================
# 3. Failure path (_panel_update_status_or_send): edit fails -> logged,
#    AND the existing fallback-to-fresh-send control flow is UNCHANGED
#    (the message still reaches the admin via _panel_result_send).
# ======================================================================

def test_3_update_status_edit_failure_falls_back_unchanged() -> None:
    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    os.unlink(log_path)
    try:
        ns = build_delivery_ns(log_path, fail_edit=True)
        asyncio.run(ns["_panel_update_status_or_send"](444, 55, "hello", "/manager_replace_commit mgr03"))
        check("3. [UNCHANGED BEHAVIOR] a failed edit still falls back to a fresh send",
              ns["client"].sent == [(444, "hello")], ns["client"].sent)
        content = _read_log_or_fail(log_path, "3.")
        check("3. [FIX] the edit failure IS now logged (stage=update_status_or_send)",
              "update_status_or_send" in content, content)
        check("3. log line carries the message id (edit target)", "msg=55" in content, content)
        check("3. [SANITIZED] arguments not logged", "mgr03" not in content, content)
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


def test_3b_update_status_success_no_log() -> None:
    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    os.unlink(log_path)
    try:
        ns = build_delivery_ns(log_path)
        asyncio.run(ns["_panel_update_status_or_send"](444, 55, "hello", "/manager_info mgr01"))
        check("3b. successful edit writes nothing to the log", not Path(log_path).exists(), None)
        check("3b. the edit itself succeeded (no fallback send triggered)",
              ns["client"].edited == [(444, 55, "hello")] and ns["client"].sent == [], (ns["client"].edited, ns["client"].sent))
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


# ======================================================================
# 4. Logging itself must never raise / never break delivery, even if the
#    log directory cannot be created (e.g. a read-only filesystem).
# ======================================================================

def test_4_logging_failure_never_breaks_delivery() -> None:
    # N5.4.6 (RF2): control proof FIRST -- confirm the fake genuinely
    # raises when its mkdir is called directly, so a later green result
    # from the delivery function is real containment, not an accidental
    # no-op (the retired version of this test never exercised its
    # try/except at all -- see the docstring above).
    try:
        _UnwritablePath().parent.mkdir()
        check("4. [CONTROL] _UnwritablePath.mkdir() genuinely raises (sanity -- proves this is a real failure path)",
              False, "did not raise")
    except OSError:
        check("4. [CONTROL] _UnwritablePath.mkdir() genuinely raises (sanity -- proves this is a real failure path)",
              True, None)

    before = set(os.listdir(BASE_DIR))
    ns = build_delivery_ns("", fail_send=True, log_path_obj=_UnwritablePath())
    # _panel_result_send's own internal exception handler calls
    # _panel_log_delivery_error, whose OWN try/except must swallow the
    # mkdir/open failure against this genuinely unwritable fake -- the
    # coroutine must still complete normally (not raise).
    try:
        asyncio.run(ns["_panel_result_send"](555, "x", "/manager_info mgr01"))
        check("4. logging to a genuinely unwritable path never raises out of the delivery function", True, None)
    except Exception as exc:
        check("4. logging to a genuinely unwritable path never raises out of the delivery function", False, repr(exc))
    after = set(os.listdir(BASE_DIR))
    check("4. [NO CONTAMINATION] this test created NO new file/directory anywhere under the real project tree",
          after == before, sorted(after - before))


# ======================================================================
# 5. AST-level: the swallow points genuinely changed (not a no-op edit),
#    and no OTHER command's execution semantics were touched -- the fix
#    is scoped to exactly these 3 functions plus their call sites' extra
#    argument.
# ======================================================================

def test_5_scope_is_exactly_the_three_functions() -> None:
    for name in ("_panel_status_send", "_panel_result_send", "_panel_update_status_or_send"):
        defs = [n for n in TREE.body if getattr(n, "name", None) == name]
        check(f"5. {name} is single-def", len(defs) == 1, len(defs))
        if defs:
            src = ast.unparse(defs[0])
            check(f"5. {name} calls _panel_log_delivery_error on its exception path",
                  "_panel_log_delivery_error" in src, src[:300])
            check(f"5. {name} no longer has a bare 'except Exception: pass'-only handler with no logging",
                  not ("except Exception:" in src and "_panel_log_delivery_error" not in src), src[:300])

    log_fn = [n for n in TREE.body if getattr(n, "name", None) == "_panel_log_delivery_error"]
    check("5. _panel_log_delivery_error is single-def", len(log_fn) == 1, len(log_fn))
    if log_fn:
        src = ast.unparse(log_fn[0])
        check("5. [SANITIZED, AST-level] the logger sanitizes the WHOLE command text for control "
              "characters BEFORE splitting off the verb (RG1: split(None, 1) on the sanitized text, "
              "not a literal-space split on the raw command)",
              "_panel_sanitize_log_field(command" in src and "split(None, 1)" in src, src)
        check("5. [SANITIZED, AST-level] the logger only ever takes the FIRST token of the "
              "sanitized command as the logged verb",
              "verb_tokens[0]" in src, src)
        check("5. [SANITIZED, AST-level] the logger's own body never references the full command/args variable in the write",
              "command" not in src.split("verb =")[-1].split("line = (")[0] if "verb =" in src and "line = (" in src else True,
              src)


# ======================================================================
# 6. RF4 (N5.4.6, independent-review fix): control-character injection.
#    A dynamic field (the command's leading verb, or the sanitized
#    exception class name) must never be able to forge an extra physical
#    log line or corrupt the tab-delimited columns via an embedded
#    newline/CR/tab/NUL. Each incident must remain exactly one line.
# ======================================================================

def test_6_control_char_injection_cannot_forge_lines() -> None:
    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    os.unlink(log_path)
    try:
        ns = build_delivery_ns(log_path, fail_send=True)
        evil_command = "/manager_reset\nFAKE\tINJECTED\r\x00LINE mgr01 SUPERSECRET confirm"
        asyncio.run(ns["_panel_result_send"](666, "x", evil_command))
        content = _read_log_or_fail(log_path, "6.")
        lines = [ln for ln in content.split("\n") if ln.strip()]
        check("6. [NEWLINE] a \\n embedded in the command's leading token does not create a second log line",
              len(lines) == 1, lines)
        # NOTE: "\t" is deliberately excluded from this scan -- the log
        # format itself is tab-delimited BY DESIGN (stage/chat/msg/cmd/exc
        # columns), so a legitimate field separator must not be flagged as
        # injected content. Only \n/\r/NUL (which have no legitimate role
        # in this single-line format) are checked here.
        check("6. [NO RAW CONTROL CHARS] the persisted line contains no raw \\n/\\r/NUL",
              all(ch not in lines[0] for ch in ("\n", "\r", "\x00")) if lines else False, lines)
        # RG1 (2026-07-25, post-re-review fix) STRENGTHENS this beyond the
        # original guarantee: the whole command is now sanitized BEFORE the
        # verb is split off, so "FAKE" -- which sits between the leading
        # control-char injection and the real space before "mgr01" -- is no
        # longer folded into the logged verb at all; it is correctly
        # treated as part of the (dropped) arguments and never reaches the
        # log in any form.
        check("6. [RG1] the injected fake marker text does not reach the log AT ALL "
              "(neither as a structurally separate line nor folded into the verb)",
              (len(lines) == 1) and ("FAKE" not in content), content)
        check("6. [SECRETS] command arguments (incl. after the injected control chars) never reach the log",
              "SUPERSECRET" not in content and "confirm" not in content, content)
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


def test_7_exception_text_with_secrets_and_injection_never_logged() -> None:
    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    os.unlink(log_path)
    try:
        evil_exc_message = "leak: api_key=SUPERSECRET_TOKEN_XYZ\nFAKE INJECTED LOG LINE\tpassword=hunter2\x00trailing"
        ns = build_delivery_ns(log_path, fail_send=True, fail_message=evil_exc_message)
        asyncio.run(ns["_panel_result_send"](777, "x", "/manager_info mgr09"))
        content = _read_log_or_fail(log_path, "7.")
        lines = [ln for ln in content.split("\n") if ln.strip()]
        check("7. [RF4 DESIGN] the exception's own message text is dropped entirely -- only the "
              "sanitized class name (RuntimeError) is logged",
              "RuntimeError" in content, content)
        check("7. [SECRETS] the exception message's secret token never reaches the log",
              "SUPERSECRET_TOKEN_XYZ" not in content and "hunter2" not in content, content)
        check("7. [NO INJECTION] the exception message's embedded fake log line never reaches the log",
              "FAKE INJECTED LOG LINE" not in content, content)
        check("7. [ONE LINE] this single failure still produced exactly one physical log line",
              len(lines) == 1, lines)
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


# ======================================================================
# 8. RG1 (2026-07-25, post-re-review fix): robust verb extraction. A
#    control character (not just a literal ASCII space) sitting between
#    the verb and its arguments must never let the argument ride along
#    inside what gets logged as the "verb" -- confirmed residual defect
#    from the independent re-review (the old split(" ", 1) only
#    recognized a literal space as the verb/argument boundary, so a
#    tab/CR/NUL-glued argument survived sanitization intact, glued to the
#    verb, as one token).
# ======================================================================

def _log_content_for_command(evil_command: str) -> str:
    fd, log_path = tempfile.mkstemp(suffix=".log")
    os.close(fd)
    os.unlink(log_path)
    try:
        ns = build_delivery_ns(log_path, fail_send=True)
        asyncio.run(ns["_panel_result_send"](888, "x", evil_command))
        return _read_log_or_fail(log_path, "8.")
    finally:
        if os.path.exists(log_path):
            os.unlink(log_path)


def test_8_verb_extraction_survives_control_glued_arguments() -> None:
    cases = [
        ("/manager_info\nsecret_manager_key", "/manager_info", "secret_manager_key"),
        ("/proxy_set\thost:port:login:password", "/proxy_set", "host:port:login:password"),
        ("/login_code\r12345", "/login_code", "12345"),
        ("/command\x00secret", "/command", "secret"),
    ]
    for evil_command, expected_verb, secret_arg in cases:
        content = _log_content_for_command(evil_command)
        check(f"8. [RG1] {evil_command!r}: log contains ONLY the verb {expected_verb!r}",
              f"cmd={expected_verb}" in content, content)
        check(f"8. [RG1] {evil_command!r}: the control-glued argument {secret_arg!r} never reaches the log",
              secret_arg not in content, content)
        lines = [ln for ln in content.split("\n") if ln.strip()]
        check(f"8. [RG1] {evil_command!r}: exactly one physical log line",
              len(lines) == 1, lines)

    # 5. Leading and repeated whitespace/control characters -- the verb is
    # still isolated cleanly, no leading junk survives as part of it.
    content = _log_content_for_command("\n\t\r  /manager_stop\n\nmgr07  extra")
    check("8. [RG1] leading/repeated whitespace+control chars: verb isolated cleanly (cmd=/manager_stop)",
          "cmd=/manager_stop" in content, content)
    check("8. [RG1] leading/repeated whitespace+control chars: argument never reaches the log",
          "mgr07" not in content and "extra" not in content, content)

    # 6. An ordinary command with a normal space is completely unchanged.
    content = _log_content_for_command("/manager_reset mgr01 confirm")
    check("8. [RG1] ordinary space-delimited command: verb unchanged (cmd=/manager_reset)",
          "cmd=/manager_reset" in content, content)
    check("8. [RG1] ordinary space-delimited command: arguments still never logged",
          "mgr01" not in content and "confirm" not in content, content)


def main() -> int:
    test_1_success_no_log_noise()
    test_2_result_send_failure_is_logged_and_sanitized()
    test_2b_status_send_failure_logged_returns_zero()
    test_3_update_status_edit_failure_falls_back_unchanged()
    test_3b_update_status_success_no_log()
    test_4_logging_failure_never_breaks_delivery()
    test_5_scope_is_exactly_the_three_functions()
    test_6_control_char_injection_cannot_forge_lines()
    test_7_exception_text_with_secrets_and_injection_never_logged()
    test_8_verb_extraction_survives_control_glued_arguments()

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
