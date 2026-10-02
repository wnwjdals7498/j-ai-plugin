"""Failure-boundary acceptance tests using actual SQLite files and child processes."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid

import pytest

from pmt.db import Database, SCHEMA_VERSION
from pmt.errors import PmtError
from pmt.lifecycle import (claim_task, create_scope, record_event, recover_claim,
                           release_claim, save_change, save_decision)


def request(operation, payload=None, **fields):
    value = {"protocol_version": 1, "operation": operation, "request_id": str(uuid.uuid4()),
             "actor": "main", "session_id": "test-main", "payload": payload or {},
             "context_refs": [], "source": {"product": "test"}}
    value.update(fields)
    return value


def invoke(db, req, handler):
    response, code = db.run_request(req, lambda conn, normalized: handler(db, conn, normalized))
    return response, code


def project(db):
    response, code = invoke(db, request("create_scope", {"kind": "project", "slug": "acceptance"}), create_scope)
    assert code == 0, response
    return response["result"]["id"]


def item(db, scope_id, *, criteria=("C1",)):
    req = request("save_change", {"kind": "item", "title": "Acceptance item", "reason": "fixture",
                                   "body": {"criteria": list(criteria), "workspace": "."}}, scope_id=scope_id)
    response, code = invoke(db, req, save_change)
    assert code == 0, response
    return response["result"]


def database(roots):
    data, config = roots
    return Database(data, config, busy_timeout_ms=500)


def test_data_02_v1_migrates_and_future_schema_is_read_guarded(roots):
    db = database(roots)
    with db.connect() as conn:
        db_id = db._meta_value(conn, "db_id")
    old_req = request("archive-proof")
    response, code = db.run_request(old_req, lambda _conn, _req: {"saved": "before migration"})
    assert code == 0
    with sqlite3.connect(db.path) as conn:
        conn.execute("ALTER TABLE requests DROP COLUMN actor")
        conn.execute("ALTER TABLE requests DROP COLUMN session_id")
        conn.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    migrated = Database(db.root, db.config_root, busy_timeout_ms=500)
    with migrated.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(SCHEMA_VERSION)
        assert conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()[0] == db_id
        assert conn.execute("SELECT response_json FROM requests WHERE request_id=?", (old_req["request_id"],)).fetchone()[0] == json.dumps(response, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert {row[1] for row in conn.execute("PRAGMA table_info(requests)")} >= {"actor", "session_id"}
    with migrated.write() as conn:
        conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
    with pytest.raises(PmtError) as raised:
        Database(db.root, db.config_root, busy_timeout_ms=500)
    # Unsupported versions are a permanent compatibility refusal, not a retryable busy condition.
    assert raised.value.exit_code == 2 and raised.value.retryable is False
    with sqlite3.connect(db.path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "99"
        assert conn.execute("SELECT response_json FROM requests WHERE request_id=?", (old_req["request_id"],)).fetchone()[0]


def test_tx_01_event_ledger_failure_rolls_back_record(roots):
    db = database(roots)
    scope_id = project(db)
    record = item(db, scope_id)
    with db.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_record_event BEFORE INSERT ON events
                        WHEN NEW.event_type='record_changed'
                        BEGIN SELECT RAISE(ABORT, 'injected ledger failure'); END""")
    change = request("save_change", {"title": "must roll back", "reason": "injection"},
                     record_id=record["id"], expected_revision=record["revision"])
    failed, code = invoke(db, change, save_change)
    assert code == 4 and failed["ok"] is False
    assert failed["error"]["code"] == "database_error"
    with db.connect() as conn:
        after = conn.execute("SELECT title,state,revision FROM records WHERE id=?", (record["id"],)).fetchone()
        assert tuple(after) == (record["title"], "Planned", record["revision"])
        assert conn.execute("SELECT count(*) FROM events WHERE record_id=?", (record["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (change["request_id"],)).fetchone()[0] == 0


def test_log_02_diagnostic_failure_keeps_committed_response_and_warning(roots):
    db = database(roots)
    req = request("diagnostic-only-failure")
    db.diagnostics.emit = lambda *_args, **_kwargs: False
    response, code = db.run_request(req, lambda conn, _request: (
        conn.execute("INSERT INTO meta(key,value) VALUES('log02_commit','yes')"), {"saved": True}
    )[1])
    assert code == 0 and response["ok"] and response["warnings"] == ["diagnostic_log_unavailable"]
    replay, replay_code = db.run_request(req, lambda *_: pytest.fail("committed call must replay"))
    assert replay_code == 0 and replay == response
    with db.connect() as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='log02_commit'").fetchone()[0] == "yes"


def test_log_03_request_ledger_failure_rolls_back_record_and_event(roots):
    db = database(roots)
    scope_id = project(db)
    record = item(db, scope_id)
    with db.connect() as conn:
        conn.execute("""CREATE TRIGGER reject_request_ledger BEFORE INSERT ON requests
                        BEGIN SELECT RAISE(ABORT, 'injected request ledger failure'); END""")
    change = request("save_change", {"title": "must roll back", "reason": "request ledger injection"},
                     record_id=record["id"], expected_revision=record["revision"])
    failed, code = invoke(db, change, save_change)
    assert code == 4 and failed["ok"] is False
    assert failed["error"]["code"] == "database_error"
    with db.connect() as conn:
        after = conn.execute("SELECT title,state,revision FROM records WHERE id=?", (record["id"],)).fetchone()
        assert tuple(after) == (record["title"], "Planned", record["revision"])
        assert conn.execute("SELECT count(*) FROM events WHERE record_id=?", (record["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (change["request_id"],)).fetchone()[0] == 0


def test_claim_03_recovery_replaces_owner_and_rejects_old_token(roots):
    db = database(roots)
    scope_id = project(db)
    record = item(db, scope_id)
    claim_req = request("claim_task", record_id=record["id"], expected_revision=record["revision"])
    claimed, code = invoke(db, claim_req, claim_task)
    assert code == 0 and claimed["result"]["claim_token"]
    old_token = claimed["result"]["claim_token"]
    recovery = request("recover_claim", {"reason": "worker isolated", "terminated_or_isolated": {"workspace": "isolated"}},
                      actor="main", session_id="test-main", record_id=record["id"],
                      expected_revision=claimed["result"]["revision"])
    recovered, code = invoke(db, recovery, recover_claim)
    assert code == 0 and recovered["result"]["claim_token"] != old_token
    stale_release = request("release_claim", {"claim_token": old_token, "status": "Paused",
                                              "reason": "stale owner", "resume": "continue"},
                            session_id="test-main", record_id=record["id"],
                            expected_revision=recovered["result"]["revision"])
    rejected, code = invoke(db, stale_release, release_claim)
    assert code == 3 and rejected["error"]["code"] == "claim_conflict"
    with db.connect() as conn:
        claim = conn.execute("SELECT owner_session,token_hash FROM claims WHERE record_id=?", (record["id"],)).fetchone()
        assert claim["owner_session"] == "test-main"
        assert claim["token_hash"] != __import__("hashlib").sha256(old_token.encode()).hexdigest()


def test_event_02_repeated_prompt_ids_and_reverse_stop_never_finish(roots):
    db = database(roots)
    scope_id = project(db)
    record = item(db, scope_id)
    claim_req = request("claim_task", record_id=record["id"], expected_revision=record["revision"])
    claimed, code = invoke(db, claim_req, claim_task)
    assert code == 0
    shared_prompt = "Continue with the same task"
    event_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
    saved = []
    # Two equal prompts represent distinct user actions because each has its own native event ID.
    for event_id in event_ids:
        event_req = request("record_event", normalized_event={"event_id": event_id, "type": "prompt",
                                      "source": {"product": "fixture"}, "meta": {"prompt": shared_prompt}},
                            record_id=record["id"])
        response, code = invoke(db, event_req, record_event)
        assert code == 0
        saved.append(response["result"])
    # A late Stop for the first action arrives after the second one; neither Stop is a completion operation.
    for event_id in reversed(event_ids):
        stop = request("record_event", normalized_event={"event_id": str(uuid.uuid4()), "type": "stop",
                                    "occurred_at": "2026-01-01T00:00:00Z",
                                    "source": {"product": "fixture"}, "meta": {"source_event_id": event_id}},
                       record_id=record["id"])
        response, code = invoke(db, stop, record_event)
        assert code == 0
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_id IN (?,?)", event_ids).fetchone()[0] == 2
        assert conn.execute("SELECT state FROM records WHERE id=?", (record["id"],)).fetchone()[0] == "In Progress"
        assert conn.execute("SELECT count(*) FROM events WHERE record_id=? AND event_type='task_finished'", (record["id"],)).fetchone()[0] == 0


def test_decision_01_explicit_choice_supersession_and_stale_rejection(roots):
    db = database(roots)
    scope_id = project(db)
    record = item(db, scope_id)
    missing_confirmation = request("save_decision", {"decision_kind": "select", "option_id": "A",
                                    "decider": "user", "reason": "picked"}, record_id=record["id"],
                                   expected_revision=record["revision"])
    rejected, code = invoke(db, missing_confirmation, save_decision)
    assert code == 2 and rejected["error"]["code"] == "decision_confirmation_required"
    first = request("save_decision", {"decision_kind": "select", "option_id": "A", "decider": "user",
                         "reason": "explicit pick", "confirmation_source": "user_selected_option"},
                    record_id=record["id"], expected_revision=record["revision"])
    saved, code = invoke(db, first, save_decision)
    assert code == 0
    first_id = saved["result"]["decision_id"]
    stale = request("save_decision", {"decision_kind": "custom", "content": "stale", "decider": "user",
                         "reason": "stale", "confirmation_source": "direct"},
                    record_id=record["id"], expected_revision=record["revision"])
    rejected, code = invoke(db, stale, save_decision)
    assert code == 3
    second = request("save_decision", {"decision_kind": "custom", "content": "Replacement", "decider": "user",
                           "reason": "replace after review", "confirmation_source": "direct", "supersedes": first_id},
                     record_id=record["id"], expected_revision=saved["result"]["revision"])
    replacement, code = invoke(db, second, save_decision)
    assert code == 0
    with db.connect() as conn:
        old = conn.execute("SELECT state,body_json FROM records WHERE id=?", (first_id,)).fetchone()
        assert old["state"] == "Superseded" and json.loads(old["body_json"])["superseded_by"] == replacement["result"]["decision_id"]
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='decision_saved'").fetchone()[0] == 2


@pytest.mark.parametrize("phase,expected_revision,expected_title", [
    ("before_commit", 1, "Acceptance item"),
    ("after_commit", 2, "after child commit"),
])
def test_recover_01_child_exit_boundary_and_request_replay(roots, phase, expected_revision, expected_title):
    db = database(roots)
    scope_id = project(db)
    record = item(db, scope_id)
    req = request("save_change", {"title": "after child commit", "reason": "crash recovery"},
                  record_id=record["id"], expected_revision=record["revision"])
    child_code = r'''import json, os
from pmt.db import Database
from pmt.lifecycle import save_change
db=Database(os.environ["PMT_TEST_DATA"], os.environ["PMT_TEST_CONFIG"], busy_timeout_ms=500)
request=json.loads(os.environ["PMT_TEST_REQUEST"])
def handler(conn, value):
    result=save_change(db, conn, value)
    if os.environ["PMT_TEST_PHASE"] == "before_commit":
        os._exit(71)
    return result
db.run_request(request, handler)
os._exit(72)
'''
    env = os.environ.copy()
    checkout = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    env["PYTHONPATH"] = os.path.join(checkout, "src") + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env["PMT_TEST_DATA"] = str(db.root)
    env["PMT_TEST_CONFIG"] = str(db.config_root)
    env["PMT_TEST_REQUEST"] = json.dumps(req, ensure_ascii=False, separators=(",", ":"))
    env["PMT_TEST_PHASE"] = phase
    result = subprocess.run([sys.executable, "-c", child_code], env=env, capture_output=True, text=True,
                            timeout=20, check=False)
    expected_exit = 71 if phase == "before_commit" else 72
    assert result.returncode == expected_exit, result.stderr
    with db.connect() as conn:
        row = conn.execute("SELECT title,revision FROM records WHERE id=?", (record["id"],)).fetchone()
        assert (row["title"], row["revision"]) == (expected_title, expected_revision)
        request_row = conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (req["request_id"],)).fetchone()[0]
        assert request_row == (0 if phase == "before_commit" else 1)
    if phase == "after_commit":
        replay, code = db.run_request(req, lambda *_: pytest.fail("same request ID must replay stored result"))
        assert code == 0 and replay["result"]["title"] == expected_title
