"""Read-only audit of a completed source or frozen-package preprocessing result.

This reads persisted native validation evidence. It does not claim to execute
another native check or manufacture a successful import from cached records.
"""
import argparse
import hashlib
import json
from pathlib import Path


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = read_json(args.model)
    model = model.get("result", model)
    occurrences = model["occurrences"]
    original_six = [row for row in occurrences if row["name"].startswith(
        ("KET400_P_ASM", "BLT-WASM612A_M6 WASHER BOLT ASSY"))]
    six = []
    for row in original_six:
        repair = row["repair"]
        audit = read_json(repair["audit_path"])
        six.append({
            "id": row["id"], "name": row["name"],
            "solid_count": row["solid_count"],
            "exact_valid": row.get("exact_method_valid"),
            "collision_ready": row.get("collision_ready"),
            "source_modified": audit.get("source_modified"),
            "roundtrip_standard_valid": audit.get("roundtrip_standard_valid"),
            "roundtrip_exact_valid": audit.get("roundtrip_exact_valid"),
            "roundtrip_preservation_checks": audit.get("roundtrip_preservation_checks"),
            "audit_path": repair["audit_path"],
        })
    part_hashes = {row["sha256"] for row in model["part_sources"]}
    templates = []
    for manifest in args.cache.glob("*/manifest.json"):
        source = read_json(manifest)
        if source["source"]["sha256"] not in part_hashes:
            continue
        results = []
        for occurrence in source["instances"]:
            digest = hashlib.sha256(Path(occurrence["brep_path"]).read_bytes()).hexdigest()
            proof = read_json(args.cache / model["repair_version"] / (digest[:24] + ".json"))
            results.append({"id": occurrence["id"], "name": occurrence["name"],
                            "exact_valid": proof.get("exact_valid"),
                            "reason": proof.get("reason"), "audit_path": proof.get("audit_path")})
        templates.append({"file": source["source"]["name"], "occurrences": len(results),
                          "exact_pass": sum(row["exact_valid"] is True for row in results),
                          "review": [row for row in results if row["exact_valid"] is not True]})
    reviewed = [row for row in occurrences if row.get("exact_method_valid") is not True]
    output = {
        "evidence_method": "Read completed result and persisted native audits; no additional native run.",
        "model_path": str(args.model.resolve()),
        "model_sha256": hashlib.sha256(args.model.read_bytes()).hexdigest(),
        "source_sha256": model["source"]["sha256"],
        "repair_version": model["repair_version"], "matching_version": model["matching_version"],
        "occurrences": len(occurrences), "solids": sum(row["solid_count"] for row in occurrences),
        "assembly_exact_pass": sum(row.get("exact_method_valid") is True for row in occurrences),
        "assembly_review": [{"id": row["id"], "name": row["name"],
                             "collision_ready": row.get("collision_ready"),
                             "reason": row.get("repair", {}).get("reason")} for row in reviewed],
        "template_files": len(templates),
        "template_occurrences": sum(row["occurrences"] for row in templates),
        "template_exact_pass": sum(row["exact_pass"] for row in templates),
        "templates": templates, "original_six": six,
        "inventory_complete": model["quality"]["inventory_complete"],
        "quality_status": model["quality"]["status"],
        "source_warning_count": model["quality"].get("source_warning_count"),
    }
    checks = {
        "expected_source_inventory": output["occurrences"] == 78 and output["solids"] == 176 and output["inventory_complete"],
        "current_repair_version": model["repair_version"] == "exact-residual-preserved-2.1.3",
        "current_matching_version": model["matching_version"] == "brep-verified-constellation-2.1.7",
        "assembly_exact_pass_76": output["assembly_exact_pass"] == 76,
        "all_review_shapes_unready": all(row.get("collision_ready") is False for row in reviewed),
        "template_exact_pass_37_of_40": output["template_files"] == 18 and output["template_occurrences"] == 40 and output["template_exact_pass"] == 37,
        "original_six_persisted_roundtrip_evidence": len(six) == 6 and all(
            row["exact_valid"] is True and row["collision_ready"] is True and row["source_modified"] is False
            and row["roundtrip_standard_valid"] is True and row["roundtrip_exact_valid"] is True
            and bool(row["roundtrip_preservation_checks"]) and all(row["roundtrip_preservation_checks"].values())
            for row in six),
    }
    output["checks"] = checks
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: value for key, value in output.items() if key not in ("templates", "original_six")}, ensure_ascii=True))
    assert all(checks.values()), checks


if __name__ == "__main__":
    main()
