"""Directive privacy, exploration bootstrap, hierarchy and version guards."""
import json
import uuid

from pmt.db import Database
from pmt.service import execute


def req(op, payload=None, **extra):
    return {"protocol_version": 1, "operation": op, "request_id": str(uuid.uuid4()),
            "actor": "main", "session_id": "main", "payload": payload or {}, **extra}


def directive():
    return {"purpose": "Find a verified approach", "goal": "Return tested alternatives", "non_goal": ["production deployment"],
            "change_scope": {"add": ["experiment"], "modify": [], "delete": [], "forbidden": ["shared configuration"]},
            "inputs": [{"name": "question", "meaning": "Unresolved requirement"}],
            "outputs": [{"name": "options", "meaning": "Methods with source and actual evidence"}],
            "method": {"summary": "Run a bounded experiment", "evidence_refs": []},
            "tests": [{"id": "research", "method": "Observe actual process output"}],
            "logging": ["start, exit and evidence reference"], "context_refs": []}


def setup(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    project = execute(db, req("create_scope", {"kind": "project", "slug": "test"}))[0]["result"]["scope_id"]
    work = execute(db, req("save_change", {"kind": "work", "title": "research", "reason": "test"}, scope_id=project))[0]["result"]["record_id"]
    item = execute(db, req("save_change", {"kind": "item", "title": "question", "parent_id": work,
                                          "body": {"criteria": ["research"]}, "reason": "test"}, scope_id=project))[0]["result"]["record_id"]
    return db, project, work, item


def save_request(item, workspace):
    return req("save_step_directive", {"item_id": item, "title": "bounded research", "directive": directive(),
        "kind": "investigate", "exploration_approved": True, "product_stage": "prototype",
        "requirements_version": "1", "plan_version": "draft1", "workspace": str(workspace),
        "scopes": [{"kind": "path", "workspace": str(workspace), "resource": "."}], "criteria": ["research"]})


def test_exploration_bootstrap_private_resource_and_replay(tmp_path):
    db, project, work, item = setup(tmp_path)
    request = save_request(item, tmp_path)
    result, code = execute(db, request)
    assert code == 0, result
    step = result["result"]
    again, code = execute(db, request)
    assert code == 0 and again == result
    read, code = execute(db, req("read_step", {"step_id": step["step_id"]}))
    assert code == 0 and "directive" not in read["result"]
    context, code = execute(db, req("read_context", {}, scope_id=project))
    assert code == 0 and "Find a verified approach" not in json.dumps(context)
    denied, code = execute(db, req("read_step_directive", {"step_id": step["step_id"]}))
    assert code != 0
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM step_specs").fetchone()[0] == 1
        assert "Find a verified approach" not in conn.execute("SELECT body_json FROM records WHERE id=?", (step["step_id"],)).fetchone()[0]


def test_implementation_cannot_start_from_unapproved_draft(tmp_path):
    db, _, _, item = setup(tmp_path)
    request = save_request(item, tmp_path)
    request["payload"].update(kind="implement", exploration_approved=False)
    result, code = execute(db, request)
    assert code != 0 and result["error"]["code"] == "plan_not_confirmed"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM step_specs").fetchone()[0] == 0


def test_version_cas_and_hierarchy_tags(tmp_path):
    db, _, work, item = setup(tmp_path)
    made, code = execute(db, save_request(item, tmp_path))
    step = made["result"]
    update = save_request(item, tmp_path)
    update.update(record_id=step["step_id"], expected_revision=1)
    updated, code = execute(db, update)
    assert code == 0 and updated["result"]["directive_version"] == 2
    stale = save_request(item, tmp_path)
    stale.update(record_id=step["step_id"], expected_revision=1)
    assert execute(db, stale)[1] == 3
    invalid, code = execute(db, req("set_task_metadata", {"kind_tag": "functional"}, record_id=work, expected_revision=1))
    assert code != 0 and invalid["error"]["code"] == "invalid_tag"
    assert execute(db, req("set_task_metadata", {"kind_tag": "research", "priority": 1}, record_id=work, expected_revision=1))[1] != 0


def test_branch_invalidation_preserves_other_steps(tmp_path):
    db, _, _, item = setup(tmp_path)
    first = execute(db, save_request(item, tmp_path))[0]["result"]["step_id"]
    second = execute(db, save_request(item, tmp_path))[0]["result"]["step_id"]
    result, code = execute(db, req("invalidate_plan_branch", {"step_ids": [first], "reason": "premise changed"}))
    assert code == 0, result
    with db.connect() as conn:
        assert json.loads(conn.execute("SELECT body_json FROM records WHERE id=?", (first,)).fetchone()[0])["invalidated"]
        assert not json.loads(conn.execute("SELECT body_json FROM records WHERE id=?", (second,)).fetchone()[0])["invalidated"]


def test_parent_cannot_cancel_until_child_step_is_explicitly_closed(tmp_path):
    db, _, _, item = setup(tmp_path)
    step = execute(db, save_request(item, tmp_path))[0]["result"]["step_id"]
    parent_cancel = req("save_change", {"status": "Canceled", "reason": "request withdrawn"}, record_id=item, expected_revision=1)
    assert execute(db, parent_cancel)[1] != 0
    response, code = execute(db, req("cancel_step", {"step_id": step, "reason": "never started"}, record_id=step, expected_revision=1))
    assert code == 0 and response["result"]["state"] == "Canceled"
    response, code = execute(db, req("save_change", {"status": "Canceled", "reason": "request withdrawn"}, record_id=item, expected_revision=1))
    assert code == 0, response


def test_classified_item_uses_project_plan_and_inherits_work_priority(tmp_path):
    from pmt.phase2_common import persist_json_resource
    from pmt.util import utc_now
    db, project, _, _ = setup(tmp_path)
    classification = execute(db, req("create_scope", {"kind": "classification", "parent_id": project, "slug": "storage"}))[0]["result"]["scope_id"]
    work = execute(db, req("save_change", {"kind": "work", "title": "classified work", "reason": "test", "body": {"priority": 8}}, scope_id=classification))[0]["result"]["record_id"]
    item = execute(db, req("save_change", {"kind": "item", "title": "classified item", "parent_id": work, "reason": "test"}, scope_id=classification))[0]["result"]["record_id"]
    artifact = persist_json_resource(db, req("test"), {"approved": "test plan metadata"}, project, "test")
    plan_id = str(uuid.uuid4())
    with db.write() as conn:
        conn.execute("INSERT INTO plans VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (plan_id, project, artifact["artifact_id"], 1,
                     "1", "1", "published", str(tmp_path), "docs/pmt-docs/plan.graph.json", artifact["sha256"], None, utc_now(), utc_now()))
    request = save_request(item, tmp_path)
    request["payload"].update(kind="implement", exploration_approved=False, plan_id=plan_id, plan_version="1")
    created, code = execute(db, request)
    assert code == 0, created
    step = created["result"]["step_id"]
    read, code = execute(db, req("read_step", {"step_id": step}))
    assert code == 0 and read["result"]["metadata"]["priority"] == 8
