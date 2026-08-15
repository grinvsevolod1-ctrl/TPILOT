# -*- coding: utf-8 -*-
"""Offline selftest for the 2026-07-19 device-login/tdata-import onboarding
convergence patch (Parts A-D, F, H of the owner's implementation request).

Covers, offline only (temp SQLite, AST-extraction with fakes, source-scan for
code embedded in the un-extractable on_callback monolith) -- no network, no
Telegram, no production DB, no spend:

  Part A  - phone self-heal normalization (_tp_normalize_phone_plus).
  Part B  - device-login phone-unavailable exact message, no premature block
            on a merely-empty-at-first-glance DB field.
  Part C  - three-state proxy status text (_devlogin_proxy_status_block):
            active (full reveal) / configured-but-disabled / no-proxy, and
            the phone fallback text used by _devlogin_waiting_text.
  Part D  - tdimport confirm command never calls _manager_finalize_login,
            does call _tp_finalize_screenshots_on + _partner_source_key_for_
            manager and sets safe-metadata-only "source_pick_needed".
  Part F  - final success message renders all required fields and the
            "Скриншоты: Включены" line. TPILOT TDATA FINAL SUCCESS SAFE
            DISPLAY 20260719: this screen has NO PIN step (unlike device-
            login), so it must NEVER leak the full phone or proxy login/
            password -- phone is masked via the canonical mask_phone helper,
            proxy shows host:port only for an active proxy. The separate
            PIN-gated device-login reveal (_devlogin_proxy_status_block /
            _pxm_full_reveal_text, full phone) is proven UNCHANGED.
  Part H  - _source_pick_buttons callback_data length: proves the OLD raw-key
            scheme COULD exceed 64 bytes for realistic keys (regression guard
            against reverting the fix), and the NEW numeric-id scheme stays
            <=64 bytes even for adversarial (near-int64-max) ids; stale/
            unknown ids resolve safely (empty dict, never a crash or a wrong
            cross-manager match).

Run:  python tools\\tdimport_onboarding_convergence_selftest.py
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from manager_registry import mask_phone  # noqa: E402

MAIN_PY = os.path.join(BASE_DIR, "main.py")
PANEL_PY = os.path.join(BASE_DIR, "panel_bot.py")

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _guard_temp_db(db_path):
    rp = os.path.realpath(db_path)
    tmp = os.path.realpath(tempfile.gettempdir())
    assert rp.startswith(tmp), f"db must live under tempdir, got {rp}"
    assert "data_tpilot.db" not in rp and os.sep + "db" + os.sep not in rp, rp


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


def src_of(tree, name):
    return ast.unparse(last_def(tree, name))


def extract_and_exec(tree, names, extra_ns):
    nodes = [last_def(tree, n) for n in names]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, f"<extract {names}>", "exec"), ns)
    return ns


def main():
    main_src = open(MAIN_PY, encoding="utf-8-sig").read()
    main_tree = ast.parse(main_src)
    panel_src = open(PANEL_PY, encoding="utf-8-sig").read()
    panel_tree = ast.parse(panel_src)

    # ======================================================================
    # Part A -- phone self-heal normalization
    # ======================================================================
    ns = extract_and_exec(main_tree, ["_tp_normalize_phone_plus"], {"Any": object})
    fn = ns["_tp_normalize_phone_plus"]
    check("A: bare digits get a leading + prepended", fn("79991234567") == "+79991234567")
    check("A: already-+-prefixed value is untouched (no reformat)", fn("+79991234567") == "+79991234567")
    check("A: empty/None input yields empty string", fn(None) == "" and fn("") == "" and fn("   ") == "")
    check("A: never raises on a non-string", fn(79991234567) == "+79991234567", detail=repr(fn(79991234567)))

    # ======================================================================
    # Part B -- device-login exact phone-unavailable message (both sides)
    # ======================================================================
    exact_msg = ("Telegram-сессия не передала номер телефона. "
                 "Подключение на другом устройстве по номеру сейчас невозможно.")
    start_src = src_of(main_tree, "_panel_manager_devlogin_start_command")
    check("B: main.py start command uses the exact owner-specified phone_unavailable text",
          exact_msg in start_src)
    check("B: main.py start command classifies it as error_class='phone_unavailable'",
          '"phone_unavailable"' in start_src or "'phone_unavailable'" in start_src)
    pin_input_src = src_of(panel_tree, "_devlogin_pin_input")
    check("B: panel_bot.py PIN handler shows the SAME exact phone_unavailable text",
          exact_msg in pin_input_src)
    check("B: panel_bot.py PIN handler no longer hard-blocks on proxy state",
          "proxy" not in pin_input_src.lower().split("token lifecycle")[0].split(exact_msg)[-1][:400]
          or "не задан proxy" not in pin_input_src)

    # ======================================================================
    # Part C -- three-state proxy status + phone fallback text
    # ======================================================================
    class _Row(dict):
        pass

    def fake_pxm_full_reveal_text(key):
        return f"🔐 Полный proxy\n\nМенеджер:\n{key}\n\nСкопируйте строку:\n\n`ACTIVE_PROXY_SENTINEL`"

    ns = extract_and_exec(
        panel_tree, ["_devlogin_proxy_status_block"],
        {"_pxm_full_reveal_text": fake_pxm_full_reveal_text},
    )
    proxy_fn = ns["_devlogin_proxy_status_block"]

    no_proxy_row = {"proxy_host": "", "proxy_port": ""}
    disabled_row = {"proxy_host": "1.2.3.4", "proxy_port": "1080", "proxy_enabled": 0}
    active_row = {"proxy_host": "1.2.3.4", "proxy_port": "1080", "proxy_enabled": 1}

    check("C: no host/port -> exact 'not connected, direct' text",
          proxy_fn("mgr", no_proxy_row) == "Не подключён — используется прямое подключение")
    check("C: host+port present but proxy_enabled=0 -> exact 'disabled, direct' text",
          proxy_fn("mgr", disabled_row) == "Отключён — используется прямое подключение")
    check("C: host+port present and proxy_enabled=1 -> delegates to full reveal (last paragraph)",
          proxy_fn("mgr", active_row) == "`ACTIVE_PROXY_SENTINEL`")
    check("C: proxy_enabled missing entirely (falsy) with host/port set == disabled, not active",
          proxy_fn("mgr", {"proxy_host": "h", "proxy_port": "1"}) == "Отключён — используется прямое подключение")

    waiting_text_src = src_of(panel_tree, "_devlogin_waiting_text")
    check("C: waiting screen's phone fallback is 'Недоступен в Telegram-сессии' (not the old 'не задан')",
          "Недоступен в Telegram-сессии" in waiting_text_src and "не задан" not in waiting_text_src)
    check("C: waiting screen delegates proxy rendering to the new 3-state helper",
          "_devlogin_proxy_status_block" in waiting_text_src)

    # ======================================================================
    # Part D -- tdimport confirm command convergence (source-scan; the real
    # function has heavy service/runtime dependencies not worth faking here --
    # covered end-to-end by tdata_import_flow_selftest's confirm_install path)
    # ======================================================================
    confirm_src = src_of(main_tree, "_panel_manager_tdimport_confirm_command")
    check("D: tdimport confirm NEVER calls _manager_finalize_login directly",
          "_manager_finalize_login(" not in confirm_src)
    check("D: tdimport confirm reuses _tp_finalize_screenshots_on",
          "_tp_finalize_screenshots_on(" in confirm_src)
    check("D: tdimport confirm reuses the canonical source-linked lookup",
          "_partner_source_key_for_manager(" in confirm_src)
    check("D: tdimport confirm gates the convergence on result.get('ok') (never on a failed install)",
          "result.get(\"ok\")" in confirm_src or "result.get('ok')" in confirm_src)
    check("D: tdimport confirm emits ONLY a safe-metadata boolean, never a raw marker string with secrets",
          'result["source_pick_needed"]' in confirm_src or "result['source_pick_needed']" in confirm_src)
    check("D: source-pick-needed write is inverted from already-linked (fail-closed = True -> not linked = False)",
          "not already_linked" in confirm_src)

    tdimport_callback_src = src_of(panel_tree, "_tdimport_callback")
    check("D/F: _tdimport_callback re-reads the manager row fresh before rendering the final success text",
          "_tdimport_final_success_text(key, mrow)" in tdimport_callback_src)
    check("D: _tdimport_callback branches on source_pick_needed to choose source-pick vs back-to-panel buttons",
          "source_pick_needed" in tdimport_callback_src and "_source_pick_buttons(key)" in tdimport_callback_src)
    check("D: _tdimport_callback appends the byte-identical marker constant when a source pick is still needed",
          "_ONBOARDING_SOURCE_PICK_MARKER" in tdimport_callback_src)

    # ======================================================================
    # Part F -- final success message renderer (non-PIN-gated -- must NEVER
    # leak full phone or proxy credentials)
    # ======================================================================
    SENTINEL_PHONE = "79995869781"
    SENTINEL_PHONE_MASKED = mask_phone(SENTINEL_PHONE)  # "+79******781"
    SENTINEL_HOST = "193.124.16.23"
    SENTINEL_PORT = "50101"
    SENTINEL_LOGIN = "secret_login"
    SENTINEL_PASSWORD = "SECRET_PROXY_PASSWORD_987"

    ns = extract_and_exec(
        panel_tree,
        ["_tdimport_final_success_text", "_tdimport_safe_proxy_status_block",
         "_tdimport_effective_schedule_text"],
        {
            "mask_phone": mask_phone,
            "_tp_visual_source_for_manager": lambda key: "Instagram RU",
            "normalize_manager_key": lambda k: str(k or "").strip().lower(),
            "_TPC2B_SMS_DEFAULTS": {"day_start": "08:00", "day_end": "17:00", "night_start": "17:00", "night_end": "08:00"},
            "_connect_panel_db": lambda: sqlite3.connect(":memory:"),
        },
    )
    final_text_fn = ns["_tdimport_final_success_text"]
    full_row = {
        "display_name": "Иван Тестов", "telegram_username": "ivantest",
        "first_name": "Иван", "last_name": "Тестов", "tg_user_id": 123456789,
        "phone": SENTINEL_PHONE,
        "proxy_host": SENTINEL_HOST, "proxy_port": SENTINEL_PORT,
        "proxy_username": SENTINEL_LOGIN, "proxy_password": SENTINEL_PASSWORD,
        "proxy_enabled": 1,
    }
    final_text = final_text_fn("testmgr", full_row)
    check("F: final message has the exact success header", final_text.startswith("✅ Аккаунт успешно подключён"))
    check("F: final message includes the manager line with username", "Иван Тестов | @ivantest" in final_text)
    check("F: final message includes Telegram identity block", "Имя: Иван Тестов" in final_text and "ID: 123456789" in final_text)
    check("F: final message shows the linked source name", "Instagram RU" in final_text)
    check("F: final message always states screenshots are on", "Скриншоты:\nВключены" in final_text)
    check("F: final message ends with the exact closing confirmation line",
          final_text.rstrip().endswith("Аккаунт успешно добавлен и запущен."))

    # --- SECRET-LEAK SENTINELS: active proxy + phone (the exact defect this
    # patch closes) -----------------------------------------------------
    check("F-SECURITY: full phone digits are ABSENT from the non-PIN final message",
          SENTINEL_PHONE not in final_text, detail="full phone leaked")
    check("F-SECURITY: canonical masked phone IS present",
          SENTINEL_PHONE_MASKED in final_text, detail=final_text)
    check("F-SECURITY: proxy login is ABSENT from the non-PIN final message",
          SENTINEL_LOGIN not in final_text, detail="proxy login leaked")
    check("F-SECURITY: proxy password is ABSENT from the non-PIN final message",
          SENTINEL_PASSWORD not in final_text, detail="proxy password leaked")
    check("F: active proxy shows host:port only", f"{SENTINEL_HOST}:{SENTINEL_PORT}" in final_text)
    check("F: active proxy explains credentials are PIN-gated elsewhere",
          "Данные авторизации скрыты" in final_text and "PIN" in final_text)

    empty_phone_row = dict(full_row)
    empty_phone_row["phone"] = ""
    check("F: missing phone renders the short 'Недоступен' fallback (not the long Part B sentence)",
          "Номер: Недоступен" in final_text_fn("testmgr", empty_phone_row))

    disabled_proxy_row = dict(full_row)
    disabled_proxy_row["proxy_enabled"] = 0
    disabled_text = final_text_fn("testmgr", disabled_proxy_row)
    check("F: disabled proxy renders the exact informational text",
          "Отключён — используется прямое подключение" in disabled_text)
    check("F-SECURITY: disabled-proxy message never contains the login", SENTINEL_LOGIN not in disabled_text)
    check("F-SECURITY: disabled-proxy message never contains the password", SENTINEL_PASSWORD not in disabled_text)

    no_proxy_row = dict(full_row)
    no_proxy_row["proxy_host"] = ""
    no_proxy_row["proxy_port"] = ""
    no_proxy_text = final_text_fn("testmgr", no_proxy_row)
    check("F: no-proxy state renders the exact informational text",
          "Не подключён — используется прямое подключение" in no_proxy_text)
    check("F-SECURITY: no-proxy message never contains the login", SENTINEL_LOGIN not in no_proxy_text)
    check("F-SECURITY: no-proxy message never contains the password", SENTINEL_PASSWORD not in no_proxy_text)

    # --- PIN-GATED REGRESSION: the device-login flow's full reveal must be
    # completely UNCHANGED by this patch (full phone + full host:port:login:
    # password remain available ONLY behind the verified danger PIN) --------
    def fake_pxm_full_reveal_text_with_real_creds(key):
        cred = f"{SENTINEL_HOST}:{SENTINEL_PORT}:{SENTINEL_LOGIN}:{SENTINEL_PASSWORD}"
        return f"🔐 Полный proxy\n\nМенеджер:\n{key}\n\nСкопируйте строку:\n\n`{cred}`"

    ns_pin = extract_and_exec(
        panel_tree, ["_devlogin_proxy_status_block", "_devlogin_phone_and_account_text"],
        {"_pxm_full_reveal_text": fake_pxm_full_reveal_text_with_real_creds},
    )
    pin_gated_proxy_text = ns_pin["_devlogin_proxy_status_block"]("testmgr", full_row)
    check("PIN-GATED REGRESSION: the verified-PIN device-login reveal STILL shows the "
          "full host:port:login:password string (unchanged by the Part F safe-display fix)",
          f"{SENTINEL_HOST}:{SENTINEL_PORT}:{SENTINEL_LOGIN}:{SENTINEL_PASSWORD}" in pin_gated_proxy_text,
          detail=pin_gated_proxy_text)
    pin_gated_phone, _account = ns_pin["_devlogin_phone_and_account_text"](full_row)
    check("PIN-GATED REGRESSION: the verified-PIN device-login screen STILL exposes the "
          "full unmasked phone (unchanged by the Part F safe-display fix)",
          pin_gated_phone == SENTINEL_PHONE, detail=pin_gated_phone)

    # ======================================================================
    # Part H -- callback_data length (regression proof + new-scheme safety)
    # ======================================================================
    long_source_key = "instagram_campaign_2026_summer_promo_extended"  # 47 chars, realistic
    long_manager_key = "manager_reserve_account_batch_july_2026_007"    # 45 chars, realistic
    old_style_callback = f"cmd:/source link {long_source_key} {long_manager_key}".encode("utf-8")
    check("H: OLD raw-key callback scheme DOES exceed 64 bytes for realistic long keys "
          "(proves the fix was not optional)",
          len(old_style_callback) > 64, detail=str(len(old_style_callback)))

    max_int64 = 9223372036854775807
    new_style_callback = f"srcpick:{max_int64}:{max_int64}".encode("utf-8")
    check("H: NEW numeric-id callback scheme stays <=64 bytes even for adversarial near-int64-max ids",
          len(new_style_callback) <= 64, detail=str(len(new_style_callback)))

    pick_buttons_node = last_def(panel_tree, "_source_pick_buttons")
    pick_buttons_src = _src_without_docstring(pick_buttons_node)
    check("H: _source_pick_buttons no longer embeds the raw manager_key/source_key f-string in callback_data",
          "cmd:/source link {key}" not in pick_buttons_src and "cmd:/source link ' + key" not in pick_buttons_src)
    check("H: _source_pick_buttons embeds only the srcpick:<id>:<id> numeric scheme",
          "srcpick:{source_id}:{manager_id}" in pick_buttons_src)

    srcpick_handler_src = src_of(panel_tree, "on_callback")
    check("H: on_callback resolves srcpick: BEFORE the generic cmd: branch",
          srcpick_handler_src.index("srcpick:") < srcpick_handler_src.index("'cmd:'"))
    check("H: srcpick: handler rebuilds the UNCHANGED '/source link <key> <mk>' command text "
          "(no duplicated source-assignment logic)",
          "/source link {source_key} {manager_key}" in srcpick_handler_src)

    # --- stale/unknown id resolution safety (real temp SQLite) -------------
    tmpd = tempfile.mkdtemp(prefix="tdimport_convergence_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE traffic_sources(source_key TEXT PRIMARY KEY, name TEXT, description TEXT, status TEXT, created_at TEXT, updated_at TEXT)")
    con.execute("INSERT INTO traffic_sources(source_key, name, description, status, created_at, updated_at) VALUES('insta','Instagram','', 'active','','')")
    con.commit()
    real_rowid = con.execute("SELECT rowid FROM traffic_sources WHERE source_key='insta'").fetchone()[0]
    con.close()

    def fake_connect():
        c = sqlite3.connect(db)
        c.row_factory = sqlite3.Row
        return c

    ns = extract_and_exec(
        panel_tree, ["_traffic_sources_rows", "_traffic_source_row_by_id"],
        {"_connect_panel_db": fake_connect, "_ensure_structure_tables_sync": lambda: None},
    )
    by_id_fn = ns["_traffic_source_row_by_id"]
    resolved = by_id_fn(real_rowid)
    check("H: a real rowid resolves back to the exact right source_key",
          resolved.get("source_key") == "insta", detail=str(resolved))
    check("H: an unknown/stale rowid resolves safely to an empty dict (no crash, no wrong match)",
          by_id_fn(real_rowid + 999) == {})
    check("H: a non-numeric id resolves safely to an empty dict", by_id_fn("not-a-number") == {})
    check("H: a negative/zero id resolves safely to an empty dict", by_id_fn(-1) == {} and by_id_fn(0) == {})

    # ======================================================================
    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL TDIMPORT ONBOARDING CONVERGENCE SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
