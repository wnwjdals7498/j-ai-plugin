"""Content-addressed resource storage and isolated SQLite backups."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import stat
import time
import uuid
from contextlib import closing
from pathlib import Path

from .db import SCHEMA_VERSION
from .errors import PmtError
from .util import canonical_json, fingerprint, new_id, utc_now

_MANIFEST = "manifest.json"
_DB_FILE = "pmt.sqlite3"
_TEMP_DAYS = 7


def _hash(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _is_reparse(st: os.stat_result) -> bool:
    return bool(getattr(st, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _reject_links(path: Path, *, include_leaf=True) -> None:
    path = Path(os.path.abspath(path))
    parts = list(path.parents)[::-1] + ([path] if include_leaf else [])
    for item in parts:
        try:
            st = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(st.st_mode) or _is_reparse(st):
            raise PmtError("resource_link_rejected", "Resource paths cannot contain links or reparse points")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _safe_source(source: str, allowed_root: str) -> tuple[Path, Path]:
    if not isinstance(source, str) or not isinstance(allowed_root, str):
        raise PmtError("invalid_resource_path", "source_path and allowed_root must be paths")
    src, root = Path(os.path.abspath(source)), Path(os.path.abspath(allowed_root))
    _reject_links(root)
    _reject_links(src)
    if not _inside(src, root):
        raise PmtError("resource_path_outside_root", "Source path is outside allowed_root")
    try:
        if not src.is_file():
            raise PmtError("resource_source_invalid", "Source must be an existing regular file")
    except OSError as exc:
        raise PmtError("resource_io_error", "Source file cannot be inspected", 4, True) from exc
    return src, root


def _start_job(db, req, operation: str, relative_path: str | None = None) -> str:
    job_id = new_id()
    with db.write() as conn:
        active = conn.execute("SELECT id FROM file_jobs WHERE state='active' LIMIT 1").fetchone()
        if active:
            raise PmtError("file_job_active", "Another file operation is active", 4, True)
        conn.execute("INSERT INTO file_jobs(id,operation,owner,state,relative_path,started_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (job_id, operation, req.get("request_id") or job_id, "active", relative_path, utc_now(), utc_now()))
    return job_id


def _finish_job(db, job_id: str, state: str, code: str | None = None, *, owner=None):
    with db.write(maintenance_owner=owner) as conn:
        conn.execute("UPDATE file_jobs SET state=?,error_code=?,updated_at=? WHERE id=?",
                     (state, code, utc_now(), job_id))


def _artifact_path(db, relative: str) -> Path:
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts:
        raise PmtError("artifact_path_invalid", "Stored artifact path is invalid", 5)
    root = db.root.resolve()
    path = root / rel
    _reject_links(path)
    if not _inside(path.resolve(strict=False), root):
        raise PmtError("artifact_path_invalid", "Stored artifact path escapes data root", 5)
    return path


def check_artifact(db, conn, artifact_id: str) -> dict:
    row = conn.execute("SELECT sha256,size_bytes,relative_path,state FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if row is None:
        return {"valid": False, "reason": "missing_record", "sha256": None}
    if row[3] != "ready":
        return {"valid": False, "reason": "not_ready", "sha256": row[0]}
    try:
        path = _artifact_path(db, row[2])
        if not path.is_file():
            return {"valid": False, "reason": "missing_file", "sha256": row[0]}
        digest, size = _hash(path)
    except (OSError, PmtError):
        return {"valid": False, "reason": "unreadable_or_unsafe", "sha256": row[0]}
    if digest != row[0] or size != row[1]:
        return {"valid": False, "reason": "corrupt", "sha256": digest}
    return {"valid": True, "reason": "ready", "sha256": digest}


def _register_handler(conn, req, *, artifact_id, digest, size, relative, retention, scope_id, owner_record_id):
    existing = conn.execute("SELECT id FROM artifacts WHERE scope_id IS ? AND sha256=? AND relative_path=?",
                            (scope_id, digest, relative)).fetchone()
    if existing:
        artifact_id = existing[0]
    else:
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,retention_until,created_at) VALUES(?,?,?,?,?,?,?,?)",
                     (artifact_id, scope_id, digest, size, relative, "ready",
                      None if retention == "evidence" else _after_days(_TEMP_DAYS), utc_now()))
    if owner_record_id:
        record = conn.execute("SELECT scope_id FROM records WHERE id=?", (owner_record_id,)).fetchone()
        if not record or (scope_id and record[0] != scope_id):
            raise PmtError("resource_owner_invalid", "owner_record_id is missing or outside scope")
        conn.execute("INSERT OR IGNORE INTO artifact_refs(artifact_id,owner_type,owner_id,purpose,created_at) VALUES(?,?,?,?,?)",
                     (artifact_id, "record", owner_record_id, "evidence" if retention == "evidence" else "temporary", utc_now()))
    return {"artifact_id": artifact_id, "sha256": digest, "size_bytes": size, "relative_path": relative, "state": "ready"}


def _after_days(days):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _diagnose(db, conn):
    rows = conn.execute("SELECT id,scope_id,sha256,size_bytes,relative_path,state,retention_until FROM artifacts ORDER BY id").fetchall()
    referenced = {r[0] for r in conn.execute("SELECT DISTINCT artifact_id FROM artifact_refs")}
    artifacts = []
    known_files = set()
    now = utc_now()
    candidates = []
    for row in rows:
        aid, scope, expected, size, relative, state, retention_until = tuple(row)
        known_files.add(relative)
        check = check_artifact(db, conn, aid)
        if not check["valid"]:
            artifacts.append({"artifact_id": aid, "issue": check["reason"]})
        if aid not in referenced and retention_until and retention_until <= now:
            candidates.append({"artifact_id": aid, "reason": "unreferenced_retention_expired", "retention_until": retention_until})
    orphans, unsafe_paths = [], []
    for relative_root in (Path("resources") / "objects", Path("resources") / ".staging"):
        stack = [db.root / relative_root]
        while stack:
            current = stack.pop()
            try:
                st = current.lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(st.st_mode) or _is_reparse(st):
                unsafe_paths.append(current.relative_to(db.root).as_posix())
                continue
            if stat.S_ISDIR(st.st_mode):
                try:
                    stack.extend(current.iterdir())
                except OSError:
                    unsafe_paths.append(current.relative_to(db.root).as_posix())
            elif stat.S_ISREG(st.st_mode):
                rel = current.relative_to(db.root).as_posix()
                if rel not in known_files:
                    orphans.append(rel)
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        artifacts.append({"issue": "database_integrity", "detail": integrity})
    active_jobs = [dict(row) for row in conn.execute(
        "SELECT id,operation,state,started_at,error_code FROM file_jobs WHERE state='active' ORDER BY started_at")]
    maintenance = conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()
    return {"issues": artifacts, "orphans": sorted(orphans), "unsafe_paths": sorted(unsafe_paths),
            "retention_candidates": candidates,
            "active_file_jobs": active_jobs, "maintenance_active": bool(maintenance and maintenance[0]),
            "database_integrity": integrity, "automatic_deletion": False}


def handle(db, conn, req):
    """Handle database-only diagnose operation on a caller-owned connection."""
    op = req.get("operation")
    if op == "diagnose":
        return _diagnose(db, conn)
    raise PmtError("unsupported_resource_operation", "Operation is not handled by the resource module")


def _check_replay(db, req):
    request_id = req.get("request_id")
    if not request_id:
        return None
    normalized = db._normalize_request(req)
    ignored = {"request_id", "correlation_id", "received_at", "received_at_utc", "retry_count", "attempt"}
    req_fp = fingerprint({key: value for key, value in normalized.items() if key not in ignored})
    with closing(db.connect()) as conn:
        old = conn.execute("SELECT request_fingerprint,response_json,exit_code,actor,session_id FROM requests WHERE request_id=?",
                            (request_id,)).fetchone()
    if old:
        if old[3] != normalized.get("actor") or old[4] != normalized.get("session_id"):
            err = PmtError("request_owner_mismatch", "request result belongs to a different actor or session", 3)
            return db._response(request_id, False, None, err.as_dict(), []), err.exit_code
        if old[0] != req_fp:
            err = PmtError("request_conflict", "request_id was already used for a different request", 3)
            return db._response(request_id, False, None, err.as_dict(), []), err.exit_code
        return json.loads(old[1]), old[2]
    return None


def _wait_for_same_request(db, req, operation):
    """Join an in-progress operation with this request ID and return its replay."""
    request_id = req.get("request_id")
    deadline = time.monotonic() + max(2.0, db.busy_timeout_ms / 1000)
    while True:
        with closing(db.connect()) as conn:
            active = conn.execute("SELECT 1 FROM file_jobs WHERE operation=? AND owner=? AND state='active' LIMIT 1",
                                  (operation, request_id)).fetchone()
        if not active:
            return _check_replay(db, req)
        replay = _check_replay(db, req)
        if replay is not None:
            return replay
        if time.monotonic() >= deadline:
            raise PmtError("file_job_active", "The same request is still processing", 4, True)
        time.sleep(0.02)


def _validate_request(req):
    if not isinstance(req, dict) or not isinstance(req.get("payload", {}), dict):
        raise PmtError("invalid_request", "Request and payload must be objects")
    request_id = req.get("request_id")
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError("noncanonical")
    except (ValueError, TypeError, AttributeError):
        raise PmtError("invalid_request_id", "request_id must be a canonical UUID")


def _register(db, req):
    replay = _check_replay(db, req)
    if replay is not None:
        return replay
    replay = _wait_for_same_request(db, req, "register_resource")
    if replay is not None:
        return replay
    p = req.get("payload", {})
    retention = p.get("retention")
    if retention not in ("evidence", "temporary"):
        raise PmtError("invalid_retention", "retention must be evidence or temporary")
    src, _ = _safe_source(p.get("source_path"), p.get("allowed_root"))
    scope_id = req.get("scope_id")
    owner_record_id = p.get("owner_record_id")
    with closing(db.connect()) as conn:
        if scope_id and not conn.execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone():
            raise PmtError("scope_not_found", "scope_id does not exist")
        if owner_record_id:
            record = conn.execute("SELECT scope_id FROM records WHERE id=?", (owner_record_id,)).fetchone()
            if not record:
                raise PmtError("resource_owner_invalid", "owner_record_id does not exist")
            if scope_id and record[0] != scope_id:
                raise PmtError("resource_owner_invalid", "owner_record_id is outside scope_id")
    artifact_id = new_id()
    staging_dir = db.root / "resources" / ".staging"
    object_dir = db.root / "resources" / "objects"
    staged = staging_dir / (artifact_id + ".part")
    try:
        job = _start_job(db, req, "register_resource", staged.relative_to(db.root).as_posix())
    except PmtError as err:
        if err.code != "file_job_active":
            raise
        replay = _wait_for_same_request(db, req, "register_resource")
        if replay is not None:
            return replay
        raise
    preflight = _check_replay(db, req)
    if preflight is not None:
        _finish_job(db, job, "completed" if preflight[1] == 0 else "failed",
                    None if preflight[1] == 0 else (preflight[0].get("error") or {}).get("code"))
        return preflight
    try:
        staging_dir.mkdir(parents=True, exist_ok=True)
        object_dir.mkdir(parents=True, exist_ok=True)
        _reject_links(staging_dir); _reject_links(object_dir)
        shutil.copyfile(src, staged)
        digest, size = _hash(staged)
        final_rel = (Path("resources") / "objects" / artifact_id).as_posix()
        final = _artifact_path(db, final_rel)
        try:
            os.link(staged, final)
        except FileExistsError:
            if _hash(final) != (digest, size):
                raise PmtError("resource_publish_conflict", "Existing artifact path has different content", 4, True)
        staged.unlink(missing_ok=True)
        result, code = db.run_request(req, lambda conn, request: _register_handler(
            conn, request, artifact_id=artifact_id, digest=digest, size=size, relative=final_rel,
            retention=retention, scope_id=scope_id, owner_record_id=p.get("owner_record_id")))
        if code == 0:
            _finish_job(db, job, "completed")
        else:
            _finish_job(db, job, "failed", (result.get("error") or {}).get("code"))
        return result, code
    except Exception as exc:
        try:
            _finish_job(db, job, "failed", getattr(exc, "code", "resource_io_error"))
        except Exception as finalize_error:
            raise PmtError("resource_job_finalize_failed", "Resource failed and its file job could not be finalized", 4, True,
                           {"operation_error": getattr(exc, "code", type(exc).__name__),
                            "finalization_error": type(finalize_error).__name__}) from finalize_error
        if isinstance(exc, PmtError):
            raise
        raise PmtError("resource_io_error", "Resource file operation failed", 4, True,
                       {"exception_type": type(exc).__name__, "errno": getattr(exc, "errno", None),
                        "winerror": getattr(exc, "winerror", None)}) from exc
    finally:
        try:
            staged.unlink(missing_ok=True)
        except OSError as exc:
            raise PmtError("resource_staging_cleanup_failed", "Staging file could not be removed", 4, True) from exc


def _check_empty_destination(path: str) -> Path:
    if not isinstance(path, str) or not path:
        raise PmtError("invalid_destination", "destination_root is required")
    target = Path(os.path.abspath(path))
    _reject_links(target, include_leaf=True)
    if target.exists():
        if not target.is_dir() or any(target.iterdir()):
            raise PmtError("destination_not_empty", "Destination must be a new or empty directory")
    return target


def _empty_destination(path: str) -> Path:
    target = _check_empty_destination(path)
    if not target.exists():
        target.mkdir(parents=True)
    return target


def _acquire_maintenance(db, operation: str, request_id: str):
    owner = f"{operation}:{new_id()}"
    job = new_id()
    with db.write() as conn:
        active = conn.execute("SELECT id FROM file_jobs WHERE state='active' LIMIT 1").fetchone()
        meta = conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()
        if active or (meta and meta[0]):
            if active:
                same = conn.execute("SELECT 1 FROM file_jobs WHERE id=? AND operation=? AND owner=? AND state='active'",
                                    (active[0], operation, request_id)).fetchone()
                if same:
                    raise PmtError("file_job_active", "The same request is still processing", 4, True)
            raise PmtError("maintenance_unavailable", "Active file work prevents maintenance", 4, True)
        conn.execute("UPDATE meta SET value=? WHERE key='maintenance_owner'", (owner,))
        conn.execute("INSERT INTO file_jobs(id,operation,owner,state,started_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (job, operation, request_id, "active", utc_now(), utc_now()))
        ready = conn.execute("SELECT id,scope_id,sha256,size_bytes,relative_path,retention_until,created_at FROM artifacts WHERE state='ready' ORDER BY id").fetchall()
    return owner, job, ready


def _release_maintenance(db, owner):
    with db.write(maintenance_owner=owner) as conn:
        conn.execute("UPDATE meta SET value='' WHERE key='maintenance_owner' AND value=?", (owner,))


def _backup(db, req):
    replay = _check_replay(db, req)
    if replay is not None:
        return replay
    replay = _wait_for_same_request(db, req, "backup")
    if replay is not None:
        return replay
    try:
        owner, job, ready = _acquire_maintenance(db, "backup", req.get("request_id"))
    except PmtError as err:
        if err.code not in ("file_job_active", "maintenance_unavailable"):
            raise
        replay = _wait_for_same_request(db, req, "backup")
        if replay is not None:
            return replay
        raise
    backup_id = new_id()
    complete = False
    started_backup = False
    dest = None
    try:
        replay = _check_replay(db, req)
        if replay is not None:
            complete = replay[1] == 0
            return replay
        dest = _empty_destination(req.get("payload", {}).get("destination_root"))
        started_backup = True
        db_path = dest / _DB_FILE
        with closing(db.connect()) as source, closing(sqlite3.connect(str(db_path), isolation_level=None)) as output:
            source.backup(output)
        # The restored snapshot must not inherit the temporary backup lock/job.
        with closing(sqlite3.connect(str(db_path))) as snapshot:
            snapshot.execute("UPDATE meta SET value='' WHERE key='maintenance_owner'")
            snapshot.execute("UPDATE file_jobs SET state='completed',error_code=NULL,updated_at=? WHERE id=?", (utc_now(), job))
            snapshot.commit()
        db_hash, db_size = _hash(db_path)
        blob_dir = dest / "resources"
        artifacts = []
        for row in ready:
            aid, scope, digest, size, relative, retention_until, created = tuple(row)
            src = _artifact_path(db, relative)
            with closing(db.connect()) as check_conn:
                check = check_artifact(db, check_conn, aid)
            if not check["valid"]:
                raise PmtError("backup_resource_invalid", f"Ready artifact {aid} failed validation", 4, True)
            relative_in_backup = f"resources/{aid}"
            target = dest / relative_in_backup
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, target)
            if _hash(target) != (digest, size):
                raise PmtError("backup_hash_mismatch", f"Artifact {aid} changed during backup", 4, True)
            artifacts.append({"id": aid, "scope_id": scope, "sha256": digest, "size_bytes": size,
                              "relative_path": relative, "backup_path": relative_in_backup,
                              "retention_until": retention_until, "created_at": created})
        manifest = {"format_version": 1, "state": "ready", "backup_id": backup_id, "schema_version": SCHEMA_VERSION,
                    "created_at": utc_now(), "database": {"path": _DB_FILE, "sha256": db_hash, "size_bytes": db_size},
                    "artifacts": artifacts}
        (dest / _MANIFEST).write_text(canonical_json(manifest), encoding="utf-8")
        request_result, code = db.run_request(req, lambda conn, request: {
            "backup_id": backup_id, "destination_root": str(dest), "artifact_count": len(artifacts),
            "manifest_sha256": _hash(dest / _MANIFEST)[0], "schema_version": SCHEMA_VERSION
        }, maintenance_owner=owner)
        complete = code == 0
        return request_result, code
    except PmtError:
        raise
    except Exception as exc:
        raise PmtError("backup_io_error", "Backup operation failed", 4, True) from exc
    finally:
        finalization_errors = []
        if started_backup and not complete:
            try:
                (dest / _MANIFEST).write_text(canonical_json({
                    "format_version": 1, "state": "incomplete", "backup_id": backup_id,
                    "schema_version": SCHEMA_VERSION, "created_at": utc_now()}), encoding="utf-8")
            except OSError:
                finalization_errors.append("incomplete_manifest_write")
        try:
            with db.write(maintenance_owner=owner) as conn:
                conn.execute("UPDATE file_jobs SET state=?,error_code=?,updated_at=? WHERE id=?",
                             ("completed" if complete else "failed", None if complete else "backup_failed", utc_now(), job))
        except Exception:
            finalization_errors.append("file_job_finalize")
        try:
            _release_maintenance(db, owner)
        except Exception:
            finalization_errors.append("maintenance_release")
        if finalization_errors:
            raise PmtError("backup_finalize_failed", "Backup finalization did not complete", 4, True,
                           {"failures": finalization_errors})


def _validate_backup(backup_path: str):
    root = Path(os.path.abspath(backup_path))
    _reject_links(root)
    if not root.is_dir():
        raise PmtError("backup_not_found", "Backup directory does not exist")
    try:
        manifest_path = root / _MANIFEST
        _reject_links(manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PmtError("backup_manifest_invalid", "Backup manifest is missing or invalid") from exc
    if not isinstance(manifest, dict) or manifest.get("format_version") != 1 or manifest.get("state", "ready") != "ready":
        raise PmtError("backup_manifest_invalid", "Backup manifest format is unsupported")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise PmtError("backup_schema_unsupported", "Backup schema does not match this runtime")
    db_info = manifest.get("database")
    if not isinstance(db_info, dict) or db_info.get("path") != _DB_FILE:
        raise PmtError("backup_manifest_invalid", "Backup database entry is invalid")
    db_file = root / _DB_FILE
    _reject_links(db_file)
    if not db_file.is_file() or _hash(db_file) != (db_info.get("sha256"), db_info.get("size_bytes")):
        raise PmtError("backup_hash_mismatch", "Backup database hash or size does not match")
    artifacts = manifest.get("artifacts")
    if not isinstance(artifacts, list):
        raise PmtError("backup_manifest_invalid", "Artifact manifest is invalid")
    for item in artifacts:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise PmtError("backup_manifest_invalid", "Artifact manifest entry is invalid")
        rel = Path(item.get("backup_path", ""))
        if rel.is_absolute() or ".." in rel.parts:
            raise PmtError("backup_manifest_invalid", "Artifact backup path is unsafe")
        file = root / rel
        _reject_links(file)
        if not file.is_file() or _hash(file) != (item.get("sha256"), item.get("size_bytes")):
            raise PmtError("backup_hash_mismatch", f"Artifact {item['id']} is missing or corrupt")
    return root, manifest


def _restore(db, req):
    replay = _check_replay(db, req)
    if replay is not None:
        return replay
    replay = _wait_for_same_request(db, req, "restore")
    if replay is not None:
        return replay
    p = req.get("payload", {})
    root, manifest = _validate_backup(p.get("backup_path"))
    try:
        dest = _check_empty_destination(p.get("destination_root"))
    except PmtError:
        replay = _check_replay(db, req)
        if replay is not None:
            return replay
        raise
    live = db.root.resolve()
    target = dest.resolve()
    if _inside(target, live) or _inside(live, target):
        raise PmtError("restore_destination_not_isolated", "Restore destination must be separate from the active data root")
    try:
        job = _start_job(db, req, "restore")
    except PmtError as err:
        if err.code != "file_job_active":
            raise
        replay = _wait_for_same_request(db, req, "restore")
        if replay is not None:
            return replay
        raise
    preflight = _check_replay(db, req)
    if preflight is not None:
        _finish_job(db, job, "completed" if preflight[1] == 0 else "failed",
                    None if preflight[1] == 0 else (preflight[0].get("error") or {}).get("code"))
        return preflight
    stage = dest.parent / (dest.name + ".pmt-restore-" + new_id())
    complete = False
    try:
        dest = _empty_destination(str(dest))
        stage.mkdir()
        with closing(sqlite3.connect(str(root / _DB_FILE))) as source, closing(sqlite3.connect(str(stage / _DB_FILE), isolation_level=None)) as output:
            source.backup(output)
        with closing(sqlite3.connect(str(stage / _DB_FILE))) as check:
            check.execute("PRAGMA foreign_keys=ON")
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_key_issue = check.execute("PRAGMA foreign_key_check").fetchone()
            version = check.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if integrity != "ok" or foreign_key_issue or not version or int(version[0]) != SCHEMA_VERSION:
                raise PmtError("restore_database_invalid", "Restored database integrity or schema check failed")
            for table, col in (("artifacts", "id"), ("scopes", "id"), ("records", "id"), ("artifact_refs", "artifact_id")):
                check.execute(f"SELECT {col} FROM {table} LIMIT 0")
        for item in manifest["artifacts"]:
            target = stage / "resources" / "objects" / item["id"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / item["backup_path"], target)
        with closing(sqlite3.connect(str(stage / _DB_FILE))) as check:
            ready_rows = check.execute("SELECT id,scope_id,sha256,size_bytes,relative_path,state,retention_until,created_at FROM artifacts WHERE state='ready' ORDER BY id").fetchall()
            manifest_ids = [item["id"] for item in manifest["artifacts"]]
            if len(manifest_ids) != len(set(manifest_ids)) or {row[0] for row in ready_rows} != set(manifest_ids):
                raise PmtError("restore_manifest_mismatch", "Manifest does not cover every ready database artifact")
            for item in manifest["artifacts"]:
                row = check.execute("SELECT sha256,size_bytes,relative_path,state FROM artifacts WHERE id=?", (item["id"],)).fetchone()
                expected_rel = f"resources/objects/{item['id']}"
                if not row or row[0] != item["sha256"] or row[1] != item["size_bytes"] or row[2] != expected_rel or row[3] != "ready":
                    raise PmtError("restore_manifest_mismatch", "Artifact manifest and database differ")
        # Publish the verified root with one same-volume rename; never expose a partial restore.
        dest.rmdir()
        os.replace(stage, dest)
        result, code = db.run_request(req, lambda _conn, _request: {
            "restored": True, "destination_root": str(dest), "backup_id": manifest["backup_id"],
            "artifact_count": len(manifest["artifacts"]), "schema_version": SCHEMA_VERSION
        })
        complete = code == 0
        return result, code
    except PmtError:
        raise
    except Exception as exc:
        raise PmtError("restore_io_error", "Restore operation failed", 4, True) from exc
    finally:
        finalization_errors = []
        try:
            shutil.rmtree(stage)
        except FileNotFoundError:
            pass
        except OSError:
            finalization_errors.append("restore_staging_cleanup")
        try:
            _finish_job(db, job, "completed" if complete else "failed", None if complete else "restore_failed")
        except Exception:
            finalization_errors.append("file_job_finalize")
        if finalization_errors:
            raise PmtError("restore_finalize_failed", "Restore finalization did not complete", 4, True,
                           {"failures": finalization_errors})


def execute(db, req):
    """Return protocol envelope and exit code for P4 operations."""
    op = req.get("operation") if isinstance(req, dict) else None
    try:
        if op in ("register_resource", "backup", "restore"):
            _validate_request(req)
        if op == "register_resource":
            return _register(db, req)
        if op == "backup":
            return _backup(db, req)
        if op == "restore":
            return _restore(db, req)
        if op == "diagnose":
            return db.run_request(req, lambda conn, request: handle(db, conn, request))
        return db.run_request(req, lambda _conn, _request: (_ for _ in ()).throw(
            PmtError("unsupported_resource_operation", "Operation is not handled by the resource module")))
    except PmtError as err:
        request_id = req.get("request_id") if isinstance(req, dict) else None
        return db._response(request_id, False, None, err.as_dict(), []), err.exit_code
    except sqlite3.OperationalError as exc:
        err = db._sqlite_error(exc)
        request_id = req.get("request_id") if isinstance(req, dict) else None
        return db._response(request_id, False, None, err.as_dict(), []), err.exit_code
    except OSError:
        err = PmtError(f"{op or 'resource'}_io_error", "File operation failed", 4, True)
        request_id = req.get("request_id") if isinstance(req, dict) else None
        return db._response(request_id, False, None, err.as_dict(), []), err.exit_code
    except Exception:
        err = PmtError("resource_internal_error", "Resource operation failed unexpectedly", 5, False)
        request_id = req.get("request_id") if isinstance(req, dict) else None
        return db._response(request_id, False, None, err.as_dict(), []), err.exit_code
