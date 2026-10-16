import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from backend import domain
from backend.planning import generate_plan,validate_plan,make_path
from backend.disassembly import reverse_path,extraction_distance
from backend.planning_settings import settings_errors,entry_settings,compatible_rules
from backend.main import Services
from tests.test_planning import box_model


class ReversePlanningTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'work/reverse-tests';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root)
        self.model=box_model(self.temp.name,[((0,0,0),(10,10,1)),((2,2,1),(2,2,2)),((6,2,1),(2,2,2))])
    def tearDown(self):self.temp.cleanup()
    def test_native_removal_reversal_precedence_and_full_assembly_validation(self):
        rules={'directions':['-Z'],'max_seconds':30,'precedence':[{'before':'unit_1','after':'unit_2'}]}
        plan=generate_plan(self.model,rules)
        self.assertEqual(plan['status'],'PASS',plan)
        self.assertEqual([s['unit_id'] for s in plan['disassembly']['removal_steps']],['unit_2','unit_1'])
        self.assertEqual([s['unit_id'] for s in plan['steps']],['unit_1','unit_2'])
        for step in plan['steps']:self.assertEqual(reverse_path(step['removal_path']),step['path'])
        self.assertEqual(validate_plan(self.model,plan,rules)['status'],'PASS')
        self.assertEqual(plan['decision_trace'],[])
        self.assertFalse(plan['learning_eligibility']['eligible'])
    def test_all_allowed_directions_are_compared_and_preference_wins(self):
        rules={'directions':['-Z','-X'],'max_seconds':30,'entry_conditions':{
            'unit_1':{'allowed_directions':['-X','-Z'],'preferred_direction':'-Z'},
            'unit_2':{'allowed_directions':['-X','-Z'],'preferred_direction':'-Z'}}}
        plan=generate_plan(self.model,rules)
        self.assertEqual(plan['status'],'PASS',plan)
        self.assertEqual({s['direction'] for s in plan['steps']},{'-Z'})
        checked={r['direction'] for r in plan['attempt_trace'] if r['unit_id']=='unit_1'}
        self.assertEqual(checked,{'-X','-Z'})
    def test_short_distance_no_draft_and_hidden_obstacle_retained(self):
        self.model['units'][0]['hidden']=True
        plan=generate_plan(self.model,{'directions':['+Z'],'distance_mm':.1,'max_seconds':15})
        self.assertFalse(plan['replace_plan']);self.assertEqual(plan['steps'],[])
        self.assertTrue(plan['disassembly']['remaining_unit_ids'])
        self.assertTrue(any(a['reason']=='START_NOT_OUTSIDE_PRODUCT' for a in plan['attempt_trace']))
        self.assertTrue(all('unit_0' in a['obstacle_unit_ids'] for a in plan['attempt_trace']))
    def test_final_overlap_stops_search_and_never_creates_plan(self):
        model=box_model(self.temp.name,[((0,0,0),(2,2,2)),((1,1,1),(2,2,2))])
        plan=generate_plan(model,{'max_seconds':20})
        self.assertEqual(plan['search']['termination'],'final_placement_collision')
        self.assertFalse(plan['replace_plan']);self.assertEqual(plan['attempt_trace'],[])
    def test_zero_targets_are_valid_not_search_failure(self):
        rules={'fixed_unit_ids':[u['id'] for u in self.model['units']],'max_seconds':20}
        plan=generate_plan(self.model,rules)
        self.assertEqual(plan['status'],'PASS');self.assertTrue(plan['replace_plan'])
        self.assertEqual(validate_plan(self.model,plan,rules)['status'],'PASS')
    def test_native_timeout_and_invalid_geometry_remain_unknown(self):
        self.model['units'][1]['collision_ready']=False
        plan=generate_plan(self.model,{'directions':['-Z'],'max_seconds':15})
        self.assertFalse(plan['replace_plan']);self.assertNotEqual(plan['status'],'PASS')
    def test_final_local_contact_policy_survives_reversal(self):
        model=box_model(self.temp.name,[((0,0,0),(2,2,1)),((.5,.5,.9),(1,1,1))])
        rules={'directions':['-Z'],'max_seconds':25,'contact_policies':[{
            'id':'contact','unit_a':'unit_0','unit_b':'unit_1','approved_by':'engineer','approval_reference':'test-only',
            'terminal_distance_mm':.2,'region_bbox':{'min':[-.1,-.1,.85],'max':[2.1,2.1,1.05]}}]}
        plan=generate_plan(model,rules)
        self.assertEqual(plan['status'],'PASS',plan)
        self.assertEqual(validate_plan(model,plan,rules)['status'],'PASS')
    def test_reverse_fixing_groups_are_contiguous_in_assembly(self):
        rules={'directions':['-Z'],'max_seconds':30,'process_policy':{'require_fixing_before_next_component':True,'required_unit_ids':['unit_1']},
            'process_groups':[{'target_unit_id':'unit_1','fastener_unit_ids':['unit_2'],'status':'confirmed','basis':'test fixture','fixation_mode':'separate_fasteners'}]}
        plan=generate_plan(self.model,rules)
        self.assertEqual(plan['status'],'PASS',plan)
        self.assertEqual([s['unit_id'] for s in plan['steps']],['unit_1','unit_2'])
        self.assertEqual(validate_plan(self.model,plan,rules)['status'],'PASS')
    def test_cancellation_propagates(self):
        with self.assertRaises(InterruptedError):generate_plan(self.model,{'max_seconds':10},cancel=lambda:True)
    def test_initial_precedence_conflict_rejected(self):
        plan=generate_plan(self.model,{'fixed_unit_ids':['unit_2'],'precedence':[{'before':'unit_1','after':'unit_2'}]})
        self.assertFalse(plan['replace_plan'])
        self.assertIn('INITIAL_STATE_RULE_CONFLICT',[x['code'] for x in plan['diagnostics']])
    def test_settings_empty_is_not_missing_and_legacy_single_direction_preserved(self):
        self.assertEqual(entry_settings({'directions':['-Z'],'entry_conditions':{'unit_1':{'direction':'-Z'}}},'unit_1'),(['-Z'],None,'legacy_single_direction'))
        for rules in ({'entry_conditions':{'unit_1':{'allowed_directions':[]}}},{'directions':['-Z'],'entry_conditions':{'unit_1':{'direction':'+Z'}}},{'beam_width':9},{'beam_width':2.5},{'extraction_margin_mm':0}):
            self.assertTrue(settings_errors(self.model,rules),rules)
        original={'beam_width':32};converted,notes=compatible_rules(original)
        self.assertEqual(original['beam_width'],32);self.assertEqual(converted['beam_width'],8);self.assertTrue(notes)
    def test_all_axes_auto_distance_start_outside_original_envelope(self):
        from backend.planning import _separated,_offset_box
        u=self.model['units'][1]
        for d in ['+X','-X','+Y','-Y','+Z','-Z']:
            distance,required,source=extraction_distance(self.model,u,d,{})
            self.assertTrue(_separated(_offset_box(u['bbox'],make_path(d,distance)['start_translation_mm']),self.model['bbox']))
            self.assertEqual(source,'axis_envelope_plus_margin')
    def test_manual_direction_cannot_override_allowed_list(self):
        plan={'steps':[{'id':'s1','name':'a','unit_id':'unit_1','direction':'+Z','distance_mm':20},
                       {'id':'s2','name':'b','unit_id':'unit_2','direction':'-Z','distance_mm':20}]}
        v=validate_plan(self.model,plan,{'directions':['-Z'],'max_seconds':20})
        self.assertEqual(v['steps'][0]['reason'],'DIRECTION_CONSTRAINT_CONFLICT');self.assertNotEqual(v['status'],'PASS')
    def test_reused_route_proofs_follow_monotone_obstacle_sets(self):
        # A fixed floor supports the test blocks; the wall still blocks +X.
        model=box_model(self.temp.name,[((0,0,0),(1,10,10)),((3,2,2),(1,1,1)),((6,2,2),(1,1,1)),((8,2,2),(1,1,1)),((1,0,1),(9,10,1))],fixed=(0,4))
        plan=generate_plan(model,{'directions':['+X','-Z'],'max_seconds':25})
        self.assertEqual(plan['status'],'PASS',plan)
        bases={r.get('cache_basis') for r in plan['attempt_trace']}
        self.assertIn('subset_of_verified_obstacles',bases)
        self.assertIn('still_occupied_proven_blocker',bases)
        self.assertEqual(validate_plan(model,plan,{'directions':['+X','-Z'],'max_seconds':25})['status'],'PASS')
    def test_compound_unit_moves_every_member_together(self):
        model=copy.deepcopy(self.model)
        model['units'][1]['occurrence_ids']+=model['units'][2]['occurrence_ids']
        from backend.geometry import combine_bbox
        model['units'][1]['bbox']=combine_bbox([model['units'][1]['bbox'],model['units'][2]['bbox']]);model['units'].pop()
        plan=generate_plan(model,{'directions':['-Z'],'max_seconds':25})
        self.assertEqual(plan['status'],'PASS',plan);self.assertEqual(len(plan['steps']),1)
        self.assertEqual({c['moving_occurrence_id'] for c in plan['steps'][0]['certificates']},{'occ_1','occ_2'})
    def test_incomplete_publication_preserves_existing_plan_and_validation(self):
        service=Services.__new__(Services);service.lock=threading.RLock();service.root=Path(self.temp.name)
        project=domain.new_project({'name':'existing'});project.update(model=self.model,row_version=1)
        project['plan']={'id':'existing','steps':[{'id':'old','unit_id':'unit_1','torque':4}],'status':'DRAFT'}
        old_plan=copy.deepcopy(project['plan']);old_validation=copy.deepcopy(project['validation'])
        service.store=Mock();service.store.get_project.return_value=project;service.store.save_project.side_effect=lambda state,*args:state
        service.release={'build_id':'test'};service.preserve_search_trace=Mock()
        job={'project_id':project['id'],'kind':'generate','id':'job','geometry_hash':domain.geometry_hash(project)}
        result=service.apply_result(job,{'plan':{'replace_plan':False,'kind':'incomplete_disassembly','steps':[],
            'disassembly':{'removed_count':1},'search':{'termination':'budget_reached'}},'validation':{'status':'UNKNOWN'}})
        saved=service.store.save_project.call_args.args[0]
        self.assertEqual(saved['plan'],old_plan);self.assertEqual(saved['validation'],old_validation)
        self.assertEqual(result['status'],'INCOMPLETE');self.assertIn('generation_report',saved)


if __name__=='__main__':unittest.main()
