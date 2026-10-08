import json
import io
import os
import sqlite3
import stat
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from pmt.errors import PmtError
from pmt import hooks as core_hooks

from pmt.client_setup import credentials, mode
from pmt.client_setup.client import write_client_metadata, read_client_metadata
from pmt.storage_config import _read_profile
from pmt import easy_hook, easy_setup


def test_c01_empty_profile_starts_local_without_host_settings(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    result = mode.prepare({"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data), "HOME": str(tmp_path)}, tmp_path)
    assert result["status"] == "ready" and result["mode"] == "local"
    assert (data / "pmt.sqlite3").is_file()
    assert _read_profile(config)[0]["mode"] == "local"
    with sqlite3.connect(data / "pmt.sqlite3") as db:
        assert db.execute("SELECT value FROM meta WHERE key='installation:claude'").fetchone()
    assert "PMT_HOST_CREDENTIAL" not in result["env"]


def test_c01_local_settings_do_not_switch_and_hosted_missing_never_opens_local(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data), "HOME": str(tmp_path)}
    mode.prepare(env, tmp_path)
    before = (config / "storage.json").read_bytes()
    env["CLAUDE_PLUGIN_OPTION_HOST_URL"] = "https://example.invalid"
    local = mode.prepare(env, tmp_path)
    assert local["status"] == "ready" and local["mode"] == "local" and local["message"]
    assert before == (config / "storage.json").read_bytes()
    (config / "storage.json").write_text(json.dumps({
        "schema_version": 1, "revision": 2, "mode": "hosted",
        "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
        "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
        "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"
    }), encoding="utf-8")
    (data / "pmt.sqlite3").unlink()
    missing = mode.prepare({"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)}, tmp_path)
    assert missing["status"] == "error" and missing["error_code"] == "hosted_settings_missing"
    assert not (data / "pmt.sqlite3").exists()


def test_c01_changed_host_settings_use_current_hash_and_preserve_old_profile(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    original = {
        "schema_version": 1, "revision": 3, "mode": "hosted",
        "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836",
        "workspace_mappings": [{"repository_id": "bdd8eaf4-cb0a-4971-90d7-8d7965f41fc7",
                                 "project_id": "4c3d4d19-28fa-4727-9895-e0fee0f43d6a",
                                 "branch": "main", "branch_key_sha256": ""
                                 , "local_root": str(tmp_path), "relative_graph_path": "docs/pmt-docs/graph.json"}],
        "endpoint": "https://old.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
        "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"
    }
    import hashlib
    original["workspace_mappings"][0]["branch_key_sha256"] = hashlib.sha256(b"main").hexdigest()
    path = config / "storage.json"
    path.write_text(json.dumps(original), encoding="utf-8")
    before = path.read_bytes()
    calls = []
    def reject_probe(config_root, request):
        calls.append(request)
        raise PmtError("remote_unavailable", "fixture probe failure")
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data),
           "CLAUDE_PLUGIN_OPTION_HOST_URL": "https://new.invalid",
           "CLAUDE_PLUGIN_OPTION_DEVICE_ID": original["device_id"],
           "CLAUDE_PLUGIN_OPTION_NAMESPACE_ID": original["namespace_id"],
           "CLAUDE_PLUGIN_OPTION_ACTOR": "fixture", "CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL": "secret"}
    result = mode.prepare(env, tmp_path, configure=reject_probe)
    assert result["status"] == "error" and result["error_code"] == "remote_unavailable"
    assert len(calls) == 1 and calls[0]["expected_config_sha256"] == _read_profile(config)[1]
    assert calls[0]["workspace_mappings"] == _read_profile(config)[0]["workspace_mappings"]
    assert path.read_bytes() == before and not (data / "pmt.sqlite3").exists()


def test_c01_rejected_probe_restores_previous_credential_and_profile(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    hosted = {"schema_version": 1, "revision": 1, "mode": "hosted",
              "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
              "endpoint": "https://old.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
              "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
              "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"}
    profile_path = config / "storage.json"
    profile_path.write_text(json.dumps(hosted), encoding="utf-8")
    old_profile = profile_path.read_bytes()
    credential_path = credentials.store_credential(config, "credential-A")
    old_credential = credential_path.read_bytes()
    def reject_probe(*_args):
        raise PmtError("remote_unavailable", "fixture probe failure")
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(tmp_path / "data"),
           "CLAUDE_PLUGIN_OPTION_HOST_URL": "https://new.invalid",
           "CLAUDE_PLUGIN_OPTION_DEVICE_ID": hosted["device_id"],
           "CLAUDE_PLUGIN_OPTION_NAMESPACE_ID": hosted["namespace_id"],
           "CLAUDE_PLUGIN_OPTION_ACTOR": "fixture", "CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL": "credential-B"}
    result = mode.prepare(env, tmp_path, configure=reject_probe)
    assert result["status"] == "error" and result["error_code"] == "remote_unavailable"
    assert profile_path.read_bytes() == old_profile and credential_path.read_bytes() == old_credential
    assert credentials.load_credential(config, {}) == "credential-A"


def test_c04_changed_credential_probes_matching_profile_before_acceptance(tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    hosted = {"schema_version": 1, "revision": 1, "mode": "hosted",
              "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
              "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
              "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
              "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"}
    profile_path = config / "storage.json"
    profile_path.write_text(json.dumps(hosted), encoding="utf-8")
    old_profile = profile_path.read_bytes()
    credential_path = credentials.store_credential(config, "credential-A")
    old_credential = credential_path.read_bytes()
    configured, probed = [], []
    def unexpected_configure(*args):
        configured.append(args)
        raise AssertionError("matching profile must not be republished")
    def reject_probe(root):
        probed.append(root)
        raise PmtError("unauthenticated", "invalid changed credential")
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(tmp_path / "data"),
           "CLAUDE_PLUGIN_OPTION_HOST_URL": hosted["endpoint"],
           "CLAUDE_PLUGIN_OPTION_DEVICE_ID": hosted["device_id"],
           "CLAUDE_PLUGIN_OPTION_NAMESPACE_ID": hosted["namespace_id"],
           "CLAUDE_PLUGIN_OPTION_ACTOR": hosted["actor"],
           "CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL": "credential-B"}
    result = mode.prepare(env, tmp_path, configure=unexpected_configure, probe=reject_probe)
    assert result["status"] == "error" and result["error_code"] == "unauthenticated"
    assert configured == [] and probed == [str(config)]
    assert profile_path.read_bytes() == old_profile and credential_path.read_bytes() == old_credential
    assert credentials.load_credential(config, {}) == "credential-A"


def test_c05_explicit_roots_and_windows_defaults(monkeypatch, tmp_path):
    env = {"PMT_CONFIG_ROOT": str(tmp_path / "explicit-c"), "PMT_DATA_ROOT": str(tmp_path / "explicit-d"),
           "APPDATA": str(tmp_path / "roaming"), "LOCALAPPDATA": str(tmp_path / "local"), "HOME": str(tmp_path)}
    monkeypatch.setattr(mode.os, "name", "nt")
    config, data = mode.default_roots(env)
    assert config == tmp_path / "explicit-c" and data == tmp_path / "explicit-d"
    env.pop("PMT_CONFIG_ROOT")
    env.pop("PMT_DATA_ROOT")
    config, data = mode.default_roots(env)
    assert config == tmp_path / "roaming" / "pmt"
    assert data == tmp_path / "local" / "pmt" / "data"


def test_c05_windows_legacy_config_keeps_existing_profile_path(monkeypatch, tmp_path):
    legacy = tmp_path / ".config" / "pmt"
    legacy.mkdir(parents=True)
    (legacy / "storage.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(mode.os, "name", "nt")
    config, data = mode.default_roots({"HOME": str(tmp_path), "APPDATA": str(tmp_path / "roaming"),
                                      "LOCALAPPDATA": str(tmp_path / "local")})
    assert config == legacy and data == tmp_path / "local" / "pmt" / "data"


def test_c05_existing_windows_legacy_data_is_preserved(monkeypatch, tmp_path):
    legacy = tmp_path / ".config" / "pmt"
    legacy.mkdir(parents=True)
    (legacy / "storage.json").write_text("{}", encoding="utf-8")
    old_data = tmp_path / ".local" / "share" / "pmt" / "data"
    old_data.mkdir(parents=True)
    monkeypatch.setattr(mode.os, "name", "nt")
    config, data = mode.default_roots({"HOME": str(tmp_path), "APPDATA": str(tmp_path / "roaming"),
                                      "LOCALAPPDATA": str(tmp_path / "local")})
    assert config == legacy and data == old_data


def test_c04_explicit_credential_wins_and_is_excluded_from_hook_environment(tmp_path):
    env = {"HOME": str(tmp_path), "PMT_HOST_CREDENTIAL": "explicit-secret"}
    result = mode.prepare(env, tmp_path)
    assert result["mode"] == "local"
    assert "PMT_HOST_CREDENTIAL" not in result["env"]
    options, missing = mode.read_options({"CLAUDE_PLUGIN_OPTION_HOST_URL": "https://host.invalid",
        "CLAUDE_PLUGIN_OPTION_DEVICE_ID": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "CLAUDE_PLUGIN_OPTION_NAMESPACE_ID": "73032707-440f-4123-9189-1ff7f046f043",
        "CLAUDE_PLUGIN_OPTION_ACTOR": "fixture", "PMT_HOST_CREDENTIAL": "explicit-secret"})
    assert not missing and options["device_credential"] == "explicit-secret"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission checks require a POSIX filesystem")
def test_c04_unix_store_permissions_and_insecure_rejection(tmp_path):
    credentials.store_credential(tmp_path, "credential-fixture")
    directory = tmp_path / "secrets"
    path = directory / "host-credential"
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert credentials.load_credential(tmp_path, {}) == "credential-fixture"
    path.chmod(0o644)
    with pytest.raises(Exception, match="permissions") as error:
        credentials.load_credential(tmp_path, {})
    assert getattr(error.value, "code", None) == "credential_store_insecure"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI validation runs on Windows only")
def test_c04_windows_dpapi_round_trip_and_corrupt_data(tmp_path):
    credentials.store_credential(tmp_path, "credential-fixture")
    path = tmp_path / "secrets" / "host-credential.dpapi"
    assert credentials.load_credential(tmp_path, {}) == "credential-fixture"
    path.write_bytes(b"invalid-dpapi-payload")
    with pytest.raises(Exception) as error:
        credentials.load_credential(tmp_path, {})
    assert getattr(error.value, "code", None) == "credential_store_unreadable"


@pytest.mark.parametrize("directory_mode,file_mode,directory_uid,file_uid", [
    (0o700, 0o600, 41, 41), (0o770, 0o600, 41, 41), (0o700, 0o644, 41, 41),
    (0o700, 0o600, 42, 41), (0o700, 0o600, 41, 42),
])
def test_c04_posix_permission_policy_from_stat_metadata(directory_mode, file_mode, directory_uid, file_uid):
    directory_stat = SimpleNamespace(st_uid=directory_uid, st_mode=stat.S_IFDIR | directory_mode)
    file_stat = SimpleNamespace(st_uid=file_uid, st_mode=stat.S_IFREG | file_mode)
    if directory_mode == 0o700 and file_mode == 0o600 and directory_uid == file_uid == 41:
        credentials._validate_unix_metadata(directory_stat, file_stat, 41)
    else:
        with pytest.raises(PmtError) as error:
            credentials._validate_unix_metadata(directory_stat, file_stat, 41)
        assert error.value.code == "credential_store_insecure"


def test_c04_reparse_point_metadata_is_rejected_cross_platform():
    attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    info = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=attributes)
    assert credentials._is_reparse_or_symlink(Path("fixture"), info)


def test_c04_failed_atomic_replace_keeps_previous_credential(monkeypatch, tmp_path):
    path = credentials.store_credential(tmp_path, "credential-A")
    original = path.read_bytes()
    monkeypatch.setattr(credentials.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("fixture")))
    with pytest.raises(PmtError) as error:
        credentials.store_credential(tmp_path, "credential-B")
    assert error.value.code == "credential_store_unreadable"
    assert path.read_bytes() == original


def test_c04_rollback_does_not_overwrite_a_later_credential_writer(tmp_path):
    credentials.store_credential(tmp_path, "credential-A")
    old, staged, changed = credentials.stage_credential(tmp_path, "credential-B")
    assert changed and old is not None and staged is not None
    credentials.store_credential(tmp_path, "credential-C")
    assert credentials.restore_credential(tmp_path, old, expected_current=staged) is False
    assert credentials.load_credential(tmp_path, {}) == "credential-C"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink fixture needs POSIX link support")
def test_c04_rejects_symlinked_credential_directory(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (tmp_path / "secrets").symlink_to(target, target_is_directory=True)
    with pytest.raises(PmtError) as error:
        credentials.store_credential(tmp_path, "credential-fixture")
    assert error.value.code == "credential_store_insecure"


def test_c10_common_option_reader_uses_handoff_loader(monkeypatch, tmp_path):
    calls = []
    document = {"host": {"url": "https://host.invalid"}, "device": {"device_id": "id", "actor": "actor"},
                "namespace_id": "namespace"}
    monkeypatch.setattr(mode.handoff, "load_handoff", lambda path: calls.append(path) or document)
    options, missing = mode.read_options({"PMT_HANDOFF_FILE": "handoff.json", "PMT_DEVICE_CREDENTIAL": "secret"},
                                         product="codex")
    assert calls == ["handoff.json"] and not missing
    assert options["host_url"] == "https://host.invalid" and options["device_credential"] == "secret"


def test_c04_env_file_never_contains_credential_even_if_passed(tmp_path):
    path = tmp_path / "claude-env"
    easy_setup.write_env_file(path, {"PMT_CONFIG_ROOT": str(tmp_path), "PMT_HOST_CREDENTIAL": "private-fixture"})
    assert "private-fixture" not in path.read_text(encoding="utf-8")


def test_c10_shared_hook_preserves_event_and_keeps_env_file_secret_free(monkeypatch, tmp_path):
    env_file = tmp_path / "claude-env"
    captured = {}
    def prepare(environ, cwd, *, product):
        captured["product"] = product
        return {"status": "ready", "env": {"PMT_CONFIG_ROOT": str(tmp_path)}, "link": "not_linked"}
    def bridge(argv):
        captured["argv"] = argv
        print('{"continue":true}')
        return 0
    monkeypatch.setattr(easy_hook, "bridge_main", bridge)
    stdin = io.TextIOWrapper(io.BytesIO(b'{"cwd":"fixture"}'), encoding="utf-8")
    stdout = io.StringIO()
    result = easy_hook.run(["--product", "codex", "--event", "SessionStart"], stdin=stdin, stdout=stdout,
                           environ={"CLAUDE_ENV_FILE": str(env_file)}, prepare=prepare)
    assert result == 0 and captured == {"product": "codex", "argv": ["--product", "codex", "--event", "SessionStart"]}
    assert json.loads(stdout.getvalue())["continue"] is True
    assert not env_file.exists()


def test_c01_invalid_handoff_is_a_sanitized_nonblocking_hook_error(tmp_path):
    missing_path = tmp_path / "private-handoff-name.json"
    env = {"CLAUDE_PLUGIN_OPTION_HANDOFF_FILE": str(missing_path),
           "CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL": "credential-fixture"}
    result = mode.prepare(env, tmp_path)
    assert result["status"] == "error" and result["error_code"] == "handoff_invalid"
    assert str(missing_path) not in result["message"] and "credential-fixture" not in result["message"]
    stdin = io.TextIOWrapper(io.BytesIO(b'{"cwd":"fixture"}'), encoding="utf-8")
    stdout = io.StringIO()
    assert easy_hook.run(["--event", "SessionStart"], stdin=stdin, stdout=stdout, environ=env) == 0
    assert "handoff_invalid" in stdout.getvalue()
    assert str(missing_path) not in stdout.getvalue() and "credential-fixture" not in stdout.getvalue()


def test_c10_codex_uses_hosted_profile_and_protected_credential_without_options(monkeypatch, tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    hosted = {"schema_version": 1, "revision": 1, "mode": "hosted",
              "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
              "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
              "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
              "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"}
    (config / "storage.json").write_text(json.dumps(hosted), encoding="utf-8")
    credentials.store_credential(config, "codex-credential")
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)}
    result = mode.prepare(env, tmp_path, product="codex")
    assert result["status"] == "ready" and result["mode"] == "hosted"
    assert env["PMT_HOST_CREDENTIAL"] == "codex-credential"
    assert not (data / "pmt.sqlite3").exists()
    env.pop("PMT_HOST_CREDENTIAL")
    env.pop("PMT_CONFIG_ROOT")
    env.pop("PMT_DATA_ROOT")
    monkeypatch.setattr(mode, "default_roots", lambda _env: (config, data))
    observed = []
    def bridge(_argv):
        observed.append(env.get("PMT_HOST_CREDENTIAL") == "codex-credential")
        print('{"continue":true}')
        return 0
    monkeypatch.setattr(easy_hook, "bridge_main", bridge)
    stdin = io.TextIOWrapper(io.BytesIO(b'{"cwd":"fixture"}'), encoding="utf-8")
    stdout = io.StringIO()
    assert easy_hook.run(["--product", "codex", "--event", "SessionStart"], stdin=stdin,
                         stdout=stdout, environ=env) == 0
    assert observed == [True]
    assert "codex-credential" not in stdout.getvalue()


@pytest.mark.parametrize("product", ["claude", "codex"])
def test_c10_actual_core_parser_receives_product_for_both_products(monkeypatch, tmp_path, product):
    captured = []
    monkeypatch.setattr(core_hooks, "process_session_start",
                        lambda passed_product, raw: captured.append((passed_product, raw)) or {"continue": True})
    def prepare(_environ, _cwd, *, product):
        return {"status": "ready", "mode": "local", "env": {}, "link": "not_linked"}
    stdin = io.TextIOWrapper(io.BytesIO(b'{"cwd":"fixture","session_id":"s"}'), encoding="utf-8")
    stdout = io.StringIO()
    argv = ["--event", "SessionStart", "--with-context"]
    if product == "codex":
        argv = ["--product", product, *argv]
    result = easy_hook.run(argv, stdin=stdin, stdout=stdout, environ={}, prepare=prepare)
    assert result == 0
    assert captured == [(product, {"cwd": "fixture", "session_id": "s"})]
    assert json.loads(stdout.getvalue())["continue"] is True


def test_c10_incomplete_product_arguments_are_nonblocking(tmp_path):
    stdin = io.TextIOWrapper(io.BytesIO(b'{"cwd":"fixture"}'), encoding="utf-8")
    stdout = io.StringIO()
    result = easy_hook.run(["--product", "--event", "SessionStart"], stdin=stdin, stdout=stdout, environ={})
    assert result == 0 and "hook_arguments_invalid" in stdout.getvalue()


def test_c10_legacy_hosted_hook_loads_credential_before_bridge(monkeypatch, tmp_path):
    config = tmp_path / "config"
    config.mkdir()
    hosted = {"schema_version": 1, "revision": 1, "mode": "hosted",
              "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
              "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
              "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
              "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"}
    (config / "storage.json").write_text(json.dumps(hosted), encoding="utf-8")
    credentials.store_credential(config, "legacy-credential")
    write_client_metadata(config, source="legacy", python_path=sys.executable, mode="hosted")
    env_file = tmp_path / "claude-env"
    env = {"PMT_CONFIG_ROOT": str(config), "CLAUDE_ENV_FILE": str(env_file)}
    observed = []
    def bridge(_argv):
        observed.append(env.get("PMT_HOST_CREDENTIAL") == "legacy-credential")
        print('{"continue":true}')
        return 0
    monkeypatch.setattr(core_hooks, "process_hook", lambda product, event, _raw: bridge([product, event]))
    stdin = io.TextIOWrapper(io.BytesIO(b'{"cwd":"fixture"}'), encoding="utf-8")
    stdout = io.StringIO()
    assert easy_hook.run(["--event", "SessionStart"], stdin=stdin, stdout=stdout, environ=env) == 0
    assert observed == [True]
    assert "legacy-credential" not in stdout.getvalue()
    assert not env_file.exists()


def test_easy_hook_empty_explicit_root_runs_managed_local_setup(monkeypatch, tmp_path):
    config, data = tmp_path / "empty-config", tmp_path / "empty-data"
    captured = []
    monkeypatch.setattr(core_hooks, "process_session_start",
                        lambda product, _raw: captured.append(product) or {"continue": True})
    stdin = io.TextIOWrapper(io.BytesIO(b'{"cwd":"fixture"}'), encoding="utf-8")
    stdout = io.StringIO()
    result = easy_hook.run(["--event", "SessionStart", "--with-context"], stdin=stdin, stdout=stdout,
                           environ={"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)})
    assert result == 0 and captured == ["claude"]
    assert _read_profile(config)[0]["mode"] == "local"
    assert (data / "pmt.sqlite3").is_file()
    metadata = read_client_metadata(config)
    assert metadata["source"] == "plugin" and metadata["last_mode"] == "local"
    assert metadata["python_path"]
    assert "initialized local storage" in stdout.getvalue()


def test_easy_hook_managed_hosted_profile_removed_options_stops_without_bridge(monkeypatch, tmp_path):
    config, data = tmp_path / "managed-config", tmp_path / "managed-data"
    config.mkdir()
    hosted = {"schema_version": 1, "revision": 1, "mode": "hosted",
              "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
              "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
              "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
              "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"}
    (config / "storage.json").write_text(json.dumps(hosted), encoding="utf-8")
    credentials.store_credential(config, "managed-credential")
    write_client_metadata(config, source="plugin", python_path=sys.executable, mode="hosted")
    bridge_called = []
    setup_errors = []
    actual_prepare = easy_setup.prepare
    def observe_prepare(environ, cwd, *, product):
        result = actual_prepare(environ, cwd, product=product)
        setup_errors.append(result.get("error_code"))
        return result
    monkeypatch.setattr(core_hooks, "process_session_start", lambda *_args: bridge_called.append(True))
    stdin = io.TextIOWrapper(io.BytesIO(b'{"cwd":"fixture"}'), encoding="utf-8")
    stdout = io.StringIO()
    result = easy_hook.run(["--event", "SessionStart", "--with-context"], stdin=stdin, stdout=stdout,
                           environ={"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)},
                           prepare=observe_prepare)
    assert result == 0 and bridge_called == []
    assert setup_errors == ["hosted_settings_missing"]
    assert "Hosted PMT settings are missing" in stdout.getvalue()
    assert not (data / "pmt.sqlite3").exists()


def test_codex_session_preserves_claude_plugin_origin(monkeypatch, tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    hosted = {"schema_version": 1, "revision": 1, "mode": "hosted",
              "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
              "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
              "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
              "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"}
    (config / "storage.json").write_text(json.dumps(hosted), encoding="utf-8")
    credentials.store_credential(config, "same-root-credential")
    write_client_metadata(config, source="plugin", python_path=sys.executable, mode="hosted")
    monkeypatch.setattr(mode, "default_roots", lambda _env: (config, data))
    env = {}
    assert mode.prepare(env, tmp_path, product="codex")["mode"] == "hosted"
    assert read_client_metadata(config)["source"] == "plugin"


def test_client_metadata_keeps_harmless_extensions_and_drops_secret_fields(tmp_path):
    path = tmp_path / "client.json"
    path.write_text(json.dumps({"schema_version": 1, "source": "plugin", "python_path": "old-python",
                                "last_mode": "local", "future_option": "preserve",
                                "api_token": "must-not-copy"}), encoding="utf-8")
    write_client_metadata(tmp_path, source="plugin", python_path="new-python", mode="hosted")
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["future_option"] == "preserve"
    assert "api_token" not in value
    assert value["python_path"] == "new-python" and value["last_mode"] == "hosted"
