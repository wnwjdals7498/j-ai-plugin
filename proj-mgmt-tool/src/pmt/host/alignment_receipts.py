"""Typed Host receipt for a client-applied Phase 4 alignment.

The Host never applies Git or document files here. It rechecks its existing
F1/F3, decision, directive, owner and source receipts before advancing the
applied-alignment pointer.
"""
from __future__ import annotations

import hashlib
import json
import uuid

from ..continuity.storage import ContinuityStore
from ..efficiency.source import pin_source, verify_source_pin
from ..efficiency.storage import Phase3Storage
from ..errors import PmtError
from ..phase2_common import project_scope_id, require_workspace_claim
from ..planning.graph import validate_graph
from ..efficiency.graph import manifest_set_fingerprint
from ..util import strict_json_loads
from ..resources import check_artifact
from ..util import canonical_json, fingerprint, utc_now
from .file_effects import _KIND as FILE_EFFECT_KIND
from .host_contract import FILE_OPERATIONS

_ACTIVE_RUNS = {"starting", "running", "review_pending", "reconciling", "cancel_requested"}
_REQUIRED = {"run_id", "expected_run_revision", "assessment_ref", "assessment_hash",
    "resolution_ref", "resolution_hash", "basis_ref", "basis_hash",
    "graph_effect_ref", "document_effect_ref", "step_effect_refs",
    "expected_pointer_revision"}
_DECISION_RECEIPT_FIELDS = {"decision_ref", "decision_revision", "expected_kind",
                           "run_id", "expected_run_revision"}


def _fail(code, message, exit_code=3, details=None):
    raise PmtError(code, message, exit_code, False, details)


def _effect_ref(value, field, scope_id):
    allowed = {"kind", "id", "revision", "scope_id"}
    if (not isinstance(value, dict) or set(value) - allowed
            or not {"kind", "id", "revision"} <= set(value)
            or value.get("kind") != FILE_EFFECT_KIND
            or (value.get("scope_id") is not None and value["scope_id"] != scope_id)
            or type(value.get("revision")) is not int or value["revision"] < 1):
        _fail("alignment_effect_ref_invalid", f"{field} is not a current file-effect reference", 2)
    try:
        if str(uuid.UUID(value["id"])) != value["id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("alignment_effect_ref_invalid", f"{field} ID is invalid", 2) from exc
    return {"kind": FILE_EFFECT_KIND, "id": value["id"], "revision": value["revision"],
            "scope_id": scope_id}


def authorize(extension, conn, req, principal):
    payload = req.get("payload", {})
    if not isinstance(payload, dict) or set(payload) != _REQUIRED:
        _fail("host_alignment_receipt_invalid", "Alignment receipt fields are incomplete or unsupported", 2)
    principal.require("runtime")
    expected = payload.get("expected_run_revision")
    if type(expected) is not int or expected < 1:
        _fail("execution_revision_conflict", "Current positive run revision is required", 3)
    run_id = payload.get("run_id")
    run = conn.execute("SELECT e.owner_session,e.state,e.revision,r.scope_id FROM execution_runs e "
        "JOIN records r ON r.id=e.step_id WHERE e.id=?", (run_id,)).fetchone()
    if (not run or project_scope_id(conn, run["scope_id"]) != req.get("scope_id")
            or run["owner_session"] != principal.session_id or run["state"] not in _ACTIVE_RUNS
            or run["revision"] != expected):
        _fail("workspace_authority_stale", "Alignment receipt needs the current Host run owner and revision", 3)
    if type(payload.get("expected_pointer_revision")) is not int or payload["expected_pointer_revision"] < 0:
        _fail("invalid_pointer_revision", "Applied-alignment pointer revision must be nonnegative", 2)
    _effect_ref(payload["graph_effect_ref"], "graph_effect_ref", req["scope_id"])
    _effect_ref(payload["document_effect_ref"], "document_effect_ref", req["scope_id"])
    if not isinstance(payload["step_effect_refs"], list) or len(payload["step_effect_refs"]) > 100:
        _fail("step_effect_refs_invalid", "step_effect_refs must be a bounded list", 2)
    for ref in payload["step_effect_refs"]:
        if (not isinstance(ref, dict) or set(ref) != {"request_id", "step_ref", "directive_ref",
                "directive_version", "directive_hash"}):
            _fail("step_effect_refs_invalid", "Step directive receipt fields are invalid", 2)


def _current_decision_receipt(conn, req, principal):
    payload = req.get("payload", {})
    if not isinstance(payload, dict) or set(payload) != _DECISION_RECEIPT_FIELDS:
        _fail("decision_receipt_request_invalid", "Decision receipt request fields are invalid", 2)
    run_id = payload.get("run_id")
    decision_ref = payload.get("decision_ref")
    expected_revision = payload.get("decision_revision")
    expected_run_revision = payload.get("expected_run_revision")
    expected_kind = payload.get("expected_kind")
    for value, name in ((run_id, "run_id"), (decision_ref, "decision_ref")):
        try:
            if not isinstance(value, str) or str(uuid.UUID(value)) != value:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise PmtError("decision_receipt_request_invalid", f"{name} is invalid", 2) from exc
    if (type(expected_revision) is not int or expected_revision < 1
            or type(expected_run_revision) is not int or expected_run_revision < 1
            or expected_kind not in {"delegate", "select", "custom"}):
        _fail("decision_receipt_request_invalid", "Decision revision, run revision or kind is invalid", 2)
    principal.require("runtime")
    from ..phase2_common import project_scope_id
    run = conn.execute("SELECT e.owner_session,e.state,e.revision,r.scope_id FROM execution_runs e "
        "JOIN records r ON r.id=e.step_id WHERE e.id=?", (run_id,)).fetchone()
    active_runs = {"starting", "running", "review_pending", "reconciling", "cancel_requested"}
    if (not run or project_scope_id(conn, run["scope_id"]) != req.get("scope_id")
            or run["owner_session"] != principal.session_id or run["state"] not in active_runs
            or run["revision"] != expected_run_revision):
        _fail("workspace_authority_stale", "Decision receipt requires the current project run owner and revision", 3)
    row = conn.execute("SELECT * FROM records WHERE id=? AND kind='decision' AND state='Current'",
                       (decision_ref,)).fetchone()
    if (not row or row["revision"] != expected_revision
            or project_scope_id(conn, row["scope_id"]) != req.get("scope_id")):
        _fail("decision_receipt_unavailable", "Current decision is unavailable in this project or revision", 3)
    try:
        body = json.loads(row["body_json"] or "{}")
    except (TypeError, ValueError) as exc:
        raise PmtError("decision_receipt_unavailable", "Current decision metadata is invalid", 3) from exc
    if body.get("decision_kind") != expected_kind:
        _fail("decision_receipt_unavailable", "Current decision kind differs from the expected kind", 3)
    target = conn.execute("SELECT id,kind,scope_id FROM records WHERE id=? UNION ALL "
        "SELECT id,kind,id AS scope_id FROM scopes WHERE id=? LIMIT 1",
        (row["parent_id"], row["parent_id"])).fetchone()
    if (not target or project_scope_id(conn, target["scope_id"]) != req.get("scope_id")):
        _fail("decision_receipt_unavailable", "Decision target is unavailable in this project", 3)
    events = conn.execute("SELECT id,event_id,actor,scope_id,record_id,old_revision,new_revision,payload_json "
        "FROM events WHERE event_type='decision_saved' AND scope_id=? AND record_id=? "
        "ORDER BY recorded_at DESC", (row["scope_id"], row["parent_id"])).fetchall()
    event = None
    event_body = None
    for candidate in events:
        try:
            candidate_body = json.loads(candidate["payload_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if candidate_body.get("decision_id") == decision_ref:
            event, event_body = candidate, candidate_body
            break
    if (not event or not isinstance(event["actor"], str) or not event["actor"].strip()
            or event["scope_id"] != row["scope_id"] or event["record_id"] != row["parent_id"]):
        _fail("decision_receipt_unavailable", "Current decision lacks its actual scoped decision_saved event", 3)
    from ..util import fingerprint
    event_hash = fingerprint({"event_ref": event["id"], "event_id": event["event_id"],
        "scope_id": event["scope_id"], "target_ref": event["record_id"],
        "decision_ref": decision_ref, "decision_revision": row["revision"],
        "decision_kind": expected_kind, "old_revision": event["old_revision"],
        "new_revision": event["new_revision"], "actor_ref": fingerprint(event["actor"]),
        "payload": event_body})
    return {"decision_ref": decision_ref, "revision": row["revision"],
        "scope_id": req["scope_id"], "target_ref": row["parent_id"],
        "kind": expected_kind, "state": "Current", "event_ref": event["id"],
        "event_ref_hash": event_hash}


def authorize_decision_receipt(conn, req, principal):
    _current_decision_receipt(conn, req, principal)


def read_decision_receipt(conn, req, principal):
    return _current_decision_receipt(conn, req, principal)


def _shared(store, conn, req, reference, kind, body_hash, field):
    obj = store.get(conn, req, reference, kind=kind)
    if not isinstance(body_hash, str) or obj["body_hash"] != body_hash:
        _fail("alignment_reference_stale", f"{field} hash differs from its retained Host object", 3)
    return obj


def _file_effect(db, conn, req, principal, reference, expected_kind):
    effect = Phase3Storage(db).get_object(FILE_EFFECT_KIND, reference["id"], req["scope_id"],
        principal.actor, principal.session_id, conn=conn)
    if (not effect or effect["revision"] != reference["revision"]
            or effect["state"] != "completed"):
        _fail("alignment_effect_unverified", "A current completed Host file-effect receipt is required", 3)
    body = effect["body"]
    owner = body.get("owner") if isinstance(body.get("owner"), dict) else {}
    if (body.get("effect_kind") != expected_kind or body.get("state") != "completed"
            or body.get("phase") != "completed" or owner.get("actor") != principal.actor
            or owner.get("device_id") != principal.device_id
            or owner.get("environment_id") != principal.environment_id
            or owner.get("session_id") != principal.session_id
            or body.get("run_id") != req["payload"]["run_id"]):
        _fail("alignment_effect_unverified", "File-effect kind, phase or current owner does not match", 3)
    return effect


def _step_directive(db, conn, req, reference, affected_refs):
    request_id = reference["request_id"]
    try:
        if str(uuid.UUID(request_id)) != request_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise PmtError("step_effect_refs_invalid", "Step directive request ID is invalid", 2) from exc
    row = conn.execute("SELECT response_json,exit_code,actor,session_id FROM requests WHERE request_id=?",
                       (request_id,)).fetchone()
    if (not row or row["actor"] != req["actor"] or row["session_id"] != req["session_id"]
            or row["exit_code"] != 0):
        _fail("step_effect_unverified", "Step directive request is not a successful current-owner receipt", 3)
    try:
        response = json.loads(row["response_json"])
        result = response.get("result") or {}
    except (TypeError, ValueError):
        _fail("step_effect_unverified", "Step directive receipt is corrupt", 5)
    step_id, artifact_id = reference["step_ref"], reference["directive_ref"]
    if (result.get("step_id") != step_id or result.get("directive_id") != artifact_id
            or result.get("directive_version") != reference["directive_version"]
            or step_id not in set(affected_refs)):
        _fail("step_effect_outside_assessment", "Step directive receipt differs from the assessment scope", 3)
    step = conn.execute("SELECT r.scope_id,r.revision,s.directive_id,s.directive_version FROM records r "
        "JOIN step_specs s ON s.step_id=r.id WHERE r.id=? AND r.kind='step'", (step_id,)).fetchone()
    artifact = conn.execute("SELECT scope_id,sha256,state FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
    event = conn.execute("SELECT payload_json FROM events WHERE event_type='planning.step_instruction_versioned' "
        "AND record_id=? ORDER BY recorded_at DESC LIMIT 1", (step_id,)).fetchone()
    if (not step or project_scope_id(conn, step["scope_id"]) != req["scope_id"]
            or step["directive_id"] != artifact_id or step["directive_version"] != reference["directive_version"]
            or not artifact or artifact["scope_id"] != req["scope_id"] or artifact["state"] != "ready"
            or artifact["sha256"] != reference["directive_hash"] or not event):
        _fail("step_effect_readback_mismatch", "Current Step directive record differs from its Host receipt", 3)
    try:
        event_body = json.loads(event["payload_json"])
    except (TypeError, ValueError):
        _fail("step_effect_event_mismatch", "Current Step directive event is invalid", 5)
    if (event_body.get("directive_id") != artifact_id
            or event_body.get("directive_version") != reference["directive_version"]):
        _fail("step_effect_event_mismatch", "Current Step directive event differs from its receipt", 3)
    checked = check_artifact(db, conn, artifact_id)
    if not checked.get("valid") or checked.get("sha256") != artifact["sha256"]:
        _fail("step_effect_resource_invalid", "Current Step directive bytes failed hash verification", 3)
    return {"step_ref": step_id, "record_revision": step["revision"],
            "directive_ref": artifact_id, "directive_version": step["directive_version"],
            "directive_hash": artifact["sha256"], "event_ref_hash": fingerprint(event_body)}


def _quiescence_conflict(conn, req, current_run_id, workspace, affected_record_refs):
    from ..phase2_common import project_scope_id

    affected = set(affected_record_refs)
    active_states = ("queued", "starting", "running", "review_pending", "reconciling", "cancel_requested")
    marks = ",".join("?" for _ in active_states)
    rows = conn.execute("SELECT e.id,e.step_id,e.workspace,e.state,s.scope_id FROM execution_runs e "
        "JOIN records s ON s.id=e.step_id WHERE e.state IN (" + marks + ")", active_states).fetchall()
    for row in rows:
        if project_scope_id(conn, row["scope_id"]) != req["scope_id"]:
            continue
        if row["step_id"] in affected:
            return {"conflict_ref": row["id"], "kind": "affected_step_run", "state": row["state"]}
        if row["id"] != current_run_id and row["workspace"] == workspace:
            return {"conflict_ref": row["id"], "kind": "active_workspace_run", "state": row["state"]}

    pending = conn.execute("SELECT id,kind,state FROM continuity_journal WHERE scope_id=? "
        "AND state IN ('prepared','applying','partial','unknown','conflict') ORDER BY created_at LIMIT 1",
        (req["scope_id"],)).fetchone()
    if pending:
        return {"conflict_ref": pending["id"], "kind": pending["kind"], "state": pending["state"]}

    file_effects = conn.execute("SELECT id,state,body_json FROM phase3_objects "
        "WHERE kind=? AND scope_id=? ORDER BY updated_at", (FILE_EFFECT_KIND, req["scope_id"])).fetchall()
    for row in file_effects:
        try:
            body = json.loads(row["body_json"] or "{}")
        except (TypeError, ValueError):
            return {"conflict_ref": row["id"], "kind": "file_effect", "state": "unknown"}
        if row["state"] != "completed" or body.get("state") != "completed" or body.get("phase") != "completed":
            return {"conflict_ref": row["id"], "kind": body.get("effect_kind", "file_effect"),
                    "state": body.get("phase") or row["state"] or "unknown"}

    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='phase3_journal'").fetchone():
        rows = conn.execute("SELECT id,kind,body_json FROM phase3_journal WHERE scope_id=? "
            "AND kind IN ('graph_change','document_segments') ORDER BY created_at", (req["scope_id"],)).fetchall()
        for row in rows:
            try:
                body = json.loads(row["body_json"] or "{}")
            except (TypeError, ValueError):
                return {"conflict_ref": row["id"], "kind": row["kind"], "state": "unknown"}
            if body.get("stage") != "completed":
                return {"conflict_ref": row["id"], "kind": row["kind"],
                        "state": body.get("stage") or "unknown"}
    return None


def apply(extension, db, conn, req, principal, headers):
    payload = req["payload"]
    store = ContinuityStore(db)
    assessment = _shared(store, conn, req, payload["assessment_ref"], "assessment",
                         payload["assessment_hash"], "assessment_ref")
    resolution = _shared(store, conn, req, payload["resolution_ref"], "resolution",
                         payload["resolution_hash"], "resolution_ref")
    basis = _shared(store, conn, req, payload["basis_ref"], "basis",
                    payload["basis_hash"], "basis_ref")
    if (assessment["body"].get("state") != "assessed" or assessment["body"].get("unknown")
            or assessment["body"].get("basis_ref") != basis["id"]
            or assessment["body"].get("basis_hash") != basis["body_hash"]
            or resolution["body"].get("assessment_ref") != assessment["id"]
            or resolution["body"].get("assessment_hash") != assessment["body_hash"]):
        _fail("alignment_reference_stale", "Assessment, resolution and basis are not a current complete chain", 3)

    graph_ref = _effect_ref(payload["graph_effect_ref"], "graph_effect_ref", req["scope_id"])
    document_ref = _effect_ref(payload["document_effect_ref"], "document_effect_ref", req["scope_id"])
    graph_effect = _file_effect(db, conn, req, principal, graph_ref, "graph_change")
    document_effect = _file_effect(db, conn, req, principal, document_ref, "document_render")
    graph_body, document_body = graph_effect["body"], document_effect["body"]
    basis_body = basis["body"]
    basis_source = basis_body.get("source", {})
    basis_scope = basis_body.get("scope", {})
    before_pin = pin_source(graph_body.get("before_source_pin"))
    current_pointer_payload = {"project_id": req["scope_id"],
        "repository_id": graph_body.get("repository_id"),
        "canonical_workspace": graph_body.get("canonical_workspace"),
        "relative_graph_path": graph_body.get("relative_graph_path"),
        "run_id": payload["run_id"], "expected_run_revision": payload["expected_run_revision"],
        "branch_key": (before_pin.selected_ref if before_pin.selected_ref is not None else
            "detached:" + before_pin.reviewed_commit if before_pin.source_kind == "git" else "non-git")}
    binding_req = dict(req) | {"payload": current_pointer_payload}
    binding = extension._workspace_binding(conn, binding_req, principal, mode="execute")
    current_pointer = binding["source_pointer"]
    current_pin = pin_source(current_pointer["body"].get("source_pin"))
    from ..phase2_common import require_workspace_claim
    require_workspace_claim(db, conn, binding_req, binding["canonical_workspace"],
        [binding["relative_graph_path"], graph_body["target_relative_path"],
         document_body["target_relative_path"]])
    if (graph_effect["revision"] != graph_ref["revision"]
            or document_effect["revision"] != document_ref["revision"]
            or graph_body.get("canonical_workspace") != basis_source.get("workspace_ref")
            or graph_body.get("repository_id") != basis_scope.get("repository_id")
            or before_pin.graph_hash != assessment["body"].get("observed_graph_hash")
            or before_pin.graph_hash != basis_body.get("contract", {}).get("graph_hash")
            or current_pin.source_hash != pin_source(graph_body.get("after_source_pin")).source_hash
            or current_pin.source_hash != pin_source(document_body.get("after_source_pin")).source_hash
            or current_pointer["body"].get("graph_resource") != graph_body.get("uploaded_source_ref")
            or graph_body.get("effect_id") != graph_ref["id"]
            or document_body.get("effect_id") != document_ref["id"]):
        _fail("alignment_effect_source_mismatch", "F1/F3 effects do not match the current basis, owner and Host SourcePin", 3)
    history = graph_body.get("history") if isinstance(graph_body.get("history"), list) else []
    if not history or history[-1].get("host_graph_bytes_verified") is not True:
        _fail("alignment_graph_readback_missing", "Host has no graph snapshot readback receipt", 3)
    before_pointer = graph_body.get("before_source_pointer")
    before_ref = graph_body.get("before_graph_resource_ref")
    if (not isinstance(before_pointer, dict) or before_pointer.get("graph_resource_ref") != before_ref
            or not isinstance(before_ref, dict) or before_ref.get("purpose") != "graph_snapshot"
            or before_ref.get("scope_id") != req["scope_id"]):
        _fail("alignment_graph_before_receipt_invalid", "F1 does not retain the prior Host graph reference", 3)
    before_resource = extension._read_resource(before_ref, req["scope_id"], headers)
    before_bytes = before_resource.get("content")
    if (not isinstance(before_bytes, bytes) or hashlib.sha256(before_bytes).hexdigest() != before_ref.get("sha256")
            or len(before_bytes) != before_ref.get("size")):
        _fail("alignment_graph_before_receipt_invalid", "F1 before graph bytes failed Host hash verification", 3)
    try:
        before_graph = strict_json_loads(before_bytes, max_bytes=8 * 1024 * 1024)
    except PmtError as exc:
        raise PmtError("alignment_graph_before_invalid", "F1 before graph is invalid JSON", 3) from exc
    before_report = validate_graph(before_graph, req["scope_id"], complete=False)
    if (before_report.get("sha256") != before_pin.graph_hash
            or hashlib.sha256(before_bytes).hexdigest() != assessment["body"].get("observed_graph_file_hash")):
        _fail("alignment_graph_assessment_mismatch", "Assessment does not match the actual F1 before graph", 3)
    current_source = extension._source_from_pointer(conn, binding, current_pointer, headers)
    current_graph = current_source.get("graph")
    current_report = validate_graph(current_graph, req["scope_id"], complete=False)
    published_ref = current_pointer["body"].get("graph_resource", {})
    if (graph_body.get("candidate_sha256") != published_ref.get("sha256")
            or current_report.get("sha256") != current_pin.graph_hash
            or assessment["body"].get("observed_graph_hash") != before_pin.graph_hash):
        _fail("alignment_graph_current_mismatch", "F1 candidate, Host SourcePin and assessment disagree", 3)
    before_nodes = {item["id"]: item for item in before_graph.get("nodes", []) if isinstance(item, dict)}
    current_nodes = {item["id"]: item for item in current_graph.get("nodes", []) if isinstance(item, dict)}
    if (set(before_nodes) != set(current_nodes)
            or before_graph.get("relations") != current_graph.get("relations")):
        _fail("alignment_graph_structure_changed", "Alignment receipt cannot add/remove nodes or alter graph relations", 3)
    changed_nodes = {node_id for node_id in before_nodes
        if canonical_json(before_nodes[node_id]) != canonical_json(current_nodes[node_id])}
    assessment_nodes = set(assessment["body"].get("affected_node_refs", []))
    if not changed_nodes or not changed_nodes <= assessment_nodes:
        _fail("alignment_graph_scope_mismatch", "F1 changed nodes outside the actual assessment", 3)
    if (document_body.get("host_document_verified") is not False
            or not document_body.get("coverage_ref")
            or not document_body.get("manifest_refs")):
        _fail("alignment_document_receipt_missing", "Document effect lacks the registered F3 manifest/coverage receipt", 3)
    document_receipt = document_body.get("history")[-1] if document_body.get("history") else None
    if not isinstance(document_receipt, dict) or document_receipt.get("host_document_verified") is not False:
        _fail("alignment_document_receipt_invalid", "Host cannot claim it verified client document bytes", 3)
    document_path = document_body.get("target_relative_path")
    publication = document_body.get("publication")
    if (not isinstance(document_path, str) or not isinstance(publication, dict)
            or publication.get("status") not in {"published", "replayed"}
            or publication.get("candidate_hash") != document_body.get("candidate_sha256")):
        _fail("alignment_document_receipt_invalid", "Document publication receipt differs from the client effect", 3)
    manifests = document_body.get("manifest_refs", [])
    manifest_set = []
    for ref in manifests:
        if (not isinstance(ref, dict) or set(ref) != {"segment_id", "manifest_hash", "revision"}
                or type(ref.get("revision")) is not int or ref["revision"] < 1):
            _fail("alignment_document_manifest_invalid", "F3 manifest receipt has an invalid shape", 3)
        row = conn.execute("SELECT revision,source_hash,body_json,state FROM phase3_objects "
            "WHERE kind='segment_manifest' AND id=? AND scope_id=?", (ref["segment_id"], req["scope_id"])).fetchone()
        try:
            manifest = json.loads(row["body_json"]) if row else None
        except (TypeError, ValueError):
            manifest = None
        if (not row or row["state"] != "ready" or row["revision"] != ref["revision"]
                or row["source_hash"] != current_pin.source_hash or not isinstance(manifest, dict)
                or fingerprint(manifest) != ref["manifest_hash"]
                or manifest.get("source_pin", {}).get("source_hash") != current_pin.source_hash
                or manifest.get("document_path") != document_path):
            _fail("alignment_document_manifest_mismatch", "Current F3 manifest does not match the document effect", 3)
        manifest_set.append({"segment_id": ref["segment_id"], "manifest_hash": ref["manifest_hash"]})
    coverage_ref = document_body.get("coverage_ref")
    if (not isinstance(coverage_ref, dict) or set(coverage_ref) != {
            "revision", "certificate_hash", "manifest_set_hash"}):
        _fail("alignment_document_coverage_invalid", "F3 document coverage receipt is missing", 3)
    coverage_row = conn.execute("SELECT revision,source_hash,body_json,state FROM phase3_objects "
        "WHERE kind='segment_coverage' AND id=? AND scope_id=?",
        (req["scope_id"], req["scope_id"])).fetchone()
    try:
        certificate = json.loads(coverage_row["body_json"]) if coverage_row else None
    except (TypeError, ValueError):
        certificate = None
    if (not coverage_row or coverage_row["state"] != "ready"
            or coverage_row["revision"] != coverage_ref["revision"]
            or coverage_row["source_hash"] != current_pin.source_hash
            or not isinstance(certificate, dict)
            or certificate.get("coverage_certificate_hash") != coverage_ref["certificate_hash"]
            or certificate.get("manifest_set_hash") != coverage_ref["manifest_set_hash"]
            or document_path not in certificate.get("document_paths", [])
            or manifest_set_fingerprint(manifest_set, certificate.get("document_paths", []), current_pin)
               != coverage_ref["manifest_set_hash"]):
        _fail("alignment_document_coverage_mismatch", "Current F3 coverage certificate does not match the effect", 3)

    from ..continuity.alignment import _decision_record
    resolution_body = resolution["body"]
    decision_refs = []
    if resolution_body.get("state") == "delegated_method_candidate":
        eligible = set(resolution_body.get("eligible_method_node_refs", []))
        for ref in assessment["body"].get("delegation_refs", []):
            decision = _decision_record(conn, req, ref.get("decision_ref"), expected_kind="delegate")
            prior_node = before_nodes.get(ref.get("node_id"))
            current_node = current_nodes.get(ref.get("node_id"))
            delegated_scope = prior_node.get("delegated_scope") if isinstance(prior_node, dict) else None
            if (not decision or decision["revision"] != ref.get("decision_revision")
                    or decision["parent_id"] not in set(assessment["body"].get("affected_record_refs", []))
                    or decision["body"].get("delegation_scope") != delegated_scope
                    or fingerprint(delegated_scope) != ref.get("scope_hash")
                    or decision["event_ref"] != ref.get("decision_event_ref")
                    or not isinstance(prior_node, dict) or not isinstance(current_node, dict)
                    or prior_node.get("stop_reason") != "user_delegated"
                    or prior_node.get("autonomy", {}).get("authority") != "user"
                    or current_node.get("stop_reason") != "user_delegated"
                    or current_node.get("autonomy", {}).get("authority") != "user"):
                _fail("alignment_delegation_stale", "Current user delegation decision no longer matches assessment", 3)
            decision_refs.append({"decision_ref": decision["id"], "revision": decision["revision"],
                                 "event_ref_hash": fingerprint(decision["event_ref"])})
        if not eligible or not decision_refs or not changed_nodes <= eligible:
            _fail("alignment_delegation_missing", "Delegated method alignment needs actual current decision receipts", 3)
        protected = {"id", "tree_kind", "node_kind", "summary", "premise", "product_scope",
                     "autonomy", "stop_reason", "delegated_scope", "source_refs", "choice", "choice_set"}
        for node_id in changed_nodes:
            old, new = before_nodes[node_id], current_nodes[node_id]
            if any(old.get(key) != new.get(key) for key in protected):
                _fail("alignment_method_scope_mismatch", "Delegated method edit changed a user-owned premise or decision field", 3)
    elif resolution_body.get("state") == "premise_decision_recorded":
        decision = _decision_record(conn, req, resolution_body.get("decision_ref"), expected_kind={"select", "custom"})
        if (not decision or decision["revision"] != resolution_body.get("decision_revision")
                or decision["parent_id"] != resolution_body.get("decision_target_ref")
                or decision["parent_id"] not in set(assessment["body"].get("affected_record_refs", []))):
            _fail("alignment_decision_stale", "Current premise decision is unavailable or outside the assessed work", 3)
        decision_refs.append({"decision_ref": decision["id"], "revision": decision["revision"],
                              "event_ref_hash": fingerprint(decision["event_ref"])})
        if not changed_nodes <= assessment_nodes:
            _fail("alignment_premise_scope_mismatch", "User decision edit changed nodes outside the assessment", 3)
    else:
        _fail("alignment_resolution_incomplete", "Resolution does not carry an actual user or delegated decision", 3)

    step_effect_refs = payload["step_effect_refs"]
    if len({ref["step_ref"] for ref in step_effect_refs}) != len(step_effect_refs):
        _fail("step_effect_refs_invalid", "Step directive receipts contain duplicate targets", 2)
    step_effects = [_step_directive(db, conn, req, ref,
        assessment["body"].get("affected_record_refs", [])) for ref in step_effect_refs]
    conflict = _quiescence_conflict(conn, req, payload["run_id"],
        binding["canonical_workspace"], assessment["body"].get("affected_record_refs", []))
    if conflict:
        _fail("alignment_quiescence_required",
            "Another active run or unresolved Host effect must be reconciled before alignment can advance",
            3, conflict)
    selector = {"repository_id": basis_source.get("repository_id"),
        "branch": basis_source.get("branch"), "workspace_ref": basis_source.get("workspace_ref"),
        "task_id": basis_body.get("work", {}).get("task_id"), "purpose": "applied_alignment",
        "environment_id": principal.environment_id}
    pointer = store.read_pointer(conn, req, selector)
    if pointer["revision"] != payload["expected_pointer_revision"]:
        _fail("revision_conflict", "Applied-alignment pointer revision changed", 3,
              {"expected_revision": payload["expected_pointer_revision"],
               "current_revision": pointer["revision"]})

    source = current_pointer["body"].get("source_pin", {})
    body = {"version": 1, "state": "applied", "assessment_ref": assessment["id"],
        "assessment_hash": assessment["body_hash"], "resolution_ref": resolution["id"],
        "resolution_hash": resolution["body_hash"], "basis_ref": basis["id"],
        "basis_hash": basis["body_hash"], "decision_refs": decision_refs,
        "graph_effect_ref": graph_ref, "document_effect_ref": document_ref,
        "step_directive_effects": step_effects,
        "before_graph_hash": before_pin.graph_hash, "source_pin": source,
        "graph_hash": current_pin.graph_hash,
        "document_hash": document_body.get("publication", {}).get("candidate_hash"),
        "manifest_refs": document_body.get("manifest_refs"),
        "coverage_ref": document_body.get("coverage_ref"),
        "preserved_unrelated_ref_hash": assessment["body"].get("unaffected_scope_hash"),
        "pointer_selector": selector, "provenance": "client_effect_readback",
        "host_git_verified": False, "host_document_verified": False,
        "captured_at": utc_now()}
    receipt_hash = fingerprint(body)
    body["receipt_hash"] = receipt_hash
    effect = store.begin_effect(conn, req, "alignment_receipt", {
        "assessment_ref": assessment["id"], "assessment_hash": assessment["body_hash"],
        "resolution_ref": resolution["id"], "basis_ref": basis["id"],
        "graph_effect_ref": graph_ref, "document_effect_ref": document_ref},
        basis_hash=basis["body_hash"])
    saved = store.put(conn, req, "alignment", body, basis_hash=basis["body_hash"],
        event_id=str(uuid.uuid5(uuid.UUID(req["request_id"]), "applied-alignment")))
    pointer_result = store.advance_pointer(conn, req, selector, saved["id"], payload["expected_pointer_revision"])
    store.update_effect(conn, req, effect["id"], "completed", {
        "alignment_ref": saved["id"], "receipt_hash": receipt_hash,
        "pointer_revision": pointer_result["revision"], "source_hash": current_pin.source_hash})
    return {"alignment_ref": saved["id"], "receipt_hash": receipt_hash, "pointer": pointer_result,
        "effect_ref": effect["id"], "effect_refs": [graph_ref, document_ref],
        "source_pin": current_pin.to_dict(), "provenance": "client_effect_readback",
        "host_git_verified": False, "host_document_verified": False, "applied_pointer_advanced": True}
