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

ALGORITHM_VERSION = "pilot-v2-cad-2.0.0"
MATCHING_VERSION = "constellation-pruned-2.0.1"
REPAIR_VERSION = "bounded-representation-2.0.1"
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
    area_props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, area_props)
    return {"area_mm2": abs(area_props.Mass()), "bbox": shape_bbox(shape), "volume_mm3": volume, "solid_count": solids, "face_count": _count(shape, TopAbs_FACE), "valid": valid, "volume_status": "calculated" if volume is not None else "unsupported_open_or_invalid_shape", "method": SETTINGS["engine"]}


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
        if cached.get("settings") != SETTINGS:
            # Dependency or precision changes receive a distinct immutable
            # cache namespace; existing projects can keep their old assets.
            policy_key=hashlib.sha256(json.dumps(SETTINGS,sort_keys=True).encode()).hexdigest()[:12]
            return import_step(path,cache_dir/("policy-"+policy_key),progress)
        # Cache files remain usable after copying a project to another PC.
        cached["scene_path"] = str(directory / "scene.json")
        cached["manifest_path"] = str(manifest_path)
        if "transfer_report" in cached:
            cached["transfer_report"] = str(directory / "transfer-report.json")
        for instance in cached["instances"] + cached.get("reference_geometry", []):
            instance["brep_path"] = str(directory / (instance["id"]+".brep"))
        for definition in cached.get("definitions",[]):
            definition["brep_path"] = str(directory / (definition["id"]+".brep"))
        cached["source"]["path"]=str(path)
        cached["source"]["name"]=path.name
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
        local_features = shape_features(shape) if definition_id not in definitions else definitions[definition_id]
        features["local_valid"] = local_features["valid"]
        features["local_volume_mm3"] = local_features["volume_mm3"]
        if not features["valid"]:
            diagnostics.append({"code": "INVALID_SHAPE", "instance_id": id_, "severity": "error", "message": f"OCCT validity check failed: {name}"})
        if not features["solid_count"]:
            diagnostics.append({"code": "NON_SOLID_GEOMETRY", "instance_id": id_, "severity": "review_required", "message": f"No closed solid; volume unavailable: {name}"})
        brep_path = directory / f"{id_}.brep"
        if not BRepTools.Write_s(world_shape, str(brep_path)):
            raise ValueError(f"CAD_CACHE_WRITE: could not save {brep_path.name}")
        instance = {**node, **features, "collision_ready": features["valid"] and features["solid_count"] > 0, "brep_path": str(brep_path), "color": _color(name), "transform_world": _matrix(world_location.Multiplied(shape.Location()))}
        instance["leaf_ids"] = [id_]
        instances.append(instance)
        node["leaf_ids"] = [id_]
        if definition_id not in definitions:
            local_path = directory / (definition_id+".brep")
            BRepTools.Write_s(shape, str(local_path))
            definitions[definition_id] = {"brep_path": str(local_path), "id": definition_id, "name": definition_name, "source_label": target_entry, **shape_features(shape)}
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


def _canonical_name(value):
    value = re.sub(r"(?:\(\d+\)|\.\d+)$", "", value.upper().strip())
    return re.sub(r"[^A-Z0-9가-힣]", "", value)


def _feature_distance(a, b):
    """Rotation-invariant screening. Missing trustworthy volume is not fabricated."""
    av = a.get("volume_mm3") or a.get("local_volume_mm3")
    bv = b.get("volume_mm3") or b.get("local_volume_mm3")
    if a["solid_count"] != b["solid_count"]:
        return None
    values = []
    if av and bv:
        values.append(abs(av-bv)/max(av,bv))
    aa, ba = a.get("area_mm2", 0), b.get("area_mm2", 0)
    if aa and ba:
        values.append(abs(aa-ba)/max(aa,ba))
    # Face counts are only a tie-breaker: equivalent STEP topology can differ.
    if not values or max(values) > 0.015:
        return None
    return sum(values) + (0 if a["face_count"] == b["face_count"] else 0.005)


def _same_constellation(template, targets, tolerance=0.08):
    """Verify all pairwise centroid distances and a proper rigid alignment."""
    if len(template) <= 1:
        return True, 0.0
    import numpy as np
    a = np.array([i["bbox"]["center"] for i in template], dtype=float)
    b = np.array([i["bbox"]["center"] for i in targets], dtype=float)
    da = np.linalg.norm(a[:, None]-a[None, :], axis=2)
    db = np.linalg.norm(b[:, None]-b[None, :], axis=2)
    if float(np.max(np.abs(da-db))) > tolerance:
        return False, float(np.max(np.abs(da-db)))
    ac, bc = a-a.mean(axis=0), b-b.mean(axis=0)
    u, _, vt = np.linalg.svd(ac.T@bc)
    r = u@vt
    if np.linalg.det(r) < 0:
        u[:, -1] *= -1
        r = u@vt
    error = float(np.max(np.linalg.norm(ac@r-bc,axis=1)))
    return error <= tolerance, error


def _role(name):
    n = name.upper()
    if any(x in n for x in ("MAIN HOUSING", "MBD-")): return "housing"
    if any(x in n for x in ("COVER", "COV-")): return "cover"
    if any(x in n for x in ("GASKET", "SEAL", "SEL-")): return "seal"
    if any(x in n for x in ("PLATE", "PLT-")): return "plate"
    if any(x in n for x in ("RELAY", "RLY-", "KET")): return "relay"
    if any(x in n for x in ("BLT", "BOLT", "WASM", "NUT", "M6X", "M5X", "M4X")): return "fastener"
    if any(x in n for x in ("BUSBAR", "BUS-")): return "busbar"
    if any(x in n for x in ("CONT", "HVSC", "HVSE")): return "connector"
    return "component"


def match_units(assembly, templates, max_combinations=25000):
    """Multiplicity-aware template grouping, with no product-specific ID rules.

    Compound candidates must preserve constituent fingerprints and complete
    centroid constellation. Repeated equivalent instances retain distinct IDs.
    Unmatched CAD leaves are usable explicit units, never dropped.
    """
    import itertools
    occurrences = assembly["instances"]
    remaining = {o["id"]: o for o in occurrences}
    units, diagnostics = [], []

    def add(members, template=None, evidence=None):
        ids = sorted(o["id"] for o in members)
        uid = "unit_"+hashlib.sha256("|".join(ids).encode()).hexdigest()[:18]
        name = Path(template["source"]["name"]).stem if template else members[0]["name"]
        role = _role(name+" "+" ".join(o["name"] for o in members))
        units.append({"id":uid,"name":name,"occurrence_ids":ids,"bbox":combine_bbox([o["bbox"] for o in members]),
            "fixed":False,"fixed_candidate":role == "housing","role":role,"hidden":False,
            "collision_ready":all(o.get("collision_ready",o.get("valid",False)) for o in members),
            "matching":{"status":"matched" if template else "cad_leaf", "method":"metadata_features_constellation" if template else "CAD leaf fallback",
                "template_name":template["source"]["name"] if template else None,"evidence":evidence or {},
                "review_required":False},"solid_count":sum(o["solid_count"] for o in members)})
        for ident in ids: remaining.pop(ident,None)

    for template in sorted(templates,key=lambda t:-len(t["instances"])):
        leaves = template["instances"]
        if len(leaves) == 1:
            leaf = leaves[0]
            scored=[]
            for candidate in remaining.values():
                score = _feature_distance(leaf,candidate)
                name_equal = _canonical_name(leaf["name"]) in (_canonical_name(candidate["name"]),_canonical_name(candidate["definition_name"]))
                if score is not None and (name_equal or score < 0.00005):
                    scored.append((candidate,score,name_equal))
            # Repeated placements of a single definition are all legitimate.
            definitions = {c["definition_id"] for c,_,_ in scored}
            if len(definitions)>1:
                exact=[row for row in scored if row[2]]
                if exact: scored=exact
                if len({c["definition_id"] for c,_,_ in scored})>1:
                    diagnostics.append({"code":"MATCH_AMBIGUOUS","severity":"review_required","message":template["source"]["name"],"candidate_ids":[c["id"] for c,_,_ in scored]})
                    continue
            for candidate,score,name_equal in scored:
                add([candidate],template,{"feature_distance":score,"name_equal":name_equal})
            continue
        candidate_lists=[]
        for leaf in leaves:
            candidates=[]
            for candidate in remaining.values():
                score=_feature_distance(leaf,candidate)
                if score is None: continue
                name_equal=_canonical_name(leaf["name"]) in (_canonical_name(candidate["name"]),_canonical_name(candidate["definition_name"]))
                candidates.append((not name_equal,score,candidate["id"]))
            candidates.sort()
            exact=[row for row in candidates if not row[0]]
            candidate_lists.append([row[2] for row in (exact or candidates)[:12]])
        if any(not row for row in candidate_lists):
            diagnostics.append({"code":"TEMPLATE_NOT_MATCHED","severity":"info","message":template["source"]["name"]})
            continue
        tested=0; found={}; exhausted=False
        order=sorted(range(len(leaves)),key=lambda i:len(candidate_lists[i]))
        selected={}
        def visit(depth):
            nonlocal tested,exhausted
            if tested>=max_combinations:exhausted=True;return
            if depth==len(order):
                ids=[selected[i] for i in range(len(leaves))]
                targets=[remaining[i] for i in ids]
                valid,error=_same_constellation(leaves,targets)
                if valid:found.setdefault(tuple(sorted(ids)),(targets,error))
                return
            index=order[depth]
            for ident in candidate_lists[index]:
                if ident in selected.values():continue
                tested+=1
                if tested>=max_combinations:exhausted=True;return
                center=remaining[ident]["bbox"]["center"]
                compatible=True
                for previous,other_id in selected.items():
                    source_distance=math.dist(leaves[index]["bbox"]["center"],leaves[previous]["bbox"]["center"])
                    target_distance=math.dist(center,remaining[other_id]["bbox"]["center"])
                    if abs(source_distance-target_distance)>0.08:compatible=False;break
                if compatible:
                    selected[index]=ident;visit(depth+1);selected.pop(index)
        visit(0)
        if len(found)==1:
            targets,error=next(iter(found.values()))
            add(targets,template,{"constellation_max_error_mm":error,"tested_combinations":tested,"member_count":len(targets)})
        elif found or exhausted:
            diagnostics.append({"code":"MATCH_SEARCH_LIMITED" if exhausted else "MATCH_AMBIGUOUS","severity":"review_required","message":template["source"]["name"],"tested_combinations":tested,"candidate_group_count":len(found)})
        else:
            diagnostics.append({"code":"TEMPLATE_NOT_MATCHED","severity":"info","message":template["source"]["name"],"tested_combinations":tested})
    for leaf in list(remaining.values()):add([leaf])
    candidates=[u for u in units if u["fixed_candidate"]]
    if candidates:
        largest=max(candidates,key=lambda u:math.prod(u["bbox"]["size"]))
        largest["fixed"]=True
        largest["fixed_basis"]="housing role and largest envelope; editable suggestion"
    return units,diagnostics


def _repair_worker(source_path,candidate_path,audit_path,result_queue):
    from .representation_repair import verified_topology_repair
    try:
        original=TopoDS_Shape()
        if not BRepTools.Read_s(original,str(source_path),BRep_Builder()):raise ValueError("REPAIR_SOURCE_READ_FAILED")
        candidate,audit=verified_topology_repair(original)
        if candidate is not None:
            if not BRepTools.Write_s(candidate,str(candidate_path)):raise ValueError("REPAIR_WRITE_FAILED")
            reread=TopoDS_Shape()
            if not BRepTools.Read_s(reread,str(candidate_path),BRep_Builder()) or not BRepCheck_Analyzer(reread).IsValid():
                raise ValueError("REPAIR_ROUNDTRIP_INVALID")
            standard=BRepCheck_Analyzer(reread).IsValid()
            exact=BRepCheck_Analyzer(reread,True,False,True).IsValid()
            audit["roundtrip_standard_valid"]=standard;audit["roundtrip_exact_valid"]=exact
        _write_json(Path(audit_path),audit)
        result_queue.put({"status":audit["status"],"candidate_path":str(candidate_path) if candidate is not None else None,
            "audit_path":str(audit_path),"standard_valid":audit.get("roundtrip_standard_valid",False),"exact_valid":audit.get("roundtrip_exact_valid"),"method":audit.get("accepted_method")})
    except BaseException as exc:result_queue.put({"status":"blocked","reason":f"{type(exc).__name__}: {exc}"})


def repair_occurrences(assembly,cache_dir,progress=None,cancel=None,timeout_seconds=90):
    """Only proven representation changes; each native attempt is terminable.

    Exact-check disagreement remains visible. Collision use is scoped to the
    standard OCCT representation policy, not an unconditional CAD certificate.
    """
    import multiprocessing as mp
    import queue
    directory=Path(cache_dir)/REPAIR_VERSION;directory.mkdir(parents=True,exist_ok=True)
    for index,row in enumerate(assembly["instances"]):
        if row.get("valid"):continue
        if progress:progress({"stage":"geometry_repair","message":row["name"],"current":index+1,"total":len(assembly["instances"])})
        source=Path(row["brep_path"]);sha=hashlib.sha256(source.read_bytes()).hexdigest()
        result_file=directory/(sha[:24]+".json");candidate=directory/(sha[:24]+".brep");audit_file=directory/(sha[:24]+"-audit.json")
        result=None
        if result_file.exists():
            old=json.loads(result_file.read_text(encoding="utf-8"))
            if old.get("status")=="accepted" and candidate.exists() and audit_file.exists():
                result={**old,"candidate_path":str(candidate.resolve()),"audit_path":str(audit_file.resolve())}
        if result is None:
            context=mp.get_context("spawn");results=context.Queue()
            process=context.Process(target=_repair_worker,args=(source.resolve(),candidate.resolve(),audit_file.resolve(),results),daemon=True);process.start()
            stop=time.monotonic()+timeout_seconds
            while time.monotonic()<stop and result is None:
                if cancel and cancel():process.terminate();process.join(timeout=2);raise InterruptedError("PREPROCESS_CANCELLED")
                try:result=results.get(timeout=.2)
                except queue.Empty:
                    if not process.is_alive():break
            if result is None:result={"status":"blocked","reason":"NATIVE_REPAIR_TIMEOUT_OR_FAILURE"}
            if process.is_alive():process.terminate()
            process.join(timeout=2)
            _write_json(result_file,result)
        if hashlib.sha256(source.read_bytes()).hexdigest()!=sha:raise ValueError("REPAIR_SOURCE_MODIFIED")
        row["repair"]={**result,"source_sha256":sha,"version":REPAIR_VERSION}
        if result.get("status")=="accepted" and result.get("standard_valid"):
            row["source_brep_path"]=row["brep_path"];row["brep_path"]=result["candidate_path"]
            row["repaired_valid"]=True;row["collision_ready"]=True
            row["analysis_scope"]="standard_occt_with_exact_geometry_preservation"
            row["exact_method_valid"]=result.get("exact_valid")
            assembly["diagnostics"].append({"code":"REPRESENTATION_REPAIR_ACCEPTED","severity":"review_required" if not result.get("exact_valid") else "info",
                "instance_id":row["id"],"message":"Standard OCCT valid, original 3D geometry/trim/member identities and local tolerance budgets preserved. Exact-method result is independently disclosed.","method":result.get("method"),"exact_method_valid":result.get("exact_valid"),"audit_path":result["audit_path"]})
        else:
            assembly["diagnostics"].append({"code":"REPRESENTATION_REPAIR_BLOCKED","severity":"review_required","instance_id":row["id"],"message":result.get("reason","No repair passed geometry preservation gates.")})
    return assembly


def preprocess(assembly_path, part_paths, cache_dir, progress=None, cancel=None):
    """Public JSON contract. Geometry files are private immutable cache assets."""
    def report(event):
        if cancel and cancel(): raise InterruptedError("PREPROCESS_CANCELLED")
        if progress:progress(event)
    assembly=import_step(assembly_path,cache_dir,report)
    assembly=repair_occurrences(assembly,cache_dir,report,cancel)
    templates=[]; diagnostics=list(assembly["diagnostics"])
    for index,path in enumerate(part_paths or []):
        report({"stage":"part_import","message":Path(path).name,"current":index+1,"total":len(part_paths)})
        try:templates.append(import_step(path,cache_dir,report))
        except Exception as exc:diagnostics.append({"code":"PART_IMPORT_FAILED","severity":"review_required","message":str(exc),"file":Path(path).name})
    units,matching_diagnostics=match_units(assembly,templates)
    diagnostics.extend(matching_diagnostics)
    coverage=[ident for unit in units for ident in unit["occurrence_ids"]]
    complete=(len(coverage)==len(set(coverage))==len(assembly["instances"])
        and not any(d.get("code")=="NULL_SHAPE" for d in diagnostics))
    bad=[o["id"] for o in assembly["instances"] if not o["collision_ready"]]
    review=any(d.get("severity")=="review_required" and d["code"].startswith("MATCH") for d in diagnostics)
    disagreements=[o["id"] for o in assembly["instances"] if o.get("repaired_valid") and o.get("exact_method_valid") is False]
    display_ready=not any(d["code"]=="TESSELLATION_FAILED" for d in diagnostics)
    quality={"status":"PASS" if not bad and complete and not review and not disagreements and display_ready else "LIMITED", "inventory_complete":complete,
        "display_ready":display_ready,"matching_ready":complete and not review,
        "collision_ready":not bad,"invalid_occurrence_ids":bad,"exact_method_disagreement_ids":disagreements,"geometry_acceptance_scope":"standard_occt_with_preserved_geometry; exact-method disagreements remain review items", "source_warning_count":sum(d.get("count",0) for d in diagnostics if d["code"]=="STEP_TRANSFER_WARNING"),
        "unit_count":len(units),"occurrence_count":len(coverage),"solid_count":assembly["metrics"]["solids"],
        "unit_results":[{"unit_id":u["id"],"collision_ready":u["collision_ready"],"matching_ready":not u["matching"]["review_required"]} for u in units]}
    fingerprint=hashlib.sha256(json.dumps({"sources":[assembly["source"]["sha256"]]+[t["source"]["sha256"] for t in templates],"units":[u["occurrence_ids"] for u in units],"settings":SETTINGS,"matching_version":MATCHING_VERSION,"repair_version":REPAIR_VERSION,"repair_results":[o.get("repair",{}).get("status") for o in assembly["instances"]]},sort_keys=True).encode()).hexdigest()
    result={"id":"model_"+fingerprint[:16],"fingerprint":fingerprint,"algorithm_version":ALGORITHM_VERSION,"matching_version":MATCHING_VERSION,"repair_version":REPAIR_VERSION,
        "source":assembly["source"],"part_sources":[t["source"] for t in templates],"occurrences":assembly["instances"],"units":units,
        "definitions":assembly["definitions"],"nodes":assembly["nodes"],"reference_geometry":assembly["reference_geometry"],
        "scene_path":assembly["scene_path"],"bbox":assembly["bbox"],"quality":quality,"diagnostics":diagnostics,
        "metrics":assembly["metrics"],"coordinate_system":"RH_Z_UP_MM","transfer_report":assembly["transfer_report"]}
    _write_json(Path(cache_dir)/("model-"+fingerprint[:20]+".json"),result)
    return result

