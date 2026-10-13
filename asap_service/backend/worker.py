"""One-shot CAD workers isolate OCCT CPU work and failures from the API."""
from __future__ import annotations
import argparse
import json
import sys
import traceback
from pathlib import Path

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    from asap_service.backend.storage import write_json, resolve_path
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    try:
        from asap_service.backend.cad_pipeline import parse_step, convert_session, split_units
        directory = resolve_path(request['session_dir'])
        operation = request["operation"]
        if operation == "parse":
            result = parse_step(directory, resolve_path(request['source_path']), request.get('options', {}))
            from asap_service.backend.unit_service import initialize
            result.update(unit_state=initialize(directory), status="unit_review_required")
        elif operation == "convert":
            result = convert_session(directory, request.get("options", {}))
        elif operation == "units":
            from asap_service.backend.unit_service import edit
            result = edit(directory, **request["edit"])
        elif operation == "templates":
            from asap_service.backend.matching_service import parse_templates
            result = parse_templates(directory,request["files"])
        elif operation == "match":
            from asap_service.backend.matching_service import match
            result = match(directory,**request["matching"])
        elif operation == "match_accept":
            from asap_service.backend.matching_service import accept
            result = accept(directory,**request["matching"])
        elif operation == "match_manual":
            from asap_service.backend.unit_service import apply_templates
            result=apply_templates(directory,**request["matching"])
        elif operation == "split":
            result = split_units(directory, request["unit_ids"])
        else:
            raise ValueError("WORKER_OPERATION: unknown CAD operation")
        write_json(Path(args.output), {"ok": True, "result": result})
    except Exception as exc:
        traceback.print_exc()
        message = str(exc)
        write_json(Path(args.output), {"ok": False, "error": {"reason_code": message.split(":")[0] if ":" in message else "PARSE_FAILED", "message": message, "action": "입력 형상과 진단 로그를 확인하세요."}})
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
