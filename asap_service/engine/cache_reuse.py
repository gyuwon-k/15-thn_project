"""Copy only completed, provenance-bound native SDF caches into a fresh run.

This does not validate physics or authorize skipping native verification. It
uses explicit prior input manifests and successful per-body/per-component
audits; failed BVH diagnostic fields and unrecorded SDF files are never scanned.
Every copied cache must be reloaded and fully rechecked by the native adapter.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import shutil
import uuid


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            result.update(block)
    return result.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    temporary.replace(target)


def valid_hash(value):
    return isinstance(value, str) and len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def safe_path(directory, relative):
    value = PurePosixPath(relative)
    if (not isinstance(relative, str) or "\\" in relative or value.is_absolute()
            or not value.parts or ".." in value.parts):
        raise ValueError("Input/cache path is not relative to its staging directory")
    result = (Path(directory) / value).resolve()
    if not result.is_relative_to(Path(directory).resolve()):
        raise ValueError("Input/cache path escapes its staging directory")
    return result


def same_inputs(previous, current):
    if not isinstance(previous, dict) or not isinstance(current, dict):
        return False
    old, new = previous.get("input_files_sha256"), current.get("input_files_sha256")
    return (isinstance(old, dict) and isinstance(new, dict)
            and all(isinstance(name, str) for name in [*old, *new])
            and {name: value for name, value in old.items() if name.endswith(".obj")}
                == {name: value for name, value in new.items() if name.endswith(".obj")}
            and previous.get("physics_profile") == current.get("physics_profile"))


def geometry_contract(manifest):
    """Identity/partition coordinates stay fixed; CAD probe metadata may change."""
    result = {}
    bodies = manifest.get("bodies")
    if not isinstance(bodies, dict):
        raise ValueError("Solid manifest bodies must be an object")
    for part, body in bodies.items():
        if (not isinstance(part, str) or not isinstance(body, dict)
                or not isinstance(body.get("components"), list)
                or not all(isinstance(component, dict) for component in body["components"])):
            raise ValueError("Solid partition metadata is malformed")
        result[part] = {"unit_id": body.get("unit_id"), "source_solid_count": body.get("source_solid_count"),
            "components": [{key: component.get(key) for key in ("source_solid_index", "path", "sha256", "mapping",
                "closed", "positive_volume", "original_shell_count", "cavity_shell_count")} for component in body.get("components", [])]}
    return {"coordinate_system": manifest.get("coordinate_system"), "mapping": manifest.get("mapping"),
            "source_occurrence_count": manifest.get("source_occurrence_count"), "source_solid_count": manifest.get("source_solid_count"),
            "all_original_solids_preserved": manifest.get("all_original_solids_preserved"), "bodies": result}


def verified_field_metadata(proof, native_record):
    if not isinstance(proof, dict) or not isinstance(native_record, dict):
        return None
    frame = proof.get("native_frame_world_row_major")
    mass = proof.get("native_mass_density_one", native_record.get("native_mass_density_one"))
    parameters = proof.get("requested_sdf_parameters")
    if (not isinstance(frame, list) or len(frame) != 16
            or not all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in frame)
            or not isinstance(mass, (int, float)) or isinstance(mass, bool) or not math.isfinite(mass) or mass <= 0
            or not isinstance(parameters, dict) or not isinstance(parameters.get("sdf_dx"), (int, float))
            or isinstance(parameters.get("sdf_dx"), bool) or not math.isfinite(parameters["sdf_dx"]) or parameters["sdf_dx"] <= 0
            or not isinstance(parameters.get("sdf_res"), int) or isinstance(parameters.get("sdf_res"), bool) or parameters["sdf_res"] <= 0):
        return None
    return {"native_frame_world_row_major": frame, "native_mass_density_one": mass,
            "previous_cache_parameters": parameters}


def body_cache_candidates(part, body, cad_body, inputs):
    """Only hashes attached to complete successful SDF proofs are eligible."""
    obj_relative = part + ".obj"
    proof = body.get("native_reload_proof", {})
    components = cad_body.get("components", [])
    if not isinstance(proof, dict) or not isinstance(components, list):
        return []
    if (body.get("status") != "passed" or body.get("native_collision_type", "SDF") != "SDF"
            or body.get("obj_sha256") != inputs.get(obj_relative)
            or proof.get("status") != "passed" or proof.get("obj_sha256") != inputs.get(obj_relative)
            or body.get("original_solid_count") != len(components) or not components
            or body.get("source_metadata") != cad_body.get("audit", {})):
        return []
    whole_hash = body.get("composed_cache_sha256") or body.get("original_cache_sha256")
    metadata = verified_field_metadata(proof, body.get("native_aggregate", {}))
    if not valid_hash(whole_hash) or metadata is None:
        return []
    candidates = [{"obj_path": obj_relative, "cache_path": part + ".sdf", "cache_sha256": whole_hash,
                   "obj_sha256": inputs[obj_relative], "part": part, "kind": "successful_body_field",
                   "source_solid_index": None, **metadata}]
    component_audits = body.get("components", [])
    if len(components) == 1:
        # A one-solid body does not create an independently verified component
        # field; its whole cache is the only eligible field.
        return candidates
    if (not isinstance(component_audits, list) or not all(isinstance(record, dict) for record in component_audits)
            or len(component_audits) != len(components)
            or [record.get("source_solid_index") for record in component_audits] != list(range(len(components)))):
        return []
    for component, record in zip(components, component_audits):
        relative = component.get("path")
        field_proof = record.get("source_occupancy_proof", {})
        if not isinstance(field_proof, dict):
            return []
        metadata = verified_field_metadata(field_proof, record)
        if (not isinstance(relative, str) or component.get("sha256") != inputs.get(relative)
                or record.get("obj_sha256") != inputs.get(relative)
                or field_proof.get("status") != "passed" or field_proof.get("obj_sha256") != inputs.get(relative)
                or not valid_hash(record.get("cache_sha256")) or metadata is None):
            return []
        candidates.append({"obj_path": relative, "cache_path": str(PurePosixPath(relative).with_suffix(".sdf")),
                           "obj_sha256": inputs[relative], "cache_sha256": record["cache_sha256"],
                           "part": part, "kind": "successful_original_component_field", "source_solid_index": component["source_solid_index"], **metadata})
    return candidates


def reuse_verified_caches(current_staging_dir, current_native_provenance, previous_manifest_paths, audit_output=None):
    current_dir = Path(current_staging_dir).resolve()
    current_manifest_path = current_dir.parent / "input_manifest.json"
    report = {"status": "complete", "module_sha256": sha256(__file__), "copied_cache_count": 0,
              "eligible_previous_run_count": 0, "copied": {}, "previous_runs": [],
              "verified_cache_sha256": {}, "verified_cache_objects": {},
              "scope": "Only completed successful SDF bodies and their independently successful original-component fields. Same source/profile/native binary, exact all OBJ hashes, normalized placement config and partition identity. Probe metadata/revision changes are allowed with explicit lineage; every copied cache still requires complete fresh native geometry/frame/mass/current occupancy verification.",
              "native_verification_skipped": False, "BVH_failed_diagnostic_caches_reused": False}
    def persist():
        if audit_output:
            write_json(audit_output, report)
    try:
        current = read_json(current_manifest_path)
        inputs = current.get("input_files_sha256")
        native_hash = current_native_provenance.get("native_binary_sha256")
        native_core_hash = current_native_provenance.get("redmax_core_sha256")
        if (not isinstance(inputs, dict) or not inputs or not all(valid_hash(value) for value in inputs.values())
                or not valid_hash(native_hash) or not valid_hash(native_core_hash)
                or "solids_manifest.json" not in inputs):
            raise ValueError("Current input/native provenance is incomplete")
        # Hash every new input before any reuse; this also binds the complete
        # original triangle contract through the exact solids-manifest hash.
        for relative, expected in inputs.items():
            target = safe_path(current_dir, relative)
            if not target.is_file() or sha256(target) != expected:
                raise ValueError("Fresh staged input hash mismatch")
        cad_manifest = read_json(current_dir / "solids_manifest.json")
        source_hash, revision = cad_manifest.get("source_sha256"), cad_manifest.get("conversion_revision")
        cad_bodies = cad_manifest.get("bodies")
        if (not valid_hash(source_hash) or not isinstance(revision, str) or not revision
                or cad_manifest.get("ready_for_physics") is not True or cad_manifest.get("all_original_solids_preserved") is not True
                or not isinstance(cad_bodies, dict)):
            raise ValueError("Current source/solid provenance is incomplete")
    except (OSError, ValueError, TypeError, KeyError) as error:
        report.update(status="not_reused", reason="CURRENT_PROVENANCE_INVALID", message=str(error))
        persist()
        return report
    seen = set()
    for manifest_value in previous_manifest_paths:
        previous_manifest_path = Path(manifest_value).resolve()
        if previous_manifest_path == current_manifest_path or str(previous_manifest_path) in seen:
            continue
        seen.add(str(previous_manifest_path))
        record = {"input_manifest": str(previous_manifest_path), "status": "checking", "copied_cache_count": 0, "rejected_caches": []}
        report["previous_runs"].append(record)
        try:
            previous = read_json(previous_manifest_path)
            if not same_inputs(previous, current):
                raise ValueError("INPUT_OR_PROFILE_MISMATCH")
            prior_dir = previous_manifest_path.parent / "input_geometry"
            previous_cad_file = safe_path(prior_dir, "solids_manifest.json")
            if sha256(previous_cad_file) != previous["input_files_sha256"].get("solids_manifest.json"):
                raise ValueError("PRIOR_SOLID_MANIFEST_HASH_MISMATCH")
            prior_cad = read_json(previous_cad_file)
            if (prior_cad.get("source_sha256") != source_hash or geometry_contract(prior_cad) != geometry_contract(cad_manifest)):
                raise ValueError("PRIOR_SOURCE_OR_GEOMETRY_CONTRACT_MISMATCH")
            # Formatting-only changes in the optional placement config do not
            # affect native coordinates; all configuration values still match.
            old_inputs = previous["input_files_sha256"]
            if ("config.json" in old_inputs) != ("config.json" in inputs):
                raise ValueError("PLACEMENT_CONFIG_PRESENCE_MISMATCH")
            if "config.json" in inputs:
                old_config = safe_path(prior_dir, "config.json")
                if sha256(old_config) != old_inputs["config.json"]:
                    raise ValueError("PRIOR_PLACEMENT_CONFIG_HASH_MISMATCH")
                if json.dumps(read_json(old_config), sort_keys=True, allow_nan=False) != json.dumps(read_json(current_dir / "config.json"), sort_keys=True, allow_nan=False):
                    raise ValueError("PLACEMENT_CONFIG_VALUE_MISMATCH")
            audit_path = Path(previous.get("output_dir", "")).resolve() / "solid_sdf_audit.json"
            audit = read_json(audit_path)
            if (audit.get("native_binary_sha256") != native_hash or audit.get("redmax_core_sha256") != native_core_hash
                    or audit.get("source_sha256") != source_hash or audit.get("conversion_revision") != prior_cad.get("conversion_revision")
                    or audit.get("manifest_sha256") != old_inputs.get("solids_manifest.json")
                    or not isinstance(audit.get("bodies"), dict)):
                raise ValueError("PRIOR_NATIVE_SOURCE_OR_REVISION_MISMATCH")
            audit_hash = sha256(audit_path)
            record.update(status="eligible", audit_path=str(audit_path), audit_sha256=audit_hash,
                          overall_audit_status=audit.get("status"), previous_revision=prior_cad.get("conversion_revision"),
                          current_revision=revision, manifest_metadata_changed=old_inputs.get("solids_manifest.json") != inputs["solids_manifest.json"])
            report["eligible_previous_run_count"] += 1
            for part, body in audit["bodies"].items():
                if part not in cad_bodies or not isinstance(body, dict):
                    continue
                candidates = body_cache_candidates(part, body, prior_cad["bodies"][part], inputs)
                for candidate in candidates:
                    relative = candidate["cache_path"]
                    if relative in report["copied"]:
                        continue
                    try:
                        source_obj = safe_path(prior_dir, candidate["obj_path"])
                        source_cache = safe_path(prior_dir, relative)
                        destination = safe_path(current_dir, relative)
                        if (not source_obj.is_file() or sha256(source_obj) != candidate["obj_sha256"]
                                or not source_cache.is_file() or sha256(source_cache) != candidate["cache_sha256"]):
                            raise ValueError("CACHE_OR_PRIOR_OBJ_HASH_MISMATCH")
                        if destination.exists():
                            raise ValueError("DESTINATION_CACHE_ALREADY_EXISTS")
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        temporary = destination.with_name(destination.name + ".reuse-" + uuid.uuid4().hex + ".tmp")
                        try:
                            shutil.copyfile(source_cache, temporary)
                            if sha256(temporary) != candidate["cache_sha256"]:
                                raise ValueError("CACHE_CHANGED_DURING_COPY")
                            temporary.replace(destination)
                        finally:
                            if temporary.exists():
                                temporary.unlink()
                        lineage = {"origin_input_manifest": str(previous_manifest_path),
                            "origin_staging_dir": str(prior_dir), "origin_audit_path": str(audit_path), "origin_audit_sha256": audit_hash,
                            "current_destination": str(destination), "native_binary_sha256": native_hash,
                            "redmax_core_sha256": native_core_hash, "source_sha256": source_hash, "conversion_revision": revision,
                            "previous_revision": prior_cad.get("conversion_revision"), "manifest_metadata_changed": record["manifest_metadata_changed"],
                            "requires_fresh_native_verification": True}
                        report["copied"][relative] = {**candidate, **lineage}
                        report["verified_cache_sha256"][relative] = candidate["cache_sha256"]
                        report["verified_cache_objects"][candidate["obj_path"]] = {**candidate, "provenance": lineage}
                        record["copied_cache_count"] += 1
                        report["copied_cache_count"] += 1
                    except (OSError, ValueError, TypeError, KeyError) as error:
                        record["rejected_caches"].append({"cache_path": relative, "reason": str(error)})
        except (OSError, ValueError, TypeError, KeyError) as error:
            record.update(status="rejected", reason=str(error))
        persist()
    persist()
    return report
