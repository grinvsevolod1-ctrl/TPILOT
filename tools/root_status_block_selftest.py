# -*- coding: utf-8 -*-
"""tools/root_status_block_selftest.py -- N5.3 Part F gate: the root
service-status block shows SIX indicators in the owner-ordered sequence
(TPilot, ManagerBot, PartnerBot, Менеджеры, Watchdog, Proxy), adds the new
ManagerBot liveness line, keeps the existing green/yellow/red Managers
semantics (expected = DB active+enabled+not-manually-stopped, archived
excluded), and pays for the ManagerBot/PartnerBot process detection with ONE
shared TTL-cached scan instead of the pre-N5.3 fresh-PowerShell-scan-per-
render behavior.

All checks run the REAL extracted functions (ast.parse+unparse+exec) against
deterministic fakes for the environment boundaries only (process list,
status-file health dict, clock, proxy counts) -- никакой PowerShell, никакой
сети, никакого спенда, никакого продакшн-DB.

    python tools\\root_status_block_selftest.py
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
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


def _last_def(name: str):
    node = None
    for n in TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            node = n
    if node is None:
        raise AssertionError(f"no def {name} found")
    return node


def _last_assign(name: str):
    node = None
    for n in TREE.body:
        if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id == name:
            node = n
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == name:
            node = n
    if node is None:
        raise AssertionError(f"no assign {name} found")
    return node


# N5.3.1: the exact owner-approved quoted title (Unicode mathematical bold).
EXPECTED_TITLE_BODY = "𝐓𝐏𝐢𝐥𝐨𝐭 𝐀𝐝𝐦𝐢𝐧 𝐏𝐚𝐧𝐞𝐥"
EXPECTED_TITLE_GREEN = "🟢" + EXPECTED_TITLE_BODY


def build_header_ns(*, procs, hp, proxy=(2, 3, "🟡"), balance=None, low=False):
    """Real last-def _panel_header + the N5.3/N5.3.1 helpers, with fakes ONLY
    at the environment boundaries. `procs` is what the fake process scan
    returns (list of {'cmd': ...} dicts, or None to simulate scan failure);
    `hp` is the _tp_visual_health_parts result dict; `balance` pre-seeds the
    panel-side proxy-balance cache (None = never sighted -> renders "—")."""
    nodes = [
        _last_assign("_N531_TITLE_BODY"),
        _last_assign("_N531_PROXY_BALANCE_CACHE"),
        _last_assign("_N53_SERVICE_SCAN_CACHE"),
        _last_assign("_N53_SERVICE_SCAN_TTL_SEC"),
        # Forward-fix P2 (post-incident review 2026-07-26): _panel_header now
        # calls _pb_proxy_counts_effective (the canonical shared-resolver
        # count), not _tpag_panel_v2_proxy_counts -- and _pb_cached_proxy_
        # balance_str/_pb_proxy_indicator_icon now go through _pb_proxy_
        # balance_value -> _pb_persisted_proxy_balance (persisted-snapshot-
        # first, in-memory-cache fallback) instead of reading the cache
        # directly. Extracted for real; _connect_panel_db is intentionally
        # left UNFAKED (NameError inside _pb_persisted_proxy_balance's own
        # try/except fail-open -> None), so these tests exercise the exact
        # same in-memory-cache fallback path they always have.
        _last_def("_pb_persisted_proxy_balance"),
        _last_def("_pb_proxy_balance_value"),
        _last_def("_pb_cached_proxy_balance_str"),
        _last_def("_pb_proxy_indicator_icon"),
        _last_def("_pb_service_scan_cached"),
        _last_def("_pb_service_proc_running"),
        _last_def("_n53_line_icon"),
        _last_def("_panel_header"),
        _last_def("_pb_render_with_title_quote"),
    ]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    calls = {"scan": 0}

    def _fake_get_procs():
        calls["scan"] += 1
        if procs is None:
            raise RuntimeError("scan failed (simulated)")
        return procs

    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _zi
    ns = {
        "Dict": dict, "Any": object,
        "BASE_DIR": BASE_DIR,
        "datetime": _dt, "ZoneInfo": _zi,
        "_norm_path": lambda s: str(s or "").replace("\\", "/").lower(),
        "_get_python_processes": _fake_get_procs,
        "_tp_visual_health_parts": lambda: dict(hp),
        "_pb_proxy_counts_effective": lambda: proxy,
        # N5.3.2 (M16): the low-balance flag reader is faked at the boundary
        # (its REAL sqlite behavior is unit-tested separately in test_7).
        "_pb_proxy_balance_low": lambda: bool(low),
        "_N53_HDR_PREV_PANEL_HEADER": lambda: "PREV_HEADER",
    }
    exec(compile(module_src, f"<{PANEL_PATH}:status_block>", "exec"), ns)
    ns["_pb_proxy_balance_low"] = lambda: bool(low)
    if balance is not None:
        ns["_N531_PROXY_BALANCE_CACHE"]["value"] = float(balance)
    ns["_scan_calls"] = calls
    return ns


HP_ALL_OK = {"overall": "🟢", "tpilot": "🟢", "watchdog": "🟢", "partner": "🟢",
             "managers": "🟢", "running": 3, "total": 3}


def _mk_proc(script: str) -> dict:
    return {"cmd": f"C:/ALM_TPilot/venv/python.exe {BASE_DIR}/{script}"}


ALL_PROCS = [_mk_proc("main.py"), _mk_proc("manager_bot.py"), _mk_proc("partner_stat_bot.py")]


# ======================================================================
# 1. N5.3.1 exact layout: quoted title, date line, five indicators in the
#    exact owner order, colon syntax, monospace values -- asserted as exact
#    RAW LINES in exact positions, never whole-file substring checks.
# ======================================================================

def test_1_exact_layout() -> None:
    ns = build_header_ns(procs=ALL_PROCS, hp=HP_ALL_OK, proxy=(3, 3, "🟢"), balance=34.80)
    text = ns["_panel_header"]()
    lines = text.splitlines()

    check("1. line 0 is EXACTLY the quoted title (🟢 + Unicode bold, no markdown, no padding)",
          lines[0] == EXPECTED_TITLE_GREEN, repr(lines[0]))
    check("1. title uses the exact Unicode mathematical-bold codepoints",
          all(ch in lines[0] for ch in "𝐓𝐏𝐢𝐥𝐨𝐭") and "TPilot" not in lines[0], repr(lines[0]))
    check("1. line 1 is blank (title separated from date)", lines[1] == "", repr(lines[1:2]))
    import re as _re
    check("1. line 2 is the date line: 📅 Дата: DD.MM.YYYY — `[HH:MM:SS]`",
          _re.fullmatch(r"📅 Дата: \d{2}\.\d{2}\.\d{4} — `\[\d{2}:\d{2}:\d{2}\]`", lines[2]) is not None,
          repr(lines[2]))
    check("1. line 3 is blank (date separated from indicators)", lines[3] == "", repr(lines[3:4]))

    check("1. indicator lines 4-8 are EXACTLY PartnerBot, ManagerBot, Managers, Proxy, Watchdog in order",
          lines[4] == "🟢 PartnerBot"
          and lines[5] == "🟢 ManagerBot"
          and lines[6] == "🟢 Managers: `3/3`"
          and lines[7] == "🟢 Proxy: `$34.80`"
          and lines[8] == "🟢 Watchdog",
          lines[4:9])
    check("1. exactly 9 lines total (no extra indicator, no separate TPilot line)",
          len(lines) == 9, lines)
    check("1. NO separate TPilot indicator anywhere below the title",
          not any(l.strip().endswith("TPilot") or l.startswith("🟢 TPilot") or l.startswith("🔴 TPilot")
                  for l in lines[1:]), lines)
    check("1. colon used ONLY where a value follows (PartnerBot/ManagerBot/Watchdog have no colon)",
          ":" not in lines[4] and ":" not in lines[5] and ":" not in lines[8], lines[4:9])
    check("1. Managers value is monospace (backticks)", "`3/3`" in lines[6], repr(lines[6]))
    check("1. Proxy value is monospace dollars (backticks)", "`$34.80`" in lines[7], repr(lines[7]))

    # --- N5.3.2 (M16): Proxy indicator color policy, all three states.
    # Unknown balance -> 🟡 + `—` (an unknown value must NEVER look green,
    # even when auth-guard health is green).
    ns2 = build_header_ns(procs=ALL_PROCS, hp=HP_ALL_OK, proxy=(3, 3, "🟢"))
    line7 = ns2["_panel_header"]().splitlines()[7]
    check("1. M16-unknown: Proxy renders 🟡 + `—` before first balance sighting (never green, no provider call)",
          line7 == "🟡 Proxy: `—`", repr(line7))
    # Known balance below the alert threshold -> 🔴 with the value shown.
    ns3 = build_header_ns(procs=ALL_PROCS, hp=HP_ALL_OK, proxy=(3, 3, "🟢"), balance=3.10, low=True)
    line7_low = ns3["_panel_header"]().splitlines()[7]
    check("1. M16-low: Proxy renders 🔴 + `$3.10` when the existing below-threshold alert flag is set",
          line7_low == "🔴 Proxy: `$3.10`", repr(line7_low))
    # Known healthy balance -> the existing auth-guard health icon passes through.
    ns4 = build_header_ns(procs=ALL_PROCS, hp=HP_ALL_OK, proxy=(1, 3, "🔴"), balance=34.80, low=False)
    line7_health = ns4["_panel_header"]().splitlines()[7]
    check("1. M16-healthy-value: known good balance keeps the auth-guard health icon (here 🔴 from proxy health)",
          line7_health == "🔴 Proxy: `$34.80`", repr(line7_health))


# ======================================================================
# 1b. Blockquote mechanism: real telethon markdown parse + a
#     MessageEntityBlockquote covering EXACTLY the title line.
# ======================================================================

def test_1b_blockquote_entity() -> None:
    ns = build_header_ns(procs=ALL_PROCS, hp=HP_ALL_OK, proxy=(3, 3, "🟢"), balance=12.5)
    text = ns["_panel_header"]() + "\n\n🧭 MENU"
    clean, entities = ns["_pb_render_with_title_quote"](text)
    check("1b. renderer returns entities for a title-led panel text", entities is not None, entities)
    first = entities[0] if entities else None
    check("1b. FIRST entity is MessageEntityBlockquote at offset 0",
          first is not None and type(first).__name__ == "MessageEntityBlockquote" and first.offset == 0,
          first)
    title_utf16 = len(clean.splitlines()[0].encode("utf-16-le")) // 2
    check("1b. blockquote length covers EXACTLY the title line (UTF-16 units)",
          first is not None and first.length == title_utf16, (getattr(first, "length", None), title_utf16))
    check("1b. monospace code entities survive the parse (values stay monospace)",
          any(type(e).__name__ == "MessageEntityCode" for e in entities), [type(e).__name__ for e in entities])
    check("1b. non-title text passes through untouched (delegation path)",
          ns["_pb_render_with_title_quote"]("обычный экран")[1] is None, None)

    # N5.3.3 (AA2): the review's mutation M11 (a second MessageEntityBlockquote
    # duplicated onto one render) passed every prior check -- none of them
    # asserted UNIQUENESS, only that the FIRST entity was a correctly-placed
    # blockquote. Close that gap explicitly.
    def _bq_count(ents) -> int:
        return sum(1 for e in (ents or []) if type(e).__name__ == "MessageEntityBlockquote")

    check("1b. [AA2] EXACTLY one MessageEntityBlockquote entity for the title-led text",
          _bq_count(entities) == 1, _bq_count(entities))

    def _utf16_slice(s: str, offset: int, length: int) -> str:
        u = s.encode("utf-16-le")
        return u[offset * 2:(offset + length) * 2].decode("utf-16-le")

    covered = _utf16_slice(clean, first.offset, first.length) if first else ""
    check("1b. [AA2] blockquote covers ONLY the title line (not the date/status lines below it)",
          covered == clean.splitlines()[0] and "\n" not in covered, repr(covered))

    # Repeated rendering (same source parsed again) must still yield exactly
    # one blockquote entity -- no accumulation across calls.
    _, entities_again = ns["_pb_render_with_title_quote"](text)
    check("1b. [AA2] repeated rendering still yields exactly one blockquote entity",
          _bq_count(entities_again) == 1, _bq_count(entities_again))

    # An ordinary non-title message must yield ZERO blockquote entities
    # (the delegation path already returns entities=None -- verify that is
    # equivalent to a zero blockquote count, not just "None").
    _, entities_plain = ns["_pb_render_with_title_quote"]("обычный экран")
    check("1b. [AA2] an ordinary non-title message yields zero blockquote entities",
          _bq_count(entities_plain) == 0, _bq_count(entities_plain))
    # The real senders must route through this mechanism.
    send_src = ast.unparse(_last_def("_send_fresh_panel"))
    edit_src = ast.unparse(_last_def("_safe_event_edit"))
    check("1b. active _send_fresh_panel uses _pb_render_with_title_quote + formatting_entities",
          "_pb_render_with_title_quote" in send_src and "formatting_entities" in send_src, send_src[:200])
    check("1b. active _safe_event_edit uses _pb_render_with_title_quote + formatting_entities",
          "_pb_render_with_title_quote" in edit_src and "formatting_entities" in edit_src, edit_src[:200])
    check("1b. no fake centering with spaces anywhere in the title",
          not clean.splitlines()[0].startswith(" ") and "  " not in clean.splitlines()[0], repr(clean.splitlines()[0]))


# ======================================================================
# 1c. Root screen composition: header + 🧭 MENU, no breadcrumb, no
#     migration-era explanation.
# ======================================================================

def test_1c_root_composition() -> None:
    root_node = _last_def("_nm_root_screen")
    helpers = [_last_def("_nm_btn"), _last_def("_nm_row")]
    src = "\n\n".join(ast.unparse(n) for n in helpers + [root_node])

    class _FakeBtn:
        def __init__(self, text, data):
            self.text = text
            self.data = data if isinstance(data, bytes) else str(data).encode()

    class _FakeButton:
        @staticmethod
        def inline(text, data):
            return _FakeBtn(text, data)

    ns = {"Button": _FakeButton, "Tuple": tuple, "List": list,
          "_panel_header": lambda: "HDRLINE"}
    exec(compile(src, "<root>", "exec"), ns)
    text, rows = ns["_nm_root_screen"]()
    lines = text.splitlines()
    check("1c. root text is EXACTLY header + blank + 🧭 MENU (3 lines)",
          lines == ["HDRLINE", "", "🧭 MENU"], lines)
    check("1c. root has NO breadcrumb/Путь block", "Путь:" not in text, text)
    check("1c. root has NO «Админ-бот → Новое меню» breadcrumb", "Админ-бот" not in text and "Новое меню" not in text, text)
    check("1c. root has NO migration explanation («Выберите раздел…»)", "Выберите раздел" not in text, text)
    check("1c. root still renders the canonical buttons (12)", sum(len(r) for r in rows) == 12,
          sum(len(r) for r in rows))


# ======================================================================
# 2. ManagerBot / PartnerBot detection from the shared scan.
# ======================================================================

def test_2_managerbot_partnerbot_detection() -> None:
    ns = build_header_ns(procs=ALL_PROCS, hp=HP_ALL_OK)
    text = ns["_panel_header"]()
    check("2. ManagerBot 🟢 when manager_bot.py process present", "🟢 ManagerBot" in text, text)
    check("2. PartnerBot 🟢 when partner_stat_bot.py process present", "🟢 PartnerBot" in text, text)

    ns2 = build_header_ns(procs=[_mk_proc("main.py")], hp=HP_ALL_OK)
    text2 = ns2["_panel_header"]()
    check("2. ManagerBot 🔴 when manager_bot.py absent from scan", "🔴 ManagerBot" in text2, text2)
    check("2. PartnerBot 🔴 when partner_stat_bot.py absent from scan", "🔴 PartnerBot" in text2, text2)

    # Fail-open: scan failure -> 🟡 (нет данных), never a false 🔴.
    ns3 = build_header_ns(procs=None, hp=HP_ALL_OK)
    text3 = ns3["_panel_header"]()
    check("2. ManagerBot 🟡 (fail-open) when the scan itself fails", "🟡 ManagerBot" in text3, text3)
    check("2. PartnerBot 🟡 (fail-open) when the scan itself fails", "🟡 PartnerBot" in text3, text3)

    # A foreign manager_bot.py OUTSIDE BASE_DIR must not count.
    ns4 = build_header_ns(procs=[{"cmd": "C:/other/project/manager_bot.py"}], hp=HP_ALL_OK)
    text4 = ns4["_panel_header"]()
    check("2. manager_bot.py from a DIFFERENT directory does not count", "🔴 ManagerBot" in text4, text4)


# ======================================================================
# 3. TTL cache: many renders -> ONE scan.
# ======================================================================

def test_3_ttl_cache_single_scan() -> None:
    ns = build_header_ns(procs=ALL_PROCS, hp=HP_ALL_OK)
    for _ in range(5):
        ns["_panel_header"]()
    check("3. five header renders cost exactly ONE process scan (60s TTL cache)",
          ns["_scan_calls"]["scan"] == 1, ns["_scan_calls"])


# ======================================================================
# 4. Managers green/yellow/red semantics -- REAL _tp_visual_health_parts
#    with deterministic _panel_health fakes.
# ======================================================================

def build_health_parts_ns(health: dict, partner_ok: bool = True):
    nodes = [_last_def("_tp_visual_health_parts")]
    src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = {
        "_panel_health": lambda: dict(health),
        "_tp_visual_partner_ok": lambda: partner_ok,
        "_tp_visual_status_icon": lambda ok: "🟢" if ok else "🔴",
    }
    exec(compile(src, "<health_parts>", "exec"), ns)
    return ns


def test_4_managers_icon_semantics() -> None:
    base = {"tpilot_ok": True, "watchdog_ok": True}
    green = build_health_parts_ns({**base, "active_managers": ["a", "b"], "manager_running": 2})["_tp_visual_health_parts"]()
    check("4. green when running == total (2/2)", green["managers"] == "🟢" and green["running"] == 2, green)
    yellow = build_health_parts_ns({**base, "active_managers": ["a", "b", "c"], "manager_running": 1})["_tp_visual_health_parts"]()
    check("4. yellow when only part is running (1/3)", yellow["managers"] == "🟡", yellow)
    red = build_health_parts_ns({**base, "active_managers": ["a", "b"], "manager_running": 0})["_tp_visual_health_parts"]()
    check("4. red when none of the expected managers run (0/2)", red["managers"] == "🔴", red)
    none_exp = build_health_parts_ns({**base, "active_managers": [], "manager_running": 0})["_tp_visual_health_parts"]()
    check("4. neutral 🟡 when zero managers are expected", none_exp["managers"] == "🟡", none_exp)


# ======================================================================
# 5. "Expected managers" exclusion semantics -- REAL manager_registry
#    query against a temp SQLite DB: archived / disabled / manually
#    stopped managers are never counted as expected.
# ======================================================================

def test_5_expected_exclusion_semantics() -> None:
    import manager_registry
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="root_status_selftest_")
    os.close(fd)
    try:
        con = sqlite3.connect(db_path)
        con.executescript(
            """
            CREATE TABLE managers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                manager_key TEXT UNIQUE, display_name TEXT,
                role TEXT DEFAULT 'manager', status TEXT DEFAULT 'active',
                is_enabled INTEGER DEFAULT 1, manual_stopped INTEGER DEFAULT 0
            );
            INSERT INTO managers(manager_key, status, is_enabled, manual_stopped) VALUES
              ('ok1', 'active', 1, 0),
              ('ok2', 'active', 1, 0),
              ('arch1', 'archived', 1, 0),
              ('dis1', 'disabled', 0, 0),
              ('deldis', 'active', 0, 0),
              ('stopped1', 'active', 1, 1);
            """
        )
        con.commit()
        con.close()
        rows = manager_registry.list_manager_rows_from_db_sync(db_path, only_enabled=True, only_active=True)
        keys = sorted(str(r.get("manager_key")) for r in rows)
        check("5. expected set == {ok1, ok2}: archived, disabled, not-enabled and manually-stopped are all excluded "
              "(the exact query the watchdog's active_managers list is built from)",
              keys == ["ok1", "ok2"], keys)
    finally:
        os.unlink(db_path)


# ======================================================================
# 6. No network / no spend: the active header must reference only local
#    sources (scan cache, health parts, proxy DB counts).
# ======================================================================

def test_6_no_network_no_spend() -> None:
    hdr = ast.unparse(_last_def("_panel_header"))
    for banned in ("prolong_make", "make_ipv4", "provider.balance", "allow_spend",
                   "urllib", "requests.", "http", "ProxySeller"):
        check(f"6. active _panel_header never references {banned!r}", banned not in hdr, hdr[:200])
    check("6. active _panel_header uses the cached scan (not a fresh scan per render)",
          "_pb_service_scan_cached" in hdr and "_get_python_processes" not in hdr, hdr[:300])
    partner = ast.unparse(_last_def("_tp_visual_partner_ok"))
    check("6. active _tp_visual_partner_ok reads the shared cache too (pre-N5.3 it ran its own scan every render)",
          "_pb_service_scan_cached" in partner, partner)


# ======================================================================
# 7. N5.3.2 (M16): REAL _pb_proxy_balance_low against a temp sqlite --
#    reads the existing proxy_balance_alert_state row exactly like the
#    production code, fail-open when the table/row is absent.
# ======================================================================

def test_7_real_balance_low_reader() -> None:
    nodes = [_last_def("_pb_proxy_balance_low")]
    src = "\n\n".join(ast.unparse(n) for n in nodes)
    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="root_status_low_")
    os.close(fd)
    try:
        def _connect():
            con = sqlite3.connect(db_path)
            con.row_factory = sqlite3.Row
            return con
        ns = {"_connect_panel_db": _connect}
        exec(compile(src, "<balance_low>", "exec"), ns)
        low = ns["_pb_proxy_balance_low"]

        check("7. fail-open: missing table -> False (never a crash, never a false red)", low() is False, None)
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE proxy_balance_alert_state(id INTEGER PRIMARY KEY, below_threshold INTEGER NOT NULL DEFAULT 0, last_alert_at TEXT, updated_at TEXT)")
        con.execute("INSERT INTO proxy_balance_alert_state(id, below_threshold) VALUES(1, 0)")
        con.commit()
        check("7. flag 0 -> False", low() is False, None)
        con.execute("UPDATE proxy_balance_alert_state SET below_threshold=1 WHERE id=1")
        con.commit()
        check("7. flag 1 -> True (the existing low-balance alert signal, pure local read)", low() is True, None)
        con.close()
        # No provider/network tokens in the reader's CODE (docstring excluded
        # -- it legitimately documents "no provider call" in prose).
        node = _last_def("_pb_proxy_balance_low")
        body_nodes = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                       and isinstance(node.body[0].value, ast.Constant)) else node.body
        code_only = "\n".join(ast.unparse(n) for n in body_nodes)
        check("7. reader CODE is pure local sqlite (no provider/network/spend tokens)",
              not any(t in code_only for t in ("provider", "urllib", "requests.", "prolong", "allow_spend")), code_only)
    finally:
        os.unlink(db_path)


def main() -> int:
    test_1_exact_layout()
    test_1b_blockquote_entity()
    test_1c_root_composition()
    test_2_managerbot_partnerbot_detection()
    test_3_ttl_cache_single_scan()
    test_4_managers_icon_semantics()
    test_5_expected_exclusion_semantics()
    test_6_no_network_no_spend()
    test_7_real_balance_low_reader()

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
