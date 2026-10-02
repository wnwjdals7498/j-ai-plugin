from __future__ import annotations

import copy
import json
import uuid

import pytest
pytest_plugins = ["test_phase3_context"]

pytest_plugins = ["test_phase3_context"]

from pmt.errors import PmtError
from pmt.phase2_common import normalized_workspace
from pmt.efficiency.batch import BatchPlanner, BATCH_SCHEMA, BINDING_SCHEMA
from pmt.util import fingerprint
from pmt.store import LocalStore


def _id():
    return str(uuid.uuid4())


def _sha(char):
    return char * 64


def _step(*, step_id=None, dependencies=None, conflicts=None, scopes=None,
          project_id=None, source_hash=None, root_hash=None):
    return {"step_id": step_id or _id(), "project_id": project_id or PROJECT,
            "scope_id": _id(), "source_pin_hash": source_hash or SOURCE,
            "root_constraint_sha256": root_hash or ROOT,
            "directive": {"ref": "directive:" + _id(), "version": "4", "sha256": _sha("a")},
            "criteria": [{"id": "criteria:one", "sha256": _sha("b")}],
            "dependencies": dependencies or [], "conflicts": conflicts or [],
            "scopes": scopes or [{"kind": "path", "workspace": WORKSPACE,
                                  "resource": "src/" + _id(), "access": "write"}],
            "authority_ref": {"kind": "scope_authority", "id": "scope-proof:" + _id(),
                              "sha256": _sha("c")}}


PROJECT = _id()
SOURCE = _sha("d")
ROOT = _sha("e")
WORKSPACE = "C:/pmt-workspace"


def _context(steps):
    return {"context_ref": "context:" + _id(), "version": 3, "project_id": PROJECT,
            "source_pin_hash": SOURCE, "scope_ref": "scope-project:" + PROJECT,
            "member_step_ids": [step["step_id"] for step in steps],
            "shared_context_sha256": _sha("f"), "omitted_required": []}


def _capability(**overrides):
    value = {"capability_ref": "capability:local-fixture", "version": 1, "sha256": _sha("1"),
             "status": "verified_supported", "max_steps": 4, "multi_directive": True,
             "structured_result_mapping": True, "result_mapping": "step_id",
             "cancellation_scope": "group", "physical_slots": 1,
             "single_step_supported": True}
    value.update(overrides)
    return value


def _eligible_plan(steps=None, **cap_overrides):
    steps = steps or [_step(), _step()]
    return BatchPlanner.prepare(steps, _context(steps), _capability(**cap_overrides))


def _binding(plan, *, children=None, **handle_overrides):
    if children is None:
        children = [{"step_id": member["step_id"], "run_id": _id(),
                     "directive_sha256": member["directive"]["sha256"],
                     "scope_authority_ref": "child-scope:" + member["step_id"]}
                    for member in plan["members"]]
    representative_run = next((item["run_id"] for item in children
                               if item["step_id"] == plan["representative_step_id"]), _id())
    handle = {"handle_ref": "handle:fixture-1", "parent_run_ref": representative_run, "state": "attached",
              "capability_ref": "capability:local-fixture", "capability_sha256": _sha("1"),
              "physical_slots": 1, "scope_union_sha256": plan["scope_union_sha256"],
              "scope_authority_ref": "scope-union-receipt:fixture"}
    handle.update(handle_overrides)
    return BatchPlanner.bind(plan, handle, children)


def _result(binding, step_id, run_id, *, state="succeeded", criteria=None, receipt="receipt:one"):
    criteria = criteria or [{"id": "criteria:one", "sha256": _sha("b"), "outcome": "pass",
                             "evidence_refs": ["artifact:evidence"], "reason": None}]
    member_map = {item["step_id"]: item for item in binding["members"]}
    return {"step_id": step_id, "run_id": run_id,
            "handle_ref": binding["handle"]["handle_ref"],
            "directive_sha256": member_map[step_id]["directive_sha256"],
            "state": state, "receipt_ref": receipt,
            "evidence_refs": ["artifact:evidence"], "criteria_results": criteria}


def _route():
    return {"agent": "codex", "provider": "fixture-provider", "model": "pmt-fixture-model",
            "mode": "cli", "adapter_kind": "cli", "selection_reason": "isolated fixture",
            "actual_support": "verified_supported", "auth_state": "authenticated",
            "capability_ref": "fixture-cli-capability-v1", "max_concurrency": 1}


def _queue_two_actual_runs(env):
    from contextlib import closing
    from pmt.phase2_common import persist_json_resource, load_json_resource
    from pmt.execution.service import _normalize_scopes
    from pmt.util import canonical_json, new_id, utc_now
    from test_phase3_context import _actual_f3_ready

    pin = _actual_f3_ready(env)
    route = _route()
    db = env["db"]
    first_run = env["run_id"]
    first_scope = [{"kind": "path", "workspace": str(env["workspace"]),
                    "resource": env["graph_path"]}]
    first_scopes = _normalize_scopes(first_scope, str(env["workspace"]))
    now = utc_now()
    with closing(db.connect()) as conn:
        first = conn.execute("SELECT * FROM execution_runs WHERE id=?", (first_run,)).fetchone()
        first_intent = json.loads(first["intent_json"])
    first_intent.update({"job_id": first["job_id"], "run_id": first_run,
        "step_id": env["step_id"], "scope_id": env["project_id"],
        "requirements_version": "requirements-v1", "plan_version": "plan-v1", "plan_id": None,
        "role": "lower", "product_stage": "prototype", "workspace": str(env["workspace"]),
        "scopes": first_scopes, "criteria": env["criteria"], "dependencies": [],
        "route": route, "policy": {"max_retries": 2}, "directive_ref": {"artifact_id": env["directive_id"],
        "version": 1}, "directive_version": 1, "task": {"task_id": env["item_id"],
        "step_id": env["step_id"]}})
    with db.write() as conn:
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (first_run,))
        conn.execute("UPDATE execution_runs SET state='queued',revision=revision+1,handle_json=NULL,result_json=NULL,"
                     "stop_confirmed=0,started_at=NULL,completed_at=NULL,route_json=?,intent_json=?,scopes_json=?,updated_at=? WHERE id=?",
                     (canonical_json(route), canonical_json(first_intent), canonical_json(first_scopes), now, first_run))
        conn.execute("UPDATE execution_jobs SET state='queued',updated_at=? WHERE id=?", (now, first["job_id"]))

    with closing(db.connect()) as conn:
        directive = load_json_resource(db, conn, env["directive_id"])
    step_id, job_id, run_id = new_id(), new_id(), new_id()
    directive["purpose"] = "Batch child fixture directive"
    directive["goal"] = "Keep its own criteria and result mapping"
    directive_id = persist_json_resource(db,
        {"request_id": new_id(), "actor": env["actor"], "session_id": env["session"],
         "scope_id": env["project_id"], "payload": {}}, directive,
        env["project_id"], "step_directive", step_id)["artifact_id"]
    child_scope = [{"kind": "path", "workspace": str(env["workspace"]), "resource": "src/f9-child.py"}]
    child_scopes = _normalize_scopes(child_scope, str(env["workspace"]))
    body = {"directive_id": directive_id, "directive_version": 1, "invalidated": False,
            "kind_tag": "test", "workspace": str(env["workspace"])}
    child_intent = {"job_id": job_id, "run_id": run_id, "step_id": step_id,
        "scope_id": env["project_id"], "directive_ref": {"artifact_id": directive_id, "version": 1},
        "requirements_version": "requirements-v1", "plan_version": "plan-v1", "plan_id": None,
        "role": "lower", "product_stage": "prototype", "workspace": str(env["workspace"]),
        "scopes": child_scopes, "criteria": env["criteria"], "dependencies": [],
        "route": route, "policy": {"max_retries": 2}, "directive_version": 1,
        "task": {"task_id": env["item_id"], "step_id": step_id}}
    with db.write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (step_id, "step", env["project_id"], env["item_id"], "Batch child Step",
                      "InProgress", canonical_json(body), 1, now, now))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (step_id, directive_id, 1, "requirements-v1", "plan-v1", None,
                      "lower", "prototype", str(env["workspace"]), canonical_json(child_scope),
                      canonical_json(env["criteria"]), "[]", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?, 'queued',?,?,?)",
                     (job_id, step_id, canonical_json({"max_retries": 2}), now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,1,'queued',1,?,1,?,?,?,?,?,?)",
                     (run_id, job_id, step_id, env["session"], str(env["workspace"]),
                      canonical_json(child_scopes), canonical_json(route), canonical_json(child_intent), now, now))
    with closing(db.connect()) as conn:
        revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (first_run,)).fetchone()[0]
    child_revision = 1
    return {**env, "batch_runs": [{"run_id": first_run, "expected_run_revision": revision},
                                  {"run_id": run_id, "expected_run_revision": child_revision}],
            "batch_step_ids": [env["step_id"], step_id], "batch_job_ids": [first["job_id"], job_id],
            "batch_child_run_id": run_id, "batch_pin": pin, "batch_route": route}


def _concurrent_prepare_worker(root, config_root, request, barrier, output):
    from pmt.db import Database
    barrier.wait(timeout=30)
    envelope, code = LocalStore(Database(root, config_root)).execute(request)
    output.put((code, envelope))


def _verify_and_review_batch_member(env, member, evidence_ids):
    from contextlib import closing
    from pmt.phase2 import execute as phase2_execute
    from pmt.steps import handle as steps_handle
    from pmt.verification import handle as verification_handle
    from pmt.util import canonical_json

    step_id, run_id = member["step_id"], member["run_id"]
    with closing(env["db"].connect()) as conn:
        record = conn.execute("SELECT body_json FROM records WHERE id=? AND kind='step'", (step_id,)).fetchone()
        body = json.loads(record["body_json"])
    body.update({"workspace": str(env["workspace"]), "criteria": env["criteria"]})
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?", (canonical_json(body), step_id))

    definition_id = "f9-local-review-fixture"
    command = ["local-fixture", "group-member-review"]
    criterion_ids = [item["id"] for item in env["criteria"]]
    before_request = {"protocol_version": 1, "operation": "lookup_verification", "request_id": _id(),
        "actor": "main", "session_id": env["session"], "scope_id": env["project_id"],
        "record_id": step_id, "payload": {"definition_id": definition_id, "definition_version": "1",
            "target_id": step_id, "command": command, "criterion_ids": criterion_ids}}
    with closing(env["db"].connect()) as conn:
        before = verification_handle(env["db"], conn, before_request)
    verification_request = {**before_request, "operation": "record_verification", "request_id": _id(),
        "payload": {**before_request["payload"], "outcome": "pass", "exit_code": 0,
            "evidence_ids": evidence_ids, "before_fingerprint": before["input_fingerprint"]}}
    verification, verification_code = env["db"].run_request(verification_request,
        lambda conn, request: verification_handle(env["db"], conn, request))
    assert verification_code == 0 and verification.get("ok"), verification.get("error")
    verification_id = verification["result"]["verification_id"]

    with closing(env["db"].connect()) as conn:
        run = conn.execute("SELECT revision,directive_version FROM execution_runs WHERE id=?", (run_id,)).fetchone()
    p2_request = {"protocol_version": 1, "operation": "review_execution", "request_id": _id(),
        "actor": "main", "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "codex"}, "payload": {"run_id": run_id,
            "expected_run_revision": run["revision"], "directive_version": run["directive_version"],
            "accepted": True, "verification_ids": [verification_id], "integration_confirmed": True,
            "evidence_refs": evidence_ids}}
    p2_review, p2_code = phase2_execute(env["db"], p2_request)
    assert p2_code == 0 and p2_review.get("ok"), p2_review.get("error")
    with closing(env["db"].connect()) as conn:
        step = conn.execute("SELECT revision FROM records WHERE id=?", (step_id,)).fetchone()
    step_request = {"protocol_version": 1, "operation": "review_step", "request_id": _id(),
        "actor": "main", "session_id": env["session"], "scope_id": env["project_id"],
        "record_id": step_id, "expected_revision": step["revision"],
        "payload": {"run_id": run_id, "verification_ids": [verification_id],
                    "integration_confirmed": True}}
    step_review, step_code = env["db"].run_request(step_request,
        lambda conn, request: steps_handle(env["db"], conn, request))
    assert step_code == 0 and step_review.get("ok"), step_review.get("error")
    return step_review["result"]


def test_f9_s1_eligible_manifest_pins_each_member_context_and_single_slot():
    steps = [_step(), _step()]
    plan = BatchPlanner.prepare(steps, _context(steps), _capability())
    assert plan["schema"] == BATCH_SCHEMA and plan["decision"] == "eligible"
    assert plan["execution_enabled"] is False
    assert plan["physical_slots"] == 1
    assert len(plan["members"]) == 2
    assert [member["step_id"] for member in plan["members"]] == [step["step_id"] for step in steps]
    assert {member["directive"]["sha256"] for member in plan["members"]} == {_sha("a")}
    assert plan["context"]["context_ref"] == _context_ref(plan, steps)
    assert plan["scope_union_sha256"] == fingerprint(plan["scope_union"])
    replay = BatchPlanner.prepare(steps, _context(steps), _capability())
    assert plan["group_nonce"] != replay["group_nonce"]


def _context_ref(plan, steps):
    return plan["context"]["context_ref"]


def test_f9_s1_cycle_and_incomplete_prerequisite_are_blocked():
    a, b = _id(), _id()
    steps = [_step(step_id=a, dependencies=[{"step_id": b, "state": "Planned"}]),
             _step(step_id=b, dependencies=[{"step_id": a, "state": "Planned"}])]
    cycle = BatchPlanner.prepare(steps, _context(steps), _capability())
    assert cycle["decision"] == "blocked" and "dependency_cycle" in cycle["reason_codes"]

    steps = [_step(dependencies=[{"step_id": _id(), "state": "In Progress"}]), _step()]
    pending = BatchPlanner.prepare(steps, _context(steps), _capability())
    assert pending["decision"] == "blocked"
    assert "external_dependency_incomplete" in pending["reason_codes"]


def test_f9_s1_conflict_version_root_context_and_capability_mismatch_split_safely():
    shared_path = [{"kind": "path", "workspace": WORKSPACE, "resource": "src/shared", "access": "write"}]
    steps = [_step(scopes=shared_path), _step(scopes=shared_path)]
    conflict = BatchPlanner.prepare(steps, _context(steps), _capability())
    assert conflict["decision"] == "split"
    assert "overlapping_write_scope" in conflict["reason_codes"]
    assert all(len(group["step_refs"]) == 1 for group in conflict["proposed_groups"])

    steps = [_step(), _step(root_hash=_sha("9"))]
    root_mismatch = BatchPlanner.prepare(steps, _context(steps), _capability())
    assert "root_constraint_mismatch" in root_mismatch["reason_codes"]

    steps = [_step(), _step()]
    context = _context(steps)
    context["omitted_required"] = ["directive:required-context"]
    omitted = BatchPlanner.prepare(steps, context, _capability())
    assert omitted["decision"] == "blocked" and "required_context_omitted" in omitted["reason_codes"]

    steps = [_step(), _step()]
    unsupported = BatchPlanner.prepare(steps, _context(steps),
        _capability(multi_directive=False, max_steps=1))
    assert unsupported["decision"] == "split"
    assert "multi_step_structured_results_unsupported" in unsupported["reason_codes"]


def test_workspace_scope_accepts_only_absolute_local_or_canonical_pmt_uri():
    repo = _id()
    workspace_uri = f"pmt://{repo}/{_sha('7')}"
    assert normalized_workspace(workspace_uri) == workspace_uri
    with pytest.raises(PmtError, match="workspace"):
        normalized_workspace(f"pmt://{repo}/not-a-hash")
    steps = [_step(scopes=[{"kind": "path", "workspace": workspace_uri,
                            "resource": "src/one.py", "access": "write"}]),
             _step(scopes=[{"kind": "path", "workspace": workspace_uri,
                            "resource": "src/two.py", "access": "write"}])]
    # Pure planning can compare host-stable workspace identity without resolving it as a local path.
    plan = BatchPlanner.prepare(steps, _context(steps), _capability())
    assert plan["decision"] == "eligible"


def test_actual_prepare_binds_two_existing_queued_runs_and_builds_verified_f5_contexts(actual_context_env,
                                                                                       monkeypatch):
    from contextlib import closing
    from pmt.efficiency.storage import Phase3Storage
    import pmt.runners.service as runner_service

    env = _queue_two_actual_runs(actual_context_env)
    monkeypatch.setattr(runner_service.shutil, "which", lambda _name: "fixture-codex")
    request = {"protocol_version": 1, "operation": "prepare_step_batch", "request_id": _id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "cli"},
        "payload": {"run_refs": env["batch_runs"], "workspace": str(env["workspace"]),
                    "repository_id": env["repo_id"], "relative_graph_path": env["graph_path"],
                    "expected_source": env["batch_pin"], "event_id": _id()}}
    result, code = LocalStore(env["db"]).execute(request)
    assert code == 0 and result["ok"], result.get("error")
    assert result["result"].get("status") == "prepared", result["result"]
    exact_replay, replay_code = LocalStore(env["db"]).execute(copy.deepcopy(request))
    assert replay_code == 0 and exact_replay == result
    changed_body = copy.deepcopy(request)
    changed_body["payload"]["event_id"] = _id()
    conflict, conflict_code = LocalStore(env["db"]).execute(changed_body)
    assert conflict_code != 0 and conflict["error"]["code"] == "request_conflict"
    binding_ref = result["result"]["batch_ref"]
    assert result["result"]["status"] == "prepared"
    assert result["result"]["physical_slots"] == 1
    assert len(result["result"]["member_run_refs"]) == 2
    with closing(env["db"].connect()) as conn:
        leader, child = [conn.execute("SELECT * FROM execution_runs WHERE id=?", (run["run_id"],)).fetchone()
                         for run in env["batch_runs"]]
        assert leader["state"] == child["state"] == "starting"
        assert conn.execute("SELECT COUNT(*) FROM scope_locks WHERE run_id=?", (leader["id"],)).fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM scope_locks WHERE run_id=?", (child["id"],)).fetchone()[0] == 0
        stored = Phase3Storage(env["db"]).get_object("batch_binding", binding_ref["id"], env["project_id"],
            env["actor"], env["session"], conn=conn)
        assert stored["body"]["status"] == "prepared"
        assert all(item["context_ref"] and item["directive_sha256"] for item in stored["body"]["members"])
        assert stored["body"]["scope_locks_retained"] is True


def test_two_processes_competing_for_same_union_have_one_atomic_winner(actual_context_env, monkeypatch):
    import multiprocessing
    from contextlib import closing
    import pmt.runners.service as runner_service

    env = _queue_two_actual_runs(actual_context_env)
    monkeypatch.setattr(runner_service.shutil, "which", lambda _name: "fixture-codex")
    base = {"protocol_version": 1, "operation": "prepare_step_batch",
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "cli"}, "payload": {"run_refs": env["batch_runs"],
            "workspace": str(env["workspace"]), "repository_id": env["repo_id"],
            "relative_graph_path": env["graph_path"], "expected_source": env["batch_pin"]}}
    requests = []
    for index in range(2):
        request = copy.deepcopy(base)
        request["request_id"] = _id()
        request["payload"]["event_id"] = _id()
        requests.append(request)
    context = multiprocessing.get_context("spawn")
    barrier, output = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_concurrent_prepare_worker,
        args=(str(env["db"].root), str(env["db"].config_root), request, barrier, output))
        for request in requests]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=45)
    try:
        assert all(process.exitcode == 0 for process in processes), [process.exitcode for process in processes]
        results = [output.get(timeout=5) for _ in processes]
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        output.close()
        output.join_thread()
    winners = [(code, envelope) for code, envelope in results
               if code == 0 and envelope.get("ok") and envelope.get("result", {}).get("status") == "prepared"]
    losers = [(code, envelope) for code, envelope in results if (code, envelope) not in winners]
    assert len(winners) == 1, [(code, envelope.get("error"), envelope.get("result")) for code, envelope in results]
    assert len(losers) == 1
    with closing(env["db"].connect()) as conn:
        rows = conn.execute("SELECT id,state,body_json FROM phase3_objects WHERE kind='batch_binding' AND scope_id=?",
                            (env["project_id"],)).fetchall()
        active = [row for row in rows if json.loads(row["body_json"]).get("status") not in
                  {"aborted", "canceled", "complete"}]
        leader = env["batch_runs"][0]["run_id"]
        locks = conn.execute("SELECT run_id,kind,workspace,resource FROM scope_locks WHERE run_id=?",
                             (leader,)).fetchall()
        states = [conn.execute("SELECT state FROM execution_runs WHERE id=?",
                               (item["run_id"],)).fetchone()[0] for item in env["batch_runs"]]
    assert len(active) == 1
    assert len(locks) == 2
    assert states == ["starting", "starting"]


def test_actual_two_queued_steps_with_route_cap_one_consume_one_physical_slot(actual_context_env, monkeypatch):
    from contextlib import closing
    from pmt.efficiency.batch import physical_active_runs
    from pmt.execution.service import _normalize_scopes
    from pmt.service import execute
    from pmt.util import canonical_json, new_id, utc_now
    import pmt.runners.service as runner_service

    env = _queue_two_actual_runs(actual_context_env)
    monkeypatch.setattr(runner_service.shutil, "which", lambda _name: "fixture-codex")
    request = {"protocol_version": 1, "operation": "prepare_step_batch", "request_id": _id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "cli"}, "payload": {"run_refs": env["batch_runs"],
            "workspace": str(env["workspace"]), "repository_id": env["repo_id"],
            "relative_graph_path": env["graph_path"], "expected_source": env["batch_pin"],
            "event_id": _id()}}
    prepared, code = LocalStore(env["db"]).execute(request)
    assert code == 0 and prepared["ok"], prepared.get("error")
    assert prepared["result"]["status"] == "prepared"
    child_dispatch, child_dispatch_code = execute(env["db"], {**request,
        "operation": "dispatch_execution", "request_id": _id(),
        "payload": {"run_id": env["batch_child_run_id"]}})
    assert child_dispatch_code != 0
    assert child_dispatch["error"]["code"] == "batch_child_dispatch_denied", json.dumps(child_dispatch, sort_keys=True)
    route = env["batch_route"]
    step_id, job_id, run_id, now = new_id(), new_id(), new_id(), utc_now()
    directive_body = {"directive_id": env["directive_id"], "directive_version": 1,
                      "invalidated": False, "kind_tag": "test"}
    scope_set = [{"kind": "path", "workspace": str(env["workspace"]), "resource": "src/f9-third.py"}]
    scopes = _normalize_scopes(scope_set, str(env["workspace"]))
    intent = {"job_id": job_id, "run_id": run_id, "step_id": step_id,
        "scope_id": env["project_id"], "directive_ref": {"artifact_id": env["directive_id"], "version": 1},
        "requirements_version": "requirements-v1", "plan_version": "plan-v1", "plan_id": None,
        "role": "lower", "product_stage": "prototype", "workspace": str(env["workspace"]),
        "scopes": scopes, "criteria": env["criteria"], "dependencies": [], "route": route,
        "policy": {"max_retries": 2}, "directive_version": 1,
        "task": {"task_id": env["item_id"], "step_id": step_id}}
    with env["db"].write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (step_id, "step", env["project_id"], env["item_id"], "Third queued Step",
                      "InProgress", canonical_json(directive_body), 1, now, now))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (step_id, env["directive_id"], 1, "requirements-v1", "plan-v1", None,
                      "lower", "prototype", str(env["workspace"]), canonical_json(scope_set),
                      canonical_json(env["criteria"]), "[]", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?, 'queued',?,?,?)",
                     (job_id, step_id, canonical_json({"max_retries": 2}), now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,1,'queued',1,?,1,?,?,?,?,?,?)",
                     (run_id, job_id, step_id, env["session"], str(env["workspace"]),
                      canonical_json(scopes), canonical_json(route), canonical_json(intent), now, now))
        active_slots = physical_active_runs(conn, {"starting", "running", "reconciling", "cancel_requested"},
                                            agent=route["agent"], executor=route["adapter_kind"])
        assert len(active_slots) == 1
        revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (run_id,)).fetchone()[0]
    queued, queued_code = execute(env["db"], {"protocol_version": 1, "operation": "prepare_execution",
        "request_id": _id(), "actor": env["actor"], "session_id": env["session"],
        "scope_id": env["project_id"], "payload": {"run_id": run_id,
        "expected_run_revision": revision}})
    assert queued_code == 0 and queued["result"]["state"] == "queued"
    assert queued["result"]["waiting_reason"] == "route_capacity"
    with closing(env["db"].connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scope_locks WHERE run_id=?", (run_id,)).fetchone()[0] == 0


def test_actual_pre_dispatch_group_cancel_cancels_existing_runs_and_releases_union(actual_context_env,
                                                                                   monkeypatch):
    from contextlib import closing
    from pmt.phase2 import execute
    import pmt.runners.service as runner_service

    env = _queue_two_actual_runs(actual_context_env)
    monkeypatch.setattr(runner_service.shutil, "which", lambda _name: "fixture-codex")
    request = {"protocol_version": 1, "operation": "prepare_step_batch", "request_id": _id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "cli"}, "payload": {"run_refs": env["batch_runs"],
            "workspace": str(env["workspace"]), "repository_id": env["repo_id"],
            "relative_graph_path": env["graph_path"], "expected_source": env["batch_pin"],
            "event_id": _id()}}
    prepared, code = LocalStore(env["db"]).execute(request)
    assert code == 0 and prepared["ok"], prepared.get("error")
    parent_id = env["batch_runs"][0]["run_id"]
    with closing(env["db"].connect()) as conn:
        revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (parent_id,)).fetchone()[0]
    canceled, code = execute(env["db"], {**request, "operation": "request_execution_cancel",
        "request_id": _id(), "payload": {"run_id": parent_id, "expected_run_revision": revision,
                                            "event_id": _id()}})
    assert code == 0 and canceled["ok"], canceled.get("error")
    assert canceled["result"]["state"] == "canceled"
    assert canceled["result"]["batch"]["status"] == "canceled"
    with closing(env["db"].connect()) as conn:
        states = [conn.execute("SELECT state FROM execution_runs WHERE id=?", (item["run_id"],)).fetchone()[0]
                  for item in env["batch_runs"]]
        assert states == ["canceled", "canceled"]
        assert conn.execute("SELECT COUNT(*) FROM scope_locks WHERE run_id=?", (parent_id,)).fetchone()[0] == 0


def test_local_and_host_queue_reads_only_selected_project_descendants(tmp_path):
    from pmt.db import Database
    from pmt.host.application import HostApplication
    from pmt.phase2 import execute as phase2_execute
    from pmt.util import canonical_json, utc_now

    db = Database(tmp_path / "data", tmp_path / "config")
    app = HostApplication(db, {"fixture": b"queue-scope-fixture-key-32-bytes"}, "fixture")
    bootstrap = app.auth.issue_device("main", ["*"], ["read", "write", "runtime", "review", "admin"])
    root_headers = {"authorization": "Bearer " + bootstrap["credential"],
        "x-pmt-device": bootstrap["device_id"], "x-pmt-environment": _id(),
        "x-pmt-namespace": app.auth.namespace_id, "x-pmt-session": "queue-admin"}
    app.register_session(root_headers, {"session_id": "queue-admin",
                                        "environment_id": root_headers["x-pmt-environment"]})

    def host_request(operation, scope_id=None, payload=None, session="queue-admin"):
        value = {"protocol_version": 1, "request_id": _id(), "operation": operation,
            "actor": "main", "session_id": session, "payload": payload or {}}
        if scope_id is not None:
            value["scope_id"] = scope_id
        return value

    def host_call(request, headers):
        envelope, code = app.execute(request, headers)
        assert code == 0 and envelope["ok"], envelope.get("error")
        return envelope["result"]

    left = host_call(host_request("create_scope", payload={"kind": "project", "slug": "left"}), root_headers)
    right = host_call(host_request("create_scope", payload={"kind": "project", "slug": "right"}), root_headers)
    classification_id = _id()
    run_ids = {}
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) "
                     "VALUES(?,'classification',?,'nested','{}',?,?)",
                     (classification_id, left["id"], now, now))
        for scope_id, record_scope_id in ((left["id"], classification_id), (right["id"], right["id"])):
            step_id, job_id, run_id = _id(), _id(), _id()
            run_ids[scope_id] = run_id
            conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,created_at,updated_at) "
                         "VALUES(?,'step',?,'Queue visibility fixture','Planned','{}',?,?)",
                         (step_id, record_scope_id, now, now))
            conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) "
                         "VALUES(?,?,'queued','{}',?,?)", (job_id, step_id, now, now))
            intent = {"scope_id": scope_id, "step_id": step_id, "job_id": job_id,
                      "run_id": run_id, "scopes": [], "criteria": []}
            conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,"
                         "directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) "
                         "VALUES(?,?,?,1,'queued',1,'queue-admin',1,'C:/fixture','[]','{}',?,?,?)",
                         (run_id, job_id, step_id, canonical_json(intent), now, now))

    selected = root_headers.copy()
    granted = app.auth.issue_device("main", [left["id"]], ["read"])
    selected.update({"authorization": "Bearer " + granted["credential"],
        "x-pmt-device": granted["device_id"], "x-pmt-environment": _id(),
        "x-pmt-session": "queue-reader"})
    app.register_session(selected, {"session_id": "queue-reader",
                                    "environment_id": selected["x-pmt-environment"]})
    host_result = host_call(host_request("list_execution_queue", left["id"], {"state": "queued"},
                                         session="queue-reader"), selected)
    local_request = {"protocol_version": 1, "operation": "list_execution_queue", "request_id": _id(),
        "actor": "main", "session_id": "queue-reader", "scope_id": left["id"],
        "payload": {"state": "queued"}}
    local_result, local_code = phase2_execute(db, local_request)
    assert local_code == 0 and local_result["ok"], local_result.get("error")
    assert host_result["count"] == local_result["result"]["count"] == 1
    assert [item["run_id"] for item in host_result["items"]] == [run_ids[left["id"]]]
    assert [item["run_id"] for item in local_result["result"]["items"]] == [run_ids[left["id"]]]
    assert run_ids[right["id"]] not in {item["run_id"] for item in host_result["items"]}


@pytest.mark.parametrize("reported_count", [2, 1])
def test_actual_native_group_handle_and_structured_report_collect_per_child(actual_context_env, reported_count):
    from contextlib import closing
    from pmt.phase2 import execute
    from pmt.phase2_common import persist_json_resource
    from pmt.util import canonical_json

    env = _queue_two_actual_runs(actual_context_env)
    native_route = {**env["batch_route"], "mode": "native", "adapter_kind": "native",
                    "model": "local-native-fixture", "actual_support": "verified_supported"}
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (canonical_json({"workspace": str(env["workspace"]), "criteria": []}), env["item_id"]))
        for ref in env["batch_runs"]:
            row = conn.execute("SELECT intent_json FROM execution_runs WHERE id=?", (ref["run_id"],)).fetchone()
            intent = json.loads(row["intent_json"])
            intent["route"] = native_route
            conn.execute("UPDATE execution_runs SET route_json=?,intent_json=? WHERE id=?",
                         (canonical_json(native_route), canonical_json(intent), ref["run_id"]))
            step_id = conn.execute("SELECT step_id FROM execution_runs WHERE id=?", (ref["run_id"],)).fetchone()[0]
            step_body = json.loads(conn.execute("SELECT body_json FROM records WHERE id=?", (step_id,)).fetchone()[0])
            step_body.update({"workspace": str(env["workspace"]), "criteria": env["criteria"]})
            conn.execute("UPDATE records SET body_json=? WHERE id=?", (canonical_json(step_body), step_id))
    prepare = {"protocol_version": 1, "operation": "prepare_step_batch", "request_id": _id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "codex"}, "payload": {"run_refs": env["batch_runs"],
            "workspace": str(env["workspace"]), "repository_id": env["repo_id"],
            "relative_graph_path": env["graph_path"], "expected_source": env["batch_pin"],
            "event_id": _id()}}
    prepared, code = LocalStore(env["db"]).execute(prepare)
    assert code == 0 and prepared["ok"], prepared.get("error")
    batch_ref = prepared["result"]["batch_ref"]["id"]
    parent_id = env["batch_runs"][0]["run_id"]
    with closing(env["db"].connect()) as conn:
        binding_row = conn.execute("SELECT body_json FROM phase3_objects WHERE id=?", (batch_ref,)).fetchone()
    binding = json.loads(binding_row[0])
    dispatch = {"protocol_version": 1, "operation": "dispatch_execution", "request_id": _id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "codex"}, "payload": {"run_id": parent_id,
            "context_ref": binding["members"][0]["context_ref"]}}
    for operation, error_code in (("dispatch_execution", "batch_child_dispatch_denied"),
                                  ("poll_execution", "batch_child_poll_denied"),
                                  ("cancel_runner", "batch_child_cancel_denied")):
        denied, denied_code = execute(env["db"], {**dispatch, "operation": operation,
            "request_id": _id(), "payload": {"run_id": env["batch_child_run_id"]}})
        assert denied_code != 0 and denied["error"]["code"] == error_code
    action, code = execute(env["db"], dispatch)
    assert code == 0 and action["ok"], action.get("error")
    main_action = action["result"]["main_action"]
    assert main_action["batch_ref"] == batch_ref
    assert len(main_action["members"]) == 2
    handle = {"id": "local-native-batch-fixture", "native_subagent_id": "fixture-child-process"}
    with closing(env["db"].connect()) as conn:
        revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (parent_id,)).fetchone()[0]
    attached, code = execute(env["db"], {**dispatch, "operation": "attach_execution_handle",
        "request_id": _id(), "payload": {"run_id": parent_id, "expected_run_revision": revision,
                                          "handle": handle}})
    assert code == 0 and attached["ok"], attached.get("error")
    assert attached["result"]["state"] == "running"

    stop_evidence = persist_json_resource(env["db"], {**dispatch, "request_id": _id(), "payload": {}},
        {"fixture": "native-handle-stop-confirmation"}, env["project_id"], "batch_stop_fixture", parent_id)
    reports = []
    for member in binding["members"][:reported_count]:
        evidence = persist_json_resource(env["db"], {**dispatch, "request_id": _id(), "payload": {}},
            {"fixture": "criterion-evidence", "step_id": member["step_id"]},
            env["project_id"], "batch_criterion_fixture", member["run_id"])
        reports.append({"step_id": member["step_id"], "run_id": member["run_id"],
            "directive_sha256": member["directive_sha256"], "context_ref": member["context_ref"],
            "summary": "Local fixture result; not model quality evidence.", "choices": [],
            "criteria_results": [{"criterion_id": criterion["id"], "outcome": "pass", "reason": None,
                                  "evidence_refs": [evidence["artifact_id"]]}
                                 for criterion in member["criteria"]],
            "tests": [], "evidence_refs": [evidence["artifact_id"]], "unresolved_items": []})
    report = {"schema": "pmt-batch-report-v1", "batch_id": batch_ref, "steps": reports}
    report_artifact = persist_json_resource(env["db"], {**dispatch, "request_id": _id(), "payload": {}},
        report, env["project_id"], "batch_runner_report_fixture", parent_id)
    with closing(env["db"].connect()) as conn:
        parent = conn.execute("SELECT revision,route_json FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
        parent_route = json.loads(parent["route_json"])
    parent_result = {"directive_version": 1, "actual_route": parent_route,
        "summary": "Local structured fixture only; no model was invoked.",
        "criteria_results": [{"criterion_id": criterion["id"], "outcome": "not_run",
                               "reason": "Group report is per child", "evidence_refs": []}
                              for criterion in env["criteria"]],
        "receipt_ref": stop_evidence["artifact_id"], "evidence_refs": [stop_evidence["artifact_id"]],
        "stop_confirmed": True, "stop_evidence_refs": [stop_evidence["artifact_id"]],
        "runner_observation": {"exit_code": 0, "fixture": True},
        "batch_ref": batch_ref, "batch_report_ref": report_artifact["artifact_id"],
        "batch_report_sha256": report_artifact["sha256"]}
    submitted, code = execute(env["db"], {**dispatch, "operation": "submit_execution_result",
        "request_id": _id(), "payload": {"run_id": parent_id,
            "expected_run_revision": parent["revision"], "result": parent_result}})
    assert code == 0 and submitted["ok"], submitted.get("error")
    assert submitted["result"]["state"] == "review_pending"
    with closing(env["db"].connect()) as conn:
        parent = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
    collected, code = LocalStore(env["db"]).execute({**dispatch, "operation": "collect_step_batch",
        "request_id": _id(), "payload": {"batch_ref": batch_ref, "parent_run_id": parent_id,
            "expected_run_revision": parent["revision"], "event_id": _id()}})
    assert code == 0 and collected["ok"], json.dumps(collected.get("error"), sort_keys=True)
    expected_status = "review_pending" if reported_count == 2 else "reconciling"
    assert collected["result"]["status"] == expected_status
    assert collected["result"]["parent_done"] is False
    assert [child["state"] for child in collected["result"]["children"]] == (
        ["review_pending", "review_pending"] if reported_count == 2 else ["review_pending", "reconciling"])
    with closing(env["db"].connect()) as conn:
        child_states = [conn.execute("SELECT state FROM execution_runs WHERE id=?", (ref["run_id"],)).fetchone()[0]
                        for ref in env["batch_runs"]]
        assert child_states == (["review_pending", "review_pending"] if reported_count == 2
                                else ["review_pending", "reconciling"])
        assert conn.execute("SELECT COUNT(*) FROM scope_locks WHERE run_id=?", (parent_id,)).fetchone()[0] == 2
        parent = conn.execute("SELECT revision,result_json FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
        saved_leader_result = json.loads(parent["result_json"])
        assert saved_leader_result["batch_report_ref"] == report_artifact["artifact_id"]
        assert saved_leader_result["physical_parent_receipt_ref"] == stop_evidence["artifact_id"]
    replayed, code = LocalStore(env["db"]).execute({**dispatch, "operation": "collect_step_batch",
        "request_id": _id(), "payload": {"batch_ref": batch_ref, "parent_run_id": parent_id,
            "expected_run_revision": parent["revision"], "event_id": _id()}})
    assert code == 0 and replayed["ok"], replayed.get("error")
    assert replayed["result"]["status"] == expected_status
    assert replayed["result"]["parent_done"] is False
    original_result = None
    other_report = copy.deepcopy(report)
    other_report["steps"][0]["summary"] += " changed after collection"
    other_artifact = persist_json_resource(env["db"], {**dispatch, "request_id": _id(), "payload": {}},
        other_report, env["project_id"], "batch_conflicting_report_fixture", parent_id)
    with env["db"].write() as conn:
        parent = conn.execute("SELECT revision,result_json FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
        original_result = json.loads(parent["result_json"])
        conflicting = {**original_result, "batch_report_ref": other_artifact["artifact_id"],
                       "batch_report_sha256": other_artifact["sha256"]}
        conn.execute("UPDATE execution_runs SET result_json=?,revision=revision+1 WHERE id=?",
                     (canonical_json(conflicting), parent_id))
    with closing(env["db"].connect()) as conn:
        current_revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (parent_id,)).fetchone()[0]
    different_report, code = LocalStore(env["db"]).execute({**dispatch, "operation": "collect_step_batch",
        "request_id": _id(), "payload": {"batch_ref": batch_ref, "parent_run_id": parent_id,
            "expected_run_revision": current_revision, "event_id": _id()}})
    assert code != 0 and different_report["error"]["code"] == "batch_report_conflict"
    with env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET result_json=?,revision=revision+1 WHERE id=?",
                     (canonical_json(original_result), parent_id))
    if reported_count == 2:
        for index, member in enumerate(binding["members"]):
            with closing(env["db"].connect()) as conn:
                result_body = json.loads(conn.execute("SELECT result_json FROM execution_runs WHERE id=?",
                    (member["run_id"],)).fetchone()[0])
            step_result = _verify_and_review_batch_member(env, member,
                [reports[index]["evidence_refs"][0], result_body["receipt_ref"]])
            assert step_result["state"] == "Done"
            with closing(env["db"].connect()) as conn:
                locks = conn.execute("SELECT COUNT(*) FROM scope_locks WHERE run_id=?", (parent_id,)).fetchone()[0]
                binding_body = json.loads(conn.execute("SELECT body_json FROM phase3_objects WHERE id=?",
                                                       (batch_ref,)).fetchone()[0])
            assert locks == (2 if index == 0 else 0)
            assert binding_body["status"] == ("review_pending" if index == 0 else "complete")
        with closing(env["db"].connect()) as conn:
            assert conn.execute("SELECT state FROM records WHERE id=?", (env["item_id"],)).fetchone()[0] == "InProgress"
    else:
        leader = binding["members"][0]
        with closing(env["db"].connect()) as conn:
            leader_result = json.loads(conn.execute("SELECT result_json FROM execution_runs WHERE id=?",
                                                    (leader["run_id"],)).fetchone()[0])
        reviewed = _verify_and_review_batch_member(env, leader,
            [reports[0]["evidence_refs"][0], leader_result["receipt_ref"]])
        assert reviewed["state"] == "Done"
        with closing(env["db"].connect()) as conn:
            remaining = conn.execute("SELECT state FROM execution_runs WHERE id=?",
                                     (env["batch_child_run_id"],)).fetchone()[0]
            locks = conn.execute("SELECT COUNT(*) FROM scope_locks WHERE run_id=?", (parent_id,)).fetchone()[0]
            binding_body = json.loads(conn.execute("SELECT body_json FROM phase3_objects WHERE id=?",
                                                   (batch_ref,)).fetchone()[0])
        assert remaining == "reconciling"
        assert locks == 2 and binding_body["status"] == "reconciling"


def test_actual_cli_group_runs_one_supervisor_and_collects_child_mapping(actual_context_env, monkeypatch):
    import sys
    import time
    from contextlib import closing
    from pmt.efficiency.local_runtime import LocalExecutionRuntime
    from pmt.phase2 import execute
    from pmt.phase2_common import persist_json_resource
    from pmt.util import canonical_json
    import pmt.runners.service as runner_service

    env = _queue_two_actual_runs(actual_context_env)
    monkeypatch.setattr(runner_service.shutil, "which", lambda _name: sys.executable)
    prepare = {"protocol_version": 1, "operation": "prepare_step_batch", "request_id": _id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "codex"}, "payload": {"run_refs": env["batch_runs"],
            "workspace": str(env["workspace"]), "repository_id": env["repo_id"],
            "relative_graph_path": env["graph_path"], "expected_source": env["batch_pin"],
            "event_id": _id()}}
    prepared, code = LocalStore(env["db"]).execute(prepare)
    assert code == 0 and prepared["ok"], prepared.get("error")
    batch_ref = prepared["result"]["batch_ref"]["id"]
    parent_id = env["batch_runs"][0]["run_id"]
    with closing(env["db"].connect()) as conn:
        binding = json.loads(conn.execute("SELECT body_json FROM phase3_objects WHERE id=?",
                                          (batch_ref,)).fetchone()[0])
    narrow = LocalExecutionRuntime._request({"request_id": _id(), "payload": {
        "run_id": parent_id, "context_ref": binding["members"][0]["context_ref"],
        "batch_ref": "caller-forgery", "members": [{"step_id": "caller-forgery"}]}},
        "dispatch_execution")
    assert set(narrow["payload"]) == {"run_id", "context_ref"}
    assert narrow["payload"]["run_id"] == parent_id
    reports = []
    for member in binding["members"]:
        evidence = persist_json_resource(env["db"], {**prepare, "request_id": _id(), "payload": {}},
            {"fixture": "local CLI child evidence", "step_id": member["step_id"]},
            env["project_id"], "batch_cli_fixture_evidence", member["run_id"])
        reports.append({"step_id": member["step_id"], "run_id": member["run_id"],
            "directive_sha256": member["directive_sha256"], "context_ref": member["context_ref"],
            "summary": "Python CLI fixture output; not model quality evidence.", "choices": [],
            "criteria_results": [{"criterion_id": item["id"], "outcome": "pass", "reason": None,
                                  "evidence_refs": [evidence["artifact_id"]]} for item in member["criteria"]],
            "tests": [], "evidence_refs": [evidence["artifact_id"]], "unresolved_items": []})
    report = {"schema": "pmt-batch-report-v1", "batch_id": batch_ref, "steps": reports}
    event_text = canonical_json(report)
    event_line = json.dumps({"type": "response.output_text.done", "text": event_text})
    command_script = f"import sys; sys.stdout.write({event_line!r})"
    monkeypatch.setattr(runner_service, "_command", lambda _route: ("codex", ["-c", command_script]))
    write_config = runner_service._write_private_json
    def write_group_fixture(path, body):
        if path.name == "config.json":
            body = {**body, "contract_fixture": True, "fixture_process": True,
                    "fixture_stdout": event_line, "fixture_stderr": "", "fixture_delay": 0.05}
        return write_config(path, body)
    monkeypatch.setattr(runner_service, "_write_private_json", write_group_fixture)
    dispatch = {"protocol_version": 1, "operation": "dispatch_execution", "request_id": _id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "codex"}, "payload": {"run_id": parent_id,
            "context_ref": binding["members"][0]["context_ref"]}}
    launched, code = execute(env["db"], dispatch)
    assert code == 0 and launched["ok"], launched.get("error")
    assert launched["result"]["state"] in {"starting", "running"}
    started = time.monotonic()
    observed = None
    while time.monotonic() - started < 20:
        observed, code = execute(env["db"], {**dispatch, "operation": "poll_execution",
            "request_id": _id(), "payload": {"run_id": parent_id}})
        assert code == 0 and observed["ok"], observed.get("error")
        if observed["result"].get("state") in {"review_pending", "reconciling", "failed", "blocked"}:
            break
        time.sleep(0.15)
    if not observed or observed["result"].get("state") != "review_pending":
        with closing(env["db"].connect()) as conn:
            run_row = conn.execute("SELECT result_json FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
            journal_row = conn.execute("SELECT body_json FROM operation_journal WHERE id=?",
                                       (str(uuid.uuid5(uuid.UUID(parent_id), "pmt-runner-dispatch")),)).fetchone()
        assert False, json.dumps({"observed": observed, "run_result": run_row[0] if run_row else None,
                                  "journal": journal_row[0] if journal_row else None}, sort_keys=True)
    collection = observed["result"].get("batch_collection")
    assert collection and collection["status"] == "review_pending", collection
    assert collection["parent_done"] is False
    assert all(child["state"] == "review_pending" for child in collection["children"])
    assert observed["result"].get("report_ref")
    with closing(env["db"].connect()) as conn:
        parent = conn.execute("SELECT result_json FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
        result = json.loads(parent["result_json"])
        assert result["batch_report_ref"] == observed["result"]["report_ref"]
        assert result["runner_observation"]["fixture"] is True
        assert conn.execute("SELECT COUNT(*) FROM scope_locks WHERE run_id=?", (parent_id,)).fetchone()[0] == 2


def test_actual_f8_native_action_carries_prepared_f9_group_prompt_and_all_children(actual_context_env):
    import hashlib
    from contextlib import closing
    from pmt.efficiency.storage import Phase3Storage
    from pmt.service import execute as service_execute
    from pmt.util import canonical_json
    from pmt.store import LocalStore
    from test_phase3_control import _reuse_ref

    env = _queue_two_actual_runs(actual_context_env)
    native_route = {"agent": "cli", "provider": "fixture-native", "model": "local-fixture",
        "mode": "native", "selection_reason": "fixture capability",
        "actual_support": "verified_supported", "auth_state": "authenticated",
        "capability_ref": "fixture-native-capability", "max_concurrency": 1}
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (canonical_json({"workspace": str(env["workspace"]), "criteria": []}), env["item_id"]))
        for ref in env["batch_runs"]:
            row = conn.execute("SELECT intent_json FROM execution_runs WHERE id=?", (ref["run_id"],)).fetchone()
            intent = json.loads(row["intent_json"])
            intent["route"] = native_route
            conn.execute("UPDATE execution_runs SET route_json=?,intent_json=? WHERE id=?",
                         (canonical_json(native_route), canonical_json(intent), ref["run_id"]))
    prepare = {"protocol_version": 1, "operation": "prepare_step_batch", "request_id": _id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "cli"}, "payload": {"run_refs": env["batch_runs"],
            "workspace": str(env["workspace"]), "repository_id": env["repo_id"],
            "relative_graph_path": env["graph_path"], "expected_source": env["batch_pin"],
            "event_id": _id()}}
    prepared, code = LocalStore(env["db"]).execute(prepare)
    assert code == 0 and prepared["ok"], prepared.get("error")
    batch_ref = prepared["result"]["batch_ref"]["id"]
    parent_id = env["batch_runs"][0]["run_id"]
    with closing(env["db"].connect()) as conn:
        binding = json.loads(conn.execute("SELECT body_json FROM phase3_objects WHERE id=?",
                                          (batch_ref,)).fetchone()[0])
    reuse_ref = _reuse_ref(env, env["batch_pin"])
    advance = {"protocol_version": 1, "operation": "advance_execution_control", "request_id": _id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "cli"}, "payload": {"run_id": parent_id,
            "context_ref": binding["members"][0]["context_ref"], "reuse_body_ref": reuse_ref}}
    started, code = service_execute(env["db"], advance)
    assert code == 0 and started["ok"], started.get("error")
    action = started["result"]["action"]
    assert action["kind"] == "main-native-call", action
    assert action["batch_ref"] == batch_ref
    assert action["batch_report_schema"] == "pmt-batch-report-v1"
    assert action["physical_slots"] == 1
    assert action["source_hash"] == env["batch_pin"]["source_hash"]
    assert action["scope_union_sha256"] == binding["scope_union_sha256"]
    assert [item["step_id"] for item in action["members"]] == env["batch_step_ids"]
    assert [item["run_id"] for item in action["members"]] == [item["run_id"] for item in env["batch_runs"]]
    assert action["group_context_refs"] == [item["context_ref"] for item in action["members"]]
    assert all(item["directive_ref"] and item["directive_sha256"] and item["criteria"]
               and item["role"] == "lower" for item in action["members"])
    assert all(item in action["group_prompt"] for item in env["batch_step_ids"])
    assert all(item["id"] in action["group_prompt"] for item in action["group_context_refs"])
    assert '"section_id":"method"' in action["group_prompt"]
    assert '"section_id":"autonomy"' in action["group_prompt"]
    assert '"section_id":"criteria"' in action["group_prompt"]
    accounting = action["group_prompt_accounting"]
    assert accounting["member_f5_content_bytes"] <= accounting["shared_f5_content_budget_bytes"]
    assert accounting["group_prompt_bytes"] == len(action["group_prompt"].encode("utf-8"))
    assert hashlib.sha256(action["group_prompt"].encode("utf-8")).hexdigest() == action["group_prompt_sha256"]
    with closing(env["db"].connect()) as conn:
        stored = Phase3Storage(env["db"]).get_object("execution_control", parent_id,
            env["project_id"], env["actor"], env["session"], conn=conn)
        assert stored["body"]["action"] == action
        assert stored["body"]["stage"] == "main_action_pending"
    ack, code = service_execute(env["db"], {**advance, "operation": "acknowledge_execution_action",
        "request_id": _id(), "payload": {"run_id": parent_id,
            "control_ref": started["result"]["control_ref"], "action_nonce": action["action_nonce"],
            "expected_run_revision": action["expected_run_revision"], "outcome": "started",
            "handle_ref": {"kind": "native_handle", "id": "f10-native-group-fixture",
                            "provider_ref": "fixture-native-parent-handle"}}})
    assert code == 0 and ack["ok"], ack.get("error")
    with closing(env["db"].connect()) as conn:
        binding_after = json.loads(conn.execute("SELECT body_json FROM phase3_objects WHERE id=?",
                                                (batch_ref,)).fetchone()[0])
        child = conn.execute("SELECT state,handle_json FROM execution_runs WHERE id=?",
                             (env["batch_child_run_id"],)).fetchone()
    assert binding_after["status"] == "running"
    assert child["state"] == "running"
    assert json.loads(child["handle_json"])["id"] == "f10-native-group-fixture"


def test_f9_s2_bind_requires_exact_members_one_slot_and_scope_union_receipt():
    plan = _eligible_plan()
    binding = _binding(plan)
    assert binding["schema"] == BINDING_SCHEMA
    assert binding["physical_slots"] == 1 and binding["execution_enabled"] is False
    assert binding["terminal_parent_promotion"] is False
    assert len(binding["members"]) == len(plan["members"])
    with pytest.raises(PmtError, match="BatchPlan"):
        BatchPlanner.bind({**plan, "manifest_sha256": _sha("0")}, {}, [])
    with pytest.raises(PmtError, match="scope authority"):
        _binding(plan, scope_union_sha256=_sha("2"))
    with pytest.raises(PmtError, match="representative"):
        _binding(plan, parent_run_ref=_id())
    with pytest.raises(PmtError, match="cover every"):
        _binding(plan, children=[])


def test_f9_s3_collect_preserves_per_step_reports_without_done_promotion():
    plan = _eligible_plan()
    binding = _binding(plan)
    first, second = binding["members"]
    reports = [_result(binding, first["step_id"], first["run_id"]),
               _result(binding, second["step_id"], second["run_id"], state="failed",
                       criteria=[{"id": "criteria:one", "sha256": _sha("b"), "outcome": "fail",
                                  "evidence_refs": ["artifact:failure"], "reason": "criterion failed"}],
                       receipt="receipt:two")]
    collected = BatchPlanner.collect(binding, reports)
    assert collected["parent_done"] is False
    assert collected["execution_enabled"] is False
    assert collected["aggregate_status"] == "results_unverified"
    assert {item["reported_state"] for item in collected["children"]} == {"succeeded", "failed"}
    assert all(item["validation_status"] == "receipt_pending_authoritative_check" for item in collected["children"])
    assert len(collected["children"]) == 2


def test_f9_s3_missing_cancelled_opaque_or_malformed_results_remain_unknown():
    plan = _eligible_plan()
    binding = _binding(plan)
    one = binding["members"][0]
    partial = BatchPlanner.collect(binding, [_result(binding, one["step_id"], one["run_id"])],
                                   cancel_observed=True)
    missing = next(item for item in partial["children"] if item["step_id"] != one["step_id"])
    assert partial["aggregate_status"] == "unknown"
    assert missing["reported_state"] == "unknown"
    assert missing["reason"] == "group_cancelled_missing_result"

    bad_criteria = [{"id": "criteria:other", "sha256": _sha("b"), "outcome": "pass",
                     "evidence_refs": ["artifact:evidence"], "reason": None}]
    with pytest.raises(PmtError, match="planned criteria"):
        BatchPlanner.collect(binding, [_result(binding, one["step_id"], one["run_id"],
                                               criteria=bad_criteria)])
    opaque = _result(binding, one["step_id"], one["run_id"], state="unknown", receipt=None)
    only = BatchPlanner.collect(binding, [opaque])
    assert next(item for item in only["children"] if item["step_id"] == one["step_id"])["validation_status"] == "receipt_missing"
