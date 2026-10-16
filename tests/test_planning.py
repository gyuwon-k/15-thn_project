import copy
import hashlib
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.geometry import shape_features,combine_bbox
from backend.planning import GeometrySession,make_path,generate_forward_plan as generate_plan,validate_plan
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder,BRepPrimAPI_MakeCone
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
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
        self.assertIsNone(result["steps"][0]["path"])
        self.assertFalse(result["steps"][0]["executable"])
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
        self.assertIsNone(result["steps"][0]["path"])
        self.assertEqual(result["steps"][0]["proposed_entry"]["direction"],"+X")
        self.assertFalse(result["steps"][0]["executable"])
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
    def test_reviewer_metadata_cannot_replace_automatic_geometry_proof(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,1))])
        model["quality"]={"exact_method_disagreement_ids":["occ_1"]}
        review={"model_fingerprint":model.get("fingerprint"),"acceptance_policy":"standard_occt_preserved","approved_by":"engineer","approval_reference":"review-001","occurrence_ids":["occ_1"]}
        result=generate_plan(model,{"directions":["-Z"],"distance_mm":10,"max_seconds":20,"geometry_review":review})
        self.assertEqual(result["status"],"UNKNOWN")
    def test_lost_source_member_cannot_be_hidden_by_present_member_coverage(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,1))])
        model["quality"]={"inventory_complete":False}
        result=generate_plan(model,{"max_seconds":10})
        self.assertEqual(result["status"],"BLOCKED")
        self.assertTrue(any(d["code"]=="MODEL_INVENTORY_INCOMPLETE" for d in result["diagnostics"]))
    def test_known_wall_crossing_never_gets_fallback_executable_path(self):
        model=box_model(self.temp.name,[((0,0,0),(1,5,5)),((5,1,1),(1,1,1)),((1,0,0),(9,5,1))],fixed=(0,2))
        result=generate_plan(model,{"directions":["+X"],"distance_mm":10,"max_seconds":20,"native_timeout_seconds":10})
        step=result["steps"][0]
        self.assertEqual(step["status"],"COLLISION",result)
        self.assertEqual(step["reason"],"ALL_ALLOWED_ENTRIES_COLLIDE")
        self.assertIsNone(step["path"])
        self.assertFalse(step["executable"])
        self.assertEqual(result["playback"]["executable_prefix_count"],0)
    def test_alternative_entry_is_actually_certified_after_wall_crossing(self):
        model=box_model(self.temp.name,[((0,0,0),(1,5,5)),((5,1,1),(1,1,1)),((1,0,0),(9,5,1))],fixed=(0,2))
        result=generate_plan(model,{"directions":["+X","-Z"],"distance_mm":10,"max_seconds":20,"native_timeout_seconds":10})
        self.assertEqual(result["status"],"PASS",result)
        self.assertEqual(result["steps"][0]["direction"],"-Z")
        self.assertTrue(result["steps"][0]["executable"])
    def test_unverified_predecessor_prevents_later_safe_step_playback(self):
        model=box_model(self.temp.name,[((0,0,0),(1,5,5)),((5,1,1),(1,1,1)),((8,8,1),(1,1,1)),((1,0,0),(9,10,1))],fixed=(0,3))
        plan={"steps":[{"id":"blocked","unit_id":"unit_1","name":"Blocked","direction":"+X","distance_mm":15},
            {"id":"safe","unit_id":"unit_2","name":"Safe later","direction":"-Z","distance_mm":15}]}
        result=validate_plan(model,plan,{"validation_seconds":20,"native_timeout_seconds":10})
        self.assertEqual(result["steps"][0]["status"],"COLLISION")
        self.assertEqual(result["steps"][1]["status"],"PASS")
        self.assertEqual(result["steps"][1]["planning_state"],"WAITING_PREDECESSOR")
        self.assertIsNone(result["steps"][1]["path"])
        self.assertFalse(result["steps"][1]["executable"])
        self.assertEqual(result["playback"]["executable_prefix_count"],0)
    def test_opening_direction_is_geometric_for_rotated_housing(self):
        model=box_model(self.temp.name,[((0,0,0),(10,10,10)),((4,4,1),(2,2,2))])
        outer=BRepPrimAPI_MakeBox(10,10,10).Shape()
        opening=BRepPrimAPI_MakeBox(gp_Pnt(1,1,1),10,8,8).Shape()
        housing=BRepAlgoAPI_Cut(outer,opening).Shape()
        BRepTools.Write_s(housing,model["occurrences"][0]["brep_path"])
        result=generate_plan(model,{"directions":["-Z","-X"],"distance_mm":20,"max_seconds":25,"native_timeout_seconds":10})
        self.assertEqual(result["status"],"PASS",result)
        self.assertEqual(result["steps"][0]["direction"],"-X")
        self.assertTrue(result["steps"][0]["executable"])
        with GeometrySession(model,timeout=10) as engine:
            wall=engine.path_pair('occ_1','occ_0',make_path('-Z',20),time.monotonic()+15)
        self.assertEqual(wall['status'],'COLLISION')
    def test_conservative_envelope_hit_does_not_reject_clear_cad_hole(self):
        model=box_model(self.temp.name,[((0,0,0),(1,1,10)),((-3,-3,4),(6,6,2)),((-3,-3,3),(6,6,1))],fixed=(0,2))
        obstacle=BRepPrimAPI_MakeCylinder(1,10).Shape()
        outer=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,0,4),gp_Dir(0,0,1)),3,2).Shape()
        inner=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,0,3),gp_Dir(0,0,1)),2,4).Shape()
        ring=BRepAlgoAPI_Cut(outer,inner).Shape()
        bearing_outer=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,0,3),gp_Dir(0,0,1)),3,1).Shape()
        bearing_inner=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,0,2),gp_Dir(0,0,1)),2,3).Shape()
        bearing=BRepAlgoAPI_Cut(bearing_outer,bearing_inner).Shape()
        for index,shape in enumerate((obstacle,ring,bearing)):
            BRepTools.Write_s(shape,model["occurrences"][index]["brep_path"])
            features=shape_features(shape);model["occurrences"][index].update(features);model["units"][index]["bbox"]=features["bbox"]
        model["bbox"]=combine_bbox([o["bbox"] for o in model["occurrences"]])
        result=generate_plan(model,{"directions":["-Z"],"distance_mm":20,"max_seconds":25,"native_timeout_seconds":10})
        self.assertEqual(result["status"],"PASS",result)
        self.assertEqual(result["steps"][0]["certificates"][0]["policy_segments"][0]["method"],"exact_monotone_analytic_boundary_sweep")
    def test_explicit_process_precedence_controls_otherwise_feasible_order(self):
        model=box_model(self.temp.name,[((0,0,0),(10,10,1)),((1,1,1),(1,1,1)),((5,5,1),(1,1,1))])
        result=generate_plan(model,{"precedence":[{"before":"unit_2","after":"unit_1"}],"directions":["-Z"],"distance_mm":15,"max_seconds":25})
        self.assertEqual(result["status"],"PASS",result)
        self.assertEqual([s["unit_id"] for s in result["steps"]],["unit_2","unit_1"])
    def test_representation_uncertainty_prevents_near_contact_pass(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1.005),(1,1,1))])
        for occurrence in model["occurrences"]:occurrence["representation_uncertainty_mm"]=.004
        result=generate_plan(model,{"directions":["-Z"],"distance_mm":10,"max_seconds":20,'collision_policy':{'mode':'uncertainty_clearance'}})
        self.assertNotEqual(result["status"],"PASS",result)
        self.assertFalse(result["steps"][0]["executable"])
        self.assertTrue(any(p.get("reason")=="REPRESENTATION_CLEARANCE_UNRESOLVED" for p in result["placement_audit"]["pairs"]))
    def test_representation_uncertainty_applies_to_entire_path(self):
        model=box_model(self.temp.name,[((4,1.005,0),(1,1,1)),((0,0,0),(1,1,1))])
        for occurrence in model["occurrences"]:occurrence["representation_uncertainty_mm"]=.004
        with GeometrySession(model,timeout=10,collision_policy={'mode':'uncertainty_clearance'}) as engine:
            result=engine.path_pair("occ_1","occ_0",make_path("-X",10),time.monotonic()+20)
        self.assertEqual(result["status"],"UNKNOWN",result)
        self.assertEqual(result["reason"],"REPRESENTATION_CLEARANCE_UNRESOLVED")
    def test_clearance_larger_than_representation_uncertainty_still_passes(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1.05),(1,1,1))])
        for occurrence in model["occurrences"]:occurrence["representation_uncertainty_mm"]=.004
        # This fixture measures clearance while held above a base, not support
        # of an assembled part. Keep that geometry claim at the path layer.
        with GeometrySession(model,timeout=10,collision_policy={'mode':'uncertainty_clearance'}) as engine:
            result=engine.path_pair('occ_1','occ_0',make_path('-Z',10),time.monotonic()+20)
        self.assertEqual(result["status"],"PASS",result)
    def test_process_group_placement_then_fixing_before_next_component(self):
        model=box_model(self.temp.name,[((0,0,0),(10,10,1)),((1,1,1),(1,1,1)),((3,3,1),(1,1,1)),((5,5,1),(1,1,1))])
        model['units'][1]['role']='plate';model['units'][2]['role']='relay';model['units'][3]['role']='fastener'
        process={'process_policy':{'require_fixing_before_next_component':True,'required_unit_ids':['unit_1']},'process_groups':[
            {'id':'fixture','target_unit_id':'unit_1','fastener_unit_ids':['unit_3'],'ordered_fastener_groups':[['unit_3']], 'status':'confirmed','basis':'fixture drawing'}]}
        result=generate_plan(model,{**process,'precedence':[{'before':'unit_1','after':'unit_2'}],'directions':['-Z'],'distance_mm':20,'max_seconds':25})
        self.assertEqual(result['status'],'PASS',result)
        self.assertEqual([s['unit_id'] for s in result['steps']],['unit_1','unit_3','unit_2'])
        wrong={**result,'steps':[result['steps'][0],result['steps'][2],result['steps'][1]]}
        validation=validate_plan(model,wrong,{**process,'directions':['-Z'],'distance_mm':20,'max_seconds':25})
        self.assertEqual(validation['status'],'BLOCKED')
        self.assertEqual(validation['steps'][1]['reason'],'PROCESS_FIXING_REQUIRED')
        self.assertFalse(validation['steps'][1]['executable'])
    def test_nominal_contact_requires_continuous_sweep_and_declares_scope(self):
        model=box_model(self.temp.name,[((0,0,0),(5,5,1)),((1,1,1),(1,1,1))])
        for occurrence in model['occurrences']:occurrence['representation_uncertainty_mm']=.004
        result=generate_plan(model,{'directions':['-Z'],'distance_mm':10,'max_seconds':20,'collision_policy':{'mode':'nominal_interference'}})
        self.assertEqual(result['status'],'PASS',result)
        self.assertEqual(result['steps'][0]['path']['certification_scope'],'nominal_cad_interference')
        self.assertFalse(result['manufacturing_tolerance_certified'])
        self.assertIn(result['steps'][0]['certificates'][0]['policy_segments'][0]['method'],{'exact_monotone_analytic_boundary_sweep','exact_conservative_translation_envelope'})
        wrong=validate_plan(model,{**result,'steps':[dict(result['steps'][0],direction='+Z')]},{'validation_seconds':20,'collision_policy':{'mode':'nominal_interference'}})
        self.assertEqual(wrong['status'],'BLOCKED')
        self.assertIsNone(wrong['steps'][0]['path'])
    def test_final_overlap_measured_after_audit_timeout_still_offers_unapproved_review(self):
        model=box_model(self.temp.name,[((0,0,0),(2,2,2)),((.5,.5,1.5),(1,1,1))])
        with patch('backend.planning._audit',return_value={'status':'UNKNOWN','pairs':[],'contact_policy_proposals':[]}):
            result=generate_plan(model,{'directions':['-Z','+Z'],'distance_mm':10,'max_seconds':20})
        self.assertEqual(len(result['placement_audit']['contact_policy_proposals']),1)
        proposal=result['placement_audit']['contact_policy_proposals'][0]
        self.assertAlmostEqual(proposal['measured_overlap_mm3'],.5,places=5)
        self.assertIsNone(proposal['approved_by']);self.assertIsNone(proposal['approval_reference'])
        self.assertFalse(proposal['applied']);self.assertFalse(result['steps'][0]['executable'])
        self.assertEqual(result['steps'][0]['reason'],'FINAL_PLACEMENT_OVERLAP')
        self.assertEqual(result['steps'][0]['status'],'COLLISION')
        self.assertEqual(result['search']['attempts'],1)


if __name__=="__main__":unittest.main()
