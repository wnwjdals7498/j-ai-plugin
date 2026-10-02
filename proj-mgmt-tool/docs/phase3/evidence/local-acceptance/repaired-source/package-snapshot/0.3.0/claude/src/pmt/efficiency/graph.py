"""Git-backed graph changes, source-pinned projections and conservative impact queries."""
from __future__ import annotations

import hashlib
import base64
from contextlib import closing
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import uuid

from ..errors import PmtError
from ..phase2_common import event, require_workspace_claim, validate_scope
from ..planning.graph import RELATIONS, SCHEMA_VERSION, validate_graph
from ..util import canonical_json, fingerprint, new_id, strict_json_loads, utc_now
from .source import SourcePin, inspect_graph_source, pin_source, verify_source_pin
from .storage import Phase3Storage

READ_OPERATIONS = {"capture_source_pin", "preview_graph_change", "query_graph", "calculate_graph_impact"}
WRITE_OPERATIONS = set()
FILE_OPERATIONS = {"apply_graph_change", "recover_graph_change", "rebuild_graph_index", "register_segment_manifest"}

_NODE_FIELDS = {
    "tree_kind", "node_kind", "summary", "premise", "source_refs", "criteria", "product_stage",
    "product_scope", "autonomy", "evidence_refs", "work_item_step_refs", "stop_reason",
    "delegated_scope", "framework_assignment", "architecture", "logging", "tests",
    "function_spec", "choice_set", "choice", "retired", "retirement_reason",
}
_INDEX_ACTOR = "pmt.graph.index"
_INDEX_SESSION = "source-projection"
_INDEX_KIND = "graph_index"
_INDEX_LIMIT = 5000
_FIELD_RULE_VERSION = "graph-field-semantics-1"
_FIELD_SEMANTICS = {
    "summary": "unknown", "product_stage": "contract",
    "product_scope": "premise", "premise": "premise", "source_refs": "evidence",
    "evidence_refs": "evidence", "criteria": "verification", "tests": "verification",
    "work_item_step_refs": "work_link", "framework_assignment": "method",
    "architecture": "method", "logging": "method", "function_spec": "method",
    "choice": "method", "choice_set": "method", "autonomy": "boundary",
    "node_kind": "structure", "tree_kind": "structure", "relations": "relation",
}
_NESTED_FIELD_SEMANTICS = {
    "product_scope.applies": "contract", "product_scope.reason": "premise",
    "product_scope.criteria": "verification", "autonomy.authority": "boundary",
    "autonomy.scope": "boundary", "function_spec.input": "contract",
    "function_spec.output": "contract", "function_spec.constraints": "method",
    "function_spec.invariants": "method", "function_spec.errors": "contract",
    "function_spec.verification": "verification", "choice.source": "method",
    "choice.selected": "method", "choice.reason": "method", "choice.scope": "boundary",
}


def _fail(code, message, exit_code=2, details=None, retryable=False):
    raise PmtError(code, message, exit_code, retryable, details)


def _payload(req):
    value = req.get("payload", {})
    if not isinstance(value, dict):
        _fail("invalid_payload", "payload must be an object")
    return value


def _uuid(value, field):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (TypeError, ValueError, AttributeError):
        _fail("invalid_identifier", f"{field} must be a canonical UUID")
    return value


def _scope_context(db, conn, req):
    trusted_source = getattr(db, "source_repository", None)
    if trusted_source is not None:
        return trusted_source.scope_context(conn, req)
    payload = _payload(req)
    scope_id = req.get("scope_id") or payload.get("scope_id")
    repository_id = _uuid(payload.get("repository_id"), "repository_id")
    scope = validate_scope(db, conn, scope_id)
    if scope["kind"] != "project":
        _fail("project_scope_required", "Graph operations require a project scope", 3)
    repository = conn.execute("SELECT id,kind,body_json FROM scopes WHERE id=?", (repository_id,)).fetchone()
    if repository is None or repository["kind"] != "repository":
        _fail("repository_scope_mismatch", "repository_id must identify the project's repository scope", 3)
    project_body = json.loads(scope.get("body_json") or "{}")
    repository_binding = project_body.get("repository_id", project_body.get("repository_scope_id"))
    if scope["parent_id"] not in {None, repository_id} or (repository_binding and repository_binding != repository_id):
        _fail("repository_scope_mismatch", "Project and repository scope IDs do not match", 3)
    workspace = payload.get("workspace")
    if not isinstance(workspace, str) or not workspace.strip() or not Path(workspace).is_absolute():
        _fail("invalid_workspace", "An absolute workspace is required")
    workspace_path = Path(os.path.abspath(workspace))
    relative = payload.get("relative_graph_path")
    if not isinstance(relative, str) or not relative.strip() or "\\" in relative:
        _fail("invalid_graph_path", "relative_graph_path must be a workspace-relative POSIX path")
    pure = PurePosixPath(relative)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts) or relative.startswith("-"):
        _fail("invalid_graph_path", "relative_graph_path must remain inside the owned workspace")
    normalized = pure.as_posix()
    try:
        common = Path(os.path.abspath(workspace_path / Path(*pure.parts)))
        common.relative_to(workspace_path)
    except (OSError, ValueError):
        _fail("invalid_graph_path", "Graph path resolves outside the workspace")
    return {"scope_id": scope_id, "repository_id": repository_id, "workspace": workspace_path,
            "relative_path": normalized, "graph_path": common, "project_body": project_body,
            "repository_body": json.loads(repository["body_json"] or "{}"),
            "project_parent_id": scope["parent_id"]}


def _git(workspace: Path, *args, timeout=15, check=True):
    try:
        completed = subprocess.run(["git", "-C", str(workspace), *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PmtError("git_source_unavailable", "Local Git source could not be inspected", 4, True) from exc
    if check and completed.returncode != 0:
        _fail("git_source_unavailable", "Local Git source does not support the requested inspection", 3)
    return completed


def _claimed_context(db, conn, req, *, allow_missing=False):
    context = _scope_context(db, conn, req)
    run = require_workspace_claim(db, conn, req, str(context["workspace"]), [context["relative_path"]])
    context["run"] = run
    try:
        context["workspace"] = context["workspace"].resolve(strict=True)
    except OSError as exc:
        raise PmtError("invalid_workspace", "Workspace does not exist", 3) from exc
    if not context["workspace"].is_dir():
        _fail("invalid_workspace", "Workspace must be a directory")
    context["graph_path"] = Path(os.path.abspath(context["workspace"] / Path(context["relative_path"])))
    if Path(run["workspace"]).resolve(strict=True) != context["workspace"]:
        _fail("ownership_conflict", "Run workspace mapping changed", 3)
    from ..resources import _reject_links
    _reject_links(context["workspace"])
    _reject_links(context["graph_path"])
    if allow_missing and not context["graph_path"].exists():
        return context
    try:
        context["graph_path"].resolve(strict=True).relative_to(context["workspace"])
    except (OSError, ValueError):
        _fail("invalid_graph_path", "Graph file is missing or resolves outside the workspace", 3)
    if not context["graph_path"].is_file():
        _fail("invalid_graph_path", "Graph source must be a regular file", 3)
    return context


def _source_graph(db, conn, req):
    """Require the request's active workspace claim before reading any project file."""
    trusted_source = getattr(db, "source_repository", None)
    if trusted_source is not None:
        # The adapter is injected by the authenticated Host application, never
        # selected through request JSON. It verifies the current P2 run/scope
        # claim and reads an immutable client-captured Host snapshot only.
        return trusted_source.capture(conn, req)
    context = _claimed_context(db, conn, req)
    workspace = context["workspace"]
    expected_remote = context["repository_body"].get("remote")
    project_repo = context["project_body"].get("repository_id", context["project_body"].get("repository_scope_id"))
    mapped_workspace = context["project_body"].get("workspace")
    project_workspace_matches = False
    if mapped_workspace:
        try:
            mapped_path = Path(mapped_workspace).expanduser()
            if not mapped_path.is_absolute():
                mapped_path = context["workspace"] / mapped_path
            project_workspace_matches = mapped_path.resolve(strict=True) == context["workspace"]
        except OSError:
            project_workspace_matches = False
    explicit_mapping = project_repo == context["repository_id"] and project_workspace_matches

    def validate_mapping(source):
        remote_mapping = False
        if source["is_git"]:
            if expected_remote and source["git_origin"] is not None:
                from ..lifecycle import _canonical_remote
                try:
                    remote_mapping = _canonical_remote(source["git_origin"]) == _canonical_remote(expected_remote)
                except PmtError:
                    remote_mapping = False
            if expected_remote and not remote_mapping:
                _fail("repository_remote_mismatch", "Git origin does not match the repository scope", 3)
            if mapped_workspace and not project_workspace_matches:
                _fail("repository_scope_mismatch", "Project workspace mapping does not match the claimed workspace", 3)
            if project_repo and project_repo != context["repository_id"]:
                _fail("repository_scope_mismatch", "Project repository mapping does not match repository_id", 3)
            if context["project_parent_id"] is None and not explicit_mapping and not remote_mapping:
                _fail("repository_mapping_required", "Standalone project needs an explicit workspace/repository mapping or matching origin", 3)
        else:
            if expected_remote:
                _fail("git_source_unavailable", "A repository with a configured remote requires an accessible Git checkout", 3)
            if mapped_workspace and not project_workspace_matches:
                _fail("repository_scope_mismatch", "Project workspace mapping does not match the claimed workspace", 3)
            if project_repo and project_repo != context["repository_id"]:
                _fail("repository_scope_mismatch", "Project repository mapping does not match repository_id", 3)
            if context["project_parent_id"] is None and not explicit_mapping:
                _fail("repository_mapping_required", "Standalone non-Git project needs explicit repository_id and workspace mapping", 3)

    inspected = inspect_graph_source(
        workspace, context["graph_path"], context["repository_id"], context["scope_id"],
        graph_scope_id=context["scope_id"], git_runner=_git, validate_graph_fn=validate_graph,
        pre_read_validator=validate_mapping)
    context.update(inspected)
    return context


def _verify_source_identity(context, expected):
    workspace = context["workspace"]
    root = _git(workspace, "rev-parse", "--show-toplevel", check=False)
    is_git = root.returncode == 0
    if expected.source_kind == "git":
        if not is_git:
            _fail("source_conflict", "Expected Git repository is no longer available", 3)
        head = os.fsdecode(_git(workspace, "rev-parse", "--verify", "HEAD").stdout).strip()
        branch = _git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        ref = os.fsdecode(branch.stdout).strip() if branch.returncode == 0 else None
        if head != expected.reviewed_commit or ref != expected.selected_ref:
            _fail("source_conflict", "Git HEAD or selected ref changed during recovery", 3)
        if context["repository_body"].get("remote"):
            origin = _git(workspace, "remote", "get-url", "origin", check=False)
            if origin.returncode != 0:
                _fail("repository_remote_mismatch", "Configured Git origin is unavailable", 3)
            from ..lifecycle import _canonical_remote
            if _canonical_remote(os.fsdecode(origin.stdout).strip()) != _canonical_remote(context["repository_body"]["remote"]):
                _fail("repository_remote_mismatch", "Git origin changed during recovery", 3)
    elif expected.source_kind == "non_git":
        if is_git or context["repository_body"].get("remote"):
            _fail("source_conflict", "Source is no longer the explicitly mapped non-Git workspace", 3)
    else:
        _fail("source_kind_unknown", "Unknown source kind cannot authorize graph recovery", 3)


def _expected_source(value):
    if value is None:
        _fail("expected_source_required", "expected_source is required")
    return pin_source(value)


def _safe_operation_summary(prepared):
    return [{"op": item["op"], "id": item.get("id"), "kind": item.get("kind"),
             "fields": item.get("fields", []), "clear": item.get("clear", []),
             "field_fingerprints": item.get("field_fingerprints", {}),
             "target_ids": item.get("target_ids", [])} for item in prepared["operations"]]


def _field_fingerprint(owner, field):
    if field in owner:
        return fingerprint({"present": True, "value": owner[field]})
    return fingerprint({"present": False})


def prepare_change_set(graph, change_set, request_id):
    """Validate typed deltas and return a deterministic candidate without mutating the input."""
    _uuid(request_id, "request_id")
    if not isinstance(change_set, dict) or set(change_set) - {"change_id", "changes", "reason", "evidence_refs"}:
        _fail("change_set_invalid", "change_set has unsupported fields")
    if not isinstance(change_set.get("changes"), list) or not change_set["changes"] or len(change_set["changes"]) > 200:
        _fail("change_set_invalid", "change_set.changes must be a nonempty array")
    change_id = _uuid(change_set.get("change_id"), "change_set.change_id")
    reason = change_set.get("reason")
    evidence_refs = change_set.get("evidence_refs", [])
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
        _fail("change_set_invalid", "change_set.reason must be nonempty text up to 500 characters")
    if not isinstance(evidence_refs, list) or len(evidence_refs) > 100 or any(
            not isinstance(ref, str) or not ref.strip() or len(ref) > 1000 for ref in evidence_refs):
        _fail("change_set_invalid", "change_set.evidence_refs must be a bounded list of nonempty references")
    candidate = json.loads(canonical_json(graph))
    original_nodes = {node["id"]: node for node in candidate["nodes"]}
    nodes = {node["id"]: node for node in candidate["nodes"]}
    relations = candidate["relations"]
    temp_ids = {}
    for index, change in enumerate(change_set["changes"]):
        if not isinstance(change, dict) or change.get("op") != "create":
            continue
        temp = change.get("temp_id")
        explicit = change.get("id")
        if temp is not None:
            if not isinstance(temp, str) or not re.fullmatch(r"temp:[A-Za-z0-9_-]{1,64}", temp) or temp in temp_ids:
                _fail("change_set_invalid", "create temp_id must use temp:<1-64 letters, digits, _ or ->")
            if explicit is not None:
                _fail("change_set_invalid", "create accepts either id or temp_id")
            temp_ids[temp] = str(uuid.uuid5(uuid.UUID(change_id), "node:" + temp))
        elif explicit is None:
            temp_ids[f"@create:{index}"] = str(uuid.uuid5(uuid.UUID(change_id), f"node-index:{index}"))

    def resolve(node_ref):
        if not isinstance(node_ref, str):
            _fail("plan_graph_invalid_reference", "Node references must be UUIDs or temp_id text", 2)
        if node_ref in nodes:
            return node_ref
        if node_ref in temp_ids:
            return temp_ids[node_ref]
        _fail("plan_graph_invalid_reference", "Change references an unknown node or temp_id", 2)

    summaries, inherited, created, retired = [], [], [], []
    allowed_top = {"op", "id", "temp_id", "node", "inherit_from", "fields", "clear",
                   "relation", "relation_id", "reason"}
    for index, change in enumerate(change_set["changes"]):
        if not isinstance(change, dict):
            _fail("change_set_invalid", f"changes[{index}] must be an object")
        extra = set(change) - allowed_top
        if extra:
            _fail("change_set_invalid", "Change operation contains unsupported fields", details={"fields": sorted(extra)})
        operation = change.get("op")
        if operation == "create":
            node_input = change.get("node")
            if not isinstance(node_input, dict) or "id" in node_input:
                _fail("change_set_invalid", "create.node must be a node object without id")
            key = change.get("temp_id", f"@create:{index}")
            object_id = change.get("id") or temp_ids.get(key)
            if not object_id:
                _fail("change_set_invalid", "create requires id or temp_id")
            if change.get("id"):
                _uuid(change["id"], "create.id")
            if object_id in nodes:
                _fail("plan_graph_duplicate_id", "create node ID already exists")
            inherit_from = change.get("inherit_from")
            if inherit_from is not None:
                inherit_id = resolve(inherit_from)
                source = nodes.get(inherit_id)
                if source is None or source.get("retired") is True:
                    _fail("node_not_available", "inherit_from must identify an active source node", 3)
                base = {key: value for key, value in source.items() if key != "id"}
                if node_input.get("tree_kind", source.get("tree_kind")) != source.get("tree_kind"):
                    _fail("change_set_invalid", "An inherited node must preserve tree_kind")
                inherited.append({"id": object_id, "from": inherit_id, "fields": sorted(base)})
            else:
                base = {}
            node = base | json.loads(canonical_json(node_input))
            node["id"] = object_id
            if "tree_kind" not in node:
                _fail("change_set_invalid", "Created node must identify tree_kind or inherit it")
            if set(node) - (_NODE_FIELDS | {"id"}):
                _fail("change_set_invalid", "Created node contains unsupported fields",
                      details={"fields": sorted(set(node) - (_NODE_FIELDS | {"id"}))})
            if node.get("retired") or node.get("retirement_reason"):
                _fail("change_set_invalid", "New nodes cannot start in retired state")
            nodes[object_id] = node
            candidate["nodes"].append(node)
            created.append(object_id)
            field_hashes = {}
            for name in sorted(node_input):
                field_hashes[name] = {"before": _field_fingerprint(base, name),
                                      "after": _field_fingerprint(node, name)}
            summaries.append({"op": operation, "id": object_id, "kind": node.get("node_kind"),
                              "fields": sorted(node_input), "field_fingerprints": field_hashes})
        elif operation == "update":
            node_ref = change.get("id")
            object_id = resolve(node_ref)
            node = nodes[object_id]
            if node.get("retired") is True:
                _fail("node_retired", "Retired nodes cannot be updated", 3)
            fields = change.get("fields", {})
            clear = change.get("clear", [])
            if not isinstance(fields, dict) or not isinstance(clear, list) or any(not isinstance(x, str) for x in clear):
                _fail("change_set_invalid", "update.fields must be an object and clear an array of field names")
            if not fields and not clear:
                _fail("change_set_invalid", "update must change or clear at least one field")
            names = set(fields) | set(clear)
            if set(fields) & set(clear) or names - _NODE_FIELDS or names & {"id", "tree_kind", "retired", "retirement_reason"}:
                _fail("change_set_invalid", "update fields are unknown, immutable, or both set and cleared",
                      details={"fields": sorted(names)})
            field_hashes = {name: {"before": _field_fingerprint(node, name),
                                   "after": fingerprint({"present": True, "value": fields[name]})}
                            for name in sorted(fields)}
            field_hashes.update({name: {"before": _field_fingerprint(node, name),
                                        "after": fingerprint({"present": False, "explicit_clear": True})}
                                 for name in sorted(clear)})
            node.update(json.loads(canonical_json(fields)))
            for key in clear:
                node.pop(key, None)
            summaries.append({"op": operation, "id": object_id, "kind": node.get("node_kind"),
                              "fields": sorted(fields), "clear": sorted(clear),
                              "field_fingerprints": field_hashes})
        elif operation == "relate":
            relation_input = change.get("relation")
            if not isinstance(relation_input, dict) or set(relation_input) - {"id", "kind", "from", "to", "evidence_ref"}:
                _fail("change_set_invalid", "relate.relation has an unsupported shape")
            relation = json.loads(canonical_json(relation_input))
            relation["from"], relation["to"] = resolve(relation.get("from")), resolve(relation.get("to"))
            if relation.get("id"):
                _uuid(relation["id"], "relation.id")
            else:
                relation["id"] = str(uuid.uuid5(uuid.UUID(change_id), f"relation-index:{index}"))
            if any(existing["id"] == relation["id"] for existing in relations):
                _fail("plan_graph_duplicate_id", "Relation ID already exists")
            relations.append(relation)
            summaries.append({"op": operation, "id": relation["id"], "kind": relation.get("kind"),
                              "target_ids": [relation["from"], relation["to"]]})
        elif operation == "unrelate":
            relation_id = _uuid(change.get("relation_id"), "relation_id")
            found = next((item for item in relations if item["id"] == relation_id), None)
            if found is None:
                _fail("relation_not_found", "Relation does not exist", 3)
            relations.remove(found)
            summaries.append({"op": operation, "id": relation_id, "kind": found["kind"],
                              "target_ids": [found["from"], found["to"]]})
        elif operation == "deprecate":
            root_id = resolve(change.get("id"))
            reason = change.get("reason")
            if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                _fail("change_set_invalid", "deprecate.reason must be nonempty text up to 500 characters")
            branch = {root_id}
            changed = True
            while changed:
                changed = False
                for relation in relations:
                    if relation["kind"] in {"parent", "refines"} and relation["from"] in branch and relation["to"] not in branch:
                        branch.add(relation["to"])
                        changed = True
            for node_id in sorted(branch):
                node = nodes[node_id]
                node["retired"] = True
                node["retirement_reason"] = reason
            retired.extend(sorted(branch))
            summaries.append({"op": operation, "id": root_id, "target_ids": sorted(branch)})
        else:
            _fail("change_set_invalid", f"Unsupported graph change operation: {operation!r}")

    candidate["nodes"] = list(nodes.values())
    candidate["graph_version"] = graph["graph_version"] + 1
    report = validate_graph(candidate, graph["project_id"], complete=False)
    candidate_wire = (canonical_json(candidate) + "\n").encode("utf-8")
    return {"graph": candidate, "wire": candidate_wire, "report": report,
            "operations": summaries, "temp_id_map": temp_ids,
            "created_ids": created, "retired_ids": retired, "inherited": inherited,
            "source_hash": report["sha256"],
            "change_set_hash": fingerprint(change_set), "change_id": change_id,
            "reason": reason, "evidence_refs": list(evidence_refs)}


def _pin_report(pin):
    return pin.to_dict()


def handle(db, conn, req):
    operation = req.get("operation")
    payload = _payload(req)
    context = _source_graph(db, conn, req)
    pin = context["source_pin"]
    if operation == "capture_source_pin":
        db.diagnostics.emit("planning.source_pin_captured", request_id=req.get("request_id"),
                            scope_id=context["scope_id"], source_hash=pin.source_hash,
                            graph_hash=pin.graph_hash, graph_revision=pin.graph_revision,
                            reason_code=pin.source_kind)
        return {"source_pin": _pin_report(pin), "graph": {"schema_version": pin.graph_schema,
                "revision": pin.graph_revision, "graph_hash": pin.graph_hash},
                "repository_id": context["repository_id"], "project_id": context["scope_id"],
                "relative_graph_path": context["relative_path"]}
    if operation == "preview_graph_change":
        expected = _expected_source(payload.get("expected_source"))
        verify_source_pin(expected, pin)
        change_set = payload.get("change_set")
        prepared = prepare_change_set(context["graph"], change_set, req["request_id"])
        target = _safe_operation_summary(prepared)
        db.diagnostics.emit("planning.graph_change_validated", request_id=req.get("request_id"),
                            event_id=_event_id(req), scope_id=context["scope_id"],
                            source_hash=pin.source_hash, graph_hash=pin.graph_hash,
                            graph_revision=pin.graph_revision, count=len(target))
        return {"change_id": prepared["change_id"],
                "source_pin": _pin_report(pin), "expected_new_source": {
                    "graph_schema": prepared["graph"]["schema_version"],
                    "graph_revision": prepared["graph"]["graph_version"],
                    "graph_hash": prepared["source_hash"]},
                "temp_id_map": prepared["temp_id_map"], "created_ids": prepared["created_ids"],
                "retired_ids": prepared["retired_ids"], "inherited": prepared["inherited"],
                "operations": target, "change_set_hash": prepared["change_set_hash"],
                "journal_status": "not_started"}
    if operation == "query_graph":
        return _query_graph(db, conn, req, context)
    if operation == "calculate_graph_impact":
        return _calculate_impact(db, conn, req, context)
    _fail("operation_unsupported", "Unsupported graph read operation")


def _request_replay(db, req):
    request_id = req["request_id"]
    normalized = db._normalize_request(req)
    semantic_hash = _request_fingerprint(normalized)
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT request_fingerprint,response_json,exit_code,actor,session_id FROM requests WHERE request_id=?",
                           (request_id,)).fetchone()
    if not row:
        return None
    if row[3] != req.get("actor") or row[4] != req.get("session_id"):
        _fail("request_owner_mismatch", "request result belongs to a different actor or session", 3)
    if row[0] != semantic_hash:
        _fail("request_conflict", "request_id was already used for a different request", 3)
    return json.loads(row[1]), row[2]


def _request_fingerprint(request):
    ignored = {"request_id", "correlation_id", "received_at", "received_at_utc", "retry_count", "attempt"}
    return fingerprint({key: value for key, value in request.items() if key not in ignored})


def _event_id(req):
    normalized = req.get("normalized_event") or {}
    value = normalized.get("event_id")
    if value:
        return _uuid(value, "event_id")
    return str(uuid.uuid5(uuid.UUID(req["request_id"]), "phase3.graph_change"))


def _derived_event_id(req, suffix):
    normalized = req.get("normalized_event") or {}
    value = normalized.get("event_id")
    if value:
        return _uuid(value, "event_id")
    return str(uuid.uuid5(uuid.UUID(req["request_id"]), "phase3." + suffix))


def _intent_row(db, conn, request_id, scope_id, actor, session):
    return conn.execute("SELECT * FROM phase3_journal WHERE kind='graph_change' AND request_id=? AND scope_id=? AND owner_actor=? AND owner_session=?",
                        (request_id, scope_id, actor, session)).fetchone()


def _journal_file(context, reference):
    if not isinstance(reference, str) or not reference or "\\" in reference:
        _fail("graph_change_journal_invalid", "Journal file reference is invalid", 3)
    relative = PurePosixPath(reference)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        _fail("graph_change_journal_invalid", "Journal file reference escapes the workspace", 3)
    path = Path(os.path.abspath(context["workspace"] / Path(*relative.parts)))
    try:
        path.relative_to(context["workspace"])
    except ValueError:
        _fail("graph_change_journal_invalid", "Journal file reference escapes the workspace", 3)
    from ..resources import _reject_links
    _reject_links(path)
    return path


def _load_prior_segment_manifests(db, conn, req, apply_receipt, *, limit=500):
    """Read prior-pinned manifest refs for F4 after F1 applied the exact change.

    The caller supplies a fresh autocommit connection; this helper never begins
    a transaction or returns graph source contents.
    """
    if type(limit) is not int or not 1 <= limit <= 500:
        _fail("segment_manifest_query_invalid", "limit must be between 1 and 500")
    receipt = apply_receipt.get("result") if isinstance(apply_receipt, dict) and isinstance(apply_receipt.get("result"), dict) else apply_receipt
    if not isinstance(receipt, dict):
        _fail("applied_change_receipt_invalid", "apply_receipt must be a graph-change result")
    before = pin_source(receipt.get("before_source_pin"))
    applied_pin = pin_source(receipt.get("source_pin"))
    change_id = _uuid(receipt.get("change_id"), "apply_receipt.change_id")
    change_set_hash = receipt.get("change_set_hash")
    if not isinstance(change_set_hash, str) or len(change_set_hash) != 64:
        _fail("applied_change_receipt_invalid", "apply receipt lacks change_set_hash")
    current = _source_graph(db, conn, req)
    verify_source_pin(applied_pin, current["source_pin"])
    if (before.project_id != current["scope_id"] or before.repository_id != current["repository_id"] or
            before.project_id != applied_pin.project_id or before.repository_id != applied_pin.repository_id):
        _fail("scope_conflict", "Applied change source pins do not belong to this project/repository", 3)
    intent_id = _uuid(receipt.get("journal_id"), "apply_receipt.journal_id")
    intent = Phase3Storage(db).get_intent(intent_id, current["scope_id"], req["actor"], req["session_id"], conn=conn)
    if intent is None or intent["body"].get("stage") != "completed":
        _fail("graph_change_journal_conflict", "Applied graph-change journal is not completed", 3)
    journal = intent["body"]
    if (journal.get("change_id") != change_id or journal.get("change_set_hash") != change_set_hash or
            journal.get("old_graph_hash") != before.graph_hash or
            journal.get("new_graph_hash") != applied_pin.graph_hash or
            journal.get("new_graph_revision") != applied_pin.graph_revision):
        _fail("applied_change_receipt_conflict", "Apply receipt does not match its completed journal", 3)
    request_id = journal.get("request_id")
    request_row = conn.execute("SELECT response_json,exit_code,actor,session_id FROM requests WHERE request_id=?",
                               (request_id,)).fetchone()
    if not request_row or request_row[1] != 0 or request_row[2] != req["actor"] or request_row[3] != req["session_id"]:
        _fail("ownership_conflict", "Completed apply request is not owned by this actor/session", 3)
    committed = json.loads(request_row[0]).get("result") or {}
    if (committed.get("change_id") != change_id or committed.get("change_set_hash") != change_set_hash or
            committed.get("source_pin", {}).get("source_hash") != applied_pin.source_hash):
        _fail("applied_change_receipt_conflict", "Apply receipt differs from the durable request result", 3)
    if receipt.get("recovery_ref") != committed.get("recovery_ref"):
        _fail("applied_change_receipt_conflict", "Apply receipt recovery reference differs from its durable result", 3)
    recovery_path = _journal_file(current, receipt.get("recovery_ref"))
    try:
        old_wire = recovery_path.read_bytes()
    except OSError as exc:
        raise PmtError("prior_source_unavailable", "Retained pre-change graph is unavailable", 3) from exc
    if hashlib.sha256(old_wire).hexdigest() != journal.get("old_file_sha256"):
        _fail("prior_source_unavailable", "Retained pre-change graph hash does not match its journal", 3)
    if len(old_wire) > 8 * 1024 * 1024:
        _fail("prior_source_unavailable", "Retained pre-change graph exceeds 8 MiB", 3)
    old_graph = strict_json_loads(old_wire, max_bytes=8 * 1024 * 1024)
    old_report = validate_graph(old_graph, before.project_id, complete=False)
    if (old_report["sha256"] != before.graph_hash or old_report["graph_version"] != before.graph_revision or
            old_report["schema_version"] != before.graph_schema):
        _fail("prior_source_unavailable", "Retained graph does not match before_source_pin", 3)
    rows = conn.execute("SELECT id,revision,source_hash,body_json FROM phase3_objects WHERE kind='segment_manifest' AND scope_id=? AND source_hash=? AND state='ready' ORDER BY id LIMIT ?",
                        (before.project_id, before.source_hash, limit)).fetchall()
    manifests = {}
    manifest_refs = []
    for row in rows:
        try:
            body = json.loads(row["body_json"])
        except (ValueError, TypeError):
            _fail("segment_manifest_corrupt", "Prior segment manifest metadata is invalid", 3,
                  {"segment_id": row["id"]})
        if not isinstance(body, dict):
            _fail("segment_manifest_corrupt", "Prior segment manifest metadata is invalid", 3,
                  {"segment_id": row["id"]})
        manifest_hash = _manifest_ref_hash(body)
        manifest_refs.append({"segment_id": row["id"], "manifest_hash": manifest_hash})
        manifests[row["id"]] = {"revision": row["revision"], "source_hash": row["source_hash"],
                                 "manifest": body}
    coverage_row = conn.execute("SELECT source_hash,body_json FROM phase3_objects WHERE kind='segment_coverage' AND id=? AND scope_id=?",
                                (before.project_id, before.project_id)).fetchone()
    coverage = {"segments": "unknown", "reason": "baseline_certificate_missing_or_stale"}
    if coverage_row and coverage_row["source_hash"] == before.source_hash:
        try:
            certificate = json.loads(coverage_row["body_json"])
            coverage = _coverage_from_certificate(certificate,
                _index_body(old_graph, before, manifests), manifests, before)
        except (ValueError, TypeError):
            coverage = {"segments": "unknown", "reason": "baseline_certificate_corrupt"}
    return {"before_source_pin": before.to_dict(), "applied_source_pin": applied_pin.to_dict(),
            "current_source_pin": current["source_pin"].to_dict(), "change_id": change_id,
            "change_set_hash": change_set_hash, "journal_id": intent_id,
            "recovery_ref": receipt["recovery_ref"], "prior_manifest_refs": manifest_refs,
            "prior_manifests": [{"segment_id": key, "revision": value["revision"],
                                  "manifest_hash": _manifest_ref_hash(value["manifest"]),
                                  "manifest": value["manifest"]} for key, value in sorted(manifests.items())],
            "prior_coverage": coverage, "prior_graph_hash": old_report["sha256"],
            "prior_graph_revision": old_report["graph_version"]}


def _active_target_intent(conn, target_key, except_request_id=None):
    rows = conn.execute("SELECT request_id,body_json FROM phase3_journal WHERE kind='graph_change' ORDER BY created_at").fetchall()
    for row in rows:
        if row[0] == except_request_id:
            continue
        try:
            body = json.loads(row[1])
        except (TypeError, ValueError):
            continue
        if body.get("target_key") == target_key and body.get("stage") not in {"completed", "conflict", "failed"}:
            return row[0]
    return None


def _target_key(context):
    return fingerprint({"workspace": str(context["workspace"]), "relative_path": context["relative_path"]})


def _journal_start(db, req, context, expected, prepared):
    storage = Phase3Storage(db)
    target_key = _target_key(context)
    stage_path = context["graph_path"].with_name(f".{context['graph_path'].name}.pmt-stage-{req['request_id']}")
    from .publication import publication_refs
    publication = publication_refs(context["graph_path"], req["request_id"], context["workspace"])
    body = {"stage": "prepared", "scope_id": context["scope_id"], "repository_id": context["repository_id"],
            "relative_path": context["relative_path"], "workspace_hash": fingerprint(str(context["workspace"])),
            "stage_path": stage_path.relative_to(context["workspace"]).as_posix(),
            "publication_refs": publication,
            "target_key": target_key, "run_id": context["run"]["id"], "request_id": req["request_id"],
            "source_pin": expected.to_dict(), "old_file_sha256": context["raw_sha256"],
            "old_graph_hash": context["report"]["sha256"], "new_file_sha256": hashlib.sha256(prepared["wire"]).hexdigest(),
            "new_graph_hash": prepared["source_hash"], "new_graph_revision": prepared["graph"]["graph_version"],
            "change_set_hash": prepared["change_set_hash"], "change_id": prepared["change_id"],
            "request_fingerprint": _request_fingerprint(db._normalize_request(req)),
            "temp_id_map": prepared["temp_id_map"], "created_ids": prepared["created_ids"],
            "retired_ids": prepared["retired_ids"], "inherited": prepared["inherited"],
            "operation_summary": _safe_operation_summary(prepared)}
    with db.write() as tx:
        active = _active_target_intent(tx, target_key, req["request_id"])
        if active:
            _fail("graph_change_in_progress", "Another graph change for this file needs completion or recovery", 3,
                  {"request_id": active})
        return storage.append_intent("graph_change", req["request_id"], context["scope_id"],
                                     req["actor"], req["session_id"], body,
                                     event_id=_event_id(req), conn=tx)


def _stage_graph(target: Path, content: bytes, request_id: str):
    from ..resources import _reject_links
    _reject_links(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    _reject_links(target.parent)
    stage = target.with_name(f".{target.name}.pmt-stage-{request_id}")
    expected_hash = hashlib.sha256(content).hexdigest()
    if stage.exists():
        if hashlib.sha256(stage.read_bytes()).hexdigest() != expected_hash:
            _fail("graph_stage_conflict", "A prior staged file has different contents", 3)
        return stage
    try:
        with stage.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        _reject_links(target)
        return stage
    except Exception:
        raise


def _publish_checkpoint(db, req, intent_id, scope_id, phase, details):
    phase_targets = {"original_preserved": "original_preserved", "target_detached": "target_detached",
                     "candidate_published": "file_written", "conflict": "conflict",
                     "durability_unknown": "recovery_required", "durability_unsupported": "unsupported"}
    target_stage = phase_targets.get(phase)
    if target_stage is None and phase != "intent":
        return
    storage = Phase3Storage(db)
    with closing(db.connect()) as conn:
        current = storage.get_intent(intent_id, scope_id, req["actor"], req["session_id"], conn=conn)
    if current is None:
        _fail("graph_change_journal_conflict", "Publication checkpoint lost its journal", 4)
    stage = current["body"].get("stage")
    if phase == "intent":
        storage.update_intent(intent_id, stage, stage, scope_id, req["actor"], req["session_id"],
                              {"publication_phase": phase, **(details if isinstance(details, dict) else {})})
        return
    order = {"prepared": 0, "original_preserved": 1, "target_detached": 2, "recovery_required": 3,
             "file_written": 4, "completed": 5, "conflict": 5, "failed": 5, "unsupported": 5}
    if order.get(stage, -1) >= order.get(target_stage, 4):
        return
    storage.update_intent(intent_id, stage, target_stage, scope_id, req["actor"], req["session_id"],
                          {"publication_phase": phase, **(details if isinstance(details, dict) else {})})


def _write_candidate(db, req, context, prepared, expected, request_id, *, intent_id=None):
    target = context["graph_path"]
    with closing(db.connect()) as conn:
        current = _source_graph(db, conn, req)
    verify_source_pin(expected, current["source_pin"])
    if current["raw_sha256"] != context["raw_sha256"]:
        _fail("source_conflict", "Graph file bytes changed before publication", 3)
    stage = _stage_graph(target, prepared["wire"], request_id)
    context["stage_path"] = stage
    _reject_source_path(context)
    with closing(db.connect()) as conn:
        after_stage = _source_graph(db, conn, req)
    verify_source_pin(expected, after_stage["source_pin"])
    if after_stage["raw_sha256"] != context["raw_sha256"]:
        _fail("source_conflict", "Graph source changed while the update was staged", 3)
    from .publication import guarded_publish
    publish = guarded_publish(target=target, stage=stage, expected_hash=context["raw_sha256"],
                              effect_id=request_id, owned_root=context["workspace"],
                              checkpoint=lambda phase, details: _publish_checkpoint(
                                  db, req, intent_id or _intent_row_id(db, req, context), context["scope_id"], phase, details))
    if publish.get("status") == "conflict":
        _fail("publication_conflict", "Graph publication preserved conflicting file versions", 3,
              {key: publish.get(key) for key in ("phase", "recovery_ref", "original_hash", "candidate_hash")})
    context["publish_receipt"] = publish
    context["recovery_ref"] = publish.get("recovery_ref")
    _publish_checkpoint(db, req, intent_id or _intent_row_id(db, req, context), context["scope_id"],
                        "candidate_published", publish)


def _confirm_publication(db, req, context, expected, request_id, intent_id, old_sha256,
                         new_sha256, new_graph_hash):
    stage = context.get("stage_path")
    if stage is None:
        stage = _stage_graph(context["graph_path"], context["wire"], request_id)
        context["stage_path"] = stage
    from .publication import guarded_publish
    receipt = guarded_publish(target=context["graph_path"], stage=stage, expected_hash=old_sha256,
                              effect_id=request_id, owned_root=context["workspace"],
                              checkpoint=lambda phase, details: _publish_checkpoint(
                                  db, req, intent_id, context["scope_id"], phase, details))
    if receipt.get("status") == "conflict":
        _fail("publication_conflict", "Graph target or retained original changed before database commit", 3,
              {key: receipt.get(key) for key in ("phase", "recovery_ref", "original_hash", "candidate_hash")})
    if receipt.get("candidate_hash") != new_sha256:
        _fail("publication_conflict", "Publication candidate hash differs from the journal", 3,
              {"recovery_ref": receipt.get("recovery_ref")})
    with closing(db.connect()) as conn:
        current = _source_graph(db, conn, req)
    if (current["raw_sha256"] != new_sha256 or current["source_pin"].graph_hash != new_graph_hash or
            current["source_pin"].reviewed_commit != expected.reviewed_commit or
            current["source_pin"].selected_ref != expected.selected_ref):
        _fail("publication_conflict", "Graph source changed during final publication checks", 3,
              {"recovery_ref": receipt.get("recovery_ref")})
    context.update({"recovery_ref": receipt.get("recovery_ref"), "publish_receipt": receipt})
    return current


def _intent_row_id(db, req, context):
    with closing(db.connect()) as conn:
        row = _intent_row(db, conn, req["request_id"], context["scope_id"], req["actor"], req["session_id"])
    if row is None:
        _fail("graph_change_journal_conflict", "Graph publication has no prepared journal", 4)
    return row["id"]


def _reject_source_path(context):
    from ..resources import _reject_links
    _reject_links(context["workspace"])
    _reject_links(context["graph_path"])
    try:
        context["graph_path"].resolve(strict=True).relative_to(context["workspace"])
    except (OSError, ValueError):
        _fail("invalid_graph_path", "Graph path changed or escaped the workspace", 3)


def _cleanup_candidate_stage(context, expected_hash):
    stage = context.get("stage_path")
    if stage is None:
        return
    from ..resources import _reject_links
    try:
        _reject_links(stage)
        if stage.is_file() and hashlib.sha256(stage.read_bytes()).hexdigest() == expected_hash:
            stage.unlink()
    except OSError:
        return


def _source_graph_snapshot_without_pin(context):
    _reject_source_path(context)
    try:
        wire = context["graph_path"].read_bytes()
    except OSError as exc:
        raise PmtError("graph_source_read_failed", "Graph source could not be read", 4, True) from exc
    if len(wire) > 8 * 1024 * 1024:
        _fail("graph_source_too_large", "Graph source exceeds 8 MiB")
    graph = strict_json_loads(wire, max_bytes=8 * 1024 * 1024)
    report = validate_graph(graph, context["scope_id"], complete=False)
    return {"graph": graph, "report": report, "wire": wire,
            "raw_sha256": hashlib.sha256(wire).hexdigest(), "graph_hash": report["sha256"]}


def _registered_manifests(conn, scope_id):
    rows = conn.execute("SELECT id,revision,source_hash,body_json FROM phase3_objects WHERE kind='segment_manifest' AND scope_id=? AND state='ready' ORDER BY id",
                        (scope_id,)).fetchall()
    manifests, corrupt = {}, False
    for row in rows:
        try:
            value = json.loads(row["body_json"])
            if not isinstance(value, dict):
                raise ValueError
        except (ValueError, TypeError):
            corrupt = True
            continue
        manifests[row["id"]] = {"revision": row["revision"], "source_hash": row["source_hash"],
                                "manifest": value}
    return manifests, corrupt


def _manifest_ref_hash(manifest):
    return fingerprint(manifest)


def manifest_set_fingerprint(manifest_refs, document_paths, source_pin):
    """Stable fingerprint for F4's complete baseline manifest handoff."""
    pin = pin_source(source_pin)
    if not isinstance(manifest_refs, list) or not isinstance(document_paths, list):
        _fail("coverage_certificate_invalid", "manifest_refs and document_paths must be arrays")
    refs = []
    for item in manifest_refs:
        if not isinstance(item, dict) or set(item) != {"segment_id", "manifest_hash"}:
            _fail("coverage_certificate_invalid", "manifest_refs entries need segment_id and manifest_hash")
        segment_id = _uuid(item["segment_id"], "manifest_refs.segment_id")
        digest = item["manifest_hash"]
        if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
            _fail("coverage_certificate_invalid", "manifest_hash must be lowercase SHA-256")
        refs.append({"segment_id": segment_id, "manifest_hash": digest})
    if len({item["segment_id"] for item in refs}) != len(refs):
        _fail("coverage_certificate_invalid", "manifest_refs contains duplicate segment IDs")
    paths = sorted({_manifest_relative_path(path) for path in document_paths})
    if not paths:
        _fail("coverage_certificate_invalid", "document_paths cannot be empty")
    return fingerprint({"source_hash": pin.source_hash, "manifest_refs": sorted(refs, key=lambda x: x["segment_id"]),
                       "document_paths": paths})


def coverage_certificate_fingerprint(certificate):
    if not isinstance(certificate, dict):
        _fail("coverage_certificate_invalid", "coverage certificate must be an object")
    semantic = {key: value for key, value in certificate.items()
                if key not in {"coverage_status", "coverage_source", "coverage_certificate_hash"}}
    for key in ("document_paths", "expected_node_ids", "expected_relation_ids", "template_versions"):
        if isinstance(semantic.get(key), list):
            semantic[key] = sorted(set(semantic[key]))
    if isinstance(semantic.get("manifest_refs"), list):
        semantic["manifest_refs"] = sorted(semantic["manifest_refs"], key=lambda item: item.get("segment_id", ""))
    if isinstance(semantic.get("expected_field_paths"), dict):
        semantic["expected_field_paths"] = {key: sorted(set(value)) if isinstance(value, list) else value
                                             for key, value in semantic["expected_field_paths"].items()}
    return fingerprint(semantic)


def _coverage_from_certificate(certificate, index_body, stored_manifests, pin):
    """Return complete coverage only when the certificate exactly covers this source/index."""
    if not isinstance(certificate, dict):
        return {"segments": "unknown", "reason": "baseline_certificate_missing"}
    try:
        cert_pin = pin_source(certificate.get("source_pin"))
        if cert_pin.source_hash != pin.source_hash:
            return {"segments": "unknown", "reason": "baseline_source_stale"}
        expected_nodes = certificate.get("expected_node_ids")
        expected_relations = certificate.get("expected_relation_ids")
        field_paths = certificate.get("expected_field_paths")
        if not isinstance(expected_nodes, list) or not isinstance(expected_relations, list) or not isinstance(field_paths, dict):
            return {"segments": "unknown", "reason": "baseline_certificate_invalid"}
        if set(expected_nodes) != set(index_body["nodes"]) or set(expected_relations) != set(index_body["relations"]):
            return {"segments": "unknown", "reason": "baseline_source_inventory_mismatch"}
        if set(field_paths) != set(expected_nodes):
            return {"segments": "unknown", "reason": "baseline_field_inventory_mismatch"}
        refs = certificate.get("manifest_refs")
        paths = certificate.get("document_paths")
        if not isinstance(refs, list) or not isinstance(paths, list):
            return {"segments": "unknown", "reason": "baseline_manifest_refs_invalid"}
        expected_refs = {entry["segment_id"]: entry["manifest_hash"] for entry in refs}
        current_manifests = {key: value for key, value in stored_manifests.items()
                             if value.get("source_hash") == pin.source_hash}
        if set(expected_refs) != set(current_manifests):
            return {"segments": "unknown", "reason": "baseline_manifest_set_changed"}
        if any(_manifest_ref_hash(current_manifests[key]["manifest"]) != digest
               for key, digest in expected_refs.items()):
            return {"segments": "unknown", "reason": "baseline_manifest_hash_mismatch"}
        actual_paths = {value["manifest"].get("document_path") for value in current_manifests.values()}
        if actual_paths != set(paths):
            return {"segments": "unknown", "reason": "baseline_document_set_mismatch"}
        if manifest_set_fingerprint(refs, paths, pin) != certificate.get("manifest_set_hash"):
            return {"segments": "unknown", "reason": "baseline_manifest_set_hash_mismatch"}
        actual_nodes, actual_relations = set(), set()
        actual_fields = {node_id: set() for node_id in expected_nodes}
        templates = set()
        for stored in current_manifests.values():
            value = stored["manifest"]
            actual_nodes.update(value.get("node_ids", []))
            actual_relations.update(value.get("relation_ids", []))
            templates.add(value.get("template_version"))
            for node_id in value.get("node_ids", []):
                actual_fields.setdefault(node_id, set()).update(value.get("field_paths", []))
        if actual_nodes != set(expected_nodes) or actual_relations != set(expected_relations):
            return {"segments": "unknown", "reason": "baseline_dependency_inventory_mismatch"}
        for node_id in expected_nodes:
            required_fields = field_paths.get(node_id)
            if not isinstance(required_fields, list) or not required_fields or not set(required_fields).issubset(actual_fields.get(node_id, set())):
                return {"segments": "unknown", "reason": "baseline_field_coverage_incomplete"}
            for field_path in required_fields:
                root_field = field_path.split(".", 1)[0]
                if root_field not in (_NODE_FIELDS | {"id"}):
                    return {"segments": "unknown", "reason": "baseline_field_registry_unknown"}
        expected_templates = certificate.get("template_versions")
        if (not isinstance(expected_templates, list) or not expected_templates or
                set(expected_templates) != templates):
            return {"segments": "unknown", "reason": "baseline_template_registry_mismatch"}
        if not isinstance(certificate.get("dependency_registry_version"), str) or not certificate["dependency_registry_version"].strip():
            return {"segments": "unknown", "reason": "baseline_dependency_registry_missing"}
        if not isinstance(certificate.get("production_receipt_ref"), str) or not certificate["production_receipt_ref"].strip():
            return {"segments": "unknown", "reason": "baseline_production_receipt_missing"}
        if not isinstance(certificate.get("source_table_version"), str) or not certificate["source_table_version"].strip():
            return {"segments": "unknown", "reason": "baseline_source_table_version_missing"}
        certificate_hash = coverage_certificate_fingerprint(certificate)
        stored_hash = certificate.get("coverage_certificate_hash")
        if stored_hash is not None and stored_hash != certificate_hash:
            return {"segments": "unknown", "reason": "baseline_certificate_hash_mismatch"}
        return {"segments": "complete", "reason": None,
                "source_hash": pin.source_hash, "manifest_set_hash": certificate["manifest_set_hash"],
                "certificate_hash": certificate_hash,
                "document_paths": sorted(paths), "segment_count": len(current_manifests),
                "dependency_registry_version": certificate["dependency_registry_version"],
                "template_versions": sorted(templates),
                "production_receipt_ref": certificate["production_receipt_ref"],
                "source_table_version": certificate["source_table_version"]}
    except (PmtError, KeyError, TypeError, ValueError):
        return {"segments": "unknown", "reason": "baseline_certificate_invalid"}


def _index_body(graph, source_pin, manifests=None, manifests_corrupt=False, coverage=None):
    nodes = {node["id"]: node for node in graph["nodes"]}
    relations = {relation["id"]: relation for relation in graph["relations"]}
    adjacency = {}
    reverse = {}
    for rel in graph["relations"]:
        adjacency.setdefault(rel["from"], []).append(rel["id"])
        reverse.setdefault(rel["to"], []).append(rel["id"])
    body = {"index_version": 1, "project_id": graph["project_id"],
            "graph_schema": graph["schema_version"], "graph_revision": graph["graph_version"],
            "graph_hash": source_pin.graph_hash, "source_pin": source_pin.to_dict(),
            "nodes": nodes, "relations": relations,
            "adjacency": {key: sorted(value) for key, value in adjacency.items()},
            "reverse_adjacency": {key: sorted(value) for key, value in reverse.items()},
            "manifests": manifests or {},
            "coverage": (coverage if coverage is not None else
                         {"segments": "unknown",
                          "reason": "manifest_corrupt" if manifests_corrupt else "baseline_manifest_not_registered"})}
    body["index_hash"] = fingerprint(body)
    return body


def _stored_coverage(conn, scope_id, source_pin, graph_index, manifests):
    row = conn.execute("SELECT source_hash,body_json FROM phase3_objects WHERE kind='segment_coverage' AND id=? AND scope_id=?",
                       (scope_id, scope_id)).fetchone()
    if row is None or row["source_hash"] != source_pin.source_hash:
        return {"segments": "unknown", "reason": "baseline_certificate_missing_or_stale"}
    try:
        certificate = json.loads(row["body_json"])
    except (TypeError, ValueError):
        return {"segments": "unknown", "reason": "baseline_certificate_corrupt"}
    return _coverage_from_certificate(certificate, graph_index, manifests, source_pin)


def query_graph_slice(index_body, query, source_pin):
    """Pure bounded projection over a source-pinned F2 index body."""
    pin = pin_source(source_pin)
    if not isinstance(index_body, dict) or not isinstance(query, dict):
        _fail("graph_query_invalid", "Index and query must be objects")
    supplied_hash = index_body.get("index_hash")
    unhashed = {key: value for key, value in index_body.items() if key != "index_hash"}
    if not isinstance(supplied_hash, str) or supplied_hash != fingerprint(unhashed):
        _fail("graph_index_corrupt", "Graph index integrity hash does not match", 3)
    if index_body.get("source_pin", {}).get("source_hash") != pin.source_hash:
        _fail("graph_index_stale", "Graph index does not match the requested SourcePin", 3,
              {"rebuild_required": True})
    nodes, relations = index_body.get("nodes"), index_body.get("relations")
    if not isinstance(nodes, dict) or not isinstance(relations, dict) or len(nodes) > _INDEX_LIMIT or len(relations) > _INDEX_LIMIT * 4:
        _fail("graph_index_corrupt", "Graph index has an invalid or oversized projection", 3,
              {"rebuild_required": True})
    allowed = {"node_ids", "relation_ids", "relation_kinds", "direction", "max_depth", "page_size", "fields", "cursor"}
    if set(query) - allowed:
        _fail("graph_query_invalid", "Query has unsupported fields", details={"fields": sorted(set(query) - allowed)})
    direction = query.get("direction", "both")
    if direction not in {"incoming", "outgoing", "both"}:
        _fail("graph_query_invalid", "direction must be incoming, outgoing, or both")
    depth = query.get("max_depth", 2)
    page_size = query.get("page_size", 100)
    if type(depth) is not int or not 1 <= depth <= 8 or type(page_size) is not int or not 1 <= page_size <= 250:
        _fail("graph_query_invalid", "max_depth must be 1..8 and page_size 1..250")
    node_ids, relation_ids, kinds = query.get("node_ids", []), query.get("relation_ids", []), query.get("relation_kinds", [])
    for label, values, maximum in (("node_ids", node_ids, 200), ("relation_ids", relation_ids, 200),
                                   ("relation_kinds", kinds, len(RELATIONS))):
        if not isinstance(values, list) or len(values) > maximum or any(not isinstance(value, str) for value in values):
            _fail("graph_query_invalid", f"{label} must be a bounded array of text values")
    for node_id in node_ids:
        _uuid(node_id, "node_id")
        if node_id not in nodes:
            _fail("plan_graph_invalid_reference", "Requested graph node does not exist", 3,
                  {"node_id": node_id})
    for relation_id in relation_ids:
        _uuid(relation_id, "relation_id")
        if relation_id not in relations:
            _fail("plan_graph_invalid_reference", "Requested graph relation does not exist", 3,
                  {"relation_id": relation_id})
    if any(kind not in RELATIONS for kind in kinds):
        _fail("graph_query_invalid", "relation_kinds includes an unsupported relation type")
    fields = query.get("fields", ["id", "tree_kind", "node_kind", "summary"])
    if not isinstance(fields, list) or any(not isinstance(field, str) or field not in (_NODE_FIELDS | {"id"}) for field in fields):
        _fail("graph_query_invalid", "fields must be a list of supported graph node fields")
    fields = sorted(set(fields) | {"id", "tree_kind"})
    effective_kinds = set(kinds) if kinds else set(RELATIONS)
    query_hash = fingerprint({"node_ids": sorted(node_ids), "relation_ids": sorted(relation_ids),
                              "relation_kinds": sorted(effective_kinds), "direction": direction,
                              "max_depth": depth, "page_size": page_size, "fields": fields})
    index_hash = supplied_hash
    offset = 0
    cursor = query.get("cursor")
    if cursor:
        if not isinstance(cursor, str) or len(cursor) > 2048:
            _fail("graph_cursor_invalid", "cursor is invalid")
        try:
            raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
            data = strict_json_loads(raw, max_bytes=1024)
        except PmtError as exc:
            raise PmtError("graph_cursor_invalid", "cursor is invalid") from exc
        if not isinstance(data, dict) or data.get("source_hash") != pin.source_hash or data.get("index_hash") != index_hash or data.get("query_hash") != query_hash:
            _fail("graph_cursor_stale", "cursor does not match this source and query", 3)
        offset = data.get("offset")
        if type(offset) is not int or offset < 0:
            _fail("graph_cursor_invalid", "cursor offset is invalid")

    relation_rows = list(relations.values())
    incident = {node_id: [] for node_id in nodes}
    for relation in relation_rows:
        if relation.get("from") not in nodes or relation.get("to") not in nodes or relation.get("kind") not in RELATIONS:
            _fail("graph_index_corrupt", "Graph index contains an invalid relation", 3,
                  {"rebuild_required": True})
        incident[relation["from"]].append(relation)
        incident[relation["to"]].append(relation)
    selected_relations = set(relation_ids)
    paths = {}
    if relation_ids:
        seeds = sorted({endpoint for relation_id in relation_ids
                        for endpoint in (relations[relation_id]["from"], relations[relation_id]["to"])})
        for relation_id in relation_ids:
            selected_relations.add(relation_id)
    elif node_ids:
        seeds = sorted(set(node_ids))
    else:
        seeds = sorted(nodes)[:_INDEX_LIMIT]
    visited = set(seeds)
    for node_id in seeds:
        paths[node_id] = []
    frontier = list(seeds)
    traversal_truncated = len(nodes) > _INDEX_LIMIT and not (node_ids or relation_ids)
    traversal_boundary = set(nodes) - visited if traversal_truncated else set()
    if node_ids or relation_ids:
        for level in range(depth):
            next_frontier = []
            for node_id in frontier:
                for relation in incident[node_id]:
                    if relation["kind"] not in effective_kinds:
                        continue
                    if direction == "outgoing" and relation["from"] != node_id:
                        continue
                    if direction == "incoming" and relation["to"] != node_id:
                        continue
                    other = relation["to"] if relation["from"] == node_id else relation["from"]
                    if other in visited:
                        selected_relations.add(relation["id"])
                        continue
                    if len(visited) >= _INDEX_LIMIT:
                        traversal_truncated = True
                        traversal_boundary.add(other)
                        continue
                    visited.add(other)
                    paths[other] = paths[node_id] + [relation["id"]]
                    next_frontier.append(other)
                    selected_relations.add(relation["id"])
            frontier = next_frontier
            if not frontier:
                break
        # Nodes reached on the final allowed edge are included. Only an eligible
        # edge from that boundary to a still-unvisited node proves truncation.
        if frontier:
            for node_id in frontier:
                for relation in incident[node_id]:
                    if relation["kind"] not in effective_kinds:
                        continue
                    if direction == "outgoing" and relation["from"] != node_id:
                        continue
                    if direction == "incoming" and relation["to"] != node_id:
                        continue
                    other = relation["to"] if relation["from"] == node_id else relation["from"]
                    if other not in visited:
                        traversal_truncated = True
                        traversal_boundary.add(other)
                        selected_relations.add(relation["id"])
    else:
        selected_relations.update(relation["id"] for relation in relation_rows
                                  if relation["kind"] in effective_kinds)

    items = []
    for node_id in sorted(visited):
        node = nodes[node_id]
        projected = {field: node[field] for field in fields if field in node}
        items.append({"entity": "node", "value": projected, "path": paths.get(node_id, [])})
    for relation_id in sorted(selected_relations):
        relation = relations[relation_id]
        if relation["kind"] in effective_kinds:
            items.append({"entity": "relation", "value": relation})
    items.sort(key=lambda item: (item["entity"], item["value"]["id"]))
    if offset > len(items):
        _fail("graph_cursor_invalid", "cursor offset exceeds this result set")
    page = items[offset:offset + page_size]
    next_offset = offset + len(page)
    next_cursor = None
    if next_offset < len(items):
        token = canonical_json({"source_hash": pin.source_hash, "index_hash": index_hash,
                                "query_hash": query_hash, "offset": next_offset}).encode("utf-8")
        next_cursor = base64.urlsafe_b64encode(token).decode("ascii").rstrip("=")
    coverage = index_body.get("coverage") or {"segments": "unknown", "reason": "manifest_not_registered"}
    unknown = []
    segment_manifests = []
    current_manifest_nodes = set()
    stale_manifest_nodes = set()
    for manifest_id, stored in sorted((index_body.get("manifests") or {}).items()):
        if not isinstance(stored, dict) or not isinstance(stored.get("manifest"), dict):
            unknown.append({"kind": "document_segments", "reason_code": "manifest_index_corrupt",
                            "segment_id": manifest_id})
            continue
        manifest = stored["manifest"]
        manifest_nodes = set(manifest.get("node_ids", []))
        manifest_relations = set(manifest.get("relation_ids", []))
        matched = bool(manifest_nodes.intersection(visited) or manifest_relations.intersection(selected_relations))
        if not matched:
            continue
        if stored.get("source_hash") != pin.source_hash or manifest.get("source_pin", {}).get("source_hash") != pin.source_hash:
            stale_manifest_nodes.update(manifest_nodes.intersection(visited))
            continue
        current_manifest_nodes.update(manifest_nodes)
        segment_manifests.append({"segment_id": manifest_id, "revision": stored.get("revision"),
                                  "document_id": manifest.get("document_id"),
                                  "document_path": manifest.get("document_path"),
                                  "node_ids": sorted(manifest_nodes),
                                  "field_paths": sorted(manifest.get("field_paths", [])),
                                  "relation_ids": sorted(manifest_relations),
                                  "template_version": manifest.get("template_version"),
                                  "output_hash": manifest.get("output_hash"),
                                  "ownership": manifest.get("ownership")})
    if stale_manifest_nodes:
        unknown.append({"kind": "document_segments", "reason_code": "segment_manifest_stale",
                        "node_ids": sorted(stale_manifest_nodes)})
    unmatched_nodes = sorted(set(visited) - current_manifest_nodes)
    if unmatched_nodes:
        unknown.append({"kind": "document_segments", "reason_code": "segment_manifest_missing",
                        "node_ids": unmatched_nodes})
    if coverage.get("segments") != "complete":
        unknown.append({"kind": "document_segments", "reason_code": coverage.get("reason", "manifest_coverage_unknown"),
                        "node_ids": sorted(visited)})
    if traversal_truncated:
        unknown.append({"kind": "graph_traversal", "reason_code": "traversal_limit_or_depth",
                        "node_ids": sorted(traversal_boundary)})
    return {"source_pin": pin.to_dict(), "index": {"version": index_body.get("index_version"),
            "hash": index_hash, "graph_revision": index_body.get("graph_revision")},
            "items": page, "paths": {key: paths[key] for key in sorted(paths)},
            "complete": next_cursor is None and not traversal_truncated,
            "traversal_complete": not traversal_truncated, "next_cursor": next_cursor,
            "coverage": coverage, "segment_manifests": segment_manifests,
            "unknown": unknown, "result_count": len(page)}


def _get_current_index(db, conn, scope_id, source_pin):
    storage = Phase3Storage(db)
    try:
        value = storage.get_object(_INDEX_KIND, scope_id, scope_id, _INDEX_ACTOR, _INDEX_SESSION, conn=conn)
    except (TypeError, ValueError, json.JSONDecodeError):
        _fail("graph_index_corrupt", "Graph index JSON is invalid", 3, {"rebuild_required": True})
    if value is None:
        _fail("graph_index_stale", "No graph index is available for this source", 3,
              {"rebuild_required": True, "index_status": "absent"})
    if value["source_hash"] != source_pin.source_hash:
        _fail("graph_index_stale", "Graph index is pinned to a different source", 3,
              {"rebuild_required": True, "index_status": "stale",
               "index_source_hash": value["source_hash"], "source_hash": source_pin.source_hash})
    if value["state"] != "ready":
        _fail("graph_index_stale", "Graph index is not ready for queries", 3,
              {"rebuild_required": True, "index_status": value["state"]})
    body = value["body"]
    if not isinstance(body, dict) or body.get("source_pin", {}).get("source_hash") != source_pin.source_hash:
        _fail("graph_index_corrupt", "Graph index metadata does not match its storage pin", 3,
              {"rebuild_required": True})
    unhashed = {key: item for key, item in body.items() if key != "index_hash"}
    if body.get("index_hash") != fingerprint(unhashed):
        _fail("graph_index_corrupt", "Graph index integrity hash does not match", 3,
              {"rebuild_required": True})
    coverage = body.get("coverage")
    if not isinstance(coverage, dict) or coverage.get("segments") not in {"unknown", "complete"}:
        _fail("graph_index_corrupt", "Graph index coverage state is invalid", 3,
              {"rebuild_required": True})
    if coverage["segments"] == "complete":
        manifests, corrupt = _registered_manifests(conn, scope_id)
        verified = _stored_coverage(conn, scope_id, source_pin, body, manifests)
        if (corrupt or verified.get("segments") != "complete" or
                verified.get("certificate_hash") != coverage.get("certificate_hash")):
            _fail("graph_index_stale", "Complete manifest coverage no longer matches registered baseline rows", 3,
                  {"rebuild_required": True, "index_status": "coverage_stale"})
    return value, body


def _query_graph(db, conn, req, context):
    payload = _payload(req)
    expected = _expected_source(payload.get("expected_source"))
    verify_source_pin(expected, context["source_pin"])
    query = payload.get("query")
    if query is None:
        query = {key: payload[key] for key in
                 ("node_ids", "relation_ids", "relation_kinds", "direction", "max_depth", "page_size", "fields", "cursor")
                 if key in payload}
    if not isinstance(query, dict):
        _fail("graph_query_invalid", "query must be an object")
    index_row, body = _get_current_index(db, conn, context["scope_id"], context["source_pin"])
    graph_slice = query_graph_slice(body, query, context["source_pin"])
    graph_slice["index"]["revision"] = index_row["revision"]
    db.diagnostics.emit("planning.graph_slice_read", request_id=req.get("request_id"),
                        scope_id=context["scope_id"], source_hash=context["source_pin"].source_hash,
                        graph_hash=context["source_pin"].graph_hash,
                        graph_revision=context["source_pin"].graph_revision,
                        count=graph_slice["result_count"], incomplete=not graph_slice["complete"],
                        reason_code=(graph_slice["unknown"][0]["reason_code"] if graph_slice["unknown"] else None))
    return {"graph_slice": graph_slice, "source_pin": graph_slice["source_pin"],
            "index": graph_slice["index"], "complete": graph_slice["complete"],
            "cursor": graph_slice["next_cursor"], "coverage": graph_slice["coverage"]}


def _field_semantic(path):
    if path in _NESTED_FIELD_SEMANTICS:
        return _NESTED_FIELD_SEMANTICS[path]
    if path in _FIELD_SEMANTICS:
        return _FIELD_SEMANTICS[path]
    root = path.split(".", 1)[0]
    return _FIELD_SEMANTICS.get(root, "unknown")


def _relation_targets(index_body, node_id, direction):
    nodes, relations = index_body["nodes"], index_body["relations"]
    found, unknown = [], []
    for relation in relations.values():
        kind, source, target = relation["kind"], relation["from"], relation["to"]
        if kind == "evidence":
            if source == node_id or target == node_id:
                unknown.append({"relation_id": relation["id"], "kind": kind,
                                "reason_code": "evidence_relation_direction_unknown"})
            continue
        if kind in {"parent", "refines"}:
            if source == node_id and direction in {"outgoing", "both"}:
                found.append((target, relation, "decomposition_consumer"))
            if target == node_id and direction in {"incoming", "both"}:
                found.append((source, relation, "decomposition_context"))
        elif kind == "depends_on":
            # X depends_on Y: a change in Y is consumed by X.
            if target == node_id and direction in {"incoming", "both"}:
                found.append((source, relation, "dependent_consumer"))
        elif kind == "implements":
            if source == node_id and nodes[source].get("tree_kind") == "requirement" and direction in {"outgoing", "both"}:
                found.append((target, relation, "implementation_consumer"))
            if target == node_id and nodes[target].get("tree_kind") == "implementation" and direction in {"incoming", "both"}:
                found.append((source, relation, "requirement_consumer"))
    return found, unknown


def _calculate_impact(db, conn, req, context, *, index_override=None):
    payload = _payload(req)
    expected = _expected_source(payload.get("expected_source"))
    verify_source_pin(expected, context["source_pin"])
    preview = payload.get("change_preview")
    if not isinstance(preview, dict) or not isinstance(preview.get("operations"), list):
        _fail("impact_request_invalid", "change_preview with validated operations is required")
    change_id = _uuid(preview.get("change_id"), "change_preview.change_id")
    preview_pin = _expected_source(preview.get("source_pin"))
    verify_source_pin(expected, preview_pin)
    change_set = payload.get("change_set")
    validated = prepare_change_set(context["graph"], change_set, req["request_id"])
    if validated["change_id"] != change_id or validated["change_set_hash"] != preview.get("change_set_hash"):
        _fail("change_preview_conflict", "change_preview does not match the supplied change_set", 3)
    if canonical_json(_safe_operation_summary(validated)) != canonical_json(preview.get("operations")):
        _fail("change_preview_conflict", "change_preview operations do not match validated changes", 3)
    expected_new = preview.get("expected_new_source") or {}
    if (expected_new.get("graph_schema") != validated["graph"]["schema_version"] or
            expected_new.get("graph_revision") != validated["graph"]["graph_version"] or
            expected_new.get("graph_hash") != validated["source_hash"]):
        _fail("change_preview_conflict", "change_preview candidate source does not match validated changes", 3)
    version = payload.get("rule_version", _FIELD_RULE_VERSION)
    if version != _FIELD_RULE_VERSION:
        _fail("impact_rule_version_unsupported", "Requested field semantic rule version is unsupported", 3,
              {"supported": _FIELD_RULE_VERSION})
    depth = payload.get("max_depth", 4)
    if type(depth) is not int or not 1 <= depth <= 8:
        _fail("impact_request_invalid", "max_depth must be between 1 and 8")
    if index_override is None:
        index_row, index_body = _get_current_index(db, conn, context["scope_id"], context["source_pin"])
    else:
        # Internal adapter seam for a verified prior graph/manifest projection.
        # No dispatcher passes a wire field into this argument.
        index_row, index_body = index_override
        verify_source_pin(context["source_pin"], index_body.get("source_pin"))
        unhashed = {key: value for key, value in index_body.items() if key != "index_hash"}
        if index_body.get("index_hash") != fingerprint(unhashed):
            _fail("graph_index_corrupt", "Trusted prior index projection failed its content hash", 3)

    field_changes, starts, unknown = [], [], []
    for operation in _safe_operation_summary(validated):
        if not isinstance(operation, dict):
            _fail("impact_request_invalid", "change_preview operation is invalid")
        op, object_id = operation.get("op"), operation.get("id")
        fields = list(operation.get("fields") or []) + list(operation.get("clear") or [])
        field_hashes = operation.get("field_fingerprints") or {}
        if op in {"update", "create"}:
            if object_id:
                _uuid(object_id, "operation.id")
                starts.append({"node_id": object_id, "operation": op, "fields": fields,
                               "reason_code": "node_changed"})
            for field in sorted(set(fields)):
                semantic = _field_semantic(field)
                hashes = field_hashes.get(field, {})
                item = {"node_id": object_id, "field": field, "semantic": semantic,
                        "before_hash": hashes.get("before"), "after_hash": hashes.get("after"),
                        "change_kind": "clear" if field in (operation.get("clear") or []) else "set"}
                field_changes.append(item)
                if semantic == "unknown":
                    unknown.append({"kind": "field", "node_id": object_id, "field": field,
                                    "reason_code": "field_semantics_require_review"})
        elif op in {"relate", "unrelate"}:
            targets = operation.get("target_ids") or []
            if not targets:
                unknown.append({"kind": "relation", "relation_id": object_id,
                                "reason_code": "old_relation_path_missing"})
            for node_id in targets:
                _uuid(node_id, "relation.target_id")
                starts.append({"node_id": node_id, "operation": op, "fields": ["relations"],
                               "relation_id": object_id, "relation_kind": operation.get("kind"),
                               "reason_code": "relation_changed",
                               "certainty": "unknown" if op == "relate" or operation.get("kind") == "evidence" else "known"})
            if op == "unrelate" and object_id not in index_body["relations"]:
                unknown.append({"kind": "relation", "relation_id": object_id,
                                "reason_code": "old_relation_path_missing"})
            if operation.get("kind") == "evidence":
                unknown.append({"kind": "relation", "relation_id": object_id,
                                "reason_code": "evidence_relation_direction_unknown"})
            elif op == "relate":
                unknown.append({"kind": "relation", "relation_id": object_id,
                                "reason_code": "new_relation_requires_review"})
        elif op == "deprecate":
            targets = operation.get("target_ids") or ([object_id] if object_id else [])
            unknown.append({"kind": "branch", "node_ids": sorted(targets),
                            "reason_code": "branch_deprecation_requires_review"})
            for node_id in targets:
                _uuid(node_id, "deprecated.node_id")
                starts.append({"node_id": node_id, "operation": op, "fields": ["retired"],
                               "reason_code": "branch_deprecated"})
        else:
            unknown.append({"kind": "change", "reason_code": "change_operation_unsupported"})

    known_nodes = {}
    change_paths = {}
    for item in starts:
        node_id = item["node_id"]
        if node_id not in index_body["nodes"]:
            reason = "created_node_not_in_current_source" if item["operation"] == "create" else "source_node_missing"
            unknown.append({"kind": "node", "node_id": node_id, "reason_code": reason})
            continue
        changed_semantics = [_field_semantic(field) for field in item["fields"]]
        certainty = "unknown" if "unknown" in changed_semantics or item.get("certainty") == "unknown" else "known"
        item["certainty"] = certainty
        entry = known_nodes.setdefault(node_id, {"node_id": node_id, "certainty": certainty,
                                                 "paths": [], "causes": [], "semantics": []})
        if certainty == "unknown":
            entry["certainty"] = "unknown"
        entry["causes"].append({"operation": item["operation"], "change_id": change_id,
                                "reason_code": item["reason_code"], "relation_id": item.get("relation_id")})
        entry["semantics"] = sorted(set(entry["semantics"] + changed_semantics))
        entry["paths"].append([])
        change_paths.setdefault(node_id, set()).update(item["fields"])

    queue = [(item["node_id"], [], 0, item) for item in starts if item["node_id"] in index_body["nodes"]]
    visited_depth = {}
    traversal_count = 0
    traversal_limited = False
    for node_id, path, level, cause in queue:
        if traversal_limited:
            break
        prior_depth = visited_depth.get((node_id, cause["node_id"]), 99)
        if prior_depth <= level:
            continue
        visited_depth[(node_id, cause["node_id"])] = level
        if level >= depth:
            targets, relation_unknowns = _relation_targets(index_body, node_id, "both")
            if targets:
                unknown.append({"kind": "graph", "node_id": node_id, "reason_code": "impact_depth_limit"})
            unknown.extend(relation_unknowns)
            continue
        targets, relation_unknowns = _relation_targets(index_body, node_id, "both")
        unknown.extend(relation_unknowns)
        for target_id, relation, reason in targets:
            traversal_count += 1
            if traversal_count > 5000:
                unknown.append({"kind": "graph", "node_id": node_id, "reason_code": "impact_traversal_limit"})
                traversal_limited = True
                break
            next_path = path + [relation["id"]]
            certainty = cause.get("certainty", "known")
            entry = known_nodes.setdefault(target_id, {"node_id": target_id, "certainty": certainty,
                                                        "paths": [], "causes": [], "semantics": []})
            if certainty == "unknown":
                entry["certainty"] = "unknown"
            if next_path not in entry["paths"]:
                entry["paths"].append(next_path)
            entry["causes"].append({"operation": cause["operation"], "change_id": change_id,
                                    "relation_id": relation["id"], "relation_kind": relation["kind"],
                                    "reason_code": reason})
            queue.append((target_id, next_path, level + 1, cause))

    step_refs, verification_refs = set(), set()
    for node_id, impact in known_nodes.items():
        node = index_body["nodes"][node_id]
        links = node.get("work_item_step_refs") or {}
        step_refs.update(links.get("work", []))
        step_refs.update(links.get("item", []))
        step_refs.update(links.get("step", []))
        if any(semantic in {"verification", "contract", "premise", "unknown"} for semantic in impact["semantics"]):
            verification_refs.update(node.get("criteria", []))
            verification_refs.update(node.get("tests", []))

    documents, current_manifest_nodes = [], set()
    for segment_id, stored in sorted((index_body.get("manifests") or {}).items()):
        manifest = stored.get("manifest") if isinstance(stored, dict) else None
        if not isinstance(manifest, dict):
            unknown.append({"kind": "document_segment", "segment_id": segment_id,
                            "reason_code": "manifest_index_corrupt"})
            continue
        manifest_nodes = set(manifest.get("node_ids", []))
        hit_nodes = manifest_nodes.intersection(known_nodes)
        if not hit_nodes:
            continue
        stale = (stored.get("source_hash") != context["source_pin"].source_hash or
                 manifest.get("source_pin", {}).get("source_hash") != context["source_pin"].source_hash)
        direct_fields = set().union(*(change_paths.get(node_id, set()) for node_id in hit_nodes))
        manifest_fields = set(manifest.get("field_paths", []))
        path_hit = any(left == right or left.startswith(right + ".") or right.startswith(left + ".")
                       for left in direct_fields for right in manifest_fields)
        relation_hit = any(cause.get("relation_id") in set(manifest.get("relation_ids", []))
                           for node_id in hit_nodes for cause in known_nodes[node_id]["causes"] if cause.get("relation_id"))
        if stale:
            unknown.append({"kind": "document_segment", "segment_id": segment_id,
                            "node_ids": sorted(hit_nodes), "reason_code": "segment_manifest_stale"})
        elif path_hit or relation_hit or not direct_fields:
            current_manifest_nodes.update(hit_nodes)
            documents.append({"segment_id": segment_id, "document_id": manifest["document_id"],
                              "document_path": manifest["document_path"], "node_ids": sorted(hit_nodes),
                              "certainty": "unknown" if any(known_nodes[node_id]["certainty"] == "unknown" for node_id in hit_nodes) else "known",
                              "reason_code": "source_dependency_path"})
    missing_nodes = sorted(set(known_nodes) - current_manifest_nodes)
    if missing_nodes:
        unknown.append({"kind": "document_segments", "node_ids": missing_nodes,
                        "reason_code": "segment_manifest_missing"})
    coverage = index_body.get("coverage") or {}
    if coverage.get("segments") != "complete":
        unknown.append({"kind": "document_coverage", "node_ids": sorted(known_nodes),
                        "reason_code": coverage.get("reason", "baseline_manifest_unknown")})
    unique_unknown = {}
    for item in unknown:
        key = fingerprint(item)
        unique_unknown[key] = item
    unknown = [unique_unknown[key] for key in sorted(unique_unknown)]
    result = {"change_id": change_id, "change_set_hash": validated["change_set_hash"],
              "source_pin": context["source_pin"].to_dict(),
              "before_source_pin": context["source_pin"].to_dict(),
              "expected_new_source": {"graph_schema": validated["graph"]["schema_version"],
                                      "graph_revision": validated["graph"]["graph_version"],
                                      "graph_hash": validated["source_hash"]},
              "index": {"revision": index_row["revision"], "hash": index_body["index_hash"]},
              "rule_version": _FIELD_RULE_VERSION, "field_changes": field_changes,
              "known": sorted(known_nodes.values(), key=lambda item: item["node_id"]),
              "documents": sorted(documents, key=lambda item: item["segment_id"]),
              "steps": sorted(step_refs), "verifications": sorted(verification_refs),
              "unknown": unknown, "complete": not unknown}
    event_name = "reconciliation.graph_impact_incomplete" if unknown else "reconciliation.graph_impact_calculated"
    db.diagnostics.emit(event_name, request_id=req.get("request_id"), scope_id=context["scope_id"],
                        record_id=change_id, source_hash=context["source_pin"].source_hash,
                        graph_hash=context["source_pin"].graph_hash,
                        graph_revision=context["source_pin"].graph_revision, rule_version=_FIELD_RULE_VERSION,
                        count=len(known_nodes) + len(unknown), incomplete=bool(unknown),
                        reason_code=unknown[0]["reason_code"] if unknown else None)
    return result


def _rebuild_graph_index(db, req):
    prior = _request_replay(db, req)
    if prior is not None:
        return prior
    payload = _payload(req)
    expected = _expected_source(payload.get("expected_source"))
    with closing(db.connect()) as conn:
        context = _source_graph(db, conn, req)
        verify_source_pin(expected, context["source_pin"])
        row = conn.execute("SELECT revision FROM phase3_objects WHERE kind=? AND id=?",
                           (_INDEX_KIND, context["scope_id"])).fetchone()
        expected_revision = row[0] if row else 0
        manifests, corrupt = _registered_manifests(conn, context["scope_id"])
        base_index = _index_body(context["graph"], context["source_pin"], manifests, corrupt)
        coverage = _stored_coverage(conn, context["scope_id"], context["source_pin"], base_index, manifests)
    index_body = _index_body(context["graph"], context["source_pin"], manifests, corrupt, coverage)
    with closing(db.connect()) as conn:
        verified = _source_graph(db, conn, req)
    verify_source_pin(expected, verified["source_pin"])
    if verified["raw_sha256"] != context["raw_sha256"]:
        _fail("source_conflict", "Graph source changed during index rebuild", 3)

    def commit(conn, request):
        _scope_context(db, conn, request)
        require_workspace_claim(db, conn, request, str(context["workspace"]), [context["relative_path"]])
        current_row = conn.execute("SELECT revision FROM phase3_objects WHERE kind=? AND id=?",
                                   (_INDEX_KIND, context["scope_id"])).fetchone()
        current_revision = current_row[0] if current_row else 0
        if current_revision != expected_revision:
            _fail("revision_conflict", "Graph index changed while the rebuild was staged", 3, {
                "expected_revision": expected_revision, "current_revision": current_revision})
        storage = Phase3Storage(db)
        receipt = storage.put_object(_INDEX_KIND, context["scope_id"], context["scope_id"], _INDEX_ACTOR,
                                     _INDEX_SESSION, context["source_pin"].source_hash, expected_revision,
                                     index_body, state="ready", request_id=request["request_id"], conn=conn)
        from ..lifecycle import _event
        _event(conn, request, event_id=_derived_event_id(request, "graph_index_rebuilt"),
               event_type="planning.graph_index_rebuilt",
               scope_id=context["scope_id"], new_revision=context["source_pin"].graph_revision,
               payload={"source_hash": context["source_pin"].source_hash,
                        "graph_hash": context["source_pin"].graph_hash,
                        "index_hash": index_body["index_hash"], "index_revision": receipt["revision"],
                        "node_count": len(index_body["nodes"]), "relation_count": len(index_body["relations"])})
        return {"source_pin": context["source_pin"].to_dict(),
                "index": {"version": index_body["index_version"], "revision": receipt["revision"],
                          "hash": index_body["index_hash"]},
                "node_count": len(index_body["nodes"]), "relation_count": len(index_body["relations"]),
                "coverage": index_body["coverage"], "replayed": receipt["replayed"]}

    return db.run_request(req, commit)


def _manifest_relative_path(value):
    if not isinstance(value, str) or not value.strip() or "\\" in value:
        _fail("segment_manifest_invalid", "document_path must be a workspace-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        _fail("segment_manifest_invalid", "document_path must remain inside the workspace")
    return path.as_posix()


def _register_segment_manifest(db, req):
    prior = _request_replay(db, req)
    if prior is not None:
        return prior
    payload = _payload(req)
    if "coverage_certificate" in payload:
        if "manifest" in payload:
            _fail("coverage_certificate_invalid", "Register one manifest or one coverage certificate per request")
        return _register_coverage_certificate(db, req, payload)
    expected = _expected_source(payload.get("expected_source"))
    manifest = payload.get("manifest")
    if not isinstance(manifest, dict):
        _fail("segment_manifest_invalid", "manifest must be an object")
    allowed = {"segment_id", "document_id", "document_path", "node_ids", "field_paths", "relation_ids",
               "template_version", "output_hash", "ownership", "source_pin"}
    if set(manifest) - allowed:
        _fail("segment_manifest_invalid", "manifest has unsupported fields",
              details={"fields": sorted(set(manifest) - allowed)})
    segment_id = _uuid(manifest.get("segment_id"), "manifest.segment_id")
    document_id = manifest.get("document_id")
    if not isinstance(document_id, str) or not document_id.strip() or len(document_id) > 500:
        _fail("segment_manifest_invalid", "document_id must be nonempty text")
    document_path = _manifest_relative_path(manifest.get("document_path", document_id))
    nodes, fields, relations = manifest.get("node_ids", []), manifest.get("field_paths", []), manifest.get("relation_ids", [])
    for label, values, maximum in (("node_ids", nodes, 500), ("relation_ids", relations, 1000)):
        if not isinstance(values, list) or len(values) > maximum:
            _fail("segment_manifest_invalid", f"{label} must be a bounded array")
        for item in values:
            _uuid(item, label)
    if not isinstance(fields, list) or len(fields) > 500 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 200 for item in fields):
        _fail("segment_manifest_invalid", "field_paths must be a bounded list of nonempty paths")
    template = manifest.get("template_version")
    if not isinstance(template, str) or not template.strip() or len(template) > 100:
        _fail("segment_manifest_invalid", "template_version must be nonempty text up to 100 characters")
    output_hash = manifest.get("output_hash")
    if not isinstance(output_hash, str) or len(output_hash) != 64 or any(ch not in "0123456789abcdef" for ch in output_hash):
        _fail("segment_manifest_invalid", "output_hash must be lowercase SHA-256")
    ownership = manifest.get("ownership")
    if ownership not in {"generated", "manual"}:
        _fail("segment_manifest_invalid", "ownership must be generated or manual")
    manifest_pin = _expected_source(manifest.get("source_pin"))
    verify_source_pin(expected, manifest_pin)
    expected_manifest_revision = payload.get("expected_manifest_revision")
    if type(expected_manifest_revision) is not int or expected_manifest_revision < 0:
        _fail("segment_manifest_invalid", "expected_manifest_revision must be a nonnegative integer")

    with closing(db.connect()) as conn:
        context = _source_graph(db, conn, req)
    verify_source_pin(expected, context["source_pin"])
    value = {"segment_id": segment_id, "document_id": document_id, "document_path": document_path,
             "node_ids": sorted(set(nodes)), "field_paths": sorted(set(fields)),
             "relation_ids": sorted(set(relations)), "template_version": template,
             "output_hash": output_hash, "ownership": ownership, "source_pin": expected.to_dict()}

    def commit(conn, request):
        _scope_context(db, conn, request)
        require_workspace_claim(db, conn, request, str(context["workspace"]),
                                [context["relative_path"], document_path])
        index_row = conn.execute("SELECT revision,source_hash,body_json FROM phase3_objects WHERE kind=? AND id=?",
                                 (_INDEX_KIND, context["scope_id"])).fetchone()
        if index_row is None or index_row["source_hash"] != expected.source_hash:
            _fail("graph_index_stale", "Current graph index must be rebuilt before manifest registration", 3,
                  {"rebuild_required": True})
        try:
            index_body = json.loads(index_row["body_json"])
        except (TypeError, ValueError):
            _fail("graph_index_corrupt", "Graph index cannot accept a manifest until rebuilt", 3,
                  {"rebuild_required": True})
        if index_body.get("index_hash") != fingerprint({key: v for key, v in index_body.items() if key != "index_hash"}):
            _fail("graph_index_corrupt", "Graph index integrity hash does not match", 3,
                  {"rebuild_required": True})
        if any(item not in index_body.get("nodes", {}) for item in value["node_ids"]):
            _fail("segment_manifest_invalid_reference", "Manifest references a node outside the pinned source", 3)
        if any(item not in index_body.get("relations", {}) for item in value["relation_ids"]):
            _fail("segment_manifest_invalid_reference", "Manifest references a relation outside the pinned source", 3)
        old = conn.execute("SELECT revision FROM phase3_objects WHERE kind='segment_manifest' AND id=? AND scope_id=?",
                           (segment_id, context["scope_id"])).fetchone()
        actual_revision = old[0] if old else 0
        if actual_revision != expected_manifest_revision:
            _fail("revision_conflict", "Segment manifest revision changed", 3,
                  {"expected_revision": expected_manifest_revision, "current_revision": actual_revision})
        storage = Phase3Storage(db)
        manifest_receipt = storage.put_object("segment_manifest", segment_id, context["scope_id"], _INDEX_ACTOR,
                                              _INDEX_SESSION, expected.source_hash, actual_revision, value,
                                              state="ready", request_id=request["request_id"], conn=conn)
        manifests, manifests_corrupt = _registered_manifests(conn, context["scope_id"])
        manifests[segment_id] = {"revision": manifest_receipt["revision"],
                                 "source_hash": expected.source_hash, "manifest": value}
        graph_projection = {"project_id": index_body["project_id"],
                            "schema_version": index_body["graph_schema"],
                            "graph_version": index_body["graph_revision"],
                            "nodes": list(index_body["nodes"].values()),
                            "relations": list(index_body["relations"].values())}
        index_body = _index_body(graph_projection, expected, manifests, manifests_corrupt)
        index_receipt = storage.put_object(_INDEX_KIND, context["scope_id"], context["scope_id"], _INDEX_ACTOR,
                                           _INDEX_SESSION, expected.source_hash, index_row["revision"],
                                           index_body, state="ready", request_id=request["request_id"], conn=conn)
        from ..lifecycle import _event
        _event(conn, request, event_id=_derived_event_id(request, "segment_manifest_registered"),
               event_type="planning.segment_manifest_registered",
               scope_id=context["scope_id"], new_revision=manifest_receipt["revision"],
               payload={"segment_id": segment_id, "document_id": document_id,
                        "source_hash": expected.source_hash, "manifest_hash": fingerprint(value),
                        "index_hash": index_body["index_hash"]})
        return {"segment_id": segment_id, "manifest_revision": manifest_receipt["revision"],
                "manifest_hash": fingerprint(value),
                "source_pin": expected.to_dict(),
                "index": {"revision": index_receipt["revision"], "hash": index_body["index_hash"]},
                "coverage": index_body["coverage"]}

    return db.run_request(req, commit)


def _register_coverage_certificate(db, req, payload):
    certificate = payload.get("coverage_certificate")
    if not isinstance(certificate, dict):
        _fail("coverage_certificate_invalid", "coverage_certificate must be an object")
    required = {"source_pin", "document_paths", "manifest_refs", "manifest_set_hash",
                "expected_node_ids", "expected_relation_ids", "expected_field_paths",
                "dependency_registry_version", "template_versions", "production_receipt_ref",
                "source_table_version"}
    if set(certificate) != required:
        _fail("coverage_certificate_invalid", "coverage_certificate has missing or unsupported fields",
              details={"missing": sorted(required - set(certificate)),
                       "extra": sorted(set(certificate) - required)})
    expected = _expected_source(certificate["source_pin"])
    expected_coverage_revision = payload.get("expected_coverage_revision")
    if type(expected_coverage_revision) is not int or expected_coverage_revision < 0:
        _fail("coverage_certificate_invalid", "expected_coverage_revision must be nonnegative")
    document_paths = sorted({_manifest_relative_path(path) for path in certificate["document_paths"]})
    if not document_paths:
        _fail("coverage_certificate_invalid", "document_paths cannot be empty")
    manifest_refs = certificate["manifest_refs"]
    manifest_set_hash = manifest_set_fingerprint(manifest_refs, document_paths, expected)
    if certificate["manifest_set_hash"] != manifest_set_hash:
        _fail("coverage_certificate_invalid", "manifest_set_hash does not match the declared baseline")
    expected_nodes, expected_relations = certificate["expected_node_ids"], certificate["expected_relation_ids"]
    for label, values in (("expected_node_ids", expected_nodes), ("expected_relation_ids", expected_relations)):
        if not isinstance(values, list):
            _fail("coverage_certificate_invalid", f"{label} must be an array")
        for value in values:
            _uuid(value, label)
        if len(set(values)) != len(values):
            _fail("coverage_certificate_invalid", f"{label} contains duplicates")
    fields = certificate["expected_field_paths"]
    if not isinstance(fields, dict):
        _fail("coverage_certificate_invalid", "expected_field_paths must map node IDs to field-path arrays")
    for node_id, paths in fields.items():
        _uuid(node_id, "expected_field_paths.node_id")
        if not isinstance(paths, list) or not paths:
            _fail("coverage_certificate_invalid", "Every expected node needs at least one template field dependency")
        if any(not isinstance(path, str) or not path.strip() or path.split(".", 1)[0] not in (_NODE_FIELDS | {"id"}) for path in paths):
            _fail("coverage_certificate_invalid", "expected_field_paths includes an unsupported node field")
    for label in ("dependency_registry_version", "production_receipt_ref", "source_table_version"):
        value = certificate[label]
        if not isinstance(value, str) or not value.strip() or len(value) > 1000:
            _fail("coverage_certificate_invalid", f"{label} must be nonempty text")
    template_versions = certificate["template_versions"]
    if not isinstance(template_versions, list) or not template_versions or any(
            not isinstance(value, str) or not value.strip() or len(value) > 100 for value in template_versions):
        _fail("coverage_certificate_invalid", "template_versions must be a nonempty list of version IDs")
    if len(set(template_versions)) != len(template_versions):
        _fail("coverage_certificate_invalid", "template_versions contains duplicates")
    with closing(db.connect()) as conn:
        context = _source_graph(db, conn, req)
    verify_source_pin(expected, context["source_pin"])
    if set(expected_nodes) != set(node["id"] for node in context["graph"]["nodes"]):
        _fail("coverage_certificate_invalid", "expected_node_ids must enumerate the complete current graph", 3)
    if set(expected_relations) != set(relation["id"] for relation in context["graph"]["relations"]):
        _fail("coverage_certificate_invalid", "expected_relation_ids must enumerate the complete current graph", 3)
    if set(fields) != set(expected_nodes):
        _fail("coverage_certificate_invalid", "expected_field_paths must describe every current node", 3)

    def commit(conn, request):
        _scope_context(db, conn, request)
        require_workspace_claim(db, conn, request, str(context["workspace"]),
                                [context["relative_path"], *document_paths])
        index_value, index_body = _get_current_index(db, conn, context["scope_id"], expected)
        manifests, corrupt = _registered_manifests(conn, context["scope_id"])
        current = {key: value for key, value in manifests.items() if value.get("source_hash") == expected.source_hash}
        actual_refs = sorted([{"segment_id": key, "manifest_hash": _manifest_ref_hash(value["manifest"])}
                              for key, value in current.items()], key=lambda item: item["segment_id"])
        if actual_refs != sorted(manifest_refs, key=lambda item: item["segment_id"]):
            _fail("coverage_certificate_invalid", "manifest_refs must exactly match current registered segment manifests", 3)
        normalized_certificate = dict(certificate)
        normalized_certificate["document_paths"] = document_paths
        coverage = _coverage_from_certificate(normalized_certificate, index_body, manifests, expected)
        if coverage["segments"] != "complete":
            _fail("coverage_certificate_incomplete", "Baseline does not cover all source dependencies", 3,
                  {"reason_code": coverage["reason"]})
        row = conn.execute("SELECT revision FROM phase3_objects WHERE kind='segment_coverage' AND id=? AND scope_id=?",
                           (context["scope_id"], context["scope_id"])).fetchone()
        actual_revision = row[0] if row else 0
        if actual_revision != expected_coverage_revision:
            _fail("revision_conflict", "Coverage certificate revision changed", 3,
                  {"expected_revision": expected_coverage_revision, "current_revision": actual_revision})
        storage = Phase3Storage(db)
        certificate_body = normalized_certificate | {"coverage_status": "complete",
                                                     "coverage_source": "F4_baseline_producer",
                                                     "coverage_certificate_hash": coverage["certificate_hash"]}
        certificate_receipt = storage.put_object("segment_coverage", context["scope_id"], context["scope_id"],
                                                 _INDEX_ACTOR, _INDEX_SESSION, expected.source_hash,
                                                 actual_revision, certificate_body, state="ready",
                                                 request_id=request["request_id"], conn=conn)
        index_body["coverage"] = coverage
        index_body.pop("index_hash", None)
        index_body["index_hash"] = fingerprint(index_body)
        index_receipt = storage.put_object(_INDEX_KIND, context["scope_id"], context["scope_id"], _INDEX_ACTOR,
                                           _INDEX_SESSION, expected.source_hash, index_value["revision"],
                                           index_body, state="ready", request_id=request["request_id"], conn=conn)
        from ..lifecycle import _event
        _event(conn, request, event_id=_derived_event_id(request, "segment_baseline_registered"),
               event_type="planning.segment_baseline_registered", scope_id=context["scope_id"],
               new_revision=certificate_receipt["revision"],
               payload={"source_hash": expected.source_hash, "manifest_set_hash": manifest_set_hash,
                        "dependency_registry_version": certificate["dependency_registry_version"],
                        "template_versions": sorted(template_versions),
                        "production_receipt_ref": certificate["production_receipt_ref"],
                        "document_count": len(document_paths), "segment_count": len(manifests),
                        "index_hash": index_body["index_hash"]})
        return {"coverage": coverage, "coverage_revision": certificate_receipt["revision"],
                "source_pin": expected.to_dict(), "index": {"revision": index_receipt["revision"],
                                                                 "hash": index_body["index_hash"]}}

    return db.run_request(req, commit)


def _commit_change(db, req, context, intent_id, graph, pin, old_pin, prepared):
    storage = Phase3Storage(db)
    result_data = {"graph_version": graph["graph_version"], "graph_hash": pin.graph_hash,
                   "source_pin": pin.to_dict(), "before_source_pin": old_pin.to_dict(),
                   "old_graph_hash": old_pin.graph_hash,
                   "old_graph_revision": old_pin.graph_revision,
                   "created_ids": prepared["created_ids"], "retired_ids": prepared["retired_ids"],
                   "temp_id_map": prepared["temp_id_map"], "inherited": prepared["inherited"],
                   "change_id": prepared["change_id"], "change_set_hash": prepared["change_set_hash"],
                   "journal_id": intent_id, "journal_status": "completed",
                   "recovery_ref": (context.get("recovery_ref") or
                                    (context.get("publish_receipt") or {}).get("recovery_ref")),
                   "operations": _safe_operation_summary(prepared)}

    def commit(conn, request):
        _scope_context(db, conn, request)
        require_workspace_claim(db, conn, request, str(context["workspace"]), [context["relative_path"]])
        current_intent = storage.get_intent(intent_id, context["scope_id"], req["actor"], req["session_id"], conn=conn)
        if current_intent is None or current_intent["body"].get("stage") not in {"file_written", "prepared"}:
            _fail("graph_change_journal_conflict", "Graph change journal is not ready to commit", 3)
        key = ("graph_index", context["scope_id"])
        row = conn.execute("SELECT revision FROM phase3_objects WHERE kind=? AND id=?", key).fetchone()
        expected_revision = row[0] if row else 0
        manifests, manifests_corrupt = _registered_manifests(conn, context["scope_id"])
        index_body = _index_body(graph, pin, manifests, manifests_corrupt)
        index_receipt = storage.put_object(_INDEX_KIND, context["scope_id"], context["scope_id"], _INDEX_ACTOR,
                                           _INDEX_SESSION, pin.source_hash, expected_revision,
                                           index_body, state="ready", request_id=req["request_id"], conn=conn)
        result_data["index"] = {"revision": index_receipt["revision"], "hash": index_body["index_hash"]}
        from ..lifecycle import _event
        _event(conn, request, event_id=_event_id(request), event_type="planning.graph_change_published",
               scope_id=context["scope_id"], old_revision=old_pin.graph_revision,
               new_revision=graph["graph_version"], reason=prepared["reason"],
               payload={"repository_id": context["repository_id"], "graph_revision": graph["graph_version"],
                        "graph_hash": pin.graph_hash, "source_hash": pin.source_hash,
                        "index_hash": index_body["index_hash"],
                        "node_ids": sorted(set(result_data["created_ids"] + result_data["retired_ids"] +
                                               [item.get("id") for item in prepared["operations"] if item.get("id")])),
                        "relation_kinds": sorted(set(item["kind"] for item in prepared["operations"] if item.get("kind"))),
                        "evidence_refs": prepared["evidence_refs"], "journal_id": intent_id})
        storage.update_intent(intent_id, current_intent["body"].get("stage"), "completed",
                              context["scope_id"], req["actor"], req["session_id"],
                              {"old_graph_hash": old_pin.graph_hash, "new_graph_hash": pin.graph_hash,
                               "old_graph_revision": old_pin.graph_revision,
                               "new_graph_revision": graph["graph_version"],
                               "new_file_sha256": hashlib.sha256((canonical_json(graph) + "\n").encode("utf-8")).hexdigest()},
                              conn=conn)
        return result_data

    return db.run_request(req, commit)


def _execute_apply(db, req, *, recovering=False):
    prior = _request_replay(db, req)
    if prior is not None:
        return prior
    payload = _payload(req)
    with closing(db.connect()) as conn:
        context = _source_graph(db, conn, req)
    expected = _expected_source(payload.get("expected_source"))
    verify_source_pin(expected, context["source_pin"])
    changes = payload.get("change_set")
    prepared = prepare_change_set(context["graph"], changes, req["request_id"])
    journal = _journal_start(db, req, context, expected, prepared)
    intent_id = journal["id"]
    if journal.get("replayed") and not recovering:
        _fail("graph_change_recovery_required", "This request has an unfinished graph-change journal", 3,
              {"journal_id": intent_id})
    _write_candidate(db, req, context, prepared, expected, req["request_id"])
    with closing(db.connect()) as conn:
        new_context = _source_graph(db, conn, req)
    new_context["recovery_ref"] = context.get("recovery_ref")
    new_context["publish_receipt"] = context.get("publish_receipt")
    new_context["stage_path"] = context.get("stage_path")
    new_pin = new_context["source_pin"]
    if new_pin.graph_hash != prepared["source_hash"] or new_pin.graph_revision != prepared["graph"]["graph_version"]:
        _fail("graph_publish_verification_failed", "Published graph does not match the validated candidate", 4)
    if new_pin.reviewed_commit != expected.reviewed_commit or new_pin.selected_ref != expected.selected_ref:
        _fail("source_conflict", "Git HEAD or selected ref changed during graph publication; both versions were retained", 3)
    new_context = _confirm_publication(db, req, context, expected, req["request_id"], intent_id,
                                       context["raw_sha256"], hashlib.sha256(prepared["wire"]).hexdigest(),
                                       prepared["source_hash"])
    new_context["stage_path"] = context.get("stage_path")
    storage = Phase3Storage(db)
    with closing(db.connect()) as conn:
        current_intent = storage.get_intent(intent_id, context["scope_id"], req["actor"], req["session_id"], conn=conn)
    if current_intent["body"].get("stage") != "file_written":
        storage.update_intent(intent_id, current_intent["body"].get("stage"), "file_written", context["scope_id"],
                              req["actor"], req["session_id"],
                              {"new_file_sha256": new_context["raw_sha256"],
                               "new_graph_hash": new_pin.graph_hash,
                               "new_graph_revision": new_pin.graph_revision,
                               "recovery_ref": context.get("recovery_ref"),
                               "publish_receipt": context.get("publish_receipt")})
    result, code = _commit_change(db, req, context, intent_id, new_context["graph"], new_pin, expected, prepared)
    if code == 0:
        _cleanup_candidate_stage(new_context, hashlib.sha256(prepared["wire"]).hexdigest())
    return result, code


def _recover_graph_change(db, req):
    replay = _request_replay(db, req)
    if replay is not None:
        return replay
    payload = _payload(req)
    original = payload.get("original_request")
    if not isinstance(original, dict) or original.get("operation") != "apply_graph_change":
        _fail("recovery_request_invalid", "original_request must be the original apply_graph_change request")
    from ..service import normalize_request
    original = normalize_request(original)
    original_id = _uuid(original.get("request_id"), "original_request.request_id")
    if original_id == req["request_id"]:
        _fail("recovery_request_invalid", "Recovery requires a new request_id")
    if original.get("actor") != req.get("actor") or original.get("session_id") != req.get("session_id"):
        _fail("ownership_conflict", "Only the original actor and session can recover this graph change", 3)
    if original.get("scope_id") != req.get("scope_id"):
        _fail("scope_conflict", "Recovery must use the same project scope", 3)
    original_payload = _payload(original)
    with closing(db.connect()) as conn:
        context = _claimed_context(db, conn, req, allow_missing=True)
        row = _intent_row(db, conn, original_id, context["scope_id"], req["actor"], req["session_id"])
        if row is None:
            _fail("graph_change_journal_not_found", "No recoverable graph-change journal was found", 3)
        intent_id = row["id"]
        intent = Phase3Storage(db).get_intent(intent_id, context["scope_id"], req["actor"], req["session_id"], conn=conn)
    if context["graph_path"].exists():
        with closing(db.connect()) as conn:
            context = _source_graph(db, conn, req)
    journal = intent["body"]
    if journal.get("request_fingerprint") != _request_fingerprint(original):
        _fail("request_conflict", "original_request does not match the journaled request", 3)
    for field in ("repository_id", "workspace", "relative_graph_path"):
        if str(original_payload.get(field)) != str(payload.get(field)):
            _fail("recovery_request_invalid", f"Recovery {field} must match original request", 3)
    if journal.get("target_key") != _target_key(context):
        _fail("recovery_target_mismatch", "Recovery workspace or graph path does not match the journal", 3)
    stage = journal.get("stage")
    if stage == "completed":
        original_result = db.get_request_result(original_id, req["actor"], req["session_id"])
        if original_result is None:
            _fail("graph_change_journal_conflict", "Completed journal has no matching request receipt", 4)
        original_envelope, original_exit = original_result
        if original_exit:
            return original_envelope, original_exit
        return db.run_request(req, lambda conn, request: {
            "original_request_id": original_id, "journal_id": intent_id,
            "journal_status": "completed", "change_result": original_envelope.get("result")})
    if stage not in {"prepared", "original_preserved", "target_detached", "file_written"}:
        _fail("graph_change_recovery_conflict", "Journal is not in a recoverable stage", 3,
              {"journal_id": intent_id, "journal_status": stage})
    expected = _expected_source(original_payload.get("expected_source"))
    change_set = original_payload.get("change_set")
    if fingerprint(change_set) != journal.get("change_set_hash"):
        _fail("request_conflict", "Recovery change_set does not match the original journal", 3)
    current_raw = context.get("raw_sha256")
    old_raw = journal.get("old_file_sha256")
    new_raw = journal.get("new_file_sha256")
    stage_path = _journal_file(context, journal.get("stage_path"))
    outcome = intent.get("outcome") or {}
    publication_refs = journal.get("publication_refs") or {}
    recovery_ref = outcome.get("recovery_ref") or publication_refs.get("recovery_ref")
    if not recovery_ref:
        _fail("graph_change_recovery_conflict", "Journal lacks the publication recovery reference", 3,
              {"journal_id": intent_id})
    recovery_path = _journal_file(context, recovery_ref)
    context["recovery_ref"] = recovery_ref
    prepared = None
    if current_raw == old_raw or current_raw is None:
        if stage == "file_written":
            _fail("graph_change_recovery_conflict", "Published file was reverted after the journal recorded it", 3,
                  {"journal_id": intent_id})
        if current_raw is not None:
            verify_source_pin(expected, context["source_pin"])
            base_graph = context["graph"]
        else:
            _verify_source_identity(context, expected)
            if not recovery_path.exists() or hashlib.sha256(recovery_path.read_bytes()).hexdigest() != old_raw:
                _fail("graph_change_recovery_conflict", "Target and verified retained original are unavailable", 3,
                      {"journal_id": intent_id, "recovery_ref": recovery_ref})
            base_graph = strict_json_loads(recovery_path.read_bytes(), max_bytes=8 * 1024 * 1024)
            base_report = validate_graph(base_graph, context["scope_id"], complete=False)
            if base_report["sha256"] != expected.graph_hash or base_report["graph_version"] != expected.graph_revision:
                _fail("graph_change_recovery_conflict", "Retained original does not match the reviewed SourcePin", 3,
                      {"journal_id": intent_id})
        prepared = prepare_change_set(base_graph, change_set, original_id)
        if prepared["change_set_hash"] != journal.get("change_set_hash"):
            _fail("request_conflict", "Recomputed change set does not match its journal", 3)
        if hashlib.sha256(prepared["wire"]).hexdigest() != new_raw:
            _fail("graph_change_recovery_conflict", "Recomputed candidate differs from the journaled hash", 3,
                  {"journal_id": intent_id})
        _verify_source_identity(context, expected)
        if current_raw is not None:
            _write_candidate(db, req, context, prepared, expected, original_id, intent_id=intent_id)
        else:
            stage_file = _stage_graph(context["graph_path"], prepared["wire"], original_id)
            if stage_file != stage_path:
                _fail("graph_change_journal_invalid", "Stage file path does not match the journal", 3)
            from .publication import guarded_publish
            publication = guarded_publish(target=context["graph_path"], stage=stage_file, expected_hash=old_raw,
                                          effect_id=original_id, owned_root=context["workspace"],
                                          checkpoint=lambda phase, details: _publish_checkpoint(
                                              db, req, intent_id, context["scope_id"], phase, details))
            if publication.get("status") == "conflict":
                _fail("publication_conflict", "Graph recovery retained conflicting versions", 3,
                      {key: publication.get(key) for key in ("phase", "recovery_ref", "original_hash", "candidate_hash")})
            context["recovery_ref"] = publication.get("recovery_ref")
            context["publish_receipt"] = publication
            _publish_checkpoint(db, req, intent_id, context["scope_id"], "candidate_published", publication)
        with closing(db.connect()) as conn:
            context = _source_graph(db, conn, req)
        context["recovery_ref"] = recovery_ref
        context["stage_path"] = stage_path
    elif current_raw == new_raw:
        _verify_source_identity(context, expected)
        if not recovery_path.exists() or hashlib.sha256(recovery_path.read_bytes()).hexdigest() != old_raw:
            _fail("graph_change_recovery_conflict", "Retained original changed after publication", 3,
                  {"journal_id": intent_id, "recovery_ref": recovery_ref})
        stage_file = _stage_graph(context["graph_path"], context["wire"], original_id)
        if stage_file != stage_path:
            _fail("graph_change_journal_invalid", "Stage file path does not match the journal", 3)
        from .publication import guarded_publish
        publication = guarded_publish(target=context["graph_path"], stage=stage_file, expected_hash=old_raw,
                                      effect_id=original_id, owned_root=context["workspace"],
                                      checkpoint=lambda phase, details: _publish_checkpoint(
                                          db, req, intent_id, context["scope_id"], phase, details))
        if publication.get("status") == "conflict":
            _fail("publication_conflict", "Graph recovery retained conflicting versions", 3,
                  {key: publication.get(key) for key in ("phase", "recovery_ref", "original_hash", "candidate_hash")})
        context["recovery_ref"] = publication.get("recovery_ref")
        context["publish_receipt"] = publication
        context["stage_path"] = stage_path
        _publish_checkpoint(db, req, intent_id, context["scope_id"], "candidate_published", publication)
    else:
        _fail("graph_change_recovery_conflict", "Target hash is neither the journaled old nor new graph", 3,
              {"journal_id": intent_id, "current_file_sha256": current_raw})
    if context["raw_sha256"] != new_raw or context["source_pin"].graph_hash != journal.get("new_graph_hash"):
        _fail("graph_change_recovery_conflict", "Current file does not match the journaled new graph", 3,
              {"journal_id": intent_id})
    if context["source_pin"].reviewed_commit != expected.reviewed_commit or context["source_pin"].selected_ref != expected.selected_ref:
        _fail("source_conflict", "Git HEAD or selected ref changed; both versions were retained", 3)
    context["stage_path"] = stage_path
    context = _confirm_publication(db, req, context, expected, original_id, intent_id,
                                  old_raw, new_raw, journal.get("new_graph_hash"))
    context["stage_path"] = stage_path
    _publish_checkpoint(db, req, intent_id, context["scope_id"], "candidate_published",
                        context.get("publish_receipt", {"recovery_ref": recovery_ref,
                                                         "candidate_hash": new_raw, "original_hash": old_raw}))
    if prepared is None:
        prepared = {"graph": context["graph"], "wire": context["wire"], "report": context["report"],
                    "operations": journal.get("operation_summary", []), "temp_id_map": journal.get("temp_id_map", {}),
                    "created_ids": journal.get("created_ids", []), "retired_ids": journal.get("retired_ids", []),
                    "inherited": journal.get("inherited", []), "source_hash": context["source_pin"].graph_hash,
                    "change_set_hash": journal["change_set_hash"], "change_id": journal["change_id"]}
    prepared["reason"] = change_set["reason"]
    prepared["evidence_refs"] = change_set.get("evidence_refs", [])
    context["recovery_ref"] = recovery_ref
    original_result, original_exit = _commit_change(db, original, context, intent_id,
                                                    context["graph"], context["source_pin"], expected, prepared)
    if original_exit:
        return original_result, original_exit
    recovery_result = db.run_request(req, lambda conn, request: {
        "original_request_id": original_id, "journal_id": intent_id,
        "journal_status": "completed", "change_result": original_result.get("result")})
    _cleanup_candidate_stage(context, new_raw)
    return recovery_result


def execute_file(db, req):
    operation = req.get("operation")
    try:
        if operation == "apply_graph_change":
            return _execute_apply(db, req)
        if operation == "recover_graph_change":
            return _recover_graph_change(db, req)
        if operation == "rebuild_graph_index":
            return _rebuild_graph_index(db, req)
        if operation == "register_segment_manifest":
            return _register_segment_manifest(db, req)
        _fail("operation_unsupported", "Unsupported graph file operation")
    except PmtError as error:
        if operation in {"apply_graph_change", "recover_graph_change"}:
            expected = (_payload(req).get("expected_source") or {})
            db.diagnostics.emit("planning.graph_change_rejected", level=40, request_id=req.get("request_id"),
                                operation=operation, scope_id=req.get("scope_id"),
                                source_hash=expected.get("source_hash"), error_code=error.code,
                                reason_code=error.code, transaction_outcome="rollback")
        return db._response(req.get("request_id"), False, None, error.as_dict(), []), error.exit_code
    except OSError as error:
        issue = PmtError("graph_source_io_error", "Graph source operation could not be completed", 4, True)
        return db._response(req.get("request_id"), False, None, issue.as_dict(), []), issue.exit_code
