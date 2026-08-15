from __future__ import annotations

import ast
import asyncio
import datetime as dt
from pathlib import Path
from typing import Any


PROJECT = Path(__file__).resolve().parents[1]
MAIN = PROJECT / "main.py"

# PEERFLOOD RECOVERY 20260812: filename kept unchanged (manifests/tooling may
# reference it), but the contract it asserts changed. Before this date,
# LIMITED+PeerFlood was a permanent internal deny ("fail-closed" -- hence the
# original name). Now the health gate allows one controlled real-send
# attempt per tick for LIMITED+PeerFlood (see _tp_hg_send_allowed); only
# BLOCKED and an explicit manual mark still fail closed. This file verifies
# BOTH halves: the new allow-through for PeerFlood/LIMITED, and that
# BLOCKED/manual_mark/an unexpired FloodWait cooldown still deny exactly as
# before.


def defs(tree: ast.AST, name: str):
    return sorted(
        [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == name
        ],
        key=lambda node: node.lineno,
    )


def segment(source: str, node: ast.AST) -> str:
    return ast.get_source_segment(source, node) or ""


async def runtime_gate(source: str, tree: ast.Module) -> None:
    gate_node = defs(tree, "_tp_hg_send_allowed")[0]
    isolated = ast.Module(body=[gate_node], type_ignores=[])
    ast.fix_missing_locations(isolated)

    current: dict[str, Any] = {}

    async def row_loader(_key: str):
        return dict(current)

    def norm(value: str) -> str:
        return str(value or "").strip().lower()

    def family(*, status: str, error_class: str) -> str:
        if status == "blocked":
            return "blocked"
        value = str(error_class or "").lower()
        if "floodwait" in value:
            return "floodwait"
        return "peerflood"

    def parse_iso(value: str):
        value = str(value or "").strip()
        return dt.datetime.fromisoformat(value) if value else None

    namespace: dict[str, Any] = {
        "Tuple": tuple,
        "datetime": dt.datetime,
        "TP_HG_STATUS_LIMITED": "limited",
        "TP_HG_STATUS_BLOCKED": "blocked",
        "_TP_HG_FAMILY_FLOODWAIT": "floodwait",
        "_TP_HG_GATE_DENY_BLOCKED": "blocked",
        "_TP_HG_GATE_DENY_MANUAL_MARK": "manual_mark_active",
        "_TP_HG_GATE_DENY_FLOODWAIT_COOLDOWN": "floodwait_cooldown_active",
        "_TP_HG_GATE_DENY_LIMITED_RESTRICTION": "limited_restriction_active",
        "_TP_HG_GATE_ALLOW_LIMITED_PROBE": "limited_probe_passthrough",
        "_tp_hg_norm_key": norm,
        "_tp_hg_row": row_loader,
        "_tp_hg_restriction_family": family,
        "_tp_hg_parse_iso": parse_iso,
    }

    exec(compile(isolated, str(MAIN), "exec"), namespace, namespace)
    gate = namespace["_tp_hg_send_allowed"]

    # PeerFlood/LIMITED: no longer a permanent deny -- one controlled probe
    # allowed per tick, both for a brand-new dialog and an existing one.
    current.update({
        "health_status": "limited",
        "error_class": "PeerFloodError",
        "error_source": "client_auto_send",
        "cooldown_until": "",
    })
    assert await gate("darias", new_dialog=True) == (
        True, "limited_probe_passthrough"
    )
    assert await gate("darias", new_dialog=False) == (
        True, "limited_probe_passthrough"
    )

    # BLOCKED safety: unchanged, still denies unconditionally.
    current.clear()
    current.update({
        "health_status": "blocked",
        "error_class": "AuthKeyUnregisteredError",
        "error_source": "client_auto_send",
        "cooldown_until": "",
    })
    assert await gate("darias", new_dialog=False) == (False, "blocked")

    # manual_mark safety: unchanged, still denies even for the PeerFlood
    # family that is otherwise allowed through above.
    current.clear()
    current.update({
        "health_status": "limited",
        "error_class": "ManualLimited",
        "error_source": "manual_mark",
        "cooldown_until": "",
    })
    assert await gate("darias", new_dialog=False) == (
        False, "manual_mark_active"
    )

    # FloodWait safety: unchanged, cooldown still gates a controlled retry.
    current.clear()
    current.update({
        "health_status": "limited",
        "error_class": "FloodWaitError",
        "error_source": "client_auto_send",
        "cooldown_until": (
            dt.datetime.utcnow().replace(microsecond=0)
            + dt.timedelta(minutes=30)
        ).isoformat(),
    })
    assert await gate("darias", new_dialog=False) == (
        False, "floodwait_cooldown_active"
    )

    current["cooldown_until"] = (
        dt.datetime.utcnow().replace(microsecond=0)
        - dt.timedelta(minutes=1)
    ).isoformat()
    assert await gate("darias", new_dialog=False) == (
        True, "floodwait_cooldown_elapsed"
    )

    assert await gate(
        "darias", new_dialog=True, recovery_probe=True
    ) == (True, "recovery_probe")


def main() -> None:
    source = MAIN.read_bytes().decode("utf-8-sig").replace("\r\n", "\n")
    compile(source, str(MAIN), "exec")
    tree = ast.parse(source, filename=str(MAIN))

    gate_nodes = defs(tree, "_tp_hg_send_allowed")
    send_nodes = defs(tree, "_send_manager_private")
    send_ex_nodes = defs(tree, "_send_manager_private_ex")
    profile_nodes = defs(tree, "_process_profile_reminders_once")
    followup_nodes = defs(tree, "_process_post_manual_followups_once")

    assert len(gate_nodes) == 1
    assert len(send_nodes) == 4
    assert len(send_ex_nodes) == 1
    assert len(profile_nodes) == 2
    assert len(followup_nodes) == 2

    gate_text = segment(source, gate_nodes[0])
    active_send_text = segment(source, send_nodes[-1])
    ex_text = segment(source, send_ex_nodes[0])
    profile_original_text = segment(source, profile_nodes[0])
    followup_original_text = segment(source, followup_nodes[0])

    assert "existing_dialog_allowed" not in gate_text
    # PEERFLOOD RECOVERY 20260812: the gate no longer returns the legacy deny
    # reason for LIMITED -- it now allows through with the new reason.
    assert "return False, _TP_HG_GATE_DENY_LIMITED_RESTRICTION" not in gate_text
    assert "_TP_HG_GATE_ALLOW_LIMITED_PROBE" in gate_text
    assert "_TP_HG_GATE_DENY_BLOCKED" in gate_text
    assert "_TP_HG_GATE_DENY_MANUAL_MARK" in gate_text
    # The fail-closed gate-error handling now lives in _send_manager_private_ex
    # (the extracted send body); the active _send_manager_private is a thin
    # wrapper that delegates to it.
    assert "gate_error_fail_closed" in ex_text
    assert "_send_manager_private_ex" in active_send_text

    for text in (
        ex_text,
        active_send_text,
        profile_original_text,
        followup_original_text,
    ):
        assert "gate_error_fail_open" not in text
        assert "gate_allowed_tick = True" not in text
        assert "still_allowed = True" not in text

    asyncio.run(runtime_gate(source, tree))
    print("TG_HEALTH_PEERFLOOD_FAILCLOSED_SELFTEST_PASS")


if __name__ == "__main__":
    main()
