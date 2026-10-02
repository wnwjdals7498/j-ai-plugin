"""Authenticated storage operations; never a dispatcher for local process effects.

This service is transport-independent. The HTTP wrapper supplies verified wire
headers; current device/scope checks also run inside replay/write transactions.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json
from contextlib import closing

from .. import __version__
from ..db import SCHEMA_VERSION
from ..errors import PmtError
from ..service import normalize_request, response
from ..util import canonical_json, fingerprint, utc_now
from .auth import AuthRegistry, identifier
from . import control_state

LIFECYCLE = frozenset({"create_scope", "save_change", "save_decision", "record_event", "claim_task",
                       "release_claim", "recover_claim", "finish_task"})
EXECUTION = frozenset({"enqueue_execution", "prepare_execution", "attach_execution_handle", "observe_execution",
                       "submit_execution_result", "request_execution_cancel", "reconcile_execution",
                       "review_execution", "retry_execution", "extend_execution_scopes", "read_execution", "list_execution_queue"})
STEP_STATE = frozenset({"read_step", "set_task_metadata", "invalidate_plan_branch", "cancel_step", "review_step"})
READ = frozenset({"read_context", "read_step", "read_execution", "list_execution_queue", "read_progress"})
REVIEW = frozenset({"recover_claim", "finish_task", "review_execution", "review_step", "invalidate_plan_branch"})
RUNTIME = EXECUTION - {"list_execution_queue", "read_execution"}
ALLOWLIST = LIFECYCLE | EXECUTION | STEP_STATE | {"read_context", "read_progress", "observe_progress"}


class HostApplication:
    def __init__(self, db, claim_keys, active_key_id, *, extension=None):
        self.db = db
        self.auth = AuthRegistry(db)
        if (not isinstance(claim_keys, dict) or not claim_keys or active_key_id not in claim_keys
                or any(not isinstance(k, str) or not k or len(k) > 100 or not isinstance(v, bytes) or len(v) < 32
                       for k, v in claim_keys.items())):
            raise PmtError("host_key_unavailable", "Host requires a valid versioned claim key", 5)
        self._keys = dict(claim_keys)
        self.active_key_id = active_key_id
        self.extension = extension
        with db.write() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS host_request_scopes (request_id TEXT NOT NULL "
                         "REFERENCES requests(request_id) ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED, scope_id TEXT NOT NULL "
                         "REFERENCES scopes(id), device_id TEXT NOT NULL REFERENCES host_devices(id), "
                         "PRIMARY KEY(request_id,scope_id))")

    def is_read(self, operation):
        return operation in READ or operation == "read_execution_control" or bool(self.extension and operation in self.extension.read_operations)

    @staticmethod
    def _header(headers, key):
        return headers.get(key.lower(), headers.get(key, ""))

    def principal(self, conn, headers, *, session=True):
        authorization = self._header(headers, "authorization")
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise PmtError("unauthenticated", "A registered device credential is required", 3)
        return self.auth.authenticate(conn, authorization[7:], self._header(headers, "x-pmt-device"),
            self._header(headers, "x-pmt-namespace"),
            session_id=self._header(headers, "x-pmt-session") if session else None,
            environment_id=self._header(headers, "x-pmt-environment") if session else None)

    def compatibility(self, headers):
        with closing(self.db.connect()) as conn:
            principal = self.principal(conn, headers, session=False)
        from ..planning.graph import SCHEMA_VERSION as GRAPH_SCHEMA
        return {"api_version": 1, "core_version": __version__, "db_schema": SCHEMA_VERSION,
                "graph_schema": GRAPH_SCHEMA, "protocol_versions": [1], "namespace_id": self.auth.namespace_id,
                "device_id": principal.device_id, "actor": principal.actor,
                "scopes": list(principal.scopes), "permissions": sorted(principal.permissions)}

    def register_session(self, headers, payload):
        if not isinstance(payload, dict) or set(payload) != {"session_id", "environment_id"}:
            raise PmtError("host_input_invalid", "Session registration fields are invalid")
        if payload["environment_id"] != self._header(headers, "x-pmt-environment"):
            raise PmtError("unauthenticated", "Environment identity does not match the request", 3)
        authorization = self._header(headers, "authorization")
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            raise PmtError("unauthenticated", "A device credential is required", 3)
        result = self.auth.register_session(authorization[7:], self._header(headers, "x-pmt-device"),
            self._header(headers, "x-pmt-namespace"), payload["session_id"], payload["environment_id"])
        return {"api_version": 1, **result}

    def _scope_ids(self, conn, req):
        scopes = set()
        if req.get("scope_id"):
            scopes.add(req["scope_id"])
        p = req["payload"]
        record_ids = [req.get("record_id")]
        record_ids.extend(p.get(k) for k in ("item_id", "step_id", "target_id"))
        record_ids.extend(p.get("step_ids", []) if isinstance(p.get("step_ids"), list) else [])
        record_ids.extend(p.get("dependencies", []) if isinstance(p.get("dependencies"), list) else [])
        for record_id in filter(None, record_ids):
            identifier(record_id, "record_id")
            row = conn.execute("SELECT scope_id FROM records WHERE id=?", (record_id,)).fetchone()
            if row:
                scopes.add(row[0])
            elif req["operation"] not in {"save_change"}:
                raise PmtError("scope_forbidden", "Referenced record is absent or inaccessible", 3)
        parent_id = p.get("parent_id")
        if parent_id:
            identifier(parent_id, "parent_id")
            row = conn.execute("SELECT id FROM scopes WHERE id=?", (parent_id,)).fetchone()
            if row:
                scopes.add(parent_id)
            else:
                row = conn.execute("SELECT scope_id FROM records WHERE id=?", (parent_id,)).fetchone()
                if not row:
                    raise PmtError("scope_forbidden", "Parent is absent or inaccessible", 3)
                scopes.add(row[0])
        run_ids = [p.get("run_id")]
        for run_id in filter(None, run_ids):
            identifier(run_id, "run_id")
            row = conn.execute("SELECT r.scope_id FROM execution_runs e JOIN records r ON r.id=e.step_id WHERE e.id=?", (run_id,)).fetchone()
            if not row:
                raise PmtError("scope_forbidden", "Run is absent or inaccessible", 3)
            scopes.add(row[0])
        if p.get("job_id"):
            identifier(p["job_id"], "job_id")
            row = conn.execute("SELECT r.scope_id FROM execution_jobs j JOIN records r ON r.id=j.step_id WHERE j.id=?", (p["job_id"],)).fetchone()
            if not row:
                raise PmtError("scope_forbidden", "Job is absent or inaccessible", 3)
            scopes.add(row[0])
        for key in ("verification_ids", "evidence_refs", "evidence_ids"):
            values = p.get(key, [])
            if not isinstance(values, list):
                continue
            for value in values:
                identifier(value, "evidence_ref")
                if key == "verification_ids":
                    row = conn.execute("SELECT r.scope_id FROM verifications v JOIN records r ON r.id=v.target_id WHERE v.id=?", (value,)).fetchone()
                else:
                    row = conn.execute("SELECT scope_id FROM artifacts WHERE id=? AND state='ready'", (value,)).fetchone()
                if not row or not row[0]:
                    raise PmtError("scope_forbidden", "Evidence is absent or inaccessible", 3)
                scopes.add(row[0])
        if self.extension:
            scopes.update(self.extension.reference_scopes(conn, req))
        return scopes

    def authorize(self, conn, req, headers, *, record_ledger=False):
        principal = self.principal(conn, headers)
        if req["actor"] != principal.actor or req["session_id"] != principal.session_id:
            raise PmtError("unauthenticated", "Body identity does not match the registered device session", 3)
        principal.require("read" if self.is_read(req["operation"]) else "write")
        if req["operation"] in REVIEW:
            principal.require("review")
        if req["operation"] in RUNTIME:
            principal.require("runtime")
        from .execution_metadata import validate_execution_metadata
        validate_execution_metadata(req)
        scopes = self._scope_ids(conn, req)
        if not scopes and not (req["operation"] in {"create_scope", "record_event"} and "*" in principal.scopes):
            raise PmtError("scope_forbidden", "An explicit accessible scope is required", 3)
        for scope_id in scopes:
            self.auth.authorize_scope(conn, principal, scope_id)
        if req["operation"] in READ and not req.get("scope_id"):
            # Existing queue/context helpers can otherwise enumerate the namespace.
            raise PmtError("scope_forbidden", "Read operations require a selected scope", 3)
        if self.extension and req["operation"] in self.extension.operations:
            self.extension.authorize(conn, req, principal)
        if req["operation"] in control_state.OPERATIONS:
            control_state.authorize(self, conn, req, principal)
        if record_ledger:
            for scope_id in scopes:
                conn.execute("INSERT OR IGNORE INTO host_request_scopes VALUES(?,?,?)",
                             (req["request_id"], scope_id, principal.device_id))
        return principal, scopes

    def _token(self, lease):
        key = self._keys.get(lease["key_id"])
        if key is None:
            raise PmtError("host_key_unavailable", "The active lease key is unavailable", 5)
        fields = {k: lease[k] for k in ("id", "record_id", "scope_id", "device_id", "actor", "session_id", "key_id", "fingerprint", "generation")}
        return hmac.new(key, canonical_json(fields).encode(), hashlib.sha256).hexdigest()

    def _claim(self, conn, req, principal):
        from .. import lifecycle
        row = lifecycle._claim_record(conn, req)
        lease = {"id": req["request_id"], "record_id": row["id"], "scope_id": row["scope_id"],
                 "device_id": principal.device_id, "actor": principal.actor, "session_id": principal.session_id,
                 "key_id": self.active_key_id, "fingerprint": fingerprint(req), "generation": row["revision"] + 1,
                 "state": "active", "created_at": utc_now()}
        if req["operation"] == "recover_claim":
            principal.require("admin")
            evidence = req["payload"].get("terminated_or_isolated")
            if not isinstance(evidence, dict) or set(evidence) != {"evidence_ids", "reason"} or not evidence["evidence_ids"]:
                raise PmtError("recovery_evidence_required", "Recovery requires scoped termination/isolation evidence")
            for ref in evidence["evidence_ids"]:
                self._validate_artifact(conn, ref, row["scope_id"])
            prior = conn.execute("SELECT owner_session FROM claims WHERE record_id=?", (row["id"],)).fetchone()
            if prior and conn.execute("SELECT 1 FROM execution_runs WHERE owner_session=? "
                                      "AND state IN ('starting','running','reconciling','cancel_requested','review_pending') LIMIT 1", (prior[0],)).fetchone():
                raise PmtError("prior_owner_active", "Prior owner execution must be reconciled before claim recovery", 3)
            conn.execute("UPDATE host_claim_leases SET state='recovered' WHERE record_id=? AND state='active'", (row["id"],))
            result = lifecycle.recover_claim(self.db, conn, req, token_factory=lambda *_: self._token(lease))
        else:
            result = lifecycle.claim_task(self.db, conn, req, token_factory=lambda *_: self._token(lease))
        result.pop("claim_token")
        conn.execute("INSERT INTO host_claim_leases VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(lease[k] for k in
            ("id", "record_id", "scope_id", "device_id", "actor", "session_id", "key_id", "fingerprint", "generation", "state", "created_at")))
        return result | {"claim_ref": {"id": lease["id"], "record_id": row["id"], "generation": lease["generation"]}}

    def _lease_request(self, conn, req, principal):
        ref = req["payload"].get("claim_ref")
        if not isinstance(ref, dict) or set(ref) != {"id", "record_id", "generation"}:
            raise PmtError("claim_conflict", "An active Host claim locator is required", 3)
        identifier(ref["id"], "claim_ref")
        row = conn.execute("SELECT * FROM host_claim_leases WHERE id=? AND state='active'", (ref["id"],)).fetchone()
        record_id = req.get("record_id") or req["payload"].get("record_id")
        if (not row or row["record_id"] != record_id or ref["record_id"] != record_id
                or row["device_id"] != principal.device_id or row["actor"] != principal.actor
                or row["session_id"] != principal.session_id or row["generation"] != ref["generation"]):
            raise PmtError("claim_conflict", "Current claim owner or generation does not match", 3)
        clone = copy.deepcopy(req)
        clone["payload"].pop("claim_ref", None)
        clone["payload"]["claim_token"] = self._token(dict(row))
        return clone, row["id"]

    def _validate_artifact(self, conn, artifact_id, scope_id):
        from ..resources import check_artifact
        identifier(artifact_id, "artifact_id")
        row = conn.execute("SELECT scope_id FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not row or row[0] != scope_id or not check_artifact(self.db, conn, artifact_id)["valid"]:
            raise PmtError("evidence_unavailable", "A current hash-valid scoped artifact is required", 3)

    def _handle(self, conn, req, headers):
        principal, scopes = self.authorize(conn, req, headers)
        op = req["operation"]
        db = self.extension.database_view(self.db, principal, req, headers) if self.extension else self.db
        if op in control_state.OPERATIONS:
            result = control_state.handle(self, conn, req, principal)
        elif self.extension and op in self.extension.operations:
            result = self.extension.handle(db, conn, req, principal)
        elif op in {"claim_task", "recover_claim"}:
            result = self._claim(conn, req, principal)
        elif op in {"release_claim", "finish_task"}:
            from .. import lifecycle
            internal, lease_id = self._lease_request(conn, req, principal)
            result = getattr(lifecycle, op)(db, conn, internal)
            conn.execute("UPDATE host_claim_leases SET state='released' WHERE id=?", (lease_id,))
        elif op in LIFECYCLE:
            from .. import lifecycle
            result = getattr(lifecycle, op)(db, conn, req)
        elif op in EXECUTION:
            from ..execution.service import handle
            result = handle(db, conn, req)
        elif op in STEP_STATE:
            from ..steps import handle
            result = handle(db, conn, req)
        elif op == "read_context":
            from ..queries import handle
            result = handle(db, conn, req)
        else:
            from ..operations import handle
            result = handle(db, conn, req)
        if isinstance(result, dict) and isinstance(result.get("scope_id"), str):
            scopes.add(result["scope_id"])
        if not self.is_read(op):
            for scope_id in scopes:
                conn.execute("INSERT OR IGNORE INTO host_request_scopes VALUES(?,?,?)", (req["request_id"], scope_id, principal.device_id))
        return result

    def execute(self, value, headers):
        req = normalize_request(value)
        allowed = ALLOWLIST | control_state.OPERATIONS | (self.extension.operations if self.extension else frozenset())
        if req["operation"] not in allowed:
            raise PmtError("host_operation_forbidden", "This operation requires the local client runtime", 3)
        with closing(self.db.connect()) as conn:
            principal, _ = self.authorize(conn, req, headers)
        if self.extension and req["operation"] in self.extension.file_operations:
            view = self.extension.database_view(self.db, principal, req, headers)
            return self.extension.execute_file(view, req, principal, headers)
        if self.is_read(req["operation"]):
            with closing(self.db.connect()) as conn:
                conn.execute("BEGIN")
                try:
                    result = self._handle(conn, req, headers)
                finally:
                    conn.rollback()
            return response(req["request_id"], result=result), 0
        return self.db.run_request(req, lambda conn, request: self._handle(conn, request, headers),
                                   authorize=lambda conn, request: self.authorize(conn, request, headers, record_ledger=True))

    def get_request_result(self, request_id, headers, expected_fingerprint=None):
        identifier(request_id, "request_id")
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            try:
                principal = self.principal(conn, headers)
                principal.require("read")
                row = conn.execute("SELECT * FROM requests WHERE request_id=?", (request_id,)).fetchone()
                if row and (row["actor"] != principal.actor or row["session_id"] != principal.session_id):
                    raise PmtError("request_owner_mismatch", "Request result is owned by another session", 3)
                grants = conn.execute("SELECT scope_id,device_id FROM host_request_scopes WHERE request_id=?", (request_id,)).fetchall()
                if row and not grants:
                    raise PmtError("request_owner_mismatch", "Historical request lacks current device/scope authority", 3)
                for grant in grants:
                    if grant["device_id"] != principal.device_id:
                        raise PmtError("request_owner_mismatch", "Request result is owned by another device", 3)
                    self.auth.authorize_scope(conn, principal, grant["scope_id"])
                if row and expected_fingerprint is not None and not hmac.compare_digest(row["request_fingerprint"], expected_fingerprint):
                    raise PmtError("request_conflict", "request_id was used for a different request", 3)
                return {"api_version": 1, "actor": principal.actor, "session_id": principal.session_id,
                        "envelope": json.loads(row["response_json"]) if row else None,
                        "exit_code": row["exit_code"] if row else None}
            finally:
                conn.rollback()
