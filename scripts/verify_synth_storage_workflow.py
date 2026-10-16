"""Actual HVJB_SYNTH_S01 through the public API; assert durable result storage."""
from pathlib import Path
import argparse,hashlib,json,os,time
from collections import Counter
import httpx


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--url',required=True);parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--max-seconds',type=int,default=300);args=parser.parse_args()
    client=httpx.Client(base_url=args.url,headers={'X-Pilot-Token':os.environ['PILOT_SESSION_TOKEN']},timeout=180)
    report={'checks':[],'fixture':'HVJB_SYNTH_S01','started_at':time.time(),'health':client.get('/api/health').raise_for_status().json()}
    def save():args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def get(path):return client.get('/api'+path).raise_for_status().json()
    def post(path,body):return client.post('/api'+path,json=body).raise_for_status().json()
    def wait(job):
        deadline=time.monotonic()+4000;last=None
        while time.monotonic()<deadline:
            value=get('/jobs/'+job['id']);state=(value['status'],value.get('message'))
            if last!=state:print(json.dumps({'kind':value['kind'],'status':state[0],'message':state[1]},ensure_ascii=False),flush=True);last=state
            if value['status'] in ('succeeded','failed','canceled','interrupted'):
                report.setdefault('jobs',[]).append(value);save()
                assert value['status']=='succeeded',value
                return value
            time.sleep(1)
        raise TimeoutError('SYNTH_STORAGE_WORKFLOW_DEADLINE')
    try:
        project=post('/projects',{'name':'저장 오류 회귀 · HVJB_SYNTH_S01','part_number':'HVJB_SYNTH_S01'})
        report['project_id']=project['id'];prefix='/projects/'+project['id']
        files=[args.source/'HVJB_SYNTH_S01_ASSEMBLY.step',*sorted((args.source/'parts').glob('*.step'))]
        assert len(files)==40
        report['inputs']=[{'name':p.name,'bytes':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in files];save()
        handles=[]
        try:
            upload=[]
            for index,p in enumerate(files):
                stream=p.open('rb');handles.append(stream);upload.append(('assembly' if index==0 else 'parts',(p.name,stream,'application/octet-stream')))
            wait(client.post('/api'+prefix+'/import',data={'expected_revision':project['revision']},files=upload).raise_for_status().json())
        finally:
            for stream in handles:stream.close()
        project=get(prefix);assert len(project['model']['occurrences'])==128
        report['checks'].append('Fresh original assembly and 39 part STEP files imported; 128 occurrences retained')
        # Reproduce the user's actual rule scope, without adding synthetic labels.
        project=client.patch('/api'+prefix,json={'expected_revision':project['revision'],'rules':{'max_seconds':args.max_seconds,'validation_seconds':args.max_seconds,'fixed_unit_ids':[],'beam_width':4}}).raise_for_status().json()
        wait(post(prefix+'/generate',{'expected_revision':project['revision']}))
        project=get(prefix);plan=project['plan'];assert len(plan['steps'])==128 and project['coverage']['complete']
        assert 'attempt_trace' not in plan
        ref=plan['attempt_trace_artifact'];response=client.get('/api/files/'+ref['file_id']).raise_for_status()
        data=response.content;assert hashlib.sha256(data).hexdigest()==ref['sha256']
        trace=json.loads(data);assert len(trace['attempts'])==ref['count']==plan['search']['attempts']
        assert plan['attempt_trace_summary']['status_counts']==dict(Counter(r['status'] for r in trace['attempts']))
        assert trace['build_id']==report['health']['build_id']
        assert trace['geometry_hash'] and trace['algorithm_version']==plan['algorithm_version']
        report['checks'].extend(['128-card result saved and read back through API','Complete attempt trace downloaded and SHA/count verified','Full trace absent from the project JSON; decision evidence retained'])
        before=project['plan']['steps']
        project=client.patch('/api'+prefix,json={'expected_revision':project['revision'],'name':project['name']+' · 저장 후 재열기 확인'}).raise_for_status().json()
        assert project['plan']['steps']==before
        if project['validation']['status']!='PASS':
            rejection=client.post('/api'+prefix+'/confirm',json={'expected_revision':project['revision']});assert rejection.status_code==422
            report['checks'].append('Blocked geometric result remains unconfirmable')
        report.update(sequence_generated=True,verified_recommendation=project['validation']['status']=='PASS',
            plan={'steps':len(plan['steps']),'step_status_counts':dict(Counter(s['status'] for s in plan['steps'])),'attempt_count':ref['count'],'attempt_trace_artifact':ref,'search':plan['search']},
            validation={'status':project['validation']['status'],'elapsed_seconds':project['validation'].get('elapsed_seconds')},
            safe_playback=project['plan']['playback'],project_response_bytes=len(json.dumps(project,ensure_ascii=False).encode('utf-8')),
            status='PASS',finished_at=time.time())
        save();print(json.dumps({'report':str(args.report),'status':'PASS','steps':128,'attempt_count':ref['count'],'trace_bytes':ref['size'],'validation':project['validation']['status']},ensure_ascii=False),flush=True)
    except Exception as error:
        report.update(error=str(error),finished_at=time.time());save();raise
    finally:client.close()


if __name__=='__main__':main()
