from __future__ import annotations

import copy
import hashlib
import json
import os
import sqlite3
import sys
import time
import uuid
import zipfile
from pathlib import Path

import pytest

from pmt import lifecycle
from pmt.db import Database, SCHEMA_VERSION
from pmt.errors import PmtError
from pmt.handoff import PLUGIN_VERSION
from pmt.host.auth import AuthRegistry, HOST_SCHEMA_VERSION
from pmt.host.resources import HostResourceStore
from pmt.migration import GRAPH_SCHEMA_VERSION, MigrationCoordinator
from pmt.server_admin.backup import create_backup, import_bundle, prune_backups, restore_check
from pmt.server_admin.config import load_config, load_config_snapshot, publish_config
from pmt.server_admin.upgrade import _candidate_install_commands, _temporary_doctor_config, upgrade_host


def _host(tmp_path, name):
    root, data, logs, backup = (tmp_path / name / part for part in ("config", "data", "logs", "backup"))
    port = 19461
    from pmt.server_admin.cli import main
    code = main(["init", "--config-root", str(root), "--public-url", f"https://127.0.0.1:{port}",
                 "--listen", f"127.0.0.1:{port}", "--data-root", str(data), "--log-dir", str(logs),
                 "--backup-dir", str(backup), "--apply", "--json"])
    assert code == 0
    return root, data, backup


def _business_fixture(root, data, *, with_resource=True, active_claim=False):
    db = Database(root=data, config_root=root)
    auth = AuthRegistry(db)
    request_id, actor = str(uuid.uuid4()), "e2-fixture"
    with db.write() as conn:
        environment = lifecycle.create_scope(db, conn, {"request_id": request_id, "actor": actor,
            "payload": {"kind": "environment", "slug": "e2-fixture-environment"}})["id"]
        repository = lifecycle.create_scope(db, conn, {"request_id": str(uuid.uuid4()), "actor": actor,
            "payload": {"kind": "repository", "slug": "e2-fixture-repository", "parent_id": environment}})["id"]
        project = lifecycle.create_scope(db, conn, {"request_id": str(uuid.uuid4()), "actor": actor,
            "payload": {"kind": "project", "slug": "e2-fixture-project", "parent_id": repository}})["id"]
        record = lifecycle.save_change(db, conn, {"request_id": str(uuid.uuid4()), "actor": actor,
            "scope_id": project, "payload": {"kind": "work", "title": "E2 fixture record", "reason": "migration fixture"}})["id"]
    artifact_bytes = b"stable resource bytes for E2 hash verification"
    artifact_path = None
    if with_resource:
        operator = auth.issue_device("e2-resource-fixture", ["*"], ["write"])
        session_id, environment_id = str(uuid.uuid4()), str(uuid.uuid4())
        auth.register_session(operator["credential"], operator["device_id"], auth.namespace_id, session_id, environment_id)
        headers = {"authorization": "Bearer " + operator["credential"], "x-pmt-device": operator["device_id"],
                   "x-pmt-namespace": auth.namespace_id, "x-pmt-session": session_id,
                   "x-pmt-environment": environment_id}
        store = HostResourceStore(db, auth)
        result = store.publish({"request_id": str(uuid.uuid4()), "scope_id": project, "purpose": "evidence",
                                "content": artifact_bytes}, headers)
        with db.connect() as connection:
            relative_path = connection.execute("SELECT relative_path FROM artifacts WHERE id=?",
                                               (result["artifact_ref"]["id"],)).fetchone()[0]
        artifact_path = data / relative_path
        auth.revoke_device(operator["device_id"], operator["revision"])
    if active_claim:
        now, token_hash = "2026-10-08T00:00:00Z", "a" * 64
        with db.write() as conn:
            conn.execute("INSERT INTO claims(record_id,owner_session,token_hash,claimed_at,heartbeat_at) VALUES(?,?,?,?,?)",
                         (record, "active-fixture-session", token_hash, now, now))
    return db, auth, {"environment_id": environment, "repository_id": repository,
                      "project_id": project, "record_id": record,
                      "artifact_bytes": artifact_bytes, "artifact_path": artifact_path}


def _zip_bundle(source, target):
    manifest = MigrationCoordinator().verify_backup(source)
    from pmt.host.transfer import _pack_bundle
    target.write_bytes(_pack_bundle(source, manifest))
    return manifest


def _cli_json(capsys, argv):
    from pmt.server_admin.cli import main
    code = main(argv + ["--json"])
    output = capsys.readouterr().out.splitlines()
    return code, json.loads(output[-1])


def test_host_backup_restore_check_sanitizes_auth_and_preserves_artifact_hashes(tmp_path):
    root, data, backup = _host(tmp_path, "source")
    db, _auth, fixture = _business_fixture(root, data)
    original_artifact_hash = hashlib.sha256(fixture["artifact_path"].read_bytes()).hexdigest()
    result = create_backup(root, apply=True)
    bundle = Path(result["bundle_path"])
    assert result["ok"] and bundle.is_dir() and result["source_kind"] == "host"
    manifest = MigrationCoordinator().verify_backup(bundle)
    assert manifest["excluded_history"]["host_devices"]["count"] >= 1
    assert manifest["excluded_history"]["host_sessions"]["count"] >= 1
    assert "host_namespace_id" not in (bundle / "transfer.sqlite3").read_bytes().decode("latin1")
    assert "claim-primary" not in json.dumps(manifest) and "PRIVATE KEY" not in json.dumps(manifest)
    asset = manifest["resource_objects"][0]
    copied = (bundle / asset["bundle_path"]).read_bytes()
    assert copied == fixture["artifact_bytes"]
    assert hashlib.sha256(copied).hexdigest() == asset["sha256"] == original_artifact_hash
    check = restore_check(root, bundle)
    assert check["ok"] and check["receipt"]["resource_count"] == 1
    assert fixture["artifact_path"].read_bytes() == fixture["artifact_bytes"]
    assert not any(path.name.startswith(".pmt-restore-check-") for path in backup.iterdir())


def test_backup_refuses_active_claim_and_keeps_existing_state(tmp_path):
    root, data, backup = _host(tmp_path, "active")
    db, auth, fixture = _business_fixture(root, data, with_resource=False, active_claim=True)
    with pytest.raises(PmtError) as error: create_backup(root, apply=True)
    assert error.value.code == "migration_source_not_quiescent"
    with sqlite3.connect(f"file:{db.path.as_posix()}?mode=ro", uri=True) as connection:
        assert connection.execute("SELECT owner_session FROM claims WHERE record_id=?", (fixture["record_id"],)).fetchone()[0] == "active-fixture-session"
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    assert all(item["state"] == "revoked" for item in auth.list_devices() if item["actor"] == "pmt-server-backup")
    assert list(backup.iterdir()) == []


@pytest.mark.parametrize("archive_kind,expected_code", [("traversal", "transfer_archive_path_invalid"),
                                                         ("too_many", "transfer_archive_file_count")])
def test_restore_check_reuses_hosttransfer_zip_slip_and_entry_bounds(tmp_path, archive_kind, expected_code):
    root, _data, backup = _host(tmp_path, "zip-boundary")
    archive = tmp_path / f"{archive_kind}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as output:
        if archive_kind == "traversal":
            output.writestr("../outside.txt", b"escape")
        else:
            for index in range(2050): output.writestr(f"entry-{index}.txt", b"x")
    with pytest.raises(PmtError) as error: restore_check(root, archive)
    assert error.value.code == expected_code
    assert not any(path.name.startswith(".pmt-restore-check-") for path in backup.iterdir())


def test_prune_removes_only_verified_owned_direct_child_bundles(tmp_path):
    root, data, backup = _host(tmp_path, "prune")
    db, _auth, _fixture = _business_fixture(root, data, with_resource=False)
    first = Path(create_backup(root, apply=True)["bundle_path"])
    time.sleep(0.02)
    second = Path(create_backup(root, apply=True)["bundle_path"])
    foreign = backup / "foreign-directory"
    foreign.mkdir(); (foreign / "untrusted.txt").write_text("not a backup", encoding="utf-8")
    plan = prune_backups(root, keep=1)
    assert not plan["applied"] and len(plan["would_remove"]) == 1
    result = prune_backups(root, keep=1, apply=True)
    assert result["ok"] and len(result["removed"]) == 1
    assert second.exists() and foreign.exists() and not first.exists()


def test_e2_cli_backup_prune_restorecheck_and_import_plan_are_wired(tmp_path, capsys):
    root, data, backup = _host(tmp_path, "cli-e2")
    _business_fixture(root, data, with_resource=False)
    code, created = _cli_json(capsys, ["backup", "--config-root", str(root), "--apply"])
    assert code == 0 and created["source_kind"] == "host"
    code, pruned = _cli_json(capsys, ["backup", "--config-root", str(root), "prune", "--keep", "1"])
    assert code == 0 and pruned["applied"] is False and pruned["verified_count"] == 1
    bundle = Path(created["bundle_path"])
    code, checked = _cli_json(capsys, ["restore-check", "--config-root", str(root), "--bundle", str(bundle)])
    assert code == 0 and checked["valid"]
    archive = tmp_path / "cli-import.zip"; _zip_bundle(bundle, archive)
    code, plan = _cli_json(capsys, ["import", "--config-root", str(root), "--bundle", str(archive)])
    assert code == 0 and plan["applied"] is False and plan["operation"] == "import"
    assert list(backup.iterdir()) == [bundle]


def test_zip_import_uses_official_empty_target_guard_and_registers_real_project_scopes(tmp_path):
    source_root, source_data, _source_backup = _host(tmp_path, "import-source")
    _source_db, _source_auth, source_fixture = _business_fixture(source_root, source_data, with_resource=True)
    bundle_path = Path(create_backup(source_root, apply=True)["bundle_path"])
    manifest = MigrationCoordinator().verify_backup(bundle_path)
    archive = tmp_path / "fixture.zip"
    _zip_bundle(bundle_path, archive)

    target_root, target_data, _target_backup = _host(tmp_path, "import-target")
    target_db, target_auth, _ = _business_fixture(target_root, target_data, with_resource=False)
    # This target already has business rows; the public import wrapper must preserve it.
    old_namespace = target_auth.namespace_id
    registry_before = load_config(target_root / "host-config.json")["registry"]["projects"]
    with pytest.raises(PmtError) as error: import_bundle(target_root, archive, apply=True)
    assert error.value.code == "migration_target_not_empty", str(error.value)
    assert load_config(target_root / "host-config.json")["registry"]["projects"] == registry_before
    with target_db.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
    assert AuthRegistry(target_db).namespace_id == old_namespace

    clean_root, clean_data, _clean_backup = _host(tmp_path, "import-clean-target")
    clean_db = Database(root=clean_data, config_root=clean_root)
    clean_auth = AuthRegistry(clean_db)
    clean_namespace = clean_auth.namespace_id
    imported = import_bundle(clean_root, archive, apply=True)
    assert imported["ok"] and imported["imported"] and imported["receipt"]["state"] == "imported"
    assert imported["receipt"]["target_namespace_id"] == clean_namespace
    config = load_config(clean_root / "host-config.json")
    project = config["registry"]["projects"][0]
    assert project["project_id"] == source_fixture["project_id"]
    assert project["repositories"] == []  # Imported IDs are registered; remote/graph metadata requires repo add.
    with clean_db.connect() as connection:
        parent = connection.execute("SELECT r.id,r.kind FROM scopes p JOIN scopes r ON r.id=p.parent_id WHERE p.id=?",
                                    (project["project_id"],)).fetchone()
        assert tuple(parent) == (_fixture_repository_id(bundle_path, project["project_id"]), "repository")
    imported_devices = AuthRegistry(clean_db).list_devices()
    assert not any(item["actor"] == "e2-resource-fixture" for item in imported_devices)
    import_devices = [item for item in imported_devices if item["actor"] == "pmt-server-import"]
    assert len(import_devices) == 1 and import_devices[0]["state"] == "revoked"
    from pmt.server_admin.devices import device_issue
    plan = device_issue(clean_root, "e2-import-probe", [project["name"]])
    assert plan["scopes"] == [project["project_id"]]


def _fixture_repository_id(bundle_path, project_id):
    with sqlite3.connect(f"file:{(Path(bundle_path) / 'transfer.sqlite3').as_posix()}?mode=ro", uri=True) as connection:
        return connection.execute("SELECT parent_id FROM scopes WHERE id=?", (project_id,)).fetchone()[0]


def test_import_cas_failure_reports_committed_partial_receipt_and_replays_safely(tmp_path, monkeypatch):
    import pmt.server_admin.backup as backup_module
    source_root, source_data, _ = _host(tmp_path, "cas-source")
    _business_fixture(source_root, source_data, with_resource=False)
    bundle = Path(create_backup(source_root, apply=True)["bundle_path"])
    archive = tmp_path / "cas-import.zip"
    _zip_bundle(bundle, archive)
    target_root, _target_data, _ = _host(tmp_path, "cas-target")
    monkeypatch.setattr(backup_module, "_publish_config_locked",
                        lambda *_a, **_k: (_ for _ in ()).throw(PmtError("config_conflict", "injected CAS conflict")))
    partial = import_bundle(target_root, archive, apply=True)
    assert not partial["ok"] and partial["imported"] and partial["receipt"]["state"] == "imported"
    assert partial["registry_updated"] is False and partial["guidance"]
    monkeypatch.undo()
    replay = import_bundle(target_root, archive, apply=True)
    assert replay["ok"] and replay["receipt"]["state"] == "replayed" and replay["registry_updated"]
    assert len(load_config(target_root / "host-config.json")["registry"]["projects"]) == 1


class _FakeUpgradeAdapter:
    def __init__(self, *, fail_compat=False):
        self.events = []
        self.fail_compat = fail_compat
        self.release = "0.5.1"
        self.live_data_root = None

    def is_admin(self): return True

    def service_state(self, config, config_root):
        self.events.append("service_state")
        return {"exists": True, "matches": True, "state": "running"}

    def version(self, python):
        self.events.append("version")
        return {"version": self.release, "core_version": "0.4.1", "db_schema": SCHEMA_VERSION,
                "graph_schema": GRAPH_SCHEMA_VERSION, "protocol": 1, "host_schema": HOST_SCHEMA_VERSION,
                "python_version": "3.14.5", "dependencies": {"fastapi": "1", "pydantic": "2", "uvicorn": "1"}}

    def doctor(self, python, config_root, environment):
        self.events.append("doctor")
        config = load_config(Path(config_root) / "host-config.json")
        assert config["service"]["kind"] == "none"  # isolated scratch Doctor never resolves live service credentials
        assert Path(config["service"]["app_root"]).resolve() == Path(python).parents[2].resolve()
        scratch_db = Path(config["paths"]["data_root"]) / "pmt.sqlite3"
        assert scratch_db != self.live_data_root
        connection = sqlite3.connect(f"file:{scratch_db.as_posix()}?mode=ro", uri=True)
        try:
            assert {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")} == {"meta"}
        finally: connection.close()
        return {"ok": True, "checks": []}

    def install_candidate(self, app_root, source_ref):
        self.events.append("install")
        assert len(source_ref) in {40, 64}
        python = Path(app_root) / "venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.write_bytes(b"synthetic candidate runtime marker")
        return {"ok": True}

    def create_backup(self, config_root):
        self.events.append("backup")
        return {"ok": True, "bundle_path": "fixture-backup"}

    def stop_service(self, config, config_root):
        self.events.append("stop")
        return {"ok": True}

    def install_and_start_service(self, config, config_root):
        self.events.append("install_start")
        return {"ok": True}

    def start_service(self, config, config_root):
        self.events.append("start")
        return {"ok": True}

    def health_compatibility(self, config_root, expected_namespace):
        self.events.append("compat")
        if self.fail_compat: raise PmtError("upgrade_health_failed", "injected post-switch compatibility failure")
        return {"health": "ok", "compatibility": "ok", "namespace_id": expected_namespace}


def _managed_host(tmp_path, name):
    root, data, backup = _host(tmp_path, name)
    releases = tmp_path / name / "releases"
    releases.mkdir()
    old_root = releases / PLUGIN_VERSION
    config, digest = load_config_snapshot(root / "host-config.json")
    config["service"]["kind"] = "windows-task" if os.name == "nt" else "systemd"
    config["service"]["account"] = "current"
    config["service"]["app_root"] = str(old_root)
    config["revision"] += 1
    publish_config(root, config, digest)
    return root, data, backup, old_root, releases


def _prepared_release(path):
    python = path / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_bytes(b"synthetic candidate runtime marker")
    return python


def test_native_candidate_install_commands_are_pinned_and_use_bootstrap_python(tmp_path):
    from pmt.server_admin import upgrade as upgrade_module
    app_root = tmp_path / "candidate"
    commands = _candidate_install_commands(app_root, "a" * 40)
    assert commands[0] == [sys.executable, "-m", "venv", str(app_root / "venv")]
    assert commands[1][:4] == [str(app_root / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
                               "-m", "pip", "install"]
    assert commands[1][4] == "proj-mgmt-tool[host] @ git+https://github.com/wnwjdals7498/j-ai-plugin@" + "a" * 40 + "#subdirectory=proj-mgmt-tool"
    injected = _candidate_install_commands(app_root, "b" * 64, bootstrap_python=tmp_path / "bootstrap-python")
    assert injected[0][0] == str(tmp_path / "bootstrap-python")
    # The builder is pure: this test validates argv only, never invokes pip/venv.
    assert upgrade_module._candidate_install_commands is _candidate_install_commands


def test_candidate_doctor_is_service_agnostic_without_changing_systemd_source(tmp_path):
    from pmt.server_admin.upgrade import _temporary_doctor_config
    root, _data, backup = _host(tmp_path, "doctor-systemd")
    config_path = root / "host-config.json"
    original_bytes = config_path.read_bytes()
    config = load_config(config_path)
    config["service"]["kind"] = "systemd"
    key_id = config["claim_key"]["key_id"]
    config["claim_key"]["source"] = {"kind": "file", "path": f"${{CREDENTIALS_DIRECTORY}}/claim-{key_id}"}
    app_root = tmp_path / "candidate-release"
    scratch, doctor_root, environment = _temporary_doctor_config(config, app_root, root, backup)
    try:
        scratch_config = load_config(doctor_root / "host-config.json")
        assert config["service"]["kind"] == "systemd"
        assert scratch_config["service"]["kind"] == "none"
        assert scratch_config["claim_key"]["source"]["kind"] == "env"
        assert environment[scratch_config["claim_key"]["source"]["name"]]
        assert config_path.read_bytes() == original_bytes
    finally:
        from pmt.server_admin.backup import _clean_scratch
        _clean_scratch(scratch, backup, ".pmt-upgrade-doctor-")


def test_upgrade_requires_immutable_source_and_orders_safe_steps(tmp_path):
    root, data, _backup, _old_root, releases = _managed_host(tmp_path, "upgrade-source")
    adapter = _FakeUpgradeAdapter(); adapter.live_data_root = data / "pmt.sqlite3"
    target = releases / "0.5.1"
    blocked = upgrade_host(root, "0.5.1", app_root=target, apply=False, adapter=adapter)
    assert not blocked["ok"] and blocked["error_code"] == "upgrade_source_required" and not target.exists()
    with pytest.raises(PmtError): upgrade_host(root, "0.5.1", app_root=target, source_ref="v0.5.1", adapter=adapter)
    success = upgrade_host(root, "0.5.1", app_root=target, source_ref="a" * 40, apply=True, adapter=adapter)
    assert success["ok"] and success["candidate"]["version"] == "0.5.1"
    assert adapter.events.index("backup") < adapter.events.index("stop") < adapter.events.index("install_start") < adapter.events.index("compat")
    assert Path(load_config(root / "host-config.json")["service"]["app_root"]).resolve() == target.resolve()


def test_upgrade_compatibility_failure_rolls_service_app_root_back(tmp_path):
    root, data, _backup, old_root, releases = _managed_host(tmp_path, "upgrade-rollback")
    target = releases / "0.5.1"
    _prepared_release(target)
    adapter = _FakeUpgradeAdapter(fail_compat=True); adapter.live_data_root = data / "pmt.sqlite3"
    result = upgrade_host(root, "0.5.1", app_root=target, apply=True, adapter=adapter)
    if result.get("error_code") != "upgrade_health_failed":
        raise AssertionError(f"unexpected upgrade failure: {result.get('error_code')} {result.get('failure_type')} stages={adapter.events}")
    assert not result["ok"] and result["rolled_back"] and result["rollback"]["service_restored"], result
    assert Path(load_config(root / "host-config.json")["service"]["app_root"]).resolve() == old_root.resolve()
    assert adapter.events.count("install_start") == 2 and adapter.events[-2:] == ["stop", "install_start"]
