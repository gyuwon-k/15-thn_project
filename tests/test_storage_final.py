"""Real MariaDB transactions and backup invariants (no backend test double)."""
import json
from pathlib import Path
import zipfile
import pytest
from backend import store
from backend import desktop_storage as service

@pytest.fixture
def storage(tmp_path):
    store.shutdown()
    old=store.DATA_ROOT
    store.DATA_ROOT=tmp_path/'data'
    store.init()
    yield store.DATA_ROOT
    store.shutdown()
    store.DATA_ROOT=old

def seed():
    state={'project':{'id':'project-test','name':'한글 프로젝트'},'sources':[],'units':[],'plan':{'revision':1}}
    store.save(state,'created')
    return state

def test_real_mariadb_dpapi_and_snapshot_integrity(storage):
    state=seed()
    assert service.status()['backend']=='mariadb'
    assert service.status()['status']=='connected'
    config=(storage/'config'/'mariadb.protected.json').read_text()
    assert service._CONFIG['password'] not in config
    assert not (storage/'config'/'bootstrap.sql').exists()
    snapshot={'id':'snapshot-test',**state}
    store.put_snapshot(snapshot,True,{'reason':'확정'})
    assert store.snapshot(snapshot['id'])==snapshot
    with store.connect() as con:store._exec(con,'UPDATE snapshots SET body=%s WHERE id=%s',(store.dumps(snapshot|{'extra':'tampered'}),snapshot['id']))
    with pytest.raises(ValueError,match='SNAPSHOT_INTEGRITY_FAILED'):store.snapshot(snapshot['id'])

def test_command_transaction_rollback_and_replay(storage):
    seed()
    with pytest.raises(RuntimeError):
        with store.transaction_scope():
            state=store.get('project-test');state['project']['name']='취소될 변경'
            store.save(state)
            store.save_request_response('request-fail','a'*64,{'saved':True})
            raise RuntimeError('transaction aborted')
    assert store.get('project-test')['project']['name']=='한글 프로젝트'
    assert store.request_response('request-fail','a'*64) is None
    with store.transaction_scope():store.save_request_response('request-good','b'*64,{'saved':True})
    assert store.request_response('request-good','b'*64)=={'saved':True}
    with pytest.raises(ValueError,match='IDEMPOTENCY_CONFLICT'):store.request_response('request-good','c'*64)

def test_backup_covers_asap_and_restores_to_new_private_database(storage):
    state=seed();store.put_snapshot({'id':'snapshot-test',**state},True)
    mesh=storage/'asap'/'sessions'/'session1'/'source'/'shape.obj'
    mesh.parent.mkdir(parents=True);mesh.write_bytes(b'original mesh')
    backup=service.create_backup();path=storage/'backups'/(backup['id']+'.zip')
    manifest=service.validate_backup(path)
    assert manifest['source_backend']=='mariadb'
    assert any(f['path']=='data/asap/sessions/session1/source/shape.obj' for f in manifest['files'])
    with zipfile.ZipFile(path) as archive:
        assert not any('protected' in name or 'bootstrap.sql' in name for name in archive.namelist())
    restored=service.restore_backup(path)
    assert restored['status']=='verified' and restored['activated'] is False
    assert (Path(restored['data_dir'])/'asap/sessions/session1/source/shape.obj').read_bytes()==b'original mesh'
    assert store.get('project-test')==state
    # The restored instance starts after the original closes, proving independent
    # datadir ownership rather than references into the running source DB.
    store.shutdown();store.DATA_ROOT=Path(restored['data_dir']);store.init()
    assert store.get('project-test')==state
    assert store.snapshot('snapshot-test')['project']['name']=='한글 프로젝트'

def test_active_job_blocks_consistent_backup(storage):
    seed();store.put_job({'id':'job1','project_id':'project-test','created_at':store.now(),'status':'running'})
    with pytest.raises(ValueError,match='ACTIVE_JOB'):service.create_backup()

def test_untrusted_zip_cannot_escape_workspace(tmp_path):
    path=tmp_path/'unsafe.zip'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('../escape.json','payload');archive.writestr('manifest.json','{}')
    with pytest.raises(ValueError,match='BACKUP_UNSAFE_PATH'):service.validate_backup(path)

def test_backup_hash_change_rejected(storage):
    seed();record=service.create_backup();path=storage/'backups'/(record['id']+'.zip')
    replacement=storage/'backups'/'tampered.zip'
    with zipfile.ZipFile(path) as original,zipfile.ZipFile(replacement,'w') as changed:
        for item in original.infolist():
            content=original.read(item.filename)
            if item.filename=='database/pilot.sqlite3':content+=b'tampered'
            changed.writestr(item.filename,content)
    with pytest.raises(ValueError,match='BACKUP_HASH_MISMATCH'):service.validate_backup(replacement)

def test_backup_excludes_running_asap_and_atomic_file_mutations(monkeypatch):
    import threading
    from types import SimpleNamespace
    from backend.asap_integration import raw_api
    manager=SimpleNamespace(guard=threading.Lock(),state_guard=threading.RLock(),jobs={})
    file_guard=threading.Lock()
    monkeypatch.setattr(raw_api,'manager',manager)
    monkeypatch.setattr(raw_api,'file_mutation_lock',file_guard)
    manager.guard.acquire()
    with pytest.raises(ValueError,match='ACTIVE_JOB'):
        with service.maintenance_guard():pass
    manager.guard.release()
    file_guard.acquire()
    with pytest.raises(ValueError,match='ACTIVE_STORAGE_MUTATION'):
        with service.maintenance_guard():pass
    file_guard.release()
    with service.maintenance_guard():
        assert not manager.guard.acquire(blocking=False)
        assert not file_guard.acquire(blocking=False)
    assert manager.guard.acquire(blocking=False)
    manager.guard.release()
