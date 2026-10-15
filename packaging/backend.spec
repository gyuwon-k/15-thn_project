# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import importlib.util
import importlib.metadata
import shutil
import site
import sys
import imageio_ffmpeg
from PyInstaller.utils.hooks import collect_all, collect_submodules

root = Path(SPECPATH).parent
datas = [(str(root / 'release.json'), '.'), (str(root / 'frontend' / 'dist'), 'frontend/dist')]
if (root / 'backend' / 'resources').is_dir():
    datas.append((str(root / 'backend' / 'resources'), 'backend/resources'))
binaries = [(imageio_ffmpeg.get_ffmpeg_exe(), 'ffmpeg')]
hidden = collect_submodules('backend')
for package in ('OCP', 'cadquery_ocp', 'cadquery_ocp_proxy', 'numpy', 'scipy', 'sklearn', 'playwright', 'pymysql', 'PIL'):
    if importlib.util.find_spec(package) is not None:
        data, libs, imports = collect_all(package, filter_submodules=lambda name: 'tests' not in name.split('.'), exclude_datas=['**/tests/**', '**/test/**'])
        datas += data
        binaries += libs
        hidden += imports
for packages in site.getsitepackages():
    for name in ('cadquery_ocp.libs', 'vtk.libs'):
        location = Path(packages) / name
        if location.is_dir():
            datas.append((str(location), name))
hidden += ['uvicorn.logging', 'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto', 'uvicorn.lifespan.on']
for package in ('fastapi','uvicorn','pydantic','PyMySQL','cadquery-ocp','cadquery-ocp-proxy','vtk','numpy','scipy','scikit-learn','playwright','Pillow','imageio-ffmpeg','psutil','python-multipart'):
    distribution = importlib.metadata.distribution(package)
    for relative in distribution.files or []:
        path = Path(str(relative))
        if '..' not in path.parts and any(word in path.name.lower() for word in ('license','copying','notice')):
            source = Path(distribution.locate_file(relative))
            if source.is_file():
                datas.append((str(source), 'third_party_licenses/' + package + '/' + path.parent.as_posix()))
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.is_file():
    datas.append((str(python_license), 'third_party_licenses/python'))
a = Analysis([str(root / 'launcher.py')], pathex=[str(root)], binaries=binaries, datas=datas, hiddenimports=hidden,
             excludes=['tkinter', 'matplotlib', 'IPython', 'pytest', 'webview', 'pythonnet'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='PILOT-Backend', debug=False, strip=False, upx=False, console=True)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='PILOT-Backend')
shutil.copytree(root / 'packaging' / 'resources', Path(DISTPATH) / 'PILOT-Backend' / '_internal', dirs_exist_ok=True, ignore=shutil.ignore_patterns('*.log'))
