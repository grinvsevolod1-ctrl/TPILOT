# -*- coding: utf-8 -*-
"""tools/proxy_notification_routing_selftest.py -- offline selftest for the
PROXY RENEWAL RELIABILITY R6 panel_bot.py button routing (20260808).

Covers the three NEW notification kinds introduced by R5 (proxy_autorenew_
unverified / proxy_autorenew_blocked / proxy_balance_low, the last of which
was previously unrouted and fell through to a bare back-to-panel) plus a
regression check that the existing proxy_autorenew_failed builder is
untouched. No new callback handlers were added anywhere in this project --
every button below points at an existing route (ppool:sync, ppool:card:,
renew:calc:, prn:root).

AST-extraction of the pure button builders + a structural (source-text)
check that _panel_notification_loop's routing table dispatches each kind to
the right builder. No network, no Telegram, no real panel_bot.py import
(same idiom as the other panel-side proxy selftests here).

Run:  python tools\\proxy_notification_routing_selftest.py
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


def find_defs(tree, name):
    return [n for n in tree.body if getattr(n, "name", None) == name]


def extract(tree, names, ns):
    nodes = [last_def(tree, n) for n in names]
    module = "\n\n".join(ast.unparse(n) for n in nodes)
    out = dict(ns)
    exec(compile(module, "<panel notification-routing extract>", "exec"), out)
    return out


class _FakeBtn:
    def __init__(self, text, data):
        self.text = text
        self.data = data if isinstance(data, (bytes, bytearray)) else str(data).encode()

    @staticmethod
    def inline(text, data):
        return _FakeBtn(text, data)


def _flatten_data(buttons) -> list[bytes]:
    out = []
    for row in buttons:
        for btn in row:
            out.append(btn.data)
    return out


def main() -> int:
    tree = ast.parse(PANEL_PY.read_text(encoding="utf-8-sig"))

    check("_prenew_autorenew_unverified_buttons is defined exactly once", len(find_defs(tree, "_prenew_autorenew_unverified_buttons")) == 1)
    check("_prenew_autorenew_blocked_buttons is defined exactly once", len(find_defs(tree, "_prenew_autorenew_blocked_buttons")) == 1)
    check("_prenew_balance_low_buttons is defined exactly once", len(find_defs(tree, "_prenew_balance_low_buttons")) == 1)

    ns = extract(
        tree,
        [
            "_prenew_autorenew_unverified_buttons", "_prenew_autorenew_blocked_buttons",
            "_prenew_balance_low_buttons", "_prenew_autorenew_failed_buttons", "_prenew_notification_buttons",
        ],
        {"Button": _FakeBtn, "re": re, "_back_to_panel_buttons": lambda: [[_FakeBtn.inline("🔙 Назад", b"panel:back")]]},
    )
    unverified_fn = ns["_prenew_autorenew_unverified_buttons"]
    blocked_fn = ns["_prenew_autorenew_blocked_buttons"]
    balance_fn = ns["_prenew_balance_low_buttons"]
    failed_fn = ns["_prenew_autorenew_failed_buttons"]
    warning_fn = ns["_prenew_notification_buttons"]

    print("\n-- proxy_autorenew_unverified: NEVER offers a spend-capable button --")
    body_unverified = "⚠️ Продление прокси не подтверждено\n\nМенеджер: Test | @tuser\nКлюч: mgr_x\nПрокси: 1.2.3.4:1080\nДействует до: 09.08.2026\n\nСписание могло пройти, но Proxy-Seller пока не подтвердил новый срок.\nПовторное списание автоматически не выполняется.\n\nЧто делать: синхронизируйте пул и проверьте прокси.\n\nlease_id: 777"
    btns_unverified = unverified_fn(body_unverified)
    data_unverified = _flatten_data(btns_unverified)
    check("unverified: includes ppool:sync (safe reconcile)", b"ppool:sync" in data_unverified, data_unverified)
    check("unverified: includes ppool:card:777 (safe read-only)", b"ppool:card:777" in data_unverified, data_unverified)
    check(
        "unverified: NEVER offers renew:calc: or renew:confirm: (no path back to a second spend)",
        not any(d.startswith(b"renew:calc:") or d.startswith(b"renew:confirm:") for d in data_unverified),
        data_unverified,
    )
    check("unverified: falls back to sync+back-to-panel on a body with NO lease_id (never crashes)", len(unverified_fn("garbage, no marker at all")) >= 1)

    print("\n-- proxy_autorenew_blocked: points at renewal/balance config, not a retry --")
    body_blocked = "⚠️ Автопродление заблокировано\n\nМенеджер: Test | @tuser\nКлюч: mgr_x\nПрокси: 1.2.3.4:1080\nДействует до: 09.08.2026\n\nПричина: аварийная остановка автопродления включена (emergency stop)\nЧто делать: проверьте настройки автопродления (лимиты/баланс/пауза).\n\nlease_id: 888"
    btns_blocked = blocked_fn(body_blocked)
    data_blocked = _flatten_data(btns_blocked)
    check("blocked: includes prn:root (renewal/balance config screen)", b"prn:root" in data_blocked, data_blocked)
    check("blocked: includes ppool:card:888", b"ppool:card:888" in data_blocked, data_blocked)
    check(
        "blocked: NEVER offers renew:calc: or renew:confirm: (a guard block isn't a per-lease retry)",
        not any(d.startswith(b"renew:calc:") or d.startswith(b"renew:confirm:") for d in data_blocked),
        data_blocked,
    )
    check("blocked: never crashes on a malformed body", len(blocked_fn("")) >= 1)

    print("\n-- proxy_balance_low: account-wide alert, no lease_id needed --")
    btns_balance = balance_fn("⚠️ Низкий баланс Proxy-Seller\n\nТекущий баланс: 12.40 $\nПорог: 20.00 $")
    data_balance = _flatten_data(btns_balance)
    check("balance_low: includes prn:root", b"prn:root" in data_balance, data_balance)
    check("balance_low: never crashes on any input", len(balance_fn(None)) >= 1)

    print("\n-- regression: proxy_autorenew_failed keeps its manual-retry button (money NOT confirmed spent) --")
    body_failed = "⚠️ Не удалось продлить прокси\n\nМенеджер: Test | @tuser\nКлюч: mgr_x\nПрокси: 1.2.3.4:1080\nДействует до: 09.08.2026\n\nПричина: Провайдер больше не находит этот прокси.\nЧто делать: проверьте прокси и при необходимости замените его.\n\nlease_id: 999"
    data_failed = _flatten_data(failed_fn(body_failed))
    check("failed: still offers renew:calc:999 (manual retry -- distinct from unverified, no confirmed spend yet)", b"renew:calc:999" in data_failed, data_failed)

    print("\n-- callback_data stays within Telegram's 64-byte inline-button limit --")
    big_lease_id = 9_223_372_036_854_775_800  # near int64 max, adversarial
    for label, fn, body_tpl in (
        ("unverified", unverified_fn, "lease_id: {}"),
        ("blocked", blocked_fn, "lease_id: {}"),
    ):
        data = _flatten_data(fn(body_tpl.format(big_lease_id)))
        check(f"29. {label} builder stays <=64 bytes for an adversarial near-int64 lease_id", all(len(d) <= 64 for d in data), data)
    check("29b. balance_low builder (no lease_id) stays <=64 bytes", all(len(d) <= 64 for d in _flatten_data(balance_fn("x"))))

    print("\n-- structural: _panel_notification_loop routes all three new kinds --")
    loop_defs = find_defs(tree, "_panel_notification_loop")
    check("_panel_notification_loop is defined exactly once", len(loop_defs) == 1, len(loop_defs))
    loop_src = ast.unparse(loop_defs[0]) if loop_defs else ""

    def _kind_routed(kind: str, builder_call: str) -> bool:
        # ast.unparse normalizes string quoting -- match quote-agnostically.
        return (f'"{kind}"' in loop_src or f"'{kind}'" in loop_src) and builder_call in loop_src

    check("routing table dispatches 'proxy_autorenew_unverified' to its builder", _kind_routed("proxy_autorenew_unverified", "_prenew_autorenew_unverified_buttons(body)"), loop_src[:2000])
    check("routing table dispatches 'proxy_autorenew_blocked' to its builder", _kind_routed("proxy_autorenew_blocked", "_prenew_autorenew_blocked_buttons(body)"), loop_src[:2000])
    check("routing table dispatches 'proxy_balance_low' to its builder", _kind_routed("proxy_balance_low", "_prenew_balance_low_buttons(body)"), loop_src[:2000])
    check("routing table still dispatches 'proxy_autorenew_failed' (unchanged)", _kind_routed("proxy_autorenew_failed", "_prenew_autorenew_failed_buttons(body)"), loop_src[:2000])
    check("routing table still dispatches 'proxy_renew_warning' (unchanged)", _kind_routed("proxy_renew_warning", "_prenew_notification_buttons(body)"), loop_src[:2000])
    check("SAFETY: no NEW callback prefix was introduced -- only ppool:/renew:/prn: routes referenced", all(
        b.startswith((b"ppool:", b"renew:", b"prn:", b"panel:")) for b in (data_unverified + data_blocked + data_balance + data_failed)
    ), data_unverified + data_blocked + data_balance + data_failed)

    print(f"\n{'='*70}")
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
