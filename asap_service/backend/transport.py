"""Argument-array native/WSL transport. Never invokes a user-supplied shell."""
from __future__ import annotations
import os
import re
import subprocess
from pathlib import Path
from asap_service.backend.storage import ROOT

def wsl_path(path: str):
    # Paths here are actual local application records, never browser filenames.
    value = str(Path(path).resolve()).replace("\\", "/")
    match = re.match(r"^([A-Za-z]):/(.*)$", value)
    if not match:
        if value.startswith("/"):
            return value
        raise ValueError("WSL_PATH: drive-mounted path required")
    return "/mnt/" + match[1].lower() + "/" + match[2]

def translate_request(request, engine):
    if engine.get("transport") != "wsl":
        return request
    result = dict(request)
    for key in ("assembly_dir", "output_dir"):
        if key in result:
            result[key] = wsl_path(result[key])
    result["engine"] = dict(engine)
    return result

def command(engine, operation: str, input_file: Path, output_file: Path):
    python = engine.get("python", "")
    if not python:
        raise ValueError("ENGINE_NOT_READY: engine Python is not configured")
    script = ROOT / "engine" / "bridge.py"
    flag = "--config" if operation == "check" else "--request"
    if engine.get("transport", "native") == "wsl":
        if os.name != "nt":
            raise ValueError("ENGINE_NOT_READY: WSL transport requires Windows")
        prefix = ["wsl.exe"]
        if engine.get("distribution"):
            prefix += ["--distribution", engine["distribution"]]
        prefix += ["--exec"]
        if operation == "run":
            prefix += ["/usr/bin/setsid"]
        return prefix + [python, "-u", wsl_path(str(script)), operation, flag, wsl_path(str(input_file)), "--output", wsl_path(str(output_file))]
    if not Path(python).is_file():
        raise ValueError("ENGINE_NOT_READY: engine Python executable is missing")
    return [python, "-u", str(script), operation, flag, str(input_file), "--output", str(output_file)]

def process_kwargs():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}

def terminate_tree(process):
    import psutil
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in reversed(children):
            try:
                child.terminate()
            except psutil.NoSuchProcess:
                pass
        parent.terminate()
        _, alive = psutil.wait_procs(children + [parent], timeout=3)
        for item in alive:
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
    except psutil.NoSuchProcess:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
