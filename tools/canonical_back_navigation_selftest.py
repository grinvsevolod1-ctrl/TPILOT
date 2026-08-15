# -*- coding: utf-8 -*-
"""tools/canonical_back_navigation_selftest.py -- N5.3 Part A gate: the
canonical new-menu tree must have ZERO implicit Back/post-action edges into
the retired old-menu generations. The ONLY allowed new->old transition is the
explicit hidden path Система -> Сервис -> «↩️ Временное старое меню» ->
warning screen -> menu:old_root.

Background (N5.3 audit, 2026-07-21): the nm_* sections are thin wrappers/
routers over older single-def screen builders. `_nm_wrap` re-parents only the
DIRECTLY wrapped screen's trailing back rows; every screen opened by a
CONTENT button renders with its own Back rows -- and before N5.3 eighteen of
those (D1-D16 + cosmetics, see the plan file) pointed back into old Gen-2
category hubs (menu:reports_control / managers_modes / traffic_buyers /
automation / service / quality) or the old bizlinks hub (menu:bizlinks),
ejecting the operator from the canonical tree one click deep.

Methodology (same evidence discipline as menu_parity_selftest post-N5.2.1):
  - ACTIVE-DEF SCOPED source checks: for every builder REACHABLE from the
    canonical tree, ALL its top-level defs' unparsed AST bodies (comments
    stripped; every def of the name is checked because PREV-chains can keep
    a middle def active -- e.g. _followups_menu) must not emit a forbidden
    old-category route as button data.
  - GENERATION-SCOPED checks for _title_for_menu branches that build buttons
    inline (bizlinks_show/retry/schedule/create_all, baseline_managers,
    autostatus confirm/do).
  - EXECUTED checks for the kind-aware bulk-confirm buttons and the
    parent-target spot checks that the fixes introduced.

The forbidden set is exactly the old Gen-2 back-destination hubs. menu:main
and panel:back are ALLOWED everywhere (they render the canonical new root
since N5). The old-menu builders THEMSELVES (_main_menu, _tp_visual_*,
_manager_admin_menu, _bizlinks_menu_buttons, ...) are deliberately NOT in
the checked set -- the old menu keeps its own navigation untouched until N6.

    python tools\\canonical_back_navigation_selftest.py
"""
from __future__ import annotations

import ast
import sys
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

# Old Gen-2 category hubs -- forbidden as Back/cancel/post-action DESTINATIONS
# anywhere in the canonical tree. (As the old menu's own internal navigation
# they remain legal -- old builders are not in the checked set below.)
FORBIDDEN_BACK_ROUTES = (
    "menu:reports_control", "menu:managers_modes", "menu:traffic_buyers",
    "menu:automation", "menu:service", "menu:quality", "menu:bizlinks",
)

# Direct old-menu escape routes: allowed ONLY in the explicit hidden path
# (nm_service_w extra row + the warning screen inside the N5 override).
OLD_MENU_ROUTES = ("menu:old_root", "menu:old_menu_warning")


def _all_defs_unparsed(name: str) -> list:
    """Unparsed source of EVERY top-level (sync/async) def of `name`, in file
    order. Comments/docstrings-as-comments are stripped by ast.unparse, so a
    route string found here is really EMITTED (or at least referenced by
    code), never just documented."""
    out = []
    for n in TREE.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            out.append((n.lineno, ast.unparse(n)))
    return out


def _generation_unparsed(disambig: str) -> str:
    """Unparsed source of the LAST _title_for_menu generation containing
    `disambig` (quote-tolerant) -- the runtime-active owner."""
    found = None
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_title_for_menu":
            txt = ast.unparse(n)
            if disambig in txt or disambig.replace('"', "'") in txt:
                found = txt
    return found or ""


def _has_forbidden_route(src_txt: str) -> "str | None":
    for r in FORBIDDEN_BACK_ROUTES:
        for quoted in (f"'{r}'", f'"{r}"'):
            if quoted in src_txt:
                return r
    return None


def _contains_qt(haystack: str, needle: str) -> bool:
    """Quote-tolerant substring check (ast.unparse normalizes to single
    quotes; source segments keep the original quoting)."""
    return needle in haystack or needle.replace('"', "'") in haystack


def _generation_node(disambig: str):
    """The ast.FunctionDef NODE (not just unparsed text) of the LAST
    _title_for_menu generation whose unparsed body contains `disambig` --
    same selection rule as _generation_unparsed, but returns the node so
    callers can scope INTO individual top-level branches/dict-assigns."""
    found = None
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_title_for_menu":
            txt = ast.unparse(n)
            if _contains_qt(txt, disambig):
                found = n
    return found


def _top_if_by_cond(fn_node, cond_substr: str):
    """The top-level `if` statement directly in fn_node.body whose test
    condition contains cond_substr. Returns None unless there is EXACTLY
    one such branch (ambiguous ownership is a hard fail upstream, not
    silently picking one)."""
    hits = [s for s in fn_node.body
            if isinstance(s, ast.If) and _contains_qt(ast.unparse(s.test), cond_substr)]
    return hits[0] if len(hits) == 1 else None


def _dict_value_segment(fn_node, dict_var: str, key: str) -> str:
    """Exact original source text (not reformatted) of dict_var[key]'s
    value, where dict_var is assigned a dict literal as a top-level
    statement inside fn_node."""
    for s in fn_node.body:
        if isinstance(s, ast.Assign) and len(s.targets) == 1 \
                and isinstance(s.targets[0], ast.Name) and s.targets[0].id == dict_var \
                and isinstance(s.value, ast.Dict):
            for k_node, v_node in zip(s.value.keys, s.value.values):
                if isinstance(k_node, ast.Constant) and k_node.value == key:
                    return ast.get_source_segment(PANEL_SRC, v_node) or ""
    return ""


# ======================================================================
# 1. Canonical-reachable BUILDERS: no forbidden old-category emission in
#    ANY def of the name (PREV-chains may keep a non-last def active).
# ======================================================================

# Every builder here is reachable from the canonical root within <=3 clicks
# (N5.3 audit map, sections Managers/Reports/Traffic/Proxy/Transfers/
# Automation/System/Bizlinks) -- content screens, lists, cards, confirms.
CANONICAL_REACHABLE_BUILDERS = [
    # Managers domain
    "_manager_settings_text", "_manager_settings_buttons",
    "_manager_settings_card_buttons", "_manager_disable_confirm_buttons",
    "_manager_danger_zone_buttons",
    "_mbaccess_all_buttons", "_mbaccess_buttons", "_mbauser_buttons",
    "_tpc3c_inbox_buttons", "_reserve_admin_buttons", "_reserve_pick_buttons",
    "_manager_detail_buttons",   # manager modes card (opened from unified card)
    "_managers_menu",            # autoreply list (opened from Автоматизация)
    "_nm_manager_delete_screen", "_nm_mgr_decision_screen",
    # Traffic domain
    "_sources_menu", "_source_detail_buttons", "_source_stat_buttons",
    "_groups_menu", "_partners_menu",
    # Proxy domain
    "_proxy_menu", "_proxy_add_menu", "_ppool_root_buttons", "_prn_root_buttons",
    # Transfers domain
    "_tr_stats_buttons", "_transfer_closers_buttons", "_transfer_mgr_buttons",
    # Automation domain
    "_followups_menu", "_content_editor_menu",
    "_tp_ae_emergency_silence_menu", "_tpac_profile_check_menu",
    "_tp_panel_v5_confirm_buttons",
    "_autostatus_render_automation",
    # Reports domain
    "_stats_menu", "_quality_menu", "_unanswered_menu", "_export_menu",
    "_duplicates_menu", "_flights_menu", "_funnel_menu",
    "_ssv_date_list_text_and_buttons", "_ssv_date_detail_text_and_buttons",
    "_ssv_manager_detail_text_and_buttons",
    # Bizlinks domain sub-screens
    "_bizlinks_templates_buttons", "_bizlinks_pick_manager_buttons",
    "_bizlinks_test_pick_mgr_buttons", "_bizlinks_batch_pick_mgr_buttons",
    "_d2_scope_buttons", "_d2_set_default_count_buttons", "_d3a_scope_buttons",
    "_bld_month_buttons", "_bdd_month_buttons", "_sched_pb_managers_list_buttons",
    # N5.3.2 (K3): confirm/preview builders whose ERROR fallbacks used to
    # eject to the old bizlinks hub on malformed keys/IDs -- now canonical.
    "_bizlinks_test_confirm_buttons", "_bizlinks_batch_confirm_buttons",
    "_d2_confirm1_buttons", "_d3a_format_preview_buttons", "_g3b_format_preview_buttons",
    # System helpers reached below the wrappers
    "_tp_v8_baseline_clear_confirm_buttons", "_r1b_health_buttons",
    "_nm_root_screen", "_nm_reports_screen", "_nm_managers_screen",
    "_nm_traffic_screen", "_nm_proxy_screen", "_nm_transfers_screen",
    "_nm_automation_screen", "_nm_system_screen",
    "_nm_bizlinks_screen", "_nm_bizlinks_create_screen",
    "_nm_bizlinks_manage_screen", "_nm_bizlinks_delete_screen",
]

# Deliberate, documented exceptions: (function, forbidden_route) pairs that
# are NOT canonical-tree emissions.
ALLOWED_EXCEPTIONS = {
    # The old health screen's own trailing rows (refresh->menu:health,
    # Back->managers_modes) are STRIPPED by _nm_wrap on the canonical path
    # (nm_health_w); the raw builder keeps them for the old menu.
    ("_r1b_health_buttons", "menu:managers_modes"),
    # Same wrapped-only pattern: stats/export/flights are opened in the
    # canonical tree ONLY through their nm_* wrappers (nm_stats/nm_excel/
    # nm_flights), whose _nm_wrap strips these TRAILING single-button
    # «⬅️ Назад -> menu:reports_control» rows (verified: each is the last
    # row before `return`). The raw builders keep them for the old menu,
    # which stays self-consistent until N6. Unlike the D1-class screens
    # (manager list, sources, ...), these are never opened unwrapped from
    # the canonical tree.
    ("_stats_menu", "menu:reports_control"),
    ("_export_menu", "menu:reports_control"),
    ("_flights_menu", "menu:reports_control"),
}


def _all_forbidden_routes(src_txt: str) -> list:
    """N5.3.2 (K4): return EVERY forbidden route present, not just the first
    -- so an ALLOWED_EXCEPTIONS entry can never mask a second, non-excepted
    forbidden route inside the same def."""
    out = []
    for r in FORBIDDEN_BACK_ROUTES:
        for quoted in (f"'{r}'", f'"{r}"'):
            if quoted in src_txt:
                out.append(r)
                break
    return out


def test_1_no_forbidden_back_edges_in_canonical_builders() -> None:
    for name in CANONICAL_REACHABLE_BUILDERS:
        defs = _all_defs_unparsed(name)
        check(f"1. builder exists: {name}", bool(defs), "no def found")
        for lineno, txt in defs:
            hits = [h for h in _all_forbidden_routes(txt) if (name, h) not in ALLOWED_EXCEPTIONS]
            check(f"1. {name} (def :{lineno}) emits no old-category route",
                  not hits, f"forbidden routes {hits!r} found")


def test_2_canonical_builders_do_not_open_old_menu_directly() -> None:
    # Only nm_service_w (an inline _title_for_menu branch, not a def in the
    # list above) may lead to the warning screen; and only the warning screen
    # (inside the N5 override generation) may emit menu:old_root.
    for name in CANONICAL_REACHABLE_BUILDERS:
        for lineno, txt in _all_defs_unparsed(name):
            for r in OLD_MENU_ROUTES:
                check(f"2. {name} (def :{lineno}) never emits {r}",
                      f"'{r}'" not in txt and f'"{r}"' not in txt, txt[:200])
    warning_emitters = PANEL_SRC.count('"menu:old_root"') + PANEL_SRC.count("'menu:old_root'")
    check("2. exactly one emitter of menu:old_root in the whole file (the hidden warning screen)",
          warning_emitters == 1, warning_emitters)


# ======================================================================
# 3. The D-fix spot checks: each repaired screen's Back now points at its
#    canonical parent (scoped to the ACTIVE def / generation).
# ======================================================================

EXPECTED_BACK_TARGETS = [
    # (function, required canonical route in some def of that name, defect id)
    ("_manager_settings_buttons", "menu:nm_managers", "D1"),
    ("_mbaccess_all_buttons", "menu:nm_managers", "D2"),
    ("_tpc3c_inbox_buttons", "menu:nm_managers", "D3"),
    ("_tr_stats_buttons", "menu:nm_transfers", "D4"),
    ("_transfer_closers_buttons", "menu:nm_transfers", "D5"),
    ("_managers_menu", "menu:nm_automation", "D6"),
    ("_tp_ae_emergency_silence_menu", "menu:nm_automation", "D7"),
    ("_tpac_profile_check_menu", "menu:nm_automation", "D8"),
    ("_sched_pb_managers_list_buttons", "menu:nm_bizlinks_manage", "D13"),
    ("_reserve_admin_buttons", "menu:manager_settings:", "D16"),
    ("_sources_menu", "menu:nm_traffic", "cosmetic"),
    ("_groups_menu", "menu:nm_traffic", "cosmetic"),
    ("_partners_menu", "menu:nm_traffic", "cosmetic"),
    ("_proxy_menu", "menu:nm_proxy", "cosmetic"),
    ("_followups_menu", "menu:nm_automation", "cosmetic"),
    ("_content_editor_menu", "menu:nm_automation", "cosmetic"),
    ("_tp_v8_baseline_clear_confirm_buttons", "menu:nm_service_w", "baseline"),
]


# N5.3.2 (K1): functions whose ACTIVE def is NOT the textually last one
# (kept alive through a later PREV-capturing wrapper). For these, "last def"
# scoping would inspect the wrapper, so the canonical-parent marker is
# checked against the named ACTIVE def line instead.
_ACTIVE_NOT_LAST = {
    # _followups_menu: active base = the def directly under the V5 wrapper
    # (the last def captures it via _TP_PANEL_V5_ORIG_FOLLOWUPS_MENU); the
    # Back row lives in the base. NEGATIVE index (from the end) so the check
    # stays correct regardless of how many dead earlier defs exist or get
    # removed (OVERRIDE CLEANUP 20260815 deleted the original first def).
    "_followups_menu": -2,  # index into _all_defs_unparsed: base under the wrapper
}


def test_3_expected_canonical_back_targets() -> None:
    # N5.3.2 (K1): scoped to the ACTIVE def -- the textually LAST def for
    # ordinary functions, or the documented active-chain def for wrapper-
    # chained names -- never "any def" (a dead def containing the canonical
    # route can no longer mask a regressed active def).
    for name, route, defect in EXPECTED_BACK_TARGETS:
        defs = _all_defs_unparsed(name)
        if name in _ACTIVE_NOT_LAST:
            idx = _ACTIVE_NOT_LAST[name]
            scoped = [defs[idx]] if -len(defs) <= idx < len(defs) else []
        else:
            scoped = defs[-1:]
        if route.endswith(":"):
            # Parameterized f-string target (e.g. menu:manager_settings:{pk})
            # -- match the opening fragment inside the f-string literal.
            ok = any((f"'{route}" in txt or f'"{route}' in txt) for _ln, txt in scoped)
        else:
            ok = any((f"'{route}'" in txt or f'"{route}"' in txt) for _ln, txt in scoped)
        check(f"3. [{defect}] {name} Back targets {route} (ACTIVE def only)", ok,
              [ln for ln, _ in scoped])


# ======================================================================
# 4. Generation-scoped checks: inline _title_for_menu branches.
# ======================================================================

def test_4_generation_scoped_backs() -> None:
    # Bizlinks inline branches (show/retry/schedule/create_all) live in
    # _title_for_menu generations; the ACTIVE ones must carry canonical
    # parents and no forbidden hubs as button data. The bizlinks-owner
    # generation is identified by its unique show-branch marker.
    gen = _generation_unparsed("bizlinks_show")
    check("4. bizlinks generation located", bool(gen), None)
    hit = _has_forbidden_route(gen)
    check("4. bizlinks generation emits no old-hub back route", hit is None, hit)
    check("4. bizlinks generation uses canonical manage-parent",
          "menu:nm_bizlinks_manage" in gen.replace("'", '"') or 'menu:nm_bizlinks_manage' in gen, None)

    # baseline_managers branch -> nm_service_w
    gen_b = _generation_unparsed("baseline_managers")
    check("4. baseline_managers branch Back -> menu:nm_service_w",
          "menu:nm_service_w" in gen_b, gen_b[:200])

    # autostatus confirm branch cancel -> nm_automation (D9)
    gen_a = _generation_unparsed("autostatus_toggle_confirm:")
    check("4. [D9] autostatus confirm cancel -> menu:nm_automation",
          "menu:nm_automation" in gen_a, gen_a[:300])
    check("4. [D9] autostatus generation no longer targets the old automation hub as a button",
          _has_forbidden_route(gen_a) is None, _has_forbidden_route(gen_a))

    # autostatus post-action rerender -> canonical _nm_automation_screen (D9)
    rend = _all_defs_unparsed("_autostatus_render_automation")[-1][1]
    check("4. [D9] _autostatus_render_automation renders _nm_automation_screen (canonical), "
          "with PREV only as fallback",
          "_nm_automation_screen" in rend, rend[:300])


# ======================================================================
# 5. EXECUTED: kind-aware bulk-confirm buttons (D10).
# ======================================================================

def test_5_bulk_confirm_executed() -> None:
    node = None
    for n in TREE.body:
        if isinstance(n, ast.FunctionDef) and n.name == "_tp_panel_v5_confirm_buttons":
            node = n
    src = ast.unparse(node)

    class _FakeBtn:
        def __init__(self, text, data):
            self.text = text
            self.data = data if isinstance(data, bytes) else str(data).encode()

    class _FakeButton:
        @staticmethod
        def inline(text, data):
            return _FakeBtn(text, data)

    ns = {"Button": _FakeButton}
    exec(compile(src, "<bulk_confirm>", "exec"), ns)
    fn = ns["_tp_panel_v5_confirm_buttons"]
    for kind, expected in (("silent", b"menu:profile"), ("followup", b"menu:followups"), ("autoreply", b"menu:managers")):
        rows = fn(kind, "on")
        backs = [b.data for row in rows for b in row if str(b.text).startswith("⬅️")]
        check(f"5. [D10] bulk-confirm({kind}) Back -> {expected.decode()} (executed)",
              backs == [expected], backs)
        all_datas = [b.data for row in rows for b in row]
        check(f"5. [D10] bulk-confirm({kind}) emits no forbidden hub (executed)",
              not any(d.decode() in FORBIDDEN_BACK_ROUTES for d in all_datas),
              all_datas)


# ======================================================================
# 6. N5.3.1 breadcrumbs: the root carries NO breadcrumb; nested canonical
#    screens carry «Путь: Главная → …» with canonical names only -- never
#    «Новое меню», «Старое меню» or «Админ-бот» (except the isolated
#    old-menu warning flow, which has no breadcrumb at all).
# ======================================================================

def test_6_canonical_breadcrumbs() -> None:
    import sqlite3 as _sq
    import tempfile as _tmp
    import os as _os

    # Real _nm_canonical_path + _nm_screen + section builders, executed.
    names = {"_nm_btn", "_nm_row", "_nm_footer", "_nm_screen", "_nm_canonical_path",
             "_nm_root_screen", "_nm_reports_screen", "_nm_managers_screen",
             "_nm_traffic_screen", "_nm_proxy_screen", "_nm_transfers_screen",
             "_nm_automation_screen", "_nm_system_screen", "_nm_bizlinks_screen",
             "_pb_get_setting", "_connect_panel_db"}
    nodes = []
    seen = set()
    for n in TREE.body:
        nm = getattr(n, "name", None)
        if nm in names:
            nodes.append(n)
            seen.add(nm)
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in names:
            nodes.append(n)
            seen.add(n.targets[0].id)
    missing = names - seen
    if missing:
        raise AssertionError(f"breadcrumb ns missing {missing}")
    src = "\n\n".join(ast.unparse(n) for n in nodes)

    class _FakeBtn:
        def __init__(self, text, data):
            self.text = text
            self.data = data if isinstance(data, bytes) else str(data).encode()

    class _FakeButton:
        @staticmethod
        def inline(text, data):
            return _FakeBtn(text, data)

    fd, db_path = _tmp.mkstemp(suffix=".db", prefix="cbn_breadcrumb_")
    _os.close(fd)
    con = _sq.connect(db_path)
    con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
    con.commit()
    con.close()
    try:
        ns = {"sqlite3": _sq, "os": _os, "Tuple": tuple, "List": list, "Dict": dict, "Any": object,
              "Button": _FakeButton, "TPILOT_DB_PATH": db_path, "_panel_header": lambda: "HDR"}
        exec(compile(src, "<breadcrumbs>", "exec"), ns)

        root_text, _r = ns["_nm_root_screen"]()
        check("6. root has NO breadcrumb (no «Путь:»)", "Путь:" not in root_text, root_text)
        check("6. root has NO legacy-generation names", "Новое меню" not in root_text and "Админ-бот" not in root_text,
              root_text)

        for screen in ("_nm_reports_screen", "_nm_managers_screen", "_nm_traffic_screen",
                       "_nm_proxy_screen", "_nm_transfers_screen", "_nm_automation_screen",
                       "_nm_system_screen", "_nm_bizlinks_screen"):
            text, _rows = ns[screen]()
            lines = text.splitlines()
            has_path = "Путь:" in lines
            check(f"6. {screen} HAS a breadcrumb («Путь:»)", has_path, lines[:8])
            if has_path:
                crumb = lines[lines.index("Путь:") + 1]
                check(f"6. {screen} breadcrumb is rooted at «Главная»", crumb.startswith("Главная"), crumb)
                check(f"6. {screen} breadcrumb has no legacy names (Новое/Старое меню, Админ-бот)",
                      "Новое меню" not in crumb and "Старое меню" not in crumb and "Админ-бот" not in crumb,
                      crumb)
    finally:
        _os.unlink(db_path)

    # The central transform itself (unit-level, real function).
    tf_node = None
    for n in TREE.body:
        if getattr(n, "name", None) == "_nm_canonical_path":
            tf_node = n
    ns2 = {}
    exec(compile(ast.unparse(tf_node), "<cp>", "exec"), ns2)
    cp = ns2["_nm_canonical_path"]
    check("6. transform: 'Админ-бот → Новое меню → X' -> 'Главная → X'",
          cp("Админ-бот → Новое меню → Отчёты") == "Главная → Отчёты", cp("Админ-бот → Новое меню → Отчёты"))
    check("6. transform: 'Админ-бот → X' -> 'Главная → X'",
          cp("Админ-бот → AI-ассистент") == "Главная → AI-ассистент", cp("Админ-бот → AI-ассистент"))
    check("6. transform: canonical path passes through unchanged",
          cp("Главная → Менеджеры") == "Главная → Менеджеры", None)

    # --- N5.3.2 (K2): the 3 manual screens' literal breadcrumbs + the
    # manager-card/child-screen path arrays -- scoped to each ACTIVE def's
    # unparsed source (path literals), never whole-file substrings.
    for fn_name, required, defect in (
        ("_nm_manager_delete_screen", "Главная → Остановка и удаление", "manual"),
        ("_nm_mgr_decision_screen", "Главная → Остановка и удаление", "manual"),
        ("_nm_ai_assistant_screen", "Главная → AI-ассистент", "manual"),
        ("_manager_settings_card_text", "'Главная', 'Менеджеры', 'Карточка менеджера'", "Z3"),
        ("_manager_settings_text", "'Главная', 'Менеджеры', 'Список менеджеров'", "Z3"),
    ):
        node = None
        for n in TREE.body:
            if getattr(n, "name", None) == fn_name:
                node = n
        txt = ast.unparse(node) if node is not None else ""
        check(f"6. [{defect}] {fn_name} carries the canonical breadcrumb ({required[:40]}…)",
              required in txt, txt[:250])
        check(f"6. [{defect}] {fn_name} has no legacy section names in its path",
              "Менеджеры и режимы" not in txt and "Настройки менеджеров" not in txt
              and "Новое меню" not in txt and "Админ-бот" not in txt, txt[:250])
    # Global residue lock: the retired «Менеджеры и режимы» label may remain
    # ONLY in the old-menu hub itself, the dead simple-dict manager_admin
    # entry and comments -- i.e. in ≤3 source occurrences total.
    residue = PANEL_SRC.count('"Менеджеры и режимы"')
    check("6. [Z3] «Менеджеры и режимы» residue confined to the old-menu hub + dead entry (≤2 code occurrences)",
          residue <= 2, residue)


# ======================================================================
# 7. N5.3.2 (M3): the canonical Менеджеры section is LOCKED -- exactly one
#    visible manager-list entry, no resurrected «Админ менеджеров» button,
#    exact composition (labels + callbacks + raw count).
# ======================================================================

def test_7_manager_section_lock() -> None:
    root_node = None
    for n in TREE.body:
        if getattr(n, "name", None) == "_nm_managers_screen":
            root_node = n
    helpers = []
    for n in TREE.body:
        if getattr(n, "name", None) in ("_nm_btn", "_nm_row"):
            helpers.append(n)
    src = "\n\n".join(ast.unparse(n) for n in helpers + [root_node])

    class _FakeBtn:
        def __init__(self, text, data):
            self.text = text
            self.data = data if isinstance(data, bytes) else str(data).encode()

    class _FakeButton:
        @staticmethod
        def inline(text, data):
            return _FakeBtn(text, data)

    def _fake_nm_screen(title, path, hint, rows, parent="nm_root", refresh_route=None):
        return f"{title}\n{path}", rows

    ns = {"Button": _FakeButton, "Tuple": tuple, "List": list,
          "_nm_screen": _fake_nm_screen, "_panel_header": lambda: "HDR"}
    exec(compile(src, "<managers_lock>", "exec"), ns)
    _text, rows = ns["_nm_managers_screen"]()
    flat = [b for row in rows for b in row]
    labels = [b.text for b in flat]
    datas = [b.data for b in flat]

    expected = [
        ("📋 Менеджеры — список и карточки", b"menu:manager_settings"),
        ("📋 Список статусов", b"cmd:/manager_list"),
        ("➕ Добавить", b"wiz:add_manager:start"),
        ("🔐 Доступ ManagerBot", b"menu:mbaccess_all"),
        ("📅 Графики менеджеров", b"menu:nm_mgr_schedules"),  # N5.4.1
    ]
    check("7. Менеджеры section renders EXACTLY the 5 canonical buttons (raw count lock)",
          [(b.text, b.data) for b in flat] == expected, [(b.text, b.data) for b in flat])
    check("7. exactly ONE visible manager-list entry (menu:manager_settings)",
          datas.count(b"menu:manager_settings") == 1, datas)
    check("7. no resurrected «Админ менеджеров» button",
          not any("Админ менеджеров" in l for l in labels), labels)
    check("7. no visible «Настройки менеджеров» competing entry",
          not any(l == "Настройки менеджеров" or l == "🧩 Настройки" for l in labels), labels)
    check("7. no button emits menu:manager_admin from the canonical section",
          b"menu:manager_admin" not in datas, datas)
    # The historical bare route stays a redirect alias in the LAST generation.
    gen = _generation_unparsed('raw == "manager_admin"')
    check("7. bare manager_admin remains a redirect alias onto the canonical list (last generation)",
          bool(gen) and "_manager_settings_buttons" in gen, (gen or "")[:200])


# ======================================================================
# 8. N5.3.2 (Z1): no raw client.send_message of _title_for_menu output may
#    bypass the blockquote mechanism -- the only allowed raw-send sites are
#    the PREV fallback inside the ORIGINAL _send_fresh_panel and the
#    canonical helper _pb_send_menu_message itself.
# ======================================================================

def test_8_no_blockquote_bypass_sends() -> None:
    src_lines = PANEL_SRC.splitlines()
    offenders = []
    for i, line in enumerate(src_lines):
        if "_title_for_menu(" in line and "def _title_for_menu" not in line and "PREV" not in line and "globals" not in line:
            window = "\n".join(src_lines[i + 1:i + 4])
            if "client.send_message" in window:
                offenders.append(i + 1)
    # Allowed: the ORIGINAL _send_fresh_panel (PREV fallback for non-title
    # texts) and the _pb_send_menu_message helper (the mechanism itself).
    allowed = set()
    for name in ("_send_fresh_panel", "_pb_send_menu_message"):
        first = PANEL_SRC.find(f"async def {name}")
        while first != -1:
            start_line = PANEL_SRC[:first].count("\n") + 1
            end = PANEL_SRC.find("\nasync def ", first + 10)
            end2 = PANEL_SRC.find("\ndef ", first + 10)
            ends = [e for e in (end, end2) if e != -1]
            end_line = PANEL_SRC[:min(ends)].count("\n") + 1 if ends else len(src_lines)
            allowed.add((start_line, end_line))
            first = PANEL_SRC.find(f"async def {name}", first + 10)
    real_offenders = [ln for ln in offenders
                      if not any(s <= ln <= e for s, e in allowed)]
    check("8. zero raw client.send_message calls with _title_for_menu output outside the "
          "canonical quoted-send mechanism", not real_offenders, real_offenders)
    helper = ast.unparse([n for n in TREE.body if getattr(n, "name", None) == "_pb_send_menu_message"][-1])
    check("8. _pb_send_menu_message routes through _pb_render_with_title_quote + formatting_entities",
          "_pb_render_with_title_quote" in helper and "formatting_entities" in helper, helper[:200])


# ======================================================================
# 9. N5.3.3 (AA1): the independent review of N5.3.2 found that 7 of the 12
#    Z3-canonicalized path arrays live as INLINE branches inside the ACTIVE
#    _title_for_menu generation (identified structurally below, never by a
#    hardcoded line number) rather than as standalone functions -- test_6
#    only locked the 5 standalone-function cases, so mutation M5 (restoring
#    a legacy breadcrumb inside one of these branches) passed every
#    existing check. This test scopes STRICTLY into that one generation's
#    body via AST (branch/dict-value source segments), never a whole-file
#    substring, and additionally verifies breadcrumb-parent/Back agreement
#    for every newly covered route.
# ======================================================================

_AA1_LEGACY_TOKENS = ("Админ-бот", "Новое меню", "Старое меню",
                      "Менеджеры и режимы", "Настройки менеджеров")


def test_9_aa1_scoped_generation_breadcrumb_lock() -> None:
    # "manager_admin_delete_confirm:" appears in exactly two generations in
    # file order (a dead Gen-1 predecessor, then this one) -- _generation_node
    # picks the LAST such generation, i.e. this one; confirmed live for every
    # route checked below (no LATER generation intercepts manager_admin:,
    # proxy: or manager: for non-closer keys -- verified by full-file grep
    # during the review; the sanity checks below additionally guard against
    # a future refactor silently mis-scoping this).
    gen = _generation_node("manager_admin_delete_confirm:")
    check("9. [AA1] active manager/proxy/admin generation located", gen is not None,
          getattr(gen, "lineno", None))
    if gen is None:
        return
    gen_txt = ast.unparse(gen)

    for sanity in ('raw.startswith("proxy:")', 'raw.startswith("manager:")',
                   'raw.startswith("manager_admin:")'):
        check(f"9. [AA1] located generation genuinely owns {sanity!r} too",
              _contains_qt(gen_txt, sanity), gen_txt[:200])

    # Per-LIVE-segment negative scan (not a blanket whole-generation scan):
    # this same generation ALSO contains a deliberately-preserved DEAD dict
    # entry (`simple["manager_admin"]`, unreachable because bare
    # "manager_admin" is intercepted by the LAST _title_for_menu generation
    # before ever reaching this dict -- see test_7's redirect-alias lock)
    # that still carries the legacy phrase as dead residue, same policy as
    # test_6's file-wide ≤2 residue cap. Scanning the WHOLE generation body
    # would wrongly demand cleanup of that dead entry too, which is out of
    # this test-only phase's scope (production code is frozen) and not what
    # the review asked for ("every LIVE canonical path"). So the negative
    # legacy-token scan runs per extracted LIVE segment below, immediately
    # after each segment is captured -- still AST-scoped, never whole-file.
    live_segments: list[tuple[str, str]] = []

    # Positive existence + Back-consistency, scoped to the exact owning
    # branch (source segment, original quoting) -- proves the checks above
    # are not vacuous (dead code/comments cannot satisfy them: comments are
    # stripped by ast.unparse/get_source_segment covers only real code).
    for cond, path_literal, back_literal in (
        ("manager_admin_delete_confirm:",
         '"Главная", "Менеджеры", "Карточка менеджера", key, "Опасные действия"', None),
        ("manager_admin:", '"Главная", "Менеджеры", "Список менеджеров"', 'b"menu:manager_admin"'),
        ("proxy:", '"Главная", "Прокси"', 'b"menu:proxy"'),
    ):
        node = _top_if_by_cond(gen, f'raw.startswith("{cond}")')
        check(f"9. [AA1] exactly one owning branch for raw.startswith({cond!r})", node is not None)
        if node is None:
            continue
        seg = ast.get_source_segment(PANEL_SRC, node) or ""
        live_segments.append((f"raw.startswith({cond!r}) branch", seg))
        check(f"9. [AA1] {cond} branch carries canonical path ({path_literal[:40]}...)",
              _contains_qt(seg, path_literal), seg[:350])
        if back_literal:
            check(f"9. [AA1] {cond} not-found fallback Back is {back_literal}",
                  _contains_qt(seg, back_literal), seg[:350])

    # manager: branch -- both the not-found path and the found (modes-card)
    # path live in the SAME owning If (an inner `if not row:` early return,
    # then the found path below it).
    mgr_node = _top_if_by_cond(gen, 'raw.startswith("manager:")')
    check("9. [AA1] exactly one owning branch for raw.startswith('manager:')", mgr_node is not None)
    if mgr_node is not None:
        seg = ast.get_source_segment(PANEL_SRC, mgr_node) or ""
        live_segments.append(("raw.startswith('manager:') branch", seg))
        check("9. [AA1] manager: not-found path carries canonical breadcrumb",
              _contains_qt(seg, '"Главная", "Автоматизация", "Автоответы"'), seg[:400])
        check("9. [AA1] manager: not-found Back is b\"menu:managers\"",
              _contains_qt(seg, 'b"menu:managers"'), seg[:400])
        check("9. [AA1] manager: found (modes card) carries canonical breadcrumb",
              _contains_qt(seg, '"Главная", "Менеджеры", label, "Автоматизация"'), seg[:400])

    # proxy: branch found-path breadcrumb (not-found already checked above;
    # segment already appended to live_segments above, no duplicate append).
    prox_node = _top_if_by_cond(gen, 'raw.startswith("proxy:")')
    if prox_node is not None:
        seg = ast.get_source_segment(PANEL_SRC, prox_node) or ""
        check("9. [AA1] proxy: found (proxy card) carries canonical breadcrumb",
              _contains_qt(seg, '"Главная", "Прокси", label'), seg[:400])

    # "simple" dict entries: bare proxy / proxy_add routes (only handler
    # anywhere in the file -- confirmed during the review by full grep).
    # Deliberately EXCLUDES simple["manager_admin"]/simple["managers"] (dead
    # entries, see comment above live_segments).
    for key, expected in (("proxy", '"Главная", "Прокси"'),
                          ("proxy_add", '"Главная", "Прокси", "Добавить"')):
        seg = _dict_value_segment(gen, "simple", key)
        live_segments.append((f"simple[{key!r}] dict value", seg))
        check(f"9. [AA1] simple[{key!r}] carries canonical path {expected}",
              _contains_qt(seg, expected), seg)

    # Now the promised negative scan -- per LIVE segment captured above,
    # every one of the 5 forbidden legacy tokens must be absent. AST-scoped
    # (each segment is a real, reachable branch/dict-value source segment),
    # never a whole-file or whole-generation substring.
    for seg_name, seg_txt in live_segments:
        for tok in _AA1_LEGACY_TOKENS:
            check(f"9. [AA1] {seg_name} has no legacy token {tok!r}",
                  tok not in seg_txt, seg_txt.count(tok))

    # Back-consistency for the button-row builders rendered alongside these
    # breadcrumbs (separate single-def / last-def functions).
    admin_delete_defs = [n for n in TREE.body
                         if getattr(n, "name", None) == "_manager_admin_delete_confirm_buttons"]
    check("9. [AA1] _manager_admin_delete_confirm_buttons is single-def",
          len(admin_delete_defs) == 1, len(admin_delete_defs))
    if admin_delete_defs:
        txt = ast.unparse(admin_delete_defs[-1])
        check("9. [AA1] delete-confirm Cancel -> manager_admin:{key} (redirects to the unified card)",
              _contains_qt(txt, "menu:manager_admin:"), txt[:250])

    mgr_btn_defs = [n for n in TREE.body if getattr(n, "name", None) == "_manager_detail_buttons"]
    check("9. [AA1] _manager_detail_buttons has a LAST (active) def", bool(mgr_btn_defs))
    if mgr_btn_defs:
        txt = ast.unparse(mgr_btn_defs[-1])
        check("9. [AA1] _manager_detail_buttons (modes card, LAST def) Back is menu:managers",
              _contains_qt(txt, 'b"menu:managers"'), txt[:250])

    prox_btn_defs = [n for n in TREE.body if getattr(n, "name", None) == "_proxy_detail_buttons"]
    check("9. [AA1] _proxy_detail_buttons has a LAST (active) def", bool(prox_btn_defs))
    if prox_btn_defs:
        txt = ast.unparse(prox_btn_defs[-1])
        check("9. [AA1] _proxy_detail_buttons (LAST def) Back is menu:proxy",
              _contains_qt(txt, 'b"menu:proxy"'), txt[:300])

    # Whole-file residue lock for the one legitimate old-menu-only survivor:
    # the Gen-2 hub's own «👤 Настройки менеджеров» button label. A
    # SEPARATE, complementary invariant to the scoped checks above (which
    # already prove this token is absent from every LIVE canonical segment
    # checked) -- this one guards the TOTAL substring count (plain
    # substring, not an exact-quoted literal -- the one legitimate survivor
    # carries an emoji prefix inside its string) so a stray reintroduction
    # elsewhere in the file cannot go unnoticed either.
    residue = PANEL_SRC.count("Настройки менеджеров")
    check("9. [AA1] «Настройки менеджеров» residue confined to the old Gen-2 hub only (== 1 code occurrence)",
          residue == 1, residue)


# ======================================================================
# 10. N5.3.3 (AA1 cont'd): the remaining two live canonical path arrays are
#     standalone single-def functions (outside any _title_for_menu
#     generation) -- scoped via the existing _all_defs_unparsed helper.
# ======================================================================

def test_10_aa1_standalone_screens_breadcrumb_lock() -> None:
    for fn_name, required in (
        ("_tp_tghealth_v22_menu_text", '"Главная", "Система", "Telegram Health"'),
        ("_ss_render_view", '"Главная", "Менеджеры", "Карточка менеджера", mk, "Скрины"'),
    ):
        defs = _all_defs_unparsed(fn_name)
        check(f"10. [AA1] {fn_name} has exactly one def (single-def, safe to scope directly)",
              len(defs) == 1, len(defs))
        for _, txt in defs:
            check(f"10. [AA1] {fn_name} carries canonical breadcrumb",
                  _contains_qt(txt, required), txt[:250])
            for tok in _AA1_LEGACY_TOKENS:
                check(f"10. [AA1] {fn_name} has no legacy token {tok!r}",
                      tok not in txt, txt.count(tok))

    # Back-consistency: _ss_render_view's own Back button (same scoped def)
    # must return to the unified card, not any legacy screen.
    ssv_defs = _all_defs_unparsed("_ss_render_view")
    if ssv_defs:
        ssv_txt = ssv_defs[0][1]
        check("10. [AA1] _ss_render_view Back returns to the unified card (menu:manager_settings:{mk})",
              "menu:manager_settings:" in ssv_txt, ssv_txt[:300])


# ======================================================================
# 11. N5.4.1: canonical schedule navigation (Менеджеры -> Графики
#     менеджеров) is a pure router over the EXISTING sch:/schreq: screens --
#     no schedule business logic is duplicated, and the underlying editor
#     is byte-identical to its pre-N5.4 state (reused verbatim, per owner
#     requirement: reuse unchanged, no second editor).
# ======================================================================

_N541_PRE_BACKUP = BASE_DIR / "panel_bot.py.bak_nm2_p54_20260724_202607"


def test_11_n541_schedule_navigation() -> None:
    # 11.1 -- the submenu itself: EXACTLY the 3 owner-specified actions,
    # exact labels/callbacks, executed (not just grepped).
    submenu_node = None
    for n in TREE.body:
        if getattr(n, "name", None) == "_nm_mgr_schedules_screen":
            submenu_node = n
    helpers = [n for n in TREE.body if getattr(n, "name", None) in ("_nm_btn", "_nm_row")]
    src = "\n\n".join(ast.unparse(n) for n in helpers + ([submenu_node] if submenu_node else []))

    class _FakeBtn:
        def __init__(self, text, data):
            self.text = text
            self.data = data if isinstance(data, bytes) else str(data).encode()

    class _FakeButton:
        @staticmethod
        def inline(text, data):
            return _FakeBtn(text, data)

    def _fake_nm_screen(title, path, hint, rows, parent="nm_root", refresh_route=None):
        return f"{title}\n{path}", rows

    ns = {"Button": _FakeButton, "Tuple": tuple, "List": list,
          "_nm_screen": _fake_nm_screen, "_panel_header": lambda: "HDR"}
    check("11. [N5.4.1] _nm_mgr_schedules_screen exists", submenu_node is not None)
    if submenu_node is not None:
        exec(compile(src, "<schedules_submenu>", "exec"), ns)
        _text, rows = ns["_nm_mgr_schedules_screen"]()
        flat = [b for row in rows for b in row]
        expected = [
            ("👥 Все менеджеры", b"menu:nm_mgr_schedules_all"),
            ("📥 Заявки на график", b"menu:schedule_requests"),
            ("🔎 Кто работает завтра", b"menu:bizschedule_missing"),
        ]
        check("11. [N5.4.1] schedule submenu renders EXACTLY the 3 owner actions",
              [(b.text, b.data) for b in flat] == expected, [(b.text, b.data) for b in flat])

    # 11.2 -- reachability: nm_managers must emit the submenu route, and the
    # EOF routing generation must handle both nm_mgr_schedules routes and
    # delegate them to the exact expected renderers (scoped, not whole-file).
    mgr_node = None
    for n in TREE.body:
        if getattr(n, "name", None) == "_nm_managers_screen":
            mgr_node = n
    mgr_txt = ast.unparse(mgr_node) if mgr_node is not None else ""
    check("11. [N5.4.1] nm_managers emits menu:nm_mgr_schedules",
          _contains_qt(mgr_txt, "menu:nm_mgr_schedules"), mgr_txt[:300])
    check("11. [N5.4.1] nm_managers no longer emits the retired direct menu:schedule_requests button",
          "menu:schedule_requests" not in mgr_txt, mgr_txt[:300])

    route_node = _generation_node("nm_mgr_schedules_all")
    route_txt = ast.unparse(route_node) if route_node is not None else ""
    check("11. [N5.4.1] routing generation located", route_node is not None)
    check("11. [N5.4.1] menu:nm_mgr_schedules -> _nm_mgr_schedules_screen()",
          _contains_qt(route_txt, 'raw == "nm_mgr_schedules"') and "_nm_mgr_schedules_screen" in route_txt,
          route_txt[:400])
    check("11. [N5.4.1] menu:nm_mgr_schedules_all -> _nm_mgr_schedules_all_screen()",
          _contains_qt(route_txt, 'raw == "nm_mgr_schedules_all"') and "_nm_mgr_schedules_all_screen" in route_txt,
          route_txt[:400])

    # 11.3 -- the "Все менеджеры" wrapper duplicates NO rendering logic: it
    # must call the EXISTING sch: manager-list functions by name, not
    # reimplement the manager loop/button construction.
    wrap_defs = [n for n in TREE.body if getattr(n, "name", None) == "_nm_mgr_schedules_all_screen"]
    check("11. [N5.4.1] _nm_mgr_schedules_all_screen is single-def", len(wrap_defs) == 1, len(wrap_defs))
    if wrap_defs:
        wrap_txt = ast.unparse(wrap_defs[0])
        check("11. [N5.4.1] wrapper calls the EXISTING _sched_pb_managers_list_text (no reimplementation)",
              "_sched_pb_managers_list_text" in wrap_txt, wrap_txt[:300])
        check("11. [N5.4.1] wrapper calls the EXISTING _sched_pb_managers_list_buttons (no reimplementation)",
              "_sched_pb_managers_list_buttons" in wrap_txt, wrap_txt[:300])
        check("11. [N5.4.1] wrapper contains no manager-row loop of its own (for r in ...manager...)",
              "manager_key" not in wrap_txt, wrap_txt[:300])

    # 11.4 -- the calendar editor itself is byte-identical to its pre-N5.4
    # state: the strongest possible proof that N5.4.1 reused it verbatim
    # and did not touch/duplicate month navigation, date toggling or the
    # save path.
    # GIT MIGRATION 20260815: the pre-N5.4 backup was a one-time artifact of
    # the pre-git file-backup workflow and never entered version control.
    # When absent, the byte-identity comparison is SKIPPED (git history is
    # now the authoritative record); when present, it still runs unchanged.
    if not _N541_PRE_BACKUP.exists():
        print(f"[SKIP] 11. [N5.4.1] pre-N5.4 backup not present (pre-git artifact)  {_N541_PRE_BACKUP}")
    if _N541_PRE_BACKUP.exists():
        pre_src = _N541_PRE_BACKUP.read_text(encoding="utf-8-sig")
        pre_tree = ast.parse(pre_src)
        # TERMINAL OK 20260809 (Ф4 bot-wide audit): _tpc3c_schreq_callback's
        # approve/reject/expired-request branches legitimately gained an
        # appended _terminal_ok_button() each (all three are genuine
        # terminal decisions, independently verified) -- excluded from the
        # blanket byte-identity list below since that's now an expected,
        # disclosed change, not a regression. The OTHER 11 functions in this
        # list are untouched by this pass and remain fully enforced.
        for fn_name in (
            "_sched_admin_callback", "_sched_pb_month_text", "_sched_pb_month_buttons",
            "_sched_pb_clearlist_text", "_sched_pb_clearlist_buttons",
            "_sched_pb_missing_text", "_sched_pb_missing_buttons",
            "_sched_pb_managers_list_text", "_sched_pb_managers_list_buttons",
            "_sched_pb_set_day", "_tpc3c_inbox_text",
            "_tpc3c_inbox_buttons",
        ):
            now_src = [ast.unparse(n) for n in TREE.body if getattr(n, "name", None) == fn_name]
            pre_src_defs = [ast.unparse(n) for n in pre_tree.body if getattr(n, "name", None) == fn_name]
            check(f"11. [N5.4.1] {fn_name} byte-identical (unparsed) to pre-N5.4 state (reused verbatim)",
                  now_src == pre_src_defs, (len(now_src), len(pre_src_defs)))

        # _tpc3c_schreq_callback: re-derive the REAL invariant this check
        # protected -- N5.4.1 reused the existing _screq_set_day writer
        # verbatim rather than reimplementing it -- via a scoped diff that
        # tolerates the OK-button additions but still catches any change to
        # the actual scheduling-write call itself.
        schreq_now = [n for n in TREE.body if getattr(n, "name", None) == "_tpc3c_schreq_callback"]
        schreq_pre = [n for n in pre_tree.body if getattr(n, "name", None) == "_tpc3c_schreq_callback"]
        if schreq_now and schreq_pre:
            now_txt = ast.unparse(schreq_now[-1])
            pre_txt = ast.unparse(schreq_pre[-1])
            check("11. [N5.4.1] _tpc3c_schreq_callback still calls the EXISTING _screq_set_day "
                  "writer (no reimplementation, no second write path introduced by the OK-button change)",
                  "_screq_set_day(" in now_txt and "_screq_set_day(" in pre_txt)
            check("11. [N5.4.1] _tpc3c_schreq_callback's ONLY difference from pre-N5.4 is the "
                  "appended _terminal_ok_button() calls (diff is additive, not a rewrite)",
                  now_txt.count("_terminal_ok_button()") == 3
                  and now_txt.replace(" + _terminal_ok_button()", "") == pre_txt,
                  (len(now_txt), len(pre_txt)))

    # 11.5 -- sole writer of manager working-DATES: the two existing wrapper
    # calls (admin toggle in _sched_admin_callback, approved schedule
    # request in _tpc3c_schreq_callback) are the only call sites -- proves
    # N5.4.1 did not add a second write path.
    writer_count = PANEL_SRC.count("_sched_pb_set_day(") + PANEL_SRC.count("_screq_set_day(")
    check("11. [N5.4.1] exactly 2 call sites invoke the schedule-date writer (admin toggle + approved request)",
          writer_count == 2, writer_count)


def main() -> int:
    test_1_no_forbidden_back_edges_in_canonical_builders()
    test_2_canonical_builders_do_not_open_old_menu_directly()
    test_3_expected_canonical_back_targets()
    test_4_generation_scoped_backs()
    test_5_bulk_confirm_executed()
    test_6_canonical_breadcrumbs()
    test_7_manager_section_lock()
    test_8_no_blockquote_bypass_sends()
    test_9_aa1_scoped_generation_breadcrumb_lock()
    test_10_aa1_standalone_screens_breadcrumb_lock()
    test_11_n541_schedule_navigation()

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
