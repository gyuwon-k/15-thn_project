# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
root = Path(SPECPATH).parent
a = Analysis([str(root / 'desktop' / 'fixed_launcher.py')], pathex=[str(root)], binaries=[], datas=[], hiddenimports=['backend.release'], excludes=['OCP','numpy','scipy','sklearn','pymysql','playwright'], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, name='PILOT-Studio', debug=False, strip=False, upx=False, console=False)
