"""Phase-four impact, evidence applicability and guarded alignment services."""
from __future__ import annotations

import json
import hashlib
from contextlib import closing
from pathlib import Path

from ..errors import PmtError
from ..planning.graph import validate_graph
from ..resources import _reject_links
from ..util import fingerprint, new_id, strict_json_loads, utc_now
from ..verification import _failure_after, _load_verification, _validate_evidence
from ..efficiency import reuse
from .contracts import authorize, bounded_result, work_access
from .storage import ContinuityStore

READ_OPERATIONS = set()
WRITE_OPERATIONS = {"propose_semantic_resolution"}
FILE_OPERATIONS = {"assess_alignment", "apply_alignment", "read_applicability"}
_APPLICABILITY = {"applicable", "not_applicable", "unknown"}


def _payload(req):
    value = req.get("payload", {})
    if not isinstance(value, dict):
        raise PmtError("invalid_payload", "payload must be an object")
    return value


def _mapped_graph_path(db, req, workspace, basis):
    from ..storage_config import _read_profile, mapping_for_request
    source = basis.get("body", {}).get("source", {})
    profile, _ = _read_profile(db.config_root)
    if not isinstance(profile, dict):
        raise PmtError("source_mapping_unknown", "No configured canonical workspace mapping exists", 3)
    mapping = mapping_for_request(profile, {"scope_id": req.get("scope_id"), "payload": {
        "repository_id": source.get("repository_id"), "project_id": req.get("scope_id"),
        "branch": source.get("branch")}})
    if (not isinstance(mapping, dict) or
            Path(mapping["local_root"]).expanduser().resolve() != workspace.resolve()):
        raise PmtError("source_mapping_conflict", "Current run workspace differs from its configured canonical mapping", 3)
    configured = mapping["relative_graph_path"]
    selected = _payload(req).get("relative_graph_path")
    if selected is not None and selected != configured:
        raise PmtError("source_mapping_conflict", "Requested graph path differs from the configured canonical mapping", 3)
    return configured


def _project_graph(db, req, paths, basis):
    payload = _payload(req)
    run_id = payload.get("run_id")
    with closing(db.connect()) as conn:
        runrow = conn.execute("SELECT workspace FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        if not runrow:
            raise PmtError("run_not_found", "Current run does not exist", 3)
        workspace = Path(runrow["workspace"]).resolve()
        relative_graph_path = _mapped_graph_path(db, req, workspace, basis)
        access_paths = sorted(set(paths + [relative_graph_path]), key=str.casefold)
        run = work_access(db, conn, req, access_paths, str(workspace))
    graph_path = workspace / Path(relative_graph_path)
    _reject_links(graph_path)
    try:
        raw = graph_path.read_bytes()
    except OSError as exc:
        raise PmtError("project_graph_unreadable", "Current project graph could not be read", 4, True) from exc
    graph = strict_json_loads(raw, max_bytes=8 * 1024 * 1024)
    report = validate_graph(graph, req["scope_id"], complete=False)
    return workspace, run, graph, report, hashlib.sha256(raw).hexdigest()


def _get(db, req, object_id, kind):
    with closing(db.connect()) as conn:
        return ContinuityStore(db).get(conn, req, object_id, kind=kind)


def _decision_record(conn, req, decision_ref, *, expected_kind=None):
    if not isinstance(decision_ref, str):
        return None
    from ..phase2_common import project_scope_id
    row = conn.execute("SELECT * FROM records WHERE id=? AND kind='decision' AND state='Current'",
                       (decision_ref,)).fetchone()
    if not row or project_scope_id(conn, row["scope_id"]) != req["scope_id"]:
        return None
    try:
        body = json.loads(row["body_json"] or "{}")
    except (TypeError, ValueError):
        return None
    if expected_kind:
        allowed_kinds = set(expected_kind) if isinstance(expected_kind, (set, tuple, list, frozenset)) else {expected_kind}
        if body.get("decision_kind") not in allowed_kinds:
            return None
    event = conn.execute("SELECT id,actor,occurred_at FROM events WHERE event_type='decision_saved' AND scope_id=? "
                         "AND json_extract(payload_json,'$.decision_id')=? ORDER BY recorded_at DESC LIMIT 1",
                         (row["scope_id"], row["id"])).fetchone()
    if not event or not isinstance(event["actor"], str) or not event["actor"].strip():
        return None
    return {"id": row["id"], "revision": row["revision"], "parent_id": row["parent_id"], "body": body,
            "event_ref": event["id"], "actor_ref": fingerprint(event["actor"])}


def _store_result(db, req, kind, body, *, basis_hash=None, event_id=None):
    def commit(conn, request):
        authorize(db, conn, request)
        saved = ContinuityStore(db).put(conn, request, kind, body, basis_hash=basis_hash, event_id=event_id)
        return {"ref": saved["id"], "body_hash": saved["body_hash"], "state": body.get("state"),
                "status": body.get("status"), "replayed": saved["created_at"] is not None}
    return db.run_request(req, commit, authorize=lambda conn, request: authorize(db, conn, request))


def _changed_nodes(change, index):
    facts = change["body"].get("facts", [])
    links = index["body"].get("links", [])
    link_by_path = {}
    for link in links:
        link_by_path.setdefault(link.get("path_ref"), []).append(link)
    affected, unknown, paths = set(), [], []
    for fact in facts:
        path_ref = fact.get("path_ref")
        paths.append(path_ref)
        path_links = link_by_path.get(path_ref, [])
        verified = [item for item in path_links if item.get("link_state") == "verified_mapping"]
        candidates = [item for item in path_links if item.get("link_state") == "extracted_candidate"]
        if verified:
            affected.update(node for item in verified for node in item.get("node_ids", []))
        elif candidates:
            affected.update(node for item in candidates for node in item.get("node_ids", []))
            unknown.append({"path_ref": path_ref, "reason_code": "mapping_candidate_requires_review"})
        else:
            unknown.append({"path_ref": path_ref, "reason_code": "implementation_mapping_missing"})
    if change["body"].get("coverage") != "complete":
        unknown.append({"reason_code": "change_capture_incomplete"})
    if index["body"].get("coverage", {}).get("complete") is not True:
        unknown.append({"reason_code": "implementation_link_coverage_incomplete"})
    return sorted(affected), unknown, paths


def _typed_graph_impact(db, req, workspace, run, graph, source_mapping, payload):
    preview, change_set = payload.get("typed_graph_preview"), payload.get("change_set")
    if preview is None and change_set is None:
        return None
    if not isinstance(preview, dict) or not isinstance(change_set, dict):
        raise PmtError("graph_preview_required", "A typed ChangeSet and its validated F1 preview are both required")
    from ..efficiency import graph as graph_service
    from ..efficiency.source import verify_source_pin
    request_id = req["request_id"]
    prepared = graph_service.prepare_change_set(graph, change_set, request_id)
    if (preview.get("change_id") != prepared["change_id"] or
            preview.get("change_set_hash") != prepared["change_set_hash"] or
            preview.get("operations") != graph_service._safe_operation_summary(prepared) or
            preview.get("expected_new_source", {}).get("graph_hash") != prepared["source_hash"]):
        raise PmtError("graph_preview_conflict", "F1 preview does not match the actual graph and typed ChangeSet", 3)
    expected_pin = preview.get("source_pin")
    if not isinstance(expected_pin, dict):
        raise PmtError("graph_preview_invalid", "F1 preview lacks its source pin")
    synthetic = {"protocol_version": req.get("protocol_version", 1), "operation": "calculate_graph_impact",
        "request_id": request_id, "actor": req["actor"], "session_id": req["session_id"],
        "scope_id": req["scope_id"], "payload": {"repository_id": source_mapping["repository_id"],
            "workspace": str(workspace), "relative_graph_path": source_mapping["relative_graph_path"],
            "run_id": run["id"], "expected_source": expected_pin,
            "change_preview": preview, "change_set": change_set,
            "rule_version": payload.get("rule_version", "graph-field-semantics-1"),
            "max_depth": payload.get("max_depth", 4)}}
    with closing(db.connect()) as conn:
        context = graph_service._source_graph(db, conn, synthetic)
        verify_source_pin(expected_pin, context["source_pin"])
        impact = graph_service._calculate_impact(db, conn, synthetic, context)
    summary = {"change_id": impact.get("change_id"), "source_pin": impact.get("source_pin"),
        "rule_version": impact.get("rule_version"), "complete": impact.get("complete"),
        "field_change_count": len(impact.get("field_changes", [])),
        "affected_node_refs": sorted({item.get("node_id") for item in impact.get("known_nodes", [])
                                       if isinstance(item, dict) and isinstance(item.get("node_id"), str)}),
        "unknown_count": len(impact.get("unknown", []))}
    summary["impact_hash"] = fingerprint(summary)
    return summary


def assess_alignment(db, req):
    payload = _payload(req)
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths or any(not isinstance(item, str) for item in paths):
        raise PmtError("source_paths_required", "Explicit claimed paths are required for alignment assessment")
    change = _get(db, req, payload.get("change_ref"), "change")
    index = _get(db, req, payload.get("index_ref"), "link_index")
    basis = _get(db, req, payload.get("basis_ref"), "basis")
    workspace, run, graph, report, graph_hash = _project_graph(db, req, paths, basis)
    from .changes import _verify_source_mapping
    source_mapping = _verify_source_mapping(db, req, workspace, basis)
    affected, unknown, path_refs = _changed_nodes(change, index)
    graph_nodes = {node["id"]: node for node in graph["nodes"]}
    missing_nodes = sorted(set(affected) - set(graph_nodes))
    if missing_nodes:
        unknown.append({"reason_code": "mapped_graph_node_missing"})
    delegation_refs = []
    eligible_methods = []
    affected_record_refs = set()
    with closing(db.connect()) as conn:
        decision_records = {}
        for decision_id in {ref for node in graph_nodes.values() for ref in node.get("source_refs", [])
                            if isinstance(ref, str)}:
            decision = _decision_record(conn, req, decision_id, expected_kind="delegate")
            if decision:
                decision_records[decision_id] = decision
    for node_id in affected:
        node = graph_nodes.get(node_id)
        if not node:
            continue
        node_refs = node.get("work_item_step_refs", {})
        if isinstance(node_refs, dict):
            for values in node_refs.values():
                if isinstance(values, list):
                    affected_record_refs.update(value for value in values if isinstance(value, str))
        decision = next((decision_records[ref] for ref in node.get("source_refs", [])
                         if ref in decision_records and decision_records[ref]["body"].get("delegation_scope") == node.get("delegated_scope")), None)
        if (node.get("stop_reason") == "user_delegated" and node.get("delegated_scope")
                and node.get("autonomy", {}).get("authority") == "user" and decision):
            delegation_refs.append({"node_id": node_id, "scope_hash": fingerprint(node["delegated_scope"]),
                                    "decision_ref": decision["id"], "decision_revision": decision["revision"],
                                    "decision_event_ref": decision["event_ref"],
                                    "decision_actor_ref": decision["actor_ref"]})
            if node.get("tree_kind") == "implementation":
                eligible_methods.append(node_id)
    basis_source = basis["body"].get("source", {})
    basis_graph_hash = basis["body"].get("contract", {}).get("graph_hash")
    reasons = []
    if basis_graph_hash and basis_graph_hash != report.get("sha256"):
        reasons.append("graph_changed_since_basis")
    if basis_source.get("branch") != source_mapping["branch"]:
        reasons.append("branch_changed_since_basis")
    change_body = change["body"]
    if (change_body.get("after_basis_ref") != basis["id"] or
            change_body.get("after_basis_hash") != basis["body_hash"]):
        reasons.append("change_after_basis_mismatch")
    if (index["body"].get("basis_ref") != basis["id"] or
            index["body"].get("basis_hash") != basis["body_hash"]):
        reasons.append("implementation_index_basis_mismatch")
    typed_impact = _typed_graph_impact(db, req, workspace, run, graph, source_mapping, payload)
    if typed_impact and (typed_impact.get("complete") is not True or typed_impact.get("unknown_count")):
        unknown.append({"reason_code": "typed_graph_impact_incomplete"})
    if reasons:
        unknown.extend({"reason_code": reason} for reason in reasons)
    assessment = {
        "state": "incomplete" if unknown or reasons else "assessed",
        "change_ref": change["id"], "change_hash": change["body_hash"],
        "index_ref": index["id"], "index_hash": index["body_hash"],
        "basis_ref": basis["id"], "basis_hash": basis["body_hash"],
        "observed_graph_hash": report["sha256"], "observed_graph_file_hash": graph_hash,
        "graph_revision": graph["graph_version"],
        "affected_node_refs": affected, "unknown": unknown,
        "affected_record_refs": sorted(affected_record_refs),
        "unaffected_scope_hash": fingerprint(sorted(set(graph_nodes) - set(affected))),
        "delegation_refs": delegation_refs, "eligible_method_node_refs": sorted(eligible_methods),
        "semantic_state": "unknown", "judgment_required": not bool(eligible_methods),
        "required_action": "native_main_review" if eligible_methods and not unknown else "user_choice_or_more_evidence",
        "typed_graph_impact": typed_impact,
        "typed_graph_preview_hash": fingerprint(payload.get("typed_graph_preview")) if typed_impact else None,
        "typed_graph_preview_used": typed_impact is not None,
        "raw_change_was_typed_graph_delta": False,
        "captured_at": utc_now(), "rule_version": "p4-alignment-1"}
    assessment["assessment_hash"] = fingerprint(assessment)
    # Source/graph coherence is read back before storing the immutable assessment.
    after = hashlib.sha256((workspace / source_mapping["relative_graph_path"]).read_bytes()).hexdigest()
    if after != graph_hash:
        assessment["state"] = "incomplete"
        assessment["unknown"].append({"reason_code": "graph_changed_during_assessment"})
        assessment["assessment_hash"] = fingerprint({k: v for k, v in assessment.items() if k != "assessment_hash"})
    envelope, code = _store_result(db, req, "assessment", assessment, basis_hash=basis["body_hash"])
    if code == 0:
        result = envelope.get("result", {})
        result.update({"assessment_ref": result.pop("ref", None), "assessment_hash": assessment["assessment_hash"],
                       "affected_node_refs": assessment["affected_node_refs"], "unknown": assessment["unknown"],
                       "required_action": assessment["required_action"]})
        envelope["result"] = bounded_result(req, result)
    return envelope, code


def propose_semantic_resolution(db, conn, req):
    payload = _payload(req)
    assessment = ContinuityStore(db).get(conn, req, payload.get("assessment_ref"), kind="assessment")
    requested = payload.get("resolution_kind")
    if requested not in {"method", "premise", "unclear"}:
        raise PmtError("resolution_kind_invalid", "resolution_kind must be method, premise, or unclear")
    allowed_nodes = assessment["body"].get("eligible_method_node_refs", [])
    delegated = requested == "method" and bool(allowed_nodes) and not assessment["body"].get("unknown")
    decision_ref = payload.get("decision_ref")
    approved_decision = _decision_record(conn, req, decision_ref,
                                         expected_kind={"select", "custom"}) if requested == "premise" else None
    if (approved_decision and
            approved_decision["parent_id"] not in set(assessment["body"].get("affected_record_refs", []))):
        approved_decision = None
    state = ("delegated_method_candidate" if delegated else
             "premise_decision_recorded" if approved_decision else
             "awaiting_user" if requested == "premise" else "needs_review")
    proposal = {"state": state, "assessment_ref": assessment["id"], "assessment_hash": assessment["body_hash"],
                "resolution_kind": requested, "eligible_method_node_refs": allowed_nodes if delegated else [],
                "decision_ref": approved_decision["id"] if approved_decision else None,
                "decision_event_ref": approved_decision["event_ref"] if approved_decision else None,
                "decision_revision": approved_decision["revision"] if approved_decision else None,
                "decision_target_ref": approved_decision["parent_id"] if approved_decision else None,
                "model_claim_is_approval": False,
                "required_action": "native_main_review" if delegated else "continue_with_recorded_user_decision" if approved_decision else "user_decision_required",
                "reason_codes": (["current_explicit_delegation_present"] if delegated else
                                 ["current_user_decision_recorded"] if approved_decision else
                                 ["premise_requires_user_decision"] if requested == "premise" else
                                 ["meaning_or_authority_unknown"]), "created_at": utc_now()}
    proposal["resolution_hash"] = fingerprint(proposal)
    saved = ContinuityStore(db).put(conn, req, "resolution", proposal, basis_hash=assessment["basis_hash"])
    return {"resolution_ref": saved["id"], "resolution_hash": saved["body_hash"], "state": state,
            "decision_ref": proposal["decision_ref"], "required_action": proposal["required_action"],
            "authority_source_refs": assessment["body"].get("delegation_refs", [])}


def _actual_applicability(db, req, workspace, basis):
    payload = _payload(req)
    # Reuse's adapter builds selector values from current records/files and validates
    # the stored P2 verification and artifact bytes; caller fingerprints are ignored.
    from .changes import _verify_source_mapping
    mapping = _verify_source_mapping(db, req, workspace, basis)
    allowed = {"definition", "run_id", "workspace", "paths", "target_id", "command", "inputs",
               "verification_id", "event_id", "reason", "repository_id", "relative_graph_path",
               "expected_source", "change_preview", "change_set", "rule_version", "max_depth"}
    actual_req = dict(req)
    actual_payload = {key: payload[key] for key in allowed if key in payload}
    actual_payload.update({"workspace": str(workspace), "repository_id": mapping["repository_id"],
                           "relative_graph_path": mapping["relative_graph_path"]})
    actual_req["payload"] = actual_payload
    actual_req["operation"] = "resolve_reuse"
    prepared, early = reuse._actual_request(db, actual_req)
    if early:
        status = "unknown" if early.get("status") == "unknown" else "not_applicable"
        return {"status": status, "reason_codes": [early.get("reason", "reuse_check_incomplete")],
                "verification_ref": payload.get("verification_id"), "condition_hash": None,
                "evidence_refs": []}
    normalized, key, actual = prepared
    candidate = actual.get("candidate")
    if not candidate or key.get("status") != "ready":
        return {"status": "not_applicable", "reason_codes": ["selected_conditions_or_evidence_changed"],
                "verification_ref": payload.get("verification_id"),
                "condition_hash": key.get("key_sha256"), "evidence_refs": []}
    row = candidate["verification"]
    # A later failed/blocked/aborted verification invalidates re-use; the F6 adapter
    # already checks this before returning candidate. Verify it again at this boundary.
    with closing(db.connect()) as conn:
        later_failure = _failure_after(conn, row["definition_id"], row["definition_version"],
                                       actual["record"]["scope_id"], row["verification_rowid"])
    if later_failure:
        return {"status": "not_applicable", "reason_codes": ["later_nonpass_exists"],
                "verification_ref": row["id"], "condition_hash": key["key_sha256"],
                "evidence_refs": []}
    evidence_refs = candidate.get("evidence_refs", [])
    if not evidence_refs:
        return {"status": "unknown", "reason_codes": ["evidence_access_unverified"],
                "verification_ref": row["id"], "condition_hash": key["key_sha256"], "evidence_refs": []}
    return {"status": "applicable", "reason_codes": [], "verification_ref": row["id"],
            "definition_ref": {"id": normalized["definition_id"], "version": normalized["definition_version"]},
            "condition_hash": key["key_sha256"], "evidence_refs": evidence_refs,
            "verification_outcome_preserved": row["outcome"], "verification_state_preserved": row["state"]}


def _file_journal_start(db, req, kind, basis_hash):
    metadata = {"operation": req["operation"], "scope_hash": fingerprint(_payload(req).get("paths", [])),
                "ref_hash": fingerprint({key: _payload(req).get(key) for key in
                                         ("assessment_ref", "resolution_ref", "verification_id", "change_ref", "index_ref")})}
    with db.write() as conn:
        return ContinuityStore(db).begin_effect(conn, req, kind, metadata, basis_hash=basis_hash)


def execute_file(db, req):
    operation = req.get("operation")
    if operation == "assess_alignment":
        return assess_alignment(db, req)
    if operation == "read_applicability":
        return read_applicability(db, req)
    if operation == "apply_alignment":
        result = apply_alignment(db, req)
        if isinstance(result, tuple) and len(result) == 2:
            return result
        from ..service import response
        return response(req["request_id"], result=result), 0
    raise PmtError("operation_unsupported", "Unsupported alignment operation")


def read_applicability(db, req):
    payload = _payload(req)
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        raise PmtError("source_paths_required", "Current source access requires explicit claimed paths")
    if "." not in paths:
        # The existing P2 snapshot resolver walks the selected workspace when it
        # computes F6 values. Require a real whole-workspace claim before invoking it.
        raise PmtError("workspace_scope_required", "Applicability checks require a current whole-workspace claim", 3)
    basis = _get(db, req, payload.get("basis_ref"), "basis")
    workspace, run, graph, report, graph_hash = _project_graph(db, req, paths, basis)
    from .changes import _replay, _verify_source_mapping
    _verify_source_mapping(db, req, workspace, basis)
    replay = _replay(db, req)
    if replay is not None:
        return replay
    result = _actual_applicability(db, req, workspace, basis)
    # _actual_request owns and closes its own read connection; verify all returned evidence refs again.
    with closing(db.connect()) as conn:
        if result["status"] == "applicable":
            ids = [item.get("id") for item in result.get("evidence_refs", []) if isinstance(item, dict)]
            valid, reasons, hashes = _validate_evidence(db, conn, ids, req["scope_id"])
            if reasons or len(valid) != len(ids):
                result["status"] = "not_applicable"
                result["reason_codes"] = ["evidence_not_ready_or_hash_invalid"]
                result["evidence_refs"] = []
            else:
                result["evidence_refs"] = [{"id": item, "sha256": hashes[item]} for item in valid]
    result.update({"basis_ref": basis["id"], "basis_hash": basis["body_hash"],
                   "graph_hash": graph_hash, "selector_version": "pmt-reuse-key-v1",
                   "captured_at": utc_now()})
    result["applicability_hash"] = fingerprint(result)
    journal = _file_journal_start(db, req, "read_applicability", basis["body_hash"])
    store = ContinuityStore(db)
    def commit(conn, request):
        authorize(db, conn, request)
        for path in paths:
            work_access(db, conn, request, [path], str(workspace))
        saved = store.put(conn, request, "applicability", result, basis_hash=basis["body_hash"])
        store.update_effect(conn, request, journal["id"], "completed",
                           {"applicability_ref": saved["id"], "status": result["status"],
                            "condition_hash": result.get("condition_hash")})
        return {"applicability_ref": saved["id"], "applicability_hash": saved["body_hash"],
                "status": result["status"], "reason_codes": result["reason_codes"],
                "verification_ref": result.get("verification_ref"),
                "condition_hash": result.get("condition_hash"), "effect_ref": journal["id"]}
    envelope, code = db.run_request(req, commit, authorize=lambda conn, request: authorize(db, conn, request))
    if code != 0:
        with db.connect() as conn:
            current = store.get_effect(conn, req, journal["id"])
        if current["state"] != "completed":
            _effect_state(db, req, journal["id"], "unknown", {"reason_code": "metadata_commit_failed"})
    return envelope, code


def _effect_state(db, req, effect_id, state, outcome):
    with db.write() as conn:
        return ContinuityStore(db).update_effect(conn, req, effect_id, state, outcome)


def _actual_completed_effect(db, req, effect_id, kind):
    """Read a completed existing P3 effect journal and return its persisted outcome."""
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT * FROM phase3_journal WHERE id=? AND scope_id=? AND owner_actor=? AND owner_session=?",
                           (effect_id, req["scope_id"], req["actor"], req["session_id"])).fetchone()
        if not row or row["kind"] != kind:
            return None
        body = json.loads(row["body_json"])
        outcome = json.loads(row["outcome_json"]) if row["outcome_json"] else None
        if kind == "graph_change":
            complete = body.get("stage") == "completed" and isinstance(outcome, dict) and outcome.get("new_graph_hash")
        else:
            complete = body.get("stage") == "completed" and isinstance(outcome, dict)
        return {"body": body, "outcome": outcome} if complete else None


def _actual_step_directive_effect(db, req, request_id, allowed_step_refs):
    """Verify an existing save_step_directive request against its current SQL/resource effect."""
    from ..resources import check_artifact
    original = db.get_request_result(request_id, actor=req["actor"], session_id=req["session_id"])
    if not original:
        raise PmtError("step_effect_unverified", "Directive request receipt is unavailable to this owner", 3)
    envelope, code = original
    result = envelope.get("result") or {}
    step_id, artifact_id = result.get("step_id"), result.get("directive_id")
    if code or not envelope.get("ok") or not isinstance(step_id, str) or not isinstance(artifact_id, str):
        raise PmtError("step_effect_unverified", "Receipt is not a completed Step directive publication", 3)
    if step_id not in allowed_step_refs:
        raise PmtError("step_effect_outside_assessment", "Directive target is outside the assessed affected tasks", 3)
    with closing(db.connect()) as conn:
        from ..phase2_common import project_scope_id
        row = conn.execute("SELECT r.scope_id,r.revision,s.directive_id,s.directive_version FROM records r "
                           "JOIN step_specs s ON s.step_id=r.id WHERE r.id=? AND r.kind='step'", (step_id,)).fetchone()
        artifact = conn.execute("SELECT scope_id,sha256,state FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        event = conn.execute("SELECT payload_json FROM events WHERE event_type='planning.step_instruction_versioned' "
                             "AND record_id=? ORDER BY recorded_at DESC LIMIT 1", (step_id,)).fetchone()
        if (not row or project_scope_id(conn, row["scope_id"]) != req["scope_id"] or
                row["directive_id"] != artifact_id or row["directive_version"] != result.get("directive_version") or
                not artifact or artifact["scope_id"] != req["scope_id"] or artifact["state"] != "ready" or
                not event):
            raise PmtError("step_effect_readback_mismatch", "Current Step directive record differs from its receipt", 3)
        event_body = json.loads(event["payload_json"])
        if (event_body.get("directive_id") != artifact_id or
                event_body.get("directive_version") != result.get("directive_version")):
            raise PmtError("step_effect_event_mismatch", "Directive publication event differs from its receipt", 3)
        checked = check_artifact(db, conn, artifact_id)
        if not checked.get("valid") or checked.get("sha256") != artifact["sha256"]:
            raise PmtError("step_effect_resource_invalid", "Current directive resource failed its hash check", 3)
        return {"step_ref": step_id, "revision": row["revision"],
                "directive_ref": artifact_id, "directive_version": row["directive_version"],
                "directive_hash": artifact["sha256"], "event_ref_hash": fingerprint(event_body)}


def _active_workspace_conflicts(db, req, current_run_id, workspace, affected_record_refs=()):
    from ..phase2_common import project_scope_id
    with closing(db.connect()) as conn:
        rows = conn.execute("SELECT r.id,r.step_id,r.workspace,r.state,s.scope_id FROM execution_runs r "
                            "JOIN records s ON s.id=r.step_id WHERE r.state IN "
                            "('queued','starting','running','review_pending','reconciling','cancel_requested')")
        for row in rows:
            if row["id"] == current_run_id:
                if row["step_id"] in set(affected_record_refs):
                    return {"run_ref": row["id"], "task_ref": row["step_id"], "state": row["state"]}
                continue
            if project_scope_id(conn, row["scope_id"]) != req["scope_id"]:
                continue
            try:
                if Path(row["workspace"]).resolve() == workspace.resolve():
                    return {"run_ref": row["id"], "task_ref": row["step_id"], "state": row["state"]}
            except OSError:
                return {"run_ref": row["id"], "task_ref": row["step_id"], "state": "unknown"}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='continuity_journal'").fetchone():
            pending = conn.execute("SELECT id,kind,state FROM continuity_journal WHERE scope_id=? "
                "AND state IN ('prepared','applying','partial','unknown','conflict') ORDER BY created_at LIMIT 1",
                (req["scope_id"],)).fetchone()
            if pending:
                return {"effect_ref": pending["id"], "kind": pending["kind"], "state": pending["state"]}
        pending_file = conn.execute("SELECT id,kind,body_json FROM phase3_journal WHERE scope_id=? AND kind IN "
            "('graph_change','document_segments') ORDER BY created_at", (req["scope_id"],))
        for row in pending_file:
            body = json.loads(row["body_json"])
            if body.get("stage") != "completed":
                return {"effect_ref": row["id"], "kind": row["kind"], "state": body.get("stage", "unknown")}
    return None


def apply_alignment(db, req):
    payload = _payload(req)
    paths = payload.get("paths")
    if not isinstance(paths, list) or not paths:
        raise PmtError("source_paths_required", "Current graph and publication scopes must be explicit")
    assessment = _get(db, req, payload.get("assessment_ref"), "assessment")
    resolution = _get(db, req, payload.get("resolution_ref"), "resolution")
    basis = _get(db, req, assessment["body"].get("basis_ref"), "basis")
    workspace, run, graph, report, graph_hash = _project_graph(db, req, paths, basis)
    from .changes import _verify_source_mapping
    source_mapping = _verify_source_mapping(db, req, workspace, basis)
    graph_path = source_mapping["relative_graph_path"]
    access_paths = sorted(set(paths + [graph_path]), key=str.casefold)
    if resolution["body"].get("assessment_ref") != assessment["id"] or resolution["body"].get("assessment_hash") != assessment["body_hash"]:
        raise PmtError("alignment_resolution_mismatch", "Resolution is not bound to this assessment", 3)
    from .changes import _replay
    replay = _replay(db, req)
    if replay is not None:
        return replay
    if assessment["body"].get("state") != "assessed" or assessment["body"].get("unknown"):
        return {"state": "incomplete", "applied": False, "required_action": "refresh_assessment",
                "reason_codes": ["assessment_incomplete_or_stale"], "applied_pointer_advanced": False}
    conflict = _active_workspace_conflicts(db, req, run["id"], workspace,
                                            assessment["body"].get("affected_record_refs", []))
    if conflict:
        return {"state": "reconciliation_required", "applied": False,
                "reason_codes": ["active_run_or_effect_requires_reconciliation"],
                "conflict_ref": conflict.get("run_ref") or conflict.get("effect_ref"),
                "applied_pointer_advanced": False}
    resolution_body = resolution["body"]
    if resolution_body.get("state") not in {"delegated_method_candidate", "premise_decision_recorded"}:
        return {"state": "awaiting_user", "applied": False,
                "reason_codes": ["user_decision_not_recorded"], "applied_pointer_advanced": False,
                "required_action": "record_authoritative_user_decision"}
    if not payload.get("graph_effect_ref"):
        return {"state": "awaiting_user", "applied": False,
                "reason_codes": ["actual_graph_effect_receipt_required"], "applied_pointer_advanced": False}
    effect = _actual_completed_effect(db, req, payload["graph_effect_ref"], "graph_change")
    if not effect:
        raise PmtError("graph_effect_unverified", "Graph effect receipt is not a completed actual F1 journal", 3)
    journal = effect["body"]
    outcome = effect["outcome"] or {}
    if journal.get("old_graph_hash") != assessment["body"].get("observed_graph_hash"):
        return {"state": "reconciliation_required", "applied": False,
                "reason_codes": ["graph_effect_before_state_does_not_match_assessment"],
                "applied_pointer_advanced": False}
    if journal.get("stage_path") and not isinstance(journal.get("new_file_sha256"), str):
        raise PmtError("graph_effect_unverified", "Graph effect receipt lacks actual target hashes", 3)
    if resolution_body.get("state") == "delegated_method_candidate":
        eligible = set(resolution_body.get("eligible_method_node_refs", []))
        operations = outcome.get("operations") or journal.get("operation_summary", [])
        target_ids = {target for item in operations if isinstance(item, dict)
                      for target in (item.get("target_ids", []) +
                                     ([item.get("id")] if item.get("op") == "update" else []))
                      if isinstance(target, str)}
        if any(not isinstance(item, dict) or item.get("op") != "update" for item in operations):
            target_ids.add("unsupported-operation")
        if (not target_ids or not target_ids <= eligible or journal.get("created_ids") or journal.get("retired_ids")):
            return {"state": "awaiting_user", "applied": False,
                    "reason_codes": ["graph_effect_exceeds_explicit_delegation_scope"],
                    "effect_target_refs": sorted(target_ids), "eligible_method_node_refs": sorted(eligible),
                    "created_node_count": len(journal.get("created_ids", [])),
                    "retired_node_count": len(journal.get("retired_ids", [])),
                    "applied_pointer_advanced": False}
        # Recheck each delegation record against current SQLite and graph source.
        with closing(db.connect()) as conn:
            for ref in assessment["body"].get("delegation_refs", []):
                if ref.get("node_id") not in target_ids:
                    continue
                decision = _decision_record(conn, req, ref.get("decision_ref"), expected_kind="delegate")
                node = next((item for item in graph["nodes"] if item.get("id") == ref.get("node_id")), None)
                if (not decision or decision["revision"] != ref.get("decision_revision") or not node or
                        node.get("stop_reason") != "user_delegated" or
                        fingerprint(node.get("delegated_scope")) != ref.get("scope_hash") or
                        decision["body"].get("delegation_scope") != node.get("delegated_scope")):
                    return {"state": "awaiting_user", "applied": False,
                            "reason_codes": ["delegation_source_changed_or_unverified"],
                            "applied_pointer_advanced": False}
    else:
        with closing(db.connect()) as conn:
            decision = _decision_record(conn, req, resolution_body.get("decision_ref"))
        if (not decision or decision["revision"] != resolution_body.get("decision_revision") or
                decision["parent_id"] != resolution_body.get("decision_target_ref") or
                decision["parent_id"] not in set(assessment["body"].get("affected_record_refs", []))):
            return {"state": "awaiting_user", "applied": False,
                    "reason_codes": ["current_user_decision_unavailable"], "applied_pointer_advanced": False}
    with closing(db.connect()) as conn:
        for path in access_paths:
            work_access(db, conn, req, [path], str(workspace))
    actual_graph = strict_json_loads((workspace / graph_path).read_bytes(), max_bytes=8 * 1024 * 1024)
    current_report = validate_graph(actual_graph, req["scope_id"], complete=False)
    actual_hash = hashlib.sha256((workspace / graph_path).read_bytes()).hexdigest()
    if current_report["sha256"] != outcome.get("new_graph_hash") or actual_hash != journal.get("new_file_sha256"):
        return {"state": "reconciliation_required", "applied": False,
                "reason_codes": ["graph_effect_readback_mismatch"], "applied_pointer_advanced": False}
    if not payload.get("document_effect_ref"):
        return {"state": "partial", "applied": False, "graph_effect_ref": payload["graph_effect_ref"],
                "reason_codes": ["actual_document_publish_receipt_required"], "applied_pointer_advanced": False}
    doc_effect = _actual_completed_effect(db, req, payload["document_effect_ref"], "document_segments")
    if not doc_effect:
        raise PmtError("document_effect_unverified", "Document effect receipt is not a completed actual publication journal", 3)
    doc_body = doc_effect["body"]
    doc_outcome = doc_effect["outcome"] or {}
    if doc_body.get("graph_hash") != outcome.get("new_graph_hash"):
        return {"state": "reconciliation_required", "applied": False,
                "reason_codes": ["document_receipt_uses_a_different_graph"], "applied_pointer_advanced": False}
    doc_path = doc_body.get("document_path")
    if not isinstance(doc_path, str):
        return {"state": "incomplete", "applied": False, "reason_codes": ["document_target_unknown"],
                "applied_pointer_advanced": False}
    doc_path = Path(doc_path)
    if doc_path.is_absolute() or ".." in doc_path.parts:
        raise PmtError("document_target_invalid", "Document receipt target is not workspace-relative", 3)
    with closing(db.connect()) as conn:
        work_access(db, conn, req, [doc_path.as_posix()], str(workspace))
    target = workspace / doc_path
    _reject_links(target)
    if not target.is_file():
        return {"state": "reconciliation_required", "applied": False,
                "reason_codes": ["document_effect_target_missing"], "applied_pointer_advanced": False}
    document_hash = hashlib.sha256(target.read_bytes()).hexdigest()
    expected_doc_hash = doc_outcome.get("document_hash") or doc_body.get("candidate_hash")
    if expected_doc_hash and document_hash != expected_doc_hash:
        return {"state": "reconciliation_required", "applied": False,
                "reason_codes": ["document_effect_readback_mismatch"], "applied_pointer_advanced": False}
    step_effect_refs = payload.get("step_effect_refs", [])
    if not isinstance(step_effect_refs, list) or len(step_effect_refs) > 100:
        raise PmtError("step_effect_refs_invalid", "Step effect refs must be a bounded array")
    step_effects = [_actual_step_directive_effect(db, req, ref,
        assessment["body"].get("affected_record_refs", [])) for ref in step_effect_refs]
    selector = {"repository_id": basis["body"].get("source", {}).get("repository_id"),
                "branch": basis["body"].get("source", {}).get("branch"),
                "workspace_ref": basis["body"].get("source", {}).get("workspace_ref"),
                "task_id": payload.get("task_id"), "purpose": "applied_alignment",
                "environment_id": db.environment_id}
    store = ContinuityStore(db)
    with closing(db.connect()) as conn:
        pointer = store.read_pointer(conn, req, selector)
    expected = payload.get("expected_pointer_revision")
    if type(expected) is not int or expected != pointer["revision"]:
        raise PmtError("revision_conflict", "Applied alignment pointer revision is stale", 3)
    receipt = {"state": "applied", "assessment_ref": assessment["id"], "assessment_hash": assessment["body_hash"],
        "resolution_ref": resolution["id"], "decision_ref": resolution_body.get("decision_ref"),
        "basis_ref": basis["id"], "basis_hash": basis["body_hash"],
        "graph_effect_ref": payload["graph_effect_ref"], "before_graph_hash": journal.get("old_graph_hash"),
        "graph_hash": actual_hash,
        "graph_revision": actual_graph["graph_version"], "document_effect_ref": payload["document_effect_ref"],
        "document_hash": document_hash, "preserved_unrelated_ref_hash": assessment["body"].get("unaffected_scope_hash"),
        "step_directive_effects": step_effects,
        "unknown": [], "captured_at": utc_now()}
    receipt["receipt_hash"] = fingerprint(receipt)
    journal = _file_journal_start(db, req, "apply_alignment", basis["body_hash"])
    def commit(conn, request):
        authorize(db, conn, request)
        for path in access_paths + [doc_path.as_posix()]:
            work_access(db, conn, request, [path], str(workspace))
        saved = store.put(conn, request, "alignment", receipt, basis_hash=basis["body_hash"])
        pointer_result = store.advance_pointer(conn, request, selector, saved["id"], expected)
        store.update_effect(conn, request, journal["id"], "completed", {
            "alignment_ref": saved["id"], "receipt_hash": receipt["receipt_hash"],
            "pointer_revision": pointer_result["revision"], "graph_hash": actual_hash,
            "document_hash": document_hash})
        return {"state": "applied", "alignment_ref": saved["id"], "receipt_hash": receipt["receipt_hash"],
                "pointer": pointer_result, "effect_ref": journal["id"], "graph_hash": actual_hash,
                "document_hash": document_hash, "applied_pointer_advanced": True}
    return db.run_request(req, commit, authorize=lambda conn, request: authorize(db, conn, request))


def handle(db, conn, req):
    if req.get("operation") == "propose_semantic_resolution":
        return propose_semantic_resolution(db, conn, req)
    raise PmtError("operation_unsupported", "Unsupported alignment operation")
