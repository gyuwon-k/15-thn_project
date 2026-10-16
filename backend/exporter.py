"""Frame-acknowledged company-template MP4 rendering and full decoding checks."""
from __future__ import annotations
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlencode
from .domain import digest, now, uid, playback_summary, geometry_hash
from .jobs import atomic_json


def render_video(payload, folder, progress, cancel, *, data_dir, build_id):
    state=payload["render"]["snapshot"]
    playback=playback_summary(state.get("plan",{}).get("steps",[]))
    proof=state.get("validation") or {}
    if not playback["total_count"] or playback["executable_prefix_count"]!=playback["total_count"] or proof.get("dependency_hash")!=geometry_hash(state) or proof.get("build_id")!=build_id:
        raise ValueError("현재 버전에서 검증하지 않은 경로는 검토용 영상에서도 재생할 수 없습니다.")
    from playwright.sync_api import sync_playwright
    import imageio_ffmpeg
    root=Path(getattr(sys,"_MEIPASS",Path(__file__).resolve().parents[1]))
    browsers=root/"browsers"
    if not browsers.is_dir() and not getattr(sys,"frozen",False):
        configured=os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
        browsers=Path(configured) if configured else root/"packaging"/"resources"/"browsers"
    if not browsers.is_dir():raise RuntimeError("영상 브라우저 런타임을 찾을 수 없습니다.")
    from .release import prepare_browser_runtime
    progress(0,"영상 브라우저 실행 영역 확인")
    if cancel():raise InterruptedError("영상 생성을 취소했습니다.")
    # Chromium GPU subprocesses may create debug.log beside their executable,
    # ignoring the parent log destination. Run a verified writable copy.
    browser_runtime=prepare_browser_runtime(browsers,data_dir,build_id)
    os.environ["PLAYWRIGHT_BROWSERS_PATH"]=str(browser_runtime)
    ffmpeg=next(iter((root/"ffmpeg").glob("*.exe")),None)
    ffmpeg=str(ffmpeg) if ffmpeg else imageio_ffmpeg.get_ffmpeg_exe()
    render=payload["render"];settings=render["settings"]
    width,height,fps=settings["width"],settings["height"],settings["fps"]
    output=Path(folder)/"video.partial.mp4";final=Path(folder)/(render["id"]+".mp4")
    command_log=Path(folder)/"encode.log";process=None
    def check():
        if cancel():raise InterruptedError("영상 생성을 취소했습니다.")
    if not render["draft"]:
        state=render["snapshot"]
        if state.get("validation",{}).get("status")!="PASS":raise ValueError("확정 영상에는 통과한 검증이 필요합니다.")
        if not render["template"].get("approved"):raise ValueError("검토한 회사 템플릿이 필요합니다.")
        for step in state["plan"]["steps"]:
            if not step.get("path"):raise ValueError("확정 단계의 경로가 없습니다.")
    # Route normal browser diagnostics to this job; GPU diagnostics stay in the
    # writable runtime copy, never in the checksum-verified installation.
    browser_log=Path(folder).resolve()/"chromium.log"
    browser_env={**os.environ,"CHROME_LOG_FILE":str(browser_log)}
    with sync_playwright() as playwright:
        browser=playwright.chromium.launch(headless=True,env=browser_env,
            args=["--hide-scrollbars","--enable-logging",f"--log-file={browser_log}"])
        try:
            page=browser.new_page(viewport={"width":width,"height":height},device_scale_factor=1,
                                  extra_http_headers={"X-Pilot-Token":payload["token"]})
            page.set_default_timeout(180000)
            errors=[];page.on("pageerror",lambda error:errors.append(str(error)))
            url=payload["base_url"].rstrip("/")+"/?"+urlencode({"render":payload["render_id"],"token":payload["token"]})
            progress(1,"모델·템플릿·한글 글꼴 준비")
            page.goto(url,wait_until="domcontentloaded")
            page.wait_for_function("window.__PILOT_RENDER__ && (window.__PILOT_RENDER__.ready === true || window.__PILOT_RENDER__.error)")
            controller_error=page.evaluate("window.__PILOT_RENDER__.error || null")
            if controller_error:raise RuntimeError("영상 준비 오류: "+str(controller_error))
            page.evaluate("async()=>{await document.fonts.ready}")
            duration=float(page.evaluate("window.__PILOT_RENDER__.duration"))
            if not math.isfinite(duration) or duration<=0:raise ValueError("영상 타임라인 길이가 올바르지 않습니다.")
            frames=max(1,math.ceil(duration*fps));target=page.locator("#render-root")
            if target.count()!=1:raise ValueError("렌더 장면이 없습니다.")
            box=target.bounding_box()
            if not box or abs(box["width"]-width)>1 or abs(box["height"]-height)>1:raise ValueError("렌더 장면 크기가 출력 설정과 다릅니다.")
            arguments=[ffmpeg,"-hide_banner","-y","-f","image2pipe","-vcodec","png","-framerate",str(fps),"-i","pipe:0","-an","-c:v","libx264","-preset","veryfast","-crf","22","-pix_fmt","yuv420p","-movflags","+faststart","-frames:v",str(frames),str(output)]
            with command_log.open("wb") as log:
                process=subprocess.Popen(arguments,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=log,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                try:
                    for frame in range(frames):
                        check()
                        frame_time=duration if frame==frames-1 else min(frame/fps,duration)
                        page.evaluate("async t=>await window.__PILOT_RENDER__.setTime(t)",frame_time)
                        if errors:raise RuntimeError("렌더 화면 오류: "+errors[-1])
                        png=page.screenshot(clip=box,animations="disabled")
                        if process.poll() is not None:raise RuntimeError("인코더가 처리 중 종료되었습니다.")
                        process.stdin.write(png)
                        if frame in {0,frames//2,frames-1}:(Path(folder)/f"frame-{frame:06d}.png").write_bytes(png)
                        if frame%max(1,fps)==0:progress(5+85*(frame+1)/frames,f"영상 프레임 {frame+1}/{frames}")
                    process.stdin.close()
                    while process.poll() is None:
                        check()
                        try:process.wait(.25)
                        except subprocess.TimeoutExpired:pass
                    if process.returncode:raise RuntimeError("MP4 인코딩 실패. 인코더 진단 로그를 확인해 주세요.")
                finally:
                    if process.poll() is None:process.kill();process.wait()
            check();progress(94,"MP4 전체 디코딩·프레임 수 확인")
            verified=subprocess.run([ffmpeg,"-v","error","-nostats","-i",str(output),"-progress","pipe:1","-f","null","-"],capture_output=True,timeout=max(60,int(duration*4)),creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            counts=[int(line.split("=",1)[1]) for line in verified.stdout.decode(errors="replace").splitlines() if line.startswith("frame=")]
            if verified.returncode or not counts or counts[-1]!=frames:raise RuntimeError("MP4 디코딩 또는 예상 프레임 수 검증에 실패했습니다.")
            output.replace(final)
            record={"id":render["id"],"video_path":str(final),"status":"COMPLETE","draft":render["draft"],"snapshot_id":render["snapshot_id"],"template_id":render["template"]["id"],"template_revision":render["template"]["revision"],"timeline_hash":digest(render["snapshot"]["plan"]),"settings":settings,"frames":frames,"duration_seconds":frames/fps,"decoded_frames":counts[-1],"last_frame_time_seconds":duration,"frame_time_policy":"fixed_fps_with_final_target_frame","created_at":now()}
            atomic_json(Path(folder)/"render-manifest.json",record)
            progress(99,"영상 검증 완료 · 저장소 등록 대기")
            return record
        finally:browser.close()
