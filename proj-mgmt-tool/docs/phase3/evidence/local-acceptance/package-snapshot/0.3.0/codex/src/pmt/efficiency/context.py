"""Budgeted F5 projections with live source, run, directive and scope checks.

Stored contexts are derived views, never ownership grants or editable copies of
the private directive. Every public read revalidates its current authority.
"""
from __future__ import annotations

import base64
from contextlib import closing
import hashlib
import json
import os
import sqlite3
import re
import uuid
from collections.abc import Mapping
from typing import Any

from ..errors import PmtError
from ..util import canonical_json, fingerprint
from .source import pin_source, verify_source_pin
from .storage import Phase3Storage

CONTEXT_VERSION = 1
ALIAS_MAP_VERSION = 1
READ_OPERATIONS = {"read_context_detail", "resolve_context_alias", "read_task_context"}
WRITE_OPERATIONS = set()
FILE_OPERATIONS = {"build_task_context", "resume_task_context"}
_MAX_CONTEXT_BYTES = 16 * 1024 * 1024
_MAX_CONTEXT_LINES = 20_000
_SENSITIVE_KEY = re.compile(r"(?i)(secret|token|authorization|password|credential|environment|conversation|transcript|api.?key|private.?key|claim)")
_SENSITIVE_TEXT = (
    re.compile(r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|credential)\b\s*[:=]\s*)([^\s,;]+)"),
    re.compile(r"(?i)(\bauthorization\s*:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{16,})\b"),
)
_ROLE_NAME = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$")
_GRAPH_FIELDS = {
    "id", "tree_kind", "node_kind", "summary", "premise", "source_refs", "criteria",
    "product_stage", "product_scope", "autonomy", "evidence_refs", "work_item_step_refs",
    "framework_assignment", "architecture", "logging", "tests", "function_spec", "choice_set", "choice",
}
_ROLE_FIELDS = {
    "lower": ("summary", "premise", "product_scope", "autonomy", "criteria", "function_spec", "work_item_step_refs", "source_refs", "evidence_refs"),
    "worker": ("summary", "premise", "product_scope", "autonomy", "criteria", "function_spec", "work_item_step_refs", "source_refs", "evidence_refs"),
    "implement": ("summary", "premise", "product_scope", "autonomy", "criteria", "function_spec", "work_item_step_refs", "source_refs", "evidence_refs"),
    "implementation": ("summary", "premise", "product_scope", "autonomy", "criteria", "function_spec", "work_item_step_refs", "source_refs", "evidence_refs"),
    "test": ("summary", "premise", "criteria", "tests", "logging", "evidence_refs", "source_refs"),
    "review": ("summary", "premise", "product_scope", "autonomy", "criteria", "tests", "logging", "evidence_refs", "source_refs"),
    "research": ("summary", "premise", "product_scope", "choice_set", "choice", "framework_assignment", "source_refs", "evidence_refs"),
}
_RELATION_KINDS = ["parent", "refines", "implements", "depends_on", "evidence"]


def _bad(code: str, message: str, exit_code: int = 2, details=None):
    raise PmtError(code, message, exit_code, False, details)


def _canonical_id(value: Any, name: str) -> str:
    if not isinstance(value, str):
        _bad("context_input_invalid", f"{name} must be a canonical UUID")
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _bad("context_input_invalid", f"{name} must be a canonical UUID")
    return value


def _opaque_reference(value: Any, name: str, *, required=False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        _bad("context_authority_missing", f"{name} must be an opaque reference")
    candidate = value
    if any(pattern.search(candidate) for pattern in _SENSITIVE_TEXT):
        _bad("context_sensitive_reference", f"{name} must not contain a credential")
    return candidate


def _safe_value(value: Any, path: str, redacted: list[str]) -> Any:
    """Copy only JSON-compatible selected data and remove sensitive subfields."""
    if isinstance(value, Mapping):
        result = {}
        for key, child in value.items():
            if not isinstance(key, str):
                _bad("context_input_invalid", "Projected object keys must be text")
            if _SENSITIVE_KEY.search(key):
                redacted.append(path + "." + key)
                result[key] = "[REDACTED]"
            else:
                result[key] = _safe_value(child, path + "." + key, redacted)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_value(item, path + "[]", redacted) for item in value]
    if isinstance(value, str):
        result = value
        for pattern in _SENSITIVE_TEXT:
            if pattern.groups:
                result = pattern.sub(lambda match: match.group(1) + "[REDACTED]", result)
            else:
                result = pattern.sub("[REDACTED]", result)
        if result != value:
            redacted.append(path)
        return result
    if value is None or type(value) in (bool, int, float):
        return value
    _bad("context_input_invalid", "Projected fields must be JSON-compatible")


def _value_lines(value: Any) -> int:
    if isinstance(value, str):
        return max(1, value.count("\n") + 1)
    if isinstance(value, Mapping):
        return sum(_value_lines(child) for child in value.values())
    if isinstance(value, list):
        return sum(_value_lines(child) for child in value)
    return 1


def _section(section_id: str, kind: str, required: bool, value: Any, refs=None, redacted=False) -> dict:
    body = {"section_id": section_id, "kind": kind, "required": required,
            "value": value, "refs": refs or [], "redacted": bool(redacted)}
    return {**body, "size_bytes": len(canonical_json(body).encode("utf-8")),
            "line_count": _value_lines(value)}


def _context_budget(value: Any) -> dict:
    if not isinstance(value, Mapping) or set(value) - {"max_bytes", "max_lines", "unit"}:
        _bad("context_budget_invalid", "Budget requires max_bytes, max_lines, and unit=utf8")
    byte_limit, line_limit = value.get("max_bytes"), value.get("max_lines")
    if (type(byte_limit) is not int or not 1 <= byte_limit <= _MAX_CONTEXT_BYTES
            or type(line_limit) is not int or not 1 <= line_limit <= _MAX_CONTEXT_LINES
            or value.get("unit") != "utf8"):
        _bad("context_budget_invalid", "Context budget must be finite UTF-8 bytes and lines")
    return {"max_bytes": byte_limit, "max_lines": line_limit, "unit": "utf8"}


def graph_query_for_context(role: str, *, node_ids=None, cursor=None, page_size=100) -> dict:
    """Build the bounded F2 query shape; role only chooses projection fields."""
    if not isinstance(role, str) or not role.strip():
        _bad("context_input_invalid", "role must be nonempty text")
    if type(page_size) is not int or not 1 <= page_size <= 250:
        _bad("context_input_invalid", "page_size must be between 1 and 250")
    ids = list(node_ids or [])
    if any(not isinstance(node_id, str) for node_id in ids):
        _bad("context_input_invalid", "node_ids must be text references")
    ids = [_canonical_id(node_id, "node_id") for node_id in ids]
    fields = list(_ROLE_FIELDS.get(role.casefold(), ("summary", "premise", "autonomy", "criteria", "function_spec", "evidence_refs", "source_refs")))
    fields = [field for field in fields if field in _GRAPH_FIELDS]
    query = {"node_ids": ids, "relation_kinds": list(_RELATION_KINDS), "direction": "both",
             "max_depth": 2, "page_size": page_size, "fields": fields}
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor:
            _bad("context_input_invalid", "graph cursor must be nonempty text")
        query["cursor"] = cursor
    return query


def _graph_sections(graph_slice: Mapping[str, Any], role: str, redacted: list[str]) -> tuple[dict, list[dict], list[dict]]:
    if not isinstance(graph_slice, Mapping):
        _bad("context_input_invalid", "graph_slice must be an object")
    items = graph_slice.get("items", [])
    if not isinstance(items, list):
        _bad("context_input_invalid", "graph_slice.items must be an array")
    fields = set(_ROLE_FIELDS.get(role.casefold(), _ROLE_FIELDS["research"]))
    nodes, relations = [], []
    for row in items:
        if not isinstance(row, Mapping) or not isinstance(row.get("value"), Mapping):
            _bad("context_input_invalid", "Graph slice item has an invalid shape")
        value = row["value"]
        entity = row.get("entity")
        if entity == "node":
            _canonical_id(value.get("id"), "node_id")
            selected = {key: value[key] for key in ("id", "tree_kind", "node_kind", *sorted(fields)) if key in value}
            selected = _safe_value(selected, "graph.node", redacted)
            nodes.append({"entity": "node", "value": selected,
                          "path": _safe_value(row.get("path", []), "graph.path", redacted)})
        elif entity == "relation":
            required = ("id", "kind", "from", "to")
            if any(not isinstance(value.get(key), str) for key in required):
                _bad("context_input_invalid", "Graph relation requires id, kind, from, and to")
            _canonical_id(value["id"], "relation_id")
            _canonical_id(value["from"], "relation_from")
            _canonical_id(value["to"], "relation_to")
            if value["kind"] not in _RELATION_KINDS:
                _bad("context_input_invalid", "Graph relation has an unsupported kind")
            relations.append({"entity": "relation", "value": {key: value[key] for key in required}})
        else:
            _bad("context_input_invalid", "Graph item entity must be node or relation")
    nodes.sort(key=lambda row: row["value"]["id"])
    relations.sort(key=lambda row: row["value"]["id"])
    unknown = graph_slice.get("unknown", [])
    if not isinstance(unknown, list):
        _bad("context_input_invalid", "graph_slice.unknown must be an array")
    unknown_projection = []
    for item in unknown:
        if not isinstance(item, Mapping):
            unknown_projection.append({"kind": "unknown", "reason_code": "unstructured_unknown"})
            continue
        unknown_projection.append({key: item[key] for key in ("kind", "reason_code", "node_ids", "relation_ids", "segment_id") if key in item})
    if graph_slice.get("next_cursor"):
        unknown_projection.append({"kind": "graph_slice", "reason_code": "more_source_graph_available",
                                  "cursor_ref": graph_slice["next_cursor"]})
    traversal_complete = graph_slice.get("traversal_complete", graph_slice.get("complete")) is True
    index_metadata = graph_slice.get("index")
    if not isinstance(index_metadata, Mapping) or not isinstance(index_metadata.get("hash"), str):
        unknown_projection.append({"kind": "graph_index", "reason_code": "index_metadata_missing"})
        index_metadata = None
    complete = traversal_complete and not any(
        item.get("reason_code") in {"source_pin_unverified", "index_metadata_missing", "traversal_limit_or_depth"}
        for item in unknown_projection if isinstance(item, Mapping))
    return {"nodes": nodes, "relations": relations, "complete": complete,
            "slice_complete": graph_slice.get("complete") is True,
            "traversal_complete": traversal_complete, "index": dict(index_metadata) if index_metadata else None,
            "known_count": len(nodes) + len(relations), "next_source_cursor": graph_slice.get("next_cursor")}, unknown_projection, nodes


def _impact_section(value, source_pin, *, required):
    if value is None:
        return None, [{"section_id": "impact_summary", "reason_code": "impact_set_missing"}] if required else [], []
    if not isinstance(value, Mapping):
        _bad("context_impact_invalid", "F3 ImpactSet must be an object")
    for field in ("source_pin", "before_source_pin"):
        if not isinstance(value.get(field), Mapping):
            _bad("context_impact_invalid", f"ImpactSet requires {field}")
        verify_source_pin(source_pin, value[field])
    change_id = _canonical_id(value.get("change_id"), "impact.change_id")
    digest = value.get("change_set_hash")
    if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        _bad("context_impact_invalid", "change_set_hash must be lowercase SHA-256")
    expected_new = value.get("expected_new_source")
    if (not isinstance(expected_new, Mapping) or type(expected_new.get("graph_schema")) is not int
            or type(expected_new.get("graph_revision")) is not int
            or not isinstance(expected_new.get("graph_hash"), str)):
        _bad("context_impact_invalid", "expected_new_source must identify the proposed graph version")
    field_changes = value.get("field_changes", [])
    known = value.get("known", [])
    documents = value.get("documents", [])
    steps = value.get("steps", [])
    verifications = value.get("verifications", [])
    unknown = value.get("unknown", [])
    if any(not isinstance(items, list) for items in (field_changes, known, documents, steps, verifications, unknown)):
        _bad("context_impact_invalid", "ImpactSet collections must be arrays")
    known_ids = []
    for item in known:
        if not isinstance(item, Mapping):
            _bad("context_impact_invalid", "ImpactSet known item must be structured")
        known_ids.append(_canonical_id(item.get("node_id"), "impact.known.node_id"))
    safe_unknown = []
    for item in unknown:
        if not isinstance(item, Mapping):
            safe_unknown.append({"kind": "impact", "reason_code": "unstructured_impact_unknown",
                                 "severity": "required"})
            continue
        row = {key: item[key] for key in ("kind", "node_id", "node_ids", "relation_id", "segment_id", "field", "reason_code")
               if key in item}
        kind = item.get("kind")
        # Missing document coverage is additional unless the caller selected that exact segment as required.
        severity = "additional" if kind in {"document_segment", "document_segments", "document_coverage"} else "required"
        row["severity"] = severity
        safe_unknown.append(row)
    summary = {"change_id": change_id, "change_set_hash": digest,
               "source_pin": pin_source(value["source_pin"]).to_dict(),
               "before_source_pin": pin_source(value["before_source_pin"]).to_dict(),
               "expected_new_source": dict(expected_new), "field_changes": field_changes,
               "known": known, "documents": documents, "steps": steps,
               "verifications": verifications, "unknown": safe_unknown,
               "complete": value.get("complete") is True}
    return summary, [], safe_unknown


def _cursor(context_id, source_hash, scope_hash, mapping_version, content_hash, offset):
    body = {"context_id": context_id, "source_hash": source_hash, "scope_hash": scope_hash,
            "mapping_version": mapping_version, "content_hash": content_hash, "offset": offset}
    return base64.urlsafe_b64encode(canonical_json(body).encode("utf-8")).decode("ascii").rstrip("=")


def project_task_context(value: Mapping[str, Any]) -> dict:
    """Pure role projection over a caller-provided, already-authorized source snapshot.

    This prepares the immutable stored bundle and initial bounded response only.
    It does not load a Step, authorize scope, inspect Git, or write Phase3Storage.
    """
    if not isinstance(value, Mapping):
        _bad("context_input_invalid", "Projection input must be an object")
    required_fields = {"task_ref", "role", "source_pin", "access", "directive", "criteria", "graph_slice", "evidence_refs", "budget"}
    missing = sorted(required_fields - set(value))
    if missing:
        _bad("context_input_invalid", "Projection input is incomplete", details={"missing": missing})
    role = value["role"]
    if not isinstance(role, str) or not _ROLE_NAME.fullmatch(role):
        _bad("context_input_invalid", "role must be a bounded role identifier")
    task = value["task_ref"]
    if not isinstance(task, Mapping):
        _bad("context_input_invalid", "task_ref must be an object")
    task_id = _canonical_id(task.get("task_id"), "task_id")
    step_id = _canonical_id(task.get("step_id"), "step_id")
    run_id = task.get("run_id")
    if run_id is not None:
        run_id = _canonical_id(run_id, "run_id")
    source_pin = pin_source(value["source_pin"])
    access = value["access"]
    if not isinstance(access, Mapping):
        _bad("context_input_invalid", "access must be an authorized scope reference")
    scope_id = _canonical_id(access.get("scope_id"), "scope_id")
    authorization_ref = _opaque_reference(access.get("authorization_ref"), "authorization_ref", required=True)
    claim_ref = _opaque_reference(access.get("claim_ref"), "claim_ref")
    access_run = access.get("run_id")
    if access_run is not None:
        access_run = _canonical_id(access_run, "access.run_id")
    if run_id != access_run:
        _bad("context_authority_mismatch", "Authorized run does not match the Task reference", 3)
    if scope_id != source_pin.project_id:
        _bad("context_scope_mismatch", "Scope and SourcePin project must match", 3)
    directive = value["directive"]
    if not isinstance(directive, Mapping):
        _bad("context_input_invalid", "directive must be an authorized private projection input")
    criteria = value["criteria"]
    if not isinstance(criteria, list):
        _bad("context_input_invalid", "criteria must be an array")
    evidence = value["evidence_refs"]
    if (not isinstance(evidence, list) or len(evidence) > 500
            or any(not isinstance(ref, str) and not isinstance(ref, Mapping) for ref in evidence)):
        _bad("context_input_invalid", "evidence_refs must be a bounded array of refs or checked ref records")
    budget = _context_budget(value["budget"])

    context_id = value.get("context_id") or str(uuid.uuid4())
    context_id = _canonical_id(context_id, "context_id")
    redacted: list[str] = []
    sections: list[dict] = []
    missing_required: list[dict] = []
    source_unknown = []
    if source_pin.source_kind == "unknown":
        source_unknown.append({"kind": "source_pin", "reason_code": "source_kind_unknown"})
    if source_pin.dirty_state == "unknown":
        source_unknown.append({"kind": "source_pin", "reason_code": "dirty_state_unknown"})

    def add_required(section_id, field, value_override=None):
        if value_override is not None:
            content = value_override
        elif field in directive:
            content = directive[field]
        else:
            missing_required.append({"section_id": section_id, "reason_code": "source_field_missing"})
            return
        safe = _safe_value(content, section_id, redacted)
        sections.append(_section(section_id, "directive", True, safe, redacted=any(x.startswith(section_id) for x in redacted)))

    add_required("purpose", "purpose")
    add_required("goal", "goal")
    add_required("non_goal", "non_goal")
    add_required("change_scope", "change_scope")
    add_required("inputs", "inputs")
    add_required("outputs", "outputs")
    if criteria:
        add_required("criteria", "criteria", criteria)
    else:
        missing_required.append({"section_id": "criteria", "reason_code": "criteria_missing"})
    add_required("tests", "tests")
    add_required("logging", "logging")
    impact_summary, missing_impact, impact_unknown = _impact_section(
        value.get("impact_set"), source_pin, required=value.get("impact_required") is True)
    missing_required.extend(missing_impact)
    if impact_summary is not None:
        impact_safe = _safe_value(impact_summary, "impact_summary", redacted)
        sections.append(_section("impact_summary", "impact_set",
                                 value.get("impact_required") is True, impact_safe,
                                 refs=[item["node_id"] for item in impact_summary["known"]],
                                 redacted=any(x.startswith("impact_summary") for x in redacted)))
    evidence_records = []
    evidence_unknown = []
    for item in evidence:
        if isinstance(item, str):
            record = {"ref": item, "status": "unknown", "reason_code": "evidence_not_verified"}
        elif isinstance(item, Mapping) and isinstance(item.get("ref"), str):
            status = item.get("status")
            if status not in {"valid", "invalid", "unknown"}:
                status = "unknown"
            record = {"ref": item["ref"], "status": status}
            if isinstance(item.get("sha256"), str):
                record["sha256"] = item["sha256"]
            if isinstance(item.get("reason_code"), str):
                record["reason_code"] = item["reason_code"]
        else:
            _bad("context_input_invalid", "Evidence record requires a reference")
        evidence_records.append(record)
        if record["status"] != "valid":
            evidence_unknown.append({"kind": "evidence", "reason_code": record.get("reason_code", "evidence_not_verified"),
                                     "refs": [record["ref"]]})
    evidence_safe = _safe_value(evidence_records, "evidence_refs", redacted)
    sections.append(_section("evidence_refs", "references", True, evidence_safe, refs=evidence_safe,
                             redacted=any(x.startswith("evidence_refs") for x in redacted)))

    method_required = role.casefold() in {"lower", "worker", "implement", "implementation"}
    if method_required:
        add_required("method", "method")

    graph, unknown, graph_nodes = _graph_sections(value["graph_slice"], role, redacted)
    unknown.extend(source_unknown)
    provided_pin = value["graph_slice"].get("source_pin") if isinstance(value["graph_slice"], Mapping) else None
    try:
        graph_pin = pin_source(provided_pin)
    except PmtError:
        graph_pin = None
    if graph_pin is None or graph_pin.source_hash != source_pin.source_hash:
        unknown.append({"kind": "graph_slice", "reason_code": "source_pin_unverified"})
        graph = {"nodes": [], "relations": [], "complete": False, "slice_complete": False,
                 "traversal_complete": False, "index": None, "known_count": 0,
                 "next_source_cursor": None}
        graph_nodes = []
    elif graph["complete"] is False and not unknown:
        unknown.append({"kind": "graph_slice", "reason_code": "graph_completeness_unknown"})
    if method_required:
        autonomy_values = [row["value"]["autonomy"] for row in graph_nodes if "autonomy" in row["value"]]
        if "autonomy" in directive:
            autonomy_values.insert(0, directive["autonomy"])
        if autonomy_values:
            add_required("autonomy", "autonomy", autonomy_values)
        else:
            missing_required.append({"section_id": "autonomy", "reason_code": "authority_boundary_unavailable"})
    graph_value = {"nodes": graph["nodes"], "relations": graph["relations"], "complete": graph["complete"],
                   "slice_complete": graph["slice_complete"], "traversal_complete": graph["traversal_complete"],
                   "index": graph["index"], "known_count": graph["known_count"]}
    sections.append(_section("related_graph", "graph_projection", False, graph_value,
                             refs=[row["value"]["id"] for row in graph_nodes]))
    unknown.extend(impact_unknown)
    unknown.extend(evidence_unknown)
    unknown_safe = _safe_value(unknown, "unknown", redacted)
    for item in unknown_safe:
        if not isinstance(item, dict):
            continue
        reason = item.get("reason_code")
        if item.get("severity") in {"required", "additional"}:
            continue
        if reason in {"source_pin_unverified", "source_kind_unknown", "index_metadata_missing",
                      "traversal_limit_or_depth", "index_partial"}:
            item["severity"] = "required"
        else:
            item["severity"] = "additional"
    required_unknown = [item for item in unknown_safe if isinstance(item, dict) and item.get("severity") == "required"]
    sections.append(_section("unresolved", "unknown_manifest", True,
                             {"items": unknown_safe, "source_graph_cursor": graph["next_source_cursor"]},
                             refs=sorted({ref for item in unknown_safe if isinstance(item, Mapping)
                                          for key in ("node_ids", "relation_ids")
                                          for ref in (item.get(key) if isinstance(item.get(key), list) else [])
                                          if isinstance(ref, str)})))

    # Lower/worker implementations must receive the approved method and autonomy boundary.
    if not method_required and "method" in directive:
        safe = _safe_value(directive["method"], "method", redacted)
        sections.append(_section("method", "directive", False, safe,
                                 redacted=any(x.startswith("method") for x in redacted)))
    if "context_refs" in directive:
        safe = _safe_value(directive["context_refs"], "context_refs", redacted)
        sections.append(_section("context_refs", "references", False, safe,
                                 refs=safe if isinstance(safe, list) else [],
                                 redacted=any(x.startswith("context_refs") for x in redacted)))

    scope_binding = {"scope_id": scope_id, "authorization_ref": authorization_ref,
                     "claim_ref": claim_ref, "run_id": run_id}
    scope_hash = fingerprint(scope_binding)
    graph_alias_ids = sorted({row["value"]["id"] for row in graph_nodes})
    alias_entries = {f"N{index:04d}": node_id for index, node_id in enumerate(graph_alias_ids, 1)}
    alias_body = {"version": ALIAS_MAP_VERSION, "context_id": context_id,
                  "project_id": source_pin.project_id, "scope_hash": scope_hash,
                  "source_hash": source_pin.source_hash, "aliases": alias_entries}
    alias_body["mapping_hash"] = fingerprint(alias_body)

    included, omitted_required, omitted_optional = [], [], []
    used_bytes = used_lines = 0
    for section in sections:
        fits = (used_bytes + section["size_bytes"] <= budget["max_bytes"]
                and used_lines + section["line_count"] <= budget["max_lines"])
        if fits:
            included.append(section)
            used_bytes += section["size_bytes"]
            used_lines += section["line_count"]
        elif section["required"]:
            omitted_required.append({"section_id": section["section_id"], "reason_code": "budget_exceeded",
                                     "available": True, "bytes": section["size_bytes"],
                                     "lines": section["line_count"]})
        else:
            omitted_optional.append({"section_id": section["section_id"], "reason_code": "budget_exceeded",
                                     "available": True, "bytes": section["size_bytes"],
                                     "lines": section["line_count"]})
    detail_sections = omitted_required + omitted_optional
    omitted_ids = {item["section_id"] for item in detail_sections}
    detail_projection = [section for section in sections if section["section_id"] in omitted_ids]
    detail_text = json.dumps(detail_projection, ensure_ascii=False, sort_keys=True, indent=2) if detail_projection else ""
    detail_bytes = detail_text.encode("utf-8")
    detail_hash = hashlib.sha256(detail_bytes).hexdigest()
    source_ref = value.get("source_ref")
    if source_ref is not None:
        if not isinstance(source_ref, Mapping):
            _bad("context_input_invalid", "source_ref must be an object")
        required_source_ref = {"repository_id", "relative_graph_path", "directive_id", "directive_version", "directive_sha256"}
        if required_source_ref - set(source_ref):
            _bad("context_input_invalid", "source_ref is missing current verified reference fields")
        text_source_fields = {"repository_id", "relative_graph_path", "directive_id", "directive_sha256"}
        if any(not isinstance(source_ref[key], str) or not source_ref[key] for key in text_source_fields):
            _bad("context_input_invalid", "source_ref values must be nonempty text")
        if type(source_ref["directive_version"]) is not int or source_ref["directive_version"] < 1:
            _bad("context_input_invalid", "directive_version must be positive")
        _canonical_id(source_ref["repository_id"], "repository_id")
        _canonical_id(source_ref["directive_id"], "directive_id")
        digest = source_ref["directive_sha256"]
        if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            _bad("context_input_invalid", "directive_sha256 must be lowercase SHA-256")
        if ("criteria_sha256" in source_ref and (not isinstance(source_ref["criteria_sha256"], str)
                or len(source_ref["criteria_sha256"]) != 64)):
            _bad("context_input_invalid", "criteria_sha256 must be SHA-256")
        if ("graph_index_hash" in source_ref and (not isinstance(source_ref["graph_index_hash"], str)
                or len(source_ref["graph_index_hash"]) != 64)):
            _bad("context_input_invalid", "graph_index_hash must be SHA-256")
        if "graph_index_revision" in source_ref and (type(source_ref["graph_index_revision"]) is not int
                                                      or source_ref["graph_index_revision"] < 1):
            _bad("context_input_invalid", "graph_index_revision must be positive")
        if ("\\" in source_ref["relative_graph_path"] or source_ref["relative_graph_path"].startswith("/")
                or any(part in {".", "..", ""} for part in source_ref["relative_graph_path"].split("/"))):
            _bad("context_input_invalid", "relative_graph_path must be a normalized workspace-relative path")
        source_ref = dict(source_ref)
    incomplete = bool(missing_required or omitted_required or required_unknown or redacted)
    bundle = {
        "schema_version": CONTEXT_VERSION, "kind": "task_context", "context_id": context_id,
        "role": role, "task_ref": {"task_id": task_id, "step_id": step_id, "run_id": run_id},
        "source_pin": source_pin.to_dict(), "scope_binding": scope_binding, "scope_hash": scope_hash,
        "sections": sections, "aliases": alias_body, "source_ref": source_ref,
        "graph_index": graph["index"],
        "completeness": {"status": "incomplete" if incomplete else "complete",
                          "missing_required": missing_required,
                          "omitted_required": omitted_required,
                          "omitted_optional": omitted_optional,
                          "unknown": unknown_safe, "redacted_refs": sorted(set(redacted))},
        "detail_text": detail_text, "detail_sha256": detail_hash,
        "detail_size_bytes": len(detail_bytes), "detail_line_count": detail_text.count("\n") + (1 if detail_text else 0),
    }
    bundle["projection_hash"] = fingerprint({key: item for key, item in bundle.items() if key != "projection_hash"})
    detail_cursor = (_cursor(context_id, source_pin.source_hash, scope_hash, ALIAS_MAP_VERSION, detail_hash, 0)
                     if detail_bytes else None)
    response = {
        "context_ref": {"kind": "task_context", "id": context_id, "source_hash": source_pin.source_hash,
                        "projection_hash": bundle["projection_hash"], "version": CONTEXT_VERSION},
        "task_ref": bundle["task_ref"], "role": role,
        "source_pin": source_pin.to_dict(),
        "scope_ref": {"scope_id": scope_id, "authorization_ref": authorization_ref,
                       "claim_ref": claim_ref, "run_id": run_id, "scope_hash": scope_hash},
        "included": included, "omitted_required": omitted_required,
        "omitted_optional": omitted_optional, "missing_required": missing_required,
        "unknown": unknown_safe, "incomplete": incomplete,
        "alias_map": {"context_id": context_id, "version": ALIAS_MAP_VERSION,
                      "mapping_hash": alias_body["mapping_hash"],
                      "map_ref": {"kind": "context_alias_map", "id": context_id,
                                  "source_hash": source_pin.source_hash, "scope_hash": scope_hash}},
        "detail_cursor": detail_cursor,
        "budget": {"requested": budget, "used_bytes": used_bytes, "used_lines": used_lines,
                   "token_usage": {"status": "unknown", "actual": None, "estimate": None,
                                   "reason": "tokenizer_unavailable"}},
    }
    request_id = value.get("request_id", context_id)
    _canonical_id(request_id, "request_id")
    built = {"bundle": bundle, "response": response}
    _apply_wire_budget(built, request_id)
    return built


def _read_cursor(value, bundle):
    if not isinstance(value, str) or len(value) > 2048:
        _bad("context_cursor_invalid", "Context cursor is invalid", 3)
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        cursor = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise PmtError("context_cursor_invalid", "Context cursor is invalid", 3) from exc
    expected = {"context_id", "source_hash", "scope_hash", "mapping_version", "content_hash", "offset"}
    if (not isinstance(cursor, dict) or set(cursor) != expected
            or cursor["context_id"] != bundle.get("context_id")
            or cursor["source_hash"] != bundle.get("source_pin", {}).get("source_hash")
            or cursor["scope_hash"] != bundle.get("scope_hash")
            or cursor["mapping_version"] != bundle.get("aliases", {}).get("version")
            or cursor["content_hash"] != bundle.get("detail_sha256")
            or type(cursor["offset"]) is not int or cursor["offset"] < 0):
        _bad("context_cursor_stale", "Cursor does not match this context, scope, and source", 3)
    return cursor["offset"]


def _utf8_end(data: bytes, start: int, end: int) -> int:
    while end > start:
        try:
            data[start:end].decode("utf-8")
            return end
        except UnicodeDecodeError:
            end -= 1
    return end


def _line_limited_end(text: str, start: int, end: int, max_lines: int) -> int:
    lines = 1
    index = start
    while index < end:
        if text[index] == "\r":
            if lines == max_lines:
                return index + (2 if index + 1 < end and text[index + 1] == "\n" else 1)
            lines += 1
            index += 2 if index + 1 < end and text[index + 1] == "\n" else 1
        elif text[index] == "\n":
            if lines == max_lines:
                return index + 1
            lines += 1
            index += 1
        else:
            index += 1
    return end


def paginate_context_detail(bundle: Mapping[str, Any], access: Mapping[str, Any], current_source_pin: Mapping[str, Any],
                            cursor: str, *, max_bytes: int, max_lines: int) -> dict:
    """Pure detail pagination. A future handler must authorize before calling this."""
    if not isinstance(bundle, Mapping) or not isinstance(access, Mapping):
        _bad("context_input_invalid", "Stored bundle and access references are required")
    expected_projection = fingerprint({key: value for key, value in bundle.items() if key != "projection_hash"})
    if expected_projection != bundle.get("projection_hash"):
        _bad("context_bundle_corrupt", "Stored context projection hash does not match", 4)
    binding = bundle.get("scope_binding", {})
    if (access.get("scope_id") != binding.get("scope_id")
            or access.get("authorization_ref") != binding.get("authorization_ref")
            or access.get("run_id") != binding.get("run_id")):
        _bad("context_scope_conflict", "Context belongs to another scope or authorization", 3)
    verify_source_pin(bundle["source_pin"], current_source_pin)
    if type(max_bytes) is not int or not 1 <= max_bytes <= 64 * 1024:
        _bad("context_budget_invalid", "max_bytes must be between 1 and 65536")
    if type(max_lines) is not int or not 1 <= max_lines <= 500:
        _bad("context_budget_invalid", "max_lines must be between 1 and 500")
    text = bundle.get("detail_text")
    if not isinstance(text, str) or not text:
        _bad("context_detail_unavailable", "No omitted stored context is available")
    raw = text.encode("utf-8")
    if hashlib.sha256(raw).hexdigest() != bundle.get("detail_sha256"):
        _bad("context_detail_corrupt", "Stored detail hash does not match", 4)
    start = _read_cursor(cursor, bundle)
    if start > len(raw):
        _bad("context_cursor_invalid", "Cursor exceeds stored context detail", 3)
    try:
        prefix = raw[:start].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PmtError("context_cursor_invalid", "Cursor splits a UTF-8 character", 3) from exc
    end = _utf8_end(raw, start, min(len(raw), start + max_bytes))
    if end == start and start < len(raw):
        _bad("context_budget_invalid", "Byte limit is too small for the next UTF-8 character")
    page_text = raw[start:end].decode("utf-8")
    line_end = _line_limited_end(page_text, 0, len(page_text), max_lines)
    page_text = page_text[:line_end]
    page = page_text.encode("utf-8")
    actual_end = start + len(page)
    next_cursor = (_cursor(bundle["context_id"], bundle["source_pin"]["source_hash"], bundle["scope_hash"],
                           bundle["aliases"]["version"], bundle["detail_sha256"], actual_end)
                   if actual_end < len(raw) else None)
    return {"context_ref": {"kind": "task_context", "id": bundle["context_id"],
                            "source_hash": bundle["source_pin"]["source_hash"],
                            "projection_hash": bundle["projection_hash"], "version": bundle["schema_version"]},
            "start_byte": start, "end_byte": actual_end,
            "start_line": prefix.count("\n") + prefix.count("\r") - prefix.count("\r\n") + 1,
            "content": page_text, "content_sha256": hashlib.sha256(page).hexdigest(),
            "available_end_byte": len(raw), "available_end_line": text.count("\n") + 1,
            "next_cursor": next_cursor}


def resolve_alias_in_bundle(bundle: Mapping[str, Any], access: Mapping[str, Any], alias: str, *,
                            expected_source_pin: Mapping[str, Any], expected_mapping_version: int) -> dict:
    """Resolve a short ID only inside its immutable owner/source-bound context."""
    if not isinstance(bundle, Mapping) or not isinstance(access, Mapping):
        _bad("context_alias_invalid", "Bundle and access references are required")
    verify_source_pin(bundle["source_pin"], expected_source_pin)
    binding = bundle.get("scope_binding", {})
    if (access.get("scope_id") != binding.get("scope_id")
            or access.get("authorization_ref") != binding.get("authorization_ref")
            or access.get("run_id") != binding.get("run_id")):
        _bad("context_scope_conflict", "Alias map belongs to another authorization", 3)
    mapping = bundle.get("aliases", {})
    if expected_mapping_version != ALIAS_MAP_VERSION or mapping.get("version") != expected_mapping_version:
        _bad("context_alias_stale", "Alias mapping version is no longer supported", 3)
    expected_hash = mapping.get("mapping_hash")
    unhashed = {key: value for key, value in mapping.items() if key != "mapping_hash"}
    if expected_hash != fingerprint(unhashed):
        _bad("context_alias_corrupt", "Alias map integrity hash does not match", 4)
    if not isinstance(alias, str) or alias not in mapping.get("aliases", {}):
        _bad("context_alias_unknown", "Alias is not present in this context map", 3)
    return {"alias": alias, "canonical_id": mapping["aliases"][alias],
            "context_id": bundle["context_id"], "source_hash": bundle["source_pin"]["source_hash"],
            "mapping_version": expected_mapping_version, "scope_hash": bundle["scope_hash"]}


def compare_resume_metadata(previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict:
    """Compare a prior summary to explicitly supplied current validation results.

    A production resume adapter must obtain `current` from fresh source, owner,
    scope, and evidence callbacks; the previous summary is never authority.
    """
    if not isinstance(previous, Mapping) or not isinstance(current, Mapping):
        _bad("context_resume_invalid", "Prior and current validation metadata are required")
    prior_pin = previous.get("source_pin")
    latest_pin = current.get("source_pin")
    checks = {
        "source": "unknown", "scope": "unknown", "owner": "unknown", "evidence": "unknown",
    }
    if isinstance(prior_pin, Mapping) and isinstance(latest_pin, Mapping):
        checks["source"] = "same" if pin_source(prior_pin).source_hash == pin_source(latest_pin).source_hash else "changed"
    previous_scope = previous.get("scope_binding", {})
    current_scope = current.get("scope_binding", {})
    if isinstance(previous_scope, Mapping) and isinstance(current_scope, Mapping):
        checks["scope"] = "same" if (previous_scope.get("scope_id") == current_scope.get("scope_id")
                                       and previous_scope.get("authorization_ref") == current_scope.get("authorization_ref")
                                       and previous_scope.get("claim_ref") == current_scope.get("claim_ref")) else "changed"
    authority = current.get("authority_check")
    if isinstance(authority, Mapping) and authority.get("checked") is True:
        checks["owner"] = "same" if authority.get("allowed") is True and authority.get("owner_matches") is True else "denied"
    evidence = current.get("evidence_checks")
    prior_evidence = previous.get("evidence_refs", [])
    if isinstance(evidence, Mapping) and isinstance(prior_evidence, list):
        statuses = [evidence.get(ref) for ref in prior_evidence]
        checks["evidence"] = "valid" if all(status == "valid" for status in statuses) else (
            "invalid" if any(status == "invalid" for status in statuses) else "unknown")
    if "denied" in checks.values() or "changed" in checks.values() or "invalid" in checks.values():
        status, next_action = "stale", "rebuild_after_current_source_and_authority_review"
    elif all(value in {"same", "valid"} for value in checks.values()):
        status, next_action = "ready_to_rebuild", "build_a_new_context_from_current_source"
    else:
        status, next_action = "unknown", "recheck_source_scope_owner_and_evidence"
    return {"status": status, "checks": checks, "next_action": next_action,
            "previous_context_authoritative": False, "criteria_verdict": "not_evaluated"}


def _actual_authorized_snapshot(db, conn, req, *, task_ref, repository_id, relative_graph_path,
                                expected_source, workspace):
    """Local adapter: DB run/Step/lock proof first, then F3's fresh Git source helper."""
    from pathlib import Path

    from ..execution import service as execution
    from ..phase2_common import project_scope_id, require_workspace_claim
    from . import graph

    scope_id = _canonical_id(req.get("scope_id") or _payload(req).get("scope_id"), "scope_id")
    expected = pin_source(expected_source)
    if expected.project_id != scope_id or expected.repository_id != repository_id:
        _bad("source_conflict", "Expected SourcePin does not match the selected project and repository", 3)
    run = execution._get_run(conn, task_ref["run_id"])
    execution._owned(req, run)
    expected_run_revision = _payload(req).get("expected_run_revision")
    if expected_run_revision is not None and (type(expected_run_revision) is not int
                                               or expected_run_revision != run["revision"]):
        _bad("execution_revision_conflict", "Context run revision changed", 3)
    if run["step_id"] != task_ref["step_id"] or run["state"] not in {
            "starting", "running", "review_pending", "reconciling", "cancel_requested"}:
        _bad("context_run_unavailable", "An active owner-bound run for this Step is required", 3)
    step = execution._step(conn, task_ref["step_id"])
    execution._validate_current_step(conn, run, step)
    if (step["directive_version"] != run["directive_version"]
            or step["state"] in {"Done", "Canceled"}
            or json.loads(step["body_json"]).get("invalidated") is True):
        _bad("directive_version_conflict", "Run does not own an active current Step directive", 3)
    if project_scope_id(conn, step["scope_id"]) != scope_id:
        _bad("context_scope_mismatch", "Step belongs to another project", 3)
    _check_task_ancestry(conn, task_ref["task_id"], task_ref["step_id"], scope_id)
    trusted_workspace = getattr(db, "workspace_context_port", None)
    if trusted_workspace is not None:
        # HostDatabaseView injects this port after authenticated Host scope
        # authorization. It validates the canonical URI and current P2 claim,
        # then supplies the server's pinned client snapshot; no Path operation
        # or local checkout access is performed in this branch.
        if (_payload(req).get("project_id", scope_id) != scope_id
                or _payload(req).get("canonical_workspace", workspace) != workspace):
            _bad("context_authority_mismatch", "Canonical project/workspace binding does not match the current run", 3)
        return trusted_workspace.authorize_context(
            conn, req, task_ref=task_ref, repository_id=repository_id,
            relative_graph_path=relative_graph_path, expected_source=expected_source,
            workspace=workspace)
    if not isinstance(workspace, str) or not workspace or not Path(workspace).is_absolute():
        _bad("invalid_workspace", "A local workspace mapping is required")
    # Check current owner and lock coverage before the Git helper reads any source file.
    require_workspace_claim(db, conn, req, workspace, [relative_graph_path])
    from ..phase2_common import normalized_workspace
    if normalized_workspace(workspace) != normalized_workspace(step["workspace"]):
        _bad("ownership_conflict", "Workspace does not match the current Step mapping", 3)
    source_req = {**req, "scope_id": scope_id,
                  "payload": {**_payload(req), "repository_id": repository_id,
                              "workspace": workspace, "relative_graph_path": relative_graph_path,
                              "run_id": task_ref["run_id"]}}
    source = graph._source_graph(db, conn, source_req)
    verify_source_pin(expected, source["source_pin"])
    if source["scope_id"] != scope_id or source["repository_id"] != repository_id:
        _bad("source_conflict", "Current graph source does not match this project", 3)
    return {"source": source, "run": run, "step": step, "scope_id": scope_id,
            "repository_id": repository_id, "relative_graph_path": relative_graph_path,
            "workspace": workspace, "task_ref": dict(task_ref),
            "scope_locks": _current_locks(conn, run["id"])}


def _current_locks(conn, run_id):
    return [dict(row) for row in conn.execute(
        "SELECT kind,workspace,resource,owner_session FROM scope_locks WHERE run_id=? ORDER BY lock_key",
        (run_id,)).fetchall()]


def _make_access_ref(req, authorized):
    locks = _current_locks_from_authorized(authorized)
    lock_digest = fingerprint(sorted(
        [{"kind": row["kind"], "resource": row["resource"],
          "workspace_hash": fingerprint(row["workspace"]), "owner_session": row["owner_session"]}
         for row in locks], key=lambda item: (item["kind"], item["resource"], item["workspace_hash"])))
    run, step = authorized["run"], authorized["step"]
    return {"scope_id": authorized["scope_id"],
            "authorization_ref": fingerprint({"actor": req["actor"], "session": req["session_id"],
                                              "run_id": run["id"], "step_id": step["id"],
                                              "directive_version": step["directive_version"],
                                              "lock_hash": lock_digest}),
            "claim_ref": fingerprint({"run_id": run["id"], "lock_hash": lock_digest}),
            "run_id": run["id"]}


def _current_locks_from_authorized(authorized):
    return authorized["scope_locks"]


def _private_directive(db, conn, req, authorized):
    from ..resources import check_artifact
    from ..steps import handle as steps_handle

    step = authorized["step"]
    directive_row = conn.execute("SELECT scope_id,sha256,state FROM artifacts WHERE id=?",
                                 (step["directive_id"],)).fetchone()
    from ..phase2_common import project_scope_id
    if (not directive_row or directive_row["state"] != "ready"
            or project_scope_id(conn, directive_row["scope_id"]) != authorized["scope_id"]
            or not check_artifact(db, conn, step["directive_id"])["valid"]):
        _bad("directive_unavailable", "Current private directive resource failed verification", 3)
    directive_req = {**req, "record_id": step["id"],
                     "payload": {**_payload(req), "run_id": authorized["run"]["id"]},
                     "operation": "read_step_directive"}
    result = steps_handle(db, conn, directive_req)
    if result.get("directive_version") != step["directive_version"]:
        _bad("directive_version_conflict", "Private directive version changed during projection", 3)
    criteria = __import__("pmt.execution.service", fromlist=["_json"])._json(
        step["criteria_json"], "Step criteria", list)
    return result["directive"], criteria, directive_row["sha256"]


def _fresh_graph_slice(db, conn, authorized, req, role, directive, impact_set=None):
    from . import graph

    index_row, index_body = graph._get_current_index(db, conn, authorized["scope_id"],
                                                    authorized["source"]["source_pin"])
    node_ids = _selected_nodes(_payload(req), directive, index_body, authorized["task_ref"])
    if isinstance(impact_set, Mapping):
        available = set(index_body.get("nodes", {}))
        node_ids = list(dict.fromkeys(node_ids + [item["node_id"] for item in impact_set.get("known", [])
                                                    if isinstance(item, Mapping)
                                                    and item.get("node_id") in available]))
        if len(node_ids) > 200:
            node_ids = node_ids[:200]
            impact_set = dict(impact_set)
            impact_set["unknown"] = list(impact_set.get("unknown", [])) + [{
                "kind": "graph", "reason_code": "context_node_selector_limit", "severity": "required"}]
            impact_set["complete"] = False
    relation_ids = _payload(req).get("relation_ids", [])
    if not isinstance(relation_ids, list) or len(relation_ids) > 200:
        _bad("graph_query_invalid", "relation_ids must be a bounded array")
    query = graph_query_for_context(role, node_ids=node_ids, page_size=100,
                                    cursor=_payload(req).get("graph_cursor"))
    if relation_ids:
        query["relation_ids"] = [_canonical_id(item, "relation_id") for item in relation_ids]
    slice_body = graph.query_graph_slice(index_body, query, authorized["source"]["source_pin"])
    slice_body["index"]["revision"] = index_row["revision"]
    return slice_body, index_row, index_body


def _actual_evidence(db, conn, scope_id, directive, criteria, graph_slice):
    refs = _collect_evidence_refs(criteria, graph_slice)
    checked = _verify_evidence_refs(db, conn, scope_id, refs)
    return checked


def _run_result_refs(run):
    result = run.get("result_json")
    if not result:
        return []
    try:
        body = json.loads(result)
    except (TypeError, ValueError):
        return []
    if not isinstance(body, dict):
        return []
    refs = [ref for ref in body.get("evidence_refs", []) if isinstance(ref, str) and ref]
    if isinstance(body.get("receipt_ref"), str) and body["receipt_ref"]:
        refs.append(body["receipt_ref"])
    for criterion in body.get("criteria_results", []) if isinstance(body.get("criteria_results"), list) else []:
        if isinstance(criterion, dict) and isinstance(criterion.get("evidence_refs"), list):
            refs.extend(ref for ref in criterion["evidence_refs"] if isinstance(ref, str) and ref)
    return list(dict.fromkeys(refs))


def _wire_context_inputs(db, conn, req, payload):
    from . import graph

    task_ref = _context_ids(payload)
    role = payload.get("role")
    if not isinstance(role, str) or not _ROLE_NAME.fullmatch(role):
        _bad("context_input_invalid", "role must be a bounded role identifier")
    budget = _context_budget(payload.get("budget"))
    expected_source = pin_source(payload.get("expected_source"))
    repository_id = _canonical_id(payload.get("repository_id"), "repository_id")
    relative_graph_path = _workspace_graph_path(payload.get("relative_graph_path"))
    workspace = payload.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip():
        _bad("invalid_workspace", "workspace is required")
    authorized = _actual_authorized_snapshot(db, conn, req, task_ref=task_ref,
                                             repository_id=repository_id,
                                             relative_graph_path=relative_graph_path,
                                             expected_source=expected_source,
                                             workspace=workspace)
    directive, criteria, directive_hash = _private_directive(db, conn, req, authorized)
    impact_set = None
    impact_required = "impact_request" in payload
    if impact_required:
        impact_req = payload.get("impact_request")
        if not isinstance(impact_req, Mapping) or set(impact_req) != {"change_preview", "change_set"}:
            _bad("context_impact_invalid", "impact_request requires change_preview and change_set")
        try:
            impact_set = graph._calculate_impact(db, conn,
                {**req, "payload": {"expected_source": authorized["source"]["source_pin"].to_dict(),
                                    "change_preview": impact_req["change_preview"],
                                    "change_set": impact_req["change_set"]}},
                {"graph": authorized["source"]["graph"], "source_pin": authorized["source"]["source_pin"],
                 "scope_id": authorized["scope_id"]})
        except PmtError as exc:
            preview = impact_req.get("change_preview") if isinstance(impact_req, Mapping) else None
            if not isinstance(preview, Mapping):
                raise
            raw_hash = preview.get("change_set_hash")
            if not isinstance(raw_hash, str) or len(raw_hash) != 64 or any(c not in "0123456789abcdef" for c in raw_hash):
                raise
            change_id = _canonical_id(preview.get("change_id"), "impact_request.change_preview.change_id")
            current_pin = authorized["source"]["source_pin"].to_dict()
            impact_set = {"change_id": change_id, "change_set_hash": raw_hash,
                          "source_pin": current_pin, "before_source_pin": current_pin,
                          "expected_new_source": {"graph_schema": current_pin["graph_schema"],
                                                  "graph_revision": current_pin["graph_revision"],
                                                  "graph_hash": current_pin["graph_hash"]},
                          "field_changes": [], "known": [], "documents": [], "steps": [],
                          "verifications": [], "unknown": [{"kind": "impact", "reason_code": exc.code,
                                                               "severity": "required"}], "complete": False}
    graph_slice, index_row, index_body = _fresh_graph_slice(db, conn, authorized, req, role, directive, impact_set)
    raw_refs = list(dict.fromkeys(_collect_evidence_refs(criteria, graph_slice)
                                  + _run_result_refs(authorized["run"])))
    evidence_refs = _verify_evidence_refs(db, conn, authorized["scope_id"], raw_refs)
    source_ref = {"repository_id": repository_id, "relative_graph_path": relative_graph_path,
                  "directive_id": authorized["step"]["directive_id"],
                  "directive_version": authorized["step"]["directive_version"],
                  "directive_sha256": directive_hash,
                  "criteria_sha256": fingerprint(criteria),
                  "graph_index_hash": index_body["index_hash"],
                  "graph_index_revision": index_row["revision"],
                  "receipt_refs": _run_result_refs(authorized["run"])}
    scope_locks = authorized["scope_locks"]
    access = _make_access_ref(req, authorized)
    context_id = str(uuid.uuid5(uuid.UUID(req["request_id"]), "pmt-context"))
    built = project_task_context({"request_id": req["request_id"], "context_id": context_id,
                                  "task_ref": task_ref, "role": role,
                                  "source_pin": authorized["source"]["source_pin"].to_dict(),
                                  "access": access, "directive": directive,
                                  "criteria": criteria, "graph_slice": graph_slice,
                                  "evidence_refs": evidence_refs, "budget": budget,
                                  "impact_set": impact_set, "impact_required": impact_required,
                                  "source_ref": source_ref})
    # Confirm no owner/revision/lock/source/index change occurred during the projection.
    current_run = __import__("pmt.execution.service", fromlist=["_get_run"])._get_run(conn, authorized["run"]["id"])
    current_step = __import__("pmt.execution.service", fromlist=["_step"])._step(conn, authorized["step"]["id"])
    if (current_run["revision"] != authorized["run"]["revision"]
            or current_run["owner_session"] != req["session_id"]
            or current_step["directive_version"] != authorized["step"]["directive_version"]
            or fingerprint(_current_locks(conn, authorized["run"]["id"])) != fingerprint(scope_locks)):
        _bad("context_authority_changed", "Run, directive, or scope claim changed during context build", 3)
    fresh = graph._source_graph(db, conn, {**req, "scope_id": authorized["scope_id"],
              "payload": {**payload, "repository_id": repository_id, "workspace": workspace,
                          "relative_graph_path": relative_graph_path, "run_id": task_ref["run_id"]}})
    verify_source_pin(authorized["source"]["source_pin"], fresh["source_pin"])
    fresh_index_row, fresh_index = graph._get_current_index(db, conn, authorized["scope_id"], fresh["source_pin"])
    if (fresh_index["index_hash"] != index_body["index_hash"]
            or fresh_index_row["revision"] != index_row["revision"]):
        _bad("context_source_changed", "F2 index changed during context build", 3)
    return built, authorized


def _response_for_build(db, req, payload, built, authorized, *, resume_report=None):
    from ..execution import service as execution
    from ..service import response

    bundle, result = built["bundle"], built["response"]
    # Context IDs and alias maps are new for each request; old aliases are never activated.
    result["context_ref"]["scope_id"] = authorized["scope_id"]
    if resume_report is not None:
        result["resume"] = resume_report
    _apply_wire_budget(built, req["request_id"])
    storage = Phase3Storage(db)

    def commit(conn, request):
        run = execution._get_run(conn, authorized["run"]["id"])
        execution._owned(request, run)
        step = execution._step(conn, authorized["step"]["id"])
        locks = _current_locks(conn, run["id"])
        if (run["revision"] != authorized["run"]["revision"]
                or run["state"] not in {"starting", "running", "review_pending", "reconciling", "cancel_requested"}
                or step["directive_version"] != authorized["step"]["directive_version"]
                or fingerprint(locks) != fingerprint(authorized["scope_locks"])):
            _bad("context_authority_changed", "Run, Step, or workspace claim changed before cache commit", 3)
        receipt = storage.put_object("task_context", bundle["context_id"], authorized["scope_id"],
                                     request["actor"], request["session_id"],
                                     authorized["source"]["source_pin"].source_hash, 0, bundle,
                                     state="ready", request_id=request["request_id"],
                                     event_id=str(uuid.uuid5(uuid.UUID(request["request_id"]), "task_context_built")),
                                     conn=conn)
        if receipt["revision"] != 1:
            _bad("context_revision_conflict", "Context cache was not created at revision 1", 3)
        # The generic request replay cache keeps only a private resource receipt,
        # never the projected context text itself.
        return {"context_ref": {"kind": "task_context", "id": bundle["context_id"],
                                "scope_id": authorized["scope_id"],
                                "source_hash": bundle["source_pin"]["source_hash"],
                                "projection_hash": bundle["projection_hash"],
                                "revision": receipt["revision"], "version": bundle["schema_version"]},
                "stored": True}

    envelope, code = db.run_request(req, commit)
    if code != 0 or not envelope.get("ok"):
        return envelope, code
    receipt = envelope.get("result") or {}
    context_receipt = receipt.get("context_ref") or {}
    stored = storage.get_object("task_context", bundle["context_id"], authorized["scope_id"],
                                req["actor"], req["session_id"])
    if (not stored or stored.get("source_hash") != authorized["source"]["source_pin"].source_hash
            or stored.get("body", {}).get("projection_hash") != bundle.get("projection_hash")
            or context_receipt.get("id") != bundle["context_id"]):
        _bad("context_receipt_invalid", f"Private context receipt failed read-back verification ({context_receipt.get('id')} != {bundle['context_id']})", 4,
             {"stored": bool(stored),
              "source_match": bool(stored and stored.get("source_hash") == authorized["source"]["source_pin"].source_hash),
              "projection_match": bool(stored and stored.get("body", {}).get("projection_hash") == bundle.get("projection_hash")),
              "receipt_match": context_receipt.get("id") == bundle["context_id"],
              "receipt_id": context_receipt.get("id"), "context_id": bundle["context_id"]})
    _apply_wire_budget(built, req["request_id"])
    # Restore the same bounded public response after a receipt-only request replay.
    return response(req["request_id"], result=result), 0


def _resume_report(db, req, scope_id, previous_id, previous, authorized, bundle):
    if not previous:
        return {"status": "unknown", "next_action": "build_fresh_context_no_prior_context_access",
                "previous_context_authoritative": False, "old_aliases_reactivated": False,
                "new_context_id": bundle["context_id"], "criteria_verdict": "not_evaluated"}
    prior_evidence = _bundle_evidence_refs(previous)
    prior_refs = [item["ref"] for item in prior_evidence
                  if isinstance(item, Mapping) and isinstance(item.get("ref"), str)]
    with closing(db.connect()) as conn:
        checked = _verify_evidence_refs(db, conn, scope_id, prior_refs)
    checked_by_ref = {item["ref"]: item for item in checked}
    statuses = {}
    for item in prior_evidence:
        if not isinstance(item, Mapping) or not isinstance(item.get("ref"), str):
            continue
        now = checked_by_ref.get(item["ref"], {"status": "unknown"})
        if item.get("status") == "valid" and (now.get("status") != "valid" or now.get("sha256") != item.get("sha256")):
            statuses[item["ref"]] = "invalid"
        else:
            statuses[item["ref"]] = now.get("status", "unknown")
    report = compare_resume_metadata(
        {"source_pin": previous["source_pin"], "scope_binding": previous["scope_binding"],
         "evidence_refs": prior_refs},
        {"source_pin": authorized["source"]["source_pin"].to_dict(),
         "scope_binding": _make_access_ref(req, authorized),
         "authority_check": {"checked": True, "allowed": True,
                             "owner_matches": authorized["run"]["owner_session"] == req["session_id"]},
         "evidence_checks": statuses})
    old_task = previous.get("task_ref", {})
    if (old_task.get("task_id") != authorized["task_ref"]["task_id"]
            or old_task.get("step_id") != authorized["task_ref"]["step_id"]):
        report["status"] = "stale"
        report["checks"]["task"] = "changed"
        report["next_action"] = "review_task_identity_and_build_fresh_context"
    report.update({"old_context_id": previous_id, "new_context_id": bundle["context_id"],
                   "old_aliases_reactivated": False, "criteria_verdict": "not_evaluated"})
    return report


def _execute_context_operation(db, req, *, resume=False):
    from ..service import response

    payload = _strict_payload(req, _BUILD_FIELDS)
    previous_id = payload.get("previous_context_id") if resume else None
    if not resume and payload.get("previous_context_id") is not None:
        _bad("context_input_invalid", "previous_context_id is valid only for resume_task_context")
    if resume and previous_id is not None:
        previous_id = _canonical_id(previous_id, "previous_context_id")
    if resume and previous_id is None:
        _bad("context_resume_invalid", "resume_task_context requires a previous_context_id")
    with closing(db.connect()) as conn:
        built, authorized = _wire_context_inputs(db, conn, req, payload)
    previous = None
    if resume:
        previous = Phase3Storage(db).get_object("task_context", previous_id, authorized["scope_id"],
                                                 req["actor"], req["session_id"])
        previous = previous["body"] if previous else None
        report = _resume_report(db, req, authorized["scope_id"], previous_id,
                                previous, authorized, built["bundle"])
    else:
        report = None
    result, code = _response_for_build(db, req, payload, built, authorized, resume_report=report)
    if code != 0:
        return result, code
    db.diagnostics.emit("context.resume_revalidated" if resume else "context.built",
                        request_id=req["request_id"], scope_id=authorized["scope_id"],
                        step_id=authorized["task_ref"]["step_id"], context_id=built["bundle"]["context_id"],
                        source_hash=authorized["source"]["source_pin"].source_hash,
                        count=len(built["response"].get("included", [])),
                        incomplete=built["response"].get("incomplete"),
                        input_bytes=built["bundle"].get("detail_size_bytes", 0))
    return result, code


def execute_file(db, req):
    from ..service import response
    try:
        if req.get("operation") == "build_task_context":
            return _execute_context_operation(db, req, resume=False)
        if req.get("operation") == "resume_task_context":
            return _execute_context_operation(db, req, resume=True)
        _bad("context_operation_unsupported", "Unsupported context file operation")
    except PmtError as exc:
        return response(req.get("request_id"), error=exc.as_dict()), exc.exit_code
    except sqlite3.Error as exc:
        issue = db._sqlite_error(exc)
        return response(req.get("request_id"), error=issue.as_dict()), issue.exit_code
    except OSError as exc:
        error = PmtError("context_source_io", "Authorized context source could not be read", 4, True,
                         {"errno": getattr(exc, "errno", None)})
        return response(req.get("request_id"), error=error.as_dict()), error.exit_code
    except (TypeError, ValueError):
        error = PmtError("context_input_invalid", "Context input could not be processed safely")
        return response(req.get("request_id"), error=error.as_dict()), error.exit_code


def _read_context_bundle(db, req, conn):
    context_id = _canonical_id(_strict_payload(req, _DETAIL_FIELDS if req["operation"] == "read_context_detail" else _ALIAS_FIELDS).get("context_id"), "context_id")
    scope_id = _canonical_id(req.get("scope_id") or _payload(req).get("scope_id"), "scope_id")
    value = Phase3Storage(db).get_object("task_context", context_id, scope_id,
                                         req["actor"], req["session_id"], conn=conn)
    if not value or value.get("state") != "ready":
        _bad("context_not_found", "Context is unavailable to this owner and scope", 3)
    bundle = value["body"]
    if (bundle.get("kind") != "task_context" or bundle.get("context_id") != context_id
            or value.get("source_hash") != bundle.get("source_pin", {}).get("source_hash")):
        _bad("context_bundle_corrupt", "Stored context identity or source pin is invalid", 4)
    if fingerprint({key: item for key, item in bundle.items() if key != "projection_hash"}) != bundle.get("projection_hash"):
        _bad("context_bundle_corrupt", "Stored context projection hash does not match", 4)
    return scope_id, context_id, bundle


def _revalidate_context(db, req, conn, scope_id, bundle):
    from ..execution import service as execution
    from ..resources import check_artifact
    from . import graph

    source_ref, task_ref = bundle.get("source_ref"), bundle.get("task_ref")
    if not isinstance(source_ref, Mapping) or not isinstance(task_ref, Mapping):
        _bad("context_bundle_corrupt", "Stored context lacks source or task references", 4)
    run = execution._get_run(conn, task_ref.get("run_id"))
    if run.get("owner_session") != req.get("session_id"):
        _bad("context_authority_changed", "Current Step run no longer belongs to this session", 3)
    step = execution._step(conn, task_ref.get("step_id"))
    current_req = {**req, "scope_id": scope_id, "record_id": task_ref["step_id"],
                   "payload": {"run_id": task_ref["run_id"],
                               "expected_run_revision": run["revision"],
                               "repository_id": source_ref["repository_id"],
                               "workspace": step["workspace"],
                               "relative_graph_path": source_ref["relative_graph_path"]}}
    authorized = _actual_authorized_snapshot(db, conn, current_req,
                    task_ref={"task_id": task_ref["task_id"], "step_id": task_ref["step_id"],
                              "run_id": task_ref["run_id"]},
                    repository_id=source_ref["repository_id"],
                    relative_graph_path=source_ref["relative_graph_path"],
                    expected_source=bundle["source_pin"], workspace=step["workspace"])
    directive, criteria, directive_hash = _private_directive(db, conn, current_req, authorized)
    directive_row = conn.execute("SELECT sha256,state FROM artifacts WHERE id=?",
                                 (authorized["step"]["directive_id"],)).fetchone()
    if (not directive_row or directive_row["state"] != "ready"
            or authorized["step"]["directive_id"] != source_ref.get("directive_id")
            or authorized["step"]["directive_version"] != source_ref.get("directive_version")
            or directive_hash != source_ref.get("directive_sha256")
            or fingerprint(criteria) != source_ref.get("criteria_sha256")
            or not check_artifact(db, conn, authorized["step"]["directive_id"])["valid"]):
        _bad("context_source_stale", "Current private Step directive or criteria changed", 3)
    index_row, index_body = graph._get_current_index(db, conn, scope_id, authorized["source"]["source_pin"])
    if (index_body.get("index_hash") != source_ref.get("graph_index_hash")
            or index_row["revision"] != source_ref.get("graph_index_revision")):
        _bad("context_source_stale", "Current F2 graph index changed since context creation", 3)
    authorized["scope_locks"] = _current_locks(conn, authorized["run"]["id"])
    current_access = _make_access_ref(req, authorized)
    if current_access != bundle.get("scope_binding"):
        _bad("context_scope_stale", "Current owner session or workspace claim changed", 3)
    evidence_section = next((section for section in bundle.get("sections", [])
                             if section.get("section_id") == "evidence_refs"), None)
    evidence_refs = evidence_section.get("value", []) if isinstance(evidence_section, Mapping) else []
    valid_refs = [item for item in evidence_refs if isinstance(item, Mapping) and item.get("status") == "valid"]
    current_evidence = _verify_evidence_refs(db, conn, scope_id,
                                             [item["ref"] for item in valid_refs if isinstance(item.get("ref"), str)])
    for prior, current in zip(valid_refs, current_evidence):
        if current.get("status") != "valid" or current.get("sha256") != prior.get("sha256"):
            _bad("context_evidence_stale", "Verified evidence is no longer available at the same hash", 3)
    return {"source_pin": authorized["source"]["source_pin"], "access": current_access,
            "directive": directive, "criteria": criteria, "index": index_body,
            "run": authorized["run"], "step": authorized["step"], "evidence": current_evidence}


def _detail_wire_result(result, req):
    envelope = {"protocol_version": 1, "request_id": req["request_id"], "ok": True,
                "result": result, "error": None, "warnings": []}
    wire_bytes = len(canonical_json(envelope).encode("utf-8"))
    result["delivery_measurement"] = {
        "content_bytes": len(result["content"].encode("utf-8")),
        "serialized_response_bytes": wire_bytes,
        "metadata_overhead_bytes": max(0, wire_bytes - len(result["content"].encode("utf-8"))),
        "serialized_lines": 1,
        "content_lines": max(1, result["content"].count("\n") + 1) if result["content"] else 0,
    }
    return result


def handle(db, conn, req):
    operation = req.get("operation")
    allowed = (_TASK_CONTEXT_FIELDS if operation == "read_task_context" else
               _DETAIL_FIELDS if operation == "read_context_detail" else _ALIAS_FIELDS)
    payload = _strict_payload(req, allowed)
    if operation == "read_task_context":
        return _read_task_context(db, req, conn, payload)
    scope_id, context_id, bundle = _read_context_bundle(db, req, conn)
    current = _revalidate_context(db, req, conn, scope_id, bundle)
    if operation == "read_context_detail":
        result = paginate_context_detail(bundle, current["access"], current["source_pin"].to_dict(),
                                        payload.get("cursor"), max_bytes=payload.get("max_bytes"),
                                        max_lines=payload.get("max_lines"))
        return _detail_wire_result(result, req)
    if operation == "resolve_context_alias":
        return resolve_alias_in_bundle(bundle, current["access"], payload.get("alias"),
                                       expected_source_pin=current["source_pin"].to_dict(),
                                       expected_mapping_version=payload.get("mapping_version"))
    _bad("context_operation_unsupported", "Context read operation is unsupported")


def _read_task_context(db, req, conn, payload):
    """Return a current-owner, source-checked bounded projection for a consumer."""
    ref = payload.get("context_ref")
    if not isinstance(ref, Mapping):
        _bad("context_ref_invalid", "context_ref is required")
    required = {"kind", "id", "scope_id", "source_hash", "version", "projection_hash"}
    if set(ref) != required or ref.get("kind") != "task_context":
        _bad("context_ref_invalid", "context_ref must contain the exact task-context binding")
    context_id = _canonical_id(ref.get("id"), "context_ref.id")
    scope_id = _canonical_id(ref.get("scope_id"), "context_ref.scope_id")
    request_scope = _canonical_id(req.get("scope_id") or payload.get("scope_id"), "scope_id")
    if scope_id != request_scope:
        _bad("context_scope_mismatch", "Context reference scope does not match request scope", 3)
    value = Phase3Storage(db).get_object("task_context", context_id, scope_id,
                                         req["actor"], req["session_id"], conn=conn)
    if not value or value.get("state") != "ready":
        _bad("context_not_found", "Context is unavailable to this owner and scope", 3)
    bundle = value["body"]
    expected_ref = {"kind": "task_context", "id": context_id, "scope_id": scope_id,
                    "source_hash": value.get("source_hash"), "version": bundle.get("schema_version"),
                    "projection_hash": bundle.get("projection_hash")}
    if dict(ref) != expected_ref:
        _bad("context_ref_stale", "Context reference does not match the stored owner-bound object", 3)
    if (bundle.get("kind") != "task_context" or bundle.get("context_id") != context_id
            or fingerprint({key: item for key, item in bundle.items() if key != "projection_hash"})
            != bundle.get("projection_hash")):
        _bad("context_bundle_corrupt", "Stored context projection hash does not match", 4)
    current = _revalidate_context(db, req, conn, scope_id, bundle)
    response = bundle.get("bounded_projection")
    if not isinstance(response, Mapping):
        _bad("context_bundle_corrupt", "Stored bounded projection is unavailable", 4)
    # Only expose sections that were part of this role- and byte-bounded projection.
    metadata = {"context_ref": expected_ref,
                "incomplete": response.get("incomplete") is True,
                "mandatory_omissions": response.get("omitted_required", []) + response.get("missing_required", []),
                "unknown": response.get("unknown", []),
                "budget": response.get("budget"), "task": bundle.get("task_ref"),
                "role": bundle.get("role"), "source": bundle.get("source_pin"),
                "private_content_ref": {"kind": "task_context_projection", "id": context_id,
                                         "projection_hash": expected_ref["projection_hash"]},
                "current_authority": {"run_id": current["run"]["id"],
                                      "owner_checked": True,
                                      "source_hash": current["source_pin"].source_hash}}
    projected = {"included": response.get("included", []),
                 "aliases": response.get("aliases", {}),
                 "completeness": bundle.get("completeness")}
    return {**metadata, "projection": projected}


def build_task_context(db, req):
    return _execute_context_operation(db, req, resume=False)


def resume_task_context(db, req):
    return _execute_context_operation(db, req, resume=True)


def _measure_public_response(result, request_id):
    envelope = {"protocol_version": 1, "request_id": request_id, "ok": True,
                "result": result, "error": None, "warnings": []}
    wire = canonical_json(envelope)
    return len(wire.encode("utf-8")), len(wire.splitlines())


def _refresh_projection_hash(bundle):
    bundle.pop("projection_hash", None)
    bundle["projection_hash"] = fingerprint(bundle)
    return bundle["projection_hash"]


def _apply_wire_budget(built, request_id):
    """Measure the complete protocol response rather than just included sections."""
    bundle, result = built["bundle"], built["response"]
    content_bytes = sum(section["size_bytes"] for section in result["included"])
    content_lines = sum(section["line_count"] for section in result["included"])
    budget = result["budget"]
    budget.update({"content_bytes": content_bytes, "content_lines": content_lines,
                   "measurement_basis": "UTF-8 protocol-v1 envelope bytes; logical source lines for section limit",
                   "serialized_lines": 1})
    budget["used_lines"] = content_lines
    for _ in range(3):
        budget["used_bytes"] = _measure_public_response(result, request_id)[0]
    over_budget = (budget["used_bytes"] > budget["requested"]["max_bytes"]
                   or content_lines > budget["requested"]["max_lines"])
    budget["over_budget"] = over_budget
    if over_budget:
        reason = {"reason_code": "minimum_response_metadata_exceeds_budget",
                  "response_bytes": budget["used_bytes"],
                  "content_lines": content_lines,
                  "max_bytes": budget["requested"]["max_bytes"]}
        bundle["completeness"]["status"] = "incomplete"
        bundle["completeness"]["budget_exceeded"] = reason
        result["incomplete"] = True
        result["budget_exceeded"] = reason
    bundle["bounded_projection"] = {
        "included": result.get("included", []),
        "omitted_required": result.get("omitted_required", []),
        "omitted_optional": result.get("omitted_optional", []),
        "missing_required": result.get("missing_required", []),
        "unknown": result.get("unknown", []),
        "aliases": result.get("alias_map"),
        "budget": result.get("budget"),
        "incomplete": result.get("incomplete") is True,
    }
    for _ in range(4):
        result["context_ref"]["projection_hash"] = _refresh_projection_hash(bundle)
        budget["used_bytes"] = _measure_public_response(result, request_id)[0]
    result["context_ref"]["projection_hash"] = _refresh_projection_hash(bundle)
    return result


_BUILD_FIELDS = {"task_ref", "role", "expected_source", "repository_id", "workspace",
                 "relative_graph_path", "run_id", "node_ids", "relation_ids", "graph_cursor",
                 "budget", "previous_context_id", "impact_request", "canonical_workspace",
                 "project_id", "expected_run_revision"}
_DETAIL_FIELDS = {"context_id", "cursor", "max_bytes", "max_lines"}
_ALIAS_FIELDS = {"context_id", "alias", "mapping_version"}
_TASK_CONTEXT_FIELDS = {"context_ref"}


def _payload(req):
    value = req.get("payload", {})
    if not isinstance(value, dict):
        _bad("context_input_invalid", "payload must be an object")
    return value


def _strict_payload(req, allowed):
    value = _payload(req)
    unknown = set(value) - allowed
    if unknown:
        _bad("unknown_fields", "Unsupported context fields", details={"fields": sorted(unknown)})
    return value


def _context_ids(payload):
    task = payload.get("task_ref")
    if not isinstance(task, Mapping):
        _bad("context_input_invalid", "task_ref must contain task_id, step_id, and run_id")
    task_id = _canonical_id(task.get("task_id"), "task_id")
    step_id = _canonical_id(task.get("step_id"), "step_id")
    run_id = _canonical_id(payload.get("run_id"), "run_id")
    if task.get("run_id") is not None and task["run_id"] != run_id:
        _bad("context_authority_mismatch", "task_ref run_id does not match payload run_id", 3)
    return {"task_id": task_id, "step_id": step_id, "run_id": run_id}


def _check_task_ancestry(conn, task_id, step_id, project_id):
    from ..phase2_common import project_scope_id

    target = conn.execute("SELECT id,scope_id,kind FROM records WHERE id=?", (task_id,)).fetchone()
    step = conn.execute("SELECT id,parent_id,scope_id,kind FROM records WHERE id=?", (step_id,)).fetchone()
    if not target or not step or step["kind"] != "step":
        _bad("context_task_unavailable", "Current Task and Step must exist", 3)
    if project_scope_id(conn, step["scope_id"]) != project_id or project_scope_id(conn, target["scope_id"]) != project_id:
        _bad("context_scope_mismatch", "Task and Step do not belong to the requested project", 3)
    parent = step
    seen = set()
    while parent and parent["id"] not in seen:
        if parent["id"] == task_id:
            return
        seen.add(parent["id"])
        parent = conn.execute("SELECT id,parent_id,scope_id,kind FROM records WHERE id=?",
                              (parent["parent_id"],)).fetchone() if parent["parent_id"] else None
    _bad("context_task_mismatch", "task_id must identify the Step or one of its ancestors", 3)


def _workspace_graph_path(value):
    from pathlib import PurePosixPath

    if not isinstance(value, str) or not value.strip() or "\\" in value or value.startswith("-"):
        _bad("invalid_graph_path", "relative_graph_path must be a workspace-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _bad("invalid_graph_path", "relative_graph_path must stay inside the workspace")
    return path.as_posix()


def _selected_nodes(payload, directive, index_body, task_ref):
    nodes = index_body.get("nodes", {})
    if not isinstance(nodes, dict):
        _bad("graph_index_corrupt", "Current F2 index has no node map", 3)
    supplied = payload.get("node_ids", [])
    if not isinstance(supplied, list) or len(supplied) > 200:
        _bad("graph_query_invalid", "node_ids must be a bounded array")
    selected = {_canonical_id(node_id, "node_id") for node_id in supplied}
    # Step references are selectors only; each is checked against the live F2 index.
    for ref in directive.get("context_refs", []) if isinstance(directive.get("context_refs"), list) else []:
        try:
            candidate = _canonical_id(ref, "context_ref")
        except PmtError:
            continue
        if candidate in nodes:
            selected.add(candidate)
    task_ids = {task_ref["task_id"], task_ref["step_id"]}
    for node_id, node in nodes.items():
        refs = node.get("work_item_step_refs", []) if isinstance(node, dict) else []
        if isinstance(refs, dict):
            refs = [ref for values in refs.values() if isinstance(values, list)
                    for ref in values if isinstance(ref, str)]
        if isinstance(refs, list) and task_ids.intersection(ref for ref in refs if isinstance(ref, str)):
            selected.add(node_id)
    return sorted(selected)


def _collect_evidence_refs(criteria, graph_slice):
    refs = []
    for criterion in criteria:
        if isinstance(criterion, dict) and isinstance(criterion.get("evidence_refs"), list):
            refs.extend(ref for ref in criterion["evidence_refs"] if isinstance(ref, str) and ref)
    for item in graph_slice.get("items", []):
        value = item.get("value", {}) if isinstance(item, dict) else {}
        if isinstance(value, dict) and item.get("entity") == "node" and isinstance(value.get("evidence_refs"), list):
            refs.extend(ref for ref in value["evidence_refs"] if isinstance(ref, str) and ref)
    return list(dict.fromkeys(refs))


def _verify_evidence_refs(db, conn, scope_id, refs):
    from ..phase2_common import project_scope_id
    from ..resources import check_artifact

    checked = []
    for ref in refs:
        try:
            identifier = _canonical_id(ref, "evidence_ref")
        except PmtError:
            checked.append({"ref": ref, "status": "unknown", "reason_code": "reference_not_registered_resource"})
            continue
        row = conn.execute("SELECT scope_id,sha256,state FROM artifacts WHERE id=?", (identifier,)).fetchone()
        if not row or row["state"] != "ready":
            checked.append({"ref": identifier, "status": "unknown", "reason_code": "evidence_unavailable"})
            continue
        try:
            same_project = project_scope_id(conn, row["scope_id"]) == scope_id
        except PmtError:
            same_project = False
        validation = check_artifact(db, conn, identifier) if same_project else {"valid": False}
        if validation.get("valid") and validation.get("sha256") == row["sha256"]:
            checked.append({"ref": identifier, "status": "valid", "sha256": row["sha256"]})
        else:
            checked.append({"ref": identifier, "status": "unknown", "reason_code": "evidence_scope_or_hash_unverified"})
    return checked
