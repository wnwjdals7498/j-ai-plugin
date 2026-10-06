"""Actual TLS proof: Phase 4 applicability preserves the original Host P2 pass."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

pytest_plugins = ["test_phase3_host_network"]

from pmt.util import canonical_json, fingerprint, new_id
from test_phase3_hosted_cli import _cli, _configure
from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout
from test_phase3_reuse import _actual_definition


def test_actual_hosted_read_applicability_saves_applicable_without_rewriting_p2_pass(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "applicability-config", tmp_path / "applicability-data"
    _configure(env, config)

    # Establish a current actual whole-workspace lock on the isolated Host run.
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

    definition = _actual_definition()
    definition["selectors"]["source"]["paths"] = [env["relative"]]
    command = ["python", "-c", "import json,sys; from pathlib import Path; "
        "g=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')); "
        "assert g['project_id']==sys.argv[2] and g['schema_version']==1; "
        "print('local graph verification passed')", env["relative"], env["project"]]
    completed = subprocess.run([sys.executable, *command[1:]], cwd=env["checkout"],
        capture_output=True, timeout=10, check=False)
    assert completed.returncode == 0, completed.stderr
    inputs = {"graph": env["pin"].graph_hash}
    input_hash = fingerprint(inputs)
    criteria_hashes = {item["id"]: fingerprint(item) for item in env["criteria"]}
    evidence = env["store_a"].publish_resource({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "evidence"}, completed.stdout, session_id=env["session_a"])["artifact_ref"]
    graph_raw = env["graph_path"].read_bytes()
    manifest = {"schema_version": 1, "target_id": env["step"],
        "definition_id": definition["definition_id"], "definition_version": definition["definition_version"],
        "environment_id": env["headers"]["x-pmt-environment"], "canonical_workspace": env["canonical"],
        "source_pin": env["pin"].to_dict(), "command": command, "inputs_sha256": input_hash,
        "criteria": criteria_hashes, "workspace_files": [{"path": env["relative"],
            "sha256": hashlib.sha256(graph_raw).hexdigest(), "size": len(graph_raw)}],
        "runtime": {"os": "fixture", "architecture": "fixture", "python": "3.13",
            "sqlite": "fixture", "packages": []}, "dependency_manifests": [],
        "configuration_hashes": [], "evidence_refs": [{"id": evidence["id"], "sha256": evidence["sha256"]}],
        "inventory_status": "complete", "provenance": "client_snapshot"}
    verification_resource = env["store_a"].publish_resource({"request_id": new_id(),
        "scope_id": env["project"], "purpose": "verification_snapshot"},
        canonical_json(manifest).encode("utf-8"), session_id=env["session_a"])["artifact_ref"]
    common = {"project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3, "expected_source": env["pin"].to_dict()}
    published, publish_code = env["store_a"].execute(_host_request(env, "publish_verification_snapshot",
        common | {"target_id": env["step"], "definition_id": definition["definition_id"],
            "definition_version": definition["definition_version"], "command": command,
            "inputs_sha256": input_hash, "verification_resource_ref": verification_resource,
            "expected_snapshot_revision": 0}))
    assert publish_code == 0 and published["ok"], published.get("error")
    snapshot_ref = published["result"]["snapshot_ref"]

    lookup, lookup_code = env["store_a"].execute(_host_request(env, "lookup_verification",
        common | {"target_id": env["step"], "definition_id": definition["definition_id"],
            "definition_version": definition["definition_version"], "command": command,
            "inputs": inputs, "verification_snapshot_ref": snapshot_ref}))
    assert lookup_code == 0 and lookup["ok"], lookup.get("error")
    assert lookup["result"]["input_fingerprint"]
    recorded, record_code = env["store_a"].execute(_host_request(env, "record_verification", common | {
        "target_id": env["step"], "definition_id": definition["definition_id"],
        "definition_version": definition["definition_version"], "command": command, "inputs": inputs,
        "outcome": "pass", "exit_code": completed.returncode, "evidence_ids": [evidence["id"]],
        "criterion_ids": [item["id"] for item in env["criteria"]],
        "before_fingerprint": lookup["result"]["input_fingerprint"],
        "expected_source": env["pin"].to_dict(), "verification_snapshot_ref": snapshot_ref}))
    assert record_code == 0 and recorded["ok"], recorded.get("error")
    verification_id = recorded["result"]["verification_id"]
    with __import__("contextlib").closing(env["db"].connect()) as conn:
        before = conn.execute("SELECT outcome,state,input_fingerprint FROM verifications WHERE id=?",
            (verification_id,)).fetchone()
        assert before and (before["outcome"], before["state"]) == ("pass", "valid")
        before_values = tuple(before)

    captured = _cli(config, data, _host_request(env, "capture_work_basis", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "inventory_paths": [env["relative"]]}))
    assert captured.returncode == 0, captured.stdout + captured.stderr
    basis_ref = json.loads(captured.stdout)["result"]["basis_ref"]
    applicability_req = _host_request(env, "read_applicability", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"],
        "paths": ["."], "basis_ref": basis_ref, "definition": definition,
        "target_id": env["step"], "command": command, "inputs": inputs, "event_id": new_id()})
    response = _cli(config, data, applicability_req)
    assert response.returncode == 0, response.stdout + response.stderr
    result = json.loads(response.stdout)["result"]
    assert result["status"] == "applicable", result
    stored = _cli(config, data, _host_request(env, "get_continuity_object", {
        "object_id": result["applicability_ref"], "kind": "applicability"}))
    assert stored.returncode == 0, stored.stdout + stored.stderr
    body = json.loads(stored.stdout)["result"]["body"]
    assert body["status"] == "applicable"
    assert body["verification_ref"] == verification_id
    with __import__("contextlib").closing(env["db"].connect()) as conn:
        after = conn.execute("SELECT outcome,state,input_fingerprint FROM verifications WHERE id=?",
            (verification_id,)).fetchone()
        assert tuple(after) == before_values
    assert not list(data.rglob("*.sqlite3"))
