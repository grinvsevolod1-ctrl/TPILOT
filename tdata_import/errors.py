# -*- coding: utf-8 -*-
"""Exception hierarchy for the tdata/session import flow.

Every exception carries a stable, safe `error_class` (one of
models.FailureClass) that is recorded in tdata_import_ops.error_class and
mapped to a user-facing safe message. Exception *text* must never contain
credential material -- callers pass only safe, generic detail.

Stdlib only (imports models, which is pure). Safe to import anywhere.
"""
from __future__ import annotations

from typing import Optional

from .models import FailureClass


class TdataImportError(Exception):
    """Base for all import errors. `error_class` is the safe machine code."""

    error_class: str = "invalid_archive"

    def __init__(self, message: str = "", *, error_class: Optional[str] = None):
        super().__init__(message or self.error_class)
        if error_class:
            self.error_class = str(error_class)

    def safe_class(self) -> str:
        ec = str(self.error_class or "")
        return ec if ec in FailureClass.ALL else FailureClass.INVALID_ARCHIVE


def _mk(name: str, code: str):
    return type(name, (TdataImportError,), {"error_class": code})


# One subclass per failure class. Distinct types so callers can `except` a
# specific failure; all share the safe-class contract of the base.
InvalidArchive = _mk("InvalidArchive", FailureClass.INVALID_ARCHIVE)
UnsafeArchive = _mk("UnsafeArchive", FailureClass.UNSAFE_ARCHIVE)
UnsupportedPackage = _mk("UnsupportedPackage", FailureClass.UNSUPPORTED_PACKAGE)
MultipleAccounts = _mk("MultipleAccounts", FailureClass.MULTIPLE_ACCOUNTS)
SessionCorrupt = _mk("SessionCorrupt", FailureClass.SESSION_CORRUPT)
SessionSchemaUnsupported = _mk("SessionSchemaUnsupported", FailureClass.SESSION_SCHEMA_UNSUPPORTED)
TdataConversionFailed = _mk("TdataConversionFailed", FailureClass.TDATA_CONVERSION_FAILED)
TdataPasscodeRequired = _mk("TdataPasscodeRequired", FailureClass.TDATA_PASSCODE_REQUIRED)
SessionUnauthorized = _mk("SessionUnauthorized", FailureClass.SESSION_UNAUTHORIZED)
ProxyMissing = _mk("ProxyMissing", FailureClass.PROXY_MISSING)
ProxyUnavailable = _mk("ProxyUnavailable", FailureClass.PROXY_UNAVAILABLE)
DirectConnectionBlocked = _mk("DirectConnectionBlocked", FailureClass.DIRECT_CONNECTION_BLOCKED)
IdentityMismatch = _mk("IdentityMismatch", FailureClass.IDENTITY_MISMATCH)
DuplicateTelegramAccount = _mk("DuplicateTelegramAccount", FailureClass.DUPLICATE_TELEGRAM_ACCOUNT)
SessionInstallFailed = _mk("SessionInstallFailed", FailureClass.SESSION_INSTALL_FAILED)
RuntimeFailed = _mk("RuntimeFailed", FailureClass.RUNTIME_FAILED)
CleanupFailed = _mk("CleanupFailed", FailureClass.CLEANUP_FAILED)
StaleOperation = _mk("StaleOperation", FailureClass.STALE_OPERATION)
ConcurrentOperation = _mk("ConcurrentOperation", FailureClass.CONCURRENT_OPERATION)


# Registry for constructing an exception from a machine code (used by the
# service when a storage/adapter layer returns a code rather than raising).
BY_CODE = {
    FailureClass.INVALID_ARCHIVE: InvalidArchive,
    FailureClass.UNSAFE_ARCHIVE: UnsafeArchive,
    FailureClass.UNSUPPORTED_PACKAGE: UnsupportedPackage,
    FailureClass.MULTIPLE_ACCOUNTS: MultipleAccounts,
    FailureClass.SESSION_CORRUPT: SessionCorrupt,
    FailureClass.SESSION_SCHEMA_UNSUPPORTED: SessionSchemaUnsupported,
    FailureClass.TDATA_CONVERSION_FAILED: TdataConversionFailed,
    FailureClass.TDATA_PASSCODE_REQUIRED: TdataPasscodeRequired,
    FailureClass.SESSION_UNAUTHORIZED: SessionUnauthorized,
    FailureClass.PROXY_MISSING: ProxyMissing,
    FailureClass.PROXY_UNAVAILABLE: ProxyUnavailable,
    FailureClass.DIRECT_CONNECTION_BLOCKED: DirectConnectionBlocked,
    FailureClass.IDENTITY_MISMATCH: IdentityMismatch,
    FailureClass.DUPLICATE_TELEGRAM_ACCOUNT: DuplicateTelegramAccount,
    FailureClass.SESSION_INSTALL_FAILED: SessionInstallFailed,
    FailureClass.RUNTIME_FAILED: RuntimeFailed,
    FailureClass.CLEANUP_FAILED: CleanupFailed,
    FailureClass.STALE_OPERATION: StaleOperation,
    FailureClass.CONCURRENT_OPERATION: ConcurrentOperation,
}


def from_code(code: str, message: str = "") -> TdataImportError:
    cls = BY_CODE.get(str(code or ""), TdataImportError)
    return cls(message)
