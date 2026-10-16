"""A metadata-only CAD repair must not turn real penetration into a clear path."""
import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from OCP.BRep import BRep_Builder
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepTools import BRepTools
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_VERTEX
from OCP.TopoDS import TopoDS
from OCP.gp import gp_Pnt

from backend import representation_repair as repair
from backend.geometry import combine_bbox, shape_features
from backend.planning import GeometrySession, make_path
from tests.test_exact_representation_repair import missed_narrow_peak


NOMINAL_REFERENCE = {"mode": "nominal_interference", "reference_mode": "assembled_reference"}


class GeometryImpactPathTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # This deliberately exceeds the former 0.01 mm calibration ceiling.
        # Only the edge tolerance may change; its actual PCurve peak must stay.
        cls.profiles = []
        for name, peak, original_tolerance in (("new_increment", .02, None),
                                                ("existing_coarse_tolerance", .1382, .138)):
            source = missed_narrow_peak(peak)
            if original_tolerance is not None:
                edge = TopoDS.Edge(repair.shapes(repair.shapes(source, TopAbs_FACE)[0], TopAbs_EDGE)[0])
                builder = BRep_Builder()
                builder.UpdateEdge(edge, original_tolerance)
                for vertex in repair.shapes(edge, TopAbs_VERTEX):
                    builder.UpdateVertex(TopoDS.Vertex(vertex), original_tolerance)
            profile = {"name": name, "source": source, "peak": peak,
                       "features": shape_features(source), "support": repair.support_geometry(source),
                       "boundary": repair.boundary_manifest(source), "pcurves": repair.pcurve_manifest(source),
                       "tolerances": repair.per_entity_tolerance_hash(source)}
            profile["candidate"], profile["audit"] = repair.verified_exact_representation_repair(
                source, allow_coarse_calibration=True)
            if profile["candidate"] is None:
                raise AssertionError("The scoped calibration must produce " + name + ": " + str(profile["audit"]))
            cls.profiles.append(profile)

    def setUp(self):
        root = Path(__file__).resolve().parents[1] / "work" / "geometry-impact-path-tests"
        root.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=root)

    def tearDown(self):
        self.folder.cleanup()

    def verdict(self, profile, obstacle_position, obstacle_size, direction, distance, collision_policy=None):
        obstacle = BRepPrimAPI_MakeBox(gp_Pnt(*obstacle_position), *obstacle_size).Shape()
        occurrences, units = [], []
        for ident, shape in (("moving", profile["candidate"]), ("obstacle", obstacle)):
            path = Path(self.folder.name) / (ident + ".brep")
            self.assertTrue(BRepTools.Write_s(shape, str(path)))
            feature = shape_features(shape)
            occurrences.append({"id": ident, "name": ident, "brep_path": str(path),
                                "collision_ready": True, **feature,
                                "representation_uncertainty_mm":
                                    profile["audit"]["representation_uncertainty_mm"] if ident == "moving" else 1e-7})
            units.append({"id": ident, "name": ident, "occurrence_ids": [ident],
                          "collision_ready": True, "bbox": feature["bbox"], "fixed": ident == "obstacle"})
        model = {"fingerprint": hashlib.sha256(str((obstacle_position, obstacle_size)).encode()).hexdigest(),
                 "occurrences": occurrences, "units": units,
                 "bbox": combine_bbox(row["bbox"] for row in occurrences)}
        with GeometrySession(model, timeout=20, collision_policy=collision_policy or NOMINAL_REFERENCE) as engine:
            return engine.path_pair("moving", "obstacle", make_path(direction, distance), time.monotonic() + 30)

    def test_calibration_preserves_original_geometry_and_material_integrals(self):
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                source, candidate, audit = profile["source"], profile["candidate"], profile["audit"]
                self.assertTrue(BRepCheck_Analyzer(source).IsValid())
                self.assertFalse(BRepCheck_Analyzer(source, True, False, True).IsValid())
                self.assertTrue(BRepCheck_Analyzer(candidate).IsValid())
                self.assertTrue(BRepCheck_Analyzer(candidate, True, False, True).IsValid())
                if profile["name"] == "new_increment":
                    self.assertGreater(audit["maximum_required_increment_mm"], .01)
                else:
                    self.assertGreater(audit["maximum_required_increment_mm"], 1e-4)
                    self.assertLess(audit["maximum_required_increment_mm"], .001)
                self.assertGreaterEqual(audit["representation_uncertainty_mm"], profile["peak"])
                self.assertTrue(audit["collision_clearance_guard_required"])
                self.assertTrue(all(audit["preservation_checks"].values()))
                self.assertEqual(profile["tolerances"], repair.per_entity_tolerance_hash(source))
                for shape in (source, candidate):
                    self.assertEqual(profile["support"], repair.support_geometry(shape))
                    self.assertEqual(profile["boundary"], repair.boundary_manifest(shape))
                    self.assertEqual(profile["pcurves"], repair.pcurve_manifest(shape))
                    feature = shape_features(shape)
                    self.assertEqual(profile["features"]["solid_count"], feature["solid_count"])
                    self.assertEqual(profile["features"]["face_count"], feature["face_count"])
                    self.assertEqual(profile["features"]["bbox"], feature["bbox"])
                    self.assertAlmostEqual(profile["features"]["volume_mm3"], feature["volume_mm3"], places=9)

    def test_free_entry_above_base_still_passes_with_nominal_scope(self):
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                result = self.verdict(profile, (-1, -1, -2), (12, 14, 2), "-Z", 30)
                self.assertEqual(result["status"], "PASS", result)
                self.assertEqual(result["certification_scope"], "nominal_cad_interference")
                self.assertFalse(result["manufacturing_clearance_certified"])
                self.assertTrue(result["representation_precision_limited"])

    def test_point_two_mm_cover_is_a_real_obstacle(self):
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                result = self.verdict(profile, (-1, -1, 20), (12, 14, .2), "-Z", 30)
                self.assertEqual(result["status"], "COLLISION", result)
                self.assertGreater(result["common_volume_mm3"], 1e-5)

    def test_nominal_permission_does_not_certify_uncertainty_clearance(self):
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                result = self.verdict(profile, (-1, -1, -2), (12, 14, 2), "-Z", 30,
                                      {"mode": "uncertainty_clearance", "reference_mode": "assembled_reference"})
                self.assertEqual(result["status"], "UNKNOWN", result)
                self.assertEqual(result["reason"], "REPRESENTATION_CLEARANCE_UNRESOLVED")

    def test_point_two_mm_wall_is_not_skipped_between_positions(self):
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                result = self.verdict(profile, (20, -1, -1), (.2, 14, 16), "-X", 30)
                self.assertEqual(result["status"], "COLLISION", result)
                self.assertGreater(result["common_volume_mm3"], 1e-5)

    def test_entering_through_mainbody_is_blocked(self):
        for profile in self.profiles:
            with self.subTest(profile=profile["name"]):
                result = self.verdict(profile, (-5, -5, -20), (20, 22, 16), "+Z", 40)
                self.assertEqual(result["status"], "COLLISION", result)
                self.assertGreater(result["common_volume_mm3"], 1e-5)


if __name__ == "__main__":
    unittest.main()
