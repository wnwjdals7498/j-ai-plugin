import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

import pmt.db as db_module
from pmt.db import Database
from pmt.efficiency.source import SourcePin, pin_source, verify_source_pin
from pmt.efficiency.storage import Phase3Storage
from pmt.errors import PmtError
from pmt.store import LocalStore
from pmt.util import new_id


def _schema3(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    with db.connect() as conn:
        conn.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
        for name in ("phase3_outbox", "phase3_journal", "phase3_objects"):
            conn.execute(f"DROP TABLE IF EXISTS {name}")
    return db


def test_schema3_migration_has_backup_and_preserves_data(tmp_path):
    old = _schema3(tmp_path)
    db = Database(old.root, old.config_root)
    with db.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(db_module.SCHEMA_VERSION)
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='phase3_objects'").fetchone()
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('preserved','project','p','t','t')")
    backups = list(db.root.glob("pmt-schema3-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
        assert backup.execute("SELECT id FROM scopes WHERE id='preserved'").fetchone() is None
    Database(db.root, db.config_root)
    assert len(list(db.root.glob("pmt-schema3-*.sqlite3"))) == 1


def test_schema4_migration_rollback_preserves_schema3_and_backup(tmp_path, monkeypatch):
    db = _schema3(tmp_path)
    monkeypatch.setattr(db_module, "PHASE3_SCHEMA", "CREATE TABLE temp_phase3(id TEXT); SELECT invalid_sql;")
    with pytest.raises(sqlite3.OperationalError):
        Database(db.root, db.config_root)
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='temp_phase3'").fetchone() is None
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""
    assert len(list(db.root.glob("pmt-schema3-*.sqlite3"))) == 1


def test_object_cas_replay_ownership_and_transaction_connection(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    store = Phase3Storage(db)
    rid = new_id()
    first = store.put_object("index", rid, "scope", "actor", "session", "source", 0,
                             {"items": [1]}, request_id=new_id())
    assert first["revision"] == 1 and first["replayed"] is False
    req = new_id()
    replay1 = store.put_object("index", rid, "scope", "actor", "session", "source", 1,
                               {"items": [2]}, request_id=req)
    replay2 = store.put_object("index", rid, "scope", "actor", "session", "source", 1,
                               {"items": [2]}, request_id=req)
    assert replay1["revision"] == replay2["revision"] == 2
    assert replay1["replayed"] is False and replay2["replayed"] is True
    assert store.get_object("index", rid, "scope", "other", "session") is None
    with pytest.raises(PmtError, match="revision"):
        store.put_object("index", rid, "scope", "actor", "session", "source", 1, {"items": [3]})
    with db.write() as conn:
        store.put_object("index", rid, "scope", "actor", "session", "source", 2,
                         {"items": [3]}, conn=conn)
        assert conn.in_transaction
    assert store.get_object("index", rid, "scope", "actor", "session")["revision"] == 3


def test_two_process_cas_has_one_winner(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    store = Phase3Storage(db)
    object_id = new_id()
    store.put_object("index", object_id, "scope", "actor", "session", "source", 0, {})
    code = """from pmt.db import Database
from pmt.efficiency.storage import Phase3Storage
import sys,uuid
d=Database(sys.argv[1],sys.argv[2]); s=Phase3Storage(d)
try:
 s.put_object('index',sys.argv[3],'scope','actor','session','source',1,{'worker':sys.argv[4]},request_id=str(uuid.uuid4())); print('won')
except Exception as e: print(getattr(e,'code','error'))
"""
    root = Path(__file__).resolve().parents[1]
    env = {**__import__("os").environ, "PYTHONPATH": str(root / "src")}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(db.root), str(db.config_root), object_id, str(i)],
                              cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for i in range(2)]
    results = [p.communicate(timeout=20) for p in procs]
    assert all(p.returncode == 0 for p in procs)
    outputs = [stdout.strip() for stdout, _ in results]
    assert outputs.count("won") == 1 and outputs.count("revision_conflict") == 1, results
    assert store.get_object("index", object_id, "scope", "actor", "session")["revision"] == 2


def test_local_store_port_parity_and_compatibility(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    store = LocalStore(db)
    request = {"protocol_version": 1, "operation": "setup", "request_id": new_id(),
               "actor": "actor", "session_id": "session", "payload": {"product": "cli"}}
    response, code = store.execute(request)
    assert response["ok"] and code == 0
    assert store.get_request_result(request["request_id"], "actor", "session") == (response, code)
    assert store.get_request_result(request["request_id"], "other", "session") is None
    assert store.check_compatibility()["db_schema"] == db_module.SCHEMA_VERSION


def test_journal_stage_cas_and_outbox_owner_controls(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    storage = Phase3Storage(db)
    intent = storage.append_intent("file.publish", new_id(), "scope", "actor", "session",
                                   {"stage": "prepared", "source_hash": "h"})
    assert intent["stage"] == "prepared"
    with db.write() as conn:
        updated = storage.update_intent(intent["id"], "prepared", "published", "scope", "actor", "session",
                                        {"receipt": "r"}, conn=conn)
        assert updated["stage"] == "published" and conn.in_transaction
    assert storage.get_intent(intent["id"], "scope", "actor", "session")["body"]["stage"] == "published"
    item = storage.enqueue_outbox("sync", new_id(), "scope", "actor", "session", {"ref": "safe"})
    storage.enqueue_outbox("sync", new_id(), "another-scope", "other", "session", {"ref": "private"})
    claimed = storage.claim_outbox("worker-a", "scope", "actor", "session")
    assert [row["id"] for row in claimed] == [item["id"]]
    assert storage.claim_outbox("worker-b", "scope", "actor", "session") == []
    with pytest.raises(PmtError) as denied:
        storage.complete_outbox(item["id"], "worker-b", {"done": True})
    assert denied.value.code == "outbox_owner_conflict"
    storage.reclaim_outbox(item["id"], "worker-a")
    assert storage.claim_outbox("worker-b", "scope", "actor", "session")[0]["owner"] == "worker-b"
    assert storage.complete_outbox(item["id"], "worker-b", {"done": True})["state"] == "done"


def test_source_pin_distinguishes_unknown_and_clean_and_checks_hash():
    pin = SourcePin("repo", "project", "main", "deadbeef", 1, 2, "graph-hash", "unknown")
    assert pin_source(pin.to_dict()).source_hash == pin.source_hash
    clean = SourcePin("repo", "project", "main", "deadbeef", 1, 2, "graph-hash", "clean")
    with pytest.raises(PmtError) as conflict:
        verify_source_pin(pin, clean)
    assert conflict.value.code == "source_conflict"
