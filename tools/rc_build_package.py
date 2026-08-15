# -*- coding: utf-8 -*-
"""tools/rc_build_package.py -- manifest-driven release packager (Master Plan P2).

Implements the packaging contract the Master Plan requires. P2 only *validates*
this builder against a disposable TEST archive; the real release ZIP is P14.

Three categories, structurally separated -- no file may belong to two:

  A PROJECT CONTENT      listed in release_content_manifest.json
                         -> extracted to C:\\ALM_TPilot\\<relative_path>
  B DEPLOYMENT CONTROL   __tpilot_release__/...  (manifest, metadata, repair/*)
                         -> server Desktop control/staging ONLY, never the project
  C SERVER-LOCAL CONFIG  .env.TPilot -- never in the ZIP, never in any manifest

Anti-circularity:
  * release_content_manifest.json NEVER lists itself.
  * release_metadata.json carries content_manifest_sha256 computed OUTSIDE it,
    and control_artifacts[] for every control file EXCEPT itself.

Counting terminology (single vocabulary, Master Plan section 16):
  CONTENT_FILE_COUNT + CONTROL_FILE_COUNT == ZIP_FILE_ENTRY_COUNT
  (the writer below emits no independent directory entries)

Operations:
  build          --content-manifest <json> --out <zip> [--control-dir <dir>]
  verify         --zip <zip>                 (or --extracted <dir>)
  read-control   --zip <zip>                 (reads control WITHOUT full extract)
"""
from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from rc_release_control import (  # noqa: E402
    CONTROL_NAMESPACE, dumps, is_forbidden_package_path, rel, sha256_bytes, sha256_file,
)

BASE_DIR = Path(__file__).resolve().parent.parent

CONTENT_MANIFEST_NAME = f"{CONTROL_NAMESPACE}/release_content_manifest.json"
METADATA_NAME = f"{CONTROL_NAMESPACE}/release_metadata.json"


class PackageError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Path safety
# --------------------------------------------------------------------------

def assert_safe_content_path(relative_path: str) -> None:
    reason = is_forbidden_package_path(relative_path)
    if reason:
        raise PackageError(f"refusing content path {relative_path!r}: {reason}")
    if relative_path.startswith(CONTROL_NAMESPACE + "/"):
        raise PackageError(
            f"refusing content path {relative_path!r}: "
            f"{CONTROL_NAMESPACE}/ is the control namespace, never project content")


def assert_safe_control_path(arcname: str) -> None:
    if not arcname.startswith(CONTROL_NAMESPACE + "/"):
        raise PackageError(f"control artefact must live under {CONTROL_NAMESPACE}/: {arcname!r}")
    if ".." in Path(arcname).parts:
        raise PackageError(f"path traversal in control artefact: {arcname!r}")
    base = arcname.rsplit("/", 1)[-1].lower()
    if base.startswith(".env"):
        raise PackageError("server-local config may never be a control artefact")


# --------------------------------------------------------------------------
# BUILD
# --------------------------------------------------------------------------

def build(content_manifest_path: Path, out_zip: Path, root: Path,
          control_dir: Optional[Path], release_id: str,
          created_at: str, not_for_deployment: bool) -> Dict[str, Any]:
    manifest = json.loads(content_manifest_path.read_text(encoding="utf-8"))
    files: List[Dict[str, Any]] = manifest["files"]

    # ---- validate content set -------------------------------------------
    seen: Dict[str, str] = {}
    for e in files:
        rp = e["relative_path"]
        assert_safe_content_path(rp)
        if rp in seen:
            raise PackageError(f"duplicate relative_path in manifest: {rp!r}")
        seen[rp] = e["sha256"]
        src = root / rp
        if not src.is_file():
            raise PackageError(f"manifest lists a missing file: {rp!r}")
        actual = sha256_file(src)
        if actual != e["sha256"]:
            raise PackageError(
                f"source changed after manifest generation: {rp!r} "
                f"manifest={e['sha256'][:12]} actual={actual[:12]}")

    # ---- content manifest must not list itself --------------------------
    for e in files:
        if e["relative_path"] in (CONTENT_MANIFEST_NAME, METADATA_NAME):
            raise PackageError("content manifest must never list control artefacts")

    manifest_bytes = dumps(manifest).encode("utf-8")
    content_manifest_sha = sha256_bytes(manifest_bytes)   # computed OUTSIDE the file

    # ---- collect control artefacts --------------------------------------
    control_entries: List[Tuple[str, bytes]] = [(CONTENT_MANIFEST_NAME, manifest_bytes)]
    control_artifacts: List[Dict[str, Any]] = [{
        "path": CONTENT_MANIFEST_NAME,
        "sha256": content_manifest_sha,
        "size": len(manifest_bytes),
    }]
    if control_dir and control_dir.is_dir():
        for p in sorted(control_dir.rglob("*")):
            if not p.is_file():
                continue
            arc = f"{CONTROL_NAMESPACE}/{p.relative_to(control_dir).as_posix()}"
            assert_safe_control_path(arc)
            data = p.read_bytes()
            control_entries.append((arc, data))
            control_artifacts.append(
                {"path": arc, "sha256": sha256_bytes(data), "size": len(data)})

    metadata = {
        "release_id": release_id,
        "created_at": created_at,
        "content_manifest_sha256": content_manifest_sha,
        "content_file_count": len(files),
        "control_file_count": len(control_entries) + 1,   # +1 = metadata itself
        "expected_control_paths": sorted([a["path"] for a in control_artifacts] + [METADATA_NAME]),
        "control_artifacts": sorted(control_artifacts, key=lambda a: a["path"]),
        "m5_applied_to_production": False,
        "not_for_deployment": bool(not_for_deployment),
        "schema_note": (
            "control_artifacts[] never includes release_metadata.json itself "
            "(anti-circularity, same rule as the content manifest)."),
    }
    if not_for_deployment:
        metadata["WARNING"] = "P2 TEST PACKAGE -- NOT FOR DEPLOYMENT"
    metadata_bytes = dumps(metadata).encode("utf-8")

    # ---- write ZIP (no independent directory entries) --------------------
    out_zip.parent.mkdir(parents=True, exist_ok=True)
    written: List[str] = []
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for e in files:
            rp = e["relative_path"]
            z.write(root / rp, arcname=rp)
            written.append(rp)
        for arc, data in control_entries:
            z.writestr(arc, data)
            written.append(arc)
        z.writestr(METADATA_NAME, metadata_bytes)
        written.append(METADATA_NAME)

    if len(set(written)) != len(written):
        raise PackageError("duplicate archive names produced")

    with zipfile.ZipFile(out_zip) as z:
        entries = z.namelist()
        dir_entries = [n for n in entries if n.endswith("/")]

    return {
        "zip": str(out_zip),
        "zip_sha256": sha256_file(out_zip),
        "CONTENT_FILE_COUNT": len(files),
        "CONTROL_FILE_COUNT": len(control_entries) + 1,
        "ZIP_FILE_ENTRY_COUNT": len(entries),
        "directory_entries": len(dir_entries),
        "content_manifest_sha256": content_manifest_sha,
        "release_id": release_id,
        "not_for_deployment": bool(not_for_deployment),
    }


# --------------------------------------------------------------------------
# READ CONTROL (without extracting the whole package) -- needed at P15B/0b
# --------------------------------------------------------------------------

def read_control(zip_path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(zip_path) as z:
        names = set(z.namelist())
        if CONTENT_MANIFEST_NAME not in names or METADATA_NAME not in names:
            raise PackageError("control artefacts absent from archive")
        manifest_bytes = z.read(CONTENT_MANIFEST_NAME)
        metadata_bytes = z.read(METADATA_NAME)
        meta = json.loads(metadata_bytes.decode("utf-8"))
        actual_sha = sha256_bytes(manifest_bytes)
        if actual_sha != meta["content_manifest_sha256"]:
            raise PackageError(
                f"content manifest SHA mismatch: archive={actual_sha[:12]} "
                f"metadata={meta['content_manifest_sha256'][:12]}")
        for a in meta.get("control_artifacts", []):
            if a["path"] not in names:
                raise PackageError(f"declared control artefact missing: {a['path']}")
            data = z.read(a["path"])
            if sha256_bytes(data) != a["sha256"]:
                raise PackageError(f"control artefact tampered: {a['path']}")
            if len(data) != a["size"]:
                raise PackageError(f"control artefact size mismatch: {a['path']}")
        return {
            "metadata": meta,
            "content_manifest": json.loads(manifest_bytes.decode("utf-8")),
            "content_manifest_sha256": actual_sha,
        }


# --------------------------------------------------------------------------
# VERIFY
# --------------------------------------------------------------------------

def verify_zip(zip_path: Path) -> Dict[str, Any]:
    problems: List[str] = []
    ctl = read_control(zip_path)
    meta, manifest = ctl["metadata"], ctl["content_manifest"]
    declared = {e["relative_path"]: e for e in manifest["files"]}

    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        content_names = [n for n in names if not n.startswith(CONTROL_NAMESPACE + "/")]
        control_names = [n for n in names if n.startswith(CONTROL_NAMESPACE + "/")]

        extra = sorted(set(content_names) - set(declared))
        missing = sorted(set(declared) - set(content_names))
        for n in extra:
            problems.append(f"extra file in archive not declared in manifest: {n}")
        for n in missing:
            problems.append(f"declared file missing from archive: {n}")

        for n in content_names:
            reason = is_forbidden_package_path(n)
            if reason:
                problems.append(f"forbidden path in archive: {n} ({reason})")
            if n in declared:
                data = z.read(n)
                if sha256_bytes(data) != declared[n]["sha256"]:
                    problems.append(f"SHA mismatch in archive: {n}")
                if len(data) != declared[n]["size"]:
                    problems.append(f"size mismatch in archive: {n}")

        expected_ctl = set(meta.get("expected_control_paths", []))
        unexpected_ctl = sorted(set(control_names) - expected_ctl)
        for n in unexpected_ctl:
            problems.append(f"unexpected control artefact: {n}")

    return {
        "ok": not problems, "problems": problems,
        "CONTENT_FILE_COUNT": len(declared),
        "CONTROL_FILE_COUNT": len(meta.get("expected_control_paths", [])),
        "ZIP_FILE_ENTRY_COUNT": len(names),
        "release_id": meta.get("release_id"),
        "not_for_deployment": meta.get("not_for_deployment"),
    }


def verify_extracted(extract_root: Path, zip_path: Optional[Path] = None,
                     control_root: Optional[Path] = None) -> Dict[str, Any]:
    """Structural proof: every declared path exists EXACTLY there, SHA matches."""
    problems: List[str] = []
    if zip_path:
        manifest = read_control(zip_path)["content_manifest"]
    else:
        cr = control_root or (extract_root / CONTROL_NAMESPACE)
        manifest = json.loads((cr / "release_content_manifest.json").read_text(encoding="utf-8"))

    checked = nested = 0
    for e in manifest["files"]:
        rp = e["relative_path"]
        target = extract_root / rp
        if not target.is_file():
            problems.append(f"declared path not extracted at its exact location: {rp}")
            continue
        if sha256_file(target) != e["sha256"]:
            problems.append(f"extracted SHA mismatch: {rp}")
        checked += 1
        if "/" in rp:
            nested += 1
    return {"ok": not problems, "problems": problems,
            "checked": checked, "nested_paths_verified": nested}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="op", required=True)

    b = sub.add_parser("build")
    b.add_argument("--content-manifest", required=True)
    b.add_argument("--out", required=True)
    b.add_argument("--root", default=str(BASE_DIR))
    b.add_argument("--control-dir")
    b.add_argument("--release-id", required=True)
    b.add_argument("--created-at", required=True)
    b.add_argument("--not-for-deployment", action="store_true")

    v = sub.add_parser("verify")
    v.add_argument("--zip")
    v.add_argument("--extracted")
    v.add_argument("--control-root")

    r = sub.add_parser("read-control")
    r.add_argument("--zip", required=True)

    a = ap.parse_args()
    try:
        if a.op == "build":
            res = build(Path(a.content_manifest), Path(a.out), Path(a.root).resolve(),
                        Path(a.control_dir) if a.control_dir else None,
                        a.release_id, a.created_at, a.not_for_deployment)
            print(dumps(res))
            return 0
        if a.op == "verify":
            if a.zip and not a.extracted:
                res = verify_zip(Path(a.zip))
            else:
                res = verify_extracted(Path(a.extracted),
                                       Path(a.zip) if a.zip else None,
                                       Path(a.control_root) if a.control_root else None)
            print(dumps(res))
            return 0 if res["ok"] else 1
        if a.op == "read-control":
            ctl = read_control(Path(a.zip))
            print(dumps({"metadata": ctl["metadata"],
                         "content_manifest_sha256": ctl["content_manifest_sha256"],
                         "content_file_count": len(ctl["content_manifest"]["files"])}))
            return 0
    except PackageError as e:
        print(f"[PACKAGE ERROR] {e}")
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
