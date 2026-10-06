"""P4-R0-01/02/03: real storage, migration, ownership, replay and process CAS."""
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from pmt.db import Database
from pmt.continuity.contracts import authorize, bounded_result, digest, validate_metadata
from pmt.continuity.storage import ContinuityStore
from pmt.errors import PmtError
from pmt.service import execute
from pmt.util import new_id


def request(scope, **overrides):
    return {"request_id": new_id(), "protocol_version": 1, "operation": "continuity.test",
            "actor": "main", "session_id": "first", "scope_id": scope, "payload": {}, **overrides}


def setup(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    wire = {"request_id": new_id(), "protocol_version": 1, "operation": "create_scope",
            "actor": "main", "session_id": "first", "payload": {"kind": "project", "slug": "test"}}
    response, code = execute(db, wire)
    assert code == 0
    return db, response["result"]["scope_id"]


def test_immutable_shared_metadata_event_distinction_and_private_owner(tmp_path):
    db, scope = setup(tmp_path)
    store = ContinuityStore(db)
    req = request(scope)
    event_id = new_id()
    with db.write() as conn:
        first = store.put(conn, req, "checkpoint", {"goal_ref": "goal"}, event_id=event_id)
        second = store.put(conn, request(scope), "checkpoint", {"goal_ref": "goal"}, event_id=event_id)
        independent = store.put(conn, request(scope), "checkpoint", {"goal_ref": "goal"}, event_id=new_id())
        assert first["id"] == second["id"] != independent["id"]
        other_session = req | {"session_id": "second"}
        assert store.get(conn, other_session, first["id"])["body"] == {"goal_ref": "goal"}
        private = store.put(conn, req, "bundle", {"source_ref": "pmt:source"}, visibility="private")
        with pytest.raises(PmtError, match="another actor or session"):
            store.get(conn, other_session, private["id"])
        with pytest.raises(PmtError, match="different metadata"):
            store.put(conn, req, "checkpoint", {"goal_ref": "other"}, event_id=event_id)
        with pytest.raises(PmtError, match="different metadata"):
            store.put(conn, req, "checkpoint", {"goal_ref": "other"}, object_id=first["id"])
        with pytest.raises(PmtError, match="different basis"):
            store.put(conn, req, "checkpoint", {"goal_ref": "goal"}, event_id=event_id, basis_hash=digest({"new": True}))
        new_basis = store.put(conn, req, "facts", {"same": True}, basis_hash=digest({"basis": 1}))
        next_basis = store.put(conn, req, "facts", {"same": True}, basis_hash=digest({"basis": 2}))
        assert new_basis["id"] != next_basis["id"]


def test_pointer_cas_is_atomic_with_request_replay_and_current_authority(tmp_path):
    db, scope = setup(tmp_path)
    store = ContinuityStore(db)
    req = request(scope)
    selector = {"purpose": "confirmed", "branch": "main", "workspace_ref": "pmt:checkout", "task_id": None}
    allowed = [True]
    def current_authority(conn, wire):
        authorize(db, conn, wire)
        if not allowed[0]:
            raise PmtError("scope_denied", "Current scope grant was revoked", 3)
    def handler(conn, wire):
        obj = store.put(conn, wire, "checkpoint", {"basis_ref": "basis"})
        return store.advance_pointer(conn, wire, selector, obj["id"], 0)
    first, code = db.run_request(req, handler, authorize=current_authority)
    assert code == 0 and first["result"]["revision"] == 1
    assert db.run_request(req, handler, authorize=current_authority) == (first, 0)
    allowed[0] = False
    replay, code = db.run_request(req, handler, authorize=current_authority)
    assert code == 3 and replay["error"]["code"] == "scope_denied"
    with db.connect() as conn:
        pointer = store.read_pointer(conn, req, selector)
        assert pointer["revision"] == 1
        assert store.read_pointer(conn, req, {key: value for key, value in selector.items() if value is not None})["revision"] == 1
        assert store.read_pointer(conn, req, selector | {"branch": "other"})["revision"] == 0
    allowed[0] = True
    rejected, code = db.run_request(request(scope), handler, authorize=current_authority)
    assert code == 3 and rejected["error"]["code"] == "revision_conflict"


def test_scope_ancestry_hash_integrity_private_fields_and_effect_recovery(tmp_path):
    db, scope = setup(tmp_path)
    store = ContinuityStore(db)
    req = request(scope)
    with db.write() as conn:
        obj = store.put(conn, req, "facts", {"source_ref": "pmt:source"})
        with pytest.raises(PmtError):
            store.get(conn, request(new_id()), obj["id"])
        conn.execute("UPDATE continuity_objects SET body_json='{}' WHERE id=?", (obj["id"],))
        with pytest.raises(PmtError, match="hash changed"):
            store.get(conn, req, obj["id"])
        journal = store.begin_effect(conn, req, "alignment", {"target_ref": "graph"})
        assert store.begin_effect(conn, req, "alignment", {"target_ref": "graph"})["id"] == journal["id"]
        partial = store.update_effect(conn, req, journal["id"], "partial", {"completed_refs": ["doc"]})
        assert partial["state"] == "partial"
        with pytest.raises(PmtError):
            store.get_effect(conn, req | {"session_id": "foreign"}, journal["id"])
        final = store.update_effect(conn, req, journal["id"], "completed", {"receipt_ref": "actual"})
        assert store.update_effect(conn, req, journal["id"], "completed", {"receipt_ref": "actual"}) == final
        with pytest.raises(PmtError):
            store.update_effect(conn, req, journal["id"], "applying")
    for body in ({"prompt": "synthetic-secret"}, {"source": {"path": "C:/private"}}, {"argv": []}):
        with pytest.raises(PmtError):
            validate_metadata(body)


def schema4(tmp_path):
    db, scope = setup(tmp_path)
    with db.connect() as conn:
        for table in ("continuity_events", "continuity_pointers", "continuity_journal", "continuity_objects"):
            conn.execute(f"DROP TABLE {table}")
        conn.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    return db, scope


def test_schema4_additive_migration_backup_and_ids(tmp_path):
    old, scope = schema4(tmp_path)
    upgraded = Database(old.root, old.config_root)
    with upgraded.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"
        assert conn.execute("SELECT id FROM scopes WHERE id=?", (scope,)).fetchone()[0] == scope
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    backups = list(upgraded.root.glob("pmt-schema4-*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
        assert conn.execute("SELECT id FROM scopes WHERE id=?", (scope,)).fetchone()[0] == scope
    Database(upgraded.root, upgraded.config_root)
    assert len(list(upgraded.root.glob("pmt-schema4-*.sqlite3"))) == 1


def test_schema5_migration_failure_keeps_old_source_and_backup(tmp_path, monkeypatch):
    import pmt.db as module
    db, scope = schema4(tmp_path)
    monkeypatch.setattr(module, "PHASE4_SCHEMA", "CREATE TABLE accidental(id TEXT); SELECT invalid_sql;")
    with pytest.raises(sqlite3.OperationalError):
        Database(db.root, db.config_root)
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
        assert conn.execute("SELECT value FROM meta WHERE key='maintenance_owner'").fetchone()[0] == ""
        assert conn.execute("SELECT id FROM scopes WHERE id=?", (scope,)).fetchone()[0] == scope
        assert not conn.execute("SELECT name FROM sqlite_master WHERE name='accidental'").fetchone()
    assert len(list(db.root.glob("pmt-schema4-*.sqlite3"))) == 1


def test_actual_separate_process_pointer_competition(tmp_path):
    db, scope = setup(tmp_path)
    gate = tmp_path / "go"
    script = r'''
import sys,time
from pathlib import Path
from pmt.db import Database
from pmt.continuity.storage import ContinuityStore
from pmt.errors import PmtError
from pmt.util import new_id
db=Database(sys.argv[1],sys.argv[2]);store=ContinuityStore(db)
req={'request_id':new_id(),'operation':'continuity.test','scope_id':sys.argv[3],'actor':'main','session_id':sys.argv[4],'payload':{}}
while not Path(sys.argv[5]).exists(): time.sleep(.01)
try:
 with db.write() as conn:
  obj=store.put(conn,req,'checkpoint',{'actor_ref':sys.argv[4]})
  store.advance_pointer(conn,req,{'purpose':'race','branch':'main'},obj['id'],0)
 print('committed')
except PmtError as error: print(error.code)
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    processes = [subprocess.Popen([sys.executable, "-c", script, str(db.root), str(db.config_root), scope,
                                  owner, str(gate)], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, encoding="utf-8") for owner in ("a", "b")]
    gate.touch()
    outputs = [process.communicate(timeout=20) for process in processes]
    assert all(process.returncode == 0 for process in processes), outputs
    assert sorted(output[0].strip() for output in outputs) == ["committed", "revision_conflict"]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM continuity_pointers").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM continuity_objects").fetchone()[0] == 1


def test_bounded_mandatory_overflow_is_incomplete(tmp_path):
    result = bounded_result(request(new_id(), payload={"budget": {"max_bytes": 256, "max_lines": 8}}),
                            {"mandatory": "x" * 2000})
    assert result["complete"] is False
    assert result["attention"] == "mandatory_budget_exceeded"
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})


def test_corrupt_pointer_and_journal_return_integrity_error(tmp_path):
    db, scope = setup(tmp_path)
    req = request(scope)
    store = ContinuityStore(db)
    with db.write() as conn:
        obj = store.put(conn, req, "checkpoint", {"confirmed": True})
        store.advance_pointer(conn, req, {"purpose": "current"}, obj["id"], 0)
        effect = store.begin_effect(conn, req, "capture", {"basis_ref": obj["id"]})
        conn.execute("UPDATE continuity_pointers SET selector_json='broken'")
        conn.execute("UPDATE continuity_journal SET outcome_json='broken'")
    with db.connect() as conn:
        with pytest.raises(PmtError) as pointer_error:
            store.read_pointer(conn, req, {"purpose": "current"})
        assert pointer_error.value.code == "continuity_corrupt"
        with pytest.raises(PmtError) as journal_error:
            store.get_effect(conn, req, effect["id"])
        assert journal_error.value.code == "continuity_corrupt"
        assert conn.execute("SELECT revision FROM continuity_pointers").fetchone()[0] == 1


def test_actual_sqlite_readonly_failure_logs_code_and_keeps_request_uncommitted(tmp_path, capsys):
    db, scope = setup(tmp_path)
    req = request(scope)
    capsys.readouterr()

    def fail_readonly(conn, _request):
        conn.execute("PRAGMA query_only=ON")
        conn.execute("UPDATE meta SET value='synthetic-private-value' WHERE key='db_id'")

    with db.connect() as conn:
        original = conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()[0]
    result, code = db.run_request(req, fail_readonly)
    assert code == 4 and result["error"]["code"] == "database_error"
    assert result["error"]["details"]["sqlite_errorcode"] == sqlite3.SQLITE_READONLY
    emitted = capsys.readouterr().err
    rows = [json.loads(line) for line in emitted.splitlines()]
    failure = next(row for row in rows if row["event_name"] == "database_write_failed")
    assert failure["sqlite_errorcode"] == sqlite3.SQLITE_READONLY
    assert failure["sqlite_errorname"] == "SQLITE_READONLY"
    assert failure["transaction_outcome"] == "rollback"
    assert "synthetic-private-value" not in emitted
    with db.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()[0] == original
        assert conn.execute("SELECT 1 FROM requests WHERE request_id=?", (req["request_id"],)).fetchone() is None
