from __future__ import annotations

import hashlib
import io
import json
import ssl
import uuid
from pathlib import Path

import pytest

from pmt import handoff
from pmt import easy_hook, easy_setup
from pmt.client_setup import client, connect as connect_module
from pmt.client_setup.credentials import ENV_NAME, _paths, load_credential, stage_credential
from pmt.errors import PmtError
from pmt.storage_config import profile_environment_id, storage_path


SECRET_A = "d2-test-credential-A"
SECRET_B = "d2-test-credential-B"


@pytest.fixture(autouse=True)
def isolate_process_host_credential(monkeypatch):
    # Track both original presence and the intentional empty test value so a
    # prior suite's protected-store load cannot leak between test modules.
    monkeypatch.setenv(ENV_NAME, "")
    monkeypatch.delenv(ENV_NAME, raising=False)


def _handoff(tmp_path, *, ca=False):
    project_id, repository_id = str(uuid.uuid4()), str(uuid.uuid4())
    document = handoff.build_handoff(
        host_url="https://example.invalid",
        namespace_id=str(uuid.uuid4()),
        device={"device_id": str(uuid.uuid4()), "actor": "d2-test",
                "permissions": ["read", "write"], "scopes": [project_id],
                "credential": {"delivery": "separate", "env": ENV_NAME}},
        projects=[{"name": "sample", "project_id": project_id,
                   "repositories": [{"name": "repo", "repository_id": repository_id,
                                     "remote": "https://example.invalid/repo.git",
                                     "graph_path": "docs/graph.json"}]}],
    )
    if ca:
        bad_pem = "not a certificate"
        document["host"].update(ca_pem=bad_pem, ca_sha256=hashlib.sha256(bad_pem.encode()).hexdigest())
    path = tmp_path / "handoff.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path, document, project_id, repository_id


def _seed_hosted_profile(root):
    environment_id = profile_environment_id(root, create=True)
    profile = {"schema_version": 1, "revision": 1, "mode": "hosted",
               "environment_id": environment_id, "workspace_mappings": [],
               "endpoint": "https://old.invalid", "credential_env": ENV_NAME,
               "device_id": str(uuid.uuid4()), "namespace_id": str(uuid.uuid4()), "actor": "old"}
    raw = (json.dumps(profile, sort_keys=True, separators=(",", ":")) + "\n").encode()
    storage_path(root).write_bytes(raw)
    return raw


def _publishing_configure(config_root, request):
    raw = (json.dumps({"test_profile": request}, sort_keys=True) + "\n").encode()
    storage_path(config_root).write_bytes(raw)
    return {"actor": request["expected_actor"], "config_sha256": hashlib.sha256(raw).hexdigest()}


def test_connect_stages_protected_credential_and_merges_projects(tmp_path):
    handoff_path, doc, project_id, repository_id = _handoff(tmp_path)
    root = tmp_path / "config"
    result = connect_module.connect(root, handoff_path, credential=SECRET_A, configure=_publishing_configure)

    assert result["actor"] == "d2-test"
    assert load_credential(root, environ={}) == SECRET_A
    registry = json.loads((root / "projects.json").read_text(encoding="utf-8"))
    assert registry["projects"][0]["project_id"] == project_id
    assert registry["projects"][0]["repository_id"] == repository_id
    metadata = json.loads((root / "client.json").read_text(encoding="utf-8"))
    assert metadata["source"] == "connect"
    assert SECRET_A not in (root / "projects.json").read_text(encoding="utf-8")


def test_connect_return_summary_has_public_connection_details_and_ca_digest(tmp_path, monkeypatch):
    handoff_path, document, _project_id, _repository_id = _handoff(tmp_path)
    ca_bytes = b"public CA summary fixture"
    document["host"].update(ca_pem=ca_bytes.decode(), ca_sha256=hashlib.sha256(ca_bytes).hexdigest())
    monkeypatch.setattr(connect_module.handoff, "load_handoff", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    root = tmp_path / "config"

    def probed_configure(config_root, request):
        raw = (json.dumps({"test_profile": request}, sort_keys=True) + "\n").encode()
        storage_path(config_root).write_bytes(raw)
        return {"actor": "d2-test", "config_sha256": hashlib.sha256(raw).hexdigest(),
                "device_id": document["device"]["device_id"], "namespace_id": document["namespace_id"],
                "host_preflight": {"core_version": "0.4.2", "db_schema": 5,
                                   "graph_schema": 1, "protocol_versions": [1],
                                   "scopes": document["device"]["scopes"]}}

    result = connect_module.connect(root, handoff_path, credential=SECRET_A, configure=probed_configure)
    summary = result["connect_summary"]
    assert summary["endpoint"] == document["host"]["url"]
    assert summary["namespace_id"] == document["namespace_id"]
    assert summary["actor"] == document["device"]["actor"]
    assert summary["scopes"] == document["device"]["scopes"]
    assert summary["permissions"] == document["device"]["permissions"]
    assert summary["compatibility_source"] == "Host probe"
    assert summary["compatibility"]["core_version"] == "0.4.2"
    assert summary["ca_sha256"] == hashlib.sha256(ca_bytes).hexdigest()
    assert SECRET_A not in json.dumps(summary)


@pytest.mark.parametrize("dry_run", [False, True])
def test_connect_cli_prints_summary_and_public_ca_hash_without_credential(tmp_path, monkeypatch, capsys, dry_run):
    from pmt import easy_cli

    root = tmp_path / "config"
    handoff_file = tmp_path / "handoff.json"
    credential_file = tmp_path / "credential.input"
    secret = "not-for-stdout-secret-fixture"
    credential_file.write_text(secret, encoding="utf-8")
    monkeypatch.setattr(easy_cli.easy_setup, "default_roots", lambda _env: (root, tmp_path / "data"))
    observed = {}

    def fake_connect(_root, path, *, credential, dry_run):
        observed.update(path=path, credential=credential, dry_run=dry_run)
        return {"connect_summary": {"endpoint": "https://host.example", "namespace_id": "namespace-id",
                "actor": "actor-name", "scopes": ["scope-id"], "permissions": ["read", "write"],
                "compatibility_source": "Host probe", "compatibility": {"core_version": "0.4.2",
                "db_schema": 5, "graph_schema": 1, "protocol_versions": [1]}, "ca_sha256": "a" * 64}}

    monkeypatch.setattr(connect_module, "connect", fake_connect)
    argv = ["connect", "--handoff", str(handoff_file), "--credential-file", str(credential_file)]
    if dry_run:
        argv.append("--dry-run")
    assert easy_cli.main(argv) == 0
    output = capsys.readouterr().out
    assert observed == {"path": str(handoff_file), "credential": secret, "dry_run": dry_run}
    assert "https://host.example" in output and "namespace-id" in output and "actor-name" in output
    assert "scope-id" in output and "read, write" in output and "Core 0.4.2" in output
    assert "Public CA SHA-256: " + "a" * 64 in output
    assert secret not in output
    assert ("Handoff validated only" in output) is dry_run


def test_failed_probe_restores_existing_profile_and_credential(tmp_path):
    handoff_path, _doc, _project_id, _repository_id = _handoff(tmp_path)
    root = tmp_path / "config"
    old_profile = _seed_hosted_profile(root)
    stage_credential(root, SECRET_A)
    secret_path = _paths(root)[1]
    secret_snapshot = secret_path.read_bytes()

    def reject_probe(_config_root, _request):
        raise PmtError("remote_compatibility_mismatch", "probe rejected")

    with pytest.raises(PmtError, match="probe rejected"):
        connect_module.connect(root, handoff_path, credential=SECRET_B, configure=reject_probe)

    assert storage_path(root).read_bytes() == old_profile
    assert secret_path.read_bytes() == secret_snapshot
    assert load_credential(root, environ={}) == SECRET_A
    assert not (root / "projects.json").exists()
    assert not (root / "client.json").exists()


def test_explicit_environment_conflict_rejects_before_writes(tmp_path):
    handoff_path, _doc, _project_id, _repository_id = _handoff(tmp_path)
    root = tmp_path / "config"
    with pytest.raises(PmtError) as error:
        connect_module.connect(root, handoff_path, credential=SECRET_B,
                               environ={ENV_NAME: SECRET_A}, configure=_publishing_configure)
    assert error.value.code == "credential_conflict"
    assert not root.exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_connect_rejects_loopback_http_before_writes_even_for_dry_run(tmp_path, dry_run):
    handoff_path, document, _project_id, _repository_id = _handoff(tmp_path)
    document["host"]["url"] = "http://127.0.0.1:18766"
    handoff_path.write_text(json.dumps(document), encoding="utf-8")
    # X-02 retains its direct validation-only diagnostic allowance; client profiles stay HTTPS-only.
    assert handoff.validate_handoff(document, allow_loopback_http=True)["host"]["url"] == document["host"]["url"]
    root = tmp_path / "config"
    with pytest.raises(PmtError) as error:
        connect_module.connect(root, handoff_path, credential=SECRET_A, dry_run=dry_run,
                               configure=_publishing_configure)
    assert error.value.code == "handoff_invalid"
    assert not root.exists()


def test_dry_run_does_not_create_anything_or_probe(tmp_path):
    handoff_path, _doc, _project_id, _repository_id = _handoff(tmp_path)
    root = tmp_path / "config"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("dry run called configure")

    result = connect_module.connect(root, handoff_path, credential=SECRET_A, dry_run=True,
                                    configure=forbidden)
    assert result["dry_run"] is True
    assert not root.exists()


def test_invalid_ca_fails_before_any_client_mutation(tmp_path):
    handoff_path, _doc, _project_id, _repository_id = _handoff(tmp_path, ca=True)
    root = tmp_path / "config"
    with pytest.raises(PmtError):
        connect_module.connect(root, handoff_path, credential=SECRET_A,
                               configure=_publishing_configure)
    assert not root.exists()


def test_disconnect_removes_only_saved_credential(tmp_path):
    root = tmp_path / "config"
    _seed_hosted_profile(root)
    stage_credential(root, SECRET_A)
    before_profile = storage_path(root).read_bytes()
    assert connect_module.disconnect(root) is True
    assert storage_path(root).read_bytes() == before_profile
    assert not _paths(root)[1].exists()
    assert connect_module.disconnect(root) is False


def test_disconnect_without_credential_is_read_only(tmp_path):
    root = tmp_path / "empty-config"
    assert connect_module.disconnect(root) is False
    assert not root.exists()


def test_storage_status_is_read_only_on_empty_config(tmp_path, monkeypatch, capsys):
    from pmt import easy_cli

    root = tmp_path / "empty-config"
    monkeypatch.setattr(easy_cli.easy_setup, "default_roots", lambda _env: (root, tmp_path / "data"))
    assert easy_cli.main(["storage", "status"]) == 0
    assert "unconfigured" in capsys.readouterr().out
    assert not root.exists()


def test_claude_session_start_handoff_uses_connect_transaction(tmp_path, monkeypatch):
    handoff_path, document, project_id, repository_id = _handoff(tmp_path)
    root, data = tmp_path / "hook-config", tmp_path / "hook-data"
    env_file = tmp_path / "claude-env"
    secret = "session-start-secret-fixture"
    env = {"PMT_CONFIG_ROOT": str(root), "PMT_DATA_ROOT": str(data),
           "CLAUDE_PLUGIN_OPTION_HANDOFF_FILE": str(handoff_path),
           "CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL": secret,
           "CLAUDE_ENV_FILE": str(env_file)}
    observed = {"configure_calls": 0}

    class FakeHost:
        def __init__(self, endpoint, credential_env, device_id, environment_id, namespace_id, ca_file):
            self.device_id, self.environment_id, self.namespace_id = device_id, environment_id, namespace_id

        def check_compatibility(self):
            return {"compatible": True, "actor": document["device"]["actor"],
                    "device_id": self.device_id, "namespace_id": self.namespace_id,
                    "core_version": "0.4.1", "db_schema": 5, "graph_schema": 1,
                    "protocol_versions": [1], "scopes": document["device"]["scopes"], "permissions": []}

        def register_session(self, session_id):
            return {"session_id": session_id, "environment_id": self.environment_id,
                    "device_id": self.device_id}

    def configure(config_root, request):
        observed["configure_calls"] += 1
        from pmt.storage_config import configure_storage
        return configure_storage(config_root, request, store_factory=FakeHost)

    prepared = {}

    def real_prepare(environ, cwd, *, product):
        result = easy_setup.prepare(environ, cwd, product=product, configure=configure)
        prepared.update(result)
        return result

    bridge = {}

    def actual_hook_bridge(argv):
        bridge["argv"] = argv
        bridge["credential_available_to_core"] = env.get(ENV_NAME) == secret
        print('{"continue":true}')
        return 0

    monkeypatch.setattr(easy_hook, "bridge_main", actual_hook_bridge)
    native = {"cwd": str(tmp_path), "hook_event_name": "SessionStart", "session_id": "d2-session"}
    stdin = io.TextIOWrapper(io.BytesIO(json.dumps(native).encode("utf-8")), encoding="utf-8")
    stdout = io.StringIO()
    assert easy_hook.run(["--event", "SessionStart"], stdin=stdin, stdout=stdout,
                         environ=env, prepare=real_prepare) == 0

    assert observed["configure_calls"] == 1
    assert bridge["argv"] == ["--product", "claude", "--event", "SessionStart"]
    assert bridge["credential_available_to_core"] is True
    assert prepared["status"] == "ready" and prepared["mode"] == "hosted"
    assert ENV_NAME not in prepared["env"]
    assert "session-start-secret-fixture" not in env_file.read_text(encoding="utf-8")
    registry = json.loads((root / "projects.json").read_text(encoding="utf-8"))
    assert registry["projects"][0]["project_id"] == project_id
    assert registry["projects"][0]["repository_id"] == repository_id
    metadata = json.loads((root / "client.json").read_text(encoding="utf-8"))
    assert metadata["source"] == "connect"
    assert not (data / "pmt.sqlite3").exists()


def test_later_storage_writer_is_preserved_with_credential_and_ca(tmp_path, monkeypatch):
    handoff_path, document, _project_id, _repository_id = _handoff(tmp_path)
    ca_bytes = b"test CA bytes for write-order fixture"
    document["host"].update(ca_pem=ca_bytes.decode(), ca_sha256=hashlib.sha256(ca_bytes).hexdigest())
    monkeypatch.setattr(connect_module.handoff, "load_handoff", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    root = tmp_path / "config"
    ca_path = root / "host-ca" / f"{document['namespace_id']}.pem"
    foreign_profile = b'{"writer":"later"}\n'

    def concurrent_configure(config_root, _request):
        path = storage_path(config_root)
        owned_profile = b'{"writer":"connect"}\n'
        path.write_bytes(owned_profile)
        result = {"actor": "d2-test", "config_sha256": hashlib.sha256(owned_profile).hexdigest()}
        path.write_bytes(foreign_profile)
        return result

    with pytest.raises(PmtError) as error:
        connect_module.connect(root, handoff_path, credential=SECRET_B, configure=concurrent_configure)

    assert error.value.code == "storage_config_conflict"
    assert storage_path(root).read_bytes() == foreign_profile
    assert load_credential(root, environ={}) == SECRET_B
    assert ca_path.read_bytes() == ca_bytes
    assert (root / "profile.json").is_file()
    assert not (root / "projects.json").exists()
    assert not (root / "client.json").exists()


def test_ca_change_before_stage_is_not_overwritten(tmp_path, monkeypatch):
    handoff_path, document, _project_id, _repository_id = _handoff(tmp_path)
    ca_bytes = b"test CA bytes for compare-and-swap fixture"
    document["host"].update(ca_pem=ca_bytes.decode(), ca_sha256=hashlib.sha256(ca_bytes).hexdigest())
    monkeypatch.setattr(connect_module.handoff, "load_handoff", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    root = tmp_path / "config"
    ca_path = root / "host-ca" / f"{document['namespace_id']}.pem"
    foreign_ca = b"concurrent CA writer"
    real_stage = connect_module.stage_credential

    def stage_then_concurrent_ca(config_root, value):
        result = real_stage(config_root, value)
        ca_path.parent.mkdir(parents=True, exist_ok=True)
        ca_path.write_bytes(foreign_ca)
        return result

    monkeypatch.setattr(connect_module, "stage_credential", stage_then_concurrent_ca)
    with pytest.raises(PmtError) as error:
        connect_module.connect(root, handoff_path, credential=SECRET_B, configure=_publishing_configure)

    assert error.value.code == "storage_config_conflict"
    assert ca_path.read_bytes() == foreign_ca
    assert not storage_path(root).exists()
    assert not (root / "profile.json").exists()
    assert not connect_module.has_credential_store(root)


def test_ca_change_during_probe_fails_connect_and_preserves_foreign_ca(tmp_path, monkeypatch):
    handoff_path, document, _project_id, _repository_id = _handoff(tmp_path)
    ca_bytes = b"test CA bytes"
    foreign_ca = b"newer CA writer"
    document["host"].update(ca_pem=ca_bytes.decode(), ca_sha256=hashlib.sha256(ca_bytes).hexdigest())
    monkeypatch.setattr(connect_module.handoff, "load_handoff", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    root = tmp_path / "config"
    ca_path = root / "host-ca" / f"{document['namespace_id']}.pem"

    def configure_and_update_ca(config_root, request):
        result = _publishing_configure(config_root, request)
        ca_path.write_bytes(foreign_ca)
        return result

    with pytest.raises(PmtError) as error:
        connect_module.connect(root, handoff_path, credential=SECRET_B, configure=configure_and_update_ca)

    assert error.value.code == "storage_config_conflict"
    assert not storage_path(root).exists()
    assert not (root / "profile.json").exists()
    assert not connect_module.has_credential_store(root)
    assert ca_path.read_bytes() == foreign_ca


def test_rollback_keeps_project_registry_writer_after_connect_merge(tmp_path, monkeypatch):
    handoff_path, _document, _project_id, _repository_id = _handoff(tmp_path)
    root = tmp_path / "config"
    concurrent = {"name": "concurrent", "project_id": str(uuid.uuid4()),
                 "repository_id": str(uuid.uuid4()), "repository_name": "other-repo"}

    def fail_after_concurrent_registry_write(config_root, **_kwargs):
        from pmt.client_setup import local_commands
        path = Path(config_root) / "projects.json"
        with local_commands._state_lock(config_root, "projects"):
            registry = json.loads(path.read_text(encoding="utf-8"))
            registry["projects"].append(concurrent)
            local_commands._write_json(path, registry)
        raise PmtError("client_metadata_write_failed", "fixture")

    monkeypatch.setattr(client, "write_client_metadata_snapshot", fail_after_concurrent_registry_write)
    with pytest.raises(PmtError) as error:
        connect_module.connect(root, handoff_path, credential=SECRET_B, configure=_publishing_configure)

    assert error.value.code == "storage_config_conflict"
    registry = json.loads((root / "projects.json").read_text(encoding="utf-8"))
    assert registry["projects"][-1] == concurrent
    assert len(registry["projects"]) == 2
    assert not storage_path(root).exists()
    assert not connect_module.has_credential_store(root)


@pytest.mark.parametrize("write_after_restore", [False, True])
def test_rollback_storage_cas_conflict_preserves_dependent_files(tmp_path, monkeypatch, write_after_restore):
    handoff_path, document, _project_id, _repository_id = _handoff(tmp_path)
    ca_bytes = b"test CA bytes for rollback interleaving fixture"
    document["host"].update(ca_pem=ca_bytes.decode(), ca_sha256=hashlib.sha256(ca_bytes).hexdigest())
    monkeypatch.setattr(connect_module.handoff, "load_handoff", lambda *_args, **_kwargs: document)
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: object())
    root = tmp_path / "config"
    ca_path = root / "host-ca" / f"{document['namespace_id']}.pem"
    foreign_profile = b'{"writer":"concurrent"}\n'
    real_writer = client.write_client_metadata_snapshot
    real_restore = connect_module._restore_file_locked

    def fail_after_metadata_write(config_root, **kwargs):
        real_writer(config_root, **kwargs)
        raise PmtError("client_metadata_write_failed", "fixture")

    def insert_storage_writer(path, before, *, expected_after):
        is_storage = path == storage_path(root)
        if is_storage and not write_after_restore:
            path.write_bytes(foreign_profile)
            return real_restore(path, before, expected_after=expected_after)
        result = real_restore(path, before, expected_after=expected_after)
        if is_storage and result and write_after_restore:
            path.write_bytes(foreign_profile)
        return result

    monkeypatch.setattr(client, "write_client_metadata_snapshot", fail_after_metadata_write)
    monkeypatch.setattr(connect_module, "_restore_file_locked", insert_storage_writer)
    with pytest.raises(PmtError) as error:
        connect_module.connect(root, handoff_path, credential=SECRET_B, configure=_publishing_configure)

    assert error.value.code == "storage_config_conflict"
    assert storage_path(root).read_bytes() == foreign_profile
    assert load_credential(root, environ={}) == SECRET_B
    assert ca_path.read_bytes() == ca_bytes
    assert (root / "profile.json").is_file()
