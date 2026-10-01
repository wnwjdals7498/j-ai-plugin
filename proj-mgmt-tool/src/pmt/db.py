"""SQLite storage foundation and atomic request boundary."""
import json
import logging
import os
import tempfile
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path

from .diagnostics import DiagnosticLogger
from .errors import PmtError
from .paths import resolve_roots
from .util import canonical_json, fingerprint, new_id, utc_now
from .phase2_schema import SCHEMA as PHASE2_SCHEMA, SCHEMA_VERSION as PHASE2_SCHEMA_VERSION

SCHEMA_VERSION = PHASE2_SCHEMA_VERSION
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scopes (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('environment','repository','project','classification')),
 parent_id TEXT REFERENCES scopes(id), slug TEXT NOT NULL, body_json TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS scopes_parent_idx ON scopes(parent_id, kind);
CREATE TABLE IF NOT EXISTS scope_paths (
 scope_id TEXT NOT NULL REFERENCES scopes(id) ON DELETE CASCADE, environment_id TEXT NOT NULL,
 path TEXT NOT NULL, PRIMARY KEY(scope_id, environment_id));
CREATE TABLE IF NOT EXISTS records (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, scope_id TEXT NOT NULL REFERENCES scopes(id),
 parent_id TEXT REFERENCES records(id), title TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'Planned',
 body_json TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 1 CHECK(revision >= 1),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS records_scope_idx ON records(scope_id, parent_id, kind);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, record_id TEXT REFERENCES records(id), scope_id TEXT REFERENCES scopes(id),
 actor TEXT, event_type TEXT NOT NULL, reason TEXT, old_revision INTEGER, new_revision INTEGER,
 payload_json TEXT NOT NULL DEFAULT '{}', occurred_at TEXT, recorded_at TEXT NOT NULL,
 correlation_id TEXT, causation_id TEXT);
CREATE TABLE IF NOT EXISTS requests (
 request_id TEXT PRIMARY KEY, fingerprint_version INTEGER NOT NULL, request_fingerprint TEXT NOT NULL,
 response_json TEXT NOT NULL, exit_code INTEGER NOT NULL, deterministic INTEGER NOT NULL,
 actor TEXT, session_id TEXT,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claims (
 record_id TEXT PRIMARY KEY REFERENCES records(id), owner_session TEXT NOT NULL, token_hash TEXT NOT NULL,
 claimed_at TEXT NOT NULL, heartbeat_at TEXT NOT NULL, claim_event_id TEXT REFERENCES events(id));
CREATE TABLE IF NOT EXISTS artifacts (
 id TEXT PRIMARY KEY, scope_id TEXT REFERENCES scopes(id), sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL CHECK(size_bytes >= 0),
 relative_path TEXT NOT NULL, state TEXT NOT NULL, retention_until TEXT, created_at TEXT NOT NULL,
 UNIQUE(scope_id, sha256, relative_path));
CREATE TABLE IF NOT EXISTS artifact_refs (
 artifact_id TEXT NOT NULL REFERENCES artifacts(id), owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
 purpose TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(artifact_id, owner_type, owner_id, purpose));
CREATE TABLE IF NOT EXISTS verifications (
 id TEXT PRIMARY KEY, definition_id TEXT NOT NULL, definition_version TEXT NOT NULL, target_id TEXT NOT NULL,
 environment_id TEXT NOT NULL, input_fingerprint TEXT NOT NULL, outcome TEXT NOT NULL,
 exit_code INTEGER, evidence_json TEXT NOT NULL DEFAULT '[]', includes_json TEXT NOT NULL DEFAULT '[]',
 command_json TEXT NOT NULL DEFAULT '{}', started_at TEXT, completed_at TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'valid');
CREATE INDEX IF NOT EXISTS verifications_lookup_idx ON verifications(definition_id, target_id, input_fingerprint);
CREATE TABLE IF NOT EXISTS file_jobs (
 id TEXT PRIMARY KEY, operation TEXT NOT NULL, owner TEXT NOT NULL, state TEXT NOT NULL,
 source_alias TEXT, relative_path TEXT, sha256 TEXT, size_bytes INTEGER, started_at TEXT NOT NULL,
 updated_at TEXT NOT NULL, error_code TEXT);
"""

class Database:
    def __init__(self, root=None, config_root=None, busy_timeout_ms=5000, logger=None):
        self.root, self.config_root = resolve_roots(root, config_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.config_root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "pmt.sqlite3"
        self.busy_timeout_ms = int(busy_timeout_ms)
        self.diagnostics = logger if isinstance(logger, DiagnosticLogger) else DiagnosticLogger(logger)
        self.environment_id = self._load_profile_id()
        self._initialize()
        try:
            from .operations import configure_logger
            configure_logger(self)
        except (OSError, PmtError, sqlite3.Error):
            self.diagnostics.sink_unavailable = True
            self.diagnostics.emit("diagnostic_log_unavailable", level=logging.WARNING)

    def _load_profile_id(self):
        path = self.config_root / "profile.json"
        import uuid
        def read_existing():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                candidate = value["environment_id"]
                if str(uuid.UUID(candidate)) != candidate:
                    raise ValueError("noncanonical UUID")
                return candidate
            except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
                raise PmtError("profile_config_invalid", "Existing profile configuration is invalid; it was preserved", 4, False) from exc
        if path.exists():
            return read_existing()
        candidate = new_id()
        fd, tmp_name = tempfile.mkstemp(prefix="profile-", suffix=".tmp", dir=self.config_root)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(canonical_json({"environment_id": candidate}))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                return read_existing()
        except Exception:
            raise
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        return candidate

    def connect(self):
        try:
            conn = sqlite3.connect(str(self.path), timeout=self.busy_timeout_ms / 1000,
                                   isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            conn.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            conn.execute("PRAGMA synchronous = FULL")
            return conn
        except sqlite3.OperationalError as exc:
            raise self._sqlite_error(exc) from exc

    def _initialize(self):
        started = time.monotonic()
        self.diagnostics.emit("database_init_started", data_root_alias="<data>")
        try:
            with self.connect() as conn:
                version_row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() if self._table_exists(conn, "meta") else None
                version = int(version_row[0]) if version_row else 0
                if version > SCHEMA_VERSION:
                    raise PmtError("schema_version_unsupported", "Database schema is newer than this runtime", 2, False,
                                   {"schema_version": version, "supported": SCHEMA_VERSION})
                if version not in (0, 1, 2, SCHEMA_VERSION):
                    raise PmtError("schema_migration_unsupported", "No migration is available for this schema version", 2, False,
                                   {"schema_version": version})
                if version == SCHEMA_VERSION:
                    if not self._meta_value(conn, "db_id"):
                        raise PmtError("database_metadata_invalid", "Database identity is missing", 2)
                    # A normal invocation is read-only at startup, including while another
                    # process holds the maintenance barrier for backup.
                    return
                conn.execute("PRAGMA journal_mode = WAL")
                backup_path = None
                if version == 2:
                    self.diagnostics.emit("schema_migration_started", schema_version=3,
                                          transaction_outcome="started")
                    owner = "schema3-" + new_id()
                    conn.execute("BEGIN IMMEDIATE")
                    current_owner = self._meta_value(conn, "maintenance_owner") or ""
                    if current_owner:
                        conn.rollback()
                        raise PmtError("maintenance_active", "Database is in maintenance", 4, True)
                    self._put_meta(conn, "maintenance_owner", owner)
                    conn.commit()
                    backup_path = self.root / ("pmt-schema2-" + new_id() + ".sqlite3")
                    try:
                        with self.connect() as source, sqlite3.connect(str(backup_path)) as target:
                            source.backup(target)
                            target.execute("UPDATE meta SET value='' WHERE key='maintenance_owner'")
                            target.commit()
                        conn.execute("BEGIN IMMEDIATE")
                        if self._meta_value(conn, "maintenance_owner") != owner:
                            raise PmtError("maintenance_lost", "Schema migration maintenance gate was lost", 4, True)
                        for statement in PHASE2_SCHEMA.split(";"):
                            if statement.strip():
                                conn.execute(statement)
                        self._put_meta(conn, "schema_version", "3")
                        self._put_meta(conn, "maintenance_owner", "")
                        conn.commit()
                        self.diagnostics.emit("schema_migration_completed", schema_version=3,
                                              transaction_outcome="commit")
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        try:
                            with conn:
                                self._put_meta(conn, "maintenance_owner", "")
                        except sqlite3.Error:
                            pass
                        self.diagnostics.emit("schema_migration_failed", level=logging.ERROR,
                                              schema_version=3, transaction_outcome="rollback")
                        raise
                else:
                    conn.execute("BEGIN IMMEDIATE")
                    for statement in SCHEMA.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    if version == 1:
                        columns = {row[1] for row in conn.execute("PRAGMA table_info(requests)")}
                        if "actor" not in columns:
                            conn.execute("ALTER TABLE requests ADD COLUMN actor TEXT")
                        if "session_id" not in columns:
                            conn.execute("ALTER TABLE requests ADD COLUMN session_id TEXT")
                    for statement in PHASE2_SCHEMA.split(";"):
                        if statement.strip():
                            conn.execute(statement)
                    self._put_meta(conn, "schema_version", str(SCHEMA_VERSION))
                    self._put_meta(conn, "db_id", self._meta_value(conn, "db_id") or new_id())
                    self._put_meta(conn, "environment_id", self.environment_id)
                    self._put_meta(conn, "maintenance_owner", self._meta_value(conn, "maintenance_owner") or "")
                    conn.commit()
            self.diagnostics.emit("database_init_succeeded", duration_ms=int((time.monotonic()-started)*1000),
                                  schema_version=SCHEMA_VERSION)
        except Exception as exc:
            self.diagnostics.emit("database_init_failed", level=logging.ERROR, error_code=getattr(exc, "code", "database_init"),
                                  duration_ms=int((time.monotonic()-started)*1000), transaction_outcome="rollback")
            message = str(exc).lower()
            retries = getattr(self, "_init_retries", 0)
            if ("locked" in message or "busy" in message) and retries < 8:
                self._init_retries = retries + 1
                time.sleep(min(0.025 * (2 ** retries), 0.5))
                return self._initialize()
            raise

    @staticmethod
    def _table_exists(conn, name):
        return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None

    @staticmethod
    def _meta_value(conn, key):
        if not Database._table_exists(conn, "meta"):
            return None
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _put_meta(conn, key, value):
        conn.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    @staticmethod
    def _sqlite_error(exc):
        busy = "locked" in str(exc).lower() or "busy" in str(exc).lower()
        return PmtError("database_busy" if busy else "database_error",
                        "Database is temporarily busy" if busy else "Database operation failed",
                        4 if busy else 4, True)

    @contextmanager
    def write(self, maintenance_owner=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()
            owner = row[0] if row else ""
            if owner and owner != maintenance_owner:
                raise PmtError("maintenance_active", "Database is in maintenance", 4, True)
            yield conn
            conn.commit()
        except sqlite3.OperationalError as exc:
            if conn.in_transaction:
                conn.rollback()
            raise self._sqlite_error(exc) from exc
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def run_request(self, request, handler, maintenance_owner=None):
        request_id = request.get("request_id") if isinstance(request, dict) else None
        started = time.monotonic()
        try:
            request = self._normalize_request(request)
            request_id = request.get("request_id")
            diagnostic_context = {key: request.get(key) for key in
                                  ("operation", "session_id", "scope_id", "record_id", "correlation_id")}
            diagnostic_context["source"] = {key: value for key, value in request.get("source", {}).items()
                                            if key in {"product", "product_version", "adapter_version", "instance_id"}}
            diagnostic_context["event_id"] = (request.get("normalized_event") or {}).get("event_id")
            import uuid
            try:
                if str(uuid.UUID(request_id)) != request_id:
                    raise ValueError("noncanonical")
            except (ValueError, TypeError, AttributeError):
                raise PmtError("invalid_request_id", "request_id must be a canonical UUID")
            ignored = {"request_id", "correlation_id", "received_at", "received_at_utc", "retry_count", "attempt"}
            semantic = {k: v for k, v in request.items() if k not in ignored}
            req_fp = fingerprint(semantic)
            with self.write(maintenance_owner) as conn:
                old = conn.execute("SELECT request_fingerprint,response_json,exit_code,actor,session_id FROM requests WHERE request_id=?", (request_id,)).fetchone()
                if old:
                    if old[3] != request.get("actor") or old[4] != request.get("session_id"):
                        raise PmtError("request_owner_mismatch", "request result belongs to a different actor or session", 3, False)
                    if old[0] != req_fp:
                        raise PmtError("request_conflict", "request_id was already used for a different request", 3, False)
                    response = json.loads(old[1])
                    self.diagnostics.emit("request_replay", request_id=request_id,
                                          **diagnostic_context, outcome="replayed", exit_code=old[2])
                    return response, old[2]
                conn.execute("SAVEPOINT request_handler")
                try:
                    result = handler(conn, request)
                    conn.execute("RELEASE SAVEPOINT request_handler")
                    response = self._response(request_id, True, result, None, [])
                    exit_code, deterministic = 0, 1
                except PmtError as err:
                    conn.execute("ROLLBACK TO SAVEPOINT request_handler")
                    conn.execute("RELEASE SAVEPOINT request_handler")
                    response = self._response(request_id, False, None, err.as_dict(), [])
                    exit_code, deterministic = err.exit_code, int(not err.retryable and err.exit_code in (2, 3))
                    if not deterministic:
                        raise
                conn.execute("INSERT INTO requests(request_id,fingerprint_version,request_fingerprint,response_json,exit_code,deterministic,created_at,actor,session_id) VALUES(?,?,?,?,?,?,?,?,?)",
                             (request_id, 1, req_fp, canonical_json(response), exit_code, deterministic, utc_now(), request.get("actor"), request.get("session_id")))
            logged = self.diagnostics.emit("request_committed" if exit_code == 0 else "request_rejected", request_id=request_id,
                                  **diagnostic_context, outcome="success" if exit_code == 0 else "error",
                                  error_code=(response.get("error") or {}).get("code"), exit_code=exit_code,
                                  old_revision=request.get("expected_revision"),
                                  new_revision=(response.get("result") or {}).get("revision"),
                                  ownership_result=(response.get("result") or {}).get("owner_session"),
                                  transaction_outcome="commit", duration_ms=int((time.monotonic()-started)*1000))
            if not logged:
                response["warnings"] = ["diagnostic_log_unavailable"]
                try:
                    sys.stderr.write(canonical_json({"at_utc": utc_now(), "level": "WARNING", "component": "pmt",
                                                     "event_name": "diagnostic_log_unavailable", "request_id": request_id}) + "\n")
                except Exception:
                    pass
                try:
                    with self.connect() as conn:
                        conn.execute("UPDATE requests SET response_json=? WHERE request_id=?", (canonical_json(response), request_id))
                except sqlite3.Error:
                    # The already committed business result remains successful even if warning persistence fails.
                    pass
            return response, exit_code
        except PmtError as err:
            response = self._response(request_id, False, None, err.as_dict(), [])
            self.diagnostics.emit("request_failed", level=logging.WARNING, request_id=request_id,
                                  operation=request.get("operation") if isinstance(request, dict) else None,
                                  outcome="error", error_code=err.code, retryable=err.retryable,
                                  exit_code=err.exit_code, transaction_outcome="rollback")
            return response, err.exit_code
        except sqlite3.Error as exc:
            err = self._sqlite_error(exc)
            self.diagnostics.emit("request_failed", level=logging.ERROR, request_id=request_id,
                                  operation=request.get("operation") if isinstance(request, dict) else None,
                                  outcome="error", error_code=err.code, retryable=err.retryable,
                                  exit_code=err.exit_code, transaction_outcome="rollback")
            return self._response(request_id, False, None, err.as_dict(), []), err.exit_code

    def _response(self, request_id, ok, result, error, warnings):
        return {"protocol_version": 1, "request_id": request_id, "ok": ok,
                "result": result, "error": error, "warnings": warnings}

    @staticmethod
    def _normalize_request(request):
        if not isinstance(request, dict):
            raise PmtError("invalid_request", "Request must be a JSON object")
        normalized = dict(request)
        normalized.setdefault("payload", {})
        normalized.setdefault("context_refs", [])
        normalized.setdefault("source", {"product": "cli"})
        if not isinstance(normalized["payload"], dict) or not isinstance(normalized["context_refs"], list) or not isinstance(normalized["source"], dict):
            raise PmtError("invalid_request", "payload, context_refs and source must be object, array and object")
        return normalized

    def get_request_result(self, request_id, actor=None, session_id=None):
        with self.connect() as conn:
            row = conn.execute("SELECT response_json,exit_code,actor,session_id FROM requests WHERE request_id=?", (request_id,)).fetchone()
        if row and ((actor is not None and actor != row[2]) or (session_id is not None and session_id != row[3])):
            return None
        return (json.loads(row[0]), row[1]) if row else None
