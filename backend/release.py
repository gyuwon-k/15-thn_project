"""One release identity, full package verification, and atomic activation."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import uuid


def resource_root():
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1])).resolve()


def load_manifest(root=None):
    path = Path(root).resolve() / "release.json" if root else resource_root() / "release.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    build = manifest["build_id"]
    if manifest.get("format") != "pilot-release/2" or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", build):
        raise ValueError("RELEASE_MANIFEST_INVALID")
    if any(manifest.get(component + "_build_id") != build for component in ("frontend", "backend", "launcher")):
        raise ValueError("RELEASE_BUILD_MISMATCH")
    return manifest


def runtime_identity():
    manifest = load_manifest()
    expected = os.environ.get("PILOT_BUILD_ID")
    if expected and expected != manifest["build_id"]:
        raise RuntimeError("RUNTIME_BUILD_MISMATCH")
    return manifest


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic(path, body):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(body, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def seal_release(folder, manifest=None):
    folder = Path(folder).resolve()
    result = dict(manifest or load_manifest(folder))
    result["file_manifest"] = [{"component_id": file.relative_to(folder).as_posix(), "size": file.stat().st_size, "sha256": _hash(file)} for file in sorted(folder.rglob("*")) if file.is_file() and file != folder / "release.json"]
    _atomic(folder / "release.json", result)
    verify_release(folder)
    return result


def verify_release(folder):
    folder = Path(folder).resolve()
    manifest = load_manifest(folder)
    files = manifest.get("file_manifest")
    if not files:
        raise ValueError("RELEASE_FILE_MANIFEST_MISSING")
    paths = set()
    for entry in files:
        relative = entry["component_id"]
        pure = PurePosixPath(relative)
        if relative in paths or pure.is_absolute() or ".." in pure.parts or "\\" in relative or ":" in relative:
            raise ValueError("RELEASE_UNSAFE_COMPONENT")
        paths.add(relative)
        candidate = (folder / relative).resolve()
        if not candidate.is_relative_to(folder) or not candidate.is_file() or candidate.stat().st_size != entry["size"] or _hash(candidate) != entry["sha256"]:
            raise ValueError("RELEASE_COMPONENT_CORRUPT: " + relative)
    actual = {file.relative_to(folder).as_posix() for file in folder.rglob("*") if file.is_file() and file != folder / "release.json"}
    if actual != paths:
        raise ValueError("RELEASE_UNREGISTERED_COMPONENT")
    for identity in (folder / "backend" / "_internal" / "release.json", folder / "frontend" / "build-info.json"):
        if identity.exists() and json.loads(identity.read_text(encoding="utf-8")).get("build_id") != manifest["build_id"]:
            raise ValueError("RELEASE_COMPONENT_BUILD_MISMATCH")
    return manifest


@contextmanager
def installation_lock(installation):
    import msvcrt
    installation = Path(installation).resolve()
    installation.mkdir(parents=True, exist_ok=True)
    with (installation / ".session.lock").open("a+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise RuntimeError("APPLICATION_RUNNING: 실행 중인 PILOT Studio를 종료한 뒤 업데이트해 주세요.")
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def active_release(installation):
    installation = Path(installation).resolve()
    pointer = json.loads((installation / "active-release.json").read_text(encoding="utf-8"))
    release_id = pointer["release_id"]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", release_id):
        raise ValueError("ACTIVE_RELEASE_INVALID")
    folder = (installation / "releases" / release_id).resolve()
    if not folder.is_relative_to(installation / "releases"):
        raise ValueError("ACTIVE_RELEASE_OUTSIDE_INSTALLATION")
    manifest = verify_release(folder)
    if manifest["build_id"] != pointer.get("build_id"):
        raise ValueError("ACTIVE_RELEASE_BUILD_MISMATCH")
    return folder, manifest


def activate_release(installation, release_id, data_dir=None, health_check=None):
    installation = Path(installation).resolve()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", release_id):
        raise ValueError("RELEASE_ID_INVALID")
    with installation_lock(installation):
        candidate = installation / "releases" / release_id
        manifest = verify_release(candidate)
        if manifest["build_id"] != release_id:
            raise ValueError("RELEASE_FOLDER_ID_MISMATCH")
        pointer_path = installation / "active-release.json"
        previous = json.loads(pointer_path.read_text(encoding="utf-8")) if pointer_path.exists() else None
        if previous and previous.get("build_id") == release_id:
            return {"status": "already_active", "build_id": release_id}
        journal = {"format": "pilot-update/2", "status": "checking", "previous": previous, "candidate": release_id, "started_at": datetime.now(timezone.utc).isoformat()}
        journal_path = installation / "update-journal.json"
        _atomic(journal_path, journal)
        try:
            if health_check:
                health_check(candidate, manifest)
            else:
                result_path = installation / ("preflight-" + uuid.uuid4().hex + ".json")
                environment = dict(os.environ, PILOT_BUILD_ID=release_id, PILOT_RELEASE_ROOT=str(candidate))
                if data_dir:
                    environment["PILOT_DATA_DIR"] = str(Path(data_dir).resolve())
                process = subprocess.run([str(candidate / "backend" / "PILOT-Backend.exe"), "--prepare-update", str(result_path)], env=environment, timeout=240, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if process.returncode or not result_path.exists():
                    raise RuntimeError("RELEASE_PREFLIGHT_FAILED")
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if result.get("status") != "ready" or result.get("build_id") != release_id:
                    raise RuntimeError("RELEASE_PREFLIGHT_BUILD_MISMATCH")
                journal["preflight"] = result
            journal["status"] = "verified"
            _atomic(journal_path, journal)
            pointer = {"format": "pilot-active-release/2", "release_id": release_id, "build_id": release_id, "app_version": manifest["app_version"]}
            _atomic(pointer_path, pointer)
            journal["status"] = "completed"
            _atomic(journal_path, journal)
            return {"status": "activated", "build_id": release_id, "app_version": manifest["app_version"]}
        except BaseException as error:
            journal.update(status="failed", error=type(error).__name__, reason=str(error))
            _atomic(journal_path, journal)
            raise


def storage_anchor(current_data_dir):
    current = Path(current_data_dir).resolve()
    if current.parent.name == "restored" and re.fullmatch(r"restore-[0-9a-f]{32}", current.name):
        return current.parent.parent
    return current.parent


def _verified_restored_dir(restored_dir, anchor):
    target = Path(restored_dir).resolve()
    if target.parent != Path(anchor).resolve() / "restored" or not re.fullmatch(r"restore-[0-9a-f]{32}", target.name):
        raise ValueError("RESTORE_DESTINATION_OUTSIDE_MANAGED_ROOT")
    record_path = target / "restore-result.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if record.get("status") != "verified" or record.get("id") != target.name or Path(record.get("data_dir", "")).resolve() != target:
        raise ValueError("RESTORE_NOT_VERIFIED")
    if not (target / "config" / "mariadb.protected.json").is_file() or not (target / "database").is_dir():
        raise ValueError("RESTORE_STORAGE_MISSING")
    return target, record_path


def stage_active_storage(restored_dir, current_data_dir):
    """Select a verified restored store for next start; never switch live DBs."""
    anchor = storage_anchor(current_data_dir)
    target, record_path = _verified_restored_dir(restored_dir, anchor)
    marker = anchor / "active-storage.json"
    value = {"format": "pilot-active-storage/2", "data_dir": str(target), "restore_id": target.name,
             "restore_record_sha256": _hash(record_path), "previous_data_dir": str(Path(current_data_dir).resolve()),
             "activated_at": datetime.now(timezone.utc).isoformat()}
    _atomic(marker, value)
    return {"status": "selected", "restore_id": target.name, "restart_required": True, "data_dir": str(target)}


def selected_data_dir(base_data_dir):
    base = Path(base_data_dir).resolve()
    anchor = storage_anchor(base)
    marker = anchor / "active-storage.json"
    if not marker.is_file():
        return base
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
        if value.get("format") != "pilot-active-storage/2":
            raise ValueError("ACTIVE_STORAGE_FORMAT")
        target, record_path = _verified_restored_dir(value["data_dir"], anchor)
        if value.get("restore_id") != target.name or _hash(record_path) != value.get("restore_record_sha256"):
            raise ValueError("ACTIVE_STORAGE_RECORD_CHANGED")
        return target
    except (OSError, KeyError, ValueError) as error:
        raise RuntimeError("ACTIVE_STORAGE_INVALID: 선택한 복원 저장소를 확인할 수 없습니다. 기존 자료를 보존하고 시작을 중단했습니다.") from error


def default_data_dir():
    base = Path(os.environ.get("PILOT_DATA_DIR", Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PILOT Studio" / "data-v2"))
    return selected_data_dir(base)


@contextmanager
def _browser_runtime_lock(folder, timeout=120):
    import msvcrt
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".prepare.lock").open("a+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0"); stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("BROWSER_RUNTIME_BUSY: 브라우저 준비가 완료되지 않았습니다.")
                time.sleep(.1)
        try:
            yield
        finally:
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def prepare_browser_runtime(source_root, data_dir, build_id):
    """Return a verified writable copy of a Playwright browser-cache directory.

    GPU subprocesses can write beside their executable despite Chromium's log
    switches. Execute a separate build-specific copy under user data; the sealed
    installation remains read-only. Every reused shipped file is compared with
    the current source SHA-256, and extra executable modules are rejected.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", build_id):
        raise ValueError("BROWSER_RUNTIME_BUILD_INVALID")
    source = Path(source_root).resolve()
    if not source.is_dir() or not list(source.glob("chromium*")):
        raise RuntimeError("BROWSER_RUNTIME_SOURCE_MISSING")
    root = Path(data_dir).resolve() / "runtime" / "browsers"
    target = root / build_id
    if target.resolve().is_relative_to(source):
        raise ValueError("BROWSER_RUNTIME_INSIDE_INSTALLATION")
    with _browser_runtime_lock(root):
        inventory = []
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.suffix.lower() == ".log" or ".links" in path.relative_to(source).parts:
                continue
            if not path.resolve().is_relative_to(source):
                raise ValueError("BROWSER_RUNTIME_SOURCE_ESCAPE")
            inventory.append({"path": path.relative_to(source).as_posix(), "sha256": _hash(path), "size": path.stat().st_size})
        if not inventory or not any(entry["path"].lower().endswith(".exe") for entry in inventory):
            raise RuntimeError("BROWSER_RUNTIME_EXECUTABLE_MISSING")
        expected = {"format": "pilot-browser-runtime/1", "build_id": build_id, "files": inventory}
        def verify(folder):
            for entry in inventory:
                file = folder / entry["path"]
                if not file.resolve().is_relative_to(folder.resolve()) or not file.is_file() or file.stat().st_size != entry["size"] or _hash(file) != entry["sha256"]:
                    raise RuntimeError("BROWSER_RUNTIME_CHANGED: " + entry["path"])
            known = {entry["path"] for entry in inventory}
            for file in folder.rglob("*"):
                if file.is_file() and file.suffix.lower() in {".exe", ".dll", ".pyd", ".node"} and file.relative_to(folder).as_posix() not in known:
                    raise RuntimeError("BROWSER_RUNTIME_UNREGISTERED_EXECUTABLE")
        if target.exists():
            marker = target / "runtime-ready.json"
            if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != expected:
                raise RuntimeError("BROWSER_RUNTIME_IDENTITY_CHANGED")
            verify(target)
            return target
        temporary = root / (".prepare-" + uuid.uuid4().hex)
        try:
            shutil.copytree(source, temporary, ignore=shutil.ignore_patterns("*.log", ".links"))
            verify(temporary)
            _atomic(temporary / "runtime-ready.json", expected)
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                if temporary.parent.resolve() != root.resolve() or temporary.is_junction() or temporary.is_symlink():
                    raise RuntimeError("BROWSER_RUNTIME_CLEANUP_UNSAFE")
                shutil.rmtree(temporary)
        return target
