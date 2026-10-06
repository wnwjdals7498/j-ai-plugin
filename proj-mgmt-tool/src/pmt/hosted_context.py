"""Hosted R4 client adapter over client source capture and Host F5 storage.

This module keeps physical checkout reads on the client. Private task content
is built, retained and revalidated only by the existing Host F5 context service.
It never opens a local PMT database or copies directive bodies into continuity
metadata.
"""
from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from pathlib import Path

from .continuity.context import propose_next_action
from .continuity.contracts import bounded_result, digest
from .continuity.current import validate_basis_components
from .errors import PmtError
from .hosted_continuity import HostedContinuityClient
from .service import response
from .storage_config import adapt_host_request, mapping_for_request
from .util import canonical_json, new_id


_OPERATIONS = {"validate_basis", "compose_task_resume", "read_resume_detail"}
_MAX_ID = 512


def _child_request(parent: Mapping, operation: str, payload: dict, label: str) -> dict:
    request_id = parent.get("request_id")
    try:
        child_id = str(uuid.uuid5(uuid.UUID(request_id), "hosted-context:" + label))
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("invalid_request_id", "Hosted context requires a canonical request ID", 2) from exc
    child = {"protocol_version": 1, "request_id": child_id, "operation": operation,
            "actor": parent["actor"], "session_id": parent["session_id"],
            "scope_id": parent["scope_id"],
            "source": {"product": "pmt-client"}, "context_refs": [], "payload": payload}
    if isinstance(parent.get("record_id"), str):
        child["record_id"] = parent["record_id"]
    return child


def _remote(state_port, request):
    try:
        envelope, code = state_port.execute(request)
    except PmtError:
        raise
    except (OSError, TimeoutError) as exc:
        raise PmtError("remote_unavailable", "Current Host state could not be read", 4, True,
                       {"effect": "unknown"}) from exc
    if code or not isinstance(envelope, dict) or envelope.get("ok") is not True:
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(error, dict):
            raise PmtError(error.get("code", "host_request_failed"),
                error.get("message", "Host request failed"), code or 3,
                error.get("retryable") is True, error.get("details"))
        raise PmtError("host_response_invalid", "Host returned an invalid context response", 3)
    return envelope.get("result")


def _strict_ref(value, field):
    if not isinstance(value, str) or not value.strip() or len(value) > _MAX_ID:
        raise PmtError("hosted_context_input_invalid", f"{field} must be a bounded reference", 2)
    return value


def _workspace_binding(profile, request):
    payload = request.get("payload", {})
    required = ("repository_id", "workspace", "relative_graph_path", "run_id")
    if not isinstance(payload, dict) or any(not isinstance(payload.get(key), str) for key in required):
        raise PmtError("hosted_context_source_required",
                       "A mapped checkout, graph path and current run are required", 2)
    if not Path(payload["workspace"]).is_absolute():
        raise PmtError("hosted_context_source_required", "Client workspace must be an absolute mapped path", 2)
    mapping = mapping_for_request(profile, request)
    if mapping is None:
        raise PmtError("storage_mapping_missing", "Hosted context requires an explicit workspace mapping", 3)
    adapted = adapt_host_request(profile, request)
    if (mapping["project_id"] != request.get("scope_id")
            or mapping["repository_id"] != payload["repository_id"]
            or Path(mapping["local_root"]).expanduser().resolve() != Path(payload["workspace"]).expanduser().resolve()
            or mapping["relative_graph_path"] != payload["relative_graph_path"]):
        raise PmtError("storage_mapping_conflict", "Hosted context source differs from the registered local mapping", 3)
    return mapping, adapted


def _capture_basis(profile, state_port, data_root, environ, request, *, label):
    mapping, _adapted = _workspace_binding(profile, request)
    child = dict(request)
    child["request_id"] = str(uuid.uuid5(uuid.UUID(request["request_id"]), "hosted-context:" + label))
    child["operation"] = "capture_work_basis"
    child["source"] = {"product": "pmt-client"}
    child["payload"] = dict(request.get("payload", {}))
    task_ref = child["payload"].get("task_ref")
    child["payload"].setdefault("task_id",
        task_ref.get("task_id") if isinstance(task_ref, dict) else request.get("record_id"))
    # The capture adapter validates the profile mapping, reads only selected
    # client files, publishes the source snapshot and asks Host to construct
    # work facts from its own current SQL transaction.
    adapter = HostedContinuityClient(profile, state_port, data_root, environ)
    envelope, code = adapter.capture_work_basis(child)
    if code or not isinstance(envelope, dict) or envelope.get("ok") is not True:
        error = envelope.get("error") if isinstance(envelope, dict) else None
        if isinstance(error, dict):
            raise PmtError(error.get("code", "basis_capture_failed"),
                error.get("message", "Current source basis could not be captured"), code or 3,
                error.get("retryable") is True, error.get("details"))
        raise PmtError("host_response_invalid", "Basis capture returned an invalid response", 3)
    result = envelope.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("basis"), dict):
        raise PmtError("host_response_invalid", "Host basis receipt is incomplete", 3)
    return mapping, result


def _read_basis(state_port, request, basis_ref, label):
    basis_ref = _strict_ref(basis_ref, "basis_ref")
    child = _child_request(request, "get_continuity_object", {
        "object_id": basis_ref, "kind": "basis"}, label)
    result = _remote(state_port, child)
    if not isinstance(result, dict) or not isinstance(result.get("body"), dict):
        raise PmtError("continuity_corrupt", "Retained Host basis is invalid", 5)
    return result


def _compare_basis(state_port, request, current, basis_ref, label):
    old = _read_basis(state_port, request, basis_ref, label)
    comparison = validate_basis_components(old["body"], current["basis"])
    if current.get("complete") is not True and comparison["status"] == "unchanged":
        comparison = {**comparison, "status": "unknown",
                      "unknown_components": list(dict.fromkeys(
                          comparison["unknown_components"] + ["current_capture_incomplete"]))}
    return old, comparison


def _authorize_workspace(state_port, request, mapping, run_revision, mode, label):
    payload = request["payload"]
    branch_key = mapping.get("branch")
    child = _child_request(request, "authorize_workspace", {
        "project_id": mapping["project_id"], "repository_id": mapping["repository_id"],
        "canonical_workspace": mapping["canonical_workspace"],
        "relative_graph_path": mapping["relative_graph_path"], "branch_key": branch_key,
        "run_id": payload["run_id"], "expected_run_revision": run_revision, "mode": mode}, label)
    authority = _remote(state_port, child)
    if (not isinstance(authority, dict) or authority.get("status") != "authorized"
            or authority.get("run_id") != payload["run_id"]
            or authority.get("run_revision") != run_revision
            or authority.get("owner", {}).get("session_id") != request.get("session_id")):
        raise PmtError("workspace_authority_stale", "Host did not confirm the current owner and run", 3)
    return authority


def _run_revision(capture, request):
    payload = request.get("payload", {})
    actual = capture.get("run_revision")
    requested = payload.get("expected_run_revision", actual)
    if type(actual) is not int or type(requested) is not int or requested != actual:
        raise PmtError("execution_revision_conflict", "Host run revision changed during hosted context capture", 3)
    return actual


def _read_object(state_port, request, object_ref, kind, label):
    if not isinstance(object_ref, str):
        return None
    value = _remote(state_port, _child_request(request, "get_continuity_object",
        {"object_id": object_ref, "kind": kind}, label))
    return value if isinstance(value, dict) and isinstance(value.get("body"), dict) else None


def _basis_record_versions(body):
    work = body.get("work", {}) if isinstance(body, Mapping) else {}
    records = work.get("records", []) if isinstance(work, Mapping) else []
    return {item.get("id"): item.get("revision") for item in records
            if isinstance(item, Mapping) and isinstance(item.get("id"), str)}


def _host_change_evidence(profile, data_root, state_port, request, mapping, current, prior_basis, label):
    """Resolve R2/R3/current-alignment refs from Host objects and pointers.

    Refs are selectors only. Host object hashes, basis bindings, current graph
    hash and the applied-alignment CAS pointer establish the reported state.
    """
    payload = request.get("payload", {})
    source = current.get("basis", {}).get("source", {})
    task_id = (payload.get("task_ref", {}).get("task_id") if isinstance(payload.get("task_ref"), dict)
               else payload.get("task_id"))
    task_id = task_id or current.get("basis", {}).get("work", {}).get("task_id")
    base = {"repository_id": mapping["repository_id"], "branch": mapping["branch"],
            "workspace_ref": mapping["canonical_workspace"], "task_id": task_id,
            "environment_id": getattr(state_port, "environment_id", None)}

    def pointer(purpose, *, environment=True):
        selector_base = base if environment else {key: value for key, value in base.items()
                                                  if key != "environment_id"}
        selector = selector_base | {"purpose": purpose}
        return _remote(state_port, _child_request(request, "read_continuity_pointer",
            {"selector": selector}, label + ":pointer:" + purpose))

    checkpoint_pointer = pointer(payload.get("checkpoint_purpose", "current"), environment=False)
    checkpoint = _read_object(state_port, request, checkpoint_pointer.get("object_id"),
                              "checkpoint", label + ":checkpoint") if checkpoint_pointer.get("object_id") else None
    checkpoint_basis_ref = checkpoint.get("body", {}).get("basis_ref") if checkpoint else None
    checkpoint_basis = _read_object(state_port, request, checkpoint_basis_ref, "basis",
                                    label + ":checkpoint-basis") if checkpoint_basis_ref else None
    current_basis = current.get("basis", {})
    current_basis_ref = current.get("basis_ref")
    checkpoint_comparison = (validate_basis_components(checkpoint_basis["body"], current_basis)
                             if checkpoint_basis else {"status": "unknown", "changed_components": [],
                                                       "unknown_components": ["checkpoint_basis"]})
    if checkpoint_comparison.get("status") == "unchanged":
        return {"status": "no_change_confirmed", "checkpoint_ref": checkpoint.get("id"),
            "checkpoint_basis_ref": checkpoint_basis_ref, "current_basis_ref": current_basis_ref,
            "change_ref": None, "assessment_ref": None, "resolution_ref": None,
            "applicability_ref": None, "alignment_ref": None, "changed_path_refs": [],
            "affected_node_refs": [], "required_action": None, "reason_codes": [],
            "applied": False, "complete": True}
    change_pointer = pointer("change")
    change_ref = payload.get("change_ref") or change_pointer.get("object_id")
    change = _read_object(state_port, request, change_ref, "change", label + ":change")
    if change is None:
        same = checkpoint_comparison["status"] == "unchanged"
        return {"status": "no_change_confirmed" if same else "unknown",
            "checkpoint_ref": checkpoint.get("id") if checkpoint else None,
            "checkpoint_basis_ref": checkpoint_basis_ref, "current_basis_ref": current_basis_ref,
            "change_ref": None, "assessment_ref": None, "applicability_ref": None,
            "alignment_ref": None, "changed_path_refs": [], "affected_node_refs": [],
            "required_action": None if same else "collect_current_change_evidence",
            "reason_codes": [] if same else ["change_evidence_unavailable"],
            "applied": False, "complete": same}

    change_body = change["body"]
    after_ref = change_body.get("after_basis_ref") or change_body.get("current_basis_ref")
    after = _read_object(state_port, request, after_ref, "basis", label + ":after-basis")
    after_inventory = None
    if after:
        try:
            from .hosted_continuity import read_private_inventory_detail
            after_inventory = read_private_inventory_detail(data_root, profile, request.get("session_id"),
                after.get("body", {}).get("source", {}).get("inventory_ref"))
        except PmtError:
            after_inventory = None
    reasons = []
    if checkpoint_basis_ref and change_body.get("before_basis_ref") != checkpoint_basis_ref:
        reasons.append("change_does_not_continue_current_checkpoint")
    if not after:
        reasons.append("change_after_basis_not_current")
    elif change_body.get("after_basis_hash") not in {None, after.get("body_hash")}:
        reasons.append("change_after_basis_hash_mismatch")
    if validate_basis_components(after.get("body", {}), current_basis)["status"] != "unchanged" if after else True:
        reasons.append("change_after_basis_semantically_stale")
    if change_body.get("coverage") != "complete" or change_body.get("state") not in {"complete", "no_change"}:
        reasons.append("change_capture_incomplete")

    assessment = _read_object(state_port, request, payload.get("assessment_ref"), "assessment",
                              label + ":assessment")
    assessment_reasons = []
    if assessment is None:
        assessment_reasons.append("current_change_assessment_unavailable")
    else:
        assessed = assessment["body"]
        if assessed.get("change_ref") != change["id"] or assessed.get("change_hash") != change.get("body_hash"):
            assessment_reasons.append("assessment_change_mismatch")
        if assessed.get("basis_ref") != (after.get("id") if after else None):
            assessment_reasons.append("assessment_basis_mismatch")
        if assessed.get("state") != "assessed" or assessed.get("unknown"):
            assessment_reasons.append("assessment_incomplete_or_unknown")
        if assessed.get("observed_graph_hash") != current_basis.get("contract", {}).get("graph_hash"):
            assessment_reasons.append("assessment_graph_stale")
        if not after or _basis_record_versions(after.get("body", {})) != _basis_record_versions(current_basis):
            assessment_reasons.append("work_or_directive_version_changed_since_assessment_basis")

    applicability = _read_object(state_port, request, payload.get("applicability_ref"), "applicability",
                                 label + ":applicability")
    applicability_reasons = []
    if applicability is None:
        applicability_reasons.append("current_applicability_unavailable")
    else:
        app = applicability["body"]
        applicability_basis = _read_object(state_port, request, app.get("basis_ref"), "basis",
                                           label + ":applicability-basis")
        if (not applicability_basis or app.get("basis_hash") != applicability_basis.get("body_hash") or
                validate_basis_components(applicability_basis.get("body", {}), current_basis)["status"] != "unchanged"):
            applicability_reasons.append("applicability_basis_stale")
        if app.get("graph_hash") != current_basis.get("contract", {}).get("graph_hash"):
            applicability_reasons.append("applicability_graph_stale")
        if app.get("status") == "unknown":
            applicability_reasons.append("applicability_unknown")

    resolution = _read_object(state_port, request, payload.get("resolution_ref"), "resolution",
                              label + ":resolution")
    if resolution and assessment and (resolution["body"].get("assessment_ref") != assessment["id"]
            or resolution["body"].get("assessment_hash") != assessment.get("body_hash")):
        assessment_reasons.append("resolution_assessment_mismatch")

    alignment_pointer = pointer("applied_alignment")
    alignment = (_read_object(state_port, request, alignment_pointer.get("object_id"), "alignment",
                              label + ":alignment") if alignment_pointer.get("object_id") else None)
    applied = False
    document_hash_current = False
    document_path = None
    changed_only_effect_targets = False
    effect_readback_current = False
    effect_readback_reason = None
    if alignment:
        receipt = alignment["body"]
        receipt_selector = receipt.get("pointer_selector", {})
        expected_selector = base | {"purpose": "applied_alignment"}
        graph_effect = receipt.get("graph_effect_ref")
        document_effect = receipt.get("document_effect_ref")
        receipt_hash = receipt.get("receipt_hash")
        receipt_hash_valid = (isinstance(receipt_hash, str) and digest(
            {key: value for key, value in receipt.items() if key != "receipt_hash"}) == receipt_hash)
        current_pin = current.get("source_pin", {})
        try:
            from .hosted_continuity import read_private_inventory_detail
            inventory = read_private_inventory_detail(data_root, profile,
                request.get("session_id"), current_basis.get("source", {}).get("inventory_ref"))
            matching_document_hashes = [item for item in inventory.get("items", [])
                if isinstance(item, Mapping) and item.get("status") == "verified"
                and item.get("content_hash") == receipt.get("document_hash")]
            document_hash_current = len(matching_document_hashes) == 1
            document_path = matching_document_hashes[0].get("relative_path") if document_hash_current else None
            before_items = {item.get("relative_path"): item for item in
                (after_inventory.get("items", []) if after_inventory else []) if isinstance(item, Mapping)}
            current_items = {item.get("relative_path"): item for item in inventory.get("items", [])
                             if isinstance(item, Mapping)}
            changed_paths = [path for path in set(before_items) | set(current_items)
                if before_items.get(path, {}).get("content_hash") != current_items.get(path, {}).get("content_hash")]
            graph_path = mapping.get("relative_graph_path")
            allowed_paths = {graph_path, document_path}
            changed_only_effect_targets = bool(after_inventory and set(before_items) == set(current_items)
                and changed_paths and all(path in allowed_paths for path in changed_paths))
            if (document_hash_current and isinstance(current.get("run_revision"), int)
                    and isinstance(graph_effect, Mapping) and isinstance(document_effect, Mapping)):
                from .hosted_changes import HostedChangesClient
                from .efficiency.source import pin_source
                client = HostedChangesClient(profile, state_port, data_root)
                context = {"run": {"id": payload.get("run_id"), "revision": current["run_revision"]},
                    "mapping": mapping, "canonical_workspace": mapping["canonical_workspace"],
                    "graph_rel": mapping["relative_graph_path"],
                    "branch_key": mapping.get("branch") or "non-git",
                    "pin": pin_source(current_pin)}
                graph_readback = client._read_file_effect(request, context, graph_effect,
                    mapping["relative_graph_path"], "graph_change")
                document_readback = client._read_file_effect(request, context, document_effect,
                    document_path, "document_render")
                graph_item = current_items.get(mapping["relative_graph_path"], {})
                doc_item = current_items.get(document_path, {})
                effect_readback_current = bool(
                    graph_readback.get("after_source_pin", {}).get("source_hash") == current_pin.get("source_hash")
                    and graph_readback.get("candidate_sha256") == graph_item.get("content_hash")
                    and document_readback.get("after_source_pin", {}).get("source_hash") == current_pin.get("source_hash")
                    and document_readback.get("publication", {}).get("candidate_hash") == receipt.get("document_hash")
                    and doc_item.get("content_hash") == receipt.get("document_hash"))
        except PmtError as exc:
            document_hash_current = False
            effect_readback_reason = exc.code
        if not effect_readback_current:
            effect_readback_reason = effect_readback_reason or "alignment_effect_hash_or_inventory_mismatch"
        applied = bool(receipt.get("state") == "applied" and receipt_hash_valid
            and assessment and receipt.get("assessment_ref") == assessment["id"]
            and receipt.get("assessment_hash") == assessment.get("body_hash")
            and resolution and receipt.get("resolution_ref") == resolution["id"]
            and receipt.get("basis_ref") == assessment.get("body", {}).get("basis_ref")
            and receipt.get("basis_hash") == assessment.get("body", {}).get("basis_hash")
            and receipt.get("graph_hash") == current_basis.get("contract", {}).get("graph_hash")
            and receipt.get("source_pin", {}).get("source_hash") == current_pin.get("source_hash")
            and receipt.get("before_graph_hash") == assessment.get("body", {}).get("observed_graph_hash")
            and isinstance(graph_effect, Mapping) and graph_effect.get("kind") == "host_local_file_effect"
            and isinstance(document_effect, Mapping) and document_effect.get("kind") == "host_local_file_effect"
            and isinstance(graph_effect.get("id"), str) and type(graph_effect.get("revision")) is int
            and graph_effect.get("scope_id") == request.get("scope_id")
            and isinstance(document_effect.get("id"), str) and type(document_effect.get("revision")) is int
            and document_effect.get("scope_id") == request.get("scope_id")
            and document_hash_current and changed_only_effect_targets and effect_readback_current
            and receipt.get("host_git_verified") is False and receipt.get("host_document_verified") is False
            and receipt.get("provenance") == "client_effect_readback"
            and alignment_pointer.get("object_id") == alignment["id"]
            and isinstance(receipt_selector, Mapping) and receipt_selector == expected_selector)
    reasons.extend(assessment_reasons)
    reasons.extend(applicability_reasons)
    if not applied:
        reasons.append("alignment_not_currently_applied")
    reasons = list(dict.fromkeys(reasons))
    if applied:
        # F1/F3 are allowed to advance graph/document bytes only when the
        # current client-bound inventory shows exactly those receipt targets.
        reasons = [reason for reason in reasons
            if reason not in {"change_after_basis_semantically_stale", "assessment_graph_stale"}]
    complete = not reasons and applied
    return {"status": "applied_current" if complete else "change_requires_review",
        "change_ref": change["id"], "change_hash": change.get("body_hash"),
        "assessment_ref": assessment.get("id") if assessment else None,
        "assessment_hash": assessment.get("body_hash") if assessment else None,
        "assessment_state": assessment.get("body", {}).get("state", "unknown") if assessment else "unknown",
        "resolution_ref": resolution.get("id") if resolution else None,
        "resolution_state": resolution.get("body", {}).get("state", "unknown") if resolution else "unknown",
        "applicability_ref": applicability.get("id") if applicability else None,
        "applicability_status": applicability.get("body", {}).get("status", "unknown") if applicability else "unknown",
        "alignment_ref": alignment.get("id") if alignment else None,
        "alignment_pointer_revision": alignment_pointer.get("revision", 0),
        "alignment_effect_readback_current": effect_readback_current,
        "alignment_inventory_delta_current": changed_only_effect_targets,
        "alignment_document_hash_current": document_hash_current,
        "alignment_effect_reason": effect_readback_reason,
        "checkpoint_ref": checkpoint.get("id") if checkpoint else None,
        "checkpoint_basis_ref": checkpoint_basis_ref, "current_basis_ref": current_basis_ref,
        "changed_path_refs": [item.get("path_ref") for item in change_body.get("facts", [])
                              if isinstance(item, Mapping) and item.get("path_ref")],
        "affected_node_refs": assessment.get("body", {}).get("affected_node_refs", []) if assessment else [],
        "required_action": None if complete else "review_change_and_applicability",
        "reason_codes": reasons, "applied": applied, "complete": complete}


def _context_payload(request, mapping, source_pin, run_revision):
    original = request.get("payload", {})
    task_ref = original.get("task_ref")
    if not isinstance(task_ref, dict) or not all(isinstance(task_ref.get(key), str)
            for key in ("task_id", "step_id")):
        raise PmtError("hosted_context_task_required", "Current Task and Step references are required", 2)
    f5_budget = original.get("f5_budget", original.get("budget", {
        "max_bytes": 16384, "max_lines": 160, "unit": "utf8"}))
    if not isinstance(f5_budget, dict):
        raise PmtError("context_budget_invalid", "F5 context budget must be an object", 2)
    f5_budget = dict(f5_budget)
    f5_budget.setdefault("unit", "utf8")
    payload = {key: original[key] for key in (
        "role", "node_ids", "relation_ids", "graph_cursor", "impact_request")
        if key in original}
    payload["budget"] = f5_budget
    payload.update({"project_id": mapping["project_id"], "repository_id": mapping["repository_id"],
        "canonical_workspace": mapping["canonical_workspace"], "workspace": mapping["canonical_workspace"],
        "relative_graph_path": mapping["relative_graph_path"],
        "run_id": original["run_id"], "expected_run_revision": run_revision,
        "task_ref": {key: task_ref[key] for key in ("task_id", "step_id", "run_id") if key in task_ref},
        "expected_source": source_pin})
    payload.setdefault("role", "main")
    if task_ref.get("run_id") not in {None, original["run_id"]}:
        raise PmtError("context_authority_mismatch", "Task ref run differs from the current Host run", 3)
    return payload


def _incomplete(request, *, status, reason, basis_ref=None, changed=(), unknown=(), refs=None):
    result = {"complete": False, "basis_status": status, "reason": reason,
              "changed_components": list(changed), "unknown_components": list(unknown),
              "source_provenance": "client_attested", "host_git_verified": False}
    if basis_ref:
        result["basis_ref"] = basis_ref
    if isinstance(refs, Mapping):
        result.update(refs)
    return response(request.get("request_id"), result=bounded_result(request, result))


def _change_next_action(evidence):
    changed = evidence.get("status") == "change_requires_review"
    refs = [ref for ref in (evidence.get("change_ref"), evidence.get("assessment_ref"),
        evidence.get("applicability_ref"), evidence.get("alignment_ref"),
        evidence.get("checkpoint_ref"), evidence.get("current_basis_ref")) if ref]
    return {"action": "request_selection" if changed or not evidence.get("complete") else "continue_from_current_checkpoint",
        "reason": "related_change_not_proven_currently_applied" if changed else evidence.get("status", "current_evidence_unknown"),
        "required_refs": refs,
        "required_conditions": (["revalidate_current_owner_and_source", "review_current_assessment_and_applicability",
                                  "verify_applied_alignment_pointer"] if changed else
                                 ["revalidate_current_owner_and_source"]),
        "unknown": list(evidence.get("reason_codes", [])), "executable": False}


class HostedContextClient:
    """Client orchestration over current Host authority and owner-bound F5 storage.

    `state_port` is the authenticated HttpStore. No local DB is constructed; only
    C's local source capture adapter reads the mapped checkout. Host F5 owns every
    retained private directive projection and every detail page.
    """

    def __init__(self, profile, state_port, data_root, environ=None):
        self.profile = profile
        self.state_port = state_port
        self.data_root = Path(data_root).expanduser().absolute()
        self.environ = environ

    def execute(self, request):
        operation = request.get("operation") if isinstance(request, dict) else None
        if operation not in _OPERATIONS:
            raise PmtError("hosted_context_operation_unsupported",
                           "Hosted context adapter does not support this operation", 2)
        try:
            if operation == "validate_basis":
                result = self.validate_basis(request)
            elif operation == "compose_task_resume":
                result = self.compose_task_resume(request)
            else:
                result = self.read_resume_detail(request)
            return result, 0
        except PmtError as exc:
            # Mid-capture client source changes are a bounded incomplete result,
            # never an invitation to reuse a stale F5 context.
            if exc.code in {"basis_source_changed", "source_conflict", "source_snapshot_stale"}:
                return _incomplete(request, status="changed", reason=exc.code), 0
            return response(request.get("request_id"), error=exc.as_dict()), exc.exit_code

    def _capture_current(self, request, label):
        return _capture_basis(self.profile, self.state_port, self.data_root,
                              self.environ, request, label=label)

    def validate_basis(self, request):
        payload = request.get("payload", {})
        basis_ref = _strict_ref(payload.get("basis_ref"), "basis_ref")
        mapping, current = self._capture_current(request, "validate-basis")
        _authorize_workspace(self.state_port, request, mapping, _run_revision(current, request),
                             "source_capture", "validate-basis-authority")
        old, comparison = _compare_basis(self.state_port, request, current, basis_ref, "basis-read")
        result = {"basis_ref": basis_ref, "current_basis_ref": current["basis_ref"],
            "current_basis_hash": current["basis_hash"], **comparison,
            "capture_complete": current.get("complete") is True,
            "source_provenance": "client_attested", "host_git_verified": False,
            "reasons": current["basis"].get("manifest", {}).get("reasons", [])}
        return response(request.get("request_id"), result=bounded_result(request, result))

    def compose_task_resume(self, request):
        payload = request.get("payload", {})
        prior_basis_ref = _strict_ref(payload.get("basis_ref"), "basis_ref")
        mapping, current = self._capture_current(request, "compose-task-basis-before")
        run_revision = _run_revision(current, request)
        old_basis, comparison = _compare_basis(
            self.state_port, request, current, prior_basis_ref, "compose-task-basis-read")
        change_evidence = _host_change_evidence(self.profile, self.data_root,
                                                self.state_port, request, mapping, current,
                                                old_basis, "compose-task-change-evidence")
        changed_resume_is_bound = (comparison["status"] == "changed"
            and change_evidence.get("change_ref")
            and change_evidence.get("current_basis_ref") == current.get("basis_ref")
            and "change_after_basis_not_current" not in change_evidence.get("reason_codes", [])
            and "change_does_not_continue_current_checkpoint" not in change_evidence.get("reason_codes", [])
            and "change_capture_incomplete" not in change_evidence.get("reason_codes", []))
        if ((comparison["status"] != "unchanged" and not changed_resume_is_bound)
                or current.get("complete") is not True):
            return _incomplete(request, status=comparison["status"],
                reason="current Host source and basis must match before F5 context build",
                basis_ref=prior_basis_ref, changed=comparison["changed_components"],
                unknown=comparison["unknown_components"], refs={
                    "current_basis_ref": current["basis_ref"], "source_provenance": "client_attested",
                    "host_git_verified": False, "change_evidence": change_evidence})

        _authorize_workspace(self.state_port, request, mapping, run_revision,
                             "execute", "compose-task-authority")
        child = _child_request(request, "build_task_context",
                               _context_payload(request, mapping, current["source_pin"], run_revision),
                               "f5-build")
        built = _remote(self.state_port, child)
        context_ref = built.get("context_ref") if isinstance(built, dict) else None
        if not isinstance(context_ref, dict) or context_ref.get("kind") != "task_context":
            raise PmtError("task_context_receipt_missing", "Host F5 did not retain an owner-bound context", 5)

        # F5 readback verifies the retained owner/source binding after its file
        # effect. This response supplies only the configured role projection.
        readback_request = _child_request(request, "read_task_context", {
            "project_id": mapping["project_id"], "repository_id": mapping["repository_id"],
            "canonical_workspace": mapping["canonical_workspace"],
            "relative_graph_path": mapping["relative_graph_path"],
            "run_id": payload["run_id"], "expected_run_revision": run_revision,
            "context_ref": context_ref}, "f5-readback")
        projection = _remote(self.state_port, readback_request)

        # Re-read Git and selected files after the Host build; a source change
        # during the remote call makes the new projection unusable for resume.
        _mapping_after, after = self._capture_current(request, "compose-task-basis-after")
        after_compare = validate_basis_components(current["basis"], after["basis"])
        if after_compare["status"] != "unchanged" or after.get("complete") is not True:
            return _incomplete(request, status=after_compare["status"],
                reason="client source changed while Host F5 context was being built",
                basis_ref=prior_basis_ref, changed=after_compare["changed_components"],
                unknown=after_compare["unknown_components"], refs={
                    "current_basis_ref": after["basis_ref"], "source_provenance": "client_attested",
                    "host_git_verified": False})

        result = {"bundle_ref": context_ref["id"], "context_ref": context_ref,
            "basis_ref": current["basis_ref"], "prior_basis_ref": old_basis["id"],
            "change_evidence": change_evidence,
            "next_action": _change_next_action(change_evidence),
            "source_hash": context_ref.get("source_hash"),
            "source_provenance": "client_attested", "host_git_verified": False,
            "owner_current": True,
            "complete": (built.get("incomplete") is not True and current.get("complete") is True
                         and projection.get("incomplete") is not True
                         and change_evidence.get("complete") is True),
            "private_directive_included": bool(projection.get("projection", {}).get("included")),
            "projection": projection.get("projection"),
            "mandatory_omissions": projection.get("mandatory_omissions", []),
            "unknown": projection.get("unknown", []),
            "detail_cursor": built.get("detail_cursor"), "detail_available": bool(built.get("detail_cursor"))}
        return response(request.get("request_id"), result=bounded_result(request, result))

    def read_resume_detail(self, request):
        payload = request.get("payload", {})
        basis_ref = _strict_ref(payload.get("basis_ref"), "basis_ref")
        context_ref = payload.get("context_ref")
        if (not isinstance(context_ref, dict) or context_ref.get("kind") != "task_context"
                or not isinstance(context_ref.get("id"), str)):
            raise PmtError("context_ref_invalid", "Current owner-bound F5 context ref is required", 2)
        mapping, current = self._capture_current(request, "resume-detail-basis-before")
        run_revision = _run_revision(current, request)
        old_basis, comparison = _compare_basis(
            self.state_port, request, current, basis_ref, "resume-detail-basis-read")
        change_evidence = _host_change_evidence(self.profile, self.data_root,
                                                self.state_port, request, mapping, current,
                                                old_basis, "resume-detail-change-evidence")
        if comparison["status"] != "unchanged" or current.get("complete") is not True:
            return _incomplete(request, status=comparison["status"],
                reason="current basis must match before private Host detail is read",
                basis_ref=basis_ref, changed=comparison["changed_components"],
                unknown=comparison["unknown_components"], refs={
                    "current_basis_ref": current["basis_ref"], "source_provenance": "client_attested",
                    "host_git_verified": False})
        _authorize_workspace(self.state_port, request, mapping, run_revision,
                             "execute", "resume-detail-authority-before")
        context_payload = {"project_id": mapping["project_id"],
            "repository_id": mapping["repository_id"], "canonical_workspace": mapping["canonical_workspace"],
            "relative_graph_path": mapping["relative_graph_path"],
            "run_id": payload["run_id"], "expected_run_revision": run_revision,
            "context_ref": context_ref}
        readback = _remote(self.state_port, _child_request(
            request, "read_task_context", context_payload, "resume-detail-context-read"))
        detail_payload = {key: context_payload[key] for key in (
            "project_id", "repository_id", "canonical_workspace", "relative_graph_path",
            "run_id", "expected_run_revision")}
        detail_payload.update({"context_id": context_ref["id"],
            "cursor": payload.get("cursor") or payload.get("detail_cursor"),
            "max_bytes": payload.get("max_bytes", 8192), "max_lines": payload.get("max_lines", 200)})
        detail = _remote(self.state_port, _child_request(
            request, "read_context_detail", detail_payload, "f5-detail-read"))

        _mapping_after, after = self._capture_current(request, "resume-detail-basis-after")
        after_compare = validate_basis_components(current["basis"], after["basis"])
        if after_compare["status"] != "unchanged" or after.get("complete") is not True:
            return _incomplete(request, status=after_compare["status"],
                reason="client source changed while Host F5 detail was being read",
                basis_ref=basis_ref, changed=after_compare["changed_components"],
                unknown=after_compare["unknown_components"], refs={
                    "current_basis_ref": after["basis_ref"], "source_provenance": "client_attested",
                    "host_git_verified": False})
        result = {"bundle_ref": context_ref["id"], "context_ref": context_ref,
            "basis_ref": old_basis["id"], "detail": detail,
            "change_evidence": change_evidence,
            "next_action": _change_next_action(change_evidence),
            "source_provenance": "client_attested", "host_git_verified": False,
            "owner_revalidated": True, "source_revalidated": True,
            "f5_owner_ref": readback.get("current_authority"),
            "complete": detail.get("next_cursor") is None and change_evidence.get("complete") is True,
            "detail_ref": context_ref["id"]}
        return response(request.get("request_id"), result=bounded_result(request, result))


def execute_hosted_context(profile, state_port, data_root, request, environ=None):
    """Factory entry point for the hosted storage selector's FILE routing."""
    return HostedContextClient(profile, state_port, data_root, environ).execute(request)
