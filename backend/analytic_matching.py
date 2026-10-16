"""Conservative full-parameter correspondence audit; no sampled PASS.

This optional audit is deliberately separate from the production Boolean
matcher. It reports unsupported parameterizations rather than declaring
different BREP exports identical from their vertices or bounding boxes.
"""
from __future__ import annotations
import math
from collections import Counter
import numpy as np
from OCP.BRep import BRep_Tool
from OCP.BRepTools import BRepTools
from OCP.TopAbs import TopAbs_FACE,TopAbs_EDGE,TopAbs_VERTEX,TopAbs_WIRE
from OCP.TopExp import TopExp,TopExp_Explorer
from OCP.TopoDS import TopoDS
from .representation_repair import KINDS,ShapeMap,shapes,geometry_coefficients

VERSION="full-parametric-audit-0.2"


def _vec(point):
    return np.array([point.X(),point.Y(),point.Z()] if hasattr(point,"Z") else [point.X(),point.Y()])


def _norm(value):return float(np.linalg.norm(value))


def _rational_bound(a,b):
    """Convex-hull quotient bound for an IDENTICAL nonnegative spline basis."""
    pa,pb=np.asarray(a["poles"]),np.asarray(b["poles"])
    wa,wb=np.asarray(a["weights"]),np.asarray(b["weights"])
    if pa.shape!=pb.shape or wa.shape!=wb.shape or min(float(wa.min()),float(wb.min()))<=0:
        raise ValueError("CONTROL_NET_OR_WEIGHT_STRUCTURE")
    delta=float(np.max(np.linalg.norm(pa-pb,axis=-1)))
    origin=pb.reshape(-1,pb.shape[-1]).mean(axis=0)
    radius=float(np.max(np.linalg.norm(pb-origin,axis=-1)))
    return delta+2*radius*float(np.max(abs(wa-wb)))/min(float(wa.min()),float(wb.min()))


def _same_basis(a,b):
    return {k:v for k,v in a.items() if k not in ("poles","weights")}=={k:v for k,v in b.items() if k not in ("poles","weights")}


def curve_bound(source,target,source_range,target_range):
    """Uniform distance upper bound over both complete normalized trim intervals."""
    if source is None or target is None:
        if source is target:return 0.
        raise ValueError("MISSING_CURVE")
    kind=source.DynamicType().Name()
    if kind!=target.DynamicType().Name():raise ValueError("CURVE_PARAMETERIZATION_TYPE_DIFFERS")
    first,last=map(float,source_range);other_first,other_last=map(float,target_range)
    if "Line" in kind:
        return max(_norm(_vec(source.Value(first))-_vec(target.Value(other_first))),
                   _norm(_vec(source.Value(last))-_vec(target.Value(other_last))))
    if "Circle" in kind:
        def coefficients(curve,start,span):
            position=curve.Position();x=_vec(position.XDirection());y=_vec(position.YDirection());radius=float(curve.Radius())
            return _vec(position.Location()),radius*(math.cos(start)*x+math.sin(start)*y),math.copysign(radius,span)*(-math.sin(start)*x+math.cos(start)*y)
        a=coefficients(source,first,last-first);b=coefficients(target,other_first,other_last-other_first)
        return sum(_norm(x-y) for x,y in zip(a,b))+abs(abs(last-first)-abs(other_last-other_first))*float(target.Radius())
    if "BSplineCurve" in kind or "BezierCurve" in kind:
        a,b=geometry_coefficients(source),geometry_coefficients(target)
        if source_range!=target_range or not _same_basis(a,b):raise ValueError("SPLINE_COMMON_TRIMMED_BASIS_NOT_PROVED")
        return _rational_bound(a,b)
    raise ValueError("UNSUPPORTED_COMPLETE_CURVE_BOUND:"+kind)


def surface_bound(source,target,uv_bounds):
    """Full-domain bound plus conservative (u,v) Lipschitz coefficients."""
    kind=source.DynamicType().Name()
    if kind!=target.DynamicType().Name():raise ValueError("SURFACE_PARAMETERIZATION_TYPE_DIFFERS")
    if kind in ("Geom_BSplineSurface","Geom_BezierSurface"):
        a,b=geometry_coefficients(source),geometry_coefficients(target)
        if not _same_basis(a,b):raise ValueError("SPLINE_COMMON_TRIMMED_BASIS_NOT_PROVED")
        # Surface bound is complete. Pcurve displacement requires a separate
        # derivative bound; infinity forbids accepting a sampled substitute.
        return _rational_bound(a,b),(math.inf,math.inf)
    pa,pb=source.Position(),target.Position()
    delta=_norm(_vec(pa.Location())-_vec(pb.Location()))
    dx=_norm(_vec(pa.XDirection())-_vec(pb.XDirection()));dy=_norm(_vec(pa.YDirection())-_vec(pb.YDirection()))
    dz=_norm(_vec(pa.Direction())-_vec(pb.Direction()))
    u=max(abs(uv_bounds[0]),abs(uv_bounds[1]));v=max(abs(uv_bounds[2]),abs(uv_bounds[3]))
    if kind=="Geom_Plane":return delta+u*dx+v*dy,(1.,1.)
    if kind=="Geom_CylindricalSurface":
        ra,rb=float(source.Radius()),float(target.Radius());r=max(ra,rb)
        return delta+abs(ra-rb)+r*(dx+dy)+v*dz,(r,1.)
    if kind=="Geom_SphericalSurface":
        ra,rb=float(source.Radius()),float(target.Radius());r=max(ra,rb)
        return delta+abs(ra-rb)+r*(dx+dy+dz),(r,r)
    if kind=="Geom_ToroidalSurface":
        r=max(source.MajorRadius(),target.MajorRadius());q=max(source.MinorRadius(),target.MinorRadius())
        return delta+abs(source.MajorRadius()-target.MajorRadius())+abs(source.MinorRadius()-target.MinorRadius())+(r+q)*(dx+dy)+q*dz,(r+q,q)
    if kind=="Geom_ConicalSurface":
        aa,ab=float(source.SemiAngle()),float(target.SemiAngle());ra,rb=float(source.RefRadius()),float(target.RefRadius())
        radius=max(abs(ra),abs(rb))+v*max(abs(math.sin(aa)),abs(math.sin(ab)))
        bound=delta+abs(ra-rb)+v*abs(math.sin(aa)-math.sin(ab))+radius*(dx+dy)+v*(abs(math.cos(aa)-math.cos(ab))+dz)
        return bound,(radius,1.)
    raise ValueError("UNSUPPORTED_COMPLETE_SURFACE_BOUND:"+kind)


def audit_correspondence(source,target,tolerance_mm=1e-5):
    """Inputs must already share one proper rigid coordinate frame.

    A matching traversal proposes correspondence, then every vertex, edge,
    supporting surface and oriented per-face 2D trimming trace is bounded.
    Unsupported/split topology remains REVIEW rather than a mismatch claim.
    """
    counts=[{name:len(shapes(s,kind)) for name,kind in KINDS.items()} for s in (source,target)]
    if counts[0]!=counts[1]:return {"status":"REVIEW","reason":"TOPOLOGY_COUNTS_DIFFER","counts":counts,"version":VERSION}
    diagnostics=[];metrics={};maximum=0.;edge_maps=[]
    for shape in (source,target):
        index=ShapeMap();TopExp.MapShapes_s(shape,TopAbs_EDGE,index);edge_maps.append(index)
    def record(category,index,work):
        nonlocal maximum
        metrics.setdefault(category,{"total":0,"bounded":0,"within_tolerance":0,"maximum_bound_mm":0.})
        row=metrics[category];row["total"]+=1
        try:
            value=float(work())
            if not math.isfinite(value):raise ValueError("NO_FINITE_COMPLETE_BOUND")
            row["bounded"]+=1;row["maximum_bound_mm"]=max(row["maximum_bound_mm"],value);maximum=max(maximum,value)
            if value<=tolerance_mm:row["within_tolerance"]+=1
            else:diagnostics.append({"category":category,"index":index,"reason":"BOUND_EXCEEDS_REQUESTED_PRECISION","bound_mm":value})
        except Exception as exc:diagnostics.append({"category":category,"index":index,"reason":str(exc)})
    # CAD exporters reorder entities. A complete sparse bipartite assignment
    # preserves repeated coincident vertices instead of nearest-neighbour
    # counting the same counterpart several times.
    from scipy.spatial import cKDTree
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import min_weight_full_bipartite_matching
    vertex_sets=[np.array([_vec(BRep_Tool.Pnt_s(TopoDS.Vertex(v))) for v in shapes(s,TopAbs_VERTEX)]) for s in (source,target)]
    try:
        distances,neighbors=cKDTree(vertex_sets[1]).query(vertex_sets[0],k=min(64,len(vertex_sets[1])),distance_upper_bound=tolerance_mm)
        if distances.ndim==1:distances=distances[:,None];neighbors=neighbors[:,None]
        ii,jj=np.where(np.isfinite(distances));graph=csr_matrix((distances[ii,jj]+1e-20,(ii,neighbors[ii,jj])),shape=(len(vertex_sets[0]),len(vertex_sets[1])))
        ri,ci=min_weight_full_bipartite_matching(graph)
        for i,j in zip(ri,ci):record("vertices",int(i),lambda:_norm(vertex_sets[0][i]-vertex_sets[1][j]))
    except Exception as exc:
        diagnostics.append({"category":"vertices","reason":"COMPLETE_VERTEX_BIJECTION_NOT_PROVED","detail":str(exc)})
    edge_reversed={}
    for i,(a,b) in enumerate(zip(shapes(source,TopAbs_EDGE),shapes(target,TopAbs_EDGE))):
        left,right=TopoDS.Edge(a),TopoDS.Edge(b)
        def check_edge():
            aa,bb=BRep_Tool.Curve_s(left,0.,0.),BRep_Tool.Curve_s(right,0.,0.)
            ar,br=BRep_Tool.Range_s(left),BRep_Tool.Range_s(right)
            forward=curve_bound(aa,bb,ar,br)
            try:reverse=curve_bound(aa,bb,ar,br[::-1])
            except Exception:reverse=math.inf
            edge_reversed[i+1]=reverse<forward
            return min(forward,reverse)
        record("edge_traces",i,check_edge)
    for i,(a,b) in enumerate(zip(shapes(source,TopAbs_FACE),shapes(target,TopAbs_FACE))):
        left,right=TopoDS.Face(a),TopoDS.Face(b)
        if left.Orientation()!=right.Orientation():diagnostics.append({"category":"face_orientation","index":i,"reason":"ORIENTATION_DIFFERS"})
        surfaces=[BRep_Tool.Surface_s(face) for face in (left,right)];bounds=[BRepTools.UVBounds_s(face) for face in (left,right)]
        uv=(min(x[0] for x in bounds),max(x[1] for x in bounds),min(x[2] for x in bounds),max(x[3] for x in bounds))
        record("support_surfaces",i,lambda:surface_bound(*surfaces,uv)[0])
        try:support,lipschitz=surface_bound(*surfaces,uv)
        except Exception:support,lipschitz=math.inf,(math.inf,math.inf)
        wires=[shapes(face,TopAbs_WIRE) for face in (left,right)]
        if len(wires[0])!=len(wires[1]):diagnostics.append({"category":"wire_topology","index":i,"reason":"WIRE_COUNT_DIFFERS"});continue
        for j,(wa,wb) in enumerate(zip(*wires)):
            sequences=[]
            for side,wire in enumerate((wa,wb)):
                seq=[];explorer=TopExp_Explorer(wire,TopAbs_EDGE)
                while explorer.More():
                    edge=TopoDS.Edge(explorer.Current());seq.append((edge_maps[side].FindIndex(edge),edge.Orientation(),edge));explorer.Next()
                sequences.append(seq)
            def incidence(sequence,target_side):
                return Counter((index,int(orientation)^int(target_side and edge_reversed.get(index,False))) for index,orientation,_ in sequence)
            if incidence(sequences[0],False)!=incidence(sequences[1],True):
                diagnostics.append({"category":"wire_topology","index":[i,j],"reason":"ORIENTED_EDGE_INCIDENCE_DIFFERS"});continue
            left_edges=sorted(sequences[0],key=lambda x:(x[0],int(x[1])));right_edges=sorted(sequences[1],key=lambda x:(x[0],int(x[1])^int(edge_reversed.get(x[0],False))))
            for k,(ea,eb) in enumerate(zip(left_edges,right_edges)):
                def pcurve_bound():
                    target_range=BRep_Tool.Range_s(eb[2],right)
                    if edge_reversed.get(ea[0],False):target_range=target_range[::-1]
                    delta=curve_bound(BRep_Tool.CurveOnSurface_s(ea[2],left,0.,0.),BRep_Tool.CurveOnSurface_s(eb[2],right,0.,0.),
                        BRep_Tool.Range_s(ea[2],left),target_range)
                    return support if delta==0. else support+sum(lipschitz)*delta
                record("placed_2d_trim_traces",[i,j,k],pcurve_bound)
    return {"status":"PASS" if not diagnostics else "REVIEW","version":VERSION,"tolerance_mm":tolerance_mm,
        "method":"full_analytic_parameter_bounds_and_oriented_trim_incidence","counts":counts,"metrics":metrics,
        "maximum_complete_bound_mm":maximum,"diagnostic_counts":dict(Counter(row["reason"] for row in diagnostics)),
        "diagnostics":diagnostics,"scope":"Complete parameter bounds where supported. Bounds concern the proposed topology correspondence; a failed bound does not alone prove the best possible alignment differs. No sampled or feature-only certificate. Unsupported parameter reconciliation remains unverified."}
