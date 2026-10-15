"""Revision, coverage, and confirmation invariants shared by HTTP and workers."""
from __future__ import annotations
import copy
import hashlib
import json
import math
import uuid
from datetime import datetime, timezone


class DomainError(ValueError):
    def __init__(self, code, message, details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix=""):
    return prefix + uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def new_project(fields):
    return dict(id=uid("project-"), name=str(fields.get("name") or "새 프로젝트"),
                vehicle=str(fields.get("vehicle") or ""), item=str(fields.get("item") or ""),
                part_number=str(fields.get("part_number") or ""), revision=1,
                created_at=now(), updated_at=now(), input_revision=None, model=None,
                rules={"fixed_unit_ids": [], "initial_obstacle_ids": [], "precedence": [],
                       "directions": ["+X", "-X", "+Y", "-Y", "+Z", "-Z"],
                       "beam_width": 4, "max_seconds": 300, "contact_policies": []},
                plan={"id":uid("plan-"), "revision":1, "steps":[], "status":"EMPTY"},
                validation=None, status="EMPTY", exports=[])


def expect(project, expected):
    if expected is None:
        raise DomainError("REVISION_REQUIRED", "현재 개정 번호를 함께 보내 주세요.")
    if expected != project["revision"]:
        raise DomainError("REVISION_CONFLICT", "다른 작업으로 내용이 변경되었습니다. 최신 내용을 다시 불러오세요.",
                          {"expected":expected, "actual":project["revision"]})


def model_hash(project):
    model=project.get("model") or {}
    return digest({"input_revision":project.get("input_revision"),"fingerprint":model.get("fingerprint"),
                   "units":[{k:u.get(k) for k in ("id","occurrence_ids","fixed","role")} for u in model.get("units",[])],
                   "quality":model.get("quality")})


def geometry_hash(project):
    return digest({"model":model_hash(project), "rules":project.get("rules"),
                   "steps":[{k:s.get(k) for k in ("id","unit_id","direction","distance_mm","path")}
                            for s in (project.get("plan") or {}).get("steps",[])]})


def confirmation_hash(project):
    """Identity of confirmed content, independent of export/job bookkeeping."""
    content={key:copy.deepcopy(project.get(key)) for key in ("id","name","vehicle","item","part_number","input_revision","source_files","model","rules","plan","validation")}
    if isinstance(content.get("plan"),dict):
        content["plan"].pop("status",None);content["plan"].pop("revision",None)
    return digest(content)


def coverage(project):
    model=project.get("model") or {}
    ids={u["id"] for u in model.get("units",[])}
    fixed=set((project.get("rules") or {}).get("fixed_unit_ids",[]))
    initially=set((project.get("rules") or {}).get("initial_obstacle_ids",[])) | fixed
    steps=(project.get("plan") or {}).get("steps",[])
    actual=[s.get("unit_id") for s in steps]
    required=ids-initially
    duplicates=sorted({x for x in actual if actual.count(x)>1})
    return {"complete":bool(ids) and set(actual)==required and not duplicates,
            "required":len(required),"covered":len(required&set(actual)),
            "missing":sorted(required-set(actual)),"extra":sorted(set(actual)-required),"duplicates":duplicates}


def check_units(model, units):
    if not isinstance(units,list) or not units:
        raise DomainError("UNITS_REQUIRED","조립 단위를 하나 이상 지정해야 합니다.")
    source={o["id"] for o in model.get("occurrences",[])}
    ids=[u.get("id") for u in units]
    members=[]
    for unit in units:
        if not unit.get("occurrence_ids"):
            raise DomainError("EMPTY_UNIT","구성 부품이 없는 조립 단위는 저장할 수 없습니다.")
        members.extend(unit["occurrence_ids"])
    if None in ids or len(ids)!=len(set(ids)) or len(members)!=len(set(members)) or set(members)!=source:
        raise DomainError("UNIT_PARTITION","모든 실제 배치는 조립 단위 하나에만 포함되어야 합니다.",
                          {"missing":sorted(source-set(members)),"unknown":sorted(set(members)-source)})


def check_rules(model, rules):
    ids={u["id"] for u in model.get("units",[])}
    for key in ("fixed_unit_ids","initial_obstacle_ids"):
        if not set(rules.get(key,[]))<=ids:
            raise DomainError("UNKNOWN_UNIT","고정/초기 부품에 존재하지 않는 단위가 있습니다.")
    directions=rules.get("directions",["+X","-X","+Y","-Y","+Z","-Z"])
    if not directions or any(d not in {"+X","-X","+Y","-Y","+Z","-Z"} for d in directions):
        raise DomainError("INVALID_DIRECTION","지원하는 진입 방향을 선택해 주세요.")
    edges={i:set() for i in ids}
    for edge in rules.get("precedence",[]):
        a,b=edge.get("before"),edge.get("after")
        if a not in ids or b not in ids or a==b:
            raise DomainError("INVALID_PRECEDENCE","선후 관계의 대상이 올바르지 않습니다.")
        edges[a].add(b)
    visited,active=set(),set()
    def visit(node):
        if node in active:raise DomainError("RULE_CYCLE","선후 관계가 서로 순환합니다.")
        if node in visited:return
        active.add(node)
        for nxt in edges[node]:visit(nxt)
        active.remove(node);visited.add(node)
    for node in ids:visit(node)
    for key,low,high in (("max_seconds",1,7200),("beam_width",1,32)):
        value=rules.get(key,300 if key=="max_seconds" else 4)
        if not isinstance(value,(int,float)) or not math.isfinite(value) or not low<=value<=high:
            raise DomainError("INVALID_BUDGET",f"{key} 설정이 허용 범위를 벗어났습니다.")
    for policy in rules.get("contact_policies",[]):
        if policy.get("approved") and not str(policy.get("reason","")).strip():
            raise DomainError("CONTACT_REASON_REQUIRED","접촉 예외 승인에는 사유가 필요합니다.")


def patch_project(project, patch):
    expect(project,patch.get("expected_revision"))
    result=copy.deepcopy(project)
    before=geometry_hash(result)
    for key in ("name","vehicle","item","part_number"):
        if key in patch:
            if not isinstance(patch[key],str) or len(patch[key])>500:
                raise DomainError("INVALID_TEXT","입력 문자열 길이를 확인해 주세요.")
            result[key]=patch[key]
    if "units" in patch:
        if not result.get("model"):raise DomainError("MODEL_REQUIRED","먼저 STEP을 등록해 주세요.")
        check_units(result["model"],patch["units"])
        result["model"]["units"]=copy.deepcopy(patch["units"])
        occurrences={o["id"]:o for o in result["model"]["occurrences"]}
        for unit in result["model"]["units"]:
            members=[occurrences[x] for x in unit["occurrence_ids"]]
            boxes=[m["bbox"] for m in members]
            low=[min(b["min"][i] for b in boxes) for i in range(3)]
            high=[max(b["max"][i] for b in boxes) for i in range(3)]
            unit["bbox"]={"min":low,"max":high,"size":[b-a for a,b in zip(low,high)],"center":[(a+b)/2 for a,b in zip(low,high)]}
            unit["collision_ready"]=all(m.get("collision_ready",False) for m in members)
    if "rules" in patch:
        result["rules"].update(copy.deepcopy(patch["rules"]))
        check_rules(result.get("model") or {},result["rules"])
    if "plan" in patch:
        changes=patch["plan"]
        if not isinstance(changes,dict) or not isinstance(changes.get("steps"),list):
            raise DomainError("INVALID_PLAN","순서 단계 목록을 입력해 주세요.")
        valid_units={u["id"] for u in (result.get("model") or {}).get("units",[])}
        step_ids=[]
        for step in changes["steps"]:
            if step.get("unit_id") not in valid_units or not step.get("id"):
                raise DomainError("INVALID_STEP","단계 ID와 조립 단위를 확인해 주세요.")
            if step.get("direction") not in {"+X","-X","+Y","-Y","+Z","-Z"}:
                raise DomainError("INVALID_DIRECTION","지원하는 진입 방향을 선택해 주세요.")
            value=step.get("distance_mm")
            if not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0:
                raise DomainError("INVALID_DISTANCE","진입 거리는 0보다 큰 유한한 mm 값이어야 합니다.")
            duration=step.get("duration_s",2)
            if type(duration) not in (int,float) or not math.isfinite(duration) or not .2<=duration<=120:
                raise DomainError("INVALID_DURATION","단계 재생 시간은 0.2~120초의 유한한 값이어야 합니다.")
            step_ids.append(step["id"])
        if len(step_ids)!=len(set(step_ids)):raise DomainError("DUPLICATE_STEP","단계 ID가 중복됩니다.")
        result["plan"]["steps"]=copy.deepcopy(changes["steps"])
        result["plan"]["revision"]=result["plan"].get("revision",0)+1
        result["plan"]["status"]="DRAFT"
    if geometry_hash(result)!=before:
        result["validation"]=None
        result["plan"]["status"]="NEEDS_VALIDATION"
        for step in result["plan"].get("steps",[]):
            step["status"]="UNKNOWN"
            if isinstance(step.get("path"),dict):step["path"]["verified"]=False
    result["revision"]+=1
    result["updated_at"]=now()
    result["status"]="DRAFT" if result["plan"].get("steps") else result["status"]
    return result


def confirmation_errors(project, release=None):
    problems=[]
    validation=project.get("validation") or {}
    if validation.get("status")!="PASS":problems.append("현행 순서의 조립성 검증이 통과되지 않았습니다.")
    if validation.get("dependency_hash")!=geometry_hash(project):problems.append("검증 후 모델·규칙·순서·경로가 변경되었습니다.")
    if release and (validation.get("build_id")!=release.get("build_id") or validation.get("algorithm_version")!=release.get("algorithm_version")):
        problems.append("현재 프로그램 버전으로 조립성 검증을 다시 실행해 주세요.")
    if not coverage(project)["complete"]:problems.append("계획의 조립 단위가 누락되거나 중복되었습니다.")
    for index,step in enumerate(project.get("plan",{}).get("steps",[]),1):
        if step.get("reviewed") is not True:problems.append(f"{index}단계 작업 정보 검토가 필요합니다.")
        if not str(step.get("caution") or "").strip() and step.get("caution_reviewed") is not True:
            problems.append(f"{index}단계 주의사항을 입력하거나 검토 결과 없음을 표시해 주세요.")
        torque=step.get("torque")
        value=torque.get("value") if isinstance(torque,dict) else torque
        if step.get("torque_status") not in ("required","not_applicable"):
            problems.append(f"{index}단계 체결 토크 또는 해당 없음을 지정해 주세요.")
        if step.get("torque_status")=="required" and (isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<=0 or (isinstance(torque,dict) and torque.get("unit","N·m") not in ("N·m","kgf·cm"))):
            problems.append(f"{index}단계 체결 토크를 입력해 주세요.")
    return problems
