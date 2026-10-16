import sys,json,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from diagnose_exact_repair import read
from OCP.BRep import BRep_Tool
from OCP.TopoDS import TopoDS
from OCP.TopAbs import TopAbs_FACE,TopAbs_EDGE,TopAbs_VERTEX

def differences(a,b,path,out):
 if isinstance(a,dict) and isinstance(b,dict) and a.keys()==b.keys():
  for k in a:differences(a[k],b[k],path+[k],out)
 elif isinstance(a,(tuple,list)) and isinstance(b,(tuple,list)) and len(a)==len(b):
  for i,(x,y) in enumerate(zip(a,b)):differences(x,y,path+[i],out)
 elif a!=b:
  item={'path':path,'source':a,'saved':b}
  if isinstance(a,(int,float)) and isinstance(b,(int,float)):
   item.update(delta=abs(a-b),ulps=abs(a-b)/max(math.ulp(a),math.ulp(b)))
  out.append(item)

def main():
 a,b=map(read,sys.argv[1:3]);out=[]
 for index,(left,right) in enumerate(zip(r.shapes(a,TopAbs_FACE),r.shapes(b,TopAbs_FACE))):
  left,right=TopoDS.Face(left),TopoDS.Face(right)
  differences(r.geometry_coefficients(BRep_Tool.Surface_s(left)),r.geometry_coefficients(BRep_Tool.Surface_s(right)),['face',index],out)
  for ei,(e,f) in enumerate(zip(r.shapes(left,TopAbs_EDGE),r.shapes(right,TopAbs_EDGE))):
   e,f=TopoDS.Edge(e),TopoDS.Edge(f)
   differences(r.geometry_coefficients(BRep_Tool.CurveOnSurface_s(e,left,0.,0.)),r.geometry_coefficients(BRep_Tool.CurveOnSurface_s(f,right,0.,0.)),['pcurve',index,ei],out)
 for index,(e,f) in enumerate(zip(r.shapes(a,TopAbs_EDGE),r.shapes(b,TopAbs_EDGE))):
  e,f=TopoDS.Edge(e),TopoDS.Edge(f)
  differences(r.geometry_coefficients(BRep_Tool.Curve_s(e,0.,0.)),r.geometry_coefficients(BRep_Tool.Curve_s(f,0.,0.)),['curve',index],out)
  differences(BRep_Tool.Range_s(e),BRep_Tool.Range_s(f),['range',index],out)
 for index,(v,w) in enumerate(zip(r.shapes(a,TopAbs_VERTEX),r.shapes(b,TopAbs_VERTEX))):
  v,w=BRep_Tool.Pnt_s(TopoDS.Vertex(v)),BRep_Tool.Pnt_s(TopoDS.Vertex(w))
  differences([v.X(),v.Y(),v.Z()],[w.X(),w.Y(),w.Z()],['vertex',index],out)
 result={'count':len(out),'maximum_delta':max((x.get('delta',0) for x in out),default=0),'maximum_ulps':max((x.get('ulps',0) for x in out),default=0),'differences':out}
 Path(sys.argv[3]).write_text(json.dumps(result,indent=2),encoding='utf-8');print({k:v for k,v in result.items() if k!='differences'});print(out[:10])
if __name__=='__main__':main()
