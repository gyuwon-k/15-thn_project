"""Native old→new activation, managed-data preservation and failed-update proof.

Runs only in a fresh workspace test directory. It does not run the installer,
modify its registration, change shortcuts, or touch the user's live data.
The previous native backend must be a separately built, earlier artifact.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import uuid
import urllib.request
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.release import load_manifest, seal_release, active_release


def main():
    package = Path(sys.argv[1]).resolve()
    previous_backend = Path(sys.argv[2]).resolve()
    manifest = next(load_manifest(p) for p in (package / "releases").iterdir() if p.is_dir())
    previous = load_manifest(previous_backend / "_internal")
    if manifest["build_id"] == previous["build_id"]:
        raise RuntimeError("DIFFERENT_NATIVE_BUILDS_REQUIRED")
    folder = ROOT / "work" / "update-verification" / uuid.uuid4().hex
    installation = folder / "installation"
    data = folder / "data"
    installation.mkdir(parents=True)
    launcher = installation / "PILOT-Studio.exe"
    shutil.copy2(package / "PILOT-Studio.exe", launcher)
    current = installation / "releases" / manifest["build_id"]
    shutil.copytree(package / "releases" / manifest["build_id"], current)
    old = installation / "releases" / previous["build_id"]
    shutil.copytree(previous_backend, old / "backend")
    previous["launcher_sha256"] = hashlib.sha256(launcher.read_bytes()).hexdigest()
    seal_release(old, previous)
    environment = dict(os.environ, PILOT_DATA_DIR=str(data))
    for variable in ("PILOT_BUILD_ID", "PILOT_RELEASE_ROOT", "PILOT_PARENT_PID", "PILOT_RESOURCE_DIR", "PILOT_MARIADB_ROOT", "PLAYWRIGHT_BROWSERS_PATH", "IMAGEIO_FFMPEG_EXE"):
        environment.pop(variable, None)

    def activate(build, expected=0):
        process = subprocess.run([str(launcher), "--activate", build], env=environment, timeout=300, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if process.returncode != expected:
            raise RuntimeError("ACTIVATION_EXIT_" + str(process.returncode))

    @contextmanager
    def server(release):
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        token = uuid.uuid4().hex + uuid.uuid4().hex
        base = "http://127.0.0.1:" + str(port)
        build = load_manifest(release)["build_id"]
        env = dict(environment, PILOT_PORT=str(port), PILOT_SESSION_TOKEN=token, PILOT_BUILD_ID=build)
        log = (folder / (build + ".log")).open("wb")
        process = subprocess.Popen([str(release / "backend" / "PILOT-Backend.exe"), "--server"], env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def api(path, body=None):
            request = urllib.request.Request(base + "/api" + path, data=json.dumps(body).encode() if body is not None else None,
                headers={"X-Pilot-Token": token, "Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read())
        api.base_url=base;api.token=token
        deadline = time.monotonic() + 90
        try:
            while True:
                if process.poll() is not None:
                    raise RuntimeError("NATIVE_SERVER_EXIT")
                try:
                    health = api("/health")
                    if health["build_id"] != build:
                        raise RuntimeError("NATIVE_BUILD_MISMATCH")
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("NATIVE_SERVER_START_TIMEOUT")
                    time.sleep(.2)
            yield api, health
        finally:
            if process.poll() is None:
                try:
                    api("/shutdown", {})
                    process.wait(30)
                except Exception:
                    process.terminate()
                    process.wait(10)
            log.close()

    activate(previous["build_id"])
    with server(old) as (api, before_health):
        project = api("/projects", {"name": "업데이트 보존 검증", "part_number": "KEEP-00017", "vehicle": "시험 차종"})
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer,STEPControl_AsIs
        fixture=folder/"update-fixture.step";writer=STEPControl_Writer()
        writer.Transfer(BRepPrimAPI_MakeBox(4,5,6).Shape(),STEPControl_AsIs);writer.Write(str(fixture))
        with fixture.open("rb") as stream:
            response=httpx.post(api.base_url+"/api/projects/"+project["id"]+"/import",headers={"X-Pilot-Token":api.token},data={"expected_revision":project["revision"]},files={"assembly":(fixture.name,stream)},timeout=180)
            response.raise_for_status();job=response.json()
        def wait_job(call,job):
            deadline=time.monotonic()+300
            while time.monotonic()<deadline:
                value=call("/jobs/"+job["id"])
                if value["status"]=="succeeded":return
                if value["status"] in ("failed","interrupted","canceled"):raise RuntimeError(value)
                time.sleep(.5)
            raise TimeoutError("UPDATE_IMPORT_TIMEOUT")
        wait_job(api,job);project=api("/projects/"+project["id"])
        backup_before = api("/backups", {})
    before_pointer = json.loads((installation / "active-release.json").read_text(encoding="utf-8"))
    activate(manifest["build_id"])
    with server(current) as (api, after_health):
        preserved = api("/projects/" + project["id"])
        assert preserved["id"] == project["id"] and preserved["name"] == project["name"] and preserved["part_number"] == "KEEP-00017"
        assert preserved["row_version"] == project["row_version"]
        backups = api("/settings")["backups"]
        assert any(backup["id"] == backup_before["id"] for backup in backups)
        assert len(backups) >= 2, "Update preflight must back up existing projects"
        reprocess_required=any(previous.get(key)!=manifest.get(key) for key in ('preprocess_version','matching_version','geometry_backend_version'))
        assert preserved["model_reprocess_required"] is reprocess_required
        if reprocess_required:
            response=httpx.post(api.base_url+"/api/projects/"+project["id"]+"/generate",headers={"X-Pilot-Token":api.token},json={"expected_revision":preserved["revision"]})
            assert response.status_code==422 and response.json()["code"]=="MODEL_REPROCESS_REQUIRED"
        wait_job(api,api("/projects/"+project["id"]+"/reprocess",{"expected_revision":preserved["revision"]}))
        reprocessed=api("/projects/"+project["id"])
        assert not reprocessed["model_reprocess_required"]
        assert reprocessed["model"]["source"]["sha256"]==project["model"]["source"]["sha256"]
        assert reprocessed["model"]["processing_versions"]["preprocess_version"]==manifest["preprocess_version"]
        assert any(e.get("kind")=="model_replaced" for e in api("/projects/"+project["id"]+"/history")["events"])
    failed_id = "pilot-invalid-update-fixture"
    failed = installation / "releases" / failed_id
    (failed / "backend").mkdir(parents=True)
    (failed / "backend" / "PILOT-Backend.exe").write_bytes(b"This is deliberately not a Windows executable.")
    invalid = dict(manifest, build_id=failed_id)
    for component in ("frontend", "backend", "launcher"):
        invalid[component + "_build_id"] = failed_id
    seal_release(failed, invalid)
    pointer_before_failure = (installation / "active-release.json").read_bytes()
    activate(failed_id, expected=1)
    assert (installation / "active-release.json").read_bytes() == pointer_before_failure
    assert active_release(installation)[1]["build_id"] == manifest["build_id"]
    with server(current) as (api, _):
        assert api("/projects/" + project["id"])["part_number"] == "KEEP-00017"
    report = {"status": "passed", "previous_build_id": before_health["build_id"], "new_build_id": after_health["build_id"],
        "old_pointer": before_pointer, "new_pointer": json.loads(pointer_before_failure), "preserved_project_id": project["id"],
        "preserved_row_version": preserved["row_version"], "previous_backup_id": backup_before["id"], "backup_count": len(backups),
        "failed_update_preserved_active_release": True, "failed_update_preserved_data": True, "test_directory": str(folder),
        "old_model_requires_reprocessing":reprocess_required,"managed_original_reprocessed_by_new_version":True,"previous_model_archived":True,
        "scope": "native launcher/backend activation; isolated directory, no installer registration changes"}
    output = ROOT / "work" / "native-update-verification.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
