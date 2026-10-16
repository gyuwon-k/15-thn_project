"""Real supplied STEP through public API; persist success and blockers separately."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
import time
from pathlib import Path
import httpx
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("--source",type=Path,required=True)
    parser.add_argument("--report",type=Path,required=True)
    parser.add_argument("--url",default="http://127.0.0.1:8772")
    parser.add_argument("--project-id")
    parser.add_argument("--max-seconds",type=int,default=120)
    parser.add_argument("--require-pass",action="store_true")
    parser.add_argument("--user-process",action="store_true")
    options=parser.parse_args()
    client=httpx.Client(base_url=options.url,headers={"X-Pilot-Token":os.environ["PILOT_SESSION_TOKEN"]},timeout=180)
    report={"started_at":time.time(),"health":client.get("/api/health").raise_for_status().json(),"checks":[]}
    def save():
        options.report.parent.mkdir(parents=True,exist_ok=True)
        options.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    def get(path):return client.get(path).raise_for_status().json()
    def post(path,body):
        response=client.post(path,json=body)
        if response.is_error:raise RuntimeError(response.text)
        return response.json()
    def wait(job):
        last=None;deadline=time.monotonic()+4000
        while time.monotonic()<deadline:
            value=get("/api/jobs/"+job["id"])
            state=(value["status"],value.get("message"))
            if state!=last:print(json.dumps({"job":job["kind"],"status":state[0],"message":state[1]},ensure_ascii=False),flush=True);last=state
            if value["status"] in ("succeeded","failed","canceled","interrupted"):
                report.setdefault("jobs",[]).append(value);save()
                if value["status"]!="succeeded":raise RuntimeError(value.get("message"))
                return value
            time.sleep(1)
        raise TimeoutError("workflow deadline")
    try:
        if options.project_id:project=get("/api/projects/"+options.project_id)
        else:
            project=post("/api/projects",{"name":"HVJB 실제 파일 검증", "vehicle":"KD1","item":"고전압 정션박스","part_number":"91958-KD000"})
            assembly=options.source/"KD1_HVJB_91958-KD000.stp"
            parts=sorted((options.source/"parts").glob("*.STP"))
            report["inputs"]=[{"name":p.name,"bytes":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in [assembly,*parts]]
            handles=[]
            try:
                files=[]
                for key,path in [("assembly",assembly),*[("parts",p) for p in parts]]:
                    stream=path.open("rb");handles.append(stream);files.append((key,(path.name,stream,"application/octet-stream")))
                response=client.post("/api/projects/"+project["id"]+"/import",data={"expected_revision":project["revision"]},files=files)
                if response.is_error:raise RuntimeError(response.text)
                wait(response.json())
            finally:
                for stream in handles:stream.close()
        report["project_id"]=project["id"];save()
        project=get("/api/projects/"+project["id"])
        model=project["model"]
        report["model"]={"occurrences":len(model["occurrences"]),"units":len(model["units"]),"quality":model["quality"],"diagnostics":model["diagnostics"],"matching_audit":model.get("matching_audit"),"processing_versions":model.get("processing_versions")}
        members=[x for u in model["units"] for x in u["occurrence_ids"]]
        assert len(members)==len(set(members))==len(model["occurrences"])==78
        report["checks"].append("78 physical occurrences partitioned exactly once")
        scene=client.get("/api/projects/"+project["id"]+"/scene").raise_for_status().json()
        assert len(scene["meshes"])==78
        assert all(m["positions"] and m["indices"] for m in scene["meshes"])
        report["checks"].append("All 78 occurrence meshes returned by actual API")
        rules={"max_seconds":options.max_seconds}
        if options.user_process:
            wait(post("/api/projects/"+project["id"]+"/process-suggestions",{"expected_revision":project["revision"]}))
            profile=get("/api/projects/"+project["id"]+"/process-suggestions")
            assert profile["profile"]=="provided_hvjb_user_intent"
            rules.update({key:profile[key] for key in ("process_groups","process_policy","precedence")})
            report["process_profile"]=profile
            report["checks"].append("User housing→plate→relay order and fixing groups loaded; uncertain assignments remain review")
        response=client.patch("/api/projects/"+project["id"],json={"expected_revision":project["revision"],"rules":rules})
        if response.is_error:raise RuntimeError(response.text)
        project=response.json()
        wait(post("/api/projects/"+project["id"]+"/generate",{"expected_revision":project["revision"]}))
        project=get("/api/projects/"+project["id"])
        report["plan"]=project["plan"];report["validation"]=project["validation"];report["coverage"]=project["coverage"]
        report["sequence_generated"]=bool(project["plan"]["steps"]) and project["coverage"]["complete"]
        report["verified_recommendation"]=project["validation"]["status"]=="PASS"
        assert report["sequence_generated"],"No full-coverage sequence generated"
        assert len({s["unit_id"] for s in project["plan"]["steps"]})==len(project["plan"]["steps"])
        report["checks"].append("Every moving assembly unit appears exactly once")
        from backend.domain import executable_step,playback_summary
        playback=playback_summary(project["plan"]["steps"])
        assert project["plan"]["playback"]["executable_prefix_count"]==playback["executable_prefix_count"]
        for index,step in enumerate(project["plan"]["steps"]):
            if index<playback["executable_prefix_count"]:assert executable_step(step)
            else:
                assert step.get("executable") is not True
                assert step.get("path") is None
        report["checks"].append("Only contiguous verified prefix has executable paths; remaining steps have no animation path")
        report["safe_playback"]=playback
        if not report["verified_recommendation"]:
            rejected=client.post("/api/projects/"+project["id"]+"/confirm",json={"expected_revision":project["revision"]})
            assert rejected.status_code==422
            report["checks"].append("Unverified physical plan cannot be confirmed")
        report["finished_at"]=time.time();save()
        print(json.dumps({"report":str(options.report),"project_id":project["id"],"sequence_generated":report["sequence_generated"],"verified_recommendation":report["verified_recommendation"],"steps":len(project["plan"]["steps"])},ensure_ascii=False),flush=True)
        return 2 if options.require_pass and not report["verified_recommendation"] else 0
    except Exception as exc:
        report["error"]=str(exc);report["finished_at"]=time.time();save();raise


if __name__=="__main__":raise SystemExit(main())
