"""Offline process boundary for the pinned official ASAP physics/GNN planner.

This module intentionally imports only the standard library until the engine
environment has been selected. It is usable from Python 3.9 onwards. It never
downloads models, reads another project's tree, or invents sequence/path data.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import inspect
import json
import locale
import math
import os
from pathlib import Path
import platform
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

PINNED_COMMIT = "3727e716b57e0b508c4e9ecd9fefa6953e8f72da"
OFFICIAL_WEIGHTS_SHA256 = "430a51253c25170e8f845ee21834a1e33714225fdf5f87b7cb687e4fe260d99a"
PINNED_REDMAX_CORE_SHA256 = "cb0ccaaf80dc7e5315bed28b1ceb6863960cfeb72cb1e36a7c55bc26ff356669"
SCHEMA_VERSION = "asap-test-engine/1"
SOURCE_HASHES = {
    "plan_sequence/planner/base.py": "b9b4ec9c361f5eb66964c3db5e916edfa75f62236b48f6648a58758e2991049d",
    "plan_sequence/planner/dfs.py": "3718b1a137f34aee60f2d89aac846b659adb7e9f6d424790030c0718f15707eb",
    "plan_sequence/physics_planner.py": "a703d55d010786f0d51272ff3737f118000be492d0e35a61eb8765ebde987ea1",
    "plan_sequence/feasibility_check.py": "07b9f923880d610787c24c3b35f4e88e859e4172b2f2d76a577bd45993016795",
    "plan_sequence/sim_string.py": "718d4f5f46323613a6a85c6d29131f6237ef277817d7e8280980f3f763c8a41a",
    "plan_sequence/generator/learning.py": "c9e8b032087a2fd566f909561b78bc0c3a63c19cd9b6e8eac4c0b86f4eae728e",
    "plan_sequence/generator/network/model.py": "250cdb1c5fb753aa47165838c72065294c96bb8cb09205daef2da57fc107d2c5",
    "plan_sequence/generator/network/data.py": "e87f738db60a58d93bbd26f1128d03926f4d7d9ce73f6cdde0c980321abbac8e",
    "assets/load.py": "a046bd096c91772bf06d407095b72970a3a46bdbc71bc3e8ed4b19a8eed979ab",
}


class BridgeError(Exception):
    def __init__(self, reason_code, message, action=""):
        super().__init__(message)
        self.reason_code = reason_code
        self.action = action


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    os.replace(str(temporary), str(path))


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def solid_adapter():
    """Load only this application's input adapter, independent of cwd/sys.path."""
    path = Path(__file__).with_name("solid_sdf.py")
    specification = importlib.util.spec_from_file_location("asap_test_solid_sdf", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def native_guard_provenance(root, binary_path=None):
    """Accept the pinned core or only the reversible canonical numeric guard."""
    path = Path(__file__).with_name("native_patches.py")
    specification = importlib.util.spec_from_file_location("asap_test_native_patches", path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    try:
        return module.verify_native_core(root, binary_path)
    except (module.NativePatchError, OSError, ValueError, KeyError) as exc:
        raise BridgeError("ENGINE_INCOMPATIBLE", "RedMax 소스·바이너리 검증에 실패했습니다: " + str(exc),
                          "고정 원본 코어 또는 검증된 수치 가드 빌드와 manifest를 사용하세요.") from exc


def emit(stage, message, **fields):
    print(json.dumps({"stage": stage, "message": message, **fields}, ensure_ascii=False), flush=True)


def config_engine(config):
    return config.get("engine", config)


def source_provenance(root):
    root = Path(root).resolve()
    missing = [name for name in SOURCE_HASHES if not (root / name).is_file()]
    if missing:
        raise BridgeError("ASAP_SOURCE_MISSING", "ASAP 소스 파일이 없습니다: " + ", ".join(missing), "고정된 공식 ASAP 소스를 설치하세요.")
    hashes = {name: hashlib.sha256((root / name).read_bytes().replace(b"\r\n", b"\n")).hexdigest() for name in SOURCE_HASHES}
    mismatch = [name for name, actual in hashes.items() if SOURCE_HASHES[name] != actual]
    if mismatch:
        raise BridgeError("ENGINE_INCOMPATIBLE", "검증한 ASAP 계획/입력 코드와 다릅니다: " + ", ".join(mismatch), "고정 커밋의 계획 코드를 복원하거나 어댑터 호환성을 검증하세요.")
    core_root = root / "simulation" / "redmax"
    native_provenance = native_guard_provenance(root)
    commit = None
    dirty = None
    try:
        git_root = subprocess.check_output(["git", "-C", str(root), "rev-parse", "--show-toplevel"], text=True, encoding="utf-8", stderr=subprocess.DEVNULL, timeout=5).strip()
        # A source archive can live inside another application's repository.
        # Git walks up to that parent, whose HEAD is unrelated to ASAP.
        # Only an ASAP checkout itself may supply commit/dirty provenance.
        if Path(git_root).resolve() == root:
            commit = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True, encoding="utf-8", stderr=subprocess.DEVNULL, timeout=5).strip()
            dirty = subprocess.check_output(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"], text=True, encoding="utf-8", stderr=subprocess.DEVNULL, timeout=5).splitlines()
    except (OSError, subprocess.SubprocessError):
        pass  # Source archives are supported only when all pinned hashes match.
    if commit is not None and commit != PINNED_COMMIT:
        raise BridgeError("ENGINE_INCOMPATIBLE", "ASAP 커밋이 다릅니다: " + commit, "고정 커밋 " + PINNED_COMMIT + "를 사용하세요.")
    binding = core_root / "python_interface.cpp"
    return {"asap_commit": commit or PINNED_COMMIT, "source_verification": "commit+sha256" if commit else "sha256-archive", "source_hashes": hashes,
            **native_provenance, "native_patch_verifier_sha256": sha256(Path(__file__).with_name("native_patches.py")),
            "redmax_binding_sha256": sha256(binding) if binding.is_file() else None, "tracked_changes": dirty or []}


def prepare_engine_path(root):
    # RedMax parses machine-generated XML and SDF decimal numbers. This process
    # boundary always uses the C numeric/character locale, independently of the
    # user's Windows display language; it does not change operating-system settings.
    locale.setlocale(locale.LC_ALL, "C")
    root = Path(root).resolve()
    sys.path.insert(0, str(root))
    # A locally compiled extension may be installed, or left in simulation/build.
    for folder in [root / "simulation", root / "simulation" / "build"]:
        if folder.is_dir():
            sys.path.insert(0, str(folder))
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ.setdefault("MPLBACKEND", "Agg")
    return root


def load_checkpoint(weights_path, expected_hash=None):
    path = Path(weights_path or "")
    if not weights_path or not path.is_file():
        raise BridgeError("MODEL_MISSING", "ASAP 사전학습 가중치 파일이 없습니다.", "환경 설정에 확보한 공식 pretrained_model.pt 경로를 지정하세요.")
    digest = sha256(path)
    if expected_hash and digest != expected_hash.lower():
        raise BridgeError("MODEL_INCOMPATIBLE", "가중치 SHA256이 설정과 다릅니다.", "가중치 파일과 설정한 해시를 확인하세요.")
    import torch
    options = {"map_location": "cpu"}
    # Never allow unrestricted pickle loading on versions that offer safe mode.
    if "weights_only" not in inspect.signature(torch.load).parameters:
        raise BridgeError("MODEL_INCOMPATIBLE", "현재 torch는 안전한 weights_only 로딩을 지원하지 않습니다.", "검증된 엔진 torch 버전을 사용하세요.")
    options["weights_only"] = True
    try:
        if digest == OFFICIAL_WEIGHTS_SHA256 and hasattr(torch.serialization, "safe_globals"):
            import numpy as np
            # The original training checkpoint stores numeric NumPy metadata.
            # Allow only exact numeric objects for this verified official file;
            # never unrestricted torch.load or arbitrary checkpoint globals.
            numeric_types = [np.core.multiarray.scalar, np.dtype, type(np.dtype("float64")), type(np.dtype("int64"))]
            with torch.serialization.safe_globals(numeric_types):
                checkpoint = torch.load(str(path), **options)
        else:
            checkpoint = torch.load(str(path), **options)
        if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("model_state_dict"), dict):
            raise ValueError("model_state_dict is absent")
        from plan_sequence.generator.network.model import ModelGraph
        from plan_sequence.generator.network.args import get_args
        model = ModelGraph(get_args())
        model.load_state_dict(checkpoint["model_state_dict"], strict=True)
        model.eval()
        for parameter in model.parameters():
            parameter.requires_grad_(False)
        return model, digest
    except Exception as exc:
        raise BridgeError("MODEL_INCOMPATIBLE", "ASAP 가중치와 네트워크를 함께 로드하지 못했습니다: " + str(exc), "공식 가중치 및 PyTorch/PyG 호환 버전을 확인하세요.") from exc


def native_smoke_test(root):
    """Exercise the compiled simulator without any display or geometry files."""
    import numpy as np
    import redmax_py as redmax
    xml = '''<redmax model="asap-native-smoke"><option integrator="BDF1" timestep="1e-3" gravity="0 0 0"/>
<robot><link name="base"><joint name="base" type="fixed" pos="0 0 0" quat="1 0 0 0" frame="WORLD"/>
<body name="base" type="cuboid" size="1 1 1" pos="0 0 0" quat="1 0 0 0" density="1"/></link></robot>
<robot><link name="moving"><joint name="moving" type="free3d-exp" pos="3 0 0" quat="1 0 0 0" frame="WORLD"/>
<body name="moving" type="cuboid" size="1 1 1" pos="0 0 0" quat="1 0 0 0" density="1"/></link></robot></redmax>'''
    sim = redmax.Simulation(xml, str(Path(root) / "assets"))
    sim.reset()
    start = np.asarray(sim.get_body_E0j("moving")).copy()
    mass = sim.get_body_mass("moving")
    if not np.isfinite(start).all() or not np.allclose(start[:3, 3], [3, 0, 0]) or not np.isclose(mass, 1):
        raise RuntimeError("native constructor returned invalid mass or frame: mass=" + str(mass) + "; frame=" + repr(start.tolist()))
    sim.forward(1)
    if not np.isfinite(sim.get_q()).all() or sim.body_in_contact("base", "moving", 0.01):
        raise RuntimeError("native dynamics/contact smoke failed")
    return {"constructor": True, "forward_steps": 1, "native_frame": True, "separated_contact": True}


def check_environment(config):
    engine = config_engine(config)
    checks = []
    capabilities = []
    provenance = {"python": sys.version.split()[0], "python_executable": sys.executable, "platform": platform.platform()}
    source_ok = False
    root = engine.get("asap_root")
    try:
        if not root:
            raise BridgeError("ASAP_SOURCE_MISSING", "ASAP 소스 경로가 설정되지 않았습니다.")
        provenance.update(source_provenance(root))
        prepare_engine_path(root)
        source_ok = True
        checks.append({"name": "asap_source", "status": "ok", "version": provenance["asap_commit"]})
    except BridgeError as exc:
        checks.append({"name": "asap_source", "status": "missing" if exc.reason_code.endswith("MISSING") else "incompatible", "reason_code": exc.reason_code, "message": str(exc), "action": exc.action})
    packages_ok = True
    for name in ["redmax_py", "numpy", "scipy", "torch", "torch_geometric", "networkx", "trimesh", "matplotlib"]:
        try:
            module = importlib.import_module(name)
            if name == "redmax_py":
                required = ["get_body_E0j", "get_joint_qm", "set_joint_qm", "update_robot"]
                if not hasattr(module, "Simulation") or any(not hasattr(module.Simulation, item) for item in required):
                    raise RuntimeError("required RedMax matrix interface is absent")
                if source_ok:
                    provenance.update(native_guard_provenance(root, module.__file__))
            version = getattr(module, "__version__", "native")
            provenance[name] = str(version)
            checks.append({"name": name, "status": "ok", "version": str(version)})
        except Exception as exc:
            packages_ok = False
            checks.append({"name": name, "status": "missing" if isinstance(exc, ModuleNotFoundError) else "incompatible", "reason_code": "ENGINE_DEPENDENCY_MISSING", "message": str(exc), "action": "엔진 전용 환경에 의존성을 설치하고 RedMax 네이티브 확장을 빌드하세요."})
    core_ok = False
    if source_ok and packages_ok:
        try:
            importlib.import_module("plan_sequence.generator")
            importlib.import_module("plan_sequence.planner")
            smoke = native_smoke_test(root)
            core_ok = True
            checks.append({"name": "planner_import", "status": "ok"})
            checks.append({"name": "redmax_native_smoke", "status": "ok", "evidence": smoke, "scope": "primitive_constructor_forward_and_contact; input_SDF_runs_require_separate_execution"})
        except Exception as exc:
            checks.append({"name": "planner_import", "status": "incompatible", "reason_code": "ENGINE_DEPENDENCY_MISSING", "message": str(exc), "action": "ASAP의 추가 의존성과 네이티브 라이브러리를 확인하세요."})
    weights = engine.get("weights_path")
    model_ok = False
    if not weights or not Path(weights).is_file():
        checks.append({"name": "pretrained_model", "status": "missing", "reason_code": "MODEL_MISSING", "message": "공식 가중치 파일 경로를 지정하세요."})
    elif core_ok:
        try:
            _, digest = load_checkpoint(weights, engine.get("weights_sha256"))
            provenance["weights_sha256"] = digest
            model_ok = True
            checks.append({"name": "pretrained_model", "status": "ok", "sha256": digest})
        except BridgeError as exc:
            checks.append({"name": "pretrained_model", "status": "incompatible", "reason_code": exc.reason_code, "message": str(exc), "action": exc.action})
    else:
        checks.append({"name": "pretrained_model", "status": "pending", "sha256": sha256(weights), "message": "엔진 의존성이 준비된 후 네트워크 호환성을 검사합니다."})
    if core_ok:
        try:
            adapter = solid_adapter()
            workspace = Path(engine.get("workspace_root") or Path(root) / ".test-runs").resolve()
            if platform.system() == "Windows" and not str(workspace).isascii():
                raise BridgeError("ENGINE_WORKSPACE_INVALID", "SDF 검증 폴더는 ASCII 경로여야 합니다.")
            proof = adapter.native_smoke_test(workspace, Path(root) / "assets")
            capabilities.append(adapter.CAPABILITY)
            provenance["solid_sdf_adapter_sha256"] = sha256(Path(__file__).with_name("solid_sdf.py"))
            checks.append({"name": adapter.CAPABILITY, "status": "ok", "evidence": proof,
                           "scope": "native_original_solid_OR_sign_cavity_inertia_frame_world_vertices_cache_reload; every_input_requires_independent_manifest_and_grid_proof"})
        except Exception as exc:
            checks.append({"name": "solid_or_sdf_v1", "status": "incompatible", "reason_code": getattr(exc, "reason_code", "SDF_ADAPTER_INCOMPATIBLE"),
                           "message": str(exc), "action": "원본 솔리드 SDF 입력 어댑터의 네이티브 검증을 확인하세요."})
    return {"schema_version": SCHEMA_VERSION, "ready": core_ok and model_ok, "learn_ready": core_ok and model_ok, "heur_out_ready": core_ok, "capabilities": capabilities, "checks": checks, "provenance": provenance, "runtime": platform.system()}


def validate_settings(raw):
    settings = {"strategy": "learn", "planner": "dfs", "seed": 0, "budget": 400, "timeout_s": 1800, "max_gripper": 3, "max_pose": 3, "pose_reuse": 0, "base_part": None, "physics_profile": None}
    unknown = set(raw) - set(settings)
    if unknown:
        raise BridgeError("INVALID_SETTINGS", "지원하지 않는 실행 설정: " + ", ".join(sorted(unknown)))
    settings.update(raw)
    if settings["strategy"] not in {"learn", "heur-out"} or settings["planner"] != "dfs":
        raise BridgeError("INVALID_SETTINGS", "지원하는 전략은 learn/heur-out, 탐색기는 dfs입니다.")
    for name, minimum, maximum in [("seed", 0, 2**31 - 1), ("budget", 1, 100000), ("timeout_s", 1, 86400), ("max_gripper", 2, 100), ("max_pose", 1, 100), ("pose_reuse", 0, 100)]:
        value = settings[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum or value > maximum:
            raise BridgeError("INVALID_SETTINGS", name + " 실행 범위를 확인하세요.")
    if settings["pose_reuse"] > settings["max_pose"]:
        raise BridgeError("INVALID_SETTINGS", "pose_reuse는 max_pose 이하이어야 합니다.")
    profile = settings["physics_profile"]
    if profile is not None:
        required = {"contact_eps_mm", "sdf_dx_mm", "sdf_res", "path_col_th_mm", "minimum_separation_mm"}
        if not isinstance(profile, dict) or set(profile) != required:
            raise BridgeError("INVALID_SETTINGS", "physics_profile의 필수 물리 거리 설정을 확인하세요.")
        for name, low, high in [("contact_eps_mm", 1e-5, 10), ("sdf_dx_mm", 1e-4, 20), ("path_col_th_mm", 0, 1), ("minimum_separation_mm", 1e-4, 100)]:
            value = profile[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise BridgeError("INVALID_SETTINGS", name + " 물리 거리 범위를 확인하세요.")
        resolution = profile["sdf_res"]
        if isinstance(resolution, bool) or not isinstance(resolution, int) or not 4 <= resolution <= 200:
            raise BridgeError("INVALID_SETTINGS", "sdf_res는 4~200 사이 정수여야 합니다.")
    return settings


def resolve_physics_parameters(profile, coordinate_map):
    """Convert explicitly requested millimetres without scaling CAD/OBJ again."""
    scale = coordinate_map.get("scale") if isinstance(coordinate_map, dict) else None
    if scale is not None and (isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0):
        scale = None
    if profile is not None and scale is None:
        raise BridgeError("INVALID_COORDINATE_MAP", "mm 물리 설정에는 유효한 CAD→엔진 좌표 배율이 필요합니다.")
    parameters = {"contact_eps": .1, "sdf_dx": .05, "sdf_res": 20, "path_col_th": .01, "minimum_separation": .5,
                  "contact_sdf_col_th": .01, "stability_col_th": 0.0}
    if profile is not None:
        for name in ["contact_eps", "sdf_dx", "path_col_th", "minimum_separation"]:
            parameters[name] = float(profile[name + "_mm"]) * scale
        parameters["sdf_res"] = profile["sdf_res"]
        # Upstream contact XML omits col_th, while the native constructor defaults
        # it to +0.01. Explicit mm contact distance needs the actual SDF zero set.
        parameters["contact_sdf_col_th"] = 0.0
    return parameters, scale


class PhysicsParameterAdapter:
    """Process-local numeric parameters; upstream equations/source stay immutable.

    Every XML factory used by contact, removal, stability and path export shares
    the same SDF profile. The original overlap compensation is audited verbatim,
    never enlarged or hidden to make a candidate succeed.
    """
    def __init__(self, profile, coordinate_map, audit_path=None):
        self.parameters, self.scale = resolve_physics_parameters(profile, coordinate_map)
        self.profile = profile
        self.audit_path = Path(audit_path) if audit_path else None
        self._restore = []
        self._pair_offsets = {}
        self._solid_caches = {}
        self._solid_bvh = {}
        self._static_query_xml = {}
        self.report = {"profile_mode": "explicit_mm" if profile else "official_defaults", "requested_profile_mm": profile,
                       "engine_units_per_mm": self.scale, "engine_parameters": self.parameters.copy(),
                       "physical_mm": {key + "_mm": value / self.scale for key, value in self.parameters.items() if key != "sdf_res"} if self.scale else None,
                       "sdf_grid_rule": "each inertia-frame bbox axis: min(sdf_dx, bbox_axis_length/sdf_res)",
                       "source_equations_changed": False, "weights_changed": False,
                       "xml_factories": {}, "initial_overlap_compensation": {"method": "official_MultiPartPathPlanner._compute_col_th_dict_unchanged", "candidate_count": 0,
                            "pairs": [], "maximum_offset_engine": 0.0, "maximum_offset_mm": 0.0 if self.scale else None,
                            "maximum_col_th_engine": self.parameters["path_col_th"], "maximum_col_th_mm": self.parameters["path_col_th"] / self.scale if self.scale else None,
                            "limitation": "SDF sampling and initial-overlap compensation are numerical model allowances, not exact CAD nonpenetration or continuous-path proof."}}

    def _set(self, owner, name, value):
        # A class may inherit an attribute. Restore absence as well as its value.
        existed = name in vars(owner)
        original = getattr(owner, name)
        self._restore.append((owner, name, existed, original))
        setattr(owner, name, value)

    def rewrite_xml(self, factory_name, original, *args, **kwargs):
        xml = original(*args, **kwargs)
        tree = ET.fromstring(xml)
        bodies = [body for body in tree.iter("body") if body.get("type") == "SDF"]
        if self.profile is not None:
            for body in bodies:
                body.set("dx", format(self.parameters["sdf_dx"], ".17g"))
                body.set("res", str(self.parameters["sdf_res"]))
                if factory_name == "get_contact_sim_string":
                    body.set("col_th", "0")
        bvh_names = set()
        if self._solid_caches or self._solid_bvh:
            for body in bodies:
                filename = Path(body.get("filename", "")).resolve()
                bvh = self._solid_bvh.get(str(filename))
                if bvh is not None:
                    stat = filename.stat()
                    if stat.st_size != bvh["size"] or stat.st_mtime_ns != bvh["mtime_ns"]:
                        raise BridgeError("SOLID_INPUT_CHANGED", "검증 후 원본 BVH 삼각형 파일이 바뀌었습니다.")
                    body.set("type", "BVH")
                    body.set("BVH_mesh_filename", str(filename))
                    body.set("load_sdf", "false")
                    body.set("save_sdf", "false")
                    bvh_names.add(body.get("name"))
                    continue
                expected = self._solid_caches.get(str(filename))
                cache = filename.with_suffix(".sdf")
                if expected is None or not cache.is_file():
                    raise BridgeError("SDF_CACHE_CHANGED", "검증한 원본 솔리드 SDF 캐시가 없습니다.")
                stat = cache.stat()
                if stat.st_size != expected["size"] or stat.st_mtime_ns != expected["mtime_ns"]:
                    raise BridgeError("SDF_CACHE_CHANGED", "검증 후 원본 솔리드 SDF 캐시가 바뀌었습니다.")
                body.set("load_sdf", "true")
                body.set("save_sdf", "false")
        if bvh_names:
            for defaults in tree.iter("default"):
                sdf_defaults = defaults.find("general_SDF_contact")
                if sdf_defaults is not None:
                    bvh_defaults = defaults.find("general_BVH_contact")
                    if bvh_defaults is None:
                        bvh_defaults = ET.SubElement(defaults, "general_BVH_contact")
                    # Native parser otherwise defaults BVH stiffness/damping to
                    # zero. Preserve all original path/stability force values.
                    bvh_defaults.attrib.clear()
                    bvh_defaults.attrib.update(sdf_defaults.attrib)
            for contacts in tree.iter("contact"):
                for contact in contacts:
                    if contact.tag == "general_SDF_contact" and contact.get("SDF_body") in bvh_names:
                        if tree.find("./default/general_BVH_contact") is None:
                            raise BridgeError("BVH_FORCE_DEFAULTS_MISSING", "BVH 접촉에 원본 강성·감쇠·마찰 설정이 필요합니다.")
                        contact.tag = "general_BVH_contact"
                        contact.set("BVH_body", contact.attrib.pop("SDF_body"))
        if self.profile is not None or self._solid_caches or self._solid_bvh:
            xml = ET.tostring(tree, encoding="unicode")
        for body in bodies:
            if not math.isclose(float(body.get("dx", ".05")), self.parameters["sdf_dx"], rel_tol=1e-12) or int(body.get("res", "20")) != self.parameters["sdf_res"]:
                raise BridgeError("PHYSICS_PROFILE_MISMATCH", "실제 SDF XML과 요청한 물리 정밀도가 다릅니다.")
        if factory_name == 'get_contact_sim_string':
            # Static metric queries have no contacts, no dynamic joints and no
            # forwards. A subset may use an already loaded identical superset.
            robots={robot.get('name',robot.find('./link').get('name')):ET.tostring(robot,encoding='unicode') for robot in tree.findall('./robot')}
            if robots and tree.find('./contact') is None and all(j.get('type')=='fixed' for j in tree.iter('joint')):
                common=ET.Element(tree.tag,tree.attrib)
                for child in tree:
                    if child.tag!='robot':
                        common.append(child)
                self._static_query_xml[hashlib.sha256(xml.encode()).hexdigest()] = (ET.tostring(common,encoding='unicode'),robots)
        entry = self.report["xml_factories"].setdefault(factory_name, {"calls": 0, "verified_sdf_body_count": 0, "example_xml_sha256": hashlib.sha256(xml.encode()).hexdigest()})
        entry["calls"] += 1
        entry["verified_sdf_body_count"] += sum(body.get("type") == "SDF" for body in bodies)
        entry["verified_bvh_body_count"] = entry.get("verified_bvh_body_count", 0) + len(bvh_names)
        thresholds = [float(body.get("col_th", ".01")) for body in bodies]
        if thresholds:
            entry["minimum_body_col_th_engine"] = min(entry.get("minimum_body_col_th_engine", min(thresholds)), min(thresholds))
            entry["maximum_body_col_th_engine"] = max(entry.get("maximum_body_col_th_engine", max(thresholds)), max(thresholds))
            if self.scale:
                entry["minimum_body_col_th_mm"] = entry["minimum_body_col_th_engine"] / self.scale
                entry["maximum_body_col_th_mm"] = entry["maximum_body_col_th_engine"] / self.scale
        return xml

    def bind_solid_caches(self, assembly_dir, parts, report):
        if report.get("status") != "passed" or set(report.get("bodies", {})) != set(parts):
            raise BridgeError("SDF_PROOF_INCOMPLETE", "모든 배치의 원본 솔리드 SDF 검증이 필요합니다.")
        for part in parts:
            obj = (Path(assembly_dir) / (part + ".obj")).resolve()
            cache = obj.with_suffix(".sdf")
            body = report["bodies"][part]
            if body.get("native_collision_type") == "BVH":
                if (body.get("status") != "passed" or body.get("original_solid_count") != 1
                        or body.get("obj_sha256") != sha256(obj)
                        or body.get("native_reload_proof", {}).get("status") != "passed"):
                    raise BridgeError("BVH_PROOF_INCOMPLETE", "단일 원본 솔리드의 실제 BVH 형상 및 점유 검증이 필요합니다.")
                stat = obj.stat()
                self._solid_bvh[str(obj)] = {"sha256": body["obj_sha256"], "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
                continue
            expected = body.get("composed_cache_sha256", body.get("original_cache_sha256"))
            if body.get("status") != "passed" or not cache.is_file() or sha256(cache) != expected:
                raise BridgeError("SDF_CACHE_CHANGED", "원본 솔리드 SDF 캐시의 검증 해시가 다릅니다.")
            stat = cache.stat()
            self._solid_caches[str(obj)] = {"sha256": expected, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
        self.report["original_solid_cache_contract"] = {"all_xml_factories": ["contact", "path", "stability", "recorded_path_export"],
            "load_sdf": True, "save_sdf": False, "verified_cache_count": len(self._solid_caches),
            "verified_original_triangle_bvh_count": len(self._solid_bvh),
            "bvh_selection": "single_original_solid_only_after_strict_SDF_occupancy_mismatch_and_native_BVH_proof",
            "bvh_force_defaults": "cloned_from_unchanged_general_SDF_contact_per_factory",
            "cache_parameters": {"sdf_dx": self.parameters["sdf_dx"], "sdf_res": self.parameters["sdf_res"]},
            "cross_run_or_profile_reuse": False}

    def record_offsets(self, part_move, values, baseline):
        audit = self.report["initial_overlap_compensation"]
        audit["candidate_count"] += 1
        for part_fix, col_th in values.items():
            if part_fix == part_move:
                continue
            col_th = float(col_th)
            if not math.isfinite(col_th):
                raise BridgeError("PHYSICS_NONFINITE", "초기 SDF 중첩 보정 값이 유한하지 않습니다.")
            offset = max(0.0, col_th - baseline)
            key = (str(part_move), str(part_fix))
            previous = self._pair_offsets.get(key)
            if previous is None or previous["offset_engine"] < offset:
                self._pair_offsets[key] = {"moving_part": key[0], "fixed_part": key[1], "offset_engine": offset, "col_th_engine": col_th,
                                           "offset_mm": offset / self.scale if self.scale else None, "col_th_mm": col_th / self.scale if self.scale else None}
            audit["maximum_offset_engine"] = max(audit["maximum_offset_engine"], offset)
            audit["maximum_col_th_engine"] = max(audit["maximum_col_th_engine"], col_th)
        audit["pairs"] = sorted(self._pair_offsets.values(), key=lambda item: (-item["offset_engine"], item["moving_part"], item["fixed_part"]))
        if self.scale:
            audit["maximum_offset_mm"] = audit["maximum_offset_engine"] / self.scale
            audit["maximum_col_th_mm"] = audit["maximum_col_th_engine"] / self.scale
        if self.audit_path:
            write_json(self.audit_path, self.report)

    def install(self):
        sim_string = importlib.import_module("plan_sequence.sim_string")
        physics = importlib.import_module("plan_sequence.physics_planner")
        feasibility = importlib.import_module("plan_sequence.feasibility_check")
        heuristic = importlib.import_module("plan_sequence.generator.heuristic")
        original_connect = physics.ConnectPathPlanner
        class DeferredConnectPathPlanner:
            """Keep the optional RRT connector available without eager SDF builds.

            Upstream rigid six-direction feasibility never calls this connector;
            upstream plan_path currently has its connector call commented out.
            If a caller does request a connector method, construct the exact
            original object with the exact original arguments on first access.
            """
            def __init__(self, *args, **kwargs):
                self._arguments = (args, kwargs)
                self._instance = None
            def __getattr__(self, name):
                if self._instance is None:
                    self._instance = original_connect(*self._arguments[0], **self._arguments[1])
                return getattr(self._instance, name)
        self._set(physics, 'ConnectPathPlanner', DeferredConnectPathPlanner)
        self.report['performance_adapters'] = {'optional_connect_planner': 'deferred_until_first_method_access',
            'connector_implementation_unchanged': True, 'collision_and_support_checks_skipped': False,
            'original_meshes_and_fields_changed': False}
        self.report["unchanged_upstream_parameters"] = {
            "force_mag": physics.FORCE_MAG, "single_direction_path_max_time_s": physics.MAX_TIME,
            "stability_position_far_engine": physics.POS_FAR_THRESHOLD,
            "stability_position_near_engine": physics.POS_NEAR_THRESHOLD,
            "stability_col_th_engine": physics.COL_TH_STABLE,
            "stability_position_far_mm": physics.POS_FAR_THRESHOLD / self.scale if self.scale else None,
            "stability_position_near_mm": physics.POS_NEAR_THRESHOLD / self.scale if self.scale else None,
            "force_and_gravity_not_rescaled": True,
        }
        for name in ["get_contact_sim_string", "get_path_sim_string", "get_stability_sim_string"]:
            original = getattr(sim_string, name)
            def factory(*args, _name=name, _original=original, **kwargs):
                # The default col_th is bound at upstream function definition.
                if self.profile is not None and _name == "get_path_sim_string" and len(args) < 8:
                    kwargs.setdefault("col_th", self.parameters["path_col_th"])
                if self.profile is not None and _name == "get_stability_sim_string" and len(args) < 8:
                    kwargs.setdefault("col_th", 0.0)
                return self.rewrite_xml(_name, _original, *args, **kwargs)
            self._set(sim_string, name, factory)
            self._set(physics, name, factory)
        if self.profile is not None:
            original_graph = physics.get_contact_graph
            def graph(*args, **kwargs):
                if len(args) < 4:
                    kwargs.setdefault("contact_eps", self.parameters["contact_eps"])
                return original_graph(*args, **kwargs)
            for owner in [physics, feasibility, heuristic]:
                self._set(owner, "get_contact_graph", graph)
            self._set(physics, "CONTACT_EPS", self.parameters["contact_eps"])
            self._set(physics, "COL_TH_PATH", self.parameters["path_col_th"])
            self._set(physics, "MIN_SEP", self.parameters["minimum_separation"])
            self._set(physics.MultiPartPathPlanner, "contact_eps", self.parameters["contact_eps"])
            self._set(physics.MultiPartPathPlanner, "col_th_base", self.parameters["path_col_th"])
            self._set(physics.MultiPartPathPlanner, "min_sep", self.parameters["minimum_separation"])
            self._set(physics.StabilityChecker, "contact_eps", self.parameters["contact_eps"])
        original_offsets = physics.MultiPartPathPlanner._compute_col_th_dict
        adapter = self
        def audited_offsets(planner, sim, parts_fix, part_move):
            values = original_offsets(planner, sim, parts_fix, part_move)
            adapter.record_offsets(part_move, values, planner.col_th_base)
            return values
        self._set(physics.MultiPartPathPlanner, "_compute_col_th_dict", audited_offsets)
        if self.audit_path:
            write_json(self.audit_path, self.report)
        return self

    def install_timing(self, deadline=None):
        """Observational timing only; native objects and outcomes are unchanged."""
        physics = importlib.import_module("plan_sequence.physics_planner")
        redmax = importlib.import_module('redmax_py')
        original_simulation = redmax.Simulation
        constructor_count = [0]
        static_scenes=[]
        adapter=self
        performance=self.report['performance_adapters']
        performance.update(static_query_scene_cache_hits=0,static_query_scope='only unchanged fixed-body no-contact metric XML; dynamic path/stability/export simulations always constructed')
        class StaticMetricView:
            def __init__(self,sim,names):
                self._sim,self._names=sim,names
            def _names_valid(self,values):
                if any(value not in self._names for value in values):
                    raise BridgeError('STATIC_QUERY_SCOPE','정적 지표 질의가 현재 입력 부품 범위를 벗어났습니다.')
            def get_body_mass(self,name):
                self._names_valid([name]);return self._sim.get_body_mass(name)
            def get_body_distance(self,a,b):
                self._names_valid([a,b]);return self._sim.get_body_distance(a,b)
            def body_in_contact(self,a,b,threshold):
                self._names_valid([a,b]);return self._sim.body_in_contact(a,b,threshold)
        def timed_simulation(*args, **kwargs):
            signature=adapter._static_query_xml.get(hashlib.sha256(args[0].encode()).hexdigest()) if args and isinstance(args[0],str) else None
            asset=str(args[1]) if len(args)>1 else str(kwargs.get('asset_folder',''))
            if signature is not None:
                common,robots=signature
                for cached_common,cached_robots,cached_asset,cached_sim in static_scenes:
                    if common==cached_common and asset==cached_asset and all(cached_robots.get(k)==v for k,v in robots.items()):
                        performance['static_query_scene_cache_hits']+=1
                        return StaticMetricView(cached_sim,set(robots))
            constructor_count[0] += 1
            started = time.monotonic()
            count = constructor_count[0]
            emit('simulation_started', '물리 시뮬레이터 구성 시작', constructor_count=count)
            sim = original_simulation(*args, **kwargs)
            if signature is not None:
                static_scenes.append((signature[0],signature[1],asset,sim))
            emit('simulation_ready', '물리 시뮬레이터 구성 완료', constructor_count=count,
                 elapsed_s=round(time.monotonic()-started, 3))
            return sim
        self._set(redmax, 'Simulation', timed_simulation)
        original_direction = physics.MultiPartPathPlanner.check_success
        def timed_direction(planner, action, *args, **kwargs):
            started = time.monotonic()
            if deadline is not None and started >= deadline:
                raise BridgeError('TIMEOUT','전체 제한시간에 도달해 이동 방향 검사를 중단했습니다.')
            values = [float(v) for v in action]
            emit('direction_started', '이동 방향 검사 시작', moving_part=planner.part_move, direction=values)
            original_limit=planner.max_time
            if deadline is not None:
                planner.max_time=min(original_limit,max(0.001,deadline-started))
            try:
                answer = original_direction(planner, action, *args, **kwargs)
            finally:
                planner.max_time=original_limit
            success = bool(answer[0] if isinstance(answer, tuple) else answer)
            emit('direction_completed', '이동 방향 검사 완료', moving_part=planner.part_move, direction=values,
                 elapsed_s=round(time.monotonic()-started, 3), success=success)
            return answer
        self._set(physics.MultiPartPathPlanner, 'check_success', timed_direction)
        base_module = importlib.import_module('plan_sequence.planner.base')
        original_feasibility=base_module.check_assemblable
        def first_valid_path(*args,**kwargs):
            # Sequence feasibility requires one valid removal path, not the
            # shortest of all six directions. Special optimization/render calls
            # retain the original implementation.
            if kwargs.get('optimize_path') or kwargs.get('render') or kwargs.get('debug',0)>0:
                return original_feasibility(*args,**kwargs)
            bound=inspect.signature(original_feasibility).bind(*args,**kwargs)
            bound.apply_defaults();values=bound.arguments
            planner=physics.MultiPartPathPlanner(values['asset_folder'],values['assembly_dir'],values['parts_fix'],
                values['part_move'],pose=values['pose'],save_sdf=values['save_sdf'])
            feasibility=importlib.import_module('plan_sequence.feasibility_check')
            for action in feasibility.get_R3_actions():
                success,path=planner.check_success(action,return_path=True,min_sep=values['min_sep'])
                if success:
                    import numpy as np
                    return (action,np.asarray(path)) if values['return_path'] else action
            return (None,None) if values['return_path'] else None
        self._set(base_module,'check_assemblable',first_valid_path)
        performance['path_selection']='first natively successful six-axis direction; no shortest-path guarantee'
        original_support = base_module.get_stable_plan_1pose_serial
        def timed_support(*args, **kwargs):
            started = time.monotonic()
            emit('support_started', '잔여 부품 지지 검사 시작', remaining_parts=len(args[2]))
            answer = original_support(*args, **kwargs)
            emit('support_completed', '잔여 부품 지지 검사 완료', elapsed_s=round(time.monotonic()-started, 3),
                 support_found=answer is not None)
            return answer
        self._set(base_module, 'get_stable_plan_1pose_serial', timed_support)
        return self

    def close(self):
        for owner, name, existed, original in reversed(self._restore):
            if existed:
                setattr(owner, name, original)
            else:
                delattr(owner, name)
        self._restore.clear()


def validate_sequence(parts, sequence, base_part=None):
    parts = list(parts)
    if len(parts) < 2 or len(set(parts)) != len(parts):
        raise BridgeError("SEQUENCE_INVALID", "분리 단위 수 또는 ID가 올바르지 않습니다.")
    if not isinstance(sequence, (list, tuple)) or len(sequence) != len(parts) - 1:
        raise BridgeError("SEQUENCE_INVALID", "분해 순서는 정확히 N−1개의 행동이어야 합니다.")
    if len(set(sequence)) != len(sequence) or not set(sequence).issubset(set(parts)):
        raise BridgeError("SEQUENCE_INVALID", "분해 순서에 중복되거나 알 수 없는 부품이 있습니다.")
    remaining = set(parts) - set(sequence)
    if len(remaining) != 1:
        raise BridgeError("SEQUENCE_INVALID", "마지막 base 부품을 결정할 수 없습니다.")
    base = next(iter(remaining))
    if base_part is not None and base != base_part:
        raise BridgeError("SEQUENCE_INVALID", "사용자가 고정한 base 부품이 이동되었습니다.")
    return base, [base] + list(reversed(sequence))


def validated_edges(tree, parts, sequence):
    """Reject a library-reported success unless every state transition is real."""
    rest = sorted(parts)
    result = []
    for moving in sequence:
        after = [part for part in rest if part != moving]
        if not tree.has_edge(tuple(rest), tuple(after)):
            raise BridgeError("SEQUENCE_INVALID", "탐색 트리에 순서의 상태 전이가 없습니다.")
        info = tree.edges[tuple(rest), tuple(after)]["sim_info"]
        if not info.get("feasible") or info.get("part_move") != moving:
            raise BridgeError("SEQUENCE_INVALID", "성공하지 않은 물리 검사 행동이 순서에 있습니다.")
        fixed = info.get("parts_fix")
        if fixed is None or len(set(fixed)) != len(fixed) or not set(fixed).issubset(after):
            raise BridgeError("SEQUENCE_INVALID", "지지 부품 집합이 상태와 맞지 않습니다.")
        result.append((rest, after, info))
        rest = after
    if len(rest) != 1 or tree.nodes[tuple(rest)].get("n_gripper") is None:
        raise BridgeError("SEQUENCE_INVALID", "유효한 전체 순서의 leaf가 없습니다.")
    return result


def find_feasible_branch(tree, parts, allow_partial=False):
    """Read only genuinely feasible edges, including trees with merged parents.

    Upstream find_sequence does not test feasibility of a chosen incoming edge.
    A forward traversal also avoids its possible non-progressing backward loop.
    Part count strictly decreases on each accepted transition.
    """
    from collections import deque
    root = tuple(sorted(parts))
    if not tree.has_node(root) or tree.nodes[root].get("n_gripper") is None:
        return None
    queue = deque([(root, [])])
    visited = {root}
    best = (root, [])
    while queue:
        node, sequence = queue.popleft()
        if len(node) < len(best[0]):
            best = (node, sequence)
        if len(node) == 1:
            return sequence, list(node)
        for child in tree.successors(node):
            info = tree.edges[node, child].get("sim_info", {})
            removed = set(node) - set(child)
            if (not info.get("feasible") or tree.nodes[child].get("n_gripper") is None
                    or len(child) != len(node) - 1 or len(removed) != 1
                    or set(child) - set(node) or info.get("part_move") not in removed):
                continue
            fixed = info.get("parts_fix")
            if fixed is None or len(set(fixed)) != len(fixed) or not set(fixed).issubset(child):
                continue
            if child not in visited:
                visited.add(child)
                queue.append((child, sequence + [info["part_move"]]))
    if allow_partial and best[1]:
        return best[1], list(best[0])
    return None


def make_offline_generator(root, assembly_dir, settings, engine):
    import torch
    import numpy as np
    random.seed(settings["seed"])
    np.random.seed(settings["seed"])
    torch.manual_seed(settings["seed"])
    torch.set_num_threads(1)
    from plan_sequence.generator import generators
    if settings["strategy"] == "heur-out":
        return generators["heur-out"](str(root / "assets"), str(assembly_dir), base_part=settings["base_part"], save_sdf=True), None
    # The official subclass's default initializer downloads missing weights.
    # Only this I/O initializer is replaced; features, GNN and ranking remain official.
    model, digest = load_checkpoint(engine.get("weights_path"), engine.get("weights_sha256"))
    from plan_sequence.generator.learning import LearningBasedGenerator
    from plan_sequence.generator.base import Generator
    from plan_sequence.generator.network.data import AssemblyDataGraph
    from plan_sequence.generator.network.args import get_args
    from plan_sequence.feasibility_check import get_contact_graph
    import trimesh
    class OfflineLearningGenerator(LearningBasedGenerator):
        def __init__(self):
            Generator.__init__(self, str(root / "assets"), str(assembly_dir), settings["base_part"], True)
            contact_graph = get_contact_graph(self.asset_folder, self.assembly_dir, save_sdf=True)
            original_sample = trimesh.sample.sample_surface
            # New trimesh versions use an independent default_rng when seed=None,
            # bypassing np.random.seed. Keep the official surface sampler and
            # seed its per-part calls from the run's deterministic numpy stream.
            supports_seed = "seed" in inspect.signature(original_sample).parameters
            def seeded_sample(*sample_args, **sample_kwargs):
                if sample_kwargs.get("seed") is None:
                    sample_kwargs["seed"] = int(np.random.randint(0, 2**31 - 1))
                return original_sample(*sample_args, **sample_kwargs)
            if supports_seed:
                trimesh.sample.sample_surface = seeded_sample
            try:
                self.nx_graph = AssemblyDataGraph.load_assembly_graph(self.assembly_dir, max_pc_size=get_args().max_pc_size, contact_graph=contact_graph)
            finally:
                trimesh.sample.sample_surface = original_sample
            self.bridge_pointcloud_sha256 = hashlib.sha256(b"".join(self.nx_graph.nodes[node]["x"].numpy().tobytes() for node in sorted(self.nx_graph.nodes))).hexdigest()
            self.model = model
    return OfflineLearningGenerator(), digest


def make_recording_planner(generator, deadline):
    """Save original successful feasibility samples before ASAP discards them."""
    import numpy as np
    import plan_sequence.planner.base as base_module
    from plan_sequence.planner.dfs import DFSSequencePlanner
    official_check = base_module.check_assemblable
    fixture_pose = None
    if generator.base_part is not None:
        # CAD is centered/scaled for the engine. Fixing a base must not put the
        # lower half of that unchanged CAD below the simulator's ground plane.
        # A common vertical translation preserves every relative placement.
        from assets.load import load_assembly
        assembly = load_assembly(generator.assembly_dir)
        fixture_pose = np.eye(4)
        fixture_pose[2,3] = max(0., -min(float(value['mesh'].bounds[0,2]) for value in assembly.values()))
        emit('fixed_base_workspace','고정 기준의 공통 바닥 배치를 설정했습니다.',base_part=generator.base_part,
             pose=matrix_list(fixture_pose),relative_placements_changed=False)
    class RecordingDFSPlanner(DFSSequencePlanner):
        bridge_physics_evaluations = 0
        def get_stats(self, tree):
            original_success, n_eval, n_gripper = base_module.SequencePlanner.check_success(tree)
            found = find_feasible_branch(tree, self.parts)
            return {"success": found is not None, "n_eval": n_eval if found else None,
                    "n_gripper": n_gripper if found else None, "sequence": found[0] if found else None,
                    "official_leaf_success": original_success}
        def _simulate(self, *args, **kwargs):
            if time.monotonic() >= deadline:
                raise BridgeError("TIMEOUT", "전체 실행 제한시간을 초과했습니다.")
            self.bridge_physics_evaluations += 1
            if fixture_pose is not None:
                values=list(args)
                if len(values)>3 and values[3] is None:
                    values[3]=fixture_pose.copy()
                    args=tuple(values)
                elif len(values)<=3 and kwargs.get('pose') is None:
                    kwargs['pose']=fixture_pose.copy()
            captured = {}
            evaluation_started = time.monotonic()
            moving_part = args[0] if args else kwargs.get('part_move')
            emit('candidate_started', '물리 후보 평가 시작', evaluation_count=self.bridge_physics_evaluations,
                 moving_part=moving_part, remaining_s=round(deadline-time.monotonic(), 3))
            def capture(*check_args, **check_kwargs):
                path_started = time.monotonic()
                emit('path_started', '부품 이동 가능성 검사 시작', moving_part=moving_part)
                answer = official_check(*check_args, **check_kwargs)
                emit('path_completed', '부품 이동 가능성 검사 완료', moving_part=moving_part,
                     elapsed_s=round(time.monotonic()-path_started, 3), path_found=answer[0] is not None)
                if isinstance(answer, tuple) and answer[0] is not None and answer[1] is not None:
                    captured["q_path"] = np.asarray(answer[1]).copy()
                return answer
            base_module.check_assemblable = capture
            try:
                info = super()._simulate(*args, **kwargs)
            finally:
                base_module.check_assemblable = official_check
            if info["feasible"] and "q_path" in captured:
                info["bridge_q_path"] = captured["q_path"]
                info["bridge_path_source"] = "recorded_feasibility"
            emit("planning", "물리 탐색 평가", evaluation_count=self.bridge_physics_evaluations, moving_part=info["part_move"], feasible=bool(info["feasible"]), elapsed_s=round(time.monotonic()-evaluation_started,3),
                 path_found=info.get('action') is not None, support_found=info.get('parts_fix') is not None)
            return info
        def _select_node(self, tree):
            # Official DFS counts the excluded base as a candidate and can stall
            # on a fully evaluated unsuccessful fixed-base state. Count real candidates.
            while self.G_path:
                current = self.G_path[-1]
                candidate_count = len(current) - int(self.base_part in current)
                if tree.out_degree(tuple(current)) < candidate_count:
                    return current
                self.G_path.pop()
            raise BridgeError("SEARCH_EXHAUSTED", "탐색 가능한 상태를 모두 평가했습니다.")
    return RecordingDFSPlanner(generator, num_proc=1, save_sdf=True)


def matrix_list(value):
    import numpy as np
    matrix = np.asarray(value, dtype=float).reshape(4, 4)
    if not np.isfinite(matrix).all() or not np.allclose(matrix[3], [0, 0, 0, 1], atol=1e-7):
        raise BridgeError("PATH_EXPORT_FAILED", "경로의 변환 행렬이 유효하지 않습니다.")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation.T @ rotation, np.eye(3), atol=1e-5) or not np.isclose(np.linalg.det(rotation), 1, atol=1e-5):
        raise BridgeError("PATH_EXPORT_FAILED", "경로 회전 행렬이 강체 변환이 아닙니다.")
    return matrix.reshape(-1).tolist()


def baked_delta(current, initial, global_pose):
    """Map an assembled OBJ vertex into the current engine world frame."""
    import numpy as np
    return np.asarray(current) @ np.linalg.inv(np.asarray(initial)) @ np.asarray(global_pose)


def validate_obj_meshes(assembly_dir, parts):
    import numpy as np
    import trimesh
    for part in parts:
        try:
            mesh = trimesh.load_mesh(str(assembly_dir / (part + ".obj")), process=False, maintain_order=True)
            if not isinstance(mesh, trimesh.Trimesh) or not len(mesh.vertices) or not len(mesh.faces):
                raise ValueError("empty or non-triangle OBJ")
            if not np.isfinite(mesh.vertices).all() or not mesh.is_watertight or not mesh.is_winding_consistent or mesh.volume <= 0:
                raise ValueError("open, nonfinite, reversed or inconsistent mesh")
        except Exception as exc:
            raise BridgeError("MESH_INVALID", "OBJ " + part + "를 물리 입력으로 사용할 수 없습니다: " + str(exc), "STEP→OBJ 품질 진단에서 해당 배치를 확인하세요.") from exc


def stage_native_input(root, assembly_dir, output_dir, inputs, engine, physics_profile=None, native_provenance=None):
    """TinyObj/RedMax Windows file APIs require an ASCII physics workspace."""
    if platform.system() != "Windows" and physics_profile is None and "solids_manifest.json" not in inputs:
        return assembly_dir
    staging_root = Path(engine.get("workspace_root") or (root / ".test-runs" if platform.system() == "Windows" else output_dir / "physics-cache")).resolve()
    if platform.system() == "Windows" and (not str(staging_root).isascii() or any(char in str(staging_root) for char in '\"&<>')):
        raise BridgeError("ENGINE_WORKSPACE_INVALID", "네이티브 물리 작업 폴더는 ASCII 경로여야 합니다.", "engine.workspace_root에 영문 경로의 쓰기 가능한 작업 폴더를 지정하세요.")
    key = hashlib.sha256((str(output_dir) + json.dumps(inputs, sort_keys=True) + json.dumps(physics_profile, sort_keys=True)).encode()).hexdigest()[:24]
    staged = staging_root / key / "input_geometry"
    staged.mkdir(parents=True, exist_ok=True)
    for filename, digest in inputs.items():
        target = staged / filename
        if not target.resolve().is_relative_to(staged.resolve()):
            raise BridgeError("INVALID_INPUT", "입력 경로가 네이티브 작업 폴더 밖입니다.")
        target.parent.mkdir(parents=True, exist_ok=True)
        # An interrupted previous run can leave a partial SDF. No stale cache is
        # loaded across invocations; caches remain reusable within one run.
        cached = target.with_suffix(".sdf")
        if target.suffix == ".obj" and cached.is_file():
            cached.unlink()
        shutil.copyfile(str(assembly_dir / filename), str(target))
        if sha256(target) != digest:
            raise BridgeError("INPUT_CHANGED", "실행 준비 중 OBJ 입력이 바뀌었습니다.", "변환을 마친 뒤 새 실행을 생성하세요.")
    write_json(staged.parent / "input_manifest.json", {"original_input_dir": str(assembly_dir), "input_files_sha256": inputs, "output_dir": str(output_dir), "physics_profile": physics_profile,
        "sdf_cache_scope": "fresh_or_explicitly_hash_verified_successful_previous_fields; every_current_native_source_probe_revalidated"})
    if native_provenance is not None:
        path = Path(__file__).with_name("cache_reuse.py")
        specification = importlib.util.spec_from_file_location("asap_test_cache_reuse", path)
        module = importlib.util.module_from_spec(specification)
        specification.loader.exec_module(module)
        previous = sorted(staging_root.glob("*/input_manifest.json"))
        module.reuse_verified_caches(staged, native_provenance, previous,
            audit_output=Path(output_dir) / "cache_reuse.json")
    return staged


def export_recorded_path(root, assembly_dir, before, after, info, deadline):
    """Use RedMax's own OBJ joint matrices without a viewer or a physics rerun.

    XML uses OBJ_TO_JOINT. E0j is thus the OBJ frame even when the simulator's
    inertial body frame is translated to the centroid. Delta=P(t) inv(P_start) G
    acts on already assembled/baked geometry; G is the action's global pose.
    """
    import numpy as np
    import redmax_py as redmax
    from plan_sequence.sim_string import get_path_sim_string, GROUND_Z
    from plan_sequence.physics_planner import MIN_SEP
    pose = np.eye(4) if info["pose"] is None else np.asarray(info["pose"], dtype=float)
    global_pose = pose.copy()
    global_pose[2, 3] += GROUND_Z
    raw_path = info.get("bridge_q_path")
    if raw_path is None or len(raw_path) < 2:
        raise BridgeError("PATH_EXPORT_FAILED", "성공 행동의 실제 경로 기록이 없습니다.")
    xml = get_path_sim_string(str(assembly_dir), after, info["part_move"], pose=pose, save_sdf=True)
    sim = redmax.Simulation(xml, str(root / "assets"))
    moving_body = "part" + info["part_move"]
    sim.reset()
    initial = np.asarray(sim.get_body_E0j(moving_body)).copy()
    frames = []
    for index, state in enumerate(raw_path):
        if index % 100 == 0 and time.monotonic() >= deadline:
            raise BridgeError("TIMEOUT", "경로 출력 중 전체 실행 제한시간을 초과했습니다.")
        sim.set_joint_qm(moving_body, np.asarray(state))
        sim.update_robot()
        frame = baked_delta(sim.get_body_E0j(moving_body), initial, global_pose)
        frames.append(matrix_list(frame))
    if not np.allclose(np.asarray(frames[0]).reshape(4, 4), global_pose, atol=1e-5):
        raise BridgeError("TRANSFORM_MISMATCH", "실제 경로 시작과 완성 배치가 일치하지 않습니다.")
    # Reconstructing the last recorded state must preserve the original physics
    # success condition. This catches frame/state conversion defects without
    # applying new forces or producing a replacement trajectory.
    if any(sim.body_in_contact("part" + fixed, moving_body, MIN_SEP) for fixed in after):
        raise BridgeError("PATH_EXPORT_FAILED", "복원한 실제 경로 끝 상태가 분리 조건을 만족하지 않습니다.")
    return matrix_list(global_pose), frames


def failure_result(code="ENGINE_ERROR", message="", action=""):
    return {"schema_version": SCHEMA_VERSION, "sequence_status": "failed", "path_status": "unavailable", "reason_code": code, "message": message, "action": action, "disassembly": [], "base_part": None, "assembly_order": [], "steps": [], "stats": {}, "provenance": {}}


def run_request(request, output_path):
    started = time.monotonic()
    physics_adapter = None
    result = failure_result("RUNNING", "엔진을 준비하고 있습니다.")
    write_json(output_path, result)
    try:
        # WSL cancellation may signal only this dedicated setsid group. The
        # caller verifies the request nonce; no distribution-wide shutdown.
        process_dir = Path(request["output_dir"]).resolve()
        process_dir.mkdir(parents=True, exist_ok=True)
        write_json(process_dir / "process.json", {"pid": os.getpid(), "pgid": os.getpgrp() if hasattr(os, "getpgrp") else None, "created_at": datetime.now(timezone.utc).isoformat(), "request_hash": hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest(), "nonce": request.get("nonce"), "request_nonce": request.get("nonce")})
        settings = validate_settings(request.get("settings", {}))
        deadline = started + settings["timeout_s"]
        engine = config_engine(request["engine"])
        if settings["strategy"] == "learn" and (not engine.get("weights_path") or not Path(engine["weights_path"]).is_file()):
            raise BridgeError("MODEL_MISSING", "ASAP 사전학습 가중치가 없습니다.", "가중치를 설정하거나 heur-out 전략을 선택하세요.")
        root = Path(engine.get("asap_root", "")).resolve()
        provenance = source_provenance(root)
        prepare_engine_path(root)
        # Import the separately built native extension before numerical packages
        # can preload competing Windows runtime libraries.
        import redmax_py as redmax
        provenance.update(native_guard_provenance(root, redmax.__file__))
        importlib.import_module("plan_sequence.sim_string")
        emit("native_loaded", "네이티브 엔진을 로드했습니다.", binary=str(redmax.__file__), sha256=sha256(redmax.__file__))
        provenance.update({"bridge_sha256": sha256(__file__), "python": sys.version.split()[0], "platform": platform.platform(), "machine_locale": locale.setlocale(locale.LC_ALL), "adapters": ["offline_weight_loader", "deterministic_surface_sampling_if_supported", "record_feasibility_path", "fixed_base_dfs_candidate_count", "headless_redmax_obj_frame_export", "process_C_locale_for_XML_and_SDF", "deferred_optional_RRT_connect_planner"]})
        provenance["adapters"].append("verified_feasible_tree_path_selection")
        build_manifest_path = Path(engine.get("build_manifest_path") or root.parent / "build_manifest.json")
        if build_manifest_path.is_file():
            provenance["native_build_manifest_sha256"] = sha256(build_manifest_path)
            provenance["native_build_manifest"] = read_json(build_manifest_path)
        assembly_dir = Path(request["assembly_dir"]).resolve()
        output_dir = Path(request["output_dir"]).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        parts = sorted(path.stem for path in assembly_dir.glob("*.obj") if path.is_file())
        if len(parts) < 2:
            raise BridgeError("NO_SEPARABLE_UNITS", "ASAP에 필요한 분리된 OBJ가 두 개 이상 있어야 합니다.")
        if any(not re.fullmatch(r"[0-9]+", part) for part in parts):
            raise BridgeError("INVALID_INPUT", "엔진 OBJ 파일명은 배치와 매핑된 숫자 ID여야 합니다.")
        if settings["base_part"] is not None and settings["base_part"] not in parts:
            raise BridgeError("INVALID_SETTINGS", "고정 base ID가 OBJ 입력에 없습니다.")
        inputs = {part + ".obj": sha256(assembly_dir / (part + ".obj")) for part in parts}
        if (assembly_dir / "config.json").is_file():
            inputs["config.json"] = sha256(assembly_dir / "config.json")
        required_capabilities = request.get("required_engine_capabilities", [])
        if not isinstance(required_capabilities, list) or any(value != "solid_or_sdf_v1" for value in required_capabilities):
            raise BridgeError("ENGINE_CAPABILITY_MISSING", "이 엔진에서 검증되지 않은 입력 기능이 필요합니다.")
        has_solid_manifest = (assembly_dir / "solids_manifest.json").is_file()
        if required_capabilities and not has_solid_manifest:
            raise BridgeError("SOLID_MANIFEST_MISSING", "원본 솔리드 보존 입력에 필요한 manifest가 없습니다.")
        if has_solid_manifest:
            if not request.get("input_revision") or not request.get("source_sha256"):
                raise BridgeError("SOLID_MANIFEST_INVALID", "실행 요청에 변환 revision과 원본 CAD 해시가 필요합니다.")
            inputs["solids_manifest.json"] = sha256(assembly_dir / "solids_manifest.json")
            for component in assembly_dir.joinpath("obj_solids").rglob("*.obj"):
                if not component.resolve().is_relative_to(assembly_dir):
                    raise BridgeError("INVALID_INPUT", "원본 솔리드 파일이 입력 폴더 밖입니다.")
                inputs[component.relative_to(assembly_dir).as_posix()] = sha256(component)
        provenance.update({"input_files_sha256": inputs, "settings_sha256": hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest()})
        original_assembly_dir = assembly_dir
        assembly_dir = stage_native_input(root, assembly_dir, output_dir, inputs, engine, settings["physics_profile"], native_provenance=provenance)
        cache_reuse_path = output_dir / "cache_reuse.json"
        cache_reuse = read_json(cache_reuse_path) if cache_reuse_path.is_file() else None
        if cache_reuse is not None:
            provenance["verified_native_field_cache_reuse"] = cache_reuse
        provenance["native_ascii_staging"] = str(assembly_dir) if assembly_dir != original_assembly_dir else None
        result["provenance"] = provenance
        result["settings"] = settings
        write_json(output_path, result)
        emit("validating", "모든 OBJ의 닫힌 형상과 방향을 확인합니다.")
        validate_obj_meshes(assembly_dir, parts)
        physics_adapter = PhysicsParameterAdapter(settings["physics_profile"], request.get("coordinate_map"), output_dir / "physics_parameters.json")
        physics_adapter.report.update(native_core_unchanged=provenance["native_core_unchanged"],
            native_source_patches=provenance["native_source_patches"],
            source_equations_changed=not provenance["native_core_unchanged"],
            force_values_unchanged=True, physical_parameters_changed_by_native_guard=False)
        physics_adapter.install()
        provenance["physics_parameters"] = physics_adapter.report
        if settings["physics_profile"] is not None:
            provenance["adapters"].append("explicit_mm_runtime_physics_parameters")
        provenance["adapters"].append("audited_upstream_initial_SDF_overlap_compensation")
        if has_solid_manifest:
            adapter = solid_adapter()
            provenance["solid_sdf_adapter_sha256"] = sha256(Path(__file__).with_name("solid_sdf.py"))
            try:
                proof = adapter.native_smoke_test(assembly_dir.parent, root / "assets")
                provenance["solid_sdf_native_capability_proof"] = proof
                worker_spec = importlib.util.spec_from_file_location("asap_preprocess_runner", Path(__file__).with_name("preprocess_runner.py"))
                workers = importlib.util.module_from_spec(worker_spec); worker_spec.loader.exec_module(workers)
                solid_report = workers.prepare_isolated(adapter, assembly_dir, root / "assets", physics_adapter.parameters,
                    deadline, output_dir / "solid_sdf_audit.json", emit,
                    request["input_revision"], request["source_sha256"], physics_adapter.scale, provenance, root,
                    verified_cache_reuse=cache_reuse)
                physics_adapter.bind_solid_caches(assembly_dir, parts, solid_report)
                provenance["original_solid_sdf"] = solid_report
                provenance["adapters"].append("original_solid_occupied_OR_in_single_native_body_SDF_grid")
                if physics_adapter._solid_bvh:
                    provenance["adapters"].append("native_original_triangle_BVH_for_single_solid_SDF_occupancy_failures")
                write_json(output_path, result)
            except adapter.SolidSDFError as exc:
                partial_audit = output_dir / "solid_sdf_audit.json"
                if partial_audit.is_file():
                    audit = read_json(partial_audit)
                    audit.update(status="failed", reason_code=exc.reason_code, message=str(exc))
                    write_json(partial_audit, audit)
                    provenance["original_solid_sdf"] = audit
                raise BridgeError(exc.reason_code, str(exc), "원본 솔리드 manifest, 형상 보존 증명 및 실제 SDF 점유 검증을 확인하세요.") from exc
        physics_adapter.install_timing(deadline)
        emit("initializing", "공식 ASAP 생성기와 시뮬레이터를 초기화합니다.")
        initialized = time.monotonic()
        generator, model_hash = make_offline_generator(root, assembly_dir, settings, engine)
        for package_name in ["numpy", "scipy", "torch", "torch_geometric", "networkx", "trimesh", "redmax_py"]:
            package = importlib.import_module(package_name)
            provenance[package_name] = str(getattr(package, "__version__", "native"))
            if package_name == "redmax_py" and getattr(package, "__file__", None):
                provenance["redmax_binary_sha256"] = sha256(package.__file__)
        if model_hash:
            provenance["weights_sha256"] = model_hash
            provenance["sampled_pointcloud_sha256"] = generator.bridge_pointcloud_sha256
        planner = make_recording_planner(generator, deadline)
        planner.seed(settings["seed"])
        remaining_time = deadline - time.monotonic()
        if remaining_time <= 0:
            raise BridgeError("TIMEOUT", "초기화 중 전체 실행 제한시간을 초과했습니다.")
        setup = {"budget": settings["budget"], "max_grippers": settings["max_gripper"], "max_poses": settings["max_pose"], "pose_reuse": settings["pose_reuse"], "early_term": True, "timeout": remaining_time, "plan_grasp": False, "plan_arm": False}
        log_dir = output_dir / "raw_log"
        run_config_path = output_dir / "run_config.json"
        run_config = read_json(run_config_path) if run_config_path.is_file() else {}
        run_config.update({"settings": settings, "setup": setup, "provenance": provenance, "coordinate_map": request.get("coordinate_map"), "part_map": request.get("part_map")})
        write_json(run_config_path, run_config)
        planning_start = time.monotonic()
        tree = planner.plan(**setup, debug=1, render=False, log_dir=str(log_dir))
        stats = planner.get_stats(tree)
        planner.log(tree, stats, str(log_dir))
        write_json(log_dir / "setup.json", setup)
        stats["bridge_physics_evaluations"] = planner.bridge_physics_evaluations
        stats["budget_scope"] = "official_search_evaluations; terminal_two_part_expansion_is_not_counted_by_upstream_budget"
        stats["times_by_stage"] = {"initialization_s": planning_start - initialized, "planning_s": time.monotonic() - planning_start}
        result["stats"] = stats
        if not stats.get("success"):
            partial = find_feasible_branch(tree, parts, allow_partial=True)
            if partial:
                # Preserve exact valid transitions separately; these are never
                # promoted to a completed assembly order or base.
                partial_sequence, remaining = partial
                state = sorted(parts)
                partial_steps = []
                import numpy as np
                for index, moving in enumerate(partial_sequence):
                    after = [part for part in state if part != moving]
                    info = tree.edges[tuple(state), tuple(after)]["sim_info"]
                    pose = np.eye(4) if info["pose"] is None else info["pose"]
                    partial_steps.append({"index": index, "moving_part": moving, "fixed_parts": list(info["parts_fix"]), "pose": matrix_list(pose), "path": [], "path_status": "unavailable", "installed_before": state, "installed_after": after, "source": "feasible_tree_edge"})
                    state = after
                result["partial"] = {"status": "validated_partial_disassembly", "disassembly": partial_sequence, "remaining_parts": remaining, "steps": partial_steps, "message": "전체 조립순서가 아닌 유효한 부분 분해 상태입니다. 연속 경로는 출력하지 않았습니다."}
            stop = planner.stop_msg or "unknown"
            reason = {"budget reached": "BUDGET_REACHED", "timeout": "TIMEOUT", "exception": "ENGINE_ERROR", "interrupt": "CANCELED", "tree fully explored": "NO_FEASIBLE_SEQUENCE"}.get(stop, "NO_FEASIBLE_SEQUENCE")
            if time.monotonic() >= deadline:
                reason = "TIMEOUT"
            raise BridgeError(reason, "전체 순서를 찾지 못했습니다: " + stop, "입력 메시/간극과 지지·고정·예산 설정을 확인하세요.")
        sequence = list(stats["sequence"])
        base, order = validate_sequence(parts, sequence, settings["base_part"])
        edges = validated_edges(tree, parts, sequence)
        stats["selected_branch_n_eval"] = max(int(tree.edges[tuple(before), tuple(after)].get("n_eval", 0)) for before, after, _ in edges)
        stats["selected_branch_n_gripper"] = max(len(info["parts_fix"]) + 1 for _, _, info in edges)
        stats["n_eval_n_gripper_scope"] = "upstream_minimum_over_successful_leaves; selected_branch_values_are_reported_separately"
        result.update({"sequence_status": "success", "path_status": "unavailable", "reason_code": "PATH_EXTRACTING", "message": "전체 순서를 찾았습니다. 실제 경로를 출력합니다.", "disassembly": sequence, "base_part": base, "assembly_order": order, "settings": settings})
        # Step boundaries are useful even if path extraction is interrupted.
        import numpy as np
        for index, (before, after, info) in enumerate(edges):
            pose = np.eye(4) if info["pose"] is None else info["pose"]
            result["steps"].append({"index": index, "moving_part": info["part_move"], "fixed_parts": list(info["parts_fix"]), "pose": matrix_list(pose), "path": [], "installed_before": before, "installed_after": after, "path_status": "unavailable", "path_source": "recorded_feasibility", "frame_convention": "delta_on_baked_engine_world_row_major"})
        write_json(output_path, result)
        path_start = time.monotonic()
        path_errors = []
        for index, (before, after, info) in enumerate(edges):
            emit("path_extracting", "실제 분해 경로의 OBJ 프레임 행렬을 추출합니다.", step=index, steps=len(edges))
            try:
                pose, frames = export_recorded_path(root, assembly_dir, before, after, info, deadline)
                step = result["steps"][index]
                step.update({"pose": pose, "path": frames, "path_status": "success", "sample_count": len(frames)})
                write_json(output_dir / "paths" / (str(index) + ".json"), step)
            except BridgeError as exc:
                result["steps"][index].update({"path_status": "failed", "reason_code": exc.reason_code, "message": str(exc)})
                path_errors.append({"step": index, "reason_code": exc.reason_code, "message": str(exc)})
                if exc.reason_code == "TIMEOUT":
                    break
            except Exception as exc:
                result["steps"][index].update({"path_status": "failed", "reason_code": "PATH_EXPORT_FAILED", "message": str(exc)})
                path_errors.append({"step": index, "reason_code": "PATH_EXPORT_FAILED", "message": str(exc)})
                traceback.print_exc()
            write_json(output_path, result)
        stats["times_by_stage"]["path_export_s"] = time.monotonic() - path_start
        result["diagnostics"] = path_errors
        paths_ok = all(step["path_status"] == "success" for step in result["steps"])
        timed_out = any(error["reason_code"] == "TIMEOUT" for error in path_errors)
        result.update({"path_status": "success" if paths_ok else "failed", "reason_code": "SUCCESS" if paths_ok else "TIMEOUT" if timed_out else "PATH_EXPORT_FAILED", "message": "조립순서와 실제 이동 경로를 저장했습니다." if paths_ok else "조립순서는 생성했지만 일부 실제 경로 출력에 실패했습니다.", "checked_scope": ["official_ASAP_rigid_body_removal", "gravity_stability_and_support", "no_tool_torque_flexible_process_validation", "no_robot_grasp_or_arm_validation", "no_verified_continuous_assembly_reorientation_path"]})
    except BridgeError as exc:
        result.update({"reason_code": exc.reason_code, "message": str(exc), "action": exc.action})
        if result["sequence_status"] == "success":
            result["path_status"] = "failed"
        emit("failed", str(exc), reason_code=exc.reason_code)
    except Exception as exc:
        result.update({"reason_code": "ENGINE_DEPENDENCY_MISSING" if isinstance(exc, ModuleNotFoundError) else "ENGINE_ERROR", "message": str(exc), "action": "engine.log와 환경 점검 결과를 확인하세요."})
        if result["sequence_status"] == "success":
            result["path_status"] = "failed"
        traceback.print_exc()
    finally:
        if physics_adapter is not None:
            result.setdefault("provenance", {})["physics_parameters"] = physics_adapter.report
            audit = physics_adapter.report["initial_overlap_compensation"]
            if audit["maximum_offset_engine"] > 0:
                result.setdefault("diagnostics", []).append({"severity": "warning", "reason_code": "UPSTREAM_INITIAL_OVERLAP_COMPENSATION",
                    "message": "공식 ASAP가 초기 SDF 중첩을 보정한 허용값을 사용했습니다. 실제 CAD 무간섭 판정과 구분해야 합니다.",
                    "maximum_offset_engine": audit["maximum_offset_engine"], "maximum_offset_mm": audit["maximum_offset_mm"],
                    "maximum_col_th_engine": audit["maximum_col_th_engine"], "maximum_col_th_mm": audit["maximum_col_th_mm"]})
            result["physical_validation_scope"] = {"collision_model": "official_RedMax_vertex_to_original_mesh_BVH_and_original_solid_OR_SDF" if physics_adapter._solid_bvh else "official_ASAP_vertex_to_SDF", "initial_overlap_compensation_audited": True,
                "exact_CAD_nonpenetration_verified": False, "continuous_mesh_collision_verified": False,
                "sdf_resolution_is_an_approximation": True,
                "native_BVH_original_triangle_body_count": len(physics_adapter._solid_bvh)}
            try:
                write_json(Path(request["output_dir"]) / "physics_parameters.json", physics_adapter.report)
            finally:
                physics_adapter.close()
    result["stats"]["total_wall_time_s"] = round(time.monotonic() - started, 4)
    write_json(output_path, result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check")
    check.add_argument("--config", required=True)
    check.add_argument("--output", required=True)
    run = commands.add_parser("run")
    run.add_argument("--request", required=True)
    run.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "check":
        report = check_environment(read_json(args.config))
        write_json(args.output, report)
        emit("environment", "환경 점검 완료", ready=report["ready"], learn_ready=report["learn_ready"], heur_out_ready=report["heur_out_ready"])
        return 0
    result = run_request(read_json(args.request), args.output)
    return 0 if result["sequence_status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
