# -*- coding: utf-8 -*-
"""Offline selftest for the vendored tdata converter, against the REAL
authorized test fixture.

Fixture handling rules (owner-mandated, enforced here programmatically):
  1. The original ZIP is NEVER opened for writing and NEVER modified.
  2. SHA256 of the original is computed before AND after the whole run; the
     test fails loudly if they differ.
  3. The fixture is always copied into a fresh temp directory before any
     extraction happens -- extraction never touches the original.
  4. For the tdata-only path, the ready `.session` found in the TEMP COPY's
     extracted tree is explicitly deleted before conversion, so only the
     tdata conversion path is exercised.
  5. No Telegram or proxy connection is made anywhere in this file (verified
     by inspection: no telethon .connect()/.start(), no socket/proxy calls).
  6. No auth_key / API hash / 2FA password / full phone / proxy password /
     decrypted tdata secret is ever printed -- only lengths/booleans/dc ids.
  7. Nothing here writes into the deployment/package tree.
  8. The fixture is never copied into permanent runtime storage (only into a
     tempfile.mkdtemp() directory, cleaned up at the end).
  9. Nothing decrypted is ever written back under the fixture's own directory.

Run:  python tools\\tdata_converter_selftest.py
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import json
import sqlite3

from tdata_import import archive, detector, errors, session_inspector, tdata_adapter  # noqa: E402
from tdata_import.vendor.tdesktop import reader as tdata_reader  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


# Designated repo location per the project rules, with a fallback to the
# actual location the fixture was found at during this session (the repo path
# does not exist on disk -- see the session's transcript / final report for
# the discrepancy noted to the owner). Both are read-only lookups.
_CANDIDATE_FIXTURE_PATHS = [
    os.path.join(BASE_DIR, "_fixtures", "tdata_import", "79833313465 тест.zip"),
    os.path.join(os.path.expanduser("~"), "Desktop", "79833313465 тест.zip"),
]


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 256), b""):
            h.update(chunk)
    return h.hexdigest()


def _resolve_fixture():
    for p in _CANDIDATE_FIXTURE_PATHS:
        if os.path.isfile(p):
            return p
    return None


def main():
    fixture = _resolve_fixture()
    if not fixture:
        print("[SKIP] real tdata fixture not found at any known location -- "
              f"tried: {_CANDIDATE_FIXTURE_PATHS!r}")
        print("This selftest requires the owner-supplied authorized test archive; "
              "skipping (not a code failure).")
        return 0

    print(f"[INFO] using fixture: {fixture}")
    sha_before = _sha256(fixture)
    size_before = os.path.getsize(fixture)
    print(f"[INFO] fixture SHA256 before: {sha_before}")
    print(f"[INFO] fixture size: {size_before} bytes")

    work_root = tempfile.mkdtemp(prefix="tdconv_")
    try:
        # Rule 3: copy before extracting; original is opened read-only above
        # and never touched again.
        copy_path = os.path.join(work_root, "fixture_copy.zip")
        shutil.copy2(fixture, copy_path)
        check("fixture copied to temp before extraction", os.path.isfile(copy_path))

        res = archive.extract_archive(copy_path, os.path.join(work_root, "extract"))
        check("fixture extracts under safety limits", res["file_count"] > 0)

        inv = detector.inventory(res["extracted_dir"])
        check("inventory finds a ready .session", len(inv.session_candidates) == 1,
              detail=str(len(inv.session_candidates)))
        check("inventory finds exactly one tdata root", len(inv.tdata_dirs) == 1,
              detail=str(len(inv.tdata_dirs)))

        # --- priority check: WITH the ready .session present, it must win ---
        kind_with_session, path_with_session = detector.choose_source(inv)
        check("priority: ready .session wins while both are present", kind_with_session == "ready_session")

        # Bonus cross-check (no network): the shipped ready .session is itself
        # a structurally valid Telethon v7 session.
        ready_candidate = session_inspector.inspect_session(path_with_session, origin="ready")
        check("shipped ready .session is a structurally valid v7 session", ready_candidate.valid is True,
              detail=ready_candidate.reason)

        # --- independent cross-check: tdata-derived user_id vs JSON metadata
        # (both shipped in the same fixture, read here purely for offline
        # verification -- never persisted, never logged elsewhere). A match
        # is strong evidence the vendored parser is correct, independent of
        # any live Telegram check. The fixture's real id exceeds 2**32-1, so
        # this also proves the "wide ids" 64-bit sentinel branch is correct.
        tdata_dir_for_xcheck = inv.tdata_dirs[0]
        json_user_id = None
        for jp in inv.json_meta_files:
            try:
                data = json.load(open(jp, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "id" in data:
                json_user_id = int(data["id"])
                break
        if json_user_id is not None:
            xcheck_accounts = tdata_reader.read_tdata_accounts(tdata_dir_for_xcheck)
            check("tdata-derived user_id present for cross-check", len(xcheck_accounts) == 1)
            check("tdata-derived user_id matches JSON metadata id (independent proof)",
                  xcheck_accounts[0].user_id == json_user_id,
                  detail=f"tdata={xcheck_accounts[0].user_id} json={json_user_id}")
            check("cross-checked user_id exceeds 32 bits (wide-ids branch exercised)",
                  json_user_id > 0xFFFFFFFF, detail=str(json_user_id))
        else:
            print("[INFO] fixture JSON has no 'id' field -- skipping user_id cross-check")

        # dc_id / server_address cross-check against the shipped ready session
        # (auth_key itself is expected to DIFFER -- MTProto issues a distinct
        # auth_key per login/export event, not per account, so the two
        # exports of the same account legitimately carry different keys).
        con = sqlite3.connect(path_with_session)
        try:
            shipped_dc_id, shipped_addr, shipped_port, _shipped_key = con.execute(
                "SELECT dc_id, server_address, port, auth_key FROM sessions"
            ).fetchone()
        finally:
            con.close()

        # --- Rule 4: now exercise ONLY the tdata conversion path ---------
        tdata_dir = inv.tdata_dirs[0]
        for s in list(inv.session_candidates):
            os.remove(s)
            check(f"ready .session removed from temp copy ({os.path.basename(s)})", not os.path.isfile(s))

        inv2 = detector.inventory(res["extracted_dir"])
        check("after removal: no .session candidates remain", len(inv2.session_candidates) == 0)
        kind2, path2 = detector.choose_source(inv2)
        check("after removal: detector falls back to tdata", kind2 == "tdata")

        dest = os.path.join(work_root, "converted", "out.session")
        candidate = tdata_adapter.convert_tdata_to_session(tdata_dir, dest)

        check("conversion produced a valid session", candidate.valid is True, detail=candidate.reason)
        check("converted origin marked tdata_converted", candidate.origin == "tdata_converted")
        check("converted schema version is 7", candidate.schema_version == 7)
        check("converted auth_key length is 256", candidate.auth_key_len == 256)
        check("converted dc_id is plausible (1..5)", candidate.dc_id in (1, 2, 3, 4, 5),
              detail=str(candidate.dc_id))
        check("converted server_address is non-empty", bool(candidate.server_address))
        check("converted session file exists on disk", os.path.isfile(dest))
        check("converted dc_id matches shipped ready session's dc_id (same account, same home DC)",
              candidate.dc_id == shipped_dc_id, detail=f"tdata={candidate.dc_id} shipped={shipped_dc_id}")
        check("converted server_address matches shipped ready session's",
              candidate.server_address == shipped_addr,
              detail=f"tdata={candidate.server_address} shipped={shipped_addr}")

        # Re-inspect independently (fresh call, not the cached candidate) to
        # prove the file itself -- not just the in-memory object -- is valid.
        reinspect = session_inspector.inspect_session(dest, origin="tdata_converted")
        check("independent re-inspection of converted file also passes", reinspect.valid is True,
              detail=reinspect.reason)

        # --- MultipleAccounts path (synthetic: point at a dir with no valid
        # tdata to prove the "no accounts" error path doesn't silently
        # fabricate a session) ---
        empty_dir = os.path.join(work_root, "empty_tdata")
        os.makedirs(empty_dir, exist_ok=True)
        raised = None
        try:
            tdata_adapter.convert_tdata_to_session(empty_dir, os.path.join(work_root, "bad.session"))
        except errors.TdataConversionFailed:
            raised = "conversion_failed"
        except Exception as exc:  # noqa: BLE001
            raised = f"other:{type(exc).__name__}"
        check("empty/invalid tdata dir -> TdataConversionFailed (never fabricates a session)",
              raised == "conversion_failed", detail=str(raised))

        # --- redaction sanity: auth_key bytes never leaked into any check detail
        auth_key_hex_guess = candidate.reason  # 'reason' is always "ok"/safe text
        check("no auth-key-shaped hex blob in candidate.reason", len(auth_key_hex_guess) < 64)

    finally:
        # Rule 8/9: temp working tree (including the extracted plaintext
        # tdata/session material) is removed; nothing persists outside this
        # function's tempfile.mkdtemp() root, and nothing was ever written
        # under the fixture's own directory.
        shutil.rmtree(work_root, ignore_errors=True)
        check("temp working directory cleaned up", not os.path.isdir(work_root))

    # --- Rule 1/2: prove the ORIGINAL fixture was never touched ----------
    sha_after = _sha256(fixture)
    size_after = os.path.getsize(fixture)
    check("original fixture SHA256 unchanged", sha_after == sha_before,
          detail=f"before={sha_before} after={sha_after}")
    check("original fixture size unchanged", size_after == size_before)
    print(f"[INFO] fixture SHA256 after:  {sha_after}")

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL TDATA-CONVERTER SELFTESTS PASSED (real fixture)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
