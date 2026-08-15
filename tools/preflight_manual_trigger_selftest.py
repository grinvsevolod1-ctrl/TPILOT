# -*- coding: utf-8 -*-
"""tools/preflight_manual_trigger_selftest.py -- offline self-test for the
AdminBot-native manual preflight trigger (2026-07-13, extended same day for
today/tomorrow split): TWO buttons -- "🛡 Проверить сегодня"
(pf:manual_check_today) and "🛡 Проверить завтра" (pf:manual_check_tomorrow)
-- plus the original "pf:manual_check" kept as a permanent alias for
"tomorrow" so any already-rendered old button keeps working.

Context: an ad-hoc EXTERNAL script was previously used to force a readiness
check, sent a wrong "Утренняя проверка" message at night, and used unstable
direct calls/signatures. This patch replaces that with a native AdminBot
callback that reuses the SAME already-tested internal helpers as the
scheduled morning/evening reports (_pf_build_report /
_pf_ensure_deep_verification(force=True) / _pf_apply_deep_results /
_pf_render_admin_text / _pf_correct_message) -- never a bespoke call. Both
modes share ONE implementation (_pf_manual_check_run(chat_id, user_id,
mode)) parameterized via _pf_manual_mode_config(mode), not two copies.

Techniques: panel_bot.py cannot be imported standalone (Telethon/env side
effects) -- the new functions are extracted via ast.parse + ast.unparse +
exec(), the same technique used by tools/preflight_message_update_selftest.py
and tools/proxy_pool_selftest.py. preflight_check.py IS directly importable
-- report_is_ok/apply_correction_note are used FOR REAL (not faked).
Telegram client calls and the settings KV are faked in-memory; no real
DB/network/Telegram. "No PartnerBot / no auth-wizard references" is checked
as a plain text scan of the new source block itself (between its own
START/END markers), not of the whole 800KB file.

    python3.12 tools\\preflight_manual_trigger_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
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
    for _ in range(5):
        await asyncio.sleep(0)


# ======================================================================
# AST extraction (same technique as preflight_message_update_selftest.py)
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


# ======================================================================
# Static text scan of the new block only (between its own START/END
# markers) -- never PartnerBot, never auth/password/wizard.
# ======================================================================

def _manual_trigger_block_text() -> str:
    src = open(PANEL_PATH, encoding="utf-8-sig").read()
    start_marker = "# --- TPILOT PREFLIGHT MANUAL TRIGGER (pf:manual_check) 20260713 START"
    end_marker = "# --- TPILOT PREFLIGHT MANUAL TRIGGER (pf:manual_check) 20260713 END"
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def _menu_collapse_block_text() -> str:
    src = open(PANEL_PATH, encoding="utf-8-sig").read()
    start_marker = "# --- TPILOT SERVICE MENU BASELINE MANAGERS COLLAPSE 20260713 START"
    end_marker = "# --- TPILOT SERVICE MENU BASELINE MANAGERS COLLAPSE 20260713 END"
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


def test_12_13_no_partner_or_auth_references() -> None:
    partner_forbidden = ["partner_stat_bot", "build_partner_report", "partner_buyers", "PARTNER_BOT_TOKEN", "PARTNER_STAT_BOT_TOKEN"]
    auth_forbidden = ["_wizard_set(", "_wizard_get(", "PANEL_ADMIN_PASSWORD", "MANAGER_ADMIN_PASSWORD", "password"]
    for label, block in (("manual-trigger", _manual_trigger_block_text()), ("menu-collapse", _menu_collapse_block_text())):
        partner_hits = [f for f in partner_forbidden if f.lower() in block.lower()]
        auth_hits = [f for f in auth_forbidden if f.lower() in block.lower()]
        check(f"12. {label} block never references PartnerBot (token/buyers/build_partner_report/module)", partner_hits == [], repr(partner_hits))
        check(f"13. {label} block never touches auth/password/wizard state", auth_hits == [], repr(auth_hits))


def test_1_2_3_buttons_and_callbacks_defined() -> None:
    block = _manual_trigger_block_text()
    check("1. service menu button 'Проверить сегодня' is defined", "🛡 Проверить сегодня" in block)
    check("1. service menu button 'Проверить завтра' is defined", "🛡 Проверить завтра" in block)
    check("2. callback pf:manual_check_today is registered", '"pf:manual_check_today"' in block)
    check("2. callback pf:manual_check_tomorrow is registered", '"pf:manual_check_tomorrow"' in block)
    check("3. old callback pf:manual_check is kept as an alias", '"pf:manual_check"' in block)


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
    def __init__(self):
        self.calls: list = []
        self._next_id = 7000
        self.edit_fail = False
        self.delete_fail = False
        self.send_fail = False

    def on(self, *args, **kwargs):
        def _decorator(fn):
            return fn
        return _decorator

    async def edit_message(self, chat_id, message_id, text, buttons=None):
        self.calls.append({"op": "edit", "chat_id": int(chat_id), "mid": int(message_id), "text": text})
        if self.edit_fail:
            raise RuntimeError("edit failed (simulated)")

    async def delete_messages(self, chat_id, ids):
        self.calls.append({"op": "delete", "chat_id": int(chat_id), "ids": list(ids)})
        if self.delete_fail:
            raise RuntimeError("delete failed (simulated)")

    async def send_message(self, chat_id, text, buttons=None):
        if self.send_fail:
            self.calls.append({"op": "send", "chat_id": int(chat_id), "text": text, "mid": None})
            raise RuntimeError("send failed (simulated)")
        self._next_id += 1
        self.calls.append({"op": "send", "chat_id": int(chat_id), "text": text, "mid": self._next_id})
        return _FakeMessage(self._next_id)


class _FakeEventsNS:
    CallbackQuery = object()


class FakeEvent:
    def __init__(self, data: bytes, chat_id: int, sender_id: int):
        self.data = data
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.answers: list = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))


def report_ops(calls: list) -> list:
    """Everything except the transient '⏳ ...' status message: its own send
    call AND the later delete call that cleans it up (matched by mid, so a
    delete of a DIFFERENT/report message is never mistakenly stripped)."""
    status_mids = {c.get("mid") for c in calls if c["op"] == "send" and "⏳" in c.get("text", "") and c.get("mid") is not None}
    out = []
    for c in calls:
        if c["op"] == "send" and "⏳" in c.get("text", ""):
            continue
        if c["op"] == "delete" and status_mids and set(c.get("ids", [])) <= status_mids:
            continue
        out.append(c)
    return out


class ReportSource:
    def __init__(self, report: dict):
        self.report = report
        self.calls: list = []


def _make_fake_ensure(verify_calls, verify_in_progress):
    async def _fake(period, date_iso, deadline_ts, *, force=False):
        verify_calls.append({"period": period, "date": date_iso, "force": force})
        verify_in_progress[period] = None  # simulate the background task finishing instantly
    return _fake


def _make_fake_build_report(report_source):
    def _fake(today_iso=None, db_path=None):
        report_source.calls.append({"today_iso": today_iso, "db_path": db_path})
        return dict(report_source.report)
    return _fake


MANUAL_RUN_NAMES = {
    "_PF_MANUAL_LABEL_TODAY", "_PF_MANUAL_LABEL_TOMORROW",
    "_PF_MANUAL_NOTE_OK_TODAY", "_PF_MANUAL_NOTE_OK_TOMORROW",
    "_PF_MANUAL_NOTE_PROBLEM_TODAY", "_PF_MANUAL_NOTE_PROBLEM_TOMORROW",
    "_PF_MANUAL_VERIFY_BUDGET_SEC", "_PF_MANUAL_POLL_INTERVAL_SEC",
    "_PF_MANUAL_CALLBACK_MODES",
    "_pf_today_status_key", "_pf_today_resolved_key",
    "_pf_manual_mode_config",
    "_safe_text", "_pf_eve_status_key", "_pf_eve_resolved_key", "_pf_correct_message",
    "_pf_manual_cleanup_stale_message",
    "_pf_manual_check_run", "_pf_manual_check_callback",
    "_tp_visual_service_menu",
}


def build_ns(report_source, kv: FakeKV, client: FakeClient, verify_calls: list, verify_in_progress: dict,
             is_allowed=True, duplicate=False, prev_service_menu_rows=None, managers=None):
    if managers is not None:
        v8_ns = _extract_and_exec(
            PANEL_PATH,
            {"_tp_v8_service_menu_rows", "_tp_v8_manager_action_rows"},
            {
                "Button": _FakeButton,
                "_manager_rows": (lambda *a, **kw: list(managers)),
                "_manager_short_label": (lambda r: str(r.get("display_name") or r.get("manager_key") or "?")),
            },
        )
        prev_menu_fn = v8_ns["_tp_v8_service_menu_rows"]
    elif prev_service_menu_rows is not None:
        prev_menu_fn = (lambda: list(prev_service_menu_rows))
    else:
        prev_menu_fn = (lambda: [])
    return _extract_and_exec(
        PANEL_PATH,
        MANUAL_RUN_NAMES,
        {
            "asyncio": asyncio,
            "timedelta": timedelta,
            "Dict": dict, "Any": object, "Optional": object, "Tuple": tuple, "List": list,
            "client": client,
            "events": _FakeEventsNS,
            "Button": _FakeButton,
            "_is_allowed": (lambda event: is_allowed),
            "_panel_callback_is_duplicate": (lambda chat_id, user_id, key_text: duplicate),
            "_PF_STAGE1_OK": True,
            "_pb_get_setting": kv.get,
            "_pf_set_setting": kv.set,
            "_pf_kyiv_now": (lambda: datetime(2026, 7, 13, 23, 5, 0)),
            "_PF_VERIFY_IN_PROGRESS": verify_in_progress,
            "_pf_ensure_deep_verification": _make_fake_ensure(verify_calls, verify_in_progress),
            "_pf_build_report": _make_fake_build_report(report_source),
            "_pf_read_verify_blob": lambda period, date_iso: {"target_date": date_iso},
            "_pf_apply_deep_results": lambda report, blob, period_kind=None: report,
            "_pf_render_admin_text": lambda report, period_kind=None: (
                f"ADMIN_REPORT kind={period_kind} status={report.get('deep_status_global')} all_ok={report.get('all_ok')}"
            ),
            "_pf_report_is_ok": pf.report_is_ok,
            "_pf_apply_correction_note": pf.apply_correction_note,
            "_PF_CORRECTED_TITLE_ADMIN": pf.CORRECTED_TITLE_ADMIN,
            "TPILOT_DB_PATH": "fake.db",
            "_PFMANUAL_PREV_SERVICE_MENU": prev_menu_fn,
        },
    )


CHAT_ID = 654321
USER_ID = 999
# fixed _pf_kyiv_now() -> 2026-07-13 23:05
TODAY_ISO = "2026-07-13"
TOMORROW_ISO = "2026-07-14"

OK_REPORT = {"all_ok": True, "deep_status_global": "ok", "deep_expected_total": 15}
PROBLEM_REPORT = {"all_ok": True, "deep_status_global": "timeout", "deep_expected_total": 15}


# ======================================================================
# 4/6: today target date + never passes DB path as date
# 5/7: tomorrow target date + never passes DB path as date
# ======================================================================

async def test_4_6_today_target_and_date_vs_path() -> None:
    kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    src = ReportSource(dict(OK_REPORT))
    ns = build_ns(src, kv, client, verify_calls, vip)

    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "today")

    check("4. today mode's build_report date argument is Kyiv TODAY, not tomorrow", src.calls[0]["today_iso"] == TODAY_ISO, repr(src.calls))
    check("6. today mode's build_report db_path argument is the actual DB path, not a date", src.calls[0]["db_path"] == "fake.db", repr(src.calls))
    check("4. today mode requests verify for period='morning' (the existing morning kind)", verify_calls[0]["period"] == "morning", repr(verify_calls))
    check("4. today mode requests verify for TODAY's date", verify_calls[0]["date"] == TODAY_ISO, repr(verify_calls))


async def test_5_7_tomorrow_target_and_date_vs_path() -> None:
    kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    src = ReportSource(dict(OK_REPORT))
    ns = build_ns(src, kv, client, verify_calls, vip)

    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "tomorrow")

    check("5. tomorrow mode's build_report date argument is Kyiv TOMORROW, not today", src.calls[0]["today_iso"] == TOMORROW_ISO, repr(src.calls))
    check("7. tomorrow mode's build_report db_path argument is the actual DB path, not a date", src.calls[0]["db_path"] == "fake.db", repr(src.calls))
    check("5. tomorrow mode requests verify for period='evening' (the existing evening kind)", verify_calls[0]["period"] == "evening", repr(verify_calls))
    check("5. tomorrow mode requests verify for TOMORROW's date", verify_calls[0]["date"] == TOMORROW_ISO, repr(verify_calls))
    # backward-compat: the default mode (no explicit mode arg) is tomorrow.
    kv2, client2, vc2, vip2 = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    src2 = ReportSource(dict(OK_REPORT))
    ns2 = build_ns(src2, kv2, client2, vc2, vip2)
    await ns2["_pf_manual_check_run"](CHAT_ID, USER_ID)
    check("(extra) default mode (omitted) behaves as tomorrow", src2.calls[0]["today_iso"] == TOMORROW_ISO, repr(src2.calls))


async def test_8_separate_slots_for_today_and_tomorrow() -> None:
    kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    src = ReportSource(dict(OK_REPORT))
    ns = build_ns(src, kv, client, verify_calls, vip)

    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "today")
    today_ref = kv.get(f"preflight_msg_admin_{CHAT_ID}")
    tomorrow_ref_after_today = kv.get(f"preflight_msg_admin_evening_{CHAT_ID}")

    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "tomorrow")
    today_ref_after_tomorrow = kv.get(f"preflight_msg_admin_{CHAT_ID}")
    tomorrow_ref = kv.get(f"preflight_msg_admin_evening_{CHAT_ID}")

    check("8. today mode writes its own message slot (preflight_msg_admin_{chat})", today_ref.startswith(f"{TODAY_ISO}:"), today_ref)
    check("8. today run does NOT touch the tomorrow slot", tomorrow_ref_after_today == "", repr(tomorrow_ref_after_today))
    check("8. tomorrow mode writes its own message slot (preflight_msg_admin_evening_{chat})", tomorrow_ref.startswith(f"{TOMORROW_ISO}:"), tomorrow_ref)
    check("8. tomorrow run does NOT overwrite/clear the today slot", today_ref_after_tomorrow == today_ref, repr((today_ref, today_ref_after_tomorrow)))
    check("8. today and tomorrow status keys are distinct", ns["_pf_today_status_key"](TODAY_ISO, CHAT_ID) != ns["_pf_eve_status_key"](TOMORROW_ISO, CHAT_ID))
    check(
        "8. today and tomorrow status/resolved KV are both set independently",
        kv.get(ns["_pf_today_status_key"](TODAY_ISO, CHAT_ID)) == "ok" and kv.get(ns["_pf_eve_status_key"](TOMORROW_ISO, CHAT_ID)) == "ok",
    )


async def test_9_repeated_today_click_no_spam() -> None:
    kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    src = ReportSource(dict(OK_REPORT))
    ns = build_ns(src, kv, client, verify_calls, vip)
    msg_key = f"preflight_msg_admin_{CHAT_ID}"

    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "today")
    mid1 = kv.get(msg_key)
    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "today")
    mid2 = kv.get(msg_key)
    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "today")
    mid3 = kv.get(msg_key)

    rops = report_ops(client.calls)
    sends = [c for c in rops if c["op"] == "send"]
    edits = [c for c in rops if c["op"] == "edit"]
    check("9. three today triggers -> exactly ONE real new send (the first)", len(sends) == 1, repr(rops))
    check("9. subsequent today triggers edit the SAME stored message, never spam new ones", len(edits) == 2, repr(rops))
    check("9. today stored message_id is stable across repeated triggers", mid1 == mid2 == mid3 and bool(mid1), repr((mid1, mid2, mid3)))
    check("9. today OK correction uses the exact 'на сегодня' note text", any("Готовность на сегодня перепроверена вручную" in c["text"] for c in rops))


async def test_10_repeated_tomorrow_click_no_spam() -> None:
    kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    src = ReportSource(dict(OK_REPORT))
    ns = build_ns(src, kv, client, verify_calls, vip)
    msg_key = f"preflight_msg_admin_evening_{CHAT_ID}"

    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "tomorrow")
    mid1 = kv.get(msg_key)
    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "tomorrow")
    mid2 = kv.get(msg_key)
    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "tomorrow")
    mid3 = kv.get(msg_key)

    rops = report_ops(client.calls)
    sends = [c for c in rops if c["op"] == "send"]
    edits = [c for c in rops if c["op"] == "edit"]
    check("10. three tomorrow triggers -> exactly ONE real new send (the first)", len(sends) == 1, repr(rops))
    check("10. subsequent tomorrow triggers edit the SAME stored message, never spam new ones", len(edits) == 2, repr(rops))
    check("10. tomorrow stored message_id is stable across repeated triggers", mid1 == mid2 == mid3 and bool(mid1), repr((mid1, mid2, mid3)))
    check("10. tomorrow OK correction uses the exact 'на завтра' note text", any("Готовность на завтра перепроверена вручную" in c["text"] for c in rops))


async def test_11_edit_failure_fallback_both_modes() -> None:
    for mode, msg_key, date_iso, note_fragment in (
        ("today", f"preflight_msg_admin_{CHAT_ID}", TODAY_ISO, "Ручная перепроверка готовности на сегодня"),
        ("tomorrow", f"preflight_msg_admin_evening_{CHAT_ID}", TOMORROW_ISO, "Ручная перепроверка готовности на завтра"),
    ):
        kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
        kv.set(msg_key, f"{date_iso}:6161")
        client.edit_fail = True
        src = ReportSource(dict(PROBLEM_REPORT))
        ns = build_ns(src, kv, client, verify_calls, vip)

        await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, mode)

        rops = report_ops(client.calls)
        check(f"11. [{mode}] edit failure -> delete then send exactly once", [c["op"] for c in rops] == ["edit", "delete", "send"], repr(rops))
        check(f"11. [{mode}] problem report note is present in the fallback send", note_fragment in rops[-1]["text"], rops[-1]["text"])


async def test_no_stored_message_both_modes() -> None:
    for mode, msg_key, date_iso in (
        ("today", f"preflight_msg_admin_{CHAT_ID}", TODAY_ISO),
        ("tomorrow", f"preflight_msg_admin_evening_{CHAT_ID}", TOMORROW_ISO),
    ):
        kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
        src = ReportSource(dict(OK_REPORT))
        ns = build_ns(src, kv, client, verify_calls, vip)

        await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, mode)

        rops = report_ops(client.calls)
        check(f"(extra) [{mode}] no stored message -> exactly one new send, no edit/delete", [c["op"] for c in rops] == ["send"], repr(rops))
        check(f"(extra) [{mode}] plain first send does NOT use the 'Обновлено' fallback title", "Обновлено" not in rops[0]["text"], rops[0]["text"])
        check(f"(extra) [{mode}] message_id stored under the correct date", kv.get(msg_key).startswith(f"{date_iso}:"))


FIXTURE_MANAGERS = [
    {"manager_key": "alpha01", "display_name": "Alpha"},
    {"manager_key": "beta02", "display_name": "Beta"},
    {"manager_key": "gamma03", "display_name": "Gamma"},
]

OLD_ISO = "2026-07-10"  # neither TODAY_ISO nor TOMORROW_ISO -- a genuinely stale date


async def test_A5_A6_stale_date_cleanup_both_modes() -> None:
    for mode, msg_key, date_iso in (
        ("today", f"preflight_msg_admin_{CHAT_ID}", TODAY_ISO),
        ("tomorrow", f"preflight_msg_admin_evening_{CHAT_ID}", TOMORROW_ISO),
    ):
        kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
        kv.set(msg_key, f"{OLD_ISO}:4242")
        src = ReportSource(dict(OK_REPORT))
        ns = build_ns(src, kv, client, verify_calls, vip)

        await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, mode)

        rops = report_ops(client.calls)
        check(f"A5/A6. [{mode}] stale old-date message (mid=4242) is deleted before the new report is sent", any(c["op"] == "delete" and c.get("ids") == [4242] for c in rops), repr(rops))
        check(f"A5/A6. [{mode}] the stale delete happens BEFORE the new send (not edited)", [c["op"] for c in rops] == ["delete", "send"], repr(rops))
        check(f"A5/A6. [{mode}] message_id key now points to the NEW date, old date gone", kv.get(msg_key).startswith(f"{date_iso}:"), kv.get(msg_key))


async def test_A7_A8_same_date_no_stale_delete_both_modes() -> None:
    for mode, msg_key, date_iso in (
        ("today", f"preflight_msg_admin_{CHAT_ID}", TODAY_ISO),
        ("tomorrow", f"preflight_msg_admin_evening_{CHAT_ID}", TOMORROW_ISO),
    ):
        kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
        kv.set(msg_key, f"{date_iso}:7777")
        src = ReportSource(dict(OK_REPORT))
        ns = build_ns(src, kv, client, verify_calls, vip)

        await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, mode)

        rops = report_ops(client.calls)
        check(f"A7/A8. [{mode}] same-date stored message is edited in place, not replaced", [c["op"] for c in rops] == ["edit"], repr(rops))
        check(f"A7/A8. [{mode}] same-date report mid (7777) is NEVER deleted", not any(c["op"] == "delete" for c in rops), repr(rops))


async def test_A9_stale_delete_failure_does_not_block_send() -> None:
    kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    kv.set(f"preflight_msg_admin_{CHAT_ID}", f"{OLD_ISO}:5151")
    client.delete_fail = True
    src = ReportSource(dict(OK_REPORT))
    ns = build_ns(src, kv, client, verify_calls, vip)

    await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "today")

    rops = report_ops(client.calls)
    check("A9. delete failure for the stale message does not block the new send", any(c["op"] == "send" for c in rops), repr(rops))
    check("A9. message_id is still updated to the new date despite the delete failure", kv.get(f"preflight_msg_admin_{CHAT_ID}").startswith(f"{TODAY_ISO}:"))


async def test_A10_invalid_stored_value_ignored() -> None:
    for bad_value in ("garbage-no-colon", "2026-07-10:notanumber", ":", "2026-07-10:"):
        kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
        kv.set(f"preflight_msg_admin_{CHAT_ID}", bad_value)
        src = ReportSource(dict(OK_REPORT))
        ns = build_ns(src, kv, client, verify_calls, vip)

        await ns["_pf_manual_check_run"](CHAT_ID, USER_ID, "today")

        rops = report_ops(client.calls)
        check(f"A10. invalid stored value {bad_value!r} triggers no delete call", not any(c["op"] == "delete" for c in rops), repr(rops))
        check(f"A10. invalid stored value {bad_value!r} is ignored safely -- new send still happens", any(c["op"] == "send" for c in rops), repr(rops))


def test_service_menu_has_both_buttons() -> None:
    kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    src = ReportSource(dict(OK_REPORT))
    # a REAL _tp_v8_service_menu_rows() (with a fixed manager fixture, not a
    # stub) proves the collapsed "По отдельному менеджеру (N)" button
    # actually appears end-to-end, composed together with the today/tomorrow
    # row the manual-trigger wrapper adds on top.
    ns = build_ns(src, kv, client, verify_calls, vip, managers=FIXTURE_MANAGERS)

    rows = ns["_tp_visual_service_menu"]()
    flat_data = [btn[2] for row in rows for btn in row if isinstance(btn, tuple)]
    flat_text = [btn[1] for row in rows for btn in row if isinstance(btn, tuple)]
    check("B1. service menu contains pf:manual_check_today button", b"pf:manual_check_today" in flat_data, rows)
    check("B1. service menu contains pf:manual_check_tomorrow button", b"pf:manual_check_tomorrow" in flat_data, rows)
    check("B1. service menu contains the collapsed 'По отдельному менеджеру (N)' button with the correct count", any(f"По отдельному менеджеру ({len(FIXTURE_MANAGERS)})" in t for t in flat_text), rows)
    check("B1. collapsed button targets menu:baseline_managers", b"menu:baseline_managers" in flat_data, rows)
    check(
        "B2. service menu does NOT contain inline per-manager baseline action rows anymore",
        not any(d.startswith(b"cmd:/baseline status alpha01") or d.startswith(b"cmd:/baseline status beta02") or d.startswith(b"cmd:/baseline status gamma03") for d in flat_data),
        rows,
    )
    check("B2. the 'status all' bulk button is still present (untouched, not per-manager)", b"cmd:/baseline status all" in flat_data, rows)


def test_baseline_managers_route() -> None:
    # N5 (2026-07-21): panel_bot.py's LAST _title_for_menu generation is now
    # the N5 root-switch override (an additive EOF block, same pattern every
    # phase uses) -- "baseline_managers" isn't "main"/"old_root", so it falls
    # through to that generation's own captured PREV
    # (_N5_PREV_TITLE_FOR_MENU). This test's extraction re-executes ALL
    # _title_for_menu defs by name (matching runtime last-wins), so it must
    # also grab that PREV capture or the fallthrough hits a NameError instead
    # of continuing down the chain to the real "baseline_managers" handler.
    # Purely a test-namespace fix -- no assertion below is touched/weakened.
    ns = _extract_and_exec(
        PANEL_PATH,
        {"_title_for_menu", "_tp_v8_manager_action_rows", "_N5_PREV_TITLE_FOR_MENU"},
        {
            "Button": _FakeButton,
            "_manager_rows": (lambda *a, **kw: list(FIXTURE_MANAGERS)),
            "_manager_short_label": (lambda r: str(r.get("display_name") or r.get("manager_key") or "?")),
            "_tp_visual_screen": (lambda path_items, description="", extra="": " > ".join(path_items)),
            "_panel_header": (lambda: "HEADER"),
        },
    )
    text, rows = ns["_title_for_menu"]("baseline_managers")
    flat_data = [btn[2] for row in rows for btn in row if isinstance(btn, tuple)]
    check("B3. baseline_managers route renders per-manager action rows (status)", b"cmd:/baseline status alpha01" in flat_data, rows)
    check("B3. baseline_managers route renders per-manager action rows (mark)", b"cmd:/baseline create alpha01" in flat_data, rows)
    check("B3. baseline_managers route renders per-manager action rows (reset)", b"menu:baseline_clear_confirm:alpha01" in flat_data, rows)
    check("B3. baseline_managers route renders rows for ALL fixture managers", all(f"cmd:/baseline status {m['manager_key']}".encode() in flat_data for m in FIXTURE_MANAGERS), rows)
    # N5.3: Back re-parented to the canonical Система -> Сервис wrapper
    # (menu:nm_service_w), not the retired old-menu category service.
    check("B4 (amended N5.3). baseline_managers route includes a Back button to menu:nm_service_w",
          any(d == b"menu:nm_service_w" for d in flat_data), rows)
    check("B4. baseline_managers screen has a non-empty title", bool(text), text)

    # a second call (repeated press) must render the identical rows -- no
    # accumulation/duplication of manager rows across repeated navigation.
    text2, rows2 = ns["_title_for_menu"]("baseline_managers")
    check("B5(extra). repeated navigation to baseline_managers renders identical rows (no growth)", rows == rows2, (rows, rows2))


async def test_callback_gating_and_aliases() -> None:
    kv, client, verify_calls, vip = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    src = ReportSource(dict(OK_REPORT))

    ns = build_ns(src, kv, client, verify_calls, vip, is_allowed=True, duplicate=False)
    ev = FakeEvent(b"menu:main", CHAT_ID, USER_ID)
    await ns["_pf_manual_check_callback"](ev)
    await _drain()
    check("(extra) unrelated callback data is ignored entirely", client.calls == [] and verify_calls == [] and ev.answers == [])

    ns2 = build_ns(src, kv, client, verify_calls, vip, is_allowed=False, duplicate=False)
    ev2 = FakeEvent(b"pf:manual_check_today", CHAT_ID, USER_ID)
    await ns2["_pf_manual_check_callback"](ev2)
    await _drain()
    check("(extra) non-admin caller is rejected before doing anything", client.calls == [] and verify_calls == [])

    ns3 = build_ns(src, kv, client, verify_calls, vip, is_allowed=True, duplicate=True)
    ev3 = FakeEvent(b"pf:manual_check_tomorrow", CHAT_ID, USER_ID)
    await ns3["_pf_manual_check_callback"](ev3)
    await _drain()
    check("(extra) duplicate-click guard answers without launching a second run", client.calls == [] and verify_calls == [] and ev3.answers, repr(ev3.answers))

    # today callback -> today mode actually runs
    kvA, clientA, vcA, vipA = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    srcA = ReportSource(dict(OK_REPORT))
    nsA = build_ns(srcA, kvA, clientA, vcA, vipA, is_allowed=True, duplicate=False)
    evA = FakeEvent(b"pf:manual_check_today", CHAT_ID, USER_ID)
    await nsA["_pf_manual_check_callback"](evA)
    await _drain()
    check("3. pf:manual_check_today callback runs in TODAY mode", vcA and vcA[0]["period"] == "morning" and vcA[0]["date"] == TODAY_ISO, repr(vcA))

    # legacy alias -> tomorrow mode actually runs (same as pf:manual_check_tomorrow)
    kvB, clientB, vcB, vipB = FakeKV(), FakeClient(), [], {"morning": None, "evening": None}
    srcB = ReportSource(dict(OK_REPORT))
    nsB = build_ns(srcB, kvB, clientB, vcB, vipB, is_allowed=True, duplicate=False)
    evB = FakeEvent(b"pf:manual_check", CHAT_ID, USER_ID)
    await nsB["_pf_manual_check_callback"](evB)
    await _drain()
    check("3. legacy pf:manual_check callback runs in TOMORROW mode (backward-compat alias)", vcB and vcB[0]["period"] == "evening" and vcB[0]["date"] == TOMORROW_ISO, repr(vcB))


async def main() -> int:
    test_12_13_no_partner_or_auth_references()
    test_1_2_3_buttons_and_callbacks_defined()
    test_service_menu_has_both_buttons()
    test_baseline_managers_route()
    await test_4_6_today_target_and_date_vs_path()
    await test_5_7_tomorrow_target_and_date_vs_path()
    await test_8_separate_slots_for_today_and_tomorrow()
    await test_9_repeated_today_click_no_spam()
    await test_10_repeated_tomorrow_click_no_spam()
    await test_11_edit_failure_fallback_both_modes()
    await test_no_stored_message_both_modes()
    await test_callback_gating_and_aliases()
    await test_A5_A6_stale_date_cleanup_both_modes()
    await test_A7_A8_same_date_no_stale_delete_both_modes()
    await test_A9_stale_delete_failure_does_not_block_send()
    await test_A10_invalid_stored_value_ignored()

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
