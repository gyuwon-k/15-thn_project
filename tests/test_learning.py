"""Explicitly SYNTHETIC controls; never inserted into production history."""
from hashlib import sha256
import json
from pathlib import Path
import pytest

from backend.learning import train, dataset, rank_candidates, similar_history, model_status
from backend.exporter import web_package


def candidate(identity, size, **extra):
    return {"id": identity, "kind": "install", "features": {"size": [size]*3,
            "center": [size, size/2, 0], "volume": size**3}, **extra}


def history(identity="synthetic-h1", project="synthetic-project"):
    # Deliberately non-alphabetic final order contains both canonical pair labels.
    return {"id": identity, "project_id": project, "status": "confirmed", "source": "synthetic_test",
            "operations": [candidate("c", 40), candidate("a", 30), candidate("d", 20), candidate("b", 10)]}


def test_at42_no_history_missing_features_single_class_are_not_fake_models(tmp_path):
    assert train([], tmp_path)["status"] == "no_history"
    missing = history()
    missing["operations"] = [{"id": str(i)} for i in range(6)]
    result = train([missing], tmp_path)
    assert result["status"] == "insufficient_labels"
    assert result["missing_features"] == 6
    single = history()
    single["operations"] = sorted(single["operations"], key=lambda op: op["id"])
    assert train([single], tmp_path)["status"] == "insufficient_labels"
    assert model_status(tmp_path)["training_state"] == "insufficient_labels"
    assert not list(tmp_path.glob("*.joblib"))


def test_at39_only_confirmed_unique_snapshots_generate_labels():
    confirmed = history()
    draft = dict(history("draft"), status="draft", confirmed_at="old timestamp")
    canceled = dict(history("canceled"), status="canceled")
    data = dataset([confirmed, confirmed, draft, canceled])
    assert data["history_count"] == 1
    assert len(data["records"]) == 6
    assert {r["confirmation"] for r in data["records"]} == {"synthetic-h1"}


def test_at40_actual_fit_reload_and_data_model_version_update(tmp_path):
    first = train([history()], tmp_path)
    assert first["status"] == "available", first
    assert Path(first["artifact_path"]).is_file()
    assert first["evaluation"]["status"] == "not_evaluated"
    assert first["source_counts"] == {"synthetic_test": 6}
    assert model_status(tmp_path)["model_version"] == first["model_version"]
    second = train([history(), history("synthetic-h2")], tmp_path)
    assert second["status"] == "available"
    assert second["model_version"] != first["model_version"]
    assert second["dataset_hash"] != first["dataset_hash"]
    assert Path(first["artifact_path"]).is_file(), "Previous artifact must survive retraining"


def test_at41_learned_preference_changes_ranking_and_never_admits_blocked(tmp_path):
    trained = train([history()], tmp_path)
    assert trained["status"] == "available"
    options = [candidate("new-small", 12), candidate("new-large", 35),
               candidate("forbidden-large", 60, eligible=False)]
    result = rank_candidates(options, tmp_path)
    assert [c["id"] for c in result] == ["new-large", "new-small"]
    assert result[0]["ml_score"] > result[1]["ml_score"]
    assert result == rank_candidates(options, tmp_path), "Reload must be reproducible"
    # No model output is a physical-success confidence.
    assert "물리 가능 확률이 아닙니다" in result[0]["score_meaning"]


def test_constraint_orders_are_excluded_from_preference_labels():
    data = history()
    data["constraints"] = [{"before": "c", "after": "a"}, {"before": "a", "after": "d"}]
    learned = dataset([data])
    assert len(learned["records"]) == 3
    assert ["c", "d"] not in [r["pair"] for r in learned["records"]]


def test_at43_evaluation_groups_keep_entire_products_out_of_training(tmp_path):
    items = [history(f"h{i}", f"product-{i//2}") for i in range(8)]
    result = train(items, tmp_path)
    assert result["status"] == "available"
    evaluation = result["evaluation"]
    assert evaluation["status"] == "evaluated"
    assert not set(evaluation["train_groups"]) & set(evaluation["test_groups"])
    assert evaluation["general_accuracy_claim"] is False


def test_training_failure_preserves_last_good_artifact(tmp_path, monkeypatch):
    trained = train([history()], tmp_path)
    import sklearn.linear_model
    def fail(*args, **kwargs):
        raise RuntimeError("controlled training failure")
    monkeypatch.setattr(sklearn.linear_model.SGDClassifier, "fit", fail)
    failed = train([history("another")], tmp_path)
    assert failed["status"] == "training_failed"
    assert model_status(tmp_path)["model_version"] == trained["model_version"]


def test_at38_similarity_reports_basis_and_never_reuses_paths():
    source = history()
    unrelated = dict(history("unrelated"), operations=[candidate("x", 100000)])
    result = similar_history(source, [source, unrelated])
    assert result["matches"][0]["history_id"] == source["id"]
    assert result["matches"][0]["similarity"] == 1
    assert result["matches"][1]["status"] == "weak_evidence"
    assert all(m["revalidation_required"] and not m["path_reuse_allowed"] for m in result["matches"])


def test_at31_web_package_is_not_published_and_never_contains_cad(tmp_path):
    # This is a packaging control, not a claim that these test bytes are an MP4.
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"synthetic packaging fixture")
    render = {"id": "r-test", "snapshot_id": "s-test", "status": "succeeded", "draft": False,
              "sha256": sha256(video.read_bytes()).hexdigest(), "video_path": str(video)}
    snapshot = {"id": "s-test", "status": "confirmed", "project": {"name": "한글 & <제품>", "product_no": "00123"}}
    result = web_package(render, snapshot, tmp_path / "packages")
    assert result["publication_status"] == "not_published"
    assert result["package_status"] == "packaged"
    assert result["url"] is None
    page = (Path(result["folder"]) / "index.html").read_text(encoding="utf-8")
    assert "00123" in page and "&lt;제품&gt;" in page
    import zipfile
    with zipfile.ZipFile(result["zip_path"]) as archive:
        assert set(archive.namelist()) == {"index.html", "assembly.mp4", "manifest.json"}
    with pytest.raises(ValueError):
        web_package(dict(render, draft=True), snapshot, tmp_path)
    with pytest.raises(ValueError):
        web_package(render, dict(snapshot, id="wrong-version"), tmp_path)
    repeated = web_package(dict(render, **result), snapshot, tmp_path / "retry")
    assert Path(repeated["zip_path"]).is_file()


@pytest.fixture
def synthetic_render_server():
    """An explicit synthetic UI control for renderer transport, not CAD proof."""
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    page = '''<!doctype html><html lang="ko"><meta charset="utf-8"><style>html,body{margin:0}#render-root{position:relative;width:100vw;height:100vh;background:#13283a;color:white;font:24px "Malgun Gothic",sans-serif;overflow:hidden}h1{margin:0;padding:20px;font-size:30px}#part{background:#33d1ba;width:60px;height:60px;margin:24px}</style><div id="render-root"><h1>합성 렌더 검증 · 품번 00123</h1><div id="part"></div><p id="time">0</p></div><script>window.__PILOT_RENDER__={ready:true,duration:.5,setTime:async t=>{document.getElementById('time').textContent='작업 시간 '+t.toFixed(3);document.getElementById('part').style.transform='translateX('+t*500+'px)'}};</script></html>'''.encode('utf-8')
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page)))
            self.end_headers()
            self.wfile.write(page)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize("preset,expected_frames", [("quick", 6), ("production", 15)])
def test_at30_actual_mp4_render_decode_frames_and_web_package(tmp_path, synthetic_render_server, preset, expected_frames):
    from backend.exporter import render_video
    events = []
    snapshot = {"id": "synthetic-render-control", "status": "confirmed", "project": {"name": "합성 시험"}}
    result = render_video(synthetic_render_server, snapshot, tmp_path, preset, lambda p,m: events.append(p))
    assert result["status"] == "succeeded"
    assert result["frame_count"] == expected_frames
    assert result["decoded_frame_count"] == expected_frames
    assert result["duration"] == .5
    assert Path(result["video_path"]).stat().st_size > 1000
    assert result["verification"] == "ffmpeg full-video decode succeeded"
    assert len(result["preview_frames"]) == 3
    assert len({sha256(Path(p).read_bytes()).hexdigest() for p in result["preview_frames"]}) == 3
    assert events[-1] == 1
    package = web_package(result, snapshot, tmp_path / "packages")
    assert Path(package["zip_path"]).is_file()


def test_at45_render_cancellation_keeps_no_success_video(tmp_path, synthetic_render_server):
    from backend.exporter import render_video, RenderCanceled
    calls = [0]
    def cancel():
        calls[0] += 1
        return calls[0] >= 4
    with pytest.raises(RenderCanceled):
        render_video(synthetic_render_server, {"id": "synthetic-cancel", "status": "draft"}, tmp_path,
                     "quick", cancel=cancel)
    assert not list(tmp_path.rglob("*.mp4"))
    failures = list(tmp_path.rglob("failure.json"))
    assert len(failures) == 1
    assert json.loads(failures[0].read_text(encoding="utf-8"))["status"] == "canceled"
