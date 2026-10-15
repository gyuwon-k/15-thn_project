"""Run the supplied-product API workflow against an isolated frozen backend."""
from pathlib import Path
import argparse
import json
import hashlib
import os
import socket
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.release import load_manifest, verify_release, prepare_browser_runtime
from backend.storage import Storage


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--application-smoke", action="store_true", help="Use the clearly labelled synthetic STEP to test confirmation, MP4, and revalidation")
    parser.add_argument("--max-seconds", type=int, default=120)
    parser.add_argument("--backup-output", type=Path)
    args = parser.parse_args()
    if not args.application_smoke and args.source is None:
        parser.error("--source is required for the supplied-product workflow")
    executable = args.backend.resolve()
    manifest = load_manifest(executable.parent / "_internal")
    sealed_release = executable.parent.parent
    if (sealed_release / "release.json").is_file():
        verify_release(sealed_release)
    folder = ROOT / "work" / ("frozen-application" if args.application_smoke else "frozen-hvjb") / uuid.uuid4().hex
    folder.mkdir(parents=True)
    token = uuid.uuid4().hex + uuid.uuid4().hex
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    url = "http://127.0.0.1:" + str(port)
    environment = dict(os.environ)
    for variable in ("PILOT_RESOURCE_DIR", "PILOT_RELEASE_ROOT", "PILOT_MARIADB_ROOT", "PILOT_PARENT_PID", "PLAYWRIGHT_BROWSERS_PATH", "IMAGEIO_FFMPEG_EXE"):
        environment.pop(variable, None)
    environment.update(PILOT_DATA_DIR=str(folder / "data"), PILOT_SESSION_TOKEN=token, PILOT_PORT=str(port), PILOT_BUILD_ID=manifest["build_id"], PYTHONUTF8="1")
    output = folder / "workflow-result.json"
    def api(path, post=False):
        request = urllib.request.Request(url + "/api" + path, data=b"{}" if post else None, headers={"X-Pilot-Token": token, "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=180 if post else 5) as response:
            return json.loads(response.read())
    with (folder / "backend.log").open("wb") as log:
        process = subprocess.Popen([str(executable), "--server"], env=environment, stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 90
            while True:
                if process.poll() is not None:
                    raise RuntimeError("FROZEN_BACKEND_START_FAILED")
                try:
                    health = api("/health")
                    assert health["build_id"] == manifest["build_id"]
                    assert health["session_fingerprint"] == hashlib.sha256(token.encode("utf-8")).hexdigest()
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise RuntimeError("FROZEN_BACKEND_START_TIMEOUT")
                    time.sleep(.25)
            print(json.dumps({"build_id": manifest["build_id"], "report": str(output)}, ensure_ascii=False), flush=True)
            workflow = "verify_application_workflow.py" if args.application_smoke else "verify_hvjb_workflow.py"
            arguments = [sys.executable, "-X", "utf8", str(ROOT / "scripts" / workflow), "--url", url, "--report", str(output)]
            if not args.application_smoke:
                arguments += ["--source", str(args.source.resolve()), "--max-seconds", str(args.max_seconds)]
            result = subprocess.run(arguments, env=environment, timeout=5000)
            if result.returncode == 0 and args.backup_output:
                report = json.loads(output.read_text(encoding="utf-8"))
                if not report.get("sequence_generated"):
                    raise RuntimeError("BACKUP_REQUIRES_GENERATED_REVIEW_DRAFT")
                backup = api("/backups", True)
                destination = args.backup_output.resolve()
                destination.parent.mkdir(parents=True, exist_ok=True)
                request = urllib.request.Request(url + "/api/backups/" + backup["id"], headers={"X-Pilot-Token": token})
                with urllib.request.urlopen(request, timeout=180) as response, destination.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
                validated = Storage.validate_backup(destination)
                digest = hashlib.sha256(destination.read_bytes()).hexdigest()
                if digest != backup["sha256"]:
                    raise RuntimeError("DOWNLOADED_BACKUP_HASH_MISMATCH")
                report["review_backup"] = {"path": str(destination), "sha256": digest, "bytes": destination.stat().st_size, "table_counts": validated["table_counts"], "verified_archive": True, "approved_assembly": report.get("verified_recommendation", False)}
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"review_backup": str(destination), "verified_archive": True}, ensure_ascii=False), flush=True)
            if result.returncode == 0:
                report = json.loads(output.read_text(encoding="utf-8"))
                try:
                    runtime = prepare_browser_runtime(executable.parent / "_internal" / "browsers", folder / "data", manifest["build_id"])
                    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(runtime)
                    browser_log = folder / "browser-smoke.log"
                    os.environ["CHROME_LOG_FILE"] = str(browser_log)
                    from playwright.sync_api import sync_playwright
                    with sync_playwright() as browser_tools:
                        browser = browser_tools.chromium.launch(headless=True, args=["--enable-logging", "--log-file=" + str(browser_log)])
                        page = browser.new_page(viewport={"width": 1440, "height": 960}, extra_http_headers={"X-Pilot-Token": token})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(url + "/?project=" + report["project_id"], wait_until="networkidle", timeout=120000)
                        page.wait_for_selector("canvas", timeout=60000)
                        page.evaluate("document.fonts.ready")
                        page.wait_for_timeout(2000)
                        screenshot = folder / "frozen-hvjb-ui.png"
                        page.screenshot(path=str(screenshot), full_page=True)
                        report["frozen_ui"] = {"screenshot": str(screenshot), "title": page.title(), "canvas_count": page.locator("canvas").count(), "sequence_cards": page.locator(".sequence-card").count(), "javascript_errors": errors, "text": page.locator("body").inner_text()[:10000]}
                        browser.close()
                except Exception as error:
                    report["frozen_ui"] = {"capture_error": type(error).__name__ + ": " + str(error)}
                output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                if (sealed_release / "release.json").is_file():
                    verify_release(sealed_release)
                    report["sealed_payload_unchanged_after_workflow"] = True
                    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            return result.returncode
        finally:
            if process.poll() is None:
                try:
                    api("/shutdown", True)
                    process.wait(30)
                except Exception:
                    process.terminate()
                    process.wait(15)


if __name__ == "__main__":
    raise SystemExit(main())
