# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from typing import Any, Dict, Optional


def _now_iso() -> str:
    return datetime.utcnow().replace(microsecond=0).isoformat()


def _connect(db_path: str) -> sqlite3.Connection:
    parent = os.path.dirname(os.path.abspath(db_path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    con = sqlite3.connect(db_path, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL;")
    except Exception:
        pass
    return con


def ensure_panel_tables(db_path: str) -> None:
    con = _connect(db_path)
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS panel_commands(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                command_text TEXT NOT NULL DEFAULT '',
                requested_by INTEGER,
                source_chat_id INTEGER,
                response_chat_id INTEGER,
                status TEXT NOT NULL DEFAULT 'new',
                result_text TEXT DEFAULT '',
                result_file TEXT DEFAULT '',
                result_caption TEXT DEFAULT '',
                error_text TEXT DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '',
                taken_at TEXT DEFAULT '',
                finished_at TEXT DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT ''
            );
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS panel_commands_status_idx ON panel_commands(status, id);")
        con.execute("CREATE INDEX IF NOT EXISTS panel_commands_created_idx ON panel_commands(created_at);")
        con.commit()
    finally:
        con.close()


def submit_panel_command(db_path: str, command_text: str, *, requested_by: int = 0, source_chat_id: int = 0, response_chat_id: int = 0) -> int:
    ensure_panel_tables(db_path)
    now = _now_iso()
    con = _connect(db_path)
    try:
        cur = con.execute(
            """
            INSERT INTO panel_commands(command_text, requested_by, source_chat_id, response_chat_id, status, created_at, updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (str(command_text or '').strip(), int(requested_by or 0), int(source_chat_id or 0), int(response_chat_id or source_chat_id or 0), 'new', now, now),
        )
        con.commit()
        return int(cur.lastrowid)
    finally:
        con.close()


def get_panel_command(db_path: str, command_id: int) -> Optional[Dict[str, Any]]:
    ensure_panel_tables(db_path)
    con = _connect(db_path)
    try:
        cur = con.execute("SELECT * FROM panel_commands WHERE id=?", (int(command_id),))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def take_next_panel_command(db_path: str) -> Optional[Dict[str, Any]]:
    ensure_panel_tables(db_path)
    con = _connect(db_path)
    try:
        con.execute("BEGIN IMMEDIATE")
        cur = con.execute("SELECT * FROM panel_commands WHERE status='new' ORDER BY id ASC LIMIT 1")
        row = cur.fetchone()
        if not row:
            con.commit()
            return None
        now = _now_iso()
        con.execute("UPDATE panel_commands SET status='running', taken_at=?, updated_at=? WHERE id=?", (now, now, int(row['id'])))
        con.commit()
        return get_panel_command(db_path, int(row['id']))
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        con.close()


def finish_panel_command(db_path: str, command_id: int, *, ok: bool, result_text: str = '', result_file: str = '', result_caption: str = '', error_text: str = '') -> None:
    ensure_panel_tables(db_path)
    now = _now_iso()
    status = 'done' if ok else 'error'
    con = _connect(db_path)
    try:
        con.execute(
            """
            UPDATE panel_commands
            SET status=?, result_text=?, result_file=?, result_caption=?, error_text=?, finished_at=?, updated_at=?
            WHERE id=?
            """,
            (status, str(result_text or ''), str(result_file or ''), str(result_caption or ''), str(error_text or ''), now, now, int(command_id)),
        )
        con.commit()
    finally:
        con.close()


def scrub_panel_command_result(db_path: str, command_id: int, *, placeholder: str = '[QR_TOKEN_SCRUBBED]') -> bool:
    """Overwrite result_text for a single panel_commands row after the caller has already
    consumed it (e.g. PanelBot parsed a one-time QR login URL out of it). Scoped strictly to
    one row id and only touches result_text — never command_text/error_text/status.

    Returns True if a row was updated, False otherwise (e.g. id not found).
    """
    ensure_panel_tables(db_path)
    now = _now_iso()
    con = _connect(db_path)
    try:
        cur = con.execute(
            "UPDATE panel_commands SET result_text=?, updated_at=? WHERE id=?",
            (str(placeholder or ''), now, int(command_id)),
        )
        con.commit()
        return bool(cur.rowcount)
    finally:
        con.close()


def cleanup_panel_commands(db_path: str, *, keep_hours: int = 48) -> None:
    ensure_panel_tables(db_path)
    # ISO strings are UTC naive and lexicographically sortable enough here.
    import datetime as _dt
    cutoff = (_dt.datetime.utcnow() - _dt.timedelta(hours=int(keep_hours or 48))).replace(microsecond=0).isoformat()
    con = _connect(db_path)
    try:
        con.execute("DELETE FROM panel_commands WHERE created_at<? AND status IN ('done','error')", (cutoff,))
        con.commit()
    finally:
        con.close()


def reap_stale_running_panel_commands(db_path: str, *, stale_minutes: int = 60) -> int:
    """Mark panel_commands stuck in 'running' longer than stale_minutes as 'error'.

    Prevents zombie rows from accumulating after controller restarts or crashes.
    Does NOT touch 'new' rows — those remain queued for the next controller pickup.
    Returns the number of rows reaped.
    """
    ensure_panel_tables(db_path)
    import datetime as _dt
    # Use taken_at as the activity timestamp; fall back to created_at if empty or missing.
    # Both columns default to '' (empty string), not NULL, so NULLIF is required to make
    # COALESCE treat empty strings as absent and fall back correctly.
    cutoff = (
        _dt.datetime.utcnow() - _dt.timedelta(minutes=max(10, int(stale_minutes or 60)))
    ).replace(microsecond=0).isoformat()
    now = _now_iso()
    con = _connect(db_path)
    try:
        cur = con.execute(
            """
            UPDATE panel_commands
               SET status='error',
                   error_text='reaped: stuck in running state',
                   finished_at=?,
                   updated_at=?
             WHERE status='running'
               AND COALESCE(NULLIF(taken_at, ''), NULLIF(created_at, '')) < ?
            """,
            (now, now, cutoff),
        )
        con.commit()
        try:
            return int(cur.rowcount or 0)
        except Exception:
            return 0
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        return 0
    finally:
        con.close()
