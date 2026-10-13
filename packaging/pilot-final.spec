# -*- mode: python ; coding: utf-8 -*-
"""Windows onedir package including OCCT, FFmpeg, Chromium and the compiled UI."""
from pathlib import Path
import importlib.util
import importlib.metadata
import json
import shutil
import subprocess
import sys
import imageio_ffmpeg
from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs, collect_submodules

root = Path(SPECPATH).parent
frontend = root / "frontend" / "dist"
if not frontend.is_dir():
    raise RuntimeError("Build frontend first: npm run build in frontend/")
browsers = root.parent / "pilot-studio" / "data" / "browsers"
if not browsers.is_dir() or not list(browsers.glob("chromium*")):
    raise RuntimeError("Install project Chromium using scripts/build_exe.ps1")

datas = [(str(frontend), "frontend/dist"), (str(browsers), "browsers"),
         (str(root / "asap_service" / "engine"), "asap_service/engine")]
binaries = [(imageio_ffmpeg.get_ffmpeg_exe(), "ffmpeg")]
# Preserve actual license texts, including nested third-party notices. This
# inventory contains build tools as well as runtime distributions by design.
notice_build = root / "build" / "notices"
notice_build.mkdir(parents=True, exist_ok=True)
versions = {"python": sys.version, "python_packages": {}, "frontend_runtime": {}}
for distribution in importlib.metadata.distributions():
    name = distribution.metadata.get("Name", "unknown")
    versions["python_packages"][name] = distribution.version
    for relative in distribution.files or []:
        path = Path(str(relative))
        if not any(word in path.name.lower() for word in ("license", "copying", "notice")):
            continue
        if "tests" in path.parts or ".." in path.parts:
            continue
        source = Path(distribution.locate_file(relative))
        if source.is_file():
            destination = Path("third_party_licenses/python-packages") / name / path.parent
            datas.append((str(source), destination.as_posix()))
for name in ("react", "react-dom", "scheduler", "three", "lucide-react"):
    package = root / "frontend" / "node_modules" / name
    versions["frontend_runtime"][name] = json.loads((package / "package.json").read_text(encoding="utf-8"))["version"]
    for license in package.glob("LICENSE*"):
        datas.append((str(license), f"third_party_licenses/frontend/{name}"))
python_license = Path(sys.base_prefix) / "LICENSE.txt"
if python_license.is_file():
    datas.append((str(python_license), "third_party_licenses/python"))
upstream_licenses = root.parent / "pilot-studio" / "data" / "license_sources"
if upstream_licenses.is_dir():
    datas.append((str(upstream_licenses), "third_party_licenses/upstream"))
license_output = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-L"], capture_output=True, check=True)
(notice_build / "ffmpeg-build-and-license.txt").write_bytes(license_output.stdout + license_output.stderr)
(notice_build / "versions.json").write_text(json.dumps(versions, ensure_ascii=False, indent=2), encoding="utf-8")
datas += [(str(notice_build / "ffmpeg-build-and-license.txt"), "third_party_licenses"),
          (str(notice_build / "versions.json"), "third_party_licenses")]
hiddenimports = ["backend.main", "backend.cad", "backend.learning", "backend.exporter",
                 "backend.worker", "backend.processing",
                 "uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                 "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on"]
hiddenimports += collect_submodules("backend") + collect_submodules("asap_service.backend") + collect_submodules("desktop")
for package in ("OCP", "cadquery_ocp", "cadquery_ocp_proxy", "playwright", "sklearn", "scipy", "numpy", "webview", "pythonnet", "clr_loader", "pymysql"):
    if importlib.util.find_spec(package) is not None:
        data, libs, imports = collect_all(package, filter_submodules=lambda name: "tests" not in name.split("."),
                                         exclude_datas=["**/tests/**", "**/test/**"])
        datas += data
        binaries += libs
        hiddenimports += imports
# The cadquery wheel's sibling DLL directory is required on a clean Windows PC.
import site
for packages in site.getsitepackages():
    for folder in ("cadquery_ocp.libs", "vtk.libs"):
        libraries = Path(packages) / folder
        if libraries.is_dir():
            # OCP's wheel initializer calls add_dll_directory on both siblings.
            datas.append((str(libraries), folder))
hiddenimports += collect_submodules("PIL")
a = Analysis([str(root / "launcher.py")], pathex=[str(root)], binaries=binaries,
             datas=datas, hiddenimports=hiddenimports, hookspath=[], hooksconfig={},
             runtime_hooks=[], excludes=["tkinter", "matplotlib", "pytest", "IPython"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="PILOT-Studio",
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PILOT-Studio")
# The independent 3.11 interpreter and MariaDB are complete verified binary
# distributions. Preserve their exact directory layout without reclassifying
# them as the app's 3.12 extensions or altering dependency loading.
shutil.copytree(root / "packaging" / "resources", Path(DISTPATH) / "PILOT-Studio" / "_internal", dirs_exist_ok=True)
shutil.copy2(root / "docs" / "THIRD_PARTY_NOTICES.md", Path(DISTPATH) / "PILOT-Studio" / "THIRD_PARTY_NOTICES.md")
