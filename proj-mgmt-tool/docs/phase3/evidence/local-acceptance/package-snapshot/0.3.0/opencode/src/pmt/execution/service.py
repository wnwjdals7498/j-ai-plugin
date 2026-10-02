"""Transactional execution queue, scope locks, receipts and recovery."""
from __future__ import annotations

import json
import os
import hashlib
from pathlib import Path, PurePosixPath

from ..errors import PmtError
from ..phase2_common import event, identifier, normalized_workspace, validate_scope, project_scope_id
from ..phase2_schema import ACTIVE_RUN_STATES
from ..util import canonical_json, fingerprint, new_id, utc_now

READ_OPERATIONS = frozenset({"read_execution", "list_execution_queue"})
WRITE_OPERATIONS = frozenset({"enqueue_execution", "prepare_execution", "attach_execution_handle",
    "observe_execution", "submit_execution_result", "request_execution_cancel", "reconcile_execution",
    "review_execution", "retry_execution", "extend_execution_scopes"})
FILE_OPERATIONS = frozenset()
_CANCEL_TERMINAL = {"canceled", "failed", "blocked", "succeeded"}


def _bad(code, message):
    raise PmtError(code, message)


def _json(text, label, expected):
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PmtError("stored_record_invalid", f"Stored {label} is invalid", 5) from exc
    if not isinstance(value, expected):
        raise PmtError("stored_record_invalid", f"Stored {label} has an invalid shape", 5)
    return value


def _payload(req):
    value = req.get("payload", {})
    if not isinstance(value, dict):
        _bad("invalid_payload", "payload must be an object")
    return value


def _step(conn, step_id, *, require_directive=True):
    identifier(step_id, "step_id")
    row = conn.execute("SELECT r.*,ss.directive_id,ss.directive_version,ss.requirements_version,ss.plan_version,ss.plan_id,ss.role,ss.product_stage,ss.workspace,ss.scopes_json,ss.criteria_json,ss.dependencies_json FROM records r JOIN step_specs ss ON ss.step_id=r.id WHERE r.id=? AND r.kind='step'", (step_id,)).fetchone()
    if row is None:
        raise PmtError("step_not_found", "A configured Step record is required")
    validate_scope(None, conn, row["scope_id"])
    if require_directive and conn.execute("SELECT state FROM artifacts WHERE id=? AND state='ready'", (row["directive_id"],)).fetchone() is None:
        raise PmtError("directive_unavailable", "The Step directive reference is unavailable", 3)
    return dict(row)


def _normalize_scopes(values, workspace):
    if not isinstance(values, list) or not values:
        raise PmtError("invalid_scope_set", "At least one declared scope is required")
    root = normalized_workspace(workspace)
    out, seen = [], set()
    for item in values:
        if not isinstance(item, dict) or item.get("kind") not in {"path", "resource", "workspace"}:
            raise PmtError("invalid_scope", "Scope requires kind path, resource, or workspace")
        item_root = normalized_workspace(item.get("workspace"))
        if item_root != root:
            raise PmtError("invalid_scope", "Scope workspace must match the Step workspace")
        kind, resource = item["kind"], item.get("resource")
        if not isinstance(resource, str) or not resource.strip():
            raise PmtError("invalid_scope", "Scope resource must be nonempty")
        if kind == "workspace":
            resource = "."
        elif kind == "path":
            p = PurePosixPath(resource.replace("\\", "/"))
            if p.is_absolute() or Path(resource).drive or any(part in {"..", ""} for part in p.parts):
                raise PmtError("invalid_scope_path", "Scope path must be relative and cannot escape workspace")
            resource = p.as_posix().strip("/")
            if os.name == "nt":
                resource = resource.casefold()
        else:
            resource = resource.strip().casefold()
        key = _scope_key(kind, item_root, resource)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "workspace": item_root, "resource": resource, "lock_key": key})
    return sorted(out, key=lambda s: s["lock_key"])


def _scope_key(kind, workspace, resource):
    key = ("resource", "", resource.casefold()) if kind == "resource" else (kind, workspace.casefold(), resource.casefold())
    return "scope:" + hashlib.sha256(canonical_json(key).encode("utf-8")).hexdigest()


def _overlap(a, b):
    if a["kind"] == "resource" or b["kind"] == "resource":
        return a["kind"] == b["kind"] == "resource" and a["resource"].casefold() == b["resource"].casefold()
    if a["workspace"].casefold() != b["workspace"].casefold():
        return False
    if a["kind"] == "workspace" or b["kind"] == "workspace":
        return True
    x = a["resource"].replace("\\", "/").casefold()
    y = b["resource"].replace("\\", "/").casefold()
    if x in {"", "."} or y in {"", "."}:
        return True
    return x == y or x.startswith(y + "/") or y.startswith(x + "/")


def _normalize_route(route):
    route = dict(route)
    mode = route.get("mode")
    aliases = {"native": "subagent", "sdk": "cli"}
    route["adapter_kind"] = route.get("adapter_kind") or mode
    mode = aliases.get(mode, mode)
    if mode not in {"auto", "subagent", "cli", "api"}:
        raise PmtError("invalid_route", "route.mode must be auto, subagent, cli, or api")
    route["mode"] = mode
    return route


def _scope_rows(conn, run_id):
    return [dict(row) for row in conn.execute("SELECT lock_key,kind,workspace,resource FROM scope_locks WHERE run_id=?", (run_id,))]


def _acquire_scopes(conn, run_id, owner, scopes):
    existing = conn.execute("SELECT run_id,lock_key,kind,workspace,resource FROM scope_locks").fetchall()
    conflicts = []
    for scope in scopes:
        for row in existing:
            if row["run_id"] != run_id and _overlap(scope, dict(row)):
                conflicts.append({"scope_ref": scope["lock_key"], "owner_run_id": row["run_id"]})
    if conflicts:
        return conflicts
    now = utc_now()
    for scope in scopes:
        conn.execute("INSERT OR IGNORE INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (scope["lock_key"], run_id, owner, scope["kind"], scope["workspace"], scope["resource"], now))
    return []


def _get_run(conn, run_id):
    identifier(run_id, "run_id")
    row = conn.execute("SELECT * FROM execution_runs WHERE id=?", (run_id,)).fetchone()
    if row is None:
        raise PmtError("run_not_found", "Execution run does not exist")
    return dict(row)


def _validate_current_step(conn, run, step):
    body = _json(step["body_json"], "Step body", dict)
    if body.get("invalidated") is True:
        raise PmtError("step_invalidated", "This Step was invalidated by a plan change", 3)
    if body.get("directive_id") not in (None, step["directive_id"]):
        raise PmtError("directive_reference_conflict", "Step directive reference changed", 3)
    if body.get("directive_version") not in (None, step["directive_version"]) or step["directive_version"] != run["directive_version"]:
        raise PmtError("directive_version_conflict", "Step directive version changed", 3)
    intent = _json(run["intent_json"], "run intent", dict)
    if step["plan_version"] != intent.get("plan_version") or step["requirements_version"] != intent.get("requirements_version"):
        raise PmtError("plan_version_conflict", "Step requirements or plan version changed", 3)
    tag = body.get("kind_tag")
    if tag in {"implement", "implementation"}:
        if not step["plan_id"]:
            raise PmtError("published_plan_required", "Implementation Step requires a published plan", 3)
        plan = conn.execute("SELECT state,plan_version,requirements_version FROM plans WHERE id=? AND scope_id=?",
                            (step["plan_id"], project_scope_id(conn, step["scope_id"]))).fetchone()
        if not plan or plan["state"] != "published" or plan["plan_version"] != step["plan_version"] or plan["requirements_version"] != step["requirements_version"]:
            raise PmtError("published_plan_required", "Implementation Step plan is not current and published", 3)


def _owned(req, run):
    if req.get("session_id") != run["owner_session"]:
        raise PmtError("ownership_conflict", "Execution run belongs to another session", 3)


def _cas(run, payload):
    expected = payload.get("expected_run_revision")
    if type(expected) is not int or expected != run["revision"]:
        raise PmtError("revision_conflict", "Run revision does not match", 3,
                       details={"expected_revision": expected, "current_revision": run["revision"]})


def _transition(conn, req, run, state, *, handle=None, result=None, stop_confirmed=None,
                started_at=None, completed_at=None, intent=None, keep_locks=True):
    now = utc_now()
    old = run["state"]
    revision = run["revision"] + 1
    conn.execute("UPDATE execution_runs SET state=?,revision=?,handle_json=COALESCE(?,handle_json),result_json=COALESCE(?,result_json),stop_confirmed=COALESCE(?,stop_confirmed),started_at=COALESCE(?,started_at),completed_at=COALESCE(?,completed_at),intent_json=COALESCE(?,intent_json),updated_at=? WHERE id=? AND revision=?",
                 (state, revision, canonical_json(handle) if handle is not None else None,
                  canonical_json(result) if result is not None else None, stop_confirmed, started_at,
                  completed_at, canonical_json(intent) if intent is not None else None, now,
                  run["id"], run["revision"]))
    if conn.execute("SELECT changes()").fetchone()[0] != 1:
        raise PmtError("revision_conflict", "Run changed concurrently", 3, True)
    if not keep_locks:
        from ..efficiency.batch import binding_for_run
        group = binding_for_run(conn, run["id"])
        if group and group["body"].get("status") not in {"complete", "canceled", "aborted"}:
            # A representative P2 run cannot release a union still protecting children.
            keep_locks = True
    if not keep_locks:
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (run["id"],))
    event(conn, req, "execution.transitioned", record_id=run["step_id"],
          payload={"job_id": run["job_id"], "run_id": run["id"], "attempt": run["attempt"],
                   "from": old, "to": state, "revision": revision})
    return revision


def _enqueue(conn, req, p):
    step = _step(conn, p.get("step_id"))
    if step["state"] in {"Done", "Canceled"}:
        raise PmtError("step_not_active", "Closed Step cannot be queued", 3)
    body = _json(step["body_json"], "Step body", dict)
    if body.get("invalidated") is True:
        raise PmtError("step_invalidated", "An invalidated Step cannot be queued", 3)
    if conn.execute("SELECT 1 FROM execution_jobs WHERE step_id=? AND state IN ('queued','starting','running','review_pending','reconciling','cancel_requested')", (step["id"],)).fetchone():
        raise PmtError("step_execution_active", "Step already has an active execution", 3)
    scope = _normalize_scopes(_json(step["scopes_json"], "Step scopes", list), step["workspace"])
    route = p.get("route")
    if not isinstance(route, dict):
        raise PmtError("invalid_route", "A selected route is required")
    for key in ("agent", "provider", "model", "mode", "selection_reason"):
        if not isinstance(route.get(key), str) or not route[key].strip():
            raise PmtError("invalid_route", f"route.{key} is required")
    route = _normalize_route(route)
    if type(route.get("max_concurrency")) is not int or route["max_concurrency"] < 1:
        raise PmtError("invalid_route", "route.max_concurrency must be positive")
    policy = p.get("policy", {})
    if not isinstance(policy, dict):
        raise PmtError("invalid_policy", "policy must be an object")
    retries = policy.get("max_retries", 2)
    if type(retries) is not int or retries < 0 or retries > 2:
        raise PmtError("invalid_policy", "max_retries must be between 0 and 2")
    dependencies = _json(step["dependencies_json"], "Step dependencies", list)
    policy_dependencies = policy.get("dependencies", [])
    if not isinstance(policy_dependencies, list):
        raise PmtError("invalid_policy", "policy.dependencies must be a list")
    for dep in policy_dependencies:
        identifier(dep, "dependency_step_id")
        if dep not in dependencies:
            dependencies.append(dep)
    for dep in dependencies:
        identifier(dep, "dependency_step_id")
        row = conn.execute("SELECT state,scope_id FROM records WHERE id=? AND kind='step'", (dep,)).fetchone()
        if row is None or project_scope_id(conn, row["scope_id"]) != project_scope_id(conn, step["scope_id"]):
            raise PmtError("invalid_dependency", "Dependency must reference a Step in the same project")
        if dep == step["id"]:
            raise PmtError("invalid_dependency", "A Step cannot depend on itself")
    # Reject cycles across stored Step dependencies before creating queue state.
    active, visited = set(), set()
    def visit(item):
        if item in active:
            raise PmtError("dependency_cycle", "Execution dependency graph contains a cycle")
        if item in visited:
            return
        active.add(item)
        child = conn.execute("SELECT dependencies_json FROM step_specs WHERE step_id=?", (item,)).fetchone()
        for parent in _json(child[0], "Step dependencies", list) if child else []:
            visit(parent)
        active.remove(item); visited.add(item)
    active.add(step["id"])
    for dep in dependencies:
        visit(dep)
    active.remove(step["id"])
    now, job_id, run_id = utc_now(), new_id(), new_id()
    conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,'queued',?,?,?)",
                 (job_id, step["id"], canonical_json({**policy, "max_retries": retries, "dependencies": dependencies}), now, now))
    intent = {"job_id": job_id, "run_id": run_id, "step_id": step["id"], "scope_id": step["scope_id"],
              "directive_ref": {"artifact_id": step["directive_id"], "version": step["directive_version"]},
              "requirements_version": step["requirements_version"], "plan_version": step["plan_version"],
              "plan_id": step["plan_id"], "role": step["role"], "product_stage": step["product_stage"],
              "workspace": normalized_workspace(step["workspace"]), "scopes": scope,
              "criteria": _json(step["criteria_json"], "Step criteria", list), "dependencies": dependencies,
              "route": route, "policy": {"max_retries": retries}}
    conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,1,'queued',1,?,?,?,?,?,?,?,?)",
                 (run_id, job_id, step["id"], req["session_id"], step["directive_version"], intent["workspace"],
                  canonical_json(scope), canonical_json(route), canonical_json(intent), now, now))
    event(conn, req, "execution.queued", scope_id=step["scope_id"], record_id=step["id"],
          payload={"job_id": job_id, "run_id": run_id, "attempt": 1, "scope_refs": [s["lock_key"] for s in scope]})
    return {"job_id": job_id, "run_id": run_id, "attempt": 1, "state": "queued", "revision": 1,
            "queue_position": conn.execute("SELECT count(*) FROM execution_runs WHERE state='queued' AND created_at<=?", (now,)).fetchone()[0]}


def _prepare(conn, req, p):
    run = _get_run(conn, p.get("run_id")); _owned(req, run); _cas(run, p)
    from ..efficiency.batch import assert_batch_child_dispatch
    assert_batch_child_dispatch(conn, run["id"], "prepare independently")
    if run["state"] != "queued":
        raise PmtError("invalid_transition", "Only a queued run can be prepared", 3)
    intent = _json(run["intent_json"], "run intent", dict)
    step = _step(conn, run["step_id"])
    _validate_current_step(conn, run, step)
    dependencies = intent["dependencies"]
    for dep in dependencies:
        depstate = conn.execute("SELECT state FROM records WHERE id=?", (dep,)).fetchone()[0]
        if depstate != "Done":
            return {"run_id": run["id"], "state": "queued", "revision": run["revision"],
                    "waiting_reason": "dependencies_incomplete", "dependencies": dependencies}
    route = _json(run["route_json"], "route", dict)
    if route.get("blocked") or route.get("actual_support") != "verified_supported":
        _transition(conn, req, run, "blocked", completed_at=utc_now(), keep_locks=False)
        conn.execute("UPDATE execution_jobs SET state='blocked',updated_at=? WHERE id=?", (utc_now(), run["job_id"]))
        return {"run_id": run["id"], "state": "blocked", "revision": run["revision"] + 1,
                "reason": route.get("selection_reason_code", "route_unsupported")}
    from ..efficiency.batch import physical_active_runs
    active_states = {"starting", "running", "reconciling", "cancel_requested"}
    global_runs = physical_active_runs(conn, active_states)
    executor = route.get("adapter_kind") or route["mode"]
    executor_runs = physical_active_runs(conn, active_states, agent=route["agent"], executor=executor)
    cap = min(3, route["max_concurrency"])
    if len(global_runs) >= 3 or len(executor_runs) >= cap:
        return {"run_id": run["id"], "state": "queued", "revision": run["revision"], "waiting_reason": "route_capacity"}
    scopes = _json(run["scopes_json"], "run scopes", list)
    conflicts = _acquire_scopes(conn, run["id"], run["owner_session"], scopes)
    if conflicts:
        return {"run_id": run["id"], "state": "queued", "revision": run["revision"],
                "waiting_reason": "scope_conflict", "conflicts": conflicts}
    updated_intent = {**intent, "pinned_at": utc_now(), "directive_version": run["directive_version"],
                      "route": route, "scope_refs": [s["lock_key"] for s in scopes]}
    revision = _transition(conn, req, run, "starting", intent=updated_intent, started_at=utc_now())
    job = conn.execute("UPDATE execution_jobs SET state='starting',updated_at=? WHERE id=? AND state='queued'", (utc_now(), run["job_id"]))
    if job.rowcount != 1:
        raise PmtError("job_state_conflict", "Execution job changed concurrently", 3)
    event(conn, req, "scope_lock.acquired", record_id=run["step_id"], payload={"run_id": run["id"], "scope_refs": [s["lock_key"] for s in scopes]})
    return {"run_id": run["id"], "job_id": run["job_id"], "state": "starting", "revision": revision,
            "intent": updated_intent, "scope_locks": [s["lock_key"] for s in scopes]}


def _attach(conn, req, p):
    run = _get_run(conn, p.get("run_id")); _owned(req, run)
    from ..efficiency.batch import assert_batch_child_dispatch
    assert_batch_child_dispatch(conn, run["id"], "attach a separate physical handle")
    handle = p.get("handle")
    if not isinstance(handle, dict) or not handle:
        raise PmtError("invalid_handle", "A nonempty execution handle object is required")
    if run["handle_json"]:
        prior = _json(run["handle_json"], "execution handle", dict)
        if prior == handle:
            return {"run_id": run["id"], "state": run["state"], "revision": run["revision"], "replayed": True}
        raise PmtError("execution_record_conflict", "A different execution handle is already attached", 3)
    _cas(run, p)
    if run["state"] != "starting":
        raise PmtError("invalid_transition", "A handle can be attached only while starting", 3)
    rev = _transition(conn, req, run, "running", handle=handle)
    conn.execute("UPDATE execution_jobs SET state='running',updated_at=? WHERE id=?", (utc_now(), run["job_id"]))
    from ..efficiency.batch import bind_actual_parent_handle
    bind_actual_parent_handle(conn, req, _get_run(conn, run["id"]), handle)
    event(conn, req, "runner.handle_recorded", record_id=run["step_id"], payload={"run_id": run["id"], "handle_ref": handle.get("id")})
    return {"run_id": run["id"], "state": "running", "revision": rev}


def _observe(conn, req, p):
    run = _get_run(conn, p.get("run_id")); _owned(req, run); _cas(run, p)
    observation = p.get("observation")
    from ..operations import PROGRESS_FIELDS
    if not isinstance(observation, dict) or set(observation) - PROGRESS_FIELDS:
        raise PmtError("invalid_observation", "Observation contains unsupported fields")
    for key in ("stage", "state", "model", "route", "wait_reason", "next_action"):
        if key in observation and (not isinstance(observation[key], str) or len(observation[key]) > 500):
            raise PmtError("invalid_observation", "Observation text must be a bounded string")
    if "artifact_refs" in observation and (not isinstance(observation["artifact_refs"], list)
            or len(observation["artifact_refs"]) > 100
            or any(not isinstance(ref, str) or len(ref) > 200 for ref in observation["artifact_refs"])):
        raise PmtError("invalid_observation", "artifact_refs must contain bounded references")
    if "user_decision_needed" in observation and type(observation["user_decision_needed"]) is not bool:
        raise PmtError("invalid_observation", "user_decision_needed must be boolean")
    now = utc_now(); body = canonical_json(observation)
    prior = conn.execute("SELECT body_json,last_changed FROM run_progress WHERE run_id=?", (run["id"],)).fetchone()
    changed = not prior or prior[0] != body
    conn.execute("INSERT INTO run_progress(run_id,last_seen,last_changed,body_json) VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET last_seen=excluded.last_seen,last_changed=CASE WHEN excluded.body_json<>run_progress.body_json THEN excluded.last_changed ELSE run_progress.last_changed END,body_json=excluded.body_json",
                 (run["id"], now, now, body))
    # An observation is telemetry only; it never changes authoritative run state or frees locks.
    return {"run_id": run["id"], "state": run["state"], "revision": run["revision"],
            "observation_changed": changed, "last_seen": now, "last_changed": now if changed else prior[1]}


def _submit_result(conn, req, p):
    run = _get_run(conn, p.get("run_id")); _owned(req, run)
    result = p.get("result")
    if not isinstance(result, dict):
        raise PmtError("invalid_result", "Execution result must be an object")
    if run["result_json"]:
        prior = _json(run["result_json"], "execution result", dict)
        if prior == result:
            return {"run_id": run["id"], "state": run["state"], "revision": run["revision"], "replayed": True}
        raise PmtError("execution_record_conflict", "A different result is already stored for this run", 3)
    _cas(run, p)
    step = _step(conn, run["step_id"], require_directive=False)
    pinned_intent = _json(run["intent_json"], "run intent", dict)
    try:
        _validate_current_step(conn, run, step)
        stale_assessment = {"current": True}
    except PmtError as exc:
        stale_assessment = {"current": False, "reason_code": exc.code}
    if result.get("directive_version") != run["directive_version"]:
        raise PmtError("directive_version_conflict", "Result does not match the pinned directive version", 3)
    if result.get("actual_route") != _json(run["route_json"], "route", dict):
        raise PmtError("actual_route_mismatch", "Result must identify the selected actual route", 3)
    criteria = pinned_intent.get("criteria")
    if not isinstance(criteria, list):
        raise PmtError("stored_record_invalid", "Pinned Step criteria are invalid", 5)
    reports = result.get("criteria_results")
    expected_criteria = {(x if isinstance(x, str) else x.get("id")) for x in criteria}
    reported_criteria = [x.get("criterion_id", x.get("id")) for x in reports if isinstance(x, dict)] if isinstance(reports, list) else []
    if not isinstance(reports, list) or len(reported_criteria) != len(expected_criteria) or set(reported_criteria) != expected_criteria:
        raise PmtError("criteria_incomplete", "Result must report every Step criterion exactly once")
    if any(not isinstance(x, dict) or x.get("outcome") not in {"pass", "fail", "blocked", "not_run"}
           or not isinstance(x.get("evidence_refs", []), list)
           or (x.get("outcome") in {"pass", "fail"} and not x.get("evidence_refs"))
           or (x.get("outcome") in {"blocked", "not_run"}
               and (not isinstance(x.get("reason"), str) or not x["reason"].strip())) for x in reports):
        raise PmtError("invalid_criterion_result", "Criterion results require evidence for pass/fail and a reason for blocked/not_run")
    for item in reports:
        for ref in item["evidence_refs"]:
            identifier(ref, "evidence_ref")
            if conn.execute("SELECT 1 FROM artifacts WHERE id=? AND state='ready'", (ref,)).fetchone() is None:
                raise PmtError("evidence_unavailable", "Criterion evidence must reference a ready artifact", 3)
    allowed = {"starting", "running", "cancel_requested", "reconciling", "failed", "canceled"}
    if run["state"] not in allowed:
        raise PmtError("invalid_transition", "This run cannot accept a new result", 3)
    # Preserve late results after cancel/failure, but they remain reviewable facts and do not imply success.
    stop_refs = result.get("stop_evidence_refs")
    stop_confirmed = result.get("stop_confirmed") is True and isinstance(stop_refs, list) and bool(stop_refs)
    if stop_confirmed and any(not isinstance(ref, str) or not ref.strip() for ref in stop_refs):
        raise PmtError("evidence_required", "Stop evidence references must be nonempty strings")
    state = ("review_pending" if stop_confirmed else "reconciling") if run["state"] not in {"cancel_requested", "canceled", "failed"} else run["state"]
    if stale_assessment["current"] is False:
        pinned_intent["result_assessment"] = {"current_step_matches": False, "reason_code": stale_assessment["reason_code"]}
    rev = _transition(conn, req, run, state, result=result,
                      stop_confirmed=1 if stop_confirmed else None,
                      completed_at=utc_now() if stop_confirmed else None,
                      intent=pinned_intent, keep_locks=True)
    if state != "review_pending":
        conn.execute("UPDATE execution_jobs SET state=?,updated_at=? WHERE id=?", (state, utc_now(), run["job_id"]))
    event(conn, req, "runner.receipt_persisted", record_id=run["step_id"], payload={"run_id": run["id"], "result_ref": result.get("receipt_ref")})
    return {"run_id": run["id"], "state": state, "revision": rev, "stored": True,
            "late_result": state != "review_pending"}


def materialize_batch_leader_result(conn, req, *, batch_ref, report_ref, report_sha256, result):
    """Replace only a validated F9 container result with its representative Step result.

    This is an internal collection seam, never exposed as a generic submit-result flag.
    The exact group/report receipt and stopped parent are checked again in the write transaction.
    """
    from ..efficiency.batch import binding_for_run
    run = _get_run(conn, req.get("payload", {}).get("run_id"))
    _owned(req, run)
    group = binding_for_run(conn, run["id"], require_leader=True)
    if not group:
        raise PmtError("batch_binding_not_found", "Representative run is not bound to an F9 group", 3)
    body = group["body"]
    if body.get("batch_id") != batch_ref or body.get("parent_run_ref") != run["id"]:
        raise PmtError("batch_binding_conflict", "Representative run belongs to another F9 group", 3)
    member = next((item for item in body.get("members", []) if item.get("run_id") == run["id"]), None)
    if not member or member.get("step_id") != run["step_id"]:
        raise PmtError("batch_child_mapping_invalid", "Leader Step is not a member of its F9 group", 3)
    if run["state"] != "review_pending" or run.get("stop_confirmed") != 1:
        raise PmtError("batch_parent_not_stopped", "Only a stopped review-pending parent can be materialized", 3)
    prior = _json(run.get("result_json"), "stored parent result", dict)
    if (prior.get("batch_ref") != batch_ref or prior.get("batch_report_ref") != report_ref
            or prior.get("batch_report_sha256") != report_sha256):
        raise PmtError("execution_record_conflict", "Stored group result does not match the collected report", 3)
    if (result.get("batch_ref") != batch_ref or result.get("batch_report_ref") != report_ref
            or result.get("batch_report_sha256") != report_sha256
            or result.get("physical_parent_receipt_ref") != prior.get("receipt_ref")):
        raise PmtError("execution_record_conflict", "Representative result lost its physical group receipt", 3)
    pinned = _json(run["intent_json"], "run intent", dict)
    current_step = _step(conn, run["step_id"], require_directive=False)
    _validate_current_step(conn, run, current_step)
    if result.get("directive_version") != run["directive_version"]:
        raise PmtError("directive_version_conflict", "Representative result directive changed", 3)
    if result.get("actual_route") != _json(run["route_json"], "route", dict):
        raise PmtError("actual_route_mismatch", "Representative result route changed", 3)
    expected = {(value if isinstance(value, str) else value.get("id")) for value in pinned.get("criteria", [])}
    reports = result.get("criteria_results")
    reported = [value.get("criterion_id", value.get("id")) for value in reports if isinstance(value, dict)] \
        if isinstance(reports, list) else []
    if (not isinstance(reports, list) or len(reported) != len(expected) or set(reported) != expected):
        raise PmtError("criteria_incomplete", "Representative result does not match its pinned criteria", 3)
    evidence_ids = set(result.get("evidence_refs", []))
    for criterion in reports:
        if (not isinstance(criterion, dict) or criterion.get("outcome") not in {"pass", "fail", "blocked", "not_run"}
                or not isinstance(criterion.get("evidence_refs"), list)):
            raise PmtError("invalid_criterion_result", "Representative criterion evidence is invalid", 3)
        evidence_ids.update(criterion["evidence_refs"])
    f7 = result.get("batch_child_f7_ref")
    if (not isinstance(f7, dict) or not isinstance(f7.get("id"), str)
            or not isinstance(f7.get("sha256"), str)
            or f7.get("id") not in evidence_ids):
        raise PmtError("batch_result_evidence_invalid", "Representative F7 evidence reference is missing", 3)
    f7_row = conn.execute("SELECT sha256,state FROM artifacts WHERE id=?", (f7["id"],)).fetchone()
    if not f7_row or f7_row["state"] != "ready" or f7_row["sha256"] != f7["sha256"]:
        raise PmtError("batch_result_evidence_invalid", "Representative F7 evidence hash is not current", 3)
    for evidence_id in evidence_ids:
        identifier(evidence_id, "evidence_ref")
        if conn.execute("SELECT 1 FROM artifacts WHERE id=? AND state='ready'", (evidence_id,)).fetchone() is None:
            raise PmtError("evidence_unavailable", "Representative result evidence is not ready", 3)
    if (result.get("stop_confirmed") is not True
            or result.get("stop_evidence_refs") != prior.get("stop_evidence_refs")
            or not result.get("stop_evidence_refs")):
        raise PmtError("evidence_required", "Representative result must retain physical stop evidence", 3)
    for stop_ref in result["stop_evidence_refs"]:
        identifier(stop_ref, "stop_evidence_ref")
        if conn.execute("SELECT 1 FROM artifacts WHERE id=? AND state='ready'", (stop_ref,)).fetchone() is None:
            raise PmtError("evidence_unavailable", "Physical stop evidence is not ready", 3)
    _cas(run, req["payload"])
    updated = conn.execute("UPDATE execution_runs SET result_json=?,revision=revision+1,updated_at=? "
        "WHERE id=? AND revision=? AND state='review_pending' AND stop_confirmed=1",
        (canonical_json(result), utc_now(), run["id"], run["revision"]))
    if updated.rowcount != 1:
        raise PmtError("revision_conflict", "Representative run changed during group materialization", 3, True)
    event(conn, req, "execution.batch_child_result_materialized", record_id=run["step_id"],
          payload={"run_id": run["id"], "batch_ref": batch_ref,
                   "container_result_sha256": fingerprint(prior),
                   "step_result_sha256": fingerprint(result),
                   "report_ref": report_ref, "report_sha256": report_sha256,
                   "f7_evidence_ref": f7["id"], "f7_evidence_sha256": f7["sha256"]})
    return {"run_id": run["id"], "revision": run["revision"] + 1, "materialized": True}


def _cancel(conn, req, p):
    run = _get_run(conn, p.get("run_id")); _owned(req, run); _cas(run, p)
    from ..efficiency.batch import assert_batch_child_dispatch, cancel_unstarted_group
    group = assert_batch_child_dispatch(conn, run["id"], "cancel the grouped physical handle")
    if group and run["id"] == group["body"].get("parent_run_ref"):
        stopped = cancel_unstarted_group(conn, req, run)
        if stopped:
            return {"run_id": run["id"], "batch": stopped, "state": "canceled",
                    "revision": conn.execute("SELECT revision FROM execution_runs WHERE id=?",
                                              (run["id"],)).fetchone()[0],
                    "awaiting_stop_confirmation": False}
    if run["state"] == "queued":
        rev = _transition(conn, req, run, "canceled", stop_confirmed=1, completed_at=utc_now(), keep_locks=False)
    elif run["state"] in _CANCEL_TERMINAL:
        return {"run_id": run["id"], "state": run["state"], "revision": run["revision"], "already_terminal": True}
    elif run["state"] == "cancel_requested":
        return {"run_id": run["id"], "state": run["state"], "revision": run["revision"], "awaiting_stop_confirmation": True}
    elif run["state"] in {"starting", "running", "review_pending", "reconciling"}:
        rev = _transition(conn, req, run, "cancel_requested")
    else:
        raise PmtError("invalid_transition", "Run cannot be canceled", 3)
    state = "canceled" if run["state"] == "queued" else "cancel_requested"
    conn.execute("UPDATE execution_jobs SET state=?,updated_at=? WHERE id=?", (state, utc_now(), run["job_id"]))
    return {"run_id": run["id"], "state": state, "revision": rev,
            "awaiting_stop_confirmation": state == "cancel_requested"}


def _reconcile(conn, req, p):
    run = _get_run(conn, p.get("run_id")); _owned(req, run); _cas(run, p)
    if run["state"] not in {"starting", "running", "cancel_requested", "reconciling"}:
        raise PmtError("invalid_transition", "Only uncertain or active runs can be reconciled", 3)
    stopped = p.get("stopped")
    not_started = p.get("not_started")
    if type(stopped) is not bool or type(not_started) is not bool or (stopped and not_started):
        raise PmtError("invalid_reconciliation", "Provide a confirmed stopped or not_started observation")
    evidence = p.get("evidence_refs")
    if not isinstance(evidence, list) or not evidence or any(not isinstance(x, str) or not x for x in evidence):
        raise PmtError("evidence_required", "Reconciliation requires evidence references")
    now = utc_now()
    if not stopped and not not_started:
        state = "reconciling"
        rev = _transition(conn, req, run, state)
        return {"run_id": run["id"], "state": state, "revision": rev, "scope_locks_retained": True}
    if run["state"] == "cancel_requested" or p.get("actual_state") == "canceled":
        state = "canceled"
    elif not_started:
        state = "blocked" if p.get("actual_state") == "blocked" else "failed"
    else:
        actual = p.get("actual_state")
        if actual not in {"failed", "blocked", "review_pending", "succeeded"}:
            raise PmtError("invalid_reconciliation", "Confirmed stopped run requires its actual terminal state")
        state = "review_pending" if actual in {"review_pending", "succeeded"} else actual
    detail = _json(run["intent_json"], "run intent", dict)
    detail["reconciliation"] = {"stopped": stopped, "not_started": not_started,
                                "actual_state": p.get("actual_state"), "evidence_refs": evidence}
    rev = _transition(conn, req, run, state, stop_confirmed=1, completed_at=now,
                      intent=detail, keep_locks=state == "review_pending")
    if state != "review_pending":
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (run["id"],))
    conn.execute("UPDATE execution_jobs SET state=?,updated_at=? WHERE id=?", (state, now, run["job_id"]))
    return {"run_id": run["id"], "state": state, "revision": rev,
            "scope_locks_retained": state == "review_pending"}


def _review(db, conn, req, p):
    run = _get_run(conn, p.get("run_id"))
    if req.get("actor") != "main":
        raise PmtError("review_requires_main", "Only main may review an execution result", 3)
    _cas(run, p)
    if run["state"] != "review_pending" or not run["result_json"]:
        raise PmtError("review_unavailable", "A durable result in review_pending is required", 3)
    step = _step(conn, run["step_id"])
    _validate_current_step(conn, run, step)
    if not run["stop_confirmed"]:
        raise PmtError("stop_confirmation_required", "Run termination must be confirmed before review", 3)
    result = _json(run["result_json"], "execution result", dict)
    if p.get("directive_version") != step["directive_version"] or p.get("directive_version") != run["directive_version"]:
        raise PmtError("directive_version_conflict", "Review must match the current Step directive", 3)
    if p.get("accepted") is not True:
        raise PmtError("review_rejected", "A rejected result must be resolved or retried", 3)
    required_refs = p.get("verification_ids")
    if not isinstance(required_refs, list) or not required_refs:
        raise PmtError("verification_required", "Step completion requires valid verification IDs", 3)
    # Reuse the existing authoritative verification validator. It raises unless every Step criterion
    # has current, passing evidence for this exact record and environment.
    from ..verification import verify_completion
    verification = verify_completion(db, conn, step, required_refs)
    if not verification.get("valid"):
        raise PmtError("verification_unavailable", "Step verification is not currently valid", 3,
                       details={"reasons": verification.get("reasons", [])})
    required_criteria = {x if isinstance(x, str) else x.get("id") for x in _json(step["criteria_json"], "Step criteria", list)}
    if not required_criteria.issubset(set(verification.get("covered_criteria", []))):
        raise PmtError("verification_unavailable", "Step criteria are not covered by current verification", 3)
    if any(item.get("outcome") != "pass" for item in result.get("criteria_results", [])):
        raise PmtError("criteria_not_passed", "Every execution criterion must pass before review")
    if p.get("integration_confirmed") is not True:
        raise PmtError("integration_unconfirmed", "Integration must be explicitly confirmed before releasing the run")
    evidence = p.get("evidence_refs")
    if not isinstance(evidence, list) or not evidence:
        raise PmtError("evidence_required", "Review requires integration evidence references")
    rev = _transition(conn, req, run, "succeeded", completed_at=utc_now(), keep_locks=False)
    conn.execute("UPDATE execution_jobs SET state='succeeded',updated_at=? WHERE id=?", (utc_now(), run["job_id"]))
    event(conn, req, "execution.reviewed", record_id=run["step_id"], payload={"run_id": run["id"], "verification_ids": required_refs, "evidence_refs": evidence})
    return {"run_id": run["id"], "state": "succeeded", "revision": rev, "step_state_unchanged": True}


def _retry(conn, req, p):
    run = _get_run(conn, p.get("run_id")); _owned(req, run); _cas(run, p)
    from ..efficiency.batch import assert_batch_child_dispatch
    if assert_batch_child_dispatch(conn, run["id"], "retry independently"):
        raise PmtError("batch_retry_requires_new_group", "Grouped retry requires a new complete member set", 3)
    policy = _json(conn.execute("SELECT policy_json FROM execution_jobs WHERE id=?", (run["job_id"],)).fetchone()[0], "execution policy", dict)
    max_retries = policy.get("max_retries", 2)
    reason = p.get("reason")
    if reason != "transient" or run["attempt"] > max_retries:
        raise PmtError("retry_not_allowed", "Retry requires a transient failure within policy", 3)
    if run["state"] not in {"failed", "blocked", "canceled"} or (run["state"] != "queued" and not run["stop_confirmed"]):
        raise PmtError("run_not_stopped", "Previous attempt must be confirmed ended before retry", 3)
    if run["attempt"] >= 3:
        raise PmtError("retry_limit", "At most two retries are allowed", 3)
    if conn.execute("SELECT 1 FROM execution_runs WHERE step_id=? AND state IN ('queued','starting','running','review_pending','reconciling','cancel_requested')", (run["step_id"],)).fetchone():
        raise PmtError("active_run_exists", "Step already has an active run", 3)
    now, next_id, attempt = utc_now(), new_id(), run["attempt"] + 1
    conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,'queued',1,?,?,?,?,?,?,?,?)",
                 (next_id, run["job_id"], run["step_id"], attempt, req["session_id"], run["directive_version"], run["workspace"], run["scopes_json"], run["route_json"], run["intent_json"], now, now))
    conn.execute("UPDATE execution_jobs SET state='queued',updated_at=? WHERE id=?", (now, run["job_id"]))
    event(conn, req, "execution.retry_queued", record_id=run["step_id"], payload={"job_id": run["job_id"], "run_id": next_id, "previous_run_id": run["id"], "attempt": attempt})
    return {"job_id": run["job_id"], "run_id": next_id, "attempt": attempt, "state": "queued", "revision": 1}


def _extend(conn, req, p):
    run = _get_run(conn, p.get("run_id")); _owned(req, run); _cas(run, p)
    from ..efficiency.batch import assert_batch_child_dispatch
    batch = assert_batch_child_dispatch(conn, run["id"], "extend scope independently")
    if batch:
        raise PmtError("batch_scope_union_immutable", "Grouped scope union cannot be extended after preparation", 3)
    if run["state"] not in {"starting", "running"}:
        raise PmtError("invalid_transition", "Scopes can only be extended by a live run", 3)
    intent = _json(run["intent_json"], "run intent", dict)
    additions = _normalize_scopes(p.get("scopes"), run["workspace"])
    existing = _json(run["scopes_json"], "run scopes", list)
    existing_keys = {s["lock_key"] for s in existing}
    additions = [s for s in additions if s["lock_key"] not in existing_keys]
    conflicts = _acquire_scopes(conn, run["id"], run["owner_session"], additions)
    if conflicts:
        # No partial acquisitions: check first, then insert. Existing lock rows remain held.
        event(conn, req, "scope_lock.conflict", record_id=run["step_id"], payload={"run_id": run["id"], "conflicts": conflicts})
        return {"run_id": run["id"], "state": run["state"], "revision": run["revision"], "extended": False, "conflicts": conflicts}
    combined = sorted(existing + additions, key=lambda s: s["lock_key"])
    intent["scopes"] = combined
    rev = _transition(conn, req, run, run["state"], intent=intent)
    conn.execute("UPDATE execution_runs SET scopes_json=? WHERE id=?", (canonical_json(combined), run["id"]))
    return {"run_id": run["id"], "state": run["state"], "revision": rev, "extended": True,
            "scope_locks": [s["lock_key"] for s in combined]}


def _read(conn, p):
    if p.get("run_id"):
        run = _get_run(conn, p["run_id"])
        run["intent"] = _json(run.pop("intent_json"), "run intent", dict)
        run["route"] = _json(run.pop("route_json"), "route", dict)
        run["scopes"] = _json(run.pop("scopes_json"), "run scopes", list)
        run["result"] = _json(run.pop("result_json"), "execution result", dict) if run.get("result_json") else None
        run.pop("handle_json", None)  # Native handles are returned only to their owner on state-changing calls.
        return {"run": run, "scope_locks": _scope_rows(conn, p["run_id"])}
    if p.get("job_id"):
        identifier(p["job_id"], "job_id")
        job = conn.execute("SELECT * FROM execution_jobs WHERE id=?", (p["job_id"],)).fetchone()
        if not job: raise PmtError("job_not_found", "Execution job does not exist")
        return {"job": dict(job), "runs": [dict(r) for r in conn.execute("SELECT id,attempt,state,revision,owner_session,directive_version,started_at,completed_at,created_at,updated_at FROM execution_runs WHERE job_id=? ORDER BY attempt", (p["job_id"],))]}
    raise PmtError("invalid_payload", "run_id or job_id is required")


def _queue(conn, p, scope_id=None):
    state = p.get("state", "queued")
    if state not in {"queued", *ACTIVE_RUN_STATES, "succeeded", "failed", "blocked", "canceled"}:
        raise PmtError("invalid_state", "Unsupported queue state")
    if scope_id is None:
        # Keep legacy local callers' namespace-wide behavior. Host READ requests require
        # scope_id and always take the descendant-filtered branch below.
        rows = conn.execute("SELECT r.id run_id,r.job_id,r.step_id,r.attempt,r.state,r.revision,r.created_at,j.state job_state FROM execution_runs r JOIN execution_jobs j ON j.id=r.job_id WHERE r.state=? ORDER BY r.created_at,r.id",
                            (state,)).fetchall()
    else:
        identifier(scope_id, "scope_id")
        if conn.execute("SELECT 1 FROM scopes WHERE id=?", (scope_id,)).fetchone() is None:
            raise PmtError("scope_not_found", "Selected queue scope does not exist", 3)
        rows = conn.execute("WITH RECURSIVE descendant_scopes(id) AS ("
            "SELECT id FROM scopes WHERE id=? UNION "
            "SELECT child.id FROM scopes child JOIN descendant_scopes parent ON child.parent_id=parent.id) "
            "SELECT r.id run_id,r.job_id,r.step_id,r.attempt,r.state,r.revision,r.created_at,j.state job_state "
            "FROM execution_runs r JOIN execution_jobs j ON j.id=r.job_id "
            "JOIN records step ON step.id=r.step_id "
            "WHERE r.state=? AND step.scope_id IN (SELECT id FROM descendant_scopes) "
            "ORDER BY r.created_at,r.id", (scope_id, state)).fetchall()
    return {"state": state, "items": [dict(row) for row in rows], "count": len(rows)}


def handle(db, conn, req) -> dict:
    """Run inside Database.run_request for writes or a read connection for reads."""
    op, p = req.get("operation"), _payload(req)
    handlers = {"enqueue_execution": _enqueue, "prepare_execution": _prepare,
        "attach_execution_handle": _attach, "observe_execution": _observe,
        "submit_execution_result": _submit_result, "request_execution_cancel": _cancel,
        "reconcile_execution": _reconcile, "review_execution": _review,
        "retry_execution": _retry, "extend_execution_scopes": _extend}
    if op == "read_execution": return _read(conn, p)
    if op == "list_execution_queue": return _queue(conn, p, req.get("scope_id"))
    if op == "review_execution":
        return _review(db, conn, req, p)
    handler = handlers.get(op)
    if handler is None: raise PmtError("operation_unsupported", "Unsupported execution operation")
    return handler(conn, req, p)
