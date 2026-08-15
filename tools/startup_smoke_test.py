# -*- coding: utf-8 -*-
"""tools/startup_smoke_test.py -- safe TRUE import-order smoke test for
panel_bot.py. TPILOT FIX-6 20260718b.

Problem this replaces: an earlier ad-hoc smoke script (kept only in a
session scratchpad, never in tools/) imported panel_bot WITHOUT overriding
DB_PATH, so panel_bot's own module-level schema-ensure calls
(ensure_bizlink_tables/ensure_transfer_tables/ensure_manager_schedule_tables,
called via TPILOT_DB_PATH at panel_bot.py import time) ran against
whatever DB_PATH the real environment/.env.TPilot resolved to -- in
practice the real production db/data_tpilot.db.

This version:
  * redirects every project path env var (DB_PATH, PANEL_SESSION_FILE,
    WATCHDOG_STATUS_FILE, API_ID/API_HASH/tokens) to a fresh temp directory
    BEFORE panel_bot is ever imported;
  * fakes the `telethon` module (no real client construction, no network,
    no bot start/poll -- matches every other selftest's convention) and the
    `dotenv` module (so panel_bot's own `load_dotenv(ENV_PATH,
    override=True)` never reads the real .env.TPilot and can never
    reintroduce a real path);
  * PROVES protection, not just avoids the obvious case: takes a full
    file-level inventory (relative path -> size + mtime_ns) of
    C:\\ALM_TPilot\\db, runtime, sessions, logs BEFORE and AFTER the import,
    and fails loudly on any added/removed/changed file under those
    directories -- a WAL/SHM/journal/sidecar file left behind would show up
    as an "added" file and fail the test.

Never: real Telegram network, real proxy/provider network, real process
spawn, production DB/runtime/session/log access or mutation.

    python tools\\startup_smoke_test.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import types
from pathlib import Path

FAILURES: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK]   {label}")
    else:
        print(f"[FAIL] {label}  {detail}")
        FAILURES.append(label)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROTECTED_DIRS = ("db", "runtime", "sessions", "logs")


def _inventory(root: Path, subdirs) -> dict:
    """{relative_path: (size, mtime_ns)} for every file under each protected
    subdir -- a strict before/after equality proof, not just a directory
    mtime check (which a transient sqlite journal create+delete can leave
    unchanged even though it touched the directory)."""
    snap: dict = {}
    for sub in subdirs:
        d = root / sub
        if not d.exists():
            continue
        for p in d.rglob("*"):
            if p.is_file():
                try:
                    st = p.stat()
                    snap[str(p.relative_to(root))] = (st.st_size, st.st_mtime_ns)
                except Exception:
                    snap[str(p.relative_to(root))] = ("stat_error", "stat_error")
    return snap


def main() -> int:
    before = _inventory(PROJECT_ROOT, PROTECTED_DIRS)

    tmp_root = Path(tempfile.mkdtemp(prefix="startup_smoke_"))
    try:
        tmp_db = tmp_root / "data_tpilot.db"
        tmp_runtime = tmp_root / "runtime"
        tmp_sessions = tmp_root / "sessions"
        tmp_logs = tmp_root / "logs"
        for d in (tmp_runtime, tmp_sessions, tmp_logs):
            d.mkdir(parents=True, exist_ok=True)

        # --- redirect every project path env var BEFORE import ---
        os.environ["DB_PATH"] = str(tmp_db)
        os.environ["PANEL_SESSION_FILE"] = str(tmp_sessions / "session_panel_bot")
        os.environ["WATCHDOG_STATUS_FILE"] = str(tmp_runtime / "soft_status.json")
        os.environ["API_ID"] = "12345"
        os.environ["API_HASH"] = "deadbeef"
        os.environ["PANEL_BOT_TOKEN"] = ""
        os.environ["PANEL_ALLOWED_USER_IDS"] = ""
        os.environ["PANEL_ALLOWED_CHAT_IDS"] = ""
        os.environ["MANAGER_ADMIN_PASSWORD"] = ""
        os.environ["PANEL_ADMIN_PASSWORD"] = ""

        # --- fake telethon: no real client construction, no network ---
        fake_telethon = types.ModuleType("telethon")

        class _FakeButton:
            @staticmethod
            def inline(text, data=b""):
                return (text, data)

        class _FakeEvents:
            class NewMessage:
                def __init__(self, *a, **kw):
                    pass

            class CallbackQuery:
                def __init__(self, *a, **kw):
                    pass

        class _FakeTelegramClient:
            def __init__(self, *a, **kw):
                self.registered = []

            def on(self, *a, **kw):
                def _decorator(fn):
                    self.registered.append(fn.__name__)
                    return fn
                return _decorator

        fake_telethon.Button = _FakeButton
        fake_telethon.TelegramClient = _FakeTelegramClient
        fake_telethon.events = _FakeEvents
        sys.modules["telethon"] = fake_telethon

        # --- fake dotenv: NEVER reads the real .env.TPilot -- panel_bot.py
        # calls load_dotenv(ENV_PATH, override=True), which would otherwise
        # overwrite the env vars we just set with whatever is in the real
        # secrets file (including the real production DB_PATH).
        fake_dotenv = types.ModuleType("dotenv")
        fake_dotenv.load_dotenv = lambda *a, **kw: None
        fake_dotenv.dotenv_values = lambda *a, **kw: {}
        sys.modules["dotenv"] = fake_dotenv

        sys.modules.pop("panel_bot", None)
        sys.path.insert(0, str(PROJECT_ROOT))
        t0 = time.time()
        import panel_bot  # noqa: E402
        t1 = time.time()

        check("module import completed", True, "")
        check(
            "import resolved TPILOT_DB_PATH to the temp path (never production)",
            str(panel_bot.TPILOT_DB_PATH) == str(tmp_db), panel_bot.TPILOT_DB_PATH,
        )
        check(
            "client is the fake TelegramClient (no real Telethon client constructed)",
            type(panel_bot.client).__name__ == "_FakeTelegramClient", type(panel_bot.client).__name__,
        )
        check(
            "at least one handler registered (module actually initialized)",
            len(panel_bot.client.registered) > 0, len(panel_bot.client.registered),
        )
        check("import completed quickly (<5s, no real network/DB stall)", (t1 - t0) < 5.0, f"{t1 - t0:.3f}s")
        check(
            "temp DB file was created by module-level schema-ensure calls (proves they ran against the temp path, not silently no-oped)",
            tmp_db.exists(), str(tmp_db),
        )
    finally:
        sys.modules.pop("panel_bot", None)
        shutil.rmtree(str(tmp_root), ignore_errors=True)

    after = _inventory(PROJECT_ROOT, PROTECTED_DIRS)
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in (set(after) & set(before)) if after[k] != before[k])
    check("no new files created under db/runtime/sessions/logs", not added, added)
    check("no files removed under db/runtime/sessions/logs", not removed, removed)
    check("no existing file under db/runtime/sessions/logs changed size/mtime", not changed, changed)

    print()
    if FAILURES:
        print(f"SMOKE TEST FAIL: {len(FAILURES)} check(s) failed:")
        for f in FAILURES:
            print(f"  - {f}")
        return 1
    print("SMOKE TEST OK: all checks passed, protected directories proven untouched.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
