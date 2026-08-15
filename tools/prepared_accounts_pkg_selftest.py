# -*- coding: utf-8 -*-
"""Phase 1 selftest for the features.prepared_accounts package skeleton
(model.py + texts.py + both __init__.py files).

Pure/offline: no network, no Telegram, no provider calls, no database --
this phase creates no SQL, no storage access, and no Telegram client at
all. Verifies:
  1. `import features.prepared_accounts` succeeds.
  2. That import does not pull telethon/main/panel_bot (and, if not already
     present, storage) into sys.modules -- the package's isolation
     contract (see prepared_accounts/__init__.py's docstring).
  3. model.py's own imports are stdlib-only.
  4. The exact UI labels fixed by the approved plan.
  5. requires_proxy_before_auth: True for qr/phone, False for session/tdata.
  6. derive_substate's DRAFT/VERIFIED/STORED matrix.
  7. Session/TData's STORED substate is never rendered with the "Telegram
     проверен" wording reserved for a real (qr/phone) verification.
  8. Source scan: none of the forbidden duplicated-implementation markers
     (qr_login, send_code_request, TelegramClient, sqlite3, allow_spend,
     manager_set_fields, ...) appear anywhere under features/.
  9. texts.py does not import telethon.
  10. PREPARED_READY_MARKER exists and is non-empty.
  11. No heavy network-capable module (telethon/aiohttp/requests/httpx/
      opentele) is newly imported as a side effect of importing the package.

Run: python3.12 tools\\prepared_accounts_pkg_selftest.py
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


FORBIDDEN_TOKENS = (
    "qr_login",
    "send_code_request",
    "sign_in",
    "SessionPasswordNeeded",
    "TelegramClient",
    "connect(",
    "get_me",
    "is_user_authorized",
    "opentele",
    "ProxySellerProvider",
    "allow_spend",
    "sqlite3",
    "aiosqlite",
    "proxy_lease_",
    "manager_add",
    "manager_set_fields",
    "_spawn_manager_process",
)

# TPILOT PREPARED ACCOUNTS PHASE 5 (2026-08-11): service.py is EXPLICITLY
# required (approved plan's "LIVE VERIFY SEQUENCE", sections 11-15) to call
# connect()/is_user_authorized()/get_me() on an INJECTED, already-
# authenticated Telegram client -- orchestrating a read-only identity
# re-check, not implementing a new auth flow. Constructing a client
# (TelegramClient(), qr_login, send_code_request, sign_in, catching
# SessionPasswordNeeded) remains fully forbidden in service.py too -- only
# these three specific method-call tokens are excepted, and ONLY for this
# one file. The zero-network contract that DOES forbid all three outright
# (approved plan section 4) is scoped to import_offline.py specifically,
# which stays fully covered by the blanket scan below.
_PER_FILE_TOKEN_EXCEPTIONS = {
    str(Path("features") / "prepared_accounts" / "service.py"): {"connect(", "get_me", "is_user_authorized"},
}

_STDLIB_ALLOWED_MODULES = {"__future__", "typing", "dataclasses", "enum"}

_HEAVY_NETWORK_MODULE_NAMES = {"telethon", "aiohttp", "requests", "httpx", "opentele"}


def _feature_package_files():
    root = BASE_DIR / "features"
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _code_without_string_literals(source: str) -> str:
    """AST-aware reconstruction of `source` with every string literal's
    VALUE blanked out (docstrings included -- a docstring is just a string
    Constant in statement position), while leaving all real code --
    imports, identifiers, attribute access, calls -- intact. This is what
    makes the forbidden-marker scan below "source-aware": a marker that
    only ever appears inside a comment (comments are not part of the AST at
    all, so they vanish from `ast.unparse` regardless) or inside a
    docstring/string literal (e.g. a field name like "proxy_lease_id" used
    as plain data, or a docstring that mentions "aiosqlite" while
    explaining an isolation contract) no longer matches, while a REAL
    duplicated-implementation marker -- an import, a function/class
    definition, a call, an attribute access -- still does, because none of
    those are string Constant nodes."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            node.value = ""
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def main() -> int:
    # --- 1/2/11: import isolation -------------------------------------------
    pre_modules = set(sys.modules.keys())
    storage_already_loaded = "storage" in pre_modules

    import features.prepared_accounts as prepared_accounts  # noqa: WPS433

    post_modules = set(sys.modules.keys())
    newly_imported = post_modules - pre_modules

    check("1. import features.prepared_accounts succeeds", prepared_accounts is not None)
    check("2a. telethon not in sys.modules after import", "telethon" not in post_modules)
    check("2b. main not in sys.modules after import", "main" not in post_modules)
    check("2c. panel_bot not in sys.modules after import", "panel_bot" not in post_modules)
    if storage_already_loaded:
        print("[SKIP] 2d. storage isolation -- storage was already imported by the test environment before this script ran")
    else:
        check("2d. storage not pulled in by feature import", "storage" not in post_modules)

    check(
        "11. no heavy network-capable module newly imported",
        not (newly_imported & _HEAVY_NETWORK_MODULE_NAMES),
        detail=str(newly_imported & _HEAVY_NETWORK_MODULE_NAMES),
    )

    # --- 3: model.py imports stdlib only -------------------------------------
    model_path = BASE_DIR / "features" / "prepared_accounts" / "model.py"
    model_src = model_path.read_text(encoding="utf-8-sig")
    model_tree = ast.parse(model_src, filename=str(model_path))
    model_import_names = []
    for node in ast.walk(model_tree):
        if isinstance(node, ast.Import):
            model_import_names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                model_import_names.append(node.module.split(".")[0])
    non_stdlib = [n for n in model_import_names if n not in _STDLIB_ALLOWED_MODULES]
    check("3. model.py imports stdlib only", not non_stdlib, detail=str(non_stdlib))

    # --- 4: exact labels -------------------------------------------------------
    texts = prepared_accounts.texts
    model = prepared_accounts.model
    check("4a. SECTION_LABEL exact", texts.SECTION_LABEL == "💾Подготовленные аккаунты", texts.SECTION_LABEL)
    check("4b. PREPARE_ACTION_LABEL exact", texts.PREPARE_ACTION_LABEL == "📦 Подготовить на потом", texts.PREPARE_ACTION_LABEL)
    check("4c. AUTH_QR_LABEL exact", texts.AUTH_QR_LABEL == "◻️ QR", texts.AUTH_QR_LABEL)
    check("4d. AUTH_PHONE_LABEL exact", texts.AUTH_PHONE_LABEL == "📱 Номер телефона", texts.AUTH_PHONE_LABEL)
    check("4e. AUTH_SESSION_LABEL exact", texts.AUTH_SESSION_LABEL == "📁 Session", texts.AUTH_SESSION_LABEL)
    check("4f. AUTH_TDATA_LABEL exact", texts.AUTH_TDATA_LABEL == "📦 TData", texts.AUTH_TDATA_LABEL)
    check("4g. BACK_LABEL exact", texts.BACK_LABEL == "⬅️ Назад", texts.BACK_LABEL)

    # --- 5: requires_proxy_before_auth -----------------------------------------
    check("5a. qr requires proxy before auth", model.requires_proxy_before_auth(model.AuthSource.QR) is True)
    check("5b. phone requires proxy before auth", model.requires_proxy_before_auth(model.AuthSource.PHONE) is True)
    check("5c. session does NOT require proxy before auth", model.requires_proxy_before_auth(model.AuthSource.SESSION) is False)
    check("5d. tdata does NOT require proxy before auth", model.requires_proxy_before_auth(model.AuthSource.TDATA) is False)

    # --- 6: derive_substate matrix ----------------------------------------------
    row_no_tg = {"status": model.PREPARED_STATUS, "tg_user_id": None}
    row_with_tg = {"status": model.PREPARED_STATUS, "tg_user_id": 987654321}
    row_not_prepared = {"status": "active", "tg_user_id": 987654321}
    meta_qr = {"auth_source": model.AuthSource.QR}
    meta_phone = {"auth_source": model.AuthSource.PHONE}
    meta_session = {"auth_source": model.AuthSource.SESSION}
    meta_tdata = {"auth_source": model.AuthSource.TDATA}
    meta_unset = {}

    check("6a. qr + no tg_user_id -> DRAFT", model.derive_substate(row_no_tg, meta_qr) == model.PreparedSubstate.DRAFT)
    check("6b. qr + tg_user_id -> VERIFIED", model.derive_substate(row_with_tg, meta_qr) == model.PreparedSubstate.VERIFIED)
    check("6c. phone + no tg_user_id -> DRAFT", model.derive_substate(row_no_tg, meta_phone) == model.PreparedSubstate.DRAFT)
    check("6d. phone + tg_user_id -> VERIFIED", model.derive_substate(row_with_tg, meta_phone) == model.PreparedSubstate.VERIFIED)
    check("6e. session + no tg_user_id -> STORED", model.derive_substate(row_no_tg, meta_session) == model.PreparedSubstate.STORED)
    check("6f. tdata + no tg_user_id -> STORED", model.derive_substate(row_no_tg, meta_tdata) == model.PreparedSubstate.STORED)
    check("6g. non-prepared row -> None", model.derive_substate(row_not_prepared, meta_qr) is None)
    check("6h. prepared row, unset auth_source -> None", model.derive_substate(row_no_tg, meta_unset) is None)
    check("6i. None row -> None", model.derive_substate(None, meta_qr) is None)

    check("6j. can_activate False for DRAFT", model.can_activate(row_no_tg, meta_qr) is False)
    check("6k. can_activate True for VERIFIED", model.can_activate(row_with_tg, meta_qr) is True)
    check("6l. can_activate True for STORED", model.can_activate(row_no_tg, meta_session) is True)
    check("6m. can_activate False for non-prepared", model.can_activate(row_not_prepared, meta_qr) is False)

    # --- 7: STORED must never read as Telegram-verified -------------------------
    stored_label = texts.substate_label(model.PreparedSubstate.STORED)
    check(
        "7. STORED substate label differs from the VERIFIED label",
        stored_label != texts.SUBSTATE_VERIFIED_LABEL and stored_label == texts.SUBSTATE_STORED_LABEL,
        detail=stored_label,
    )
    check(
        "7b. DRAFT substate label also differs from VERIFIED",
        texts.substate_label(model.PreparedSubstate.DRAFT) != texts.SUBSTATE_VERIFIED_LABEL,
    )

    # --- 8: no duplicated-implementation markers anywhere under features/ ------
    # AST-aware: string literals (docstrings included) are blanked before the
    # scan, so a docstring explaining "storage's aiosqlite-adjacent import
    # surface" or a plain data field named "proxy_lease_id" cannot trigger a
    # false positive -- only real code (imports, calls, attribute/identifier
    # references) can.
    offenders = []
    for path in _feature_package_files():
        src = path.read_text(encoding="utf-8-sig")
        cleaned = _code_without_string_literals(src)
        rel = str(path.relative_to(BASE_DIR))
        exceptions = _PER_FILE_TOKEN_EXCEPTIONS.get(rel, set())
        hits = [tok for tok in FORBIDDEN_TOKENS if tok in cleaned and tok not in exceptions]
        if hits:
            offenders.append((rel, hits))
    check("8. no forbidden duplicated-implementation markers under features/ (per-file exceptions: service.py's injected-client connect/get_me/is_user_authorized calls -- see _PER_FILE_TOKEN_EXCEPTIONS)", not offenders, detail=str(offenders))

    # --- 9: texts.py does not import telethon -----------------------------------
    texts_path = BASE_DIR / "features" / "prepared_accounts" / "texts.py"
    texts_tree = ast.parse(texts_path.read_text(encoding="utf-8-sig"), filename=str(texts_path))
    texts_import_names = []
    for node in ast.walk(texts_tree):
        if isinstance(node, ast.Import):
            texts_import_names.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                texts_import_names.append(node.module.split(".")[0])
    check("9. texts.py does not import telethon", "telethon" not in texts_import_names, detail=str(texts_import_names))

    # --- 10: PREPARED_READY_MARKER ----------------------------------------------
    check(
        "10. PREPARED_READY_MARKER exists and is non-empty",
        bool(str(model.PREPARED_READY_MARKER or "").strip()),
        detail=repr(getattr(model, "PREPARED_READY_MARKER", None)),
    )

    print()
    if FAILURES:
        print(f"SELFTEST FAILED: {len(FAILURES)} check(s) failed: {FAILURES}")
        return 1
    print("SELFTEST OK: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
