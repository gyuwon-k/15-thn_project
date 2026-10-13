"""Self-contained STEP/XDE adapter for ASAP. Coordinates are RH Z-up mm.

Each leaf occurrence is a rigid analysis unit. Repeated references are never
deduplicated, and every scene/OBJ vertex already has its final placement baked
in. A single common affine transform maps those vertices to the engine frame.
This module neither guesses a sequence nor reads the PILOT database.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import shutil
import time
import uuid
from pathlib import Path

import numpy as np
import OCP
from OCP.Bnd import Bnd_Box
from OCP.BRep import BRep_Builder, BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepClass import BRepClass_FaceClassifier
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_MakeVertex
from OCP.BRepExtrema import BRepExtrema_DistShapeShape
from OCP.BRepClass3d import BRepClass3d_SolidClassifier
from OCP.BRepAlgoAPI import BRepAlgoAPI_Common, BRepAlgoAPI_Fuse, BRepAlgoAPI_Cut
from OCP.BRepBndLib import BRepBndLib
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRepTools import BRepTools
from OCP.GProp import GProp_GProps
from OCP.IFSelect import IFSelect_RetDone
from OCP.IMeshTools import IMeshTools_Parameters, IMeshTools_MeshAlgoType
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
from OCP.TDataStd import TDataStd_Name
from OCP.TDF import TDF_Label, TDF_Tool
from OCP.TDocStd import TDocStd_Document
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SOLID, TopAbs_SHELL, TopAbs_EDGE, TopAbs_VERTEX, TopAbs_IN, TopAbs_OUT
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS, TopoDS_Shape, TopoDS_Compound
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.gp import gp_Trsf, gp_Pnt, gp_Pnt2d, gp_Vec
from .cad_repair import verified_topology_repair
try:
    from OCP.collections import Sequence_TDF_Label as LabelSequence
    from OCP.collections import Sequence_TCollection_AsciiString as StringSequence
    from OCP.collections import List_TopoDS_Shape as ShapeList
    from OCP.collections import IndexedMap_TopoDS_Shape_TopTools_ShapeMapHasher as ShapeMap
except ImportError:
    from OCP.TDF import TDF_LabelSequence as LabelSequence
    from OCP.TColStd import TColStd_SequenceOfAsciiString as StringSequence
    from OCP.TopTools import TopTools_ListOfShape as ShapeList
    from OCP.TopTools import TopTools_IndexedMapOfShape as ShapeMap

VERSION = "asap-cad-1.6-auto"
SCHEMA_VERSION = 1


def _write_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False), encoding="utf-8")
    temporary.replace(path)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _local(directory: Path, relative: str) -> Path:
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("CAD_PATH_OUTSIDE_SESSION: unsafe geometry cache path")
    return path


def _progress(stage, progress, message, **extra):
    print(json.dumps({"stage": stage, "progress": progress, "message": message, **extra}, ensure_ascii=False), flush=True)


def _entry(label):
    value = TCollection_AsciiString()
    TDF_Tool.Entry_s(label, value)
    return value.ToCString()


def _decode_name(raw_name):
    """Recover reversible UTF-8 mojibake only; source text remains auditable.

    Some STEP exporters put literal UTF-8 bytes in strings, and OCCT exposes
    each byte as a Latin-1 code point. CP1252 is also encountered. Do not use
    replacement decoding or alter already-correct Korean/other Unicode names.
    """
    for encoding in ("latin-1", "cp1252"):
        try:
            octets = raw_name.encode(encoding, errors="strict")
            decoded = octets.decode("utf-8", errors="strict")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        if decoded != raw_name and decoded.encode("utf-8") == octets:
            return decoded, {"operation": "reversible_utf8_display_name_recovery", "occt_byte_encoding": encoding,
                             "target_encoding": "utf-8", "source_modified": False}
    return raw_name, None


def _name_info(label):
    attribute = TDataStd_Name()
    raw_name = attribute.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(), attribute) else ""
    decoded, repair = _decode_name(raw_name)
    return {"name": decoded.strip(), "raw_name": raw_name, "name_encoding_recovery": repair}


def _matrix(location):
    transform = location.Transformation()
    return [transform.Value(row, column) for row in range(1, 4) for column in range(1, 5)] + [0, 0, 0, 1]


def _moved_by_matrix(shape, matrix):
    transform = gp_Trsf()
    transform.SetValues(*np.asarray(matrix, dtype=float).reshape(4, 4)[:3].ravel().tolist())
    return shape.Moved(TopLoc_Location(transform))


def shape_bbox(shape):
    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    if box.IsVoid():
        return None
    low, high = box.CornerMin(), box.CornerMax()
    return _bbox([low.X(), low.Y(), low.Z()], [high.X(), high.Y(), high.Z()])


def _bbox(low, high):
    low, high = [float(x) for x in low], [float(x) for x in high]
    return {"min": low, "max": high, "size": [b-a for a, b in zip(low, high)], "center": [(a+b)/2 for a, b in zip(low, high)]}


def combine_bbox(boxes):
    boxes = [box for box in boxes if box]
    if not boxes:
        return None
    return _bbox([min(b["min"][i] for b in boxes) for i in range(3)], [max(b["max"][i] for b in boxes) for i in range(3)])


def _count(shape, kind):
    iterator, count = TopExp_Explorer(shape, kind), 0
    while iterator.More():
        count += 1
        iterator.Next()
    return count


def shape_features(shape):
    if shape.IsNull():
        return {"bbox": None, "volume_mm3": None, "solid_count": 0, "face_count": 0, "valid": False}
    valid = bool(BRepCheck_Analyzer(shape).IsValid())
    solid_count = _count(shape, TopAbs_SOLID)
    volume = None
    if valid and solid_count:
        properties = GProp_GProps()
        BRepGProp.VolumeProperties_s(shape, properties)
        volume = float(abs(properties.Mass()))
    return {"bbox": shape_bbox(shape), "volume_mm3": volume, "solid_count": solid_count,
            "face_count": _count(shape, TopAbs_FACE), "valid": valid,
            "volume_status": "calculated" if volume is not None else "open_or_invalid"}


def _color(id_):
    # Identity-based display colour; classification never depends on the name.
    digest = hashlib.sha256(id_.encode()).digest()
    return "#" + "".join(f"{90 + value % 100:02x}" for value in digest[:3])


def _finite_option(options, key, default, minimum, maximum):
    value = float(options.get(key, default))
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"CAD_INVALID_OPTION: {key} must be between {minimum} and {maximum}")
    return value


def _transform_mesh(mesh, matrix):
    matrix = np.asarray(matrix, dtype=np.float64).reshape(4, 4)
    vertices = np.asarray(mesh["positions"], dtype=np.float64).reshape(-1, 3)
    normals = np.asarray(mesh["normals"], dtype=np.float64).reshape(-1, 3)
    transformed = vertices @ matrix[:3, :3].T + matrix[:3, 3]
    transformed_normals = normals @ np.linalg.inv(matrix[:3, :3])
    lengths = np.linalg.norm(transformed_normals, axis=1)
    transformed_normals /= np.maximum(lengths, 1e-15)[:, None]
    faces = np.asarray(mesh["indices"], dtype=np.int64).reshape(-1, 3)
    if np.linalg.det(matrix[:3, :3]) < 0:
        faces = faces[:, [0, 2, 1]]
    return {**mesh, "positions": transformed.ravel().tolist(), "normals": transformed_normals.ravel().tolist(), "indices": faces.ravel().tolist()}


def _tessellate_once(shape, deflection, angular, max_triangles=2_000_000, clean=True, mesh_algorithm="default"):
    # Force remeshing when accuracy changes; otherwise OCCT may reuse a coarse
    # triangulation from the display pass without honouring analysis settings.
    if clean:
        BRepTools.Clean_s(shape)
    if mesh_algorithm == "default":
        mesher = BRepMesh_IncrementalMesh(shape, deflection, False, angular, True)
    elif mesh_algorithm == "delabella":
        parameters = IMeshTools_Parameters()
        parameters.Deflection, parameters.Angle, parameters.InParallel = deflection, angular, True
        parameters.MeshAlgo = IMeshTools_MeshAlgoType.IMeshTools_MeshAlgoType_Delabella
        parameters.MinSize, parameters.AdjustMinSize = 1e-6, True
        parameters.EnableControlSurfaceDeflectionAllSurfaces = True
        mesher = BRepMesh_IncrementalMesh(shape, parameters)
    else:
        raise ValueError("CAD_INVALID_MESH_ALGORITHM")
    if not mesher.IsDone():
        raise ValueError("CAD_MESH_FAILED: OCCT tessellation did not complete")
    positions, normals, indices, weld_groups, triangle_solids = [], [], [], [], []
    total_faces, meshed_faces, zero_area_faces = _count(shape, TopAbs_FACE), 0, []
    # Coincident coordinates in two different original BREP shells must remain
    # separate mesh vertices. In particular, an inner cavity may touch the
    # outer shell along a curve: globally welding that curve creates false
    # four-face edges. This preserves all shells and their original winding.
    grouped_faces, shell_faces, shell_count, solid_shell_counts = [], ShapeMap(), 0, []
    source_solids = _solids(shape)
    # Source-solid membership is retained even when different members touch or
    # overlap. Each member's outer and cavity shells stay in the same group.
    for solid_index, member in enumerate(source_solids or [shape]):
        count = 0
        shells = TopExp_Explorer(member, TopAbs_SHELL)
        while shells.More():
            iterator = TopExp_Explorer(shells.Current(), TopAbs_FACE)
            while iterator.More():
                face = iterator.Current()
                grouped_faces.append((face, shell_count, solid_index if source_solids else -1))
                shell_faces.Add(face)
                iterator.Next()
            shell_count += 1
            count += 1
            shells.Next()
        solid_shell_counts.append(count)
    iterator = TopExp_Explorer(shape, TopAbs_FACE)
    while iterator.More():
        if not shell_faces.Contains(iterator.Current()):
            grouped_faces.append((iterator.Current(), shell_count, -1))
        iterator.Next()
    if len(grouped_faces) != total_faces:
        raise ValueError("CAD_SHELL_FACE_COUNT_MISMATCH: original shell face occurrences cannot be preserved")
    for face_shape, weld_group, solid_index in grouped_faces:
        face = TopoDS.Face(face_shape) if hasattr(TopoDS, "Face") else TopoDS.Face_s(face_shape)
        location = TopLoc_Location()
        triangulation = BRep_Tool.Triangulation_s(face, location)
        if triangulation is None or triangulation.NbTriangles() == 0:
            properties = GProp_GProps()
            BRepGProp.SurfaceProperties_s(face, properties)
            area = float(abs(properties.Mass()))
            if area <= 1e-12:
                zero_area_faces.append({"face_index": meshed_faces+len(zero_area_faces)+1, "area_mm2": area})
                continue
            raise ValueError(f"CAD_FACE_WITHOUT_TRIANGULATION: BREP face with area {area:.9g} mm2 is missing from the mesh")
        triangulation.ComputeNormals()
        orientation_reverse = face.Orientation() == TopAbs_REVERSED
        mirrored = bool(location.Transformation().IsNegative())
        reverse = orientation_reverse != mirrored
        offset = len(positions) // 3
        for index in range(1, triangulation.NbNodes() + 1):
            point = triangulation.Node(index).Transformed(location.Transformation())
            normal = triangulation.Normal(index).Transformed(location.Transformation())
            positions.extend((point.X(), point.Y(), point.Z()))
            weld_groups.append(weld_group)
            factor = -1 if orientation_reverse else 1
            normals.extend((factor*normal.X(), factor*normal.Y(), factor*normal.Z()))
        for index in range(1, triangulation.NbTriangles() + 1):
            a, b, c = triangulation.Triangle(index).Get()
            if reverse:
                b, c = c, b
            indices.extend((offset+a-1, offset+b-1, offset+c-1))
            triangle_solids.append(solid_index)
        meshed_faces += 1
        if len(indices) // 3 > max_triangles:
            raise ValueError("CAD_MESH_LIMIT: analysis mesh exceeded the per-unit triangle limit")
    if not indices:
        raise ValueError("CAD_EMPTY_MESH: no tessellated faces")
    if meshed_faces+len(zero_area_faces) != total_faces:
        raise ValueError("CAD_INCOMPLETE_MESH: source faces were not all tessellated")
    return {"positions": positions, "normals": normals, "indices": indices, "weld_groups": weld_groups,
            "triangle_solid_indices": triangle_solids,
            "tessellation_audit": {"source_face_count": total_faces, "meshed_face_count": meshed_faces,
                                   "zero_area_faces_without_triangles": zero_area_faces, "zero_area_face_tolerance_mm2": 1e-12,
                                   "effective_deflection_mm": deflection, "effective_angular_deflection_rad": angular,
                                   "original_shell_count": shell_count, "shell_vertex_identity_preserved": True,
                                   "original_solid_count": len(source_solids), "shell_count_per_original_solid": solid_shell_counts,
                                   "mesh_algorithm": mesh_algorithm,
                                   "occt_status_flags": int(mesher.GetStatusFlags())}}


def _tessellate(shape, deflection, angular, max_triangles=2_000_000, clean=True, adaptive=True, validate_topology=False):
    profiles = [(deflection, angular, "default")]
    if adaptive:
        profiles.extend([(deflection/2, max(.01, angular*.75), "default"), (deflection*2, min(1, angular*1.75), "default")])
        if validate_topology:
            # Exhaust requested linear precision with both triangulators and
            # an audited angular fallback before using coarser linear meshes.
            profiles = [profiles[0], profiles[1],
                        (deflection, angular, "delabella"),
                        (deflection, min(1, angular*2.25), "delabella"),
                        profiles[2],
                        (deflection*2, min(1, angular*1.75), "delabella"),
                        (deflection*3, min(1, angular*2.25), "delabella"),
                        (deflection*3.5, min(1, angular*2.5), "default")]
    attempts, best_mesh, best_rank = [], None, None
    features = shape_features(shape) if validate_topology else None
    for index, (linear, angle, algorithm) in enumerate(profiles):
        started = time.perf_counter()
        try:
            mesh = _tessellate_once(shape, linear, angle, max_triangles, clean if index == 0 else True, algorithm)
            attempt = {"deflection_mm": linear, "angular_deflection_rad": angle, "mesh_algorithm": algorithm, "status": "success", "elapsed_s": time.perf_counter()-started}
            if validate_topology:
                vertices, faces, audit = _weld_mesh(mesh)
                vertices, faces, audit = _remove_zero_area(vertices, faces, audit)
                quality = mesh_quality(vertices, faces, features, deflection)
                attempt.update(quality_status=quality["status"], quality_errors=quality["errors"],
                               boundary_edges=quality["boundary_edge_count"], nonmanifold_edges=quality["nonmanifold_edge_count"])
                rank = (len(quality["errors"]), sum(quality[key] for key in ("boundary_edge_count", "nonmanifold_edge_count", "degenerate_triangle_count", "inconsistent_winding_edge_count")))
                if best_rank is None or rank < best_rank:
                    best_mesh, best_rank = mesh, rank
                attempts.append(attempt)
                if quality["errors"]:
                    continue
            else:
                attempts.append(attempt)
            mesh["tessellation_audit"].update(requested_deflection_mm=deflection, requested_angular_deflection_rad=angular,
                                               adaptive_attempts=attempts, adaptive_used=index > 0,
                                               coarser_than_requested=linear > deflection,
                                               angular_coarser_than_requested=angle > angular)
            return mesh
        except ValueError as exc:
            attempts.append({"deflection_mm": linear, "angular_deflection_rad": angle, "mesh_algorithm": algorithm, "status": "failed", "reason": str(exc), "elapsed_s": time.perf_counter()-started})
            if not str(exc).startswith(("CAD_FACE_WITHOUT_TRIANGULATION", "CAD_MESH_FAILED", "CAD_MESH_LIMIT")) or index == len(profiles)-1:
                if best_mesh is not None:
                    break
                raise ValueError(str(exc) + "; deterministic meshing attempts=" + json.dumps(attempts, separators=(",", ":"))) from exc
    if best_mesh is not None:
        effective = best_mesh["tessellation_audit"]["effective_deflection_mm"]
        best_mesh["tessellation_audit"].update(requested_deflection_mm=deflection, requested_angular_deflection_rad=angular,
                                             adaptive_attempts=attempts, adaptive_used=effective != deflection,
                                             coarser_than_requested=effective > deflection,
                                             angular_coarser_than_requested=best_mesh["tessellation_audit"]["effective_angular_deflection_rad"] > angular,
                                             topology_attempts_exhausted=True)
        return best_mesh
    raise ValueError("CAD_MESH_FAILED: all deterministic mesh profiles failed")


def _transfer_report(reader):
    iterator = reader.Reader().WS().TransferReader().TransientProcess().CheckList(False)
    iterator.Start()
    entries = []
    while iterator.More():
        check = iterator.Value()
        for kind, number, getter in (("failure", check.NbFails(), check.CFail), ("warning", check.NbWarnings(), check.CWarning)):
            for index in range(1, number+1):
                entries.append({"entity_number": iterator.Number(), "kind": kind, "message": getter(index).strip()})
        iterator.Next()
    return {"entries": entries, "entry_count": len(entries), "failure_count": sum(e["kind"] == "failure" for e in entries)}


def _diagnostic(code, severity, message, unit_id=None, **extra):
    item = {"code": code, "severity": severity, "message": message, **extra}
    if unit_id is not None:
        item["unit_id"] = unit_id
    return item


def _save_brep(directory, id_, shape):
    relative = "brep/" + id_ + ".brep"
    path = _local(directory, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not BRepTools.Write_s(shape, str(path)):
        raise ValueError("CAD_BREP_WRITE_FAILED: " + id_)
    return relative


def _load_brep(directory, unit):
    shape = TopoDS_Shape()
    if not unit.get("brep_path") or not BRepTools.Read_s(shape, str(_local(directory, unit["brep_path"])), BRep_Builder()) or shape.IsNull():
        raise ValueError("CAD_BREP_READ_FAILED: " + unit["id"])
    return shape


def parse_step(session_dir: Path, source_path: Path, options: dict | None = None) -> dict:
    options, directory, source = options or {}, Path(session_dir).resolve(), Path(source_path).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    if source.suffix.lower() not in (".step", ".stp") or not source.is_file():
        raise ValueError("CAD_INVALID_STEP: choose an existing STEP/STP file")
    with source.open("rb") as stream:
        header = stream.read(1_000_000).decode("utf-8", errors="replace")
    if not header.lstrip("\ufeff \t\r\n").upper().startswith("ISO-10303-21;"):
        raise ValueError("CAD_INVALID_STEP: missing ISO-10303-21 header")
    started = time.perf_counter()
    with source.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    saved_source = directory / "source" / "model.step"
    saved_source.parent.mkdir(exist_ok=True)
    if source != saved_source.resolve():
        shutil.copyfile(source, saved_source)
    schema_match = re.search(r"FILE_SCHEMA\s*\(\s*\((.*?)\)\s*\)", header, re.I | re.S)
    deflection = _finite_option(options, "display_deflection_mm", 0.35, 0.001, 5)
    angular = _finite_option(options, "display_angular_deflection_rad", 0.35, 0.01, 1)
    _progress("step_read", 0.03, "STEP 구조와 형상 읽는 중")
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    reader.SetColorMode(True)
    reader.SetLayerMode(True)
    status = reader.ReadFile(str(saved_source))
    if status != IFSelect_RetDone:
        raise ValueError(f"CAD_READ_FAILED: OCCT STEP status {status}")
    lengths, angles, solids = StringSequence(), StringSequence(), StringSequence()
    reader.Reader().FileUnits(lengths, angles, solids)
    units = [lengths.Value(i).ToCString() for i in range(1, lengths.Length()+1)]
    reader.ChangeReader().SetSystemLengthUnit(1.0)
    document = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    if not reader.Transfer(document):
        raise ValueError("CAD_TRANSFER_FAILED: OCCT could not transfer STEP geometry")
    report = _transfer_report(reader)
    _write_json(directory / "transfer_report.json", report)
    tool = XCAFDoc_DocumentTool.ShapeTool_s(document.Main())
    roots = LabelSequence()
    tool.GetFreeShapes(roots)
    nodes, occurrences, definitions, meshes, auxiliary, diagnostics = [], [], {}, [], [], []
    definition_meshes = {}
    if not units:
        diagnostics.append(_diagnostic("SOURCE_UNIT_UNKNOWN", "error", "원본 길이 단위를 확인할 수 없습니다. 단위를 확인한 STEP으로 다시 내보내세요."))
    if report["failure_count"]:
        diagnostics.append(_diagnostic("STEP_TRANSFER_FAILURE", "error", "일부 STEP 엔티티의 OCCT 변환에 실패했습니다.", count=report["failure_count"]))
    if report["entry_count"] > report["failure_count"]:
        diagnostics.append(_diagnostic("STEP_TRANSFER_WARNINGS", "warning", "OCCT 변환 경고는 transfer_report.json에서 확인할 수 있습니다.", count=report["entry_count"]-report["failure_count"]))

    def visit(label, parent_id, parent_location, path, depth=0):
        if depth > 100:
            raise ValueError("CAD_TREE_DEPTH: cyclic or excessively deep assembly")
        entry, referred = _entry(label), TDF_Label()
        is_reference = bool(tool.GetReferredShape_s(label, referred))
        target = referred if is_reference else label
        occurrence_path = path + [entry]
        id_ = "occ_" + hashlib.sha256((digest + "/".join(occurrence_path)).encode()).hexdigest()[:20]
        definition_id = "def_" + hashlib.sha256((digest + _entry(target)).encode()).hexdigest()[:20]
        label_name, target_name = _name_info(label), _name_info(target)
        name_info = label_name if label_name["name"] else target_name
        name = name_info["name"] or "부품 " + id_[4:12]
        relative = tool.GetLocation_s(label) if is_reference else TopLoc_Location()
        world_location = parent_location.Multiplied(relative)
        children = LabelSequence()
        assembly = bool(tool.GetComponents_s(target, children) and children.Length())
        node = {"id": id_, "name": name, "definition_id": definition_id, "parent_id": parent_id,
                "raw_name": name_info["raw_name"], "name_encoding_recovery": name_info["name_encoding_recovery"],
                "source_label": entry, "path": occurrence_path, "is_assembly": assembly,
                "transform_world": _matrix(world_location), "children": []}
        nodes.append(node)
        if name_info["name_encoding_recovery"]:
            diagnostics.append(_diagnostic("STEP_NAME_ENCODING_RECOVERED", "info", "표시 이름의 UTF-8 인코딩을 복원하고 원본 이름을 보존했습니다.", id_,
                                           raw_name=name_info["raw_name"], display_name=name, recovery=name_info["name_encoding_recovery"]))
        if assembly:
            child_parent_location = world_location.Multiplied(tool.GetLocation_s(target))
            for index in range(1, children.Length()+1):
                node["children"].append(visit(children.Value(index), id_, child_parent_location, occurrence_path, depth+1))
            return id_
        shape = tool.GetShape_s(target)
        world_shape = shape.Moved(world_location) if not shape.IsNull() else shape
        features = shape_features(world_shape)
        item = {**node, **features, "color": _color(id_), "geometry_role": "physical_unit"}
        if shape.IsNull():
            item["brep_path"] = None
            occurrences.append(item)
            diagnostics.append(_diagnostic("NULL_SHAPE", "error", "배치에 형상 데이터가 없습니다.", id_))
            return id_
        item["brep_path"] = _save_brep(directory, id_, world_shape)
        item["transform_world"] = _matrix(world_location.Multiplied(shape.Location()))
        if not features["face_count"]:
            item["geometry_role"] = "auxiliary_curve_or_point"
            auxiliary.append(item)
            diagnostics.append(_diagnostic("AUXILIARY_GEOMETRY", "info", "보조 곡선/점은 보존하고 조립 단위에서 제외했습니다.", id_))
            return id_
        occurrences.append(item)
        if not features["valid"]:
            diagnostics.append(_diagnostic("INVALID_BREP", "error", "OCCT 형상 유효성 검사에 실패했습니다.", id_))
        if not features["solid_count"]:
            diagnostics.append(_diagnostic("NON_SOLID_GEOMETRY", "error", "닫힌 솔리드가 없는 형상입니다.", id_))
        if features["solid_count"] > 1:
            diagnostics.append(_diagnostic("MULTI_SOLID_UNIT", "info", "여러 솔리드를 한 강체로 유지합니다. 실제 독립 부품인지 검토한 뒤 분할할 수 있습니다.", id_, count=features["solid_count"]))
        definitions.setdefault(definition_id, {"id": definition_id, "name": target_name["name"] or name,
                                               "raw_name": target_name["raw_name"], "name_encoding_recovery": target_name["name_encoding_recovery"],
                                               "source_label": _entry(target), **shape_features(shape)})
        try:
            transform = np.asarray(item["transform_world"]).reshape(4, 4)
            if definition_id not in definition_meshes:
                first_mesh = _tessellate(world_shape, deflection, angular, clean=False)
                definition_meshes[definition_id] = _transform_mesh(first_mesh, np.linalg.inv(transform))
            mesh = _transform_mesh(definition_meshes[definition_id], transform)
            meshes.append({"id": id_, "name": name, "color": item["color"], **mesh})
        except Exception as exc:
            diagnostics.append(_diagnostic("DISPLAY_MESH_FAILED", "error", str(exc), id_))
        _progress("step_mesh", min(0.9, 0.15+len(occurrences)*0.006), name, unit_count=len(occurrences))
        return id_

    for index in range(1, roots.Length()+1):
        visit(roots.Value(index), None, TopLoc_Location(), [])
    if not occurrences:
        raise ValueError("CAD_NO_PHYSICAL_GEOMETRY: no readable face-bearing leaf occurrences")
    bbox = combine_bbox([unit["bbox"] for unit in occurrences])
    errors = [d for d in diagnostics if d["severity"] == "error"]
    manifest = {"schema_version": SCHEMA_VERSION, "adapter_version": VERSION, "occt_version": OCP.__version__,
                "status": "partial" if errors else ("viewer_only" if len(occurrences) < 2 else "ready"),
                "source": {"name": source.name, "path": saved_source.relative_to(directory).as_posix(), "sha256": digest,
                           "size": source.stat().st_size, "schema": schema_match.group(1).strip() if schema_match else "unknown", "units": units},
                "length_unit": "mm", "coordinate_system": "RH_Z_UP_MM", "placement": "world_baked_once",
                "bbox": bbox, "nodes": nodes, "definitions": list(definitions.values()), "auxiliary_geometry": auxiliary,
                "diagnostics": diagnostics, "unit_revision": 0, "engine_ids": {}, "conversion": None,
                "metrics": {"unit_count": len(occurrences), "definition_count": len(definitions), "solid_count": sum(u["solid_count"] for u in occurrences),
                            "display_mesh_count": len(meshes), "triangle_count": sum(len(m["indices"])//3 for m in meshes), "elapsed_s": time.perf_counter()-started}}
    _write_json(directory / "units.json", occurrences)
    _write_json(directory / "display_scene.json", {"coordinate_system": "RH_Z_UP_MM", "bbox": bbox, "meshes": meshes})
    _write_json(directory / "manifest.json", manifest)
    _progress("step_ready", 1, "STEP 확인 완료", unit_count=len(occurrences), error_count=len(errors))
    return {"status": manifest["status"], "unit_count": len(occurrences), "mesh_count": len(meshes), "diagnostics": diagnostics, "manifest": manifest}


def _weld_mesh(mesh, tolerance_mm=1e-6):
    vertices = np.asarray(mesh["positions"], dtype=np.float64).reshape(-1, 3)
    faces = np.asarray(mesh["indices"], dtype=np.int64).reshape(-1, 3)
    if not np.isfinite(vertices).all():
        raise ValueError("MESH_NON_FINITE: invalid vertex coordinate")
    # OCCT emits separate nodes on neighbouring faces. Welding coincident
    # nodes does not close holes, invent triangles or change source topology.
    anchor = vertices.min(axis=0)
    coordinate_keys = (vertices-anchor)/tolerance_mm
    if np.max(coordinate_keys) > 9e18:
        raise ValueError("MESH_COORDINATE_RANGE: dimensions exceed safe coincidence-welding precision")
    keys = np.rint(coordinate_keys).astype(np.int64)
    if "weld_groups" in mesh:
        groups = np.asarray(mesh["weld_groups"], dtype=np.int64)
        if groups.shape != (len(vertices),) or np.any(groups < 0):
            raise ValueError("MESH_SHELL_IDENTITY_INVALID: every raw node must retain its original shell identity")
        keys = np.column_stack((groups, keys))
    _, keep, inverse = np.unique(keys, axis=0, return_index=True, return_inverse=True)
    welded = vertices[keep]
    return welded, inverse[faces], {"operation": "weld_coincident_nodes", "tolerance_mm": tolerance_mm,
                                   "vertices_before": len(vertices), "vertices_after": len(welded),
                                   "weld_scope": "original_BREP_shell" if "weld_groups" in mesh else "coincident_coordinates",
                                   "original_shell_count": mesh.get("tessellation_audit", {}).get("original_shell_count"),
                                   "shell_vertex_identity_preserved": "weld_groups" in mesh,
                                   "max_node_displacement_mm": float(np.max(np.linalg.norm(vertices-welded[inverse], axis=1))),
                                   "faces_removed": 0, "holes_filled": 0}


def _remove_zero_area(vertices, faces, audit):
    triangles = vertices[faces]
    areas = np.linalg.norm(np.cross(triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0]), axis=1)/2
    edge_lengths = np.stack([np.linalg.norm(triangles[:, (index+1) % 3]-triangles[:, index], axis=1) for index in range(3)], axis=1)
    longest = edge_lengths.max(axis=1)
    # Detect numerical collinearity geometrically rather than from area alone;
    # a transformed zero-area OCCT connector can acquire a tiny nonzero cross
    # product through floating-point roundoff.
    heights = 2*areas/np.maximum(longest, 1e-30)
    remove = (areas <= 1e-14) | (heights <= 1e-8)
    kept = faces[~remove]
    # OCCT sometimes uses a collinear zero-area triangle to join one long edge
    # to two shorter edges. Merely deleting it creates a T-junction. Subdivide
    # the already existing neighbouring triangle at those existing edge points;
    # this changes its triangulation, without adding a cap across a CAD hole.
    split_edges = {}
    for face in faces[remove]:
        if len(set(int(index) for index in face)) != 3:
            continue
        points = vertices[face]
        lengths = [np.linalg.norm(points[(i+1) % 3]-points[i]) for i in range(3)]
        edge_index = int(np.argmax(lengths))
        a, b, middle = int(face[edge_index]), int(face[(edge_index+1) % 3]), int(face[(edge_index+2) % 3])
        vector = vertices[b]-vertices[a]
        denominator = float(vector @ vector)
        if denominator <= 1e-20:
            continue
        fraction = float((vertices[middle]-vertices[a]) @ vector / denominator)
        distance = np.linalg.norm(vertices[middle]-(vertices[a]+fraction*vector))
        if 1e-10 < fraction < 1-1e-10 and distance <= 1e-8:
            split_edges.setdefault(tuple(sorted((a, b))), set()).add(middle)
    affected, vertices_list, new_faces = {}, vertices.tolist(), []
    if split_edges and len(kept):
        count = len(vertices)
        edges = np.concatenate((kept[:, [0, 1]], kept[:, [1, 2]], kept[:, [2, 0]]), axis=0)
        sorted_pairs = np.sort(edges, axis=1)
        codes = sorted_pairs[:, 0]*count+sorted_pairs[:, 1]
        order = np.argsort(codes)
        sorted_codes = codes[order]
        for (a, b), points in split_edges.items():
            code = a*count+b
            start, end = np.searchsorted(sorted_codes, [code, code], side="left")[0], np.searchsorted(sorted_codes, code, side="right")
            for edge_index in order[start:end]:
                face_index = int(edge_index % len(kept))
                affected.setdefault(face_index, {})[(a, b)] = points
        for face_index, edge_points in affected.items():
            face, polygon = kept[face_index], []
            for index in range(3):
                a, b = int(face[index]), int(face[(index+1) % 3])
                polygon.append(a)
                points = edge_points.get(tuple(sorted((a, b))), set())
                vector = vertices[b]-vertices[a]
                denominator = float(vector @ vector)
                polygon.extend(sorted(points, key=lambda point: float((vertices[point]-vertices[a]) @ vector/denominator)))
            centroid = vertices[face].mean(axis=0)
            center_index = len(vertices_list)
            vertices_list.append(centroid.tolist())
            new_faces.extend([[polygon[index], polygon[(index+1) % len(polygon)], center_index] for index in range(len(polygon))])
    keep_mask = np.ones(len(kept), dtype=bool)
    if affected:
        keep_mask[list(affected)] = False
    cleaned = np.concatenate((kept[keep_mask], np.asarray(new_faces, dtype=np.int64).reshape(-1, 3)))
    result_vertices = np.asarray(vertices_list, dtype=float)
    clean_triangles = result_vertices[cleaned]
    after_area = float(np.linalg.norm(np.cross(clean_triangles[:, 1]-clean_triangles[:, 0], clean_triangles[:, 2]-clean_triangles[:, 0]), axis=1).sum()/2)
    return result_vertices, cleaned, {**audit, "operation": "coincidence_weld_zero_area_removal_and_collinear_edge_subdivision",
                           "zero_area_triangle_tolerance_mm2": 1e-14, "faces_removed": int(np.count_nonzero(remove)),
                           "collinearity_height_tolerance_mm": 1e-8,
                           "removed_surface_area_mm2": float(areas[remove].sum()), "holes_filled": 0,
                           "collinear_edge_targets": len(split_edges), "existing_faces_retriangulated": len(affected),
                           "new_on_face_centroid_vertices": len(vertices_list)-len(vertices),
                           "surface_area_change_mm2": after_area-float(areas.sum()),
                           "raw_mesh_preserved": True}


def mesh_quality(vertices, faces, unit, deflection_mm):
    if not len(vertices) or not len(faces):
        raise ValueError("MESH_EMPTY: no triangles")
    triangles = vertices[faces]
    cross = np.cross(triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0])
    area = np.linalg.norm(cross, axis=1)/2
    edge_vectors = np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]]), axis=0)
    sorted_edges = np.sort(edge_vectors, axis=1)
    _, inverse, counts = np.unique(sorted_edges, axis=0, return_inverse=True, return_counts=True)
    direction = np.where(edge_vectors[:, 0] < edge_vectors[:, 1], 1, -1)
    orientation_sum = np.bincount(inverse, weights=direction)
    boundary = int(np.count_nonzero(counts == 1))
    nonmanifold = int(np.count_nonzero(counts > 2))
    winding_bad = int(np.count_nonzero((counts == 2) & (orientation_sum != 0)))
    degenerate = int(np.count_nonzero(area <= 1e-14))
    unique_faces = np.unique(np.sort(faces, axis=1), axis=0)
    duplicate_faces = len(faces)-len(unique_faces)
    # Recenter for numerical precision when CAD coordinates are far from origin.
    centered = triangles - vertices.mean(axis=0)
    signed_volume = float(np.einsum("ij,ij->i", centered[:, 0], np.cross(centered[:, 1], centered[:, 2])).sum()/6)
    bbox = _bbox(vertices.min(axis=0), vertices.max(axis=0))
    cad_bbox, cad_volume = unit.get("bbox"), unit.get("volume_mm3")
    bbox_error = float(max(np.max(np.abs(np.asarray(bbox[side])-np.asarray(cad_bbox[side]))) for side in ("min", "max"))) if cad_bbox else None
    relative_error = abs(abs(signed_volume)-cad_volume)/cad_volume if cad_volume and cad_volume > 0 else None
    watertight = not boundary and not nonmanifold and not degenerate and not duplicate_faces
    quality = {"vertex_count": len(vertices), "triangle_count": len(faces), "watertight": watertight,
               "boundary_edge_count": boundary, "nonmanifold_edge_count": nonmanifold,
               "inconsistent_winding_edge_count": winding_bad, "degenerate_triangle_count": degenerate,
               "duplicate_triangle_count": duplicate_faces, "winding_consistent": winding_bad == 0,
               "signed_volume_mm3": signed_volume, "mesh_volume_mm3": abs(signed_volume), "cad_volume_mm3": cad_volume,
               "volume_relative_error": relative_error, "bbox": bbox, "bbox_max_error_mm": bbox_error,
               "bbox_tolerance_mm": max(2*deflection_mm, 1e-5),
               "volume_relative_tolerance": 0.03,
               "limitations": ["Edge closure does not prove absence of self-intersections.", "Chordal and bounding/volume checks do not guarantee every small feature is preserved."]}
    errors, warnings = [], []
    if not unit.get("valid"):
        errors.append("INVALID_BREP")
    if not unit.get("solid_count"):
        errors.append("NON_SOLID_GEOMETRY")
    if not watertight:
        errors.append("MESH_NOT_WATERTIGHT")
    if winding_bad or signed_volume <= 0:
        errors.append("MESH_ORIENTATION_INVALID")
    if bbox_error is None or bbox_error > quality["bbox_tolerance_mm"]:
        errors.append("MESH_BOUNDS_DEVIATION")
    if relative_error is None:
        errors.append("MESH_VOLUME_UNVERIFIABLE")
    elif relative_error > quality["volume_relative_tolerance"]:
        errors.append("MESH_VOLUME_DEVIATION")
    if relative_error is not None and relative_error > 0.005:
        warnings.append("MESH_VOLUME_APPROXIMATION")
    quality.update(errors=errors, warnings=warnings, status="blocked" if errors else "ready")
    return quality


def _vertex_normals(vertices, faces):
    normals = np.zeros(vertices.shape)
    triangles = vertices[faces]
    cross = np.cross(triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0])
    for column in range(3):
        np.add.at(normals, faces[:, column], cross)
    lengths = np.linalg.norm(normals, axis=1)
    lengths[lengths < 1e-15] = 1
    return normals / lengths[:, None]


def _write_obj(path, vertices, faces):
    with path.open("w", encoding="ascii", newline="\n") as stream:
        stream.write("# ASAP adapter: assembled coordinates baked once\n")
        for vertex in vertices:
            stream.write("v " + " ".join(f"{value:.12g}" for value in vertex) + "\n")
        for face in faces:
            stream.write("f " + " ".join(str(int(value)+1) for value in face) + "\n")


def _partition_original_solids(mesh, features, deflection_mm):
    """Partition only recorded OCCT source-solid triangle membership.

    No inferred connected-component segmentation, remeshing, cavity inversion
    or hole capping is performed. Returns definition-local mm meshes.
    """
    vertices = np.asarray(mesh["positions"], dtype=float).reshape(-1, 3)
    normals = np.asarray(mesh["normals"], dtype=float).reshape(-1, 3)
    faces = np.asarray(mesh["indices"], dtype=np.int64).reshape(-1, 3)
    labels = np.asarray(mesh.get("triangle_solid_indices", []), dtype=np.int64)
    groups = np.asarray(mesh.get("weld_groups", []), dtype=np.int64)
    if labels.shape != (len(faces),) or groups.shape != (len(vertices),) or np.any(labels < 0) or np.any(labels >= len(features)):
        raise ValueError("CAD_SOLID_MEMBERSHIP_UNVERIFIABLE: every original triangle must belong to one original solid")
    components, offset = [], 0
    for index, feature in enumerate(features):
        selected = faces[labels == index]
        used = np.unique(selected)
        if not len(selected):
            raise ValueError("CAD_ORIGINAL_SOLID_WITHOUT_TRIANGLES: no source solid can be omitted")
        remap = np.full(len(vertices), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        piece = {"positions": vertices[used].ravel().tolist(), "normals": normals[used].ravel().tolist(),
                 "indices": remap[selected].ravel().tolist(), "weld_groups": groups[used].tolist(),
                 "tessellation_audit": {"original_shell_count": len(np.unique(groups[used]))}}
        v, f, preprocessing = _weld_mesh(piece)
        v, f, preprocessing = _remove_zero_area(v, f, preprocessing)
        used_after = np.unique(f)
        remap_after = np.full(len(v), -1, dtype=np.int64)
        remap_after[used_after] = np.arange(len(used_after))
        v, f = v[used_after], remap_after[f]
        components.append({"vertices": v, "faces": f, "features": feature,
                           "source_solid_index": index, "source_triangle_count": len(selected),
                           "original_shell_count": piece["tessellation_audit"]["original_shell_count"],
                           "preprocessing": preprocessing})
        offset += len(v)
    return components


def _concat_components(components):
    vertices, faces, offset = [], [], 0
    for component in components:
        vertices.append(component["vertices"])
        faces.append(component["faces"]+offset)
        offset += len(component["vertices"])
    return np.vstack(vertices), np.vstack(faces)


def _triangle_geometry_digest(vertices, faces):
    # Ordered triangle coordinates retain winding, repetitions and all surfaces.
    values = np.asarray(vertices, dtype="<f8")[faces]
    return hashlib.sha256(values.tobytes()).hexdigest()


def _probe_numerical_budget(coordinate_map):
    """Shared transform/float precision budget, independent of any part or SDF."""
    scale = float(coordinate_map["scale"])
    box = coordinate_map["cad_bbox"]
    center = np.asarray(coordinate_map["center_mm"], dtype=float)
    max_engine_coordinate = float(np.max(np.abs(scale*(np.asarray([box["min"], box["max"]])-center))))
    # Match the native world-vertex check and the native query round-trip check.
    world_float_tolerance = float(max(1e-7, max_engine_coordinate*np.finfo(np.float32).eps*4))
    return {"native_query_tolerance_engine": 2e-5,
            "native_world_vertex_tolerance_engine": world_float_tolerance,
            "maximum_scene_engine_coordinate": max_engine_coordinate,
            "engine_units_per_mm": scale,
            "numerical_clearance_mm": (2e-5+world_float_tolerance)/scale,
            "basis": "common_scene_transform_float32_native_vertices_and_native_query_tolerances"}


def _solid_probe_boundary(solid):
    """Boundary faces only: point-to-solid distance would be zero inside."""
    builder, boundary = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(boundary)
    iterator = TopExp_Explorer(solid, TopAbs_FACE)
    while iterator.More():
        builder.Add(boundary, iterator.Current())
        iterator.Next()
    return boundary, _source_max_edge_vertex_tolerance(solid)


def _source_max_edge_vertex_tolerance(solid):
    tolerance = 0.0
    for kind, cast in ((TopAbs_EDGE, "Edge"), (TopAbs_VERTEX, "Vertex")):
        iterator = TopExp_Explorer(solid, kind)
        convert = getattr(TopoDS, cast, None) or getattr(TopoDS, cast+"_s")
        while iterator.More():
            tolerance = max(tolerance, float(BRep_Tool.Tolerance_s(convert(iterator.Current()))))
            iterator.Next()
    return tolerance


def _solid_occupancy_probes(solid, numerical_clearance_mm=0.0, return_audit=False, original_tolerance_mm=None):
    """Only decisive original-BREP signs become probes; all rejected points remain auditable.

    Candidate generation and acceptance use CAD and numerical uncertainty only.
    They never depend on mesh/SDF results, part names or a desired sequence.
    """
    if not math.isfinite(float(numerical_clearance_mm)) or numerical_clearance_mm < 0:
        raise ValueError("CAD_INVALID_PROBE_NUMERICAL_BUDGET")
    box = shape_bbox(solid)
    audit = {"schema_version": 1, "status": "checking", "source_geometry_modified": False,
             "boundary_distance_method": "OCCT_vertex_to_compound_of_original_trimmed_faces",
             "numerical_clearance_mm": float(numerical_clearance_mm), "excluded_candidates": [],
             "candidate_generation": "cavity_shell_centers_then_bbox_axis_fractions_then_bounded_CAD_grid_and_face_normal_offsets_if_required"}
    if not box or not BRepCheck_Analyzer(solid).IsValid():
        audit.update(status="unavailable", reason="original_solid_invalid_or_without_bounds")
        return ([], audit) if return_audit else []
    boundary, source_tolerance = _solid_probe_boundary(solid)
    if original_tolerance_mm is not None:
        source_tolerance = max(source_tolerance, float(original_tolerance_mm))
    classification_tolerance = 1e-7
    required_clearance = source_tolerance+classification_tolerance+float(numerical_clearance_mm)
    audit.update(source_max_edge_vertex_tolerance_mm=source_tolerance,
                 classification_tolerance_mm=classification_tolerance,
                 required_boundary_clearance_mm=required_clearance)
    candidates = []
    cavity_regions = []
    iterator = TopExp_Explorer(solid, TopAbs_SHELL)
    while iterator.More():
        shell = iterator.Current()
        properties = GProp_GProps()
        BRepGProp.VolumeProperties_s(shell, properties)
        if properties.Mass() < 0:
            cavity_box = shape_bbox(shell)
            point = cavity_box["center"]
            # Reversal is confined to a temporary classification copy. The
            # exported original cavity shell winding is never changed.
            shell_cast = TopoDS.Shell(shell.Reversed()) if hasattr(TopoDS, "Shell") else TopoDS.Shell_s(shell.Reversed())
            cavity = BRepBuilderAPI_MakeSolid(shell_cast).Solid()
            cavity_regions.append((cavity_box, cavity))
            if BRepClass3d_SolidClassifier(cavity, gp_Pnt(*point), 1e-7).State() == TopAbs_IN:
                candidates.append((point, "cavity"))
        iterator.Next()
    for fraction in (.2, .5, .8):
        for axis in range(3):
            point = list(box["center"])
            point[axis] = box["min"][axis]+fraction*box["size"][axis]
            candidates.append((point, "material"))
    margin = max(max(box["size"])*.1, .01)
    candidates.append(([box["max"][0]+margin, box["center"][1], box["center"][2]], "outside"))
    probes, seen = [], set()

    def consider(point, kind, provenance=None):
        key = tuple(point)
        if key in seen:
            return
        seen.add(key)
        classifier = BRepClass3d_SolidClassifier(solid, gp_Pnt(*point), classification_tolerance)
        state = classifier.State()
        candidate = {"point_local_cad_mm": list(point), "kind": kind,
                     "classifier": "OCCT_BRepClass3d", "classifier_state": str(state),
                     "classification_tolerance_mm": classification_tolerance}
        if provenance:
            candidate["candidate_provenance"] = provenance
        if state not in (TopAbs_IN, TopAbs_OUT):
            candidate["reason"] = "CAD_classifier_boundary_or_unknown"
            audit["excluded_candidates"].append(candidate)
            return
        inside = state == TopAbs_IN
        candidate["inside"] = inside
        if (kind == "material" and not inside) or (kind in ("cavity", "outside") and inside):
            candidate["reason"] = "CAD_classification_disagrees_with_candidate_role"
            audit["excluded_candidates"].append(candidate)
            return
        distance = BRepExtrema_DistShapeShape(BRepBuilderAPI_MakeVertex(gp_Pnt(*point)).Vertex(), boundary)
        clearance = float(distance.Value()) if distance.IsDone() else None
        candidate["boundary_clearance_mm"] = clearance
        if clearance is None or not math.isfinite(clearance):
            candidate["reason"] = "CAD_boundary_distance_unavailable"
            audit["excluded_candidates"].append(candidate)
            return
        if clearance <= required_clearance:
            candidate["reason"] = "CAD_boundary_clearance_not_decisive_at_original_and_native_uncertainty"
            audit["excluded_candidates"].append(candidate)
            return
        probes.append(candidate)

    for point, kind in candidates:
        consider(point, kind)
    # Deterministic bounded fallback supplies a robust point for thin/off-center
    # material or cavity regions. Original candidate failures remain in the audit.
    def grid(region):
        for x in (.2, .5, .8):
            for y in (.2, .5, .8):
                for z in (.2, .5, .8):
                    yield [region["min"][axis]+fraction*region["size"][axis] for axis, fraction in enumerate((x, y, z))]
    if not any(probe["kind"] == "material" for probe in probes):
        for point in grid(box):
            consider(point, "material")
    if not any(probe["kind"] == "material" for probe in probes):
        # Thin shells and open bores can have no material along any bbox axis.
        # Sample trimmed CAD faces in their native order, in both normal
        # directions, at a bounded geometry-derived range of offsets. The
        # solid classifier and exact boundary distance remain the only judges.
        positive_sizes = [size for size in box["size"] if size > 0]
        minimum_size = min(positive_sizes) if positive_sizes else 0
        offsets = sorted({value for value in (
            *(required_clearance*factor for factor in (1.05, 1.25, 1.5, 2, 4)),
            *(minimum_size*fraction for fraction in (.0001, .001, .01, .1, .25, .5)))
            if value > required_clearance}, reverse=True)
        iterator, face_index = TopExp_Explorer(solid, TopAbs_FACE), 0
        fallback_start = len(probes)
        while iterator.More() and face_index < 128 and not any(probe["kind"] == "material" for probe in probes):
            face = TopoDS.Face(iterator.Current()) if hasattr(TopoDS, "Face") else TopoDS.Face_s(iterator.Current())
            surface = BRepAdaptor_Surface(face)
            umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
            for uf, vf in ((.5, .5), (.2, .2), (.8, .8), (.2, .8), (.8, .2)):
                u, v = umin+uf*(umax-umin), vmin+vf*(vmax-vmin)
                if not math.isfinite(u) or not math.isfinite(v):
                    continue
                if BRepClass_FaceClassifier(face, gp_Pnt2d(u, v), classification_tolerance).State() != TopAbs_IN:
                    continue
                point, du, dv = gp_Pnt(), gp_Vec(), gp_Vec()
                try:
                    surface.D1(u, v, point, du, dv)
                    normal = du.Crossed(dv)
                    if normal.Magnitude() <= 1e-15:
                        continue
                    normal.Normalize()
                except Exception as exc:
                    audit.setdefault("face_sampling_failures", []).append({"face_index": face_index, "uv": [u, v], "reason": type(exc).__name__})
                    continue
                origin = np.asarray([point.X(), point.Y(), point.Z()])
                direction = np.asarray([normal.X(), normal.Y(), normal.Z()])
                for offset in offsets:
                    for sign in (-1, 1):
                        consider((origin+sign*offset*direction).tolist(), "material", {
                            "method": "original_trimmed_face_normal_offset", "face_index": face_index,
                            "uv": [u, v], "offset_mm": sign*offset})
            iterator.Next()
            face_index += 1
        fallback_probes = probes[fallback_start:]
        if fallback_probes:
            strongest = max(fallback_probes, key=lambda candidate: candidate["boundary_clearance_mm"])
            for candidate in fallback_probes:
                if candidate is not strongest:
                    audit["excluded_candidates"].append({**candidate, "reason": "decisive_CAD_candidate_not_selected_for_bounded_sample_set"})
            probes[fallback_start:] = [strongest]
        audit["material_face_sampling"] = {"maximum_faces": 128, "visited_faces": face_index, "offsets_mm": offsets}
    unresolved_cavity_regions = 0
    for region, cavity in cavity_regions:
        if not any(probe["kind"] == "cavity" and BRepClass3d_SolidClassifier(cavity, gp_Pnt(*probe["point_local_cad_mm"]), classification_tolerance).State() == TopAbs_IN for probe in probes):
            fallback_start = len(probes)
            for point in grid(region):
                if BRepClass3d_SolidClassifier(cavity, gp_Pnt(*point), classification_tolerance).State() == TopAbs_IN:
                    consider(point, "cavity")
            fallback_probes = probes[fallback_start:]
            if fallback_probes:
                strongest = max(fallback_probes, key=lambda candidate: candidate["boundary_clearance_mm"])
                for candidate in fallback_probes:
                    if candidate is not strongest:
                        audit["excluded_candidates"].append({**candidate, "reason": "decisive_CAD_candidate_not_selected_for_bounded_sample_set"})
                probes[fallback_start:] = [strongest]
            else:
                # A concave cavity can have no interior point on its bbox grid.
                # Its temporary reversed shell is sampled as an ordinary CAD
                # solid; only decisive interior points are reconsidered as
                # void points of the untouched original solid.
                cavity_probes, cavity_audit = _solid_occupancy_probes(cavity,
                    numerical_clearance_mm=numerical_clearance_mm, return_audit=True,
                    original_tolerance_mm=source_tolerance)
                audit.setdefault("cavity_region_sampling", []).append(cavity_audit)
                for candidate in cavity_probes:
                    if candidate["inside"]:
                        consider(candidate["point_local_cad_mm"], "cavity", {
                            "method": "temporary_reversed_original_cavity_shell_CAD_interior",
                            "temporary_cavity_boundary_clearance_mm": candidate["boundary_clearance_mm"]})
        if not any(probe["kind"] == "cavity" and BRepClass3d_SolidClassifier(cavity, gp_Pnt(*probe["point_local_cad_mm"]), classification_tolerance).State() == TopAbs_IN for probe in probes):
            unresolved_cavity_regions += 1
    audit.update(status="passed" if any(p["inside"] for p in probes) and any(p["kind"] == "outside" for p in probes) and not unresolved_cavity_regions else "insufficient_decisive_samples",
                 accepted_probe_count=len(probes), excluded_candidate_count=len(audit["excluded_candidates"]),
                 cavity_region_count=len(cavity_regions), unresolved_cavity_region_count=unresolved_cavity_regions)
    return (probes, audit) if return_audit else probes


def _solid_shell_counts(solid):
    outer, cavity, zero = 0, 0, 0
    iterator = TopExp_Explorer(solid, TopAbs_SHELL)
    while iterator.More():
        properties = GProp_GProps()
        BRepGProp.VolumeProperties_s(iterator.Current(), properties)
        mass = properties.Mass()
        if mass > 0:
            outer += 1
        elif mass < 0:
            cavity += 1
        else:
            zero += 1
        iterator.Next()
    return {"outer_shell_count": outer, "cavity_shell_count": cavity, "zero_signed_volume_shell_count": zero}


def _penetration_limits(options):
    limits = {"timeout_s": _finite_option(options, "penetration_timeout_s", 15, 0, 120)}
    for key, name, default, maximum in (
        ("penetration_max_pairs", "max_pairs", 80, 10_000),
        ("penetration_max_faces_per_unit", "max_faces_per_unit", 2_000, 20_000),
        ("penetration_max_face_pairs", "max_face_pairs", 250_000, 5_000_000),
    ):
        value = _finite_option(options, key, default, 0, maximum)
        if value != int(value):
            raise ValueError("CAD_INVALID_OPTION: "+key+" must be an integer")
        limits[name] = int(value)
    limits.update(complexity_policy="conservative_face_count_admission_before_BREP_loading",
                  timeout_scope="between OCCT operations; complexity guard is heuristic, not a per-operation deadline; outer worker enforces hard timeout")
    return limits


def _initial_penetration(directory, units, options):
    started = time.perf_counter()
    limits = _penetration_limits(options)
    timeout, max_pairs = limits["timeout_s"], limits["max_pairs"]
    tolerance, candidates, skipped, unchecked, reasons = 1e-5, [], [], [], {}
    detail_limit, aabb_excluded = 5000, 0

    def uncheck(a, b, reason, **evidence):
        reasons[reason] = reasons.get(reason, 0)+1
        if len(unchecked) < detail_limit:
            unchecked.append({"a":a["id"], "b":b["id"], "status":"not_checked", "reason_code":reason, **evidence})

    def face_count(unit):
        value = unit.get("face_count")
        # Missing/invalid cached complexity never admits an unbounded Boolean.
        return int(value) if isinstance(value, (int, np.integer)) and not isinstance(value, bool) and value > 0 else None

    for index, a in enumerate(units):
        for b in units[index+1:]:
            if not a.get("bbox") or not b.get("bbox"):
                skipped.append([a["id"], b["id"]])
                uncheck(a, b, "MISSING_BOUNDS")
                continue
            overlap = np.minimum(a["bbox"]["max"], b["bbox"]["max"])-np.maximum(a["bbox"]["min"], b["bbox"]["min"])
            # Zero-volume AABB contact cannot have significant solid overlap.
            if not np.all(overlap > tolerance):
                aabb_excluded += 1
                continue
            if not a["valid"] or not b["valid"] or not a["solid_count"] or not b["solid_count"]:
                skipped.append([a["id"], b["id"]])
                uncheck(a, b, "INVALID_OR_NON_SOLID_BREP")
            else:
                candidates.append((a, b, float(np.prod(overlap))))
    candidates.sort(key=lambda pair: pair[2], reverse=True)
    admitted, complexity_skipped = [], 0
    for a, b, aabb_volume in candidates:
        counts = [face_count(a), face_count(b)]
        evidence = {"face_counts":counts, "aabb_overlap_volume_mm3":aabb_volume}
        if None in counts:
            uncheck(a, b, "FACE_COMPLEXITY_UNKNOWN", **evidence)
            complexity_skipped += 1
            continue
        product = counts[0]*counts[1]
        evidence["face_pair_product"] = product
        if max(counts) > limits["max_faces_per_unit"] or product > limits["max_face_pairs"]:
            uncheck(a, b, "FACE_COMPLEXITY_LIMIT", **evidence)
            complexity_skipped += 1
            continue
        admitted.append((a, b, aabb_volume))
    if complexity_skipped:
        _progress("initial_penetration", 0.9, "고비용 겹침 검사 쌍은 미검사로 기록", complexity_skipped_pairs=complexity_skipped,
                  admitted_pairs=len(admitted), checked_pairs=0)
    shapes, pairs, evaluated, budget_skipped = {}, [], 0, 0
    for index, (a, b, aabb_volume) in enumerate(admitted):
        if evaluated >= max_pairs or time.perf_counter()-started >= timeout:
            reason = "PAIR_BUDGET_EXHAUSTED" if evaluated >= max_pairs else "TIME_BUDGET_EXHAUSTED"
            for pending_a, pending_b, pending_volume in admitted[index:]:
                uncheck(pending_a, pending_b, reason, aabb_overlap_volume_mm3=pending_volume)
                budget_skipped += 1
            break
        try:
            for unit in (a, b):
                if unit["id"] not in shapes:
                    shapes[unit["id"]] = _load_brep(directory, unit)
            common, arguments, tools = BRepAlgoAPI_Common(), ShapeList(), ShapeList()
            arguments.Append(shapes[a["id"]])
            tools.Append(shapes[b["id"]])
            common.SetArguments(arguments)
            common.SetTools(tools)
            common.SetFuzzyValue(tolerance)
            common.SetNonDestructive(True)
            common.Build()
            if not common.IsDone():
                raise ValueError("OCCT BooleanCommon failed")
            properties = GProp_GProps()
            if not common.Shape().IsNull():
                BRepGProp.VolumeProperties_s(common.Shape(), properties)
            volume = float(abs(properties.Mass()))
            threshold = max(1e-4, min(a["volume_mm3"] or 0, b["volume_mm3"] or 0)*1e-8)
            pairs.append({"a": a["id"], "b": b["id"], "status": "penetration" if volume > threshold else "no_volume_penetration",
                          "common_volume_mm3": volume, "volume_tolerance_mm3": threshold, "method": "OCCT_BooleanCommon"})
        except Exception as exc:
            pairs.append({"a": a["id"], "b": b["id"], "status": "calculation_failed", "reason": str(exc)})
        evaluated += 1
        _progress("initial_penetration", 0.9, "초기 겹침 진단", checked_pairs=evaluated)
    unchecked_count = sum(reasons.values())
    failed = sum(p["status"] == "calculation_failed" for p in pairs)
    return {"method": "positive_AABB_overlap_then_complexity_bounded_OCCT_BooleanCommon", "status": "partial" if unchecked_count or failed else "complete",
            "total_pairs": len(units)*(len(units)-1)//2, "broadphase_candidates": len(candidates), "checked_pairs": evaluated,
            "aabb_excluded_pair_count": aabb_excluded, "admitted_pair_count": len(admitted),
            "complexity_skipped_pair_count": complexity_skipped, "budget_skipped_pair_count": budget_skipped,
            "unresolved_pair_count": unchecked_count+failed,
            "unchecked_pairs": unchecked, "unchecked_pair_count_by_reason": reasons,
            "unchecked_pair_details_limit": detail_limit, "unchecked_pair_details_truncated": max(0, unchecked_count-len(unchecked)),
            "invalid_or_missing_pairs": skipped[:200], "penetration_pairs": [p for p in pairs if p["status"] == "penetration"],
            "pairs": pairs, "elapsed_s": time.perf_counter()-started,
            "limits": limits,
            "limitation": "Static rigid BREP overlap diagnostic; no assembly path, tools, fastening or elastic contact verification."}


def _boolean_operator(kind, arguments, tools, tolerance_mm):
    operator, arg_list, tool_list = kind(), ShapeList(), ShapeList()
    for shape in arguments:
        arg_list.Append(shape)
    for shape in tools:
        tool_list.Append(shape)
    operator.SetArguments(arg_list)
    operator.SetTools(tool_list)
    operator.SetFuzzyValue(tolerance_mm)
    operator.SetNonDestructive(True)
    operator.Build()
    if not operator.IsDone():
        raise ValueError("CAD_BOOLEAN_FAILED: " + kind.__name__)
    return operator.Shape()


def _shape_volume(shape):
    if shape.IsNull():
        return 0.0
    properties = GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, properties)
    return float(abs(properties.Mass()))


def audit_solid_occupancy(shape, options=None):
    """Diagnose interior overlaps in one rigid unit without changing solids.

    A closed concatenated multi-solid OBJ can represent XOR occupancy in the
    upstream SDF loader. Its mesh topology alone therefore cannot verify its
    occupied material. Every skipped positive-AABB pair remains unresolved.
    """
    limits = _penetration_limits(options or {})
    started = time.perf_counter()
    solids = _solids(shape)
    features = [shape_features(solid) for solid in solids]
    checked, admitted, excluded, unresolved, positive, reasons = 0, 0, 0, [], [], {}
    for first in range(len(solids)):
        for second in range(first+1, len(solids)):
            a, b = features[first], features[second]
            bbox_a, bbox_b = a["bbox"], b["bbox"]
            if bbox_a and bbox_b and min(min(bbox_a["max"][axis], bbox_b["max"][axis])-max(bbox_a["min"][axis], bbox_b["min"][axis]) for axis in range(3)) <= 1e-5:
                excluded += 1
                continue
            row = {"solid_indices": [first, second], "face_counts": [a["face_count"], b["face_count"]]}
            reason = None
            if not a["valid"] or not b["valid"]:
                reason = "INVALID_SOURCE_SOLID"
            elif max(a["face_count"], b["face_count"]) > limits["max_faces_per_unit"] or a["face_count"]*b["face_count"] > limits["max_face_pairs"]:
                reason = "FACE_COMPLEXITY_LIMIT"
            elif admitted >= limits["max_pairs"] or time.perf_counter()-started >= limits["timeout_s"]:
                reason = "PAIR_OR_TIME_BUDGET"
            if reason is None:
                admitted += 1
                try:
                    common = _boolean_operator(BRepAlgoAPI_Common, [solids[first]], [solids[second]], 1e-7)
                    if not common.IsNull() and not BRepCheck_Analyzer(common).IsValid():
                        reason = "COMMON_INVALID"
                    else:
                        overlap = _shape_volume(common)
                        checked += 1
                        if overlap > 1e-6:
                            positive.append({**row, "volume_mm3": overlap})
                except Exception as exc:
                    reason = "BOOLEAN_FAILED"
                    row["detail"] = str(exc)
            if reason is not None:
                reasons[reason] = reasons.get(reason, 0)+1
                unresolved.append({**row, "reason": reason, "status": "not_checked"})
    return {"operation": "original_solid_interior_overlap_diagnostic", "solid_count": len(solids),
            "status": "partial" if unresolved else "complete", "pair_count": len(solids)*(len(solids)-1)//2,
            "bbox_excluded_pair_count": excluded, "checked_pair_count": checked, "admitted_pair_count": admitted,
            "positive_overlap_pair_count": len(positive), "positive_overlap_pairs": positive,
            "unresolved_pair_count": len(unresolved), "unresolved_pairs": unresolved[:200],
            "unresolved_details_truncated": max(0, len(unresolved)-200), "unresolved_reason_counts": reasons,
            "sdf_parity_union_equivalent": not positive and not unresolved,
            "limits": limits, "elapsed_s": time.perf_counter()-started,
            "limitations": ["Boolean checks use tolerances and are diagnostics, not a proof of every occupied point.",
                            "The timeout is checked between OCCT operations; it is not a hard per-operation timeout."]}


def verified_solid_union(shape, tolerance_mm=1e-6):
    """Optional representation repair, never a part-segmentation operation.

    A valid Boolean result alone is insufficient. Reject changes in bounds,
    material outside the source or source material omitted from the result.
    The source BREP and source OBJ are retained by the caller.
    """
    started, audit = time.perf_counter(), {"operation": "verified_OCCT_solid_union", "tolerance_mm": tolerance_mm,
                                          "status": "rejected", "part_identity_changed": False, "source_preserved": True}
    try:
        source = shape_features(shape)
        audit["source_features"] = source
        if not source["valid"] or not source["solid_count"]:
            raise ValueError("SOURCE_BREP_INVALID: union does not heal an invalid source")
        solids = _solids(shape)
        if len(solids) < 2:
            audit.update(status="not_required", elapsed_s=time.perf_counter()-started)
            return shape, audit
        united = _boolean_operator(BRepAlgoAPI_Fuse, solids[:1], solids[1:], tolerance_mm)
        after = shape_features(united)
        audit["analysis_features"] = after
        if not after["valid"] or not after["solid_count"] or not after["volume_mm3"]:
            raise ValueError("UNION_INVALID: the Boolean result is not a valid positive-volume solid representation")
        bounds_error = max(abs(source["bbox"][side][axis]-after["bbox"][side][axis]) for side in ("min", "max") for axis in range(3))
        audit["bbox_max_change_mm"] = bounds_error
        if bounds_error > max(2*tolerance_mm, 1e-6):
            raise ValueError("UNION_BOUNDS_CHANGED: Boolean union changed the occupied bounds")
        volume_tolerance = max(1e-4, source["volume_mm3"]*1e-8)
        missing = _shape_volume(_boolean_operator(BRepAlgoAPI_Cut, [shape], [united], tolerance_mm))
        extra = _shape_volume(_boolean_operator(BRepAlgoAPI_Cut, [united], [shape], tolerance_mm))
        audit.update(source_minus_union_mm3=missing, union_minus_source_mm3=extra, volume_difference_tolerance_mm3=volume_tolerance,
                     aggregate_source_volume_mm3=source["volume_mm3"], occupied_union_volume_mm3=after["volume_mm3"],
                     aggregate_to_occupied_volume_change_mm3=after["volume_mm3"]-source["volume_mm3"])
        if missing > volume_tolerance or extra > volume_tolerance or after["volume_mm3"] > source["volume_mm3"]+volume_tolerance:
            raise ValueError("UNION_OCCUPIED_VOLUME_CHANGED: two-way Boolean differences could not verify the occupied volume")
        audit.update(status="accepted", elapsed_s=time.perf_counter()-started)
        return united, audit
    except Exception as exc:
        audit.update(reason=str(exc), elapsed_s=time.perf_counter()-started)
        return None, audit


def convert_session(session_dir: Path, options: dict | None = None) -> dict:
    directory, options = Path(session_dir).resolve(), options or {}
    manifest, units = _read_json(directory / "manifest.json"), _read_json(directory / "units.json")
    started = time.perf_counter()
    deflection = _finite_option(options, "deflection_mm", 0.1, 0.001, 5)
    angular = _finite_option(options, "angular_deflection_rad", 0.2, 0.01, 1)
    engine_extent = _finite_option(options, "engine_extent", 10, 0.001, 1000)
    union_policy = options.get("multi_solid_policy", "preserve")
    if union_policy not in ("preserve", "verified_union"):
        raise ValueError("CAD_INVALID_OPTION: multi_solid_policy must be preserve or verified_union")
    union_tolerance = _finite_option(options, "union_tolerance_mm", 1e-6, 1e-8, 0.001)
    repair_policy = options.get("brep_repair_policy", "verified_topology")
    if repair_policy not in ("preserve", "verified_topology"):
        raise ValueError("CAD_INVALID_OPTION: brep_repair_policy must be preserve or verified_topology")
    penetration_limits = _penetration_limits(options)
    bbox = combine_bbox([unit.get("bbox") for unit in units])
    if not bbox or max(bbox["size"]) <= 1e-10:
        raise ValueError("CAD_EMPTY_BOUNDS: cannot determine common assembly dimensions")
    # Invalidate readiness before touching the OBJ set. Cancellation or a crash
    # therefore cannot leave an older ready flag beside a partially new input.
    manifest["conversion"] = None
    manifest["conversion_revision"] = None
    _write_json(directory / "manifest.json", manifest)
    (directory / "conversion.json").unlink(missing_ok=True)
    (directory / "solids_manifest.json").unlink(missing_ok=True)
    center = [(bbox["min"][0]+bbox["max"][0])/2, (bbox["min"][1]+bbox["max"][1])/2, bbox["min"][2]]
    scale = engine_extent/max(bbox["size"])
    forward = np.eye(4)
    forward[:3, :3] *= scale
    forward[:3, 3] = -scale*np.asarray(center)
    inverse = np.linalg.inv(forward)
    coordinate_map = {"schema_version": 1, "cad_frame": "RH_Z_UP_MM", "engine_frame": "RH_Z_UP_ENGINE_UNITS",
                      "cad_to_engine": forward.ravel().tolist(), "engine_to_cad": inverse.ravel().tolist(),
                      "forward": forward.ravel().tolist(), "inverse": inverse.ravel().tolist(),
                      "scale": scale, "center_mm": center, "rotation": np.eye(3).ravel().tolist(),
                      "engine_extent": engine_extent, "cad_bbox": bbox, "placement": "baked_OBJ_no_config_pose",
                      "engine_units_per_mm": scale, "mm_per_engine_unit": 1/scale,
                      "physics_reference_mm": {"contact_eps": 0.1/scale, "sdf_dx": 0.05/scale, "path_collision_threshold": 0.01/scale, "minimum_separation": 0.5/scale},
                      "physics_reference_note": "Reference upstream defaults only. Actual bridge configuration/provenance must confirm values."}
    probe_numerical_budget = _probe_numerical_budget(coordinate_map)
    settings = {"deflection_mm": deflection, "angular_deflection_rad": angular, "engine_extent": engine_extent,
                "mesh_weld_tolerance_mm": 1e-6, "normalization": "one_common_scale_and_center", "automatic_hole_repair": False,
                "adaptive_meshing": bool(options.get("adaptive_meshing", True)), "multi_solid_policy": union_policy, "union_tolerance_mm": union_tolerance,
                "brep_repair_policy": repair_policy,
                "initial_penetration_limits": penetration_limits}
    for relative in ("obj_raw", "obj_analysis"):
        folder = directory / relative
        folder.mkdir(exist_ok=True)
        # Remove only generated numeric OBJ files in the verified session folder.
        for old in folder.glob("*.obj"):
            if re.fullmatch(r"\d+\.obj", old.name):
                old.unlink()
    solids_folder = _local(directory, "obj_solids")
    solids_folder.mkdir(exist_ok=True)
    for old in solids_folder.iterdir():
        if old.is_dir() and re.fullmatch(r"\d+", old.name):
            verified = _local(directory, "obj_solids/"+old.name)
            shutil.rmtree(verified)
    existing_ids = manifest.get("engine_ids", {})
    next_number = 1+max((int(value) for value in existing_ids.values()), default=-1)
    part_map, meshes, per_unit, definition_meshes, analysis_units = {}, [], [], {}, []
    solids_manifest = {"schema_version": 1, "coordinate_system": "RH_Z_UP_ENGINE_UNITS", "mapping": "identity_baked_engine_world",
                       "source_occurrence_count": len(units), "source_solid_count": sum(unit["solid_count"] for unit in units),
                       "source_sha256": manifest["source"]["sha256"], "bodies": {},
                       "original_solid_partition": "OCCT_source_solid_membership_no_inferred_segmentation",
                       "required_engine_capabilities": []}
    for index, unit in enumerate(units):
        id_ = unit["id"]
        if id_ not in existing_ids:
            existing_ids[id_] = f"{next_number:05d}"
            next_number += 1
        engine_id = existing_ids[id_]
        part_map[engine_id] = {"id": id_, "occurrence_id": id_, "name": unit["name"], "raw_name": unit.get("raw_name", unit["name"]),
                               "name_encoding_recovery": unit.get("name_encoding_recovery"),
                               "definition_id": unit["definition_id"], "parent_id": unit.get("parent_id"), "engine_id": engine_id}
        part_map[engine_id].update(unit_id=id_, unit_revision=manifest["unit_revision"],
                                  member_refs=unit.get("member_refs", [{"occurrence_id": id_}]),
                                  member_instance_ids=list(dict.fromkeys(m["occurrence_id"] for m in unit.get("member_refs", [{"occurrence_id": id_}]))))
        part_map[engine_id].update(original_names=unit.get("original_names", [unit["name"]]),
                                  definition_ids=unit.get("definition_ids", [unit["definition_id"]]))
        _progress("analysis_mesh", 0.05+0.75*(index+1)/max(1, len(units)), unit["name"], unit_index=index+1, unit_count=len(units))
        try:
            definition_id = unit["definition_id"]
            definition_reused = definition_id in definition_meshes
            transform = np.asarray(unit["transform_world"], dtype=float).reshape(4, 4)
            if definition_id not in definition_meshes:
                shape = _load_brep(directory, unit)
                original_source_tolerance = _source_max_edge_vertex_tolerance(shape)
                first_mesh = _tessellate(shape, deflection, angular, adaptive=bool(options.get("adaptive_meshing", True)), validate_topology=unit["valid"])
                local_raw = _transform_mesh(first_mesh, np.linalg.inv(transform))
                analysis_raw, union_audit, analysis_volume = local_raw, {"status": "not_requested"}, unit.get("volume_mm3")
                repair_audit = {"status": "already_valid" if unit["valid"] else "not_requested", "source_modified": False}
                analysis_features, analysis_shape = shape_features(shape), shape
                repair_path = None
                if not unit["valid"] and repair_policy == "verified_topology":
                    repaired, repair_audit = verified_topology_repair(shape)
                    repair_path = "cad_repair/"+definition_id+".json"
                    _write_json(_local(directory, repair_path), repair_audit)
                    if repaired is not None and repair_audit["status"] == "accepted":
                        candidate_features = shape_features(repaired)
                        if candidate_features["solid_count"] != unit["solid_count"] or candidate_features["face_count"] != unit["face_count"]:
                            raise ValueError("CAD_REPAIR_OCCURRENCE_TOPOLOGY_CHANGED: source solids/faces cannot be omitted")
                        shape, analysis_shape, analysis_features = repaired, repaired, candidate_features
                        analysis_volume = candidate_features["volume_mm3"]
                        analysis_raw = _transform_mesh(_tessellate(repaired, deflection, angular, adaptive=bool(options.get("adaptive_meshing", True)), validate_topology=True), np.linalg.inv(transform))
                local_solids = [_moved_by_matrix(solid, np.linalg.inv(transform)) for solid in _solids(shape)]
                original_solid_features = [shape_features(solid) for solid in local_solids]
                original_components = _partition_original_solids(analysis_raw, original_solid_features, deflection) if original_solid_features else []
                for component, solid in zip(original_components, local_solids):
                    component["occupancy_probes"], component["occupancy_probe_audit"] = _solid_occupancy_probes(
                        solid, numerical_clearance_mm=probe_numerical_budget["numerical_clearance_mm"], return_audit=True,
                        original_tolerance_mm=original_source_tolerance)
                    component["occupancy_probe_audit"].update(numerical_budget=probe_numerical_budget,
                        original_tolerance_scope="source_definition_maximum_before_verified_topology_repair")
                    component["shell_counts"] = _solid_shell_counts(solid)
                if repair_audit["status"] == "accepted":
                    accepted = next(attempt for attempt in repair_audit["attempts"] if attempt["method"] == repair_audit["accepted_method"])
                    source_mapping = accepted["source_solid_preservation_map"]["source_to_candidate"]
                    reverse_indices = {entry["candidate_solid_index"]-1: entry["source_solid_index"]-1 for entry in source_mapping}
                    for component in original_components:
                        component["source_solid_index"] = reverse_indices[component["source_solid_index"]]
                    original_components.sort(key=lambda component: component["source_solid_index"])
                occupancy_audit = audit_solid_occupancy(shape, options) if unit["solid_count"] > 1 else {"status": "not_required", "solid_count": 1,
                    "positive_overlap_pair_count": 0, "unresolved_pair_count": 0, "sdf_parity_union_equivalent": True}
                if union_policy == "verified_union" and unit["solid_count"] > 1:
                    united, union_audit = verified_solid_union(shape, union_tolerance)
                    if united is not None and union_audit["status"] == "accepted":
                        union_relative = "analysis_brep/" + definition_id + "_union.brep"
                        union_path = _local(directory, union_relative)
                        union_path.parent.mkdir(exist_ok=True)
                        if not BRepTools.Write_s(united, str(union_path)):
                            raise ValueError("CAD_UNION_CACHE_WRITE_FAILED")
                        union_audit.update(analysis_brep_path=union_relative, reference_occurrence_id=id_, reference_transform_world=unit["transform_world"])
                        analysis_raw = _transform_mesh(_tessellate(united, deflection, angular, adaptive=bool(options.get("adaptive_meshing", True)), validate_topology=True), np.linalg.inv(transform))
                        analysis_volume = union_audit["analysis_features"]["volume_mm3"]
                if union_audit["status"] != "accepted" and original_components:
                    local_vertices, local_faces = _concat_components(original_components)
                    preprocessing = {"operation": "original_solid_partition_and_shell_preserving_preprocessing", "source_preserved": True,
                                     "faces_removed": sum(component["preprocessing"].get("faces_removed", 0) for component in original_components),
                                     "holes_filled": 0, "shell_vertex_identity_preserved": True,
                                     "components": [component["preprocessing"] for component in original_components]}
                else:
                    local_vertices, local_faces, preprocessing = _weld_mesh(analysis_raw)
                    local_vertices, local_faces, preprocessing = _remove_zero_area(local_vertices, local_faces, preprocessing)
                used_vertices = np.unique(local_faces)
                remap = np.full(len(local_vertices), -1, dtype=np.int64)
                remap[used_vertices] = np.arange(len(used_vertices))
                preprocessing["unreferenced_vertices_removed"] = len(local_vertices)-len(used_vertices)
                local_vertices, local_faces = local_vertices[used_vertices], remap[local_faces]
                definition_meshes[definition_id] = (local_raw, local_vertices, local_faces, preprocessing, union_audit, analysis_volume, analysis_raw.get("tessellation_audit", {}), original_components, occupancy_audit, repair_audit, repair_path, analysis_features, analysis_shape, transform)
            local_raw, local_vertices, local_faces, preprocessing, union_audit, analysis_volume, analysis_tessellation, original_components, occupancy_audit, repair_audit, repair_path, analysis_features, analysis_shape, reference_transform = definition_meshes[definition_id]
            raw = _transform_mesh(local_raw, transform)
            vertices = local_vertices @ transform[:3, :3].T + transform[:3, 3]
            faces = local_faces[:, [0, 2, 1]] if np.linalg.det(transform[:3, :3]) < 0 else local_faces
            quality = mesh_quality(vertices, faces, {**unit, "valid": analysis_features["valid"], "volume_mm3": analysis_volume}, deflection)
            quality.update(unit_id=id_, engine_id=engine_id, name=unit["name"], preprocessing=preprocessing)
            quality["definition_mesh_reused"] = definition_reused
            quality["tessellation_audit"] = analysis_tessellation
            quality["source_cad_volume_mm3"] = unit.get("volume_mm3")
            quality.update(source_brep_valid=unit["valid"], analysis_brep_valid=analysis_features["valid"],
                           topology_repair_audit=repair_audit, topology_repair_audit_path=repair_path)
            if repair_audit.get('status')=='accepted':
                accepted_checks=next(a for a in repair_audit['attempts'] if a['method']==repair_audit['accepted_method'])
                quality.update(source_exact_brep_valid=repair_audit['source'].get('exact_method_valid'),
                               analysis_exact_brep_valid=accepted_checks.get('exact_method_valid'))
                if accepted_checks.get('exact_method_valid') is False:quality['warnings'].append('EXACT_BREP_REVIEW_REQUIRED')
            analysis_unit = dict(unit)
            if repair_audit["status"] == "accepted":
                repaired_placed = _moved_by_matrix(analysis_shape, transform @ np.linalg.inv(reference_transform))
                relative = "analysis_brep/"+id_+"_topology.brep"
                path = _local(directory, relative)
                path.parent.mkdir(exist_ok=True)
                if not BRepTools.Write_s(repaired_placed, str(path)):
                    raise ValueError("CAD_REPAIR_CACHE_WRITE_FAILED")
                analysis_unit.update(**shape_features(repaired_placed), brep_path=relative)
                quality["analysis_brep_path"] = relative
                quality["warnings"].append("VERIFIED_TOPOLOGY_REPAIR_SOURCE_RETAINED")
            part_map[engine_id].update(source_brep_valid=unit["valid"], analysis_brep_valid=analysis_features["valid"],
                                      source_solid_count=unit["solid_count"], source_brep_path=unit.get("brep_path"),
                                      analysis_brep_path=quality.get("analysis_brep_path"), topology_repair_audit_path=repair_path)
            quality["representation_audit"] = union_audit
            quality["internal_occupancy_audit"] = occupancy_audit
            component_records, occupied_vertices, occupied_faces, component_offset = [], [], [], 0
            component_folder = _local(directory, "obj_solids/"+engine_id)
            component_folder.mkdir(exist_ok=True)
            for component in original_components:
                component_vertices = component["vertices"] @ transform[:3, :3].T + transform[:3, 3]
                component_faces = component["faces"][:, [0, 2, 1]] if np.linalg.det(transform[:3, :3]) < 0 else component["faces"]
                # Component geometry is validated in definition-local mm against
                # the actual local BREP bounds, before its occurrence placement.
                component_quality = mesh_quality(component["vertices"], component["faces"], component["features"], deflection)
                component_quality["preprocessing"] = component["preprocessing"]
                if component["occupancy_probe_audit"]["status"] != "passed":
                    component_quality["errors"].append("SOURCE_OCCUPANCY_UNRESOLVED")
                    component_quality["status"] = "blocked"
                    if "SOURCE_OCCUPANCY_UNRESOLVED" not in quality["errors"]:
                        quality["errors"].append("SOURCE_OCCUPANCY_UNRESOLVED")
                    quality["status"] = "blocked"
                engine_component_vertices = scale*(component_vertices-np.asarray(center))
                relative = "obj_solids/"+engine_id+f"/{component['source_solid_index']:05d}.obj"
                component_path = _local(directory, relative)
                _write_obj(component_path, engine_component_vertices, component_faces)
                component_records.append({"id": "solid_"+str(component["source_solid_index"]), "source_solid_index": component["source_solid_index"],
                    "path": relative, "sha256": hashlib.sha256(component_path.read_bytes()).hexdigest(), "mapping": "identity_baked_engine_world",
                    "closed": component_quality["watertight"], "positive_volume": component_quality["signed_volume_mm3"] > 0,
                    "source_triangle_count": component["source_triangle_count"], "original_shell_count": component["original_shell_count"],
                    **component["shell_counts"], "quality": component_quality,
                    "occupancy_probes": [{"point_engine_world": (scale*(np.asarray(probe["point_local_cad_mm"]) @ transform[:3, :3].T + transform[:3, 3]-np.asarray(center))).tolist(),
                                          **{key: value for key, value in probe.items() if key != "point_local_cad_mm"}} for probe in component.get("occupancy_probes", [])],
                    "occupancy_probe_audit": {**component["occupancy_probe_audit"], "excluded_candidates": [
                        {**candidate, "point_engine_world": (scale*(np.asarray(candidate["point_local_cad_mm"]) @ transform[:3, :3].T + transform[:3, 3]-np.asarray(center))).tolist()}
                        for candidate in component["occupancy_probe_audit"]["excluded_candidates"]]},
                    "triangle_geometry_sha256": _triangle_geometry_digest(engine_component_vertices, component_faces)})
                occupied_vertices.append(engine_component_vertices)
                occupied_faces.append(component_faces+component_offset)
                component_offset += len(engine_component_vertices)
                if component_quality["status"] != "ready" and "ORIGINAL_SOLID_MESH_INVALID" not in quality["errors"]:
                    quality["errors"].append("ORIGINAL_SOLID_MESH_INVALID")
                    quality["status"] = "blocked"
            combined_components = _triangle_geometry_digest(np.vstack(occupied_vertices), np.vstack(occupied_faces)) if occupied_vertices else None
            whole_digest = _triangle_geometry_digest(scale*(vertices-np.asarray(center)), faces)
            same_triangles = whole_digest == combined_components
            if unit["solid_count"] and not same_triangles and union_audit["status"] != "accepted":
                quality["errors"].append("ORIGINAL_SOLID_PARTITION_GEOMETRY_MISMATCH")
                quality["status"] = "blocked"
            if unit["solid_count"] > 1 and union_audit["status"] != "accepted":
                if "solid_or_sdf_v1" not in solids_manifest["required_engine_capabilities"]:
                    solids_manifest["required_engine_capabilities"].append("solid_or_sdf_v1")
                quality["warnings"].append("ORIGINAL_SOLID_OR_SDF_REQUIRED")
            solids_manifest["bodies"][engine_id] = {"unit_id": id_, "definition_id": definition_id, "components": component_records,
                "source_solid_count": unit["solid_count"], "status": "ready" if component_records and all(c["quality"]["status"] == "ready" for c in component_records) else "blocked",
                "audit": {"partition_method": "exact_original_OCCT_solid_triangle_membership", "all_original_solids_preserved": len(component_records) == unit["solid_count"],
                    "whole_mesh_triangle_sha256": whole_digest, "component_concat_triangle_sha256": combined_components,
                    "component_concat_exactly_equals_whole_mesh": same_triangles, "occupied_union_equivalence_audit": union_audit,
                    "internal_occupancy_audit": occupancy_audit, "cavity_shell_winding_preserved": True,
                    "topology_repair_audit_path": repair_path, "topology_repair_audit": repair_audit,
                    "source_brep_valid": unit["valid"], "analysis_brep_valid": analysis_features["valid"],
                    "original_cad_brep_preserved": True, "source_sha256": manifest["source"]["sha256"]}}
            if union_audit["status"] == "accepted":
                quality["warnings"].append("VERIFIED_SOLID_UNION")
            elif union_audit["status"] == "rejected":
                quality["warnings"].append("SOLID_UNION_REJECTED_SOURCE_RETAINED")
            if preprocessing.get("faces_removed"):
                quality["warnings"].append("ZERO_AREA_TRIANGLES_REMOVED")
            if raw.get("tessellation_audit", {}).get("zero_area_faces_without_triangles"):
                quality["warnings"].append("ZERO_AREA_BREP_FACES")
            if analysis_tessellation.get("coarser_than_requested"):
                quality["warnings"].append("ADAPTIVE_COARSER_MESH")
            if analysis_tessellation.get("angular_coarser_than_requested"):
                quality["warnings"].append("ADAPTIVE_COARSER_ANGULAR_MESH")
            # Preserve raw tessellation before coincidence welding, in CAD mm.
            _write_obj(directory / "obj_raw" / (engine_id+".obj"), np.asarray(raw["positions"]).reshape(-1, 3), np.asarray(raw["indices"]).reshape(-1, 3))
            engine_vertices = scale*(vertices-np.asarray(center))
            _write_obj(directory / "obj_analysis" / (engine_id+".obj"), engine_vertices, faces)
            restored = engine_vertices/scale+np.asarray(center)
            quality["roundtrip_max_error_mm"] = float(np.max(np.abs(restored-vertices)))
            meshes.append({"id": id_, "name": unit["name"], "color": unit["color"], "positions": restored.ravel().tolist(),
                           "normals": _vertex_normals(restored, faces).ravel().tolist(), "indices": faces.ravel().tolist()})
            per_unit.append(quality)
            analysis_units.append(analysis_unit)
        except Exception as exc:
            analysis_units.append(unit)
            per_unit.append({"unit_id": id_, "engine_id": engine_id, "name": unit["name"], "status": "blocked",
                             "errors": ["ANALYSIS_MESH_FAILED"], "warnings": [], "reason": str(exc)})
    penetration = _initial_penetration(directory, analysis_units, options)
    blocking = [{"unit_id": q["unit_id"], "code": code} for q in per_unit for code in q["errors"]]
    warnings = [{"unit_id": q["unit_id"], "code": code} for q in per_unit for code in q["warnings"]]
    for diagnostic in manifest["diagnostics"]:
        if diagnostic["severity"] == "error" and not diagnostic.get("unit_id"):
            blocking.append({"code": diagnostic["code"]})
    if len(units) < 2:
        blocking.append({"code": "INSUFFICIENT_PARTS"})
    if len(meshes) != len(units):
        blocking.append({"code": "MESH_COUNT_MISMATCH"})
    exported_solid_count = sum(len(body["components"]) for body in solids_manifest["bodies"].values())
    solids_manifest.update(exported_occurrence_count=len(solids_manifest["bodies"]), exported_solid_count=exported_solid_count,
                           all_original_solids_preserved=exported_solid_count == solids_manifest["source_solid_count"] and len(solids_manifest["bodies"]) == len(units))
    if not solids_manifest["all_original_solids_preserved"]:
        blocking.append({"code": "ORIGINAL_SOLID_COUNT_MISMATCH"})
    if penetration["penetration_pairs"]:
        warnings.append({"code": "INITIAL_PENETRATION", "pair_count": len(penetration["penetration_pairs"]),
                         "message": "CAD 원본에 실제 솔리드 겹침이 있습니다. ASAP 접촉 허용치는 이 진단을 정확한 간섭 없음으로 바꾸지 않습니다."})
    if penetration["status"] != "complete":
        warnings.append({"code": "INITIAL_PENETRATION_PARTIAL", "unresolved_pair_count": penetration["unresolved_pair_count"]})
    warnings.append({"code": "RIGID_APPROXIMATION", "message": "각 단위를 강체로 근사합니다. 유연체 변형, 체결력, 공구 접근은 미검증입니다."})
    mesh_hasher = hashlib.sha256()
    for path in sorted((directory / "obj_analysis").glob("*.obj")):
        mesh_hasher.update(path.name.encode())
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024*1024), b""):
                mesh_hasher.update(chunk)
    for body in solids_manifest["bodies"].values():
        for component in body["components"]:
            mesh_hasher.update(component["path"].encode())
            mesh_hasher.update(component["sha256"].encode())
    revision = hashlib.sha256(json.dumps({"adapter_version": VERSION, "source": manifest["source"]["sha256"], "unit_revision": manifest["unit_revision"], "settings": settings, "mesh_hash": mesh_hasher.hexdigest()}, sort_keys=True).encode()).hexdigest()
    solids_manifest["conversion_revision"] = revision
    quality = {"schema_version": 1, "adapter_version": VERSION, "status": "blocked" if blocking else "ready", "unit_count": len(units), "mesh_count": len(meshes),
               "units": per_unit, "blocking": blocking, "warnings": warnings, "initial_penetration": penetration,
               "source_sha256": manifest["source"]["sha256"], "revision": revision, "settings": settings, "elapsed_s": time.perf_counter()-started}
    solids_manifest.update(status=quality["status"], ready_for_physics=not blocking,
                           geometry_verified=not blocking, native_occupancy_cache_verified=False,
                           physics_prerequisites=solids_manifest["required_engine_capabilities"],
                           readiness_basis="CAD geometry eligibility only; required bridge capabilities and native occupancy-cache proof must pass before physics.")
    summary = {"adapter_version": VERSION, "status": quality["status"], "ready": not blocking, "reason_code": blocking[0]["code"] if blocking else None,
               "part_count": len(units), "mesh_count": len(meshes), "blocking_count": len(blocking), "warning_count": len(warnings),
               "quality": quality, "part_map": part_map, "coordinate_map": coordinate_map, "settings": settings, "revision": revision,
               "assembly_dir": "obj_analysis", "elapsed_s": quality["elapsed_s"]}
    summary.update(solids_manifest_path="solids_manifest.json", original_solid_count=solids_manifest["source_solid_count"],
                   exported_solid_count=exported_solid_count, required_engine_capabilities=solids_manifest["required_engine_capabilities"])
    quality.update(required_engine_capabilities=solids_manifest["required_engine_capabilities"],
                   original_solid_count=solids_manifest["source_solid_count"], exported_solid_count=exported_solid_count)
    _write_json(directory / "obj_scene.json", {"coordinate_system": "RH_Z_UP_MM", "bbox": bbox, "meshes": meshes})
    _write_json(directory / "part_map.json", part_map)
    _write_json(directory / "coordinate_map.json", coordinate_map)
    _write_json(directory / "quality_report.json", quality)
    _write_json(directory / "conversion.json", summary)
    _write_json(directory / "solids_manifest.json", solids_manifest)
    manifest.update(engine_ids=existing_ids, conversion=summary, bbox=bbox, conversion_revision=revision,
                    conversion_settings_sha256=hashlib.sha256(json.dumps(settings, sort_keys=True).encode()).hexdigest())
    _write_json(directory / "manifest.json", manifest)
    _progress("conversion_ready", 1, "OBJ 변환·진단 완료", status=quality["status"], blocking_count=len(blocking))
    return summary


def split_units(session_dir: Path, unit_ids: list[str]) -> dict:
    directory = Path(session_dir).resolve()
    manifest, units, scene = _read_json(directory / "manifest.json"), _read_json(directory / "units.json"), _read_json(directory / "display_scene.json")
    requested = set(unit_ids)
    if not requested or not requested.issubset({unit["id"] for unit in units}):
        raise ValueError("CAD_UNKNOWN_UNIT: select existing multi-solid occurrence IDs")
    replacements, replacement_meshes, split_records = {}, {}, []
    for unit in units:
        if unit["id"] not in requested:
            continue
        if unit["solid_count"] < 2:
            raise ValueError("CAD_NOT_MULTI_SOLID: selected unit has fewer than two solids")
        shape = _load_brep(directory, unit)
        if _count(shape, TopAbs_FACE) != sum(_count(solid, TopAbs_FACE) for solid in _solids(shape)):
            raise ValueError("CAD_SPLIT_WOULD_OMIT_GEOMETRY: contains faces outside the independent solids")
        children, child_meshes = [], []
        for index, solid in enumerate(_solids(shape), 1):
            child_id = unit["id"] + "_s" + str(index)
            child = {**unit, **shape_features(solid), "id": child_id, "name": unit["name"] + f" / 솔리드 {index}",
                     "definition_id": unit["definition_id"] + "_s" + str(index), "parent_id": unit["id"],
                     "source_occurrence_id": unit.get("source_occurrence_id", unit["id"]), "split_index": index,
                     "split_provenance": "user_requested_independent_solid_split", "color": _color(child_id),
                     "brep_path": _save_brep(directory, child_id, solid)}
            child_meshes.append({"id": child_id, "name": child["name"], "color": child["color"], **_tessellate(solid, 0.35, 0.35)})
            children.append(child)
        replacements[unit["id"]], replacement_meshes[unit["id"]] = children, child_meshes
        split_records.append({"parent_id": unit["id"], "parent": unit, "child_ids": [child["id"] for child in children]})
    new_units, new_meshes = [], []
    for unit in units:
        new_units.extend(replacements.get(unit["id"], [unit]))
    for mesh in scene["meshes"]:
        new_meshes.extend(replacement_meshes.get(mesh["id"], [mesh]))
    # A previously missing parent display mesh still gets its new child meshes.
    for parent in replacements:
        if not any(mesh["id"] == parent for mesh in scene["meshes"]):
            new_meshes.extend(replacement_meshes[parent])
    manifest["split_history"] = manifest.get("split_history", []) + split_records
    manifest["unit_revision"] += 1
    manifest["conversion"] = None
    manifest["conversion_revision"] = None
    manifest["diagnostics"] = [d for d in manifest["diagnostics"] if d.get("unit_id") not in requested]
    for child in new_units:
        if child["id"] not in {u["id"] for u in units} and not child["valid"]:
            manifest["diagnostics"].append(_diagnostic("INVALID_BREP", "error", "분할된 솔리드의 OCCT 유효성 검사에 실패했습니다.", child["id"]))
    manifest["bbox"] = combine_bbox([u["bbox"] for u in new_units])
    manifest["metrics"]["unit_count"] = len(new_units)
    manifest["metrics"]["display_mesh_count"] = len(new_meshes)
    manifest["status"] = "partial" if any(d["severity"] == "error" for d in manifest["diagnostics"]) else "ready"
    _write_json(directory / "units.json", new_units)
    _write_json(directory / "display_scene.json", {"coordinate_system": "RH_Z_UP_MM", "bbox": manifest["bbox"], "meshes": new_meshes})
    _write_json(directory / "manifest.json", manifest)
    # Existing run directories remain immutable; stale conversion artifacts are
    # removed so an API cannot accidentally use the pre-split OBJ set.
    for relative in ("conversion.json", "coordinate_map.json", "part_map.json", "quality_report.json", "obj_scene.json", "solids_manifest.json"):
        (directory / relative).unlink(missing_ok=True)
    for relative in ("obj_raw", "obj_analysis"):
        folder = directory / relative
        if folder.exists():
            for path in folder.glob("*.obj"):
                if re.fullmatch(r"\d+\.obj", path.name):
                    path.unlink()
    solids_folder = _local(directory, "obj_solids")
    if solids_folder.exists():
        for path in solids_folder.iterdir():
            if path.is_dir() and re.fullmatch(r"\d+", path.name):
                shutil.rmtree(_local(directory, "obj_solids/"+path.name))
    return {"status": manifest["status"], "unit_count": len(new_units), "splits": split_records, "conversion_invalidated": True}


def _solids(shape):
    result, iterator = [], TopExp_Explorer(shape, TopAbs_SOLID)
    while iterator.More():
        result.append(iterator.Current())
        iterator.Next()
    return result
