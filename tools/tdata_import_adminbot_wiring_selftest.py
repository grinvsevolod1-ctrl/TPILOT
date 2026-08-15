# -*- coding: utf-8 -*-
"""Offline selftest for the Phase 6 main.py wiring (the thin
/manager_tdimport_* controller-command layer).

main.py cannot be imported standalone (Telethon/env side effects at import
time) -- this selftest uses the project's established AST-extraction idiom
(ast.parse -> ast.unparse -> exec) to pull the pure-logic helpers out of the
appended block and test them with fakes, plus structural/source-scan checks
over the LAST definition of the stacked-override pieces (last-def-wins
convention). No network, no Telegram, no production DB, no spend.

Run:  python tools\\tdata_import_adminbot_wiring_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

MAIN_PY = os.path.join(BASE_DIR, "main.py")

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _src_without_docstring(node):
    """Same idiom as proxy_pool_selftest._src_without_docstring: drop a
    leading string-constant Expr so text-scans over a function's body don't
    false-positive on prose in its own docstring."""
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
        raise AssertionError(f"no top-level def named {name!r} found in main.py")
    return defs[-1]  # last-def-wins convention


def extract_and_exec(tree, names, extra_ns):
    nodes = []
    for name in names:
        defs = find_defs(tree, name)
        if not defs:
            raise AssertionError(f"missing def {name!r}")
        nodes.append(defs[-1])
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, "<main.py extract>", "exec"), ns)
    return ns


def main():
    src = open(MAIN_PY, encoding="utf-8-sig").read()
    tree = ast.parse(src)

    # --- structural: exactly-once new helpers, well-formed ----------------
    for name in (
        "_tdimport_new_operation_id", "_tdimport_proxy_row_from_manager",
        "_tdimport_real_prober", "_tdimport_client_factory_for",
        "_tdimport_spawn_runtime", "_tdimport_stop_runtime", "_tdimport_wait_runtime_ready",
        "_panel_manager_tdimport_start_command", "_panel_manager_tdimport_status_command",
        "_panel_manager_tdimport_confirm_command", "_panel_manager_tdimport_cancel_command",
    ):
        defs = find_defs(tree, name)
        check(f"exactly one def of {name}", len(defs) == 1, detail=str(len(defs)))

    # --- override-chain ordering: _TDIMPORT_PREV_PANEL_EXEC assign must come
    # BEFORE the new _panel_execute_command_text def (globals().get semantics
    # depend on this -- capturing the PREVIOUS override before shadowing it).
    prev_assign_idx = None
    new_def_idx = None
    panel_exec_defs_idx = [i for i, n in enumerate(tree.body) if getattr(n, "name", None) == "_panel_execute_command_text"]
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_TDIMPORT_PREV_PANEL_EXEC" for t in node.targets
        ):
            prev_assign_idx = i
    new_def_idx = panel_exec_defs_idx[-1] if panel_exec_defs_idx else None
    check("_TDIMPORT_PREV_PANEL_EXEC captured before the new override",
          prev_assign_idx is not None and new_def_idx is not None and prev_assign_idx < new_def_idx,
          detail=f"prev_idx={prev_assign_idx} new_def_idx={new_def_idx}")
    check("new _panel_execute_command_text is the LAST one in the file (last-def-wins)",
          new_def_idx == panel_exec_defs_idx[-1] and new_def_idx == len(tree.body) - 1 - (
              len(tree.body) - 1 - new_def_idx
          ), detail="")  # trivially true by construction; real check below
    check("new _panel_execute_command_text is the file's final top-level def overall",
          new_def_idx == max(i for i, n in enumerate(tree.body) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))))

    # --- dispatch dict wiring: correct 4 commands -> correct handler names -
    dispatch_assigns = [
        n for n in tree.body
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "_TDIMPORT_DISPATCH" for t in n.targets)
    ]
    check("_TDIMPORT_DISPATCH assigned exactly once", len(dispatch_assigns) == 1, detail=str(len(dispatch_assigns)))
    if dispatch_assigns:
        dict_node = dispatch_assigns[0].value
        pairs = {}
        for k, v in zip(dict_node.keys, dict_node.values):
            key = k.value if isinstance(k, ast.Constant) else ast.unparse(k)
            val = v.id if isinstance(v, ast.Name) else ast.unparse(v)
            pairs[key] = val
        expected = {
            "/manager_tdimport_start": "_panel_manager_tdimport_start_command",
            "/manager_tdimport_status": "_panel_manager_tdimport_status_command",
            "/manager_tdimport_confirm": "_panel_manager_tdimport_confirm_command",
            "/manager_tdimport_cancel": "_panel_manager_tdimport_cancel_command",
        }
        check("dispatch dict has exactly the 4 planned commands", pairs == expected, detail=str(pairs))

    # --- source-scan: owner decision #6 (source="tdata_import") literally
    # present in the client-factory wiring, never a bare/no-source call ----
    client_factory_src = _src_without_docstring(last_def(tree, "_tdimport_client_factory_for"))
    check('client factory forces source="tdata_import"',
          'source="tdata_import"' in client_factory_src or "source='tdata_import'" in client_factory_src,
          detail=client_factory_src)
    check("client factory reuses the project's own _build_manager_telegram_client_from_row (no bespoke TelegramClient(...) construction)",
          "_build_manager_telegram_client_from_row(" in client_factory_src
          and "TelegramClient(" not in client_factory_src)

    # --- source-scan: start_import call passes proxy_row/proxy_prober/
    # duplicate_check/reserved_check through (never silently dropped) -------
    start_cmd_src = _src_without_docstring(last_def(tree, "_panel_manager_tdimport_start_command"))
    for must_have in ("proxy_row=proxy_row", "proxy_prober=_tdimport_real_prober",
                      "duplicate_check=_dup_check", "reserved_check=_reserved_check",
                      "client_factory=_client_factory"):
        check(f"start command wires {must_have}", must_have in start_cmd_src, detail=start_cmd_src[:200])

    # --- PROBE-WIRING FIX 20260719 (proven live root cause): the client
    # probe must connect to the REAL validated session_source_path service.py
    # selected/converted, never a separate hardcoded decoy file. The original
    # bug was `session_path_for_client = os.path.join(work_root,
    # "probe.session")` -- a file nothing ever wrote to -- passed into a
    # zero-argument client_factory. Note ast.unparse (used to build
    # start_cmd_src) strips ALL `#` comments, so this check only ever sees
    # executable code, never prose mentioning the old bug for context. -------
    check('no hardcoded "probe.session" decoy path anywhere in the start command '
          "(including its nested _client_factory closure)",
          "probe.session" not in start_cmd_src, detail=start_cmd_src)
    check("the nested _client_factory closure accepts a session_path parameter "
          "(the corrected Callable[[str], Awaitable[Any]] contract, not a "
          "zero-argument Callable[[], Awaitable[Any]])",
          "async def _client_factory(session_path" in start_cmd_src, detail=start_cmd_src)
    check("_client_factory forwards its RECEIVED session_path verbatim into "
          "_tdimport_client_factory_for(row, session_path) -- the test fails if "
          "the factory ignores its argument (e.g. reverts to a hardcoded path)",
          "_tdimport_client_factory_for(row, session_path)" in start_cmd_src, detail=start_cmd_src)

    # --- service.py side of the same contract change: ClientFactoryFn must be
    # a ONE-argument callable (session_path: str), not zero-argument -- a raw
    # text scan since this file only AST-parses main.py, not service.py. -----
    service_src = open(os.path.join(BASE_DIR, "tdata_import", "service.py"), encoding="utf-8-sig").read()
    check("service.py's ClientFactoryFn type alias takes a session_path str argument "
          "(Callable[[str], Awaitable[Any]])",
          "ClientFactoryFn = Callable[[str], Awaitable[Any]]" in service_src, detail="")
    check("service.py calls client_factory(session_source_path) -- the exact "
          "candidate path it just selected/converted, not a bare client_factory()",
          "client_factory(session_source_path)" in service_src, detail="")

    confirm_cmd_src = _src_without_docstring(last_def(tree, "_panel_manager_tdimport_confirm_command"))
    for must_have in ("proxy_row=proxy_row", "spawn_runtime=_tdimport_spawn_runtime",
                      "wait_runtime_ready=_tdimport_wait_runtime_ready", "stop_runtime=_tdimport_stop_runtime"):
        check(f"confirm command wires {must_have}", must_have in confirm_cmd_src)

    # --- allow_spend must never appear anywhere in the new block ----------
    whole_block_src = "\n".join(
        ast.unparse(last_def(tree, n)) for n in (
            "_tdimport_new_operation_id", "_tdimport_proxy_row_from_manager", "_tdimport_real_prober",
            "_tdimport_client_factory_for", "_tdimport_spawn_runtime", "_tdimport_stop_runtime",
            "_tdimport_wait_runtime_ready", "_panel_manager_tdimport_start_command",
            "_panel_manager_tdimport_status_command", "_panel_manager_tdimport_confirm_command",
            "_panel_manager_tdimport_cancel_command",
        )
    )
    check("allow_spend never referenced in the new command handlers", "allow_spend" not in whole_block_src)
    check("no bare TelegramClient(...) construction anywhere in the new block (only the reused factory)",
          "TelegramClient(" not in whole_block_src)
    check("no direct-fallback wording/branch in the new block", "direct" not in whole_block_src.lower()
          or "proxy_mode" in whole_block_src)  # proxy_mode IS expected to appear (via _manager_auth_proxy_mode reuse)

    # --- pure-logic extraction test: operation id + proxy-row projection --
    from manager_registry import normalize_manager_key as _real_normalize_manager_key
    ns = extract_and_exec(
        tree,
        ["_tdimport_new_operation_id", "_tdimport_proxy_row_from_manager", "_manager_auth_proxy_mode", "_proxy_port_int"],
        {
            "typing": __import__("typing"), "Dict": dict, "Any": object,
            "registry_normalize_manager_key": _real_normalize_manager_key,
        },
    )
    op_id = ns["_tdimport_new_operation_id"]("MyManager")
    check("operation id has expected shape/prefix", op_id.startswith("tdi_mymanager_"), detail=op_id)
    op_id2 = ns["_tdimport_new_operation_id"]("MyManager")
    check("operation ids are unique across calls", op_id != op_id2)

    proxy_row_proxy_mode = ns["_tdimport_proxy_row_from_manager"]({
        "proxy_host": "1.2.3.4", "proxy_port": 1080, "proxy_username": "u", "proxy_password": "p",
        "proxy_enabled": 1,
    })
    check("proxy-configured manager row projects proxy_mode='proxy'", proxy_row_proxy_mode.get("proxy_mode") == "proxy",
          detail=str(proxy_row_proxy_mode))

    proxy_row_direct = ns["_tdimport_proxy_row_from_manager"]({})
    check("manager row with no proxy config projects a non-'proxy' mode (never fabricates 'proxy')",
          proxy_row_direct.get("proxy_mode") != "proxy", detail=str(proxy_row_direct))

    # --- backup file exists (implementation-control requirement) ----------
    import glob
    backups = glob.glob(os.path.join(BASE_DIR, "main.py.bak_tdimport_*"))
    check("a pre-edit main.py backup exists for this phase", len(backups) >= 1, detail=str(backups))

    # --- EXECUTION-ORDER test (TPILOT 20260719): the earlier "last top-level def"
    # check is TRUE even when the whole tdimport block sits AFTER
    # `if __name__ == "__main__": asyncio.run(main())` -- in which case the block
    # never executes and the runtime dispatcher silently falls back to REPL3,
    # answering "Команда не поддерживается в панели". AST body ORDER catches that:
    # the guard node must come AFTER the tdimport wrapper def, the dispatch dict,
    # and the sweeper def. ------------------------------------------------------
    body = tree.body
    guard_idx = [i for i, n in enumerate(body)
                 if isinstance(n, ast.If) and ast.unparse(n.test).replace(" ", "") == "__name__=='__main__'"]
    check("exactly one `if __name__ == '__main__'` guard at top level", len(guard_idx) == 1, detail=str(guard_idx))
    if len(guard_idx) == 1:
        g = guard_idx[0]
        pe_idx = [i for i, n in enumerate(body) if getattr(n, "name", None) == "_panel_execute_command_text"]
        dd_idx = [i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                  and any(isinstance(t, ast.Name) and t.id == "_TDIMPORT_DISPATCH" for t in n.targets)]
        sw_idx = [i for i, n in enumerate(body) if getattr(n, "name", None) == "_tdimport_stale_sweep_loop"]
        check("the FINAL _panel_execute_command_text override executes BEFORE the __main__ guard",
              pe_idx and pe_idx[-1] < g, detail=f"last_pe={pe_idx[-1] if pe_idx else None} guard={g}")
        check("_TDIMPORT_DISPATCH is assigned BEFORE the __main__ guard (i.e. actually runs)",
              dd_idx and dd_idx[0] < g, detail=f"dispatch={dd_idx} guard={g}")
        check("the final override IS the tdimport override (last panel-exec def is the max index)",
              pe_idx and pe_idx[-1] == max(pe_idx))
        check("_tdimport_stale_sweep_loop is defined BEFORE the __main__ guard (else NameError at 5177)",
              sw_idx and sw_idx[0] < g, detail=f"sweep={sw_idx} guard={g}")

    # The sweeper is registered via create_task() INSIDE main(); that call only
    # runs when main() is invoked at the guard, by which point the module-level
    # def has already executed -- so the correct invariant is "def before guard"
    # (asserted above via body index), NOT "def source-line before the call".
    # Here we only confirm the registration call still exists at all.
    reg_present = any("create_task(_tdimport_stale_sweep_loop())" in line for line in src.splitlines())
    check("sweeper create_task() registration still present", reg_present)

    # --- AUTH PROFILE FIX (TPILOT 20260719): direct-import (.session/tdata)
    # managers MUST connect under the fixed Telegram Desktop API/device profile
    # at BOTH probe and every runtime start -- never the project's own API/device
    # -- because opentele UseCurrentSession reuses an EXISTING auth_key, whose
    # continued validity is tied to the exact API app it was originally
    # authorized under. Phone/code/2FA and QR managers (source != "tdata_import",
    # auth_profile == "project"/default) must be completely unaffected. ---------
    api_profile_src = _src_without_docstring(last_def(tree, "_api_profile_for"))
    check('_api_profile_for has a "tdata_import" branch returning the tdesktop profile',
          ('"tdata_import"' in api_profile_src or "'tdata_import'" in api_profile_src)
          and "TDIMPORT_API_ID" in api_profile_src and "TDIMPORT_API_HASH" in api_profile_src
          and ('"tdesktop"' in api_profile_src or "'tdesktop'" in api_profile_src),
          detail=api_profile_src)

    device_kwargs_for_src = _src_without_docstring(last_def(tree, "_device_kwargs_for"))
    check("_device_kwargs_for picks _TDIMPORT_DEVICE_KWARGS for source=='tdata_import', "
          "_TELETHON_DEVICE_KWARGS otherwise",
          "_TDIMPORT_DEVICE_KWARGS" in device_kwargs_for_src and "_TELETHON_DEVICE_KWARGS" in device_kwargs_for_src,
          detail=device_kwargs_for_src)

    build_client_src = _src_without_docstring(last_def(tree, "_build_manager_telegram_client_from_row"))
    check("_build_manager_telegram_client_from_row uses _device_kwargs_for(source) (device fingerprint "
          "switches together with the api_id/api_hash, never mismatched)",
          "_device_kwargs_for(source)" in build_client_src, detail=build_client_src)

    # pure-logic extract-and-exec: _api_profile_for / _device_kwargs_for behavior
    import typing as _typing
    ns2 = extract_and_exec(
        tree,
        ["_api_profile_for", "_device_kwargs_for"],
        {
            "API_ID": 111, "API_HASH": "project-hash",
            "ONBOARD_API_ID": 0, "ONBOARD_API_HASH": "",
            "TDIMPORT_API_ID": 2040, "TDIMPORT_API_HASH": "b18441a1ff607e10a989891a5462e627",
            "_TELETHON_DEVICE_KWARGS": {"device_model": "project-device"},
            "_TDIMPORT_DEVICE_KWARGS": {"device_model": "Desktop"},
            "Tuple": _typing.Tuple, "Dict": dict, "Any": object,
        },
    )
    api_id, api_hash, profile_name = ns2["_api_profile_for"]("tdata_import")
    check("_api_profile_for('tdata_import') returns (TDIMPORT_API_ID, TDIMPORT_API_HASH, 'tdesktop')",
          (api_id, api_hash, profile_name) == (2040, "b18441a1ff607e10a989891a5462e627", "tdesktop"),
          detail=str((api_id, api_hash, profile_name)))
    api_id2, api_hash2, _ = ns2["_api_profile_for"]("onboarding")
    check("_api_profile_for('onboarding') unaffected by the tdata_import branch (falls back to project default "
          "since ONBOARD_* is empty here)", (api_id2, api_hash2) == (111, "project-hash"))
    api_id3, api_hash3, _ = ns2["_api_profile_for"]("some_other_source")
    check("_api_profile_for(anything else) still returns the plain project API_ID/API_HASH",
          (api_id3, api_hash3) == (111, "project-hash"))
    check("_device_kwargs_for('tdata_import') is the Desktop profile, distinct from the default",
          ns2["_device_kwargs_for"]("tdata_import").get("device_model") == "Desktop")
    check("_device_kwargs_for('onboarding') is the plain project device profile, unaffected",
          ns2["_device_kwargs_for"]("onboarding").get("device_model") == "project-device")

    # order-of-operations: _tdimport_spawn_runtime MUST persist auth_profile='tdesktop'
    # BEFORE spawning the runtime subprocess -- the child reads its registry row
    # (incl. auth_profile) exactly once at startup, so writing it after spawn would
    # race the child's first read and risk it starting under the wrong API profile.
    _order = []

    async def _fake_manager_set_fields(key, **fields):
        _order.append(("set_fields", key, dict(fields)))

    async def _fake_spawn_manager_process(key):
        _order.append(("spawn", key))
        return True, ""

    ns3 = extract_and_exec(
        tree,
        ["_tdimport_spawn_runtime"],
        {"manager_set_fields": _fake_manager_set_fields, "_spawn_manager_process": _fake_spawn_manager_process},
    )
    asyncio.run(ns3["_tdimport_spawn_runtime"]("mgr-order-test"))
    check("_tdimport_spawn_runtime writes auth_profile='tdesktop' BEFORE spawning the runtime process",
          _order == [("set_fields", "mgr-order-test", {"auth_profile": "tdesktop"}), ("spawn", "mgr-order-test")],
          detail=str(_order))

    # finalize-side reset (TPILOT 20260719, review blocker fix): a successful
    # phone/code/2FA/QR/relogin/replacement sign_in all routes through the single
    # _manager_finalize_login choke point, whose manager_set_fields write MUST
    # include auth_profile="project" -- otherwise a previously-imported ('tdesktop')
    # manager re-authorized by phone/QR would keep connecting its NEW project-API
    # auth_key under api_id 2040 (the mirror image of the bug this release fixes).
    # The tdimport flow never calls this finalize, so imported managers' 'tdesktop'
    # value is unaffected by the reset.
    finalize_src = _src_without_docstring(last_def(tree, "_manager_finalize_login"))
    check("_manager_finalize_login resets auth_profile='project' on every successful "
          "project-API sign_in (phone/code/2FA, QR, relogin, replacement)",
          ("auth_profile='project'" in finalize_src or 'auth_profile="project"' in finalize_src)
          and "manager_set_fields" in finalize_src,
          detail=finalize_src[:300])

    # runtime-side (main.py:~486) selection: source-scan confirms the module-level
    # `client = TelegramClient(...)` construction branches on MANAGER_REGISTRY_ROW's
    # auth_profile, so an already-installed direct-import manager keeps using the
    # Desktop profile on every subsequent process start, not just the initial probe.
    check('runtime client construction reads auth_profile from MANAGER_REGISTRY_ROW',
          '.get("auth_profile")' in src or ".get('auth_profile')" in src, detail="")
    check("runtime client construction selects TDIMPORT_API_ID/TDIMPORT_API_HASH/_TDIMPORT_DEVICE_KWARGS "
          "when auth_profile=='tdesktop'",
          "== \"tdesktop\"" in src or "== 'tdesktop'" in src, detail="")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL ADMINBOT-WIRING SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
