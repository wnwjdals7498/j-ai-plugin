"""Observable full workflow using only the public subprocess protocol."""
import json
import subprocess
import sys
import time


def test_done_02_full_workflow_and_new_session_context(cli, request_factory, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "app.py").write_text("assert 2 + 2 == 4\n", encoding="utf-8")
    evidence = tmp_path / "test-output.txt"

    def call(op, payload=None, **kwargs):
        request = request_factory(op, payload or {}, **kwargs)
        for attempt in range(3):
            proc = cli.call(request)
            value = json.loads(proc.stdout)
            if proc.returncode != 4 or not (value.get("error") or {}).get("retryable"):
                break
            time.sleep(0.05 * (attempt + 1))
        assert proc.returncode == 0 and value["ok"], value
        return value["result"]

    scope = call("create_scope", {"kind": "project", "slug": "full-flow"})["scope_id"]
    item = call("save_change", {"kind": "item", "title": "Complete with measured evidence",
                "reason": "new task", "body": {"criteria": ["C1"], "workspace": str(workspace),
                "next": "execute the measured check"}}, scope_id=scope)
    decision = call("save_decision", {"decision_kind": "custom", "decider": "user",
                    "content": "Use a measured subprocess test", "reason": "explicit selection",
                    "confirmation_source": "user_selected"}, record_id=item["id"], expected_revision=item["revision"])
    claim = call("claim_task", record_id=item["id"], expected_revision=decision["revision"])
    before = call("lookup_verification", {"definition_id": "app-test", "definition_version": "1",
                   "command": [sys.executable, "app.py"]}, record_id=item["id"])
    measured = subprocess.run([sys.executable, str(workspace / "app.py")], capture_output=True)
    assert measured.returncode == 0
    evidence.write_text("app.py executed: exit=0\n", encoding="utf-8")
    artifact = call("register_resource", {"source_path": str(evidence), "allowed_root": str(tmp_path),
                    "retention": "evidence", "owner_record_id": item["id"]}, scope_id=scope)
    verification = call("record_verification", {"definition_id": "app-test", "definition_version": "1",
                        "command": [sys.executable, "app.py"], "outcome": "pass", "exit_code": measured.returncode,
                        "before_fingerprint": before["input_fingerprint"],
                        "criterion_ids": ["C1"], "evidence_ids": [artifact["artifact_id"]]}, record_id=item["id"])
    finished = call("finish_task", {"result": "Measured subprocess check passed", "claim_token": claim["claim_token"],
                    "verification_ids": [verification["verification_id"]]}, record_id=item["id"], expected_revision=claim["revision"])
    assert finished["state"] == "Done"
    context = call("read_context", scope_id=scope, session_id="another-real-process-session")
    assert any(record["record_id"] == item["id"] and record["state"] == "Done" for record in context["records"])
    assert context["current_decisions"][0]["body"]["content"] == "Use a measured subprocess test"
    assert "Use a measured subprocess test" in context["context_markdown"]


def test_work_child_progress_is_derived_without_automatic_parent_completion(cli, request_factory):
    def call(op, payload=None, **kwargs):
        process = cli.call(request_factory(op, payload or {}, **kwargs))
        value = json.loads(process.stdout)
        assert process.returncode == 0 and value["ok"], value
        return value["result"]
    scope = call("create_scope", {"kind": "project", "slug": "aggregate"})["scope_id"]
    parent = call("save_change", {"kind": "work", "title": "Parent", "reason": "fixture"}, scope_id=scope)
    child = call("save_change", {"kind": "item", "title": "Child", "reason": "fixture",
                 "parent_id": parent["id"], "body": {"criteria": ["C1"]}}, scope_id=scope)
    call("claim_task", record_id=child["id"], expected_revision=child["revision"])
    context = call("read_context", scope_id=scope)
    displayed = next(record for record in context["records"] if record["record_id"] == parent["id"])
    assert displayed["state"] == "Planned" and displayed["aggregate_state"] == "In Progress"
