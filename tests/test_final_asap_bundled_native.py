"""Explicit offline proof using only the staged distributable ASAP runtime."""
from pathlib import Path
import json,os,shutil,subprocess,sys,uuid

def main():
    root=Path(__file__).resolve().parents[1]
    runtime=root/'packaging/resources/runtime/asap'
    evidence=root/'evidence/bundled-native';evidence.mkdir(parents=True,exist_ok=True)
    original=json.loads((root/'evidence/app-data/asap/jobs/dc59afa972f64d7585f56e06811c1829/request.json').read_text(encoding='utf-8'))
    engine={'transport':'native','python':str(runtime/'python/python.exe'),'asap_root':str(runtime/'ASAP'),
            'weights_path':str(runtime/'pretrained_model.pt'),'workspace_root':str(Path(os.environ['LOCALAPPDATA'])/'PILOT-Studio-Final/bundled-native-proof'),'distribution':''}
    config=evidence/'config.json';config.write_text(json.dumps({'engine':engine}),encoding='utf-8')
    bridge=root/'asap_service/engine/bridge.py'
    with (evidence/'environment.log').open('wb') as log:
        checked=subprocess.run([engine['python'],'-u',str(bridge),'check','--config',str(config),'--output',str(evidence/'environment.json')],stdout=log,stderr=subprocess.STDOUT,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    environment=json.loads((evidence/'environment.json').read_text(encoding='utf-8'))
    assert checked.returncode==0 and environment.get('ready') and environment.get('learn_ready'),environment
    geometry=evidence/'input_obj'
    if not geometry.exists():shutil.copytree(original['assembly_dir'],geometry)
    result_dir=evidence/'run';result_dir.mkdir(exist_ok=True)
    original.update(engine=engine,assembly_dir=str(geometry),output_dir=str(result_dir),nonce=uuid.uuid4().hex)
    request=evidence/'request.json';request.write_text(json.dumps(original,ensure_ascii=False),encoding='utf-8')
    with (evidence/'run.log').open('wb') as log:
        ran=subprocess.run([engine['python'],'-u',str(bridge),'run','--request',str(request),'--output',str(result_dir/'result.json')],stdout=log,stderr=subprocess.STDOUT,timeout=220,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    result=json.loads((result_dir/'result.json').read_text(encoding='utf-8'))
    assert ran.returncode==0 and result.get('sequence_status')=='success' and result.get('path_status')=='success',result
    summary={'runtime':'staged_distributable_only','environment_ready':True,'learn_ready':True,
             'sequence_status':result['sequence_status'],'path_status':result['path_status'],
             'units':len(result['assembly_order']),'weights_sha256':result['provenance']['weights_sha256'],
             'native_guard_runtime_binding':result['provenance'].get('native_guard_runtime_binding')}
    (evidence/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(summary)

if __name__=='__main__':main()
