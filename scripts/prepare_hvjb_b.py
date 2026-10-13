"""Real complete HVJB + all supplier STEP references; preserve match evidence."""
from pathlib import Path
import httpx,json,time
ROOT=Path(__file__).resolve().parents[1]
E=ROOT/'evidence/hvjb-final-b';E.mkdir(exist_ok=True)
SOURCE=ROOT.parent/'프로젝트_제공_파일/3D DATA/STP'
c=httpx.Client(base_url='http://127.0.0.1:8782',timeout=180)
def save(name,v): (E/name).write_text(json.dumps(v,ensure_ascii=False,indent=2),encoding='utf-8')
def call(method,path,**kw):
    r=c.request(method,path,**kw);r.raise_for_status();return r.json()
def wait(jid,limit=1800):
    started=time.monotonic();last=None
    while time.monotonic()-started<limit:
        j=call('GET','/api/asap/jobs/'+jid);key=(j['status'],j.get('stage'),int(j.get('elapsed_s',0)//30))
        if key!=last:print(json.dumps({k:j.get(k) for k in ('id','status','stage','elapsed_s','error')},ensure_ascii=False),flush=True);last=key
        if j['status'] in ('success','failed','canceled','interrupted'):
            save(jid+'-job.json',j)
            if j['status']!='success':raise RuntimeError(j)
            return j
        time.sleep(1)
    raise TimeoutError(jid)
if (E/'state.json').exists():s=json.loads((E/'state.json').read_text(encoding='utf-8'))
else:
    p=call('POST','/api/projects',json={'name':'실제 HVJB 하위 부품 기준 검증','input_mode':'assembly_with_parts'})
    with (SOURCE/'KD1_HVJB_91958-KD000.stp').open('rb') as stream:
        r=call('POST','/api/asap/sessions',data={'input_mode':'assembly_with_parts'},files={'file':('KD1_HVJB_91958-KD000.stp',stream)})
    s={'project_id':p['project']['id'],'session_id':r['id'],'parse_job':r['job_id']};save('state.json',s)
    call('POST','/api/projects/'+s['project_id']+'/asap-link',json={'session_id':s['session_id']})
wait(s['parse_job']);base='/api/asap/sessions/'+s['session_id']
if 'template_job' not in s:
    files=sorted((SOURCE/'parts').glob('*.STP'));streams=[p.open('rb') for p in files]
    try:r=call('POST',base+'/templates',files=[('files',(p.name,stream)) for p,stream in zip(files,streams)])
    finally:
        for stream in streams:stream.close()
    s.update(template_job=r['job_id']);save('state.json',s)
wait(s['template_job']);u=call('GET',base);save('session-before-match.json',u)
if 'match_job' not in s:
    r=call('POST',base+'/matches',json={'template_ids':[t['id'] for t in u['templates']['templates'] if t['status']=='ready'],
        'expected_revision':u['unit_state']['revision'],'tolerance_mm':.5,'timeout_s':600})
    s.update(match_job=r['job_id']);save('state.json',s)
wait(s['match_job']);u=call('GET',base);save('session-after-match.json',u)
mid=u['templates']['last_match_id'];match=call('GET',base+'/matches/'+mid);save('matches.json',match)
print(json.dumps({'ready_templates':len(u['templates']['templates']),'candidates':len(match['candidates']),'timeout':match['timeout_reached']},ensure_ascii=False),flush=True)
