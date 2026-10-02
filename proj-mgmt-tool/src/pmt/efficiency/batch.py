"""Pure F9 batch planning and result-shape validation.

This module does not create queue rows, acquire scopes, launch a runner, or
declare that an adapter supports grouped execution. Its manifests are plans for
the later F8/F9 integration gate, not execution receipts.
"""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import closing
from collections import defaultdict
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from ..errors import PmtError
from ..phase2_common import (event, normalized_workspace, project_scope_id,
                             require_workspace_claim, validate_scope)
from ..util import canonical_json, fingerprint, new_id, utc_now
from .storage import Phase3Storage

BATCH_SCHEMA = "pmt-batch-plan-v1"
BINDING_SCHEMA = "pmt-batch-binding-v1"
COLLECTION_SCHEMA = "pmt-batch-collection-v1"
READ_OPERATIONS: set[str] = set()
WRITE_OPERATIONS: set[str] = set()
FILE_OPERATIONS: set[str] = {"prepare_step_batch", "bind_step_batch", "collect_step_batch"}


def _bad(message: str, code: str = "batch_input_invalid") -> None:
    raise PmtError(code, message, 2)


def _uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        _bad(f"{label} must be a canonical UUID")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        _bad(f"{label} must be a canonical UUID")
    return value


def _ref(value: Any, label: str, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum \
            or "\n" in value or "\r" in value:
        _bad(f"{label} must be a short stable reference")
    return value


def _sha(value: Any, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        _bad(f"{label} must be a lowercase SHA-256 digest")
    return value


def _canonical(value: Any) -> str:
    try:
        return canonical_json(value)
    except (TypeError, ValueError) as exc:
        _bad("manifest contains a non-canonical value")


def _read_scope(value: Any, label: str) -> dict[str, str]:
    keys = {"kind", "workspace", "resource", "access"}
    if not isinstance(value, Mapping) or set(value) != keys:
        _bad(f"{label} must contain kind/workspace/resource/access")
    kind, access = value["kind"], value["access"]
    if kind not in {"workspace", "path", "resource"} or access not in {"read", "write"}:
        _bad(f"{label} has an unsupported kind or access mode")
    workspace = value["workspace"]
    try:
        workspace = normalized_workspace(workspace)
    except PmtError as exc:
        _bad(f"{label}.workspace is not an absolute local path or canonical PMT workspace URI")
    resource = value["resource"]
    if not isinstance(resource, str) or not resource.strip():
        _bad(f"{label}.resource must be nonempty")
    if kind == "workspace":
        resource = "."
    elif kind == "path":
        pure = PurePosixPath(resource.replace("\\", "/"))
        if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts) or Path(resource).drive:
            _bad(f"{label}.resource must remain workspace-relative")
        resource = pure.as_posix()
    else:
        resource = resource.strip().casefold()
    return {"kind": kind, "workspace": workspace, "resource": resource, "access": access}


def _scope_overlap(left: Mapping[str, str], right: Mapping[str, str]) -> bool:
    if left["kind"] == "resource" or right["kind"] == "resource":
        return left["kind"] == right["kind"] == "resource" and left["resource"] == right["resource"]
    if left["workspace"].casefold() != right["workspace"].casefold():
        return False
    if left["kind"] == "workspace" or right["kind"] == "workspace":
        return True
    a, b = left["resource"].casefold(), right["resource"].casefold()
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def _step(value: Any, index: int) -> dict[str, Any]:
    label = f"step_refs[{index}]"
    expected = {"step_id", "project_id", "scope_id", "source_pin_hash", "root_constraint_sha256",
                "directive", "criteria", "dependencies", "conflicts", "scopes", "authority_ref"}
    if not isinstance(value, Mapping) or set(value) != expected:
        _bad(f"{label} has missing or unsupported fields")
    step_id = _uuid(value["step_id"], label + ".step_id")
    project_id = _uuid(value["project_id"], label + ".project_id")
    scope_id = _uuid(value["scope_id"], label + ".scope_id")
    source_hash = _sha(value["source_pin_hash"], label + ".source_pin_hash")
    root_hash = _sha(value["root_constraint_sha256"], label + ".root_constraint_sha256")
    directive = value["directive"]
    if not isinstance(directive, Mapping) or set(directive) != {"ref", "version", "sha256"}:
        _bad(f"{label}.directive must pin ref/version/hash")
    directive_ref = {"ref": _ref(directive["ref"], label + ".directive.ref"),
                     "version": _ref(directive["version"], label + ".directive.version", 128),
                     "sha256": _sha(directive["sha256"], label + ".directive.sha256")}
    criteria = value["criteria"]
    if not isinstance(criteria, list) or not criteria:
        _bad(f"{label}.criteria must be a nonempty array")
    normalized_criteria, criterion_ids = [], set()
    for cindex, item in enumerate(criteria):
        if not isinstance(item, Mapping) or set(item) != {"id", "sha256"}:
            _bad(f"{label}.criteria[{cindex}] must pin id/hash")
        criterion_id = _ref(item["id"], f"{label}.criteria[{cindex}].id")
        if criterion_id in criterion_ids:
            _bad(f"{label}.criteria contains duplicate IDs")
        criterion_ids.add(criterion_id)
        normalized_criteria.append({"id": criterion_id,
                                    "sha256": _sha(item["sha256"], f"{label}.criteria[{cindex}].sha256")})
    dependencies = value["dependencies"]
    if not isinstance(dependencies, list):
        _bad(f"{label}.dependencies must be an array")
    normalized_dependencies = []
    seen_dependencies = set()
    for dindex, dependency in enumerate(dependencies):
        if not isinstance(dependency, Mapping) or set(dependency) != {"step_id", "state"}:
            _bad(f"{label}.dependencies[{dindex}] must pin step/state")
        dep_id = _uuid(dependency["step_id"], f"{label}.dependencies[{dindex}].step_id")
        dep_state = _ref(dependency["state"], f"{label}.dependencies[{dindex}].state", 64)
        if dep_id in seen_dependencies or dep_id == step_id:
            _bad(f"{label}.dependencies has duplicate or self reference")
        seen_dependencies.add(dep_id)
        normalized_dependencies.append({"step_id": dep_id, "state": dep_state})
    conflicts = value["conflicts"]
    if not isinstance(conflicts, list):
        _bad(f"{label}.conflicts must be an array")
    normalized_conflicts = sorted({_uuid(item, f"{label}.conflicts") for item in conflicts})
    if len(normalized_conflicts) != len(conflicts) or step_id in normalized_conflicts:
        _bad(f"{label}.conflicts has duplicate or self reference")
    raw_scopes = value["scopes"]
    if not isinstance(raw_scopes, list) or not raw_scopes:
        _bad(f"{label}.scopes must be a nonempty array")
    scopes = [_read_scope(item, f"{label}.scopes[{sindex}]") for sindex, item in enumerate(raw_scopes)]
    authority = value["authority_ref"]
    if not isinstance(authority, Mapping) or set(authority) != {"kind", "id", "sha256"}:
        _bad(f"{label}.authority_ref must be a bounded authority reference")
    authority_ref = {"kind": _ref(authority["kind"], label + ".authority_ref.kind", 64),
                     "id": _ref(authority["id"], label + ".authority_ref.id"),
                     "sha256": _sha(authority["sha256"], label + ".authority_ref.sha256")}
    return {"step_id": step_id, "project_id": project_id, "scope_id": scope_id,
            "source_pin_hash": source_hash, "root_constraint_sha256": root_hash,
            "directive": directive_ref, "criteria": sorted(normalized_criteria, key=lambda x: x["id"]),
            "dependencies": sorted(normalized_dependencies, key=lambda x: x["step_id"]),
            "conflicts": normalized_conflicts, "scopes": scopes, "authority_ref": authority_ref}


def _context(value: Any, member_ids: list[str]) -> dict[str, Any]:
    fields = {"context_ref", "version", "project_id", "source_pin_hash", "scope_ref",
              "member_step_ids", "shared_context_sha256", "omitted_required"}
    if not isinstance(value, Mapping) or set(value) != fields:
        _bad("context must pin version/scope/source/members and omissions")
    version = value["version"]
    if type(version) is not int or version < 1:
        _bad("context.version must be a positive integer")
    members = value["member_step_ids"]
    if not isinstance(members, list) or any(not isinstance(item, str) for item in members):
        _bad("context.member_step_ids must be an array")
    normalized_members = sorted(_uuid(item, "context.member_step_ids") for item in members)
    if len(set(normalized_members)) != len(normalized_members):
        _bad("context member IDs must be unique")
    omitted = value["omitted_required"]
    if not isinstance(omitted, list) or any(not isinstance(item, str) or not item for item in omitted):
        _bad("context.omitted_required must be an array of labels")
    return {"context_ref": _ref(value["context_ref"], "context.context_ref"),
            "version": version, "project_id": _uuid(value["project_id"], "context.project_id"),
            "source_pin_hash": _sha(value["source_pin_hash"], "context.source_pin_hash"),
            "scope_ref": _ref(value["scope_ref"], "context.scope_ref"),
            "member_step_ids": normalized_members,
            "shared_context_sha256": _sha(value["shared_context_sha256"], "context.shared_context_sha256"),
            "omitted_required": sorted(set(omitted)),
            "_members_match": normalized_members == sorted(member_ids)}


def _capability(value: Any) -> tuple[dict[str, Any] | None, str | None]:
    fields = {"capability_ref", "version", "sha256", "status", "max_steps",
              "multi_directive", "structured_result_mapping", "result_mapping", "cancellation_scope",
              "physical_slots", "single_step_supported"}
    if not isinstance(value, Mapping) or set(value) != fields:
        _bad("capability must include the complete tested adapter contract")
    version = value["version"]
    if type(version) is not int or version < 1:
        _bad("capability.version must be a positive integer")
    if type(value["max_steps"]) is not int or value["max_steps"] < 1:
        _bad("capability.max_steps must be positive")
    for name in ("multi_directive", "structured_result_mapping", "single_step_supported"):
        if type(value[name]) is not bool:
            _bad(f"capability.{name} must be boolean")
    if type(value["physical_slots"]) is not int or value["physical_slots"] != 1:
        _bad("one physical handle must occupy exactly one physical slot")
    if value["result_mapping"] not in {"step_id", "unsupported", "unknown"}:
        _bad("capability.result_mapping is unsupported")
    if value["cancellation_scope"] not in {"group", "isolated", "unknown"}:
        _bad("capability.cancellation_scope is unsupported")
    status = _ref(value["status"], "capability.status", 64)
    result = {key: value[key] for key in fields}
    result["capability_ref"] = _ref(value["capability_ref"], "capability.capability_ref")
    result["sha256"] = _sha(value["sha256"], "capability.sha256")
    if status != "verified_supported":
        return result, "capability_not_verified_supported"
    if not result["single_step_supported"]:
        return result, "single_step_not_supported"
    if result["max_steps"] < 2 or not result["multi_directive"] \
            or not result["structured_result_mapping"] or result["result_mapping"] != "step_id":
        return result, "multi_step_structured_results_unsupported"
    if result["cancellation_scope"] != "group":
        return result, "group_cancellation_not_supported"
    return result, None


def _topological_order(steps: list[dict[str, Any]]) -> tuple[list[str] | None, bool]:
    ids = {step["step_id"] for step in steps}
    incoming = {step_id: set() for step_id in ids}
    outgoing: dict[str, set[str]] = defaultdict(set)
    for step in steps:
        for dep in step["dependencies"]:
            if dep["step_id"] in ids:
                incoming[step["step_id"]].add(dep["step_id"])
                outgoing[dep["step_id"]].add(step["step_id"])
            elif dep["state"].casefold() != "done":
                return None, True
    ready = sorted(step_id for step_id, deps in incoming.items() if not deps)
    ordered = []
    while ready:
        step_id = ready.pop(0)
        ordered.append(step_id)
        for child in sorted(outgoing[step_id]):
            incoming[child].discard(step_id)
            if not incoming[child] and child not in ordered and child not in ready:
                ready.append(child)
                ready.sort()
    if len(ordered) != len(ids):
        return None, False
    return ordered, False


def _split_groups(steps: list[dict[str, Any]], reason_codes: list[str]) -> list[dict[str, Any]]:
    return [{"step_refs": [step["step_id"]], "reason_codes": list(reason_codes)} for step in steps]


def _scope_union_and_conflicts(steps: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[str]]:
    union: dict[tuple[str, str, str], dict[str, str]] = {}
    conflict_reasons = set()
    for step in steps:
        for scope in step["scopes"]:
            key = (scope["kind"], scope["workspace"], scope["resource"])
            if key not in union:
                union[key] = dict(scope)
            elif scope["access"] == "write":
                union[key]["access"] = "write"
    for index, left in enumerate(steps):
        for right in steps[index + 1:]:
            explicit = set(left["conflicts"]).intersection({right["step_id"]}) \
                or set(right["conflicts"]).intersection({left["step_id"]})
            if explicit:
                conflict_reasons.add("explicit_step_conflict")
            for a in left["scopes"]:
                for b in right["scopes"]:
                    if _scope_overlap(a, b) and "write" in {a["access"], b["access"]}:
                        conflict_reasons.add("overlapping_write_scope")
    return sorted(union.values(), key=lambda x: (x["kind"], x["workspace"], x["resource"])), sorted(conflict_reasons)


def _hash_manifest(body: dict[str, Any]) -> dict[str, Any]:
    return body | {"manifest_sha256": fingerprint(body)}


def _verified_manifest(value: Any, schema: str, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _bad(f"{label} must be an object")
    manifest = dict(value)
    digest = manifest.pop("manifest_sha256", None)
    if manifest.get("schema") != schema or not isinstance(digest, str) or fingerprint(manifest) != digest:
        _bad(f"{label} hash or schema is invalid", "batch_manifest_invalid")
    manifest["manifest_sha256"] = digest
    return manifest


class BatchPlanner:
    """Pure grouping decisions; persistence, authority and runner calls are adapters."""

    @staticmethod
    def prepare(step_refs: Any, context: Any, capability: Any) -> dict[str, Any]:
        if not isinstance(step_refs, list) or len(step_refs) < 2:
            _bad("batch planning requires at least two Step refs")
        steps = [_step(item, index) for index, item in enumerate(step_refs)]
        ids = [step["step_id"] for step in steps]
        if len(set(ids)) != len(ids):
            _bad("step_refs must have unique Step IDs")
        normalized_context = _context(context, ids)
        normalized_capability, capability_reason = _capability(capability)
        reasons: list[str] = []
        blockers: list[str] = []
        if normalized_context["omitted_required"]:
            blockers.append("required_context_omitted")
        if not normalized_context["_members_match"]:
            blockers.append("context_membership_mismatch")
        if any(step["project_id"] != normalized_context["project_id"] for step in steps):
            reasons.append("cross_project_steps")
        if any(step["source_pin_hash"] != normalized_context["source_pin_hash"] for step in steps):
            reasons.append("source_pin_mismatch")
        if any(step["root_constraint_sha256"] != steps[0]["root_constraint_sha256"] for step in steps):
            reasons.append("root_constraint_mismatch")
        workspaces = {scope["workspace"] for step in steps for scope in step["scopes"]
                      if scope["kind"] != "resource"}
        if len(workspaces) > 1:
            reasons.append("workspace_mismatch")
        order, dependency_blocked = _topological_order(steps)
        if dependency_blocked:
            blockers.append("external_dependency_incomplete")
        elif order is None:
            blockers.append("dependency_cycle")
        scope_union, scope_conflicts = _scope_union_and_conflicts(steps)
        reasons.extend(scope_conflicts)
        if capability_reason:
            (blockers if capability_reason == "single_step_not_supported" else reasons).append(capability_reason)
        if blockers:
            decision = "blocked"
            reason_codes = sorted(set(blockers + reasons))
            body = {"schema": BATCH_SCHEMA, "decision": decision, "reason_codes": reason_codes,
                    "step_refs": ids, "context": {k: v for k, v in normalized_context.items() if not k.startswith("_")},
                    "capability": normalized_capability, "physical_slots": 1,
                    "execution_enabled": False}
            return _hash_manifest(body)
        if reasons:
            decision = "split"
            reason_codes = sorted(set(reasons))
            body = {"schema": BATCH_SCHEMA, "decision": decision, "reason_codes": reason_codes,
                    "step_refs": ids, "proposed_groups": _split_groups(steps, reason_codes),
                    "context": {k: v for k, v in normalized_context.items() if not k.startswith("_")},
                    "capability": normalized_capability, "physical_slots": 1,
                    "execution_enabled": False}
            return _hash_manifest(body)
        nonce = str(uuid.uuid4())
        members = [{"step_id": step["step_id"], "project_id": step["project_id"],
                    "scope_id": step["scope_id"], "source_pin_hash": step["source_pin_hash"],
                    "root_constraint_sha256": step["root_constraint_sha256"],
                    "directive": step["directive"], "criteria": step["criteria"],
                    "dependencies": step["dependencies"], "authority_ref": step["authority_ref"]}
                   for step in steps]
        body = {"schema": BATCH_SCHEMA, "decision": "eligible", "reason_codes": [],
                "group_nonce": nonce, "project_id": normalized_context["project_id"],
                "source_pin_hash": normalized_context["source_pin_hash"], "context": normalized_context,
                "capability": normalized_capability["capability_ref"],
                "capability_sha256": normalized_capability["sha256"], "members": members,
                "representative_step_id": members[0]["step_id"],
                "step_order": order, "scope_union": scope_union,
                "scope_union_sha256": fingerprint(scope_union), "physical_slots": 1,
                "cancellation_scope": "group", "execution_enabled": False,
                "revalidation_required": ["step_state", "directive_hash", "source_pin", "authority", "scope_union"]}
        return _hash_manifest(body)

    @staticmethod
    def bind(plan: Any, actual_handle: Any, child_runs: Any) -> dict[str, Any]:
        normalized = _verified_manifest(plan, BATCH_SCHEMA, "BatchPlan")
        if normalized.get("decision") != "eligible" or normalized.get("execution_enabled") is not False:
            _bad("only a pure eligible plan can be proposed for binding", "batch_plan_not_eligible")
        if not isinstance(actual_handle, Mapping) or set(actual_handle) != {
                "handle_ref", "parent_run_ref", "state", "capability_ref", "capability_sha256",
                "physical_slots", "scope_union_sha256", "scope_authority_ref"}:
            _bad("actual_handle must carry the verified single-handle receipt")
        handle = {"handle_ref": _ref(actual_handle["handle_ref"], "handle_ref"),
                  "parent_run_ref": _uuid(actual_handle["parent_run_ref"], "parent_run_ref"),
                  "state": _ref(actual_handle["state"], "handle.state", 64),
                  "capability_ref": _ref(actual_handle["capability_ref"], "handle.capability_ref"),
                  "capability_sha256": _sha(actual_handle["capability_sha256"], "handle.capability_sha256"),
                  "physical_slots": actual_handle["physical_slots"],
                  "scope_union_sha256": _sha(actual_handle["scope_union_sha256"], "scope_union_sha256"),
                  "scope_authority_ref": _ref(actual_handle["scope_authority_ref"], "scope_authority_ref")}
        if type(handle["physical_slots"]) is not int or handle["physical_slots"] != 1:
            _bad("one physical handle must bind one slot", "batch_slot_mismatch")
        if handle["state"] not in {"starting", "running", "attached"}:
            _bad("handle state is not bindable", "batch_handle_unconfirmed")
        if handle["capability_ref"] != normalized["capability"] \
                or handle["capability_sha256"] != normalized["capability_sha256"]:
            _bad("handle capability does not match the planned capability", "batch_capability_mismatch")
        if handle["scope_union_sha256"] != normalized["scope_union_sha256"]:
            _bad("actual scope authority does not cover the planned union", "batch_scope_union_mismatch")
        if not isinstance(child_runs, list):
            _bad("child_runs must be an array")
        planned = {item["step_id"]: item for item in normalized["members"]}
        mapping, run_ids = [], set()
        for index, child in enumerate(child_runs):
            if not isinstance(child, Mapping) or set(child) != {
                    "step_id", "run_id", "directive_sha256", "scope_authority_ref"}:
                _bad(f"child_runs[{index}] has an invalid shape")
            step_id = _uuid(child["step_id"], f"child_runs[{index}].step_id")
            run_id = _uuid(child["run_id"], f"child_runs[{index}].run_id")
            member = planned.get(step_id)
            if not member or step_id in {item["step_id"] for item in mapping}:
                _bad("child run mapping has an unknown or duplicate Step", "batch_child_mapping_invalid")
            if run_id in run_ids:
                _bad("child run IDs must be unique", "batch_child_mapping_invalid")
            run_ids.add(run_id)
            if _sha(child["directive_sha256"], "child.directive_sha256") != member["directive"]["sha256"]:
                _bad("child run directive differs from the plan", "batch_directive_mismatch")
            mapping.append({"step_id": step_id, "run_id": run_id,
                            "directive_sha256": member["directive"]["sha256"],
                            "scope_authority_ref": _ref(child["scope_authority_ref"], "scope_authority_ref"),
                            "criteria": member["criteria"]})
        if set(item["step_id"] for item in mapping) != set(planned):
            _bad("child run mapping must cover every planned Step exactly once", "batch_child_mapping_incomplete")
        representative = normalized["representative_step_id"]
        representative_run = next(item["run_id"] for item in mapping if item["step_id"] == representative)
        if handle["parent_run_ref"] != representative_run:
            _bad("physical parent must be the representative member's existing P2 run",
                 "batch_representative_run_mismatch")
        body = {"schema": BINDING_SCHEMA, "plan_sha256": normalized["manifest_sha256"],
                "group_nonce": normalized["group_nonce"], "handle": handle,
                "members": mapping, "physical_slots": 1,
                "status": "binding_shape_validated_only", "evidence_tier": "pure_validation_only",
                "execution_enabled": False, "terminal_parent_promotion": False}
        return _hash_manifest(body)

    @staticmethod
    def collect(binding: Any, results: Any, *, cancel_observed: bool = False) -> dict[str, Any]:
        bound = _verified_manifest(binding, BINDING_SCHEMA, "BatchBinding")
        if type(cancel_observed) is not bool:
            _bad("cancel_observed must be boolean")
        if not isinstance(results, list):
            _bad("results must be an array")
        members = {item["step_id"]: item for item in bound["members"]}
        seen, normalized_results = set(), {}
        for index, result in enumerate(results):
            if not isinstance(result, Mapping):
                _bad(f"results[{index}] must be an object")
            step_id = _uuid(result.get("step_id"), f"results[{index}].step_id")
            member = members.get(step_id)
            if member is None or step_id in seen:
                _bad("result has an unknown or duplicate Step", "batch_result_mapping_invalid")
            seen.add(step_id)
            if _uuid(result.get("run_id"), f"results[{index}].run_id") != member["run_id"]:
                _bad("result run does not match the child mapping", "batch_result_run_mismatch")
            if _ref(result.get("handle_ref"), f"results[{index}].handle_ref") != bound["handle"]["handle_ref"]:
                _bad("result handle does not match the one physical binding", "batch_result_handle_mismatch")
            if _sha(result.get("directive_sha256"), f"results[{index}].directive_sha256") != member["directive_sha256"]:
                _bad("result directive does not match the child mapping", "batch_directive_mismatch")
            receipt = result.get("receipt_ref")
            evidence = result.get("evidence_refs")
            state = result.get("state")
            if state not in {"succeeded", "failed", "blocked", "canceled", "unknown", "not_run"}:
                _bad("result state is unsupported")
            if receipt is not None:
                receipt = _ref(receipt, f"results[{index}].receipt_ref")
            if not isinstance(evidence, list) or any(not isinstance(x, str) or not x for x in evidence):
                _bad("result evidence_refs must be an array of opaque refs")
            criteria = result.get("criteria_results", [])
            if not isinstance(criteria, list):
                _bad("criteria_results must be an array")
            expected_criteria = {item["id"]: item["sha256"] for item in member["criteria"]}
            criterion_results, criterion_ids = [], set()
            for cindex, item in enumerate(criteria):
                required = {"id", "sha256", "outcome", "evidence_refs", "reason"}
                if not isinstance(item, Mapping) or set(item) != required:
                    _bad(f"results[{index}].criteria_results[{cindex}] has an invalid shape")
                criterion_id = _ref(item["id"], "criterion.id")
                if criterion_id in criterion_ids or expected_criteria.get(criterion_id) != _sha(item["sha256"], "criterion.sha256"):
                    _bad("criterion result does not match the planned criteria", "batch_criteria_mismatch")
                criterion_ids.add(criterion_id)
                outcome = item["outcome"]
                if outcome not in {"pass", "fail", "blocked", "not_run"}:
                    _bad("criterion outcome is unsupported")
                refs = item["evidence_refs"]
                if not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs):
                    _bad("criterion evidence_refs must be an array")
                reason = item["reason"]
                if outcome in {"pass", "fail"} and not refs:
                    _bad("pass/fail criterion requires evidence refs", "batch_criterion_evidence_missing")
                if outcome in {"blocked", "not_run"} and (not isinstance(reason, str) or not reason.strip()):
                    _bad("blocked/not_run criterion requires a reason")
                criterion_results.append({"id": criterion_id, "sha256": item["sha256"],
                                          "reported_outcome": outcome, "evidence_refs": sorted(set(refs)),
                                          "reason": reason})
            validation_status = "receipt_pending_authoritative_check"
            if state == "succeeded" and criterion_ids != set(expected_criteria):
                validation_status = "criteria_mapping_incomplete"
            if state in {"failed", "blocked", "canceled", "unknown", "not_run"} and not receipt:
                validation_status = "receipt_missing"
            normalized_results[step_id] = {"step_id": step_id, "run_id": member["run_id"],
                "reported_state": state, "validation_status": validation_status,
                "receipt_ref": receipt, "evidence_refs": sorted(set(evidence)),
                "criteria_results": criterion_results}
        children = []
        for step_id in sorted(members):
            result = normalized_results.get(step_id)
            if result is None:
                result = {"step_id": step_id, "run_id": members[step_id]["run_id"],
                          "reported_state": "unknown", "validation_status": "unknown",
                          "receipt_ref": None, "evidence_refs": [],
                          "criteria_results": [], "reason": "group_cancelled_missing_result" if cancel_observed
                          else "child_result_missing"}
            children.append(result)
        states = {child["reported_state"] for child in children}
        aggregate = "unknown" if "unknown" in states else "results_unverified"
        body = {"schema": COLLECTION_SCHEMA, "binding_sha256": bound["manifest_sha256"],
                "group_nonce": bound["group_nonce"], "handle_ref": bound["handle"]["handle_ref"],
                "children": children, "cancel_observed": cancel_observed,
                "aggregate_status": aggregate, "parent_done": False,
                "physical_slots": 1, "evidence_tier": "caller_packet_unverified",
                "execution_enabled": False,
                "review_required": True}
        return _hash_manifest(body)


def prepare(step_refs: Any, context: Any, capability: Any) -> dict[str, Any]:
    return BatchPlanner.prepare(step_refs, context, capability)


def bind(plan: Any, actual_handle: Any, child_runs: Any) -> dict[str, Any]:
    return BatchPlanner.bind(plan, actual_handle, child_runs)


def collect(binding: Any, results: Any, *, cancel_observed: bool = False) -> dict[str, Any]:
    return BatchPlanner.collect(binding, results, cancel_observed=cancel_observed)


def _scope_covers(scope: Mapping[str, Any], workspace: str, relative: str) -> bool:
    if scope.get("workspace") != workspace:
        return False
    kind, resource = scope.get("kind"), scope.get("resource")
    if kind == "workspace" or resource in {"", "."}:
        return kind in {"workspace", "path"}
    if kind != "path":
        return False
    path, root = relative.replace("\\", "/").strip("/"), resource.replace("\\", "/").strip("/")
    return path == root or path.startswith(root + "/")


def _object(conn, scope_id, batch_id):
    row = conn.execute("SELECT * FROM phase3_objects WHERE kind='batch_binding' AND id=? AND scope_id=?",
                       (batch_id, scope_id)).fetchone()
    if not row:
        return None
    try:
        body = json.loads(row["body_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        raise PmtError("batch_binding_corrupt", "Stored batch binding is invalid JSON", 4)
    if not isinstance(body, dict) or body.get("batch_id") != batch_id:
        raise PmtError("batch_binding_corrupt", "Stored batch binding identity is invalid", 4)
    digest = body.get("manifest_sha256")
    unsigned = dict(body)
    unsigned.pop("manifest_sha256", None)
    if not isinstance(digest, str) or fingerprint(unsigned) != digest:
        raise PmtError("batch_binding_corrupt", "Stored batch binding hash does not match", 4)
    return {"row": row, "body": body}


def _save_object(conn, existing, *, batch_id, scope_id, actor, session, body, state, request_id):
    now = utc_now()
    digest = fingerprint({key: value for key, value in body.items() if key != "manifest_sha256"})
    body = dict(body, manifest_sha256=digest)
    if existing is None:
        conn.execute("INSERT INTO phase3_objects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("batch_binding", batch_id, scope_id, actor, session, digest, 1,
             canonical_json(body), state, now, now))
        return {"batch_ref": {"kind": "batch_binding", "id": batch_id, "scope_id": scope_id,
                              "revision": 1, "source_hash": digest}, "body": body}
    row, old_body = existing["row"], existing["body"]
    if (row["scope_id"], row["owner_actor"], row["owner_session"]) != (scope_id, actor, session):
        raise PmtError("ownership_conflict", "Batch binding belongs to another owner", 3)
    old_revision = row["revision"]
    conn.execute("UPDATE phase3_objects SET source_hash=?,revision=revision+1,body_json=?,state=?,updated_at=? "
                 "WHERE kind='batch_binding' AND id=? AND revision=?",
                 (digest, canonical_json(body), state, now, batch_id, old_revision))
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise PmtError("revision_conflict", "Batch binding changed concurrently", 3, True)
    return {"batch_ref": {"kind": "batch_binding", "id": batch_id, "scope_id": scope_id,
                          "revision": old_revision + 1, "source_hash": digest}, "body": body}


def _active_binding_rows(conn, scope_id=None):
    query = "SELECT * FROM phase3_objects WHERE kind='batch_binding' AND state IN ('contexts_building','prepared','running','cancel_requested','reconciling','review_pending')"
    args = ()
    if scope_id:
        query += " AND scope_id=?"
        args = (scope_id,)
    rows = conn.execute(query + " ORDER BY id", args).fetchall()
    result = []
    for row in rows:
        try:
            body = json.loads(row["body_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            raise PmtError("batch_binding_corrupt", "Active batch binding is invalid JSON", 4)
        unsigned = dict(body) if isinstance(body, dict) else {}
        digest = unsigned.pop("manifest_sha256", None)
        if body.get("schema") != BINDING_SCHEMA or not isinstance(digest, str) or fingerprint(unsigned) != digest:
            raise PmtError("batch_binding_corrupt", "Active batch binding hash does not match", 4)
        result.append({"row": row, "body": body})
    return result


def physical_active_runs(conn, active_states: set[str], *, agent=None, executor=None) -> list[dict[str, Any]]:
    """Collapse active child run rows to one existing representative physical run."""
    runs = [dict(row) for row in conn.execute("SELECT * FROM execution_runs ORDER BY id")
            if row["state"] in active_states]
    by_id = {run["id"]: run for run in runs}
    bindings = _active_binding_rows(conn)
    child_ids, group_leaders, inactive_leaders = set(), {}, set()
    for item in bindings:
        body = item["body"]
        leader = body.get("parent_run_ref")
        members = body.get("members")
        if not isinstance(leader, str) or not isinstance(members, list):
            raise PmtError("batch_binding_corrupt", "Batch physical slot mapping is invalid", 4)
        for member in members:
            if isinstance(member, dict) and isinstance(member.get("run_id"), str) and member["run_id"] != leader:
                child_ids.add(member["run_id"])
        if body.get("handle_active") is False:
            inactive_leaders.add(leader)
        group_leaders[leader] = body
    physical = []
    for run in runs:
        if run["id"] in child_ids or run["id"] in inactive_leaders:
            continue
        route = json.loads(run["route_json"])
        if agent is not None and route.get("agent") != agent:
            continue
        current_executor = route.get("adapter_kind") or route.get("mode")
        if executor is not None and current_executor != executor:
            continue
        physical.append(run)
    return physical


def child_scope_authority(db, conn, req, run, workspace, paths):
    """Resolve an explicit active group grant; session equality alone is never a grant."""
    from ..phase2_common import normalized_workspace, project_scope_id

    if not isinstance(paths, (list, tuple)):
        raise PmtError("scope_not_owned", "A child batch grant requires path refs", 3)
    empty_path_operations = {"read_step_directive"}
    if not paths and req.get("operation") not in empty_path_operations:
        raise PmtError("scope_not_owned", "This child operation requires an explicit claimed path", 3)
    step_scope = conn.execute("SELECT scope_id FROM records WHERE id=? AND kind='step'",
                              (run["step_id"],)).fetchone()
    if not step_scope:
        raise PmtError("scope_not_owned", "Batch child is not an existing Step run", 3)
    project_id = project_scope_id(conn, step_scope[0])
    run_workspace = normalized_workspace(run["workspace"])
    requested_workspace = normalized_workspace(workspace)
    if run_workspace != requested_workspace:
        raise PmtError("ownership_conflict", "Batch child belongs to another workspace", 3)
    from ..execution.service import _normalize_scopes, _get_run
    requested = (_normalize_scopes([{"kind": "path", "workspace": workspace, "resource": path}
                                    for path in paths], workspace) if paths else [])
    allowed_states = {"contexts_building", "prepared", "running", "cancel_requested",
                      "reconciling", "review_pending"}
    read_only_operations = {"read_step_directive", "build_task_context", "read_task_context",
                            "read_context_detail", "resolve_context_alias", "capture_source_pin",
                            "query_graph", "calculate_graph_impact", "lookup_verification"}
    for item in _active_binding_rows(conn, project_id):
        body, row = item["body"], item["row"]
        if (row["owner_actor"], row["owner_session"]) != (req.get("actor"), req.get("session_id")):
            continue
        if body.get("status") not in allowed_states or body.get("scope_id") != project_id:
            continue
        if normalized_workspace(body.get("workspace")) != run_workspace:
            continue
        leader_id = body.get("parent_run_ref")
        leader = conn.execute("SELECT * FROM execution_runs WHERE id=?", (leader_id,)).fetchone()
        if (not leader or leader["owner_session"] != req.get("session_id")
                or normalized_workspace(leader["workspace"]) != run_workspace):
            continue
        member = next((candidate for candidate in body.get("members", [])
                       if isinstance(candidate, dict) and candidate.get("run_id") == run["id"]
                       and candidate.get("step_id") == run["step_id"]), None)
        if not member:
            continue
        if run["state"] not in {"starting", "running", "review_pending", "cancel_requested", "reconciling"}:
            continue
        locks = [dict(lock) for lock in conn.execute("SELECT kind,workspace,resource,owner_session,run_id "
            "FROM scope_locks WHERE run_id=?", (leader_id,)).fetchall()]
        if not locks or any(lock["owner_session"] != req.get("session_id") for lock in locks):
            continue
        declared = member.get("declared_scopes", [])
        shared_reads = body.get("shared_read_paths", []) if req.get("operation") in read_only_operations else []
        for scope in requested:
            in_child = any(_scope_covers(item_scope, run_workspace, scope["resource"])
                           for item_scope in declared)
            in_shared_read = scope["resource"] in shared_reads
            parent_covered = any(_scope_covers(lock, run_workspace, scope["resource"]) for lock in locks)
            if not parent_covered or not (in_child or in_shared_read):
                raise PmtError("scope_not_owned", "Batch grant does not cover this child path", 3,
                               details={"batch_ref": body["batch_id"], "child_run_ref": run["id"]})
        return {"batch_ref": body["batch_id"], "parent_run_ref": leader_id,
                "scope_id": project_id, "step_id": run["step_id"], "run_id": run["id"],
                "scope_locks": locks, "declared_scopes": declared,
                "effective_authority": "explicit_batch_binding"}
    return None


def binding_for_run(conn, run_id, *, require_leader=False):
    for item in _active_binding_rows(conn):
        body = item["body"]
        member_ids = {member.get("run_id") for member in body.get("members", []) if isinstance(member, dict)}
        if run_id == body.get("parent_run_ref") or run_id in member_ids:
            if require_leader and run_id != body.get("parent_run_ref"):
                raise PmtError("batch_child_dispatch_denied", "Only the representative run may dispatch the physical handle", 3)
            return item
    return None


def assert_batch_child_dispatch(conn, run_id, operation):
    binding = binding_for_run(conn, run_id)
    if binding and run_id != binding["body"].get("parent_run_ref"):
        raise PmtError("batch_child_dispatch_denied", f"Child runs cannot independently {operation}", 3,
                       details={"batch_ref": binding["body"]["batch_id"],
                                "parent_run_ref": binding["body"]["parent_run_ref"]})
    return binding


def grouped_runner_input(db, req, parent_run, leader_context_ref):
    """Read each current child context/directive and build one bounded group prompt."""
    from ..service import execute as service_execute
    from ..steps import handle as steps_handle
    with closing(db.connect()) as conn:
        found = binding_for_run(conn, parent_run["id"], require_leader=True)
        if not found:
            raise PmtError("batch_binding_not_found", "Physical runner has no F9 batch binding", 3)
        body = found["body"]
        if body.get("status") not in {"prepared", "running"}:
            raise PmtError("batch_not_prepared", "Grouped runner cannot start before all F5 contexts are ready", 3)
        if leader_context_ref != body["members"][0].get("context_ref"):
            raise PmtError("batch_context_mismatch", "F8 context ref must match the representative child", 3)
        member_snapshots = [dict(member) for member in body["members"]]
    read_contexts, directives = [], []
    for member in member_snapshots:
        child_req = {**req, "scope_id": body["scope_id"],
                     "request_id": str(uuid.uuid5(uuid.UUID(body["batch_id"]),
                                                   "runner-context:" + member["run_id"])),
                     "operation": "read_task_context",
                     "payload": {"context_ref": member["context_ref"]}}
        metadata, code = service_execute(db, child_req)
        if code or not isinstance(metadata, dict) or not metadata.get("ok"):
            error = metadata.get("error") if isinstance(metadata, dict) else None
            raise PmtError((error or {}).get("code", "batch_context_unavailable"),
                           "A current child F5 context could not be revalidated", code or 3,
                           bool((error or {}).get("retryable")), (error or {}).get("details"))
        context = metadata.get("result") or {}
        if (context.get("current_authority", {}).get("run_id") != member["run_id"]
                or context.get("task", {}).get("step_id") != member["step_id"]
                or context.get("role") != member.get("role")
                or context.get("incomplete") is True or context.get("mandatory_omissions")
                or context.get("source", {}).get("source_hash") != body["source_hash"]):
            raise PmtError("batch_context_stale", "Child context is stale, incomplete, or bound to another run", 3)
        projection = context.get("projection")
        included = projection.get("included") if isinstance(projection, dict) else None
        budget = context.get("budget")
        required_sections = {"purpose", "goal", "non_goal", "change_scope", "inputs", "outputs",
                             "criteria", "tests", "logging", "unresolved"}
        if str(member.get("role", "")).casefold() in {"lower", "worker", "implement", "implementation"}:
            required_sections.update({"method", "autonomy"})
        included_ids = {item.get("section_id") for item in included if isinstance(item, dict)} \
            if isinstance(included, list) else set()
        if (not required_sections.issubset(included_ids) or not isinstance(budget, dict)
                or budget.get("over_budget") is True
                or type(budget.get("content_bytes")) is not int
                or type(budget.get("used_bytes")) is not int):
            raise PmtError("batch_context_incomplete", "Every child requires a complete role-specific bounded F5 projection", 3,
                           details={"step_id": member["step_id"],
                                    "missing_sections": sorted(required_sections - included_ids)})
        read_contexts.append(context)
        directive_req = {**req, "scope_id": body["scope_id"], "record_id": member["step_id"],
                         "operation": "read_step_directive",
                         "payload": {"run_id": member["run_id"], "step_id": member["step_id"]}}
        with closing(db.connect()) as conn:
            directive_result = steps_handle(db, conn, directive_req)
            current = conn.execute("SELECT directive_version FROM step_specs WHERE step_id=?",
                                   (member["step_id"],)).fetchone()
            if not current or str(current[0]) != member["directive"]["version"]:
                raise PmtError("directive_version_conflict", "Child directive changed after group preparation", 3)
        if fingerprint(directive_result["directive"]) != member["directive"]["sha256"]:
            raise PmtError("directive_hash_mismatch", "Private child directive hash no longer matches the group", 3)
        directives.append(directive_result["directive"])
    section_maps = []
    for context in read_contexts:
        included = context.get("projection", {}).get("included", [])
        section_maps.append({item.get("section_id"): (fingerprint(item), item)
                             for item in included if isinstance(item, dict)
                             and isinstance(item.get("section_id"), str)})
    common = {}
    common_ids = set(section_maps[0]) if section_maps else set()
    for section_map in section_maps[1:]:
        common_ids &= set(section_map)
    for section_id in sorted(common_ids):
        hashes = {section_map[section_id][0] for section_map in section_maps}
        if len(hashes) == 1:
            common[section_id] = section_maps[0][section_id][1]
    steps = []
    f5_content_bytes = 0
    f5_response_bytes = 0
    for member, directive, context, section_map in zip(member_snapshots, directives, read_contexts, section_maps):
        specifics = [section_map[name][1] for name in sorted(section_map)
                     if name not in common or section_map[name][0] != fingerprint(common[name])]
        budget = context["budget"]
        f5_content_bytes += budget["content_bytes"]
        f5_response_bytes += budget["used_bytes"]
        steps.append({"step_id": member["step_id"], "run_id": member["run_id"],
                      "role": context["role"],
                      "directive_version": member["directive"]["version"],
                      "directive_sha256": member["directive"]["sha256"],
                      "context_ref": member["context_ref"], "criteria": member["criteria"],
                      "directive_ref": member["directive"]["ref"],
                      "context_sections": specifics})
    aggregate_limit = body.get("context_budget", {}).get("max_bytes")
    if type(aggregate_limit) is not int or f5_content_bytes > aggregate_limit:
        raise PmtError("batch_aggregate_context_over_budget",
            "The complete group F5 projection exceeds its shared aggregate byte budget", 3,
            details={"member_content_bytes": f5_content_bytes,
                     "aggregate_limit_bytes": aggregate_limit})
    transmitted_f5 = {"shared_context_sections": list(common.values()),
                      "step_context_sections": [{"step_id": item["step_id"],
                                                 "sections": item["context_sections"]} for item in steps]}
    transmitted_f5_bytes = len(canonical_json(transmitted_f5).encode("utf-8"))
    prompt_body = {"batch_id": body["batch_id"], "group_contract": "pmt-batch-report-v1",
        "source_pin": body["source_pin"], "shared_context_sections": list(common.values()),
        "steps": steps, "step_order": body["plan"]["step_order"],
        "return_requirements": {"one_result_per_step": True, "map_by_step_id": True,
                                 "missing_or_uncertain": "report unknown; never infer success",
                                 "preserve_independent_criteria_and_evidence": True}}
    prompt = ("Complete each listed PMT Step within the shared source/scope using only its bounded F5 context. "
              "Directive refs are identity/provenance; do not fetch or reproduce full directives. Each criteria set is "
              "independent; do not merge outcomes. Return one JSON object with exactly `schema`, `batch_id`, and "
              "`steps`. Use schema `pmt-batch-report-v1`. Each step item must include step_id, run_id, "
              "directive_sha256, context_ref, summary, choices, criteria_results, tests, evidence_refs, and "
              "unresolved_items. Include exactly one criteria_result per listed criterion, with criterion_id, "
              "outcome (pass/fail/blocked/not_run), reason, and evidence_refs. Never claim evidence or tests "
              "that were not observed. An omitted or uncertain Step is not successful.\n\n"
              + canonical_json(prompt_body))
    prompt_bytes = len(prompt.encode("utf-8"))
    prompt_limit = 1024 * 1024
    if prompt_bytes > prompt_limit:
        raise PmtError("batch_group_prompt_too_large", "The complete grouped native action exceeds the 1 MiB prompt limit", 3,
                       details={"prompt_bytes": prompt_bytes, "limit_bytes": prompt_limit})
    return {"batch_ref": body["batch_id"], "parent_run_ref": body["parent_run_ref"],
            "handle_ref": body.get("handle_ref"), "members": [
                {"step_id": item["step_id"], "run_id": item["run_id"],
                 "role": item["role"], "directive_version": item["directive"]["version"],
                 "directive_ref": item["directive"]["ref"], "directive_sha256": item["directive"]["sha256"],
                 "context_ref": item["context_ref"], "criteria": item["criteria"]}
                for item in member_snapshots],
            "prompt": prompt, "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "context_refs": [item["context_ref"] for item in member_snapshots],
            "source_hash": body["source_hash"], "scope_union_sha256": body["scope_union_sha256"],
            "physical_slots": 1, "accounting": {"schema": "pmt-batch-prompt-accounting-v1",
                "measurement_basis": "UTF-8 bytes", "member_f5_content_bytes": f5_content_bytes,
                "member_f5_response_bytes": f5_response_bytes,
                "shared_f5_content_budget_bytes": aggregate_limit,
                "deduplicated_f5_payload_bytes": transmitted_f5_bytes,
                "group_prompt_bytes": prompt_bytes,
                "group_prompt_overhead_bytes": prompt_bytes - transmitted_f5_bytes,
                "group_prompt_limit_bytes": prompt_limit}}


def _run_payload(conn, run_id):
    from ..execution.service import _get_run, _json as _exec_json, _step
    run = _get_run(conn, run_id)
    step = _step(conn, run["step_id"])
    run["intent"] = _exec_json(run["intent_json"], "run intent", dict)
    run["route"] = _exec_json(run["route_json"], "run route", dict)
    run["scopes"] = _exec_json(run["scopes_json"], "run scopes", list)
    run["step"] = step
    return run


def _ancestor_task_id(conn, step_id):
    current = conn.execute("SELECT id,parent_id,kind FROM records WHERE id=?", (step_id,)).fetchone()
    seen = set()
    while current and current["id"] not in seen:
        seen.add(current["id"])
        if current["kind"] in {"work", "item"}:
            return current["id"]
        current = (conn.execute("SELECT id,parent_id,kind FROM records WHERE id=?",
                                (current["parent_id"],)).fetchone() if current["parent_id"] else None)
    return step_id


def _source_cover(scopes, workspace, relative):
    return any(_scope_covers(scope, workspace, relative) for scope in scopes)


def _capability_for_route(route):
    from ..runners.service import _command
    mode, agent = route.get("mode"), route.get("agent")
    single_supported = route.get("actual_support") == "verified_supported"
    if mode == "cli":
        if agent not in {"codex", "claude"} or route.get("auth_state") != "authenticated":
            single_supported = False
        else:
            runner, _ = _command(route)
            import shutil
            single_supported = single_supported and bool(shutil.which(runner))
    elif mode not in {"native", "subagent"}:
        single_supported = False
    raw = {"runner_capability_ref": route.get("capability_ref"), "mode": mode, "agent": agent,
           "provider": route.get("provider"), "model": route.get("model"),
           "batch_report_schema": "pmt-batch-report-v1", "cancel_scope": "group"}
    capability_hash = fingerprint(raw)
    return {"capability_ref": "pmt-batch-capability:" + capability_hash[:32],
            "version": 1, "sha256": capability_hash,
            "status": "verified_supported" if single_supported else "unknown",
            "max_steps": 64 if single_supported else 1,
            "multi_directive": single_supported, "structured_result_mapping": single_supported,
            "result_mapping": "step_id" if single_supported else "unknown",
            "cancellation_scope": "group" if single_supported else "unknown",
            "physical_slots": 1, "single_step_supported": single_supported}


def _actual_member_refs(conn, runs, project_id, expected_source_hash):
    from ..execution.service import _normalize_scopes, _validate_current_step
    from ..verification import _criteria
    members = []
    route_hashes = set()
    workspace_values = set()
    for run in runs:
        route_hashes.add(fingerprint(run["route"]))
        workspace = os.path.normcase(os.path.abspath(run["workspace"]))
        workspace_values.add(workspace)
        step = run["step"]
        step_project = project_scope_id(conn, step["scope_id"])
        if step_project != project_id:
            raise PmtError("batch_scope_mismatch", "Every grouped Step must belong to the authorized project", 3)
        if run["state"] != "queued" or run["owner_session"] is None:
            raise PmtError("batch_run_not_queued", "Every grouped P2 run must be queued before atomic group preparation", 3)
        if conn.execute("SELECT 1 FROM scope_locks WHERE run_id=?", (run["id"],)).fetchone():
            raise PmtError("batch_run_already_claimed", "Queued group members must not carry separate partial scope claims", 3)
        _validate_current_step(conn, run, step)
        body = json.loads(step["body_json"])
        if body.get("invalidated") is True or step["state"] in {"Done", "Canceled"}:
            raise PmtError("batch_step_unavailable", "Closed or invalidated Step cannot join a batch", 3)
        raw_scope = run["intent"].get("scopes") or run["scopes"]
        scopes = _normalize_scopes(raw_scope, run["workspace"])
        for item in scopes:
            item.pop("lock_key", None)
            item["access"] = "write"
        criteria = _criteria({"criteria": json.loads(step["criteria_json"])})
        dependencies = []
        for dep_id in run["intent"].get("dependencies", []):
            dep = conn.execute("SELECT state,scope_id FROM records WHERE id=? AND kind='step'", (dep_id,)).fetchone()
            if not dep or project_scope_id(conn, dep["scope_id"]) != project_id:
                raise PmtError("invalid_dependency", "Batch dependency must be a Step in the same project", 3)
            dependencies.append({"step_id": dep_id,
                                 "state": "Done" if dep["state"] == "Done" else "pending"})
        root_facts = {"project_id": project_id, "requirements_version": step["requirements_version"],
                      "plan_version": step["plan_version"], "plan_id": step["plan_id"],
                      "product_stage": step["product_stage"],
                      "ancestor_constraints": []}
        parent_id = step["parent_id"]
        visited = set()
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            parent = conn.execute("SELECT id,kind,parent_id,body_json FROM records WHERE id=?", (parent_id,)).fetchone()
            if not parent:
                break
            parent_body = json.loads(parent["body_json"])
            root_facts["ancestor_constraints"].append({"id": parent["id"], "kind": parent["kind"],
                "requirements": parent_body.get("criteria"), "product_scope": parent_body.get("product_scope"),
                "autonomy": parent_body.get("autonomy")})
            parent_id = parent["parent_id"]
        members.append({"step_id": step["id"], "run_id": run["id"],
            "project_id": project_id, "scope_id": project_id,
            "source_pin_hash": expected_source_hash,
            "root_constraint_sha256": fingerprint(root_facts),
            "directive": {"ref": step["directive_id"], "version": str(step["directive_version"]),
                          "sha256": conn.execute("SELECT sha256 FROM artifacts WHERE id=? AND state='ready'",
                                                 (step["directive_id"],)).fetchone()[0]},
            "criteria": [{"id": key, "sha256": digest} for key, digest in sorted(criteria.items())],
            "dependencies": dependencies,
            "conflicts": [],
            "scopes": scopes,
            "authority_ref": {"kind": "p2_run_scope_intent", "id": run["id"],
                              "sha256": fingerprint({"run_id": run["id"], "owner_session": run["owner_session"],
                                                     "scope_intent": scopes})},
            "task_id": _ancestor_task_id(conn, step["id"]),
            "role": step["role"], "directive_id": step["directive_id"],
            "directive_version": step["directive_version"], "criteria_values": json.loads(step["criteria_json"]),
            "criteria_sha256": fingerprint(json.loads(step["criteria_json"])),
            "workspace": workspace,
            "route": run["route"], "route_hash": fingerprint(run["route"]),
            "prior_intent": run["intent"], "prior_scopes_json": run["scopes_json"],
            "prior_route_json": run["route_json"], "prior_revision": run["revision"]})
    return members, route_hashes, workspace_values


def _context_request(req, operation, run, batch, *, context_ref=None):
    payload = {"run_id": run["id"], "workspace": batch["workspace"],
               "repository_id": batch["repository_id"],
               "relative_graph_path": batch["relative_graph_path"]}
    if operation == "read_task_context":
        payload = {"context_ref": context_ref}
    else:
        payload.update({"task_ref": {"task_id": run["task_id"], "step_id": run["step_id"],
                                      "run_id": run["id"]},
                       "role": run["role"], "expected_source": batch["source_pin"],
                       "budget": batch["context_budget"], "node_ids": batch["node_ids"],
                       "expected_run_revision": run["expected_run_revision"]})
    return {"protocol_version": 1, "operation": operation,
            "request_id": str(uuid.uuid5(uuid.UUID(batch["batch_id"]), operation + ":" + run["id"])),
            "actor": req["actor"], "session_id": req["session_id"],
            "scope_id": batch["scope_id"], "source": req.get("source", {"product": "cli"}),
            "payload": payload}


def _f5_build_all(db, req, batch):
    from ..service import execute as service_execute
    refs, projections, sources = [], [], []
    for member in batch["members"]:
        with closing(db.connect()) as conn:
            current = conn.execute("SELECT revision FROM execution_runs WHERE id=? AND step_id=?",
                                   (member["run_id"], member["step_id"])).fetchone()
        if not current:
            raise PmtError("batch_member_changed", "A current batch run disappeared before F5", 3)
        run = {"id": member["run_id"], "step_id": member["step_id"],
               "task_id": member["task_id"], "role": member["role"],
               "expected_run_revision": current["revision"]}
        request = _context_request(req, "build_task_context", run, batch)
        response, code = service_execute(db, request)
        if code or not isinstance(response, dict) or not response.get("ok"):
            error = response.get("error") if isinstance(response, dict) else None
            raise PmtError((error or {}).get("code", "batch_context_failed"),
                           "A child F5 context could not be built", code or 3,
                           bool((error or {}).get("retryable")),
                           {**((error or {}).get("details") or {}),
                            "cause_message": (error or {}).get("message")})
        result = response.get("result") or {}
        supplied_ref = result.get("context_ref")
        context_ref = ({key: supplied_ref[key] for key in
                        ("kind", "id", "scope_id", "source_hash", "version", "projection_hash")}
                       if isinstance(supplied_ref, dict) and all(key in supplied_ref for key in
                        ("kind", "id", "scope_id", "source_hash", "version", "projection_hash")) else None)
        if (result.get("incomplete") is True or result.get("mandatory_omissions")
                or not isinstance(context_ref, dict) or context_ref.get("scope_id") != batch["scope_id"]
                or context_ref.get("source_hash") != batch["source_hash"]):
            raise PmtError("batch_context_incomplete", "Every child requires a current complete F5 context", 3)
        read_req = _context_request(req, "read_task_context", run, batch, context_ref=context_ref)
        read, read_code = service_execute(db, read_req)
        if read_code or not read.get("ok"):
            error = read.get("error") if isinstance(read, dict) else None
            raise PmtError((error or {}).get("code", "batch_context_revalidation_failed"),
                           "A child F5 context failed current authority revalidation", read_code or 3,
                           bool((error or {}).get("retryable")),
                           {**((error or {}).get("details") or {}),
                            "cause_message": (error or {}).get("message")})
        checked = read.get("result") or {}
        if (checked.get("current_authority", {}).get("run_id") != run["id"]
                or checked.get("task", {}).get("step_id") != run["step_id"]
                or checked.get("incomplete") is True or checked.get("mandatory_omissions")
                or checked.get("source", {}).get("source_hash") != batch["source_hash"]):
            raise PmtError("batch_context_authority_mismatch", "F5 context is not current for its child run", 3)
        refs.append(context_ref)
        sources.append(checked.get("source", {}).get("source_hash"))
        projection = checked.get("projection") or {}
        projections.append({"step_id": run["step_id"],
            "sections": projection.get("included") if isinstance(projection.get("included"), list) else []})
    section_maps = []
    for projection in projections:
        section_maps.append({item.get("section_id"): fingerprint(item) for item in projection["sections"]
                             if isinstance(item, dict) and isinstance(item.get("section_id"), str)})
    common_sections = {}
    if section_maps:
        common_ids = set(section_maps[0])
        for section_map in section_maps[1:]:
            common_ids &= set(section_map)
        for section_id in sorted(common_ids):
            hashes = {section_map[section_id] for section_map in section_maps}
            if len(hashes) == 1:
                common_sections[section_id] = next(iter(hashes))
    shared_hash = fingerprint({"source_hash": batch["source_hash"], "project_id": batch["scope_id"],
                               "common_sections": common_sections,
                               "context_refs": sorted(item["id"] for item in refs)})
    return refs, shared_hash, common_sections


def _claim_group(db, req, run_refs, expected_source, workspace, repository_id,
                 relative_graph_path, context_budget, node_ids):
    from ..execution import service as execution
    from ..lifecycle import _event
    from .source import pin_source
    from ..resources import check_artifact
    from ..phase2_common import normalized_workspace

    p = req["payload"]
    expected_pin = pin_source(expected_source)
    run_ids = [item["run_id"] for item in run_refs]
    internal_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "pmt-batch-claim"))
    run_by_ref = {item["run_id"]: item for item in run_refs}
    actor, session = req["actor"], req["session_id"]

    def commit(conn, request):
        project_id = project_scope_id(conn, req["scope_id"])
        validate_scope(db, conn, project_id)
        if project_scope_id(conn, req["scope_id"]) != project_id:
            raise PmtError("batch_scope_mismatch", "Request scope is not the current project", 3)
        runs = [_run_payload(conn, run_id) for run_id in run_ids]
        if any(run["owner_session"] != session for run in runs):
            raise PmtError("ownership_conflict", "Every queued child run must belong to this session", 3)
        if len({run["step_id"] for run in runs}) != len(runs):
            raise PmtError("batch_members_invalid", "A Step may appear only once in a batch")
        for run in runs:
            supplied_revision = run_by_ref[run["id"]]["expected_run_revision"]
            if type(supplied_revision) is not int or supplied_revision != run["revision"]:
                raise PmtError("revision_conflict", "A queued child run changed before group preparation", 3,
                               details={"run_id": run["id"], "current_revision": run["revision"]})
            if run["state"] != "queued":
                raise PmtError("batch_run_not_queued", "Every group member must still be queued", 3)
            if conn.execute("SELECT 1 FROM scope_locks WHERE run_id=?", (run["id"],)).fetchone():
                raise PmtError("batch_partial_claim", "Queued child already holds a separate scope claim", 3)
            if run["job_id"] and conn.execute("SELECT state FROM execution_jobs WHERE id=?", (run["job_id"],)).fetchone()[0] != "queued":
                raise PmtError("batch_job_not_queued", "Every child job must still be queued", 3)
        members, route_hashes, workspaces = _actual_member_refs(conn, runs, project_id, expected_pin.source_hash)
        if len(route_hashes) != 1 or len(workspaces) != 1:
            steps = members
            return {"plan": {"schema": BATCH_SCHEMA, "decision": "split",
                "reason_codes": ["runner_route_mismatch" if len(route_hashes) != 1 else "workspace_mismatch"],
                "proposed_groups": [{"step_refs": [item["step_id"]], "reason_codes": ["single_runner_required"]}
                                    for item in steps], "execution_enabled": False}}
        route = runs[0]["route"]
        capability = _capability_for_route(route)
        context_seed = {"context_ref": "pending-batch-context:" + req["request_id"], "version": 1,
                        "project_id": project_id, "source_pin_hash": expected_pin.source_hash,
                        "scope_ref": "project-scope:" + project_id,
                        "member_step_ids": [member["step_id"] for member in members],
                        "shared_context_sha256": fingerprint({"source_hash": expected_pin.source_hash,
                            "project_id": project_id, "context_budget": context_budget}),
                        "omitted_required": []}
        plan_members = [{key: member[key] for key in (
            "step_id", "project_id", "scope_id", "source_pin_hash", "root_constraint_sha256",
            "directive", "criteria", "dependencies", "conflicts", "scopes", "authority_ref")}
                        for member in members]
        plan = BatchPlanner.prepare(plan_members, context_seed, capability)
        if plan["decision"] != "eligible":
            return {"plan": plan}
        scopes, scope_conflicts = [], []
        for run in runs:
            normalized = execution._normalize_scopes(run["intent"].get("scopes", run["scopes"]), run["workspace"])
            scopes.extend(normalized)
        union_by_key = {item["lock_key"]: item for item in scopes}
        scope_union = sorted(union_by_key.values(), key=lambda item: item["lock_key"])
        for item in scope_union:
            if not _scope_covers(item, normalized_workspace(workspace), relative_graph_path):
                continue
            break
        else:
            raise PmtError("batch_source_scope_required", "The F0 graph path must be within the declared scope union", 3)
        active_states = {"starting", "running", "reconciling", "cancel_requested"}
        physical = physical_active_runs(conn, active_states)
        same_route = physical_active_runs(conn, active_states, agent=route.get("agent"),
                                         executor=route.get("adapter_kind") or route.get("mode"))
        cap = min(3, route.get("max_concurrency", 1))
        if len(physical) >= 3 or len(same_route) >= cap:
            return {"plan": {"schema": BATCH_SCHEMA, "decision": "blocked",
                "reason_codes": ["physical_route_capacity"], "physical_slots": 1,
                "execution_enabled": False}}
        batch_id = plan["group_nonce"]
        for item in _active_binding_rows(conn, project_id):
            if set(run_ids) & {member.get("run_id") for member in item["body"].get("members", [])}:
                raise PmtError("batch_member_already_bound", "An active group already contains a requested run", 3)
        leader = runs[0]
        conflicts = execution._acquire_scopes(conn, leader["id"], session, scope_union)
        if conflicts:
            return {"plan": {"schema": BATCH_SCHEMA, "decision": "split",
                "reason_codes": ["scope_union_conflict"], "conflicts": conflicts,
                "proposed_groups": [{"step_refs": [member["step_id"]],
                                     "reason_codes": ["scope_union_conflict"]} for member in members],
                "execution_enabled": False}}
        now = utc_now()
        original_runs = []
        for run in runs:
            original_runs.append({"run_id": run["id"], "run_revision": run["revision"],
                "intent": run["intent"], "scopes": run["scopes"], "scopes_json": run["scopes_json"],
                "job_state": conn.execute("SELECT state FROM execution_jobs WHERE id=?", (run["job_id"],)).fetchone()[0]})
        source_path = relative_graph_path.replace("\\", "/").strip("/")
        body = {"schema": BINDING_SCHEMA, "batch_id": batch_id, "plan": plan,
                "status": "contexts_building", "project_id": project_id,
                "scope_id": project_id,
                "owner_actor": actor, "owner_session": session,
                "parent_run_ref": leader["id"], "handle_ref": None,
                "workspace": normalized_workspace(workspace), "repository_id": repository_id,
                "relative_graph_path": source_path,
                "expected_source_pin": expected_pin.to_dict(),
                "source_pin": None, "source_hash": None,
                "scope_union": [{key: value for key, value in item.items() if key != "lock_key"}
                                for item in scope_union],
                "scope_union_lock_refs": [item["lock_key"] for item in scope_union],
                "scope_union_sha256": fingerprint(scope_union),
                "shared_read_paths": [source_path],
                "context_budget": context_budget, "node_ids": node_ids,
                "capability": capability, "context_refs": [], "shared_context_sha256": None,
                "common_sections": {}, "members": [], "original_runs": original_runs,
                "runner_report_ref": None, "receipt_ref": None, "result_refs": [],
                "scope_locks_retained": True, "physical_slots": 1}
        storage = Phase3Storage(db)
        for index, (run, member) in enumerate(zip(runs, members)):
            intent = dict(run["intent"])
            intent.update({"batch_ref": batch_id, "batch_parent_run_id": leader["id"],
                           "batch_role": "leader" if index == 0 else "child",
                           "batch_context_required": True})
            if index == 0:
                intent["scopes"] = scope_union
                scopes_json = canonical_json(scope_union)
            else:
                scopes_json = run["scopes_json"]
            transition_req = {**req, "payload": {**req["payload"],
                "event_id": str(uuid.uuid5(uuid.UUID(req["payload"]["event_id"]),
                                           "batch-run-transition:" + run["id"]))}}
            revision = execution._transition(conn, transition_req, run, "starting", intent=intent,
                                             started_at=now, keep_locks=True)
            conn.execute("UPDATE execution_runs SET scopes_json=?,updated_at=? WHERE id=?",
                         (scopes_json, now, run["id"]))
            changed = conn.execute("UPDATE execution_jobs SET state='starting',updated_at=? WHERE id=? AND state='queued'",
                                   (now, run["job_id"]))
            if changed.rowcount != 1:
                raise PmtError("job_state_conflict", "Child job changed during group preparation", 3)
            member.update({"declared_scopes": [item for item in run["scopes"]],
                           "directive_sha256": member["directive"]["sha256"],
                           "context_ref": None, "context_hash": None,
                           "run_revision": revision, "job_id": run["job_id"],
                           "run_id": run["id"], "task_id": member["task_id"],
                           "role": run["step"]["role"], "state": "starting"})
        body["members"] = members
        body["leader_run_revision"] = conn.execute("SELECT revision FROM execution_runs WHERE id=?",
                                                   (leader["id"],)).fetchone()[0]
        body["manifest_sha256"] = fingerprint(body)
        storage.put_object("batch_binding", batch_id, project_id, actor, session,
                           body["manifest_sha256"], 0, body, state="contexts_building",
                           request_id=request["request_id"], conn=conn)
        _event(conn, request, event_id=_batch_event_id(request, "batch-created"),
               event_type="batch.created", scope_id=project_id, record_id=leader["step_id"],
               payload={"batch_ref": batch_id, "parent_run_ref": leader["id"],
                        "member_count": len(members), "scope_count": len(scope_union),
                        "source_hash": expected_pin.source_hash,
                        "plan_sha256": plan["manifest_sha256"], "physical_slots": 1})
        return {"batch_id": batch_id, "plan": plan, "leader_run_id": leader["id"],
                "scope_id": project_id, "workspace": normalized_workspace(workspace),
                "source_pin_expected": expected_pin.to_dict(), "member_count": len(members)}

    return commit


def _batch_event_id(req, purpose):
    source = req.get("payload", {}).get("event_id") or req["request_id"]
    try:
        base = uuid.UUID(source)
    except (TypeError, ValueError, AttributeError):
        raise PmtError("batch_event_invalid", "event_id must be a canonical UUID")
    return str(uuid.uuid5(base, "pmt-batch:" + purpose))


def _batch_response(req, result=None, error=None):
    from ..service import response
    return response(req.get("request_id"), result=result, error=error)


def _batch_contexts(db, req, initial, p):
    from .graph import _source_graph
    from .source import pin_source, verify_source_pin

    with closing(db.connect()) as conn:
        source_context = _source_graph(db, conn, req)
    expected = pin_source(initial["source_pin_expected"])
    verify_source_pin(expected, source_context["source_pin"])
    if source_context["scope_id"] != initial["scope_id"]:
        raise PmtError("source_conflict", "F0 SourcePin project does not match the group scope", 3)
    group = {**initial, "source_pin": source_context["source_pin"].to_dict(),
             "source_hash": source_context["source_pin"].source_hash}
    context_refs, shared_hash, common_sections = _f5_build_all(db, req, group)
    final_plan = dict(initial["plan"])
    final_plan.pop("manifest_sha256", None)
    context_data = dict(final_plan["context"])
    context_data.update({"context_ref": "batch-context:" + initial["batch_id"],
                         "source_pin_hash": source_context["source_pin"].source_hash,
                         "shared_context_sha256": shared_hash})
    final_plan["context"] = context_data
    final_plan["member_context_refs"] = context_refs
    final_plan["common_sections"] = common_sections
    final_plan["manifest_sha256"] = fingerprint(final_plan)
    group["plan"] = final_plan
    group["context_refs"] = context_refs
    group["shared_context_sha256"] = shared_hash
    group["common_sections"] = common_sections
    for member, ref in zip(group["members"], context_refs):
        member["context_ref"] = ref
        member["context_hash"] = fingerprint(ref)
    return group


def _rollback_pre_dispatch(db, req, batch_ref, code):
    from ..execution import service as execution
    from ..lifecycle import _event
    with closing(db.connect()) as conn:
        batch = _object(conn, req["scope_id"], batch_ref)
    if not batch:
        return False
    body = batch["body"]
    if body.get("status") != "contexts_building" or body.get("handle_ref"):
        return False
    members = body.get("members", [])
    leader_id = body.get("parent_run_ref")
    if not members or not leader_id:
        return False
    binding_revision = batch["row"]["revision"]
    internal = {**req, "request_id": str(uuid.uuid5(uuid.UUID(req["request_id"]), "batch-context-abort")),
                "operation": "prepare_step_batch"}

    def rollback(conn, request):
        current_binding = _object(conn, req["scope_id"], batch_ref)
        if not current_binding or current_binding["row"]["revision"] != binding_revision:
            raise PmtError("batch_abort_conflict", "Batch binding changed; group locks are retained", 3)
        body_now = current_binding["body"]
        parent = execution._get_run(conn, leader_id)
        runner_journal = str(uuid.uuid5(uuid.UUID(leader_id), "pmt-runner-dispatch"))
        if (parent.get("handle_json") is not None
                or conn.execute("SELECT 1 FROM operation_journal WHERE id=?", (runner_journal,)).fetchone()):
            raise PmtError("batch_abort_unsafe", "A runner effect may have started; group locks are retained", 3)
        current_locks = conn.execute("SELECT lock_key,owner_session FROM scope_locks WHERE run_id=?",
                                     (leader_id,)).fetchall()
        if ({item["lock_key"] for item in current_locks} != set(body_now.get("scope_union_lock_refs", []))
                or any(item["owner_session"] != req["session_id"] for item in current_locks)):
            raise PmtError("batch_abort_unsafe", "Representative scope claim changed; locks are retained", 3)
        originals = {item["run_id"]: item for item in body_now.get("original_runs", [])}
        for member in body_now["members"]:
            run = execution._get_run(conn, member["run_id"])
            original = originals.get(run["id"])
            if (not original or run["state"] != "starting" or run.get("handle_json") is not None
                    or run["revision"] != member["run_revision"]):
                raise PmtError("batch_abort_unsafe", "A child changed after group prepare; locks are retained", 3)
            prior_intent = original["intent"]
            transition_req = {**request, "payload": {**request["payload"],
                "event_id": str(uuid.uuid5(uuid.UUID(req["payload"]["event_id"]),
                                           "batch-abort-transition:" + run["id"]))}}
            revision = execution._transition(conn, transition_req, run, "queued", intent=prior_intent,
                                              keep_locks=run["id"] != leader_id)
            conn.execute("UPDATE execution_runs SET scopes_json=?,updated_at=? WHERE id=?",
                         (original["scopes_json"], utc_now(), run["id"]))
            conn.execute("UPDATE execution_jobs SET state='queued',updated_at=? WHERE id=?",
                         (utc_now(), run["job_id"]))
        # Leader's P2 transition removes only the newly acquired union lock rows.
        next_body = {**body_now, "status": "aborted", "abort_reason": code,
                     "scope_locks_retained": False}
        _save_object(conn, current_binding, batch_id=batch_ref, scope_id=req["scope_id"],
                     actor=req["actor"], session=req["session_id"], body=next_body,
                     state="aborted", request_id=request["request_id"])
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (leader_id,))
        _event(conn, request, event_id=_batch_event_id(req, "batch-context-aborted"),
               event_type="batch.split", scope_id=req["scope_id"],
               payload={"batch_ref": batch_ref, "reason_code": code,
                        "parent_run_ref": leader_id, "scope_locks_retained": False})
        return {"batch_ref": batch_ref, "status": "aborted", "reason_code": code,
                "scope_locks_retained": False}
    envelope, exit_code = db.run_request(internal, rollback)
    return exit_code == 0 and bool(envelope.get("ok"))


def _complete_prepared_group(db, req, group):
    from ..execution import service as execution
    from ..lifecycle import _event
    batch_ref = group["batch_id"]
    internal = {**req, "request_id": str(uuid.uuid5(uuid.UUID(req["request_id"]), "batch-context-ready")),
                "operation": "prepare_step_batch"}

    def commit(conn, request):
        current = _object(conn, group["scope_id"], batch_ref)
        if not current or current["body"].get("status") != "contexts_building":
            raise PmtError("batch_prepare_conflict", "Batch is no longer waiting for F5 contexts", 3)
        body = {**current["body"], "members": group["members"],
                "plan": group["plan"], "source_pin": group["source_pin"],
                "source_hash": group["source_hash"], "context_refs": group["context_refs"],
                "shared_context_sha256": group["shared_context_sha256"],
                "common_sections": group["common_sections"]}
        for member in body["members"]:
            run = execution._get_run(conn, member["run_id"])
            if (run["state"] != "starting" or run["revision"] != member["run_revision"]
                    or run["owner_session"] != req["session_id"]):
                raise PmtError("batch_member_changed", "A child run changed during F5 context preparation", 3)
            step = execution._step(conn, member["step_id"])
            execution._validate_current_step(conn, run, step)
            if (step["directive_version"] != member["directive_version"]
                    or conn.execute("SELECT sha256 FROM artifacts WHERE id=? AND state='ready'",
                                    (step["directive_id"],)).fetchone()[0] != member["directive_sha256"]
                    or fingerprint(json.loads(step["criteria_json"])) != member["criteria_sha256"]
                    or fingerprint(json.loads(run["route_json"])) != member["route_hash"]):
                raise PmtError("batch_member_changed", "Directive, criteria, or runner route changed during context build", 3)
            context_object = Phase3Storage(db).get_object("task_context", member["context_ref"]["id"],
                body["scope_id"], req["actor"], req["session_id"], conn=conn)
            if (not context_object or context_object["state"] != "ready"
                    or context_object["body"].get("projection_hash") != member["context_ref"].get("projection_hash")
                    or context_object["source_hash"] != body["source_hash"]):
                raise PmtError("batch_context_stale", "Stored F5 context does not match the group pin", 3)
            intent = json.loads(run["intent_json"])
            intent["batch_context_ref"] = member["context_ref"]
            intent["batch_context_sha256"] = member["context_hash"]
            if run["id"] == body["parent_run_ref"]:
                # F8 needs the immutable F9 binding identity to re-read and
                # validate every member before handing off one native action.
                intent["batch_ref"] = batch_ref
                intent["context_ref"] = member["context_ref"]
            conn.execute("UPDATE execution_runs SET intent_json=?,updated_at=? WHERE id=? AND revision=?",
                         (canonical_json(intent), utc_now(), run["id"], run["revision"]))
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise PmtError("revision_conflict", "Child run changed while context refs were bound", 3, True)
        locked = conn.execute("SELECT lock_key,owner_session FROM scope_locks WHERE run_id=? ORDER BY lock_key",
                              (body["parent_run_ref"],)).fetchall()
        if ({item["lock_key"] for item in locked} != set(body["scope_union_lock_refs"])
                or any(item["owner_session"] != req["session_id"] for item in locked)):
            raise PmtError("batch_scope_changed", "Representative no longer owns the complete group union", 3)
        next_body = {**body, "plan": group["plan"], "source_pin": group["source_pin"],
                     "source_hash": group["source_hash"], "context_refs": group["context_refs"],
                     "shared_context_sha256": group["shared_context_sha256"],
                     "common_sections": group["common_sections"], "status": "prepared",
                     "prepared_at": utc_now(), "scope_locks_retained": True}
        saved = _save_object(conn, current, batch_id=batch_ref, scope_id=group["scope_id"],
                             actor=req["actor"], session=req["session_id"], body=next_body,
                             state="prepared", request_id=request["request_id"])
        _event(conn, request, event_id=_batch_event_id(req, "batch-contexts-ready"),
               event_type="batch.step_bound", scope_id=group["scope_id"],
               record_id=body["members"][0]["step_id"],
               payload={"batch_ref": batch_ref, "parent_run_ref": body["parent_run_ref"],
                        "member_count": len(body["members"]),
                        "context_hash": group["shared_context_sha256"],
                        "scope_union_sha256": body["scope_union_sha256"]})
        return {"batch_ref": saved["batch_ref"], "status": "prepared", "parent_run_ref": body["parent_run_ref"],
                "member_run_refs": [{"step_id": item["step_id"], "run_id": item["run_id"],
                                     "directive_sha256": item["directive"]["sha256"],
                                     "context_ref": item["context_ref"]} for item in next_body["members"]],
                "scope_union_sha256": body["scope_union_sha256"], "physical_slots": 1,
                "execution_enabled": True}
    envelope, code = db.run_request(internal, commit)
    if code or not envelope.get("ok"):
        error = envelope.get("error") or {}
        raise PmtError(error.get("code", "batch_context_bind_failed"),
                       "F5 context refs could not be bound to the group", code or 3,
                       bool(error.get("retryable")), error.get("details"))
    return envelope["result"]


def _prepare_step_batch(db, req):
    from ..service import response
    p = req.get("payload", {})
    allowed = {"run_refs", "workspace", "repository_id", "relative_graph_path", "expected_source",
               "context_budget", "event_id"}
    if set(p) - allowed:
        raise PmtError("unknown_fields", "Unsupported batch preparation fields",
                       details={"fields": sorted(set(p) - allowed)})
    run_refs = p.get("run_refs")
    if not isinstance(run_refs, list) or len(run_refs) < 2:
        raise PmtError("batch_input_invalid", "At least two queued P2 run refs are required")
    normalized_runs = []
    for index, item in enumerate(run_refs):
        if not isinstance(item, dict) or set(item) != {"run_id", "expected_run_revision"}:
            raise PmtError("batch_input_invalid", f"run_refs[{index}] must pin run_id and revision")
        normalized_runs.append({"run_id": _uuid(item["run_id"], f"run_refs[{index}].run_id"),
                                "expected_run_revision": item["expected_run_revision"]})
    if len({item["run_id"] for item in normalized_runs}) != len(normalized_runs):
        raise PmtError("batch_input_invalid", "Run refs must be unique")
    try:
        event_id = _uuid(p.get("event_id"), "event_id")
        if event_id == req["request_id"]:
            raise PmtError("batch_input_invalid", "request_id and event_id are distinct identities")
        expected_source = p.get("expected_source")
        from .source import pin_source
        expected_pin = pin_source(expected_source)
        repository_id = _uuid(p.get("repository_id"), "repository_id")
        workspace = p.get("workspace")
        relative_graph_path = _ref(p.get("relative_graph_path"), "relative_graph_path", 1024)
        from .context import _workspace_graph_path, _context_budget
        relative_graph_path = _workspace_graph_path(relative_graph_path)
        if not isinstance(workspace, str):
            raise PmtError("invalid_workspace", "Batch requires a current workspace identity")
        from ..phase2_common import normalized_workspace
        workspace = normalized_workspace(workspace)
        context_budget = _context_budget(p.get("context_budget", {"max_bytes": 32768,
                                                                     "max_lines": 500, "unit": "utf8"}))
        # F5 itself selects current Step-linked nodes; do not accept arbitrary caller graph selectors.
        node_ids = []
    except PmtError:
        raise

    internal_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "pmt-batch-claim"))
    internal = {**req, "request_id": internal_id, "operation": "prepare_step_batch"}
    claim = _claim_group(db, internal, normalized_runs, expected_pin.to_dict(), workspace,
                         repository_id, relative_graph_path, context_budget, node_ids)
    claim_envelope, claim_code = db.run_request(internal, claim)
    if claim_code or not claim_envelope.get("ok"):
        error = claim_envelope.get("error") or {}
        raise PmtError(error.get("code", "batch_prepare_failed"), error.get("message", "P2 group claim failed"),
                       claim_code or 3, bool(error.get("retryable")), error.get("details"))
    claim_result = claim_envelope.get("result") or {}
    if isinstance(claim_result.get("plan"), dict) and claim_result["plan"].get("decision") != "eligible":
        response_envelope, code = db.run_request(req, lambda conn, request: claim_result)
        return response_envelope, code
    batch_id = claim_result["batch_id"]
    with closing(db.connect()) as conn:
        stored = _object(conn, req["scope_id"], batch_id)
    if not stored:
        raise PmtError("batch_binding_not_found", "Prepared batch binding disappeared", 4)
    if stored["body"].get("status") == "prepared":
        ready = {"batch_ref": {"kind": "batch_binding", "id": batch_id,
                 "scope_id": stored["body"]["scope_id"], "revision": stored["row"]["revision"],
                 "source_hash": stored["row"]["source_hash"]},
                 "status": "prepared", "parent_run_ref": stored["body"]["parent_run_ref"],
                 "member_run_refs": [{"step_id": member["step_id"], "run_id": member["run_id"],
                                      "directive_sha256": member["directive"]["sha256"],
                                      "context_ref": member["context_ref"]}
                                     for member in stored["body"]["members"]],
                 "scope_union_sha256": stored["body"]["scope_union_sha256"],
                 "physical_slots": 1, "execution_enabled": True}
        envelope, code = db.run_request(req, lambda conn, request: ready)
        return envelope, code
    if stored["body"].get("status") != "contexts_building":
        raise PmtError("batch_prepare_not_resumable", "Group preparation is no longer pending F5 context", 3)
    batch = stored["body"]
    source_req = {**req, "scope_id": batch["scope_id"],
                  "payload": {**p, "run_id": batch["parent_run_ref"]}}
    from .graph import _source_graph
    with closing(db.connect()) as conn:
        current_source = _source_graph(db, conn, source_req)
    from .source import verify_source_pin
    verify_source_pin(batch["expected_source_pin"], current_source["source_pin"])
    batch["source_pin"] = current_source["source_pin"].to_dict()
    batch["source_hash"] = current_source["source_pin"].source_hash
    try:
        context_refs, shared_hash, common_sections = _f5_build_all(db, req, batch)
        actual_plan = dict(batch["plan"])
        actual_plan.pop("manifest_sha256", None)
        actual_plan["context"] = {**actual_plan["context"],
            "context_ref": "batch-context:" + batch_id,
            "source_pin_hash": batch["source_hash"],
            "shared_context_sha256": shared_hash}
        actual_plan["member_context_refs"] = context_refs
        actual_plan["common_sections"] = common_sections
        actual_plan["manifest_sha256"] = fingerprint(actual_plan)
        batch.update(plan=actual_plan, context_refs=context_refs,
                     shared_context_sha256=shared_hash, common_sections=common_sections)
        for member, ref in zip(batch["members"], context_refs):
            member["context_ref"] = ref
            member["context_hash"] = fingerprint(ref)
        from .graph import _source_graph
        with closing(db.connect()) as conn:
            fresh = _source_graph(db, conn, {**req, "scope_id": batch["scope_id"],
                "payload": {**p, "run_id": batch["parent_run_ref"]}})
        verify_source_pin(batch["source_pin"], fresh["source_pin"])
        ready = _complete_prepared_group(db, req, batch)
    except PmtError as exc:
        if exc.code in {"source_conflict", "context_source_changed", "context_authority_changed",
                        "batch_member_changed"}:
            # Source/authority changed after acquisition; preserve union locks for reconcile.
            with db.write() as conn:
                current = _object(conn, batch["scope_id"], batch_id)
                if current and current["body"].get("status") == "contexts_building":
                    revised = {**current["body"], "status": "reconciling",
                               "unknown_reason": exc.code, "handle_active": False,
                               "scope_locks_retained": True}
                    _save_object(conn, current, batch_id=batch_id, scope_id=batch["scope_id"],
                                 actor=req["actor"], session=req["session_id"], body=revised,
                                 state="reconciling", request_id=internal_id)
            raise
        rolled_back = _rollback_pre_dispatch(db, req, batch_id, exc.code)
        if rolled_back:
            raise PmtError("batch_context_failed", "F5 context preparation failed before any runner dispatch; queued runs and the new group scope claim were restored",
                           exc.exit_code, exc.retryable,
                           {"cause": exc.code, "cause_details": exc.details, "batch_ref": batch_id}) from exc
        raise PmtError("batch_context_unknown", "F5 failed and the group could not be safely rolled back; scope locks remain held",
                       3, False, {"cause": exc.code, "batch_ref": batch_id}) from exc
    envelope, code = db.run_request(req, lambda conn, request: ready)
    return envelope, code


def bind_actual_parent_handle(conn, req, parent_run, handle):
    """P2 attach hook: bind the one acknowledged leader handle to every logical child."""
    binding_item = binding_for_run(conn, parent_run["id"], require_leader=True)
    if not binding_item:
        return None
    from ..execution import service as execution
    from ..lifecycle import _event
    body, row = binding_item["body"], binding_item["row"]
    if body.get("status") == "running":
        if body.get("handle_ref") == handle.get("id"):
            return {"batch_ref": body["batch_id"], "status": "running", "replayed": True}
        raise PmtError("batch_handle_conflict", "A different physical handle is already bound", 3)
    if body.get("status") != "prepared" or body.get("parent_run_ref") != parent_run["id"]:
        raise PmtError("batch_not_prepared", "Only a prepared batch leader can attach the physical handle", 3)
    leader = execution._get_run(conn, body["parent_run_ref"])
    if leader["state"] != "running" or not leader.get("handle_json") \
            or json.loads(leader["handle_json"]) != handle:
        raise PmtError("batch_handle_unverified", "P2 leader handle must be durably attached first", 3)
    next_members = []
    for member in body["members"]:
        child = execution._get_run(conn, member["run_id"])
        if child["owner_session"] != req["session_id"]:
            raise PmtError("ownership_conflict", "Batch child owner changed before handle binding", 3)
        if child["id"] != leader["id"]:
            if child["state"] != "starting" or child.get("handle_json") is not None:
                raise PmtError("batch_child_state_conflict", "Every nonleader child must still be unbound and starting", 3)
            event_source = req["payload"].get("event_id") or req["request_id"]
            transition_req = {**req, "payload": {**req["payload"],
                "event_id": str(uuid.uuid5(uuid.UUID(event_source),
                                           "batch-child-handle:" + child["id"]))}}
            execution._transition(conn, transition_req, child, "running", handle=handle, keep_locks=True)
            conn.execute("UPDATE execution_jobs SET state='running',updated_at=? WHERE id=?",
                         (utc_now(), child["job_id"]))
        updated = dict(member, state="running", handle_ref=handle.get("id"))
        next_members.append(updated)
    revised = {**body, "members": next_members, "status": "running", "handle_ref": handle.get("id"),
               "handle_sha256": fingerprint({"id": handle.get("id"),
                                               "runner_kind": handle.get("runner_kind"),
                                               "state_ref": handle.get("state_ref")}),
               "handle_acknowledged_at": utc_now(), "physical_slots": 1}
    _save_object(conn, binding_item, batch_id=body["batch_id"], scope_id=body["scope_id"],
                 actor=req["actor"], session=req["session_id"], body=revised,
                 state="running", request_id=req["request_id"])
    _event(conn, req, event_id=_batch_event_id(req, "batch-handle-bound"),
           event_type="batch.step_bound", scope_id=body["scope_id"],
           record_id=leader["step_id"], payload={"batch_ref": body["batch_id"],
             "parent_run_ref": leader["id"], "handle_ref": handle.get("id"),
             "member_count": len(next_members), "physical_slots": 1})
    return {"batch_ref": body["batch_id"], "status": "running", "handle_ref": handle.get("id"),
            "physical_slots": 1}


def _bind_step_batch(db, req):
    p = req["payload"]
    if set(p) != {"batch_ref", "parent_run_id", "event_id"}:
        raise PmtError("batch_input_invalid", "bind_step_batch requires batch_ref/parent_run_id/event_id")
    batch_ref = _uuid(p["batch_ref"], "batch_ref")
    parent_id = _uuid(p["parent_run_id"], "parent_run_id")
    event_id = _uuid(p["event_id"], "event_id")
    if event_id == req["request_id"]:
        raise PmtError("batch_input_invalid", "request_id and event_id are distinct identities")

    def commit(conn, request):
        binding = _object(conn, req["scope_id"], batch_ref)
        if not binding:
            raise PmtError("batch_binding_not_found", "Batch binding is unavailable", 3)
        body = binding["body"]
        if body.get("parent_run_ref") != parent_id or body.get("owner_actor") != req["actor"] \
                or body.get("owner_session") != req["session_id"]:
            raise PmtError("ownership_conflict", "Batch binding belongs to another owner or parent run", 3)
        if body.get("status") == "running":
            return {"batch_ref": batch_ref, "status": "running", "handle_ref": body.get("handle_ref"),
                    "physical_slots": 1, "replayed": True}
        leader = conn.execute("SELECT state,handle_json FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
        if not leader or leader["state"] != "running" or not leader["handle_json"]:
            raise PmtError("batch_handle_unverified", "Parent run has no acknowledged physical handle", 3)
        handle = json.loads(leader["handle_json"])
        result = bind_actual_parent_handle(conn, request, {"id": parent_id}, handle)
        return result
    envelope, code = db.run_request(req, commit)
    return envelope, code


def record_step_review(conn, req, run_id, step_id):
    """Keep the leader's union locks until every child Step has independent review."""
    item = binding_for_run(conn, run_id)
    if not item:
        return None
    body = item["body"]
    if (req.get("actor") != item["row"]["owner_actor"]
            and req.get("actor") != "main"):
        raise PmtError("ownership_conflict", "Only the group owner or PMT main may record child review", 3)
    member = next((value for value in body.get("members", [])
                   if value.get("run_id") == run_id and value.get("step_id") == step_id), None)
    if not member:
        raise PmtError("batch_child_mapping_invalid", "Reviewed Step is not a member of its group", 3)
    if body.get("handle_active"):
        raise PmtError("batch_handle_active", "A child Step cannot be reviewed while the grouped handle is live", 3)
    if body.get("status") not in {"review_pending", "reconciling"}:
        raise PmtError("batch_not_reviewable", "Batch has not collected a stopped per-Step result", 3)
    row = conn.execute("SELECT state FROM records WHERE id=? AND kind='step'", (step_id,)).fetchone()
    if not row or row["state"] != "Done":
        raise PmtError("batch_step_review_unconfirmed", "Step must be independently reviewed before releasing group ownership", 3)
    members = []
    for existing in body["members"]:
        updated = dict(existing)
        if updated["step_id"] == step_id:
            updated["business_state"] = "Done"
            updated["reviewed_event_ref"] = req.get("request_id")
        members.append(updated)
    all_done = all(member.get("business_state") == "Done" for member in members)
    status = ("complete" if all_done else
              "reconciling" if any(member.get("state") == "reconciling" for member in members)
              else "review_pending")
    next_body = {**body, "members": members, "status": status,
                 "scope_locks_retained": not all_done,
                 "completed_at": utc_now() if all_done else None}
    saved = _save_object(conn, item, batch_id=body["batch_id"], scope_id=body["scope_id"],
                         actor=item["row"]["owner_actor"], session=item["row"]["owner_session"], body=next_body,
                         state=status, request_id=req["request_id"])
    if all_done:
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (body["parent_run_ref"],))
    event_id = _batch_event_id({**req, "payload": {**req.get("payload", {}),
        "event_id": req.get("payload", {}).get("event_id", req["request_id"])}},
        "child-step-reviewed:" + step_id)
    from ..lifecycle import _event
    _event(conn, req, event_id=event_id, event_type="batch.step_result_recorded",
           scope_id=body["scope_id"], record_id=step_id,
           payload={"batch_ref": body["batch_id"], "step_id": step_id,
                    "state": "Done", "all_children_done": all_done,
                    "scope_locks_retained": not all_done})
    return {"batch_ref": saved["batch_ref"], "all_children_done": all_done,
            "scope_locks_retained": not all_done}


def cancel_unstarted_group(conn, req, run):
    """Cancel a prepared group only when durable state proves no runner dispatch occurred."""
    from ..execution import service as execution
    group = binding_for_run(conn, run["id"], require_leader=True)
    if not group:
        return None
    body, row = group["body"], group["row"]
    if body.get("status") not in {"contexts_building", "prepared"} or body.get("handle_ref"):
        return None
    journal_id = str(uuid.uuid5(uuid.UUID(run["id"]), "pmt-runner-dispatch"))
    if run.get("handle_json") or conn.execute("SELECT 1 FROM operation_journal WHERE id=?", (journal_id,)).fetchone():
        return None
    members = []
    for member in body["members"]:
        current = execution._get_run(conn, member["run_id"])
        if (current["owner_session"] != req["session_id"]
                or current["state"] not in {"starting", "running"}
                or current.get("handle_json") is not None):
            raise PmtError("batch_cancel_unknown", "A child may have started; group cancellation requires reconciliation", 3)
        transition_req = {**req, "payload": {**req.get("payload", {}),
            "event_id": str(uuid.uuid5(uuid.UUID(req.get("payload", {}).get("event_id") or req["request_id"]),
                                       "batch-unstarted-cancel:" + current["id"]))}}
        execution._transition(conn, transition_req, current, "canceled", stop_confirmed=1,
                               completed_at=utc_now(), keep_locks=True)
        conn.execute("UPDATE execution_jobs SET state='canceled',updated_at=? WHERE id=?",
                     (utc_now(), current["job_id"]))
        members.append(dict(member, state="canceled", result_status="not_started"))
    next_body = {**body, "members": members, "status": "canceled", "handle_active": False,
                 "cancel_reason": "no_runner_dispatch_or_handle_recorded",
                 "scope_locks_retained": False, "completed_at": utc_now()}
    saved = _save_object(conn, group, batch_id=body["batch_id"], scope_id=body["scope_id"],
                         actor=row["owner_actor"], session=row["owner_session"], body=next_body,
                         state="canceled", request_id=req["request_id"])
    conn.execute("DELETE FROM scope_locks WHERE run_id=?", (body["parent_run_ref"],))
    from ..lifecycle import _event
    _event(conn, req, event_id=str(uuid.uuid5(uuid.UUID(req.get("payload", {}).get("event_id") or req["request_id"]),
                                               "batch-group-canceled-before-dispatch")),
           event_type="batch.step_result_recorded", scope_id=body["scope_id"],
           record_id=run["step_id"], payload={"batch_ref": body["batch_id"],
           "state": "canceled", "reason_code": "no_runner_dispatch_or_handle_recorded",
           "member_count": len(members), "scope_locks_retained": False})
    return {"batch_ref": saved["batch_ref"], "status": "canceled", "physical_slots": 0,
            "member_count": len(members), "scope_locks_retained": False}


def _report_members(binding, report):
    if (not isinstance(report, dict) or set(report) != {"schema", "batch_id", "steps"}
            or report.get("schema") != "pmt-batch-report-v1"
            or report.get("batch_id") != binding.get("batch_id")
            or not isinstance(report.get("steps"), list)):
        return {}, ["batch_report_schema_or_id_invalid"]
    members = {item["step_id"]: item for item in binding["members"]}
    out, errors = {}, []
    for index, item in enumerate(report["steps"]):
        fields = {"step_id", "run_id", "directive_sha256", "context_ref", "summary", "choices",
                  "criteria_results", "tests", "evidence_refs", "unresolved_items"}
        if not isinstance(item, dict) or set(item) != fields:
            errors.append(f"step_result_shape_invalid:{index}")
            continue
        step_id = item.get("step_id")
        member = members.get(step_id)
        if member is None or step_id in out:
            errors.append("step_result_unknown_or_duplicate")
            continue
        if (item.get("run_id") != member.get("run_id")
                or item.get("directive_sha256") != member.get("directive_sha256")
                or item.get("context_ref") != member.get("context_ref")):
            errors.append(f"step_binding_mismatch:{step_id}")
            continue
        if (not isinstance(item.get("summary"), str)
                or not all(isinstance(item.get(key), list)
                           for key in ("choices", "criteria_results", "tests", "evidence_refs", "unresolved_items"))):
            errors.append(f"step_result_fields_invalid:{step_id}")
            continue
        expected = {entry["id"] for entry in member.get("criteria", [])}
        rows, criterion_ids = [], []
        invalid = False
        for row in item["criteria_results"]:
            if not isinstance(row, dict) or set(row) != {"criterion_id", "outcome", "reason", "evidence_refs"}:
                invalid = True
                break
            if (not isinstance(row["criterion_id"], str)
                    or row["outcome"] not in {"pass", "fail", "blocked", "not_run"}
                    or not isinstance(row["evidence_refs"], list)
                    or any(not isinstance(ref, str) or not ref for ref in row["evidence_refs"])
                    or (row["outcome"] in {"pass", "fail"} and not row["evidence_refs"])
                    or (row["outcome"] in {"blocked", "not_run"}
                        and (not isinstance(row["reason"], str) or not row["reason"].strip()))):
                invalid = True
                break
            criterion_ids.append(row["criterion_id"])
            rows.append({"criterion_id": row["criterion_id"], "outcome": row["outcome"],
                         "reason": row["reason"], "evidence_refs": list(row["evidence_refs"])})
        if invalid or len(criterion_ids) != len(set(criterion_ids)) or set(criterion_ids) != expected:
            errors.append(f"step_criteria_mapping_invalid:{step_id}")
            continue
        if any(not isinstance(ref, str) or not ref for ref in item["evidence_refs"]):
            errors.append(f"step_evidence_refs_invalid:{step_id}")
            continue
        out[step_id] = {**item, "criteria_results": rows}
    return out, errors


def _batch_for_parent(conn, parent_run_id):
    item = binding_for_run(conn, parent_run_id, require_leader=True)
    return item


def _collect_step_batch(db, req):
    from ..resources import check_artifact
    from ..phase2_common import load_json_resource
    from ..service import execute as service_execute
    from ..execution import service as execution
    p = req["payload"]
    if set(p) != {"batch_ref", "parent_run_id", "expected_run_revision", "event_id"}:
        raise PmtError("batch_input_invalid", "collect_step_batch requires the bound parent run and event identity")
    batch_ref = _uuid(p["batch_ref"], "batch_ref")
    parent_run_id = _uuid(p["parent_run_id"], "parent_run_id")
    event_id = _uuid(p["event_id"], "event_id")
    if event_id == req["request_id"]:
        raise PmtError("batch_input_invalid", "request_id and event_id are distinct identities")
    with closing(db.connect()) as conn:
        binding = _object(conn, req["scope_id"], batch_ref)
        if not binding or binding["body"].get("parent_run_ref") != parent_run_id:
            raise PmtError("batch_binding_not_found", "Batch binding does not match its parent run", 3)
        body = binding["body"]
        parent = execution._get_run(conn, parent_run_id)
        if (binding["row"]["owner_actor"], binding["row"]["owner_session"]) != (
                req["actor"], req["session_id"]):
            raise PmtError("ownership_conflict", "Batch binding belongs to another owner", 3)
        if type(p["expected_run_revision"]) is not int or p["expected_run_revision"] != parent["revision"]:
            raise PmtError("revision_conflict", "Parent run changed before child result collection", 3)
        if parent["state"] not in {"review_pending", "reconciling", "cancel_requested", "canceled"} \
                or not parent["stop_confirmed"]:
            raise PmtError("batch_parent_not_stopped", "Group results require an actual stopped parent receipt", 3)
        try:
            parent_result = json.loads(parent["result_json"]) if parent["result_json"] else None
        except (TypeError, ValueError, json.JSONDecodeError):
            parent_result = None
        report_ref = parent_result.get("batch_report_ref") if isinstance(parent_result, dict) else None
        report_hash = parent_result.get("batch_report_sha256") if isinstance(parent_result, dict) else None
        accepted_report_ref = body.get("runner_report_ref")
        accepted_report_hash = body.get("parent_container_result", {}).get("report_sha256") \
            if isinstance(body.get("parent_container_result"), dict) else None
        if accepted_report_ref and (report_ref != accepted_report_ref or report_hash != accepted_report_hash):
            raise PmtError("batch_report_conflict", "A collected group report cannot be replaced by another report", 3)
        report_row = conn.execute("SELECT scope_id,sha256,state FROM artifacts WHERE id=?", (report_ref,)).fetchone() \
            if isinstance(report_ref, str) else None
        checked = check_artifact(db, conn, report_ref) if report_row else {"valid": False}
        report_owner = conn.execute("SELECT 1 FROM artifact_refs WHERE artifact_id=? AND owner_id=? LIMIT 1",
                                     (report_ref, parent_run_id)).fetchone() if report_row else None
        runner_observation = parent_result.get("runner_observation", {}) if isinstance(parent_result, dict) else {}
        parent_runner_fixture = runner_observation.get("fixture") if isinstance(runner_observation, dict) else None
        exit_code = runner_observation.get("exit_code") if type(runner_observation.get("exit_code")) is int else None
        stop_receipt = parent_result.get("receipt_ref") if isinstance(parent_result, dict) else None
        if not isinstance(stop_receipt, str) and parent.get("intent_json"):
            try:
                reconcile_evidence = json.loads(parent["intent_json"]).get("reconciliation", {}).get("evidence_refs", [])
                stop_receipt = reconcile_evidence[0] if reconcile_evidence else None
            except (TypeError, ValueError, json.JSONDecodeError, IndexError):
                stop_receipt = None
        handle_ref = body.get("handle_ref")
        if not isinstance(handle_ref, str):
            raise PmtError("batch_handle_unverified", "Stopped group has no acknowledged parent handle", 3)
        report = None
        report_by_step, report_errors = {}, ["batch_report_missing"]
        if (report_row and report_row["scope_id"] == req["scope_id"] and report_row["state"] == "ready"
                and checked.get("valid") and checked.get("sha256") == report_hash and report_owner):
            report = load_json_resource(db, conn, report_ref)
            report_by_step, report_errors = _report_members(body, report)
        original_binding_revision = binding["row"]["revision"]
        members_snapshot = [dict(member) for member in body["members"]]
        parent_snapshot = dict(parent)
    if report_errors:
        report_by_step = {}

    # F7 publishes a private child-specific observation before P2 result rows are changed.
    f7_refs, f7_errors = {}, []
    for member in members_snapshot:
        step_id = member["step_id"]
        child_report = report_by_step.get(step_id)
        if child_report is None:
            f7_errors.append(step_id)
            continue
        child_id = member["run_id"]
        child_output = {"schema": "pmt-batch-child-observation-v1", "batch_id": batch_ref,
                        "step_id": step_id, "run_id": child_id,
                        "directive_sha256": member["directive_sha256"],
                        "context_ref": member["context_ref"], "runner_report": child_report,
                        "parent_receipt_ref": stop_receipt, "parent_exit_code": exit_code}
        result_id = str(uuid.uuid5(uuid.UUID(batch_ref), "f7-child-result:" + child_id))
        f7_request = {**req, "request_id": str(uuid.uuid5(uuid.UUID(batch_ref), "f7-child-request:" + child_id)),
                      "operation": "compact_tool_result",
                      "payload": {"task_id": member["task_id"], "run_id": child_id,
                          "step_id": step_id, "status": "succeeded" if exit_code == 0 else "failed",
                          "exit_code": exit_code, "format": "json", "output": canonical_json(child_output),
                          "result_id": result_id,
                          "criteria_claims": child_report["criteria_results"]}}
        f7_response, f7_code = service_execute(db, f7_request)
        if f7_code or not isinstance(f7_response, dict) or not f7_response.get("ok"):
            f7_errors.append(step_id)
            continue
        f7_result = f7_response.get("result") or {}
        evidence = f7_result.get("evidence_ref")
        if (f7_result.get("result_id") != result_id or f7_result.get("run_id") != child_id
                or not isinstance(evidence, dict) or not isinstance(evidence.get("id"), str)
                or not isinstance(evidence.get("sha256"), str)):
            f7_errors.append(step_id)
            continue
        f7_refs[step_id] = {"result_id": result_id, "evidence_ref": evidence,
                            "source_sha256": evidence["sha256"],
                            "criteria_verdict": f7_result.get("criteria_verdict")}

    # Revalidate evidence IDs used by reported criteria outside the P2 state transaction.
    valid_reports, report_evidence_errors = {}, []
    with closing(db.connect()) as conn:
        for member in members_snapshot:
            child_report = report_by_step.get(member["step_id"])
            if child_report is None or member["step_id"] not in f7_refs:
                continue
            all_refs = set(child_report["evidence_refs"])
            for criterion in child_report["criteria_results"]:
                all_refs.update(criterion["evidence_refs"])
            invalid_refs = []
            for ref in all_refs:
                try:
                    canonical = str(uuid.UUID(ref)) == ref
                except (ValueError, TypeError, AttributeError):
                    canonical = False
                artifact = conn.execute("SELECT scope_id,state FROM artifacts WHERE id=?", (ref,)).fetchone() if canonical else None
                artifact_check = check_artifact(db, conn, ref) if artifact else {"valid": False}
                if not artifact or artifact["scope_id"] != req["scope_id"] or artifact["state"] != "ready" \
                        or not artifact_check.get("valid"):
                    invalid_refs.append(ref)
            if invalid_refs:
                report_evidence_errors.append(member["step_id"])
            else:
                valid_reports[member["step_id"]] = child_report

    # Bind each logical Step's own validated report and F7 receipt to its P2 run.
    from ..lifecycle import _event
    internal = {**req, "request_id": str(uuid.uuid5(uuid.UUID(req["request_id"]), "pmt-batch-collect")),
                "operation": "collect_step_batch"}

    def commit(conn, request):
        binding = _object(conn, req["scope_id"], batch_ref)
        if not binding or binding["row"]["revision"] != original_binding_revision:
            raise PmtError("batch_revision_conflict", "Batch binding changed while results were prepared", 3)
        body = binding["body"]
        current_parent = execution._get_run(conn, parent_run_id)
        if (current_parent["revision"] != p["expected_run_revision"]
                or current_parent["state"] not in {"review_pending", "reconciling", "cancel_requested", "canceled"}
                or not current_parent["stop_confirmed"]):
            raise PmtError("batch_parent_changed", "Stopped parent receipt changed before child commit", 3)
        next_members = []
        for member in body["members"]:
            step_id, child_id = member["step_id"], member["run_id"]
            child = execution._get_run(conn, child_id)
            if child["owner_session"] != req["session_id"] or child["step_id"] != step_id:
                raise PmtError("batch_child_binding_invalid", "Child run/Step owner binding changed", 3)
            if child["result_json"]:
                saved_result = json.loads(child["result_json"])
                if member.get("f7_result_ref") and saved_result.get("receipt_ref") == member["f7_result_ref"]:
                    next_members.append(member)
                    continue
                if not (child_id == parent_run_id
                        and saved_result.get("batch_ref") == batch_ref
                        and saved_result.get("batch_report_ref") == report_ref
                        and saved_result.get("batch_report_sha256") == report_hash):
                    raise PmtError("execution_record_conflict", "Child already has a different result", 3)
            child_report = valid_reports.get(step_id)
            child_f7 = f7_refs.get(step_id)
            member_next = dict(member)
            if not child_report or not child_f7 or step_id in report_evidence_errors:
                if current_parent["state"] == "canceled":
                    if child["state"] in {"running", "starting", "cancel_requested", "reconciling"}:
                        transition_req = {**request, "payload": {**request["payload"],
                            "event_id": str(uuid.uuid5(uuid.UUID(event_id), "batch-child-cancel:" + child_id))}}
                        execution._transition(conn, transition_req, child, "canceled", stop_confirmed=1,
                            completed_at=utc_now(), intent={**json.loads(child["intent_json"]),
                            "batch_cancel_confirmed": {"batch_ref": batch_ref,
                                "parent_receipt_ref": stop_receipt}}, keep_locks=True)
                        conn.execute("UPDATE execution_jobs SET state='canceled',updated_at=? WHERE id=?",
                                     (utc_now(), child["job_id"]))
                    member_next.update({"state": "canceled", "result_status": "canceled",
                                        "reason_code": "group_handle_cancelled_without_child_result"})
                    next_members.append(member_next)
                    continue
                if child["state"] in {"running", "starting", "cancel_requested"}:
                    intent = json.loads(child["intent_json"])
                    intent["batch_result_unknown"] = {"batch_ref": batch_ref,
                                                       "reason_code": "child_result_missing_or_invalid"}
                    transition_req = {**request, "payload": {**request["payload"],
                        "event_id": str(uuid.uuid5(uuid.UUID(event_id), "batch-child-unknown:" + child_id))}}
                    execution._transition(conn, transition_req, child, "reconciling", intent=intent, keep_locks=True)
                    conn.execute("UPDATE execution_jobs SET state='reconciling',updated_at=? WHERE id=?",
                                 (utc_now(), child["job_id"]))
                member_next.update({"state": "reconciling", "result_status": "unknown",
                                    "reason_code": "child_result_missing_or_invalid"})
                next_members.append(member_next)
                continue
            result_body = {"directive_version": child["directive_version"],
                "actual_route": json.loads(child["route_json"]),
                "summary": child_report["summary"],
                "choices": child_report["choices"], "criteria_results": child_report["criteria_results"],
                "tests": child_report["tests"], "evidence_refs": [child_f7["evidence_ref"]["id"]],
                "receipt_ref": child_f7["evidence_ref"]["id"],
                "stop_confirmed": True,
                "stop_evidence_refs": [stop_receipt or child_f7["evidence_ref"]["id"]],
                "runner_observation": {"exit_code": exit_code, "fixture": parent_runner_fixture},
                "batch_ref": batch_ref, "batch_report_ref": report_ref,
                "batch_report_sha256": report_hash,
                "batch_child_f7_ref": child_f7["evidence_ref"]}
            if child_id == parent_run_id:
                # Keep the exact physical stop receipt and immutable container result traceable
                # after the representative run is materialized as its own logical Step result.
                result_body["physical_parent_receipt_ref"] = stop_receipt
            revision = child["revision"]
            child_request = {**request, "request_id": str(uuid.uuid5(uuid.UUID(batch_ref),
                "p2-child-result:" + child_id)), "operation": "submit_execution_result",
                "payload": {"event_id": str(uuid.uuid5(uuid.UUID(event_id), "batch-child-result:" + child_id)),
                            "run_id": child_id, "expected_run_revision": revision,
                            "result": result_body}}
            # P2 state/result/event remain authoritative; run this inside the same DB transaction.
            if child_id == parent_run_id:
                execution.materialize_batch_leader_result(conn, child_request, batch_ref=batch_ref,
                    report_ref=report_ref, report_sha256=report_hash, result=result_body)
            else:
                execution.handle(db, conn, child_request)
            member_next.update({"state": "review_pending", "result_status": "reported",
                                "receipt_ref": child_f7["evidence_ref"],
                                "f7_result_ref": child_f7["evidence_ref"]["id"],
                                "result_ref": {"run_id": child_id,
                                               "receipt_ref": child_f7["evidence_ref"]["id"],
                                               "source_sha256": child_f7["source_sha256"]},
                                "criteria_claim_count": len(child_report["criteria_results"])})
            next_members.append(member_next)
        missing_ids = {item["run_id"] for item in next_members if item.get("state") == "reconciling"}
        all_stopped_results = not missing_ids and all(item.get("state") == "review_pending" for item in next_members)
        canceled = current_parent["state"] == "canceled"
        all_canceled = canceled and all(item.get("state") == "canceled" for item in next_members)
        status = "canceled" if all_canceled else ("review_pending" if all_stopped_results else "reconciling")
        next_body = {**body, "members": next_members, "status": status,
                     "handle_active": False, "runner_report_ref": report_ref,
                     "receipt_ref": stop_receipt, "collect_request_id": req["request_id"],
                     "parent_container_result": {
                         "sha256": fingerprint(json.loads(parent_snapshot["result_json"])),
                         "report_ref": report_ref, "report_sha256": report_hash,
                         "physical_receipt_ref": stop_receipt},
                     "scope_locks_retained": True,
                     "result_errors": sorted(set(f7_errors + report_evidence_errors))}
        saved = _save_object(conn, binding, batch_id=batch_ref, scope_id=req["scope_id"],
                             actor=req["actor"], session=req["session_id"], body=next_body,
                             state=status, request_id=request["request_id"])
        if all_canceled:
            conn.execute("DELETE FROM scope_locks WHERE run_id=?", (parent_run_id,))
        _event(conn, request, event_id=event_id, event_type="batch.step_result_recorded",
               scope_id=req["scope_id"], record_id=current_parent["step_id"],
               payload={"batch_ref": batch_ref, "parent_run_ref": parent_run_id,
                        "result_count": len(f7_refs), "unknown_count": len(missing_ids),
                        "scope_locks_retained": True})
        return {"batch_ref": saved["batch_ref"], "status": status,
                "parent_run_ref": parent_run_id,
                "children": [{"step_id": item["step_id"], "run_id": item["run_id"],
                              "state": item.get("state", "unknown"),
                              "receipt_ref": item.get("receipt_ref"),
                              "reason_code": item.get("reason_code")}
                             for item in next_members],
                "scope_locks_retained": True, "parent_done": False,
                "physical_slots": 0}
    envelope, code = db.run_request(internal, commit)
    if code or not envelope.get("ok"):
        error = envelope.get("error") or {}
        raise PmtError(error.get("code", "batch_collect_failed"),
                       "Child result collection did not commit", code or 3,
                       bool(error.get("retryable")), error.get("details"))
    return envelope["result"]


def prepare_step_batch(db, req):
    return _prepare_step_batch(db, req)


def bind_step_batch(db, req):
    return _bind_step_batch(db, req)


def collect_step_batch(db, req):
    result = _collect_step_batch(db, req)
    envelope, code = db.run_request(req, lambda conn, request: result)
    return envelope, code


def _exact_request_replay(db, req):
    normalized = db._normalize_request(req)
    ignored = {"request_id", "correlation_id", "received_at", "received_at_utc", "retry_count", "attempt"}
    semantic = {key: value for key, value in normalized.items() if key not in ignored}
    request_hash = fingerprint(semantic)
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT request_fingerprint,response_json,exit_code,actor,session_id "
                           "FROM requests WHERE request_id=?", (normalized["request_id"],)).fetchone()
    if not row:
        return None
    if (row["actor"], row["session_id"]) != (normalized["actor"], normalized["session_id"]):
        raise PmtError("request_owner_mismatch", "request result belongs to another actor or session", 3)
    if row["request_fingerprint"] != request_hash:
        raise PmtError("request_conflict", "request_id was already used for a different batch payload", 3)
    return json.loads(row["response_json"]), row["exit_code"]


def execute_file(db, req):
    from ..service import response
    request_id = req.get("request_id") if isinstance(req, dict) else None
    try:
        operation = req.get("operation")
        if operation not in FILE_OPERATIONS:
            raise PmtError("operation_unavailable", "F9 operation is unavailable")
        prior = _exact_request_replay(db, req)
        if prior is not None:
            return prior
        if operation == "prepare_step_batch":
            return _prepare_step_batch(db, req)
        if operation == "bind_step_batch":
            return _bind_step_batch(db, req)
        return collect_step_batch(db, req)
    except PmtError as exc:
        return response(request_id, error=exc.as_dict()), exc.exit_code
    except OSError as exc:
        error = PmtError("batch_io_error", "F9 could not verify a required local artifact", 4, True,
                         {"errno": getattr(exc, "errno", None)})
        return response(request_id, error=error.as_dict()), error.exit_code
    except Exception as exc:
        import traceback
        frame = traceback.extract_tb(exc.__traceback__)[-1] if exc.__traceback__ else None
        error = PmtError("batch_internal_error", "F9 operation failed unexpectedly", 5,
                         details={"exception_type": type(exc).__name__,
                                  "missing_module": getattr(exc, "name", None),
                                  "exception_message": str(exc)[:160],
                                  "location": f"{frame.name}:{frame.lineno}" if frame else None})
        return response(request_id, error=error.as_dict()), error.exit_code
