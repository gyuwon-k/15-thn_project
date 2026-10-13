"""Loopback API and launcher. Each expensive job preserves its input revision."""
from __future__ import annotations

import argparse
import copy
import csv
import io
import json
import math
import os
import re
import sys
import threading
import time
import traceback
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Body, FastAPI, File, HTTPException, Request, UploadFile, Form
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import domain, importer, store
from .domain import DomainError
from .processing import add_features, geometry_checker
from .worker import compute

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    store.init()
    from . import asap_integration
    asap_integration.startup()
    try:
        yield
    finally:
        for event in list(CANCEL.values()):event.set()
        asap_integration.shutdown()
        import asyncio
        await asyncio.to_thread(POOL.shutdown,wait=True,cancel_futures=False)
        store.shutdown()

app = FastAPI(title='PILOT Studio', version='1.0.0', lifespan=lifespan)
POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix='pilot-job')
CAD_LOCK = threading.Lock()
CANCEL = {}
SERVER_PORT = int(os.environ.get('PILOT_PORT', '8770'))


@app.middleware('http')
async def local_only(request: Request, call_next):
    host = request.headers.get('host', '').split(':')[0]
    if host not in ('127.0.0.1', 'localhost', '[::1]', 'testserver'):
        return JSONResponse(status_code=403, content={'detail': '로컬 API는 루프백 주소에서만 사용할 수 있습니다.'})
    origin = request.headers.get('origin')
    if origin and not re.fullmatch(r'https?://(localhost|127\.0\.0\.1)(:\d+)?', origin):
        return JSONResponse(status_code=403, content={'detail': '외부 사이트에서의 API 요청을 허용하지 않습니다.'})
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    return response


@app.exception_handler(DomainError)
async def domain_error(_, exc):
    return JSONResponse(status_code=409 if exc.code in ('STALE_VALIDATION', 'REVISION_CONFLICT', 'JOB_BUSY', 'CONFIRMED_IMMUTABLE') else 422,
                        content={'detail': str(exc), 'code': exc.code, 'details': exc.details})


@app.exception_handler(KeyError)
async def missing(_, exc):
    return JSONResponse(status_code=404, content={'detail': f'항목을 찾을 수 없습니다: {exc.args[0]}'})


@app.exception_handler(ValueError)
async def invalid_value(_, exc):
    conflict=str(exc)=='IDEMPOTENCY_CONFLICT'
    return JSONResponse(status_code=409 if conflict else 422, content={'detail': str(exc), 'code': 'IDEMPOTENCY_CONFLICT' if conflict else 'INVALID_INPUT'})


def aggregate(project_id):
    state = store.get(project_id)
    state['snapshots'] = [dict(id=s['id'], status=s['status'], draft=s.get('draft'), revision=s['plan']['revision'],
                               created_at=s['created_at'], confirmed_by=s.get('confirmed_by'), reason=s.get('reason'),
                               snapshot_hash=store.fingerprint(s)) for s in store.snapshots(project_id)]
    state['jobs'] = store.jobs(project_id)
    state['audit'] = store.audit(project_id)
    from .learning import model_status
    state['learning'] = model_status(store.DATA_ROOT / 'models')
    state['learning']['training_state'] = state['learning'].get('training_state', state['learning'].get('status', 'no_history'))
    state['learning']['history_count'] = len(store.snapshots(confirmed_only=True))
    state['metrics']=domain.plan_metrics(state)
    return state


def submit(project_id, kind, fn):
    with store.LOCK:
        for j in store.jobs(project_id):
            if j['status'] in ('queued', 'running'):
                raise DomainError('JOB_BUSY', '진행 중인 작업을 완료하거나 취소한 뒤 다시 시도해 주세요.')
        job = dict(id=store.uid('job-'), project_id=project_id, kind=kind, status='queued', progress=0,
                   message='작업 대기 중', created_at=store.now(), error=None)
        event = threading.Event()
        CANCEL[job['id']] = event
        store.put_job(job)

    def progress(value, message=''):
        job.update(progress=round(float(value), 4), message=message or job['message'])
        if event.is_set():
            job['message'] = '취소 요청됨 · 현재 계산 종료 대기'
        store.put_job(job)

    def run():
        job.update(status='running', started_at=store.now())
        store.put_job(job)
        try:
            result = fn(progress, event.is_set)
            if event.is_set():
                raise DomainError('JOB_CANCELED', '작업을 취소했습니다.')
            job.update(status='succeeded', progress=1, message='작업 완료', result=result or {})
            if isinstance(result, dict):
                for k in ('output_url', 'video_path', 'package_url', 'publish_status', 'snapshot_id'):
                    if k in result:
                        job[k] = result[k]
        except Exception as exc:
            code = getattr(exc, 'code', 'WORKER_FAILED')
            job.update(status='canceled' if code == 'JOB_CANCELED' or event.is_set() else 'failed',
                       message=str(exc), error=code, details=getattr(exc, 'details', []), diagnostic_id=job['id'])
            logs = store.DATA_ROOT / 'logs'
            logs.mkdir(parents=True, exist_ok=True)
            (logs / (job['id'] + '.log')).write_text(traceback.format_exc(), encoding='utf-8')
        finally:
            job['ended_at'] = store.now()
            store.put_job(job)
            CANCEL.pop(job['id'], None)
    POOL.submit(run)
    return {'job_id': job['id']}


def commit_result(project_id, initial, updated, action):
    with store.LOCK:
        current = store.get(project_id)
        if current['plan'].get('status') == 'confirmed' or current['plan'].get('snapshot_id') != initial['plan'].get('snapshot_id') or domain.input_fingerprint(current) != domain.input_fingerprint(initial):
            raise DomainError('REVISION_CONFLICT', '계산 도중 입력이 변경되어 결과를 반영하지 않았습니다. 다시 실행해 주세요.')
        store.save(updated, action)


@app.get('/api/health')
def health():
    return {'status': 'ok', 'service':'pilot-studio-final', 'version': app.version,'spec_version':'1.6', 'data_dir': str(store.DATA_ROOT), 'binding': 'loopback', 'sharing': 'not_configured'}


@app.get('/api/projects')
def list_projects():
    return [s['project'] | {'operation_count': len(s['operations']), 'unit_count': len(s['units']), 'plan_status': s['plan']['status']} for s in store.projects()]


@app.post('/api/projects')
def create_project(body: dict = Body(default={})):
    state = domain.new_project(body)
    store.save(state, 'project_created')
    return aggregate(state['project']['id'])


@app.get('/api/projects/{project_id}')
def get_project(project_id: str):
    return aggregate(project_id)


@app.get('/api/scenes/{sha}')
def get_scene(sha: str):
    if not re.fullmatch(r'[0-9a-f]{64}', sha):
        raise HTTPException(404)
    manifest = store.DATA_ROOT / 'cad-cache' / sha / 'manifest.json'
    if not manifest.exists():
        raise HTTPException(404, 'CAD 장면을 찾을 수 없습니다.')
    data = json.loads(manifest.read_text(encoding='utf-8'))
    from .desktop_storage import resolve_path
    return FileResponse(resolve_path(data['scene_path']), media_type='application/json')


@app.get('/api/snapshots/{snapshot_id}')
def get_snapshot(snapshot_id: str):
    return store.snapshot(snapshot_id)


async def save_upload(upload, folder):
    name = Path((upload.filename or 'unnamed').replace('\\', '/')).name
    if Path(name).suffix.lower() not in ('.step', '.stp'):
        raise DomainError('INVALID_INPUT', f'{name}: STEP 파일만 선택해 주세요.')
    target = folder / store.uid() / name
    target.parent.mkdir(parents=True, exist_ok=True)
    size = 0
    with target.open('wb') as f:
        while data := await upload.read(1024 * 1024):
            size += len(data)
            if size > 512 * 1024 * 1024:
                raise DomainError('FILE_TOO_LARGE', '초기 버전의 파일당 입력 한도는 512MB입니다.')
            f.write(data)
    return target


@app.post('/api/projects/{project_id}/import')
async def upload_models(project_id: str, assembly: UploadFile = File(...), parts: list[UploadFile] = File(default=[]), input_mode: str = Form('assembly_only')):
    initial = store.get(project_id)
    if initial['plan']['status'] == 'confirmed':
        raise DomainError('CONFIRMED_IMMUTABLE', '새 초안에서 파일을 교체해 주세요.')
    if input_mode not in ('assembly_only','assembly_with_parts'):
        raise DomainError('INVALID_INPUT_MODE','입력 방식을 확인하세요.')
    if input_mode=='assembly_with_parts' and not parts:
        raise DomainError('TEMPLATES_REQUIRED','하위 부품 파일을 선택하거나 완제품만 방식으로 변경하세요.')
    folder = store.DATA_ROOT / 'uploads' / store.uid()
    folder.mkdir(parents=True, exist_ok=True)
    assembly_path = await save_upload(assembly, folder)
    part_paths = [await save_upload(p, folder) for p in parts]
    def work(progress, cancel):
        result = compute('import', initial, {'assembly':str(assembly_path),'parts':[str(p) for p in part_paths]}, progress, cancel)
        result['project']['input_mode']=input_mode
        commit_result(project_id, initial, result, 'models_imported')
        return {'status': result['project']['status']}
    return submit(project_id, 'import', work)


@app.post('/api/projects/{project_id}/metadata')
async def import_metadata(project_id: str, file: UploadFile = File(...)):
    raw = await file.read()
    try:
        rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
    except (UnicodeError, csv.Error):
        raise DomainError('INVALID_CSV', 'UTF-8 CSV 파일을 사용해 주세요.')
    if not rows or not {'id', 'company_part_no', 'display_name'} <= set(rows[0]):
        raise DomainError('INVALID_CSV', '필수 열: id, company_part_no, display_name (cad_aliases 선택).')
    if len({r['id'] for r in rows}) != len(rows):
        raise DomainError('DUPLICATE_KEY', 'CSV에 같은 품목 ID가 중복되었습니다.')
    with store.LOCK:
        state = store.get(project_id)
        known = {p['id']: p for p in state['parts']}
        if any(r['id'] not in known for r in rows):
            raise DomainError('INVALID_REFERENCE', 'CSV에 없는 품목 ID가 있습니다.')
        domain.invalidate(state, 'CSV 메타데이터 변경')
        for row in rows:
            part = known[row['id']]
            part.update(company_part_no=row['company_part_no'], display_name=row['display_name'], metadata_status='provided', metadata_source=file.filename)
            if row.get('cad_aliases'):
                part['cad_aliases'] = row['cad_aliases'].split('|')
        sync_parts(state)
        state['versions']['metadata'] += 1
        store.save(state, 'metadata_imported', {'rows': len(rows)})
    return aggregate(project_id)


@app.get('/api/projects/{project_id}/metadata.csv')
def export_metadata(project_id: str):
    from fastapi.responses import Response
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=['id', 'company_part_no', 'display_name', 'cad_aliases'])
    writer.writeheader()
    for p in store.get(project_id)['parts']:
        writer.writerow(dict(id=p['id'], company_part_no=p.get('company_part_no') or '', display_name=p['display_name'], cad_aliases='|'.join(p.get('cad_aliases', []))))
    return Response('\ufeff' + buf.getvalue(), media_type='text/csv; charset=utf-8', headers={'Content-Disposition': 'attachment; filename="metadata.csv"'})


def sync_parts(state):
    parts = {p['id']: p for p in state['parts']}
    units = {u['id']: u for u in state['units']}
    for unit in units.values():
        if unit['part_definition_id'] in parts:
            unit['part_no'] = parts[unit['part_definition_id']].get('company_part_no')
    for op in state['operations']:
        targets = op.get('target_unit_ids', [])
        if len(targets) == 1 and targets[0] in units and op['kind'] in domain.INSERT_KINDS:
            unit = units[targets[0]]
            op.update(part_no=unit.get('part_no'), part_name=parts.get(unit['part_definition_id'], {}).get('display_name', unit['name']))


@app.post('/api/projects/{project_id}/actions/{action}')
def action(project_id: str, action: str, body: dict = Body(default={})):
    with store.LOCK:
        state = store.get(project_id)
        initial = copy.deepcopy(state)
        expected=body.get('expected_revision',body.get('revision'))
        if expected is not None and expected!=state['plan']['revision']:
            raise DomainError('REVISION_CONFLICT','현재 초안이 변경됐습니다. 최신 상태를 확인하세요.')
        if action=='preview_move':
            return domain.preview_move(state,body.get('operation_id'),body.get('target_index'))
        if state['plan']['status'] == 'confirmed' and action in ('load_sample', 'generate', 'validate'):
            raise DomainError('CONFIRMED_IMMUTABLE', '확정본을 유지합니다. 새 초안을 만든 뒤 편집·재검증해 주세요.')
        if action in ('generate', 'validate'):
            def work(progress, cancel):
                result = compute(action, initial, body, progress, cancel)
                commit_result(project_id, initial, result, action)
                return {'status': result['plan']['status']}
            return submit(project_id, action, work)
        if action == 'train':
            def work(progress, cancel):
                from .learning import train
                histories = store.snapshots(confirmed_only=True)
                progress(.1, f'확정 이력 {len(histories)}건 분석')
                result = train(histories, store.DATA_ROOT / 'models')
                progress(1, result.get('reason', '학습 상태 갱신'))
                return result
            return submit(project_id, 'train', work)
        if action == 'confirm':
            if state['plan']['status'] == 'confirmed':
                return aggregate(project_id)
            snapshot = domain.make_snapshot(state, body.get('reviewer'), body.get('reason', ''))
            snapshot['constraints'] = state['rules']
            store.put_snapshot(snapshot, True, {'reason': body.get('reason', ''), 'baseline_order': state['baseline'], 'final_order': state['plan']['ordered_operation_ids'], 'label_source': 'user_confirmation'})
            state['plan'].update(status='confirmed', snapshot_id=snapshot['id'], confirmed_by=body['reviewer'], confirmed_at=store.now())
            store.save(state, 'plan_confirmed', {'snapshot_id': snapshot['id']})
            return aggregate(project_id)
        if action == 'new_draft':
            old = state['plan']
            state['plan'] = copy.deepcopy(old) | dict(id=store.uid('plan-'), revision=old['revision'] + 1, status='draft', origin=old.get('origin','manual'), parent_plan_id=old['id'], reason=body.get('reason', '확정본에서 파생'))
            state['plan'].pop('snapshot_id', None)
            state['validation'] = None
            store.save(state, 'draft_created')
            return aggregate(project_id)
        if action == 'render':
            from .exporter import render_video
            snapshot_id = body.get('snapshot_id') or state['plan'].get('snapshot_id')
            if snapshot_id:
                snapshot = store.snapshot(snapshot_id)
                if snapshot['project']['id'] != project_id:
                    raise DomainError('INVALID_REFERENCE', '다른 프로젝트의 스냅샷입니다.')
            elif body.get('draft'):
                snapshot = domain.make_snapshot(state, draft=True)
                store.put_snapshot(snapshot, False)
            else:
                raise DomainError('REVIEW_REQUIRED', '공식 출력은 확정본이 필요합니다. 검토 중인 계획은 초안 출력으로 진행해 주세요.')
            def work(progress, cancel):
                folder = store.DATA_ROOT / 'exports' / snapshot['id'] / store.uid('render-')
                result = render_video(f'http://127.0.0.1:{SERVER_PORT}', snapshot, folder,
                                      preset='production' if body.get('preset', 'full') == 'full' else 'quick',
                                      progress=progress, cancel=cancel)
                video = Path(result['video_path'])
                result.update(output_url='/artifacts/' + video.relative_to(store.DATA_ROOT / 'exports').as_posix(), snapshot_id=snapshot['id'])
                return result
            return submit(project_id, 'render', work)
        if action == 'package':
            from .exporter import web_package
            job = next((j for j in store.jobs(project_id) if j['id'] == body.get('job_id')), None)
            if not job or job['kind'] != 'render' or job['status'] != 'succeeded':
                raise DomainError('RENDER_REQUIRED', '성공한 MP4 출력 작업을 선택해 주세요.')
            snapshot = store.snapshot(job['result']['snapshot_id'])
            target = store.DATA_ROOT / 'exports' / snapshot['id'] / store.uid('web-')
            result = web_package(job['result'], snapshot, target)
            package = Path(result.get('zip_path') or result.get('package_path'))
            result['package_url'] = '/artifacts/' + package.relative_to(store.DATA_ROOT / 'exports').as_posix()
            job['result'].update(result)
            job.update(package_url=result['package_url'], publish_status='not_published')
            store.put_job(job)
            return aggregate(project_id)
        if action == 'cancel_job':
            job_id = body.get('job_id')
            if job_id not in {j['id'] for j in store.jobs(project_id)}:
                raise DomainError('INVALID_REFERENCE', '작업을 찾을 수 없습니다.')
            if job_id in CANCEL:
                CANCEL[job_id].set()
            return aggregate(project_id)
        if action == 'save_plan':
            if body.get('revision') != state['plan']['revision']:
                raise DomainError('REVISION_CONFLICT', '초안 개정이 변경되었습니다. 새로 불러온 뒤 수정해 주세요.')
            ids = body.get('ordered_operation_ids', [])
            known = {o['id'] for o in state['operations']}
            if any(i not in known for i in ids) or len(set(ids)) != len(ids):
                raise DomainError('INVALID_PLAN', '중복 또는 없는 작업이 포함되어 있습니다.')
            positions={oid:n for n,oid in enumerate(ids)}
            violations=[r for r in state['rules'] if r.get('strength','hard')=='hard' and r['before_operation_id'] in positions and r['after_operation_id'] in positions and positions[r['before_operation_id']]>=positions[r['after_operation_id']]]
            if violations:raise DomainError('PRECEDENCE_VIOLATION','필수 선행 규칙을 위반하는 순서입니다.',violations)
            for op in state['operations']:
                if op.get('asap_path'):op['path_validity']='stale'
                if op.get('asap_motion'):op['asap_motion']['validity']='stale'
            if state['plan'].get('asap_run_id'):state['plan']['asap_path_stale']=True
            domain.invalidate(state, body.get('reason', '사용자 순서 편집'))
            state['plan'].update(ordered_operation_ids=ids, origin='manual')
        elif action == 'update_operation':
            operation = next((o for o in state['operations'] if o['id'] == body.get('id')), None)
            if not operation:
                raise DomainError('INVALID_REFERENCE', '작업을 찾을 수 없습니다.')
            initial_op=copy.deepcopy(operation)
            allowed = {'title', 'group_id', 'kind', 'target_unit_ids', 'part_no', 'part_name', 'direction', 'duration_s', 'torque', 'warning', 'camera_id', 'source_ref', 'review_status','tool_id','tool_axis','fastening_stage','access_review','height_offset_mm','recommended_directions','motion_distance_mm','tool_contact_point_mm'}
            patch = {k:v for k,v in body.items() if k in allowed}
            if 'duration_s' in patch and (not isinstance(patch['duration_s'], (int,float)) or not math.isfinite(patch['duration_s']) or not .1 <= patch['duration_s'] <= 120):
                raise DomainError('INVALID_DURATION', '재생 시간은 0.1~120초로 입력해 주세요.')
            if patch.get('direction', '+Z') not in domain.DIRECTIONS or patch.get('kind', operation['kind']) not in domain.KINDS:
                raise DomainError('INVALID_INPUT', '지원하지 않는 작업 종류 또는 방향입니다.')
            domain.validate_operation(state,operation|patch)
            domain.invalidate(state, body.get('reason', '작업 정보 변경'))
            target_changed = 'target_unit_ids' in patch and patch['target_unit_ids'] != operation.get('target_unit_ids')
            operation.update(patch)
            if operation.get('asap_path') and any(k in patch for k in ('direction','target_unit_ids','height_offset_mm','motion_distance_mm')):
                operation['path_validity']='stale'
            if operation.get('asap_motion') and any(k in patch and patch[k]!=initial_op.get(k) for k in ('direction','target_unit_ids','height_offset_mm','motion_distance_mm','kind','fastening_stage')):
                operation['asap_motion']['validity']='stale';state['plan']['asap_path_stale']=True
                if any(k in patch and patch[k]!=initial_op.get(k) for k in ('motion_distance_mm','height_offset_mm')):
                    operation['asap_motion']['parameters_changed']=True
            if target_changed:
                sync_parts(state)
                operation.update(torque=None, warning={'status':'missing','text':'','source_ref':None}, review_status='review_required')
            state['versions']['metadata'] += 1
        elif action == 'create_operation':
            domain.invalidate(state, '작업 정의 추가')
            operation = dict(id=store.uid('op-'), title=body.get('title') or '새 작업', kind=body.get('kind', 'place'),
                             target_unit_ids=body.get('target_unit_ids', []), group_id=body.get('group_id', 'manual'),
                             part_no=None, part_name='', direction=body.get('direction', '+Z'), duration_s=body.get('duration_s', 3),
                             torque=body.get('torque'), warning=body.get('warning', {'status':'missing','text':''}),
                             source_ref='사용자 등록', review_status='review_required')
            for key in ('part_no','part_name','tool_id','fastening_stage','access_review'):
                if key in body:operation[key]=body[key]
            domain.validate_operation(state,operation)
            state['operations'].append(operation)
            state['baseline'].append(operation['id'])
            state['plan']['ordered_operation_ids'].append(operation['id'])
            sync_parts(state)
        elif action == 'delete_operation':
            oid = body.get('id')
            if any(r['before_operation_id'] == oid or r['after_operation_id'] == oid for r in state['rules']):
                raise DomainError('REFERENCED_OPERATION', '규칙이 참조하는 작업입니다. 해당 규칙을 먼저 수정해 주세요.')
            domain.invalidate(state, '작업 정의 삭제')
            state['operations'] = [o for o in state['operations'] if o['id'] != oid]
            state['baseline'] = [i for i in state['baseline'] if i != oid]
            state['plan']['ordered_operation_ids'] = [i for i in state['plan']['ordered_operation_ids'] if i != oid]
        elif action == 'save_rules':
            rules = body.get('rules', [])
            domain.check_rules(state['operations'], rules)
            domain.invalidate(state, '공정 규칙 변경')
            state['rules'] = [r | {'id': r.get('id') or store.uid('rule-')} for r in rules]
            state['versions']['rules'] += 1
        elif action=='tool_update':
            tools=state.setdefault('tools',[])
            tool=next((t for t in tools if t['id']==body.get('id')),None)
            if body.get('id') and not tool:raise DomainError('INVALID_TOOL','공구를 찾을 수 없습니다.')
            item={k:v for k,v in body.items() if k in {'name','manufacturer','model','type','assembly_id','envelope','change_time_s','source_ref','reviewed'}}
            item={'type':'manual','change_time_s':0.,'reviewed':False,**(tool or {}),**item}
            if not str(item.get('name','')).strip() or item['type'] not in ('manual','powered','fixture','none'):
                raise DomainError('INVALID_TOOL','공구명과 유형을 확인하세요.')
            seconds=item['change_time_s']
            if isinstance(seconds,bool) or not isinstance(seconds,(int,float)) or not math.isfinite(seconds) or not 0<=seconds<=3600:
                raise DomainError('INVALID_TOOL','공구 교체 시간은 0~3600초입니다.')
            if item.get('envelope'):
                for key in ('radius_mm','length_mm'):
                    v=item['envelope'].get(key)
                    if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or v<=0:
                        raise DomainError('INVALID_TOOL','공구 외형의 반경과 길이는 양수 mm로 입력하세요.')
            domain.invalidate(state,'공구 구성 변경');state['versions']['metadata']+=1
            if tool:tool.update(item)
            else:tools.append(item|{'id':store.uid('tool-')})
        elif action=='tool_delete':
            if any(op.get('tool_id')==body.get('id') for op in state['operations']):
                raise DomainError('REFERENCED_TOOL','작업이 참조하는 공구입니다. 먼저 작업의 공구를 변경하세요.')
            domain.invalidate(state,'공구 제거')
            state['tools']=[t for t in state.get('tools',[]) if t['id']!=body.get('id')]
        elif action=='save_exception':
            if body.get('kind') not in ('contact_review','flexible_process') or not body.get('reviewer') or not str(body.get('reason','')).strip():
                raise DomainError('INVALID_EXCEPTION','예외 유형·검토자·구체적인 근거를 입력하세요.')
            if body.get('operation_id') not in {o['id'] for o in state['operations']}:
                raise DomainError('INVALID_REFERENCE','예외의 작업을 선택하세요.')
            if not set(body.get('unit_ids',[]))<={u['id'] for u in state['units']}:
                raise DomainError('INVALID_REFERENCE','예외 단위를 확인하세요.')
            op=next(o for o in state['operations'] if o['id']==body['operation_id'])
            if set(body.get('unit_ids',[]))!=set(op.get('target_unit_ids',[])):
                raise DomainError('INVALID_EXCEPTION','검토 범위는 해당 작업의 대상 단위와 정확히 같아야 합니다.')
            if body['kind']=='flexible_process' and op['kind']!='install_flexible':
                raise DomainError('INVALID_EXCEPTION','유연체 공정 검토는 유연체 설치 작업에만 적용합니다.')
            domain.invalidate(state,'범위 한정 공정 검토 기록')
            state['exceptions'].append({k:body.get(k) for k in ('operation_id','unit_ids','kind','reviewer','reason')}|{'id':store.uid('exception-'),'created_at':store.now(),'scope':'review_record_only_no_collision_bypass','scope_hash':domain.exception_scope(state,op)})
            state['versions']['exceptions']+=1
        elif action == 'confirm_mapping':
            if not str(body.get('reviewer', '')).strip():
                raise DomainError('MISSING_REVIEWER', '대응 검토자를 입력해 주세요.')
            selected = set(body.get('unit_ids', []))
            if not selected or not selected <= {u['id'] for u in state['units']}:
                raise DomainError('INVALID_REFERENCE', '검토할 조립 단위를 선택해 주세요.')
            domain.invalidate(state, 'CAD 대응 검토')
            for unit in state['units']:
                if unit['id'] in selected:
                    part = next(p for p in state['parts'] if p['id'] == unit['part_definition_id'])
                    if not part.get('company_part_no'):
                        raise DomainError('MISSING_METADATA', '공식 품번을 먼저 입력해 주세요.')
                    unit.update(mapping_status='confirmed', reviewer=body['reviewer'], reviewed_at=store.now())
            state['versions']['mapping'] += 1
            if all(u['mapping_status'] == 'confirmed' for u in state['units']) and all(f['parse_status'] not in ('failed','partial') for f in state['files'] if f['input_revision'] == state['project']['input_revision']):
                state['project']['status'] = 'ready'
        elif action == 'save_units':
            units = body.get('units', [])
            members = [m for u in units for m in u.get('member_instance_ids', [])]
            if len(members) != len(set(members)) or set(members) != {i['id'] for i in state['instances']}:
                raise DomainError('INVALID_MEMBERSHIP', '각 CAD 배치는 정확히 하나의 조립 단위에 포함되어야 합니다.')
            if len({u['id'] for u in units}) != len(units) or any(not u.get('member_instance_ids') for u in units):
                raise DomainError('INVALID_UNITS', '단위 ID 중복 또는 빈 단위가 있습니다.')
            part_ids = {p['id'] for p in state['parts']}
            if any(u.get('part_definition_id') not in part_ids for u in units):
                raise DomainError('INVALID_REFERENCE', '품목 정의에 없는 단위가 있습니다.')
            domain.invalidate(state, '조립 단위 묶음 변경')
            state['units'] = [u | {'mapping_status':'candidate','reviewer':None} for u in units]
            state['versions']['mapping'] += 1
            state['project']['status'] = 'mapping_required'
            if state['plan'].get('asap_run_id'):state['plan']['asap_path_stale']=True
        elif action == 'update_project':
            domain.invalidate(state, '제품 정보 변경')
            for key in ('name','vehicle_model','item_name','product_no'):
                if key in body:
                    state['project'][key] = str(body[key])
            state['versions']['metadata'] += 1
        elif action == 'update_part':
            part = next((p for p in state['parts'] if p['id'] == body.get('id')), None)
            if not part:
                raise DomainError('INVALID_REFERENCE', '품목을 찾을 수 없습니다.')
            domain.invalidate(state, '품목 메타데이터 변경')
            part.update({k:v for k,v in body.items() if k in ('company_part_no','display_name','cad_aliases','metadata_source')})
            part['metadata_status'] = 'provided'
            sync_parts(state)
            state['versions']['metadata'] += 1
        else:
            raise HTTPException(404, '지원하지 않는 동작입니다.')
        domain.refresh_asap_validity(state)
        add_features(state)
        store.save(state, action, {'plan_revision': state['plan']['revision']})
    return aggregate(project_id)


from . import asap_integration
app.include_router(asap_integration.router)
app.mount('/api/asap', asap_integration.app)
from .storage_routes import router as storage_router
app.include_router(storage_router)
from .commands import router as command_router
app.include_router(command_router)
store.DATA_ROOT.joinpath('exports').mkdir(parents=True, exist_ok=True)
app.mount('/artifacts', StaticFiles(directory=store.DATA_ROOT / 'exports'), name='artifacts')
FRONTEND = store.RESOURCE_ROOT / 'frontend' / 'dist'
if FRONTEND.exists():
    app.mount('/', StaticFiles(directory=FRONTEND, html=True), name='frontend')


def run():
    global SERVER_PORT
    parser = argparse.ArgumentParser(description='PILOT Studio local desktop application')
    parser.add_argument('--port', type=int, default=SERVER_PORT)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    SERVER_PORT = args.port
    if args.self_test:
        from .cad import ALGORITHM_VERSION, SETTINGS
        print(json.dumps({'version':app.version,'cad':ALGORITHM_VERSION,'settings':SETTINGS,'data_dir':str(store.DATA_ROOT)}, ensure_ascii=True))
        return
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=SERVER_PORT, log_level='info')


if __name__ == '__main__':
    run()
