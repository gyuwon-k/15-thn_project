"""Recheck every supplied part/member against actual assembly BREP geometry."""
from __future__ import annotations
import argparse
import json
import multiprocessing as mp
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.geometry import match_units
from backend.geometry_matching import ShapeVerifier, VERSION, POLICY


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--model", required=True)
    parser.add_argument("--output", required=True); parser.add_argument("--timeout", type=float, default=90)
    args=parser.parse_args(); started=time.monotonic()
    model=json.loads(Path(args.model).read_text(encoding="utf-8")); model=model.get("result",model)
    cache=Path(model["transfer_report"]).parent.parent
    templates=[]
    for source in model["part_sources"]:
        manifest=next(cache.glob(source["sha256"][:20]+"*/manifest.json"))
        template=json.loads(manifest.read_text(encoding="utf-8"))
        for row in template["instances"]:
            row["brep_path"]=str(manifest.parent/(row["id"]+".brep"))
        templates.append(template)
    output=Path(args.output); output.parent.mkdir(parents=True,exist_ok=True)
    verifier=ShapeVerifier(output.parent/"matching-cache",progress=lambda event:print(event["message"],flush=True),timeout_seconds=args.timeout)
    units,diagnostics=match_units({"instances":model["occurrences"]},templates,shape_verifier=verifier)
    members=[m for unit in units for m in (unit["matching"].get("geometry_verification") or {}).get("members",[])]
    result={"source_sha256":model["source"]["sha256"],"matching_version":VERSION,"policy":POLICY,
        "scope":"Every proposed occurrence/member uses actual solid BREP differences; no image/name/feature-only match is certified.",
        "template_count":len(templates),"occurrence_count":len(model["occurrences"]),"unit_count":len(units),
        "matched_member_count":len(members),"member_results":dict(Counter(row["status"] for row in members)),
        "unit_results":dict(Counter((unit["matching"].get("geometry_verification") or {}).get("status","UNVERIFIED") for unit in units)),
        "exact_unique_coverage":len({i for u in units for i in u["occurrence_ids"]})==sum(len(u["occurrence_ids"]) for u in units)==len(model["occurrences"]),
        "elapsed_seconds":round(time.monotonic()-started,3),"units":units,"diagnostics":diagnostics}
    output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k not in ("units","diagnostics")},ensure_ascii=False),flush=True)


if __name__=="__main__":
    mp.freeze_support();main()
