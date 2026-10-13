"""OCCT-backed STEP/XDE import and bounded rigid-body assembly checks.

REQ-REG-002..004 / REQ-ANA-001..004,008. Geometry is RH, Z-up,
millimetres. Scene vertices already include every placement; do not apply
transform_world again. Rendering uses (x,z,-y)/1000. Collision checks use
precise BREP, never display triangles or bounding-box overlap as a verdict.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from pathlib import Path
from typing import Callable

import OCP
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_Tool
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.gp import gp_Trsf, gp_Vec
try:
    from OCP.collections import Sequence_TDF_Label as LabelSequence
    from OCP.collections import Sequence_TCollection_AsciiString as StringSequence
    from OCP.collections import List_TopoDS_Shape as ShapeList
except ImportError:  # compatible with OCP 7.x
    from OCP.TDF import TDF_LabelSequence as LabelSequence
    from OCP.TColStd import TColStd_SequenceOfAsciiString as StringSequence
    from OCP.TopTools import TopTools_ListOfShape as ShapeList

ALGORITHM_VERSION = "pilot-occt-1.3"
AXES = {"+X": [1, 0, 0], "-X": [-1, 0, 0], "+Y": [0, 1, 0], "-Y": [0, -1, 0], "+Z": [0, 0, 1], "-Z": [0, 0, -1]}
SETTINGS = {"algorithm": ALGORITHM_VERSION, "engine": f"OCCT/OCP {OCP.__version__}", "length_unit": "mm", "coordinate_system": "RH_Z_UP_MM", "linear_tolerance_mm": 1e-5, "volume_tolerance_mm3": 1e-4, "mesh_deflection_mm": 0.35, "mesh_angular_deflection_rad": 0.35}


def _write_json(path: Path, value):
    temporary = path.with_suffix(path.suffix + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temporary.replace(path)


def _entry(label):
    text = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, text)
    return text.ToCString()


def _name(label):
    attr = TDataStd_Name()
    return attr.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(), attr) else ""


def _matrix(location):
    tr = location.Transformation()
    return [tr.Value(r, c) for r in range(1, 4) for c in range(1, 5)] + [0, 0, 0, 1]


def shape_bbox(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        return None
    # OCCT 8 returns an unbound Bnd_Box::Limits from Get(); corner accessors
    # also work with OCCT 7 and preserve the exact same bounds.
    p, q = box.CornerMin(), box.CornerMax()
    low, high = [p.X(), p.Y(), p.Z()], [q.X(), q.Y(), q.Z()]
    return {"min": low, "max": high, "size": [b-a for a, b in zip(low, high)], "center": [(a+b)/2 for a, b in zip(low, high)]}


def combine_bbox(boxes):
    boxes = [b for b in boxes if b]
    if not boxes:
        return None
    low = [min(b["min"][j] for b in boxes) for j in range(3)]
    high = [max(b["max"][j] for b in boxes) for j in range(3)]
    return {"min": low, "max": high, "size": [b-a for a, b in zip(low, high)], "center": [(a+b)/2 for a, b in zip(low, high)]}


def _count(shape, kind):
    exp = TopExp_Explorer(shape, kind)
    n = 0
    while exp.More():
        n += 1
        exp.Next()
    return n


def shape_features(shape):
    if shape.IsNull():
        raise ValueError("CAD_NULL_SHAPE: no transferable geometry")
    valid = BRepCheck_Analyzer(shape).IsValid()
    solids = _count(shape, TopAbs_SOLID)
    props = GProp_GProps()
    volume = None
    if valid and solids:
        BRepGProp.VolumeProperties_s(shape, props)
        volume = abs(props.Mass())
    return {"bbox": shape_bbox(shape), "volume_mm3": volume, "solid_count": solids, "face_count": _count(shape, TopAbs_FACE), "valid": valid, "volume_status": "calculated" if volume is not None else "unsupported_open_or_invalid_shape", "method": SETTINGS["engine"]}


def _tessellate(shape):
    mesher = BRepMesh_IncrementalMesh(shape, SETTINGS["mesh_deflection_mm"], False, SETTINGS["mesh_angular_deflection_rad"], True)
    if not mesher.IsDone():
        raise ValueError("CAD_MESH_FAILED: OCCT tessellation did not finish")
    positions, normals, indices = [], [], []
    explorer = TopExp_Explorer(shape, TopAbs_FACE)
    while explorer.More():
        face = (TopoDS.Face(explorer.Current()) if hasattr(TopoDS, "Face") else TopoDS.Face_s(explorer.Current()))
        location = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, location)
        if tri is not None:
            tri.ComputeNormals()
            reverse = face.Orientation() == TopAbs_REVERSED
            offset = len(positions) // 3
            for i in range(1, tri.NbNodes()+1):
                p = tri.Node(i).Transformed(location.Transformation())
                n = tri.Normal(i).Transformed(location.Transformation())
                factor = -1 if reverse else 1
                positions.extend(round(v, 6) for v in (p.X(), p.Y(), p.Z()))
                normals.extend(round(factor*v, 6) for v in (n.X(), n.Y(), n.Z()))
            for i in range(1, tri.NbTriangles()+1):
                a, b, c = tri.Triangle(i).Get()
                if reverse:
                    b, c = c, b
                indices.extend([offset+a-1, offset+b-1, offset+c-1])
        explorer.Next()
    if not indices:
        raise ValueError("CAD_EMPTY_MESH: geometry has no displayable faces")
    return {"positions": positions, "normals": normals, "indices": indices}


def _color(name):
    name = name.upper()
    if "BUSBAR" in name or "BUS_" in name:
        return "#ce9a63"
    if any(x in name for x in ("BOLT", "WASM", "NUT", "BUSH", "M6_", "M5_")):
        return "#9ba6b7"
    if any(x in name for x in ("GASKET", "SEAL", "HARNESS")):
        return "#485566"
    if "COVER" in name:
        return "#b7c6d7"
    if "HOUSING" in name:
        return "#bdc7d0"
    if any(x in name for x in ("RELAY", "RLY", "CONTACT", "HVSC", "HVSE")):
        return "#6682a1"
    return "#92a6bc"


def _transfer_report(reader):
    """Keep OCCT's per-entity conversion issues, including auxiliary loss."""
    iterator = reader.Reader().WS().TransferReader().TransientProcess().CheckList(False)
    iterator.Start()
    entries, grouped = [], {}
    while iterator.More():
        check = iterator.Value()
        entity = iterator.Number()
        for kind, count, get_message in (("failure", check.NbFails(), check.CFail), ("warning", check.NbWarnings(), check.CWarning)):
            for index in range(1, count+1):
                message = get_message(index).strip()
                entries.append({"entity_number": entity, "kind": kind, "message": message})
                key = (kind, message)
                grouped.setdefault(key, []).append(entity)
        iterator.Next()
    summaries = []
    for (kind, message), entities in grouped.items():
        # Corrected topology still has a detailed audit record; unmapped or
        # poorly projected input needs review, never a silent full success.
        severity = "error" if kind == "failure" else ("info" if "corrected" in message.lower() else "review_required")
        summaries.append({"code": "STEP_TRANSFER_"+kind.upper(), "severity": severity, "message": message, "count": len(entities), "source_entities": entities[:20]})
    return {"entries": entries, "summary": summaries, "entry_count": len(entries)}


def import_step(path: str | Path, cache_dir: str | Path, progress: Callable | None = None):
    """Read STEP using OCCT XDE; write immutable hash-keyed BREP/scene cache.

    Names never determine identity. A definition is the referred XDE label;
    an occurrence identity is its complete component-label path. A compound
    of several solids stays one occurrence. No source files are modified.
    """
    started = time.perf_counter()
    path, cache_dir = Path(path).resolve(), Path(cache_dir).resolve()
    if path.suffix.lower() not in (".stp", ".step"):
        raise ValueError("CAD_EXTENSION: STEP requires .stp or .step")
    data = path.read_bytes()
    if not data or b"ISO-10303-21" not in data[:512]:
        raise ValueError("CAD_INVALID_STEP: empty file or missing ISO-10303-21 header")
    digest = hashlib.sha256(data).hexdigest()
    header = data[:16384].decode("latin-1", errors="replace")
    schema_match = re.search(r"FILE_SCHEMA\s*\(\((.*?)\)\)", header, re.S)
    schema = schema_match.group(1).replace("'", "").strip() if schema_match else "unknown"
    del data
    directory = cache_dir / f"{digest[:20]}-{ALGORITHM_VERSION}"
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / "manifest.json"
    cached_geometry = None
    if manifest_path.exists():
        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        # Cache files remain usable after copying a project to another PC.
        cached["scene_path"] = str(directory / "scene.json")
        cached["manifest_path"] = str(manifest_path)
        if "transfer_report" in cached:
            cached["transfer_report"] = str(directory / "transfer-report.json")
        for instance in cached["instances"] + cached.get("reference_geometry", []):
            instance["brep_path"] = str(directory / (instance["id"]+".brep"))
        if Path(cached["scene_path"]).exists() and all(Path(i["brep_path"]).exists() for i in cached["instances"]):
            cached["cache_hit"] = True
            if "transfer_report" in cached:
                return cached
            cached_geometry = cached
    if progress:
        progress({"stage": "cad_read", "message": path.name, "progress": 0.05})
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    read_status = reader.ReadFile(str(path))
    if read_status != IFSelect_RetDone:
        raise ValueError(f"CAD_READ_FAILED: OCCT STEP status {read_status}")
    lengths, angles, solids = StringSequence(), StringSequence(), StringSequence()
    reader.Reader().FileUnits(lengths, angles, solids)
    units = [lengths.Value(i).ToCString() for i in range(1, lengths.Length()+1)]
    reader.ChangeReader().SetSystemLengthUnit(1.0)  # OCCT internal millimetres
    document = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    if not reader.Transfer(document):
        raise ValueError("CAD_TRANSFER_FAILED: OCCT could not transfer STEP geometry")
    transfer_report = _transfer_report(reader)
    _write_json(directory / "transfer-report.json", transfer_report)
    if cached_geometry is not None:
        cached_geometry["diagnostics"] = transfer_report["summary"] + cached_geometry["diagnostics"]
        cached_geometry["transfer_report"] = str(directory / "transfer-report.json")
        cached_geometry["status"] = "partial" if any(d["severity"] == "error" for d in cached_geometry["diagnostics"]) else ("review_required" if any(d["severity"] == "review_required" for d in cached_geometry["diagnostics"]) else "ready")
        _write_json(manifest_path, cached_geometry)
        return cached_geometry
    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    roots = LabelSequence()
    shape_tool.GetFreeShapes(roots)
    nodes, instances, definitions, mesh_list, diagnostics, reference_geometry = [], [], {}, [], list(transfer_report["summary"]), []
    if not units:
        diagnostics.append({"code": "UNKNOWN_SOURCE_UNIT", "severity": "review_required", "message": "Source length unit could not be read; manual confirmation required."})

    def visit(label, parent_id, parent_location, path_labels, depth=0):
        if depth > 100:
            raise ValueError("CAD_TREE_DEPTH: possible cyclic assembly reference")
        entry = _entry(label)
        occurrence_path = path_labels + [entry]
        id_ = "cad_" + hashlib.sha256((digest+"/".join(occurrence_path)).encode()).hexdigest()[:18]
        referred = TDF_Label()
        reference = shape_tool.GetReferredShape_s(label, referred)
        target = referred if reference else label
        target_entry = _entry(target)
        definition_id = "def_" + hashlib.sha256((digest+target_entry).encode()).hexdigest()[:18]
        name = _name(label) or _name(target) or f"Shape {entry}"
        definition_name = _name(target) or name
        # Component location is relative to its parent; referred shape carries
        # its own local location. GetShape(component) is intentionally NOT used.
        relative = shape_tool.GetLocation_s(label) if reference else TopLoc_Location()
        world_location = parent_location.Multiplied(relative)
        children = LabelSequence()
        assembly = shape_tool.GetComponents_s(target, children)
        node = {"id": id_, "name": name, "definition_name": definition_name, "definition_id": definition_id, "parent_id": parent_id, "path": occurrence_path, "source_label": entry, "is_assembly": bool(assembly and children.Length()), "transform_world": _matrix(world_location), "children": [], "leaf_ids": []}
        nodes.append(node)
        if assembly and children.Length():
            target_location = shape_tool.GetLocation_s(target)
            child_parent_location = world_location.Multiplied(target_location)
            for j in range(1, children.Length()+1):
                child = visit(children.Value(j), id_, child_parent_location, occurrence_path, depth+1)
                node["children"].append(child["id"])
                node["leaf_ids"].extend(child["leaf_ids"])
            return node
        shape = shape_tool.GetShape_s(target)
        if shape.IsNull():
            diagnostics.append({"code": "NULL_SHAPE", "instance_id": id_, "severity": "error", "message": f"Missing geometry for {name}"})
            return node
        world_shape = shape.Moved(world_location)
        features = shape_features(world_shape)
        if not features["face_count"]:
            # XDE may also expose AP242 auxiliary wire/point representations.
            # Preserve and report them without turning them into installable
            # physical parts or silently dropping their source structure.
            brep_path = directory / f"{id_}.brep"
            BRepTools.Write_s(world_shape, str(brep_path))
            node["geometry_role"] = "auxiliary_curve_or_point"
            reference_geometry.append({**node, **features, "brep_path": str(brep_path)})
            diagnostics.append({"code": "AUXILIARY_NON_FACE_GEOMETRY", "instance_id": id_, "severity": "info", "message": f"Curve/point geometry retained separately from physical part occurrences: {name}"})
            return node
        if not features["valid"]:
            diagnostics.append({"code": "INVALID_SHAPE", "instance_id": id_, "severity": "error", "message": f"OCCT validity check failed: {name}"})
        if not features["solid_count"]:
            diagnostics.append({"code": "NON_SOLID_GEOMETRY", "instance_id": id_, "severity": "review_required", "message": f"No closed solid; volume unavailable: {name}"})
        brep_path = directory / f"{id_}.brep"
        if not BRepTools.Write_s(world_shape, str(brep_path)):
            raise ValueError(f"CAD_CACHE_WRITE: could not save {brep_path.name}")
        instance = {**node, **features, "brep_path": str(brep_path), "color": _color(name), "transform_world": _matrix(world_location.Multiplied(shape.Location()))}
        instance["leaf_ids"] = [id_]
        instances.append(instance)
        node["leaf_ids"] = [id_]
        if definition_id not in definitions:
            definitions[definition_id] = {"id": definition_id, "name": definition_name, "source_label": target_entry, **shape_features(shape)}
        try:
            mesh_list.append({"id": id_, "name": name, "definition_id": definition_id, "color": instance["color"], **_tessellate(world_shape)})
        except Exception as exc:
            diagnostics.append({"code": "TESSELLATION_FAILED", "instance_id": id_, "severity": "error", "message": str(exc)})
        if progress:
            progress({"stage": "cad_mesh", "message": name, "instances": len(instances), "progress": min(0.9, 0.2+len(instances)*0.007)})
        return node

    for index in range(1, roots.Length()+1):
        visit(roots.Value(index), None, TopLoc_Location(), [])
    if not instances:
        raise ValueError("CAD_NO_GEOMETRY: STEP contained no readable leaf shapes")
    bbox = combine_bbox([i["bbox"] for i in instances])
    scene_path = directory / "scene.json"
    _write_json(scene_path, {"coordinate_system": "RH_Z_UP_MM", "bbox": bbox, "meshes": mesh_list})
    elapsed = time.perf_counter()-started
    result = {"status": "partial" if any(d["severity"] == "error" for d in diagnostics) else ("review_required" if any(d["severity"] == "review_required" for d in diagnostics) else "ready"), "source": {"name": path.name, "path": str(path), "sha256": digest, "size": path.stat().st_size, "schema": schema, "units": units}, "instances": instances, "definitions": list(definitions.values()), "nodes": nodes, "reference_geometry": reference_geometry, "bbox": bbox, "scene_path": str(scene_path), "manifest_path": str(manifest_path), "diagnostics": diagnostics, "settings": dict(SETTINGS), "coordinate_transform": {"source_to_internal": "STEP unit conversion by OCCT to mm; recursive XDE occurrence transforms", "internal_to_display": "(x,y,z) -> (x,z,-y)/1000", "scene_vertices": "world mm; placement already applied once"}, "metrics": {"elapsed_seconds": round(elapsed, 3), "instances": len(instances), "definitions": len(definitions), "solids": sum(i["solid_count"] for i in instances), "triangles": sum(len(m["indices"])//3 for m in mesh_list), "auxiliary_geometry": len(reference_geometry)}, "cache_hit": False}
    result["transfer_report"] = str(directory / "transfer-report.json")
    _write_json(manifest_path, result)
    return result


def _boxes_separated(a, b, tolerance=1e-5):
    return any(a["max"][j] < b["min"][j]-tolerance or b["max"][j] < a["min"][j]-tolerance for j in range(3))


def static_interference(a, b, *, validate=True, tolerance_mm=1e-5, volume_tolerance_mm3=1e-4):
    """Classify solid BREP pairs; touching surfaces are not penetration."""
    try:
        if a.IsNull() or b.IsNull():
            return {"status": "calculation_failed", "reason": "null_shape"}
        if validate and (not BRepCheck_Analyzer(a).IsValid() or not BRepCheck_Analyzer(b).IsValid()):
            return {"status": "calculation_failed", "reason": "invalid_shape"}
        bbox_a, bbox_b = shape_bbox(a), shape_bbox(b)
        if not bbox_a or not bbox_b:
            return {"status": "calculation_failed", "reason": "empty_bounds"}
        if _boxes_separated(bbox_a, bbox_b, tolerance_mm):
            return {"status": "separated", "method": "exact_geometry_bounds_broadphase", "common_volume_mm3": 0.0, "distance_mm": None}
        if not _count(a, TopAbs_SOLID) or not _count(b, TopAbs_SOLID):
            return {"status": "review_required", "reason": "non_solid_geometry_no_penetration_verdict"}
        # The two-shape constructor builds immediately. Configure the empty
        # operator first to avoid doing every expensive Boolean twice.
        common = BRepAlgoAPI_Common()
        arguments, tools = ShapeList(), ShapeList()
        arguments.Append(a)
        tools.Append(b)
        common.SetArguments(arguments)
        common.SetTools(tools)
        common.SetFuzzyValue(tolerance_mm)
        common.Build()
        if not common.IsDone():
            return {"status": "calculation_failed", "reason": "boolean_common_failed"}
        props = GProp_GProps()
        if not common.Shape().IsNull():
            BRepGProp.VolumeProperties_s(common.Shape(), props)
        volume = abs(props.Mass())
        if not math.isfinite(volume):
            return {"status": "calculation_failed", "reason": "non_finite_common_volume"}
        if volume > volume_tolerance_mm3:
            return {"status": "collision", "common_volume_mm3": volume, "distance_mm": 0.0, "method": "OCCT_BooleanCommon"}
        distance = BRepExtrema_DistShapeShape(a, b)
        distance.Perform()
        if not distance.IsDone():
            return {"status": "calculation_failed", "reason": "distance_failed"}
        gap = distance.Value()
        return {"status": "contact" if gap <= tolerance_mm else "separated", "common_volume_mm3": volume, "distance_mm": gap, "method": "OCCT_BooleanCommon+DistShapeShape"}
    except Exception as exc:
        return {"status": "calculation_failed", "reason": f"{type(exc).__name__}: {exc}"}


def _translated(shape, vector):
    tr = gp_Trsf()
    tr.SetTranslation(gp_Vec(*vector))
    return shape.Moved(TopLoc_Location(tr))


def _overlap_interval(moving_box, fixed_box, offset):
    """Parameter interval where translating bounds overlap; t=0 is final."""
    low, high = 0.0, 1.0
    for j in range(3):
        if abs(offset[j]) < 1e-12:
            if moving_box["max"][j] < fixed_box["min"][j] or moving_box["min"][j] > fixed_box["max"][j]:
                return None
            continue
        a = (fixed_box["min"][j]-moving_box["max"][j])/offset[j]
        b = (fixed_box["max"][j]-moving_box["min"][j])/offset[j]
        low, high = max(low, min(a, b)), min(high, max(a, b))
        if low > high:
            return None
    return low, high


class GeometryEngine:
    def __init__(self, manifest):
        if isinstance(manifest, (str, Path)):
            from .desktop_storage import resolve_path
            manifest_path = Path(resolve_path(manifest)).resolve()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for instance in manifest["instances"]:
                instance["brep_path"] = str(manifest_path.parent / (instance["id"]+".brep"))
        self.manifest = manifest
        self.instances = {i["id"]: i for i in manifest["instances"]}
        self.shapes = {}

    def shape(self, id_):
        if id_ not in self.shapes:
            shape = TopoDS_Shape()
            from .desktop_storage import resolve_path
            if not BRepTools.Read_s(shape, str(resolve_path(self.instances[id_]["brep_path"])), BRep_Builder()) or shape.IsNull():
                raise ValueError(f"CAD_BREP_READ_FAILED: {id_}")
            self.shapes[id_] = shape
        return self.shapes[id_]

    def check_pair(self, a_id, b_id):
        return {"moving_id": a_id, "obstacle_id": b_id, **static_interference(self.shape(a_id), self.shape(b_id))}

    def check_path(self, moving_ids, assembled_ids, direction="+Z", distance_mm=None, sample_spacing_mm=2.0, max_samples=201, timeout_s=20.0):
        started = time.perf_counter()
        moving_ids = list(dict.fromkeys(moving_ids))
        obstacles = [i for i in dict.fromkeys(assembled_ids) if i not in moving_ids]
        result = {"status": "passed", "direction": direction, "collisions": [], "contacts": [], "errors": [], "conditions": {**SETTINGS, "method": "sampled_translation_precise_BREP", "nominal_spacing_mm": sample_spacing_mm, "max_samples": max_samples, "timeout_s": timeout_s, "assembled_ids": obstacles, "moving_ids": moving_ids, "self_and_future_excluded": True, "near_interval_refinement": "each swept-AABB overlap interval contributes midpoint and quarter points", "limitation": "Discrete rigid translation only; no continuous collision proof. Tools, fixtures, rotation and elastic deformation are not checked. Timeout checked between kernel operations."}}
        if not moving_ids or any(i not in self.instances for i in moving_ids+obstacles):
            return {**result, "status": "calculation_failed", "reason": "missing_or_unknown_instance_ids"}
        vector = AXES.get(direction) if isinstance(direction, str) else direction
        if not vector or len(vector) != 3 or any(not math.isfinite(float(x)) for x in vector) or math.sqrt(sum(x*x for x in vector)) < 1e-12:
            return {**result, "status": "review_required", "reason": "unsupported_direction_or_rotation"}
        length = math.sqrt(sum(v*v for v in vector))
        vector = [v/length for v in vector]
        if sample_spacing_mm <= 0 or max_samples < 2 or timeout_s <= 0:
            return {**result, "status": "calculation_failed", "reason": "invalid_sampling_settings"}
        invalid = [i for i in moving_ids+obstacles if not self.instances[i].get("valid", True)]
        nonsolid = [i for i in moving_ids+obstacles if self.instances[i].get("solid_count", 0) == 0]
        if invalid:
            return {**result, "status": "calculation_failed", "reason": "invalid_shape", "instance_ids": invalid}
        if nonsolid:
            return {**result, "status": "review_required", "reason": "non_solid_geometry", "instance_ids": nonsolid}
        extent = self.manifest.get("bbox") or combine_bbox([i["bbox"] for i in self.instances.values()])
        distance = float(distance_mm) if distance_mm is not None else max(extent["size"])*1.25+20.0
        if not math.isfinite(distance) or distance <= 0:
            return {**result, "status": "calculation_failed", "reason": "invalid_distance"}
        offset = [v*distance for v in vector]
        result["start_offset_mm"] = offset
        result["end_offset_mm"] = [0, 0, 0]
        result["conditions"]["distance_mm"] = distance
        # Parameters are fractions of start offset: inspect start -> final.
        count = min(max_samples, max(2, math.ceil(distance/sample_spacing_mm)+1))
        ts = {i/(count-1) for i in range(count)}
        pairs = []
        for moving in moving_ids:
            for obstacle in obstacles:
                interval = _overlap_interval(self.instances[moving]["bbox"], self.instances[obstacle]["bbox"], offset)
                if interval:
                    pairs.append((moving, obstacle, interval))
                    lo, hi = interval
                    ts.update([lo, hi, (lo+hi)/2, lo+(hi-lo)/4, lo+3*(hi-lo)/4])
        samples = sorted(ts, reverse=True)
        result["conditions"].update({"sample_count": len(samples), "base_spacing_mm": distance/(count-1), "broadphase_pair_count": len(pairs), "max_samples_applies_to": "uniform grid; overlap refinement adds samples"})
        checked = 0
        for t in samples:
            for moving, obstacle, (lo, hi) in pairs:
                if t < lo-1e-10 or t > hi+1e-10:
                    continue
                if time.perf_counter()-started > timeout_s:
                    result.update(status="review_required", reason="timeout", checked_pairs=checked, elapsed_seconds=round(time.perf_counter()-started, 3))
                    return result
                try:
                    verdict = static_interference(_translated(self.shape(moving), [v*t for v in offset]), self.shape(obstacle), validate=False)
                except Exception as exc:
                    verdict = {"status": "calculation_failed", "reason": str(exc)}
                checked += 1
                evidence = {**verdict, "moving_id": moving, "obstacle_id": obstacle, "path_fraction": round(1-t, 8), "offset_mm": [v*t for v in offset]}
                if verdict["status"] == "collision":
                    result["collisions"].append(evidence)
                    result.update(status="collision", reason="detected_penetration")
                    break
                if verdict["status"] == "contact":
                    if len(result["contacts"]) < 100:
                        result["contacts"].append(evidence)
                if verdict["status"] in ("calculation_failed", "review_required"):
                    result["errors"].append(evidence)
                    result.update(status=verdict["status"], reason=verdict.get("reason"))
                    break
            if result["status"] != "passed":
                break
        result.update(checked_pairs=checked, elapsed_seconds=round(time.perf_counter()-started, 3))
        return result

    def find_directions(self, moving_ids, assembled_ids, **kwargs):
        candidates = [self.check_path(moving_ids, assembled_ids, direction=axis, **kwargs) for axis in ("+Z", "-Z", "+X", "-X", "+Y", "-Y")]
        passed = [r["direction"] for r in candidates if r["status"] == "passed"]
        return {"status": "passed" if passed else "review_required", "available_directions": passed, "candidates": candidates, "reason": None if passed else "No verified candidate within six straight translation directions; rotation/deformation/other paths untested."}
