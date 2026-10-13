"""Narrow native capability: open a verified restored storage by record ID."""
from __future__ import annotations
import json, os, re, subprocess, sys, threading, time, uuid
from pathlib import Path

class DesktopApi:
    def __init__(self,persist_default=False):
        # Pywebview exposes public object attributes recursively. Keep the
        # native Window private so only the narrow method is an IPC surface.
        self._window=None
        self._switch=threading.Lock()
        self._persist_default=persist_default
    def activate_restore(self,restore_id):
        if not isinstance(restore_id,str) or not re.fullmatch(r'restore-[0-9a-f]{32}',restore_id):
            return {'status':'failed','code':'RESTORE_ID_INVALID','message':'검증된 복원 ID가 필요합니다.'}
        if not self._switch.acquire(blocking=False):
            return {'status':'failed','code':'RESTORE_SWITCH_BUSY','message':'이미 복원 저장소를 열고 있습니다.'}
        try:
            from backend import store
            from backend.desktop_storage import maintenance_guard
            expected_root=store.DATA_ROOT.parent.resolve()/'restored'
            root=expected_root.resolve()
            candidate=root/restore_id
            destination=candidate.resolve()
            if root!=expected_root or destination.parent!=root or candidate.is_symlink() or getattr(candidate,'is_junction',lambda:False)():
                raise ValueError('RESTORE_PATH_INVALID')
            record=json.loads((destination/'restore-result.json').read_text(encoding='utf-8'))
            if record.get('id')!=restore_id or record.get('status')!='verified' or Path(record['data_dir']).resolve()!=destination:
                raise ValueError('RESTORE_NOT_VERIFIED')
            with maintenance_guard(),store.LOCK:
                with store.connect() as con:
                    if any(json.loads(row['body']).get('status') in ('queued','running') for row in store._fetch(con,'SELECT body FROM jobs')):
                        raise ValueError('ACTIVE_JOB')
                self._open_target(store,destination)
                if self._persist_default:
                    from .settings import remember_storage
                    remember_storage(destination)
            # Release current-storage maintenance locks before normal shutdown.
            if self._window:threading.Timer(.5,self._window.destroy).start()
            return {'status':'opened','data_dir':str(destination),'message':'복원 저장소를 새 독립 창으로 열었습니다.'}
        except Exception as exc:
            return {'status':'failed','code':str(exc).split(':')[0] if isinstance(exc,ValueError) else type(exc).__name__,
                    'message':'복원 창을 열지 못했습니다. 기존 창과 저장소는 유지했습니다. 진단을 확인하세요.'}
        finally:self._switch.release()

    def _open_target(self,store,destination):
        report=destination/'logs'/('activation-'+uuid.uuid4().hex+'.json')
        report.parent.mkdir(exist_ok=True)
        command=[sys.executable]+([] if getattr(sys,'frozen',False) else [str(store.APP_ROOT/'launcher.py')])+['--data-dir',str(destination),'--report',str(report)]
        if self._persist_default:command+=['--restore-activation']
        child=subprocess.Popen(command,env=os.environ|{'PILOT_DATA_DIR':str(destination)},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        deadline=time.monotonic()+120
        while time.monotonic()<deadline:
            if report.is_file():
                ready=json.loads(report.read_text(encoding='utf-8'))
                if ready.get('status')=='loaded':return
                if ready.get('status')=='failed':raise ValueError('RESTORE_START_FAILED')
            if child.poll() is not None:raise ValueError('RESTORE_PROCESS_EXITED')
            time.sleep(.2)
        raise ValueError('RESTORE_START_TIMEOUT')
