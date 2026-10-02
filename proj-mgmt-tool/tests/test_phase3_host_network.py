"""Loopback HTTPS acceptance tests for two independent Host clients.

These exercise the real Uvicorn/HTTP boundary with isolated test databases and
credentials. They do not represent a deployed Host or external reverse proxy.
"""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import time
from urllib.request import urlopen
import uuid

import pytest

from pmt.errors import PmtError
from pmt.http_store import HttpStore
from pmt.phase2_common import persist_json_resource
from pmt.util import canonical_json, fingerprint, new_id, utc_now


def _certificate(directory: Path, name: str):
    from datetime import datetime, timedelta, timezone
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256()))
    cert_path, key_path = directory / f"{name}.pem", directory / f"{name}-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return cert_path, key_path


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _seed_private_directive(env):
    directive = {"purpose": "F5 network fixture", "goal": "Use the authenticated source snapshot",
        "non_goal": ["Host filesystem access"],
        "change_scope": {"add": [], "modify": [], "delete": [], "forbidden": ["local execution"]},
        "inputs": [{"name": "graph", "meaning": "client captured graph"}],
        "outputs": [{"name": "context", "meaning": "bounded role projection"}],
        "tests": [{"name": "F5", "meaning": "source reference is verified"}],
        "logging": [{"name": "trace", "meaning": "IDs and hashes only"}],
        "method": {"steps": ["read source", "build projection"]},
        "context_refs": [node["id"] for node in env["graph"]["nodes"]],
        "autonomy": {"authority": "method", "scope": "current Step"}}
    req = {"request_id": new_id(), "actor": env["actor"], "session_id": env["session"],
           "scope_id": env["project"], "payload": {}}
    resource = persist_json_resource(env["db"], req, directive, env["project"], "step_directive", env["step"])
    criteria = [{"id": "current-source", "meaning": "Use current SourcePin"}]
    now = utc_now()
    with env["db"].write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
            (canonical_json({"criteria": criteria}), env["step"]))
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
            "VALUES(?,?,?,?,?,NULL,?,?,?,?,?,?,?,?) ON CONFLICT(step_id) DO UPDATE SET directive_id=excluded.directive_id,"
            "directive_version=excluded.directive_version,requirements_version=excluded.requirements_version,"
            "plan_version=excluded.plan_version,role=excluded.role,product_stage=excluded.product_stage,"
            "workspace=excluded.workspace,scopes_json=excluded.scopes_json,criteria_json=excluded.criteria_json,"
            "dependencies_json=excluded.dependencies_json,updated_at=excluded.updated_at",
            (env["step"], resource["artifact_id"], 1, "requirements-v1", "plan-v1", "lower", "prototype",
             env["canonical"], canonical_json([{"kind": "path", "workspace": env["canonical"],
                 "resource": env["relative"]}]), canonical_json(criteria), "[]", now, now))
        intent = {"run_id": env["run"], "task": {"task_id": env["item"], "step_id": env["step"]},
            "plan_version": "plan-v1", "requirements_version": "requirements-v1",
            "directive_ref": resource["artifact_id"], "criteria": criteria, "role": "lower",
            "scope_id": env["project"], "directive_version": 1}
        conn.execute("UPDATE execution_runs SET directive_version=1,intent_json=? WHERE id=?",
            (canonical_json(intent), env["run"]))
    return directive, resource, criteria


@pytest.fixture
def live_host(tmp_path, monkeypatch):
    from test_phase3_host_data import host_data_env

    env = host_data_env.__wrapped__(tmp_path / "prepared")
    directive, directive_resource, criteria = _seed_private_directive(env)
    claim_target, foreign_project = new_id(), new_id()
    now = utc_now()
    with env["db"].write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
            "VALUES(?,'item',?,?,?,'Planned','{}',1,?,?)",
            (claim_target, env["project"], env["work"], "HTTP claim race", now, now))
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (foreign_project, "project", env["repo"], "foreign-project",
             canonical_json({"repository_id": env["repo"]}), now, now))
    cert, key = _certificate(tmp_path, "host")
    wrong_ca, _ = _certificate(tmp_path, "wrong-ca")
    port = _free_port()
    base = f"https://127.0.0.1:{port}"
    stdout_path, stderr_path = tmp_path / "server.stdout", tmp_path / "server.stderr"
    stdout, stderr = stdout_path.open("wb"), stderr_path.open("wb")
    process_env = os.environ.copy()
    process_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src") + os.pathsep + process_env.get("PYTHONPATH", "")
    claim_key_name = "PMT_TEST_HOST_CLAIM_KEY"
    process_env[claim_key_name] = base64.b64encode(b"isolated-network-fixture-claim-key-32bytes").decode("ascii")
    command = [sys.executable, "-m", "pmt.host.cli", "--data-root", str(env["db"].root),
        "--config-root", str(env["db"].config_root), "serve", "--host", "127.0.0.1",
        "--port", str(port), "--claim-key-env", claim_key_name,
        "--ssl-certfile", str(cert), "--ssl-keyfile", str(key)]
    process = subprocess.Popen(command, cwd=Path(__file__).resolve().parents[1], env=process_env,
        stdout=stdout, stderr=stderr)
    context = ssl.create_default_context(cafile=str(cert))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urlopen(base + "/health", context=context, timeout=0.5) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        process.terminate()
        process.wait(timeout=5)
        pytest.fail("isolated Uvicorn server did not become ready")
    if process.poll() is not None:
        stderr.flush()
        pytest.fail("isolated Uvicorn server exited during startup")

    token_a = env["headers"]["authorization"][7:]
    store_a = HttpStore(base, "PMT_CLIENT_A_TOKEN", env["headers"]["x-pmt-device"],
        env["headers"]["x-pmt-environment"], env["headers"]["x-pmt-namespace"], ca_file=str(cert), timeout=2)
    monkeypatch.setenv("PMT_CLIENT_A_TOKEN", token_a)
    session_a = env["session"]
    store_a.register_session(session_a)
    device_b = env["app"].auth.issue_device("network-client-b", [env["project"]], ["read", "write", "runtime"])
    session_b, environment_b = "network-client-b-session", new_id()
    headers_b = {"authorization": "Bearer " + device_b["credential"], "x-pmt-device": device_b["device_id"],
        "x-pmt-environment": environment_b, "x-pmt-namespace": env["app"].auth.namespace_id,
        "x-pmt-session": session_b}
    env["app"].register_session(headers_b, {"session_id": session_b, "environment_id": environment_b})
    monkeypatch.setenv("PMT_CLIENT_B_TOKEN", device_b["credential"])
    store_b = HttpStore(base, "PMT_CLIENT_B_TOKEN", device_b["device_id"], environment_b,
        env["app"].auth.namespace_id, ca_file=str(cert), timeout=2)
    store_b.register_session(session_b)
    device_admin = env["app"].auth.issue_device("network-resource-admin", ["*"],
        ["read", "write", "runtime", "review", "admin"])
    session_admin, environment_admin = "network-resource-admin-session", new_id()
    headers_admin = {"authorization": "Bearer " + device_admin["credential"],
        "x-pmt-device": device_admin["device_id"], "x-pmt-environment": environment_admin,
        "x-pmt-namespace": env["app"].auth.namespace_id, "x-pmt-session": session_admin}
    env["app"].register_session(headers_admin,
        {"session_id": session_admin, "environment_id": environment_admin})
    monkeypatch.setenv("PMT_CLIENT_ADMIN_TOKEN", device_admin["credential"])
    store_admin = HttpStore(base, "PMT_CLIENT_ADMIN_TOKEN", device_admin["device_id"],
        environment_admin, env["app"].auth.namespace_id, ca_file=str(cert), timeout=2)
    store_admin.register_session(session_admin)

    fixture = {**env, "base": base, "cert": cert, "wrong_ca": wrong_ca,
        "process": process, "stdout": stdout, "stderr": stderr,
        "stdout_path": stdout_path, "stderr_path": stderr_path,
        "store_a": store_a, "store_b": store_b, "session_a": session_a,
        "session_b": session_b, "device_b": device_b, "environment_b": environment_b,
        "token_a": token_a, "token_b": device_b["credential"]}
    fixture.update(claim_target=claim_target, foreign_project=foreign_project,
                   store_admin=store_admin, session_admin=session_admin,
                   token_admin=device_admin["credential"], directive=directive,
                   directive_resource=directive_resource, criteria=criteria)
    try:
        yield fixture
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stdout.close()
        stderr.close()
        store_a._opener.close() if hasattr(store_a._opener, "close") else None
        store_b._opener.close() if hasattr(store_b._opener, "close") else None
        wire_log = stdout_path.read_bytes() + stderr_path.read_bytes()
        assert fixture["token_a"].encode() not in wire_log
        assert fixture["token_b"].encode() not in wire_log
        assert fixture["token_admin"].encode() not in wire_log


def _request(env, store, session, operation, payload, *, request_id=None, scope_id=None, **fields):
    request = {"protocol_version": 1, "operation": operation, "request_id": request_id or new_id(),
        "actor": env["actor"] if store is env["store_a"] else "network-client-b",
        "session_id": session, "scope_id": scope_id or env["project"], "payload": payload,
        "context_refs": [], "source": {}}
    request.update(fields)
    return request


def test_real_https_two_clients_compatibility_replay_and_claim_race(live_host):
    env = live_host
    compatibility_a = env["store_a"].check_compatibility()
    compatibility_b = env["store_b"].check_compatibility()
    assert compatibility_a["compatible"] and compatibility_b["compatible"]
    assert compatibility_a["actor"] == env["actor"]
    assert compatibility_b["actor"] == "network-client-b"
    assert compatibility_a["device_id"] != compatibility_b["device_id"]

    req = _request(env, env["store_a"], env["session_a"], "save_change",
        {"kind": "item", "title": "HTTP idempotency fixture", "reason": "acceptance"},
        scope_id=env["project"])
    first, code = env["store_a"].execute(req)
    assert code == 0 and first["ok"], first.get("error")
    replay, replay_code = env["store_a"].execute(req)
    assert replay_code == 0 and replay["result"] == first["result"]
    lookup = env["store_a"].get_request_result(req["request_id"], env["actor"], env["session_a"], expected_request=req)
    assert lookup == (first, 0)
    changed = json.loads(canonical_json(req))
    changed["payload"]["title"] = "different semantic body"
    conflict, conflict_code = env["store_a"].execute(changed)
    assert conflict_code == 3 and conflict["error"]["code"] == "request_conflict"
    with pytest.raises(PmtError) as mismatch:
        env["store_a"].get_request_result(req["request_id"], env["actor"], env["session_a"], expected_request=changed)
    assert mismatch.value.code == "request_conflict"

    lost = _request(env, env["store_a"], env["session_a"], "save_change",
        {"kind": "item", "title": "lost HTTP reply fixture", "reason": "acceptance"}, scope_id=env["project"])
    transport_request = env["store_a"]._request
    def discard_once(method, path, **kwargs):
        result = transport_request(method, path, **kwargs)
        if method == "POST" and path == "/api/v1/operations" and kwargs.get("request_id") == lost["request_id"]:
            raise PmtError("remote_unavailable", "Injected post-commit response loss", 3, True,
                           {"effect": "unknown"})
        return result
    env["store_a"]._request = discard_once
    try:
        with pytest.raises(PmtError) as unknown:
            env["store_a"].execute(lost)
        assert unknown.value.code == "remote_unavailable"
    finally:
        env["store_a"]._request = transport_request
    recovered, recovered_code = env["store_a"].execute(lost)
    assert recovered_code == 0 and recovered["ok"]
    assert recovered == env["store_a"].get_request_result(
        lost["request_id"], env["actor"], env["session_a"], expected_request=lost)[0]

    claim = _request(env, env["store_a"], env["session_a"], "claim_task",
        {}, scope_id=env["project"], record_id=env["claim_target"], expected_revision=1)
    rival = _request(env, env["store_b"], env["session_b"], "claim_task",
        {}, scope_id=env["project"], record_id=env["claim_target"], expected_revision=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda pair: pair[0].execute(pair[1]),
            [(env["store_a"], claim), (env["store_b"], rival)]))
    successes = [value for value, code in outcomes if code == 0 and value["ok"]]
    failures = [value for value, code in outcomes if code != 0 or not value["ok"]]
    assert len(successes) == len(failures) == 1, [value.get("error") for value, _ in outcomes]
    assert failures[0]["error"]["code"] in {"claim_conflict", "revision_conflict"}


def test_real_https_source_snapshot_f2_query_and_local_only_rejection(live_host):
    env = live_host
    tls = ssl.create_default_context(cafile=str(env["cert"]))
    with urlopen(env["base"] + "/openapi.json", context=tls, timeout=2) as response:
        openapi = json.loads(response.read())
    assert {"/api/v1/operations", "/api/v1/compatibility", "/api/v1/resources",
            "/api/v1/resources/{resource_id}"}.issubset(openapi["paths"])
    assert "/docs" not in openapi["paths"]
    graph_bytes = canonical_json(env["graph"]).encode("utf-8")
    upload = env["store_a"].publish_resource({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "graph_snapshot"}, graph_bytes, session_id=env["session_a"])
    graph_ref = upload["artifact_ref"]
    request = _request(env, env["store_a"], env["session_a"], "publish_source_snapshot", {
        "project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3, "branch_key": "main",
        "source_pin": env["pin"].to_dict(), "graph_resource_ref": graph_ref,
        "expected_source_revision": 0})
    receipt, code = env["store_a"].execute(request)
    assert code == 0 and receipt["ok"], receipt.get("error")
    assert receipt["result"]["provenance"] == "client_snapshot"
    assert receipt["result"]["host_git_verified"] is False
    resource = env["store_a"].read_resource(graph_ref["id"], session_id=env["session_a"],
        expected_sha256=graph_ref["sha256"], scope_id=env["project"])
    assert resource["content"] == graph_bytes and resource["sha256"] == graph_ref["sha256"]

    common = {"project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3, "expected_source": env["pin"].to_dict()}
    rebuild, code = env["store_a"].execute(_request(env, env["store_a"], env["session_a"],
        "rebuild_graph_index", common))
    assert code == 0 and rebuild["ok"], rebuild.get("error")
    query, code = env["store_a"].execute(_request(env, env["store_a"], env["session_a"], "query_graph",
        {**common, "query": {"node_ids": [node["id"] for node in env["graph"]["nodes"]],
                              "max_depth": 2, "page_size": 100}}))
    assert code == 0 and query["ok"], query.get("error")
    assert query["result"]["source_pin"]["source_hash"] == env["pin"].source_hash

    context_request = _request(env, env["store_a"], env["session_a"], "build_task_context", {
        **common, "task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
        "role": "lower", "workspace": env["canonical"],
        "node_ids": [node["id"] for node in env["graph"]["nodes"]],
        "budget": {"max_bytes": 128000, "max_lines": 2000, "unit": "utf8"}})
    context, context_code = env["store_a"].execute(context_request)
    assert context_code == 0 and context["ok"], context.get("error")
    assert context["result"]["context_ref"]["source_hash"] == env["pin"].source_hash
    read_context, read_code = env["store_a"].execute(_request(env, env["store_a"], env["session_a"],
        "read_task_context", {**common, "context_ref": context["result"]["context_ref"]}))
    assert read_code == 0 and read_context["ok"], read_context.get("error")
    projection = read_context["result"]
    assert projection["source"]["source_hash"] == env["pin"].source_hash
    assert projection["current_authority"]["owner_checked"] is True
    assert projection["incomplete"] is False and projection["mandatory_omissions"] == []
    assert projection["budget"]["used_bytes"] <= projection["budget"]["requested"]["max_bytes"]
    assert env["db"].root.as_posix() not in json.dumps(projection)

    evidence = env["store_a"].publish_resource({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "evidence"}, b"F6 current scoped evidence", session_id=env["session_a"])["artifact_ref"]
    definition_id = "test.py_compile"
    command = ["fixture-test"]
    input_hash = fingerprint({"target": "fixture"})
    criteria_hash = {item["id"]: fingerprint(item) for item in env["criteria"]}
    manifest = {"schema_version": 1, "target_id": env["step"], "definition_id": definition_id,
        "definition_version": "1", "environment_id": env["headers"]["x-pmt-environment"],
        "canonical_workspace": env["canonical"], "source_pin": env["pin"].to_dict(),
        "command": command, "inputs_sha256": input_hash, "criteria": criteria_hash,
        "workspace_files": [{"path": env["relative"], "sha256": "d" * 64, "size": 23}],
        "runtime": {"os": "fixture", "architecture": "fixture", "python": "3.13",
            "sqlite": "fixture", "packages": []}, "dependency_manifests": [],
        "configuration_hashes": [], "evidence_refs": [{"id": evidence["id"], "sha256": evidence["sha256"]}],
        "inventory_status": "complete", "provenance": "client_snapshot"}
    verification_resource = env["store_a"].publish_resource({"request_id": new_id(),
        "scope_id": env["project"], "purpose": "verification_snapshot"},
        canonical_json(manifest).encode("utf-8"), session_id=env["session_a"])["artifact_ref"]
    verification_request = _request(env, env["store_a"], env["session_a"], "publish_verification_snapshot", {
        **common, "target_id": env["step"], "definition_id": definition_id, "definition_version": "1",
        "command": command, "inputs_sha256": input_hash, "verification_resource_ref": verification_resource,
        "expected_snapshot_revision": 0})
    snapshot, snapshot_code = env["store_a"].execute(verification_request)
    replayed_snapshot, replayed_code = env["store_a"].execute(verification_request)
    assert replayed_code == snapshot_code and replayed_snapshot == snapshot
    assert snapshot_code == 0 and snapshot["ok"], snapshot.get("error")

    from test_phase3_reuse import _actual_definition
    definition = _actual_definition()
    definition["selectors"]["source"]["paths"] = [env["relative"]]
    reuse_request = _request(env, env["store_a"], env["session_a"], "resolve_reuse", {
        **common, "definition": definition, "target_id": env["step"], "workspace": env["canonical"],
        "paths": [env["relative"]], "command": command, "inputs": {"target": "fixture"},
        "event_id": new_id()})
    reuse, reuse_code = env["store_a"].execute(reuse_request)
    assert reuse_code == 0 and reuse["ok"], reuse.get("error")
    assert reuse["result"]["status"] == "claimed"
    decision_request = _request(env, env["store_a"], env["session_a"], "read_reuse_decision", {
        **common, "body_ref": reuse["result"]["body_ref"], "workspace": env["canonical"],
        "paths": [env["relative"]]})
    decision, decision_code = env["store_a"].execute(decision_request)
    assert decision_code == 0 and decision["ok"], decision.get("error")
    assert decision["result"]["status"] == "active"

    for operation in ("apply_graph_change", "dispatch_execution", "poll_execution"):
        local_only = _request(env, env["store_a"], env["session_a"], operation, common)
        envelope, exit_code = env["store_a"].execute(local_only)
        assert exit_code == 3 and envelope["error"]["code"] in {
            "host_operation_forbidden", "operation_unavailable"}, envelope.get("error")


def test_https_resources_are_hash_bound_private_bounded_and_tls_verified(live_host):
    env = live_host
    body = bytes(range(256)) * (32 * 1024)
    published = env["store_a"].publish_resource({"request_id": new_id(), "scope_id": env["project"],
        "purpose": "evidence"}, body, session_id=env["session_a"])
    artifact = published["artifact_ref"]
    returned = env["store_a"].read_resource(artifact["id"], session_id=env["session_a"],
        expected_sha256=artifact["sha256"], scope_id=env["project"])
    assert returned["content"] == body and returned["size"] == len(body)

    private = env["store_admin"].publish_resource({"request_id": new_id(), "scope_id": env["foreign_project"],
        "purpose": "evidence"}, b"private-to-another-scope", session_id=env["session_admin"])
    with pytest.raises(PmtError) as denied:
        env["store_b"].read_resource(private["artifact_ref"]["id"], session_id=env["session_b"],
            expected_sha256=private["artifact_ref"]["sha256"], scope_id=env["foreign_project"])
    assert denied.value.code in {"scope_forbidden", "host_resource_forbidden"}
    with pytest.raises(PmtError) as oversize:
        env["store_a"].publish_resource({"request_id": new_id(), "scope_id": env["project"],
            "purpose": "evidence"}, b"x" * (8 * 1024 * 1024 + 1), session_id=env["session_a"])
    assert oversize.value.code == "resource_too_large"

    wrong = HttpStore(env["base"], "PMT_CLIENT_A_TOKEN", env["headers"]["x-pmt-device"],
        env["headers"]["x-pmt-environment"], env["headers"]["x-pmt-namespace"],
        ca_file=str(env["wrong_ca"]), timeout=2)
    with pytest.raises(PmtError) as bad_ca:
        wrong.check_compatibility()
    assert bad_ca.value.code == "remote_unavailable"


def test_remote_scope_and_device_revocation_apply_to_cached_requests(live_host):
    env = live_host
    outside = new_id()
    request = _request(env, env["store_b"], env["session_b"], "read_context", {}, scope_id=outside)
    envelope, code = env["store_b"].execute(request)
    assert code == 3 and envelope["error"]["code"] == "scope_forbidden"

    saved = _request(env, env["store_a"], env["session_a"], "save_change",
        {"kind": "item", "title": "revocation fixture", "reason": "acceptance"}, scope_id=env["project"])
    envelope, code = env["store_a"].execute(saved)
    assert code == 0 and envelope["ok"]
    env["app"].auth.revoke_device(env["headers"]["x-pmt-device"], expected_revision=1)
    with pytest.raises(PmtError) as revoked:
        env["store_a"].get_request_result(saved["request_id"], env["actor"], env["session_a"], expected_request=saved)
    assert revoked.value.code in {"unauthenticated", "credential_unavailable"}
    denied, denied_code = env["store_a"].execute(saved)
    assert denied_code == 3 and not denied["ok"]
