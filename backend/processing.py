"""CAD orchestration used only inside isolated native workers."""
import json
import threading
import time
from pathlib import Path
from . import store, domain, importer
from .domain import DomainError
CAD_LOCK = threading.Lock()

def geometry_checker(state, cancel=None):
    from .tool_access import check_tool
    if state.get('plan',{}).get('asap_run_id'):
        original_hash=state['plan'].get('asap_order_fingerprint')
        moving_order=[oid for oid in state['plan']['ordered_operation_ids'] if any(o['id']==oid and domain.installs(o) for o in state['operations'])]
        path_current=not state['plan'].get('asap_path_stale') and original_hash==store.fingerprint(moving_order)
        def check_recorded(op,assembled):
            if op.get('kind')=='fasten' and op.get('fastening_stage')=='final':
                return check_tool(state,op,assembled)
            motion=op.get('asap_motion') or {}
            valid=path_current and (motion.get('verified') or (motion.get('base') and op.get('kind')=='prepare')) and motion.get('validity','current')=='current'
            result=[domain.make_finding('ASAP_RECORDED_PATH','원시 ASAP의 해당 순서·단위·행렬 경로 검사 증적을 확인했습니다. 현장 공정·공구 검사와 별도입니다.' if valid else '편집한 순서/단위/경로 조건에 맞는 실제 ASAP 검증이 필요합니다.',op['id'],'passed' if valid else 'review_required',
                run_id=motion.get('run_id'),conditions={'method':'verified_recorded_ASAP_path','scope':'rigid_geometry_only','order_fingerprint_match':path_current})]
            if op.get('kind')=='install_flexible':
                result.append(domain.make_finding('FLEXIBLE_UNSUPPORTED','ASAP는 강체 입력입니다. 유연체 변형·압입에 대한 현장 검토가 필요합니다.',op['id']))
            return result+check_tool(state,op,assembled)
        return check_recorded
    from .cad import GeometryEngine
    from .desktop_storage import resolve_path
    manifest_file = state['project'].get('cad_manifest')
    if manifest_file:manifest_file=resolve_path(manifest_file)
    if not manifest_file or not Path(manifest_file).exists():
        return None
    manifest = json.loads(Path(manifest_file).read_text(encoding='utf-8'))
    engine = GeometryEngine(manifest)
    units = {u['id']: u for u in state['units']}
    cache = {}
    started = time.monotonic()

    def check(op, assembled):
        if op.get('kind')=='fasten' and op.get('fastening_stage')=='final':
            return check_tool(state,op,assembled,engine)
        if cancel and cancel():
            raise DomainError('JOB_CANCELED', 'CAD 검증을 취소했습니다.')
        if time.monotonic() - started > 180:
            return [domain.make_finding('JOB_TIMEOUT', '전체 CAD 검사 시간 한도 180초를 초과했습니다. 이 작업은 검사되지 않았습니다.', op['id'], 'timeout')]
        targets = op.get('target_unit_ids', [])
        moving = [m for t in targets if t in units for m in units[t]['member_instance_ids']]
        obstacles = [m for t in assembled if t in units and t not in targets for m in units[t]['member_instance_ids']]
        if not moving:
            return [domain.make_finding('MISSING_GEOMETRY', '대상 CAD 배치가 없습니다.', op['id'], 'review_required')]
        key = (tuple(sorted(moving)), tuple(sorted(obstacles)), op.get('direction', '+Z'), op['kind'], op.get('motion_distance_mm'))
        if key not in cache:
            with CAD_LOCK:
                if op['kind'] == 'prepare':
                    invalid = [i for i in moving + obstacles if not engine.instances.get(i, {}).get('valid')]
                    pairs = [] if invalid else [engine.check_pair(m, o) for m in moving for o in obstacles if o not in moving]
                    severity = {'separated':0, 'contact':1, 'review_required':2, 'calculation_failed':3, 'collision':4}
                    verdict = max(pairs, key=lambda p:severity.get(p['status'],3))['status'] if pairs else 'passed'
                    cache[key] = {'status':'calculation_failed' if invalid else 'passed' if verdict=='separated' else verdict,
                                  'reason':'invalid_shape' if invalid else 'static_initial_placement', 'instance_ids':invalid,
                                  'collisions':[p for p in pairs if p['status']=='collision'],
                                  'conditions':{'method':'static_initial_placement','moving_ids':moving,'assembled_ids':obstacles}}
                else:
                    cache[key] = engine.check_path(moving, obstacles, direction=op.get('direction', '+Z'),
                                                   distance_mm=op.get('motion_distance_mm'), sample_spacing_mm=5, max_samples=101, timeout_s=12)
        result = cache[key]
        status = 'timeout' if result.get('reason') == 'timeout' else result.get('status', 'calculation_failed')
        details = result.get('collisions', [])
        message = {'passed': '직선 경로의 이산 검사 범위 내 간섭이 검출되지 않았습니다.',
                   'contact': '접촉이 확인되었으며 허용오차를 넘는 침투는 검출되지 않았습니다.',
                   'collision': '이동 경로에서 정밀 형상 침투가 검출되었습니다.',
                   'calculation_failed': '정밀 CAD 계산이 실패했습니다.',
                   'review_required': '경로 검사 범위가 부족하거나 지원하지 않는 형상이 있습니다.'}.get(status, '검사 범위를 확인해 주세요.')
        findings = [domain.make_finding('MOTION', message, op['id'], status,
                                       details=details, conditions=result.get('conditions', {}), reason_code=result.get('reason'),
                                       instance_ids=result.get('instance_ids', []), errors=result.get('errors', []),
                                       other_unit_id=next((u['id'] for u in units.values() if details and any(m in str(details[0]) for m in u['member_instance_ids'])), None))]
        if op['kind'] == 'install_flexible':
            findings.append(domain.make_finding('FLEXIBLE_UNSUPPORTED', '유연체 변형·가스켓 압입은 강체 직선 검사로 확인할 수 없습니다.', op['id']))
        return findings+check_tool(state,op,assembled,engine)
    return check


def add_features(state):
    units = {u['id']: u for u in state['units']}
    instances = {i['id']: i for i in state['instances']}
    for op in state['operations']:
        if op['kind'] in domain.INSERT_KINDS:
            size = (state['project'].get('bbox') or {}).get('size', [200,200,200])
            op.setdefault('motion_distance_mm', max(size) * 1.25 + 20)
        members = [instances[m] for u in op.get('target_unit_ids', []) if u in units for m in units[u]['member_instance_ids'] if m in instances]
        bbox = importer.union_bbox(members)
        if bbox:
            volumes = [i.get('volume_mm3') for i in members]
            op['features'] = dict(size=bbox['size'], center=bbox['center'], volume=sum(volumes) if volumes and all(v is not None for v in volumes) else None,
                                  interference_count=sum(1 for f in (state.get('validation') or {}).get('findings', []) if f.get('operation_id') == op['id'] and f.get('status') == 'collision'))
    return state


