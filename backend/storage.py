"""Project persistence backed by one app-owned MariaDB and immutable files.

No alternate database is selected when MariaDB is unavailable. Domain objects
are versioned JSON documents; transactional relational keys enforce ownership,
optimistic concurrency, and immutable revision/snapshot history.
"""
from __future__ import annotations

import atexit
import base64
import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import secrets
import shutil
import socket
import subprocess
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone

import pymysql

SCHEMA_VERSION = 1
TABLES = ("project", "project_revision", "change_event", "job_run", "confirmation_snapshot", "file_asset", "app_setting", "backup_record")
MAX_PACKET_BYTES = 64 * 1024 * 1024


class StorageError(RuntimeError):
    def __init__(self, code, message, details=None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


class PacketBoundedCursor(pymysql.cursors.DictCursor):
    """Bound the escaped UTF-8 command, not the unescaped JSON character count."""
    def execute(self, query, args=None):
        query = self.mogrify(query, args)
        connection = self._get_db()
        encoded = query.encode(connection.encoding, 'surrogateescape') if isinstance(query, str) else query
        limit = getattr(connection, 'pilot_packet_limit', MAX_PACKET_BYTES)
        if len(encoded) + 1024 >= limit:
            raise StorageError('STORAGE_PAYLOAD_TOO_LARGE',
                '저장 데이터가 DB 전송 한도를 초과했습니다. 기존 저장본은 유지되며 계산 결과와 진단 파일을 확인해 주세요.',
                {'query_bytes': len(encoded), 'packet_limit_bytes': limit})
        return super().execute(query)


def now():
    return datetime.now(timezone.utc).isoformat()


def uid(prefix=""):
    return prefix + uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(dumps(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _protect(raw, decrypt=False):
    if os.name != "nt":
        raise RuntimeError("WINDOWS_REQUIRED: 이 패키지의 로컬 DB는 Windows용입니다.")
    class Blob(ctypes.Structure):
        _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(raw)
    incoming = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = Blob()
    function = ctypes.windll.crypt32.CryptUnprotectData if decrypt else ctypes.windll.crypt32.CryptProtectData
    if not function(ctypes.byref(incoming), None, None, None, None, 1, ctypes.byref(outgoing)):
        raise RuntimeError("CREDENTIAL_PROTECTION_FAILED")
    try:
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.windll.kernel32.LocalFree(outgoing.data)


class StorageConflict(RuntimeError):
    def __init__(self, current):
        self.current = current
        super().__init__("STORAGE_CONFLICT: 다른 작업이 먼저 저장했습니다. 최신 내용을 확인해 주세요.")


class MariaServer:
    def __init__(self, root, resources):
        self.root = Path(root).resolve()
        explicit = os.environ.get("PILOT_MARIADB_ROOT")
        self.runtime = Path(explicit).resolve() if explicit else Path(resources).resolve() / "runtime" / "mariadb"
        self.process = self.lock = self.log = None
        self.config = None
        self.packet_limit = MAX_PACKET_BYTES

    def _persist(self):
        atomic_json(self.root / "config" / "mariadb.protected.json", {
            "format": "windows-dpapi/1", "payload": base64.b64encode(_protect(dumps(self.config).encode())).decode()
        })

    def start(self):
        if self.process is not None:
            if self.process.poll() is not None:
                raise RuntimeError("MARIADB_PROCESS_EXITED")
            return
        executable = self.runtime / "bin" / "mariadbd.exe"
        initializer = self.runtime / "bin" / "mariadb-install-db.exe"
        if not executable.is_file() or not initializer.is_file():
            raise RuntimeError("MARIADB_RUNTIME_MISSING: 설치 패키지의 MariaDB 구성 요소가 필요합니다.")
        import msvcrt
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / ".storage.lock").open("a+b")
        if os.fstat(self.lock.fileno()).st_size == 0:
            self.lock.write(b"0")
            self.lock.flush()
        self.lock.seek(0)
        try:
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            self.lock.close()
            self.lock = None
            raise RuntimeError("STORAGE_LOCKED: 이 저장소를 사용하는 프로그램이 이미 실행 중입니다.")
        try:
            config_path = self.root / "config" / "mariadb.protected.json"
            dbdir = self.root / "database"
            fresh = not config_path.exists()
            if fresh:
                if dbdir.exists() and any(dbdir.iterdir()):
                    raise RuntimeError("DB_CONFIG_MISSING: 기존 DB를 보존했습니다. 연결 설정을 복구해 주세요.")
                self.config = {"database": "pilot_studio_v2", "host": "127.0.0.1", "user": "pilot_app", "password": secrets.token_hex(32), "root_password": secrets.token_hex(32), "binding_id": uid("storage-")}
            else:
                protected = json.loads(config_path.read_text(encoding="utf-8"))
                if protected.get("format") != "windows-dpapi/1":
                    raise RuntimeError("DB_CONFIG_UNSUPPORTED")
                self.config = json.loads(_protect(base64.b64decode(protected["payload"]), True))
            # The storage lock establishes ownership. Only exact datadir server
            # processes can be recovered, never another local MariaDB service.
            import psutil
            for process in psutil.process_iter(["exe", "cmdline"]):
                try:
                    if not process.info["exe"] or Path(process.info["exe"]).resolve() != executable.resolve():
                        continue
                    targets = [arg.split("=", 1)[1] for arg in (process.info["cmdline"] or []) if arg.lower().startswith("--datadir=")]
                    if len(targets) != 1 or Path(targets[0]).resolve() != dbdir.resolve():
                        continue
                    try:
                        connection = pymysql.connect(host="127.0.0.1", port=self.config.get("port", 0), user="root", password=self.config["root_password"], connect_timeout=2)
                        with connection.cursor() as cursor:
                            cursor.execute("SHUTDOWN")
                        connection.close()
                    except Exception:
                        pass
                    try:
                        process.wait(5)
                    except psutil.TimeoutExpired:
                        process.terminate()
                        process.wait(10)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
            with socket.socket() as reservation:
                reservation.bind(("127.0.0.1", 0))
                self.config["port"] = reservation.getsockname()[1]
            logs = self.root / "logs"
            logs.mkdir(exist_ok=True)
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            if fresh:
                result = subprocess.run([str(initializer), "--datadir=" + str(dbdir), "--port=" + str(self.config["port"]), "--silent"], capture_output=True, timeout=120, creationflags=flags)
                if result.returncode:
                    (logs / "database-initialize.log").write_bytes(result.stdout + result.stderr)
                    raise RuntimeError("MARIADB_INITIALIZE_FAILED")
                self.config["bootstrap_pending"] = True
            bootstrap = self.root / "config" / "bootstrap.sql"
            if self.config.get("bootstrap_pending"):
                bootstrap.parent.mkdir(parents=True, exist_ok=True)
                sql = [f"ALTER USER 'root'@'localhost' IDENTIFIED BY '{self.config['root_password']}';", "CREATE DATABASE IF NOT EXISTS pilot_studio_v2 CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"]
                for host in ("localhost", "127.0.0.1"):
                    sql += [f"CREATE USER IF NOT EXISTS 'pilot_app'@'{host}' IDENTIFIED BY '{self.config['password']}';", f"GRANT ALL PRIVILEGES ON pilot_studio_v2.* TO 'pilot_app'@'{host}';"]
                bootstrap.write_text("\n".join(sql), encoding="utf-8")
            self._persist()
            command = [str(executable), "--no-defaults", "--basedir=" + str(self.runtime), "--datadir=" + str(dbdir), "--bind-address=127.0.0.1", "--port=" + str(self.config["port"]), "--skip-name-resolve", "--max-connections=40", "--max-allowed-packet=" + str(MAX_PACKET_BYTES), "--innodb-buffer-pool-size=64M", "--log-error=" + str(logs / "mariadb-error.log")]
            if bootstrap.exists():
                command.append("--init-file=" + str(bootstrap))
            self.log = (logs / "mariadb-process.log").open("ab")
            self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT, creationflags=flags)
            deadline = time.monotonic() + 60
            last_code = None
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError("MARIADB_START_FAILED: 데이터베이스 진단 로그를 확인해 주세요.")
                try:
                    connection = pymysql.connect(**self.options(), connect_timeout=1)
                    with connection.cursor() as cursor:
                        cursor.execute('SELECT @@max_allowed_packet')
                        self.packet_limit = int(cursor.fetchone()[0])
                    connection.close()
                    self.config.pop("bootstrap_pending", None)
                    self._persist()
                    bootstrap.unlink(missing_ok=True)
                    return
                except pymysql.Error as error:
                    last_code = error.args[0] if error.args else None
                    time.sleep(0.15)
            raise RuntimeError(f"MARIADB_START_TIMEOUT: 로컬 DB 연결을 확인할 수 없습니다. 연결 오류 코드 {last_code}.")
        except BaseException:
            (self.root / "config" / "bootstrap.sql").unlink(missing_ok=True)
            self.close()
            raise

    def options(self):
        return {key: self.config[key] for key in ("host", "port", "database", "user", "password")}

    def close(self):
        if self.process is not None and self.process.poll() is None:
            try:
                connection = pymysql.connect(host="127.0.0.1", port=self.config["port"], user="root", password=self.config["root_password"], connect_timeout=2)
                with connection.cursor() as cursor:
                    cursor.execute("SHUTDOWN")
                connection.close()
            except Exception:
                self.process.terminate()
            try:
                self.process.wait(20)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(5)
        if self.log:
            self.log.close()
        if self.lock:
            import msvcrt
            self.lock.seek(0)
            msvcrt.locking(self.lock.fileno(), msvcrt.LK_UNLCK, 1)
            self.lock.close()
        self.process = self.log = self.lock = None


class Storage:
    def __init__(self, data_dir, resource_dir=None):
        self.data_dir = Path(data_dir).resolve()
        self.resource_dir = Path(resource_dir or os.environ.get("PILOT_RESOURCE_DIR", Path(__file__).resolve().parent.parent / "packaging" / "resources")).resolve()
        self.server = MariaServer(self.data_dir, self.resource_dir)
        self.lock = threading.RLock()
        self.initialized = False
        atexit.register(self.close)

    @contextmanager
    def connect(self):
        connection = None
        try:
            connection = pymysql.connect(**self.server.options(), charset="utf8mb4", cursorclass=PacketBoundedCursor,
                autocommit=False, connect_timeout=5, max_allowed_packet=MAX_PACKET_BYTES)
            connection.pilot_packet_limit = self.server.packet_limit
            yield connection
            connection.commit()
        except BaseException as original:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception as cleanup:
                    original.add_note('DB rollback also failed: ' + type(cleanup).__name__)
            if isinstance(original, pymysql.Error):
                errno = original.args[0] if original.args else None
                raise StorageError('STORAGE_CONNECTION_FAILED',
                    'DB 저장 또는 연결에 실패했습니다. 저장 상태와 진단 로그의 최초 오류를 확인해 주세요.',
                    {'database_error_code': errno, 'exception_type': type(original).__name__}) from original
            raise
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass  # Closing must never mask the original query/commit failure.

    def init(self):
        with self.lock:
            if self.initialized:
                return self
            self.server.start()
            schema = [
                "CREATE TABLE IF NOT EXISTS schema_migration (version INT PRIMARY KEY, checksum CHAR(64) NOT NULL, applied_at VARCHAR(40) NOT NULL)",
                "CREATE TABLE IF NOT EXISTS storage_binding (id VARCHAR(64) PRIMARY KEY, schema_version INT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS project (id VARCHAR(64) PRIMARY KEY, row_version BIGINT NOT NULL, body LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL)",
                "CREATE TABLE IF NOT EXISTS project_revision (id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL, row_version BIGINT NOT NULL, body LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, UNIQUE(project_id,row_version), FOREIGN KEY(project_id) REFERENCES project(id))",
                "CREATE TABLE IF NOT EXISTS change_event (id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NULL, kind VARCHAR(80) NOT NULL, body LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, INDEX(project_id), FOREIGN KEY(project_id) REFERENCES project(id))",
                "CREATE TABLE IF NOT EXISTS job_run (id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NULL, body LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL, INDEX(project_id), FOREIGN KEY(project_id) REFERENCES project(id))",
                "CREATE TABLE IF NOT EXISTS confirmation_snapshot (id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NOT NULL, body LONGTEXT NOT NULL, hash CHAR(64) NOT NULL, created_at VARCHAR(40) NOT NULL, INDEX(project_id), FOREIGN KEY(project_id) REFERENCES project(id))",
                "CREATE TABLE IF NOT EXISTS file_asset (id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64) NULL, body LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, INDEX(project_id), FOREIGN KEY(project_id) REFERENCES project(id))",
                "CREATE TABLE IF NOT EXISTS app_setting (id VARCHAR(120) PRIMARY KEY, body LONGTEXT NOT NULL, updated_at VARCHAR(40) NOT NULL)",
                "CREATE TABLE IF NOT EXISTS backup_record (id VARCHAR(64) PRIMARY KEY, body LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL)",
            ]
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute(schema[0])
                cursor.execute("SELECT * FROM schema_migration ORDER BY version")
                existing = cursor.fetchall()
                if existing and max(row["version"] for row in existing) > SCHEMA_VERSION:
                    raise RuntimeError("DB_SCHEMA_TOO_NEW: 이 프로그램은 저장소 버전을 지원하지 않습니다.")
                checksum = fingerprint(schema)
                if existing and existing[0]["checksum"] != checksum:
                    raise RuntimeError("DB_MIGRATION_CHECKSUM_MISMATCH")
                for statement in schema[1:]:
                    cursor.execute(statement)
                cursor.execute("SELECT * FROM storage_binding")
                binding = cursor.fetchone()
                if binding and binding["id"] != self.server.config["binding_id"]:
                    raise RuntimeError("STORAGE_BINDING_MISMATCH")
                cursor.execute("INSERT IGNORE INTO storage_binding VALUES (%s,%s)", (self.server.config["binding_id"], SCHEMA_VERSION))
                cursor.execute("INSERT IGNORE INTO schema_migration VALUES (%s,%s,%s)", (SCHEMA_VERSION, checksum, now()))
            self.initialized = True
            (self.data_dir / "assets").mkdir(exist_ok=True)
            return self

    def close(self):
        with self.lock:
            self.server.close()
            self.initialized = False

    def _project(self, cursor, project_id, locked=False):
        cursor.execute("SELECT body FROM project WHERE id=%s" + (" FOR UPDATE" if locked else ""), (project_id,))
        row = cursor.fetchone()
        return json.loads(row["body"]) if row else None

    def create_project(self, project):
        result = json.loads(dumps(project))
        result.setdefault("id", uid("project-"))
        result.setdefault("created_at", now())
        result.update(row_version=1, updated_at=now())
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO project VALUES(%s,%s,%s,%s,%s)", (result["id"], 1, dumps(result), result["created_at"], result["updated_at"]))
            cursor.execute("INSERT INTO project_revision VALUES(%s,%s,%s,%s,%s)", (uid("revision-"), result["id"], 1, dumps(result), now()))
        return result

    def list_projects(self):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT body FROM project ORDER BY updated_at DESC")
            return [json.loads(row["body"]) for row in cursor.fetchall()]

    def get_project(self, project_id):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            return self._project(cursor, project_id)

    def save_project(self, project, expected_revision, event=None):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            current = self._project(cursor, project["id"], True)
            if current is None:
                raise KeyError(project["id"])
            if current["row_version"] != expected_revision:
                raise StorageConflict(current)
            result = json.loads(dumps(project))
            result.update(row_version=current["row_version"] + 1, created_at=current["created_at"], updated_at=now())
            cursor.execute("UPDATE project SET row_version=%s,body=%s,updated_at=%s WHERE id=%s", (result["row_version"], dumps(result), result["updated_at"], result["id"]))
            cursor.execute("INSERT INTO project_revision VALUES(%s,%s,%s,%s,%s)", (uid("revision-"), result["id"], result["row_version"], dumps(result), now()))
            if event:
                body = event if isinstance(event, dict) else {"kind": str(event)}
                self._event(cursor, result["id"], body.get("kind", "project_saved"), body)
            return result

    def _event(self, cursor, project_id, kind, data):
        result = {"id": uid("event-"), "project_id": project_id, "kind": kind, "data": data, "created_at": now()}
        cursor.execute("INSERT INTO change_event VALUES(%s,%s,%s,%s,%s)", (result["id"], project_id, kind, dumps(result), result["created_at"]))
        return result

    def append_event(self, project_id, kind, data):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            return self._event(cursor, project_id, kind, data)

    record_event = append_event

    def events(self, project_id=None):
        return self._list_bodies("change_event", project_id)

    def _list_bodies(self, table, project_id=None):
        if table not in TABLES:
            raise ValueError("INVALID_TABLE")
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT body FROM `" + table + "`" + (" WHERE project_id=%s" if project_id else "") + " ORDER BY created_at DESC", (project_id,) if project_id else ())
            return [json.loads(row["body"]) for row in cursor.fetchall()]

    def create_job(self, job):
        result = json.loads(dumps(job))
        result.setdefault("id", uid("job-"))
        result.setdefault("created_at", now())
        result["updated_at"] = now()
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO job_run VALUES(%s,%s,%s,%s,%s)", (result["id"], result.get("project_id"), dumps(result), result["created_at"], result["updated_at"]))
        return result

    def save_job(self, job):
        result = json.loads(dumps(job))
        result["updated_at"] = now()
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE job_run SET body=%s,updated_at=%s WHERE id=%s", (dumps(result), result["updated_at"], result["id"]))
            if not cursor.rowcount:
                raise KeyError(result["id"])
        return result

    def get_job(self, job_id):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT body FROM job_run WHERE id=%s", (job_id,))
            row = cursor.fetchone()
            return json.loads(row["body"]) if row else None

    def list_jobs(self, project_id=None):
        return self._list_bodies("job_run", project_id)

    def add_snapshot(self, project_id, body, expected_revision=None):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            project = self._project(cursor, project_id, True)
            if project is None:
                raise KeyError(project_id)
            if expected_revision is not None and project["row_version"] != expected_revision:
                raise StorageConflict(project)
            result = json.loads(dumps(body))
            result.setdefault("id", uid("snapshot-"))
            result.update(project_id=project_id, created_at=now())
            digest = fingerprint(result)
            cursor.execute("INSERT INTO confirmation_snapshot VALUES(%s,%s,%s,%s,%s)", (result["id"], project_id, dumps(result), digest, result["created_at"]))
            self._event(cursor, project_id, "snapshot_created", {"snapshot_id": result["id"], "hash": digest, "row_version": project["row_version"]})
            return result

    def snapshots(self, project_id=None):
        return self._list_bodies("confirmation_snapshot", project_id)

    def get_snapshot(self, snapshot_id):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT body,hash FROM confirmation_snapshot WHERE id=%s", (snapshot_id,))
            row = cursor.fetchone()
            if not row:
                return None
            result = json.loads(row["body"])
            if fingerprint(result) != row["hash"]:
                raise RuntimeError("SNAPSHOT_INTEGRITY_FAILED")
            return result

    def get_setting(self, key, default=None):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT body FROM app_setting WHERE id=%s", (key,))
            row = cursor.fetchone()
            return json.loads(row["body"]) if row else default

    def set_setting(self, key, value):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("INSERT INTO app_setting VALUES(%s,%s,%s) ON DUPLICATE KEY UPDATE body=VALUES(body),updated_at=VALUES(updated_at)", (key, dumps(value), now()))
        return value

    def register_file(self, source, role, project_id=None):
        source = Path(source).resolve()
        with self.lock:
            digest = file_hash(source)
            relative = PurePosixPath("assets") / digest[:2] / digest
            destination = self.data_dir / str(relative)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if not destination.exists():
                temporary = destination.with_suffix("." + uuid.uuid4().hex + ".partial")
                with source.open("rb") as incoming, temporary.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                if file_hash(temporary) != digest:
                    raise RuntimeError("SOURCE_FILE_CHANGED")
                os.replace(temporary, destination)
            elif file_hash(destination) != digest:
                raise RuntimeError("MANAGED_FILE_CORRUPT")
            result = {"id": uid("file-"), "project_id": project_id, "name": source.name, "role": role, "relative_path": relative.as_posix(), "sha256": digest, "size": destination.stat().st_size, "state": "READY", "created_at": now()}
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO file_asset VALUES(%s,%s,%s,%s)", (result["id"], project_id, dumps(result), result["created_at"]))
            return result

    def get_file(self, file_id):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT body FROM file_asset WHERE id=%s", (file_id,))
            row = cursor.fetchone()
            return json.loads(row["body"]) if row else None

    def file_path(self, file_id, verify=False):
        record = self.get_file(file_id)
        if record is None:
            raise KeyError(file_id)
        path = (self.data_dir / record["relative_path"]).resolve()
        if not path.is_relative_to(self.data_dir / "assets") or not path.is_file():
            raise RuntimeError("FILE_ASSET_MISSING")
        if verify and file_hash(path) != record["sha256"]:
            raise RuntimeError("FILE_ASSET_CORRUPT")
        return path

    def status(self):
        with self.lock, self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT VERSION() AS version")
            version = cursor.fetchone()["version"]
        return {"backend": "mariadb", "status": "connected", "schema_version": SCHEMA_VERSION, "server_version": version, "binding_id": self.server.config["binding_id"], "credentials": "windows_user_dpapi", "free_bytes": shutil.disk_usage(self.data_dir).free}

    def backup(self):
        # Holding this lock includes file publication and every registered DB
        # mutation through completion of archive verification.
        with self.lock:
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
                cursor.execute("SHOW TABLES")
                tables = sorted(next(iter(row.values())) for row in cursor.fetchall())
                snapshot = {}
                for table in tables:
                    if not table.replace("_", "").isalnum():
                        raise RuntimeError("INVALID_SCHEMA_TABLE")
                    cursor.execute("SELECT * FROM `" + table + "`")
                    snapshot[table] = cursor.fetchall()
            record = {"id": uid("backup-"), "created_at": now(), "format": "pilot-backup/3", "schema_version": SCHEMA_VERSION}
            folder = self.data_dir / "backups"
            folder.mkdir(exist_ok=True)
            temporary = folder / (record["id"] + ".partial")
            archive_path = folder / (record["id"] + ".zip")
            manifest = dict(record, table_counts={table: len(rows) for table, rows in snapshot.items()}, files=[])
            with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
                database = dumps(snapshot).encode("utf-8")
                archive.writestr("database.json", database)
                manifest["files"].append({"path": "database.json", "sha256": hashlib.sha256(database).hexdigest(), "size": len(database)})
                registered = {json.loads(row["body"])["relative_path"]: json.loads(row["body"]) for row in snapshot["file_asset"]}
                for relative, asset in registered.items():
                    source = (self.data_dir / relative).resolve()
                    if not source.is_relative_to(self.data_dir / "assets") or not source.is_file() or file_hash(source) != asset["sha256"]:
                        raise RuntimeError("BACKUP_FILE_CORRUPT")
                    archive.write(source, relative)
                    manifest["files"].append({"path": relative, "sha256": asset["sha256"], "size": source.stat().st_size})
                archive.writestr("manifest.json", dumps(manifest))
            self.validate_backup(temporary)
            os.replace(temporary, archive_path)
            record.update(size=archive_path.stat().st_size, sha256=file_hash(archive_path), file_count=len(manifest["files"]) - 1, table_counts=manifest["table_counts"], relative_path="backups/" + archive_path.name)
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("INSERT INTO backup_record VALUES(%s,%s,%s)", (record["id"], dumps(record), record["created_at"]))
            return dict(record, path=str(archive_path), available=True)

    @staticmethod
    def validate_backup(path):
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = [info.filename for info in infos]
            if len(names) > 200000 or len(names) != len(set(names)) or sum(info.file_size for info in infos) > 64 * 1024**3:
                raise ValueError("BACKUP_RESOURCE_LIMIT_OR_DUPLICATES")
            for info in infos:
                parts = PurePosixPath(info.filename).parts
                if not parts or info.filename.startswith(("/", "\\")) or "\\" in info.filename or ":" in info.filename or ".." in parts or (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("BACKUP_UNSAFE_PATH")
            if archive.getinfo("manifest.json").file_size > 16 * 1024**2:
                raise ValueError("BACKUP_MANIFEST_LIMIT")
            manifest = json.loads(archive.read("manifest.json"))
            if manifest.get("format") != "pilot-backup/3" or manifest.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("BACKUP_VERSION_UNSUPPORTED")
            expected = {entry["path"] for entry in manifest["files"]} | {"manifest.json"}
            if expected != set(names) or "database.json" not in expected:
                raise ValueError("BACKUP_FILE_COVERAGE")
            for entry in manifest["files"]:
                if entry["path"] != "database.json" and not entry["path"].startswith("assets/"):
                    raise ValueError("BACKUP_UNEXPECTED_PATH")
                digest = hashlib.sha256()
                size = 0
                with archive.open(entry["path"]) as source:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        size += len(chunk)
                        digest.update(chunk)
                if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise ValueError("BACKUP_HASH_MISMATCH")
            rows = json.loads(archive.read("database.json"))
            if set(rows) != set(TABLES) | {"schema_migration", "storage_binding"}:
                raise ValueError("BACKUP_SCHEMA_TABLES")
            for table, entries in rows.items():
                if len(entries) != manifest["table_counts"].get(table):
                    raise ValueError("BACKUP_ROW_COUNT")
            for row in rows["confirmation_snapshot"]:
                if fingerprint(json.loads(row["body"])) != row["hash"]:
                    raise ValueError("SNAPSHOT_INTEGRITY_FAILED")
            return manifest

    def restore(self, archive_path, destination):
        manifest = self.validate_backup(archive_path)
        destination = Path(destination).resolve()
        if destination.exists():
            raise ValueError("RESTORE_REQUIRES_NEW_DIRECTORY")
        destination.mkdir(parents=True)
        with zipfile.ZipFile(archive_path) as archive:
            rows = json.loads(archive.read("database.json"))
            for entry in manifest["files"]:
                if entry["path"].startswith("assets/"):
                    archive.extract(entry["path"], destination)
        restored = Storage(destination, self.resource_dir)
        try:
            restored.init()
            with restored.connect() as connection, connection.cursor() as cursor:
                for table in TABLES:
                    for row in rows[table]:
                        keys = list(row)
                        if any(not key.replace("_", "").isalnum() for key in keys):
                            raise ValueError("BACKUP_COLUMN_INVALID")
                        cursor.execute("INSERT INTO `" + table + "` (" + ",".join("`" + key + "`" for key in keys) + ") VALUES (" + ",".join(["%s"] * len(keys)) + ")", [row[key] for key in keys])
                for table in TABLES:
                    cursor.execute("SELECT * FROM `" + table + "`")
                    actual = sorted(cursor.fetchall(), key=lambda row: row["id"])
                    if fingerprint(actual) != fingerprint(sorted(rows[table], key=lambda row: row["id"])):
                        raise ValueError("RESTORE_ROW_HASH_MISMATCH")
            result = {"status": "verified", "activated": False, "id": destination.name, "data_dir": str(destination), "table_counts": manifest["table_counts"], "created_at": now()}
            atomic_json(destination / "restore-result.json", result)
            return result
        finally:
            restored.close()

    def list_backups(self):
        result = []
        for record in self._list_bodies("backup_record"):
            # Historic backup metadata may survive restoration; archive bytes
            # are deliberately not recursively embedded in later backups.
            filename = str(record["id"]) + ".zip"
            if Path(filename).name != filename or "/" in filename or "\\" in filename:
                raise RuntimeError("BACKUP_RECORD_INVALID")
            path = self.data_dir / "backups" / filename
            result.append(dict(record, path=str(path), available=path.is_file()))
        return result

    # Concise aliases for callers that treat Storage as a repository.
    create = create_project
    get = get_project
    save = save_project
    list = list_projects
