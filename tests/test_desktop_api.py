"""Narrow restore activation rejects paths and holds current-storage locks."""
import json, threading
from types import SimpleNamespace
import pytest
from desktop.api import DesktopApi
from backend import store

@pytest.mark.parametrize('value',['../other','restore-../../x','C:/Windows',None,'restore-nothex'])
def test_native_restore_accepts_only_record_id(value):
    assert DesktopApi().activate_restore(value)['code']=='RESTORE_ID_INVALID'

def test_restore_activation_maintains_exclusion_until_target_ready(tmp_path,monkeypatch):
    from backend.asap_integration import raw_api
    store.shutdown();old=store.DATA_ROOT;store.DATA_ROOT=tmp_path/'data';store.init()
    try:
        restore_id='restore-'+'a'*32
        target=store.DATA_ROOT.parent/'restored'/restore_id;target.mkdir(parents=True)
        (target/'restore-result.json').write_text(json.dumps({'id':restore_id,'status':'verified','data_dir':str(target)}),encoding='utf-8')
        manager=SimpleNamespace(guard=threading.Lock(),state_guard=threading.RLock(),jobs={})
        file_guard=threading.Lock()
        monkeypatch.setattr(raw_api,'manager',manager);monkeypatch.setattr(raw_api,'file_mutation_lock',file_guard)
        def ready(self,db,destination):
            assert destination==target
            assert not manager.guard.acquire(blocking=False)
            assert not file_guard.acquire(blocking=False)
            acquired=[]
            def check():
                value=store.LOCK.acquire(blocking=False);acquired.append(value)
                if value:store.LOCK.release()
            thread=threading.Thread(target=check);thread.start();thread.join()
            assert acquired==[False]
        monkeypatch.setattr(DesktopApi,'_open_target',ready)
        assert DesktopApi().activate_restore(restore_id)['status']=='opened'
        assert manager.guard.acquire(blocking=False);manager.guard.release()
        assert file_guard.acquire(blocking=False);file_guard.release()
    finally:store.shutdown();store.DATA_ROOT=old
