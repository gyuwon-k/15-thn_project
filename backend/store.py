"""MariaDB operational persistence. SQLite is a logical backup format only."""
from __future__ import annotations
import hashlib, json, os, sys, threading, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]
RESOURCE_ROOT = Path(getattr(sys, '_MEIPASS', APP_ROOT))
DATA_ROOT = Path(os.environ.get('PILOT_DATA_DIR', str(Path(os.environ.get('LOCALAPPDATA', APP_ROOT)) / 'PILOT Studio' / 'data'))).resolve()
LOCK = threading.RLock()
SCHEMA_VERSION = 1
TABLES = ('projects', 'snapshots', 'feedback', 'jobs', 'audit', 'request_log')
_TRANSACTION = threading.local()
def now(): return datetime.now(timezone.utc).isoformat()
def uid(prefix=''): return prefix + uuid.uuid4().hex
def dumps(value): return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
def fingerprint(value): return hashlib.sha256(dumps(value).encode('utf-8')).hexdigest()

@contextmanager
def connect():
    active = getattr(_TRANSACTION, 'connection', None)
    if active is not None:
        yield active
        return
    import pymysql
    from .desktop_storage import connection_options
    con = pymysql.connect(**connection_options(), charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
                          connect_timeout=5, read_timeout=30, write_timeout=30, local_infile=False, autocommit=False)
    try:
        with con.cursor() as cur: cur.execute('SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ')
        yield con
        con.commit()
    except BaseException:
        con.rollback(); raise
    finally: con.close()

@contextmanager
def transaction_scope():
    """Keep command mutation and idempotent response in the same transaction."""
    if getattr(_TRANSACTION, 'connection', None) is not None:
        raise RuntimeError('NESTED_TRANSACTION_SCOPE')
    with LOCK, connect() as con:
        _TRANSACTION.connection = con
        try: yield
        finally: _TRANSACTION.connection = None

def request_response(request_id, body_hash):
    with connect() as con:
        row = _fetch(con, 'SELECT * FROM request_log WHERE id=%s', (request_id,), True)
        if row is None: return None
        if row['body_hash'] != body_hash: raise ValueError('IDEMPOTENCY_CONFLICT')
        return json.loads(row['response'])

def save_request_response(request_id, body_hash, response):
    with connect() as con:
        _exec(con, 'INSERT INTO request_log VALUES(%s,%s,%s,%s)', (request_id, body_hash, dumps(response), now()))

def _fetch(con, query, args=(), one=False):
    with con.cursor() as cur:
        cur.execute(query, args)
        return cur.fetchone() if one else cur.fetchall()
def _exec(con, query, args=()):
    with con.cursor() as cur: cur.execute(query, args)

DDL = (
    'CREATE TABLE IF NOT EXISTS schema_migration(version INTEGER PRIMARY KEY, hash CHAR(64) NOT NULL, applied_at VARCHAR(40) NOT NULL) ENGINE=InnoDB',
    'CREATE TABLE IF NOT EXISTS storage_binding(id INTEGER PRIMARY KEY, root_hash CHAR(64) NOT NULL, installation_id VARCHAR(64) NOT NULL) ENGINE=InnoDB',
    'CREATE TABLE IF NOT EXISTS projects(id VARCHAR(64) PRIMARY KEY, body LONGTEXT NOT NULL, updated_at VARCHAR(40) NOT NULL) ENGINE=InnoDB',
    'CREATE TABLE IF NOT EXISTS snapshots(id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL, confirmed INTEGER NOT NULL, body LONGTEXT NOT NULL, hash CHAR(64) NOT NULL, created_at VARCHAR(40) NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id)) ENGINE=InnoDB',
    'CREATE TABLE IF NOT EXISTS feedback(id VARCHAR(64) PRIMARY KEY, snapshot_id VARCHAR(64) UNIQUE NOT NULL, body LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)) ENGINE=InnoDB',
    'CREATE TABLE IF NOT EXISTS jobs(id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL, body LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id)) ENGINE=InnoDB',
    'CREATE TABLE IF NOT EXISTS audit(id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64), action VARCHAR(128) NOT NULL, body LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, FOREIGN KEY(project_id) REFERENCES projects(id)) ENGINE=InnoDB',
    'CREATE TABLE IF NOT EXISTS request_log(id VARCHAR(64) PRIMARY KEY, body_hash CHAR(64) NOT NULL, response LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL) ENGINE=InnoDB',
)
def init():
    from .desktop_storage import ensure_database
    ensure_database(DATA_ROOT, RESOURCE_ROOT)
    with LOCK, connect() as con:
        for sql in DDL: _exec(con, sql)
        migrations = _fetch(con, 'SELECT * FROM schema_migration ORDER BY version')
        if any(r['version'] > SCHEMA_VERSION for r in migrations): raise RuntimeError('SCHEMA_TOO_NEW')
        if migrations and migrations[0]['hash'] != fingerprint(DDL): raise RuntimeError('MIGRATION_CHANGED')
        if not migrations: _exec(con, 'INSERT INTO schema_migration VALUES(%s,%s,%s)', (SCHEMA_VERSION, fingerprint(DDL), now()))
        root_hash = hashlib.sha256(str(DATA_ROOT).casefold().encode('utf-8')).hexdigest()
        binding = _fetch(con, 'SELECT * FROM storage_binding WHERE id=1', one=True)
        if binding and binding['root_hash'] != root_hash: raise RuntimeError('STORAGE_BINDING_MISMATCH')
        if not binding: _exec(con, 'INSERT INTO storage_binding VALUES(1,%s,%s)', (root_hash, uid('installation-')))
        for row in _fetch(con, 'SELECT id,body FROM jobs'):
            job = json.loads(row['body'])
            if job['status'] in ('queued', 'running'):
                job.update(status='failed', message='앱 종료로 작업이 중단되었습니다. 다시 실행해 주세요.', error='INTERRUPTED', ended_at=now())
                _exec(con, 'UPDATE jobs SET body=%s WHERE id=%s', (dumps(job), job['id']))
def projects():
    with connect() as con: return [json.loads(r['body']) for r in _fetch(con, 'SELECT body FROM projects ORDER BY updated_at DESC')]
def get(project_id):
    with connect() as con:
        row = _fetch(con, 'SELECT body FROM projects WHERE id=%s', (project_id,), True)
        if not row: raise KeyError(project_id)
        return json.loads(row['body'])
def save(state, action=None, detail=None):
    state['project']['updated_at'] = now()
    with LOCK, connect() as con:
        _exec(con, 'INSERT INTO projects VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE body=VALUES(body),updated_at=VALUES(updated_at)', (state['project']['id'], dumps(state), now()))
        if action: _exec(con, 'INSERT INTO audit VALUES(%s,%s,%s,%s,%s)', (uid('audit-'), state['project']['id'], action, dumps(detail or {}), now()))
def snapshot(snapshot_id):
    with connect() as con:
        row = _fetch(con, 'SELECT body,hash FROM snapshots WHERE id=%s', (snapshot_id,), True)
        if not row: raise KeyError(snapshot_id)
        data = json.loads(row['body'])
        if fingerprint(data) != row['hash']: raise ValueError('SNAPSHOT_INTEGRITY_FAILED')
        return data
def snapshots(project_id=None, confirmed_only=False):
    with connect() as con:
        rows = _fetch(con, 'SELECT id,body,hash FROM snapshots WHERE (%s IS NULL OR project_id=%s) AND (%s=0 OR confirmed=1) ORDER BY created_at DESC', (project_id, project_id, int(confirmed_only)))
        values=[]
        for row in rows:
            value=json.loads(row['body'])
            if fingerprint(value)!=row['hash']: raise ValueError('SNAPSHOT_INTEGRITY_FAILED')
            values.append(value)
        return values
def put_snapshot(data, confirmed=False, feedback=None):
    with LOCK, connect() as con:
        _exec(con, 'INSERT INTO snapshots VALUES(%s,%s,%s,%s,%s,%s)', (data['id'], data['project']['id'], int(confirmed), dumps(data), fingerprint(data), now()))
        if confirmed and feedback: _exec(con, 'INSERT INTO feedback VALUES(%s,%s,%s,%s)', (uid('feedback-'), data['id'], dumps(feedback), now()))
def put_job(job):
    with LOCK, connect() as con: _exec(con, 'INSERT INTO jobs VALUES(%s,%s,%s,%s) ON DUPLICATE KEY UPDATE body=VALUES(body)', (job['id'], job['project_id'], dumps(job), job['created_at']))
def jobs(project_id):
    with connect() as con: return [json.loads(r['body']) for r in _fetch(con, 'SELECT body FROM jobs WHERE project_id=%s ORDER BY created_at DESC', (project_id,))]
def audit(project_id):
    with connect() as con: return [r | {'body': json.loads(r['body'])} for r in _fetch(con, 'SELECT * FROM audit WHERE project_id=%s ORDER BY created_at DESC LIMIT 100', (project_id,))]
def shutdown():
    from .desktop_storage import shutdown_database
    shutdown_database()
