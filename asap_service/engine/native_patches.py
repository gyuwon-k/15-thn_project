"""Exact, auditable native fork for undefined zero-norm friction derivatives.

Force values and every positive-norm derivative retain the upstream formulas.
At an exactly zero vector norm its derivative is undefined; the guarded fork
selects the symmetric zero subgradient instead of evaluating 1/0. Source
acceptance requires reversing only this canonical patch to the pinned core.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re

PATCH_ID = "redmax-bvh-zero-norm-derivatives-v1"
PINNED_CORE_SHA256 = "cb0ccaaf80dc7e5315bed28b1ceb6863960cfeb72cb1e36a7c55bc26ff356669"
PATCH_FILE = "Force/ForceGeneralBVHContact.cpp"
BASE_FILE_SHA256 = "08a7350eed1a61be04ec07b0ffd5d2b50aeba265899201112cab7f4f1271e64d"
PATCHED_FILE_SHA256 = "64730de98a0f7d244544ead2adade6a6ee448c1cf18e08165d762fcdbc2d4c2d"
MANIFEST_NAME = "native_guard_manifest.json"


class NativePatchError(Exception):
    pass


def normalized_bytes(value):
    return value.replace(b"\r\n", b"\n")


def digest(value):
    return hashlib.sha256(value).hexdigest()


def file_sha256(path):
    return digest(Path(path).read_bytes())


def _guard_lines(indent):
    return (indent + "// " + PATCH_ID + ": symmetric zero subgradient only at norm == 0.\n"
            + indent + "dtype inverse_fc_norm = fc_norm > 0. ? 1. / fc_norm : 0.;\n"
            + indent + "dtype inverse_tdot_norm = tdot_norm > 0. ? 1. / tdot_norm : 0.;\n")


def patch_force_source(value):
    text = normalized_bytes(value).decode("utf-8")
    if digest(text.encode()) != BASE_FILE_SHA256:
        raise NativePatchError("The native force source is not the exact pinned base file")
    pattern = r"(?m)^(?P<indent> +)RowVector6 dfc_norm_dq1"
    text, inserted = re.subn(pattern, lambda match: _guard_lines(match.group("indent"))
                            + match.group("indent") + "RowVector6 dfc_norm_dq1", text)
    if (inserted != 2 or text.count("(1. / fc_norm)") != 8
            or text.count("(1. / tdot_norm)") != 8 or text.count("fc1.transpose() / fc_norm") != 3):
        raise NativePatchError("The exact two derivative overloads and parameter sites were not found")
    text = text.replace("(1. / fc_norm)", "inverse_fc_norm")
    text = text.replace("(1. / tdot_norm)", "inverse_tdot_norm")
    text = text.replace("fc1.transpose() / fc_norm", "fc1.transpose() * inverse_fc_norm")
    return text.encode("utf-8")


def restore_force_source(value):
    text = normalized_bytes(value).decode("utf-8")
    if digest(text.encode()) != PATCHED_FILE_SHA256:
        raise NativePatchError("The native guard file is not the exact canonical patched payload")
    pattern = r"(?P<indent> +)// " + re.escape(PATCH_ID) + r": symmetric zero subgradient only at norm == 0\.\n(?P=indent)dtype inverse_fc_norm = fc_norm > 0\. \? 1\. / fc_norm : 0\.;\n(?P=indent)dtype inverse_tdot_norm = tdot_norm > 0\. \? 1\. / tdot_norm : 0\.;\n"
    text, removed = re.subn(pattern, "", text)
    if removed != 2:
        raise NativePatchError("The canonical zero-norm guards are incomplete")
    text = text.replace("fc1.transpose() * inverse_fc_norm", "fc1.transpose() / fc_norm")
    text = text.replace("inverse_fc_norm", "(1. / fc_norm)")
    text = text.replace("inverse_tdot_norm", "(1. / tdot_norm)")
    if digest(text.encode()) != BASE_FILE_SHA256:
        raise NativePatchError("Restoring the guard did not restore the exact base force source")
    return text.encode("utf-8")


def core_digest(core_root, restored_force=None):
    core_root = Path(core_root)
    files = sorted((path for path in core_root.rglob("*") if path.suffix in {".h", ".cpp"}
                    and path.name != "python_interface.cpp"), key=lambda path: path.relative_to(core_root).as_posix())
    result = hashlib.sha256()
    for path in files:
        relative = path.relative_to(core_root).as_posix()
        result.update(relative.encode() + b"\0")
        payload = restored_force if relative == PATCH_FILE and restored_force is not None else normalized_bytes(path.read_bytes())
        result.update(payload + b"\0")
    return result.hexdigest()


def canonical_patch_record():
    return {"patch_id": PATCH_ID, "file": "simulation/redmax/" + PATCH_FILE,
            "base_file_sha256_lf": BASE_FILE_SHA256, "patched_file_sha256_lf": PATCHED_FILE_SHA256,
            "state_derivative_overloads": 2, "guarded_force_norm_state_derivatives": 8,
            "guarded_tangent_norm_state_derivatives": 8, "guarded_force_norm_parameter_sites": 3,
            "rule": "norm > 0: identical upstream reciprocal; norm == 0: symmetric zero subgradient",
            "force_values_unchanged": True, "physical_parameters_unchanged": True,
            "scope": "Finite derivative choice at an undefined exactly-zero norm; no force/friction/gravity/damping/contact or collision-distance changes."}


def verify_native_core(asap_root, binary_path=None):
    root = Path(asap_root).resolve()
    core = root / "simulation/redmax"
    actual = core_digest(core)
    patched = actual != PINNED_CORE_SHA256
    record = {"redmax_core_sha256": actual, "base_redmax_core_sha256": PINNED_CORE_SHA256,
              "native_core_unchanged": not patched, "native_source_patches": [],
              "guarded_binary_verification": "pending" if binary_path is None else "passed"}
    if patched:
        restored = restore_force_source((core / PATCH_FILE).read_bytes())
        if core_digest(core, restored) != PINNED_CORE_SHA256:
            raise NativePatchError("Native source differs beyond the exact allowed zero-norm derivative guard")
        record["native_source_patches"] = [canonical_patch_record()]
    if binary_path is not None:
        binary = Path(binary_path).resolve()
        if not binary.is_file() or not binary.is_relative_to(root / "simulation"):
            raise NativePatchError("The imported native extension file does not exist")
        binary_hash = file_sha256(binary)
        record.update(native_binary_path=str(binary), native_binary_sha256=binary_hash)
        if patched:
            path = root / MANIFEST_NAME
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                raise NativePatchError("The guarded build manifest is missing or invalid") from error
            if not isinstance(manifest, dict):
                raise NativePatchError("The guarded build manifest must be an object")
            declared=Path(manifest.get('extension',''))
            # Installed copies retain the historical build path as provenance.
            # Runtime trust binds to the selected root, exact filename and hash.
            binding_matches=declared.resolve()==binary or declared.name==binary.name
            if (manifest.get("patch_id") != PATCH_ID or manifest.get("base_redmax_core_sha256") != PINNED_CORE_SHA256
                    or manifest.get("redmax_core_sha256") != actual
                    or manifest.get("canonical_patch") != canonical_patch_record()
                    or not binding_matches
                    or manifest.get("extension_sha256") != binary_hash
                    or manifest.get("status") != "built"):
                raise NativePatchError("The imported guarded binary is not bound to the verified canonical build manifest")
            record["native_guard_build_manifest_sha256"] = file_sha256(path)
            record["native_guard_build"] = manifest
            record['native_guard_runtime_binding']={'root':str(root),'extension':str(binary),
                                                    'historical_extension':str(declared),
                                                    'relocated':declared.resolve()!=binary,
                                                    'exact_filename_and_sha256':True}
    return record
