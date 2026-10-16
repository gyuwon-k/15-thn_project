"""Compare exact edge extrema on full and split parameter intervals."""
import sys,json,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from diagnose_exact_repair import read
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAdaptor import GeomAdaptor_Curve,GeomAdaptor_Surface
from OCP.Geom2dAdaptor import Geom2dAdaptor_Curve
from OCP.Adaptor3d import Adaptor3d_CurveOnSurface
from OCP.BRepLib import BRepLib_ValidateEdge
from OCP.GeomLib import GeomLib_CheckCurveOnSurface
from OCP.TopoDS import TopoDS
source=read(sys.argv[1]);index,incident=r._incident_edge_index(source);result=[]
for ei in map(int,sys.argv[3:]):
 edge=TopoDS.Edge(index.FindKey(ei));ref=BRepAdaptor_Curve(edge);low,high=BRep_Tool.Range_s(edge);curve=BRep_Tool.Curve_s(edge,0.,0.)
 for fi,entity,oriented in incident[ei]:
  face=TopoDS.Face(entity);pc=BRep_Tool.CurveOnSurface_s(TopoDS.Edge(oriented),face,0.,0.);surf=BRep_Tool.Surface_s(face)
  first,last=BRep_Tool.Range_s(TopoDS.Edge(oriented),face)
  on=Adaptor3d_CurveOnSurface(Geom2dAdaptor_Curve(pc,first,last),GeomAdaptor_Surface(surf))
  samples=max(ref.Value(low+(high-low)*j/4096).Distance(on.Value(low+(high-low)*j/4096)) for j in range(4097))
  row={'edge':ei,'face':fi,'surface':surf.DynamicType().Name(),'pcurve':pc.DynamicType().Name(),'range':[first,last],'edge_range':[low,high],'tol':BRep_Tool.Tolerance_s(edge),'sample4097':samples,'subdivisions':[]}
  for n in [1,2,4,8,16,32,64,128]:
   maxima=[]
   for j in range(n):
    l,h=low+(high-low)*j/n,low+(high-low)*(j+1)/n
    subref=GeomAdaptor_Curve(curve,l,h);subon=Adaptor3d_CurveOnSurface(Geom2dAdaptor_Curve(pc,l,h),GeomAdaptor_Surface(surf))
    checker=GeomLib_CheckCurveOnSurface(subref);checker.Perform(subon)
    maxima.append(checker.MaxDistance() if checker.IsDone() else None)
   row['subdivisions'].append({'n':n,'max':max(x for x in maxima if x is not None),'values':maxima})
  result.append(row);print({**row,'subdivisions':[{'n':v['n'],'max':v['max']} for v in row['subdivisions']]},flush=True)
Path(sys.argv[2]).write_text(json.dumps(result,indent=2),encoding='utf-8')
