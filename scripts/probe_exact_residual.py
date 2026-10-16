import sys,json,argparse,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from diagnose_exact_repair import read,diagnose
from OCP.BRep import BRep_Builder,BRep_Tool
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopoDS import TopoDS
from OCP.TopAbs import TopAbs_EDGE,TopAbs_VERTEX

p=argparse.ArgumentParser();p.add_argument('model');p.add_argument('output');p.add_argument('--all',action='store_true');a=p.parse_args()
model=json.loads(Path(a.model).read_text(encoding='utf-8'))['result'];result=[]
for occ in model['occurrences']:
    if not a.all and not occ.get('repair'):continue
    print(occ['name'],flush=True);start=time.monotonic()
    shape=read(occ['brep_path']);before=diagnose(shape)
    candidate=r.copied(shape);edges=r.shapes(candidate,TopAbs_EDGE);builder=BRep_Builder();budgets=r.original_local_tolerance_budgets(shape)
    targets={}
    for face in before['faces']:
        for edge in face['edges']:
            if edge['exact']['done']:targets[edge['index']]=max(targets.get(edge['index'],0),edge['exact']['distance'])
    changes=[]
    for index,distance in targets.items():
        edge=TopoDS.Edge(edges[index-1]);old=BRep_Tool.Tolerance_s(edge);new=max(old,distance+1e-10)
        builder.UpdateEdge(edge,new)
        for vertex in r.shapes(edge,TopAbs_VERTEX):builder.UpdateVertex(TopoDS.Vertex(vertex),new)
        changes.append(dict(edge_index=index,old=old,new=new))
    row=dict(id=occ['id'],name=occ['name'],before_exact=before['exact_valid'],after_exact=BRepCheck_Analyzer(candidate,True,False,True).IsValid(),after_ordinary=BRepCheck_Analyzer(candidate).IsValid(),changes=changes,local_budget_audit=r.local_tolerance_budget_audit(shape,candidate),maximum_before=r.max_tolerances(shape),maximum_after=r.max_tolerances(candidate),elapsed_seconds=time.monotonic()-start)
    result.append(row);Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(row['after_exact'],len(changes),row['local_budget_audit']['accepted'],flush=True)
