"""Real HTTP positive workflow using a clearly labelled synthetic STEP fixture."""
from pathlib import Path
import argparse, copy, json, os, time
import httpx
from PIL import Image, ImageDraw


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--url",default="http://127.0.0.1:8772")
    parser.add_argument("--token",default=os.environ.get("PILOT_SESSION_TOKEN","pilot-v2-local-integration"))
    parser.add_argument("--report",required=True);args=parser.parse_args()
    output=Path(args.report).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    client=httpx.Client(base_url=args.url,headers={"X-Pilot-Token":args.token},timeout=180)
    report={"fixture":"synthetic_three_solids_not_company_validation","checks":{}}
    def save():output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    def request(method,url,**kwargs):
        response=client.request(method,url,**kwargs)
        if response.status_code>=400:raise RuntimeError(f"{response.status_code}: {response.text[:2000]}")
        return response.json()
    def wait(job):
        last=None;deadline=time.monotonic()+900
        while time.monotonic()<deadline:
            current=request("GET","/api/jobs/"+job["id"])
            message=(current["status"],current.get("message"))
            if message!=last:print(json.dumps({"job":job["kind"],"status":message[0],"message":message[1]},ensure_ascii=False),flush=True);last=message
            if current["status"]=="succeeded":return current
            if current["status"] in ("failed","canceled","interrupted"):raise RuntimeError(current)
            time.sleep(.5)
        raise TimeoutError(job)
    try:
        from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
        from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
        from OCP.gp import gp_Pnt
        step=output.parent/"synthetic-three-solids.step";writer=STEPControl_Writer()
        for point,size in [((0,0,0),(120,80,5)),((10,10,5),(30,20,5)),((60,10,5),(20,20,8))]:
            writer.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(*point),*size).Shape(),STEPControl_AsIs)
        writer.Write(str(step))
        report["release"]=request("GET","/api/health")
        project=request("POST","/api/projects",json={"name":"기능시험 · 합성 3부품 · 실제 제품 아님","part_number":"000-TEST"})
        assert project['rules']['max_seconds']==project['rules']['validation_seconds']==1800
        assert project['rules']['candidate_seconds']==project['rules']['native_timeout_seconds']==120
        report['checks']['new_project_30_minute_generation_and_validation_defaults']=True
        ident=project["id"];report["project_id"]=ident;prefix="/api/projects/"+ident
        with step.open("rb") as stream:wait(request("POST",prefix+"/import",files={"assembly":(step.name,stream)},data={"expected_revision":project["revision"]}))
        project=request("GET",prefix);assert len(project["model"]["occurrences"])==3
        report["checks"]["step_import_three_occurrences"]=True
        wait(request("POST",prefix+"/process-suggestions",json={"expected_revision":project["revision"]}))
        proposal=request("GET",prefix+"/process-suggestions")
        assert proposal["profile"]=="generic_geometry_candidates" and proposal["dependency_hash"]
        unchanged=request("GET",prefix);assert unchanged["revision"]==project["revision"]
        original_fixed=project["rules"]["fixed_unit_ids"]
        modified=request("PATCH",prefix,json={"expected_revision":project["revision"],"rules":{"fixed_unit_ids":[project["model"]["units"][0]["id"]]}})
        assert client.get(prefix+"/process-suggestions").status_code==422
        project=request("PATCH",prefix,json={"expected_revision":modified["revision"],"rules":{"fixed_unit_ids":original_fixed}})
        report["checks"]["readonly_process_proposal_invalidated_by_fixed_unit_change"]=True
        project=request("PATCH",prefix,json={"expected_revision":project["revision"],"rules":{"max_seconds":30}})
        wait(request("POST",prefix+"/generate",json={"expected_revision":project["revision"],"request_id":"positive-generate"}))
        project=request("GET",prefix);assert project["validation"]["status"]=="PASS",project["validation"]
        report["checks"]["generated_complete_PASS"]=project["coverage"]["complete"]
        blank_steps=copy.deepcopy(project["plan"]["steps"])
        for entry in blank_steps:
            entry.update(reviewed=False,torque=None,torque_status="required",caution="",caution_reviewed=False)
        project=request("PATCH",prefix,json={"expected_revision":project["revision"],"plan":{"steps":blank_steps}})
        assert project["validation"]["status"]=="PASS"
        blank_confirmation=request("POST",prefix+"/confirm",json={"expected_revision":project["revision"]})
        report["blank_work_information_snapshot_id"]=blank_confirmation["id"]
        report["checks"]["confirmation_without_torque_caution_or_review_check"]=True
        project=request("GET",prefix);assert project["plan"]["status"]=="CONFIRMED"
        steps=copy.deepcopy(project["plan"]["steps"])
        for index,entry in enumerate(steps):
            entry.update(reviewed=True,duration_s=.5,name=f"시험 조립 {index+1}",torque={"value":5,"unit":"N·m"},torque_status="required",caution="시험용 주의사항입니다. 작업 대상과 진입 방향을 확인해 주세요.")
        project=request("PATCH",prefix,json={"expected_revision":project["revision"],"plan":{"steps":steps}})
        confirmed=request("POST",prefix+"/confirm",json={"expected_revision":project["revision"],"reviewer":"자동화 기능시험","reason":"합성 STEP으로 API·화면·MP4 흐름을 확인하는 시험이며 실제 회사 승인이 아님"})
        report["snapshot_id"]=confirmed["id"]
        current=request("GET",prefix);assert current["plan"]["status"]=="CONFIRMED"
        report["checks"]["confirmation_numeric_torque_object"]=True
        history=request("GET",prefix+"/history");assert history["learning"]["eligible_count"]==0
        report["checks"]["unreviewed_confirmation_excluded_from_learning"]=True
        template=output.parent/"synthetic-template.png";image=Image.new("RGB",(1280,720),"#f3f6fa");ImageDraw.Draw(image).rectangle((0,0,1279,719),outline="#0c8275",width=8);image.save(template)
        with template.open("rb") as stream:
            template_record=request("POST","/api/templates",files={"file":(template.name,stream,"image/png")},data={"name":"기능시험 전용 양식 · 회사 승인 양식 아님","approved":"true","layout":"custom"})
        job=wait(request("POST",prefix+"/exports",json={"expected_revision":current["revision"],"snapshot_id":confirmed["id"],"template_id":template_record["id"],"draft":False,"width":640,"height":360,"fps":12}))
        exported=request("GET",prefix+"/exports")[-1]
        assert exported["decoded_frames"]==exported["frames"]==len(steps)*6
        video=client.get(exported["url"]);video.raise_for_status();video_path=output.parent/"synthetic-verified-video.mp4";video_path.write_bytes(video.content)
        report["checks"]["formal_mp4_full_decode"]=True;report["export"]=exported;report["video_path"]=str(video_path)
        current=request("GET",prefix);assert current["plan"]["status"]=="CONFIRMED"
        report["checks"]["video_export_preserves_current_confirmation"]=True
        steps=copy.deepcopy(current["plan"]["steps"]);steps[-1]["distance_mm"]+=1
        edited=request("PATCH",prefix,json={"expected_revision":current["revision"],"plan":{"steps":steps}})
        rejected=client.post(prefix+"/confirm",json={"expected_revision":edited["revision"]})
        assert rejected.status_code==422 and edited["validation"] is None
        report["checks"]["edited_path_requires_revalidation"]=True
        wait(request("POST",prefix+"/validate",json={"expected_revision":edited["revision"]}))
        current=request("GET",prefix);assert current["validation"]["status"]=="PASS"
        report["checks"]["edited_plan_revalidation_PASS"]=True
        previous_sources=copy.deepcopy(current.get("source_files"));previous_model=current["model"]["fingerprint"]
        failed=request("POST",prefix+"/import",files={"assembly":("invalid.step",b"not a STEP file")},data={"expected_revision":current["revision"]})
        while True:
            failed=request("GET","/api/jobs/"+failed["id"])
            if failed["status"] not in ("queued","running","cancel_requested"):break
            time.sleep(.5)
        assert failed["status"]=="failed"
        preserved=request("GET",prefix)
        assert preserved.get("source_files")==previous_sources and preserved["model"]["fingerprint"]==previous_model
        assert preserved["validation"]["status"]=="PASS"
        report["checks"]["failed_import_preserves_matching_original_and_model"]=True
        report["status"]="PASS";save();print(json.dumps({"status":"PASS","report":str(output),"checks":report["checks"]},ensure_ascii=False))
    except Exception as exc:
        report["status"]="FAILED";report["error"]=str(exc);save();raise


if __name__=="__main__":main()
