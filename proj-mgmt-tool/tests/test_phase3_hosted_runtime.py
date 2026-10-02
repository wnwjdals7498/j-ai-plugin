"""Hosted controller port tests against the real local HTTPS Host tier."""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone

import pytest

pytest_plugins = ["test_phase3_host_network"]

from pmt.errors import PmtError
from pmt.efficiency.source import inspect_graph_source
from pmt.hosted_runtime import HostedControlStateRepository
from pmt.util import canonical_json, fingerprint, new_id


def _host_request(env, operation, payload, *, request_id=None):
    return {"protocol_version": 1, "operation": operation, "request_id": request_id or new_id(),
        "actor": env["actor"], "session_id": env["session_a"], "scope_id": env["project"],
        "source": {"product": "cli"}, "context_refs": [], "payload": payload}


def _publish_host_source(env):
    graph_wire = canonical_json(env["graph"]).encode("utf-8")
    artifact = env["store_a"].publish_resource({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "graph_snapshot"}, graph_wire, session_id=env["session_a"])["artifact_ref"]
    request = _host_request(env, "publish_source_snapshot", {
        "project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3, "expected_source_revision": 0,
        "branch_key": "main", "source_pin": env["pin"].to_dict(), "graph_resource_ref": artifact})
    envelope, code = env["store_a"].execute(request)
    assert code == 0 and envelope["ok"], envelope.get("error")
    return envelope["result"]


def _seed_hosted_git_checkout(env, tmp_path):
    checkout = tmp_path / "client-checkout"
    graph_path = checkout / Path(*env["relative"].split("/"))
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text(canonical_json(env["graph"]) + "\n", encoding="utf-8")
    subprocess.run(["git", "init", "-b", "main"], cwd=checkout, check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "add", "-f", "--", env["relative"]], cwd=checkout,
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=PMT Hosted Fixture",
                    "commit", "-m", "initial hosted source"], cwd=checkout,
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    inspected = inspect_graph_source(checkout, graph_path, env["repo"], env["project"],
                                     graph_scope_id=env["project"])
    pin = inspected["source_pin"]
    assert pin.selected_ref == "main" and pin.source_kind == "git"
    assert pin.dirty_state in {"clean", "dirty"}
    if pin.dirty_state == "dirty":
        assert pin.dirty_fingerprint

    now = "2026-10-02T00:00:00Z"
    with env["db"].write() as conn:
        row = conn.execute("SELECT job_id,intent_json,scopes_json FROM execution_runs WHERE id=?",
                           (env["run"],)).fetchone()
        intent = json.loads(row["intent_json"])
        scopes = json.loads(row["scopes_json"])
        intent.update(run_id=env["run"], job_id=row["job_id"], step_id=env["step"],
            workspace=env["canonical"], scopes=scopes, dependencies=[],
            role="lower", criteria=env["criteria"], directive_ref=env["directive_resource"]["artifact_id"],
            directive_version=1, requirements_version="requirements-v1", plan_version="plan-v1")
        route = {"agent": "codex", "provider": "fixture-provider", "model": "fixture-model",
            "mode": "cli", "adapter_kind": "cli", "auth_state": "authenticated",
            "actual_support": "verified_supported", "capability_ref": "fixture-cli-capability"}
        conn.execute("UPDATE execution_runs SET state='starting',route_json=?,intent_json=?,updated_at=? WHERE id=?",
            (canonical_json(route), canonical_json(intent), now, env["run"]))
        conn.execute("UPDATE execution_jobs SET state='starting',updated_at=? WHERE id=?", (now, row["job_id"]))

    env["pin"] = pin
    env["checkout"] = checkout
    env["graph_path"] = graph_path
    env["branch_key_sha256"] = hashlib.sha256(b"main").hexdigest()
    graph_wire = canonical_json(env["graph"]).encode("utf-8")
    artifact = env["store_a"].publish_resource({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "graph_snapshot"}, graph_wire, session_id=env["session_a"])["artifact_ref"]
    published = env["store_a"].execute(_host_request(env, "publish_source_snapshot", {
        "project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3, "expected_source_revision": 0,
        "branch_key": "main", "source_pin": pin.to_dict(), "graph_resource_ref": artifact}))
    assert published[1] == 0 and published[0]["ok"], published[0].get("error")
    common = {"project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3, "expected_source": pin.to_dict()}
    rebuilt, code = env["store_a"].execute(_host_request(env, "rebuild_graph_index", common))
    assert code == 0 and rebuilt["ok"], rebuilt.get("error")
    context, code = env["store_a"].execute(_host_request(env, "build_task_context", {
        **common, "task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
        "role": "lower", "workspace": env["canonical"],
        "node_ids": [node["id"] for node in env["graph"]["nodes"]],
        "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}}))
    assert code == 0 and context["ok"] and context["result"]["incomplete"] is False
    env["context_ref"] = context["result"]["context_ref"]
    env["common"] = common
    return env


def _host_f6_reuse(env):
    evidence = env["store_a"].publish_resource({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "evidence"}, b"hosted F6 fixture evidence", session_id=env["session_a"])["artifact_ref"]
    from test_phase3_reuse import _actual_definition
    definition = _actual_definition()
    definition["selectors"]["source"]["paths"] = [env["relative"]]
    inputs = {"graph": env["pin"].graph_hash}
    command = ["codex", "fixture-test"]
    input_hash = fingerprint(inputs)
    criteria_hashes = {item["id"]: fingerprint(item) for item in env["criteria"]}
    manifest = {"schema_version": 1, "target_id": env["step"], "definition_id": "test.py_compile",
        "definition_version": "1", "environment_id": env["headers"]["x-pmt-environment"],
        "canonical_workspace": env["canonical"], "source_pin": env["pin"].to_dict(),
        "command": command, "inputs_sha256": input_hash, "criteria": criteria_hashes,
        "workspace_files": [{"path": env["relative"], "sha256": hashlib.sha256(
            env["graph_path"].read_bytes()).hexdigest(), "size": env["graph_path"].stat().st_size}],
        "runtime": {"os": "fixture", "architecture": "fixture", "python": "3.13",
            "sqlite": "fixture", "packages": []}, "dependency_manifests": [],
        "configuration_hashes": [], "evidence_refs": [{"id": evidence["id"], "sha256": evidence["sha256"]}],
        "inventory_status": "complete", "provenance": "client_snapshot"}
    verification_resource = env["store_a"].publish_resource({"request_id": new_id(),
        "scope_id": env["project"], "purpose": "verification_snapshot"},
        canonical_json(manifest).encode(), session_id=env["session_a"])["artifact_ref"]
    request = _host_request(env, "publish_verification_snapshot", {**env["common"],
        "target_id": env["step"], "definition_id": "test.py_compile", "definition_version": "1",
        "command": command, "inputs_sha256": input_hash,
        "verification_resource_ref": verification_resource, "expected_snapshot_revision": 0})
    envelope, code = env["store_a"].execute(request)
    assert code == 0 and envelope["ok"], envelope.get("error")
    reuse_request = _host_request(env, "resolve_reuse", {**env["common"],
        "definition": definition, "target_id": env["step"], "workspace": env["canonical"],
        "paths": [env["relative"]], "command": command, "inputs": inputs, "event_id": new_id()})
    reuse, code = env["store_a"].execute(reuse_request)
    assert code == 0 and reuse["ok"], reuse.get("error")
    assert reuse["result"]["status"] == "claimed", reuse["result"]
    env["reuse_ref"] = reuse["result"]["body_ref"]
    return env["reuse_ref"]


def test_hosted_control_repository_cas_original_response_and_owner_replay(live_host):
    env = live_host
    _publish_host_source(env)
    repository = HostedControlStateRepository(env["store_a"])
    context_ref = {"kind": "task_context", "id": new_id(), "scope_id": env["project"],
        "source_hash": env["pin"].source_hash, "version": 1, "projection_hash": "a" * 64}
    original = _host_request(env, "advance_execution_control", {
        "run_id": env["run"], "context_ref": context_ref,
        "reuse_body_ref": {"kind": "reuse_claim", "id": new_id()}})
    body = {"schema_version": 1, "run_id": env["run"], "scope_id": env["project"],
        "stage": "waiting_context", "source_hash": env["pin"].source_hash,
        "context_ref": context_ref,
        "reason_code": "fixture_wait", "retry_count": 0, "locks_retained": True}
    first = repository.compare_and_set(original, env["run"], env["project"], env["pin"].source_hash,
        body, expected_revision=0, event_name="control.state_observed", request_suffix="first")
    assert first["revision"] == 1 and first["control_ref"]["kind"] == "execution_control"
    current = repository.get(original, env["run"])
    assert current["revision"] == 1 and current["body"] == body

    with closing(env["db"].connect()) as conn:
        run_state = conn.execute("SELECT state FROM execution_runs WHERE id=?", (env["run"],)).fetchone()[0]
    response = {"control_ref": first["control_ref"], "run_id": env["run"],
        "run_state": run_state, "locks_retained": True,
        "action": {"kind": "wait", "run_id": env["run"], "reason": "fixture_wait"}}
    stored, exit_code = repository.complete_response(original, response, run_id=env["run"],
        scope_id=env["project"], stage="waiting_context", operation="advance_execution_control", body=body)
    assert exit_code == 0 and stored["ok"] and stored["result"] == response
    assert stored["request_id"] == original["request_id"]
    replay = env["store_a"].get_request_result(original["request_id"], env["actor"],
        env["session_a"], expected_request=original)
    assert replay == (stored, 0)

    changed = json.loads(canonical_json(original))
    changed["payload"]["run_id"] = new_id()
    with pytest.raises(PmtError) as conflict:
        env["store_a"].get_request_result(original["request_id"], env["actor"],
            env["session_a"], expected_request=changed)
    assert conflict.value.code == "request_conflict"
    with pytest.raises(PmtError) as cas_conflict:
        repository.compare_and_set(original, env["run"], env["project"], env["pin"].source_hash,
            body | {"reason_code": "changed"}, expected_revision=0, request_suffix="stale")
    assert cas_conflict.value.code == "revision_conflict"

    denied, denied_code = env["store_b"].execute(_host_request(
        env | {"actor": "network-client-b", "session_a": env["session_b"]},
        "read_execution_control", {"run_id": env["run"]}))
    assert denied_code == 3 and not denied["ok"]
    assert denied["error"]["code"] in {"ownership_conflict", "scope_forbidden"}


def test_hosted_control_metadata_rejects_local_paths_and_private_process_values(live_host):
    env = live_host
    _publish_host_source(env)
    repository = HostedControlStateRepository(env["store_a"])
    base = {"schema_version": 1, "run_id": env["run"], "scope_id": env["project"],
        "stage": "waiting", "source_hash": env["pin"].source_hash, "locks_retained": True}
    for key, value in (("pid", 1234), ("argv", ["codex"]), ("private_path", "C:\\private\\repo"),
                       ("prompt", "private directive"), ("authorization", "Bearer fixture")):
        with pytest.raises(PmtError) as caught:
            repository.compare_and_set(_host_request(env, "advance_execution_control", {"run_id": env["run"]}),
                env["run"], env["project"], env["pin"].source_hash, base | {key: value},
                expected_revision=0, request_suffix="bad:" + key)
        assert caught.value.code == "control_body_invalid"


def test_pending_native_action_body_keeps_only_refs_and_reads_private_original_response(live_host):
    env = live_host
    _publish_host_source(env)
    repository = HostedControlStateRepository(env["store_a"])
    original = _host_request(env, "advance_execution_control", {
        "run_id": env["run"], "context_ref": {"kind": "task_context", "id": new_id(),
            "scope_id": env["project"], "source_hash": env["pin"].source_hash,
            "version": 1, "projection_hash": "a" * 64},
        "reuse_body_ref": {"kind": "reuse_claim", "id": new_id()}})
    nonce = new_id()
    context_ref = original["payload"]["context_ref"]
    instruction = ("Use only the current bounded F5 context. Invoke exactly one native subagent call. "
        "Do not read or request a larger private directive. Return its real opaque handle and final result "
        "through the supplied operations; do not report a test or evidence that was not observed.")
    return_contract = {"native_handle": "attach_execution_handle after actual invocation",
        "result_operation": "submit_execution_result after actual completion"}
    action = {"kind": "main-native-call", "run_id": env["run"], "expected_run_revision": 3,
        "action_nonce": nonce, "context_ref": context_ref, "prompt_sha256": "b" * 64,
        "capability_ref": "fixture-capability", "directive_ref": "fixture-directive-ref",
        "agent": "fixture", "provider": "fixture", "model": "fixture-model",
        "instruction": instruction, "return_contract": return_contract}
    body = {"schema_version": 1, "run_id": env["run"], "scope_id": env["project"],
        "stage": "main_action_pending", "source_hash": env["pin"].source_hash,
        "action_nonce": nonce, "context_ref": context_ref, "prompt_sha256": "b" * 64,
        "reuse_decision_ref": {"id": new_id()},
        "action": action, "locks_retained": True}
    state = repository.compare_and_set(original, env["run"], env["project"], env["pin"].source_hash,
        body, expected_revision=0, event_name="control.observation_started", request_suffix="pending-action")
    with closing(env["db"].connect()) as conn:
        run_state = conn.execute("SELECT state FROM execution_runs WHERE id=?", (env["run"],)).fetchone()[0]
    result = {"control_ref": state["control_ref"], "run_id": env["run"], "run_state": run_state,
        "locks_retained": True, "action": action}
    stored, code = repository.complete_response(original, result, run_id=env["run"],
        scope_id=env["project"], stage="main_action_pending", operation="advance_execution_control", body=body)
    assert code == 0 and stored["ok"] and stored["result"]["action"] == action

    raw_request = _host_request(env, "read_execution_control", {"run_id": env["run"]})
    raw, raw_code = env["store_a"].execute(raw_request)
    assert raw_code == 0 and raw["ok"]
    persisted = raw["result"]["body"]
    assert "instruction" not in json.dumps(persisted) and "group_prompt" not in json.dumps(persisted)
    assert persisted["action_response_request_id"] == original["request_id"]

    current_req = _host_request(env, "read_execution_control", {"run_id": env["run"]})
    current, current_code = env["store_a"].execute(current_req)
    assert current_code == 0 and current["ok"]
    assert "instruction" not in current["result"]["body"]["action"]
    private_request = _host_request(env, "read_execution_control", {
        "run_id": env["run"], "include_pending_action": True})
    private, private_code = env["store_a"].execute(private_request)
    assert private_code == 0 and private["ok"]
    assert private["result"]["pending_action"]["instruction"] == instruction
    assert private["result"]["pending_action"]["return_contract"] == return_contract


def test_hosted_workspace_resolver_requires_current_pin_and_uses_only_local_mapping(live_host, tmp_path):
    from pmt.hosted_runtime import HostedLocalRuntime
    from pmt.workspace import canonical_workspace

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    runtime = HostedLocalRuntime(env["store_a"], lambda _run, pin: {
        "repository_id": env["repo"], "project_id": env["project"],
        "branch": pin["selected_ref"], "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"]},
        tmp_path / "local-runtime-spool")
    request = _host_request(env, "dispatch_execution", {"run_id": env["run"],
        "context_ref": env["context_ref"]})
    try:
        observation = runtime.observe(request)
    except PmtError as error:
        pytest.fail(f"Host/WorkspaceResolver prepare failed: {error.code} {error.details}")
    assert observation["status"] == "not_dispatched"
    assert observation["run_id"] == env["run"]
    assert canonical_workspace(env["repo"], "main") == env["canonical"]
    assert str(env["checkout"]) not in json.dumps(observation)
    assert not (tmp_path / "local-runtime-spool" / env["run"] / "dispatch.json").exists()

    with (env["checkout"] / Path(*env["relative"].split("/"))).open("a", encoding="utf-8") as stream:
        stream.write("\n")
    subprocess.run(["git", "add", "-f", "--", env["relative"]], cwd=env["checkout"], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-c", "user.email=fixture@example.invalid", "-c", "user.name=PMT Hosted Fixture",
                    "commit", "-m", "source changed after Host authorization"], cwd=env["checkout"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with pytest.raises(PmtError) as stale:
        runtime.observe(request)
    assert stale.value.code == "source_conflict"
    assert not (tmp_path / "local-runtime-spool" / env["run"] / "dispatch.json").exists()


def test_hosted_execution_controller_uses_fixture_local_process_and_hash_bound_stop_receipt(live_host, tmp_path):
    from pmt.efficiency.control import execute_with_ports
    from pmt.hosted_runtime import HostedLocalRuntime
    from pmt.pending import PendingOutbox
    from pmt.runners import service as runner_service

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    reuse_ref = _host_f6_reuse(env)
    fake_report = {"summary": "fixture runner completed; no test claim",
        "choices": [], "criteria_results": [{"criterion_id": item["id"], "outcome": "not_run",
            "reason": "isolated fixture process; no model verification", "evidence_refs": []}
            for item in env["criteria"]], "tests": [], "evidence_refs": [], "unresolved_items": []}
    fixture_event = {"type": "item.completed", "item": {"type": "agent_message",
        "text": json.dumps(fake_report, separators=(",", ":"))}}
    launches = []
    def fixture_launcher(config_path, prompt):
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        config.update(contract_fixture=True, fixture_process=True,
            fixture_stdout=json.dumps(fixture_event, separators=(",", ":")) + "\n",
        fixture_stderr="", fixture_delay=6.0)
        runner_service._write_private_json(Path(config_path), config)
        launches.append(hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        return runner_service._launch_helper(Path(config_path), prompt)

    now = [datetime(2026, 10, 2, 12, 0, 0, tzinfo=timezone.utc)]
    pending = PendingOutbox(tmp_path / "pending-local", namespace_id=env["headers"]["x-pmt-namespace"],
        actor=env["actor"], device_id=env["headers"]["x-pmt-device"],
        environment_id=env["headers"]["x-pmt-environment"], session_id=env["session_a"])
    runtime = HostedLocalRuntime(env["store_a"], lambda _run, pin: {
        "repository_id": env["repo"], "project_id": env["project"],
        "branch": pin["selected_ref"], "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"]},
        tmp_path / "local-runtime-spool", process_launcher=fixture_launcher, pending_outbox=pending)
    diagnostic_events = []
    class DiagnosticSink:
        def emit(self, event_name, **fields):
            diagnostic_events.append((event_name, fields))
    diagnostics = DiagnosticSink()
    repository = HostedControlStateRepository(env["store_a"])
    request = _host_request(env, "advance_execution_control", {"run_id": env["run"],
        "context_ref": env["context_ref"], "reuse_body_ref": reuse_ref})
    first, first_code = execute_with_ports(request, state_port=env["store_a"], local_runtime=runtime,
        control_state_repository=repository, clock=lambda: now[0], diagnostics=diagnostics)
    assert first_code == 0 and first["ok"], first.get("error")
    assert first["result"]["action"]["kind"] == "wait"
    assert len(launches) == 1
    assert str(env["checkout"]) not in canonical_json(first)

    time.sleep(9.0)
    now[0] += timedelta(seconds=60)
    runtime_observation = runtime.observe(_host_request(env, "dispatch_execution", {
        "run_id": env["run"], "context_ref": env["context_ref"]}))
    assert runtime_observation["status"] == "terminal", runtime_observation
    followup = _host_request(env, "advance_execution_control", {"run_id": env["run"],
        "context_ref": env["context_ref"], "reuse_body_ref": reuse_ref})
    final, final_code = execute_with_ports(followup, state_port=env["store_a"], local_runtime=runtime,
        control_state_repository=repository, clock=lambda: now[0], diagnostics=diagnostics)
    assert final_code == 0 and final["ok"], (final.get("error"), diagnostic_events)
    assert len(launches) == 1, "a second controller turn must not launch a second local process"
    run_result, result_code = env["store_a"].execute(_host_request(env, "read_execution",
        {"run_id": env["run"]}))
    assert result_code == 0 and run_result["ok"]
    run = run_result["result"]["run"]
    assert run["state"] == "review_pending" and bool(run["stop_confirmed"]) is True, final["result"]
    receipt = run["result"]
    assert all(item["outcome"] == "not_run" for item in receipt["criteria_results"])
    assert receipt["runtime_receipt_ref"].startswith("local-runner-receipt:" + env["run"] + ":")
    assert receipt["runtime_receipt_sha256"] == receipt["runtime_receipt_ref"].rsplit(":", 1)[1]
    assert str(env["checkout"]) not in canonical_json(run)
    assert "supervisor_pid" not in canonical_json(run)

    local_ref = receipt["runtime_receipt_ref"]
    manifest_path = tmp_path / "local-runtime-spool" / env["run"] / "receipt_manifest.json"
    assert manifest_path.exists()
    manifest_bytes = manifest_path.read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == receipt["runtime_receipt_sha256"]
    manifest = json.loads(manifest_bytes)
    assert manifest["receipt"]["stop_confirmed"] is True
    assert manifest["owner"]["session_id"] == env["session_a"]
    assert manifest["source_hash"] == env["pin"].source_hash
    recovered = runtime.read_terminal_receipt(request, env["context_ref"], local_ref)
    assert hashlib.sha256(recovered["manifest_bytes"]).hexdigest() == receipt["runtime_receipt_sha256"]
    assert hashlib.sha256(recovered["receipt_bytes"]).hexdigest() == recovered["receipt_sha256"]
    assert hashlib.sha256(recovered["output_bytes"]).hexdigest() == recovered["output_sha256"]
    remote_port = runtime.state_port
    runtime.state_port = object()  # Capture must use only the already attached local spool.
    offline = runtime.capture_terminal_receipt_offline(env["run"], env["context_ref"],
        env["pin"].source_hash, {"actor": env["actor"], "session_id": env["session_a"],
        "device_id": env["headers"]["x-pmt-device"],
        "environment_id": env["headers"]["x-pmt-environment"],
        "namespace_id": env["headers"]["x-pmt-namespace"]}, local_ref)
    runtime.state_port = remote_port
    assert offline["manifest_bytes"] == recovered["manifest_bytes"]
    assert offline["receipt_bytes"] == recovered["receipt_bytes"]
    assert offline["output_bytes"] == recovered["output_bytes"]
    hosted_artifact = receipt["runtime_receipt_resource_ref"]
    downloaded = env["store_a"].read_resource(hosted_artifact["id"], session_id=env["session_a"],
        expected_sha256=hosted_artifact["sha256"], scope_id=env["project"])
    assert downloaded["content"] == recovered["manifest_bytes"]
    pending_rows = pending.list_pending()
    assert len(pending_rows) == 1 and pending_rows[0].state == "applied"
    # Model a later review/terminal transition, release the P2 lock, and make
    # the original checkout unavailable. An already committed response remains
    # discoverable from authenticated request and immutable source metadata.
    with env["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='succeeded',revision=revision+1 WHERE id=?", (env["run"],))
        conn.execute("UPDATE execution_jobs SET state='succeeded' WHERE id=?", (run["job_id"],))
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (env["run"],))
    env["checkout"].rename(tmp_path / "checkout-unavailable-after-review")
    prepare_again = runtime._prepare
    runtime._prepare = lambda *_: pytest.fail("an already committed result must be looked up before current checkout inspection")
    cached = runtime.reconcile_pending_result(_host_request(env, "reconcile_pending_result", {
        "run_id": env["run"], "context_ref": env["context_ref"],
        "source_hash": env["pin"].source_hash, "pending_request_id": pending_rows[0].request_id}))
    runtime._prepare = prepare_again
    assert cached[0]["ok"] and cached[0]["result"]["state"] in {"applied", "already_applied"}
    resource_upload_id = str(uuid.uuid5(uuid.UUID(env["run"]),
        "pmt-hosted-result:" + manifest["receipt_sha256"]))
    resource_receipt = pending.publish_staged_resource(resource_upload_id, env["store_a"],
        lambda immutable: runtime._pending_current_facts(request, env["run"], env["context_ref"], immutable))
    assert resource_receipt["state"] == "published"


def test_host_shutdown_allows_only_existing_terminal_spool_capture(live_host, tmp_path):
    from pmt.hosted_runtime import HostedLocalRuntime
    from pmt.pending import PendingOutbox
    from pmt.runners import service as runner_service

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    report = {"summary": "isolated stop receipt fixture", "choices": [],
        "criteria_results": [{"criterion_id": item["id"], "outcome": "not_run",
            "reason": "fixture output is not verification", "evidence_refs": []} for item in env["criteria"]],
        "tests": [], "evidence_refs": [], "unresolved_items": []}
    event = {"type": "item.completed", "item": {"type": "agent_message",
        "text": json.dumps(report, separators=(",", ":"))}}
    def fixture_launcher(config_path, prompt):
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        config.update(contract_fixture=True, fixture_process=True,
            fixture_stdout=json.dumps(event, separators=(",", ":")) + "\n",
            fixture_stderr="", fixture_delay=2.0)
        runner_service._write_private_json(Path(config_path), config)
        return runner_service._launch_helper(Path(config_path), prompt)
    spool_root = tmp_path / "offline-local-spool"
    pending = PendingOutbox(tmp_path / "offline-pending", namespace_id=env["headers"]["x-pmt-namespace"],
        actor=env["actor"], device_id=env["headers"]["x-pmt-device"],
        environment_id=env["headers"]["x-pmt-environment"], session_id=env["session_a"])
    runtime = HostedLocalRuntime(env["store_a"], lambda _run, pin: {
        "repository_id": env["repo"], "project_id": env["project"],
        "branch": pin["selected_ref"], "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"]},
        spool_root, process_launcher=fixture_launcher, pending_outbox=pending)
    dispatch_id = new_id()
    request = _host_request(env, "dispatch_execution", {"run_id": env["run"],
        "context_ref": env["context_ref"]}, request_id=dispatch_id)
    launched, code = runtime.dispatch(request)
    assert code == 0 and launched["ok"], launched.get("error")
    assert launched["result"]["state"] == "running"
    spool = spool_root / env["run"]
    assert not (spool / "receipt_manifest.json").exists()
    env["process"].terminate()
    env["process"].wait(timeout=10)
    time.sleep(3.0)

    with closing(env["db"].connect()) as conn:
        before = dict(conn.execute("SELECT state,stop_confirmed,revision FROM execution_runs WHERE id=?",
                                   (env["run"],)).fetchone())
    offline_ref = "local-runner-dispatch:" + env["run"] + ":" + dispatch_id
    owner = {"actor": env["actor"], "session_id": env["session_a"],
        "device_id": env["headers"]["x-pmt-device"],
        "environment_id": env["headers"]["x-pmt-environment"],
        "namespace_id": env["headers"]["x-pmt-namespace"]}
    runtime.state_port = object()
    offline_result = runtime.capture_terminal_receipt_offline(env["run"], env["context_ref"],
        env["pin"].source_hash, owner, offline_ref)
    pending_capture_result = runtime.capture_pending_terminal_receipt(_host_request(env,
        "capture_pending_terminal_receipt", {"run_id": env["run"], "context_ref": env["context_ref"],
            "source_hash": env["pin"].source_hash, "dispatch_ref": offline_ref}))
    assert offline_result["runtime_receipt_ref"].startswith("local-runner-receipt:" + env["run"] + ":")
    assert hashlib.sha256(offline_result["manifest_bytes"]).hexdigest() == offline_result["runtime_receipt_sha256"]
    assert hashlib.sha256(offline_result["receipt_bytes"]).hexdigest() == offline_result["receipt_sha256"]
    assert hashlib.sha256(offline_result["output_bytes"]).hexdigest() == offline_result["output_sha256"]
    assert offline_result["manifest"]["receipt"]["stop_confirmed"] is True
    assert pending_capture_result["state"] == "staged"
    assert pending_capture_result["runtime_receipt_ref"] == offline_result["runtime_receipt_ref"]
    assert (spool / "receipt_manifest.json").read_bytes() == offline_result["manifest_bytes"]
    with closing(env["db"].connect()) as conn:
        after = dict(conn.execute("SELECT state,stop_confirmed,revision FROM execution_runs WHERE id=?",
                                  (env["run"],)).fetchone())
    assert after == before and before["state"] == "running" and not bool(before["stop_confirmed"])


def test_hosted_native_group_action_reloads_host_batch_and_every_f5_context(live_host, tmp_path):
    from pmt.hosted_runtime import HostedLocalRuntime
    from pmt.util import utc_now

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    route = {"agent": "codex", "provider": "fixture-provider", "model": "fixture-native",
        "mode": "native", "adapter_kind": "native", "auth_state": "authenticated",
        "actual_support": "verified_supported", "capability_ref": "fixture-native-v1"}
    now = utc_now()
    with env["db"].write() as conn:
        parent = conn.execute("SELECT * FROM execution_runs WHERE id=?", (env["run"],)).fetchone()
        parent_intent = json.loads(parent["intent_json"])
        scopes = json.loads(parent["scopes_json"])
        child_scopes = [{"kind": "resource", "workspace": env["canonical"],
            "resource": "hosted-group-child:" + new_id()}]
        parent_intent["route"] = route
        conn.execute("UPDATE execution_runs SET state='queued',route_json=?,intent_json=? WHERE id=?",
            (canonical_json(route), canonical_json(parent_intent), env["run"]))
        conn.execute("UPDATE execution_jobs SET state='queued' WHERE id=?", (parent["job_id"],))
        conn.execute("DELETE FROM scope_locks WHERE run_id=?", (env["run"],))
        spec = conn.execute("SELECT * FROM step_specs WHERE step_id=?", (env["step"],)).fetchone()
        child_step, child_job, child_run = new_id(), new_id(), new_id()
        child_body = {"directive_id": env["directive_resource"]["artifact_id"],
            "directive_version": 1, "invalidated": False, "kind_tag": "fixture",
            "workspace": env["canonical"], "criteria": env["criteria"]}
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
            "VALUES(?, 'step', ?, ?, ?, 'InProgress', ?, 1, ?, ?)",
            (child_step, env["project"], env["item"], "Hosted group child", canonical_json(child_body), now, now))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,"
                "plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (child_step, spec["directive_id"], spec["directive_version"],
                spec["requirements_version"], spec["plan_version"], spec["plan_id"], spec["role"],
                spec["product_stage"], env["canonical"], canonical_json(child_scopes), spec["criteria_json"],
                spec["dependencies_json"], now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) "
            "VALUES(?,?,'queued',?,?,?)", (child_job, child_step, canonical_json({"max_retries": 2}), now, now))
        child_intent = dict(parent_intent)
        child_intent.update(job_id=child_job, run_id=child_run, step_id=child_step,
            task={"task_id": env["item"], "step_id": child_step}, criteria=env["criteria"],
            scopes=child_scopes)
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,"
            "directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) "
            "VALUES(?,?,?,1,'queued',1,?,1,?,?,?,?,?,?)", (child_run, child_job, child_step,
                env["session_a"], env["canonical"], canonical_json(child_scopes), canonical_json(route),
                canonical_json(child_intent), now, now))
        parent_revision = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (env["run"],)).fetchone()[0]

    prepare = _host_request(env, "prepare_step_batch", {"run_refs": [
        {"run_id": env["run"], "expected_run_revision": parent_revision},
        {"run_id": child_run, "expected_run_revision": 1}],
        "workspace": env["canonical"], "repository_id": env["repo"],
        "relative_graph_path": env["relative"], "expected_source": env["pin"].to_dict(),
        "event_id": new_id()})
    prepared, code = env["store_a"].execute(prepare)
    assert code == 0 and prepared["ok"], prepared.get("error")
    batch_id = (prepared["result"].get("batch_ref") or {}).get("id")
    if not batch_id:
        batch_id = prepared["result"].get("batch_id") or prepared["result"].get("id")
    assert isinstance(batch_id, str), prepared["result"]
    runtime = HostedLocalRuntime(env["store_a"], lambda *_: {}, tmp_path / "host-group-spool")
    parent_run, _ = runtime._run(prepare, env["run"])
    assert parent_run.get("intent", {}).get("batch_ref") == batch_id, parent_run.get("intent")
    request = _host_request(env, "read_step_batch", {"batch_ref": batch_id,
        "parent_run_id": env["run"], "expected_run_revision": parent_run["revision"]})
    batch_wire, code = env["store_a"].execute(request)
    assert code == 0 and batch_wire["ok"], batch_wire.get("error")
    batch = batch_wire["result"]
    assert batch["execution_enabled"] is True
    assert len(batch["members"]) == 2 and all(m["context_current"] and m["directive_current"]
        for m in batch["members"])
    from pmt.efficiency.control import execute_with_ports
    env["common"] = dict(env["common"], expected_run_revision=parent_run["revision"])
    reuse_ref = _host_f6_reuse(env)
    runtime = HostedLocalRuntime(env["store_a"], lambda _run, pin: {
        "repository_id": env["repo"], "project_id": env["project"],
        "branch": pin["selected_ref"], "branch_key_sha256": env["branch_key_sha256"],
        "local_root": str(env["checkout"]), "relative_graph_path": env["relative"]},
        tmp_path / "host-group-spool")
    repository = HostedControlStateRepository(env["store_a"])
    advance = _host_request(env, "advance_execution_control", {"run_id": env["run"],
        "context_ref": batch["members"][0]["context_ref"], "reuse_body_ref": reuse_ref})
    action_response, action_code = execute_with_ports(advance, state_port=env["store_a"],
        local_runtime=runtime, control_state_repository=repository)
    assert action_code == 0 and action_response["ok"], action_response.get("error")
    action = action_response["result"]["action"]
    assert action["kind"] == "main-native-call"
    member_count = len(action["members"])
    assert member_count == 2 and action["batch_ref"] == batch_id
    assert action["batch_report_schema"] == "pmt-batch-report-v1"
    assert action["source_hash"] == env["pin"].source_hash and action["physical_slots"] == 1
    assert action["scope_union_sha256"] == batch["scope_union_sha256"]
    assert action["group_context_refs"] == [member["context_ref"] for member in action["members"]]
    assert [member["run_id"] for member in action["members"]] == [env["run"], child_run]
    prompt = json.loads(action["group_prompt"])
    assert [member["run_id"] for member in prompt["members"]] == [env["run"], child_run]
    assert action["group_prompt_sha256"] == hashlib.sha256(action["group_prompt"].encode()).hexdigest()
    replay, replay_code = execute_with_ports(advance, state_port=env["store_a"],
        local_runtime=runtime, control_state_repository=repository)
    assert replay_code == 0 and replay["ok"]
    assert replay["result"]["action"]["action_nonce"] == action["action_nonce"]

    fixture_handle_id = "fixture-native-handle-" + new_id()
    fixture_process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    ack_request = _host_request(env, "acknowledge_execution_action", {
        "run_id": env["run"], "action_nonce": action["action_nonce"],
        "control_ref": action_response["result"]["control_ref"],
        "expected_run_revision": action["expected_run_revision"], "outcome": "started",
        "handle_ref": {"kind": "native_handle", "id": fixture_handle_id,
            "provider_ref": "fixture-local-native-action"}})
    ack, ack_code = execute_with_ports(ack_request, state_port=env["store_a"],
        local_runtime=runtime, control_state_repository=repository)
    assert ack_code == 0 and ack["ok"], ack.get("error")
    assert ack["result"]["acknowledged"] is True and ack["result"]["run_state"] == "running"
    replay_ack, replay_ack_code = execute_with_ports(ack_request, state_port=env["store_a"],
        local_runtime=runtime, control_state_repository=repository)
    assert replay_ack_code == 0 and replay_ack["ok"]
    assert replay_ack["result"]["control_ref"] == ack["result"]["control_ref"]
    changed_ack = json.loads(canonical_json(ack_request))
    changed_ack["request_id"] = new_id()
    changed_ack["payload"]["handle_ref"]["id"] = "different-fixture-handle"
    conflicting_ack, conflict_code = execute_with_ports(changed_ack, state_port=env["store_a"],
        local_runtime=runtime, control_state_repository=repository)
    assert conflict_code == 3 and not conflicting_ack["ok"]
    assert conflicting_ack["error"]["code"] == "control_action_conflict"
    assert fixture_process.wait(timeout=5) == 0

    parent_run, _ = runtime._run(ack_request, env["run"])
    bind, bind_code = env["store_a"].execute(_host_request(env, "bind_step_batch", {
        "batch_ref": batch_id, "parent_run_id": env["run"], "event_id": new_id()}))
    assert bind_code == 0 and bind["ok"], bind.get("error")
    assert bind["result"]["status"] == "running"
    stop_artifact = env["store_a"].publish_resource({"request_id": new_id(),
        "scope_id": env["project"], "purpose": "result"}, canonical_json({
            "fixture_only": True, "stop_confirmed": True, "run_id": env["run"],
            "handle_ref": fixture_handle_id, "exit_code": 0}).encode(),
        session_id=env["session_a"])["artifact_ref"]
    report_steps = [{"step_id": member["step_id"], "run_id": member["run_id"],
        "directive_sha256": member["directive_sha256"], "context_ref": member["context_ref"],
        "summary": "Local fixture process only; no model or tests were run.", "choices": [],
        "criteria_results": [{"criterion_id": criterion["id"], "outcome": "not_run",
            "reason": "fixture process did not perform model verification", "evidence_refs": []}
            for criterion in member["criteria"]], "tests": [], "evidence_refs": [],
        "unresolved_items": []} for member in batch["members"]]
    report_resource = env["store_a"].publish_resource({"request_id": new_id(),
        "scope_id": env["project"], "purpose": "result"}, canonical_json({
            "schema": "pmt-batch-report-v1", "batch_id": batch_id,
            "steps": report_steps}).encode(), session_id=env["session_a"])["artifact_ref"]
    parent_result = {"directive_version": 1, "actual_route": parent_run["route"],
        "summary": "Fixture-only native handle lifecycle; no model was invoked.",
        "criteria_results": [{"criterion_id": item["id"], "outcome": "not_run",
            "reason": "fixture process did not perform model verification", "evidence_refs": []}
            for item in env["criteria"]], "receipt_ref": stop_artifact["id"],
        "evidence_refs": [stop_artifact["id"], report_resource["id"]],
        "stop_confirmed": True, "stop_evidence_refs": [stop_artifact["id"]],
        "runner_observation": {"exit_code": 0, "fixture": True},
        "batch_ref": batch_id, "batch_report_ref": report_resource["id"],
        "batch_report_sha256": report_resource["sha256"]}
    submitted, submit_code = env["store_a"].execute(_host_request(env, "submit_execution_result", {
        "run_id": env["run"], "expected_run_revision": parent_run["revision"],
        "result": parent_result}))
    assert submit_code == 0 and submitted["ok"], submitted.get("error")
    after_submit, _ = runtime._run(ack_request, env["run"])
    collected, collect_code = env["store_a"].execute(_host_request(env, "collect_step_batch", {
        "batch_ref": batch_id, "parent_run_id": env["run"],
        "expected_run_revision": after_submit["revision"], "event_id": new_id()}))
    assert collect_code == 0 and collected["ok"], collected.get("error")
    assert collected["result"]["status"] == "review_pending"
    assert len(collected["result"]["children"]) == 2
    assert all(child.get("state") in {"review_pending", "not_run", "failed"}
        for child in collected["result"]["children"])
    with closing(env["db"].connect()) as conn:
        locks = conn.execute("SELECT run_id,owner_session FROM scope_locks WHERE run_id=?",
            (env["run"],)).fetchall()
    assert locks and all(item["owner_session"] == env["session_a"] for item in locks)
