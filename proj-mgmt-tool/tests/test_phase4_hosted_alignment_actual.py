"""Hosted R3 delegated-method alignment through isolated loopback HTTPS."""
from __future__ import annotations

import json
import subprocess

import pytest

pytest_plugins = ["test_phase3_host_network"]

from pmt.util import canonical_json, fingerprint, new_id
from test_phase3_hosted_cli import _cli, _configure
from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout


def _capture(env, config, data, paths):
    request = _host_request(env, "capture_work_basis", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "inventory_paths": paths})
    response = _cli(config, data, request)
    assert response.returncode == 0, response.stdout + response.stderr
    return json.loads(response.stdout)["result"]


def test_hosted_apply_alignment_receipt_uses_current_decision_event_and_actual_f1_f3(live_host, tmp_path):
    env = live_host
    # This is an actual authenticated save_decision operation on the isolated Host.
    decision_request = _host_request(env, "save_decision", {
        "decision_kind": "delegate", "decider": "main",
        "content": "Implementation details may change within this fixture scope.",
        "reason": "Explicit hosted integration test delegation",
        "delegation_scope": "implementation method details only",
        "confirmation_source": "user_selected"})
    decision_request["record_id"] = env["item"]
    decision_request["expected_revision"] = 1
    decision_envelope, decision_code = env["store_a"].execute(decision_request)
    assert decision_code == 0 and decision_envelope["ok"], decision_envelope.get("error")
    decision_ref = decision_envelope["result"]["decision_id"]

    graph = json.loads(canonical_json(env["graph"]))
    method = next(node for node in graph["nodes"] if node["tree_kind"] == "implementation")
    requirement = next(node for node in graph["nodes"] if node["tree_kind"] == "requirement")
    requirement["stop_reason"] = "implementation_boundary"
    method.update({"stop_reason": "user_delegated",
        "delegated_scope": "implementation method details only",
        "source_refs": sorted(set(method.get("source_refs", [])) | {decision_ref}),
        "work_item_step_refs": {"item_refs": [env["item"]]},
        "autonomy": {"authority": "user", "scope": "implementation method details only"}})
    env["graph"] = graph

    env = _seed_hosted_git_checkout(env, tmp_path)
    config, data = tmp_path / "hosted-alignment-config", tmp_path / "hosted-alignment-data"
    _configure(env, config)
    baseline_document = env["checkout"] / "docs" / "pmt-docs" / "plan.md"
    baseline_document.parent.mkdir(parents=True, exist_ok=True)
    baseline_document.write_text("# Hosted alignment plan\n", encoding="utf-8")
    subprocess.run(["git", "add", "--", "docs/pmt-docs/plan.md"], cwd=env["checkout"], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=PMT fixture",
        "commit", "-m", "hosted R3 document baseline"], cwd=env["checkout"], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    module = env["checkout"] / "src" / "fixture_module.py"
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text("def run():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "-f", "--", "src/fixture_module.py"], cwd=env["checkout"], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=PMT fixture",
        "commit", "-m", "hosted R3 fixture baseline"], cwd=env["checkout"], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Extend the isolated active run's actual claim to the fixture workspace so
    # code, graph and generated document paths are all inside the current owner scope.
    scopes = [{"kind": "path", "workspace": env["canonical"], "resource": "."}]
    with env["db"].write() as conn:
        row = conn.execute("SELECT intent_json FROM execution_runs WHERE id=?", (env["run"],)).fetchone()
        intent = json.loads(row["intent_json"])
        intent["scopes"] = scopes
        conn.execute("UPDATE execution_runs SET scopes_json=?,intent_json=? WHERE id=?",
            (canonical_json(scopes), canonical_json(intent), env["run"]))
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (env["run"],))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (new_id(), env["run"], env["session_a"], "path",
                env["canonical"], ".", "2026-10-06T00:00:00Z"))

    inventory_paths = [env["relative"], "src/fixture_module.py", "docs/pmt-docs/plan.md"]
    change_paths = ["src"]
    before = _capture(env, config, data, inventory_paths)
    before_pin = before["source_pin"]
    context_common = {"project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3, "expected_source": before_pin}
    rebuilt_before, rebuilt_before_code = env["store_a"].execute(_host_request(env,
        "rebuild_graph_index", context_common))
    assert rebuilt_before_code == 0 and rebuilt_before["ok"], rebuilt_before.get("error")
    context_response, context_code = env["store_a"].execute(_host_request(env, "build_task_context",
        context_common | {"task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
            "workspace": env["canonical"], "role": "lower",
            "node_ids": [node["id"] for node in graph["nodes"]],
            "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}}))
    assert context_code == 0 and context_response["ok"], context_response.get("error")
    context_ref = context_response["result"]["context_ref"]
    baseline_common = {"repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "expected_run_revision": 3,
        "context_ref": context_ref, "expected_source": before_pin}
    baseline_prepare_response = _cli(config, data, _host_request(env, "prepare_document_segments", baseline_common))
    assert baseline_prepare_response.returncode == 0, baseline_prepare_response.stdout + baseline_prepare_response.stderr
    baseline_prepare = json.loads(baseline_prepare_response.stdout)["result"]
    baseline_publish_response = _cli(config, data, _host_request(env, "publish_document_segments",
        baseline_common | {"journal_id": baseline_prepare["journal_id"]}))
    assert baseline_publish_response.returncode == 0, baseline_publish_response.stdout + baseline_publish_response.stderr
    module.write_text("def run():\n    return 2\n", encoding="utf-8")
    after = _capture(env, config, data, inventory_paths)
    after_pin = after["source_pin"]
    after_context_common = context_common | {"expected_source": after_pin}
    rebuilt_after, rebuilt_after_code = env["store_a"].execute(_host_request(env,
        "rebuild_graph_index", after_context_common))
    assert rebuilt_after_code == 0 and rebuilt_after["ok"], rebuilt_after.get("error")
    after_context, after_context_code = env["store_a"].execute(_host_request(env, "build_task_context",
        after_context_common | {"task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
            "workspace": env["canonical"], "role": "lower",
            "node_ids": [node["id"] for node in graph["nodes"]],
            "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}}))
    assert after_context_code == 0 and after_context["ok"], after_context.get("error")
    context_ref = after_context["result"]["context_ref"]
    collect = _cli(config, data, _host_request(env, "collect_changes", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"],
            "paths": change_paths, "before_basis_ref": before["basis_ref"], "after_basis_ref": after["basis_ref"],
        "expected_pointer_revision": 0}))
    assert collect.returncode == 0, collect.stdout + collect.stderr
    change = json.loads(collect.stdout)["result"]

    index_call = _cli(config, data, _host_request(env, "build_implementation_links", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"],
            "paths": change_paths, "basis_ref": after["basis_ref"], "expected_pointer_revision": 0,
        "mappings": [{"path": "src/fixture_module.py", "node_id": method["id"],
            "decision_ref": decision_ref}]}))
    assert index_call.returncode == 0, index_call.stdout + index_call.stderr
    index = json.loads(index_call.stdout)["result"]

    pin = after["source_pin"]
    common = {"repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "expected_run_revision": 3, "context_ref": context_ref, "expected_source": pin}
    change_set = {"change_id": new_id(),
        "reason": "Adjust only implementation method details in the recorded delegation",
        "changes": [{"op": "update", "id": method["id"],
            "fields": {"architecture": "Use a stable source-bound implementation flow."}}]}
    preview_response = _cli(config, data, _host_request(env, "preview_graph_change",
        common | {"change_set": change_set}))
    assert preview_response.returncode == 0, preview_response.stdout + preview_response.stderr
    preview = json.loads(preview_response.stdout)["result"]
    impact_response = _cli(config, data, _host_request(env, "calculate_graph_impact",
        common | {"change_set": change_set, "change_preview": preview}))
    assert impact_response.returncode == 0, impact_response.stdout + impact_response.stderr
    impact = json.loads(impact_response.stdout)["result"]
    assert impact["complete"] is True, impact.get("unknown")

    assess_response = _cli(config, data, _host_request(env, "assess_alignment", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"],
            "paths": change_paths, "basis_ref": after["basis_ref"], "change_ref": change["change_ref"],
        "index_ref": index["index_ref"], "typed_graph_preview": preview, "change_set": change_set}))
    assert assess_response.returncode == 0, assess_response.stdout + assess_response.stderr
    assessed = json.loads(assess_response.stdout)["result"]
    assert assessed["state"] == "assessed", {"unknown": assessed.get("unknown"),
        "typed_graph_impact": assessed.get("typed_graph_impact"), "assessment": assessed}
    assessment = env["store_a"].execute(_host_request(env, "get_continuity_object", {
        "object_id": assessed["assessment_ref"], "kind": "assessment"}))[0]["result"]
    decision_binding = next(item for item in assessment["body"]["delegation_refs"]
        if item["decision_ref"] == decision_ref)
    with __import__("contextlib").closing(env["db"].connect()) as conn:
        actual_event = conn.execute("SELECT id FROM events WHERE event_type='decision_saved' AND "
            "json_extract(payload_json,'$.decision_id')=? ORDER BY recorded_at DESC LIMIT 1", (decision_ref,)).fetchone()
    assert decision_binding["decision_event_ref"] == actual_event["id"]

    resolution_response = _cli(config, data, _host_request(env, "propose_semantic_resolution", {
        "assessment_ref": assessed["assessment_ref"], "resolution_kind": "method"}))
    assert resolution_response.returncode == 0, resolution_response.stdout + resolution_response.stderr
    resolution = json.loads(resolution_response.stdout)["result"]
    assert resolution["state"] == "delegated_method_candidate"

    graph_apply_response = _cli(config, data, _host_request(env, "apply_graph_change",
        common | {"change_set": change_set}))
    assert graph_apply_response.returncode == 0, graph_apply_response.stdout + graph_apply_response.stderr
    graph_effect = json.loads(graph_apply_response.stdout)["result"]
    after_f1 = graph_effect["source_pin"]
    f1_context_common = context_common | {"expected_source": after_f1}
    rebuilt_f1, rebuilt_f1_code = env["store_a"].execute(_host_request(env,
        "rebuild_graph_index", f1_context_common))
    assert rebuilt_f1_code == 0 and rebuilt_f1["ok"], rebuilt_f1.get("error")
    f1_context, f1_context_code = env["store_a"].execute(_host_request(env, "build_task_context",
        f1_context_common | {"task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
            "workspace": env["canonical"], "role": "lower",
            "node_ids": [node["id"] for node in json.loads(env["graph_path"].read_text(encoding="utf-8"))["nodes"]],
            "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}}))
    assert f1_context_code == 0 and f1_context["ok"], f1_context.get("error")
    next_common = common | {"expected_source": graph_effect["source_pin"], "change_set": change_set,
        "context_ref": f1_context["result"]["context_ref"],
        "change_preview": preview, "impact_set": impact, "apply_receipt": graph_effect}
    prepare_response = _cli(config, data, _host_request(env, "prepare_document_segments", next_common))
    assert prepare_response.returncode == 0, prepare_response.stdout + prepare_response.stderr
    prepared = json.loads(prepare_response.stdout)["result"]
    publish_response = _cli(config, data, _host_request(env, "publish_document_segments",
        next_common | {"journal_id": prepared["journal_id"]}))
    assert publish_response.returncode == 0, publish_response.stdout + publish_response.stderr
    document_effect = json.loads(publish_response.stdout)["result"]

    applied_request = _host_request(env, "apply_alignment", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"],
        "paths": change_paths, "document_path": document_effect["document_path"],
        "basis_ref": after["basis_ref"], "assessment_ref": assessed["assessment_ref"],
        "resolution_ref": resolution["resolution_ref"], "graph_effect_ref": graph_effect["effect_ref"],
        "document_effect_ref": document_effect["effect_ref"], "step_effect_refs": [],
        "expected_pointer_revision": 0})
    applied = _cli(config, data, applied_request)
    assert applied.returncode == 0, applied.stdout + applied.stderr
    result = json.loads(applied.stdout)["result"]
    assert result["applied_pointer_advanced"] is True
    assert result["client_post_readback"] is True
    assert result["host_git_verified"] is False and result["host_document_verified"] is False

    replay = _cli(config, data, applied_request)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    replay_result = json.loads(replay.stdout)["result"]
    assert replay_result["replayed"] is True
    assert replay_result["alignment_ref"] == result["alignment_ref"]
    applied_pointer = env["store_a"].execute(_host_request(env, "read_continuity_pointer", {
        "selector": {"repository_id": env["repo"], "branch": "main",
            "workspace_ref": env["canonical"], "task_id": env["item"],
            "purpose": "applied_alignment", "environment_id": env["headers"]["x-pmt-environment"]}}))[0]
    assert applied_pointer["ok"] and applied_pointer["result"]["revision"] == 1

    # R4 consumes the immutable pre-apply R2/R3 chain and the actual current
    # F1/F3 alignment receipt. Its current basis includes both effect targets.
    current = _capture(env, config, data, inventory_paths)
    post_pin = current["source_pin"]
    rebuilt_post, rebuilt_post_code = env["store_a"].execute(_host_request(env,
        "rebuild_graph_index", context_common | {"expected_source": post_pin}))
    assert rebuilt_post_code == 0 and rebuilt_post["ok"], rebuilt_post.get("error")
    from test_phase3_reuse import _actual_definition
    reuse_definition = _actual_definition()
    reuse_definition["selectors"]["source"]["paths"] = ["src/fixture_module.py"]
    applicability_request = _host_request(env, "read_applicability", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "paths": ["."], "basis_ref": current["basis_ref"],
        "definition": reuse_definition, "target_id": env["item"],
        "command": ["python", "-m", "py_compile", "src/fixture_module.py"],
        "inputs": {"target": "src/fixture_module.py"}})
    applicability_response = _cli(config, data, applicability_request)
    assert applicability_response.returncode == 0, applicability_response.stdout + applicability_response.stderr
    applicability = json.loads(applicability_response.stdout)["result"]
    current_after_app = _capture(env, config, data, inventory_paths)
    r4_request = _host_request(env, "compose_task_resume", {
        "basis_ref": current_after_app["basis_ref"], "repository_id": env["repo"],
        "workspace": str(env["checkout"]), "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3,
        "task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
        "role": "lower", "inventory_paths": inventory_paths,
        "node_ids": [node["id"] for node in json.loads(env["graph_path"].read_text(encoding="utf-8"))["nodes"]],
        "change_ref": change["change_ref"], "assessment_ref": assessed["assessment_ref"],
        "resolution_ref": resolution["resolution_ref"], "applicability_ref": applicability["applicability_ref"],
        "f5_budget": {"max_bytes": 512, "max_lines": 40, "unit": "utf8"},
        "budget": {"max_bytes": 32768, "max_lines": 400}})
    r4_response = _cli(config, data, r4_request)
    assert r4_response.returncode == 0, r4_response.stdout + r4_response.stderr
    r4 = json.loads(r4_response.stdout)["result"]
    current_change = r4["change_evidence"]
    assert current_change["change_ref"] == change["change_ref"]
    assert current_change["assessment_ref"] == assessed["assessment_ref"]
    assert current_change["resolution_ref"] == resolution["resolution_ref"]
    assert current_change["applicability_ref"] == applicability["applicability_ref"]
    assert current_change["alignment_ref"] == result["alignment_ref"]
    assert current_change["alignment_pointer_revision"] == 1
    assert current_change["applied"] is True, json.dumps(current_change, ensure_ascii=False, indent=2)
    assert r4["next_action"]["executable"] is False
    assert r4.get("context_ref") and r4.get("detail_cursor")
    r4_detail_request = _host_request(env, "read_resume_detail", {
        "basis_ref": r4["basis_ref"], "context_ref": r4["context_ref"],
        "cursor": r4["detail_cursor"], "repository_id": env["repo"],
        "workspace": str(env["checkout"]), "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3,
        "task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
        "inventory_paths": inventory_paths, "change_ref": change["change_ref"],
        "assessment_ref": assessed["assessment_ref"], "resolution_ref": resolution["resolution_ref"],
        "applicability_ref": applicability["applicability_ref"], "max_bytes": 4096, "max_lines": 200})
    detail_response = _cli(config, data, r4_detail_request)
    assert detail_response.returncode == 0, detail_response.stdout + detail_response.stderr
    detail = json.loads(detail_response.stdout)["result"]
    assert detail["owner_revalidated"] is True and detail["source_revalidated"] is True
    assert detail["change_evidence"]["applied"] is True
    assert not list(data.rglob("*.sqlite3"))
