"""Current source metadata is scope-bound and grants no execution permission."""
from contextlib import closing
import json
import subprocess

pytest_plugins = ["test_phase3_host_network"]

from pmt.efficiency.source import inspect_graph_source
from pmt.util import canonical_json, new_id
from test_phase3_hosted_runtime import _seed_hosted_git_checkout, _host_request


def test_https_source_metadata_keeps_projects_distinct_without_run_lock(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    admin = env["store_admin"]
    actor = admin.check_compatibility()["actor"]
    def call(operation, payload, scope=None, request_id=None):
        request = {"protocol_version": 1, "request_id": request_id or new_id(), "operation": operation,
            "actor": actor, "session_id": env["session_admin"],
            "source": {"product": "cli"}, "payload": payload}
        if scope is not None:
            request["scope_id"] = scope
        reply, code = admin.execute(request)
        assert code == 0 and reply["ok"], (code, reply.get("error"))
        return reply["result"]
    project = call("create_scope", {"kind": "project", "parent_id": env["repo"],
        "slug": "second-project", "body": {"repository_id": env["repo"]}})["id"]
    work = call("save_change", {"kind": "work", "title": "second work", "reason": "fixture"}, project)["record_id"]
    item = call("save_change", {"kind": "item", "title": "second item", "reason": "fixture", "parent_id": work}, project)["record_id"]
    relative = "docs/pmt-docs/second-project.graph.json"
    step = call("save_step_directive", {"item_id": item, "title": "bounded metadata fixture",
        "directive": env["directive"], "kind": "investigate", "exploration_approved": True,
        "product_stage": "prototype", "requirements_version": "1", "plan_version": "draft1",
        "workspace": env["canonical"], "scopes": [{"kind": "path", "workspace": env["canonical"],
        "resource": relative}], "criteria": [{"id": "metadata", "meaning": "separate source"}]}, project)["step_id"]
    queued = call("enqueue_execution", {"step_id": step, "route": {"agent": "codex", "provider": "fixture",
        "model": "fixture", "mode": "cli", "adapter_kind": "cli", "selection_reason": "fixture only",
        "actual_support": "verified_supported", "auth_state": "authenticated", "max_concurrency": 3}}, project)
    run = queued["run_id"]
    prepared = call("prepare_execution", {"run_id": run, "expected_run_revision": queued["revision"]}, project)
    assert prepared["state"] == "starting", prepared
    graph = json.loads(canonical_json(env["graph"]))
    graph["project_id"] = project
    id_map = {node["id"]: new_id() for node in graph["nodes"]}
    for node in graph["nodes"]:
        node["id"] = id_map[node["id"]]
        node["work_item_step_refs"] = {"work": [work], "item": [item], "step": [step]}
    for relation in graph["relations"]:
        relation.update(id=new_id(), **{"from": id_map[relation["from"]], "to": id_map[relation["to"]]})
    graph_path = env["checkout"] / relative
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    pin = inspect_graph_source(env["checkout"], graph_path, env["repo"], project,
                               graph_scope_id=project)["source_pin"]
    resource = admin.publish_resource({"request_id": new_id(), "scope_id": project, "purpose": "graph_snapshot"},
        canonical_json(graph).encode("utf-8"), session_id=env["session_admin"])["artifact_ref"]
    publication_id = new_id()
    publication_body = {"repository_id": env["repo"], "project_id": project,
        "canonical_workspace": env["canonical"], "relative_graph_path": relative, "run_id": run,
        "expected_run_revision": prepared["revision"], "expected_source_revision": 0, "branch_key": "main",
        "source_pin": pin.to_dict(), "graph_resource_ref": resource}
    published = call("publish_source_snapshot", publication_body, project, publication_id)
    assert call("publish_source_snapshot", publication_body, project, publication_id) == published
    first = call("read_source_metadata", {"repository_id": env["repo"], "project_id": env["project"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"]}, env["project"])
    assert first["source_pin"] == env["pin"].to_dict()
    assert first["snapshot_ref"]["id"] != published["snapshot_ref"]["id"]
    with env["db"].write() as conn:
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (run,))
    metadata = call("read_source_metadata", {"repository_id": env["repo"], "project_id": project,
        "canonical_workspace": env["canonical"], "relative_graph_path": relative}, project)
    assert metadata["source_pin"] == pin.to_dict() and metadata["execution_authorized"] is False
    denied_request = _host_request(env, "read_source_metadata", {"repository_id": env["repo"],
        "project_id": project, "canonical_workspace": env["canonical"], "relative_graph_path": relative})
    denied_request["scope_id"] = project
    denied, code = env["store_a"].execute(denied_request)
    assert code == 3 and denied["error"]["code"] == "scope_forbidden"
