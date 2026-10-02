"""Actual Host F9 batch state reuse and private metadata projection tests."""
from __future__ import annotations

from contextlib import closing
import json
import uuid

import pytest
pytest_plugins = ["test_phase3_context"]

from pmt.db import Database
from pmt.errors import PmtError
from pmt.host.application import HostApplication
from pmt.host.data import HostDataExtension
from pmt.host.resources import HostResourceStore
from pmt.util import canonical_json, new_id, utc_now
from pmt.workspace import canonical_workspace


def _host_fixture(env, monkeypatch):
    from pmt.efficiency.source import pin_source
    from pmt.execution.service import _normalize_scopes
    from test_phase3_context import _actual_f3_ready
    import pmt.runners.service as runner_service

    monkeypatch.setattr(runner_service.shutil, "which", lambda _name: "fixture-unused-for-native")
    pin = pin_source(_actual_f3_ready(env))
    branch = pin.selected_ref if pin.selected_ref is not None else "detached:" + pin.reviewed_commit
    workspace = canonical_workspace(env["repo_id"], branch)
    route = {"agent": "codex", "provider": "fixture", "model": "fixture-native",
        "mode": "native", "adapter_kind": "native", "selection_reason": "isolated Host batch fixture",
        "actual_support": "verified_supported", "auth_state": "authenticated",
        "capability_ref": "fixture-native-batch-v1", "max_concurrency": 1}
    first_run_id = env["run_id"]
    first_scope = [{"kind": "path", "workspace": str(env["workspace"]),
                    "resource": env["graph_path"]}]
    first_scopes = _normalize_scopes(first_scope, str(env["workspace"]))
    now = utc_now()
    with env["db"].write() as conn:
        run = conn.execute("SELECT * FROM execution_runs WHERE id=?", (first_run_id,)).fetchone()
        first_intent = json.loads(run["intent_json"])
        first_intent.update({"job_id": run["job_id"], "run_id": first_run_id,
            "step_id": env["step_id"], "scope_id": env["project_id"],
            "requirements_version": "requirements-v1", "plan_version": "plan-v1", "plan_id": None,
            "role": "lower", "product_stage": "prototype", "workspace": str(env["workspace"]),
            "scopes": first_scopes, "criteria": env["criteria"], "dependencies": [], "route": route,
            "policy": {"max_retries": 2}, "directive_ref": {"artifact_id": env["directive_id"], "version": 1},
            "directive_version": 1, "task": {"task_id": env["item_id"], "step_id": env["step_id"]}})
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (first_run_id,))
        conn.execute("UPDATE execution_runs SET state='queued',revision=revision+1,handle_json=NULL,result_json=NULL,"
            "stop_confirmed=0,started_at=NULL,completed_at=NULL,route_json=?,intent_json=?,scopes_json=?,updated_at=? WHERE id=?",
            (canonical_json(route), canonical_json(first_intent), canonical_json(first_scopes), now, first_run_id))
        conn.execute("UPDATE execution_jobs SET state='queued',updated_at=? WHERE id=?", (now, run["job_id"]))
        conn.execute("UPDATE step_specs SET workspace=?,scopes_json=? WHERE step_id=?",
            (workspace, canonical_json([dict(item, workspace=workspace) for item in first_scope]), env["step_id"]))
        first_workspace_scopes = [dict(item, workspace=workspace) for item in first_scopes]
        first_intent.update(workspace=workspace, scopes=first_workspace_scopes)
        conn.execute("UPDATE execution_runs SET workspace=?,scopes_json=?,intent_json=? WHERE id=?",
            (workspace, canonical_json(first_workspace_scopes), canonical_json(first_intent), first_run_id))

        child_step, child_job, child_run = new_id(), new_id(), new_id()
        child_scope = [{"kind": "path", "workspace": str(env["workspace"]), "resource": "src/f9-child.py"}]
        child_scopes = _normalize_scopes(child_scope, str(env["workspace"]))
        child_body = {"directive_id": env["directive_id"], "directive_version": 1,
            "invalidated": False, "kind_tag": "test", "workspace": str(env["workspace"]),
            "criteria": env["criteria"]}
        child_intent = {"job_id": child_job, "run_id": child_run, "step_id": child_step,
            "scope_id": env["project_id"], "directive_ref": {"artifact_id": env["directive_id"], "version": 1},
            "requirements_version": "requirements-v1", "plan_version": "plan-v1", "plan_id": None,
            "role": "lower", "product_stage": "prototype", "workspace": str(env["workspace"]),
            "scopes": child_scopes, "criteria": env["criteria"], "dependencies": [], "route": route,
            "policy": {"max_retries": 2}, "directive_version": 1,
            "task": {"task_id": env["item_id"], "step_id": child_step}}
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (child_step, "step", env["project_id"], env["item_id"], "Host batch child Step", "InProgress",
             canonical_json(child_body), 1, now, now))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (child_step, env["directive_id"], 1, "requirements-v1", "plan-v1", None, "lower", "prototype",
             workspace, canonical_json([dict(item, workspace=workspace) for item in child_scope]),
             canonical_json(env["criteria"]), "[]", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?, 'queued',?,?,?)",
            (child_job, child_step, canonical_json({"max_retries": 2}), now, now))
        child_workspace_scopes = [dict(item, workspace=workspace) for item in child_scopes]
        child_intent.update(workspace=workspace, scopes=child_workspace_scopes)
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,1,'queued',1,?,1,?,?,?,?,?,?)",
            (child_run, child_job, child_step, env["session"], workspace, canonical_json(child_workspace_scopes),
             canonical_json(route), canonical_json(child_intent), now, now))

        first_run = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (first_run_id,)).fetchone()
        refs = [{"run_id": first_run_id, "expected_run_revision": first_run["revision"]},
                {"run_id": child_run, "expected_run_revision": 1}]
    env = env | {"batch_runs": refs, "batch_step_ids": [env["step_id"], child_step],
        "batch_pin": pin.to_dict()}

    app = HostApplication(env["db"], {"fixture": b"host F9 isolated claim key material 32 bytes"}, "fixture")
    device = app.auth.issue_device(env["actor"], [env["project_id"]], ["read", "write", "runtime"])
    headers = {"authorization": "Bearer " + device["credential"], "x-pmt-device": device["device_id"],
        "x-pmt-environment": new_id(), "x-pmt-namespace": app.auth.namespace_id,
        "x-pmt-session": env["session"]}
    app.register_session(headers, {"session_id": env["session"], "environment_id": headers["x-pmt-environment"]})
    resources = HostResourceStore(env["db"], app.auth)
    extension = HostDataExtension(env["db"], app.auth, resources, authorizer=app.authorize)
    app.extension = extension

    # Publish the client-captured graph through Host resources while one real
    # P2 run temporarily owns the exact graph path. No Host checkout is read.
    leader_ref = env["batch_runs"][0]
    leader_id = leader_ref["run_id"]
    now = utc_now()
    with env["db"].write() as conn:
        run = conn.execute("SELECT * FROM execution_runs WHERE id=?", (leader_id,)).fetchone()
        scopes = json.loads(run["scopes_json"])
        conn.execute("UPDATE execution_runs SET state='running' WHERE id=?", (leader_id,))
        conn.execute("UPDATE execution_jobs SET state='running' WHERE id=?", (run["job_id"],))
        from pmt.execution.service import _normalize_scopes
        normalized = _normalize_scopes(scopes, workspace)
        for scope in normalized:
            conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                (scope["lock_key"], leader_id, env["session"], scope["kind"], scope["workspace"], scope["resource"], now))

    graph_path = env["workspace"] / env["graph_path"]
    graph_bytes = graph_path.read_bytes()
    graph_ref = resources.publish({"request_id": new_id(), "scope_id": env["project_id"],
        "purpose": "graph_snapshot", "content": graph_bytes}, headers)["artifact_ref"]
    publish = {"protocol_version": 1, "operation": "publish_source_snapshot", "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "payload": {"project_id": env["project_id"], "repository_id": env["repo_id"],
            "canonical_workspace": workspace, "relative_graph_path": env["graph_path"],
            "run_id": leader_id, "expected_run_revision": leader_ref["expected_run_revision"],
            "expected_source_revision": 0, "branch_key": branch, "source_pin": pin.to_dict(),
            "graph_resource_ref": graph_ref}}
    published, code = app.execute(publish, headers)
    assert code == 0 and published["ok"], published.get("error")
    rebuild = {**publish, "operation": "rebuild_graph_index", "request_id": new_id(),
        "payload": {"project_id": env["project_id"], "repository_id": env["repo_id"],
            "canonical_workspace": workspace, "relative_graph_path": env["graph_path"],
            "run_id": leader_id, "expected_run_revision": leader_ref["expected_run_revision"],
            "expected_source": pin.to_dict()}}
    rebuilt, code = app.execute(rebuild, headers)
    assert code == 0 and rebuilt["ok"], rebuilt.get("error")
    with env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='queued' WHERE id=?", (leader_id,))
        conn.execute("UPDATE execution_jobs SET state='queued' WHERE id=?", (run["job_id"],))
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (leader_id,))
    return env | {"batch_runs": refs, "batch_step_ids": [env["step_id"], child_step],
                  "batch_pin": pin.to_dict(), "host_app": app, "host_headers": headers, "host_resources": resources,
                  "host_workspace": workspace, "host_pin": pin, "host_route": route}


def test_host_f9_prepare_two_real_steps_and_read_private_metadata_only(actual_context_env, monkeypatch):
    env = _host_fixture(actual_context_env, monkeypatch)
    request = {"protocol_version": 1, "operation": "prepare_step_batch", "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "source": {"product": "codex"},
        "payload": {"run_refs": env["batch_runs"], "workspace": env["host_workspace"],
            "repository_id": env["repo_id"], "relative_graph_path": env["graph_path"],
            "expected_source": env["host_pin"].to_dict(), "event_id": new_id()}}
    prepared, code = env["host_app"].execute(request, env["host_headers"])
    assert code == 0 and prepared["ok"], prepared.get("error")
    assert prepared["result"]["status"] == "prepared"
    assert prepared["result"]["physical_slots"] == 1
    assert len(prepared["result"]["member_run_refs"]) == 2

    with closing(env["db"].connect()) as conn:
        parent = conn.execute("SELECT revision FROM execution_runs WHERE id=?",
                              (prepared["result"]["parent_run_ref"],)).fetchone()
    read = {"protocol_version": 1, "operation": "read_step_batch", "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "payload": {"batch_ref": prepared["result"]["batch_ref"]["id"],
            "parent_run_id": prepared["result"]["parent_run_ref"],
            "expected_run_revision": parent["revision"]}}
    from pmt.store import LocalStore
    local_read, local_code = LocalStore(env["db"]).execute({**read, "request_id": new_id()})
    assert local_code != 0 and local_read["error"]["code"] == "host_connection_required"
    result, code = env["host_app"].execute(read, env["host_headers"])
    assert code == 0 and result["ok"], result.get("error")
    metadata = result["result"]
    assert metadata["execution_enabled"] is True
    assert metadata["source"]["source_hash"] == env["host_pin"].source_hash
    assert [member["step_id"] for member in metadata["members"]] == env["batch_step_ids"]
    assert all(member["context_current"] and member["directive_current"] for member in metadata["members"])
    assert all(member["context_ref"] and member["criteria"] for member in metadata["members"])
    assert env["workspace"].as_posix() not in canonical_json(metadata)
    assert "prompt" not in metadata and "directive" not in metadata
    assert "argv" not in canonical_json(metadata) and "environment" not in canonical_json(metadata)

    parent_id = prepared["result"]["parent_run_ref"]
    with closing(env["db"].connect()) as conn:
        revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (parent_id,)).fetchone()[0]
    attach = {"protocol_version": 1, "operation": "attach_execution_handle", "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "payload": {"run_id": parent_id, "expected_run_revision": revision,
            "handle": {"id": "fixture-host-batch-handle", "native_subagent_id": "local-fixture"}}}
    attached, code = env["host_app"].execute(attach, env["host_headers"])
    assert code == 0 and attached["ok"], attached.get("error")
    bind = {"protocol_version": 1, "operation": "bind_step_batch", "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "payload": {"batch_ref": prepared["result"]["batch_ref"]["id"],
            "parent_run_id": parent_id, "event_id": new_id()}}
    bound, code = env["host_app"].execute(bind, env["host_headers"])
    assert code == 0 and bound["ok"], bound.get("error")
    assert bound["result"]["status"] == "running"
    assert bound["result"]["handle_ref"] == "fixture-host-batch-handle"

    def publish_result(value):
        return env["host_resources"].publish({"request_id": new_id(), "scope_id": env["project_id"],
            "purpose": "result", "content": canonical_json(value).encode("utf-8")},
            env["host_headers"])["artifact_ref"]

    members = metadata["members"]
    report_steps = []
    for member in members:
        evidence_ref = publish_result({"fixture": "Host child evidence", "step_id": member["step_id"]})
        report_steps.append({"step_id": member["step_id"], "run_id": member["run_id"],
            "directive_sha256": member["directive_sha256"], "context_ref": member["context_ref"],
            "summary": "Host fixture observation; not model quality evidence.", "choices": [],
            "criteria_results": [{"criterion_id": criterion["id"], "outcome": "pass", "reason": None,
                "evidence_refs": [evidence_ref["id"]]} for criterion in member["criteria"]],
            "tests": [], "evidence_refs": [evidence_ref["id"]], "unresolved_items": []})
    report = {"schema": "pmt-batch-report-v1", "batch_id": metadata["batch_ref"]["id"],
        "steps": report_steps}
    report_ref = publish_result(report)
    stop_ref = publish_result({"fixture": "Host parent stop confirmation"})
    with closing(env["db"].connect()) as conn:
        parent = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
    parent_result = {"directive_version": 1, "actual_route": env["host_route"],
        "summary": "Host fixture result; no model was invoked.",
        "criteria_results": [{"criterion_id": criterion["id"], "outcome": "not_run",
            "reason": "Per-Step results are in the batch report", "evidence_refs": []}
            for criterion in env["criteria"]],
        "receipt_ref": stop_ref["id"], "evidence_refs": [stop_ref["id"], report_ref["id"]],
        "stop_confirmed": True, "stop_evidence_refs": [stop_ref["id"]],
        "runner_observation": {"exit_code": 0, "fixture": True},
        "batch_ref": metadata["batch_ref"]["id"], "batch_report_ref": report_ref["id"],
        "batch_report_sha256": report_ref["sha256"]}
    submitted, code = env["host_app"].execute({"protocol_version": 1,
        "operation": "submit_execution_result", "request_id": new_id(), "actor": env["actor"],
        "session_id": env["session"], "scope_id": env["project_id"],
        "payload": {"run_id": parent_id, "expected_run_revision": parent["revision"],
            "result": parent_result}}, env["host_headers"])
    assert code == 0 and submitted["ok"], submitted.get("error")
    with closing(env["db"].connect()) as conn:
        parent = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
    collected, code = env["host_app"].execute({"protocol_version": 1,
        "operation": "collect_step_batch", "request_id": new_id(), "actor": env["actor"],
        "session_id": env["session"], "scope_id": env["project_id"],
        "payload": {"batch_ref": metadata["batch_ref"]["id"], "parent_run_id": parent_id,
            "expected_run_revision": parent["revision"], "event_id": new_id()}}, env["host_headers"])
    assert code == 0 and collected["ok"], collected.get("error")
    with closing(env["db"].connect()) as conn:
        stored_batch = conn.execute("SELECT body_json FROM phase3_objects WHERE id=?",
            (metadata["batch_ref"]["id"],)).fetchone()
    batch_errors = json.loads(stored_batch[0]).get("result_errors", [])
    assert collected["result"]["status"] == "review_pending", {
        "status": collected["result"].get("status"),
        "result_errors": batch_errors,
        "children": [{key: item.get(key) for key in ("step_id", "run_id", "state", "reason_code")}
                     for item in collected["result"].get("children", [])]}
    assert len(collected["result"]["children"]) == 2

    # A stale/mutated parent report hash cannot be linked or recollected.
    with env["db"].write() as conn:
        parent_row = conn.execute("SELECT revision,result_json FROM execution_runs WHERE id=?", (parent_id,)).fetchone()
        changed_result = json.loads(parent_row["result_json"])
        changed_result["batch_report_sha256"] = "0" * 64
        conn.execute("UPDATE execution_runs SET result_json=?,revision=revision+1 WHERE id=?",
            (canonical_json(changed_result), parent_id))
        changed_revision = parent_row["revision"] + 1
    stale_collect, stale_code = env["host_app"].execute({"protocol_version": 1,
        "operation": "collect_step_batch", "request_id": new_id(), "actor": env["actor"],
        "session_id": env["session"], "scope_id": env["project_id"],
        "payload": {"batch_ref": metadata["batch_ref"]["id"], "parent_run_id": parent_id,
            "expected_run_revision": changed_revision, "event_id": new_id()}}, env["host_headers"])
    assert stale_code != 0 and stale_collect["error"]["code"] == "batch_report_invalid"

    other_session = new_id()
    other_device = env["host_app"].auth.issue_device("other-actor", [env["project_id"]],
        ["read", "write", "runtime"])
    other_headers = {"authorization": "Bearer " + other_device["credential"],
        "x-pmt-device": other_device["device_id"], "x-pmt-environment": new_id(),
        "x-pmt-namespace": env["host_app"].auth.namespace_id, "x-pmt-session": other_session}
    env["host_app"].register_session(other_headers,
        {"session_id": other_session, "environment_id": other_headers["x-pmt-environment"]})
    foreign = dict(read, request_id=new_id(), actor="other-actor", session_id=other_session)
    with pytest.raises(PmtError):
        env["host_app"].execute(foreign, other_headers)
