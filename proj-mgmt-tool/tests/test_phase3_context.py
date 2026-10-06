from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import sys
import uuid
from pathlib import Path
from contextlib import closing

import pytest

from pmt.efficiency.context import (
    compare_resume_metadata,
    graph_query_for_context,
    project_task_context,
    paginate_context_detail,
    resolve_alias_in_bundle,
)
from pmt.efficiency.source import SourcePin
from pmt.db import Database
from pmt.errors import PmtError
from pmt.efficiency.storage import Phase3Storage
from pmt.store import LocalStore
from pmt.util import canonical_json, new_id, utc_now


def _graph_node(node_id, tree_kind, step_id):
    node = {"id": node_id, "tree_kind": tree_kind, "node_kind": "goal",
            "summary": "Preserve verified task context", "premise": "Stable source pin",
            "product_stage": "prototype",
            "product_scope": {"applies": True, "reason": "Current project", "criteria": []},
            "autonomy": {"authority": "method", "scope": "approved Step"},
            "work_item_step_refs": {"work": [], "item": [], "step": [step_id]}}
    if tree_kind == "requirement":
        node.update(source_refs=["request:fixture"], criteria=["SourcePin matches"], evidence_refs=[])
    else:
        node.update(framework_assignment="Python", architecture="bounded context",
                    logging="IDs and hashes only", tests=["current source check"],
                    function_spec={"input": "pinned graph", "output": "context", "constraints": "no raw conversation",
                                   "invariants": "stable IDs", "errors": "unknown is not complete",
                                   "verification": "source hash"},
                    choice_set={"options": [], "insufficient_reason": "Fixture"},
                    choice={"source": "user", "selected": "F5", "reason": "approved", "scope": "Step"})
    return node


@pytest.fixture
def actual_context_env(tmp_path, request):
    temporary = tempfile.TemporaryDirectory(prefix="p5c-", dir=tmp_path.parents[1])
    root = Path(temporary.name)
    failures_before = request.session.testsfailed

    def cleanup_or_preserve_failure():
        if request.session.testsfailed > failures_before:
            temporary._finalizer.detach()
            (tmp_path / "fixture-artifacts.json").write_text(canonical_json({
                "root": str(root), "test": request.node.nodeid,
                "reason": "failed fixture retained for DB/Git diagnosis"}) + "\n", encoding="utf-8")
        else:
            assert root.resolve().is_relative_to(tmp_path.parents[1].resolve())
            temporary.cleanup()

    request.addfinalizer(cleanup_or_preserve_failure)
    workspace = root / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "PMT test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "pmt-test@example.invalid"], check=True)
    project_id, repo_id, work_id, item_id, step_id, run_id, job_id = (new_id() for _ in range(7))
    requirement_id, implementation_id, relation_id = (new_id() for _ in range(3))
    relative_graph_path = "docs/pmt-docs/plan.graph.json"
    graph_doc = {"schema_version": 1, "project_id": project_id, "graph_version": 1,
                 "nodes": [_graph_node(requirement_id, "requirement", step_id),
                           _graph_node(implementation_id, "implementation", step_id)],
                 "relations": [{"id": relation_id, "kind": "implements",
                                "from": requirement_id, "to": implementation_id}],
                 "provenance": {"request_ref": "fixture:request"}}
    graph_path = workspace / relative_graph_path
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text(canonical_json(graph_doc) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", relative_graph_path], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "fixture graph"], check=True)

    session, actor = "context-session", "context-test"
    db = Database(root / "data", root / "config")
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (repo_id, "repository", "repo", "{}", now, now))
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (project_id, "project", repo_id, "project",
                      canonical_json({"repository_id": repo_id, "workspace": str(workspace)}), now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (work_id, "work", project_id, "Work", "InProgress", "{}", 1, now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (item_id, "item", project_id, work_id, "Item", "InProgress", "{}", 1, now, now))

    directive = {"purpose": "Build a verified context", "goal": "Preserve source and criteria",
                 "non_goal": ["Do not broaden access"], "change_scope": {"add": [], "modify": [],
                 "delete": [], "forbidden": ["claims and authentication"]},
                 "inputs": [{"name": "graph", "meaning": "source-pinned graph"}],
                 "outputs": [{"name": "context", "meaning": "bounded projection"}],
                 "tests": [{"name": "source test", "meaning": "actual hash"}],
                 "logging": [{"name": "trace", "meaning": "safe IDs only"}],
                 "method": {"steps": ["read", "project"]}, "context_refs": [requirement_id]}
    directive_req = {"request_id": new_id(), "actor": actor, "session_id": session,
                     "scope_id": project_id, "payload": {}}
    from pmt.phase2_common import persist_json_resource
    directive_resource = persist_json_resource(db, directive_req, directive, project_id,
                                               "step_directive", step_id)
    requirements_version, plan_version = "requirements-v1", "plan-v1"
    evidence_resource = persist_json_resource(db, {**directive_req, "request_id": new_id()},
                                              {"fixture": "source proof"}, project_id,
                                              "evidence", step_id)
    criteria = [{"id": "source-current", "meaning": "SourcePin equals current source", "evidence_refs": []},
                {"id": "bounded-context", "meaning": "Required fields are preserved",
                 "evidence_refs": [evidence_resource["artifact_id"]]}]
    step_body = {"directive_id": directive_resource["artifact_id"], "directive_version": 1,
                 "invalidated": False, "kind_tag": "test"}
    scopes = [{"kind": "path", "workspace": str(workspace), "resource": relative_graph_path}]
    with db.write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (step_id, "step", project_id, item_id, "Step", "InProgress", canonical_json(step_body), 1, now, now))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (step_id, directive_resource["artifact_id"], 1, requirements_version, plan_version, None,
                      "lower", "prototype", str(workspace), canonical_json(scopes), canonical_json(criteria), "[]", now, now))
        intent = {"task": {"task_id": item_id, "step_id": step_id}, "run_id": run_id,
                  "plan_version": plan_version, "requirements_version": requirements_version,
                  "directive_ref": directive_resource["artifact_id"], "criteria": criteria,
                  "role": "lower", "scope_id": project_id, "directive_version": 1}
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,'running','{}',?,?)",
                     (job_id, step_id, now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,1,'running',1,?,1,?,?, '{}',?,?,?)",
                     (run_id, job_id, step_id, session, str(workspace), canonical_json(scopes),
                      canonical_json(intent), now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     ("context-graph", run_id, session, "path", str(workspace), relative_graph_path, now))
    return {"db": db, "workspace": workspace, "project_id": project_id, "repo_id": repo_id,
            "work_id": work_id, "item_id": item_id, "step_id": step_id, "run_id": run_id,
            "actor": actor, "session": session, "graph_path": relative_graph_path,
            "node_id": requirement_id, "implementation_node_id": implementation_id,
            "directive_id": directive_resource["artifact_id"],
            "criteria": criteria, "evidence_id": evidence_resource["artifact_id"],
            "evidence_path": db.root / evidence_resource["relative_path"]}


def _actual_request(env, operation, *, session=None, request_id=None, **payload):
    return {"protocol_version": 1, "operation": operation, "request_id": request_id or new_id(),
            "actor": env["actor"], "session_id": session or env["session"],
            "scope_id": env["project_id"],
            "payload": {"repository_id": env["repo_id"], "workspace": str(env["workspace"]),
                        "relative_graph_path": env["graph_path"], "run_id": env["run_id"], **payload}}


def _actual_f3_ready(env, session=None):
    store = LocalStore(env["db"])
    request = _actual_request(env, "capture_source_pin", session=session)
    captured, code = store.execute(request)
    assert code == 0 and captured["ok"], captured.get("error")
    pin = captured["result"]["source_pin"]
    rebuilt, code = store.execute(_actual_request(env, "rebuild_graph_index", session=session,
                                                 expected_source=pin))
    assert code == 0 and rebuilt["ok"], rebuilt.get("error")
    return pin


def _build_actual(env, pin, *, session=None, operation="build_task_context", previous_context_id=None,
                  max_bytes=20_000, max_lines=1_000, request_id=None, impact_request=None,
                  node_ids=None):
    payload = {"task_ref": {"task_id": env["item_id"], "step_id": env["step_id"],
                            "run_id": env["run_id"]},
               "expected_source": pin, "role": "lower",
               "budget": {"max_bytes": max_bytes, "max_lines": max_lines, "unit": "utf8"},
               "node_ids": node_ids if node_ids is not None else [env["node_id"]]}
    if previous_context_id is not None:
        payload["previous_context_id"] = previous_context_id
    if impact_request is not None:
        payload["impact_request"] = impact_request
    return LocalStore(env["db"]).execute(_actual_request(env, operation, session=session,
                                                          request_id=request_id, **payload))


def _detail_actual(env, context_id, cursor, *, session=None, max_bytes=256, max_lines=4):
    request = {"protocol_version": 1, "operation": "read_context_detail", "request_id": new_id(),
               "actor": env["actor"], "session_id": session or env["session"],
               "scope_id": env["project_id"],
               "payload": {"context_id": context_id, "cursor": cursor,
                           "max_bytes": max_bytes, "max_lines": max_lines}}
    return LocalStore(env["db"]).execute(request)


PROJECT_ID = "00000000-0000-4000-8000-000000000101"
TASK_ID = "00000000-0000-4000-8000-000000000102"
STEP_ID = "00000000-0000-4000-8000-000000000103"
RUN_ID = "00000000-0000-4000-8000-000000000104"
NODE_A = "00000000-0000-4000-8000-000000000105"
NODE_B = "00000000-0000-4000-8000-000000000106"
RELATION_ID = "00000000-0000-4000-8000-000000000107"
AUTH_REF = "authz-ref-opaque"
CLAIM_REF = "claim-ref-opaque"


def _pin(graph_hash="graph-a", *, dirty_state="clean"):
    return SourcePin("00000000-0000-4000-8000-000000000108", PROJECT_ID,
                     "main", "commit-a", 1, 4, graph_hash, dirty_state).to_dict()


def _directive():
    return {
        "purpose": "Build scoped implementation context",
        "goal": "Preserve verified requirements and constraints",
        "non_goal": ["Do not edit unrelated projects"],
        "change_scope": {"add": ["module"], "modify": ["service"],
                         "delete": [], "forbidden": ["database schema"]},
        "inputs": [{"name": "graph", "meaning": "source pinned graph slice"}],
        "outputs": [{"name": "context", "meaning": "bounded role projection"}],
        "tests": [{"name": "context test", "meaning": "verify required sections"}],
        "logging": [{"name": "safe trace", "meaning": "refs and sizes only"}],
        "method": {"steps": ["read", "project", "verify"]},
        "context_refs": ["artifact:fixture-1"],
        "private_environment": {"API_TOKEN": "must-not-copy"},
    }


def _graph(pin=None, *, unknown=None, next_cursor=None, complete=True):
    pin = pin or _pin()
    return {
        "source_pin": pin,
        "items": [
            {"entity": "node", "value": {"id": NODE_A, "tree_kind": "requirement",
             "node_kind": "quality", "summary": "Preserve exact IDs", "premise": "Stable source",
             "autonomy": {"authority": "method only", "scope": "step"},
             "criteria": ["check-hash"], "function_spec": {"input": "pin", "output": "context"},
             "unselected_secret": "must-not-copy"}, "path": []},
            {"entity": "node", "value": {"id": NODE_B, "tree_kind": "implementation",
             "node_kind": "step", "summary": "Use bounded pages", "criteria": ["bounded"]}, "path": [RELATION_ID]},
            {"entity": "relation", "value": {"id": RELATION_ID, "kind": "implements",
             "from": NODE_B, "to": NODE_A, "extra": "not allowed"}},
        ],
        "complete": complete, "unknown": list(unknown or []), "next_cursor": next_cursor,
        "traversal_complete": complete,
        "index": {"version": 1, "hash": "a" * 64, "graph_revision": 4},
    }


def _input(*, role="implement", budget=None, pin=None, graph=None, directive=None, criteria=None,
           evidence=None, context_id="00000000-0000-4000-8000-000000000109"):
    pin = pin or _pin()
    return {
        "context_id": context_id,
        "task_ref": {"task_id": TASK_ID, "step_id": STEP_ID, "run_id": RUN_ID},
        "role": role,
        "source_pin": pin,
        "access": {"scope_id": PROJECT_ID, "authorization_ref": AUTH_REF,
                    "claim_ref": CLAIM_REF, "run_id": RUN_ID},
        "directive": directive or _directive(),
        "criteria": criteria if criteria is not None else [
            {"id": "acceptance-1", "meaning": "hash matches current source"},
            {"id": "acceptance-2", "meaning": "omitted content is reported"},
        ],
        "graph_slice": graph if graph is not None else _graph(pin),
        "evidence_refs": evidence if evidence is not None else [
            {"ref": "artifact:verified-1", "status": "valid", "sha256": "b" * 64}],
        "budget": budget or {"max_bytes": 100_000, "max_lines": 1_000, "unit": "utf8"},
    }


def test_f5_s1_s2_required_sections_unknown_scope_and_source_pin_are_explicit():
    built = project_task_context(_input())
    response, bundle = built["response"], built["bundle"]
    assert response["source_pin"]["source_hash"] == bundle["source_pin"]["source_hash"]
    assert response["scope_ref"]["authorization_ref"] == AUTH_REF
    assert response["scope_ref"]["claim_ref"] == CLAIM_REF
    assert response["budget"]["token_usage"] == {
        "status": "unknown", "actual": None, "estimate": None, "reason": "tokenizer_unavailable"}
    assert [section["section_id"] for section in response["included"]][:9] == [
        "purpose", "goal", "non_goal", "change_scope", "inputs", "outputs", "criteria", "tests", "logging"]
    assert any(section["section_id"] == "method" and section["required"]
               for section in bundle["sections"])
    assert any(section["section_id"] == "autonomy" and section["required"]
               for section in bundle["sections"])
    assert response["incomplete"] is False
    assert all("private_environment" not in json.dumps(section) for section in bundle["sections"])
    assert all("unselected_secret" not in json.dumps(section) for section in bundle["sections"])
    assert "must-not-copy" not in json.dumps(bundle)


def test_f5_s2_small_budget_omits_whole_required_sections_and_offers_only_saved_detail():
    budget = {"max_bytes": 10, "max_lines": 1, "unit": "utf8"}
    built = project_task_context(_input(budget=budget))
    response, bundle = built["response"], built["bundle"]
    assert response["incomplete"] is True
    assert response["budget"]["over_budget"] is True
    assert response["budget"]["used_bytes"] > budget["max_bytes"]
    assert response["included"] == []
    assert len(response["omitted_required"]) > 0
    assert all(item["available"] is True for item in response["omitted_required"])
    cursor = response["detail_cursor"]
    assert cursor and bundle["detail_size_bytes"] > 10
    page = paginate_context_detail(bundle, _input()["access"], bundle["source_pin"], cursor,
                               max_bytes=48, max_lines=2)
    assert len(page["content"].encode("utf-8")) <= 48
    assert page["start_byte"] == 0 and page["end_byte"] > 0
    assert page["next_cursor"]
    assert page["content_sha256"]


def test_f5_s1_f3_partial_source_and_unknowns_remain_incomplete():
    partial = _graph(unknown=[{"kind": "relation", "reason_code": "index_partial",
                               "node_ids": [NODE_A]}], next_cursor="graph-cursor-real", complete=False)
    result = project_task_context(_input(graph=partial))["response"]
    assert result["incomplete"] is True
    unresolved = next(section for section in result["included"] if section["section_id"] == "unresolved")
    assert unresolved["value"]["items"][0]["reason_code"] == "index_partial"
    assert unresolved["value"]["source_graph_cursor"] == "graph-cursor-real"

    stale_graph = _graph(_pin(graph_hash="older-graph"))
    stale = project_task_context(_input(graph=stale_graph))["response"]
    graph_section = next(section for section in stale["included"] if section["section_id"] == "related_graph")
    assert graph_section["value"]["nodes"] == []
    assert stale["incomplete"] is True
    assert any(item["reason_code"] == "source_pin_unverified" for item in stale["unknown"])


def test_f5_s2_role_changes_projection_not_scope_authority():
    implement = project_task_context(_input(role="implement"))["response"]
    review = project_task_context(_input(role="review", context_id="00000000-0000-4000-8000-000000000110"))["response"]
    assert implement["scope_ref"]["scope_hash"] == review["scope_ref"]["scope_hash"]
    assert implement["role"] == "implement" and review["role"] == "review"
    assert implement["alias_map"]["map_ref"]["id"] == implement["context_ref"]["id"]
    assert len(implement["alias_map"]["mapping_hash"]) == 64
    assert review["alias_map"]["version"] == implement["alias_map"]["version"]
    assert graph_query_for_context("implement")["fields"] != graph_query_for_context("review")["fields"]
    query = graph_query_for_context("review", node_ids=[NODE_A], cursor="source-cursor")
    assert query["node_ids"] == [NODE_A] and query["cursor"] == "source-cursor"
    assert "criteria" in query["fields"]
    assert "function_spec" in graph_query_for_context("implement")["fields"]


def test_f5_s3_aliases_are_immutable_and_bound_to_context_scope_and_source():
    built = project_task_context(_input())
    bundle, response = built["bundle"], built["response"]
    alias, canonical = next(iter(bundle["aliases"]["aliases"].items()))
    resolved = resolve_alias_in_bundle(bundle, _input()["access"], alias,
                                    expected_source_pin=bundle["source_pin"],
                                    expected_mapping_version=1)
    assert resolved["canonical_id"] == canonical
    assert canonical in {NODE_A, NODE_B}
    changed_pin = _pin(graph_hash="changed")
    with pytest.raises(PmtError, match="Source changed"):
        resolve_alias_in_bundle(bundle, _input()["access"], alias,
                              expected_source_pin=changed_pin, expected_mapping_version=1)
    wrong_access = {**_input()["access"], "scope_id": str(uuid.uuid4())}
    with pytest.raises(PmtError, match="another authorization"):
        resolve_alias_in_bundle(bundle, wrong_access, alias,
                              expected_source_pin=bundle["source_pin"], expected_mapping_version=1)
    with pytest.raises(PmtError, match="version"):
        resolve_alias_in_bundle(bundle, _input()["access"], alias,
                              expected_source_pin=bundle["source_pin"], expected_mapping_version=2)


def test_f5_s3_cursor_rejects_changed_scope_source_bundle_and_utf8_split():
    built = project_task_context(_input(budget={"max_bytes": 5, "max_lines": 1, "unit": "utf8"}))
    bundle, access = built["bundle"], _input()["access"]
    cursor = built["response"]["detail_cursor"]
    with pytest.raises(PmtError, match="another scope"):
        paginate_context_detail(bundle, {**access, "scope_id": str(uuid.uuid4())}, bundle["source_pin"],
                            cursor, max_bytes=20, max_lines=3)
    with pytest.raises(PmtError, match="Source changed"):
        paginate_context_detail(bundle, access, _pin(graph_hash="changed"), cursor,
                            max_bytes=20, max_lines=3)
    page = paginate_context_detail(bundle, access, bundle["source_pin"], cursor,
                               max_bytes=32, max_lines=2)
    assert page["start_byte"] == 0 and page["end_byte"] <= 32
    chunks = [page["content"]]
    while page["next_cursor"]:
        page = paginate_context_detail(bundle, access, bundle["source_pin"], page["next_cursor"],
                                   max_bytes=37, max_lines=2)
        assert page["start_byte"] == sum(len(chunk.encode("utf-8")) for chunk in chunks)
        assert len(page["content"].encode("utf-8")) <= 37
        chunks.append(page["content"])
    assert "".join(chunks) == bundle["detail_text"]
    forged = "not-a-cursor"
    with pytest.raises(PmtError):
        paginate_context_detail(bundle, access, bundle["source_pin"], forged,
                            max_bytes=32, max_lines=2)


def test_f5_s4_resume_requires_fresh_source_authority_owner_and_evidence():
    built = project_task_context(_input())
    previous = {"source_pin": built["bundle"]["source_pin"],
                "scope_binding": built["bundle"]["scope_binding"],
                "evidence_refs": ["artifact:verified-1"]}
    valid_current = {"source_pin": previous["source_pin"],
                     "scope_binding": previous["scope_binding"],
                     "authority_check": {"checked": True, "allowed": True, "owner_matches": True},
                     "evidence_checks": {"artifact:verified-1": "valid"}}
    ready = compare_resume_metadata(previous, valid_current)
    assert ready["status"] == "ready_to_rebuild"
    assert ready["previous_context_authoritative"] is False
    assert ready["criteria_verdict"] == "not_evaluated"
    changed = {**valid_current, "source_pin": _pin(graph_hash="new-source")}
    assert compare_resume_metadata(previous, changed)["status"] == "stale"
    denied = {**valid_current, "authority_check": {"checked": True, "allowed": False, "owner_matches": False}}
    assert compare_resume_metadata(previous, denied)["status"] == "stale"
    invalid_evidence = {**valid_current, "evidence_checks": {"artifact:verified-1": "invalid"}}
    assert compare_resume_metadata(previous, invalid_evidence)["status"] == "stale"
    unknown = {"source_pin": previous["source_pin"], "scope_binding": previous["scope_binding"]}
    assert compare_resume_metadata(previous, unknown)["status"] == "unknown"


def test_f5_rejects_unbounded_or_unenforceable_budget_and_empty_required_criteria():
    with pytest.raises(PmtError, match="finite UTF-8"):
        project_task_context(_input(budget={"max_bytes": 0, "max_lines": 1, "unit": "utf8"}))
    result = project_task_context(_input(criteria=[]))["response"]
    assert result["incomplete"] is True
    assert any(item["section_id"] == "criteria" and item["reason_code"] == "criteria_missing"
               for item in result["missing_required"])



def _read_task_context_actual(env, context_ref):
    request = {"protocol_version": 1, "operation": "read_task_context", "request_id": new_id(),
               "actor": env["actor"], "session_id": env["session"],
               "scope_id": env["project_id"], "payload": {"context_ref": context_ref}}
    return LocalStore(env["db"]).execute(request)


def test_p3_f5_context_getref_returns_only_current_hash_bound_projection(actual_context_env):
    pin = _actual_f3_ready(actual_context_env)
    built, code = _build_actual(actual_context_env, pin)
    assert code == 0 and built["ok"], built.get("error")
    ref = built["result"]["context_ref"]
    assert set(ref) == {"kind", "id", "scope_id", "source_hash", "version", "projection_hash"}
    read, read_code = _read_task_context_actual(actual_context_env, {key: ref[key] for key in
        ("kind", "id", "scope_id", "source_hash", "version", "projection_hash")})
    assert read_code == 0 and read["ok"], read.get("error")
    metadata = read["result"]
    assert metadata["context_ref"]["projection_hash"] == ref["projection_hash"]
    assert metadata["current_authority"]["run_id"] == actual_context_env["run_id"]
    assert metadata["current_authority"]["owner_checked"] is True
    assert metadata["current_authority"]["source_hash"] == pin["source_hash"]
    assert metadata["projection"]["included"] == built["result"]["included"]
    assert all(item["section_id"] != "private_environment" for item in metadata["projection"]["included"])
    stale = {key: ref[key] for key in ("kind", "id", "scope_id", "source_hash", "version", "projection_hash")}
    stale["projection_hash"] = "0" * 64
    denied, denied_code = _read_task_context_actual(actual_context_env, stale)
    assert denied_code == 3 and denied["error"]["code"] == "context_ref_stale"


def test_p3_f5_impact_request_is_recomputed_against_current_source(actual_context_env):
    pin = _actual_f3_ready(actual_context_env)
    change_set = {"change_id": new_id(), "reason": "F5 context impact fixture",
                  "evidence_refs": [], "changes": [{"op": "update", "id": actual_context_env["node_id"],
                                                       "fields": {"premise": "changed requirement premise"}}]}
    preview, preview_code = LocalStore(actual_context_env["db"]).execute(
        _actual_request(actual_context_env, "preview_graph_change", expected_source=pin, change_set=change_set))
    assert preview_code == 0 and preview["ok"], preview.get("error")
    built, code = _build_actual(actual_context_env, pin, impact_request={
        "change_preview": preview["result"], "change_set": change_set})
    assert code == 0 and built["ok"], built.get("error")
    sections = {item["section_id"]: item for item in built["result"]["included"]}
    assert "impact_summary" in sections
    impact = sections["impact_summary"]["value"]
    assert impact["change_id"] == change_set["change_id"]
    assert impact["source_pin"]["source_hash"] == pin["source_hash"]
    assert any(item["node_id"] == actual_context_env["node_id"] for item in impact["known"])

def test_p3_f5_01_localstore_uses_current_git_pin_private_directive_and_p2_index(actual_context_env):
    pin = _actual_f3_ready(actual_context_env)
    request_id = new_id()
    built, code = _build_actual(actual_context_env, pin, request_id=request_id)
    assert code == 0 and built["ok"], built.get("error")
    result = built["result"]
    assert result["source_pin"]["source_hash"] == pin["source_hash"]
    assert result["scope_ref"]["scope_id"] == actual_context_env["project_id"]
    assert result["scope_ref"]["claim_ref"]
    assert result["budget"]["token_usage"]["status"] == "unknown"
    assert result["budget"]["used_bytes"] == len(canonical_json({
        "protocol_version": 1, "request_id": built["request_id"], "ok": True,
        "result": result, "error": None, "warnings": []}).encode("utf-8"))
    assert result["budget"]["used_lines"] <= result["budget"]["requested"]["max_lines"]
    assert not result["incomplete"]
    section_ids = [item["section_id"] for item in result["included"]]
    for required in ("goal", "non_goal", "change_scope", "criteria", "method", "autonomy", "evidence_refs"):
        assert required in section_ids
    assert actual_context_env["workspace"].as_posix() not in json.dumps(result)
    with closing(actual_context_env["db"].connect()) as conn:
        cached = Phase3Storage(actual_context_env["db"]).get_object(
            "task_context", result["context_ref"]["id"], actual_context_env["project_id"],
            actual_context_env["actor"], actual_context_env["session"], conn=conn)
        assert cached and cached["state"] == "ready"
        assert cached["source_hash"] == pin["source_hash"]
        assert cached["body"]["source_ref"]["directive_version"] == 1
        assert cached["body"]["source_ref"]["graph_index_hash"]
        assert cached["body"]["scope_binding"]["authorization_ref"]
        request_cache = conn.execute("SELECT response_json FROM requests WHERE request_id=?", (request_id,)).fetchone()[0]
        assert "Preserve source and criteria" not in request_cache
        assert "Do not broaden access" not in request_cache


def test_f5_actual_depth_frontier_stays_unknown_but_full_explicit_selection_is_complete(actual_context_env):
    env = actual_context_env
    graph_path = env["workspace"] / env["graph_path"]
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    first_extra, second_extra = new_id(), new_id()
    base_node = next(node for node in graph["nodes"] if node["id"] == env["implementation_node_id"])
    middle = json.loads(canonical_json(base_node))
    middle.update(id=first_extra, summary="Third node beyond the seed")
    terminal = json.loads(canonical_json(base_node))
    terminal.update(id=second_extra, summary="Fourth node beyond depth two")
    # Only the explicit seed is selected through Step references; the remaining
    # graph must be discovered through real F2 traversal.
    for node in graph["nodes"]:
        if node["id"] != env["node_id"]:
            node.pop("work_item_step_refs", None)
    middle.pop("work_item_step_refs", None)
    terminal.pop("work_item_step_refs", None)
    graph["nodes"].extend([middle, terminal])
    graph["relations"].extend([
        {"id": new_id(), "kind": "depends_on", "from": env["implementation_node_id"], "to": first_extra},
        {"id": new_id(), "kind": "depends_on", "from": first_extra, "to": second_extra},
    ])
    graph["graph_version"] += 1
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    pin = _actual_f3_ready(env)

    clipped, code = _build_actual(env, pin, node_ids=[env["node_id"]])
    assert code == 0 and clipped["ok"], clipped.get("error")
    assert clipped["result"]["incomplete"] is True
    traversal_unknown = [item for item in clipped["result"]["unknown"]
                         if item.get("reason_code") == "traversal_limit_or_depth"]
    assert len(traversal_unknown) == 1
    assert second_extra in traversal_unknown[0]["node_ids"]
    assert traversal_unknown[0]["severity"] == "required"

    full_ids = [env["node_id"], env["implementation_node_id"], first_extra, second_extra]
    full, full_code = _build_actual(env, pin, node_ids=full_ids)
    assert full_code == 0 and full["ok"], full.get("error")
    assert not any(item.get("reason_code") == "traversal_limit_or_depth"
                   for item in full["result"]["unknown"])


def test_p3_f5_01_detail_cursor_and_alias_are_current_source_bound(actual_context_env):
    pin = _actual_f3_ready(actual_context_env)
    built, code = _build_actual(actual_context_env, pin, max_bytes=400, max_lines=8)
    assert code == 0 and built["ok"], built.get("error")
    result = built["result"]
    assert result["incomplete"] is True
    assert result["budget"]["over_budget"] is True
    cursor = result["detail_cursor"]
    assert cursor
    detail, detail_code = _detail_actual(actual_context_env, result["context_ref"]["id"], cursor,
                                         max_bytes=80, max_lines=2)
    assert detail_code == 0 and detail["ok"], detail.get("error")
    page = detail["result"]
    assert len(page["content"].encode("utf-8")) <= 80
    assert page["start_byte"] == 0 and page["end_byte"] > 0
    assert page["next_cursor"]
    assert page["delivery_measurement"]["metadata_overhead_bytes"] > 0

    alias_request = {"protocol_version": 1, "operation": "resolve_context_alias", "request_id": new_id(),
                     "actor": actual_context_env["actor"], "session_id": actual_context_env["session"],
                     "scope_id": actual_context_env["project_id"],
                     "payload": {"context_id": result["context_ref"]["id"],
                                 "alias": "N0001", "mapping_version": 1}}
    alias_reply, alias_code = LocalStore(actual_context_env["db"]).execute(alias_request)
    assert alias_code == 0 and alias_reply["ok"], alias_reply.get("error")
    assert alias_reply["result"]["canonical_id"] in {
        actual_context_env["node_id"], actual_context_env["implementation_node_id"]}


def test_p3_f5_02_claim_directive_and_source_changes_block_detail(actual_context_env):
    pin = _actual_f3_ready(actual_context_env)
    built, code = _build_actual(actual_context_env, pin, max_bytes=400, max_lines=8)
    assert code == 0 and built["ok"]
    context_id = built["result"]["context_ref"]["id"]
    cursor = built["result"]["detail_cursor"]
    with actual_context_env["db"].write() as conn:
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (actual_context_env["run_id"],))
    denied, denied_code = _detail_actual(actual_context_env, context_id, cursor)
    assert denied_code == 3 and denied["error"]["code"] in {"scope_not_owned", "scope_not_owned"}


def test_p3_f5_02_stale_source_pin_rejected_on_fresh_projection(actual_context_env):
    pin = _actual_f3_ready(actual_context_env)
    # A different reviewed source may not silently reuse the prior graph index.
    stale_pin = dict(pin)
    stale_pin["graph_hash"] = "f" * 64
    stale_pin.pop("source_hash", None)
    result, code = _build_actual(actual_context_env, stale_pin)
    assert code == 3 and result["error"]["code"] in {"source_conflict", "graph_index_stale"}


def test_p3_f5_03_new_session_resume_builds_fresh_context_without_old_alias_activation(actual_context_env):
    pin = _actual_f3_ready(actual_context_env)
    original, code = _build_actual(actual_context_env, pin)
    assert code == 0 and original["ok"]
    old_context_id = original["result"]["context_ref"]["id"]

    new_session, new_run, new_job = "context-session-next", new_id(), new_id()
    now = utc_now()
    with actual_context_env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='failed',revision=revision+1,updated_at=? WHERE id=?",
                     (now, actual_context_env["run_id"]))
        conn.execute("UPDATE execution_jobs SET state='failed',updated_at=? WHERE id=?",
                     (now, conn.execute("SELECT job_id FROM execution_runs WHERE id=?",
                                        (actual_context_env["run_id"],)).fetchone()[0]))
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (actual_context_env["run_id"],))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,'running','{}',?,?)",
                     (new_job, actual_context_env["step_id"], now, now))
        run_row = conn.execute("SELECT * FROM execution_runs WHERE id=?", (actual_context_env["run_id"],)).fetchone()
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?, 'running',1,?,1,?,?,?,?,?,?)",
                     (new_run, new_job, actual_context_env["step_id"], 2, new_session,
                      run_row["workspace"], run_row["scopes_json"], run_row["route_json"],
                      run_row["intent_json"], now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     ("context-graph-next", new_run, new_session, "path", str(actual_context_env["workspace"]),
                      actual_context_env["graph_path"], now))
    # Host/local current auth is for the new run; the prior private context is not readable in this session.
    next_env = {**actual_context_env, "session": new_session, "run_id": new_run}
    resumed, resume_code = _build_actual(next_env, pin, session=new_session,
                                         operation="resume_task_context", previous_context_id=old_context_id)
    assert resume_code == 0 and resumed["ok"], resumed.get("error")
    assert resumed["result"]["context_ref"]["id"] != old_context_id
    assert resumed["result"]["resume"]["status"] == "unknown"
    assert resumed["result"]["resume"]["old_aliases_reactivated"] is False


def test_p3_f5_01_detail_through_protocol_v1_cli(actual_context_env):
    pin = _actual_f3_ready(actual_context_env)
    built, code = _build_actual(actual_context_env, pin, max_bytes=400, max_lines=8)
    assert code == 0 and built["ok"]
    result = built["result"]
    request = {"protocol_version": 1, "operation": "read_context_detail", "request_id": new_id(),
               "actor": actual_context_env["actor"], "session_id": actual_context_env["session"],
               "scope_id": actual_context_env["project_id"],
               "payload": {"context_id": result["context_ref"]["id"],
                           "cursor": result["detail_cursor"], "max_bytes": 80, "max_lines": 2}}
    checkout = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(checkout / "src")}
    completed = subprocess.run([sys.executable, "-m", "pmt", "--data-root", str(actual_context_env["db"].root),
                                "--config-root", str(actual_context_env["db"].config_root)],
                               input=json.dumps(request), text=True, encoding="utf-8", capture_output=True,
                               cwd=checkout, env=env, timeout=20, check=False)
    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert response["ok"] and response["result"]["content"]
    assert len(response["result"]["content"].encode("utf-8")) <= 80
