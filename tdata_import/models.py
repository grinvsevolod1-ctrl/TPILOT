# -*- coding: utf-8 -*-
"""Pure data model for the tdata/session import operation.

Stdlib only. No telethon, no main.py, no I/O. Values are plain strings to match
the project's "string-in-DB" convention (statuses/stages are stored verbatim in
the tdata_import_ops table). Mirror of the storage-layer constants in
storage.py -- keep the two in sync.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet, Optional, Tuple


class Status:
    """Coarse lifecycle of an import operation (tdata_import_ops.status)."""

    CREATED = "created"
    PROCESSING = "processing"
    DONE = "done"
    ERROR = "error"
    CANCELLED = "cancelled"

    ACTIVE: FrozenSet[str] = frozenset({CREATED, PROCESSING})
    TERMINAL: FrozenSet[str] = frozenset({DONE, ERROR, CANCELLED})


class Stage:
    """Fine-grained pipeline stage (tdata_import_ops.stage). Forward-only."""

    CREATED = "created"
    MANAGER_PREPARED = "manager_prepared"
    SOURCE_ASSIGNED = "source_assigned"
    PROXY_ASSIGNED = "proxy_assigned"
    PROXY_VERIFIED = "proxy_verified"
    ARCHIVE_UPLOADED = "archive_uploaded"
    ARCHIVE_VALIDATED = "archive_validated"
    SESSION_DETECTED = "session_detected"
    SESSION_SELECTED = "session_selected"      # ready .session chosen
    TDATA_CONVERTED = "tdata_converted"        # produced from tdata fallback
    SESSION_VALIDATED = "session_validated"
    IDENTITY_CHECKING = "identity_checking"
    IDENTITY_VERIFIED = "identity_verified"
    SESSION_INSTALLING = "session_installing"
    SESSION_INSTALLED = "session_installed"
    RUNTIME_STARTING = "runtime_starting"
    RUNTIME_RUNNING = "runtime_running"
    COMPLETED = "completed"

    ORDER: Tuple[str, ...] = (
        CREATED, MANAGER_PREPARED, SOURCE_ASSIGNED, PROXY_ASSIGNED, PROXY_VERIFIED,
        ARCHIVE_UPLOADED, ARCHIVE_VALIDATED, SESSION_DETECTED, SESSION_SELECTED,
        TDATA_CONVERTED, SESSION_VALIDATED, IDENTITY_CHECKING, IDENTITY_VERIFIED,
        SESSION_INSTALLING, SESSION_INSTALLED, RUNTIME_STARTING, RUNTIME_RUNNING,
        COMPLETED,
    )

    # Forward-only edges. session_detected forks to either the ready-session
    # path or the tdata-conversion path, which re-merge at session_validated.
    TRANSITIONS = {
        CREATED: (MANAGER_PREPARED,),
        MANAGER_PREPARED: (SOURCE_ASSIGNED,),
        SOURCE_ASSIGNED: (PROXY_ASSIGNED,),
        PROXY_ASSIGNED: (PROXY_VERIFIED,),
        PROXY_VERIFIED: (ARCHIVE_UPLOADED,),
        ARCHIVE_UPLOADED: (ARCHIVE_VALIDATED,),
        ARCHIVE_VALIDATED: (SESSION_DETECTED,),
        SESSION_DETECTED: (SESSION_SELECTED, TDATA_CONVERTED),
        SESSION_SELECTED: (SESSION_VALIDATED,),
        TDATA_CONVERTED: (SESSION_VALIDATED,),
        SESSION_VALIDATED: (IDENTITY_CHECKING,),
        IDENTITY_CHECKING: (IDENTITY_VERIFIED,),
        IDENTITY_VERIFIED: (SESSION_INSTALLING,),
        SESSION_INSTALLING: (SESSION_INSTALLED,),
        SESSION_INSTALLED: (RUNTIME_STARTING,),
        RUNTIME_STARTING: (RUNTIME_RUNNING,),
        RUNTIME_RUNNING: (COMPLETED,),
        COMPLETED: (),
    }

    @classmethod
    def can_transition(cls, from_stage: str, to_stage: str) -> bool:
        return str(to_stage) in cls.TRANSITIONS.get(str(from_stage), ())


class ImportMethod:
    READY_SESSION = "ready_session"
    TDATA_CONVERTED = "tdata_converted"


class FailureClass:
    """Safe error classes recorded in tdata_import_ops.error_class."""

    INVALID_ARCHIVE = "invalid_archive"
    UNSAFE_ARCHIVE = "unsafe_archive"
    UNSUPPORTED_PACKAGE = "unsupported_package"
    MULTIPLE_ACCOUNTS = "multiple_accounts"
    SESSION_CORRUPT = "session_corrupt"
    SESSION_SCHEMA_UNSUPPORTED = "session_schema_unsupported"
    TDATA_CONVERSION_FAILED = "tdata_conversion_failed"
    TDATA_PASSCODE_REQUIRED = "tdata_passcode_required"
    SESSION_UNAUTHORIZED = "session_unauthorized"
    PROXY_MISSING = "proxy_missing"
    PROXY_UNAVAILABLE = "proxy_unavailable"
    DIRECT_CONNECTION_BLOCKED = "direct_connection_blocked"
    IDENTITY_MISMATCH = "identity_mismatch"
    DUPLICATE_TELEGRAM_ACCOUNT = "duplicate_telegram_account"
    SESSION_INSTALL_FAILED = "session_install_failed"
    RUNTIME_FAILED = "runtime_failed"
    CLEANUP_FAILED = "cleanup_failed"
    STALE_OPERATION = "stale_operation"
    CONCURRENT_OPERATION = "concurrent_operation"

    ALL: FrozenSet[str] = frozenset({
        INVALID_ARCHIVE, UNSAFE_ARCHIVE, UNSUPPORTED_PACKAGE, MULTIPLE_ACCOUNTS,
        SESSION_CORRUPT, SESSION_SCHEMA_UNSUPPORTED, TDATA_CONVERSION_FAILED,
        TDATA_PASSCODE_REQUIRED, SESSION_UNAUTHORIZED, PROXY_MISSING,
        PROXY_UNAVAILABLE, DIRECT_CONNECTION_BLOCKED, IDENTITY_MISMATCH,
        DUPLICATE_TELEGRAM_ACCOUNT, SESSION_INSTALL_FAILED, RUNTIME_FAILED,
        CLEANUP_FAILED, STALE_OPERATION, CONCURRENT_OPERATION,
    })


@dataclass(frozen=True)
class ArchiveLimits:
    """Default safety limits for archive intake/extraction (Phase 2 uses these).

    Kept here so both the intake handler and the extractor share one source of
    truth; values match the approved plan.
    """

    max_zip_bytes: int = 50 * 1024 * 1024            # 50 MB uploaded ZIP
    max_total_extracted_bytes: int = 200 * 1024 * 1024   # 200 MB soft
    hard_total_extracted_bytes: int = 400 * 1024 * 1024  # 400 MB hard
    max_compression_ratio: float = 100.0
    max_file_count: int = 2000
    max_path_depth: int = 16
    extract_timeout_sec: int = 30
    allow_nested_archives: bool = False


@dataclass
class SafeMeta:
    """The ONLY identity-related data allowed to be persisted / surfaced.

    Never holds an auth_key, session blob, 2FA/passcode, API hash, full phone
    or proxy password. `masked_phone` is already masked by the caller.
    """

    tg_user_id: Optional[int] = None
    username: str = ""
    display_name: str = ""
    masked_phone: str = ""
    import_method: str = ""
    proxy_ref: str = ""
    proxy_verified_ip: str = ""
    source_key: str = ""


@dataclass
class SessionCandidate:
    """A .session file discovered/produced for the flow (Phase 2/3/4)."""

    path: str = ""
    origin: str = ""              # "ready" | "tdata_converted"
    valid: bool = False
    schema_version: Optional[int] = None
    dc_id: Optional[int] = None
    server_address: str = ""
    port: Optional[int] = None
    auth_key_len: int = 0
    reason: str = ""              # safe classification, never a secret
    failure_class: str = ""       # one of FailureClass when valid is False


@dataclass
class ArchiveInventory:
    """Result of detector inventory over an extracted archive (Phase 2)."""

    session_candidates: list = field(default_factory=list)
    tdata_dirs: list = field(default_factory=list)
    json_meta_files: list = field(default_factory=list)
    accounts_txt: list = field(default_factory=list)
    other_files: list = field(default_factory=list)
