"""Frozen backend/worker entry point; desktop shell is Electron."""
from multiprocessing import freeze_support
import json
import os
from pathlib import Path
import sys


def main():
    freeze_support()
    os.environ.setdefault("PYTHONUTF8", "1")
    from backend.release import default_data_dir, runtime_identity, resource_root
    if getattr(sys, "frozen", False):
        browser_log_dir = default_data_dir() / "logs"
        if "--identity" not in sys.argv:
            browser_log_dir.mkdir(parents=True, exist_ok=True)
        os.environ["CHROME_LOG_FILE"] = str(browser_log_dir / "chromium.log")
        os.environ.setdefault("PILOT_RESOURCE_DIR", str(resource_root()))
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(resource_root() / "browsers"))
        encoders = list((resource_root() / "ffmpeg").glob("*.exe"))
        if encoders:
            os.environ.setdefault("IMAGEIO_FFMPEG_EXE", str(encoders[0]))
        if sys.stdout is None or sys.stderr is None:
            folder = browser_log_dir
            folder.mkdir(parents=True, exist_ok=True)
            stream = (folder / ("backend-" + str(os.getpid()) + ".log")).open("a", encoding="utf-8", buffering=1)
            sys.stdout = sys.stdout or stream
            sys.stderr = sys.stderr or stream
    manifest = runtime_identity()
    if "--prepare-update" in sys.argv:
        from backend.storage import Storage, atomic_json
        result_path = Path(sys.argv[sys.argv.index("--prepare-update") + 1])
        storage = Storage(default_data_dir())
        try:
            storage.init()
            backup = storage.backup() if storage.list_projects() else None
            import OCP
            from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
            if BRepPrimAPI_MakeBox(1.0, 1.0, 1.0).Shape().IsNull():
                raise RuntimeError("GEOMETRY_RUNTIME_UNAVAILABLE")
            import numpy
            import imageio_ffmpeg
            import subprocess
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-version"], check=True, capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            from backend.main import create_app
            frontend = Path(os.environ.get("PILOT_RELEASE_ROOT", resource_root())) / "frontend" / "build-info.json"
            if frontend.is_file() and json.loads(frontend.read_text(encoding="utf-8")).get("build_id") != manifest["build_id"]:
                raise RuntimeError("FRONTEND_BUILD_MISMATCH")
            atomic_json(result_path, {"status": "ready", "build_id": manifest["build_id"], "schema_version": storage.status()["schema_version"], "backup_id": backup["id"] if backup else None})
        finally:
            storage.close()
    elif "--identity" in sys.argv:
        print(json.dumps(manifest, ensure_ascii=False))
    elif "--server" in sys.argv:
        from backend.main import run
        parent = os.environ.get("PILOT_PARENT_PID")
        if parent:
            import psutil
            import threading
            import time
            try:
                identity = psutil.Process(int(parent)).create_time()
            except psutil.Error:
                raise RuntimeError("DESKTOP_PARENT_MISSING")
            def watch_parent():
                while True:
                    time.sleep(1.0)
                    try:
                        alive = psutil.Process(int(parent)).create_time() == identity
                    except psutil.Error:
                        alive = False
                    if not alive:
                        from backend import main as api
                        if api.SERVER is not None:
                            api.SERVER.should_exit = True
                        return
            threading.Thread(target=watch_parent, daemon=True).start()
        run()
    elif "--worker" in sys.argv:
        from backend.worker import run_worker
        raise SystemExit(run_worker(sys.argv[sys.argv.index("--worker") + 1]))
    else:
        raise SystemExit("Start PILOT-Studio.exe to open the desktop application.")


if __name__ == "__main__":
    main()
