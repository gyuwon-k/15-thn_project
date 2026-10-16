import sys,json,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from diagnose_exact_repair import read,diagnose
from OCP.BRep import BRep_Builder,BRep_Tool
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepLib import BRepLib
from OCP.ShapeFix import ShapeFix_Edge,ShapeFix_ShapeTolerance
from OCP.TopoDS import TopoDS
from OCP.TopAbs import TopAbs_EDGE,TopAbs_VERTEX,TopAbs_FACE
model=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))['result'];result=[]
for occ in model['occurrences']:
 if not occ.get('repair'):continue
 shape=read(occ['brep_path']);source=r.support_geometry(shape);start=time.monotonic()
 for method in ['reset_tol_same_parameter','reproject']:
    print(occ['name'],method,flush=True)
    candidate=r.copied(shape);before=diagnose(candidate);edges=r.shapes(candidate,TopAbs_EDGE);faces=r.shapes(candidate,TopAbs_FACE);builder=BRep_Builder();targets=set();errors=[];fix=ShapeFix_Edge()
    for face in before['faces']:
      for edge in face['edges']:
        ei=edge['index'];fi=face['face_index'];e=TopoDS.Edge(edges[ei-1]);f=TopoDS.Face(faces[fi-1]);targets.add(ei)
        try:
          if method=='reproject':
            if BRep_Tool.IsClosed_s(e,f):continue
            builder.UpdateEdge(e,None,f,BRep_Tool.Tolerance_s(e));fix.FixAddPCurve(e,f,False,1e-9)
        except Exception as error:errors.append(str(error))
    for ei in targets:
      e=TopoDS.Edge(edges[ei-1]);ShapeFix_ShapeTolerance().SetTolerance(e,1e-7,TopAbs_EDGE);BRepLib.SameParameter_s(e,1e-9,True)
    after=diagnose(candidate);targets={}
    for face in after['faces']:
        for edge in face['edges']:
            if edge['exact']['done']:targets[edge['index']]=max(targets.get(edge['index'],0),edge['exact']['distance'])
    for index,distance in targets.items():
        edge=TopoDS.Edge(edges[index-1]);new=max(BRep_Tool.Tolerance_s(edge),distance+1e-10);builder.UpdateEdge(edge,new)
        for vertex in r.shapes(edge,TopAbs_VERTEX):builder.UpdateVertex(TopoDS.Vertex(vertex),new)
    audit=r.local_tolerance_budget_audit(shape,candidate)
    row=dict(name=occ['name'],method=method,errors=errors[:3],before_exact=before['exact_valid'],after_exact=BRepCheck_Analyzer(candidate,True,False,True).IsValid(),after_ordinary=BRepCheck_Analyzer(candidate).IsValid(),local_budget_accepted=audit['accepted'],violation_count=len(audit['violations']),ratio=audit['max_tolerance_to_local_budget_ratio'],supports_preserved=source==r.support_geometry(candidate),trim_preserved=r.boundary_manifest(shape)==r.boundary_manifest(candidate),pcurve_identity=r.pcurve_manifest(shape)==r.pcurve_manifest(candidate),maximum_before=r.max_tolerances(shape),maximum_after=r.max_tolerances(candidate),elapsed_seconds=time.monotonic()-start)
    result.append(row);Path(sys.argv[2]).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(row,flush=True)
