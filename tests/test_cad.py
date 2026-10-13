"""Synthetic CAD acceptance evidence; not labelled as company validation."""
import tempfile
import unittest
import json
from pathlib import Path

from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeFace
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Pnt, gp_Pln, gp_Dir
from OCP.TopoDS import TopoDS_Shape
from backend.cad import GeometryEngine, combine_bbox, import_step, shape_features, static_interference


def box(x=0, y=0, z=0, dx=10, dy=10, dz=10):
    return BRepPrimAPI_MakeBox(gp_Pnt(x, y, z), dx, dy, dz).Shape()


class CadTests(unittest.TestCase):
    def test_AT03_AT04_nested_repeated_placements_and_mesh(self):
        from OCP.TDocStd import TDocStd_Document
        from OCP.TCollection import TCollection_ExtendedString
        from OCP.XCAFDoc import XCAFDoc_DocumentTool
        from OCP.STEPCAFControl import STEPCAFControl_Writer
        from OCP.TopLoc import TopLoc_Location
        from OCP.gp import gp_Trsf, gp_Vec
        from OCP.Interface import Interface_Static
        from OCP.IFSelect import IFSelect_RetDone
        doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
        tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
        root = tool.NewShape()
        sub = tool.NewShape()
        leaf = tool.AddShape(box(dx=10, dy=20, dz=30), False)
        def loc(x, y, z):
            tr = gp_Trsf()
            tr.SetTranslation(gp_Vec(x, y, z))
            return TopLoc_Location(tr)
        tool.AddComponent(sub, leaf, loc(10, 0, 0))
        tool.AddComponent(sub, leaf, loc(30, 0, 0))
        tool.AddComponent(root, sub, loc(0, 100, 0))
        tool.UpdateAssemblies()
        with tempfile.TemporaryDirectory() as tmp:
            for unit in ("MM", "INCH"):
                writer = STEPCAFControl_Writer()
                Interface_Static.SetCVal_s("write.step.unit", unit)
                path = Path(tmp) / f"중첩 반복 {unit}.step"
                self.assertTrue(writer.Transfer(doc))
                self.assertEqual(writer.Write(str(path)), IFSelect_RetDone)
                result = import_step(path, Path(tmp)/"cache")
                self.assertEqual(result["status"], "ready")
                self.assertEqual(len(result["instances"]), 2)
                self.assertEqual(len(result["definitions"]), 1)
                xs = sorted(i["bbox"]["min"][0] for i in result["instances"])
                for actual, expected in zip(xs, [10, 30]):
                    self.assertAlmostEqual(actual, expected, places=5)
                for instance in result["instances"]:
                    self.assertAlmostEqual(instance["bbox"]["min"][1], 100, places=5)
                    self.assertAlmostEqual(instance["volume_mm3"], 6000, places=4)
                scene = json.loads(Path(result["scene_path"]).read_text(encoding="utf-8"))
                self.assertEqual(len(scene["meshes"]), 2)
                for mesh in scene["meshes"]:
                    self.assertAlmostEqual(min(mesh["positions"][1::3]), 100, places=5)
                ids = {i["id"] for i in result["instances"]}
                reopened = import_step(path, Path(tmp)/"cache")
                self.assertTrue(reopened["cache_hit"])
                self.assertEqual(ids, {i["id"] for i in reopened["instances"]})
        Interface_Static.SetCVal_s("write.step.unit", "MM")

    def test_AT09_dimensions_volume_and_open_shell(self):
        features = shape_features(box(dx=10, dy=20, dz=30))
        self.assertEqual(features["solid_count"], 1)
        self.assertAlmostEqual(features["volume_mm3"], 6000, places=5)
        for actual, expected in zip(features["bbox"]["size"], [10, 20, 30]):
            self.assertAlmostEqual(actual, expected, places=5)
        face = BRepBuilderAPI_MakeFace(gp_Pln(gp_Pnt(0, 0, 0), gp_Dir(0, 0, 1)), 0, 10, 0, 10).Face()
        self.assertIsNone(shape_features(face)["volume_mm3"])

    def test_AT11_separated_contact_penetration_failure(self):
        a = box()
        self.assertEqual(static_interference(a, box(x=20))["status"], "separated")
        self.assertEqual(static_interference(a, box(x=10))["status"], "contact")
        collision = static_interference(a, box(x=5))
        self.assertEqual(collision["status"], "collision")
        self.assertAlmostEqual(collision["common_volume_mm3"], 500, places=5)
        self.assertEqual(static_interference(a, TopoDS_Shape())["status"], "calculation_failed")

    def test_AT12_middle_thin_obstacle_future_and_self(self):
        with tempfile.TemporaryDirectory() as tmp:
            shapes = {"moving": box(dx=1, dy=1, dz=1), "thin_wall": box(x=10.015, dx=.025, dy=1, dz=1), "future": box(x=0, dx=1, dy=1, dz=1)}
            instances = []
            for key, shape in shapes.items():
                path = Path(tmp) / (key+".brep")
                BRepTools.Write_s(shape, str(path))
                instances.append({"id": key, "brep_path": str(path), **shape_features(shape)})
            engine = GeometryEngine({"instances": instances, "bbox": combine_bbox([i["bbox"] for i in instances])})
            result = engine.check_path(["moving"], ["thin_wall", "moving"], direction="+X", distance_mm=20, sample_spacing_mm=5, timeout_s=30)
            self.assertEqual(result["status"], "collision")
            self.assertGreater(result["collisions"][0]["path_fraction"], 0)
            self.assertLess(result["collisions"][0]["path_fraction"], 1)
            self.assertEqual(result["collisions"][0]["obstacle_id"], "thin_wall")
            unobstructed = engine.check_path(["moving"], ["moving"], direction="+X", distance_mm=20)
            self.assertEqual(unobstructed["status"], "passed")
            self.assertNotIn("future", unobstructed["conditions"]["assembled_ids"])
            directions = engine.find_directions(["moving"], ["thin_wall"], distance_mm=20, sample_spacing_mm=5)
            self.assertIn("-X", directions["available_directions"])
            self.assertNotIn("+X", directions["available_directions"])

    def test_AT02_empty_and_nonstep_are_diagnosed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "빈 파일.STP"
            path.write_text("", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "CAD_INVALID_STEP"):
                import_step(path, Path(tmp) / "cache")
            path.write_text("not STEP", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "CAD_INVALID_STEP"):
                import_step(path, Path(tmp) / "cache")


if __name__ == "__main__":
    unittest.main()
