# -*- coding: utf-8 -*-
"""tools/rc_tooling_selftest.py -- selftests for the P2 release-control tooling.

Covers cases A..P from the Master Plan P2 brief plus the adversarial attacks in
section 33. Everything runs against TEMP fixtures -- the project tree is never
mutated and the project DB/sessions/runtime/logs are never touched.

Usage:  python tools/rc_tooling_selftest.py
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import allow_spend_ast_gate as spend          # noqa: E402
import rc_build_package as pkg                 # noqa: E402
import rc_dependency_closure as dep            # noqa: E402
import rc_freeze_manifest as freeze            # noqa: E402
from rc_release_control import (               # noqa: E402
    ModuleIndex, dumps, is_forbidden_package_path, sha256_bytes, sha256_file,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent
OK = FAILED = 0


def check(label: str, cond: bool, extra: str = "") -> None:
    global OK, FAILED
    if cond:
        OK += 1
        print(f"[OK]   {label}")
    else:
        FAILED += 1
        print(f"[FAIL] {label}   {extra}")


def make_fixture(tmp: Path) -> Path:
    """Minimal project-shaped fixture with nested packages."""
    root = tmp / "proj"
    (root / "features" / "prepared_accounts").mkdir(parents=True)
    (root / "tdata_import" / "vendor" / "tdesktop").mkdir(parents=True)
    (root / "db").mkdir()
    (root / "sessions").mkdir()

    (root / "main.py").write_text(
        "import features.prepared_accounts as _p\n"
        "from tdata_import import service as _s\n"
        "def go():\n    return 1\n", encoding="utf-8")
    (root / "features" / "__init__.py").write_text("", encoding="utf-8")
    (root / "features" / "prepared_accounts" / "__init__.py").write_text("", encoding="utf-8")
    (root / "features" / "prepared_accounts" / "model.py").write_text(
        "PREPARED_READY_MARKER='x'\ndef get_prepared():\n    return None\n", encoding="utf-8")
    (root / "tdata_import" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tdata_import" / "service.py").write_text("def run():\n    return 2\n", encoding="utf-8")
    (root / "tdata_import" / "vendor" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tdata_import" / "vendor" / "tdesktop" / "__init__.py").write_text("", encoding="utf-8")
    (root / "tdata_import" / "vendor" / "tdesktop" / "reader.py").write_text(
        "def read():\n    return 3\n", encoding="utf-8")
    (root / ".env.TPilot").write_text("SECRET=nope\n", encoding="utf-8")
    (root / "db" / "data.db").write_bytes(b"\x00")
    (root / "sessions" / "a.session").write_bytes(b"\x00")
    return root


def manifest_for(root: Path, paths):
    files = []
    for rp in sorted(paths):
        p = root / rp
        files.append({
            "relative_path": rp, "sha256": sha256_file(p), "size": p.stat().st_size,
            "role": "runtime", "reason_changed": None, "phase": "TEST",
            "migration_relevance": None, "compile_required": rp.endswith(".py"),
            "import_smoke_group": None, "existed_on_historical_baseline": None,
            "protected_symbols": [], "symbol_ast_sha256": {},
        })
    return {"schema_version": "1.0", "artifact": "TEST", "files": files}


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="rc_tool_"))
    try:
        root = make_fixture(tmp)
        content = ["main.py",
                   "features/__init__.py",
                   "features/prepared_accounts/__init__.py",
                   "features/prepared_accounts/model.py",
                   "tdata_import/__init__.py",
                   "tdata_import/service.py",
                   "tdata_import/vendor/__init__.py",
                   "tdata_import/vendor/tdesktop/__init__.py",
                   "tdata_import/vendor/tdesktop/reader.py"]
        mpath = tmp / "content_manifest.json"
        mpath.write_text(dumps(manifest_for(root, content)), encoding="utf-8")

        # ---- A/B: manifest determinism -------------------------------------
        print("\n-- A/B. detерминизм манифеста --")
        m1 = dumps(manifest_for(root, content))
        m2 = dumps(manifest_for(root, content))
        check("A. один и тот же вход -> побайтово одинаковый манифест", m1 == m2)
        check("B. SHA манифеста стабилен", sha256_bytes(m1.encode()) == sha256_bytes(m2.encode()))

        # ---- build ---------------------------------------------------------
        zp = tmp / "test.zip"
        ctl_dir = tmp / "control_extra"
        ctl_dir.mkdir()
        (ctl_dir / "repair").mkdir()
        (ctl_dir / "repair" / "r1a3_targets.json").write_text('{"targets": []}', encoding="utf-8")
        res = pkg.build(mpath, zp, root, ctl_dir, "TEST-REL", "2026-01-01T00:00:00Z", True)
        check("build: CONTENT_FILE_COUNT", res["CONTENT_FILE_COUNT"] == len(content), res)
        check("build: no independent directory entries", res["directory_entries"] == 0, res)
        check("build: counts sum exactly",
              res["ZIP_FILE_ENTRY_COUNT"] == res["CONTENT_FILE_COUNT"] + res["CONTROL_FILE_COUNT"], res)

        # ---- C/D: extract + nested structure --------------------------------
        print("\n-- C/D. извлечение и вложенные пути --")
        ext = tmp / "ext"
        with zipfile.ZipFile(zp) as z:
            z.extractall(ext)
        v = pkg.verify_extracted(ext, zp)
        check("C. SHA всех извлечённых файлов совпадает", v["ok"], v["problems"][:3])
        check("D. вложенные пути сохранены (не уплощены)",
              (ext / "tdata_import" / "vendor" / "tdesktop" / "reader.py").is_file())
        check("D2. features/prepared_accounts/model.py на своём месте",
              (ext / "features" / "prepared_accounts" / "model.py").is_file())
        check("D3. каждая запись проверена структурно", v["checked"] == len(content), v)

        # ---- E: traversal / absolute ---------------------------------------
        print("\n-- E. отклонение traversal / абсолютных путей --")
        for bad in ("../evil.py", "..\\evil.py", "/etc/passwd", "C:/Windows/x.py"):
            try:
                pkg.assert_safe_content_path(bad)
                check(f"E. отклонён {bad!r}", False, "НЕ отклонён")
            except pkg.PackageError:
                check(f"E. отклонён {bad!r}", True)

        # ---- F: extra file in archive --------------------------------------
        print("\n-- F. лишний файл в архиве отклоняется --")
        zp2 = tmp / "extra.zip"
        shutil.copy2(zp, zp2)
        with zipfile.ZipFile(zp2, "a") as z:
            z.writestr("sneaky.py", "x=1\n")
        vr = pkg.verify_zip(zp2)
        check("F. verify ловит незадекларированный файл",
              not vr["ok"] and any("extra file" in p for p in vr["problems"]), vr["problems"][:2])

        # ---- G: missing required file --------------------------------------
        print("\n-- G. отсутствующий обязательный файл --")
        bad_manifest = manifest_for(root, content)
        bad_manifest["files"].append({
            "relative_path": "does_not_exist.py", "sha256": "0" * 64, "size": 0,
            "role": "runtime", "reason_changed": None, "phase": "TEST",
            "migration_relevance": None, "compile_required": True,
            "import_smoke_group": None, "existed_on_historical_baseline": None,
            "protected_symbols": [], "symbol_ast_sha256": {}})
        bp = tmp / "bad.json"
        bp.write_text(dumps(bad_manifest), encoding="utf-8")
        try:
            pkg.build(bp, tmp / "bad.zip", root, None, "X", "Y", True)
            check("G. build падает на отсутствующем файле", False, "не упал")
        except pkg.PackageError as e:
            check("G. build падает на отсутствующем файле", "missing file" in str(e), str(e))

        # ---- H: manifest self-reference impossible --------------------------
        print("\n-- H. самоссылка манифеста невозможна --")
        selfref = manifest_for(root, content)
        selfref["files"].append({
            "relative_path": pkg.CONTENT_MANIFEST_NAME, "sha256": "0" * 64, "size": 0,
            "role": "runtime", "reason_changed": None, "phase": "T",
            "migration_relevance": None, "compile_required": False,
            "import_smoke_group": None, "existed_on_historical_baseline": None,
            "protected_symbols": [], "symbol_ast_sha256": {}})
        sp = tmp / "selfref.json"
        sp.write_text(dumps(selfref), encoding="utf-8")
        try:
            pkg.build(sp, tmp / "sr.zip", root, None, "X", "Y", True)
            check("H. манифест не может ссылаться на себя", False, "не отклонено")
        except pkg.PackageError:
            check("H. манифест не может ссылаться на себя", True)

        # ---- I: control artifact SHA validated ------------------------------
        print("\n-- I. подделка control-артефакта ловится --")
        ctl = pkg.read_control(zp)
        check("I1. read-control проходит на честном архиве",
              ctl["metadata"]["release_id"] == "TEST-REL")
        zp3 = tmp / "tampered.zip"
        with zipfile.ZipFile(zp) as zin, zipfile.ZipFile(zp3, "w") as zout:
            for it in zin.infolist():
                data = zin.read(it.filename)
                if it.filename.endswith("repair/r1a3_targets.json"):
                    data = b'{"targets": ["TAMPERED"]}'
                zout.writestr(it, data)
        try:
            pkg.read_control(zp3)
            check("I2. подделка repair-артефакта отклонена", False, "не отклонена")
        except pkg.PackageError as e:
            check("I2. подделка repair-артефакта отклонена", "tampered" in str(e), str(e))

        # tampered content manifest
        zp4 = tmp / "tampered2.zip"
        with zipfile.ZipFile(zp) as zin, zipfile.ZipFile(zp4, "w") as zout:
            for it in zin.infolist():
                data = zin.read(it.filename)
                if it.filename == pkg.CONTENT_MANIFEST_NAME:
                    data = data.replace(b'"artifact": "TEST"', b'"artifact": "HACK"')
                zout.writestr(it, data)
        try:
            pkg.read_control(zp4)
            check("I3. подделка content-манифеста отклонена", False, "не отклонена")
        except pkg.PackageError as e:
            check("I3. подделка content-манифеста отклонена", "SHA mismatch" in str(e), str(e))

        # ---- J: content/control separation ----------------------------------
        print("\n-- J. разделение content / control --")
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
        ctl_names = [n for n in names if n.startswith(pkg.CONTROL_NAMESPACE + "/")]
        cnt_names = [n for n in names if not n.startswith(pkg.CONTROL_NAMESPACE + "/")]
        check("J1. control строго под __tpilot_release__/", len(ctl_names) >= 3, ctl_names)
        check("J2. ни один content-файл не лежит в control-namespace",
              all(not n.startswith(pkg.CONTROL_NAMESPACE) for n in cnt_names))
        check("J3. repair-артефакт — control, не content",
              any("repair/r1a3_targets.json" in n for n in ctl_names))
        try:
            pkg.assert_safe_content_path(f"{pkg.CONTROL_NAMESPACE}/x.json")
            check("J4. control-путь запрещён как content", False)
        except pkg.PackageError:
            check("J4. control-путь запрещён как content", True)

        # ---- K/L: forbidden paths -------------------------------------------
        print("\n-- K/L. .env и db/sessions/runtime/logs отклоняются --")
        for bad, why in [(".env", "env"), (".env.TPilot", "env"),
                         ("db/data.db", "db"), ("sessions/a.session", "sessions"),
                         ("runtime/x.json", "runtime"), ("logs/a.log", "logs"),
                         ("exports/x.xlsx", "exports"), ("venv/x.py", "venv"),
                         ("main.py.bak_x", "bak"), ("db/data.db-wal", "wal")]:
            check(f"K/L. отклонён {bad}", is_forbidden_package_path(bad) is not None, why)
        check("K2. .env есть в фикстуре, но НЕ в архиве",
              (root / ".env.TPilot").is_file() and not any(".env" in n for n in names))

        # ---- M/N: dependency closure detects missing packages ---------------
        print("\n-- M/N. closure ловит отсутствие features / tdata_import --")
        r2 = dep.closure(root)
        check("M1. features найден как HARD", "features" in r2["hard_first_party_modules"], r2["hard_first_party_modules"])
        check("N1. tdata_import найден как HARD", "tdata_import" in r2["hard_first_party_modules"])
        broken = tmp / "broken"
        shutil.copytree(root, broken)
        shutil.rmtree(broken / "features")
        r3 = dep.closure(broken)
        check("M2. отсутствие features детектируется",
              (not r3["hard_required_package_gate"]["features"]["discovered_as_hard_dependency"])
              or bool(r3["missing_hard_first_party"]), r3["missing_hard_first_party"])
        broken2 = tmp / "broken2"
        shutil.copytree(root, broken2)
        shutil.rmtree(broken2 / "tdata_import")
        r4 = dep.closure(broken2)
        check("N2. отсутствие tdata_import детектируется",
              (not r4["hard_required_package_gate"]["tdata_import"]["discovered_as_hard_dependency"])
              or bool(r4["missing_hard_first_party"]), r4["missing_hard_first_party"])

        # ---- O: scope guard finds an intentional mutation --------------------
        print("\n-- O. scope guard ловит намеренную мутацию --")
        import rc_baseline_scope_guard as guard
        bl = tmp / "bl"
        (bl / "control").mkdir(parents=True)
        (bl / "original").mkdir(parents=True)
        gm = {"schema_version": "1.0", "files": manifest_for(root, content)["files"]}
        (bl / "control" / "golden_runtime_manifest.json").write_text(dumps(gm), encoding="utf-8")
        for rp in content:
            d = bl / "original" / rp
            d.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root / rp, d)
        g0 = guard.compare(root, bl, [])
        check("O1. без изменений -> 0 расхождений", g0["counts"]["CHANGED"] == 0 and g0["ok"], g0["counts"])
        (root / "main.py").write_text("import features.prepared_accounts as _p\n"
                                      "from tdata_import import service as _s\n"
                                      "def go():\n    return 999\n", encoding="utf-8")
        g1 = guard.compare(root, bl, [])
        check("O2. мутация обнаружена как CHANGED", g1["counts"]["CHANGED"] == 1, g1["counts"])
        check("O3. незадекларированная мутация -> FAIL", not g1["ok"])
        g2 = guard.compare(root, bl, ["main.py"])
        check("O4. задекларированная мутация -> PASS", g2["ok"])

        # ---- P: allow_spend semantic count on the REAL project --------------
        print("\n-- P. allow_spend семантический счёт (реальный проект) --")
        sres = spend.scan(BASE_DIR)
        check("P1. ровно 2 аргумента вызова allow_spend=True",
              sres["actual_true_call_sites"] == 2, sres["true_call_sites"])
        check("P2. оба в main.py", all(s["file"] == "main.py" for s in sres["true_call_sites"]))
        check("P3. ЭФФЕКТИВНЫЕ цели трат — make_ipv4 и prolong_make "
              "(разрешено через forwarding-обёртку _pbuy_call)",
              {s["effective_target"] for s in sres["true_call_sites"]} == {"make_ipv4", "prolong_make"},
              [s["effective_target"] for s in sres["true_call_sites"]])
        check("P3b. охватывающие бизнес-функции — buy-confirm и renewal",
              {s["function"] for s in sres["true_call_sites"]} ==
              {"_handle_manager_proxy_buy_confirm_command", "_prenew_execute_renewal"},
              [s["function"] for s in sres["true_call_sites"]])
        check("P4. gate PASS", sres["ok"], sres["problems"])

        # ---- extra adversarial (section 33) ---------------------------------
        print("\n-- доп. адверсариальные проверки --")
        dupm = manifest_for(root, content)
        dupm["files"].append(dict(dupm["files"][0]))
        dp = tmp / "dup.json"
        dp.write_text(dumps(dupm), encoding="utf-8")
        try:
            pkg.build(dp, tmp / "dup.zip", root, None, "X", "Y", True)
            check("дубликат relative_path отклонён", False, "не отклонён")
        except pkg.PackageError as e:
            check("дубликат relative_path отклонён", "duplicate" in str(e), str(e))

        stale = manifest_for(root, content)
        (root / "features" / "prepared_accounts" / "model.py").write_text(
            "PREPARED_READY_MARKER='CHANGED'\n", encoding="utf-8")
        sp2 = tmp / "stale.json"
        sp2.write_text(dumps(stale), encoding="utf-8")
        try:
            pkg.build(sp2, tmp / "stale.zip", root, None, "X", "Y", True)
            check("исходник изменился после генерации манифеста -> отклонено", False, "не отклонено")
        except pkg.PackageError as e:
            check("исходник изменился после генерации манифеста -> отклонено",
                  "changed after manifest" in str(e), str(e))

        # capture-chain classifier must not call a live delegate dead
        mi = ModuleIndex(BASE_DIR / "manager_bot.py", BASE_DIR)
        cls = mi.classify_definition("_send_event_to_user", 4593)
        check("классификатор: захваченное тело B-1 @4593 = ACTIVE_CAPTURED (НЕ кандидат на удаление)",
              cls == "ACTIVE_CAPTURED", cls)

        print("\n" + "=" * 74)
        print(f"RESULT: {'PASS' if FAILED == 0 else 'FAIL'}   OK={OK}  FAIL={FAILED}")
        print("=" * 74)
        return 0 if FAILED == 0 else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
