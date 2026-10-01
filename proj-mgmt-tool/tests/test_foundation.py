import json
import sqlite3
import uuid

import pytest

from pmt.db import Database, SCHEMA_VERSION
from pmt.errors import PmtError
from pmt.util import canonical_json, fingerprint, strict_json_loads


def make_db(tmp_path):
    return Database(tmp_path / "data", tmp_path / "config")


def test_schema_initializes_and_ids_persist(tmp_path):
    db = make_db(tmp_path)
    again = make_db(tmp_path)
    with db.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        first = conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()[0]
        assert conn.execute("SELECT value FROM meta WHERE key='environment_id'").fetchone()[0] == db.environment_id
    assert again.environment_id == db.environment_id
    with again.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()[0] == first
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"meta", "scopes", "records", "events", "requests", "claims", "artifacts",
            "artifact_refs", "verifications", "file_jobs"} <= names


def test_run_request_replay_conflict_and_atomic_error(tmp_path):
    db = make_db(tmp_path)
    req = {"request_id": str(uuid.uuid4()), "operation": "write", "payload": {"x": 1}}
    def handler(conn, request):
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('s','project','s','t','t')")
        return {"id": "s"}
    response, code = db.run_request(req, handler)
    assert code == 0 and response["result"] == {"id": "s"}
    replay, replay_code = db.run_request(req, lambda *_: pytest.fail("replay called handler"))
    assert replay == response and replay_code == 0
    changed = {**req, "payload": {"x": 2}}
    conflict, code = db.run_request(changed, handler)
    assert code == 3 and conflict["error"]["code"] == "request_conflict"

    bad = {"request_id": str(uuid.uuid4()), "operation": "bad"}
    def fail(conn, request):
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES('s2','project','s2','t','t')")
        raise PmtError("invalid_state", "no", 2)
    error, code = db.run_request(bad, fail)
    assert code == 2 and error["error"]["code"] == "invalid_state"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scopes WHERE id='s2'").fetchone()[0] == 0
    assert db.get_request_result(bad["request_id"]) == (error, 2)


def test_maintenance_foreign_keys_and_strict_json(tmp_path):
    db = make_db(tmp_path)
    with db.write() as conn:
        conn.execute("UPDATE meta SET value='backup' WHERE key='maintenance_owner'")
    with pytest.raises(PmtError, match="maintenance"):
        with db.write():
            pass
    with db.write(maintenance_owner="backup") as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO records(id,kind,scope_id,title,created_at,updated_at) VALUES('x','item','missing','x','t','t')")
    assert strict_json_loads('{"a":1}') == {"a": 1}
    with pytest.raises(PmtError):
        strict_json_loads('{"a":1,"a":2}')
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})
    assert fingerprint({"x": 1}) == fingerprint({"x": 1})
