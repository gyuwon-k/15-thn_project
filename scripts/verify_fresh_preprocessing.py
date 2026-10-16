"""Reimport original STEP/part files with full generic validation and matching."""
import sys,json,time,argparse,hashlib
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.geometry import preprocess

def main():
 p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--cache',type=Path);a=p.parse_args()
 a.output.mkdir(parents=True,exist_ok=True);events=a.output/'progress.jsonl';began=time.monotonic()
 cache=a.cache or a.output/'cache'
 cached_manifests=[str(path.resolve()) for path in cache.glob('*/manifest.json')]
 code_files=[Path(__file__).resolve().parents[1]/'backend'/name for name in ('geometry.py','representation_repair.py','exact_extrema.py','geometry_matching.py')]
 source_hashes={str(path.relative_to(Path(__file__).resolve().parents[1])):hashlib.sha256(path.read_bytes()).hexdigest() for path in code_files}
 run_id=str(time.time_ns())
 (a.output/'run-manifest.json').write_text(json.dumps({'run_id':run_id,'source_hashes':source_hashes,'immutable_import_cache':str(cache.resolve()),'preexisting_import_manifests':cached_manifests},indent=2),encoding='utf-8')
 def progress(event):
  row={'run_id':run_id,'elapsed_seconds':round(time.monotonic()-began,3),**event}
  with events.open('a',encoding='utf-8') as stream:stream.write(json.dumps(row,ensure_ascii=False)+'\n')
  if event.get('stage') in ('cad_read','part_import','geometry_repair','matching_geometry'):print(json.dumps(row,ensure_ascii=True),flush=True)
 model=preprocess(a.source/'KD1_HVJB_91958-KD000.stp',sorted((a.source/'parts').glob('*.STP')),cache,progress)
 (a.output/'hvjb-model.json').write_text(json.dumps(model,ensure_ascii=False,indent=2),encoding='utf-8')
 occurrences=model['occurrences']
 template_audits=[]
 sources={row['sha256'] for row in model['part_sources']}
 for path in cache.glob('*/manifest.json'):
  imported=json.loads(path.read_text(encoding='utf-8'))
  if imported['source']['sha256'] not in sources:continue
  checked=[]
  for row in imported['instances']:
   sha=hashlib.sha256(Path(row['brep_path']).read_bytes()).hexdigest()
   record=cache/model['repair_version']/(sha[:24]+'.json')
   proof=json.loads(record.read_text(encoding='utf-8'))
   checked.append({'id':row['id'],'name':row['name'],'solid_count':row['solid_count'],**proof})
  template_audits.append({'file':imported['source']['name'],'source_sha256':imported['source']['sha256'],'occurrences':checked,
    'exact_pass':sum(row.get('exact_valid') is True for row in checked),'exact_review':sum(row.get('exact_valid') is not True for row in checked)})
 (a.output/'template-exact-audit.json').write_text(json.dumps(template_audits,ensure_ascii=False,indent=2),encoding='utf-8')
 summary={'actual_fresh_step_import':not cached_manifests,'import_mode':'fresh_step_import' if not cached_manifests else 'reprocess_immutable_original_step_import_cache','source_sha256':model['source']['sha256'],'repair_version':model['repair_version'],
  'matching_version':model['matching_version'],'occurrences':len(occurrences),'solids':sum(o['solid_count'] for o in occurrences),
  'units':len(model['units']),'exact_valid_occurrences':sum(o.get('exact_method_valid') is True for o in occurrences),
  'remaining_exact_review':[{'id':o['id'],'name':o['name'],'reason':o.get('repair',{}).get('reason'),'audit_path':o.get('repair',{}).get('audit_path'),'representation_uncertainty_mm':o.get('representation_uncertainty_mm')} for o in occurrences if o.get('exact_method_valid') is not True],
  'quality':model['quality'],'elapsed_seconds':time.monotonic()-began,'template_files':len(template_audits),
  'template_occurrences':sum(len(row['occurrences']) for row in template_audits),'template_exact_pass':sum(row['exact_pass'] for row in template_audits),
  'template_exact_review':sum(row['exact_review'] for row in template_audits),
  'source_hashes':source_hashes}
 assert source_hashes=={str(path.relative_to(Path(__file__).resolve().parents[1])):hashlib.sha256(path.read_bytes()).hexdigest() for path in code_files},'Source code changed during verification'
 (a.output/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
 assert len(occurrences)==78 and summary['solids']==176
 assert model['quality']['inventory_complete']
 original_six=[o for o in occurrences if o['name'].startswith('KET400_P_ASM') or o['name'].startswith('BLT-WASM612A_M6 WASHER BOLT ASSY')]
 assert len(original_six)==6 and all(o.get('exact_method_valid') is True for o in original_six)
 print(json.dumps({k:v for k,v in summary.items() if k not in ('quality','remaining_exact_review','source_hashes')},ensure_ascii=True),flush=True)

if __name__=='__main__':main()
