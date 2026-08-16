# -*- coding: utf-8 -*-
"""tools/auto_status_disable_selftest.py -- offline self-test for the
"Automatic lead-status disable switch" patch (settings key
'auto_status_enabled') added to main.py / panel_bot.py.

Business rule under test: manual manager status (ManagerBot / lead_status_overrides
/ /lead fix) must always remain the single source of truth; the switch only gates
AUTOMATIC lead-status / quality-bucket writes.

Two complementary techniques, matching the project's own testing convention
(see tools/proxy_pool_selftest.py, tools/proxy_buy_flow_selftest.py):

  A/B) Real behavior via AST extraction + exec of the actual gate helpers
       (_autostatus_runtime_enabled / _autostatus_enabled_sync) and the actual
       _tp_pss_decide_from_facts derivation function straight out of main.py
       (main.py cannot be imported directly -- Telethon/env side effects at
       import time).
  C/D/E) Structural regex verification (same technique as the project's own
       "allow_spend=True appears in exactly 2 places" check) that every
       automatic-write guard point named in the audit actually contains the
       new guard call, and that the manual-status paths (ManagerBot, /lead fix)
       do NOT reference the new switch at all.

Pure/offline: no network, no Telegram, no real DB, no real API calls.

    python3.12 tools\\auto_status_disable_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

MAIN_PY = BASE_DIR / "main.py"
PANEL_BOT_PY = BASE_DIR / "panel_bot.py"
MANAGER_BOT_PY = BASE_DIR / "manager_bot.py"

FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def extract_and_exec(path: Path, names: set[str], extra_ns: dict) -> dict:
    """Same AST-extraction technique used by the other tools/*_selftest.py
    files: pull specific top-level def nodes out of an un-importable module
    by name, unparse+recompile just those, and exec into a seeded namespace."""
    src = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    nodes = [n for n in tree.body if getattr(n, "name", None) in names]
    found = {getattr(n, "name", None) for n in nodes}
    if found != names:
        raise AssertionError(f"expected {names}, found {found} in {path}")
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<{path.name}>", "exec"), ns)
    return ns


def find_defs(path: Path, name: str) -> list[ast.AST]:
    """Return every top-level def/async-def node with the given name (there
    may be several stacked override layers -- see CLAUDE.md override-stack
    convention)."""
    src = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(src)
    return [n for n in tree.body if getattr(n, "name", None) == name]


# ======================================================================
# GROUP A -- the gate helpers themselves, extracted from the real main.py
# ======================================================================

def run_group_a() -> None:
    print("\n-- Group A: _autostatus_runtime_enabled / _autostatus_enabled_sync (real code) --")

    class _FakeGetSetting:
        """Stand-in for storage.get_setting_from_db -- returns a scripted
        value, or raises if configured to simulate a DB error."""

        def __init__(self):
            self.value = "1"
            self.raise_error = False
            self.calls = 0

        async def __call__(self, db_path, key):
            self.calls += 1
            if self.raise_error:
                raise RuntimeError("simulated DB read error")
            return self.value

    fake_get_setting = _FakeGetSetting()

    def make_ns():
        ns = extract_and_exec(
            MAIN_PY,
            {"_autostatus_runtime_enabled", "_autostatus_enabled_sync"},
            {
                "Optional": object,  # only used as a type annotation subscript target
                "get_setting_from_db": fake_get_setting,
                "TPILOT_DB_PATH": "dummy_tpilot.db",
            },
        )
        # Pre-seed the module-level cache vars the extracted function closes
        # over via `global` -- these are plain assignments in the real file,
        # which ast-name-extraction (function/class defs only) does not pull
        # in, so we seed them directly instead (equivalent effect).
        ns["_AUTOSTATUS_RTGATE_CACHE"] = None
        ns["_AUTOSTATUS_RTGATE_CACHE_AT"] = 0.0
        ns["_AUTOSTATUS_RTGATE_TTL"] = 20.0
        return ns

    # A1: value "0" -> disabled
    ns = make_ns()
    fake_get_setting.value = "0"
    result = asyncio.run(ns["_autostatus_runtime_enabled"]())
    check("gate returns False when settings.auto_status_enabled == '0'", result is False)
    check("gate caches the result after a DB read", ns["_AUTOSTATUS_RTGATE_CACHE"] is False)

    # A2: value "1" -> enabled
    ns = make_ns()
    fake_get_setting.value = "1"
    result = asyncio.run(ns["_autostatus_runtime_enabled"]())
    check("gate returns True when settings.auto_status_enabled == '1'", result is True)

    # A3: missing/unexpected value -> enabled (backward-compatible default)
    ns = make_ns()
    fake_get_setting.value = ""
    result = asyncio.run(ns["_autostatus_runtime_enabled"]())
    check("gate returns True (enabled) when the settings key is missing/empty -- backward compatible", result is True)

    # A4: TTL caching -- second call within the TTL window must NOT re-read the DB
    ns = make_ns()
    fake_get_setting.value = "1"
    fake_get_setting.calls = 0
    asyncio.run(ns["_autostatus_runtime_enabled"]())
    calls_after_first = fake_get_setting.calls
    fake_get_setting.value = "0"  # change underlying value; should not matter yet
    result2 = asyncio.run(ns["_autostatus_runtime_enabled"]())
    check("gate does not re-read the DB within the TTL window", fake_get_setting.calls == calls_after_first)
    check("cached value (True) is returned even though the underlying setting flipped to '0'", result2 is True)

    # A5: DB read failure with no prior cache -> fail-safe True (preserve current behavior)
    ns = make_ns()
    fake_get_setting.raise_error = True
    result = asyncio.run(ns["_autostatus_runtime_enabled"]())
    check("gate fails safe to True (enabled) on a DB read error with no prior cache", result is True)
    fake_get_setting.raise_error = False

    # A6: DB read failure with a prior cached value -> preserve that cached value
    ns = make_ns()
    ns["_AUTOSTATUS_RTGATE_CACHE"] = False
    ns["_AUTOSTATUS_RTGATE_CACHE_AT"] = 0.0  # force-expire so the code path re-reads
    fake_get_setting.raise_error = True
    result = asyncio.run(ns["_autostatus_runtime_enabled"]())
    check("gate preserves the last-known cached value (False) on a DB read error", result is False)
    fake_get_setting.raise_error = False

    # A7: sync snapshot mirrors the cache, defaults to True (enabled) when never warmed
    ns = make_ns()
    check("_autostatus_enabled_sync() defaults to True (enabled) before any async gate call", ns["_autostatus_enabled_sync"]() is True)
    ns["_AUTOSTATUS_RTGATE_CACHE"] = False
    check("_autostatus_enabled_sync() reflects a cached False", ns["_autostatus_enabled_sync"]() is False)
    ns["_AUTOSTATUS_RTGATE_CACHE"] = True
    check("_autostatus_enabled_sync() reflects a cached True", ns["_autostatus_enabled_sync"]() is True)


# ======================================================================
# GROUP B -- the real automatic-derivation function _tp_pss_decide_from_facts,
# proving it stops deriving ANY status when the switch is off, while manual
# override protection stays intact regardless of the switch.
# ======================================================================

def run_group_b() -> None:
    print("\n-- Group B: _tp_pss_decide_from_facts (real code) respects the switch --")

    class _DummyDatetime:
        @staticmethod
        def utcnow():
            import datetime as _dt
            return _dt.datetime(2026, 1, 1)

        # UTCNOW MIGRATION 20260815: product code now calls
        # .now(timezone.utc).replace(tzinfo=None) instead of .utcnow();
        # the stub must expose the same frozen instant via .now(tz).
        @staticmethod
        def now(tz=None):
            import datetime as _dt
            return _dt.datetime(2026, 1, 1, tzinfo=tz)

    ns = extract_and_exec(
        MAIN_PY,
        {
            "_tp_pss_decide_from_facts", "_tp_pss_now_iso", "_tp_pss_text",
            "_tp_pss_low", "_tp_pss_int_or_none", "_tp_pss_is_russia",
            "_autostatus_enabled_sync",
        },
        {"re": re, "_tp_pss_datetime": _DummyDatetime},
    )
    decide = ns["_tp_pss_decide_from_facts"]

    liquid_row = {"manual_status_override": 0, "age": 25, "country": "Россия", "city": "Москва"}
    trash_row = {"manual_status_override": 0, "trash": 1, "trash_reason": "spam"}

    # B1: switch ON (cache=True) -> normal derivation still works (regression guard)
    ns["_AUTOSTATUS_RTGATE_CACHE"] = True
    dec = decide(dict(liquid_row))
    check("auto_status ON: a clearly-liquid row still gets auto-derived to bucket=liquid", bool(dec) and dec.get("bucket") == "liquid", repr(dec))

    # B2: switch ON, never-warmed cache (None -> defaults enabled) -> same result
    ns["_AUTOSTATUS_RTGATE_CACHE"] = None
    dec = decide(dict(liquid_row))
    check("auto_status cache cold (never warmed) defaults to enabled -- same liquid derivation", bool(dec) and dec.get("bucket") == "liquid", repr(dec))

    # B3: switch OFF -> no automatic derivation at all, even for a clear-cut case
    ns["_AUTOSTATUS_RTGATE_CACHE"] = False
    dec = decide(dict(liquid_row))
    check("auto_status OFF: the exact same clearly-liquid row now returns None (no auto write)", dec is None, repr(dec))

    # B4: switch OFF also stops auto-trash-via-facts-sync
    dec = decide(dict(trash_row))
    check("auto_status OFF: a trash-shaped row also returns None (auto-trash stopped)", dec is None, repr(dec))

    # B5: manual_status_override=1 protection is independent of the switch (ON)
    ns["_AUTOSTATUS_RTGATE_CACHE"] = True
    dec = decide({**liquid_row, "manual_status_override": 1})
    check("manual_status_override=1 blocks auto-derivation when switch is ON (pre-existing protection intact)", dec is None, repr(dec))

    # B6: manual_status_override=1 protection also holds when the switch is OFF
    ns["_AUTOSTATUS_RTGATE_CACHE"] = False
    dec = decide({**liquid_row, "manual_status_override": 1})
    check("manual_status_override=1 still blocks derivation when switch is OFF (double-safe)", dec is None, repr(dec))


# ======================================================================
# GROUP C -- structural verification that every named automatic-write site
# in main.py actually contains the new guard (regex over the real source,
# same technique as the project's own allow_spend=True count check).
# ======================================================================

def run_group_c() -> None:
    print("\n-- Group C: structural guard-presence checks in main.py (real source) --")
    src = MAIN_PY.read_text(encoding="utf-8-sig")

    # C1: _update_daily_lead_fields recompute block (orig body)
    m = re.search(
        r"if int\(data\.get\(\"manual_status_override\"\) or 0\) == 1 or any\(k\.startswith\(\"quality_\"\) for k in data\.keys\(\)\):\s*\n\s*return\s*\n\s*if not await _autostatus_runtime_enabled\(\):\s*\n\s*return",
        src,
    )
    check("_update_daily_lead_fields recompute block is guarded right after the manual_status_override check", m is not None)

    # C2: _combine_profile decision injection
    m = re.search(r"def _combine_profile\(.*?\n(?:.*?\n){0,20}?\s*if _autostatus_enabled_sync\(\):\s*\n\s*row = dict\(existing or \{\}\)", src, re.DOTALL)
    check("_combine_profile wraps the _tp_qs_decide injection in the sync gate", m is not None)

    # C3: _tp_qs_repair_db short-circuit
    m = re.search(r"async def _tp_qs_repair_db\(.*?\n(?:.*?\n){0,5}?\s*if not await _autostatus_runtime_enabled\(\):\s*\n\s*return 0", src, re.DOTALL)
    check("_tp_qs_repair_db (/lead repair) short-circuits to 0 rows when disabled", m is not None)

    # C4: _unanswered_mark_trash daily_leads write is guarded
    m = re.search(
        r"if await _autostatus_runtime_enabled\(\):\s*\n\s*await _ensure_daily_leads_table\(db_path\)\s*\n\s*async with aiosqlite\.connect\(db_path\) as db:\s*\n\s*await db\.execute\(\"UPDATE daily_leads SET status='trash'",
        src,
    )
    check("_unanswered_mark_trash's daily_leads.status='trash' write is gated", m is not None)

    # C5: the ACTIVE (last) _apply_profile_from_text strips status/quality keys when disabled too
    apply_profile_defs = find_defs(MAIN_PY, "_apply_profile_from_text")
    check("at least one _apply_profile_from_text def found", len(apply_profile_defs) >= 1, str(len(apply_profile_defs)))
    last_def_src = ast.unparse(apply_profile_defs[-1]) if apply_profile_defs else ""
    check(
        "the ACTIVE (last) _apply_profile_from_text strips status/quality keys when manual override OR auto_status disabled",
        "manual_status_override" in last_def_src and "not await _autostatus_runtime_enabled()" in last_def_src,
    )

    # C6: cache warm-up in the manual /lead status_sync admin command
    m = re.search(r"async def _tp_pss_status_sync_command\(.*?\n\s*await _autostatus_runtime_enabled\(\)", src, re.DOTALL)
    check("_tp_pss_status_sync_command warms the sync-context gate snapshot before use", m is not None)

    # C7: _tp_pss_decide_from_facts itself is gated right after manual_status_override
    m = re.search(
        r'if int\(row\.get\("manual_status_override"\) or 0\) == 1:\s*\n\s*return None\s*\n\s*if not _autostatus_enabled_sync\(\):\s*\n\s*return None',
        src,
    )
    check("_tp_pss_decide_from_facts is gated right after its manual_status_override check", m is not None)

    # C8: count lock -- 11 real guard/warm-up call sites (update_daily_lead_fields,
    # combine_profile, repair_db, unanswered_mark_trash, decide_from_facts,
    # status_sync_command warm-up, apply_profile_from_text no-answer branch,
    # apply_profile_from_text parsed branch, base-body status="unknown" initializer,
    # _tpac_create_snapshot, _tpac_apply_snapshot) + the 2 `def name():` signature
    # lines themselves also match `name\(\)` -- 11 + 2 = 13 total occurrences.
    call_count = len(re.findall(r"_autostatus_runtime_enabled\(\)", src)) + len(re.findall(r"_autostatus_enabled_sync\(\)", src))
    check(
        "exactly 13 total occurrences (11 guard/warm-up call sites + 2 def signatures) of the gate helpers exist in main.py (regression lock; update this test if a future patch intentionally adds/removes a guard)",
        call_count == 13,
        f"found {call_count}",
    )

    # C9 (FAIL-1 fix): the active /lead repair snapshot path is gated
    m = re.search(
        r'async def _tpac_create_snapshot\(.*?\n\s*if not await _autostatus_runtime_enabled\(\):\s*\n\s*return "⛔ Автоматический статус выключен',
        src,
    )
    check("_tpac_create_snapshot (/lead repair dry, active snapshot path) is gated (FAIL-1 fix)", m is not None)
    m = re.search(
        r'async def _tpac_apply_snapshot\(.*?\n\s*if not await _autostatus_runtime_enabled\(\):\s*\n\s*return "⛔ Автоматический статус выключен',
        src,
    )
    check("_tpac_apply_snapshot (/lead repair apply, active snapshot path) is gated (FAIL-1 fix)", m is not None)

    # C10 (FAIL-2 fix): no-answer branch strips pending status/quality keys when disabled
    m = re.search(
        r'pending = _tp_pa_pending_fields\("Нет данных после первого сообщения клиента"\)\s*\n'
        r'\s*if not await _autostatus_runtime_enabled\(\):\s*\n'
        r'\s*for k in \(\s*\n'
        r'\s*"status", "nonliquid_reason", "profile_done",',
        src,
    )
    check("_apply_profile_from_text no-answer branch strips status/quality keys when auto_status disabled (FAIL-2 fix)", m is not None)

    # C11 (FAIL-2 fix): base-body status='unknown' initializer is gated
    m = re.search(
        r'if not str\(\(lead_row or \{\}\)\.get\("status"\) or ""\)\.strip\(\) and await _autostatus_runtime_enabled\(\):\s*\n'
        r'\s*await _update_daily_lead_fields\(DB_PATH,.*?status="unknown", profile_done=0\)',
        src,
    )
    check("the base-body status='unknown' initializer is gated by the switch (FAIL-2 fix)", m is not None)


# ======================================================================
# GROUP D -- the manual-status paths must NOT reference the new switch at all.
# ======================================================================

def run_group_d() -> None:
    print("\n-- Group D: manual-status paths are completely untouched --")

    # D1: every override layer of /lead (_tp_qs_handle_lead_command) is free of the
    # gate -- each layer either handles its own subcommand or delegates to a captured
    # PREV (override-stack convention), so only the base layer is expected to contain
    # the actual "fix" -> lead_status_overrides write; but NONE of the layers may
    # reference the new auto-status switch (the manual /lead fix path stays fully
    # independent of it, at every level of the stack).
    lead_fix_defs = find_defs(MAIN_PY, "_tp_qs_handle_lead_command")
    check("_tp_qs_handle_lead_command def(s) found in main.py", len(lead_fix_defs) >= 1)
    any_layer_writes_overrides = False
    for i, node in enumerate(lead_fix_defs):
        body_src = ast.unparse(node)
        check(
            f"/lead command handler layer #{i + 1} does not reference the auto-status gate (manual admin override stays independent)",
            "_autostatus_runtime_enabled" not in body_src and "_autostatus_enabled_sync" not in body_src,
        )
        if "lead_status_overrides" in body_src:
            any_layer_writes_overrides = True
    check(
        "at least one /lead command handler layer still writes lead_status_overrides (manual override contract unchanged)",
        any_layer_writes_overrides,
    )

    # D2: manager_bot.py (ManagerBot manual status buttons/writer) has zero references
    mb_src = MANAGER_BOT_PY.read_text(encoding="utf-8-sig")
    check(
        "manager_bot.py contains zero references to the new auto-status switch (ManagerBot untouched)",
        "auto_status_enabled" not in mb_src and "_autostatus_runtime_enabled" not in mb_src and "_autostatus_enabled_sync" not in mb_src,
    )
    check("manager_bot.py still writes lead_status_overrides (manual write path intact)", "lead_status_overrides" in mb_src)
    check("manager_bot.py still defines STATUS_ACTIONS (manual status button set intact)", "STATUS_ACTIONS" in mb_src)


# ======================================================================
# GROUP E -- panel_bot.py UI wiring (structural).
# ======================================================================

def run_group_e() -> None:
    print("\n-- Group E: panel_bot.py toggle button + handler wiring --")
    src = PANEL_BOT_PY.read_text(encoding="utf-8-sig")

    # E1: the automation menu button is wired to the setting + the new callback
    automation_defs = find_defs(PANEL_BOT_PY, "_tp_visual_automation_menu")
    check("_tp_visual_automation_menu def(s) found in panel_bot.py", len(automation_defs) >= 1)
    active_menu_src = ast.unparse(automation_defs[-1]) if automation_defs else ""
    check(
        "the ACTIVE _tp_visual_automation_menu reads settings.auto_status_enabled",
        '_pb_get_setting(\'auto_status_enabled\')' in active_menu_src or '_pb_get_setting("auto_status_enabled")' in active_menu_src,
    )
    # N3: the automation-screen button no longer emits the bare instant-flip
    # route -- it routes to the confirm screen with the target encoded.
    check(
        "the ACTIVE _tp_visual_automation_menu emits the N3 confirm-gated menu:autostatus_toggle_confirm:<target> callback",
        "menu:autostatus_toggle_confirm:" in active_menu_src,
    )
    check(
        "the ACTIVE _tp_visual_automation_menu status line still shows both ON/OFF wordings",
        "Автоматический статус: ВЫКЛ" in active_menu_src and "Автоматический статус: ВКЛ" in active_menu_src,
    )
    check(
        "N3: the action button label is now the opposite-action wording, not the status wording",
        "Выключить автостатусы" in active_menu_src and "Включить автостатусы" in active_menu_src,
    )

    # E2: the HISTORICAL bare "autostatus_toggle" route (no target) is kept working
    # verbatim (N3 contract: historical callbacks stay handled), comment lines tolerated.
    m = re.search(
        r'if raw == "autostatus_toggle":\s*\n'
        r'(?:\s*#[^\n]*\n)*'
        r'\s*current = _pb_get_setting\("auto_status_enabled"\)\s*\n'
        r'\s*new_val = "0" if current != "0" else "1"\s*\n'
        r'\s*_pb_set_setting\("auto_status_enabled", new_val\)',
        src,
    )
    check("the historical bare autostatus_toggle route still toggles settings.auto_status_enabled between '0' and '1'", m is not None)

    # N3: the new target-encoded confirm + do routes exist and both re-read
    # the setting fresh before comparing to the encoded target (stale no-op).
    m = re.search(r'if raw\.startswith\("autostatus_toggle_confirm:"\)', src)
    check("N3: autostatus_toggle_confirm:<target> route exists", m is not None)
    m = re.search(r'if raw\.startswith\("autostatus_toggle_do:"\)', src)
    check("N3: autostatus_toggle_do:<target> route exists", m is not None)
    m = re.search(r'if \(current != "0"\) == \(target != "0"\):\s*\n(?:\s*#[^\n]*\n)*\s*text, buttons = _autostatus_render_automation\(\)\s*\n\s*return "ℹ️ Состояние уже изменилось\.\\n\\n" \+ text, buttons', src)
    check("N3: the do: route no-ops with a stale-state notice when current already matches target", m is not None)

    # E3: the toggle handler is chain-safe (captures + delegates to the previous override,
    # matching the project's override-stack convention -- see CLAUDE.md section 4).
    m = re.search(r"_AUTOSTATUS_PREV_TITLE_FOR_MENU = globals\(\)\.get\(\"_title_for_menu\"\)", src)
    check("the new _title_for_menu override captures the previous chain link (_PREV pattern)", m is not None)
    m = re.search(r"if callable\(_AUTOSTATUS_PREV_TITLE_FOR_MENU\):\s*\n\s*return _AUTOSTATUS_PREV_TITLE_FOR_MENU\(menu\)", src)
    check("the new _title_for_menu override delegates unmatched menus to the previous chain link", m is not None)

    # E4: after toggling (historical route), it re-renders the "automation" screen in
    # place via the shared _autostatus_render_automation() helper (no message spam --
    # menu: dispatch always edits the existing message, never sends a new one).
    m = re.search(r'_pb_set_setting\("auto_status_enabled", new_val\)\s*\n\s*return _autostatus_render_automation\(\)', src)
    check("after toggling (historical route), the handler re-renders the 'automation' screen (edit-in-place, no spam)", m is not None)
    # N5.3 (D9): the shared render helper now renders the CANONICAL
    # Автоматизация screen (_nm_automation_screen) first -- the operator
    # stays in the canonical tree after every auto-status outcome -- and
    # only falls back to the PREV chain ("automation") when the canonical
    # builder is unavailable (test namespaces / partial extraction).
    m = re.search(r'def _autostatus_render_automation\(\)[^\n]*:', src)
    check("N3/N5.3: the shared render helper exists", m is not None)
    seg_start = src.find("def _autostatus_render_automation()")
    seg_end = src.find("\ndef ", seg_start + 10)
    seg = src[seg_start:seg_end] if seg_start != -1 else ""
    check("N5.3 (D9): the render helper prefers the CANONICAL _nm_automation_screen",
          '_nm_automation_screen' in seg)
    check("N5.3 (D9): the render helper keeps the PREV 'automation' fallback for chain safety",
          '_AUTOSTATUS_PREV_TITLE_FOR_MENU("automation")' in seg or "_AUTOSTATUS_PREV_TITLE_FOR_MENU('automation')" in seg)

    # E5 (fixed -- was stale/pre-existing-broken, unrelated to N3): "the
    # autostatus override is the LAST _title_for_menu def in the file" has
    # not been true since a later generation (baseline_managers, added in
    # Phase N1) became the true last def and simply delegates down to this
    # one via the PREV chain -- that is correct/expected override-stack
    # behavior, not a bug. The REAL invariant to verify: the autostatus
    # generation is reachable (exists) and no LATER generation re-defines/
    # shadows autostatus_toggle / autostatus_toggle_confirm: /
    # autostatus_toggle_do: before delegation reaches it (interception-safe,
    # same technique used in tools/manager_card_unified_selftest.py N2.1-2).
    all_defs = find_defs(PANEL_BOT_PY, "_title_for_menu")
    check("at least one _title_for_menu def found in panel_bot.py", len(all_defs) >= 1)
    autostatus_gen_idx = None
    for i, d in enumerate(all_defs):
        if "autostatus_toggle_confirm:" in ast.unparse(d):
            autostatus_gen_idx = i
    check("exactly one _title_for_menu generation owns the autostatus_toggle_confirm:/_do: routes", autostatus_gen_idx is not None)
    if autostatus_gen_idx is not None:
        later = all_defs[autostatus_gen_idx + 1:]
        shadowing = [d for d in later if "autostatus_toggle" in ast.unparse(d)]
        check("no LATER _title_for_menu generation re-handles autostatus_toggle (interception-proof)", not shadowing,
              [d.lineno for d in shadowing])


# ======================================================================
# GROUP F (FAIL-1 fix) -- the ACTIVE /lead repair snapshot path
# (_tpac_create_snapshot / _tpac_apply_snapshot) is genuinely gated: this
# executes the real shipped function bodies (AST-extracted) with NO stubs
# for their downstream dependencies (aiosqlite, _tpac_ensure_admin_schema,
# _tpac_calculate_changes, _tpac_snapshot_row, ...). If the new guard does
# NOT short-circuit first, the call will hit an unresolved name/attribute
# and raise -- so "returns the safe message cleanly" is only possible
# because the guard fired before touching any real dependency, and (as a
# negative control) "raises when the switch is ON" proves the same real
# functions still reach their original logic once re-enabled.
# ======================================================================

def run_group_f() -> None:
    print("\n-- Group F (FAIL-1 fix): active /lead repair snapshot path is gated --")

    ns = extract_and_exec(
        MAIN_PY,
        {"_tpac_create_snapshot", "_tpac_apply_snapshot", "_autostatus_runtime_enabled", "_autostatus_enabled_sync"},
        {},  # deliberately no stubs for aiosqlite / _tpac_* helpers -- see docstring above
    )
    ns["_AUTOSTATUS_RTGATE_TTL"] = 20.0

    import time as _time_mod

    # F1/F2: switch OFF -> both functions return the safe blocked message and
    # never touch any real (unstubbed) dependency -- no exception is raised.
    ns["_AUTOSTATUS_RTGATE_CACHE"] = False
    ns["_AUTOSTATUS_RTGATE_CACHE_AT"] = _time_mod.monotonic()
    result_create = asyncio.run(ns["_tpac_create_snapshot"]("today", "2026-01-01", "2026-01-01", "test", user_id=1))
    check(
        "auto_status OFF: _tpac_create_snapshot (/lead repair dry) returns the safe blocked message without touching any real dependency",
        result_create == "⛔ Автоматический статус выключен — пересчёт статусов недоступен.",
        repr(result_create),
    )

    ns["_AUTOSTATUS_RTGATE_CACHE"] = False
    ns["_AUTOSTATUS_RTGATE_CACHE_AT"] = _time_mod.monotonic()
    result_apply = asyncio.run(ns["_tpac_apply_snapshot"]("fake_snapshot_id", user_id=1))
    check(
        "auto_status OFF: _tpac_apply_snapshot (/lead repair apply) returns the safe blocked message without touching any real dependency",
        result_apply == "⛔ Автоматический статус выключен — пересчёт статусов недоступен.",
        repr(result_apply),
    )

    # F3/F4 (negative control): switch ON -> the SAME real functions must proceed
    # past the guard into their original logic, which immediately hits an
    # unresolved name (aiosqlite/_tpac_ensure_admin_schema/etc. were never
    # stubbed) and raises. This proves F1/F2 passed because the guard fired,
    # not because the functions are no-ops.
    ns["_AUTOSTATUS_RTGATE_CACHE"] = True
    ns["_AUTOSTATUS_RTGATE_CACHE_AT"] = _time_mod.monotonic()
    threw_create = False
    try:
        asyncio.run(ns["_tpac_create_snapshot"]("today", "2026-01-01", "2026-01-01", "test", user_id=1))
    except Exception:
        threw_create = True
    check(
        "auto_status ON: _tpac_create_snapshot proceeds past the guard into real logic (negative control -- proves the guard is a real gate, not dead code)",
        threw_create,
    )

    ns["_AUTOSTATUS_RTGATE_CACHE"] = True
    ns["_AUTOSTATUS_RTGATE_CACHE_AT"] = _time_mod.monotonic()
    threw_apply = False
    try:
        asyncio.run(ns["_tpac_apply_snapshot"]("fake_snapshot_id", user_id=1))
    except Exception:
        threw_apply = True
    check(
        "auto_status ON: _tpac_apply_snapshot proceeds past the guard into real logic (negative control -- proves the guard is a real gate, not dead code)",
        threw_apply,
    )


# ======================================================================
# GROUP G (FAIL-2 fix) -- the ACTIVE _apply_profile_from_text no-answer
# branch genuinely stops writing status/quality placeholder keys when the
# switch is off. Real function, AST-extracted; only I/O boundaries
# (schema ensure, message collection, the field writer) are stubbed --
# the field writer stub CAPTURES the kwargs it would have written so the
# test can inspect them directly, instead of guessing from source text.
# ======================================================================

def run_group_g() -> None:
    print("\n-- Group G (FAIL-2 fix): no-answer branch stops writing status/quality placeholders when OFF --")

    captured_calls: list = []

    async def _fake_update_fields(db_path, lead_row, chat_id, **fields):
        captured_calls.append(dict(fields))

    async def _fake_ensure_schema(db_path):
        return None

    async def _fake_collect_msgs(db_path, chat_id, raw_text):
        return []  # force the no-answer branch every time

    def _fake_now_iso():
        return "2026-01-01T00:00:00"

    def _poison_get_setting(*a, **kw):
        raise AssertionError("get_setting_from_db should never be hit -- the cache must already be warm")

    ns = extract_and_exec(
        MAIN_PY,
        {"_apply_profile_from_text", "_tp_pa_pending_fields", "_autostatus_runtime_enabled", "_autostatus_enabled_sync"},
        {
            "_tp_pa_str": lambda v: str(v or "").strip(),
            "_tp_pa_ensure_profile_schema": _fake_ensure_schema,
            "_tp_pa_collect_client_messages_after_first": _fake_collect_msgs,
            "_tp_pa_update_fields": _fake_update_fields,
            "_tp_pa_now_iso": _fake_now_iso,
            "get_setting_from_db": _poison_get_setting,
            "TPILOT_DB_PATH": "dummy_tpilot.db",
            "Optional": object,
            "_TP_PA_VERSION": "test_profile_parse_always_v3",  # module-level constant _tp_pa_pending_fields reads
        },
    )
    ns["_AUTOSTATUS_RTGATE_TTL"] = 20.0

    import time as _time_mod

    forbidden_keys = {
        "status", "quality_status", "quality_bucket", "quality_reason", "nonliquid_reason",
        "profile_done", "quality_confidence", "quality_source", "quality_version", "profile_decision_reason",
    }
    lead_row = {"chat_id": 555, "first_message_ignored_at": ""}

    # G1: switch OFF -> writer is called once, with bookkeeping fields, but with
    # NONE of the status/quality placeholder keys.
    ns["_AUTOSTATUS_RTGATE_CACHE"] = False
    ns["_AUTOSTATUS_RTGATE_CACHE_AT"] = _time_mod.monotonic()
    captured_calls.clear()
    asyncio.run(ns["_apply_profile_from_text"]("dummy.db", dict(lead_row), ""))
    check("no-answer branch (auto_status OFF) calls the field writer exactly once", len(captured_calls) == 1, str(len(captured_calls)))
    written = captured_calls[0] if captured_calls else {}
    leaked = forbidden_keys & set(written.keys())
    check(
        "no-answer branch (auto_status OFF) writes NO status/quality placeholder keys (FAIL-2 fix)",
        not leaked,
        f"leaked keys: {leaked}; full written dict: {written}",
    )
    check("no-answer branch (auto_status OFF) still writes first_message_ignored=1 bookkeeping", written.get("first_message_ignored") == 1, repr(written))
    check("no-answer branch (auto_status OFF) still writes profile_raw_text bookkeeping", "profile_raw_text" in written, repr(written))

    # G2 (negative control): switch ON -> the SAME branch DOES write the pending
    # status placeholders, proving G1 passed because of the guard, not because
    # the writer/branch is broken or a no-op.
    ns["_AUTOSTATUS_RTGATE_CACHE"] = True
    ns["_AUTOSTATUS_RTGATE_CACHE_AT"] = _time_mod.monotonic()
    captured_calls.clear()
    asyncio.run(ns["_apply_profile_from_text"]("dummy.db", dict(lead_row), ""))
    written2 = captured_calls[0] if captured_calls else {}
    check("no-answer branch (auto_status ON) DOES write quality_status='pending' (negative control)", written2.get("quality_status") == "pending", repr(written2))
    check("no-answer branch (auto_status ON) DOES write quality_bucket='na_pending' (negative control)", written2.get("quality_bucket") == "na_pending", repr(written2))
    check("no-answer branch (auto_status ON) DOES write status='unknown' (negative control)", written2.get("status") == "unknown", repr(written2))


# ======================================================================
# GROUP H (FAIL-2 fix) -- the base-body status='unknown' initializer
# (inside the base _record_incoming_from_manager, reached via the live
# delegate chain) is gated. Structural check: this snippet sits inside a
# huge Telethon event handler with too many transitive dependencies to
# safely isolate by AST-extraction+exec, so this mirrors the project's own
# convention (see Group C / the allow_spend=True count check) of verifying
# the guard text is present at the exact known anchor.
# ======================================================================

def run_group_h() -> None:
    print("\n-- Group H (FAIL-2 fix): base-body status='unknown' initializer is gated (structural) --")
    src = MAIN_PY.read_text(encoding="utf-8-sig")
    m = re.search(
        r'if not str\(\(lead_row or \{\}\)\.get\("status"\) or ""\)\.strip\(\) and await _autostatus_runtime_enabled\(\):\s*\n'
        r'\s*await _update_daily_lead_fields\(DB_PATH,.*?status="unknown", profile_done=0\)\s*\n'
        r'\s*lead_row = dict\(lead_row or \{\}\)\s*\n'
        r'\s*lead_row\["status"\] = "unknown"',
        src,
    )
    check("the base-body status='unknown' initializer (incl. the in-memory lead_row mutation) is fully gated", m is not None)


def main() -> int:
    run_group_a()
    run_group_b()
    run_group_c()
    run_group_d()
    run_group_e()
    run_group_f()
    run_group_g()
    run_group_h()

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
