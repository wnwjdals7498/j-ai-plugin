from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from pmt.efficiency.source import SourcePin
from pmt.errors import PmtError
from pmt.hosted_files import HostedFiles
from pmt.planning.graph import validate_graph
from pmt.util import canonical_json, new_id

pytest_plugins = ["test_phase3_host_network"]


class _Port:
    device_id = new_id()
    environment_id = new_id()
    namespace_id = new_id()

    def execute(self, _request):
        raise AssertionError("preview must not issue Host writes")

    def get_request_result(self, *_args, **_kwargs):
        return None


def _node(node_id, kind):
    value = {"id": node_id, "tree_kind": kind, "node_kind": "goal", "summary": "Fixture",
        "premise": "Pinned local source", "product_stage": "prototype",
        "product_scope": {"applies": False, "reason": "fixture"},
        "autonomy": {"authority": "user", "scope": "fixture"}}
    if kind == "requirement":
        value.update(criteria=["preserve source"], source_refs=["request:fixture"], evidence_refs=[])
    else:
        value.update(framework_assignment="stdlib", architecture="local adapter", logging="hashes only",
            tests=["preview"], function_spec={"input": "graph", "output": "graph", "constraints": "no Host file IO",
                "invariants": "SourcePin", "errors": "conflict", "verification": "fixture"},
            choice_set={"options": [], "insufficient_reason": "fixture"},
            choice={"source": "user", "selected": "fixture", "reason": "test", "scope": "local"})
    return value


@pytest.fixture
def adapter():
    project_id, repo_id, node_id, impl_id = (new_id() for _ in range(4))
    graph = {"schema_version": 1, "project_id": project_id, "graph_version": 4,
        "nodes": [_node(node_id, "requirement"), _node(impl_id, "implementation")], "relations": [],
        "provenance": {"request_ref": "request:fixture"}}
    report = validate_graph(graph, project_id, complete=False)
    pin = SourcePin(repo_id, project_id, "main", "c" * 40, 1, 4, report["sha256"], "clean")
    value = HostedFiles({"workspace_mappings": []}, _Port(), __import__("pathlib").Path.cwd() / ".pmt-test" / "hosted-files-spool")
    run = {"id": new_id(), "job_id": new_id(), "state": "running", "revision": 7,
        "workspace": "pmt://" + repo_id + "/" + ("a" * 64)}
    resolved = {"graph": graph, "source_pin": pin, "relative_graph_path": "docs/pmt/graph.json",
        "canonical_workspace": run["workspace"], "workspace": __import__("pathlib").Path.cwd(),
        "graph_path": __import__("pathlib").Path.cwd() / "graph.json"}
    value.runtime._prepare = lambda _req: (run, {}, {}, {}, pin, resolved)
    return value, pin, node_id


def _request(pin, node_id, *, expected=None):
    request_id = new_id()
    return {"protocol_version": 1, "operation": "preview_graph_change", "request_id": request_id,
        "actor": "fixture-actor", "session_id": "fixture-session", "scope_id": pin.project_id,
        "source": {"product": "fixture"}, "payload": {"run_id": new_id(), "context_ref": {"id": new_id()},
            "expected_source": (expected or pin).to_dict(), "change_set": {"change_id": new_id(),
                "reason": "fixture adjustment", "evidence_refs": [], "changes": [
                    {"op": "update", "id": node_id, "fields": {"premise": "updated"}}]}}}


def test_hosted_file_preview_uses_current_pinned_graph_and_makes_no_host_calls(adapter):
    value, pin, node_id = adapter
    req = _request(pin, node_id)
    envelope, code = value.execute(req)
    assert code == 0 and envelope["ok"]
    result = envelope["result"]
    assert result["source_pin"] == pin.to_dict()
    assert result["expected_new_source"]["graph_revision"] == pin.graph_revision + 1
    assert result["operations"][0]["op"] == "update"
    assert result["journal_status"] == "preview_only"


def test_hosted_file_preview_rejects_a_stale_source_pin(adapter):
    value, pin, node_id = adapter
    stale = SourcePin(pin.repository_id, pin.project_id, "main", "b" * 40, 1,
        pin.graph_revision, pin.graph_hash, "clean")
    envelope, code = value.execute(_request(pin, node_id, expected=stale))
    assert code == 3
    assert envelope["error"]["code"] == "source_conflict"


def test_hosted_file_branch_key_accepts_verified_non_git_and_detached_pins_only():
    from pmt.hosted_runtime import HostedLocalRuntime
    repo, project = new_id(), new_id()
    non_git = SourcePin(repo, project, None, None, 1, 1, "a" * 64, "clean")
    detached = SourcePin(repo, project, None, "c" * 40, 1, 1, "b" * 64, "clean")
    unknown = SourcePin(repo, project, "main", None, 1, 1, "d" * 64, "clean")
    assert HostedLocalRuntime._branch_key(non_git) == "non-git"
    assert HostedLocalRuntime._branch_key(detached) == "detached:" + "c" * 40
    with pytest.raises(PmtError) as caught:
        HostedLocalRuntime._branch_key(unknown)
    assert caught.value.code == "source_kind_unknown"


def test_actual_https_hosted_graph_preview_reads_pinned_client_checkout_only(live_host, tmp_path):
    from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    profile = {"workspace_mappings": [{"repository_id": env["repo"], "project_id": env["project"],
        "branch": "main", "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"],
        "canonical_workspace": env["canonical"]}]}
    adapter = HostedFiles(profile, env["store_a"], tmp_path / "file-spool")
    target = next(node for node in env["graph"]["nodes"] if node["tree_kind"] == "requirement")
    request = _host_request(env, "preview_graph_change", {**env["common"],
        "context_ref": env["context_ref"], "change_set": {"change_id": new_id(),
            "reason": "Hosted local preview fixture", "evidence_refs": [], "changes": [
                {"op": "update", "id": target["id"], "fields": {"premise": "Pinned preview only"}}]}})
    envelope, code = adapter.execute(request)
    assert code == 0 and envelope["ok"], envelope.get("error")
    assert envelope["result"]["source_pin"] == env["pin"].to_dict()
    assert envelope["result"]["expected_new_source"]["graph_revision"] == env["pin"].graph_revision + 1
    assert not list((tmp_path / "file-spool").rglob("*.sqlite3"))


def test_actual_https_graph_apply_uses_file_effect_and_publishes_source(live_host, tmp_path):
    from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    profile = {"workspace_mappings": [{"repository_id": env["repo"], "project_id": env["project"],
        "branch": "main", "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"],
        "canonical_workspace": env["canonical"]}]}
    adapter = HostedFiles(profile, env["store_a"], tmp_path / "apply-spool")
    target = next(node for node in env["graph"]["nodes"] if node["tree_kind"] == "requirement")
    request = _host_request(env, "apply_graph_change", {**env["common"],
        "context_ref": env["context_ref"], "change_set": {"change_id": new_id(),
            "reason": "Hosted local apply fixture", "evidence_refs": [], "changes": [
                {"op": "update", "id": target["id"], "fields": {"premise": "Applied locally"}}]}})
    envelope, code = adapter.execute(request)
    assert code == 0 and envelope["ok"], envelope.get("error")
    assert envelope["result"]["source_pin"]["graph_revision"] == env["pin"].graph_revision + 1
    assert json.loads((env["checkout"] / env["relative"]).read_text(encoding="utf-8"))["nodes"][0]
    assert not list((tmp_path / "apply-spool").rglob("*.sqlite3"))


@pytest.mark.parametrize("interrupt_at", ["candidate_upload", "target_detached", "target_detached_user_edit"])
def test_actual_https_graph_recovery_finishes_local_candidate_before_source_pointer(live_host, tmp_path, interrupt_at):
    from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    profile = {"workspace_mappings": [{"repository_id": env["repo"], "project_id": env["project"],
        "branch": "main", "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"],
        "canonical_workspace": env["canonical"]}]}
    adapter = HostedFiles(profile, env["store_a"], tmp_path / "recover-spool")
    target_node = next(node for node in env["graph"]["nodes"] if node["tree_kind"] == "requirement")
    original = _host_request(env, "apply_graph_change", {**env["common"],
        "context_ref": env["context_ref"], "change_set": {"change_id": new_id(),
            "reason": "F1 crash-recovery fixture", "evidence_refs": [], "changes": [
                {"op": "update", "id": target_node["id"], "fields": {"premise": "Recovered candidate"}}]}})
    publish_resource = env["store_a"].publish_resource
    execute = env["store_a"].execute
    if interrupt_at == "candidate_upload":
        def fail_effect(metadata, content, *, session_id):
            if metadata.get("purpose") == "graph_snapshot":
                raise PmtError("remote_unavailable", "injected upload interruption after local publish", 4, True)
            return publish_resource(metadata, content, session_id=session_id)
        env["store_a"].publish_resource = fail_effect
    else:
        def fail_effect(request):
            if (request.get("operation") == "complete_local_file_effect"
                    and request.get("payload", {}).get("phase") == "target_detached"):
                raise PmtError("remote_unavailable", "injected interruption after original detach", 4, True)
            return execute(request)
        env["store_a"].execute = fail_effect
    failed, code = adapter.execute(original)
    env["store_a"].publish_resource = publish_resource
    env["store_a"].execute = execute
    assert code == 4 and failed["error"]["code"] in {"remote_unavailable", "publication_reconcile_required"}
    local_path = env["checkout"] / env["relative"]
    if interrupt_at.startswith("target_detached"):
        assert not local_path.exists()
    else:
        local = json.loads(local_path.read_text(encoding="utf-8"))
        assert local["graph_version"] == env["pin"].graph_revision + 1
    source = env["store_a"].execute(_host_request(env, "read_source_snapshot", {
        **env["common"], "expected_source": env["pin"].to_dict()}))
    assert source[1] == 0 and source[0]["ok"]
    assert source[0]["result"]["source_pin"]["source_hash"] == env["pin"].source_hash
    recovery = _host_request(env, "recover_graph_change", {"original_request": original})
    if interrupt_at == "target_detached_user_edit":
        user_bytes = b"user recreated graph target during recovery\n"
        local_path.write_bytes(user_bytes)
        rejected, code = adapter.execute(recovery)
        assert code == 3 and rejected["error"]["code"] == "hosted_file_recovery_conflict"
        assert local_path.read_bytes() == user_bytes
        return
    recovered, code = adapter.execute(recovery)
    assert code == 0 and recovered["ok"], recovered.get("error")
    assert recovered["result"]["source_pin"]["graph_revision"] == env["pin"].graph_revision + 1
    assert local_path.exists() and json.loads(local_path.read_text(encoding="utf-8"))["graph_version"] == env["pin"].graph_revision + 1
    assert not list((tmp_path / "recover-spool").rglob("*.sqlite3"))


def test_actual_https_document_first_baseline_preserves_manual_prefix(live_host, tmp_path):
    from contextlib import closing
    from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout
    from pmt.util import utc_now

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    doc_path = "docs/pmt-docs/plan.md"
    target = env["checkout"] / doc_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Human note\n\nKeep this paragraph.\n", encoding="utf-8")
    with env["db"].write() as conn:
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
            (new_id(), env["run"], env["session_a"], "path", env["canonical"], doc_path, utc_now()))
    # The network fixture's graph is sufficient for F5 but has intentionally
    # incomplete leaf stop reasons; reuse the canonical document fixture shape.
    from test_phase3_documents import _node
    from pmt.efficiency.source import inspect_graph_source
    env["graph"]["nodes"] = [_node(item["id"], item["tree_kind"]) for item in env["graph"]["nodes"]]
    graph_path = env["checkout"] / env["relative"]
    graph_path.write_text(canonical_json(env["graph"]) + "\n", encoding="utf-8")
    env["pin"] = inspect_graph_source(env["checkout"], graph_path, env["repo"], env["project"],
        graph_scope_id=env["project"])["source_pin"]
    graph_bytes = canonical_json(env["graph"]).encode("utf-8")
    graph_resource = env["store_a"].publish_resource({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "graph_snapshot"}, graph_bytes, session_id=env["session_a"])["artifact_ref"]
    source_req = _host_request(env, "publish_source_snapshot", {"project_id": env["project"],
        "repository_id": env["repo"], "canonical_workspace": env["canonical"],
        "relative_graph_path": env["relative"], "run_id": env["run"], "expected_run_revision": 3,
        "expected_source_revision": 1, "branch_key": "main", "source_pin": env["pin"].to_dict(),
        "graph_resource_ref": graph_resource})
    source_result, source_code = env["store_a"].execute(source_req)
    assert source_code == 0 and source_result["ok"], source_result.get("error")
    rebuild_req = _host_request(env, "rebuild_graph_index", {"project_id": env["project"],
        "repository_id": env["repo"], "workspace": env["canonical"],
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "expected_source": env["pin"].to_dict()})
    rebuilt, rebuild_code = env["store_a"].execute(rebuild_req)
    assert rebuild_code == 0 and rebuilt["ok"], rebuilt.get("error")
    env["common"] = dict(env["common"], expected_source=env["pin"].to_dict())
    context_req = _host_request(env, "build_task_context", {**env["common"],
        "task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
        "role": "lower", "workspace": env["canonical"],
        "node_ids": [node["id"] for node in env["graph"]["nodes"]],
        "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}})
    context, context_code = env["store_a"].execute(context_req)
    assert context_code == 0 and context["ok"], context.get("error")
    env["context_ref"] = context["result"]["context_ref"]
    profile = {"workspace_mappings": [{"repository_id": env["repo"], "project_id": env["project"],
        "branch": "main", "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"],
        "canonical_workspace": env["canonical"]}]}
    adapter = HostedFiles(profile, env["store_a"], tmp_path / "document-spool")
    base = {**env["common"], "context_ref": env["context_ref"], "document_path": doc_path}
    prepare_req = _host_request(env, "prepare_document_segments", base)
    prepared, code = adapter.execute(prepare_req)
    assert code == 0 and prepared["ok"], prepared.get("error")
    publish_req = _host_request(env, "publish_document_segments", base | {
        "journal_id": prepared["result"]["journal_id"]})
    execute = env["store_a"].execute
    def interrupt_after_detach(request):
        if (request.get("operation") == "complete_local_file_effect"
                and request.get("payload", {}).get("phase") == "target_detached"):
            raise PmtError("remote_unavailable", "fixture interruption after document target detach", 4, True)
        return execute(request)
    env["store_a"].execute = interrupt_after_detach
    interrupted, code = adapter.execute(publish_req)
    env["store_a"].execute = execute
    assert code == 4 and interrupted["error"]["code"] == "publication_reconcile_required"
    assert not target.exists()
    recovery_req = _host_request(env, "recover_document_segments", {"original_request": publish_req})
    published, code = adapter.execute(recovery_req)
    assert code == 0 and published["ok"], published.get("error")
    text = target.read_text(encoding="utf-8")
    assert text.startswith("# Human note\n\nKeep this paragraph.\n")
    assert "<!-- PMT:DOCUMENT:BEGIN -->" in text and published["result"]["host_document_verified"] is False
    refreshed_req = _host_request(env, "build_task_context", {**env["common"],
        "task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
        "role": "lower", "workspace": env["canonical"],
        "node_ids": [node["id"] for node in env["graph"]["nodes"]],
        "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}})
    refreshed, code = env["store_a"].execute(refreshed_req)
    assert code == 0 and refreshed["ok"], refreshed.get("error")
    env["context_ref"] = refreshed["result"]["context_ref"]

    # F3 derives a partial-document scope from the actual old graph and the
    # registered baseline; then F1 applies the same original ChangeSet.
    old_pin = env["pin"]
    changed_node = next(node for node in env["graph"]["nodes"] if node["tree_kind"] == "requirement")
    change_set = {"change_id": new_id(), "reason": "Hosted premise update", "evidence_refs": [],
        "changes": [{"op": "update", "id": changed_node["id"], "fields": {"premise": "Updated premise"}}]}
    preview_req = _host_request(env, "preview_graph_change", {**env["common"],
        "context_ref": env["context_ref"], "change_set": change_set})
    preview, code = adapter.execute(preview_req)
    assert code == 0 and preview["ok"], preview.get("error")
    impact_req = _host_request(env, "calculate_graph_impact", {**env["common"],
        "expected_source": old_pin.to_dict(), "change_set": change_set,
        "change_preview": preview["result"], "rule_version": "graph-field-semantics-1", "max_depth": 4})
    impact, code = env["store_a"].execute(impact_req)
    assert code == 0 and impact["ok"], impact.get("error")
    assert impact["result"]["complete"] is True and impact["result"]["unknown"] == []
    apply_req = _host_request(env, "apply_graph_change", {**env["common"],
        "context_ref": env["context_ref"], "change_set": change_set})
    applied, code = adapter.execute(apply_req)
    assert code == 0 and applied["ok"], applied.get("error")
    env["pin"] = applied["result"]["source_pin"]
    env["common"] = dict(env["common"], expected_source=env["pin"])
    next_context_req = _host_request(env, "build_task_context", {**env["common"],
        "task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
        "role": "lower", "workspace": env["canonical"],
        "node_ids": [node["id"] for node in json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]],
        "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}})
    next_context, code = env["store_a"].execute(next_context_req)
    assert code == 0 and next_context["ok"], next_context.get("error")
    env["context_ref"] = next_context["result"]["context_ref"]
    target.write_text("## New user note\n\n" + target.read_text(encoding="utf-8"), encoding="utf-8")
    from pmt.efficiency.documents import _parse_document
    assert _parse_document(target.read_text(encoding="utf-8"))["has_block"] is True
    partial_payload = {**env["common"], "context_ref": env["context_ref"], "document_path": doc_path,
        "impact_set": impact["result"], "apply_receipt": applied, "change_set": change_set,
        "change_preview": preview["result"]}
    partial_prepared, code = adapter.execute(_host_request(env, "prepare_document_segments", partial_payload))
    assert code == 0 and partial_prepared["ok"], canonical_json(partial_prepared.get("error"))
    partial_published, code = adapter.execute(_host_request(env, "publish_document_segments",
        partial_payload | {"journal_id": partial_prepared["result"]["journal_id"]}))
    assert code == 0 and partial_published["ok"], partial_published.get("error")
    text = target.read_text(encoding="utf-8")
    assert text.startswith("## New user note\n\n# Human note\n")
    assert partial_published["result"]["host_document_verified"] is False
    refreshed_req = _host_request(env, "build_task_context", {**env["common"],
        "task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
        "role": "lower", "workspace": env["canonical"],
        "node_ids": [node["id"] for node in json.loads(graph_path.read_text(encoding="utf-8"))["nodes"]],
        "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}})
    refreshed, code = env["store_a"].execute(refreshed_req)
    assert code == 0 and refreshed["ok"], refreshed.get("error")
    env["context_ref"] = refreshed["result"]["context_ref"]
    forged = text.replace("Premise: Updated premise", "Premise: edited outside the guarded publisher", 1)
    assert forged != text
    target.write_text(forged, encoding="utf-8")
    rejected, code = adapter.execute(_host_request(env, "prepare_document_segments", {
        **env["common"], "context_ref": env["context_ref"], "document_path": doc_path}))
    assert code == 3 and rejected["error"]["code"] == "document_generated_edit_conflict"
    assert target.read_text(encoding="utf-8") == forged
