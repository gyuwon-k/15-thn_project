"""Reproducible edge/face exact-check diagnosis of the supplied model cache."""
import json, sys, time, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepTools import BRepTools
from OCP.TopoDS import TopoDS_Shape, TopoDS
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAdaptor import GeomAdaptor_Surface
from OCP.Geom2dAdaptor import Geom2dAdaptor_Curve
from OCP.Adaptor3d import Adaptor3d_CurveOnSurface
from OCP.BRepLib import BRepLib_ValidateEdge
from OCP.TopAbs import TopAbs_FACE, TopAbs_EDGE

def read(path):
    shape = TopoDS_Shape()
    assert BRepTools.Read_s(shape, str(path), BRep_Builder())
    return shape

def diagnose(shape):
    ordinary, exact = BRepCheck_Analyzer(shape), BRepCheck_Analyzer(shape, True, False, True)
    result = dict(standard_valid=ordinary.IsValid(), exact_valid=exact.IsValid(), faces=[])
    if result['exact_valid']: return result
    edgeindex, incident = r._incident_edge_index(shape)
    for fi, face in enumerate(r.shapes(shape, TopAbs_FACE), 1):
        if exact.IsValid(face): continue
        face = TopoDS.Face(face)
        row = dict(face_index=fi, standard_valid=ordinary.IsValid(face), surface=BRep_Tool.Surface_s(face).DynamicType().Name(), edges=[])
        for entity in r.shapes(face, TopAbs_EDGE):
            edge=TopoDS.Edge(entity)
            if BRep_Tool.Degenerated_s(edge): continue
            pc=BRep_Tool.CurveOnSurface_s(edge, face, 0., 0.)
            first,last=BRep_Tool.Range_s(edge,face)
            on=Adaptor3d_CurveOnSurface(Geom2dAdaptor_Curve(pc,first,last),GeomAdaptor_Surface(BRep_Tool.Surface_s(face)))
            ref=BRepAdaptor_Curve(edge)
            checks={}
            for exact_method in (False,True):
                val=BRepLib_ValidateEdge(ref,on,BRep_Tool.SameParameter_s(edge));val.SetExactMethod(exact_method);val.Process()
                checks['exact' if exact_method else 'sampled']=dict(done=val.IsDone(),distance=val.GetMaxDistance(),within_tolerance=val.CheckTolerance(BRep_Tool.Tolerance_s(edge)))
            if not checks['exact']['done'] or not checks['exact']['within_tolerance']:
                row['edges'].append(dict(index=edgeindex.FindIndex(edge),tolerance=BRep_Tool.Tolerance_s(edge),same_parameter=BRep_Tool.SameParameter_s(edge),range=[first,last],pcurve_type=pc.DynamicType().Name(),**checks))
        result['faces'].append(row)
    return result

def main():
    p=argparse.ArgumentParser();p.add_argument('model');p.add_argument('output');p.add_argument('--all',action='store_true');a=p.parse_args()
    model=json.loads(Path(a.model).read_text(encoding='utf-8'));model=model.get('result',model)
    result=[]
    for occ in model['occurrences']:
        if not a.all and not occ.get('representation_repair'): continue
        print(occ['name'],flush=True)
        began=time.monotonic();row={'id':occ['id'],'name':occ['name'],'path':occ['brep_path'],**diagnose(read(occ['brep_path'])),'elapsed_seconds':time.monotonic()-began}
        result.append(row);Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        print(row['standard_valid'],row['exact_valid'],len(row['faces']),flush=True)

if __name__=='__main__':main()
