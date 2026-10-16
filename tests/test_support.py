import tempfile
import time
import unittest
from pathlib import Path

from backend.process_rules import compile_process_rules
from backend.support import (native_support_pair, prepare_support, infer_support_process, support_eligibility,
                             support_removal, support_state, support_summary)
from tests.test_planning import box_model


class DirectGeometry:
    """Use real OCC solids for support regressions without process startup."""
    def __init__(self, model):
        from OCP.BRep import BRep_Builder
        from OCP.BRepTools import BRepTools
        from OCP.TopoDS import TopoDS_Shape
        self.shapes = {}
        for row in model['occurrences']:
            shape = TopoDS_Shape()
            BRepTools.Read_s(shape, row['brep_path'], BRep_Builder())
            self.shapes[row['id']] = shape

    def request(self, operation, payload, deadline):
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
        from OCP.BRepGProp import BRepGProp
        from OCP.GProp import GProp_GProps
        from OCP.gp import gp_Trsf, gp_Vec
        from OCP.TopLoc import TopLoc_Location
        def common(a, b):
            operation = BRepAlgoAPI_Common(a, b); operation.Build()
            if not operation.IsDone():
                raise AssertionError('Exact common failed in fixture')
            props = GProp_GProps(); BRepGProp.VolumeProperties_s(operation.Shape(), props)
            return abs(props.Mass()), None
        def moved(shape, offset):
            transform = gp_Trsf(); transform.SetTranslation(gp_Vec(*offset))
            return shape.Moved(TopLoc_Location(transform))
        assert operation == 'support'
        return native_support_pair(self.shapes[payload['a']], self.shapes[payload['b']], common, moved)


class SupportTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'work/support-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)

    def tearDown(self):
        self.temp.cleanup()

    def model(self, boxes):
        return box_model(self.temp.name, boxes)

    def compile(self, model, rules=None, engine=None):
        rules = rules or {}; initial = {'unit_0'}; targets = {u['id'] for u in model['units']} - initial
        process = compile_process_rules(model, rules, initial, targets)
        return prepare_support(model, rules, initial, targets, engine or DirectGeometry(model),
                               time.monotonic()+60, process=process)

    def test_supported_stack_requires_plate_before_relay(self):
        model = self.model([((0,0,0),(10,10,1)), ((1,1,1),(8,8,1)), ((3,3,2),(2,2,2))])
        support = self.compile(model)
        allowed, reasons = support_eligibility({'unit_1','unit_2'}, {'unit_0'}, support)
        self.assertEqual(allowed, {'unit_1'})
        self.assertEqual(reasons['unit_2']['reason'], 'UNSUPPORTED_COMPONENT')
        self.assertEqual(support_state({'unit_0','unit_1','unit_2'}, support)['status'], 'PASS')
        self.assertEqual(support_removal('unit_1', {'unit_0','unit_1','unit_2'}, support)['code'],
                         'REMOVAL_LEAVES_UNSUPPORTED_COMPONENTS')
        self.assertEqual(support_removal('unit_2', {'unit_0','unit_1','unit_2'}, support)['status'], 'PASS')

    def test_air_gap_is_not_support_and_fixture_must_be_declared(self):
        model = self.model([((0,0,0),(10,10,1)), ((3,3,4),(2,2,2))])
        support = self.compile(model)
        self.assertEqual(support_state({'unit_0','unit_1'}, support)['status'], 'UNSUPPORTED')
        self.assertEqual(support_summary(support)['native_probe_count'], 0)
        support = self.compile(model, {'support_fixtures':[{'unit_id':'unit_1','basis':'Workholding fixture at final pose'}]})
        self.assertEqual(support_state({'unit_0','unit_1'}, support)['status'], 'PASS')

    def test_vertical_side_touch_is_not_a_bearing(self):
        model = self.model([((0,0,0),(1,10,10)), ((1,2,3),(2,2,2))])
        support = self.compile(model)
        self.assertEqual(support_state({'unit_0','unit_1'}, support)['status'], 'UNSUPPORTED')
        support = self.compile(model, {'process_policy':{'require_fixing_before_next_component':True},
            'process_groups':[{'target_unit_id':'unit_1','fixation_mode':'self_fixed',
                               'status':'confirmed','basis':'Declared retained side latch','fastener_unit_ids':[]}]})
        self.assertEqual(support_state({'unit_0','unit_1'}, support)['status'], 'PASS')

    def test_hole_bbox_overlap_cannot_prove_support(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.gp import gp_Pnt
        from OCP.BRepTools import BRepTools
        model = self.model([((0,0,0),(10,10,3)), ((4,4,3),(2,2,2))])
        rim = BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(gp_Pnt(0,0,0),10,10,3).Shape(),
                             BRepPrimAPI_MakeBox(gp_Pnt(3,3,-1),4,4,5).Shape()).Shape()
        BRepTools.Write_s(rim, model['occurrences'][0]['brep_path'])
        support = self.compile(model)
        self.assertEqual(support_state({'unit_0','unit_1'}, support)['status'], 'UNSUPPORTED')

    def test_nominal_seat_overlap_uses_new_downward_volume(self):
        model = self.model([((0,0,0),(10,10,1)), ((3,3,.95),(2,2,2))])
        support = self.compile(model)
        self.assertEqual(support_state({'unit_0','unit_1'}, support)['status'], 'PASS')
        evidence = support_summary(support)['relations'][0]['evidence'][0]
        self.assertGreater(evidence['baseline_volume_mm3'], 0)
        self.assertTrue(evidence['b_supported_by_a'])

    def test_native_failure_does_not_certify_floating_part(self):
        class FailedEngine:
            def request(self, *args):
                return {'status':'UNKNOWN','reason':'NATIVE_TIMEOUT'}
        model = self.model([((0,0,0),(10,10,1)), ((3,3,1),(2,2,2))])
        support = self.compile(model, engine=FailedEngine())
        self.assertEqual(support_state({'unit_0','unit_1'}, support)['status'], 'UNKNOWN')

    def test_fixing_group_cannot_make_entire_detached_group_float(self):
        model = self.model([((0,0,0),(10,10,1)), ((3,3,5),(2,2,2)), ((3,3,7),(1,1,1))])
        rules = {'process_policy':{'require_fixing_before_next_component':True},
                 'process_groups':[{'target_unit_id':'unit_1','fastener_unit_ids':['unit_2'],
                                    'status':'confirmed','basis':'Owner mount bolt'}]}
        support = self.compile(model, rules)
        self.assertEqual(support_state({'unit_0','unit_1','unit_2'}, support)['status'], 'UNSUPPORTED')

    def test_mounted_side_part_can_be_held_only_until_immediate_fixing(self):
        # Side bracket has no gravity bearing. Its declared mount bolt bridges
        # that bracket to the housing; process rules enforce immediate fixing.
        model = self.model([((0,0,0),(1,10,10)), ((1,2,3),(2,2,2)), ((.5,2.5,3.5),(1.5,1,1))])
        rules = {'process_policy':{'require_fixing_before_next_component':True},
                 'process_groups':[{'target_unit_id':'unit_1','fastener_unit_ids':['unit_2'],
                                    'status':'confirmed','basis':'Side bracket mount bolt'}]}
        support = self.compile(model, rules)
        state = support_state({'unit_0','unit_1'}, support)
        self.assertEqual(state['status'], 'PASS')
        self.assertEqual(state['temporary_supported_unit_ids'], ['unit_1'])
        full = support_state({'unit_0','unit_1','unit_2'}, support)
        self.assertEqual(full['status'], 'PASS')
        self.assertFalse(full['temporary_supported_unit_ids'])

    def test_no_fixed_base_uses_workbench_without_preinstalling_base(self):
        model = self.model([((0,0,0),(10,10,1)), ((3,3,1),(2,2,2))])
        for unit in model['units']:
            unit['fixed'] = False
        compiled = prepare_support(model, {}, set(), {'unit_0','unit_1'}, DirectGeometry(model), time.monotonic()+60)
        allowed, _ = support_eligibility({'unit_0','unit_1'}, set(), compiled)
        self.assertEqual(allowed, {'unit_0'})
        self.assertEqual(support_state({'unit_0','unit_1'}, compiled)['status'], 'PASS')
        self.assertEqual(support_summary(compiled)['workbench_assumption']['resting_unit_ids'], ['unit_0'])

    def test_fixture_with_unknown_unit_or_missing_basis_is_rejected(self):
        model = self.model([((0,0,0),(10,10,1)), ((3,3,4),(2,2,2))])
        for fixture in ({'unit_id':'typo','basis':'fixture'}, {'unit_id':'unit_1'}):
            compiled = self.compile(model, {'support_fixtures':[fixture]})
            self.assertEqual(support_state({'unit_0','unit_1'}, compiled)['status'], 'UNKNOWN')
            self.assertEqual(support_summary(compiled)['diagnostics'][0]['code'], 'INVALID_SUPPORT_FIXTURE')

    def replace_shapes(self, model, shapes):
        from OCP.BRepTools import BRepTools
        from backend.geometry import shape_features, combine_bbox
        for index,shape in enumerate(shapes):
            BRepTools.Write_s(shape,model['occurrences'][index]['brep_path'])
            features=shape_features(shape);model['occurrences'][index].update(features)
            model['units'][index]['bbox']=features['bbox']
        model['bbox']=combine_bbox([u['bbox'] for u in model['units']])

    def shaft_model(self, bore_radius=2.2, opened=False):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox, BRepPrimAPI_MakeCylinder
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
        from OCP.gp import gp_Pnt,gp_Ax2,gp_Dir
        model=self.model([((-5,0,-5),(10,4,10)),((-2,-2,-2),(4,8,4))])
        base=BRepPrimAPI_MakeBox(gp_Pnt(-5,0,-5),10,4,10).Shape()
        bore=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,-1,0),gp_Dir(0,1,0)),bore_radius,6).Shape()
        base=BRepAlgoAPI_Cut(base,bore).Shape()
        if opened:
            opening=BRepPrimAPI_MakeBox(gp_Pnt(-3,-1,0),6,6,6).Shape()
            base=BRepAlgoAPI_Cut(base,opening).Shape()
        shaft=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,-2,0),gp_Dir(0,1,0)),1.85,8).Shape()
        self.replace_shapes(model,[base,shaft])
        return model

    def test_coaxial_horizontal_shaft_is_radially_supported_with_fit_clearance(self):
        support=self.compile(self.shaft_model())
        self.assertEqual(support_state({'unit_0','unit_1'},support)['status'],'PASS')
        evidence=support_summary(support)['relations'][0]['evidence'][0]
        self.assertAlmostEqual(evidence['coaxial_fits'][0]['radial_clearance_mm'],.35)
        self.assertTrue(evidence['radially_constrained'])

    def test_open_partial_cylinder_and_oversized_hole_do_not_prove_fit(self):
        for options in ({'opened':True},{'bore_radius':3.}):
            support=self.compile(self.shaft_model(**options))
            self.assertEqual(support_state({'unit_0','unit_1'},support)['status'],'UNSUPPORTED')

    def test_unique_seated_head_infers_component_then_immediate_bolt(self):
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder
        from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut,BRepAlgoAPI_Fuse
        from OCP.gp import gp_Pnt,gp_Ax2,gp_Dir
        from backend.process_rules import process_eligibility
        model=self.model([((-5,0,-5),(10,4,10)),((-3,-2,-3),(6,2,6)),((-3,-4,-3),(6,9,6))])
        model['units'][2]['role']='fastener'
        bore=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,-3,0),gp_Dir(0,1,0)),2.2,8).Shape()
        base=BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(gp_Pnt(-5,0,-5),10,4,10).Shape(),bore).Shape()
        plate=BRepAlgoAPI_Cut(BRepPrimAPI_MakeBox(gp_Pnt(-3,-2,-3),6,2,6).Shape(),bore).Shape()
        shaft=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,-2,0),gp_Dir(0,1,0)),1.85,7).Shape()
        head=BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0,-4,0),gp_Dir(0,1,0)),3,2).Shape()
        bolt=BRepAlgoAPI_Fuse(shaft,head).Shape()
        self.replace_shapes(model,[base,plate,bolt]);compiled=self.compile(model)
        edges=infer_support_process(compiled)
        self.assertIn(('unit_1','unit_2'),edges)
        self.assertEqual(compiled['process']['fastener_owners']['unit_2'],'unit_1')
        self.assertEqual(support_state({'unit_0','unit_1'},compiled)['status'],'PASS')
        self.assertEqual(support_state({'unit_0','unit_1'},compiled)['temporary_supported_unit_ids'],['unit_1'])
        self.assertEqual(process_eligibility({'unit_2','unrelated'},{'unit_0','unit_1'},compiled['process'])[0],{'unit_2'})

    def test_remove_supported_dependents_before_opening_fixing_group(self):
        # Opening the plate's fixing group commits the reverse process to
        # removing the plate next. Its seated child must therefore leave first.
        model=self.model([((0,0,0),(10,10,1)),((1,1,1),(8,8,1)),
                          ((3,3,2),(2,2,2)),((7,7,2),(1,1,1))])
        rules={'process_policy':{'require_fixing_before_next_component':True},
               'process_groups':[{'target_unit_id':'unit_1','fastener_unit_ids':['unit_3'],
                                  'status':'confirmed','basis':'Plate fixing hardware'}]}
        compiled=self.compile(model,rules);occupied={'unit_0','unit_1','unit_2','unit_3'}
        self.assertEqual(support_state(occupied,compiled)['status'],'PASS')
        blocked=support_removal('unit_3',occupied,compiled)
        self.assertEqual(blocked['code'],'REMOVAL_FIXING_DEPENDENTS')
        self.assertEqual(blocked['dependent_unit_ids'],['unit_2'])
        self.assertEqual(support_removal('unit_2',occupied,compiled)['status'],'PASS')
        self.assertEqual(support_removal('unit_3',occupied-{'unit_2'},compiled)['status'],'PASS')


if __name__ == '__main__':
    unittest.main()
