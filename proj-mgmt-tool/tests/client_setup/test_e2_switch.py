from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

from pmt import easy_cli, handoff, storage_config
from pmt.client_setup import client, connect, local_commands
from pmt.client_setup.credentials import ENV_NAME, _paths, has_credential_store, load_credential, stage_credential
from pmt.client_setup.switch import switch_storage
from pmt.db import Database
from pmt.errors import PmtError
from pmt.migration import MigrationCoordinator
from pmt.storage_config import _read_profile, configure_storage, profile_environment_id, storage_path


TOKEN = "e2-switch-test-credential"
_DEVICE_SCOPES = {}


@pytest.fixture(autouse=True)
def isolate_process_host_credential(monkeypatch):
    # Prior C08 runtime tests load a saved credential into os.environ. Track the
    # original absence so this suite neither consumes nor leaks that process state.
    monkeypatch.setenv(ENV_NAME, "")
    monkeypatch.delenv(ENV_NAME, raising=False)


class FakeHost:
    def __init__(self, endpoint, credential_env, device_id, environment_id, namespace_id, ca_file):
        self.device_id = device_id
        self.environment_id = environment_id
        self.namespace_id = namespace_id

    def check_compatibility(self):
        return {"compatible": True, "actor": "e2-actor", "device_id": self.device_id,
                "namespace_id": self.namespace_id, "core_version": "0.4.1", "db_schema": 5,
                "graph_schema": 1, "protocol_versions": [1],
                "scopes": list(_DEVICE_SCOPES.get(self.device_id, [])), "permissions": ["read", "write"]}

    def register_session(self, session_id):
        return {"session_id": session_id, "environment_id": self.environment_id,
                "device_id": self.device_id}


def _configure_fake(config_root, request):
    return configure_storage(config_root, request, store_factory=FakeHost)


def _write_handoff(path, *, project_id=None, repository_id=None, extra_scopes=()):
    project_id = project_id or str(uuid.uuid4())
    repository_id = repository_id or str(uuid.uuid4())
    document = handoff.build_handoff(
        host_url="https://e2-host.example",
        namespace_id=str(uuid.uuid4()),
        device={"device_id": str(uuid.uuid4()), "actor": "e2-actor", "permissions": ["read", "write"],
                "scopes": [project_id, *extra_scopes], "credential": {"delivery": "separate", "env": ENV_NAME}},
        projects=[{"name": "e2-project", "project_id": project_id,
                   "repositories": [{"name": "e2-repository", "repository_id": repository_id}]}],
    )
    _DEVICE_SCOPES[document["device"]["device_id"]] = list(document["device"]["scopes"])
    Path(path).write_text(json.dumps(document), encoding="utf-8")
    return document, project_id, repository_id


def _probe_result(config_root, scopes):
    profile, _digest = _read_profile(config_root)
    return {"mode": "hosted", "configured": True, "actor": profile["actor"],
            "device_id": profile["device_id"], "namespace_id": profile["namespace_id"],
            "host_preflight": {"scopes": list(scopes)}}


def _local_install(config_root, data_root, *, workspace_mappings=None):
    configure_storage(str(config_root), {"mode": "local", "expected_config_sha256": None,
        "workspace_mappings": workspace_mappings or []})
    db = Database(str(data_root), str(config_root))
    return db


def _seed_local_claim(db):
    scope_id, record_id = str(uuid.uuid4()), str(uuid.uuid4())
    with db.write() as connection:
        now = "2026-10-08T00:00:00Z"
        connection.execute("INSERT INTO scopes VALUES(?,?,?,?,?,?,?)",
            (scope_id, "project", None, "fixture", "{}", now, now))
        connection.execute("INSERT INTO records VALUES(?,?,?,?,?,?,?,?,?,?)",
            (record_id, "item", scope_id, None, "busy fixture", "In Progress", "{}", 1, now, now))
        connection.execute("INSERT INTO claims VALUES(?,?,?,?,?,?)",
            (record_id, "session", hashlib.sha256(b"claim").hexdigest(), now, now, None))


def _storage_bytes(config_root):
    return storage_path(config_root).read_bytes()


def test_default_connect_still_refuses_local_profile(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    with pytest.raises(PmtError) as error:
        connect.connect(config, handoff_path, credential=TOKEN, configure=_configure_fake)
    assert error.value.code == "local_profile_exists"


def test_local_active_sqlite_claim_blocks_switch_without_changing_files(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    db = _local_install(config, data)
    _seed_local_claim(db)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    profile_before = _storage_bytes(config)
    db_before = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()

    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                       credential=TOKEN, configure=_configure_fake)

    assert error.value.code == "switch_busy"
    assert _storage_bytes(config) == profile_before
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == db_before
    assert not has_credential_store(config)


def test_claim_sidecar_blocks_switch_and_malformed_claims_fail_closed(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    claims_path = data / "easy-claims.json"
    claims_path.write_text(json.dumps({str(uuid.uuid4()): {"claim_token": "fixture", "session_id": "session",
        "revision": 1}}), encoding="utf-8")
    profile_before = _storage_bytes(config)
    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                       credential=TOKEN, configure=_configure_fake)
    assert error.value.code == "switch_busy"
    assert _storage_bytes(config) == profile_before
    claims_path.write_text("{broken", encoding="utf-8")
    with pytest.raises(PmtError):
        switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                       credential=TOKEN, configure=_configure_fake)
    assert _storage_bytes(config) == profile_before


def test_hook_pending_and_all_session_outbox_block_switch(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    pending_file = data / "hook-pending" / (str(uuid.uuid4()) + ".json")
    pending_file.parent.mkdir()
    pending_file.write_text("{}", encoding="utf-8")
    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                       credential=TOKEN, configure=_configure_fake)
    assert error.value.code == "switch_busy"
    pending_file.unlink()

    outbox = data / "hosted-pending" / "ns" / "device" / "environment" / "session-2" / "outbox.sqlite3"
    outbox.parent.mkdir(parents=True)
    with sqlite3.connect(outbox) as connection:
        connection.executescript("""
          CREATE TABLE outbox_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE pending_results(request_id TEXT PRIMARY KEY,state TEXT NOT NULL);
          CREATE TABLE pending_resources(upload_request_id TEXT PRIMARY KEY,state TEXT NOT NULL);
          INSERT INTO outbox_meta VALUES('schema_version','1');
          INSERT INTO pending_results VALUES('pending-request','pending');
        """)
    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                       credential=TOKEN, configure=_configure_fake)
    assert error.value.code == "switch_busy"


def test_local_to_hosted_filters_unmatched_mapping_and_reuses_database(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    db = _local_install(config, data)
    profile_before = _read_profile(config)[0]
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    db_before = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()
    requests = []

    def configure(config_root, request):
        requests.append(dict(request))
        return _configure_fake(config_root, request)

    result = switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                            credential=TOKEN, configure=configure)

    assert result["mode"] == "hosted"
    assert requests[0]["workspace_mappings"] == []
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == db_before
    assert _read_profile(config)[0]["mode"] == "hosted"
    metadata = json.loads((config / "client.json").read_text(encoding="utf-8"))
    assert metadata["local_workspace_mappings"] == profile_before["workspace_mappings"]
    assert load_credential(config, environ={}) == TOKEN


def test_matching_local_mapping_round_trips_through_hosted_and_back(tmp_path, monkeypatch):
    workspace = tmp_path / "clean checkout"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "E2 Test"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "e2@example.invalid"], check=True)
    (workspace / "tracked.txt").write_text("clean\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "baseline"], check=True)
    project_id, repository_id = str(uuid.uuid4()), str(uuid.uuid4())
    mapping = {"repository_id": repository_id, "project_id": project_id, "branch": "main",
               "branch_key_sha256": hashlib.sha256(b"main").hexdigest(),
               "local_root": str(workspace.resolve()), "relative_graph_path": "docs/pmt-docs/graph.json"}
    config, data = tmp_path / "config", tmp_path / "data"
    db = _local_install(config, data, workspace_mappings=[mapping])
    mapping = _read_profile(config)[0]["workspace_mappings"][0]
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path, project_id=project_id, repository_id=repository_id)
    before_db = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()
    result = switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                            credential=TOKEN, configure=_configure_fake)
    assert result["mode"] == "hosted"
    hosted_profile, _ = _read_profile(config)
    assert hosted_profile["workspace_mappings"] == [mapping]
    metadata = json.loads((config / "client.json").read_text(encoding="utf-8"))
    saved_mapping = {key: value for key, value in mapping.items()
                     if key in {"repository_id", "project_id", "branch", "branch_key_sha256",
                                "local_root", "relative_graph_path"}}
    assert metadata["local_workspace_mappings"] == [saved_mapping]

    monkeypatch.setattr(storage_config, "probe_storage", lambda root: _probe_result(root, [project_id]))
    hosted_reader = lambda _scope, _payload: {"records": [], "next_cursor": None}
    switch_storage(config, data, to="local", hosted_reader=hosted_reader)
    local_profile, _ = _read_profile(config)
    assert local_profile["workspace_mappings"] == [mapping]
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == before_db
    assert db.path.is_file()


def test_export_is_valid_zip_from_clone_and_original_database_hash_is_unchanged(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    destination = tmp_path / "bundle.zip"
    db_path = data / "pmt.sqlite3"
    before = hashlib.sha256(db_path.read_bytes()).hexdigest()

    result = switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                            credential=TOKEN, export_path=destination, configure=_configure_fake)

    assert result["export"]["file_count"] >= 2
    assert destination.is_file()
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before
    stage = tmp_path / "verify"
    stage.mkdir()
    with zipfile.ZipFile(destination, "r") as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            target = stage / Path(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(name))
    manifest = MigrationCoordinator().verify_backup(stage)
    assert manifest["manifest_sha256"] == result["export"]["manifest_sha256"]


def test_export_refuses_existing_or_source_root_destinations_without_overwrite(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    profile_before = _storage_bytes(config)
    db_before = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()
    existing = tmp_path / "already.zip"
    existing.write_bytes(b"keep me")
    for destination, expected in ((existing, "migration_destination_exists"),
                                  (data / "inside.zip", "migration_destination_invalid")):
        with pytest.raises(PmtError) as error:
            switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                           credential=TOKEN, export_path=destination, configure=_configure_fake)
        assert error.value.code == expected
    assert existing.read_bytes() == b"keep me"
    assert _storage_bytes(config) == profile_before
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == db_before


def test_failed_probe_keeps_local_profile_database_and_credential_store(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    before_profile = _storage_bytes(config)
    before_db = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()

    def reject_probe(_config_root, _request):
        raise PmtError("remote_compatibility_mismatch", "fixture")

    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                       credential=TOKEN, configure=reject_probe)
    assert error.value.code == "remote_compatibility_mismatch"
    assert _storage_bytes(config) == before_profile
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == before_db
    assert not has_credential_store(config)


def test_cas_rejection_restores_existing_profile_and_credential(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    prior_profile = _storage_bytes(config)
    core_profile = (config / "profile.json").read_bytes()
    db_before = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()
    stage_credential(config, "prior-test-credential")
    credential_path = _paths(config)[1]
    credential_before = credential_path.read_bytes()

    def reject_cas(_config_root, _request):
        raise PmtError("storage_config_conflict", "simulated CAS rejection", 3)

    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                       credential=TOKEN, configure=reject_cas)

    assert error.value.code == "storage_config_conflict"
    assert _storage_bytes(config) == prior_profile
    assert (config / "profile.json").read_bytes() == core_profile
    assert credential_path.read_bytes() == credential_before
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == db_before


def test_independent_sqlite_writer_is_blocked_during_host_probe(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path)
    attempts = []

    def configure(config_root, request):
        code = ("import sqlite3,sys; c=sqlite3.connect(sys.argv[1],timeout=.1,isolation_level=None); "
                "exec('try:\\n c.execute(\\\"BEGIN IMMEDIATE\\\")\\n print(\\\"acquired\\\")\\n'"
                "+'except sqlite3.OperationalError:\\n print(\\\"busy\\\")'); c.close()")
        completed = subprocess.run([sys.executable, "-c", code, str(data / "pmt.sqlite3")],
                                   capture_output=True, text=True, timeout=5, check=False)
        attempts.append((completed.returncode, completed.stdout.strip()))
        return _configure_fake(config_root, request)

    switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                   credential=TOKEN, configure=configure)
    assert attempts == [(0, "busy")]


def test_hosted_to_local_requires_online_claim_verification_and_reuses_local_data(tmp_path, monkeypatch):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    document, project_id, repository_id = _write_handoff(handoff_path)
    local_commands._save_projects(config, [{"name": "e2-project", "project_id": project_id,
                                            "repository_id": repository_id, "repository_name": "e2-repository"}])
    stage_credential(config, TOKEN)
    local_profile, local_hash = _read_profile(config)
    configure_storage(str(config), {"mode": "hosted", "expected_config_sha256": local_hash,
        "endpoint": document["host"]["url"], "credential_env": ENV_NAME,
        "device_id": document["device"]["device_id"], "namespace_id": document["namespace_id"],
        "expected_actor": document["device"]["actor"], "workspace_mappings": []},
        store_factory=FakeHost)
    client.write_client_metadata(config, source="connect", python_path=sys.executable, mode="hosted")
    before_db = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()
    monkeypatch.setattr(storage_config, "probe_storage", lambda root: _probe_result(root, [project_id]))
    hosted_reader = lambda _scope, _payload: {"records": [], "next_cursor": None}

    result = switch_storage(config, data, to="local", hosted_reader=hosted_reader)

    assert result["mode"] == "local"
    assert _read_profile(config)[0]["mode"] == "local"
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == before_db
    assert load_credential(config, environ={}) == TOKEN


def test_hosted_active_claim_or_offline_state_blocks_local_switch_without_mutation(tmp_path, monkeypatch):
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    document, project_id, repository_id = _write_handoff(handoff_path)
    local_commands._save_projects(config, [{"name": "e2-project", "project_id": project_id,
                                            "repository_id": repository_id, "repository_name": "e2-repository"}])
    stage_credential(config, TOKEN)
    local_profile, local_hash = _read_profile(config)
    configure_storage(str(config), {"mode": "hosted", "expected_config_sha256": local_hash,
        "endpoint": document["host"]["url"], "credential_env": ENV_NAME,
        "device_id": document["device"]["device_id"], "namespace_id": document["namespace_id"],
        "expected_actor": document["device"]["actor"], "workspace_mappings": []},
        store_factory=FakeHost)
    client.write_client_metadata(config, source="connect", python_path=sys.executable, mode="hosted")
    before_storage = _storage_bytes(config)
    before_db = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()
    monkeypatch.setattr(storage_config, "probe_storage", lambda root: _probe_result(root, [project_id]))
    active_reader = lambda _scope, _payload: {"records": [{"state": "In Progress"}], "next_cursor": None}
    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="local", hosted_reader=active_reader)
    assert error.value.code == "switch_busy"
    assert _storage_bytes(config) == before_storage
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == before_db

    monkeypatch.setattr(storage_config, "probe_storage", lambda _root: (_ for _ in ()).throw(
        PmtError("remote_unavailable", "offline")))
    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="local", hosted_reader=lambda *_args: {"records": [], "next_cursor": None})
    assert error.value.code == "switch_state_unverifiable"
    assert _storage_bytes(config) == before_storage


def _switch_to_hosted_with_extra_scope(tmp_path, extra_scope):
    config, data = tmp_path / "config", tmp_path / "data"
    db = _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    document, project_id, _repository_id = _write_handoff(handoff_path, extra_scopes=[extra_scope])
    result = switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                            credential=TOKEN, configure=_configure_fake)
    assert result["mode"] == "hosted"
    return config, data, db, document, project_id


def test_hosted_claim_scan_includes_device_scope_without_project_entry(tmp_path, monkeypatch):
    hidden_scope = str(uuid.uuid4())
    config, data, db, document, project_id = _switch_to_hosted_with_extra_scope(tmp_path, hidden_scope)
    profile_before = _storage_bytes(config)
    db_before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    monkeypatch.setattr(storage_config, "probe_storage",
                        lambda root: _probe_result(root, [project_id, hidden_scope]))
    observed = []

    def read_scope(scope, _payload):
        observed.append(scope)
        records = [{"state": "In Progress"}] if scope == hidden_scope else []
        return {"records": records, "next_cursor": None}

    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="local", hosted_reader=read_scope)

    assert error.value.code == "switch_busy"
    assert hidden_scope in observed
    assert _storage_bytes(config) == profile_before
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == db_before


@pytest.mark.parametrize("fresh_scopes", [[str(uuid.uuid4())], ["*"]])
def test_stale_removed_or_wildcard_host_grants_refuse_local_switch(tmp_path, monkeypatch, fresh_scopes):
    hidden_scope = str(uuid.uuid4())
    config, data, db, document, project_id = _switch_to_hosted_with_extra_scope(tmp_path, hidden_scope)
    profile_before = _storage_bytes(config)
    db_before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    monkeypatch.setattr(storage_config, "probe_storage",
                        lambda root: _probe_result(root, fresh_scopes))

    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="local", hosted_reader=lambda *_args: {"records": [], "next_cursor": None})

    assert error.value.code == "switch_state_unverifiable"
    assert _storage_bytes(config) == profile_before
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == db_before


def test_client_scope_metadata_cas_failure_restores_prior_metadata_and_hosted_profile(tmp_path, monkeypatch):
    project_id, repository_id = str(uuid.uuid4()), str(uuid.uuid4())
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path, project_id=project_id, repository_id=repository_id)
    switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                   credential=TOKEN, configure=_configure_fake)
    local_commands._save_projects(config, [{"name": "e2-project", "project_id": project_id,
        "repository_id": repository_id, "repository_name": "e2-repository"}])
    profile_before = _storage_bytes(config)
    metadata_before = (config / "client.json").read_bytes()
    db_before = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()
    monkeypatch.setattr(storage_config, "probe_storage", lambda root: _probe_result(root, [project_id]))
    monkeypatch.setattr(connect, "_merge_client_metadata_extra", lambda *_args, **_kwargs:
        (_ for _ in ()).throw(PmtError("client_metadata_conflict", "fixture")))

    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="local", hosted_reader=lambda *_args: {"records": [], "next_cursor": None})

    assert error.value.code == "client_metadata_conflict"
    assert _storage_bytes(config) == profile_before
    assert (config / "client.json").read_bytes() == metadata_before
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == db_before


def test_metadata_cas_conflict_preserves_later_writer_and_hosted_profile(tmp_path, monkeypatch):
    project_id, repository_id = str(uuid.uuid4()), str(uuid.uuid4())
    config, data = tmp_path / "config", tmp_path / "data"
    _local_install(config, data)
    handoff_path = tmp_path / "handoff.json"
    _write_handoff(handoff_path, project_id=project_id, repository_id=repository_id)
    switch_storage(config, data, to="hosted", handoff_path=handoff_path,
                   credential=TOKEN, configure=_configure_fake)
    local_commands._save_projects(config, [{"name": "e2-project", "project_id": project_id,
        "repository_id": repository_id, "repository_name": "e2-repository"}])
    profile_before = _storage_bytes(config)
    metadata_path = config / "client.json"
    foreign_metadata = b'{"writer":"later"}\n'
    db_before = hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest()
    monkeypatch.setattr(storage_config, "probe_storage", lambda root: _probe_result(root, [project_id]))

    def concurrent_metadata_writer(_root, _expected, _extra):
        metadata_path.write_bytes(foreign_metadata)
        raise PmtError("client_metadata_conflict", "fixture")

    monkeypatch.setattr(connect, "_merge_client_metadata_extra", concurrent_metadata_writer)
    with pytest.raises(PmtError) as error:
        switch_storage(config, data, to="local", hosted_reader=lambda *_args: {"records": [], "next_cursor": None})

    assert error.value.code == "storage_config_conflict"
    assert _storage_bytes(config) == profile_before
    assert metadata_path.read_bytes() == foreign_metadata
    assert hashlib.sha256((data / "pmt.sqlite3").read_bytes()).hexdigest() == db_before


def test_cli_registers_storage_switch_surface():
    args = easy_cli.build_parser().parse_args(["storage", "switch", "--to", "hosted", "--handoff", "handoff.json"])
    assert args.command == "storage" and args.storage_command == "switch" and args.to == "hosted"
