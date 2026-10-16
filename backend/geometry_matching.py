"""Bounded, bidirectional BREP comparison for proposed part correspondences.

Names, areas and volumes select candidates; they never prove a match. A
positive certificate requires two successful OCCT solid differences with no
remaining faces, a proper rigid transform, and conserved solid multiplicity.
Unknown native results remain review items. Display triangles are not used.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import multiprocessing as mp
import queue
import time
from pathlib import Path

import numpy as np
import OCP
from OCP.BRep import BRep_Builder
from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools
from OCP.GProp import GProp_GProps
from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS_Shape
from OCP.gp import gp_Trsf
try:
    from OCP.collections import List_TopoDS_Shape as ShapeList
except ImportError:
    from OCP.TopTools import TopTools_ListOfShape as ShapeList

VERSION = "brep-bidirectional-2.1.7"
POLICY = {"version": VERSION, "engine":f"OCCT/OCP {OCP.__version__}", "boolean_fuzzy_mm": 1e-7,
          "group_placement_tolerance_mm": 1e-4, "max_alignment_candidates": 26,
          "bbox_candidate_floor_mm": 1e-4,
          "bbox_candidate_basis": "Maximum of floor and sum of both shapes' declared CAD tolerances; candidate pruning only, never an acceptance criterion.",
          "positive_basis": "Both directed solid cuts contain no faces; preserved solid multiplicity; proper rigid alignment."}


def _count(shape, kind):
    it = TopExp_Explorer(shape, kind); result = 0
    while it.More():
        result += 1; it.Next()
    return result


def _matrix(location):
    tr = location.Transformation()
    return np.array([[tr.Value(r, c) for c in range(1, 5)] for r in range(1, 4)] + [[0, 0, 0, 1]], dtype=float)


def _location(matrix):
    tr = gp_Trsf(); tr.SetValues(*matrix[:3, :].reshape(-1).tolist())
    return TopLoc_Location(tr)


def _read(path):
    shape = TopoDS_Shape()
    if not BRepTools.Read_s(shape, str(path), BRep_Builder()) or shape.IsNull():
        raise ValueError("MATCH_BREP_READ_FAILED")
    return shape


def _properties(shape):
    props = GProp_GProps(); BRepGProp.VolumeProperties_s(shape, props)
    center = props.CentreOfMass()
    inertia = props.MatrixOfInertia()
    return {"volume_mm3": float(abs(props.Mass())),
            "center": np.array([center.X(), center.Y(), center.Z()]),
            "inertia": np.array([[inertia.Value(i, j) for j in range(1, 4)] for i in range(1, 4)])}


def _bbox(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib
    box = Bnd_Box(); BRepBndLib.AddOptimal_s(shape, box, False, False)
    low, high = box.CornerMin(), box.CornerMax()
    return np.array([[low.X(), low.Y(), low.Z()], [high.X(), high.Y(), high.Z()]])


def _bbox_candidate_tolerance(source, target):
    from .representation_repair import max_tolerances
    # Surface/edge extrema may differ within the precision explicitly carried
    # by the two imported BREP representations. Do not discard such candidates
    # before the actual material comparison. This never increases Boolean fuzzy
    # tolerance, nor does it certify a match by bounding boxes.
    return max(POLICY["bbox_candidate_floor_mm"],
               sum(max(max_tolerances(shape).values()) for shape in (source, target)))


def _alignments(source, target):
    # XDE local coordinate systems are tried first. Inertia axes are merely
    # alignment proposals; every proposal must pass the complete solid cuts.
    yield np.eye(4)
    sa, sb = _properties(source), _properties(target)
    _, ua = np.linalg.eigh(sa["inertia"]); _, ub = np.linalg.eigh(sb["inertia"])
    seen = {tuple(np.eye(4).reshape(-1))}
    translation = np.eye(4); translation[:3, 3] = sb["center"] - sa["center"]
    candidates = [translation]
    for permutation in itertools.permutations(range(3)):
        for signs in itertools.product((-1, 1), repeat=3):
            rotation = ub[:, permutation] @ np.diag(signs) @ ua.T
            if np.linalg.det(rotation) < 0.999999:
                continue
            matrix = np.eye(4); matrix[:3, :3] = rotation
            matrix[:3, 3] = sb["center"] - rotation @ sa["center"]
            candidates.append(matrix)
    for matrix in candidates:
        key = tuple(np.round(matrix.reshape(-1), 10))
        if key not in seen:
            seen.add(key); yield matrix


def _difference(source, target):
    arguments, tools = ShapeList(), ShapeList(); arguments.Append(source); tools.Append(target)
    operation = BRepAlgoAPI_Cut(); operation.SetArguments(arguments); operation.SetTools(tools)
    operation.SetNonDestructive(True); operation.SetFuzzyValue(POLICY["boolean_fuzzy_mm"])
    operation.Build()
    if not operation.IsDone():
        raise ValueError("MATCH_BOOLEAN_NOT_DONE")
    result = operation.Shape()
    if result.IsNull():
        raise ValueError("MATCH_BOOLEAN_NULL_RESULT")
    if not BRepCheck_Analyzer(result).IsValid():
        raise ValueError("MATCH_BOOLEAN_INVALID_RESULT")
    faces = _count(result, TopAbs_FACE)
    return {"remaining_faces": faces, "remaining_solids": _count(result, TopAbs_SOLID),
            "remaining_volume_mm3": _properties(result)["volume_mm3"] if faces else 0.0,
            "empty_material_boundary": faces == 0}


def _solid_differences(source, target, progress=None):
    """Compare the solid multiset without fusing overlapping CAD members.

    A valid imported compound may contain overlapping individual solids. A
    whole-compound Boolean can fail or erase this multiplicity, so each solid
    must have a unique geometrically equal counterpart under the same rigid
    placement. Candidate boxes only prune; both CAD cuts prove each pair.
    """
    def solids(shape):
        result=[]; explorer=TopExp_Explorer(shape,TopAbs_SOLID)
        while explorer.More():result.append(explorer.Current());explorer.Next()
        return result
    left,right=solids(source),solids(target);used=set(); pairs=[]
    for index,original in enumerate(left):
        source_bbox=_bbox(original)
        candidates=sorted((float(np.max(np.abs(source_bbox-_bbox(candidate)))),j,candidate)
            for j,candidate in enumerate(right) if j not in used)
        matched=None; failures=[]
        for bbox_error,j,candidate in candidates:
            bbox_candidate_tolerance = _bbox_candidate_tolerance(original, candidate)
            if bbox_error > bbox_candidate_tolerance:continue
            try:
                if progress:progress({"stage":"solid_difference","source_solid_index":index,"target_solid_index":j,"solid_count":len(left)})
                forward,reverse=_difference(original,candidate),_difference(candidate,original)
                attempt={"source_solid_index":index,"target_solid_index":j,
                         "bbox_error_mm":bbox_error,"bbox_candidate_tolerance_mm":bbox_candidate_tolerance,
                         "source_minus_target":forward,"target_minus_source":reverse}
                if forward["empty_material_boundary"] and reverse["empty_material_boundary"]:
                    matched=attempt;used.add(j);break
                failures.append(attempt)
            except Exception as exc:
                failures.append({"target_solid_index":j,"error":f"{type(exc).__name__}: {exc}"})
        if matched is None:
            return {"accepted":False,"verified_solid_pairs":pairs,"unverified_source_solid_index":index,"failures":failures}
        pairs.append(matched)
    empty={"remaining_faces":0,"remaining_solids":0,"remaining_volume_mm3":0.0,"empty_material_boundary":True}
    return {"accepted":len(pairs)==len(left)==len(right),"verified_solid_pairs":pairs,
            "source_minus_target":empty,"target_minus_source":dict(empty)}


def compare_breps(source_path, target_path, progress=None):
    """Run inside a terminable subprocess when called on supplied CAD files."""
    started = time.monotonic()
    source_world, target_world = _read(source_path), _read(target_path)
    from .representation_repair import max_tolerances
    input_tolerances=[max_tolerances(shape) for shape in (source_world,target_world)]
    resolution=max(POLICY["boolean_fuzzy_mm"],*(value for row in input_tolerances for value in row.values()))
    tolerance_evidence={"input_tolerances_mm":input_tolerances,"kernel_input_tolerance_mm":resolution,
        "scope":"Empty BREP differences are evaluated under these CAD kernel input tolerances; the fuzzy option alone is not an accuracy bound."}
    source_place, target_place = _matrix(source_world.Location()), _matrix(target_world.Location())
    source = source_world.Located(TopLoc_Location()); target = target_world.Located(TopLoc_Location())
    counts = [_count(shape, TopAbs_SOLID) for shape in (source, target)]
    if not all(counts) or counts[0] != counts[1]:
        return {"status": "DIFFERENT", "reason": "SOLID_MULTIPLICITY_DIFFERS", "solid_counts": counts}
    validity = [bool(BRepCheck_Analyzer(shape).IsValid()) for shape in (source, target)]
    if not all(validity):
        return {"status": "REVIEW", "reason": "MATCH_SOURCE_INVALID", "standard_valid": validity}
    source_props, target_props = _properties(source), _properties(target)
    volumes = [source_props["volume_mm3"], target_props["volume_mm3"]]
    # OCCT's default volume quadrature may vary between equivalent exported
    # surface parameterizations. This value is alignment evidence only; it
    # must not reject a correspondence or certify material equality.
    if min(volumes) <= 0:
        return {"status": "REVIEW", "reason": "NON_POSITIVE_SOLID_VOLUME", "volumes_mm3": volumes}
    target_bbox = _bbox(target); attempts = []; candidates = 0
    bbox_candidate_tolerance = max(POLICY["bbox_candidate_floor_mm"],
                                   sum(max(row.values()) for row in input_tolerances))
    tolerance_evidence["bbox_candidate_tolerance_mm"] = bbox_candidate_tolerance
    for alignment in _alignments(source, target):
        candidates += 1
        if candidates > POLICY["max_alignment_candidates"]:
            break
        world=target_place @ alignment @ np.linalg.inv(source_place)
        rotation=world[:3,:3]
        determinant=float(np.linalg.det(rotation))
        orthonormal_error=float(np.max(np.abs(rotation.T@rotation-np.eye(3))))
        if abs(determinant-1.0)>1e-8 or orthonormal_error>1e-8:
            # Local BREP equality cannot certify a reflected or scaled world
            # placement as a rigid part correspondence.
            attempts.append({"candidate":candidates,"reason":"NON_PROPER_RIGID_WORLD_TRANSFORM",
                "determinant":determinant,"orthonormal_error":orthonormal_error})
            continue
        aligned = source.Moved(_location(alignment))
        bbox_error = float(np.max(np.abs(_bbox(aligned) - target_bbox)))
        if bbox_error > bbox_candidate_tolerance:
            continue
        if progress:progress({"stage":"alignment_candidate","candidate":candidates,"bbox_error_mm":bbox_error})
        comparison=_solid_differences(aligned,target,progress)
        attempt = {"candidate": candidates, "bbox_error_mm": bbox_error,
                   **comparison}
        attempts.append(attempt)
        if progress:progress({"stage":"alignment_result","candidate":candidates,"accepted":comparison["accepted"],
            "unverified_source_solid_index":comparison.get("unverified_source_solid_index"),"failures":comparison.get("failures")})
        if comparison["accepted"]:
            return {"status": "PASS", "method": "bidirectional_brep_empty_difference",
                    "solid_counts": counts, "volumes_mm3": volumes, "comparison": attempt,
                    "proper_rotation_determinant":determinant,"orthonormal_error":orthonormal_error,
                    "source_to_target_world": world.reshape(-1).tolist(),
                    "elapsed_seconds": round(time.monotonic() - started, 3),**tolerance_evidence, "policy": POLICY}
    return {"status": "REVIEW", "reason": "NO_PROVEN_RIGID_SHAPE_MATCH", "solid_counts": counts,
            "volumes_mm3": volumes, "alignment_candidates": candidates,
            "attempts": attempts, "elapsed_seconds": round(time.monotonic() - started, 3),**tolerance_evidence, "policy": POLICY}


def _worker(source_path, target_path, output):
    try:
        output.put(compare_breps(source_path, target_path,lambda value:output.put({"__progress__":value})))
    except BaseException as exc:
        output.put({"status": "REVIEW", "reason": "MATCH_NATIVE_FAILURE", "detail": f"{type(exc).__name__}: {exc}"})


class ShapeVerifier:
    def __init__(self, cache_dir, progress=None, cancel=None, timeout_seconds=90):
        self.directory = Path(cache_dir) / VERSION; self.directory.mkdir(parents=True, exist_ok=True)
        self.progress, self.cancel, self.timeout_seconds = progress, cancel, timeout_seconds
        self.results = {}; self.file_hashes = {}

    def compare(self, source, target):
        paths = [Path(row["brep_path"]).resolve() for row in (source, target)]
        for path in paths:
            if str(path) not in self.file_hashes:
                self.file_hashes[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
        hashes = [self.file_hashes[str(path)] for path in paths]
        key = hashlib.sha256(json.dumps([POLICY, hashes], sort_keys=True).encode()).hexdigest()
        if key in self.results:
            if [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]!=hashes:
                raise ValueError("MATCH_INPUT_CHANGED_DURING_VERIFICATION")
            return self.results[key]
        result_path = self.directory / (key + ".json")
        if result_path.exists():
            saved = json.loads(result_path.read_text(encoding="utf-8"))
            # Retry prior timeouts/failures rather than freezing uncertainty.
            if saved.get("status") in ("PASS", "DIFFERENT"):
                if [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]!=hashes:
                    raise ValueError("MATCH_INPUT_CHANGED_DURING_VERIFICATION")
                self.results[key] = saved; return saved
        if self.progress:
            self.progress({"stage": "matching_geometry", "message": target.get("name", target["id"]), "occurrence_id": target["id"]})
        context = mp.get_context("spawn"); output = context.Queue()
        process = context.Process(target=_worker, args=(*map(str, paths), output), daemon=True)
        process.start(); stop = time.monotonic() + self.timeout_seconds; result = None;native_progress=[]
        try:
            while time.monotonic() < stop:
                if self.cancel and self.cancel():
                    raise InterruptedError("PREPROCESS_CANCELLED")
                try:
                    message = output.get(timeout=.1)
                    if "__progress__" in message:
                        native_progress.append(message["__progress__"]);continue
                    result=message;break
                except queue.Empty:
                    if not process.is_alive():
                        break
            if result is None:
                result = {"status": "REVIEW", "reason": "MATCH_NATIVE_TIMEOUT_OR_FAILURE", "timeout_seconds": self.timeout_seconds,"native_progress":native_progress}
        finally:
            if process.is_alive(): process.terminate()
            process.join(timeout=2); output.close()
        if [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]!=hashes:
            raise ValueError("MATCH_INPUT_CHANGED_DURING_VERIFICATION")
        result = {**result, "source_sha256": hashes[0], "target_sha256": hashes[1], "version": VERSION}
        from .jobs import atomic_json
        atomic_json(result_path, result); self.results[key] = result
        return result


def group_alignment_audit(pairs):
    """Preserve whole-template placement, not only independent shape equality."""
    if not pairs or any(result.get("status") != "PASS" for _, _, result in pairs):
        return {"status": "REVIEW", "reason": "MEMBER_GEOMETRY_UNVERIFIED"}
    reference = np.array(pairs[0][2]["source_to_target_world"]).reshape(4, 4)
    maximum = 0.0
    for source, target, result in pairs:
        actual = np.array(result["source_to_target_world"]).reshape(4, 4)
        for corner in itertools.product(*zip(source["bbox"]["min"], source["bbox"]["max"])):
            point = np.array([*corner, 1.0]); maximum = max(maximum, float(np.linalg.norm((reference @ point - actual @ point)[:3])))
    return {"status": "PASS" if maximum <= POLICY["group_placement_tolerance_mm"] else "REVIEW",
            "method": "one_proper_rigid_transform_for_all_members", "max_placement_difference_mm": maximum,
            "tolerance_mm": POLICY["group_placement_tolerance_mm"], "source_to_target_world": reference.reshape(-1).tolist()}
