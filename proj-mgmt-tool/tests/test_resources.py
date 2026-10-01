from __future__ import annotations

import uuid
import threading
import os
import subprocess
import hashlib
from concurrent.futures import ThreadPoolExecutor
import pytest
from pmt.errors import PmtError

from pmt.db import Database
from pmt.resources import check_artifact, execute
import pmt.resources as resources


def _request(op, payload=None, **extra):
    return {"protocol_version": 1, "operation": op, "request_id": str(uuid.uuid4()),
            "actor": "test", "session_id": "resources", "payload": payload or {}, **extra}


def _db(tmp_path):
    return Database(tmp_path / "data", tmp_path / "config")


def test_resource_stages_hashes_and_registers_reference(tmp_path):
    db = _db(tmp_path)
    source_root = tmp_path / "allowed"
    source_root.mkdir()
    source = source_root / "proof.txt"
    source.write_bytes(b"verified evidence")
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('scope','project','p','t','t')")
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES('record','item','scope','proof','t','t')")
    response, code = execute(db, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "evidence",
        "owner_record_id": "record"}, scope_id="scope"))
    assert code == 0 and response["ok"]
    result = response["result"]
    assert result["size_bytes"] == len(b"verified evidence")
    with db.connect() as conn:
        assert check_artifact(db, conn, result["artifact_id"]) == {
            "valid": True, "reason": "ready", "sha256": result["sha256"]}
        assert conn.execute("SELECT purpose FROM artifact_refs WHERE artifact_id=?", (result["artifact_id"],)).fetchone()[0] == "evidence"
        assert conn.execute("SELECT retention_until FROM artifacts WHERE id=?", (result["artifact_id"],)).fetchone()[0] is None


def test_resource_rejects_escape_and_symlink(tmp_path):
    db = _db(tmp_path)
    root = tmp_path / "allowed"
    root.mkdir()
    external = tmp_path / "external.txt"
    external.write_text("outside")
    escaped, code = execute(db, _request("register_resource", {
        "source_path": str(external), "allowed_root": str(root), "retention": "temporary"}))
    assert code == 2 and escaped["error"]["code"] == "resource_path_outside_root"
    link = root / "linked.txt"
    try:
        link.symlink_to(external)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symlinks")
    rejected, code = execute(db, _request("register_resource", {
        "source_path": str(link), "allowed_root": str(root), "retention": "temporary"}))
    assert code == 2 and rejected["error"]["code"] == "resource_link_rejected"
    assert external.read_text() == "outside"


def test_diagnose_reports_missing_corrupt_orphan_and_retention_candidate(tmp_path):
    db = _db(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "temporary.txt"
    source.write_text("temporary")
    response, code = execute(db, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "temporary"}))
    assert code == 0
    artifact_id = response["result"]["artifact_id"]
    with db.write() as conn:
        conn.execute("UPDATE artifacts SET retention_until='2000-01-01T00:00:00Z' WHERE id=?", (artifact_id,))
    (db.root / "resources" / "objects" / artifact_id).write_text("changed")
    orphan = db.root / "resources" / "objects" / "untracked"
    orphan.write_text("orphan")
    diagnosed, code = execute(db, _request("diagnose"))
    assert code == 0
    result = diagnosed["result"]
    assert {entry["issue"] for entry in result["issues"]} == {"corrupt"}
    assert "resources/objects/untracked" in result["orphans"]
    assert result["retention_candidates"][0]["artifact_id"] == artifact_id
    assert result["automatic_deletion"] is False


def test_missing_artifact_is_not_valid(tmp_path):
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("INSERT INTO artifacts(id,sha256,size_bytes,relative_path,state,created_at) VALUES('missing','abc',1,'resources/objects/missing','ready','t')")
    with db.connect() as conn:
        assert check_artifact(db, conn, "missing") == {"valid": False, "reason": "missing_file", "sha256": "abc"}


def test_register_replay_and_concurrent_same_request_publish_once(tmp_path, monkeypatch):
    db = _db(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "file"
    source.write_text("content")
    req = _request("register_resource", {"source_path": str(source), "allowed_root": str(source_root), "retention": "evidence"})
    entered, release = threading.Event(), threading.Event()
    original = resources.shutil.copyfile
    copies = []

    def blocked_copy(src, dst):
        if str(dst).endswith(".part"):
            copies.append((str(src), str(dst)))
            entered.set()
            assert release.wait(5)
        return original(src, dst)

    monkeypatch.setattr(resources.shutil, "copyfile", blocked_copy)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, db, req)
        assert entered.wait(5)
        second = pool.submit(execute, db, req)
        release.set()
        first_result, second_result = first.result(timeout=10), second.result(timeout=10)
    assert first_result == second_result
    assert len(copies) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1
        jobs = conn.execute("SELECT state FROM file_jobs WHERE owner=?", (req["request_id"],)).fetchall()
        assert [row[0] for row in jobs] == ["completed"]
    assert execute(db, req) == first_result


def test_register_same_request_changed_payload_conflicts_before_second_copy(tmp_path, monkeypatch):
    db = _db(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "file"
    source.write_text("content")
    other = source_root / "other"
    other.write_text("other")
    req = _request("register_resource", {"source_path": str(source), "allowed_root": str(source_root), "retention": "evidence"})
    entered, release = threading.Event(), threading.Event()
    original = resources.shutil.copyfile
    copies = []

    def blocked_copy(src, dst):
        if str(dst).endswith(".part"):
            copies.append(str(src))
            entered.set()
            assert release.wait(5)
        return original(src, dst)

    monkeypatch.setattr(resources.shutil, "copyfile", blocked_copy)
    changed = {**req, "payload": {**req["payload"], "source_path": str(other)}}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, db, req)
        assert entered.wait(5)
        second = pool.submit(execute, db, changed)
        release.set()
        success, conflict = first.result(timeout=10), second.result(timeout=10)
    assert success[1] == 0 and conflict[1] == 3
    assert conflict[0]["error"]["code"] == "request_conflict"
    assert copies == [str(source)]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 1


def test_register_rejects_foreign_scope_and_owner_before_file_job(tmp_path):
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('one','project','one','t','t')")
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('two','project','two','t','t')")
        conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES('record','item','one','item','t','t')")
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "file"
    source.write_text("content")
    base = {"source_path": str(source), "allowed_root": str(source_root), "retention": "evidence", "owner_record_id": "record"}
    response, code = execute(db, _request("register_resource", base, scope_id="two"))
    assert code == 2 and response["error"]["code"] == "resource_owner_invalid"
    missing_scope, code = execute(db, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "evidence"}, scope_id="missing"))
    assert code == 2 and missing_scope["error"]["code"] == "scope_not_found"
    missing_owner, code = execute(db, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "evidence",
        "owner_record_id": "missing"}, scope_id="one"))
    assert code == 2 and missing_owner["error"]["code"] == "resource_owner_invalid"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM file_jobs").fetchone()[0] == 0


def test_register_db_failure_leaves_diagnosable_orphan_and_failed_job(tmp_path, monkeypatch):
    db = _db(tmp_path)
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "file"
    source.write_text("content")
    req = _request("register_resource", {"source_path": str(source), "allowed_root": str(source_root), "retention": "evidence"})

    def fail_handler(*_args, **_kwargs):
        from pmt.errors import PmtError
        raise PmtError("injected_db_failure", "Injected registration failure")

    monkeypatch.setattr(resources, "_register_handler", fail_handler)
    response, code = execute(db, req)
    assert code == 2 and response["error"]["code"] == "injected_db_failure"
    with db.connect() as conn:
        job = conn.execute("SELECT state,error_code FROM file_jobs WHERE owner=?", (req["request_id"],)).fetchone()
        assert tuple(job) == ("failed", "injected_db_failure")
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
    diagnosed, code = execute(db, _request("diagnose"))
    assert code == 0 and len(diagnosed["result"]["orphans"]) == 1


def test_diagnose_excludes_runtime_data_locations(tmp_path):
    db = _db(tmp_path)
    (db.root / "unrelated.txt").write_text("runtime is not resource data")
    (db.config_root / "unrelated-config.txt").write_text("configuration is not resource data")
    response, code = execute(db, _request("diagnose"))
    assert code == 0
    assert response["result"]["orphans"] == []


def test_diagnose_reports_resource_links_without_traversing_them(tmp_path):
    db = _db(tmp_path)
    object_dir = db.root / "resources" / "objects"
    object_dir.mkdir(parents=True)
    target = tmp_path / "external"
    target.mkdir()
    (target / "secret").write_text("do not traverse")
    link = object_dir / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("This Windows account cannot create symlinks")
    response, code = execute(db, _request("diagnose"))
    assert code == 0
    result = response["result"]
    assert result["orphans"] == []
    assert result["unsafe_paths"] == ["resources/objects/linked"]


def test_windows_junctions_are_rejected_without_traversing_target(tmp_path):
    if os.name != "nt":
        pytest.skip("NTFS junctions are Windows-only; POSIX symlink behavior has separate tests")
    db = _db(tmp_path)
    target = tmp_path / "outside-target"
    target.mkdir()
    sentinel = target / "sentinel.bin"
    sentinel.write_bytes(b"outside")
    source_root = tmp_path / "allowed-source"
    source_root.mkdir()
    source_link = source_root / "junction"
    object_root = db.root / "resources" / "objects"
    object_root.mkdir(parents=True)
    resource_link = object_root / "junction"

    def make_junction(link):
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        script = ("$ErrorActionPreference='Stop'; "
                  f"New-Item -ItemType Junction -Path {quote(link)} -Target {quote(target)} | Out-Null")
        try:
            completed = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                                       capture_output=True, text=True, timeout=15, check=False)
        except FileNotFoundError:
            pytest.skip("Windows PowerShell is unavailable for junction verification")
        assert completed.returncode == 0, completed.stderr or completed.stdout
        assert resources._is_reparse(link.lstat())

    make_junction(source_link)
    make_junction(resource_link)
    try:
        req = _request("register_resource", {
            "source_path": str(source_link / sentinel.name), "allowed_root": str(source_root), "retention": "evidence"})
        rejected, code = execute(db, req)
        assert code == 2 and rejected["error"]["code"] == "resource_link_rejected"
        with db.write() as conn:
            conn.execute("INSERT INTO artifacts(id,sha256,size_bytes,relative_path,state,created_at) VALUES(?,?,?,?,?,?)",
                         ("junction-artifact", hashlib.sha256(b"outside").hexdigest(), len(b"outside"),
                          "resources/objects/junction/sentinel.bin", "ready", "t"))
        with db.connect() as conn:
            assert check_artifact(db, conn, "junction-artifact") == {
                "valid": False, "reason": "unreadable_or_unsafe", "sha256": hashlib.sha256(b"outside").hexdigest()}
        diagnosed, code = execute(db, _request("diagnose"))
        assert code == 0
        result = diagnosed["result"]
        assert result["unsafe_paths"] == ["resources/objects/junction"]
        assert result["orphans"] == []
        assert sentinel.read_bytes() == b"outside"
    finally:
        for link in (resource_link, source_link):
            if link.exists():
                link.rmdir()  # Remove only the junction; the target is a separate directory.
        assert sentinel.read_bytes() == b"outside"


def test_active_maintenance_blocks_constructor_side_effects_and_resource_jobs(tmp_path):
    db = _db(tmp_path)
    with db.write() as conn:
        conn.execute("UPDATE meta SET value='backup-owner' WHERE key='maintenance_owner'")
    # Constructing a second service handle must not rewrite or clear active maintenance metadata.
    second = Database(db.root, db.config_root)
    with second.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == "backup-owner"
    source_root = tmp_path / "source"
    source_root.mkdir()
    source = source_root / "file"
    source.write_text("content")
    response, code = execute(second, _request("register_resource", {
        "source_path": str(source), "allowed_root": str(source_root), "retention": "evidence"}))
    assert code == 4 and response["error"]["code"] == "maintenance_active"
    with second.connect() as conn:
        assert conn.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM file_jobs").fetchone()[0] == 0
    assert not (second.root / "resources").exists()
    with pytest.raises(PmtError, match="maintenance"):
        with second.write():
            pass
