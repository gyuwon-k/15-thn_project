"""Saved calibrated geometry must preserve identity; exact failures stay gated."""
import hashlib
import json
from pathlib import Path
from queue import Queue
import tempfile
import unittest
from unittest.mock import patch

from OCP.BRepTools import BRepTools
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from backend.geometry import _repair_worker, _run_repair_attempt, repair_occurrences, REPAIR_VERSION
from tests.test_exact_representation_repair import missed_narrow_peak

TEST_ROOT=Path(__file__).resolve().parents[1]/'work'/'test-exact-repair'
TEST_ROOT.mkdir(parents=True,exist_ok=True)

class RepairPipelineExactGateTests(unittest.TestCase):
    def test_atomic_reply_survives_repeated_spawn_without_queue_semaphores(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root=Path(directory);source=root/'source.brep'
            self.assertTrue(BRepTools.Write_s(BRepPrimAPI_MakeBox(2,3,4).Shape(),str(source)))
            original=source.read_bytes()
            for index in range(3):
                result=_run_repair_attempt(source,root/f'candidate-{index}.brep',root/f'audit-{index}.json',20)
                self.assertEqual(result['status'],'already_valid')
                self.assertTrue(result['exact_valid'])
                self.assertEqual(result['source_sha256'],hashlib.sha256(original).hexdigest())
            self.assertEqual(source.read_bytes(),original)
            self.assertFalse(list(root.glob('*.reply.json')))

    def test_atomic_reply_timeout_and_cancel_never_reuse_old_success(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root=Path(directory);source=root/'source.brep'
            self.assertTrue(BRepTools.Write_s(BRepPrimAPI_MakeBox(2,3,4).Shape(),str(source)))
            # A reply for another attempt must not turn a timeout into success.
            stale=root/'candidate.old.reply.json';stale.write_text('{"status":"already_valid"}',encoding='utf-8')
            result=_run_repair_attempt(source,root/'candidate.brep',root/'audit.json',0)
            self.assertEqual(result['status'],'blocked')
            self.assertEqual(result['reason'],'NATIVE_REPAIR_TIMEOUT_OR_FAILURE')
            with self.assertRaisesRegex(InterruptedError,'PREPROCESS_CANCELLED'):
                _run_repair_attempt(source,root/'candidate.brep',root/'audit.json',20,cancel=lambda:True)
            self.assertEqual(list(root.glob('*.reply.json')),[stale])

    def test_saved_candidate_is_rechecked_and_identity_proved(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root=Path(directory);source=root/'source.brep';candidate=root/'derived.brep';audit=root/'audit.json'
            self.assertTrue(BRepTools.Write_s(missed_narrow_peak(.0075),str(source)))
            original=hashlib.sha256(source.read_bytes()).hexdigest();output=Queue()
            _repair_worker(source,candidate,audit,output)
            result=output.get_nowait();proof=json.loads(audit.read_text(encoding='utf-8'))
            self.assertEqual(result['status'],'accepted')
            self.assertTrue(result['standard_valid'] and result['exact_valid'])
            self.assertTrue(all(proof['roundtrip_preservation_checks'].values()))
            self.assertTrue(proof['roundtrip_standard_valid'] and proof['roundtrip_exact_valid'])
            self.assertEqual(result['calibration_budget']['assembly_maximum_per_edge_increment_mm'],.05)
            self.assertEqual(result['calibration_budget']['assembly_maximum_increment_fraction_of_diagonal'],.001)
            self.assertTrue(result['collision_clearance_guard_required'])
            self.assertEqual(original,hashlib.sha256(source.read_bytes()).hexdigest())

    def test_ordinary_pass_exact_failure_is_not_collision_ready_even_from_cache(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root=Path(directory);source=root/'large-gap.brep'
            self.assertTrue(BRepTools.Write_s(missed_narrow_peak(.04),str(source)))
            for repeat in range(2):
                assembly={'instances':[{'id':'edge-gap','name':'unrecognized-new-part',
                    'brep_path':str(source),'collision_ready':True}], 'diagnostics':[]}
                repair_occurrences(assembly,root/'cache',timeout_seconds=20)
                row=assembly['instances'][0]
                self.assertEqual(row['repair']['version'],REPAIR_VERSION)
                self.assertEqual(row['repair']['status'],'exact_review_required')
                self.assertTrue(row['repair']['standard_valid'])
                self.assertFalse(row['exact_method_valid'])
                self.assertFalse(row['collision_ready'])
                self.assertEqual(row['brep_path'],str(source))

    def test_serialization_geometry_change_is_rejected_even_if_native_checks_pass(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root=Path(directory);source=root/'source.brep';candidate=root/'derived.brep';audit=root/'audit.json'
            native_write=BRepTools.Write_s
            self.assertTrue(native_write(missed_narrow_peak(.0075),str(source)))
            def altered_write(shape,path,*args):
                if str(path)==str(candidate):shape=BRepPrimAPI_MakeBox(10.,12.,14.001).Shape()
                return native_write(shape,path,*args)
            output=Queue()
            with patch('backend.geometry.BRepTools.Write_s',side_effect=altered_write):
                _repair_worker(source,candidate,audit,output)
            result=output.get_nowait()
            self.assertEqual(result['status'],'blocked')
            self.assertIn('REPAIR_ROUNDTRIP_GEOMETRY_CHANGED',result['reason'])

    def test_cached_candidate_or_audit_tampering_forces_fresh_assessment(self):
        with tempfile.TemporaryDirectory(dir=TEST_ROOT) as directory:
            root=Path(directory);source=root/'source.brep'
            self.assertTrue(BRepTools.Write_s(missed_narrow_peak(.02),str(source)))
            def assess():
                assembly={'instances':[{'id':'minor','name':'arbitrary',
                    'brep_path':str(source),'collision_ready':False}], 'diagnostics':[]}
                repair_occurrences(assembly,root/'cache',timeout_seconds=20)
                return assembly['instances'][0]
            first=assess();self.assertTrue(first['collision_ready'])
            candidate=Path(first['brep_path']);expected=candidate.read_bytes()
            self.assertTrue(BRepTools.Write_s(BRepPrimAPI_MakeBox(2,3,4).Shape(),str(candidate)))
            second=assess()
            self.assertTrue(second['collision_ready'])
            self.assertEqual(candidate.read_bytes(),expected)
            audit=Path(second['repair']['audit_path']);audit.write_text('{}',encoding='utf-8')
            third=assess()
            self.assertEqual(third['repair']['audit_sha256'],hashlib.sha256(audit.read_bytes()).hexdigest())
            self.assertIn('roundtrip_preservation_checks',json.loads(audit.read_text(encoding='utf-8')))


if __name__=='__main__':unittest.main()
