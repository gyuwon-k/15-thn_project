"""Isolated compute entry point used by development and packaged executable."""
from __future__ import annotations
import json
import math
import sys
import traceback
from pathlib import Path
from .jobs import atomic_json, path_values
from .domain import now


def run_worker(request_path):
    request=json.loads(Path(request_path).read_text(encoding="utf-8"))
    root=Path(request["data_dir"]);folder=Path(request["folder"])
    payload=path_values(request["payload"],root)
    def cancel():return (folder/"cancel").exists()
    def progress(*args,**kwargs):
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
        elif kind in ("generate","validate"):
            from .planning import generate_plan,validate_plan
            project=payload["project"];model=project["model"];rules=dict(project["rules"])
            if kind=="generate" and payload.get("ranking_model"):rules["ranking_model"]=payload["ranking_model"]
            cache=payload["cache_dir"]
            if kind=="generate":
                plan=generate_plan(model,rules,cache,progress=progress,cancel=cancel)
                validation=validate_plan(model,plan,rules,cache,progress=progress,cancel=cancel) if plan.get("steps") else {"status":"UNKNOWN","reason":"완전한 후보가 없습니다."}
                result={"plan":plan,"validation":validation}
            else:result={"validation":validate_plan(model,project["plan"],rules,cache,progress=progress,cancel=cancel)}
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
