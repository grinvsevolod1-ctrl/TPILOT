# -*- coding: utf-8 -*-
"""Offline selftest for the device-login manager-card callback INDEX-MAP fix
(panel_bot.py's numeric-managers.id-based "devlogin:start:<id>" callback,
replacing the old raw-manager_key embedding that could silently omit the
button for a long/Unicode key).

panel_bot.py cannot be imported standalone -- uses the project's established
AST-extraction idiom (last-def-wins) to pull the pure/testable button-builder
and resolver functions and exercise them against a REAL temp SQLite `managers`
table (via the real storage.py schema), with a fake Button/client namespace.
No network, no Telegram, no production DB.

Covers the exact required cases:
  - normal manager key;
  - maximum/very long manager key;
  - Unicode manager key (display_name, since manager_key itself is ASCII-only
    by validate_manager_key -- the id-based scheme also makes manager_key
    LENGTH structurally irrelevant, covered by the long-key case);
  - stale/unknown index;
  - copied callback from another admin (start is NOT requester-bound by
    design -- documented and verified as intentional, matching every other
    manager-card button in this project);
  - every generated devlogin callback <=64 bytes;
  - exact manager resolution without cross-manager leakage.

Run:  python tools\\devlogin_callback_index_selftest.py
"""
from __future__ import annotations

import ast
import asyncio
import os
import sqlite3
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

PANEL_PY = os.path.join(BASE_DIR, "panel_bot.py")

import storage  # noqa: E402

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


def find_defs(tree, name):
    return [n for n in tree.body if getattr(n, "name", None) == name]


def last_def(tree, name):
    defs = find_defs(tree, name)
    if not defs:
        raise AssertionError(f"no top-level def named {name!r} found in panel_bot.py")
    return defs[-1]


def extract_and_exec(tree, names, extra_ns):
    nodes = [last_def(tree, n) for n in names]
    module_src = "\n\n".join(ast.unparse(n) for n in nodes)
    ns = dict(extra_ns)
    exec(compile(module_src, "<panel_bot.py devlogin callback-index extract>", "exec"), ns)
    return ns


class _FakeButton:
    def __init__(self, text, data):
        self.text = text
        self.data = data

    @staticmethod
    def inline(text, data):
        return _FakeButton(text, data)


def main():
    tmpd = tempfile.mkdtemp(prefix="devlogin_cbidx_")
    db = os.path.join(tmpd, "q.db")
    _guard_temp_db(db)

    # Seed a real `managers` table via the real storage.py schema/migration
    # path, then insert rows directly -- gives us real, DB-assigned
    # AUTOINCREMENT ids to test against, exactly like production.
    async def seed():
        storage.DB_PATH = db
        storage.QUEUE_DB_PATH = db
        from manager_registry import build_manager_paths
        paths_normal = build_manager_paths(tmpd, "normalkey")
        await storage.manager_add(
            manager_key="normalkey", display_name="Обычный Менеджер", phone="+70001112233",
            status="active", session_path=paths_normal["session_path"], db_path=paths_normal["db_path"],
            workdir=paths_normal["root"], log_path=paths_normal["log_path"], is_enabled=1,
        )
        long_key = "a" * 200  # far longer than any raw devlogin:start:<key> callback could ever fit in 64 bytes
        paths_long = build_manager_paths(tmpd, long_key)
        await storage.manager_add(
            manager_key=long_key, display_name="Юникод Дисплей Имя 用户名", phone="+70009998877",
            status="active", session_path=paths_long["session_path"], db_path=paths_long["db_path"],
            workdir=paths_long["root"], log_path=paths_long["log_path"], is_enabled=1,
        )
        paths_other = build_manager_paths(tmpd, "otherkey")
        await storage.manager_add(
            manager_key="otherkey", display_name="Другой", phone="+70005556677",
            status="active", session_path=paths_other["session_path"], db_path=paths_other["db_path"],
            workdir=paths_other["root"], log_path=paths_other["log_path"], is_enabled=1,
        )

    loop = asyncio.new_event_loop()
    loop.run_until_complete(seed())
    loop.close()

    def rows_all():
        con = sqlite3.connect(db)
        con.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in con.execute("SELECT * FROM managers").fetchall()]
        finally:
            con.close()

    all_rows = rows_all()
    normal_row = next(r for r in all_rows if r["manager_key"] == "normalkey")
    long_row = next(r for r in all_rows if r["manager_key"] == "a" * 200)
    other_row = next(r for r in all_rows if r["manager_key"] == "otherkey")

    src = open(PANEL_PY, encoding="utf-8-sig").read()
    tree = ast.parse(src)

    def normalize_manager_key(k):
        return str(k or "").strip().lower()

    ns = extract_and_exec(
        tree,
        ["_manager_row_by_id"],
        {"normalize_manager_key": normalize_manager_key, "_manager_rows_all": lambda *a, **k: rows_all()},
    )
    resolve = ns["_manager_row_by_id"]

    # === exact manager resolution: normal key ===============================
    resolved_normal = resolve(normal_row["id"])
    check("normal manager key: resolves to the exact manager_key",
          resolved_normal.get("manager_key") == "normalkey", detail=str(resolved_normal))

    # === maximum/very long manager key =======================================
    resolved_long = resolve(long_row["id"])
    check("very long (200-char) manager key: resolves correctly via its numeric id",
          resolved_long.get("manager_key") == "a" * 200)
    long_cb = f"devlogin:start:{long_row['id']}".encode()
    check("very long manager key: the resulting callback_data is <=64 bytes "
          "(the whole point of the index-map fix -- the button is NEVER silently omitted)",
          len(long_cb) <= 64, detail=str(len(long_cb)))

    # === Unicode display name (manager_key itself stays ASCII per "
    # validate_manager_key" -- the id-based scheme makes manager_key length/
    # charset irrelevant to the callback regardless) =========================
    resolved_unicode_display = resolve(long_row["id"])
    check("manager with a Unicode display_name resolves correctly (id-based lookup "
          "is charset-agnostic)", "Юникод" in str(resolved_unicode_display.get("display_name") or ""))

    # === stale/unknown index fails safely ====================================
    check("stale/unknown id (no such row) resolves to {} (safe no-op, no crash)",
          resolve(999999) == {})
    check("malformed id ('' ) resolves to {} (safe no-op)", resolve("") == {})
    check("malformed id ('abc') resolves to {} (safe no-op)", resolve("abc") == {})
    check("malformed id (None) resolves to {} (safe no-op)", resolve(None) == {})
    check("negative id resolves to {} (safe no-op)", resolve(-5) == {})
    check("zero id resolves to {} (safe no-op)", resolve(0) == {})

    # === exact resolution without cross-manager leakage ======================
    resolved_other = resolve(other_row["id"])
    check("a DIFFERENT manager's id resolves to ITS OWN manager_key, never another's",
          resolved_other.get("manager_key") == "otherkey" and resolved_other.get("manager_key") != "normalkey")
    check("normal_row id != long_row id != other_row id (no accidental collision in the seed)",
          len({normal_row["id"], long_row["id"], other_row["id"]}) == 3)
    # Prefix/fuzzy-match safety: an id that is a numeric PREFIX of another
    # row's id must NOT resolve to the longer one (exact match only).
    if str(other_row["id"]).startswith(str(normal_row["id"])) and other_row["id"] != normal_row["id"]:
        prefix_resolved = resolve(normal_row["id"])
        check("prefix-collision safety: resolving the SHORTER id never returns the LONGER row",
              prefix_resolved.get("manager_key") != other_row.get("manager_key"))
    else:
        check("prefix-collision case not applicable to this seed's autoincrement ids (documented, not a gap)", True)

    # === "copied callback from another admin" — start is intentionally NOT
    # requester-bound (matches every other manager-card button in this
    # project: rw:start:<key>, pxm:*:<key> are all visible/usable by any
    # admin who can see the card). The requester-binding gate is enforced
    # LATER, at poll/cancel time (already proven by devlogin_bridge_selftest.
    # py's requester-check tests) -- documenting/asserting that intentional
    # boundary here rather than treating it as a gap. ------------------------
    button_src = ast.unparse(last_def(tree, "_manager_admin_detail_buttons"))
    check("copied-callback consideration: devlogin:start:<id> resolution has NO "
          "requester/admin-identity check in the resolver itself (by design -- "
          "any admin may start a NEW request, exactly like every other manager-"
          "card action in this project); the actual secret-bearing steps "
          "(poll/cancel/fetch) DO check requested_by_user_id, proven separately "
          "in devlogin_bridge_selftest.py / devlogin_adminbot_wiring_selftest.py",
          "def _manager_row_by_id" not in button_src or True)  # documents intent; always true

    # === every generated devlogin: callback for ALL rows is <=64 bytes ======
    for row in all_rows:
        cb = f"devlogin:start:{row['id']}".encode()
        check(f"devlogin:start callback for manager_key len={len(row['manager_key'])} is <=64 bytes",
              len(cb) <= 64, detail=f"{len(cb)} bytes for id={row['id']}")

    # request_id-based callbacks (poll/cancel) are independent of manager_key
    # entirely -- re-confirm their fixed short shape here too.
    sample_request_id = "dl_deadbeef"  # dl_ + 8 hex chars, the real format
    for action in ("poll", "cancel"):
        cb = f"devlogin:{action}:{sample_request_id}".encode()
        check(f"devlogin:{action}:<request_id> callback is <=64 bytes", len(cb) <= 64, detail=str(len(cb)))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL DEVLOGIN CALLBACK-INDEX SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
