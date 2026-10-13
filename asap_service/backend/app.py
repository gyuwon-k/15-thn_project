from __future__ import annotations
import asyncio
import anyio
import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
import sys
import uuid
import zipfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.background import BackgroundTask
from starlette.middleware.trustedhost import TrustedHostMiddleware
from asap_service.backend.jobs import BusyError, JobManager, TERMINAL
from asap_service.backend.storage import DATA, ROOT, checked_id, get_config, now, read_json, run_path, session_path, write_json
from asap_service.backend.transport import command, process_kwargs, terminate_tree

from asap_service.backend import unit_service

MAX_FILE_BYTES = 512 * 1024 * 1024
manager = None
file_mutation_lock = threading.Lock()

async def serialized_file_mutations(request: Request, call_next):
    if request.method not in {'POST','PATCH','PUT','DELETE'} and request.url.path != '/environment':
        return await call_next(request)
    acquired=False
    acquisition=asyncio.create_task(asyncio.to_thread(file_mutation_lock.acquire))
    try:
        await asyncio.shield(acquisition)
        acquired=True
        return await call_next(request)
    except asyncio.CancelledError:
        if not acquired:
            await acquisition
            acquired=True
        raise
    finally:
        if acquired:file_mutation_lock.release()

@asynccontextmanager
async def lifespan(app):
    global manager
    manager = JobManager()
    yield
    manager.shutdown()

app = FastAPI(title="PILOT ASAP", version="1.0.0", lifespan=lifespan)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "testserver"])
app.middleware('http')(serialized_file_mutations)

@app.middleware("http")
async def local_origin(request: Request, call_next):
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
        return JSONResponse({"reason_code": "LOCAL_ONLY", "message": "로컬 화면에서만 접근할 수 있습니다."}, status_code=403)
    return await call_next(request)

@app.exception_handler(FileNotFoundError)
async def missing(_, exc):
    return JSONResponse({"reason_code": "NOT_FOUND", "message": str(exc), "action": "저장 목록을 새로 고침하세요."}, status_code=404)

@app.exception_handler(ValueError)
async def invalid(_, exc):
    return JSONResponse({"reason_code": str(exc).split(":")[0], "message": str(exc), "action": "입력 값을 확인하세요."}, status_code=400)

@app.exception_handler(BusyError)
async def busy(_, exc):
    return JSONResponse({"reason_code": "JOB_BUSY", "message": str(exc), "action": "진행 중인 작업의 완료를 기다리거나 취소하세요."}, status_code=409)

class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    transport: str = "native"
    python: str = ""
    asap_root: str = ""
    weights_path: str = ""
    distribution: str = ""
    workspace_root: str = ""

class ConfigInput(BaseModel):
    model_config = ConfigDict(extra="ignore")
    engine: EngineConfig

class ConvertInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    deflection_mm: float = Field(default=0.1, ge=0.001, le=5)
    angular_deflection_rad: float = Field(default=0.2, ge=0.03, le=1)
    engine_extent: float = Field(default=10, ge=0.1, le=1000)
    adaptive_meshing: bool = True
    brep_repair_policy: Literal["verified_topology", "preserve"] = "verified_topology"
    multi_solid_policy: Literal["preserve", "verified_union"] = "preserve"
    union_tolerance_mm: float = Field(default=1e-6, ge=1e-8, le=0.001)
    penetration_max_faces_per_unit: int = Field(default=2000, ge=0, le=20000)
    penetration_max_face_pairs: int = Field(default=250000, ge=0, le=5000000)

class UnitEditInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str
    operation: Literal["merge", "ungroup", "rename", "reassign", "detach", "split", "undo", "redo","clear_template"]
    unit_ids: list[str] = Field(default_factory=list, max_length=1000)
    name: str | None = Field(default=None, max_length=200)
    member_refs: list[dict] | None = None
    target_unit_id: str | None = None

class UnitConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: str

class SplitInput(BaseModel):
    unit_ids: list[str] = Field(min_length=1, max_length=1000)

class PhysicsProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    contact_eps_mm: float = Field(ge=1e-5, le=10)
    sdf_dx_mm: float = Field(ge=1e-4, le=20)
    sdf_res: int = Field(ge=4, le=200)
    path_col_th_mm: float = Field(ge=0, le=1)
    minimum_separation_mm: float = Field(ge=1e-4, le=100)

class RunInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: str = "learn"
    planner: str = "dfs"
    seed: int = Field(default=0, ge=0, le=2147483647)
    budget: int = Field(default=400, ge=1, le=100000)
    timeout_s: int = Field(default=1800, ge=5, le=86400)
    max_gripper: int = Field(default=3, ge=2, le=20)
    max_pose: int = Field(default=3, ge=1, le=100)
    pose_reuse: int = Field(default=0, ge=0, le=100)
    base_part: str | None = None
    physics_profile: PhysicsProfile | None = None
    expected_unit_revision: str | None = None

def require_session(value):
    directory = session_path(value)
    if not (directory / "session.json").is_file():
        raise FileNotFoundError("SESSION_NOT_FOUND")
    return directory

def busy_check():
    if manager is None:
        raise HTTPException(503, "server initializing")
    if manager.guard.locked():
        raise BusyError("다른 작업이 실행 중입니다.")

@app.get("/health")
def health():
    return {"status": "ok", "service": "pilot-asap", "data_key": hashlib.sha256(str(DATA).casefold().encode()).hexdigest()[:16], "version": app.version, "busy": bool(manager and manager.guard.locked())}

@app.get("/config")
def config_get():
    return {**get_config(), "data_dir": str(DATA), "cad_python": sys.executable}

@app.put("/config")
def config_put(config: ConfigInput):
    busy_check()
    result = config.model_dump()
    if result["engine"]["transport"] not in {"native", "wsl"}:
        raise ValueError("CONFIG_TRANSPORT: native 또는 wsl을 선택하세요.")
    write_json(DATA / "config.json", result)
    return config_get()

def check_environment():
    config = get_config()
    checks = [{"name": "CAD / OCP", "status": "ok" if importlib.util.find_spec("OCP") else "missing", "message": "STEP CAD 작업자 라이브러리"}]
    temporary = DATA / "checks" / uuid.uuid4().hex
    temporary.mkdir(parents=True, exist_ok=True)
    write_json(temporary / "config.json", config)
    log = temporary / "check.log"
    process = None
    try:
        cmd = command(config["engine"], "check", temporary / "config.json", temporary / "output.json")
        with log.open("wb") as output:
            process = subprocess.Popen(cmd, stdout=output, stderr=subprocess.STDOUT, cwd=ROOT, **process_kwargs())
            try:
                process.wait(timeout=90)
            except subprocess.TimeoutExpired:
                terminate_tree(process)
                raise TimeoutError("환경 점검 제한시간을 초과했습니다.")
        result = read_json(temporary / "output.json", {})
        if not result:
            raise RuntimeError(log.read_text(encoding="utf-8", errors="replace")[-4000:] or "엔진 점검 결과가 없습니다.")
        result["checks"] = checks + result.get("checks", [])
        result["checked_at"] = now()
        result["engine"] = config["engine"]
        result["heuristic_ready"] = bool(result.get("heuristic_ready", result.get("heur_out_ready", result.get("ready"))))
        result["ready"] = result["heuristic_ready"] and checks[0]["status"] == "ok"
        write_json(DATA / "environment.json", result)
        return result
    except Exception as exc:
        checks.append({"name": "ASAP engine", "status": "error", "message": str(exc)})
        result = {"ready": False, "learn_ready": False, "heuristic_ready": False, "checks": checks, "checked_at": now(), "engine": config["engine"]}
        write_json(DATA / "environment.json", result)
        return result

@app.get("/environment")
async def environment():
    if manager and manager.guard.locked():
        return {**read_json(DATA / "environment.json", {"ready": False, "checks": []}), "busy": True}
    return await asyncio.to_thread(check_environment)

@app.get("/sessions")
def sessions():
    records = [read_json(x) for x in (DATA / "sessions").glob("*/session.json")]
    return sorted(records, key=lambda x:x.get("updated_at", x.get("created_at", "")), reverse=True)

@app.post("/sessions")
async def upload(file: UploadFile = File(...), input_mode: Literal["assembly_only","assembly_with_parts"] = Form("assembly_only")):
    directory, worker_started = None, False
    try:
        try:
            busy_check()
            filename = (file.filename or "model.step").replace("\\", "/").split("/")[-1]
            if Path(filename).suffix.lower() not in {".stp", ".step"}:
                raise ValueError("PARSE_FAILED: .stp 또는 .step 파일을 선택하세요.")
            session_id = uuid.uuid4().hex
            directory = session_path(session_id)
            source = directory / "source" / "model.step"
            source.parent.mkdir(parents=True)
            size = 0
            with source.open("wb") as output:
                while data := await file.read(1024*1024):
                    size += len(data)
                    if size > MAX_FILE_BYTES:
                        raise ValueError("FILE_TOO_LARGE: 최대 512 MiB 파일을 지원합니다.")
                    output.write(data)
        finally:
            # Finish releasing multipart input before handing the source to a
            # worker, so a request cancellation cannot delete active CAD input.
            with anyio.CancelScope(shield=True):
                await file.close()
        write_json(directory / "session.json", {"id": session_id, "status": "uploaded", "input_mode":input_mode, "source": {"name": filename, "size": size}, "created_at": now(), "updated_at": now()})
        job_id = manager.start("parse", session_id, {"operation": "parse", "session_dir": str(directory), "source_path": str(source), "options": {"source_name": filename}}, timeout_s=1800)
        worker_started = True
        return {"id": session_id, "job_id": job_id}
    except BaseException:
        # Only the fresh, unpublished UUID from this request may be discarded.
        # Cancellation is a BaseException and must follow the same boundary.
        if directory is not None and not worker_started and directory.exists():
            target = directory.resolve()
            if target.parent != (DATA / "sessions").resolve():
                raise RuntimeError("UPLOAD_CLEANUP_PATH: 업로드 폴더 경로가 저장 영역을 벗어났습니다.")
            shutil.rmtree(target)
        raise

@app.get("/sessions/{id}")
def session_get(id: str):
    directory = require_session(id)
    result = read_json(directory / "session.json")
    source_units = read_json(directory / "units.json", [])
    current = unit_service.state(directory)
    result.update(templates=read_json(directory / "templates.json", {"files":[],"templates":[]}), source_units=source_units, unit_state=current, transfer_report=read_json(directory / "transfer_report.json"))
    directory = unit_service.unit_directory(directory) if current else directory
    result.update(units=read_json(directory / "units.json", []), manifest=read_json(directory / "manifest.json", {}),
                  conversion=read_json(directory / "conversion.json"), part_map=read_json(directory / "part_map.json", {}),
                  coordinate_map=read_json(directory / "coordinate_map.json", {}), quality_report=read_json(directory / "quality_report.json", {}))
    result["runs"] = [read_run(x, x.name) for x in (require_session(id) / "runs").glob("*") if x.is_dir()]
    result["runs"].sort(key=lambda x:x.get("completed_at", x.get("created_at", "")), reverse=True)
    return result

class InputModeInput(BaseModel):
    input_mode: Literal["assembly_only","assembly_with_parts"]

class MatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    template_ids:list[str]=Field(min_length=1,max_length=100)
    expected_revision:str
    tolerance_mm:float=Field(default=.5,ge=.01,le=5)
    timeout_s:int=Field(default=120,ge=10,le=600)

class MatchAcceptInput(BaseModel):
    candidate_ids:list[str]=Field(min_length=1,max_length=1000)
    expected_revision:str

class TemplateEditInput(BaseModel):
    name:str=Field(min_length=1,max_length=200)
    part_number:str=Field(default="",max_length=100)
    scope_ids:list[str]|None=None

class ManualMatchInput(BaseModel):
    template_id:str
    unit_ids:list[str]=Field(min_length=1,max_length=1000)
    expected_revision:str

@app.patch("/sessions/{id}/input-mode")
def input_mode(id:str,options:InputModeInput):
    busy_check();directory=require_session(id);record=read_json(directory/"session.json")
    record.update(input_mode=options.input_mode,updated_at=now());write_json(directory/"session.json",record)
    return record

@app.post("/sessions/{id}/templates")
async def template_upload(id:str,files:list[UploadFile]=File(...)):
    directory=require_session(id);busy_check()
    if len(files)>100:raise ValueError("TEMPLATE_LIMIT: 한 번에 100개 이하를 선택하세요.")
    entries=[]
    batch_size=0
    try:
        for file in files:
            name=(file.filename or "part.step").replace("\\","/").split("/")[-1]
            if Path(name).suffix.lower() not in {".stp",".step"}:raise ValueError("TEMPLATE_PARSE_FAILED: STEP 파일을 선택하세요.")
            fid=uuid.uuid4().hex;source=directory/"templates"/fid/"source/model.step";source.parent.mkdir(parents=True)
            size=0;hasher=hashlib.sha256()
            with source.open("wb") as stream:
                while chunk:=await file.read(1024*1024):
                    size+=len(chunk);batch_size+=len(chunk)
                    if batch_size>MAX_FILE_BYTES:raise ValueError("FILE_TOO_LARGE: 한 번의 기준 파일 등록 합계는 512 MiB 이하입니다.")
                    if size>MAX_FILE_BYTES:raise ValueError("FILE_TOO_LARGE: 기준 파일 최대 512 MiB")
                    hasher.update(chunk);stream.write(chunk)
            entries.append({"id":fid,"name":name,"size":size,"sha256":hasher.hexdigest()})
        job_id=manager.start("templates",id,{"operation":"templates","session_dir":str(directory),"files":entries},timeout_s=1800)
        return {"job_id":job_id,"file_ids":[e["id"] for e in entries]}
    finally:
        for file in files:await file.close()

@app.patch("/sessions/{id}/templates/{template}")
def template_edit(id:str,template:str,options:TemplateEditInput):
    from asap_service.backend.matching_service import catalog
    directory=require_session(id);busy_check();data=catalog(directory)
    item=next((t for t in data["templates"] if t["id"]==checked_id(template)),None)
    if not item:raise FileNotFoundError("TEMPLATE_NOT_FOUND")
    if options.scope_ids is not None:
        used=[u for u in read_json(unit_service.unit_directory(directory)/"units.json") if u.get("template_id")==template]
        if used and options.scope_ids!=item['scope_ids']:raise ValueError('TEMPLATE_IN_USE: 단위 편집에서 기준 연결을 해제한 뒤 범위를 변경하세요.')
        available={u["id"]:u for u in read_json(directory/"templates"/template/"units.json")}
        if not options.scope_ids or not set(options.scope_ids)<=set(available):raise ValueError("TEMPLATE_SCOPE_INVALID: 기준 형상 범위를 확인하세요.")
        item.update(scope_ids=options.scope_ids,solid_count=sum(available[x]["solid_count"] for x in options.scope_ids))
    item.update(name=options.name,part_number=options.part_number,matching_status="not_matched")
    data.pop("last_match_id",None);write_json(directory/"templates.json",data);return item

@app.delete("/sessions/{id}/templates/{template}")
def template_remove(id:str,template:str):
    from asap_service.backend.matching_service import catalog
    directory=require_session(id);busy_check();data=catalog(directory);checked_id(template)
    affected=[u["id"] for u in read_json(unit_service.unit_directory(directory)/"units.json") if u.get("template_id")==template]
    if affected:raise ValueError("TEMPLATE_IN_USE: 해당 기준으로 확정한 단위의 근거를 먼저 편집하세요.")
    data["templates"]=[t for t in data["templates"] if t["id"]!=template]
    for file in data["files"]:
        if file["id"]==template:file["status"]="inactive"
    write_json(directory/"templates.json",data);return {"removed_from_active_input":True,"source_file_preserved":True}

@app.get("/sessions/{id}/templates/{template}/scene")
def template_scene(id:str,template:str):
    return FileResponse(require_session(id)/"templates"/checked_id(template)/"display_scene.json",media_type="application/json")

@app.post("/sessions/{id}/matches")
def matching_start(id:str,options:MatchInput):
    directory=require_session(id);busy_check()
    job_id=manager.start("match",id,{"operation":"match","session_dir":str(directory),"matching":options.model_dump()},timeout_s=options.timeout_s+120)
    return {"job_id":job_id}

@app.get("/sessions/{id}/matches/{match}")
def matching_get(id:str,match:str):
    return read_json(require_session(id)/"matching"/checked_id(match)/"candidates.json")

@app.post("/sessions/{id}/matches/{match}/accept")
def matching_accept(id:str,match:str,options:MatchAcceptInput):
    directory=require_session(id);busy_check()
    job_id=manager.start("match_accept",id,{"operation":"match_accept","session_dir":str(directory),
        "matching":{"match_id":checked_id(match),**options.model_dump()}},timeout_s=600)
    return {"job_id":job_id}

@app.post("/sessions/{id}/matches/manual")
def matching_manual(id:str,options:ManualMatchInput):
    from asap_service.backend.matching_service import catalog
    directory=require_session(id);busy_check();data=catalog(directory)
    template=next((t for t in data["templates"] if t["id"]==options.template_id),None)
    if not template:raise FileNotFoundError("TEMPLATE_NOT_FOUND")
    units=read_json(unit_service.unit_directory(directory)/"units.json")
    selected=[u for u in units if u["id"] in options.unit_ids]
    if len(selected)!=len(set(options.unit_ids)):raise ValueError("UNIT_UNKNOWN: 대응할 단위를 확인하세요.")
    candidate={"id":uuid.uuid4().hex,"template_id":template["id"],"template_name":template["name"],
        "part_number":template.get("part_number",""),"member_refs":[m for u in selected for m in u["member_refs"]],"metrics":None}
    job_id=manager.start("match_manual",id,{"operation":"match_manual","session_dir":str(directory),
        "matching":{"expected_revision":options.expected_revision,"candidates":[candidate]}},timeout_s=600)
    return {"job_id":job_id}

@app.get("/sessions/{id}/scene")
def scene(id: str, mode: str = "cad"):
    if mode not in {"cad", "obj", "source"}:
        raise ValueError("SCENE_MODE: cad, obj 또는 source를 선택하세요.")
    directory = require_session(id)
    if mode != "source" and unit_service.state(directory):
        directory = unit_service.unit_directory(directory)
    file = directory / ("obj_scene.json" if mode == "obj" else "display_scene.json")
    if not file.exists():
        raise FileNotFoundError("SCENE_NOT_READY")
    return FileResponse(file, media_type="application/json")

@app.post("/sessions/{id}/convert")
def convert(id: str, options: ConvertInput):
    directory = require_session(id)
    if not (directory / "units.json").exists():
        raise HTTPException(409, detail={"reason_code": "PARSE_REQUIRED", "message": "STEP 해석을 먼저 완료하세요."})
    busy_check()
    if options.multi_solid_policy not in {"preserve", "verified_union"}:
        raise ValueError("MULTI_SOLID_POLICY: preserve 또는 verified_union을 선택하세요.")
    directory = unit_service.require_confirmed(directory)
    job_id = manager.start("convert", id, {"operation": "convert", "session_dir": str(directory), "options": options.model_dump()}, timeout_s=1800)
    return {"job_id": job_id}

@app.get("/sessions/{id}/units")
def units_get(id: str):
    directory = require_session(id)
    return {"state": unit_service.state(directory), "units": read_json(unit_service.unit_directory(directory) / "units.json"),
            "source_units": read_json(directory / "units.json")}

@app.patch("/sessions/{id}/units")
def units_edit(id: str, options: UnitEditInput):
    directory = require_session(id)
    busy_check()
    if unit_service.state(directory).get("revision") != options.expected_revision:
        raise HTTPException(409, detail={"reason_code":"REVISION_CONFLICT", "message":"현재 리비전을 다시 확인하세요."})
    job_id = manager.start("units", id, {"operation":"units", "session_dir":str(directory), "edit":options.model_dump()}, timeout_s=600)
    return {"job_id":job_id}

@app.post("/sessions/{id}/units/confirm")
def units_confirm(id: str, options: UnitConfirmInput):
    directory = require_session(id)
    if manager is None or not manager.guard.acquire(blocking=False):
        raise BusyError("다른 작업이 실행 중입니다.")
    try:
        result = unit_service.confirm(directory, options.expected_revision)
        record = read_json(directory / "session.json")
        record.update(status="units_confirmed", updated_at=now())
        write_json(directory / "session.json", record)
        return result
    finally:
        manager.guard.release()

@app.post("/sessions/{id}/units/split")
def split(id: str, options: SplitInput):
    if len(options.unit_ids) != 1:
        raise ValueError("UNIT_SELECTION: 분할할 단위 하나를 선택하세요.")
    return units_edit(id, UnitEditInput(expected_revision=unit_service.state(require_session(id))["revision"],
                                      operation="split", unit_ids=options.unit_ids))

@app.post("/sessions/{id}/runs")
def run(id: str, options: RunInput):
    directory = require_session(id)
    if options.strategy not in {"learn", "heur-out"} or options.planner != "dfs":
        raise ValueError("STRATEGY_INVALID: learn/heur-out과 dfs만 지원합니다.")
    if manager is None:
        raise HTTPException(503, "server initializing")
    run_id = uuid.uuid4().hex
    run_dir = directory / "runs" / run_id
    run_index = DATA / "runs" / (run_id+".json")
    config = get_config()

    def prepare():
        # JobManager holds its guard throughout preparation and computation.
        # Readiness, geometry and maps must all belong to the same revision.
        if options.expected_unit_revision and unit_service.state(directory)['revision']!=options.expected_unit_revision:
            raise ValueError('REVISION_CONFLICT: 실행 요청 이후 단위 구성이 변경되었습니다.')
        if read_json(directory/"session.json").get("input_mode")=="assembly_with_parts":
            available=read_json(directory/"templates.json",{}).get("templates",[])
            if not any(t.get("status")=="ready" for t in available):raise ValueError("TEMPLATES_REQUIRED: 하위 부품을 등록하거나 완제품만 방식을 선택하세요.")
            active_templates={t['id'] for t in available if t.get('status')=='ready'}
            if not any(u.get('template_id') in active_templates for u in read_json(unit_service.unit_directory(directory)/'units.json')):
                raise ValueError('MATCH_REVIEW_REQUIRED: 하위 부품 매칭 또는 수동 대응을 조립 단위에 적용하세요.')
        geometry_dir = unit_service.require_confirmed(directory)
        conversion = read_json(geometry_dir / "conversion.json", {})
        if not conversion.get("ready", conversion.get("status") == "ready"):
            raise HTTPException(409, detail={"reason_code": conversion.get("reason_code", "CONVERSION_REQUIRED"), "message": "OBJ 변환·품질 검사를 통과해야 실행할 수 있습니다.", "action": "품질 진단의 차단 항목을 확인하세요."})
        part_map = read_json(geometry_dir / "part_map.json", {})
        manifest = read_json(geometry_dir / "manifest.json", {})
        revision = conversion.get("revision", conversion.get("conversion_revision"))
        obj_files = {file.stem for file in (geometry_dir / "obj_analysis").glob("*.obj") if file.is_file()}
        artifacts = ("part_map.json", "coordinate_map.json", "quality_report.json", "manifest.json", "display_scene.json", "obj_scene.json", "units.json", "conversion.json", "membership.json", "confirmation.json")
        if (not part_map or obj_files != set(part_map) or not revision or
            manifest.get("conversion_revision") != revision or any(not (geometry_dir / name).is_file() for name in artifacts)):
            raise HTTPException(409, detail={"reason_code": "CONVERSION_ARTIFACTS_INCONSISTENT", "message": "변환 리비전과 OBJ·매핑 파일이 일치하지 않습니다.", "action": "OBJ 변환과 품질 검사를 다시 실행하세요."})
        required_capabilities = conversion.get("required_engine_capabilities", [])
        if required_capabilities:
            environment = check_environment()
            if not environment.get("heuristic_ready"):
                raise ValueError("ENGINE_NOT_READY: 원본 솔리드 입력을 처리할 ASAP 환경이 준비되지 않았습니다.")
            missing = sorted(set(required_capabilities) - set(environment.get("capabilities", [])))
            if missing:
                raise HTTPException(409, detail={"reason_code": "ENGINE_CAPABILITY_REQUIRED", "message": "원본 솔리드의 점유 형상을 처리할 엔진 기능이 필요합니다: " + ", ".join(missing), "action": "검증된 엔진 어댑터를 사용하세요."})
        if required_capabilities or conversion.get("solids_manifest_path"):
            if conversion.get("solids_manifest_path", "solids_manifest.json") != "solids_manifest.json":
                raise HTTPException(409, detail={"reason_code": "SOLID_MANIFEST_INVALID", "message": "원본 솔리드 manifest 경로가 입력 계약과 다릅니다.", "action": "OBJ 변환을 다시 실행하세요."})
            if not (geometry_dir / "solids_manifest.json").is_file() or not (geometry_dir / "obj_solids").is_dir():
                raise HTTPException(409, detail={"reason_code": "SOLID_INPUT_MISSING", "message": "원본 솔리드 입력 파일이 없습니다.", "action": "OBJ 변환을 다시 실행하세요."})
        settings = options.model_dump()
        settings.pop('expected_unit_revision',None)
        if settings["base_part"]:
            base = settings["base_part"]
            if base not in part_map:
                base = next((k for k,v in part_map.items() if v.get("unit_id", v.get("id")) == base), None)
            if base is None or base not in part_map:
                raise ValueError("BASE_PART_INVALID: 현재 변환에 포함된 부품을 선택하세요.")
            settings["base_part"] = base
        run_dir.mkdir(parents=True)
        shutil.copytree(geometry_dir / "obj_analysis", run_dir / "input_obj")
        if (geometry_dir / "solids_manifest.json").is_file():
            shutil.copy2(geometry_dir / "solids_manifest.json", run_dir / "input_obj" / "solids_manifest.json")
            shutil.copytree(geometry_dir / "obj_solids", run_dir / "input_obj" / "obj_solids")
        for name in artifacts:
            shutil.copy2(geometry_dir / name, run_dir / name)
        run_config = {"settings": settings, "engine": config["engine"], "created_at": now(), "input_revision": revision, "unit_revision":manifest["unit_revision"], "unit_manifest_hash":manifest["unit_manifest_hash"]}
        write_json(run_dir / "run_config.json", run_config)
        write_json(run_index, {"session_id": id, "run_id": run_id})
        return {"assembly_dir": str(run_dir / "input_obj"), "output_dir": str(run_dir), "host_output_dir": str(run_dir), "settings": settings,
                "engine": config["engine"], "part_map": part_map, "coordinate_map": read_json(run_dir / "coordinate_map.json", {}),
                "input_revision": revision, "source_sha256": manifest["source"]["sha256"],
                "required_engine_capabilities": required_capabilities, "nonce": uuid.uuid4().hex}

    try:
        # The engine owns the exact computation deadline. A bounded publication
        # grace lets it save partial tree/timing evidence before hard termination.
        job_id = manager.start("run", id, None, config=config, run_id=run_id, timeout_s=options.timeout_s+10, prepare=prepare)
    except Exception:
        # Only this fresh UUID belongs to the unstarted request.
        if run_dir.exists():
            if run_dir.resolve().parent != (directory / "runs").resolve():
                raise RuntimeError("RUN_CLEANUP_PATH: 실행 폴더 경로가 저장 영역을 벗어났습니다.")
            shutil.rmtree(run_dir)
        run_index.unlink(missing_ok=True)
        raise
    write_json(run_dir / "job.json", {"job_id": job_id})
    return {"id": run_id, "run_id": run_id, "job_id": job_id}

@app.get("/runs/{id}")
def get_run(id: str):
    return read_run(run_path(id), id)

def read_run(directory: Path, id: str):
    result = read_json(directory / "result.json")
    if result:
        config = read_json(directory / "run_config.json", {})
        prep=read_json(directory/'solid_sdf_audit.json',{})
        record=read_json(DATA/'jobs'/(checked_id(result['job_id'])+'.json'),{}) if result.get('job_id') else {}
        result={**result}
        if result.get('elapsed_s') is None and record.get('elapsed_s') is not None:
            result['elapsed_s']=record['elapsed_s']
        if result.get('reason_code')=='TIMEOUT':
            result.update(message='전체 실행 제한시간 안에 작업을 완료하지 못했습니다. 준비 상태와 마지막 탐색 로그를 확인하세요.',
                          action='전처리 성공 여부·실제 평가 수를 확인하고 조립 단위와 탐색 조건을 검토하세요.')
        context = None
        if result.get("reason_code") not in {None, "SUCCESS"} and result.get("job_id"):
            log = DATA / "jobs" / checked_id(result["job_id"]) / "worker.log"
            if log.is_file():
                with log.open("rb") as stream:
                    stream.seek(max(0, log.stat().st_size-50000))
                    tail = stream.read().decode("utf-8", errors="replace")
                events = []
                for line in tail.splitlines():
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and event.get("stage"):
                            events.append(event)
                    except ValueError:
                        pass
                last_part = next((event for event in reversed(events) if event.get("part")), None)
                mapped = result.get("part_map", {}).get(str(last_part["part"]), {}) if last_part else {}
                context = {"last_event": events[-1] if events else None, "last_part_event": last_part,
                           "last_part_name": mapped.get("name"),
                           "unit_ids": [mapped.get("unit_id", mapped.get("id"))] if mapped else [],
                           "association": "last_logged_part_not_confirmed_root_cause", "log_tail": tail[-5000:]}
        return {**result, "preprocessing_status":prep.get('status','unavailable'),
                "preprocessing_summary":{k:prep.get(k) for k in ['preprocess_version','prepared_body_count','input_unit_count','original_solid_count','failed_body_count','failures']},
                "unit_revision": result.get("unit_revision") or config.get("unit_revision"),
                "unit_manifest_hash": result.get("unit_manifest_hash") or config.get("unit_manifest_hash"),
                "diagnostic_context": context}
    job = read_json(directory / "job.json", {})
    job_id = job.get("job_id")
    record = read_json(DATA / "jobs" / (checked_id(job_id)+".json"), {}) if job_id else {}
    fallback = {"id": id, "run_id": id, **read_json(directory / "run_config.json", {}), **job,
                "status": record.get("status", "running")}
    if record.get("error"):
        fallback.update(error=record["error"], reason_code=record["error"].get("reason_code"))
    if record.get("ended_at"):
        fallback["completed_at"] = record["ended_at"]
    return fallback

@app.get("/runs/{id}/steps/{step}")
def get_step(id: str, step: int):
    result = get_run(id)
    steps = result.get("steps", [])
    if step < 0 or step >= len(steps):
        raise FileNotFoundError("STEP_NOT_FOUND")
    return steps[step]

@app.get("/runs/{id}/scene")
def run_scene(id: str, mode: str = "cad"):
    if mode not in {"cad", "obj"}:
        raise ValueError("SCENE_MODE: cad 또는 obj 모드를 선택하세요.")
    file = run_path(id) / ("display_scene.json" if mode == "cad" else "obj_scene.json")
    if not file.exists():
        raise FileNotFoundError("RUN_SCENE_NOT_READY")
    return FileResponse(file, media_type="application/json")

@app.get("/runs/{id}/export")
def export_result(id: str):
    directory, _ = finished_run(id)
    file = directory / "result.json"
    return FileResponse(file, media_type="application/json", filename="asap-"+id[:8]+".json")

def finished_run(id: str):
    directory = run_path(id)
    result = read_json(directory / "result.json", {})
    job_id = result.get("job_id") or read_json(directory / "job.json", {}).get("job_id")
    record = read_json(DATA / "jobs" / (checked_id(job_id)+".json"), {}) if job_id else {}
    if not result or (record and record.get("status") not in TERMINAL):
        raise HTTPException(409, detail={"reason_code": "RUN_NOT_FINISHED", "message": "작업 종료 후 실행 결과를 다운로드할 수 있습니다.", "action": "작업이 완료되거나 취소된 뒤 다시 다운로드하세요."})
    return directory, result

@app.get("/runs/{id}/bundle")
def export_bundle(id: str):
    directory, result = finished_run(id)
    # A download never opens a ZIP another response may still be reading.
    target = DATA / "exports" / (id+"-"+uuid.uuid4().hex+".zip")
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in directory.rglob("*"):
                if file.is_file() and not file.name.endswith(".tmp"):
                    archive.write(file, arcname=str(file.relative_to(directory)).replace("\\", "/"))
            job_id = result.get("job_id")
            if job_id and (DATA / "jobs" / checked_id(job_id) / "worker.log").is_file():
                archive.write(DATA / "jobs" / job_id / "worker.log", arcname="engine.log")
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return FileResponse(target, media_type="application/zip", filename="asap-"+id[:8]+".zip", background=BackgroundTask(target.unlink, missing_ok=True))

@app.get("/jobs/{id}")
def get_job(id: str):
    checked_id(id)
    return manager.get(id)

@app.post("/jobs/{id}/cancel")
def cancel_job(id: str):
    checked_id(id)
    return manager.cancel(id)
