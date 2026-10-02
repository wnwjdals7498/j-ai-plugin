"""Isolated Git/SQLite acceptance tests for phase-three graph work."""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.planning.graph import validate_graph
from pmt.service import execute
from pmt.util import canonical_json, new_id, utc_now


def _node(node_id, tree_kind):
    base = {"id": node_id, "tree_kind": tree_kind, "node_kind": "goal", "summary": "Baseline node",
            "premise": "Keep the source meaning", "product_stage": "prototype",
            "product_scope": {"applies": False, "reason": "Scope check"},
            "autonomy": {"authority": "user", "scope": "Preserve intent"}}
    if tree_kind == "requirement":
        base.update(source_refs=["request:source"], criteria=["source is pinned"], evidence_refs=["evidence:one"])
    else:
        base.update(framework_assignment="Python", architecture="Graph source → validated index",
                    logging="IDs and hashes only", tests=["isolated Git test"],
                    function_spec={"input": "Graph", "output": "Index", "constraints": "No secrets",
                                   "invariants": "Stable IDs", "errors": "Conflict", "verification": "Git hash"},
                    choice_set={"options": [], "insufficient_reason": "Existing graph fixture"},
                    choice={"source": "user", "selected": "SQLite", "reason": "Current contract",
                            "scope": "Local index"})
    return base


@pytest.fixture
def graph_env(tmp_path, request):
    # Keep paths short enough for Git's nested object paths on Windows while
    # retaining each test's data beneath the requested pytest basetemp root.
    temporary = tempfile.TemporaryDirectory(prefix="p3g-", dir=tmp_path.parents[1])
    temp_root = Path(temporary.name)
    request.addfinalizer(temporary.cleanup)
    workspace = temp_root / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "PMT test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "pmt-test@example.invalid"], check=True)
    scope_id, repository_id, req_id, impl_id = (new_id() for _ in range(4))
    implements_relation_id = new_id()
    graph = {"schema_version": 1, "project_id": scope_id, "graph_version": 1,
             "nodes": [_node(req_id, "requirement"), _node(impl_id, "implementation")],
             "relations": [{"id": implements_relation_id, "kind": "implements", "from": req_id, "to": impl_id}],
             "provenance": {"request_ref": "request:test"}}
    path = workspace / "docs" / "pmt-docs" / "plan.graph.json"
    path.parent.mkdir(parents=True)
    path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "docs/pmt-docs/plan.graph.json"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "fixture graph"], check=True)

    db = Database(temp_root / "data", temp_root / "config")
    run_id, job_id, step_id = new_id(), new_id(), new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (repository_id, "repository", "repo", now, now))
        conn.execute("UPDATE scopes SET parent_id=? WHERE id=?", (repository_id, scope_id)) if False else None
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (scope_id, "project", repository_id, "project", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (step_id, "step", scope_id, "Graph test", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (job_id, step_id, "running", "{}", now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run_id, job_id, step_id, 1, "running", 1, "graph-session", 1, str(workspace), "[]", "{}", "{}", now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run_id, "graph-session", "path", str(workspace), "docs/pmt-docs/plan.graph.json", now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run_id, "graph-session", "path", str(workspace), "docs/pmt-docs/plan.md", now))
    return {"db": db, "workspace": workspace, "scope_id": scope_id, "repository_id": repository_id,
            "run_id": run_id, "graph": graph, "graph_path": "docs/pmt-docs/plan.graph.json",
            "req_id": req_id, "impl_id": impl_id, "implements_relation_id": implements_relation_id}


def _request(env, operation, **payload):
    return {"protocol_version": 1, "operation": operation, "request_id": new_id(),
            "actor": "graph-test", "session_id": "graph-session", "scope_id": env["scope_id"],
            "payload": {"repository_id": env["repository_id"], "workspace": str(env["workspace"]),
                        "relative_graph_path": env["graph_path"], "run_id": env["run_id"], **payload}}


def _invoke(env, operation, **payload):
    response, code = execute(env["db"], _request(env, operation, **payload))
    return response, code


def _change_set(env, *, change_id=None):
    return {"change_id": change_id or new_id(), "reason": "Preserve the reviewed graph basis",
            "evidence_refs": ["evidence:review"], "changes": [
        {"op": "create", "temp_id": "temp:new-req", "inherit_from": env["req_id"],
         "node": {"summary": "Inherited requirement"}},
        {"op": "update", "id": env["req_id"], "fields": {"premise": "Revised premise"},
         "clear": ["evidence_refs"]},
        {"op": "relate", "relation": {"kind": "parent", "from": env["req_id"], "to": "temp:new-req"}},
    ]}


def test_source_pin_and_preview_create_inherit_clear_without_mutation(graph_env):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0 and captured["ok"]
    pin = captured["result"]["source_pin"]
    change_set = _change_set(graph_env)
    before = (graph_env["workspace"] / graph_env["graph_path"]).read_bytes()
    first_req, second_req = _request(graph_env, "preview_graph_change", expected_source=pin, change_set=change_set), _request(
        graph_env, "preview_graph_change", expected_source=pin, change_set=change_set)
    first, code1 = execute(graph_env["db"], first_req)
    second, code2 = execute(graph_env["db"], second_req)
    assert code1 == code2 == 0
    a, b = first["result"], second["result"]
    assert a["change_id"] == change_set["change_id"]
    assert a["temp_id_map"] == b["temp_id_map"]
    assert a["expected_new_source"]["graph_revision"] == 2
    assert (graph_env["workspace"] / graph_env["graph_path"]).read_bytes() == before
    assert a["operations"][1]["fields"] == ["premise"] and a["operations"][1]["clear"] == ["evidence_refs"]


def test_apply_publishes_uuid_relations_and_replays_across_new_request_id(graph_env):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    pin = captured["result"]["source_pin"]
    change_set = _change_set(graph_env)
    preview_req = _request(graph_env, "preview_graph_change", expected_source=pin, change_set=change_set)
    preview, code = execute(graph_env["db"], preview_req)
    assert code == 0
    original_bytes = (graph_env["workspace"] / graph_env["graph_path"]).read_bytes()
    apply_req = _request(graph_env, "apply_graph_change", expected_source=pin, change_set=change_set)
    applied, code = execute(graph_env["db"], apply_req)
    assert code == 0 and applied["ok"]
    result = applied["result"]
    assert result["source_pin"]["graph_revision"] == 2
    assert result["before_source_pin"]["source_hash"] == pin["source_hash"]
    assert result["temp_id_map"] == preview["result"]["temp_id_map"]
    graph_path = graph_env["workspace"] / graph_env["graph_path"]
    disk = json.loads(graph_path.read_text(encoding="utf-8"))
    report = validate_graph(disk, graph_env["scope_id"], complete=False)
    assert report["sha256"] == result["graph_hash"]
    new_id = result["temp_id_map"]["temp:new-req"]
    child = next(node for node in disk["nodes"] if node["id"] == new_id)
    parent_relation = next(rel for rel in disk["relations"] if rel["to"] == new_id)
    old = next(node for node in disk["nodes"] if node["id"] == graph_env["req_id"])
    assert child["summary"] == "Inherited requirement" and child["premise"] == "Keep the source meaning"
    assert parent_relation["from"] == old["id"] and "evidence_refs" not in old
    replay, replay_code = execute(graph_env["db"], apply_req)
    assert replay_code == 0 and replay == applied
    with closing(graph_env["db"].connect()) as conn:
        index = conn.execute("SELECT source_hash,revision,body_json FROM phase3_objects WHERE kind='graph_index' AND id=?",
                             (graph_env["scope_id"],)).fetchone()
        assert index["source_hash"] == result["source_pin"]["source_hash"]
        journal = conn.execute("SELECT body_json FROM phase3_journal WHERE kind='graph_change' AND request_id=?",
                               (apply_req["request_id"],)).fetchone()
        assert json.loads(journal[0])["stage"] == "completed"
        event_id = str(uuid.uuid5(uuid.UUID(apply_req["request_id"]), "phase3.graph_change"))
        event = conn.execute("SELECT event_type,old_revision,new_revision,payload_json FROM events WHERE event_id=?",
                             (event_id,)).fetchone()
        assert event["event_type"] == "planning.graph_change_published"
        assert (event["old_revision"], event["new_revision"]) == (1, 2)
        assert json.loads(event["payload_json"])["graph_hash"] == result["graph_hash"]
    recovery_copy = graph_env["workspace"] / Path(result["recovery_ref"])
    assert recovery_copy.is_file() and recovery_copy.read_bytes() == original_bytes


def test_stale_source_and_uncovered_claim_fail_without_partial_write(graph_env):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    pin = captured["result"]["source_pin"]
    change_set = _change_set(graph_env)
    graph_path = graph_env["workspace"] / graph_env["graph_path"]
    before = graph_path.read_bytes()
    externally_edited = json.loads(before)
    externally_edited["nodes"][0]["summary"] = "Manual edit wins"
    graph_path.write_text(canonical_json(externally_edited) + "\n", encoding="utf-8")
    stale, stale_code = _invoke(graph_env, "preview_graph_change", expected_source=pin, change_set=change_set)
    assert stale_code == 3 and stale["error"]["code"] == "source_conflict"
    edited_bytes = graph_path.read_bytes()
    assert edited_bytes != before and json.loads(edited_bytes)["nodes"][0]["summary"] == "Manual edit wins"

    (graph_env["workspace"] / "outside.json").write_text("{}", encoding="utf-8")
    request = _request(graph_env, "capture_source_pin")
    request["payload"]["relative_graph_path"] = "outside.json"
    response, denied_code = execute(graph_env["db"], request)
    assert denied_code == 3 and response["error"]["code"] in {"scope_not_owned", "invalid_graph_path"}


def test_invalid_change_reference_and_duplicate_ids_have_no_partial_effect(graph_env):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    pin = captured["result"]["source_pin"]
    graph_path = graph_env["workspace"] / graph_env["graph_path"]
    before = graph_path.read_bytes()
    invalid = {"change_id": new_id(), "reason": "Reject an incomplete relation", "changes": [
        {"op": "relate", "relation": {"kind": "depends_on", "from": graph_env["req_id"], "to": new_id()}}]}
    result, exit_code = _invoke(graph_env, "apply_graph_change", expected_source=pin, change_set=invalid)
    assert exit_code == 2 and result["error"]["code"] == "plan_graph_invalid_reference"
    assert graph_path.read_bytes() == before
    assert not list(graph_env["workspace"].glob("*.pmt-*"))


def test_apply_recovers_after_target_detach_before_candidate_publish(graph_env, monkeypatch):
    import pmt.efficiency.graph as graph_module
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    pin = captured["result"]["source_pin"]
    change_set = _change_set(graph_env)
    apply_req = _request(graph_env, "apply_graph_change", expected_source=pin, change_set=change_set)
    update = graph_module.Phase3Storage.update_intent

    def stop_after_replace(self, intent_id, expected_stage, stage, *args, **kwargs):
        if expected_stage == "prepared" and stage == "original_preserved":
            raise OSError("simulated process interruption after target detach")
        return update(self, intent_id, expected_stage, stage, *args, **kwargs)

    monkeypatch.setattr(graph_module.Phase3Storage, "update_intent", stop_after_replace)
    interrupted, interrupted_code = execute(graph_env["db"], apply_req)
    assert interrupted_code == 4 and interrupted["error"]["retryable"] is True
    monkeypatch.setattr(graph_module.Phase3Storage, "update_intent", update)
    with closing(graph_env["db"].connect()) as conn:
        row = conn.execute("SELECT body_json,outcome_json FROM phase3_journal WHERE kind='graph_change' AND request_id=?",
                           (apply_req["request_id"],)).fetchone()
        assert json.loads(row[0])["stage"] == "prepared"
        recovery_ref = json.loads(row[1])["recovery_ref"]
    assert not (graph_env["workspace"] / graph_env["graph_path"]).exists()
    assert (graph_env["workspace"] / Path(recovery_ref)).is_file()
    recovery = _request(graph_env, "recover_graph_change", original_request=apply_req)
    recovered, recovery_code = execute(graph_env["db"], recovery)
    assert recovery_code == 0 and recovered["ok"], recovered.get("error")
    replay, replay_code = execute(graph_env["db"], apply_req)
    assert replay_code == 0 and replay["result"]["graph_hash"] == recovered["result"]["change_result"]["graph_hash"]
    with closing(graph_env["db"].connect()) as conn:
        journal = conn.execute("SELECT body_json FROM phase3_journal WHERE kind='graph_change' AND request_id=?",
                               (apply_req["request_id"],)).fetchone()
        assert json.loads(journal[0])["stage"] == "completed"


def test_two_process_graph_writers_cas_and_preserve_one_result(graph_env):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    expected = captured["result"]["source_pin"]
    change_sets = [_change_set(graph_env), _change_set(graph_env)]
    requests = [_request(graph_env, "apply_graph_change", expected_source=expected, change_set=change_set)
                for change_set in change_sets]
    code_text = """import json,sys
from pmt.db import Database
from pmt.service import execute
d=Database(sys.argv[1],sys.argv[2]); req=json.loads(sys.argv[3]); result,code=execute(d,req)
print(json.dumps({'code':code,'ok':result.get('ok'),'error':(result.get('error') or {}).get('code')}))
"""
    checkout = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(checkout / "src")
    processes = [subprocess.Popen([sys.executable, "-c", code_text, str(graph_env["db"].root),
                                   str(graph_env["db"].config_root), json.dumps(req)], cwd=checkout,
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for req in requests]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    results = [json.loads(stdout) for stdout, _ in outputs]
    assert sum(result["code"] == 0 for result in results) == 1, results
    assert all(result["code"] in {0, 3} for result in results), results
    with closing(graph_env["db"].connect()) as conn:
        index = conn.execute("SELECT body_json FROM phase3_objects WHERE kind='graph_index' AND id=?",
                             (graph_env["scope_id"],)).fetchone()
        graph = json.loads(index[0])
        request_count = conn.execute("SELECT count(*) FROM requests WHERE request_id IN (?,?)",
                                     tuple(req["request_id"] for req in requests)).fetchone()[0]
    assert len(graph["nodes"]) == 3 and request_count in {1, 2}


def test_non_git_source_is_explicit_and_dirty_state_stays_unknown(graph_env):
    with tempfile.TemporaryDirectory(prefix="pmt-non-git-") as temp:
        target = Path(temp)
        destination = target / graph_env["graph_path"]
        destination.parent.mkdir(parents=True)
        shutil.copy2(graph_env["workspace"] / graph_env["graph_path"], destination)
        with graph_env["db"].write() as conn:
            conn.execute("UPDATE execution_runs SET workspace=? WHERE id=?", (str(target), graph_env["run_id"]))
            conn.execute("UPDATE scope_locks SET workspace=? WHERE run_id=?", (str(target), graph_env["run_id"]))
        request = _request(graph_env, "capture_source_pin")
        request["payload"]["workspace"] = str(target)
        source, code = execute(graph_env["db"], request)
        assert code == 0 and source["ok"]
        pin = source["result"]["source_pin"]
        assert pin["source_kind"] == "non_git" and pin["reviewed_commit"] is None
        assert pin["dirty_state"] == "unknown" and pin["dirty_fingerprint"] is None


def test_standalone_project_uses_explicit_repository_and_workspace_mapping(graph_env):
    body = {"repository_id": graph_env["repository_id"], "workspace": str(graph_env["workspace"])}
    with graph_env["db"].write() as conn:
        conn.execute("UPDATE scopes SET parent_id=NULL,body_json=? WHERE id=?",
                     (canonical_json(body), graph_env["scope_id"]))
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0 and captured["ok"]
    pin = captured["result"]["source_pin"]
    assert pin["project_id"] == graph_env["scope_id"]
    assert pin["repository_id"] == graph_env["repository_id"]
    assert pin["source_kind"] == "git"


def test_source_kind_derivation_and_temp_alias_cannot_shadow_stable_uuid(graph_env):
    from pmt.efficiency.source import SourcePin, pin_source
    from pmt.efficiency.graph import prepare_change_set
    base = SourcePin(graph_env["repository_id"], graph_env["scope_id"], None, None, 1, 1, "hash", "unknown")
    assert base.to_dict()["source_kind"] == "non_git"
    with pytest.raises(PmtError) as invalid_kind:
        pin_source(base.to_dict() | {"source_kind": "git"})
    assert invalid_kind.value.code == "source_pin_invalid"
    bad = {"change_id": new_id(), "reason": "Test alias collision", "changes": [
        {"op": "create", "temp_id": graph_env["req_id"], "inherit_from": graph_env["req_id"], "node": {"summary": "shadow"}}]}
    with pytest.raises(PmtError) as collision:
        prepare_change_set(graph_env["graph"], bad, new_id())
    assert collision.value.code == "change_set_invalid"
    stable = {"change_id": new_id(), "reason": "Test stable identifier", "changes": [
        {"op": "update", "id": graph_env["req_id"], "fields": {"summary": "stable wins"}}]}
    changed = prepare_change_set(graph_env["graph"], stable, new_id())
    assert next(node for node in changed["graph"]["nodes"] if node["id"] == graph_env["req_id"])["summary"] == "stable wins"


def test_f2_pure_slice_reverse_direction_paging_and_stale_cursor(graph_env):
    from pmt.efficiency.graph import _index_body, query_graph_slice
    from pmt.efficiency.source import SourcePin
    graph = copy.deepcopy(graph_env["graph"])
    child_id = new_id()
    child = copy.deepcopy(graph["nodes"][0])
    child["id"] = child_id
    child["summary"] = "Child"
    graph["nodes"].append(child)
    relation = {"id": new_id(), "kind": "parent", "from": graph_env["req_id"], "to": child_id}
    graph["relations"].append(relation)
    graph["graph_version"] = 2
    report = validate_graph(graph, graph_env["scope_id"], complete=False)
    pin = SourcePin(graph_env["repository_id"], graph_env["scope_id"], "main", "a" * 40, 1, 2,
                    report["sha256"], "clean")
    index = _index_body(graph, pin)
    first = query_graph_slice(index, {"node_ids": [child_id], "direction": "incoming", "max_depth": 2,
                                      "page_size": 2}, pin)
    assert any(item["entity"] == "node" and item["value"]["id"] == graph_env["req_id"] for item in first["items"])
    second = query_graph_slice(index, {"node_ids": [child_id], "direction": "incoming", "max_depth": 2,
                                       "page_size": 2, "cursor": first["next_cursor"]}, pin)
    assert first["index"]["hash"] == second["index"]["hash"]
    if first["next_cursor"]:
        with pytest.raises(PmtError) as wrong_query:
            query_graph_slice(index, {"node_ids": [child_id], "direction": "outgoing", "max_depth": 2,
                                      "page_size": 2, "cursor": first["next_cursor"]}, pin)
        assert wrong_query.value.code == "graph_cursor_stale"
        other_pin = SourcePin(graph_env["repository_id"], graph_env["scope_id"], "main", "b" * 40, 1, 2,
                              report["sha256"], "clean")
        with pytest.raises(PmtError) as stale:
            query_graph_slice(index, {"node_ids": [child_id], "direction": "incoming", "max_depth": 2,
                                      "page_size": 2, "cursor": first["next_cursor"]}, other_pin)
        assert stale.value.code == "graph_index_stale"
    corrupt = copy.deepcopy(index)
    corrupt["nodes"].pop(graph_env["req_id"])
    with pytest.raises(PmtError) as damaged:
        query_graph_slice(corrupt, {"node_ids": [child_id]}, pin)
    assert damaged.value.code == "graph_index_corrupt"

    cyclic = copy.deepcopy(graph)
    cyclic["relations"].extend([
        {"id": new_id(), "kind": "evidence", "from": graph_env["req_id"], "to": graph_env["impl_id"],
         "evidence_ref": "evidence:a"},
        {"id": new_id(), "kind": "evidence", "from": graph_env["impl_id"], "to": graph_env["req_id"],
         "evidence_ref": "evidence:b"},
    ])
    cyclic["graph_version"] += 1
    cyclic_report = validate_graph(cyclic, graph_env["scope_id"], complete=False)
    cyclic_pin = SourcePin(graph_env["repository_id"], graph_env["scope_id"], "main", "c" * 40, 1,
                           cyclic["graph_version"], cyclic_report["sha256"], "clean")
    cyclic_slice = query_graph_slice(_index_body(cyclic, cyclic_pin),
                                     {"node_ids": [graph_env["req_id"]], "direction": "both",
                                      "max_depth": 8, "page_size": 100}, cyclic_pin)
    assert cyclic_slice["traversal_complete"] is True
    assert len({item["value"]["id"] for item in cyclic_slice["items"] if item["entity"] == "node"}) == 3


def test_f2_depth_limit_includes_last_level_and_reports_only_real_truncation():
    from pmt.efficiency.graph import _index_body, query_graph_slice
    from pmt.efficiency.source import SourcePin

    repo_id, project_id = new_id(), new_id()
    ids = [new_id() for _ in range(4)]
    nodes = [{"id": node_id, "tree_kind": "requirement" if index % 2 == 0 else "implementation",
              "node_kind": "goal", "summary": f"node {index}"} for index, node_id in enumerate(ids)]
    relations = [{"id": new_id(), "kind": "parent", "from": ids[0], "to": ids[1]},
                 {"id": new_id(), "kind": "implements", "from": ids[1], "to": ids[2]},
                 {"id": new_id(), "kind": "depends_on", "from": ids[2], "to": ids[3]}]
    pin = SourcePin(repo_id, project_id, "main", "a" * 40, 1, 1, "b" * 64, "clean")
    query = {"node_ids": ids[:3], "relation_kinds": ["parent", "implements", "depends_on"],
             "direction": "both", "max_depth": 2, "page_size": 100}

    full_graph = {"schema_version": 1, "project_id": project_id, "graph_version": 1,
                  "nodes": nodes[:3], "relations": relations[:2]}
    full = query_graph_slice(_index_body(full_graph, pin), query, pin)
    assert full["traversal_complete"] is True
    assert not any(item.get("reason_code") == "traversal_limit_or_depth" for item in full["unknown"])
    assert {item["value"]["id"] for item in full["items"] if item["entity"] == "node"} == set(ids[:3])

    truncated_graph = {"schema_version": 1, "project_id": project_id, "graph_version": 1,
                       "nodes": nodes, "relations": relations}
    truncated = query_graph_slice(_index_body(truncated_graph, pin),
        {**query, "node_ids": [ids[0]]}, pin)
    assert truncated["traversal_complete"] is False
    assert {item["value"]["id"] for item in truncated["items"] if item["entity"] == "node"} == set(ids[:3])
    unknown = [item for item in truncated["unknown"]
               if item.get("reason_code") == "traversal_limit_or_depth"]
    assert len(unknown) == 1 and unknown[0]["node_ids"] == [ids[3]]

    expanded = query_graph_slice(_index_body(truncated_graph, pin),
        {**query, "node_ids": [ids[0]], "max_depth": 3}, pin)
    assert expanded["traversal_complete"] is True
    assert {item["value"]["id"] for item in expanded["items"] if item["entity"] == "node"} == set(ids)


def test_f2_rebuild_query_two_tree_relation_and_register_manifest(graph_env):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    pin = captured["result"]["source_pin"]
    rebuilt, rebuild_code = _invoke(graph_env, "rebuild_graph_index", expected_source=pin)
    assert rebuild_code == 0 and rebuilt["ok"]
    assert rebuilt["result"]["index"]["hash"]
    first, query_code = _invoke(graph_env, "query_graph", expected_source=pin,
                                query={"node_ids": [graph_env["req_id"]], "direction": "outgoing",
                                       "relation_kinds": ["implements"], "max_depth": 2,
                                       "page_size": 1})
    assert query_code == 0 and first["ok"]
    graph_slice = first["result"]["graph_slice"]
    items = list(graph_slice["items"])
    cursor = graph_slice["next_cursor"]
    while cursor:
        page, code = _invoke(graph_env, "query_graph", expected_source=pin,
                             query={"node_ids": [graph_env["req_id"]], "direction": "outgoing",
                                    "relation_kinds": ["implements"], "max_depth": 2,
                                    "page_size": 1, "cursor": cursor})
        assert code == 0
        items.extend(page["result"]["graph_slice"]["items"])
        cursor = page["result"]["graph_slice"]["next_cursor"]
    assert {item["value"]["id"] for item in items if item["entity"] == "node"} == {
        graph_env["req_id"], graph_env["impl_id"]}
    assert any(item["entity"] == "relation" and item["value"]["kind"] == "implements" for item in items)

    manifest = {"segment_id": new_id(), "document_id": "docs/pmt-docs/plan.md",
                "node_ids": [graph_env["req_id"]], "field_paths": ["premise", "criteria"],
                "relation_ids": [], "template_version": "baseline-1", "output_hash": "a" * 64,
                "ownership": "generated", "source_pin": pin}
    registered, register_code = _invoke(graph_env, "register_segment_manifest", expected_source=pin,
                                        expected_manifest_revision=0, manifest=manifest)
    assert register_code == 0 and registered["ok"]
    assert registered["result"]["manifest_revision"] == 1
    revised_manifest = copy.deepcopy(manifest) | {"output_hash": "b" * 64}
    revised, revise_code = _invoke(graph_env, "register_segment_manifest", expected_source=pin,
                                   expected_manifest_revision=1, manifest=revised_manifest)
    assert revise_code == 0 and revised["result"]["manifest_revision"] == 2
    conflict, conflict_code = _invoke(graph_env, "register_segment_manifest", expected_source=pin,
                                      expected_manifest_revision=1, manifest=manifest)
    assert conflict_code == 3 and conflict["error"]["code"] == "revision_conflict"
    page, page_code = _invoke(graph_env, "query_graph", expected_source=pin,
                              query={"node_ids": [graph_env["req_id"]], "page_size": 10})
    assert page_code == 0
    refs = page["result"]["graph_slice"]["segment_manifests"]
    assert len(refs) == 1 and refs[0]["segment_id"] == manifest["segment_id"]
    assert refs[0]["revision"] == 2 and refs[0]["output_hash"] == "b" * 64
    assert page["result"]["graph_slice"]["coverage"]["segments"] == "unknown"


def test_f2_stale_and_damaged_index_are_explicitly_rebuilt(graph_env):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    old_pin = captured["result"]["source_pin"]
    rebuilt, code = _invoke(graph_env, "rebuild_graph_index", expected_source=old_pin)
    assert code == 0
    graph_path = graph_env["workspace"] / graph_env["graph_path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["premise"] = "An externally changed but valid premise"
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    new_capture, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    new_pin = new_capture["result"]["source_pin"]
    stale, stale_code = _invoke(graph_env, "query_graph", expected_source=new_pin,
                                query={"node_ids": [graph_env["req_id"]]})
    assert stale_code == 3 and stale["error"]["code"] == "graph_index_stale"
    assert stale["error"]["details"]["rebuild_required"] is True
    rebuilt, code = _invoke(graph_env, "rebuild_graph_index", expected_source=new_pin)
    assert code == 0 and rebuilt["ok"]
    with graph_env["db"].write() as conn:
        conn.execute("UPDATE phase3_objects SET body_json='{}' WHERE kind='graph_index' AND id=?",
                     (graph_env["scope_id"],))
    damaged, damaged_code = _invoke(graph_env, "query_graph", expected_source=new_pin,
                                    query={"node_ids": [graph_env["req_id"]]})
    assert damaged_code == 3 and damaged["error"]["code"] == "graph_index_corrupt"
    rebuilt, code = _invoke(graph_env, "rebuild_graph_index", expected_source=new_pin)
    assert code == 0 and rebuilt["ok"]


def test_f2_manifest_cas_serializes_independent_processes(graph_env):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    pin = captured["result"]["source_pin"]
    assert _invoke(graph_env, "rebuild_graph_index", expected_source=pin)[1] == 0
    manifest = {"segment_id": new_id(), "document_id": "docs/pmt-docs/plan.md",
                "node_ids": [graph_env["req_id"]], "field_paths": ["premise"], "relation_ids": [],
                "template_version": "t1", "output_hash": "0" * 64,
                "ownership": "generated", "source_pin": pin}
    initial, code = _invoke(graph_env, "register_segment_manifest", expected_source=pin,
                            expected_manifest_revision=0, manifest=manifest)
    assert code == 0 and initial["result"]["manifest_revision"] == 1
    requests = []
    for char in ("c", "d"):
        value = copy.deepcopy(manifest) | {"output_hash": char * 64}
        requests.append(_request(graph_env, "register_segment_manifest", expected_source=pin,
                                 expected_manifest_revision=1, manifest=value))
    code_text = ("import json,sys; from pmt.db import Database; from pmt.service import execute; "
                 "d=Database(sys.argv[1],sys.argv[2]); result,code=execute(d,json.loads(sys.argv[3])); "
                 "print(json.dumps({'code':code,'ok':result.get('ok'),'error':(result.get('error') or {}).get('code')}))")
    checkout = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(checkout / "src")
    processes = [subprocess.Popen([sys.executable, "-c", code_text, str(graph_env["db"].root),
                                   str(graph_env["db"].config_root), json.dumps(request)], cwd=checkout,
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for request in requests]
    outputs = [process.communicate(timeout=30) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    results = [json.loads(stdout) for stdout, _ in outputs]
    assert sorted(result["code"] for result in results) == [0, 3]
    assert [result["error"] for result in results if result["code"] == 3] == ["revision_conflict"]
    with closing(graph_env["db"].connect()) as conn:
        row = conn.execute("SELECT body_json,revision FROM phase3_objects WHERE kind='segment_manifest' AND id=?",
                           (manifest["segment_id"],)).fetchone()
    assert row["revision"] == 2 and json.loads(row["body_json"])["output_hash"] in {"c" * 64, "d" * 64}


def test_f2_query_closes_database_handle_after_live_source_read(graph_env):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    pin = captured["result"]["source_pin"]
    assert _invoke(graph_env, "rebuild_graph_index", expected_source=pin)[1] == 0
    query, code = _invoke(graph_env, "query_graph", expected_source=pin,
                          query={"node_ids": [graph_env["req_id"]], "page_size": 10})
    assert code == 0 and query["ok"]
    db_path = graph_env["db"].path
    moved = db_path.with_name(db_path.name + ".closed-handle-check")
    db_path.rename(moved)
    moved.rename(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def _impact_preview(graph_env, change_set):
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    pin = captured["result"]["source_pin"]
    rebuilt, code = _invoke(graph_env, "rebuild_graph_index", expected_source=pin)
    assert code == 0 and rebuilt["ok"]
    preview, code = _invoke(graph_env, "preview_graph_change", expected_source=pin, change_set=change_set)
    assert code == 0 and preview["ok"]
    return pin, preview["result"], change_set


def test_f3_typed_fields_relationship_consumers_and_unknown_manifest_coverage(graph_env):
    graph_path = graph_env["workspace"] / graph_env["graph_path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["work_item_step_refs"] = {"work": [], "item": [], "step": ["step:followup"]}
    unrelated_id = new_id()
    graph["nodes"].append(_node(unrelated_id, "requirement"))
    graph["relations"].extend([
        {"id": new_id(), "kind": "depends_on", "from": graph_env["impl_id"], "to": graph_env["req_id"]},
        {"id": new_id(), "kind": "evidence", "from": graph_env["req_id"], "to": unrelated_id,
         "evidence_ref": "evidence:dynamic-owner"},
    ])
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(graph_env["workspace"]), "add", graph_env["graph_path"]], check=True)
    subprocess.run(["git", "-C", str(graph_env["workspace"]), "commit", "-qm", "impact fixture"], check=True)
    change = {"change_id": new_id(), "reason": "Revise requirement meaning",
              "changes": [{"op": "update", "id": graph_env["req_id"],
                           "fields": {"premise": "New goal premise", "summary": "Goal may have changed"}}]}
    pin, preview, change_set = _impact_preview(graph_env, change)
    impact, code = _invoke(graph_env, "calculate_graph_impact", expected_source=pin,
                           change_preview=preview, change_set=change_set, max_depth=4)
    assert code == 0 and impact["ok"]
    result = impact["result"]
    assert result["before_source_pin"] == pin
    assert result["rule_version"] == "graph-field-semantics-1"
    assert result["change_id"] == change["change_id"]
    assert result["change_set_hash"] == preview["change_set_hash"]
    assert result["expected_new_source"] == preview["expected_new_source"]
    semantics = {item["field"]: item["semantic"] for item in result["field_changes"]}
    assert semantics["premise"] == "premise" and semantics["summary"] == "unknown"
    known_ids = {item["node_id"] for item in result["known"]}
    assert graph_env["req_id"] in known_ids and graph_env["impl_id"] in known_ids
    assert unrelated_id not in known_ids
    assert result["steps"] == ["step:followup"]
    reasons = {item["reason_code"] for item in result["unknown"]}
    assert {"field_semantics_require_review", "evidence_relation_direction_unknown",
            "segment_manifest_missing", "baseline_certificate_missing_or_stale"}.issubset(reasons)
    assert result["complete"] is False
    rendered = json.dumps(result, sort_keys=True)
    assert "New goal premise" not in rendered and "Goal may have changed" not in rendered
    repeated, repeated_code = _invoke(graph_env, "calculate_graph_impact", expected_source=pin,
                                      change_preview=preview, change_set=change_set, max_depth=4)
    assert repeated_code == 0 and repeated["result"] == result

    unlink = {"change_id": new_id(), "reason": "Review an implementation link removal",
              "changes": [{"op": "unrelate", "relation_id": graph_env["implements_relation_id"]}]}
    unlink_pin, unlink_preview, unlink_change = _impact_preview(graph_env, unlink)
    unlink_impact, unlink_code = _invoke(graph_env, "calculate_graph_impact", expected_source=unlink_pin,
                                         change_preview=unlink_preview, change_set=unlink_change, max_depth=4)
    assert unlink_code == 0
    unlink_ids = {item["node_id"] for item in unlink_impact["result"]["known"]}
    assert graph_env["req_id"] in unlink_ids and graph_env["impl_id"] in unlink_ids


def test_f3_unknowns_and_unsupported_rule_are_explicit(graph_env):
    change = {"change_id": new_id(), "reason": "Check semantic uncertainty",
              "changes": [{"op": "update", "id": graph_env["req_id"],
                           "fields": {"summary": "Potentially semantic summary"}}]}
    pin, preview, change_set = _impact_preview(graph_env, change)
    tampered = copy.deepcopy(preview)
    tampered["operations"][0]["fields"] = []
    mismatch, mismatch_code = _invoke(graph_env, "calculate_graph_impact", expected_source=pin,
                                      change_preview=tampered, change_set=change_set)
    assert mismatch_code == 3 and mismatch["error"]["code"] == "change_preview_conflict"
    unsupported, code = _invoke(graph_env, "calculate_graph_impact", expected_source=pin,
                                change_preview=preview, change_set=change_set, rule_version="unknown-rules-9")
    assert code == 3 and unsupported["error"]["code"] == "impact_rule_version_unsupported"
    too_deep, depth_code = _invoke(graph_env, "calculate_graph_impact", expected_source=pin,
                                   change_preview=preview, change_set=change_set, max_depth=9)
    assert depth_code == 2 and too_deep["error"]["code"] == "impact_request_invalid"
    impact, code = _invoke(graph_env, "calculate_graph_impact", expected_source=pin,
                           change_preview=preview, change_set=change_set, max_depth=4)
    assert code == 0
    assert any(item["reason_code"] == "field_semantics_require_review" for item in impact["result"]["unknown"])
    graph_path = graph_env["workspace"] / graph_env["graph_path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["premise"] = "Changed after the preview"
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    stale, stale_code = _invoke(graph_env, "calculate_graph_impact", expected_source=pin,
                                change_preview=preview, change_set=change_set, max_depth=4)
    assert stale_code == 3 and stale["error"]["code"] == "source_conflict"


def test_f3_complete_baseline_certificate_turns_pinned_dependencies_known(graph_env):
    from pmt.efficiency.graph import manifest_set_fingerprint
    captured, code = _invoke(graph_env, "capture_source_pin")
    assert code == 0
    pin = captured["result"]["source_pin"]
    assert _invoke(graph_env, "rebuild_graph_index", expected_source=pin)[1] == 0
    path = "docs/pmt-docs/plan.md"
    manifests = [
        {"segment_id": new_id(), "document_id": path, "document_path": path,
         "node_ids": [graph_env["req_id"]], "field_paths": ["premise"], "relation_ids": [],
         "template_version": "baseline-v1", "output_hash": "1" * 64,
         "ownership": "generated", "source_pin": pin},
        {"segment_id": new_id(), "document_id": path, "document_path": path,
         "node_ids": [graph_env["impl_id"]], "field_paths": ["architecture"], "relation_ids": [],
         "template_version": "baseline-v1", "output_hash": "2" * 64,
         "ownership": "generated", "source_pin": pin},
        {"segment_id": new_id(), "document_id": path, "document_path": path,
         "node_ids": [], "field_paths": [], "relation_ids": [graph_env["implements_relation_id"]],
         "template_version": "baseline-v1", "output_hash": "3" * 64,
         "ownership": "generated", "source_pin": pin},
    ]
    refs = []
    for manifest in manifests:
        registered, register_code = _invoke(graph_env, "register_segment_manifest", expected_source=pin,
                                            expected_manifest_revision=0, manifest=manifest)
        assert register_code == 0 and registered["ok"]
        refs.append({"segment_id": manifest["segment_id"], "manifest_hash": registered["result"]["manifest_hash"]})
    certificate = {"source_pin": pin, "document_paths": [path], "manifest_refs": refs,
                   "manifest_set_hash": manifest_set_fingerprint(refs, [path], pin),
                   "expected_node_ids": sorted([graph_env["req_id"], graph_env["impl_id"]]),
                   "expected_relation_ids": [graph_env["implements_relation_id"]],
                   "expected_field_paths": {graph_env["req_id"]: ["premise"],
                                             graph_env["impl_id"]: ["architecture"]},
                   "dependency_registry_version": "deps-v1", "template_versions": ["baseline-v1"],
                   "production_receipt_ref": "receipt:baseline-1", "source_table_version": "graph-schema-1"}
    baseline, baseline_code = _invoke(graph_env, "register_segment_manifest", expected_coverage_revision=0,
                                      coverage_certificate=certificate)
    assert baseline_code == 0 and baseline["ok"]
    assert baseline["result"]["coverage"]["segments"] == "complete"
    change = {"change_id": new_id(), "reason": "Update a documented premise",
              "changes": [{"op": "update", "id": graph_env["req_id"],
                           "fields": {"premise": "A different premise"}}]}
    pinned, preview, change_set = _impact_preview(graph_env, change)
    impact, impact_code = _invoke(graph_env, "calculate_graph_impact", expected_source=pinned,
                                  change_preview=preview, change_set=change_set)
    assert impact_code == 0
    result = impact["result"]
    assert result["complete"] is True
    assert result["expected_new_source"]["graph_hash"] == preview["expected_new_source"]["graph_hash"]
    assert {item["segment_id"] for item in result["documents"]} == {entry["segment_id"] for entry in manifests[:2]}
    assert result["unknown"] == []
    applied, apply_code = _invoke(graph_env, "apply_graph_change", expected_source=pin, change_set=change)
    assert apply_code == 0 and applied["ok"]
    receipt = applied["result"]
    assert receipt["change_id"] == result["change_id"]
    assert receipt["change_set_hash"] == result["change_set_hash"]
    assert receipt["before_source_pin"]["source_hash"] == result["before_source_pin"]["source_hash"]
    assert receipt["source_pin"]["graph_hash"] == result["expected_new_source"]["graph_hash"]
    current, current_code = _invoke(graph_env, "capture_source_pin")
    assert current_code == 0 and current["result"]["source_pin"]["source_hash"] == receipt["source_pin"]["source_hash"]


def test_module_exposes_f1_contract_and_no_user_db_copy(graph_env):
    from pmt.efficiency import graph as graph_module
    assert graph_module.READ_OPERATIONS == {"capture_source_pin", "preview_graph_change", "query_graph", "calculate_graph_impact"}
    assert graph_module.FILE_OPERATIONS == {"apply_graph_change", "recover_graph_change", "rebuild_graph_index",
                                            "register_segment_manifest"}
    assert not (graph_env["workspace"] / ".pmt" / "graph.json").exists()
