"""Representation repairs must correct measured metadata, never hide gaps."""
import sys, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import representation_repair as r
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.Geom2d import Geom2d_BSplineCurve
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Pnt2d
try:
    from OCP.collections import Array1_gp_Pnt2d as Points, Array1_double as Reals, Array1_int as Ints
except ImportError:
    from OCP.TColgp import TColgp_Array1OfPnt2d as Points
    from OCP.TColStd import TColStd_Array1OfReal as Reals, TColStd_Array1OfInteger as Ints


def missed_narrow_peak(height=2e-5, source_envelope=None, dimensions=(10., 12., 14.)):
    shape = BRepPrimAPI_MakeBox(*dimensions).Shape()
    face = TopoDS.Face(r.shapes(shape, TopAbs_FACE)[0])
    edge = TopoDS.Edge(r.shapes(face, TopAbs_EDGE)[0])
    pc = BRep_Tool.CurveOnSurface_s(edge, face, 0., 0.)
    low, high = BRep_Tool.Range_s(edge, face)
    points, knots, multiplicities = Points(1, 4), Reals(1, 4), Ints(1, 4)
    first, last = pc.Value(low), pc.Value(high)
    du, dv = last.X() - first.X(), last.Y() - first.Y()
    length = (du*du+dv*dv)**.5
    for i, fraction in enumerate((0., .01, .02, 1.), 1):
        t = low + fraction*(high-low)
        point = pc.Value(t)
        amplitude = height if i == 2 else 0.
        points.SetValue(i, gp_Pnt2d(point.X()-dv/length*amplitude, point.Y()+du/length*amplitude))
        knots.SetValue(i,t);multiplicities.SetValue(i,2 if i in (1,4) else 1)
    changed = Geom2d_BSplineCurve(points,knots,multiplicities,1,False)
    builder = BRep_Builder()
    builder.UpdateEdge(edge,changed,face,BRep_Tool.Tolerance_s(edge))
    builder.Range(edge,face,low,high)
    builder.SameRange(edge,True);builder.SameParameter(edge,True)
    if source_envelope:
        other = TopoDS.Edge(r.shapes(shape,TopAbs_EDGE)[-1])
        builder.UpdateEdge(other,source_envelope)
        for vertex in r.shapes(other,TopAbs_VERTEX):builder.UpdateVertex(TopoDS.Vertex(vertex),source_envelope)
    return shape


class ExactRepresentationRepairTests(unittest.TestCase):
    def test_narrow_curve_residual_missed_by_standard_sampling(self):
        shape = missed_narrow_peak()
        self.assertTrue(BRepCheck_Analyzer(shape).IsValid())
        self.assertFalse(BRepCheck_Analyzer(shape,True,False,True).IsValid())
        original = r.pcurve_manifest(shape)
        candidate, audit = r.verified_exact_representation_repair(shape)
        self.assertIsNotNone(candidate)
        self.assertTrue(BRepCheck_Analyzer(candidate,True,False,True).IsValid())
        self.assertEqual(original,r.pcurve_manifest(shape))
        self.assertEqual(original,r.pcurve_manifest(candidate))
        self.assertTrue(all(audit['preservation_checks'].values()))
        self.assertLessEqual(audit['maximum_required_increment_mm'],1e-4)

    def test_source_gap_larger_than_budget_is_rejected(self):
        candidate,audit=r.verified_exact_representation_repair(missed_narrow_peak(.002))
        self.assertIsNone(candidate)
        self.assertEqual(audit['status'],'blocked')
        self.assertGreater(audit['maximum_required_increment_mm'],1e-4)

    def test_coarse_calibration_requires_explicit_scope_and_uncertainty(self):
        shape=missed_narrow_peak(.002,source_envelope=.003)
        candidate,audit=r.verified_exact_representation_repair(shape,allow_coarse_calibration=True)
        self.assertIsNotNone(candidate)
        self.assertTrue(audit['collision_clearance_guard_required'])
        self.assertGreaterEqual(audit['representation_uncertainty_mm'],.003)
        self.assertEqual(audit['analysis_scope'],'assembly_collision_with_source_uncertainty')
        self.assertTrue(BRepCheck_Analyzer(candidate,True,False,True).IsValid())

    def test_coarse_policy_measures_local_gap_without_borrowing_global_tolerance(self):
        shape=missed_narrow_peak(.0075)
        before=r.per_entity_tolerance_hash(shape)
        candidate,audit=r.verified_exact_representation_repair(shape,allow_coarse_calibration=True)
        self.assertIsNotNone(candidate)
        self.assertTrue(all(audit['preservation_checks'].values()))
        self.assertEqual(before,r.per_entity_tolerance_hash(shape))
        self.assertGreater(audit['representation_uncertainty_mm'],.0075)
        self.assertLessEqual(audit['representation_uncertainty_mm'],.01)
        self.assertLessEqual(audit['maximum_required_increment_mm'],.01)
        self.assertAlmostEqual(audit['calibration_budget']['evaluated_assembly_per_edge_increment_mm'],(10**2+12**2+14**2)**.5*.001,places=7)
        self.assertTrue(audit['collision_clearance_guard_required'])

    def test_coarse_policy_does_not_admit_large_gap_even_with_large_global_tolerance(self):
        for height,envelope in ((.04,None),(.04,.3)):
            candidate,audit=r.verified_exact_representation_repair(missed_narrow_peak(height,envelope),allow_coarse_calibration=True)
            self.assertIsNone(candidate)
            self.assertEqual(audit['status'],'blocked')

    def test_existing_local_tolerance_is_disclosed_and_only_increment_is_budgeted(self):
        shape=missed_narrow_peak(.1382)
        edge=TopoDS.Edge(r.shapes(r.shapes(shape,TopAbs_FACE)[0],TopAbs_EDGE)[0])
        builder=BRep_Builder();builder.UpdateEdge(edge,.13801)
        for vertex in r.shapes(edge,TopAbs_VERTEX):builder.UpdateVertex(TopoDS.Vertex(vertex),.13801)
        source=r.per_entity_tolerance_hash(shape)
        candidate,audit=r.verified_exact_representation_repair(shape,allow_coarse_calibration=True)
        self.assertIsNotNone(candidate)
        self.assertLess(audit['maximum_required_increment_mm'],.0003)
        self.assertGreater(audit['representation_uncertainty_mm'],.1382)
        self.assertTrue(all(audit['preservation_checks'].values()))
        self.assertEqual(source,r.per_entity_tolerance_hash(shape))

    def test_coarse_size_budget_accepts_measured_small_gap_but_strict_does_not(self):
        shape=missed_narrow_peak(.02)
        candidate,strict=r.verified_exact_representation_repair(shape)
        self.assertIsNone(candidate)
        candidate,coarse=r.verified_exact_representation_repair(shape,allow_coarse_calibration=True)
        self.assertIsNotNone(candidate)
        self.assertGreater(coarse['maximum_required_increment_mm'],.02)
        self.assertTrue(all(coarse['preservation_checks'].values()))

    def test_assembly_increment_still_has_absolute_and_relative_limits(self):
        for dimensions,height,budget in (((100.,120.,140.),.051,.05),((1.,1.,1.),.002,3**.5*.001),((.01,.01,.01),.00011,1e-4)):
            candidate,audit=r.verified_exact_representation_repair(missed_narrow_peak(height,dimensions=dimensions),allow_coarse_calibration=True)
            self.assertIsNone(candidate)
            self.assertAlmostEqual(audit['calibration_budget']['evaluated_assembly_per_edge_increment_mm'],budget,places=7)
            self.assertGreater(audit['maximum_required_increment_mm'],budget)

    def test_incomplete_measurements_never_become_an_assembly_candidate(self):
        shape=missed_narrow_peak(.002)
        before=r.per_entity_tolerance_hash(shape)
        with patch('backend.exact_extrema.measured_residual',side_effect=ValueError('Native extrema failed')):
            candidate,audit=r.verified_exact_representation_repair(shape,allow_coarse_calibration=True)
        self.assertIsNone(candidate)
        self.assertFalse(audit['residual_measurement_complete'])
        self.assertIsNone(audit['representation_uncertainty_mm'])
        self.assertEqual(before,r.per_entity_tolerance_hash(shape))

    def test_valid_shape_records_its_uncertainty_without_changes(self):
        shape=BRepPrimAPI_MakeBox(1.,2.,3.).Shape()
        candidate,audit=r.verified_exact_representation_repair(shape)
        self.assertIsNone(candidate)
        self.assertEqual(audit['status'],'already_valid')
        self.assertTrue(audit['source_exact_valid'])
        self.assertGreater(audit['representation_uncertainty_mm'],0)

    def test_excessive_numeric_allowance_cannot_be_requested(self):
        with self.assertRaises(ValueError):
            r.verified_exact_representation_repair(BRepPrimAPI_MakeBox(1.,2.,3.).Shape(),.001)

if __name__=='__main__':unittest.main()
