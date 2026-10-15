"""Reproducible real-input test; every assertion tests evidence, never forces PASS."""
import argparse
import json
import sys
import time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.geometry import preprocess
from backend.planning import generate_plan,validate_plan


def verify_real_subset(model,cache):
    units=model["units"]
    fixed=next(u for u in units if u["name"]=="AIR-BENT0A")
    moving=next(u for u in units if u["name"]=="BUS-KD000")
    rules={"selected_scope":[fixed["id"],moving["id"]],"fixed_unit_ids":[fixed["id"]],"initial_obstacle_ids":[],"environment_confirmed":True,
        "directions":["-Z"],"distance_mm":550,"max_seconds":30,"validation_seconds":30}
    plan=generate_plan(model,rules,cache);validation=validate_plan(model,plan,rules,cache)
    evidence={"test":"actual STEP restricted two-part collision fixture","source_sha256":model["source"]["sha256"],"fixture_conditions":"Only the named two original CAD units are present. Housing and remaining units are absent for this isolated software verification, not approved for the full product.",
        "fixed_name":fixed["name"],"moving_name":moving["name"],"plan":plan,"validation":validation,"whole_product_verified":False}
    (cache/"hvjb-restricted-real-cad-fixture.json").write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding="utf-8")
    assert plan["status"]=="PASS" and validation["status"]=="PASS",evidence
    return {"plan_status":plan["status"],"validation_status":validation["status"],"moving_name":moving["name"],"fixed_name":fixed["name"],"whole_product_verified":False}


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--source",type=Path);parser.add_argument("--cache",type=Path,default=Path(__file__).resolve().parents[1]/".test-cache")
    parser.add_argument("--seconds",type=float,default=120);parser.add_argument("--validation-seconds",type=float);parser.add_argument("--native-timeout",type=float,default=4);parser.add_argument("--candidate-seconds",type=float,default=4);parser.add_argument("--iteration",default="2");parser.add_argument("--subset-only",action="store_true");args=parser.parse_args()
    source=args.source or Path(__file__).resolve().parents[2]/"프로젝트_제공_파일"/"3D DATA"/"STP"
    args.cache.mkdir(parents=True,exist_ok=True)
    if args.subset_only:
        model=json.loads((args.cache/"hvjb-model.json").read_text(encoding="utf-8"))
        print(json.dumps(verify_real_subset(model,args.cache),ensure_ascii=False,indent=2));return
    def progress(event):
        if event["stage"] in ("cad_read","part_import") or event.get("current",0)%10==0:print(json.dumps(event,ensure_ascii=False),flush=True)
    started=time.monotonic();model=preprocess(source/"KD1_HVJB_91958-KD000.stp",sorted((source/"parts").glob("*.STP")),args.cache,progress)
    (args.cache/"hvjb-model.json").write_text(json.dumps(model,ensure_ascii=False,indent=2),encoding="utf-8")
    rules={"max_seconds":args.seconds,"validation_seconds":args.validation_seconds or args.seconds,"native_timeout_seconds":args.native_timeout,"candidate_seconds":args.candidate_seconds,"beam_width":3}
    plan=generate_plan(model,rules,args.cache,progress)
    (args.cache/f"hvjb-plan-iteration-{args.iteration}.json").write_text(json.dumps(plan,ensure_ascii=False,indent=2),encoding="utf-8")
    validation=validate_plan(model,plan,rules,args.cache,progress)
    (args.cache/f"hvjb-validation-iteration-{args.iteration}.json").write_text(json.dumps(validation,ensure_ascii=False,indent=2),encoding="utf-8")
    summary={"source":source.joinpath("KD1_HVJB_91958-KD000.stp").name,"source_sha256":model["source"]["sha256"],"iteration":args.iteration,"actual_step_import":True,
        "preprocess_version":model["algorithm_version"],"planner_version":plan["algorithm_version"],"source_occurrences":len(model["occurrences"]),"source_solids":model["metrics"]["solids"],"part_files":len(model["part_sources"]),
        "assembly_units":len(model["units"]),"compound_units":[{"name":u["name"],"members":len(u["occurrence_ids"])} for u in model["units"] if len(u["occurrence_ids"])>1],
        "quality":model["quality"],"generated_steps":len(plan["steps"]),"plan_status":plan["status"],"plan_coverage":plan["coverage"],"verified_steps":sum(s["status"]=="PASS" for s in plan["steps"]),
        "validation_status":validation["status"],"final_collisions":[p for p in validation.get("placement_audit",{}).get("pairs",[]) if p["status"]=="COLLISION"],"search":plan["search"],"elapsed_seconds":time.monotonic()-started,
        "diagnostics":plan["diagnostics"]}
    (args.cache/f"hvjb-summary-iteration-{args.iteration}.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8")
    assert model["quality"]["inventory_complete"] and plan["coverage"]["complete"],"Physical source coverage was lost"
    assert len(model["occurrences"])==78 and model["metrics"]["solids"]==176,"Supplied product inventory changed"
    assert not (plan["status"]=="PASS" and any(s["status"]!="PASS" for s in plan["steps"])),"Unsafe PASS"
    print(json.dumps({k:v for k,v in summary.items() if k not in ("quality","final_collisions")},ensure_ascii=False,indent=2),flush=True)


if __name__=="__main__":main()
