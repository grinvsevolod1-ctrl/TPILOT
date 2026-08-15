# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / '.env.TPilot'

# TPILOT RESTART ISOLATION 20260809: same-directory import (no package install, no
# heavy side effects at import time -- manager_registry.py only defines functions/
# constants and guards its CLI behind __main__) for the manager_scoped/unknown
# start-failure taxonomy, reused here instead of re-declaring it.
sys.path.insert(0, str(BASE_DIR))
import manager_registry  # noqa: E402


def _load_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text(encoding='utf-8', errors='ignore').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        k, v = line.split('=', 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


ENV = _load_env(ENV_PATH)


def _env_bool(name: str, default: bool = False) -> bool:
    v = str(ENV.get(name, '')).strip().lower()
    if not v:
        return default
    return v in ('1', 'true', 'yes', 'on', 'да')


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(ENV.get(name, '')).strip() or default)
    except Exception:
        return int(default)


WATCHDOG_ENABLED = _env_bool('WATCHDOG_ENABLED', True)
PING_URL = str(ENV.get('WATCHDOG_PING_URL', '')).strip()
FAIL_URL = str(ENV.get('WATCHDOG_FAIL_URL', '')).strip()
INTERVAL_SEC = max(20, _env_int('WATCHDOG_INTERVAL_SEC', 60))
REQUIRE_TPILOT = _env_bool('WATCHDOG_REQUIRE_TPILOT', True)
REQUIRE_MANAGERS = _env_bool('WATCHDOG_REQUIRE_MANAGERS', True)
REQUIRE_PANEL = _env_bool('WATCHDOG_REQUIRE_PANEL', False)
# R1B/F-37 (plan section 23): before this, presence was only checked for
# main.py and panel_bot.py -- ManagerBot/PartnerBot could exit (Telethon
# exhausts its default reconnect attempts, run_until_disconnected() returns,
# the process falls off the end and exits -- see main.py's own manager
# runtime for the identical shape) and nobody would notice. Defaulting to
# True (unlike REQUIRE_PANEL's opt-in False) is deliberate: this closes the
# exact blind spot F-37 documents, and this module never restarts or kills
# anything (confirmed: no subprocess.Popen/terminate/kill calls anywhere in
# this file) -- the only effect of a positive default is an extra line in
# `reasons`/an extra ping-fail if one of these two processes is genuinely
# down, never a restart storm.
REQUIRE_MANAGERBOT = _env_bool('WATCHDOG_REQUIRE_MANAGERBOT', True)
REQUIRE_PARTNERBOT = _env_bool('WATCHDOG_REQUIRE_PARTNERBOT', True)
WATCHDOG_NAME = str(ENV.get('WATCHDOG_NAME', 'ALM_TPilot')).strip() or 'ALM_TPilot'
LOG_FILE = BASE_DIR / str(ENV.get('WATCHDOG_LOG_FILE', 'logs/soft_watchdog.log')).strip()
STATUS_FILE = BASE_DIR / str(ENV.get('WATCHDOG_STATUS_FILE', 'runtime/soft_status.json')).strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _log(msg: str) -> None:
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open('a', encoding='utf-8') as f:
            f.write(f'{_now_iso()} {msg}\n')
    except Exception:
        pass


def _write_status(payload: dict) -> None:
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATUS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass


def _norm_path(s: str) -> str:
    return str(s or '').replace('\\', '/').lower()


def _get_python_processes() -> List[Dict[str, str]]:
    if sys.platform.startswith('win'):
        ps = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.Name -like 'python*' -and $_.CommandLine } | "
            "Select-Object ProcessId,ParentProcessId,Name,CommandLine | "
            "ConvertTo-Json -Compress"
        )
        try:
            cp = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', ps],
                                cwd=str(BASE_DIR), capture_output=True, text=True, timeout=15)
            raw = (cp.stdout or '').strip()
            if not raw:
                return []
            data = json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            out = []
            for row in data or []:
                out.append({
                    'pid': str(row.get('ProcessId', '')),
                    'ppid': str(row.get('ParentProcessId', '')),
                    'name': str(row.get('Name', '')),
                    'cmd': str(row.get('CommandLine', '')),
                })
            return out
        except Exception as e:
            _log(f'process_list_error={e!r}')
            return []
    try:
        cp = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=15)
        return [{'pid': '', 'ppid': '', 'name': 'python', 'cmd': line} for line in (cp.stdout or '').splitlines() if 'python' in line]
    except Exception as e:
        _log(f'process_list_error={e!r}')
        return []


def _list_active_manager_keys() -> List[str]:
    py = BASE_DIR / 'venv' / 'Scripts' / 'python.exe'
    if not py.exists():
        py = Path(sys.executable)
    reg = BASE_DIR / 'manager_registry.py'
    if not reg.exists():
        return []
    try:
        cp = subprocess.run([str(py), str(reg), '--env', str(ENV_PATH), 'list-active-keys'],
                            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=20)
        return [line.strip().lower() for line in (cp.stdout or '').splitlines() if line.strip()]
    except Exception as e:
        _log(f'manager_registry_error={e!r}')
        return []


def _collapse_venv_children(procs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Remove Windows venv-launcher child processes from a list of relevant procs.

    On Windows, running venv/Scripts/python.exe spawns a real Python child
    (e.g. Python312/python.exe) with the same CommandLine.  Both entries appear
    in Win32_Process and both contain the TPilot base path, so raw counting
    reports double the actual logical instances.

    The child's ParentProcessId (ppid) points to the venv launcher parent whose
    PID is also in the relevant set.  We keep only the parent (logical root) and
    drop the child.

    Rule: if a process's ppid is in the set of relevant PIDs, it is a child of
    another TPilot-relevant Python process → drop it.

    On non-Windows the function is a no-op (ppid is always '' from ps aux).
    """
    if not sys.platform.startswith('win'):
        return procs
    relevant_pids = {str(p.get('pid') or '') for p in procs if p.get('pid')}
    result = []
    for p in procs:
        ppid = str(p.get('ppid') or '')
        if ppid and ppid in relevant_pids:
            # Parent is also a TPilot-relevant process: this is the venv child.
            # Drop it so each logical instance is counted exactly once.
            continue
        result.append(p)
    return result


def _check_soft() -> Tuple[bool, List[str], dict]:
    procs = _get_python_processes()
    base_norm = _norm_path(str(BASE_DIR))

    # Filter to TPilot-relevant processes first, then collapse venv parent/child pairs
    # so each logical instance is counted exactly once regardless of the Python launcher.
    relevant_procs = [p for p in procs if base_norm in _norm_path(p.get('cmd', ''))]
    relevant_procs = _collapse_venv_children(relevant_procs)

    main_procs = []
    panel_procs = []
    managerbot_procs = []
    partnerbot_procs = []
    manager_procs_by_key: Dict[str, int] = {}

    for p in relevant_procs:
        cmd = _norm_path(p.get('cmd', ''))
        if 'main.py' in cmd:
            main_procs.append(p)
            m = re.search(r'--manager\s+([a-z0-9_-]+)', cmd, flags=re.I)
            if m:
                key = m.group(1).lower()
                manager_procs_by_key[key] = manager_procs_by_key.get(key, 0) + 1
        if 'panel_bot.py' in cmd:
            panel_procs.append(p)
        # R1B/F-37: presence-only, same shape as the panel_bot.py check above.
        if 'manager_bot.py' in cmd:
            managerbot_procs.append(p)
        if 'partner_stat_bot.py' in cmd:
            partnerbot_procs.append(p)

    reasons: List[str] = []
    if REQUIRE_TPILOT:
        tpilot = [p for p in main_procs if '--manager' not in _norm_path(p.get('cmd', ''))]
        if not tpilot:
            reasons.append('TPilot process not found')
        elif len(tpilot) > 1:
            # Report-only: duplicate controller processes are dangerous but should not
            # suppress the healthcheck ping (the system is still partly running).
            # Logged separately so operators can investigate without alert fatigue.
            _log(f'WARNING: {len(tpilot)} controller (main.py without --manager) processes found')

    active_keys = _list_active_manager_keys() if REQUIRE_MANAGERS else []

    # M2.12A + TPILOT RESTART ISOLATION 20260809: read the per-manager down reason
    # from start_status.json BEFORE deciding what goes into `reasons`, so a
    # manager-scoped failure (a reason_class start_manager.ps1 already isolates at
    # startup, or one a manager later died on by itself) can be routed to
    # `quarantined_managers` instead of `reasons` -- otherwise this watchdog reports
    # ok=False for the ENTIRE TPilot deployment forever because of a single already
    # -known, already-isolated manager problem (plan section H3 / F1). Prefer the
    # sticky last_exit_reason_class (main.py HNV2 D1, survives respawns) over the
    # plain reason_class, which main.py blanks on every non-'exited' status write and
    # so can race with a respawn attempt mid-cycle.
    manager_down_reasons: Dict[str, str] = {}
    manager_down_classification: Dict[str, str] = {}
    if REQUIRE_MANAGERS:
        for k in active_keys:
            if manager_procs_by_key.get(k, 0) <= 0:
                try:
                    ss_path = BASE_DIR / 'runtime' / 'managers' / k / 'start_status.json'
                    if ss_path.is_file():
                        ss = json.loads(ss_path.read_text(encoding='utf-8')) or {}
                        rc = str(ss.get('last_exit_reason_class') or ss.get('reason_class') or '').strip()
                        err = str(ss.get('last_exit_error') or ss.get('error') or '').strip()[:120]
                        if rc or err:
                            manager_down_reasons[k] = f'{rc}: {err}' if err else rc
                        manager_down_classification[k] = manager_registry.classify_start_failure(rc)
                except Exception:
                    pass

    quarantined_managers: List[str] = []
    if REQUIRE_MANAGERS:
        for k in active_keys:
            if manager_procs_by_key.get(k, 0) <= 0:
                if manager_down_classification.get(k) == 'manager_scoped':
                    quarantined_managers.append(k)
                else:
                    reasons.append(f'manager process not found: {k}')

    if REQUIRE_PANEL and not panel_procs:
        reasons.append('PanelBot process not found')

    if REQUIRE_MANAGERBOT and not managerbot_procs:
        reasons.append('ManagerBot process not found')

    if REQUIRE_PARTNERBOT and not partnerbot_procs:
        reasons.append('PartnerBot process not found')

    # Duplicate manager process detection — report-only, never modifies 'reasons'.
    # A count > 1 means two processes share the same --manager key and same DB,
    # which causes duplicate profile parsing, double sends, and conflicting DB writes.
    duplicate_warnings: List[str] = []
    for k, count in manager_procs_by_key.items():
        if count > 1:
            msg = f'duplicate manager process: key={k} count={count}'
            duplicate_warnings.append(msg)
            _log(f'WARNING: {msg}')

    meta = {
        'name': WATCHDOG_NAME,
        'time_utc': _now_iso(),
        'ok': not reasons,
        'reasons': reasons,
        'duplicate_warnings': duplicate_warnings,
        'active_managers': active_keys,
        'main_processes': len(main_procs),
        'manager_processes': manager_procs_by_key,
        'panel_processes': len(panel_procs),
        'managerbot_processes': len(managerbot_procs),
        'partnerbot_processes': len(partnerbot_procs),
        'manager_down_reasons': manager_down_reasons,
        # TPILOT RESTART ISOLATION 20260809: managers down for a reason already
        # classified manager_scoped -- excluded from `reasons`/`ok` on purpose (see
        # the comment above where this list is built). Still fully visible here for
        # operators/AdminBot; only the external ok/reasons signal is suppressed.
        'quarantined_managers': quarantined_managers,
    }
    return (not reasons), reasons, meta


def _http_ping(url: str, body: str = '') -> bool:
    if not url:
        return False
    try:
        data = body.encode('utf-8') if body else None
        req = urllib.request.Request(
            url,
            data=data,
            method='POST' if data else 'GET',
            headers={'User-Agent': 'ALM_TPilot watchdog', 'Content-Type': 'text/plain; charset=utf-8'},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            code = int(getattr(resp, 'status', 0) or 0)
            return 200 <= code < 300
    except Exception as e:
        _log(f'http_ping_error url={url[:60]} err={e!r}')
        return False


def main() -> int:
    if not WATCHDOG_ENABLED:
        _log('watchdog disabled by WATCHDOG_ENABLED=0')
        return 0

    _log(f'watchdog started name={WATCHDOG_NAME} interval={INTERVAL_SEC}s ping_url_set={bool(PING_URL)} fail_url_set={bool(FAIL_URL)}')
    last_state = None
    fail_sent_for_state = False

    while True:
        ok, reasons, meta = _check_soft()
        _write_status(meta)

        if ok:
            fail_sent_for_state = False
            if last_state is not True:
                _log('state=OK')
            if PING_URL:
                _http_ping(PING_URL, f'OK {WATCHDOG_NAME} {json.dumps(meta, ensure_ascii=False)}')
        else:
            if last_state is not False:
                _log('state=FAIL reasons=' + '; '.join(reasons))
            if FAIL_URL and not fail_sent_for_state:
                sent = _http_ping(FAIL_URL, 'FAIL ' + WATCHDOG_NAME + ' | ' + '; '.join(reasons))
                fail_sent_for_state = bool(sent)

        last_state = bool(ok)
        time.sleep(INTERVAL_SEC)


if __name__ == '__main__':
    raise SystemExit(main())
