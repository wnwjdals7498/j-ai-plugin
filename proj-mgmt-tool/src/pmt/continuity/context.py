"""Two-stage, bounded resume projections for phase four."""
from __future__ import annotations

import json
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from ..errors import PmtError
from .contracts import authorize, budget, bounded_result, digest, work_access
from .storage import ContinuityStore

READ_OPERATIONS = {"compose_resume_overview", "propose_next_action"}
FILE_OPERATIONS = {"compose_task_resume", "read_resume_detail"}
WRITE_OPERATIONS: set[str] = set()

_ACTIVE_STATES = {"running", "review_pending", "pending", "reconciling", "cancel_requested"}


def _wire_size(value: Any) -> tuple[int, int]:
    wire = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return len(wire.encode("utf-8")), max(1, wire.count("\n") + 1)


def compose_overview(*, scope: Mapping[str, Any], facts: Mapping[str, Any],
                     checkpoint: Mapping[str, Any] | None,
                     role: str, max_bytes: int, max_lines: int) -> dict[str, Any]:
    """Produce a metadata-only overview; mandatory omissions remain visible."""
    if type(max_bytes) is not int or max_bytes <= 0 or type(max_lines) is not int or max_lines <= 0:
        raise ValueError("resume budgets must be positive byte and line limits")
    required = {
        "scope": dict(scope),
        "direction": dict(facts.get("direction", {})),
        "work_items": list(facts.get("work_items", [])),
        "decision_refs": list(facts.get("decision_refs", [])),
        "decision_summaries": list(facts.get("decision_summaries", [])),
        "direction_refs": list(facts.get("direction_refs", [])),
        "implementation": dict(facts.get("implementation", {"level": "unknown",
            "source_currentness": "unknown_without_selected_basis",
            "current_applicability": "unknown_requires_read_applicability",
            "unknowns": ["implementation_receipts_not_selected"]})),
        "current_status": list(facts.get("current_status", [])),
        "active_execution": list(facts.get("active_execution", [])),
        "unknowns": list(facts.get("unknowns", [])),
    }
    optional = {
        "checkpoint_ref": checkpoint.get("id") if isinstance(checkpoint, Mapping) else None,
        "basis_ref": checkpoint.get("basis_ref") if isinstance(checkpoint, Mapping) else None,
        "attention": list(facts.get("attention", [])),
        "candidate_work_refs": list(facts.get("candidate_work_refs", [])),
        "detail_refs": list(facts.get("detail_refs", [])),
        "role": role,
    }
    result = {**required, **optional, "incomplete": False,
              "missing_required": [], "omitted_optional": []}
    size, lines = _wire_size(result)
    if size <= max_bytes and lines <= max_lines:
        return {**result, "delivery": {"bytes": size, "lines": lines,
                                        "unit": "utf8", "token_count": None}}

    # Keep the fields that explain why a bounded overview cannot be complete.
    omitted_optional = []
    for key in ("detail_refs", "candidate_work_refs", "attention", "role",
                "basis_ref", "checkpoint_ref"):
        result[key] = None if key in {"basis_ref", "checkpoint_ref", "role"} else []
        omitted_optional.append(key)
        size, lines = _wire_size(result)
        if size <= max_bytes and lines <= max_lines:
            break
    missing_required = []
    if size > max_bytes or lines > max_lines:
        for key in ("active_execution", "current_status", "decision_summaries", "decision_refs", "work_items",
                    "direction", "direction_refs", "scope", "unknowns"):
            value = result[key]
            if value:
                result[key] = []
                missing_required.append(key)
                size, lines = _wire_size(result)
                if size <= max_bytes and lines <= max_lines:
                    break
    result.update({"incomplete": True, "missing_required": missing_required,
                   "omitted_optional": omitted_optional})
    size, lines = _wire_size(result)
    return {**result, "delivery": {"bytes": size, "lines": lines,
                                    "unit": "utf8", "token_count": None}}


def propose_next_action(*, facts: Mapping[str, Any], basis_status: str,
                        authority_current: bool, alignment: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Return a conditional suggestion. This function never changes PMT state."""
    active = [item for item in facts.get("active_execution", [])
              if isinstance(item, Mapping) and item.get("state") in (_ACTIVE_STATES | {"queued", "starting"})]
    required = ["current_metadata_authority", "current_basis"]
    if not authority_current:
        return {"action": "request_selection", "reason": "current_authority_unknown_or_denied",
                "required_refs": [], "required_conditions": required,
                "unknown": ["current_authority"], "executable": False}
    if active:
        active_item = active[0]
        state = active_item.get("state")
        action = "inspect_existing_result" if state == "review_pending" else (
            "observe_existing_run" if state in {"running", "cancel_requested"} else "wait")
        return {"action": action, "reason": "existing_execution_is_unresolved",
                "required_refs": [ref for ref in (active_item.get("run_ref"), active_item.get("receipt_ref")) if ref],
                "required_conditions": ["query_authoritative_run_state", "preserve_current_owner"],
                "unknown": [], "executable": False}
    if basis_status == "changed":
        return {"action": "analyze_changes", "reason": "basis_component_changed",
                "required_refs": list(facts.get("basis_refs", [])),
                "required_conditions": required + ["inspect_changed_components"],
                "unknown": [], "executable": False}
    if basis_status != "unchanged":
        return {"action": "request_selection", "reason": "current_source_basis_needs_owner_check",
                "required_refs": list(facts.get("basis_refs", [])),
                "required_conditions": required + ["current_work_access_and_source_capture"],
                "unknown": ["basis_currentness"], "executable": False}
    if not facts.get("scope_selected"):
        return {"action": "request_selection", "reason": "scope_selection_required",
                "required_refs": [], "required_conditions": ["explicit_project_or_task_scope"],
                "unknown": ["selected_scope"], "executable": False}
    if isinstance(alignment, Mapping) and alignment.get("status") in {"conflict", "attention"}:
        return {"action": "request_selection", "reason": "premise_or_alignment_requires_review",
                "required_refs": list(alignment.get("refs", [])),
                "required_conditions": ["main_or_user_review"], "unknown": [], "executable": False}
    candidate = facts.get("next_action")
    permitted = {"refresh_plan", "start_confirmed_step", "verify_integration", "wait"}
    if isinstance(candidate, Mapping) and candidate.get("action") in permitted:
        return {"action": candidate["action"], "reason": candidate.get("reason", "current facts support this candidate"),
                "required_refs": list(candidate.get("required_refs", [])),
                "required_conditions": list(candidate.get("required_conditions", [])) + ["revalidate_before_action"],
                "unknown": list(candidate.get("unknown", [])), "executable": False}
    return {"action": "request_selection", "reason": "no_supported_action_from_current_facts",
            "required_refs": [], "required_conditions": ["explicit_current_decision"],
            "unknown": ["next_action_basis"], "executable": False}


def task_resume_request(*, work_access: Mapping[str, Any], basis: Mapping[str, Any],
                        role: str, budget: Mapping[str, Any], detail_refs=()) -> dict[str, Any]:
    """Bind task resume inputs to current ownership; private text stays in F5."""
    if not work_access.get("authorized") or not work_access.get("owner_current"):
        raise PermissionError("task resume requires current work owner access")
    if basis.get("complete") is not True:
        raise ValueError("task resume requires a complete basis")
    return {"work_access_ref": work_access.get("ref"), "basis_ref": basis.get("ref"),
            "role": role, "budget": dict(budget), "detail_refs": list(detail_refs),
            "private_directive_source": "existing_task_context_service"}


def _scope(req):
    scope_id = req.get("scope_id")
    if not isinstance(scope_id, str) or not scope_id:
        raise PmtError("scope_required", "An explicit project scope is required")
    return scope_id


def _facts(db, conn, req):
    from .current import _facts as read_facts
    return read_facts(conn, req, db)


def _basis_currentness(db, conn, req, facts, basis_ref):
    if not isinstance(basis_ref, str):
        return "unknown", None
    basis_obj = ContinuityStore(db).get(conn, req, basis_ref, kind="basis")
    basis = basis_obj["body"]
    work_hash = basis.get("work", {}).get("capture_ref")
    if not work_hash or not facts.get("revision_hash"):
        return "unknown", basis_obj
    if work_hash != facts["revision_hash"]:
        return "changed", basis_obj
    # Metadata-only reads cannot validate Git, selected files or environment.
    return "unknown", basis_obj


def _basis_records(body):
    work = body.get("work", {}) if isinstance(body, Mapping) else {}
    records = work.get("records", []) if isinstance(work, Mapping) else []
    return {item.get("id"): item.get("revision") for item in records
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)}


def _resume_change_evidence(db, conn, req, payload, current_basis_obj, current_basis):
    """Bind changed-source resume claims to retained change/R3/alignment objects.

    The returned values are deliberately small metadata summaries. A caller
    supplied ref selects an immutable object but never makes it current or
    applied; those states are derived from the stored bodies and current pointer.
    """
    from .current import validate_basis_components

    store = ContinuityStore(db)
    source = current_basis.get("source", {})
    task_id = (payload.get("task_id") or req.get("record_id") or
               current_basis.get("work", {}).get("task_id"))
    selector_base = {"repository_id": source.get("repository_id"),
                     "branch": source.get("branch"), "workspace_ref": source.get("workspace_ref"),
                     "task_id": task_id, "environment_id": db.environment_id}
    checkpoint_selector = {key: value for key, value in selector_base.items()
                          if key != "environment_id"} | {
                              "purpose": payload.get("checkpoint_purpose", "current")}
    checkpoint_pointer = store.read_pointer(conn, req, checkpoint_selector)
    checkpoint = (store.get(conn, req, checkpoint_pointer["object_id"], kind="checkpoint")
                  if checkpoint_pointer.get("object_id") else None)
    checkpoint_basis_ref = checkpoint["body"].get("basis_ref") if checkpoint else None
    current_basis_ref = current_basis_obj["id"]
    checkpoint_basis = (store.get(conn, req, checkpoint_basis_ref, kind="basis")
                        if checkpoint_basis_ref else None)
    checkpoint_comparison = (validate_basis_components(checkpoint_basis["body"], current_basis)
                             if checkpoint_basis else {"status": "unknown"})
    if checkpoint_comparison.get("status") == "unchanged":
        return {"status": "no_change_confirmed", "change_ref": None,
                "assessment_ref": None, "resolution_ref": None, "applicability_ref": None,
                "alignment_ref": None, "checkpoint_ref": checkpoint["id"],
                "checkpoint_basis_ref": checkpoint_basis_ref,
                "current_basis_ref": current_basis_ref, "changed_path_refs": [],
                "affected_node_refs": [], "required_action": None, "reason_codes": [],
                "applied": False, "complete": True}

    change_pointer = store.read_pointer(conn, req, selector_base | {"purpose": "observed_change"})
    change_ref = payload.get("change_ref") or change_pointer.get("object_id")
    change = store.get(conn, req, change_ref, kind="change") if change_ref else None
    if change is None:
        same_checkpoint = checkpoint_comparison.get("status") == "unchanged"
        return {"status": "no_change_confirmed" if same_checkpoint else "unknown",
                "change_ref": None, "assessment_ref": None, "applicability_ref": None,
                "alignment_ref": None, "checkpoint_ref": checkpoint["id"] if checkpoint else None,
                "checkpoint_basis_ref": checkpoint_basis_ref,
                "current_basis_ref": current_basis_ref,
                "reason_codes": [] if same_checkpoint else ["change_evidence_unavailable"],
                "changed_path_refs": [], "affected_node_refs": [],
                "required_action": None if same_checkpoint else "collect_current_change_evidence",
                "applied": False, "complete": same_checkpoint}

    change_body = change["body"]
    after_ref = change_body.get("after_basis_ref") or change_body.get("current_basis_ref")
    after = store.get(conn, req, after_ref, kind="basis") if isinstance(after_ref, str) else None
    change_reasons = []
    if checkpoint_basis_ref and change_body.get("before_basis_ref") != checkpoint_basis_ref:
        change_reasons.append("change_does_not_continue_current_checkpoint")
    if not after:
        change_reasons.append("change_after_basis_not_current")
    elif change_body.get("after_basis_hash") not in {None, after["body_hash"]}:
        change_reasons.append("change_after_basis_hash_mismatch")
    if change_body.get("state") not in {"complete", "no_change"} or change_body.get("coverage") != "complete":
        change_reasons.append("change_capture_incomplete")
    if after and validate_basis_components(after["body"], current_basis)["status"] != "unchanged":
        change_reasons.append("change_after_basis_semantically_stale")

    if (change_body.get("state") == "no_change" and not change_reasons
            and checkpoint_comparison.get("status") == "unchanged"
            and checkpoint_basis_ref == change_body.get("before_basis_ref")):
        return {"status": "no_change_confirmed", "change_ref": change["id"],
                "change_hash": change["body_hash"], "assessment_ref": None,
                "resolution_ref": None, "applicability_ref": None, "alignment_ref": None,
                "checkpoint_ref": checkpoint["id"], "checkpoint_basis_ref": checkpoint_basis_ref,
                "current_basis_ref": current_basis_ref, "changed_path_refs": [],
                "affected_node_refs": [], "required_action": None, "reason_codes": [],
                "applied": False, "complete": True}

    assessment_ref = payload.get("assessment_ref")
    assessment = store.get(conn, req, assessment_ref, kind="assessment") if assessment_ref else None
    assessment_reasons = []
    if assessment is None:
        assessment_reasons.append("current_change_assessment_unavailable")
    else:
        assessed = assessment["body"]
        if assessed.get("change_ref") != change["id"] or assessed.get("change_hash") != change["body_hash"]:
            assessment_reasons.append("assessment_change_mismatch")
        if assessed.get("basis_ref") != (after["id"] if after else None):
            assessment_reasons.append("assessment_basis_mismatch")
        if assessed.get("state") != "assessed" or assessed.get("unknown"):
            assessment_reasons.append("assessment_incomplete_or_unknown")
        if assessed.get("observed_graph_hash") != current_basis.get("contract", {}).get("graph_hash"):
            assessment_reasons.append("assessment_graph_stale")
        if after and _basis_records(after["body"]) != _basis_records(current_basis):
            assessment_reasons.append("work_or_directive_version_changed_since_assessment_basis")

    applicability_ref = payload.get("applicability_ref")
    applicability = (store.get(conn, req, applicability_ref, kind="applicability")
                     if applicability_ref else None)
    applicability_reasons = []
    if applicability is None:
        applicability_reasons.append("current_applicability_unavailable")
    else:
        applied_body = applicability["body"]
        applicability_basis = store.get(conn, req, applied_body.get("basis_ref"), kind="basis")
        if (applied_body.get("basis_hash") != applicability_basis["body_hash"] or
                validate_basis_components(applicability_basis["body"], current_basis)["status"] != "unchanged"):
            applicability_reasons.append("applicability_basis_stale")
        if applied_body.get("graph_hash") != current_basis.get("contract", {}).get("graph_hash"):
            applicability_reasons.append("applicability_graph_stale")
        if applied_body.get("status") == "unknown":
            applicability_reasons.append("applicability_unknown")

    resolution = (store.get(conn, req, payload.get("resolution_ref"), kind="resolution")
                  if payload.get("resolution_ref") else None)
    if resolution and assessment and (resolution["body"].get("assessment_ref") != assessment["id"]
            or resolution["body"].get("assessment_hash") != assessment["body_hash"]):
        assessment_reasons.append("resolution_assessment_mismatch")

    alignment_pointer = store.read_pointer(conn, req, selector_base | {"purpose": "applied_alignment"})
    alignment = (store.get(conn, req, alignment_pointer["object_id"], kind="alignment")
                 if alignment_pointer.get("object_id") else None)
    applied = False
    if alignment:
        receipt = alignment["body"]
        receipt_reasons = []
        applied = bool(receipt.get("state") == "applied" and receipt.get("unknown") == []
            and assessment and receipt.get("assessment_ref") == assessment["id"]
            and receipt.get("assessment_hash") == assessment["body_hash"]
            and resolution and receipt.get("resolution_ref") == resolution["id"]
            and receipt.get("basis_ref") == assessment["body"].get("basis_ref")
            and receipt.get("basis_hash") == assessment["body"].get("basis_hash")
            and receipt.get("graph_revision") == current_basis.get("contract", {}).get("graph_revision")
            and alignment_pointer.get("object_id") == alignment["id"]
            and isinstance(receipt.get("graph_effect_ref"), str)
            and isinstance(receipt.get("document_effect_ref"), str))
        if applied:
            from .alignment import _actual_completed_effect
            graph_effect = _actual_completed_effect(db, req, receipt["graph_effect_ref"], "graph_change")
            document_effect = _actual_completed_effect(db, req, receipt["document_effect_ref"], "document_segments")
            if not graph_effect or not document_effect:
                applied = False
                receipt_reasons.append("alignment_effect_receipt_unavailable")
            else:
                graph_body, graph_outcome = graph_effect["body"], graph_effect["outcome"] or {}
                document_body, document_outcome = document_effect["body"], document_effect["outcome"] or {}
                graph_path = req.get("payload", {}).get("relative_graph_path")
                inventory = store.get(conn, req, current_basis.get("source", {}).get("inventory_ref"), kind="detail")
                items = {item.get("relative_path"): item for item in inventory["body"].get("items", [])
                         if isinstance(item, Mapping)}
                if (not isinstance(graph_path, str) or
                        graph_body.get("old_graph_hash") != assessment["body"].get("observed_graph_hash") or
                        graph_outcome.get("new_graph_hash") != current_basis.get("contract", {}).get("graph_hash") or
                        receipt.get("graph_hash") != graph_body.get("new_file_sha256") or
                        not isinstance(graph_body.get("new_file_sha256"), str) or
                        items.get(graph_path, {}).get("content_hash") != graph_body.get("new_file_sha256")):
                    applied = False
                    receipt_reasons.append("alignment_graph_readback_not_current")
                document_path = document_body.get("document_path")
                if (not isinstance(document_path, str) or Path(document_path).is_absolute()
                        or ".." in Path(document_path).parts):
                    applied = False
                    receipt_reasons.append("alignment_document_target_unknown")
                else:
                    expected_document_hash = (document_outcome.get("document_hash") or
                                              document_body.get("candidate_hash"))
                    if (receipt.get("document_hash") != expected_document_hash or
                            items.get(Path(document_path).as_posix(), {}).get("content_hash") != expected_document_hash):
                        applied = False
                        receipt_reasons.append("alignment_document_readback_not_current")
                old_inventory = store.get(conn, req,
                    after["body"].get("source", {}).get("inventory_ref"), kind="detail") if after else None
                old_items = {item.get("relative_path"): item for item in
                    (old_inventory["body"].get("items", []) if old_inventory else [])
                    if isinstance(item, Mapping)}
                changed_paths = [path for path in set(old_items) | set(items)
                    if old_items.get(path, {}).get("content_hash") != items.get(path, {}).get("content_hash")]
                allowed_effect_paths = {graph_path, Path(document_path).as_posix()
                    if isinstance(document_path, str) and not Path(document_path).is_absolute()
                    and ".." not in Path(document_path).parts else None}
                if (not old_inventory or set(old_items) != set(items)
                        or not changed_paths or any(path not in allowed_effect_paths for path in changed_paths)):
                    applied = False
                    receipt_reasons.append("alignment_inventory_delta_not_bound_to_effects")
        if receipt_reasons:
            assessment_reasons.extend(receipt_reasons)
    if applied:
        # A completed current alignment is allowed to change only its verified
        # graph/document effects after the R3 assessment basis.
        change_reasons = [reason for reason in change_reasons
            if reason != "change_after_basis_semantically_stale"]
        assessment_reasons = [reason for reason in assessment_reasons
            if reason != "assessment_graph_stale"]
    reasons = list(dict.fromkeys(change_reasons + assessment_reasons + applicability_reasons))
    if not applied:
        reasons.append("alignment_not_currently_applied")
    complete = not reasons and applied
    action = None if complete else (
        "refresh_change_assessment" if any("assessment" in item or "basis" in item or "graph" in item for item in reasons)
        else "review_change_and_applicability")
    return {"status": "applied_current" if complete else "change_requires_review",
            "change_ref": change["id"], "change_hash": change["body_hash"],
            "assessment_ref": assessment["id"] if assessment else None,
            "assessment_state": assessment["body"].get("state") if assessment else "unknown",
            "assessment_hash": assessment["body_hash"] if assessment else None,
            "resolution_ref": resolution["id"] if resolution else None,
            "resolution_state": resolution["body"].get("state") if resolution else "unknown",
            "applicability_ref": applicability["id"] if applicability else None,
            "applicability_status": applicability["body"].get("status") if applicability else "unknown",
            "alignment_ref": alignment["id"] if alignment else None,
            "alignment_pointer_revision": alignment_pointer.get("revision", 0),
            "checkpoint_ref": checkpoint["id"] if checkpoint else None,
            "checkpoint_basis_ref": checkpoint_basis_ref, "current_basis_ref": current_basis_ref,
            "changed_path_refs": [item.get("path_ref") for item in change_body.get("facts", [])
                                  if isinstance(item, Mapping) and item.get("path_ref")],
            "affected_node_refs": assessment["body"].get("affected_node_refs", []) if assessment else [],
            "required_action": action, "reason_codes": reasons, "applied": applied,
            "complete": complete}


def handle(db, conn, req):
    """Metadata-only overview and action reads. Task detail is FILE-only."""
    authorize(db, conn, req)
    operation, payload = req.get("operation"), req.get("payload", {})
    if not isinstance(payload, dict):
        raise PmtError("invalid_payload", "Continuity payload must be an object")
    scope_id = _scope(req)
    if operation == "compose_resume_overview":
        selector = payload.get("selector")
        if selector is None:
            selector = {"repository_id": None, "branch": None, "workspace_ref": None,
                        "task_id": payload.get("task_id") or req.get("record_id"),
                        "purpose": "planning", "environment_id": None}
        if not isinstance(selector, dict) or not selector.get("purpose"):
            raise PmtError("selection_required", "An explicit checkpoint purpose is required", 3)
        store = ContinuityStore(db)
        pointer = store.read_pointer(conn, req, selector)
        checkpoint_obj = store.get(conn, req, pointer["object_id"], kind="checkpoint") if pointer["object_id"] else None
        facts_request = dict(req)
        facts_request["payload"] = dict(payload)
        if checkpoint_obj and checkpoint_obj["body"].get("basis_ref"):
            facts_request["payload"].setdefault("basis_ref", checkpoint_obj["body"]["basis_ref"])
        facts = _facts(db, conn, facts_request)
        basis_status, basis_obj = _basis_currentness(
            db, conn, req, facts, checkpoint_obj["body"].get("basis_ref") if checkpoint_obj else None)
        alignment = None
        if isinstance(payload.get("assessment_ref"), str):
            assessment = store.get(conn, req, payload["assessment_ref"], kind="assessment")
            alignment = {"status": assessment["body"].get("status"),
                         "refs": [assessment["id"]]}
        refs = list(facts.get("active_execution", []))
        facts["candidate_work_refs"] = [item["record_ref"] for item in facts.get("work_items", [])
                                         if item.get("kind") in {"work", "item"}]
        facts["basis_refs"] = [basis_obj["id"]] if basis_obj else []
        action = propose_next_action(facts=facts, basis_status=basis_status,
                                     authority_current=True,
                                     alignment=alignment)
        projection = compose_overview(
            scope={"project_ref": scope_id, "task_ref": payload.get("task_id")},
            facts=facts,
            checkpoint=({"id": checkpoint_obj["id"],
                         "basis_ref": checkpoint_obj["body"].get("basis_ref")}
                        if checkpoint_obj else None),
            role=payload.get("role", "main"), **budget(payload))
        result = {"pointer": pointer, "currentness": {"status": basis_status,
                    "work_revision_hash": facts.get("revision_hash"),
                    "source": "unknown_without_current_work_access"},
                  "next_action": action, "overview": projection,
                  "metadata_only": True, "private_detail_read": False,
                  "active_execution_count": len(refs), "complete": projection.get("incomplete") is not True
                               and facts.get("complete") is True}
        return bounded_result(req, result)
    if operation == "propose_next_action":
        facts_request = dict(req)
        facts_request["payload"] = dict(payload)
        facts = _facts(db, conn, facts_request)
        basis_status, basis_obj = _basis_currentness(db, conn, req, facts, payload.get("basis_ref"))
        facts["basis_refs"] = [basis_obj["id"]] if basis_obj else []
        alignment = None
        if isinstance(payload.get("assessment_ref"), str):
            assessment = ContinuityStore(db).get(conn, req, payload["assessment_ref"], kind="assessment")
            alignment = {"status": assessment["body"].get("status"), "refs": [assessment["id"]]}
        return bounded_result(req, propose_next_action(
            facts=facts, basis_status=basis_status, authority_current=True,
            alignment=alignment))
    raise PmtError("operation_unsupported", "Unsupported metadata resume operation")


def execute_file(db, req):
    from ..service import response
    operation, payload = req.get("operation"), req.get("payload", {})
    try:
        if operation == "compose_task_resume":
            return _compose_task_resume(db, req)
        if operation == "read_resume_detail":
            return _read_resume_detail(db, req)
        raise PmtError("operation_unsupported", "Unsupported task resume operation")
    except PmtError as exc:
        if operation == "compose_task_resume":
            try:
                effect_id = __import__("uuid").uuid5(
                    __import__("uuid").UUID(req["request_id"]), "continuity:task_resume").__str__()
                with db.write() as conn:
                    authorize(db, conn, req)
                    store = ContinuityStore(db)
                    if conn.execute("SELECT 1 FROM continuity_journal WHERE id=?", (effect_id,)).fetchone():
                        effect = store.get_effect(conn, req, effect_id)
                        if effect["state"] != "completed":
                            store.update_effect(conn, req, effect_id, "unknown", {"reason_code": exc.code})
            except (PmtError, OSError):
                db.diagnostics.emit("continuity.task_resume_recovery_required",
                                    request_id=req.get("request_id"), scope_id=req.get("scope_id"),
                                    outcome="unknown")
        return response(req.get("request_id"), error=exc.as_dict()), exc.exit_code
    except (OSError, TypeError, ValueError, KeyError) as exc:
        error = PmtError("resume_source_unavailable", "Current task context could not be revalidated", 4, True)
        return response(req.get("request_id"), error=error.as_dict()), error.exit_code


def _compose_task_resume(db, req):
    """Build the existing F5 private projection after fresh owner/source checks."""
    from ..service import response
    from .current import _capture, validate_basis_components
    import uuid

    payload = req.get("payload", {})
    basis_ref = payload.get("basis_ref")
    graph_path = payload.get("relative_graph_path")
    workspace = payload.get("workspace")
    if not isinstance(graph_path, str) or not isinstance(workspace, str):
        raise PmtError("resume_source_required", "Current claimed workspace and graph path are required")
    store = ContinuityStore(db)
    effect_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "continuity:task_resume"))
    with db.write() as conn:
        authorize(db, conn, req)
        work_access(db, conn, req, paths=[graph_path], workspace=workspace)
        prior = store.get(conn, req, basis_ref, kind="basis")
    current_basis, evidence = _capture(db, req)
    comparison = validate_basis_components(prior["body"], current_basis)
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        authorize(db, conn, req)
        change_evidence = _resume_change_evidence(db, conn, req, payload, prior, current_basis)
    previous = db.get_request_result(req["request_id"], req.get("actor"), req.get("session_id"),
                                     expected_request=req)
    if previous:
        previous_result = previous[0].get("result") or {}
        previous_bundle_ref = previous_result.get("bundle_ref")
        if previous_bundle_ref:
            with closing(db.connect()) as conn:
                conn.execute("BEGIN")
                authorize(db, conn, req)
                previous_bundle = store.get(conn, req, previous_bundle_ref, kind="bundle")
                previous_basis = store.get(conn, req, previous_bundle["body"].get("basis_ref"), kind="basis")
            previous_comparison = validate_basis_components(previous_basis["body"], current_basis)
            if previous_comparison["status"] != "unchanged":
                raise PmtError("resume_basis_stale", "Current source changed since this request's task bundle", 3)
        return previous
    if comparison["status"] != "unchanged" or current_basis.get("complete") is not True:
        return response(req["request_id"], result={"complete": False,
            "basis_ref": basis_ref, "current_basis_hash": digest(current_basis),
            "basis_status": comparison["status"],
            "changed_components": comparison["changed_components"],
            "unknown_components": comparison["unknown_components"],
            "reason": "task context needs a fresh basis before private detail is read"}), 0
    effect_body = {"basis_ref": basis_ref, "run_id": payload.get("run_id"),
                   "repository_id": payload.get("repository_id"),
                   "graph_path_ref": digest(graph_path),
                   "inventory_hash": current_basis["source"]["inventory_hash"]}
    with db.write() as conn:
        authorize(db, conn, req)
        effect = conn.execute("SELECT id FROM continuity_journal WHERE id=?", (effect_id,)).fetchone()
        if effect:
            old_effect = store.get_effect(conn, req, effect_id)
            if old_effect["body"] != effect_body:
                raise PmtError("request_conflict", "Original task resume is bound to a different basis or source", 3)
            if old_effect["state"] == "completed":
                raise PmtError("effect_response_missing", "Completed task resume effect has no original request receipt", 5)
        else:
            store.begin_effect(conn, req, "task_resume", effect_body, basis_hash=prior["body_hash"])
        store.update_effect(conn, req, effect_id, "applying")
    expected = payload.get("expected_source") or evidence["source_pin"]
    task_ref = payload.get("task_ref")
    if not isinstance(task_ref, dict):
        task_ref = {"task_id": payload.get("task_id"), "step_id": payload.get("step_id"),
                    "run_id": payload.get("run_id")}
    if not all(isinstance(task_ref.get(key), str) for key in ("task_id", "step_id")):
        raise PmtError("resume_task_invalid", "Task and Step references are required")
    child_req = dict(req)
    child_req["request_id"] = str(uuid.uuid5(uuid.UUID(req["request_id"]), "continuity:task-context"))
    child_req["operation"] = "build_task_context"
    child_req["payload"] = {key: payload[key] for key in (
        "role", "repository_id", "workspace", "relative_graph_path", "node_ids", "relation_ids",
        "graph_cursor", "budget", "impact_request", "canonical_workspace", "project_id",
        "expected_run_revision") if key in payload}
    if isinstance(payload.get("f5_budget"), dict):
        # Keep the F5 role projection budget distinct from the full R4 wire
        # budget so a retained detail cursor remains discoverable in the envelope.
        child_req["payload"]["budget"] = dict(payload["f5_budget"])
    child_req["payload"].update({"run_id": payload["run_id"], "task_ref": task_ref,
                                 "expected_source": expected})
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        authorize(db, conn, req)
        run = work_access(db, conn, req, paths=[payload["relative_graph_path"]],
                          workspace=payload["workspace"])
    from ..efficiency import context as task_context
    envelope, exit_code = task_context.execute_file(db, child_req)
    if exit_code or not envelope.get("ok"):
        with db.write() as conn:
            authorize(db, conn, req)
            store.update_effect(conn, req, effect_id, "partial", {"reason_code": (envelope.get("error") or {}).get("code")})
        return response(req["request_id"], error=envelope.get("error"),
                        warnings=envelope.get("warnings")), exit_code
    context_ref = (envelope.get("result") or {}).get("context_ref")
    if not isinstance(context_ref, Mapping):
        raise PmtError("task_context_receipt_missing", "Existing task context did not return a private receipt", 5)
    body = {"version": 1, "basis_ref": basis_ref, "basis_hash": prior["body_hash"],
            "task_ref": task_ref, "run_ref": run["id"], "role": payload.get("role"),
            "repository_id": payload["repository_id"],
            "relative_graph_path": payload["relative_graph_path"],
            "context_ref": dict(context_ref), "source_hash": context_ref.get("source_hash"),
            "context_incomplete": (envelope.get("result") or {}).get("incomplete") is True,
            "context_unknown": (envelope.get("result") or {}).get("unknown", []),
            "change_evidence": change_evidence,
            "next_action": ({"action": "request_selection", "reason": "related_change_not_proven_currently_applied",
                "required_refs": [ref for ref in (change_evidence.get("change_ref"),
                    change_evidence.get("assessment_ref"), change_evidence.get("applicability_ref"),
                    change_evidence.get("alignment_ref")) if ref],
                "required_conditions": ["revalidate_current_owner_and_source",
                    "review_current_assessment_and_applicability", "verify_applied_alignment_pointer"],
                "unknown": list(change_evidence.get("reason_codes", [])), "executable": False}
                if change_evidence.get("status") == "change_requires_review" else
                {"action": "continue_from_current_checkpoint" if change_evidence.get("complete") else "request_selection",
                 "reason": change_evidence.get("status", "current_evidence_unknown"),
                 "required_refs": [ref for ref in (change_evidence.get("checkpoint_ref"),
                    change_evidence.get("basis_ref")) if ref],
                 "required_conditions": ["revalidate_current_owner_and_source"],
                 "unknown": list(change_evidence.get("reason_codes", [])), "executable": False}),
            "detail_cursor": (envelope.get("result") or {}).get("detail_cursor"),
            "detail_available": bool((envelope.get("result") or {}).get("detail_cursor")),
            "alias_reactivated": False}

    def authorize_task(conn, request):
        authorize(db, conn, request)
        work_access(db, conn, request, paths=[graph_path], workspace=workspace)

    def commit_bundle(conn, request):
        current_work = work_access(db, conn, request, paths=[graph_path], workspace=workspace)
        if current_work["revision"] != run["revision"]:
            raise PmtError("resume_owner_changed", "Task run changed before the private bundle was stored", 3)
        result = store.put(conn, request, "bundle", body, visibility="private",
                           basis_hash=prior["body_hash"])
        public = {"bundle_ref": result["id"], "basis_ref": basis_ref,
                  "context_ref": dict(context_ref), "complete": not body["context_incomplete"],
                  "source_current": True, "owner_current": True,
                  "private_directive_included": False, "detail_available": body["detail_available"],
                  "change_evidence": body["change_evidence"],
                  "next_action": body["next_action"],
                  "unknown": body["context_unknown"] + body["change_evidence"].get("reason_codes", [])}
        public["complete"] = public["complete"] and body["change_evidence"].get("complete") is True
        budget_request = dict(request)
        budget_request["payload"] = dict(request.get("payload", {}))
        configured = budget_request["payload"].get("budget")
        if isinstance(configured, dict) and "unit" in configured:
            budget_request["payload"]["budget"] = {key: configured[key]
                                                     for key in ("max_bytes", "max_lines") if key in configured}
        bounded = bounded_result(budget_request, public | {"detail_ref": result["id"]})
        store.update_effect(conn, request, effect_id, "completed", {"bundle_ref": result["id"],
                            "basis_ref": basis_ref, "complete": bounded.get("complete") is True})
        return bounded

    result, code = db.run_request(req, commit_bundle, authorize=authorize_task)
    if code != 0 or not result.get("ok"):
        try:
            with db.write() as conn:
                authorize(db, conn, req)
                prior_effect = store.get_effect(conn, req, effect_id)
                if prior_effect["state"] != "completed":
                    store.update_effect(conn, req, effect_id, "unknown",
                                        {"reason_code": (result.get("error") or {}).get("code", "request_failed")})
        except (PmtError, OSError):
            db.diagnostics.emit("continuity.task_resume_recovery_required",
                                request_id=req.get("request_id"), scope_id=req.get("scope_id"),
                                outcome="unknown")
    return result, code


def _read_resume_detail(db, req):
    from ..service import response
    from ..efficiency import context as task_context
    from .current import _capture, validate_basis_components

    payload, store = req.get("payload", {}), ContinuityStore(db)
    bundle_ref = payload.get("bundle_ref")
    relative_graph_path = payload.get("relative_graph_path")
    workspace = payload.get("workspace")
    if not isinstance(relative_graph_path, str) or not isinstance(workspace, str):
        raise PmtError("resume_source_required", "Current claimed workspace and graph path are required")
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        authorize(db, conn, req)
        work_access(db, conn, req, paths=[relative_graph_path], workspace=workspace)
        bundle = store.get(conn, req, bundle_ref, kind="bundle")
        body = bundle["body"]
        if not isinstance(body.get("context_ref"), Mapping):
            raise PmtError("resume_bundle_corrupt", "Private resume bundle lacks its current task context ref", 5)
        basis_obj = store.get(conn, req, body.get("basis_ref"), kind="basis")
        inventory = store.get(conn, req, basis_obj["body"].get("source", {}).get("inventory_ref"), kind="detail")
    selected_paths = [item["relative_path"] for item in inventory["body"].get("items", [])
                      if isinstance(item, Mapping) and isinstance(item.get("relative_path"), str)]
    capture_req = dict(req)
    capture_req["payload"] = {**payload, "repository_id": body["repository_id"],
                              "relative_graph_path": body["relative_graph_path"],
                              "inventory_paths": selected_paths}
    current_basis, _ = _capture(db, capture_req)
    comparison = validate_basis_components(basis_obj["body"], current_basis)
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        authorize(db, conn, req)
        current_evidence = _resume_change_evidence(db, conn, req,
            {**payload, "change_ref": body.get("change_evidence", {}).get("change_ref"),
             "assessment_ref": body.get("change_evidence", {}).get("assessment_ref"),
             "applicability_ref": body.get("change_evidence", {}).get("applicability_ref")},
            basis_obj, current_basis)
    saved_evidence = body.get("change_evidence", {})
    if (current_evidence.get("change_ref") != saved_evidence.get("change_ref")
            or current_evidence.get("assessment_ref") != saved_evidence.get("assessment_ref")
            or current_evidence.get("applicability_ref") != saved_evidence.get("applicability_ref")
            or current_evidence.get("alignment_ref") != saved_evidence.get("alignment_ref")
            or current_evidence.get("status") != saved_evidence.get("status")):
        result = {"bundle_ref": bundle_ref, "basis_ref": basis_obj["id"],
                  "detail": None, "complete": False, "change_evidence": current_evidence,
                  "reason": "current change and alignment evidence must be reviewed before private detail is read"}
        return response(req["request_id"], result=bounded_result(req, result)), 0
    if comparison["status"] != "unchanged" or current_basis.get("complete") is not True:
        result = {"bundle_ref": bundle_ref, "basis_ref": basis_obj["id"],
                  "detail": None, "complete": False,
                  "basis_status": comparison["status"],
                  "changed_components": comparison["changed_components"],
                  "unknown_components": comparison["unknown_components"],
                  "reason": "current basis must be reviewed before private detail is read"}
        return response(req["request_id"], result=bounded_result(req, result)), 0
    context_id = body["context_ref"].get("id")
    request = dict(req)
    request["operation"] = "read_context_detail"
    request["payload"] = {"context_id": context_id,
                           "cursor": payload.get("cursor") or body.get("detail_cursor"),
                           "max_bytes": payload.get("max_bytes"), "max_lines": payload.get("max_lines")}
    with closing(db.connect()) as conn:
        conn.execute("BEGIN")
        authorize(db, conn, req)
        detail = task_context.handle(db, conn, request)
    result = {"bundle_ref": bundle_ref, "basis_ref": body.get("basis_ref"),
              "detail": detail, "source_revalidated": True, "owner_revalidated": True,
              "detail_ref": bundle_ref,
              "change_evidence": current_evidence,
              "next_action": body.get("next_action"),
              "complete": detail.get("incomplete") is not True
                          and current_evidence.get("complete") is True}
    return response(req["request_id"], result=bounded_result(req, result)), 0
