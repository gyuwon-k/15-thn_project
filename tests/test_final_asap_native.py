"""Explicit real native A/B proof. Not run by ordinary unit-test discovery."""
from __future__ import annotations
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import asap_integration as integration, domain, store
from asap_service.backend import storage

EVIDENCE=ROOT/'evidence/native-integration'
FIXTURE=ROOT.parent/'asap-unit-test/evidence/two-input-api'


@asynccontextmanager
async def lifespan(_):
    store.init();integration.startup()
    yield
    integration.shutdown()
    from backend.desktop_storage import shutdown_database
    shutdown_database()


def wait(client,jid,seconds=200):
    deadline=time.monotonic()+seconds
    last=None
    while time.monotonic()<deadline:
        result=client.get('/api/asap/jobs/'+jid).json()
        if result['status']!=last:
            print('native job',jid,result['status'],result.get('stage'),flush=True);last=result['status']
        if result['status'] in {'success','failed','canceled','interrupted'}:
            assert result['status']=='success',result
            return result
        time.sleep(.25)
    raise TimeoutError(jid)


def posted(client,url,**kwargs):
    response=client.post(url,**kwargs)
    assert response.status_code==200,(url,response.status_code,response.text)
    return response.json()


def proof(client,mode):
    state=posted(client,'/api/projects',json={'name':'Native '+mode,'vehicle_model':'테스트','item_name':'범용 형상 검증','product_no':'검증용'})
    created=posted(client,'/api/asap/sessions',data={'input_mode':mode},files={'file':('assembly.step',(FIXTURE/'assembly.step').read_bytes())})
    wait(client,created['job_id']);sid=created['id'];url='/api/asap/sessions/'+sid
    posted(client,f'/api/projects/{state["project"]["id"]}/asap-link',json={'session_id':sid,'expected_input_revision':0})
    if mode=='assembly_with_parts':
        uploads=posted(client,url+'/templates',files=[('files',('compound.step',(FIXTURE/'group.step').read_bytes())),('files',('single.step',(FIXTURE/'single.step').read_bytes()))])
        wait(client,uploads['job_id']);session=client.get(url).json();revision=session['unit_state']['revision']
        start=posted(client,url+'/matches',json={'template_ids':[t['id'] for t in session['templates']['templates']],
                                               'expected_revision':revision,'tolerance_mm':.1,'timeout_s':60})
        wait(client,start['job_id']);session=client.get(url).json();mid=session['templates']['last_match_id']
        matches=client.get(url+'/matches/'+mid).json()
        selected=[c['id'] for c in matches['candidates'] if set(c['member_names'])=={'occurrence 1','occurrence 2'} or c['member_names']==['occurrence 3'] or c['member_names']==['occurrence 4']]
        assert len(selected)==3,matches
        applying=posted(client,url+'/matches/'+mid+'/accept',json={'candidate_ids':selected,'expected_revision':revision})
        wait(client,applying['job_id'])
    session=client.get(url).json();revision=session['unit_state']['revision']
    posted(client,url+'/units/confirm',json={'expected_revision':revision})
    converted=posted(client,url+'/convert',json={'penetration_max_faces_per_unit':0})
    wait(client,converted['job_id'])
    run=posted(client,url+'/runs',json={'strategy':'learn','budget':40,'timeout_s':180,'max_pose':1,'max_gripper':4,
                                      'expected_unit_revision':revision,'physics_profile':{'contact_eps_mm':.1,
                                       'sdf_dx_mm':.5,'sdf_res':20,'path_col_th_mm':.02,'minimum_separation_mm':5}})
    wait(client,run['job_id']);result=client.get('/api/asap/runs/'+run['run_id']).json()
    assert result['sequence_status']=='success' and result['path_status']=='success',result
    adopted=posted(client,f'/api/projects/{state["project"]["id"]}/asap-import',json={'run_id':run['run_id'],'expected_input_revision':0})
    output={'mode':mode,'project_id':state['project']['id'],'session_id':sid,'run_id':run['run_id'],
            'units':len(result['assembly_order']),'original_solids':result['preprocessing_summary']['original_solid_count'],
            'preprocessing_status':result['preprocessing_status'],'sequence_status':result['sequence_status'],
            'path_status':result['path_status'],'adoption':adopted,'elapsed_s':result.get('elapsed_s')}
    print(json.dumps(output,ensure_ascii=False),flush=True)
    return output


def main():
    import httpx
    EVIDENCE.mkdir(parents=True,exist_ok=True)
    endpoint=sys.argv[1] if len(sys.argv)>1 else 'http://127.0.0.1:8770'
    with httpx.Client(base_url=endpoint,timeout=120) as client:
        environment=client.get('/api/asap/environment').json()
        storage.write_json(EVIDENCE/'environment.json',environment)
        assert environment.get('ready') and environment.get('learn_ready'),environment
        results=[proof(client,'assembly_only'),proof(client,'assembly_with_parts')]
        states=[client.get('/api/projects/'+r['project_id']).json() for r in results]
        storage.write_json(EVIDENCE/'summary.json',{'environment_ready':True,'project_persistence':'mariadb',
                                                   'native_proofs':results,'project_states':states,
                                                   'settings_note':'Four independent cubes require up to four grippers in the real support check.'})


if __name__=='__main__':main()
