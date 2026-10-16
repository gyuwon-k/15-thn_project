"""Durable, cancelable process jobs; stale results never replace edited plans."""
from __future__ import annotations
import copy
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from .domain import DomainError, digest, geometry_hash, model_hash, now, uid


def atomic_json(path, value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+"."+uid()+".tmp")
    with temporary.open("w",encoding="utf-8") as stream:
        json.dump(value,stream,ensure_ascii=False,allow_nan=False)
        stream.flush();os.fsync(stream.fileno())
    for attempt in range(40):
        try:
            temporary.replace(path)
            return
        except PermissionError:
            # Windows readers can briefly hold a non-delete-sharing handle.
            # Keep the old complete JSON visible and retry publication.
            if attempt==39:
                temporary.unlink(missing_ok=True)
                raise
            time.sleep(.025*(1+min(attempt,3)))


def path_values(value, root, encode=False):
    if isinstance(value,dict):return {k:path_values(v,root,encode) for k,v in value.items()}
    if isinstance(value,list):return [path_values(v,root,encode) for v in value]
    if isinstance(value,str):
        if not encode and value.startswith("@data/"):
            result=(root/value[6:]).resolve()
            if not result.is_relative_to(root.resolve()):raise DomainError("INVALID_REFERENCE","관리 파일 경로가 잘못되었습니다.")
            return str(result)
        if encode:
            try:
                path=Path(value)
                if path.is_absolute() and path.resolve().is_relative_to(root.resolve()):
                    return "@data/"+path.resolve().relative_to(root.resolve()).as_posix()
            except (OSError,ValueError):pass
    return value


class JobService:
    def __init__(self, storage, data_dir, apply_result, release):
        self.storage=storage;self.root=Path(data_dir);self.apply_result=apply_result;self.release=release
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix="pilot-job")
        self.lock=threading.RLock();self.cad_lock=threading.Lock();self.stopping=False
        self.cancel_events={};self.processes={}
        for job in storage.list_jobs():
            if job.get("status") in ("queued","running","cancel_requested"):
                job.update(status="interrupted",finished_at=now(),message="이전 실행이 종료되어 작업이 중단되었습니다.")
                storage.save_job(job)

    def submit(self, project, kind, payload, request_id=None):
        with self.lock:
            if self.stopping:raise DomainError("SHUTTING_DOWN","프로그램이 종료 중입니다.")
            request_hash=digest({"kind":kind,"payload":payload,"project_revision":project["revision"]})
            existing=self.storage.list_jobs(project["id"])
            for job in existing:
                if request_id and job.get("request_id")==request_id:
                    if job.get("request_hash")!=request_hash:
                        raise DomainError("REQUEST_ID_CONFLICT","같은 요청 ID를 다른 작업 내용에 사용할 수 없습니다.")
                    return job
            for job in existing:
                if job.get("status") in ("queued","running","cancel_requested"):
                    raise DomainError("JOB_BUSY","현재 작업이 끝나거나 취소된 뒤 실행해 주세요.")
            ident=uid("job-");folder=self.root/"jobs"/ident
            folder.mkdir(parents=True,exist_ok=True)
            job={"id":ident,"project_id":project["id"],"kind":kind,"status":"queued",
                 "progress":None,"message":"작업 대기", "created_at":now(),"input_revision":project["revision"],
                 "model_hash":model_hash(project),"geometry_hash":geometry_hash(project),
                 "build_id":self.release["build_id"],"request_id":request_id,"request_hash":request_hash,"diagnostics":[]}
            if kind=="import":job["source_files"]=payload.get("source_files",[])
            request={"kind":kind,"payload":payload,"data_dir":str(self.root),"folder":str(folder),"release":self.release}
            atomic_json(folder/"request.json",request)
            self.storage.create_job(job);self.cancel_events[ident]=threading.Event()
            self.pool.submit(self._run,job,folder)
            return copy.deepcopy(job)

    def cancel(self, ident):
        with self.lock:
            job=self.storage.get_job(ident)
            if not job:raise KeyError(ident)
            if job["status"] not in ("queued","running","cancel_requested"):return job
            self.cancel_events.setdefault(ident,threading.Event()).set()
            (self.root/"jobs"/ident/"cancel").touch()
            job.update(status="cancel_requested",message="작업 종료를 요청했습니다.")
            self.storage.save_job(job);return job

    def _run(self, job, folder):
        cancel=self.cancel_events[job["id"]]
        guard=self.cad_lock if job["kind"] in ("import","generate","validate","verify_matching","process_suggestions") else threading.Lock()
        process=None
        try:
            with guard:
                if cancel.is_set():raise InterruptedError("취소되었습니다.")
                job.update(status="running",started_at=now(),message="작업 시작")
                self.storage.save_job(job)
                command=[sys.executable,"--worker",str(folder/"request.json")] if getattr(sys,"frozen",False) else [sys.executable,"-X","utf8","-m","backend.worker",str(folder/"request.json")]
                environment=os.environ.copy();environment["PYTHONUTF8"]="1";environment["PILOT_BUILD_ID"]=self.release["build_id"]
                with (folder/"process.log").open("wb") as log:
                    process=subprocess.Popen(command,cwd=Path(__file__).resolve().parents[1],env=environment,
                        stdout=log,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                    self.processes[job["id"]]=process
                    deadline=time.monotonic()+float(json.loads((folder/"request.json").read_text(encoding="utf-8"))["payload"].get("timeout_seconds",7200))
                    last_progress=None
                    while process.poll() is None:
                        if cancel.is_set() or self.stopping:
                            self._terminate(process);raise InterruptedError("작업이 취소되었습니다.")
                        if time.monotonic()>deadline:
                            self._terminate(process);raise TimeoutError("작업 시간 예산을 초과했습니다.")
                        progress_path=folder/"progress.json"
                        if progress_path.is_file():
                            try:
                                progress=json.loads(progress_path.read_text(encoding="utf-8"))
                                if progress!=last_progress:
                                    job.update(progress);job["status"]="running";self.storage.save_job(job);last_progress=progress
                            except (OSError,json.JSONDecodeError):pass
                        cancel.wait(.35)
                if cancel.is_set():raise InterruptedError("작업이 취소되었습니다.")
                result_path=folder/"result.json"
                if not result_path.is_file():raise RuntimeError(f"작업 프로세스가 결과 없이 종료되었습니다. 종료 코드: {process.returncode}")
                envelope=json.loads(result_path.read_text(encoding="utf-8"))
                if envelope.get("error"):
                    job["diagnostics"]=envelope.get("diagnostics",[])
                    raise RuntimeError(envelope["error"])
                if process.returncode:raise RuntimeError(f"작업 종료 코드: {process.returncode}")
                result=path_values(envelope["result"],self.root,encode=True)
                published=self.apply_result(job,result)
                job.update(status="succeeded",progress=100,message=published.get("message","작업 완료"),
                           result=published,finished_at=now())
        except InterruptedError as exc:job.update(status="canceled",message=str(exc),finished_at=now())
        except Exception as exc:
            diagnostic_path=folder/'publication-error.log'
            try:diagnostic_path.write_text(traceback.format_exc(),encoding='utf-8')
            except OSError:pass
            job.update(status="failed",message=str(exc),error_code=getattr(exc,"code",type(exc).__name__),finished_at=now(),
                error_details=getattr(exc,'details',{}),publication_diagnostic='@data/'+diagnostic_path.relative_to(self.root).as_posix())
        finally:
            self.processes.pop(job["id"],None)
            self.storage.save_job(job)
            self.cancel_events.pop(job["id"],None)

    @staticmethod
    def _terminate(process):
        try:
            import psutil
            parent=psutil.Process(process.pid);children=parent.children(recursive=True)
            for child in reversed(children):
                try:child.terminate()
                except psutil.Error:pass
            process.terminate()
            _,alive=psutil.wait_procs(children,timeout=2)
            for child in alive:
                try:child.kill()
                except psutil.Error:pass
            try:process.wait(timeout=3)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
        except Exception:
            if process.poll() is None:process.kill()

    def close(self):
        self.stopping=True
        for event in list(self.cancel_events.values()):event.set()
        self.pool.shutdown(wait=True,cancel_futures=False)
