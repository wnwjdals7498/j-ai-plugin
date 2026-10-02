from __future__ import annotations

import hashlib
from contextlib import closing
import gc
import json
import os
import subprocess
import sys
import shutil
from pathlib import Path
import uuid

import pytest

from pmt.db import Database
from pmt.efficiency.storage import Phase3Storage
from pmt.errors import PmtError
from pmt.resources import check_artifact
from pmt.store import LocalStore
from pmt.util import new_id

SCOPE_ID = "00000000-0000-4000-8000-000000000001"


def _request(output, *, operation="compact_tool_result", request_id=None, **payload):
    step_id = new_id()
    body = {"task_id": step_id, "run_id": new_id(), "step_id": step_id,
            "status": "succeeded", "exit_code": 0, "format": "text", "output": output,
            **payload}
    return {"protocol_version": 1, "operation": operation,
            "request_id": request_id or new_id(), "actor": "actor-a",
            "session_id": "session-a", "scope_id": SCOPE_ID, "payload": body}


def _db(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,slug,created_at,updated_at) VALUES(?,'project','scope','now','now')", (SCOPE_ID,))
    return db


def _seed_run(db, req):
    p = req["payload"]
    with db.write() as conn:
        if not conn.execute("SELECT 1 FROM records WHERE id=?", (p["step_id"],)).fetchone():
            now = "2026-10-02T00:00:00Z"
            conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?, 'step', ?, 'test step', 'InProgress', '{}', 1, ?, ?)",
                         (p["step_id"], req["scope_id"], now, now))
            conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,'running','{}',?,?)",
                         (new_id(), p["step_id"], now, now))
            job_id = conn.execute("SELECT id FROM execution_jobs WHERE step_id=?", (p["step_id"],)).fetchone()[0]
            conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) VALUES(?,?,?,1,'running',1,?,1,'.','[]','{}','{}',?,?)",
                         (p["run_id"], job_id, p["step_id"], req["session_id"], now, now))


def _compact(db, req):
    _seed_run(db, req)
    return LocalStore(db).execute(req)


def _detail(db, compact_response, *, actor="actor-a", session="session-a", scope=SCOPE_ID, **payload):
    request = {"protocol_version": 1, "operation": "read_tool_result_detail", "request_id": new_id(),
               "actor": actor, "session_id": session,
               "scope_id": scope,
               "payload": {"result_id": compact_response["result"]["result_id"], **payload}}
    envelope, code = LocalStore(db).execute(request)
    if code:
        issue = envelope.get("error") or {}
        raise PmtError(issue.get("code", "detail_failed"), issue.get("message", "detail failed"), code)
    return envelope["result"]


def test_f7_01_actual_resource_hash_summary_and_no_criteria_pass(tmp_path):
    db = _db(tmp_path)
    req = _request("first line\nsecond line\n", criteria_claims=[{"status": "pass", "text": "claimed"}])
    envelope, code = _compact(db, req)
    result = envelope["result"]
    assert code == 0 and envelope["ok"]
    assert result["status"] == "succeeded" and result["exit_code"] == 0
    assert result["criteria_verdict"]["status"] == "not_evaluated"
    assert result["preservation"] == "complete_redacted"
    staging = db.root / "resources" / ".staging"
    assert not list(staging.glob("tool-result-*.txt"))
    with closing(db.connect()) as conn:
        assert check_artifact(db, conn, result["evidence_ref"]["id"])["valid"]
        metadata = Phase3Storage(db).get_object("tool_result", result["result_id"], SCOPE_ID, "actor-a", "session-a", conn=conn)
        assert metadata["body"]["storage_kind"] == "registered_resource"
        assert conn.execute("SELECT count(*) FROM artifact_refs WHERE owner_id=?", (result["result_id"],)).fetchone()[0] == 1
    detail = _detail(db, envelope)
    assert detail["content"] == "first line\nsecond line\n"
    assert detail["artifact_sha256"] == hashlib.sha256(detail["content"].encode()).hexdigest()


def test_f7_02_secret_redaction_and_line_byte_cursor_are_exact(tmp_path):
    db = _db(tmp_path)
    secret = "api_key=sk_live_secret\nTOKEN=topsecret\n가나다\nlast\n"
    req = _request(secret, criteria_claims=["must not be copied"])
    envelope, code = _compact(db, req)
    assert code == 0
    assert "sk_live_secret" not in str(envelope)
    assert "topsecret" not in str(envelope)
    with closing(db.connect()) as conn:
        stored = " ".join(str(row[0]) for row in conn.execute(
            "SELECT body_json FROM phase3_objects UNION ALL SELECT body_json FROM phase3_journal UNION ALL SELECT outcome_json FROM phase3_journal WHERE outcome_json IS NOT NULL"))
    assert "sk_live_secret" not in stored and "topsecret" not in stored
    first = _detail(db, envelope, max_bytes=20, max_lines=1)
    assert len(first["content"].encode("utf-8")) <= 20
    assert len(first["content"].splitlines()) <= 1
    cursor = first["next_cursor"]
    assert cursor
    parts = [first["content"]]
    while cursor:
        part = _detail(db, envelope, cursor=cursor, max_bytes=20, max_lines=1)
        assert part["start_byte"] == sum(len(x.encode("utf-8")) for x in parts)
        parts.append(part["content"])
        cursor = part["next_cursor"]
    combined = "".join(parts)
    assert "[REDACTED]" in combined and "sk_live_secret" not in combined and "topsecret" not in combined
    assert combined == "api_key=[REDACTED]\nTOKEN=[REDACTED]\n가나다\nlast\n"


def test_f7_02_unlabeled_provider_key_is_redacted_and_claim_body_is_fingerprinted(tmp_path):
    db = _db(tmp_path)
    req = _request("provider response sk-abcdefgh12345678\n", criteria_claims=[{"claim": "one"}])
    envelope, code = _compact(db, req)
    assert code == 0
    detail = _detail(db, envelope)
    assert "sk-abcdefgh12345678" not in detail["content"]
    changed_claim = {**req, "payload": {**req["payload"], "criteria_claims": [{"claim": "two"}]}}
    conflict, conflict_code = _compact(db, changed_claim)
    assert conflict_code != 0 and conflict["error"]["code"] == "request_conflict"


def test_f7_01_status_exit_mismatch_is_unknown_and_runner_receipt_is_authoritative(tmp_path):
    db = _db(tmp_path)
    contradictory = _request("reported output\n", status="succeeded", exit_code=7)
    result, code = _compact(db, contradictory)
    assert code == 0
    assert result["result"]["reported_status"] == "succeeded"
    assert result["result"]["status"] == "unknown"
    assert result["result"]["status_reason"] == "success_claim_has_nonzero_or_unknown_exit"
    assert result["result"]["criteria_verdict"]["status"] == "not_evaluated"

    with_unknown_exit = _request("result\n", status="failed", exit_code=None)
    unknown_result, unknown_code = _compact(db, with_unknown_exit)
    assert unknown_code == 0 and unknown_result["result"]["status"] == "unknown"

    receipt_req = _request("runner-backed output\n", status="succeeded", exit_code=7)
    _seed_run(db, receipt_req)
    receipt_id = new_id()
    relative = f"resources/objects/{receipt_id}"
    path = db.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    receipt_bytes = json.dumps({"run_id": receipt_req["payload"]["run_id"],
                                "receipt": {"exit_code": 7, "state": "failed"}}).encode("utf-8")
    path.write_bytes(receipt_bytes)
    with db.write() as conn:
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,created_at) VALUES(?,?,?,?,?,'ready','now')",
                     (receipt_id, SCOPE_ID, hashlib.sha256(receipt_bytes).hexdigest(), len(receipt_bytes), relative))
        conn.execute("UPDATE execution_runs SET result_json=? WHERE id=?",
                     (json.dumps({"receipt_ref": receipt_id, "runner_observation": {"exit_code": 7}}),
                      receipt_req["payload"]["run_id"]))
    actual, actual_code = LocalStore(db).execute(receipt_req)
    assert actual_code == 0
    assert actual["result"]["status"] == "unknown"
    assert actual["result"]["exit_code"] == 7
    assert actual["result"]["status_source"] == "runner_receipt"
    assert actual["result"]["runner_receipt_ref"] == receipt_id


def test_f7_01_status_consistent_with_producer_exit_is_still_not_a_criteria_pass(tmp_path):
    db = _db(tmp_path)
    failed = _request("failure output\n", status="failed", exit_code=9)
    result, code = _compact(db, failed)
    assert code == 0 and result["result"]["status"] == "failed"
    assert result["result"]["status_source"] == "producer_observation"
    assert result["result"]["criteria_verdict"]["status"] == "not_evaluated"


def test_public_detail_operation_releases_database_handle_for_cleanup(tmp_path):
    db = _db(tmp_path)
    compact, code = _compact(db, _request("first\r\nsecond\r\n"))
    assert code == 0
    detail = _detail(db, compact, max_bytes=100, max_lines=1)
    assert detail["content"] == "first\r\n"
    data_root = db.root
    gc.collect()
    shutil.rmtree(data_root)
    assert not data_root.exists()


def test_f7_01_detail_is_available_through_protocol_v1_cli(tmp_path):
    db = _db(tmp_path)
    compact, compact_code = _compact(db, _request("cli output\nsecond line\n"))
    assert compact_code == 0
    request = {"protocol_version": 1, "operation": "read_tool_result_detail", "request_id": new_id(),
               "actor": "actor-a", "session_id": "session-a", "scope_id": SCOPE_ID,
               "payload": {"result_id": compact["result"]["result_id"], "max_bytes": 100, "max_lines": 1}}
    checkout = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PYTHONPATH": str(checkout / "src")}
    process = subprocess.run([sys.executable, "-m", "pmt", "--data-root", str(db.root),
                              "--config-root", str(db.config_root)],
                             input=json.dumps(request), text=True, encoding="utf-8",
                             capture_output=True, cwd=checkout, env=env, timeout=20, check=False)
    assert process.returncode == 0, process.stderr
    envelope = json.loads(process.stdout)
    assert envelope["ok"] and envelope["result"]["content"] == "cli output\n"


def test_f7_01_registered_source_file_over_inline_limit_is_scope_and_owner_checked(tmp_path):
    db = _db(tmp_path)
    req = _request("")
    p = req["payload"]
    p.pop("output")
    raw = ("line-data-한글\n" * 80_000).encode("utf-8")
    artifact_id = new_id()
    relative = f"resources/objects/{artifact_id}"
    path = db.root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    with db.write() as conn:
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,created_at) VALUES(?,?,?,?,?,'ready','now')",
                     (artifact_id, SCOPE_ID, hashlib.sha256(raw).hexdigest(), len(raw), relative))
        conn.execute("INSERT INTO artifact_refs(artifact_id,owner_type,owner_id,purpose,created_at) VALUES(?,?,?,?,?)",
                     (artifact_id, "record", p["step_id"], "evidence", "now"))
    p["source_artifact_id"] = artifact_id
    envelope, code = _compact(db, req)
    assert code == 0 and envelope["result"]["source_size_bytes"] == len(raw)
    detail = _detail(db, envelope, max_bytes=4096, max_lines=4)
    assert len(detail["content"].encode("utf-8")) <= 4096
    assert len(detail["content"].splitlines()) <= 4


def test_f7_02_nested_sensitive_json_fields_are_replaced(tmp_path):
    db = _db(tmp_path)
    req = _request('{"ok":true,"nested":{"access_token":"secret-value","answer":3},"conversation":"private"}', format="json")
    envelope, code = _compact(db, req)
    assert code == 0
    detail = _detail(db, envelope)
    assert "secret-value" not in detail["content"]
    assert '"conversation":"[REDACTED]"' in detail["content"]
    assert '"access_token":"[REDACTED]"' in detail["content"]


def test_f7_02_environment_dump_and_plain_transcript_are_not_preserved(tmp_path):
    db = _db(tmp_path)
    env_output = _request("HOME=C:\\Users\\private\nPATH=C:\\private\\bin\nOS=private-os\n")
    env_result, env_code = _compact(db, env_output)
    assert env_code == 0
    env_detail = _detail(db, env_result)
    assert "C:\\Users\\private" not in env_detail["content"]
    assert "private-os" not in env_detail["content"]
    transcript = _request("user: private question\nassistant: private answer\n")
    transcript_result, transcript_code = _compact(db, transcript)
    assert transcript_code == 0
    transcript_detail = _detail(db, transcript_result)
    assert "private question" not in transcript_detail["content"]
    assert "private answer" not in transcript_detail["content"]


def test_f7_02_owner_scope_hash_cursor_and_resource_corruption_rejected(tmp_path):
    db = _db(tmp_path)
    envelope, _ = _compact(db, _request("0123456789\n" * 6))
    with pytest.raises(PmtError, match="unavailable"):
        _detail(db, envelope, actor="other")
    with pytest.raises(PmtError, match="unavailable"):
        _detail(db, envelope, scope="00000000-0000-4000-8000-000000000002")
    page = _detail(db, envelope, max_bytes=8)
    with pytest.raises(PmtError):
        _detail(db, envelope, cursor=page["next_cursor"] + "x", max_bytes=8)
    ref_id = envelope["result"]["evidence_ref"]["id"]
    with closing(db.connect()) as conn:
        path = db.root / conn.execute("SELECT relative_path FROM artifacts WHERE id=?", (ref_id,)).fetchone()[0]
    path.write_bytes(b"tampered")
    with pytest.raises(PmtError, match="integrity"):
        _detail(db, envelope)


def test_f7_03_binary_invalid_json_and_oversize_are_not_successfully_preserved(tmp_path):
    db = _db(tmp_path)
    binary, binary_code = _compact(db, _request("", format="binary"))
    assert binary_code == 0 and binary["result"]["capability"] == "unsupported"
    assert binary["result"]["preservation"] == "not_preserved"
    invalid_json, json_code = _compact(db, _request("not-json", format="json"))
    assert json_code == 0 and invalid_json["result"]["capability"] == "unsupported_json"
    oversized, size_code = _compact(db, _request("x" * (900 * 1024 + 1)))
    assert size_code != 0 and oversized["error"]["code"] == "result_too_large"


def test_f7_02_failed_publish_can_resume_after_restart_with_same_request(tmp_path, monkeypatch):
    db = _db(tmp_path)
    req = _request("resume me\n")
    import pmt.resources as resource_module
    original = resource_module.execute
    calls = {"count": 0}

    def fail_once(database, request):
        calls["count"] += 1
        if calls["count"] == 1:
            return {"ok": False, "error": {"code": "injected"}}, 4
        return original(database, request)

    monkeypatch.setattr(resource_module, "execute", fail_once)
    failed, failed_code = _compact(db, req)
    assert failed_code == 4 and failed["error"]["code"] == "injected"
    restarted = Database(db.root, db.config_root)
    resumed, resumed_code = _compact(restarted, req)
    assert resumed_code == 0 and resumed["result"]["preservation"] == "complete_redacted"
    replay, replay_code = _compact(restarted, req)
    assert replay_code == 0 and replay["result"] == resumed["result"]
    changed = {**req, "payload": {**req["payload"], "output": "different"}}
    conflict, conflict_code = _compact(restarted, changed)
    assert conflict_code != 0 and conflict["error"]["code"] == "request_conflict"


def test_f7_02_storage_failure_does_not_return_evidence_or_pass(tmp_path, monkeypatch):
    db = _db(tmp_path)
    req = _request("actual output\n")
    original = Phase3Storage.put_object

    def fail(self, *args, **kwargs):
        raise PmtError("injected_storage_failure", "injected")

    monkeypatch.setattr(Phase3Storage, "put_object", fail)
    envelope, code = _compact(db, req)
    assert code != 0 and envelope["error"]["code"] == "injected_storage_failure"
    assert not envelope.get("result")
    monkeypatch.setattr(Phase3Storage, "put_object", original)
    replay, replay_code = _compact(Database(db.root, db.config_root), req)
    assert replay_code == 0 and replay["result"]["criteria_verdict"]["status"] == "not_evaluated"
