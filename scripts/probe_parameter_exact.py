import sys,json,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from diagnose_exact_repair import read,diagnose
from OCP.BRep import BRep_Builder,BRep_Tool
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepLib import BRepLib
from OCP.TopoDS import TopoDS
from OCP.TopAbs import TopAbs_EDGE,TopAbs_VERTEX
model=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))['result'];result=[]
for occ in model['occurrences']:
 if not occ.get('repair'):continue
 shape=read(occ['brep_path']);source=r.support_geometry(shape)
 for method in ['same_parameter','shape_fix_edge','shape_fix']:
    print(occ['name'],method,flush=True);start=time.monotonic()
    if method=='same_parameter':candidate,settings=r.parameter_fix(shape,True,True)
    elif method=='shape_fix_edge':candidate,settings=r.parameter_fix(shape,False,True,use_shape_fix_edge=True)
    else:candidate,settings=r.shape_fix(shape,1e-4,True)
    before=diagnose(candidate);edges=r.shapes(candidate,TopAbs_EDGE);builder=BRep_Builder();targets={}
    for face in before['faces']:
        for edge in face['edges']:
            if edge['exact']['done']:targets[edge['index']]=max(targets.get(edge['index'],0),edge['exact']['distance'])
    for index,distance in targets.items():
        edge=TopoDS.Edge(edges[index-1]);old=BRep_Tool.Tolerance_s(edge);new=max(old,distance+1e-10)
        builder.UpdateEdge(edge,new)
        for vertex in r.shapes(edge,TopAbs_VERTEX):builder.UpdateVertex(TopoDS.Vertex(vertex),new)
    audit=r.local_tolerance_budget_audit(shape,candidate)
    row=dict(name=occ['name'],method=method,before_exact=before['exact_valid'],after_exact=BRepCheck_Analyzer(candidate,True,False,True).IsValid(),after_ordinary=BRepCheck_Analyzer(candidate).IsValid(),local_budget_accepted=audit['accepted'],violation_count=len(audit['violations']),ratio=audit['max_tolerance_to_local_budget_ratio'],supports_preserved=source==r.support_geometry(candidate),trim_preserved=r.boundary_manifest(shape)==r.boundary_manifest(candidate),pcurve_identity=r.pcurve_manifest(shape)==r.pcurve_manifest(candidate),maximum_before=r.max_tolerances(shape),maximum_after=r.max_tolerances(candidate),elapsed_seconds=time.monotonic()-start)
    result.append(row);Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(row,flush=True)
