"""Schema-five continuity storage through the real loopback HTTPS CLI path."""
from __future__ import annotations

import json
import os
from contextlib import closing

import pytest

pytest_plugins = ["test_phase3_host_network"]

from test_phase3_hosted_cli import _cli, _configure
from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout
from pmt.continuity.current import _snapshot, basis_body
from pmt.hooks import ADAPTER_VERSION
from pmt.util import canonical_json, new_id


def test_hosted_cli_continuity_store_uses_current_tls_host_without_local_database(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "phase4-hosted-config", tmp_path / "phase4-hosted-data"
    _configure(env, config)
    selector = {"repository_id": env["repo"], "branch": "main",
        "workspace_ref": env["canonical"], "task_id": None, "purpose": "basis",
        "environment_id": env["headers"]["x-pmt-environment"]}
    body = {"capture": "loopback-synthetic", "source_provenance": "client_observed",
        "source": {"repository_id": env["repo"], "branch": "main", "workspace_ref": env["canonical"]},
        "work": {"task_id": None},
        "conditions": {"environment_id": env["headers"]["x-pmt-environment"]}}
    put = _host_request(env, "put_continuity_object", {
        "project_id": env["project"], "repository_id": env["repo"], "branch": "main",
        "kind": "basis", "body": body, "event_id": new_id()})
    saved = _cli(config, data, put)
    assert saved.returncode == 0, saved.stdout + saved.stderr
    saved_envelope = json.loads(saved.stdout)
    assert saved_envelope["ok"] and saved_envelope["result"]["body_hash"]

    get = _host_request(env, "get_continuity_object", {
        "project_id": env["project"], "repository_id": env["repo"], "branch": "main",
        "object_id": saved_envelope["result"]["id"], "kind": "basis"})
    read = _cli(config, data, get)
    assert read.returncode == 0
    assert json.loads(read.stdout)["result"]["body"] == body

    advance = _host_request(env, "advance_continuity_pointer", {
        "project_id": env["project"], "repository_id": env["repo"], "branch": "main",
        "selector": selector, "object_id": saved_envelope["result"]["id"],
        "expected_pointer_revision": 0})
    published = _cli(config, data, advance)
    assert published.returncode == 0 and json.loads(published.stdout)["result"]["revision"] == 1
    stale = _cli(config, data, {**advance, "request_id": new_id()})
    assert stale.returncode == 3 and json.loads(stale.stdout)["error"]["code"] == "revision_conflict"

    private = _host_request(env, "put_continuity_object", {
        "project_id": env["project"], "repository_id": env["repo"], "branch": "main",
        "kind": "checkpoint", "body": {"boundary": "caller_claim"}})
    denied = _cli(config, data, private)
    assert denied.returncode == 3
    assert json.loads(denied.stdout)["error"]["code"] == "private_metadata_forbidden"
    assert not list(data.rglob("*.sqlite3")), "Hosted mode must not instantiate a local primary database"
    with closing(env["db"].connect()) as conn:
        assert conn.execute("SELECT body_json FROM continuity_objects WHERE id=?",
            (saved_envelope["result"]["id"],)).fetchone() is not None


def test_hosted_native_session_overview_uses_registered_principal_explicit_scope_and_tls(live_host, tmp_path,
                                                                                           monkeypatch):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "hook-hosted-config", tmp_path / "hook-hosted-data"
    _configure(env, config)
    monkeypatch.setenv("PMT_SCOPE_ID", env["project"])
    request = _host_request(env, "compose_resume_overview", {
        "selector": {"repository_id": env["repo"], "branch": "main",
            "workspace_ref": env["canonical"], "task_id": None, "purpose": "current",
            "environment_id": None},
        "role": "main", "budget": {"max_bytes": 4096, "max_lines": 48}})
    request["actor"] = "hook"
    request["source"] = {"product": "codex", "adapter_version": ADAPTER_VERSION,
        "installation_id": env["headers"]["x-pmt-environment"],
        "native_event": "SessionStart", "native_session_id": env["session_a"]}
    response = _cli(config, data, request)
    assert response.returncode == 0, response.stdout + response.stderr
    overview = json.loads(response.stdout)["result"]
    assert overview["metadata_only"] is True
    assert overview["private_detail_read"] is False
    assert overview["overview"]["scope"]["project_ref"] == env["project"]
    assert "fresh" not in response.stdout
    assert not list(data.rglob("*.sqlite3"))

    monkeypatch.delenv("PMT_SCOPE_ID")
    no_scope = _cli(config, data, request)
    assert no_scope.returncode == 3
    assert json.loads(no_scope.stdout)["error"]["code"] == "hook_scope_required"


def test_hosted_checkpoint_requires_actual_decision_receipt_and_ignores_idle_event(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "checkpoint-hosted-config", tmp_path / "checkpoint-hosted-data"
    _configure(env, config)

    decision = _host_request(env, "save_decision", {
        "decision_kind": "custom", "decider": "user", "content": "keep the fixture scope",
        "reason": "explicit fixture decision", "confirmation_source": "user_selected"})
    decision["record_id"] = env["item"]
    decision["expected_revision"] = 1
    decision_result = _cli(config, data, decision)
    assert decision_result.returncode == 0, decision_result.stdout + decision_result.stderr
    with closing(env["db"].connect()) as conn:
        boundary = conn.execute("SELECT event_id,new_revision FROM events WHERE event_type='decision_saved' "
            "AND scope_id=? AND record_id=? ORDER BY recorded_at DESC LIMIT 1",
            (env["project"], env["item"])).fetchone()
        assert boundary and boundary["new_revision"] == 2

    with closing(env["db"].connect()) as conn:
        conn.execute("BEGIN")
        current_snapshot = _snapshot(conn, env["project"])
    work_revisions = [{"id": key, "revision": value}
        for key, value in current_snapshot["revision_set"].items()
        if not key.startswith(("run:", "pending:", "step_spec:"))]
    body = basis_body(
        scope={"project_id": env["project"], "repository_id": env["repo"]},
        source={"repository_id": env["repo"], "branch": "main", "workspace_ref": env["canonical"],
            "observed_head": env["pin"].reviewed_commit, "analyzed_ref": env["pin"].reviewed_commit,
            "applied_ref": None, "dirty_state": "clean", "dirty_fingerprint": None,
            "inventory_ref": "fixture-inventory", "inventory_hash": "b" * 64,
            "inventory_coverage": {"selected_count": 1, "verified_count": 1,
                "unknown_count": 0, "complete": True, "reason_codes": []}},
        contract={"graph_schema": 1, "graph_revision": 1, "graph_hash": "c" * 64,
            "requirement_refs": [], "decision_refs": current_snapshot["decisions"]},
        work={"capture_ref": current_snapshot["snapshot_hash"], "task_id": env["item"],
            "records": work_revisions, "run_refs": current_snapshot["active_execution"],
            "claim_refs": current_snapshot["claim_refs"], "pending_refs": current_snapshot["pending_refs"]},
        conditions={"environment_id": env["headers"]["x-pmt-environment"],
            "selected": ["loopback-fixture"], "unknown": []},
        manifest={"components": [{"name": "synthetic-test-basis", "complete": True}],
            "coherence": "coherent", "captured_at": "2026-10-06T00:00:00Z"})
    basis = _host_request(env, "put_continuity_object", {"kind": "basis", "body": body,
        "event_id": new_id()})
    basis_result = _cli(config, data, basis)
    assert basis_result.returncode == 0, basis_result.stdout + basis_result.stderr
    basis_ref = json.loads(basis_result.stdout)["result"]["id"]
    create = _host_request(env, "create_checkpoint", {"basis_ref": basis_ref,
        "boundary_event_id": boundary["event_id"], "expected_pointer_revision": 0})
    created = _cli(config, data, create)
    assert created.returncode == 0, created.stdout + created.stderr
    checkpoint_ref = json.loads(created.stdout)["result"]["checkpoint_ref"]

    selector = {"repository_id": env["repo"], "branch": "main", "workspace_ref": env["canonical"],
        "task_id": env["item"], "purpose": "current", "environment_id": None}
    read = _cli(config, data, _host_request(env, "read_checkpoint", {"selector": selector}))
    assert read.returncode == 0
    saved_checkpoint = json.loads(read.stdout)["result"]
    assert saved_checkpoint["current_pointer"] is True
    assert saved_checkpoint["checkpoint_ref"] == checkpoint_ref
    assert saved_checkpoint["checkpoint"]["boundary_kind"] == "decision_saved"

    from pmt.hooks import process_session_start
    hook_env = os.environ.copy() | {
        "PMT_SCOPE_ID": env["project"], "PMT_RECORD_ID": env["item"],
        "PMT_DATA_ROOT": str(data), "PMT_CONFIG_ROOT": str(config),
        "PMT_INSTALLATION_ID": env["headers"]["x-pmt-environment"]}
    hook_result = process_session_start("codex", {"session_id": env["session_a"],
        "hook_event_name": "SessionStart", "source": "resume"}, environ=hook_env, timeout=15)
    assert hook_result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    context_text = hook_result["hookSpecificOutput"]["additionalContext"]
    overview = json.loads(context_text.split("\n", 1)[1])
    assert overview["checkpoint_ref"] == checkpoint_ref
    assert overview["basis_ref"] == saved_checkpoint["checkpoint"]["basis_ref"]

    idle_event_id = new_id()
    idle = _host_request(env, "record_event", {})
    idle["normalized_event"] = {"event_id": idle_event_id, "type": "session_idle"}
    assert _cli(config, data, idle).returncode == 0
    rejected = _cli(config, data, _host_request(env, "create_checkpoint", {
        "basis_ref": basis_ref, "boundary_event_id": idle_event_id,
        "expected_pointer_revision": 1}))
    assert rejected.returncode == 3
    assert json.loads(rejected.stdout)["error"]["code"] == "checkpoint_boundary_not_confirmed"
    unchanged = _cli(config, data, _host_request(env, "read_checkpoint", {"selector": selector}))
    assert json.loads(unchanged.stdout)["result"]["pointer"]["revision"] == 1
    assert not list(data.rglob("*.sqlite3"))


def test_hosted_capture_work_basis_reads_local_git_and_publishes_only_attested_metadata(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "capture-hosted-config", tmp_path / "capture-hosted-data"
    _configure(env, config)
    request = _host_request(env, "capture_work_basis", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "inventory_paths": [env["relative"]]})
    captured = _cli(config, data, request)
    assert captured.returncode == 0, captured.stdout + captured.stderr
    result = json.loads(captured.stdout)["result"]
    assert result["complete"] is True
    assert result["source_provenance"] == "client_attested"
    assert result["host_git_verified"] is False
    assert result["inventory_coverage"]["verified_count"] == 1
    assert result["client_detail_available"] is True
    assert env["checkout"].as_posix() not in canonical_json(result)
    assert not list(data.rglob("*.sqlite3"))
    with closing(env["db"].connect()) as conn:
        stored = conn.execute("SELECT body_json FROM continuity_objects WHERE id=? AND kind='basis'",
                              (result["basis_ref"],)).fetchone()
        assert stored is not None
        body = json.loads(stored["body_json"])
        assert body["source"]["source_provenance"] == "client_attested"
        assert body["source"]["host_git_verified"] is False
        assert env["checkout"].as_posix() not in canonical_json(body)
    detail_files = list((data / "hosted-continuity-private").rglob("*.json"))
    assert len(detail_files) == 1
    private_detail = json.loads(detail_files[0].read_text(encoding="utf-8"))
    assert private_detail["items"][0]["relative_path"] == env["relative"]


def test_hosted_cli_routes_validate_basis_through_client_file_adapter(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "validate-hosted-config", tmp_path / "validate-hosted-data"
    _configure(env, config)
    captured = _cli(config, data, _host_request(env, "capture_work_basis", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "inventory_paths": [env["relative"]]}))
    assert captured.returncode == 0, captured.stdout + captured.stderr
    basis_ref = json.loads(captured.stdout)["result"]["basis_ref"]
    request = _host_request(env, "validate_basis", {"basis_ref": basis_ref,
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "inventory_paths": [env["relative"]]})
    validated = _cli(config, data, request)
    assert validated.returncode == 0, validated.stdout + validated.stderr
    result = json.loads(validated.stdout)["result"]
    assert result["status"] == "unchanged"
    assert result["source_provenance"] == "client_attested"
    assert result["host_git_verified"] is False
    assert not list(data.rglob("*.sqlite3"))
