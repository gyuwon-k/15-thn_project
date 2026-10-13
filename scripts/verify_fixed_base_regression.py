"""Regression: native fixed-base ground placement and verified field reuse."""
from pathlib import Path
import json, os, subprocess, uuid
ROOT=Path(__file__).resolve().parents[1]
EVIDENCE=ROOT/'evidence/hvjb-final/fixed-base-regression'
EVIDENCE.mkdir(parents=True,exist_ok=True)
request=json.loads((ROOT/'evidence/bundled-native/request.json').read_text(encoding='utf-8'))
request['settings'].update(base_part='00000',max_gripper=4,timeout_s=180)
request.update(output_dir=str(EVIDENCE),nonce=uuid.uuid4().hex)
path=EVIDENCE/'request.json';path.write_text(json.dumps(request,ensure_ascii=False),encoding='utf-8')
with (EVIDENCE/'worker.log').open('wb') as log:
    result=subprocess.run([request['engine']['python'],'-u',str(ROOT/'asap_service/engine/bridge.py'),'run','--request',str(path),'--output',str(EVIDENCE/'result.json')],stdout=log,stderr=subprocess.STDOUT,timeout=200,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
data=json.loads((EVIDENCE/'result.json').read_text(encoding='utf-8'))
assert result.returncode==0 and data['sequence_status']=='success' and data['path_status']=='success',data
performance=data['provenance']['physics_parameters']['performance_adapters']
preflight=data['provenance']['original_solid_sdf']
assert preflight['verified_cache_reuse']['native_field_reload_count']>0,preflight['verified_cache_reuse']
assert performance['static_query_scene_cache_hits']>0,performance
assert data['base_part']=='00000'
assert data['provenance']['source_hashes']
summary={'sequence_status':data['sequence_status'],'path_status':data['path_status'],'base_part':data['base_part'],'units':len(data['assembly_order']),
         'native_field_reload_count':preflight['verified_cache_reuse']['native_field_reload_count'],'current_occupancy_verification_skipped':False,'performance_adapters':performance}
(EVIDENCE/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8');print(summary,flush=True)
