"""Isolated acceptance checks for deterministic document segment publishing."""
from __future__ import annotations

from contextlib import closing
import json
import re
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

from pmt.db import Database
from pmt import service as pmt_service
from pmt.planning.graph import render_docs
from pmt.service import execute as execute_service
from pmt.util import canonical_json, new_id, utc_now


def _node(node_id, kind):
    node = {"id": node_id, "tree_kind": kind, "node_kind": "goal", "summary": "Baseline node",
            "premise": "Keep the source meaning", "product_stage": "prototype",
            "product_scope": {"applies": False, "reason": "Scope", "criteria": []},
            "autonomy": {"authority": "user", "scope": "Preserve intent"}}
    if kind == "requirement":
        node.update(source_refs=["request:source"], criteria=["source is pinned"], evidence_refs=["evidence:one"],
                    stop_reason="implementation_boundary")
    else:
        node.update(framework_assignment="Python", architecture="Graph to index", logging="IDs and hashes",
                    tests=["isolated"], function_spec={"input": "Graph", "output": "Index", "constraints": "No secrets",
                    "invariants": "Stable IDs", "errors": "Conflict", "verification": "Git hash"},
                    choice_set={"options": [], "insufficient_reason": "Existing fixture"},
                    choice={"source": "user", "selected": "SQLite", "reason": "Current", "scope": "Local"})
        node["stop_reason"] = "file_edit_boundary"
    return node


@pytest.fixture
def document_env(tmp_path):
    root = Path(tempfile.mkdtemp(prefix="p3doc-", dir=tmp_path.parents[1]))
    workspace = root / "repo"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "PMT test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "pmt-test@example.invalid"], check=True)
    project, repository, requirement, implementation = (new_id() for _ in range(4))
    relation = new_id()
    graph = {"schema_version": 1, "project_id": project, "graph_version": 1,
             "nodes": [_node(requirement, "requirement"), _node(implementation, "implementation")],
             "relations": [{"id": relation, "kind": "implements", "from": requirement, "to": implementation}],
             "provenance": {"request_ref": "request:test"}}
    graph_path = workspace / "docs/pmt-docs/plan.graph.json"
    graph_path.parent.mkdir(parents=True)
    graph_path.write_text(canonical_json(graph) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "docs/pmt-docs/plan.graph.json"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "graph"], check=True)
    db = Database(root / "data", root / "config")
    run_id, job_id, step_id = new_id(), new_id(), new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES(?,?,?,?,?)",
                     (repository, "repository", "repo", now, now))
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (project, "project", repository, "project", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (step_id, "step", project, "Document test", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (job_id, step_id, "running", "{}", now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (run_id, job_id, step_id, 1, "running", 1, "doc-session", 1, str(workspace), "[]", "{}", "{}", now, now))
        for relative in ("docs/pmt-docs/plan.graph.json", "docs/pmt-docs/plan.md"):
            conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                         (new_id(), run_id, "doc-session", "path", str(workspace), relative, now))
    env = {"db": db, "root": root, "workspace": workspace, "scope": project, "repository": repository,
           "run": run_id, "graph": graph, "graph_path": "docs/pmt-docs/plan.graph.json"}
    capture, code = _invoke(env, "capture_source_pin")
    assert code == 0
    env["pin"] = capture["result"]["source_pin"]
    return env


def _request(env, operation, **payload):
    return {"protocol_version": 1, "operation": operation, "request_id": new_id(),
            "actor": "document-test", "session_id": "doc-session", "scope_id": env["scope"],
            "payload": {"repository_id": env["repository"], "workspace": str(env["workspace"]),
                        "relative_graph_path": env["graph_path"], "run_id": env["run"], **payload}}


def _invoke(env, operation, **payload):
    return execute_service(env["db"], _request(env, operation, **payload))


def test_baseline_generation_matches_canonical_render_and_registers_complete_coverage(document_env):
    env = document_env
    expected, _, _ = render_docs(env["graph"])
    prepared, code = _invoke(env, "prepare_document_segments", expected_source=env["pin"])
    assert code == 0 and prepared["ok"]
    published, code = _invoke(env, "publish_document_segments", expected_source=env["pin"],
                              journal_id=prepared["result"]["journal_id"])
    assert code == 0 and published["ok"], published
    text = (env["workspace"] / "docs/pmt-docs/plan.md").read_text(encoding="utf-8")
    rendered = "".join(match.group(1) for match in re.finditer(
        r"<!-- PMT:SEGMENT:[0-9a-f-]{36}:BEGIN -->\n(.*?)<!-- PMT:SEGMENT:[0-9a-f-]{36}:END -->\n?", text, re.S))
    assert rendered == expected
    assert published["result"]["manifest_count"] >= len(env["graph"]["nodes"])
    assert published["result"]["coverage"]["segments"] == "complete"
    with closing(env["db"].connect()) as conn:
        coverage = conn.execute("SELECT source_hash,body_json FROM phase3_objects WHERE kind='segment_coverage' AND id=?",
                                (env["scope"],)).fetchone()
        assert coverage and coverage["source_hash"] == env["pin"]["source_hash"]


def test_manual_prefix_is_preserved_and_target_edit_after_prepare_conflicts(document_env):
    env = document_env
    target = env["workspace"] / "docs/pmt-docs/plan.md"
    target.write_text("# Human note\n\nKeep this paragraph.\n", encoding="utf-8")
    prepared, code = _invoke(env, "prepare_document_segments", expected_source=env["pin"])
    assert code == 0 and prepared["ok"]
    target.write_text("# Human note\n\nUser edit during publication.\n", encoding="utf-8")
    result, code = _invoke(env, "publish_document_segments", expected_source=env["pin"],
                           journal_id=prepared["result"]["journal_id"])
    assert code == 3 and result["error"]["code"] == "document_conflict"
    assert target.read_text(encoding="utf-8") == "# Human note\n\nUser edit during publication.\n"


def test_partial_render_is_bound_to_apply_and_preserves_unaffected_segment_ids(document_env):
    env = document_env
    document_path = env["workspace"] / "docs/pmt-docs/plan.md"
    document_path.write_text("# Human notes\n\nOriginal manual paragraph.\n", encoding="utf-8")
    first, code = _invoke(env, "prepare_document_segments", expected_source=env["pin"])
    assert code == 0
    first, code = _invoke(env, "publish_document_segments", expected_source=env["pin"],
                         journal_id=first["result"]["journal_id"])
    assert code == 0
    old_document = document_path.read_text(encoding="utf-8")
    document_path.write_text(old_document.replace("Original manual paragraph.",
                                                  "User revised the manual paragraph."), encoding="utf-8")
    change = {"change_id": new_id(), "reason": "Update a documented premise",
              "changes": [{"op": "update", "id": env["graph"]["nodes"][0]["id"],
                           "fields": {"premise": "A revised documented premise"}}]}
    preview, code = _invoke(env, "preview_graph_change", expected_source=env["pin"], change_set=change)
    assert code == 0
    impact, code = _invoke(env, "calculate_graph_impact", expected_source=env["pin"],
                           change_preview=preview["result"], change_set=change)
    assert code == 0 and impact["result"]["complete"] is True
    applied, code = _invoke(env, "apply_graph_change", expected_source=env["pin"], change_set=change)
    assert code == 0
    current, code = _invoke(env, "capture_source_pin")
    assert code == 0
    prepared, code = _invoke(env, "prepare_document_segments", expected_source=current["result"]["source_pin"],
        impact_set=impact["result"], apply_receipt=applied["result"], change_set=change,
        change_preview=preview["result"])
    assert code == 0 and prepared["ok"], (code, prepared.get("error"))
    published, code = _invoke(env, "publish_document_segments", expected_source=current["result"]["source_pin"],
                              journal_id=prepared["result"]["journal_id"])
    assert code == 0 and published["ok"], published
    new_document = (env["workspace"] / "docs/pmt-docs/plan.md").read_text(encoding="utf-8")
    assert "User revised the manual paragraph." in new_document
    assert "Original manual paragraph." not in new_document
    old_markers = re.findall(r"<!-- PMT:SEGMENT:([0-9a-f-]{36}):BEGIN -->\n(.*?)<!-- PMT:SEGMENT:\1:END -->", old_document, re.S)
    new_markers = re.findall(r"<!-- PMT:SEGMENT:([0-9a-f-]{36}):BEGIN -->\n(.*?)<!-- PMT:SEGMENT:\1:END -->", new_document, re.S)
    old_map, new_map = dict(old_markers), dict(new_markers)
    changed = {segment_id for segment_id in old_map if old_map[segment_id] != new_map[segment_id]}
    assert changed
    impact_ids = {item["segment_id"] for item in impact["result"]["documents"]}
    preamble_id = str(uuid.uuid5(uuid.UUID(env["scope"]),
        "pmt.segment:pmt-render-docs-v1:docs/pmt-docs/plan.md:preamble"))
    assert changed - impact_ids == {preamble_id}
    assert set(old_map) == set(new_map)
    assert len(new_map) == len(old_map)


def test_detach_interruption_recovers_original_publish_request(document_env, monkeypatch):
    env = document_env
    target = env["workspace"] / "docs/pmt-docs/plan.md"
    target.write_text("# Existing hand-edited preface\n", encoding="utf-8")
    prepared, code = _invoke(env, "prepare_document_segments", expected_source=env["pin"])
    assert code == 0
    original = _request(env, "publish_document_segments", expected_source=env["pin"],
                        journal_id=prepared["result"]["journal_id"])
    import pmt.efficiency.documents as documents
    publisher = documents.guarded_publish
    def interrupt_once(target, stage, expected_hash, effect_id, owned_root, callback=None):
        def checkpoint(phase, details):
            if callback:
                callback(phase, details)
            if phase == "target_detached":
                raise OSError("simulated interruption")
        return publisher(target, stage, expected_hash, effect_id, owned_root, checkpoint)
    monkeypatch.setattr(documents, "guarded_publish", interrupt_once)
    failed, code = pmt_service.execute(env["db"], original)
    assert code == 4 and failed["error"]["code"] == "publication_reconcile_required"
    assert not target.exists()
    monkeypatch.setattr(documents, "guarded_publish", publisher)
    recovered, code = _invoke(env, "recover_document_segments", original_request=original)
    assert code == 0 and recovered["ok"], recovered.get("error")
    durable, durable_code = env["db"].get_request_result(original["request_id"], actor="document-test",
                                                          session_id="doc-session")
    assert durable_code == 0 and durable["ok"]
    assert target.is_file() and recovered["result"]["document_hash"] == __import__("hashlib").sha256(target.read_bytes()).hexdigest()


def test_template_source_and_claim_mismatches_fail_before_target_publication(document_env):
    env = document_env
    wrong_template, code = _invoke(env, "prepare_document_segments", expected_source=env["pin"],
                                  template_version="unregistered-template")
    assert code == 3 and wrong_template["error"]["code"] == "document_template_unsupported"
    forged_pin = dict(env["pin"], graph_hash="0" * 64)
    stale, code = _invoke(env, "prepare_document_segments", expected_source=forged_pin)
    assert code in {2, 3} and stale["error"]
    wrong_owner = _request(env, "prepare_document_segments", expected_source=env["pin"])
    wrong_owner["session_id"] = "other-session"
    denied, code = execute_service(env["db"], wrong_owner)
    assert code == 3 and denied["error"]["code"] in {"workspace_claim_required", "ownership_conflict"}
    assert not (env["workspace"] / "docs/pmt-docs/plan.md").exists()


def test_source_change_after_prepare_keeps_original_target_unpublished(document_env):
    env = document_env
    target = env["workspace"] / "docs/pmt-docs/plan.md"
    prepared, code = _invoke(env, "prepare_document_segments", expected_source=env["pin"])
    assert code == 0
    graph_path = env["workspace"] / env["graph_path"]
    changed = json.loads(graph_path.read_text(encoding="utf-8"))
    changed["nodes"][0]["premise"] = "Changed after document preparation"
    graph_path.write_text(canonical_json(changed) + "\n", encoding="utf-8")
    result, code = _invoke(env, "publish_document_segments", expected_source=env["pin"],
                           journal_id=prepared["result"]["journal_id"])
    assert code == 3 and result["error"]["code"] == "source_conflict"
    assert not target.exists()


def test_graph_change_at_publication_checkpoint_aborts_before_candidate_creation(document_env, monkeypatch):
    env = document_env
    target = env["workspace"] / "docs/pmt-docs/plan.md"
    prepared, code = _invoke(env, "prepare_document_segments", expected_source=env["pin"])
    assert code == 0
    import pmt.efficiency.documents as documents
    publisher = documents.guarded_publish
    changed = {"done": False}
    def edit_source_before_create(target_path, stage, expected_hash, effect_id, owned_root, callback=None):
        def checkpoint(phase, details):
            if phase == "candidate_staged" and not changed["done"]:
                graph_path = env["workspace"] / env["graph_path"]
                value = json.loads(graph_path.read_text(encoding="utf-8"))
                value["nodes"][0]["premise"] = "Changed at the publication boundary"
                graph_path.write_text(canonical_json(value) + "\n", encoding="utf-8")
                changed["done"] = True
            if callback:
                callback(phase, details)
        return publisher(target_path, stage, expected_hash, effect_id, owned_root, checkpoint)
    monkeypatch.setattr(documents, "guarded_publish", edit_source_before_create)
    result, code = _invoke(env, "publish_document_segments", expected_source=env["pin"],
                           journal_id=prepared["result"]["journal_id"])
    assert changed["done"] and code == 4
    assert result["error"]["code"] == "publication_reconcile_required"
    assert not target.exists()
