"""Shared validation and existing authority adapters for continuity services."""
from __future__ import annotations

import hashlib
import re

from ..errors import PmtError
from ..phase2_common import identifier, project_scope_id, require_workspace_claim, validate_scope
from ..util import canonical_json

CONTRACT_VERSION = "phase4-1"
KINDS = frozenset({"basis", "facts", "checkpoint", "session_link", "change", "link_index",
                   "assessment", "resolution", "alignment", "applicability", "overview",
                   "bundle", "detail", "measurement"})
PRIVATE_KINDS = frozenset({"bundle", "detail"})
WORK_OPERATIONS = frozenset({"capture_work_basis", "collect_changes", "build_implementation_links",
                            "assess_alignment", "apply_alignment", "read_applicability",
                            "compose_task_resume", "validate_basis", "read_resume_detail"})
FORBIDDEN = frozenset({"transcript", "raw_transcript", "prompt", "native_prompt", "directive_body",
                      "api_key", "password", "credential", "credentials", "authorization",
                      "access_token", "claim_token", "token", "env", "environment_variables",
                      "argv", "pid", "absolute_path", "command"})


def digest(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def validate_metadata(value):
    """Reject raw execution fields and absolute client paths at shared storage."""
    if not isinstance(value, dict):
        raise PmtError("invalid_continuity_body", "Continuity body must be an object")

    def visit(node, depth=0):
        if depth > 32:
            raise PmtError("continuity_depth_exceeded", "Metadata nesting exceeds the limit")
        if isinstance(node, dict):
            for key, item in node.items():
                if not isinstance(key, str) or key.lower() in FORBIDDEN:
                    raise PmtError("private_metadata_forbidden", "Raw private execution fields are not shared metadata")
                visit(item, depth + 1)
        elif isinstance(node, list):
            if len(node) > 10000:
                raise PmtError("continuity_too_large", "Metadata collection exceeds the limit")
            for item in node:
                visit(item, depth + 1)
        elif isinstance(node, str):
            if re.match(r"^(?:[A-Za-z]:[\\/]|\\\\|/|file:|vscode:)", node, re.IGNORECASE):
                raise PmtError("private_metadata_forbidden", "Absolute client paths are not shared metadata")
        elif node is not None and type(node) not in {bool, int, float}:
            raise PmtError("invalid_continuity_body", "Metadata must contain JSON values")
    visit(value)
    wire = canonical_json(value).encode("utf-8")
    if len(wire) > 512 * 1024:
        raise PmtError("continuity_too_large", "Continuity metadata exceeds 512 KiB")
    return value


def authorize(db, conn, req):
    """Local OS access plus explicit project ancestry; Host adds current grants."""
    scope_id = req.get("scope_id")
    validate_scope(db, conn, scope_id)
    project_id = project_scope_id(conn, scope_id)
    if scope_id != project_id:
        raise PmtError("project_scope_required", "Continuity requests require an explicit project scope")
    record_id = req.get("record_id") or req.get("payload", {}).get("task_id")
    if record_id is not None:
        identifier(record_id, "task_id")
        row = conn.execute("SELECT scope_id FROM records WHERE id=?", (record_id,)).fetchone()
        if not row or project_scope_id(conn, row[0]) != project_id:
            raise PmtError("scope_mismatch", "Selected task does not belong to this project", 3)
    for field in ("actor", "session_id"):
        if not isinstance(req.get(field), str) or not req[field].strip():
            raise PmtError("invalid_identity", "Current actor and session are required")
    if req.get("operation") in WORK_OPERATIONS:
        payload = req.get("payload", {})
        run_id = identifier(payload.get("run_id"), "run_id")
        row = conn.execute("SELECT r.owner_session,r.state,s.scope_id FROM execution_runs r "
                           "JOIN records s ON s.id=r.step_id WHERE r.id=?", (run_id,)).fetchone()
        if not row or row["owner_session"] != req["session_id"] or row["state"] not in {
                "starting", "running", "review_pending", "reconciling", "cancel_requested"}:
            raise PmtError("ownership_conflict", "An actual current run owned by this session is required", 3)
        if project_scope_id(conn, row["scope_id"]) != project_id:
            raise PmtError("scope_mismatch", "Run does not belong to this project", 3)
    return {"project_id": project_id, "environment_id": db.environment_id}


def work_access(db, conn, req, paths=(".",), workspace=None):
    authorize(db, conn, req)
    run_id = identifier(req.get("payload", {}).get("run_id"), "run_id")
    row = conn.execute("SELECT workspace FROM execution_runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise PmtError("run_not_found", "Execution run is unavailable", 3)
    return require_workspace_claim(db, conn, req, workspace or row["workspace"], paths)


def budget(payload):
    configured = payload.get("budget", {})
    if not isinstance(configured, dict) or set(configured) - {"max_bytes", "max_lines", "unit"}:
        raise PmtError("invalid_budget", "Budget must contain max_bytes and max_lines")
    if configured.get("unit", "utf8") != "utf8":
        raise PmtError("invalid_budget", "Only UTF-8 byte and line budgets are supported")
    max_bytes = configured.get("max_bytes", 16384)
    max_lines = configured.get("max_lines", 160)
    if type(max_bytes) is not int or not 256 <= max_bytes <= 262144:
        raise PmtError("invalid_budget", "Byte budget is outside the supported range")
    if type(max_lines) is not int or not 8 <= max_lines <= 2000:
        raise PmtError("invalid_budget", "Line budget is outside the supported range")
    return {"max_bytes": max_bytes, "max_lines": max_lines}


def bounded_result(req, result):
    """Mark mandatory overflow without silently dropping its contents."""
    from ..service import response
    limits = budget(req.get("payload", {}))
    result = dict(result)
    result.setdefault("complete", result.get("incomplete") is not True)
    result["budget"] = limits
    for _ in range(3):
        wire = canonical_json(response(req["request_id"], result=result)).encode("utf-8")
        result["response_bytes"] = len(wire)
        result["response_lines"] = wire.count(b"\n") + 1
    if result["response_bytes"] > limits["max_bytes"] or result["response_lines"] > limits["max_lines"]:
        original = result
        result = {"complete": False, "attention": "mandatory_budget_exceeded"}
        # Preserve actual producer refs only; a caller hint is not a retained detail.
        for field in ("detail_ref", "bundle_ref", "overview_ref", "checkpoint_ref", "basis_ref", "source_refs"):
            ref = original.get(field)
            if not ref:
                continue
            validate_metadata({field: ref})
            candidate = result | {field: ref}
            if len(canonical_json(response(req["request_id"], result=candidate)).encode("utf-8")) <= limits["max_bytes"]:
                result = candidate
        lookup = original.get("required_lookup")
        if lookup in {"read_current_facts", "compose_resume_overview", "read_resume_detail"}:
            candidate = result | {"required_lookup": lookup}
            if len(canonical_json(response(req["request_id"], result=candidate)).encode("utf-8")) <= limits["max_bytes"]:
                result = candidate
    return result
