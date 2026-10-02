"""Hosted client-local routing and source-bound plan metadata tests."""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import sqlite3
import uuid

import pytest

pytest_plugins = ["test_phase3_host_network"]

from pmt.errors import PmtError
from pmt.routing.client_config import ClientRoutingConfig, route_for_enqueue
from pmt.util import canonical_json, new_id


def _request(operation, payload, *, actor="fixture-actor", session="fixture-session",
             scope=None, request_id=None, expected_revision=None):
    request = {"protocol_version": 1, "operation": operation,
        "request_id": request_id or new_id(), "actor": actor, "session_id": session,
        "payload": payload, "source": {"product": "fixture"}}
    if scope:
        request["scope_id"] = scope
    if expected_revision is not None:
        request["expected_revision"] = expected_revision
    return request


def _capability():
    return {"agent": "codex", "provider": "fixture-provider", "model": "fixture-native-v1",
        "mode": "native", "support": "supported", "capabilities": ["code"],
        "capability_ref": "fixture-capability-v1", "auth_state": "authenticated",
        "max_concurrency": 1, "evidence_ref": "fixture:observed-support",
        "source_ref": "fixture:local-probe", "observed_at": "2026-10-02T00:00:00Z"}


def test_client_routing_port_reuses_phase2_selection_without_host_or_business_db(tmp_path):
    config_root, data_root = tmp_path / "config", tmp_path / "business-data"
    port = ClientRoutingConfig(config_root)
    project = new_id()
    policy_request = _request("save_routing_policy", {"policy": {"mode": "auto", "economy": True,
        "role_preferences": {"lower": {"model": "fixture-native-v1", "agent": "codex"}},
        "api_allowed": False}}, scope=project, expected_revision=1)
    policy, code = port.execute(policy_request)
    assert code == 0 and policy["ok"], policy.get("error")
    replay, replay_code = port.execute(policy_request)
    assert replay_code == 0 and replay == policy
    changed = json.loads(canonical_json(policy_request))
    changed["payload"]["policy"]["economy"] = False
    conflict, conflict_code = port.execute(changed)
    assert conflict_code == 3 and conflict["error"]["code"] == "request_conflict"

    caps, caps_code = port.execute(_request("register_capabilities",
        {"capabilities": [_capability()]}, scope=project, expected_revision=1))
    assert caps_code == 0 and caps["ok"], caps.get("error")
    selection, selection_code = port.execute(_request("select_execution_route", {"requirements": {
        "role": "lower", "needs": ["code"], "active_agent": "codex", "run_state": "not_started",
        "requested_route": "native"}}, scope=project))
    assert selection_code == 0 and selection["ok"], selection.get("error")
    route = route_for_enqueue(selection["result"])
    assert route["mode"] == "subagent" and route["actual_support"] == "verified_supported"
    assert "settings_revision" not in route and "capability_revision" not in route
    from pmt.host.execution_metadata import validate_execution_metadata
    validate_execution_metadata({"operation": "enqueue_execution", "payload": {"route": route}})
    assert not data_root.exists(), "Client routing must not create/open the business database root"
    assert port.path.name == "routing-client.sqlite3"


def test_legacy_routing_settings_are_imported_read_only_and_capabilities_require_reobservation(tmp_path):
    legacy_root, config_root = tmp_path / "old-data", tmp_path / "new-config"
    legacy_root.mkdir()
    legacy_path = legacy_root / "pmt.sqlite3"
    policy = {"mode": "auto", "economy": False,
        "role_preferences": {"lower": {"model": "remembered-model", "agent": "claude",
            "provider": "remembered-provider", "priority": ["remembered-model"]}},
        "api_allowed": False, "price_status": "unknown"}
    old_capability = _capability()
    rows = {"routing_policy": {"revision": 3, "body": policy},
        "routing_capabilities": {"revision": 5, "body": {"items": [old_capability]}}}
    with closing(sqlite3.connect(legacy_path)) as conn:
        conn.execute("CREATE TABLE routing_settings(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, body_json TEXT NOT NULL, updated_at TEXT NOT NULL)")
        for key, value in rows.items():
            conn.execute("INSERT INTO routing_settings VALUES(?,?,?,?)",
                (key, value["revision"], canonical_json(value["body"]), "fixture"))
        conn.commit()
    original_hash = hashlib.sha256(legacy_path.read_bytes()).hexdigest()

    port = ClientRoutingConfig(config_root, legacy_data_root=legacy_root)
    policy_result, policy_code = port.execute(_request("read_routing_policy", {}))
    caps_result, caps_code = port.execute(_request("inspect_capabilities", {}))
    assert policy_code == caps_code == 0
    assert policy_result["result"]["body"]["role_preferences"] == policy["role_preferences"]
    assert policy_result["result"]["revision"] == 3
    assert caps_result["result"]["revision"] == 5
    assert caps_result["result"]["body"]["items"][0]["capability_ref"] == old_capability["capability_ref"]
    assert caps_result["result"]["body"]["items"][0]["support"] == "unknown"
    with closing(sqlite3.connect(port.path)) as conn:
        migration = conn.execute("SELECT source_fingerprint,metadata_json FROM client_routing_migrations "
            "WHERE id='legacy-routing-v1'").fetchone()
    assert migration and len(migration[0]) == 64
    assert json.loads(migration[1])["capabilities_downgraded"] == 1
    assert hashlib.sha256(legacy_path.read_bytes()).hexdigest() == original_hash


def test_client_routing_does_not_select_unverified_or_direct_api_routes(tmp_path):
    port = ClientRoutingConfig(tmp_path / "config")
    policy, code = port.execute(_request("save_routing_policy", {"policy": {"mode": "auto",
        "economy": True, "role_preferences": {}, "api_allowed": False}}, expected_revision=1))
    assert code == 0 and policy["ok"]
    register, code = port.execute(_request("register_capabilities", {"capabilities": [
        dict(_capability(), support="unknown")]}, expected_revision=1))
    assert code == 0 and register["ok"]
    selection, code = port.execute(_request("select_execution_route", {"requirements": {
        "role": "lower", "needs": ["code"], "active_agent": "codex", "requested_route": "api"}}))
    assert code == 0 and selection["ok"] and selection["result"]["blocked"] is True
    with pytest.raises(PmtError) as blocked:
        route_for_enqueue(selection["result"])
    assert blocked.value.code == selection["result"]["selection_reason_code"]


def test_https_new_project_publishes_f4_plan_then_selects_local_native_route_and_builds_f5(
        live_host, tmp_path, monkeypatch):
    from contextlib import closing
    from test_phase3_documents import _node
    from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout
    from pmt.efficiency.control import _snapshot
    from pmt.hosted_files import HostedFiles
    from pmt.hosted_runtime import HostedLocalRuntime
    from pmt.util import utc_now

    env = live_host
    env["graph"]["nodes"] = [_node(item["id"], item["tree_kind"]) for item in env["graph"]["nodes"]]
    env["graph"]["relations"] = [{"id": new_id(), "kind": "implements",
        "from": next(node["id"] for node in env["graph"]["nodes"] if node["tree_kind"] == "requirement"),
        "to": next(node["id"] for node in env["graph"]["nodes"] if node["tree_kind"] == "implementation")}]
    with env["db"].write() as conn:
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (new_id(), env["run"], env["session_a"], "path",
                env["canonical"], "docs/pmt-docs/plan.md", utc_now()))
    env = _seed_hosted_git_checkout(env, tmp_path)
    with env["db"].write() as conn:
        row = conn.execute("SELECT permissions_json FROM host_devices WHERE id=?",
            (env["headers"]["x-pmt-device"],)).fetchone()
        permissions = set(json.loads(row[0]))
        permissions.add("review")
        conn.execute("UPDATE host_devices SET permissions_json=? WHERE id=?",
            (canonical_json(sorted(permissions)), env["headers"]["x-pmt-device"]))

    profile = {"workspace_mappings": [{"repository_id": env["repo"], "project_id": env["project"],
        "branch": "main", "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"],
        "canonical_workspace": env["canonical"]}]}
    files = HostedFiles(profile, env["store_a"], tmp_path / "plan-doc-effects")
    document = "docs/pmt-docs/plan.md"
    doc_base = dict(env["common"], context_ref=env["context_ref"], document_path=document)
    prepared, code = files.execute(_host_request(env, "prepare_document_segments", doc_base))
    assert code == 0 and prepared["ok"], prepared.get("error")
    published, code = files.execute(_host_request(env, "publish_document_segments",
        doc_base | {"journal_id": prepared["result"]["journal_id"]}))
    assert code == 0 and published["ok"], published.get("error")
    doc_effect = published["result"]["effect_ref"]
    assert published["result"]["coverage"]["segments"] == "complete"

    plan_id = new_id()
    from pmt.efficiency.source import pin_source
    plan_payload = {"plan_id": plan_id,
        "expected_plan_hash": None, "requirements_version": "requirements-v1", "plan_version": "plan-v1",
        "repository_id": env["repo"], "project_id": env["project"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3, "source_pin": env["pin"].to_dict(),
        "document_effect_id": doc_effect["id"],
        "expected_document_effect_revision": doc_effect["revision"]}
    wrong_pin = dict(plan_payload["source_pin"], graph_hash="0" * 64)
    wrong_pin.pop("source_hash", None)
    wrong_pin.pop("source_kind", None)
    wrong_source = dict(plan_payload, plan_id=new_id(), source_pin=pin_source(wrong_pin).to_dict())
    rejected, rejected_code = env["store_a"].execute(_host_request(env, "publish_client_plan", wrong_source))
    assert rejected_code == 3 and not rejected["ok"]
    wrong_effect = dict(plan_payload, plan_id=new_id(),
        expected_document_effect_revision=doc_effect["revision"] + 1)
    rejected, rejected_code = env["store_a"].execute(_host_request(env, "publish_client_plan", wrong_effect))
    assert rejected_code == 3 and not rejected["ok"]
    with closing(env["db"].connect()) as conn:
        coverage_row = conn.execute("SELECT body_json FROM phase3_objects WHERE kind='segment_coverage' AND id=?",
            (env["project"],)).fetchone()
    saved_coverage = coverage_row[0]
    incomplete_certificate = json.loads(saved_coverage)
    incomplete_certificate["coverage_status"] = "unknown"
    with env["db"].write() as conn:
        conn.execute("UPDATE phase3_objects SET body_json=? WHERE kind='segment_coverage' AND id=?",
            (canonical_json(incomplete_certificate), env["project"]))
    try:
        rejected, rejected_code = env["store_a"].execute(_host_request(env, "publish_client_plan",
            dict(plan_payload, plan_id=new_id())))
        assert rejected_code == 3 and not rejected["ok"]
        assert rejected["error"]["code"] == "client_plan_coverage_invalid"
    finally:
        with env["db"].write() as conn:
            conn.execute("UPDATE phase3_objects SET body_json=? WHERE kind='segment_coverage' AND id=?",
                (saved_coverage, env["project"]))
    plan_request = _host_request(env, "publish_client_plan", plan_payload)
    plan, code = env["store_a"].execute(plan_request)
    assert code == 0 and plan["ok"], plan.get("error")
    assert plan["result"]["state"] == "published" and plan["result"]["host_document_verified"] is False
    plan_replay, replay_code = env["store_a"].execute(plan_request)
    assert replay_code == 0 and plan_replay == plan
    changed_request = json.loads(canonical_json(plan_request))
    changed_request["payload"]["plan_version"] = "different-version"
    changed, changed_code = env["store_a"].execute(changed_request)
    assert changed_code == 3 and changed["error"]["code"] == "request_conflict"
    meta, code = env["store_a"].execute(_host_request(env, "read_client_plan",
        {"plan_id": plan_id, "project_id": env["project"]}))
    assert code == 0 and meta["ok"] and meta["result"]["plan_hash"] == plan["result"]["plan_hash"]
    assert "graph" not in meta["result"]

    old_run, code = env["store_a"].execute(_host_request(env, "read_execution", {"run_id": env["run"]}))
    assert code == 0 and old_run["ok"]
    no_launch_evidence = env["store_a"].publish_resource({"request_id": new_id(),
        "scope_id": env["project"], "purpose": "evidence"}, canonical_json({
            "fixture_only": True, "run_id": env["run"], "plan_id": plan_id,
            "document_effect_id": doc_effect["id"], "native_dispatch_performed": False}).encode(),
        session_id=env["session_a"])["artifact_ref"]
    no_dispatch, code = env["store_a"].execute(_host_request(env, "reconcile_execution", {
        "run_id": env["run"], "expected_run_revision": old_run["result"]["run"]["revision"],
        "stopped": False, "not_started": True, "actual_state": "blocked",
        "evidence_refs": [no_launch_evidence["id"]]}))
    assert code == 0 and no_dispatch["ok"], no_dispatch.get("error")
    assert no_dispatch["result"]["state"] == "blocked"

    directive = env["directive"]
    step_req = _host_request(env, "save_step_directive", {"item_id": env["item"],
        "title": "Hosted implementation Step", "directive": directive,
        "requirements_version": "requirements-v1", "plan_version": "plan-v1",
        "plan_id": plan_id, "workspace": env["canonical"],
        "canonical_workspace": env["canonical"], "repository_id": env["repo"],
        "relative_graph_path": env["relative"], "kind": "implement",
        "product_stage": "prototype", "role": "lower", "scopes": [{"kind": "path",
            "workspace": env["canonical"], "resource": env["relative"]}],
        "criteria": env["criteria"], "dependencies": []})
    created, code = env["store_a"].execute(step_req)
    assert code == 0 and created["ok"], created.get("error")
    step_id = created["result"]["step_id"]

    from test_phase3_hosted_cli import _cli, _configure
    routing_config, routing_data = tmp_path / "routing-client-config", tmp_path / "routing-client-data"
    _configure(env, routing_config)
    cap = _capability()
    cap.update(capabilities=["code"], auth_state="authenticated", max_concurrency=8)
    register_request = _request("register_capabilities", {"capabilities": [cap]},
        actor=env["actor"], session=env["session_a"], scope=env["project"], expected_revision=1)
    registered = _cli(routing_config, routing_data, register_request)
    assert registered.returncode == 0, registered.stderr or registered.stdout
    assert json.loads(registered.stdout)["ok"]
    selected_process = _cli(routing_config, routing_data, _request("select_execution_route", {"requirements": {
        "role": "lower", "needs": ["code"], "active_agent": "codex",
        "requested_route": "native", "run_state": "not_started"}},
        actor=env["actor"], session=env["session_a"], scope=env["project"]))
    assert selected_process.returncode == 0, selected_process.stderr or selected_process.stdout
    selected = json.loads(selected_process.stdout)
    assert selected["ok"], selected.get("error")
    route = route_for_enqueue(selected["result"])
    queued, code = env["store_a"].execute(_host_request(env, "enqueue_execution",
        {"step_id": step_id, "route": route}))
    assert code == 0 and queued["ok"], queued.get("error")
    run_id = queued["result"]["run_id"]
    prepared_run, code = env["store_a"].execute(_host_request(env, "prepare_execution",
        {"run_id": run_id, "expected_run_revision": 1}))
    assert code == 0 and prepared_run["ok"], prepared_run.get("error")
    run = prepared_run["result"]
    run_read, run_read_code = env["store_a"].execute(_host_request(env, "read_execution", {"run_id": run_id}))
    assert run_read_code == 0 and run_read["ok"], run_read.get("error")
    assert run_read["result"]["run"]["workspace"] == env["canonical"]
    assert run_read["result"]["run"]["owner_session"] == env["session_a"]
    assert run_read["result"]["run"]["state"] == "starting", (prepared_run["result"], run_read["result"]["run"]["route"])
    context, code = env["store_a"].execute(_host_request(env, "build_task_context", {
        "project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": run_id, "expected_run_revision": run["revision"],
        "expected_source": env["pin"].to_dict(),
        "task_ref": {"task_id": env["item"], "step_id": step_id, "run_id": run_id},
        "role": "lower", "workspace": env["canonical"],
        "node_ids": [node["id"] for node in env["graph"]["nodes"]],
        "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}}))
    assert code == 0 and context["ok"] and context["result"]["incomplete"] is False, context.get("error")
    runtime = HostedLocalRuntime(env["store_a"], lambda _run, pin: {
        "repository_id": env["repo"], "project_id": env["project"],
        "branch": pin["selected_ref"], "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"]},
        tmp_path / "plan-runtime-spool")
    native, code = runtime.dispatch(_host_request(env, "dispatch_execution", {
        "run_id": run_id, "context_ref": context["result"]["context_ref"]}))
    assert code == 0 and native["ok"], native.get("error")
    action = native["result"]["main_action"]
    assert action["kind"] == "invoke_native_subagent"
    assert action["context_source_hash"] == env["pin"].source_hash
    assert action["capability_ref"] == cap["capability_ref"]
    assert "graph" not in native["result"]
