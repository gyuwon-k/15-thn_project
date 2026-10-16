"""Isolated compute entry point used by development and packaged executable."""
from __future__ import annotations
import json
import math
import sys
import time
import traceback
from pathlib import Path
from .jobs import atomic_json, path_values
from .domain import now


def run_worker(request_path):
    request=json.loads(Path(request_path).read_text(encoding="utf-8"))
    root=Path(request["data_dir"]);folder=Path(request["folder"])
    payload=path_values(request["payload"],root)
    def cancel():return (folder/"cancel").exists()
    last_progress=[-float('inf')]
    def progress(*args,**kwargs):
        if cancel():raise InterruptedError("취소되었습니다.")
        tick=time.monotonic()
        # Progress is a display snapshot. Fsync for every cached candidate can
        # consume the search budget; cancellation remains checked every call.
        if tick-last_progress[0]<.25:return
        last_progress[0]=tick
        event=args[0] if args and isinstance(args[0],dict) else {}
        if event:
            kwargs={**event,**kwargs}
            if isinstance(event.get("progress"),(int,float)) and 0<=event["progress"]<=1:
                kwargs["progress"]=event["progress"]*100
        percent=next((float(a) for a in args if isinstance(a,(int,float)) and not isinstance(a,bool)),kwargs.get("progress"))
        if percent is not None and (not math.isfinite(percent) or not 0<=percent<=100):percent=None
        message=next((a for a in args if isinstance(a,str)),kwargs.get("message","처리 중"))
        try:
            atomic_json(folder/"progress.json",{"progress":percent,"message":message,"stage":kwargs.get("stage"),"processed":kwargs.get("current"),"total":kwargs.get("total"),"updated_at":now()})
        except PermissionError:
            # A transient progress display failure must not discard CAD work.
            print("PROGRESS_PUBLICATION_RETRY_EXHAUSTED",flush=True)
        if cancel():raise InterruptedError("취소되었습니다.")
    try:
        from .release import runtime_identity
        if runtime_identity()["build_id"]!=request["release"]["build_id"]:
            raise RuntimeError("WORKER_BUILD_MISMATCH: 작업자와 앱의 빌드가 달라 작업을 시작하지 않았습니다.")
        kind=request["kind"]
        if kind=="import":
            from .geometry import preprocess
            result=preprocess(payload["assembly_path"],payload.get("part_paths",[]),payload["cache_dir"],progress=progress,cancel=cancel)
            result["processing_versions"]={key:request["release"].get(key) for key in ("preprocess_version","matching_version","geometry_backend_version","analysis_geometry_version")}
        elif kind in ("generate","validate"):
            from .planning import generate_plan,validate_plan
            from .planning_settings import DEFAULT_ANALYSIS_SECONDS
            from .analysis_refresh import refresh_analysis_model, incomplete_refresh_plan
            project=payload["project"];model=project["model"];rules=dict(project["rules"])
            if kind=="generate" and payload.get("ranking_model"):rules["ranking_model"]=payload["ranking_model"]
            cache=payload["cache_dir"]
            stage_budget=float(rules.get('max_seconds',DEFAULT_ANALYSIS_SECONDS) if kind=='generate'
                else rules.get('validation_seconds',rules.get('max_seconds',DEFAULT_ANALYSIS_SECONDS)))
            stage_started=time.monotonic()
            analysis_model,refresh=refresh_analysis_model(model,cache,deadline=stage_started+stage_budget,
                native_timeout_seconds=rules.get('native_timeout_seconds',120),progress=progress,cancel=cancel)
            refresh_elapsed=time.monotonic()-stage_started
            remaining=max(0.,stage_budget-refresh_elapsed)
            if analysis_model is not None:
                model=analysis_model
                if request['release'].get('analysis_geometry_version'):
                    model.setdefault('processing_versions',{})['analysis_geometry_version']=request['release']['analysis_geometry_version']
            run_rules=dict(rules)
            run_rules['max_seconds' if kind=='generate' else 'validation_seconds']=remaining
            if kind=="generate":
                plan=generate_plan(model,run_rules,cache,progress=progress,cancel=cancel) if remaining>=1 else incomplete_refresh_plan(model,refresh)
                previous={s['unit_id']:s for s in project.get('plan',{}).get('steps',[])}
                for step in plan.get('steps',[]):
                    old=previous.get(step['unit_id'],{})
                    for key in ('name','torque','torque_status','caution','caution_reviewed','camera','duration_s'):
                        if key in old:step[key]=old[key]
                    step['reviewed']=False
                tick=time.monotonic()
                should_validate=plan.get('replace_plan',bool(plan.get('steps')))
                if should_validate:progress(stage='assembly_revalidation',message='조립 순서 재검증 중')
                validation=validate_plan(model,plan,rules,cache,progress=progress,cancel=cancel) if should_validate else {"status":"UNKNOWN","reason":"완전한 후보가 없습니다."}
                plan['timing']={'generation_seconds':plan.get('search',{}).get('elapsed_seconds',0),
                    'analysis_refresh_seconds':round(refresh_elapsed,3),'generation_search_budget_seconds':remaining,
                    'revalidation_seconds':round(time.monotonic()-tick,3),
                    'generation_budget_seconds':rules.get('max_seconds',DEFAULT_ANALYSIS_SECONDS),
                    'validation_budget_seconds':rules.get('validation_seconds',rules.get('max_seconds',DEFAULT_ANALYSIS_SECONDS))}
                plan['timing']['total_seconds']=round(plan['timing']['analysis_refresh_seconds']+plan['timing']['generation_seconds']+plan['timing']['revalidation_seconds'],3)
                result={"plan":plan,"validation":validation}
            else:
                validation=validate_plan(model,project["plan"],run_rules,cache,progress=progress,cancel=cancel) if remaining>=1 else {
                    'status':'UNKNOWN','reason':'ANALYSIS_REFRESH_BUDGET_REACHED',
                    'diagnostics':[{'code':'ANALYSIS_REFRESH_BUDGET_REACHED','message':'형상 재평가 후 재검증에 사용할 시간이 남지 않았습니다.'}]}
                validation['analysis_refresh_seconds']=round(refresh_elapsed,3)
                validation['validation_budget_seconds']=stage_budget
                validation['remaining_validation_budget_seconds']=remaining
                result={"validation":validation}
            if analysis_model is not None:result['analysis_model']=analysis_model
            if refresh.get('attempted_occurrence_ids') or refresh.get('budget_exhausted'):result['analysis_refresh']=refresh
        elif kind=="verify_matching":
            from .geometry import verify_unit_mappings
            result=verify_unit_mappings(payload["model"],payload["cache_dir"],progress=progress,cancel=cancel,unit_ids=payload.get("unit_ids"))
        elif kind=="process_suggestions":
            from .process_rules import recommend_process_rules
            progress(5,"체결품의 실제 원통 축과 고정 위치 검사")
            result=recommend_process_rules(payload["model"])
            result["dependency_hash"]=payload["process_profile_hash"]
        elif kind=="export":
            from .exporter import render_video
            result=render_video(payload,folder,progress,cancel,data_dir=root,build_id=request["release"]["build_id"])
        elif kind=="train":
            from .learning import train_ranker
            result=train_ranker(payload["histories"],payload["settings"],payload["output_dir"],progress,cancel)
        else:raise ValueError("알 수 없는 작업: "+kind)
        atomic_json(folder/"result.json",{"result":result})
    except Exception as exc:
        traceback.print_exc()
        atomic_json(folder/"result.json",{"error":str(exc),"diagnostics":[{"code":getattr(exc,"code",type(exc).__name__),"message":str(exc)}]})
        return 1
    return 0


if __name__=="__main__":sys.exit(run_worker(sys.argv[1]))
