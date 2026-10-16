"""Regressions for geometry-independent declarations bypassing actual support."""
import tempfile
import time
import unittest
from unittest.mock import patch
from pathlib import Path

from backend.planning import GeometrySession, _action, _bbox_union, generate_plan, validate_plan
from backend.process_rules import compile_process_rules
from backend.support import prepare_support, support_state
from tests.test_planning import box_model
from tests.test_support import DirectGeometry


class SupportProofRegressions(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'work/support-proof-regressions'
        root.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=root)

    def tearDown(self):
        self.folder.cleanup()

    def mounted_state(self, owner_position, fastener_position):
        model = box_model(self.folder.name, [((0, 0, 0), (10, 10, 1)),
                                             (owner_position, (2, 2, 2)),
                                             (fastener_position, (1, 1, 1))])
        rules = {'process_policy': {'require_fixing_before_next_component': True},
                 'process_groups': [{'target_unit_id': 'unit_1',
                                     'fastener_unit_ids': ['unit_2'],
                                     'status': 'confirmed', 'basis': 'Declared mounting hardware'}]}
        initial, targets = {'unit_0'}, {'unit_1', 'unit_2'}
        process = compile_process_rules(model, rules, initial, targets)
        self.assertFalse(process['errors'])
        compiled = prepare_support(model, rules, initial, targets, DirectGeometry(model),
                                   time.monotonic() + 60, process)
        return support_state(initial | targets, compiled)

    def test_remote_bolt_at_ground_cannot_hold_disconnected_owner(self):
        # The bolt touches the root; its declared owner is separated above it.
        state = self.mounted_state((3, 3, 5), (3, 3, 1))
        self.assertNotEqual(state['status'], 'PASS')
        self.assertIn('unit_1', state['unsupported_unit_ids'])

    def test_grounded_owner_cannot_hold_remote_declared_bolt(self):
        # The owner has a real bearing, but the alleged bolt floats far above it.
        state = self.mounted_state((3, 3, 1), (3, 3, 10))
        self.assertNotEqual(state['status'], 'PASS')
        self.assertIn('unit_2', state['unsupported_unit_ids'])

    def test_no_moving_targets_does_not_bypass_invalid_analysis_representation(self):
        model = box_model(self.folder.name, [((0, 0, 0), (10, 10, 1)),
                                             ((0, 0, 0), (10, 10, 1))])
        model['occurrences'][0]['collision_ready'] = False
        model['units'][0]['collision_ready'] = False
        rules = {'fixed_unit_ids': ['unit_0', 'unit_1'],
                 'collision_policy': {'mode': 'nominal_interference',
                                      'reference_mode': 'assembled_reference'}}
        plan = generate_plan(model, rules)
        validation = validate_plan(model, {'steps': []}, rules)
        self.assertNotEqual(plan['status'], 'PASS')
        self.assertNotEqual(validation['status'], 'PASS')

    def test_valid_initial_only_reference_is_an_explicit_empty_plan(self):
        model = box_model(self.folder.name, [((0, 0, 0), (10, 10, 1))])
        rules = {'fixed_unit_ids': ['unit_0'],
                 'collision_policy': {'mode': 'nominal_interference',
                                      'reference_mode': 'assembled_reference'}}
        plan = generate_plan(model, rules)
        validation = validate_plan(model, plan, rules)
        self.assertEqual(plan['status'], 'PASS')
        self.assertEqual(plan['steps'], [])
        self.assertEqual(validation['status'], 'PASS')
        self.assertTrue(validation['coverage']['complete'])
        self.assertTrue(validation['placement_audit']['assumed_valid_final_assembly'])

    def test_invalid_geometry_skips_expensive_support_inference(self):
        model = box_model(self.folder.name, [((0, 0, 0), (10, 10, 1)),
                                             ((3, 3, 1), (2, 2, 2))])
        model['occurrences'][1]['collision_ready'] = False
        model['units'][1]['collision_ready'] = False
        rules = {'fixed_unit_ids': ['unit_0'],
                 'collision_policy': {'mode': 'nominal_interference',
                                      'reference_mode': 'assembled_reference'}}
        with patch('backend.planning._prepare_support',
                   side_effect=AssertionError('Invalid geometry must stop before native inference')):
            plan = generate_plan(model, rules)
        self.assertEqual(plan['status'], 'UNKNOWN')
        self.assertEqual(plan['search']['termination'], 'geometry_unresolved')

    def grouped_path(self, obstacle_size):
        # Two adjacent members move as one rigid assembly unit. The obstacle's
        # nominal overlap belongs to the right member, but the left member
        # crosses the same reference space while entering from the right.
        model = box_model(self.folder.name, [((1.2, .2, .2), obstacle_size),
                                             ((0, 0, 0), (1, 1, 1)),
                                             ((1, 0, 0), (1, 1, 1))])
        group = {**model['units'][1], 'id': 'moving_group',
                 'occurrence_ids': model['units'][1]['occurrence_ids'] + model['units'][2]['occurrence_ids'],
                 'bbox': _bbox_union([model['units'][1]['bbox'], model['units'][2]['bbox']])}
        model['units'] = [model['units'][0], group]
        units = {unit['id']: unit for unit in model['units']}
        with GeometrySession(model, timeout=20, collision_policy={
                'mode': 'nominal_interference', 'reference_mode': 'assembled_reference'}) as engine:
            return _action(group, {'unit_0'}, units, engine, '-X', 6, time.monotonic() + 30)

    def test_group_reference_includes_every_moving_members_final_shape(self):
        self.assertEqual(self.grouped_path((.6, .6, .6))['status'], 'PASS')

    def test_group_reference_keeps_obstacle_material_outside_member_union(self):
        self.assertEqual(self.grouped_path((2.3, .6, .6))['status'], 'COLLISION')


if __name__ == '__main__':
    unittest.main()
