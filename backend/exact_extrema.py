"""Bounded retry for native curve/surface extrema inconsistent with samples.

The samples only detect an underestimated native result. They never certify
an edge by themselves. Each retry runs the native extrema solver on a complete
partition of the unchanged original parameter interval.
"""
from __future__ import annotations
import math
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAdaptor import GeomAdaptor_Curve, GeomAdaptor_Surface
from OCP.Geom2dAdaptor import Geom2dAdaptor_Curve
from OCP.Adaptor3d import Adaptor3d_CurveOnSurface
from OCP.GeomLib import GeomLib_CheckCurveOnSurface
from OCP.BRepLib import BRepLib_ValidateEdge

EXTREMA_POLICY = {
    "version": "native-reference-extrema-2.4.0",
    "reference_adaptor": "GeomAdaptor_Curve on the placed 3D curve, matching BRepCheck_Edge",
    "complete_interval_retry_partitions": [2,4,8,16,32,64],
    "native_parameter_tolerance": 1e-9,
    "independent_sanity_allowance_mm": 1e-9,
    "consecutive_maximum_agreement_mm": 1e-9,
    "native_residual_margin_factor": 1.00001,
    "retry_budget_revision": "2.4.0; native reference adaptor aligned; bounded retries and independent sanity criteria retained",
}


def measured_residual(edge, oriented_edge, face, sample_count=257):
    if not isinstance(sample_count, int) or sample_count < 3:
        raise ValueError("At least three independent sanity samples are required")
    first, last = BRep_Tool.Range_s(oriented_edge, face)
    pcurve = BRep_Tool.CurveOnSurface_s(oriented_edge, face, 0.0, 0.0)
    if pcurve is None or not BRep_Tool.SameParameter_s(edge):
        raise ValueError("Missing pcurve or SameParameter flag")
    placed = BRepAdaptor_Curve(edge)
    low, high = placed.FirstParameter(), placed.LastParameter()
    if not all(math.isfinite(value) for value in (first,last,low,high)) or high <= low:
        raise ValueError("Invalid curve parameter interval")
    if abs(low-first)>1e-9 or abs(high-last)>1e-9:
        raise ValueError("Curve and pcurve parameter ranges differ")
    curve = BRep_Tool.Curve_s(edge, 0.0, 0.0)
    if curve is None:
        raise ValueError("Missing placed 3D reference curve")
    # BRepCheck_Edge::Minimum uses a GeomAdaptor_Curve of the placed curve.
    # BRepAdaptor_Curve can produce different extrema subdivisions even for
    # the same geometry; its smaller maximum does not certify native validity.
    reference = GeomAdaptor_Curve(curve, low, high)
    surface = BRep_Tool.Surface_s(face)
    on_surface = Adaptor3d_CurveOnSurface(Geom2dAdaptor_Curve(pcurve, first, last), GeomAdaptor_Surface(surface))
    checker = BRepLib_ValidateEdge(reference, on_surface, True)
    checker.SetExactMethod(True); checker.Process()
    if not checker.IsDone():
        raise ValueError("Exact curve-on-surface extrema did not complete")
    measured = float(checker.GetMaxDistance())
    if not math.isfinite(measured):
        raise ValueError("Non-finite exact curve-on-surface residual")
    tolerance = BRep_Tool.Tolerance_s(edge)
    needs_calibration = not checker.CheckTolerance(tolerance)
    result = {"measured_mm":measured, "within_source_tolerance":not needs_calibration,
              "native_full_interval_mm":measured, "independent_sample_lower_bound_mm":None,
              "interval_retry":[]}
    # Even a native maximum below the source tolerance can be underestimated.
    # Check every targeted edge; samples detect contradictions, never prove PASS.
    sampled = max(reference.Value(low+(high-low)*j/(sample_count-1)).Distance(
                  on_surface.Value(low+(high-low)*j/(sample_count-1))) for j in range(sample_count))
    result["independent_sample_lower_bound_mm"] = float(sampled)
    if sampled <= measured + EXTREMA_POLICY["independent_sanity_allowance_mm"]:
        return result
    # Complete partitions are retried; all observed native maxima are retained.
    # At least two successive partitions must agree to numerical roundoff.
    previous = None
    for subdivisions in EXTREMA_POLICY["complete_interval_retry_partitions"]:
        maxima=[]
        for index in range(subdivisions):
            start=low+(high-low)*index/subdivisions
            end=low+(high-low)*(index+1)/subdivisions
            subref=GeomAdaptor_Curve(curve,start,end)
            subcurve=Adaptor3d_CurveOnSurface(Geom2dAdaptor_Curve(pcurve,start,end),GeomAdaptor_Surface(surface))
            native=GeomLib_CheckCurveOnSurface(subref,EXTREMA_POLICY["native_parameter_tolerance"]);native.Perform(subcurve)
            if not native.IsDone() or not math.isfinite(float(native.MaxDistance())):
                raise ValueError("Subinterval exact curve-on-surface extrema did not complete")
            maxima.append(float(native.MaxDistance())*EXTREMA_POLICY["native_residual_margin_factor"])
        maximum=max(maxima)
        measured=max(measured,maximum)
        result["interval_retry"].append({"subdivisions":subdivisions,"native_maximum_mm":maximum,
                                          "entire_original_interval_covered":True})
        if previous is not None and abs(maximum-previous)<=EXTREMA_POLICY["consecutive_maximum_agreement_mm"] and sampled<=maximum+EXTREMA_POLICY["independent_sanity_allowance_mm"]:
            result["measured_mm"]=measured
            result["within_source_tolerance"]=measured <= tolerance
            return result
        previous=maximum
    raise ValueError("Exact extrema remain inconsistent after bounded complete-interval subdivision")
