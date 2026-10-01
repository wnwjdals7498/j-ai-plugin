"""VERIFY-01/02/03 against a real temporary workspace and SQLite database."""
import hashlib
import json
import pytest

from pmt.db import Database
from pmt.errors import PmtError
from pmt.util import new_id, utc_now
from pmt.verification import handle, verify_completion


@pytest.fixture
def verification_case(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    workspace = tmp_path / "workspace with spaces 한글"
    workspace.mkdir()
    (workspace / "tracked.py").write_text("VALUE = 1\n", encoding="utf-8")
    (workspace / "untracked.fixture").write_text("fixture input\n", encoding="utf-8")
    scope_id, record_id = new_id(), new_id()
    now = utc_now()
    body = {"criteria": ["C1", "C2"], "workspace": str(workspace), "next": "test"}
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (scope_id, "project", None, "verify-project", "{}", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                     (record_id, "item", scope_id, None, "verify item", "Planned",
                      json.dumps(body, ensure_ascii=False), 1, now, now))
    return db, workspace, scope_id, record_id


def req(operation, payload, record_id=None):
    request = {"protocol_version": 1, "operation": operation, "request_id": new_id(),
               "actor": "main", "session_id": "test-main", "payload": payload}
    if record_id:
        request["record_id"] = record_id
    return request


def record(db, payload, record_id):
    payload = dict(payload)
    if payload.get("outcome") == "pass" and "before_fingerprint" not in payload:
        before_request = req("lookup_verification", {
            key: payload[key] for key in ("definition_id", "definition_version", "target_id", "command", "inputs", "input", "criterion_ids") if key in payload
        }, record_id)
        with db.connect() as conn:
            before = handle(db, conn, before_request)
        payload["before_fingerprint"] = before["input_fingerprint"]
    return db.run_request(req("record_verification", payload, record_id),
                          lambda conn, request: handle(db, conn, request))


def lookup(db, record_id, *, command=None, criterion_ids=None):
    payload = {
        "definition_id": "test-suite", "definition_version": "1", "target_id": record_id,
        "command": command or ["python", "-m", "pytest"],
    }
    if criterion_ids is not None:
        payload["criterion_ids"] = criterion_ids
    return db.run_request(req("lookup_verification", payload, record_id),
                          lambda conn, request: handle(db, conn, request))


def _item(db, scope_id, workspace, criteria=("C1", "C2"), title="reuse candidate"):
    record_id = new_id()
    now = utc_now()
    body = {"criteria": list(criteria), "workspace": str(workspace)}
    with db.write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
                     "VALUES(?,'item',?,NULL,?,'Planned',?,1,?,?)",
                     (record_id, scope_id, title, json.dumps(body, ensure_ascii=False), now, now))
    return record_id


def _project(db, slug):
    scope_id = new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,'project',NULL,?,'{}',?,?)",
                     (scope_id, slug, now, now))
    return scope_id


def test_verify_02_actual_dirty_and_untracked_workspace_changes_cannot_reuse(verification_case):
    db, workspace, _, record_id = verification_case
    payload = {"definition_id": "test-suite", "definition_version": "1", "target_id": record_id,
               "command": ["python", "-m", "pytest"], "outcome": "fail", "exit_code": 1,
               "evidence_ids": [], "criterion_ids": ["C1"],
               "input_fingerprint": "caller-claims-this-is-current"}
    saved, code = record(db, payload, record_id)
    assert code == 0 and saved["ok"]
    assert saved["result"]["provided_input_fingerprint_used_as_authority"] is False
    initial = saved["result"]["input_fingerprint"]
    check, code = lookup(db, record_id)
    assert code == 0 and check["ok"]
    assert check["result"]["reusable"] is False
    assert check["result"]["input_fingerprint"] == initial

    # An untracked/ignored file is still part of the observed workspace input.
    (workspace / "ignored-or-untracked.txt").write_text("changed input\n", encoding="utf-8")
    changed, code = lookup(db, record_id)
    assert code == 0
    assert changed["result"]["input_fingerprint"] != initial
    assert changed["result"]["reusable"] is False


def test_verify_02_missing_workspace_makes_pass_impossible(verification_case, tmp_path):
    db, workspace, scope_id, record_id = verification_case
    import shutil
    shutil.rmtree(workspace)
    saved, code = record(db, {"definition_id": "test-suite", "target_id": record_id,
                              "command": "pytest", "outcome": "pass", "exit_code": 0,
                              "evidence_ids": [new_id()], "criterion_ids": ["C1", "C2"]}, record_id)
    assert code == 2 and saved["ok"] is False
    assert saved["error"]["code"] == "verification_fingerprint_unknown"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM verifications").fetchone()[0] == 0


def test_verify_01_missing_evidence_never_accepts_pass(verification_case):
    db, _, _, record_id = verification_case
    saved, code = record(db, {"definition_id": "test-suite", "target_id": record_id,
                              "command": "pytest", "outcome": "pass", "exit_code": 0,
                              "evidence_ids": [new_id()], "criterion_ids": ["C1", "C2"]}, record_id)
    assert code == 2 and saved["ok"] is False
    assert saved["error"]["code"] == "verification_evidence_invalid"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM verifications").fetchone()[0] == 0


def _ready_evidence(db, scope_id):
    """Build an artifact row/file using the P1 relative-path contract."""
    content = b"verified evidence bytes\n"
    artifact_id = new_id()
    relative = f"resources/{artifact_id}.txt"
    target = db.root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO artifacts(id,scope_id,sha256,size_bytes,relative_path,state,created_at) VALUES(?,?,?,?,?,?,?)",
                     (artifact_id, scope_id, digest, len(content), relative, "ready", now))
    return artifact_id


def test_verify_01_matching_snapshot_and_real_ready_artifact_is_reusable(verification_case):
    pytest.importorskip("pmt.resources")
    db, _, scope_id, record_id = verification_case
    artifact_id = _ready_evidence(db, scope_id)
    saved, code = record(db, {"definition_id": "test-suite", "definition_version": "1",
                              "target_id": record_id, "command": ["python", "-m", "pytest"],
                              "outcome": "pass", "exit_code": 0, "evidence_ids": [artifact_id],
                              "criterion_ids": ["C1", "C2"]}, record_id)
    assert code == 0 and saved["ok"]
    candidate, code = lookup(db, record_id, criterion_ids=["C1"])
    assert code == 0 and candidate["result"]["reusable"] is True
    assert candidate["result"]["verification_id"] == saved["result"]["verification_id"]
    with db.connect() as conn:
        saved_row = conn.execute("SELECT target_id,command_json FROM verifications WHERE id=?",
                                 (saved["result"]["verification_id"],)).fetchone()
        row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        completion = verify_completion(db, conn, row, [saved["result"]["verification_id"]])
    assert saved_row["target_id"] == record_id
    assert "target_id" not in json.loads(saved_row["command_json"])["snapshot"]
    assert json.loads(saved_row["command_json"])["snapshot"]["verification_scope_id"] == scope_id
    assert completion["valid"] is True
    assert completion["covered_criteria"] == ["C1", "C2"]
    assert completion["evidence_ids"] == [artifact_id]
    # The exact snapshot belongs to the workspace/scope, not to the source Item UUID.
    other_item = _item(db, scope_id, verification_case[1])
    candidate, code = lookup(db, other_item)
    assert code == 0 and candidate["result"]["reusable"] is True
    assert candidate["result"]["verification_id"] == saved["result"]["verification_id"]
    with db.connect() as conn:
        target = conn.execute("SELECT * FROM records WHERE id=?", (other_item,)).fetchone()
        completion = verify_completion(db, conn, target, [saved["result"]["verification_id"]])
    assert completion["valid"] is True and completion["covered_criteria"] == ["C1", "C2"]


def test_verify_01_reuse_is_limited_by_scope_workspace_criteria_command_and_environment(verification_case, tmp_path):
    pytest.importorskip("pmt.resources")
    db, workspace, scope_id, record_id = verification_case
    artifact_id = _ready_evidence(db, scope_id)
    saved, code = record(db, {"definition_id": "test-suite", "definition_version": "1",
                              "target_id": record_id, "command": ["python", "-m", "pytest"],
                              "outcome": "pass", "exit_code": 0, "evidence_ids": [artifact_id],
                              "criterion_ids": ["C1"]}, record_id)
    assert code == 0
    verification_id = saved["result"]["verification_id"]

    same_snapshot = _item(db, scope_id, workspace, criteria=("C1", "C2"))
    found, code = lookup(db, same_snapshot, criterion_ids=["C1"])
    assert code == 0 and found["result"]["reusable"] is True
    assert found["result"]["criterion_ids"] == ["C1"]

    other_workspace = tmp_path / "different workspace same bytes"
    other_workspace.mkdir()
    for path in workspace.iterdir():
        if path.is_file():
            (other_workspace / path.name).write_bytes(path.read_bytes())
    different_workspace_item = _item(db, scope_id, other_workspace)
    found, code = lookup(db, different_workspace_item)
    assert code == 0 and found["result"]["reusable"] is False
    assert "current_fingerprint_mismatch" in found["result"]["reasons"]

    different_criteria = _item(db, scope_id, workspace, criteria=("C1", "C2"))
    with db.write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (json.dumps({"criteria": [{"id": "C1", "description": "changed meaning"}, "C2"],
                                  "workspace": str(workspace)}), different_criteria))
    found, code = lookup(db, different_criteria)
    assert code == 0 and found["result"]["reusable"] is False

    found, code = lookup(db, same_snapshot, command=["python", "-m", "pytest", "-q"])
    assert code == 0 and found["result"]["reusable"] is False

    another_scope = _project(db, "separate verification scope")
    another_item = _item(db, another_scope, workspace)
    found, code = lookup(db, another_item)
    assert code == 0 and found["result"]["reusable"] is False
    assert found["result"]["status"] == "not_found"

    # A second profile over the same SQLite image is a different environment.
    other = Database(tmp_path / "other-data", tmp_path / "other-config")
    with db.connect() as source, other.connect() as target:
        source.backup(target)
    found, code = lookup(other, same_snapshot)
    assert code == 0 and found["result"]["reusable"] is False


def test_verify_01_subset_passes_union_to_satisfy_all_finish_criteria(verification_case):
    pytest.importorskip("pmt.resources")
    db, _, scope_id, record_id = verification_case
    first_evidence = _ready_evidence(db, scope_id)
    second_evidence = _ready_evidence(db, scope_id)
    base = {"definition_id": "test-suite", "definition_version": "1", "target_id": record_id,
            "command": ["python", "-m", "pytest"], "outcome": "pass", "exit_code": 0}
    first, code = record(db, {**base, "evidence_ids": [first_evidence], "criterion_ids": ["C1"]}, record_id)
    assert code == 0
    second, code = record(db, {**base, "evidence_ids": [second_evidence], "criterion_ids": ["C2"]}, record_id)
    assert code == 0
    with db.connect() as conn:
        target = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        only_first = verify_completion(db, conn, target, [first["result"]["verification_id"]])
        combined = verify_completion(db, conn, target,
                                     [first["result"]["verification_id"], second["result"]["verification_id"]])
    assert only_first["valid"] is False
    assert combined["valid"] is True and combined["covered_criteria"] == ["C1", "C2"]
    assert set(combined["evidence_ids"]) == {first_evidence, second_evidence}


def test_verify_01_id_only_criterion_object_matches_its_canonical_string_form(verification_case):
    pytest.importorskip("pmt.resources")
    db, workspace, scope_id, record_id = verification_case
    artifact_id = _ready_evidence(db, scope_id)
    with db.write() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (json.dumps({"criteria": [{"id": "C1"}, "C2"], "workspace": str(workspace)}), record_id))
    saved, code = record(db, {"definition_id": "test-suite", "target_id": record_id,
                              "command": ["python", "-m", "pytest"], "outcome": "pass", "exit_code": 0,
                              "evidence_ids": [artifact_id], "criterion_ids": ["C1"]}, record_id)
    assert code == 0
    canonical = _item(db, scope_id, workspace, criteria=("C1", "C2"))
    candidate, code = lookup(db, canonical, criterion_ids=["C1"])
    assert code == 0 and candidate["result"]["reusable"] is True
    assert candidate["result"]["verification_id"] == saved["result"]["verification_id"]


def test_verify_02_content_command_and_criteria_changes_stale_success(verification_case):
    pytest.importorskip("pmt.resources")
    db, workspace, _, record_id = verification_case
    artifact_id = _ready_evidence(db, verification_case[2])
    saved, code = record(db, {"definition_id": "test-suite", "target_id": record_id,
                              "command": ["python", "-m", "pytest"], "outcome": "pass", "exit_code": 0,
                              "evidence_ids": [artifact_id], "criterion_ids": ["C1", "C2"]}, record_id)
    assert code == 0
    verification_id = saved["result"]["verification_id"]
    different_command, code = lookup(db, record_id, command=["python", "-m", "pytest", "-q"])
    assert code == 0 and different_command["result"]["reusable"] is False
    (workspace / "untracked.fixture").write_text("different test input\n", encoding="utf-8")
    candidate, code = lookup(db, record_id, criterion_ids=["C1"])
    assert code == 0 and candidate["result"]["reusable"] is False
    assert "current_fingerprint_mismatch" in candidate["result"]["reasons"]
    with db.connect() as conn:
        conn.execute("UPDATE records SET body_json=? WHERE id=?",
                     (json.dumps({"criteria": ["C1", "C2", "C3"], "workspace": str(workspace)}), record_id))
        row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        completion = verify_completion(db, conn, row, [verification_id])
    assert completion["valid"] is False
    assert any("C3" in reason for reason in completion["reasons"])


def test_verify_03_later_failure_or_corrupt_evidence_invalidates_prior_pass(verification_case):
    pytest.importorskip("pmt.resources")
    db, workspace, scope_id, record_id = verification_case
    artifact_id = _ready_evidence(db, scope_id)
    base = {"definition_id": "test-suite", "target_id": record_id,
            "command": ["python", "-m", "pytest"], "exit_code": 0,
            "evidence_ids": [artifact_id], "criterion_ids": ["C1", "C2"]}
    saved, code = record(db, {**base, "outcome": "pass"}, record_id)
    assert code == 0
    verify_id = saved["result"]["verification_id"]
    same_scope_other_item = _item(db, scope_id, workspace)
    failed, code = record(db, {**base, "target_id": same_scope_other_item, "outcome": "fail",
                               "exit_code": 1, "evidence_ids": []}, same_scope_other_item)
    assert code == 0
    candidate, code = lookup(db, record_id, criterion_ids=["C1"])
    assert code == 0 and candidate["result"]["reusable"] is False
    assert "later_nonpass_verification" in candidate["result"]["reasons"]
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        completion = verify_completion(db, conn, row, [verify_id])
    assert completion["valid"] is False


def test_verify_03_failure_in_an_independent_scope_does_not_stale_candidate(verification_case):
    pytest.importorskip("pmt.resources")
    db, workspace, scope_id, record_id = verification_case
    artifact_id = _ready_evidence(db, scope_id)
    passed, code = record(db, {"definition_id": "test-suite", "definition_version": "1",
                               "target_id": record_id, "command": ["python", "-m", "pytest"],
                               "outcome": "pass", "exit_code": 0, "evidence_ids": [artifact_id],
                               "criterion_ids": ["C1"]}, record_id)
    assert code == 0
    independent_scope = _project(db, "independent"); other = _item(db, independent_scope, workspace)
    failed, code = record(db, {"definition_id": "test-suite", "definition_version": "1",
                               "target_id": other, "command": ["python", "-m", "pytest"],
                               "outcome": "fail", "exit_code": 1, "evidence_ids": [],
                               "criterion_ids": ["C1"]}, other)
    assert code == 0
    candidate, code = lookup(db, record_id, criterion_ids=["C1"])
    assert code == 0 and candidate["result"]["reusable"] is True
    assert candidate["result"]["verification_id"] == passed["result"]["verification_id"]


def test_verify_03_evidence_hash_change_invalidates_prior_pass(verification_case):
    pytest.importorskip("pmt.resources")
    db, _, scope_id, record_id = verification_case
    artifact_id = _ready_evidence(db, scope_id)
    saved, code = record(db, {"definition_id": "test-suite", "target_id": record_id,
                              "command": ["python", "-m", "pytest"], "outcome": "pass", "exit_code": 0,
                              "evidence_ids": [artifact_id], "criterion_ids": ["C1", "C2"]}, record_id)
    assert code == 0
    verify_id = saved["result"]["verification_id"]
    with db.connect() as conn:
        relative = conn.execute("SELECT relative_path FROM artifacts WHERE id=?", (artifact_id,)).fetchone()[0]
    (db.root / relative).write_bytes(b"tampered evidence")
    candidate, code = lookup(db, record_id)
    assert code == 0 and candidate["result"]["reusable"] is False
    assert any(artifact_id in reason and "corrupt" in reason for reason in candidate["result"]["reasons"])
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        completion = verify_completion(db, conn, row, [verify_id])
    assert completion["valid"] is False


def test_verify_02_before_snapshot_is_required_and_execution_change_refuses_pass(verification_case):
    db, workspace, scope_id, record_id = verification_case
    evidence_id = _ready_evidence(db, scope_id)
    payload = {"definition_id": "test-suite", "target_id": record_id,
               "command": ["python", "-m", "pytest"], "outcome": "pass", "exit_code": 0,
               "evidence_ids": [evidence_id], "criterion_ids": ["C1"]}
    missing, code = db.run_request(req("record_verification", payload, record_id),
                                  lambda conn, request: handle(db, conn, request))
    assert code == 2 and missing["error"]["code"] == "verification_before_snapshot_required"
    before, code = lookup(db, record_id)
    assert code == 0
    (workspace / "tracked.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed, code = record(db, {**payload, "before_fingerprint": before["result"]["input_fingerprint"]}, record_id)
    assert code == 2 and changed["error"]["code"] == "verification_changed_during_execution"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM verifications").fetchone()[0] == 0


@pytest.mark.parametrize("change", ["configuration", "dependency", "inputs", "runtime"])
def test_verify_02_configuration_dependency_inputs_runtime_change_invalidates_pass(verification_case, change, monkeypatch):
    db, workspace, scope_id, record_id = verification_case
    evidence_id = _ready_evidence(db, scope_id)
    payload = {"definition_id": "test-suite", "target_id": record_id,
               "command": ["python", "-m", "pytest"], "outcome": "pass", "exit_code": 0,
               "evidence_ids": [evidence_id], "criterion_ids": ["C1"]}
    passed, code = record(db, payload, record_id)
    assert code == 0
    if change == "configuration":
        (db.config_root / "test-settings.json").write_text('{"mode":"different"}', encoding="utf-8")
    elif change == "dependency":
        (workspace / "requirements.lock").write_text("different-package==2\n", encoding="utf-8")
    elif change == "runtime":
        # Exercise the exact runtime-version comparison without changing the host runtime.
        monkeypatch.setattr("pmt.verification.platform.python_version", lambda: "99.1.0")
    lookup_payload = {key: payload[key] for key in ("definition_id", "target_id", "command", "criterion_ids")}
    if change == "inputs":
        lookup_payload["inputs"] = {"fixture": "different"}
    with db.connect() as conn:
        candidate = handle(db, conn, req("lookup_verification", lookup_payload, record_id))
    assert candidate["reusable"] is False and candidate["status"] == "stale"
