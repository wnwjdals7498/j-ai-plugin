"""Q7 Git baseline tests using real temporary repositories and isolated SQLite."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import uuid
from pathlib import Path

from pmt.db import Database
from pmt.util import new_id, utc_now
from pmt.verification import handle as verification_handle


def _git(workspace: Path, *args):
    result = subprocess.run(["git", "-C", str(workspace), *args], capture_output=True,
                            check=False, text=True, encoding="utf-8", shell=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _claim(db, scope_id, workspace, *, session="test-main", resource=".", step_id=None, record_scope_id=None):
    step_id, job_id, run_id = step_id or new_id(), new_id(), new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (step_id, "step", record_scope_id or scope_id, "Git sync", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (job_id, step_id, "running", "{}", now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run_id, job_id, step_id, 1, "running", 1, session, 1, str(workspace.resolve()), "[]", "{}", "{}", now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                     (new_id(), run_id, session, "path", str(workspace.resolve()), resource, now))
    return run_id


def _graph(scope_id, step_id=None, extra_step_ids=()):
    req_id, impl_id = new_id(), new_id()
    return {
        "schema_version": 1, "project_id": scope_id, "graph_version": 1,
        "nodes": [
            {"id": req_id, "tree_kind": "requirement", "node_kind": "goal", "summary": "기준 commit 추적",
             "premise": "승인된 변경만 기준으로 반영한다", "source_refs": ["request:git"],
             "criteria": ["검토 commit 저장"], "product_stage": "prototype",
             "product_scope": {"applies": True, "reason": "초기 적용", "criteria": ["변경을 추적"]},
             "autonomy": {"authority": "user", "scope": "commit 검토"},
             "stop_reason": "implementation_boundary"},
            {"id": impl_id, "tree_kind": "implementation", "node_kind": "reconcile", "summary": "변경 참조를 확인",
             "premise": "파일 이름은 의미 판단을 대신하지 않는다", "product_stage": "prototype",
             "product_scope": {"applies": True, "reason": "기준 갱신", "criteria": ["review gate"]},
             "autonomy": {"authority": "ai", "scope": "Git 조회 방법"}, "stop_reason": "file_edit_boundary",
             "framework_assignment": "Python stdlib", "architecture": "Git read → review → SQLite refs",
             "logging": "git.baseline.updated", "tests": ["temporary Git repository"],
             "file_refs": ["src/feature.txt"],
             "work_item_step_refs": {"work": [], "item": [], "step": ([step_id] if step_id else []) + list(extra_step_ids)},
             "function_spec": {"input": "claimed workspace", "output": "baseline metadata",
                               "constraints": "read-only Git", "invariants": "dirty content preserved",
                               "errors": "divergence remains pending", "verification": "isolated SQLite"},
             "choice_set": {"options": [
                 {"label": "git CLI", "rationale": "repository metadata is local", "verified_refs": ["source:git"]},
                 {"label": "Python binding", "rationale": "library would add dependency", "verified_refs": ["source:stdlib"]},
                 {"label": "manual hash", "rationale": "does not expose commit graph", "verified_refs": ["source:git-contract"]}]},
             "choice": {"source": "ai", "selected": "git CLI", "reason": "existing environment provides git", "scope": "read-only"}},
        ],
        "relations": [{"id": new_id(), "kind": "implements", "from": req_id, "to": impl_id}],
        "provenance": {"request_ref": "request:git", "decision": "baseline review required"},
    }


def _init_git_project(workspace, scope_id, step_id=None, extra_step_ids=()):
    workspace.mkdir(parents=True)
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.email", "pmt-test@example.invalid")
    _git(workspace, "config", "user.name", "PMT test")
    docs = workspace / "docs" / "pmt-docs"
    docs.mkdir(parents=True)
    (docs / "plan.graph.json").write_text(json.dumps(_graph(scope_id, step_id, extra_step_ids), ensure_ascii=False), encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "feature.txt").write_text("one\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "initial project")
    return _git(workspace, "rev-parse", "HEAD")


def _init_nested_git_project(repo_root, project_relative, scope_id):
    repo_root.mkdir(parents=True)
    _git(repo_root, "init", "-b", "main")
    _git(repo_root, "config", "user.email", "pmt-test@example.invalid")
    _git(repo_root, "config", "user.name", "PMT test")
    workspace = repo_root / project_relative
    docs = workspace / "docs" / "pmt-docs"
    docs.mkdir(parents=True)
    (docs / "plan.graph.json").write_text(json.dumps(_graph(scope_id), ensure_ascii=False), encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "feature.txt").write_text("one\n", encoding="utf-8")
    (repo_root / "unrelated.txt").write_text("outside project scope\n", encoding="utf-8")
    _git(repo_root, "add", ".")
    _git(repo_root, "commit", "-m", "nested project baseline")
    return workspace, _git(repo_root, "rev-parse", "HEAD")


def test_actual_project_checkout_resolves_its_parent_repository_anchor():
    from pmt.reconciliation.service import _git_context

    workspace = Path(__file__).resolve().parents[1]
    root, relative = _git_context(workspace)
    assert root == workspace.parent
    assert relative == workspace.name


def _sync(cli, request_factory, scope_id, workspace, run_id, **payload):
    data = {"workspace": str(workspace.resolve()), "run_id": run_id, "activity": "active", **payload}
    return cli.call(request_factory("sync_project_baseline", data, scope_id=scope_id))


def _verify_request(operation, payload, target_id):
    return {"protocol_version": 1, "operation": operation, "request_id": new_id(), "actor": "main",
            "session_id": "test-main", "record_id": target_id, "payload": payload}


def _record_pass_evidence(db, scope_id, workspace, target_id):
    body = json.dumps({"workspace": str(workspace), "criteria": ["snapshot matches"]})
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,created_at,updated_at) "
                     "VALUES(?,'item',?,'evidence reuse','Planned',?,?,?)",
                     (target_id, scope_id, body, now, now))
    content = b"real evidence\n"
    artifact_id = new_id()
    relative = f"resources/evidence-{artifact_id}.txt"
    evidence = db.root / relative
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    with db.write() as conn:
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,created_at) "
                     "VALUES(?,?,?,?,?,'ready',?)",
                     (artifact_id, scope_id, digest, len(content), relative, now))
    base = {"definition_id": "q7-real-test", "definition_version": "1", "target_id": target_id,
            "command": ["python", "-m", "pytest"]}
    with db.connect() as conn:
        before = verification_handle(db, conn, _verify_request("lookup_verification", base, target_id))
    payload = {**base, "outcome": "pass", "exit_code": 0, "evidence_ids": [artifact_id],
               "before_fingerprint": before["input_fingerprint"]}
    saved, code = db.run_request(_verify_request("record_verification", payload, target_id),
                                 lambda conn, req: verification_handle(db, conn, req))
    assert code == 0 and saved["ok"], saved
    with db.connect() as conn:
        current = verification_handle(db, conn, _verify_request("lookup_verification", base, target_id))
    assert current["reusable"] is True
    return base


def test_real_git_baseline_review_dirty_preservation_and_same_commit_skip(cli, request_factory, create_project,
                                                                          parse_cli_response, tmp_path):
    scope_id = create_project("Q7 Git")
    workspace = tmp_path / "repository"
    first = _init_git_project(workspace, scope_id)
    graph = json.loads((workspace / "docs" / "pmt-docs" / "plan.graph.json").read_text(encoding="utf-8"))
    implementation_id = next(node["id"] for node in graph["nodes"] if node["tree_kind"] == "implementation")
    db = Database(cli.data_root, cli.config_root)
    run_id = _claim(db, scope_id, workspace)

    pending = _sync(cli, request_factory, scope_id, workspace, run_id)
    assert pending.returncode == 0, pending.stdout + pending.stderr
    pending_result = parse_cli_response(pending.stdout)["result"]
    assert pending_result["status"] == "review_required"
    assert pending_result["baseline_advanced"] is False

    accepted = _sync(cli, request_factory, scope_id, workspace, run_id,
                     reviewed_changes=True, impact_set_reconciled=True,
                     reviewed_paths=[], impact_node_ids=[])
    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    accepted_result = parse_cli_response(accepted.stdout)["result"]
    assert accepted_result["status"] == "updated" and accepted_result["reviewed_commit"] == first
    with db.connect() as conn:
        baseline = conn.execute("SELECT reviewed_commit,revision FROM project_baselines WHERE scope_id=?", (scope_id,)).fetchone()
        assert baseline["reviewed_commit"] == first

    verification_target = new_id()
    evidence_lookup = _record_pass_evidence(db, scope_id, workspace, verification_target)

    unchanged = _sync(cli, request_factory, scope_id, workspace, run_id)
    unchanged_result = parse_cli_response(unchanged.stdout)["result"]
    assert unchanged_result["status"] == "unchanged" and unchanged_result["analysis_skipped"] is True

    (workspace / "src" / "feature.txt").write_text("two\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "update feature")
    second = _git(workspace, "rev-parse", "HEAD")
    review = _sync(cli, request_factory, scope_id, workspace, run_id)
    review_result = parse_cli_response(review.stdout)["result"]
    assert review_result["status"] == "review_required"
    assert review_result["head_commit"] == second
    assert review_result["changed_paths"] == ["src/feature.txt"]
    assert review_result["baseline_advanced"] is False
    assert review_result["main_action"]
    with db.connect() as conn:
        stale = verification_handle(db, conn, _verify_request("lookup_verification", evidence_lookup, verification_target))
    assert stale["reusable"] is False and stale["status"] == "stale"

    accepted_change = _sync(cli, request_factory, scope_id, workspace, run_id,
                            reviewed_changes=True, impact_set_reconciled=True,
                            reviewed_paths=["src/feature.txt"], impact_node_ids=[implementation_id])
    assert accepted_change.returncode == 0, accepted_change.stdout + accepted_change.stderr
    assert parse_cli_response(accepted_change.stdout)["result"]["reviewed_commit"] == second

    (workspace / "local-dirty.txt").write_text("keep me\n", encoding="utf-8")
    dirty = _sync(cli, request_factory, scope_id, workspace, run_id, dirty_owner_session="human-owner")
    dirty_result = parse_cli_response(dirty.stdout)["result"]
    assert dirty_result["status"] == "main_action_required" and dirty_result["baseline_advanced"] is False
    assert dirty_result["reason"] == "dirty_owner_unconfirmed"
    assert dirty_result["dirty_owner"] == "human-owner"
    assert (workspace / "local-dirty.txt").read_text(encoding="utf-8") == "keep me\n"


def test_same_owner_dirty_fingerprint_pins_current_execution_basis(cli, request_factory, create_project,
                                                                   parse_cli_response, tmp_path):
    scope_id = create_project("Q7 same owner dirty pin")
    workspace = tmp_path / "repository"
    first = _init_git_project(workspace, scope_id)
    db = Database(cli.data_root, cli.config_root)
    run_id = _claim(db, scope_id, workspace)
    initial = _sync(cli, request_factory, scope_id, workspace, run_id,
                    reviewed_changes=True, impact_set_reconciled=True, reviewed_paths=[], impact_node_ids=[])
    assert parse_cli_response(initial.stdout)["result"]["reviewed_commit"] == first
    local_change = workspace / "src" / "maintained-locally.txt"
    local_change.write_text("preserve and pin this state\n", encoding="utf-8")

    unconfirmed = _sync(cli, request_factory, scope_id, workspace, run_id,
                        dirty_owner_session="test-main")
    observation = parse_cli_response(unconfirmed.stdout)["result"]
    assert observation["status"] == "main_action_required"
    assert observation["reason"] == "dirty_fingerprint_unconfirmed"

    pinned = _sync(cli, request_factory, scope_id, workspace, run_id,
                   dirty_owner_session="test-main",
                   reviewed_dirty_fingerprint=observation["dirty_fingerprint"])
    pinned_result = parse_cli_response(pinned.stdout)["result"]
    assert pinned_result["status"] == "dirty_owner_pinned"
    assert pinned_result["baseline_advanced"] is False and pinned_result["state_pinned"] is True
    assert pinned_result["execution_basis"]["commit"] == first
    assert pinned_result["execution_basis"]["documents"]["docs/pmt-docs/plan.graph.json"]
    with db.connect() as conn:
        baseline = conn.execute("SELECT reviewed_commit FROM project_baselines WHERE scope_id=?", (scope_id,)).fetchone()
    assert baseline["reviewed_commit"] == first
    assert local_change.read_text(encoding="utf-8") == "preserve and pin this state\n"


def test_acknowledged_impact_invalidates_plan_and_requests_active_run_cancel(cli, request_factory,
                                                                              create_project,
                                                                              parse_cli_response, tmp_path):
    scope_id = create_project("Q7 plan impact")
    workspace = tmp_path / "repository"
    db = Database(cli.data_root, cli.config_root)
    step_id = new_id()
    queued_step_id, unrelated_step_id = new_id(), new_id()
    first = _init_git_project(workspace, scope_id, step_id, [queued_step_id])
    classification_id = new_id()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (classification_id, "classification", scope_id, "feature", "{}", utc_now(), utc_now()))
    run_id = _claim(db, scope_id, workspace, step_id=step_id, record_scope_id=classification_id)
    graph = json.loads((workspace / "docs" / "pmt-docs" / "plan.graph.json").read_text(encoding="utf-8"))
    graph_path = workspace / "docs" / "pmt-docs" / "plan.graph.json"
    graph_hash = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    now = utc_now()
    plan_id = new_id()
    directive_id = new_id()
    with db.write() as conn:
        conn.execute("INSERT INTO plans(id,scope_id,artifact_id,graph_version,requirements_version,plan_version,state,workspace,relative_path,sha256,baseline_commit,created_at,updated_at) "
                     "VALUES(?,?,NULL,1,'1','1','published',?,'docs/pmt-docs/plan.graph.json',?,?,?,?)",
                     (plan_id, scope_id, str(workspace.resolve()), graph_hash, first, now, now))
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,created_at) VALUES(?,?,?,?,?,'ready',?)",
                     (directive_id, scope_id, "d" * 64, 0, f"resources/directive-{directive_id}", now))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
                     "VALUES(?,?,1,'1','1',?,'worker','prototype',?,?, '[]','[]',?,?)",
                     (step_id, directive_id, plan_id, str(workspace.resolve()),
                          json.dumps([{"kind": "path", "workspace": str(workspace.resolve()), "resource": "src/feature.txt"}]),
                          now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,body_json,created_at,updated_at) VALUES(?,'step',?,'Queued impacted Step','{}',?,?)",
                     (queued_step_id, classification_id, now, now))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
                     "SELECT ?,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,?,? FROM step_specs WHERE step_id=?",
                     (queued_step_id, now, now, step_id))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,body_json,created_at,updated_at) VALUES(?,'step',?,'Unrelated Step','{}',?,?)",
                     (unrelated_step_id, classification_id, now, now))
        queued_job, queued_run = new_id(), new_id()
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,'queued','{}',?,?)",
                     (queued_job, queued_step_id, now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,1,'queued',1,'other-owner',1,?,'[]','{}','{}',?,?)",
                     (queued_run, queued_job, queued_step_id, str(workspace.resolve()), now, now))
    initial = _sync(cli, request_factory, scope_id, workspace, run_id,
                    reviewed_changes=True, impact_set_reconciled=True, reviewed_paths=[], impact_node_ids=[])
    assert parse_cli_response(initial.stdout)["result"]["reviewed_commit"] == first
    (workspace / "src" / "feature.txt").write_text("changed implementation\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "change mapped implementation")
    second = _git(workspace, "rev-parse", "HEAD")
    implementation_id = next(node["id"] for node in graph["nodes"] if node["tree_kind"] == "implementation")
    from pmt.reconciliation.service import execute_file
    accepted, accepted_code = execute_file(db, request_factory("sync_project_baseline", {
        "workspace": str(workspace.resolve()), "run_id": run_id, "activity": "active",
        "reviewed_changes": True, "impact_set_reconciled": True,
        "reviewed_paths": ["src/feature.txt"], "impact_node_ids": [implementation_id]}, scope_id=scope_id))
    assert accepted_code == 0, accepted
    result = accepted["result"]
    assert result["reviewed_commit"] == second
    assert result["invalidated_step_ids"] == sorted([step_id, queued_step_id])
    with db.connect() as conn:
        plan = conn.execute("SELECT plan_version FROM plans WHERE scope_id=?", (scope_id,)).fetchone()
        run = conn.execute("SELECT state FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        locks = conn.execute("SELECT count(*) FROM scope_locks WHERE run_id=?", (run_id,)).fetchone()[0]
        step = conn.execute("SELECT body_json,revision,scope_id FROM records WHERE id=?", (step_id,)).fetchone()
        queued = conn.execute("SELECT r.state,r.stop_confirmed,r.completed_at,j.state job_state FROM execution_runs r JOIN execution_jobs j ON j.id=r.job_id WHERE r.step_id=?",
                              (queued_step_id,)).fetchone()
        queued_step = conn.execute("SELECT body_json,revision FROM records WHERE id=?", (queued_step_id,)).fetchone()
        unrelated = conn.execute("SELECT body_json,revision FROM records WHERE id=?", (unrelated_step_id,)).fetchone()
    assert plan["plan_version"] == "2"
    assert run["state"] == "cancel_requested" and locks == 1
    assert json.loads(step["body_json"])["invalidated"] is True
    assert json.loads(step["body_json"])["invalidation_reason"] == "git_baseline_changed"
    assert step["revision"] == 2 and step["scope_id"] == classification_id
    assert queued["state"] == queued["job_state"] == "canceled" and queued["stop_confirmed"] == 1
    assert queued["completed_at"] and queued_step["revision"] == 2
    assert json.loads(queued_step["body_json"])["invalidated"] is True
    assert json.loads(unrelated["body_json"]).get("invalidated") is None and unrelated["revision"] == 1
    execution = __import__("pmt.execution.service", fromlist=["handle"])
    enqueue = {"protocol_version": 1, "operation": "enqueue_execution", "request_id": new_id(),
               "actor": "main", "session_id": "test-main", "payload": {"step_id": step_id}}
    rejected, code = db.run_request(enqueue, lambda conn, req: execution.handle(db, conn, req))
    assert code == 3 and rejected["error"]["code"] == "step_invalidated"


def test_diverged_branch_requires_reconciliation_and_does_not_advance(cli, request_factory, create_project,
                                                                      parse_cli_response, tmp_path):
    scope_id = create_project("Q7 branch")
    workspace = tmp_path / "repository"
    first = _init_git_project(workspace, scope_id)
    db = Database(cli.data_root, cli.config_root)
    run_id = _claim(db, scope_id, workspace)
    initial = _sync(cli, request_factory, scope_id, workspace, run_id,
                    reviewed_changes=True, impact_set_reconciled=True, reviewed_paths=[], impact_node_ids=[])
    assert parse_cli_response(initial.stdout)["result"]["reviewed_commit"] == first
    _git(workspace, "checkout", "--orphan", "other")
    for child in workspace.iterdir():
        if child.name != ".git":
            if child.is_dir():
                import shutil
                shutil.rmtree(child)
            else:
                child.unlink()
    (workspace / "other.txt").write_text("different history\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "new root")
    _git(workspace, "branch", "-D", "main")
    _git(workspace, "branch", "-m", "main")
    response = _sync(cli, request_factory, scope_id, workspace, run_id)
    result = parse_cli_response(response.stdout)["result"]
    assert result["status"] == "reconciliation_required"
    assert result["reason"] == "history_diverged"
    assert result["baseline_advanced"] is False
    with db.connect() as conn:
        row = conn.execute("SELECT reviewed_commit,selected_ref FROM project_baselines WHERE scope_id=?", (scope_id,)).fetchone()
        assert row["reviewed_commit"] == first and row["selected_ref"] == "main"


def test_nested_project_uses_verified_nearest_git_root_and_scoped_diffs(cli, request_factory,
                                                                        create_project,
                                                                        parse_cli_response, tmp_path):
    scope_id = create_project("Q7 nested project")
    repo_root = tmp_path / "outer-repository"
    workspace, first = _init_nested_git_project(repo_root, "packages/project-a", scope_id)
    db = Database(cli.data_root, cli.config_root)
    run_id = _claim(db, scope_id, workspace)
    initial = _sync(cli, request_factory, scope_id, workspace, run_id,
                    reviewed_changes=True, impact_set_reconciled=True, reviewed_paths=[], impact_node_ids=[])
    initial_result = parse_cli_response(initial.stdout)["result"]
    assert initial_result["reviewed_commit"] == first
    assert initial_result["repository_root"] == str(repo_root.resolve())
    assert initial_result["project_relative"] == "packages/project-a"

    (repo_root / "unrelated.txt").write_text("outside project changed\n", encoding="utf-8")
    _git(repo_root, "add", "unrelated.txt")
    _git(repo_root, "commit", "-m", "outer repository only")
    second = _git(repo_root, "rev-parse", "HEAD")
    outside = _sync(cli, request_factory, scope_id, workspace, run_id,
                    reviewed_changes=True, impact_set_reconciled=True, reviewed_paths=[], impact_node_ids=[])
    outside_result = parse_cli_response(outside.stdout)["result"]
    assert outside_result["reviewed_commit"] == second
    assert outside_result["changed_paths"] == []

    (workspace / "src" / "feature.txt").write_text("two\n", encoding="utf-8")
    _git(repo_root, "add", "packages/project-a/src/feature.txt")
    _git(repo_root, "commit", "-m", "nested project source change")
    review = _sync(cli, request_factory, scope_id, workspace, run_id)
    review_result = parse_cli_response(review.stdout)["result"]
    assert review_result["changed_paths"] == ["src/feature.txt"]
    assert review_result["mapped_paths"] == ["src/feature.txt"]


def test_nested_project_uses_verified_nearest_git_root_and_scoped_diffs(cli, request_factory,
                                                                        create_project,
                                                                        parse_cli_response, tmp_path):
    scope_id = create_project("Q7 nested project")
    repo_root = tmp_path / "outer-repository"
    workspace, first = _init_nested_git_project(repo_root, "packages/project-a", scope_id)
    db = Database(cli.data_root, cli.config_root)
    run_id = _claim(db, scope_id, workspace)
    initial = _sync(cli, request_factory, scope_id, workspace, run_id,
                    reviewed_changes=True, impact_set_reconciled=True, reviewed_paths=[], impact_node_ids=[])
    initial_result = parse_cli_response(initial.stdout)["result"]
    assert initial_result["reviewed_commit"] == first
    assert initial_result["repository_root"] == str(repo_root.resolve())
    assert initial_result["project_relative"] == "packages/project-a"

    (repo_root / "unrelated.txt").write_text("outside project changed\n", encoding="utf-8")
    _git(repo_root, "add", "unrelated.txt")
    _git(repo_root, "commit", "-m", "outer repository only")
    second = _git(repo_root, "rev-parse", "HEAD")
    outside = _sync(cli, request_factory, scope_id, workspace, run_id,
                    reviewed_changes=True, impact_set_reconciled=True, reviewed_paths=[], impact_node_ids=[])
    outside_result = parse_cli_response(outside.stdout)["result"]
    assert outside_result["reviewed_commit"] == second
    assert outside_result["changed_paths"] == []

    (workspace / "src" / "feature.txt").write_text("two\n", encoding="utf-8")
    _git(repo_root, "add", "packages/project-a/src/feature.txt")
    _git(repo_root, "commit", "-m", "nested project source change")
    review = _sync(cli, request_factory, scope_id, workspace, run_id)
    review_result = parse_cli_response(review.stdout)["result"]
    assert review_result["changed_paths"] == ["src/feature.txt"]
    assert review_result["mapped_paths"] == ["src/feature.txt"]


def test_non_git_and_missing_scope_lock_are_reported_without_touching_docs(cli, request_factory, create_project,
                                                                            parse_cli_response, tmp_path):
    scope_id = create_project("Q7 without Git")
    db = Database(cli.data_root, cli.config_root)
    with tempfile.TemporaryDirectory(prefix="pmt-q7-nongit-") as raw:
        workspace = Path(raw) / "plain"
        workspace.mkdir()
        (workspace / "docs" / "pmt-docs").mkdir(parents=True)
        (workspace / "docs" / "pmt-docs" / "note.md").write_text("preserve\n", encoding="utf-8")
        run_id = _claim(db, scope_id, workspace)
        response = _sync(cli, request_factory, scope_id, workspace, run_id)
        result = parse_cli_response(response.stdout)["result"]
        assert result["status"] == "git_unavailable" and result["baseline_advanced"] is False
        assert (workspace / "docs" / "pmt-docs" / "note.md").read_text(encoding="utf-8") == "preserve\n"

        unclaimed = Path(raw) / "another"
        unclaimed.mkdir()
        denied = _sync(cli, request_factory, scope_id, unclaimed, new_id())
        assert denied.returncode == 3
        assert parse_cli_response(denied.stdout)["error"]["code"] == "ownership_conflict"


def test_incomplete_git_journal_is_resumed_on_retry(cli, request_factory, create_project,
                                                     parse_cli_response, tmp_path):
    scope_id = create_project("Q7 recovery")
    workspace = tmp_path / "repository"
    _init_git_project(workspace, scope_id)
    db = Database(cli.data_root, cli.config_root)
    run_id = _claim(db, scope_id, workspace)
    request = request_factory("sync_project_baseline", {"workspace": str(workspace.resolve()),
                                                          "run_id": run_id, "activity": "active"},
                              scope_id=scope_id)
    journal_id = str(uuid.uuid5(uuid.UUID(request["request_id"]), "git.sync_project_baseline"))
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO operation_journal(id,kind,state,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (journal_id, "git.sync_project_baseline", "reading",
                      json.dumps({"request_id": request["request_id"]}), now, now))
    recovered = cli.call(request)
    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert parse_cli_response(recovered.stdout)["result"]["status"] == "review_required"
    with db.connect() as conn:
        journal = conn.execute("SELECT state,body_json FROM operation_journal WHERE id=?", (journal_id,)).fetchone()
        assert journal["state"] == "review_required"
        assert json.loads(journal["body_json"])["request_id"] == request["request_id"]


def test_dirty_file_outside_run_scope_is_not_hashed(cli, request_factory, create_project, tmp_path, monkeypatch):
    scope_id = create_project("Q7 scoped reader")
    workspace = tmp_path / "repository"
    _init_git_project(workspace, scope_id)
    db = Database(cli.data_root, cli.config_root)
    run_id = _claim(db, scope_id, workspace, resource="src/owned.txt")
    secret_path = workspace / "src" / "private-by-other-worker.txt"
    secret_path.write_text("another owner's content\n", encoding="utf-8")

    import pmt.reconciliation.service as reconciliation
    def forbidden_hash(*args, **kwargs):
        raise AssertionError("an out-of-scope dirty file was read")
    monkeypatch.setattr(reconciliation, "_status_and_dirty", forbidden_hash)
    request = {"protocol_version": 1, "operation": "sync_project_baseline", "request_id": new_id(),
               "actor": "main", "session_id": "test-main", "scope_id": scope_id,
               "payload": {"workspace": str(workspace.resolve()), "run_id": run_id, "activity": "active"}}
    with db.connect() as conn:
        assert tuple(conn.execute("SELECT kind,resource FROM scope_locks WHERE run_id=?", (run_id,)).fetchone()) == ("path", "src/owned.txt")
        assert reconciliation._require_any_run_scope(conn, db, request, workspace)["id"] == run_id
    response, code = reconciliation.execute_file(db, request)
    assert code == 0 and response["ok"] is True, response.get("error")
    assert response["result"]["status"] == "main_action_required"
    assert "dirty_paths_outside_owned_scope" == response["result"]["reason"]
    assert secret_path.read_text(encoding="utf-8") == "another owner's content\n"


def test_independent_worker_scope_does_not_require_repository_wide_exclusive_lock(cli, request_factory,
                                                                                   create_project,
                                                                                   parse_cli_response, tmp_path):
    scope_id = create_project("Q7 parallel scopes")
    workspace = tmp_path / "repository"
    _init_git_project(workspace, scope_id)
    db = Database(cli.data_root, cli.config_root)
    run_id = _claim(db, scope_id, workspace, resource="src/feature.txt")
    _claim(db, scope_id, workspace, session="other-worker", resource="src/independent.txt")
    initial = _sync(cli, request_factory, scope_id, workspace, run_id,
                    reviewed_changes=True, impact_set_reconciled=True, reviewed_paths=[], impact_node_ids=[])
    assert parse_cli_response(initial.stdout)["result"]["status"] == "updated"

    (workspace / "src" / "feature.txt").write_text("next\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "owned source change")
    review = _sync(cli, request_factory, scope_id, workspace, run_id)
    result = parse_cli_response(review.stdout)["result"]
    assert result["status"] == "review_required"
    assert result["main_action"] == "the current run does not own a read scope for this path"
