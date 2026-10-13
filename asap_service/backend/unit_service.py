"""Immutable source CAD and revisioned assembly units; no geometry guessing."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import shutil
import uuid
from pathlib import Path
import numpy as np
from asap_service.backend.storage import read_json, write_json, now


def state(directory):
    return read_json(Path(directory) / 'unit_state.json', {})


def unit_directory(directory, revision=None):
    directory = Path(directory)
    value = revision or state(directory).get('revision')
    if not value or len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
        raise ValueError('UNITS_NOT_INITIALIZED: 조립 단위가 준비되지 않았습니다.')
    target = directory / 'unit_revisions' / value
    if not (target / 'units.json').is_file():
        raise ValueError('UNIT_REVISION_NOT_FOUND: 조립 단위 리비전이 없습니다.')
    return target


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def coverage(source, groups):
    leaves = {u['id']: set(range(u['solid_count'])) if u['solid_count'] else {-1} for u in source}
    seen = set()
    for group in groups:
        if not group.get('member_refs'):
            raise ValueError('UNIT_MEMBERSHIP_INCOMPLETE: 빈 조립 단위는 허용하지 않습니다.')
        for member in group['member_refs']:
            oid = member['occurrence_id']
            if oid not in leaves:
                raise ValueError('UNIT_MEMBER_UNKNOWN: 원본 CAD 항목이 없습니다.')
            indices = member.get('solid_indices')
            if indices is not None and (not indices or any(type(i) is not int for i in indices) or len(indices) != len(set(indices))):
                raise ValueError('UNIT_MEMBER_INVALID: 솔리드 인덱스를 확인하세요.')
            selected = set(indices) if indices is not None else leaves[oid]
            if not selected <= leaves[oid]:
                raise ValueError('UNIT_MEMBER_INVALID: 원본 솔리드 범위를 벗어났습니다.')
            for i in selected:
                key = (oid, i)
                if key in seen:
                    raise ValueError('UNIT_MEMBERSHIP_DUPLICATE: 한 원본 형상이 두 번 배정되었습니다.')
                seen.add(key)
    expected = {(oid, i) for oid, values in leaves.items() for i in values}
    if seen != expected:
        raise ValueError('UNIT_MEMBERSHIP_INCOMPLETE: 미배정 원본 형상이 있습니다.')
    return {'complete': True, 'duplicate_count': 0, 'unassigned_count': 0,
            'source_occurrence_count': len(source), 'source_solid_count': sum(u['solid_count'] for u in source),
            'assembly_unit_count': len(groups), 'assigned_geometry_count': len(seen)}


def _member_mesh(mesh, member):
    if 'solid_indices' not in member:
        return mesh
    labels = np.asarray(mesh.get('triangle_solid_indices', []))
    faces = np.asarray(mesh['indices'], dtype=int).reshape(-1, 3)
    if len(labels) != len(faces):
        raise ValueError('SOURCE_MESH_PARTITION_MISSING: 원본 메시 솔리드 구분이 필요합니다.')
    faces = faces[np.isin(labels, member['solid_indices'])]
    vertices, inverse = np.unique(faces, return_inverse=True)
    return {'positions': np.asarray(mesh['positions']).reshape(-1, 3)[vertices].ravel().tolist(),
            'normals': np.asarray(mesh['normals']).reshape(-1, 3)[vertices].ravel().tolist(),
            'indices': inverse.ravel().tolist()}


def _build(directory, specs, previous, operation):
    from asap_service.backend.cad_pipeline import _load_brep, _save_brep, _solids, shape_features, combine_bbox, _color, _write_json, _progress
    from OCP.BRep import BRep_Builder
    from OCP.TopoDS import TopoDS_Compound
    directory = Path(directory)
    source = read_json(directory / 'units.json')
    report = coverage(source, specs)
    original = {u['id']: u for u in source}
    raw = read_json(directory / 'display_scene.json')
    meshes = {m['id']: m for m in raw['meshes']}
    revision = uuid.uuid4().hex
    target = directory / 'unit_revisions' / revision
    target.mkdir(parents=True)
    units, display = [], []
    try:
        for index, spec in enumerate(specs):
            builder, compound = BRep_Builder(), TopoDS_Compound()
            builder.MakeCompound(compound)
            positions, normals, indices = [], [], []
            for member in spec['member_refs']:
                item = original[member['occurrence_id']]
                shape = _load_brep(directory, item)
                if 'solid_indices' in member:
                    solids = _solids(shape)
                    for i in member['solid_indices']:
                        builder.Add(compound, solids[i])
                else:
                    builder.Add(compound, shape)
                if item['id'] not in meshes:
                    raise ValueError('SOURCE_DISPLAY_MISSING: 원본 형상 표시 메시가 없습니다.')
                mesh = _member_mesh(meshes[item['id']], member)
                offset = len(positions) // 3
                positions.extend(mesh['positions']); normals.extend(mesh['normals'])
                indices.extend(i + offset for i in mesh['indices'])
            single = original[spec['member_refs'][0]['occurrence_id']] if len(spec['member_refs']) == 1 else None
            if single and 'solid_indices' not in spec['member_refs'][0]:
                features = {key: single[key] for key in ('bbox', 'volume_mm3', 'solid_count', 'face_count', 'valid', 'volume_status')}
            else:
                features = shape_features(compound)
            definition = ('unitdef_' + _digest(spec['member_refs'])[:20])
            transform = np.eye(4).ravel().tolist()
            if single:
                definition = single['definition_id']
                if 'solid_indices' in spec['member_refs'][0]:
                    definition += '_solids_' + _digest(spec['member_refs'][0]['solid_indices'])[:12]
                transform = single['transform_world']
            unit = {**spec, **features, 'color': _color(spec['id']), 'geometry_role': 'assembly_unit',
                    'definition_id': definition,
                    'transform_world': transform, 'parent_id': None,
                    'brep_path': _save_brep(target, spec['id'], compound), 'unit_revision': revision}
            unit.update(original_names=[original[m['occurrence_id']]['name'] for m in spec['member_refs']],
                        definition_ids=list(dict.fromkeys(original[m['occurrence_id']]['definition_id'] for m in spec['member_refs'])))
            units.append(unit)
            display.append({'id': spec['id'], 'name': spec['name'], 'color': unit['color'],
                            'positions': positions, 'normals': normals, 'indices': indices})
            if index % 10 == 0:
                _progress('unit_geometry', .1 + .7*(index+1)/len(specs), spec['name'], unit_count=len(specs))
        manifest = deepcopy(read_json(directory / 'manifest.json'))
        manifest.update(unit_revision=revision, unit_manifest_hash=_digest(specs), conversion=None,
                        conversion_revision=None, engine_ids={}, source_occurrence_count=len(source))
        manifest['metrics'].update(unit_count=len(units), solid_count=report['source_solid_count'],
                                   display_mesh_count=len(display), source_occurrence_count=len(source))
        write_json(target / 'units.json', units)
        write_json(target / 'manifest.json', manifest)
        _progress('unit_scene_save', .9, '조립 단위 장면 저장 중')
        _write_json(target / 'display_scene.json', {'coordinate_system': raw['coordinate_system'],
                   'bbox': combine_bbox([u['bbox'] for u in units]), 'meshes': display})
        write_json(target / 'membership.json', report)
        write_json(target / 'edit.json', {'operation': operation, 'previous_revision': previous,
                                        'created_at': now(), 'original_geometry_modified': False})
        write_json(target / 'confirmation.json', {'confirmed': False})
        return revision, report
    except BaseException:
        # This newly created revision is unpublished and belongs only to this call.
        if target.resolve().parent == (directory / 'unit_revisions').resolve():
            shutil.rmtree(target)
        raise


def initialize(directory):
    if state(directory):
        return state(directory)
    source = read_json(Path(directory) / 'units.json')
    specs = [{'id': u['id'], 'name': u['name'], 'member_refs': [{'occurrence_id': u['id']}],
              'creation_method': 'cad_initial'} for u in source]
    revision, report = _build(directory, specs, None, 'initialize')
    value = {'revision': revision, 'confirmed': False, 'undo': [], 'redo': [], 'coverage': report}
    write_json(Path(directory) / 'unit_state.json', value)
    return value


def edit(directory, expected_revision, operation, unit_ids=None, name=None, member_refs=None, target_unit_id=None):
    current = state(directory)
    if current.get('revision') != expected_revision:
        raise ValueError('REVISION_CONFLICT: 화면을 새로 고침한 뒤 다시 편집하세요.')
    original = read_json(Path(directory) / 'units.json')
    loaded = read_json(unit_directory(directory) / 'units.json')
    fields = ('id', 'name', 'member_refs', 'creation_method', 'part_number', 'type_id','template_id','match_id','matching_metrics','template_to_cad_matrix')
    specs = [{k: deepcopy(u[k]) for k in fields if k in u} for u in loaded]
    requested = unit_ids or []
    selected = [u for u in specs if u['id'] in requested]
    if len(set(requested)) != len(requested) or len(selected) != len(requested):
        raise ValueError('UNIT_UNKNOWN: 선택한 조립 단위를 확인하세요.')
    if operation in ('undo', 'redo'):
        stack = current.get(operation, [])
        if not stack:
            raise ValueError('HISTORY_EMPTY: 되돌릴 변경이 없습니다.')
        loaded = read_json(unit_directory(directory, stack[-1]) / 'units.json')
        specs = [{k: deepcopy(u[k]) for k in fields if k in u} for u in loaded]
    elif operation == 'merge':
        if len(selected) < 2:
            raise ValueError('UNIT_SELECTION: 두 개 이상의 단위를 선택하세요.')
        specs = [u for u in specs if u not in selected]
        specs.append({'id': 'unit_' + uuid.uuid4().hex[:20], 'name': (name or '').strip() or '조립 묶음',
                      'member_refs': [m for u in selected for m in u['member_refs']], 'creation_method': 'manual_group'})
    elif operation == 'ungroup':
        if not selected:
            raise ValueError('UNIT_SELECTION: 해제할 단위를 선택하세요.')
        specs = [u for u in specs if u not in selected]
        names = {u['id']: u['name'] for u in original}
        for u in selected:
            for m in u['member_refs']:
                # Atomic solid references remain atomic; ungroup never guesses a cut.
                specs.append({'id': 'unit_' + uuid.uuid4().hex[:20], 'name': names[m['occurrence_id']],
                              'member_refs': [m], 'creation_method': 'manual_ungroup'})
    elif operation == 'rename':
        if len(selected) != 1 or not (name or '').strip():
            raise ValueError('UNIT_SELECTION: 이름과 한 개의 단위를 지정하세요.')
        selected[0]['name'] = name.strip()
    elif operation == 'clear_template':
        if not selected:raise ValueError('UNIT_SELECTION: 기준 연결을 해제할 단위를 선택하세요.')
        for unit in selected:
            for key in ['template_id','type_id','match_id','matching_metrics','template_to_cad_matrix']:unit.pop(key,None)
            unit['creation_method']='manual_group'
    elif operation == 'split':
        from asap_service.backend.cad_pipeline import _load_brep, _solids
        if len(selected) != 1:
            raise ValueError('UNIT_SELECTION: 분할할 단위 하나를 선택하세요.')
        specs.remove(selected[0])
        original_by_id = {u['id']: u for u in original}
        for member in selected[0]['member_refs']:
            oid = member['occurrence_id']; count = len(_solids(_load_brep(Path(directory), original_by_id[oid])))
            if not count:
                raise ValueError('NO_SOLIDS: 독립 솔리드가 없는 항목은 분할할 수 없습니다.')
            for i in member.get('solid_indices', list(range(count))):
                specs.append({'id': 'unit_' + uuid.uuid4().hex[:20], 'name': original_by_id[oid]['name'] + f' / 솔리드 {i+1}',
                              'member_refs': [{'occurrence_id': oid, 'solid_indices': [i]}], 'creation_method': 'manual_split'})
    elif operation == 'reassign':
        destination = next((u for u in specs if u['id'] == target_unit_id), None)
        if not destination or not member_refs:
            raise ValueError('UNIT_SELECTION: 대상 단위와 이동할 구성원을 선택하세요.')
        for member in member_refs:
            owners = [u for u in specs if member in u['member_refs']]
            if len(owners) != 1:
                raise ValueError('UNIT_MEMBER_UNKNOWN: 현재 리비전의 구성원 전체를 선택하세요.')
            owner = owners[0]
            if owner is not destination:
                owner['member_refs'].remove(member); destination['member_refs'].append(member)
        specs = [u for u in specs if u['member_refs']]
    elif operation == 'detach':
        if not member_refs:
            raise ValueError('UNIT_SELECTION: 분리할 구성원을 선택하세요.')
        names = {u['id']: u['name'] for u in original}
        detached = []
        for member in member_refs:
            owners = [u for u in specs if member in u['member_refs']]
            if len(owners) != 1:
                raise ValueError('UNIT_MEMBER_UNKNOWN: 현재 구성원을 선택하세요.')
            owners[0]['member_refs'].remove(member)
            detached.append({'id': 'unit_' + uuid.uuid4().hex[:20], 'name': names[member['occurrence_id']],
                             'member_refs': [member], 'creation_method': 'manual_detach'})
        specs = [u for u in specs if u['member_refs']] + detached
    else:
        raise ValueError('UNIT_OPERATION: 지원하지 않는 편집입니다.')
    revision, report = _build(directory, specs, expected_revision, operation)
    if operation == 'undo':
        undo = current['undo'][:-1]; redo = current.get('redo', []) + [expected_revision]
    elif operation == 'redo':
        undo = current.get('undo', []) + [expected_revision]; redo = current['redo'][:-1]
    else:
        undo = current.get('undo', []) + [expected_revision]; redo = []
    value = {'revision': revision, 'confirmed': False, 'undo': undo, 'redo': redo, 'coverage': report}
    write_json(Path(directory) / 'unit_state.json', value)
    return {'status': 'unit_review_required', **value}


def confirm(directory, expected_revision):
    current = state(directory)
    if current.get('revision') != expected_revision:
        raise ValueError('REVISION_CONFLICT: 현재 리비전을 다시 확인하세요.')
    target = unit_directory(directory)
    report = coverage(read_json(Path(directory) / 'units.json'), read_json(target / 'units.json'))
    value = {'confirmed': True, 'revision': expected_revision, 'coverage': report, 'confirmed_at': now()}
    write_json(target / 'confirmation.json', value)
    current.update(confirmed=True, coverage=report)
    write_json(Path(directory) / 'unit_state.json', current)
    return current


def require_confirmed(directory):
    current = state(directory)
    if not current.get('confirmed'):
        raise ValueError('UNITS_NOT_CONFIRMED: 조립 단위 화면에서 전체 구성을 확정하세요.')
    return unit_directory(directory)


def apply_templates(directory,expected_revision,candidates):
    current=state(directory)
    if current.get('revision')!=expected_revision:raise ValueError('REVISION_CONFLICT: 현재 단위를 다시 확인하세요.')
    units=read_json(unit_directory(directory)/'units.json');used=set()
    for c in candidates:
        for member in c['member_refs']:
            oid=member['occurrence_id']
            if oid in used:raise ValueError('MATCH_MEMBERSHIP_CONFLICT: 서로 겹치는 후보를 동시에 확정할 수 없습니다.')
            used.add(oid)
    fields=('id','name','member_refs','creation_method','part_number','type_id','template_id','match_id','matching_metrics','template_to_cad_matrix')
    specs=[]
    for unit in units:
        if any(m['occurrence_id'] in used and 'solid_indices' in m for m in unit['member_refs']):
            raise ValueError('MATCH_REQUIRES_REVIEW: 솔리드 분할된 항목은 수동 단위 구성으로 확인하세요.')
        refs=[m for m in unit['member_refs'] if m['occurrence_id'] not in used]
        if refs:specs.append({**{k:unit[k] for k in fields if k in unit},'member_refs':refs})
    for c in candidates:
        specs.append({'id':'unit_'+uuid.uuid4().hex[:20],'name':c['template_name'],'member_refs':c['member_refs'],
          'creation_method':'template_match' if c.get('metrics') else 'manual_template','template_id':c['template_id'],
          'type_id':c['template_id'],'match_id':c['id'],'matching_metrics':c.get('metrics'),
          'template_to_cad_matrix':c.get('template_to_cad_matrix'),'part_number':c.get('part_number','')})
    revision,report=_build(directory,specs,expected_revision,'template_accept')
    value={'revision':revision,'confirmed':False,'undo':current.get('undo',[])+[expected_revision],'redo':[],'coverage':report}
    write_json(Path(directory)/'unit_state.json',value)
    return {'status':'unit_review_required',**value}
