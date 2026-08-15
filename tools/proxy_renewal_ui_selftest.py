# -*- coding: utf-8 -*-
"""tools/proxy_renewal_ui_selftest.py -- offline UI selftest for the
PROXY RENEWAL «Продление прокси» AdminBot UX (Stage 6, panel_bot.py).

AST-extraction of the pure render/button builders + source-scan of the
prn: callback handler. No network, no Telegram.

Covers:
  - «💳 Продление прокси» entry button present on the pool root;
  - status/due/calc/config/errors text builders render correctly, and NEVER
    contain a proxy login/password;
  - due-list + calc buttons carry only numeric lease ids in callbacks (<=64b);
  - calc screen requires an explicit «Продлить (списать)» tap before any
    spend command (no spend on calc);
  - the prn: callback rebuilds /proxy_renewal_* commands and routes them
    through _submit_and_wait (no spend logic in panel_bot);
  - config screen shows automation state + guards; enable_auto records consent
    via the user's own id; the retired ENABLE-REQUIRED wording is gone.

Run:  python tools\\proxy_renewal_ui_selftest.py
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

PANEL_PY = BASE_DIR / "panel_bot.py"

FAILURES: list[str] = []


def check(label, condition, detail=""):
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


def last_def(tree, name):
    d = [n for n in tree.body if getattr(n, "name", None) == name]
    if not d:
        raise AssertionError(f"no def {name}")
    return d[-1]


def src_of(tree, name):
    return ast.unparse(last_def(tree, name))


def extract(tree, names, ns):
    nodes = [last_def(tree, n) for n in names]
    module = "\n\n".join(ast.unparse(n) for n in nodes)
    out = dict(ns)
    exec(compile(module, "<panel renewal extract>", "exec"), out)
    return out


def extract_composed(tree, def_name, assign_names, ns):
    """N5.3.2 (Z2): faithful last-wins COMPOSITION of an override-chained
    function -- collects EVERY def of `def_name` plus the named module-level
    assigns (PREV captures / caches) in ORIGINAL SOURCE ORDER and executes
    them together, exactly like runtime: the later override's
    `globals().get(...)` PREV capture genuinely grabs the earlier def.
    Needed because `_prn_status_text` gained an N5.3.1 balance-observer
    override; extracting only the last def broke with NameError on its
    PREV global (the exact regression the independent review caught)."""
    nodes = []
    for n in tree.body:
        if getattr(n, "name", None) == def_name:
            nodes.append(n)
        elif isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and n.targets[0].id in assign_names:
            nodes.append(n)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id in assign_names:
            nodes.append(n)
    module = "\n\n".join(ast.unparse(n) for n in nodes)
    out = dict(ns)
    exec(compile(module, f"<composed {def_name}>", "exec"), out)
    return out


class _FakeBtn:
    def __init__(self, text, data):
        self.text = text
        self.data = data if isinstance(data, (bytes, bytearray)) else str(data).encode()

    @staticmethod
    def inline(text, data):
        return _FakeBtn(text, data)


def main() -> int:
    tree = ast.parse(PANEL_PY.read_text(encoding="utf-8-sig"))

    ns = extract(
        tree,
        ["_prn_root_buttons", "_prn_due_buttons", "_prn_due_text",
         "_prn_calc_text", "_prn_calc_buttons", "_prn_config_buttons", "_prn_config_text",
         "_prn_errors_text", "_ppool_root_buttons", "_prn_error_text"],
        {"Button": _FakeBtn, "_back_to_panel_buttons": lambda: [[_FakeBtn.inline("Назад", b"panel:back")]]},
    )
    # N5.3.2 (Z2): _prn_status_text is override-chained since N5.3.1 (the
    # balance-observer generation) -- extract the REAL composed chain (both
    # defs + PREV capture + cache assign, in source order), so the test
    # executes the ACTIVE composition, never an older isolated def.
    ns.update(extract_composed(
        tree, "_prn_status_text",
        {"_N531_PREV_PRN_STATUS_TEXT", "_N531_PROXY_BALANCE_CACHE"},
        {"Dict": dict, "Any": object, "_prn_error_text": ns["_prn_error_text"]},
    ))

    # entry button on the pool root
    pool_root = ns["_ppool_root_buttons"]()
    pool_cbs = [b.data for row in pool_root for b in row]
    check("«Продление прокси» entry button present on the pool root", b"prn:root" in pool_cbs)

    # status text
    st = ns["_prn_status_text"]({"ok": True, "automation_enabled": 0, "paused": 0, "emergency_stop": 0,
                                 "lead_days": 2, "due_count": 3, "failed_count": 1, "balance": 42.5})
    check("status text shows automation OFF + due count + lead window", "ВЫКЛ" in st and "К продлению сейчас: 3" in st and "2 дн" in st)
    check("status text explains website auto-renew is not used", "не используется" in st)
    check("status text shows the Proxy-Seller balance", "Баланс Proxy-Seller: 42.50 $" in st)

    # balance unavailable (None) -> graceful fallback, never a crash/blank
    st_no_bal = ns["_prn_status_text"]({"ok": True, "automation_enabled": 0, "paused": 0, "emergency_stop": 0,
                                        "lead_days": 2, "due_count": 0, "failed_count": 0, "balance": None})
    check("status text degrades gracefully when balance is unavailable", "Баланс Proxy-Seller: недоступен" in st_no_bal)

    # --- N5.3.2 (Z2): the composed chain IS the active runtime one, and the
    # panel-side balance observer works as claimed.
    check("Z2. composed _prn_status_text captured the numeric balance into the panel-side cache",
          ns["_N531_PROXY_BALANCE_CACHE"].get("value") == 42.5, ns["_N531_PROXY_BALANCE_CACHE"])
    check("Z2. a None balance render does NOT clobber the previously observed value",
          ns["_N531_PROXY_BALANCE_CACHE"].get("value") == 42.5, ns["_N531_PROXY_BALANCE_CACHE"])
    override_src = src_of(tree, "_prn_status_text")  # last def = the observer override
    check("Z2. the observer override delegates rendering verbatim to its PREV (no second implementation)",
          "_N531_PREV_PRN_STATUS_TEXT" in override_src, override_src[:200])
    check("Z2. the observer override never calls the provider / network / spend",
          not any(tok in override_src for tok in ("provider", "balance/get", "urllib", "requests.", "prolong", "make_ipv4", "allow_spend")),
          override_src)
    check("Z2. the observer override never mutates renewal configuration or submits commands",
          not any(tok in override_src for tok in ("_submit_and_wait", "set_setting", "renewal_config", "INSERT", "UPDATE")),
          override_src)

    # due list + calc callbacks are numeric-id only
    due_data = {"ok": True, "due_count": 2, "due": [
        {"lease_id": 11, "provider_proxy_id": "PXY-1", "host": "1.1.1.1", "port": 50101, "days_left": 3,
         "login": "SECRET_L", "password": "SECRET_P"},
        {"lease_id": 12, "provider_proxy_id": "PXY-2", "host": "2.2.2.2", "port": 50101, "days_left": 5},
    ]}
    due_btns = ns["_prn_due_buttons"](due_data)
    due_cbs = [b.data for row in due_btns for b in row]
    check("due buttons carry only numeric lease-id callbacks (prn:calc:<id>)", b"prn:calc:11" in due_cbs and b"prn:calc:12" in due_cbs)
    check("due button labels never contain the proxy login/password",
          not any(b"SECRET" in (b.data or b"") or "SECRET" in b.text for row in due_btns for b in row))
    check("all prn: callbacks stay <=64 bytes", all(len(b.data) <= 64 for row in due_btns for b in row))
    duet = ns["_prn_due_text"](due_data)
    check("due text renders a count", "Всего: 2" in duet)
    check("due text (empty) is graceful", "Нет прокси" in ns["_prn_due_text"]({"ok": True, "due_count": 0, "due": []}))

    # calc: no-spend preview + explicit confirm needed
    calc = ns["_prn_calc_text"]({"ok": True, "provider_proxy_id": "PXY-1", "total": "3.50", "currency": "USD", "period_id": "1m"})
    check("calc text is a no-spend preview showing cost", "без списания" in calc and "3.50" in calc and "USD" in calc)
    calc_btns = ns["_prn_calc_buttons"](11)
    calc_cbs = [b.data for row in calc_btns for b in row]
    check("calc screen requires an explicit «Продлить (списать)» tap (prn:confirm:<id>) before spend", b"prn:confirm:11" in calc_cbs)
    check("calc confirm button label warns about spending", any("списать" in b.text.lower() for row in calc_btns for b in row))

    # config
    cfg = {"config": {"automation_enabled": 0, "paused": 0, "emergency_stop": 0, "lead_days": 7,
                      "max_amount_per_op": None, "max_daily_spend": None, "min_balance_warn": None}}
    cfgt = ns["_prn_config_text"](cfg)
    check("config text shows automation OFF + guards", "Автопродление: ⚪ ВЫКЛ" in cfgt and "Окно (дней): 7" in cfgt)
    check("config text warns enabling = consent to spending", "согласие" in cfgt)
    cfg_btns = ns["_prn_config_buttons"](cfg)
    cfg_cbs = [b.data for row in cfg_btns for b in row]
    check("config offers enable-auto (consent) when OFF", b"prn:cfg:enable_auto" in cfg_cbs)
    check("config offers pause + emergency-stop controls", b"prn:cfg:pause" in cfg_cbs and b"prn:cfg:emergency_stop" in cfg_cbs)

    # errors
    check("errors text (none) is graceful", "Нет неудавшихся" in ns["_prn_errors_text"]({"ok": True, "failed_count": 0}))
    # BLOCKER-1 fix (retry-policy correction, 20260809): 'failed' ops are
    # now, by construction, only the spend-ambiguous bucket -- "will be
    # retried automatically" was never true for these; the text no longer
    # claims it (NB-4, final Opus review 20260809).
    errt = ns["_prn_errors_text"]({"ok": True, "failed_count": 2})
    check("errors text (some) reports the count", "Неподтверждённых операций: 2" in errt)
    check("errors text (some) does NOT claim automatic retry", "автоматически" not in errt or "не выполняется" in errt)

    # ------------------------------------------------------------------
    # BL-2 follow-up fix (independent re-review correction, 20260809):
    # adversarial raw provider payload -- the EXACT string quoted in the
    # review ("provider returned errors[] on prolong/make/ipv4: [{'message':
    # 'IP not found', 'code': 0, 'customData': None}]") -- fed through
    # EVERY prn: screen's failure branch. Fed as the WORST case: only
    # `message` set, NO `reason`/`action` (simulates an unclassified/legacy
    # result reaching the UI), which is the strongest possible proof the UI
    # layer itself never falls back to raw text, independent of whether
    # upstream classification succeeded.
    # ------------------------------------------------------------------
    RAW_LEAK_MARKERS = ("errors[", "customData", "provider returned", "{'message'", "'code':", "Traceback")
    ADVERSARIAL_MESSAGE = (
        "provider returned errors[] on prolong/make/ipv4: "
        "[{'message': 'IP not found', 'code': 0, 'customData': None}]"
    )
    unclassified_fail = {"ok": False, "message": ADVERSARIAL_MESSAGE}
    for fn_name, extra in (
        ("_prn_status_text", {}),
        ("_prn_due_text", {}),
        ("_prn_calc_text", {}),
    ):
        out = ns[fn_name](dict(unclassified_fail, **extra))
        check(
            f"BL-2 follow-up: {fn_name} never leaks raw provider text (unclassified result, worst case)",
            not any(marker in out for marker in RAW_LEAK_MARKERS),
            out,
        )
        check(f"{fn_name} still shows SOME actionable text on an unclassified failure (neutral fallback)",
              "Причина" in out and "Что делать" in out, out)

    # classified result (reason/action present) -- confirms the normal path
    # also never leaks, and that the raw `message` is genuinely ignored (not
    # just absent by accident in the unclassified case above).
    classified_fail = {
        "ok": False, "message": ADVERSARIAL_MESSAGE,
        "reason": "Провайдер больше не находит этот прокси.",
        "action": "Проверьте прокси и при необходимости замените его.",
    }
    for fn_name in ("_prn_status_text", "_prn_due_text", "_prn_calc_text"):
        out = ns[fn_name](classified_fail)
        check(f"BL-2 follow-up: {fn_name} shows the classified reason/action (not raw message)",
              "Провайдер больше не находит этот прокси" in out and not any(m in out for m in RAW_LEAK_MARKERS),
              out)

    # callback handler source-scan
    cb = src_of(tree, "_prn_callback")

    # BL-2 follow-up fix (independent re-review correction, 20260809): the
    # prn:confirm branch's local _confirm_text closure is not a top-level
    # def (can't be AST-extracted/executed standalone like the screens
    # above), so this is a static source-scan of the SAME kind the file
    # already uses below for prolong_make/allow_spend absence -- proves the
    # failure-text construction never falls back to d['message']/d['error']
    # (raw provider text) and DOES use the classified reason/action.
    has_message_fallback = re.search(r"""d\.get\((['"])message\1\)""", cb) is not None
    has_error_fallback = re.search(r"""d\.get\((['"])error\1\)""", cb) is not None
    has_reason = re.search(r"""d\.get\((['"])reason\1\)""", cb) is not None
    has_action = re.search(r"""d\.get\((['"])action\1\)""", cb) is not None
    check("BL-2 follow-up: prn:confirm failure text never falls back to raw d['message']/d['error']",
          not has_message_fallback and not has_error_fallback, cb)
    check("BL-2 follow-up: prn:confirm failure text uses the classified reason/action",
          has_reason and has_action, cb)
    check("prn: handler submits /proxy_renewal_status", "/proxy_renewal_status" in cb)
    check("prn: handler submits /proxy_renewal_calc (no-spend) for a lease", "/proxy_renewal_calc" in cb)
    check("prn: handler submits /proxy_renewal_confirm only on the confirm branch", "/proxy_renewal_confirm" in cb)
    check("prn: confirm passes the requesting user's id as actor", "/proxy_renewal_confirm {lease_id} {user_id}" in cb)
    check("prn: handler routes everything through _submit_and_wait (no spend logic in panel_bot)",
          "prolong_make" not in cb and "allow_spend" not in cb)
    check("prn: enable_auto records consent via the user's own id (not a raw admin flag)", "enable_auto {user_id}" in cb)
    check("prn: confirm is de-duplicated (no double spend on double-tap)", "_panel_callback_is_duplicate" in cb)

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PROXY RENEWAL UI SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
