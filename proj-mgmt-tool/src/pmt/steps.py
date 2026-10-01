"""Versioned internal Step directives and explicit, evidence-based review."""
from __future__ import annotations

import json
from pathlib import Path

from .errors import PmtError
from .phase2_common import (event, identifier, load_json_resource, normalized_workspace,
                           persist_json_resource, replay, require_workspace_claim, project_scope_id)
from .resources import check_artifact
from .util import canonical_json, new_id, utc_now

READ_OPERATIONS = {"read_step", "read_step_directive"}
WRITE_OPERATIONS = {"review_step", "set_task_metadata", "invalidate_plan_branch", "cancel_step"}
FILE_OPERATIONS = {"save_step_directive"}
TAG_KINDS = {
    "work": {"feature", "bugfix", "improvement", "ops", "research"},
    "item": {"functional", "quality", "constraint", "compatibility"},
    "step": {"investigate", "experiment", "implement", "test", "review", "docs", "integrate"},
}


def _step(conn, step_id):
    identifier(step_id, "step_id")
    row = conn.execute("SELECT * FROM records WHERE id=? AND kind='step'", (step_id,)).fetchone()
    spec = conn.execute("SELECT * FROM step_specs WHERE step_id=?", (step_id,)).fetchone()
    if not row or not spec:
        raise PmtError("step_not_found", "Step and directive metadata must exist")
    return dict(row), dict(spec)


def _criteria(value):
    if not isinstance(value, list) or not value:
        raise PmtError("invalid_criteria", "Step requires explicit completion criteria")
    ids = []
    for item in value:
        name = item if isinstance(item, str) else item.get("id") if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip() or name in ids:
            raise PmtError("invalid_criteria", "Criterion IDs must be nonempty and unique")
        ids.append(name)
    return value


def _validate_directive(directive):
    if not isinstance(directive, dict):
        raise PmtError("invalid_directive", "Directive must be structured")
    for key in ("purpose", "goal", "method"):
        if not isinstance(directive.get(key), (str, dict)) or not directive[key]:
            raise PmtError("invalid_directive", f"Directive requires {key}")
    for key in ("non_goal", "inputs", "outputs", "tests", "logging", "context_refs"):
        if not isinstance(directive.get(key), list):
            raise PmtError("invalid_directive", f"Directive requires an array for {key}")
    if not directive["tests"] or not directive["logging"]:
        raise PmtError("invalid_directive", "Actual test and logging methods are required")
    for key in ("inputs", "outputs"):
        for field in directive[key]:
            if not isinstance(field, dict) or not field.get("meaning") or not field.get("name"):
                raise PmtError("invalid_directive", "Input/output fields need names and meanings")
    boundaries = directive.get("change_scope")
    if not isinstance(boundaries, dict) or any(not isinstance(boundaries.get(key), list)
                                              for key in ("add", "modify", "delete", "forbidden")):
        raise PmtError("invalid_directive", "Functional change boundaries are required")
    canonical_json(directive)


def _save_preflight(db, conn, req):
    p = req["payload"]
    item_id = identifier(p.get("item_id"), "item_id")
    item = conn.execute("SELECT * FROM records WHERE id=? AND kind='item'", (item_id,)).fetchone()
    if not item or item["state"] in {"Done", "Canceled"}:
        raise PmtError("item_not_active", "An active parent Item is required")
    owner = conn.execute("SELECT owner_session FROM claims WHERE record_id=?", (item_id,)).fetchone()
    if owner and owner[0] != req["session_id"]:
        raise PmtError("ownership_conflict", "Parent Item is owned by another session", 3)
    _validate_directive(p.get("directive"))
    _criteria(p.get("criteria"))
    for version in ("requirements_version", "plan_version"):
        if not isinstance(p.get(version), str) or not p[version]:
            raise PmtError("invalid_directive", f"{version} is required")
    workspace = normalized_workspace(p.get("workspace"))
    kind = p.get("kind", "implement")
    if kind not in TAG_KINDS["step"]:
        raise PmtError("invalid_step_kind", "Unsupported execution kind")
    if p.get("product_stage") not in {"prototype", "expansion", "production"}:
        raise PmtError("invalid_product_stage", "Explicit product stage is required")
    if not isinstance(p.get("scopes"), list) or not p["scopes"]:
        raise PmtError("invalid_scopes", "Declared execution scopes are required")
    if kind in {"investigate", "experiment"} and p.get("exploration_approved") is True:
        pass
    else:
        plan = conn.execute("SELECT * FROM plans WHERE id=?", (p.get("plan_id"),)).fetchone()
        if not plan or plan["state"] != "published" or plan["scope_id"] != project_scope_id(conn, item["scope_id"]):
            raise PmtError("plan_not_confirmed", "Implementation requires a published matching plan")
        if plan["requirements_version"] != p["requirements_version"] or plan["plan_version"] != p["plan_version"]:
            raise PmtError("plan_version_conflict", "Directive does not match current plan", 3)
    dependencies = p.get("dependencies", [])
    if not isinstance(dependencies, list) or len(set(dependencies)) != len(dependencies):
        raise PmtError("invalid_dependencies", "Dependencies must be distinct Step IDs")
    for dependency in dependencies:
        identifier(dependency, "dependency")
        dep = conn.execute("SELECT scope_id,kind FROM records WHERE id=?", (dependency,)).fetchone()
        if not dep or dep["kind"] != "step" or project_scope_id(conn, dep["scope_id"]) != project_scope_id(conn, item["scope_id"]):
            raise PmtError("invalid_dependencies", "Dependencies must belong to this project scope")
    step_id = req.get("record_id")
    version = 1
    revision = 1
    if step_id:
        record, spec = _step(conn, step_id)
        if record["parent_id"] != item_id or req.get("expected_revision") != record["revision"]:
            raise PmtError("revision_conflict", "Step parent or revision changed", 3)
        if record["state"] in {"Done", "Canceled"}:
            raise PmtError("step_not_active", "Closed Step cannot receive a new directive")
        active = conn.execute("SELECT id FROM execution_runs WHERE step_id=? AND state IN "
                              "('queued','starting','running','review_pending','cancel_requested','reconciling')",
                              (step_id,)).fetchone()
        if active:
            raise PmtError("step_execution_active", "Stop or remove active execution before changing its directive", 3)
        version, revision = spec["directive_version"] + 1, record["revision"] + 1
    if step_id in dependencies:
        raise PmtError("dependency_cycle", "A Step cannot depend on itself")
    if step_id:
        adjacency = {r["step_id"]: json.loads(r["dependencies_json"]) for r in conn.execute("SELECT step_id,dependencies_json FROM step_specs")}
        adjacency[step_id] = dependencies
        def visit(node, path):
            if node in path:
                raise PmtError("dependency_cycle", "Step dependency graph must be acyclic")
            for child in adjacency.get(node, []):
                visit(child, path | {node})
        visit(step_id, set())
    return dict(item), workspace, version, revision


def execute_file(db, req):
    prior = replay(db, req)
    if prior:
        return prior
    with db.connect() as conn:
        item, workspace, version, revision = _save_preflight(db, conn, req)
    step_id = req.get("record_id") or new_id()
    resource = persist_json_resource(db, req, req["payload"]["directive"], item["scope_id"], "step_directive", step_id)
    def store(conn, request):
        current_item, current_ws, current_version, current_revision = _save_preflight(db, conn, request)
        p = request["payload"]
        if (current_ws, current_version, current_revision) != (workspace, version, revision):
            raise PmtError("revision_conflict", "Directive changed during publication", 3)
        now = utc_now()
        body = {"directive_id": resource["artifact_id"], "directive_version": version,
                "criteria": p["criteria"], "workspace": workspace, "kind_tag": p.get("kind", "implement"),
                "product_stage": p["product_stage"], "invalidated": False}
        title = p.get("title") or p["directive"]["goal"]
        if not isinstance(title, str) or not title.strip():
            raise PmtError("invalid_title", "Step title must be nonempty text")
        if version == 1:
            conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
                         "VALUES(?,'step',?,?,?,'Planned',?,?,?,?)", (step_id, item["scope_id"], item["id"], title,
                                                                    canonical_json(body), revision, now, now))
        else:
            conn.execute("UPDATE records SET title=?,body_json=?,revision=?,updated_at=? WHERE id=?",
                         (title, canonical_json(body), revision, now, step_id))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,"
                     "product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(step_id) DO UPDATE SET directive_id=excluded.directive_id,"
                     "directive_version=excluded.directive_version,requirements_version=excluded.requirements_version,"
                     "plan_version=excluded.plan_version,plan_id=excluded.plan_id,role=excluded.role,product_stage=excluded.product_stage,"
                     "workspace=excluded.workspace,scopes_json=excluded.scopes_json,criteria_json=excluded.criteria_json,"
                     "dependencies_json=excluded.dependencies_json,updated_at=excluded.updated_at",
                     (step_id, resource["artifact_id"], version, p["requirements_version"], p["plan_version"], p.get("plan_id"),
                      p.get("role", "lower"), p["product_stage"], workspace, canonical_json(p["scopes"]),
                      canonical_json(p["criteria"]), canonical_json(p.get("dependencies", [])), now, now))
        event(conn, req, "planning.step_instruction_versioned", item["scope_id"], step_id,
              {"directive_version": version, "directive_id": resource["artifact_id"]})
        return {"step_id": step_id, "record_id": step_id, "revision": revision,
                "directive_version": version, "directive_id": resource["artifact_id"], "sha256": resource["sha256"]}
    return db.run_request(req, store)


def handle(db, conn, req):
    p, op = req["payload"], req["operation"]
    if op == "set_task_metadata":
        record_id = req.get("record_id") or p.get("record_id")
        identifier(record_id)
        row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row or row["kind"] not in TAG_KINDS:
            raise PmtError("invalid_task", "Metadata requires Work, Item or Step")
        if req.get("expected_revision") != row["revision"]:
            raise PmtError("revision_conflict", "Task revision changed", 3)
        if "kind_tag" in p and p["kind_tag"] not in TAG_KINDS[row["kind"]]:
            raise PmtError("invalid_tag", "Kind tag does not match this hierarchy level")
        if "classification_id" in p:
            classification = conn.execute("SELECT kind FROM scopes WHERE id=?", (identifier(p["classification_id"], "classification_id"),)).fetchone()
            if (not classification or classification["kind"] != "classification" or
                    project_scope_id(conn, p["classification_id"]) != project_scope_id(conn, row["scope_id"])):
                raise PmtError("classification_scope_conflict", "Classification must belong to this project")
        if "tags" in p and (not isinstance(p["tags"], list) or len(p["tags"]) > 50 or
                            any(not isinstance(tag, str) or not tag or len(tag) > 50 for tag in p["tags"])):
            raise PmtError("invalid_tag", "Tags must be bounded text labels")
        if "priority" in p and (type(p["priority"]) is not int or not 0 <= p["priority"] <= 100):
            raise PmtError("invalid_priority", "Priority must be an integer from 0 to 100")
        body = json.loads(row["body_json"])
        for key in ("kind_tag", "tags", "priority", "priority_reason", "classification_id"):
            if key in p:
                body[key] = p[key]
        if "priority" in p and not p.get("priority_reason"):
            raise PmtError("priority_reason_required", "Priority override requires a reason")
        conn.execute("UPDATE records SET body_json=?,revision=revision+1,updated_at=? WHERE id=?",
                     (canonical_json(body), utc_now(), record_id))
        event(conn, req, "planning.classification_assigned", row["scope_id"], record_id)
        return {"record_id": record_id, "revision": row["revision"] + 1, "metadata": body}
    if op == "invalidate_plan_branch":
        if req["actor"] != "main" or not p.get("reason") or not isinstance(p.get("step_ids"), list):
            raise PmtError("invalidation_requires_main", "Main must name impacted Steps and the reason")
        invalidated = []
        for step_id in p["step_ids"]:
            row, spec = _step(conn, step_id)
            if p.get("plan_id") and spec["plan_id"] != p["plan_id"]:
                raise PmtError("impact_scope_conflict", "Step is outside the affected plan")
            body = json.loads(row["body_json"])
            body.update(invalidated=True, invalidation_reason=p["reason"])
            conn.execute("UPDATE records SET body_json=?,revision=revision+1,updated_at=? WHERE id=?",
                         (canonical_json(body), utc_now(), step_id))
            for run in conn.execute("SELECT id,state,job_id FROM execution_runs WHERE step_id=? AND state IN "
                                    "('queued','starting','running','review_pending','reconciling','cancel_requested')", (step_id,)):
                state = "canceled" if run["state"] == "queued" else "cancel_requested"
                conn.execute("UPDATE execution_runs SET state=?,revision=revision+1,updated_at=? WHERE id=?", (state, utc_now(), run["id"]))
                conn.execute("UPDATE execution_jobs SET state=?,updated_at=? WHERE id=?", (state, utc_now(), run["job_id"]))
            invalidated.append(step_id)
            event(conn, req, "reconciliation.premise_impact_detected", row["scope_id"], step_id,
                  {"plan_id": spec["plan_id"], "reason": p["reason"]})
        return {"invalidated_step_ids": invalidated, "reason": p["reason"]}
    row, spec = _step(conn, req.get("record_id") or p.get("step_id"))
    if op == "cancel_step":
        if req["actor"] != "main" or req.get("expected_revision") != row["revision"] or not p.get("reason"):
            raise PmtError("cancel_requires_main", "Main, current revision and cancellation reason are required", 3)
        if row["state"] == "Done":
            raise PmtError("step_not_active", "A completed Step cannot be canceled")
        active = conn.execute("SELECT 1 FROM execution_runs WHERE step_id=? AND state IN "
                              "('queued','starting','running','review_pending','cancel_requested','reconciling')", (row["id"],)).fetchone()
        locked = conn.execute("SELECT 1 FROM scope_locks l JOIN execution_runs r ON r.id=l.run_id WHERE r.step_id=?", (row["id"],)).fetchone()
        if active or locked:
            raise PmtError("stop_confirmation_required", "Confirm every execution has ended before canceling the Step", 3)
        conn.execute("UPDATE records SET state='Canceled',revision=revision+1,updated_at=? WHERE id=?", (utc_now(), row["id"]))
        event(conn, req, "planning.step_canceled", row["scope_id"], row["id"], {"reason": p["reason"]})
        return {"step_id": row["id"], "state": "Canceled", "revision": row["revision"] + 1}
    if op == "read_step":
        body = json.loads(row["body_json"])
        parent_id, visited = row["parent_id"], set()
        while "priority" not in body and parent_id and parent_id not in visited:
            visited.add(parent_id)
            parent = conn.execute("SELECT body_json,parent_id FROM records WHERE id=?", (parent_id,)).fetchone()
            if not parent:
                break
            inherited = json.loads(parent[0]).get("priority")
            if inherited is not None:
                body["priority"] = inherited
            parent_id = parent[1]
        return {"step_id": row["id"], "parent_id": row["parent_id"], "state": row["state"],
                "revision": row["revision"], "metadata": body, "directive_version": spec["directive_version"]}
    if op == "read_step_directive":
        run = require_workspace_claim(db, conn, req, spec["workspace"], [])
        if run["step_id"] != row["id"] or run["directive_version"] != spec["directive_version"]:
            raise PmtError("directive_version_conflict", "Run does not own this directive version", 3)
        if json.loads(row["body_json"]).get("invalidated"):
            raise PmtError("plan_invalidated", "Directive is no longer approved", 3)
        return {"step_id": row["id"], "directive_version": spec["directive_version"],
                "directive": load_json_resource(db, conn, spec["directive_id"])}
    if op == "review_step":
        if req["actor"] != "main" or req.get("expected_revision") != row["revision"]:
            raise PmtError("review_requires_main", "Main and current task revision are required", 3)
        if json.loads(row["body_json"]).get("invalidated"):
            raise PmtError("plan_invalidated", "Old directive cannot complete current requirements", 3)
        run = conn.execute("SELECT * FROM execution_runs WHERE id=? AND step_id=?", (p.get("run_id"), row["id"])).fetchone()
        if not run or run["state"] not in {"review_pending", "succeeded"} or run["directive_version"] != spec["directive_version"]:
            raise PmtError("execution_not_reviewable", "Current execution results are required", 3)
        from .verification import verify_completion
        checked = verify_completion(db, conn, row, p.get("verification_ids", []))
        if not checked["valid"] or not checked["evidence_ids"]:
            raise PmtError("completion_criteria_unmet", "Current passing verification and evidence are required", 2,
                           details={"reasons": checked["reasons"]})
        if not p.get("integration_confirmed") or not run["stop_confirmed"]:
            raise PmtError("integration_confirmation_required", "Execution termination and integration review are required")
        conn.execute("UPDATE records SET state='Done',revision=revision+1,updated_at=? WHERE id=?", (utc_now(), row["id"]))
        conn.execute("UPDATE execution_runs SET state='succeeded',revision=revision+1,updated_at=? WHERE id=?", (utc_now(), run["id"]))
        conn.execute("UPDATE execution_jobs SET state='succeeded',updated_at=? WHERE id=?", (utc_now(), run["job_id"]))
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (run["id"],))
        event(conn, req, "planning.result_reviewed", row["scope_id"], row["id"],
              {"run_id": run["id"], "verification_ids": p["verification_ids"]})
        return {"step_id": row["id"], "state": "Done", "revision": row["revision"] + 1,
                "item_completed": False, "work_completed": False}
    raise PmtError("operation_unsupported", "Unknown Step operation")
