import concurrent.futures
import json
import sqlite3
import threading
import uuid

import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.lifecycle import (claim_task, create_scope, finish_task, record_event, recover_claim,
                           release_claim, save_change, save_decision)


def uid():
    return str(uuid.uuid4())

def req(operation, payload=None, **kw):
    result = {"request_id": uid(), "operation": operation, "actor": "user", "session_id": "sess-a",
              "payload": payload or {}, "context_refs": [], "source": {"product": "test"}}
    result.update(kw)
    return result

def db_at(path, config=None):
    return Database(path / "data", (config or path / "config"))

def call(db, request, fn):
    response, code = db.run_request(request, lambda conn, value: fn(db, conn, value))
    return (response["result"] if response["ok"] else response), code

def setup_item(db, criteria=("criterion-a",)):
    scope_request = req("create_scope", {"kind": "project", "slug": "root"})
    scope, code = call(db, scope_request, create_scope)
    assert code == 0
    create = req("save_change", {"kind": "item", "title": "Implement", "reason": "start"}, scope_id=scope["id"])
    create["payload"]["body"] = {"criteria": list(criteria), "workspace": ".", "blocked_by": []}
    record, code = call(db, create, save_change)
    assert code == 0
    return scope, record

def test_scope_record_event_and_idempotent_replay(tmp_path):
    db = db_at(tmp_path)
    scope, record = setup_item(db)
    assert scope["scope_id"] == scope["id"]
    assert record["record_id"] == record["id"] and record["revision"] == 1
    event_id = uid()
    event_req = req("record_event", normalized_event={"event_id": event_id, "type": "session_stop",
                                                       "source": {"product": "fixture"}, "meta": {"turn_id": "t"}})
    first, code = call(db, event_req, record_event)
    assert code == 0 and not first["duplicate"]
    replay, code = call(db, event_req, record_event)
    assert code == 0 and replay == first
    duplicate_request = req("record_event", normalized_event=event_req["normalized_event"])
    duplicate, code = call(db, duplicate_request, record_event)
    assert code == 0 and duplicate["duplicate"]
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (record["id"],)).fetchone()[0] == "Planned"
        stored = json.loads(conn.execute("SELECT payload_json FROM events WHERE event_id=?", (event_id,)).fetchone()[0])
        assert set(stored) == {"source", "meta"}

def test_scope_relationship_remote_normalization_and_no_merge(tmp_path):
    db = db_at(tmp_path)
    env, code = call(db, req("create_scope", {"kind": "environment", "slug": "machine"}), create_scope)
    assert code == 0
    first, code = call(db, req("create_scope", {"kind": "repository", "slug": "repo",
                                                "body": {"remote": "https://user:token@GitHub.com/org/repo.git/"}},
                              parent_id=env["id"]), create_scope)
    assert code == 0 and first["body"]["remote"] == "https://github.com/org/repo"
    second, code = call(db, req("create_scope", {"kind": "repository", "slug": "fork",
                                                 "body": {"remote": "https://github.com/org/repo"}},
                               parent_id=env["id"]), create_scope)
    assert code == 0 and second["id"] != first["id"]
    invalid, code = call(db, req("create_scope", {"kind": "classification", "slug": "wrong-parent"},
                                 parent_id=first["id"]), create_scope)
    assert code == 2

def test_claim_release_recover_and_stale_owner(tmp_path):
    db = db_at(tmp_path)
    _, item = setup_item(db)
    take = req("claim_task", expected_revision=item["revision"], record_id=item["id"])
    owned, code = call(db, take, claim_task)
    assert code == 0 and owned["state"] == "In Progress"
    conflict, code = call(db, {**req("claim_task", expected_revision=owned["revision"], record_id=item["id"]),
                               "request_id": uid(), "session_id": "sess-b"}, claim_task)
    assert code == 3
    wrong = req("release_claim", {"claim_token": uid(), "status": "Paused", "reason": "pause", "resume": "resume"},
                record_id=item["id"], expected_revision=owned["revision"])
    wrong["session_id"] = "sess-b"
    rejected, code = call(db, wrong, release_claim)
    assert code == 3 and rejected["error"]["code"] == "claim_conflict"
    release = req("release_claim", {"claim_token": owned["claim_token"], "status": "Paused",
                                    "reason": "waiting", "resume": "continue implementation"},
                  record_id=item["id"], expected_revision=owned["revision"])
    paused, code = call(db, release, release_claim)
    assert code == 0 and paused["state"] == "Paused" and paused["revision"] == 3
    take2 = req("claim_task", expected_revision=paused["revision"], record_id=item["id"])
    owned2, code = call(db, take2, claim_task)
    assert code == 0
    recovery = req("recover_claim", {"reason": "owner isolated", "terminated_or_isolated": {"workspace": "isolated"}},
                   record_id=item["id"], expected_revision=owned2["revision"])
    recovery["actor"] = "main"
    recovery["session_id"] = "main"
    recovered, code = call(db, recovery, recover_claim)
    assert code == 0 and recovered["revision"] == owned2["revision"] + 1
    stale = req("release_claim", {"claim_token": owned2["claim_token"], "status": "Paused",
                                  "reason": "old owner", "resume": "resume"},
                record_id=item["id"], expected_revision=recovered["revision"])
    stale["session_id"] = "sess-a"
    rejected, code = call(db, stale, release_claim)
    assert code == 3
    with db.connect() as conn:
        event = conn.execute("SELECT claim_event_id FROM claims WHERE record_id=?", (item["id"],)).fetchone()[0]
        assert conn.execute("SELECT 1 FROM events WHERE id=?", (event,)).fetchone()

def test_competing_claims_commit_once(tmp_path):
    db = db_at(tmp_path)
    _, item = setup_item(db)
    gate = threading.Barrier(2)
    def attempt(session):
        request = req("claim_task", expected_revision=item["revision"], record_id=item["id"])
        request["session_id"] = session
        gate.wait()
        return call(db, request, claim_task)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("session-one", "session-two")))
    assert sorted(code for _, code in results) == [0, 3]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM claims WHERE record_id=?", (item["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT revision FROM records WHERE id=?", (item["id"],)).fetchone()[0] == 2

def test_change_revision_decision_and_finish_fails_closed(tmp_path):
    db = db_at(tmp_path)
    _, item = setup_item(db)
    change = req("save_change", {"reason": "clarify", "body": {"criteria": [{"id": "criterion-a"}], "workspace": "."}},
                 record_id=item["id"], expected_revision=1)
    changed, code = call(db, change, save_change)
    assert code == 0 and changed["revision"] == 2 and changed["body"]["criteria"] == ["criterion-a"]
    stale = req("save_change", {"reason": "stale", "title": "ignored"}, record_id=item["id"], expected_revision=1)
    rejected, code = call(db, stale, save_change)
    assert code == 3
    decision = req("save_decision", {"decision_kind": "select", "option_id": "o1", "reason": "user picked",
                                     "decider": "user", "confirmation_source": "explicit_choice"},
                   record_id=item["id"], expected_revision=2)
    decided, code = call(db, decision, save_decision)
    assert code == 0 and decided["revision"] == 3 and decided["decision_id"]
    take = req("claim_task", expected_revision=3, record_id=item["id"])
    owned, code = call(db, take, claim_task)
    assert code == 0
    finish = req("finish_task", {"claim_token": owned["claim_token"], "result": "done", "verification_ids": [uid()]},
                 record_id=item["id"], expected_revision=owned["revision"])
    rejected, code = call(db, finish, finish_task)
    assert code == 2 and rejected["error"]["code"] == "completion_evidence_unavailable"
    repeated, repeat_code = call(db, finish, finish_task)
    assert repeat_code == code and repeated == rejected
    with db.connect() as conn:
        row = conn.execute("SELECT state,revision FROM records WHERE id=?", (item["id"],)).fetchone()
        assert row[0] == "In Progress" and row[1] == owned["revision"]
        assert conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (finish["request_id"],)).fetchone()[0] == 1

def test_profile_concurrent_creation_and_corruption_preservation(tmp_path):
    profile = tmp_path / "config"
    def initialize(_):
        return Database(tmp_path / "data", profile).environment_id
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        ids = list(pool.map(initialize, range(6)))
    assert len(set(ids)) == 1
    broken = tmp_path / "bad-config"
    broken.mkdir()
    config_file = broken / "profile.json"
    config_file.write_text('{broken', encoding="utf-8")
    with pytest.raises(Exception):
        Database(tmp_path / "other-data", broken)
    assert config_file.read_text(encoding="utf-8") == '{broken'

def test_v1_request_owner_migration_and_diagnostic_warning(tmp_path):
    data, config = tmp_path / "data", tmp_path / "config"
    data.mkdir()
    old = sqlite3.connect(data / "pmt.sqlite3")
    old.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
    old.execute("INSERT INTO meta VALUES('schema_version','1')")
    old.execute("CREATE TABLE requests(request_id TEXT PRIMARY KEY,fingerprint_version INTEGER NOT NULL,request_fingerprint TEXT NOT NULL,response_json TEXT NOT NULL,exit_code INTEGER NOT NULL,deterministic INTEGER NOT NULL,created_at TEXT NOT NULL)")
    old.commit()
    old.close()
    db = Database(data, config)
    with db.connect() as conn:
        assert {"actor", "session_id"} <= {r[1] for r in conn.execute("PRAGMA table_info(requests)")}
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "3"
    request = req("diagnostic_test")
    db.diagnostics.emit = lambda *_a, **_kw: False
    response, code = db.run_request(request, lambda *_: {"saved": True})
    assert code == 0 and response["warnings"] == ["diagnostic_log_unavailable"]
    saved, saved_code = db.get_request_result(request["request_id"], actor="user", session_id="sess-a")
    assert saved_code == 0 and saved["warnings"] == ["diagnostic_log_unavailable"]
    assert db.get_request_result(request["request_id"], actor="someone_else", session_id="sess-a") is None

def test_alias_conflict_and_unsupported_schema_preservation(tmp_path):
    db = db_at(tmp_path)
    request = req("save_change", {"record_id": uid(), "expected_revision": 1}, record_id=uid())
    response, code = call(db, request, save_change)
    assert code == 2 and response["error"]["code"] == "input_conflict"
    with db.write() as conn:
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(PmtError, match="newer"):
        Database(tmp_path / "data", tmp_path / "config")
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "99"
