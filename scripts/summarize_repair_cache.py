"""Read-only cache audit; optional clearly marked geometry-only planner fixture."""
import argparse,hashlib,json,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.geometry import REPAIR_VERSION

def main():
 p=argparse.ArgumentParser();p.add_argument('--cache',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--baseline-model',type=Path);a=p.parse_args()
 a.output.mkdir(parents=True,exist_ok=True);sources=[];assembly=None
 for manifest in a.cache.glob('*/manifest.json'):
  document=json.loads(manifest.read_text(encoding='utf-8'));rows=[]
  for occurrence in document['instances']:
   digest=hashlib.sha256(Path(occurrence['brep_path']).read_bytes()).hexdigest()
   result_path=a.cache/REPAIR_VERSION/(digest[:24]+'.json')
   result=json.loads(result_path.read_text(encoding='utf-8')) if result_path.exists() else {'status':'pending'}
   rows.append({'id':occurrence['id'],'name':occurrence['name'],'solid_count':occurrence['solid_count'],**result})
  report={'source':document['source'],'occurrences':rows,'pending':sum(r['status']=='pending' for r in rows),
   'exact_pass':sum(r.get('exact_valid') is True for r in rows),'exact_review':sum(r['status']!='pending' and r.get('exact_valid') is not True for r in rows)}
  sources.append(report)
  if len(document['instances'])==78:assembly=(document,report)
 (a.output/'repair-cache-progress.json').write_text(json.dumps({'repair_version':REPAIR_VERSION,'sources':sources},ensure_ascii=False,indent=2),encoding='utf-8')
 print(json.dumps([{'file':r['source']['name'],'count':len(r['occurrences']),**{k:r[k] for k in ('pending','exact_pass','exact_review')}} for r in sources],ensure_ascii=True))
 if assembly and not assembly[1]['pending']:
  (a.output/'assembly-repair-summary.json').write_text(json.dumps(assembly[1]['occurrences'],ensure_ascii=False,indent=2),encoding='utf-8')
  if a.baseline_model:
   model=json.loads(a.baseline_model.read_text(encoding='utf-8'));rows=[];proofs={r['id']:r for r in assembly[1]['occurrences']}
   for row in assembly[0]['instances']:
    proof=proofs[row['id']];row['repair']={**proof,'version':REPAIR_VERSION}
    row.update(exact_method_valid=proof.get('exact_valid'),collision_ready=proof.get('exact_valid') is True,
      representation_uncertainty_mm=proof.get('representation_uncertainty_mm'),analysis_scope=proof.get('analysis_scope'),
      collision_clearance_guard_required=proof.get('collision_clearance_guard_required',False),calibration_budget=proof.get('calibration_budget'))
    if proof.get('candidate_path'):row['source_brep_path']=row['brep_path'];row['brep_path']=proof['candidate_path'];row['repaired_valid']=bool(proof.get('standard_valid'))
    rows.append(row)
   model.update(occurrences=rows,repair_version=REPAIR_VERSION,temporary_geometry_fixture=True)
   model['fingerprint']=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
   by_id={r['id']:r for r in rows}
   for unit in model['units']:unit['collision_ready']=all(by_id[i]['collision_ready'] for i in unit['occurrence_ids'])
   model['quality'].update(status='LIMITED',matching_ready=False,matching_geometry_proof_pending=True,
    exact_method_disagreement_ids=[r['id'] for r in rows if not r['exact_method_valid']],
    collision_ready=all(r['collision_ready'] for r in rows),invalid_occurrence_ids=[r['id'] for r in rows if not r['collision_ready']])
   model['fixture_scope']='Final-version fresh representation repair with prior unit inventory retained only for geometry/planner iteration. Final matching is still running; this is not final preprocessing evidence.'
   (a.output/'hvjb-geometry-preview.json').write_text(json.dumps(model,ensure_ascii=False,indent=2),encoding='utf-8')

if __name__=='__main__':main()
