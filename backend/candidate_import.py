"""Product-independent initial candidates. Geometry matching lives in ASAP service."""
from pathlib import Path
from . import store


def initial_units(assembly, part_manifests):
    from .importer import normalized_candidate, union_bbox
    instances=assembly.get('instances',[])
    parts=[];units=[];by_alias={}
    for record,manifest in part_manifests:
        if not manifest.get('instances'):continue
        pid=store.uid('part-');name=Path(record['original_name']).stem
        aliases=list(dict.fromkeys(i.get('definition_name') or i.get('name','') for i in manifest['instances']))
        parts.append(dict(id=pid,company_part_no=None,display_name=name,cad_aliases=aliases,
                          source_part_file_id=record['id'],metadata_source='기준 파일명·CAD 이름 후보',
                          metadata_status='candidate',expected_quantity=None))
        for alias in aliases:by_alias.setdefault(normalized_candidate(alias),[]).append(pid)
    for i in instances:
        candidates=by_alias.get(normalized_candidate(i.get('definition_name') or i.get('name','')),[])
        # A name match proposes identity, never physical grouping or placement count.
        pid=candidates[0] if len(candidates)==1 else None
        if not pid:
            pid=store.uid('part-');parts.append(dict(id=pid,company_part_no=None,display_name=i.get('name','CAD 배치'),
                cad_aliases=[i.get('name','')],metadata_status='candidate',expected_quantity=None))
        units.append(dict(id=store.uid('unit-'),part_definition_id=pid,part_no=None,name=i.get('name','CAD 배치'),
            member_instance_ids=[i['id']],preassembled=False,mapping_status='candidate' if candidates else 'unmapped',
            reviewer=None,mapping_basis='원본 CAD 배치. 이름 대응은 검토 후보이며 기하 매칭/단위 확정과 별도',bbox=i.get('bbox')))
    return parts,units


def initial_operations(state):
    operations=[]
    for unit in state['units']:
        operations.append(dict(id=store.uid('op-'),group_id='unassigned',kind='place',target_unit_ids=[unit['id']],
            title=unit['name']+' 설치',part_no=unit.get('part_no'),part_name=unit['name'],direction='+Z',duration_s=3.,
            torque=None,tool_id=None,fastening_stage='single',warning={'status':'missing','text':'','source_ref':None},
            source_ref='입력 CAD 단위에서 등록한 작업 후보',review_status='review_required'))
    return operations,[dict(id='unassigned',name='공정 검토 필요',source_ref='입력 CAD')],[]
