"""Pure validation and deterministic document rendering for Q2 plan graphs."""
from __future__ import annotations

import json
import uuid

from ..errors import PmtError
from ..util import canonical_json, sha256_text

SCHEMA_VERSION = 1
TREE_KINDS = {"requirement", "implementation"}
STAGES = {"prototype", "expansion", "production"}
STOP_REASONS = {"user_delegated", "implementation_boundary", "file_edit_boundary"}
RELATIONS = {"parent", "refines", "implements", "depends_on", "evidence"}


def _bad(code, message, details=None):
    raise PmtError(code, message, 2, False, details)


def _uuid(value, where):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        _bad("plan_graph_invalid", f"{where} must be a canonical UUID")


def _text(value, where, *, max_len=None, nonempty=True):
    if not isinstance(value, str) or (nonempty and not value.strip()) or (max_len is not None and len(value) > max_len):
        _bad("plan_graph_invalid", f"{where} must be text" + (f" of at most {max_len} characters" if max_len else ""))
    return value


def _refs(value, where):
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        _bad("plan_graph_invalid", f"{where} must be an array of nonempty references")


def validate_graph(graph, expected_project=None, *, complete=True):
    if not isinstance(graph, dict):
        _bad("plan_graph_invalid", "graph must be an object")
    required = {"schema_version", "project_id", "graph_version", "nodes", "relations", "provenance"}
    if required - set(graph):
        _bad("plan_graph_invalid", "graph is missing required fields", {"fields": sorted(required - set(graph))})
    if type(graph["schema_version"]) is not int or graph["schema_version"] != SCHEMA_VERSION:
        _bad("plan_graph_version_unsupported", "Only graph schema_version=1 is supported")
    _uuid(graph["project_id"], "project_id")
    if expected_project and graph["project_id"] != expected_project:
        _bad("plan_graph_project_mismatch", "graph project_id does not match the requested project")
    if type(graph["graph_version"]) is not int or graph["graph_version"] < 1:
        _bad("plan_graph_invalid", "graph_version must be a positive integer")
    if not isinstance(graph["nodes"], list) or not graph["nodes"]:
        _bad("plan_graph_invalid", "nodes must be a nonempty array")
    if not isinstance(graph["relations"], list):
        _bad("plan_graph_invalid", "relations must be an array")
    if not isinstance(graph["provenance"], (dict, list)) or not graph["provenance"]:
        _bad("plan_graph_invalid", "provenance must identify the source decisions or evidence")

    nodes = {}
    for i, node in enumerate(graph["nodes"]):
        where = f"nodes[{i}]"
        if not isinstance(node, dict):
            _bad("plan_graph_invalid", f"{where} must be an object")
        for field in ("id", "tree_kind", "node_kind", "summary", "product_stage"):
            if field not in node:
                _bad("plan_graph_invalid", f"{where}.{field} is required")
        _uuid(node["id"], f"{where}.id")
        if node["id"] in nodes:
            _bad("plan_graph_duplicate_id", "node IDs must be unique")
        if node["tree_kind"] not in TREE_KINDS:
            _bad("plan_graph_invalid", f"{where}.tree_kind is unsupported")
        _text(node["node_kind"], f"{where}.node_kind", max_len=64)
        _text(node["summary"], f"{where}.summary", max_len=50)
        if "\n" in node["summary"] or "\r" in node["summary"]:
            _bad("plan_graph_invalid", f"{where}.summary must be one line")
        if node["product_stage"] not in STAGES:
            _bad("plan_graph_invalid", f"{where}.product_stage is unsupported")
        product_scope = node.get("product_scope")
        if not isinstance(product_scope, dict) or type(product_scope.get("applies")) is not bool:
            _bad("plan_graph_invalid", f"{where}.product_scope must state whether this node applies")
        _text(product_scope.get("reason"), f"{where}.product_scope.reason", max_len=500)
        if product_scope["applies"]:
            _refs(product_scope.get("criteria"), f"{where}.product_scope.criteria")
        if "autonomy" not in node or not isinstance(node["autonomy"], dict):
            _bad("plan_graph_invalid", f"{where}.autonomy must record the decision authority and scope")
        authority = node["autonomy"].get("authority")
        _text(authority, f"{where}.autonomy.authority", max_len=80)
        _text(node["autonomy"].get("scope"), f"{where}.autonomy.scope", max_len=500)
        if node.get("premise") is None:
            _bad("plan_graph_invalid", f"{where}.premise is required")
        _text(node["premise"], f"{where}.premise", max_len=2000)
        if "evidence_refs" in node:
            _refs(node["evidence_refs"], f"{where}.evidence_refs")
        if "work_item_step_refs" in node:
            refs = node["work_item_step_refs"]
            if not isinstance(refs, dict):
                _bad("plan_graph_invalid", f"{where}.work_item_step_refs must be an object")
            for key in ("work", "item", "step"):
                _refs(refs.get(key, []), f"{where}.work_item_step_refs.{key}")
        if node["tree_kind"] == "requirement":
            _refs(node.get("source_refs"), f"{where}.source_refs")
            _refs(node.get("criteria"), f"{where}.criteria")
            if node["product_stage"] == "production" and not node.get("criteria"):
                _bad("plan_graph_invalid", f"{where} production criteria cannot be empty")
        else:
            for key in ("framework_assignment", "architecture", "logging"):
                _text(node.get(key), f"{where}.{key}", max_len=2000)
            _refs(node.get("tests"), f"{where}.tests")
            spec = node.get("function_spec")
            if not isinstance(spec, dict):
                _bad("plan_graph_invalid", f"{where}.function_spec is required")
            for key in ("input", "output", "constraints", "invariants", "errors", "verification"):
                _text(spec.get(key), f"{where}.function_spec.{key}", max_len=2000)
            choices = node.get("choice_set")
            if not isinstance(choices, dict):
                _bad("plan_graph_invalid", f"{where}.choice_set is required")
            options = choices.get("options")
            if not isinstance(options, list):
                _bad("plan_graph_invalid", f"{where}.choice_set.options must be an array")
            for j, option in enumerate(options):
                if not isinstance(option, dict):
                    _bad("plan_graph_invalid", f"{where}.choice_set.options[{j}] must be an object")
                _text(option.get("label"), f"{where}.choice_set.options[{j}].label", max_len=30)
                _text(option.get("rationale"), f"{where}.choice_set.options[{j}].rationale", max_len=1000)
                _refs(option.get("verified_refs"), f"{where}.choice_set.options[{j}].verified_refs")
            if len(options) < 3 and not _text(choices.get("insufficient_reason"), f"{where}.choice_set.insufficient_reason", max_len=500):
                _bad("plan_graph_choices_insufficient", f"{where} needs three verified alternatives or an insufficient_reason")
            choice = node.get("choice")
            if not isinstance(choice, dict) or choice.get("source") not in {"user", "ai"}:
                _bad("plan_graph_invalid", f"{where}.choice must identify user or AI selection")
            for key in ("selected", "reason", "scope"):
                _text(choice.get(key), f"{where}.choice.{key}", max_len=1000)

        nodes[node["id"]] = node

    edge_ids, parent_of = set(), {}
    adjacency = {"decomposition": {node_id: set() for node_id in nodes},
                 "dependency": {node_id: set() for node_id in nodes}}
    for i, relation in enumerate(graph["relations"]):
        where = f"relations[{i}]"
        if not isinstance(relation, dict):
            _bad("plan_graph_invalid", f"{where} must be an object")
        for key in ("id", "kind", "from", "to"):
            if key not in relation:
                _bad("plan_graph_invalid", f"{where}.{key} is required")
        _uuid(relation["id"], f"{where}.id")
        if relation["id"] in edge_ids:
            _bad("plan_graph_duplicate_id", "relation IDs must be unique")
        edge_ids.add(relation["id"])
        if relation["kind"] not in RELATIONS:
            _bad("plan_graph_invalid", f"{where}.kind is unsupported")
        if relation["from"] not in nodes or relation["to"] not in nodes:
            _bad("plan_graph_invalid_reference", f"{where} references a missing node")
        if relation["from"] == relation["to"]:
            _bad("plan_graph_cycle", f"{where} cannot point to itself")
        if relation["kind"] == "parent":
            if relation["to"] in parent_of:
                _bad("plan_graph_invalid_parent", "a node may have only one parent")
            if nodes[relation["from"]]["tree_kind"] != nodes[relation["to"]]["tree_kind"]:
                _bad("plan_graph_invalid_parent", "parent relations must stay within one tree")
            parent_of[relation["to"]] = relation["from"]
            adjacency["decomposition"][relation["from"]].add(relation["to"])
        elif relation["kind"] == "refines":
            if nodes[relation["from"]]["tree_kind"] != nodes[relation["to"]]["tree_kind"]:
                _bad("plan_graph_invalid_reference", "refines relations must stay within one tree")
            adjacency["decomposition"][relation["from"]].add(relation["to"])
        elif relation["kind"] == "depends_on":
            adjacency["dependency"][relation["from"]].add(relation["to"])
        elif relation["kind"] == "implements":
            if (nodes[relation["from"]]["tree_kind"] != "requirement" or
                    nodes[relation["to"]]["tree_kind"] != "implementation"):
                _bad("plan_graph_invalid_reference", "implements relations must point from requirements to implementation")
        elif relation["kind"] == "evidence":
            if "evidence_ref" not in relation:
                _bad("plan_graph_invalid", f"{where}.evidence_ref is required")
            _text(relation["evidence_ref"], f"{where}.evidence_ref", max_len=1000)

    for graph_kind, edges in adjacency.items():
        visiting, visited = set(), set()
        def visit(node_id):
            if node_id in visiting:
                _bad("plan_graph_cycle", f"{graph_kind} relations must be acyclic")
            if node_id in visited:
                return
            visiting.add(node_id)
            for child in edges[node_id]:
                visit(child)
            visiting.remove(node_id)
            visited.add(node_id)
        for node_id in nodes:
            visit(node_id)

    child_ids = set(parent_of.values())
    for node_id, node in nodes.items():
        if node_id not in child_ids:
            reason = node.get("stop_reason")
            if complete and reason not in STOP_REASONS:
                _bad("plan_graph_termination_missing", f"leaf node {node_id} needs a valid stop_reason")
            if reason is not None and reason not in STOP_REASONS:
                _bad("plan_graph_termination_invalid", "Unknown termination reason")
            if reason == "user_delegated":
                _text(node.get("delegated_scope"), f"node {node_id}.delegated_scope", max_len=500)
            if reason == "implementation_boundary" and node["tree_kind"] != "requirement":
                _bad("plan_graph_termination_invalid", "implementation_boundary applies to requirement tree leaves")
            if reason == "file_edit_boundary" and node["tree_kind"] != "implementation":
                _bad("plan_graph_termination_invalid", "file_edit_boundary applies to implementation tree leaves")
        elif node.get("stop_reason") is not None:
            _bad("plan_graph_termination_invalid", "split nodes cannot have a stop_reason")
    if complete and {node["tree_kind"] for node in nodes.values()} != TREE_KINDS:
        _bad("plan_graph_incomplete", "Both requirement and implementation trees are required for publication")
    return {"valid": True, "complete": complete, "schema_version": SCHEMA_VERSION, "project_id": graph["project_id"],
            "graph_version": graph["graph_version"], "node_count": len(nodes),
            "relation_count": len(graph["relations"]), "sha256": sha256_text(canonical_json(graph))}


def render_docs(graph):
    """Produce stable UTF-8 JSON and a concise human-readable project plan."""
    report = validate_graph(graph)
    lines = ["# Project plan", "", f"Graph version: {graph['graph_version']}", "",
             "This document and the adjacent graph JSON are the project plan source of truth.", ""]
    for tree_kind in ("requirement", "implementation"):
        lines.extend(["## " + ("Requirements" if tree_kind == "requirement" else "Implementation"), ""])
        nodes = [n for n in graph["nodes"] if n["tree_kind"] == tree_kind]
        for node in nodes:
            lines.extend([f"### {node['summary']}", "", f"ID: `{node['id']}`", "",
                          f"Premise: {node['premise']}", "", f"Product stage: {node['product_stage']}", ""])
            scope = node["product_scope"]
            lines.extend([f"Applicable: {'yes' if scope['applies'] else 'no'} — {scope['reason']}", ""])
            if scope["applies"]:
                lines.extend(["Stage criteria:", *[f"- {item}" for item in scope["criteria"]], ""])
            if node.get("stop_reason"):
                lines.append(f"Termination: {node['stop_reason']}")
                if node.get("delegated_scope"):
                    lines.append(f"Delegated scope: {node['delegated_scope']}")
                lines.append("")
            if tree_kind == "requirement":
                lines.extend(["Criteria:", *[f"- {criterion}" for criterion in node["criteria"]], ""])
            else:
                spec = node["function_spec"]
                lines.extend([f"Framework assignment: {node['framework_assignment']}", "",
                              f"Architecture: {node['architecture']}", "",
                              f"Logging: {node['logging']}", "", "Tests:",
                              *[f"- {test}" for test in node["tests"]], "", "Function specification:", *[f"- {label}: {spec[key]}" for label, key in
                              (("Input", "input"), ("Output", "output"), ("Constraints", "constraints"),
                               ("Invariants", "invariants"), ("Errors", "errors"), ("Verification", "verification"))], ""])
    lines.extend(["## Relations", ""])
    for relation in graph["relations"]:
        lines.append(f"- {relation['from']} —{relation['kind']}→ {relation['to']}")
    lines.append("")
    return "\n".join(lines), canonical_json(graph) + "\n", report
