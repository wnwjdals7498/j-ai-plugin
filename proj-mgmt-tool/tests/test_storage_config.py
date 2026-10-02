"""F12 ConfigRoot storage selection and hosted request-boundary tests."""
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

pytest_plugins = ["test_phase3_host_network"]

from pmt.errors import PmtError
from pmt.storage_config import (adapt_host_request, configure_storage, profile_environment_id,
    adapt_host_actor, select_store, storage_path, storage_status)
from pmt.util import canonical_json


DEVICE = "00000000-0000-4000-8000-000000000201"
NAMESPACE = "00000000-0000-4000-8000-000000000202"
REPOSITORY = "00000000-0000-4000-8000-000000000203"
PROJECT = "00000000-0000-4000-8000-000000000204"


def _mapping(root, branch="main"):
    return {"repository_id": REPOSITORY, "project_id": PROJECT, "branch": branch,
        "branch_key_sha256": hashlib.sha256(branch.encode()).hexdigest(),
        "local_root": str(root), "relative_graph_path": "docs/pmt-docs/plan.graph.json"}


class FakeHttpStore:
    calls = []
    response_actor = "pmt-user"
    is_compatible = True

    def __init__(self, endpoint, credential_env, device_id, environment_id, namespace_id, ca_file=None):
        self.endpoint, self.credential_env, self.device_id = endpoint, credential_env, device_id
        self.environment_id, self.namespace_id, self.ca_file = environment_id, namespace_id, ca_file
        type(self).calls.append(("construct", credential_env, device_id, environment_id, namespace_id, ca_file))
        self.requests = []

    def check_compatibility(self):
        type(self).calls.append(("compatibility",))
        return {"compatible": self.is_compatible, "incompatibilities": [] if self.is_compatible else ["db_schema"],
            "actor": self.response_actor, "device_id": self.device_id, "namespace_id": self.namespace_id,
            "core_version": "0.3.0", "db_schema": 4, "graph_schema": 1,
            "protocol_versions": [1], "scopes": [PROJECT], "permissions": ["read", "write", "runtime"]}

    def register_session(self, session_id):
        type(self).calls.append(("register_session", session_id, self.environment_id))
        return {"session_id": session_id, "environment_id": self.environment_id, "device_id": self.device_id}

    def execute(self, request):
        self.requests.append(request)
        return {"ok": True, "request_id": request["request_id"], "result": {"operation": request["operation"]}}, 0

    def get_request_result(self, request_id, actor, session_id, *, expected_request=None):
        return None


def _host_setup(config_root, workspace_root, *, expected_hash=None, factory=FakeHttpStore):
    return configure_storage(config_root, {"request_id": str(uuid.uuid4()), "mode": "hosted",
        "expected_config_sha256": expected_hash, "endpoint": "https://pmt.example.invalid",
        "credential_env": "PMT_HOST_TOKEN", "device_id": DEVICE, "namespace_id": NAMESPACE,
        "workspace_mappings": [_mapping(workspace_root)]}, store_factory=factory)


def test_storage_profile_probe_precedes_atomic_hosted_config_publish(tmp_path):
    config = tmp_path / "config"
    workspace = tmp_path / "checkout"
    workspace.mkdir()
    FakeHttpStore.calls.clear()
    configured = _host_setup(config, workspace, expected_hash=None)
    profile_path = storage_path(config)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert configured["mode"] == "hosted" and configured["actor"] == "pmt-user"
    assert configured["environment_id"] == profile_environment_id(config)
    assert profile["device_id"] == DEVICE and profile["namespace_id"] == NAMESPACE
    assert profile["credential_env"] == "PMT_HOST_TOKEN"
    assert "credential" not in profile and "token" not in profile
    assert [item[0] for item in FakeHttpStore.calls] == ["construct", "compatibility", "register_session"]
    assert profile["revision"] == 1
    assert storage_status(config)["config_sha256"] == configured["config_sha256"]


def test_storage_settings_cas_failure_and_host_probe_failure_preserve_current_file(tmp_path):
    config, workspace = tmp_path / "cfg", tmp_path / "checkout"
    workspace.mkdir()
    local = configure_storage(config, {"mode": "local", "expected_config_sha256": None})
    path = storage_path(config)
    original = path.read_bytes()
    with pytest.raises(PmtError) as conflict:
        _host_setup(config, workspace, expected_hash="0" * 64)
    assert conflict.value.code == "storage_config_conflict"
    assert path.read_bytes() == original

    FakeHttpStore.is_compatible = False
    FakeHttpStore.calls.clear()
    with pytest.raises(PmtError) as incompatible:
        _host_setup(config, workspace, expected_hash=local["config_sha256"])
    assert incompatible.value.code == "remote_compatibility_mismatch"
    assert [item[0] for item in FakeHttpStore.calls] == ["construct", "compatibility"]
    assert path.read_bytes() == original
    FakeHttpStore.is_compatible = True


def test_setup_rejects_host_actor_mismatch_before_settings_publish(tmp_path):
    config, workspace = tmp_path / "cfg", tmp_path / "checkout"
    workspace.mkdir()
    FakeHttpStore.response_actor = "registered-actor"
    request = {"mode": "hosted", "expected_config_sha256": None,
        "endpoint": "https://pmt.example.invalid", "credential_env": "PMT_HOST_TOKEN",
        "device_id": DEVICE, "namespace_id": NAMESPACE, "expected_actor": "other-actor",
        "workspace_mappings": [_mapping(workspace)]}
    with pytest.raises(PmtError) as mismatch:
        configure_storage(config, request, store_factory=FakeHttpStore)
    assert mismatch.value.code == "host_principal_mismatch"
    assert not storage_path(config).exists()
    FakeHttpStore.response_actor = "pmt-user"


def test_host_request_uses_canonical_workspace_and_never_sends_local_path(tmp_path):
    config, workspace = tmp_path / "cfg", tmp_path / "checkout"
    workspace.mkdir()
    _host_setup(config, workspace, expected_hash=None)
    from pmt.storage_config import _read_profile
    profile, _ = _read_profile(config)
    request = {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": "query_graph",
        "actor": "pmt-user", "session_id": "native-session", "scope_id": PROJECT,
        "payload": {"repository_id": REPOSITORY, "project_id": PROJECT,
            "workspace": str(workspace), "relative_graph_path": "docs/pmt-docs/plan.graph.json",
            "expected_source": {"repository_id": REPOSITORY, "project_id": PROJECT,
                "source_kind": "git", "selected_ref": "main"}}}
    adapted = adapt_host_request(profile, request)
    payload = adapted["payload"]
    assert payload["workspace"] == f"pmt://{REPOSITORY}/{hashlib.sha256(b'main').hexdigest()}"
    assert payload["canonical_workspace"] == payload["workspace"]
    assert payload["relative_graph_path"] == "docs/pmt-docs/plan.graph.json"
    assert str(workspace) not in canonical_json(adapted)
    assert "local_root" not in payload and "local_workspace" not in payload


def test_hosted_selector_routes_allowlist_and_blocks_local_database_fallback(tmp_path):
    config, data, workspace = tmp_path / "cfg", tmp_path / "data", tmp_path / "checkout"
    workspace.mkdir()
    _host_setup(config, workspace, expected_hash=None)
    from pmt.storage_config import _read_profile
    profile, _ = _read_profile(config)
    request = {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": "query_graph",
        "actor": profile["actor"], "session_id": "session-a", "scope_id": PROJECT,
        "payload": {"repository_id": REPOSITORY, "project_id": PROJECT, "workspace": str(workspace),
            "relative_graph_path": "docs/pmt-docs/plan.graph.json"}}
    factories = []
    def http_factory(*args):
        value = FakeHttpStore(*args); factories.append(value); return value
    store = select_store(data, config, request, http_store_factory=http_factory)
    response, exit_code = store.execute(request)
    assert exit_code == 0 and response["ok"]
    assert factories[-1].requests[0]["payload"]["workspace"].startswith("pmt://")

    called_local = []
    local_operation = dict(request, operation="apply_graph_change")
    client_files = select_store(data, config, local_operation, http_store_factory=http_factory,
        local_store_factory=lambda *_: called_local.append(True))
    unavailable, code = client_files.execute(local_operation)
    assert code == 2 and unavailable["error"]["code"] == "runtime_input_invalid"
    assert not called_local

    wrong_actor = dict(request, actor="hook")
    with pytest.raises(PmtError) as denied:
        select_store(data, config, wrong_actor, http_store_factory=http_factory,
            local_store_factory=lambda *_: called_local.append(True))
    assert denied.value.code == "host_actor_mismatch"
    assert not called_local


def test_local_default_selector_preserves_existing_local_store_behavior(tmp_path):
    called = []
    request = {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": "setup",
        "actor": "main", "session_id": "session", "payload": {}}
    marker = object()
    store = select_store(tmp_path / "data", tmp_path / "config", request,
        local_store_factory=lambda data, config: called.append((data, config)) or marker)
    assert store is marker and len(called) == 1


def test_profile_conflicting_branch_path_mapping_is_rejected_before_http(tmp_path):
    config, workspace = tmp_path / "cfg", tmp_path / "checkout"
    workspace.mkdir()
    _host_setup(config, workspace, expected_hash=None)
    from pmt.storage_config import _read_profile
    profile, _ = _read_profile(config)
    request = {"protocol_version": 1, "request_id": str(uuid.uuid4()), "operation": "query_graph",
        "actor": profile["actor"], "session_id": "session-a", "scope_id": PROJECT,
        "payload": {"repository_id": REPOSITORY, "project_id": PROJECT, "workspace": str(workspace),
            "relative_graph_path": "docs/other.graph.json"}}
    with pytest.raises(PmtError) as mismatch:
        adapt_host_request(profile, request)
    assert mismatch.value.code == "storage_mapping_conflict"


def test_only_normalized_native_hook_event_is_bound_to_host_actor(tmp_path):
    config, workspace = tmp_path / "cfg", tmp_path / "checkout"
    workspace.mkdir()
    _host_setup(config, workspace, expected_hash=None)
    from pmt.storage_config import _read_profile
    from pmt.hooks import normalize_event
    profile, _ = _read_profile(config)
    native = normalize_event("codex", "UserPromptSubmit", {
        "hook_event_name": "UserPromptSubmit", "session_id": "native-session",
        "turn_id": "native-turn", "prompt": "sensitive prompt excluded from event body"},
        environ={"PMT_INSTALLATION_ID": profile["environment_id"]})
    adapted = adapt_host_actor(profile, native)
    assert adapted["actor"] == profile["actor"]
    assert adapted["source"]["original_actor"] == "hook"
    assert adapted["source"]["product"] == "codex"
    assert adapted["session_id"] == native["session_id"]
    assert adapted["request_id"] == native["request_id"]
    assert adapted["normalized_event"] == native["normalized_event"]
    assert "sensitive prompt" not in canonical_json(adapted)

    forged_business = {**native, "operation": "save_change"}
    with pytest.raises(PmtError) as denied:
        adapt_host_actor(profile, forged_business)
    assert denied.value.code == "host_actor_mismatch"
    forged_origin = json.loads(canonical_json(native))
    forged_origin["source"]["installation_id"] = str(uuid.uuid4())
    with pytest.raises(PmtError):
        adapt_host_actor(profile, forged_origin)
    wrong_user = json.loads(canonical_json(native))
    wrong_user["actor"] = "another-user"
    with pytest.raises(PmtError):
        adapt_host_actor(profile, wrong_user)


def test_storage_cli_configure_status_keeps_protocol_json_and_writes_only_configroot(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    body = {"request_id": str(uuid.uuid4()), "mode": "local", "expected_config_sha256": None,
            "workspace_mappings": []}
    source = Path(__file__).resolve().parents[1] / "src"
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    configured = subprocess.run([sys.executable, "-m", "pmt", "--config-root", str(config),
        "--data-root", str(data), "storage", "configure"], input=canonical_json(body), text=True,
        encoding="utf-8", capture_output=True, cwd=Path(__file__).resolve().parents[1], env=env, timeout=15)
    assert configured.returncode == 0, configured.stderr
    envelope = json.loads(configured.stdout)
    assert envelope["ok"] and envelope["result"]["mode"] == "local"
    assert storage_path(config).is_file()
    assert not (data / "pmt.sqlite3").exists()
    status = subprocess.run([sys.executable, "-m", "pmt", "--config-root", str(config),
        "storage", "status"], input="{}", text=True, encoding="utf-8", capture_output=True,
        cwd=Path(__file__).resolve().parents[1], env=env, timeout=15)
    assert status.returncode == 0, status.stderr
    status_value = json.loads(status.stdout)
    assert status_value["ok"] and status_value["result"]["configured"] is True


def test_storage_selector_uses_tls_host_and_two_local_workspace_mappings(live_host, tmp_path):
    from pmt.http_store import HttpStore
    from test_phase3_host_network import _request

    env = live_host
    client_root_a, client_root_b = tmp_path / "client-a", tmp_path / "client-b"
    client_root_a.mkdir(); client_root_b.mkdir()
    config = tmp_path / "hosted-config"
    config.mkdir()
    (config / "profile.json").write_text(canonical_json({
        "environment_id": env["headers"]["x-pmt-environment"]}), encoding="utf-8")
    mappings = [{"repository_id": env["repo"], "project_id": env["project"], "branch": "main",
        "branch_key_sha256": hashlib.sha256(b"main").hexdigest(), "local_root": str(client_root_a),
        "relative_graph_path": env["relative"]},
        {"repository_id": env["repo"], "project_id": env["foreign_project"], "branch": "main",
        "branch_key_sha256": hashlib.sha256(b"main").hexdigest(), "local_root": str(client_root_b),
        "relative_graph_path": env["relative"]}]
    configure_storage(config, {"mode": "hosted", "expected_config_sha256": None,
        "endpoint": env["base"], "credential_env": "PMT_CLIENT_A_TOKEN",
        "device_id": env["headers"]["x-pmt-device"], "namespace_id": env["headers"]["x-pmt-namespace"],
        "ca_file": str(env["cert"]), "workspace_mappings": mappings})
    stored_profile_text = storage_path(config).read_text(encoding="utf-8")
    assert env["token_a"] not in stored_profile_text
    assert "PMT_CLIENT_A_TOKEN" in stored_profile_text

    graph_bytes = canonical_json(env["graph"]).encode("utf-8")
    graph_ref = env["store_a"].publish_resource({"request_id": str(uuid.uuid4()),
        "scope_id": env["project"], "purpose": "graph_snapshot"}, graph_bytes,
        session_id=env["session_a"])["artifact_ref"]
    publish = _request(env, env["store_a"], env["session_a"], "publish_source_snapshot", {
        "project_id": env["project"], "repository_id": env["repo"],
        "canonical_workspace": env["canonical"], "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3, "branch_key": "main",
        "source_pin": env["pin"].to_dict(), "graph_resource_ref": graph_ref,
        "expected_source_revision": 0})
    pointer, code = env["store_a"].execute(publish)
    assert code == 0 and pointer["ok"], pointer.get("error")

    seen = []
    class ObservedHttpStore(HttpStore):
        def execute(self, request):
            seen.append(json.loads(canonical_json(request)))
            return super().execute(request)

    read_request = _request(env, env["store_a"], env["session_a"], "read_source_snapshot", {
        "project_id": env["project"], "repository_id": env["repo"],
        "workspace": str(client_root_a), "canonical_workspace": env["canonical"],
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "expected_run_revision": 3, "expected_source": env["pin"].to_dict()})
    selected = select_store(tmp_path / "must-not-create-local-db", config, read_request,
                            http_store_factory=ObservedHttpStore)
    source, code = selected.execute(read_request)
    assert code == 0 and source["ok"], source.get("error")
    assert source["result"]["source_pin"]["source_hash"] == env["pin"].source_hash
    assert seen and seen[-1]["payload"]["workspace"] == env["canonical"]
    assert str(client_root_a) not in canonical_json(seen[-1])
    assert not (tmp_path / "must-not-create-local-db" / "pmt.sqlite3").exists()

    cross_scope = _request(env, env["store_a"], env["session_a"], "read_source_snapshot", {
        "project_id": env["foreign_project"], "repository_id": env["repo"],
        "workspace": str(client_root_b), "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": 3}, scope_id=env["foreign_project"])
    store_b = select_store(tmp_path / "must-not-create-local-db", config, cross_scope,
                           http_store_factory=ObservedHttpStore)
    denied, denied_code = store_b.execute(cross_scope)
    assert denied_code == 3 and denied["error"]["code"] == "scope_forbidden"
    assert seen[-1]["payload"]["workspace"] == f"pmt://{env['repo']}/{hashlib.sha256(b'main').hexdigest()}"
    assert str(client_root_b) not in canonical_json(seen[-1])

    from pmt.hooks import normalize_event
    native = normalize_event("codex", "UserPromptSubmit", {"hook_event_name": "UserPromptSubmit",
        "session_id": env["session_a"], "turn_id": "f12-native-turn", "prompt": "not copied"},
        environ={"PMT_INSTALLATION_ID": env["headers"]["x-pmt-environment"]})
    hook_store = select_store(tmp_path / "must-not-create-local-db", config, native,
        environ={"PMT_SCOPE_ID": env["project"]}, http_store_factory=ObservedHttpStore)
    hook_result, hook_code = hook_store.execute(native)
    assert hook_code == 0 and hook_result["ok"], {"error": hook_result.get("error"),
        "wire_actor": seen[-1].get("actor"), "wire_session": seen[-1].get("session_id"),
        "expected_actor": env["actor"], "expected_session": env["session_a"],
        "profile_environment": storage_status(config).get("environment_id"),
        "host_environment": env["headers"]["x-pmt-environment"]}
    assert seen[-1]["actor"] == env["actor"]
    assert seen[-1]["source"]["original_actor"] == "hook"
    assert seen[-1]["session_id"] == native["session_id"]
    assert seen[-1]["request_id"] == native["request_id"]
    assert seen[-1]["normalized_event"] == native["normalized_event"]
    assert seen[-1]["scope_id"] == env["project"]
    assert "not copied" not in canonical_json(seen[-1])
