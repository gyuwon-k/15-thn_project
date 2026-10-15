import copy
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.geometry import shape_features,combine_bbox
from backend.planning import GeometrySession,make_path,generate_plan,validate_plan
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder,BRepPrimAPI_MakeCone
from OCP.BRepTools import BRepTools
from OCP.gp import gp_Pnt,gp_Ax2,gp_Dir


def box_model(directory,boxes,fixed=(0,)):
    occurrences=[];units=[]
    for index,(position,size) in enumerate(boxes):
        shape=BRepPrimAPI_MakeBox(gp_Pnt(*position),*size).Shape()
        ident=f"occ_{index}";path=Path(directory)/(ident+".brep")
        BRepTools.Write_s(shape,str(path))
        feature=shape_features(shape)
        occurrences.append({"id":ident,"name":ident,"brep_path":str(path),"collision_ready":True,**feature})
        units.append({"id":f"unit_{index}","name":f"Part {index}","occurrence_ids":[ident],"bbox":feature["bbox"],"fixed":index in fixed,"role":"housing" if index in fixed else "component","collision_ready":True})
    return {"fingerprint":hashlib.sha256(str(boxes).encode()).hexdigest(),"occurrences":occurrences,"units":units,"bbox":combine_bbox([o["bbox"] for o in occurrences])}


class PlanningTests(unittest.TestCase):
    def setUp(self):
        directory=Path(__file__).resolve().parents[1]/".test-cache"/"synthetic"
        directory.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=directory)
    def tearDown(self):self.temp.cleanup()
    def test_thin_obstruction_cannot_be_missed_between_samples(self):
        model=box_model(self.temp.name,[((0,0,0),(.01,1,1)),((4.501,-.2,-.2),(.001,1.4,1.4))],fixed=(1,))
        with GeometrySession(model,timeout=10) as engine:
            result=engine.path_pair("occ_0","occ_1",make_path("-X",10),time.monotonic()+20)
        self.assertEqual(result["status"],"COLLISION",result)
        self.assertEqual(result["reason"],"SWEPT_VOLUME_PENETRATION")
    def test_planar_contact_docking_has_complete_sweep_certificate(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,1))])
        result=generate_plan(model,{"directions":["-Z"],"distance_mm":10,"max_seconds":20,"native_timeout_seconds":10})
        self.assertEqual(result["status"],"PASS",result)
        self.assertTrue(result["coverage"]["complete"])
        self.assertTrue(result["steps"][0]["path"]["verified"])
    def test_invalid_geometry_never_becomes_pass(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,1))])
        model["occurrences"][1]["collision_ready"]=False;model["units"][1]["collision_ready"]=False
        result=generate_plan(model,{"directions":["-Z"],"max_seconds":5})
        self.assertEqual(result["status"],"UNKNOWN")
        self.assertTrue(result["coverage"]["complete"])
        self.assertFalse(result["steps"][0]["path"]["verified"])
    def test_incomplete_plan_is_rejected(self):
        model=box_model(self.temp.name,[((0,0,0),(1,1,1)),((3,0,0),(1,1,1))])
        result=validate_plan(model,{"steps":[]},{"max_seconds":5})
        self.assertEqual(result["status"],"BLOCKED")
        self.assertFalse(result["coverage"]["complete"])
    def test_hidden_initial_obstacle_is_still_checked(self):
        model=box_model(self.temp.name,[((0,0,0),(1,1,1)),((5,0,0),(1,1,1))])
        model["units"][0]["hidden"]=True
        plan={"steps":[{"id":"step1","unit_id":"unit_1","name":"Part 1","direction":"+X","distance_mm":10,"path":make_path("+X",10)}]}
        result=validate_plan(model,plan,{"max_seconds":20,"native_timeout_seconds":10})
        self.assertEqual(result["status"],"BLOCKED",result)
        self.assertEqual(result["steps"][0]["status"],"COLLISION")
    def test_manual_direction_rebuilds_previous_path(self):
        model=box_model(self.temp.name,[((0,0,0),(1,1,1)),((5,0,0),(1,1,1))])
        plan={"steps":[{"id":"step1","unit_id":"unit_1","name":"Part 1","direction":"+X","distance_mm":10,"path":make_path("-X",10)}]}
        result=validate_plan(model,plan,{"max_seconds":20,"native_timeout_seconds":10})
        self.assertEqual(result["status"],"BLOCKED")
        self.assertEqual(result["steps"][0]["path"]["start_translation_mm"],[-10.,0.,0.])
    def test_initial_fixed_pair_overlap_is_audited(self):
        model=box_model(self.temp.name,[((0,0,0),(2,2,2)),((1,1,1),(2,2,2))],fixed=(0,1))
        result=validate_plan(model,{"steps":[]},{"max_seconds":20,"native_timeout_seconds":10})
        self.assertEqual(result["status"],"BLOCKED",result)
        self.assertTrue(result["placement_audit"]["pairs"][0]["initial_pair"])
    def test_plan_with_nonphysical_display_hide_is_still_full_coverage(self):
        model=box_model(self.temp.name,[((0,0,0),(1,1,1)),((5,0,0),(1,1,1))])
        model["units"][1]["hidden"]=True
        result=generate_plan(model,{"directions":["-X"],"max_seconds":10,"distance_mm":10})
        self.assertEqual(len(result["steps"]),1)
        self.assertEqual(result["steps"][0]["unit_id"],"unit_1")
    def test_approved_contact_is_spatially_and_terminally_limited(self):
        model=box_model(self.temp.name,[((0,0,0),(2,2,1)),((.5,.5,.9),(1,1,1))])
        policy={"id":"seal-1","unit_a":"unit_0","unit_b":"unit_1","approved_by":"test engineer","approval_reference":"fixture-test-001","terminal_distance_mm":.2,
            "region_bbox":{"min":[-.1,-.1,.85],"max":[2.1,2.1,1.05]}}
        result=generate_plan(model,{"directions":["-Z"],"distance_mm":10,"max_seconds":20,"native_timeout_seconds":10,"contact_policies":[policy]})
        self.assertEqual(result["status"],"PASS",result)
        self.assertTrue(result["steps"][0]["path"]["verified"])
    def test_contact_mask_cannot_waive_new_overlap_outside_region(self):
        model=box_model(self.temp.name,[((0,0,0),(2,2,1)),((.5,.5,.9),(1,1,1))])
        policy={"id":"bad-region","unit_a":"unit_0","unit_b":"unit_1","approved_by":"test engineer","approval_reference":"fixture-test-002","terminal_distance_mm":.2,
            "region_bbox":{"min":[-.1,-.1,.85],"max":[.6,2.1,1.05]}}
        result=validate_plan(model,{"steps":[{"id":"s1","unit_id":"unit_1","name":"Part","direction":"-Z","distance_mm":10,"path":make_path("-Z",10)}]},
            {"max_seconds":20,"native_timeout_seconds":10,"contact_policies":[policy]})
        self.assertEqual(result["status"],"BLOCKED",result)
    def test_unapproved_contact_is_rejected(self):
        model=box_model(self.temp.name,[((0,0,0),(2,2,1)),((.5,.5,.9),(1,1,1))])
        result=generate_plan(model,{"contact_policies":[{"unit_a":"unit_0","unit_b":"unit_1","approved":True}]})
        self.assertEqual(result["status"],"BLOCKED")
        self.assertEqual(result["diagnostics"][0]["code"],"INVALID_CONTACT_POLICY")
    def test_cylindrical_contact_docking_is_continuously_certified(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,2))])
        shape=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(2,2,1),gp_Dir(0,0,1)),.5,2).Shape()
        BRepTools.Write_s(shape,model["occurrences"][1]["brep_path"])
        features=shape_features(shape);model["occurrences"][1].update(features);model["units"][1]["bbox"]=features["bbox"]
        result=generate_plan(model,{"directions":["-Z"],"distance_mm":10,"max_seconds":20,"native_timeout_seconds":10})
        self.assertEqual(result["status"],"PASS",result)
    def test_conical_contact_docking_uses_monotone_surface_sweep(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,2))])
        shape=BRepPrimAPI_MakeCone(gp_Ax2(gp_Pnt(2,2,1),gp_Dir(0,0,1)),.8,.4,2).Shape()
        BRepTools.Write_s(shape,model["occurrences"][1]["brep_path"])
        features=shape_features(shape);model["occurrences"][1].update(features);model["units"][1]["bbox"]=features["bbox"]
        result=generate_plan(model,{"directions":["-Z"],"distance_mm":10,"max_seconds":20,"native_timeout_seconds":10})
        self.assertEqual(result["status"],"PASS",result)
    def test_exact_method_disagreement_cannot_silently_confirm(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,1))])
        model["quality"]={"exact_method_disagreement_ids":["occ_1"]}
        result=generate_plan(model,{"directions":["-Z"],"distance_mm":10,"max_seconds":20,"native_timeout_seconds":10})
        self.assertEqual(result["steps"][0]["status"],"PASS")
        self.assertEqual(result["status"],"UNKNOWN")
        self.assertTrue(any(d["code"]=="GEOMETRY_EXACT_METHOD_REVIEW_REQUIRED" for d in result["diagnostics"]))
    def test_stale_geometry_review_is_rejected(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,1))])
        model["quality"]={"exact_method_disagreement_ids":["occ_1"]}
        review={"model_fingerprint":"old-input","acceptance_policy":"standard_occt_preserved","approved_by":"engineer","approval_reference":"review-001","occurrence_ids":["occ_1"]}
        result=generate_plan(model,{"directions":["-Z"],"distance_mm":10,"max_seconds":20,"geometry_review":review})
        self.assertEqual(result["status"],"UNKNOWN")
    def test_lost_source_member_cannot_be_hidden_by_present_member_coverage(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,1))])
        model["quality"]={"inventory_complete":False}
        result=generate_plan(model,{"max_seconds":10})
        self.assertEqual(result["status"],"BLOCKED")
        self.assertTrue(any(d["code"]=="MODEL_INVENTORY_INCOMPLETE" for d in result["diagnostics"]))


if __name__=="__main__":unittest.main()
