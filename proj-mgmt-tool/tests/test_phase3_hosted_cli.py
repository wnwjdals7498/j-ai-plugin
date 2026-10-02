"""Production CLI selector against an isolated actual HTTPS Host."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from contextlib import closing

import pytest

pytest_plugins = ["test_phase3_host_network"]

from pmt.storage_config import configure_storage
from pmt.util import canonical_json
from pmt.errors import PmtError
from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout


def _configure(env, config):
    config.mkdir()
    (config / "profile.json").write_text(canonical_json({
        "environment_id": env["headers"]["x-pmt-environment"]}), encoding="utf-8")
    return configure_storage(config, {"mode": "hosted", "expected_config_sha256": None,
        "endpoint": env["base"], "credential_env": "PMT_CLIENT_A_TOKEN",
        "device_id": env["headers"]["x-pmt-device"], "namespace_id": env["headers"]["x-pmt-namespace"],
        "ca_file": str(env["cert"]), "workspace_mappings": [{
            "repository_id": env["repo"], "project_id": env["project"], "branch": "main",
            "branch_key_sha256": hashlib.sha256(b"main").hexdigest(),
            "local_root": str(env["checkout"]), "relative_graph_path": env["relative"]}]})


def _cli(config, data, body, *arguments):
    return subprocess.run([sys.executable, "-m", "pmt", "--data-root", str(data),
        "--config-root", str(config), *arguments], input=canonical_json(body), capture_output=True,
        text=True, encoding="utf-8", env=dict(os.environ,
        PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src")), timeout=30)


def test_cli_default_hosted_runtime_observes_local_checkout_without_local_db(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "cli-config", tmp_path / "cli-data"
    configured = _configure(env, config)
    assert configured["mode"] == "hosted"
    request = _host_request(env, "poll_execution", {"run_id": env["run"],
        "context_ref": env["context_ref"]})
    source_root = Path(__file__).resolve().parents[1] / "src"
    process_env = dict(os.environ, PYTHONPATH=str(source_root))
    observed = subprocess.run([sys.executable, "-m", "pmt", "--data-root", str(data),
        "--config-root", str(config)], input=canonical_json(request), capture_output=True,
        text=True, encoding="utf-8", env=process_env, timeout=30)
    assert observed.returncode == 0, observed.stdout + observed.stderr
    response = json.loads(observed.stdout)
    assert response["ok"] and response["result"]["status"] == "not_dispatched"
    assert response["result"]["run_id"] == env["run"]
    assert not list(data.rglob("*.sqlite3")), "Hosted effects must not instantiate a local primary"
    assert str(env["checkout"]) not in observed.stdout


def test_execution_wire_rejects_private_process_settings_on_client_and_host(live_host):
    env = live_host
    private_value = "isolated-private-fixture-do-not-save"
    cases = [("enqueue_execution", {"step_id": env["step"], "route": {
        "agent": "codex", "provider": "fixture", "model": "fixture", "mode": "cli",
        "command": ["codex", private_value]}}),
        ("attach_execution_handle", {"run_id": env["run"], "expected_run_revision": 3,
         "handle": {"id": "opaque-fixture", "env": {"TOKEN": private_value}}})]
    for operation, payload in cases:
        request = _host_request(env, operation, payload)
        with pytest.raises(PmtError) as rejected:
            env["store_a"].execute(request)
        assert rejected.value.code == "host_execution_metadata_invalid"
        # Bypass the SDK guard to independently test the actual server boundary.
        result, code = env["store_a"]._operation(request)
        assert code == 2 and result["error"]["code"] == "host_execution_metadata_invalid"
        assert private_value not in canonical_json(result)
    with closing(env["db"].connect()) as conn:
        assert private_value not in "\n".join(conn.iterdump())


def test_cli_preserves_offline_terminal_receipt_then_reconciles_once(live_host, tmp_path):
    from pmt.storage_config import _read_profile, _default_hosted_runtime
    from pmt.runners import service as runners
    from pmt.util import new_id
    from test_phase3_pending_http import _start_again

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "pending-cli-config", tmp_path / "pending-cli-data"
    _configure(env, config)
    profile, _ = _read_profile(config)
    request = _host_request(env, "dispatch_execution", {
        "run_id": env["run"], "context_ref": env["context_ref"]})
    runtime = _default_hosted_runtime(profile=profile, state_port=env["store_a"], request=request,
                                      data_root=data, environ=os.environ)
    report = {"summary": "local fixture completed offline", "choices": [], "tests": [],
        "evidence_refs": [], "unresolved_items": [], "criteria_results": [{
            "criterion_id": item["id"], "outcome": "not_run", "reason": "fixture process only",
            "evidence_refs": []} for item in env["criteria"]]}
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": canonical_json(report)}}
    def launch(config_path, prompt):
        value = json.loads(Path(config_path).read_text(encoding="utf-8"))
        value.update(contract_fixture=True, fixture_process=True, fixture_delay=2.0,
                     fixture_stdout=canonical_json(event) + "\n", fixture_stderr="")
        runners._write_private_json(Path(config_path), value)
        return runners._launch_helper(Path(config_path), prompt)
    runtime.process_launcher = launch
    started, code = runtime.dispatch(request)
    assert code == 0 and started["ok"], started
    env["process"].terminate()
    env["process"].wait(timeout=10)
    receipt_path = runtime.spool_root / env["run"] / "receipt.json"
    deadline = time.monotonic() + 15
    while not receipt_path.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert receipt_path.exists(), "Actual local fixture must physically finish after Host stops"
    body = {"request_id": new_id(), "session_id": env["session_a"], "scope_id": env["project"],
        "run_id": env["run"], "context_ref": env["context_ref"], "source_hash": env["pin"].source_hash,
        "dispatch_ref": "local-runner-dispatch:" + env["run"] + ":" + request["request_id"]}
    captured = _cli(config, data, body, "pending", "capture")
    assert captured.returncode == 0, captured.stdout + captured.stderr
    capture_reply = json.loads(captured.stdout)
    assert capture_reply["ok"] and capture_reply["result"]["state"] == "staged"
    status = _cli(config, data, {"request_id": new_id(), "session_id": env["session_a"]},
                   "pending", "status")
    assert status.returncode == 0, status.stdout + status.stderr
    status_result = json.loads(status.stdout)["result"]
    assert status_result["pending_resources"][0]["state"] == "staged"
    assert "relative_path" not in canonical_json(status_result)
    with closing(env["db"].connect()) as conn:
        assert conn.execute("SELECT state FROM execution_runs WHERE id=?", (env["run"],)).fetchone()[0] == "running"
    restarted = _start_again(env)
    try:
        body["request_id"] = new_id()
        reconciled = _cli(config, data, body, "pending", "reconcile")
        assert reconciled.returncode == 0, reconciled.stdout + reconciled.stderr
        result = json.loads(reconciled.stdout)["result"]
        assert result["state"] in {"applied", "already_applied"}, result
        original_id = result["request_id"]
        body["request_id"] = new_id()
        body["pending_request_id"] = original_id
        repeated = _cli(config, data, body, "pending", "reconcile")
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        repeated_result = json.loads(repeated.stdout)["result"]
        assert repeated_result["state"] in {"applied", "already_applied"}
        assert repeated_result["request_id"] == original_id
        with closing(env["db"].connect()) as conn:
            assert conn.execute("SELECT COUNT(*) FROM requests WHERE request_id=?", (original_id,)).fetchone()[0] == 1
            row = conn.execute("SELECT state,stop_confirmed,result_json FROM execution_runs WHERE id=?", (env["run"],)).fetchone()
        assert row["state"] == "review_pending" and row["stop_confirmed"] == 1
        assert all(item["outcome"] == "not_run" for item in json.loads(row["result_json"])["criteria_results"])
        assert not (data / "pmt.sqlite3").exists()
    finally:
        restarted.terminate()
        restarted.wait(timeout=10)
