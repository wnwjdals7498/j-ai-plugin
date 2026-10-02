"""Metadata-only publication of an explicitly approved client F4 plan baseline."""
from __future__ import annotations

from contextlib import closing
import json
import re
import uuid

from ..efficiency.source import pin_source, verify_source_pin
from ..efficiency.storage import Phase3Storage
from ..errors import PmtError
from ..planning.graph import validate_graph
from ..phase2_common import event, project_scope_id, require_workspace_claim
from ..util import fingerprint, utc_now
from . import data as host_data
from .auth import identifier


OPERATIONS = frozenset({"publish_client_plan", "read_client_plan"})
READ_OPERATIONS = frozenset({"read_client_plan"})
FILE_OPERATIONS = frozenset({"publish_client_plan"})
WRITE_OPERATIONS = frozenset()
_HASH = re.compile(r"^[0-9a-f]{64}$")
_PLAN_DOCUMENT = "docs/pmt-docs/plan.md"


def _fail(code, message, exit_code=3, details=None, retryable=False):
    raise PmtError(code, message, exit_code, retryable, details)


def _nonempty(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        _fail("client_plan_invalid", f"{name} must be bounded nonempty text", 2)
    return value.strip()


def _payload(req, operation):
    payload = req.get("payload", {})
    expected = ({"plan_id", "project_id"} if operation == "read_client_plan" else {
        "plan_id", "expected_plan_hash", "requirements_version", "plan_version", "repository_id",
        "project_id", "canonical_workspace", "relative_graph_path", "run_id",
        "expected_run_revision", "source_pin", "document_effect_id",
        "expected_document_effect_revision"})
    if not isinstance(payload, dict) or set(payload) != expected:
        _fail("client_plan_invalid", f"{operation} fields are incomplete or unsupported", 2)
    if req.get("scope_id") != payload.get("project_id"):
        _fail("scope_forbidden", "Plan metadata must use the selected Project scope", 3)
    return payload


def authorize(extension, conn, req, principal, headers=None):
    """Authorize the explicit review action and its current run/source binding."""
    operation = req.get("operation")
    if operation not in OPERATIONS:
        _fail("host_operation_forbidden", "Client plan operation is not supported", 3)
    payload = _payload(req, operation)
    principal.require("read" if operation == "read_client_plan" else "review")
    if operation == "read_client_plan":
        identifier(payload["plan_id"], "plan_id")
        project_id = identifier(payload["project_id"], "project_id")
        extension.auth.authorize_scope(conn, principal, project_id)
        return
    principal.require("write")
    principal.require("runtime")
    payload = payload | {"expected_source": pin_source(payload["source_pin"]).to_dict()}
    binding = extension._workspace_binding(conn, dict(req) | {"payload": payload}, principal, mode="execute")
    current = extension._current_source_pointer(conn, binding, required=True)
    verify_source_pin(payload["source_pin"], current["body"].get("source_pin"))


def authorize_step_directive(extension, conn, req, principal):
    """Reject an implement Step if its published Plan no longer names current source."""
    payload = req.get("payload", {})
    if payload.get("kind", "implement") in {"investigate", "experiment"} and payload.get("exploration_approved") is True:
        return
    plan_id = identifier(payload.get("plan_id"), "plan_id")
    project_id = identifier(req.get("scope_id"), "project_id")
    workspace, repository_id, relative = (payload.get("workspace"), payload.get("repository_id"),
        payload.get("relative_graph_path"))
    match = host_data._WORKSPACE_URI.fullmatch(workspace) if isinstance(workspace, str) else None
    if (not match or not isinstance(repository_id, str) or match.group(1) != repository_id
            or not isinstance(relative, str)):
        _fail("plan_scope_conflict", "Implementation Step must use the current canonical source mapping", 3)
    extension.auth.authorize_scope(conn, principal, project_id)
    project = conn.execute("SELECT parent_id,body_json FROM scopes WHERE id=? AND kind='project'",
                           (project_id,)).fetchone()
    project_body = json.loads(project["body_json"] or "{}") if project else {}
    bound_repo = project["parent_id"] or project_body.get("repository_id") or project_body.get("repository_scope_id")
    if bound_repo != repository_id:
        _fail("repository_scope_mismatch", "Plan repository does not match the selected Project", 3)
    pointer = extension._current_source_pointer(conn, {"project_id": project_id,
        "repository_id": repository_id, "canonical_workspace": workspace,
        "relative_graph_path": host_data._relative(relative, "relative_graph_path")})
    pin = pin_source(pointer["body"].get("source_pin"))
    plan = conn.execute("SELECT * FROM plans WHERE id=? AND scope_id=?", (plan_id, project_id)).fetchone()
    artifact = conn.execute("SELECT scope_id,sha256,state FROM artifacts WHERE id=?",
        (plan["artifact_id"],)).fetchone() if plan else None
    graph_ref = pointer["body"].get("graph_resource")
    if (not plan or plan["state"] != "published" or not artifact or artifact["state"] != "ready"
            or artifact["scope_id"] != project_id or not isinstance(graph_ref, dict)
            or plan["artifact_id"] != graph_ref.get("id") or artifact["sha256"] != graph_ref.get("sha256")
            or plan["workspace"] != workspace or plan["relative_path"] != relative
            or plan["graph_version"] != pin.graph_revision or plan["sha256"] != pin.graph_hash
            or plan["baseline_commit"] != pin.reviewed_commit):
        _fail("plan_source_stale", "Published Plan metadata does not match the current Host SourcePin", 3)


def _plan_hash(row):
    return fingerprint({key: row[key] for key in ("id", "scope_id", "artifact_id", "graph_version",
        "requirements_version", "plan_version", "state", "workspace", "relative_path", "sha256",
        "baseline_commit")})


def _prepare(extension, conn, req, principal, headers, *, validate_graph_bytes=True):
    payload = _payload(req, "publish_client_plan")
    plan_id = identifier(payload["plan_id"], "plan_id")
    project_id = identifier(payload["project_id"], "project_id")
    repository_id = identifier(payload["repository_id"], "repository_id")
    run_id = identifier(payload["run_id"], "run_id")
    expected_revision = payload["expected_run_revision"]
    if type(expected_revision) is not int or expected_revision < 1:
        _fail("execution_revision_conflict", "Current positive run revision is required", 3)
    requirements_version = _nonempty(payload["requirements_version"], "requirements_version")
    plan_version = _nonempty(payload["plan_version"], "plan_version")
    prior_hash = payload["expected_plan_hash"]
    if prior_hash is not None and (not isinstance(prior_hash, str) or not _HASH.fullmatch(prior_hash)):
        _fail("client_plan_invalid", "expected_plan_hash must be null or SHA-256", 2)
    effect_id = identifier(payload["document_effect_id"], "document_effect_id")
    effect_revision = payload["expected_document_effect_revision"]
    if type(effect_revision) is not int or effect_revision < 1:
        _fail("client_plan_invalid", "Expected document effect revision must be positive", 2)
    expected_pin = pin_source(payload["source_pin"])
    if (expected_pin.project_id != project_id or expected_pin.repository_id != repository_id
            or expected_pin.source_kind not in {"git", "non_git"}):
        _fail("source_pin_invalid", "Plan requires a known SourcePin for the selected project", 3)
    bind_request = dict(req) | {"payload": dict(payload) | {"expected_source": expected_pin.to_dict()}}
    binding = extension._workspace_binding(conn, bind_request, principal, mode="execute")
    if (binding["project_id"] != project_id or binding["repository_id"] != repository_id
            or binding["canonical_workspace"] != payload["canonical_workspace"]
            or binding["relative_graph_path"] != payload["relative_graph_path"]
            or binding["run"]["id"] != run_id or binding["run"]["revision"] != expected_revision):
        _fail("workspace_authority_stale", "Current run or canonical SourcePin mapping differs", 3)
    require_workspace_claim(extension._db_for(conn, req), conn, bind_request,
        binding["canonical_workspace"], [binding["relative_graph_path"], _PLAN_DOCUMENT])
    pointer = extension._current_source_pointer(conn, binding, required=True)
    current_pin = pin_source(pointer["body"].get("source_pin"))
    verify_source_pin(expected_pin, current_pin)
    resource_ref = pointer["body"].get("graph_resource")
    if not isinstance(resource_ref, dict) or resource_ref.get("purpose") != "graph_snapshot":
        _fail("client_plan_source_mismatch", "Current SourcePin has no immutable graph snapshot ref", 3)
    graph, report = None, None
    if validate_graph_bytes:
        resource = extension._read_resource(resource_ref, project_id, headers)
        source = extension._validated_graph_resource(resource, pointer["body"], binding)
        graph = source["graph"]
        report = validate_graph(graph, project_id, complete=True)
        if (report["sha256"] != current_pin.graph_hash or report["graph_version"] != current_pin.graph_revision
                or report["schema_version"] != current_pin.graph_schema):
            _fail("client_plan_source_mismatch", "Validated graph differs from current SourcePin", 3)

    storage = Phase3Storage(extension._db_for(conn, req))
    effect = storage.get_object("host_local_file_effect", effect_id, project_id,
        principal.actor, principal.session_id, conn=conn)
    if not effect or effect["revision"] != effect_revision or effect["state"] != "completed":
        _fail("client_plan_document_effect_invalid", "Completed owned F4 document effect is required", 3)
    body = effect["body"]
    identity = {"namespace_id": extension.auth.namespace_id, "actor": principal.actor,
        "device_id": principal.device_id, "environment_id": principal.environment_id,
        "session_id": principal.session_id}
    if (body.get("effect_kind") != "document_render" or body.get("phase") != "completed"
            or body.get("host_document_verified") is not False or body.get("owner") != identity
            or body.get("run_id") != run_id or body.get("project_id") != project_id
            or body.get("repository_id") != repository_id
            or body.get("canonical_workspace") != binding["canonical_workspace"]
            or body.get("relative_graph_path") != binding["relative_graph_path"]
            or body.get("target_relative_path") != _PLAN_DOCUMENT
            or not isinstance(body.get("after_source_pin"), dict)):
        _fail("client_plan_document_effect_mismatch", "F4 receipt is not bound to this owner, source and plan document", 3)
    verify_source_pin(expected_pin, pin_source(body["after_source_pin"]))
    coverage_ref = body.get("coverage_ref")
    effect_manifests = body.get("manifest_refs")
    if (not isinstance(coverage_ref, dict) or set(coverage_ref) != {"revision", "certificate_hash", "manifest_set_hash"}
            or not isinstance(effect_manifests, list) or not effect_manifests):
        _fail("client_plan_coverage_unknown", "F4 effect has no complete manifest and coverage receipt", 3)

    # F4 manifests and coverage are immutable Host metadata. Reconstruct the
    # existing source index in memory solely to revalidate the certificate.
    from ..efficiency import graph as graph_module
    manifests, corrupt = graph_module._registered_manifests(conn, project_id)
    if corrupt:
        _fail("client_plan_manifest_corrupt", "Registered F4 manifest metadata is corrupt", 3)
    expected_manifest_refs = []
    stored_manifests = {}
    for item in effect_manifests:
        if not isinstance(item, dict) or set(item) != {"segment_id", "manifest_hash", "revision"}:
            _fail("client_plan_manifest_invalid", "F4 manifest receipt shape is invalid", 3)
        segment_id = identifier(item["segment_id"], "segment_id")
        manifest_hash = item["manifest_hash"]
        if not isinstance(manifest_hash, str) or not _HASH.fullmatch(manifest_hash):
            _fail("client_plan_manifest_invalid", "F4 manifest hash is invalid", 3)
        stored = manifests.get(segment_id)
        if (not stored or stored["revision"] != item["revision"]
                or stored["source_hash"] != current_pin.source_hash
                or stored["manifest"].get("document_path") != _PLAN_DOCUMENT
                or graph_module._manifest_ref_hash(stored["manifest"]) != manifest_hash):
            _fail("client_plan_manifest_stale", "F4 manifest is missing or differs from its receipt", 3)
        expected_manifest_refs.append({"segment_id": segment_id, "manifest_hash": manifest_hash})
        stored_manifests[segment_id] = stored
    coverage_item = storage.get_object("segment_coverage", project_id, project_id,
        graph_module._INDEX_ACTOR, graph_module._INDEX_SESSION, conn=conn)
    if (not coverage_item or coverage_item["revision"] != coverage_ref["revision"]
            or coverage_item["source_hash"] != current_pin.source_hash):
        _fail("client_plan_coverage_stale", "F4 coverage certificate is not current for this SourcePin", 3)
    certificate = coverage_item["body"]
    if (not isinstance(certificate, dict) or certificate.get("coverage_status") != "complete"
            or certificate.get("coverage_source") != "F4_baseline_producer"
            or certificate.get("document_paths") != [_PLAN_DOCUMENT]
            or certificate.get("manifest_set_hash") != coverage_ref["manifest_set_hash"]
            or certificate.get("coverage_certificate_hash") != coverage_ref["certificate_hash"]
            or graph_module.coverage_certificate_fingerprint(certificate) != coverage_ref["certificate_hash"]):
        _fail("client_plan_coverage_invalid", "F4 coverage certificate hash or producer provenance is invalid", 3)
    certificate_refs = certificate.get("manifest_refs")
    if (not isinstance(certificate_refs, list)
            or sorted(certificate_refs, key=lambda x: x.get("segment_id", "")) !=
               sorted(expected_manifest_refs, key=lambda x: x["segment_id"])):
        _fail("client_plan_manifest_mismatch", "F4 coverage certificate does not name its exact manifest set", 3)
    _index_row, index_body = graph_module._get_current_index(
        extension._db_for(conn, req), conn, project_id, current_pin)
    completeness = graph_module._coverage_from_certificate(certificate, index_body, stored_manifests, current_pin)
    if completeness.get("segments") != "complete":
        _fail("client_plan_coverage_incomplete", "F4 metadata does not prove a complete plan baseline", 3,
              {"reason_code": completeness.get("reason")})
    expected_set_hash = graph_module.manifest_set_fingerprint(expected_manifest_refs,
        [_PLAN_DOCUMENT], current_pin)
    if (expected_set_hash != coverage_ref["manifest_set_hash"]
            or completeness.get("certificate_hash") != coverage_ref["certificate_hash"]):
        _fail("client_plan_manifest_mismatch", "F4 coverage refs differ from their verified set", 3)

    prior = conn.execute("SELECT * FROM plans WHERE id=?", (plan_id,)).fetchone()
    prior_hash = _plan_hash(prior) if prior else None
    if prior and prior["scope_id"] != project_id:
        _fail("client_plan_scope_conflict", "Plan ID belongs to another project", 3)
    if prior:
        prior_hash = _plan_hash(prior)
        if prior_hash != payload["expected_plan_hash"]:
            _fail("revision_conflict", "Published plan metadata changed before replacement", 3,
                  {"current_plan_hash": prior_hash})
        active = conn.execute("SELECT id,intent_json FROM execution_runs WHERE state IN "
            "('queued','starting','running','review_pending','reconciling','cancel_requested')").fetchall()
        for run_row in active:
            try:
                intent = json.loads(run_row["intent_json"])
            except (TypeError, ValueError):
                continue
            if run_row["id"] != run_id and intent.get("plan_id") == plan_id:
                _fail("client_plan_in_use", "An active run still consumes this plan version", 3,
                      {"run_id": run_row["id"]})
    elif payload["expected_plan_hash"] is not None:
        _fail("revision_conflict", "Plan metadata does not exist at the expected hash", 3)
    plan_row = {"id": plan_id, "scope_id": project_id, "artifact_id": resource_ref["id"],
        "graph_version": current_pin.graph_revision, "requirements_version": requirements_version,
        "plan_version": plan_version, "state": "published", "workspace": binding["canonical_workspace"],
        "relative_path": binding["relative_graph_path"], "sha256": report["sha256"] if report else current_pin.graph_hash,
        "baseline_commit": current_pin.reviewed_commit}
    plan_row["plan_hash"] = _plan_hash(plan_row)
    return {"binding": binding, "pin": current_pin, "graph": graph, "report": report,
        "resource_ref": resource_ref, "effect": effect, "coverage_ref": coverage_ref,
        "manifest_set_hash": expected_set_hash, "plan_row": plan_row, "prior": prior,
        "prior_hash": prior_hash}


def handle(extension, db, conn, req, principal, headers=None):
    payload = _payload(req, req.get("operation"))
    if req["operation"] == "publish_client_plan":
        raise PmtError("host_plan_transport_required", "Plan publication must run through its short CAS file boundary", 3)
    project_id = payload["project_id"]
    row = conn.execute("SELECT * FROM plans WHERE id=? AND scope_id=?", (payload["plan_id"], project_id)).fetchone()
    if not row:
        _fail("plan_not_found", "Published plan metadata is unavailable", 3)
    artifact = conn.execute("SELECT sha256,size_bytes,state FROM artifacts WHERE id=?", (row["artifact_id"],)).fetchone()
    if not artifact or artifact["state"] != "ready":
        _fail("plan_resource_unavailable", "Plan source resource is unavailable", 3)
    plan = dict(row)
    from .data import _HOST_ACTOR, _system_session
    receipt = Phase3Storage(db).get_object("client_plan_receipt", plan["id"], project_id,
        _HOST_ACTOR, _system_session(extension.auth.namespace_id), conn=conn)
    receipt_body = receipt.get("body") if receipt else None
    current_plan_hash = _plan_hash(plan)
    receipt_valid = (isinstance(receipt_body, dict) and receipt_body.get("plan_hash") == current_plan_hash
        and receipt_body.get("source_pin", {}).get("graph_hash") == plan["sha256"])
    return {"plan_id": plan["id"], "scope_id": plan["scope_id"], "state": plan["state"],
        "requirements_version": plan["requirements_version"], "plan_version": plan["plan_version"],
        "graph_version": plan["graph_version"], "source_hash": plan["sha256"],
        "workspace": plan["workspace"], "relative_path": plan["relative_path"],
        "baseline_commit": plan["baseline_commit"],
        "graph_artifact_ref": {"id": plan["artifact_id"], "sha256": artifact["sha256"],
            "size": artifact["size_bytes"], "scope_id": project_id, "purpose": "graph_snapshot"},
        "plan_hash": current_plan_hash,
        "document_effect_ref": receipt_body.get("document_effect_ref") if receipt_valid else None,
        "manifest_set_hash": receipt_body.get("manifest_set_hash") if receipt_valid else None,
        "source_pin": receipt_body.get("source_pin") if receipt_valid else None,
        "provenance": receipt_body.get("provenance") if receipt_valid else "legacy_plan_metadata",
        "host_document_verified": False}


def execute_file(extension, db, req, principal, headers=None, authorizer=None):
    """Validate client F4 receipts outside the write transaction, then publish plan metadata by CAS."""
    headers = dict(headers or {})
    if req.get("operation") != "publish_client_plan":
        raise PmtError("host_operation_forbidden", "Unsupported Host plan file operation", 3)
    with closing(db.connect()) as conn:
        if authorizer is not None:
            actual, _ = authorizer(conn, req, headers, record_ledger=False)
        else:
            actual, _ = extension._fresh_authorize(conn, req, headers)
        authorize(extension, conn, req, actual, headers)

    # Exact owner-authorized replay is resolved before graph/effect reevaluation.
    # The request fingerprint still binds every publication input.
    prior = db.get_request_result(req["request_id"], req["actor"], req["session_id"], expected_request=req)
    if prior is not None:
        return prior

    with closing(db.connect()) as conn:
        if authorizer is not None:
            actual, _ = authorizer(conn, req, headers, record_ledger=False)
        else:
            actual, _ = extension._fresh_authorize(conn, req, headers)
        _prepare(extension, conn, req, actual, headers)

    def commit(conn, request):
        if authorizer is not None:
            actual, _ = authorizer(conn, request, headers, record_ledger=True)
        else:
            actual, _ = extension._fresh_authorize(conn, request, headers)
        current = _prepare(extension, conn, request, actual, headers, validate_graph_bytes=False)
        plan = current["plan_row"]
        old = conn.execute("SELECT * FROM plans WHERE id=? AND scope_id=?",
                           (plan["id"], plan["scope_id"])).fetchone()
        if (old is None) != (current["prior"] is None):
            _fail("revision_conflict", "Published plan row changed during metadata validation", 3)
        if old and _plan_hash(old) != current["prior_hash"]:
            _fail("revision_conflict", "Published plan metadata changed during validation", 3)
        now = utc_now()
        values = (plan["artifact_id"], plan["graph_version"], plan["requirements_version"],
            plan["plan_version"], plan["state"], plan["workspace"], plan["relative_path"],
            plan["sha256"], plan["baseline_commit"], now)
        if old:
            conn.execute("UPDATE plans SET artifact_id=?,graph_version=?,requirements_version=?,plan_version=?,"
                "state=?,workspace=?,relative_path=?,sha256=?,baseline_commit=?,updated_at=? WHERE id=? AND scope_id=?",
                (*values, plan["id"], plan["scope_id"]))
        else:
            conn.execute("INSERT INTO plans(id,scope_id,artifact_id,graph_version,requirements_version,plan_version,"
                "state,workspace,relative_path,sha256,baseline_commit,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (plan["id"], plan["scope_id"], *values[:-1], now, now))
        from .data import _HOST_ACTOR, _system_session
        internal_actor, internal_session = _HOST_ACTOR, _system_session(extension.auth.namespace_id)
        receipt_storage = Phase3Storage(db)
        previous_receipt = receipt_storage.get_object("client_plan_receipt", plan["id"], plan["scope_id"],
            internal_actor, internal_session, conn=conn)
        receipt_body = {"schema_version": 1, "plan_id": plan["id"], "plan_hash": plan["plan_hash"],
            "source_pin": current["pin"].to_dict(), "graph_artifact_ref": {"id": plan["artifact_id"],
                "sha256": current["resource_ref"]["sha256"], "size": current["resource_ref"]["size"],
                "scope_id": plan["scope_id"], "purpose": "graph_snapshot"},
            "document_effect_ref": {"kind": "host_local_file_effect", "id": current["effect"]["id"],
                "scope_id": plan["scope_id"], "revision": current["effect"]["revision"]},
            "coverage_ref": current["coverage_ref"], "manifest_set_hash": current["manifest_set_hash"],
            "provenance": "client_doc_receipt", "host_document_verified": False}
        receipt_storage.put_object("client_plan_receipt", plan["id"], plan["scope_id"],
            internal_actor, internal_session, current["pin"].source_hash,
            previous_receipt["revision"] if previous_receipt else 0, receipt_body,
            state="published", request_id=request["request_id"],
            event_id=str(uuid.uuid5(uuid.UUID(request["request_id"]), "client-plan-receipt")), conn=conn)
        event(conn, request, "planning.client_plan_published", scope_id=plan["scope_id"],
            record_id=None, payload={"plan_id": plan["id"], "plan_hash": plan["plan_hash"],
                "source_hash": current["pin"].source_hash, "plan_version": plan["plan_version"],
                "requirements_version": plan["requirements_version"],
                "document_effect_id": current["effect"]["id"],
                "manifest_set_hash": current["manifest_set_hash"],
                "provenance": "client_doc_receipt"})
        return {"plan_id": plan["id"], "scope_id": plan["scope_id"], "state": "published",
            "requirements_version": plan["requirements_version"], "plan_version": plan["plan_version"],
            "source_hash": current["pin"].source_hash, "plan_hash": plan["plan_hash"],
            "graph_artifact_ref": {"id": plan["artifact_id"], "sha256": current["resource_ref"]["sha256"],
                "size": current["resource_ref"]["size"], "scope_id": plan["scope_id"],
                "purpose": "graph_snapshot"},
            "document_effect_ref": {"kind": "host_local_file_effect", "id": current["effect"]["id"],
                "scope_id": plan["scope_id"], "revision": current["effect"]["revision"]},
            "manifest_set_hash": current["manifest_set_hash"],
            "provenance": "client_doc_receipt", "host_document_verified": False,
            "replayed": False}
    return db.run_request(req, commit, authorize=(lambda conn, request:
        authorizer(conn, request, headers, record_ledger=True) if authorizer is not None else
        extension._fresh_authorize(conn, request, headers)))
