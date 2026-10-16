"""Exercise frozen CAD import and planning with a known small STEP fixture."""
from pathlib import Path
import json
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend.domain import new_project
from backend.release import load_manifest
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox
from OCP.STEPControl import STEPControl_Writer, STEPControl_AsIs
from OCP.gp import gp_Pnt


def main():
    executable = Path(sys.argv[1]).resolve()
    destination = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else ROOT / "work" / "frozen-worker-smoke"
    destination.mkdir(parents=True, exist_ok=True)
    step = destination / "known-three-solids.step"
    writer = STEPControl_Writer()
    for origin, lengths in [((0, 0, 0), (120, 80, 5)), ((10, 10, 5), (30, 20, 5)), ((60, 10, 5), (20, 20, 8))]:
        writer.Transfer(BRepPrimAPI_MakeBox(gp_Pnt(*origin), *lengths).Shape(), STEPControl_AsIs)
    writer.Write(str(step))
    manifest = load_manifest(executable.parent / "_internal")
    environment = dict(os.environ, PILOT_BUILD_ID=manifest["build_id"], PILOT_DATA_DIR=str(destination))
    def worker(kind, payload):
        folder = destination / kind
        folder.mkdir(exist_ok=True)
        request = {"kind": kind, "payload": payload, "data_dir": str(destination), "folder": str(folder), "release": manifest}
        (folder / "request.json").write_text(json.dumps(request), encoding="utf-8")
        with (folder / "frozen-process.log").open("wb") as log:
            process = subprocess.run([str(executable), "--worker", str(folder / "request.json")], stdout=log, stderr=subprocess.STDOUT, env=environment, timeout=240, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        envelope = json.loads((folder / "result.json").read_text(encoding="utf-8"))
        if process.returncode or envelope.get("error"):
            raise RuntimeError(envelope.get("error", "FROZEN_WORKER_EXIT"))
        return envelope["result"]
    model = worker("import", {"assembly_path": str(step), "part_paths": [], "cache_dir": str(destination / "cache")})
    project = new_project({"name": "Frozen runtime fixture"})
    project.update(model=model, input_revision="fixture-input-1")
    project["rules"]["fixed_unit_ids"] = [unit["id"] for unit in model["units"] if unit.get("fixed")]
    project["rules"]["max_seconds"] = 30
    result = worker("generate", {"project": project, "cache_dir": str(destination / "analysis")})
    report = {"build_id": manifest["build_id"], "occurrences": len(model["occurrences"]), "units": len(model["units"]), "steps": len(result["plan"].get("steps", [])), "validation": result["validation"].get("status"), "status": "passed" if result["validation"].get("status") == "PASS" else "failed"}
    (destination / "smoke-result.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report))
    if report["status"] != "passed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
