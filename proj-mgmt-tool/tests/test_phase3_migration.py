from __future__ import annotations

import json
import hashlib
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import subprocess
import uuid

import pytest

from pmt.db import Database, SCHEMA_VERSION
from pmt.errors import PmtError
from pmt.host.application import HostApplication
from pmt.host.resources import HostResourceStore
from pmt.migration import MigrationCoordinator
from pmt.continuity.storage import ContinuityStore
from pmt.phase2_common import persist_json_resource
from pmt.util import canonical_json, new_id, utc_now
from pmt.workspace import canonical_workspace


SECRET = "migration-test-secret-should-not-transfer"


def _git(workspace: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(workspace), *args], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, check=False, timeout=10)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout.decode("utf-8").strip()


def _host(db, *, session="fixture-import-session"):
    app = HostApplication(db, {"fixture": b"migration-host-fixture-key-32-bytes"}, "fixture")
    issued = app.auth.issue_device("main", ["*"], ["read", "write", "runtime", "review", "admin"])
    headers = {"authorization": "Bearer " + issued["credential"],
        "x-pmt-device": issued["device_id"], "x-pmt-environment": new_id(),
        "x-pmt-namespace": app.auth.namespace_id, "x-pmt-session": session}
    app.register_session(headers, {"session_id": session, "environment_id": headers["x-pmt-environment"]})
    HostResourceStore(db, app.auth)
    return app, headers, issued["device_id"]


def _source_case(tmp_path):
    db = Database(tmp_path / "source-data", tmp_path / "source-config")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.name", "Migration fixture"], check=True)
    subprocess.run(["git", "-C", str(workspace), "config", "user.email", "migration@example.invalid"], check=True)
    (workspace / "graph.json").write_text('{"schema_version":1}\n', encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "graph.json"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "fixture baseline"], check=True)
    branch, commit = _git(workspace, "symbolic-ref", "--quiet", "--short", "HEAD"), _git(workspace, "rev-parse", "HEAD")
    repo_id, project_id, class_id = new_id(), new_id(), new_id()
    work_id, item_id, step_id = new_id(), new_id(), new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                     (repo_id, "repository", "fixture-repo", canonical_json({"workspace": str(workspace)}), now, now))
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (project_id, "project", repo_id, "fixture-project",
                      canonical_json({"repository_id": repo_id, "workspace": str(workspace)}), now, now))
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (class_id, "classification", project_id, "fixture-class", "{}", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?)", (work_id, "work", project_id, "Migration fixture", "InProgress", "{}", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (item_id, "item", project_id, work_id, "Fixture item", "InProgress", "{}", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?)", (step_id, "step", class_id, item_id, "Fixture Step", "Done",
                        canonical_json({"workspace": str(workspace), "criteria": ["C1"], "claim_token": SECRET}),
                        2, now, now))
    directive = {"purpose": "bounded fixture", "goal": "preserve IDs", "non_goal": [],
        "change_scope": {"add": [], "modify": [], "delete": [], "forbidden": []},
        "inputs": [], "outputs": [], "tests": [], "logging": [], "context_refs": []}
    request = {"request_id": new_id(), "actor": "main", "session_id": "source-session",
               "scope_id": project_id, "payload": {}}
    directive_ref = persist_json_resource(db, request, directive, project_id, "step_directive", step_id)
    evidence_ref = persist_json_resource(db, {**request, "request_id": new_id()},
        {"evidence": "unchanged-byte-fixture"}, project_id, "evidence", step_id)
    evidence_path = db.root / evidence_ref["relative_path"]
    scopes = [{"kind": "path", "workspace": str(workspace), "resource": "graph.json"}]
    criteria = [{"id": "C1", "meaning": "preserved"}]
    job_id, run_id = new_id(), new_id()
    route = {"agent": "codex", "provider": "fixture", "model": "local-fixture", "mode": "cli",
        "command": ["codex", "exec", "--model", "local"], "env": {"TOKEN": SECRET}, "pid": 1234,
        "selection_reason": "fixture evidence", "auth_state": "authenticated"}
    intent = {"run_id": run_id, "job_id": job_id, "step_id": step_id,
        "scope_id": project_id, "workspace": str(workspace), "scopes": scopes,
        "criteria": criteria, "route": route, "task": {"task_id": item_id, "step_id": step_id},
        "claim_token": SECRET}
    with db.write() as conn:
        conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
                     "VALUES(?,?,1,'req1','plan1',NULL,'lower','prototype',?,?,?,?,?,?)",
                     (step_id, directive_ref["artifact_id"], str(workspace), canonical_json(scopes),
                      canonical_json(criteria), "[]", now, now))
        conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) "
                     "VALUES(?,?,'succeeded','{}',?,?)", (job_id, step_id, now, now))
        conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,handle_json,result_json,stop_confirmed,started_at,completed_at,created_at,updated_at) "
                     "VALUES(?,?,?,1,'succeeded',3,'source-session',1,?,?,?,?,?, ?,1,?,?,?,?)",
                     (run_id, job_id, step_id, str(workspace), canonical_json(scopes), canonical_json(route),
                      canonical_json(intent), canonical_json({"id": "native-handle-fixture", "pid": 1234}),
                      canonical_json({"summary": "Fixture result", "receipt_ref": evidence_ref["artifact_id"],
                          "evidence_refs": [evidence_ref["artifact_id"]], "claim_token": SECRET,
                          "actual_route": route}), now, now, now, now))
        conn.execute("INSERT INTO events(id,event_id,record_id,scope_id,actor,event_type,reason,payload_json,recorded_at) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (new_id(), new_id(), step_id, project_id, "main",
                     "migration.fixture", "fixture path " + str(workspace),
                     canonical_json({"workspace": str(workspace), "claim_token": SECRET}), now))
        conn.execute("INSERT INTO project_baselines(scope_id,workspace,selected_ref,reviewed_commit,fingerprint,body_json,revision,updated_at) "
                     "VALUES(?,?,?,?,?,?,1,?)", (project_id, str(workspace), branch, commit,
                       hashlib.sha256(b"baseline").hexdigest(), canonical_json({"workspace": str(workspace)}), now))
        conn.execute("INSERT INTO verifications(id,definition_id,definition_version,target_id,environment_id,input_fingerprint,outcome,exit_code,evidence_json,includes_json,command_json,completed_at,state) "
                     "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (new_id(), "fixture-suite", "1", step_id,
                       db.environment_id, hashlib.sha256(b"verification").hexdigest(), "pass", 0,
                       canonical_json([evidence_ref["artifact_id"]]), canonical_json([{"id": "C1", "sha256": hashlib.sha256(b"C1").hexdigest()}]),
                       canonical_json({"command": ["pytest", str(workspace / "tests")],
                           "before_fingerprint": hashlib.sha256(b"verification").hexdigest(),
                           "snapshot_hash": hashlib.sha256(b"verification").hexdigest(),
                           "snapshot": {"verification_scope_id": project_id,
                                        "workspace_identity_sha256": hashlib.sha256(str(workspace).encode()).hexdigest()}}), now, "valid"))
        conn.execute("INSERT INTO scope_paths(scope_id,environment_id,path) VALUES(?,?,?)",
                     (project_id, db.environment_id, str(workspace)))
        conn.execute("INSERT INTO requests(request_id,fingerprint_version,request_fingerprint,response_json,exit_code,deterministic,actor,session_id,created_at) "
                     "VALUES(?,1,?,?,0,1,'main','source-session',?)", (history_request_id := new_id(), hashlib.sha256(b"req").hexdigest(),
                     canonical_json({"claim_token": SECRET}), now))
        conn.execute("INSERT INTO routing_settings(id,revision,body_json,updated_at) VALUES(?,1,?,?)",
                     (new_id(), canonical_json({"token": SECRET, "command": ["local"]}), now))
        conn.execute("INSERT INTO operation_journal(id,kind,state,body_json,created_at,updated_at) VALUES(?,?, 'completed',?,?,?)",
                     (new_id(), "fixture", canonical_json({"pid": 1234, "workspace": str(workspace)}), now, now))
        conn.execute("INSERT INTO phase3_objects(kind,id,scope_id,owner_actor,owner_session,source_hash,revision,body_json,state,created_at,updated_at) "
                     "VALUES('task_context',?,?, 'main','source-session',?,1,?,'ready',?,?)",
                     (new_id(), project_id, hashlib.sha256(b"source").hexdigest(),
                      canonical_json({"workspace": str(workspace), "claim_token": SECRET}), now, now))
        conn.execute("INSERT INTO phase3_outbox(id,kind,request_id,scope_id,owner_actor,owner_session,body_json,state,attempts,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,?,?,'done',1,?,?)", (new_id(), "fixture", new_id(), project_id,
                     "main", "source-session", canonical_json({"pid": 1234}), now, now))

    spool_file = db.root / "runner-spool" / run_id / "receipt.json"
    spool_file.parent.mkdir(parents=True)
    spool_file.write_text(canonical_json({"run_id": run_id, "state": "completed", "raw_path": str(workspace)}),
                          encoding="utf-8")

    source_host, source_headers, source_device = _host(db, session="source-host-session")
    with db.write() as conn:
        conn.execute("INSERT INTO host_resource_metadata(artifact_id,scope_id,purpose,private_step_id,run_id,directive_version,owner_device_id,owner_session_id,request_id,created_at) "
                     "VALUES(?,?,'evidence',NULL,NULL,NULL,?,?,?,?)", (evidence_ref["artifact_id"], project_id,
                        source_device, source_headers["x-pmt-session"], new_id(), now))
    mapping = {"repository_id": repo_id, "local_workspace": str(workspace), "branch": branch}
    return {"db": db, "workspace": workspace, "repo_id": repo_id, "project_id": project_id,
            "classification_id": class_id, "work_id": work_id, "item_id": item_id,
            "step_id": step_id, "run_id": run_id, "branch": branch, "commit": commit,
            "directive_id": directive_ref["artifact_id"], "evidence_id": evidence_ref["artifact_id"],
            "evidence_path": evidence_path, "mapping": mapping,
            "source_host": source_host, "source_device": source_device,
            "history_request_id": history_request_id, "spool_file": spool_file}


def _target_host(tmp_path):
    db = Database(tmp_path / "target-data", tmp_path / "target-config")
    app, headers, device_id = _host(db, session="target-import-session")
    return db, app, headers, device_id


def test_sanitized_backup_import_preserves_business_ids_resources_and_target_auth(tmp_path):
    source = _source_case(tmp_path)
    target_db, target_host, target_headers, target_device = _target_host(tmp_path)
    namespace = target_host.auth.namespace_id
    coordinator = MigrationCoordinator()
    bundle = tmp_path / "migration-bundle"
    original_evidence = source["evidence_path"]
    source_bytes = original_evidence.read_bytes()
    source_run_count = 1
    receipt = coordinator.create_backup(source["db"], bundle, [source["mapping"]])
    manifest = coordinator.verify_backup(bundle)
    assert manifest["tier"] == "local-fixture-preparation"
    assert manifest["source_versions"]["db_schema"] == SCHEMA_VERSION
    assert manifest["portable_workspace_mappings"][0]["canonical_workspace"] == canonical_workspace(
        source["repo_id"], source["branch"])
    assert manifest["baseline_mapping_status"][0]["mapping_status"] == "current_source_match"
    assert manifest["baseline_mapping_status"][0]["ready_for_resume"] is False
    assert manifest["tables"]["execution_runs"]["count"] == source_run_count
    assert manifest["derived_state_invalidated"]["count"] == 1
    assert manifest["request_id_history"]["ids"] == [source["history_request_id"]]
    assert manifest["terminal_runner_spool_preserved_in_place"]["file_count"] == 1
    assert source["spool_file"].is_file()
    assert (bundle / "resources" / (hashlib.sha256(source_bytes).hexdigest())).read_bytes() == source_bytes
    for path in bundle.rglob("*"):
        if path.is_file():
            assert SECRET.encode() not in path.read_bytes()
            assert str(source["workspace"]).encode() not in path.read_bytes()
            assert b"runner-spool" not in path.read_bytes()
    with closing(sqlite3.connect(bundle / "transfer.sqlite3")) as staged:
        staged.row_factory = sqlite3.Row
        assert staged.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert staged.execute("SELECT COUNT(*) FROM scope_paths").fetchone()[0] == 0
        assert staged.execute("SELECT COUNT(*) FROM routing_settings").fetchone()[0] == 0
        assert staged.execute("SELECT COUNT(*) FROM host_devices").fetchone()[0] == 0
        assert staged.execute("SELECT COUNT(*) FROM phase3_objects").fetchone()[0] == 0
        run = dict(staged.execute("SELECT * FROM execution_runs WHERE id=?", (source["run_id"],)).fetchone())
        assert run["workspace"] == manifest["portable_workspace_mappings"][0]["canonical_workspace"]
        assert run["owner_session"].startswith("migration:")
        assert run["handle_json"] is None
        route = json.loads(run["route_json"])
        assert route["agent"] == "codex" and route["model"] == "local-fixture" and route["mode"] == "cli"
        assert not {"command", "env", "pid"} & set(route)
        verification = staged.execute("SELECT state,environment_id,command_json FROM verifications").fetchone()
        assert verification["state"] == "migrated_stale"
        assert verification["environment_id"].startswith("migrated:")
        assert "workspace_identity_sha256" in verification["command_json"] or "source_snapshot_sha256" in verification["command_json"]

    imported = coordinator.stage_import(target_host, bundle, target_headers)
    assert imported["state"] == "imported"
    assert target_host.auth.namespace_id == namespace
    with closing(target_db.connect()) as conn:
        assert conn.execute("SELECT 1 FROM host_devices WHERE id=?", (target_device,)).fetchone()
        assert conn.execute("SELECT 1 FROM host_devices WHERE id=?", (source["source_device"],)).fetchone() is None
        for table, identifier in (("scopes", source["project_id"]), ("scopes", source["classification_id"]),
                                  ("records", source["work_id"]), ("records", source["item_id"]),
                                  ("records", source["step_id"]), ("execution_runs", source["run_id"]),
                                  ("artifacts", source["evidence_id"])):
            assert conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (identifier,)).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM scope_paths").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM phase3_objects").fetchone()[0] == 0
        metadata = conn.execute("SELECT owner_device_id,owner_session_id,purpose FROM host_resource_metadata WHERE artifact_id=?",
                                (source["evidence_id"],)).fetchone()
        assert metadata[0] == target_device and metadata[1] == target_headers["x-pmt-session"]
    read = HostResourceStore(target_db, target_host.auth).read({"resource_id": source["evidence_id"]}, target_headers)
    assert read["content"] == source_bytes and read["resource_id"] == source["evidence_id"]
    replay = coordinator.restore_backup(target_host, bundle, target_headers)
    assert replay["state"] == "replayed" and replay["manifest_sha256"] == imported["manifest_sha256"]
    assert original_evidence.read_bytes() == source_bytes
    with closing(source["db"].connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM records WHERE id=?", (source["step_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""


def test_quiescence_blocks_running_execution_and_always_releases_source_barrier(tmp_path):
    source = _source_case(tmp_path)
    with source["db"].write() as conn:
        conn.execute("UPDATE execution_runs SET state='running' WHERE id=?", (source["run_id"],))
    with pytest.raises(PmtError, match="active work") as caught:
        MigrationCoordinator().create_backup(source["db"], tmp_path / "blocked-bundle", [source["mapping"]])
    assert caught.value.code == "migration_source_not_quiescent"
    with closing(source["db"].connect()) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""
        assert conn.execute("SELECT state FROM execution_runs WHERE id=?", (source["run_id"],)).fetchone()[0] == "running"
    assert not (tmp_path / "blocked-bundle").exists()


@pytest.mark.parametrize("busy_kind", ["claim", "phase3_journal", "phase3_outbox", "host_local_file_effect"])
def test_unsettled_claim_journal_or_outbox_blocks_export(tmp_path, busy_kind):
    source = _source_case(tmp_path)
    now = utc_now()
    with source["db"].write() as conn:
        if busy_kind == "claim":
            conn.execute("INSERT INTO claims(record_id,owner_session,token_hash,claimed_at,heartbeat_at,claim_event_id) "
                         "VALUES(?,?,?,?,?,NULL)", (source["step_id"], "source-session",
                         hashlib.sha256(b"fixture-token").hexdigest(), now, now))
        elif busy_kind == "phase3_journal":
            conn.execute("INSERT INTO phase3_journal(id,request_id,event_id,kind,scope_id,owner_actor,owner_session,body_json,created_at) "
                         "VALUES(?,?,NULL,'fixture',?,'main','source-session','{}',?)",
                         (new_id(), new_id(), source["project_id"], now))
        elif busy_kind == "phase3_outbox":
            conn.execute("INSERT INTO phase3_outbox(id,kind,request_id,scope_id,owner_actor,owner_session,body_json,state,attempts,created_at,updated_at) "
                         "VALUES(?,?,?,?,'main','source-session','{}','pending',0,?,?)",
                         (new_id(), "fixture", new_id(), source["project_id"], now, now))
        else:
            conn.execute("INSERT INTO phase3_objects(kind,id,scope_id,owner_actor,owner_session,source_hash,revision,body_json,state,created_at,updated_at) "
                         "VALUES('host_local_file_effect',?,?,?,?,?,1,'{}','intent',?,?)",
                         (new_id(), source["project_id"], "main", "source-session", "a" * 64, now, now))
    with pytest.raises(PmtError) as caught:
        MigrationCoordinator().create_backup(source["db"], tmp_path / "blocked-bundle", [source["mapping"]])
    assert caught.value.code == "migration_source_not_quiescent"
    with closing(source["db"].connect()) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""


def test_missing_workspace_mapping_stays_unknown_instead_of_fabricating_a_canonical_uri(tmp_path):
    source = _source_case(tmp_path)
    bundle = tmp_path / "unmapped-bundle"
    manifest = MigrationCoordinator().create_backup(source["db"], bundle, [])
    with closing(sqlite3.connect(bundle / "transfer.sqlite3")) as staged:
        workspace = staged.execute("SELECT workspace FROM execution_runs WHERE id=?",
                                   (source["run_id"],)).fetchone()[0]
        baseline_workspace = staged.execute("SELECT workspace FROM project_baselines WHERE scope_id=?",
                                             (source["project_id"],)).fetchone()[0]
    assert workspace.startswith("unknown-workspace:")
    assert baseline_workspace.startswith("unknown-workspace:")
    assert manifest["baseline_mapping_status"][0]["mapping_status"] == "workspace_mapping_unknown"
    assert manifest["baseline_mapping_status"][0]["ready_for_resume"] is False


def test_active_target_claim_blocks_import_without_overwriting_target(tmp_path):
    source = _source_case(tmp_path)
    bundle = tmp_path / "migration-bundle"
    MigrationCoordinator().create_backup(source["db"], bundle, [source["mapping"]])
    target_db, host, headers, device = _target_host(tmp_path)
    project_id, item_id = new_id(), new_id()
    now = utc_now()
    with target_db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,body_json,created_at,updated_at) VALUES(?,'project','busy','{}',?,?)",
                     (project_id, now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,created_at,updated_at) "
                     "VALUES(?,'item',?,'Active target item','InProgress','{}',?,?)",
                     (item_id, project_id, now, now))
        conn.execute("INSERT INTO host_claim_leases(id,record_id,scope_id,device_id,actor,session_id,key_id,fingerprint,generation,state,created_at) "
                     "VALUES(?,?,?,?,?,?,?, ?,1,'active',?)", (new_id(), item_id, project_id, device, "main",
                        headers["x-pmt-session"], "fixture", hashlib.sha256(b"claim").hexdigest(), now))
    with pytest.raises(PmtError) as caught:
        MigrationCoordinator().restore_backup(host, bundle, headers)
    assert caught.value.code == "migration_target_not_quiescent"
    with closing(target_db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""


def test_duplicate_source_id_collision_refuses_import_without_rewriting_existing_record(tmp_path):
    source = _source_case(tmp_path)
    bundle = tmp_path / "migration-bundle"
    MigrationCoordinator().create_backup(source["db"], bundle, [source["mapping"]])
    target_db, host, headers, _device = _target_host(tmp_path)
    now = utc_now()
    with target_db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,body_json,created_at,updated_at) VALUES(?,'project','preexisting','{}',?,?)",
                     (source["project_id"], now, now))
    with pytest.raises(PmtError) as caught:
        MigrationCoordinator().restore_backup(host, bundle, headers)
    assert caught.value.code == "migration_target_not_empty"
    with closing(target_db.connect()) as conn:
        row = conn.execute("SELECT kind,slug FROM scopes WHERE id=?", (source["project_id"],)).fetchone()
        assert tuple(row) == ("project", "preexisting")


def test_target_schema_mismatch_is_rejected_before_resource_or_row_publication(tmp_path):
    source = _source_case(tmp_path)
    bundle = tmp_path / "migration-bundle"
    MigrationCoordinator().create_backup(source["db"], bundle, [source["mapping"]])
    target_db, host, headers, _device = _target_host(tmp_path)
    with target_db.write() as conn:
        conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    with pytest.raises(PmtError) as caught:
        MigrationCoordinator().restore_backup(host, bundle, headers)
    assert caught.value.code == "migration_target_version_incompatible"
    with closing(target_db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""


def test_target_must_be_business_empty_but_keeps_admin_device_and_namespace(tmp_path):
    source = _source_case(tmp_path)
    bundle = tmp_path / "migration-bundle"
    MigrationCoordinator().create_backup(source["db"], bundle, [source["mapping"]])
    target_db, host, headers, device = _target_host(tmp_path)
    request = {"protocol_version": 1, "request_id": new_id(), "operation": "create_scope",
        "actor": "main", "session_id": headers["x-pmt-session"], "payload": {"kind": "project", "slug": "preexisting"}}
    envelope, code = host.execute(request, headers)
    assert code == 0 and envelope["ok"]
    old_namespace = host.auth.namespace_id
    with pytest.raises(PmtError, match="business data") as caught:
        MigrationCoordinator().restore_backup(host, bundle, headers)
    assert caught.value.code == "migration_target_not_empty"
    with closing(target_db.connect()) as conn:
        assert conn.execute("SELECT 1 FROM host_devices WHERE id=?", (device,)).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM meta WHERE key='host_namespace_id'").fetchone()[0] == old_namespace


def test_bundle_manifest_and_resource_corruption_are_rejected(tmp_path):
    source = _source_case(tmp_path)
    bundle = tmp_path / "migration-bundle"
    MigrationCoordinator().create_backup(source["db"], bundle, [source["mapping"]])
    manifest_path = bundle / "manifest.json"
    original_manifest = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(original_manifest)
    manifest["source_versions"]["core"] = "9.9.9"
    manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
    with pytest.raises(PmtError) as caught:
        MigrationCoordinator().verify_backup(bundle)
    assert caught.value.code == "migration_manifest_corrupt"
    manifest_path.write_text(original_manifest, encoding="utf-8")
    resource_file = bundle / "resources" / manifest["resources"][0]["sha256"]
    resource_file.write_bytes(resource_file.read_bytes() + b"tamper")
    with pytest.raises(PmtError) as caught:
        MigrationCoordinator().verify_backup(bundle)
    assert caught.value.code == "migration_bundle_corrupt"


def test_resource_publish_crash_retries_and_postcommit_crash_replays(tmp_path, monkeypatch):
    source = _source_case(tmp_path)
    bundle = tmp_path / "migration-bundle"
    coordinator = MigrationCoordinator()
    coordinator.create_backup(source["db"], bundle, [source["mapping"]])
    target_db, host, headers, _device = _target_host(tmp_path)
    original_publish = coordinator._publish_resources
    def crash_after_resource_publication(target, root, manifest):
        original_publish(target, root, manifest)
        raise RuntimeError("fixture crash after immutable blob publication")
    monkeypatch.setattr(coordinator, "_publish_resources", crash_after_resource_publication)
    with pytest.raises(RuntimeError, match="fixture crash"):
        coordinator.restore_backup(host, bundle, headers)
    with closing(target_db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0] == 0
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""
    monkeypatch.setattr(coordinator, "_publish_resources", original_publish)
    crash_once = {"enabled": True}
    original_after_commit = coordinator._after_commit
    def crash_after_sql_commit(target, manifest):
        if crash_once["enabled"]:
            crash_once["enabled"] = False
            raise RuntimeError("fixture crash after SQL commit")
        return original_after_commit(target, manifest)
    monkeypatch.setattr(coordinator, "_after_commit", crash_after_sql_commit)
    with pytest.raises(RuntimeError, match="after SQL commit"):
        coordinator.restore_backup(host, bundle, headers)
    with closing(target_db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM scopes").fetchone()[0] == 3
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""
    replay = coordinator.restore_backup(host, bundle, headers)
    assert replay["state"] == "replayed"
    with closing(target_db.connect()) as conn:
        assert conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 2


def test_primary_switch_and_schema_mismatch_remain_explicitly_unsupported(tmp_path):
    source = _source_case(tmp_path)
    coordinator = MigrationCoordinator()
    with pytest.raises(PmtError) as caught:
        coordinator.switch_primary(source["db"], "verified-mapping-required")
    assert caught.value.code == "migration_primary_switch_unavailable"
    with source["db"].write() as conn:
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(PmtError) as caught:
        coordinator.create_backup(source["db"], tmp_path / "bad-version", [source["mapping"]])
    assert caught.value.code == "migration_schema_unsupported"


def test_backup_import_preserves_shared_continuity_refs_and_invalidates_private_projection(tmp_path):
    source = _source_case(tmp_path)
    request = {"request_id": new_id(), "operation": "put_continuity_object",
        "actor": "main", "session_id": "source-session", "scope_id": source["project_id"],
        "payload": {}}
    store = ContinuityStore(source["db"])
    with source["db"].write() as conn:
        shared = store.put(conn, request, "basis", {"complete": True,
            "manifest": {"coherence": "coherent"}, "source": {"branch": source["branch"]},
            "evidence_ref": source["evidence_id"]},
            event_id=new_id())
        private = store.put(conn, request, "detail", {"private_summary": "synthetic detail"},
            visibility="private")
        selector = {"repository_id": source["repo_id"], "branch": source["branch"],
            "workspace_ref": canonical_workspace(source["repo_id"], source["branch"]),
            "task_id": None, "purpose": "basis", "environment_id": source["db"].environment_id}
        pointer = store.advance_pointer(conn, request, selector, shared["id"], 0)

    bundle = tmp_path / "continuity-bundle"
    manifest = MigrationCoordinator().create_backup(source["db"], bundle, [source["mapping"]])
    assert manifest["continuity_private_invalidated"] == {
        "count": 1, "ids_sha256": hashlib.sha256(canonical_json([private["id"]]).encode()).hexdigest(),
        "reason": "private_session_projection_requires_recreation_by_current_owner"}
    assert manifest["tables"]["continuity_objects"]["count"] == 1
    assert manifest["tables"]["continuity_pointers"]["count"] == 1
    with closing(sqlite3.connect(bundle / "transfer.sqlite3")) as staged:
        staged.row_factory = sqlite3.Row
        migrated_object = staged.execute("SELECT * FROM continuity_objects WHERE id=?", (shared["id"],)).fetchone()
        migrated_pointer = staged.execute("SELECT object_id,revision FROM continuity_pointers WHERE pointer_key=?",
                                          (pointer["pointer_key"],)).fetchone()
        assert migrated_object is not None and migrated_object["body_hash"] == shared["body_hash"]
        assert migrated_pointer["object_id"] == shared["id"] and migrated_pointer["revision"] == 1
        assert staged.execute("SELECT 1 FROM continuity_objects WHERE id=?", (private["id"],)).fetchone() is None
        assert staged.execute("SELECT 1 FROM artifact_refs WHERE owner_type='continuity' AND owner_id=?",
                              (shared["id"],)).fetchone() is not None

    target_db, target_host, target_headers, _device = _target_host(tmp_path)
    imported = MigrationCoordinator().restore_backup(target_host, bundle, target_headers)
    assert imported["state"] == "imported"
    with closing(target_db.connect()) as conn:
        row = conn.execute("SELECT body_json,body_hash,owner_actor,owner_session FROM continuity_objects WHERE id=?",
                           (shared["id"],)).fetchone()
        assert row is not None and hashlib.sha256(canonical_json(json.loads(row["body_json"])).encode()).hexdigest() == row["body_hash"]
        assert row["owner_actor"].startswith("migration:") and row["owner_session"].startswith("migration:")
        assert conn.execute("SELECT COUNT(*) FROM continuity_pointers WHERE object_id=?", (shared["id"],)).fetchone()[0] == 1


def test_unresolved_continuity_effect_blocks_backup_and_preserves_journal(tmp_path):
    source = _source_case(tmp_path)
    request = {"request_id": new_id(), "operation": "begin_continuity_effect",
        "actor": "main", "session_id": "source-session", "scope_id": source["project_id"],
        "payload": {}}
    with source["db"].write() as conn:
        effect = ContinuityStore(source["db"]).begin_effect(conn, request,
            "fixture_publication", {"outcome_ref": "synthetic"})
    with pytest.raises(PmtError) as caught:
        MigrationCoordinator().create_backup(source["db"], tmp_path / "blocked-continuity", [source["mapping"]])
    assert caught.value.code == "migration_source_not_quiescent"
    assert caught.value.details["counts"]["continuity_journal"] == 1
    with closing(source["db"].connect()) as conn:
        journal = conn.execute("SELECT state,body_hash FROM continuity_journal WHERE id=?", (effect["id"],)).fetchone()
        assert journal["state"] == "prepared" and journal["body_hash"] == effect["body_hash"]
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""
    assert not (tmp_path / "blocked-continuity").exists()
