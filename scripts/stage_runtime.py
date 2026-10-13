"""Build relocatable, offline runtime resources from verified local distributions."""
from pathlib import Path
import hashlib, json, os, shutil, sys
ROOT=Path(__file__).resolve().parents[1]
RESOURCE=ROOT/'packaging'/'resources'

def tree(source,target,exclude=()):
    shutil.copytree(source,target,dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__','*.pyc','.git','tests','test',*exclude))
def main():
    runtime=RESOURCE/'runtime';runtime.mkdir(parents=True,exist_ok=True)
    maria=Path(os.environ.get('PILOT_BUILD_MARIADB_ROOT','C:/Program Files/MariaDB 12.3'))
    tree(maria/'bin',runtime/'mariadb'/'bin')
    tree(maria/'share',runtime/'mariadb'/'share')
    tree(maria/'lib'/'plugin',runtime/'mariadb'/'lib'/'plugin')
    for name in ('COPYING','CREDITS','README.md','THIRDPARTY'):shutil.copy2(maria/name,runtime/'mariadb'/name)
    base=Path(os.environ.get('PILOT_BUILD_ENGINE_BASE','C:/Users/Win11Pro/miniconda3/envs/TORCH_PY311'))
    packages=ROOT.parent/'asap-test'/'.engine-venv'/'Lib'/'site-packages'
    engine=runtime/'asap'/'python';engine.mkdir(parents=True,exist_ok=True)
    for file in base.iterdir():
        if file.is_file() and (file.suffix.lower() in ('.exe','.dll') or 'LICENSE' in file.name):shutil.copy2(file,engine/file.name)
    tree(base/'Lib',engine/'Lib',exclude=('site-packages','ensurepip','idlelib','tkinter'))
    tree(base/'DLLs',engine/'DLLs',exclude=('_tkinter.pyd',))
    # Conda's standard-library extension modules depend on these libraries.
    for name in ('ffi-8.dll','libbz2.dll','libcrypto-3-x64.dll','libexpat.dll','liblzma.dll','libssl-3-x64.dll','sqlite3.dll','zlib.dll'):
        for location in (base/'Library'/'bin'/name,base/name):
            if location.is_file():shutil.copy2(location,engine/name);break
    tree(packages,engine/'Lib'/'site-packages',exclude=('cmake','cmake-*','ninja','ninja-*','pip','pip-*','setuptools','setuptools-*','pkg_resources'))
    config=json.loads((ROOT.parent/'asap-unit-test'/'data'/'config.json').read_text(encoding='utf-8'))['engine']
    tree(Path(config['asap_root']),runtime/'asap'/'ASAP')
    shutil.copy2(config['weights_path'],runtime/'asap'/'pretrained_model.pt')
    # Keep engine writable workspaces outside installed resources.
    manifest={'format':'pilot-bundled-runtime/1','python':'3.11','native_source':config['asap_root'],'weights_sha256':hashlib.sha256(Path(config['weights_path']).read_bytes()).hexdigest(),
              'mariadb_version':'12.3.2','binding':'private_loopback','files':{}}
    for rel in ('ASAP/simulation/redmax_py.cp311-win_amd64.pyd','ASAP/simulation/redmax/redmax_core.dll','ASAP/native_guard_manifest.json'):
        path=runtime/'asap'/rel
        if path.is_file():manifest['files'][rel]=hashlib.sha256(path.read_bytes()).hexdigest()
    (RESOURCE/'runtime-manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2),encoding='utf-8')
    print('Offline runtime resources staged:',RESOURCE)
if __name__=='__main__':main()
