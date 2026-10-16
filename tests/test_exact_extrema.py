"""An underestimated native maximum must be re-solved, never sample-certified."""
import sys,unittest
from pathlib import Path
from unittest.mock import MagicMock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.exact_extrema import measured_residual
from backend import representation_repair as r
from tests.test_exact_representation_repair import missed_narrow_peak
from OCP.TopAbs import TopAbs_EDGE,TopAbs_FACE
from OCP.TopoDS import TopoDS
from OCP.BRepAdaptor import BRepAdaptor_Curve
from OCP.GeomAdaptor import GeomAdaptor_Curve
from OCP.BRepLib import BRepLib_ValidateEdge

class ExactExtremaTests(unittest.TestCase):
    def setUp(self):
        self.shape=missed_narrow_peak()
        self.face=TopoDS.Face(r.shapes(self.shape,TopAbs_FACE)[0])
        self.edge=TopoDS.Edge(r.shapes(self.face,TopAbs_EDGE)[0])

    def test_underestimated_native_maximum_is_recomputed_on_complete_intervals(self):
        native=MagicMock();native.IsDone.return_value=True;native.GetMaxDistance.return_value=1e-5;native.CheckTolerance.return_value=False
        source=r.pcurve_manifest(self.shape)
        with patch('backend.exact_extrema.BRepLib_ValidateEdge',return_value=native):
            result=measured_residual(self.edge,self.edge,self.face)
        self.assertGreaterEqual(result['measured_mm'],2e-5)
        self.assertGreaterEqual(len(result['interval_retry']),2)
        self.assertTrue(all(x['entire_original_interval_covered'] for x in result['interval_retry']))
        self.assertEqual(source,r.pcurve_manifest(self.shape))

    def test_sample_evidence_alone_does_not_accept_failed_extrema_solver(self):
        native=MagicMock();native.IsDone.return_value=True;native.GetMaxDistance.return_value=1e-5;native.CheckTolerance.return_value=False
        failed=MagicMock();failed.IsDone.return_value=False
        with patch('backend.exact_extrema.BRepLib_ValidateEdge',return_value=native),patch('backend.exact_extrema.GeomLib_CheckCurveOnSurface',return_value=failed):
            with self.assertRaisesRegex(ValueError,'did not complete'):
                measured_residual(self.edge,self.edge,self.face)

    def test_native_claim_within_tolerance_does_not_skip_sanity_check(self):
        native=MagicMock();native.IsDone.return_value=True;native.GetMaxDistance.return_value=1e-8;native.CheckTolerance.return_value=True
        with patch('backend.exact_extrema.BRepLib_ValidateEdge',return_value=native):
            result=measured_residual(self.edge,self.edge,self.face)
        self.assertGreaterEqual(result['measured_mm'],2e-5)
        self.assertFalse(result['within_source_tolerance'])
        self.assertGreaterEqual(len(result['interval_retry']),2)

    def test_peak_first_found_at_16_partitions_is_confirmed_at_32(self):
        native=MagicMock();native.IsDone.return_value=True;native.GetMaxDistance.return_value=1e-5;native.CheckTolerance.return_value=False
        full=BRepAdaptor_Curve(self.edge);span=full.LastParameter()-full.FirstParameter()
        def extrema(curve,parameter_tolerance):
            self.assertEqual(parameter_tolerance,1e-9)
            found=curve.LastParameter()-curve.FirstParameter()<=span/16+1e-12
            checked=MagicMock();checked.IsDone.return_value=True;checked.MaxDistance.return_value=2e-5 if found else 1e-5
            return checked
        with patch('backend.exact_extrema.BRepLib_ValidateEdge',return_value=native),patch('backend.exact_extrema.GeomLib_CheckCurveOnSurface',side_effect=extrema):
            result=measured_residual(self.edge,self.edge,self.face)
        self.assertEqual([row['subdivisions'] for row in result['interval_retry']],[2,4,8,16,32])
        self.assertGreaterEqual(result['measured_mm'],2e-5)
        self.assertFalse(result['within_source_tolerance'])

    def test_measurement_uses_same_native_reference_contract_as_exact_analyzer(self):
        # BRepCheck_Edge::Minimum uses the placed GeomAdaptor reference. Actual
        # supplied-CAD regressions show that substituting BRepAdaptor can miss
        # a maximum even when all independent samples are below that maximum.
        # Keep company geometry out of distributable tests; exercise the same
        # native contract on this generated narrow-peak curve.
        calls=[]
        def native_checker(reference,on_surface,same_parameter):
            self.assertIsInstance(reference,GeomAdaptor_Curve)
            self.assertTrue(same_parameter)
            calls.append((reference.FirstParameter(),reference.LastParameter()))
            return BRepLib_ValidateEdge(reference,on_surface,same_parameter)
        with patch('backend.exact_extrema.BRepLib_ValidateEdge',side_effect=native_checker):
            result=measured_residual(self.edge,self.edge,self.face)
        self.assertEqual(len(calls),1)
        self.assertGreaterEqual(result['measured_mm'],2e-5)
        self.assertFalse(result['within_source_tolerance'])

if __name__=='__main__':unittest.main()
