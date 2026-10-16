"""Local application API with revision-safe jobs, immutable files and snapshots."""
from __future__ import annotations
import copy
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import sys
import threading
from collections import Counter
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import Body, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from . import domain
from .domain import DomainError, digest, now, uid
from .jobs import JobService, atomic_json, path_values
from .storage import Storage, StorageConflict, StorageError

PROJECT_ROOT=Path(__file__).resolve().parents[1]
RESOURCE_ROOT=Path(getattr(sys,"_MEIPASS",PROJECT_ROOT))
SERVER=None


def release_info():
    from .release import runtime_identity
    release=runtime_identity()
    return {**release,"version":release["app_version"],"algorithm_version":release["recommendation_version"]}


class Services:
    def __init__(self, data_dir, resources, release):
        self.root=Path(data_dir).resolve();self.release=release
        self.store=Storage(self.root,resources);self.store.init()
        self.lock=threading.RLock()
        self.jobs=JobService(self.store,self.root,self.apply_result,release)
        self.base_url="http://127.0.0.1:"+os.environ.get("PILOT_PORT","8772")
        self.token=os.environ.get("PILOT_SESSION_TOKEN") or secrets.token_urlsafe(32)
        self.stop_event=threading.Event()
        reference=RESOURCE_ROOT/"backend"/"resources"/"company-reference.png"
        if reference.is_file() and not self.store.get_setting("company_template_seeded",False):
            asset=self.store.register_file(reference,"video_template")
            templates=self.store.get_setting("video_templates",[])
            templates.append({"id":uid("template-"),"name":"제공 HVJB 영상 양식 · 검토 대기","approved":False,"revision":1,
                              "layout":"company-hvjb-reference","file_id":asset["id"],"background_url":"/api/files/"+asset["id"],
                              "created_at":now(),"source":"제공 영상 91958-KD000.mp4의 정보 배치","fields":["part_name","part_number","torque","caution"]})
            self.store.set_setting("video_templates",templates);self.store.set_setting("company_template_seeded",True)
        self.learning_dispatcher=threading.Thread(target=self.dispatch_learning,name="pilot-learning-scheduler",daemon=True)
        self.learning_dispatcher.start()

    def dispatch_learning(self):
        from .learning import build_dataset,reviewed_histories
        while not self.stop_event.wait(15):
            try:
                settings=self.store.get_setting("learning_settings",{})
                threshold=settings.get("minimum_cases")
                if not settings.get("automatic_training") or type(threshold) is not int or threshold<1:continue
                histories=reviewed_histories(self.store.snapshots(),self.store.get_setting("learning_reviews",[]))
                data=build_dataset(histories);signature=digest(data)
                models=self.store.get_setting("learning_models",[])
                previous=max((m.get("eligible_count",0) for m in models if m.get("status") in ("SHADOW_EVALUATION","APPROVED")),default=0)
                if len(data["eligible_confirmations"])-previous<threshold or self.store.get_setting("last_auto_training_hash")==signature:continue
                if len({r["group"] for r in data["rows"]})<3:continue
                if any(j.get("status") in ("queued","running","cancel_requested") for j in self.store.list_jobs()):continue
                target=next((h["project"]["id"] for h in histories if h["id"] in data["eligible_confirmations"]),None)
                if not target:continue
                self.jobs.submit(self.project(target),"train",{"histories":histories,"settings":settings,"output_dir":str(self.root/"models"),"timeout_seconds":600})
                self.store.set_setting("last_auto_training_hash",signature)
            except Exception as exc:
                self.store.set_setting("automatic_training_diagnostic",{"message":str(exc),"at":now()})

    def project(self,ident):
        result=self.store.get_project(ident)
        if result is None:raise KeyError(ident)
        from .planning_settings import compatible_rules
        result['rules'],notes=compatible_rules(result.get('rules',{}))
        if notes:result['rules_compatibility']=notes
        return result

    def aggregate(self,ident):
        result=self.project(ident)
        signature=domain.confirmation_hash(result)
        confirmations=self.store.snapshots(ident)
        current=next((s for s in confirmations if s.get("source_hash")==signature),None)
        result["jobs"]=self.store.list_jobs(ident)
        result["history_count"]=len(confirmations)
        result["coverage"]=domain.coverage(result)
        result["model_reprocess_required"]=bool(result.get("model")) and not self.model_current(result["model"])
        result["process_profile_hash"]=domain.process_profile_hash(result)
        result["can_confirm"]=not domain.confirmation_errors(result,self.release) and not any(j.get("status") in ("queued","running","cancel_requested") for j in result["jobs"])
        validation=result.get("validation") or {}
        current_proof=validation.get("build_id")==self.release["build_id"] and validation.get("algorithm_version")==self.release.get("algorithm_version") and validation.get("dependency_hash")==domain.geometry_hash(result)
        if not current_proof:
            for step in result.get("plan",{}).get("steps",[]):
                step["executable"]=False
            result["plan"]["revalidation_required"]=bool(result["plan"].get("steps"))
        result["plan"]["playback"]=domain.playback_summary(result["plan"].get("steps",[]))
        if current and current_proof:
            result["confirmed_snapshot_id"]=current["id"]
            result["plan"]["status"]="CONFIRMED"
            result["status"]="CONFIRMED";result["can_confirm"]=False
        return result

    def model_current(self,model):
        versions=model.get("processing_versions") or {}
        return all(versions.get(key)==self.release.get(key) for key in ("preprocess_version","matching_version","geometry_backend_version"))

    def preserve_model(self,model,project_id):
        """Publish every referenced shape/scene before linking the new model."""
        model=path_values(model,self.root)
        published={}
        def visit(value):
            if isinstance(value,dict):return {k:visit(v) for k,v in value.items()}
            if isinstance(value,list):return [visit(v) for v in value]
            if isinstance(value,str):
                try:
                    path=Path(value)
                    managed=path.is_absolute() and path.is_file() and path.resolve().is_relative_to(self.root)
                except (ValueError,OSError):managed=False
                if managed:
                    key=str(path.resolve())
                    if key not in published:
                        # Publication failures must abort the project update;
                        # never fall back to linking an unregistered cache file.
                        asset=self.store.register_file(path,"model_artifact",project_id)
                        published[key]=str(self.store.file_path(asset["id"]))
                    return published[key]
            return value
        return path_values(visit(model),self.root,encode=True)

    def preserve_search_trace(self, plan, job):
        """Publish complete search evidence before saving a compact plan row."""
        attempts = plan.get('attempt_trace')
        if not isinstance(attempts, list):
            return
        path = self.root / 'jobs' / job['id'] / 'search-attempts.json'
        atomic_json(path, {'format': 'pilot-search-attempts/1', 'job_id': job['id'],
            'project_id': job['project_id'], 'build_id': self.release['build_id'],
            'geometry_hash': job['geometry_hash'], 'model_hash': job.get('model_hash'),
            'algorithm_version': plan.get('algorithm_version'), 'attempts': attempts})
        asset = self.store.register_file(path, 'search_trace', job['project_id'])
        # File copy/hash and relational registration must succeed first. A
        # failure leaves the previous project revision and worker result intact.
        plan['attempt_trace_artifact'] = {'format': 'pilot-search-attempts/1',
            'file_id': asset['id'], 'sha256': asset['sha256'], 'size': asset['size'],
            'count': len(attempts), 'url': '/api/files/' + asset['id']}
        plan['attempt_trace_summary'] = {'count': len(attempts),
            'status_counts': dict(Counter(row.get('status', 'UNKNOWN') for row in attempts))}
        del plan['attempt_trace']

    def apply_result(self,job,result):
        with self.lock:
            project=self.project(job["project_id"]);kind=job["kind"]
            if kind=="import":
                if project["revision"]!=job["input_revision"]:
                    raise DomainError("STALE_RESULT","가져오기 중 프로젝트가 변경되어 결과를 자동 적용하지 않았습니다.")
                model=self.preserve_model(result,project["id"])
                if project.get("model"):
                    archived=self.root/"snapshots"/(uid("before-reprocess-")+".json")
                    atomic_json(archived,project)
                    asset=self.store.register_file(archived,"preprocessing_history",project["id"])
                    self.store.append_event(project["id"],"model_replaced",{"file_id":asset["id"],"previous_revision":project["revision"],"message":"모델 교체 전 원본·작업·규칙·순서 보존"})
                project["source_files"]=job.get("source_files",[])
                model["part_files"]=[a for a in project["source_files"] if a["role"]=="part_step"]
                project["model"]=model
                project["input_revision"]=uid("input-")
                units=model.get("units",[])
                project["rules"]["fixed_unit_ids"]=[u["id"] for u in units if u.get("fixed")]
                project["rules"]["precedence"]=[]
                project["rules"]["contact_policies"]=[]
                project["rules"]["initial_obstacle_ids"]=[]
                project["rules"].pop("geometry_review",None)
                for key in ("entry_conditions","process_groups","process_policy"):
                    project["rules"].pop(key,None)
                project["plan"]={"id":uid("plan-"),"revision":1,"steps":[],"status":"EMPTY"}
                project.pop('generation_report',None)
                project["validation"]=None;project["status"]="MODEL_READY"
                message=f"STEP 처리 완료 · {len(model.get('occurrences',[]))}개 배치 · {len(units)}개 조립 단위"
            elif kind=="verify_matching":
                if project["revision"]!=job["input_revision"]:raise DomainError("STALE_RESULT","대응 검사 중 작업이 변경되어 결과를 적용하지 않았습니다.")
                checks={row["unit_id"]:row["matching"] for row in result["unit_results"]}
                for unit in project["model"]["units"]:
                    if unit["id"] in checks:unit["matching"]=checks[unit["id"]]
                domain.refresh_matching_quality(project["model"])
                project["model"]["diagnostics"]=[d for d in project["model"].get("diagnostics",[]) if not (d.get("code")=="MATCH_GEOMETRY_REVIEW" and d.get("unit_id") in checks)]+result.get("diagnostics",[])
                project["validation"]=None
                project["plan"]["status"]="NEEDS_VALIDATION"
                for step in project["plan"].get("steps",[]):step.update(status="UNKNOWN",executable=False,planning_state="UNPLANNED",path=None,reason="EDIT_REQUIRES_VALIDATION")
                project["plan"]["playback"]=domain.playback_summary(project["plan"].get("steps",[]))
                project["status"]="REVIEW_REQUIRED"
                message="실제 형상 대응 검사 완료 · 순서 재검증 필요"
            elif kind=="process_suggestions":
                if domain.model_hash(project)!=job["model_hash"]:raise DomainError("STALE_RESULT","모델이 변경되어 체결품 후보를 다시 검사해야 합니다.")
                if domain.process_profile_hash(project)!=result.get("dependency_hash"):raise DomainError("STALE_RESULT","고정품 또는 부품 정보가 변경되어 체결품 후보를 다시 검사해야 합니다.")
                result["dependency_hash"]=domain.process_profile_hash(project)
                self.store.set_setting("process_suggestions:"+project["id"],result)
                return {"message":"체결품 후보 검사 완료 · 고정 대상과 순서를 확인하세요.","project_id":project["id"]}
            elif kind in ("generate","validate"):
                if domain.geometry_hash(project)!=job["geometry_hash"]:
                    raise DomainError("STALE_RESULT","계산 중 조건이 변경되어 이전 결과를 적용하지 않았습니다.")
                analysis_changed=False
                if result.get('analysis_model') is not None:
                    updated=copy.deepcopy(result['analysis_model'])
                    # Work labels may change while geometry is being checked.
                    # Keep the current unit metadata; only analysis readiness
                    # comes from the worker's replacement representation.
                    readiness={unit['id']:unit.get('collision_ready',False) for unit in updated.get('units',[])}
                    updated['units']=[{**unit,'collision_ready':readiness.get(unit['id'],False)}
                        for unit in project['model'].get('units',[])]
                    project['model']=self.preserve_model(updated,project['id'])
                    analysis_changed=True
                    project['validation']=None
                    project['plan']['status']='NEEDS_VALIDATION'
                    project['status']='REVIEW_REQUIRED'
                    project.pop('confirmed_snapshot_id',None)
                    for step in project['plan'].get('steps',[]):
                        step.update(status='UNKNOWN',executable=False,planning_state='UNPLANNED',path=None,
                                    reason='ANALYSIS_MODEL_CHANGED')
                    project['plan']['playback']=domain.playback_summary(project['plan'].get('steps',[]))
                if kind=="generate":
                    plan=result["plan"];plan.setdefault("id",uid("plan-"));plan["revision"]=project["plan"].get("revision",0)+1
                    self.preserve_search_trace(plan, job)
                    project['generation_report']={key:copy.deepcopy(plan[key]) for key in ('status','kind','generation_method','diagnostics','placement_audit','disassembly','search','timing','attempt_trace_artifact','attempt_trace_summary','learning_eligibility') if key in plan}
                    if result.get('analysis_refresh'):project['generation_report']['analysis_refresh']=copy.deepcopy(result['analysis_refresh'])
                    if plan.get('replace_plan') is False:
                        project['revision']+=1
                        saved=self.store.save_project(project,project['row_version'],{'kind':'generation_incomplete','job_id':job['id'],'message':'분해 탐색 미완료 · 기존 조립 계획 보존'})
                        return {'project_id':project['id'],'revision':saved['revision'],'status':'INCOMPLETE',
                            'message':f"분해 탐색 미완료 · {plan.get('disassembly',{}).get('removed_count',0)}개 제거 경로 확인 · 기존 계획 보존"+(' · 형상 갱신으로 재검증 필요' if analysis_changed else '')}
                    unit_map={u["id"]:u for u in project["model"].get("units",[])}
                    for step in plan.get("steps",[]):
                        step.setdefault("id",uid("step-"));step.setdefault("name",unit_map.get(step.get("unit_id"),{}).get("name","조립"))
                        step.setdefault("reviewed",False);step.setdefault("caution","");step.setdefault("torque",None)
                        role=unit_map.get(step.get("unit_id"),{}).get("role","")
                        step.setdefault("torque_status","required" if role in ("fastener","bolt","nut") else "not_applicable")
                    project["plan"]=plan
                validation=result["validation"]
                # Canonical paths returned by validation replace edited/stale paths.
                validated_steps=validation.get("steps") or validation.get("validated_steps")
                if isinstance(validated_steps,list):
                    checks={s.get("id",s.get("step_id")):s for s in validated_steps}
                    for step in project["plan"].get("steps",[]):
                        checked=checks.get(step["id"])
                        if checked:
                            for key in ("path","status","diagnostics","executable","planning_state","reason","proposed_entry","rejected_candidate_summary","process_diagnostic","support_diagnostic","support_evidence"):
                                if key in checked:step[key]=checked[key]
                project["plan"]["playback"]=domain.playback_summary(project["plan"].get("steps",[]))
                validation.update(id=uid("validation-"),dependency_hash=domain.geometry_hash(project),validated_at=now(),
                                  build_id=self.release["build_id"],algorithm_version=self.release.get("algorithm_version"))
                if not domain.coverage(project)["complete"] and validation.get("status")=="PASS":
                    validation.update(status="UNKNOWN",reason="조립 단위 전체 포함 조건을 충족하지 않았습니다.")
                project["validation"]=validation
                project["plan"]["status"]="PASS" if validation.get("status")=="PASS" else "DRAFT"
                project["status"]="VALIDATED" if validation.get("status")=="PASS" else "REVIEW_REQUIRED"
                playable=project["plan"]["playback"]["executable_prefix_count"]
                message=("조립순서 생성·검증 통과" if kind=="generate" else "조립성 검증 통과") if validation.get("status")=="PASS" else f"검증 경로 {playable}단계 · 나머지 단계는 진단 확인 후 재검증 필요"
            elif kind=="export":
                path=Path(path_values(result["video_path"],self.root))
                asset=self.store.register_file(path,"video",project["id"])
                result.update(file_id=asset["id"],url="/api/files/"+asset["id"],build_id=self.release["build_id"])
                result.pop("video_path",None)
                project.setdefault("exports",[]).append(result);message="MP4 생성·디코딩 검증 완료"
            elif kind=="train":
                result=self.preserve_model(result,project["id"])
                models=self.store.get_setting("learning_models",[]);models.append(result)
                self.store.set_setting("learning_models",models)
                self.store.append_event(project["id"],"learning_completed",result)
                return {"message":result.get("message","학습 평가 완료"),"learning":result}
            else:raise ValueError(kind)
            project["revision"]+=1
            saved=self.store.save_project(project,project["row_version"],{"kind":kind+"_completed","job_id":job["id"],"result_status":project["status"]})
            return {"project_id":project["id"],"revision":saved["revision"],"message":message,"status":project["status"]}

    def close(self):
        self.stop_event.set();self.learning_dispatcher.join(timeout=20)
        self.jobs.close();self.store.close()


def create_app(data_dir=None,resources=None):
    release=release_info()
    from .release import default_data_dir
    root=Path(data_dir or default_data_dir())
    resource_dir=Path(resources or os.environ.get("PILOT_RESOURCE_DIR") or RESOURCE_ROOT)
    @asynccontextmanager
    async def lifespan(app):
        services=Services(root,resource_dir,release);app.state.services=services
        try:yield
        finally:
            import asyncio
            await asyncio.to_thread(services.close)
    app=FastAPI(title="PILOT Studio",version=release["version"],lifespan=lifespan)
    def service():return app.state.services

    @app.middleware("http")
    async def authenticated(request:Request,call_next):
        host=request.headers.get("host","").split(":")[0]
        if host not in ("127.0.0.1","localhost","testserver"):
            return JSONResponse(status_code=403,content={"detail":"로컬 주소만 사용할 수 있습니다."})
        origin=request.headers.get("origin")
        if origin and not re.fullmatch(r"https?://(127\.0\.0\.1|localhost)(:\d+)?",origin):
            return JSONResponse(status_code=403,content={"detail":"외부 사이트의 요청은 허용되지 않습니다."})
        token=request.headers.get("X-Pilot-Token") or request.cookies.get("pilot_session")
        bootstrap=request.query_params.get("token") if request.url.path=="/" else None
        public=request.url.path=="/api/health" or not request.url.path.startswith("/api/")
        if not public and not secrets.compare_digest(str(token or ""),service().token):
            return JSONResponse(status_code=401,content={"detail":"앱 연결이 만료되었습니다. 프로그램을 다시 열어 주세요."})
        response=await call_next(request)
        if bootstrap and secrets.compare_digest(bootstrap,service().token):
            response.set_cookie("pilot_session",bootstrap,httponly=True,samesite="strict")
        response.headers["X-Content-Type-Options"]="nosniff"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(_,exc):
        return JSONResponse(status_code=409 if exc.code in ("REVISION_CONFLICT","JOB_BUSY","STALE_RESULT") else 422,
                            content={"detail":str(exc),"code":exc.code,"details":exc.details})
    @app.exception_handler(StorageConflict)
    async def conflict(_,exc):return JSONResponse(status_code=409,content={"detail":str(exc),"current":exc.current})
    @app.exception_handler(StorageError)
    async def storage_error(_,exc):
        return JSONResponse(status_code=413 if exc.code=='STORAGE_PAYLOAD_TOO_LARGE' else 503,
            content={'detail':str(exc),'code':exc.code,'details':exc.details})
    @app.exception_handler(KeyError)
    async def missing(_,exc):return JSONResponse(status_code=404,content={"detail":"항목을 찾을 수 없습니다.","id":str(exc)})

    @app.get("/api/health")
    def health():
        return {**release,"status":"ready","storage":service().store.status(),"pid":os.getpid(),"session_fingerprint":hashlib.sha256(service().token.encode()).hexdigest()}
    @app.post("/api/shutdown")
    def shutdown():
        if SERVER is not None:SERVER.should_exit=True
        return {"status":"stopping"}
    @app.get("/api/projects")
    def projects():return service().store.list_projects()
    @app.post("/api/projects")
    def create_project(body:dict=Body(default={})):
        return service().store.create_project(domain.new_project(body))
    @app.get("/api/projects/{ident}")
    def get_project(ident:str):return service().aggregate(ident)
    @app.patch("/api/projects/{ident}")
    def update_project(ident:str,body:dict=Body(...)):
        svc=service()
        with svc.lock:
            old=svc.project(ident);project=domain.patch_project(old,body)
            result=svc.store.save_project(project,old["row_version"],{"kind":"user_edit","before_geometry_hash":domain.geometry_hash(old),"after_geometry_hash":domain.geometry_hash(project),"before_steps":[s["id"] for s in old["plan"].get("steps",[])],"after_steps":[s["id"] for s in project["plan"].get("steps",[])]})
        return svc.aggregate(result["id"])

    async def receive(upload,folder):
        filename=(upload.filename or "model.step").replace("\\","/").rsplit("/",1)[-1]
        if Path(filename).suffix.lower() not in (".stp",".step"):
            raise DomainError("STEP_EXTENSION","STEP(.stp/.step) 파일을 선택해 주세요.")
        folder.mkdir(parents=True,exist_ok=True);path=folder/filename;size=0
        with path.open("xb") as outgoing:
            while chunk:=await upload.read(1024*1024):
                size+=len(chunk)
                if size>2*1024**3:raise DomainError("FILE_LIMIT","개별 STEP 파일은 2 GB 이하로 등록해 주세요.")
                outgoing.write(chunk)
            outgoing.flush();os.fsync(outgoing.fileno())
        await upload.close()
        return path

    @app.post("/api/projects/{ident}/import")
    async def import_files(ident:str,assembly:UploadFile=File(...),parts:list[UploadFile]=File(default=[]),expected_revision:int=Form(...)):
        svc=service();project=svc.project(ident);domain.expect(project,expected_revision)
        names=[(part.filename or "model.step").replace("\\","/").rsplit("/",1)[-1].casefold() for part in parts]
        if len(names)!=len(set(names)):raise DomainError("DUPLICATE_PART_FILENAME","부품 파일 이름이 중복됩니다. 각 부품을 구분할 수 있도록 파일 이름을 다르게 지정해 주세요.")
        folder=svc.root/"inputs"/uid("input-")
        assembly_path=await receive(assembly,folder/"assembly")
        part_paths=[await receive(part,folder/("part-"+str(index))) for index,part in enumerate(parts)]
        descriptors=[]
        for index,path in enumerate([assembly_path,*part_paths]):
            descriptors.append(svc.store.register_file(path,"assembly_step" if index==0 else "part_step",ident))
        # Keep the last complete model and its source files together until the
        # new worker succeeds. A failed import cannot relabel the old model.
        with svc.lock:
            project=svc.project(ident);domain.expect(project,expected_revision)
            return svc.jobs.submit(project,"import",{"assembly_path":str(assembly_path),"part_paths":[str(p) for p in part_paths],"source_files":descriptors,"cache_dir":str(svc.root/"cache"/uid()),"timeout_seconds":3600})

    @app.get("/api/projects/{ident}/scene")
    def scene(ident:str):
        svc=service();model=svc.project(ident).get("model") or {}
        path=model.get("scene_path")
        if not path:raise DomainError("SCENE_NOT_READY","표시 모델이 아직 준비되지 않았습니다.")
        return FileResponse(path_values(path,svc.root),media_type="application/json")

    @app.post("/api/projects/{ident}/reprocess")
    def reprocess(ident:str,body:dict=Body(...)):
        svc=service()
        with svc.lock:
            project=svc.project(ident);domain.expect(project,body.get("expected_revision"))
            sources=project.get("source_files") or []
            assemblies=[s for s in sources if s.get("role")=="assembly_step"]
            if len(assemblies)!=1:raise DomainError("SOURCE_REQUIRED","보관된 완제품 원본을 찾을 수 없습니다. STEP을 다시 등록해 주세요.")
            folder=svc.root/"inputs"/uid("reprocess-")
            assembly_path=None;part_paths=[]
            for index,source in enumerate(sources):
                if source.get("role") not in ("assembly_step","part_step"):continue
                original=svc.store.file_path(source["id"],verify=True)
                name=source.get("original_name") or source.get("name") or original.name
                name=Path(str(name).replace("\\","/")).name
                target=folder/str(index)/name;target.parent.mkdir(parents=True,exist_ok=True)
                shutil.copy2(original,target)
                if source["role"]=="assembly_step":assembly_path=str(target)
                else:part_paths.append(str(target))
            return svc.jobs.submit(project,"import",{"assembly_path":assembly_path,"part_paths":part_paths,"source_files":sources,"cache_dir":str(svc.root/"cache"/uid()),"timeout_seconds":3600})

    def computation(ident,kind,body):
        svc=service();project=svc.project(ident);domain.expect(project,body.get("expected_revision"))
        if not project.get("model"):raise DomainError("MODEL_REQUIRED","먼저 STEP을 등록해 주세요.")
        if not svc.model_current(project["model"]):raise DomainError("MODEL_REPROCESS_REQUIRED","새 전처리·형상 대조 기준을 적용하려면 보관된 원본으로 모델을 재처리해 주세요.")
        domain.check_rules(project["model"],project["rules"])
        active=next((m for m in svc.store.get_setting("learning_models",[]) if m.get("active")),None)
        if active and project.get("part_number") not in active.get("supported_part_numbers",[]):active=None
        from .planning_settings import analysis_job_timeout
        return svc.jobs.submit(project,kind,{"project":project,"ranking_model":active,"cache_dir":str(svc.root/"analysis"/ident),"timeout_seconds":analysis_job_timeout(project['rules'],kind)},body.get("request_id"))
    @app.post("/api/projects/{ident}/generate")
    def generate(ident:str,body:dict=Body(...)):return computation(ident,"generate",body)
    @app.post("/api/projects/{ident}/validate")
    def validate(ident:str,body:dict=Body(...)):return computation(ident,"validate",body)
    @app.post("/api/projects/{ident}/verify-matching")
    def verify_matching(ident:str,body:dict=Body(...)):
        svc=service();project=svc.project(ident);domain.expect(project,body.get("expected_revision"))
        if not project.get("model"):raise DomainError("MODEL_REQUIRED","먼저 STEP을 등록해 주세요.")
        if not svc.model_current(project["model"]):raise DomainError("MODEL_REPROCESS_REQUIRED","새 검사 기준으로 원본 모델을 먼저 재처리해 주세요.")
        ids=body.get("unit_ids")
        if ids is not None and (not isinstance(ids,list) or not ids or any(not isinstance(i,str) for i in ids) or len(ids)!=len(set(ids)) or not set(ids)<={u["id"] for u in project["model"]["units"]}):
            raise DomainError("INVALID_MATCHING_SCOPE","검사할 실제 조립 단위를 하나 이상 선택해 주세요.")
        return svc.jobs.submit(project,"verify_matching",{"model":project["model"],"unit_ids":body.get("unit_ids"),"cache_dir":str(svc.root/"matching"/uid()),"timeout_seconds":3600})

    @app.get("/api/projects/{ident}/process-suggestions")
    def process_suggestions(ident:str):
        project=service().project(ident)
        if not project.get("model"):raise DomainError("MODEL_REQUIRED","먼저 STEP을 등록해 주세요.")
        result=service().store.get_setting("process_suggestions:"+ident)
        if not result or result.get("dependency_hash")!=domain.process_profile_hash(project):raise DomainError("PROCESS_CANDIDATES_REQUIRED","현재 부품 대응·고정 기준으로 체결품 후보 검사를 먼저 실행해 주세요.")
        return result

    @app.post("/api/projects/{ident}/process-suggestions")
    def start_process_suggestions(ident:str,body:dict=Body(...)):
        svc=service();project=svc.project(ident);domain.expect(project,body.get("expected_revision"))
        if not project.get("model"):raise DomainError("MODEL_REQUIRED","먼저 STEP을 등록해 주세요.")
        if not svc.model_current(project["model"]):raise DomainError("MODEL_REPROCESS_REQUIRED","새 검사 기준으로 원본 모델을 먼저 재처리해 주세요.")
        model=copy.deepcopy(project["model"])
        fixed=set(project["rules"].get("fixed_unit_ids",[]))
        for unit in model["units"]:unit["fixed"]=unit["id"] in fixed
        return svc.jobs.submit(project,"process_suggestions",{"model":model,"process_profile_hash":domain.process_profile_hash(project),"timeout_seconds":360})
    @app.get("/api/jobs/{ident}")
    def job(ident:str):
        result=service().store.get_job(ident)
        if result is None:raise KeyError(ident)
        return result
    @app.post("/api/jobs/{ident}/cancel")
    def cancel(ident:str):return service().jobs.cancel(ident)

    @app.post("/api/projects/{ident}/confirm")
    def confirm(ident:str,body:dict=Body(...)):
        svc=service()
        with svc.lock:
            project=svc.project(ident);domain.expect(project,body.get("expected_revision"))
            if any(j.get("status") in ("queued","running","cancel_requested") for j in svc.store.list_jobs(ident)):
                raise DomainError("JOB_BUSY","진행 중인 작업이 끝난 후 현재 결과를 확인하고 확정해 주세요.")
            errors=domain.confirmation_errors(project,release)
            if errors:raise DomainError("CONFIRMATION_BLOCKED","순서를 확정할 수 없습니다. "+" / ".join(errors[:6]),{"reasons":errors})
            signature=domain.confirmation_hash(project)
            for existing in svc.store.snapshots(ident):
                if existing.get("source_hash")==signature:return existing
            snapshot={"id":uid("snapshot-"),"status":"CONFIRMED","source_hash":signature,"project":copy.deepcopy(project),"release":release,"confirmed_at":now(),"reviewer":str(body.get("reviewer") or "사용자"),"reason":str(body.get("reason") or "")}
            snapshot_path=svc.root/"snapshots"/(snapshot["id"]+".json");atomic_json(snapshot_path,snapshot)
            asset=svc.store.register_file(snapshot_path,"confirmation_snapshot",ident);snapshot["file_id"]=asset["id"]
            return svc.store.add_snapshot(ident,snapshot,expected_revision=project["row_version"])

    @app.get("/api/projects/{ident}/history")
    def history(ident:str):
        svc=service();svc.project(ident)
        from .learning import build_dataset,reviewed_histories
        snapshots=svc.store.snapshots(ident);dataset=build_dataset(reviewed_histories(snapshots,svc.store.get_setting("learning_reviews",[])))
        company=[x for x in svc.store.get_setting("company_evaluations",[]) if x["project_id"]==ident]
        field=[x for x in svc.store.get_setting("field_tests",[]) if x["project_id"]==ident]
        learning_settings=svc.store.get_setting("learning_settings",{})
        models=svc.store.get_setting("learning_models",[])
        learning={"eligible_count":len(dataset["eligible_confirmations"]),"excluded":dataset["excluded"],"models":models,"active_model":next((m["id"] for m in models if m.get("active")),"휴리스틱"),"company_evaluation_count":len(company),"field_test_count":len(field),"can_train":bool(learning_settings.get("minimum_cases")),"status":"collecting","settings":learning_settings}
        return {"snapshots":snapshots,"events":svc.store.events(ident),"jobs":svc.store.list_jobs(ident),"learning":learning,"company_evaluations":company,"field_tests":field}
    @app.get("/api/files/{ident}")
    def file(ident:str):
        store=service().store;asset=store.get_file(ident)
        if not asset:raise KeyError(ident)
        return FileResponse(store.file_path(ident,verify=True),media_type=mimetypes.guess_type(asset["name"])[0] or "application/octet-stream",filename=asset["name"],content_disposition_type="inline")

    @app.get("/api/templates")
    def templates():return service().store.get_setting("video_templates",[])
    @app.post("/api/templates")
    async def add_template(file:UploadFile=File(...),name:str=Form(...),approved:bool=Form(False),layout:str=Form("custom")):
        svc=service();filename=(file.filename or "template.png").replace("\\","/").rsplit("/",1)[-1]
        if Path(filename).suffix.lower() not in (".png",".jpg",".jpeg",".webp"):
            raise DomainError("TEMPLATE_FORMAT","영상 배경 이미지는 PNG/JPG/WebP로 등록해 주세요.")
        data=await file.read(20*1024*1024+1)
        if len(data)>20*1024*1024:raise DomainError("TEMPLATE_SIZE","템플릿 이미지는 20 MB 이하로 등록해 주세요.")
        from PIL import Image
        import io
        try:
            image=Image.open(io.BytesIO(data));image.verify()
        except Exception as exc:raise DomainError("TEMPLATE_INVALID","읽을 수 있는 이미지 파일이 아닙니다.") from exc
        folder=svc.root/"template-import"/uid();folder.mkdir(parents=True);path=folder/filename;path.write_bytes(data)
        asset=svc.store.register_file(path,"video_template")
        record={"id":uid("template-"),"name":name,"approved":approved,"revision":1,"layout":layout,"file_id":asset["id"],"background_url":"/api/files/"+asset["id"],"created_at":now(),"fields":["part_name","part_number","torque","caution"]}
        records=templates();records.append(record);svc.store.set_setting("video_templates",records);return record
    @app.patch("/api/templates/{ident}")
    def edit_template(ident:str,body:dict=Body(...)):
        svc=service();records=templates()
        for record in records:
            if record["id"]==ident:
                for key in ("name","approved","layout","fields"):
                    if key in body:record[key]=body[key]
                record["revision"]+=1;svc.store.set_setting("video_templates",records);return record
        raise KeyError(ident)

    @app.get("/api/projects/{ident}/exports")
    def exports(ident:str):return service().project(ident).get("exports",[])
    @app.post("/api/projects/{ident}/exports")
    def export(ident:str,body:dict=Body(...)):
        svc=service();project=svc.project(ident);domain.expect(project,body.get("expected_revision"))
        draft=bool(body.get("draft",False));selected=next((t for t in templates() if t["id"]==body.get("template_id")),None)
        if not selected:raise DomainError("TEMPLATE_REQUIRED","영상 템플릿을 선택해 주세요.")
        if not draft and not selected.get("approved"):raise DomainError("TEMPLATE_REVIEW_REQUIRED","회사 양식으로 사용할 템플릿을 검토해 주세요.")
        snapshots=svc.store.snapshots(ident)
        snapshot=next((s for s in snapshots if s["id"]==body.get("snapshot_id")),None) if body.get("snapshot_id") else (snapshots[0] if snapshots else None)
        if not draft and snapshot is None:raise DomainError("CONFIRMATION_REQUIRED","영상 출력 전 순서를 확정해 주세요.")
        state=project if draft else snapshot["project"]
        if not state.get("plan",{}).get("steps"):raise DomainError("PLAN_REQUIRED","출력할 조립순서가 없습니다.")
        playback=domain.playback_summary(state["plan"]["steps"])
        proof=state.get("validation") or {}
        if playback["executable_prefix_count"]!=playback["total_count"] or proof.get("dependency_hash")!=domain.geometry_hash(state) or proof.get("build_id")!=svc.release["build_id"]:
            raise DomainError("VERIFIED_PATHS_REQUIRED","모든 단계의 경로를 현재 버전으로 검증한 후 영상을 생성해 주세요. 보류된 단계는 영상으로 재생할 수 없습니다.")
        if "seconds_per_step" in body:raise DomainError("STEP_TIMING_REQUIRED","영상 시간은 순서 단계의 재생 시간으로 저장한 뒤 확정해 주세요.")
        settings={"width":int(body.get("width",1280)),"height":int(body.get("height",720)),"fps":int(body.get("fps",24))}
        if (settings["width"],settings["height"]) not in ((640,360),(1280,720),(1920,1080)) or settings["fps"] not in (12,24,30):
            raise DomainError("RENDER_SETTINGS","지원하는 영상 크기·프레임·단계 시간을 선택해 주세요.")
        render_id=uid("render-")
        render={"id":render_id,"snapshot":state,"snapshot_id":snapshot["id"] if snapshot and not draft else None,"template":copy.deepcopy(selected),"settings":settings,"draft":draft,"project_id":ident,"scene_url":"/api/renders/"+render_id+"/scene","created_at":now()}
        svc.store.set_setting("render:"+render_id,render)
        return svc.jobs.submit(project,"export",{"render":render,"render_id":render_id,"base_url":svc.base_url,"token":svc.token,"timeout_seconds":7200})
    @app.get("/api/renders/{ident}")
    def render_definition(ident:str):
        result=service().store.get_setting("render:"+ident)
        if not result:raise KeyError(ident)
        return result
    @app.get("/api/renders/{ident}/scene")
    def render_scene(ident:str):
        definition=render_definition(ident)
        return FileResponse(path_values(definition["snapshot"]["model"]["scene_path"],service().root),media_type="application/json")

    @app.get("/api/settings")
    def settings():
        svc=service()
        return {"release":release,"storage":svc.store.status(),"learning":svc.store.get_setting("learning_settings",{"minimum_cases":None,"automatic_training":False}),"models":svc.store.get_setting("learning_models",[]),"backups":[{**b,"download_url":"/api/backups/"+b["id"]} for b in svc.store.list_backups()]}
    @app.patch("/api/settings")
    def save_settings(body:dict=Body(...)):
        if "learning" in body:
            learning=body["learning"];minimum=learning.get("minimum_cases")
            if minimum is not None and (type(minimum) is not int or not 1<=minimum<=1000000):raise DomainError("LEARNING_THRESHOLD","재학습 기준은 1 이상 정수로 입력해 주세요.")
            service().store.set_setting("learning_settings",learning)
        return settings()
    @app.post("/api/backups")
    def backup():return service().store.backup()
    @app.get("/api/backups/{ident}")
    def backup_file(ident:str):
        record=next((b for b in service().store.list_backups() if b["id"]==ident),None)
        if not record or not record.get("available"):raise KeyError(ident)
        return FileResponse(record["path"],filename=Path(record["path"]).name)

    @app.post("/api/restores/upload")
    async def restore_upload(file:UploadFile=File(...)):
        import asyncio
        from .release import storage_anchor
        svc=service();folder=svc.root/"restore-import"/uid();folder.mkdir(parents=True)
        archive=folder/"backup.zip";size=0
        with archive.open("xb") as stream:
            while chunk:=await file.read(1024*1024):
                size+=len(chunk)
                if size>64*1024**3:raise DomainError("BACKUP_TOO_LARGE","복원 파일 크기 제한을 초과했습니다.")
                stream.write(chunk)
            stream.flush();os.fsync(stream.fileno())
        await file.close()
        destination=storage_anchor(svc.root)/"restored"/uid("restore-")
        try:result=await asyncio.to_thread(svc.store.restore,archive,destination)
        except (ValueError,RuntimeError) as exc:raise DomainError("RESTORE_VALIDATION_FAILED","백업 검증 또는 복원에 실패했습니다. 기존 저장소는 유지됩니다.",{"reason":str(exc)}) from exc
        with svc.lock:
            restored=svc.store.get_setting("restored_stores",[]);restored.append(result);svc.store.set_setting("restored_stores",restored)
        return result

    @app.post("/api/restores/{ident}/activate")
    def restore_activate(ident:str):
        from .release import stage_active_storage
        svc=service()
        with svc.lock:
            if any(j.get("status") in ("queued","running","cancel_requested") for j in svc.store.list_jobs()):
                raise DomainError("JOB_BUSY","진행 중인 작업을 완료하거나 취소한 후 복원 저장소를 적용해 주세요.")
            record=next((r for r in svc.store.get_setting("restored_stores",[]) if r["id"]==ident),None)
            if not record:raise KeyError(ident)
            return stage_active_storage(record["data_dir"],svc.root)
    @app.post("/api/projects/{ident}/train")
    def train(ident:str,body:dict=Body(...)):
        svc=service();project=svc.project(ident);domain.expect(project,body.get("expected_revision"))
        from .learning import reviewed_histories
        histories=reviewed_histories(svc.store.snapshots(),svc.store.get_setting("learning_reviews",[]))
        return svc.jobs.submit(project,"train",{"histories":histories,"settings":svc.store.get_setting("learning_settings",{}),"output_dir":str(svc.root/"models"),"timeout_seconds":600})

    @app.get("/api/projects/{ident}/learning-reviews")
    def learning_reviews(ident:str):
        from .learning import decision_id
        svc=service();svc.project(ident);records=[]
        for snapshot in svc.store.snapshots(ident):
            state=snapshot["project"];units={u["id"]:u for u in (state.get("model") or {}).get("units",[])}
            decisions=[]
            for decision in state.get("plan",{}).get("decision_trace",[]):
                feasible=decision.get("feasible_unit_ids",[])
                if len(feasible)<2:continue
                decisions.append({"id":decision_id(decision),"installed_ids":decision.get("installed_ids",[]),"feasible_unit_ids":feasible,
                                  "candidates":[{"unit_id":u,"name":units.get(u,{}).get("name",u),"status":"PASS"} for u in feasible]})
            records.append({"id":snapshot["id"],"revision":state.get("revision"),"decisions":decisions})
        return {"snapshots":records,"reviews":[r for r in svc.store.get_setting("learning_reviews",[]) if r.get("project_id")==ident]}

    @app.post("/api/projects/{ident}/learning-reviews")
    def add_learning_review(ident:str,body:dict=Body(...)):
        from .learning import preference_review
        svc=service()
        with svc.lock:
            snapshot=next((s for s in svc.store.snapshots(ident) if s["id"]==body.get("snapshot_id")),None)
            if not snapshot:raise KeyError(body.get("snapshot_id"))
            review=preference_review(snapshot,body);review["project_id"]=ident
            reviews=svc.store.get_setting("learning_reviews",[]);reviews.append(review);svc.store.set_setting("learning_reviews",reviews)
            svc.store.append_event(ident,"expert_preference_review",review)
            return review

    @app.post("/api/learning/models/{ident}/activate")
    def activate_learning_model(ident:str,body:dict=Body(...)):
        svc=service()
        with svc.lock:
            if not str(body.get("reviewer","")).strip() or not str(body.get("reference","")).strip():raise DomainError("MODEL_REVIEW_REQUIRED","적용 검토자와 근거를 입력해 주세요.")
            models=svc.store.get_setting("learning_models",[]);model=next((m for m in models if m["id"]==ident),None)
            if not model or model.get("status") not in ("SHADOW_EVALUATION","APPROVED"):raise DomainError("MODEL_NOT_TRAINED","학습·분리 평가를 완료한 후보 모델을 선택해 주세요.")
            company=next((e for e in svc.store.get_setting("company_evaluations",[]) if e["id"]==body.get("company_evaluation_id")),{})
            field=next((e for e in svc.store.get_setting("field_tests",[]) if e["id"]==body.get("field_test_id")),{})
            for evidence in (company,field):
                if evidence.get("model_id")!=ident or evidence.get("passed") is not True or not evidence.get("reference"):
                    raise DomainError("MODEL_EVIDENCE_REQUIRED","해당 모델의 회사 평가·필드테스트 통과 기록과 근거가 필요합니다.")
            baseline,score=company.get("heuristic_similarity"),company.get("model_similarity")
            if not all(type(x) in (int,float) and 0<=x<=1 for x in (baseline,score)) or score<=baseline:
                raise DomainError("MODEL_GAIN_REQUIRED","동일 평가 자료에서 휴리스틱보다 향상된 회사 유사도 결과가 필요합니다.")
            previous=next((m["id"] for m in models if m.get("active")),None)
            for m in models:m["active"]=m["id"]==ident
            model.update(status="APPROVED",activation={**body,"created_at":now()},operational_evaluation="PASS",company_evaluation="PASS",field_test="PASS")
            svc.store.set_setting("previous_learning_model",previous);svc.store.set_setting("learning_models",models)
            return {"active_model":ident,"previous_model":previous,"scope":"새 순서 생성의 검증 통과 후보 우선순위"}

    @app.post("/api/learning/rollback")
    def rollback_learning(body:dict=Body(...)):
        if not str(body.get("reviewer","")).strip() or not str(body.get("reason","")).strip():raise DomainError("ROLLBACK_REASON","복귀 요청자와 사유를 입력해 주세요.")
        svc=service()
        with svc.lock:
            models=svc.store.get_setting("learning_models",[])
            for model in models:model["active"]=False
            svc.store.set_setting("learning_models",models);svc.store.set_setting("learning_rollback",{**body,"created_at":now()})
            return {"active_model":"휴리스틱"}
    @app.post("/api/projects/{ident}/evaluations")
    def evaluation(ident:str,body:dict=Body(...)):
        svc=service();svc.project(ident)
        kind=body.get("kind")
        if kind not in ("company_evaluation","field_test"):raise DomainError("EVALUATION_KIND","회사 평가 또는 필드테스트를 선택해 주세요.")
        if not str(body.get("reviewer","")).strip() or not str(body.get("result","")).strip():raise DomainError("EVALUATION_DETAILS","검토자와 실제 평가 결과를 입력해 주세요.")
        record={**body,"id":uid("evaluation-"),"project_id":ident,"created_at":now()}
        key="company_evaluations" if kind=="company_evaluation" else "field_tests"
        values=svc.store.get_setting(key,[]);values.append(record);svc.store.set_setting(key,values);svc.store.append_event(ident,kind,record);return record

    frontend=RESOURCE_ROOT/"frontend"/"dist"
    if not frontend.is_dir():frontend=PROJECT_ROOT/"frontend"/"dist"
    app.mount("/assets",StaticFiles(directory=frontend/"assets",check_dir=False),name="assets")
    app.mount("/fonts",StaticFiles(directory=frontend/"fonts",check_dir=False),name="fonts")
    @app.get("/")
    def index():
        if not (frontend/"index.html").is_file():raise HTTPException(503,"화면 빌드가 없습니다. 설치 패키지를 복구해 주세요.")
        return FileResponse(frontend/"index.html",headers={"Cache-Control":"no-store"})
    return app


def run():
    global SERVER
    import uvicorn
    release=release_info()
    expected=os.environ.get("PILOT_BUILD_ID")
    if expected and expected!=release["build_id"]:raise RuntimeError("BUILD_ID_MISMATCH: 실행 구성 요소의 버전이 다릅니다.")
    app=create_app()
    SERVER=uvicorn.Server(uvicorn.Config(app,host="127.0.0.1",port=int(os.environ.get("PILOT_PORT","8772")),log_level="info",access_log=False))
    SERVER.run()


if __name__=="__main__":run()
