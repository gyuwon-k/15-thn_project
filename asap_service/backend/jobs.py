from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from asap_service.backend.storage import DATA, ROOT, now, read_json, write_json, session_path, resolve_path
from asap_service.backend.transport import command, translate_request, process_kwargs, terminate_tree

TERMINAL = {"success", "failed", "canceled", "interrupted"}

class BusyError(Exception):
    pass

class JobManager:
    def __init__(self):
        self.guard = threading.Lock()
        self.state_guard = threading.RLock()
        self.jobs = {}
        self.cancel_events = {}
        self.processes = {}
        self.threads = {}
        self.closing = False
        (DATA / "jobs").mkdir(parents=True, exist_ok=True)
        for file in (DATA / "jobs").glob("*.json"):
            record = read_json(file)
            if record["status"] not in TERMINAL:
                record.update(status="interrupted", stage="interrupted", ended_at=now(), error={"reason_code": "INTERRUPTED", "message": "서버 종료로 중단된 작업입니다.", "action": "저장된 진단을 확인한 뒤 다시 실행하세요."})
                if record.get("type") == "run" and record.get("run_id"):
                    run_dir = session_path(record["session_id"]) / "runs" / record["run_id"]
                    partial = self._partial_output(run_dir / "engine_result.json") or self._partial_output(run_dir / "result.json")
                    partial.update(reason_code="INTERRUPTED", outer_status="interrupted", status="interrupted")
                    partial.setdefault("sequence_status", "failed")
                    partial.setdefault("path_status", "unavailable")
                    self._finish_run(record, partial)
                write_json(file, record)
                self._session_status(record, "interrupted", current_only=True)
            self.jobs[record["id"]] = record

    def _save(self, record):
        with self.state_guard:
            self.jobs[record["id"]] = record
            write_json(DATA / "jobs" / (record["id"]+".json"), record)

    @staticmethod
    def _partial_output(path):
        try:
            output = read_json(path, {})
        except (OSError, ValueError):
            return {}
        return output if isinstance(output, dict) else {}

    def _session_status(self, record, status, current_only=False):
        if not record.get("session_id"):
            return
        path = session_path(record["session_id"]) / "session.json"
        info = read_json(path, {"id": record["session_id"]})
        if current_only and info.get("job_id") not in {None, record["id"]}:
            return
        info.update(status=status, updated_at=now(), job_id=record["id"])
        write_json(path, info)

    def start(self, kind, session_id, request=None, config=None, run_id=None, timeout_s=1800, prepare=None):
        if not self.guard.acquire(blocking=False):
            raise BusyError("다른 작업을 처리하고 있습니다. 완료 또는 취소 후 실행하세요.")
        job_id = directory = session_file = previous_session = None
        session_attempted = worker_started = False
        try:
            with self.state_guard:
                if self.closing:
                    raise BusyError("서버가 종료 중입니다.")
            if prepare is not None:
                request = prepare()
            if not isinstance(request, dict):
                raise ValueError("JOB_REQUEST: 작업 요청이 필요합니다.")
            job_id = uuid.uuid4().hex
            directory = DATA / "jobs" / job_id
            directory.mkdir(parents=True, exist_ok=True)
            request_path = directory / "request.json"
            output_path = directory / "output.json"
            record = {"id": job_id, "type": kind, "session_id": session_id, "run_id": run_id,
                      "status": "queued", "stage": kind, "progress": 0, "created_at": now(),
                      "elapsed_s": 0, "cancel_requested": False, "log_file": str(directory / "worker.log")}
            if kind == "run":
                request = translate_request(request, config["engine"])
                output_path = Path(request["host_output_dir"]) / "engine_result.json"
                request.pop("host_output_dir", None)
                write_json(request_path, request)
                cmd = command(config["engine"], "run", request_path, output_path)
            else:
                write_json(request_path, request)
                if getattr(sys, 'frozen', False):
                    cmd = [sys.executable, '--asap-worker', '--request', str(request_path), '--output', str(output_path)]
                else:
                    cmd = [sys.executable, '-u', str(ROOT / 'backend' / 'worker.py'), '--request', str(request_path), '--output', str(output_path)]
            with self.state_guard:
                if self.closing:
                    raise BusyError("서버가 종료 중입니다.")
                self.cancel_events[job_id] = threading.Event()
                self._save(record)
                if session_id:
                    session_file = session_path(session_id) / "session.json"
                    previous_session = read_json(session_file)
                session_attempted = True
                self._session_status(record, {"parse": "parsing", "convert": "converting", "split": "splitting", "units": "unit_editing",
                    "templates":"template_parsing","match":"matching","match_accept":"unit_editing","match_manual":"unit_editing","run": "running"}[kind])
                thread = threading.Thread(target=self._run, args=(record, cmd, output_path, timeout_s, config), daemon=True)
                self.threads[job_id] = thread
                thread.start()
                worker_started = True
            return job_id
        except BaseException:
            with self.state_guard:
                self.jobs.pop(job_id, None)
                self.cancel_events.pop(job_id, None)
                self.threads.pop(job_id, None)
                if job_id:
                    try:
                        (DATA / "jobs" / (job_id + ".json")).unlink(missing_ok=True)
                    except OSError:
                        pass
                if session_attempted and session_file:
                    try:
                        if previous_session is None:
                            session_file.unlink(missing_ok=True)
                        else:
                            write_json(session_file, previous_session)
                    except OSError:
                        pass
                if directory:
                    cleanup_root = (DATA / "jobs").resolve()
                    cleanup_directory = directory.resolve()
                    if cleanup_directory.parent == cleanup_root:
                        shutil.rmtree(cleanup_directory, ignore_errors=True)
            raise
        finally:
            if not worker_started:
                self.guard.release()

    def _terminate(self, process, record, config):
        if config and config["engine"].get("transport") == "wsl" and record.get("run_id"):
            runtime_file = session_path(record["session_id"]) / "runs" / record["run_id"] / "process.json"
            runtime = read_json(runtime_file, {})
            pid, pgid = runtime.get("pid"), runtime.get("pgid")
            # setsid gives this job an isolated process group, never the distro.
            request = read_json(DATA / "jobs" / record["id"] / "request.json", {})
            if isinstance(pid, int) and pid > 1 and pid == pgid and runtime.get("nonce") == request.get("nonce") and request.get("nonce"):
                prefix = ["wsl.exe"]
                distribution = config["engine"].get("distribution")
                if distribution:
                    prefix += ["--distribution", distribution]
                for signal in ("-TERM", "-KILL"):
                    try:
                        subprocess.run(prefix + ["--exec", "/bin/kill", signal, "--", "-"+str(pgid)], capture_output=True, timeout=5, **process_kwargs())
                    except (OSError, subprocess.TimeoutExpired):
                        pass
        terminate_tree(process)

    def _run(self, record, cmd, output_path, timeout_s, config):
        started = time.monotonic()
        process = None
        terminal_saved = False
        cancel_event = self.cancel_events[record["id"]]
        try:
            with self.state_guard:
                if cancel_event.is_set():
                    raise InterruptedError("CANCELED")
                record.update(status="running", started_at=now())
                self._save(record)
            env = os.environ.copy()
            env.update(PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", OMP_NUM_THREADS="2", MKL_NUM_THREADS="2")
            env['PILOT_ASAP_DATA'] = str(DATA)
            with Path(record["log_file"]).open("wb") as log:
                if cancel_event.is_set():
                    raise InterruptedError("CANCELED")
                process = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, env=env, **process_kwargs())
                with self.state_guard:
                    self.processes[record["id"]] = process
                if cancel_event.is_set():
                    self._terminate(process, record, config)
                    raise InterruptedError("CANCELED")
                while process.poll() is None:
                    with self.state_guard:
                        record["elapsed_s"] = round(time.monotonic()-started, 2)
                    if cancel_event.is_set():
                        self._terminate(process, record, config)
                        raise InterruptedError("CANCELED")
                    if record["elapsed_s"] > timeout_s:
                        self._terminate(process, record, config)
                        raise TimeoutError("TIMEOUT")
                    self._save(record)
                    cancel_event.wait(0.5)
                with self.state_guard:
                    record["exit_code"] = process.returncode
            if cancel_event.is_set():
                raise InterruptedError("CANCELED")
            output = read_json(output_path)
            if not isinstance(output, dict) or not output:
                raise RuntimeError("ENGINE_ERROR: 작업 결과 파일이 생성되지 않았습니다. 로그를 확인하세요.")
            # Cancel and terminal publication share one commit boundary. A cancel
            # accepted while output is being read must win; after this lock the
            # cancel endpoint observes the completed terminal record.
            with self.state_guard:
                if cancel_event.is_set():
                    raise InterruptedError("CANCELED")
                if record["type"] == "run":
                    if output.get("reason_code") == "RUNNING":
                        code = process.returncode
                        output.update(reason_code="ENGINE_CRASH", message="ASAP 엔진 프로세스가 결과를 완료하기 전에 종료됐습니다. 종료 코드: " + str(code) + " (" + hex(code & 0xffffffff) + ")", action="엔진 로그와 네이티브 빌드·입력 형상을 확인하세요.")
                        if output.get("sequence_status") == "success":
                            output["path_status"] = "failed"
                    if output.get("sequence_status") == "success":
                        record.update(status="success", stage="ready_to_view", progress=1)
                        session_status = "ready_to_view"
                    else:
                        record.update(status="failed", stage="unresolved", error={"reason_code": output.get("reason_code", "ENGINE_ERROR"), "message": output.get("message", "현재 설정에서 전체 조립순서를 찾지 못했습니다."), "action": "결과와 로그의 종료 이유를 확인하세요."})
                        session_status = "unresolved"
                    self._finish_run(record, output)
                    record["result"] = {"run_id": record["run_id"], "sequence_status": output.get("sequence_status"), "path_status": output.get("path_status")}
                    self._session_status(record, session_status)
                else:
                    if not output.get("ok"):
                        error = output.get("error", {})
                        raise RuntimeError(error.get("reason_code", "PARSE_FAILED")+": "+error.get("message", "CAD worker failed"))
                    result = output["result"]
                    if record["type"] == "convert":
                        write_json(session_path(record["session_id"]) / "conversion.json", result)
                        status = "ready" if result.get("ready", result.get("status") == "ready") else "quality_review_required"
                    else:
                        status = result.get("status", "parsed")
                    record.update(status="success", stage=status, progress=1, result=result)
                    self._session_status(record, status)
                record.update(ended_at=now(), elapsed_s=round(time.monotonic()-started, 2))
                self._save(record)
                terminal_saved = True
        except Exception as exc:
            with self.state_guard:
                reason = "CANCELED" if cancel_event.is_set() or isinstance(exc, InterruptedError) else "TIMEOUT" if isinstance(exc, TimeoutError) else str(exc).split(":")[0]
                if len(reason) > 80 or not reason.replace("_", "").isalnum():
                    reason = "ENGINE_ERROR" if record["type"] == "run" else "PARSE_FAILED"
                record.update(status="canceled" if reason == "CANCELED" else "failed", stage=reason.lower(), error={"reason_code": reason, "message": "CANCELED" if reason == "CANCELED" else str(exc), "action": "입력·환경·작업 로그를 확인하고 다시 실행하세요."})
                if record["type"] == "run":
                    partial = self._partial_output(output_path)
                    partial.update(reason_code=reason, outer_status=record["status"], elapsed_s=round(time.monotonic()-started,2))
                    if reason == "TIMEOUT":
                        partial.update(message="전체 실행 제한시간 안에 작업을 완료하지 못했습니다. 준비 상태와 마지막 탐색 로그를 확인하세요.", action="전처리 성공 여부·실제 평가 수를 확인하고 조립 단위와 탐색 조건을 검토하세요.")
                    elif reason == "CANCELED":
                        partial.update(message="사용자 요청으로 실행을 중지했습니다.", action="저장된 입력과 준비 결과를 확인할 수 있습니다.")
                    partial.setdefault("sequence_status", "failed")
                    partial.setdefault("path_status", "unavailable")
                    self._finish_run(record, partial)
                self._session_status(record, record["status"])
                record.update(ended_at=now(), elapsed_s=round(time.monotonic()-started, 2))
                self._save(record)
                terminal_saved = True
        finally:
            try:
                if process and process.poll() is None:
                    self._terminate(process, record, config)
            finally:
                try:
                    if not terminal_saved:
                        with self.state_guard:
                            record.update(ended_at=now(), elapsed_s=round(time.monotonic()-started, 2))
                            self._save(record)
                finally:
                    with self.state_guard:
                        self.processes.pop(record["id"], None)
                        self.threads.pop(record["id"], None)
                        self.cancel_events.pop(record["id"], None)
                        self.guard.release()

    def _finish_run(self, record, output):
        directory = session_path(record["session_id"])
        run_dir = directory / "runs" / record["run_id"]
        run_config = read_json(run_dir / "run_config.json", {})
        output.update(id=record["run_id"], run_id=record["run_id"], session_id=record["session_id"],
                      settings=run_config.get("settings", {}), part_map=read_json(run_dir / "part_map.json", {}),
                      coordinate_map=read_json(run_dir / "coordinate_map.json", {}), completed_at=now(),
                      input_revision=run_config.get("input_revision"), job_id=record["id"])
        output.update(unit_revision=run_config.get("unit_revision"), unit_manifest_hash=run_config.get("unit_manifest_hash"))
        if record["status"] in TERMINAL:
            output.update(status=record["status"], outer_status=record["status"])
        output.setdefault("schema_version", "asap-test-result/1")
        output.setdefault("checked_scope", "Rigid-body ASAP with gravity/support approximation; field rules, tools, torque and flexible processes unverified")
        write_json(run_dir / "result.json", output)

    def get(self, job_id):
        with self.state_guard:
            source = self.jobs.get(job_id)
            if source is None:
                raise FileNotFoundError("JOB_NOT_FOUND")
            result = dict(source)
        log_path = resolve_path(result.pop('log_file'))
        if log_path.exists():
            with log_path.open("rb") as handle:
                handle.seek(max(0, log_path.stat().st_size - 50000))
                result["logs"] = handle.read().decode("utf-8", errors="replace")
        else:
            result["logs"] = ""
        for line in reversed(result["logs"].splitlines()):
            if not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict) and "stage" in event:
                    if result["status"] not in TERMINAL:
                        result["stage"] = event["stage"]
                        result["stage_message"] = event.get("message", "")
                        if "progress" in event:
                            result["progress"] = event["progress"]
                    if "evaluation_count" in event:
                        result["evaluation_count"] = event["evaluation_count"]
                    break
            except (ValueError, TypeError):
                pass
        # Full CAD arrays stay in artifact endpoints, not polling payloads.
        if isinstance(result.get("result"), dict) and result["type"] != "run":
            result["result"] = {k:v for k,v in result["result"].items() if k not in {"units", "meshes", "part_map", "coordinate_map", "quality"}}
        return result

    def cancel(self, job_id):
        with self.state_guard:
            record = self.jobs.get(job_id)
            if record is None:
                raise FileNotFoundError("JOB_NOT_FOUND")
            if record["status"] in TERMINAL:
                return self.get(job_id)
            self.cancel_events[job_id].set()
            record["cancel_requested"] = True
            self._save(record)
        return self.get(job_id)

    def shutdown(self):
        with self.state_guard:
            self.closing = True
            for job_id, event in self.cancel_events.items():
                record = self.jobs[job_id]
                if record["status"] not in TERMINAL:
                    event.set()
                    record["cancel_requested"] = True
                    try:
                        self._save(record)
                    except OSError:
                        pass
            threads = list(self.threads.values())
        deadline = time.monotonic()+12
        for thread in threads:
            if thread is not threading.current_thread():
                thread.join(timeout=max(0, deadline-time.monotonic()))
