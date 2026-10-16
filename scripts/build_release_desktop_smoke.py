"""Launch the packaged desktop twice and prove its immutable payload stays intact."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.release import load_manifest, verify_release


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("package", type=Path)
    parser.add_argument("--data", type=Path)
    parser.add_argument("--model", action="store_true", help="Exercise the packaged Electron WebGL view and simulation before restart")
    parser.add_argument("--review-plan", action="store_true", help="Check the supplied HVJB review plan's 55 cards and blocked second-step playback")
    parser.add_argument("--expected-trace-sha256", help="Download the search trace from the real desktop link and check this expected SHA-256")
    args = parser.parse_args()
    package = args.package.resolve()
    releases = [p for p in (package / "releases").iterdir() if p.is_dir()]
    if len(releases) != 1:
        raise RuntimeError("SMOKE_REQUIRES_ONE_RELEASE_PACKAGE")
    release = releases[0]
    manifest = load_manifest(release)
    verify_release(release)
    folder = ROOT / "work" / "frozen-desktop" / uuid.uuid4().hex
    folder.mkdir(parents=True)
    data = args.data.resolve() if args.data else folder / "data"
    environment = dict(os.environ)
    for variable in ("PILOT_RESOURCE_DIR", "PILOT_RELEASE_ROOT", "PILOT_MARIADB_ROOT", "PILOT_PARENT_PID", "PILOT_BUILD_ID", "PLAYWRIGHT_BROWSERS_PATH", "IMAGEIO_FFMPEG_EXE"):
        environment.pop(variable, None)
    environment["PILOT_DATA_DIR"] = str(data)
    launcher = package / "PILOT-Studio.exe"
    def run(arguments):
        result = subprocess.run([str(launcher), *arguments], env=environment, timeout=300, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("PACKAGED_DESKTOP_EXIT_" + str(result.returncode))
    run(["--activate", manifest["build_id"]])
    launches = []
    model_evidence = None
    if args.model:
        from playwright.sync_api import sync_playwright
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0)); debug_port = reservation.getsockname()[1]
        process = subprocess.Popen([str(launcher), "--remote-debugging-port=" + str(debug_port)], env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            endpoint = "http://127.0.0.1:" + str(debug_port)
            deadline = time.monotonic() + 120
            while True:
                if process.poll() is not None: raise RuntimeError("ELECTRON_MODEL_START_FAILED")
                try:
                    with urllib.request.urlopen(endpoint + "/json/version", timeout=2) as response: response.read()
                    break
                except OSError:
                    if time.monotonic() > deadline: raise RuntimeError("ELECTRON_DEBUG_START_TIMEOUT")
                    time.sleep(.25)
            with sync_playwright() as automation:
                browser = automation.chromium.connect_over_cdp(endpoint)
                context = browser.contexts[0]
                page = context.pages[0] if context.pages else context.wait_for_event("page", timeout=120000)
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.locator("nav button").filter(has_text="모델·조립 순서").click(timeout=120000)
                page.locator(".sequence-card").first.wait_for(timeout=60000)
                page.locator("canvas").wait_for(timeout=60000)
                play = page.get_by_role("button", name="재생", exact=True)
                play.click(timeout=60000)
                page.wait_for_timeout(1800)
                review_evidence = None
                if args.review_plan:
                    assert page.locator(".sequence-card").count() == 55
                    assert "검증 경로 1/55단계" in page.locator("body").inner_text()
                    page.locator(".sequence-card").nth(1).click()
                    page.get_by_text("이 단계는 재생이 보류되었습니다. 완성 위치에서 부품과 진단을 확인해 주세요.", exact=True).wait_for(timeout=10000)
                    assert page.get_by_role("button", name="일시정지", exact=True).count() == 0
                    assert "검증된 경로 재생" not in page.locator(".scene-selection").inner_text()
                    review_evidence = {"cards": 55, "verified_prefix": 1, "blocked_second_step_returns_to_static_inspection": True}
                screenshot = folder / "packaged-electron-model.png"
                page.screenshot(path=str(screenshot), full_page=True)
                model_evidence = {"screenshot": str(screenshot), "canvas_count": page.locator("canvas").count(), "sequence_cards": page.locator(".sequence-card").count(), "javascript_errors": errors, "simulation_played": True}
                if review_evidence is not None: model_evidence["review_plan"] = review_evidence
                if args.expected_trace_sha256:
                    page.locator('.diagnostics-panel summary').click()
                    link=page.get_by_role('link',name=re.compile('전체 탐색 기록 내려받기'))
                    with page.expect_download(timeout=120000) as download_info:
                        link.click()
                    downloaded=folder/'downloaded-search-attempts.json'
                    download_info.value.save_as(str(downloaded))
                    trace_bytes=downloaded.read_bytes();digest=hashlib.sha256(trace_bytes).hexdigest()
                    assert digest==args.expected_trace_sha256
                    trace=json.loads(trace_bytes)
                    model_evidence['search_trace_download']={'sha256':digest,'attempt_count':len(trace['attempts']),'bytes':len(trace_bytes),'download_link_verified':True}
                assert not errors
                page.close()
                process.wait(40)
            verify_release(release)
            model_evidence["sealed_payload_unchanged"] = True
        finally:
            if process.poll() is None:
                try:
                    with sync_playwright() as cleanup:
                        owned_browser = cleanup.chromium.connect_over_cdp(endpoint, timeout=5000)
                        for owned_context in owned_browser.contexts:
                            for owned_page in list(owned_context.pages):
                                owned_page.close()
                    process.wait(30)
                except Exception:
                    process.terminate(); process.wait(15)
    for number in (1, 2):
        output = folder / ("launch-" + str(number) + ".json")
        environment["PILOT_SMOKE_RESULT"] = str(output)
        run(["--smoke"])
        result = json.loads(output.read_text(encoding="utf-8"))
        assert result["status"] == "ready" and result["build_id"] == manifest["build_id"]
        verify_release(release)
        launches.append({"attempt": number, "status": "ready", "build_id": result["build_id"], "sealed_payload_unchanged": True, "evidence": str(output)})
    report = {"status": "passed", "build_id": manifest["build_id"], "data_dir": str(data), "model_view": model_evidence, "launches": launches}
    destination = ROOT / "work" / "final-desktop-restart-result.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
