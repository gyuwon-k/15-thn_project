"""Confirmed-history pairwise preference learning (REQ-LRN-001..007).

The classifier ranks only the candidates supplied by the constraint engine. Its
score is a preference, never a probability of physical assembly feasibility.
There is deliberately no seeded production history or synthetic expert label.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from hashlib import sha256
from itertools import combinations
import json
import math
from pathlib import Path
from typing import Any
import uuid

FEATURE_VERSION = "pilot-candidate-v1"
FEATURE_NAMES = ["log_volume_mm3", "log_size_x_mm", "log_size_y_mm", "log_size_z_mm",
                 "center_x_m", "center_y_m", "center_z_m", "direction_x", "direction_y",
                 "direction_z", "interference_count", "is_fastener", "is_connection",
                 "is_inspection", "installed_fraction"]
MIN_PAIRS = 4


def _hash(value: Any) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _write_json(path: Path, value: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temp.replace(path)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _vector(value: Any) -> list[float] | None:
    if isinstance(value, dict):
        value = [value.get(k) for k in ("x", "y", "z")]
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return None
    result = [_number(v) for v in value]
    return result if all(v is not None for v in result) else None


def _candidate_features(candidate: dict, state: dict | None = None) -> list[float] | None:
    """Missing CAD dimensions are not silently substituted with zero geometry."""
    feature = candidate.get("features") or candidate.get("geometry") or candidate
    if isinstance(feature, list):
        values = [_number(v) for v in feature]
        return values if len(values) == len(FEATURE_NAMES) and None not in values else None
    if not isinstance(feature, dict):
        return None
    size = _vector(feature.get("size") or feature.get("dimensions") or feature.get("bbox_size"))
    center = _vector(feature.get("center") or feature.get("centroid"))
    bbox = feature.get("bbox") or feature.get("bounds")
    if isinstance(bbox, dict):
        lo, hi = _vector(bbox.get("min")), _vector(bbox.get("max"))
    elif isinstance(bbox, list) and len(bbox) == 6:
        lo, hi = _vector(bbox[:3]), _vector(bbox[3:])
    elif isinstance(bbox, list) and len(bbox) == 2:
        lo, hi = _vector(bbox[0]), _vector(bbox[1])
    else:
        lo, hi = None, None
    if lo is not None and hi is not None:
        size = size or [hi[i] - lo[i] for i in range(3)]
        center = center or [(lo[i] + hi[i]) / 2 for i in range(3)]
    if size is None or center is None or any(v < 0 for v in size):
        return None
    volume = _number(feature.get("volume", feature.get("volume_mm3")))
    # A missing true volume remains distinguishable in the source history; box
    # volume is explicitly the fallback feature, not a claimed CAD volume.
    volume = volume if volume is not None and volume >= 0 else math.prod(size)
    direction_raw = candidate.get("direction", feature.get("direction", [0, 0, 1]))
    direction = _vector(direction_raw)
    if direction is None and isinstance(direction_raw, str):
        direction = {"+X": [1,0,0], "-X": [-1,0,0], "+Y": [0,1,0],
                     "-Y": [0,-1,0], "+Z": [0,0,1], "-Z": [0,0,-1]}.get(direction_raw.upper())
    direction = direction or [0, 0, 0]
    magnitude = math.sqrt(sum(v*v for v in direction)) or 1
    kind = str(candidate.get("kind", candidate.get("type", "install"))).lower()
    interference = _number(feature.get("interference_count", candidate.get("interference_count", 0))) or 0
    installed = _number((state or {}).get("installed_fraction", feature.get("installed_fraction", 0))) or 0
    return [math.log1p(volume), *[math.log1p(v) for v in size], *[v / 1000 for v in center],
            *[v / magnitude for v in direction], max(0, interference),
            float(kind in {"fasten", "fastener", "bolt", "tighten"} or candidate.get("is_fastener", False)),
            float(kind in {"connect", "connection"}), float(kind in {"inspect", "inspection", "test"}),
            min(1, max(0, installed))]


def _operations(history: dict) -> list[dict]:
    state = history.get("state") or history.get("snapshot") or history
    plan = state.get("plan") or {}
    operations = state.get("operations") or plan.get("operations") or []
    lookup = state.get("features_by_operation") or state.get("features") or {}
    units = {str(u.get("id")): u for u in state.get("units", [])}
    instances = {str(i.get("id")): i for i in state.get("instances", [])}
    result = []
    for raw in operations:
        if not isinstance(raw, dict):
            continue
        op = dict(raw)
        oid = str(op.get("id", op.get("operation_id", "")))
        op["id"] = oid
        if not op.get("features"):
            if isinstance(lookup, dict) and isinstance(lookup.get(oid), (dict, list)):
                op["features"] = lookup[oid]
            else:
                target_ids = op.get("target_unit_ids") or []
                unit = units.get(str(op.get("unit_id", target_ids[0] if target_ids else "")), {})
                instance = instances.get(str(op.get("instance_id", "")), {})
                if not instance and unit.get("instance_ids"):
                    instance = instances.get(str(unit["instance_ids"][0]), {})
                op["features"] = (unit.get("features") or (unit if unit.get("bbox") else None)
                                  or instance.get("features") or instance.get("geometry")
                                  or (instance if instance.get("bbox") else None))
        result.append(op)
    return result


def _confirmed(histories: list[dict]) -> list[dict]:
    seen = set()
    result = []
    for history in histories:
        snapshot = history.get("snapshot") or history.get("state") or history
        status = history.get("status", snapshot.get("status"))
        if status != "confirmed":
            continue
        identity = history.get("confirmation_id") or history.get("id") or snapshot.get("id")
        if identity is None or str(identity) in seen:
            continue
        seen.add(str(identity))
        result.append(history)
    return result


def _blocked_pairs(history: dict, operations: list[dict]) -> set[tuple[str, str]]:
    """Exclude compulsory precedence: constraints are not user preferences."""
    state = history.get("state") or history.get("snapshot") or history
    edges: dict[str, set[str]] = {str(op["id"]): set() for op in operations}
    rules = state.get("constraints") or state.get("rules") or []
    for rule in rules:
        if not isinstance(rule, dict):
            continue
        if rule.get("strength", "hard") != "hard":
            continue
        a = rule.get("before", rule.get("before_operation_id", rule.get("from")))
        b = rule.get("after", rule.get("after_operation_id", rule.get("to")))
        if a is not None and b is not None:
            edges.setdefault(str(a), set()).add(str(b))
    for op in operations:
        for dep in op.get("predecessors", op.get("depends_on", [])):
            edges.setdefault(str(dep), set()).add(str(op["id"]))
    blocked = set()
    for start in edges:
        stack, visited = list(edges[start]), set()
        while stack:
            end = stack.pop()
            if end in visited:
                continue
            visited.add(end)
            blocked.add(tuple(sorted((start, end))))
            stack.extend(edges.get(end, set()) - visited)
    return blocked


def dataset(histories: list[dict]) -> dict:
    rows, labels, groups, records = [], [], [], []
    missing = 0
    confirmed = _confirmed(histories)
    for history in confirmed:
        operations = _operations(history)
        positions = {op["id"]: index for index, op in enumerate(operations)}
        vectors = {op["id"]: _candidate_features(op) for op in operations}
        missing += sum(v is None for v in vectors.values())
        blocked = _blocked_pairs(history, operations)
        identity = str(history.get("confirmation_id") or history.get("id"))
        # Keep all revisions of a product together during evaluation.
        group = str(history.get("product_id") or history.get("project_id") or identity)
        for a, b in combinations(sorted(vectors), 2):
            if (a, b) in blocked or vectors[a] is None or vectors[b] is None:
                continue
            diff = [x-y for x, y in zip(vectors[a], vectors[b])]
            if not any(abs(x) > 1e-12 for x in diff):
                continue
            label = int(positions[a] < positions[b])
            rows.append(diff)
            labels.append(label)
            groups.append(group)
            records.append({"confirmation": identity, "pair": [a,b], "x": diff, "y": label,
                            "source": history.get("source", "user_confirmed"), "group": group})
    return {"x": rows, "y": labels, "groups": groups, "records": records,
            "history_count": len(confirmed), "missing_features": missing,
            "dataset_hash": _hash(records), "feature_version": FEATURE_VERSION}


def model_status(model_dir: Path | str) -> dict:
    def read(name):
        try:
            return json.loads((Path(model_dir) / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    latest, attempt = read("latest.json"), read("last_attempt.json")
    if latest:
        return dict(latest, training_state=latest["status"], model_available=True,
                    last_training_status=(attempt or latest)["status"],
                    last_training_reason=(attempt or latest).get("reason"))
    if attempt:
        return dict(attempt, training_state=attempt["status"], model_available=False)
    return {"status": "no_history", "training_state": "no_history", "model_available": False,
            "reason": "확정된 학습 이력이 없습니다.", "feature_version": FEATURE_VERSION}


def train(histories: list[dict], model_dir: Path | str) -> dict:
    """Fit a real sklearn model; failed training never replaces latest good model."""
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    data = dataset(histories)
    meta = {"status": "training", "history_count": data["history_count"], "pair_count": len(data["y"]),
            "missing_features": data["missing_features"], "feature_version": FEATURE_VERSION,
            "feature_names": FEATURE_NAMES, "dataset_hash": data["dataset_hash"],
            "data_version": data["dataset_hash"][:16], "trained_at": datetime.now(timezone.utc).isoformat(),
            "classes": sorted(set(data["y"])), "seed": 42,
            "score_meaning": "제약을 통과한 후보 간 학습된 순서 선호 점수. 물리 가능 확률이 아닙니다.",
            "source_counts": dict(Counter(record["source"] for record in data["records"]))}
    reason = None
    if not data["history_count"]:
        meta["status"], reason = "no_history", "확정된 학습 이력이 없습니다. 기본안과 수동 편집을 사용할 수 있습니다."
    elif len(data["y"]) < MIN_PAIRS:
        meta["status"], reason = "insufficient_labels", "기하 특징과 자유 선택 관계가 있는 유효 선호 쌍이 4개 이상 필요합니다."
    elif len(set(data["y"])) < 2:
        meta["status"], reason = "insufficient_labels", "선호 라벨이 단일 클래스입니다. 다양성이 확보될 때까지 기본안을 사용합니다."
    if reason:
        meta["reason"] = reason
        meta["training_state"] = meta["status"]
        meta["previous_model"] = model_status(model_dir).get("model_version")
        _write_json(model_dir / "last_attempt.json", meta)
        return meta
    try:
        import joblib
        import numpy as np
        import sklearn
        from sklearn.linear_model import SGDClassifier
        from sklearn.metrics import accuracy_score, balanced_accuracy_score
        from sklearn.pipeline import make_pipeline
        from sklearn.preprocessing import StandardScaler

        def estimator():
            return make_pipeline(StandardScaler(with_mean=False), SGDClassifier(
                loss="log_loss", alpha=0.001, max_iter=2000, tol=1e-5,
                random_state=42, class_weight="balanced", fit_intercept=False))

        x, y = np.asarray(data["x"], dtype=float), np.asarray(data["y"], dtype=int)
        unique_groups = sorted(set(data["groups"]))
        evaluation = {"status": "not_evaluated", "reason": "독립 제품/프로젝트 3개 이상이 없어 일반화 성능을 평가하지 않았습니다.",
                      "split": "product/project grouped holdout; all revisions remain in one group",
                      "group_count": len(unique_groups), "general_accuracy_claim": False}
        if len(unique_groups) >= 3:
            # Deterministic product-level split. Never split preference rows randomly.
            holdout = unique_groups[-max(1, len(unique_groups)//4):]
            mask = np.asarray([g in holdout for g in data["groups"]])
            if len(set(y[~mask])) == 2 and mask.any():
                eval_model = estimator().fit(x[~mask], y[~mask])
                predicted = eval_model.predict(x[mask])
                evaluation.update(status="evaluated", reason="독립 제품 그룹 홀드아웃 시험",
                                  accuracy=float(accuracy_score(y[mask], predicted)),
                                  balanced_accuracy=float(balanced_accuracy_score(y[mask], predicted)),
                                  train_groups=[g for g in unique_groups if g not in holdout], test_groups=holdout,
                                  train_pair_count=int((~mask).sum()), test_pair_count=int(mask.sum()))
            else:
                evaluation["reason"] = "제품 단위 분리 후 학습 클래스가 부족하여 평가를 생략했습니다."
        model = estimator().fit(x, y)
        version = "ml-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
        artifact = model_dir / (version + ".joblib")
        temporary = artifact.with_suffix(".joblib.tmp")
        joblib.dump({"model": model, "feature_version": FEATURE_VERSION, "feature_names": FEATURE_NAMES,
                     "dataset_hash": data["dataset_hash"]}, temporary)
        temporary.replace(artifact)
        dataset_path = model_dir / (version + "-dataset.json")
        _write_json(dataset_path, {"feature_version": FEATURE_VERSION, "records": data["records"]})
        meta.update(status="available", model_version=version, artifact_path=str(artifact.resolve()),
                    dataset_path=str(dataset_path.resolve()), sklearn_version=sklearn.__version__,
                    algorithm="StandardScaler + SGDClassifier(loss=log_loss), pairwise candidate differences",
                    evaluation=evaluation, artifact_sha256=sha256(artifact.read_bytes()).hexdigest(),
                    reason="사용자가 확정한 유효 선호 이력으로 실제 모델 학습을 완료했습니다.")
        meta["training_state"] = meta["status"]
        _write_json(model_dir / (version + ".json"), meta)
        _write_json(model_dir / "latest.json", meta)
    except Exception as exc:
        meta.update(status="training_failed", reason=f"학습 실패: {type(exc).__name__}: {exc}",
                    previous_model=model_status(model_dir).get("model_version"))
    meta["training_state"] = meta["status"]
    _write_json(model_dir / "last_attempt.json", meta)
    return meta


def rank_candidates(candidates: list[dict], model_dir: Path | str | dict,
                    state: dict | None = None) -> list[dict]:
    """Hard-ineligible candidates never receive a ranking, even with an ML score."""
    result = [dict(c) for c in candidates if c.get("eligible", True) and not c.get("blocked", False)]
    meta = model_dir if isinstance(model_dir, dict) else model_status(model_dir)
    if meta.get("status") != "available":
        return [dict(c, ml_score=None, ml_status=meta.get("status", "no_history")) for c in result]
    try:
        import joblib
        import numpy as np
        path = Path(meta["artifact_path"])
        if sha256(path.read_bytes()).hexdigest() != meta.get("artifact_sha256"):
            raise ValueError("모델 파일 해시가 일치하지 않습니다.")
        artifact = joblib.load(path)
        if artifact["feature_version"] != FEATURE_VERSION:
            raise ValueError("특징 버전이 일치하지 않습니다.")
        vectors = [_candidate_features(c, state) for c in result]
        valid = [index for index, vector in enumerate(vectors) if vector is not None]
        sums, counts = [0.] * len(result), [0] * len(result)
        for a, b in combinations(valid, 2):
            diff = np.asarray([x-y for x,y in zip(vectors[a], vectors[b])]).reshape(1,-1)
            score = float(artifact["model"].decision_function(diff)[0])
            sums[a] += score
            sums[b] -= score
            counts[a] += 1
            counts[b] += 1
        for i, candidate in enumerate(result):
            candidate.update(ml_score=sums[i] / counts[i] if counts[i] else None,
                             ml_status="available" if counts[i] else "missing_features" if vectors[i] is None else "single_candidate",
                             model_version=meta["model_version"], score_meaning=meta["score_meaning"])
        # Keep candidates without sufficient features in stable fallback positions.
        ranked = sorted((result[i] for i in valid), key=lambda c: -(c.get("ml_score") or 0))
        for index, candidate in zip(valid, ranked):
            result[index] = candidate
        return result
    except Exception as exc:
        return [dict(c, ml_score=None, ml_status="model_unavailable", ml_reason=str(exc)) for c in result]


score_candidates = rank_candidates


def similar_history(project: dict, histories: list[dict], limit: int = 5) -> dict:
    """Inspectable geometry/composition similarity, not unverified path reuse."""
    def signature(value):
        ops = _operations(value)
        vectors = [_candidate_features(op) for op in ops]
        valid = [v for v in vectors if v is not None]
        if not valid:
            return None
        means = [sum(v[i] for v in valid)/len(valid) for i in range(7)]
        return [math.log1p(len(ops)), *means, sum(v[11] for v in valid)/len(valid)]
    query = signature(project)
    confirmed = _confirmed(histories)
    if not confirmed:
        return {"status": "no_history", "matches": [], "reason": "확정 이력이 없습니다."}
    if query is None:
        return {"status": "missing_features", "matches": [], "reason": "비교할 형상 특징이 부족합니다."}
    matches = []
    for history in confirmed:
        target = signature(history)
        if target is None:
            continue
        distance = sum(abs(a-b)/(1+abs(a)+abs(b)) for a,b in zip(query,target))/len(query)
        similarity = math.exp(-6 * distance)
        matches.append({"history_id": history.get("id"), "project_id": history.get("project_id"),
                        "similarity": similarity, "distance": distance,
                        "basis": ["작업 수", "평균 형상 크기·체적 특징", "중심 위치", "체결 작업 비율"],
                        "revalidation_required": True, "status": "reference_only" if similarity >= .75 else "weak_evidence",
                        "path_reuse_allowed": False})
    matches.sort(key=lambda row: (-row["similarity"], str(row["history_id"])))
    return {"status": "available" if matches and matches[0]["similarity"] >= .75 else "insufficient_similarity",
            "matches": matches[:limit], "feature_version": FEATURE_VERSION,
            "reason": "이력은 참고 근거이며 현재 형상과 경로를 다시 검증해야 합니다."}
