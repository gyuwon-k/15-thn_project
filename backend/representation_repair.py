"""Conservative, name-independent BREP topology representation repair.

Repairs run only on deep copies. Acceptance requires exact placed 3D support
surfaces, curves, trimming ranges and vertex coordinates; every source solid;
all unique topology counts; source local tolerance budgets and independent
bounded distance samples. Invalid-source integrals remain unverified evidence.
No parts are removed, holes capped, or underlying 3D surfaces rebuilt.
"""
from __future__ import annotations
from collections import Counter
import hashlib
import io
import json
import math
import time
from fractions import Fraction
import OCP
from .exact_extrema import EXTREMA_POLICY
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy, BRepBuilderAPI_MakeVertex
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepLib import BRepLib
from OCP.BRepLib import BRepLib_ValidateEdge
from OCP.BRepTools import BRepTools
from OCP.GeomTools import GeomTools
from OCP.GeomLib import GeomLib_CheckCurveOnSurface
from OCP.Geom2dAdaptor import Geom2dAdaptor_Curve
from OCP.GeomAdaptor import GeomAdaptor_Surface
from OCP.Adaptor3d import Adaptor3d_CurveOnSurface
from OCP.GProp import GProp_GProps
from OCP.ShapeFix import ShapeFix_Shape, ShapeFix_Edge, ShapeFix_ShapeTolerance
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID, TopAbs_VERTEX, TopAbs_WIRE, TopAbs_IN, TopAbs_ON
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Pnt2d
try:
    from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as ShapeMap
except ImportError:
    from OCP.TopTools import TopTools_IndexedMapOfShape as ShapeMap

KINDS = {"solid": TopAbs_SOLID, "shell": TopAbs_SHELL, "face": TopAbs_FACE,
         "wire": TopAbs_WIRE, "edge": TopAbs_EDGE, "vertex": TopAbs_VERTEX}
STATUS_BINDING_ERROR = None
EXACT_CALIBRATION_POLICY = {
    "version": "measured-unchanged-cad-2.4.0",
    "strict_maximum_per_edge_increment_mm": 1e-4,
    "changed_edge_tolerance_basis": "original local edge tolerance plus independently measured bounded increment",
    "assembly_maximum_per_edge_increment_mm": 0.05,
    "assembly_maximum_increment_fraction_of_diagonal": 0.001,
    "assembly_minimum_increment_budget_mm": 1e-4,
    "geometry_movement_allowed": False,
    "native_extrema_method": "full_interval_with_independent_samples_and_bounded_complete_interval_retry",
    "extrema_policy": dict(EXTREMA_POLICY),
    "clearance_certification_requires_uncertainty_guard": True,
    "nominal_scope": "No detected volume interference at the recorded CAD representation precision; manufacturing clearance is not certified.",
}


def _shape_volume(shape):
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return float(abs(properties.Mass()))


def shape_bbox(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        return None
    low, high = box.CornerMin(), box.CornerMax()
    minimum, maximum = [low.X(), low.Y(), low.Z()], [high.X(), high.Y(), high.Z()]
    return {"min": minimum, "max": maximum,
            "size": [b - a for a, b in zip(minimum, maximum)],
            "center": [(a + b) / 2 for a, b in zip(minimum, maximum)]}

def shapes(shape, kind):
    index = ShapeMap()
    TopExp.MapShapes_s(shape, kind, index)
    return [index.FindKey(i) for i in range(1, index.Extent() + 1)]

def enum_name(value):
    return getattr(value, "name", str(value).split(".")[-1])

def status_names(values):
    return [enum_name(value) for value in values if enum_name(value) != "BRepCheck_NoError"]

def analyzer_statuses(check):
    global STATUS_BINDING_ERROR
    if STATUS_BINDING_ERROR:
        return [], []
    try:
        direct = status_names(check.Status())
        contexts = []
        check.InitContextIterator()
        while check.MoreShapeInContext():
            context = check.ContextualShape()
            statuses = status_names(check.StatusOnShape())
            if statuses:
                contexts.append({"shape_type": enum_name(context.ShapeType()), "statuses": statuses})
            check.NextShapeInContext()
        return direct, contexts
    except TypeError as exc:
        STATUS_BINDING_ERROR = str(exc)
        return [], []

def shape_area(shape):
    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, props)
    return float(abs(props.Mass()))

def geometry_coefficients(value):
    """Full-double defining coefficients; never rounded ostream text.

    GeomTools.Write on Python BytesIO uses the ostream's default six-digit
    precision. It is unsuitable for exact identity. Unsupported geometry
    fails closed rather than silently substituting rounded serialization.
    """
    if value is None:
        return None
    kind = value.DynamicType().Name()

    def coordinates(point):
        return [point.X(), point.Y(), point.Z()] if hasattr(point, "Z") else [point.X(), point.Y()]

    def frame(position):
        result = {"location": coordinates(position.Location())}
        for name in ("Direction", "XDirection", "YDirection"):
            if hasattr(position, name):
                result[name] = coordinates(getattr(position, name)())
        return result

    result = {"type": kind}
    if kind in ("Geom_BSplineCurve", "Geom2d_BSplineCurve", "Geom_BezierCurve", "Geom2d_BezierCurve"):
        result.update(degree=value.Degree(), rational=value.IsRational(),
                      poles=[coordinates(value.Pole(i)) for i in range(1, value.NbPoles() + 1)],
                      weights=[value.Weight(i) for i in range(1, value.NbPoles() + 1)])
        if "BSpline" in kind:
            result.update(periodic=value.IsPeriodic(),
                          knots=[value.Knot(i) for i in range(1, value.NbKnots() + 1)],
                          multiplicities=[value.Multiplicity(i) for i in range(1, value.NbKnots() + 1)])
    elif kind in ("Geom_BSplineSurface", "Geom_BezierSurface"):
        result.update(u_degree=value.UDegree(), v_degree=value.VDegree(),
                      u_rational=value.IsURational(), v_rational=value.IsVRational(),
                      poles=[[coordinates(value.Pole(i, j)) for j in range(1, value.NbVPoles() + 1)]
                             for i in range(1, value.NbUPoles() + 1)],
                      weights=[[value.Weight(i, j) for j in range(1, value.NbVPoles() + 1)]
                               for i in range(1, value.NbUPoles() + 1)])
        if "BSpline" in kind:
            result.update(u_periodic=value.IsUPeriodic(), v_periodic=value.IsVPeriodic(),
                          u_knots=[value.UKnot(i) for i in range(1, value.NbUKnots() + 1)],
                          v_knots=[value.VKnot(i) for i in range(1, value.NbVKnots() + 1)],
                          u_multiplicities=[value.UMultiplicity(i) for i in range(1, value.NbUKnots() + 1)],
                          v_multiplicities=[value.VMultiplicity(i) for i in range(1, value.NbVKnots() + 1)])
    elif kind in ("Geom_Line", "Geom2d_Line", "Geom_Circle", "Geom2d_Circle", "Geom_Ellipse", "Geom2d_Ellipse",
                  "Geom_Hyperbola", "Geom2d_Hyperbola", "Geom_Parabola", "Geom2d_Parabola", "Geom_Plane",
                  "Geom_CylindricalSurface", "Geom_ConicalSurface", "Geom_SphericalSurface", "Geom_ToroidalSurface"):
        result["position"] = frame(value.Position())
        for name in ("Radius", "MajorRadius", "MinorRadius", "Focal", "RefRadius", "SemiAngle"):
            if hasattr(value, name):
                result[name] = getattr(value, name)()
    elif kind in ("Geom_TrimmedCurve", "Geom2d_TrimmedCurve"):
        result.update(basis=geometry_coefficients(value.BasisCurve()),
                      interval=[value.FirstParameter(), value.LastParameter()])
    elif kind == "Geom_RectangularTrimmedSurface":
        result.update(basis=geometry_coefficients(value.BasisSurface()), bounds=value.Bounds())
    elif kind in ("Geom_OffsetCurve", "Geom2d_OffsetCurve", "Geom_OffsetSurface"):
        result.update(basis=geometry_coefficients(value.BasisSurface() if hasattr(value, "BasisSurface") else value.BasisCurve()),
                      offset=value.Offset())
        if hasattr(value, "Direction"):
            result["direction"] = coordinates(value.Direction())
    elif kind in ("Geom_SurfaceOfLinearExtrusion", "Geom_SurfaceOfRevolution"):
        result["basis"] = geometry_coefficients(value.BasisCurve())
        if hasattr(value, "Direction"):
            result["direction"] = coordinates(value.Direction())
        if hasattr(value, "Axis"):
            result["axis"] = frame(value.Axis())
    else:
        raise ValueError("Unsupported exact-identity geometry type: " + kind)
    return result

def geometry_hash(value):
    encoded = json.dumps(geometry_coefficients(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def support_geometry(shape):
    """Full-double defining coefficients of placed 3D surfaces and curves.

    The multiset detects support geometry replacement and material face loss;
    trimmed-domain preservation is additionally checked by per-face area/bbox.
    """
    surfaces = Counter(geometry_hash(BRep_Tool.Surface_s(TopoDS.Face(f)))
                       for f in shapes(shape, TopAbs_FACE))
    curves = Counter(geometry_hash(BRep_Tool.Curve_s(TopoDS.Edge(e), 0.0, 0.0))
                     for e in shapes(shape, TopAbs_EDGE))
    return {"surfaces": surfaces, "curves": curves}

def snapshot(shape):
    standard_valid = bool(BRepCheck_Analyzer(shape).IsValid())
    exact_valid = bool(BRepCheck_Analyzer(shape, True, False, True).IsValid())
    return {"valid": standard_valid,
            "validity_acceptance_method": "BRepCheck_Analyzer standard method (pipeline criterion)",
            "exact_method_valid": exact_valid,
            "standard_exact_method_disagreement": standard_valid != exact_valid,
            "counts": {name: len(shapes(shape, kind)) for name, kind in KINDS.items()},
            "bbox": shape_bbox(shape), "unverified_volume_mm3": _shape_volume(shape),
            "area_mm2": shape_area(shape), "max_tolerance_mm": max_tolerances(shape)}

def max_tolerances(shape):
    return {name: max((float(BRep_Tool.Tolerance_s(cast(e))) for e in shapes(shape, kind)), default=0.0)
            for name, kind, cast in (("edge", TopAbs_EDGE, TopoDS.Edge),
                                      ("vertex", TopAbs_VERTEX, TopoDS.Vertex),
                                      ("face", TopAbs_FACE, TopoDS.Face))}

def per_entity_tolerance_hash(shape):
    """Identity of every tolerance, including duplicate coincident entities."""
    entries = []
    for kind, cast in (("edge", TopoDS.Edge), ("vertex", TopoDS.Vertex), ("face", TopoDS.Face)):
        for entity in shapes(shape, KINDS[kind]):
            key = edge_key(entity) if kind == "edge" else point_key(entity) if kind == "vertex" else next(iter(boundary_manifest(entity)))
            entries.append((kind, key, float(BRep_Tool.Tolerance_s(cast(entity)))))
    encoded = json.dumps(sorted(entries), separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

def boundary_manifest(shape):
    """Exact placed support surface plus each face's actual 3D edge trims.

    A surface/curve multiset alone could miss changing a trimming range. This
    nested face/wire manifest preserves per-face wire membership, 3D curve
    ranges and exact vertex positions while allowing orientation correction.
    """
    entries = []
    for face in shapes(shape, TopAbs_FACE):
        wires = []
        for wire in shapes(face, TopAbs_WIRE):
            edges = []
            iterator = TopExp_Explorer(wire, TopAbs_EDGE)
            while iterator.More():
                edge = TopoDS.Edge(iterator.Current())
                vertices = []
                for vertex in shapes(edge, TopAbs_VERTEX):
                    point = BRep_Tool.Pnt_s(TopoDS.Vertex(vertex))
                    vertices.append((point.X(), point.Y(), point.Z()))
                edges.append((geometry_hash(BRep_Tool.Curve_s(edge, 0.0, 0.0)),
                              BRep_Tool.Range_s(edge), tuple(sorted(vertices)),
                              bool(BRep_Tool.Degenerated_s(edge))))
                iterator.Next()
            wires.append(tuple(sorted(edges)))
        entries.append((geometry_hash(BRep_Tool.Surface_s(TopoDS.Face(face))), tuple(sorted(wires))))
    return Counter(entries)

def pcurve_manifest(shape):
    """Exact 2D pcurve coefficients, ranges and nested face/wire membership."""
    entries = []
    for entity in shapes(shape, TopAbs_FACE):
        face = TopoDS.Face(entity)
        wires = []
        for wire in shapes(face, TopAbs_WIRE):
            edges = []
            iterator = TopExp_Explorer(wire, TopAbs_EDGE)
            while iterator.More():
                edge = TopoDS.Edge(iterator.Current())
                pcurve = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
                edges.append((edge_key(edge), geometry_hash(pcurve), BRep_Tool.Range_s(edge, face)))
                iterator.Next()
            wires.append(tuple(sorted(edges)))
        entries.append((geometry_hash(BRep_Tool.Surface_s(face)), tuple(sorted(wires))))
    return Counter(entries)

def affine_pcurve_certificate(source, candidate, trim_range, surface):
    """Exact rational polynomial comparison of two degree-one pcurves.

    IEEE doubles are converted to exact rationals. Each curve is affine on
    each knot span; the maximum coordinate error over a trim interval is
    therefore attained at an endpoint or knot. This is a complete bound,
    independent of finite spatial sampling.
    """
    def eligible(curve):
        return (curve.DynamicType().Name() == "Geom2d_BSplineCurve" and curve.Degree() == 1
                and not curve.IsRational() and not curve.IsPeriodic()
                and curve.NbPoles() == curve.NbKnots()
                and curve.Multiplicity(1) == curve.Multiplicity(curve.NbKnots()) == 2
                and all(curve.Multiplicity(i) == 1 for i in range(2, curve.NbKnots())))
    if not eligible(source) or not eligible(candidate) or source.NbPoles() != 2:
        return {"accepted": False, "reason": "UNSUPPORTED_AFFINE_PCURVE_FORM"}
    kind = surface.DynamicType().Name()
    if kind not in ("Geom_Plane", "Geom_CylindricalSurface", "Geom_ConicalSurface"):
        return {"accepted": False, "reason": "UNSUPPORTED_ANALYTIC_SURFACE_METRIC"}
    def data(curve):
        knots = [Fraction(curve.Knot(i)) for i in range(1, curve.NbKnots() + 1)]
        poles = [[Fraction(curve.Pole(i).X()), Fraction(curve.Pole(i).Y())] for i in range(1, curve.NbPoles() + 1)]
        return knots, poles
    sk, sp = data(source)
    ck, cp = data(candidate)
    first, last = map(Fraction, trim_range)
    if not ck[0] <= first <= last <= ck[-1]:
        return {"accepted": False, "reason": "CANDIDATE_NATURAL_DOMAIN_DOES_NOT_COVER_ORIGINAL_TRIM"}
    def point(knots, poles, parameter):
        interval = max(0, min(len(knots) - 2, next((i - 1 for i, knot in enumerate(knots) if knot >= parameter), len(knots) - 2)))
        ratio = (parameter - knots[interval]) / (knots[interval + 1] - knots[interval])
        return [poles[interval][axis] + ratio * (poles[interval + 1][axis] - poles[interval][axis]) for axis in (0, 1)]
    checkpoints = sorted({first, last, *(knot for knot in ck if first <= knot <= last),
                          *(knot for knot in sk if first <= knot <= last)})
    errors = [Fraction(0), Fraction(0)]
    for parameter in checkpoints:
        source_point, candidate_point = point(sk, sp, parameter), point(ck, cp, parameter)
        errors = [max(errors[axis], abs(source_point[axis] - candidate_point[axis])) for axis in (0, 1)]
    uv_bounds = [math.nextafter(float(value), math.inf) if value else 0.0 for value in errors]
    position = surface.Position()
    def length_bound(direction):
        return math.nextafter(abs(direction.X()) + abs(direction.Y()) + abs(direction.Z()), math.inf)
    x, y = length_bound(position.XDirection()), length_bound(position.YDirection())
    metric = [x, y]
    if kind == "Geom_CylindricalSurface":
        metric = [math.nextafter((x + y) * abs(float(surface.Radius())), math.inf), length_bound(position.Direction())]
    elif kind == "Geom_ConicalSurface":
        maximum_v = max(abs(point(knots, poles, parameter)[1]) for knots, poles in ((sk, sp), (ck, cp)) for parameter in checkpoints)
        v_bound = math.nextafter(float(maximum_v), math.inf)
        sine, cosine = math.nextafter(abs(math.sin(surface.SemiAngle())), math.inf), math.nextafter(abs(math.cos(surface.SemiAngle())), math.inf)
        radius_bound = math.nextafter(abs(surface.RefRadius()) + math.nextafter(v_bound * sine, math.inf), math.inf)
        metric = [math.nextafter(math.nextafter(x + y, math.inf) * radius_bound, math.inf),
                  math.nextafter(math.nextafter(math.nextafter(x + y, math.inf) * sine, math.inf)
                                 + math.nextafter(length_bound(position.Direction()) * cosine, math.inf), math.inf)]
    combined = sum(uv_bounds[axis] * metric[axis] for axis in (0, 1))
    spatial_bound = math.nextafter(combined, math.inf) if combined else 0.0
    return {"accepted": spatial_bound <= 1e-12, "proof": "Exact-rational piecewise affine polynomial sup bound on the unchanged original trim interval",
            "source_coefficients": geometry_coefficients(source), "candidate_coefficients": geometry_coefficients(candidate),
            "original_trim_range": list(trim_range), "rational_checkpoint_count": len(checkpoints),
            "coordinate_error_bounds_uv": uv_bounds, "rational_coordinate_error_bounds": [str(value) for value in errors],
            "analytic_surface_metric_bounds": metric, "spatial_roundoff_error_bound_mm": spatial_bound,
            "maximum_allowed_spatial_roundoff_error_mm": 1e-12,
            "same_original_trim_interval": True, "source_extrapolated_affine_trace": True}

def extend_affine_pcurve_domain(source, trim_range, surface):
    """Extend an original linear B-spline without changing its trimmed trace."""
    if (source.DynamicType().Name() != "Geom2d_BSplineCurve" or source.Degree() != 1
            or source.IsRational() or source.IsPeriodic() or source.NbPoles() != 2 or source.NbKnots() != 2
            or source.Multiplicity(1) != 2 or source.Multiplicity(2) != 2):
        return None, {"accepted": False, "reason": "UNSUPPORTED_SOURCE_LINEAR_PCURVE"}
    first, last = trim_range
    low, high = source.Knot(1), source.Knot(2)
    if low <= first <= last <= high:
        return None, {"accepted": False, "reason": "ORIGINAL_TRIM_ALREADY_INSIDE_NATURAL_DOMAIN"}
    from OCP.Geom2d import Geom2d_BSplineCurve
    try:
        from OCP.collections import Array1_gp_Pnt2d as PointArray, Array1_double as RealArray, Array1_int as IntArray
    except ImportError:
        from OCP.TColgp import TColgp_Array1OfPnt2d as PointArray
        from OCP.TColStd import TColStd_Array1OfReal as RealArray, TColStd_Array1OfInteger as IntArray
    a, b = source.Pole(1), source.Pole(2)
    source_coordinates = [[Fraction(a.X()), Fraction(a.Y())], [Fraction(b.X()), Fraction(b.Y())]]
    delta = [source_coordinates[1][i] - source_coordinates[0][i] for i in (0, 1)]
    source_knots = [Fraction(low), Fraction(high)]
    span = source_knots[1] - source_knots[0]
    endpoints, integer_spans = [], []
    for side in (0, 1):
        direction = -1 if side == 0 else 1
        endpoint = None
        for count in range(1, 257):
            values = [source_knots[side] + direction * count * span,
                      *(source_coordinates[side][axis] + direction * count * delta[axis] for axis in (0, 1))]
            floats = [float(value) for value in values]
            if (all(math.isfinite(value) and Fraction(value) == exact for value, exact in zip(floats, values))
                    and (floats[0] <= first if side == 0 else floats[0] >= last)):
                endpoint = floats
                integer_spans.append(count)
                break
        if endpoint is None:
            return None, {"accepted": False, "reason": "NO_EXACTLY_REPRESENTABLE_AFFINE_EXTENSION_WITHIN_256_SPANS"}
        endpoints.append(endpoint)
    poles, knots, multiplicities = PointArray(1, 2), RealArray(1, 2), IntArray(1, 2)
    for index, endpoint in enumerate(endpoints, 1):
        knot, point = endpoint[0], endpoint[1:]
        poles.SetValue(index, gp_Pnt2d(*point))
        knots.SetValue(index, knot)
        multiplicities.SetValue(index, 2)
    candidate = Geom2d_BSplineCurve(poles, knots, multiplicities, 1, False)
    certificate = affine_pcurve_certificate(source, candidate, trim_range, surface)
    certificate.update(extrapolated_integer_spans=integer_spans, all_defining_values_exactly_representable=True)
    certificate["accepted"] = certificate["accepted"] and certificate["spatial_roundoff_error_bound_mm"] == 0.0
    return (candidate if certificate["accepted"] else None), certificate

def pcurve_preservation_audit(original, candidate):
    exact = pcurve_manifest(original) == pcurve_manifest(candidate)
    if exact:
        return {"accepted": True, "full_double_coefficients_and_ranges_exactly_preserved": True, "affine_certificates": []}
    source_faces, next_faces = {}, {}
    for shape, groups in ((original, source_faces), (candidate, next_faces)):
        for index, face in enumerate(shapes(shape, TopAbs_FACE), 1):
            key = next(iter(boundary_manifest(face)))
            groups.setdefault(key, []).append((index, face))
    if source_faces.keys() != next_faces.keys():
        return {"accepted": False, "reason": "FACE_3D_BOUNDARIES_CHANGED"}
    certificates = []
    for key, sources in source_faces.items():
        counterparts = list(next_faces[key])
        for source_index, source_face in sources:
            source_pc = pcurve_manifest(source_face)
            identical = next((i for i, (_, face) in enumerate(counterparts) if pcurve_manifest(face) == source_pc), None)
            if identical is not None:
                counterparts.pop(identical)
                continue
            if len(sources) != 1 or len(counterparts) != 1:
                return {"accepted": False, "reason": "AMBIGUOUS_CHANGED_COINCIDENT_FACE"}
            candidate_index, candidate_face = counterparts.pop()
            def entries(face):
                output = {}
                iterator = TopExp_Explorer(face, TopAbs_EDGE)
                while iterator.More():
                    edge = TopoDS.Edge(iterator.Current())
                    edge_identity = (edge_key(edge), enum_name(edge.Orientation()))
                    if edge_identity in output:
                        raise ValueError("Ambiguous duplicate oriented face edge")
                    output[edge_identity] = edge
                    iterator.Next()
                return output
            source_edges, candidate_edges = entries(source_face), entries(candidate_face)
            if source_edges.keys() != candidate_edges.keys():
                return {"accepted": False, "reason": "ORIENTED_FACE_EDGE_MEMBERSHIP_CHANGED"}
            for edge_identity, source_edge in source_edges.items():
                source_face, candidate_face = TopoDS.Face(source_face), TopoDS.Face(candidate_face)
                candidate_edge = candidate_edges[edge_identity]
                source_range = BRep_Tool.Range_s(source_edge, source_face)
                next_range = BRep_Tool.Range_s(candidate_edge, candidate_face)
                if source_range != next_range:
                    return {"accepted": False, "reason": "STORED_2D_TRIM_RANGE_CHANGED"}
                source_curve = BRep_Tool.CurveOnSurface_s(source_edge, source_face, 0.0, 0.0)
                next_curve = BRep_Tool.CurveOnSurface_s(candidate_edge, candidate_face, 0.0, 0.0)
                if geometry_hash(source_curve) == geometry_hash(next_curve):
                    continue
                certificate = affine_pcurve_certificate(source_curve, next_curve, source_range, BRep_Tool.Surface_s(source_face))
                certificate.update(source_face_index=source_index, candidate_face_index=candidate_index)
                certificates.append(certificate)
    return {"accepted": bool(certificates) and all(value["accepted"] and value["spatial_roundoff_error_bound_mm"] == 0.0 for value in certificates),
            "full_double_coefficients_and_ranges_exactly_preserved": False, "affine_certificates": certificates,
            "basis": "Each changed original 2D trim has an exact-rational identical affine function on its unchanged stored trim interval; natural-domain extension adds no material surface region."}

def tolerance_budget_audit(original, candidate, maximum=1e-4):
    def manifest(shape, kind):
        output = {}
        for entity in shapes(shape, KINDS[kind]):
            if kind == "edge":
                edge = TopoDS.Edge(entity)
                key = (geometry_hash(BRep_Tool.Curve_s(edge, 0.0, 0.0)), BRep_Tool.Range_s(edge))
                tolerance = float(BRep_Tool.Tolerance_s(edge))
            elif kind == "vertex":
                point = BRep_Tool.Pnt_s(TopoDS.Vertex(entity))
                key = (point.X(), point.Y(), point.Z())
                tolerance = float(BRep_Tool.Tolerance_s(TopoDS.Vertex(entity)))
            else:
                key = geometry_hash(BRep_Tool.Surface_s(TopoDS.Face(entity)))
                tolerance = float(BRep_Tool.Tolerance_s(TopoDS.Face(entity)))
            output.setdefault(key, []).append(tolerance)
        return {key: sorted(values) for key, values in output.items()}
    checks = {}
    for kind in ("edge", "vertex", "face"):
        before, after = manifest(original, kind), manifest(candidate, kind)
        matching = before.keys() == after.keys() and all(len(before[k]) == len(after[k]) for k in before)
        increases = [b - a for k in before for a, b in zip(before[k], after[k])] if matching else []
        checks[kind] = {"geometry_key_and_entity_counts_match": matching,
                       "max_tolerance_increase_mm": max(increases, default=None),
                       "within_per_entity_budget": matching and all(b <= max(a, maximum) + 1e-12
                           for k in before for a, b in zip(before[k], after[k]))}
    return {"new_tolerance_budget_mm": maximum, "existing_source_tolerances_above_budget_retained": True,
            "per_kind": checks, "accepted": all(row["within_per_entity_budget"] for row in checks.values())}

def point_key(vertex):
    p = BRep_Tool.Pnt_s(TopoDS.Vertex(vertex))
    return p.X(), p.Y(), p.Z()

def edge_key(edge):
    return (geometry_hash(BRep_Tool.Curve_s(TopoDS.Edge(edge), 0.0, 0.0)),
            BRep_Tool.Range_s(TopoDS.Edge(edge)),
            tuple(sorted(point_key(v) for v in shapes(edge, TopAbs_VERTEX))))

def original_local_tolerance_budgets(original, minimum_budget=1e-4):
    # Compute each original entity's incident budget before reducing by exact
    # geometry. Coincident but topologically separate entities must not lend
    # each other a larger tolerance; use the smallest duplicate-key budget.
    budgets = {"edge": {}, "vertex": {}, "face": {}}
    edge_map, vertex_map = ShapeMap(), ShapeMap()
    TopExp.MapShapes_s(original, TopAbs_EDGE, edge_map)
    TopExp.MapShapes_s(original, TopAbs_VERTEX, vertex_map)
    edge_budgets, vertex_budgets = {}, {}
    for index in range(1, vertex_map.Extent() + 1):
        vertex = vertex_map.FindKey(index)
        vertex_budgets[index] = max(minimum_budget, float(BRep_Tool.Tolerance_s(TopoDS.Vertex(vertex))))
    for index in range(1, edge_map.Extent() + 1):
        edge = edge_map.FindKey(index)
        edge_budgets[index] = max([minimum_budget, float(BRep_Tool.Tolerance_s(TopoDS.Edge(edge))),
                                  *(float(BRep_Tool.Tolerance_s(TopoDS.Vertex(v))) for v in shapes(edge, TopAbs_VERTEX))])
    for face in shapes(original, TopAbs_FACE):
        face_tolerance = float(BRep_Tool.Tolerance_s(TopoDS.Face(face)))
        face_key = next(iter(boundary_manifest(face)))
        value = max(minimum_budget, face_tolerance)
        budgets["face"][face_key] = min(budgets["face"].get(face_key, value), value)
        for edge in shapes(face, TopAbs_EDGE):
            own = float(BRep_Tool.Tolerance_s(TopoDS.Edge(edge)))
            vertices = shapes(edge, TopAbs_VERTEX)
            edge_index = edge_map.FindIndex(edge)
            edge_budgets[edge_index] = max(edge_budgets[edge_index], face_tolerance)
            for vertex in vertices:
                vertex_index = vertex_map.FindIndex(vertex)
                vertex_budgets[vertex_index] = max(vertex_budgets[vertex_index], own, face_tolerance)
    for kind, index_map, values, key_function in (("edge", edge_map, edge_budgets, edge_key),
                                                ("vertex", vertex_map, vertex_budgets, point_key)):
        for index in range(1, index_map.Extent() + 1):
            key, value = key_function(index_map.FindKey(index)), values[index]
            budgets[kind][key] = min(budgets[kind].get(key, value), value)
    return budgets

def local_tolerance_budget_audit(original, candidate, minimum_budget=1e-4):
    """Reuse only original uncertainty directly incident to each entity.

    Edge budget: its original tolerance, original endpoint vertex tolerances,
    and original incident face tolerances. Vertex budget: its own original
    tolerance plus original incident edge/face tolerances. No shape-wide max.
    """
    budgets = original_local_tolerance_budgets(original, minimum_budget)
    violations, maxima = [], {}
    for kind in ("edge", "vertex", "face"):
        largest_ratio = 0.0
        for index, entity in enumerate(shapes(candidate, KINDS[kind]), 1):
            cast = {"edge": TopoDS.Edge, "vertex": TopoDS.Vertex, "face": TopoDS.Face}[kind]
            tolerance = float(BRep_Tool.Tolerance_s(cast(entity)))
            key = edge_key(entity) if kind == "edge" else point_key(entity) if kind == "vertex" else next(iter(boundary_manifest(entity)))
            budget = budgets[kind].get(key)
            if budget is None or tolerance > budget + 1e-12:
                violations.append({"kind": kind, "index": index, "candidate_tolerance_mm": tolerance,
                                   "original_incident_tolerance_budget_mm": budget})
            if budget:
                largest_ratio = max(largest_ratio, tolerance / budget)
        maxima[kind] = largest_ratio
    return {"minimum_new_tolerance_budget_mm": minimum_budget,
            "method": "Original directly incident vertex/edge/face tolerances; no unrelated global maximum.",
            "violations": violations, "max_tolerance_to_local_budget_ratio": maxima,
            "accepted": not violations}

def clamp_to_original_local_budgets(original, candidate, minimum_budget=1e-4):
    budgets = original_local_tolerance_budgets(original, minimum_budget)
    tolerance_tool = ShapeFix_ShapeTolerance()
    changed = Counter()
    for kind in ("edge", "vertex", "face"):
        for entity in shapes(candidate, KINDS[kind]):
            cast = {"edge": TopoDS.Edge, "vertex": TopoDS.Vertex, "face": TopoDS.Face}[kind]
            tolerance = float(BRep_Tool.Tolerance_s(cast(entity)))
            key = edge_key(entity) if kind == "edge" else point_key(entity) if kind == "vertex" else next(iter(boundary_manifest(entity)))
            budget = budgets[kind].get(key)
            if budget is None:
                raise ValueError("Unmatched geometry during local tolerance clamp")
            if tolerance > budget:
                tolerance_tool.SetTolerance(entity, budget, KINDS[kind])
                changed[kind] += 1
    return candidate, {"clamped_entity_counts": dict(changed),
                       "original_incident_local_budget_minimum_mm": minimum_budget,
                       "analyzer_valid_after_clamp": bool(BRepCheck_Analyzer(candidate).IsValid())}

def face_local_budget(face, minimum=1e-4):
    return max([minimum, float(BRep_Tool.Tolerance_s(TopoDS.Face(face))),
                *(float(BRep_Tool.Tolerance_s(TopoDS.Edge(e))) for e in shapes(face, TopAbs_EDGE)),
                *(float(BRep_Tool.Tolerance_s(TopoDS.Vertex(v))) for v in shapes(face, TopAbs_VERTEX))])

def edge_local_budget(edge, minimum=1e-4):
    return max([minimum, float(BRep_Tool.Tolerance_s(TopoDS.Edge(edge))),
                *(float(BRep_Tool.Tolerance_s(TopoDS.Vertex(v))) for v in shapes(edge, TopAbs_VERTEX))])

def measure_source_edge_residual(edge, incident_faces):
    """Measure stored source contradictions in one explicitly placed frame."""
    edge = TopoDS.Edge(edge)
    curve = BRepAdaptor_Curve(edge)
    low, high = curve.FirstParameter(), curve.LastParameter()
    endpoint_gaps = []
    for vertex, parameter in ((TopExp.FirstVertex_s(edge), low), (TopExp.LastVertex_s(edge), high)):
        if vertex.IsNull():
            raise ValueError("Missing source edge endpoint vertex")
        endpoint_gaps.append(float(BRep_Tool.Pnt_s(vertex).Distance(curve.Value(parameter))))
    rows = []
    for face_index, face, oriented_edge in incident_faces:
        face = TopoDS.Face(face)
        oriented_edge = TopoDS.Edge(oriented_edge)
        first, last = BRep_Tool.Range_s(oriented_edge, face)
        pcurve = BRep_Tool.CurveOnSurface_s(oriented_edge, face, 0.0, 0.0)
        if pcurve is None:
            raise ValueError("Missing original source pcurve")
        # CurveOnSurface() extracted from BRepAdaptor strips its transform.
        # Rebuild from stored 2D curve and the explicitly placed 3D surface.
        placed_surface = BRep_Tool.Surface_s(face)
        on_surface = Adaptor3d_CurveOnSurface(Geom2dAdaptor_Curve(pcurve, first, last),
                                             GeomAdaptor_Surface(placed_surface))
        checker = GeomLib_CheckCurveOnSurface(curve)
        checker.Perform(on_surface)
        if not checker.IsDone():
            raise ValueError("Original curve-surface residual calculation failed")
        residual = float(checker.MaxDistance())
        sampled = max(curve.Value(low + (high - low) * j / 256).Distance(
                      on_surface.Value(low + (high - low) * j / 256)) for j in range(257))
        native_22_max = max(curve.Value(low * (1 - j / 22) + high * j / 22).Distance(
                            on_surface.Value(low * (1 - j / 22) + high * j / 22)) for j in range(23))
        if not math.isfinite(residual) or residual + 1e-9 < sampled:
            raise ValueError("Original source residual maximum failed independent sampling sanity check")
        pcurve_endpoint_gaps = []
        for vertex, parameter in ((TopExp.FirstVertex_s(edge), low), (TopExp.LastVertex_s(edge), high)):
            pcurve_endpoint_gaps.append(float(BRep_Tool.Pnt_s(vertex).Distance(on_surface.Value(parameter))))
        rows.append({"source_face_index": face_index, "edge_orientation": enum_name(oriented_edge.Orientation()),
                     "maximum_curve_to_placed_pcurve_surface_residual_mm": residual,
                     "independent_257_point_residual_lower_bound_mm": float(sampled),
                     "native_22_segment_residual_max_mm": float(native_22_max),
                     "native_ComputeTol_1_5_safety_envelope_mm": max(1e-7, 1.5 * float(native_22_max)),
                     "maximum_parameter": float(checker.MaxParameter()),
                     "source_pcurve_endpoint_to_vertex_gaps_mm": pcurve_endpoint_gaps})
    maximum = max([*endpoint_gaps,
                   *(row["maximum_curve_to_placed_pcurve_surface_residual_mm"] for row in rows),
                   *(gap for row in rows for gap in row["source_pcurve_endpoint_to_vertex_gaps_mm"])])
    return {"maximum_existing_source_residual_mm": maximum, "source_3d_curve_endpoint_to_vertex_gaps_mm": endpoint_gaps,
            "source_curve_surface_checks": rows, "success": True}

def _incident_edge_index(shape):
    index = ShapeMap()
    TopExp.MapShapes_s(shape, TopAbs_EDGE, index)
    incident = {i: [] for i in range(1, index.Extent() + 1)}
    for face_index, face in enumerate(shapes(shape, TopAbs_FACE), 1):
        iterator = TopExp_Explorer(face, TopAbs_EDGE)
        while iterator.More():
            edge = iterator.Current()
            incident[index.FindIndex(edge)].append((face_index, face, edge))
            iterator.Next()
    return index, incident

def source_residual_metadata_repair(shape):
    """Retain every original curve; repair only tolerance/parameter metadata.

    The documented OCCT ComputeTol 1.5 margin is applied to independently
    measured original maxima; its coarser 22-segment result is also recorded.
    Reparameterization is
    followed by restoring COPIES of every original pcurve and face range.
    Acceptance separately verifies all full-double 3D/2D manifests and gaps.
    """
    from OCP.BRep import BRep_Builder
    candidate = copied(shape)
    source_support, source_trim, source_pc = support_geometry(shape), boundary_manifest(shape), pcurve_manifest(shape)
    source_tol = max_tolerances(shape)
    source_entity_tolerance_hash = per_entity_tolerance_hash(shape)
    source_index, incident = _incident_edge_index(shape)
    candidate_edges = shapes(candidate, TopAbs_EDGE)
    analyzer = BRepCheck_Analyzer(shape)
    targets = [i for i, rows in incident.items() if any(not analyzer.IsValid(face) for _, face, _ in rows)]
    builder = BRep_Builder()
    measurements, changes = {}, []
    for index in targets:
        source_edge = TopoDS.Edge(source_index.FindKey(index))
        edge = TopoDS.Edge(candidate_edges[index - 1])
        if edge_key(source_edge) != edge_key(edge):
            raise ValueError("Deep copy changed source edge order or geometry")
        if BRep_Tool.Degenerated_s(edge):
            continue
        measurement = measure_source_edge_residual(source_edge, incident[index])
        measurements[index] = measurement
        envelope = max([1e-7, 1.5 * measurement["maximum_existing_source_residual_mm"],
                        *measurement["source_3d_curve_endpoint_to_vertex_gaps_mm"],
                        *(row["native_ComputeTol_1_5_safety_envelope_mm"] for row in measurement["source_curve_surface_checks"])])
        if measurement["maximum_existing_source_residual_mm"] > envelope + 1e-9:
            raise ValueError("Independent source residual exceeds the documented native envelope")
        requested = max(float(BRep_Tool.Tolerance_s(source_edge)), envelope)
        if requested > min(source_tol["edge"], source_tol["vertex"]) + 1e-12:
            raise ValueError("Source native residual envelope exceeds an original global tolerance cap")
        builder.UpdateEdge(edge, requested)
        for vertex in shapes(edge, TopAbs_VERTEX):
            builder.UpdateVertex(TopoDS.Vertex(vertex), requested)
        changes.append({"source_edge_index": index, "source_edge_tolerance_mm": float(BRep_Tool.Tolerance_s(source_edge)),
                        "source_maximum_actual_residual_mm": measurement["maximum_existing_source_residual_mm"],
                        "documented_source_safety_envelope_mm": envelope, "requested_edge_and_incident_vertex_tolerance_mm": requested})
    remaining = BRepCheck_Analyzer(candidate)
    selected = ShapeMap()
    for face in shapes(candidate, TopAbs_FACE):
        if not remaining.IsValid(face):
            for edge in shapes(face, TopAbs_EDGE):
                selected.Add(edge)
    selected_indices = []
    for i in range(1, selected.Extent() + 1):
        edge = selected.FindKey(i)
        selected_indices.append(next(j for j, value in enumerate(candidate_edges, 1) if value.IsSame(edge)))
        BRepLib.SameParameter_s(edge, 1e-7, True)
    candidate_faces = shapes(candidate, TopAbs_FACE)
    restored = []
    for index in selected_indices:
        edge = TopoDS.Edge(candidate_edges[index - 1])
        for face_index, source_face, source_oriented_edge in incident[index]:
            source_face = TopoDS.Face(source_face)
            candidate_face = TopoDS.Face(candidate_faces[face_index - 1])
            if BRep_Tool.IsClosed_s(edge, candidate_face):
                raise ValueError("Restoring periodic seam pcurves is unsupported; candidate rejected")
            pcurve = BRep_Tool.CurveOnSurface_s(TopoDS.Edge(source_oriented_edge), source_face, 0.0, 0.0)
            first, last = BRep_Tool.Range_s(TopoDS.Edge(source_oriented_edge), source_face)
            current = BRep_Tool.CurveOnSurface_s(edge, candidate_face, 0.0, 0.0)
            if (geometry_hash(current) == geometry_hash(pcurve)
                    and BRep_Tool.Range_s(edge, candidate_face) == (first, last)):
                continue
            flags = bool(BRep_Tool.SameParameter_s(edge)), bool(BRep_Tool.SameRange_s(edge))
            builder.UpdateEdge(edge, pcurve.Copy(), candidate_face, BRep_Tool.Tolerance_s(edge))
            builder.Range(edge, candidate_face, first, last)
            builder.SameParameter(edge, flags[0])
            builder.SameRange(edge, flags[1])
            restored.append({"source_edge_index": index, "source_face_index": face_index})
    affine_extensions = []
    remaining = BRepCheck_Analyzer(candidate)
    for face_index, face in enumerate(candidate_faces, 1):
        if remaining.IsValid(face):
            continue
        face = TopoDS.Face(face)
        for entity in shapes(face, TopAbs_EDGE):
            edge = TopoDS.Edge(entity)
            source_pc = BRep_Tool.CurveOnSurface_s(edge, face, 0.0, 0.0)
            interval = BRep_Tool.Range_s(edge, face)
            extension, certificate = extend_affine_pcurve_domain(source_pc, interval, BRep_Tool.Surface_s(face))
            if extension is None:
                continue
            flags = bool(BRep_Tool.SameParameter_s(edge)), bool(BRep_Tool.SameRange_s(edge))
            builder.UpdateEdge(edge, extension, face, BRep_Tool.Tolerance_s(edge))
            builder.Range(edge, face, *interval)
            builder.SameParameter(edge, flags[0])
            builder.SameRange(edge, flags[1])
            edge_index = next(i for i, value in enumerate(candidate_edges, 1) if value.IsSame(edge))
            affine_extensions.append({"source_edge_index": edge_index, "candidate_face_index": face_index, **certificate})
    next_index, next_incident = _incident_edge_index(candidate)
    affine_residual_checks = []
    for edge_index in sorted({value["source_edge_index"] for value in affine_extensions}):
        source = measurements.get(edge_index) or measure_source_edge_residual(source_index.FindKey(edge_index), incident[edge_index])
        after = measure_source_edge_residual(next_index.FindKey(edge_index), next_incident[edge_index])
        affine_residual_checks.append({"source_edge_index": edge_index, "source": source, "candidate": after,
            "source_actual_maximum_residual_mm": source["maximum_existing_source_residual_mm"],
            "candidate_actual_maximum_residual_mm": after["maximum_existing_source_residual_mm"],
            "numeric_epsilon_mm": 1e-9,
            "actual_residual_not_increased": after["maximum_existing_source_residual_mm"] <= source["maximum_existing_source_residual_mm"] + 1e-9})
    if not all(value["actual_residual_not_increased"] for value in affine_residual_checks):
        raise ValueError("Candidate affine-domain representation increased an original geometric residual")
    actual_changes = []
    for index, entity in enumerate(candidate_edges, 1):
        source_edge = TopoDS.Edge(source_index.FindKey(index))
        prior, final = float(BRep_Tool.Tolerance_s(source_edge)), float(BRep_Tool.Tolerance_s(TopoDS.Edge(entity)))
        if prior != final:
            actual_changes.append({"kind": "edge", "source_entity_index": index, "candidate_entity_index": index,
                                   "source_tolerance_mm": prior, "final_candidate_tolerance_mm": final,
                                   "incident_source_edge_indices": [index]})
    source_vertices, candidate_vertices = shapes(shape, TopAbs_VERTEX), shapes(candidate, TopAbs_VERTEX)
    source_vertex_index = ShapeMap()
    TopExp.MapShapes_s(shape, TopAbs_VERTEX, source_vertex_index)
    adjacent_by_vertex = {i: [] for i in range(1, source_vertex_index.Extent() + 1)}
    for edge_index in range(1, source_index.Extent() + 1):
        for vertex in shapes(source_index.FindKey(edge_index), TopAbs_VERTEX):
            adjacent_by_vertex[source_vertex_index.FindIndex(vertex)].append(edge_index)
    for index, (source_vertex, vertex) in enumerate(zip(source_vertices, candidate_vertices), 1):
        if point_key(source_vertex) != point_key(vertex):
            raise ValueError("Deep copy changed source vertex order or coordinates")
        prior, final = float(BRep_Tool.Tolerance_s(TopoDS.Vertex(source_vertex))), float(BRep_Tool.Tolerance_s(TopoDS.Vertex(vertex)))
        if prior != final:
            actual_changes.append({"kind": "vertex", "source_entity_index": index, "candidate_entity_index": index,
                                   "source_tolerance_mm": prior, "final_candidate_tolerance_mm": final,
                                   "incident_source_edge_indices": adjacent_by_vertex[index]})
    after_entity_tolerance_hash = per_entity_tolerance_hash(shape)
    source_unchanged = (source_support == support_geometry(shape) and source_trim == boundary_manifest(shape)
                        and source_pc == pcurve_manifest(shape) and source_tol == max_tolerances(shape)
                        and source_entity_tolerance_hash == after_entity_tolerance_hash)
    return candidate, {"source_native_tolerance_envelopes": changes,
                       "source_residual_measurements": [{"source_edge_index": i, **value} for i, value in measurements.items()],
                       "remaining_invalid_face_edge_parameter_metadata_indices": selected_indices,
                       "original_pcurves_restored_from_independent_copies": restored,
                       "proved_original_trim_affine_domain_extensions": affine_extensions,
                       "direct_source_and_candidate_affine_edge_residual_checks": affine_residual_checks,
                       "source_full_double_geometry_and_tolerances_unchanged": source_unchanged,
                       "source_per_entity_tolerance_hash_before": source_entity_tolerance_hash,
                       "source_per_entity_tolerance_hash_after": after_entity_tolerance_hash,
                       "actual_final_per_entity_tolerance_changes": actual_changes,
                       "official_native_envelope_source": "https://raw.githubusercontent.com/Open-Cascade-SAS/OCCT/V8_0_0/src/ModelingAlgorithms/TKTopAlgo/BRepLib/BRepLib.cxx",
                       "official_native_envelope_lines": "ComputeTol: 1012-1126; NCONTROL=22: 1227",
                       "new_pcurve_geometry_allowed": False}

def original_measured_residual_budget_audit(original, candidate, minimum_budget=1e-4, epsilon=1e-9):
    local = local_tolerance_budget_audit(original, candidate, minimum_budget)
    source_edges = {}
    source_vertex_edges = {}
    for index, edge in enumerate(shapes(original, TopAbs_EDGE), 1):
        key = edge_key(edge)
        if key in source_edges:
            return {"accepted": False, "reason": "AMBIGUOUS_COINCIDENT_SOURCE_EDGES",
                    "original_local_budget_audit": local, "expanded_metadata_checks": [],
                    "source_measurements": [], "numeric_epsilon_mm": epsilon}
        source_edges[key] = {"index": index, "edge": edge, "faces": []}
        for vertex in shapes(edge, TopAbs_VERTEX):
            source_vertex_edges.setdefault(point_key(vertex), set()).add(key)
    for face_index, face in enumerate(shapes(original, TopAbs_FACE), 1):
        iterator = TopExp_Explorer(face, TopAbs_EDGE)
        while iterator.More():
            oriented_edge = iterator.Current()
            source_edges[edge_key(oriented_edge)]["faces"].append((face_index, face, oriented_edge))
            iterator.Next()
    measurements, rows = {}, []
    entities = {kind: shapes(candidate, KINDS[kind]) for kind in ("edge", "vertex")}
    for violation in local["violations"]:
        kind = violation["kind"]
        if kind not in entities:
            rows.append({**violation, "accepted": False, "reason": "FACE_TOLERANCE_EXPANSION_FORBIDDEN"})
            continue
        entity = entities[kind][violation["index"] - 1]
        keys = {edge_key(entity)} if kind == "edge" else source_vertex_edges.get(point_key(entity), set())
        measured = []
        for key in sorted(keys):
            if key not in measurements:
                source = source_edges[key]
                try:
                    result = measure_source_edge_residual(source["edge"], source["faces"])
                except Exception as exc:
                    result = {"success": False, "error": str(exc)}
                measurements[key] = {"source_edge_index": source["index"], **result}
            measured.append(measurements[key])
        success = bool(measured) and all(row["success"] for row in measured)
        maximum = max((row["maximum_existing_source_residual_mm"] for row in measured if row["success"]), default=None)
        budget = max(violation["original_incident_tolerance_budget_mm"], maximum or 0.0)
        accepted = success and violation["candidate_tolerance_mm"] <= budget + epsilon
        rows.append({**violation, "source_existing_residual_budget_mm": budget,
                     "maximum_measured_source_residual_mm": maximum, "accepted": accepted,
                     "basis": "Existing original curve/surface/endpoint contradiction on this edge or directly incident source edges; vertex tolerance inherits incident edge uncertainty.",
                     "source_edge_indices": [row["source_edge_index"] for row in measured]})
    return {"numeric_epsilon_mm": epsilon, "minimum_numeric_budget_mm": minimum_budget,
            "original_local_budget_audit": local, "expanded_metadata_checks": rows,
            "source_measurements": sorted(measurements.values(), key=lambda row: row["source_edge_index"]),
            "accepted": all(row["accepted"] for row in rows)}

def native_source_residual_budget_audit(original, candidate, minimum_budget=1e-4, epsilon=1e-9):
    """Audit native margins on existing source gaps without changing a curve."""
    local = local_tolerance_budget_audit(original, candidate, minimum_budget)
    source_index, source_incident = _incident_edge_index(original)
    next_index, next_incident = _incident_edge_index(candidate)
    source_by_key, next_by_key, vertex_edges = {}, {}, {}
    for index_map, by_key in ((source_index, source_by_key), (next_index, next_by_key)):
        for i in range(1, index_map.Extent() + 1):
            edge = index_map.FindKey(i)
            key = edge_key(edge)
            if key in by_key:
                return {"accepted": False, "reason": "AMBIGUOUS_COINCIDENT_EDGE_IDENTITY"}
            by_key[key] = i
            if index_map is source_index:
                for vertex in shapes(edge, TopAbs_VERTEX):
                    vertex_edges.setdefault(point_key(vertex), set()).add(key)
    if source_by_key.keys() != next_by_key.keys():
        return {"accepted": False, "reason": "SOURCE_CANDIDATE_EDGE_GEOMETRY_DIFFERS"}
    measurements, rows = {}, []
    entities = {kind: shapes(candidate, KINDS[kind]) for kind in ("edge", "vertex")}
    for violation in local["violations"]:
        kind = violation["kind"]
        if kind not in entities:
            rows.append({**violation, "accepted": False, "reason": "FACE_TOLERANCE_EXPANSION_FORBIDDEN"})
            continue
        entity = entities[kind][violation["index"] - 1]
        keys = {edge_key(entity)} if kind == "edge" else vertex_edges.get(point_key(entity), set())
        relevant = []
        for key in sorted(keys):
            if key not in measurements:
                source_i, next_i = source_by_key[key], next_by_key[key]
                try:
                    source = measure_source_edge_residual(source_index.FindKey(source_i), source_incident[source_i])
                    after = measure_source_edge_residual(next_index.FindKey(next_i), next_incident[next_i])
                    envelope = max([1e-7, 1.5 * source["maximum_existing_source_residual_mm"],
                                    *source["source_3d_curve_endpoint_to_vertex_gaps_mm"],
                                    *(value["native_ComputeTol_1_5_safety_envelope_mm"] for value in source["source_curve_surface_checks"])])
                    measurements[key] = {"source_edge_index": source_i, "candidate_edge_index": next_i,
                        "source": source, "candidate": after, "native_source_safety_envelope_mm": envelope,
                        "actual_geometric_residual_not_increased": after["maximum_existing_source_residual_mm"] <= source["maximum_existing_source_residual_mm"] + epsilon,
                        "independent_source_residual_within_native_envelope": source["maximum_existing_source_residual_mm"] <= envelope + epsilon,
                        "success": True}
                except Exception as exc:
                    measurements[key] = {"source_edge_index": source_i, "success": False, "error": str(exc)}
            relevant.append(measurements[key])
        success = bool(relevant) and all(value["success"] and value["actual_geometric_residual_not_increased"]
                                        and value["independent_source_residual_within_native_envelope"] for value in relevant)
        envelope = max((value["native_source_safety_envelope_mm"] for value in relevant if value["success"]), default=0.0)
        budget = max(violation["original_incident_tolerance_budget_mm"] or 0.0, envelope)
        rows.append({**violation, "documented_source_native_safety_budget_mm": budget,
                     "source_edge_indices": [value["source_edge_index"] for value in relevant],
                     "actual_source_candidate_residual_not_increased": success,
                     "accepted": success and violation["candidate_tolerance_mm"] <= budget + epsilon})
    return {"numeric_epsilon_mm": epsilon, "source_native_safety_margin": 1.5,
            "native_control_segment_count": 22, "minimum_native_tolerance_mm": 1e-7,
            "basis": "The documented OCCT ComputeTol1.5 safety factor applied to an independently measured original GeomLib maximum (22-segment native samples recorded separately), plus original endpoint gaps; unchanged full-double 2D/3D geometry required. Vertex budget only inherits directly incident source edges.",
            "official_source": "https://raw.githubusercontent.com/Open-Cascade-SAS/OCCT/V8_0_0/src/ModelingAlgorithms/TKTopAlgo/BRepLib/BRepLib.cxx",
            "official_lines": "ComputeTol 1012-1126; NCONTROL=22 at1227",
            "original_local_budget_audit": local, "expanded_metadata_checks": rows,
            "source_and_candidate_measurements": sorted(measurements.values(), key=lambda value: value["source_edge_index"]),
            "accepted": all(value["accepted"] for value in rows)}


def solid_preservation_map(original, candidate):
    def signature(solid):
        encoded = json.dumps(sorted(boundary_manifest(solid).items()), separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    source_solids, candidate_solids = shapes(original, TopAbs_SOLID), shapes(candidate, TopAbs_SOLID)
    remaining = {}
    for index, solid in enumerate(candidate_solids, 1):
        remaining.setdefault(signature(solid), []).append((index, solid))
    mapping = []
    for index, solid in enumerate(source_solids, 1):
        key = signature(solid)
        matches = remaining.get(key, [])
        if not matches:
            mapping.append({"source_solid_index": index, "exact_geometry_retained": False})
            continue
        candidate_index, next_solid = matches.pop(0)
        original_valid, candidate_valid = bool(BRepCheck_Analyzer(solid).IsValid()), bool(BRepCheck_Analyzer(next_solid).IsValid())
        mapping.append({"source_solid_index": index, "candidate_solid_index": candidate_index,
                        "placed_surface_and_3d_trim_manifest_sha256": key, "exact_geometry_retained": True,
                        "source_valid": original_valid, "candidate_valid": candidate_valid,
                        "source_volume_mm3": _shape_volume(solid), "source_volume_verified": original_valid,
                        "candidate_volume_mm3": _shape_volume(next_solid), "candidate_volume_verified": candidate_valid,
                        "source_area_mm2": shape_area(solid), "candidate_area_mm2": shape_area(next_solid)})
    return {"source_to_candidate": mapping,
            "all_source_solids_exactly_retained": len(source_solids) == len(candidate_solids) and all(row["exact_geometry_retained"] for row in mapping),
            "unmatched_candidate_solids": sum(len(values) for values in remaining.values())}

def sampled_bidirectional_distance(original, candidate, limit=48):
    """Bounded corroborating sample test, never substituted for exact gates."""
    distances, face_samples, edge_samples, local_violations, max_ratio = [], 0, 0, [], 0.0
    for direction, (source, target) in enumerate(((original, candidate), (candidate, original))):
        source_faces = shapes(source, TopAbs_FACE)
        target_faces = shapes(target, TopAbs_FACE)
        # Face order does not need to be stable: choose the placed support
        # surface and closest matching geometric area/bbox among duplicates.
        targets = {}
        for face in target_faces:
            key = geometry_hash(BRep_Tool.Surface_s(TopoDS.Face(face)))
            targets.setdefault(key, []).append(face)
        stride = max(1, math.ceil(len(source_faces) / limit))
        for entity in source_faces[::stride]:
            face = TopoDS.Face(entity)
            key = geometry_hash(BRep_Tool.Surface_s(face))
            counterparts = targets.get(key, [])
            if not counterparts:
                raise ValueError("Missing matching surface in sample comparison")
            bbox = shape_bbox(face)
            counterpart = min(counterparts, key=lambda f: sum(abs(shape_bbox(f)[s][i] - bbox[s][i])
                              for s in ("min", "max") for i in range(3)))
            low_u, high_u, low_v, high_v = BRepTools.UVBounds_s(face)
            surface = BRep_Tool.Surface_s(face)
            for fu, fv in ((.5, .5), (.25, .25), (.75, .75), (.25, .75), (.75, .25)):
                u, v = low_u + fu * (high_u - low_u), low_v + fv * (high_v - low_v)
                if not all(math.isfinite(t) for t in (u, v)):
                    continue
                if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), 1e-7).State() not in (TopAbs_IN, TopAbs_ON):
                    continue
                distance = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(surface.Value(u, v)).Vertex(), counterpart)
                if not distance.IsDone():
                    raise ValueError("Face sample projection did not complete")
                value = float(distance.Value())
                distances.append(value)
                reference_face = face if direction == 0 else counterpart
                budget = face_local_budget(reference_face)
                max_ratio = max(max_ratio, value / budget)
                if value > budget + 1e-12:
                    local_violations.append({"kind": "face", "direction": direction,
                                             "distance_mm": value, "original_local_budget_mm": budget})
                face_samples += 1
                break
        source_edges = shapes(source, TopAbs_EDGE)
        target_edges = {}
        for entity in shapes(target, TopAbs_EDGE):
            edge = TopoDS.Edge(entity)
            key = (geometry_hash(BRep_Tool.Curve_s(edge, 0.0, 0.0)), BRep_Tool.Range_s(edge))
            target_edges.setdefault(key, []).append(edge)
        edge_stride = max(1, math.ceil(len(source_edges) / limit))
        for entity in source_edges[::edge_stride]:
            edge = TopoDS.Edge(entity)
            if BRep_Tool.Degenerated_s(edge):
                continue
            curve = BRepAdaptor_Curve(edge)
            key = (geometry_hash(BRep_Tool.Curve_s(edge, 0.0, 0.0)), BRep_Tool.Range_s(edge))
            counterparts = target_edges.get(key, [])
            if not counterparts:
                raise ValueError("Missing matching trimmed 3D edge in sample comparison")
            bbox = shape_bbox(edge)
            counterpart = min(counterparts, key=lambda e: sum(abs(shape_bbox(e)[s][i] - bbox[s][i])
                              for s in ("min", "max") for i in range(3)))
            for fraction in (0.0, .5, 1.0):
                parameter = curve.FirstParameter() + fraction * (curve.LastParameter() - curve.FirstParameter())
                distance = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(curve.Value(parameter)).Vertex(), counterpart)
                if not distance.IsDone():
                    raise ValueError("Edge sample projection did not complete")
                value = float(distance.Value())
                distances.append(value)
                reference_edge = edge if direction == 0 else counterpart
                budget = edge_local_budget(reference_edge)
                max_ratio = max(max_ratio, value / budget)
                if value > budget + 1e-12:
                    local_violations.append({"kind": "edge", "direction": direction,
                                             "distance_mm": value, "original_local_budget_mm": budget})
                edge_samples += 1
    return {"max_distance_mm": max(distances, default=None), "face_sample_count": face_samples,
            "edge_sample_count": edge_samples, "limit_per_kind_per_direction": limit,
            "local_budget_violations": local_violations, "max_distance_to_local_budget_ratio": max_ratio,
            "within_original_incident_local_budgets": not local_violations,
            "limitation": "Bounded corroborating samples are not a complete Hausdorff distance proof."}

def diagnose_status_counts(shape):
    analyzer = BRepCheck_Analyzer(shape)
    counts = Counter()
    for kind in KINDS.values():
        for entity in shapes(shape, kind):
            check = analyzer.Result(entity)
            if check is None:
                continue
            direct, contexts = analyzer_statuses(check)
            counts.update(direct)
            for context in contexts:
                counts.update(context["statuses"])
            if not analyzer.IsValid(entity):
                counts["invalid_" + enum_name(kind)] += 1
    return dict(counts)

def copied(shape):
    return BRepBuilderAPI_Copy(shape, True, False).Shape()

def shape_fix(shape, maximum, constrained=False):
    fix = ShapeFix_Shape(copied(shape))
    fix.SetPrecision(1e-7)
    fix.SetMinTolerance(1e-7)
    fix.SetMaxTolerance(maximum)
    if constrained:
        fix.FixVertexPositionMode = 0
        face = fix.FixFaceTool()
        for mode in ("RemoveSmallAreaFaceMode", "FixSmallAreaWireMode", "FixAddNaturalBoundMode",
                     "FixSplitFaceMode", "FixLoopWiresMode", "FixIntersectingWiresMode"):
            setattr(face, mode, 0)
        wire = fix.FixWireTool()
        wire.ModifyGeometryMode = False
        for mode in ("FixSmallMode", "FixGaps3dMode", "FixSelfIntersectionMode",
                     "FixIntersectingEdgesMode", "FixNonAdjacentIntersectingEdgesMode",
                     "FixNotchedEdgesMode", "FixTailMode", "FixLackingMode"):
            setattr(wire, mode, 0)
    fix.Perform()
    return fix.Shape(), {"precision_mm": 1e-7, "max_tolerance_mm": maximum,
                          "constrained_geometry_changes": constrained}

def parameter_fix(shape, forced_same_parameter=False, vertex_tolerances=True, inner_tolerances=False,
                  target_invalid_faces=False, use_shape_fix_edge=False, clamp_local_tolerances=False):
    candidate = copied(shape)
    changed_edges = 0
    selected = shapes(candidate, TopAbs_EDGE)
    if target_invalid_faces:
        analyzer = BRepCheck_Analyzer(candidate)
        targets = ShapeMap()
        for face in shapes(candidate, TopAbs_FACE):
            if not analyzer.IsValid(face):
                for edge in shapes(face, TopAbs_EDGE):
                    targets.Add(edge)
        selected = [targets.FindKey(i) for i in range(1, targets.Extent() + 1)]
    if forced_same_parameter:
        if target_invalid_faces:
            for edge in selected:
                BRepLib.SameParameter_s(edge, 1e-7, True)
        else:
            BRepLib.SameParameter_s(candidate, 1e-7, True)
    if use_shape_fix_edge:
        fix = ShapeFix_Edge()
        for entity in selected:
            fix.FixSameParameter(TopoDS.Edge(entity), 1e-7)
    if vertex_tolerances:
        fix = ShapeFix_Edge()
        for entity in selected:
            changed_edges += bool(fix.FixVertexTolerance(TopoDS.Edge(entity)))
    if inner_tolerances:
        BRepLib.UpdateInnerTolerances_s(candidate)
    clamp_settings = {}
    if clamp_local_tolerances:
        candidate, clamp_settings = clamp_to_original_local_budgets(shape, candidate)
    return candidate, {"forced_same_parameter": forced_same_parameter,
                       "vertex_tolerance_update": vertex_tolerances,
                       "inner_tolerance_update": inner_tolerances,
                       "target_invalid_face_edges_only": target_invalid_faces,
                       "use_shape_fix_edge": use_shape_fix_edge, "selected_edge_count": len(selected),
                       "clamp_to_original_local_tolerances": clamp_local_tolerances, **clamp_settings,
                       "changed_edge_vertex_tolerances": changed_edges,
                       "new_tolerance_budget_enforced_by_preservation_gate_mm": 1e-4}

def face_domain_signatures(shape):
    signatures = {}
    for face in shapes(shape, TopAbs_FACE):
        bbox = shape_bbox(face)
        key = next(iter(boundary_manifest(face)))
        signatures.setdefault(key, []).append([shape_area(face), *(bbox[side][i] for side in ("min", "max") for i in range(3))])
    return {key: sorted(values) for key, values in signatures.items()}

def compare(original, candidate, support, before):
    after = snapshot(candidate)
    maximum_bbox_delta = max(abs(before["bbox"][s][i] - after["bbox"][s][i])
                             for s in ("min", "max") for i in range(3))
    volume_delta = abs(before["unverified_volume_mm3"] - after["unverified_volume_mm3"])
    geometry = support_geometry(candidate)
    prior_faces, next_faces = face_domain_signatures(original), face_domain_signatures(candidate)
    face_pairing_matches = (prior_faces.keys() == next_faces.keys() and
                            all(len(prior_faces[key]) == len(next_faces[key]) for key in prior_faces))
    face_delta = max((abs(a - b) for key in prior_faces for left, right in zip(prior_faces[key], next_faces[key])
                      for a, b in zip(left, right)), default=0.0) if face_pairing_matches else None
    face_area_delta = max((abs(left[0] - right[0]) for key in prior_faces
                           for left, right in zip(prior_faces[key], next_faces[key])), default=0.0) if face_pairing_matches else None
    face_bbox_delta = max((abs(a - b) for key in prior_faces for left, right in zip(prior_faces[key], next_faces[key])
                          for a, b in zip(left[1:], right[1:])), default=0.0) if face_pairing_matches else None
    tolerance_audit = tolerance_budget_audit(original, candidate)
    local_tolerance_audit = local_tolerance_budget_audit(original, candidate)
    solid_manifest_audit = solid_preservation_map(original, candidate)
    pcurve_audit = pcurve_preservation_audit(original, candidate)
    checks = {"valid": after["valid"], "solid_count_preserved": before["counts"]["solid"] == after["counts"]["solid"],
              "shell_count_preserved": before["counts"]["shell"] == after["counts"]["shell"],
              "face_count_preserved": before["counts"]["face"] == after["counts"]["face"],
              "placed_3d_surface_multiset_exactly_preserved": support["surfaces"] == geometry["surfaces"],
              "placed_3d_curve_multiset_exactly_preserved": support["curves"] == geometry["curves"],
              "exact_face_wire_3d_trim_manifest_preserved": boundary_manifest(original) == boundary_manifest(candidate),
              "exact_2d_pcurve_coefficients_and_trim_manifest_preserved": pcurve_manifest(original) == pcurve_manifest(candidate),
              "per_entity_tolerance_budget_preserved": tolerance_audit["accepted"],
              "volume_preserved_within_1e_6_relative": volume_delta <= max(1e-6, abs(before["unverified_volume_mm3"]) * 1e-6),
              "bbox_preserved_within_1e_6_mm": maximum_bbox_delta <= 1e-6,
              "area_preserved_within_1e_7_relative": abs(before["area_mm2"] - after["area_mm2"]) <= max(1e-6, before["area_mm2"] * 1e-7),
              "per_face_area_bbox_preserved_within_1e_5": face_delta is not None and face_delta <= 1e-5}
    sampled = None
    if all(checks.values()):
        sampled = sampled_bidirectional_distance(original, candidate)
        checks["sampled_bidirectional_distance_within_1e_6_mm"] = sampled["max_distance_mm"] is not None and sampled["max_distance_mm"] <= 1e-6
    analytic_gate_names = ("valid", "solid_count_preserved", "shell_count_preserved", "face_count_preserved",
                          "placed_3d_surface_multiset_exactly_preserved", "placed_3d_curve_multiset_exactly_preserved",
                          "exact_face_wire_3d_trim_manifest_preserved", "bbox_preserved_within_1e_6_mm")
    analytic_checks = {key: checks[key] for key in analytic_gate_names}
    analytic_checks["all_unique_topology_counts_preserved"] = before["counts"] == after["counts"]
    analytic_checks["every_original_solid_exactly_retained"] = solid_manifest_audit["all_source_solids_exactly_retained"]
    analytic_checks["original_2d_trimmed_trace_proved_by_full_double_identity_or_exact_affine_certificate"] = pcurve_audit["accepted"]
    residual_audit = None
    native_residual_audit = None
    analytic_checks["global_edge_vertex_tolerance_max_not_increased"] = all(after["max_tolerance_mm"][key] <= before["max_tolerance_mm"][key] + 1e-12 for key in ("edge", "vertex"))
    if all(analytic_checks.values()) and not local_tolerance_audit["accepted"]:
        residual_audit = original_measured_residual_budget_audit(original, candidate)
        if not residual_audit["accepted"]:
            native_residual_audit = native_source_residual_budget_audit(original, candidate)
    analytic_checks["original_incident_tolerance_or_directly_measured_source_residual_budget_preserved"] = (
        local_tolerance_audit["accepted"] or bool(residual_audit and residual_audit["accepted"])
        or bool(native_residual_audit and native_residual_audit["accepted"]))
    if all(analytic_checks.values()) and sampled is None:
        sampled = sampled_bidirectional_distance(original, candidate)
    analytic_checks["bounded_samples_within_original_incident_local_budgets"] = bool(sampled and sampled["within_original_incident_local_budgets"])
    integral_volume_relative_delta = volume_delta / abs(before["unverified_volume_mm3"]) if before["unverified_volume_mm3"] else None
    integral_area_relative_delta = abs(before["area_mm2"] - after["area_mm2"]) / before["area_mm2"] if before["area_mm2"] else None
    return {**after, "preservation_checks": checks, "strictly_acceptable": all(checks.values()),
            "analytic_geometry_preservation_checks": analytic_checks,
            "analytic_geometry_acceptable": all(analytic_checks.values()),
            "sampled_bidirectional_distance": sampled,
            "tolerance_budget_audit": tolerance_audit,
            "original_incident_local_tolerance_budget_audit": local_tolerance_audit,
            "directly_measured_original_source_residual_budget_audit": residual_audit,
            "documented_native_source_residual_budget_audit": native_residual_audit,
            "source_solid_preservation_map": solid_manifest_audit,
            "original_2d_pcurve_preservation_audit": pcurve_audit,
            "integral_comparison": {"original_values_verified": before["valid"],
                "candidate_values_verified": after["valid"], "volume_relative_delta": integral_volume_relative_delta,
                "area_relative_delta": integral_area_relative_delta, "sanity_warning_relative_threshold": 1e-5,
                "sanity_warnings": [name for name, value in (("volume", integral_volume_relative_delta), ("area", integral_area_relative_delta))
                                    if value is None or value > 1e-5],
                "basis": "Invalid-source surface/volume integrals are comparison evidence, not a certified material baseline; exact 3D geometry and original local tolerance gates govern analytic repair acceptance."},
            "verified_volume_mm3": after["unverified_volume_mm3"] if after["valid"] else None,
            "topology_count_deltas": {kind: after["counts"][kind] - before["counts"][kind] for kind in KINDS},
            "bbox_max_delta_mm": maximum_bbox_delta, "volume_absolute_delta_mm3": volume_delta,
            "face_area_bbox_max_delta_mixed_units": face_delta,
            "per_face_area_max_delta_mm2": face_area_delta, "per_face_bbox_max_delta_mm": face_bbox_delta,
            "remaining_status_counts": diagnose_status_counts(candidate)}

def verified_topology_repair(shape):
    """Return a proved deep-copy candidate or None, together with every audit.

    A 0.0001 mm numerical repair allowance is combined only with uncertainty
    already declared on directly incident original topology. The shape-wide
    edge/vertex maximum is never increased. Global source tolerance does not
    authorize expanding unrelated local tolerance.
    """
    started = time.perf_counter()
    before = snapshot(shape)
    audit = {"schema_version": 1, "occt_version": OCP.__version__,
             "operation": "verified_topology_representation_repair", "source": before,
             "source_modified": False, "attempts": [],
             "acceptance_basis": "Exact placed 3D support/trim/vertex identity, source solid identity, all topology counts, original incident tolerance or directly measured original residual budgets, standard BRepCheck_Analyzer validity and bounded bidirectional samples.",
             "limitations": ["Bounded distance samples are corroborating evidence and do not prove arbitrary absence of self-intersections.",
                             "Standard and exact BRepCheck_Analyzer results are recorded independently; acceptance follows the pipeline's standard method, and disagreements require review when stronger validity is required.",
                             "Computed integrals of an invalid source are unverified comparison evidence, not a certified material baseline."]}
    if before["valid"]:
        audit.update(status="already_valid", elapsed_s=time.perf_counter() - started)
        return None, audit
    if not before["bbox"] or not before["counts"]["solid"]:
        audit.update(status="blocked", reason="EMPTY_OR_NON_SOLID_SOURCE", elapsed_s=time.perf_counter() - started)
        return None, audit
    try:
        supports = support_geometry(shape)
    except Exception as exc:
        audit.update(status="blocked", reason="UNSUPPORTED_EXACT_SOURCE_GEOMETRY", error=str(exc),
                     elapsed_s=time.perf_counter() - started)
        return None, audit
    recipes = [("constrained_shape_fix", lambda: shape_fix(shape, 1e-4, True)),
               ("source_residual_metadata_with_original_pcurves", lambda: source_residual_metadata_repair(shape)),
               ("targeted_same_parameter", lambda: parameter_fix(shape, True, False, False, True, False, False)),
               ("targeted_same_parameter_with_original_local_tolerance_clamp", lambda: parameter_fix(shape, True, False, False, True, False, True))]
    for name, recipe in recipes:
        attempt_started = time.perf_counter()
        try:
            candidate, settings = recipe()
            attempt = {"method": name, "settings": settings, **compare(shape, candidate, supports, before)}
        except Exception as exc:
            candidate = None
            attempt = {"method": name, "analytic_geometry_acceptable": False, "error": str(exc)}
        attempt["elapsed_s"] = time.perf_counter() - attempt_started
        audit["attempts"].append(attempt)
        if attempt["analytic_geometry_acceptable"]:
            audit.update(status="accepted", accepted_method=name,
                         candidate_features=snapshot(candidate), elapsed_s=time.perf_counter() - started)
            return candidate, audit
    audit.update(status="blocked", reason="NO_CANDIDATE_PASSED_GEOMETRY_VALIDITY_AND_ORIGINAL_LOCAL_TOLERANCE_GATES",
                 elapsed_s=time.perf_counter() - started)
    return None, audit


def verified_exact_representation_repair(shape, maximum_increment_mm=1e-4, allow_coarse_calibration=False):
    """Calibrate understated edge tolerances against measured exact residuals.

    This never moves geometry or changes pcurves, parameter ranges, topology,
    or validation settings. The strict policy limits each changed edge to its
    own tolerance plus the fixed numerical repair allowance. Optional assembly
    analysis calibration permits a measured per-edge increment limited to
    max(0.0001 mm, min(0.05 mm, source bounding-box diagonal * 0.001)). An
    existing local tolerance is retained, not treated as new damage. No
    unrelated global tolerance lends its budget to an edge. Local uncertainty is
    disclosed even when original metadata understated it. Clearance certification
    requires a downstream uncertainty guard; nominal interference detection is
    explicitly limited to the recorded CAD representation precision.
    A vertex can inherit
    only a directly incident changed edge's tolerance. Both standard and exact
    OCCT checks must pass on the candidate. Larger source contradictions are
    reported and the candidate is rejected, not silently declared valid.
    """
    from OCP.BRep import BRep_Builder
    from .exact_extrema import measured_residual
    if not math.isfinite(maximum_increment_mm) or not 0 <= maximum_increment_mm <= 1e-4:
        raise ValueError("Exact repair increment must be between 0 and 0.0001 mm")
    started = time.perf_counter()
    ordinary = bool(BRepCheck_Analyzer(shape).IsValid())
    exact = BRepCheck_Analyzer(shape, True, False, True)
    source_tolerance = max_tolerances(shape)
    audit = {"schema_version": 4, "operation": "exact_curve_surface_tolerance_calibration",
             "occt_version": OCP.__version__, "source_modified": False,
             "source_standard_valid": ordinary, "source_exact_valid": bool(exact.IsValid()),
             "maximum_per_edge_tolerance_increment_mm": maximum_increment_mm,
             "source_max_tolerance_mm": source_tolerance,
             "representation_uncertainty_mm": max(source_tolerance.values()),
             "allow_coarse_calibration": bool(allow_coarse_calibration),
             "calibration_budget": dict(EXACT_CALIBRATION_POLICY),
             "coarse_calibrated_edge_indices": [],
             "geometry_changed": False, "measurements": [], "rejected_edges": [],
             "acceptance_basis": "Unchanged full-double 3D/2D supports, trims and vertices; complete independently checked native exact extrema; local tolerance increment bounded by 0.0001 mm (strict) or max(0.0001 mm, min(0.05 mm, source diagonal * 0.001)) (assembly analysis); existing local tolerances remain disclosed; both OCCT analyzers must pass.",
             "official_method_source": "https://github.com/Open-Cascade-SAS/OCCT/blob/V8_0_0/src/ModelingAlgorithms/TKTopAlgo/BRepLib/BRepLib_ValidateEdge.cxx",
             "limitations": ["Tolerance calibration describes an existing representation mismatch; it does not improve the precision of the source CAD surfaces.",
                             "Assembly clearance certification must retain the reported representation uncertainty.",
                             EXACT_CALIBRATION_POLICY["nominal_scope"]]}
    if audit["source_exact_valid"]:
        audit.update(status="already_valid", elapsed_s=time.perf_counter() - started)
        return None, audit
    if not ordinary:
        audit.update(status="blocked", reason="STANDARD_TOPOLOGY_REPAIR_REQUIRED_FIRST",
                     elapsed_s=time.perf_counter() - started)
        return None, audit
    source_bbox = shape_bbox(shape)
    dimensions = source_bbox["size"] if source_bbox else []
    if len(dimensions) != 3 or not all(math.isfinite(v) and v >= 0 for v in dimensions):
        audit.update(status="blocked", reason="NON_FINITE_SOURCE_GEOMETRY_EXTENT", elapsed_s=time.perf_counter() - started)
        return None, audit
    diagonal = math.hypot(*dimensions)
    if not math.isfinite(diagonal) or diagonal <= 0:
        audit.update(status="blocked", reason="EMPTY_SOURCE_GEOMETRY_EXTENT", elapsed_s=time.perf_counter() - started)
        return None, audit
    assembly_increment_budget = max(EXACT_CALIBRATION_POLICY["assembly_minimum_increment_budget_mm"],
        min(EXACT_CALIBRATION_POLICY["assembly_maximum_per_edge_increment_mm"],
            diagonal * EXACT_CALIBRATION_POLICY["assembly_maximum_increment_fraction_of_diagonal"]))
    audit["calibration_budget"].update(source_diagonal_mm=diagonal,
        evaluated_assembly_per_edge_increment_mm=assembly_increment_budget,
        evaluated_strict_per_edge_increment_mm=maximum_increment_mm)
    original_support = support_geometry(shape)
    original_boundary = boundary_manifest(shape)
    original_pcurves = pcurve_manifest(shape)
    original_tolerances = per_entity_tolerance_hash(shape)
    original_counts = {name: len(shapes(shape, kind)) for name, kind in KINDS.items()}
    source_index, incident = _incident_edge_index(shape)
    targets = {index for index, rows in incident.items()
               if any(not exact.IsValid(face) for _, face, _ in rows)}
    required = {}
    for index in sorted(targets):
        edge = TopoDS.Edge(source_index.FindKey(index))
        if BRep_Tool.Degenerated_s(edge):
            continue
        old_tolerance = float(BRep_Tool.Tolerance_s(edge))
        edge_measurements = []
        maximum = 0.0
        failure = None
        for face_index, entity, oriented in incident[index]:
            face, oriented = TopoDS.Face(entity), TopoDS.Edge(oriented)
            try:
                pcurve = BRep_Tool.CurveOnSurface_s(oriented, face, 0.0, 0.0)
                measurement = measured_residual(edge, oriented, face)
                measured = measurement["measured_mm"]
                maximum = max(maximum, measured)
                if not measurement["within_source_tolerance"]:
                    edge_measurements.append({"face_index": face_index,
                        "edge_orientation": enum_name(oriented.Orientation()),
                        "surface_type": BRep_Tool.Surface_s(face).DynamicType().Name(),
                        "pcurve_type": pcurve.DynamicType().Name(),
                        "exact_residual_with_native_1_00001_margin_mm": measured,
                        "independent_257_point_residual_lower_bound_mm": measurement["independent_sample_lower_bound_mm"],
                        "native_full_interval_mm": measurement["native_full_interval_mm"],
                        "complete_interval_extrema_retries": measurement["interval_retry"]})
            except Exception as exc:
                failure = str(exc)
                break
        if failure:
            audit["rejected_edges"].append({"edge_index": index, "reason": failure})
            continue
        if not edge_measurements:
            continue
        requested = max(old_tolerance, maximum + 1e-10)
        row = {"edge_index": index, "source_edge_tolerance_mm": old_tolerance,
               "measured_existing_source_residual_mm": maximum,
               "requested_edge_tolerance_mm": requested,
               "increment_mm": requested - old_tolerance, "faces": edge_measurements}
        audit["measurements"].append(row)
        if requested - old_tolerance > maximum_increment_mm + 1e-12:
            coarse_allowed = (allow_coarse_calibration and requested - old_tolerance <= assembly_increment_budget)
            if coarse_allowed:
                required[index] = requested
                audit["coarse_calibrated_edge_indices"].append(index)
            else:
                audit["rejected_edges"].append({**row,
                    "allowed_local_increment_mm": assembly_increment_budget if allow_coarse_calibration else maximum_increment_mm,
                    "reason": "MEASURED_RESIDUAL_EXCEEDS_LOCAL_NUMERICAL_REPAIR_BUDGET"})
        else:
            required[index] = requested
    audit["maximum_measured_source_residual_mm"] = max(
        (row["measured_existing_source_residual_mm"] for row in audit["measurements"]), default=0.0)
    audit["maximum_required_increment_mm"] = max((row["increment_mm"] for row in audit["measurements"]), default=0.0)
    measurement_complete = not any("requested_edge_tolerance_mm" not in row for row in audit["rejected_edges"])
    audit["residual_measurement_complete"] = measurement_complete
    audit["representation_uncertainty_mm"] = max(max(source_tolerance.values()), audit["maximum_measured_source_residual_mm"]) if measurement_complete else None
    if audit["rejected_edges"]:
        audit.update(status="blocked", reason="EXACT_RESIDUAL_REQUIRES_SOURCE_GEOMETRY_REVIEW",
                     elapsed_s=time.perf_counter() - started)
        return None, audit
    candidate = copied(shape)
    candidate_edges = shapes(candidate, TopAbs_EDGE)
    builder = BRep_Builder()
    for index, tolerance in required.items():
        edge = TopoDS.Edge(candidate_edges[index - 1])
        if edge_key(edge) != edge_key(source_index.FindKey(index)):
            raise ValueError("Deep-copy edge mapping changed")
        builder.UpdateEdge(edge, tolerance)
        for vertex in shapes(edge, TopAbs_VERTEX):
            builder.UpdateVertex(TopoDS.Vertex(vertex), tolerance)
    checks = {"placed_3d_supports_unchanged": original_support == support_geometry(candidate),
              "face_wire_3d_trims_and_vertices_unchanged": original_boundary == boundary_manifest(candidate),
              "every_original_2d_pcurve_and_range_unchanged": original_pcurves == pcurve_manifest(candidate),
              "all_unique_topology_counts_unchanged": original_counts == {name: len(shapes(candidate, kind)) for name, kind in KINDS.items()},
              "source_tolerances_unchanged": original_tolerances == per_entity_tolerance_hash(shape),
              "standard_valid": bool(BRepCheck_Analyzer(candidate).IsValid()),
              "exact_method_valid": bool(BRepCheck_Analyzer(candidate, True, False, True).IsValid())}
    audit.update(preservation_checks=checks, changed_edge_count=len(required),
                 source_max_tolerance_mm=max_tolerances(shape),
                 candidate_max_tolerance_mm=max_tolerances(candidate),
                 representation_uncertainty_mm=max(max_tolerances(candidate).values()),
                 elapsed_s=time.perf_counter() - started)
    if all(checks.values()):
        coarse = bool(audit["coarse_calibrated_edge_indices"])
        audit.update(status="accepted", accepted_method="bounded_measured_source_uncertainty_calibration" if coarse else "bounded_exact_residual_metadata_calibration",
                     collision_clearance_guard_required=True,
                     analysis_scope="assembly_collision_with_source_uncertainty" if coarse else "exact_occt_preserved_geometry")
        return candidate, audit
    audit.update(status="blocked", reason="EXACT_CANDIDATE_DID_NOT_PASS_ALL_PRESERVATION_AND_VALIDITY_GATES")
    return None, audit
