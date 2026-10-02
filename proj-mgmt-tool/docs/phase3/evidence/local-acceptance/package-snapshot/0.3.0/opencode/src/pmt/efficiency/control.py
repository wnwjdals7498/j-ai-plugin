"""Single-Step execution control using verified F5/F6/F7 refs and Phase-2 state."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import uuid

from ..errors import PmtError
from ..store import LocalStore
from ..util import canonical_json, fingerprint
from .local_runtime import LocalExecutionRuntime
from .storage import Phase3Storage

READ_OPERATIONS = {"read_execution_control"}
WRITE_OPERATIONS = set()
FILE_OPERATIONS = {"advance_execution_control", "acknowledge_execution_action"}
_CONTROL_KIND = "execution_control"
_POLL_BASE_SECONDS = 15
_POLL_MAX_SECONDS = 60


class ControlStateRepository:
    """Authority-neutral persistence boundary for owner-bound F8 control state."""

    def get(self, req, run_id, *, conn=None):
        raise NotImplementedError

    def compare_and_set(self, req, run_id, scope_id, source_hash, body, *,
                        event_name=None, expected_revision=None, request_suffix="state"):
        raise NotImplementedError

    def complete_response(self, req, result, *, run_id, scope_id, stage, operation, body):
        raise NotImplementedError


class SQLiteControlStateRepository(ControlStateRepository):
    """Local backend; Phase3Storage remains the only SQL state implementation."""

    def __init__(self, db):
        self.db = db

    def get(self, req, run_id, *, conn=None):
        storage = Phase3Storage(self.db)
        if conn is not None:
            return storage.get_object(_CONTROL_KIND, run_id, req.get("scope_id"),
                                      req["actor"], req["session_id"], conn=conn)
        with closing(self.db.connect()) as connection:
            return storage.get_object(_CONTROL_KIND, run_id, req.get("scope_id"),
                                      req["actor"], req["session_id"], conn=connection)

    def compare_and_set(self, req, run_id, scope_id, source_hash, body, *,
                        event_name=None, expected_revision=None, request_suffix="state"):
        storage = Phase3Storage(self.db)
        internal_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "pmt-control:" + request_suffix))
        internal = _request(req, "advance_execution_control", {"run_id": run_id,
            "control_stage": body.get("stage")}, request_id=internal_id)

        def commit(conn, request):
            old = storage.get_object(_CONTROL_KIND, run_id, scope_id,
                                     req["actor"], req["session_id"], conn=conn)
            actual_revision = old["revision"] if old else 0
            if expected_revision is not None and actual_revision != expected_revision:
                _fail("revision_conflict", "Execution control changed concurrently", 3,
                      {"expected_revision": expected_revision, "current_revision": actual_revision})
            receipt = storage.put_object(_CONTROL_KIND, run_id, scope_id, req["actor"], req["session_id"],
                source_hash, actual_revision, body, state=body.get("stage", "active"),
                request_id=request["request_id"], conn=conn)
            if event_name:
                from ..lifecycle import _event
                event_id = str(uuid.uuid5(uuid.UUID(request["request_id"]), "event:" + event_name))
                run_row = conn.execute("SELECT step_id FROM execution_runs WHERE id=?", (run_id,)).fetchone()
                _event(conn, request, event_id=event_id, event_type=event_name, scope_id=scope_id,
                       record_id=run_row["step_id"] if run_row else None,
                       payload={key: body.get(key) for key in ("stage", "source_hash", "context_ref",
                           "reuse_key_sha256", "action_nonce", "observation_hash", "notice_id",
                           "reason_code") if body.get(key) is not None})
            return {"control_ref": _control_ref(scope_id, run_id, receipt["revision"]),
                    "revision": receipt["revision"]}
        envelope, code = self.db.run_request(internal, commit)
        if code or not envelope.get("ok"):
            error = envelope.get("error") or {}
            raise PmtError(error.get("code", "control_store_failed"),
                           "Execution control state could not be saved", code or 3,
                           bool(error.get("retryable")), error.get("details"))
        return envelope["result"]

    def complete_response(self, req, result, *, run_id, scope_id, stage, operation, body):
        def commit(conn, request):
            control = Phase3Storage(self.db).get_object(_CONTROL_KIND, run_id, scope_id,
                req["actor"], req["session_id"], conn=conn)
            if control is None:
                _fail("execution_control_not_found", "Execution control state disappeared before response commit", 3)
            if control["body"].get("stage") != stage:
                _fail("execution_control_conflict", "Execution control stage changed before response commit", 3)
            return result
        return self.db.run_request(req, commit)


class _ControlDatabaseView:
    def __init__(self, db, repository, *, diagnostics=None):
        self._db = db
        self.control_state_repository = repository
        self.diagnostics = diagnostics if diagnostics is not None else getattr(db, "diagnostics", None)

    def __getattr__(self, name):
        return getattr(self._db, name)


def _fail(code, message, exit_code=2, details=None, retryable=False):
    raise PmtError(code, message, exit_code, retryable, details)


def _uuid(value, field):
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise PmtError("invalid_payload", f"{field} must be a canonical UUID") from exc
    return value


def _now(clock):
    value = clock() if callable(clock) else datetime.now(timezone.utc)
    if not isinstance(value, datetime):
        _fail("clock_invalid", "Controller clock must return a datetime", 5)
    if value.tzinfo is None:
        _fail("clock_invalid", "Controller clock must be timezone-aware", 5)
    return value.astimezone(timezone.utc)


def _request(req, operation, payload, request_id=None):
    return {"protocol_version": 1, "operation": operation,
            "request_id": request_id or req["request_id"], "actor": req["actor"],
            "session_id": req["session_id"], "scope_id": req.get("scope_id"),
            "source": req.get("source", {"product": "cli"}), "payload": payload}


def _call(port, request, label):
    if port is None or not callable(getattr(port, "execute", None)):
        _fail("controller_port_unavailable", f"{label} port is unavailable", 3)
    envelope, code = port.execute(request)
    if code or not isinstance(envelope, dict) or not envelope.get("ok"):
        error = envelope.get("error") if isinstance(envelope, dict) else None
        issue = error or {"code": f"{label}_failed", "details": None}
        details = dict(issue["details"]) if isinstance(issue.get("details"), dict) else {}
        details["operation"] = label
        raise PmtError(issue.get("code", f"{label}_failed"), f"{label} operation failed", code or 3,
                       bool(issue.get("retryable")), details)
    return envelope.get("result") or {}


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    state: str
    revision: int
    step_id: str
    attempt: int
    owner_session: str
    workspace: str
    scopes: list
    route: dict
    intent: dict
    result: dict | None
    stop_confirmed: bool


@dataclass(frozen=True)
class ControlAction:
    kind: str
    run_id: str
    reason: str
    nonce: str | None = None
    next_poll_at: str | None = None
    refs: dict | None = None
    locks_retained: bool = True

    def to_dict(self):
        return {key: value for key, value in {
            "kind": self.kind, "run_id": self.run_id, "reason": self.reason,
            "nonce": self.nonce, "next_poll_at": self.next_poll_at,
            "refs": self.refs, "locks_retained": self.locks_retained}.items() if value is not None}


def _snapshot(run, expected_run_id, session_id):
    if not isinstance(run, dict) or run.get("id") != expected_run_id or run.get("owner_session") != session_id:
        _fail("execution_owner_mismatch", "Execution result does not match the current run owner", 3)
    route, intent = run.get("route"), run.get("intent")
    if not isinstance(route, dict) or not isinstance(intent, dict):
        _fail("execution_snapshot_invalid", "Execution state lacks its pinned route or intent", 4)
    revision = run.get("revision")
    if type(revision) is not int or revision < 1:
        _fail("execution_snapshot_invalid", "Execution revision is invalid", 4)
    return RunSnapshot(run["id"], run["state"], revision, run["step_id"], run.get("attempt", 1), session_id,
                       run.get("workspace", ""), run.get("scopes", []), route, intent,
                       run.get("result"), bool(run.get("stop_confirmed")))


class ExecutionController:
    """Policy/orchestration only; P2 remains authoritative for run transitions."""

    def __init__(self, state_port, local_runtime, context_port=None, reuse_port=None,
                 result_port=None, clock=None, notifier=None, display=None, hosted_context_binding=False):
        self.state_port = state_port
        self.local_runtime = local_runtime
        self.context_port = context_port or state_port
        self.reuse_port = reuse_port or state_port
        self.result_port = result_port or state_port
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.notifier = notifier
        self.display = display
        self.hosted_context_binding = bool(hosted_context_binding)

    def read_run(self, req, run_id):
        result = _call(self.state_port, _request(req, "read_execution", {"run_id": run_id}), "read_execution")
        return _snapshot(result.get("run"), run_id, req["session_id"])

    def read_context(self, req, run, context_ref):
        if not isinstance(context_ref, dict) or set(context_ref) != {
                "kind", "id", "scope_id", "source_hash", "version", "projection_hash"}:
            _fail("context_ref_invalid", "A complete F5 task context reference is required", 3)
        context_payload = {"context_ref": context_ref}
        if self.hosted_context_binding:
            context_payload.update({"run_id": run.run_id, "project_id": req.get("scope_id"),
                "canonical_workspace": run.workspace, "expected_run_revision": run.revision})
        result = _call(self.context_port, _request(req, "read_task_context", context_payload),
                       "read_task_context")
        task = result.get("task") or {}
        if (result.get("current_authority", {}).get("owner_checked") is not True or
                result.get("current_authority", {}).get("run_id") != run.run_id or
                task.get("run_id") != run.run_id or task.get("step_id") != run.step_id or
                context_ref.get("scope_id") != req.get("scope_id")):
            _fail("context_authority_mismatch", "F5 context is not current for this run, Step and project", 3)
        if result.get("incomplete") is True or result.get("mandatory_omissions"):
            return {"valid": False, "reason": "context_incomplete", "result": result}
        unknown = result.get("unknown") or []
        required_unknown = [item for item in unknown if isinstance(item, dict) and item.get("severity") == "required"]
        if required_unknown:
            return {"valid": False, "reason": "context_unknown", "result": result}
        projection = result.get("projection")
        included = projection.get("included") if isinstance(projection, dict) else None
        section_ids = {item.get("section_id") for item in included if isinstance(item, dict)} if isinstance(included, list) else set()
        required_sections = {"purpose", "goal", "non_goal", "change_scope", "inputs", "outputs", "criteria",
                             "tests", "logging", "unresolved"}
        if str(result.get("role", "")).casefold() in {"lower", "worker", "implement", "implementation"}:
            required_sections.update({"method", "autonomy"})
        if required_sections - section_ids:
            return {"valid": False, "reason": "context_required_section_missing", "result": result}
        private_ref = result.get("private_content_ref")
        if not isinstance(private_ref, dict) or private_ref.get("kind") != "task_context_projection" or \
                private_ref.get("id") != context_ref["id"] or private_ref.get("projection_hash") != context_ref["projection_hash"]:
            _fail("context_private_ref_invalid", "F5 private content ref does not match its bounded projection", 3)
        source = result.get("source")
        if not isinstance(source, dict) or source.get("source_hash") != context_ref["source_hash"]:
            _fail("context_source_mismatch", "F5 source pin does not match the context reference", 3)
        return {"valid": True, "context_ref": dict(context_ref), "source_hash": source["source_hash"],
                "projection_hash": context_ref["projection_hash"],
                "projection": result.get("projection"), "role": result.get("role"),
                "task": task,
                "unknown": unknown, "private_content_ref": private_ref}

    def read_reuse(self, req, run, body_ref):
        if not isinstance(body_ref, dict):
            return {"valid": False, "reason": "reuse_decision_required"}
        paths = [item.get("resource") for item in run.scopes
                 if isinstance(item, dict) and item.get("kind") in {"path", "workspace"}
                 and isinstance(item.get("resource"), str)]
        if not paths:
            return {"valid": False, "reason": "execution_scope_unavailable"}
        result = _call(self.reuse_port, _request(req, "read_reuse_decision", {
            "body_ref": body_ref, "run_id": run.run_id, "workspace": run.workspace, "paths": paths}),
            "read_reuse_decision")
        manifest = result.get("manifest")
        if not isinstance(manifest, dict) or result.get("decision_ref") != body_ref:
            _fail("reuse_decision_invalid", "F6 returned a different decision reference", 3)
        stored_status = result.get("status")
        status = "reusable" if stored_status == "completed" else ("claimed" if stored_status == "active" else stored_status)
        if status == "reusable":
            origin = manifest.get("origin_ref")
            evidence = manifest.get("evidence_refs")
            if (not isinstance(origin, dict) or origin.get("kind") != "verification" or
                    not isinstance(evidence, list) or not evidence):
                _fail("reuse_decision_invalid", "Reusable F6 result lacks verified origin/evidence refs", 3)
        if status == "active" and manifest.get("original_run_ref") != run.run_id:
            return {"valid": True, "status": "active_other_run", "decision_ref": body_ref,
                    "key_sha256": manifest.get("key_sha256"), "original_run_ref": manifest.get("original_run_ref")}
        if status not in {"active", "completed", "reusable", "invalid", "unknown", "miss", "claimed"}:
            _fail("reuse_decision_invalid", "F6 returned an unsupported decision state", 3)
        return {"valid": True, "status": status, "decision_ref": dict(body_ref),
                "key_sha256": manifest.get("key_sha256"), "original_run_ref": manifest.get("original_run_ref"),
                "origin_ref": manifest.get("origin_ref"),
                "evidence_refs": manifest.get("evidence_refs", []),
                "manifest_sha256": body_ref.get("manifest_sha256")}

    def plan(self, run, context, reuse, runtime_observation=None):
        """Deterministic pure decision; all state and effect calls are outside this method."""
        if not context.get("valid"):
            return ControlAction("review-needed", run.run_id, context.get("reason", "context_unverified"),
                                 refs={"context_ref": context.get("context_ref")})
        if not reuse.get("valid"):
            return ControlAction("review-needed", run.run_id, reuse.get("reason", "reuse_unverified"))
        if reuse.get("status") == "reusable":
            return ControlAction("read-result", run.run_id, "verified_reuse_available",
                refs={"origin_ref": reuse.get("origin_ref"), "decision_ref": reuse.get("decision_ref"),
                      "evidence_refs": reuse.get("evidence_refs", [])})
        if reuse.get("status") == "active_other_run":
            return ControlAction("wait", run.run_id, "reuse_key_owned_by_another_active_run")
        if runtime_observation and runtime_observation.get("status") == "unknown":
            return ControlAction("review-needed", run.run_id, "runner_status_unknown",
                                 refs={"receipt_ref": runtime_observation.get("receipt_ref")})
        if runtime_observation and runtime_observation.get("status") == "terminal":
            return ControlAction("read-result", run.run_id, "runner_receipt_confirmed",
                                 refs={"receipt_ref": runtime_observation.get("receipt_ref")})
        return ControlAction("wait", run.run_id, "execution_active")


def _control_ref(scope_id, run_id, revision):
    return {"kind": _CONTROL_KIND, "id": run_id, "scope_id": scope_id, "revision": revision}


def _get_control(db, req, run_id, conn=None):
    repository = getattr(db, "control_state_repository", None)
    if repository is not None:
        return repository.get(req, run_id, conn=conn)
    if conn is not None:
        item = Phase3Storage(db).get_object(_CONTROL_KIND, run_id, req.get("scope_id"),
                                             req["actor"], req["session_id"], conn=conn)
        return item
    with closing(db.connect()) as connection:
        return Phase3Storage(db).get_object(_CONTROL_KIND, run_id, req.get("scope_id"),
                                             req["actor"], req["session_id"], conn=connection)


def _persist(db, req, run_id, scope_id, source_hash, body, *, event_name=None, expected_revision=None,
             request_suffix="state"):
    repository = getattr(db, "control_state_repository", None)
    if repository is not None:
        return repository.compare_and_set(req, run_id, scope_id, source_hash, body,
            event_name=event_name, expected_revision=expected_revision, request_suffix=request_suffix)
    storage = Phase3Storage(db)
    internal_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "pmt-control:" + request_suffix))
    internal = _request(req, "advance_execution_control", {"run_id": run_id, "control_stage": body.get("stage")},
                        request_id=internal_id)

    def commit(conn, request):
        old = storage.get_object(_CONTROL_KIND, run_id, scope_id, req["actor"], req["session_id"], conn=conn)
        actual_revision = old["revision"] if old else 0
        if expected_revision is not None and actual_revision != expected_revision:
            _fail("revision_conflict", "Execution control changed concurrently", 3,
                  {"expected_revision": expected_revision, "current_revision": actual_revision})
        receipt = storage.put_object(_CONTROL_KIND, run_id, scope_id, req["actor"], req["session_id"],
            source_hash, actual_revision, body, state=body.get("stage", "active"),
            request_id=request["request_id"], conn=conn)
        if event_name:
            from ..lifecycle import _event
            event_id = str(uuid.uuid5(uuid.UUID(request["request_id"]), "event:" + event_name))
            run_row = conn.execute("SELECT step_id FROM execution_runs WHERE id=?", (run_id,)).fetchone()
            _event(conn, request, event_id=event_id, event_type=event_name, scope_id=scope_id,
                   record_id=run_row["step_id"] if run_row else None, payload={key: body.get(key) for key in
                       ("stage", "source_hash", "context_ref", "reuse_key_sha256", "action_nonce",
                        "observation_hash", "notice_id", "reason_code") if body.get(key) is not None})
        return {"control_ref": _control_ref(scope_id, run_id, receipt["revision"]),
                "revision": receipt["revision"]}
    envelope, code = db.run_request(internal, commit)
    if code or not envelope.get("ok"):
        error = envelope.get("error") or {}
        raise PmtError(error.get("code", "control_store_failed"), "Execution control state could not be saved",
                       code or 3, bool(error.get("retryable")), error.get("details"))
    return envelope["result"]


def _store_response(db, req, result, *, run_id, scope_id, stage, operation):
    repository = getattr(db, "control_state_repository", None)
    if repository is not None:
        control = repository.get(req, run_id)
        return repository.complete_response(req, result, run_id=run_id, scope_id=scope_id,
            stage=stage, operation=operation, body=control["body"] if control else {})
    def commit(conn, request):
        # Recheck the owner-bound state before recording the original API response.
        control = Phase3Storage(db).get_object(_CONTROL_KIND, run_id, scope_id,
                                               req["actor"], req["session_id"], conn=conn)
        if control is None:
            _fail("execution_control_not_found", "Execution control state disappeared before response commit", 3)
        if control["body"].get("stage") != stage:
            _fail("execution_control_conflict", "Execution control stage changed before response commit", 3)
        return result
    return db.run_request(req, commit)


def _complete_response(db, req, run_id, scope_id, body, result):
    return _store_response(db, req, result, run_id=run_id, scope_id=scope_id,
                           stage=body.get("stage"), operation=req.get("operation"))


def _read_port_replay(port, req):
    lookup = getattr(port, "get_request_result", None)
    if not callable(lookup):
        _fail("request_result_lookup_unavailable", "StorePort cannot verify request replay identity", 3)
    # These are explicitly retry/correlation-only transport annotations. Some
    # StorePort normalizers reject them before applying the semantic fingerprint
    # ignore list, so omit them from the optional expected-request lookup body.
    expected = {key: value for key, value in req.items()
                if key not in {"received_at", "received_at_utc", "retry_count", "attempt"}}
    try:
        return lookup(req["request_id"], actor=req["actor"], session_id=req["session_id"],
                      expected_request=expected)
    except TypeError as exc:
        raise PmtError("request_result_lookup_unavailable",
                       "StorePort does not support expected-request replay checks", 3) from exc


def _f7_observation(req, port, run, context, observation):
    result = run.result
    if not isinstance(result, dict) or not isinstance(result.get("receipt_ref"), str):
        _fail("tool_result_unavailable", "Execution has no verified output receipt for F7", 3)
    task_id = context.get("task", {}).get("task_id")
    if not isinstance(task_id, str):
        _fail("context_task_missing", "F5 context does not identify the authorized task", 3)
    runner_observation = result.get("runner_observation") or {}
    exit_code = runner_observation.get("exit_code")
    status = ("cancelled" if run.state == "canceled" else "succeeded" if exit_code == 0 else
              "failed" if type(exit_code) is int else "unknown")
    result_id = str(uuid.uuid5(uuid.UUID(run.run_id), "pmt-f8-tool-result:" + result["receipt_ref"]))
    compact_payload = {
        "task_id": task_id, "run_id": run.run_id, "step_id": run.step_id, "status": status,
        "exit_code": exit_code if type(exit_code) is int else None, "format": "json",
        "result_id": result_id, "criteria_claims": []}
    host_receipt_ref = result.get("runtime_receipt_resource_ref")
    if isinstance(host_receipt_ref, dict) and callable(getattr(port, "read_resource", None)):
        resource = port.read_resource(host_receipt_ref.get("id"), session_id=req["session_id"],
            expected_sha256=host_receipt_ref.get("sha256"), scope_id=req.get("scope_id"))
        if (resource.get("purpose") != "result" or resource.get("resource_id") != host_receipt_ref.get("id")
                or resource.get("sha256") != host_receipt_ref.get("sha256")):
            _fail("tool_result_hash_mismatch", "Hosted runner receipt resource is not hash-bound", 3)
        try:
            manifest = json.loads(resource["content"].decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PmtError("tool_result_source_invalid", "Hosted runner receipt is invalid JSON", 3) from exc
        receipt_metadata = manifest.get("receipt") if isinstance(manifest, dict) else None
        if (not isinstance(manifest, dict) or manifest.get("run_id") != run.run_id
                or manifest.get("source_hash") != context.get("source_hash")
                or not isinstance(receipt_metadata, dict) or receipt_metadata.get("exit_code") != exit_code
                or receipt_metadata.get("stop_confirmed") is not True):
            _fail("tool_result_binding_invalid", "Hosted runner receipt does not match the current P2 result", 3)
        compact_payload["output"] = resource["content"].decode("utf-8")
    else:
        compact_payload["source_artifact_id"] = result["receipt_ref"]
    compact = _call(port, _request(req, "compact_tool_result", compact_payload,
        request_id=str(uuid.uuid5(uuid.UUID(run.run_id), "pmt-f8-tool-result-request:" + result["receipt_ref"]))),
        "compact_tool_result")
    if (compact.get("result_id") != result_id or compact.get("run_id") != run.run_id or
            not isinstance(compact.get("evidence_ref"), dict) or not isinstance(compact.get("artifact_sha256"), str)):
        _fail("tool_result_binding_invalid", "F7 result does not match the current run and actual receipt", 3)
    detail = _call(port, _request(req, "read_tool_result_detail", {
        "result_id": result_id, "max_bytes": 128, "max_lines": 2},
        request_id=str(uuid.uuid5(uuid.UUID(run.run_id), "pmt-f8-tool-result-verify:" + result["receipt_ref"]))),
        "read_tool_result_detail")
    compact_ref, detail_ref = compact.get("evidence_ref"), detail.get("evidence_ref")
    if (detail.get("result_id") != result_id or detail.get("artifact_sha256") != compact["artifact_sha256"] or
            not isinstance(detail_ref, dict) or not isinstance(compact_ref, dict) or
            detail_ref.get("kind") != compact_ref.get("kind") or detail_ref.get("id") != compact_ref.get("id") or
            detail_ref.get("scope_id") != compact_ref.get("scope_id") or
            detail_ref.get("source_hash") != compact_ref.get("sha256") or
            detail_ref.get("source_revision") != compact_ref.get("source_revision")):
        _fail("tool_result_hash_mismatch", "F7 resource hash could not be revalidated", 3)
    return {"result_id": result_id, "run_id": run.run_id, "status": compact.get("status"),
            "exit_code": compact.get("exit_code"), "artifact_sha256": compact["artifact_sha256"],
            "evidence_ref": compact["evidence_ref"], "criteria_verdict": compact.get("criteria_verdict"),
            "source": "F7_actual_resource", "content_sha256": detail.get("content_sha256")}


def _read_execution(db, req, run_id):
    port = LocalStore(db)
    return ExecutionController(port, LocalExecutionRuntime(port), port, port, port).read_run(req, run_id)


def _persist_observation(db, req, run_id, observation, *, expected_revision, source_hash):
    observed_hash = fingerprint({key: observation.get(key) for key in
        ("status", "run_state", "runner_kind", "receipt_ref", "receipt_sha256", "stop_confirmed", "reason")})
    old = _get_control(db, req, run_id)
    if old and old["body"].get("observation_hash") == observed_hash:
        return old, False
    body = dict(old["body"]) if old else {"schema_version": 1, "run_id": run_id, "scope_id": req["scope_id"]}
    body.update(stage="observed", observation_hash=observed_hash,
                last_observation={key: observation.get(key) for key in
                    ("status", "run_state", "runner_kind", "receipt_ref", "receipt_sha256", "stop_confirmed", "reason")
                    if observation.get(key) is not None}, source_hash=source_hash,
                last_changed_at=_now(None).isoformat().replace("+00:00", "Z"))
    body["observation_revision"] = (body.get("observation_revision", 0) + 1)
    result = _persist(db, req, run_id, req["scope_id"], source_hash, body,
                      expected_revision=expected_revision, event_name="control.state_observed",
                      request_suffix="observation:" + observed_hash)
    body["control_revision"] = result["revision"]
    return {"body": body, "revision": result["revision"]}, True


def _delay(body):
    attempts = min(3, max(0, int(body.get("poll_attempt", 0))))
    return min(_POLL_MAX_SECONDS, _POLL_BASE_SECONDS * (2 ** attempts))


def _next_poll(clock, body):
    return (_now(clock) + timedelta(seconds=_delay(body))).isoformat(timespec="seconds").replace("+00:00", "Z")


def _read_control(db, conn, req):
    payload = req.get("payload", {})
    if set(payload) != {"run_id"}:
        _fail("control_input_invalid", "read_execution_control accepts only run_id")
    run_id = _uuid(payload.get("run_id"), "run_id")
    item = _get_control(db, req, run_id, conn=conn)
    if item is None:
        return {"found": False, "run_id": run_id}
    body = item["body"]
    return {"found": True, "control_ref": _control_ref(item["scope_id"], run_id, item["revision"]),
            "stage": body.get("stage"), "run_id": run_id, "source_hash": item["source_hash"],
            "context_ref": body.get("context_ref"), "reuse_decision_ref": body.get("reuse_decision_ref"),
            "action": body.get("action"), "action_nonce": body.get("action_nonce"),
            "last_observation": body.get("last_observation"), "next_poll_at": body.get("next_poll_at"),
            "notice": body.get("notice"), "retry_count": body.get("retry_count", 0),
            "retry_run_id": body.get("retry_run_id"), "retry_from_run_id": body.get("retry_from_run_id"),
            "expected_source_hash": body.get("expected_source_hash"),
            "locks_retained": body.get("locks_retained", True), "reason_code": body.get("reason_code")}


def _base_body(run, context, reuse, control=None):
    body = dict(control["body"]) if control else {"schema_version": 1, "run_id": run.run_id,
        "scope_id": context.get("context_ref", {}).get("scope_id"), "retry_count": 0}
    body.update(context_ref=context.get("context_ref"), source_hash=context.get("source_hash"),
        reuse_decision_ref=reuse.get("decision_ref"), reuse_key_sha256=reuse.get("key_sha256"),
        context_projection_hash=context.get("projection_hash"), current_run_revision=run.revision)
    return body


def _reconcile_unknown(db, req, controller, run, context, reuse, control, observation):
    evidence = "pmt-control-observation:" + fingerprint({"run_id": run.run_id,
        "status": observation.get("status"), "reason": observation.get("reason"),
        "receipt_ref": observation.get("receipt_ref")})
    if run.state in {"starting", "running", "cancel_requested", "reconciling"} and run.state != "reconciling":
        reconcile_id = str(uuid.uuid5(uuid.UUID(req["request_id"]),
            "pmt-f8-reconcile:" + run.run_id + ":" + fingerprint(observation)))
        reconcile_req = _request(req, "reconcile_execution", {
            "run_id": run.run_id, "expected_run_revision": run.revision,
            "stopped": False, "not_started": False, "evidence_refs": [evidence]}, request_id=reconcile_id)
        _call(controller.state_port, reconcile_req, "reconcile_execution")
        run = controller.read_run(req, run.run_id)
    body = _base_body(run, context, reuse, control)
    body.update(stage="reconcile_required", reason_code=observation.get("reason", "runner_status_unknown"),
                observation_hash=fingerprint(observation),
                last_observation={key: observation.get(key) for key in
                    ("status", "run_state", "runner_kind", "receipt_ref", "reason") if observation.get(key) is not None},
                locks_retained=True)
    previous = _get_control(db, req, run.run_id)
    state = _persist(db, req, run.run_id, req["scope_id"], context["source_hash"], body,
        expected_revision=previous["revision"] if previous else 0,
        event_name="control.reconcile_required", request_suffix="reconcile:" + evidence[-32:])
    result = {"action": ControlAction("review-needed", run.run_id, body["reason_code"],
                refs={"receipt_ref": observation.get("receipt_ref"), "evidence_ref": evidence}).to_dict(),
              "control_ref": state["control_ref"], "run_state": run.state, "locks_retained": True,
              "notification": {"status": "query_only", "reason": body["reason_code"]}}
    return _complete_response(db, req, run.run_id, req["scope_id"], body, result)


def _block_before_dispatch(db, req, controller, run, context, reuse, control, reason):
    # Phase-2 state remains authoritative. The local runner preflight is known not to
    # have dispatched because no runner journal exists; preserve evidence in F8 state.
    evidence = "pmt-control-preflight:" + fingerprint({"run_id": run.run_id,
                                                         "reason": reason,
                                                         "context_ref": context["context_ref"]})
    reconcile_id = str(uuid.uuid5(uuid.UUID(req["request_id"]),
        "pmt-f8-preflight-reconcile:" + run.run_id + ":" + fingerprint(evidence)))
    reconcile_req = _request(req, "reconcile_execution", {
        "run_id": run.run_id, "expected_run_revision": run.revision,
        "stopped": False, "not_started": True, "actual_state": "blocked",
        "evidence_refs": [evidence]}, request_id=reconcile_id)
    _call(controller.state_port, reconcile_req, "reconcile_execution")
    run = controller.read_run(req, run.run_id)
    body = _base_body(run, context, reuse, control)
    body.update(stage="review_required", reason_code=reason, locks_retained=False,
                preflight_evidence_ref=evidence)
    old = _get_control(db, req, run.run_id)
    state = _persist(db, req, run.run_id, req["scope_id"], context["source_hash"], body,
        expected_revision=old["revision"] if old else 0,
        event_name="control.reconcile_required", request_suffix="preflight-blocked:" + reason)
    result = {"action": ControlAction("review-needed", run.run_id, reason,
                refs={"evidence_ref": evidence}, locks_retained=False).to_dict(),
              "control_ref": state["control_ref"], "run_state": run.state, "locks_retained": False,
              "notification": {"status": "query_only", "reason": reason}}
    return _complete_response(db, req, run.run_id, req["scope_id"], body, result)


def _notify(controller, notice_id, notice):
    if controller.notifier is None:
        return {"status": "query_only", "notice_id": notice_id}
    try:
        delivered = controller.notifier(notice_id, notice)
    except Exception:
        return {"status": "pending", "notice_id": notice_id}
    return {"status": "delivered" if delivered is not False else "pending", "notice_id": notice_id}


def _display(controller, notice_id, body):
    if controller.display is None:
        return {"supported": False, "path": "explicit_query"}
    try:
        outcome = controller.display(notice_id, body)
    except Exception:
        return {"supported": "unknown", "path": "explicit_query"}
    if outcome is True:
        return {"supported": True, "path": "display"}
    if outcome is False:
        return {"supported": False, "path": "explicit_query"}
    return {"supported": "unknown", "path": "explicit_query"}


def _pending_notice(body):
    notice = body.get("notice")
    if isinstance(notice, dict) and notice.get("status") in {"pending", "query_only"}:
        return notice
    return {"status": "none"}


def _emit_change(db, req, controller, run, context, reuse, control, observation, *, reason):
    observed_hash = fingerprint({key: observation.get(key) for key in
        ("status", "run_state", "runner_kind", "receipt_ref", "receipt_sha256", "stop_confirmed", "reason")})
    old_body = control["body"] if control else {}
    if old_body.get("observation_hash") == observed_hash:
        body = dict(old_body)
        body["next_poll_at"] = _next_poll(controller.clock, body)
        notice = body.get("notice")
        notification = _pending_notice(body)
        if isinstance(notice, dict) and notice.get("status") == "pending" and controller.notifier is not None:
            notification = _notify(controller, notice["id"], notice)
            notification.update(_display(controller, notice["id"], notice))
            body["notice"] = notice | notification
            persisted = _persist(db, req, run.run_id, req["scope_id"], context["source_hash"], body,
                expected_revision=control["revision"],
                event_name="control.ai_notified" if notification["status"] == "delivered" else None,
                request_suffix="notice-retry:" + notice["id"])
            control_ref = persisted["control_ref"]
        else:
            control_ref = _control_ref(req["scope_id"], run.run_id, control["revision"])
        result = {"action": ControlAction("wait", run.run_id, reason,
                    next_poll_at=body["next_poll_at"], locks_retained=True).to_dict(),
                  "control_ref": control_ref,
                  "run_state": run.state, "locks_retained": True,
                  "notification": notification, "observation_changed": False}
        return _complete_response(db, req, run.run_id, req["scope_id"], body, result)
    body = _base_body(run, context, reuse, control)
    notice_id = str(uuid.uuid5(uuid.UUID(run.run_id), "pmt-f8-notice:" + observed_hash))
    notice = {"id": notice_id, "status": "pending", "reason": reason,
              "run_id": run.run_id, "run_state": run.state,
              "observation_hash": observed_hash,
              "receipt_ref": observation.get("receipt_ref"),
              "context_ref": context.get("context_ref")}
    body.update(stage="observed", observation_hash=observed_hash,
        last_observation={key: observation.get(key) for key in
            ("status", "run_state", "runner_kind", "receipt_ref", "receipt_sha256", "stop_confirmed", "reason")
            if observation.get(key) is not None},
        notice=notice, next_poll_at=_next_poll(controller.clock, body),
        locks_retained=run.state not in {"succeeded", "failed", "blocked", "canceled"})
    state = _persist(db, req, run.run_id, req["scope_id"], context["source_hash"], body,
        expected_revision=control["revision"] if control else 0,
        event_name="control.state_observed", request_suffix="observation-change:" + observed_hash)
    display = _display(controller, notice_id, notice)
    notification = _notify(controller, notice_id, notice)
    notification.update(display)
    body["notice"] = notice | notification
    state2 = _persist(db, req, run.run_id, req["scope_id"], context["source_hash"], body,
        expected_revision=state["revision"],
        event_name="control.ai_notified" if notification["status"] == "delivered" else None,
        request_suffix="notice:" + notice_id)
    action_kind = "read-result" if observation.get("status") == "result_recorded" else "wait"
    refs = {"context_ref": context["context_ref"], "reuse_decision_ref": reuse.get("decision_ref"),
            "receipt_ref": observation.get("receipt_ref")}
    if observation.get("tool_observation"):
        refs["tool_observation"] = observation["tool_observation"]
    action = ControlAction(action_kind, run.run_id, reason,
        next_poll_at=body.get("next_poll_at") if action_kind == "wait" else None,
        refs=refs, locks_retained=body["locks_retained"])
    result = {"action": action.to_dict(), "control_ref": state2["control_ref"],
              "run_state": run.state, "locks_retained": body["locks_retained"],
              "observation_changed": True, "notification": notification}
    return _complete_response(db, req, run.run_id, req["scope_id"], body, result)


def _observation_response(db, req, controller, local_runtime, run, context, reuse, control, observation):
    if observation.get("status") == "terminal" and run.result is None:
        request = _request(req, "dispatch_execution", {"run_id": run.run_id,
            "context_ref": context["context_ref"]})
        envelope, code = local_runtime.collect_terminal(request, observation)
        if code or not envelope.get("ok"):
            return _reconcile_unknown(db, req, controller, run, context, reuse, control,
                {"status": "unknown", "reason": (envelope.get("error") or {}).get("code", "terminal_collect_failed"),
                 "receipt_ref": observation.get("receipt_ref")})
        run = controller.read_run(req, run.run_id)
    if observation.get("status") == "result_recorded" or run.result is not None:
        try:
            tool_observation = _f7_observation(req, controller.result_port, run, context, observation)
        except PmtError as error:
            return _reconcile_unknown(db, req, controller, run, context, reuse, control,
                {"status": "unknown", "reason": error.code, "receipt_ref": observation.get("receipt_ref")})
        observation = dict(observation, status="result_recorded", tool_observation=tool_observation)
        reason = "actual_result_available_for_review"
    elif observation.get("status") == "running":
        reason = "runner_progress_observed"
    elif observation.get("status") in {"awaiting_main_action", "cancel_requested"}:
        reason = "awaiting_main_action_ack" if observation.get("status") == "awaiting_main_action" else "awaiting_stop_confirmation"
    else:
        return _reconcile_unknown(db, req, controller, run, context, reuse, control, observation)
    return _emit_change(db, req, controller, run, context, reuse, control, observation, reason=reason)


def _terminal_advance(db, req, controller, local_runtime, run, control):
    if run.result is None:
        body = dict(control["body"]) if control else {"schema_version": 1, "run_id": run.run_id,
            "scope_id": req["scope_id"], "source_hash": fingerprint({"run_id": run.run_id})}
        body.update(stage="review_required", reason_code="terminal_run_has_no_durable_result",
                    locks_retained=not run.stop_confirmed)
        source_hash = body.get("source_hash")
        state = _persist(db, req, run.run_id, req["scope_id"], source_hash, body,
                         expected_revision=control["revision"] if control else 0,
                         event_name="control.reconcile_required", request_suffix="terminal-no-result")
        result = {"action": ControlAction("review-needed", run.run_id, body["reason_code"],
                    locks_retained=body["locks_retained"]).to_dict(), "control_ref": state["control_ref"],
                  "run_state": run.state, "locks_retained": body["locks_retained"],
                  "notification": {"status": "query_only"}}
        return _complete_response(db, req, run.run_id, req["scope_id"], body, result)
    context_ref = control["body"].get("context_ref") if control else None
    reuse_ref = control["body"].get("reuse_decision_ref") if control else None
    if not context_ref:
        _fail("context_ref_required", "Result presentation requires the original verified context reference", 3)
    context = controller.read_context(req, run, context_ref)
    if not context.get("valid"):
        _fail("context_source_stale", "The current F5 context cannot authorize result handoff", 3)
    observation = {"status": "result_recorded", "run_state": run.state,
        "receipt_ref": run.result.get("receipt_ref"), "runner_kind": run.route.get("mode"),
        "stop_confirmed": run.stop_confirmed}
    return _observation_response(db, req, controller, local_runtime, run, context,
                                 {"valid": True, "status": "completed", "decision_ref": reuse_ref},
                                 control, observation)


def _reuse_result(db, req, controller, run, context, reuse, control):
    if run.state not in {"starting", "running"}:
        return _reconcile_unknown(db, req, controller, run, context, reuse, control,
            {"status": "unknown", "reason": "reuse_run_state_unexpected"})
    evidence = [item.get("id") for item in reuse.get("evidence_refs", [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)]
    if not evidence:
        _fail("reuse_evidence_missing", "Verified F6 result has no resource evidence refs", 3)
    criteria = run.intent.get("criteria", [])
    criteria_results = [{"criterion_id": item if isinstance(item, str) else item.get("id"),
                         "outcome": "not_run", "reason": "reused receipt is available for main review",
                         "evidence_refs": []} for item in criteria]
    result_body = {"directive_version": run.intent.get("directive_version", run.revision),
        "actual_route": run.route,
        "summary": "A scope-authorized reusable result is available; main review remains required.",
        "criteria_results": criteria_results,
        "receipt_ref": reuse.get("origin_ref", {}).get("id"), "evidence_refs": evidence,
        "stop_confirmed": True, "stop_evidence_refs": evidence,
        "reuse_ref": reuse.get("decision_ref", {}).get("id"),
        "reuse_key_sha256": reuse.get("key_sha256")}
    result_id = str(uuid.uuid5(uuid.UUID(run.run_id), "pmt-f8-reuse-result:" + str(reuse.get("key_sha256"))))
    stored = _call(controller.state_port, _request(req, "submit_execution_result", {
        "run_id": run.run_id, "expected_run_revision": run.revision, "result": result_body},
        request_id=result_id), "submit_execution_result")
    run = controller.read_run(req, run.run_id)
    body = _base_body(run, context, reuse, control)
    body.update(stage="reuse_review_pending", reason_code="verified_reuse_requires_main_review",
                reuse_result_ref=reuse.get("origin_ref"), locks_retained=True,
                reuse_key_sha256=reuse.get("key_sha256"))
    old = _get_control(db, req, run.run_id)
    state = _persist(db, req, run.run_id, req["scope_id"], context["source_hash"], body,
        expected_revision=old["revision"] if old else 0,
        event_name="control.state_observed", request_suffix="reuse-result:" + str(reuse.get("key_sha256")))
    result = {"action": ControlAction("read-result", run.run_id, "verified_reuse_available",
        refs={"decision_ref": reuse["decision_ref"], "origin_ref": reuse.get("origin_ref"),
              "evidence_refs": reuse["evidence_refs"]}).to_dict(),
        "control_ref": state["control_ref"], "run_state": run.state, "locks_retained": True,
        "notification": {"status": "query_only", "reason": "main_review_required"},
        "stored": stored}
    return _complete_response(db, req, run.run_id, req["scope_id"], body, result)


def _cancel_control(db, req, controller, local_runtime, run, context, reuse, control):
    observed = local_runtime.observe(_request(req, "dispatch_execution", {"run_id": run.run_id,
        "context_ref": context["context_ref"]}))
    if observed.get("status") == "not_dispatched" and run.state == "starting":
        proof = "pmt-control-cancel-before-dispatch:" + fingerprint({"run_id": run.run_id,
            "request_id": req["request_id"], "context_ref": context["context_ref"]})
        _call(controller.state_port, _request(req, "reconcile_execution", {
            "run_id": run.run_id, "expected_run_revision": run.revision, "stopped": False,
            "not_started": True, "actual_state": "canceled", "evidence_refs": [proof]},
            request_id=str(uuid.uuid5(uuid.UUID(req["request_id"]), "cancel-not-started"))),
            "reconcile_execution")
        run = controller.read_run(req, run.run_id)
        body = _base_body(run, context, reuse, control)
        body.update(stage="cancelled_before_dispatch", reason_code="cancel_confirmed_not_started",
                    locks_retained=False, cancel_evidence_ref=proof)
        old = _get_control(db, req, run.run_id)
        state = _persist(db, req, run.run_id, req["scope_id"], context["source_hash"], body,
            expected_revision=old["revision"] if old else 0,
            event_name="control.state_observed", request_suffix="cancel-before-dispatch")
        result = {"action": ControlAction("read-result", run.run_id, body["reason_code"],
                    refs={"evidence_ref": proof}, locks_retained=False).to_dict(),
                  "control_ref": state["control_ref"], "run_state": run.state,
                  "locks_retained": False, "notification": {"status": "query_only"}}
        return _complete_response(db, req, run.run_id, req["scope_id"], body, result)
    cancel_state_id = str(uuid.uuid5(uuid.UUID(run.run_id), "pmt-f8-cancel-state:" + req["request_id"]))
    cancel_runtime_id = str(uuid.uuid5(uuid.UUID(run.run_id), "pmt-f8-cancel-runner:" + req["request_id"]))
    if run.state != "cancel_requested":
        _call(controller.state_port, _request(req, "request_execution_cancel", {
            "run_id": run.run_id, "expected_run_revision": run.revision}, request_id=cancel_state_id),
            "request_execution_cancel")
        run = controller.read_run(req, run.run_id)
    cancel_req = _request(req, "cancel_runner", {"run_id": run.run_id}, request_id=cancel_runtime_id)
    envelope, code = local_runtime.cancel(cancel_req)
    if code or not envelope.get("ok"):
        return _reconcile_unknown(db, req, controller, run, context, reuse, control,
            {"status": "unknown", "reason": (envelope.get("error") or {}).get("code", "cancel_outcome_unknown")})
    effect = envelope.get("result") or {}
    body = _base_body(run, context, reuse, control)
    if isinstance(effect.get("main_action"), dict):
        native = effect["main_action"]
        nonce = str(uuid.uuid5(uuid.UUID(run.run_id), "pmt-f8-native-cancel:" + cancel_runtime_id))
        current_control = _get_control(db, req, run.run_id)
        handle = (current_control["body"].get("handle_ref") if current_control else None) or \
                 native.get("native_handle") or {}
        handle_id = handle.get("id", handle.get("native_handle_id")) if isinstance(handle, dict) else None
        action = {"kind": "main-native-cancel", "run_id": run.run_id,
                  "expected_run_revision": run.revision, "action_nonce": nonce,
                  "handle_ref": {"kind": "native_handle", "id": handle_id},
                  "instruction": "Request cancellation once and report only the actual tool acknowledgement for this nonce."}
        body.update(stage="cancel_action_pending", action_nonce=nonce, action=action,
                    cancel_request_id=cancel_runtime_id, action_kind="main-native-cancel", locks_retained=True)
        result_action = action
    else:
        body.update(stage="cancel_wait", cancel_request_id=cancel_runtime_id,
                    reason_code="cancel_requested_stop_unconfirmed", locks_retained=True)
        result_action = ControlAction("wait", run.run_id, body["reason_code"],
            next_poll_at=_next_poll(controller.clock, body), locks_retained=True).to_dict()
    old = _get_control(db, req, run.run_id)
    state = _persist(db, req, run.run_id, req["scope_id"], context["source_hash"], body,
        expected_revision=old["revision"] if old else 0,
        event_name="control.state_observed", request_suffix="cancel-requested:" + cancel_runtime_id)
    result = {"action": result_action, "control_ref": state["control_ref"], "run_state": run.state,
              "locks_retained": True, "notification": {"status": "query_only"}}
    return _complete_response(db, req, run.run_id, req["scope_id"], body, result)


def _retry_terminal(db, req, controller, local_runtime, run, control):
    if control is None or not isinstance(control["body"].get("context_ref"), dict):
        _fail("retry_context_missing", "A verified original context is required before retry", 3)
    if control["body"].get("reason_code") == "retry_source_changed":
        _fail("retry_not_allowed", "A source-mismatched attempt requires review before another retry", 3)
    if control["body"].get("stage") == "retry_queued" and control["body"].get("retry_run_id"):
        retry_run_id = control["body"]["retry_run_id"]
        result = {"action": ControlAction("wait", retry_run_id, "retry_already_queued",
                    refs={"previous_run_id": run.run_id,
                          "expected_source_hash": control["source_hash"]}, locks_retained=False).to_dict(),
                  "control_ref": _control_ref(req["scope_id"], run.run_id, control["revision"]),
                  "run_id": retry_run_id, "previous_run_id": run.run_id,
                  "retry_count": control["body"].get("retry_count", 0),
                  "locks_retained": False, "notification": {"status": "query_only"}, "replayed": True}
        return _complete_response(db, req, run.run_id, req["scope_id"], control["body"], result)
    if not run.stop_confirmed or run.state not in {"failed", "blocked", "canceled"}:
        _fail("retry_not_allowed", "Retry requires a terminal, stop-confirmed prior attempt", 3)
    if control["body"].get("retry_count", 0) >= 2 or run.attempt >= 3:
        _fail("retry_limit", "At most two verified transient retries are allowed", 3)
    observed = local_runtime.observe(_request(req, "dispatch_execution", {"run_id": run.run_id,
        "context_ref": control["body"]["context_ref"]}))
    if observed.get("status") != "terminal" or observed.get("stop_confirmed") is not True or \
            observed.get("retryable") is not True or observed.get("failure_class") not in {
                "transient_network", "provider_unavailable", "rate_limited"}:
        _fail("retry_not_allowed", "Runtime has no verified transient failure classification", 3)
    attempt_id = str(uuid.uuid5(uuid.UUID(run.run_id), "pmt-f8-retry:" +
        str(observed.get("receipt_sha256"))))
    retried = _call(controller.state_port, _request(req, "retry_execution", {
        "run_id": run.run_id, "expected_run_revision": run.revision, "reason": "transient"},
        request_id=attempt_id), "retry_execution")
    body = dict(control["body"])
    body.update(stage="retry_queued", retry_count=body.get("retry_count", 0) + 1,
        retry_run_id=retried["run_id"], retry_reason=observed["failure_class"],
        retry_receipt_ref=observed.get("receipt_ref"), locks_retained=False)
    state = _persist(db, req, run.run_id, req["scope_id"], control["source_hash"], body,
        expected_revision=control["revision"], event_name="control.retry_decided",
        request_suffix="retry:" + attempt_id)
    retry_gate = {"schema_version": 1, "run_id": retried["run_id"], "scope_id": req["scope_id"],
        "stage": "retry_context_required", "retry_from_run_id": run.run_id,
        "expected_source_hash": control["source_hash"], "source_hash": control["source_hash"],
        "retry_receipt_ref": observed.get("receipt_ref"), "retry_count": body["retry_count"],
        "locks_retained": True}
    _persist(db, req, retried["run_id"], req["scope_id"], control["source_hash"], retry_gate,
        expected_revision=0, event_name="control.retry_decided", request_suffix="retry-gate:" + attempt_id)
    result = {"action": ControlAction("wait", retried["run_id"], "new_attempt_needs_fresh_context_and_reuse_check",
                refs={"previous_run_id": run.run_id, "previous_control_ref": _control_ref(
                    req["scope_id"], run.run_id, state["revision"]),
                    "expected_source_hash": control["source_hash"]}, locks_retained=False).to_dict(),
              "control_ref": state["control_ref"], "previous_run_id": run.run_id,
              "run_id": retried["run_id"], "attempt": retried["attempt"],
              "retry_count": body["retry_count"], "locks_retained": False,
              "notification": {"status": "query_only"}}
    return _complete_response(db, req, run.run_id, req["scope_id"], body, result)


def _advance(db, req, controller, local_runtime):
    replay = _read_port_replay(controller.state_port, req)
    if replay is not None:
        return replay
    payload = req.get("payload", {})
    allowed = {"run_id", "context_ref", "reuse_body_ref", "retry", "cancel", "previous_run_id"}
    extra = set(payload) - allowed
    if extra:
        _fail("unknown_fields", "Unsupported execution control fields", details={"fields": sorted(extra)})
    run_id = _uuid(payload.get("run_id"), "run_id")
    scope_id = req.get("scope_id")
    run = controller.read_run(req, run_id)
    control = _get_control(db, req, run_id)
    if control and control["scope_id"] != scope_id:
        _fail("scope_conflict", "Execution control belongs to another scope", 3)
    if run.state == "queued":
        prepared = _call(controller.state_port, _request(req, "prepare_execution", {
            "run_id": run_id, "expected_run_revision": run.revision},
            request_id=str(uuid.uuid5(uuid.UUID(req["request_id"]), "pmt-f8-prepare:" + run_id))), "prepare_execution")
        run = controller.read_run(req, run_id)
        if run.state == "queued":
            body = dict(control["body"]) if control else {"schema_version": 1, "run_id": run_id,
                "scope_id": scope_id, "retry_count": 0}
            body.update(stage="waiting", reason_code=prepared.get("waiting_reason", "execution_queued"),
                        locks_retained=True)
            source_hash = body.get("source_hash") or fingerprint({"run_id": run_id, "queued": True})
            state = _persist(db, req, run_id, scope_id, source_hash, body,
                expected_revision=control["revision"] if control else 0,
                event_name="control.state_observed", request_suffix="queue-wait")
            result = {"action": ControlAction("wait", run_id, body["reason_code"],
                        next_poll_at=_next_poll(controller.clock, body)).to_dict(),
                      "control_ref": state["control_ref"], "run_state": run.state,
                      "locks_retained": True, "notification": {"status": "query_only"}}
            return _complete_response(db, req, run_id, scope_id, body, result)
    if run.state in {"failed", "blocked", "canceled"} and payload.get("retry") is True:
        return _retry_terminal(db, req, controller, local_runtime, run, control)
    if run.state in {"failed", "blocked", "canceled", "succeeded"}:
        return _terminal_advance(db, req, controller, local_runtime, run, control)

    context_ref = payload.get("context_ref") or (control["body"].get("context_ref") if control else None)
    reuse_ref = payload.get("reuse_body_ref") or (control["body"].get("reuse_decision_ref") if control else None)
    if not isinstance(context_ref, dict):
        body = dict(control["body"]) if control else {"schema_version": 1, "run_id": run_id,
            "scope_id": scope_id, "retry_count": 0}
        body.update(stage="waiting_context", reason_code="context_ref_required", locks_retained=True)
        source_hash = body.get("source_hash") or fingerprint({"run_id": run_id, "revision": run.revision})
        state = _persist(db, req, run_id, scope_id, source_hash, body,
            expected_revision=control["revision"] if control else 0,
            event_name="control.state_observed", request_suffix="context-required")
        result = {"action": ControlAction("wait", run_id, "verified_context_required",
                    locks_retained=True).to_dict(), "control_ref": state["control_ref"],
                  "run_state": run.state, "locks_retained": True,
                  "notification": {"status": "query_only", "reason": "context_ref_required"}}
        return _complete_response(db, req, run_id, scope_id, body, result)
    context = controller.read_context(req, run, context_ref)
    context_hash = context.get("source_hash") or context_ref.get("source_hash")
    if not context.get("valid"):
        body = dict(control["body"]) if control else {"schema_version": 1, "run_id": run_id, "scope_id": scope_id}
        body.update(stage="review_required", context_ref=context_ref, source_hash=context_hash,
                    reason_code=context.get("reason", "context_unverified"), locks_retained=True)
        state = _persist(db, req, run_id, scope_id, context_hash, body,
            expected_revision=control["revision"] if control else 0,
            event_name="control.reconcile_required", request_suffix="context-review")
        result = {"action": ControlAction("review-needed", run_id, body["reason_code"],
                    refs={"context_ref": context_ref}).to_dict(), "control_ref": state["control_ref"],
                  "run_state": run.state, "locks_retained": True,
                  "notification": {"status": "query_only", "reason": body["reason_code"]}}
        return _complete_response(db, req, run_id, scope_id, body, result)
    if control and control["body"].get("retry_from_run_id") and control["body"].get("expected_source_hash"):
        expected_source_hash = control["body"].get("expected_source_hash")
        if context_hash != expected_source_hash:
            proof = "pmt-control-retry-source-mismatch:" + fingerprint({"run_id": run_id,
                "expected_source_hash": expected_source_hash, "current_source_hash": context_hash})
            _call(controller.state_port, _request(req, "reconcile_execution", {
                "run_id": run_id, "expected_run_revision": run.revision, "stopped": False,
                "not_started": True, "actual_state": "blocked", "evidence_refs": [proof]},
                request_id=str(uuid.uuid5(uuid.UUID(req["request_id"]), "retry-source-mismatch:" + run_id))),
                "reconcile_execution")
            run = controller.read_run(req, run_id)
            body = dict(control["body"])
            body.update(stage="review_required", reason_code="retry_source_changed", locks_retained=False,
                        preflight_evidence_ref=proof)
            state = _persist(db, req, run_id, scope_id, control["source_hash"], body,
                expected_revision=control["revision"], event_name="control.reconcile_required",
                request_suffix="retry-source-changed")
            result = {"action": ControlAction("review-needed", run_id, body["reason_code"],
                        refs={"evidence_ref": proof, "retry_from_run_id": body.get("retry_from_run_id")},
                        locks_retained=False).to_dict(), "control_ref": state["control_ref"],
                      "run_state": run.state, "locks_retained": False,
                      "notification": {"status": "query_only", "reason": "retry_source_changed"}}
            return _complete_response(db, req, run_id, scope_id, body, result)
    previous_run_id = payload.get("previous_run_id")
    if previous_run_id is not None:
        previous_run_id = _uuid(previous_run_id, "previous_run_id")
        previous = _get_control(db, req, previous_run_id)
        if not previous or previous["body"].get("retry_run_id") != run_id:
            _fail("retry_link_invalid", "Retry run is not linked to its verified prior attempt", 3)
    reuse = controller.read_reuse(req, run, reuse_ref)
    if not reuse.get("valid"):
        body = {**(control["body"] if control else {}), "schema_version": 1, "run_id": run_id,
                "scope_id": scope_id, "context_ref": context_ref, "source_hash": context_hash,
                "stage": "review_required", "reason_code": reuse.get("reason", "reuse_decision_unverified"),
                "locks_retained": True}
        state = _persist(db, req, run_id, scope_id, context_hash, body,
            expected_revision=control["revision"] if control else 0,
            event_name="control.reconcile_required", request_suffix="reuse-review")
        result = {"action": ControlAction("review-needed", run_id, body["reason_code"],
                    refs={"context_ref": context_ref}).to_dict(), "control_ref": state["control_ref"],
                  "run_state": run.state, "locks_retained": True,
                  "notification": {"status": "query_only", "reason": body["reason_code"]}}
        return _complete_response(db, req, run_id, scope_id, body, result)
    if payload.get("cancel") is True:
        return _cancel_control(db, req, controller, local_runtime, run, context, reuse, control)
    if reuse.get("status") == "reusable":
        return _reuse_result(db, req, controller, run, context, reuse, control)
    if reuse.get("status") == "active_other_run":
        body = {**(control["body"] if control else {}), "schema_version": 1, "run_id": run_id,
                "scope_id": scope_id, "context_ref": context_ref, "source_hash": context_hash,
                "reuse_decision_ref": reuse.get("decision_ref"), "stage": "waiting_reuse",
                "reason_code": "reuse_key_owned_by_another_active_run", "locks_retained": True}
        state = _persist(db, req, run_id, scope_id, context_hash, body,
            expected_revision=control["revision"] if control else 0,
            event_name="control.state_observed", request_suffix="reuse-wait")
        result = {"action": ControlAction("wait", run_id, body["reason_code"],
                    next_poll_at=_next_poll(controller.clock, body)).to_dict(),
                  "control_ref": state["control_ref"], "run_state": run.state,
                  "locks_retained": True, "notification": {"status": "query_only"}}
        return _complete_response(db, req, run_id, scope_id, body, result)
    if reuse.get("status") not in {"active", "claimed"}:
        body = {**(control["body"] if control else {}), "schema_version": 1, "run_id": run_id,
                "scope_id": scope_id, "context_ref": context_ref, "source_hash": context_hash,
                "reuse_decision_ref": reuse.get("decision_ref"), "stage": "review_required",
                "reason_code": "reuse_decision_not_executable", "locks_retained": True}
        state = _persist(db, req, run_id, scope_id, context_hash, body,
            expected_revision=control["revision"] if control else 0,
            event_name="control.reconcile_required", request_suffix="reuse-nonexecutable")
        result = {"action": ControlAction("review-needed", run_id, body["reason_code"],
                    refs={"decision_ref": reuse.get("decision_ref")}).to_dict(),
                  "control_ref": state["control_ref"], "run_state": run.state,
                  "locks_retained": True, "notification": {"status": "query_only"}}
        return _complete_response(db, req, run_id, scope_id, body, result)

    control = _get_control(db, req, run_id)
    control_body = control["body"] if control else None
    if control_body and control_body.get("stage") in {"main_action_pending", "cancel_action_pending"}:
        if isinstance(control_body.get("action_response_request_id"), str):
            reason = "original_native_action_response_pending"
            result = {"action": ControlAction("review-needed", run_id, reason,
                nonce=control_body.get("action_nonce"), refs={
                    "original_request_id": control_body["action_response_request_id"],
                    "context_ref": context_ref,
                    "prompt_sha256": control_body.get("prompt_sha256")},
                locks_retained=True).to_dict(),
                "control_ref": _control_ref(scope_id, run_id, control["revision"]),
                "run_state": run.state, "locks_retained": True,
                "notification": {"status": "query_only", "reason": reason}}
            return _complete_response(db, req, run_id, scope_id, control_body, result)
        if control_body.get("pending_action_unavailable") is True:
            reason = "pending_action_response_unavailable"
            result = {"action": ControlAction("review-needed", run_id, reason,
                nonce=control_body.get("action_nonce"),
                refs={"context_ref": context_ref,
                      "action_response_request_id": control_body.get("action_response_request_id")},
                locks_retained=True).to_dict(),
                "control_ref": _control_ref(scope_id, run_id, control["revision"]),
                "run_state": run.state, "locks_retained": True,
                "notification": {"status": "query_only", "reason": reason}}
            return _complete_response(db, req, run_id, scope_id, control_body, result)
        action = control_body.get("action")
        if not isinstance(action, dict):
            _fail("execution_control_corrupt", "Pending main action metadata is unavailable", 4)
        result = {"action": action, "control_ref": _control_ref(scope_id, run_id, control["revision"]),
                  "run_state": run.state, "locks_retained": True,
                  "notification": _pending_notice(control_body)}
        return _complete_response(db, req, run_id, scope_id, control_body, result)

    runtime_request = _request(req, "dispatch_execution", {"run_id": run_id, "context_ref": context_ref})
    dispatch_id = str(uuid.uuid5(uuid.UUID(run_id), "pmt-f8-dispatch:" + context_ref["projection_hash"]))
    runtime_request["request_id"] = dispatch_id
    runtime_observation = local_runtime.observe(runtime_request)
    if runtime_observation.get("status") == "unknown":
        return _reconcile_unknown(db, req, controller, run, context, reuse, control, runtime_observation)
    if runtime_observation.get("status") == "terminal":
        return _observation_response(db, req, controller, local_runtime, run, context, reuse, control,
                                     runtime_observation)
    if runtime_observation.get("status") == "awaiting_main_action":
        if not control_body or control_body.get("stage") != "main_action_pending":
            return _reconcile_unknown(db, req, controller, run, context, reuse, control, runtime_observation)
    elif runtime_observation.get("status") == "not_dispatched":
        capability = local_runtime.capabilities({"route": run.route})
        if capability.get("state") != "supported":
            return _block_before_dispatch(db, req, controller, run, context, reuse, control,
                                          capability.get("reason", "runner_unsupported"))
        if control_body and control_body.get("stage") == "dispatch_pending":
            nonce = control_body["action_nonce"]
            dispatch_id = control_body["dispatch_request_id"]
        else:
            nonce = str(uuid.uuid5(uuid.UUID(run_id), "pmt-f8-native-action:" + dispatch_id))
            control_body = _base_body(run, context, reuse, control)
            control_body.update(stage="dispatch_pending", action_nonce=nonce,
                dispatch_request_id=dispatch_id,
                action_kind="main-native-call" if capability.get("kind") == "main_native_call" else "local-cli",
                capability_ref=capability.get("capability_ref"), locks_retained=True)
            _persist(db, req, run_id, scope_id, context_hash, control_body,
                expected_revision=control["revision"] if control else 0,
                event_name="control.observation_started", request_suffix="dispatch-intent")
        runtime_request = _request(req, "dispatch_execution", {"run_id": run_id, "context_ref": context_ref},
                                   request_id=dispatch_id)
        try:
            envelope, code = local_runtime.dispatch(runtime_request)
            if code or not envelope.get("ok"):
                issue = envelope.get("error") or {}
                raise PmtError(issue.get("code", "runner_dispatch_failed"), "Local runner dispatch failed",
                               code or 3, bool(issue.get("retryable")), issue.get("details"))
            dispatched = envelope.get("result") or {}
        except PmtError as error:
            latest = controller.read_run(req, run_id)
            current_control = _get_control(db, req, run_id)
            body = dict(current_control["body"])
            body.update(stage="reconcile_required" if latest.state in {"starting", "running", "reconciling"}
                        else "review_required", reason_code=error.code, locks_retained=True)
            state = _persist(db, req, run_id, scope_id, context_hash, body,
                expected_revision=current_control["revision"], event_name="control.reconcile_required",
                request_suffix="dispatch-error:" + error.code)
            result = {"action": ControlAction("review-needed", run_id, error.code, nonce=nonce,
                        locks_retained=True).to_dict(), "control_ref": state["control_ref"],
                      "run_state": latest.state, "locks_retained": True,
                      "notification": {"status": "query_only", "reason": error.code}}
            return _complete_response(db, req, run_id, scope_id, body, result)
        latest = controller.read_run(req, run_id)
        native_action = dispatched.get("main_action")
        current_control = _get_control(db, req, run_id)
        body = dict(current_control["body"])
        if isinstance(native_action, dict):
            action = {"kind": "main-native-call", "run_id": run_id,
                "expected_run_revision": latest.revision, "action_nonce": nonce,
                "context_ref": context_ref, "prompt_sha256": native_action.get("prompt_sha256"),
                "capability_ref": capability.get("capability_ref"),
                "directive_ref": native_action.get("directive_ref"), "agent": native_action.get("agent"),
                "provider": native_action.get("provider"), "model": native_action.get("model"),
                "instruction": native_action.get("instruction"),
                "return_contract": native_action.get("return_contract")}
            expected_batch_ref = run.intent.get("batch_ref")
            reported_batch_ref = native_action.get("batch_ref")
            if expected_batch_ref:
                members = native_action.get("members")
                group_prompt = native_action.get("group_prompt")
                group_prompt_sha = native_action.get("group_prompt_sha256")
                context_refs = native_action.get("group_context_refs")
                member_runs = [item.get("run_id") for item in members if isinstance(item, dict)] \
                    if isinstance(members, list) else []
                member_steps = [item.get("step_id") for item in members if isinstance(item, dict)] \
                    if isinstance(members, list) else []
                actual_prompt_sha = hashlib.sha256(group_prompt.encode("utf-8")).hexdigest() \
                    if isinstance(group_prompt, str) else None
                if (reported_batch_ref != expected_batch_ref
                        or native_action.get("batch_report_schema") != "pmt-batch-report-v1"
                        or not isinstance(members, list) or len(members) < 2
                        or len(member_runs) != len(members) or len(set(member_runs)) != len(members)
                        or len(member_steps) != len(members) or len(set(member_steps)) != len(members)
                        or member_runs[0] != run_id
                        or members[0].get("context_ref") != context_ref
                        or context_refs != [item.get("context_ref") for item in members]
                        or not isinstance(group_prompt, str) or not group_prompt
                        or group_prompt_sha != actual_prompt_sha
                        or native_action.get("prompt_sha256") != group_prompt_sha
                        or native_action.get("source_hash") != context_hash
                        or native_action.get("physical_slots") != 1
                        or not isinstance(native_action.get("scope_union_sha256"), str)
                        or len(native_action["scope_union_sha256"]) != 64
                        or not isinstance(native_action.get("group_prompt_accounting"), dict)):
                    _fail("native_batch_action_invalid",
                          "The native action does not carry the complete current F9 group contract", 3)
                action.update({key: native_action[key] for key in (
                    "batch_ref", "batch_report_schema", "members", "group_context_refs", "group_prompt",
                    "group_prompt_sha256", "source_hash", "scope_union_sha256", "physical_slots",
                    "group_prompt_accounting")})
            elif reported_batch_ref is not None:
                _fail("native_batch_action_unexpected", "An ungrouped run received an F9 group action", 3)
            body.update(stage="main_action_pending", action=action,
                        prompt_sha256=action.get("prompt_sha256"), locks_retained=True)
            state = _persist(db, req, run_id, scope_id, context_hash, body,
                expected_revision=current_control["revision"], event_name="control.observation_started",
                request_suffix="main-action-ready")
            result = {"action": action, "control_ref": state["control_ref"], "run_state": latest.state,
                      "locks_retained": True, "notification": {"status": "query_only"}}
            return _complete_response(db, req, run_id, scope_id, body, result)
        if latest.state == "reconciling" or dispatched.get("state") == "reconciling":
            return _reconcile_unknown(db, req, controller, latest, context, reuse, current_control,
                {"status": "unknown", "reason": dispatched.get("reason", "dispatch_outcome_unknown"),
                 "receipt_ref": dispatched.get("receipt_ref")})
        observed = local_runtime.observe(runtime_request)
        if observed.get("status") == "unknown" and dispatched.get("state") == "running":
            observed = {"run_id": run_id, "status": "running", "run_state": latest.state,
                        "runner_kind": dispatched.get("runner_kind"), "receipt_ref": None,
                        "handle_ref": dispatched.get("handle_ref"), "stop_confirmed": False,
                        "reason": "startup_pending"}
        return _observation_response(db, req, controller, local_runtime, latest, context, reuse,
                                     _get_control(db, req, run_id), observed)
    return _observation_response(db, req, controller, local_runtime, run, context, reuse, control,
                                 runtime_observation)


def _acknowledge(db, req, controller):
    replay = _read_port_replay(controller.state_port, req)
    if replay is not None:
        return replay
    payload = req.get("payload", {})
    allowed = {"control_ref", "action_nonce", "outcome", "handle_ref", "run_id",
               "expected_run_revision", "verified_trace_ref"}
    if set(payload) - allowed:
        _fail("unknown_fields", "Unsupported native action acknowledgement fields",
              details={"fields": sorted(set(payload) - allowed)})
    run_id = _uuid(payload.get("run_id"), "run_id")
    action_nonce = _uuid(payload.get("action_nonce"), "action_nonce")
    ref = payload.get("control_ref")
    if not isinstance(ref, dict) or set(ref) != {"kind", "id", "scope_id", "revision"} or \
            ref.get("kind") != _CONTROL_KIND or ref.get("id") != run_id or ref.get("scope_id") != req.get("scope_id") or \
            type(ref.get("revision")) is not int or ref["revision"] < 1:
        _fail("control_ref_invalid", "Action acknowledgement control ref is invalid", 3)
    control = _get_control(db, req, run_id)
    if control is None:
        _fail("execution_control_not_found", "Native action control state is unavailable", 3)
    body = control["body"]
    action_acks = body.get("action_acks") if isinstance(body.get("action_acks"), dict) else {}
    prior_ack = action_acks.get(action_nonce)
    from ..db import semantic_request_fingerprint
    ack_request_fingerprint = semantic_request_fingerprint(req)
    if prior_ack:
        if (prior_ack.get("request_fingerprint") == ack_request_fingerprint and
                prior_ack.get("nonce") == action_nonce and prior_ack.get("outcome") == payload.get("outcome") and
                prior_ack.get("handle_ref") == payload.get("handle_ref")):
            result = {"control_ref": _control_ref(req["scope_id"], run_id, control["revision"]),
                      "run_id": run_id, "acknowledged": True, "replayed": True,
                      "run_state": prior_ack.get("run_state"), "locks_retained": True}
            return _store_response(db, req, result, run_id=run_id, scope_id=req["scope_id"],
                                   stage=body.get("stage"), operation=req.get("operation"))
        _fail("control_action_conflict", "A different acknowledgement already consumed this action nonce", 3)
    cancel_action = body.get("stage") == "cancel_action_pending" and body.get("action_kind") == "main-native-cancel"
    expected_stage = "cancel_action_pending" if cancel_action else "main_action_pending"
    if (body.get("stage") != expected_stage or body.get("action_nonce") != action_nonce or
            ref["revision"] != control["revision"]):
        _fail("control_action_stale", "Action nonce or control revision is no longer pending", 3)
    outcome = payload.get("outcome")
    if outcome not in {"started", "not_started", "unknown", "cancel_requested"}:
        _fail("control_ack_invalid", "outcome is not supported for this pending control action")
    run = controller.read_run(req, run_id)
    expected_run_revision = payload.get("expected_run_revision")
    pending_run_revision = (body.get("action") or {}).get("expected_run_revision")
    if type(expected_run_revision) is not int or type(pending_run_revision) is not int or \
            expected_run_revision != pending_run_revision:
        _fail("execution_revision_conflict", "Native action acknowledgement has a stale run revision", 3,
              {"expected_revision": pending_run_revision, "received_revision": expected_run_revision})
    if cancel_action:
        if run.state != "cancel_requested" or run.revision != pending_run_revision:
            _fail("control_action_run_state_conflict", "Native cancel action no longer matches the run state", 3)
    elif not (run.state == "starting" and run.revision == pending_run_revision or
              run.state == "running" and run.revision == pending_run_revision + 1 and outcome == "started" or
              run.state == "reconciling" and run.revision == pending_run_revision + 1 and outcome == "unknown"):
        _fail("control_action_run_state_conflict", "Native action can only attach while the run is starting", 3)
    context = controller.read_context(req, run, body.get("context_ref"))
    if not context.get("valid") or context.get("source_hash") != body.get("source_hash"):
        _fail("context_source_stale", "Current F5 context no longer matches the pending native action", 3)
    reuse = controller.read_reuse(req, run, body.get("reuse_decision_ref"))
    if not reuse.get("valid") or reuse.get("status") not in {"active", "claimed"}:
        _fail("reuse_decision_stale", "Current F6 decision no longer authorizes this action", 3)
    evidence_refs = []
    handle = None
    if cancel_action and outcome == "cancel_requested":
        handle_ref = body.get("action", {}).get("handle_ref")
        if not isinstance(handle_ref, dict) or not isinstance(handle_ref.get("id"), str):
            _fail("native_cancel_ref_invalid", "Pending cancel action lost its handle ref", 4)
    elif outcome == "started" and not cancel_action:
        handle_ref = payload.get("handle_ref")
        if not isinstance(handle_ref, dict) or set(handle_ref) != {"kind", "id", "provider_ref"} or \
                handle_ref.get("kind") != "native_handle" or not all(
                    isinstance(handle_ref.get(key), str) and handle_ref[key].strip() and len(handle_ref[key]) <= 256
                    for key in ("id", "provider_ref")):
            _fail("native_handle_ref_invalid", "A verified opaque native handle ref is required", 3)
        handle = {"id": handle_ref["id"], "runner_kind": "native",
                  "provider_ref": handle_ref["provider_ref"], "action_nonce": action_nonce}
        attached = _call(controller.state_port, _request(req, "attach_execution_handle", {
            "run_id": run_id, "expected_run_revision": expected_run_revision, "handle": handle},
            request_id=str(uuid.uuid5(uuid.UUID(action_nonce), "pmt-f8-attach-native-handle"))),
            "attach_execution_handle")
        run = controller.read_run(req, run_id)
        if attached.get("state") != "running" or run.state != "running":
            _fail("native_action_ack_unconfirmed", "Phase-2 did not confirm the actual native handle", 3)
    elif outcome == "unknown":
        evidence_refs = ["pmt-control-native-ack:" + action_nonce]
        _call(controller.state_port, _request(req, "reconcile_execution", {
            "run_id": run_id, "expected_run_revision": expected_run_revision, "stopped": False,
            "not_started": False, "evidence_refs": evidence_refs},
            request_id=str(uuid.uuid5(uuid.UUID(action_nonce), "pmt-f8-native-ack-unknown"))),
            "reconcile_execution")
        run = controller.read_run(req, run_id)
    else:
        _fail("native_not_started_unverified", "A caller assertion cannot prove native launch did not occur", 3)

    body = dict(body)
    ack_stage = "cancel_wait" if cancel_action and outcome == "cancel_requested" else \
                "running" if outcome == "started" else "reconcile_required"
    ack_record = {"nonce": action_nonce, "outcome": outcome,
                  "handle_ref": payload.get("handle_ref"), "run_state": run.state,
                  "request_fingerprint": ack_request_fingerprint}
    action_acks[action_nonce] = ack_record
    body.update(stage=ack_stage, action_acks=action_acks, action_ack=ack_record,
        handle_ref=payload.get("handle_ref") or body.get("handle_ref"), locks_retained=True,
        reason_code="native_handle_acknowledged" if outcome == "started" else "native_start_outcome_unknown")
    state = _persist(db, req, run_id, req["scope_id"], context["source_hash"], body,
        expected_revision=control["revision"],
        event_name="control.state_observed" if outcome == "started" else "control.reconcile_required",
        request_suffix="native-ack:" + outcome + ":" + str(action_nonce))
    result = {"control_ref": state["control_ref"], "run_id": run_id,
              "acknowledged": outcome in {"started", "cancel_requested"}, "reconcile_required": outcome == "unknown",
              "run_state": run.state, "handle_ref": payload.get("handle_ref"),
              "locks_retained": True,
              "action": ControlAction("wait" if outcome in {"started", "cancel_requested"} else "review-needed", run_id,
                  "native_cancel_acknowledged_stop_unconfirmed" if outcome == "cancel_requested" else
                  "native_handle_attached" if outcome == "started" else "native_start_outcome_unknown",
                  next_poll_at=_next_poll(controller.clock, body) if outcome in {"started", "cancel_requested"} else None,
                  refs={"context_ref": context["context_ref"], "handle_ref": payload.get("handle_ref")}).to_dict()}
    return _complete_response(db, req, run_id, req["scope_id"], body, result)


def execute_file(db, req):
    from ..service import response
    prior_repository = getattr(db, "control_state_repository", None)
    db.control_state_repository = SQLiteControlStateRepository(db)
    try:
        port = LocalStore(db)
        runtime = LocalExecutionRuntime(port)
        controller = ExecutionController(port, runtime, port, port, port)
        if req.get("operation") == "advance_execution_control":
            return _advance(db, req, controller, runtime)
        if req.get("operation") == "acknowledge_execution_action":
            return _acknowledge(db, req, controller)
        return response(req.get("request_id"), error={"code": "operation_unavailable",
                       "message": "Execution control operation is unavailable", "retryable": False}), 2
    except PmtError as error:
        diagnostics = getattr(db, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.emit("control.operation_failed", request_id=req.get("request_id"),
                operation=req.get("operation"), scope_id=req.get("scope_id"),
                outcome="error", error_code=error.code, retryable=error.retryable,
                reason_code=error.code)
        return response(req.get("request_id"), error=error.as_dict()), error.exit_code
    except Exception as error:
        diagnostics = getattr(db, "diagnostics", None)
        if diagnostics is not None:
            diagnostics.emit("control.internal_error", request_id=req.get("request_id"),
                operation=req.get("operation"), scope_id=req.get("scope_id"),
                outcome="error", error_code="execution_control_internal_error",
                reason_code=type(error).__name__)
        error = PmtError("execution_control_internal_error", "Execution control failed safely", 5)
        return response(req.get("request_id"), error=error.as_dict()), error.exit_code
    finally:
        if prior_repository is None:
            try:
                del db.control_state_repository
            except AttributeError:
                pass
        else:
            db.control_state_repository = prior_repository


def execute_with_ports(req, *, state_port, local_runtime, control_state_repository,
                       context_port=None, reuse_port=None, result_port=None, clock=None,
                       notifier=None, display=None, diagnostics=None):
    """Run the existing F8 controller with injected Host state and local effects.

    Hosted callers must supply both ports. This entry point never constructs a
    LocalStore or opens SQLite, and it fails closed without a remote control
    state repository.
    """
    from ..service import response
    if (state_port is None or control_state_repository is None or local_runtime is None
            or not callable(getattr(state_port, "execute", None))
            or not callable(getattr(state_port, "get_request_result", None))):
        issue = PmtError("controller_port_unavailable", "Hosted control requires complete state and runtime ports", 3)
        return response(req.get("request_id"), error=issue.as_dict()), issue.exit_code
    if not all(callable(getattr(control_state_repository, name, None))
               for name in ("get", "compare_and_set", "complete_response")):
        issue = PmtError("controller_port_unavailable", "Hosted control state CAS repository is unavailable", 3)
        return response(req.get("request_id"), error=issue.as_dict()), issue.exit_code
    view = _ControlDatabaseView(None, control_state_repository, diagnostics=diagnostics)
    controller = ExecutionController(state_port, local_runtime, context_port, reuse_port,
                                     result_port, clock, notifier, display, hosted_context_binding=True)
    try:
        if req.get("operation") == "advance_execution_control":
            return _advance(view, req, controller, local_runtime)
        if req.get("operation") == "acknowledge_execution_action":
            return _acknowledge(view, req, controller)
        issue = PmtError("operation_unavailable", "Hosted control operation is unavailable", 2)
        return response(req.get("request_id"), error=issue.as_dict()), issue.exit_code
    except PmtError as error:
        if diagnostics is not None:
            diagnostics.emit("control.operation_failed", request_id=req.get("request_id"),
                operation=req.get("operation"), scope_id=req.get("scope_id"), outcome="error",
                error_code=error.code, retryable=error.retryable, reason_code=error.code)
        return response(req.get("request_id"), error=error.as_dict()), error.exit_code
    except Exception as error:
        if diagnostics is not None:
            diagnostics.emit("control.internal_error", request_id=req.get("request_id"),
                operation=req.get("operation"), scope_id=req.get("scope_id"), outcome="error",
                error_code="execution_control_internal_error", reason_code=type(error).__name__)
        issue = PmtError("execution_control_internal_error", "Execution control failed safely", 5)
        return response(req.get("request_id"), error=issue.as_dict()), issue.exit_code


def handle(db, conn, req):
    if req.get("operation") == "read_execution_control":
        return _read_control(db, conn, req)
    _fail("operation_unavailable", "Execution control read operation is unavailable")
