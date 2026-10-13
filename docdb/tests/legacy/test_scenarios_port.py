"""원본 스키마 시나리오를 현재 docdb 스키마에 이식한다."""
import json

import pytest


NOW = "2026-10-13T00:00:00+00:00"
ENTITY = "DOC-REQ-HWCHECK|D01|behavior"


@pytest.fixture
def scenario_con(mini_db, frozen_now):
    """시나리오가 공유하는 최소 프로젝트와 레코드를 준비한다."""
    from docdb import db, history

    con = db.connect(mini_db)
    try:
        with db.write_tx(con):
            con.execute(
                "INSERT INTO work "
                "(work_id, project_id, work_type, title, status, owner_id, "
                "base_refs, result_refs, opened_at) "
                "VALUES ('WORK-2026-0001', 't', 'feature', '서버 점검 자동화', "
                "'active', 'u-tester', '[]', '[]', ?)",
                (NOW,),
            )
            con.execute(
                "INSERT INTO fact_value "
                "(project_id, fact_key, value, reason, confirmed_by, confirmed_at) "
                "VALUES ('t', 'cap.database', 1, '운영 DB 사용', 'u-tester', ?), "
                "('t', 'cap.ui', 0, '화면 없음', 'u-tester', ?), "
                "('t', 'cap.production', NULL, NULL, NULL, NULL)",
                (NOW, NOW),
            )
            facts = {
                key: value
                for key, value in con.execute(
                    "SELECT fact_key, value FROM fact_value WHERE project_id='t'"
                )
            }

            def ev(rule):  # 제한 연산만 지원 (eval 금지, 10.6)
                if 'always' in rule: return True
                if 'fact' in rule:
                    v = facts.get(rule['fact']); return None if v is None else bool(v)
                if 'selected_kind' in rule: return False
                if 'all' in rule:
                    vs = [ev(r) for r in rule['all']]
                    return False if False in vs else (True if all(v is True for v in vs) else None)
                if 'any' in rule:
                    vs = [ev(r) for r in rule['any']]
                    return True if True in vs else (False if all(v is False for v in vs) else None)
                raise ValueError(rule)

            tri = lambda value: {True: "true", False: "false", None: "unknown"}[value]
            for kind_id, applies_rule, required_rule in con.execute(
                "SELECT kind_id, applies_when, required_when FROM cat_kind ORDER BY kind_id"
            ).fetchall():
                applies_rule = json.loads(applies_rule)
                required_rule = json.loads(required_rule)
                con.execute(
                    "INSERT INTO kind_applicability "
                    "(project_id, kind_id, applies, required, reason, profile_revision, evaluated_at) "
                    "VALUES ('t', ?, ?, ?, ?, 1, ?)",
                    (
                        kind_id,
                        tri(ev(applies_rule)),
                        tri(ev(required_rule)),
                        json.dumps(required_rule, ensure_ascii=False),
                        NOW,
                    ),
                )

            con.execute(
                "INSERT INTO document "
                "(doc_id, project_id, title, lifecycle, category, authority, "
                "baseline_mode, owner_id, created_at, updated_at) "
                "VALUES ('DOC-REQ-HWCHECK', 't', '서버 점검 요구사항', 'living', "
                "'D', 'normative', 'desired', 'u-tester', ?, ?)",
                (NOW, NOW),
            )
            con.execute(
                "INSERT INTO document_kind (doc_id, kind_id) "
                "VALUES ('DOC-REQ-HWCHECK', 'D01')"
            )
            con.execute(
                "INSERT INTO field_value "
                "(doc_id, kind_id, field_key, fill_state, value_text, author_id, updated_at) "
                "VALUES ('DOC-REQ-HWCHECK', 'D01', 'requirement_ids', 'filled', "
                "'REQ-HW-001', 'ai-codex', ?)",
                (NOW,),
            )
            con.execute(
                "INSERT INTO item "
                "(item_id, project_id, item_type, title, defined_in, created_at, updated_at) "
                "VALUES ('REQ-HW-001', 't', 'requirement', '디스크 사용률 점검', "
                "'DOC-REQ-HWCHECK', ?, ?)",
                (NOW, NOW),
            )
            con.execute(
                "INSERT INTO item "
                "(item_id, project_id, item_type, title, attrs, created_at, updated_at) "
                "VALUES ('RES-DB01', 't', 'infra_resource', 'DB 서버 1호기', "
                "'{\"kind\":\"server\",\"env\":\"ENV-PROD\"}', ?, ?)",
                (NOW, NOW),
            )
            con.execute(
                "INSERT INTO item "
                "(item_id, project_id, item_type, title, created_at, updated_at) "
                "VALUES ('TC-HW-001', 't', 'test_case', '디스크 점검 테스트', ?, ?), "
                "('ROLE-OPS', 't', 'role', '운영 책임', ?, ?)",
                (NOW, NOW, NOW, NOW),
            )
            con.execute(
                "INSERT INTO relation "
                "(from_type, from_id, rel_type, to_type, to_id, is_required, "
                "created_at, source, proposed_by) "
                "VALUES ('item', 'REQ-HW-001', 'verified_by', 'item', 'TC-HW-001', "
                "1, ?, 'human', 'u-tester')",
                (NOW,),
            )
            con.execute(
                "INSERT INTO run "
                "(run_id, work_id, run_type, target_ref, env_item_id, runner, "
                "command_argv, result, trust_source, recorded_at) "
                "VALUES ('RUN-2026-0001', 'WORK-2026-0001', 'hw_check', "
                "'git:abc123', 'RES-DB01', 'ci-runner', '[\"check_disk\",\"--host\",\"db01\"]', "
                "'PASS', 'ci', ?)",
                (NOW,),
            )
            for index in range(1, 4):
                history.append_change(
                    con,
                    "field_value",
                    ENTITY,
                    "insert" if index == 1 else "update",
                    None if index == 1 else {"value_text": f"v{index - 1}"},
                    {"value_text": f"v{index}"},
                    "u-tester",
                    None,
                    None,
                )
            con.execute(
                "INSERT INTO doc_revision "
                "(doc_id, revision_no, prev_revision_id, snapshot_json, content_digest, "
                "reason, created_by, created_at) "
                "VALUES ('DOC-REQ-HWCHECK', 1, NULL, '{}', 'sha256:r1', "
                "'초안 기준선', 'u-tester', ?)",
                (NOW,),
            )
            con.execute(
                "INSERT INTO handoff "
                "(handoff_id, work_id, from_role, to_role, payload, created_at) "
                "VALUES ('HO-2026-0001', 'WORK-2026-0001', 'implementer', 'reviewer', "
                "'{\"next_actions\":[\"디스크 임계값 검토\"],\"not_run\":[\"부하 테스트\"]}', ?)",
                (NOW,),
            )
        yield con
    finally:
        con.close()


def _expect_db_error(con, sql, args=()):
    """실행이 DB 제약으로 거부되는지 확인한다."""
    from docdb import db

    with pytest.raises(db.DatabaseError):
        with db.write_tx(con):
            con.execute(sql, args)


def _append(con, value_text, actor_id="u-tester"):
    """시나리오의 필드 이력에 정상적인 다음 행을 추가한다."""
    from docdb import db, history

    with db.write_tx(con):
        last = con.execute(
            "SELECT seq FROM change_log WHERE entity_type='field_value' AND entity_id=? "
            "ORDER BY seq DESC LIMIT 1",
            (ENTITY,),
        ).fetchone()
        seq = last[0] + 1
        return history.append_change(
            con,
            "field_value",
            ENTITY,
            "update",
            None,
            {"value_text": value_text},
            actor_id,
            "WORK-2026-0001",
            None,
        )


def _add_context_read(con, change_id):
    """문서를 읽은 시점의 변경 번호를 컨텍스트에 기록한다."""
    con.execute(
        "INSERT INTO context_read "
        "(work_id, actor_id, session_ref, target_type, target_id, last_change_id, "
        "reason, read_at) VALUES ('WORK-2026-0001', 'ai-codex', 'S-1', 'document', "
        "'DOC-REQ-HWCHECK', ?, '변경 대상 요구사항', ?)",
        (change_id, NOW),
    )


def test_t13_1_database_fact_makes_kind_required(scenario_con):
    assert scenario_con.execute(
        "SELECT required FROM kind_applicability WHERE kind_id='I01'"
    ).fetchone()[0] == "true"


def test_t13_2_false_fact_makes_kind_optional(scenario_con):
    assert scenario_con.execute(
        "SELECT required FROM kind_applicability WHERE kind_id='E01'"
    ).fetchone()[0] == "false"


def test_t13_3_unknown_fact_stays_unknown(scenario_con):
    assert scenario_con.execute(
        "SELECT required FROM kind_applicability WHERE kind_id='F04'"
    ).fetchone()[0] == "unknown"


def test_t13_4_applicability_count_matches_loaded_kinds(scenario_con):
    counts = dict(
        scenario_con.execute(
            "SELECT required, count(*) FROM kind_applicability GROUP BY 1"
        ).fetchall()
    )
    loaded = scenario_con.execute("SELECT count(*) FROM cat_kind").fetchone()[0]
    assert sum(counts.values()) == loaded


def test_t13_5_duplicate_document_id_is_rejected(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO document "
        "(doc_id, project_id, title, lifecycle, category, authority, baseline_mode, "
        "owner_id, created_at, updated_at) VALUES "
        "('DOC-REQ-HWCHECK', 't', 'x', 'living', 'D', 'normative', 'desired', "
        "'u-tester', 't', 't')",
    )


def test_t13_6_living_document_rejects_work_kind(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO document_kind (doc_id, kind_id) "
        "VALUES ('DOC-REQ-HWCHECK', 'F05')",
    )


def test_t13_7_required_field_view_reports_missing_fields(scenario_con):
    missing = [
        row[2]
        for row in scenario_con.execute(
            "SELECT * FROM v_missing_required_fields WHERE doc_id='DOC-REQ-HWCHECK'"
        )
    ]
    assert set(missing) == {"behavior", "acceptance", "scope", "owners"}


def test_t13_8_tbd_field_requires_reason(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO field_value "
        "(doc_id, kind_id, field_key, fill_state, author_id, updated_at) "
        "VALUES ('DOC-REQ-HWCHECK', 'D01', 'scope', 'tbd', 'u-tester', 't')",
    )


def test_t13_9_unknown_field_key_is_rejected(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO field_value "
        "(doc_id, kind_id, field_key, fill_state, value_text, author_id, updated_at) "
        "VALUES ('DOC-REQ-HWCHECK', 'D01', 'foo', 'filled', 'x', 'u-tester', 't')",
    )


def test_t13_10_item_id_must_match_type_prefix(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO item "
        "(item_id, project_id, item_type, title, created_at, updated_at) "
        "VALUES ('X-1', 't', 'requirement', 'x', 't', 't')",
    )


def test_t13_11_rtm_is_derived_from_relation(scenario_con):
    rows = scenario_con.execute(
        "SELECT requirement_id, verified_by FROM v_rtm"
    ).fetchall()
    assert rows == [("REQ-HW-001", "TC-HW-001")]


def test_t13_12_artifact_rejects_parent_path(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO artifact (artifact_id, uri, role, source_mode) "
        "VALUES ('ART-2026-0001', '../etc/passwd', 'code', 'authored')",
    )


def test_t13_13_artifact_rejects_absolute_path(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO artifact (artifact_id, uri, role, source_mode) "
        "VALUES ('ART-2026-0002', '/etc/passwd', 'code', 'authored')",
    )


def test_t13_14_run_evidence_is_immutable(scenario_con):
    _expect_db_error(
        scenario_con,
        "UPDATE run SET result='FAIL' WHERE run_id='RUN-2026-0001'",
    )


def test_t13_15_run_result_must_be_one_of_five_states(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO run "
        "(run_id, run_type, target_ref, runner, result, trust_source, recorded_at) "
        "VALUES ('RUN-2026-0002', 'test', 'x', 'r', 'SUCCESS', 'ci', 't')",
    )


def test_t13_16_history_recursive_walk_returns_reverse_sequence(scenario_con):
    rows = scenario_con.execute(
        "WITH RECURSIVE h(id, seq, prev) AS ("
        "SELECT change_id, seq, prev_change_id FROM change_log c WHERE entity_id=? "
        "AND NOT EXISTS (SELECT 1 FROM change_log n WHERE n.prev_change_id=c.change_id) "
        "UNION ALL SELECT c.change_id, c.seq, c.prev_change_id FROM change_log c "
        "JOIN h ON c.change_id=h.prev) SELECT seq FROM h",
        (ENTITY,),
    ).fetchall()
    assert [seq for (seq,) in rows] == [3, 2, 1]


def test_t13_17_history_sequence_supports_direct_lookup(scenario_con):
    value = scenario_con.execute(
        "SELECT json_extract(after_json, '$.value_text') FROM change_log "
        "WHERE entity_id=? AND seq=2",
        (ENTITY,),
    ).fetchone()[0]
    assert value == "v2"


def test_t13_18_history_rejects_forked_previous_link(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO change_log "
        "(entity_type, entity_id, seq, prev_change_id, op, actor_id, changed_at, "
        "row_digest, prev_digest) SELECT 'field_value', ?, 3, change_id, 'update', "
        "'u-tester', 't', 'x', row_digest FROM change_log "
        "WHERE entity_id=? AND seq=2",
        (ENTITY, ENTITY),
    )


def test_t13_19_history_rejects_broken_digest_link(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO change_log "
        "(entity_type, entity_id, seq, prev_change_id, op, actor_id, changed_at, "
        "row_digest, prev_digest) SELECT 'field_value', ?, 4, change_id, 'update', "
        "'u-tester', 't', 'x', 'sha256:forged' FROM change_log "
        "WHERE entity_id=? AND seq=3",
        (ENTITY, ENTITY),
    )


def test_t13_20_history_rows_cannot_be_updated(scenario_con):
    _expect_db_error(
        scenario_con,
        "UPDATE change_log SET reason='x' WHERE entity_id=? AND seq=1",
        (ENTITY,),
    )


def test_t13_21_history_rows_cannot_be_deleted(scenario_con):
    _expect_db_error(
        scenario_con,
        "DELETE FROM change_log WHERE entity_id=? AND seq=1",
        (ENTITY,),
    )


def test_t13_22_ai_actor_cannot_approve(scenario_con):
    _expect_db_error(
        scenario_con,
        "INSERT INTO approval "
        "(approval_id, revision_id, approver_id, decision, decided_at) "
        "VALUES ('APR-2026-0001', 1, 'ai-codex', 'approved', 't')",
    )


def test_t13_23_human_approval_is_recorded(scenario_con):
    scenario_con.execute(
        "INSERT INTO approval "
        "(approval_id, revision_id, gate, approver_id, decision, decided_at) "
        "VALUES ('APR-2026-0002', 1, 'design-approval', 'u-tester', 'approved', ?)",
        (NOW,),
    )
    assert scenario_con.execute("SELECT count(*) FROM approval").fetchone()[0] == 1


def test_t13_24_legal_hold_document_cannot_be_deleted(scenario_con):
    from docdb import db

    with db.write_tx(scenario_con):
        scenario_con.execute(
            "UPDATE document SET legal_hold=1 WHERE doc_id='DOC-REQ-HWCHECK'"
        )
    _expect_db_error(
        scenario_con,
        "DELETE FROM document WHERE doc_id='DOC-REQ-HWCHECK'",
    )


def test_t13_25_role_has_one_active_accountable_actor(scenario_con):
    scenario_con.execute(
        "INSERT INTO role_assignment VALUES ('ROLE-OPS', 'u-tester', 'A', ?, NULL)",
        (NOW,),
    )
    _expect_db_error(
        scenario_con,
        "INSERT INTO role_assignment VALUES ('ROLE-OPS', 'team-ci', 'A', '2026-10-14', NULL)",
    )


def test_t13_26_entity_head_is_latest_history_row(scenario_con):
    head = scenario_con.execute(
        "SELECT change_id FROM v_entity_head WHERE entity_id=?",
        (ENTITY,),
    ).fetchone()[0]
    assert scenario_con.execute(
        "SELECT seq FROM change_log WHERE change_id=?", (head,)
    ).fetchone()[0] == 3


def test_t13_27_unchanged_context_is_not_stale(scenario_con):
    from docdb import db

    head = scenario_con.execute(
        "SELECT change_id FROM v_entity_head WHERE entity_id=?", (ENTITY,)
    ).fetchone()[0]
    with db.write_tx(scenario_con):
        _add_context_read(scenario_con, head)
    assert scenario_con.execute("SELECT count(*) FROM v_context_stale").fetchone()[0] == 0


def test_t13_28_changed_context_is_marked_stale(scenario_con):
    from docdb import db

    head = scenario_con.execute(
        "SELECT change_id FROM v_entity_head WHERE entity_id=?", (ENTITY,)
    ).fetchone()[0]
    with db.write_tx(scenario_con):
        _add_context_read(scenario_con, head)
    _append(scenario_con, "v4")
    stale = scenario_con.execute(
        "SELECT target_id, read_change_id, latest_change_id FROM v_context_stale"
    ).fetchall()
    assert len(stale) == 1


def test_t13_29_resume_card_reports_handoff_runs_and_stale_context(scenario_con):
    from docdb import db

    head = scenario_con.execute(
        "SELECT change_id FROM v_entity_head WHERE entity_id=?", (ENTITY,)
    ).fetchone()[0]
    with db.write_tx(scenario_con):
        _add_context_read(scenario_con, head)
    _append(scenario_con, "v4")
    scenario_con.execute(
        "INSERT INTO run "
        "(run_id, work_id, run_type, target_ref, runner, result, trust_source, recorded_at) "
        "VALUES ('RUN-2026-0002', 'WORK-2026-0001', 'load_test', 'git:abc123', "
        "'manual', 'NOT_RUN', 'manual', ?)",
        (NOW,),
    )
    card = scenario_con.execute(
        "SELECT last_handoff_id, last_handoff_ack, next_actions, unresolved_runs, "
        "stale_context FROM v_work_resume WHERE work_id='WORK-2026-0001'"
    ).fetchone()
    assert card == (
        "HO-2026-0001",
        "pending",
        '["디스크 임계값 검토"]',
        1,
        1,
    )


def test_t13_30_handoff_ack_requires_checked_baseline(scenario_con):
    _expect_db_error(
        scenario_con,
        "UPDATE handoff SET ack_status='accepted', ack_by='u-tester' "
        "WHERE handoff_id='HO-2026-0001'",
    )


def test_t13_31_recent_changes_show_human_actor(scenario_con):
    _append(scenario_con, "v4")
    row = scenario_con.execute(
        "SELECT actor_type, entity_id, seq FROM v_recent_changes LIMIT 1"
    ).fetchone()
    assert row == ("human", ENTITY, 4)


def test_t13_32_context_read_rows_cannot_be_updated(scenario_con):
    from docdb import db

    head = scenario_con.execute(
        "SELECT change_id FROM v_entity_head WHERE entity_id=?", (ENTITY,)
    ).fetchone()[0]
    with db.write_tx(scenario_con):
        _add_context_read(scenario_con, head)
    _expect_db_error(
        scenario_con,
        "UPDATE context_read SET reason='x' WHERE target_id='DOC-REQ-HWCHECK'",
    )


def test_t13_33_foreign_keys_are_consistent(scenario_con):
    assert scenario_con.execute("PRAGMA foreign_key_check").fetchall() == []
