"""Q2 plan graph validation tests."""
from __future__ import annotations

import copy
import hashlib
import uuid

import pytest

from pmt.errors import PmtError
from pmt.db import Database
from pmt.util import new_id, utc_now
from pmt.planning.graph import render_docs, validate_graph


def _id():
    return str(uuid.uuid4())


@pytest.fixture
def graph():
    project_id, req_root, req_leaf, implementation = (_id(), _id(), _id(), _id())
    graph = {
        "schema_version": 1,
        "project_id": project_id,
        "graph_version": 1,
        "nodes": [
            {"id": req_root, "tree_kind": "requirement", "node_kind": "goal", "summary": "사용자 목표를 보존한다",
             "premise": "요청의 목표와 범위가 기준이다", "source_refs": ["request:root"], "criteria": ["목표 기준이 연결됨"],
             "product_stage": "prototype", "product_scope": {"applies": True, "reason": "초기 결과 검증", "criteria": ["목표 기준이 연결됨"]},
             "autonomy": {"authority": "user", "scope": "목표 유지"}},
            {"id": req_leaf, "tree_kind": "requirement", "node_kind": "quality", "summary": "결과를 검증한다",
             "premise": "품질 기준은 관찰 가능해야 한다", "source_refs": ["request:acceptance"], "criteria": ["재현 가능한 확인"],
             "product_stage": "production", "product_scope": {"applies": True, "reason": "운영 판정 기준", "criteria": ["재현 가능한 확인"]},
             "autonomy": {"authority": "user", "scope": "기준 유지"},
             "stop_reason": "implementation_boundary"},
            {"id": implementation, "tree_kind": "implementation", "node_kind": "test", "summary": "검증 결과를 보관한다",
             "premise": "검증은 기준과 결과를 연결한다", "product_stage": "prototype",
             "product_scope": {"applies": True, "reason": "시제품의 검증 확인", "criteria": ["실제 로그 확인"]},
             "autonomy": {"authority": "ai", "scope": "구현 방법"}, "stop_reason": "file_edit_boundary",
             "function_spec": {"input": "기준과 대상", "output": "기준별 판정", "constraints": "비밀 미포함",
                               "invariants": "증거가 판정을 뒷받침", "errors": "미실행과 차단 구분", "verification": "실제 로그 확인"},
             "framework_assignment": "Python stdlib", "architecture": "planning validator → resource → SQLite refs",
             "logging": "planning.graph_validated", "tests": ["validator tests", "isolated SQLite CLI test"],
             "choice_set": {"options": [
                 {"label": "SQLite", "rationale": "기존 저장 경계를 따른다", "verified_refs": ["source:sqlite"]},
                 {"label": "JSON artifact", "rationale": "대용량 결과를 분리한다", "verified_refs": ["source:resources"]},
                 {"label": "Git file", "rationale": "사람이 검토할 수 있다", "verified_refs": ["source:docs"]}]},
             "choice": {"source": "ai", "selected": "Git file", "reason": "계약상 프로젝트 문서가 원본이다", "scope": "문서 생성"}},
        ],
        "relations": [
            {"id": _id(), "kind": "parent", "from": req_root, "to": req_leaf},
            {"id": _id(), "kind": "implements", "from": req_leaf, "to": implementation},
        ],
        "provenance": {"request_ref": "request:root", "decision": "scope confirmed"},
    }
    return graph


def test_valid_graph_has_stable_report_and_rendered_json(graph):
    result = validate_graph(graph, graph["project_id"])
    markdown, json_text, rendered = render_docs(graph)
    assert result["valid"] is True
    assert result["node_count"] == 3
    assert rendered["sha256"] == result["sha256"]
    assert json_text.endswith("\n") and "## Requirements" in markdown and "## Implementation" in markdown
    assert "Framework assignment" in markdown and "Architecture" in markdown and "Tests" in markdown


@pytest.mark.parametrize("mutation,code", [
    (lambda g: g["nodes"].append(copy.deepcopy(g["nodes"][0])), "plan_graph_duplicate_id"),
    (lambda g: g["relations"].append({"id": _id(), "kind": "depends_on", "from": g["nodes"][0]["id"], "to": _id()}), "plan_graph_invalid_reference"),
    (lambda g: g["nodes"][1].pop("stop_reason"), "plan_graph_termination_missing"),
    (lambda g: g["nodes"][0].update(summary="x" * 51), "plan_graph_invalid"),
])
def test_invalid_graph_fields_and_references_are_rejected(graph, mutation, code):
    mutation(graph)
    with pytest.raises(PmtError) as error:
        validate_graph(graph)
    assert error.value.code == code


def test_parent_cycle_is_rejected(graph):
    graph["relations"].append({"id": _id(), "kind": "parent", "from": graph["nodes"][1]["id"], "to": graph["nodes"][0]["id"]})
    with pytest.raises(PmtError) as error:
        validate_graph(graph)
    assert error.value.code == "plan_graph_cycle"


def test_decomposition_and_dependency_cycles_are_checked_separately(graph):
    graph["relations"].append({"id": _id(), "kind": "depends_on",
                               "from": graph["nodes"][1]["id"], "to": graph["nodes"][0]["id"]})
    assert validate_graph(graph)["valid"] is True


def test_too_few_choices_need_an_explicit_insufficiency_reason(graph):
    choices = graph["nodes"][2]["choice_set"]
    choices["options"].pop()
    with pytest.raises(PmtError) as error:
        validate_graph(graph)
    assert error.value.code == "plan_graph_invalid"
    choices["insufficient_reason"] = "확인된 대안은 둘뿐이다"
    assert validate_graph(graph)["valid"] is True


def test_graph_rejects_wrong_project_id(graph):
    with pytest.raises(PmtError) as error:
        validate_graph(graph, _id())
    assert error.value.code == "plan_graph_project_mismatch"


def test_save_draft_persists_only_resource_reference_and_can_be_read(cli, request_factory, create_project,
                                                                      parse_cli_response, graph):
    scope_id = create_project("Q2 planning")
    graph["project_id"] = scope_id
    request = request_factory("save_plan_draft", {"graph": graph}, scope_id=scope_id)
    saved_proc = cli.call(request)
    assert saved_proc.returncode == 0, saved_proc.stdout + saved_proc.stderr
    saved = parse_cli_response(saved_proc.stdout)
    assert saved["ok"] is True
    result = saved["result"]
    assert result["state"] == "draft" and result["artifact_id"]

    replay_proc = cli.call(request)
    assert replay_proc.returncode == 0, replay_proc.stderr
    assert parse_cli_response(replay_proc.stdout)["result"] == result

    read_proc = cli.call(request_factory("read_plan", {"plan_id": result["plan_id"], "include_graph": True},
                                        scope_id=scope_id))
    assert read_proc.returncode == 0, read_proc.stderr
    read = parse_cli_response(read_proc.stdout)
    assert read["ok"] is True
    assert read["result"]["state"] == "draft"
    assert read["result"]["graph"]["project_id"] == scope_id


def test_publish_writes_docs_only_with_owned_workspace_scope(cli, request_factory, create_project,
                                                              parse_cli_response, graph, tmp_path):
    scope_id = create_project("Q2 publish")
    graph["project_id"] = scope_id
    workspace = tmp_path / "project"
    workspace.mkdir()
    agents = workspace / "AGENTS.md"
    agents.write_text("# Existing project rules\nKeep this text.\n", encoding="utf-8")
    draft_proc = cli.call(request_factory("save_plan_draft", {"graph": graph, "workspace": str(workspace)},
                                          scope_id=scope_id))
    assert draft_proc.returncode == 0, draft_proc.stdout + draft_proc.stderr
    draft = parse_cli_response(draft_proc.stdout)["result"]

    db = Database(cli.data_root, cli.config_root)
    step_id, job_id, run_id = new_id(), new_id(), new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (step_id, "step", scope_id, "Publish plan", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (job_id, step_id, "running", "{}", now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run_id, job_id, step_id, 1, "running", 1, "test-main", 1, str(workspace.resolve()), "[]", "{}", "{}", now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run_id, "test-main", "workspace", str(workspace.resolve()), ".", now))
    agents_hash = hashlib.sha256(agents.read_bytes()).hexdigest()
    publish_proc = cli.call(request_factory("publish_project_docs", {
        "plan_id": draft["plan_id"], "workspace": str(workspace), "run_id": run_id,
        "expected_file_hashes": {"AGENTS.md": agents_hash}, "baseline_commit": "test-baseline",
    }, scope_id=scope_id))
    assert publish_proc.returncode == 0, publish_proc.stdout + publish_proc.stderr
    published = parse_cli_response(publish_proc.stdout)
    assert published["ok"] is True and published["result"]["state"] == "published"
    assert "Keep this text." in agents.read_text(encoding="utf-8")
    assert (workspace / "docs/pmt-docs/plan.graph.json").is_file()
    assert (workspace / "docs/pmt-docs/plan.md").is_file()


def test_publish_without_owned_paths_leaves_project_docs_untouched(cli, request_factory, create_project,
                                                                    parse_cli_response, graph, tmp_path):
    scope_id = create_project("Q2 denied publish")
    graph["project_id"] = scope_id
    workspace = tmp_path / "project"
    workspace.mkdir()
    draft_proc = cli.call(request_factory("save_plan_draft", {"graph": graph, "workspace": str(workspace)},
                                          scope_id=scope_id))
    assert draft_proc.returncode == 0, draft_proc.stdout + draft_proc.stderr
    draft = parse_cli_response(draft_proc.stdout)["result"]
    request = request_factory("publish_project_docs", {"plan_id": draft["plan_id"], "workspace": str(workspace),
                                                        "run_id": new_id()}, scope_id=scope_id)
    response = cli.call(request)
    assert response.returncode == 3
    assert parse_cli_response(response.stdout)["error"]["code"] == "ownership_conflict"
    assert not (workspace / "docs/pmt-docs/plan.graph.json").exists()


def test_unfinished_requirement_only_draft_is_saved_but_not_publishable(graph):
    graph['nodes'] = [n for n in graph['nodes'] if n['tree_kind'] == 'requirement']
    ids = {n['id'] for n in graph['nodes']}
    graph['relations'] = [r for r in graph['relations'] if r['from'] in ids and r['to'] in ids]
    graph['nodes'][-1].pop('stop_reason', None)
    assert validate_graph(graph, complete=False)['valid']
    with pytest.raises(PmtError):
        validate_graph(graph)
