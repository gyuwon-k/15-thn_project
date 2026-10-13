"""One executable entrypoint: native desktop by default, isolated workers by flag."""
from multiprocessing import freeze_support
import os
import sys

def main():
    freeze_support()
    os.environ.setdefault('PYTHONUTF8', '1')
    if getattr(sys, 'frozen', False) and (sys.stdout is None or sys.stderr is None):
        from pathlib import Path
        data=Path(os.environ.get('PILOT_DATA_DIR',str(Path(os.environ.get('LOCALAPPDATA',Path.home()))/'PILOT Studio'/'data')))
        folder=data/'logs';folder.mkdir(parents=True,exist_ok=True)
        stream=(folder/('process-'+str(os.getpid())+'.log')).open('a',encoding='utf-8',buffering=1)
        sys.stdout=sys.stdout or stream;sys.stderr=sys.stderr or stream
        sys.__stdout__=sys.__stdout__ or stream;sys.__stderr__=sys.__stderr__ or stream
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        from backend.worker import run_worker
        run_worker(sys.argv[2])
    elif '--asap-worker' in sys.argv:
        sys.argv.remove('--asap-worker')
        from asap_service.backend.worker import main as run_worker
        run_worker()
    elif len(sys.argv) == 4 and sys.argv[1] == '--restore-worker':
        from backend.desktop_storage import restore_worker
        restore_worker(sys.argv[2], sys.argv[3])
    elif '--no-browser' in sys.argv or '--server' in sys.argv:
        if '--server' in sys.argv: sys.argv.remove('--server')
        from backend.main import run
        run()
    else:
        from desktop.shell import run
        run()

if __name__ == '__main__':
    main()
