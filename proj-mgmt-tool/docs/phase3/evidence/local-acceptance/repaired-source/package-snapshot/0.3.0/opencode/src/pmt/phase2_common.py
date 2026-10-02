"""Shared phase-two validation, resource publication and ownership boundaries."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
import uuid

from .errors import PmtError
from .lifecycle import _event
from .resources import _artifact_path, _reject_links, check_artifact
from .util import canonical_json, new_id, strict_json_loads, utc_now


def identifier(value, label="id"):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise PmtError("invalid_identifier", f"{label} must be a canonical UUID")
    return value


def validate_scope(db, conn, scope_id):
    identifier(scope_id, "scope_id")
    row = conn.execute("SELECT * FROM scopes WHERE id=?", (scope_id,)).fetchone()
    if row is None:
        raise PmtError("scope_not_found", "Scope does not exist")
    return dict(row)


def project_scope_id(conn, scope_id):
    """Resolve classification records to their project's stable identity."""
    seen = set()
    while scope_id and scope_id not in seen:
        seen.add(scope_id)
        row = conn.execute("SELECT kind,parent_id FROM scopes WHERE id=?", (scope_id,)).fetchone()
        if not row:
            break
        if row["kind"] == "project":
            return scope_id
        scope_id = row["parent_id"]
    raise PmtError("project_scope_required", "Task must belong to a project")


def event(conn, req, event_type, scope_id=None, record_id=None, payload=None):
    return _event(conn, req, event_type=event_type, scope_id=scope_id,
                  record_id=record_id, reason=req.get("payload", {}).get("reason"), payload=payload)


def replay(db, req):
    """Check semantic equality before performing filesystem side effects."""
    from .resources import _check_replay
    return _check_replay(db, req)


def persist_json_resource(db, req, value, scope_id, purpose, owner_id=None):
    wire = canonical_json(value).encode("utf-8")
    if len(wire) > 8 * 1024 * 1024:
        raise PmtError("resource_too_large", "Structured resource exceeds 8 MiB")
    digest = hashlib.sha256(wire).hexdigest()
    artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"pmt:{scope_id}:{digest}"))
    journal_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), purpose + digest))
    relative = f"resources/objects/{digest[:2]}/{digest}.json"
    stage_relative = f"resources/objects/{digest[:2]}/.pmt-{journal_id}.stage"
    immutable_journal = {"artifact_id": artifact_id, "relative_path": relative, "sha256": digest,
                        "size_bytes": len(wire), "stage_relative_path": stage_relative}
    now = utc_now()
    with db.write() as conn:
        validate_scope(db, conn, scope_id)
        conn.execute("INSERT OR IGNORE INTO operation_journal(id,kind,state,body_json,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?)", (journal_id, "resource_publish", "staging",
                         canonical_json(immutable_journal), now, now))
        journal = conn.execute("SELECT state,body_json FROM operation_journal WHERE id=?", (journal_id,)).fetchone()
        try:
            prior_journal = json.loads(journal["body_json"])
        except (TypeError, ValueError) as exc:
            raise PmtError("resource_journal_corrupt", "Resource publication journal is invalid", 5) from exc
        if not isinstance(prior_journal, dict):
            raise PmtError("resource_journal_corrupt", "Resource publication journal must be an object", 5)
        for key in ("artifact_id", "relative_path", "sha256"):
            if prior_journal.get(key) != immutable_journal[key]:
                raise PmtError("resource_journal_conflict", "Request journal is bound to different resource bytes", 3)
        prior_stage = prior_journal.get("stage_relative_path")
        if prior_stage is not None and prior_stage != stage_relative:
            raise PmtError("resource_journal_conflict", "Request journal is bound to a different local stage", 3)
        journal_body = dict(prior_journal) | immutable_journal
        conn.execute("UPDATE operation_journal SET state='staging',body_json=?,updated_at=? WHERE id=?",
                     (canonical_json(journal_body), now, journal_id))
    target = _artifact_path(db, relative)
    stage = _artifact_path(db, stage_relative)
    try:
        from .resources import _publish_content_addressed_bytes
        publication = _publish_content_addressed_bytes(target, stage, wire, digest)
        with db.write() as conn:
            validate_scope(db, conn, scope_id)
            existing = conn.execute("SELECT id,state FROM artifacts WHERE scope_id=? AND sha256=? AND relative_path=?",
                                    (scope_id, digest, relative)).fetchone()
            if existing and existing["state"] != "ready":
                raise PmtError("resource_not_ready", "Resource is reserved or unavailable", 3)
            if existing:
                artifact_id = existing["id"]
            else:
                conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,retention_until,created_at) "
                             "VALUES(?,?,?,?,?,'ready',NULL,?)", (artifact_id, scope_id, digest, len(wire), relative, now))
            if owner_id:
                conn.execute("INSERT OR IGNORE INTO artifact_refs(artifact_id,owner_type,owner_id,purpose,created_at) "
                     "VALUES(?,?,?,?,?)", (artifact_id, "phase2", owner_id, purpose, now))
            # Graphs and directives can reference existing evidence without copying its body.
            # Index those immutable references so retention cannot remove an in-use resource.
            referenced_ids = set(re.findall(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", wire.decode("utf-8")))
            for referenced_id in referenced_ids - {artifact_id}:
                referred = conn.execute("SELECT state FROM artifacts WHERE id=?", (referenced_id,)).fetchone()
                if referred:
                    if referred[0] != "ready":
                        raise PmtError("resource_not_ready", "Referenced resource is reserved or unavailable", 3)
                    conn.execute("INSERT OR IGNORE INTO artifact_refs VALUES(?,?,?,?,?)",
                                 (referenced_id, "artifact", artifact_id, "context", now))
            final_journal = journal_body | {"state": "completed", "publication": publication}
            conn.execute("UPDATE operation_journal SET state='completed',body_json=?,updated_at=? WHERE id=?",
                         (canonical_json(final_journal), utc_now(), journal_id))
        return {"artifact_id": artifact_id, "sha256": digest, "size_bytes": len(wire),
                "relative_path": relative, "state": "ready"}
    except OSError as exc:
        raise PmtError("resource_io_error", "Structured resource publication failed", 4, True) from exc


def load_json_resource(db, conn, artifact_id):
    identifier(artifact_id, "artifact_id")
    check = check_artifact(db, conn, artifact_id)
    if not check["valid"]:
        raise PmtError("resource_invalid", "Referenced resource is unavailable or corrupt", 3,
                       details={"reason": check["reason"]})
    row = conn.execute("SELECT relative_path,size_bytes FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    if row["size_bytes"] > 8 * 1024 * 1024:
        raise PmtError("resource_too_large", "Structured resource exceeds 8 MiB")
    return strict_json_loads(_artifact_path(db, row["relative_path"]).read_bytes(), max_bytes=8 * 1024 * 1024)


_CANONICAL_WORKSPACE_URI = re.compile(r"^pmt://([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/([0-9a-f]{64})$")


def normalized_workspace(value):
    if not isinstance(value, str) or not value.strip():
        raise PmtError("invalid_workspace", "An absolute workspace or canonical PMT workspace URI is required")
    match = _CANONICAL_WORKSPACE_URI.fullmatch(value)
    if match:
        try:
            if str(uuid.UUID(match.group(1))) != match.group(1):
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            raise PmtError("invalid_workspace", "Canonical PMT workspace URI has an invalid repository UUID")
        return value
    if not Path(value).is_absolute():
        raise PmtError("invalid_workspace", "An absolute workspace is required")
    return os.path.normcase(str(Path(value).resolve()))


def require_workspace_claim(db, conn, req, workspace, paths):
    run_id = identifier(req.get("payload", {}).get("run_id"), "run_id")
    run = conn.execute("SELECT * FROM execution_runs WHERE id=?", (run_id,)).fetchone()
    if not run or run["owner_session"] != req["session_id"] or run["state"] not in {
            "starting", "running", "review_pending", "reconciling", "cancel_requested"}:
        raise PmtError("ownership_conflict", "An active run owned by this session is required", 3)
    root = normalized_workspace(workspace)
    if normalized_workspace(run["workspace"]) != root:
        raise PmtError("ownership_conflict", "Run belongs to another workspace", 3)
    locks = conn.execute("SELECT kind,workspace,resource FROM scope_locks WHERE run_id=?", (run_id,)).fetchall()
    if not locks:
        # A grouped child may use the representative P2 run's union only through
        # the explicit, owner-bound batch_binding written by F9.
        from .efficiency.batch import child_scope_authority
        grant = child_scope_authority(db, conn, req, run, workspace, paths)
        if grant is None:
            raise PmtError("scope_not_owned", "Run has no confirmed scope ownership", 3)
        return dict(run) | {"batch_authority": grant}
    for path in paths:
        relative = Path(path)
        if relative.is_absolute() or ".." in relative.parts:
            raise PmtError("invalid_scope_path", "Claim path must be relative to workspace")
        candidate = os.path.normcase(relative.as_posix()).replace("\\", "/").strip("/") or "."
        covered = False
        for lock in locks:
            if lock["kind"] not in {"path", "workspace"} or normalized_workspace(lock["workspace"]) != root:
                continue
            resource = os.path.normcase(lock["resource"]).replace("\\", "/").strip("/") or "."
            if resource == "." or candidate == resource or candidate.startswith(resource + "/"):
                covered = True
                break
        if not covered:
            raise PmtError("scope_not_owned", "Required workspace scope has not been claimed", 3)
    return dict(run)
