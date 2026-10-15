"""API backup/restore plus actual restored BREP/scene and plan verification."""
import argparse, hashlib, json, os, sys
from pathlib import Path
import httpx
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from backend.storage import Storage
from backend.jobs import path_values
from backend.planning import validate_plan


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--url",default="http://127.0.0.1:8772")
    parser.add_argument("--report",required=True);parser.add_argument("--resources",required=True)
    parser.add_argument("--synthetic-project",required=True);parser.add_argument("--hvjb-project",required=True)
    args=parser.parse_args();output=Path(args.report).resolve();output.parent.mkdir(parents=True,exist_ok=True)
    client=httpx.Client(base_url=args.url,headers={"X-Pilot-Token":os.environ.get("PILOT_SESSION_TOKEN","pilot-v2-local-integration")},timeout=600)
    report={"checks":{}}
    def call(method,url,**kwargs):
        response=client.request(method,url,**kwargs);response.raise_for_status();return response.json()
    original_count=len(call("GET","/api/projects"))
    backup=call("POST","/api/backups");report["backup"]={k:v for k,v in backup.items() if k!="path"}
    archive=output.parent/"verified-workflow-backup.zip"
    with client.stream("GET","/api/backups/"+backup["id"]) as response:
        response.raise_for_status()
        with archive.open("wb") as stream:
            for chunk in response.iter_bytes(1024*1024):stream.write(chunk)
    assert hashlib.file_digest(archive.open("rb"),"sha256").hexdigest()==backup["sha256"]
    report["checks"]["download_matches_backup_hash"]=True;print("Backup downloaded and hash verified",flush=True)
    with archive.open("rb") as stream:restored=call("POST","/api/restores/upload",files={"file":(archive.name,stream,"application/zip")})
    assert restored["status"]=="verified" and restored["activated"] is False
    report["restore"]=restored;print("Restored to verified new storage",flush=True)
    store=Storage(restored["data_dir"],args.resources);store.init()
    try:
        assert len(store.list_projects())==original_count
        hvjb=store.get_project(args.hvjb_project);model=path_values(hvjb["model"],Path(restored["data_dir"]))
        scene=json.loads(Path(model["scene_path"]).read_text(encoding="utf-8"))
        assert len(scene["meshes"])==78
        assert all(Path(o["brep_path"]).is_file() for o in model["occurrences"])
        report["checks"]["hvjb_78_shapes_scene_paths_portable"]=True
        synthetic=path_values(store.get_project(args.synthetic_project),Path(restored["data_dir"]))
        result=validate_plan(synthetic["model"],synthetic["plan"],synthetic["rules"],Path(restored["data_dir"])/"recovery-analysis")
        assert result["status"]=="PASS",result
        report["checks"]["restored_BREP_plan_revalidated_PASS"]=True
        snapshots=store.snapshots(args.synthetic_project);assert snapshots
        videos=store.get_project(args.synthetic_project).get("exports",[]);assert videos
        for video in videos:assert store.file_path(video["file_id"],verify=True).is_file()
        report["checks"]["snapshot_video_integrity"]=True
    finally:store.close()
    report["status"]="PASS";output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({"status":"PASS","report":str(output),"checks":report["checks"]}))


if __name__=="__main__":main()
