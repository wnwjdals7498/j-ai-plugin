"""Focused G1 acceptance scenarios using the public CLI and real SQLite modules."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from pmt.db import Database
from pmt.diagnostics import DiagnosticLogger, JsonFormatter, ObservableStreamHandler
from pmt.lifecycle import handle as lifecycle_handle
from pmt.resources import execute as resource_execute
from pmt.verification import handle as verification_handle


def _id():
    return str(uuid.uuid4())


def _request(operation, payload=None, **overrides):
    value = {"protocol_version": 1, "operation": operation, "request_id": _id(),
             "actor": "main", "session_id": "acceptance-session", "payload": payload or {},
             "context_refs": [], "source": {"product": "test"}}
    value.update(overrides)
    return value


def _life(db, request):
    return db.run_request(request, lambda conn, req: lifecycle_handle(db, conn, req))


def _scope(db, name="acceptance"):
    response, code = _life(db, _request("create_scope", {"kind": "project", "slug": name}))
    assert code == 0, response
    return response["result"]["scope_id"]


def _item(db, scope_id, workspace, *, criteria=("C1",), parent_id=None, title="Acceptance item"):
    body = {"criteria": list(criteria), "workspace": str(workspace), "blocked_by": []}
    payload = {"kind": "item", "title": title, "reason": "G1 fixture", "body": body}
    if parent_id:
        payload["parent_id"] = parent_id
    response, code = _life(db, _request("save_change", payload, scope_id=scope_id))
    assert code == 0, response
    return response["result"]


def _claim(db, item, session="acceptance-session"):
    response, code = _life(db, _request("claim_task", record_id=item["id"],
                                         expected_revision=item["revision"], session_id=session))
    assert code == 0, response
    return response["result"]


def _lookup_before(db, item, definition="acceptance-suite", version="1", command=None):
    command = command or ["python", "-m", "pytest"]
    request = _request("lookup_verification", {
        "definition_id": definition, "definition_version": version,
        "target_id": item["id"], "command": command,
    }, record_id=item["id"])
    with db.connect() as conn:
        result = verification_handle(db, conn, request)
    assert isinstance(result.get("input_fingerprint"), str) and len(result["input_fingerprint"]) == 64
    return result["input_fingerprint"]


def _record_verification(db, item, evidence_ids, *, outcome="pass", exit_code=0,
                         criterion_ids=None, definition="acceptance-suite", version="1", command=None):
    command = command or ["python", "-m", "pytest"]
    before_fingerprint = _lookup_before(db, item, definition, version, command)
    payload = {"definition_id": definition, "definition_version": version, "target_id": item["id"],
               "command": command, "outcome": outcome, "exit_code": exit_code,
               "evidence_ids": list(evidence_ids), "before_fingerprint": before_fingerprint}
    if criterion_ids is not None:
        payload["criterion_ids"] = list(criterion_ids)
    request = _request("record_verification", payload, record_id=item["id"])
    return db.run_request(request, lambda conn, req: verification_handle(db, conn, req))


def _register_evidence(db, scope_id, tmp_path, content=b"G1 evidence\n"):
    allowed = tmp_path / "evidence-source"
    allowed.mkdir(exist_ok=True)
    source = allowed / "evidence.txt"
    source.write_bytes(content)
    request = _request("register_resource", {"source_path": str(source), "allowed_root": str(allowed),
                                             "retention": "evidence"}, scope_id=scope_id)
    response, code = resource_execute(db, request)
    assert code == 0, response
    return response["result"]["artifact_id"]


def _finish(db, owned, item, *, result="Finished with evidence", verification_ids=None):
    request = _request("finish_task", {"claim_token": owned["claim_token"], "result": result,
                                       "verification_ids": verification_ids or []},
                       record_id=item["id"], expected_revision=owned["revision"],
                       session_id=owned["owner_session"])
    return _life(db, request)


def test_done01_unknown_verification_snapshot_cannot_finish(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    item = _item(db, _scope(db), tmp_path / "workspace-not-created")
    owned = _claim(db, item)
    blocked, code = _record_verification(db, item, [], outcome="blocked", exit_code=None)
    assert code == 0 and blocked["result"]["state"] == "unknown"
    rejected, code = _finish(db, owned, item, verification_ids=[blocked["result"]["verification_id"]])
    assert code == 2 and rejected["error"]["code"] == "completion_criteria_unmet"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (item["id"],)).fetchone()[0] == "In Progress"
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='task_finished'").fetchone()[0] == 0


def test_data01_cli_setup_is_stable_across_processes_and_record_reads(cli, request_factory):
    setups = []
    for _ in range(2):
        request = request_factory("setup", {"product": "cli"})
        completed = cli.call(request)
        assert completed.returncode == 0, completed.stderr
        setups.append(json.loads(completed.stdout))
    keys = ("db_id", "environment_id", "installation_id")
    assert all(setups[0]["result"][key] == setups[1]["result"][key] for key in keys)

    created = cli.call(request_factory("create_scope", {"kind": "project", "slug": "DATA-01"}))
    assert created.returncode == 0, created.stderr
    scope_id = json.loads(created.stdout)["result"]["scope_id"]
    saved = cli.call(request_factory("save_change", {
        "kind": "item", "title": "Persisted item", "reason": "DATA-01",
        "body": {"criteria": ["C1"], "workspace": str(cli.cwd)},
    }, scope_id=scope_id))
    assert saved.returncode == 0, saved.stderr
    record_id = json.loads(saved.stdout)["result"]["record_id"]
    read = cli.call(request_factory("read_context", {}, scope_id=scope_id))
    assert read.returncode == 0, read.stderr
    view = json.loads(read.stdout)["result"]
    assert record_id in {record["record_id"] for record in view["records"]}


def test_data02_migration_failure_rolls_back_schema_and_existing_rows(tmp_path, monkeypatch):
    data, config = tmp_path / "legacy-data", tmp_path / "legacy-config"
    data.mkdir(); config.mkdir()
    db_path = data / "pmt.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES('schema_version','1')")
        conn.execute("INSERT INTO meta VALUES('db_id','preserved-db-id')")
        conn.execute("CREATE TABLE requests(request_id TEXT PRIMARY KEY,fingerprint_version INTEGER NOT NULL,request_fingerprint TEXT NOT NULL,response_json TEXT NOT NULL,exit_code INTEGER NOT NULL,deterministic INTEGER NOT NULL,created_at TEXT NOT NULL)")
        conn.execute("INSERT INTO requests VALUES('legacy-request',1,'fp','{}',0,1,'legacy-time')")
    original = Database._put_meta

    def fail_after_schema_mutations(conn, key, value):
        if key == "environment_id":
            raise sqlite3.OperationalError("injected migration metadata write failure")
        return original(conn, key, value)

    monkeypatch.setattr(Database, "_put_meta", staticmethod(fail_after_schema_mutations))
    with pytest.raises(sqlite3.OperationalError, match="injected migration"):
        Database(data, config)
    with sqlite3.connect(db_path) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "1"
        assert conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()[0] == "preserved-db-id"
        assert tuple(conn.execute("SELECT request_id,response_json FROM requests").fetchone()) == ("legacy-request", "{}")
        assert {row[1] for row in conn.execute("PRAGMA table_info(requests)")} == {
            "request_id", "fingerprint_version", "request_fingerprint", "response_json", "exit_code", "deterministic", "created_at"}
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='artifacts'").fetchone() is None


def test_idem01_two_processes_create_one_record_and_replay_original(cli, request_factory):
    scope = json.loads(cli.call(request_factory("create_scope", {"kind": "project", "slug": "IDEM-01"})).stdout)["result"]["scope_id"]
    request = request_factory("save_change", {"kind": "item", "title": "one logical creation",
                                                 "reason": "IDEM-01", "body": {"criteria": ["C1"]}},
                              scope_id=scope)
    barrier = threading.Barrier(2)

    def invoke():
        barrier.wait(timeout=10)
        return cli.call(request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: invoke(), range(2)))
    assert first.returncode == second.returncode == 0
    a, b = json.loads(first.stdout), json.loads(second.stdout)
    assert a["result"] == b["result"]
    assert a["result"]["revision"] == 1 and a["result"]["record_id"] == b["result"]["record_id"]
    with sqlite3.connect(cli.data_root / "pmt.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM records WHERE id=?", (a["result"]["record_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='record_created' AND record_id=?",
                            (a["result"]["record_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (request["request_id"],)).fetchone()[0] == 1


def test_idem02_semantic_field_changes_conflict_without_mutating_original(cli, request_factory):
    scope = json.loads(cli.call(request_factory("create_scope", {"kind": "project", "slug": "IDEM-02"})).stdout)["result"]["scope_id"]
    created = cli.call(request_factory("save_change", {"kind": "item", "title": "before", "reason": "fixture"}, scope_id=scope))
    assert created.returncode == 0
    record_id = json.loads(created.stdout)["result"]["record_id"]
    original = request_factory("save_change", {"title": "after", "reason": "original meaning"},
                               record_id=record_id, expected_revision=1)
    success = cli.call(original)
    assert success.returncode == 0
    original_response = json.loads(success.stdout)
    with sqlite3.connect(cli.data_root / "pmt.sqlite3") as conn:
        records_before = conn.execute("SELECT id,title,state,revision,body_json FROM records ORDER BY id").fetchall()
        events_before = conn.execute("SELECT event_id,event_type,record_id,old_revision,new_revision,payload_json FROM events ORDER BY id").fetchall()
        requests_before = conn.execute("SELECT request_id,request_fingerprint,response_json,exit_code FROM requests ORDER BY request_id").fetchall()

    variants = []
    changed = json.loads(json.dumps(original)); changed["operation"] = "record_event"; changed["normalized_event"] = {"event_id": str(uuid.uuid4()), "type": "noise"}; variants.append(changed)
    changed = json.loads(json.dumps(original)); changed["actor"] = "different-actor"; variants.append(changed)
    changed = json.loads(json.dumps(original)); changed["session_id"] = "different-session"; variants.append(changed)
    changed = json.loads(json.dumps(original)); changed["record_id"] = str(uuid.uuid4()); variants.append(changed)
    changed = json.loads(json.dumps(original)); changed["expected_revision"] = 2; variants.append(changed)
    changed = json.loads(json.dumps(original)); changed["payload"]["title"] = "different payload"; variants.append(changed)
    changed = json.loads(json.dumps(original)); changed["context_refs"] = [str(uuid.uuid4())]; variants.append(changed)

    for variant in variants:
        completed = cli.call(variant)
        assert completed.returncode == 3
        response = json.loads(completed.stdout)
        assert response["ok"] is False and response["error"]["code"] in {"request_conflict", "request_owner_mismatch"}
        with sqlite3.connect(cli.data_root / "pmt.sqlite3") as conn:
            assert conn.execute("SELECT id,title,state,revision,body_json FROM records ORDER BY id").fetchall() == records_before
            assert conn.execute("SELECT event_id,event_type,record_id,old_revision,new_revision,payload_json FROM events ORDER BY id").fetchall() == events_before
            assert conn.execute("SELECT request_id,request_fingerprint,response_json,exit_code FROM requests ORDER BY request_id").fetchall() == requests_before
        replay = cli.call(original)
        assert replay.returncode == 0 and json.loads(replay.stdout) == original_response


def test_claim02_other_session_and_old_token_cannot_release_or_finish(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    workspace = tmp_path / "workspace"; workspace.mkdir()
    item = _item(db, _scope(db), workspace)
    first = _claim(db, item)
    wrong_session = _request("release_claim", {"claim_token": first["claim_token"], "status": "Paused",
                                                "reason": "wrong owner", "resume": "later"},
                             record_id=item["id"], expected_revision=first["revision"], session_id="other-session")
    rejected, code = _life(db, wrong_session)
    assert code == 3 and rejected["error"]["code"] == "claim_conflict"
    release, code = _life(db, _request("release_claim", {"claim_token": first["claim_token"], "status": "Paused",
                                                           "reason": "pause", "resume": "reclaim"},
                                        record_id=item["id"], expected_revision=first["revision"]))
    assert code == 0
    second = _claim(db, release["result"])
    stale_finish = _request("finish_task", {"claim_token": first["claim_token"], "result": "stale owner",
                                            "verification_ids": []}, record_id=item["id"],
                            expected_revision=second["revision"])
    rejected, code = _life(db, stale_finish)
    assert code == 3 and rejected["error"]["code"] == "claim_conflict"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (item["id"],)).fetchone()[0] == "In Progress"
        claim = conn.execute("SELECT owner_session,token_hash FROM claims WHERE record_id=?", (item["id"],)).fetchone()
        assert claim[0] == "acceptance-session"


def test_claim03_confirmed_child_process_termination_allows_explicit_recovery(cli, request_factory):
    scope_cp = cli.call(request_factory("create_scope", {"kind": "project", "slug": "CLAIM-03"}))
    assert scope_cp.returncode == 0, scope_cp.stderr
    scope_id = json.loads(scope_cp.stdout)["result"]["scope_id"]
    item_cp = cli.call(request_factory("save_change", {
        "kind": "item", "title": "worker item", "reason": "CLAIM-03", "body": {"criteria": ["C1"]}},
        scope_id=scope_id))
    assert item_cp.returncode == 0, item_cp.stderr
    item = json.loads(item_cp.stdout)["result"]
    worker_session = "crashed-worker-session"
    claim_request = request_factory("claim_task", {}, record_id=item["record_id"],
                                    expected_revision=item["revision"], session_id=worker_session)
    script = """
import json, os, subprocess, sys
request = sys.argv[1]
command = [sys.executable, '-m', 'pmt', '--data-root', sys.argv[2], '--config-root', sys.argv[3]]
completed = subprocess.run(command, input=request, text=True, encoding='utf-8', capture_output=True,
                           cwd=sys.argv[4], env=os.environ, check=False)
sys.stdout.write(completed.stdout)
sys.stdout.flush()
if completed.returncode != 0:
    sys.stderr.write(completed.stderr)
    sys.stderr.flush()
    os._exit(74)
os._exit(73)
"""
    process = subprocess.Popen([sys.executable, "-c", script, json.dumps(claim_request),
                                str(cli.data_root), str(cli.config_root), str(cli.cwd)],
                               cwd=cli.cwd, env=cli.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8")
    child_pid = process.pid
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 73, stderr
    claim_response = json.loads(stdout)
    assert claim_response["ok"] is True
    old_token = claim_response["result"]["claim_token"]
    terminated_or_isolated = {"pid": child_pid, "exit_code": process.returncode, "observed_terminated": True}

    recover_request = request_factory("recover_claim", {
        "reason": "worker process exit was observed", "terminated_or_isolated": terminated_or_isolated},
        record_id=item["record_id"], expected_revision=claim_response["result"]["revision"],
        actor="main", session_id="recovery-session")
    recovered_cp = cli.call(recover_request)
    assert recovered_cp.returncode == 0, recovered_cp.stderr
    recovered = json.loads(recovered_cp.stdout)["result"]
    assert recovered["state"] == "In Progress" and recovered["owner_session"] == "recovery-session"
    assert recovered["revision"] == claim_response["result"]["revision"] + 1

    stale_finish = request_factory("finish_task", {"claim_token": old_token, "result": "stale child result",
                                                    "verification_ids": []},
                                   record_id=item["record_id"], expected_revision=recovered["revision"],
                                   session_id=worker_session)
    rejected_cp = cli.call(stale_finish)
    assert rejected_cp.returncode == 3
    rejected = json.loads(rejected_cp.stdout)
    assert rejected["error"]["code"] == "claim_conflict"
    with sqlite3.connect(cli.data_root / "pmt.sqlite3") as conn:
        row = conn.execute("SELECT state,revision FROM records WHERE id=?", (item["record_id"],)).fetchone()
        claim = conn.execute("SELECT owner_session FROM claims WHERE record_id=?", (item["record_id"],)).fetchone()
        assert row == ("In Progress", recovered["revision"])
        assert claim == ("recovery-session",)


def test_done01_missing_result_and_failed_verification_never_mark_done(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    workspace = tmp_path / "workspace"; workspace.mkdir()
    item = _item(db, _scope(db), workspace)
    owned = _claim(db, item)
    no_result = _request("finish_task", {"claim_token": owned["claim_token"], "verification_ids": []},
                         record_id=item["id"], expected_revision=owned["revision"])
    rejected, code = _life(db, no_result)
    assert code == 2 and rejected["error"]["code"] == "finish_result_required"

    failed, code = _record_verification(db, item, [], outcome="fail", exit_code=1, criterion_ids=["C1"])
    assert code == 0, failed
    rejected, code = _finish(db, owned, item, verification_ids=[failed["result"]["verification_id"]])
    assert code == 2 and rejected["error"]["code"] == "completion_criteria_unmet"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (item["id"],)).fetchone()[0] == "In Progress"


def test_done01_corrupt_evidence_and_unfinished_child_prevent_done(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    workspace = tmp_path / "workspace"; workspace.mkdir()
    (workspace / "input.txt").write_text("stable input", encoding="utf-8")
    scope_id = _scope(db)
    item = _item(db, scope_id, workspace)
    evidence = _register_evidence(db, scope_id, tmp_path)
    passed, code = _record_verification(db, item, [evidence], criterion_ids=["C1"])
    assert code == 0, passed
    owned = _claim(db, item)
    with db.connect() as conn:
        relative = conn.execute("SELECT relative_path FROM artifacts WHERE id=?", (evidence,)).fetchone()[0]
    (db.root / relative).write_bytes(b"corrupted evidence")
    rejected, code = _finish(db, owned, item, verification_ids=[passed["result"]["verification_id"]])
    assert code == 2 and rejected["error"]["code"] in {"completion_criteria_unmet", "completion_evidence_unavailable"}
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (item["id"],)).fetchone()[0] == "In Progress"

    parent_payload = {"kind": "work", "title": "parent", "reason": "fixture", "body": {}}
    parent, code = _life(db, _request("save_change", parent_payload, scope_id=scope_id))
    assert code == 0
    parent_row = parent["result"]
    child = _item(db, scope_id, workspace, parent_id=parent_row["id"])
    parent_claim = _claim(db, parent_row)
    unfinished, code = _finish(db, parent_claim, parent_row)
    assert code == 2 and unfinished["error"]["code"] == "unfinished_children"
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (parent_row["id"],)).fetchone()[0] == "In Progress"
        assert conn.execute("SELECT state FROM records WHERE id=?", (child["id"],)).fetchone()[0] == "Planned"


def test_done02_successful_finish_replay_keeps_one_completion_event(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    workspace = tmp_path / "workspace"; workspace.mkdir()
    (workspace / "input.txt").write_text("stable", encoding="utf-8")
    scope_id = _scope(db)
    item = _item(db, scope_id, workspace)
    evidence = _register_evidence(db, scope_id, tmp_path)
    passed, code = _record_verification(db, item, [evidence], criterion_ids=["C1"])
    assert code == 0, passed
    owned = _claim(db, item)
    request = _request("finish_task", {"claim_token": owned["claim_token"], "result": "verified complete",
                                       "verification_ids": [passed["result"]["verification_id"]]},
                       record_id=item["id"], expected_revision=owned["revision"])
    first, code = _life(db, request)
    assert code == 0 and first["result"]["state"] == "Done"
    replay, replay_code = _life(db, request)
    assert replay_code == 0 and replay == first
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE record_id=? AND event_type='task_finished'",
                            (item["id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT state,revision FROM records WHERE id=?", (item["id"],)).fetchone()[0] == "Done"


def test_decision01_delegate_requires_scope_and_custom_can_be_replaced_by_select(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    target = _item(db, _scope(db), tmp_path)
    missing_scope = _request("save_decision", {"decision_kind": "delegate", "decider": "main",
                                               "reason": "delegate", "confirmation_source": "explicit"},
                             record_id=target["id"], expected_revision=target["revision"])
    rejected, code = _life(db, missing_scope)
    assert code == 2 and rejected["error"]["code"] == "invalid_decision"
    delegated, code = _life(db, _request("save_decision", {
        "decision_kind": "delegate", "decider": "main", "delegation_scope": {"allowed": ["inspect", "test"]},
        "reason": "delegate bounded work", "confirmation_source": "explicit_user_approval"},
        record_id=target["id"], expected_revision=target["revision"]))
    assert code == 0
    with db.connect() as conn:
        body = json.loads(conn.execute("SELECT body_json FROM records WHERE id=?", (delegated["result"]["decision_id"],)).fetchone()[0])
    assert body["delegation_scope"] == {"allowed": ["inspect", "test"]}

    second_target = _item(db, _scope(db, "replacement"), tmp_path, title="decision target")
    custom, code = _life(db, _request("save_decision", {
        "decision_kind": "custom", "decider": "user", "content": "Use a focused solution.",
        "reason": "custom option selected", "confirmation_source": "explicit_user_text"},
        record_id=second_target["id"], expected_revision=second_target["revision"]))
    assert code == 0
    old_id = custom["result"]["decision_id"]
    target_revision = custom["result"]["revision"]
    select_request = _request("save_decision", {
        "decision_kind": "select", "decider": "user", "option_id": "option-2",
        "reason": "user selected replacement", "confirmation_source": "explicit_user_choice",
        "supersedes": old_id}, record_id=second_target["id"], expected_revision=target_revision)
    selected, code = _life(db, select_request)
    assert code == 0
    replay, replay_code = _life(db, select_request)
    assert replay_code == 0 and replay == selected
    with db.connect() as conn:
        prior = conn.execute("SELECT state,body_json FROM records WHERE id=?", (old_id,)).fetchone()
        current = conn.execute("SELECT state,body_json FROM records WHERE id=?", (selected["result"]["decision_id"],)).fetchone()
        assert conn.execute("SELECT count(*) FROM records WHERE parent_id=? AND kind='decision'", (second_target["id"],)).fetchone()[0] == 2
        assert conn.execute("SELECT count(*) FROM events WHERE record_id=? AND event_type='decision_saved'", (second_target["id"],)).fetchone()[0] == 2
    assert prior[0] == "Superseded" and json.loads(prior[1])["superseded_by"] == selected["result"]["decision_id"]
    assert current[0] == "Current" and json.loads(current[1])["option_id"] == "option-2"


class _CapturingStream:
    def __init__(self):
        self.values = []

    def write(self, value):
        self.values.append(value)
        return len(value)

    def flush(self):
        return None


def test_log01_json_diagnostic_keeps_required_ids_and_excludes_secrets_paths(tmp_path):
    stream = _CapturingStream()
    logger = logging.Logger(f"pmt-log01-{_id()}", level=logging.INFO)
    logger.propagate = False
    handler = ObservableStreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    db = Database(tmp_path / "data", tmp_path / "config", logger=DiagnosticLogger(logger))
    identifiers = {"request_id": _id(), "operation": "record_event", "scope_id": _id(),
                   "record_id": _id(), "event_id": _id(), "correlation_id": _id()}
    db.diagnostics.emit("log01_acceptance", **identifiers,
                        source={"product": "codex", "token": "secret-token-value",
                                "transcript": "private transcript body", "path": "PRIVATE_ABSOLUTE_PATH"},
                        path="PRIVATE_ABSOLUTE_PATH", data_root="PRIVATE_DATA_ROOT")
    lines = [json.loads(line) for line in "".join(stream.values).splitlines() if line]
    entry = next(row for row in lines if row["event_name"] == "log01_acceptance")
    assert all(entry[key] == value for key, value in identifiers.items())
    serialized = json.dumps(entry)
    assert "secret-token-value" not in serialized
    assert "private transcript body" not in serialized
    assert "PRIVATE_ABSOLUTE_PATH" not in serialized and "PRIVATE_DATA_ROOT" not in serialized
    assert entry["source"]["token"] == "<redacted>" and entry["source"]["transcript"] == "<redacted>"
