"""Temporary planner fixture: fresh CAD, previous unit inventory, no match claim."""
import json,hashlib,sys
from pathlib import Path
p=Path(sys.argv[1]);prior=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'));prior=prior.get('result',prior)
assembly=json.loads(next((p/'cache').glob('3048*/manifest.json')).read_text(encoding='utf-8'))
results={x['name']:x for x in json.loads((p/'assembly-repair-summary.json').read_text(encoding='utf-8'))}
rows=[]
for row in assembly['instances']:
 result=results[row['name']]; row['repair']={k:v for k,v in result.items() if k!='name'}
 for key in ('exact_method_valid','representation_uncertainty_mm','analysis_scope','collision_clearance_guard_required'):
  row[key]=result.get('exact_valid' if key=='exact_method_valid' else key)
 if result['status']=='accepted':row['source_brep_path']=row['brep_path'];row['brep_path']=result['candidate_path'];row['repaired_valid']=True;row['collision_ready']=True
 rows.append(row)
prior.update(occurrences=rows,source=assembly['source'],scene_path=assembly['scene_path'],repair_version='exact-residual-preserved-2.1.0',temporary_geometry_fixture=True)
prior['fingerprint']=hashlib.sha256(json.dumps(rows,sort_keys=True).encode()).hexdigest()
prior['quality']['exact_method_disagreement_ids']=[x['id'] for x in rows if x.get('exact_method_valid') is not True]
prior['quality']['status']='LIMITED';prior['quality']['matching_geometry_proof_pending']=True
prior['fixture_scope']='Fresh original STEP geometry and exact repair; previous unit inventory reused only while full geometry matching runs. Not final preprocessing evidence.'
(p/'hvjb-geometry-preview.json').write_text(json.dumps(prior,ensure_ascii=False,indent=2),encoding='utf-8')
print(p/'hvjb-geometry-preview.json')
