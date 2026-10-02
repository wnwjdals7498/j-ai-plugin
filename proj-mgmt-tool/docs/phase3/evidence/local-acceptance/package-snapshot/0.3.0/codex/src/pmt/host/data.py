"""Authenticated Host adapters for client-captured source and verification data.

The Host persists client snapshots and applies existing PMT graph/context/
verification rules. It never inspects a client checkout, invokes Git, or infers
that a client-side execution passed.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from contextlib import closing, contextmanager
from pathlib import PurePosixPath

from ..errors import PmtError
from ..efficiency.source import pin_source, verify_source_pin
from ..efficiency.storage import Phase3Storage
from ..phase2_common import project_scope_id, require_workspace_claim, validate_scope
from ..planning.graph import validate_graph
from ..util import canonical_json, fingerprint, strict_json_loads, utc_now
from ..workspace import canonical_workspace
from .host_contract import (FILE_OPERATIONS, HOST_DATA_OPERATIONS, READ_OPERATIONS,
                            WRITE_OPERATIONS)
from .host_contract import BATCH_OPERATIONS, LOCAL_FILE_EFFECT_OPERATIONS, PLAN_METADATA_OPERATIONS

_HOST_ACTOR = "pmt.host.snapshot"
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_WORKSPACE_URI = re.compile(r"^pmt://([0-9a-f-]{36})/([0-9a-f]{64})$")
_ACTIVE_RUNS = {"starting", "running", "review_pending", "reconciling", "cancel_requested"}
_SOURCE_KIND = "host_source_current"
_VERIFICATION_KIND = "host_verification_current"
_RESOURCE_LIMIT = 8 * 1024 * 1024


def _fail(code, message, exit_code=3, details=None, retryable=False):
    raise PmtError(code, message, exit_code, retryable, details)


def _uuid(value, name):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("host_input_invalid", f"{name} must be a canonical UUID") from exc
    return value


def _relative(value, name):
    if not isinstance(value, str) or not value or "\\" in value or value.startswith("-"):
        raise PmtError("host_input_invalid", f"{name} must be a workspace-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PmtError("host_input_invalid", f"{name} must remain inside the workspace")
    return path.as_posix()


def _branch_key(pin):
    if pin.source_kind == "git":
        return pin.selected_ref if pin.selected_ref is not None else "detached:" + pin.reviewed_commit
    if pin.source_kind == "non_git":
        return "non-git"
    _fail("source_kind_unknown", "Unknown SourcePin cannot identify a Host snapshot", 3)


def _system_session(namespace_id):
    return "namespace:" + namespace_id


def _pointer_id(kind, canonical_ref, project_id=None, relative_graph_path=None):
    parts = [kind, canonical_ref]
    if project_id is not None:
        parts.extend([project_id, relative_graph_path])
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


class HostDatabaseView:
    """Per-request trusted adapter around Host SQLite, never a wire-selected port."""

    def __init__(self, extension, db, principal, req, headers, authorizer=None):
        self._db = db
        self._extension = extension
        self._principal = principal
        self._req = req
        self._headers = dict(headers)
        self._authorizer = authorizer
        self.environment_id = principal.environment_id
        self.source_repository = HostSourceRepository(extension, principal, req, headers, authorizer)
        self.workspace_context_port = self.source_repository
        self.verification_snapshot_provider = HostVerificationSnapshotProvider(
            extension, self.source_repository, principal, req, headers, authorizer)

    def __getattr__(self, name):
        return getattr(self._db, name)

    def _reauthorize(self, conn, req):
        if self._authorizer is not None:
            return self._authorizer(conn, req, self._headers, record_ledger=False)
        return self._extension._fresh_authorize(conn, req, self._headers)

    @contextmanager
    def write(self, *args, **kwargs):
        with self._db.write(*args, **kwargs) as conn:
            self._reauthorize(conn, self._req)
            yield conn

    def run_request(self, request, handler, maintenance_owner=None, *, authorize=None):
        def check(conn, current):
            if self._authorizer is not None:
                self._authorizer(conn, current, self._headers, record_ledger=True)
            else:
                self._reauthorize(conn, current)
            if authorize is not None:
                authorize(conn, current)
        return self._db.run_request(request, handler, maintenance_owner, authorize=check)


class HostSourceRepository:
    """Trusted server-side bridge from a canonical URI to a pinned Host snapshot."""

    def __init__(self, extension, principal, req, headers, authorizer=None):
        self.extension, self.principal, self.req = extension, principal, req
        self.headers = dict(headers)
        self.authorizer = authorizer

    def _fresh_principal(self, conn, req):
        if self.authorizer is not None:
            auth_request = req
            # read_task_context narrows its payload to `context_ref` before the
            # shared F5 reader sees it. Reuse the exact outer wire request kept
            # in this per-request view so its caller-supplied run revision is
            # reauthorized on this same current connection.
            if (req.get("operation") == "read_task_context"
                    and isinstance(self.req, dict)
                    and self.req.get("operation") == "read_task_context"
                    and self.req.get("request_id") == req.get("request_id")
                    and self.req.get("actor") == req.get("actor")
                    and self.req.get("session_id") == req.get("session_id")
                    and isinstance(self.req.get("payload"), dict)
                    and self.req["payload"].get("expected_run_revision") is not None):
                auth_request = self.req
            actual, _scopes = self.authorizer(conn, auth_request, self.headers, record_ledger=False)
            return actual
        return self.extension._fresh_authorize(conn, req, self.headers)[0]

    def scope_context(self, conn, req):
        principal = self._fresh_principal(conn, req)
        binding = self.extension._workspace_binding(conn, req, principal, mode="execute")
        return {"scope_id": binding["project_id"], "repository_id": binding["repository_id"],
                "workspace": binding["canonical_workspace"],
                "relative_path": binding["relative_graph_path"], "graph_path": None,
                "project_body": binding["project_body"], "repository_body": binding["repository_body"],
                "project_parent_id": binding["project_parent_id"]}

    def capture(self, conn, req):
        principal = self._fresh_principal(conn, req)
        binding = self.extension._workspace_binding(conn, req, principal, mode="execute")
        pointer = self.extension._current_source_pointer(conn, binding)
        expected = (req.get("payload") or {}).get("expected_source")
        if expected is not None:
            verify_source_pin(expected, pointer["body"]["source_pin"])
        resource = self.extension._read_resource(pointer["body"]["graph_resource"],
                                                 binding["project_id"], self.headers)
        source = self.extension._validated_graph_resource(resource, pointer["body"], binding)
        return source | {"scope_id": binding["project_id"], "repository_id": binding["repository_id"],
                         "workspace": binding["canonical_workspace"],
                         "relative_path": binding["relative_graph_path"], "graph_path": None,
                         "repo_root": None, "git_relative_path": binding["relative_graph_path"],
                         "project_body": binding["project_body"], "repository_body": binding["repository_body"],
                         "project_parent_id": binding["project_parent_id"], "run": binding["run"],
                         "source_provenance": "client_snapshot", "host_git_verified": False,
                         "snapshot_revision": pointer["revision"]}

    def authorize_context(self, conn, req, *, task_ref, repository_id, relative_graph_path,
                          expected_source, workspace):
        principal = self._fresh_principal(conn, req)
        payload = dict(req.get("payload") or {})
        run_row = conn.execute("SELECT revision FROM execution_runs WHERE id=?",
                               (task_ref.get("run_id"),)).fetchone()
        if run_row is None:
            _fail("workspace_authority_stale", "Current Host run is unavailable", 3)
        payload.update({"task_ref": task_ref, "repository_id": repository_id,
                        "relative_graph_path": relative_graph_path,
                        "canonical_workspace": workspace, "run_id": task_ref.get("run_id"),
                        "mode": "execute", "expected_run_revision": run_row["revision"],
                        "expected_source": expected_source})
        projected = dict(req) | {"payload": payload}
        binding = self.extension._workspace_binding(conn, projected, principal, mode="execute")
        pointer = self.extension._current_source_pointer(conn, binding)
        resource = self.extension._read_resource(pointer["body"]["graph_resource"],
                                                 binding["project_id"], self.headers)
        source = self.extension._validated_graph_resource(resource, pointer["body"], binding)
        expected = pin_source(expected_source)
        verify_source_pin(expected, source["source_pin"])
        from ..execution.service import _step
        step = _step(conn, task_ref["step_id"])
        if step["id"] != binding["run"]["step_id"]:
            _fail("context_authority_mismatch", "Active Host run does not own the requested Step", 3)
        return {"source": source | {"scope_id": binding["project_id"],
                                   "repository_id": binding["repository_id"],
                                   "workspace": binding["canonical_workspace"],
                                   "relative_path": binding["relative_graph_path"],
                                   "graph_path": None, "repo_root": None,
                                   "git_relative_path": binding["relative_graph_path"],
                                   "project_body": binding["project_body"],
                                   "repository_body": binding["repository_body"],
                                   "project_parent_id": binding["project_parent_id"],
                                   "run": binding["run"], "source_provenance": "client_snapshot",
                                   "host_git_verified": False,
                                   "snapshot_revision": pointer["revision"]},
                "run": binding["run"], "step": step, "scope_id": binding["project_id"],
                "repository_id": binding["repository_id"],
                "relative_graph_path": binding["relative_graph_path"],
                "workspace": binding["canonical_workspace"], "task_ref": dict(task_ref),
                "scope_locks": self.extension._current_locks(conn, binding["run"]["id"])}


class HostVerificationSnapshotProvider:
    """Supply exact, client-captured verification inventory; never scans Host paths."""

    def __init__(self, extension, source_repository, principal, req, headers, authorizer=None):
        self.extension, self.source_repository = extension, source_repository
        self.principal, self.req, self.headers = principal, req, dict(headers)
        self.authorizer = authorizer

    def snapshot(self, conn, target_id, definition_id, definition_version, command,
                 inputs_fingerprint, record, scope, body, criterion_hashes):
        from ..verification import _verification_scope_id
        scope_id = _verification_scope_id(conn, scope)
        if self.authorizer is not None:
            self.principal, _scopes = self.authorizer(conn, self.req, self.headers, record_ledger=False)
        else:
            self.principal, _scopes = self.extension._fresh_authorize(conn, self.req, self.headers)
        payload = self.req.get("payload", {})
        source_ref = payload.get("verification_snapshot_ref")
        current = self.source_repository.capture(conn, self.req)
        if source_ref is None:
            pointer_id = self.extension._verification_pointer_id(scope_id, definition_id,
                definition_version, self.principal.environment_id, current["workspace"])
            stored = self.extension._get_internal_object(conn, _VERIFICATION_KIND, pointer_id)
            if stored:
                source_ref = dict(stored["body"].get("verification_resource", {})) | {
                    "kind": "verification_snapshot", "revision": stored["revision"]}
        required_ref = {"kind", "id", "sha256", "size", "scope_id", "purpose", "revision"}
        if not isinstance(source_ref, Mapping) or set(source_ref) != required_ref:
            snapshot = {"fingerprint_version": 1, "definition_id": definition_id,
                "definition_version": definition_version, "verification_scope_id": scope_id,
                "workspace_identity_sha256": None, "environment_id": self.principal.environment_id,
                "command": command, "inputs_sha256": inputs_fingerprint,
                "criteria": criterion_hashes, "workspace_files": [], "workspace_known": False,
                "runtime": {}, "dependency_manifests": {}, "configuration_hashes": {},
                "source_provenance": "client_snapshot_missing"}
            reasons = ["client_verification_snapshot_missing"]
            return snapshot, fingerprint(snapshot), reasons, criterion_hashes, scope["id"]
        if source_ref.get("kind") != "verification_snapshot" or type(source_ref.get("revision")) is not int:
            _fail("verification_snapshot_ref_invalid", "Verification snapshot reference is invalid", 3)
        requested_workspace = payload.get("canonical_workspace", payload.get("workspace"))
        if requested_workspace is not None and requested_workspace != current["workspace"]:
            _fail("verification_workspace_mismatch", "Verification snapshot does not match current workspace grant", 3)
        expected_pin = payload.get("expected_source")
        if expected_pin is not None:
            verify_source_pin(expected_pin, current["source_pin"])
        stored = self.extension._get_internal_object(conn, _VERIFICATION_KIND,
            self.extension._verification_pointer_id(scope_id, definition_id, definition_version,
                self.principal.environment_id, current["workspace"]))
        if (not stored or stored["revision"] != source_ref["revision"]
                or stored["body"].get("verification_resource", {}).get("id") != source_ref["id"]
                or stored["body"].get("verification_resource", {}).get("sha256") != source_ref["sha256"]):
            _fail("verification_snapshot_stale", "Client verification snapshot is no longer current", 3)
        resource = self.extension._read_resource(source_ref, scope_id, self.headers)
        if resource.get("purpose") != "verification_snapshot":
            _fail("verification_snapshot_ref_invalid", "Resource purpose is not a verification snapshot", 3)
        try:
            manifest_body = strict_json_loads(resource["content"], max_bytes=_RESOURCE_LIMIT)
        except PmtError as exc:
            raise PmtError("verification_snapshot_invalid", "Client verification manifest is invalid JSON", 3) from exc
        manifest = self.extension._validate_verification_manifest(manifest_body, source_ref, current, self.principal,
            target_id, definition_id, definition_version, command, inputs_fingerprint,
            criterion_hashes, conn, request=self.req)
        snapshot = {"fingerprint_version": 1, "definition_id": definition_id,
            "definition_version": definition_version, "verification_scope_id": scope_id,
            "workspace_identity_sha256": fingerprint(current["workspace"]),
            "environment_id": self.principal.environment_id, "command": command,
            "inputs_sha256": inputs_fingerprint or manifest["inputs_sha256"], "criteria": criterion_hashes,
            "workspace_files": manifest["workspace_files"],
            "workspace_known": manifest["inventory_status"] == "complete",
            "runtime": manifest["runtime"], "dependency_manifests": manifest["dependency_manifests"],
            "configuration_hashes": manifest["configuration_hashes"],
            "source_snapshot_hash": current["source_pin"].source_hash,
            "verification_snapshot_sha256": source_ref["sha256"],
            "source_provenance": "client_snapshot"}
        reasons = list(manifest.get("snapshot_reasons", []))
        return snapshot, fingerprint(snapshot), reasons, criterion_hashes, scope["id"]


class HostDataExtension:
    """HostApplication extension using only server-authenticated scope/run state."""

    operations = HOST_DATA_OPERATIONS
    read_operations = READ_OPERATIONS
    file_operations = FILE_OPERATIONS
    write_operations = WRITE_OPERATIONS

    def __init__(self, db, auth, resource_port, *, authorizer=None):
        self._db = db
        self.auth = auth
        self.resource_port = resource_port
        self.authorizer = authorizer

    def reference_scopes(self, conn, req):
        payload = req.get("payload", {})
        scopes = set()
        for value, name in ((req.get("scope_id"), "scope_id"),
                            (payload.get("project_id"), "project_id"),
                            (payload.get("repository_id"), "repository_id"),
                            (payload.get("target_id"), "target_id"),
                            (payload.get("run_id"), "run_id")):
            if not value:
                continue
            _uuid(value, name)
            if name == "run_id":
                row = conn.execute("SELECT s.scope_id FROM execution_runs e JOIN records s ON s.id=e.step_id WHERE e.id=?",
                                   (value,)).fetchone()
                if row:
                    scopes.add(row[0])
            elif name == "repository_id":
                # Repository identity is checked against the authorized Project
                # mapping below; a Project-only grant does not imply a new
                # grant on its parent Repository scope.
                repository = conn.execute("SELECT kind FROM scopes WHERE id=?", (value,)).fetchone()
                project_id = payload.get("project_id") or req.get("scope_id")
                project = conn.execute("SELECT parent_id,body_json FROM scopes WHERE id=? AND kind='project'",
                                       (project_id,)).fetchone() if project_id else None
                if not repository or repository[0] != "repository" or not project:
                    raise PmtError("repository_scope_mismatch", "Project and repository scopes are required", 3)
                body = json.loads(project["body_json"] or "{}")
                bound = body.get("repository_id", body.get("repository_scope_id"))
                if project["parent_id"] not in {None, value} or (bound and bound != value) \
                        or (project["parent_id"] is None and bound != value):
                    raise PmtError("repository_scope_mismatch", "Repository does not match the authorized Project mapping", 3)
            elif name == "target_id":
                row = conn.execute("SELECT scope_id FROM records WHERE id=? UNION SELECT id FROM scopes WHERE id=?",
                                   (value, value)).fetchone()
                if row:
                    scopes.add(row[0])
            else:
                row = conn.execute("SELECT id FROM scopes WHERE id=?", (value,)).fetchone()
                if row:
                    scopes.add(row[0])
        for item in payload.get("task_ref", {}).values() if isinstance(payload.get("task_ref"), dict) else ():
            if isinstance(item, str):
                row = conn.execute("SELECT scope_id FROM records WHERE id=?", (item,)).fetchone()
                if row:
                    scopes.add(row[0])
        return scopes

    def _fresh_authorize(self, conn, req, headers):
        """Authenticate current headers again before a nested P2/F0 request replay or write."""
        authorization = headers.get("authorization", headers.get("Authorization", ""))
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            _fail("unauthenticated", "A registered Host device credential is required", 3)
        principal = self.auth.authenticate(conn, authorization[7:],
            headers.get("x-pmt-device", headers.get("X-PMT-Device", "")),
            headers.get("x-pmt-namespace", headers.get("X-PMT-Namespace", "")),
            session_id=headers.get("x-pmt-session", headers.get("X-PMT-Session")),
            environment_id=headers.get("x-pmt-environment", headers.get("X-PMT-Environment")))
        if req.get("actor") != principal.actor or req.get("session_id") != principal.session_id:
            _fail("unauthenticated", "Request body identity no longer matches the current Host session", 3)
        principal.require("read" if req.get("operation") in READ_OPERATIONS else "write")
        scopes = self.reference_scopes(conn, req)
        if not scopes:
            _fail("scope_forbidden", "An explicit current Host scope is required", 3)
        for scope_id in scopes:
            self.auth.authorize_scope(conn, principal, scope_id)
        self.authorize(conn, req, principal)
        return principal, scopes

    def _authorize_now(self, conn, req, headers):
        if self.authorizer is not None:
            return self.authorizer(conn, req, headers, record_ledger=False)
        return self._fresh_authorize(conn, req, headers)

    def authorize(self, conn, req, principal):
        op = req.get("operation")
        if op not in self.operations:
            _fail("host_operation_forbidden", "Host data operation is not allowlisted", 3)
        principal.require("read" if op in READ_OPERATIONS else "write")
        payload = req.get("payload", {})
        if op in PLAN_METADATA_OPERATIONS:
            from .plans import authorize as authorize_plan_metadata
            authorize_plan_metadata(self, conn, req, principal)
            return
        if op == "read_source_metadata":
            self._read_source_metadata(conn, req, principal, validate_only=True)
            return
        if op in LOCAL_FILE_EFFECT_OPERATIONS:
            from .file_effects import authorize as authorize_file_effect
            authorize_file_effect(self, conn, req, principal)
            return
        if op == "authorize_workspace":
            mode = payload.get("mode", "execute")
            principal.require("read")
            if mode != "source_capture":
                principal.require("runtime")
        elif op in {"build_task_context", "resume_task_context", "read_task_context", "read_context_detail",
                    "resolve_context_alias", "read_reuse_decision", "read_step_directive",
                    "save_step_directive", "compact_tool_result", "read_tool_result_detail", "record_verification",
                    "lookup_verification", "publish_verification_snapshot"} or op in BATCH_OPERATIONS:
            principal.require("runtime")
        if op in BATCH_OPERATIONS:
            from .batch_state import authorize as authorize_batch
            authorize_batch(self, conn, req, principal, None)
            return
        if op in {"publish_source_snapshot", "read_source_snapshot", "authorize_workspace",
                  "publish_verification_snapshot", "capture_source_pin", "rebuild_graph_index",
                  "register_segment_manifest",
                  "query_graph", "calculate_graph_impact", "build_task_context", "resume_task_context",
                  "read_task_context", "read_context_detail", "resolve_context_alias", "read_reuse_decision",
                  "resolve_reuse", "record_reuse_result", "invalidate_reuse", "record_verification",
                  "lookup_verification", "compact_tool_result", "read_tool_result_detail"}:
            mode = (payload.get("mode", "execute") if op == "authorize_workspace" else
                    "source_capture" if op == "publish_source_snapshot" else "execute")
            self._workspace_binding(conn, req, principal, mode=mode,
                allow_source_capture=(op == "authorize_workspace"))
        elif op in {"save_step_directive", "read_step_directive"}:
            # Existing versioned Step resource handlers do their own active-parent/run checks.
            if req.get("scope_id") is None:
                _fail("scope_forbidden", "A project scope is required", 3)
            principal.require("runtime")
            if op == "save_step_directive":
                item_id = _uuid(payload.get("item_id"), "item_id")
                item = conn.execute("SELECT * FROM records WHERE id=? AND kind='item'", (item_id,)).fetchone()
                if (not item or item["state"] in {"Done", "Canceled"}
                        or project_scope_id(conn, item["scope_id"]) != req["scope_id"]):
                    _fail("step_parent_scope_mismatch", "Current parent Item must be active in this project", 3)
                owner = conn.execute("SELECT owner_session FROM claims WHERE record_id=?", (item_id,)).fetchone()
                if owner and owner[0] != principal.session_id:
                    _fail("ownership_conflict", "Parent Item belongs to another current owner", 3)
                workspace = payload.get("workspace")
                match = _WORKSPACE_URI.fullmatch(workspace) if isinstance(workspace, str) else None
                if not match:
                    _fail("workspace_mapping_invalid", "Host Step metadata requires a canonical workspace URI", 3)
                project = conn.execute("SELECT parent_id,body_json FROM scopes WHERE id=? AND kind='project'",
                                       (req["scope_id"],)).fetchone()
                project_body = json.loads(project["body_json"] or "{}") if project else {}
                repo_id = project["parent_id"] or project_body.get("repository_id") or project_body.get("repository_scope_id")
                if not repo_id or match.group(1) != repo_id:
                    _fail("repository_scope_mismatch", "Step workspace URI does not match its project repository", 3)
                from .plans import authorize_step_directive
                authorize_step_directive(self, conn, req, principal)

    def database_view(self, db, principal, req, headers, authorizer=None):
        return HostDatabaseView(self, db, principal, req, headers, authorizer or self.authorizer)

    def handle(self, db, conn, req, principal):
        op = req["operation"]
        if op == "read_client_plan":
            from .plans import handle as handle_plan_metadata
            return handle_plan_metadata(self, db, conn, req, principal)
        if op == "read_source_metadata":
            return self._read_source_metadata(conn, req, principal,
                                               headers=getattr(db, "_headers", {}))
        if op == "read_local_file_effect":
            from .file_effects import handle as handle_file_effect
            return handle_file_effect(self, db, conn, req, principal, getattr(db, "_headers", {}))
        if op == "authorize_workspace":
            payload = req.get("payload", {})
            return self._workspace_authority(conn, req, principal, payload.get("mode", "execute"))
        if op == "read_step_batch":
            from .batch_state import read as read_batch
            return read_batch(self, db, conn, req, principal, getattr(db, "_headers", {}))
        if op == "read_source_snapshot":
            source = db.source_repository.capture(conn, req)
            return self._source_receipt(source)
        if op in {"capture_source_pin", "query_graph", "calculate_graph_impact",
                  "read_reuse_decision",
                  "read_task_context", "read_context_detail", "resolve_context_alias",
                  "read_tool_result_detail", "record_verification", "lookup_verification"}:
            from ..efficiency import graph, context, reuse, results
            from .. import verification
            module = (graph if op in graph.READ_OPERATIONS else context if op in context.READ_OPERATIONS
                     else reuse if op in reuse.READ_OPERATIONS else results if op in results.READ_OPERATIONS
                     else verification)
            if op == "read_reuse_decision":
                reuse_req = dict(req)
                reuse_payload = req.get("payload", {})
                reuse_req["payload"] = {key: reuse_payload[key] for key in
                    ("body_ref", "run_id", "workspace", "paths") if key in reuse_payload}
                req = reuse_req
            elif op == "read_task_context":
                context_req = dict(req)
                context_payload = req.get("payload", {})
                context_req["payload"] = {"context_ref": context_payload.get("context_ref")}
                req = context_req
            return module.handle(db, conn, req)
        _fail("host_operation_unavailable", "Host operation is not a state-only read/write", 3)

    def execute_file(self, db, req, principal, headers):
        from ..service import response
        view = self.database_view(db, principal, req, headers, self.authorizer)
        try:
            op = req["operation"]
            if op == "publish_client_plan":
                from .plans import execute_file as execute_plan_metadata
                return execute_plan_metadata(self, view, req, principal, headers)
            if op in {"begin_local_file_effect", "complete_local_file_effect"}:
                from .file_effects import execute_file as execute_file_effect
                return execute_file_effect(view, req)
            if op == "publish_source_snapshot":
                return self._publish_source_snapshot(view, req, principal, headers)
            if op == "publish_verification_snapshot":
                return self._publish_verification_snapshot(view, req, principal, headers)
            if op == "rebuild_graph_index":
                from ..efficiency.graph import execute_file
                return execute_file(view, req)
            if op == "register_segment_manifest":
                from ..efficiency.graph import execute_file
                return self._register_client_document_receipt(view, execute_file, req)
            if op in {"build_task_context", "resume_task_context"}:
                from ..efficiency.context import execute_file
                return execute_file(view, req)
            if op in {"resolve_reuse", "record_reuse_result", "invalidate_reuse"}:
                from ..efficiency.reuse import execute_file
                # These Host boundary fields have already been authorized and
                # checked against the current run above. The shared F6 adapter
                # accepts its narrower local request shape, so remove only the
                # Host transport aliases before delegation.
                reuse_req = dict(req)
                reuse_payload = dict(req.get("payload", {}))
                for key in ("project_id", "canonical_workspace", "expected_run_revision"):
                    reuse_payload.pop(key, None)
                reuse_req["payload"] = reuse_payload
                return execute_file(view, reuse_req)
            if op == "compact_tool_result":
                from ..efficiency.results import execute_file
                return execute_file(view, req)
            if op in {"prepare_step_batch", "bind_step_batch", "collect_step_batch"}:
                from .batch_state import link_parent_report, verify_current_source
                with closing(self._db.connect()) as conn:
                    self._authorize_now(conn, req, headers)
                    if op != "prepare_step_batch":
                        verify_current_source(self, conn, req, headers)
                if op == "collect_step_batch":
                    link_parent_report(self, self._db, req, principal, headers)
                from ..efficiency.batch import execute_file
                return execute_file(view, req)
            if op == "save_step_directive":
                from ..steps import execute_file
                return execute_file(view, req)
            return response(req.get("request_id"), error={"code": "host_operation_unavailable",
                "message": "Host file operation is unavailable", "retryable": False}), 3
        except PmtError as exc:
            return response(req.get("request_id"), error=exc.as_dict()), exc.exit_code

    def _workspace_binding(self, conn, req, principal, *, mode="execute", allow_source_capture=False):
        payload = req.get("payload", {})
        if not isinstance(payload, dict):
            _fail("host_input_invalid", "payload must be an object", 2)
        project_id = _uuid(payload.get("project_id") or req.get("scope_id"), "project_id")
        project = conn.execute("SELECT * FROM scopes WHERE id=? AND kind='project'", (project_id,)).fetchone()
        if not project:
            _fail("project_scope_required", "Workspace operations require an existing project scope", 3)
        project_body = json.loads(project["body_json"] or "{}")
        repository_value = payload.get("repository_id") or project["parent_id"] or \
            project_body.get("repository_id", project_body.get("repository_scope_id"))
        repository_id = _uuid(repository_value, "repository_id")
        repository = conn.execute("SELECT * FROM scopes WHERE id=? AND kind='repository'", (repository_id,)).fetchone()
        if not project or not repository:
            _fail("repository_scope_mismatch", "Project and repository scopes are required", 3)
        bound_repo = project_body.get("repository_id", project_body.get("repository_scope_id"))
        if (project["parent_id"] not in {None, repository_id}
                or (bound_repo and bound_repo != repository_id)):
            _fail("repository_scope_mismatch", "Project is not bound to the requested repository", 3)
        if project["parent_id"] is None and bound_repo != repository_id:
            _fail("repository_scope_mismatch", "Standalone project needs an explicit repository binding", 3)
        self.auth.authorize_scope(conn, principal, project_id)
        public_project = {"repository_id": repository_id}
        stored_repository_body = json.loads(repository["body_json"] or "{}")
        public_repository = {"remote": stored_repository_body["remote"]} if isinstance(
            stored_repository_body.get("remote"), str) else {}
        task_ref = payload.get("task_ref") if isinstance(payload.get("task_ref"), dict) else {}
        run_value = payload.get("run_id") or task_ref.get("run_id")
        run_id = _uuid(run_value, "run_id")
        run = conn.execute("SELECT * FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        step = conn.execute("SELECT * FROM records WHERE id=? AND kind='step'", (run["step_id"],)).fetchone() if run else None
        canonical_ref = payload.get("canonical_workspace", payload.get("workspace"))
        if canonical_ref is None and run:
            canonical_ref = run["workspace"]
        match = _WORKSPACE_URI.fullmatch(canonical_ref) if isinstance(canonical_ref, str) else None
        if not match or match.group(1) != repository_id:
            _fail("workspace_mapping_invalid", "canonical_workspace must identify this repository", 3)
        relative_value = payload.get("relative_graph_path")
        if relative_value is None:
            hints = conn.execute("SELECT body_json FROM phase3_objects WHERE kind=? AND scope_id=? "
                "AND owner_actor=? AND owner_session=?", (_SOURCE_KIND, project_id,
                _HOST_ACTOR, _system_session(self.auth.namespace_id))).fetchall()
            paths = {json.loads(row[0]).get("relative_graph_path") for row in hints
                     if json.loads(row[0]).get("canonical_workspace") == canonical_ref}
            if len(paths) == 1:
                relative_value = paths.pop()
        relative = _relative(relative_value, "relative_graph_path")
        branch_key = payload.get("branch_key")
        if branch_key is not None and canonical_workspace(repository_id, branch_key) != canonical_ref:
            _fail("workspace_mapping_invalid", "Canonical workspace hash does not match branch_key", 3)
        if mode == "source_capture" and not isinstance(branch_key, str):
            _fail("workspace_mapping_invalid", "Source capture requires an explicit branch_key", 3)
        if (not run or not step or run["owner_session"] != principal.session_id
                or run["state"] not in _ACTIVE_RUNS or run["workspace"] != canonical_ref
                or project_scope_id(conn, step["scope_id"]) != project_id):
            _fail("workspace_authority_stale", "Current Host run, owner, project, or canonical workspace does not match", 3)
        expected_revision = payload.get("expected_run_revision")
        if (req.get("operation") in {"authorize_workspace", "publish_source_snapshot",
                                      "publish_verification_snapshot", "build_task_context",
                                      "resume_task_context", "read_task_context"}
                and expected_revision is None):
            _fail("execution_revision_conflict", "Current run revision is required for Host source access", 3)
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision != run["revision"]):
            _fail("execution_revision_conflict", "Host run revision changed", 3,
                  {"expected_revision": expected_revision, "current_revision": run["revision"]})
        if (run["owner_session"] != req.get("session_id") or req.get("actor") != principal.actor):
            _fail("ownership_conflict", "Current Host owner does not match request identity", 3)
        # Host claim leases add device attribution when present. Existing P2
        # current-run ownership and exact path scope locks remain authoritative.
        auth_req = dict(req) | {"payload": dict(payload) | {"run_id": run_id}}
        try:
            require_workspace_claim(self._db_for(conn, req), conn, auth_req, canonical_ref, [relative])
        except PmtError:
            raise
        if mode not in {"source_capture", "execute", "publish"}:
            _fail("workspace_mode_invalid", "Workspace operation mode is unsupported", 2)
        pointer = self._current_source_pointer(conn, {"project_id": project_id,
            "repository_id": repository_id, "canonical_workspace": canonical_ref,
            "relative_graph_path": relative}, required=False)
        if mode == "execute" and pointer is None:
            _fail("source_snapshot_required", "No client-captured current graph snapshot is registered", 3)
        if pointer and branch_key is None:
            branch_key = _branch_key(pin_source(pointer["body"].get("source_pin")))
            if canonical_workspace(repository_id, branch_key) != canonical_ref:
                _fail("source_conflict", "Current SourcePin no longer matches the canonical workspace", 3)
        return {"project_id": project_id, "repository_id": repository_id,
                "canonical_workspace": canonical_ref, "relative_graph_path": relative,
                "project_body": public_project, "repository_body": public_repository,
                "project_parent_id": project["parent_id"], "run": dict(run), "step": dict(step),
                "source_pointer": pointer}

    def _db_for(self, conn, req):
        # require_workspace_claim only needs the Database environment for F9
        # child grants; this extension stores the canonical URI in existing P2 locks.
        return self.db

    @staticmethod
    def _current_locks(conn, run_id):
        return [dict(row) for row in conn.execute(
            "SELECT kind,workspace,resource,owner_session FROM scope_locks WHERE run_id=? ORDER BY lock_key",
            (run_id,)).fetchall()]

    def _current_source_pointer(self, conn, binding, *, required=True):
        value = self._get_internal_object(conn, _SOURCE_KIND,
            _pointer_id(_SOURCE_KIND, binding["canonical_workspace"], binding["project_id"],
                        binding["relative_graph_path"]))
        if value is None:
            legacy = self._get_internal_object(conn, _SOURCE_KIND,
                _pointer_id(_SOURCE_KIND, binding["canonical_workspace"]))
            if (legacy and legacy["scope_id"] == binding["project_id"]
                    and legacy["body"].get("relative_graph_path") == binding["relative_graph_path"]):
                value = legacy
        if value is None:
            if required:
                _fail("source_snapshot_unavailable", "No client-captured current graph snapshot is registered", 3)
            return None
        body = value["body"]
        if (body.get("canonical_workspace") != binding["canonical_workspace"]
                or body.get("project_id") != binding["project_id"]
                or body.get("repository_id") != binding["repository_id"]
                or value["scope_id"] != binding["project_id"]):
            _fail("source_snapshot_corrupt", "Current source pointer has inconsistent project or repository metadata", 5)
        return value

    def _read_source_metadata(self, conn, req, principal, *, headers=None, validate_only=False):
        """Dashboard/history metadata reads grant no execution or checkout access."""
        payload = req.get("payload", {})
        required = {"project_id", "repository_id", "canonical_workspace", "relative_graph_path"}
        if set(payload) != required or payload["project_id"] != req.get("scope_id"):
            _fail("host_input_invalid", "Source metadata requires an exact selected Project mapping", 2)
        self.reference_scopes(conn, req)
        self.auth.authorize_scope(conn, principal, payload["project_id"])
        match = _WORKSPACE_URI.fullmatch(payload["canonical_workspace"]) if isinstance(
            payload["canonical_workspace"], str) else None
        if not match or match.group(1) != payload["repository_id"]:
            _fail("workspace_mapping_invalid", "Source metadata URI does not match its repository", 3)
        relative = _relative(payload["relative_graph_path"], "relative_graph_path")
        binding = {"project_id": payload["project_id"], "repository_id": payload["repository_id"],
            "canonical_workspace": payload["canonical_workspace"], "relative_graph_path": relative}
        pointer = self._current_source_pointer(conn, binding)
        if validate_only:
            return None
        resource = self._read_resource(pointer["body"]["graph_resource"], binding["project_id"], headers)
        source = self._validated_graph_resource(resource, pointer["body"], binding)
        return {"source_pin": source["source_pin"].to_dict(),
            "source_hash": source["source_pin"].source_hash,
            "snapshot_ref": {"kind": _SOURCE_KIND, "id": pointer["id"], "scope_id": binding["project_id"],
                "revision": pointer["revision"], "source_hash": pointer["body"]["source_hash"]},
            "provenance": "client_snapshot", "host_git_verified": False,
            "execution_authorized": False}

    def _get_internal_object(self, conn, kind, object_id):
        row = conn.execute("SELECT * FROM phase3_objects WHERE kind=? AND id=? AND owner_actor=? AND owner_session=?",
            (kind, object_id, _HOST_ACTOR, _system_session(self.auth.namespace_id))).fetchone()
        if row is None:
            return None
        try:
            body = json.loads(row["body_json"])
        except (TypeError, ValueError) as exc:
            raise PmtError("host_snapshot_corrupt", "Stored Host snapshot metadata is invalid", 5) from exc
        return {"kind": row["kind"], "id": row["id"], "scope_id": row["scope_id"],
                "source_hash": row["source_hash"], "revision": row["revision"],
                "body": body, "state": row["state"]}

    def _read_resource(self, reference, scope_id, headers):
        if not isinstance(reference, Mapping) or not isinstance(reference.get("id"), str):
            _fail("host_resource_ref_invalid", "A content-addressed Host resource ref is required", 5)
        result = self.resource_port.read({"resource_id": reference["id"]}, headers)
        if (result.get("scope_id") != scope_id or result.get("sha256") != reference.get("sha256")
                or type(result.get("size")) is not int or len(result.get("content", b"")) != result.get("size")):
            _fail("host_resource_corrupt", "Host resource bytes do not match their metadata", 5)
        return result

    def _validated_graph_resource(self, resource, pointer_body, binding):
        if resource.get("purpose") != "graph_snapshot":
            _fail("host_source_resource_invalid", "Source pointer does not reference a graph snapshot", 5)
        try:
            graph = strict_json_loads(resource["content"], max_bytes=_RESOURCE_LIMIT)
        except PmtError as exc:
            raise PmtError("host_source_resource_invalid", "Graph snapshot is invalid JSON", 5) from exc
        pin = pin_source(pointer_body.get("source_pin"))
        if (pin.repository_id != binding["repository_id"] or pin.project_id != binding["project_id"]
                or pointer_body.get("branch_key") != _branch_key(pin)
                or canonical_workspace(pin.repository_id, pointer_body.get("branch_key")) != binding["canonical_workspace"]):
            _fail("host_source_resource_invalid", "SourcePin does not match its canonical workspace", 5)
        report = validate_graph(graph, binding["project_id"], complete=False)
        resource_ref = pointer_body.get("graph_resource", {})
        if (report.get("sha256") != pin.graph_hash or report.get("schema_version") != pin.graph_schema
                or report.get("graph_version") != pin.graph_revision
                or pointer_body.get("source_hash") != pin.source_hash
                or resource_ref.get("id") != resource.get("resource_id")
                or resource_ref.get("sha256") != resource.get("sha256")):
            _fail("host_source_resource_mismatch", "Graph bytes do not match the registered SourcePin", 5)
        wire = resource["content"]
        return {"graph": graph, "wire": wire, "raw_sha256": hashlib.sha256(wire).hexdigest(),
                "report": report, "source_pin": pin, "git_origin": None,
                "is_git": pin.source_kind == "git", "repo_root": None,
                "git_relative_path": binding["relative_graph_path"]}

    def _workspace_authority(self, conn, req, principal, mode):
        binding = self._workspace_binding(conn, req, principal, mode=mode)
        pin = None
        if mode != "source_capture" and binding.get("source_pointer"):
            pin = binding["source_pointer"]["body"].get("source_pin")
            expected = req.get("payload", {}).get("expected_source")
            if expected is not None:
                verify_source_pin(expected, pin)
        locks = self._current_locks(conn, binding["run"]["id"])
        grant = {"repository_id": binding["repository_id"], "project_id": binding["project_id"],
            "canonical_workspace": binding["canonical_workspace"],
            "branch": req.get("payload", {}).get("branch"),
            "relative_graph_path": binding["relative_graph_path"], "run_id": binding["run"]["id"],
            "run_revision": binding["run"]["revision"],
            "owner": {"actor": principal.actor, "device_id": principal.device_id,
                      "session_id": principal.session_id, "environment_id": principal.environment_id},
            "scope_locks": [{"kind": item["kind"], "resource": item["resource"]} for item in locks],
            "source_pin": pin}
        grant["context_hash"] = fingerprint(grant)
        return {"status": "authorized", **grant,
            "source_capture_only": mode == "source_capture",
            "model_execution_allowed": mode != "source_capture"}

    def _publish_source_snapshot(self, db, req, principal, headers):
        payload = req.get("payload", {})
        required = {"project_id", "repository_id", "canonical_workspace", "relative_graph_path",
            "run_id", "expected_run_revision", "expected_source_revision", "branch_key", "source_pin",
            "graph_resource_ref"}
        if set(payload) != required:
            _fail("host_input_invalid", "Source snapshot fields are incomplete or unsupported", 2)
        if type(payload["expected_source_revision"]) is not int or payload["expected_source_revision"] < 0:
            _fail("host_input_invalid", "expected_source_revision must be a nonnegative pointer revision", 2)
        with closing(db.connect()) as conn:
            self._authorize_now(conn, req, headers)
            binding = self._workspace_binding(conn, req, principal, mode="source_capture")
            current = self._current_source_pointer(conn, binding, required=False)
            # CAS belongs in the authorized write transaction after exact cache
            # replay. A successful original publication already advanced it.
        pin = pin_source(payload["source_pin"])
        if (pin.repository_id != binding["repository_id"] or pin.project_id != binding["project_id"]
                or payload["branch_key"] != _branch_key(pin)
                or canonical_workspace(pin.repository_id, payload["branch_key"]) != binding["canonical_workspace"]):
            _fail("source_conflict", "Client SourcePin does not match the authorized canonical mapping", 3)
        artifact_ref = payload["graph_resource_ref"]
        if (not isinstance(artifact_ref, Mapping) or set(artifact_ref) != {"id", "sha256", "size", "scope_id", "purpose"}
                or artifact_ref.get("purpose") != "graph_snapshot" or artifact_ref.get("scope_id") != binding["project_id"]):
            _fail("host_resource_ref_invalid", "graph_resource_ref must be a scoped graph snapshot artifact", 3)
        resource = self._read_resource(artifact_ref, binding["project_id"], headers)
        try:
            graph = strict_json_loads(resource["content"], max_bytes=_RESOURCE_LIMIT)
        except PmtError as exc:
            raise PmtError("source_snapshot_invalid", "Graph resource is invalid JSON", 3) from exc
        report = validate_graph(graph, binding["project_id"], complete=False)
        if (report["sha256"] != pin.graph_hash or report["schema_version"] != pin.graph_schema
                or report["graph_version"] != pin.graph_revision):
            _fail("source_snapshot_invalid", "Client graph content and SourcePin do not match", 3)
        artifact = dict(artifact_ref)
        pointer_id = current["id"] if current else _pointer_id(_SOURCE_KIND,
            binding["canonical_workspace"], binding["project_id"], binding["relative_graph_path"])
        pointer_body = {"schema_version": 1, "canonical_workspace": binding["canonical_workspace"],
            "repository_id": binding["repository_id"], "project_id": binding["project_id"],
            "relative_graph_path": binding["relative_graph_path"],
            "branch_key": payload["branch_key"],
            "source_pin": pin.to_dict(), "source_hash": pin.source_hash, "graph_hash": pin.graph_hash,
            "graph_resource": artifact, "provenance": "client_snapshot", "host_git_verified": False,
            "owner": {"actor": principal.actor, "device_id": principal.device_id,
                      "session_id": principal.session_id, "environment_id": principal.environment_id}}

        def commit(conn, request):
            self.authorize(conn, request, principal)
            current_binding = self._workspace_binding(conn, request, principal, mode="source_capture")
            current = self._current_source_pointer(conn, current_binding, required=False)
            revision = current["revision"] if current else 0
            if revision != payload["expected_source_revision"]:
                _fail("revision_conflict", "Host source pointer changed during upload", 3,
                    {"expected_revision": payload["expected_source_revision"], "current_revision": revision})
            receipt = Phase3Storage(db).put_object(_SOURCE_KIND, pointer_id, binding["project_id"],
                _HOST_ACTOR, _system_session(self.auth.namespace_id), pin.source_hash, revision, pointer_body,
                state="ready", request_id=request["request_id"],
                event_id=str(uuid.uuid5(uuid.UUID(request["request_id"]), "host-source-snapshot-published")),
                conn=conn)
            return {"snapshot_ref": {"kind": _SOURCE_KIND, "id": pointer_id,
                "scope_id": binding["project_id"], "canonical_workspace": binding["canonical_workspace"],
                "revision": receipt["revision"], "source_hash": pin.source_hash},
                "source_pin": pin.to_dict(), "graph_hash": pin.graph_hash,
                "graph_resource": artifact, "provenance": "client_snapshot",
                "host_git_verified": False, "revision": receipt["revision"],
                "replayed": receipt["replayed"]}
        return db.run_request(req, commit)

    def _source_receipt(self, source):
        return {"canonical_workspace": source["workspace"], "repository_id": source["repository_id"],
            "project_id": source["scope_id"], "relative_graph_path": source["relative_path"],
            "source_pin": source["source_pin"].to_dict(), "graph": source["graph"],
            "graph_hash": source["source_pin"].graph_hash,
            "graph_schema": source["source_pin"].graph_schema,
            "graph_revision": source["source_pin"].graph_revision,
            "provenance": "client_snapshot", "host_git_verified": False,
            "snapshot_revision": source["snapshot_revision"]}

    def _verification_pointer_id(self, scope_id, definition_id, definition_version, environment_id, canonical_ref):
        return fingerprint({"scope_id": scope_id, "definition_id": definition_id,
            "definition_version": definition_version, "environment_id": environment_id,
            "canonical_workspace": canonical_ref})

    def _publish_verification_snapshot(self, db, req, principal, headers):
        payload = req.get("payload", {})
        required = {"target_id", "definition_id", "definition_version", "project_id", "repository_id",
            "canonical_workspace", "relative_graph_path", "run_id", "expected_run_revision",
            "expected_source", "verification_resource_ref", "expected_snapshot_revision",
            "command", "inputs_sha256"}
        if set(payload) != required:
            _fail("host_input_invalid", "Verification snapshot fields are incomplete or unsupported", 2)
        expected_revision = payload["expected_snapshot_revision"]
        if type(expected_revision) is not int or expected_revision < 0:
            _fail("host_input_invalid", "expected_snapshot_revision must be nonnegative", 2)
        with closing(db.connect()) as conn:
            self._authorize_now(conn, req, headers)
            binding = self._workspace_binding(conn, req, principal, mode="execute")
            current_source = self._current_source_pointer(conn, binding)
            verify_source_pin(payload["expected_source"], current_source["body"]["source_pin"])
            target_scope = self._target_scope(conn, payload["target_id"])
            if project_scope_id(conn, target_scope) != binding["project_id"]:
                _fail("verification_scope_mismatch", "Verification target is outside the authorized project", 3)
            from ..verification import _criteria, _record_and_scope
            target_record, target_scope_row, target_body = _record_and_scope(conn, payload["target_id"])
            criterion_hashes = (_criteria(target_body) if target_record and
                                target_record["kind"] in {"work", "item", "step"} else {})
            resource = self._read_resource(payload["verification_resource_ref"], binding["project_id"], headers)
            if resource.get("purpose") != "verification_snapshot":
                _fail("host_resource_ref_invalid", "verification_resource_ref has the wrong purpose", 3)
            if resource.get("purpose") != "verification_snapshot":
                _fail("host_resource_ref_invalid", "verification_resource_ref has the wrong purpose", 3)
            try:
                submitted_manifest = strict_json_loads(resource["content"], max_bytes=_RESOURCE_LIMIT)
            except PmtError as exc:
                raise PmtError("verification_snapshot_invalid", "Verification resource is invalid JSON", 3) from exc
            manifest = self._validate_verification_manifest(submitted_manifest, payload["verification_resource_ref"],
                self._source_from_pointer(conn, binding, current_source, headers), principal,
                payload["target_id"], payload["definition_id"], payload["definition_version"],
                payload["command"], payload["inputs_sha256"],
                criterion_hashes, conn, request=req)
            # Exact replay is checked inside run_request before the write CAS.
        artifact = dict(payload["verification_resource_ref"])
        pointer_body = {"schema_version": 1, "project_id": binding["project_id"],
            "target_id": payload["target_id"], "definition_id": payload["definition_id"],
            "definition_version": payload["definition_version"], "environment_id": principal.environment_id,
            "canonical_workspace": binding["canonical_workspace"],
            "source_hash": current_source["body"]["source_hash"],
            "verification_resource": artifact, "provenance": "client_snapshot"}
        pointer_id = self._verification_pointer_id(binding["project_id"], payload["definition_id"],
            payload["definition_version"], principal.environment_id, binding["canonical_workspace"])

        def commit(conn, request):
            self.authorize(conn, request, principal)
            current_binding = self._workspace_binding(conn, request, principal, mode="execute")
            source_now = self._current_source_pointer(conn, current_binding)
            verify_source_pin(payload["expected_source"], source_now["body"]["source_pin"])
            old = self._get_internal_object(conn, _VERIFICATION_KIND, pointer_id)
            revision = old["revision"] if old else 0
            if revision != expected_revision:
                _fail("revision_conflict", "Host verification snapshot changed during upload", 3,
                    {"expected_revision": expected_revision, "current_revision": revision})
            receipt = Phase3Storage(db).put_object(_VERIFICATION_KIND, pointer_id, binding["project_id"],
                _HOST_ACTOR, _system_session(self.auth.namespace_id), source_now["body"]["source_hash"],
                revision, pointer_body, state="ready", request_id=request["request_id"],
                event_id=str(uuid.uuid5(uuid.UUID(request["request_id"]), "host-verification-snapshot-published")),
                conn=conn)
            return {"snapshot_ref": {"kind": "verification_snapshot", "id": artifact["id"],
                "sha256": artifact["sha256"], "size": artifact["size"],
                "scope_id": binding["project_id"], "purpose": artifact["purpose"],
                "revision": receipt["revision"]}, "input_fingerprint": manifest.get("input_fingerprint"),
                "source_hash": source_now["body"]["source_hash"], "revision": receipt["revision"],
                "provenance": "client_snapshot", "replayed": receipt["replayed"]}
        return db.run_request(req, commit)

    def _register_client_document_receipt(self, db, graph_execute_file, req):
        envelope, code = graph_execute_file(db, req)
        if code or not isinstance(envelope, dict) or not envelope.get("ok"):
            return envelope, code
        result = envelope.get("result")
        if not isinstance(result, dict):
            _fail("document_receipt_invalid", "Graph manifest registration returned no receipt", 5)
        payload = req.get("payload", {})
        manifest = payload.get("manifest")
        certificate = payload.get("coverage_certificate")
        if isinstance(manifest, dict):
            if (result.get("segment_id") != manifest.get("segment_id")
                    or type(result.get("manifest_revision")) is not int
                    or result["manifest_revision"] < 1
                    or not isinstance(result.get("manifest_hash"), str)
                    or not _HEX64.fullmatch(result["manifest_hash"])):
                _fail("document_receipt_invalid", "Graph manifest receipt identity or hash is invalid", 5)
            segment_id = manifest.get("segment_id")
            document_path = manifest.get("document_path")
            source_pin = manifest.get("source_pin")
            manifest_hash = result.get("manifest_hash")
            producer_ref = None
        elif isinstance(certificate, dict):
            if (type(result.get("coverage_revision")) is not int or result["coverage_revision"] < 1
                    or not isinstance(result.get("coverage"), dict)):
                _fail("document_receipt_invalid", "Graph coverage registration returned no versioned receipt", 5)
            segment_id = None
            document_path = certificate.get("document_paths")
            source_pin = certificate.get("source_pin")
            manifest_hash = result.get("certificate_hash") or fingerprint(certificate)
            producer_ref = certificate.get("production_receipt_ref")
        else:
            _fail("document_receipt_invalid", "Manifest registration request lacks a typed document receipt", 5)
        expected = pin_source(source_pin)
        scope_id = req.get("scope_id")
        object_id = segment_id or str(uuid.uuid5(uuid.UUID(scope_id),
            "host-document-coverage:" + expected.source_hash))
        body = {"schema_version": 1, "segment_id": segment_id, "document_path": document_path,
            "manifest_hash": manifest_hash, "source_hash": expected.source_hash,
            "source_pin": expected.to_dict(), "producer_receipt_ref": producer_ref,
            "provenance": "client_doc_receipt", "host_document_verified": False}
        internal_event = str(uuid.uuid5(uuid.UUID(req["request_id"]),
            "client-doc-receipt:" + str(result.get("manifest_revision", result.get("coverage_revision", 1)))))
        storage = Phase3Storage(db)
        with db.write() as conn:
            old = storage.get_object("host_document_receipt", object_id, scope_id,
                _HOST_ACTOR, _system_session(self.auth.namespace_id), conn=conn)
            if old and old["body"] == body:
                receipt = {"id": object_id, "revision": old["revision"], "replayed": True}
            else:
                revision = old["revision"] if old else 0
                receipt = storage.put_object("host_document_receipt", object_id, scope_id, _HOST_ACTOR,
                    _system_session(self.auth.namespace_id), expected.source_hash, revision, body,
                    state="ready", request_id=req["request_id"], event_id=internal_event, conn=conn)
        result["client_document_receipt"] = {"kind": "host_document_receipt", "id": object_id,
            "scope_id": scope_id, "source_hash": expected.source_hash,
            "manifest_hash": manifest_hash, "provenance": "client_doc_receipt",
            "host_document_verified": False, "revision": receipt["revision"]}
        envelope["result"] = result
        return envelope, code

    def _source_from_pointer(self, conn, binding, pointer, headers):
        resource = self._read_resource(pointer["body"]["graph_resource"], binding["project_id"], headers)
        source = self._validated_graph_resource(resource, pointer["body"], binding)
        return source | {"scope_id": binding["project_id"], "repository_id": binding["repository_id"],
            "workspace": binding["canonical_workspace"], "relative_path": binding["relative_graph_path"],
            "graph_path": None, "project_body": binding["project_body"],
            "repository_body": binding["repository_body"], "project_parent_id": binding["project_parent_id"]}

    @staticmethod
    def _target_scope(conn, target_id):
        _uuid(target_id, "target_id")
        row = conn.execute("SELECT scope_id FROM records WHERE id=? UNION SELECT id FROM scopes WHERE id=?",
                           (target_id, target_id)).fetchone()
        if not row:
            _fail("verification_target_not_found", "Verification target is not stored in Host state", 3)
        return row[0]

    def _validate_verification_manifest(self, manifest, reference, source, principal, target_id,
                                        definition_id, definition_version, command, inputs_fingerprint,
                                        criterion_hashes, conn, *, request=None):
        required = {"schema_version", "target_id", "definition_id", "definition_version", "environment_id",
            "canonical_workspace", "source_pin", "command", "inputs_sha256", "criteria", "workspace_files",
            "runtime", "dependency_manifests", "configuration_hashes", "evidence_refs",
            "inventory_status", "provenance"}
        if not isinstance(manifest, Mapping) or set(manifest) != required:
            _fail("verification_snapshot_invalid", "Client verification manifest fields are invalid", 3)
        if (type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1
                or manifest["target_id"] != target_id or manifest["definition_id"] != definition_id
                or manifest["definition_version"] != definition_version
                or manifest["environment_id"] != principal.environment_id
                or manifest["canonical_workspace"] != source["workspace"]
                or manifest["provenance"] != "client_snapshot"):
            _fail("verification_snapshot_binding_mismatch", "Client verification bindings do not match current Host state", 3)
        pin = pin_source(manifest["source_pin"])
        verify_source_pin(pin, source["source_pin"])
        if manifest["inventory_status"] not in {"complete", "partial", "unknown"}:
            _fail("verification_snapshot_invalid", "inventory_status is invalid", 2)
        files = manifest["workspace_files"]
        if not isinstance(files, list) or len(files) > 5000:
            _fail("verification_snapshot_invalid", "workspace_files must be a bounded array", 2)
        checked_files, seen = [], set()
        for item in files:
            if not isinstance(item, Mapping) or set(item) != {"path", "sha256", "size"}:
                _fail("verification_snapshot_invalid", "Workspace file inventory entry is invalid", 2)
            path = _relative(item["path"], "workspace_files.path")
            digest = item["sha256"]
            if (not isinstance(digest, str) or not _HEX64.fullmatch(digest) or path in seen
                    or type(item["size"]) is not int or item["size"] < 0):
                _fail("verification_snapshot_invalid", "Workspace file path or hash is invalid", 2)
            seen.add(path); checked_files.append({"path": path, "sha256": digest, "size": item["size"]})
        if checked_files != sorted(checked_files, key=lambda item: item["path"]):
            _fail("verification_snapshot_invalid", "workspace_files must be sorted by relative path", 2)
        if request is not None and checked_files:
            workspace = source["workspace"]
            paths = [item["path"] for item in checked_files]
            claim_request = dict(request) | {"payload": dict(request.get("payload") or {}) |
                {"run_id": request.get("payload", {}).get("run_id")}}
            require_workspace_claim(self._db, conn, claim_request, workspace, paths)
        runtime = manifest["runtime"]
        if (not isinstance(runtime, Mapping) or set(runtime) != {"os", "architecture", "python", "sqlite", "packages"}
                or any(not isinstance(runtime[name], str) or not runtime[name] or len(runtime[name]) > 200
                       for name in ("os", "architecture", "python", "sqlite"))
                or not isinstance(runtime["packages"], list) or len(runtime["packages"]) > 10000
                or any(not isinstance(item, str) or not item or len(item) > 500 for item in runtime["packages"])):
            _fail("verification_snapshot_invalid", "Runtime inventory is invalid or unbounded", 2)
        manifests, configs = manifest["dependency_manifests"], manifest["configuration_hashes"]
        for name, values in (("dependency_manifests", manifests), ("configuration_hashes", configs)):
            if not isinstance(values, list) or len(values) > 500:
                _fail("verification_snapshot_invalid", f"{name} must be a bounded array", 2)
            seen_paths = set()
            for value in values:
                if not isinstance(value, Mapping) or set(value) != {"path", "sha256"}:
                    _fail("verification_snapshot_invalid", f"{name} entry is invalid", 2)
                path = _relative(value["path"], name + ".path")
                if path in seen_paths or not isinstance(value["sha256"], str) or not _HEX64.fullmatch(value["sha256"]):
                    _fail("verification_snapshot_invalid", f"{name} path or hash is invalid", 2)
                seen_paths.add(path)
        if criterion_hashes is not None and canonical_json(manifest["criteria"]) != canonical_json(criterion_hashes):
            _fail("verification_criteria_changed", "Client criteria hashes differ from current Host records", 3)
        if command is not None and canonical_json(manifest["command"]) != canonical_json(command):
            _fail("verification_command_mismatch", "Client verification command differs from the request", 3)
        if inputs_fingerprint is not None and manifest["inputs_sha256"] != inputs_fingerprint:
            _fail("verification_input_mismatch", "Client input hash differs from the request", 3)
        if not isinstance(manifest["inputs_sha256"], str) or not _HEX64.fullmatch(manifest["inputs_sha256"]):
            _fail("verification_snapshot_invalid", "inputs_sha256 must be SHA-256", 2)
        evidence = manifest["evidence_refs"]
        if not isinstance(evidence, list) or len(evidence) > 500:
            _fail("verification_snapshot_invalid", "evidence_refs must be a bounded array", 2)
        for item in evidence:
            if not isinstance(item, Mapping) or set(item) != {"id", "sha256"} or not isinstance(item.get("sha256"), str) or not _HEX64.fullmatch(item["sha256"]):
                _fail("verification_snapshot_invalid", "Evidence ref must contain an artifact id and SHA-256", 2)
            artifact = conn.execute("SELECT scope_id,sha256,state FROM artifacts WHERE id=?", (item["id"],)).fetchone()
            if not artifact or artifact["scope_id"] != self._target_scope(conn, target_id) \
                    or artifact["state"] != "ready" or artifact["sha256"] != item["sha256"]:
                _fail("verification_evidence_mismatch", "Evidence is not current in Host resource storage", 3)
            from ..resources import check_artifact
            if not check_artifact(self._db, conn, item["id"])["valid"]:
                _fail("verification_evidence_mismatch", "Evidence resource failed Host hash verification", 3)
        result = dict(manifest)
        result["snapshot_reasons"] = ([] if manifest["inventory_status"] == "complete"
                                      else ["client_inventory_" + manifest["inventory_status"]])
        result["input_fingerprint"] = fingerprint(result)
        return result

    @property
    def db(self):
        return self._db
