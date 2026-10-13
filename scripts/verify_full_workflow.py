"""Actual HTTP/MariaDB/native-path adoption to immutable confirmation and MP4."""
import json,time,uuid,hashlib
from pathlib import Path
import httpx
root=Path(__file__).resolve().parents[1];evidence=root/'evidence/full-workflow';evidence.mkdir(parents=True,exist_ok=True)
client=httpx.Client(base_url='http://127.0.0.1:8770',timeout=60)
pid='project-66314d4cc9704a4880ce03047ee5d1d4';base='/api/projects/'+pid
def call(method,url,**kwargs):
    r=client.request(method,url,**kwargs);r.raise_for_status();return r.json()
def action(name,body):return call('POST',base+'/actions/'+name,json=body)
def wait(jid,timeout=240):
    deadline=time.monotonic()+timeout
    while time.monotonic()<deadline:
        state=call('GET',base)
        job=next(j for j in state['jobs'] if j['id']==jid)
        if job['status'] in ('succeeded','failed','canceled'):
            if job['status']!='succeeded':raise RuntimeError(json.dumps(job,ensure_ascii=False))
            return job
        time.sleep(.3)
    raise TimeoutError(jid)
state=call('GET',base)
if state['plan']['status']=='confirmed':state=action('new_draft',{'reason':'Independent full-flow proof'})
state=action('update_project',{'name':'완제품＋하위 부품 통합 기능 검증','vehicle_model':'TEST','item_name':'Synthetic native integration','product_no':'TEST-NATIVE-001'})
for n,p in enumerate(state['parts'],1):
    state=action('update_part',{'id':p['id'],'company_part_no':f'TEST-{n:03d}','display_name':p['display_name'],'metadata_source':'개발 시험 형상; 기업 부품 메타데이터 아님'})
for op in state['operations']:
    state=action('update_operation',{'id':op['id'],'review_status':'reviewed','duration_s':.5,
        'warning':{'status':'explicit_none','text':'','source_ref':'개발 시험 검토'},'source_ref':'합성 기능 검증'})
assert not state['plan'].get('asap_path_stale'), 'Metadata edits must preserve native paths'
validate=action('validate',{});wait(validate['job_id'])
state=call('GET',base)
assert state['validation']['status']=='validated',state['validation']
confirmed=action('confirm',{'reviewer':'개발 검증용 검토자','reason':'합성 입력의 기능 연결 검증; 실제 현장 승인 아님'})
snapshot=confirmed['plan']['snapshot_id'];snapshot_data=call('GET','/api/snapshots/'+snapshot)
assert all(o.get('asap_motion',{}).get('verified') or o.get('asap_motion',{}).get('base') for o in snapshot_data['operations'])
render=action('render',{'snapshot_id':snapshot,'preset':'quick'});job=wait(render['job_id'])
package=action('package',{'job_id':render['job_id']})
rendered=next(j for j in package['jobs'] if j['id']==render['job_id'])
video=client.get(rendered['output_url']);video.raise_for_status()
(evidence/'assembly.mp4').write_bytes(video.content)
archive=client.get(rendered['package_url']);archive.raise_for_status();(evidence/'web-package.zip').write_bytes(archive.content)
request={'request_id':'proof-'+uuid.uuid4().hex,'command':'project.create','payload':{'name':'명령 멱등성 검증'}}
first=call('POST','/api/commands',json=request);second=call('POST','/api/commands',json=request)
assert first==second
conflict=client.post('/api/commands',json={**request,'payload':{'name':'different'}});assert conflict.status_code in (409,422)
backup=call('POST','/api/backups',json={})
summary={'project_id':pid,'snapshot_id':snapshot,'sequence_and_native_paths':'success','field_validation':state['validation']['status'],
         'snapshot_hash':hashlib.sha256(json.dumps(snapshot_data,sort_keys=True,ensure_ascii=False).encode()).hexdigest(),
         'render':rendered.get('result'),'command_replay_identical':True,'idempotency_conflict_http':conflict.status_code,'backup':backup,
         'scope':'synthetic integration; independent business CAD/full HVJB sequence unproven'}
(evidence/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False),flush=True)
