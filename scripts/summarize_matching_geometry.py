"""Inspect saved per-member geometric certificates without rerunning CAD work."""
from __future__ import annotations
import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
import numpy as np


def summarize(model):
    occurrences={row["id"]:row for row in model["occurrences"]};units=[]
    for unit in model["units"]:
        matching=unit.get("matching") or {};proof=matching.get("geometry_verification") or {}
        members=[]
        for row in proof.get("members",[]):
            target=occurrences[row["occurrence_id"]]
            path=Path(target["brep_path"]);current=hashlib.sha256(path.read_bytes()).hexdigest()
            matrix=np.array(row.get("source_to_target_world") or np.eye(4).reshape(-1)).reshape(4,4)
            determinant=float(np.linalg.det(matrix[:3,:3]));error=float(np.max(abs(matrix[:3,:3].T@matrix[:3,:3]-np.eye(3))))
            solid_pairs=row.get("comparison",{}).get("verified_solid_pairs",[])
            solid_indices=list(range(target["solid_count"]))
            full_solid_certificate=(row.get("solid_counts")==[target["solid_count"],target["solid_count"]]
                and sorted(pair["source_solid_index"] for pair in solid_pairs)==solid_indices
                and sorted(pair["target_solid_index"] for pair in solid_pairs)==solid_indices
                and all(pair.get(side,{}).get("remaining_faces")==0 and pair.get(side,{}).get("empty_material_boundary") is True
                    for pair in solid_pairs for side in ("source_minus_target","target_minus_source")))
            members.append({"occurrence_id":row["occurrence_id"],"name":target["name"],"template_occurrence_id":row["template_occurrence_id"],
                "status":row["status"],"reason":row.get("reason"),"solid_count":target["solid_count"],
                "verified_solid_pairs":len(solid_pairs),"complete_solid_bijection_certificate":full_solid_certificate,
                "target_content_hash_current":current==row.get("target_sha256"),
                "proper_rigid_transform":row["status"]=="PASS" and abs(determinant-1)<1e-8 and error<1e-8,
                "kernel_input_tolerance_mm":row.get("kernel_input_tolerance_mm"),
                "rotation_determinant":determinant if row["status"]=="PASS" else None,
                "orthonormal_error":error if row["status"]=="PASS" else None})
        units.append({"unit_id":unit["id"],"name":unit["name"],"template":matching.get("template_name"),
            "status":proof.get("status","UNVERIFIED"),"group_alignment":proof.get("group_alignment"),
            "candidate_search_complete":matching.get("evidence",{}).get("candidate_search_complete"),
            "member_ids_match_unit":sorted(row["occurrence_id"] for row in members)==sorted(unit["occurrence_ids"]),
            "members":members})
    members=[row for unit in units for row in unit["members"]]
    checked_ids=[row["occurrence_id"] for row in members]
    return {"source_sha256":model["source"]["sha256"],"model_fingerprint":model["fingerprint"],"matching_version":model["matching_version"],
        "template_files":len(model["part_sources"]),"units":len(units),"occurrences":len(occurrences),"solids":sum(row["solid_count"] for row in occurrences.values()),
        "checked_members":len(members),"unit_results":dict(Counter(u["status"] for u in units)),"member_results":dict(Counter(m["status"] for m in members)),
        "every_occurrence_checked_exactly_once":len(checked_ids)==len(set(checked_ids)) and set(checked_ids)==set(occurrences),
        "all_unit_members_covered":all(unit["member_ids_match_unit"] for unit in units),
        "all_pass_members_have_complete_solid_bijection":all(m["complete_solid_bijection_certificate"] for m in members if m["status"]=="PASS"),
        "limited_candidate_search_units":[u["unit_id"] for u in units if u["candidate_search_complete"] is False],
        "global_matching_limit_diagnostics":[row for row in model.get("diagnostics",[]) if row.get("code") in ("MATCH_SEARCH_LIMITED","MATCH_AMBIGUOUS")],
        "verified_solid_pairs":sum(m["verified_solid_pairs"] for m in members if m["status"]=="PASS"),
        "all_target_content_hashes_current":all(m["target_content_hash_current"] for m in members),
        "all_pass_transforms_proper_rigid":all(m["proper_rigid_transform"] for m in members if m["status"]=="PASS"),
        "member_reason_counts":dict(Counter(m["reason"] for m in members if m["status"]!="PASS")),
        "unit_audit":units,"scope":"Each saved PASS is a bounded per-solid geometry correspondence at declared CAD input precision. Review is not proof of an incorrect part identity."}


def main():
    parser=argparse.ArgumentParser();parser.add_argument("--model",required=True);parser.add_argument("--output",required=True);args=parser.parse_args()
    model=json.loads(Path(args.model).read_text(encoding="utf-8"));model=model.get("result",model)
    result=summarize(model);Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in result.items() if k!="unit_audit"},ensure_ascii=False,indent=2))


if __name__=="__main__":main()
