"""FB 최종 리뷰 수정 회귀 테스트."""
import json

import pytest

from docdb import db, profile, relations, runs
from docdb.actors import Actor
from docdb.errors import DocdbError
from docdb.llm import DocDB

_BASE_PAYLOAD = {
    "run_type": "test",
    "target_ref": "c1234567",
    "result": "PASS",
    "runner": "pytest",
    "trust_source": "ci",
}
_SECRET = "AKIA" + "A" * 16
_NOW = "2026-10-13T00:00:00+00:00"


def _create_item(con, item_id):
    """테스트용 항목을 만든다."""
    DocDB(con, Actor("u-tester", "human")).put(
        [{"h": f"i:{item_id}", "op": "upsert", "title": "테스트 항목"}]
    )


def _document(con, doc_id):
    """L02 종류에 연결한 테스트 문서를 만든다."""
    con.execute(
        "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
        "baseline_mode, owner_id, created_at, updated_at) "
        "VALUES (?, 't', ?, 'living', 'L', 'normative', 'desired', 'u-tester', ?, ?)",
        (doc_id, doc_id, _NOW, _NOW),
    )
    con.execute("INSERT INTO document_kind (doc_id, kind_id) VALUES (?, 'L02')", (doc_id,))


def _put_test_ids(con, doc_id, text):
    """L02.test_ids 필드를 기록한다."""
    return DocDB(con, Actor("ai-claude", "ai")).put(
        [{"h": f"f:{doc_id}/L02/test_ids", "op": "set", "text": text}]
    )


@pytest.mark.parametrize(
    "location",
    [
        "run_type",
        "started_at",
        "ended_at",
        "raw_log.digest",
        "findings.code",
        "findings.target_id",
        "findings.disposition_ref",
        "nested.unused",
    ],
)
def test_fix_m1_scans_every_nested_run_string_and_rolls_back(project_db, location):
    con = db.connect(project_db)
    try:
        _create_item(con, "TC-HW-001")
        actor = Actor("u-tester", "human")
        payload = dict(
            _BASE_PAYLOAD,
            raw_log={"uri": "logs/run.txt", "digest": "sha256:abcd1234"},
            covers=["TC-HW-001"],
            findings=[
                {
                    "code": "DRIFT",
                    "severity": "warning",
                    "target_type": "item",
                    "target_id": "TC-HW-001",
                    "message": "발견",
                    "disposition_ref": "review-1",
                }
            ],
        )
        if location in ("run_type", "started_at", "ended_at"):
            payload[location] = _SECRET
        elif location == "raw_log.digest":
            payload["raw_log"]["digest"] = _SECRET
        elif location == "findings.code":
            payload["findings"][0]["code"] = _SECRET
        elif location == "findings.target_id":
            payload["findings"][0]["target_id"] = _SECRET
        elif location == "findings.disposition_ref":
            payload["findings"][0]["disposition_ref"] = _SECRET
        else:
            payload["unused"] = {"nested": [_SECRET]}

        with pytest.raises(DocdbError) as exc_info:
            runs.ingest(con, actor, payload)
        assert exc_info.value.code == "SECRET_DETECTED"
        assert _SECRET not in str(exc_info.value)
        assert _SECRET not in exc_info.value.detail
        for table in ("run", "artifact", "finding", "relation"):
            assert con.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

        run_id = runs.ingest(con, actor, dict(_BASE_PAYLOAD))
        assert run_id == "RUN-2026-0001"
    finally:
        con.close()


def test_fix_m2_r3_keeps_shared_relation_with_deterministic_anchor(project_db, frozen_now):
    con = db.connect(project_db)
    try:
        for item_id in ("REQ-HW-001", "TC-HW-001", "REQ-HW-002", "TC-HW-002"):
            _create_item(con, item_id)
        for doc_id in ("DOC-TC-A", "DOC-TC-B", "DOC-TC-C", "DOC-TC-D"):
            _document(con, doc_id)

        pair_one = "TC-HW-001 REQ-HW-001"
        pair_two = "TC-HW-002 REQ-HW-002"
        _put_test_ids(con, "DOC-TC-A", pair_one)
        _put_test_ids(con, "DOC-TC-B", pair_one)
        _put_test_ids(con, "DOC-TC-D", pair_two)
        _put_test_ids(con, "DOC-TC-C", pair_two)

        rel_one = con.execute(
            "SELECT rel_id FROM relation WHERE from_id='REQ-HW-001' AND to_id='TC-HW-001'"
        ).fetchone()[0]
        rel_two = con.execute(
            "SELECT rel_id FROM relation WHERE from_id='REQ-HW-002' AND to_id='TC-HW-002'"
        ).fetchone()[0]

        _put_test_ids(con, "DOC-TC-A", "TC-HW-001")
        _put_test_ids(con, "DOC-TC-D", "TC-HW-002")

        rows = con.execute(
            "SELECT from_id, to_id, anchor FROM relation WHERE source='rule:R3' ORDER BY from_id"
        ).fetchall()
        assert rows == [
            ("REQ-HW-001", "TC-HW-001", "f:DOC-TC-B/L02/test_ids"),
            ("REQ-HW-002", "TC-HW-002", "f:DOC-TC-C/L02/test_ids"),
        ]

        history = con.execute(
            "SELECT entity_id, op, reason, before_json, after_json FROM change_log "
            "WHERE entity_type='relation' AND entity_id IN (?, ?) ORDER BY change_id",
            (str(rel_one), str(rel_two)),
        ).fetchall()
        updates = [row for row in history if row[1] == "update"]
        assert [(row[0], row[1], row[2]) for row in updates] == [
            (str(rel_two), "update", "rule:R3"),
            (str(rel_one), "update", "rule:R3"),
        ]
        before = json.loads(updates[-1][3])
        after = json.loads(updates[-1][4])
        assert before["anchor"] == "f:DOC-TC-A/L02/test_ids"
        assert after["anchor"] == "f:DOC-TC-B/L02/test_ids"
    finally:
        con.close()


@pytest.mark.parametrize(
    "key,value,finding_key",
    [
        ("result", _SECRET, None),
        ("trust_source", _SECRET, None),
        ("severity", _SECRET, "severity"),
        ("target_type", _SECRET, "target_type"),
        ("disposition", _SECRET, "disposition"),
    ],
)
def test_fix_l2_run_ingest_does_not_echo_invalid_values(
    project_db, key, value, finding_key
):
    con = db.connect(project_db)
    try:
        payload = dict(_BASE_PAYLOAD)
        if finding_key is None:
            payload[key] = value
        else:
            payload["findings"] = [
                {
                    "code": "DRIFT",
                    "severity": "warning",
                    "target_type": "item",
                    "target_id": "TC-HW-001",
                    "message": "발견",
                    key: value,
                }
            ]
        with pytest.raises(DocdbError) as exc_info:
            runs.ingest(con, Actor("u-tester", "human"), payload)
        assert exc_info.value.code == "BAD_ARGS"
        assert _SECRET not in str(exc_info.value)
        assert _SECRET not in exc_info.value.detail
    finally:
        con.close()


def test_fix_l2_relation_invalid_type_does_not_echo_value(project_db):
    con = db.connect(project_db)
    try:
        _create_item(con, "REQ-HW-001")
        _create_item(con, "TC-HW-001")
        with pytest.raises(DocdbError) as exc_info:
            relations.propose(
                con,
                Actor("ai-claude", "ai"),
                "i:REQ-HW-001",
                _SECRET,
                "i:TC-HW-001",
                "검증 관계",
            )
        assert exc_info.value.code == "BAD_OP"
        assert _SECRET not in str(exc_info.value)
        assert _SECRET not in exc_info.value.detail
    finally:
        con.close()


def test_fix_l2_profile_invalid_rule_does_not_echo_value(monkeypatch):
    monkeypatch.setattr(profile, "_ALLOWED_RULE_KEYS", frozenset({"sensitive"}))
    with pytest.raises(DocdbError) as exc_info:
        profile.evaluate({"sensitive": _SECRET}, {}, set())
    assert exc_info.value.code == "BAD_ARGS"
    assert _SECRET not in str(exc_info.value)
    assert _SECRET not in exc_info.value.detail
