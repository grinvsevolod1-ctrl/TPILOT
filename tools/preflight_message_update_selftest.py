# -*- coding: utf-8 -*-
"""tools/preflight_message_update_selftest.py -- offline self-test for the
AUTO-RECHECK + MESSAGE CORRECTION patch (2026-07-12):

* panel_bot.py (AdminBot): bounded automatic re-check of a "problem" evening
  report (force=True deep verification, MAX 3 attempts, ~5 min apart) and
  in-place correction of the already-sent message once it resolves --
  edit_message -> (on failure) delete_messages+send_message -> (if delete
  also fails) exactly one new message titled "Обновлено: готовность
  перепроверена". Never spams once resolved=1 is set.
* partner_stat_bot.py (PartnerBot): reaction-only mirror -- PartnerBot NEVER
  triggers deep verification itself, it only reacts to whatever blob
  PanelBot has already written, strictly scoped to one uid/source at a time.

Business rules under test (see preflight_check.report_is_ok /
report_has_recheckable_problem, already covered in isolation by
tools/preflight_readiness_selftest.py -- this file tests the MESSAGE-
CORRECTION ORCHESTRATION built on top of those pure helpers).

Techniques: panel_bot.py/partner_stat_bot.py cannot be imported standalone
(Telethon/env side effects at import time) -- functions under test are
extracted via ast.parse + ast.unparse + exec(), the same technique already
used by tools/proxy_pool_selftest.py and friends. preflight_check.py IS
directly importable (no side effects) -- report_is_ok/apply_correction_note/
the correction-note constants are imported and used FOR REAL, not faked, so
this test also locks in the exact Russian wording the requirements specify.
Telegram client calls (edit_message/delete_messages/send_message) and the
settings KV are faked in-memory; no real DB/network/Telegram.

    python3.12 tools\\preflight_message_update_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import preflight_check as pf

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


async def _drain() -> None:
    """Let any asyncio.create_task()-scheduled background task run to
    completion before the test inspects its side effects."""
    for _ in range(5):
        await asyncio.sleep(0)


# ======================================================================
# AST extraction (panel_bot.py / partner_stat_bot.py cannot be imported
# standalone). Handles plain function defs AND simple top-level
# Assign/AnnAssign constants, so both functions and the small
# _PF_RECHECK_* / _PF_EVE_*_DEADLINE_* constants can be pulled in.
# ======================================================================

def _extract_and_exec(path: str, names: set, extra_ns: dict) -> dict:
    src = open(path, encoding="utf-8-sig").read()
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
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in names:
            nodes.append(n)
            seen.add(n.target.id)
            continue
    missing = names - seen
    if missing:
        raise AssertionError(f"expected {names}, missing {missing} in {path}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path}>", "exec"), ns)
    return ns


PANEL_PATH = str(BASE_DIR / "panel_bot.py")
PARTNER_PATH = str(BASE_DIR / "partner_stat_bot.py")

# real, trivial key-format helper -- extracted once so the ensure-verify
# test below never has to hand-duplicate the settings-key format string.
_pf_verify_blob_key_real = _extract_and_exec(PANEL_PATH, {"_pf_verify_blob_key"}, {})["_pf_verify_blob_key"]


# ======================================================================
# Shared fakes
# ======================================================================

class FakeKV:
    def __init__(self):
        self.store: dict = {}

    def get(self, key: str) -> str:
        return self.store.get(str(key), "")

    def set(self, key: str, value: str) -> None:
        self.store[str(key)] = str(value)


class _FakeButton:
    @staticmethod
    def inline(text, data):
        return ("btn", text, data)


class _FakeMessage:
    def __init__(self, mid: int):
        self.id = mid


class FakeClient:
    """Records every edit_message/delete_messages/send_message call. Each
    op can be made to raise on demand (edit_fail/delete_fail/send_fail) to
    exercise the edit -> delete+send -> new-message fallback chain."""

    def __init__(self):
        self.calls: list = []
        self._next_id = 9000
        self.edit_fail = False
        self.delete_fail = False
        self.send_fail = False

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        self.calls.append({"op": "edit", "chat_id": int(chat_id), "mid": int(message_id), "text": text})
        if self.edit_fail:
            raise RuntimeError("edit failed (simulated)")

    async def delete_messages(self, chat_id, ids):
        self.calls.append({"op": "delete", "chat_id": int(chat_id), "ids": list(ids)})
        if self.delete_fail:
            raise RuntimeError("delete failed (simulated)")

    async def send_message(self, chat_id, text, buttons=None):
        self.calls.append({"op": "send", "chat_id": int(chat_id), "text": text})
        if self.send_fail:
            raise RuntimeError("send failed (simulated)")
        self._next_id += 1
        return _FakeMessage(self._next_id)


class VerifyRecorder:
    def __init__(self):
        self.calls: list = []

    async def fake_ensure(self, period, date_iso, deadline_ts, *, force=False):
        self.calls.append({"period": period, "date": date_iso, "force": force})


class ReportSource:
    def __init__(self, report: dict):
        self.report = report


# ======================================================================
# panel_bot.py (AdminBot) orchestration extraction
# ======================================================================

PANEL_ORCH_NAMES = {
    "_safe_text",
    "_pf_deadline_loop_time",
    "_PF_RECHECK_MAX_ATTEMPTS",
    "_PF_RECHECK_INTERVAL_SEC",
    "_pf_eve_status_key",
    "_pf_eve_recheck_key",
    "_pf_eve_recheck_ts_key",
    "_pf_eve_resolved_key",
    "_pf_correct_message",
    "_pf_admin_apply_correction",
    "_pf_admin_evening_maybe_correct",
    "_pf_send_admin_evening_report",
}


def build_panel_ns(report_source: ReportSource, kv: FakeKV, client: FakeClient, verify_recorder: VerifyRecorder) -> dict:
    return _extract_and_exec(
        PANEL_PATH,
        PANEL_ORCH_NAMES,
        {
            "asyncio": asyncio,
            "datetime": datetime,
            "Dict": dict, "Any": object, "Optional": object, "Tuple": tuple, "List": list,
            "client": client,
            "Button": _FakeButton,
            "_pb_get_setting": kv.get,
            "_pf_set_setting": kv.set,
            "_pf_build_report": lambda today_iso=None, db_path=None: dict(report_source.report),
            "_pf_read_verify_blob": lambda period, date_iso: {"target_date": date_iso},
            "_pf_apply_deep_results": lambda report, blob, period_kind=None: report,
            "_pf_render_admin_text": lambda report, period_kind=None: (
                f"ADMIN_REPORT status={report.get('deep_status_global')} all_ok={report.get('all_ok')}"
            ),
            "_pf_report_is_ok": pf.report_is_ok,
            "_pf_report_has_recheckable_problem": pf.report_has_recheckable_problem,
            "_pf_apply_correction_note": pf.apply_correction_note,
            "_PF_CORRECTION_NOTE_ADMIN": pf.CORRECTION_NOTE_ADMIN,
            "_PF_CORRECTED_TITLE_ADMIN": pf.CORRECTED_TITLE_ADMIN,
            "_pf_ensure_deep_verification": verify_recorder.fake_ensure,
            "TPILOT_DB_PATH": "fake.db",
            "_PF_EVE_VERIFY_DEADLINE_HOUR": 16,
            "_PF_EVE_VERIFY_DEADLINE_MINUTE": 48,
        },
    )


PROBLEM_TIMEOUT_REPORT = {"all_ok": True, "deep_status_global": "timeout", "deep_expected_total": 15}
OK_REPORT = {"all_ok": True, "deep_status_global": "ok", "deep_expected_total": 15}
STRUCTURAL_PROBLEM_REPORT = {"all_ok": False, "deep_status_global": "not_checked", "deep_expected_total": 0}

CHAT_ID = 555111
DATE_ISO = "2026-07-13"


async def test_1_admin_initial_problem_send() -> None:
    kv, client, verify = FakeKV(), FakeClient(), VerifyRecorder()
    src = ReportSource(dict(PROBLEM_TIMEOUT_REPORT))
    ns = build_panel_ns(src, kv, client, verify)

    await ns["_pf_send_admin_evening_report"](CHAT_ID, DATE_ISO)

    check("1. initial problem report: sent flag stored", kv.get(f"preflight_admin_evening_sent_{DATE_ISO}_{CHAT_ID}") == "1")
    check("1. initial problem report: message_id stored", kv.get(f"preflight_msg_admin_evening_{CHAT_ID}").startswith(f"{DATE_ISO}:"))
    check("1. initial problem report: status stored as problem", kv.get(ns["_pf_eve_status_key"](DATE_ISO, CHAT_ID)) == "problem")
    check("1. initial problem report: resolved flag NOT set", kv.get(ns["_pf_eve_resolved_key"](DATE_ISO, CHAT_ID)) != "1")
    check("1. initial problem report: exactly one send, no edit/delete", [c["op"] for c in client.calls] == ["send"])


async def test_2_retry_resolves_edit_in_place() -> None:
    kv, client, verify = FakeKV(), FakeClient(), VerifyRecorder()
    src = ReportSource(dict(PROBLEM_TIMEOUT_REPORT))
    ns = build_panel_ns(src, kv, client, verify)

    await ns["_pf_send_admin_evening_report"](CHAT_ID, DATE_ISO)
    client.calls.clear()

    now1 = datetime(2026, 7, 12, 16, 55, 0)
    await ns["_pf_admin_evening_maybe_correct"](CHAT_ID, DATE_ISO, now1)
    check("2. tick with still-timeout report triggers a force re-check", len(verify.calls) == 1 and verify.calls[0]["force"] is True)
    check("2. tick with still-timeout report does not touch the message yet", client.calls == [])
    check("2. tick with still-timeout report does not resolve yet", kv.get(ns["_pf_eve_resolved_key"](DATE_ISO, CHAT_ID)) != "1")

    # simulate the background force-verify having resolved the blob, and
    # enough time passing for the next tick.
    src.report = dict(OK_REPORT)
    now2 = datetime(2026, 7, 12, 17, 1, 0)
    await ns["_pf_admin_evening_maybe_correct"](CHAT_ID, DATE_ISO, now2)

    check("2. resolved tick edits the existing message in place", [c["op"] for c in client.calls] == ["edit"], repr(client.calls))
    edit_text = client.calls[0]["text"] if client.calls else ""
    check("2. correction text mentions 'Неполадки устранены'", "Неполадки устранены" in edit_text, edit_text)
    check("2. correction text mentions 'Готовность перепроверена'", "Готовность перепроверена" in edit_text, edit_text)
    check("2. no new message sent (only 1 edit call total)", len(client.calls) == 1)
    check("2. resolved flag set", kv.get(ns["_pf_eve_resolved_key"](DATE_ISO, CHAT_ID)) == "1")
    check("2. status flipped to ok", kv.get(ns["_pf_eve_status_key"](DATE_ISO, CHAT_ID)) == "ok")


async def test_3_edit_failure_falls_back_to_delete_send() -> None:
    kv, client, verify = FakeKV(), FakeClient(), VerifyRecorder()
    src = ReportSource(dict(PROBLEM_TIMEOUT_REPORT))
    ns = build_panel_ns(src, kv, client, verify)
    await ns["_pf_send_admin_evening_report"](CHAT_ID, DATE_ISO)
    client.calls.clear()

    client.edit_fail = True
    src.report = dict(OK_REPORT)
    await ns["_pf_admin_evening_maybe_correct"](CHAT_ID, DATE_ISO, datetime(2026, 7, 12, 17, 5, 0))

    ops = [c["op"] for c in client.calls]
    check("3. edit failure -> delete then send (exactly one send)", ops == ["edit", "delete", "send"], repr(ops))
    check("3. corrected text (not the '...Обновлено...' fallback title) used for send-after-delete", "Обновлено" not in client.calls[-1]["text"], client.calls[-1]["text"])
    check("3. resolved flag set after successful fallback", kv.get(ns["_pf_eve_resolved_key"](DATE_ISO, CHAT_ID)) == "1")
    new_mid = client.calls[-1] and client._next_id
    check("3. stored message_id updated to the new message", kv.get(f"preflight_msg_admin_evening_{CHAT_ID}") == f"{DATE_ISO}:{new_mid}")


async def test_4_edit_and_delete_failure_single_titled_send_no_spam() -> None:
    kv, client, verify = FakeKV(), FakeClient(), VerifyRecorder()
    src = ReportSource(dict(PROBLEM_TIMEOUT_REPORT))
    ns = build_panel_ns(src, kv, client, verify)
    await ns["_pf_send_admin_evening_report"](CHAT_ID, DATE_ISO)
    client.calls.clear()

    client.edit_fail = True
    client.delete_fail = True
    src.report = dict(OK_REPORT)
    await ns["_pf_admin_evening_maybe_correct"](CHAT_ID, DATE_ISO, datetime(2026, 7, 12, 17, 5, 0))

    ops = [c["op"] for c in client.calls]
    check("4. edit+delete failure -> exactly one new send, no further ops", ops == ["edit", "delete", "send"], repr(ops))
    sends = [c for c in client.calls if c["op"] == "send"]
    check("4. exactly one send call", len(sends) == 1)
    check("4. fallback send titled 'Обновлено: готовность перепроверена'", "Обновлено: готовность перепроверена" in sends[0]["text"], sends[0]["text"])
    check("4. resolved flag set", kv.get(ns["_pf_eve_resolved_key"](DATE_ISO, CHAT_ID)) == "1")

    # a later tick must be a total no-op -- no spam.
    client.calls.clear()
    await ns["_pf_admin_evening_maybe_correct"](CHAT_ID, DATE_ISO, datetime(2026, 7, 12, 17, 20, 0))
    check("4. later tick after resolved=1 makes no further calls (no spam)", client.calls == [])


async def test_6_retry_is_bounded() -> None:
    kv, client, verify = FakeKV(), FakeClient(), VerifyRecorder()
    src = ReportSource(dict(PROBLEM_TIMEOUT_REPORT))  # never resolves in this test
    ns = build_panel_ns(src, kv, client, verify)
    await ns["_pf_send_admin_evening_report"](CHAT_ID, DATE_ISO)

    base = datetime(2026, 7, 12, 17, 0, 0)
    for i in range(6):  # far more ticks than MAX_ATTEMPTS
        tick_time = datetime(base.year, base.month, base.day, base.hour, base.minute + i * 6, 0)
        await ns["_pf_admin_evening_maybe_correct"](CHAT_ID, DATE_ISO, tick_time)

    check("6. force-verify is called at most MAX_ATTEMPTS (3) times, never endlessly", len(verify.calls) == 3, repr(verify.calls))
    check("6. still not resolved (report never actually recovered)", kv.get(ns["_pf_eve_resolved_key"](DATE_ISO, CHAT_ID)) != "1")
    check("6. still marked as a problem", kv.get(ns["_pf_eve_status_key"](DATE_ISO, CHAT_ID)) == "problem")


async def test_structural_problem_never_retried() -> None:
    """Not one of the 8 numbered cases, but a direct check of the 'do NOT
    retry forever' requirement for a genuinely structural (non-transient)
    problem -- all_ok=False must never trigger a force re-check."""
    kv, client, verify = FakeKV(), FakeClient(), VerifyRecorder()
    src = ReportSource(dict(STRUCTURAL_PROBLEM_REPORT))
    ns = build_panel_ns(src, kv, client, verify)
    await ns["_pf_send_admin_evening_report"](CHAT_ID, DATE_ISO)
    await ns["_pf_admin_evening_maybe_correct"](CHAT_ID, DATE_ISO, datetime(2026, 7, 12, 17, 30, 0))
    check("(extra) structural (all_ok=False) problem never triggers a force re-check", verify.calls == [])
    check("(extra) structural problem is never auto-corrected", client.calls == [c for c in client.calls if c["op"] == "send"] and len(client.calls) == 1)


async def test_7_force_overwrites_only_same_kind_date_blob() -> None:
    kv = FakeKV()
    run_calls: list = []

    async def fake_run_deep(date_iso, db_path, period, deadline_ts):
        run_calls.append((period, date_iso))
        return {"target_date": date_iso, "run_count": len(run_calls)}

    def fake_read_blob(period, date_iso):
        raw = kv.get(_pf_verify_blob_key_real(period, date_iso))
        return json.loads(raw) if raw else None

    ns = _extract_and_exec(
        PANEL_PATH,
        # R1B/F-18/F-40 (2026-08-12, large reliability batch): every
        # `asyncio.create_task(...)` call site in panel_bot.py -- including
        # inside _pf_ensure_deep_verification -- was mechanically rewritten
        # to `_pb_track_task(...)` (strong reference + exception retrieval).
        {"_PF_VERIFY_IN_PROGRESS", "_pf_verify_blob_key", "_pf_ensure_deep_verification", "_pb_track_task"},
        {
            "asyncio": asyncio, "json": json, "print": print,
            "Dict": dict, "Optional": object,
            "_PF_STAGE1_OK": True,
            "_PF_DEEP_VERIFY_AVAILABLE": True,
            "_pf_run_deep_verification": fake_run_deep,
            "_pf_read_verify_blob": fake_read_blob,
            "_pf_set_setting": kv.set,
            "TPILOT_DB_PATH": "fake.db",
            "_PANEL_BACKGROUND_TASKS": set(),
        },
    )
    ensure = ns["_pf_ensure_deep_verification"]
    key = ns["_pf_verify_blob_key"]

    kv.set(key("evening", DATE_ISO), json.dumps({"target_date": DATE_ISO, "run_count": 0}))
    kv.set(key("morning", DATE_ISO), json.dumps({"target_date": DATE_ISO, "run_count": -1}))

    await ensure("evening", DATE_ISO, 0.0, force=False)
    await _drain()
    check("7. force=False short-circuits when a blob already exists (no rerun)", run_calls == [])

    await ensure("evening", DATE_ISO, 0.0, force=True)
    await _drain()
    check("7. force=True triggers a real run_deep_verification call", run_calls == [("evening", DATE_ISO)], repr(run_calls))

    fresh = json.loads(kv.get(key("evening", DATE_ISO)))
    check("7. force=True overwrites the same (kind,date) blob with the fresh result", fresh.get("run_count") == 1, repr(fresh))

    other = json.loads(kv.get(key("morning", DATE_ISO)))
    check("7. force=True never touches a different kind's blob for the same date", other.get("run_count") == -1, repr(other))


# ======================================================================
# partner_stat_bot.py (PartnerBot) reaction-only, source-scoped extraction
# ======================================================================

PARTNER_NAMES = {
    "_pf_partner_status_key",
    "_pf_partner_resolved_key",
    "_pf_correct_partner_message",
    "_pf_partner_maybe_correct",
    "_pf_send_partner_report",
}


def build_partner_ns(kv: FakeKV, client: FakeClient) -> dict:
    return _extract_and_exec(
        PARTNER_PATH,
        PARTNER_NAMES,
        {
            "Dict": dict, "Any": object, "Optional": object,
            "client": client,
            "Button": _FakeButton,
            "_pf_get_setting": kv.get,
            "_pf_set_setting": kv.set,
            "_pf_render_partner_text": lambda report, period_kind=None: (
                f"PARTNER_REPORT status={report.get('deep_status_global')} all_ok={report.get('all_ok')}"
            ),
            "_pf_report_is_ok": pf.report_is_ok,
            "_pf_apply_correction_note": pf.apply_correction_note,
            "_PF_CORRECTION_NOTE_PARTNER": pf.CORRECTION_NOTE_PARTNER,
            "_PF_CORRECTED_TITLE": pf.CORRECTED_TITLE_ADMIN,
        },
    )


async def test_5_partner_correction_is_source_scoped() -> None:
    kv, client = FakeKV(), FakeClient()
    ns = build_partner_ns(kv, client)

    uid_a, uid_b = 111, 222
    report_a_problem = {"all_ok": True, "deep_status_global": "timeout", "deep_expected_total": 15}
    report_b_problem = {"all_ok": True, "deep_status_global": "timeout", "deep_expected_total": 8}

    text_a = "PARTNER_REPORT status=timeout"
    text_b = "PARTNER_REPORT status=timeout"
    await ns["_pf_send_partner_report"](uid_a, DATE_ISO, "tomorrow", text_a, report_a_problem)
    await ns["_pf_send_partner_report"](uid_b, DATE_ISO, "tomorrow", text_b, report_b_problem)
    client.calls.clear()

    # source A resolves this tick, source B does not.
    report_a_ok = {"all_ok": True, "deep_status_global": "ok", "deep_expected_total": 15}
    report_b_still_problem = dict(report_b_problem)

    await ns["_pf_partner_maybe_correct"](uid_a, "src_a", DATE_ISO, "tomorrow", report_a_ok)
    await ns["_pf_partner_maybe_correct"](uid_b, "src_b", DATE_ISO, "tomorrow", report_b_still_problem)

    a_calls = [c for c in client.calls if c["chat_id"] == uid_a]
    b_calls = [c for c in client.calls if c["chat_id"] == uid_b]
    check("5. resolved source's uid gets corrected (edit call)", [c["op"] for c in a_calls] == ["edit"], repr(a_calls))
    check("5. correction text has the partner note", "Готовность перепроверена" in (a_calls[0]["text"] if a_calls else ""), a_calls)
    check("5. unrelated (still-problem) source/uid is completely untouched", b_calls == [], repr(b_calls))
    check("5. resolved uid marked resolved", kv.get(ns["_pf_partner_resolved_key"]("tomorrow", DATE_ISO, uid_a)) == "1")
    check("5. unrelated uid NOT marked resolved", kv.get(ns["_pf_partner_resolved_key"]("tomorrow", DATE_ISO, uid_b)) != "1")
    check("5. unrelated uid status still problem", kv.get(ns["_pf_partner_status_key"]("tomorrow", DATE_ISO, uid_b)) == "problem")


async def test_partner_stale_date_cleanup() -> None:
    """Regression guard for _pf_send_partner_report's own (already-deployed,
    pre-existing) prev-date cleanup -- if the stored preflight_msg_partner_
    {kind}_{uid} entry belongs to a DIFFERENT date, the old message is
    deleted before the new one is sent; an unrelated uid's own stored
    message must never be touched by another uid's send."""
    kv, client = FakeKV(), FakeClient()
    ns = build_partner_ns(kv, client)

    uid_stale, uid_other = 333, 444
    old_iso = "2026-07-10"
    kv.set(f"preflight_msg_partner_tomorrow_{uid_stale}", f"{old_iso}:8181")
    kv.set(f"preflight_msg_partner_tomorrow_{uid_other}", f"{old_iso}:9191")

    report_ok = {"all_ok": True, "deep_status_global": "ok", "deep_expected_total": 5}
    await ns["_pf_send_partner_report"](uid_stale, DATE_ISO, "tomorrow", "PARTNER_REPORT new", report_ok)

    stale_calls = [c for c in client.calls if c["chat_id"] == uid_stale]
    other_calls = [c for c in client.calls if c["chat_id"] == uid_other]
    check("partner-cleanup. stale prev-date message (mid=8181) is deleted before the new send", any(c["op"] == "delete" and c.get("ids") == [8181] for c in stale_calls), repr(stale_calls))
    check("partner-cleanup. exactly one new send follows the stale delete", [c["op"] for c in stale_calls] == ["delete", "send"], repr(stale_calls))
    check("partner-cleanup. message_id key now points to the new date", kv.get(f"preflight_msg_partner_tomorrow_{uid_stale}").startswith(f"{DATE_ISO}:"))
    check("partner-cleanup. unrelated uid's own stored message is completely untouched", other_calls == [], repr(other_calls))
    check("partner-cleanup. unrelated uid's stored ref is unchanged", kv.get(f"preflight_msg_partner_tomorrow_{uid_other}") == f"{old_iso}:9191")


# ======================================================================
# 20260713: PartnerBot defer-if-verify-pending (Fix 2) -- _pf_partner_tick
# itself, extracted and run FOR REAL, so the exact deadline/report_is_ok/
# report_has_recheckable_problem gating is exercised end-to-end, not
# re-implemented in the test.
# ======================================================================

PARTNER_TICK_NAMES = {
    "_PF_MORNING_DEFER_HOUR_DEADLINE", "_PF_MORNING_DEFER_MINUTE_DEADLINE",
    "_PF_EVENING_DEFER_HOUR_DEADLINE", "_PF_EVENING_DEFER_MINUTE_DEADLINE",
    "_pf_partner_status_key", "_pf_partner_resolved_key",
    "_pf_correct_partner_message", "_pf_partner_maybe_correct",
    "_pf_send_partner_report", "_pf_partner_tick",
}


def build_partner_tick_ns(kv: FakeKV, client: FakeClient, report_source, buyers: list, now_dt: datetime) -> dict:
    return _extract_and_exec(
        PARTNER_PATH,
        PARTNER_TICK_NAMES,
        {
            "Dict": dict, "Any": object, "Optional": object, "List": list,
            "timedelta": timedelta,
            "client": client,
            "Button": _FakeButton,
            "_pf_get_setting": kv.get,
            "_pf_set_setting": kv.set,
            "_pf_render_partner_text": lambda report, period_kind=None: (
                f"PARTNER_REPORT status={report.get('deep_status_global')} all_ok={report.get('all_ok')}"
            ),
            "_pf_report_is_ok": pf.report_is_ok,
            "_pf_report_has_recheckable_problem": pf.report_has_recheckable_problem,
            "_pf_apply_correction_note": pf.apply_correction_note,
            "_PF_CORRECTION_NOTE_PARTNER": pf.CORRECTION_NOTE_PARTNER,
            "_PF_CORRECTED_TITLE": pf.CORRECTED_TITLE_ADMIN,
            "_PF_PARTNER_OK": True,
            "_kyiv_now": (lambda: now_dt),
            "_enabled_buyers": (lambda: list(buyers)),
            "_norm_key": (lambda raw: str(raw or "").strip().lower()),
            "_pf_relevant_source_keys_for_date": (lambda date_iso, db_path: set()),
            "_pf_build_partner_report": (lambda date_iso, db_path, sk: dict(report_source.report)),
            "_pf_read_verify_blob": (lambda period, date_iso: {"target_date": date_iso}),
            "_pf_apply_deep_results": (lambda report, blob, period_kind=None: report),
            "TPILOT_DB_PATH": "fake.db",
        },
    )


PARTNER_TICK_UID = 555
PARTNER_TICK_SK = "rassylka"
# _pf_partner_tick("tomorrow") targets (now.date() + 1 day) -- "now" must be
# the day BEFORE DATE_ISO so the resulting target_date_iso equals DATE_ISO.
_NOW_BASE_DATE = datetime.strptime(DATE_ISO, "%Y-%m-%d").date() - timedelta(days=1)
TIMEOUT_RECHECKABLE_REPORT = {"send_recommended": True, "all_ok": True, "deep_status_global": "timeout", "deep_expected_total": 3}
OK_PARTNER_REPORT = {"send_recommended": True, "all_ok": True, "deep_status_global": "ok", "deep_expected_total": 3}
STRUCTURAL_PARTNER_REPORT = {"send_recommended": True, "all_ok": False, "deep_status_global": "not_checked", "deep_expected_total": 0}


async def test_partner_defer_timeout_before_deadline() -> None:
    kv, client = FakeKV(), FakeClient()
    src = ReportSource(dict(TIMEOUT_RECHECKABLE_REPORT))
    buyers = [{"source_key": PARTNER_TICK_SK, "user_id": PARTNER_TICK_UID}]
    now_before_deadline = datetime(_NOW_BASE_DATE.year, _NOW_BASE_DATE.month, _NOW_BASE_DATE.day, 18, 0, 0)  # evening deadline is 18:30
    ns = build_partner_tick_ns(kv, client, src, buyers, now_before_deadline)

    await ns["_pf_partner_tick"]("tomorrow")

    check("defer. timeout-only report before the deadline is NOT sent yet", client.calls == [], repr(client.calls))
    check("defer. sent-flag is NOT set while deferred", kv.get(f"preflight_partner_tomorrow_sent_{DATE_ISO}_{PARTNER_TICK_UID}") != "1")


async def test_partner_defer_timeout_after_deadline() -> None:
    kv, client = FakeKV(), FakeClient()
    src = ReportSource(dict(TIMEOUT_RECHECKABLE_REPORT))
    buyers = [{"source_key": PARTNER_TICK_SK, "user_id": PARTNER_TICK_UID}]
    now_after_deadline = datetime(_NOW_BASE_DATE.year, _NOW_BASE_DATE.month, _NOW_BASE_DATE.day, 18, 45, 0)  # past the 18:30 evening deadline
    ns = build_partner_tick_ns(kv, client, src, buyers, now_after_deadline)

    await ns["_pf_partner_tick"]("tomorrow")

    check("defer. timeout-only report AFTER the deadline is sent as-is (exactly one send)", [c["op"] for c in client.calls] == ["send"], repr(client.calls))
    check("defer. sent text uses the neutral (non-technical) wording, never the raw timeout phrase", "не успели пройти проверку" not in client.calls[0]["text"], client.calls[0]["text"])


async def test_partner_ok_report_never_deferred() -> None:
    kv, client = FakeKV(), FakeClient()
    src = ReportSource(dict(OK_PARTNER_REPORT))
    buyers = [{"source_key": PARTNER_TICK_SK, "user_id": PARTNER_TICK_UID}]
    now_well_before_deadline = datetime(_NOW_BASE_DATE.year, _NOW_BASE_DATE.month, _NOW_BASE_DATE.day, 16, 50, 0)
    ns = build_partner_tick_ns(kv, client, src, buyers, now_well_before_deadline)

    await ns["_pf_partner_tick"]("tomorrow")

    check("defer. a genuinely OK report is sent immediately, never deferred", [c["op"] for c in client.calls] == ["send"], repr(client.calls))


async def test_partner_structural_problem_never_deferred() -> None:
    kv, client = FakeKV(), FakeClient()
    src = ReportSource(dict(STRUCTURAL_PARTNER_REPORT))
    buyers = [{"source_key": PARTNER_TICK_SK, "user_id": PARTNER_TICK_UID}]
    now_well_before_deadline = datetime(_NOW_BASE_DATE.year, _NOW_BASE_DATE.month, _NOW_BASE_DATE.day, 16, 50, 0)
    ns = build_partner_tick_ns(kv, client, src, buyers, now_well_before_deadline)

    await ns["_pf_partner_tick"]("tomorrow")

    check("defer. a real structural problem (all_ok=False) is sent immediately, never deferred", [c["op"] for c in client.calls] == ["send"], repr(client.calls))


async def test_partner_already_sent_uid_not_deferred() -> None:
    kv, client = FakeKV(), FakeClient()
    src = ReportSource(dict(TIMEOUT_RECHECKABLE_REPORT))
    buyers = [{"source_key": PARTNER_TICK_SK, "user_id": PARTNER_TICK_UID}]
    kv.set(f"preflight_partner_tomorrow_sent_{DATE_ISO}_{PARTNER_TICK_UID}", "1")
    kv.set(f"preflight_msg_partner_tomorrow_{PARTNER_TICK_UID}", f"{DATE_ISO}:7171")
    kv.set(f"preflight_partner_status_tomorrow_{DATE_ISO}_{PARTNER_TICK_UID}", "problem")
    now_before_deadline = datetime(_NOW_BASE_DATE.year, _NOW_BASE_DATE.month, _NOW_BASE_DATE.day, 18, 0, 0)

    src.report = dict(OK_PARTNER_REPORT)  # resolved by this tick
    ns = build_partner_tick_ns(kv, client, src, buyers, now_before_deadline)

    await ns["_pf_partner_tick"]("tomorrow")

    check("defer. an already-sent uid is corrected on the SAME tick even though a first-send would still be deferred", [c["op"] for c in client.calls] == ["edit"], repr(client.calls))


async def test_8_valeria0863_end_to_end() -> None:
    # (a) timeout + DB already has expected links AT the first verify --
    #     run_deep_verification's own DB-evidence fallback (tested in
    #     preflight_readiness_selftest.py) already resolved deep_status_
    #     global to "ok" BEFORE the first send -- so the first admin send
    #     is already fully OK, nothing is ever marked a problem.
    kv_a, client_a, verify_a = FakeKV(), FakeClient(), VerifyRecorder()
    src_a = ReportSource({"all_ok": True, "deep_status_global": "ok", "deep_expected_total": 15})
    ns_a = build_panel_ns(src_a, kv_a, client_a, verify_a)
    await ns_a["_pf_send_admin_evening_report"](CHAT_ID, DATE_ISO)
    check("8a. valeria0863 timeout+DB-evidence resolved at first verify -> first send already ok", kv_a.get(ns_a["_pf_eve_status_key"](DATE_ISO, CHAT_ID)) == "ok")
    check("8a. valeria0863 first-send-ok case is marked resolved immediately (nothing to correct, ever)", kv_a.get(ns_a["_pf_eve_resolved_key"](DATE_ISO, CHAT_ID)) == "1")
    await ns_a["_pf_admin_evening_maybe_correct"](CHAT_ID, DATE_ISO, datetime(2026, 7, 12, 17, 30, 0))
    check("8a. later ticks are a no-op (never touch an already-ok report)", client_a.calls == [c for c in client_a.calls if c["op"] == "send"] and len(client_a.calls) == 1)

    # (b) timeout FIRST (manager runtime did not answer in time and the DB
    #     did not yet have enough links either) -- old message stays a
    #     "problem"; a LATER recheck sees the links have since been created
    #     -> the old message is corrected in place.
    kv_b, client_b, verify_b = FakeKV(), FakeClient(), VerifyRecorder()
    src_b = ReportSource({"all_ok": True, "deep_status_global": "timeout", "deep_expected_total": 15})
    ns_b = build_panel_ns(src_b, kv_b, client_b, verify_b)
    await ns_b["_pf_send_admin_evening_report"](CHAT_ID, DATE_ISO)
    check("8b. valeria0863 timeout-first send is stored as a problem", kv_b.get(ns_b["_pf_eve_status_key"](DATE_ISO, CHAT_ID)) == "problem")
    client_b.calls.clear()

    src_b.report = {"all_ok": True, "deep_status_global": "ok", "deep_expected_total": 15}
    await ns_b["_pf_admin_evening_maybe_correct"](CHAT_ID, DATE_ISO, datetime(2026, 7, 12, 17, 5, 0))
    check("8b. valeria0863 later recheck (links now created) corrects the old message in place", [c["op"] for c in client_b.calls] == ["edit"], repr(client_b.calls))
    check("8b. valeria0863 corrected message resolved, no duplicate send", kv_b.get(ns_b["_pf_eve_resolved_key"](DATE_ISO, CHAT_ID)) == "1")


async def main() -> int:
    await test_1_admin_initial_problem_send()
    await test_2_retry_resolves_edit_in_place()
    await test_3_edit_failure_falls_back_to_delete_send()
    await test_4_edit_and_delete_failure_single_titled_send_no_spam()
    await test_5_partner_correction_is_source_scoped()
    await test_partner_stale_date_cleanup()
    await test_partner_defer_timeout_before_deadline()
    await test_partner_defer_timeout_after_deadline()
    await test_partner_ok_report_never_deferred()
    await test_partner_structural_problem_never_deferred()
    await test_partner_already_sent_uid_not_deferred()
    await test_6_retry_is_bounded()
    await test_7_force_overwrites_only_same_kind_date_blob()
    await test_8_valeria0863_end_to_end()
    await test_structural_problem_never_retried()

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
