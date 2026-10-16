import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend.jobs import JobService
from backend.main import Services
from backend.planning_settings import (
    DEFAULT_ANALYSIS_SECONDS, DEFAULT_CANDIDATE_SECONDS,
    DEFAULT_NATIVE_TIMEOUT_SECONDS, TIMEOUT_POLICY_VERSION,
    analysis_job_timeout, compatible_rules, settings_errors,
)


class AnalysisTimeoutTests(unittest.TestCase):
    def test_legacy_defaults_expand_without_changing_the_stored_snapshot(self):
        original = {'max_seconds': 300, 'validation_seconds': 300,
                    'candidate_seconds': 4, 'native_timeout_seconds': 20,
                    'collision_policy': {'mode': 'nominal_interference'}}
        before = copy.deepcopy(original)
        converted, notes = compatible_rules(original)
        self.assertEqual(original, before)
        self.assertEqual(converted['max_seconds'], 1800)
        self.assertEqual(converted['validation_seconds'], 1800)
        self.assertEqual(converted['candidate_seconds'], 120)
        self.assertEqual(converted['native_timeout_seconds'], 120)
        self.assertEqual(converted['timeout_policy_version'], TIMEOUT_POLICY_VERSION)
        self.assertIn('LEGACY_ANALYSIS_TIMEOUT_EXTENDED', [n['code'] for n in notes])
        again, repeated_notes = compatible_rules(converted)
        self.assertEqual(again, converted)
        self.assertEqual(repeated_notes, [])

    def test_custom_limits_are_preserved_and_new_policy_can_choose_300_seconds(self):
        custom = {'max_seconds': 600, 'validation_seconds': 700,
                  'candidate_seconds': 80, 'native_timeout_seconds': 90}
        converted, _ = compatible_rules(custom)
        for key, value in custom.items():
            self.assertEqual(converted[key], value)
        explicit = {'max_seconds': 300, 'validation_seconds': 300,
                    'candidate_seconds': 4, 'native_timeout_seconds': 20,
                    'timeout_policy_version': TIMEOUT_POLICY_VERSION}
        converted, _ = compatible_rules(explicit)
        for key, value in explicit.items():
            self.assertEqual(converted[key], value)

    def test_services_reads_legacy_projects_with_updated_limits_without_archive_write(self):
        original = {'id': 'p', 'rules': {'max_seconds': 300, 'validation_seconds': 300}}
        service = Services.__new__(Services)
        service.store = Mock()
        service.store.get_project.side_effect = lambda ident: copy.deepcopy(original)
        current = service.project('p')
        self.assertEqual(current['rules']['max_seconds'], 1800)
        self.assertEqual(original['rules']['max_seconds'], 300)
        service.store.save_project.assert_not_called()
        service.store.snapshots.assert_not_called()

    def test_budget_settings_and_supervisor_cover_both_complete_stages(self):
        rules = {'max_seconds': DEFAULT_ANALYSIS_SECONDS,
                 'validation_seconds': DEFAULT_ANALYSIS_SECONDS,
                 'candidate_seconds': DEFAULT_CANDIDATE_SECONDS,
                 'native_timeout_seconds': DEFAULT_NATIVE_TIMEOUT_SECONDS}
        self.assertEqual(settings_errors({'units': []}, rules), [])
        self.assertEqual(analysis_job_timeout(rules, 'generate'), 3720)
        self.assertEqual(analysis_job_timeout(rules, 'validate'), 1920)
        self.assertEqual(analysis_job_timeout({}, 'generate'), 3720)
        self.assertEqual(analysis_job_timeout({'max_seconds': 7200, 'validation_seconds': 7200}, 'generate'), 14520)

    def test_final_assembled_reference_is_default_but_strict_mode_is_preserved(self):
        converted, _ = compatible_rules({})
        self.assertEqual(converted['collision_policy']['reference_mode'], 'assembled_reference')
        strict = {'collision_policy': {'mode': 'nominal_interference', 'reference_mode': 'strict_geometry'}}
        converted, _ = compatible_rules(strict)
        self.assertEqual(converted['collision_policy']['reference_mode'], 'strict_geometry')
        self.assertEqual(settings_errors({'units': []}, strict), [])
        self.assertTrue(settings_errors({'units': []}, {'collision_policy': {'reference_mode': 'skip_all_collision'}}))

    def _supervisor_run(self, elapsed, cancel=False):
        root = Path(__file__).resolve().parents[1] / 'work' / 'timeout-tests'
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as folder:
            folder = Path(folder)
            (folder / 'request.json').write_text(json.dumps({'payload': {
                'timeout_seconds': analysis_job_timeout({}, 'generate')}}), encoding='utf-8')
            (folder / 'result.json').write_text(json.dumps({'result': {'complete': True}}), encoding='utf-8')
            service = JobService.__new__(JobService)
            service.root = folder
            service.release = {'build_id': 'test-build'}
            service.cad_lock = threading.Lock()
            service.stopping = False
            service.storage = Mock()
            service.apply_result = Mock(return_value={'message': '완료'})
            service.processes = {}
            event = Mock()
            event.is_set.side_effect = [False, True] if cancel else lambda: False
            service.cancel_events = {'job-test': event}
            process = Mock(returncode=0)
            process.poll.side_effect = [None, 0]
            job = {'id': 'job-test', 'kind': 'generate', 'project_id': 'p'}
            with patch('backend.jobs.subprocess.Popen', return_value=process), \
                    patch('backend.jobs.time.monotonic', side_effect=[0, elapsed]), \
                    patch.object(service, '_terminate') as terminate:
                service._run(job, folder)
                return job, service.apply_result.called, terminate.called

    def test_worker_supervisor_does_not_stop_at_300_seconds(self):
        job, applied, terminated = self._supervisor_run(301)
        self.assertEqual(job['status'], 'succeeded', job)
        self.assertTrue(applied)
        self.assertFalse(terminated)

    def test_expired_outer_guard_is_a_timeout_not_collision_or_success(self):
        job, applied, terminated = self._supervisor_run(3721)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'TimeoutError')
        self.assertFalse(applied)
        self.assertTrue(terminated)

    def test_user_cancel_still_stops_long_running_analysis(self):
        job, applied, terminated = self._supervisor_run(301, cancel=True)
        self.assertEqual(job['status'], 'canceled')
        self.assertFalse(applied)
        self.assertTrue(terminated)


if __name__ == '__main__':
    unittest.main()
