"""R1/R4 deterministic rules and isolated metadata projections."""
from __future__ import annotations

import json
import hashlib
import sqlite3
import subprocess
import uuid
from pathlib import Path

from pmt.db import Database
from pmt.store import LocalStore
from pmt.util import canonical_json, new_id, utc_now
from pmt.continuity import context, current


def _basis(*, source_hash="a" * 64, inventory_ref="11111111-1111-4111-8111-111111111111",
           work_hash="b" * 64, complete=True):
    return current.basis_body(
        scope={"project_id": "project-ref"},
        source={"repository_id": "repository-ref", "branch": "main", "workspace_ref": "pmt://repo/ref",
                "observed_head": "head", "analyzed_ref": None, "applied_ref": None,
                "dirty_state": "clean", "dirty_fingerprint": None, "inventory_ref": inventory_ref,
                "inventory_hash": source_hash,
                "inventory_coverage": {"selected_count": 1, "verified_count": 1, "unknown_count": 0,
                                       "complete": complete, "reason_codes": []}},
        contract={"graph_schema": 1, "graph_revision": 1, "graph_hash": "c" * 64,
                  "requirement_refs": [], "decision_refs": []},
        work={"capture_ref": work_hash, "records": [], "run_refs": [], "claim_refs": [], "pending_refs": []},
        conditions={"environment_id": "environment-ref", "selected": [], "unknown": []},
        manifest={"components": [{"name": "source", "captured_at": "time", "authority": "test",
                                  "version": "v1", "complete": complete},
                                 {"name": "work", "captured_at": "time", "authority": "test",
                                  "version": "v1", "complete": True}],
                  "coherence": "coherent", "reasons": []})


def test_implementation_level_needs_actual_structured_verification_receipt():
    assert current.classify_implementation(planned=True) == "planned"
    assert current.classify_implementation(planned=False, implementation_refs=["module-ref"]) == "implemented"
    assert current.classify_implementation(
        planned=False, implementation_refs=["module-ref"],
        verification={"state": "valid", "outcome": "pass", "definition_ref": "def-ref",
                      "evidence_refs": ["artifact-ref"], "basis_ref": "basis-ref"}) == "verified"
    assert current.classify_implementation(
        planned=False, implementation_refs=["module-ref"],
        verification={"state": "valid", "outcome": "pass", "definition_ref": "def-ref",
                      "evidence_refs": [], "basis_ref": "basis-ref"}) == "implemented"


def test_basis_comparison_ignores_private_detail_ref_but_checks_source_and_work():
    prior = _basis()
    same = _basis(inventory_ref="22222222-2222-4222-8222-222222222222")
    assert current.validate_basis_components(prior, same)["status"] == "unchanged"
    changed_source = _basis(source_hash="d" * 64)
    assert current.validate_basis_components(prior, changed_source)["changed_components"] == ["source"]
    changed_work = _basis(work_hash="e" * 64)
    assert current.validate_basis_components(prior, changed_work)["changed_components"] == ["work"]
    assert _basis(complete=False)["complete"] is False


def test_next_action_waits_for_existing_execution_and_never_executes():
    facts = {"scope_selected": True, "active_execution": [{"run_ref": "run-ref", "state": "queued"}]}
    action = context.propose_next_action(facts=facts, basis_status="unknown", authority_current=True)
    assert action["action"] == "wait"
    assert action["executable"] is False
    assert action["required_conditions"] == ["query_authoritative_run_state", "preserve_current_owner"]


def test_unknown_basis_requests_current_work_access():
    action = context.propose_next_action(
        facts={"scope_selected": True, "active_execution": []},
        basis_status="unknown", authority_current=True)
    assert action["action"] == "request_selection"
    assert "current_work_access_and_source_capture" in action["required_conditions"]
    assert action["executable"] is False


def test_overview_budget_marks_missing_mandatory_fields():
    overview = context.compose_overview(
        scope={"project_ref": "p"}, facts={"direction": {"goal": "x" * 2000},
        "work_items": [], "decision_refs": [], "direction_refs": [], "current_status": [],
        "active_execution": [], "unknowns": ["source_unknown"]},
        checkpoint=None, role="main", max_bytes=256, max_lines=8)
    assert overview["incomplete"] is True
    assert "direction" in overview["missing_required"]
    assert overview["delivery"]["unit"] == "utf8"


def test_current_facts_and_overview_use_isolated_cli_database(cli, request_factory, create_project, parse_cli_response):
    project_id = create_project("phase4 continuity")
    facts_result = cli.call(request_factory("read_current_facts", {}, scope_id=project_id))
    assert facts_result.returncode == 0, facts_result.stderr
    facts = parse_cli_response(facts_result.stdout)["result"]
    assert facts["metadata_only"] is True
    assert facts["private_directive_included"] is False
    assert facts["facts"]["scope_id"] == project_id
    assert "local_pending_spool_not_checked" in facts["facts"]["unknowns"]

    overview_result = cli.call(request_factory("compose_resume_overview", {}, scope_id=project_id))
    assert overview_result.returncode == 0, overview_result.stderr
    overview = parse_cli_response(overview_result.stdout)["result"]
    assert overview["metadata_only"] is True
    assert overview["private_detail_read"] is False
    assert overview["currentness"]["source"] == "unknown_without_current_work_access"
    assert overview["complete"] is False


def test_planning_checkpoint_uses_actual_project_decision_without_run_or_source(cli, request_factory, parse_cli_response):
    project = cli.call(request_factory("create_scope", {
        "kind": "project", "slug": "planning without run",
        "body": {"goal": "Preserve the current project direction",
                 "premise": "No implementation run exists yet",
                 "autonomy": "Ask before changing requirements"}}))
    assert project.returncode == 0, project.stderr
    project_id = parse_cli_response(project.stdout)["result"]["scope_id"]
    work = cli.call(request_factory("save_change", {
        "kind": "work", "title": "Clarify project direction", "reason": "Planning boundary",
        "body": {"summary": "Keep the decision visible", "goal": "Record the user decision"}},
        scope_id=project_id))
    assert work.returncode == 0, work.stderr
    work_id = parse_cli_response(work.stdout)["result"]["record_id"]
    decision = cli.call(request_factory("save_decision", {
        "decision_kind": "select", "decider": "test user", "title": "Planning choice",
        "option_id": "direction-a", "content": "Continue with direction A",
        "reason": "The user confirmed the project direction",
        "confirmation_source": "explicit test choice"},
        scope_id=project_id, record_id=work_id, expected_revision=1))
    assert decision.returncode == 0, decision.stderr
    decision_result = parse_cli_response(decision.stdout)
    with sqlite3.connect(cli.data_root / "pmt.sqlite3") as conn:
        boundary_event_id = conn.execute(
            "SELECT event_id FROM events WHERE event_type='decision_saved' AND record_id=? "
            "ORDER BY recorded_at DESC LIMIT 1", (work_id,)).fetchone()[0]

    checkpoint = cli.call(request_factory("create_checkpoint", {
        "boundary_event_id": boundary_event_id,
        "expected_pointer_revision": 0, "purpose": "planning"},
        scope_id=project_id, record_id=work_id))
    assert checkpoint.returncode == 0, checkpoint.stderr
    checkpoint_value = parse_cli_response(checkpoint.stdout)["result"]
    assert checkpoint_value["basis_ref"]
    stored = cli.call(request_factory("read_checkpoint", {
        "selector": {"repository_id": None, "branch": None, "workspace_ref": None,
                     "task_id": None, "purpose": "planning", "environment_id": None}},
        scope_id=project_id))
    assert stored.returncode == 0, stored.stderr
    saved_basis = parse_cli_response(stored.stdout)["result"]["checkpoint"]["basis_ref"]
    assert saved_basis == checkpoint_value["basis_ref"]
    checkpoint_body = parse_cli_response(stored.stdout)["result"]["checkpoint"]
    assert checkpoint_body["basis_complete"] is False
    assert checkpoint_body["source_currentness"] == "unknown"
    assert checkpoint_body["source"]["observed_head"] is None
    assert checkpoint_body["active_execution"] == []

    overview = cli.call(request_factory("compose_resume_overview", {}, scope_id=project_id))
    assert overview.returncode == 0, overview.stderr
    projection = parse_cli_response(overview.stdout)["result"]
    assert projection["metadata_only"] is True
    assert projection["overview"]["direction"]["goal"] == "Preserve the current project direction"
    assert projection["overview"]["implementation"]["level"] == "planned"
    assert projection["overview"]["implementation"]["source_currentness"] == "not_revalidated_by_metadata_read"
    assert projection["active_execution_count"] == 0


def _source_fixture(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "PMT test"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "pmt-test@example.invalid"], check=True)
    project_id, repo_id, work_id, item_id, step_id, run_id, job_id = (new_id() for _ in range(7))
    node_id = new_id()
    graph_path = "docs/pmt-docs/plan.graph.json"
    graph_file = repo / graph_path
    graph_file.parent.mkdir(parents=True)
    graph = {"schema_version": 1, "project_id": project_id, "graph_version": 1,
             "nodes": [{"id": node_id, "tree_kind": "requirement", "node_kind": "goal",
                        "summary": "Keep current source visible", "premise": "Only selected files are captured",
                        "source_refs": ["request:test"], "criteria": ["basis is current"],
                        "product_stage": "prototype", "product_scope": {"applies": True, "reason": "Selected test project",
                                                                                "criteria": ["basis is current"]},
                        "autonomy": {"authority": "method", "scope": "current selected task"}, "evidence_refs": [],
                        "work_item_step_refs": {"work": [work_id], "item": [item_id], "step": [step_id]}}],
             "relations": [], "provenance": {"request_ref": "fixture"}}
    graph_file.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", graph_path], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "fixture graph"], check=True)
    data, config = tmp_path / "data", tmp_path / "config"
    db = Database(data, config)
    now, actor, session = utc_now(), "phase4-test", "phase4-session"
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (repo_id, "repository", "repo", "{}", now, now))
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (project_id, "project", repo_id, "project",
                      canonical_json({"repository_id": repo_id, "workspace": str(repo),
                                      "goal": "Keep the selected source and project direction visible"}), now, now))
        for record_id, kind, parent_id, title, body in (
            (work_id, "work", None, "Current work", {"summary": "Resume safely", "goal": "Preserve direction"}),
            (item_id, "item", work_id, "Current item", {"summary": "Use actual source"}),
            (step_id, "step", item_id, "Current step", {"summary": "Capture selected basis"}),
        ):
            conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
                         "VALUES(?,?,?,?,?,'InProgress',?,1,?,?)",
                         (record_id, kind, project_id, parent_id, title, canonical_json(body), now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) "
                     "VALUES(?,?,'running','{}',?,?)", (job_id, step_id, now, now))
        scopes = [{"kind": "path", "workspace": str(repo), "resource": graph_path}]
        intent = {"task": {"task_id": item_id, "step_id": step_id}, "run_id": run_id,
                  "directive_version": 1, "scope_id": project_id}
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,"
                     "workspace,scopes_json,route_json,intent_json,created_at,updated_at) "
                     "VALUES(?,?,?,1,'running',1,?,1,?,?, '{}',?,?,?)",
                     (run_id, job_id, step_id, session, str(repo), canonical_json(scopes),
                      canonical_json(intent), now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) "
                     "VALUES(?,?,?,?,?,?,?)", (new_id(), run_id, session, "path", str(repo), graph_path, now))
    from pmt.phase2_common import persist_json_resource
    directive = {"purpose": "Build a verified current task context", "goal": "Use the current source basis",
                 "non_goal": ["Do not broaden project access"], "change_scope": {"add": [], "modify": [],
                 "delete": [], "forbidden": ["claims and authentication"]},
                 "inputs": [{"name": "graph", "meaning": "source-pinned project graph"}],
                 "outputs": [{"name": "context", "meaning": "bounded owner-bound task projection"}],
                 "tests": [{"name": "source", "meaning": "actual source pin"}],
                 "logging": [{"name": "trace", "meaning": "safe identifiers only"}],
                 "context_refs": [node_id]}
    resource_req = {"request_id": new_id(), "actor": actor, "session_id": session,
                    "scope_id": project_id, "payload": {}}
    directive_resource = persist_json_resource(db, resource_req, directive, project_id,
                                                "step_directive", step_id)
    criteria = [{"id": "source-current", "meaning": "SourcePin is current", "evidence_refs": []}]
    step_body = {"directive_id": directive_resource["artifact_id"], "directive_version": 1,
                 "invalidated": False, "kind_tag": "test"}
    intent = {"task": {"task_id": item_id, "step_id": step_id}, "run_id": run_id,
              "directive_ref": directive_resource["artifact_id"], "directive_version": 1,
              "requirements_version": "requirements-v1", "plan_version": "plan-v1",
              "criteria": criteria, "role": "lower", "scope_id": project_id}
    with db.write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (canonical_json(step_body), step_id))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,"
                     "plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
                     "VALUES(?,?,1,'requirements-v1','plan-v1',NULL,'lower','prototype',?,?,?,'[]',?,?)",
                     (step_id, directive_resource["artifact_id"], str(repo),
                      canonical_json([{"kind": "path", "workspace": str(repo), "resource": graph_path}]),
                      canonical_json(criteria), now, now))
        conn.execute("UPDATE execution_runs SET intent_json=? WHERE id=?", (canonical_json(intent), run_id))
    return {"db": db, "store": LocalStore(db), "repo": repo, "project_id": project_id,
            "repo_id": repo_id, "work_id": work_id, "item_id": item_id, "step_id": step_id,
            "run_id": run_id, "actor": actor, "session": session, "graph_path": graph_path,
            "node_id": node_id, "directive_id": directive_resource["artifact_id"], "criteria": criteria}


def _continuity_request(env, operation, payload, *, request_id=None):
    return {"protocol_version": 1, "operation": operation, "request_id": request_id or new_id(),
            "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
            "record_id": env["work_id"], "payload": payload}


def test_capture_work_basis_reads_real_claimed_git_and_retains_private_inventory(tmp_path):
    env = _source_fixture(tmp_path)
    request = _continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"],
        "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"],
        "inventory_paths": [env["graph_path"]],
    })
    result, code = env["store"].execute(request)
    assert code == 0, result.get("error")
    basis = result["result"]["basis"]
    assert basis["complete"] is True
    assert basis["source"]["observed_head"]
    assert basis["source"]["workspace_ref"].startswith("pmt://" + env["repo_id"] + "/")
    assert basis["source"]["inventory_coverage"] == {
        "selected_count": 1, "verified_count": 1, "unknown_count": 0,
        "complete": True, "reason_codes": []}
    assert str(env["repo"]) not in canonical_json(basis)
    with env["db"].connect() as conn:
        stored_basis = env["db"]
        inventory_row = conn.execute("SELECT visibility,owner_session,body_json FROM continuity_objects WHERE id=?",
                                      (basis["source"]["inventory_ref"],)).fetchone()
        effect = conn.execute("SELECT state,outcome_json FROM continuity_journal WHERE request_id=?",
                              (request["request_id"],)).fetchone()
    assert inventory_row["visibility"] == "private"
    assert inventory_row["owner_session"] == env["session"]
    inventory = json.loads(inventory_row["body_json"])
    assert inventory["items"][0]["relative_path"] == env["graph_path"]
    assert effect["state"] == "completed"

    replay, replay_code = env["store"].execute(request)
    assert replay_code == 0
    assert replay == result
    env["repo"].joinpath(env["graph_path"]).write_text("changed after the recorded request", encoding="utf-8")
    assert env["store"].execute(request) == (result, 0)


def test_checkpoint_requires_persisted_boundary_and_same_event_never_moves_pointer_back(tmp_path):
    env = _source_fixture(tmp_path)
    store = env["store"]
    basis_request = _continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]]})
    captured, code = store.execute(basis_request)
    assert code == 0 and captured["ok"], captured.get("error")
    bad_request = _continuity_request(env, "create_checkpoint", {
        "basis_ref": captured["result"]["basis_ref"], "boundary_event_id": new_id(),
        "expected_pointer_revision": 0})
    denied, denied_code = store.execute(bad_request)
    assert denied_code == 3
    assert denied["error"]["code"] == "checkpoint_boundary_not_found"
    assert env["db"].get_request_result(bad_request["request_id"], env["actor"], env["session"])

    saved, code = store.execute({"protocol_version": 1, "operation": "save_decision", "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "record_id": env["work_id"], "expected_revision": 1,
        "payload": {"decision_kind": "select", "decider": "test user", "title": "Boundary",
                    "option_id": "resume", "content": "Persisted decision boundary",
                    "reason": "Fixture event", "confirmation_source": "explicit fixture choice"}})
    assert code == 0, saved
    with env["db"].connect() as conn:
        boundary = conn.execute("SELECT event_id FROM events WHERE event_type='decision_saved' "
                                 "AND record_id=? ORDER BY recorded_at DESC LIMIT 1", (env["work_id"],)).fetchone()[0]
    fresh_req = _continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]]})
    fresh, code = store.execute(fresh_req)
    assert code == 0, fresh
    checkpoint_payload = {"basis_ref": fresh["result"]["basis_ref"], "boundary_event_id": boundary,
                          "expected_pointer_revision": 0, "purpose": "current"}
    checkpoint, code = store.execute(_continuity_request(env, "create_checkpoint", checkpoint_payload))
    assert code == 0, checkpoint
    first_ref = checkpoint["result"]["checkpoint_ref"]
    second, code = store.execute(_continuity_request(env, "create_checkpoint", checkpoint_payload))
    assert code == 0, second.get("error")
    assert second["result"]["checkpoint_ref"] == first_ref
    assert second["result"]["pointer"]["revision"] == 1
    assert second["result"]["pointer"]["replayed"] is True

    saved2, code = store.execute({"protocol_version": 1, "operation": "save_decision", "request_id": new_id(),
        "actor": env["actor"], "session_id": env["session"], "scope_id": env["project_id"],
        "record_id": env["work_id"], "expected_revision": 2,
        "payload": {"decision_kind": "select", "decider": "test user", "title": "Boundary 2",
                    "option_id": "resume-2", "content": "A second persisted decision",
                    "reason": "Another explicit choice", "confirmation_source": "explicit test choice",
                    "supersedes": saved["result"]["decision_id"]}})
    assert code == 0, saved2.get("error")
    with env["db"].connect() as conn:
        boundary2 = conn.execute("SELECT event_id FROM events WHERE event_type='decision_saved' "
                                  "AND record_id=? ORDER BY recorded_at DESC LIMIT 1", (env["work_id"],)).fetchone()[0]
    fresh2, code = store.execute(_continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]]}))
    assert code == 0, fresh2.get("error")
    newer, code = store.execute(_continuity_request(env, "create_checkpoint", {
        "basis_ref": fresh2["result"]["basis_ref"], "boundary_event_id": boundary2,
        "expected_pointer_revision": 1, "purpose": "current"}))
    assert code == 0, newer.get("error")
    newer_ref = newer["result"]["checkpoint_ref"]
    assert newer_ref != first_ref

    old_event, code = store.execute(_continuity_request(env, "create_checkpoint", checkpoint_payload))
    assert code == 0, old_event.get("error")
    assert old_event["result"]["checkpoint_ref"] == first_ref
    assert old_event["result"]["pointer"]["revision"] == 2
    assert old_event["result"]["pointer"]["object_id"] == newer_ref
    with env["db"].connect() as conn:
        pointer = conn.execute("SELECT object_id,revision FROM continuity_pointers WHERE scope_id=?",
                               (env["project_id"],)).fetchone()
    assert (pointer["object_id"], pointer["revision"]) == (newer_ref, 2)


def test_r1_after_basis_and_real_change_ref_are_consumed_by_r4_bundle(tmp_path):
    env = _source_fixture(tmp_path)
    from pmt.storage_config import configure_storage

    branch = subprocess.run(["git", "-C", str(env["repo"]), "symbolic-ref", "--quiet", "--short", "HEAD"],
                            check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    configure_storage(env["db"].config_root, {"mode": "local", "expected_config_sha256": None,
        "workspace_mappings": [{"repository_id": env["repo_id"], "project_id": env["project_id"],
            "branch": branch, "branch_key_sha256": hashlib.sha256(branch.encode("utf-8")).hexdigest(),
            "local_root": str(env["repo"]), "relative_graph_path": env["graph_path"]}]})
    captured, code = env["store"].execute(_continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]]}))
    assert code == 0, captured.get("error")
    source = captured["result"]["basis"]["source"]
    branch_key = branch
    expected_workspace_ref = "pmt://" + env["repo_id"] + "/" + hashlib.sha256(branch_key.encode("utf-8")).hexdigest()
    assert source["workspace_ref"] == expected_workspace_ref

    graph_file = env["repo"] / env["graph_path"]
    graph_file.write_bytes(graph_file.read_bytes() + b" ")

    captured_after, code = env["store"].execute(_continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]]}))
    assert code == 0, captured_after.get("error")

    change_req = _continuity_request(env, "collect_changes", {
        "run_id": env["run_id"], "before_basis_ref": captured["result"]["basis_ref"],
        "after_basis_ref": captured_after["result"]["basis_ref"],
        "paths": [env["graph_path"]], "task_id": env["work_id"],
        "expected_pointer_revision": 0})
    change, code = env["store"].execute(change_req)
    assert code == 0, change.get("error")
    assert change["result"]["coverage"] == "complete"
    assert change["result"]["state"] == "complete"
    with env["db"].connect() as conn:
        change_body = __import__("pmt.continuity.storage", fromlist=["ContinuityStore"]).ContinuityStore(
            env["db"]).get(conn, change_req, change["result"]["change_ref"], kind="change")["body"]
    assert change_body["after_basis_ref"] == captured_after["result"]["basis_ref"]

    pin, code = env["store"].execute(_continuity_request(env, "capture_source_pin", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"]}))
    assert code == 0, pin.get("error")
    index, code = env["store"].execute(_continuity_request(env, "rebuild_graph_index", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "expected_source": pin["result"]["source_pin"]}))
    assert code == 0, index.get("error")
    links, code = env["store"].execute(_continuity_request(env, "build_implementation_links", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "paths": [env["graph_path"]],
        "basis_ref": captured_after["result"]["basis_ref"], "expected_pointer_revision": 0}))
    assert code == 0, links.get("error")
    assessment, code = env["store"].execute(_continuity_request(env, "assess_alignment", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "paths": [env["graph_path"]],
        "basis_ref": captured_after["result"]["basis_ref"],
        "change_ref": change["result"]["change_ref"], "index_ref": links["result"]["index_ref"]}))
    assert code == 0, assessment.get("error")
    captured_resume, code = env["store"].execute(_continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]]}))
    assert code == 0, captured_resume.get("error")

    resume, code = env["store"].execute(_continuity_request(env, "compose_task_resume", {
        "basis_ref": captured_resume["result"]["basis_ref"],
        "change_ref": change["result"]["change_ref"],
        "assessment_ref": assessment["result"]["assessment_ref"],
        "run_id": env["run_id"], "task_ref": {"task_id": env["item_id"],
            "step_id": env["step_id"], "run_id": env["run_id"]},
        "role": "lower", "expected_source": pin["result"]["source_pin"],
        "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]],
        "node_ids": [env["node_id"]],
        "budget": {"max_bytes": 262144, "max_lines": 2000, "unit": "utf8"},
        "expected_run_revision": 1}))
    assert code == 0, resume.get("error")
    assert "result" in resume, resume
    assert "change_evidence" in resume["result"], resume["result"]
    evidence = resume["result"]["change_evidence"]
    assert evidence["change_ref"] == change["result"]["change_ref"]
    assert evidence["assessment_ref"] == assessment["result"]["assessment_ref"]
    assert evidence["applied"] is False
    assert evidence["status"] == "change_requires_review"
    assert "current_applicability_unavailable" in evidence["reason_codes"]
    assert resume["result"]["complete"] is False


def test_r1_facts_classify_only_same_basis_link_and_actual_verification_receipts(tmp_path):
    env = _source_fixture(tmp_path)
    from pmt.storage_config import configure_storage

    branch = subprocess.run(["git", "-C", str(env["repo"]), "symbolic-ref", "--quiet", "--short", "HEAD"],
        check=True, capture_output=True, text=True, encoding="utf-8").stdout.strip()
    configure_storage(env["db"].config_root, {"mode": "local", "expected_config_sha256": None,
        "workspace_mappings": [{"repository_id": env["repo_id"], "project_id": env["project_id"],
            "branch": branch, "branch_key_sha256": hashlib.sha256(branch.encode("utf-8")).hexdigest(),
            "local_root": str(env["repo"]), "relative_graph_path": env["graph_path"]}]})
    verification_id, definition_id = new_id(), new_id()
    with env["db"].write() as conn:
        conn.execute("INSERT INTO verifications(id,definition_id,definition_version,target_id,environment_id,"
            "input_fingerprint,outcome,exit_code,evidence_json,includes_json,command_json,completed_at,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (verification_id, definition_id, "definition-v1",
                env["step_id"], env["db"].environment_id, "a" * 64, "pass", 0,
                canonical_json([env["directive_id"]]), "[]", "{}", "2026-10-06T00:00:01Z", "valid"))
    captured, code = env["store"].execute(_continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]],
        "condition_refs": [verification_id]}))
    assert code == 0, captured.get("error")
    basis_ref = captured["result"]["basis_ref"]
    links, code = env["store"].execute(_continuity_request(env, "build_implementation_links", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "paths": [env["graph_path"]],
        "basis_ref": basis_ref, "expected_pointer_revision": 0}))
    assert code == 0, links.get("error")

    facts_request = _continuity_request(env, "read_current_facts", {"basis_ref": basis_ref})
    facts, code = env["store"].execute(facts_request)
    assert code == 0, facts.get("error")
    implementation = facts["result"]["facts"]["implementation"]
    assert implementation["level"] == "verification_at_basis"
    assert implementation["source_currentness"] == "not_revalidated_by_metadata_read"
    assert implementation["implementation_refs"][0]["ref"] == links["result"]["index_ref"]
    assert implementation["verification_at_basis"][0]["verification_ref"] == verification_id
    assert implementation["current_applicability"] == "unknown_requires_read_applicability"

    with env["db"].write() as conn:
        conn.execute("INSERT INTO verifications(id,definition_id,definition_version,target_id,environment_id,"
            "input_fingerprint,outcome,exit_code,evidence_json,includes_json,command_json,completed_at,state) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (new_id(), definition_id, "definition-v1", env["step_id"],
                env["db"].environment_id, "b" * 64, "fail", 1, "[]", "[]", "{}",
                "2026-10-07T00:00:01Z", "valid"))
    after_failure, code = env["store"].execute(_continuity_request(env, "read_current_facts", {
        "basis_ref": basis_ref, "implementation_refs": ["caller-forged-ref"], "verified": True}))
    assert code == 0, after_failure.get("error")
    after = after_failure["result"]["facts"]["implementation"]
    assert after["level"] == "verification_at_basis"
    assert after["verification_after_basis_fail_refs"]
    assert "caller-forged-ref" not in canonical_json(after)
    assert after["current_applicability"] == "unknown_requires_read_applicability"


def test_r4_reads_the_actual_current_checkpoint_for_an_unchanged_basis(tmp_path):
    env = _source_fixture(tmp_path)
    store = env["store"]
    decision, code = store.execute({"protocol_version": 1, "operation": "save_decision",
        "request_id": new_id(), "actor": env["actor"], "session_id": env["session"],
        "scope_id": env["project_id"], "record_id": env["work_id"], "expected_revision": 1,
        "payload": {"decision_kind": "select", "decider": "fixture user", "title": "Keep source",
            "option_id": "keep", "content": "Keep the current source basis",
            "reason": "R4 checkpoint selector fixture", "confirmation_source": "explicit fixture choice"}})
    assert code == 0, decision.get("error")
    with env["db"].connect() as conn:
        event_id = conn.execute("SELECT event_id FROM events WHERE event_type='decision_saved' "
            "AND record_id=? ORDER BY recorded_at DESC LIMIT 1", (env["work_id"],)).fetchone()[0]
    captured, code = store.execute(_continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]]}))
    assert code == 0, captured.get("error")
    checkpoint, code = store.execute(_continuity_request(env, "create_checkpoint", {
        "basis_ref": captured["result"]["basis_ref"], "boundary_event_id": event_id,
        "expected_pointer_revision": 0, "purpose": "current"}))
    assert code == 0, checkpoint.get("error")
    pin, code = store.execute(_continuity_request(env, "capture_source_pin", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"]}))
    assert code == 0, pin.get("error")
    rebuilt, code = store.execute(_continuity_request(env, "rebuild_graph_index", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "expected_source": pin["result"]["source_pin"]}))
    assert code == 0, rebuilt.get("error")
    resume, code = store.execute(_continuity_request(env, "compose_task_resume", {
        "basis_ref": captured["result"]["basis_ref"], "run_id": env["run_id"],
        "task_ref": {"task_id": env["item_id"], "step_id": env["step_id"], "run_id": env["run_id"]},
        "role": "lower", "expected_source": pin["result"]["source_pin"],
        "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]],
        "node_ids": [env["node_id"]], "expected_run_revision": 1,
        "budget": {"max_bytes": 32768, "max_lines": 400},
        "f5_budget": {"max_bytes": 512, "max_lines": 40, "unit": "utf8"}}))
    assert code == 0, resume.get("error")
    evidence = resume["result"]["change_evidence"]
    assert evidence["status"] == "no_change_confirmed", evidence
    assert evidence["checkpoint_ref"] == checkpoint["result"]["checkpoint_ref"]
    assert evidence["checkpoint_basis_ref"] == captured["result"]["basis_ref"]


def test_task_resume_uses_existing_f5_owner_bound_projection_and_rechecks_detail(tmp_path):
    env = _source_fixture(tmp_path)
    store = env["store"]
    pin_req = _continuity_request(env, "capture_source_pin", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"]})
    pin_result, code = store.execute(pin_req)
    assert code == 0, pin_result.get("error")
    pin = pin_result["result"]["source_pin"]
    index_result, code = store.execute(_continuity_request(env, "rebuild_graph_index", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "expected_source": pin}))
    assert code == 0, index_result.get("error")

    captured, code = store.execute(_continuity_request(env, "capture_work_basis", {
        "run_id": env["run_id"], "repository_id": env["repo_id"], "workspace": str(env["repo"]),
        "relative_graph_path": env["graph_path"], "inventory_paths": [env["graph_path"]]}))
    assert code == 0, captured.get("error")
    payload = {"basis_ref": captured["result"]["basis_ref"], "run_id": env["run_id"],
        "task_ref": {"task_id": env["item_id"], "step_id": env["step_id"], "run_id": env["run_id"]},
        "role": "lower", "expected_source": pin, "repository_id": env["repo_id"],
        "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"],
        "inventory_paths": [env["graph_path"]], "node_ids": [env["node_id"]],
        "budget": {"max_bytes": 600, "max_lines": 50, "unit": "utf8"},
        "expected_run_revision": 1}
    bundle, code = store.execute(_continuity_request(env, "compose_task_resume", payload))
    assert code == 0, bundle.get("error")
    bundle_ref = bundle["result"].get("bundle_ref") or bundle["result"].get("detail_ref")
    assert bundle_ref

    detail, code = store.execute(_continuity_request(env, "read_resume_detail", {
        "bundle_ref": bundle_ref, "run_id": env["run_id"],
        "workspace": str(env["repo"]), "relative_graph_path": env["graph_path"],
        "max_bytes": 10_000, "max_lines": 100}))
    assert code == 0, detail.get("error")
    assert detail["result"]["owner_revalidated"] is True
    assert detail["result"]["source_revalidated"] is True
    assert detail["result"]["complete"] is False
    assert detail["result"]["detail"]["content"]
