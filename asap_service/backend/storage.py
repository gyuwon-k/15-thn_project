from __future__ import annotations
import json
import os
import re
import uuid
import sys
import importlib.util
import hashlib
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_ROOT = ROOT.parent
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', APP_ROOT))
ROOT = RESOURCE_ROOT / 'asap_service' if getattr(sys, 'frozen', False) else ROOT
_pilot_data = Path(os.environ.get('PILOT_DATA_DIR', str(Path(os.environ.get('LOCALAPPDATA', str(APP_ROOT))) / 'PILOT Studio Final')))
DATA = Path(os.environ.get('PILOT_ASAP_DATA', str(_pilot_data / 'asap'))).resolve()

def now():
    return datetime.now(timezone.utc).isoformat()

def resolve_path(value):
    """Rebind restored DATA reads, retaining immutable stored JSON values."""
    path=Path(value)
    if path.is_absolute():
        from backend.desktop_storage import resolve_path as restored
        return restored(path)
    return path

def write_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(temporary, path)

def read_json(path: Path, default=None):
    path=resolve_path(path)
    if not path.exists():
        return default
    # Windows PowerShell 5 writes a BOM for -Encoding utf8.
    return json.loads(path.read_text(encoding="utf-8-sig"))

def checked_id(value: str):
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("INVALID_ID: malformed local record ID")
    return value

def session_path(value: str):
    return DATA / "sessions" / checked_id(value)

def run_path(value: str):
    index = read_json(DATA / "runs" / (checked_id(value)+".json"))
    if not index:
        raise FileNotFoundError("RUN_NOT_FOUND")
    return session_path(index["session_id"]) / "runs" / value

def default_config():
    """Discover an installed runtime; never replace ASAP with another planner."""
    runtime = Path(os.environ.get('PILOT_RUNTIME_DIR', str(RESOURCE_ROOT / 'runtime')))
    python = runtime / 'asap' / 'python' / ('python.exe' if os.name == 'nt' else 'bin/python')
    asap_root = runtime / 'asap' / 'ASAP'
    defaults = {'transport':'native','python':str(python),'asap_root':str(asap_root),
                'weights_path':str(runtime / 'asap' / 'pretrained_model.pt'),'distribution':'',
                'workspace_root':str(Path(os.environ.get('LOCALAPPDATA',str(APP_ROOT))) / 'PILOT-Studio-Final' / 'native-work')}
    origin='installed_runtime'
    if not getattr(sys,'frozen',False):
        candidate = APP_ROOT.parent / 'asap-unit-test' / 'data' / 'config.json'
        development = read_json(candidate,{})
        selected=development.get('engine',{})
        if Path(selected.get('python','')).is_file() and Path(selected.get('asap_root','')).is_dir():
            defaults.update(selected)
            staging=Path(selected.get('workspace_root',str(asap_root / '.test-runs')))
            defaults['workspace_root']=str(staging.parent / 'physics-pilot-final')
            origin='verified_development_runtime'
    for key,env in [('python','PILOT_ASAP_ENGINE_PYTHON'),('asap_root','PILOT_ASAP_ROOT'),('weights_path','PILOT_ASAP_WEIGHTS'),('workspace_root','PILOT_ASAP_WORKSPACE')]:
        if os.environ.get(env):
            defaults[key]=os.environ[env]
            origin='explicit_environment'
    return {'engine':defaults,'discovery':origin}

def get_config():
    config=read_json(DATA / 'config.json',default_config())
    config={**config,'engine':dict(config.get('engine',{}))}
    for key in ('python','asap_root','weights_path','workspace_root'):
        value=config['engine'].get(key)
        if value and not (config['engine'].get('transport')=='wsl' and str(value).startswith('/')):
            config['engine'][key]=str(resolve_path(value))
    required=('python','asap_root','weights_path')
    missing=any(not config['engine'].get(key) or not Path(config['engine'][key]).exists() for key in required)
    if missing and config['engine'].get('transport','native')=='native' and (DATA.parent/'path_map.json').is_file():
        replacement=default_config()
        selected=replacement['engine']
        previous=read_json(DATA/'environment.json',{}).get('provenance',{})
        try:
            spec=importlib.util.spec_from_file_location('pilot_runtime_identity_bridge',ROOT/'engine/bridge.py')
            bridge=importlib.util.module_from_spec(spec);spec.loader.exec_module(bridge)
            proven=previous.get('asap_commit')==bridge.PINNED_COMMIT and previous.get('weights_sha256')==bridge.OFFICIAL_WEIGHTS_SHA256
            proven=proven and all(Path(selected[k]).exists() for k in required)
            if proven:
                proven=bridge.sha256(selected['weights_path'])==bridge.OFFICIAL_WEIGHTS_SHA256
                proven=proven and all(hashlib.sha256((Path(selected['asap_root'])/path).read_bytes().replace(b'\r\n',b'\n')).hexdigest()==expected for path,expected in bridge.SOURCE_HASHES.items())
                if proven:
                    binaries=list((Path(selected['asap_root'])/'simulation').glob('redmax_py*.pyd'))
                    if len(binaries)!=1:proven=False
                    else:bridge.native_guard_provenance(Path(selected['asap_root']),binaries[0])
            if proven:
                config['engine'].update({k:selected[k] for k in ('python','asap_root','weights_path','workspace_root')})
                config['runtime_resolution']='same_pinned_engine_and_weights_verified_after_restore'
        except Exception:
            pass
    return config
