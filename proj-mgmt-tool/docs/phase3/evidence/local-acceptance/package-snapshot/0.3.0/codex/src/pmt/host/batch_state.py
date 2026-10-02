"""Host authorization and metadata projection for the shared F9 batch state."""
from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from contextlib import closing

from ..errors import PmtError
from ..efficiency.source import pin_source, verify_source_pin
from ..efficiency.storage import Phase3Storage
from ..phase2_common import project_scope_id
from ..util import fingerprint

_WORKSPACE_URI = re.compile(r"^pmt://([0-9a-f-]{36})/([0-9a-f]{64})$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _fail(code, message, exit_code=3):
    raise PmtError(code, message, exit_code)


def _uuid(value, field):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("host_input_invalid", f"{field} must be a canonical UUID") from exc
    return value


def _workspace(value, repository_id):
    match = _WORKSPACE_URI.fullmatch(value) if isinstance(value, str) else None
    if not match or match.group(1) != repository_id:
        _fail("workspace_mapping_invalid", "Batch workspace must be a canonical Host workspace URI")
    return value


def _source(extension, conn, scope_id, repository_id, workspace, relative_path, headers):
    pointer = extension._current_source_pointer(conn, {
        "project_id": scope_id, "repository_id": repository_id,
        "canonical_workspace": workspace, "relative_graph_path": relative_path})
    body = pointer["body"]
    if body.get("relative_graph_path") != relative_path:
        _fail("source_conflict", "Batch graph reference differs from the current Host source snapshot")
    resource = extension._read_resource(body.get("graph_resource"), scope_id, headers)
    source = extension._validated_graph_resource(resource, body, {
        "project_id": scope_id, "repository_id": repository_id,
        "canonical_workspace": workspace, "relative_graph_path": relative_path})
    return pointer, source["source_pin"]


def _project_repository(conn, scope_id, repository_id):
    project = conn.execute("SELECT parent_id,body_json FROM scopes WHERE id=? AND kind='project'",
                           (scope_id,)).fetchone()
    repository = conn.execute("SELECT kind FROM scopes WHERE id=?", (repository_id,)).fetchone()
    if not project or not repository or repository["kind"] != "repository":
        _fail("repository_scope_mismatch", "Current Project and Repository scopes are required")
    body = json.loads(project["body_json"] or "{}")
    bound = body.get("repository_id", body.get("repository_scope_id"))
    if project["parent_id"] not in {None, repository_id} or (bound and bound != repository_id) \
            or (project["parent_id"] is None and bound != repository_id):
        _fail("repository_scope_mismatch", "Repository does not match the current Project")


def authorize(extension, conn, req, principal, headers):
    """Validate the current Host actor, owner and source before delegating to Core F9."""
    op, payload = req["operation"], req.get("payload", {})
    scope_id = _uuid(req.get("scope_id"), "scope_id")
    if not isinstance(payload, dict):
        _fail("host_input_invalid", "Batch payload must be an object", 2)
    if op == "prepare_step_batch":
        allowed = {"run_refs", "workspace", "repository_id", "relative_graph_path",
                   "expected_source", "context_budget", "event_id"}
        unsupported = set(payload) - allowed - {"run_id"}
        if unsupported or not {"run_refs", "workspace", "repository_id",
                "relative_graph_path", "expected_source", "event_id"}.issubset(payload):
            raise PmtError("host_input_invalid", "Batch prepare fields are incomplete or unsupported", 2,
                details={"missing_fields": sorted({"run_refs", "workspace", "repository_id",
                    "relative_graph_path", "expected_source", "event_id"} - set(payload)),
                    "unsupported_fields": sorted(unsupported)})
        repository_id = _uuid(payload["repository_id"], "repository_id")
        _project_repository(conn, scope_id, repository_id)
        workspace = _workspace(payload["workspace"], repository_id)
        expected = pin_source(payload["expected_source"])
        if expected.project_id != scope_id or expected.repository_id != repository_id:
            _fail("source_conflict", "Expected SourcePin does not match the authorized Project")
        refs = payload["run_refs"]
        if not isinstance(refs, list) or len(refs) < 2 or len(refs) > 64:
            _fail("batch_input_invalid", "At least two bounded run references are required", 2)
        run_ids = []
        from ..execution.service import _get_run, _step
        run_rows = []
        for ref in refs:
            if not isinstance(ref, dict) or set(ref) != {"run_id", "expected_run_revision"}:
                _fail("batch_input_invalid", "Each run reference must contain only its ID and revision", 2)
            run_id = _uuid(ref["run_id"], "run_id")
            if type(ref["expected_run_revision"]) is not int or ref["expected_run_revision"] < 1:
                _fail("batch_input_invalid", "Run revision must be a positive integer", 2)
            run_ids.append(run_id)
            run = _get_run(conn, run_id)
            step = _step(conn, run["step_id"])
            if (run["owner_session"] != principal.session_id or run["workspace"] != workspace
                    or project_scope_id(conn, step["scope_id"]) != scope_id):
                _fail("ownership_conflict", "Every batch run must belong to this Host session and Project")
            run_rows.append((run, ref))
        if len(set(run_ids)) != len(run_ids):
            _fail("batch_input_invalid", "Run references must be unique", 2)
        if payload.get("run_id") is not None and payload["run_id"] != run_ids[0]:
            _fail("batch_input_invalid", "Internal source lookup must bind the representative run")
        pointer = extension._current_source_pointer(conn, {"project_id": scope_id,
            "repository_id": repository_id, "canonical_workspace": workspace,
            "relative_graph_path": payload["relative_graph_path"]})
        if pointer["body"].get("relative_graph_path") != payload["relative_graph_path"]:
            _fail("source_conflict", "Batch graph reference differs from the current Host source snapshot")
        verify_source_pin(expected, pointer["body"].get("source_pin"))
        # An in-progress revalidation is permitted only for the exact original
        # revisions and complete owner-bound member set already acquired by F9.
        if any(run["state"] != "queued" for run, _ref in run_rows):
            from ..efficiency.batch import binding_for_run
            if any(run["state"] != "starting" for run, _ref in run_rows):
                _fail("revision_conflict", "Every batch run must remain queued or in the same starting batch")
            bound = [binding_for_run(conn, run_id) for run_id in run_ids]
            if (not all(bound) or len({item["body"]["batch_id"] for item in bound}) != 1
                    or set(run_ids) != {member["run_id"] for member in bound[0]["body"]["members"]}
                    or bound[0]["row"]["owner_actor"] != principal.actor
                    or bound[0]["row"]["owner_session"] != principal.session_id):
                _fail("batch_member_already_bound", "Starting runs are not a replay of this owner’s complete batch")
            originals = {item["run_id"]: item["run_revision"]
                         for item in bound[0]["body"].get("original_runs", [])}
            if any(originals.get(run["id"]) != ref["expected_run_revision"]
                   for run, ref in run_rows):
                _fail("revision_conflict", "Batch references do not match the original acquired revisions")
        elif any(run["revision"] != ref["expected_run_revision"] for run, ref in run_rows):
            _fail("revision_conflict", "Every queued batch run must match its current revision")
        return

    fields = {
        "bind_step_batch": {"batch_ref", "parent_run_id", "event_id"},
        "collect_step_batch": {"batch_ref", "parent_run_id", "expected_run_revision", "event_id"},
        "read_step_batch": {"batch_ref", "parent_run_id", "expected_run_revision"},
    }[op]
    if set(payload) != fields:
        _fail("host_input_invalid", f"{op} fields are incomplete or unsupported", 2)
    batch_id = _uuid(payload["batch_ref"], "batch_ref")
    parent_id = _uuid(payload["parent_run_id"], "parent_run_id")
    item = _batch_object(conn, scope_id, batch_id, principal.actor, principal.session_id)
    body = item["body"]
    if body.get("parent_run_ref") != parent_id or body.get("scope_id") != scope_id:
        _fail("batch_binding_not_found", "Batch does not match the selected Parent run")
    from ..execution.service import _get_run
    parent = _get_run(conn, parent_id)
    from ..execution.service import _step
    if parent["owner_session"] != principal.session_id or project_scope_id(
            conn, _step(conn, parent["step_id"])["scope_id"]) != scope_id:
        _fail("ownership_conflict", "Batch Parent run is not owned by this Host session")
    if "expected_run_revision" in payload:
        expected_revision = payload["expected_run_revision"]
        same_completed_collect = (op == "collect_step_batch"
            and body.get("collect_request_id") == req.get("request_id")
            and body.get("status") in {"review_pending", "reconciling", "complete", "canceled"}
            and body.get("runner_report_ref") is not None)
        if (type(expected_revision) is not int or (expected_revision != parent["revision"]
                and not same_completed_collect)):
            _fail("revision_conflict", "Batch Parent run revision is stale")
    workspace = body.get("workspace")
    repository_id = body.get("repository_id")
    _workspace(workspace, repository_id)
    pointer = extension._current_source_pointer(conn, {"project_id": scope_id,
        "repository_id": repository_id, "canonical_workspace": workspace,
        "relative_graph_path": body.get("relative_graph_path")})
    current_pin = pin_source(pointer["body"].get("source_pin"))
    if pointer["body"].get("relative_graph_path") != body.get("relative_graph_path"):
        _fail("source_conflict", "Batch graph reference differs from the current Host source snapshot")
    pinned = body.get("source_pin") or body.get("expected_source_pin")
    if not isinstance(pinned, Mapping):
        _fail("batch_source_unknown", "Batch has no validated SourcePin")
    verify_source_pin(pinned, current_pin)
    if pointer.get("source_hash") != current_pin.source_hash:
        _fail("source_conflict", "Current Host SourcePin changed after batch preparation")
    if body.get("scope_locks_retained") is True and parent["state"] in {
            "starting", "running", "review_pending", "reconciling", "cancel_requested"}:
        locks = conn.execute("SELECT owner_session FROM scope_locks WHERE run_id=?", (parent_id,)).fetchall()
        if not locks or any(row["owner_session"] != principal.session_id for row in locks):
            _fail("scope_not_owned", "The current Host session no longer owns the batch scope union")


def _batch_object(conn, scope_id, batch_id, actor, session):
    from ..efficiency.batch import _object
    item = _object(conn, scope_id, batch_id)
    if not item or (item["row"]["owner_actor"], item["row"]["owner_session"]) != (actor, session):
        _fail("batch_binding_not_found", "Current owner’s batch binding was not found")
    return item


def read(extension, db, conn, req, principal, headers):
    """Return a fresh, bounded metadata projection; never return directives or prompts."""
    authorize(extension, conn, req, principal, headers)
    payload = req["payload"]
    scope_id, batch_id = req["scope_id"], payload["batch_ref"]
    item = _batch_object(conn, scope_id, batch_id, principal.actor, principal.session_id)
    body = item["body"]
    from ..execution.service import _get_run, _step
    parent = _get_run(conn, body["parent_run_ref"])
    source_pointer, current_pin = _source(extension, conn, scope_id, body["repository_id"],
        body["workspace"], body.get("relative_graph_path"), headers)
    members = []
    unknown = []
    from ..steps import handle as steps_handle
    from ..verification import _criteria
    from ..efficiency import context as context_module
    for stored in body.get("members", []):
        run = _get_run(conn, stored["run_id"])
        if run["owner_session"] != principal.session_id or run["step_id"] != stored["step_id"]:
            _fail("batch_member_changed", "A current batch child no longer matches its bound owner")
        spec = conn.execute("SELECT * FROM step_specs WHERE step_id=?", (stored["step_id"],)).fetchone()
        if not spec:
            _fail("batch_member_changed", "A current batch Step specification is unavailable")
        current_criteria = _criteria({"criteria": json.loads(spec["criteria_json"])})
        criteria = [{"id": key, "sha256": digest} for key, digest in sorted(current_criteria.items())]
        if criteria != stored.get("criteria"):
            _fail("batch_criteria_stale", "A batch Step’s current criteria differ from its binding")
        directive_req = {"protocol_version": 1, "operation": "read_step_directive",
            "request_id": str(uuid.uuid5(uuid.UUID(batch_id), "host-batch-directive:" + run["id"])),
            "actor": principal.actor, "session_id": principal.session_id, "scope_id": scope_id,
            "record_id": stored["step_id"], "payload": {"run_id": run["id"],
                "step_id": stored["step_id"]}}
        directive = steps_handle(db, conn, directive_req)
        directive_version = str(spec["directive_version"])
        directive_ok = (directive_version == stored["directive"]["version"]
                        and fingerprint(directive.get("directive")) == stored["directive"]["sha256"])
        if not directive_ok:
            _fail("batch_directive_stale", "A bound Step directive has changed")
        context_req = {"protocol_version": 1, "operation": "read_task_context",
            "request_id": str(uuid.uuid5(uuid.UUID(batch_id), "host-batch-context:" + run["id"])),
            "actor": principal.actor, "session_id": principal.session_id, "scope_id": scope_id,
            "payload": {"project_id": scope_id, "repository_id": body["repository_id"],
                "canonical_workspace": body["workspace"], "relative_graph_path": body["relative_graph_path"],
                "run_id": run["id"], "expected_run_revision": run["revision"],
                "context_ref": stored.get("context_ref")}}
        extension.authorize(conn, context_req, principal)
        checked_context = context_module.handle(db, conn, context_req | {
            "payload": {"context_ref": stored.get("context_ref")}})
        if (checked_context.get("current_authority", {}).get("run_id") != run["id"]
                or checked_context.get("task", {}).get("step_id") != stored["step_id"]
                or checked_context.get("source", {}).get("source_hash") != current_pin.source_hash):
            _fail("batch_context_stale", "A bound F5 context is no longer current for its child")
        omitted = list(checked_context.get("mandatory_omissions") or [])
        projection = checked_context.get("projection") or {}
        included = {part.get("section_id") for part in projection.get("included", [])
                    if isinstance(part, dict)}
        required = {"purpose", "goal", "non_goal", "change_scope", "inputs", "outputs",
                    "criteria", "tests", "logging", "unresolved"}
        if str(stored.get("role", "")).casefold() in {"lower", "worker", "implement", "implementation"}:
            required.update({"method", "autonomy"})
        missing = sorted(required - included)
        context_ok = checked_context.get("incomplete") is not True and not omitted and not missing
        if not context_ok:
            unknown.append({"step_id": stored["step_id"], "reason_code": "batch_context_incomplete",
                            "missing_sections": missing, "mandatory_omissions": omitted})
        members.append({"step_id": stored["step_id"], "run_id": run["id"],
            "role": stored.get("role"), "run_revision": run["revision"], "run_state": run["state"],
            "directive_ref": stored["directive"]["ref"],
            "directive_version": directive_version, "directive_sha256": stored["directive"]["sha256"],
            "directive_current": True, "context_ref": stored.get("context_ref"),
            "context_current": context_ok,
            "context_budget": checked_context.get("budget"), "criteria": criteria})
    if current_pin.source_hash != body.get("source_hash"):
        _fail("source_conflict", "Current SourcePin differs from the prepared batch")
    status = body.get("status", "unknown")
    executable = (status in {"prepared", "running"} and not unknown
                  and len(members) == len(body.get("members", [])))
    return {"batch_ref": {"kind": "batch_binding", "id": batch_id, "scope_id": scope_id,
            "revision": item["row"]["revision"], "source_hash": item["row"]["source_hash"]},
        "status": status, "execution_enabled": executable,
        "parent_run_ref": parent["id"], "parent_run_revision": parent["revision"],
        "source": {"repository_id": body["repository_id"], "project_id": scope_id,
            "canonical_workspace": body["workspace"], "source_hash": current_pin.source_hash,
            "source_pin": current_pin.to_dict()},
        "members": members, "scope_union_sha256": body.get("scope_union_sha256"),
        "physical_slots": body.get("physical_slots", 1),
        "handle_ref": {"id": body["handle_ref"]} if isinstance(body.get("handle_ref"), str) else None,
        "report": {"schema": "pmt-batch-report-v1", "artifact_ref": body.get("runner_report_ref"),
            "sha256": (body.get("parent_container_result") or {}).get("report_sha256")},
        "unknown": unknown}


def verify_current_source(extension, conn, req, headers):
    """Recheck the Host-owned source bytes before the Core batch file operation."""
    payload = req.get("payload", {})
    if req["operation"] == "prepare_step_batch":
        project_id = req["scope_id"]
        repository_id = payload["repository_id"]
        workspace = payload["workspace"]
        relative_path = payload["relative_graph_path"]
        expected = payload["expected_source"]
    else:
        item = _batch_object(conn, req["scope_id"], payload["batch_ref"],
                             req["actor"], req["session_id"])
        body = item["body"]
        project_id, repository_id = req["scope_id"], body["repository_id"]
        workspace, relative_path = body["workspace"], body["relative_graph_path"]
        expected = body.get("source_pin") or body.get("expected_source_pin")
    _pointer, current = _source(extension, conn, project_id, repository_id,
                                workspace, relative_path, headers)
    if expected:
        verify_source_pin(expected, current)


def link_parent_report(extension, db, req, principal, headers):
    """Bind only the stopped parent’s exact submitted report to its P2 run."""
    from ..execution.service import _get_run
    from ..phase2_common import load_json_resource
    from ..resources import check_artifact
    from ..util import utc_now
    payload = req["payload"]
    batch_id, parent_id, scope_id = payload["batch_ref"], payload["parent_run_id"], req["scope_id"]

    def read_binding(conn):
        item = _batch_object(conn, scope_id, batch_id, principal.actor, principal.session_id)
        body = item["body"]
        parent = _get_run(conn, parent_id)
        if (body.get("parent_run_ref") != parent_id or parent["owner_session"] != principal.session_id
                or parent["state"] not in {"review_pending", "reconciling", "cancel_requested", "canceled"}
                or not parent.get("stop_confirmed")):
            _fail("batch_parent_not_stopped", "A current stopped parent result is required")
        try:
            result = json.loads(parent["result_json"]) if parent.get("result_json") else None
        except (TypeError, ValueError, json.JSONDecodeError):
            result = None
        report_ref = result.get("batch_report_ref") if isinstance(result, dict) else None
        report_hash = result.get("batch_report_sha256") if isinstance(result, dict) else None
        if (result.get("batch_ref") != batch_id if isinstance(result, dict) else True):
            _fail("batch_report_conflict", "Parent result is not bound to the current Host batch")
        report = conn.execute("SELECT scope_id,sha256,size_bytes,state FROM artifacts WHERE id=?",
                              (report_ref,)).fetchone() if isinstance(report_ref, str) else None
        metadata = conn.execute("SELECT purpose,owner_device_id,owner_session_id FROM host_resource_metadata WHERE artifact_id=?",
                                (report_ref,)).fetchone() if report_ref else None
        if (not report or report["scope_id"] != scope_id or report["state"] != "ready"
                or report["sha256"] != report_hash or not _SHA256.fullmatch(report_hash or "")
                or not metadata or metadata["purpose"] != "result"
                or metadata["owner_device_id"] != principal.device_id
                or metadata["owner_session_id"] != principal.session_id):
            _fail("batch_report_invalid", "Parent report must be a current owner-published result resource")
        if not check_artifact(db, conn, report_ref).get("valid"):
            _fail("batch_report_invalid", "Parent report resource failed SHA-256 verification")
        report_body = load_json_resource(db, conn, report_ref)
        from ..efficiency.batch import _report_members
        rows, errors = _report_members(body, report_body)
        if errors or set(rows) != {member["step_id"] for member in body.get("members", [])}:
            _fail("batch_report_invalid", "Parent report does not match every current Step and criterion")
        if result.get("receipt_ref") is None or result.get("stop_confirmed") is not True:
            _fail("batch_parent_not_stopped", "Parent result lacks its immutable stop receipt")
        return report_ref, report_hash

    with closing(db.connect()) as conn:
        extension._authorize_now(conn, req, headers)
        artifact_id, report_hash = read_binding(conn)

    with db.write() as conn:
        extension._authorize_now(conn, req, headers)
        item = _batch_object(conn, scope_id, batch_id, principal.actor, principal.session_id)
        parent = _get_run(conn, parent_id)
        try:
            result = json.loads(parent["result_json"]) if parent.get("result_json") else None
        except (TypeError, ValueError, json.JSONDecodeError):
            result = None
        if (item["body"].get("source_hash") is None or parent.get("stop_confirmed") != 1
                or not isinstance(result, dict) or result.get("batch_ref") != batch_id
                or result.get("batch_report_ref") != artifact_id
                or result.get("batch_report_sha256") != report_hash):
            _fail("batch_report_conflict", "Batch or stopped parent report changed before receipt link")
        artifact = conn.execute("SELECT scope_id,sha256,state FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if not artifact or artifact["scope_id"] != scope_id or artifact["state"] != "ready" \
                or artifact["sha256"] != report_hash:
            _fail("batch_report_invalid", "Parent report changed before receipt link")
        conn.execute("INSERT OR IGNORE INTO artifact_refs(artifact_id,owner_type,owner_id,purpose,created_at) "
                     "VALUES(?,?,?,?,?)", (artifact_id, "phase2", parent_id, "batch_report", utc_now()))
