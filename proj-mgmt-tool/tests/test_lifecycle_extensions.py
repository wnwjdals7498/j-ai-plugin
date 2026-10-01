"""Regression tests for approved task-state, criterion, and event-source contracts."""
from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
import uuid

import pytest

from pmt.db import Database
from pmt.diagnostics import DiagnosticLogger, JsonFormatter, ObservableStreamHandler
from pmt.errors import PmtError
from pmt.lifecycle import handle


def _id():
    return str(uuid.uuid4())


def _request(operation, payload=None, **overrides):
    request = {"protocol_version": 1, "operation": operation, "request_id": _id(),
               "actor": "user", "session_id": "session-a", "payload": payload or {},
               "context_refs": [], "source": {"product": "test"}}
    request.update(overrides)
    return request


def _call(db, request):
    return db.run_request(request, lambda conn, value: handle(db, conn, value))


def _scope(db):
    response, code = _call(db, _request("create_scope", {"kind": "project", "slug": "test"}))
    assert code == 0, response
    return response["result"]["scope_id"]


def _record(db, scope_id, *, kind="item", parent_id=None, body=None):
    payload = {"kind": kind, "title": f"{kind} record", "reason": "fixture", "body": body or {}}
    if parent_id:
        payload["parent_id"] = parent_id
    response, code = _call(db, _request("save_change", payload, scope_id=scope_id))
    assert code == 0, response
    return response["result"]


def _claim(db, record, *, session="session-a"):
    request = _request("claim_task", record_id=record["id"], expected_revision=record["revision"], session_id=session)
    response, code = _call(db, request)
    assert code == 0, response
    return response["result"]


def _change(db, record, payload, *, session="session-a", request_id=None):
    request = _request("save_change", payload, record_id=record["id"], expected_revision=record["revision"],
                       session_id=session)
    if request_id:
        request["request_id"] = request_id
    return _call(db, request)


def _set_state(db, record, state, revision=None):
    with db.write() as conn:
        conn.execute("UPDATE records SET state=?,revision=? WHERE id=?",
                     (state, revision if revision is not None else record["revision"], record["id"]))


def test_blocked_task_can_be_explicitly_unblocked_to_planned(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    scope_id = _scope(db)
    item = _record(db, scope_id)
    owned = _claim(db, item)
    release = _request("release_claim", {
        "claim_token": owned["claim_token"], "status": "Blocked", "reason": "waiting on input",
        "next_action": "resume after input"}, record_id=item["id"], expected_revision=owned["revision"])
    blocked, code = _call(db, release)
    assert code == 0 and blocked["result"]["state"] == "Blocked"
    planned, code = _change(db, blocked["result"], {"status": "Planned", "reason": "dependency is resolved"})
    assert code == 0 and planned["result"]["state"] == "Planned"
    assert planned["result"]["revision"] == blocked["result"]["revision"] + 1
    with db.connect() as conn:
        event = conn.execute("SELECT event_type,reason FROM events WHERE record_id=? ORDER BY recorded_at DESC LIMIT 1",
                             (item["id"],)).fetchone()
    assert tuple(event) == ("record_changed", "dependency is resolved")


def test_only_blocked_tasks_can_transition_to_planned_and_reason_is_required(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    item = _record(db, _scope(db))
    paused_claim = _claim(db, item)
    release = _request("release_claim", {"claim_token": paused_claim["claim_token"], "status": "Paused",
                                           "reason": "pause", "resume": "continue later"},
                       record_id=item["id"], expected_revision=paused_claim["revision"])
    paused, code = _call(db, release)
    assert code == 0 and paused["result"]["state"] == "Paused"
    invalid, code = _change(db, paused["result"], {"status": "Planned", "reason": "unblock"})
    assert code == 2 and invalid["error"]["code"] == "invalid_transition"
    blocked = {**paused["result"], "state": "Blocked"}
    _set_state(db, blocked, "Blocked")
    missing_reason, code = _change(db, blocked, {"status": "Planned"})
    assert code == 2 and missing_reason["error"]["code"] == "missing_reason"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (item["id"],)).fetchone()[0] == "Blocked"


def test_canceled_transition_requires_explicit_stopped_owned_claim(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    item = _record(db, _scope(db))
    owned = _claim(db, item)
    absent_proof, code = _change(db, owned, {"status": "Canceled", "reason": "stop"})
    assert code == 2 and absent_proof["error"]["code"] == "missing_field"
    token_without_stop, code = _change(db, owned, {"status": "Canceled", "reason": "stop",
                                                   "claim_token": owned["claim_token"]})
    assert code == 2 and token_without_stop["error"]["code"] == "stop_confirmation_required"
    wrong_owner, code = _change(db, owned, {"status": "Canceled", "reason": "stop", "stopped": True,
                                            "claim_token": owned["claim_token"]}, session="session-b")
    assert code == 3 and wrong_owner["error"]["code"] == "claim_conflict"
    canceled, code = _change(db, owned, {"status": "Canceled", "reason": "user stopped work",
                                         "stopped": True, "claim_token": owned["claim_token"]})
    assert code == 0 and canceled["result"]["state"] == "Canceled"
    with db.connect() as conn:
        assert conn.execute("SELECT 1 FROM claims WHERE record_id=?", (item["id"],)).fetchone() is None


def test_unclaimed_planned_task_can_be_canceled_but_other_kinds_cannot(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    scope_id = _scope(db)
    item = _record(db, scope_id)
    canceled, code = _change(db, item, {"status": "Canceled", "reason": "no longer needed"})
    assert code == 0 and canceled["result"]["state"] == "Canceled"
    principle = _record(db, scope_id, kind="principle", body={"explanation": "Preserve this principle."})
    rejected, code = _change(db, principle, {"status": "Canceled", "reason": "keep explanation"})
    assert code == 2 and rejected["error"]["code"] == "invalid_transition"
    with db.connect() as conn:
        saved = conn.execute("SELECT state,body_json FROM records WHERE id=?", (principle["id"],)).fetchone()
    assert saved[0] == "Planned" and json.loads(saved[1]) == {"explanation": "Preserve this principle."}


@pytest.mark.parametrize("terminal", ["Done", "Canceled"])
def test_terminal_record_edits_are_rejected(tmp_path, terminal):
    db = Database(tmp_path / "data", tmp_path / "config")
    item = _record(db, _scope(db))
    _set_state(db, item, terminal, revision=7)
    closed = {**item, "revision": 7, "state": terminal}
    response, code = _change(db, closed, {"title": "must stay closed", "reason": "try edit"})
    assert code == 2 and response["error"]["code"] == "record_closed"
    with db.connect() as conn:
        row = conn.execute("SELECT title,state,revision FROM records WHERE id=?", (item["id"],)).fetchone()
    assert tuple(row) == ("item record", terminal, 7)


def test_parent_cannot_be_canceled_while_descendant_is_claimed(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    scope_id = _scope(db)
    parent = _record(db, scope_id, kind="work")
    child = _record(db, scope_id, parent_id=parent["id"])
    owned_child = _claim(db, child)
    rejected, code = _change(db, parent, {"status": "Canceled", "reason": "stop parent"})
    assert code == 3 and rejected["error"]["code"] == "active_children"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (parent["id"],)).fetchone()[0] == "Planned"
        assert conn.execute("SELECT 1 FROM claims WHERE record_id=?", (child["id"],)).fetchone()
    assert owned_child["state"] == "In Progress"


def test_rich_criteria_are_preserved_and_finish_uses_coverage_union(tmp_path, monkeypatch):
    db = Database(tmp_path / "data", tmp_path / "config")
    scope_id = _scope(db)
    criteria = [{"id": "build", "description": "Build completes."},
                {"id": "review", "description": "Review confirms the change."}]
    item = _record(db, scope_id, body={"criteria": criteria, "workspace": str(tmp_path), "blocked_by": []})
    assert item["body"]["criteria"] == criteria
    owned = _claim(db, item)
    verification_ids = [_id(), _id()]
    with db.write() as conn:
        for verification_id in verification_ids:
            conn.execute("INSERT INTO verifications(id,definition_id,definition_version,target_id,environment_id,input_fingerprint,outcome,completed_at) VALUES(?,?,?,?,?,?,?,?)",
                         (verification_id, "definition", "1", item["id"], db.environment_id, "fingerprint", "pass", "t"))
        conn.execute("INSERT INTO artifacts(id,sha256,size_bytes,relative_path,state,created_at) VALUES('evidence',?,0,'resources/objects/evidence','ready','t')",
                     (hashlib.sha256(b"").hexdigest(),))
    (db.root / "resources" / "objects").mkdir(parents=True)
    (db.root / "resources" / "objects" / "evidence").write_bytes(b"")

    def coverage_union(_db, _conn, row, ids):
        assert row["id"] == item["id"] and list(ids) == verification_ids
        return {"valid": True, "covered_criteria": ["build", "review"], "evidence_ids": ["evidence"], "reasons": []}

    monkeypatch.setattr("pmt.verification.verify_completion", coverage_union)
    finish = _request("finish_task", {"claim_token": owned["claim_token"], "result": "Implemented and reviewed.",
                                      "verification_ids": verification_ids},
                      record_id=item["id"], expected_revision=owned["revision"])
    response, code = _call(db, finish)
    assert code == 0 and response["result"]["state"] == "Done"
    assert response["result"]["body"]["criteria"] == criteria
    with db.connect() as conn:
        stored = json.loads(conn.execute("SELECT body_json FROM records WHERE id=?", (item["id"],)).fetchone()[0])
    assert stored["criteria"] == criteria and stored["result"] == "Implemented and reviewed."


def test_item_without_criteria_cannot_finish(tmp_path, monkeypatch):
    db = Database(tmp_path / "data", tmp_path / "config")
    item = _record(db, _scope(db), body={"criteria": [], "workspace": str(tmp_path), "blocked_by": []})
    owned = _claim(db, item)
    verification_id = _id()
    with db.write() as conn:
        conn.execute("INSERT INTO verifications(id,definition_id,definition_version,target_id,environment_id,input_fingerprint,outcome,completed_at) VALUES(?,?,?,?,?,?,?,?)",
                     (verification_id, "definition", "1", item["id"], db.environment_id, "fingerprint", "pass", "t"))
    monkeypatch.setattr("pmt.verification.verify_completion", lambda *_: {
        "valid": True, "covered_criteria": [], "evidence_ids": [], "reasons": []})
    finish = _request("finish_task", {"claim_token": owned["claim_token"], "result": "done",
                                      "verification_ids": [verification_id]},
                      record_id=item["id"], expected_revision=owned["revision"])
    response, code = _call(db, finish)
    assert code == 2 and response["error"]["code"] == "completion_criteria_unmet"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (item["id"],)).fetchone()[0] == "In Progress"


def test_record_event_preserves_allowlisted_source_metadata_and_drops_prompt_secrets(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    event_id = _id()
    normalized = {"event_id": event_id, "type": "tool_call", "source": {
        "product": "codex", "product_version": "1.2", "adapter_version": "3", "instance_id": "install-a",
        "prompt": "private prompt", "token": "secret-token", "other": "not allowed"},
        "source_metadata": {"native_event_name": "post_tool", "turn_id": "turn-a",
                            "source_event_id": "native-17", "tool_name": "pytest", "status": "complete",
                            "source_session_id": "native-session", "prompt": "private prompt",
                            "token": "secret-token", "other": "not allowed"},
        "meta": {"turn_id": "meta-turn", "prompt": "private prompt", "token": "secret-token"}}
    response, code = _call(db, _request("record_event", normalized_event=normalized))
    assert code == 0, response
    event = response["result"]
    assert event["source"] == {"product": "codex", "product_version": "1.2", "adapter_version": "3", "instance_id": "install-a"}
    assert event["meta"] == {"native_event_name": "post_tool", "turn_id": "turn-a",
                             "source_event_id": "native-17", "tool_name": "pytest", "status": "complete",
                             "source_session_id": "native-session"}
    with db.connect() as conn:
        stored = json.loads(conn.execute("SELECT payload_json FROM events WHERE event_id=?", (event_id,)).fetchone()[0])
    assert stored == {"source": event["source"], "meta": event["meta"]}
    assert "secret-token" not in json.dumps(stored) and "private prompt" not in json.dumps(stored)

    legacy_id = _id()
    legacy, code = _call(db, _request("record_event", normalized_event={
        "event_id": legacy_id, "type": "session_end", "source": {"product": "codex"},
        "meta": {"turn_id": "legacy-turn", "prompt": "private prompt", "token": "secret-token"}}))
    assert code == 0
    assert legacy["result"]["meta"] == {"turn_id": "legacy-turn"}
    with db.connect() as conn:
        legacy_stored = json.loads(conn.execute("SELECT payload_json FROM events WHERE event_id=?", (legacy_id,)).fetchone()[0])
    assert legacy_stored == {"source": {"product": "codex"}, "meta": {"turn_id": "legacy-turn"}}


class _FailingStream:
    def __init__(self):
        self.attempts = 0

    def write(self, _value):
        self.attempts += 1
        raise OSError("diagnostic destination is unavailable")

    def flush(self):
        return None


def test_log02_real_stream_failure_keeps_commit_warns_and_replays(tmp_path, capsys):
    stream = _FailingStream()
    logger = logging.Logger(f"pmt-log02-{_id()}", level=logging.INFO)
    logger.propagate = False
    handler = ObservableStreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    diagnostics = DiagnosticLogger(logger)
    db = Database(tmp_path / "data", tmp_path / "config", logger=diagnostics)
    assert db.diagnostics is diagnostics and stream.attempts > 0

    request = _request("diagnostic_commit", {"value": "committed"})

    def commit(conn, _request):
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES(?,?,?,?,?)",
                     ("log02-scope", "project", "log02", "t", "t"))
        return {"scope_id": "log02-scope"}

    response, code = db.run_request(request, commit)
    assert code == 0 and response["ok"]
    assert response["warnings"] == ["diagnostic_log_unavailable"]
    persisted, persisted_code = db.get_request_result(request["request_id"], actor="user", session_id="session-a")
    assert persisted_code == 0 and persisted["warnings"] == ["diagnostic_log_unavailable"]
    replay, replay_code = db.run_request(request, lambda *_: pytest.fail("committed handler must not run on replay"))
    assert replay_code == 0 and replay == response
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scopes WHERE id='log02-scope'").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (request["request_id"],)).fetchone()[0] == 1
    diagnostic = capsys.readouterr().err
    assert '"event_name":"diagnostic_log_unavailable"' in diagnostic
    assert stream.attempts >= 3
