# -*- coding: utf-8 -*-
"""tools/closer_settings_selftest.py -- offline self-test for the closer
self-service settings feature (2026-07-13): a ManagerBot-only, closer-only
("⚙️ Настройки клоузера") screen letting a closer (role='closer' in
`managers`) configure their own work window (writes the EXISTING
manager_client_message_schedule table main.py's _tp_gq_get_schedule already
reads as a per-manager override) and custom greeting/away texts (new
2-column settings KV keys, read by main.py's _maybe_auto_reply_to_lead via
a small new helper, _tp_closer_text).

Scope: manager_bot.py (UI/wizard/callback) + main.py (_tp_closer_text +
3 call-site wires inside the ACTIVE _maybe_auto_reply_to_lead). No
AdminBot/PartnerBot changes, no destructive schema changes -- only reuses
the pre-existing manager_client_message_schedule table and the standard
2-column settings KV pattern already used elsewhere in this project.

Techniques: manager_bot.py/main.py cannot be imported standalone
(Telethon/env side effects at import time) -- functions under test are
extracted via ast.parse + ast.unparse + exec(), the same technique used by
tools/preflight_manual_trigger_selftest.py and friends. _access_targets/
_tr_is_closer/_connect are extracted FOR REAL (not faked) and run against a
REAL temporary SQLite file (never db/data_tpilot.db or db/data.db) so the
identity/role gate is exercised exactly as production runs it. Telegram
client calls are faked in-memory; no real DB/network/Telegram.

    python3.12 tools\\closer_settings_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import sqlite3
import sys
import tempfile
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


async def _drain() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ======================================================================
# AST extraction (same technique as tools/preflight_manual_trigger_selftest.py)
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


def _extract_last_and_exec(path: str, name: str, extra_ns: dict, containing: str = "") -> dict:
    """Like _extract_and_exec but grabs ONLY the LAST top-level def with
    this name (the active one in an override-stack file), skipping every
    shadowed earlier definition and any module-level assignment of the
    same name -- used for _mbstat_start_buttons so the test controls the
    injected PREV callable directly instead of chain-rebuilding through
    unrelated dependencies.

    `containing`: when a LATER override block (e.g. W1 SAFE SELF-DIAGNOSTIC
    20260729) re-redefines the same name with a DIFFERENT PREV variable,
    the naive "last def" no longer references the PREV callable this test
    injects. Passing a marker (e.g. "_CLS_PREV_START_BTNS") selects the
    last def whose source actually contains it -- i.e. the specific
    override under test -- keeping the test stable as more override
    blocks are appended after it."""
    src = open(path, encoding="utf-8-sig").read()
    tree = ast.parse(src)
    matches = [n for n in tree.body if getattr(n, "name", None) == name]
    if containing:
        matches = [n for n in matches if containing in ast.unparse(n)]
    if not matches:
        raise AssertionError(f"no def named {name} found in {path}")
    module_src = ast.unparse(matches[-1])
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path}>", "exec"), ns)
    return ns


MANAGER_BOT_PATH = str(BASE_DIR / "manager_bot.py")
MAIN_PATH = str(BASE_DIR / "main.py")


# ======================================================================
# Static text scan of the new blocks (no destructive SQL, no AdminBot/
# PartnerBot references, no auth/password/wizard state).
# ======================================================================

def _block_text(path: str, start_marker: str, end_marker: str) -> str:
    src = open(path, encoding="utf-8-sig").read()
    i = src.index(start_marker)
    j = src.index(end_marker, i)
    return src[i:j]


MB_BLOCK_START = "# --- TPILOT CLOSER SELF-SERVICE SETTINGS 20260713 START"
MB_BLOCK_END = "# --- TPILOT CLOSER SELF-SERVICE SETTINGS 20260713 END"
MAIN_BLOCK_START = "# --- TPILOT CLOSER SELF-SERVICE SETTINGS 20260713 START"
MAIN_BLOCK_END = "# --- TPILOT CLOSER SELF-SERVICE SETTINGS 20260713 END"


def test_18_no_destructive_sql() -> None:
    forbidden = ["DROP TABLE", "DROP COLUMN", "DELETE FROM managers", "DELETE FROM access_", "ALTER TABLE managers", "TRUNCATE"]
    for label, path, s, e in (
        ("manager_bot.py", MANAGER_BOT_PATH, MB_BLOCK_START, MB_BLOCK_END),
        ("main.py", MAIN_PATH, MAIN_BLOCK_START, MAIN_BLOCK_END),
    ):
        block = _block_text(path, s, e)
        hits = [f for f in forbidden if f.lower() in block.lower()]
        check(f"18. {label} closer block has no destructive SQL ({forbidden})", hits == [], repr(hits))


def test_19_no_adminbot_partnerbot_changes() -> None:
    for label, path in (("panel_bot.py", str(BASE_DIR / "panel_bot.py")), ("partner_stat_bot.py", str(BASE_DIR / "partner_stat_bot.py"))):
        try:
            text = open(path, encoding="utf-8-sig").read()
        except Exception as e:
            check(f"19. {label} is readable for the scope check", False, repr(e))
            continue
        check(f"19. {label} does not contain the closer-settings block marker", "TPILOT CLOSER SELF-SERVICE SETTINGS" not in text, "marker leaked")


# ======================================================================
# Temp DB fixture (manager_bot.py side: access_targets + managers +
# manager_client_message_schedule + settings, all auto-created by the
# extracted real functions themselves via CREATE TABLE IF NOT EXISTS)
# ======================================================================

def _selftest_db_guard(db_path: str, base_dir: Path, *, temp_root: str = "") -> None:
    """Fail-fast production-path guard (2026-07-16, matches the identical
    guard already deployed in the sibling replacement selftests --
    manager_replacement_backend_selftest.py, manager_replacement_storage_
    selftest.py, etc.): refuses any db_path located under base_dir/db (the
    project's own db/ directory), using os.path.commonpath (Windows-safe
    path-component comparison, not a naive string startswith) so a sibling
    directory like .../dbfoo is never a false match. This file never
    mutates a real `storage` module's DB_PATH/QUEUE_DB_PATH globals (its
    extracted manager_bot.py namespace takes TPILOT_DB_PATH as a plain
    string), so there is no storage_mod to cross-check here -- only the
    path-safety and (when given) temp_root containment checks apply. Raises
    AssertionError on any violation; callers must call this BEFORE any
    schema/fixture write."""
    prod_db_dir = os.path.abspath(os.path.join(str(base_dir), "db"))
    target = os.path.abspath(str(db_path))
    try:
        common = os.path.commonpath([prod_db_dir, target])
    except ValueError:
        common = ""  # different drives on Windows -> definitely not nested
    assert common != prod_db_dir, f"refusing to run selftest storage against a path under {prod_db_dir}: {db_path}"
    if temp_root:
        root = os.path.abspath(str(temp_root))
        try:
            root_common = os.path.commonpath([root, target])
        except ValueError:
            root_common = ""
        assert root_common == root, f"selftest db path must stay inside its temp root {root}: {db_path}"


def _make_temp_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db", prefix="closer_settings_selftest_")
    os.close(fd)
    _selftest_db_guard(path, BASE_DIR, temp_root=tempfile.gettempdir())
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE access_targets(tg_user_id INTEGER NOT NULL, manager_key TEXT NOT NULL)")
        con.execute("CREATE TABLE managers(manager_key TEXT PRIMARY KEY, role TEXT)")
        con.commit()
    finally:
        con.close()
    return path


def _seed(db_path: str, *, access: dict, roles: dict) -> None:
    con = sqlite3.connect(db_path)
    try:
        for uid, keys in access.items():
            for k in keys:
                con.execute("INSERT INTO access_targets(tg_user_id, manager_key) VALUES (?,?)", (int(uid), k))
        for k, role in roles.items():
            con.execute(
                "INSERT INTO managers(manager_key, role) VALUES (?,?) "
                "ON CONFLICT(manager_key) DO UPDATE SET role=excluded.role",
                (k, role),
            )
        con.commit()
    finally:
        con.close()


class FakeLog:
    def warning(self, *a, **kw): pass
    def info(self, *a, **kw): pass
    def exception(self, *a, **kw): pass


class FakeButton:
    @staticmethod
    def inline(text, data):
        return ("btn", text, data if isinstance(data, (bytes, bytearray)) else str(data).encode("utf-8"))


class FakeEventsNS:
    CallbackQuery = object()
    NewMessage = object()


class FakeClient:
    def on(self, *a, **kw):
        def _decorator(fn):
            return fn
        return _decorator


class FakeCallbackEvent:
    def __init__(self, data: bytes, sender_id: int):
        self.data = data
        self.sender_id = sender_id
        self.answers: list = []
        self.edits: list = []

    async def answer(self, text=None, alert=False):
        self.answers.append((text, alert))

    async def edit(self, text, buttons=None):
        self.edits.append((text, buttons))


class FakeMessageEvent:
    def __init__(self, raw_text: str, sender_id: int, is_private: bool = True):
        self.raw_text = raw_text
        self._sender_id = sender_id
        self.is_private = is_private
        self.photo = None
        self.document = None
        self.responds: list = []

    async def get_sender(self):
        return type("S", (), {"id": self._sender_id})()

    async def respond(self, text):
        self.responds.append(text)


MB_NAMES = {
    "_connect", "_norm_key", "_access_targets", "_tr_is_closer", "_tr_manager_label", "_now_iso",
    "_MBSTAT_PENDING",
    "_CLS_PRESETS", "_CLS_TEXT_MAX_LEN", "_CLS_PENDING", "_CLS_DRAFT",
    "_cls_closer_keys", "_cls_owns_key", "_cls_manager_label",
    "_cls_get_setting", "_cls_set_setting",
    "_cls_greeting_key", "_cls_away_key", "_cls_updated_key",
    "_cls_parse_hhmm", "_cls_fmt_hhmm",
    "_cls_ensure_schedule_table", "_cls_schedule_get", "_cls_schedule_set", "_cls_apply_hour",
    "_cls_menu_text", "_cls_menu_buttons", "_cls_sched_text", "_cls_sched_buttons",
    "_cls_hour_picker_text", "_cls_hour_buttons", "_cls_view_text",
    "_cls_text_prompt", "_cls_validate_text", "_cls_handle_text_input",
    "_cls_callback", "_cls_pending_input",
}


def build_mb_ns(db_path: str) -> dict:
    return _extract_and_exec(
        MANAGER_BOT_PATH,
        MB_NAMES,
        {
            "sqlite3": sqlite3,
            "Path": Path,
            "datetime": __import__("datetime").datetime,
            "TPILOT_DB_PATH": db_path,
            "log": FakeLog(),
            "Dict": dict, "List": list, "Tuple": tuple, "Any": object,
            "client": FakeClient(),
            "events": FakeEventsNS,
            "Button": FakeButton,
            "_cls_re": re,
        },
    )


CHAT_MK = "vinch"   # closer
OTHER_CLOSER_MK = "beta_closer"
NORMAL_MK = "alpha01"  # regular manager
CLOSER_UID = 1001
OTHER_CLOSER_UID = 1002
NORMAL_UID = 2002


def _fresh_mb_ns() -> tuple:
    db_path = _make_temp_db()
    _seed(
        db_path,
        access={CLOSER_UID: [CHAT_MK], OTHER_CLOSER_UID: [OTHER_CLOSER_MK], NORMAL_UID: [NORMAL_MK]},
        roles={CHAT_MK: "closer", OTHER_CLOSER_MK: "closer", NORMAL_MK: "manager"},
    )
    ns = build_mb_ns(db_path)
    return ns, db_path


async def test_1_2_menu_visibility() -> None:
    ns, db_path = _fresh_mb_ns()
    try:
        prev_rows = [[FakeButton.inline("existing-row", b"noop")]]
        ns2 = _extract_last_and_exec(
            MANAGER_BOT_PATH, "_mbstat_start_buttons",
            {**{k: ns[k] for k in ns if not k.startswith("__")}, "_CLS_PREV_START_BTNS": (lambda uid: list(prev_rows))},
            containing="_CLS_PREV_START_BTNS",
        )
        closer_rows = ns2["_mbstat_start_buttons"](CLOSER_UID)
        closer_data = [btn[2] for row in (closer_rows or []) for btn in row]
        check("1. closer-only menu button appears for a closer", any(b"cls:menu" in d for d in closer_data), closer_rows)
        check("1. existing/prev rows are preserved (additive, not replacing)", any(b"noop" in d for d in closer_data), closer_rows)

        normal_rows = ns2["_mbstat_start_buttons"](NORMAL_UID)
        normal_data = [btn[2] for row in (normal_rows or []) for btn in row]
        check("2. closer-only menu button does NOT appear for a normal manager", not any(b"cls:menu" in d for d in normal_data), normal_rows)
    finally:
        os.unlink(db_path)


async def test_3_15_forged_callback_denied() -> None:
    ns, db_path = _fresh_mb_ns()
    try:
        # normal manager (no closer key at all) tries any cls: callback -> denied, no state change.
        ev = FakeCallbackEvent(b"cls:menu", NORMAL_UID)
        await ns["_cls_callback"](ev)
        check("3. non-closer sending cls: callback gets denied (alert)", ev.answers and ev.answers[0][1] is True, ev.answers)
        check("3. non-closer callback never edits any message", ev.edits == [], ev.edits)

        # closer A (owns CHAT_MK) tries to act on closer B's key (OTHER_CLOSER_MK) -> denied.
        ev2 = FakeCallbackEvent(f"cls:sched:{OTHER_CLOSER_MK}".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](ev2)
        check("15. closer A cannot act on closer B's manager_key (alert, no edit)", ev2.answers and ev2.answers[-1][1] is True, ev2.answers)
        check("15. forged cross-closer callback never edits any message", ev2.edits == [], ev2.edits)

        # sanity: closer A CAN act on their own key.
        ev3 = FakeCallbackEvent(f"cls:sched:{CHAT_MK}".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](ev3)
        check("(extra) closer acting on their OWN key is allowed (edit happens)", len(ev3.edits) == 1, ev3.edits)
    finally:
        os.unlink(db_path)


async def test_1_2_hour_picker_schedule_screen_buttons() -> None:
    """1/2. schedule screen shows the new hour-picker entry buttons instead
    of the removed free-text flow; presets are kept."""
    ns, db_path = _fresh_mb_ns()
    try:
        rows = ns["_cls_sched_buttons"](CHAT_MK)
        flat_text = [btn[1] for row in rows for btn in row]
        flat_data = [btn[2] for row in rows for btn in row]
        check("1. schedule screen has '🟢 Начало работы'", any(t == "🟢 Начало работы" for t in flat_text), rows)
        check("2. schedule screen has '🔴 Конец работы'", any(t == "🔴 Конец работы" for t in flat_text), rows)
        check("(extra) 'Начало работы' opens the hour picker (cls:hp:start:...)", f"cls:hp:start:{CHAT_MK}".encode("utf-8") in flat_data, rows)
        check("(extra) 'Конец работы' opens the hour picker (cls:hp:end:...)", f"cls:hp:end:{CHAT_MK}".encode("utf-8") in flat_data, rows)
        check("9. presets are still present on the schedule screen", any(d.startswith(b"cls:p:") for d in flat_data), rows)
        check("(extra) the removed free-text manual-entry button/callback is gone", not any(b"cls:t:" in d for d in flat_data), rows)

        # pressing '🟢 Начало работы' opens the real hour-picker screen.
        cb = FakeCallbackEvent(f"cls:hp:start:{CHAT_MK}".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](cb)
        check("(extra) pressing 'Начало работы' edits to the hour-picker screen", len(cb.edits) == 1, cb.edits)
        picker_text, picker_buttons = cb.edits[0]
        check("(extra) hour-picker screen title matches the required wording", picker_text.startswith("Выберите час начала работы"), picker_text)
        check("(extra) hour-picker screen has 24 hour buttons + Back", sum(len(row) for row in picker_buttons) == 25, picker_buttons)
    finally:
        os.unlink(db_path)


def test_3_4_hour_picker_renders_24_buttons() -> None:
    """3/4. the hour picker renders exactly 24 buttons (00..23)."""
    ns, db_path = _fresh_mb_ns()
    try:
        for field in ("start", "end"):
            rows = ns["_cls_hour_buttons"](CHAT_MK, field)
            back_data = f"cls:sched:{CHAT_MK}".encode("utf-8")
            hour_buttons = [btn for row in rows for btn in row if btn[2] != back_data]
            check(f"3. [{field}] hour picker renders exactly 24 hour buttons", len(hour_buttons) == 24, len(hour_buttons))
            labels = sorted(btn[1] for btn in hour_buttons)
            expected = sorted(f"{h:02d}" for h in range(24))
            check(f"3. [{field}] hour picker labels are exactly 00..23", labels == expected, labels)
            check(f"4. [{field}] hour picker includes '00'", "00" in labels)
            check(f"4. [{field}] hour picker includes '23'", "23" in labels)
            data_set = {btn[2] for btn in hour_buttons}
            check(
                f"(extra) [{field}] hour buttons carry the correct callback prefix",
                all(d.startswith(f"cls:h:{field}:{CHAT_MK}:".encode("utf-8")) for d in data_set),
                data_set,
            )
            check(
                f"(extra) [{field}] picker has a Back button to the schedule screen",
                any(btn[2] == back_data for row in rows for btn in row),
                rows,
            )

        title_start = ns["_cls_hour_picker_text"](CHAT_MK, "start")
        title_end = ns["_cls_hour_picker_text"](CHAT_MK, "end")
        check("(extra) hour picker title wording (start)", title_start.startswith("Выберите час начала работы"), title_start)
        check("(extra) hour picker title wording (end)", title_end.startswith("Выберите час окончания работы"), title_end)
    finally:
        os.unlink(db_path)


async def test_5_6_hour_pick_updates_only_one_side() -> None:
    """5/6. clicking a start/end hour updates only that side, preserves the other."""
    ns, db_path = _fresh_mb_ns()
    try:
        cb = FakeCallbackEvent(f"cls:h:start:{CHAT_MK}:9".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](cb)
        start, end = ns["_cls_schedule_get"](CHAT_MK)
        check("5. clicking a start hour updates start", start == "09:00", (start, end))
        check("5. clicking a start hour preserves the existing end", end == "17:00", (start, end))
        check("5. hour-pick click edits the schedule screen in place", len(cb.edits) == 1, cb.edits)

        cb2 = FakeCallbackEvent(f"cls:h:end:{CHAT_MK}:18".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](cb2)
        start2, end2 = ns["_cls_schedule_get"](CHAT_MK)
        check("6. clicking an end hour updates end", end2 == "18:00", (start2, end2))
        check("6. clicking an end hour preserves the existing start", start2 == "09:00", (start2, end2))
    finally:
        os.unlink(db_path)


async def test_7_hour_pick_start_ge_end_rejected() -> None:
    """7. selecting start >= end via the hour picker is rejected, schedule unchanged."""
    ns, db_path = _fresh_mb_ns()
    try:
        cb0 = FakeCallbackEvent(f"cls:h:end:{CHAT_MK}:10".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](cb0)
        start0, end0 = ns["_cls_schedule_get"](CHAT_MK)
        check("(setup) end moved to 10:00", (start0, end0) == ("08:00", "10:00"), (start0, end0))

        cb = FakeCallbackEvent(f"cls:h:start:{CHAT_MK}:10".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](cb)
        start1, end1 = ns["_cls_schedule_get"](CHAT_MK)
        check("7. start == end via hour picker is rejected (schedule unchanged)", (start1, end1) == (start0, end0), (start1, end1))
        check(
            "7. rejected hour pick answers with the exact required message",
            cb.answers and cb.answers[-1][0] == "Начало должно быть раньше конца. Выберите другое время.",
            cb.answers,
        )
        check("7. rejected hour pick does NOT edit the message", cb.edits == [], cb.edits)

        cb2 = FakeCallbackEvent(f"cls:h:start:{CHAT_MK}:15".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](cb2)
        start2, end2 = ns["_cls_schedule_get"](CHAT_MK)
        check("7. start > end via hour picker is also rejected (schedule unchanged)", (start2, end2) == (start0, end0), (start2, end2))
    finally:
        os.unlink(db_path)


async def test_8_hour_pick_forged_callback_denied() -> None:
    """8. a forged hour-picker callback naming another closer's key (or sent
    by a non-closer) is denied/no-op; an out-of-range hour is rejected."""
    ns, db_path = _fresh_mb_ns()
    try:
        forged = FakeCallbackEvent(f"cls:h:start:{OTHER_CLOSER_MK}:9".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](forged)
        check("8. forged hour callback for another closer's key is denied", forged.answers and forged.answers[-1][1] is True, forged.answers)
        check("8. forged hour callback never edits any message", forged.edits == [], forged.edits)
        other_start, other_end = ns["_cls_schedule_get"](OTHER_CLOSER_MK)
        check("8. the target closer's own schedule is completely untouched", (other_start, other_end) == ("08:00", "17:00"), (other_start, other_end))

        forged2 = FakeCallbackEvent(f"cls:h:start:{CHAT_MK}:9".encode("utf-8"), NORMAL_UID)
        await ns["_cls_callback"](forged2)
        check("8. non-closer sending an hour-picker callback is denied", forged2.answers and forged2.answers[-1][1] is True, forged2.answers)
        check("8. non-closer hour-picker callback never edits any message", forged2.edits == [], forged2.edits)

        bad_hour = FakeCallbackEvent(f"cls:h:start:{CHAT_MK}:99".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](bad_hour)
        check(
            "(extra) an out-of-range hour value is rejected (alert, no edit)",
            bad_hour.answers and bad_hour.answers[-1][1] is True and bad_hour.edits == [],
            (bad_hour.answers, bad_hour.edits),
        )
    finally:
        os.unlink(db_path)


def test_11_no_source_schedule_tables_touched() -> None:
    """11. the closer block only ever writes manager_client_message_schedule
    for the closer's own manager_key -- never source_message_schedule,
    source_work_schedule/windows, or manager_work_schedule_days."""
    block = _block_text(MANAGER_BOT_PATH, MB_BLOCK_START, MB_BLOCK_END)
    for forbidden_table in ("source_message_schedule", "source_work_schedule", "source_work_windows", "manager_work_schedule_days"):
        check(
            f"11. closer block never writes to '{forbidden_table}'",
            f"INTO {forbidden_table}" not in block and f"UPDATE {forbidden_table}" not in block,
            forbidden_table,
        )
    check("11. closer block DOES write the intended manager_client_message_schedule table", "INTO manager_client_message_schedule" in block)


async def test_8_preset_saves_both_times() -> None:
    ns, db_path = _fresh_mb_ns()
    try:
        ev = FakeCallbackEvent(f"cls:p:1200_2100:{CHAT_MK}".encode("utf-8"), CLOSER_UID)
        await ns["_cls_callback"](ev)
        start, end = ns["_cls_schedule_get"](CHAT_MK)
        check("8. preset saves BOTH start and end", (start, end) == ("12:00", "21:00"), (start, end))
        check("8. preset click edits the schedule screen in place", len(ev.edits) == 1, ev.edits)
    finally:
        os.unlink(db_path)


async def test_9_10_11_text_wizard() -> None:
    ns, db_path = _fresh_mb_ns()
    try:
        ev = FakeMessageEvent("", CLOSER_UID)  # matches production: called from the NewMessage pending handler

        # 9. greeting text wizard saves only after confirmation.
        await ns["_cls_handle_text_input"](ev, CLOSER_UID, {"manager_key": CHAT_MK, "field": "greet"}, "  Привет!\nМы скоро ответим.  ")
        check("9. text captured into a draft, NOT written to KV yet", ns["_cls_get_setting"](ns["_cls_greeting_key"](CHAT_MK)) == "")
        save_ev = FakeCallbackEvent(b"cls:save", CLOSER_UID)
        await ns["_cls_callback"](save_ev)
        saved = ns["_cls_get_setting"](ns["_cls_greeting_key"](CHAT_MK))
        check("9. confirmed greeting text is stored EXACTLY (outer whitespace stripped, inner newline kept)", saved == "Привет!\nМы скоро ответим.", repr(saved))

        # 10. away text wizard saves only after confirmation.
        await ns["_cls_handle_text_input"](ev, CLOSER_UID, {"manager_key": CHAT_MK, "field": "away"}, "Мы сейчас не на месте.")
        check("10. away text captured into a draft, NOT written to KV yet", ns["_cls_get_setting"](ns["_cls_away_key"](CHAT_MK)) == "")
        save_ev2 = FakeCallbackEvent(b"cls:save", CLOSER_UID)
        await ns["_cls_callback"](save_ev2)
        saved_away = ns["_cls_get_setting"](ns["_cls_away_key"](CHAT_MK))
        check("10. confirmed away text is stored exactly", saved_away == "Мы сейчас не на месте.", repr(saved_away))

        # 11. cancel does not save.
        await ns["_cls_handle_text_input"](ev, CLOSER_UID, {"manager_key": CHAT_MK, "field": "greet"}, "ЭТОТ ТЕКСТ НЕ ДОЛЖЕН СОХРАНИТЬСЯ")
        cancel_ev = FakeCallbackEvent(b"cls:cancel", CLOSER_UID)
        await ns["_cls_callback"](cancel_ev)
        still_saved = ns["_cls_get_setting"](ns["_cls_greeting_key"](CHAT_MK))
        check("11. cancel does not overwrite the previously saved text", still_saved == "Привет!\nМы скоро ответим.", repr(still_saved))
    finally:
        os.unlink(db_path)


async def test_12_preview_shows_current_settings() -> None:
    ns, db_path = _fresh_mb_ns()
    try:
        default_view = ns["_cls_view_text"](CHAT_MK)
        check("12. preview shows fallback wording when no custom text is set", "по умолчанию" in default_view, default_view)

        ns["_cls_set_setting"](ns["_cls_greeting_key"](CHAT_MK), "Моё приветствие")
        ns["_cls_set_setting"](ns["_cls_away_key"](CHAT_MK), "Моё нет-на-месте")
        ns["_cls_schedule_set"](CHAT_MK, "10:00", "19:00", CLOSER_UID)
        view = ns["_cls_view_text"](CHAT_MK)
        check("12. preview shows the current schedule", "10:00" in view and "19:00" in view, view)
        check("12. preview shows the current greeting text", "Моё приветствие" in view, view)
        check("12. preview shows the current away text", "Моё нет-на-месте" in view, view)
    finally:
        os.unlink(db_path)


MAIN_NAMES = {"_tp_is_closer_manager", "_tp_closer_setting_get", "_tp_closer_text", "_TP_CLOSER_ROLE_CACHE", "_TP_CLOSER_ROLE_CACHE_TTL_SEC"}


def build_main_ns(db_path: str) -> dict:
    return _extract_and_exec(
        MAIN_PATH,
        MAIN_NAMES,
        {
            "time": __import__("time"),
            "TPILOT_DB_PATH": db_path,
            "Dict": dict, "Any": object,
        },
    )


def test_13_14_16_auto_reply_integration() -> None:
    db_path = _make_temp_db()
    try:
        con = sqlite3.connect(db_path)
        con.execute(
            "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')"
        )
        con.execute("INSERT INTO managers(manager_key, role) VALUES ('vinch','closer')")
        con.execute("INSERT INTO managers(manager_key, role) VALUES ('alpha01','manager')")
        con.execute(
            "INSERT INTO settings(key, value) VALUES ('closer_greeting_text_vinch', 'Custom greeting')"
        )
        con.execute(
            "INSERT INTO settings(key, value) VALUES ('closer_away_text_vinch', 'Custom away')"
        )
        con.commit()
        con.close()

        ns = build_main_ns(db_path)
        closer_text = ns["_tp_closer_text"]

        check("13. closer WITH custom greeting -> custom text is used inside work window", closer_text("vinch", "profile_question", "DEFAULT_GREETING") == "Custom greeting")
        check("13/away. closer WITH custom away -> custom text is used outside work window", closer_text("vinch", "offline_notice", "DEFAULT_AWAY") == "Custom away")

        # 14. no custom text for this manager -> falls back to the existing default.
        check("14. closer WITHOUT custom text falls back to the default", closer_text("nobody_closer_here", "profile_question", "DEFAULT_GREETING") == "DEFAULT_GREETING")

        # 16. a normal (non-closer) manager is NEVER affected, even if a
        # closer_*_text_ KV key happens to exist under their manager_key
        # (defensive: role gate is checked FIRST, before any KV read).
        con = sqlite3.connect(db_path)
        con.execute("INSERT INTO settings(key, value) VALUES ('closer_greeting_text_alpha01', 'Should never be used')")
        con.commit()
        con.close()
        check("16. normal manager (role != closer) always gets the default text, regardless of KV content", closer_text("alpha01", "profile_question", "DEFAULT_GREETING") == "DEFAULT_GREETING")
        check("16. unrelated/unknown manager_key also always gets the default", closer_text("totally_unknown", "offline_notice", "DEFAULT_AWAY") == "DEFAULT_AWAY")
    finally:
        os.unlink(db_path)


def test_cls_ordered_before_prefix_gate() -> None:
    """Static confirmation that the cls: early-return in the broad
    on_callback dispatcher sits BEFORE the generic CALLBACK_PREFIX gate --
    the exact ordering the micro-fix requires (matches the existing ss:
    delegation style)."""
    src = open(MANAGER_BOT_PATH, encoding="utf-8-sig").read()
    cls_idx = src.index('if data.startswith(b"cls:"):')
    prefix_gate_idx = src.index("if not data.startswith(CALLBACK_PREFIX):")
    check("cls: early-return appears BEFORE the generic CALLBACK_PREFIX gate in on_callback", cls_idx < prefix_gate_idx, (cls_idx, prefix_gate_idx))


async def test_cls_delegated_from_broad_on_callback() -> None:
    """Behavioral confirmation: calling the REAL (extracted) broad
    on_callback with cls: data returns immediately without answering or
    editing anything -- ownership of the toast/answer stays entirely with
    the dedicated _cls_callback handler."""
    ns = _extract_and_exec(
        MANAGER_BOT_PATH,
        {"on_callback", "CALLBACK_PREFIX"},
        {
            "client": FakeClient(),
            "events": FakeEventsNS,
            "log": FakeLog(),
        },
    )
    ev = FakeCallbackEvent(b"cls:menu", CLOSER_UID)
    await ns["on_callback"](ev)
    check("broad on_callback delegates cls:menu via early return (no answer/edit from the generic handler)", ev.answers == [] and ev.edits == [], (ev.answers, ev.edits))

    ev2 = FakeCallbackEvent(f"cls:sched:{CHAT_MK}".encode("utf-8"), CLOSER_UID)
    await ns["on_callback"](ev2)
    check("broad on_callback delegates other cls: subpaths too (e.g. cls:sched:...)", ev2.answers == [] and ev2.edits == [], (ev2.answers, ev2.edits))


def test_dbguard() -> None:
    """Direct unit test of _selftest_db_guard's own logic against every
    unsafe scenario, without ever touching a real DB or creating a file
    under the project's db/."""
    prod_dir = os.path.join(str(BASE_DIR), "db")
    prod_file = os.path.join(prod_dir, "data_tpilot.db")
    prod_nested = os.path.join(prod_dir, "nested", "sub", "x.db")
    temp_root = tempfile.mkdtemp(prefix="dbguard_closer_settings_probe_")
    safe_path = os.path.join(temp_root, "safe.db")
    outside_temp_root_path = os.path.join(tempfile.gettempdir(), "dbguard_closer_settings_outside.db")
    sibling_path = os.path.join(str(BASE_DIR), "dbfoo", "x.db")

    def _raises(db_path, temp_root_arg: str = "") -> bool:
        try:
            _selftest_db_guard(db_path, BASE_DIR, temp_root=temp_root_arg)
            return False
        except AssertionError:
            return True

    check("dbguard-1. valid temp path passes", not _raises(safe_path))
    check("dbguard-2. production DB file rejected", _raises(prod_file))
    check("dbguard-3. nested production DB path rejected", _raises(prod_nested))
    check("dbguard-3b. bare production db/ directory itself rejected", _raises(prod_dir))
    check("dbguard-7. similarly named sibling directory ('dbfoo') allowed", not _raises(sibling_path))
    prod_mtime_before = os.path.getmtime(prod_file) if os.path.exists(prod_file) else None
    _raises(prod_file)
    prod_mtime_after = os.path.getmtime(prod_file) if os.path.exists(prod_file) else None
    check("dbguard-8. the guard never opens/modifies the real production file", prod_mtime_before == prod_mtime_after, (prod_mtime_before, prod_mtime_after))
    check("dbguard-9. path outside its declared temp_root is rejected", _raises(outside_temp_root_path, temp_root_arg=temp_root))
    check("dbguard-10. path inside its declared temp_root passes", not _raises(safe_path, temp_root_arg=temp_root))
    probe_db = _make_temp_db()
    try:
        check("dbguard-11. _make_temp_db actually produces a guarded, non-production temp path", not str(Path(probe_db)).lower().startswith(prod_dir.lower()))
    finally:
        os.unlink(probe_db)
    try:
        os.rmdir(temp_root)
    except Exception:
        pass


async def main() -> int:
    test_dbguard()
    test_18_no_destructive_sql()
    test_19_no_adminbot_partnerbot_changes()
    test_cls_ordered_before_prefix_gate()
    await test_cls_delegated_from_broad_on_callback()
    await test_1_2_menu_visibility()
    await test_3_15_forged_callback_denied()
    await test_1_2_hour_picker_schedule_screen_buttons()
    test_3_4_hour_picker_renders_24_buttons()
    await test_5_6_hour_pick_updates_only_one_side()
    await test_7_hour_pick_start_ge_end_rejected()
    await test_8_hour_pick_forged_callback_denied()
    test_11_no_source_schedule_tables_touched()
    await test_8_preset_saves_both_times()
    await test_9_10_11_text_wizard()
    await test_12_preview_shows_current_settings()
    test_13_14_16_auto_reply_integration()

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
