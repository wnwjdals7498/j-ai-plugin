"""F6 reuse-key rules and scoped SQLite adapters."""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from contextlib import closing
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..errors import PmtError
from ..phase2_common import event, project_scope_id, require_workspace_claim, validate_scope
from ..util import canonical_json, fingerprint, new_id, utc_now
from .storage import Phase3Storage

READ_OPERATIONS = {"read_reuse_decision"}
WRITE_OPERATIONS = set()
FILE_OPERATIONS = {"resolve_reuse", "record_reuse_result", "invalidate_reuse"}

KEY_SCHEMA_VERSION = 1
KEY_SCHEMA_ID = "pmt-reuse-key-v1"
_DIMENSIONS = {"input", "environment", "source", "baseline", "tool", "dependencies", "project", "model"}
_IMPACT_ONLY_DIMENSIONS = {"target", "scope", "document_coverage", "segment_coverage", "dynamic_dependency"}
_HASH = re.compile(r"^[0-9a-f]{64}$")
_SCOPE_LEVELS = {"environment", "repository", "project"}
_ACTIVE_RUN_STATES = {"queued", "starting", "running", "review_pending", "reconciling", "cancel_requested"}


def _invalid(message: str, code: str = "reuse_input_invalid") -> PmtError:
    return PmtError(code, message, 2, False)


def _sha(value: Any) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _ref(value: Any, label: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\n" in value or "\r" in value:
        raise _invalid(f"{label} must be a short stable reference")
    return value


def _canonical_uuid(value: Any, label: str) -> str:
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise _invalid(f"{label} must be a canonical UUID") from exc
    return value


def _not_applicable(value: Any) -> bool:
    return (isinstance(value, Mapping) and value.get("state") == "not_applicable"
            and set(value) == {"state", "reason_code"}
            and isinstance(value.get("reason_code"), str) and bool(value["reason_code"])
            and len(value["reason_code"]) <= 128 and "\n" not in value["reason_code"])


def _dimension_value(name: str, value: Any) -> tuple[Any | None, str | None]:
    if value is None or (isinstance(value, Mapping) and value.get("state") == "unknown"):
        reason = value.get("reason_code", "dimension_unknown") if isinstance(value, Mapping) else "dimension_missing"
        return None, f"{name}:{reason}"
    if _not_applicable(value):
        if name in {"input", "model"}:
            return None, f"{name}:not_applicable_is_not_permitted"
        return {"state": "not_applicable", "reason_code": value["reason_code"]}, None
    if name in {"input", "environment", "source", "baseline", "dependencies"}:
        if not isinstance(value, Mapping) or set(value) != {"state", "sha256"}:
            return None, f"{name}:fingerprint_missing"
        if value.get("state") != "known" or not _sha(value.get("sha256")):
            return None, f"{name}:fingerprint_unknown"
        return {"state": "known", "sha256": value["sha256"]}, None
    if name == "tool":
        required = {"tool_id", "version", "capability_sha256"}
        if (not isinstance(value, Mapping) or set(value) != required
                or not _sha(value.get("capability_sha256"))):
            return None, "tool:version_or_capability_unknown"
        try:
            return {"tool_id": _ref(value["tool_id"], "tool_id"),
                    "version": _ref(value["version"], "tool.version"),
                    "capability_sha256": value["capability_sha256"]}, None
        except PmtError:
            return None, "tool:version_or_capability_unknown"
    if name == "project":
        if (not isinstance(value, Mapping) or set(value) != {"state", "id"}
                or value.get("state") != "known" or not isinstance(value.get("id"), str) or not value["id"]):
            return None, "project:identity_unknown"
        return {"state": "known", "id": value["id"]}, None
    if name == "model":
        required = {"provider", "model", "version"}
        if (not isinstance(value, Mapping) or set(value) != required
                or not all(isinstance(value.get(k), str) and value[k] for k in required)):
            return None, "model:identity_unknown"
        try:
            return {key: _ref(value[key], f"model.{key}") for key in sorted(required)}, None
        except PmtError:
            return None, "model:identity_unknown"
    return None, f"{name}:unsupported_dimension"


def _definition(definition: Any) -> tuple[dict[str, Any] | None, list[str]]:
    if not isinstance(definition, Mapping):
        raise _invalid("definition must be an object")
    if set(definition) - {"definition_id", "definition_version", "meaning_sha256",
                          "key_schema_version", "required_dimensions", "model_is_subject",
                          "applicability", "selectors", "target_selector"}:
        raise _invalid("definition contains unsupported fields")
    definition_id = _ref(definition.get("definition_id"), "definition_id")
    definition_version = _ref(definition.get("definition_version"), "definition_version", 128)
    meaning_sha256 = definition.get("meaning_sha256")
    if not _sha(meaning_sha256):
        raise _invalid("meaning_sha256 must pin the canonical definition semantics")
    version = definition.get("key_schema_version")
    if type(version) is not int or version < 1:
        raise _invalid("key_schema_version must be a positive integer")
    if version != KEY_SCHEMA_VERSION:
        return None, [f"unsupported_key_schema:{version}"]
    model_is_subject = definition.get("model_is_subject")
    if type(model_is_subject) is not bool:
        raise _invalid("model_is_subject must be explicit")
    required = definition.get("required_dimensions")
    if (not isinstance(required, list) or any(not isinstance(dim, str) for dim in required)
            or len(required) != len(set(required)) or any(dim not in _DIMENSIONS for dim in required)):
        raise _invalid("required_dimensions must be a unique array of supported dimensions")
    if not {"input"}.issubset(required):
        raise _invalid("input is always required for a reusable investigation or verification")
    if model_is_subject and "model" not in required:
        raise _invalid("a model that is itself under test must be a required key dimension")
    if not model_is_subject and "model" in required:
        raise _invalid("model provenance is not a key dimension unless model_is_subject is true")
    selectors = definition.get("selectors", {})
    if not isinstance(selectors, Mapping) or set(selectors) - set(required):
        raise _invalid("selectors must map required dimensions only")
    normalized_selectors = {}
    for dimension, selector in selectors.items():
        if not isinstance(selector, Mapping) or type(selector.get("version")) is not int or selector["version"] != 1:
            raise _invalid(f"{dimension} selector must use version 1")
        kind = selector.get("kind")
        if dimension == "input" and selector == {"version": 1, "kind": "snapshot_inputs"}:
            normalized_selectors[dimension] = dict(selector)
        elif dimension == "environment" and selector == {"version": 1, "kind": "environment_id"}:
            normalized_selectors[dimension] = dict(selector)
        elif dimension == "source" and kind == "workspace_files" and set(selector) == {"version", "kind", "paths"}:
            paths = selector.get("paths")
            if (not isinstance(paths, list) or not paths or len(paths) != len(set(paths))
                    or any(not isinstance(path, str) or not path or Path(path).is_absolute()
                           or ".." in Path(path).parts for path in paths)):
                raise _invalid("source selector paths must be unique workspace-relative paths")
            normalized_selectors[dimension] = {"version": 1, "kind": kind, "paths": sorted(paths)}
        elif dimension == "tool" and kind == "runtime_fields" and set(selector) == {"version", "kind", "fields"}:
            fields = selector.get("fields")
            if (not isinstance(fields, list) or not fields or len(fields) != len(set(fields))
                    or any(field not in {"os", "architecture", "python", "sqlite", "packages"} for field in fields)):
                raise _invalid("tool selector fields must name known runtime fields")
            normalized_selectors[dimension] = {"version": 1, "kind": kind, "fields": sorted(fields)}
        elif dimension == "dependencies" and kind == "dependency_manifests" and set(selector) == {"version", "kind", "names"}:
            names = selector.get("names")
            if (not isinstance(names, list) or not names or len(names) != len(set(names))
                    or any(not isinstance(name, str) or not name for name in names)):
                raise _invalid("dependency selector names must be nonempty and unique")
            normalized_selectors[dimension] = {"version": 1, "kind": kind, "names": sorted(names)}
        elif dimension == "baseline" and kind == "criteria" and set(selector) == {"version", "kind", "ids"}:
            ids = selector.get("ids")
            if (not isinstance(ids, list) or not ids or len(ids) != len(set(ids))
                    or any(not isinstance(value, str) or not value for value in ids)):
                raise _invalid("baseline selector criterion ids must be nonempty and unique")
            normalized_selectors[dimension] = {"version": 1, "kind": kind, "ids": sorted(ids)}
        elif dimension in {"source", "baseline"} and kind == "source_pin_fields" and set(selector) == {"version", "kind", "fields"}:
            fields = selector.get("fields")
            allowed_pin_fields = {"repository_id", "project_id", "selected_ref", "reviewed_commit",
                                  "graph_schema", "graph_revision", "graph_hash", "dirty_state",
                                  "dirty_fingerprint", "source_kind", "source_hash"}
            if (not isinstance(fields, list) or not fields or len(fields) != len(set(fields))
                    or any(field not in allowed_pin_fields for field in fields)
                    or not {"graph_schema", "graph_revision", "graph_hash"}.issubset(fields)):
                raise _invalid("source pin selectors require graph schema/revision/hash fields")
            normalized_selectors[dimension] = {"version": 1, "kind": kind, "fields": sorted(fields)}
        elif dimension == "model" and kind == "verification_model" and set(selector) == {"version", "kind"}:
            normalized_selectors[dimension] = {"version": 1, "kind": kind}
        else:
            raise _invalid(f"unsupported {dimension} selector")
    target_selector = definition.get("target_selector", {"version": 1, "kind": "definition_semantics"})
    if not isinstance(target_selector, Mapping) or type(target_selector.get("version")) is not int \
            or target_selector.get("version") != 1:
        raise _invalid("target_selector must use version 1")
    if target_selector == {"version": 1, "kind": "definition_semantics"}:
        normalized_target_selector = dict(target_selector)
    elif target_selector.get("kind") == "record_fields" and set(target_selector) == {"version", "kind", "fields"}:
        fields = target_selector.get("fields")
        if (not isinstance(fields, list) or not fields or len(fields) != len(set(fields))
                or any(not isinstance(field, str) or not field or field in {"workspace", "criteria"}
                       or field.startswith("_") for field in fields)):
            raise _invalid("target record_fields selector must use explicit semantic body fields")
        normalized_target_selector = {"version": 1, "kind": "record_fields", "fields": sorted(fields)}
    elif target_selector == {"version": 1, "kind": "record_revision"}:
        normalized_target_selector = dict(target_selector)
    else:
        raise _invalid("unsupported target selector")
    applicability = definition.get("applicability")
    if not isinstance(applicability, Mapping):
        raise _invalid("applicability must state field-to-dimension dependencies")
    if set(applicability) - {"field_dimensions", "unknown_reason_dimensions"}:
        raise _invalid("applicability contains unsupported fields")
    if set(applicability) - {"field_dimensions", "unknown_reason_dimensions"}:
        raise _invalid("applicability contains unsupported fields")
    field_map = applicability.get("field_dimensions", {})
    if not isinstance(field_map, Mapping):
        raise _invalid("applicability.field_dimensions must be an object")
    normalized_map: dict[str, list[str]] = {}
    for field_name, dimensions in field_map.items():
        if not isinstance(field_name, str) or not field_name or not isinstance(dimensions, list):
            raise _invalid("field applicability entries must map field names to dimension arrays")
        if any(not isinstance(dim, str) or dim not in _DIMENSIONS | _IMPACT_ONLY_DIMENSIONS for dim in dimensions):
            raise _invalid(f"field applicability for {field_name} contains an unsupported dimension")
        normalized_map[field_name] = sorted(set(dimensions))
    unknown_map = applicability.get("unknown_reason_dimensions", {})
    if not isinstance(unknown_map, Mapping):
        raise _invalid("applicability.unknown_reason_dimensions must be an object")
    normalized_unknown: dict[str, list[str]] = {}
    for reason, dimensions in unknown_map.items():
        if not isinstance(reason, str) or not reason or not isinstance(dimensions, list):
            raise _invalid("unknown reason applicability must map reason codes to dimension arrays")
        if any(not isinstance(dim, str) or dim not in _DIMENSIONS | _IMPACT_ONLY_DIMENSIONS for dim in dimensions):
            raise _invalid(f"unknown reason {reason} contains an unsupported dimension")
        normalized_unknown[reason] = sorted(set(dimensions))
    record = {"definition_id": definition_id, "definition_version": definition_version,
              "meaning_sha256": meaning_sha256, "key_schema_version": version,
              "required_dimensions": sorted(required), "model_is_subject": model_is_subject,
              "selectors": normalized_selectors,
              "target_selector": normalized_target_selector,
              "applicability": {"field_dimensions": normalized_map,
                                "unknown_reason_dimensions": normalized_unknown}}
    return record, []


def build_reuse_key(definition: Mapping[str, Any], conditions: Mapping[str, Any]) -> dict[str, Any]:
    """Build a stable key from explicitly known, relevant condition dimensions."""
    normalized_definition, definition_reasons = _definition(definition)
    if definition_reasons:
        return {"key_schema": KEY_SCHEMA_ID, "key": None, "key_sha256": None,
                "status": "unknown", "unknown_dimensions": definition_reasons}
    if not isinstance(conditions, Mapping):
        raise _invalid("conditions must be an object")
    if set(conditions) - {"scope", "target", "dimensions", "model_provenance"}:
        raise _invalid("conditions contain unsupported fields")

    scope = conditions.get("scope")
    if (not isinstance(scope, Mapping) or scope.get("level") not in _SCOPE_LEVELS
            or not isinstance(scope.get("id"), str) or not scope["id"]):
        raise _invalid("scope must identify an environment, repository, or project")
    try:
        if str(uuid.UUID(scope["id"])) != scope["id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise _invalid("scope.id must be a canonical UUID") from exc
    project_dimension = scope.get("project_dimension")
    if project_dimension is None or (not _not_applicable(project_dimension)
                                     and not (isinstance(project_dimension, Mapping)
                                              and set(project_dimension) == {"state", "id"}
                                              and project_dimension.get("state") == "known"
                                              and isinstance(project_dimension.get("id"), str)
                                              and project_dimension["id"])):
        return {"key_schema": KEY_SCHEMA_ID, "key": None, "key_sha256": None, "status": "unknown",
                "unknown_dimensions": ["project:explicit_identity_or_not_applicable_required"]}
    if isinstance(project_dimension, Mapping) and project_dimension.get("state") == "known" and set(project_dimension) != {"state", "id"}:
        raise _invalid("project_dimension may contain only state and id")
    if isinstance(project_dimension, Mapping) and project_dimension.get("state") == "known":
        try:
            if str(uuid.UUID(project_dimension["id"])) != project_dimension["id"]:
                raise ValueError
        except (ValueError, TypeError, AttributeError) as exc:
            raise _invalid("project_dimension.id must be a canonical UUID") from exc
    if scope["level"] == "project":
        if not isinstance(project_dimension, Mapping) or project_dimension.get("id") != scope["id"]:
            raise _invalid("project scope must use its own project identity")
    elif _not_applicable(project_dimension) is False and project_dimension.get("id") == scope["id"]:
        # Repository/environment work must state a distinct project context when one applies.
        raise _invalid("project scope cannot be silently substituted for its parent scope")
    scope_key = {"level": scope["level"], "id": scope["id"],
                 "project_dimension": dict(project_dimension)}

    target = conditions.get("target")
    if (not isinstance(target, Mapping) or not all(isinstance(target.get(key), str) and target[key]
                                                   for key in ("kind", "id", "version"))):
        return {"key_schema": KEY_SCHEMA_ID, "key": None, "key_sha256": None, "status": "unknown",
                "unknown_dimensions": ["target:identity_or_version_unknown"]}
    try:
        if str(uuid.UUID(target["id"])) != target["id"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise _invalid("target.id must be a canonical UUID") from exc
    target_key = {key: target[key] for key in ("kind", "id", "version")}

    required = normalized_definition["required_dimensions"]
    conditions_dims = conditions.get("dimensions")
    if not isinstance(conditions_dims, Mapping):
        conditions_dims = {}
    unexpected_dimensions = set(conditions_dims) - set(required)
    if unexpected_dimensions:
        raise _invalid("condition dimensions are not declared by the key definition",
                       "reuse_dimension_undeclared")
    normalized_dims: dict[str, Any] = {}
    unknowns: list[str] = []
    for dim in required:
        raw = conditions.get("model_provenance") if dim == "model" else conditions_dims.get(dim)
        normalized, reason = _dimension_value(dim, raw)
        if reason:
            unknowns.append(reason)
        else:
            normalized_dims[dim] = normalized
    model_provenance = conditions.get("model_provenance")
    if model_provenance is not None:
        if not isinstance(model_provenance, Mapping) or set(model_provenance) - {"provider", "model", "version"}:
            raise _invalid("model_provenance contains unsupported fields")
        for name, value in model_provenance.items():
            _ref(value, f"model_provenance.{name}")
    if unknowns:
        return {"key_schema": KEY_SCHEMA_ID, "key": None, "key_sha256": None,
                "status": "unknown", "unknown_dimensions": sorted(unknowns)}

    body = {"key_schema": KEY_SCHEMA_ID, "definition": normalized_definition,
            "scope": scope_key, "target": target_key, "dimensions": normalized_dims}
    key_hash = fingerprint(body)
    provenance = {}
    if isinstance(model_provenance, Mapping):
        provenance["model"] = {key: model_provenance[key] for key in ("provider", "model", "version")
                               if isinstance(model_provenance.get(key), str)}
    return {"key_schema": KEY_SCHEMA_ID, "key": body, "key_sha256": key_hash,
            "status": "ready", "unknown_dimensions": [], "provenance": provenance}


def related_source_dimension(components: Mapping[str, Any], relevant_refs: list[str]) -> dict[str, Any]:
    """Fingerprint only verified source components used by this definition."""
    return _related_dimension("source", components, relevant_refs)


def related_baseline_dimension(components: Mapping[str, Any], relevant_refs: list[str]) -> dict[str, Any]:
    """Fingerprint only the baseline artifacts/criteria used by this definition."""
    return _related_dimension("baseline", components, relevant_refs)


def _related_dimension(kind: str, components: Mapping[str, Any],
                       relevant_refs: list[str]) -> dict[str, Any]:
    if not isinstance(components, Mapping) or not isinstance(relevant_refs, list):
        raise _invalid(f"{kind} components and relevant_refs have invalid types")
    if not relevant_refs or any(not isinstance(ref, str) or not ref.strip() or len(ref) > 256 for ref in relevant_refs):
        return {"state": "unknown", "reason_code": f"{kind}_reference_set_unknown"}
    if len(relevant_refs) != len(set(relevant_refs)):
        raise _invalid(f"{kind} relevant_refs must be unique")
    selected, missing = [], []
    for ref in sorted(relevant_refs):
        digest = components.get(ref)
        if not _sha(digest):
            missing.append(ref)
        else:
            selected.append({"ref": ref, "sha256": digest})
    if missing:
        return {"state": "unknown", "reason_code": f"{kind}_component_unknown",
                "missing_refs": missing}
    return {"state": "known", "sha256": fingerprint({"dimension_schema": f"related-{kind}-v1",
                                                       "components": selected})}


def _evidence_status(candidate: Mapping[str, Any], evidence_facts: Mapping[str, Any],
                     scope_id: str) -> tuple[str, list[str]]:
    refs = candidate.get("evidence_refs")
    if not isinstance(refs, list) or not refs:
        return "invalid", ["evidence_missing"]
    unknowns, invalids = [], []
    for ref in refs:
        if not isinstance(ref, Mapping) or not isinstance(ref.get("id"), str) or not _sha(ref.get("sha256")):
            invalids.append("evidence_reference_invalid")
            continue
        try:
            if str(uuid.UUID(ref["id"])) != ref["id"]:
                raise ValueError
        except (ValueError, TypeError, AttributeError):
            invalids.append("evidence_reference_invalid")
            continue
        fact = evidence_facts.get(ref["id"]) if isinstance(evidence_facts, Mapping) else None
        if not isinstance(fact, Mapping):
            unknowns.append(f"evidence_unavailable:{ref['id']}")
        elif fact.get("state") != "valid":
            state = fact.get("state")
            if state == "unknown":
                unknowns.append(f"evidence_unknown:{ref['id']}")
            else:
                invalids.append(f"evidence_{state or 'invalid'}:{ref['id']}")
        elif (fact.get("sha256") != ref["sha256"] or fact.get("scope_id") != scope_id
              or fact.get("accessible") is not True):
            invalids.append(f"evidence_hash_scope_or_access_mismatch:{ref['id']}")
    if invalids:
        return "invalid", sorted(invalids)
    if unknowns:
        return "unknown", sorted(unknowns)
    return "valid", []


def _candidate_decision(key: Mapping[str, Any], conditions: Mapping[str, Any],
                        candidate: Mapping[str, Any], evidence_facts: Mapping[str, Any]) -> dict[str, Any]:
    if candidate.get("key_schema") != KEY_SCHEMA_ID:
        return {"status": "invalid", "reason": "key_schema_mismatch"}
    saved_key = candidate.get("key")
    if (not isinstance(saved_key, Mapping) or candidate.get("key_sha256") != key.get("key_sha256")
            or fingerprint(saved_key) != candidate.get("key_sha256")):
        return {"status": "invalid", "reason": "stored_key_manifest_invalid"}
    if saved_key != key.get("key"):
        return {"status": "miss", "reason": "exact_key_mismatch"}
    scope_id = conditions["scope"]["id"]
    if candidate.get("scope_id") != scope_id:
        return {"status": "invalid", "reason": "scope_mismatch"}
    if candidate.get("accessible") is not True:
        return {"status": "unknown", "reason": "scope_access_unconfirmed"}
    run_ref = candidate.get("run_ref")
    origin_ref = candidate.get("origin_ref")
    if origin_ref is None and isinstance(run_ref, str):
        origin_ref = {"kind": "run", "id": run_ref}
    owner = candidate.get("owner")
    if (not isinstance(origin_ref, Mapping) or origin_ref.get("kind") not in {"run", "verification"}
            or not isinstance(origin_ref.get("id"), str)):
        return {"status": "invalid", "reason": "original_run_or_owner_missing"}
    try:
        if str(uuid.UUID(origin_ref["id"])) != origin_ref["id"]:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        return {"status": "invalid", "reason": "original_run_or_owner_missing"}
    if not isinstance(owner, Mapping):
        return {"status": "invalid", "reason": "original_run_or_owner_missing"}
    if candidate.get("status") == "active":
        if origin_ref.get("kind") != "run":
            return {"status": "invalid", "reason": "active_claim_requires_run_origin"}
        if candidate.get("run_state") not in _ACTIVE_RUN_STATES:
            return {"status": "invalid", "reason": "active_run_state_mismatch"}
        if not all(isinstance(owner.get(key), str) and owner[key] for key in ("actor", "session_id")):
            return {"status": "invalid", "reason": "active_owner_missing"}
        return {"status": "active", "reason": "exact_active_claim",
                "original_run_ref": origin_ref["id"], "owner": dict(owner), "scope_id": scope_id}
    if candidate.get("status") != "completed":
        return {"status": "miss", "reason": "candidate_not_completed"}
    receipt = candidate.get("receipt")
    if not isinstance(receipt, Mapping):
        return {"status": "invalid", "reason": "receipt_missing"}
    receipt_kind = receipt.get("kind")
    if receipt_kind == "verification":
        if receipt.get("outcome") != "pass" or receipt.get("state") != "valid":
            return {"status": "invalid", "reason": "verification_not_valid_pass"}
        if not isinstance(receipt.get("receipt_ref"), str) or not receipt["receipt_ref"]:
            return {"status": "invalid", "reason": "verification_receipt_ref_missing"}
        if origin_ref.get("kind") != "verification":
            return {"status": "invalid", "reason": "verification_origin_ref_required"}
    elif receipt_kind == "runner":
        if (receipt.get("state") != "succeeded" or receipt.get("stop_confirmed") is not True
                or not isinstance(receipt.get("receipt_ref"), str) or not receipt["receipt_ref"]):
            return {"status": "invalid", "reason": "runner_completion_receipt_unverified"}
        if origin_ref.get("kind") != "run":
            return {"status": "invalid", "reason": "runner_origin_ref_required"}
    else:
        return {"status": "invalid", "reason": "model_claim_or_unknown_receipt_kind"}
    later_failure = candidate.get("later_failure")
    if later_failure is True:
        return {"status": "invalid", "reason": "later_nonpass_or_failed_attempt"}
    if later_failure is not False:
        return {"status": "unknown", "reason": "later_failure_state_unknown"}
    evidence_status, evidence_reasons = _evidence_status(candidate, evidence_facts, scope_id)
    if evidence_status != "valid":
        return {"status": evidence_status, "reason": "evidence_not_applicable", "details": evidence_reasons}
    result = {"status": "reusable", "reason": "exact_key_valid_receipt_and_evidence",
            "origin_ref": dict(origin_ref), "receipt_ref": receipt["receipt_ref"],
            "evidence_refs": [{"id": ref["id"], "sha256": ref["sha256"]}
                              for ref in candidate["evidence_refs"]],
            "scope_id": scope_id}
    if origin_ref["kind"] == "run":
        result["original_run_ref"] = origin_ref["id"]
    return result


def resolve_reuse(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Pure lookup planner; no cache read, claim, or event is performed."""
    if not isinstance(payload, Mapping):
        raise _invalid("resolve payload must be an object")
    request_id = _canonical_uuid(payload.get("request_id"), "request_id")
    event_id = _canonical_uuid(payload.get("event_id"), "event_id")
    if request_id == event_id:
        raise _invalid("request_id and event_id are distinct identities")
    definition, conditions = payload.get("definition"), payload.get("conditions")
    key_result = build_reuse_key(definition, conditions)
    if key_result["status"] != "ready":
        return {"status": "unknown", "reusable": False, "reason": "required_key_dimension_unknown",
                "unknown_dimensions": key_result["unknown_dimensions"], "request_id": request_id,
                "event_id": event_id, "key_sha256": None}
    impact = payload.get("impact_set")
    if impact is not None:
        impact_result = impact_invalidates(definition, impact, key_result["key"])
        if impact_result["status"] != "unaffected":
            return {"status": impact_result["status"], "reusable": False,
                    "reason": impact_result["reason"], "details": impact_result,
                    "request_id": request_id, "event_id": event_id,
                    "key_sha256": key_result["key_sha256"]}
    candidates = payload.get("candidates", [])
    evidence_facts = payload.get("evidence_facts", {})
    if not isinstance(candidates, list):
        raise _invalid("candidates must be an array")
    decisions = [_candidate_decision(key_result, conditions, candidate, evidence_facts)
                 if isinstance(candidate, Mapping)
                 else {"status": "invalid", "reason": "candidate_manifest_invalid"}
                 for candidate in candidates]
    reusable = next((item for item in decisions if item["status"] == "reusable"), None)
    active = next((item for item in decisions if item["status"] == "active"), None)
    selected = active or reusable
    if selected is None:
        selected = next((item for item in decisions if item["status"] in {"unknown", "invalid"}), None)
    selected = selected or {"status": "miss", "reason": "no_matching_candidate"}
    return {"status": selected["status"], "reusable": selected["status"] == "reusable",
            "reason": selected["reason"], "key_schema": KEY_SCHEMA_ID,
            "key": key_result["key"], "key_sha256": key_result["key_sha256"], "decision": selected,
            "provenance": key_result["provenance"], "request_id": request_id, "event_id": event_id}


def record_reuse_result(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build a body-free result manifest for a later authorized store adapter."""
    if not isinstance(payload, Mapping):
        raise _invalid("record payload must be an object")
    request_id = _canonical_uuid(payload.get("request_id"), "request_id")
    event_id = _canonical_uuid(payload.get("event_id"), "event_id")
    if request_id == event_id:
        raise _invalid("request_id and event_id are distinct identities")
    result = resolve_reuse(payload)
    if result["status"] != "reusable":
        raise _invalid("only a reusable exact result can be recorded as reusable", "reuse_result_not_applicable")
    decision = result["decision"]
    body = {"manifest_schema": 1, "key_schema": KEY_SCHEMA_ID,
            "key": result["key"], "key_sha256": result["key_sha256"],
            "scope_id": decision["scope_id"],
            "origin_ref": decision["origin_ref"],
            "original_run_ref": decision.get("original_run_ref"), "receipt_ref": decision["receipt_ref"],
            "evidence_refs": decision["evidence_refs"], "event_id": event_id,
            "provenance": result["provenance"]}
    return {"status": "ready_to_store", "request_id": request_id, "event_id": event_id,
            "manifest": body, "manifest_sha256": fingerprint(body)}


def impact_invalidates(definition: Mapping[str, Any], impact_set: Mapping[str, Any],
                       key: Mapping[str, Any]) -> dict[str, Any]:
    """Map F3 fields/unknowns only to declared reuse dimensions."""
    normalized, reasons = _definition(definition)
    if reasons or normalized is None:
        return {"status": "unknown", "invalidated": True, "reason": "definition_version_unknown"}
    if not isinstance(impact_set, Mapping) or not isinstance(key, Mapping):
        return {"status": "unknown", "invalidated": True, "reason": "impact_or_key_missing"}
    key_dimensions = set(key.get("dimensions", {})) | {"scope", "target"}
    app = normalized["applicability"]
    changed_fields = impact_set.get("changed_fields", [])
    if not isinstance(changed_fields, list) or any(not isinstance(field, str) for field in changed_fields):
        return {"status": "unknown", "invalidated": True, "reason": "changed_fields_unknown"}
    changed_dimensions: set[str] = set()
    unmapped_fields: list[str] = []
    for field in changed_fields:
        mapped = app["field_dimensions"].get(field)
        if mapped is None:
            unmapped_fields.append(field)
        else:
            changed_dimensions.update(mapped)
    if changed_dimensions & key_dimensions:
        return {"status": "invalid", "invalidated": True, "reason": "related_key_dimension_changed",
                "dimensions": sorted(changed_dimensions & key_dimensions)}
    if unmapped_fields:
        return {"status": "unknown", "invalidated": True, "reason": "field_applicability_unmapped",
                "fields": sorted(unmapped_fields)}

    unknowns = impact_set.get("unknowns", [])
    if not isinstance(unknowns, list):
        return {"status": "unknown", "invalidated": True, "reason": "impact_unknowns_malformed"}
    relevant_unknowns, unrelated_unknowns = [], []
    for item in unknowns:
        if not isinstance(item, Mapping) or not isinstance(item.get("reason_code"), str):
            return {"status": "unknown", "invalidated": True, "reason": "impact_unknown_unclassified"}
        dimensions = item.get("dimensions")
        if dimensions is None:
            dimensions = app["unknown_reason_dimensions"].get(item["reason_code"])
        if not isinstance(dimensions, list) or any(not isinstance(dim, str) for dim in dimensions):
            return {"status": "unknown", "invalidated": True, "reason": "impact_unknown_dimension_unmapped",
                    "reason_code": item["reason_code"]}
        overlap = set(dimensions) & key_dimensions
        if overlap:
            relevant_unknowns.append({"reason_code": item["reason_code"], "dimensions": sorted(overlap)})
        else:
            unrelated_unknowns.append({"reason_code": item["reason_code"], "dimensions": sorted(set(dimensions))})
    if relevant_unknowns:
        return {"status": "unknown", "invalidated": True, "reason": "related_impact_unknown",
                "unknowns": relevant_unknowns}
    completeness = impact_set.get("completeness")
    if completeness not in {"known", "partial", "unknown"}:
        return {"status": "unknown", "invalidated": True, "reason": "impact_completeness_missing_or_unknown"}
    if completeness in {"partial", "unknown"} and not unknowns:
        return {"status": "unknown", "invalidated": True, "reason": "partial_impact_has_no_dimension_detail"}
    if completeness == "unknown" and not unrelated_unknowns:
        return {"status": "unknown", "invalidated": True, "reason": "impact_completeness_unknown"}
    return {"status": "unaffected", "invalidated": False,
            "reason": "no_related_key_change", "unrelated_unknowns": unrelated_unknowns}


def invalidate_reuse(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Pure F3-to-F6 applicability decision; it does not mutate stored claims."""
    if not isinstance(payload, Mapping):
        raise _invalid("invalidation payload must be an object")
    definition = payload.get("definition")
    conditions = payload.get("conditions")
    key_result = build_reuse_key(definition, conditions)
    if key_result["status"] != "ready":
        return {"status": "unknown", "invalidated": True, "reason": "required_key_dimension_unknown",
                "unknown_dimensions": key_result["unknown_dimensions"]}
    result = impact_invalidates(definition, payload.get("impact_set"), key_result["key"])
    return result | {"key_sha256": key_result["key_sha256"]}


def _selector_value(dimension, selector, snapshot, command_data):
    kind = selector["kind"]
    if dimension == "input":
        value = snapshot.get("inputs_sha256")
        return ({"state": "known", "sha256": fingerprint({"command": snapshot.get("command"),
                                                              "inputs_sha256": value})}
                if _sha(value) else {"state": "unknown", "reason_code": "input_snapshot_missing"})
    if dimension == "environment":
        value = snapshot.get("environment_id")
        return ({"state": "known", "sha256": fingerprint({"environment_id": value})}
                if isinstance(value, str) and value else {"state": "unknown", "reason_code": "environment_unknown"})
    if dimension == "source" and kind == "workspace_files":
        by_path = {item.get("path"): item.get("sha256") for item in snapshot.get("workspace_files", [])
                   if isinstance(item, Mapping)}
        return related_source_dimension(by_path, selector["paths"])
    if dimension in {"source", "baseline"} and kind == "source_pin_fields":
        pin = snapshot.get("source_pin")
        if not isinstance(pin, Mapping) or any(field not in pin for field in selector["fields"]):
            return {"state": "unknown", "reason_code": "source_pin_field_unknown"}
        selected = {field: pin[field] for field in selector["fields"]}
        if any(value is None for value in selected.values()):
            return {"state": "unknown", "reason_code": "source_pin_field_unknown"}
        return {"state": "known", "sha256": fingerprint({"selector_version": 1,
                                                              "source_pin": selected})}
    if dimension == "tool" and kind == "runtime_fields":
        runtime = snapshot.get("runtime")
        if not isinstance(runtime, Mapping) or any(field not in runtime for field in selector["fields"]):
            return {"state": "unknown", "reason_code": "runtime_selector_value_missing"}
        selected = {field: runtime[field] for field in selector["fields"]}
        return {"tool_id": "pmt-p2-runtime", "version": str(runtime.get("python", "unknown")),
                "capability_sha256": fingerprint(selected)}
    if dimension == "dependencies" and kind == "dependency_manifests":
        manifests = snapshot.get("dependency_manifests")
        if not isinstance(manifests, list):
            return {"state": "unknown", "reason_code": "dependency_manifests_unknown"}
        selected = {item.get("path"): item.get("sha256") for item in manifests if isinstance(item, Mapping)}
        return related_baseline_dimension(selected, selector["names"])
    if dimension == "baseline" and kind == "criteria":
        criteria = snapshot.get("criteria")
        if not isinstance(criteria, Mapping):
            return {"state": "unknown", "reason_code": "criteria_unknown"}
        return related_baseline_dimension(criteria, selector["ids"])
    if dimension == "model" and kind == "verification_model":
        model = command_data.get("model_provenance") if isinstance(command_data, Mapping) else None
        if not isinstance(model, Mapping):
            return {"state": "unknown", "reason_code": "verified_model_provenance_missing"}
        return dict(model)
    return {"state": "unknown", "reason_code": f"{dimension}_selector_unsupported"}


def _validate_p2_candidate(db, conn, row, current_snapshot, current_conditions, definition,
                           command, target_record, *, allow_external_source_pin=False):
    """Validate a P2 receipt by the declared F6 dimensions, not P2's coarse whole snapshot hash."""
    from ..verification import (_array, _failure_after, _load_verification, _stored_scope_id,
                                _validate_evidence)
    item = _load_verification(row)
    stored_command = json.loads(row["command_json"])
    snapshot = item.get("snapshot")
    if (item["outcome"] != "pass" or item["state"] != "valid"
            or stored_command.get("before_fingerprint") != row["input_fingerprint"]
            or stored_command.get("snapshot_hash") != row["input_fingerprint"]
            or item.get("command") != command or not isinstance(snapshot, Mapping)):
        return None
    selectors = definition["selectors"]
    saved_dimensions = {dim: _selector_value(dim, selector, snapshot, stored_command)
                        for dim, selector in selectors.items()}
    if allow_external_source_pin:
        for dim, selector in selectors.items():
            if selector["kind"] == "source_pin_fields" and saved_dimensions[dim].get("state") == "unknown":
                saved_dimensions[dim] = current_conditions["dimensions"][dim]
    saved_conditions = json.loads(canonical_json(current_conditions))
    saved_conditions["dimensions"] = saved_dimensions
    saved_key = build_reuse_key(definition, saved_conditions)
    current_key = build_reuse_key(definition, current_conditions)
    if saved_key.get("status") != "ready" or current_key.get("status") != "ready" \
            or saved_key["key_sha256"] != current_key["key_sha256"]:
        return None
    if _stored_scope_id(conn, row, item) != target_record["scope_id"]:
        return None
    if _failure_after(conn, item["definition_id"], item["definition_version"],
                      target_record["scope_id"], row["verification_rowid"]):
        return None
    # P2 criterion coverage remains authoritative for criteria explicitly selected
    # into the F6 baseline dimension; unrelated criteria do not widen this key.
    selector = selectors.get("baseline")
    if selector and selector.get("kind") == "criteria":
        current_criteria = current_snapshot.get("criteria", {})
        coverage = {value.get("id"): value.get("sha256") for value in item["criterion_coverage"]
                    if isinstance(value, Mapping)}
        if any(not isinstance(current_criteria, Mapping) or criterion not in current_criteria
               or coverage.get(criterion) != current_criteria[criterion] for criterion in selector["ids"]):
            return None
    valid_ids, reasons, hashes = _validate_evidence(db, conn, item["evidence_ids"], target_record["scope_id"])
    if reasons or not valid_ids:
        return None
    return {"verification": dict(row),
            "evidence_refs": [{"id": aid, "sha256": hashes[aid]} for aid in valid_ids]}


def _actual_request(db, req):
    """Create conditions from a live target/P2 snapshot; caller cannot supply fingerprints."""
    p = req["payload"]
    allowed = {"definition", "run_id", "workspace", "paths", "target_id", "command", "inputs",
               "verification_id", "event_id", "reason", "repository_id", "relative_graph_path",
               "expected_source", "change_preview", "change_set", "rule_version", "max_depth"}
    if set(p) - allowed:
        raise _invalid("unsupported reuse adapter fields")
    definition = p.get("definition")
    normalized, definition_reasons = _definition(definition)
    if definition_reasons or normalized is None:
        return None, {"status": "unknown", "reason": "unsupported_definition_schema"}
    if set(normalized["required_dimensions"]) - set(normalized["selectors"]):
        return None, {"status": "unknown", "reason": "required_selector_missing",
                      "unknown_dimensions": sorted(set(normalized["required_dimensions"]) - set(normalized["selectors"]))}
    run_id = _canonical_uuid(p.get("run_id"), "run_id")
    paths = p.get("paths")
    if not isinstance(paths, list) or not paths or any(not isinstance(x, str) for x in paths):
        raise _invalid("paths must list the claimed relative target paths")
    command = p.get("command")
    if not ((isinstance(command, str) and command.strip()) or
            (isinstance(command, list) and command and all(isinstance(x, str) and x for x in command))):
        raise _invalid("command must be a nonempty string or string array")
    inputs = p.get("inputs")
    target_id = p.get("target_id")
    with closing(db.connect()) as conn:
        run = require_workspace_claim(db, conn, req, p.get("workspace"), paths)
        current_step = conn.execute("SELECT * FROM records WHERE id=?", (run["step_id"],)).fetchone()
        if current_step is None:
            raise PmtError("reuse_target_unavailable", "Current run step is unavailable", 3)
        target_id = _canonical_uuid(target_id, "target_id") if target_id else current_step["id"]
        # Only the active step or one of its record ancestors can be selected.
        ancestors, cursor = set(), current_step
        while cursor is not None and cursor["id"] not in ancestors:
            ancestors.add(cursor["id"])
            cursor = conn.execute("SELECT * FROM records WHERE id=?", (cursor["parent_id"],)).fetchone() if cursor["parent_id"] else None
        if target_id not in ancestors:
            raise PmtError("reuse_target_unavailable", "Target must be the current step or an ancestor", 3)
        record = conn.execute("SELECT * FROM records WHERE id=?", (target_id,)).fetchone()
        project_id = project_scope_id(conn, record["scope_id"])
        run_project_id = project_scope_id(conn, current_step["scope_id"])
        request_project_id = project_scope_id(conn, req.get("scope_id"))
        if project_id != run_project_id or request_project_id != project_id:
            raise PmtError("reuse_scope_mismatch", "Target, run and authorized request must share a project", 3)
        from ..verification import _snapshot, _load_verification
        before_snapshot, before_digest, reasons, _, verification_scope = _snapshot(
            db, conn, target_id, normalized["definition_id"], normalized["definition_version"], command, inputs)
        if reasons:
            return None, {"status": "unknown", "reason": "current_snapshot_incomplete", "unknown_dimensions": reasons}
        selectors = normalized["selectors"]
        if any(selector["kind"] == "source_pin_fields" for selector in selectors.values()):
            if not isinstance(p.get("repository_id"), str) or not isinstance(p.get("relative_graph_path"), str):
                return None, {"status": "unknown", "reason": "source_pin_request_missing"}
            from .graph import _source_graph
            source_context = _source_graph(db, conn, req)
            before_snapshot["source_pin"] = source_context["source_pin"].to_dict()
        dimensions = {dim: _selector_value(dim, selector, before_snapshot, {})
                      for dim, selector in selectors.items()}
        target_selector = normalized["target_selector"]
        target_body = json.loads(record["body_json"])
        if target_selector["kind"] == "record_revision":
            target_version = str(record["revision"])
        elif target_selector["kind"] == "record_fields":
            missing = [field for field in target_selector["fields"] if field not in target_body]
            extra = set(target_body) - {"workspace", "criteria"} - set(target_selector["fields"])
            if missing or extra:
                return None, {"status": "unknown", "reason": "target_semantic_fields_unselected",
                              "unknown_dimensions": sorted(missing + list(extra))}
            selected_fields = {field: target_body[field] for field in target_selector["fields"]}
            target_version = fingerprint({"target_kind": record["kind"], "title": record["title"],
                                           "fields": selected_fields})
        else:
            if set(target_body) - {"workspace", "criteria"}:
                return None, {"status": "unknown", "reason": "target_semantics_require_selector",
                              "unknown_dimensions": sorted(set(target_body) - {"workspace", "criteria"})}
            target_version = fingerprint({"target_kind": record["kind"], "title": record["title"],
                                           "definition_meaning_sha256": normalized["meaning_sha256"]})
        # Prove requested source paths are within this run's claimed scope.
        source_selector = selectors.get("source")
        if source_selector and source_selector["kind"] == "workspace_files":
            require_workspace_claim(db, conn, req, p["workspace"], source_selector["paths"])
        actual_conditions = {"scope": {"level": "project", "id": project_id,
                            "project_dimension": {"state": "known", "id": project_id}},
             "target": {"kind": record["kind"], "id": target_id, "version": target_version},
             "dimensions": dimensions}
        current_conditions = json.loads(canonical_json(actual_conditions))
        candidate = None
        evidence_refs = []
        if p.get("verification_id"):
            verification_id = _canonical_uuid(p["verification_id"], "verification_id")
            vrow = conn.execute("SELECT rowid AS verification_rowid,* FROM verifications WHERE id=?",
                                (verification_id,)).fetchone()
            if not vrow or (vrow["target_id"], vrow["definition_id"], vrow["definition_version"]) != (
                    target_id, normalized["definition_id"], normalized["definition_version"]):
                raise PmtError("reuse_verification_mismatch", "Verification does not match definition and target", 3)
            command_data = json.loads(vrow["command_json"])
            if (command_data.get("snapshot_hash") != vrow["input_fingerprint"]
                    or command_data.get("snapshot", {}).get("inputs_sha256") != before_snapshot.get("inputs_sha256")
                    or command_data.get("command") != command):
                return None, {"status": "invalid", "reason": "verification_condition_mismatch"}
            candidate = _validate_p2_candidate(db, conn, vrow, before_snapshot, actual_conditions,
                                               normalized, command, dict(record),
                                               allow_external_source_pin=req.get("operation") == "record_reuse_result")
            if not candidate:
                return None, {"status": "invalid", "reason": "verification_or_evidence_invalid",
                              "reason_codes": ["P2 receipt no longer matches its selected dimensions or evidence"]}
            evidence_refs = candidate["evidence_refs"]
        else:
            # Search actual P2 verification rows for this definition and target. Each
            # candidate is revalidated by verify_completion, including current snapshot,
            # evidence bytes, and later-failure ordering before it can match.
            from ..verification import _load_verification
            rows = conn.execute("SELECT rowid AS verification_rowid,* FROM verifications "
                "WHERE target_id=? AND definition_id=? AND definition_version=? AND environment_id=? "
                "AND outcome='pass' AND state='valid' ORDER BY rowid DESC LIMIT 500",
                (target_id, normalized["definition_id"], normalized["definition_version"], db.environment_id)).fetchall()
            for item_row in rows:
                item = _load_verification(item_row)
                saved = item.get("snapshot")
                saved_command = item.get("command")
                if (not isinstance(saved, Mapping) or saved_command != command
                        or item.get("snapshot_hash") != item_row["input_fingerprint"]
                        or saved.get("inputs_sha256") != before_snapshot.get("inputs_sha256")):
                    continue
                selected_dims = {dim: _selector_value(dim, selector, saved, item)
                                 for dim, selector in selectors.items()}
                candidate_conditions = dict(actual_conditions)
                candidate_conditions["dimensions"] = selected_dims
                candidate_key = build_reuse_key(normalized, candidate_conditions)
                if candidate_key.get("status") != "ready":
                    continue
                current_key = build_reuse_key(normalized, current_conditions)
                if current_key.get("status") != "ready" or candidate_key["key_sha256"] != current_key["key_sha256"]:
                    continue
                validated = _validate_p2_candidate(db, conn, item_row, before_snapshot,
                                                   current_conditions, normalized, command, dict(record))
                if not validated:
                    continue
                # Keep the candidate; the pure key builder is rerun after leaving the DB read.
                candidate = {**validated, "conditions": current_conditions}
                break
            vrow = candidate["verification"] if candidate else None
            command_data = json.loads(vrow["command_json"]) if vrow else {"command": command}
            if candidate:
                actual_conditions = candidate["conditions"]
        actual = {"run": dict(run), "record": dict(record), "project_id": project_id,
                  "snapshot": before_snapshot, "snapshot_hash": before_digest,
                  "verification": dict(vrow) if vrow else None, "command_data": command_data,
                  "candidate": candidate, "evidence_refs": evidence_refs,
                  "owner": {"actor": req["actor"], "session_id": req["session_id"]},
                  "conditions": actual_conditions, "verification_scope": verification_scope}
    key_result = build_reuse_key(normalized, actual_conditions)
    if key_result["status"] != "ready":
        return None, {"status": "unknown", "reason": "required_key_dimension_unknown",
                      "unknown_dimensions": key_result["unknown_dimensions"]}
    if (not actual.get("candidate") and any(selector["kind"] == "source_pin_fields"
                                             for selector in normalized["selectors"].values())):
        # SourcePin is not part of legacy P2 snapshots. It can be used for an automatic
        # hit only when an earlier F6 receipt bound that exact pin into the immutable key.
        claim_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "pmt-reuse:" + key_result["key"]["scope"]["id"]
                                  + ":" + key_result["key_sha256"]))
        with closing(db.connect()) as conn:
            saved = conn.execute("SELECT body_json FROM phase3_objects WHERE kind='reuse_claim' AND id=? "
                                 "AND scope_id=?", (claim_id, key_result["key"]["scope"]["id"])).fetchone()
            saved_body = json.loads(saved[0]) if saved else None
            origin = saved_body.get("origin_ref") if isinstance(saved_body, Mapping) else None
            verification_id = origin.get("id") if isinstance(origin, Mapping) and origin.get("kind") == "verification" else None
            if (saved_body and saved_body.get("status") == "completed"
                    and saved_body.get("key_sha256") == key_result["key_sha256"] and verification_id):
                vrow = conn.execute("SELECT rowid AS verification_rowid,* FROM verifications WHERE id=?",
                                    (verification_id,)).fetchone()
                if vrow:
                    verified = _validate_p2_candidate(db, conn, vrow, actual["snapshot"], actual["conditions"],
                                                      normalized, actual["command_data"].get("command"),
                                                      actual["record"], allow_external_source_pin=True)
                    if verified and verified["evidence_refs"] == saved_body.get("evidence_refs"):
                        actual["candidate"] = verified
    if req.get("operation") == "invalidate_reuse":
        from .graph import _calculate_impact, _source_graph
        with closing(db.connect()) as conn:
            context = _source_graph(db, conn, req)
            impact_source = _calculate_impact(db, conn, req, context)
        impact_set = {"completeness": "known" if impact_source.get("complete") else "partial",
                      "changed_fields": sorted({item["field"] for item in impact_source.get("field_changes", [])
                                                if isinstance(item, Mapping) and isinstance(item.get("field"), str)}),
                      "unknowns": [{"reason_code": item.get("reason_code")} for item in impact_source.get("unknown", [])
                                   if isinstance(item, Mapping) and isinstance(item.get("reason_code"), str)]}
        actual["impact_source"] = {"source_pin": impact_source.get("source_pin"),
                                   "rule_version": impact_source.get("rule_version"),
                                   "change_id": impact_source.get("change_id"),
                                   "complete": impact_source.get("complete")}
        actual["impact_decision"] = impact_invalidates(normalized, impact_set, key_result["key"])
    return (normalized, key_result, actual), None


def _decision_refs(scope_id, claim_id, revision, body):
    return {"body_ref": {"kind": "reuse_claim", "id": claim_id, "scope_id": scope_id,
                          "revision": revision, "key_sha256": body["key_sha256"],
                          "manifest_sha256": fingerprint(body)},
            "condition_refs": {name: fingerprint(value)
                               for name, value in body.get("key", {}).get("dimensions", {}).items()}}


def read_reuse_decision(db, conn, req):
    """Read the hash-bound, scope-authorized decision manifest for the F8 consumer."""
    p = req["payload"]
    if set(p) != {"body_ref", "run_id", "workspace", "paths"}:
        raise _invalid("read_reuse_decision requires body_ref and current run scope")
    ref = p["body_ref"]
    if not isinstance(ref, Mapping) or set(ref) != {"kind", "id", "scope_id", "revision",
                                                    "key_sha256", "manifest_sha256"}:
        raise _invalid("body_ref shape is invalid")
    if ref.get("kind") != "reuse_claim" or not _canonical_uuid(ref.get("id"), "body_ref.id"):
        raise _invalid("body_ref kind or id is invalid")
    if type(ref.get("revision")) is not int or ref["revision"] < 1 or not _sha(ref.get("key_sha256")) \
            or not _sha(ref.get("manifest_sha256")):
        raise _invalid("body_ref revision or fingerprints are invalid")
    run = require_workspace_claim(db, conn, req, p["workspace"], p["paths"])
    project_id = project_scope_id(conn, req["scope_id"])
    run_step = conn.execute("SELECT scope_id FROM records WHERE id=?", (run["step_id"],)).fetchone()
    if not run_step or project_scope_id(conn, run_step["scope_id"]) != project_id or ref["scope_id"] != project_id:
        raise PmtError("reuse_scope_mismatch", "Decision and active run must share an authorized project", 3)
    row = conn.execute("SELECT * FROM phase3_objects WHERE kind='reuse_claim' AND id=? AND scope_id=?",
                       (ref["id"], project_id)).fetchone()
    if not row or row["revision"] != ref["revision"]:
        raise PmtError("reuse_decision_stale", "Decision reference is missing or has changed", 3)
    body = json.loads(row["body_json"])
    if (body.get("key_sha256") != ref["key_sha256"] or fingerprint(body) != ref["manifest_sha256"]
            or not isinstance(body.get("key"), Mapping) or fingerprint(body["key"]) != ref["key_sha256"]):
        raise PmtError("reuse_decision_corrupt", "Decision manifest failed hash verification", 4)
    return {"decision_ref": dict(ref), "status": body.get("status"),
            "condition_refs": {name: fingerprint(value) for name, value in body["key"].get("dimensions", {}).items()},
            "manifest": body}


def handle(db, conn, req):
    if req.get("operation") != "read_reuse_decision":
        raise PmtError("operation_unavailable", "Reuse read operation is unavailable")
    return read_reuse_decision(db, conn, req)


def execute_file(db, req):
    """SQLite-backed F6 bridge; evidence checks precede its short request transaction."""
    from ..service import response
    from ..resources import check_artifact
    operation, p = req["operation"], req["payload"]
    prepared, early = _actual_request(db, req)
    if early:
        return response(req["request_id"], result=early), 0
    definition, key_result, actual = prepared
    key_hash = key_result["key_sha256"]
    scope_id = key_result["key"]["scope"]["id"]
    claim_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "pmt-reuse:" + scope_id + ":" + key_hash))
    request_id = _canonical_uuid(req.get("request_id"), "request_id")
    event_id = _canonical_uuid(p.get("event_id"), "event_id")
    if request_id == event_id:
        raise _invalid("request_id and event_id are distinct identities")

    def write(conn, request):
        # Revalidate current authorization and run state at the write boundary.
        run = require_workspace_claim(db, conn, req, p["workspace"], p["paths"])
        if run["id"] != actual["run"]["id"]:
            raise PmtError("reuse_run_changed", "Current run changed during evidence preparation", 3)
        validate_scope(db, conn, scope_id)
        if operation == "invalidate_reuse":
            from ..lifecycle import _event
            decision = actual["impact_decision"]
            _event(conn, request, event_id=event_id, event_type="efficiency.reuse_invalidated",
                   scope_id=scope_id, record_id=actual["record"]["id"],
                   payload={"key_sha256": key_hash, "status": decision["status"],
                            "reason": decision["reason"],
                            "source_pin": actual["impact_source"].get("source_pin"),
                            "rule_version": actual["impact_source"].get("rule_version"),
                            "change_id": actual["impact_source"].get("change_id")})
            return decision | {"key_sha256": key_hash, "impact_source": actual["impact_source"]}
        row = conn.execute("SELECT * FROM phase3_objects WHERE kind='reuse_claim' AND id=?", (claim_id,)).fetchone()
        if operation == "record_reuse_result":
            if row is None:
                raise PmtError("reuse_claim_not_found", "No active claim exists for this exact key", 3)
            body = json.loads(row["body_json"])
            if body.get("key_sha256") != key_hash or (row["owner_actor"], row["owner_session"], body.get("original_run_ref")) != (
                    req["actor"], req["session_id"], actual["run"]["id"]):
                raise PmtError("reuse_claim_owner_conflict", "Only the original run owner may record its verification", 3)
            run_now = conn.execute("SELECT state,stop_confirmed FROM execution_runs WHERE id=?",
                                   (actual["run"]["id"],)).fetchone()
            if not run_now or run_now["state"] not in {"review_pending", "succeeded", "completed"} or not run_now["stop_confirmed"]:
                raise PmtError("reuse_run_not_stopped", "Execution must be physically stopped before recording reusable evidence", 3)
            body.update({"status": "completed", "origin_ref": {"kind": "verification",
                          "id": actual["verification"]["id"]},
                         "receipt_ref": actual["verification"]["id"],
                         "evidence_refs": actual["evidence_refs"],
                         "verification_outcome": "pass"})
            conn.execute("UPDATE phase3_objects SET body_json=?,state='completed',revision=revision+1,updated_at=? "
                         "WHERE kind='reuse_claim' AND id=? AND revision=?",
                         (canonical_json(body), utc_now(), claim_id, row["revision"] if "revision" in row.keys() else 1))
            from ..lifecycle import _event
            _event(conn, request, event_id=event_id, event_type="efficiency.reuse_result_recorded",
                   scope_id=scope_id, record_id=actual["record"]["id"],
                   payload={"key_sha256": key_hash, "verification_ref": actual["verification"]["id"],
                            "evidence_count": len(actual["evidence_refs"])})
            return {"status": "recorded", "origin_ref": body["origin_ref"],
                    "receipt_ref": body["receipt_ref"], "key_sha256": key_hash,
                    "reuse_ref": claim_id,
                    "claim_receipt": {"state": "completed", "key_sha256": key_hash,
                                      "origin_ref": body["origin_ref"]},
                    **_decision_refs(scope_id, claim_id, row["revision"] + 1, body)}
        if row is None and actual.get("candidate"):
            verification_id = actual["candidate"]["verification"]["id"]
            body = {"manifest_schema": 1, "key_schema": KEY_SCHEMA_ID, "key": key_result["key"],
                    "key_sha256": key_hash, "status": "completed", "scope_id": scope_id,
                    "origin_ref": {"kind": "verification", "id": verification_id},
                    "receipt_ref": verification_id, "evidence_refs": actual["candidate"]["evidence_refs"],
                    "definition": {"id": definition["definition_id"], "version": definition["definition_version"]}}
            now = utc_now()
            conn.execute("INSERT INTO phase3_objects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         ("reuse_claim", claim_id, scope_id, req["actor"], req["session_id"], key_hash,
                          1, canonical_json(body), "completed", now, now))
            from ..lifecycle import _event
            _event(conn, request, event_id=event_id, event_type="efficiency.reuse_resolved",
                   scope_id=scope_id, record_id=actual["record"]["id"],
                   payload={"key_sha256": key_hash, "status": "reusable",
                            "verification_ref": verification_id,
                            "evidence_count": len(body["evidence_refs"])})
            return {"status": "reusable", "reusable": True, "reason": "exact_current_p2_receipt",
                    "origin_ref": body["origin_ref"], "receipt_ref": verification_id,
                    "evidence_refs": body["evidence_refs"], "key_sha256": key_hash,
                    "reuse_ref": claim_id,
                    "claim_receipt": {"state": "completed", "key_sha256": key_hash,
                                      "origin_ref": body["origin_ref"]},
                    **_decision_refs(scope_id, claim_id, 1, body)}
        if row:
            body = json.loads(row["body_json"])
            if row["scope_id"] != scope_id or body.get("key_sha256") != key_hash:
                raise PmtError("reuse_claim_conflict", "Stored claim identity conflicts with its manifest", 3)
            if body.get("status") == "active":
                result = {"status": "active", "reusable": False, "reason": "exact_key_active",
                          "original_run_ref": body["original_run_ref"],
                          "owner_ref": body["owner_ref"], "key_sha256": key_hash,
                          "reuse_ref": claim_id,
                          "claim_receipt": {"state": "active", "key_sha256": key_hash,
                                            "original_run_ref": body["original_run_ref"]}}
            else:
                if actual.get("candidate"):
                    verification_id = actual["candidate"]["verification"]["id"]
                    body.update({"status": "completed", "origin_ref": {"kind": "verification", "id": verification_id},
                                 "receipt_ref": verification_id,
                                 "evidence_refs": actual["candidate"]["evidence_refs"]})
                    conn.execute("UPDATE phase3_objects SET body_json=?,state='completed',revision=revision+1,updated_at=? "
                                 "WHERE kind='reuse_claim' AND id=? AND revision=?",
                                 (canonical_json(body), utc_now(), claim_id, row["revision"]))
                    result = {"status": "reusable", "reusable": True, "reason": "exact_key_verified",
                              "origin_ref": body["origin_ref"], "receipt_ref": verification_id,
                              "evidence_refs": body["evidence_refs"], "key_sha256": key_hash,
                              "reuse_ref": claim_id,
                              "claim_receipt": {"state": "completed", "key_sha256": key_hash,
                                                "origin_ref": body["origin_ref"]}}
                else:
                    # A formerly valid receipt is now stale or damaged. Retain its event
                    # history, but never return it as reusable; claim a fresh exact-key run.
                    body.update({"status": "active", "original_run_ref": actual["run"]["id"],
                                 "owner_ref": actual["owner"]})
                    for field in ("origin_ref", "receipt_ref", "evidence_refs", "verification_outcome"):
                        body.pop(field, None)
                    conn.execute("UPDATE phase3_objects SET owner_actor=?,owner_session=?,body_json=?,state='active',"
                                 "revision=revision+1,updated_at=? WHERE kind='reuse_claim' AND id=? AND revision=?",
                                 (req["actor"], req["session_id"], canonical_json(body), utc_now(),
                                  claim_id, row["revision"]))
                    result = {"status": "claimed", "reusable": False,
                              "reason": "stored_receipt_no_longer_applicable", "claim_ref": claim_id,
                              "reuse_ref": claim_id, "original_run_ref": actual["run"]["id"],
                              "key_sha256": key_hash,
                              "claim_receipt": {"state": "active", "key_sha256": key_hash,
                                                "original_run_ref": actual["run"]["id"]}}
            from ..lifecycle import _event
            current_revision = conn.execute("SELECT revision FROM phase3_objects WHERE kind='reuse_claim' AND id=?",
                                            (claim_id,)).fetchone()[0]
            result.update(_decision_refs(scope_id, claim_id, current_revision, body))
            _event(conn, request, event_id=event_id,
                   event_type="efficiency.reuse_claimed" if result["status"] == "claimed"
                   else "efficiency.reuse_resolved",
                   scope_id=scope_id, record_id=actual["record"]["id"],
                   payload={"key_sha256": key_hash, "status": result["status"],
                            "origin_ref": result.get("origin_ref", {"kind": "run", "id": result.get("original_run_ref")}),
                            "replaced_stale_receipt": result["status"] == "claimed"})
            return result
        body = {"manifest_schema": 1, "key_schema": KEY_SCHEMA_ID, "key": key_result["key"],
                "key_sha256": key_hash, "status": "active", "scope_id": scope_id,
                "original_run_ref": actual["run"]["id"], "owner_ref": actual["owner"],
                "definition": {"id": definition["definition_id"], "version": definition["definition_version"]}}
        now = utc_now()
        conn.execute("INSERT INTO phase3_objects VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                     ("reuse_claim", claim_id, scope_id, req["actor"], req["session_id"], key_hash,
                      1, canonical_json(body), "active", now, now))
        from ..lifecycle import _event
        _event(conn, request, event_id=event_id, event_type="efficiency.reuse_claimed",
               scope_id=scope_id, record_id=actual["record"]["id"],
               payload={"key_sha256": key_hash, "original_run_ref": actual["run"]["id"]})
        return {"status": "claimed", "reusable": False, "reason": "no_verified_prior_result",
                "claim_ref": claim_id, "reuse_ref": claim_id,
                "original_run_ref": actual["run"]["id"], "key_sha256": key_hash,
                "claim_receipt": {"state": "active", "key_sha256": key_hash,
                                  "original_run_ref": actual["run"]["id"]},
                **_decision_refs(scope_id, claim_id, 1, body)}

    envelope, code = db.run_request(req, write)
    return envelope, code
