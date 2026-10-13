"""Isolated CAD process: OCCT native operations cannot block the local UI server."""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path
from . import store, domain, importer


def write_json(path, value):
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(store.dumps(value), encoding='utf-8')
    tmp.replace(path)


def run_worker(payload_path):
    from .processing import geometry_checker, add_features
    payload_path = Path(payload_path)
    payload = json.loads(payload_path.read_text(encoding='utf-8'))
    folder = payload_path.parent
    state, params, mode = payload['state'], payload.get('params', {}), payload['mode']
    def progress(value, message=''):
        write_json(folder / 'progress.json', dict(progress=value, message=message))
    try:
        if mode == 'import':
            result = importer.import_files(state, params['assembly'], params['parts'], progress=progress)
            add_features(result)
        elif mode == 'validate':
            result = state
            result['validation'] = domain.validate(result, geometry_checker(result), progress=progress)
            result['plan']['status'] = result['validation']['status']
        elif mode == 'generate':
            result = state
            strategy = params.get('strategy', 'heuristic')
            rank = None
            if strategy == 'ml':
                from .learning import model_status, rank_candidates
                if model_status(store.DATA_ROOT / 'models').get('status') != 'available':
                    raise domain.DomainError('TRAINING_UNAVAILABLE','사용 가능한 학습 모델이 없습니다. 유효한 확정 이력을 먼저 학습해 주세요.')
                rank = lambda candidates: rank_candidates(candidates,store.DATA_ROOT / 'models')
            check = geometry_checker(result) if params.get('geometry',False) else None
            proposal = domain.generate(result,check,rank,progress=progress)
            if strategy == 'ml':
                result['ml_plan'] = proposal
            else:
                domain.invalidate(result,proposal['reason'])
                result['plan'].update(proposal)
                for op in result['operations']:
                    op['direction'] = proposal['directions'].get(op['id'],op.get('direction','+Z'))
        else:
            raise ValueError('Unsupported worker mode')
        write_json(folder/'result.json',dict(ok=True,state=result))
    except Exception as exc:
        write_json(folder/'result.json',dict(ok=False,code=getattr(exc,'code','WORKER_FAILED'),message=str(exc),details=getattr(exc,'details',[])))
        (folder/'worker-error.log').write_text(traceback.format_exc(),encoding='utf-8')


def compute(mode,state,params,progress,cancel,timeout_s=300):
    folder = store.DATA_ROOT/'work'/store.uid('worker-')
    folder.mkdir(parents=True,exist_ok=True)
    payload = folder/'input.json'
    write_json(payload,dict(mode=mode,state=state,params=params))
    command = [sys.executable] + ([] if getattr(sys,'frozen',False) else [str(store.APP_ROOT/'launcher.py')]) + ['--worker',str(payload)]
    environment = os.environ.copy() | {'PILOT_DATA_DIR':str(store.DATA_ROOT),'PYTHONUTF8':'1'}
    started=time.monotonic()
    with (folder/'native.log').open('wb') as log:
        process=subprocess.Popen(command,stdout=log,stderr=log,env=environment,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        last=None
        try:
            while process.poll() is None:
                if cancel():
                    raise domain.DomainError('JOB_CANCELED','별도 CAD 계산 프로세스를 종료해 취소했습니다.')
                if time.monotonic()-started > timeout_s:
                    raise domain.DomainError('JOB_TIMEOUT',f'CAD 작업 전체 제한 {timeout_s}초를 초과했습니다. 입력과 기존 확정본은 보존됩니다.')
                progress_path=folder/'progress.json'
                if progress_path.exists():
                    try:
                        data=progress_path.read_text(encoding='utf-8')
                        if data != last:
                            update=json.loads(data)
                            progress(update['progress'],update['message'])
                            last=data
                    except (OSError,ValueError):
                        pass
                time.sleep(.15)
            output=folder/'result.json'
            if process.returncode or not output.exists():
                raise domain.DomainError('WORKER_FAILED',f'CAD 작업자가 비정상 종료했습니다 (code={process.returncode}).')
            result=json.loads(output.read_text(encoding='utf-8'))
            if not result['ok']:
                raise domain.DomainError(result['code'],result['message'],result.get('details'))
            return result['state']
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
