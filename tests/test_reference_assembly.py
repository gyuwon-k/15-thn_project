"""Motion must avoid new material even when the final CAD assembly is accepted."""
import copy
import queue
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend import domain
from backend.planning import GeometrySession, _native_worker, generate_plan, make_path, validate_plan
from tests.test_planning import box_model


REFERENCE = {'mode': 'nominal_interference', 'reference_mode': 'assembled_reference'}


class AssembledReferenceTests(unittest.TestCase):
    def setUp(self):
        root = Path(__file__).resolve().parents[1] / 'work' / 'reference-assembly-tests'
        root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=root)

    def tearDown(self):
        self.temp.cleanup()

    def overlapping_seat(self):
        return box_model(self.temp.name, [((0, 0, 0), (5, 5, 1)),
                                         ((1, 1, .999), (1, 1, 1))])

    def test_final_overlap_allows_travel_away_from_base_but_not_through_base(self):
        model = self.overlapping_seat()
        with GeometrySession(model, timeout=20, collision_policy=REFERENCE) as engine:
            initial = engine.pair('occ_1', 'occ_0', time.monotonic()+30)
            above = engine.path_pair('occ_1', 'occ_0', make_path('-Z', 10), time.monotonic()+30)
            below = engine.path_pair('occ_1', 'occ_0', make_path('+Z', 10), time.monotonic()+30)
        self.assertEqual(initial['status'], 'COLLISION')
        self.assertGreater(initial['common_volume_mm3'], 1e-5)
        self.assertEqual(above['status'], 'PASS', above)
        self.assertEqual(below['status'], 'COLLISION', below)
        self.assertEqual(below['reason'], 'SWEPT_VOLUME_PENETRATION')
        self.assertGreater(below['common_volume_mm3'], initial['common_volume_mm3'])

    def test_reference_cut_keeps_other_material_from_the_same_obstacle(self):
        model = self.overlapping_seat()
        with GeometrySession(model, timeout=20, collision_policy=REFERENCE) as engine:
            # Sliding sideways would scrape the rest of the original base.
            slide = engine.path_pair('occ_1', 'occ_0', make_path('-X', 10), time.monotonic()+30)
        self.assertEqual(slide['status'], 'COLLISION', slide)
        self.assertGreater(slide['common_volume_mm3'], 1e-5)
        self.assertEqual(slide['reference_scope'], 'exact_final_moving_unit_only')

    def test_low_level_default_retains_strict_geometry_for_existing_callers(self):
        model = self.overlapping_seat()
        with GeometrySession(model, timeout=20) as engine:
            above = engine.path_pair('occ_1', 'occ_0', make_path('-Z', 10), time.monotonic()+30)
        self.assertEqual(above['status'], 'COLLISION', above)
        self.assertEqual(above['reason'], 'FINAL_PENETRATION')
        self.assertEqual(domain.new_project({})['rules']['collision_policy']['reference_mode'], 'assembled_reference')

    def test_cover_remains_a_real_obstacle_for_an_inner_component(self):
        model = box_model(self.temp.name, [((0, 0, 0), (10, 10, 1)),
                                          ((2, 2, 1), (2, 2, 2)),
                                          ((1, 1, 3), (4, 4, .5))])
        model['units'][2]['hidden'] = True
        with GeometrySession(model, timeout=20, collision_policy=REFERENCE) as engine:
            inner = engine.path_pair('occ_1', 'occ_2', make_path('-Z', 10), time.monotonic()+30)
            cover = engine.path_pair('occ_2', 'occ_1', make_path('-Z', 10), time.monotonic()+30)
        self.assertEqual(inner['status'], 'COLLISION', inner)
        self.assertEqual(cover['status'], 'PASS', cover)

    def test_thin_obstacle_between_sample_positions_is_still_blocked(self):
        model = box_model(self.temp.name, [((0, 0, 0), (.01, 1, 1)),
                                          ((4.501, -.2, -.2), (.001, 1.4, 1.4))], fixed=(1,))
        with GeometrySession(model, timeout=20, collision_policy=REFERENCE) as engine:
            verdict = engine.path_pair('occ_0', 'occ_1', make_path('-X', 10), time.monotonic()+30)
        self.assertEqual(verdict['status'], 'COLLISION', verdict)
        self.assertEqual(verdict['reason'], 'SWEPT_VOLUME_PENETRATION')

    def test_reference_boolean_failure_is_unknown_and_cannot_certify_a_path(self):
        model = self.overlapping_seat()
        requests, results = queue.Queue(), queue.Queue()
        requests.put((1, 'envelope', {
            'a': 'occ_1', 'b': 'occ_0', 'assembled_reference': True,
            'region_bbox': {'min': [1, 1, .999], 'max': [2, 2, 11.999]}}))
        requests.put(None)
        failed_cut = Mock()
        failed_cut.IsDone.return_value = False
        with patch('OCP.BRepAlgoAPI.BRepAlgoAPI_Cut', return_value=failed_cut):
            _native_worker(requests, results, model['occurrences'])
        ident, verdict = results.get_nowait()
        self.assertEqual(ident, 1)
        self.assertEqual(verdict['status'], 'UNKNOWN', verdict)
        self.assertIn('ASSEMBLED_REFERENCE_CUT_FAILED', verdict['detail'])

    def test_reference_cache_respects_which_part_is_moving(self):
        model = box_model(self.temp.name, [((0, 0, 0), (2, 1, 1)),
                                          ((1, 0, 0), (2, 1, 1))])
        with GeometrySession(model, timeout=20, collision_policy=REFERENCE) as engine:
            first_left = engine.path_pair('occ_0', 'occ_1', make_path('+X', 10), time.monotonic()+30)
            second_right = engine.path_pair('occ_1', 'occ_0', make_path('-X', 10), time.monotonic()+30)
            second_left = engine.path_pair('occ_1', 'occ_0', make_path('+X', 10), time.monotonic()+30)
        self.assertEqual(first_left['status'], 'PASS', first_left)
        self.assertEqual(second_right['status'], 'PASS', second_right)
        self.assertEqual(second_left['status'], 'COLLISION', second_left)

    def test_clipped_obstacle_from_another_moving_shape_cannot_hide_new_material(self):
        model = box_model(self.temp.name, [((0, 0, 0), (2, 1, 1)),
                                          ((0, 0, 0), (1, 1, 1)),
                                          ((.5, 0, 0), (2.5, 1, 1))], fixed=(2,))
        common_envelope = {'min': [0, 0, 0], 'max': [4, 1, 1]}
        with GeometrySession(model, timeout=20, collision_policy=REFERENCE) as engine:
            first = engine.request('envelope', {
                'a': 'occ_0', 'b': 'occ_2', 'assembled_reference': True,
                'region_bbox': common_envelope}, time.monotonic()+30)
            second = engine.request('envelope', {
                'a': 'occ_1', 'b': 'occ_2', 'assembled_reference': True,
                'region_bbox': common_envelope}, time.monotonic()+30)
            # Only the second part's reference footprint can be removed. Reusing
            # the larger first part's clipped obstacle would falsely pass here.
            verdict = engine.request('sweep', {
                'a': 'occ_1', 'b': 'occ_2', 'assembled_reference': True,
                'offset': [.9, 0, 0], 'end_offset': [0, 0, 0],
                'clipped_obstacle_key': second['clipped_obstacle_key']}, time.monotonic()+30)
        self.assertNotEqual(first['clipped_obstacle_key'], second['clipped_obstacle_key'])
        self.assertEqual(verdict['status'], 'COLLISION', verdict)
        self.assertGreater(verdict['common_volume_mm3'], .8)

    def test_reverse_search_removes_cover_before_inner_without_manual_precedence(self):
        model = box_model(self.temp.name, [((0, 0, 0), (10, 10, 1)),
                                          ((2, 2, 1), (2, 2, 2)),
                                          ((1, 1, 3), (4, 4, .5))])
        rules = {'directions': ['-Z'], 'collision_policy': copy.deepcopy(REFERENCE),
                 'max_seconds': 45, 'validation_seconds': 45, 'native_timeout_seconds': 20}
        plan = generate_plan(model, rules)
        self.assertEqual(plan['status'], 'PASS', plan)
        self.assertEqual([s['unit_id'] for s in plan['disassembly']['removal_steps']], ['unit_2', 'unit_1'])
        self.assertEqual([s['unit_id'] for s in plan['steps']], ['unit_1', 'unit_2'])
        self.assertEqual(validate_plan(model, plan, rules)['status'], 'PASS')


if __name__ == '__main__':
    unittest.main()
