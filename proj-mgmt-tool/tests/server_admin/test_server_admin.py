from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.host.auth import AuthRegistry
from pmt.lifecycle import create_scope
from pmt.server_admin.config import load_config, load_config_snapshot, publish_config, validate_config
from pmt.server_admin.init import _base_config, _adopt_metadata
from pmt.server_admin.secrets import _check_windows_acl, _dpapi, _set_windows_acl, check_source, store_key


def _config(root, data_root):
    return {
        "schema_version": 1, "revision": 1,
        "paths": {"data_root": str(data_root), "log_dir": str(root / "logs"), "backup_dir": str(root / "backup")},
        "listen": {"host": "127.0.0.1", "port": 18765}, "public_url": "https://127.0.0.1:18765",
        "tls": {"cert_file": str(root / "test.crt"), "key_file": str(root / "test.key")},
        "claim_key": {"key_id": "primary", "source": {"kind": "file", "path": str(root / "key")}, "retained": []},
        "proxy": {"enabled": False, "trusted": []}, "access": {"allowed_sources": []},
        "service": {"kind": "none", "name": "PMT Host", "account": "current", "app_root": str(root)},
        "logging": {"level": "info", "retain_days": 30}, "registry": {"projects": []},
    }


def _cas_writer(root, digest, ready, start, queue, port):
    ready.put(True)
    start.wait(15)
    from pmt.server_admin.cli import main
    import contextlib
    import io
    with contextlib.redirect_stdout(io.StringIO()):
        code = main(["config", "set", "listen.port", str(port), "--config-root", root,
                     "--expected-sha256", digest, "--json"])
    queue.put("won" if code == 0 else "config_conflict")


def _init_writer(root, data, logs, backup, ready, start, queue):
    ready.put(True)
    start.wait(15)
    from pmt.server_admin.cli import main
    import contextlib, io
    argv = ["init", "--config-root", root, "--public-url", "https://127.0.0.1:18765", "--listen", "127.0.0.1:18765",
            "--data-root", data, "--log-dir", logs, "--backup-dir", backup, "--apply", "--json"]
    with contextlib.redirect_stdout(io.StringIO()):
        code = main(argv)
    queue.put("won" if code == 0 else "config_exists")


@pytest.mark.parametrize("mutate,code", [
    (lambda c: c.update(extra=True), "config_invalid"),
    (lambda c: c["listen"].update(port=True), "config_invalid"),
    (lambda c: c["paths"].update(data_root="A" * 48), "config_secret_rejected"),
    (lambda c: c["claim_key"].update(source={"kind": "file", "path": "x", "value": "secret"}), "config_invalid"),
])
def test_schema_rejects_unknown_types_and_secret_material(tmp_path, mutate, code):
    config = _config(tmp_path, tmp_path / "data")
    mutate(config)
    with pytest.raises(PmtError) as error:
        validate_config(config)
    assert error.value.code == code


@pytest.mark.parametrize("mutate", [
    lambda c: c["service"].update(kind=[]),
    lambda c: c["logging"].update(level=[]),
    lambda c: c["claim_key"].update(source={"kind": [], "path": "x"}),
    lambda c: c["access"].update(allowed_sources=["not-a-network"]),
    lambda c: c["proxy"].update(trusted=["proxy.local"]),
    lambda c: c["tls"].update(cert_file="~/host.crt", key_file="~/host.key"),
])
def test_nested_schema_failures_are_safe_pmt_errors(tmp_path, mutate):
    config = _config(tmp_path, tmp_path / "data")
    mutate(config)
    with pytest.raises(PmtError) as error:
        validate_config(config)
    assert error.value.code == "config_invalid"


def test_config_loader_rejects_duplicate_keys_and_oversized_json(tmp_path):
    path = tmp_path / "host-config.json"
    path.write_bytes(b'{"schema_version":1,"schema_version":1}')
    with pytest.raises(PmtError): load_config_snapshot(path)
    path.write_bytes(b" " * (1024 * 1024 + 1))
    with pytest.raises(PmtError): load_config_snapshot(path)


def test_config_cas_serializes_independent_processes_with_one_winner(tmp_path, monkeypatch):
    # Spawned children must find the package before the same-named CLI launcher.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'src'))
    root, data_root = tmp_path / "config", tmp_path / "data"
    root.mkdir()
    initial = _config(root, data_root)
    path = publish_config(root, initial, create=True)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    context = multiprocessing.get_context("spawn")
    ready, start, output = context.Queue(), context.Event(), context.Queue()
    workers = [context.Process(target=_cas_writer, args=(str(root), digest, ready, start, output, port)) for port in (19001, 19002)]
    for worker in workers: worker.start()
    assert ready.get(timeout=20) and ready.get(timeout=20)
    start.set()
    results = [output.get(timeout=20), output.get(timeout=20)]
    for worker in workers:
        worker.join(timeout=20)
        assert worker.exitcode == 0
    assert results.count("won") == 1
    assert results.count("config_conflict") == 1
    assert load_config(path)["revision"] == 2


def test_concurrent_init_has_one_winner(tmp_path, monkeypatch):
    # Spawned children must find the package before the same-named CLI launcher.
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'src'))
    context = multiprocessing.get_context("spawn")
    ready, start, output = context.Queue(), context.Event(), context.Queue()
    roots = [tmp_path / name for name in ("config", "data", "logs", "backup")]
    workers = [context.Process(target=_init_writer, args=(*map(str, roots), ready, start, output)) for _ in range(2)]
    for worker in workers: worker.start()
    assert ready.get(timeout=20) and ready.get(timeout=20)
    start.set()
    results = [output.get(timeout=30), output.get(timeout=30)]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    assert results.count("won") == 1
    assert results.count("config_exists") == 1
    assert (roots[1] / "pmt.sqlite3").exists()


def test_init_plan_apply_and_existing_partial_state_are_preserved(tmp_path, capsys):
    from pmt.server_admin.cli import main
    config, data = tmp_path / "config", tmp_path / "data"
    common = ["--config-root", str(config), "init", "--public-url", "https://127.0.0.1:18765",
              "--listen", "127.0.0.1:18765", "--data-root", str(data), "--log-dir", str(tmp_path / "logs"), "--backup-dir", str(tmp_path / "backup")]
    assert main(common + ["--json"]) == 0
    assert not config.exists() and not data.exists()
    assert main(common + ["--apply", "--json"]) == 0
    applied = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert applied["applied"] and applied["namespace_id"]
    saved = load_config(config / "host-config.json")
    assert saved["claim_key"]["source"]["kind"] in {"file", "dpapi"}
    assert (data / "pmt.sqlite3").exists()
    assert main(common + ["--apply", "--json"]) != 0
    assert load_config(config / "host-config.json")["claim_key"] == saved["claim_key"]


def test_init_refuses_corrupt_partial_state_without_changing_it(tmp_path, capsys):
    from pmt.server_admin.cli import main
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir(); data.mkdir()
    corrupt = data / "pmt.sqlite3"
    corrupt.write_bytes(b"corrupt fixture")
    before = hashlib.sha256(corrupt.read_bytes()).hexdigest()
    args = ["--config-root", str(config), "init", "--public-url", "https://127.0.0.1:18765",
            "--listen", "127.0.0.1:18765", "--data-root", str(data), "--apply", "--json"]
    assert main(args) != 0
    assert hashlib.sha256(corrupt.read_bytes()).hexdigest() == before
    assert not (config / "host-config.json").exists()


def test_init_rolls_back_new_state_if_final_publish_fails(tmp_path, monkeypatch):
    import pmt.server_admin.init as init_module
    from pmt.server_admin.cli import main
    config, data, logs, backup = [tmp_path / name for name in ("config", "data", "logs", "backup")]
    def fail(*_args, **_kwargs): raise PmtError("config_conflict", "injected test failure")
    monkeypatch.setattr(init_module, "_publish_config_locked", fail)
    argv = ["init", "--config-root", str(config), "--public-url", "https://127.0.0.1:18765", "--listen", "127.0.0.1:18765",
            "--data-root", str(data), "--log-dir", str(logs), "--backup-dir", str(backup), "--apply", "--json"]
    assert main(argv) != 0
    assert not (config / "host-config.json").exists() and not (config / "profile.json").exists()
    assert not (config / "secrets" / "claim-primary.key").exists() and not (config / "secrets" / "claim-primary.dpapi").exists()
    assert not (data / "pmt.sqlite3").exists() and not logs.exists() and not backup.exists()


def test_init_removes_new_empty_secrets_dir_when_key_helper_fails_before_return(tmp_path, monkeypatch):
    import pmt.server_admin.init as init_module
    from pmt.server_admin.cli import main
    config, data, logs, backup = [tmp_path / name for name in ("config", "data", "logs", "backup")]
    real_create = init_module.create_key_reference

    def fail_after_directory_creation(root, *_args, **_kwargs):
        (Path(root) / "secrets").mkdir()
        raise PmtError("host_key_insecure", "injected key setup failure")

    monkeypatch.setattr(init_module, "create_key_reference", fail_after_directory_creation)
    argv = ["init", "--config-root", str(config), "--public-url", "https://127.0.0.1:18765", "--listen", "127.0.0.1:18765",
            "--data-root", str(data), "--log-dir", str(logs), "--backup-dir", str(backup), "--apply", "--json"]
    assert main(argv) != 0
    assert not (config / "secrets").exists()
    assert not (config / "profile.json").exists() and not (data / "pmt.sqlite3").exists()

    monkeypatch.setattr(init_module, "create_key_reference", real_create)
    assert main(argv) == 0
    assert (config / "secrets").is_dir() and (config / "host-config.json").is_file()


def test_secret_rotate_migrate_and_retire_preserve_material_and_key_identity(tmp_path, monkeypatch):
    from pmt.server_admin.cli import main
    from pmt.server_admin.secrets import read_key
    config_root, data_root = tmp_path / "config", tmp_path / "data"
    config_root.mkdir()
    database = Database(root=data_root, config_root=config_root)
    AuthRegistry(database)
    original = os.urandom(48)
    monkeypatch.setenv("PMT_C1_ORIGINAL_KEY", __import__("base64").b64encode(original).decode("ascii"))
    config = _config(config_root, data_root)
    config["claim_key"]["source"] = {"kind": "env", "name": "PMT_C1_ORIGINAL_KEY"}
    publish_config(config_root, config, create=True)
    args = ["--config-root", str(config_root), "secret"]

    assert main(args + ["rotate-claim-key", "--new-key-id", "rotated", "--apply", "--json"]) == 0
    rotated = load_config(config_root / "host-config.json")["claim_key"]
    assert rotated["key_id"] == "rotated"
    assert rotated["retained"][0] == {"key_id": "primary", "source": {"kind": "env", "name": "PMT_C1_ORIGINAL_KEY"}}
    generated = read_key(rotated["source"], account="current")
    assert len(generated) == 48

    assert main(args + ["migrate-claim-key", "--key-id", "primary", "--to", "file", "--apply", "--json"]) == 0
    migrated = load_config(config_root / "host-config.json")["claim_key"]["retained"][0]
    assert migrated["key_id"] == "primary"
    assert read_key(migrated["source"], account="current") == original
    old_source = Path(migrated["source"]["path"])
    old_bytes = old_source.read_bytes()

    assert main(args + ["retire-claim-key", "--key-id", "primary", "--apply", "--json"]) == 0
    final = load_config(config_root / "host-config.json")["claim_key"]
    assert final["key_id"] == "rotated" and not final["retained"]
    assert old_source.read_bytes() == old_bytes


def test_secret_retire_blocks_active_lease_and_config_publish_failure_rolls_back_new_key(tmp_path, monkeypatch):
    import pmt.server_admin.cli as cli_module
    from pmt.server_admin.cli import main
    from pmt.server_admin.secrets import read_key
    config_root, data_root = tmp_path / "config", tmp_path / "data"
    config_root.mkdir()
    database = Database(root=data_root, config_root=config_root)
    AuthRegistry(database)
    config = _config(config_root, data_root)
    source = store_key(__import__("base64").b64encode(os.urandom(48)), config_root / "secrets" / "claim-primary.key", "file")
    config["claim_key"]["source"] = source
    config["claim_key"]["retained"] = [{"key_id": "old", "source": source}]
    publish_config(config_root, config, create=True)
    import uuid
    from pmt.util import utc_now
    scope_id, record_id, device_id, session_id, now = [str(uuid.uuid4()) for _ in range(4)] + [utc_now()]
    with database.write() as connection:
        connection.execute("INSERT INTO scopes(id,kind,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (scope_id, "project", "lease-fixture", "{}", now, now))
        connection.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (record_id, "work", scope_id, "lease fixture", "Active", "{}", 1, now, now))
        connection.execute("INSERT INTO host_devices(id,actor,credential_hash,scopes_json,permissions_json,revision,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (device_id, "fixture", "credential-hash", "[]", "[]", 1, "active", now, now))
        connection.execute("INSERT INTO host_sessions(id,device_id,environment_id,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                           (session_id, device_id, "env-fixture", "active", now, now))
        connection.execute("INSERT INTO host_claim_leases(id,record_id,scope_id,device_id,actor,session_id,key_id,fingerprint,generation,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (str(uuid.uuid4()), record_id, scope_id, device_id, "fixture", session_id, "old", "f" * 64, 1, "active", now))
    args = ["--config-root", str(config_root), "secret", "retire-claim-key", "--key-id", "old", "--apply", "--json"]
    assert main(args) != 0
    assert load_config(config_root / "host-config.json")["claim_key"]["retained"]

    monkeypatch.setattr(cli_module, "_publish_config_locked", lambda *_a, **_k: (_ for _ in ()).throw(PmtError("config_conflict", "injected")))
    rotate_args = ["--config-root", str(config_root), "secret", "rotate-claim-key", "--new-key-id", "failed", "--apply", "--json"]
    assert main(rotate_args) != 0
    assert not (config_root / "secrets" / "claim-failed.key").exists()
    assert load_config(config_root / "host-config.json")["claim_key"]["key_id"] == "primary"


def test_init_claim_key_fills_only_a_configured_missing_file(tmp_path, capsys):
    from pmt.server_admin.cli import main
    from pmt.server_admin.secrets import read_key
    config_root, data_root = tmp_path / "config", tmp_path / "data"
    config_root.mkdir()
    config = _config(config_root, data_root)
    config["claim_key"]["source"] = {"kind": "file", "path": str(config_root / "secrets" / "claim-primary.key")}
    publish_config(config_root, config, create=True)
    args = ["--config-root", str(config_root), "secret", "init-claim-key", "--apply", "--json"]
    assert main(args) == 0
    source = load_config(config_root / "host-config.json")["claim_key"]["source"]
    assert len(read_key(source, account="current")) == 48
    before = Path(source["path"]).read_bytes()
    assert main(args) != 0
    assert Path(source["path"]).read_bytes() == before


def test_adopt_only_reads_host_metadata_and_publishes_declaration(tmp_path):
    from pmt.server_admin.cli import main
    old_config, data, admin_config = tmp_path / "legacy-config", tmp_path / "legacy-data", tmp_path / "admin-config"
    database = Database(root=data, config_root=old_config)
    registry = AuthRegistry(database)
    scope_id = str(__import__("uuid").uuid4())
    with database.write() as connection:
        create_scope(database, connection, {"scope_id": scope_id, "actor": "fixture", "payload": {"kind": "project", "slug": "adopt-fixture"}})
    issued = registry.issue_device("legacy-device", [scope_id], ["read"])
    expected_devices = registry.list_devices()
    profile_before = (old_config / "profile.json").read_bytes()
    db_before = (data / "pmt.sqlite3").read_bytes()
    args = ["init", "--adopt", "--config-root", str(admin_config), "--host-config-root", str(old_config),
            "--data-root", str(data), "--public-url", "https://127.0.0.1:18765", "--listen", "127.0.0.1:18765",
            "--claim-key-env", "PMT_TEST_CLAIM_KEY", "--tls-cert", str(tmp_path / "old.crt"), "--tls-key", str(tmp_path / "old.key"), "--json"]
    assert main(args) == 0
    assert not (admin_config / "host-config.json").exists()
    assert main(args[:-1] + ["--apply", "--json"]) == 0
    adopted = load_config(admin_config / "host-config.json")
    assert adopted["claim_key"]["source"] == {"kind": "env", "name": "PMT_TEST_CLAIM_KEY"}
    assert adopted["paths"]["data_root"] == str(data)
    assert _adopt_metadata(data, old_config)["namespace_id"] == registry.namespace_id
    assert registry.list_devices() == expected_devices
    assert (old_config / "profile.json").read_bytes() == profile_before
    assert (data / "pmt.sqlite3").read_bytes() == db_before


@pytest.mark.skipif(os.name != "nt", reason="Native Windows DPAPI and DACL check")
def test_windows_local_machine_dpapi_roundtrip_and_widened_acl_rejection(tmp_path):
    secret = os.urandom(48)
    encrypted = _dpapi(secret)
    assert _dpapi(encrypted, decrypt=True) == secret
    path = tmp_path / "claim.dpapi"
    path.write_bytes(encrypted)
    _set_windows_acl(path)
    _check_windows_acl(path)
    subprocess.run(["icacls", str(path), "/grant", "*S-1-1-0:R"], check=True, capture_output=True)
    with pytest.raises(PmtError, match="unexpected account"):
        _check_windows_acl(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode check")
def test_linux_file_secret_rejects_widened_permissions(tmp_path):
    path = tmp_path / "claim.key"
    path.write_bytes(b"secret")
    path.chmod(0o644)
    with pytest.raises(PmtError, match="group or other"):
        check_source({"kind": "file", "path": str(path)})


def test_file_secret_rejects_widened_stat_fixture_on_windows_too(tmp_path, monkeypatch):
    from pmt.server_admin import secrets as secret_module
    path = tmp_path / "claim.key"
    path.write_bytes(b"fixture only")
    monkeypatch.setattr(secret_module.stat, "S_IMODE", lambda _mode: 0o640)
    monkeypatch.setattr(secret_module, "_account_uid", lambda _account=None: path.lstat().st_uid)
    with pytest.raises(PmtError, match="group or other"):
        secret_module._check_file_acl(path)


def test_version_reports_missing_host_extras_in_site_free_interpreter():
    source_root = Path(__file__).resolve().parents[2] / "src"
    code = ("import sys; sys.path.insert(0, " + repr(str(source_root)) + "); "
            "from pmt.server_admin.cli import main; raise SystemExit(main(['version','--json']))")
    result = subprocess.run([sys.executable, "-S", "-c", code], capture_output=True, text=True)
    assert result.returncode == 5
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "host_dependency_missing"
