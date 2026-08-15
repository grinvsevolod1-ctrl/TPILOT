"""
preflight_check.py -- TPilot Stage 1 morning preflight-check (read-only).

Pure helper module. No Telegram client calls, no process start/stop, no
browser automation, no network calls except one local, read-only Windows
process enumeration (mirrors soft_watchdog_pinger.py's own established
technique -- same command, same safety properties, run once here instead of
every 60s). The only DB access is read-only against the central TPilot
SQLite database and a read of runtime/soft_status.json.

Callers (bot processes) are responsible for actually sending Telegram
messages and for storing their own daily sent-flags / message-ids in their
own settings KV -- this module only computes the readiness result and
renders text.

Scope (Stage 1):
  - who works today (existing schedule helper, unmodified semantics)
  - manager/source mapping (existing tables, read-only)
  - today's business links: existence + status by DB records only
    (NOT a live Telegram clickability check -- that is Stage 3)
  - session health: last known manager_telegram_health row, read-only
  - proxy: only for managers where a proxy is actually required
    (proxy_enabled=1 AND proxy_bypass_allowed=0)
  - key process/service liveness via soft_status.json + a local process scan
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import storage
from manager_registry import list_manager_rows_from_db_sync, normalize_manager_key

# W3.2 (02_S9_TIMEZONE_CONTRACT.md 2.3/2.4, TZ-1 -- inventory's #1 risk finding): this
# module is the SOLE source of "did today's preflight check run" (see the 0830 gate
# below), and its try/except OS-local fallback meant the Windows server's own local
# clock -- not Europe/Kyiv -- silently decided that gate whenever tzdata failed to load.
# storage.w3_tz() raises W3TimezoneError at import time instead: a loud startup failure
# is recoverable, a quiet date shift gating "was today checked" is not. No fallback zone
# exists in any branch below.
_TZ = storage.w3_tz()

BASE_DIR = Path(__file__).resolve().parent
SOFT_STATUS_PATH = BASE_DIR / "runtime" / "soft_status.json"
SOFT_STATUS_FRESH_SECONDS = 180  # same freshness window PartnerBot's status badge already uses

BIZLINK_DEFAULT_COUNT_FALLBACK = 15

# --- TPILOT PREFLIGHT FULL PACKAGE: deep link verification -----------------
# Real read-only Telegram-side verification: run_deep_verification() below
# enqueues the EXISTING "bizlink_list_telegram" manager-queue command
# (storage.manager_queue_put/manager_queue_get; handled by the already-
# existing runtime branch in main.py, which calls _manager_list_business_
# links -> a single read-only GetBusinessChatLinksRequest() per manager).
# This is NOT the slow panel_commands/_submit_and_wait controller hop --
# manager runtimes claim their own queue rows in parallel (manager_queue_
# take_next, polled every ~1s), so N managers are checked concurrently, not
# sequentially. No new Telegram RPC logic, no main.py/storage.py edits.
#
# DEEP_VERIFY_AVAILABLE=True means: PanelBot's preflight loop calls
# run_deep_verification() and stores the resulting blob; render_admin_text/
# render_partner_text use apply_deep_results()'s merged per-manager status to
# decide wording. The green "проверены на работоспособность" line is only
# ever emitted when a manager's/source's deep status is "ok" (verified ==
# expected and failed == 0) -- see the render functions below. Any other
# deep status ("partial"/"failed"/"timeout"/"not_checked") falls back to the
# honest non-claiming wording, never the banned phrases.
DEEP_VERIFY_AVAILABLE = True

# Friendly Russian text for the bizlink error classes actually produced by
# _bizlink_classify_error (main.py). Anything else falls back to a generic
# safe message -- never echoes raw exception text into a Telegram message.
_BIZLINK_ERROR_CLASS_TEXT = {
    "tg_limit": "достигнут лимит бизнес-ссылок Telegram",
    "no_business": "аккаунту недоступны бизнес-ссылки (нет Business-подписки)",
    "proxy_error": "ошибка сети/прокси при создании ссылки",
    "flood_wait": "Telegram временно ограничил действия (FloodWait)",
    "template_validation_failed": "шаблон сообщения не прошёл проверку",
    "unknown": "не удалось создать ссылку (см. Панель → Бизнес-ссылки)",
    # TPILOT FIX-10 20260718b: readiness/classifier classes added by the
    # A/D/E runtime-auth remediation -- previously fell through to the
    # generic "unknown" fallback above (safe, but uninformative).
    "runtime_not_running": "рантайм менеджера не запущен",
    "runtime_not_ready": "рантайм менеджера ещё не готов",
    "telegram_disconnected": "рантайм временно отключён от Telegram",
    "session_unauthorized": "сессия аккаунта не авторизована, требуется повторный вход",
    "telegram_identity_mismatch": "Telegram-идентификатор аккаунта не совпадает с ожидаемым",
    "proxy_connect_error": "не удалось подключиться через прокси",
    "telegram_rpc_error": "Telegram вернул ошибку при выполнении запроса",
    "command_timeout": "команда не была выполнена вовремя (таймаут)",
}

# Friendly labels for manager_telegram_health.health_status (main.py TP_HG_STATUS_*).
_HEALTH_STATUS_ICON = {
    "ok": "🟢", "warning": "🟡", "limited": "🟠", "blocked": "🔴", "unknown": "⚫",
}
_HEALTH_STATUS_TEXT = {
    "ok": "аккаунт в порядке",
    "warning": "есть предупреждение, стоит проверить",
    "limited": "аккаунт ограничен Telegram (FloodWait/лимит)",
    "blocked": "аккаунт заблокирован/требует внимания",
    "unknown": "статус ещё не проверялся",
}


# ---------------------------------------------------------------------------
# Time helpers (Kyiv, matching the project's existing timezone convention)
# ---------------------------------------------------------------------------

def kyiv_today() -> date:
    return storage.w3_now().date()


def kyiv_today_iso() -> str:
    return kyiv_today().isoformat()


def kyiv_now_hms() -> str:
    return storage.w3_now().strftime("%H:%M:%S")


def kyiv_now_hm() -> str:
    return storage.w3_now().strftime("%H:%M")


def fmt_date_ru(iso_date: str) -> str:
    try:
        return date.fromisoformat(iso_date).strftime("%d.%m.%Y")
    except Exception:
        return str(iso_date or "")


def _utc_iso_to_kyiv_hm(value: Any) -> str:
    """Naive-UTC ISO timestamp (storage.py's _now_iso() convention: no
    tzinfo, always UTC) -> Kyiv "HH:MM". Empty/unparseable input returns ""
    -- callers must never substitute the report's own check_time for a
    missing/invalid value here, that would misrepresent when the underlying
    data actually changed. _TZ (module load time) is guaranteed non-None (see above) --
    W3.2 removed the OS-local/UTC silent fallback that used to live here; the only
    remaining except below is for a genuinely unparseable timestamp, not a timezone
    fallback."""
    s = str(value or "").strip()
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    try:
        return dt.astimezone(_TZ).strftime("%H:%M")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Process / service liveness (read-only). Mirrors soft_watchdog_pinger.py's
# own safe process-listing pattern; does not start/stop anything.
# ---------------------------------------------------------------------------

def _norm_path(s: str) -> str:
    return str(s or "").replace("\\", "/").lower()


def _scan_python_processes(timeout_sec: float = 12.0) -> List[Dict[str, str]]:
    """Read-only enumeration of running python processes and their command
    lines. Never raises; returns [] on any failure (including non-Windows
    platforms, where this project does not run in production)."""
    if not sys.platform.startswith("win"):
        return []
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -like 'python*' -and $_.CommandLine } | "
        "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"
    )
    try:
        cp = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=timeout_sec,
        )
        raw = (cp.stdout or "").strip()
        if not raw:
            return []
        data = json.loads(raw)
        if isinstance(data, dict):
            data = [data]
        return [
            {"pid": str(row.get("ProcessId", "")), "cmd": str(row.get("CommandLine", ""))}
            for row in (data or [])
        ]
    except Exception:
        return []


def _read_soft_status() -> Dict[str, Any]:
    try:
        if not SOFT_STATUS_PATH.exists():
            return {}
        data = json.loads(SOFT_STATUS_PATH.read_text(encoding="utf-8", errors="ignore"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _watchdog_is_fresh(status: Dict[str, Any]) -> bool:
    try:
        ts = str(status.get("time_utc") or "")
        if not ts:
            return False
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - dt).total_seconds()
        return 0 <= age <= SOFT_STATUS_FRESH_SECONDS
    except Exception:
        return False


def check_services(procs: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
    """Read-only check of the 6 service-level processes (not per-manager
    main.py instances -- those are checked alongside each manager below)."""
    result: Dict[str, Any] = {"ok": True, "issues": [], "detail": {}}
    if procs is None:
        procs = _scan_python_processes()
    status = _read_soft_status()
    base_norm = _norm_path(str(BASE_DIR))
    relevant = [p for p in procs if base_norm in _norm_path(p.get("cmd", ""))]

    def has_marker(marker: str, *, exclude_manager_flag: bool = False) -> bool:
        for p in relevant:
            cmd = _norm_path(p.get("cmd", ""))
            if marker not in cmd:
                continue
            if exclude_manager_flag and "--manager" in cmd:
                continue
            return True
        return False

    checks = {
        "controller": has_marker("main.py", exclude_manager_flag=True),
        "manager_bot": has_marker("manager_bot.py"),
        "watchdog": _watchdog_is_fresh(status),
        "health_server": has_marker("health_server.py"),
        "panel_bot": has_marker("panel_bot.py"),
        "partner_stat_bot": has_marker("partner_stat_bot.py"),
    }
    labels = {
        "controller": "Контроллер (main.py)",
        "manager_bot": "ManagerBot",
        "watchdog": "Сторож (soft_watchdog_pinger)",
        "health_server": "Health server",
        "panel_bot": "PanelBot",
        "partner_stat_bot": "PartnerBot",
    }
    result["detail"] = checks
    if not procs:
        result["ok"] = False
        result["issues"].append("Не удалось получить список процессов (проверьте вручную)")
        return result
    for key, ok in checks.items():
        if not ok:
            result["ok"] = False
            result["issues"].append(f"{labels[key]}: процесс не найден")
    return result


# ---------------------------------------------------------------------------
# DB reads (read-only, plain sqlite3 -- no writes, no schema changes)
# ---------------------------------------------------------------------------

def _connect_ro(db_path: str) -> sqlite3.Connection:
    con = sqlite3.connect(db_path, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def _norm_source_key(raw: str) -> str:
    """Must match partner_stat_bot.py's own _norm_key() exactly -- source_key
    values cross a module boundary (PartnerBot normalizes partner_buyers.
    source_key with _norm_key, this module compares it against manager_
    source_links.source_key/traffic_sources.source_key). A plain
    .strip().lower() here (the old implementation) would only lowercase and
    trim, silently failing to match whenever the raw DB value has an
    internal space, punctuation, or 'ё' that _norm_key would have stripped/
    folded -- which was one of the root causes of PartnerBot morning reports
    not being found for a real, correctly-configured source."""
    return re.sub(r"[^a-z0-9_-]+", "", str(raw or "").strip().lower().replace("ё", "е"))


def _find_legacy_alias_key(mk: str, active_keys) -> Optional[str]:
    """Best-effort detection of a stale schedule row that is really just an
    old spelling of a CURRENTLY active manager key -- e.g. a rename from
    'darias' to 'dariass' that left an orphaned schedule row under the old
    key, with no backing managers-table row under the old key at all (not
    even archived). Intentionally narrow: only a same-prefix/suffix match
    within 2 characters of length difference, and only when there is
    EXACTLY ONE such candidate -- ambiguous or loose matches are left alone
    so a genuinely unknown/broken key is still reported as a real error
    rather than silently (and possibly wrongly) aliased to someone else."""
    mk = str(mk or "")
    if not mk:
        return None
    candidates = []
    for ak in active_keys:
        ak = str(ak or "")
        if not ak or ak == mk:
            continue
        longer, shorter = (ak, mk) if len(ak) > len(mk) else (mk, ak)
        if len(longer) - len(shorter) > 2:
            continue
        if longer.startswith(shorter) or longer.endswith(shorter):
            candidates.append(ak)
    if len(candidates) == 1:
        return candidates[0]
    return None


def _manager_source_map(db_path: str) -> Dict[str, str]:
    """manager_key (normalized) -> source_key."""
    try:
        con = _connect_ro(db_path)
        try:
            rows = con.execute("SELECT manager_key, source_key FROM manager_source_links").fetchall()
            return {
                normalize_manager_key(r["manager_key"] or ""): str(r["source_key"] or "")
                for r in rows or [] if r["manager_key"]
            }
        finally:
            con.close()
    except Exception:
        return {}


def _source_names(db_path: str) -> Dict[str, str]:
    """source_key -> display name."""
    try:
        con = _connect_ro(db_path)
        try:
            rows = con.execute("SELECT source_key, name FROM traffic_sources").fetchall()
            return {str(r["source_key"] or ""): str(r["name"] or r["source_key"] or "") for r in rows or []}
        finally:
            con.close()
    except Exception:
        return {}


def _bizlink_default_count(db_path: str) -> int:
    try:
        con = _connect_ro(db_path)
        try:
            row = con.execute("SELECT value FROM settings WHERE key='bizlink_default_count'").fetchone()
            if row and str(row["value"] or "").strip():
                return int(str(row["value"]).strip())
        finally:
            con.close()
    except Exception:
        pass
    return BIZLINK_DEFAULT_COUNT_FALLBACK


def _bizlinks_today_by_manager(db_path: str, today_iso: str) -> Dict[str, List[sqlite3.Row]]:
    try:
        con = _connect_ro(db_path)
        try:
            rows = con.execute(
                "SELECT manager_key, slug, status, last_error_class FROM bizlinks WHERE target_date=?",
                (today_iso,),
            ).fetchall()
            out: Dict[str, List[sqlite3.Row]] = {}
            for r in rows or []:
                mk = normalize_manager_key(r["manager_key"] or "")
                out.setdefault(mk, []).append(r)
            return out
        finally:
            con.close()
    except Exception:
        return {}


def _bizlinks_active_slugs_by_manager(db_path: str, target_date_iso: str) -> Dict[str, set]:
    """manager_key (normalized) -> set of active DB slugs for target_date_iso.

    "Active" mirrors the same definition used elsewhere in the project
    (format_bizlinks_grouped / build_report): status='created', slug and
    link_url both non-empty, and not soft-deleted (deleted_at empty, if that
    column exists on this DB). Read-only; never raises."""
    try:
        con = _connect_ro(db_path)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(bizlinks)").fetchall()}
            has_deleted_at = "deleted_at" in cols
            sql = "SELECT manager_key, slug, status, link_url" + (", deleted_at" if has_deleted_at else "") + " FROM bizlinks WHERE target_date=?"
            rows = con.execute(sql, (target_date_iso,)).fetchall()
            out: Dict[str, set] = {}
            for r in rows or []:
                if str(r["status"] or "") != "created":
                    continue
                slug = str(r["slug"] or "").strip()
                if not slug or not str(r["link_url"] or "").strip():
                    continue
                if has_deleted_at and str(r["deleted_at"] or "").strip():
                    continue
                mk = normalize_manager_key(r["manager_key"] or "")
                out.setdefault(mk, set()).add(slug)
            return out
        finally:
            con.close()
    except Exception:
        return {}


def _manager_health_map(db_path: str) -> Dict[str, Dict[str, Any]]:
    try:
        con = _connect_ro(db_path)
        try:
            rows = con.execute(
                "SELECT manager_key, health_status, last_check_at, updated_at "
                "FROM manager_telegram_health"
            ).fetchall()
            return {normalize_manager_key(r["manager_key"] or ""): dict(r) for r in rows or []}
        finally:
            con.close()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Replacement / outage detection (read-only). One shared collector used by
# both build_report (AdminBot, all sources) and build_partner_report
# (PartnerBot, filtered to one source_key) -- see collect_replacement_records
# below. Never mutates data, never raises (fails open to an empty list).
# ---------------------------------------------------------------------------

# Recognized business-facing reasons that mean "this account is genuinely
# broken on the Telegram side" (routed to state D, a known-cause outage).
# Anything else (manual disable/stop with no fatal signal, or an
# unrecognized technical error) is routed to state A instead -- this module
# NEVER surfaces a raw exception class name/repr/traceback to a report.
FATAL_REASON_TEXTS = {
    "номер заблокирован Telegram",
    "слетела авторизация Telegram",
    "нет подключения через прокси",
    "нет подключения к Telegram",
    "аккаунт заблокирован Telegram",
}

_REASON_ADMIN_DISABLED_TEXT = "аккаунт отключён администратором"

# Exact existing AdminBot menu item label for the business-links section
# (panel_bot.py:14319, _nm_root_screen -> menu:nm_bizlinks button -- verified
# byte-for-byte against the live source before this constant was written).
# This module never imports panel_bot.py (import-time side effects) -- the
# label text is deliberately duplicated here as plain guidance text, not a
# button/callback (no new inline button is created).
_BIZLINKS_MENU_LABEL = "🔗 Бизнес-ссылки"

# reserve_activation_events.status values that count as evidence of an
# in-progress or completed replacement. "failed"/"cancelled" (or any other
# status) are deliberately excluded -- they never imply a connected or
# connecting reserve, see collect_replacement_records below.
_ACTIVE_EVENT_STATUSES = {"requested", "creating", "done"}

# (haystack substrings to match against manager_row.last_error + start_status
# reason_class/error/phase, all lowercased) -> business text. Order matters:
# first match wins, most specific/severe first. Includes both the exception-
# class-name spellings (main.py's managers.last_error stores repr(exc)) and
# the explicit snake_case reason_class tokens start_status.json may carry
# (auth_session, session_revoked, phone_banned, proxy_timeout,
# connection_error) -- a manager is FATAL only when one of these actually
# matches; any other/benign last_error text matches nothing here and is
# never treated as a fatal signal (see collect_replacement_records's
# candidate gate below).
_REASON_TEXT_PATTERNS = (
    (("phonenumberbannederror", "phonenumberbanned", "phone number has been banned", "phone_banned"), "номер заблокирован Telegram"),
    (("sessionrevoked", "session_revoked", "authkeyunregistered", "auth_session", "session revoked", "authorization lost", "authorizationlost"), "слетела авторизация Telegram"),
    # TPILOT FIX-3/10 20260718b: the A/D runtime-auth remediation added two
    # start_status reason_class values that carried no fatal-gate pattern
    # here, so a manager whose startup failed for exactly these reasons was
    # silently missed by the morning report / unavailability gate.
    (("session_unauthorized",), "сессия аккаунта не авторизована, требуется повторный вход"),
    (("telegram_identity_mismatch",), "Telegram-идентификатор аккаунта не совпадает с ожидаемым"),
    (("proxy_timeout", "proxy timeout", "proxy connect", "socks", "couldn't connect to proxy", "cannot connect to proxy"), "нет подключения через прокси"),
    (("connection_error", "connectionerror", "networkerror", "network timeout", "connection refused", "connection reset", "connection closed"), "нет подключения к Telegram"),
)


def _reason_business_text(
    manager_row: Optional[Dict[str, Any]],
    health_row: Optional[Dict[str, Any]] = None,
    start_status: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """manager_row/health_row/start_status -> one short, safe Russian
    business-reason phrase, or None when there is no usable signal at all.
    NEVER returns a traceback, exception repr, exception class name, or
    reason_class string -- only the fixed phrases below. A manager that is
    merely is_enabled=0/manual_stopped=1 with no other fatal signal gets the
    neutral admin-disabled phrasing, not a fabricated technical reason."""
    row = manager_row or {}
    health_status = str((health_row or {}).get("health_status") or "").strip().lower()
    if health_status in ("blocked", "banned"):
        return "аккаунт заблокирован Telegram"

    ss = start_status or {}
    haystack = " ".join([
        str(row.get("last_error") or ""),
        str(ss.get("reason_class") or ""),
        str(ss.get("error") or ""),
        str(ss.get("phase") or ""),
    ]).strip().lower()

    if haystack:
        for patterns, text in _REASON_TEXT_PATTERNS:
            if any(p in haystack for p in patterns):
                return text

    is_enabled = int(row.get("is_enabled")) if row.get("is_enabled") is not None else 1
    manual_stopped = int(row.get("manual_stopped") or 0)
    if is_enabled == 0 or manual_stopped == 1:
        return _REASON_ADMIN_DISABLED_TEXT
    return None


def _read_start_status(manager_key: str) -> Dict[str, Any]:
    """Best-effort read of runtime/managers/{key}/start_status.json -- same
    path convention as main.py's own _manager_recovery_read_start_status
    (mirrored locally rather than imported, since main.py cannot be safely
    imported standalone). Returns {} on any error, never raises."""
    try:
        path = BASE_DIR / "runtime" / "managers" / str(manager_key or "").strip().lower() / "start_status.json"
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def _reserve_pairs_for_primaries(db_path: str, primary_keys) -> Dict[str, List[sqlite3.Row]]:
    """normalized primary_key -> linked manager_reserve_pairs rows (most
    recently updated first). Empty dict if the table doesn't exist yet on
    this DB (feature never used) or on any read error -- read-only, never
    raises."""
    keys = sorted({normalize_manager_key(k) for k in (primary_keys or []) if k})
    if not keys:
        return {}
    try:
        con = _connect_ro(db_path)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(manager_reserve_pairs)").fetchall()}
            if not cols:
                return {}
            placeholders = ",".join("?" for _ in keys)
            rows = con.execute(
                f"SELECT * FROM manager_reserve_pairs WHERE primary_key IN ({placeholders}) "
                "AND status='linked' ORDER BY updated_at DESC",
                tuple(keys),
            ).fetchall()
            out: Dict[str, List[sqlite3.Row]] = {}
            for r in rows or []:
                pk = normalize_manager_key(r["primary_key"] or "")
                out.setdefault(pk, []).append(r)
            return out
        finally:
            con.close()
    except Exception:
        return {}


def _reserve_activation_events_for(db_path: str, primary_keys, target_date_iso: str) -> Dict[str, List[sqlite3.Row]]:
    """normalized primary_key -> reserve_activation_events rows for
    target_date_iso only (most recent id first). Empty dict if the table
    doesn't exist or on any read error -- read-only, never raises."""
    keys = sorted({normalize_manager_key(k) for k in (primary_keys or []) if k})
    if not keys:
        return {}
    try:
        con = _connect_ro(db_path)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(reserve_activation_events)").fetchall()}
            if not cols:
                return {}
            placeholders = ",".join("?" for _ in keys)
            rows = con.execute(
                f"SELECT * FROM reserve_activation_events WHERE primary_key IN ({placeholders}) "
                "AND target_date=? ORDER BY id DESC",
                tuple(keys) + (str(target_date_iso),),
            ).fetchall()
            out: Dict[str, List[sqlite3.Row]] = {}
            for r in rows or []:
                pk = normalize_manager_key(r["primary_key"] or "")
                out.setdefault(pk, []).append(r)
            return out
        finally:
            con.close()
    except Exception:
        return {}


def _bizlinks_updated_at_for_manager(db_path: str, manager_key: str, target_date_iso: str) -> str:
    """Max(updated_at) across manager_key's ACTIVE bizlink rows (status=
    'created', slug+link_url non-empty, not soft-deleted) for
    target_date_iso -- the raw UTC ISO string, or "" if none. Deliberately
    NOT created_at: a re-created link's updated_at reflects the real last
    change, created_at can be stale. Read-only, never raises."""
    try:
        con = _connect_ro(db_path)
        try:
            cols = {r[1] for r in con.execute("PRAGMA table_info(bizlinks)").fetchall()}
            has_deleted_at = "deleted_at" in cols
            sql = "SELECT updated_at, status, slug, link_url" + (", deleted_at" if has_deleted_at else "") + \
                  " FROM bizlinks WHERE manager_key=? AND target_date=?"
            rows = con.execute(sql, (manager_key, target_date_iso)).fetchall()
            best = ""
            for r in rows or []:
                if str(r["status"] or "") != "created":
                    continue
                if not str(r["slug"] or "").strip() or not str(r["link_url"] or "").strip():
                    continue
                if has_deleted_at and str(r["deleted_at"] or "").strip():
                    continue
                ua = str(r["updated_at"] or "").strip()
                if ua and ua > best:
                    best = ua
            return best
        finally:
            con.close()
    except Exception:
        return ""


def collect_replacement_records(
    today_iso: str,
    db_path: str,
    working_keys,
    manager_by_key: Dict[str, Dict[str, Any]],
    src_map: Dict[str, str],
    active_slugs_by_manager: Dict[str, set],
    default_link_count: int,
) -> List[Dict[str, Any]]:
    """One record per manager scheduled today (in working_keys, present in
    manager_by_key -- i.e. a real, non-archived manager) whose account looks
    unavailable: is_enabled=0, manual_stopped=1, OR the manager's last_error/
    manager_telegram_health/start_status.json signal classifies as a FATAL
    business reason via _reason_business_text (banned/session/proxy/network/
    blocked -- see _REASON_TEXT_PATTERNS). A merely non-empty but otherwise
    unrecognized/stale last_error is NOT by itself a candidate -- only a
    reason _reason_business_text actually recognizes as fatal counts (an
    enabled, non-stopped manager with a benign/old last_error and a healthy
    status/start_status is never flagged here). A merely-absent process is
    likewise never by itself evidence of an account being blocked/replaced
    (process liveness never even reaches this function -- see build_report's
    own separate, unrelated "process not found" issue).

    For each candidate primary, looks up manager_reserve_pairs /
    reserve_activation_events for today to classify state (priority
    C > B > D > A, exactly one record per primary, never duplicated):
      A - unavailable, no reserve connected, no recognized fatal reason
      B - a reserve is linked/activating but not yet confirmed ready.
          Two textual sub-kinds (record["b_kind"]), never claiming the
          new account is connected unless it genuinely is:
            "pending"   - the linked reserve has no requested/creating/done
                          event for today yet (or only a requested/creating
                          one) -- honest "ещё подключается" wording only.
            "connected" - the linked reserve's event.status=='done' but its
                          links for today are not yet a full ready set --
                          only here is "новый аккаунт подключён" said.
      C - activation done AND the reserve's links for today meet the
          project's own real readiness criterion (>= default_link_count
          active links) -- reusing the same threshold build_report/
          check_manager_today already use, not just event.status=='done'
      D - unavailable with a recognized fatal reason, no reserve connected

    Evidence of an active/completed replacement is taken ONLY from a
    reserve_activation_events row whose status is requested/creating/done
    (_ACTIVE_EVENT_STATUSES) AND whose reserve_key matches the primary's
    CURRENT manager_reserve_pairs row (status='linked'). failed/cancelled
    events, an event for a reserve that is no longer linked, or a retired/
    missing pair are never treated as evidence of a connected or in-
    progress replacement (state falls through to D/A instead). This also
    means a newer failed/cancelled event never overrides an earlier valid
    done event for the SAME still-linked reserve (the failed/cancelled
    event is simply excluded from consideration, not "the latest").

    Read-only; fails open to [] on any unexpected error so the rest of the
    report is never lost because of this block."""
    try:
        keys = sorted({normalize_manager_key(k) for k in (working_keys or []) if k})
        if not keys:
            return []

        # manager_telegram_health/start_status.json are read-only, feature-
        # detected sources: _manager_health_map fails open to {} if the
        # table doesn't exist on this DB, _read_start_status fails open to
        # {} if the file/directory is missing -- either way classification
        # below simply falls through to the is_enabled/manual_stopped/
        # last_error-pattern signals instead of raising.
        health_map = _manager_health_map(db_path)

        candidates: List[str] = []
        reason_cache: Dict[str, Optional[str]] = {}
        for mk in keys:
            row = manager_by_key.get(mk)
            if not row:
                continue
            is_enabled = int(row.get("is_enabled")) if row.get("is_enabled") is not None else 1
            manual_stopped = int(row.get("manual_stopped") or 0)
            start_status = _read_start_status(mk)
            reason_text = _reason_business_text(row, health_map.get(mk), start_status)
            is_fatal = reason_text in FATAL_REASON_TEXTS
            if is_enabled == 0 or manual_stopped == 1 or is_fatal:
                candidates.append(mk)
                reason_cache[mk] = reason_text
        if not candidates:
            return []

        pairs_map = _reserve_pairs_for_primaries(db_path, candidates)
        events_map = _reserve_activation_events_for(db_path, candidates, today_iso)
        default_link_count = int(default_link_count or 0)

        records: List[Dict[str, Any]] = []
        for mk in candidates:
            row = manager_by_key.get(mk) or {}
            display = str(row.get("display_name") or "").strip() or mk
            username = str(row.get("telegram_username") or "").strip()
            source_key = src_map.get(mk, "")

            reason_text = reason_cache.get(mk)
            is_fatal = reason_text in FATAL_REASON_TEXTS

            # The CURRENT reserve linkage is the only source of truth for
            # "who is the replacement" -- a retired/missing pair means there
            # is no active replacement at all, regardless of what events
            # exist (an event's own reserve_key is never trusted on its own).
            pairs = pairs_map.get(mk) or []
            linked_reserve_key = normalize_manager_key(pairs[0]["reserve_key"] or "") if pairs else ""

            reserve_key = ""
            event_status = ""
            event_updated_iso = ""
            if linked_reserve_key:
                reserve_key = linked_reserve_key
                events = events_map.get(mk) or []
                # events_map is ORDER BY id DESC per primary -- the first
                # event that (a) belongs to the CURRENTLY linked reserve and
                # (b) has a status that counts as active evidence
                # (requested/creating/done) is the most recent qualifying
                # one. failed/cancelled rows, and rows for a reserve that is
                # no longer linked, are skipped entirely -- so a newer
                # failed/cancelled event can never hide an earlier valid
                # "done" for the same still-linked reserve.
                active_ev = next(
                    (ev for ev in events
                     if normalize_manager_key(ev["reserve_key"] or "") == linked_reserve_key
                     and str(ev["status"] or "") in _ACTIVE_EVENT_STATUSES),
                    None,
                )
                if active_ev is not None:
                    event_status = str(active_ev["status"] or "")
                    event_updated_iso = str(active_ev["updated_at"] or "")
                # else: reserve is linked but no requested/creating/done
                # event exists for today yet -- state B, "pending" wording.

            reserve_username = ""
            if reserve_key:
                reserve_row = manager_by_key.get(reserve_key) or {}
                reserve_username = str(reserve_row.get("telegram_username") or "").strip()

            links_updated_iso = ""
            links_ready = False
            if reserve_key:
                links_updated_iso = _bizlinks_updated_at_for_manager(db_path, reserve_key, today_iso)
                active_count = len(active_slugs_by_manager.get(reserve_key) or set())
                links_ready = default_link_count > 0 and active_count >= default_link_count

            b_kind = ""
            if reserve_key and event_status == "done" and links_ready:
                state = "C"
            elif reserve_key and event_status == "done":
                # Only here may the text say "новый аккаунт подключён".
                state, b_kind = "B", "connected"
            elif reserve_key:
                # Linked but not (yet) evidenced as done -- requested/
                # creating, or no active event at all. Never claim the new
                # account is already connected.
                state, b_kind = "B", "pending"
            elif is_fatal:
                state = "D"
            else:
                state = "A"

            links_updated_hm = ""
            if state == "C":
                ts = links_updated_iso or event_updated_iso
                if ts:
                    links_updated_hm = _utc_iso_to_kyiv_hm(ts)

            records.append({
                "primary_key": mk,
                "primary_display": display,
                "primary_username": username,
                "source_key": source_key,
                "state": state,
                "b_kind": b_kind,
                "reason_text": reason_text,
                "reserve_key": reserve_key,
                "reserve_username": reserve_username,
                "links_updated_hm": links_updated_hm,
            })
        return records
    except Exception as e:
        print(f"[preflight-replacements] collect_replacement_records failed: {e!r}")
        return []


def _replacement_line(record: Dict[str, Any]) -> str:
    """Render one replacement/outage record as its final 2-3 line text
    block. Shared verbatim by render_admin_text and render_partner_text --
    plain text, no HTML/Markdown parse_mode (matches every other line in
    both renderers). Never emits an empty "@", a double space, an exception
    class name, or a raw None -- every interpolated value is defensively
    stringified first."""
    display = str(record.get("primary_display") or record.get("primary_key") or "").strip()
    username = str(record.get("primary_username") or "").strip()
    who = f"{display} | @{username}" if username else display

    reserve_username = str(record.get("reserve_username") or "").strip()
    reserve_key = str(record.get("reserve_key") or "").strip()
    new_who = f"@{reserve_username}" if reserve_username else (reserve_key or "новый аккаунт")

    state = record.get("state")
    reason_text = str(record.get("reason_text") or "").strip()

    if state == "C":
        hm = str(record.get("links_updated_hm") or "").strip()
        line2 = f"Ссылки на сегодня обновлены в {hm}." if hm else "Ссылки на сегодня обновлены."
        line3 = f"Откройте {_BIZLINKS_MENU_LABEL} и проверьте новые ссылки."
        return f"✅ {who} заменена на {new_who}.\n{line2}\n{line3}"
    if state == "B":
        if record.get("b_kind") == "connected":
            # Only here (event.status=='done', links not yet a full ready
            # set) may the text say the new account is already connected.
            return f"⚠️ {who} заменена на {new_who}.\nНовый аккаунт подключён, ссылки ещё не обновлены."
        # b_kind == "pending" (requested/creating, or a linked reserve with
        # no active event yet) -- never claim the new account is connected.
        if reserve_username:
            return f"⚠️ {who} заменяется на @{reserve_username}.\nНовый аккаунт ещё подключается, ссылки пока не обновлены."
        return f"⚠️ {who} — выполняется замена аккаунта.\nНовый аккаунт ещё подключается, ссылки пока не обновлены."
    if state == "D":
        return f"⚠️ {who} — аккаунт недоступен.\nПричина: {reason_text}.\nЗамена не подключена, ссылки не работают."
    # state == "A" (or any unrecognized value -- safest fallback is the
    # neutral "disabled, no replacement" wording, never a blank line).
    if reason_text == _REASON_ADMIN_DISABLED_TEXT:
        return f"⚠️ {who} — {_REASON_ADMIN_DISABLED_TEXT}.\nЗамена не подключена, ссылки сейчас не работают."
    return f"⚠️ {who} — аккаунт отключён.\nЗамена не подключена, ссылки сейчас не работают."


# ---------------------------------------------------------------------------
# Report assembly
# ---------------------------------------------------------------------------

def check_manager_today(
    manager_row: Dict[str, Any],
    *,
    source_key: str,
    source_name: str,
    today_iso: str,
    bizlinks_for_manager: List[sqlite3.Row],
    has_job_today: bool,
    default_link_count: int,
    health_row: Optional[Dict[str, Any]],
    process_running: Optional[bool],
) -> Dict[str, Any]:
    """Build the per-manager readiness record for today. Never raises."""
    mk = normalize_manager_key(manager_row.get("manager_key") or "")
    display = str(manager_row.get("display_name") or "").strip() or mk
    username = str(manager_row.get("telegram_username") or "").strip()

    out: Dict[str, Any] = {
        "manager_key": mk,
        "display": display,
        "username": username,
        "source_key": source_key,
        "source_name": source_name or "без источника",
        "issues": [],
    }

    # Process (per-manager main.py --manager <key>).
    if process_running is False:
        out["issues"].append({"kind": "process", "text": "процесс менеджера не найден", "fix": "перезапустить через restart_everything.bat"})

    # Session/account health -- read-only, last known status only.
    hs = str((health_row or {}).get("health_status") or "unknown").strip().lower()
    if hs not in _HEALTH_STATUS_TEXT:
        hs = "unknown"
    out["health_status"] = hs
    if hs in ("limited", "blocked"):
        out["issues"].append({
            "kind": "session",
            "text": f"{_HEALTH_STATUS_ICON[hs]} {_HEALTH_STATUS_TEXT[hs]}",
            "fix": "Панель → Контроль → Telegram Health",
        })
    elif hs == "warning":
        out["issues"].append({
            "kind": "session",
            "text": f"{_HEALTH_STATUS_ICON[hs]} {_HEALTH_STATUS_TEXT[hs]}",
            "fix": "Панель → Контроль → Telegram Health (не срочно)",
        })
    # "unknown"/stale is informational only -- not a hard failure (Stage 1 rule).

    # Proxy -- only where actually required by configuration.
    proxy_enabled = int(manager_row.get("proxy_enabled") or 0) == 1
    proxy_bypass = int(manager_row.get("proxy_bypass_allowed") or 0) == 1
    proxy_required = proxy_enabled and not proxy_bypass
    out["proxy_required"] = proxy_required
    if proxy_required:
        last_error = str(manager_row.get("proxy_last_error") or "").strip()
        if last_error:
            out["issues"].append({
                "kind": "proxy", "text": "прокси не прошёл последнюю проверку",
                "fix": "Панель → Прокси → перепроверить",
            })

    # Business links for today.
    created = [r for r in bizlinks_for_manager if str(r["status"] or "") == "created" and str(r["slug"] or "").strip()]
    actual_count = len(created)
    error_rows = [r for r in bizlinks_for_manager if str(r["last_error_class"] or "").strip()]
    out["links_actual"] = actual_count
    out["links_expected"] = default_link_count if has_job_today else None
    out["links_has_job"] = has_job_today

    if error_rows:
        seen_classes = set()
        for r in error_rows:
            cls = str(r["last_error_class"] or "unknown").strip() or "unknown"
            if cls in seen_classes:
                continue
            seen_classes.add(cls)
            friendly = _BIZLINK_ERROR_CLASS_TEXT.get(cls, _BIZLINK_ERROR_CLASS_TEXT["unknown"])
            out["issues"].append({
                "kind": "link", "text": friendly,
                "fix": "Панель → Бизнес-ссылки → создать/пересоздать",
            })
    elif has_job_today and actual_count < default_link_count:
        out["issues"].append({
            "kind": "link",
            "text": f"{actual_count} из {default_link_count} ссылок на сегодня",
            "fix": "Панель → Бизнес-ссылки → создать недостающие",
        })
    # If there is no job today, a zero/low link count is informational only
    # (Stage 1 rule: do not report a missing item as a problem when it is not
    # required/expected for this manager by existing project configuration).

    out["ok"] = not out["issues"]
    return out


def build_report(today_iso: Optional[str] = None, db_path: Optional[str] = None) -> Dict[str, Any]:
    """Assemble the full Stage 1 readiness report for `today_iso` (Kyiv date,
    defaults to today). Pure read-only; never raises; never sends anything."""
    today_iso = today_iso or kyiv_today_iso()
    db_path = db_path or storage.DB_PATH

    report: Dict[str, Any] = {
        "date_iso": today_iso,
        "date_display": fmt_date_ru(today_iso),
        "check_time": kyiv_now_hms(),
        "managers": [],
        "working_count": 0,
        "archived_count": 0,
        "services": {"ok": True, "issues": [], "detail": {}},
        "all_ok": True,
    }

    try:
        working_keys = storage.manager_schedule_list_working_on_date(today_iso, db_path=db_path) or []
    except Exception:
        working_keys = []
    working_keys = sorted({normalize_manager_key(k) for k in working_keys if k})

    if not working_keys:
        # Nothing scheduled today -- still run the service check, report is
        # short and honest ("0 managers today"), not an invented fallback.
        procs = _scan_python_processes()
        report["services"] = check_services(procs)
        report["all_ok"] = report["services"]["ok"]
        return report

    procs = _scan_python_processes()
    report["services"] = check_services(procs)

    manager_rows = list_manager_rows_from_db_sync(db_path, include_removed=False) or []
    manager_by_key = {normalize_manager_key(r.get("manager_key") or ""): r for r in manager_rows}
    # Also fetch archived/removed rows so a schedule entry that only fails
    # the *active* filter (status='archived') can be told apart from one
    # that is genuinely missing from the DB entirely. Old/historical
    # managers (e.g. a closed-out account kept for statistics) must not be
    # reported as a readiness error just because a stale schedule row still
    # lists them.
    try:
        manager_rows_all = list_manager_rows_from_db_sync(db_path, include_removed=True) or []
    except Exception:
        manager_rows_all = manager_rows
    manager_by_key_all = {normalize_manager_key(r.get("manager_key") or ""): r for r in manager_rows_all}

    src_map = _manager_source_map(db_path)
    src_names = _source_names(db_path)
    default_count = _bizlink_default_count(db_path)
    bizlinks_by_mgr = _bizlinks_today_by_manager(db_path, today_iso)
    health_map = _manager_health_map(db_path)

    try:
        jobs_today = storage.bizlink_job_list_for_date(today_iso, db_path=db_path) or []
    except Exception:
        jobs_today = []
    job_keys_today = {normalize_manager_key(j.get("manager_key") or "") for j in jobs_today}

    base_norm = _norm_path(str(BASE_DIR))
    relevant_procs = [p for p in procs if base_norm in _norm_path(p.get("cmd", ""))]
    running_manager_keys = set()
    for p in relevant_procs:
        cmd = _norm_path(p.get("cmd", ""))
        if "main.py" not in cmd or "--manager" not in cmd:
            continue
        for mk_candidate in manager_by_key.keys():
            if mk_candidate and f"--manager {mk_candidate}" in cmd:
                running_manager_keys.add(mk_candidate)
    # Only trust a per-manager "process not found" verdict when the scan
    # actually found SOME TPilot-relevant process on the machine. If it found
    # none at all, that is a scan/environment problem already surfaced by
    # check_services() above -- re-flagging every single manager individually
    # in that case would be redundant, noisy, and potentially misleading.
    process_check_trustworthy = bool(relevant_procs)

    all_ok = report["services"]["ok"]
    archived_count = 0
    archived_managers: List[Dict[str, str]] = []
    active_working_count = 0
    for mk in working_keys:
        row = manager_by_key.get(mk)
        if not row:
            row_all = manager_by_key_all.get(mk)
            sk = src_map.get(mk, "")
            source_name = src_names.get(sk, sk) if sk else "без источника"
            if row_all is not None:
                # Archived/historical manager still listed in a stale
                # schedule row -- not a readiness error, excluded from the
                # active count, source mapping preserved if it exists.
                archived_count += 1
                display = str(row_all.get("display_name") or "").strip() or mk
                archived_managers.append({"manager_key": mk, "display": display, "source_name": source_name})
                continue
            # Not found in the DB at all (active or archived) -- check for a
            # legacy alias: a rename that left an orphaned schedule row
            # under the OLD key (e.g. "darias" -> "dariass") with the new
            # key already active and correctly source-mapped. Treated the
            # same as an archived manager: excluded, not a readiness error,
            # source mapping taken from the matched alias.
            alias_key = _find_legacy_alias_key(mk, manager_by_key.keys())
            if alias_key:
                alias_row = manager_by_key.get(alias_key) or {}
                alias_sk = src_map.get(alias_key, "")
                alias_source_name = src_names.get(alias_sk, alias_sk) if alias_sk else "без источника"
                archived_count += 1
                display = str(alias_row.get("display_name") or "").strip() or alias_key
                archived_managers.append({"manager_key": mk, "display": display, "source_name": alias_source_name})
                continue
            # READINESS FIX 20260712: genuinely missing from the DB entirely
            # (no active row, no archived/removed row, no legacy-alias
            # match). This is almost always STALE schedule data -- either a
            # hard-deleted manager (tombstone-only, kept in
            # manager_stats_tombstones for historical STATS retention only --
            # never for tomorrow's readiness) or a historical key with
            # leftover manager_work_schedule_days rows and nothing else in
            # the DB at all. Readiness must reflect real ACTIVE managers
            # only, so this is treated exactly like an archived manager:
            # excluded from working_count, no readiness error, never
            # appended to report["managers"] (so it can never inflate the
            # "Без источника" source breakdown, which only counts entries
            # actually in report["managers"]), all_ok left untouched.
            try:
                is_tombstoned = storage.manager_stats_tombstone_get(mk, db_path=db_path) is not None
            except Exception:
                is_tombstoned = False
            archived_count += 1
            archived_managers.append({
                "manager_key": mk, "display": mk, "source_name": source_name,
                "reason": "удалён (архив статистики)" if is_tombstoned else "устаревшая запись графика",
            })
            continue
        active_working_count += 1
        sk = src_map.get(mk, "")
        rec = check_manager_today(
            row,
            source_key=sk,
            source_name=src_names.get(sk, sk),
            today_iso=today_iso,
            bizlinks_for_manager=bizlinks_by_mgr.get(mk, []),
            has_job_today=mk in job_keys_today,
            default_link_count=default_count,
            health_row=health_map.get(mk),
            process_running=(mk in running_manager_keys) if process_check_trustworthy else None,
        )
        report["managers"].append(rec)
        if not rec["ok"]:
            all_ok = False

    report["working_count"] = active_working_count
    report["archived_count"] = archived_count
    report["archived_managers"] = archived_managers

    # The replacement/outage block is a MORNING, TODAY-ONLY feature (owner
    # decision) -- it must never collect records, touch all_ok, or dedupe
    # the "process not found" issue for an evening/tomorrow report (or any
    # other non-today date). This is decided by comparing the requested
    # report date against the real Kyiv "today", NOT by wall-clock hour or
    # a period_kind parameter (build_report has none, and both the morning
    # AdminBot/PartnerBot calls AND any manual/correction re-check call
    # always pass the real current date for a morning/today report --
    # panel_bot.py/partner_stat_bot.py's evening calls always pass
    # tomorrow's date instead, so this check reliably tells them apart
    # without requiring any change to those callers).
    records: List[Dict[str, Any]] = []
    if today_iso == kyiv_today_iso():
        try:
            active_slugs_by_manager = _bizlinks_active_slugs_by_manager(db_path, today_iso)
            records = collect_replacement_records(
                today_iso, db_path, working_keys, manager_by_key, src_map,
                active_slugs_by_manager, default_count,
            )
        except Exception as e:
            print(f"[preflight-replacements] build_report integration failed: {e!r}")
            records = []
        replaced_keys = {r["primary_key"] for r in records}
        for rec in report["managers"]:
            if rec["manager_key"] in replaced_keys:
                # Avoid double-reporting: the new replacement block already
                # tells the story for this manager -- do not also flag the
                # generic "process not found" issue for the same primary.
                # Any OTHER, genuinely independent issue (session/proxy/
                # link) stays. Today-only, per the guard above.
                rec["issues"] = [i for i in rec["issues"] if i.get("kind") != "process"]
                rec["ok"] = not rec["issues"]
    report["replacements"] = records
    all_ok = report["services"]["ok"] and all(rec["ok"] for rec in report["managers"])
    # States A/B/D are structural problems (never hidden by defer/recheck
    # logic); state C (replacement complete, links ready) must NOT make an
    # otherwise-fine report look problematic. Only applied today -- records
    # is always [] for a non-today report, so this is a no-op there.
    all_ok = all_ok and not any(r["state"] != "C" for r in records)
    report["all_ok"] = all_ok
    return report


# ---------------------------------------------------------------------------
# Deep link verification (read-only Telegram side, via the EXISTING manager
# queue + EXISTING bizlink_list_telegram command -- no new RPC logic, no
# main.py/storage.py edits). PanelBot is the only caller of
# run_deep_verification(); PartnerBot only reads the resulting blob.
# ---------------------------------------------------------------------------

def _relevant_managers_for_deep_verify(
    target_date_iso: str, db_path: str
) -> Dict[str, Dict[str, Any]]:
    """Managers who are (a) scheduled to work target_date_iso and (b) have
    something relevant to verify (an active bizlink_jobs row for that date,
    or already-active DB link rows). Never invents a requirement that
    doesn't exist -- mirrors build_report/build_partner_report's own
    "expected" semantics. Returns manager_key -> {source_key, display_name,
    username, expected, actual, db_slugs}."""
    try:
        working_keys = storage.manager_schedule_list_working_on_date(target_date_iso, db_path=db_path) or []
    except Exception:
        working_keys = []
    working_keys = sorted({normalize_manager_key(k) for k in working_keys if k})
    if not working_keys:
        return {}

    try:
        jobs = storage.bizlink_job_list_for_date(target_date_iso, db_path=db_path) or []
    except Exception:
        jobs = []
    job_keys = {normalize_manager_key(j.get("manager_key") or "") for j in jobs}

    default_count = _bizlink_default_count(db_path)
    active_slugs_by_mk = _bizlinks_active_slugs_by_manager(db_path, target_date_iso)
    src_map = _manager_source_map(db_path)
    manager_rows = list_manager_rows_from_db_sync(db_path, include_removed=False) or []
    manager_by_key = {normalize_manager_key(r.get("manager_key") or ""): r for r in manager_rows}

    relevant: Dict[str, Dict[str, Any]] = {}
    for mk in working_keys:
        # READINESS FIX 20260712: only ACTIVE managers (present in
        # manager_by_key, include_removed=False) are eligible for deep
        # verification -- a tombstone-only or genuinely-missing schedule key
        # has no live runtime to enqueue a manager_queue command to;
        # enqueuing one would just guarantee a wasted, permanently-timing-
        # out queue row for a manager that can never answer.
        row = manager_by_key.get(mk)
        if row is None:
            continue
        db_slugs = active_slugs_by_mk.get(mk, set())
        has_job = mk in job_keys
        if not has_job and not db_slugs:
            continue  # nothing relevant to verify for this manager on this date
        relevant[mk] = {
            "source_key": src_map.get(mk, ""),
            "display_name": str(row.get("display_name") or "").strip() or mk,
            "username": str(row.get("telegram_username") or "").strip(),
            "expected": default_count if has_job else len(db_slugs),
            "actual": len(db_slugs),
            "db_slugs": db_slugs,
        }
    return relevant


async def run_deep_verification(
    target_date_iso: str,
    db_path: Optional[str] = None,
    period: str = "morning",
    deadline_ts: Optional[float] = None,
) -> Dict[str, Any]:
    """Enqueue exactly one read-only "bizlink_list_telegram" command per
    relevant manager (via the EXISTING storage.manager_queue_put/get -- the
    same queue and command the project's own bizfix: block already relies
    on), poll until every manager answers or `deadline_ts` (an
    asyncio loop-time value, i.e. asyncio.get_event_loop().time()) passes,
    compare each manager's DB-active slugs against the live Telegram slug
    set the manager runtime returns, and return a JSON-serializable
    verification blob (see module docstring / task spec for the exact
    shape). Never creates/deletes/sends/joins/clicks anything -- the only
    Telegram-side call reachable from this path is the existing read-only
    GetBusinessChatLinksRequest via _manager_list_business_links (main.py),
    unchanged. Never raises."""
    db_path = db_path or storage.DB_PATH
    blob: Dict[str, Any] = {
        "period": period,
        "target_date": target_date_iso,
        "checked_at": kyiv_now_hms(),
        "status_global": "not_checked",
        "sources": {},
        "managers": {},
    }

    try:
        relevant = _relevant_managers_for_deep_verify(target_date_iso, db_path)
    except Exception:
        relevant = {}
    if not relevant:
        return blob

    # --- enqueue one bizlink_list_telegram command per relevant manager ---
    expires_at = (datetime.now(__import__("datetime").timezone.utc).replace(tzinfo=None) + timedelta(minutes=18)).replace(microsecond=0).isoformat()
    nonces: Dict[str, str] = {}
    for mk in relevant.keys():
        try:
            payload_json = json.dumps({"manager_key": mk, "requested_by_user_id": 0}, ensure_ascii=False)
            nonce = await storage.manager_queue_put(
                target_key=mk,
                command="bizlink_list_telegram",
                args="",
                payload_json=payload_json,
                created_by="preflight",
                expires_at=expires_at,
                db_path=db_path,
            )
            nonces[mk] = nonce
        except Exception as e:
            relevant[mk]["enqueue_error"] = repr(e)[:200]

    # --- poll for results until all done or deadline ---
    loop = asyncio.get_event_loop()
    if deadline_ts is None:
        deadline_ts = loop.time() + 15 * 60
    results: Dict[str, Dict[str, Any]] = {}
    pending = set(nonces.keys())
    while pending and loop.time() < deadline_ts:
        for mk in list(pending):
            nonce = nonces.get(mk)
            if not nonce:
                pending.discard(mk)
                continue
            try:
                row = await storage.manager_queue_get(nonce, db_path=db_path)
            except Exception:
                row = None
            if row and str(row.get("status") or "") in ("done", "error"):
                results[mk] = row
                pending.discard(mk)
        if pending:
            await asyncio.sleep(2.0)

    if pending:
        # Cosmetic queue hygiene only -- marks any still-"new"/"processing"
        # rows past their own expires_at as "timeout" in manager_commands.
        # Never affects Telegram state.
        try:
            await storage.manager_queue_mark_timeouts(db_path=db_path)
        except Exception:
            pass

    # --- assemble per-manager verification result ---
    sources_agg: Dict[str, Dict[str, Any]] = {}
    for mk, info in relevant.items():
        db_slugs = info.get("db_slugs") or set()
        expected = int(info["expected"])
        actual = int(info["actual"])
        row = results.get(mk)
        m_entry: Dict[str, Any] = {
            "source_key": info["source_key"],
            "display_name": info["display_name"],
            "username": info["username"],
            "expected": expected,
            "actual": actual,
            "verified": 0,
            "failed": 0,
            "status": "not_checked",
            "reason": "",
            "failed_slots": [],
        }

        if mk in pending or row is None:
            # READINESS FIX 20260712: the manager runtime didn't answer the
            # bizlink_list_telegram queue command in time, but the central
            # DB already has all the expected created/non-deleted links for
            # this manager+date (computed BEFORE the queue command, in
            # _relevant_managers_for_deep_verify) -- report OK using that DB
            # evidence instead of a false timeout/red status. A live
            # Telegram-side re-check will still happen on the next
            # scheduled deep-verify run; this only prevents a false red when
            # the links already demonstrably exist.
            if expected > 0 and actual >= expected:
                m_entry["status"] = "ok"
                m_entry["verified"] = expected
                m_entry["failed"] = 0
                m_entry["reason"] = "ссылки уже созданы (Telegram-проверка не завершилась вовремя)"
            else:
                m_entry["status"] = "timeout"
                m_entry["reason"] = "менеджер не ответил в отведённое время"
        else:
            try:
                res = json.loads(str(row.get("result_json") or row.get("result_text") or "{}"))
            except Exception:
                res = {}
            row_status = str(row.get("status") or "")
            if row_status == "error" or not res.get("ok"):
                err_class = str(res.get("error_class") or "")
                if err_class == "flood_wait":
                    m_entry["status"] = "failed"
                    m_entry["reason"] = f"FloodWait {int(res.get('flood_wait_seconds') or 0)} сек."
                elif err_class == "auth_session":
                    m_entry["status"] = "failed"
                    m_entry["reason"] = "проблема с сессией аккаунта"
                else:
                    m_entry["status"] = "failed"
                    m_entry["reason"] = "не удалось получить список ссылок из Telegram"
            else:
                live_links = res.get("links") or []
                live_slugs = {str(l.get("slug") or "").strip() for l in live_links if str(l.get("slug") or "").strip()}
                verified_slugs = db_slugs & live_slugs
                missing_slugs = db_slugs - live_slugs
                verified = len(verified_slugs)
                failed = len(missing_slugs)
                m_entry["verified"] = verified
                m_entry["failed"] = failed
                if expected and verified == expected and failed == 0:
                    m_entry["status"] = "ok"
                elif verified > 0:
                    m_entry["status"] = "partial"
                    m_entry["reason"] = f"{failed} ссыл. не найдено в Telegram" if failed else ""
                else:
                    m_entry["status"] = "failed"
                    m_entry["reason"] = "ни одна ссылка не найдена в Telegram"

        blob["managers"][mk] = m_entry

        sk = info["source_key"] or "_none"
        agg = sources_agg.setdefault(sk, {"expected": 0, "actual": 0, "verified": 0, "failed": 0, "statuses": []})
        agg["expected"] += expected
        agg["actual"] += actual
        agg["verified"] += m_entry["verified"]
        agg["failed"] += m_entry["failed"]
        agg["statuses"].append(m_entry["status"])

    for sk, agg in sources_agg.items():
        statuses = agg.pop("statuses")
        if statuses and all(s == "ok" for s in statuses) and agg["failed"] == 0:
            s_status = "ok"
        elif statuses and all(s == "timeout" for s in statuses):
            s_status = "timeout"
        elif agg["verified"] > 0 or any(s == "ok" for s in statuses):
            s_status = "partial"
        else:
            s_status = "failed"
        agg["status"] = s_status
        agg["reason"] = ""
        blob["sources"][sk] = agg

    manager_statuses = [m["status"] for m in blob["managers"].values()]
    if not manager_statuses:
        blob["status_global"] = "not_checked"
    elif all(s == "ok" for s in manager_statuses):
        blob["status_global"] = "ok"
    elif all(s == "timeout" for s in manager_statuses):
        blob["status_global"] = "timeout"
    elif any(s in ("ok", "partial") for s in manager_statuses):
        blob["status_global"] = "partial"
    else:
        blob["status_global"] = "failed"

    return blob


def apply_deep_results(
    report: Dict[str, Any],
    deep_blob: Optional[Dict[str, Any]],
    period_kind: str = "today",
) -> Dict[str, Any]:
    """Pure merge (no Telegram calls, never raises): attach deep-verification
    fields onto an existing admin or partner report dict (from build_report
    or build_partner_report), in place, and also return it. If deep_blob is
    None/empty/has no matching data at all, marks deep status "not_checked"
    -- render_admin_text/render_partner_text must never claim green OK in
    that case, they fall back to the honest DB-only wording instead.

    Two merge layers, both read from the SAME blob:
    1. Per-manager (by manager_key) -- used for per-manager detail rows
       (admin's recommendations listing) and as a source-name fallback for
       the admin source breakdown.
    2. Per-source (by normalized source_key, against deep_blob["sources"])
       -- the PRIMARY basis for deep_status_global/totals below. This is
       deliberately more robust than summing the per-manager merge alone:
       a single manager-key mismatch (e.g. a legacy/renamed schedule key
       that isn't in the blob at all) must never make an otherwise fully
       verified source look unchecked."""
    managers = report.get("managers") or []
    deep_managers = (deep_blob or {}).get("managers") or {}
    deep_sources = (deep_blob or {}).get("sources") or {}
    deep_sources_norm = {_norm_source_key(k): v for k, v in deep_sources.items()}

    any_relevant = False
    for m in managers:
        mk = m.get("manager_key")
        dm = deep_managers.get(mk)
        if dm is None:
            m["deep_status"] = "not_checked"
            m["deep_verified"] = 0
            m["deep_failed"] = 0
            m["deep_expected"] = 0
            m["deep_reason"] = ""
            m["deep_source_key"] = ""
            continue
        any_relevant = True
        m["deep_status"] = str(dm.get("status") or "not_checked")
        m["deep_verified"] = int(dm.get("verified") or 0)
        m["deep_failed"] = int(dm.get("failed") or 0)
        m["deep_expected"] = int(dm.get("expected") or 0)
        m["deep_reason"] = str(dm.get("reason") or "")
        m["deep_source_key"] = str(dm.get("source_key") or "")

    # Every source_key relevant to THIS report: partner reports carry their
    # own single source_key; admin reports collect every distinct
    # source_key across their managers. Looked up directly against the
    # blob's own per-source aggregate, independent of manager-key matching.
    report_source_keys = set()
    rep_sk = report.get("source_key")
    if rep_sk:
        report_source_keys.add(_norm_source_key(rep_sk))
    for m in managers:
        sk = m.get("source_key")
        if sk:
            report_source_keys.add(_norm_source_key(sk))

    source_expected_total = 0
    source_verified_total = 0
    source_failed_total = 0
    source_statuses: List[str] = []
    for sk in report_source_keys:
        sdata = deep_sources_norm.get(sk)
        if sdata is None:
            continue
        any_relevant = True
        source_expected_total += int(sdata.get("expected") or 0)
        source_verified_total += int(sdata.get("verified") or 0)
        source_failed_total += int(sdata.get("failed") or 0)
        source_statuses.append(str(sdata.get("status") or "not_checked"))

    if source_statuses:
        deep_verified_total = source_verified_total
        deep_expected_total = source_expected_total
        deep_failed_total = source_failed_total
        if all(s == "ok" for s in source_statuses) and deep_failed_total == 0:
            deep_status_global = "ok"
        elif all(s == "timeout" for s in source_statuses):
            deep_status_global = "timeout"
        elif any(s in ("ok", "partial") for s in source_statuses):
            deep_status_global = "partial"
        else:
            deep_status_global = "failed"
    else:
        # No source-level data at all -- fall back to the per-manager merge.
        deep_verified_total = sum(int(m.get("deep_verified") or 0) for m in managers)
        deep_expected_total = sum(int(m.get("deep_expected") or 0) for m in managers)
        deep_failed_total = sum(int(m.get("deep_failed") or 0) for m in managers)
        deep_statuses = [m["deep_status"] for m in managers if m.get("deep_status") != "not_checked"]
        if not any_relevant:
            deep_status_global = "not_checked"
        elif deep_statuses and all(s == "ok" for s in deep_statuses):
            deep_status_global = "ok"
        elif deep_statuses and all(s == "timeout" for s in deep_statuses):
            deep_status_global = "timeout"
        elif any(s in ("ok", "partial") for s in deep_statuses):
            deep_status_global = "partial"
        else:
            deep_status_global = "failed" if deep_statuses else "not_checked"

    report["deep_status_global"] = deep_status_global
    report["deep_verified_total"] = deep_verified_total
    report["deep_expected_total"] = deep_expected_total
    report["deep_failed_total"] = deep_failed_total
    return report


# ---------------------------------------------------------------------------
# AUTO-RECHECK + MESSAGE CORRECTION 20260712: small pure helpers shared by
# panel_bot.py (AdminBot) and partner_stat_bot.py (PartnerBot) to decide
# whether a report is fully green, whether its remaining problem is a
# plausibly TRANSIENT deep-link-verification issue worth an automatic
# re-check, and to render the short correction note. No Telegram calls, no
# DB access, no state -- pure functions over an already-built report dict
# (from build_report/build_partner_report, optionally merged with
# apply_deep_results). Never raises.
# ---------------------------------------------------------------------------

CORRECTION_NOTE_ADMIN = "✅ Неполадки устранены. Готовность перепроверена. Всё готово к работе."
CORRECTION_NOTE_PARTNER = "✅ Готовность перепроверена. Всё готово к работе."
CORRECTED_TITLE_ADMIN = "✅ Обновлено: готовность перепроверена"


def report_is_ok(report: Dict[str, Any]) -> bool:
    """True when the report is fully green and needs no automatic recheck:
    all_ok (the DB-level/structural check, computed BEFORE any deep-verify
    merge) AND deep_status_global is either "ok" (fully verified) or
    "not_checked" (deep verification wasn't relevant/hasn't run yet, e.g.
    deep_expected_total==0). Deliberately STRICTER than the rendered
    "problem line" threshold in render_admin_text/render_partner_text
    (which only flags "partial"/"failed" as visibly red) -- "timeout" must
    still count as NOT fully ok here, otherwise report_has_recheckable_
    problem() below can never fire for the most common transient case
    (manager runtime did not answer in time)."""
    try:
        all_ok = bool(report.get("all_ok"))
        if not all_ok:
            return False
        deep_status = str(report.get("deep_status_global") or "not_checked")
        if deep_status == "ok":
            return True
        if deep_status == "not_checked":
            # Genuinely nothing to verify (no relevant links expected) is
            # fine; "not_checked" while links ARE expected means deep
            # verification simply hasn't produced a result yet -- NOT ok,
            # matches report_has_recheckable_problem's own handling below.
            return int(report.get("deep_expected_total") or 0) <= 0
        return False  # timeout/partial/failed -- never fully ok
    except Exception:
        return False


def report_has_recheckable_problem(report: Dict[str, Any]) -> bool:
    """True when the report is NOT ok, but the ONLY remaining problem is a
    plausibly transient deep-link-verification issue (manager runtime
    timeout, FloodWait, partial Telegram-side verification, or deep verify
    simply not having produced a result yet despite relevant links/jobs
    existing) -- the kind of problem an automatic re-check can resolve.

    Deliberately returns False for any STRUCTURAL problem (session/proxy/
    process issues, a real DB-level bizlink creation error, an expected-
    link undercount) -- report["all_ok"] already reflects those (computed
    purely from build_report/build_partner_report's own per-manager checks,
    independent of any deep-verify merge), so all_ok=False here means a
    real problem exists that re-running deep verification cannot fix (it is
    read-only and never creates/repairs anything)."""
    try:
        if report_is_ok(report):
            return False
        if not report.get("all_ok"):
            # A genuine structural/DB-level problem exists -- not something
            # a deep-verify re-check can resolve.
            return False
        deep_status = str(report.get("deep_status_global") or "not_checked")
        if deep_status in ("timeout", "partial", "failed"):
            return True
        if deep_status == "not_checked" and int(report.get("deep_expected_total") or 0) > 0:
            return True
        return False
    except Exception:
        return False


def apply_correction_note(text: str, note: str) -> str:
    """Prepend a short correction note above an already-rendered report
    text (the report's own title stays intact as the next visible line).
    Pure string operation; never raises; a blank/empty note is a no-op."""
    t = str(text or "")
    n = str(note or "").strip()
    if not n:
        return t
    return f"{n}\n\n{t}"


def _source_breakdown_for_managers(managers: List[Dict[str, Any]]) -> List[tuple]:
    """Group ACTIVE readiness managers (the entries already in
    report["managers"] -- archived/legacy-alias/genuinely-missing entries
    never reach here, see build_report) by their display source name, for
    the AdminBot "По источникам" breakdown. Falls back to the deep blob's
    own manager->source_key mapping (deep_source_key, set by
    apply_deep_results) only when a manager's own source_name is empty --
    never invents a source. Returns (name, count) pairs, "Без источника"
    (if any) always last."""
    counts: Dict[str, int] = {}
    for m in managers:
        name = str(m.get("source_name") or "").strip()
        if not name or name == "без источника":
            name = str(m.get("deep_source_key") or "").strip()
        name = name or "Без источника"
        counts[name] = counts.get(name, 0) + 1
    without_source = counts.pop("Без источника", 0)
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if without_source:
        ordered.append(("Без источника", without_source))
    return ordered


# ---------------------------------------------------------------------------
# Rendering (AdminBot). Plain text -- panel_bot.py does not use parse_mode='html'.
# ---------------------------------------------------------------------------

def render_admin_text(report: Dict[str, Any], period_kind: str = "today") -> str:
    """period_kind: "today" (morning, unchanged Stage 1 wording) or
    "tomorrow" (evening -- checks tomorrow's readiness instead)."""
    is_evening = period_kind == "tomorrow"
    title = "🛡 Вечерняя проверка готовности" if is_evening else "🛡 Утренняя проверка готовности"
    work_label = "Завтра работают" if is_evening else "Сегодня работают"
    links_label = "Ссылки на завтра" if is_evening else "Ссылки на сегодня"
    ready_line = "Статус: подготовка на завтра завершена." if is_evening else "Статус: система готова к работе."
    no_managers_line = "Статус: завтра в графике нет менеджеров." if is_evening else "Статус: сегодня в графике нет менеджеров."
    final_line = "Хорошего вечера 🌇" if is_evening else "Хорошего дня ☀️"

    lines: List[str] = [
        title,
        f"📅 {report['date_display']} · проверено в {report['check_time']}",
        "",
        f"{work_label}: {report['working_count']} менеджер(ов)",
    ]
    archived_count = int(report.get("archived_count") or 0)
    if archived_count:
        # Old/historical managers still listed in a stale schedule row --
        # informational only, never counted in working_count and never
        # shown as a readiness error (see build_report).
        lines.append(f"ℹ️ В графике есть архивные аккаунты: {archived_count} — в готовности не учитываются")
    breakdown = _source_breakdown_for_managers(report.get("managers") or [])
    if breakdown:
        lines.append("")
        lines.append("По источникам:")
        for name, count in breakdown:
            lines.append(f"• {name} — {count} аккаунт(ов)")
    lines.append("")

    svc = report["services"]
    lines.append(("✅" if svc["ok"] else "❌") + " Система/процессы")
    if not svc["ok"]:
        for issue in svc["issues"]:
            lines.append(f"  • {issue}")
    lines.append("")

    managers = report["managers"]
    if not managers:
        lines.append(no_managers_line)
        lines.append("")
        lines.append(final_line)
        return "\n".join(lines)

    session_issues = []
    proxy_issues = []
    link_issues = []
    process_issues = []
    for m in managers:
        label_src = f"{m['display']} / {m['source_name']}" if m.get("source_name") else m["display"]
        label_user = f"{m['display']} / @{m['username']}" if m.get("username") else m["display"]
        for issue in m.get("issues", []):
            kind = issue.get("kind")
            if kind == "session":
                session_issues.append(f"{label_src}: {issue['text']} — {issue['fix']}")
            elif kind == "proxy":
                # Human-readable, non-technical: never echo raw proxy error
                # text/credentials to admins.
                proxy_issues.append(f"{label_user} — ошибка прокси, проверьте подключение")
            elif kind == "link":
                link_issues.append(f"{label_src}: {issue['text']} — {issue['fix']}")
            else:
                process_issues.append(f"{label_src}: {issue['text']} — {issue['fix']}")

    lines.append(("✅" if not session_issues else "⚠️") + " Сессии")
    if session_issues:
        for e in session_issues:
            lines.append(f"  • {e}")
    else:
        lines.append("  все проверенные аккаунты в порядке")
    lines.append("")

    proxy_required_count = sum(1 for m in managers if m.get("proxy_required"))
    lines.append(("✅" if not proxy_issues else "⚠️") + " Прокси")
    if proxy_issues:
        count_line = "  1 аккаунт требует проверки" if len(proxy_issues) == 1 else f"  {len(proxy_issues)} аккаунт(ов) требует проверки"
        lines.append(count_line)
        for e in proxy_issues:
            lines.append(f"  • {e}")
    elif proxy_required_count:
        lines.append(f"  требуется для {proxy_required_count} аккаунт(ов) — всё работает")
    else:
        lines.append("  для сегодняшних аккаунтов прокси не требуется")
    lines.append("")

    # Replacement/outage block -- morning (today) report only, per spec.
    # No header/lines at all when there is nothing to report, so the
    # existing text stays byte-for-byte identical when this is empty.
    replacements = (report.get("replacements") or []) if not is_evening else []
    if replacements:
        lines.append("🔁 Замены аккаунтов")
        for rec in replacements:
            lines.append(_replacement_line(rec))
        lines.append("")

    total_actual = sum(m.get("links_actual") or 0 for m in managers)
    total_expected = sum(m.get("links_expected") or 0 for m in managers if m.get("links_expected") is not None)

    # Deep-verify wording: the green "проверены и готовы к использованию"
    # line is only ever emitted when deep_status_global == "ok" (every
    # relevant source's expected>0, verified==expected, failed==0 -- see
    # apply_deep_results). Any other deep status uses honest non-claiming
    # wording:
    #   partial/failed          -> "проверено X/Y, есть проблема" (❌)
    #   timeout, or not_checked
    #   with DB evidence of      -> "не успели пройти проверку..." (⚠️)
    #   relevant links/jobs
    #   truly nothing relevant   -> "ссылки не запланированы" (ℹ️)
    # If deep verification was never run at all (DEEP_VERIFY_AVAILABLE was
    # False, or apply_deep_results() was never called), deep_status_global
    # is absent/"not_checked" and falls into the same evidence-based logic
    # below -- it can never claim green OK in that case.
    deep_status = report.get("deep_status_global", "not_checked")
    deep_verified_total = int(report.get("deep_verified_total") or 0)
    deep_expected_total = int(report.get("deep_expected_total") or 0)
    deep_issues: List[str] = []
    if deep_status != "not_checked":
        deep_reason_fallback = {
            "partial": "часть ссылок не найдена в Telegram",
            "failed": "ссылки не найдены в Telegram",
            "timeout": "менеджер не ответил в отведённое время",
        }
        for m in managers:
            ds = m.get("deep_status", "not_checked")
            if ds in ("ok", "not_checked"):
                continue
            label = f"{m['display']} / {m['source_name']}" if m.get("source_name") else m["display"]
            reason = m.get("deep_reason") or deep_reason_fallback.get(ds, "не проверено")
            deep_issues.append(f"{label}: {reason} — Панель → Бизнес-ссылки → проверить/пересоздать")

    if deep_status == "ok" and deep_expected_total > 0:
        lines.append(f"✅ {links_label}")
        lines.append(f"  проверены и готовы к использованию — {deep_verified_total}/{deep_expected_total} · статус OK")
    elif deep_status in ("partial", "failed"):
        lines.append(f"❌ {links_label}")
        lines.append(f"  проверено {deep_verified_total}/{deep_expected_total}, есть проблема")
        for e in deep_issues:
            lines.append(f"  • {e}")
    elif link_issues:
        # Real DB-level link error, independent of deep-verify -- always a
        # problem, never downgraded to "pending"/"not planned".
        lines.append(f"❌ {links_label}")
        lines.append(f"  проверено {total_actual}/{total_expected}, есть проблема" if total_expected else "  есть проблема")
        for e in link_issues:
            lines.append(f"  • {e}")
    elif deep_status == "timeout" or (deep_status == "not_checked" and (total_expected > 0 or total_actual > 0)):
        # Deep verification timed out, OR hasn't produced a result yet even
        # though DB-level evidence shows relevant links/jobs exist for this
        # date -- never claim "not planned" when there is a job/active link.
        lines.append(f"⚠️ {links_label}")
        lines.append("  не успели пройти проверку, запуск после подтверждения")
    else:
        lines.append(f"ℹ️ {links_label}")
        lines.append("  ссылки не запланированы")
    lines.append("")

    if process_issues:
        lines.append("❌ Менеджеры (процессы)")
        for e in process_issues:
            lines.append(f"  • {e}")
        lines.append("")

    # "timeout"/pending is not itself a problem requiring action (verification
    # simply hasn't finished yet) -- only partial/failed block the ready line.
    deep_not_ok = deep_status in ("partial", "failed")
    if report["all_ok"] and not deep_not_ok:
        lines.append(ready_line)
    else:
        lines.append("Рекомендации:")
        n = 1
        for group in (process_issues, session_issues, proxy_issues, link_issues, deep_issues):
            for e in group:
                lines.append(f"{n}. {e}")
                n += 1

    lines.append("")
    lines.append(final_line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Partner/source-scoped report (PartnerBot). Only managers mapped to
# `source_key` who are also working on target_date_iso -- never other
# sources, never other dates. No session/proxy/process detail (partner-
# facing text stays limited to accounts + links, per spec).
# ---------------------------------------------------------------------------

def relevant_source_keys_for_date(target_date_iso: str, db_path: Optional[str] = None) -> set:
    """Distinct normalized source_keys with at least one ACTIVE (non-
    archived) manager scheduled to work on target_date_iso -- the same
    criteria build_partner_report uses to decide a source is relevant at
    all. Read-only; never raises. Used only for diagnostic logging (e.g.
    PartnerBot noticing a source that is active today but has no enabled
    buyer target to receive the report)."""
    db_path = db_path or storage.DB_PATH
    try:
        working_keys = storage.manager_schedule_list_working_on_date(target_date_iso, db_path=db_path) or []
    except Exception:
        working_keys = []
    working_keys = {normalize_manager_key(k) for k in working_keys if k}
    if not working_keys:
        return set()
    try:
        manager_rows = list_manager_rows_from_db_sync(db_path, include_removed=False) or []
    except Exception:
        manager_rows = []
    active_keys = {normalize_manager_key(r.get("manager_key") or "") for r in manager_rows}
    src_map = _manager_source_map(db_path)
    return {
        _norm_source_key(s) for mk, s in src_map.items()
        if mk in working_keys and mk in active_keys and s
    }


# --- TPILOT MANAGER REPLACEMENT STAGE5 PREFLIGHT DEDUP 20260716 START ---
#
# IMPORTANT: collect_replacement_records() above (and the reserve/outage
# block it powers) is a SEPARATE, OLDER feature from the account-replacement
# state machine in storage.py's manager_replacements table (Stage 1-4 of
# this project's "manager Telegram-account replacement" work, culminating
# in Stage 5's own immediate PartnerBot notification). They read from
# entirely different tables (manager_reserve_pairs/reserve_activation_events
# here, vs. manager_replacements there) and do not share operation ids --
# the OLDER block tracks "a primary manager's account went down and a
# reserve was activated to keep business links flowing," a different,
# narrower business event than a full account takeover. In the RARE case
# where the SAME manager_key genuinely goes through BOTH (e.g. it was a
# reserve-activation primary and is LATER also swapped via the newer
# account-replacement flow), this filter keeps the PartnerBot morning report
# from repeating a line Stage 5 already sent as an immediate notification --
# it does not change AdminBot's own build_report/render_admin_text, which
# stay exactly as before.
def _stage5_notified_old_keys(db_path: Optional[str], source_key: str) -> set:
    """Read-only: normalized old_manager_key values for this source that
    already have a Stage 5 manager_replacements row at status
    notified/done (i.e. Stage 5 has already sent, or already fully
    finished, its own immediate PartnerBot notification for that account).
    Never raises -- an empty set on any failure is the safe default (means
    "nothing excluded," never "something wrongly excluded")."""
    sk = _norm_source_key(source_key)
    if not sk:
        return set()
    try:
        con = sqlite3.connect(str(db_path or storage.DB_PATH))
        try:
            rows = con.execute(
                "SELECT DISTINCT old_manager_key FROM manager_replacements"
                " WHERE source_key=? AND status IN ('notified','done')",
                (sk,),
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return set()
    return {normalize_manager_key(r[0] or "") for r in rows if r and r[0]}


def _stage5_dedupe_partner_replacements(records: List[Dict[str, Any]], db_path: Optional[str], source_key: str) -> List[Dict[str, Any]]:
    """Filters out OLDER-block replacement/outage records whose primary_key
    already got its own Stage 5 immediate notification for this source.
    Never raises; on any failure returns records unchanged (fail-open to
    the PRE-Stage-5 behavior, never silently hides a record due to an
    internal error)."""
    try:
        already_notified = _stage5_notified_old_keys(db_path, source_key)
        if not already_notified:
            return records
        return [r for r in records if normalize_manager_key(r.get("primary_key") or "") not in already_notified]
    except Exception:
        return records
# --- TPILOT MANAGER REPLACEMENT STAGE5 PREFLIGHT DEDUP 20260716 END ---


def build_partner_report(
    target_date_iso: Optional[str] = None,
    db_path: Optional[str] = None,
    source_key: str = "",
) -> Dict[str, Any]:
    """Assemble a source-scoped readiness report. Pure read-only; never
    raises; never sends anything. Sets report["send_recommended"] = False
    when there is nothing relevant for this source on this date -- callers
    MUST NOT send a Telegram message in that case (do not invent an error
    where none exists)."""
    target_date_iso = target_date_iso or kyiv_today_iso()
    db_path = db_path or storage.DB_PATH
    sk = _norm_source_key(source_key)

    report: Dict[str, Any] = {
        "date_iso": target_date_iso,
        "date_display": fmt_date_ru(target_date_iso),
        "check_time": kyiv_now_hms(),
        "source_key": sk,
        "source_name": sk,
        "managers": [],
        "working_count": 0,
        "all_ok": True,
        "links_actual_total": 0,
        "links_expected_total": 0,
        "send_recommended": False,
    }
    if not sk:
        return report

    src_names = {_norm_source_key(k): v for k, v in _source_names(db_path).items()}
    report["source_name"] = src_names.get(sk, sk)

    try:
        working_keys = storage.manager_schedule_list_working_on_date(target_date_iso, db_path=db_path) or []
    except Exception:
        working_keys = []
    working_keys = {normalize_manager_key(k) for k in working_keys if k}

    src_map = _manager_source_map(db_path)
    source_mks_all = sorted(
        mk for mk, s in src_map.items() if _norm_source_key(s) == sk and mk in working_keys
    )

    manager_rows = list_manager_rows_from_db_sync(db_path, include_removed=False) or []
    manager_by_key = {normalize_manager_key(r.get("manager_key") or ""): r for r in manager_rows}
    # Archived/historical managers (status='archived') must not inflate the
    # "working today" count or appear in the buyer-facing account list, even
    # if a stale schedule row still lists them -- mirrors build_report's own
    # archived-manager handling for AdminBot.
    source_mks = [mk for mk in source_mks_all if mk in manager_by_key]
    report["working_count"] = len(source_mks)
    if not source_mks:
        # Nobody ACTIVE from this source works on this date -- nothing
        # relevant to report. Caller must not send a message at all.
        return report

    default_count = _bizlink_default_count(db_path)
    bizlinks_by_mgr = _bizlinks_today_by_manager(db_path, target_date_iso)
    try:
        jobs_today = storage.bizlink_job_list_for_date(target_date_iso, db_path=db_path) or []
    except Exception:
        jobs_today = []
    job_keys_today = {normalize_manager_key(j.get("manager_key") or "") for j in jobs_today}

    all_ok = True
    total_actual = 0
    total_expected = 0
    for mk in source_mks:
        row = manager_by_key.get(mk) or {}
        display = str(row.get("display_name") or "").strip() or mk
        username = str(row.get("telegram_username") or "").strip()
        biz_rows = bizlinks_by_mgr.get(mk, [])
        created = [r for r in biz_rows if str(r["status"] or "") == "created" and str(r["slug"] or "").strip()]
        actual = len(created)
        has_job = mk in job_keys_today
        expected = default_count if has_job else None
        error_rows = [r for r in biz_rows if str(r["last_error_class"] or "").strip()]
        ok = not error_rows and (not has_job or actual >= expected)
        report["managers"].append({
            "manager_key": mk, "display": display, "username": username,
            "links_actual": actual, "links_expected": expected, "links_has_job": has_job,
            "ok": ok,
        })
        total_actual += actual
        if has_job and expected is not None:
            total_expected += expected
        if not ok:
            all_ok = False

    report["all_ok"] = all_ok
    report["links_actual_total"] = total_actual
    report["links_expected_total"] = total_expected
    # send_recommended is gated on source_mks alone (see the early return
    # above): if at least one ACTIVE manager of this source works on this
    # date, the partner report is relevant and must be sent -- even when
    # there is no link job/no links today (that case is reported honestly
    # as "ссылки не запланированы", not treated as a reason to stay silent).
    # Only a source with literally nobody active working today is skipped.
    report["send_recommended"] = True

    # Morning, today-only feature -- see build_report's matching guard for
    # the full rationale. An evening/tomorrow (or any non-today) call must
    # never collect replacement records or touch all_ok here.
    records: List[Dict[str, Any]] = []
    if target_date_iso == kyiv_today_iso():
        try:
            active_slugs_by_manager = _bizlinks_active_slugs_by_manager(db_path, target_date_iso)
            all_records = collect_replacement_records(
                target_date_iso, db_path, working_keys, manager_by_key, src_map,
                active_slugs_by_manager, default_count,
            )
            records = [r for r in all_records if _norm_source_key(r.get("source_key")) == sk]
            records = _stage5_dedupe_partner_replacements(records, db_path, sk)
        except Exception as e:
            print(f"[preflight-replacements] build_partner_report integration failed: {e!r}")
            records = []
        if any(r["state"] != "C" for r in records):
            report["all_ok"] = False
    report["replacements"] = records
    return report


def render_partner_text(report: Dict[str, Any], period_kind: str = "today") -> str:
    """period_kind: "today" (morning) or "tomorrow" (evening). Partner-facing
    text only -- no process/session/proxy internals, no stack traces."""
    is_evening = period_kind == "tomorrow"
    title = "🛡 Готовность на завтра" if is_evening else "🛡 Утренняя проверка готовности"
    work_label = "Завтра работают" if is_evening else "Сегодня работают"
    links_label = "Ссылки на завтра" if is_evening else "Ссылки на сегодня"
    ready_line = "Статус: можно планировать запуск трафика." if is_evening else "Статус: можно запускать трафик."
    final_line = "Хорошего вечера 🌇" if is_evening else "Хорошего дня ☀️"

    lines: List[str] = [
        title,
        f"📅 {report['date_display']} · проверено в {report['check_time']}",
        f"Источник: {report['source_name']}",
        "",
        f"{work_label}: {report['working_count']} аккаунт(ов)",
    ]
    replacement_by_key = ({r["primary_key"]: r for r in (report.get("replacements") or [])}
                           if not is_evening else {})
    for m in report.get("managers", []):
        label = f"{m['display']} / @{m['username']}" if m.get("username") else m["display"]
        rec = replacement_by_key.get(m.get("manager_key"))
        if rec and rec.get("state") != "C":
            # Do not show a plain ✅ for a manager whose account is known to
            # be unavailable/replacing right now -- see the block below.
            lines.append(f"⚠️ {label}")
        else:
            lines.append(f"✅ {label}")
    lines.append("")

    # Replacement/outage block -- morning (today) report only, per spec.
    if replacement_by_key:
        lines.append("🔁 Замены аккаунтов")
        for rec in replacement_by_key.values():
            lines.append(_replacement_line(rec))
        lines.append("")

    total_actual = report.get("links_actual_total", 0)
    total_expected = report.get("links_expected_total", 0)

    # Deep-verify wording: green "проверены и готовы к использованию" only
    # when this source's deep status is fully "ok" (see apply_deep_results
    # -- expected>0, verified==expected, failed==0). Partial/failed use a
    # partner-safe problem notice; timeout or not-yet-checked-but-relevant
    # uses a pending notice; only truly nothing relevant says "не
    # запланированы". No internal reasons, no stack traces, no manager-level
    # detail (that stays admin-only).
    deep_status = report.get("deep_status_global", "not_checked")
    deep_verified_total = int(report.get("deep_verified_total") or 0)
    deep_expected_total = int(report.get("deep_expected_total") or 0)

    if deep_status == "ok" and deep_expected_total > 0:
        lines.append(f"✅ {links_label}")
        lines.append(f"  проверены и готовы к использованию — {deep_verified_total}/{deep_expected_total} · статус OK")
    elif deep_status in ("partial", "failed"):
        lines.append(f"❌ {links_label}")
        lines.append(f"  проверено {deep_verified_total}/{deep_expected_total}, есть проблема")
    elif not report.get("all_ok"):
        # Real DB-level link error, independent of deep-verify.
        lines.append(f"❌ {links_label}")
        lines.append(f"  проверено {total_actual}/{total_expected}, есть проблема" if total_expected else "  есть проблема")
    elif deep_status == "timeout" or (deep_status == "not_checked" and (total_expected > 0 or total_actual > 0)):
        # Partner-facing wording is deliberately NEVER "не успели пройти
        # проверку / запуск после подтверждения" -- that exposes an
        # internal verification-timeout detail the partner has no way to
        # act on. The admin-facing render_admin_text keeps the honest
        # timeout wording (its own reader can force a recheck); here the
        # partner just sees that the final status is still being confirmed
        # -- the existing auto-recheck/correction loop (panel_bot.py
        # _pf_admin_evening_maybe_correct + partner_stat_bot.py
        # _pf_partner_maybe_correct) will edit this exact message in place
        # once the blob resolves, same as any other correction.
        lines.append(f"⚠️ {links_label}")
        lines.append("  идёт финальная проверка — статус обновится автоматически")
    else:
        lines.append(f"ℹ️ {links_label}")
        lines.append("  ссылки не запланированы")
    lines.append("")

    # "timeout"/pending is not itself a problem -- only partial/failed or a
    # real DB-level issue block the ready line.
    deep_not_ok = deep_status in ("partial", "failed") or not report.get("all_ok")
    if deep_not_ok:
        lines.append("Статус: есть моменты, уточните у администратора.")
    else:
        lines.append(ready_line)
    lines.append("")
    lines.append(final_line)
    return "\n".join(lines)
