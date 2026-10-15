"""Build a new immutable release; all versions and paths come from release.json."""
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.release import load_manifest, seal_release, verify_release


def run(arguments, cwd=ROOT, env=None):
    subprocess.run([str(value) for value in arguments], cwd=cwd, env=env, check=True)


def compile_installer(output, manifest, compiler):
    if compiler is None or not compiler.is_file():
        raise RuntimeError("INNO_COMPILER_MISSING: supply --inno or add ISCC.exe to PATH")
    release = output / "releases" / manifest["build_id"]
    sealed = verify_release(release)
    # Inno's file compressor cannot read some source paths over MAX_PATH even
    # though Python can. A short, temporary junction preserves the exact sealed
    # payload without copying or changing it, and avoids a global drive mapping.
    alias = ROOT / ("p" + uuid.uuid4().hex[:8])
    if os.name != "nt":
        raise RuntimeError("WINDOWS_INSTALLER_BUILD_REQUIRED")
    import _winapi
    _winapi.CreateJunction(str(output.resolve()), str(alias))
    try:
        longest = max(len(str(alias / "releases" / manifest["build_id"] / entry["component_id"])) for entry in sealed["file_manifest"])
        if longest >= 260:
            raise RuntimeError("INSTALLER_SOURCE_PATH_TOO_LONG: move this source project to a shorter directory")
        run([compiler, "/DMyAppVersion=" + manifest["app_version"], "/DBuildId=" + manifest["build_id"], "/DPackageSource=" + str(alias), "/DOutputRoot=" + str(ROOT / "dist"), ROOT / "packaging" / "installer.iss"])
    finally:
        if alias.parent.resolve() != ROOT.resolve() or not alias.is_junction() or alias.resolve() != output.resolve():
            raise RuntimeError("INSTALLER_ALIAS_CHANGED: refusing cleanup")
        os.rmdir(alias)  # Remove the junction itself, never its target or contents.
    verify_release(release)
    return ROOT / "dist" / ("PILOT-Studio-Setup-" + manifest["app_version"] + ".exe")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--inno", type=Path, default=shutil.which("ISCC.exe"))
    parser.add_argument("--skip-frontend", action="store_true")
    parser.add_argument("--skip-stage", action="store_true")
    parser.add_argument("--skip-installer", action="store_true")
    parser.add_argument("--installer-only", action="store_true", help="Retry installer compilation using an already sealed release")
    args = parser.parse_args()
    manifest = load_manifest(ROOT)
    build = manifest["build_id"]
    if args.installer_only:
        output = ROOT / "dist" / build
        installer = compile_installer(output, manifest, args.inno)
        sealed = verify_release(output / "releases" / build)
        report = {"build_id": build, "app_version": manifest["app_version"], "release_dir": str(output), "files": len(sealed["file_manifest"]), "installer": str(installer), "created_at": datetime.now(timezone.utc).isoformat()}
        (output / "build-report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    environment = dict(os.environ, VITE_BUILD_ID=build, VITE_APP_VERSION=manifest["app_version"])
    if not args.skip_frontend:
        run(["npm.cmd", "run", "build"], ROOT / "frontend", environment)
    frontend = ROOT / "frontend" / "dist"
    if not (frontend / "index.html").is_file():
        raise RuntimeError("FRONTEND_BUILD_MISSING")
    # Skipping compilation must never relabel an older UI as this release.
    identity_path = frontend / "build-info.json"
    if args.skip_frontend:
        if not identity_path.is_file() or json.loads(identity_path.read_text(encoding="utf-8")).get("build_id") != build:
            raise RuntimeError("STALE_FRONTEND_BUILD: rebuild frontend for this manifest")
    else:
        identity_path.write_text(json.dumps({"build_id": build, "app_version": manifest["app_version"]}), encoding="utf-8")
    if not args.skip_stage:
        run([args.python, "-X", "utf8", "scripts/stage_runtime.py"])
    unique = build + "-" + uuid.uuid4().hex[:8]
    work = ROOT / "build" / unique
    products = work / "products"
    run([args.python, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", products, "--workpath", work / "backend", ROOT / "packaging" / "backend.spec"])
    run([args.python, "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", products, "--workpath", work / "launcher", ROOT / "packaging" / "launcher.spec"])
    staging = work / "installation"
    release = staging / "releases" / build
    release.mkdir(parents=True)
    shutil.copytree(products / "PILOT-Backend", release / "backend")
    shutil.copytree(frontend, release / "frontend")
    electron = ROOT / "desktop" / "node_modules" / "electron" / "dist"
    if not (electron / "electron.exe").is_file():
        raise RuntimeError("ELECTRON_RUNTIME_MISSING: run npm ci in desktop")
    shutil.copytree(electron, release / "desktop", ignore=shutil.ignore_patterns("*.log"))
    (release / "desktop" / "electron.exe").rename(release / "desktop" / "PILOT-Studio-Desktop.exe")
    appdir = release / "desktop" / "resources" / "app"
    appdir.mkdir(parents=True)
    for name in ("main.cjs", "preload.cjs", "package.json"):
        shutil.copy2(ROOT / "desktop" / name, appdir / name)
    desktop_package = json.loads((appdir / "package.json").read_text(encoding="utf-8"))
    desktop_package["version"] = manifest["app_version"]
    (appdir / "package.json").write_text(json.dumps(desktop_package, indent=2), encoding="utf-8")
    shutil.copy2(ROOT / "release.json", release / "release.json")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", release / "THIRD_PARTY_NOTICES.md")
    frontend_licenses = release / "third_party_licenses" / "frontend"
    # Preserve the installed runtime dependencies' original notice files.
    # Copy direct packages and the React scheduler runtime; Electron/Chromium,
    # MariaDB, Python and the font ship their own notices beside the binaries.
    for package in ("react", "react-dom", "scheduler", "three", "lucide-react"):
        package_root = ROOT / "frontend" / "node_modules" / package
        metadata = package_root / "package.json"
        if not metadata.is_file():
            raise RuntimeError("FRONTEND_LICENSE_METADATA_MISSING: " + package)
        destination = frontend_licenses / package
        destination.mkdir(parents=True, exist_ok=True)
        shutil.copy2(metadata, destination / "package.json")
        for path in package_root.iterdir():
            if path.is_file() and path.name.lower().startswith(("license", "copying", "notice")):
                shutil.copy2(path, destination / path.name)
    shutil.copy2(products / "PILOT-Studio.exe", staging / "PILOT-Studio.exe")
    manifest["launcher_sha256"] = hashlib.sha256((staging / "PILOT-Studio.exe").read_bytes()).hexdigest()
    sealed = seal_release(release, manifest)
    verify_release(release)
    output = ROOT / "dist" / build
    if output.exists():
        raise RuntimeError("IMMUTABLE_RELEASE_ALREADY_EXISTS: increment build_id before rebuilding")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not staging.resolve().is_relative_to(ROOT / "build") or not output.resolve().is_relative_to(ROOT / "dist"):
        raise RuntimeError("BUILD_OUTPUT_OUTSIDE_PROJECT")
    shutil.move(str(staging), str(output))
    build_report = {"build_id": build, "app_version": manifest["app_version"], "release_dir": str(output), "files": len(sealed["file_manifest"]), "created_at": datetime.now(timezone.utc).isoformat()}
    if not args.skip_installer:
        build_report["installer"] = str(compile_installer(output, manifest, args.inno))
    (output / "build-report.json").write_text(json.dumps(build_report, indent=2), encoding="utf-8")
    print(json.dumps(build_report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
