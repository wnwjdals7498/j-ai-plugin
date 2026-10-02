from __future__ import annotations

import base64
import copy
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen

import pytest

from pmt.errors import PmtError
from pmt.pending import PendingOutbox, preflight_new_shared_write
from pmt.efficiency.source import inspect_graph_source
from pmt.util import canonical_json, fingerprint, new_id, utc_now
from pmt.workspace import canonical_workspace
from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout
from test_phase3_host_data import _publish_source
from test_phase3_host_network import _request, live_host


def _actual_facts(env, store, session):
    identity = {"namespace_id": env["app"].auth.namespace_id, "actor": env["actor"],
        "device_id": store.device_id, "environment_id": store.environment_id, "session_id": session}
    compatibility = store.check_compatibility()
    run_req = _request(env, store, session, "read_execution", {"run_id": env["run"]}, scope_id=env["project"])
    run_reply, code = store.execute(run_req)
    assert code == 0 and run_reply["ok"], run_reply.get("error")
    run = run_reply["result"]["run"]
    source_req = _request(env, store, session, "read_source_snapshot",
        {"project_id": env["project"], "repository_id": env["repo"],
         "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
         "run_id": env["run"], "expected_run_revision": run["revision"],
         "expected_source": env["pin"].to_dict()}, scope_id=env["project"])
    source_reply, code = store.execute(source_req)
    assert code == 0 and source_reply["ok"], source_reply.get("error")
    pin = source_reply["result"]["source_pin"]
    owner = {"actor": identity["actor"], "device_id": identity["device_id"], "session_id": session}
    auth = {"schema": "pmt-host-auth-facts-v1", "ref": "compatibility+registered-session",
        "namespace_id": identity["namespace_id"], "actor": identity["actor"], "device_id": identity["device_id"],
        "environment_id": identity["environment_id"], "session_id": identity["session_id"],
        "scope_id": env["project"], "scopes": compatibility["scopes"], "permissions": compatibility["permissions"]}
    auth["sha256"] = fingerprint(auth)
    run_ref = {"schema": "pmt-host-run-read-v1", "ref": "read_execution:" + run["id"],
        "run_id": run["id"], "scope_id": env["project"], "revision": run["revision"],
        "state": run["state"], "owner": owner, "workspace": run["workspace"], "actual_route": run["route"]}
    run_ref["sha256"] = fingerprint(run_ref)
    return {"schema": "pmt-pending-current-facts-v1", **identity, "scope_id": env["project"],
        "source_fingerprint": pin["source_hash"], "source_ref": {"schema": "pmt-source-pin-ref-v1",
            "ref": "read_source_snapshot:" + source_req["request_id"], "pin": pin},
        "authorization_ref": auth, "run_ref": run_ref, "run_id": run["id"],
        "run_revision": run["revision"], "run_state": run["state"], "run_owner": owner}


def _start_again(env):
    claim_env_name = "PMT_TEST_HOST_CLAIM_KEY"
    child_env = os.environ.copy()
    child_env[claim_env_name] = base64.b64encode(b"isolated-network-fixture-claim-key-32bytes").decode("ascii")
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + child_env.get("PYTHONPATH", "")
    process = subprocess.Popen(env["process"].args, cwd=Path(__file__).resolve().parents[1], env=child_env,
        stdout=env["stdout"], stderr=env["stderr"])
    import ssl
    context = ssl.create_default_context(cafile=str(env["cert"]))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with urlopen(env["base"] + "/health", context=context, timeout=0.5) as response:
                if response.status == 200:
                    return process
        except Exception:
            time.sleep(0.1)
    process.terminate()
    process.wait(timeout=5)
    pytest.fail("loopback HTTPS Host did not restart with the same isolated database")


def test_loopback_disconnect_keeps_only_existing_resource_and_replays_same_upload_after_reconnect(tmp_path, live_host):
    env = live_host
    store = env["store_a"]
    source = _publish_source(env)
    assert source["source_pin"]["source_hash"] == env["pin"].source_hash
    facts = _actual_facts(env, store, env["session_a"])
    identity = {key: facts[key] for key in ("namespace_id", "actor", "device_id", "environment_id", "session_id")}
    outbox = PendingOutbox(tmp_path / "client-state", **identity)
    payload = b"pre-existing sanitized result resource fixture"
    upload_id, result_ref = new_id(), "local-result-fixture:already-created"
    staged = outbox.stage_resource(upload_request_id=upload_id, scope_id=env["project"], purpose="result",
        source_fingerprint=facts["source_fingerprint"], output_ref=result_ref,
        output_reader=lambda ref: payload if ref == result_ref else None)
    assert staged.state == "staged"

    env["process"].terminate()
    env["process"].wait(timeout=10)
    with pytest.raises(PmtError) as blocked:
        preflight_new_shared_write(store, "claim_task", scope_id=env["project"])
    assert blocked.value.code == "hosted_write_blocked"
    pending = outbox.publish_staged_resource(upload_id, store, lambda _immutable: _actual_facts(env, store, env["session_a"]))
    assert pending.state == "unknown"
    with closing(outbox._connect()) as conn:
        row = conn.execute("SELECT state,immutable_json FROM pending_resources WHERE upload_request_id=?", (upload_id,)).fetchone()
    assert row["state"] == "unknown"
    assert result_ref not in row["immutable_json"] or "local-result-fixture" in row["immutable_json"]
    stored = outbox.root / "resources" / staged.sha256
    assert hashlib.sha256(stored.read_bytes()).hexdigest() == staged.sha256
    with closing(env["db"].connect()) as conn:
        assert conn.execute("SELECT 1 FROM host_resource_journal WHERE request_id=?", (upload_id,)).fetchone() is None

    restarted = _start_again(env)
    try:
        published = outbox.publish_staged_resource(upload_id, store,
            lambda _immutable: _actual_facts(env, store, env["session_a"]))
        assert published["state"] == "published"
        assert published["artifact_ref"]["sha256"] == staged.sha256
        assert published["artifact_ref"]["scope_id"] == env["project"]
        assert published["receipt_ref"]["request_id"] == upload_id
        with closing(env["db"].connect()) as conn:
            rows = conn.execute("SELECT state,sha256,size_bytes FROM host_resource_journal WHERE request_id=?",
                                (upload_id,)).fetchall()
        assert len(rows) == 1 and rows[0]["state"] == "committed"
        assert rows[0]["sha256"] == staged.sha256 and rows[0]["size_bytes"] == len(payload)
    finally:
        restarted.terminate()
        restarted.wait(timeout=10)


def test_actual_local_fixture_result_lost_https_reply_is_resolved_without_second_write(tmp_path, live_host):
    from pmt.hosted_runtime import HostedLocalRuntime
    from pmt.runners import service as runner_service

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    store = env["store_a"]
    identity = {"namespace_id": env["app"].auth.namespace_id, "actor": env["actor"],
        "device_id": store.device_id, "environment_id": store.environment_id, "session_id": env["session_a"]}
    outbox = PendingOutbox(tmp_path / "result-client-state", **identity)
    spool_root = tmp_path / "result-runtime-spool"
    report = {"summary": "fixture process completed; no verification claim", "choices": [],
        "criteria_results": [{"criterion_id": item["id"], "outcome": "not_run",
            "reason": "fixture process; model quality not evaluated", "evidence_refs": []} for item in env["criteria"]],
        "tests": [], "evidence_refs": [], "unresolved_items": []}
    event = {"type": "item.completed", "item": {"type": "agent_message",
        "text": json.dumps(report, separators=(",", ":"))}}
    launches = []

    def fixture_launcher(config_path, prompt):
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
        config.update(contract_fixture=True, fixture_process=True,
            fixture_stdout=json.dumps(event, separators=(",", ":")) + "\n",
            fixture_stderr="", fixture_delay=0.25)
        runner_service._write_private_json(Path(config_path), config)
        launches.append(hashlib.sha256(prompt.encode("utf-8")).hexdigest())
        return runner_service._launch_helper(Path(config_path), prompt)

    class DropResultResponseStore:
        def __init__(self, wrapped):
            self.wrapped = wrapped
            self.runtime = None
            self.dispatch_request = None
            self.pending_ref = None
            self.result_calls = 0

        def __getattr__(self, name):
            return getattr(self.wrapped, name)

        def execute(self, request):
            reply = self.wrapped.execute(request)
            if request.get("operation") != "submit_execution_result":
                return reply
            self.result_calls += 1
            envelope, exit_code = reply
            assert exit_code == 0 and envelope["ok"], envelope.get("error")
            result = request["payload"]["result"]
            receipt_ref = result["runtime_receipt_ref"]
            captured = self.runtime.read_terminal_receipt(self.dispatch_request,
                self.dispatch_request["payload"]["context_ref"], receipt_ref)
            self.pending_ref = outbox.enqueue_result(request,
                base_run_revision=request["payload"]["expected_run_revision"],
                source_fingerprint=captured["source_hash"], runtime_receipt_ref=receipt_ref,
                runtime_receipt_sha256=result["runtime_receipt_sha256"],
                receipt_reader=lambda ref: {key: captured[key] for key in
                    ("manifest_bytes", "receipt_bytes", "output_bytes")} if ref == receipt_ref else None,
                run_id=request["payload"]["run_id"])
            raise PmtError("remote_unavailable", "HTTPS result response was lost after Host commit", 3, True,
                           {"effect": "unknown"})

    runtime = HostedLocalRuntime(store, lambda _run, pin: {
        "repository_id": env["repo"], "project_id": env["project"], "branch": pin["selected_ref"],
        "branch_key_sha256": env["branch_key_sha256"], "local_root": str(env["checkout"]),
        "relative_graph_path": env["relative"]}, spool_root, process_launcher=fixture_launcher)
    dispatch = _host_request(env, "dispatch_execution", {"run_id": env["run"],
        "context_ref": env["context_ref"]})
    dispatched, dispatch_code = runtime.dispatch(dispatch)
    assert dispatch_code == 0 and dispatched["ok"], dispatched.get("error")
    deadline = time.monotonic() + 20
    observation = None
    while time.monotonic() < deadline:
        observation = runtime.observe(dispatch)
        if observation.get("status") == "terminal":
            break
        time.sleep(0.1)
    assert observation and observation["status"] == "terminal" and observation["stop_confirmed"] is True
    port = DropResultResponseStore(store)
    port.runtime = runtime
    port.dispatch_request = dispatch
    runtime.state_port = port
    result_reply, result_code = runtime.collect_terminal(dispatch, observation)
    assert result_code != 0 and not result_reply["ok"]
    assert port.pending_ref is not None and port.pending_ref.state == "pending", result_reply.get("error")
    assert port.result_calls == 1

    decision = outbox.reconcile(port.pending_ref.request_id, store,
        lambda _immutable: _actual_facts(env, store, env["session_a"]))
    assert decision.state == "already_applied"
    assert port.result_calls == 1, "Reconciliation must retrieve the committed Host receipt, not send a second result"
    with closing(env["db"].connect()) as conn:
        rows = conn.execute("SELECT request_fingerprint,exit_code FROM requests WHERE request_id=?",
                            (port.pending_ref.request_id,)).fetchall()
    assert len(rows) == 1 and rows[0]["exit_code"] == 0
    with closing(env["db"].connect()) as conn:
        run = conn.execute("SELECT state,stop_confirmed,result_json FROM execution_runs WHERE id=?", (env["run"],)).fetchone()
    assert run["state"] == "review_pending" and run["stop_confirmed"] == 1
    assert json.loads(run["result_json"])["criteria_results"][0]["outcome"] == "not_run"


def test_host_stopped_before_local_fixture_finishes_pending_result_reconnects_and_submits_once(tmp_path, live_host):
    from uuid import UUID, uuid5
    from pmt.hosted_runtime import HostedLocalRuntime, _operation
    from pmt.runners import service as runner_service

    env = _seed_hosted_git_checkout(live_host, tmp_path)
    store = env["store_a"]
    spool_root = tmp_path / "offline-finish-spool"
    report = {"summary": "fixture process finished offline; no verification claim", "choices": [],
        "criteria_results": [{"criterion_id": item["id"], "outcome": "not_run",
            "reason": "offline fixture; model quality not evaluated", "evidence_refs": []} for item in env["criteria"]],
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

    runtime = HostedLocalRuntime(store, lambda _run, pin: {
        "repository_id": env["repo"], "project_id": env["project"], "branch": pin["selected_ref"],
        "branch_key_sha256": env["branch_key_sha256"], "local_root": str(env["checkout"]),
        "relative_graph_path": env["relative"]}, spool_root, process_launcher=fixture_launcher)
    dispatch_id = new_id()
    dispatch = _host_request(env, "dispatch_execution", {"run_id": env["run"],
        "context_ref": env["context_ref"]}, request_id=dispatch_id)
    launched, code = runtime.dispatch(dispatch)
    assert code == 0 and launched["ok"], launched.get("error")
    owner = {"namespace_id": env["app"].auth.namespace_id, "actor": env["actor"],
        "device_id": store.device_id, "environment_id": store.environment_id, "session_id": env["session_a"]}
    pin_hash = env["pin"].source_hash
    offline_ref = "local-runner-dispatch:" + env["run"] + ":" + dispatch_id
    env["process"].terminate()
    env["process"].wait(timeout=10)
    spool = spool_root / env["run"]
    receipt = None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        receipt_path = spool / "receipt.json"
        if receipt_path.exists():
            try:
                receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                if receipt.get("state") in {"completed", "failed", "canceled"}:
                    break
            except (ValueError, OSError):
                pass
        time.sleep(0.1)
    assert isinstance(receipt, dict) and receipt.get("state") == "completed"
    with closing(env["db"].connect()) as conn:
        run_before = dict(conn.execute("SELECT state,stop_confirmed,revision FROM execution_runs WHERE id=?",
                                       (env["run"],)).fetchone())
    assert run_before["state"] == "running" and not run_before["stop_confirmed"]
    runtime.state_port = object()
    captured = runtime.capture_terminal_receipt_offline(env["run"], env["context_ref"], pin_hash, owner, offline_ref)
    assert captured["manifest"]["fixture"] is True
    assert captured["manifest"]["receipt"]["stop_confirmed"] is True
    assert hashlib.sha256(captured["receipt_bytes"]).hexdigest() == captured["receipt_sha256"]
    assert hashlib.sha256(captured["output_bytes"]).hexdigest() == captured["output_sha256"]
    assert hashlib.sha256(captured["manifest_bytes"]).hexdigest() == captured["runtime_receipt_sha256"]
    with closing(env["db"].connect()) as conn:
        run_after_capture = dict(conn.execute("SELECT state,stop_confirmed,revision FROM execution_runs WHERE id=?",
                                              (env["run"],)).fetchone())
    assert run_after_capture == run_before

    outbox = PendingOutbox(tmp_path / "offline-result-client", **owner)
    raw_upload_id = str(uuid5(UUID(env["run"]), "pmt-hosted-result:" + captured["receipt_sha256"]))
    staged = outbox.stage_resource(upload_request_id=raw_upload_id, scope_id=env["project"], purpose="result",
        source_fingerprint=pin_hash, output_ref=captured["runtime_receipt_ref"],
        output_reader=lambda ref: captured["manifest_bytes"] if ref == captured["runtime_receipt_ref"] else None)
    assert staged.state == "staged"
    with pytest.raises(PmtError) as blocked:
        preflight_new_shared_write(store, "claim_task", scope_id=env["project"])
    assert blocked.value.code == "hosted_write_blocked"

    restarted = _start_again(env)
    try:
        facts = _actual_facts(env, store, env["session_a"])
        published = outbox.publish_staged_resource(raw_upload_id, store, lambda _item: facts)
        assert published["state"] == "published"
        assert published["artifact_ref"]["sha256"] == staged.sha256
        run_request = _host_request(env, "read_execution", {"run_id": env["run"]})
        run_reply, run_code = store.execute(run_request)
        assert run_code == 0 and run_reply["ok"], run_reply.get("error")
        run = run_reply["result"]["run"]
        safe_receipt = captured["manifest"]["receipt"]
        raw_output = json.loads(captured["output_bytes"].decode("utf-8")) if captured["output_bytes"] else {}
        model_report = raw_output.get("model_report") if isinstance(raw_output.get("model_report"), dict) else None
        from pmt.runners.service import _result_for
        result = _result_for(run, safe_receipt, model_report, published["artifact_ref"]["id"])
        result.update(runtime_receipt_ref=captured["runtime_receipt_ref"],
            runtime_receipt_sha256=captured["runtime_receipt_sha256"],
            runtime_supervisor_receipt_sha256=captured["receipt_sha256"],
            runtime_output_sha256=captured["output_sha256"],
            runtime_receipt_resource_ref=published["artifact_ref"])
        result_request = _operation(dispatch, "submit_execution_result", {"run_id": env["run"],
            "expected_run_revision": run["revision"], "result": result},
            suffix="submit-terminal:" + captured["receipt_sha256"])
        pending = outbox.enqueue_result(result_request, base_run_revision=run["revision"],
            source_fingerprint=pin_hash, runtime_receipt_ref=captured["runtime_receipt_ref"],
            runtime_receipt_sha256=captured["runtime_receipt_sha256"],
            receipt_reader=lambda ref: {key: captured[key] for key in
                ("manifest_bytes", "receipt_bytes", "output_bytes")} if ref == captured["runtime_receipt_ref"] else None,
            run_id=env["run"])
        assert pending.state == "pending"
        current_facts = _actual_facts(env, store, env["session_a"])
        applied = outbox.reconcile(pending.request_id, store, lambda _item: current_facts)
        assert applied.state == "applied"
        with closing(env["db"].connect()) as conn:
            stored = conn.execute("SELECT COUNT(*) FROM requests WHERE request_id=?", (pending.request_id,)).fetchone()[0]
            run_after = dict(conn.execute("SELECT state,stop_confirmed,result_json FROM execution_runs WHERE id=?",
                                          (env["run"],)).fetchone())
        assert stored == 1 and run_after["state"] == "review_pending" and run_after["stop_confirmed"] == 1
        assert json.loads(run_after["result_json"])["criteria_results"][0]["outcome"] == "not_run"
    finally:
        restarted.terminate()
        restarted.wait(timeout=10)


def test_actual_two_environments_keep_branch_contexts_separate_and_shared_main_scope_conflicts(tmp_path, live_host):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    store_a, store_b = env["store_a"], env["store_b"]
    pin_a = env["pin"]
    context_a = env["context_ref"]
    branch = "feature/f14-scope"
    graph_path = env["graph_path"]
    feature_graph = copy.deepcopy(env["graph"])
    feature_graph["graph_version"] += 1
    graph_path.write_text(canonical_json(feature_graph) + "\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(env["checkout"]), "switch", "-c", branch], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(env["checkout"]), "add", "-f", "--", env["relative"]], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    subprocess.run(["git", "-C", str(env["checkout"]), "commit", "-m", "feature branch source"], check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pin_b = inspect_graph_source(env["checkout"], graph_path, env["repo"], env["project"],
        graph_scope_id=env["project"])["source_pin"]
    workspace_a = canonical_workspace(env["repo"], pin_a.selected_ref)
    workspace_b = canonical_workspace(env["repo"], branch)
    assert workspace_a == env["canonical"] and workspace_b != workspace_a
    assert pin_b.selected_ref == branch and pin_b.source_hash != pin_a.source_hash

    item_b, step_b, job_b, run_b = new_id(), new_id(), new_id(), new_id()
    scope_b = [{"kind": "path", "workspace": workspace_b, "resource": env["relative"]}]
    route_b = {"agent": "codex", "provider": "fixture-provider", "model": "fixture-model",
        "mode": "cli", "adapter_kind": "cli", "auth_state": "authenticated",
        "actual_support": "verified_supported", "capability_ref": "fixture-feature-capability"}
    now = utc_now()
    with env["db"].write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
            "VALUES(?,'item',?,?,?,'InProgress','{}',1,?,?)",
            (item_b, env["project"], env["work"], "Feature branch item", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
            "VALUES(?,'step',?,?,?,'InProgress','{}',1,?,?)",
            (step_b, env["project"], item_b, "Feature branch step", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) "
            "VALUES(?,?,'running','{}',?,?)", (job_b, step_b, now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) "
            "VALUES(?,?,?,1,'running',3,?,1,?,?,?,'{}',?,?)",
            (run_b, job_b, step_b, env["session_b"], workspace_b, canonical_json(scope_b), canonical_json(route_b), now, now))
        conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) "
            "VALUES(?,?,?,?,?,?,?)", (new_id(), run_b, env["session_b"], "path", workspace_b, env["relative"], now))

    headers_b = {"authorization": "Bearer " + env["token_b"], "x-pmt-device": env["device_b"]["device_id"],
        "x-pmt-environment": env["environment_b"], "x-pmt-namespace": env["app"].auth.namespace_id,
        "x-pmt-session": env["session_b"]}
    env_b = dict(env) | {"actor": "network-client-b", "session": env["session_b"], "headers": headers_b,
        "item": item_b, "step": step_b, "job": job_b, "run": run_b, "canonical": workspace_b,
        "pin": pin_b, "graph": feature_graph}
    from test_phase3_host_network import _seed_private_directive
    _seed_private_directive(env_b)
    graph_bytes = canonical_json(feature_graph).encode("utf-8")
    graph_artifact = store_b.publish_resource({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "graph_snapshot"}, graph_bytes, session_id=env["session_b"])["artifact_ref"]
    publish = {"protocol_version": 1, "operation": "publish_source_snapshot", "request_id": new_id(),
        "actor": "network-client-b", "session_id": env["session_b"], "scope_id": env["project"],
        "source": {"product": "cli"}, "context_refs": [], "payload": {
            "project_id": env["project"], "repository_id": env["repo"],
            "canonical_workspace": workspace_b, "relative_graph_path": env["relative"],
            "run_id": run_b, "expected_run_revision": 3, "expected_source_revision": 0,
            "branch_key": branch, "source_pin": pin_b.to_dict(), "graph_resource_ref": graph_artifact}}
    published, code = store_b.execute(publish)
    assert code == 0 and published["ok"], published.get("error")
    assert published["result"]["source_pin"]["source_hash"] == pin_b.source_hash

    feature_common = {"project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": workspace_b, "relative_graph_path": env["relative"],
        "run_id": run_b, "expected_run_revision": 3, "expected_source": pin_b.to_dict()}
    rebuilt, code = store_b.execute({**publish, "operation": "rebuild_graph_index", "request_id": new_id(),
        "payload": feature_common})
    assert code == 0 and rebuilt["ok"], rebuilt.get("error")
    context_request = {**publish, "operation": "build_task_context", "request_id": new_id(),
        "payload": {**feature_common, "task_ref": {"task_id": item_b, "step_id": step_b, "run_id": run_b},
            "role": "lower", "workspace": workspace_b,
            "node_ids": [node["id"] for node in feature_graph["nodes"]],
            "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}}}
    built, code = store_b.execute(context_request)
    assert code == 0 and built["ok"], built.get("error")
    context_b = built["result"]["context_ref"]
    assert context_a["source_hash"] == pin_a.source_hash
    assert context_b["source_hash"] == pin_b.source_hash
    assert context_b["source_hash"] != context_a["source_hash"]
    assert context_b["id"] != context_a["id"]

    owner_a = {"namespace_id": env["app"].auth.namespace_id, "actor": env["actor"],
        "device_id": store_a.device_id, "environment_id": store_a.environment_id, "session_id": env["session_a"]}
    owner_b = {"namespace_id": env["app"].auth.namespace_id, "actor": "network-client-b",
        "device_id": store_b.device_id, "environment_id": store_b.environment_id, "session_id": env["session_b"]}
    local_a = PendingOutbox(tmp_path / "env-scoped-outbox", **owner_a)
    upload_id = new_id()
    content = b"already-created environment-A result fixture"
    local_a.stage_resource(upload_request_id=upload_id, scope_id=env["project"], purpose="result",
        source_fingerprint=pin_a.source_hash, output_ref="env-A-existing-result", output_reader=lambda _ref: content)
    local_b = PendingOutbox(tmp_path / "env-scoped-outbox", **owner_b)
    assert local_b.list_pending() == ()
    with pytest.raises(PmtError) as owner_denied:
        local_b.publish_staged_resource(upload_id, store_b,
            lambda _item: _actual_facts(env, store_b, env["session_b"]))
    assert owner_denied.value.code == "pending_owner_mismatch"

    run_reply, code = store_b.execute({**publish, "operation": "read_execution", "request_id": new_id(),
        "payload": {"run_id": run_b}})
    assert code == 0 and run_reply["ok"]
    assert {row["workspace"] for row in run_reply["result"]["scope_locks"]} == {workspace_b}

    # A second environment with a different checkout but the same main-branch
    # canonical scope cannot acquire the main graph resource already held by A.
    item_c, step_c = new_id(), new_id()
    with env["db"].write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
            "VALUES(?,'item',?,?,?,'InProgress','{}',1,?,?)",
            (item_c, env["project"], env["work"], "Same-branch contention item", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
            "VALUES(?,'step',?,?,?,'InProgress','{}',1,?,?)",
            (step_c, env["project"], item_c, "Same-branch contention step", now, now))
    run_c = new_id()
    env_c = dict(env) | {"actor": "network-client-b", "session": env["session_b"], "headers": headers_b,
        "item": item_c, "step": step_c, "run": run_c, "canonical": workspace_a, "pin": pin_a,
        "graph": env["graph"]}
    _seed_private_directive(env_c)
    queued, code = store_b.execute({"protocol_version": 1, "operation": "enqueue_execution", "request_id": new_id(),
        "actor": "network-client-b", "session_id": env["session_b"], "scope_id": env["project"],
        "source": {"product": "cli"}, "context_refs": [], "payload": {"step_id": step_c,
            "route": {"agent": "claude", "provider": "fixture-provider", "model": "fixture-model",
                "mode": "cli", "adapter_kind": "cli", "auth_state": "authenticated",
                "actual_support": "verified_supported", "capability_ref": "fixture-main-capability",
                "max_concurrency": 2, "selection_reason": "isolated F14 scope test"},
            "policy": {"max_retries": 0}}})
    assert code == 0 and queued["ok"], queued.get("error")
    run_c = queued["result"]["run_id"]
    prepared, code = store_b.execute({"protocol_version": 1, "operation": "prepare_execution",
        "request_id": new_id(), "actor": "network-client-b", "session_id": env["session_b"],
        "scope_id": env["project"], "source": {"product": "cli"}, "context_refs": [],
        "payload": {"run_id": run_c, "expected_run_revision": 1}})
    assert code == 0 and prepared["ok"], prepared.get("error")
    assert prepared["result"]["state"] == "queued" and prepared["result"]["waiting_reason"] == "scope_conflict"
    assert any(item.get("owner_run_id") == env["run"] for item in prepared["result"]["conflicts"])
    with closing(env["db"].connect()) as conn:
        current = dict(conn.execute("SELECT state,revision FROM execution_runs WHERE id=?", (run_c,)).fetchone())
        acquired = conn.execute("SELECT 1 FROM scope_locks WHERE run_id=?", (run_c,)).fetchone()
    assert current == {"state": "queued", "revision": 1} and acquired is None
