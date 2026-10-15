"""Auditable pairwise rank learning; confirmed paths never become safety labels."""
from __future__ import annotations
import math
from pathlib import Path
import numpy as np
from .domain import digest, now, uid
from .jobs import atomic_json

FEATURES=["log_size_x","log_size_y","log_size_z","height","fastener","installed_fraction"]


def decision_id(decision):
    return "decision-"+digest({"installed_ids":sorted(decision.get("installed_ids",[])),
                               "feasible_unit_ids":sorted(decision.get("feasible_unit_ids",[])),
                               "candidates":decision.get("candidates",[])})[:24]


def reviewed_histories(histories, reviews):
    """Overlay explicit preference labels without changing confirmed snapshots."""
    import copy
    result=[]
    for history in histories:
        value=copy.deepcopy(history)
        matching=[r for r in reviews if r.get("snapshot_id")==history.get("id") and r.get("source_hash")==history.get("source_hash")]
        if matching:
            review=matching[-1];choices={s["decision_id"]:s["selected_unit_id"] for s in review["selections"]}
            value["expert_preference_reviewed"]=True;value["product_family"]=review["product_family"]
            for decision in value["project"]["plan"].get("decision_trace",[]):
                decision["selected_unit_id"]=choices.get(decision_id(decision))
            value["preference_review_id"]=review["id"]
        result.append(value)
    return result


def preference_review(snapshot, body):
    from .domain import DomainError
    if snapshot.get("status")!="CONFIRMED" or snapshot.get("project",{}).get("validation",{}).get("status")!="PASS":
        raise DomainError("LEARNING_REVIEW_NOT_ELIGIBLE","검증 통과 후 확정한 이력만 선호 검토 대상으로 사용할 수 있습니다.")
    for key in ("reviewer","reference","product_family"):
        if not str(body.get(key,"")).strip():raise DomainError("LEARNING_REVIEW_DETAILS","검토자·검토 근거·제품군을 입력해 주세요.")
    decisions={decision_id(d):d for d in snapshot["project"]["plan"].get("decision_trace",[])}
    selections=body.get("selections") or []
    if not selections or len({s.get("decision_id") for s in selections})!=len(selections):
        raise DomainError("LEARNING_CHOICES_REQUIRED","중복되지 않은 후보 비교 결과가 하나 이상 필요합니다.")
    for choice in selections:
        decision=decisions.get(choice.get("decision_id")) or {}
        feasible=set(decision.get("feasible_unit_ids",[]))
        if len(feasible)<2 or choice.get("selected_unit_id") not in feasible:
            raise DomainError("LEARNING_UNVERIFIED_CHOICE","같은 설치 상태에서 경로 검증을 통과한 후보 두 개 이상만 비교할 수 있습니다.")
    return {"id":uid("preference-"),"snapshot_id":snapshot["id"],"source_hash":snapshot["source_hash"],
            "reviewer":str(body["reviewer"]).strip(),"reference":str(body["reference"]).strip(),
            "product_family":str(body["product_family"]).strip(),"selections":selections,"created_at":now()}


def features(unit, installed_fraction):
    box=unit.get("bbox") or {}
    size=box.get("size") or [b-a for a,b in zip(box.get("min",[]),box.get("max",[]))]
    center=box.get("center") or [(b+a)/2 for a,b in zip(box.get("min",[]),box.get("max",[]))]
    if len(size)!=3 or len(center)!=3:return None
    values=[*[math.log1p(max(0,float(x))) for x in size],float(center[2])/1000,
            float(unit.get("role") in ("fastener","bolt","nut")),float(installed_fraction)]
    return values if all(math.isfinite(x) for x in values) else None


def build_dataset(histories):
    rows=[];excluded=[];seen=set();eligible=[]
    for history in histories:
        state=history.get("project") or history.get("snapshot") or history
        key=history.get("source_hash") or digest(state)
        reason=None
        if key in seen:reason="duplicate_confirmation"
        elif history.get("status")!="CONFIRMED":reason="not_confirmed"
        elif state.get("validation",{}).get("status")!="PASS":reason="not_validated"
        elif not history.get("expert_preference_reviewed"):reason="expert_preference_not_reviewed"
        trace=state.get("plan",{}).get("decision_trace") or []
        if not reason and not trace:reason="feasible_candidates_not_recorded"
        if reason:
            excluded.append({"id":history.get("id"),"reason":reason});continue
        seen.add(key)
        group=history.get("product_family") or state.get("part_number") or state.get("input_revision")
        units={u["id"]:u for u in state["model"]["units"]}
        case_rows=[]
        for decision in trace:
            selected=decision.get("selected_unit_id")
            alternatives=decision.get("feasible_unit_ids",[])
            if selected not in alternatives or selected not in units:continue
            fraction=len(decision.get("installed_ids",[]))/max(1,len(units))
            a=features(units[selected],fraction)
            if a is None:continue
            for alternative in alternatives:
                if alternative==selected or alternative not in units:continue
                b=features(units[alternative],fraction)
                if b is None:continue
                # Interaction terms retain state information, which would be
                # canceled by naive feature subtraction alone.
                difference=np.asarray(a[:-1])-np.asarray(b[:-1])
                pair=np.concatenate([difference,difference*fraction]).tolist()
                case_rows.extend([{"x":pair,"y":1,"group":group,"confirmation_id":history["id"]},
                                  {"x":[-x for x in pair],"y":0,"group":group,"confirmation_id":history["id"]}])
        if case_rows:rows.extend(case_rows);eligible.append(history["id"])
        else:excluded.append({"id":history.get("id"),"reason":"no_comparable_alternatives"})
    return {"rows":rows,"eligible_confirmations":eligible,"excluded":excluded}


def train_ranker(histories, settings, output_dir, progress=None, cancel=None):
    progress=progress or (lambda *args:None);cancel=cancel or (lambda:False)
    data=build_dataset(histories);threshold=settings.get("minimum_cases")
    record={"id":uid("ranker-"),"created_at":now(),"feature_version":"pairwise-2.0.0","features":FEATURES,
            "dataset_hash":digest(data),"eligible_count":len(data["eligible_confirmations"]),
            "pair_count":len(data["rows"]),"excluded":data["excluded"],"active":False,
            "supported_part_numbers":sorted({str(h.get("project",{}).get("part_number")) for h in histories if h.get("id") in data["eligible_confirmations"] and h.get("project",{}).get("part_number")})}
    if threshold is None:
        return {**record,"status":"WAITING","message":"재학습 기준 건수가 미설정되어 이력만 수집합니다."}
    if not isinstance(threshold,int) or threshold<1:raise ValueError("재학습 기준은 양의 정수여야 합니다.")
    if len(data["eligible_confirmations"])<threshold:
        return {**record,"status":"WAITING","message":"검토된 학습 적격 확정 이력이 부족합니다."}
    groups=sorted({str(row["group"]) for row in data["rows"]})
    if len(groups)<3:
        return {**record,"status":"WAITING","message":"제품군을 분리해 평가할 수 있도록 최소 3개 제품군 자료가 필요합니다."}
    if cancel():raise InterruptedError()
    progress(20,"제품군 분리와 학습 데이터 준비")
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    heldout=set(groups[::3]);training=[r for r in data["rows"] if str(r["group"]) not in heldout]
    evaluation=[r for r in data["rows"] if str(r["group"]) in heldout]
    scaler=StandardScaler().fit([r["x"] for r in training])
    learner=LogisticRegression(C=.25,max_iter=1000,random_state=17).fit(scaler.transform([r["x"] for r in training]),[r["y"] for r in training])
    predicted=learner.predict(scaler.transform([r["x"] for r in evaluation]))
    accuracy=float(np.mean(predicted==[r["y"] for r in evaluation]))
    record.update(status="SHADOW_EVALUATION",message="후보 모델 학습 완료 · 회사 평가와 필드테스트 후 적용 가능",
                  coefficients=learner.coef_[0].tolist(),intercept=float(learner.intercept_[0]),
                  mean=scaler.mean_.tolist(),scale=scaler.scale_.tolist(),heldout_groups=sorted(heldout),
                  training_groups=sorted(set(groups)-heldout),pairwise_accuracy=accuracy,
                  operational_evaluation="PENDING",company_evaluation="PENDING",field_test="PENDING")
    output=Path(output_dir);output.mkdir(parents=True,exist_ok=True)
    dataset_path=output/(record["id"]+"-dataset.json");model_path=output/(record["id"]+".json")
    atomic_json(dataset_path,data)
    atomic_json(model_path,record)
    record.update(dataset_path=str(dataset_path),model_path=str(model_path))
    progress(100,"후보 모델 저장 완료 · 운영 모델 유지")
    return record
