"""Stable shortcut target; executes only the verified active release."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from backend.release import activate_release, active_release, installation_lock, default_data_dir


def main():
    installation = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(os.environ.get("PILOT_INSTALL_ROOT", Path(__file__).resolve().parents[1] / "dist" / "installation")).resolve()
    if "--activate" in sys.argv:
        release_id = sys.argv[sys.argv.index("--activate") + 1]
        result = activate_release(installation, release_id)
        if sys.stdout:
            print(json.dumps(result))
        return 0
    waiting = '--wait-close' in sys.argv
    if waiting:
        sys.argv.remove('--wait-close')
    deadline = time.monotonic() + (45 if waiting else 0)
    while True:
        guard = installation_lock(installation)
        try:
            guard.__enter__()
            break
        except RuntimeError:
            if not waiting or time.monotonic() >= deadline:
                raise
            time.sleep(.2)
    try:
        folder, manifest = active_release(installation)
        if getattr(sys, "frozen", False) and manifest.get("launcher_sha256") != hashlib.sha256(Path(sys.executable).read_bytes()).hexdigest():
            raise RuntimeError("LAUNCHER_INTEGRITY_FAILED: 설치 프로그램으로 런처를 복구해 주세요.")
        executable = folder / "desktop" / "PILOT-Studio-Desktop.exe"
        if not executable.is_file():
            raise RuntimeError("DESKTOP_RUNTIME_MISSING")
        log_dir = default_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ, PILOT_RELEASE_ROOT=str(folder), PILOT_BUILD_ID=manifest["build_id"], PILOT_INSTALL_ROOT=str(installation), ELECTRON_LOG_FILE=str(log_dir / "desktop.log"), CHROME_LOG_FILE=str(log_dir / "chromium.log"))
        process = subprocess.Popen([str(executable), *sys.argv[1:]], cwd=str(folder), env=environment, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return process.wait()
    finally:
        guard.__exit__(None, None, None)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        try:
            error_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(os.environ.get("PILOT_INSTALL_ROOT", Path.cwd())).resolve()
            (error_root / "launcher-error.json").write_text(json.dumps({"error":type(error).__name__,"message":str(error)},ensure_ascii=False),encoding="utf-8")
        except OSError:
            pass
        if os.name == "nt" and '--activate' not in sys.argv:
            ctypes.windll.user32.MessageBoxW(None, str(error), "PILOT Studio 실행/업데이트 확인", 0x10)
        raise SystemExit(1)
