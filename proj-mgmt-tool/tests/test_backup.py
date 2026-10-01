from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

from pmt.db import Database
from pmt.resources import execute
import pmt.resources as resources


def _request(op, payload=None):
    return {"protocol_version": 1, "operation": op, "request_id": str(uuid.uuid4()),
            "actor": "test", "session_id": "backup", "payload": payload or {}}


def test_backup_restore_roundtrip_preserves_database_and_blobs(tmp_path):
    db = Database(tmp_path / "live-data", tmp_path / "config")
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('scope','project','project','t','t')")
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES('record','item','scope','item','t','t')")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "evidence.bin"
    source.write_bytes(b"round trip evidence")
    registered, code = execute(db, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "evidence",
        "owner_record_id": "record"}))
    assert code == 0
    backup_dir = tmp_path / "backup"
    backup, code = execute(db, _request("backup", {"destination_root": str(backup_dir)}))
    assert code == 0 and backup["ok"]
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 2 and len(manifest["artifacts"]) == 1
    restore_dir = tmp_path / "restored"
    restore_request = _request("restore", {"backup_path": str(backup_dir), "destination_root": str(restore_dir)})
    restored, code = execute(db, restore_request)
    assert code == 0 and restored["result"]["restored"]
    restored_db = Database(restore_dir, tmp_path / "restored-config")
    with restored_db.connect() as conn:
        assert conn.execute("SELECT title FROM records WHERE id='record'").fetchone()[0] == "item"
        artifact_id = registered["result"]["artifact_id"]
        row = conn.execute("SELECT sha256,relative_path,state FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        assert tuple(row) == (registered["result"]["sha256"], f"resources/objects/{artifact_id}", "ready")
        assert conn.execute("SELECT count(*) FROM artifact_refs WHERE artifact_id=?", (artifact_id,)).fetchone()[0] == 1
    assert (restore_dir / "resources" / "objects" / artifact_id).read_bytes() == b"round trip evidence"
    assert execute(db, restore_request) == (restored, code)


def test_restore_rejects_corrupt_manifest_blob_without_publishing(tmp_path):
    db = Database(tmp_path / "live-data", tmp_path / "config")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "proof"
    source.write_text("proof")
    registered, code = execute(db, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "evidence"}))
    assert code == 0
    backup_dir = tmp_path / "backup"
    assert execute(db, _request("backup", {"destination_root": str(backup_dir)}))[1] == 0
    artifact_id = registered["result"]["artifact_id"]
    (backup_dir / "resources" / artifact_id).write_text("tampered")
    destination = tmp_path / "restored"
    response, code = execute(db, _request("restore", {
        "backup_path": str(backup_dir), "destination_root": str(destination)}))
    assert code == 2 and response["error"]["code"] == "backup_hash_mismatch"
    assert not (destination / "pmt.sqlite3").exists()


def test_backup_rejects_active_resource_job_and_nonempty_destination(tmp_path):
    db = Database(tmp_path / "live-data", tmp_path / "config")
    with db.write() as conn:
        conn.execute("INSERT INTO file_jobs(id,operation,owner,state,started_at,updated_at) VALUES('active','register','test','active','t','t')")
    blocked, code = execute(db, _request("backup", {"destination_root": str(tmp_path / "backup")}))
    assert code == 4 and blocked["error"]["code"] == "maintenance_unavailable"
    with db.write() as conn:
        conn.execute("UPDATE file_jobs SET state='completed' WHERE id='active'")
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep").write_text("keep")
    rejected, code = execute(db, _request("backup", {"destination_root": str(nonempty)}))
    assert code == 2 and rejected["error"]["code"] == "destination_not_empty"
    assert (nonempty / "keep").read_text() == "keep"


def test_backup_same_request_concurrent_and_replay_copies_once(tmp_path, monkeypatch):
    db = Database(tmp_path / "live-data", tmp_path / "config")
    destination = tmp_path / "backup"
    req = _request("backup", {"destination_root": str(destination)})
    entered, release = threading.Event(), threading.Event()
    release_entered, allow_maintenance_release = threading.Event(), threading.Event()
    original = resources._hash
    original_release = resources._release_maintenance

    def blocked_hash(path):
        if str(path).endswith("pmt.sqlite3"):
            entered.set()
            assert release.wait(5)
        return original(path)

    def held_maintenance_release(target_db, owner):
        release_entered.set()
        assert allow_maintenance_release.wait(5)
        return original_release(target_db, owner)

    monkeypatch.setattr(resources, "_hash", blocked_hash)
    monkeypatch.setattr(resources, "_release_maintenance", held_maintenance_release)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, db, req)
        assert entered.wait(5)
        second = pool.submit(execute, db, req)
        release.set()
        assert release_entered.wait(5)
        two = second.result(timeout=5)
        changed_while_maintenance = {**req, "payload": {"destination_root": str(tmp_path / "other-during-backup")}}
        conflict, conflict_code = execute(db, changed_while_maintenance)
        assert conflict_code == 3 and conflict["error"]["code"] == "request_conflict"
        assert not (tmp_path / "other-during-backup").exists()
        allow_maintenance_release.set()
        one = first.result(timeout=10)
    assert one == two and one[1] == 0
    assert execute(db, req) == one
    changed = {**req, "payload": {"destination_root": str(tmp_path / "other-backup")}}
    conflict, code = execute(db, changed)
    assert code == 3 and conflict["error"]["code"] == "request_conflict"
    assert not (tmp_path / "other-backup").exists()
    with db.connect() as conn:
        jobs = conn.execute("SELECT state FROM file_jobs WHERE operation='backup' AND owner=?", (req["request_id"],)).fetchall()
        assert [row[0] for row in jobs] == ["completed"]


def test_backup_maintenance_blocks_independent_cli_writer_and_resource_registration(cli, request_factory, tmp_path, monkeypatch):
    scope_process = cli.call(request_factory("create_scope", {"kind": "project", "slug": "maintenance"}))
    assert scope_process.returncode == 0
    scope_id = json.loads(scope_process.stdout)["result"]["scope_id"]
    db = Database(cli.data_root, cli.config_root)
    destination = tmp_path / "backup"
    entered, release = threading.Event(), threading.Event()
    original = resources._hash
    def wait_during_snapshot(path):
        if path == destination / "pmt.sqlite3":
            entered.set()
            assert release.wait(10)
        return original(path)
    monkeypatch.setattr(resources, "_hash", wait_during_snapshot)
    source = tmp_path / "evidence.txt"
    source.write_text("preserve evidence", encoding="utf-8")
    with ThreadPoolExecutor(max_workers=1) as pool:
        backup = pool.submit(execute, db, _request("backup", {"destination_root": str(destination)}))
        assert entered.wait(5)
        try:
            write_req = request_factory("save_change", {"kind": "item", "title": "must wait", "reason": "contend"}, scope_id=scope_id)
            failed_write = cli.call(write_req)
            result = json.loads(failed_write.stdout)
            assert failed_write.returncode == 4 and result["error"]["code"] == "maintenance_active"
            assert result["error"]["retryable"] is True
            failed_resource = cli.call(request_factory("register_resource", {
                "source_path": str(source), "allowed_root": str(tmp_path), "retention": "evidence"}, scope_id=scope_id))
            assert failed_resource.returncode == 4
            assert json.loads(failed_resource.stdout)["error"]["code"] == "maintenance_active"
            context = cli.call(request_factory("read_context", scope_id=scope_id))
            assert context.returncode == 0 and json.loads(context.stdout)["result"]["records"] == []
        finally:
            release.set()
        saved, code = backup.result(timeout=10)
    assert code == 0 and saved["ok"]
    retried = cli.call(write_req)
    assert retried.returncode == 0 and json.loads(retried.stdout)["ok"]


def test_backup_copy_failure_marks_incomplete_and_releases_maintenance(tmp_path, monkeypatch):
    db = Database(tmp_path / "live-data", tmp_path / "config")
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('scope','project','p','t','t')")
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES('record','item','scope','keep','t','t')")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "proof"
    source.write_text("proof")
    assert execute(db, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "evidence"}))[1] == 0
    before = None
    with db.connect() as conn:
        before = [tuple(row) for row in conn.execute("SELECT id,title,state FROM records ORDER BY id")]
    destination = tmp_path / "failed-backup"
    original = resources.shutil.copyfile

    def fail_blob_copy(src, dst):
        if str(dst).startswith(str(destination)):
            raise OSError("injected blob-copy failure")
        return original(src, dst)

    monkeypatch.setattr(resources.shutil, "copyfile", fail_blob_copy)
    req = _request("backup", {"destination_root": str(destination)})
    response, code = execute(db, req)
    assert code == 4 and response["error"]["code"] == "backup_io_error"
    manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "incomplete"
    with db.connect() as conn:
        after = [tuple(row) for row in conn.execute("SELECT id,title,state FROM records ORDER BY id")]
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""
        job = conn.execute("SELECT state,error_code FROM file_jobs WHERE operation='backup' AND owner=?", (req["request_id"],)).fetchone()
        assert tuple(job) == ("failed", "backup_failed")
    assert after == before


def test_restore_bad_schema_and_missing_blob_leave_live_records_unchanged(tmp_path):
    db = Database(tmp_path / "live-data", tmp_path / "config")
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('scope','project','p','t','t')")
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES('record','item','scope','keep','t','t')")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "proof"
    source.write_text("proof")
    registered, code = execute(db, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "evidence"}))
    assert code == 0
    backup_dir = tmp_path / "backup"
    assert execute(db, _request("backup", {"destination_root": str(backup_dir)}))[1] == 0
    artifact_id = registered["result"]["artifact_id"]
    with db.connect() as conn:
        before = [tuple(row) for row in conn.execute("SELECT id,title,state FROM records ORDER BY id")]

    # Corrupt the backup DB schema while keeping its manifest hash internally consistent.
    db_file = backup_dir / "pmt.sqlite3"
    with sqlite3.connect(db_file) as conn:
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    manifest_file = backup_dir / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    raw = db_file.read_bytes()
    manifest["database"]["sha256"] = hashlib.sha256(raw).hexdigest()
    manifest["database"]["size_bytes"] = len(raw)
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")
    bad_schema, code = execute(db, _request("restore", {"backup_path": str(backup_dir), "destination_root": str(tmp_path / "restore-schema")}))
    assert code == 2 and bad_schema["error"]["code"] == "restore_database_invalid"

    # Restore the valid DB copy from an independent backup, then remove a required blob.
    backup_two = tmp_path / "backup-two"
    assert execute(db, _request("backup", {"destination_root": str(backup_two)}))[1] == 0
    (backup_two / "resources" / artifact_id).unlink()
    missing_blob, code = execute(db, _request("restore", {"backup_path": str(backup_two), "destination_root": str(tmp_path / "restore-blob")}))
    assert code == 2 and missing_blob["error"]["code"] == "backup_hash_mismatch"
    with db.connect() as conn:
        after = [tuple(row) for row in conn.execute("SELECT id,title,state FROM records ORDER BY id")]
    assert after == before


def test_restore_same_request_concurrent_publishes_once(tmp_path, monkeypatch):
    db = Database(tmp_path / "live-data", tmp_path / "config")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "proof"
    source.write_text("proof")
    registered, code = execute(db, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "evidence"}))
    assert code == 0
    backup = tmp_path / "backup"
    assert execute(db, _request("backup", {"destination_root": str(backup)}))[1] == 0
    req = _request("restore", {"backup_path": str(backup), "destination_root": str(tmp_path / "restored")})
    entered, release = threading.Event(), threading.Event()
    original = resources.shutil.copyfile

    def blocked_copy(src, dst):
        if "pmt-restore-" in str(dst):
            entered.set()
            assert release.wait(5)
        return original(src, dst)

    monkeypatch.setattr(resources.shutil, "copyfile", blocked_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, db, req)
        assert entered.wait(5)
        second = pool.submit(execute, db, req)
        release.set()
        one, two = first.result(timeout=10), second.result(timeout=10)
    assert one == two and one[1] == 0
    changed = {**req, "payload": {**req["payload"], "destination_root": str(tmp_path / "other-restored")}}
    conflict, code = execute(db, changed)
    assert code == 3 and conflict["error"]["code"] == "request_conflict"
    assert not (tmp_path / "other-restored").exists()
    artifact_id = registered["result"]["artifact_id"]
    assert (tmp_path / "restored" / "resources" / "objects" / artifact_id).read_text() == "proof"
    with db.connect() as conn:
        jobs = conn.execute("SELECT state FROM file_jobs WHERE operation='restore' AND owner=?", (req["request_id"],)).fetchall()
        assert [row[0] for row in jobs] == ["completed"]
