"""Final compiled EXE: frozen CAD workers and bundled native planner over HTTP."""
from pathlib import Path
import json,time
import httpx
root=Path(__file__).resolve().parents[1];source=root.parent/'asap-unit-test/evidence/two-input-api'
evidence=root/'evidence/packaged-native';evidence.mkdir(parents=True,exist_ok=True)
c=httpx.Client(base_url='http://127.0.0.1:8780',timeout=120)
def call(method,path,**kwargs):
    r=c.request(method,path,**kwargs);r.raise_for_status();return r.json()
def wait(jid,seconds=240):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        j=call('GET','/api/asap/jobs/'+jid)
        if j['status'] in ('success','failed','canceled','interrupted'):
            assert j['status']=='success',{k:j.get(k) for k in ('id','status','stage','error','logs')}
            return j
        time.sleep(.3)
    raise TimeoutError(jid)
config=call('GET','/api/asap/config');engine=config['engine']
assert config['discovery']=='installed_runtime'
assert all(str(root/'dist/PILOT-Studio/_internal/runtime/asap').casefold() in engine[k].casefold() for k in ('python','asap_root','weights_path'))
environment=call('GET','/api/asap/environment');assert environment['learn_ready'],environment
project=call('POST','/api/projects',json={'name':'설치형 EXE 실제 엔진 검증','input_mode':'assembly_with_parts'})
pid=project['project']['id']
session=call('POST','/api/asap/sessions',data={'input_mode':'assembly_with_parts'},files={'file':('assembly.step',(source/'assembly.step').read_bytes())})
wait(session['job_id']);sid=session['id'];base='/api/asap/sessions/'+sid
call('POST','/api/projects/'+pid+'/asap-link',json={'session_id':sid})
registered=call('POST',base+'/templates',files=[('files',('compound.step',(source/'group.step').read_bytes())),('files',('single.step',(source/'single.step').read_bytes()))])
wait(registered['job_id']);s=call('GET',base);revision=s['unit_state']['revision']
job=call('POST',base+'/matches',json={'template_ids':[t['id'] for t in s['templates']['templates']],'expected_revision':revision,'tolerance_mm':.1,'timeout_s':60})
wait(job['job_id']);s=call('GET',base);match=call('GET',base+'/matches/'+s['templates']['last_match_id'])
ids=[x['id'] for x in match['candidates'] if set(x['member_names'])=={'occurrence 1','occurrence 2'} or x['member_names'] in (['occurrence 3'],['occurrence 4'])]
assert len(ids)==3,match
applied=call('POST',base+'/matches/'+match['id']+'/accept',json={'candidate_ids':ids,'expected_revision':revision});wait(applied['job_id'])
s=call('GET',base);revision=s['unit_state']['revision'];call('POST',base+'/units/confirm',json={'expected_revision':revision})
job=call('POST',base+'/convert',json={'penetration_max_faces_per_unit':0});wait(job['job_id'])
run=call('POST',base+'/runs',json={'strategy':'learn','budget':40,'timeout_s':180,'max_gripper':4,'max_pose':1,'expected_unit_revision':revision,
    'physics_profile':{'contact_eps_mm':.1,'sdf_dx_mm':.5,'sdf_res':20,'path_col_th_mm':.02,'minimum_separation_mm':5}})
wait(run['job_id'],200);result=call('GET','/api/asap/runs/'+run['run_id']);assert result['sequence_status']=='success' and result['path_status']=='success'
adopt=call('POST','/api/projects/'+pid+'/asap-import',json={'run_id':run['run_id']})
summary={'application':'compiled_EXE','runtime':config['discovery'],'engine_paths':engine,'environment_learn_ready':environment['learn_ready'],
    'project_id':pid,'session_id':sid,'run_id':run['run_id'],'input_mode':'assembly_with_parts','templates':2,'assembly_units':3,'original_solids':4,
    'preprocessing_status':result['preprocessing_status'],'sequence_status':result['sequence_status'],'path_status':result['path_status'],'adopt':adopt,
    'weights_sha256':result['provenance'].get('weights_sha256'),'scope':'synthetic compiled application proof, not HVJB'}
(evidence/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False),flush=True)
