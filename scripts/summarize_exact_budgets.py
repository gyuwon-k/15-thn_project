import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from diagnose_exact_repair import read
from OCP.TopAbs import TopAbs_EDGE
a=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'));out=[]
for occ in a:
 if occ['exact_valid']:continue
 shape=read(occ['path']);budgets=r.original_local_tolerance_budgets(shape);edges=r.shapes(shape,TopAbs_EDGE)
 rows=[]
 for face in occ['faces']:
  for edge in face['edges']:
   key=r.edge_key(edges[edge['index']-1]);budget=budgets['edge'][key]
   rows.append(dict(edge_index=edge['index'],budget=budget,actual=edge['exact']['distance'],excess=edge['exact']['distance']-budget))
 worst=max(rows,key=lambda x:x['excess'],default={})
 out.append(dict(name=occ['name'],exact_bad_faces=len(occ['faces']),max_excess=worst.get('excess'),worst=worst,within_1e4_increment=all(x['excess']<=1e-4 for x in rows)))
Path(sys.argv[2]).write_text(json.dumps(out,ensure_ascii=False,indent=2),encoding='utf-8')
print('fixed potential',sum(x['within_1e4_increment'] for x in out),'of',len(out));print([x for x in out if not x['within_1e4_increment']])
