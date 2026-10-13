"""Validated default-storage selection, separate from project database bytes."""
import json, os, re
from pathlib import Path

def user_base():
    return (Path(os.environ.get('LOCALAPPDATA',Path.home()))/'PILOT Studio').resolve()
def verified_restore(path):
    candidate=Path(path)
    destination=candidate.resolve()
    base=user_base()/'restored'
    if candidate!=destination or not destination.is_relative_to(base) or not re.fullmatch(r'restore-[0-9a-f]{32}',destination.name):
        raise ValueError('ACTIVE_STORAGE_INVALID')
    record=json.loads((destination/'restore-result.json').read_text(encoding='utf-8'))
    if record.get('status')!='verified' or record.get('id')!=destination.name or Path(record['data_dir']).resolve()!=destination:
        raise ValueError('ACTIVE_STORAGE_NOT_VERIFIED')
    return destination
def selected_data_root():
    marker=user_base()/'active-storage.json'
    if not marker.is_file():return user_base()/'data'
    return verified_restore(json.loads(marker.read_text(encoding='utf-8'))['data_dir'])
def remember_storage(path):
    destination=verified_restore(path)
    base=user_base();base.mkdir(parents=True,exist_ok=True)
    marker=base/'active-storage.json';temporary=marker.with_suffix('.tmp')
    temporary.write_text(json.dumps({'format':'pilot-active-storage/1','data_dir':str(destination)}),encoding='utf-8')
    temporary.replace(marker)
