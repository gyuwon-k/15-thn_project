import json,time
from pathlib import Path
import httpx
root=Path(__file__).resolve().parents[1];folder=root/'evidence/full-workflow'
summary=json.loads((folder/'summary.json').read_text(encoding='utf-8'));pid=summary['project_id'];c=httpx.Client(base_url='http://127.0.0.1:8770',timeout=60)
r=c.post('/api/projects/'+pid+'/actions/render',json={'snapshot_id':summary['snapshot_id'],'preset':'quick'});r.raise_for_status();jid=r.json()['job_id']
deadline=time.monotonic()+120
while time.monotonic()<deadline:
    r=c.get('/api/projects/'+pid);r.raise_for_status();job=next(j for j in r.json()['jobs'] if j['id']==jid)
    if job['status'] in ('succeeded','failed','canceled'):break
    time.sleep(.3)
assert job['status']=='succeeded',job
r=c.get(job['output_url']);r.raise_for_status();(folder/'assembly-final.mp4').write_bytes(r.content)
(folder/'final-render.json').write_text(json.dumps(job,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'job':jid,'frames':job['result']['decoded_frame_count'],'status':job['status'],'video':str(folder/'assembly-final.mp4')},ensure_ascii=False))
