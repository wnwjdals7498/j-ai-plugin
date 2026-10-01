"""READ-01/02: context boundaries, stable pagination, and structural budget."""
import json

import pytest

from pmt.db import Database
from pmt.queries import handle
from pmt.util import new_id, utc_now


@pytest.fixture
def query_db(tmp_path):
    db = Database(tmp_path / "data", tmp_path / "config")
    workspace = tmp_path / "repo workspace 한글"
    workspace.mkdir()
    now = utc_now()
    with db.write() as conn:
        scope_a, scope_b = new_id(), new_id()
        for scope_id, slug in ((scope_a, "project-a"), (scope_b, "project-b")):
            conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                         (scope_id, "project", None, slug, "{}", now, now))
        records = {}
        for index, (kind, title, body, parent_id) in enumerate([
            ("work", "Work", {"next": "작업 요약"}, None),
            ("item", "Alpha needle", {"criteria": ["C1"], "next": "다음 단계", "watch": "중요 주의"}, None),
            ("decision", "Current decision", {"watch": "핵심 결정 경고"}, None),
            ("item", "Child", {"next": "자식 작업"}, None),
        ]):
            record_id = new_id()
            records[title] = record_id
            conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (record_id, kind, scope_a, parent_id, title, "Planned", json.dumps(body, ensure_ascii=False), 1,
                          now, f"2026-10-01T00:00:0{index}Z"))
        foreign_id = new_id()
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (foreign_id, "item", scope_b, "Out of scope needle", "Planned", "{}", 1, now, now))
    return db, scope_a, scope_b, records, workspace


def request(operation, *, scope_id=None, record_id=None, payload=None):
    value = {"protocol_version": 1, "operation": operation, "request_id": new_id(),
             "actor": "main", "session_id": "test-main", "payload": payload or {}}
    if scope_id:
        value["scope_id"] = scope_id
    if record_id:
        value["record_id"] = record_id
    return value


def test_read_01_scoped_records_decisions_next_watch_and_cursor(query_db):
    db, scope_a, scope_b, records, _ = query_db
    first_request = request("read_context", scope_id=scope_a, payload={"limit": 2})
    first, code = db.run_request(first_request, lambda conn, req: handle(db, conn, req))
    assert code == 0 and first["ok"]
    result = first["result"]
    assert len(result["records"]) == 2
    assert result["next_cursor"]
    assert all(row["scope_id"] == scope_a for row in result["records"])

    second_request = request("read_context", scope_id=scope_a,
                             payload={"limit": 2, "cursor": result["next_cursor"]})
    second, code = db.run_request(second_request, lambda conn, req: handle(db, conn, req))
    assert code == 0 and second["ok"]
    assert not ({x["record_id"] for x in result["records"]} & {x["record_id"] for x in second["result"]["records"]})
    assert {x["record_id"] for x in result["records"] + second["result"]["records"]} == set(records.values())
    assert any(x["record_id"] == records["Current decision"] for x in second["result"]["current_decisions"])
    assert result["next_actions"] or second["result"]["next_actions"]
    assert result["watch"] or second["result"]["watch"]
    query_req = request("read_context", scope_id=scope_a, payload={"query": "needle"})
    query_resp, code = db.run_request(query_req, lambda conn, req: handle(db, conn, req))
    assert code == 0
    assert [r["title"] for r in query_resp["result"]["records"]] == ["Alpha needle"]
    assert scope_b not in [r["scope_id"] for r in query_resp["result"]["records"]]


def test_read_01_record_selection_includes_only_same_scope_descendants(query_db):
    db, scope_a, _, records, _ = query_db
    # Add a same-scope child under a selected record. The selection never pulls another scope.
    child = new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("UPDATE records SET kind='work' WHERE id=?", (records["Work"],))
        conn.execute("UPDATE records SET parent_id=? WHERE id=?", (records["Work"], records["Alpha needle"]))
    req = request("read_context", scope_id=scope_a, record_id=records["Work"])
    response, code = db.run_request(req, lambda conn, value: handle(db, conn, value))
    assert code == 0
    assert {row["record_id"] for row in response["result"]["records"]} == {records["Work"], records["Alpha needle"]}


def test_read_01_scope_includes_descendant_projects_but_not_siblings(query_db):
    db, root_scope, sibling_scope, records, _ = query_db
    child_scope, child_record = new_id(), new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                     (child_scope, "classification", root_scope, "child-class", "{}", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (child_record, "item", child_scope, "descendant record", "Planned", "{}", 1, now, now))
    response, code = db.run_request(request("read_context", scope_id=root_scope),
                                    lambda conn, value: handle(db, conn, value))
    assert code == 0
    ids = {row["record_id"] for row in response["result"]["records"]}
    assert child_record in ids
    assert records["Work"] in ids
    assert sibling_scope not in {row["scope_id"] for row in response["result"]["records"]}


def test_read_02_summary_is_bounded_and_keeps_current_warning(query_db):
    db, scope_id, _, _, _ = query_db
    now = utc_now()
    with db.write() as conn:
        for index in range(24):
            body = {"next": f"다음 {index} " + "N" * 450,
                    "watch": f"주의 {index} " + "W" * 450,
                    "criteria": [f"C{index}"]}
            conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                         (new_id(), "item", scope_id, f"Work {index:02d}", "Planned",
                          json.dumps(body, ensure_ascii=False), 1, now, f"2026-10-01T01:{index:02d}:00Z"))
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (new_id(), "decision", scope_id, "Critical current decision", "Accepted",
                      json.dumps({"watch": "핵심 경고: 데이터 손실을 막고 확인할 것"}, ensure_ascii=False),
                      1, now, "2026-10-01T00:00:00Z"))
    req = request("read_context", scope_id=scope_id, payload={"limit": 20, "budget": 4500})
    response, code = db.run_request(req, lambda conn, value: handle(db, conn, value))
    assert code == 0
    result = response["result"]
    assert len(result["context_markdown"]) <= 4500
    assert "핵심 경고" in result["context_markdown"]
    assert result["truncated"] is True
    assert result["next_cursor"]
    assert any(item["title"] == "Critical current decision" for item in result["current_decisions"])


def test_read_invalid_cursor_and_cross_scope_record_rejected(query_db):
    db, scope_a, scope_b, records, _ = query_db
    invalid_cursor = request("read_context", scope_id=scope_a, payload={"cursor": "not-a-cursor"})
    response, code = db.run_request(invalid_cursor, lambda conn, value: handle(db, conn, value))
    assert code == 2 and response["ok"] is False
    foreign = new_id()
    now = utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,title,state,body_json,revision,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                     (foreign, "item", scope_b, "other", "Planned", "{}", 1, now, now))
    req = request("read_context", scope_id=scope_a, record_id=foreign)
    response, code = db.run_request(req, lambda conn, value: handle(db, conn, value))
    assert code == 2 and response["ok"] is False
