import copy
import hashlib
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from backend import domain
from backend.analysis_refresh import refresh_analysis_model
from backend.main import Services
from backend.worker import run_worker
from tests.test_application_invariants import fixture


class AnalysisRefreshTests(unittest.TestCase):
    def setUp(self):
        root=Path(__file__).resolve().parents[1]/'work'/'analysis-refresh-tests';root.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=root);self.root=Path(self.temp.name)
        self.source=self.root/'source.brep';self.source.write_bytes(b'original display and source CAD')
        self.old=self.root/'old-analysis.brep';self.old.write_bytes(b'previous failed analysis representation')
        self.candidate=self.root/'new-analysis.brep';self.candidate.write_bytes(b'new checked analysis representation')
        self.model={'fingerprint':'model-before','scene_path':'unchanged-scene.glb',
            'occurrences':[{'id':'bad','name':'Bad','brep_path':str(self.old),'source_brep_path':str(self.source),
                'bbox':{'min':[0,0,0],'max':[1,1,1]},'transform_world':[1,0,0,0],
                'collision_ready':False,'exact_method_valid':False},
                {'id':'good','name':'Good','brep_path':str(self.source),'collision_ready':True,'exact_method_valid':True}],
            'units':[{'id':'u','name':'Edited label','occurrence_ids':['bad'],'collision_ready':False,
                      'matching':{'method':'manual_verified','review_required':False,'geometry_verification':{'status':'PASS'}}},
                     {'id':'v','occurrence_ids':['good'],'collision_ready':True}],
            'quality':{'inventory_complete':True,'display_ready':True,'matching_ready':True,
                'unit_results':[{'unit_id':'u','collision_ready':False,'matching_ready':True}]},
            'diagnostics':[{'code':'MATCH_KEEP','instance_id':'bad'},
                {'code':'REPRESENTATION_REPAIR_BLOCKED','instance_id':'bad'}]}

    def tearDown(self):self.temp.cleanup()

    def accepted(self, assembly, cache, **kwargs):
        row=assembly['instances'][0]
        self.assertEqual(row['brep_path'],str(self.source))
        row.update(brep_path=str(self.candidate),source_brep_path=str(self.source),collision_ready=True,
                   exact_method_valid=True,repair={'status':'accepted','standard_valid':True,'exact_valid':True,'method':'checked-repair'})
        assembly['diagnostics'].append({'code':'REPRESENTATION_REPAIR_ACCEPTED','instance_id':row['id']})

    def test_only_failed_occurrence_reassessed_and_current_policy_reused(self):
        original=copy.deepcopy(self.model);repair=Mock(side_effect=self.accepted)
        updated,report=refresh_analysis_model(self.model,self.root,deadline=50,repair=repair,policy_version='policy-new',clock=lambda:0)
        self.assertEqual(self.model,original);self.assertEqual(repair.call_count,1)
        self.assertEqual(report['attempted_occurrence_ids'],['bad'])
        self.assertEqual(updated['scene_path'],original['scene_path'])
        self.assertEqual(updated['occurrences'][0]['bbox'],original['occurrences'][0]['bbox'])
        self.assertEqual(updated['occurrences'][0]['transform_world'],original['occurrences'][0]['transform_world'])
        self.assertEqual(updated['occurrences'][1],original['occurrences'][1])
        self.assertEqual(updated['units'][0]['matching'],original['units'][0]['matching'])
        self.assertEqual(updated['units'][0]['name'],'Edited label')
        self.assertTrue(updated['units'][0]['collision_ready'])
        self.assertTrue(updated['quality']['analysis_ready'])
        self.assertEqual(updated['quality']['automatically_repaired_occurrence_ids'],['bad'])
        self.assertTrue(updated['quality']['unit_results'][0]['collision_ready'])
        self.assertEqual(updated['analysis_policy_version'],'policy-new')
        self.assertNotEqual(updated['fingerprint'],original['fingerprint'])
        again,summary=refresh_analysis_model(updated,self.root,deadline=50,repair=repair,policy_version='policy-new',clock=lambda:0)
        self.assertIsNone(again);self.assertEqual(summary['attempted_occurrence_ids'],[])
        self.assertEqual(repair.call_count,1)

    def test_ready_models_do_not_read_files_or_start_native_reassessment(self):
        for row in self.model['occurrences']:
            row.update(collision_ready=True,exact_method_valid=True,brep_path='never-read.brep')
        repair=Mock()
        with patch('backend.analysis_refresh._file_sha',side_effect=AssertionError('valid rows must be untouched')):
            updated,report=refresh_analysis_model(self.model,self.root,deadline=50,repair=repair,policy_version='new',clock=lambda:0)
        self.assertIsNone(updated);repair.assert_not_called();self.assertEqual(report['attempted_occurrence_ids'],[])

    def test_failed_current_policy_is_not_repeated_or_promoted(self):
        def rejected(assembly,*args,**kwargs):
            row=assembly['instances'][0]
            row.update(collision_ready=True,exact_method_valid=False,repair={'status':'exact_review_required','version':'new','standard_valid':True,'exact_valid':False})
        repair=Mock(side_effect=rejected)
        updated,_=refresh_analysis_model(self.model,self.root,deadline=50,repair=repair,policy_version='new',clock=lambda:0)
        self.assertFalse(updated['occurrences'][0]['collision_ready'])
        self.assertFalse(updated['units'][0]['collision_ready'])
        self.assertFalse(updated['quality']['analysis_ready'])
        again,_=refresh_analysis_model(updated,self.root,deadline=50,repair=repair,policy_version='new',clock=lambda:0)
        self.assertIsNone(again);self.assertEqual(repair.call_count,1)

    def test_recent_preprocessing_failed_result_is_not_reassessed_again(self):
        self.model['analysis_policy_version']='new'
        self.model['occurrences'][0]['repair']={'version':'new','status':'exact_review_required',
            'source_sha256':hashlib.sha256(self.source.read_bytes()).hexdigest()}
        repair=Mock()
        updated,_=refresh_analysis_model(self.model,self.root,deadline=50,repair=repair,policy_version='new',clock=lambda:0)
        self.assertIsNone(updated);repair.assert_not_called()

    def test_changed_source_bytes_require_step_registration_before_any_policy_repair(self):
        expected=hashlib.sha256(self.source.read_bytes()).hexdigest()
        self.source.write_bytes(b'changed source at the original path')
        for policy in ('old','new'):
            for field in ('source_brep_sha256','repair'):
                with self.subTest(policy=policy,hash_field=field):
                    model=copy.deepcopy(self.model);row=model['occurrences'][0]
                    row['analysis_policy_version']=policy
                    if field=='repair':row['repair']={'version':policy,'source_sha256':expected}
                    else:row['source_brep_sha256']=expected
                    original=copy.deepcopy(model);repair=Mock()
                    with self.assertRaises(domain.DomainError) as raised:
                        refresh_analysis_model(model,self.root,deadline=50,repair=repair,policy_version='new',clock=lambda:0)
                    self.assertEqual(raised.exception.code,'ANALYSIS_SOURCE_HASH_MISMATCH')
                    self.assertIn('STEP을 다시 등록',str(raised.exception))
                    repair.assert_not_called();self.assertEqual(model,original)

    def test_legacy_row_without_source_hash_records_both_hashes_on_first_assessment(self):
        self.model['analysis_policy_version']='new'
        self.model['occurrences'][0]['repair']={'version':'new','status':'exact_review_required'}
        repair=Mock(side_effect=self.accepted)
        updated,_=refresh_analysis_model(self.model,self.root,deadline=50,repair=repair,policy_version='new',clock=lambda:0)
        row=updated['occurrences'][0];repair.assert_called_once()
        self.assertEqual(row['source_brep_sha256'],hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertEqual(row['analysis_brep_sha256'],hashlib.sha256(self.candidate.read_bytes()).hexdigest())

    def test_same_policy_rechecks_changed_derived_analysis_bytes(self):
        def rejected(assembly,*args,**kwargs):
            assembly['instances'][0].update(collision_ready=False,exact_method_valid=False,
                brep_path=str(self.candidate),repair={'version':'new','status':'exact_review_required'})
        repaired=Mock(side_effect=rejected)
        first,_=refresh_analysis_model(self.model,self.root,deadline=50,repair=repaired,policy_version='new',clock=lambda:0)
        self.candidate.write_bytes(b'updated analysis bytes')
        second,report=refresh_analysis_model(first,self.root,deadline=50,repair=repaired,policy_version='new',clock=lambda:0)
        self.assertEqual(repaired.call_count,2);self.assertTrue(report['changed'])
        self.assertNotEqual(first['fingerprint'],second['fingerprint'])

    def test_native_timeout_can_retry_without_fingerprint_drift(self):
        def timed_out(assembly,*args,**kwargs):
            assembly['instances'][0].update(collision_ready=False,exact_method_valid=None,
                repair={'status':'blocked','reason':'NATIVE_REPAIR_TIMEOUT_OR_FAILURE'})
        first,_=refresh_analysis_model(self.model,self.root,deadline=50,repair=timed_out,policy_version='new',clock=lambda:0)
        fingerprint=first['fingerprint']
        repeated,report=refresh_analysis_model(first,self.root,deadline=50,repair=timed_out,policy_version='new',clock=lambda:0)
        self.assertIsNone(repeated);self.assertFalse(report['changed'])
        self.assertEqual(first['fingerprint'],fingerprint)
        self.assertEqual(report['attempted_occurrence_ids'],['bad'])

    def test_each_native_repair_is_limited_by_remaining_stage_budget(self):
        calls=[];clock=[0.]
        self.model['occurrences'][1].update(collision_ready=False,exact_method_valid=False)
        def repair(assembly,cache,**kwargs):
            calls.append(kwargs['timeout_seconds']);self.accepted(assembly,cache,**kwargs);clock[0]=181.
        updated,report=refresh_analysis_model(self.model,self.root,deadline=180,native_timeout_seconds=120,
            repair=repair,policy_version='new',clock=lambda:clock[0])
        self.assertEqual(calls,[120]);self.assertTrue(report['budget_exhausted'])
        self.assertEqual(report['pending_occurrence_ids'],['good'])
        self.assertFalse(updated['occurrences'][1]['collision_ready'])
        remaining=Mock(side_effect=self.accepted)
        refresh_analysis_model(self.model,self.root,deadline=17,native_timeout_seconds=120,
            repair=remaining,policy_version='new',clock=lambda:0)
        self.assertEqual(remaining.call_args.kwargs['timeout_seconds'],17)

    def test_failure_and_cancellation_leave_original_model_untouched(self):
        before=copy.deepcopy(self.model)
        def failed(assembly,*args,**kwargs):
            assembly['instances'][0]['collision_ready']=True
            raise ValueError('native operation failed')
        with self.assertRaises(ValueError):
            refresh_analysis_model(self.model,self.root,deadline=50,repair=failed,policy_version='new',clock=lambda:0)
        self.assertEqual(self.model,before)
        cancel=Mock(side_effect=[False,True])
        with self.assertRaises(InterruptedError):
            refresh_analysis_model(self.model,self.root,deadline=50,repair=self.accepted,policy_version='new',clock=lambda:0,cancel=cancel)
        self.assertEqual(self.model,before)

    def test_source_changed_during_native_operation_rejects_replacement_model(self):
        before=copy.deepcopy(self.model)
        def changed(assembly,*args,**kwargs):
            self.accepted(assembly,*args,**kwargs)
            self.source.write_bytes(b'changed while native repair was running')
        with self.assertRaises(domain.DomainError) as raised:
            refresh_analysis_model(self.model,self.root,deadline=50,repair=changed,policy_version='new',clock=lambda:0)
        self.assertEqual(raised.exception.code,'ANALYSIS_SOURCE_HASH_MISMATCH')
        self.assertIn('STEP을 다시 등록',str(raised.exception))
        self.assertEqual(self.model,before)


class AnalysisRefreshPublicationTests(unittest.TestCase):
    def service(self):
        project=fixture();project['row_version']=1
        service=Services.__new__(Services);service.lock=threading.RLock();service.root=Path('.')
        service.store=Mock();service.store.get_project.side_effect=lambda ident:copy.deepcopy(project)
        service.store.save_project.side_effect=lambda state,*args:copy.deepcopy(state)
        service.release={'build_id':'new-build','algorithm_version':'2.4.0'}
        service.preserve_model=Mock(side_effect=lambda model,*args:copy.deepcopy(model));service.preserve_search_trace=Mock()
        job={'id':'j','project_id':project['id'],'kind':'generate','geometry_hash':domain.geometry_hash(project)}
        updated=copy.deepcopy(project['model']);updated['fingerprint']='new-analysis-fingerprint';updated['analysis_policy_version']='new'
        return service,project,job,updated

    def test_incomplete_generation_publishes_analysis_without_old_pass_reuse(self):
        service,project,job,updated=self.service()
        result={'analysis_model':updated,'plan':{'replace_plan':False,'steps':[],'disassembly':{'removed_count':0}},
                'validation':{'status':'UNKNOWN'}}
        response=service.apply_result(job,result);saved=service.store.save_project.call_args.args[0]
        self.assertEqual(response['status'],'INCOMPLETE')
        self.assertEqual(saved['model']['fingerprint'],'new-analysis-fingerprint')
        self.assertEqual(saved['rules'],project['rules'])
        self.assertEqual(saved['plan']['id'],project['plan']['id'])
        self.assertEqual(saved['plan']['steps'][0]['unit_id'],project['plan']['steps'][0]['unit_id'])
        self.assertEqual(saved['plan']['steps'][0]['torque_status'],project['plan']['steps'][0]['torque_status'])
        self.assertIsNone(saved['validation']);self.assertIsNone(saved['plan']['steps'][0]['path'])
        self.assertFalse(saved['plan']['steps'][0]['executable'])
        self.assertTrue(domain.confirmation_errors(saved))

    def test_validated_new_representation_uses_new_model_hash_and_current_labels(self):
        service,project,job,updated=self.service()
        project['model']['units'][0]['name']='Newer UI label'
        updated['units'][0]['name']='Old worker label'
        result={'analysis_model':updated,'plan':copy.deepcopy(project['plan']),
                'validation':{'status':'PASS','steps':copy.deepcopy(project['plan']['steps'])}}
        service.apply_result(job,result);saved=service.store.save_project.call_args.args[0]
        self.assertEqual(saved['model']['units'][0]['name'],'Newer UI label')
        self.assertEqual(saved['validation']['dependency_hash'],domain.geometry_hash(saved))
        self.assertNotEqual(saved['validation']['dependency_hash'],job['geometry_hash'])
        self.assertEqual(saved['validation']['status'],'PASS')

    def test_stale_result_does_not_register_or_publish_replacement_assets(self):
        service,project,job,updated=self.service();job['geometry_hash']='stale'
        with self.assertRaises(domain.DomainError) as failure:
            service.apply_result(job,{'analysis_model':updated})
        self.assertEqual(failure.exception.code,'STALE_RESULT')
        service.preserve_model.assert_not_called();service.store.save_project.assert_not_called()

    def test_asset_publication_failure_keeps_stored_project_unchanged(self):
        service,project,job,updated=self.service();before=copy.deepcopy(project)
        service.preserve_model.side_effect=OSError('asset write failed')
        with self.assertRaises(OSError):service.apply_result(job,{'analysis_model':updated})
        service.store.save_project.assert_not_called();self.assertEqual(project,before)

    def test_native_asset_registration_error_is_not_silently_ignored(self):
        root=Path(__file__).resolve().parents[1]/'work'/'analysis-refresh-tests';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            path=Path(directory)/'derived.brep';path.write_bytes(b'derived analysis geometry')
            service=Services.__new__(Services);service.root=Path(directory);service.store=Mock()
            service.store.register_file.side_effect=OSError('asset copy failed')
            with self.assertRaisesRegex(OSError,'asset copy failed'):
                service.preserve_model({'brep_path':str(path)},'p')


class AnalysisRefreshWorkerBudgetTests(unittest.TestCase):
    def test_refresh_time_is_subtracted_from_generation_and_validation_budgets(self):
        root=Path(__file__).resolve().parents[1]/'work'/'analysis-refresh-tests';root.mkdir(parents=True,exist_ok=True)
        for kind,budget_key,total in [('generate','max_seconds',100),('validate','validation_seconds',200)]:
            with self.subTest(kind=kind), tempfile.TemporaryDirectory(dir=root) as directory:
                folder=Path(directory);project=fixture();project['rules'].update(max_seconds=100,validation_seconds=200)
                changed=copy.deepcopy(project['model']);changed['fingerprint']='refreshed'
                request={'kind':kind,'payload':{'project':project,'cache_dir':str(folder/'cache')},
                    'data_dir':str(folder),'folder':str(folder),'release':{'build_id':'test'}}
                request_path=folder/'request.json';request_path.write_text(json.dumps(request),encoding='utf-8')
                clock=[0.]
                def refresh(*args,**kwargs):
                    self.assertEqual(kwargs['deadline'],total);clock[0]=7.
                    return changed,{'attempted_occurrence_ids':['a'],'elapsed_seconds':7,'changed':True}
                with patch('backend.release.runtime_identity',return_value={'build_id':'test'}), \
                     patch('backend.analysis_refresh.refresh_analysis_model',side_effect=refresh), \
                     patch('backend.worker.time.monotonic',side_effect=lambda:clock[0]), \
                     patch('backend.planning.generate_plan',return_value={'steps':[],'replace_plan':True,'search':{'elapsed_seconds':0}}) as generate, \
                     patch('backend.planning.validate_plan',return_value={'status':'PASS'}) as validate:
                    self.assertEqual(run_worker(request_path),0)
                    target=generate if kind=='generate' else validate
                    self.assertEqual(target.call_args.args[1 if kind=='generate' else 2][budget_key],total-7)
                    self.assertEqual(target.call_args.args[0]['fingerprint'],'refreshed')
                envelope=json.loads((folder/'result.json').read_text(encoding='utf-8'))
                self.assertEqual(envelope['result']['analysis_model']['fingerprint'],'refreshed')

    def test_exhausted_refresh_budget_never_starts_plan_or_certifies_old_proof(self):
        root=Path(__file__).resolve().parents[1]/'work'/'analysis-refresh-tests';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root) as directory:
            folder=Path(directory);project=fixture();project['rules']['max_seconds']=5
            request={'kind':'generate','payload':{'project':project,'cache_dir':str(folder/'cache')},
                'data_dir':str(folder),'folder':str(folder),'release':{'build_id':'test'}}
            request_path=folder/'request.json';request_path.write_text(json.dumps(request),encoding='utf-8')
            clock=[0.]
            def refresh(*args,**kwargs):
                clock[0]=6.
                return None,{'attempted_occurrence_ids':[],'elapsed_seconds':6,'budget_exhausted':True}
            with patch('backend.release.runtime_identity',return_value={'build_id':'test'}), \
                 patch('backend.analysis_refresh.refresh_analysis_model',side_effect=refresh), \
                 patch('backend.worker.time.monotonic',side_effect=lambda:clock[0]), \
                 patch('backend.planning.generate_plan') as generate,patch('backend.planning.validate_plan') as validate:
                self.assertEqual(run_worker(request_path),0);generate.assert_not_called();validate.assert_not_called()
            result=json.loads((folder/'result.json').read_text(encoding='utf-8'))['result']
            self.assertFalse(result['plan']['replace_plan']);self.assertEqual(result['validation']['status'],'UNKNOWN')


if __name__=='__main__':unittest.main()
