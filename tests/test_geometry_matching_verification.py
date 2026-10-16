import json
import hashlib
import math
import tempfile
import unittest
from pathlib import Path

import numpy as np
from OCP.BRep import BRep_Builder
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeSphere
from OCP.BRepTools import BRepTools
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Compound
from OCP.TopAbs import TopAbs_VERTEX
from OCP.TopExp import TopExp_Explorer
from OCP.gp import gp_Ax1, gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs

from backend.geometry_matching import compare_breps, group_alignment_audit


def compound(parts):
    builder=BRep_Builder(); shape=TopoDS_Compound(); builder.MakeCompound(shape)
    for part in parts: builder.Add(shape,part)
    return shape


class GeometryVerificationTests(unittest.TestCase):
    def compare(self,a,b):
        parent=Path(__file__).resolve().parents[1]/"work"/"matching-test-temp"
        parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            left,right=Path(temporary)/"a.brep",Path(temporary)/"b.brep"
            BRepTools.Write_s(a,str(left)); BRepTools.Write_s(b,str(right))
            return compare_breps(left,right)

    def test_true_rigid_placement_uses_actual_solid_differences(self):
        original=BRepPrimAPI_MakeBox(3,5,7).Shape()
        tr=gp_Trsf();tr.SetRotation(gp_Ax1(gp_Pnt(0,0,0),gp_Dir(1,2,3)),.47)
        tr.SetTranslationPart(gp_Vec(38,-22,9))
        moved=original.Moved(TopLoc_Location(tr))
        result=self.compare(original,moved)
        self.assertEqual(result["status"],"PASS",result)
        self.assertEqual(result["comparison"]["source_minus_target"]["remaining_faces"],0)
        self.assertEqual(result["comparison"]["target_minus_source"]["remaining_faces"],0)
        self.assertAlmostEqual(result["proper_rotation_determinant"],1)

    def test_baked_rigid_rotation_can_be_aligned_without_same_cad_frame(self):
        original=BRepPrimAPI_MakeBox(3,5,7).Shape()
        tr=gp_Trsf();tr.SetRotation(gp_Ax1(gp_Pnt(0,0,0),gp_Dir(1,2,3)),.47)
        tr.SetTranslationPart(gp_Vec(38,-22,9))
        baked=BRepBuilderAPI_Transform(original,tr,True).Shape()
        self.assertEqual(self.compare(original,baked)["status"],"PASS")

    def test_equal_volume_different_geometry_never_passes(self):
        original=BRepPrimAPI_MakeBox(2,3,4).Shape()
        different=BRepPrimAPI_MakeBox(1,4,6).Shape()
        self.assertNotEqual(self.compare(original,different)["status"],"PASS")

    def test_candidate_boxes_respect_declared_cad_precision_without_changing_boolean_fuzzy(self):
        original=BRepPrimAPI_MakeBox(2,3,4).Shape()
        exported=BRepPrimAPI_MakeBox(2.002,3,4).Shape()
        # At normal CAD precision these are different boundaries.
        self.assertNotEqual(self.compare(original,exported)["status"],"PASS")
        builder=BRep_Builder()
        vertex_cast=getattr(TopoDS,"Vertex_s",None) or TopoDS.Vertex
        for shape in (original,exported):
            vertices=TopExp_Explorer(shape,TopAbs_VERTEX)
            while vertices.More():
                builder.UpdateVertex(vertex_cast(vertices.Current()),.004);vertices.Next()
        # Imported precision may encompass the export difference. Actual two-
        # sided Boolean evidence is still required and its precision disclosed.
        result=self.compare(original,exported)
        self.assertEqual(result["status"],"PASS",result)
        self.assertGreater(result["comparison"]["bbox_error_mm"],1e-4)
        self.assertEqual(result["kernel_input_tolerance_mm"],.004)
        self.assertEqual(result["bbox_candidate_tolerance_mm"],.008)
        self.assertEqual(result["policy"]["boolean_fuzzy_mm"],1e-7)
        for pair in result["comparison"]["verified_solid_pairs"]:
            self.assertTrue(pair["source_minus_target"]["empty_material_boundary"])
            self.assertTrue(pair["target_minus_source"]["empty_material_boundary"])
        self.assertNotEqual(self.compare(original,BRepPrimAPI_MakeBox(2.02,3,4).Shape())["status"],"PASS")

    def test_mirrored_chiral_geometry_never_passes(self):
        entries=[((0,0,0),1),((8,0,0),1.2),((0,9,0),1.4),((0,0,10),1.6)]
        original=compound([BRepPrimAPI_MakeSphere(gp_Pnt(*p),r).Shape() for p,r in entries])
        mirrored=compound([BRepPrimAPI_MakeSphere(gp_Pnt(-p[0],p[1],p[2]),r).Shape() for p,r in entries])
        self.assertNotEqual(self.compare(original,mirrored)["status"],"PASS")

    def test_mirrored_location_never_claims_proper_rigid_equivalence(self):
        entries=[((0,0,0),1),((8,0,0),1.2),((0,9,0),1.4),((0,0,10),1.6)]
        original=compound([BRepPrimAPI_MakeSphere(gp_Pnt(*p),r).Shape() for p,r in entries])
        transform=gp_Trsf();transform.SetMirror(gp_Ax2(gp_Pnt(0,0,0),gp_Dir(1,0,0)))
        reflected=original.Moved(TopLoc_Location(transform))
        self.assertNotEqual(self.compare(original,reflected)["status"],"PASS")

    def test_scaled_location_never_claims_rigid_equivalence(self):
        original=BRepPrimAPI_MakeBox(2,3,4).Shape()
        transform=gp_Trsf();transform.SetScale(gp_Pnt(0,0,0),2.)
        scaled=original.Moved(TopLoc_Location(transform))
        self.assertNotEqual(self.compare(original,scaled)["status"],"PASS")

    def test_duplicate_solids_cannot_disappear_into_same_union(self):
        box=BRepPrimAPI_MakeBox(2,3,4).Shape()
        duplicate=compound([box,box])
        result=self.compare(duplicate,box)
        self.assertEqual(result["status"],"DIFFERENT")
        self.assertEqual(result["reason"],"SOLID_MULTIPLICITY_DIFFERS")

    def test_group_member_shapes_do_not_override_inconsistent_placement(self):
        source={"bbox":{"min":[0,0,0],"max":[1,1,1]}}
        transform=np.eye(4);bad=transform.copy();bad[0,3]=5
        pairs=[(source,source,{"status":"PASS","source_to_target_world":tr.reshape(-1).tolist()}) for tr in (transform,bad)]
        self.assertEqual(group_alignment_audit(pairs)["status"],"REVIEW")

    def test_manual_recheck_reads_extensionless_asset_and_retains_registered_name(self):
        from backend.geometry import shape_features,verify_unit_mappings
        parent=Path(__file__).resolve().parents[1]/"work"/"matching-test-temp"
        parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            directory=Path(temporary)
            original=BRepPrimAPI_MakeBox(2,3,4).Shape()
            writer=STEPControl_Writer();writer.Transfer(original,STEPControl_AsIs)
            source_file=directory/"original.step";writer.Write(str(source_file))
            content=source_file.read_bytes();sha=hashlib.sha256(content).hexdigest()
            managed=directory/sha;managed.write_bytes(content)
            placed=directory/"placed.brep";BRepTools.Write_s(original,str(placed))
            occurrence={"id":"member-1","name":"registered-part","definition_name":"registered-part","definition_id":"definition-1",
                "brep_path":str(placed),"collision_ready":True,**shape_features(original)}
            model={"part_sources":[{"name":"registered-part.STP","path":str(managed),"sha256":sha}],"occurrences":[occurrence],
                "units":[{"id":"preserved-unit-id","occurrence_ids":["member-1"],"matching":{"template_name":"registered-part.STP"}}]}
            result=verify_unit_mappings(model,directory/"cache")
            self.assertTrue(result["matching_ready"],result)
            checked=result["unit_results"][0]
            self.assertEqual(checked["unit_id"],"preserved-unit-id")
            self.assertEqual(checked["matching"]["template_name"],"registered-part.STP")
            self.assertEqual(checked["matching"]["geometry_verification"]["status"],"PASS")
            # A former PASS object cannot certify a manually changed member.
            other=BRepPrimAPI_MakeBox(1,4,6).Shape();BRepTools.Write_s(other,str(placed))
            model["occurrences"][0].update(shape_features(other))
            model["units"][0]["matching"]=checked["matching"]
            failed=verify_unit_mappings(model,directory/"cache")
            self.assertFalse(failed["matching_ready"],failed)
            self.assertEqual(failed["unit_results"][0]["matching"]["geometry_verification"]["status"],"REVIEW")

    def test_recheck_preserves_explicit_units_without_template_and_does_not_certify_forged_proof(self):
        from backend.geometry import verify_unit_mappings
        parent=Path(__file__).resolve().parents[1]/"work"/"matching-test-temp"
        parent.mkdir(parents=True,exist_ok=True)
        model={"part_sources":[],"occurrences":[{"id":"member"}],
            "units":[{"id":"explicit","occurrence_ids":["member"],"matching":{
                "status":"manual","method":"explicit_user_unit","review_required":False,"geometry_verification":{"status":"PASS"}}}]}
        with tempfile.TemporaryDirectory(dir=parent) as temporary:
            result=verify_unit_mappings(model,temporary)
            self.assertTrue(result["matching_ready"])
            self.assertEqual(result["unit_results"][0]["unit_id"],"explicit")
            self.assertEqual(result["unit_results"][0]["matching"]["method"],"explicit_user_unit")
            self.assertEqual(result["unit_results"][0]["matching"]["geometry_verification"]["status"],"NOT_APPLICABLE")
            model["units"][0]["matching"]["review_required"]=True
            self.assertFalse(verify_unit_mappings(model,temporary)["matching_ready"])
            for scope in ([],["missing"],["explicit","explicit"],"explicit"):
                with self.assertRaisesRegex(ValueError,"MATCH_INVALID_UNIT_SCOPE"):
                    verify_unit_mappings(model,temporary,unit_ids=scope)


if __name__=="__main__":unittest.main()
