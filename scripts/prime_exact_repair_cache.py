"""Validate immutable original STEP import BREPs independently of matching."""
import argparse,hashlib,inspect,json,sys,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.geometry import _repair_worker, repair_occurrences, REPAIR_VERSION

def main():
 p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
 a.output.mkdir(parents=True,exist_ok=True);began=time.monotonic()
 modules=[Path(__file__).resolve().parents[1]/'backend'/name for name in ('representation_repair.py','exact_extrema.py')]
 hashes={path.name:hashlib.sha256(path.read_bytes()).hexdigest() for path in modules}
 hashes.update({f.__name__:hashlib.sha256(inspect.getsource(f).encode()).hexdigest() for f in (_repair_worker,repair_occurrences)})
 manifests=sorted(a.cache.glob('*/manifest.json'),key=lambda p:('3048cab7' not in p.parent.name,str(p)))
 reports=[]
 for manifest in manifests:
  assembly=json.loads(manifest.read_text(encoding='utf-8'));source=assembly['source']['name']
  def progress(event):
   row={'source':source,'elapsed_seconds':round(time.monotonic()-began,3),**event}
   with (a.output/'prime-repair-progress.jsonl').open('a',encoding='utf-8') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
   print(json.dumps(row,ensure_ascii=True),flush=True)
  repaired=repair_occurrences(assembly,a.cache,progress)
  (a.output/('repaired-source-'+assembly['source']['sha256'][:20]+'.json')).write_text(json.dumps(repaired,ensure_ascii=False,indent=2),encoding='utf-8')
  reports.append({'source':source,'occurrences':len(repaired['instances']),
   'exact_pass':sum(row.get('exact_method_valid') is True for row in repaired['instances']),
   'review':[{'id':row['id'],'name':row['name'],'repair':row['repair']} for row in repaired['instances'] if row.get('exact_method_valid') is not True]})
  (a.output/'prime-repair-summary.json').write_text(json.dumps({'repair_version':REPAIR_VERSION,'repair_source_hashes':hashes,'sources':reports},ensure_ascii=False,indent=2),encoding='utf-8')
 assert all(hashes[path.name]==hashlib.sha256(path.read_bytes()).hexdigest() for path in modules),'Repair source changed during cache validation'
 print(json.dumps({'completed_sources':len(reports),'elapsed_seconds':time.monotonic()-began}),flush=True)

if __name__=='__main__':main()
