"""Host data storage-tier tests; these do not exercise HTTP or HTTPS transport."""
from __future__ import annotations

from contextlib import closing
import json
import uuid

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.host.application import HostApplication
from pmt.host.data import HostDataExtension
from pmt.host.resources import HostResourceStore
from pmt.phase2_common import persist_json_resource
from pmt.planning.graph import validate_graph
from pmt.util import canonical_json, fingerprint, new_id, utc_now
from pmt.workspace import canonical_workspace
from pmt.efficiency.source import SourcePin


def _node(node_id, kind):
    node = {"id": node_id, "tree_kind": kind, "node_kind": "goal", "summary": "Host graph node",
            "premise": "Client pin remains authoritative", "product_stage": "prototype",
            "product_scope": {"applies": False, "reason": "Fixture", "criteria": []},
            "autonomy": {"authority": "user", "scope": "Fixture only"}}
    if kind == "requirement":
        node.update(criteria=["source hash matches"], source_refs=["request:fixture"], evidence_refs=[])
    else:
        node.update(framework_assignment="stdlib", architecture="snapshot graph",
            logging="IDs and hashes", tests=["Host storage tier"],
            function_spec={"input": "client snapshot", "output": "F2 index", "constraints": "no Git on Host",
                "invariants": "source pin bound", "errors": "unknown", "verification": "hash"},
            choice_set={"options": [], "insufficient_reason": "fixture"},
            choice={"source": "user", "selected": "snapshot", "reason": "fixture", "scope": "local"})
    return node


@pytest.fixture
def host_data_env(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    app = HostApplication(db, {"fixture": b"test-only host claim key material"}, "fixture")
    repo_id, project_id, work_id, item_id, step_id, job_id, run_id = (new_id() for _ in range(7))
    session_id, actor = "host-data-session", "host-data-actor"
    now = utc_now()
    relative = "docs/pmt-docs/plan.graph.json"
    canonical = canonical_workspace(repo_id, "main")
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (repo_id, "repository", "fixture-repo", now, now))
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (project_id, "project", repo_id, "fixture-project", canonical_json({"repository_id": repo_id}), now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (work_id, "work", project_id, "Work", "InProgress", "{}", 1, now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (item_id, "item", project_id, work_id, "Item", "InProgress", "{}", 1, now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (step_id, "step", project_id, item_id, "Step", "InProgress", "{}", 1, now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (job_id, step_id, "running", "{}", now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) "
                     "VALUES(?,?,?,1,'running',3,?,1,?,?, '{}','{}',?,?)",
                     (run_id, job_id, step_id, session_id, canonical,
                      canonical_json([{"kind": "path", "workspace": canonical, "resource": relative}]), now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run_id, session_id, "path", canonical, relative, now))
    # A Project grant anchors its mapped repository identity without silently
    # broadening the device's separate Repository scope grants.
    device = app.auth.issue_device(actor, [project_id], ["read", "write", "runtime"])
    headers = {"authorization": "Bearer " + device["credential"], "x-pmt-device": device["device_id"],
        "x-pmt-environment": new_id(), "x-pmt-namespace": app.auth.namespace_id, "x-pmt-session": session_id}
    app.register_session(headers, {"session_id": session_id, "environment_id": headers["x-pmt-environment"]})
    principal = app.auth.principal(headers["authorization"][7:], device["device_id"], app.auth.namespace_id,
        session_id=session_id, environment_id=headers["x-pmt-environment"])
    resources = HostResourceStore(db, app.auth)
    extension = HostDataExtension(db, app.auth, resources, authorizer=app.authorize)
    graph = {"schema_version": 1, "project_id": project_id, "graph_version": 1,
        "nodes": [_node(new_id(), "requirement"), _node(new_id(), "implementation")],
        "relations": [], "provenance": {"source_ref": "client fixture"}}
    pin = SourcePin(repo_id, project_id, "main", "a" * 40, 1, 1,
        validate_graph(graph, project_id, complete=False)["sha256"], "clean")
    return {"db": db, "app": app, "extension": extension, "resources": resources,
        "principal": principal, "headers": headers, "repo": repo_id, "project": project_id,
        "work": work_id, "item": item_id, "step": step_id, "job": job_id, "run": run_id,
        "session": session_id, "actor": actor, "canonical": canonical, "relative": relative,
        "graph": graph, "pin": pin}


def _request(env, operation, **payload):
    return {"protocol_version": 1, "operation": operation, "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project"],
        "payload": {"project_id": env["project"], "repository_id": env["repo"],
            "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
            "run_id": env["run"], "expected_run_revision": 3, **payload}}


def _publish_source(env):
    graph_bytes = canonical_json(env["graph"]).encode("utf-8")
    graph_blob = env["resources"].publish({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "graph_snapshot", "content": graph_bytes}, env["headers"])["artifact_ref"]
    req = _request(env, "publish_source_snapshot", branch_key="main",
        source_pin=env["pin"].to_dict(), graph_resource_ref=graph_blob, expected_source_revision=0)
    principal = env["principal"]
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, req, principal)
    envelope, code = env["extension"].execute_file(env["db"], req, principal, env["headers"])
    assert code == 0 and envelope["ok"], envelope.get("error")
    return envelope["result"]


def test_host_source_snapshot_is_client_provenance_scope_cas_and_no_git(host_data_env, monkeypatch):
    env = host_data_env
    receipt = _publish_source(env)
    assert receipt["provenance"] == "client_snapshot" and receipt["host_git_verified"] is False
    assert receipt["source_pin"]["source_hash"] == env["pin"].source_hash
    import pmt.efficiency.graph as graph_module
    monkeypatch.setattr(graph_module, "_git", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("Host must not run Git")))
    read_req = _request(env, "read_source_snapshot", expected_source=env["pin"].to_dict())
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, read_req, env["principal"])
        view = env["extension"].database_view(env["db"], env["principal"], read_req, env["headers"])
        source = view.source_repository.capture(conn, read_req)
    assert source["source_pin"].source_hash == env["pin"].source_hash
    assert source["graph"] == env["graph"]
    assert source["source_provenance"] == "client_snapshot"

    rebuild_req = _request(env, "rebuild_graph_index", expected_source=env["pin"].to_dict())
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, rebuild_req, env["principal"])
    rebuilt, code = env["extension"].execute_file(env["db"], rebuild_req, env["principal"], env["headers"])
    assert code == 0 and rebuilt["ok"], rebuilt.get("error")
    query_req = _request(env, "query_graph", expected_source=env["pin"].to_dict(),
        query={"node_ids": [node["id"] for node in env["graph"]["nodes"]], "max_depth": 2,
               "page_size": 100})
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, query_req, env["principal"])
        view = env["extension"].database_view(env["db"], env["principal"], query_req, env["headers"])
        result = env["extension"].handle(view, conn, query_req, env["principal"])
    assert {item["value"]["id"] for item in result["graph_slice"]["items"] if item["entity"] == "node"} == {
        node["id"] for node in env["graph"]["nodes"]}


def test_host_source_pointer_rejects_stale_run_and_revision(host_data_env):
    env = host_data_env
    _publish_source(env)
    stale = _request(env, "authorize_workspace", mode="execute", branch_key="main",
                     expected_run_revision=2)
    with closing(env["db"].connect()) as conn:
        with pytest.raises(PmtError) as caught:
            env["extension"].authorize(conn, stale, env["principal"])
    assert caught.value.code == "execution_revision_conflict"

    source_capture = _request(env, "authorize_workspace", mode="source_capture", branch_key="main")
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, source_capture, env["principal"])
        response = env["extension"].handle(env["extension"].database_view(env["db"], env["principal"],
            source_capture, env["headers"]), conn, source_capture, env["principal"])
    assert response["status"] == "authorized" and response["source_pin"] is None
    assert response["source_capture_only"] is True and response["model_execution_allowed"] is False

    other_session = "another-host-session"
    headers = dict(env["headers"], **{"x-pmt-session": other_session})
    env["app"].register_session(headers, {"session_id": other_session,
        "environment_id": headers["x-pmt-environment"]})
    other_principal = env["app"].auth.principal(headers["authorization"][7:], headers["x-pmt-device"],
        headers["x-pmt-namespace"], session_id=other_session, environment_id=headers["x-pmt-environment"])
    wrong_owner = _request(env, "read_source_snapshot", expected_source=env["pin"].to_dict())
    wrong_owner["session_id"] = other_session
    with closing(env["db"].connect()) as conn:
        with pytest.raises(PmtError) as caught:
            env["extension"].authorize(conn, wrong_owner, other_principal)
    assert caught.value.code == "workspace_authority_stale"


def test_missing_local_file_effect_is_a_cli_not_found_error(host_data_env):
    env = host_data_env
    _publish_source(env)
    req = _request(env, "read_local_file_effect", effect_id=new_id(),
        target_relative_path=env["relative"], branch_key="main", expected_source=env["pin"].to_dict())
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, req, env["principal"])
        with pytest.raises(PmtError) as caught:
            env["extension"].handle(env["db"], conn, req, env["principal"])
    assert caught.value.code == "host_file_effect_not_found"
    assert caught.value.exit_code == 2


def test_host_source_publication_rejects_pin_and_branch_mismatch(host_data_env):
    env = host_data_env
    graph_bytes = canonical_json(env["graph"]).encode("utf-8")
    graph_blob = env["resources"].publish({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "graph_snapshot", "content": graph_bytes}, env["headers"])["artifact_ref"]
    bad_pin = SourcePin(env["repo"], env["project"], "main", "a" * 40, 1, 1, "0" * 64, "clean")
    wrong_hash = _request(env, "publish_source_snapshot", branch_key="main", source_pin=bad_pin.to_dict(),
        graph_resource_ref=graph_blob, expected_source_revision=0)
    envelope, code = env["extension"].execute_file(env["db"], wrong_hash,
        env["principal"], env["headers"])
    assert code == 3 and envelope["error"]["code"] == "source_snapshot_invalid"

    wrong_branch = _request(env, "authorize_workspace", mode="source_capture", branch_key="other",
        canonical_workspace=canonical_workspace(env["repo"], "other"))
    with closing(env["db"].connect()) as conn:
        with pytest.raises(PmtError) as caught:
            env["extension"].authorize(conn, wrong_branch, env["principal"])
    assert caught.value.code == "workspace_authority_stale"

    other_repository = new_id()
    with env["db"].write() as conn:
        now = utc_now()
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES(?,?,?,?,?)",
            (other_repository, "repository", "foreign-repo", now, now))
    wrong_repo = _request(env, "authorize_workspace", mode="source_capture", branch_key="main",
        repository_id=other_repository,
        canonical_workspace=canonical_workspace(other_repository, "main"))
    with closing(env["db"].connect()) as conn:
        with pytest.raises(PmtError) as caught:
            env["extension"].authorize(conn, wrong_repo, env["principal"])
    assert caught.value.code == "repository_scope_mismatch"


def test_host_verification_manifest_is_bound_to_source_criteria_environment_and_evidence(host_data_env):
    env = host_data_env
    _publish_source(env)
    evidence_bytes = b"verified fixture evidence"
    evidence = env["resources"].publish({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "evidence", "content": evidence_bytes}, env["headers"])["artifact_ref"]
    criteria_hash = {"criterion-1": fingerprint({"id": "criterion-1", "meaning": "current criteria"})}
    input_hash = fingerprint({"target": "fixture"})
    manifest = {"schema_version": 1, "target_id": env["step"], "definition_id": "test.py_compile",
        "definition_version": "1", "environment_id": env["headers"]["x-pmt-environment"],
        "canonical_workspace": env["canonical"], "source_pin": env["pin"].to_dict(),
        "command": ["fixture-check"], "inputs_sha256": input_hash, "criteria": criteria_hash,
        "workspace_files": [{"path": env["relative"], "sha256": "c" * 64, "size": 20}],
        "runtime": {"os": "fixture", "architecture": "fixture", "python": "3.13",
            "sqlite": "fixture", "packages": []},
        "dependency_manifests": [], "configuration_hashes": [],
        "evidence_refs": [{"id": evidence["id"], "sha256": evidence["sha256"]}],
        "inventory_status": "complete", "provenance": "client_snapshot"}
    manifest_bytes = canonical_json(manifest).encode("utf-8")
    manifest_ref = env["resources"].publish({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "verification_snapshot", "content": manifest_bytes}, env["headers"])["artifact_ref"]
    request = _request(env, "publish_verification_snapshot", target_id=env["step"],
        definition_id=manifest["definition_id"], definition_version="1", expected_source=env["pin"].to_dict(),
        verification_resource_ref=manifest_ref, expected_snapshot_revision=0,
        command=["fixture-check"], inputs_sha256=input_hash)
    with closing(env["db"].connect()) as conn:
        # The actual Host Step criteria are empty in this storage fixture; use
        # a Work target with current Host criteria via explicit record body below.
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
            (canonical_json({"criteria": [{"id": "criterion-1", "meaning": "current criteria"}]}), env["step"]))
        env["extension"].authorize(conn, request, env["principal"])
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
            (canonical_json({"criteria": [{"id": "criterion-1", "meaning": "current criteria"}]}), env["step"]))
    principal = env["principal"]
    envelope, code = env["extension"].execute_file(env["db"], request, principal, env["headers"])
    assert code == 0 and envelope["ok"], envelope.get("error")
    req = _request(env, "record_verification", target_id=env["step"],
        definition_id=manifest["definition_id"], definition_version="1", command=["fixture-check"],
        outcome="pass", exit_code=0, evidence_ids=[evidence["id"]], criterion_ids=["criterion-1"],
        inputs={"target": "fixture"}, inputs_sha256=input_hash,
        expected_source=env["pin"].to_dict(), verification_snapshot_ref=envelope["result"]["snapshot_ref"])
    with closing(env["db"].connect()) as conn:
        view = env["extension"].database_view(env["db"], principal, req, env["headers"])
        from pmt.verification import _record_and_scope, _criteria
        record, scope, body = _record_and_scope(conn, env["step"])
        snapshot, digest, reasons, _, _ = view.verification_snapshot_provider.snapshot(
            conn, env["step"], manifest["definition_id"], "1", ["fixture-check"],
            fingerprint({"target": "fixture"}), record, scope, body, _criteria(body))
    assert snapshot["environment_id"] == env["headers"]["x-pmt-environment"]
    assert snapshot["source_snapshot_hash"] == env["pin"].source_hash
    assert snapshot["workspace_known"] is True and reasons == [] and len(digest) == 64

    from test_phase3_reuse import _actual_definition
    definition = _actual_definition()
    definition["selectors"]["source"]["paths"] = [env["relative"]]
    reuse_request = _request(env, "resolve_reuse", definition=definition, target_id=env["step"],
        workspace=env["canonical"], paths=[env["relative"]],
        command=["fixture-check"], inputs={"target": "fixture"}, event_id=new_id(),
        expected_source=env["pin"].to_dict())
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, reuse_request, principal)
    reuse_result, reuse_code = env["extension"].execute_file(
        env["db"], reuse_request, principal, env["headers"])
    assert reuse_code == 0 and reuse_result["ok"], reuse_result.get("error")
    assert reuse_result["result"]["status"] == "claimed", reuse_result["result"]
    reuse_ref = reuse_result["result"]["body_ref"]
    reuse_read = _request(env, "read_reuse_decision", body_ref=reuse_ref,
        workspace=env["canonical"], paths=[env["relative"]])
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, reuse_read, principal)
        view = env["extension"].database_view(env["db"], principal, reuse_read, env["headers"])
        decision = env["extension"].handle(view, conn, reuse_read, principal)
    assert decision["status"] == "active"
    assert decision["decision_ref"] == reuse_ref

    unknown_manifest = dict(manifest, inventory_status="unknown")
    unknown_resource = env["resources"].publish({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "verification_snapshot", "content": canonical_json(unknown_manifest).encode("utf-8")},
        env["headers"])["artifact_ref"]
    unknown_publish = _request(env, "publish_verification_snapshot", target_id=env["step"],
        definition_id=manifest["definition_id"], definition_version="1", expected_source=env["pin"].to_dict(),
        verification_resource_ref=unknown_resource, expected_snapshot_revision=1,
        command=["fixture-check"], inputs_sha256=input_hash)
    unknown_envelope, unknown_code = env["extension"].execute_file(
        env["db"], unknown_publish, env["principal"], env["headers"])
    assert unknown_code == 0 and unknown_envelope["ok"], unknown_envelope.get("error")
    unknown_request = _request(env, "record_verification", target_id=env["step"],
        definition_id=manifest["definition_id"], definition_version="1", command=["fixture-check"],
        outcome="pass", exit_code=0, before_fingerprint=digest,
        evidence_ids=[evidence["id"]], criterion_ids=["criterion-1"], inputs={"target": "fixture"},
        inputs_sha256=input_hash, expected_source=env["pin"].to_dict(),
        verification_snapshot_ref=unknown_envelope["result"]["snapshot_ref"])
    with closing(env["db"].connect()) as conn:
        from pmt.verification import _record_and_scope, _criteria
        view = env["extension"].database_view(env["db"], principal, unknown_request, env["headers"])
        record, scope, body = _record_and_scope(conn, env["step"])
        unknown_snapshot, _, snapshot_reasons, _, _ = view.verification_snapshot_provider.snapshot(
            conn, env["step"], manifest["definition_id"], "1", ["fixture-check"],
            input_hash, record, scope, body, _criteria(body))
        assert unknown_snapshot["workspace_known"] is False
        assert snapshot_reasons == ["client_inventory_unknown"]
        with pytest.raises(PmtError) as caught:
            env["extension"].handle(view, conn, unknown_request, principal)
    assert caught.value.code == "verification_fingerprint_unknown"


def test_host_f5_reuses_private_directive_current_claim_and_snapshot_source(host_data_env):
    env = host_data_env
    _publish_source(env)
    directive = {"purpose": "Verify the Host source projection", "goal": "Preserve current graph and criteria",
        "non_goal": ["No local checkout access"],
        "change_scope": {"add": [], "modify": [], "delete": [], "forbidden": ["Host Git access"]},
        "inputs": [{"name": "graph", "meaning": "client-captured source snapshot"}],
        "outputs": [{"name": "context", "meaning": "bounded role projection"}],
        "tests": [{"name": "host source", "meaning": "F2 hash and traversal"}],
        "logging": [{"name": "safe trace", "meaning": "IDs and hashes only"}],
        "method": {"steps": ["read authorized source", "project required fields"]},
        "context_refs": [node["id"] for node in env["graph"]["nodes"]],
        "autonomy": {"authority": "method", "scope": "current Step"}}
    evidence = env["resources"].publish({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "evidence", "content": b"host verification evidence"}, env["headers"])["artifact_ref"]
    criteria = [{"id": "current-source", "meaning": "Use current SourcePin",
                 "evidence_refs": [evidence["id"]]}]
    now = utc_now()
    resource_req = {"request_id": new_id(), "actor": env["actor"], "session_id": env["session"],
                    "scope_id": env["project"], "payload": {}}
    directive_resource = persist_json_resource(env["db"], resource_req, directive,
                                                env["project"], "step_directive", env["step"])
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?", (canonical_json({
            "directive_id": directive_resource["artifact_id"], "directive_version": 1,
            "invalidated": False, "criteria": criteria}), env["step"]))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,?,?) ON CONFLICT(step_id) DO UPDATE SET directive_id=excluded.directive_id,"
            "directive_version=excluded.directive_version,requirements_version=excluded.requirements_version,"
            "plan_version=excluded.plan_version,role=excluded.role,product_stage=excluded.product_stage,"
            "workspace=excluded.workspace,scopes_json=excluded.scopes_json,criteria_json=excluded.criteria_json,"
            "dependencies_json=excluded.dependencies_json,updated_at=excluded.updated_at",
            (env["step"], directive_resource["artifact_id"], 1, "requirements-v1", "plan-v1", "lower",
             "prototype", env["canonical"], canonical_json([{"kind": "path", "workspace": env["canonical"],
             "resource": env["relative"]}]), canonical_json(criteria), "[]", now, now))
        intent = {"run_id": env["run"], "task": {"task_id": env["item"], "step_id": env["step"]},
                  "plan_version": "plan-v1", "requirements_version": "requirements-v1",
                  "directive_ref": directive_resource["artifact_id"], "criteria": criteria,
                  "role": "lower", "scope_id": env["project"], "directive_version": 1}
        conn.execute("UPDATE execution_runs SET directive_version=1,intent_json=? WHERE id=?",
                     (canonical_json(intent), env["run"]))

    pin = env["pin"].to_dict()
    rebuild = _request(env, "rebuild_graph_index", expected_source=pin)
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, rebuild, env["principal"])
    rebuilt, code = env["extension"].execute_file(env["db"], rebuild, env["principal"], env["headers"])
    assert code == 0 and rebuilt["ok"], rebuilt.get("error")
    node_ids = [node["id"] for node in env["graph"]["nodes"]]
    request = _request(env, "build_task_context", task_ref={"task_id": env["item"],
        "step_id": env["step"], "run_id": env["run"]}, expected_source=pin, role="lower",
        workspace=env["canonical"], node_ids=node_ids,
        budget={"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"})
    with closing(env["db"].connect()) as conn:
        env["extension"].authorize(conn, request, env["principal"])
    result, code = env["extension"].execute_file(env["db"], request, env["principal"], env["headers"])
    assert code == 0 and result["ok"], result.get("error")
    context = result["result"]
    assert context["incomplete"] is False
    assert not any(item.get("reason_code") in {"source_pin_unverified", "traversal_limit_or_depth"}
                   for item in context["unknown"])
    assert {item["value"]["id"] for item in next(section for section in context["included"]
            if section["section_id"] == "related_graph")["value"]["nodes"]} == set(node_ids)
    assert env["db"].root.as_posix() not in json.dumps(context)
