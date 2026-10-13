"""Source-preserving STEP registration and reviewable CAD / process candidates."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from collections import defaultdict
from pathlib import Path

from . import store
from .domain import DomainError


def normalized_candidate(name):
    # Used ONLY for proposing matches; originals always kept and review is required.
    return re.sub(r'\(\d+\)$', '', name).strip().casefold()


def source_record(path, project_id, role, revision):
    if path.suffix.lower() not in ('.step', '.stp'):
        raise DomainError('INVALID_INPUT', f'{path.name}: STEP 확장자(.stp/.step)가 필요합니다.')
    if path.stat().st_size == 0:
        raise DomainError('INVALID_INPUT', f'{path.name}: 빈 파일입니다.')
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    source_id = store.uid('source-')
    destination = store.DATA_ROOT / 'sources' / project_id / str(revision) / (source_id + path.suffix.lower())
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(path, destination)
    return dict(id=source_id, project_revision_id=project_id, input_revision=revision, role=role,
                original_name=path.name, stored_path=str(destination), sha256=digest, bytes=path.stat().st_size,
                registered_at=store.now(), parse_status='queued', schema=None, source_unit=None, diagnostics=[])


def import_files(state, assembly_path, part_paths, progress=None, cancel=None, sample=False):
    from .cad import import_step
    if not assembly_path:
        raise DomainError('INVALID_INPUT', '완제품 STEP 1개를 선택하세요.')
    sample=False
    if state['plan']['status'] == 'confirmed':
        raise DomainError('CONFIRMED_IMMUTABLE', '확정본에서 새 초안을 만든 뒤 입력을 교체해 주세요.')
    result = copy.deepcopy(state)
    revision = state['project'].get('input_revision', 0) + 1
    files, manifests, diagnostics = [], [], []
    paths = [('assembly', Path(assembly_path))] + [('part', Path(p)) for p in part_paths]
    for n, (role, path) in enumerate(paths):
        if cancel and cancel():
            raise DomainError('JOB_CANCELED', 'STEP 등록을 취소했습니다.')
        if progress:
            progress(n / len(paths), f'{n+1}/{len(paths)} STEP 읽기 · {path.name}')
        record = source_record(path, state['project']['id'], role, revision)
        try:
            cache = store.DATA_ROOT / 'cad-cache' / record['sha256']
            manifest_path = cache / 'manifest.json'
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
            else:
                manifest = import_step(record['stored_path'], store.DATA_ROOT / 'cad-cache')
                cache.mkdir(parents=True, exist_ok=True)
                manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
            record.update(parse_status=manifest.get('status', 'loaded'), schema=manifest.get('source', {}).get('schema'),
                          source_unit=manifest.get('source', {}).get('units'), diagnostics=manifest.get('diagnostics', []),
                          manifest_path=str(manifest_path), instance_count=len(manifest.get('instances', [])))
            if not manifest.get('instances'):
                record['parse_status'] = 'failed'
                record['diagnostics'].append('CAD 배치를 읽지 못했습니다.')
            if record['parse_status'] in ('failed', 'partial'):
                diagnostics.append(record['original_name'])
            manifests.append((record, manifest))
        except Exception as exc:
            record.update(parse_status='failed', diagnostics=[f'{type(exc).__name__}: {exc}'])
            diagnostics.append(record['original_name'])
            manifests.append((record, {}))
        files.append(record)
    result['files'] = state.get('files', []) + files
    result['project'].update(input_revision=revision, revision=state['project']['revision'] + (1 if revision > 1 else 0),
                             status='import_failed' if diagnostics else 'mapping_required')
    assembly_record, assembly = manifests[0]
    if assembly.get('instances'):
        result['instances'] = assembly['instances']
        result['nodes'] = assembly.get('nodes', [])
        result['project'].update(cad_manifest=assembly_record['manifest_path'],
                                 scene_url=f'/api/scenes/{assembly_record["sha256"]}', bbox=assembly.get('bbox'),
                                 cad_engine=assembly.get('engine'))
        result['parts'], result['units'] = propose_units(assembly, manifests[1:], sample)
        result['operations'], result['groups'], result['rules'] = make_operations(result, sample)
        result['baseline'] = [o['id'] for o in result['operations']]
        result['plan'] = dict(id=store.uid('plan-'), revision=state['plan']['revision'] + 1, status='draft', origin='manual',
                              ordered_operation_ids=[], reason='입력 형상에서 작업 후보를 등록했습니다. 사용자 계획 또는 생성기 결과로 순서를 구성하세요.')
        result['validation'] = None
        result['ml_plan'] = None
        for key in result['versions']:
            result['versions'][key] += 1
    return result


def propose_units(assembly, part_manifests, sample=False):
    from .candidate_import import initial_units
    return initial_units(assembly, part_manifests)


def union_bbox(members):
    boxes = [i['bbox'] for i in members if i.get('bbox')]
    if not boxes:
        return None
    lo = [min(b['min'][j] for b in boxes) for j in range(3)]
    hi = [max(b['max'][j] for b in boxes) for j in range(3)]
    return dict(min=lo, max=hi, size=[hi[j] - lo[j] for j in range(3)], center=[(hi[j] + lo[j]) / 2 for j in range(3)])


def make_operations(state, sample=False):
    from .candidate_import import initial_operations
    return initial_operations(state)
