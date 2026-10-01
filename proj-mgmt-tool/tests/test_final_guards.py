"""Negative guards for decision supersession, verification order, and explicit roots."""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pmt.db import Database
from pmt.lifecycle import create_scope, save_change, save_decision
from pmt.paths import default_roots
from pmt.resources import execute as resource_execute
from pmt.util import new_id
import pmt.verification as verification


def request(operation, payload=None, **fields):
    result = {"protocol_version": 1, "operation": operation, "request_id": str(uuid.uuid4()),
              "actor": "main", "session_id": "final-guard", "payload": payload or {}}
    result.update(fields)
    return result


def invoke(db, req, handler):
    return db.run_request(req, lambda conn, value: handler(db, conn, value))


def add_item(db, workspace: Path):
    scope_response, code = invoke(db, request("create_scope", {"kind": "project", "slug": "guard"}), create_scope)
    assert code == 0
    scope_id = scope_response["result"].get("scope_id") or scope_response["result"]["id"]
    item_response, code = invoke(db, request("save_change", {
        "kind": "item", "title": "guard item", "reason": "isolated negative guard",
        "body": {"criteria": ["C1"], "workspace": str(workspace)},
    }, scope_id=scope_id), save_change)
    assert code == 0
    return scope_id, item_response["result"]


def test_obsolete_supersedes_cannot_create_multiple_current_decisions(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    scope_id, item = add_item(db, workspace)

    first, code = invoke(db, request("save_decision", {
        "decision_kind": "select", "option_id": "A", "decider": "user",
        "reason": "first selection", "confirmation_source": "explicit user choice",
    }, record_id=item["id"], expected_revision=item["revision"]), save_decision)
    assert code == 0
    first_result = first["result"]
    first_id = first_result["decision_id"]

    second, code = invoke(db, request("save_decision", {
        "decision_kind": "custom", "content": "second decision", "decider": "user",
        "reason": "supersede current", "confirmation_source": "explicit user text",
        "supersedes": first_id,
    }, record_id=item["id"], expected_revision=first_result["revision"]), save_decision)
    assert code == 0
    second_result = second["result"]

    obsolete, code = invoke(db, request("save_decision", {
        "decision_kind": "custom", "content": "third decision", "decider": "user",
        "reason": "must not supersede an obsolete decision", "confirmation_source": "explicit user text",
        "supersedes": first_id,
    }, record_id=item["id"], expected_revision=second_result["revision"]), save_decision)
    assert code == 2 and obsolete["ok"] is False
    assert obsolete["error"]["code"] == "invalid_supersedes"
    with db.connect() as conn:
        current = conn.execute("SELECT id FROM records WHERE parent_id=? AND kind='decision' AND state='Current'",
                               (item["id"],)).fetchall()
        old = conn.execute("SELECT state,body_json FROM records WHERE id=?", (first_id,)).fetchone()
        third_count = conn.execute("SELECT count(*) FROM records WHERE parent_id=? AND title='third decision'",
                                   (item["id"],)).fetchone()[0]
    assert len(current) == 1 and current[0]["id"] == second_result["decision_id"]
    assert old["state"] == "Superseded"
    assert json.loads(old["body_json"])["superseded_by"] == second_result["decision_id"]
    assert third_count == 0


@pytest.mark.parametrize("clock_mode", ["same_timestamp", "clock_back"])
def test_failure_inserted_after_success_invalidates_even_when_clock_does_not_advance(tmp_path, monkeypatch, clock_mode):
    db = Database(tmp_path / "data", tmp_path / "config", busy_timeout_ms=500)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("stable verification input\n", encoding="utf-8")
    scope_id, item = add_item(db, workspace)
    evidence = tmp_path / "verification proof.txt"
    evidence.write_text("verified evidence\n", encoding="utf-8")
    artifact, code = resource_execute(db, request("register_resource", {
        "source_path": str(evidence), "allowed_root": str(tmp_path),
        "retention": "evidence", "owner_record_id": item["id"],
    }, scope_id=scope_id))
    assert code == 0 and artifact["ok"]
    artifact_id = artifact["result"]["artifact_id"]
    command = ["python", "-m", "pytest"]
    common = {"definition_id": "verification-order-guard", "definition_version": "1",
              "target_id": item["id"], "command": command, "criterion_ids": ["C1"]}
    before_request = request("lookup_verification", common, record_id=item["id"])
    with db.connect() as conn:
        before = verification.handle(db, conn, before_request)
    before_fingerprint = before["input_fingerprint"]

    passed, code = invoke(db, request("record_verification", {
        **common, "outcome": "pass", "exit_code": 0, "evidence_ids": [artifact_id],
        "before_fingerprint": before_fingerprint,
    }, record_id=item["id"]), verification.handle)
    assert code == 0 and passed["ok"]
    pass_id = passed["result"]["verification_id"]
    with db.connect() as conn:
        pass_row = conn.execute("SELECT rowid,completed_at FROM verifications WHERE id=?", (pass_id,)).fetchone()
    pass_time = pass_row["completed_at"]
    failure_time = pass_time
    if clock_mode == "clock_back":
        parsed = datetime.fromisoformat(pass_time.replace("Z", "+00:00"))
        failure_time = (parsed - timedelta(seconds=1)).isoformat(timespec="microseconds").replace("+00:00", "Z")
    monkeypatch.setattr(verification, "utc_now", lambda: failure_time)
    failed, code = invoke(db, request("record_verification", {
        **common, "outcome": "fail", "exit_code": 1, "evidence_ids": [],
        "before_fingerprint": before_fingerprint,
    }, record_id=item["id"]), verification.handle)
    assert code == 0 and failed["ok"]
    with db.connect() as conn:
        failure_row = conn.execute("SELECT rowid,completed_at FROM verifications WHERE id=?",
                                   (failed["result"]["verification_id"],)).fetchone()
        lookup = verification.handle(db, conn, request("lookup_verification", common, record_id=item["id"]))
    assert failure_row["rowid"] > pass_row["rowid"]
    assert failure_row["completed_at"] <= pass_row["completed_at"]
    assert lookup["reusable"] is False
    assert lookup["status"] == "stale"
    assert "later_nonpass_verification" in lookup["reasons"]


@pytest.mark.parametrize("root_source", ["arguments", "environment", "platform_defaults"])
def test_explicit_and_configured_roots_do_not_require_path_home(tmp_path, monkeypatch, root_source):
    data, config = tmp_path / "explicit-data", tmp_path / "explicit-config"
    monkeypatch.setenv("PMT_DATA_ROOT", str(data))
    monkeypatch.setenv("PMT_CONFIG_ROOT", str(config))
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
        monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))

    def forbidden_home(_cls):
        raise AssertionError("Path.home was consulted despite configured roots")

    monkeypatch.setattr(Path, "home", classmethod(forbidden_home))
    if root_source == "arguments":
        db = Database(data, config)
        assert db.root == data.resolve() and db.config_root == config.resolve()
    elif root_source == "environment":
        db = Database()
        assert db.root == data.resolve() and db.config_root == config.resolve()
    else:
        default_data, default_config = default_roots()
        if os.name == "nt":
            assert default_data == (tmp_path / "local" / "pmt-v3")
            assert default_config == (tmp_path / "roaming" / "pmt-v3")
        else:
            assert default_data == (tmp_path / "xdg-data" / "pmt-v3")
            assert default_config == (tmp_path / "xdg-config" / "pmt-v3")
