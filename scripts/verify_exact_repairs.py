"""Fresh bounded exact repair plus BREP round-trip verification on real CAD."""
import sys,json,time,argparse
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from diagnose_exact_repair import read
from OCP.BRepTools import BRepTools
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopAbs import TopAbs_SOLID

def main():
 p=argparse.ArgumentParser();p.add_argument('model');p.add_argument('output');p.add_argument('--all',action='store_true');p.add_argument('--coarse',action='store_true');p.add_argument('--original',action='store_true');a=p.parse_args()
 model=json.loads(Path(a.model).read_text(encoding='utf-8'));model=model.get('result',model)
 destination=Path(a.output);destination.mkdir(parents=True,exist_ok=True);result=[]
 for occ in model['occurrences']:
  if not a.all and not occ.get('repair'):continue
  print(occ['name'],flush=True);shape=read(occ.get('source_brep_path',occ['brep_path']) if a.original else occ['brep_path'])
  topology=None
  if not BRepCheck_Analyzer(shape).IsValid():
   fixed,topology=r.verified_topology_repair(shape)
   if fixed is not None:shape=fixed
  candidate,audit=r.verified_exact_representation_repair(shape,allow_coarse_calibration=a.coarse)
  if topology:audit['initial_topology_repair']=topology
  (destination/(occ['id']+'-audit.json')).write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
  row=dict(id=occ['id'],name=occ['name'],status=audit['status'],reason=audit.get('reason'),exact_before=audit['source_exact_valid'],maximum_increment_mm=audit.get('maximum_required_increment_mm'),rejected_edges=len(audit.get('rejected_edges',[])))
  if candidate is not None:
   path=destination/(occ['id']+'.brep');BRepTools.Write_s(candidate,str(path));fresh=read(path)
   row.update(exact_after=BRepCheck_Analyzer(fresh,True,False,True).IsValid(),standard_after=BRepCheck_Analyzer(fresh).IsValid(),source_solid_count=len(r.shapes(shape,TopAbs_SOLID)),roundtrip_solid_count=len(r.shapes(fresh,TopAbs_SOLID)),preservation_checks=audit['preservation_checks'],representation_uncertainty_mm=audit['representation_uncertainty_mm'])
   assert row['exact_after'] and row['standard_after'] and row['source_solid_count']==row['roundtrip_solid_count'] and all(row['preservation_checks'].values())
  result.append(row);(destination/'summary.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(row['status'],row.get('exact_after'),flush=True)

if __name__=='__main__':main()
