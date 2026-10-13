from __future__ import annotations
import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend import asap_integration as integration, domain, store
from asap_service.backend import app as raw, storage, jobs

ROOT=Path(__file__).resolve().parents[1]
FIXTURES=ROOT.parent/'asap-unit-test/data'
SID='4af58483c8da40919dc3a7bcb69b402a'
RID='dcfbdb01bc70489b973d0afadc319b57'


@asynccontextmanager
async def lifespan(_):
    store.init();integration.startup()
    yield
    integration.shutdown()


class FinalIntegrationTests(unittest.TestCase):
    def setUp(self):
        (ROOT/'evidence').mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=ROOT/'evidence')
        self.data=Path(self.temp.name)
        self.patch=patch.object(store,'DATA_ROOT',self.data);self.patch.start()
        self.projects={}
        def save(state,*args,**kwargs):self.projects[state['project']['id']]=copy.deepcopy(state)
        self.store_patches=[patch.object(store,'init',lambda:None),patch.object(store,'save',save),
                            patch.object(store,'get',lambda pid:copy.deepcopy(self.projects[pid]))]
        for item in self.store_patches:item.start()
        integration.shutdown()
        integration.startup()
        source=FIXTURES/'sessions'/SID
        shutil.copytree(source,self.data/'asap/sessions'/SID)
        index=FIXTURES/'runs'/(RID+'.json')
        storage.write_json(self.data/'asap/runs'/(RID+'.json'),storage.read_json(index))
        store.init()
        self.state=domain.new_project({'name':'실제 ASAP fixture 도입'})
        store.save(self.state)
        self.pid=self.state['project']['id']
        self.client=TestClient(FastAPI())
        self.client.app.include_router(integration.router)
        self.client.app.mount('/api/asap',raw.app)
        integration.link(self.pid,integration.LinkInput(session_id=SID,expected_input_revision=0))

    def tearDown(self):
        integration.shutdown()
        for item in self.store_patches:item.stop()
        self.patch.stop();self.temp.cleanup()

    def test_prefix_and_namespace(self):
        response=self.client.get('/api/asap/sessions/'+SID)
        self.assertEqual(response.status_code,200)
        self.assertEqual(len(response.json()['units']),3)
        self.assertFalse(any('/api/' in route.path for route in raw.app.routes if hasattr(route,'path')))

    def test_real_complete_adoption_preserves_repeat_bolts_and_raw_paths(self):
        source=self.data/'asap/sessions'/SID/'runs'/RID/'result.json'
        before=hashlib.sha256(source.read_bytes()).hexdigest()
        result=integration.import_run(self.pid,integration.ImportInput(run_id=RID,expected_input_revision=0))
        state=store.get(self.pid)
        self.assertEqual(result['units'],3)
        self.assertEqual(len(state['instances']),4)
        self.assertEqual(len(set(u['id'] for u in state['units'])),3)
        self.assertEqual(len(state['parts']),2)
        self.assertEqual(state['plan']['status'],'draft')
        self.assertEqual(state['plan']['origin'],'asap')
        self.assertEqual(state['rules'],[])
        self.assertTrue(all(op['review_status']=='review_required' for op in state['operations']))
        original=json.loads(source.read_text(encoding='utf-8'))
        by_engine={s['moving_part']:s for s in original['steps']}
        for op in state['operations'][1:]:
            self.assertEqual(op['asap_motion']['step'],by_engine[op['asap_engine_id']])
        self.assertEqual(before,hashlib.sha256(source.read_bytes()).hexdigest())

    def test_revision_change_blocks_adoption(self):
        session=self.data/'asap/sessions'/SID
        units=storage.read_json(session/'unit_state.json');units['revision']='0'*32
        storage.write_json(session/'unit_state.json',units)
        with self.assertRaisesRegex(domain.DomainError,'현재 확정'):
            integration.import_run(self.pid,integration.ImportInput(run_id=RID))

    def test_wrong_session_and_project_revision_block(self):
        with self.assertRaises(domain.DomainError):
            integration.import_run(self.pid,integration.ImportInput(run_id=RID,expected_input_revision=3))
        state=store.get(self.pid);state['asap']['session_id']='f'*32;store.save(state)
        with self.assertRaises(domain.DomainError):integration.import_run(self.pid,integration.ImportInput(run_id=RID))

    def test_no_result_or_missing_geometry_success(self):
        source=self.data/'asap/sessions'/SID/'runs'/RID/'result.json'
        result=storage.read_json(source)
        bad=copy.deepcopy(result);bad['sequence_status']='failed'
        with self.assertRaises(domain.DomainError):integration.validate_result(bad)
        bad=copy.deepcopy(result);bad['assembly_order'].append(bad['base_part'])
        with self.assertRaises(domain.DomainError):integration.validate_result(bad)
        bad=copy.deepcopy(result);bad['steps'][0]['path']=[]
        with self.assertRaises(domain.DomainError):integration.validate_result(bad)

    def test_one_source_member_not_assigned_twice(self):
        source=self.data/'asap/sessions'/SID/'runs'/RID/'result.json'
        result=storage.read_json(source);ordered=result['assembly_order']
        result['part_map'][ordered[2]]['member_refs']=copy.deepcopy(result['part_map'][ordered[1]]['member_refs'])
        with self.assertRaises(domain.DomainError):
            integration.adopted_state(store.get(self.pid),result,self.data/'asap/sessions'/SID,source.parent)

    def test_frozen_config_never_uses_development_sibling(self):
        with patch.object(storage.sys,'frozen',True,create=True),patch.dict(os.environ,{},clear=True):
            config=storage.default_config()
        self.assertEqual(config['discovery'],'installed_runtime')
        self.assertTrue(config['engine']['python'].replace('\\','/').endswith('/runtime/asap/python/python.exe'))
        self.assertNotIn('asap-unit-test',json.dumps(config))

    def test_restored_absolute_source_and_logs_without_old_data(self):
        previous=self.data/'absent-old-root'
        storage.write_json(self.data/'path_map.json',{'source_root':str(previous),'destination_root':str(self.data)})
        immutable=self.data/'asap/sessions'/SID/'manifest.json'
        original=hashlib.sha256(immutable.read_bytes()).hexdigest()
        restored=storage.read_json(previous/'asap/sessions'/SID/'manifest.json')
        self.assertEqual(restored['metrics']['solid_count'],4)
        from asap_service.backend.cad_pipeline import _load_brep,shape_features
        directory=storage.resolve_path(previous/'asap/sessions'/SID)
        units=storage.read_json(directory/'units.json')
        self.assertEqual(shape_features(_load_brep(directory,units[0]))['solid_count'],1)
        jid='f'*32
        log=self.data/'asap/jobs'/jid/'worker.log';log.parent.mkdir(parents=True);log.write_text('restored log',encoding='utf-8')
        raw.manager.jobs[jid]={'id':jid,'status':'success','log_file':str(previous/'asap/jobs'/jid/'worker.log')}
        self.assertEqual(raw.manager.get(jid)['logs'],'restored log')
        self.assertEqual(self.client.get('/api/asap/runs/'+RID+'/scene').status_code,200)
        self.assertEqual(original,hashlib.sha256(immutable.read_bytes()).hexdigest())

    def test_restored_runtime_requires_matching_pinned_identity(self):
        runtime=ROOT/'packaging/resources/runtime'
        if not (runtime/'asap/python/python.exe').is_file():self.skipTest('staged distributable runtime not present')
        previous=self.data/'absent-runtime'
        storage.write_json(self.data/'path_map.json',{'source_root':str(previous),'destination_root':str(self.data)})
        engine={'transport':'native','python':str(previous/'python.exe'),'asap_root':str(previous/'ASAP'),
                'weights_path':str(previous/'weights.pt'),'workspace_root':str(previous/'work')}
        config=storage.DATA/'config.json';storage.write_json(config,{'engine':engine})
        original=hashlib.sha256(config.read_bytes()).hexdigest()
        proof={'asap_commit':'3727e716b57e0b508c4e9ecd9fefa6953e8f72da',
               'weights_sha256':'430a51253c25170e8f845ee21834a1e33714225fdf5f87b7cb687e4fe260d99a'}
        storage.write_json(storage.DATA/'environment.json',{'provenance':proof})
        with patch.object(storage.sys,'frozen',True,create=True),patch.dict(os.environ,{'PILOT_RUNTIME_DIR':str(runtime)},clear=True):
            resolved=storage.get_config()
            self.assertEqual(resolved.get('runtime_resolution'),'same_pinned_engine_and_weights_verified_after_restore')
            self.assertEqual(Path(resolved['engine']['python']),runtime/'asap/python/python.exe')
            proof['weights_sha256']='f'*64
            storage.write_json(storage.DATA/'environment.json',{'provenance':proof})
            rejected=storage.get_config()
            self.assertNotIn('runtime_resolution',rejected)
        self.assertEqual(original,hashlib.sha256(config.read_bytes()).hexdigest())


if __name__=='__main__':unittest.main()
