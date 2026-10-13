"""Preserve original closed solids as a union in an upstream single-body SDF.

No native physics equations are replaced. Each original solid (including its
negative cavity shells) is sampled by the pinned BodySDFObj. Those fields are
transformed into the combined body's native inertia frame and min-composed.
The cache uses the original native format and is verified through native reload.
"""
from __future__ import annotations

import hashlib
import copy
import json
import math
from pathlib import Path, PurePosixPath
import tempfile
import time
import xml.etree.ElementTree as ET

CAPABILITY = "solid_or_sdf_v1"
PREPROCESS_VERSION = "automatic-preprocess/0.3.0"
MAX_GRID_CELLS = 50_000_000
MAX_GRID_AXIS = 10000


class SolidSDFError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.reason_code = code


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    temporary.replace(target)


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise SolidSDFError("TIMEOUT", "원본 솔리드 점유 SDF 준비 중 실행 제한시간을 초과했습니다.")


class NativeGrid:
    def __init__(self, dimensions, minimum, maximum, spacing, values):
        import numpy as np
        self.dimensions = tuple(int(item) for item in dimensions)
        self.minimum = np.asarray(minimum, dtype=float)
        self.maximum = np.asarray(maximum, dtype=float)
        self.spacing = np.asarray(spacing, dtype=float)
        self.values = np.asarray(values, dtype=float).reshape(self.dimensions, order="F")
        if (len(self.dimensions) != 3 or min(self.dimensions) < 2
                or not np.isfinite(self.minimum).all() or not np.isfinite(self.maximum).all()
                or not np.isfinite(self.spacing).all() or (self.spacing <= 0).any()
                or (self.minimum >= self.maximum).any() or not np.isfinite(self.values).all()):
            raise SolidSDFError("SDF_CACHE_INVALID", "네이티브 SDF 격자 형식/수치가 유효하지 않습니다.")

    def validate_native_domain(self):
        import numpy as np
        maximum_index = (self.maximum-self.minimum)/self.spacing
        lower = np.floor(maximum_index+1e-10).astype(np.int64)
        last = np.asarray(self.dimensions)-1
        if (lower < 0).any() or (lower > last).any():
            raise SolidSDFError("SDF_CACHE_DOMAIN_MISMATCH", "SDF 최대 경계의 인덱스가 배열 범위를 벗어납니다: "
                                + json.dumps({"maximum_grid_index":maximum_index.tolist(),"last_valid_index":last.tolist()}))
        return {"status":"passed","maximum_grid_index":maximum_index.tolist(),"last_valid_index":last.tolist(),
                "data_count":int(self.values.size),"dimensions":list(self.dimensions)}

    @classmethod
    def read(cls, path):
        import numpy as np
        with Path(path).open("r", encoding="ascii") as stream:
            dimensions = [int(value) for value in stream.readline().split()]
            minimum = [float(value) for value in stream.readline().split()]
            maximum = [float(value) for value in stream.readline().split()]
            spacing = [float(value) for value in stream.readline().split()]
            if (len(dimensions) != 3 or any(value < 2 for value in dimensions)
                    or math.prod(dimensions) > MAX_GRID_CELLS or max(dimensions) > MAX_GRID_AXIS):
                raise SolidSDFError("SDF_RESOURCE_LIMIT", "원본 점유 SDF 격자가 안전한 입력 범위를 초과합니다.")
            values = np.fromfile(stream, sep=" ")
        if values.size != math.prod(dimensions) or any(len(row) != 3 for row in [minimum, maximum, spacing]):
            raise SolidSDFError("SDF_CACHE_INVALID", "네이티브 SDF 격자 데이터가 불완전합니다.")
        grid = cls(dimensions, minimum, maximum, spacing, values)
        grid.validate_native_domain()
        return grid

    def write(self, path):
        import numpy as np
        self.validate_native_domain()
        target = Path(path)
        temporary = target.with_name(target.name + ".tmp")
        with temporary.open("w", encoding="ascii", newline="\n") as stream:
            stream.write(" ".join(str(value) for value in self.dimensions) + "\n")
            for row in [self.minimum, self.maximum, self.spacing]:
                stream.write(" ".join(format(float(value), ".17g") for value in row) + "\n")
            np.savetxt(stream, self.values.ravel(order="F"), fmt="%.17g")
        temporary.replace(target)
        self.read(target)

    def query(self, points):
        """The native trilinear rule plus boundary-distance extrapolation."""
        import numpy as np
        points = np.asarray(points, dtype=float).reshape((-1, 3))
        boundary = np.minimum(np.maximum(points, self.minimum), self.maximum)
        coords = (boundary - self.minimum) / self.spacing
        coords = np.minimum(np.maximum(coords, 0), np.asarray(self.dimensions) - 1)
        lower = np.floor(coords).astype(np.int64)
        upper = np.minimum(lower + 1, np.asarray(self.dimensions) - 1)
        fraction = coords - lower
        result = np.zeros(len(points), dtype=float)
        for sx in [0, 1]:
            ix = upper[:, 0] if sx else lower[:, 0]
            wx = fraction[:, 0] if sx else 1 - fraction[:, 0]
            for sy in [0, 1]:
                iy = upper[:, 1] if sy else lower[:, 1]
                wy = fraction[:, 1] if sy else 1 - fraction[:, 1]
                for sz in [0, 1]:
                    iz = upper[:, 2] if sz else lower[:, 2]
                    wz = fraction[:, 2] if sz else 1 - fraction[:, 2]
                    result += wx * wy * wz * self.values[ix, iy, iz]
        return result + np.linalg.norm(points - boundary, axis=1)


def transform_points(points, matrix):
    import numpy as np
    return np.asarray(points) @ np.asarray(matrix)[:3, :3].T + np.asarray(matrix)[:3, 3]


def _matrix(value):
    import numpy as np
    result = np.asarray(value, dtype=float).reshape(4, 4)
    if (not np.isfinite(result).all() or not np.allclose(result[3], [0, 0, 0, 1], atol=1e-8)
            or not np.allclose(result[:3, :3].T @ result[:3, :3], np.eye(3), atol=1e-6)
            or not np.isclose(np.linalg.det(result[:3, :3]), 1, atol=1e-6)):
        raise SolidSDFError("SDF_FRAME_INVALID", "원본 솔리드 SDF의 관성 좌표 변환이 강체 변환이 아닙니다.")
    return result


def _mesh(path):
    import numpy as np
    import trimesh
    mesh = trimesh.load_mesh(str(path), process=False, maintain_order=True)
    if (not isinstance(mesh, trimesh.Trimesh) or not len(mesh.faces)
            or not np.isfinite(mesh.vertices).all() or not mesh.is_watertight
            or not mesh.is_winding_consistent or not math.isfinite(float(mesh.volume)) or mesh.volume <= 0):
        raise SolidSDFError("SOLID_COMPONENT_INVALID", "원본 솔리드 OBJ가 닫힌 양의 점유 형상이 아닙니다: " + str(path))
    return mesh


def triangle_fingerprint(mesh, quantum=1e-8):
    """Order-independent oriented triangle checksum without welding vertices.

    Two independent uint64 checksums plus the count are evidence of unchanged
    triangle payload, not a repair. Coordinates are quantized only for comparison.
    Input file SHA256s remain separately recorded.
    """
    import numpy as np
    total0, total1, count = 0, 0, 0
    mask = (1 << 64) - 1
    for start in range(0, len(mesh.faces), 65536):
        triangles = np.rint(mesh.vertices[mesh.faces[start:start + 65536]] / quantum).astype(np.int64)
        # Canonical cyclic rotation keeps orientation while ignoring face order.
        first = np.lexsort((triangles[:, :, 2], triangles[:, :, 1], triangles[:, :, 0]), axis=1)[:, 0]
        order = (first[:, None] + np.arange(3)[None, :]) % 3
        triangles = np.take_along_axis(triangles, order[:, :, None], axis=1).reshape((-1, 9)).view(np.uint64)
        with np.errstate(over="ignore"):
            a = np.full(len(triangles), np.uint64(0xCBF29CE484222325), dtype=np.uint64)
            b = np.full(len(triangles), np.uint64(0x9E3779B97F4A7C15), dtype=np.uint64)
            for column in range(9):
                a = (a ^ triangles[:, column]) * np.uint64(0x100000001B3)
                b = (b ^ (triangles[:, column] + np.uint64(column + 1))) * np.uint64(0x9E3779B185EBCA87)
            total0 = (total0 + int(a.sum(dtype=np.uint64))) & mask
            total1 = (total1 + int(b.sum(dtype=np.uint64))) & mask
        count += len(triangles)
    return {"triangle_count": count, "oriented_coordinate_checksum_u64": [format(total0, "016x"), format(total1, "016x")], "comparison_quantum_engine": quantum}


def combine_fingerprints(values):
    mask = (1 << 64) - 1
    return {"triangle_count": sum(value["triangle_count"] for value in values),
            "oriented_coordinate_checksum_u64": [format(sum(int(value["oriented_coordinate_checksum_u64"][column], 16) for value in values) & mask, "016x") for column in range(2)],
            "comparison_quantum_engine": values[0]["comparison_quantum_engine"]}


def body_xml(obj_path, parameters, name="solid", load=False, save=True, probes=None):
    root = ET.Element("redmax", {"model": "original-solid-sdf-cache"})
    ET.SubElement(root, "option", {"integrator": "BDF1", "timestep": "1e-3", "gravity": "0 0 0"})
    robot = ET.SubElement(root, "robot")
    link = ET.SubElement(robot, "link", {"name": name})
    ET.SubElement(link, "joint", {"name": name, "type": "fixed", "pos": "0 0 0", "quat": "1 0 0 0", "frame": "WORLD"})
    ET.SubElement(link, "body", {"name": name, "type": "SDF", "filename": str(Path(obj_path).resolve()),
        "pos": "0 0 0", "quat": "1 0 0 0", "scale": "1 1 1", "transform_type": "OBJ_TO_JOINT",
        "density": "1", "dx": format(parameters["sdf_dx"], ".17g"), "res": str(parameters["sdf_res"]),
        "col_th": "0", "load_sdf": "true" if load else "false", "save_sdf": "true" if save else "false"})
    for index, point in enumerate(probes or []):
        robot = ET.SubElement(root, "robot")
        link = ET.SubElement(robot, "link", {"name": "probe" + str(index)})
        ET.SubElement(link, "joint", {"name": "probe" + str(index), "type": "fixed", "pos": " ".join(format(float(v), ".17g") for v in point), "quat": "1 0 0 0", "frame": "WORLD"})
        ET.SubElement(link, "body", {"name": "probe" + str(index), "type": "cuboid", "size": "0.000001 0.000001 0.000001", "pos": "0 0 0", "quat": "1 0 0 0", "density": "1"})
    return ET.tostring(root, encoding="unicode")


def bvh_body_xml(obj_path, parameters, probes=None):
    """Pinned native closest-triangle body; no dense SDF is requested."""
    root = ET.fromstring(body_xml(obj_path, parameters, save=False, probes=probes))
    body = root.find("./robot/link/body")
    body.set("type", "BVH")
    body.set("BVH_mesh_filename", str(Path(obj_path).resolve()))
    body.attrib.pop("dx", None)
    body.attrib.pop("res", None)
    return ET.tostring(root, encoding="unicode")


def verify_native_bvh(obj_path, parameters, asset_folder, occupancy_probes, expected_frame=None,
                      audit_record=None, persist=None, scale=None):
    """Verify the unchanged single original solid through the native BVH.

    A closest-triangle/pseudonormal field avoids SDF grid interpolation, but is
    still a tessellated geometry query and finite probe proof, not exact CAD or
    a continuous path guarantee. It must never represent overlapping solids.
    """
    import numpy as np
    import redmax_py as redmax
    import trimesh
    mesh = _mesh(obj_path)
    probes = occupancy_probes or []
    if (not isinstance(probes, list) or not probes
            or any(not isinstance(probe, dict) or type(probe.get("inside")) is not bool for probe in probes)
            or {probe["inside"] for probe in probes} != {True, False}):
        raise SolidSDFError("BVH_SOURCE_PROBES_MISSING", "BVH 대체에는 원본 CAD의 내부/외부 점유 표본이 필요합니다.")
    points = np.asarray([probe.get("point_engine_world") for probe in probes], dtype=float)
    if points.shape != (len(probes), 3) or not np.isfinite(points).all():
        raise SolidSDFError("BVH_SOURCE_PROBES_INVALID", "원본 CAD 점유 표본 좌표가 유한한 세계 좌표가 아닙니다.")
    audit = audit_record if audit_record is not None else {}
    cache = Path(obj_path).with_suffix(".sdf")
    existing_cache_hash = sha256(cache) if cache.is_file() else None
    audit.update(status="checking", representation="native_BVH_closest_triangle_pseudonormal",
                 obj_sha256=sha256(obj_path), source_probe_count=len(probes), occupancy_probes=[],
                 sdf_cache_used=False, sdf_cache_written=False, dense_grid_allocated=False,
                 native_distance_comparison_tolerance_engine=2e-5,
                 limitation="Unchanged tessellated single solid, native closest-triangle distance/pseudonormal sign and finite CAD probe validation. No exact CAD or continuous collision/path proof.")
    if scale:
        audit["native_distance_comparison_tolerance_mm"] = 2e-5/scale
    if persist:
        persist()
    sim = redmax.Simulation(bvh_body_xml(obj_path, parameters, probes=points.tolist()), str(asset_folder))
    frame = _matrix(sim.get_body_E0i("solid"))
    native_vertices = np.asarray(sim.get_body_vertices("solid", True)).T
    native_faces = np.asarray(sim.get_body_faces("solid")).T
    tolerance = max(1e-7, float(np.max(np.abs(mesh.vertices))) * np.finfo(np.float32).eps * 4)
    if (native_vertices.shape != mesh.vertices.shape or native_faces.shape != mesh.faces.shape
            or not np.array_equal(native_faces, mesh.faces)):
        audit.update(status="failed", reason_code="NATIVE_GEOMETRY_CHANGED")
        if persist:
            persist()
        raise SolidSDFError("NATIVE_GEOMETRY_CHANGED", "BVH 로딩에서 원본 정점/삼각형/방향이 변경되었습니다.")
    deviation = float(np.max(np.abs(native_vertices-mesh.vertices)))
    audit.update(native_world_vertex_max_abs_deviation_engine=deviation,
                 native_world_vertex_tolerance_engine=tolerance, native_vertex_count=int(len(native_vertices)),
                 native_triangle_count=int(len(native_faces)), native_triangle_indices_and_winding_preserved=True,
                 native_frame_world_row_major=frame.reshape(-1).tolist(), native_mass_density_one=float(sim.get_body_mass("solid")))
    if deviation > tolerance or not math.isfinite(audit["native_mass_density_one"]) or audit["native_mass_density_one"] <= 0:
        audit.update(status="failed", reason_code="NATIVE_GEOMETRY_CHANGED")
        if persist:
            persist()
        raise SolidSDFError("NATIVE_GEOMETRY_CHANGED", "BVH의 원본 세계 좌표 또는 질량이 유효하지 않습니다.")
    if expected_frame is not None:
        expected_frame = _matrix(expected_frame)
        audit["native_reload_frame_max_abs_deviation"] = float(np.max(np.abs(frame-expected_frame)))
        if not np.allclose(frame, expected_frame, atol=1e-8):
            audit.update(status="failed", reason_code="SDF_FRAME_INVALID")
            if persist:
                persist()
            raise SolidSDFError("SDF_FRAME_INVALID", "BVH 대체 시 원본 관성 좌표가 변경되었습니다.")
    audit["native_reload_frame_preserved"] = True
    # Independent unchanged triangle mesh distance and ray occupancy complement
    # the native query; neither can substitute for the source CAD classifier.
    _, mesh_distances, _ = trimesh.proximity.closest_point(mesh, points)
    mesh_inside = mesh.contains(points)
    inverse = np.linalg.inv(frame)
    for index, probe in enumerate(probes):
        measured = float(sim.get_body_distance("probe" + str(index), "solid"))
        expected = -float(mesh_distances[index]) if probe["inside"] else float(mesh_distances[index])
        result = {**probe, "probe_index": index, "point_native_inertia_frame": transform_points([points[index]], inverse)[0].tolist(),
                  "native_bvh_distance_engine": measured if math.isfinite(measured) else None,
                  "unchanged_mesh_surface_distance_engine": float(mesh_distances[index]),
                  "unchanged_mesh_inside": bool(mesh_inside[index]),
                  "native_vs_source_signed_mesh_distance_abs_difference_engine": abs(measured-expected) if math.isfinite(measured) else None,
                  "status": "checking"}
        if scale:
            result.update(native_bvh_distance_mm=measured/scale if math.isfinite(measured) else None,
                          unchanged_mesh_surface_distance_mm=float(mesh_distances[index])/scale)
        audit["occupancy_probes"].append(result)
        if (not math.isfinite(measured) or bool(mesh_inside[index]) != probe["inside"]
                or abs(measured) <= 2e-5 or (measured < 0) != probe["inside"]
                or abs(measured-expected) > 2e-5):
            result.update(status="failed", failure_condition="source_mesh_and_native_BVH_occupancy_or_distance_disagree")
            audit.update(status="failed", reason_code="BVH_OCCUPANCY_MISMATCH", failed_probe_index=index)
            if persist:
                persist()
            raise SolidSDFError("BVH_OCCUPANCY_MISMATCH", "네이티브 BVH의 원본 CAD 점유/거리 검증이 실패했습니다.")
        result["status"] = "passed"
        if persist:
            persist()
    current_cache_hash = sha256(cache) if cache.is_file() else None
    if current_cache_hash != existing_cache_hash:
        audit.update(status="failed", reason_code="BVH_WROTE_SDF_CACHE")
        if persist:
            persist()
        raise SolidSDFError("BVH_WROTE_SDF_CACHE", "BVH 검증이 SDF 진단 캐시를 변경했습니다.")
    audit.update(status="passed", native_geometry_and_occupancy_verified=True)
    if persist:
        persist()
    return audit


def native_grid(obj_path, parameters, asset_folder, verified_cache=None):
    import numpy as np
    import redmax_py as redmax
    mesh = _mesh(obj_path)
    # Guard the dense native allocation before crossing the C++ boundary. This
    # uses the same signed triangle inertia axes; sign flips do not alter extents.
    _, axes = np.linalg.eigh(mesh.moment_inertia)
    extents = np.ptp((mesh.vertices - mesh.center_mass) @ axes, axis=0)
    spacing = np.minimum(parameters["sdf_dx"], extents / parameters["sdf_res"])
    if not np.isfinite(spacing).all() or (spacing <= 0).any():
        raise SolidSDFError("SDF_RESOURCE_LIMIT", "원본 솔리드가 유한한 3D SDF 격자를 만들 수 없습니다.")
    predicted = [int(math.ceil(float(length / delta))) + 4 for length, delta in zip(extents, spacing)]
    if math.prod(predicted) > MAX_GRID_CELLS or max(predicted) > MAX_GRID_AXIS:
        raise SolidSDFError("SDF_RESOURCE_LIMIT", "요청한 물리 정밀도의 네이티브 격자가 입력 자원 한계를 초과합니다. 수치를 자동 완화하지 않았습니다.")
    # Only the explicit provenance-bound helper result permits a cached field
    # to cross the native load boundary. An unrecorded existing file is never
    # trusted. Current occupancy probes are still verified by the caller.
    cache = Path(obj_path).with_suffix(".sdf")
    original_obj_hash = sha256(obj_path)
    reused = verified_cache is not None
    field_parameters = parameters.copy()
    if reused:
        if (not isinstance(verified_cache, dict)
                or verified_cache.get("obj_sha256") != original_obj_hash
                or not isinstance(verified_cache.get("provenance"), dict)
                or verified_cache["provenance"].get("requires_fresh_native_verification") is not True
                or not cache.is_file()
                or sha256(cache) != verified_cache.get("cache_sha256")):
            raise SolidSDFError("CACHE_REUSE_INVALID", "검증된 이전 필드의 OBJ/캐시 해시·물리 설정·출처가 현재 입력과 일치하지 않습니다.")
        previous_parameters = verified_cache.get("previous_cache_parameters")
        if (not isinstance(previous_parameters, dict)
                or set(previous_parameters) != set(parameters)
                or any(previous_parameters[key] != value for key, value in parameters.items() if key not in ("sdf_dx", "sdf_res"))
                or not isinstance(previous_parameters.get("sdf_dx"), (int, float))
                or isinstance(previous_parameters.get("sdf_dx"), bool)
                or not math.isfinite(previous_parameters["sdf_dx"])
                or not 0 < previous_parameters["sdf_dx"] <= parameters["sdf_dx"]
                or not isinstance(previous_parameters.get("sdf_res"), int)
                or isinstance(previous_parameters.get("sdf_res"), bool)
                or previous_parameters["sdf_res"] < parameters["sdf_res"]):
            raise SolidSDFError("CACHE_REUSE_INVALID", "재사용 필드 정밀도가 현재 최대 간격/최소 해상도 또는 물리 설정과 일치하지 않습니다.")
        field_parameters = previous_parameters.copy()
        try:
            expected_frame = _matrix(verified_cache.get("native_frame_world_row_major"))
        except (ValueError, TypeError, SolidSDFError) as error:
            raise SolidSDFError("CACHE_REUSE_INVALID", "이전 검증 필드의 네이티브 관성 좌표가 유효하지 않습니다.") from error
        expected_mass = verified_cache.get("native_mass_density_one")
        if (not isinstance(expected_mass, (int, float)) or isinstance(expected_mass, bool)
                or not math.isfinite(expected_mass) or expected_mass <= 0):
            raise SolidSDFError("CACHE_REUSE_INVALID", "이전 검증 필드의 네이티브 질량이 유효하지 않습니다.")
        # Parse/guard the complete payload before C++ can allocate from it.
        grid = NativeGrid.read(cache)
    else:
        # Recompute only in the dedicated run workspace, never source files.
        if cache.exists():
            cache.unlink()
    sim = redmax.Simulation(body_xml(obj_path, field_parameters, load=reused, save=not reused), str(asset_folder))
    frame = _matrix(sim.get_body_E0i("solid"))
    native_vertices = np.asarray(sim.get_body_vertices("solid", True)).T
    native_faces = np.asarray(sim.get_body_faces("solid")).T
    tolerance = max(1e-7, float(np.max(np.abs(mesh.vertices))) * np.finfo(np.float32).eps * 4)
    if native_vertices.shape != mesh.vertices.shape:
        raise SolidSDFError("NATIVE_GEOMETRY_CHANGED", "네이티브 로딩에서 원본 솔리드 정점이 누락되었습니다.")
    if native_faces.shape != mesh.faces.shape or not np.array_equal(native_faces, mesh.faces):
        raise SolidSDFError("NATIVE_GEOMETRY_CHANGED", "네이티브 로딩에서 원본 삼각형 또는 방향이 누락/변경되었습니다.")
    deviation = float(np.max(np.abs(native_vertices - mesh.vertices)))
    if deviation > tolerance:
        raise SolidSDFError("NATIVE_GEOMETRY_CHANGED", "원본 솔리드의 네이티브 세계 좌표가 보존되지 않았습니다.")
    if not reused:
        grid = NativeGrid.read(cache)
    mass = float(sim.get_body_mass("solid"))
    if not math.isfinite(mass) or mass <= 0:
        raise SolidSDFError("NATIVE_MASS_INVALID", "원본 솔리드의 네이티브 질량이 유효하지 않습니다.")
    if reused:
        if not np.allclose(frame, expected_frame, atol=1e-8, rtol=0):
            raise SolidSDFError("SDF_FRAME_INVALID", "재사용 필드의 원본 네이티브 관성 좌표가 현재 OBJ와 일치하지 않습니다.")
        if not math.isclose(mass, expected_mass, rel_tol=1e-12, abs_tol=1e-12):
            raise SolidSDFError("NATIVE_MASS_CHANGED", "재사용 필드의 원본 네이티브 질량이 현재 OBJ와 일치하지 않습니다.")
        if sha256(cache) != verified_cache["cache_sha256"]:
            raise SolidSDFError("CACHE_REUSE_CHANGED", "네이티브 읽기에서 검증된 재사용 캐시가 변경되었습니다.")
    if sha256(obj_path) != original_obj_hash:
        raise SolidSDFError("SOLID_INPUT_CHANGED", "네이티브 필드 준비 중 원본 OBJ 해시가 변경되었습니다.")
    del sim
    audit = {"obj_sha256": original_obj_hash, "native_world_vertex_max_abs_deviation_engine": deviation,
        "native_triangle_indices_and_winding_preserved": True, "native_triangle_count": int(len(native_faces)),
        "predicted_guard_grid_dimensions": predicted, "predicted_guard_grid_cell_count": math.prod(predicted),
        "predicted_guard_rule": "ceil(each native-inertia bbox extent/min(requested_dx,extent/res))+4; conservative allocation guard",
        "native_world_vertex_tolerance_engine": tolerance, "native_frame_world_row_major": frame.reshape(-1).tolist(),
        "native_mass_density_one": mass, "dimensions": list(grid.dimensions), "actual_grid_cell_count": math.prod(grid.dimensions),
        "spacing_engine": grid.spacing.tolist(), "cache_sha256": sha256(cache),
        "native_field_parameters": field_parameters, "requested_max_sdf_parameters": parameters.copy()}
    if reused:
        audit["cache_reuse"] = {"status": "native_reloaded", "cache_sha256": verified_cache["cache_sha256"],
            "provenance": copy.deepcopy(verified_cache["provenance"]), "native_geometry_frame_mass_verified": True,
            "current_occupancy_verification_skipped": False, "cache_modified_by_native_reload": False}
    return grid, frame, audit


def verify_source_mesh_probes(obj_path, probes, scale=None):
    """Independent current CAD/unchanged-triangle agreement before refinement."""
    import numpy as np
    import trimesh
    mesh = _mesh(obj_path)
    audit = {"status": "checking", "obj_sha256": sha256(obj_path), "method": "unchanged_oriented_mesh_ray_occupancy_and_closest_triangle",
             "decisive_clearance_engine": 2e-5, "probes": [], "CAD_probes_modified": False}
    if not probes:
        audit.update(status="failed", reason_code="SOLID_SOURCE_MESH_MISMATCH")
        return audit
    points = np.asarray([probe.get("point_engine_world") for probe in probes], dtype=float)
    if points.shape != (len(probes), 3) or not np.isfinite(points).all():
        raise SolidSDFError("SOLID_SOURCE_MESH_MISMATCH", "현재 원본 CAD 검증점 좌표가 유효하지 않습니다.")
    inside = mesh.contains(points)
    _, distances, _ = trimesh.proximity.closest_point(mesh, points)
    for index, (probe, occupied, distance) in enumerate(zip(probes, inside, distances)):
        decisive = math.isfinite(float(distance)) and float(distance) > 2e-5
        agrees = type(probe.get("inside")) is bool and bool(occupied) == probe["inside"]
        result = {"index": index, "point_engine_world": points[index].tolist(), "CAD_inside": probe.get("inside"),
                  "unchanged_mesh_inside": bool(occupied), "mesh_boundary_distance_engine": float(distance),
                  "decisive": decisive, "occupancy_agrees": agrees,
                  "status": "passed" if agrees and decisive else "failed"}
        if scale:
            result["mesh_boundary_distance_mm"] = float(distance) / scale
        audit["probes"].append(result)
    audit["status"] = "passed" if all(item["status"] == "passed" for item in audit["probes"]) else "failed"
    if audit["status"] == "failed":
        audit["reason_code"] = "SOLID_SOURCE_MESH_MISMATCH"
    return audit


def verify_component_field(obj_path, parameters, asset_folder, initial_grid, initial_frame, initial_audit,
                           probes, component_report, deadline=None, persist=None, scale=None):
    """Regenerate only official native fields after decisive CAD/mesh agreement."""
    import numpy as np
    field, frame, native_audit = initial_grid, initial_frame, initial_audit
    actual_parameters = native_audit.get("native_field_parameters", parameters).copy()
    expected_mass = initial_audit.get("native_mass_density_one")
    refinement = {"status": "checking", "method": "official_native_original_component_dx_halving_res_doubling",
        "eligible_failure": "SDF_OCCUPANCY_MISMATCH_with_decisive_current_CAD_and_unchanged_mesh_agreement",
        "requested_max_sdf_parameters": parameters.copy(), "source_geometry_unchanged": True,
        "CAD_probes_modified": False, "sign_checks_relaxed": False, "targeted_probe_value_edits": False,
        "resource_guard": {"maximum_grid_cells": MAX_GRID_CELLS, "maximum_axis_nodes": MAX_GRID_AXIS}, "attempts": []}
    component_report["native_field_refinement"] = refinement
    source_mesh_proof = None
    while True:
        check_deadline(deadline)
        proof_record = {"status": "pending"}
        attempt = {"index": len(refinement["attempts"]), "parameters": actual_parameters.copy(),
                   "status": "checking", "native_field": copy.deepcopy(native_audit), "source_occupancy_proof": proof_record}
        refinement["attempts"].append(attempt)
        component_report["source_occupancy_proof"] = proof_record
        if persist:
            persist()
        try:
            proof = verify_native_reload(obj_path, actual_parameters, asset_folder, field, frame,
                [(field, frame)], probes, audit_record=proof_record, persist=persist, scale=scale, expected_mass=expected_mass)
        except SolidSDFError as error:
            attempt.update(status="failed", reason_code=error.reason_code, message=str(error))
            refinement.update(status="failed", reason_code=error.reason_code)
            if persist:
                persist()
            if error.reason_code != "SDF_OCCUPANCY_MISMATCH":
                raise
            if source_mesh_proof is None:
                check_deadline(deadline)
                source_mesh_proof = verify_source_mesh_probes(obj_path, probes, scale)
                refinement["current_source_mesh_agreement"] = source_mesh_proof
                if persist:
                    persist()
            if source_mesh_proof["status"] != "passed":
                refinement.update(status="failed", reason_code="SOLID_SOURCE_MESH_MISMATCH")
                if persist:
                    persist()
                raise SolidSDFError("SOLID_SOURCE_MESH_MISMATCH", "현재 CAD 검증점과 원본 삼각형 점유/경계 여유가 일치하지 않아 네이티브 필드를 정밀화할 수 없습니다.") from error
            actual_parameters = {**actual_parameters, "sdf_dx": actual_parameters["sdf_dx"]/2,
                                 "sdf_res": actual_parameters["sdf_res"]*2}
            refinement.update(status="refining", next_parameters=actual_parameters.copy())
            if persist:
                persist()
            try:
                check_deadline(deadline)
                field, frame, native_audit = native_grid(obj_path, actual_parameters, asset_folder)
                if not np.allclose(frame, initial_frame, atol=1e-8, rtol=0):
                    raise SolidSDFError("SDF_FRAME_INVALID", "원본 솔리드 필드 정밀화에서 관성 좌표가 변경되었습니다.")
                mass = native_audit.get("native_mass_density_one")
                if expected_mass is not None and (mass is None or not math.isclose(mass, expected_mass, rel_tol=1e-12, abs_tol=1e-12)):
                    raise SolidSDFError("NATIVE_MASS_CHANGED", "원본 솔리드 필드 정밀화에서 네이티브 질량이 변경되었습니다.")
            except SolidSDFError as refinement_error:
                refinement.update(status="failed", reason_code=refinement_error.reason_code, message=str(refinement_error))
                if persist:
                    persist()
                raise
            continue
        attempt["status"] = "passed"
        refinement.update(status="passed", refinement_count=attempt["index"], actual_parameters=actual_parameters.copy(),
                          actual_grid_dimensions=list(field.dimensions), actual_grid_cell_count=math.prod(field.dimensions),
                          actual_spacing_engine=field.spacing.tolist())
        refinement.pop("reason_code", None)
        refinement.pop("next_parameters", None)
        if scale:
            refinement["actual_spacing_mm"] = (field.spacing/scale).tolist()
        component_report.pop("cache_reuse", None)
        component_report.update(native_audit, source_occupancy_proof=proof)
        if persist:
            persist()
        return field, frame


def composite_grid_layout(aggregate, refine=False):
    """Preserve the aggregate inertia-frame bounds and halve every axis together."""
    import numpy as np
    spacing = aggregate.spacing / 2 if refine else aggregate.spacing
    required = np.ceil((aggregate.maximum-aggregate.minimum)/spacing).astype(np.int64)+2
    shape = tuple(np.maximum(np.asarray(aggregate.dimensions),required).tolist())
    if math.prod(shape) > MAX_GRID_CELLS or max(shape) > MAX_GRID_AXIS:
        raise SolidSDFError("SDF_RESOURCE_LIMIT", "원본 점유 OR 격자 정밀화가 입력 자원 한계를 초과합니다. 형상이나 부호 검사를 완화하지 않았습니다.")
    return shape, spacing


def compose_grid(aggregate, aggregate_frame, components, deadline=None, refine=False):
    import numpy as np
    check_deadline(deadline)
    shape, spacing = composite_grid_layout(aggregate, refine)
    flat = np.empty(math.prod(shape), dtype=float)
    inverse_frames = [np.linalg.inv(frame) @ aggregate_frame for grid, frame in components]
    for start in range(0, len(flat), 65536):
        check_deadline(deadline)
        indices = np.arange(start, min(start + 65536, len(flat)), dtype=np.int64)
        points = np.column_stack([indices % shape[0], (indices // shape[0]) % shape[1], indices // (shape[0] * shape[1])])
        points = aggregate.minimum + points * spacing
        values = np.full(len(points), np.inf)
        for (grid, frame), relative in zip(components, inverse_frames):
            values = np.minimum(values, grid.query(transform_points(points, relative)))
        if not np.isfinite(values).all():
            raise SolidSDFError("SDF_COMPOSITION_INVALID", "원본 솔리드 점유 합성 결과가 유한하지 않습니다.")
        flat[start:start + len(values)] = values
    return NativeGrid(shape, aggregate.minimum, aggregate.maximum, spacing, flat)


def compose_verified_grid(obj_path, parameters, asset_folder, aggregate, frame, fields, union_probes,
                          body_report, deadline=None, persist=None, scale=None, uses_verified_union=False):
    """Uniformly refine only a composite OR whose strict occupancy proof failed.

    All component fields must already have passed their original CAD probes. No
    component is regenerated or changed, and no sample value is patched at a
    failed probe. Every new grid node samples the min of every original field.
    """
    cache_path = Path(obj_path).with_suffix(".sdf")
    refinement = {"status": "preparing", "method": "uniform_all_axes_halving_and_full_original_component_min_OR",
                  "eligible_failure": "SDF_OCCUPANCY_MISMATCH_in_composite_only",
                  "component_fields_reused_unchanged": True, "component_count": len(fields),
                  "original_aggregate_inertia_frame_preserved": True,
                  "aggregate_bounds_preserved": True, "sign_checks_relaxed": False,
                  "targeted_probe_value_edits": False, "requested_max_spacing_engine": parameters["sdf_dx"],
                  "resource_guard": {"maximum_grid_cells": MAX_GRID_CELLS, "maximum_axis_nodes": MAX_GRID_AXIS},
                  "attempts": []}
    body_report["composite_grid_refinement"] = refinement
    if scale:
        refinement["requested_max_spacing_mm"] = parameters["sdf_dx"] / scale
    if persist:
        persist()
    composed = aggregate if uses_verified_union else compose_grid(aggregate, frame, fields, deadline)
    while True:
        check_deadline(deadline)
        attempt = {"index": len(refinement["attempts"]), "status": "checking",
                   "dimensions": list(composed.dimensions), "cell_count": math.prod(composed.dimensions),
                   "spacing_engine": composed.spacing.tolist(), "grid_domain": composed.validate_native_domain(),
                   "native_reload_proof": {"status": "pending"}}
        if scale:
            attempt["spacing_mm"] = (composed.spacing / scale).tolist()
        refinement["attempts"].append(attempt)
        body_report["native_reload_proof"] = attempt["native_reload_proof"]
        refinement["status"] = "verifying"
        if persist:
            persist()
        composed.write(cache_path)
        attempt["cache_sha256"] = sha256(cache_path)
        try:
            check_deadline(deadline)
            proof = verify_native_reload(obj_path, parameters, asset_folder, composed, frame, fields, union_probes,
                audit_record=attempt["native_reload_proof"], persist=persist, scale=scale,
                expected_mass=body_report.get("native_aggregate", {}).get("native_mass_density_one"))
        except SolidSDFError as error:
            attempt.update(status="failed", reason_code=error.reason_code, message=str(error))
            refinement.update(status="failed", reason_code=error.reason_code)
            if persist:
                persist()
            if error.reason_code != "SDF_OCCUPANCY_MISMATCH" or uses_verified_union:
                raise
            refinement["status"] = "refining"
            try:
                dimensions, spacing = composite_grid_layout(composed, refine=True)
                refinement["next_uniform_grid"] = {"dimensions": list(dimensions), "cell_count": math.prod(dimensions),
                                                   "spacing_engine": spacing.tolist()}
                if persist:
                    persist()
                composed = compose_grid(composed, frame, fields, deadline, refine=True)
            except SolidSDFError as refinement_error:
                refinement.update(status="failed", reason_code=refinement_error.reason_code,
                                  message=str(refinement_error))
                if persist:
                    persist()
                raise
            continue
        attempt["status"] = "passed"
        refinement.update(status="passed", refinement_count=attempt["index"],
                          actual_grid_dimensions=list(composed.dimensions), actual_grid_cell_count=math.prod(composed.dimensions),
                          actual_spacing_engine=composed.spacing.tolist())
        refinement.pop("next_uniform_grid", None)
        refinement.pop("reason_code", None)
        if scale:
            refinement["actual_spacing_mm"] = (composed.spacing / scale).tolist()
        if persist:
            persist()
        return composed, proof


def verify_native_reload(obj_path, parameters, asset_folder, grid, frame, component_fields, occupancy_probes=None,
                         audit_record=None, persist=None, scale=None, expected_mass=None):
    import numpy as np
    import redmax_py as redmax
    domain = grid.validate_native_domain()
    candidates = []
    for component, component_frame in component_fields:
        where = np.unravel_index(int(np.argmin(component.values)), component.dimensions)
        point = component.minimum + np.asarray(where) * component.spacing
        world = transform_points([point], component_frame)[0]
        candidates.append({"point_engine_world": world.tolist(), "inside": True, "kind": "component_negative_grid_extremum"})
    # One grid-padding corner is outside the bounding box of all source material.
    world = transform_points([grid.minimum], frame)[0]
    candidates.append({"point_engine_world": world.tolist(), "inside": False, "kind": "aggregate_padding_outside"})
    world = transform_points([grid.maximum + 5*grid.spacing], frame)[0]
    candidates.append({"point_engine_world": world.tolist(), "inside": False, "kind": "native_outside_grid_boundary_extrapolation"})
    candidates.extend(occupancy_probes or [])
    audit = audit_record if audit_record is not None else {}
    audit.update(status="checking", grid_domain=domain, obj_sha256=sha256(obj_path), grid_dimensions=list(grid.dimensions),
                 grid_spacing_engine=grid.spacing.tolist(), grid_minimum_engine=grid.minimum.tolist(),
                 grid_maximum_engine=grid.maximum.tolist(), native_frame_world_row_major=frame.reshape(-1).tolist(),
                 requested_sdf_parameters=parameters.copy(), candidate_count=len(candidates), occupancy_probes=[],
                 native_query_roundtrip_tolerance_engine=2e-5,
                 limitation="Finite SDF probe validation; voxel interpolation is not exact CAD or continuous collision verification.")
    if scale:
        audit.update(grid_spacing_mm=[float(value)/scale for value in grid.spacing],
                     native_query_roundtrip_tolerance_mm=2e-5/scale)
    if persist:
        persist()
    points = [value["point_engine_world"] for value in candidates]
    sim = redmax.Simulation(body_xml(obj_path, parameters, load=True, save=False, probes=points), str(asset_folder))
    mass = float(sim.get_body_mass("solid"))
    audit["native_mass_density_one"] = mass if math.isfinite(mass) else None
    if not math.isfinite(mass) or mass <= 0:
        audit.update(status="failed", reason_code="NATIVE_MASS_INVALID")
        if persist:
            persist()
        raise SolidSDFError("NATIVE_MASS_INVALID", "SDF 재로딩 시 원본 솔리드의 네이티브 질량이 유효하지 않습니다.")
    if expected_mass is not None:
        audit["native_reload_mass_abs_deviation"] = abs(mass - expected_mass)
        if not math.isclose(mass, expected_mass, rel_tol=1e-12, abs_tol=1e-12):
            audit.update(status="failed", reason_code="NATIVE_MASS_CHANGED")
            if persist:
                persist()
            raise SolidSDFError("NATIVE_MASS_CHANGED", "복합 SDF 정밀화에서 원본 OBJ의 네이티브 질량이 바뀌었습니다.")
    reloaded_frame = _matrix(sim.get_body_E0i("solid"))
    audit["native_reload_frame_max_abs_deviation"] = float(np.max(np.abs(frame-reloaded_frame)))
    if not np.allclose(frame, reloaded_frame, atol=1e-8):
        audit.update(status="failed", reason_code="SDF_FRAME_INVALID", reloaded_frame_world_row_major=reloaded_frame.reshape(-1).tolist())
        if persist:
            persist()
        raise SolidSDFError("SDF_FRAME_INVALID", "SDF 캐시 재로딩 시 원본 관성 좌표가 바뀌었습니다.")
    audit["native_reload_frame_preserved"] = True
    inverse = np.linalg.inv(frame)
    for index, candidate in enumerate(candidates):
        point = candidate["point_engine_world"]
        local_point = transform_points([point], inverse)[0]
        expected = float(grid.query([local_point])[0])
        measured = float(sim.get_body_distance("probe" + str(index), "solid"))
        probe = {**candidate, "probe_index": index, "point_native_inertia_frame": local_point.tolist(),
                 "grid_cell_coordinate": ((local_point-grid.minimum)/grid.spacing).tolist(),
                 "composed_sdf_engine": expected, "native_sdf_engine": measured if math.isfinite(measured) else None,
                 "native_query_abs_difference_engine": abs(measured-expected) if math.isfinite(measured) else None,
                 "near_SDF_zero_at_query_tolerance": abs(expected) <= 2e-5, "status": "checking"}
        if scale:
            probe.update(composed_sdf_mm=expected/scale, native_sdf_mm=measured/scale if math.isfinite(measured) else None)
        audit["occupancy_probes"].append(probe)
        if not math.isfinite(measured) or abs(measured - expected) > 2e-5:
            probe.update(status="failed", failure_condition="native_cache_query_does_not_match_composed_field")
            audit.update(status="failed", reason_code="SDF_NATIVE_RELOAD_MISMATCH", failed_probe_index=index)
            if persist:
                persist()
            raise SolidSDFError("SDF_NATIVE_RELOAD_MISMATCH", "합성 SDF와 네이티브 캐시 쿼리 결과가 다릅니다.")
        # Near-boundary values have no reliable occupancy sign at this grid scale.
        if abs(expected) <= 2e-5 or (measured < 0) != bool(candidate["inside"]):
            probe.update(status="failed", failure_condition="near_SDF_zero_at_query_tolerance" if abs(expected) <= 2e-5 else "source_occupancy_sign_disagrees_with_SDF")
            audit.update(status="failed", reason_code="SDF_OCCUPANCY_MISMATCH", failed_probe_index=index)
            if persist:
                persist()
            raise SolidSDFError("SDF_OCCUPANCY_MISMATCH", "원본 솔리드 점유/공동 부호 검증을 통과하지 못했습니다.")
        probe["status"] = "passed"
        if persist:
            persist()
    del sim
    audit["status"] = "passed"
    if persist:
        persist()
    return audit


def verified_union(metadata):
    audit = metadata.get("audit", {}).get("occupied_union_equivalence_audit", {})
    if audit.get("status") != "accepted":
        return False
    source, analysis = audit.get("source_features", {}), audit.get("analysis_features", {})
    numeric = [audit.get(name) for name in ("source_minus_union_mm3", "union_minus_source_mm3", "volume_difference_tolerance_mm3",
                                           "bbox_max_change_mm", "tolerance_mm", "aggregate_source_volume_mm3", "occupied_union_volume_mm3")]
    if (any(not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or value < 0 for value in numeric)
            or not source.get("valid") or not analysis.get("valid") or not analysis.get("solid_count")
            or audit.get("source_preserved") is not True or audit.get("part_identity_changed") is not False
            or audit["source_minus_union_mm3"] > audit["volume_difference_tolerance_mm3"]
            or audit["union_minus_source_mm3"] > audit["volume_difference_tolerance_mm3"]
            or audit["bbox_max_change_mm"] > max(2*audit["tolerance_mm"], 1e-6)
            or audit["occupied_union_volume_mm3"] <= 0
            or audit["occupied_union_volume_mm3"] > audit["aggregate_source_volume_mm3"]+audit["volume_difference_tolerance_mm3"]):
        raise SolidSDFError("SOLID_UNION_PROOF_INVALID", "검증된 합집합의 양방향 점유 체적/경계 증명이 불완전합니다.")
    return True


def triangle_count_audit(mesh, component):
    """Raw tessellation and its surface-preserving analysis tessellation differ.

    Collinear connectors can be replaced by subdivisions of existing faces.
    Their final face count may increase. The actual final payload must still
    exactly match the exported quality count, hash and oriented full-body
    fingerprint; the raw count is retained as a separately audited quantity.
    """
    raw = component.get("source_triangle_count")
    quality = component.get("quality", {})
    final = quality.get("triangle_count")
    if (any(not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in (raw, final))
            or len(mesh.faces) != final):
        raise SolidSDFError("SOLID_GEOMETRY_CHANGED", "원본 솔리드의 실제 최종 삼각형 수가 분석 품질 manifest와 다릅니다.")
    preprocessing = quality.get("preprocessing", {})
    if raw != final:
        if (preprocessing.get("raw_mesh_preserved") is not True
                or preprocessing.get("shell_vertex_identity_preserved") is not True
                or preprocessing.get("holes_filled") != 0
                or preprocessing.get("operation") != "coincidence_weld_zero_area_removal_and_collinear_edge_subdivision"
                or any(not isinstance(preprocessing.get(name), (int, float)) or isinstance(preprocessing.get(name), bool)
                       or not math.isfinite(preprocessing[name]) for name in ("surface_area_change_mm2", "removed_surface_area_mm2", "max_node_displacement_mm"))):
            raise SolidSDFError("SOLID_RETESSSELLATION_PROOF_MISSING", "원본과 최종 삼각형 수가 다르면 원본 보존 및 기존 표면 재분할 증명이 필요합니다.")
        removed, retriangulated = preprocessing.get("faces_removed"), preprocessing.get("existing_faces_retriangulated")
        centroids = preprocessing.get("new_on_face_centroid_vertices")
        if (any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in (removed, retriangulated, centroids))
                or removed > raw or centroids != retriangulated
                or (retriangulated == 0 and final != raw - removed)
                or (retriangulated > 0 and final < raw - removed + 3*retriangulated)):
            raise SolidSDFError("SOLID_RETESSSELLATION_PROOF_INVALID", "원본 표면 재분할의 제거/기존 면/추가 정점 수량이 일관되지 않습니다.")
    return {"raw_source_triangle_count": raw, "final_analysis_triangle_count": final,
            "actual_OBJ_triangle_count": int(len(mesh.faces)), "raw_equals_final": raw == final,
            "surface_preserving_preprocessing": preprocessing}


def validate_manifest(assembly_dir, expected_revision=None, expected_source_sha256=None):
    assembly_dir = Path(assembly_dir).resolve()
    manifest_path = assembly_dir / "solids_manifest.json"
    if not manifest_path.is_file():
        raise SolidSDFError("SOLID_MANIFEST_MISSING", "원본 솔리드 manifest가 없습니다.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    bodies = manifest.get("bodies")
    parts = {path.stem for path in assembly_dir.glob("*.obj")}
    if manifest.get("schema_version") != 1 or not isinstance(bodies, dict) or set(bodies) != parts:
        raise SolidSDFError("SOLID_MANIFEST_INVALID", "모든 배치가 포함된 원본 솔리드 manifest가 필요합니다.")
    if (manifest.get("status") != "ready" or manifest.get("ready_for_physics") is not True
            or manifest.get("all_original_solids_preserved") is not True
            or manifest.get("coordinate_system") != "RH_Z_UP_ENGINE_UNITS"
            or manifest.get("mapping") != "identity_baked_engine_world"):
        raise SolidSDFError("SOLID_INPUT_NOT_READY", "원본 솔리드 보존/품질/좌표 검증이 완료되지 않았습니다.")
    revision, source_hash = manifest.get("conversion_revision"), manifest.get("source_sha256")
    if not isinstance(revision, str) or not revision or not isinstance(source_hash, str) or len(source_hash) != 64:
        raise SolidSDFError("SOLID_MANIFEST_INVALID", "변환 revision과 원본 CAD 해시가 필요합니다.")
    if expected_revision is not None and revision != expected_revision:
        raise SolidSDFError("INPUT_CHANGED", "솔리드 manifest와 실행의 변환 revision이 다릅니다.")
    if expected_source_sha256 is not None and source_hash != expected_source_sha256:
        raise SolidSDFError("INPUT_CHANGED", "솔리드 manifest와 실행의 원본 CAD 해시가 다릅니다.")
    count = 0
    for part, metadata in bodies.items():
        if not isinstance(metadata, dict):
            raise SolidSDFError("SOLID_MANIFEST_INVALID", "원본 솔리드 배치 metadata가 유효하지 않습니다.")
        components = metadata.get("components") if isinstance(metadata, dict) else None
        audit = metadata.get("audit", {}) if isinstance(metadata, dict) else {}
        uses_verified_union = verified_union(metadata)
        if (metadata.get("status") != "ready" or not isinstance(components, list) or not components
                or metadata.get("source_solid_count") != len(components)
                or audit.get("all_original_solids_preserved") is not True
                or (audit.get("component_concat_exactly_equals_whole_mesh") is not True and not uses_verified_union)
                or audit.get("cavity_shell_winding_preserved") is not True
                or audit.get("original_cad_brep_preserved") is not True
                or audit.get("source_sha256") != source_hash):
            raise SolidSDFError("SOLID_INPUT_NOT_READY", "배치 " + part + "의 모든 원본 솔리드 보존 증명이 필요합니다.")
        if [value.get("source_solid_index") for value in components] != list(range(len(components))):
            raise SolidSDFError("SOLID_MANIFEST_INVALID", "배치 " + part + "의 원본 솔리드 인덱스가 불완전합니다.")
        fingerprints = []
        for component in components:
            quality = component.get("quality", {})
            if (component.get("closed") is not True or component.get("positive_volume") is not True
                    or quality.get("status") != "ready" or component.get("mapping") != "identity_baked_engine_world"):
                raise SolidSDFError("SOLID_INPUT_NOT_READY", "닫히고 방향이 검증된 모든 원본 솔리드가 필요합니다: " + part)
            path_value = component.get("path")
            relative = PurePosixPath(path_value or "")
            if (not isinstance(path_value, str) or relative.is_absolute() or ".." in relative.parts
                    or not relative.parts or relative.parts[0] != "obj_solids" or relative.suffix != ".obj"):
                raise SolidSDFError("SOLID_MANIFEST_INVALID", "원본 솔리드 경로는 입력 폴더 안의 OBJ여야 합니다.")
            path = (assembly_dir / relative).resolve()
            if not path.is_relative_to(assembly_dir) or not path.is_file() or sha256(path) != component.get("sha256"):
                raise SolidSDFError("SOLID_INPUT_CHANGED", "원본 솔리드 파일이 누락되었거나 해시가 바뀌었습니다: " + path_value)
            mesh = _mesh(path)
            triangle_count_audit(mesh, component)
            fingerprints.append(triangle_fingerprint(mesh))
        if combine_fingerprints(fingerprints) != triangle_fingerprint(_mesh(assembly_dir / (part + ".obj"))) and not uses_verified_union:
            raise SolidSDFError("SOLID_GEOMETRY_CHANGED", "원본 솔리드의 모든 삼각형과 방향이 분석 OBJ에 보존되지 않았습니다: " + part)
        count += len(components)
    if (manifest.get("source_occurrence_count") != len(parts) or manifest.get("exported_occurrence_count") != len(parts)
            or manifest.get("source_solid_count") != count or manifest.get("exported_solid_count") != count):
        raise SolidSDFError("SOLID_MANIFEST_INVALID", "원본 배치/솔리드 전체 수량과 출력 수량이 일치하지 않습니다.")
    return manifest


def prepare_caches(assembly_dir, asset_folder, parameters, deadline=None, audit_path=None, emit=None,
                   expected_revision=None, expected_source_sha256=None, scale=None, native_provenance=None,
                   verified_cache_reuse=None):
    import numpy as np
    assembly_dir = Path(assembly_dir).resolve()
    manifest_path = assembly_dir / "solids_manifest.json"
    manifest = validate_manifest(assembly_dir, expected_revision, expected_source_sha256)
    bodies = manifest["bodies"]
    parts = set(bodies)
    if verified_cache_reuse is not None and not isinstance(verified_cache_reuse, dict):
        raise SolidSDFError("CACHE_REUSE_INVALID", "명시적인 캐시 재사용 보고서 형식이 유효하지 않습니다.")
    reused_objects = {} if verified_cache_reuse is None else verified_cache_reuse.get("verified_cache_objects", {})
    if (not isinstance(reused_objects, dict)
            or (reused_objects and verified_cache_reuse.get("status") != "complete")):
        raise SolidSDFError("CACHE_REUSE_INVALID", "명시적으로 검증된 캐시 재사용 보고서가 완전하지 않습니다.")
    def field_cache(path):
        relative = Path(path).resolve().relative_to(assembly_dir).as_posix()
        entry = reused_objects.get(relative)
        if entry is not None:
            cache_relative = str(PurePosixPath(relative).with_suffix(".sdf"))
            cache_hashes = verified_cache_reuse.get("verified_cache_sha256", {})
            if (not isinstance(entry, dict) or not isinstance(cache_hashes, dict)
                    or entry.get("obj_path") != relative or entry.get("cache_path") != cache_relative
                    or entry.get("cache_sha256") != cache_hashes.get(cache_relative)):
                raise SolidSDFError("CACHE_REUSE_INVALID", "재사용 필드 경로와 검증 해시 맵이 일치하지 않습니다.")
        return entry
    report = {"capability": CAPABILITY, "adapter_sha256": sha256(__file__), "manifest_sha256": sha256(manifest_path),
        "conversion_revision": manifest["conversion_revision"], "source_sha256": manifest["source_sha256"],
        "source_occurrence_count": manifest["source_occurrence_count"], "source_solid_count": manifest["source_solid_count"],
        "status": "preparing", "input_unit_count": len(parts), "original_solid_count": 0, "composed_body_count": 0, "bvh_body_count": 0,
        "source_unchanged": True, "planner_source_unchanged": True,
        "native_core_unchanged": True if native_provenance is None else native_provenance.get("native_core_unchanged") is True,
        "native_source_patches": [] if native_provenance is None else copy.deepcopy(native_provenance.get("native_source_patches", [])),
        "method": "native_original_solid_SDF_OR_with_strictly_verified_single_solid_BVH_fallback",
        "verified_cache_reuse": {"eligible_field_count": len(reused_objects), "native_field_reload_count": 0,
            "current_occupancy_verification_skipped": False, "unrecorded_existing_caches_trusted": False},
        "bodies": {}, "mass_scope": "Upstream signed triangle integral of original concatenated solids; overlapping material may be counted more than once. No material density or exact occupied-union inertia validation.",
        "voxel_accuracy_scope": "Original triangles/vertices and native placement preserved; occupied OR is approximated on the requested SDF grid. Min of component distance fields is an implicit occupied-union field, not the exact Euclidean signed distance to the union boundary in overlaps. Penetration depth/gradient, grid interpolation and outside-grid extrapolation remain numerical approximations. No exact CAD or continuous path proof.",
        "bvh_accuracy_scope": "Only one original closed solid may use the pinned native closest-triangle/pseudonormal BVH after a strict SDF occupancy failure and complete native geometry/frame/all supplied CAD probe verification. No dense grid, skipped geometry or relaxed sign checks. Force values and physical parameters are unchanged; separately verified zero-norm derivative guards are declared in native_source_patches. Tessellation and finite probe/path limitations remain."}
    if native_provenance is not None:
        for key in ("redmax_core_sha256", "base_redmax_core_sha256", "native_binary_sha256", "guarded_binary_verification"):
            if key in native_provenance:
                report[key] = copy.deepcopy(native_provenance[key])
    def persist():
        if audit_path:
            write_json(audit_path, report)
    persist()
    for part in sorted(parts):
        check_deadline(deadline)
        metadata = bodies[part]
        components = metadata.get("components") if isinstance(metadata, dict) else None
        if not isinstance(components, list) or not components:
            raise SolidSDFError("SOLID_MANIFEST_INVALID", "배치 " + part + "의 원본 솔리드가 누락되었습니다.")
        original_indices = [value.get("source_solid_index") for value in components]
        if original_indices != list(range(len(components))):
            raise SolidSDFError("SOLID_MANIFEST_INVALID", "배치 " + part + "의 원본 솔리드 인덱스가 불완전합니다.")
        whole = _mesh(assembly_dir / (part + ".obj"))
        whole_fingerprint = triangle_fingerprint(whole)
        component_meshes, paths, fingerprints, probes = [], [], [], []
        for component in components:
            path_value = component.get("path")
            relative = PurePosixPath(path_value or "")
            if (not isinstance(path_value, str) or relative.is_absolute() or ".." in relative.parts
                    or not relative.parts or relative.parts[0] != "obj_solids" or relative.suffix != ".obj"):
                raise SolidSDFError("SOLID_MANIFEST_INVALID", "원본 솔리드 경로는 입력 폴더 안의 OBJ여야 합니다.")
            path = (assembly_dir / relative).resolve()
            if not path.is_relative_to(assembly_dir) or not path.is_file() or sha256(path) != component.get("sha256"):
                raise SolidSDFError("SOLID_INPUT_CHANGED", "원본 솔리드 파일이 누락되었거나 해시가 바뀌었습니다: " + path_value)
            mesh = _mesh(path)
            paths.append(path)
            component_meshes.append(mesh)
            fingerprints.append(triangle_fingerprint(mesh))
            probes.extend(component.get("occupancy_probes", []))
        uses_verified_union = verified_union(metadata)
        if combine_fingerprints(fingerprints) != whole_fingerprint and not uses_verified_union:
            raise SolidSDFError("SOLID_GEOMETRY_CHANGED", "원본 솔리드의 모든 삼각형과 방향이 분석 OBJ에 보존되지 않았습니다: " + part)
        body_report = {"unit_id": metadata.get("unit_id"), "original_solid_count": len(components), "triangle_payload_fingerprint": whole_fingerprint,
                       "native_collision_type": "SDF", "obj_sha256": sha256(assembly_dir / (part + ".obj")),
                       "source_metadata": metadata.get("audit", {}), "status": "single_solid_original" if len(components) == 1 else "preparing", "components": [],
                       "triangle_count_audits": [triangle_count_audit(mesh, component) for mesh, component in zip(component_meshes, components)]}
        report["bodies"][part] = body_report
        report["original_solid_count"] += len(components)
        persist()
        if emit:
            emit("solid_sdf_preparing", "원본 솔리드의 점유 체적 SDF를 합성합니다.", part=part, original_solids=len(components))
        aggregate_path = assembly_dir / (part + ".obj")
        aggregate, frame, body_audit = native_grid(aggregate_path, parameters, asset_folder,
                                                 verified_cache=field_cache(aggregate_path))
        report["verified_cache_reuse"]["native_field_reload_count"] += int("cache_reuse" in body_audit)
        body_report.update(native_aggregate=body_audit, native_reload_proof={"status": "pending"})
        persist()
        if len(components) == 1:
            try:
                proof = verify_native_reload(assembly_dir / (part + ".obj"), parameters, asset_folder, aggregate, frame, [(aggregate, frame)], probes,
                                             audit_record=body_report["native_reload_proof"], persist=persist, scale=scale)
            except SolidSDFError as error:
                if error.reason_code != "SDF_OCCUPANCY_MISMATCH":
                    raise
                fallback = {"reason_code": error.reason_code,
                            "eligibility": "exactly_one_original_solid_and_strict_SDF_source_occupancy_failure",
                            "original_geometry_unchanged": True, "sign_checks_relaxed": False}
                body_report.update(status="verifying_native_BVH", fallback=fallback,
                    sdf_fallback={**fallback, "failed_sdf_native_reload_proof": copy.deepcopy(body_report["native_reload_proof"]),
                                  "failed_sdf_cache_sha256": sha256(assembly_dir / (part + ".sdf"))},
                    native_reload_proof={"status": "pending"})
                persist()
                check_deadline(deadline)
                proof = verify_native_bvh(assembly_dir / (part + ".obj"), parameters, asset_folder, probes, expected_frame=frame,
                                          audit_record=body_report["native_reload_proof"], persist=persist, scale=scale)
                body_report.update(status="passed", native_collision_type="BVH", native_reload_proof=proof,
                                   representation="one_original_solid_native_BVH_including_original_cavity_shells")
                report["bvh_body_count"] += 1
            else:
                body_report.update({"status": "passed", "representation": "one_original_solid_including_original_cavity_shells", "native_aggregate": body_audit,
                                    "native_reload_proof": proof, "original_cache_sha256": sha256(assembly_dir / (part + ".sdf"))})
            persist()
            continue
        fields = []
        for component, path in zip(components, paths):
            check_deadline(deadline)
            field, component_frame, component_audit = native_grid(path, parameters, asset_folder,
                                                                  verified_cache=field_cache(path))
            report["verified_cache_reuse"]["native_field_reload_count"] += int("cache_reuse" in component_audit)
            component_report = {"source_solid_index": component["source_solid_index"], **component_audit, "source_occupancy_proof": {"status": "pending"}}
            body_report["components"].append(component_report)
            persist()
            field, component_frame = verify_component_field(path, parameters, asset_folder, field, component_frame,
                component_audit, component.get("occupancy_probes", []), component_report, deadline, persist, scale)
            fields.append((field, component_frame))
            persist()
        cache_path = assembly_dir / (part + ".sdf")
        # A void of one original solid may be occupied by another. Each source
        # classifier was checked above against its own field; aggregate probes
        # now require the OR occupancy of all original fields at that point.
        union_probes = []
        for probe in probes:
            point = [probe["point_engine_world"]]
            minimum = min(float(field.query(transform_points(point, np.linalg.inv(component_frame)))[0])
                          for field, component_frame in fields)
            union_probes.append({**probe, "source_component_inside": probe["inside"], "inside": minimum < 0,
                                 "expected_union_sign_method": "OR_of_individually_source_classified_native_component_fields"})
        composed, proof = compose_verified_grid(assembly_dir / (part + ".obj"), parameters, asset_folder,
            aggregate, frame, fields, union_probes, body_report, deadline, persist, scale, uses_verified_union)
        body_report.update({"status": "passed", "native_aggregate": body_audit, "native_reload_proof": proof,
                            "representation": "validated_OCCT_occupied_union_mesh_original_components_retained" if uses_verified_union else "implicit_min_OR_of_original_solids",
                            "composed_cache_sha256": sha256(cache_path), "composed_grid_dimensions": list(composed.dimensions),
                            "composed_grid_spacing_engine": composed.spacing.tolist(), "composed_grid_cell_count": math.prod(composed.dimensions),
                            "composed_grid_value_min": float(composed.values.min()), "composed_grid_value_max": float(composed.values.max())})
        report["composed_body_count"] += int(not uses_verified_union)
        if scale:
            body_report["composed_grid_spacing_mm"] = (composed.spacing / scale).tolist()
        persist()
    report["status"] = "passed"
    if scale:
        for value in report["bodies"].values():
            aggregate_audit = value["native_aggregate"]
            aggregate_audit["spacing_mm"] = [item / scale for item in aggregate_audit["spacing_engine"]]
            aggregate_audit["native_world_vertex_max_abs_deviation_mm"] = aggregate_audit["native_world_vertex_max_abs_deviation_engine"] / scale
        report["native_query_roundtrip_tolerance_mm"] = 2e-5 / scale
    persist()
    return report


def native_smoke_test(workspace, asset_folder):
    """Actual native overlap/cavity/rigid-world-geometry adapter proof."""
    import numpy as np
    import trimesh
    workspace = Path(workspace).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="solid-or-smoke-", dir=str(workspace)) as temporary:
        directory = Path(temporary)
        outer = trimesh.creation.box(extents=[3, 2.6, 2.2])
        inner = trimesh.creation.box(extents=[1, 1, 1])
        inner.invert()
        hollow = trimesh.util.concatenate([outer, inner])
        second = trimesh.creation.box(extents=[2, 1.8, 1.4])
        second.apply_translation([1, 0, 0])
        pose = trimesh.transformations.rotation_matrix(.61, [0, 0, 1])
        pose[:3, 3] = [3.2, -2.7, 1.6]
        hollow.apply_transform(pose)
        second.apply_transform(pose)
        aggregate = trimesh.util.concatenate([hollow, second])
        meshes = [aggregate, hollow, second]
        paths = []
        for index, mesh in enumerate(meshes):
            path = directory / (str(index) + ".obj")
            mesh.export(str(path), file_type="obj", digits=17, header=None)
            paths.append(path)
        parameters = {"sdf_dx": .08, "sdf_res": 20}
        aggregate_grid, aggregate_frame, geometry_proof = native_grid(paths[0], parameters, asset_folder)
        fields = [native_grid(path, parameters, asset_folder)[:2] for path in paths[1:]]
        composed = compose_grid(aggregate_grid, aggregate_frame, fields)
        composed.write(paths[0].with_suffix(".sdf"))
        probes = [{"point_engine_world": transform_points([[.75, 0, 0]], pose)[0].tolist(), "inside": True, "kind": "overlapping_original_solids"},
                  {"point_engine_world": transform_points([[-.25, 0, 0]], pose)[0].tolist(), "inside": False, "kind": "preserved_cavity"}]
        proof = verify_native_reload(paths[0], parameters, asset_folder, composed, aggregate_frame, fields, probes)
        original_overlap = float(aggregate_grid.query(transform_points([probes[0]["point_engine_world"]], np.linalg.inv(aggregate_frame)))[0])
        composed_overlap = proof["occupancy_probes"][-2]["native_sdf_engine"]
        if original_overlap <= 0 or composed_overlap >= 0:
            raise SolidSDFError("SDF_OCCUPANCY_MISMATCH", "원본 중첩 parity와 점유 OR 부호를 구분하지 못했습니다.")
        bvh_probes = [{"point_engine_world": transform_points([[1, 0, 0]], pose)[0].tolist(), "inside": True, "kind": "material"},
                      {"point_engine_world": transform_points([[0, 0, 0]], pose)[0].tolist(), "inside": False, "kind": "cavity"},
                      {"point_engine_world": transform_points([[3, 0, 0]], pose)[0].tolist(), "inside": False, "kind": "outside"}]
        bvh_proof = verify_native_bvh(paths[1], parameters, asset_folder, bvh_probes, expected_frame=fields[0][1])
        return {"capability": CAPABILITY, "status": "passed", "adapter_sha256": sha256(__file__),
                "native_world_geometry": geometry_proof, "native_reload_proof": proof,
                "single_original_solid_native_BVH_cavity_proof": bvh_proof,
                "original_concatenated_parity_overlap_sdf": original_overlap, "composed_occupied_union_overlap_sdf": composed_overlap}
