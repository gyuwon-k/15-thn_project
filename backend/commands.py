"""Versioned command envelope and durable replay for synchronous mutations."""
import re,uuid
from fastapi import APIRouter,Body
from . import store,domain
router=APIRouter()
ACTIONS={'project.update':'update_project','plan.generate':'generate','plan.validate':'validate','plan.confirm':'confirm','plan.previewMove':'preview_move',
         'plan.applyChanges':'save_plan','plan.newDraft':'new_draft','operation.update':'update_operation','operation.create':'create_operation',
         'operation.delete':'delete_operation','rules.save':'save_rules','tool.update':'tool_update','tool.remove':'tool_delete','model.train':'train',
         'export.render':'render','export.package':'package','job.cancel':'cancel_job'}

@router.post('/api/commands')
def execute(body:dict=Body(...)):
    from .main import action,create_project,aggregate
    request_id=body.get('request_id','')
    if not isinstance(request_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,64}',request_id):
        raise domain.DomainError('INVALID_REQUEST_ID','request_id는 고유한 8~64자리 문자열입니다.')
    command=body.get('command');payload=body.get('payload',{})
    if not isinstance(payload,dict):raise domain.DomainError('INVALID_INPUT','payload는 JSON 객체입니다.')
    digest=store.fingerprint(body)
    with store.LOCK,store.transaction_scope():
        previous=store.request_response(request_id,digest)
        if previous is not None:return previous
        if command=='project.create':result=create_project(payload)
        elif command in ACTIONS:
            pid=body.get('project_id')
            if not pid:raise domain.DomainError('INVALID_REFERENCE','프로젝트 ID가 필요합니다.')
            data={**payload}
            if body.get('expected_revision') is not None:data['expected_revision']=body['expected_revision']
            result=action(pid,ACTIONS[command],data)
        else:raise domain.DomainError('COMMAND_UNSUPPORTED','지원하지 않는 명령입니다.')
        response={'ok':True,'result':result,'request_id':request_id}
        store.save_request_response(request_id,digest,response)
        return response
