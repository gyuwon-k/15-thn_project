import sys,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from backend.exact_extrema import measured_residual
from diagnose_exact_repair import read
from OCP.GeomLib import GeomLib_CheckCurveOnSurface
from OCP.BRepLib import BRepLib_ValidateEdge
from OCP.BRep import BRep_Tool
from OCP.TopoDS import TopoDS

def main():
 shape=read(sys.argv[1]);index,incident=r._incident_edge_index(shape);result=[]
 for number in map(int,sys.argv[3:]):
  edge=TopoDS.Edge(index.FindKey(number))
  for face_index,face,oriented in incident[number]:
   calls=[];full=[]
   class Native:
    def __init__(self,*args):self.native=GeomLib_CheckCurveOnSurface(*args)
    def Perform(self,*args):return self.native.Perform(*args)
    def IsDone(self):return self.native.IsDone()
    def MaxDistance(self):
     value=self.native.MaxDistance();calls.append(value);return value
   class Whole:
    def __init__(self,*args):self.native=BRepLib_ValidateEdge(*args)
    def __getattr__(self,name):return getattr(self.native,name)
    def GetMaxDistance(self):
     value=self.native.GetMaxDistance();full.append(value);return value
   try:
    with patch('backend.exact_extrema.GeomLib_CheckCurveOnSurface',Native),patch('backend.exact_extrema.BRepLib_ValidateEdge',Whole):
     proof=measured_residual(edge,TopoDS.Edge(oriented),TopoDS.Face(face))
   except Exception as exc:proof={'error':str(exc)}
   result.append({'edge':number,'face':face_index,'source_tolerance':BRep_Tool.Tolerance_s(edge),'whole':full,'partition_calls':calls,'result':proof})
 Path(sys.argv[2]).write_text(json.dumps(result,indent=2),encoding='utf-8');print(result)
if __name__=='__main__':main()
