import sys,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from diagnose_exact_repair import read,diagnose
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS
from OCP.TopAbs import TopAbs_EDGE,TopAbs_VERTEX
from OCP.TopAbs import TopAbs_FACE
from OCP.BRepCheck import BRepCheck_Analyzer
source=read(sys.argv[1]);audit=json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))['exact_representation_validation'];candidate=r.copied(source)
edges=r.shapes(candidate,TopAbs_EDGE);builder=BRep_Builder()
for row in audit['measurements']:
 edge=TopoDS.Edge(edges[row['edge_index']-1]);builder.UpdateEdge(edge,row['requested_edge_tolerance_mm'])
 for vertex in r.shapes(edge,TopAbs_VERTEX):builder.UpdateVertex(TopoDS.Vertex(vertex),row['requested_edge_tolerance_mm'])
result=diagnose(candidate)
analyzer=BRepCheck_Analyzer(candidate,True,False,True)
for row in result['faces']:
 face=r.shapes(candidate,TopAbs_FACE)[row['face_index']-1]
 row['isolated_exact_valid']=BRepCheck_Analyzer(face,True,False,True).IsValid()
 row['isolated_standard_valid']=BRepCheck_Analyzer(face).IsValid()
 row['native_edge_validity']=[{'index':index,'valid':analyzer.IsValid(edge),'isolated':BRepCheck_Analyzer(edge,True,False,True).IsValid()} for index,edge in enumerate(r.shapes(candidate,TopAbs_EDGE),1) if any(edge.IsSame(e) for e in r.shapes(face,TopAbs_EDGE))]
Path(sys.argv[3]).write_text(json.dumps(result,indent=2),encoding='utf-8');print(result)
