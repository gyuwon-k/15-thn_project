"""Shared-scene deterministic MP4 and portable web packages (REQ-VID-001..006)."""
from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import html
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import Callable
from urllib.parse import quote, urlparse
import uuid
import zipfile

RENDERER_VERSION = "pilot-playwright-timeline-v1"
PRESETS = {
    "production": {"width": 1920, "height": 1080, "fps": 30, "crf": 20, "codec": "libx264"},
    "quick": {"width": 640, "height": 360, "fps": 12, "crf": 26, "codec": "libx264"},
}


class RenderCanceled(RuntimeError):
    pass


def _hash(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _ffmpeg() -> str:
    explicit = os.environ.get("PILOT_FFMPEG")
    if explicit and Path(explicit).is_file():
        return explicit
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        bundled = list((base / "ffmpeg").glob("*.exe"))
        if bundled:
            return str(bundled[0])
    import imageio_ffmpeg
    return imageio_ffmpeg.get_ffmpeg_exe()


def _browser_environment() -> None:
    if getattr(sys, "frozen", False):
        browsers = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "browsers"
        if browsers.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    elif "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
        browsers = Path(__file__).resolve().parent.parent / "data" / "browsers"
        if browsers.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)


def render_video(base_url: str, snapshot: dict, output_dir: Path | str,
                 preset: str | dict = "production", progress: Callable | None = None,
                 cancel: Callable[[], bool] | None = None) -> dict:
    """Capture the same immutable snapshot/scene as preview, then verify decoding.

    Frontend contract: /?render=<id>, #render-root sized to viewport, and
    window.__PILOT_RENDER__ = {ready, duration, async setTime(seconds)}.
    A draft is allowed only as a visibly watermarked preview and may not publish.
    """
    settings = dict(PRESETS[preset]) if isinstance(preset, str) else dict(preset)
    width, height, fps = (int(settings[key]) for key in ("width", "height", "fps"))
    if width < 64 or height < 64 or width % 2 or height % 2 or not 1 <= fps <= 60:
        raise ValueError("영상 크기는 64 이상 짝수이며 FPS는 1~60이어야 합니다.")
    snapshot_id = snapshot.get("id", snapshot.get("snapshot_id"))
    if not snapshot_id:
        raise ValueError("불변 스냅샷 ID가 필요합니다.")
    status = snapshot.get("status", (snapshot.get("state") or {}).get("status", "draft"))
    draft = status != "confirmed"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    render_id = "render-" + uuid.uuid4().hex[:12]
    staging = output_dir / ("." + render_id + ".partial")
    staging.mkdir()
    target = output_dir / render_id
    video_temp = staging / "assembly.mp4"
    stderr_path = staging / "encoder.log"
    ffmpeg = _ffmpeg()
    started = time.perf_counter()
    process = None

    def check_cancel():
        if cancel and cancel():
            raise RenderCanceled("영상 생성이 취소되었습니다. 미완성 파일은 정상 출력에 등록되지 않았습니다.")

    def report(value, message):
        if progress:
            progress(value / 100, message)

    try:
        check_cancel()
        _browser_environment()
        from playwright.sync_api import sync_playwright
        with sync_playwright() as playwright:
            report(1, "공통 타임라인과 한글 글꼴 준비")
            browser = playwright.chromium.launch(headless=True, args=["--hide-scrollbars"])
            try:
                page = browser.new_page(viewport={"width": width, "height": height}, device_scale_factor=1)
                page.set_default_timeout(120000)
                page.goto(base_url.rstrip("/") + "/?render=" + quote(str(snapshot_id), safe=""), wait_until="domcontentloaded")
                page.wait_for_function("window.__PILOT_RENDER__ && window.__PILOT_RENDER__.ready === true")
                page.evaluate("async () => { await document.fonts.ready; }")
                duration = float(page.evaluate("window.__PILOT_RENDER__.duration"))
                if not math.isfinite(duration) or duration <= 0:
                    raise ValueError("타임라인 길이가 유효하지 않습니다.")
                frames = max(1, int(math.ceil(duration * fps)))
                root = page.locator("#render-root")
                if root.count() != 1:
                    raise ValueError("공통 렌더 장면 #render-root가 없습니다.")
                clip = root.bounding_box()
                if not clip or clip["width"] <= 0 or clip["height"] <= 0:
                    raise ValueError("공통 렌더 장면의 표시 영역이 유효하지 않습니다.")
                # Defense in depth: the exporter itself guarantees draft marking.
                if draft:
                    page.evaluate("""() => { if(document.querySelector('.draft-watermark')) return; const mark=document.createElement('div');mark.textContent='DRAFT · 미확정 미리보기';mark.id='export-draft';Object.assign(mark.style,{position:'absolute',inset:'42% 0 auto',textAlign:'center',color:'rgba(255,185,95,.85)',fontSize:'clamp(24px,4vw,70px)',fontWeight:'900',zIndex:'99999',pointerEvents:'none',transform:'rotate(-12deg)',textShadow:'0 2px 6px #000'});document.querySelector('#render-root').appendChild(mark); }""")
                args = [ffmpeg, "-hide_banner", "-y", "-f", "image2pipe", "-vcodec", "png", "-framerate", str(fps),
                        "-i", "pipe:0", "-an", "-c:v", settings.get("codec", "libx264"), "-preset", "veryfast",
                        "-crf", str(settings.get("crf", 22)), "-vf", f"scale={width}:{height}", "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart", "-frames:v", str(frames), str(video_temp)]
                with stderr_path.open("wb") as encoder_log:
                    process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=encoder_log,
                                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    for frame in range(frames):
                        check_cancel()
                        seconds = min(frame / fps, max(0., duration - .000001))
                        # setTime resolves only when the frontend scene is drawn.
                        # A fixed viewport clip avoids Locator's per-frame layout
                        # stability waits, which repeatedly drew large CAD scenes.
                        page.evaluate("async (seconds) => { await window.__PILOT_RENDER__.setTime(seconds); }", seconds)
                        png = page.screenshot(clip=clip, type="png", animations="disabled")
                        if process.poll() is not None:
                            raise RuntimeError("MP4 인코더가 프레임 처리 중 종료되었습니다.")
                        process.stdin.write(png)
                        if frame in {0, frames//2, frames-1}:
                            (staging / f"frame-{frame:06d}.png").write_bytes(png)
                        if frame % max(1, fps//2) == 0 or frame == frames-1:
                            report(5 + 88 * (frame+1)/frames, f"프레임 {frame+1:,}/{frames:,} · {seconds:.1f}/{duration:.1f}초")
                    process.stdin.close()
                    while process.poll() is None:
                        check_cancel()
                        time.sleep(.1)
                    if process.returncode != 0 or not video_temp.is_file() or video_temp.stat().st_size < 100:
                        raise RuntimeError("인코더가 재생 가능한 MP4를 생성하지 못했습니다.")
                check_cancel()
                report(95, "MP4 전체 디코딩 검증")
                checked = subprocess.run([ffmpeg, "-v", "error", "-nostats", "-i", str(video_temp),
                                          "-progress", "pipe:1", "-f", "null", "-"],
                                         capture_output=True, timeout=max(60, int(duration*4)),
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if checked.returncode:
                    raise RuntimeError("생성 MP4 디코딩 검증에 실패했습니다: " + checked.stderr.decode(errors="replace")[-1000:])
                decoded = [int(line.partition("=")[2].strip()) for line in checked.stdout.decode(errors="replace").splitlines()
                           if line.startswith("frame=")]
                if not decoded or decoded[-1] != frames:
                    raise RuntimeError(f"MP4 프레임 수 불일치: 예상 {frames}, 실제 {decoded[-1] if decoded else '미확인'}")
                result = {"id": render_id, "status": "succeeded", "snapshot_id": str(snapshot_id),
                          "snapshot_status": status, "draft": draft, "publication_status": "not_published",
                          "preset": settings, "duration": frames/fps, "timeline_duration": duration, "frame_count": frames,
                          "video_path": str((target / "assembly.mp4").resolve()), "sha256": sha256(video_temp.read_bytes()).hexdigest(),
                          "bytes": video_temp.stat().st_size, "snapshot_hash": _hash(snapshot),
                          "renderer_version": RENDERER_VERSION, "browser_version": browser.version,
                          "elapsed_seconds": round(time.perf_counter()-started, 3), "created_at": datetime.now(timezone.utc).isoformat(),
                          "verification": "ffmpeg full-video decode succeeded", "decoded_frame_count": decoded[-1],
                          "preview_frames": [str((target/p.name).resolve()) for p in sorted(staging.glob("frame-*.png"))]}
                _json(staging / "render.json", result)
                check_cancel()
                staging.replace(target)
                report(100, "MP4 생성 및 디코딩 확인 완료")
                return result
            finally:
                browser.close()
    except Exception as exc:
        if process and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        # Diagnostic remains, but partial video can never masquerade as output.
        if video_temp.exists():
            video_temp.unlink()
        failure = {"id": render_id, "status": "canceled" if isinstance(exc, RenderCanceled) else "failed",
                   "snapshot_id": str(snapshot_id), "reason": str(exc), "renderer_version": RENDERER_VERSION}
        _json(staging / "failure.json", failure)
        raise


def web_package(render: dict, snapshot: dict, target_dir: Path | str) -> dict:
    """Create only video/HTML metadata, never CAD. Local packaging is not publish."""
    if render.get("status") != "succeeded" or render.get("draft") or snapshot.get("status") != "confirmed":
        raise ValueError("확정 스냅샷에서 성공적으로 생성한 MP4만 Web 배포 패키지를 만들 수 있습니다.")
    from .desktop_storage import resolve_path
    video = Path(resolve_path(render["video_path"]))
    if not video.is_file() or sha256(video.read_bytes()).hexdigest() != render.get("sha256"):
        raise ValueError("영상이 없거나 출력 해시가 일치하지 않습니다. 해당 확정본으로 다시 렌더링하세요.")
    identity = snapshot.get("id", snapshot.get("snapshot_id"))
    if str(identity) != str(render.get("snapshot_id")):
        raise ValueError("영상과 확정 스냅샷 버전이 일치하지 않습니다.")
    folder = Path(target_dir) / (render["id"] + "-web")
    folder.mkdir(parents=True, exist_ok=True)
    project = snapshot.get("project") or (snapshot.get("state") or {}).get("project") or {}
    title = str(project.get("name") or project.get("item_name") or project.get("item") or "PILOT 조립 공법서")
    part = str(project.get("product_no") or project.get("part_number") or project.get("product_code") or "미입력")
    model = str(project.get("vehicle_model") or project.get("vehicle") or "미입력")
    page = f'''<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{html.escape(title)} | PILOT</title><style>body{{margin:0;background:#0b101a;color:#e6edf8;font:16px "Malgun Gothic",system-ui,sans-serif}}main{{max-width:1200px;margin:50px auto;padding:24px}}small{{color:#80d9b9}}h1{{margin:12px 0;font-size:32px}}p{{color:#9ba8be;line-height:1.7}}video{{display:block;width:100%;border-radius:18px;background:#040609;margin-top:28px}}footer{{font-size:12px;margin-top:20px;color:#748298}}</style></head><body><main><small>PILOT · DIGITAL ASSEMBLY</small><h1>{html.escape(title)}</h1><p>차종 {html.escape(model)} · 완제품 품번 {html.escape(part)}<br>확정 개정 {html.escape(str(identity))}</p><video controls preload="metadata" playsinline src="assembly.mp4">MP4 동영상 재생을 지원하는 브라우저가 필요합니다.</video><footer>출력 {html.escape(render['id'])} · 생성 {html.escape(render.get('created_at',''))}<br>검토된 공정과 명시된 분석 범위에 대한 영상입니다.</footer></main></body></html>'''
    (folder / "index.html").write_text(page, encoding="utf-8")
    shutil.copy2(video, folder / "assembly.mp4")
    public = {key: render.get(key) for key in ("id", "snapshot_id", "sha256", "preset", "duration", "created_at", "renderer_version")}
    _json(folder / "manifest.json", public)
    archive = folder.with_suffix(".zip")
    temporary = archive.with_suffix(".zip.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name in ("index.html", "assembly.mp4", "manifest.json"):
            bundle.write(folder/name, name)
    temporary.replace(archive)
    return {"status": "succeeded", "package_status": "packaged", "publication_status": "not_published", "folder": str(folder.resolve()),
            "zip_path": str(archive.resolve()), "snapshot_id": str(identity),
            "files": ["index.html", "assembly.mp4", "manifest.json"], "url": None,
            "reason": "배포 패키지는 준비되었습니다. DEC-04의 게시 서버 설정과 다른 PC 접속 확인이 필요합니다."}
