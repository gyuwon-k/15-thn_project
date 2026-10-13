"""REQ-AUT / EDT: state-based planning, version checks and immutable timeline inputs."""
from __future__ import annotations

import copy
import math
from collections import Counter

from .store import fingerprint, now, uid

KINDS = {'prepare', 'place', 'fasten', 'mate_connector', 'inspect', 'install_flexible', 'final_view'}
INSERT_KINDS = {'prepare', 'place', 'fasten', 'install_flexible'}
DIRECTIONS = {'+X', '-X', '+Y', '-Y', '+Z', '-Z'}


class DomainError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or []


def new_project(body):
    project_id = uid('project-')
    project = {k: str(body.get(k, '')).strip() for k in ('name', 'vehicle_model', 'item_name', 'product_no')}
    input_mode=body.get('input_mode','assembly_only')
    if input_mode not in ('assembly_only','assembly_with_parts'):
        raise DomainError('INVALID_INPUT_MODE','두 입력 방식 중 하나를 선택하세요.')
    project.update(id=project_id, name=project['name'] or '새 조립 프로젝트', revision=1, input_mode=input_mode,
                   input_revision=0, status='created', created_at=now(), updated_at=now(), scene_url=None,
                   coordinate_convention='world mm +Z up; display (x,z,-y)/1000')
    return dict(project=project, files=[], parts=[], instances=[], units=[], operations=[], rules=[], exceptions=[], tools=[],
                baseline=[], ml_plan=None, plan=dict(id=uid('plan-'), revision=1, status='draft', origin='manual',
                ordered_operation_ids=[], reason='최초 초안'), validation=None,
                learning=dict(training_state='no_history', history_count=0, model_version=None, reason='확정된 학습 이력이 없습니다.'),
                versions=dict(mapping=0, rules=0, metadata=0, exceptions=0), decisions=[], groups=[])


def dependencies(state):
    return {k: state.get(k, []) for k in ('project', 'files', 'parts', 'instances', 'units', 'operations', 'rules', 'exceptions', 'versions','tools','groups')} | {
        'plan': {k: state['plan'].get(k) for k in ('id', 'revision', 'ordered_operation_ids')}
    }


def input_fingerprint(state):
    data = copy.deepcopy(dependencies(state))
    data['project'].pop('updated_at', None)
    data['project'].pop('status', None)
    return fingerprint(data)


def invalidate(state, reason='편집 후 전체 재검증이 필요합니다.'):
    if state['plan']['status'] == 'confirmed':
        raise DomainError('CONFIRMED_IMMUTABLE', '확정본은 직접 수정할 수 없습니다. 새 초안을 만들어 주세요.')
    state['plan']['revision'] += 1
    state['plan']['status'] = 'draft'
    state['plan']['reason'] = reason
    if state.get('validation'):
        state['validation']['stale'] = True


def installs(op):
    return op.get('kind') in INSERT_KINDS and not (op.get('kind')=='fasten' and op.get('fastening_stage')=='final')


def refresh_asap_validity(state):
    if not state['plan'].get('asap_run_id'):return
    ops={o['id']:o for o in state['operations']};units={u['id']:u for u in state['units']}
    moving_order=[oid for oid in state['plan']['ordered_operation_ids'] if oid in ops and installs(ops[oid])]
    matches=fingerprint(moving_order)==state['plan'].get('asap_order_fingerprint')
    for op in state['operations']:
        motion=op.get('asap_motion')
        if not motion:continue
        expected=[]
        for ref in motion.get('original_members',[]):
            solid=ref.get('solid_indices')
            expected.append(ref['occurrence_id'] if solid is None else ref['occurrence_id']+'#solids-'+','.join(map(str,solid)))
        targets=op.get('target_unit_ids',[])
        current=matches and targets==[motion.get('unit_id')] and sorted(units.get(targets[0],{}).get('member_instance_ids',[]))==sorted(expected) if targets else False
        current=current and op.get('direction','+Z')==motion.get('original_direction','+Z') and not op.get('height_offset_mm') and not motion.get('parameters_changed')
        original_kind=motion.get('original_kind','prepare' if motion.get('base') else 'place')
        kind_compatible=op.get('kind')==original_kind or (original_kind=='place' and op.get('kind') in ('fasten','install_flexible') and installs(op))
        current=current and kind_compatible
        motion['validity']='current' if current else 'stale'
        matches=matches and current
    state['plan']['asap_path_stale']=not matches


def exception_scope(state,op):
    units={u['id']:u for u in state['units']}
    order=state['plan']['ordered_operation_ids'];position=order.index(op['id']) if op['id'] in order else 0
    return fingerprint({'input_revision':state['project'].get('input_revision'),'kind':op['kind'],
        'targets':op.get('target_unit_ids',[]),'members':[units[t].get('member_instance_ids',[]) for t in op.get('target_unit_ids',[]) if t in units],
        'direction':op.get('direction'),'tool_id':op.get('tool_id'),'prefix':order[:position]})


def check_rules(operations, rules):
    op_ids = {o['id'] for o in operations}
    predecessors = {i: set() for i in op_ids}
    for r in rules:
        if r.get('strength','hard') not in ('hard','preferred'):
            raise DomainError('INVALID_RULE','규칙 강도는 필수 또는 선호로 지정하세요.')
        before, after = r.get('before_operation_id'), r.get('after_operation_id')
        if before not in op_ids or after not in op_ids:
            raise DomainError('INVALID_REFERENCE', '규칙에 없는 작업 ID가 포함되어 있습니다.', [r])
        if r.get('strength', 'hard') == 'hard':
            predecessors[after].add(before)
    pending = {i: set(v) for i, v in predecessors.items()}
    while pending:
        free = {i for i, v in pending.items() if not v}
        if not free:
            raise DomainError('CONSTRAINT_CYCLE', '필수 선후 관계가 순환합니다.', sorted(pending))
        pending = {i: v - free for i, v in pending.items() if i not in free}
    return predecessors


def make_finding(code, reason, operation_id=None, status='review_required', **extra):
    return dict(check_type=code, reason=reason, operation_id=operation_id, status=status, **extra)


def structural_findings(state):
    findings, plan = [], state['plan']
    order = plan['ordered_operation_ids']
    ops = {o['id']: o for o in state['operations']}
    units = {u['id']: u for u in state['units']}
    parts = {p['id']: p for p in state.get('parts', [])}
    for field in ('vehicle_model', 'item_name', 'product_no'):
        if not state['project'].get(field):
            findings.append(make_finding('MISSING_METADATA', f'제품 정보 {field}가 미입력입니다.'))
    if state['project'].get('status') not in ('ready', 'mapping_required'):
        findings.append(make_finding('INPUT_INCOMPLETE', '완제품과 부품 STEP 입력이 완료되지 않았습니다.'))
    for source in state.get('files', []):
        if source.get('input_revision') == state['project'].get('input_revision') and source.get('parse_status') in ('failed', 'partial', 'review_required'):
            findings.append(make_finding('SOURCE_GEOMETRY_REVIEW', f"{source.get('original_name')}: 일부 형상 오류 또는 미검토 항목이 있습니다.", file_id=source['id']))
    if not units:
        findings.append(make_finding('INPUT_INCOMPLETE', '조립 단위가 없습니다.'))
    for part in parts.values():
        expected = part.get('expected_quantity')
        actual = sum(u.get('part_definition_id') == part['id'] for u in units.values())
        if expected is not None and actual != expected:
            findings.append(make_finding('QUANTITY_MISMATCH', f"{part.get('display_name')}: 기준 수량 {expected}, 조립 단위 {actual}. 수량 차이를 검토해 주세요.", part_id=part['id'], expected=expected, actual=actual))
    for u in units.values():
        part = parts.get(u.get('part_definition_id'))
        if not part or not str(part.get('company_part_no') or '').strip() or not str(part.get('display_name') or '').strip():
            findings.append(make_finding('MISSING_PART_METADATA', f"{u.get('name',u['id'])}: 공식 품번·부품명이 미입력입니다.", unit_id=u['id']))
        if u.get('mapping_status') != 'confirmed':
            findings.append(make_finding('MAPPING_REQUIRED', f"{u.get('name',u['id'])}: CAD 대응 검토가 필요합니다.", unit_id=u['id']))
    member_counts = Counter(m for u in units.values() for m in u['member_instance_ids'])
    duplicate_members = [i for i, n in member_counts.items() if n > 1]
    if duplicate_members:
        findings.append(make_finding('DUPLICATE_MEMBERS', '서로 다른 조립 단위에 같은 CAD 배치가 포함되었습니다.', status='invalid', instance_ids=duplicate_members))
    missing_members = {i['id'] for i in state['instances']} - set(member_counts)
    if missing_members:
        findings.append(make_finding('UNMAPPED_INSTANCES', '조립 단위에 포함되지 않은 CAD 배치가 있습니다.', instance_ids=sorted(missing_members)))
    for oid, count in Counter(order).items():
        if count > 1:
            findings.append(make_finding('DUPLICATE_OPERATION', '동일 작업이 여러 번 배치되었습니다.', oid, 'invalid'))
    for oid in set(order) - ops.keys():
        findings.append(make_finding('INVALID_REFERENCE', '존재하지 않는 작업입니다.', oid, 'invalid'))
    for oid in ops.keys() - set(order):
        findings.append(make_finding('MISSING_OPERATION', '필수 작업이 계획에서 빠졌습니다.', oid, 'invalid'))
    installed = Counter()
    position = {oid: n for n, oid in enumerate(order)}
    for oid in order:
        if oid not in ops:
            continue
        op = ops[oid]
        targets = op.get('target_unit_ids', [])
        if op['kind'] not in KINDS:
            findings.append(make_finding('INVALID_KIND', '지원하지 않는 작업 종류입니다.', oid, 'invalid'))
        if any(u not in units for u in targets):
            findings.append(make_finding('INVALID_TARGET', '대상 단위가 존재하지 않습니다.', oid, 'invalid'))
        if installs(op):
            installed.update(targets)
            if not targets:
                findings.append(make_finding('MISSING_TARGET', '설치 대상이 없습니다.', oid, 'invalid'))
            if not str(op.get('part_no') or '').strip() or not str(op.get('part_name') or '').strip():
                findings.append(make_finding('MISSING_OPERATION_METADATA', '설치 작업의 품번·부품명이 미입력입니다.', oid))
            if len(targets) == 1 and targets[0] in units:
                target_part = parts.get(units[targets[0]].get('part_definition_id'), {})
                if op.get('part_no') != target_part.get('company_part_no'):
                    findings.append(make_finding('PART_METADATA_MISMATCH', '작업의 품번과 대상 품목의 공식 품번이 다릅니다.', oid, 'invalid'))
        if op['kind'] == 'fasten' and len(targets) != 1:
            findings.append(make_finding('BOLT_INDIVIDUAL', '체결 작업은 개별 단위 한 개씩 등록해야 합니다.', oid, 'invalid'))
        if op['kind']=='fasten' and op.get('fastening_stage')=='final' and any(installed[u]==0 for u in targets if u in units):
            findings.append(make_finding('FASTEN_NOT_INSTALLED','최종 체결 전 해당 볼트의 설치/가체결이 필요합니다.',oid,'invalid'))
        if op['kind'] == 'mate_connector' and (not targets or any(installed[u] == 0 for u in targets)):
            findings.append(make_finding('CONNECTOR_NOT_INSTALLED', '연결 대상이 아직 조립되지 않았습니다.', oid, 'invalid'))
        if op.get('review_status') != 'reviewed':
            findings.append(make_finding('PROCESS_REVIEW', '작업 대상·순서·공구 접근·정보의 공정 검토가 필요합니다.', oid))
        if op.get('warning', {}).get('status', 'missing') == 'missing':
            findings.append(make_finding('MISSING_WARNING', '주의사항을 입력하거나 검토 후 특이사항 없음을 명시해 주세요.', oid))
        warning = op.get('warning') or {}
        if warning.get('status') not in ('provided', 'explicit_none', 'missing') or (warning.get('status') == 'provided' and (not str(warning.get('text') or '').strip() or not str(warning.get('source_ref') or '').strip())):
            findings.append(make_finding('INVALID_WARNING', '주의사항 상태·본문·출처를 확인해 주세요.', oid))
        if op['kind'] == 'fasten' and not op.get('torque'):
            findings.append(make_finding('MISSING_TORQUE', '출처가 있는 토크 정보가 필요합니다.', oid))
        if op['kind']=='fasten':
            tool=next((t for t in state.get('tools',[]) if t['id']==op.get('tool_id')),None)
            if not tool:
                findings.append(make_finding('MISSING_TOOL','체결 공구를 지정하거나 무공구 작업으로 명시하세요.',oid))
            elif not tool.get('reviewed'):
                findings.append(make_finding('TOOL_REVIEW','공구 규격·구성/소켓을 검토하세요.',oid,tool_id=tool['id']))
            access=op.get('access_review',{})
            if access.get('status')=='reviewed' and not (str(access.get('reviewer','')).strip() and str(access.get('reason','')).strip()):
                findings.append(make_finding('INVALID_ACCESS_REVIEW','접근성 검토자와 근거를 입력하세요.',oid))
        if op.get('torque'):
            torque = op['torque']
            try:
                lo, hi = float(torque['min']), float(torque['max'])
                if not (math.isfinite(lo) and math.isfinite(hi) and 0 <= lo <= hi and str(torque.get('unit') or '').strip() and str(torque.get('source_ref') or '').strip()):
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                findings.append(make_finding('INVALID_TORQUE', '토크 범위·단위·출처가 유효하지 않습니다.', oid))
        if op.get('direction', '+Z') not in DIRECTIONS:
            findings.append(make_finding('UNSUPPORTED_PATH', '초기 버전은 ±X/±Y/±Z 직선 이동을 지원합니다.', oid))
        try:
            if not math.isfinite(float(op.get('duration_s', 0))) or not 0.1 <= float(op['duration_s']) <= 120:
                raise ValueError()
        except (ValueError, TypeError):
            findings.append(make_finding('INVALID_DURATION', '재생 시간은 0.1~120초여야 합니다.', oid, 'invalid'))
    for target in units:
        if installed[target] == 0:
            findings.append(make_finding('MISSING_UNIT', f"조립 단위 {units[target].get('name',target)} 설치가 누락되었습니다.", status='invalid', unit_id=target))
        elif installed[target] > 1:
            findings.append(make_finding('DUPLICATE_INSTALL', f"조립 단위 {units[target].get('name',target)}가 중복 신규 설치됩니다.", status='invalid', unit_id=target))
    try:
        check_rules(state['operations'], state['rules'])
        for rule in state['rules']:
            a, b = rule['before_operation_id'], rule['after_operation_id']
            if rule.get('strength', 'hard') == 'hard' and a in position and b in position and position[a] >= position[b]:
                findings.append(make_finding('PRECEDENCE_VIOLATION', '필수 선행 작업보다 먼저 배치되었습니다.', b, 'invalid', rule_id=rule['id'], before_operation_id=a))
    except DomainError as e:
        findings.append(make_finding(e.code, str(e), status='invalid', details=e.details))
    return findings


def validate(state, geometry_check=None, progress=None, cancel=None):
    findings = structural_findings(state)
    order = state['plan']['ordered_operation_ids']
    ops = {o['id']: o for o in state['operations']}
    assembled = []
    for n, oid in enumerate(order):
        if cancel and cancel():
            raise DomainError('JOB_CANCELED', '검증을 취소했습니다.')
        op = ops.get(oid)
        if not op:
            continue
        if progress:
            progress(n / max(len(order), 1), f'{n+1}/{len(order)} · {op["title"]}')
        if installs(op) or (op.get('kind')=='fasten' and op.get('fastening_stage')=='final'):
            if geometry_check:
                findings.extend(geometry_check(op, assembled))
            else:
                findings.append(make_finding('GEOMETRY_UNAVAILABLE', '정밀 CAD 검사가 실행되지 않았습니다.', oid))
            for target in op.get('target_unit_ids', []) if installs(op) else []:
                if target not in assembled:
                    assembled.append(target)
        elif op['kind'] in ('mate_connector', 'inspect'):
            findings.append(make_finding('PROCESS_INSTRUCTION', '공법 안내 단계입니다. 실제 장비 검사·연결 합격을 의미하지 않습니다.', oid, 'info'))
    failures = {'invalid', 'collision', 'calculation_failed', 'failed'}
    for f in findings:
        if f['check_type']!='FLEXIBLE_UNSUPPORTED' or f['status']!='review_required':continue
        op=ops.get(f.get('operation_id'))
        if not op:continue
        approved=next((e for e in state.get('exceptions',[]) if e.get('kind')=='flexible_process' and e.get('operation_id')==op['id'] and e.get('scope_hash')==exception_scope(state,op)),None)
        if approved:
            f.update(status='exception',exception_id=approved['id'],reviewer=approved['reviewer'],
                reason='유연체 공정의 제한된 현장 검토 기록을 적용했습니다. 변형 물리 시뮬레이션은 수행하지 않았습니다.',approved_scope=approved)
    status = ('invalid' if any(f['status'] in failures for f in findings) else
              'review_required' if any(f['status'] in ('review_required', 'unsupported', 'timeout') for f in findings) else
              'validated_with_exceptions' if any(f['status'] == 'exception' for f in findings) else 'validated')
    return dict(id=uid('validation-'), status=status, findings=findings, input_fingerprint=input_fingerprint(state), stale=False,
                settings=dict(length_unit='mm', directions=sorted(DIRECTIONS), scope='rigid six-axis straight sampled motion',
                              excluded=['manufacturing_tolerances', 'deformation', 'tool_and_fixture_without_model']),
                started_at=now(), ended_at=now())


def generate(state, geometry_check=None, rank=None, progress=None, cancel=None, max_nodes=250):
    """Bounded DFS over eligible candidates; CAD rejected candidates are never relabeled pass."""
    predecessors = check_rules(state['operations'], state['rules'])
    ops = {o['id']: o for o in state['operations']}
    # Reference flows and previous plan order are excluded from automatic ranking.
    priority = {oid: n for n, oid in enumerate(sorted(ops))}
    nodes, reasons = 0, []
    selected_directions = {}

    def search(done, assembled):
        nonlocal nodes
        if cancel and cancel():
            raise DomainError('JOB_CANCELED', '순서 생성을 취소했습니다.')
        if len(done) == len(ops):
            return done
        nodes += 1
        if nodes > max_nodes:
            return None
        if progress:
            progress(len(done) / max(len(ops), 1), f'{len(done)}/{len(ops)} 작업 · 후보 탐색 {nodes}')
        candidates = [ops[i] for i in ops if i not in done and predecessors[i] <= set(done)]
        candidates.sort(key=lambda o: (priority.get(o['id'], 10**9), o['id']))
        if rank:
            candidates = rank(candidates)
        for op in candidates:
            chosen_direction = op.get('direction', '+Z')
            if geometry_check and op['kind'] in INSERT_KINDS:
                accepted = False
                for direction in [chosen_direction] + [d for d in sorted(DIRECTIONS) if d != chosen_direction]:
                    candidate = op | {'direction': direction}
                    result = geometry_check(candidate, assembled)
                    if all(f['status'] in ('passed', 'contact', 'info', 'exception') for f in result):
                        chosen_direction, accepted = direction, True
                        break
                    reasons.extend(result[:1])
                if not accepted:
                    continue
            next_assembled = assembled + ([u for u in op.get('target_unit_ids', []) if u not in assembled] if installs(op) else [])
            result = search(done + [op['id']], next_assembled)
            if result is not None:
                selected_directions[op['id']] = chosen_direction
                return result
        return None

    result = search([], [])
    if result is None:
        raise DomainError('PATH_NOT_FOUND_IN_SCOPE', f'지원하는 방향과 {max_nodes}회 탐색 범위에서 전체 조립 후보를 찾지 못했습니다.', reasons[-20:])
    return dict(ordered_operation_ids=result, directions=selected_directions, origin='ml' if rank else 'rule',
                status='draft', geometry_checked=bool(geometry_check), nodes=nodes,
                reason='현재 상태의 필수 규칙·경로를 검사한 후보' if geometry_check else '필수 공정 규칙을 만족한 후보 · CAD 재검증 필요')


def build_timeline(state):
    ops = {o['id']: o for o in state['operations']}
    cursor, segments = 0.0, []
    for oid in state['plan']['ordered_operation_ids']:
        if oid not in ops:
            continue
        op = copy.deepcopy(ops[oid])
        if op.get('asap_path') and op.get('path_validity')=='stale':
            op.pop('asap_path',None)
            op['path_status']='unavailable'
        duration = float(op.get('duration_s', 2))
        segments.append(dict(operation_id=oid, start_s=cursor, end_s=cursor + duration, operation=op, **{k:v for k,v in op.items() if k != 'id'}))
        cursor += duration
    return dict(duration_s=cursor, segments=segments, coordinate_convention='world mm +Z up')


def make_snapshot(state, reviewer=None, reason='', draft=False):
    if not draft:
        validation = state.get('validation')
        if not validation or validation.get('stale') or validation.get('input_fingerprint') != input_fingerprint(state):
            raise DomainError('STALE_VALIDATION', '현재 개정의 검증이 필요합니다. 재검증 후 확정해 주세요.')
        if validation['status'] not in ('validated', 'validated_with_exceptions'):
            raise DomainError('REVIEW_REQUIRED', '미해결 오류 또는 검토 항목이 있어 확정할 수 없습니다.', validation.get('findings', []))
        if not reviewer or not reviewer.strip():
            raise DomainError('MISSING_REVIEWER', '검토자 이름을 입력해 주세요.')
    if not state['plan']['ordered_operation_ids']:
        raise DomainError('EMPTY_PLAN', '작업이 없는 계획은 출력할 수 없습니다.')
    if state['plan'].get('asap_run_id') and (state['plan'].get('asap_path_stale') or state['plan'].get('asap_path_status')!='success'):
        raise DomainError('PATH_REVALIDATION_REQUIRED','현재 계획의 실제 ASAP 이동 경로를 확보해야 영상으로 출력할 수 있습니다. 정적 단계 검토는 계속할 수 있습니다.')
    snapshot = copy.deepcopy(state)
    snapshot.update(id=uid('preview-' if draft else 'snapshot-'), draft=draft, status='draft' if draft else 'confirmed',
                    created_at=now(), confirmed_by=reviewer, reason=reason, project_id=state['project']['id'],
                    product_revision=state['project']['revision'], baseline_order=state.get('baseline', []),
                    timeline=build_timeline(state))
    snapshot['operations'] = [copy.deepcopy(next(o for o in state['operations'] if o['id'] == oid)) for oid in state['plan']['ordered_operation_ids']]
    return snapshot


def plan_metrics(state):
    tools={t['id']:t for t in state.get('tools',[])}
    operations={o['id']:o for o in state.get('operations',[])}
    previous=None; switches=0; change_time=0.; initial=0.; duration=0.; unknown=False
    for oid in state.get('plan',{}).get('ordered_operation_ids',[]):
        op=operations.get(oid,{})
        duration+=float(op.get('duration_s') or 0)
        tool=tools.get(op.get('tool_id'))
        if not tool or tool.get('type')=='none':
            if op.get('kind')=='fasten' and not tool:unknown=True
            continue
        identity=tool.get('assembly_id') or tool['id']
        if previous is None:initial=float(tool.get('change_time_s') or 0)
        elif identity!=previous:
            switches+=1;change_time+=float(tool.get('change_time_s') or 0)
        previous=identity
    return dict(duration_s=duration,tool_switch_count=switches,tool_change_time_s=change_time,
                initial_preparation_s=initial,tool_cost_status='incomplete' if unknown else 'defined',
                scope='입력한 작업 시간·공구 구성 단위의 교체 시간. 현장 실측과 별도')


def preview_move(state,operation_id,target_index):
    order=list(state['plan']['ordered_operation_ids'])
    if operation_id not in order or isinstance(target_index,bool) or not isinstance(target_index,int) or not 0<=target_index<len(order):
        raise DomainError('INVALID_PLAN','이동할 작업과 유효한 목표 위치를 선택하세요.')
    order.remove(operation_id);order.insert(target_index,operation_id)
    positions={oid:n for n,oid in enumerate(order)}
    violations=[r for r in state.get('rules',[]) if r.get('strength','hard')=='hard' and r['before_operation_id'] in positions and r['after_operation_id'] in positions and positions[r['before_operation_id']]>=positions[r['after_operation_id']]]
    return dict(allowed=not violations,ordered_operation_ids=order,violations=violations,
                geometry_status='not_checked',expected_revision=state['plan']['revision'],
                message='필수 선행 규칙 충족. 반영 후 전체 CAD 재검증이 필요합니다.' if not violations else '필수 선행 규칙 때문에 이동할 수 없습니다.')


def validate_operation(state,op):
    if op.get('kind') not in KINDS or op.get('direction','+Z') not in DIRECTIONS:
        raise DomainError('INVALID_INPUT','지원하는 작업 종류·방향을 지정하세요.')
    targets=op.get('target_unit_ids',[])
    if not isinstance(targets,list) or len(targets)!=len(set(targets)) or not set(targets)<={u['id'] for u in state['units']}:
        raise DomainError('INVALID_TARGET','대상 단위가 중복됐거나 현재 입력에 없습니다.')
    if op.get('kind')=='fasten' and len(targets)!=1:
        raise DomainError('BOLT_INDIVIDUAL','체결 작업은 한 배치 단위씩 지정하세요.')
    duration=op.get('duration_s',3)
    if isinstance(duration,bool) or not isinstance(duration,(int,float)) or not math.isfinite(duration) or not .1<=duration<=120:
        raise DomainError('INVALID_DURATION','작업 재생 시간은 0.1~120초입니다.')
    if op.get('tool_id') is not None and op['tool_id'] not in {t['id'] for t in state.get('tools',[])}:
        raise DomainError('INVALID_TOOL','등록된 공구를 선택하세요.')
    if op.get('fastening_stage','single') not in ('single','temporary','final'):
        raise DomainError('INVALID_STAGE','체결 단계는 단일·가체결·최종체결입니다.')
    if op.get('recommended_directions') is not None and (not isinstance(op['recommended_directions'],list) or not set(op['recommended_directions'])<=DIRECTIONS):
        raise DomainError('INVALID_DIRECTION','추천 방향 목록을 확인하세요.')
    torque=op.get('torque')
    if torque:
        lower=torque.get('min',torque.get('value'));upper=torque.get('max')
        if isinstance(lower,bool) or not isinstance(lower,(int,float)) or not math.isfinite(lower) or lower<0 or (upper is not None and (isinstance(upper,bool) or not isinstance(upper,(int,float)) or not math.isfinite(upper) or upper<lower)):
            raise DomainError('INVALID_TORQUE','토크 최솟값·최댓값을 확인하세요.')
    for field in ('motion_distance_mm','height_offset_mm'):
        value=op.get(field)
        if value is not None and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or (field=='motion_distance_mm' and value<=0)):
            raise DomainError('INVALID_INPUT',field+' 수치를 확인하세요.')
