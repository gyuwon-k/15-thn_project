"""Dedicated user-owned MariaDB lifecycle. No OS service or fallback database."""
from __future__ import annotations
import atexit, base64, ctypes, hashlib, json, os, secrets, shutil, socket, sqlite3, subprocess, threading, time, zipfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
_CONFIG = _PROCESS = _LOCK_HANDLE = _LOG_HANDLE = _ROOT = None
_MUTEX=threading.RLock()

def _protect(raw, decrypt=False):
    if os.name!='nt': raise RuntimeError('WINDOWS_REQUIRED')
    class Blob(ctypes.Structure): _fields_=[('size',ctypes.c_ulong),('data',ctypes.POINTER(ctypes.c_ubyte))]
    buffer=ctypes.create_string_buffer(raw)
    incoming=Blob(len(raw),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_ubyte))); outgoing=Blob()
    fn=ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not fn(ctypes.byref(incoming),None,None,None,None,1,ctypes.byref(outgoing)): raise RuntimeError('CREDENTIAL_PROTECTION_FAILED')
    try:return ctypes.string_at(outgoing.data,outgoing.size)
    finally:ctypes.windll.kernel32.LocalFree(outgoing.data)
def _save_config(path, config):
    path.parent.mkdir(parents=True,exist_ok=True)
    temp=path.with_suffix('.tmp')
    temp.write_text(json.dumps({'format':'windows-dpapi/1','payload':base64.b64encode(_protect(json.dumps(config).encode())).decode()}),encoding='utf-8')
    temp.replace(path)
def _load_config(path):
    data=json.loads(path.read_text(encoding='utf-8'))
    if data.get('format')!='windows-dpapi/1':raise RuntimeError('DB_CONFIG_UNSUPPORTED')
    return json.loads(_protect(base64.b64decode(data['payload']),True))
def _runtime(resource):
    explicit=os.environ.get('PILOT_MARIADB_ROOT')
    options=[Path(explicit)] if explicit else [resource/'runtime'/'mariadb']
    if not getattr(__import__('sys'),'frozen',False):options+=[Path('C:/Program Files/MariaDB 12.3')]
    for folder in options:
        if (folder/'bin'/'mariadbd.exe').is_file() and (folder/'bin'/'mariadb-install-db.exe').is_file():return folder.resolve()
    raise RuntimeError('MARIADB_RUNTIME_MISSING: 설치 프로그램으로 MariaDB 구성 요소를 복구해 주세요.')
def _port():
    with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
def _recover_orphan(runtime,dbdir,config):
    """Only after DATA lock acquired: stop a previous app's exact owned server."""
    import psutil
    owned=[]
    executable=(runtime/'bin'/'mariadbd.exe').resolve()
    for process in psutil.process_iter(['pid','exe','cmdline']):
        try:
            if not process.info['exe'] or Path(process.info['exe']).resolve()!=executable:continue
            args=process.info['cmdline'] or []
            targets=[arg.split('=',1)[1] for arg in args if arg.lower().startswith('--datadir=')]
            if len(targets)==1 and Path(targets[0]).resolve()==dbdir.resolve():owned.append(process)
        except (psutil.Error,OSError):continue
    if not owned:return
    try:
        import pymysql
        con=pymysql.connect(host='127.0.0.1',port=config.get('port',0),user='root',password=config['root_password'],connect_timeout=2)
        with con.cursor() as cur:cur.execute('SHUTDOWN')
        con.close()
    except Exception:pass
    for process in owned:
        try:process.wait(timeout=5)
        except psutil.TimeoutExpired:
            process.terminate()
            try:process.wait(timeout=10)
            except psutil.TimeoutExpired:process.kill();process.wait(timeout=5)
def ensure_database(data_root,resource_root):
    global _CONFIG,_PROCESS,_LOCK_HANDLE,_LOG_HANDLE,_ROOT
    with _MUTEX:
        root=data_root.resolve()
        if _CONFIG is not None:
            if _ROOT!=root:raise RuntimeError('STORAGE_ALREADY_BOUND')
            if _PROCESS.poll() is not None:raise RuntimeError('MARIADB_PROCESS_EXITED')
            return
        import msvcrt
        root.mkdir(parents=True,exist_ok=True)
        lock=(root/'.storage.lock').open('a+b');lock.seek(0)
        if lock.read(1)==b'':lock.write(b'0');lock.flush()
        lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:lock.close();raise RuntimeError('STORAGE_LOCKED: 이 저장소의 앱이 이미 실행 중입니다.')
        _LOCK_HANDLE=lock;_ROOT=root
        try:
            runtime=_runtime(resource_root); config_path=root/'config'/'mariadb.protected.json';dbdir=root/'database'
            fresh=not config_path.is_file()
            config=_load_config(config_path) if not fresh else {'database':'pilot_studio','user':'pilot_app','password':secrets.token_hex(32),'root_password':secrets.token_hex(32),'host':'127.0.0.1'}
            _recover_orphan(runtime,dbdir,config)
            config['port']=_port();root.joinpath('logs').mkdir(exist_ok=True)
            flags=getattr(subprocess,'CREATE_NO_WINDOW',0)
            if fresh:
                if dbdir.exists() and any(dbdir.iterdir()):raise RuntimeError('DB_CONFIG_MISSING: 기존 DB 파일을 보존했습니다. 보호 연결 설정 복구가 필요합니다.')
                result=subprocess.run([str(runtime/'bin'/'mariadb-install-db.exe'),'--datadir='+str(dbdir),'--port='+str(config['port']),'--silent'],capture_output=True,timeout=120,creationflags=flags)
                if result.returncode:
                    (root/'logs'/'database-initialize.log').write_bytes(result.stdout+result.stderr)
                    raise RuntimeError('MARIADB_INITIALIZE_FAILED')
                config['bootstrap_pending']=True
            initfile=root/'config'/'bootstrap.sql';initfile.parent.mkdir(parents=True,exist_ok=True)
            if config.get('bootstrap_pending') or initfile.is_file():
                sql=[f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{config['root_password']}';",'CREATE DATABASE IF NOT EXISTS pilot_studio CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;']
                for host in ('127.0.0.1','localhost'):
                    sql+=[f"CREATE USER IF NOT EXISTS 'pilot_app'@'{host}' IDENTIFIED BY '{config['password']}';",f"GRANT ALL PRIVILEGES ON pilot_studio.* TO 'pilot_app'@'{host}';"]
                initfile.write_text('\n'.join(sql),encoding='utf-8')
                _save_config(config_path,config)
            command=[str(runtime/'bin'/'mariadbd.exe'),'--no-defaults','--basedir='+str(runtime),'--datadir='+str(dbdir),'--bind-address=127.0.0.1','--port='+str(config['port']),'--skip-name-resolve','--max-connections=40','--innodb-buffer-pool-size=64M','--log-error='+str(root/'logs'/'mariadb-error.log')]
            if initfile.is_file():command+=['--init-file='+str(initfile)]
            _LOG_HANDLE=(root/'logs'/'mariadb-process.log').open('ab')
            _PROCESS=subprocess.Popen(command,stdout=_LOG_HANDLE,stderr=subprocess.STDOUT,creationflags=flags)
            import pymysql
            deadline=time.monotonic()+60
            while time.monotonic()<deadline:
                if _PROCESS.poll() is not None:raise RuntimeError('MARIADB_START_FAILED: 데이터베이스 진단 로그를 확인해 주세요.')
                try:
                    con=pymysql.connect(**{k:config[k] for k in ('host','port','user','password','database')},connect_timeout=1);con.close()
                    config.pop('bootstrap_pending',None)
                    _CONFIG=config;_save_config(config_path,config);initfile.unlink(missing_ok=True);return
                except pymysql.Error:time.sleep(.15)
            raise RuntimeError('MARIADB_START_TIMEOUT')
        except BaseException:
            root.joinpath('config','bootstrap.sql').unlink(missing_ok=True)
            shutdown_database();raise
def connection_options():
    if _CONFIG is None:
        from .store import DATA_ROOT,RESOURCE_ROOT
        ensure_database(DATA_ROOT,RESOURCE_ROOT)
    return {k:_CONFIG[k] for k in ('host','port','user','password','database')}
def shutdown_database():
    global _CONFIG,_PROCESS,_LOCK_HANDLE,_LOG_HANDLE,_ROOT
    with _MUTEX:
        if _PROCESS is not None and _PROCESS.poll() is None:
            try:
                import pymysql
                con=pymysql.connect(host='127.0.0.1',port=_CONFIG['port'],user='root',password=_CONFIG['root_password'],connect_timeout=2)
                with con.cursor() as cur:cur.execute('SHUTDOWN')
                con.close()
            except Exception:_PROCESS.terminate()
            try:_PROCESS.wait(timeout=15)
            except subprocess.TimeoutExpired:_PROCESS.kill();_PROCESS.wait(timeout=5)
        if _LOG_HANDLE:_LOG_HANDLE.close()
        if _LOCK_HANDLE:
            import msvcrt
            try:_LOCK_HANDLE.seek(0);msvcrt.locking(_LOCK_HANDLE.fileno(),msvcrt.LK_UNLCK,1)
            finally:_LOCK_HANDLE.close()
        _CONFIG=_PROCESS=_LOCK_HANDLE=_LOG_HANDLE=_ROOT=None
atexit.register(shutdown_database)
def status():
    from . import store
    result={'backend':'mariadb','status':'unavailable','data_dir':str(store.DATA_ROOT),'schema_version':store.SCHEMA_VERSION,'credentials':'windows_user_dpapi','binding':'loopback','backup_format':'pilot-backup/2','last_backup':None}
    try:
        with store.connect() as con:row=store._fetch(con,'SELECT VERSION() AS version',one=True)
        usage=shutil.disk_usage(store.DATA_ROOT)
        result.update(status='connected',server_version=row['version'],host='127.0.0.1',port=_CONFIG['port'],database=_CONFIG['database'],free_bytes=usage.free,total_bytes=usage.total)
        backups=list_backups();result['last_backup']=backups[0] if backups else None
    except Exception as exc:result.update(error=type(exc).__name__,message='저장 서버 연결을 확인해 주세요.')
    return result
def _managed_files(root):
    exclusions={'database','config','backups','restore-staging','work','webview','.storage.lock'}
    for file in root.rglob('*'):
        if file.is_file() and not file.is_symlink() and file.relative_to(root).parts[0] not in exclusions:yield file
def list_backups():
    from . import store
    folder=store.DATA_ROOT/'backups'
    return sorted([json.loads(f.read_text(encoding='utf-8')) for f in folder.glob('*.json')],key=lambda r:r['created_at'],reverse=True) if folder.exists() else []
@contextmanager
def maintenance_guard():
    """Reject active native workers and prevent new ones while copying DATA."""
    from .asap_integration import raw_api
    manager=raw_api.manager
    if manager is None:
        yield
        return
    if not manager.guard.acquire(blocking=False):raise ValueError('ACTIVE_JOB: ASAP 작업 완료 또는 취소 후 백업해 주세요.')
    file_guard=getattr(raw_api,'file_mutation_lock',None)
    file_acquired=False
    try:
        if file_guard is not None:
            file_acquired=file_guard.acquire(blocking=False)
            if not file_acquired:raise ValueError('ACTIVE_STORAGE_MUTATION: 입력 편집 완료 후 백업해 주세요.')
        with manager.state_guard:
            if any(j.get('status') not in ('success','failed','canceled','interrupted') for j in manager.jobs.values()):raise ValueError('ACTIVE_JOB')
            yield
    finally:
        if file_acquired:file_guard.release()
        manager.guard.release()
def create_backup():
    with maintenance_guard():return _create_backup()
def _create_backup():
    from . import store
    with store.LOCK:
        with store.connect() as con:
            for row in store._fetch(con,'SELECT body FROM jobs'):
                if json.loads(row['body']).get('status') in ('queued','running'):raise ValueError('ACTIVE_JOB: 작업 완료 또는 취소 후 백업해 주세요.')
            tables={table:store._fetch(con,'SELECT * FROM `'+table+'`') for table in store.TABLES}
        backup_id=store.uid('backup-');folder=store.DATA_ROOT/'backups';folder.mkdir(parents=True,exist_ok=True)
        stage=folder/(backup_id+'.stage');stage.mkdir()
        temporary=folder/(backup_id+'.partial')
        try:
            sqlitefile=stage/'pilot.sqlite3'
            with sqlite3.connect(sqlitefile) as database:
                database.execute('CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
                database.executemany('INSERT INTO metadata VALUES(?,?)',[('schema_version',str(store.SCHEMA_VERSION)),('source_backend','mariadb')])
                for table,rows in tables.items():
                    database.execute('CREATE TABLE '+table+'(id TEXT PRIMARY KEY,body TEXT NOT NULL)')
                    database.executemany('INSERT INTO '+table+' VALUES(?,?)',[(row['id'],store.dumps(row)) for row in rows])
            manifest={'format':'pilot-backup/2','source_backend':'mariadb','snapshot_format':'sqlite3','schema_version':store.SCHEMA_VERSION,'created_at':store.now(),'source_data_dir':str(store.DATA_ROOT),'table_counts':{t:len(r) for t,r in tables.items()},'files':[]}
            entries=[('database/pilot.sqlite3',sqlitefile)]+[('data/'+f.relative_to(store.DATA_ROOT).as_posix(),f) for f in _managed_files(store.DATA_ROOT)]
            with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as out:
                for name,file in entries:
                    before=file.stat();digest=hashlib.sha256()
                    with file.open('rb') as source,out.open(name,'w',force_zip64=True) as destination:
                        while chunk:=source.read(1024*1024):digest.update(chunk);destination.write(chunk)
                    after=file.stat()
                    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):raise ValueError('FILE_CHANGED_DURING_BACKUP')
                    manifest['files'].append({'path':name,'size':before.st_size,'sha256':digest.hexdigest()})
                out.writestr('manifest.json',store.dumps(manifest))
            validate_backup(temporary);archive=folder/(backup_id+'.zip');temporary.replace(archive)
            record={'id':backup_id,'created_at':manifest['created_at'],'size_bytes':archive.stat().st_size,'sha256':file_hash(archive),'file_count':len(manifest['files']),'table_counts':manifest['table_counts'],'format':manifest['format'],'download_url':'/api/backups/'+backup_id}
            (folder/(backup_id+'.json')).write_text(store.dumps(record),encoding='utf-8');return record
        finally:shutil.rmtree(stage,ignore_errors=True);temporary.unlink(missing_ok=True)
def file_hash(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as source:
        while chunk:=source.read(1024*1024):digest.update(chunk)
    return digest.hexdigest()
def validate_backup(path):
    from . import store
    with zipfile.ZipFile(path) as archive:
        infos=archive.infolist()
        if len(infos)>200000 or sum(i.file_size for i in infos)>16*1024**3:raise ValueError('BACKUP_RESOURCE_LIMIT')
        names=[i.filename for i in infos]
        if len(set(names))!=len(names):raise ValueError('BACKUP_DUPLICATE_PATH')
        for info in infos:
            parts=PurePosixPath(info.filename).parts
            if info.filename.startswith(('/','\\')) or '\\' in info.filename or ':' in info.filename or '..' in parts or not parts or (info.external_attr>>16)&0o170000==0o120000:raise ValueError('BACKUP_UNSAFE_PATH')
        if archive.getinfo('manifest.json').file_size>16*1024**2:raise ValueError('BACKUP_MANIFEST_LIMIT')
        manifest=json.loads(archive.read('manifest.json'))
        if manifest.get('format')!='pilot-backup/2' or manifest.get('schema_version')!=store.SCHEMA_VERSION:raise ValueError('BACKUP_VERSION_UNSUPPORTED')
        expected={f['path'] for f in manifest['files']}|{'manifest.json'}
        if expected!=set(names) or 'database/pilot.sqlite3' not in expected:raise ValueError('BACKUP_FILE_COVERAGE')
        for entry in manifest['files']:
            if not (entry['path'].startswith('data/') or entry['path']=='database/pilot.sqlite3'):raise ValueError('BACKUP_UNEXPECTED_PATH')
            digest=hashlib.sha256();size=0
            with archive.open(entry['path']) as source:
                while chunk:=source.read(1024*1024):digest.update(chunk);size+=len(chunk)
            if size!=entry['size'] or digest.hexdigest()!=entry['sha256']:raise ValueError('BACKUP_HASH_MISMATCH')
        return manifest

def restore_backup(path):
    """Validate and build an independently usable new DATA/private database."""
    from . import store
    with store.LOCK:
        manifest=validate_backup(path)
        restore_id=store.uid('restore-')
        destination=store.DATA_ROOT.parent/'restored'/restore_id
        stage=store.DATA_ROOT/'restore-staging'/restore_id
        stage.mkdir(parents=True)
        with zipfile.ZipFile(path) as archive: archive.extractall(stage)
        request=stage/'request.json';output=stage/'result.json'
        request.write_text(store.dumps({'stage':str(stage),'destination':str(destination),'manifest':manifest}),encoding='utf-8')
        import sys
        command=[sys.executable]+([] if getattr(sys,'frozen',False) else [str(store.APP_ROOT/'launcher.py')])+['--restore-worker',str(request),str(output)]
        result=subprocess.run(command,capture_output=True,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),env=os.environ|{'PILOT_DATA_DIR':str(destination),'PYTHONUTF8':'1'})
        if result.returncode or not output.is_file():
            raise RuntimeError('RESTORE_FAILED: 복원 준비 폴더와 진단을 보존했습니다.')
        restored=json.loads(output.read_text(encoding='utf-8'))
        if restored.get('status')!='verified':raise RuntimeError('RESTORE_VERIFY_FAILED')
        shutil.rmtree(stage,ignore_errors=True)
        return restored

def restore_worker(request_path,output_path):
    """Separate process prevents touching the active storage binding."""
    from . import store
    request=json.loads(Path(request_path).read_text(encoding='utf-8'))
    stage=Path(request['stage']);destination=Path(request['destination']);manifest=request['manifest']
    rows={}
    with sqlite3.connect(stage/'database/pilot.sqlite3') as snapshot:
        if snapshot.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise ValueError('BACKUP_DATABASE_CORRUPT')
        for table in store.TABLES:
            rows[table]=[json.loads(r[0]) for r in snapshot.execute('SELECT body FROM '+table)]
            if len(rows[table])!=manifest['table_counts'][table]:raise ValueError('BACKUP_ROW_COUNT')
    for row in rows['snapshots']:
        if store.fingerprint(json.loads(row['body']))!=row['hash']:raise ValueError('SNAPSHOT_INTEGRITY_FAILED')
    destination.mkdir(parents=True,exist_ok=False)
    if (stage/'data').exists():shutil.copytree(stage/'data',destination,dirs_exist_ok=True)
    # Absolute source paths are resolved at read time without changing original
    # immutable snapshot rows or their hashes.
    (destination/'path_map.json').write_text(store.dumps({'source_root':manifest['source_data_dir'],'destination_root':str(destination)}),encoding='utf-8')
    store.DATA_ROOT=destination.resolve()
    try:
        store.init()
        with store.connect() as con:
            for table in store.TABLES:
                for row in rows[table]:
                    keys=list(row)
                    store._exec(con,'INSERT INTO `'+table+'` ('+','.join('`'+k+'`' for k in keys)+') VALUES('+','.join(['%s']*len(keys))+')',[row[k] for k in keys])
            for table in store.TABLES:
                actual=store._fetch(con,'SELECT * FROM `'+table+'`')
                if store.fingerprint(sorted(actual,key=lambda r:r['id']))!=store.fingerprint(sorted(rows[table],key=lambda r:r['id'])):raise ValueError('RESTORE_ROW_HASH_MISMATCH')
        result={'id':destination.name,'status':'verified','data_dir':str(destination),'table_counts':manifest['table_counts'],'file_count':len(manifest['files'])-1,'activated':False,'message':'새 DB와 DATA에 검증 복원했습니다. 현재 저장소는 변경하지 않았습니다.'}
        (destination/'restore-result.json').write_text(store.dumps(result),encoding='utf-8')
        Path(output_path).write_text(store.dumps(result),encoding='utf-8')
    finally:store.shutdown()

def resolve_path(value):
    from . import store
    path=Path(value)
    mapping=store.DATA_ROOT/'path_map.json'
    if mapping.is_file():
        data=json.loads(mapping.read_text(encoding='utf-8'))
        try:return store.DATA_ROOT/path.relative_to(data['source_root'])
        except ValueError:pass
    if not path.is_absolute():return store.DATA_ROOT/path
    return path
