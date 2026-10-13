"""ASAP raw experiments and explicit adoption into a reviewed PILOT draft.

The native planner receives geometry and settings only. Project process rules,
metadata, and Flow documents never enter its model input.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field

from . import domain, store
from asap_service.backend import app as raw_api, jobs as raw_jobs, storage

app = raw_api.app
router = APIRouter()


def startup():
    """The parent lifespan owns the mounted application's worker lifecycle."""
    if raw_api.manager is not None:
        return
    data = (store.DATA_ROOT / 'asap').resolve()
    for module in (storage, raw_jobs, raw_api):
        module.DATA = data
    data.mkdir(parents=True, exist_ok=True)
    if not (data / 'config.json').exists():
        storage.write_json(data / 'config.json', storage.default_config())
    raw_api.manager = raw_jobs.JobManager()


def shutdown():
    if raw_api.manager is not None:
        raw_api.manager.shutdown()
        raw_api.manager = None


class LinkInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    session_id: str
    expected_input_revision: int | None = Field(default=None, ge=0)


class ImportInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: str
    expected_input_revision: int | None = Field(default=None, ge=0)


def editable(state, expected):
    if state['plan'].get('status') == 'confirmed':
        raise domain.DomainError('CONFIRMED_IMMUTABLE', '확정본은 직접 교체할 수 없습니다. 새 초안을 생성하세요.')
    if expected is not None and expected != state['project'].get('input_revision', 0):
        raise domain.DomainError('REVISION_CONFLICT', '프로젝트 입력 개정이 변경되었습니다. 새로 고침 후 실행하세요.')


def association(state):
    linked=state.get('asap',{})
    sid=linked.get('session_id')
    result={'project_id':state['project']['id'], 'session_id':sid, 'run_id':linked.get('run_id')}
    if sid:
        directory=raw_api.require_session(sid)
        info=storage.read_json(directory/'session.json',{})
        units=storage.read_json(directory/'unit_state.json',{})
        result.update(input_mode=info.get('input_mode','assembly_only'), unit_revision=units.get('revision'),
                      session_url='/api/asap/sessions/'+sid)
    return result


@router.get('/api/projects/{project_id}/asap-link')
def get_link(project_id: str):
    return association(store.get(project_id))


@router.post('/api/projects/{project_id}/asap-link')
def link(project_id: str, body: LinkInput):
    directory=raw_api.require_session(body.session_id)
    with store.LOCK:
        state=store.get(project_id)
        editable(state,body.expected_input_revision)
        info=storage.read_json(directory/'session.json',{})
        if info.get('project_id') not in (None,project_id):
            raise domain.DomainError('ASAP_SESSION_OWNERSHIP', '다른 프로젝트에 연결된 ASAP 입력입니다.')
        info['project_id']=project_id
        storage.write_json(directory/'session.json',info)
        state['asap']={'session_id':body.session_id,'linked_at':store.now(),'status':'linked'}
        store.save(state,'asap_link',{'session_id':body.session_id})
        return association(state)


def sha(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            digest.update(chunk)
    return digest.hexdigest()


def validate_result(result):
    if result.get('sequence_status') != 'success':
        raise domain.DomainError('ASAP_SEQUENCE_UNAVAILABLE','전체 순서가 생성된 실제 ASAP 실행만 공정 초안으로 가져올 수 있습니다.')
    mapping=result.get('part_map',{})
    order=result.get('assembly_order',[])
    base=result.get('base_part')
    removal=result.get('disassembly',[])
    if not mapping or len(order)!=len(mapping) or len(set(order))!=len(order) or set(order)!=set(mapping):
        raise domain.DomainError('ASAP_RESULT_COVERAGE','ASAP 단위 대응에 누락 또는 중복이 있습니다.')
    if not order or base != order[0] or removal != list(reversed(order[1:])):
        raise domain.DomainError('ASAP_RESULT_ORDER','기준 부품과 분해 역순의 대응이 유효하지 않습니다.')
    unit_ids=[mapping[i].get('unit_id',mapping[i].get('id')) for i in order]
    if any(not u for u in unit_ids) or len(set(unit_ids))!=len(unit_ids):
        raise domain.DomainError('ASAP_RESULT_COVERAGE','배치 단위 ID가 누락되었거나 중복되었습니다.')
    if result.get('path_status')=='success':
        paths={s.get('moving_part'):s for s in result.get('steps',[])}
        if set(paths)!=set(removal):
            raise domain.DomainError('ASAP_PATH_COVERAGE','전체 경로 판정과 실제 경로 부품 수가 일치하지 않습니다.')
        for step in paths.values():
            matrices=step.get('path',[])
            if len(matrices)<2 or any(len(m)!=16 or any(not isinstance(x,(int,float)) or not math.isfinite(x) for x in m) for m in matrices):
                raise domain.DomainError('ASAP_PATH_INVALID','ASAP의 실제 경로 행렬을 확인할 수 없습니다.')
    return mapping,order


def bbox_from_mesh(mesh):
    positions=mesh.get('positions',[])
    if not positions:
        return None
    lo=[min(positions[j::3]) for j in range(3)]
    hi=[max(positions[j::3]) for j in range(3)]
    return {'min':lo,'max':hi,'size':[hi[j]-lo[j] for j in range(3)],'center':[(hi[j]+lo[j])/2 for j in range(3)]}


def adopted_state(state,result,session,run_dir):
    """Preserve unit occurrence identities and real transforms without inventing work."""
    mapping,order=validate_result(result)
    sid=state['asap']['session_id']
    original_order=['asap-op-'+mapping[i].get('unit_id',mapping[i]['id']) for i in order]
    raw_hash=sha(run_dir/'result.json')
    scene=storage.read_json(run_dir/'display_scene.json',{})
    meshes={m['id']:m for m in scene.get('meshes',[])}
    source=storage.read_json(session/'manifest.json',{}).get('source',{})
    source_units={u['id']:u for u in storage.read_json(session/'units.json',[])}
    catalog=storage.read_json(session/'templates.json',{})
    templates={t['id']:t for t in catalog.get('templates',[])}
    current_directory=raw_api.unit_service.unit_directory(session)
    current_units=storage.read_json(current_directory/'units.json',[])
    units_by_id={u['id']:u for u in current_units}
    parts,units,instances,operations=[],[],[],[]
    definitions={}
    step_by_part={s['moving_part']:s for s in result.get('steps',[])}
    for index,engine_id in enumerate(order):
        entry=mapping[engine_id]
        unit_id=entry.get('unit_id',entry['id'])
        raw_unit=units_by_id[unit_id]
        template=templates.get(raw_unit.get('template_id'),{})
        type_id=raw_unit.get('type_id') or entry.get('definition_id') or unit_id
        part_id='asap-part-'+type_id
        if part_id not in definitions:
            part={'id':part_id,'company_part_no':template.get('part_number') or None,
                  'display_name':template.get('name') or entry.get('name') or unit_id,
                  'cad_aliases':entry.get('original_names',[]),'expected_quantity':None,
                  'metadata_status':'review_required','source_ref':'ASAP template/CAD label','asap_type_id':type_id}
            definitions[part_id]=part
            parts.append(part)
        part=definitions[part_id]
        members=[]
        for ref in entry.get('member_refs',[]):
            occurrence=ref['occurrence_id']
            selected=ref.get('solid_indices')
            iid=occurrence if selected is None else occurrence+'#solids-'+','.join(map(str,selected))
            members.append(iid)
            original=source_units.get(occurrence,{})
            instances.append({'id':iid,'definition_id':original.get('definition_id'),
                              'name':original.get('name',occurrence),'bbox':original.get('bbox'),
                              'transform_world':original.get('transform_world'), 'asap_member_ref':ref,
                              'geometry_cache_root':str(session),'geometry_brep_path':original.get('brep_path'),
                              'source_brep_path':original.get('brep_path'),'world_baked':True})
        box=bbox_from_mesh(meshes.get(unit_id,{}))
        units.append({'id':unit_id,'name':entry.get('name',unit_id),'part_no':part['company_part_no'],
                      'part_definition_id':part_id,'member_instance_ids':members,'asap_engine_id':engine_id,
                      'asap_unit_revision':result.get('unit_revision'),'asap_member_refs':entry.get('member_refs',[]),
                      'preassembled':len(members)>1,'mapping_status':'confirmed','reviewer':'ASAP 조립 단위 확인',
                      'mapping_basis':'확정된 ASAP 단위 manifest · 원본 형상 보존','bbox':box,
                      'geometry_cache_root':str(current_directory),'geometry_brep_path':entry.get('source_brep_path'),
                      'source_brep_path':entry.get('source_brep_path')})
        step=step_by_part.get(engine_id)
        operation={'id':'asap-op-'+unit_id,'kind':'prepare' if index==0 else 'place','target_unit_ids':[unit_id],
                   'title':entry.get('name',unit_id)+(' 준비' if index==0 else ' 조립'),
                   'part_no':part['company_part_no'],'part_name':part['display_name'],'group_id':'asap',
                   'direction':'+Z','duration_s':3.,'torque':None,
                   'warning':{'status':'missing','text':'','source_ref':None},'review_status':'review_required',
                   'source_ref':'ASAP '+result.get('strategy',result.get('settings',{}).get('strategy','native')),
                   'asap_engine_id':engine_id,'asap_unit_revision':result.get('unit_revision')}
        if step:
            operation['asap_motion']={'verified':bool(step.get('path')),'validity':'current','source_direction':'disassembly',
                                     'step':copy.deepcopy(step),'coordinate_map':copy.deepcopy(result.get('coordinate_map',{})),
                                     'run_id':result['run_id'],'unit_id':unit_id,'immutable':True,
                                     'original_order':original_order,'raw_result_sha256':raw_hash,
                                     'original_members':copy.deepcopy(entry.get('member_refs',[])),
                                     'original_kind':'place','original_direction':'+Z'}
        else:
            operation['asap_motion']={'verified':False,'base':True,'validity':'current','run_id':result['run_id'],'unit_id':unit_id,
                                     'coordinate_map':copy.deepcopy(result.get('coordinate_map',{})),
                                     'assembly_pose':copy.deepcopy(result.get('steps',[])[-1].get('pose')) if result.get('steps') else None,
                                     'original_order':original_order,'raw_result_sha256':raw_hash,
                                     'original_members':copy.deepcopy(entry.get('member_refs',[])),
                                     'original_kind':'prepare','original_direction':'+Z'}
        operations.append(operation)
    if len({i['id'] for i in instances}) != len(instances):
        raise domain.DomainError('ASAP_RESULT_COVERAGE','같은 원본 구성원이 여러 단위에 중복 배정되었습니다.')
    state.update(parts=parts,units=units,instances=instances,operations=operations,rules=[],exceptions=[],
                 baseline=[o['id'] for o in operations],ml_plan=None,groups=[{'id':'asap','name':'ASAP 파생 공정 초안'}],validation=None)
    state['versions'].update(mapping=state['versions'].get('mapping',0)+1,rules=state['versions'].get('rules',0)+1)
    state['project'].update(input_revision=state['project'].get('input_revision',0)+1,status='ready',
                            bbox=scene.get('bbox'),scene_url=f'/api/projects/{state["project"]["id"]}/asap-scene/{result["run_id"]}',
                            coordinate_convention='world mm +Z up; display (x,z,-y)/1000')
    state['project'].pop('cad_manifest',None)
    state['files']=[{'id':'asap-source-'+sid,'original_name':storage.read_json(session/'session.json',{}).get('filename',source.get('name','assembly.step')),
                     'role':'assembly','sha256':source.get('sha256'),'bytes':source.get('size',0),'parse_status':'ready',
                     'stored_path':str(session/'source/model.step'),'input_revision':state['project']['input_revision']}]
    for entry in catalog.get('files',[]):
        state['files'].append({'id':'asap-template-'+entry['id'],'original_name':entry.get('name'),
                               'role':'part','sha256':entry.get('sha256'),'bytes':entry.get('size',0),
                               'parse_status':entry.get('status','review_required'),'stored_path':str(session/'templates'/entry['id']/'source/model.step'),
                               'input_revision':state['project']['input_revision']})
    domain.invalidate(state,'실제 ASAP 순서를 공정 초안으로 채택했습니다. 품번·체결·토크·공구·현장 규칙 검토가 필요합니다.')
    state['plan'].update(origin='asap',ordered_operation_ids=state['baseline'],geometry_checked=False,
                         asap_path_status=result.get('path_status'),asap_raw_hash=sha(run_dir/'result.json'),
                         asap_run_id=result['run_id'],asap_order_fingerprint=store.fingerprint(state['baseline']),
                         reason='실제 ASAP 강체 경로 · 공정 및 현장 검토 필요')
    state['asap'].update(status='adopted_draft',run_id=result['run_id'],unit_revision=result.get('unit_revision'),
                          raw_result_sha256=sha(run_dir/'result.json'),result_url='/api/asap/runs/'+result['run_id'],
                          raw_order=order,scope='rigid_geometry_only',adopted_at=store.now(),
                          preprocessing_status=result.get('preprocessing_status'),
                          preprocessing_summary=result.get('preprocessing_summary'),
                          raw_checked_scope=copy.deepcopy(result.get('checked_scope')),
                          raw_physical_validation_scope=copy.deepcopy(result.get('physical_validation_scope')),
                          source_sha256=source.get('sha256'))
    return state


@router.post('/api/projects/{project_id}/asap-import')
def import_run(project_id:str,body:ImportInput):
    run_dir,_=raw_api.finished_run(body.run_id)
    result=raw_api.read_run(run_dir,body.run_id)
    result['run_id']=body.run_id
    index=storage.read_json(storage.DATA/'runs'/(body.run_id+'.json'),{})
    sid=index.get('session_id')
    with store.LOCK:
        state=store.get(project_id)
        editable(state,body.expected_input_revision)
        if state.get('asap',{}).get('session_id') != sid:
            raise domain.DomainError('ASAP_SESSION_MISMATCH','프로젝트에 연결된 완제품 입력의 실행만 가져올 수 있습니다.')
        directory=storage.session_path(sid)
        revision=storage.read_json(directory/'unit_state.json',{})
        if not revision.get('confirmed') or result.get('unit_revision')!=revision.get('revision'):
            raise domain.DomainError('ASAP_STALE_INPUT','현재 확정된 조립 단위와 다른 실행입니다. 현재 개정으로 재실행하세요.')
        validate_result(result)
        unit_dir=raw_api.unit_service.unit_directory(directory)
        current={u['id']:u for u in storage.read_json(unit_dir/'units.json',[])}
        exported=result['part_map']
        if set(current)!={u.get('unit_id',u.get('id')) for u in exported.values()}:
            raise domain.DomainError('ASAP_RESULT_COVERAGE','현재 조립 단위와 결과의 단위 수 또는 ID가 다릅니다.')
        for entry in exported.values():
            uid=entry.get('unit_id',entry.get('id'))
            if store.fingerprint(entry.get('member_refs',[])) != store.fingerprint(current[uid].get('member_refs',[])):
                raise domain.DomainError('ASAP_RESULT_COVERAGE','원본 구성원과 결과의 단위 대응이 다릅니다.')
        raw_api.unit_service.coverage(storage.read_json(directory/'units.json',[]),list(exported.values()))
        updated=adopted_state(copy.deepcopy(state),result,directory,run_dir)
        store.save(updated,'asap_adopt',{'session_id':sid,'run_id':body.run_id,'raw_result_sha256':updated['asap']['raw_result_sha256']})
        return {'project_id':project_id,'session_id':sid,'run_id':body.run_id,'status':'adopted_draft',
                'units':len(updated['units']),'operations':len(updated['operations']),
                'sequence_status':result['sequence_status'],'path_status':result.get('path_status'),
                'input_revision':updated['project']['input_revision'],'plan_revision':updated['plan']['revision']}


@router.get('/api/projects/{project_id}/asap-scene/{run_id}')
def project_scene(project_id:str,run_id:str):
    store.get(project_id)
    index=storage.read_json(storage.DATA/'runs'/(storage.checked_id(run_id)+'.json'),{})
    info=storage.read_json(storage.session_path(index.get('session_id'))/'session.json',{})
    if info.get('project_id') != project_id:
        raise domain.DomainError('ASAP_SESSION_MISMATCH','다른 프로젝트 장면입니다.')
    return FileResponse(storage.run_path(run_id)/'display_scene.json',media_type='application/json')
