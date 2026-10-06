from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path

from pmt.continuity.storage import ContinuityStore
from pmt.db import Database
from pmt.efficiency.source import SourcePin
from pmt.planning.graph import validate_graph
from pmt.service import execute
from pmt.storage_config import _read_profile, configure_storage
from pmt.util import canonical_json, fingerprint, new_id, utc_now
from pmt.continuity.current import basis_body
from pmt.continuity import changes
from test_phase2_reconciliation import _claim, _graph


def _git(path: Path, *args):
    result = subprocess.run(["git", "-C", str(path), *args], capture_output=True,
                            check=False, text=True, encoding="utf-8", shell=False)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _request(operation, payload, scope_id, *, request_id=None):
    return {"protocol_version": 1, "operation": operation, "request_id": request_id or new_id(),
            "actor": "main", "session_id": "test-main", "scope_id": scope_id, "payload": payload}


def _workspace(tmp_path, scope_id, repository_id, decision_id, config_root, *,
               relative_graph_path="docs/pmt-docs/plan.graph.json"):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.email", "pmt-test@example.invalid")
    _git(workspace, "config", "user.name", "PMT test")
    graph_path = workspace / Path(relative_graph_path)
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph = _graph(scope_id)
    method = next(item for item in graph["nodes"] if item["tree_kind"] == "implementation")
    method.update({"stop_reason": "user_delegated", "delegated_scope": "method details within accepted requirements",
                   "autonomy": {"authority": "user", "scope": "method details within accepted requirements"},
                   "source_refs": [decision_id]})
    graph_path.write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    (workspace / "docs" / "pmt-docs").mkdir(parents=True, exist_ok=True)
    (workspace / "docs" / "pmt-docs" / "plan.md").write_text("# Current plan\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "feature.py").write_text("def run():\n    return 1\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "initial project")
    head = _git(workspace, "rev-parse", "HEAD")
    repo_graph = validate_graph(graph, scope_id, complete=False)
    branch_hash = hashlib.sha256(b"main").hexdigest()
    configure_storage(config_root, {"mode": "local", "expected_config_sha256": None,
        "workspace_mappings": [{"repository_id": repository_id, "project_id": scope_id,
            "branch": "main", "branch_key_sha256": branch_hash, "local_root": str(workspace.resolve()),
            "relative_graph_path": relative_graph_path}]})
    return workspace, graph, head, repo_graph


def _setup_scope(cli, request_factory, scope_id):
    db = Database(cli.data_root, cli.config_root)
    repository_id, work_id = new_id(), new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,'repository',NULL,'test repository','{}',?,?)",
                     (repository_id, now, now))
        conn.execute("UPDATE scopes SET parent_id=? WHERE id=?", (repository_id, scope_id))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,'work',?,'Decision target','Planned','{}',1,?,?)",
                     (work_id, scope_id, now, now))
    decision_req = request_factory("save_decision", {"decision_kind": "delegate", "decider": "user",
        "reason": "Explicitly delegated implementation method details", "confirmation_source": "user choice",
        "delegation_scope": "method details within accepted requirements"}, scope_id=scope_id,
        record_id=work_id, expected_revision=1)
    response, code = execute(db, decision_req)
    assert code == 0 and response["ok"], response.get("error") or response
    return db, repository_id, response["result"]["decision_id"]


def _basis(db, scope_id, repository_id, workspace, graph, head, graph_report,
           inventory_paths=None):
    branch_hash = hashlib.sha256(b"main").hexdigest()
    workspace_ref = f"pmt://{repository_id}/{branch_hash}"
    inventory_paths = inventory_paths or ["src/feature.py", "docs/pmt-docs/plan.graph.json"]
    items, rows, statuses = [], [], []
    for relative in inventory_paths:
        target = workspace / Path(relative)
        content_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        item_status = "verified" if content_hash else "unknown"
        reason = None if content_hash else "inventory_source_unavailable"
        path_ref = fingerprint({"workspace_ref": workspace_ref, "relative_path": relative})
        items.append({"relative_path": relative, "content_hash": content_hash,
                      "status": item_status, "reason_code": reason})
        rows.append({"path_ref": path_ref, "content_hash": content_hash,
                     "status": item_status, "reason_code": reason})
        status = subprocess.run(["git", "-C", str(workspace), "status", "--porcelain=v1", "-z",
            "--untracked-files=all", "--", relative], capture_output=True, shell=False, check=True).stdout
        statuses.append(status.decode("utf-8"))
    inventory_hash = fingerprint(rows)
    inventory_body = {"version": 1, "workspace_ref": workspace_ref,
                      "inventory_hash": inventory_hash, "items": items}
    private_req = _request("read_current_facts", {}, scope_id)
    with db.write() as conn:
        inventory_obj = ContinuityStore(db).put(conn, private_req, "detail", inventory_body, visibility="private")
    dirty = any(item.strip() for item in statuses)
    body = {"version": 1, "scope": {"project_id": scope_id},
        "source": {"repository_id": repository_id, "branch": "main",
            "workspace_ref": workspace_ref, "observed_head": head,
            "analyzed_ref": head, "applied_ref": head,
            "dirty_state": "dirty" if dirty else "clean",
            "dirty_fingerprint": fingerprint({"selected": list(zip(inventory_paths, statuses))}) if dirty else None,
            "inventory_ref": inventory_obj["id"], "inventory_hash": inventory_hash,
            "inventory_coverage": {"selected_count": len(items),
                "verified_count": sum(item["status"] == "verified" for item in items),
                "unknown_count": sum(item["status"] != "verified" for item in items),
                "complete": all(item["status"] == "verified" for item in items),
                "reason_codes": sorted({item["reason_code"] for item in items if item["reason_code"]})}},
        "contract": {"graph_schema": 1, "graph_revision": graph["graph_version"], "graph_hash": graph_report["sha256"],
                     "requirement_refs": [], "decision_refs": []},
        "work": {"capture_ref": "capture:test", "records": [], "run_refs": [], "claim_refs": [], "pending_refs": []},
        "conditions": {"selected": [], "unknown": []},
        "manifest": {"components": [{"name": "git", "captured_at": utc_now(), "authority": "local", "version": "test", "complete": True}],
                     "coherence": "coherent", "reasons": []}, "complete": True}
    req = _request("read_current_facts", {}, scope_id)
    with db.write() as conn:
        return ContinuityStore(db).put(conn, req, "basis", body)


def _non_git_basis(db, scope_id, repository_id, workspace, graph, graph_report, path, run_id):
    from pmt.continuity.contracts import digest
    workspace_ref = f"pmt://{repository_id}/{hashlib.sha256(b'non-git').hexdigest()}"
    content_hash = hashlib.sha256((workspace / Path(path)).read_bytes()).hexdigest()
    rows = [{"relative_path": path, "path_ref": fingerprint({"workspace_ref": workspace_ref,
            "relative_path": path}), "content_hash": content_hash, "status": "verified", "reason_code": None}]
    inventory_hash = digest(rows)
    req = _request("capture_work_basis", {"run_id": run_id, "paths": [path]}, scope_id)
    detail = {"schema": "pmt-client-inventory-v1", "scope_id": scope_id,
        "workspace_ref": workspace_ref, "inventory_hash": inventory_hash,
        "coverage": {"selected_count": 1, "verified_count": 1, "unknown_count": 0,
                     "complete": True, "reason_codes": []}, "items": rows}
    with db.write() as conn:
        saved_detail = ContinuityStore(db).put(conn, req, "detail", detail, visibility="private")
        body = basis_body(scope={"project_id": scope_id, "repository_id": repository_id},
            source={"repository_id": repository_id, "branch": None, "workspace_ref": workspace_ref,
                "observed_head": None, "analyzed_ref": None, "applied_ref": None,
                "dirty_state": "unknown", "dirty_fingerprint": None,
                "inventory_ref": saved_detail["id"], "inventory_hash": inventory_hash,
                "inventory_coverage": {"selected_count": 1, "verified_count": 1, "unknown_count": 0,
                    "complete": True, "reason_codes": []}},
            contract={"graph_schema": 1, "graph_revision": graph["graph_version"],
                "graph_hash": graph_report["sha256"], "requirement_refs": [], "decision_refs": []},
            work={"capture_ref": fingerprint({"inventory_hash": inventory_hash}), "task_id": None,
                "records": [], "run_refs": [], "claim_refs": [], "pending_refs": []},
            conditions={"environment_id": db.environment_id, "selected": [], "unknown": []},
            manifest={"components": [{"name": "source", "complete": False,
                    "authority": "client_local_readback", "version": "non_git"}],
                "coherence": "incomplete", "reasons": ["non_git_source"]})
        return ContinuityStore(db).put(conn, req, "basis", body)


def test_collect_changes_reads_actual_git_and_request_replay_keeps_observed_pointer(cli, request_factory,
                                                                                    create_project, tmp_path):
    scope_id = create_project("P4 R2 source")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    workspace, graph, head, report = _workspace(tmp_path, scope_id, repository_id, decision_id, cli.config_root)
    run_id = _claim(db, scope_id, workspace, resource=".")
    basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    target = workspace / "src" / "feature.py"
    before = hashlib.sha256(target.read_bytes()).hexdigest()
    target.write_text("def run():\n    return 2\n", encoding="utf-8")
    after_basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    req = _request("collect_changes", {"run_id": run_id, "paths": ["src"],
        "before_basis_ref": basis["id"], "after_basis_ref": after_basis["id"],
        "expected_pointer_revision": 0}, scope_id)
    response, code = execute(db, req)
    assert code == 0 and response["ok"], response.get("error") or response
    result = response["result"]
    assert result["state"] == "complete" and result["coverage"] == "complete"
    assert result["observed_head"] == head and result["pointer"]["revision"] == 1
    with closing(db.connect()) as conn:
        change = ContinuityStore(db).get(conn, req, result["change_ref"], kind="change")
        pointer = ContinuityStore(db).read_pointer(conn, req, {"repository_id": repository_id,
            "branch": "main", "workspace_ref": basis["body"]["source"]["workspace_ref"],
            "task_id": None, "purpose": "observed_change", "environment_id": db.environment_id})
    assert pointer["object_id"] == result["change_ref"] and pointer["revision"] == 1
    assert change["body"]["after_basis_ref"] == after_basis["id"]
    assert change["body"]["after_basis_hash"] == after_basis["body_hash"]
    assert change["body"]["facts"][0]["content_hash"] != before
    assert "path" not in change["body"]["facts"][0] and "diff" not in change["body"]
    detail_req = _request("read_change_slice", {"change_ref": result["change_ref"],
        "include_detail": True, "run_id": run_id, "paths": ["src"]}, scope_id)
    detail, detail_code = execute(db, detail_req)
    assert detail_code == 0 and detail["ok"], detail.get("error")
    detail_value = detail["result"]["detail"]
    assert detail_value["available"] is True and detail_value["diff_base64"]
    assert "src/feature.py" in [item["path"] for item in detail_value["files"]]
    foreign = dict(detail_req, session_id="foreign-session")
    denied, denied_code = execute(db, foreign)
    assert denied_code == 3 and denied["ok"] is False
    replay, replay_code = execute(db, req)
    assert replay_code == 0 and replay == response
    with closing(db.connect()) as conn:
        current = ContinuityStore(db).read_pointer(conn, req, pointer["selector"])
    assert current["revision"] == 1


def test_change_capture_detects_mid_read_mutation_and_does_not_advance_pointer(cli, request_factory,
                                                                                create_project, tmp_path,
                                                                                monkeypatch):
    scope_id = create_project("P4 R2 incomplete")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    workspace, graph, head, report = _workspace(tmp_path, scope_id, repository_id, decision_id, cli.config_root)
    run_id = _claim(db, scope_id, workspace, resource=".")
    basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    target = workspace / "src" / "feature.py"
    target.write_text("def run():\n    return 2\n", encoding="utf-8")
    after_basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    original = changes._capture_git
    calls = {"count": 0}
    def mutate_after_capture(*args, **kwargs):
        value = original(*args, **kwargs)
        calls["count"] += 1
        if calls["count"] == 1:
            target.write_text("def run():\n    return 3\n", encoding="utf-8")
        return value
    monkeypatch.setattr(changes, "_capture_git", mutate_after_capture)
    req = _request("collect_changes", {"run_id": run_id, "paths": ["src"],
        "before_basis_ref": basis["id"], "after_basis_ref": after_basis["id"],
        "expected_pointer_revision": 0}, scope_id)
    response, code = execute(db, req)
    assert code == 0 and response["ok"], response.get("error") or response
    assert response["result"]["state"] == "incomplete"
    assert response["result"]["reason_codes"] == ["source_changed_during_capture"]
    assert response["result"]["pointer"]["revision"] == 0
    with closing(db.connect()) as conn:
        journal = ContinuityStore(db).get_effect(conn, req, response["result"]["effect_ref"])
        pointer = ContinuityStore(db).read_pointer(conn, req, {"repository_id": repository_id,
            "branch": "main", "workspace_ref": basis["body"]["source"]["workspace_ref"],
            "task_id": None, "purpose": "observed_change", "environment_id": db.environment_id})
    assert journal["state"] == "partial" and pointer["revision"] == 0
    retry, retry_code = execute(db, req)
    assert retry_code == 0 and retry == response
    with closing(db.connect()) as conn:
        pointer_after_retry = ContinuityStore(db).read_pointer(conn, req, pointer["selector"])
    assert pointer_after_retry["revision"] == 0 and pointer_after_retry["object_id"] is None


def test_collect_changes_rejects_branch_switch_and_preserves_observed_pointer(cli, request_factory,
                                                                              create_project, tmp_path):
    scope_id = create_project("P4 R2 branch mapping")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    workspace, graph, head, report = _workspace(tmp_path, scope_id, repository_id, decision_id, cli.config_root)
    run_id = _claim(db, scope_id, workspace, resource=".")
    basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    after_basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    # A divergent branch identity is enough to invalidate this branch-bound map;
    # collection must stop before deciding that the source is unchanged.
    _git(workspace, "checkout", "-b", "diverged-client-branch")
    req = _request("collect_changes", {"run_id": run_id, "paths": ["src"],
        "before_basis_ref": basis["id"], "after_basis_ref": after_basis["id"],
        "expected_pointer_revision": 0}, scope_id)
    response, code = execute(db, req)
    assert code == 3 and response["ok"] is False
    assert response["error"]["code"] in {"source_mapping_unknown", "storage_mapping_missing"}
    with closing(db.connect()) as conn:
        pointer = ContinuityStore(db).read_pointer(conn, req, {"repository_id": repository_id,
            "branch": "main", "workspace_ref": basis["body"]["source"]["workspace_ref"],
            "task_id": None, "purpose": "observed_change", "environment_id": db.environment_id})
    assert pointer["revision"] == 0 and pointer["object_id"] is None


def test_collect_changes_marks_same_branch_unrelated_root_as_history_diverged(cli, request_factory,
                                                                              create_project, tmp_path):
    scope_id = create_project("P4 R2 diverged history")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    workspace, graph, old_head, report = _workspace(tmp_path, scope_id, repository_id,
        decision_id, cli.config_root)
    run_id = _claim(db, scope_id, workspace, resource=".")
    before = _basis(db, scope_id, repository_id, workspace, graph, old_head, report)
    _git(workspace, "checkout", "--orphan", "unrelated-root")
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "independent root on mapped branch")
    _git(workspace, "branch", "-M", "main")
    new_head = _git(workspace, "rev-parse", "HEAD")
    after = _basis(db, scope_id, repository_id, workspace, graph, new_head, report)
    target = workspace / "src" / "feature.py"
    original = target.read_bytes()
    req = _request("collect_changes", {"run_id": run_id, "paths": ["src"],
        "before_basis_ref": before["id"], "after_basis_ref": after["id"],
        "expected_pointer_revision": 0}, scope_id)
    response, code = execute(db, req)
    assert code == 0 and response["ok"], response.get("error")
    assert response["result"]["state"] == "incomplete"
    assert "history_diverged" in response["result"]["reason_codes"]
    assert target.read_bytes() == original
    with closing(db.connect()) as conn:
        pointer = ContinuityStore(db).read_pointer(conn, req, {"repository_id": repository_id,
            "branch": "main", "workspace_ref": before["body"]["source"]["workspace_ref"],
            "task_id": None, "purpose": "observed_change", "environment_id": db.environment_id})
    assert pointer["revision"] == 0 and pointer["object_id"] is None


def test_collect_changes_denies_non_owner_before_source_read_or_pointer_write(cli, request_factory,
                                                                             create_project, tmp_path):
    scope_id = create_project("P4 R2 owner denial")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    workspace, graph, head, report = _workspace(tmp_path, scope_id, repository_id, decision_id, cli.config_root)
    run_id = _claim(db, scope_id, workspace, resource=".")
    basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    target = workspace / "src" / "feature.py"
    target.write_text("def run():\n    return 8\n", encoding="utf-8")
    after_basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    req = _request("collect_changes", {"run_id": run_id, "paths": ["src"],
        "before_basis_ref": basis["id"], "after_basis_ref": after_basis["id"],
        "expected_pointer_revision": 0}, scope_id)
    req["session_id"] = "foreign-session"
    response, code = execute(db, req)
    assert code == 3 and response["ok"] is False
    assert response["error"]["code"] in {"ownership_conflict", "workspace_authority_stale"}
    with closing(db.connect()) as conn:
        pointer = ContinuityStore(db).read_pointer(conn, _request("read_current_facts", {}, scope_id),
            {"repository_id": repository_id, "branch": "main",
             "workspace_ref": basis["body"]["source"]["workspace_ref"], "task_id": None,
             "purpose": "observed_change", "environment_id": db.environment_id})
    assert pointer["revision"] == 0 and pointer["object_id"] is None


def test_non_git_change_capture_remains_unknown_and_never_advances_observed_pointer(cli, request_factory,
                                                                                    create_project, tmp_path, request):
    scope_id = create_project("P4 R2 non git")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    temp_parent = Path(tempfile.gettempdir()).resolve()
    external_root = Path(tempfile.mkdtemp(prefix="pmt-non-git-")).resolve()
    external_root.relative_to(temp_parent)
    request.addfinalizer(lambda: shutil.rmtree(external_root, ignore_errors=True))
    workspace, graph, head, report = _workspace(external_root, scope_id, repository_id,
        decision_id, cli.config_root)
    git_dir = (workspace / ".git").resolve(strict=True)
    git_dir.relative_to(external_root)
    def remove_readonly(function, path, _error):
        os.chmod(path, stat.S_IWRITE)
        function(path)
    shutil.rmtree(git_dir, onexc=remove_readonly)
    _profile, config_hash = _read_profile(cli.config_root)
    non_git_hash = hashlib.sha256(b"non-git").hexdigest()
    configure_storage(cli.config_root, {"mode": "local", "expected_config_sha256": config_hash,
        "workspace_mappings": [{"repository_id": repository_id, "project_id": scope_id,
            "branch": None, "branch_key_sha256": non_git_hash, "local_root": str(workspace.resolve()),
            "relative_graph_path": "docs/pmt-docs/plan.graph.json"}]})
    run_id = _claim(db, scope_id, workspace, resource=".")
    path = "src/feature.py"
    before = _non_git_basis(db, scope_id, repository_id, workspace, graph, report, path, run_id)
    target = workspace / Path(path)
    target.write_text("def run():\n    return 9\n", encoding="utf-8")
    after = _non_git_basis(db, scope_id, repository_id, workspace, graph, report, path, run_id)
    req = _request("collect_changes", {"run_id": run_id, "paths": ["src"],
        "before_basis_ref": before["id"], "after_basis_ref": after["id"],
        "expected_pointer_revision": 0}, scope_id)
    response, code = execute(db, req)
    assert code == 0 and response["ok"], response.get("error")
    assert response["result"]["state"] == "incomplete"
    assert response["result"]["coverage"] == "unknown"
    assert "non_git_source" in response["result"]["reason_codes"]
    with closing(db.connect()) as conn:
        pointer = ContinuityStore(db).read_pointer(conn, req, {"repository_id": repository_id,
            "branch": None, "workspace_ref": before["body"]["source"]["workspace_ref"],
            "task_id": None, "purpose": "observed_change", "environment_id": db.environment_id})
    assert pointer["revision"] == 0 and pointer["object_id"] is None


def test_nested_git_workspace_normalizes_change_and_link_paths_to_mapping_root(cli, request_factory,
                                                                              create_project, tmp_path):
    scope_id = create_project("P4 R2 nested checkout")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    git_root = tmp_path / "monorepo"
    git_root.mkdir()
    _git(git_root, "init", "-b", "main")
    _git(git_root, "config", "user.email", "pmt-test@example.invalid")
    _git(git_root, "config", "user.name", "PMT nested fixture")
    workspace = git_root / "packages" / "component"
    graph_path = workspace / "docs" / "pmt-docs" / "plan.graph.json"
    graph_path.parent.mkdir(parents=True)
    graph = _graph(scope_id)
    method = next(node for node in graph["nodes"] if node["tree_kind"] == "implementation")
    method.update({"stop_reason": "user_delegated",
        "delegated_scope": "nested workspace method fixture",
        "autonomy": {"authority": "user", "scope": "nested workspace method fixture"},
        "source_refs": [decision_id]})
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    code_path = workspace / "src" / "feature.py"
    code_path.parent.mkdir(parents=True)
    code_path.write_text("def run():\n    return 1\n", encoding="utf-8")
    _git(git_root, "add", "-A")
    _git(git_root, "commit", "-m", "initial nested project")
    head = _git(workspace, "rev-parse", "HEAD")
    report = validate_graph(graph, scope_id, complete=False)
    branch_hash = hashlib.sha256(b"main").hexdigest()
    configure_storage(cli.config_root, {"mode": "local", "expected_config_sha256": None,
        "workspace_mappings": [{"repository_id": repository_id, "project_id": scope_id,
            "branch": "main", "branch_key_sha256": branch_hash,
            "local_root": str(workspace.resolve()),
            "relative_graph_path": "docs/pmt-docs/plan.graph.json"}]})
    run_id = _claim(db, scope_id, workspace, resource=".")
    inventory_paths = ["src/feature.py", "docs/pmt-docs/plan.graph.json"]
    before = _basis(db, scope_id, repository_id, workspace, graph, head, report,
                    inventory_paths=inventory_paths)
    old_hash = hashlib.sha256(code_path.read_bytes()).hexdigest()
    code_path.write_text("def run():\n    return 2\n", encoding="utf-8")
    after = _basis(db, scope_id, repository_id, workspace, graph, head, report,
                   inventory_paths=inventory_paths)
    collect_request = _request("collect_changes", {"run_id": run_id, "paths": ["src"],
        "before_basis_ref": before["id"], "after_basis_ref": after["id"],
        "expected_pointer_revision": 0}, scope_id)
    collected, code = execute(db, collect_request)
    assert code == 0 and collected["ok"], collected.get("error")
    with closing(db.connect()) as conn:
        change = ContinuityStore(db).get(conn, collect_request, collected["result"]["change_ref"], kind="change")
    expected_path_ref = hashlib.sha256(b"src/feature.py").hexdigest()
    assert change["body"]["coverage"] == "complete"
    assert len(change["body"]["facts"]) == 1
    assert change["body"]["facts"][0]["path_ref"] == expected_path_ref
    assert change["body"]["facts"][0]["content_hash"] != old_hash

    node_id = method["id"]
    link_request = _request("build_implementation_links", {"run_id": run_id, "paths": ["src"],
        "basis_ref": after["id"], "expected_pointer_revision": 0,
        "mappings": [{"path": "src/feature.py", "node_id": node_id, "decision_ref": decision_id}]}, scope_id)
    linked, link_code = execute(db, link_request)
    assert link_code == 0 and linked["ok"], linked.get("error")
    with closing(db.connect()) as conn:
        index = ContinuityStore(db).get(conn, link_request, linked["result"]["index_ref"], kind="link_index")
    assert index["body"]["coverage"]["complete"] is True
    assert index["body"]["entries"][0]["path_ref"] == expected_path_ref
    assert index["body"]["links"][0]["path_ref"] == expected_path_ref


def test_link_index_uses_actual_current_decision_and_keeps_unreviewed_mapping_as_candidate(cli,
                                                                                             request_factory,
                                                                                             create_project,
                                                                                             tmp_path):
    scope_id = create_project("P4 R2 index")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    workspace, graph, head, report = _workspace(tmp_path, scope_id, repository_id, decision_id, cli.config_root)
    run_id = _claim(db, scope_id, workspace, resource=".")
    basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    payload = {"run_id": run_id, "paths": ["src"], "basis_ref": basis["id"],
        "expected_pointer_revision": 0,
        "mappings": [{"path": "src/feature.py", "node_id": next(n["id"] for n in graph["nodes"] if n["tree_kind"] == "implementation"),
                           "decision_ref": decision_id, "reviewed_by": "caller supplied"}]}
    response, code = execute(db, _request("build_implementation_links", payload, scope_id))
    assert code == 0 and response["ok"], response.get("error") or response
    assert response["result"]["coverage"]["complete"] is True
    with closing(db.connect()) as conn:
        index = ContinuityStore(db).get(conn, _request("read_change_slice", {}, scope_id),
                                         response["result"]["index_ref"], kind="link_index")
    links = index["body"]["links"]
    assert any(item["link_state"] == "verified_mapping" and item["decision_ref"] == decision_id for item in links), links


def test_caller_review_flag_without_live_decision_ref_stays_candidate(cli, request_factory,
                                                                      create_project, tmp_path):
    scope_id = create_project("P4 R2 fake approval")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    workspace, graph, head, report = _workspace(tmp_path, scope_id, repository_id, decision_id, cli.config_root)
    run_id = _claim(db, scope_id, workspace, resource=".")
    basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    payload = {"run_id": run_id, "paths": ["src"], "basis_ref": basis["id"],
        "expected_pointer_revision": 0,
        "mappings": [{"path": "src/feature.py", "node_id": next(n["id"] for n in graph["nodes"] if n["tree_kind"] == "implementation"),
                       "decision_ref": new_id(), "reviewed_by": "user", "verified_mapping_ref": decision_id}]}
    response, code = execute(db, _request("build_implementation_links", payload, scope_id))
    assert code == 0 and response["ok"], response
    with closing(db.connect()) as conn:
        saved = conn.execute("SELECT body_json FROM continuity_objects WHERE id=?", (response["result"]["index_ref"],)).fetchone()
    stored = json.loads(saved[0])
    assert all(item["link_state"] != "verified_mapping" for item in stored["links"])


def test_reviewed_method_alignment_applies_actual_graph_and_document_effects(cli, request_factory,
                                                                           create_project, tmp_path):
    scope_id = create_project("P4 R3 apply")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    graph_relative_path = "spec/current-project-graph.json"
    workspace, graph, head, report = _workspace(tmp_path, scope_id, repository_id, decision_id,
        cli.config_root, relative_graph_path=graph_relative_path)
    run_id = _claim(db, scope_id, workspace, resource=".")
    inventory_paths = ["src/feature.py", graph_relative_path]
    basis = _basis(db, scope_id, repository_id, workspace, graph, head, report,
                   inventory_paths=inventory_paths)
    changed_file = workspace / "src" / "feature.py"
    changed_file.write_text("def run():\n    return 2\n", encoding="utf-8")
    after_basis = _basis(db, scope_id, repository_id, workspace, graph, head, report,
                         inventory_paths=inventory_paths)
    collect_req = _request("collect_changes", {"run_id": run_id, "paths": ["src"],
        "before_basis_ref": basis["id"], "after_basis_ref": after_basis["id"],
        "expected_pointer_revision": 0}, scope_id)
    collected, code = execute(db, collect_req)
    assert code == 0 and collected["ok"], collected.get("error")
    method = next(item for item in graph["nodes"] if item["tree_kind"] == "implementation")
    link_req = _request("build_implementation_links", {"run_id": run_id, "paths": ["src"],
        "basis_ref": after_basis["id"], "expected_pointer_revision": 0,
        "mappings": [{"path": "src/feature.py", "node_id": method["id"], "decision_ref": decision_id}]}, scope_id)
    linked, code = execute(db, link_req)
    assert code == 0 and linked["ok"], linked.get("error")

    common = {"repository_id": repository_id, "workspace": str(workspace.resolve()),
              "relative_graph_path": graph_relative_path, "run_id": run_id}
    pin_response, code = execute(db, _request("capture_source_pin", common, scope_id))
    assert code == 0 and pin_response["ok"], pin_response.get("error")
    pin = pin_response["result"]["source_pin"]
    baseline_prepare, code = execute(db, _request("prepare_document_segments",
        common | {"expected_source": pin}, scope_id))
    assert code == 0 and baseline_prepare["ok"], baseline_prepare.get("error")
    baseline_publish, code = execute(db, _request("publish_document_segments",
        common | {"journal_id": baseline_prepare["result"]["journal_id"]}, scope_id))
    assert code == 0 and baseline_publish["ok"], baseline_publish.get("error")
    index, code = execute(db, _request("rebuild_graph_index", common | {"expected_source": pin}, scope_id))
    assert code == 0 and index["ok"], index.get("error")

    change_set = {"change_id": new_id(), "reason": "Adjust an implementation method within recorded delegation",
        "changes": [{"op": "update", "id": method["id"],
                     "fields": {"architecture": "Use the stable source-bound implementation path."}}]}
    preview, code = execute(db, _request("preview_graph_change", common | {
        "expected_source": pin, "change_set": change_set}, scope_id))
    assert code == 0 and preview["ok"], preview.get("error")
    impact, code = execute(db, _request("calculate_graph_impact", common | {
        "expected_source": pin, "change_preview": preview["result"], "change_set": change_set}, scope_id))
    assert code == 0 and impact["ok"], impact.get("error")
    assert impact["result"]["complete"] is True, impact["result"].get("unknown")

    assess_req = _request("assess_alignment", {"run_id": run_id,
        "paths": ["src", graph_relative_path], "basis_ref": after_basis["id"],
        "change_ref": collected["result"]["change_ref"], "index_ref": linked["result"]["index_ref"],
        "typed_graph_preview": preview["result"], "change_set": change_set}, scope_id)
    assessed, code = execute(db, assess_req)
    assert code == 0 and assessed["ok"], assessed.get("error")
    assert assessed["result"]["required_action"] == "native_main_review"
    with closing(db.connect()) as conn:
        assessment = ContinuityStore(db).get(conn, assess_req, assessed["result"]["assessment_ref"], kind="assessment")
        assert assessment["body"]["typed_graph_preview_used"] is True
    premise_request = _request("propose_semantic_resolution", {
        "assessment_ref": assessed["result"]["assessment_ref"], "resolution_kind": "premise",
        "approved": True}, scope_id)
    premise, code = execute(db, premise_request)
    assert code == 0 and premise["ok"]
    assert premise["result"]["state"] == "awaiting_user"
    resolution, code = execute(db, _request("propose_semantic_resolution", {
        "assessment_ref": assessed["result"]["assessment_ref"], "resolution_kind": "method"}, scope_id))
    assert code == 0 and resolution["ok"], resolution.get("error")
    assert resolution["result"]["state"] == "delegated_method_candidate"

    graph_applied, code = execute(db, _request("apply_graph_change", common | {
        "expected_source": pin, "change_set": change_set}, scope_id))
    assert code == 0 and graph_applied["ok"], graph_applied.get("error")
    new_pin = graph_applied["result"]["source_pin"]
    document_prepare, code = execute(db, _request("prepare_document_segments", common | {
        "expected_source": new_pin, "impact_set": impact["result"],
        "apply_receipt": graph_applied["result"], "change_preview": preview["result"],
        "change_set": change_set}, scope_id))
    assert code == 0 and document_prepare["ok"], document_prepare.get("error")
    document_publish, code = execute(db, _request("publish_document_segments", common | {
        "journal_id": document_prepare["result"]["journal_id"]}, scope_id))
    assert code == 0 and document_publish["ok"], document_publish.get("error")

    second_run_id = _claim(db, scope_id, workspace, session="other-active", resource=".")
    apply_payload = {"run_id": run_id,
        "paths": ["src", graph_relative_path, "docs/pmt-docs/plan.md"],
        "assessment_ref": assessed["result"]["assessment_ref"],
        "resolution_ref": resolution["result"]["resolution_ref"],
        "graph_effect_ref": graph_applied["result"]["journal_id"],
        "document_effect_ref": document_prepare["result"]["journal_id"],
        "expected_pointer_revision": 0}
    blocked_req = _request("apply_alignment", apply_payload, scope_id)
    blocked, blocked_code = execute(db, blocked_req)
    assert blocked_code == 0 and blocked["ok"]
    assert blocked["result"]["state"] == "reconciliation_required"
    with db.write() as conn:
        conn.execute("UPDATE execution_runs SET state='failed',stop_confirmed=1,completed_at=?,revision=revision+1 WHERE id=?",
                     (utc_now(), second_run_id))
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (second_run_id,))
    apply_req = _request("apply_alignment", apply_payload, scope_id)
    applied, code = execute(db, apply_req)
    assert code == 0 and applied["ok"], applied.get("error")
    assert applied["result"]["state"] == "applied", applied["result"]
    assert applied["result"]["applied_pointer_advanced"] is True
    assert hashlib.sha256((workspace / graph_relative_path).read_bytes()).hexdigest() == applied["result"]["graph_hash"]
    assert hashlib.sha256((workspace / "docs/pmt-docs/plan.md").read_bytes()).hexdigest() == applied["result"]["document_hash"]
    with closing(db.connect()) as conn:
        receipt = ContinuityStore(db).get(conn, _request("read_checkpoint", {}, scope_id),
                                           applied["result"]["alignment_ref"], kind="alignment")
        pointer = ContinuityStore(db).read_pointer(conn, _request("read_checkpoint", {}, scope_id),
                                                   applied["result"]["pointer"]["selector"])
    assert receipt["body"]["graph_effect_ref"] == graph_applied["result"]["journal_id"]
    assert pointer["revision"] == 1 and pointer["object_id"] == applied["result"]["alignment_ref"]
    replay, replay_code = execute(db, apply_req)
    assert replay_code == 0 and replay == applied


def test_committed_rename_and_delete_are_observed_without_modifying_files(cli, request_factory,
                                                                          create_project, tmp_path):
    scope_id = create_project("P4 R2 rename delete")
    db, repository_id, decision_id = _setup_scope(cli, request_factory, scope_id)
    workspace, graph, head, report = _workspace(tmp_path, scope_id, repository_id, decision_id, cli.config_root)
    gone = workspace / "src" / "gone.py"
    gone.write_text("def obsolete():\n    return False\n", encoding="utf-8")
    _git(workspace, "add", "src/gone.py")
    _git(workspace, "commit", "-m", "add an independent tracked file")
    head = _git(workspace, "rev-parse", "HEAD")
    basis = _basis(db, scope_id, repository_id, workspace, graph, head, report)
    run_id = _claim(db, scope_id, workspace, resource=".")
    old = workspace / "src" / "feature.py"
    new = workspace / "src" / "feature-renamed.py"
    old.rename(new)
    gone.unlink()
    _git(workspace, "add", "-A")
    _git(workspace, "commit", "-m", "move one implementation and remove another")
    after_head = _git(workspace, "rev-parse", "HEAD")
    after_basis = _basis(db, scope_id, repository_id, workspace, graph, after_head, report,
                         inventory_paths=["src/feature-renamed.py", "src/gone.py", "docs/pmt-docs/plan.graph.json"])
    before_bytes = new.read_bytes()
    req = _request("collect_changes", {"run_id": run_id, "paths": ["src"],
        "before_basis_ref": basis["id"], "after_basis_ref": after_basis["id"],
        "expected_pointer_revision": 0}, scope_id)
    response, code = execute(db, req)
    assert code == 0 and response["ok"], response.get("error")
    assert response["result"]["observed_head"] == after_head
    with closing(db.connect()) as conn:
        change = ContinuityStore(db).get(conn, req, response["result"]["change_ref"], kind="change")
    facts = change["body"]["facts"]
    kinds = {item["change_kind"] for item in facts}
    assert "renamed" in kinds and "deleted" in kinds
    assert any(item["before_path_ref"] for item in facts if item["change_kind"] == "renamed")
    assert new.read_bytes() == before_bytes and not old.exists() and not gone.exists()


def test_applicability_uses_current_f6_selectors_and_actual_p2_evidence(tmp_path):
    from test_phase3_reuse import (_actual_definition, _actual_fixture, _actual_request,
                                   _local_execute, _verified_p2_result)
    from pmt.storage_config import configure_storage
    env = _actual_fixture(tmp_path, session="applicability-session")
    db, scope_id, workspace = env["db"], env["scope"], env["root"]
    repository_id = new_id()
    now = utc_now()
    _git(workspace, "init", "-b", "main")
    _git(workspace, "config", "user.email", "pmt-test@example.invalid")
    _git(workspace, "config", "user.name", "PMT test")
    graph = _graph(scope_id, step_id=env["step"])
    docs = workspace / "docs" / "pmt-docs"
    docs.mkdir(parents=True)
    (docs / "plan.graph.json").write_text(json.dumps(graph, ensure_ascii=False), encoding="utf-8")
    (docs / "plan.md").write_text("# Plan\n", encoding="utf-8")
    _git(workspace, "add", ".")
    _git(workspace, "commit", "-m", "initialize current source")
    head = _git(workspace, "rev-parse", "HEAD")
    graph_report = validate_graph(graph, scope_id, complete=False)
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,'repository',NULL,'applicability repository','{}',?,?)",
                     (repository_id, now, now))
        conn.execute("UPDATE scopes SET parent_id=? WHERE id=?", (repository_id, scope_id))
        conn.execute("UPDATE scope_locks SET resource='.' WHERE run_id=?", (env["run"],))
    branch_hash = hashlib.sha256(b"main").hexdigest()
    configure_storage(db.config_root, {"mode": "local", "expected_config_sha256": None,
        "workspace_mappings": [{"repository_id": repository_id, "project_id": scope_id,
            "branch": "main", "branch_key_sha256": branch_hash, "local_root": str(workspace.resolve()),
            "relative_graph_path": "docs/pmt-docs/plan.graph.json"}]})
    basis_body = {"version": 1, "scope": {"project_id": scope_id},
        "source": {"repository_id": repository_id, "branch": "main",
            "workspace_ref": f"pmt://{repository_id}/{branch_hash}", "observed_head": head,
            "analyzed_ref": head, "applied_ref": head, "dirty_state": "clean", "dirty_fingerprint": None,
            "inventory_ref": "local-inventory:applicability", "inventory_hash": fingerprint(["src/target.py"]),
            "inventory_coverage": {"selected_count": 1, "verified_count": 1, "unknown_count": 0,
                "complete": True, "reason_codes": []}},
        "contract": {"graph_schema": 1, "graph_revision": graph["graph_version"],
            "graph_hash": graph_report["sha256"], "requirement_refs": [], "decision_refs": []},
        "work": {"capture_ref": "capture:applicability", "records": [], "run_refs": [],
            "claim_refs": [], "pending_refs": []}, "conditions": {"selected": [], "unknown": []},
        "manifest": {"components": [{"name": "source", "captured_at": utc_now(),
            "authority": "local", "version": "test", "complete": True}],
            "coherence": "coherent", "reasons": []}, "complete": True}
    basis_req = {"protocol_version": 1, "operation": "read_current_facts", "request_id": new_id(),
        "actor": "reuse-test", "session_id": env["session"], "scope_id": scope_id, "payload": {}}
    with db.write() as conn:
        basis = ContinuityStore(db).put(conn, basis_req, "basis", basis_body)

    definition = _actual_definition()
    claim_req = _actual_request(env, "resolve_reuse", definition=definition)
    claimed, code = _local_execute(db, claim_req)
    assert code == 0 and claimed["ok"] and claimed["result"]["status"] == "claimed"
    verification_id = _verified_p2_result(env)
    with db.write() as conn:
        conn.execute("UPDATE execution_runs SET state='review_pending',stop_confirmed=1 WHERE id=?", (env["run"],))
    recorded, code = _local_execute(db, _actual_request(env, "record_reuse_result",
        verification_id=verification_id, definition=definition))
    assert code == 0 and recorded["ok"] and recorded["result"]["status"] == "recorded"

    request = _actual_request(env, "read_applicability", verification_id=verification_id, definition=definition)
    request["payload"].update({"basis_ref": basis["id"], "paths": ["."], "event_id": new_id()})
    observed, code = _local_execute(db, request)
    assert code == 0 and observed["ok"], observed.get("error") or observed
    assert observed["result"]["status"] == "applicable", observed["result"]
    assert observed["result"]["verification_ref"] == verification_id
    with closing(db.connect()) as conn:
        saved = ContinuityStore(db).get(conn, request, observed["result"]["applicability_ref"], kind="applicability")
        p2 = conn.execute("SELECT outcome,state FROM verifications WHERE id=?", (verification_id,)).fetchone()
    assert tuple(p2) == ("pass", "valid")
    assert saved["body"]["status"] == "applicable"
    assert saved["body"]["verification_outcome_preserved"] == "pass"
