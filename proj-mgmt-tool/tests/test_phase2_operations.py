"""Real storage, retention reservation and diagnostics failure checks."""
import io
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import uuid

import pytest

from pmt.db import Database
from pmt.diagnostics import DiagnosticLogger, JsonFormatter
from pmt.operations import handle
from pmt import operations
from pmt.phase2_common import persist_json_resource
from pmt.resources import _register_handler
from pmt.service import execute
from pmt.util import canonical_json, utc_now
from test_phase2_steps import req, setup, save_request


def test_progress_heartbeat_changes_seen_but_not_actual_progress(tmp_path):
    db, project, _, item = setup(tmp_path)
    step = execute(db, save_request(item, tmp_path))[0]["result"]["step_id"]
    route = {"agent": "codex", "provider": "openai", "model": "test", "mode": "subagent", "max_concurrency": 3,
             "selection_reason": "test", "actual_support": "verified_supported"}
    enqueued, code = execute(db, req("enqueue_execution", {"step_id": step, "route": route}))
    assert code == 0, enqueued
    run_id = enqueued["result"]["run_id"]
    observation = {"stage": "queued", "wait_reason": "dependencies", "next_action": "wait"}
    first, code = execute(db, req("observe_progress", {"run_id": run_id, "observation": observation}))
    again, code = execute(db, req("observe_progress", {"run_id": run_id, "observation": observation}))
    assert code == 0 and again["result"]["last_changed"] == first["result"]["last_changed"]
    assert again["result"]["last_seen"] >= first["result"]["last_seen"]
    read, code = execute(db, req("read_progress", {"run_id": run_id}))
    assert code == 0 and not read["result"]["observation_stale"]
    assert execute(db, req("observe_progress", {"run_id": run_id, "observation": {"payload": "private"}}))[1] != 0


def test_referenced_and_newly_referenced_resources_are_not_deleted(tmp_path):
    db, scope, _, item = setup(tmp_path)
    resource = persist_json_resource(db, req("test"), {"evidence": "must keep"}, scope, "test")
    aid = resource["artifact_id"]
    with db.write() as conn:
        conn.execute("UPDATE artifacts SET retention_until='2000-01-01T00:00:00Z',created_at='2000-01-01T00:00:00Z' WHERE id=?", (aid,))
    candidates, code = execute(db, req("plan_retention"))
    assert aid in candidates["result"]["temporary_artifact_ids"]
    with db.write() as conn:
        conn.execute("INSERT INTO artifact_refs VALUES(?,?,?,?,?)", (aid, "record", item, "evidence", utc_now()))
    rejected, code = execute(db, req("execute_retention", {"artifact_ids": [aid]}))
    assert code == 3 and rejected["error"]["code"] == "retention_protected"
    assert (db.root / resource["relative_path"]).exists()


def test_graph_evidence_reference_is_indexed_for_retention(tmp_path):
    db, scope, _, _ = setup(tmp_path)
    proof = persist_json_resource(db, req("test"), {"proof": "actual fixture evidence"}, scope, "test")
    graph = persist_json_resource(db, req("test"), {"evidence_refs": [proof["artifact_id"]]}, scope, "plan_draft", scope)
    with db.write() as conn:
        conn.execute("UPDATE artifacts SET retention_until='2000-01-01T00:00:00Z',created_at='2000-01-01T00:00:00Z' WHERE id=?", (proof["artifact_id"],))
        row = conn.execute("SELECT owner_id FROM artifact_refs WHERE artifact_id=? AND owner_type='artifact'", (proof["artifact_id"],)).fetchone()
        assert row[0] == graph["artifact_id"]
    candidates, code = execute(db, req("plan_retention"))
    assert code == 0 and proof["artifact_id"] not in candidates["result"]["temporary_artifact_ids"]


def test_reserved_resource_cannot_receive_new_reference(tmp_path):
    db, scope, _, item = setup(tmp_path)
    resource = persist_json_resource(db, req("test"), {"temporary": "value"}, scope, "test")
    aid = resource["artifact_id"]
    with db.write() as conn:
        conn.execute("UPDATE artifacts SET state='deleting' WHERE id=?", (aid,))
    with db.write() as conn, pytest.raises(Exception) as raised:
        _register_handler(conn, req("register_resource"), artifact_id=aid, digest=resource["sha256"],
                          size=resource["size_bytes"], relative=resource["relative_path"], retention="evidence",
                          scope_id=scope, owner_record_id=item)
    assert getattr(raised.value, "code", None) == "resource_not_ready"


def test_expired_unreferenced_cleanup_is_replayable(tmp_path):
    db, scope, _, _ = setup(tmp_path)
    resource = persist_json_resource(db, req("test"), {"temporary": True}, scope, "test")
    aid = resource["artifact_id"]
    with db.write() as conn:
        conn.execute("UPDATE artifacts SET retention_until='2000-01-01T00:00:00Z',created_at='2000-01-01T00:00:00Z' WHERE id=?", (aid,))
    cleanup = req("execute_retention", {"artifact_ids": [aid]})
    result, code = execute(db, cleanup)
    assert code == 0 and result["result"]["removed_artifact_ids"] == [aid]
    assert not (db.root / resource["relative_path"]).exists()
    assert execute(db, cleanup) == (result, code)


def test_log_rotation_is_bounded_and_redacts_tokens(tmp_path):
    db, _, _, _ = setup(tmp_path)
    configured, code = execute(db, req("configure_diagnostics", {"max_bytes": 800, "file_count": 3}))
    assert code == 0
    reopened = Database(db.root, db.config_root)
    for index in range(15):
        reopened.diagnostics.emit("run.observed", run_id="sk-this-is-a-secret-token", outcome=str(index))
    files = list((db.root / "logs").glob("pmt.jsonl*"))
    assert 1 <= len(files) <= 3
    assert all("sk-this-is-a-secret-token" not in f.read_text(encoding="utf-8") for f in files)


def test_diagnostic_failure_does_not_repeat_committed_business(tmp_path):
    db, _, _, _ = setup(tmp_path)
    db.diagnostics.sink_unavailable = True
    request = req("save_routing_policy", {"policy": {"mode": "auto", "economy": True, "role_preferences": {}, "api_allowed": False}})
    result, code = execute(db, request)
    assert code == 0 and "diagnostic_log_unavailable" in result["warnings"]
    assert execute(db, request)[0]["result"] == result["result"]


def test_temp_retention_requires_seven_full_days_by_module_clock(tmp_path, monkeypatch):
    db, scope, _, _ = setup(tmp_path)
    resource = persist_json_resource(db, req("test"), {"temporary": True}, scope, "test")
    aid = resource["artifact_id"]
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(operations, "_now", lambda: now)
    with db.write() as conn:
        conn.execute("UPDATE artifacts SET retention_until='2026-09-20T00:00:00Z',created_at='2026-09-23T00:00:00Z' WHERE id=?", (aid,))
    candidates, code = execute(db, req("plan_retention"))
    assert code == 0 and aid in candidates["result"]["temporary_artifact_ids"]
    with db.write() as conn:
        conn.execute("UPDATE artifacts SET created_at='2026-09-25T00:00:00Z' WHERE id=?", (aid,))
    candidates, code = execute(db, req("plan_retention"))
    assert code == 0 and aid not in candidates["result"]["temporary_artifact_ids"]


def test_three_calendar_month_history_cleanup_keeps_dedup_tombstones(tmp_path, monkeypatch):
    db, _, _, item = setup(tmp_path)
    made, code = execute(db, save_request(item, tmp_path))
    step = made["result"]["step_id"]
    route = {"agent": "codex", "provider": "openai", "model": "test", "mode": "subagent", "max_concurrency": 3,
             "selection_reason": "test", "actual_support": "verified_supported"}
    queued, code = execute(db, req("enqueue_execution", {"step_id": step, "route": route}))
    run_id, job_id = queued["result"]["run_id"], queued["result"]["job_id"]
    old = "2026-06-30T12:00:00.000000Z"
    old_request = str(uuid.uuid4())
    with db.write() as conn:
        conn.execute("UPDATE execution_runs SET state='succeeded',completed_at=?,result_json=?,handle_json=?,intent_json=? WHERE id=?",
                     (old, canonical_json({"private_result": "discard me"}), canonical_json({"handle": "old"}), canonical_json({"raw_intent": "old"}), run_id))
        conn.execute("UPDATE execution_jobs SET state='succeeded' WHERE id=?", (job_id,))
        conn.execute("UPDATE records SET state='Done' WHERE id=?", (step,))
        conn.execute("INSERT INTO run_progress VALUES(?,?,?,?)", (run_id, old, old, canonical_json({"state": "done"})))
        conn.execute("INSERT INTO events(id,event_id,record_id,event_type,payload_json,recorded_at) VALUES(?,?,?,?,?,?)",
                     (str(uuid.uuid4()), str(uuid.uuid4()), step, "execution.transitioned", canonical_json({"run_id": run_id}), old))
        conn.execute("INSERT INTO requests(request_id,fingerprint_version,request_fingerprint,response_json,exit_code,deterministic,created_at,actor,session_id) VALUES(?,1,'old-fp',?,0,1,?,'main','main')",
                     (old_request, canonical_json({"protocol_version": 1, "request_id": old_request, "ok": True, "result": {"large": "old response"}, "error": None, "warnings": []}), old))
    monkeypatch.setattr(operations, "_now", lambda: datetime(2026, 10, 1, tzinfo=timezone.utc))
    result, code = execute(db, req("execute_retention", {"artifact_ids": []}))
    assert code == 0 and result["result"]["runs_tombstoned"] == 1
    assert result["result"]["requests_tombstoned"] >= 1
    with db.connect() as conn:
        run = conn.execute("SELECT id,job_id,attempt,state,result_json,handle_json,intent_json FROM execution_runs WHERE id=?", (run_id,)).fetchone()
        assert tuple(run[:4]) == (run_id, job_id, 1, "succeeded")
        assert run["result_json"] is None and run["handle_json"] is None
        assert json.loads(run["intent_json"]) == {"tombstone": True, "run_id": run_id, "job_id": job_id,
            "step_id": step, "attempt": 1, "directive_version": 1}
        assert conn.execute("SELECT 1 FROM run_progress WHERE run_id=?", (run_id,)).fetchone() is None
        assert conn.execute("SELECT 1 FROM events WHERE event_type='execution.transitioned' AND record_id=?", (step,)).fetchone() is None
        req_result = json.loads(conn.execute("SELECT response_json FROM requests WHERE request_id=?", (old_request,)).fetchone()[0])
        assert req_result["result"]["tombstone"] is True
        assert conn.execute("SELECT 1 FROM requests WHERE request_id=?", (old_request,)).fetchone() is not None


def test_failed_file_delete_is_journaled_and_same_request_resumes(tmp_path, monkeypatch):
    db, scope, _, _ = setup(tmp_path)
    resource = persist_json_resource(db, req("test"), {"temporary": "retry"}, scope, "test")
    aid, target = resource["artifact_id"], db.root / resource["relative_path"]
    with db.write() as conn:
        conn.execute("UPDATE artifacts SET retention_until='2000-01-01T00:00:00Z',created_at='2000-01-01T00:00:00Z' WHERE id=?", (aid,))
    request = req("execute_retention", {"artifact_ids": [aid]})
    original_unlink = Path.unlink
    attempts = {"count": 0}
    def fail_once(path, *args, **kwargs):
        if path == target and attempts["count"] == 0:
            attempts["count"] += 1
            raise OSError("simulated delete failure")
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", fail_once)
    first, code = execute(db, request)
    assert code == 0 and first["result"]["failed_artifact_ids"] == [aid]
    assert first["result"]["warnings"] == ["retention_partial_failure"]
    assert first["warnings"] == ["retention_partial_failure"] and target.exists()
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM artifacts WHERE id=?", (aid,)).fetchone()[0] == "deleting"
        assert conn.execute("SELECT state FROM operation_journal WHERE kind='resource_cleanup'").fetchone()[0] == "failed"
    replay, code = execute(db, request)
    assert code == 0 and replay == first
    assert not target.exists()
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM artifacts WHERE id=?", (aid,)).fetchone()[0] == "deleted"
        assert conn.execute("SELECT state FROM operation_journal WHERE kind='resource_cleanup'").fetchone()[0] == "completed"


def test_progress_corruption_is_stale_and_observable(tmp_path):
    db, _, _, item = setup(tmp_path)
    made, _ = execute(db, save_request(item, tmp_path))
    step = made["result"]["step_id"]
    route = {"agent": "codex", "provider": "openai", "model": "test", "mode": "subagent", "max_concurrency": 3,
             "selection_reason": "test", "actual_support": "verified_supported"}
    queued, _ = execute(db, req("enqueue_execution", {"step_id": step, "route": route}))
    run_id = queued["result"]["run_id"]
    with db.write() as conn:
        conn.execute("INSERT INTO run_progress VALUES(?,?,?,?)", (run_id, utc_now(), utc_now(), "{broken"))
    read, code = execute(db, req("read_progress", {"run_id": run_id}))
    assert code == 0 and read["result"]["observation_unavailable"] is True
    assert read["result"]["observation_stale"] is True and read["result"]["next_action"] == "check runner status"


def test_progress_references_require_ready_uuid_artifacts(tmp_path):
    db, _, _, item = setup(tmp_path)
    made, _ = execute(db, save_request(item, tmp_path))
    step = made["result"]["step_id"]
    route = {"agent": "codex", "provider": "openai", "model": "test", "mode": "subagent", "max_concurrency": 3,
             "selection_reason": "test", "actual_support": "verified_supported"}
    queued, _ = execute(db, req("enqueue_execution", {"step_id": step, "route": route}))
    rejected, code = execute(db, req("observe_progress", {"run_id": queued["result"]["run_id"],
        "observation": {"artifact_refs": [{"id": "raw-object"}]}}))
    assert code == 2 and rejected["error"]["code"] == "invalid_identifier"


def test_new_reference_racing_reserved_delete_is_rejected(tmp_path, monkeypatch):
    db, scope, _, item = setup(tmp_path)
    resource = persist_json_resource(db, req("test"), {"temporary": "race"}, scope, "test")
    aid = resource["artifact_id"]
    with db.write() as conn:
        conn.execute("UPDATE artifacts SET retention_until='2000-01-01T00:00:00Z',created_at='2000-01-01T00:00:00Z' WHERE id=?", (aid,))
    request = req("execute_retention", {"artifact_ids": [aid]})
    original_path = operations._artifact_path
    attempted = []
    def reference_during_delete(target_db, relative):
        if not attempted:
            try:
                with target_db.write() as conn:
                    _register_handler(conn, req("register_resource"), artifact_id=aid,
                        digest=resource["sha256"], size=resource["size_bytes"], relative=relative,
                        retention="evidence", scope_id=scope, owner_record_id=item)
            except Exception as error:
                attempted.append(getattr(error, "code", type(error).__name__))
        return original_path(target_db, relative)
    monkeypatch.setattr(operations, "_artifact_path", reference_during_delete)
    result, code = execute(db, request)
    assert code == 0 and result["result"]["removed_artifact_ids"] == [aid]
    assert attempted == ["resource_not_ready"]
    assert not (db.root / resource["relative_path"]).exists()
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM artifact_refs WHERE artifact_id=?", (aid,)).fetchone()[0] == 0
