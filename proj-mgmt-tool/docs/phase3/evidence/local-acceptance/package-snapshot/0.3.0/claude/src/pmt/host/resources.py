"""Authenticated, content-addressed Host resource storage.

The transport adapter passes resource bytes separately from ordinary JSON
requests. Client paths and filenames are never accepted by this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import closing
from pathlib import Path

from .. import resources as local_resources
from ..errors import PmtError
from ..util import canonical_json, new_id, utc_now
from .auth import identifier

MAX_RESOURCE_BYTES = 8 * 1024 * 1024
PURPOSES = frozenset({"evidence", "result", "graph_snapshot", "verification_snapshot", "step_directive"})
DDL = """
CREATE TABLE IF NOT EXISTS host_resource_journal(
 request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, session_id TEXT NOT NULL,
 device_id TEXT NOT NULL, scope_id TEXT NOT NULL, purpose TEXT NOT NULL,
 private_step_id TEXT, run_id TEXT, directive_version INTEGER,
 sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL, staging_path TEXT NOT NULL,
 artifact_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
 state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_resource_requests(
 request_id TEXT PRIMARY KEY, actor TEXT NOT NULL, session_id TEXT NOT NULL,
 device_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response_json TEXT NOT NULL,
 created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS host_resource_metadata(
 artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id), scope_id TEXT NOT NULL,
 purpose TEXT NOT NULL, private_step_id TEXT, run_id TEXT, directive_version INTEGER,
 owner_device_id TEXT NOT NULL, owner_session_id TEXT NOT NULL,
 request_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL);
"""


def _hash_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_request_id(value: object) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PmtError("invalid_request_id", "request_id must be a canonical UUID")
    return value


class HostResourceStore:
    """Host resource boundary using current Host authentication on every call."""

    def __init__(self, db, auth, *, max_bytes: int = MAX_RESOURCE_BYTES):
        if type(max_bytes) is not int or not 1 <= max_bytes <= MAX_RESOURCE_BYTES:
            raise ValueError("max_bytes must be in the supported 1..8 MiB range")
        self.db, self.auth, self.max_bytes = db, auth, max_bytes
        with db.write() as conn:
            for statement in DDL.split(";"):
                if statement.strip():
                    conn.execute(statement)

    def _principal(self, conn, headers):
        authorization = headers.get("authorization", headers.get("Authorization", ""))
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise PmtError("unauthenticated", "A registered device credential is required", 3)
        session_id = headers.get("x-pmt-session", headers.get("X-PMT-Session"))
        environment_id = headers.get("x-pmt-environment", headers.get("X-PMT-Environment"))
        if not isinstance(session_id, str) or not session_id.strip() or not isinstance(environment_id, str) or not environment_id.strip():
            raise PmtError("unauthenticated", "A registered nonempty session and environment are required", 3)
        return self.auth.authenticate(conn, authorization[7:],
            headers.get("x-pmt-device", headers.get("X-PMT-Device", "")),
            headers.get("x-pmt-namespace", headers.get("X-PMT-Namespace", "")),
            session_id=session_id, environment_id=environment_id)

    def _authorize_scope(self, conn, principal, scope_id):
        principal.require("write")
        self.auth.authorize_scope(conn, principal, scope_id)

    def _publish_staged(self, request_id, scope_id, staging_rel, final_rel, digest, size, headers):
        """Publish only the journaled hash target; retry transient Windows sharing locks briefly."""
        staging = local_resources._artifact_path(self.db, staging_rel)
        final = local_resources._artifact_path(self.db, final_rel)
        delays = (0.05, 0.15, 0.30)
        for attempt in range(len(delays) + 1):
            # Each attempt rechecks caller authority and the exact journal binding.
            with closing(self.db.connect()) as conn:
                conn.execute("BEGIN")
                try:
                    principal = self._principal(conn, headers)
                    self._authorize_scope(conn, principal, scope_id)
                    journal = conn.execute("SELECT * FROM host_resource_journal WHERE request_id=?",
                                           (request_id,)).fetchone()
                    if (not journal or journal["actor"] != principal.actor
                            or journal["session_id"] != principal.session_id
                            or journal["device_id"] != principal.device_id
                            or journal["scope_id"] != scope_id or journal["sha256"] != digest
                            or journal["size_bytes"] != size or journal["staging_path"] != staging_rel
                            or journal["state"] not in {"authorized", "staged"}):
                        raise PmtError("request_owner_mismatch", "Current authority or resource journal changed", 3)
                    conn.rollback()
                except Exception:
                    conn.rollback()
                    raise
            local_resources._reject_links(staging)
            local_resources._reject_links(final)
            if final.exists():
                if local_resources._hash(final) != (digest, size):
                    raise PmtError("host_resource_publish_conflict", "Content-addressed object path has different bytes", 5)
                if staging.exists():
                    if local_resources._hash(staging) != (digest, size):
                        raise PmtError("host_resource_stage_conflict", "Staged resource bytes do not match journal", 5)
                    try:
                        staging.unlink(missing_ok=True)
                    except OSError:
                        # A complete, hash-verified final object is sufficient; a leftover stage
                        # remains journaled and can be removed on a later recovery.
                        pass
                return final
            if not staging.is_file():
                raise PmtError("host_resource_stage_missing", "No matching staged or published resource exists", 4, True,
                               {"request_id": request_id, "sha256": digest})
            if local_resources._hash(staging) != (digest, size):
                raise PmtError("host_resource_stage_conflict", "Staged resource bytes do not match journal", 5)
            try:
                os.link(staging, final)
            except FileExistsError:
                if not final.is_file() or local_resources._hash(final) != (digest, size):
                    raise PmtError("host_resource_publish_conflict", "Content-addressed target exists with different bytes", 5)
            except OSError as exc:
                if getattr(exc, "winerror", None) not in {32, 33}:
                    raise PmtError("resource_io_error", "Resource object publication failed", 4, True,
                                   {"operation": "content_addressed_link", "exception_type": type(exc).__name__,
                                    "winerror": getattr(exc, "winerror", None)}) from exc
                if attempt < len(delays):
                    time.sleep(delays[attempt])
                    continue
                raise PmtError("resource_io_unknown", "Resource publication remains blocked by a Windows file-sharing lock", 4, True,
                               {"operation": "content_addressed_link", "attempts": len(delays) + 1,
                                "request_id": request_id, "sha256": digest, "size_bytes": size}) from exc
            if not final.is_file() or local_resources._hash(final) != (digest, size):
                raise PmtError("host_resource_hash_mismatch", "Published resource failed hash verification", 5)
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                # The final link is verified and the active journal permits safe cleanup later.
                pass
            return final
        raise PmtError("resource_io_unknown", "Resource publication did not reach a final state", 4, True,
                       {"operation": "content_addressed_link", "request_id": request_id, "sha256": digest})

    def _private_binding(self, conn, principal, scope_id, step_id, run_id, directive_version):
        if not all((step_id, run_id)) or type(directive_version) is not int:
            raise PmtError("private_resource_binding_required", "Private directive access requires current run and directive binding", 3)
        principal.require("runtime")
        identifier(step_id, "step_id"); identifier(run_id, "run_id")
        from ..execution import service as execution
        from ..efficiency.storage import Phase3Storage
        from ..efficiency.batch import binding_for_run
        run = execution._get_run(conn, run_id)
        if (run["step_id"] != step_id or run["owner_session"] != principal.session_id
                or run["state"] not in {"starting", "running", "review_pending", "reconciling", "cancel_requested"}
                or run["directive_version"] != directive_version):
            raise PmtError("private_resource_forbidden", "Current owner, active run or pinned directive version does not match", 3)
        step = execution._step(conn, step_id)
        execution._validate_current_step(conn, run, step)
        if step["scope_id"] != scope_id or step["directive_version"] != directive_version:
            raise PmtError("private_resource_forbidden", "Current Step scope or directive version does not match", 3)

        # Reuse the existing P2 workspace/Step authorization path. It validates
        # current run ownership and either its scope locks or the explicit F9
        # parent/child grant; a claims row on the Step itself is not authority.
        read_req = {"operation": "read_step_directive", "actor": principal.actor,
            "session_id": principal.session_id, "scope_id": scope_id, "record_id": step_id,
            "payload": {"run_id": run_id}}
        from ..steps import handle as steps_handle
        steps_handle(self.db, conn, read_req)

        binding = binding_for_run(conn, run_id)
        context_ref, source_hash = None, None
        if binding:
            body = binding["body"]
            if (binding["row"]["owner_actor"] != principal.actor
                    or binding["row"]["owner_session"] != principal.session_id
                    or body.get("status") not in {"prepared", "running", "cancel_requested", "reconciling", "review_pending"}
                    or body.get("scope_id") != scope_id):
                raise PmtError("private_resource_forbidden", "Current F9 binding owner or scope does not match", 3)
            member = next((item for item in body.get("members", []) if item.get("run_id") == run_id), None)
            if (not member or member.get("step_id") != step_id
                    or member.get("directive_sha256") != conn.execute(
                        "SELECT sha256 FROM artifacts WHERE id=? AND state='ready'", (step["directive_id"],)).fetchone()[0]):
                raise PmtError("private_resource_forbidden", "Current F9 member directive binding does not match", 3)
            context_ref, source_hash = member.get("context_ref"), body.get("source_hash")
        else:
            control = Phase3Storage(self.db).get_object("execution_control", run_id, scope_id,
                principal.actor, principal.session_id, conn=conn)
            if not control:
                raise PmtError("private_resource_forbidden", "Current F8 control/source binding is required", 3)
            control_body = control.get("body") or {}
            if control_body.get("stage") not in {"main_action_pending", "cancel_action_pending", "running",
                    "cancel_wait", "result_review_pending", "result_available", "reconcile_required"}:
                raise PmtError("private_resource_forbidden", "F8 control is not at an authorized runtime stage", 3)
            context_ref, source_hash = control_body.get("context_ref"), control_body.get("source_hash")
        if not isinstance(context_ref, dict) or not isinstance(context_ref.get("id"), str) or not isinstance(source_hash, str):
            raise PmtError("private_resource_forbidden", "A current source-bound F5 context is required", 3)
        context = Phase3Storage(self.db).get_object("task_context", context_ref["id"], scope_id,
            principal.actor, principal.session_id, conn=conn)
        if (not context or context.get("state") != "ready"
                or context.get("source_hash") != source_hash
                or context.get("body", {}).get("projection_hash") != context_ref.get("projection_hash")
                or context.get("body", {}).get("task_ref", {}).get("run_id") != run_id
                or context.get("body", {}).get("task_ref", {}).get("step_id") != step_id):
            raise PmtError("private_resource_stale", "Current owner/source-bound F5 context does not match", 3)
        return step["directive_id"]

    def publish(self, request: dict, headers: dict) -> dict:
        """Stage and publish bytes under server-owned paths, then commit metadata."""
        if not isinstance(request, dict):
            raise PmtError("host_input_invalid", "Resource request must be an object")
        required = {"request_id", "scope_id", "purpose", "content"}
        allowed = required | {"sha256", "size", "private_step_id", "run_id", "directive_version"}
        if set(request) - allowed or not required.issubset(request):
            raise PmtError("host_input_invalid", "Resource request fields are invalid")
        request_id = _canonical_request_id(request["request_id"])
        scope_id = identifier(request["scope_id"], "scope_id")
        purpose = request["purpose"]
        if not isinstance(purpose, str) or purpose not in PURPOSES:
            raise PmtError("host_resource_purpose_invalid", "Resource purpose is unsupported")
        content = request["content"]
        if not isinstance(content, bytes) or len(content) > self.max_bytes:
            raise PmtError("host_resource_size_limit", "Resource must be bytes within the configured upload limit")
        digest = _hash_bytes(content)
        claimed_size = request.get("size", len(content))
        claimed_hash = request.get("sha256", digest)
        if (type(claimed_size) is not int or claimed_size != len(content)
                or not isinstance(claimed_hash, str) or claimed_hash != digest):
            raise PmtError("host_resource_hash_mismatch", "Provided resource size or hash does not match bytes")
        private_step_id, run_id = request.get("private_step_id"), request.get("run_id")
        directive_version = request.get("directive_version")
        if purpose == "step_directive":
            raise PmtError("private_resource_publish_forbidden", "Step directives use the versioned Step publisher")
        elif any(value is not None for value in (private_step_id, run_id, directive_version)):
            raise PmtError("host_input_invalid", "Private binding fields are only valid for step directives")
        fingerprint_body = {"scope_id": scope_id, "purpose": purpose, "sha256": digest,
            "size": len(content), "private_step_id": private_step_id, "run_id": run_id,
            "directive_version": directive_version}
        request_fingerprint = hashlib.sha256(canonical_json(fingerprint_body).encode("utf-8")).hexdigest()
        now = utc_now()
        staging_rel = (Path("resources") / ".staging" / f"host-{request_id}.part").as_posix()
        artifact_id = new_id()
        with self.db.write() as conn:
            principal = self._principal(conn, headers)
            self._authorize_scope(conn, principal, scope_id)
            private_hash = conn.execute("SELECT 1 FROM step_specs ss JOIN records r ON r.id=ss.step_id "
                "JOIN artifacts a ON a.id=ss.directive_id WHERE r.scope_id=? AND a.sha256=? AND a.state='ready' LIMIT 1",
                (scope_id, digest)).fetchone()
            if private_hash:
                raise PmtError("host_resource_private_downgrade", "Bytes matching a private Step directive cannot be republished as a generic resource", 3)
            old = conn.execute("SELECT * FROM host_resource_requests WHERE request_id=?", (request_id,)).fetchone()
            if old:
                if (old["actor"] != principal.actor or old["session_id"] != principal.session_id
                        or old["device_id"] != principal.device_id
                        or old["fingerprint"] != request_fingerprint):
                    raise PmtError("request_conflict", "request_id was used for another resource body or owner", 3)
                cached = json.loads(old["response_json"])
                cached_id = cached.get("artifact_ref", {}).get("id") if isinstance(cached, dict) else None
                valid = local_resources.check_artifact(self.db, conn, cached_id) if isinstance(cached_id, str) else {"valid": False}
                if not valid["valid"]:
                    raise PmtError("host_resource_corrupt", "Replayed resource failed integrity verification", 5)
                return cached
            pending = conn.execute("SELECT * FROM host_resource_journal WHERE request_id=?", (request_id,)).fetchone()
            if pending and (pending["actor"] != principal.actor or pending["session_id"] != principal.session_id
                    or pending["device_id"] != principal.device_id or pending["sha256"] != digest
                    or pending["scope_id"] != scope_id or pending["purpose"] != purpose
                    or pending["fingerprint"] != request_fingerprint):
                raise PmtError("request_conflict", "Pending resource journal does not match this request", 3)
            if not pending:
                conn.execute("INSERT INTO host_resource_journal VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (request_id, principal.actor, principal.session_id or "", principal.device_id,
                     scope_id, purpose, private_step_id, run_id, directive_version, digest,
                     len(content), staging_rel, artifact_id, request_fingerprint, "authorized", now, now))
            else:
                artifact_id = pending["artifact_id"]
        staging = local_resources._artifact_path(self.db, staging_rel)
        final_rel = (Path("resources") / "objects" / digest).as_posix()
        final = local_resources._artifact_path(self.db, final_rel)
        try:
            staging.parent.mkdir(parents=True, exist_ok=True)
            final.parent.mkdir(parents=True, exist_ok=True)
            local_resources._reject_links(staging.parent)
            if staging.exists():
                if local_resources._hash(staging) != (digest, len(content)):
                    raise PmtError("host_resource_stage_conflict", "Staged resource bytes do not match journal", 5)
            else:
                with staging.open("xb") as stream:
                    stream.write(content); stream.flush(); os.fsync(stream.fileno())
            with self.db.write() as conn:
                conn.execute("UPDATE host_resource_journal SET state='staged',updated_at=? WHERE request_id=?", (utc_now(), request_id))
            final = self._publish_staged(request_id, scope_id, staging_rel, final_rel, digest, len(content), headers)
            with self.db.write() as conn:
                principal = self._principal(conn, headers)
                self._authorize_scope(conn, principal, scope_id)
                old = conn.execute("SELECT fingerprint,response_json FROM host_resource_requests WHERE request_id=?", (request_id,)).fetchone()
                if old:
                    if old[0] != request_fingerprint:
                        raise PmtError("request_conflict", "request_id was used for another resource body", 3)
                    return json.loads(old[1])
                existing = conn.execute("SELECT id,state FROM artifacts WHERE scope_id=? AND sha256=? AND relative_path=?",
                    (scope_id, digest, final_rel)).fetchone()
                if existing:
                    if existing["state"] != "ready":
                        raise PmtError("host_resource_unavailable", "Matching content-addressed object is not ready", 3)
                    artifact_id = existing["id"]
                    metadata = conn.execute("SELECT purpose FROM host_resource_metadata WHERE artifact_id=?", (artifact_id,)).fetchone()
                    if metadata and metadata[0] != purpose:
                        raise PmtError("host_resource_purpose_conflict", "Identical scoped bytes are already registered for another purpose", 3)
                else:
                    conn.execute("INSERT OR IGNORE INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,retention_until,created_at) "
                        "VALUES(?,?,?,?,?,'ready',NULL,?)", (artifact_id, scope_id, digest, len(content), final_rel, now))
                    existing = conn.execute("SELECT id,state FROM artifacts WHERE scope_id=? AND sha256=? AND relative_path=?",
                        (scope_id, digest, final_rel)).fetchone()
                    artifact_id = existing["id"]
                if not conn.execute("SELECT 1 FROM host_resource_metadata WHERE artifact_id=?", (artifact_id,)).fetchone():
                    conn.execute("INSERT INTO host_resource_metadata VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (artifact_id, scope_id, purpose, private_step_id, run_id, directive_version,
                         principal.device_id, principal.session_id or "", request_id, now))
                response = {"artifact_ref": {"id": artifact_id, "sha256": digest, "size": len(content),
                    "scope_id": scope_id, "purpose": purpose},
                    "receipt_ref": {"request_id": request_id, "state": "published"}}
                conn.execute("INSERT INTO host_resource_requests VALUES(?,?,?,?,?,?,?)",
                    (request_id, principal.actor, principal.session_id or "", principal.device_id,
                     request_fingerprint, canonical_json(response), now))
                conn.execute("UPDATE host_resource_journal SET state='committed',updated_at=? WHERE request_id=?", (utc_now(), request_id))
                conn.execute("UPDATE host_resource_journal SET artifact_id=? WHERE request_id=?", (artifact_id, request_id))
            try:
                staging.unlink(missing_ok=True)
            except OSError:
                # A verified content-addressed final object and committed receipt are authoritative.
                pass
            return response
        except (OSError, PmtError):
            # Keep the journal and any staged bytes for an authenticated replay/recovery.
            raise

    def read(self, request: dict, headers: dict) -> dict:
        if not isinstance(request, dict) or set(request) - {"resource_id", "run_id"} or "resource_id" not in request:
            raise PmtError("host_input_invalid", "Resource read fields are invalid")
        artifact_id = identifier(request["resource_id"], "resource_id")
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            try:
                principal = self._principal(conn, headers)
                principal.require("read")
                row = conn.execute("SELECT m.*,a.sha256,a.size_bytes,a.relative_path,a.state FROM host_resource_metadata m "
                    "JOIN artifacts a ON a.id=m.artifact_id WHERE m.artifact_id=?", (artifact_id,)).fetchone()
                current_private = conn.execute("SELECT 1 FROM step_specs WHERE directive_id=? LIMIT 1", (artifact_id,)).fetchone()
                if row is None or current_private:
                    private = conn.execute("SELECT a.id AS artifact_id,a.scope_id,ss.directive_version,r.id AS private_step_id,"
                        "e.id AS run_id,'step_directive' AS purpose,'ready' AS state,a.sha256,a.size_bytes,a.relative_path "
                        "FROM artifacts a JOIN step_specs ss ON ss.directive_id=a.id JOIN records r ON r.id=ss.step_id "
                        "JOIN execution_runs e ON e.step_id=ss.step_id WHERE a.id=? AND a.state='ready' LIMIT 1",
                        (artifact_id,)).fetchone()
                    row = private
                if not row or row["state"] != "ready":
                    raise PmtError("host_resource_unavailable", "Resource is absent or unavailable", 3)
                if type(row["size_bytes"]) is not int or row["size_bytes"] > self.max_bytes:
                    raise PmtError("host_resource_size_limit", "Stored resource exceeds the read limit", 5)
                self.auth.authorize_scope(conn, principal, row["scope_id"])
                if row["purpose"] == "step_directive":
                    principal.require("runtime")
                    run_id = request.get("run_id")
                    run = conn.execute("SELECT step_id,directive_version FROM execution_runs WHERE id=?", (run_id,)).fetchone() if run_id else None
                    spec_artifact = self._private_binding(conn, principal, row["scope_id"], run[0] if run else None,
                        run_id, run[1] if run else None)
                    if spec_artifact != artifact_id:
                        raise PmtError("private_resource_stale", "Resource is not the current pinned directive", 3)
                elif request.get("run_id") is not None:
                    raise PmtError("host_input_invalid", "run_id is only accepted for private Step resources")
                path = local_resources._artifact_path(self.db, row["relative_path"])
                verified = local_resources.check_artifact(self.db, conn, artifact_id)
                if not verified["valid"]:
                    raise PmtError("host_resource_corrupt", "Stored resource failed integrity verification", 5)
                raw = path.read_bytes()
                if len(raw) != row["size_bytes"] or _hash_bytes(raw) != row["sha256"]:
                    raise PmtError("host_resource_corrupt", "Stored resource failed integrity verification", 5)
                return {"content": raw, "sha256": row["sha256"], "size": row["size_bytes"],
                    "purpose": row["purpose"], "scope_id": row["scope_id"], "resource_id": artifact_id}
            finally:
                conn.rollback()

    def recover(self, request_id: str, headers: dict) -> dict:
        """Finish a previously journaled publish after rechecking current authority."""
        _canonical_request_id(request_id)
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            try:
                principal = self._principal(conn, headers)
                old = conn.execute("SELECT * FROM host_resource_requests WHERE request_id=?", (request_id,)).fetchone()
                if old:
                    if (old["actor"] != principal.actor or old["session_id"] != principal.session_id
                            or old["device_id"] != principal.device_id):
                        raise PmtError("request_owner_mismatch", "Resource receipt belongs to another current owner", 3)
                    self._authorize_scope(conn, principal, json.loads(old["response_json"])["artifact_ref"]["scope_id"])
                    cached = json.loads(old["response_json"])
                    cached_id = cached["artifact_ref"]["id"]
                    if not local_resources.check_artifact(self.db, conn, cached_id)["valid"]:
                        raise PmtError("host_resource_corrupt", "Replayed resource failed integrity verification", 5)
                    return cached
                row = conn.execute("SELECT * FROM host_resource_journal WHERE request_id=?", (request_id,)).fetchone()
                if not row or row["device_id"] != principal.device_id or row["actor"] != principal.actor or row["session_id"] != (principal.session_id or ""):
                    raise PmtError("request_owner_mismatch", "Resource journal belongs to another current owner", 3)
                self._authorize_scope(conn, principal, row["scope_id"])
                if row["purpose"] == "step_directive":
                    artifact = self._private_binding(conn, principal, row["scope_id"], row["private_step_id"], row["run_id"], row["directive_version"])
                    if artifact is None:
                        raise PmtError("private_resource_stale", "Current private directive changed", 3)
            finally:
                conn.rollback()
        final_rel = (Path("resources") / "objects" / row["sha256"]).as_posix()
        final = local_resources._artifact_path(self.db, final_rel)
        final.parent.mkdir(parents=True, exist_ok=True)
        self._publish_staged(request_id, row["scope_id"], row["staging_path"], final_rel,
                             row["sha256"], row["size_bytes"], headers)
        with self.db.write() as conn:
            principal = self._principal(conn, headers)
            self._authorize_scope(conn, principal, row["scope_id"])
            if row["purpose"] == "step_directive":
                self._private_binding(conn, principal, row["scope_id"], row["private_step_id"], row["run_id"], row["directive_version"])
            existing = conn.execute("SELECT id,state FROM artifacts WHERE scope_id=? AND sha256=? AND relative_path=?",
                (row["scope_id"], row["sha256"], final_rel)).fetchone()
            artifact_id = existing["id"] if existing else row["artifact_id"]
            if not existing:
                conn.execute("INSERT OR IGNORE INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,retention_until,created_at) "
                    "VALUES(?,?,?,?,?,'ready',NULL,?)", (artifact_id, row["scope_id"], row["sha256"], row["size_bytes"], final_rel, utc_now()))
                existing = conn.execute("SELECT id,state FROM artifacts WHERE scope_id=? AND sha256=? AND relative_path=?",
                    (row["scope_id"], row["sha256"], final_rel)).fetchone()
                artifact_id = existing["id"]
            if existing["state"] != "ready":
                raise PmtError("host_resource_unavailable", "Matching content-addressed object is not ready", 3)
            metadata = conn.execute("SELECT purpose FROM host_resource_metadata WHERE artifact_id=?", (artifact_id,)).fetchone()
            if metadata and metadata[0] != row["purpose"]:
                raise PmtError("host_resource_purpose_conflict", "Identical scoped bytes are already registered for another purpose", 3)
            if not metadata:
                conn.execute("INSERT INTO host_resource_metadata VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (artifact_id, row["scope_id"], row["purpose"], row["private_step_id"], row["run_id"],
                     row["directive_version"], principal.device_id, principal.session_id or "", request_id, utc_now()))
            response = {"artifact_ref": {"id": artifact_id, "sha256": row["sha256"],
                "size": row["size_bytes"], "scope_id": row["scope_id"], "purpose": row["purpose"]},
                "receipt_ref": {"request_id": request_id, "state": "published"}}
            conn.execute("INSERT OR IGNORE INTO host_resource_requests VALUES(?,?,?,?,?,?,?)",
                (request_id, principal.actor, principal.session_id or "", principal.device_id,
                 row["fingerprint"], canonical_json(response), utc_now()))
            conn.execute("UPDATE host_resource_journal SET state='committed',updated_at=? WHERE request_id=?", (utc_now(), request_id))
            conn.execute("UPDATE host_resource_journal SET artifact_id=? WHERE request_id=?", (artifact_id, request_id))
        return response
