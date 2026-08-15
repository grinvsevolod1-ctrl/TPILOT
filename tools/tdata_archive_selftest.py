# -*- coding: utf-8 -*-
"""Offline selftest for tdata_import.archive (safe ZIP extraction) + detector.

Pure/offline: builds throwaway ZIPs in a temp dir, no network, no Telegram, no
production data. Covers ZIP-slip, absolute/drive/UNC paths, reserved names,
ADS, symlink entries, zip bomb (ratio), too-many-files, exec/nested quarantine,
case collisions, and detector priority/MultipleAccounts/UnsupportedPackage.

Run:  python tools\\tdata_archive_selftest.py
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
import zipfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tdata_import import archive, detector, errors  # noqa: E402
from tdata_import.models import ArchiveLimits  # noqa: E402

FAILURES = []


def check(label, condition, detail=""):
    status = "OK" if condition else "FAIL"
    print(f"[{status}] {label}" + (f" -- {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _mkzip(path, entries, *, compression=zipfile.ZIP_STORED):
    """entries: list of (name, data_bytes) or (ZipInfo, data)."""
    with zipfile.ZipFile(path, "w", compression=compression) as zf:
        for name, data in entries:
            if isinstance(name, zipfile.ZipInfo):
                zf.writestr(name, data)
            else:
                zf.writestr(name, data)


def _expect_unsafe(label, zip_entries, *, limits=None, compression=zipfile.ZIP_STORED):
    tmpd = tempfile.mkdtemp(prefix="tdz_")
    zp = os.path.join(tmpd, "a.zip")
    _mkzip(zp, zip_entries, compression=compression)
    raised = None
    try:
        archive.extract_archive(zp, os.path.join(tmpd, "work"), limits=limits)
    except errors.UnsafeArchive:
        raised = "unsafe"
    except errors.InvalidArchive:
        raised = "invalid"
    except Exception as exc:  # noqa: BLE001
        raised = f"other:{type(exc).__name__}"
    check(label, raised == "unsafe", detail=str(raised))


def main():
    # --- valid archive: session + tdata + json + accounts ---------------
    tmpd = tempfile.mkdtemp(prefix="tdz_valid_")
    zp = os.path.join(tmpd, "pkg.zip")
    _mkzip(zp, [
        ("acct/telethon.session", b"SQLite format 3\x00fake"),
        ("tdata/key_datas", b"\x01\x02\x03"),
        ("tdata/D877F783D5D3EF8C/maps", b"\x00"),
        ("meta.json", b'{"phone":"x"}'),
        ("Accounts.txt", b"login:pass"),
    ])
    sha_before = hashlib.sha256(open(zp, "rb").read()).hexdigest()
    res = archive.extract_archive(zp, os.path.join(tmpd, "work"))
    check("valid archive extracts", res["file_count"] >= 2)
    check("original zip unchanged after extract",
          hashlib.sha256(open(zp, "rb").read()).hexdigest() == sha_before)
    inv = detector.inventory(res["extracted_dir"])
    check("detector finds the .session", len(inv.session_candidates) == 1)
    check("detector finds tdata root", len(inv.tdata_dirs) == 1)
    check("detector finds json + accounts", len(inv.json_meta_files) == 1 and len(inv.accounts_txt) == 1)
    kind, path = detector.choose_source(inv)
    check("ready .session has priority over tdata", kind == "ready_session")

    # --- unsafe path families -------------------------------------------
    _expect_unsafe("ZIP slip (../) rejected", [("../evil.txt", b"x")])
    _expect_unsafe("absolute path rejected", [("/etc/passwd", b"x")])
    _expect_unsafe("drive path rejected", [("C:/Windows/x.txt", b"x")])
    _expect_unsafe("reserved name rejected", [("CON.txt", b"x")])
    _expect_unsafe("ADS colon rejected", [("dir/note.txt:secret", b"x")])
    _expect_unsafe("trailing dot component rejected", [("weird./x.txt", b"x")])

    # --- symlink entry ---------------------------------------------------
    zi = zipfile.ZipInfo("link")
    zi.external_attr = (stat.S_IFLNK | 0o777) << 16
    _expect_unsafe("symlink entry rejected", [(zi, b"/etc/passwd")])

    # --- too many files --------------------------------------------------
    many = [(f"f{i}.txt", b"x") for i in range(6)]
    _expect_unsafe("too many files rejected", many, limits=ArchiveLimits(max_file_count=3))

    # --- zip bomb by ratio ----------------------------------------------
    bomb = [("big.bin", b"\x00" * (1024 * 1024))]  # 1 MB zeros, compresses ~1000x
    _expect_unsafe("zip bomb (ratio) rejected", bomb,
                   limits=ArchiveLimits(max_compression_ratio=5.0),
                   compression=zipfile.ZIP_DEFLATED)

    # --- case-insensitive collision -------------------------------------
    _expect_unsafe("case-insensitive collision rejected",
                   [("dir/File.TXT", b"a"), ("dir/file.txt", b"b")])

    # --- exec + nested archive quarantined (NOT into extracted tree) ----
    tq = tempfile.mkdtemp(prefix="tdz_q_")
    zq = os.path.join(tq, "q.zip")
    _mkzip(zq, [
        ("acct/telethon.session", b"SQLite format 3\x00"),
        ("tool.exe", b"MZfake"),
        ("inner.zip", b"PKfake"),
    ])
    rq = archive.extract_archive(zq, os.path.join(tq, "work"))
    check("exec + nested quarantined", set(rq["quarantined"]) == {"tool.exe", "inner.zip"})
    ext_files = {os.path.basename(p) for p in rq["normal_files"]}
    check("quarantined items absent from extracted tree",
          "tool.exe" not in ext_files and "inner.zip" not in ext_files)
    check("exec file physically under quarantine dir",
          os.path.isfile(os.path.join(rq["quarantine_dir"], "tool.exe")))
    check("exec file NOT under extracted dir",
          not os.path.isfile(os.path.join(rq["extracted_dir"], "tool.exe")))

    # --- detector: multiple accounts ------------------------------------
    tm = tempfile.mkdtemp(prefix="tdz_multi_")
    zm = os.path.join(tm, "m.zip")
    _mkzip(zm, [("a/one.session", b"S"), ("b/two.session", b"S")])
    rm = archive.extract_archive(zm, os.path.join(tm, "work"))
    invm = detector.inventory(rm["extracted_dir"])
    raised = False
    try:
        detector.choose_source(invm)
    except errors.MultipleAccounts:
        raised = True
    check("multiple .session -> MultipleAccounts", raised)

    # --- detector: unsupported package ----------------------------------
    tu = tempfile.mkdtemp(prefix="tdz_unsup_")
    zu = os.path.join(tu, "u.zip")
    _mkzip(zu, [("readme.txt", b"nothing useful"), ("data.json", b"{}")])
    ru = archive.extract_archive(zu, os.path.join(tu, "work"))
    invu = detector.inventory(ru["extracted_dir"])
    raised2 = False
    try:
        detector.choose_source(invu)
    except errors.UnsupportedPackage:
        raised2 = True
    check("no session/tdata -> UnsupportedPackage", raised2)

    # --- detector: tdata-only picks tdata -------------------------------
    tt = tempfile.mkdtemp(prefix="tdz_tdata_")
    zt = os.path.join(tt, "t.zip")
    _mkzip(zt, [("tdata/key_datas", b"\x01"), ("tdata/D877/maps", b"\x00"), ("meta.json", b"{}")])
    rt = archive.extract_archive(zt, os.path.join(tt, "work"))
    invt = detector.inventory(rt["extracted_dir"])
    kt, pt = detector.choose_source(invt)
    check("tdata-only package -> tdata source", kt == "tdata")

    # --- not a zip -------------------------------------------------------
    tnz = tempfile.mkdtemp(prefix="tdz_nz_")
    bad = os.path.join(tnz, "bad.zip")
    open(bad, "wb").write(b"this is not a zip")
    raised3 = None
    try:
        archive.extract_archive(bad, os.path.join(tnz, "work"))
    except errors.InvalidArchive:
        raised3 = True
    except Exception as exc:  # noqa: BLE001
        raised3 = f"other:{type(exc).__name__}"
    check("non-zip -> InvalidArchive", raised3 is True, detail=str(raised3))

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL ARCHIVE/DETECTOR SELFTESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
