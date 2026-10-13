"""원본 토큰 절감 검사 28건을 pytest와 미니 DB로 옮긴다."""
import json
import random

import pytest

from docdb import db, textnorm
from docdb.actors import Actor
from docdb.llm import DocDB


NOW = "2026-10-13T00:00:00+00:00"
SENTENCES = [
    "디스크 사용률이 임계값을 넘으면 경고를 보낸다.",
    "점검 결과는 실행 기록으로 남기고 문서에 복사하지 않는다.",
    "운영 서버는 매일 새벽 3시에 백업을 확인한다.",
    "장애가 발생하면 담당자는 15분 안에 상황을 공유한다.",
    "설정 변경은 Git 커밋과 함께 기록한다.",
    "검증되지 않은 값은 TBD로 표시하고 사유를 적는다.",
    "하드웨어 교체 후에는 인벤토리를 갱신한다.",
    "롤백 절차는 배포 전에 리허설한다.",
    "모니터링 대시보드에서 응답 시간을 확인한다.",
    "접근 권한은 분기마다 재검토한다.",
]


def _body(rng, n_sec=3):
    """원본 검사와 같은 형태의 합성 문서 본문을 만든다."""
    parts = ["## 개요", " ".join(rng.choice(SENTENCES) for _ in range(4))]
    for index in range(1, n_sec + 1):
        parts.extend(
            [
                f"### {index}단계",
                " ".join(rng.choice(SENTENCES) for _ in range(rng.randint(3, 7))),
            ]
        )
    parts.extend(["## 확인", " ".join(rng.choice(SENTENCES) for _ in range(3))])
    return "\n".join(parts)


def _seed(con):
    """mini_db에 원본 S1~S11 검사에 필요한 합성 데이터를 넣는다."""
    rng = random.Random(42)
    con.execute(
        "INSERT INTO work (work_id, project_id, work_type, title, status, owner_id, opened_at) "
        "VALUES ('WORK-2026-0001', 't', 'feature', '서버 점검 자동화', 'active', 'u-tester', ?)",
        (NOW,),
    )
    narrative = [
        row[0]
        for row in con.execute(
            "SELECT kind_id FROM cat_kind WHERE lifecycle='living' "
            "AND storage_class='narrative' ORDER BY kind_id"
        )
    ]
    picked = (["D01"] if "D01" in narrative else []) + [
        kind for kind in narrative if kind != "D01"
    ][:11]
    docs = []
    for number, kind in enumerate(picked):
        doc_id = f"DOC-{kind}-{number:02d}"
        related = number < 4
        con.execute(
            "INSERT INTO document (doc_id, project_id, title, lifecycle, category, authority, "
            "baseline_mode, owner_id, export_policy, origin_work_id, created_at, updated_at) "
            "VALUES (?, 't', ?, 'living', ?, 'normative', 'desired', 'u-tester', 'public', ?, ?, ?)",
            (
                doc_id,
                f"{kind} 문서 {number}",
                kind[0],
                "WORK-2026-0001" if related else None,
                NOW,
                NOW,
            ),
        )
        con.execute(
            "INSERT INTO document_kind (doc_id, kind_id) VALUES (?, ?)", (doc_id, kind)
        )
        fields = [
            row[0]
            for row in con.execute(
                "SELECT field_key FROM cat_kind_field WHERE kind_id=? ORDER BY sort_order",
                (kind,),
            )
        ][:6]
        if number == 0:
            fields = fields[:2] + fields[3:]
        for field in fields:
            value = _body(rng, rng.randint(2, 5))
            con.execute(
                "INSERT INTO field_value (doc_id, kind_id, field_key, fill_state, format, "
                "value_text, author_id, updated_at, digest, tok_est) "
                "VALUES (?, ?, ?, 'filled', 'md', ?, 'u-tester', ?, ?, ?)",
                (
                    doc_id,
                    kind,
                    field,
                    value,
                    NOW,
                    textnorm.digest16(value),
                    textnorm.tok_est(value),
                ),
            )
        docs.append((doc_id, kind, related))

    first_doc = docs[0][0]
    for number in range(1, 41):
        requirement = f"REQ-HW-{number:03d}"
        test_case = f"TC-HW-{number:03d}"
        con.execute(
            "INSERT INTO item (item_id, project_id, item_type, title, status, defined_in, "
            "attrs, created_at, updated_at) VALUES (?, 't', 'requirement', ?, 'approved', ?, ?, ?, ?)",
            (
                requirement,
                f"점검 요구 {number}: {rng.choice(SENTENCES)}",
                first_doc,
                json.dumps({"priority": rng.choice(["P1", "P2"])}, ensure_ascii=False),
                NOW,
                NOW,
            ),
        )
        con.execute(
            "INSERT INTO item (item_id, project_id, item_type, title, created_at, updated_at) "
            "VALUES (?, 't', 'test_case', ?, ?, ?)",
            (test_case, f"테스트 {number}", NOW, NOW),
        )
        con.execute(
            "INSERT INTO relation (from_type, from_id, rel_type, to_type, to_id, created_at, "
            "status, source, proposed_by, decided_by) "
            "VALUES ('item', ?, 'verified_by', 'item', ?, ?, 'confirmed', 'human', 'u-tester', 'u-tester')",
            (requirement, test_case, NOW),
        )
    con.execute(
        "INSERT INTO run (run_id, work_id, run_type, target_ref, runner, result, trust_source, recorded_at) "
        "VALUES ('RUN-2026-0001', 'WORK-2026-0001', 'hw_check', 'git:abc', 'ci', 'PASS', 'ci', ?), "
        "('RUN-2026-0002', 'WORK-2026-0001', 'load_test', 'git:abc', 'manual', 'NOT_RUN', 'manual', ?)",
        (NOW, NOW),
    )
    con.execute(
        "INSERT INTO finding (run_id, code, severity, target_type, target_id, message) "
        "VALUES ('RUN-2026-0001', 'FIELD_MISSING', 'error', 'document', ?, '필수 필드 누락'), "
        "('RUN-2026-0001', 'DRIFT', 'warning', 'item', 'REQ-HW-003', '관측값이 기대값과 다름')",
        (first_doc,),
    )
    con.execute(
        "INSERT INTO handoff (handoff_id, work_id, from_role, to_role, payload, created_at) "
        "VALUES ('HO-2026-0001', 'WORK-2026-0001', 'implementer', 'reviewer', ?, ?)",
        (json.dumps({"next_actions": ["디스크 임계값 검토", "부하 테스트 실행"]}, ensure_ascii=False), NOW),
    )
    con.commit()
    return docs


@pytest.fixture
def token_scenario(mini_db):
    """합성 토큰 시나리오의 연결과 문서 목록을 제공한다."""
    con = db.connect(mini_db)
    try:
        docs = _seed(con)
        yield con, docs, DocDB(con, Actor("ai-codex", "ai"))
    finally:
        con.close()


def _rows(con, sql, args=()):
    """쿼리 결과를 열 이름을 가진 사전 목록으로 바꾼다."""
    cur = con.execute(sql, args)
    names = [column[0] for column in cur.description]
    return [dict(zip(names, row)) for row in cur.fetchall()]


def _size(value):
    """문자 수와 추정 토큰 수를 반환한다."""
    return len(value), textnorm.tok_est(value)


def _field_handle(con, doc_id):
    """문서에서 첫 저장 필드의 핸들을 돌려준다."""
    row = con.execute(
        "SELECT kind_id, field_key FROM field_value WHERE doc_id=? ORDER BY rowid LIMIT 1",
        (doc_id,),
    ).fetchone()
    return f"f:{doc_id}/{row[0]}/{row[1]}"


def test_t12_1_ctx_stays_within_original_token_budget(token_scenario):
    _con, _docs, client = token_scenario
    output = client.ctx("WORK-2026-0001", budget=1500)
    used = int(output.split("tok=")[1].split("/")[0])
    assert used <= 1600


def test_t12_2_ctx_lists_unresolved_run_and_open_finding(token_scenario):
    _con, _docs, client = token_scenario
    output = client.ctx("WORK-2026-0001")
    assert "! r:RUN-2026-0002 load_test NOT_RUN" in output
    assert "! F1 error FIELD_MISSING" in output


def test_t12_3_ctx_limits_documents_to_work_scope(token_scenario):
    _con, docs, client = token_scenario
    output = client.ctx("WORK-2026-0001")
    assert all(f"D {doc_id} " in output for doc_id, _kind, related in docs if related)
    assert not any(f"D {doc_id} " in output for doc_id, _kind, related in docs if not related)


def test_t12_4_ctx_marks_missing_required_field(token_scenario):
    _con, docs, client = token_scenario
    output = client.ctx("WORK-2026-0001")
    assert any(
        line.startswith(f"D {docs[0][0]} ") and "missing=" in line
        for line in output.splitlines()
    )


def test_t12_5_ctx_summarizes_items_by_type(token_scenario):
    _con, _docs, client = token_scenario
    output = client.ctx("WORK-2026-0001")
    assert any(line.startswith("I requirement×40") for line in output.splitlines())


def test_t12_6_ctx_uses_fewer_tokens_than_raw_dump(token_scenario):
    con, docs, client = token_scenario
    related = [doc_id for doc_id, _kind, is_related in docs if is_related]
    placeholders = ",".join("?" for _ in related)
    naive = json.dumps(
        {
            "work": _rows(con, "SELECT * FROM work WHERE work_id='WORK-2026-0001'"),
            "documents": _rows(
                con, f"SELECT * FROM document WHERE doc_id IN ({placeholders})", related
            ),
            "fields": _rows(
                con, f"SELECT * FROM field_value WHERE doc_id IN ({placeholders})", related
            ),
            "items": _rows(
                con, f"SELECT * FROM item WHERE defined_in IN ({placeholders})", related
            ),
            "relations": _rows(con, "SELECT * FROM relation"),
            "runs": _rows(con, "SELECT * FROM run WHERE work_id='WORK-2026-0001'"),
            "findings": _rows(con, "SELECT * FROM finding"),
            "handoffs": _rows(con, "SELECT * FROM handoff"),
        },
        ensure_ascii=False,
        indent=1,
    )
    assert _size(client.ctx("WORK-2026-0001"))[1] < _size(naive)[1]


def test_t12_7_ctx_records_context_read(token_scenario):
    con, _docs, client = token_scenario
    client.ctx("WORK-2026-0001")
    assert con.execute(
        "SELECT count(*) FROM context_read WHERE work_id='WORK-2026-0001' "
        "AND actor_id='ai-codex'"
    ).fetchone()[0] >= 1


def test_t12_8_get_items_uses_less_than_json(token_scenario):
    con, _docs, client = token_scenario
    item_rows = con.execute(
        "SELECT item_id, item_type, status, title FROM item WHERE item_type='requirement'"
    ).fetchall()
    json_rows = json.dumps(
        [dict(zip(("item_id", "item_type", "status", "title"), row)) for row in item_rows],
        ensure_ascii=False,
    )
    compact = client.get([f"i:REQ-HW-{number:03d}" for number in range(1, 41)], level=0, budget=None)
    assert _size(compact)[1] < _size(json_rows)[1]


def test_t12_9_get_returns_unchanged_for_known_digest(token_scenario):
    con, docs, client = token_scenario
    handle = _field_handle(con, docs[0][0])
    digest = con.execute(
        "SELECT digest FROM field_value WHERE doc_id=? ORDER BY rowid LIMIT 1", (docs[0][0],)
    ).fetchone()[0]
    output = client.get([handle], level=2, known={handle: digest})
    assert output.splitlines()[1] == f"{handle} UNCHANGED"


def test_t12_10_put_replace_section_preserves_other_sections(token_scenario):
    con, docs, _client = token_scenario
    handle = _field_handle(con, docs[0][0])
    row = con.execute(
        "SELECT value_text, digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?",
        (handle,),
    ).fetchone()
    client = DocDB(con, Actor("ai-codex", "ai"), work="WORK-2026-0001")
    result = client.put(
        [
            {
                "h": handle,
                "op": "replace_section",
                "anchor": "### 1단계",
                "text": "임계값은 85%로 한다. 초과 시 RUN 기록을 남긴다.",
                "expect": row[1],
            }
        ],
        reason="임계값 확정",
    )
    value = con.execute(
        "SELECT value_text FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?",
        (handle,),
    ).fetchone()[0]
    assert "APPLIED" in result and "임계값은 85%로 한다. 초과 시 RUN 기록을 남긴다." in value
    assert "## 확인" in value and "### 2단계" in value


def test_t12_11_put_section_patch_costs_less_than_rewrite(token_scenario):
    con, docs, _client = token_scenario
    handle = _field_handle(con, docs[0][0])
    current, digest = con.execute(
        "SELECT value_text, digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?",
        (handle,),
    ).fetchone()
    patch = {
        "h": handle,
        "op": "replace_section",
        "anchor": "### 1단계",
        "text": "임계값은 85%로 한다. 초과 시 RUN 기록을 남긴다.",
        "expect": digest,
    }
    rewrite = current.replace(
        current.split("### 1단계")[1].split("###")[0], "\n" + patch["text"] + "\n"
    )
    assert textnorm.tok_est(json.dumps(patch, ensure_ascii=False)) < textnorm.tok_est(rewrite)


def test_t12_12_put_response_does_not_return_body(token_scenario):
    con, docs, _client = token_scenario
    handle = _field_handle(con, docs[0][0])
    body = "임계값은 85%로 한다. 초과 시 RUN 기록을 남긴다."
    digest = con.execute(
        "SELECT digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?",
        (handle,),
    ).fetchone()[0]
    patch = {
        "h": handle,
        "op": "replace_section",
        "anchor": "### 1단계",
        "text": body,
        "expect": digest,
    }
    result = DocDB(con, Actor("ai-codex", "ai"), work="WORK-2026-0001").put(
        [patch], reason="임계값 확정"
    )
    assert body not in result
    assert "OK " + handle in result


def test_t12_13_put_rejects_stale_digest(token_scenario):
    con, docs, _client = token_scenario
    handle = _field_handle(con, docs[0][0])
    row = con.execute(
        "SELECT digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?",
        (handle,),
    ).fetchone()
    client = DocDB(con, Actor("ai-codex", "ai"), work="WORK-2026-0001")
    client.put([{"h": handle, "op": "append", "text": "선행 변경"}])
    result = client.put([{"h": handle, "op": "append", "text": "낡은 변경", "expect": row[0]}])
    assert "NOTHING_APPLIED" in result and "CONFLICT" in result
    current = con.execute(
        "SELECT digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?",
        (handle,),
    ).fetchone()[0]
    assert result.splitlines()[-1] == (
        f"ERR {handle} CONFLICT now={current} (since 로 변경분 확인 후 재시도)"
    )


def test_t12_14_put_batch_rolls_back_all_changes_on_one_error(token_scenario):
    con, docs, _client = token_scenario
    handle = _field_handle(con, docs[0][0])
    before = con.execute("SELECT count(*) FROM change_log").fetchone()[0]
    bad = handle.rsplit("/", 1)[0] + "/no_such_field"
    client = DocDB(con, Actor("ai-codex", "ai"), work="WORK-2026-0001")
    result = client.put(
        [{"h": handle, "op": "append", "text": "취소될 변경"}, {"h": bad, "text": "x"}]
    )
    assert "NOTHING_APPLIED" in result
    assert con.execute("SELECT count(*) FROM change_log").fetchone()[0] == before


def test_t12_15_since_groups_three_changed_fields(token_scenario):
    con, docs, _client = token_scenario
    cursor = con.execute("SELECT coalesce(max(change_id), 0) FROM change_log").fetchone()[0]
    client = DocDB(con, Actor("ai-codex", "ai"), work="WORK-2026-0001")
    for doc_id, kind, _related in docs[1:4]:
        client.put([{"h": _field_handle(con, doc_id), "op": "append", "text": "변경: 점검 주기"}])
    result = client.since(cursor)
    assert "entities=3" in result.splitlines()[0]


def test_t12_16_since_is_smaller_than_rereading_related_fields(token_scenario):
    con, docs, _client = token_scenario
    handles = [
        f"f:{doc_id}/{kind}/{field}"
        for doc_id, kind, related in docs
        if related
        for (field,) in con.execute("SELECT field_key FROM field_value WHERE doc_id=?", (doc_id,))
    ]
    cursor = con.execute("SELECT coalesce(max(change_id), 0) FROM change_log").fetchone()[0]
    client = DocDB(con, Actor("ai-codex", "ai"), work="WORK-2026-0001")
    for doc_id, _kind, related in docs[1:4]:
        if related:
            client.put([{"h": _field_handle(con, doc_id), "op": "append", "text": "짧은 변경"}])
    reread = "\n".join(client.get(handles, level=2, budget=None).splitlines())
    delta = client.since(cursor)
    assert _size(delta)[1] < _size(reread)[1]


def test_t12_17_ctx_marks_changed_read_document_stale(token_scenario):
    con, docs, client = token_scenario
    client.ctx("WORK-2026-0001")
    coder = DocDB(con, Actor("ai-codex", "ai"), work="WORK-2026-0001")
    coder.put([{"h": _field_handle(con, docs[1][0]), "op": "append", "text": "이후 변경"}])
    output = client.ctx("WORK-2026-0001")
    assert "! stale d:" in output


def test_t12_18_find_uses_fts_for_three_character_query(token_scenario):
    _con, _docs, client = token_scenario
    result = client.find("임계값을")
    assert "mode=fts" in result.splitlines()[0]
    assert "hits=0" not in result.splitlines()[0]


def test_t12_19_find_uses_like_for_two_character_query(token_scenario):
    _con, _docs, client = token_scenario
    result = client.find("장애")
    assert "mode=like" in result.splitlines()[0]
    assert "hits=0" not in result.splitlines()[0]


def test_t12_20_trigram_match_does_not_find_two_characters(token_scenario):
    con, _docs, _client = token_scenario
    assert con.execute("SELECT count(*) FROM fts_text WHERE fts_text MATCH '\"장애\"'").fetchone()[0] == 0


def test_t12_21_find_index_updates_and_survives_vacuum(token_scenario):
    con, docs, client = token_scenario
    handle = _field_handle(con, docs[0][0])
    client.put([{"h": handle, "op": "append", "text": "신규어휘콴텀점검"}])
    indexed = con.execute(
        "SELECT count(*) FROM fts_text WHERE fts_text MATCH '\"콴텀점검\"'"
    ).fetchone()[0]
    con.execute("VACUUM")
    result = client.find("콴텀점검")
    assert indexed == 1 and handle in result


def test_t12_22_fts_integrity_check_succeeds(token_scenario):
    con, _docs, _client = token_scenario
    assert con.execute("INSERT INTO fts_text(fts_text) VALUES('integrity-check')") is not None


def test_t12_23_get_shows_fresh_and_stale_summary(token_scenario):
    con, docs, _client = token_scenario
    handle = _field_handle(con, docs[0][0])
    digest = con.execute(
        "SELECT digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?",
        (handle,),
    ).fetchone()[0]
    con.execute(
        "INSERT INTO entity_summary VALUES (?, ?, ?, 12, 'llm', 'ai-claude', ?)",
        (handle, "임계값 85%, 실행 기록", digest, NOW),
    )
    client = DocDB(con, Actor("ai-codex", "ai"), work="WORK-2026-0001")
    fresh = client.get([handle], level=1)
    client.put([{"h": handle, "op": "append", "text": "요약 이후 변경"}])
    stale = client.get([handle], level=1)
    assert 'sum="임계값' in fresh and "sum=stale" in stale


def test_t12_24_summary_staleness_view_marks_changed_source(token_scenario):
    con, docs, _client = token_scenario
    handle = _field_handle(con, docs[0][0])
    digest = con.execute(
        "SELECT digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?",
        (handle,),
    ).fetchone()[0]
    con.execute(
        "INSERT INTO entity_summary VALUES (?, '요약', ?, 1, 'llm', 'ai-claude', ?)",
        (handle, digest, NOW),
    )
    DocDB(con, Actor("ai-codex", "ai")).put([{"h": handle, "op": "append", "text": "변경"}])
    assert con.execute("SELECT count(*) FROM v_summary_stale WHERE handle=?", (handle,)).fetchone()[0] == 1


def test_t12_25_trace_returns_relation_ids_only(token_scenario):
    _con, _docs, client = token_scenario
    result = client.trace("i:REQ-HW-001")
    assert result.splitlines()[1] == "i:REQ-HW-001 -verified_by-> i:TC-HW-001"


def test_t12_26_put_changes_have_valid_history_chain(token_scenario):
    con, docs, _client = token_scenario
    client = DocDB(con, Actor("ai-codex", "ai"), work="WORK-2026-0001")
    handles = [_field_handle(con, doc_id) for doc_id, _kind, _related in docs[:5]]
    client.put([{"h": handle, "op": "append", "text": "이력 체인 확인"} for handle in handles])
    assert con.execute("SELECT count(*) FROM change_log WHERE actor_id='ai-codex'").fetchone()[0] >= 5
    assert all(len(row[0]) == 71 and row[0].startswith("sha256:") for row in con.execute(
        "SELECT row_digest FROM change_log WHERE actor_id='ai-codex'"
    ))


def test_t12_27_fixture_preserves_foreign_key_integrity(token_scenario):
    con, docs, _client = token_scenario
    client = DocDB(con, Actor("u-tester", "human"), work="WORK-2026-0001")
    for doc_id, _kind, _related in docs:
        fields = con.execute(
            "SELECT kind_id, field_key, value_text FROM field_value WHERE doc_id=? ORDER BY rowid",
            (doc_id,),
        ).fetchall()
        result = client.put(
            [
                {"h": f"f:{doc_id}/{kind_id}/{field_key}", "op": "set", "text": value_text}
                for kind_id, field_key, value_text in fields
            ]
        )
        assert result.splitlines()[1] == f"APPLIED ok={len(fields)} err=0"
    handle = _field_handle(con, docs[0][0])
    digest = con.execute(
        "SELECT digest FROM field_value WHERE 'f:'||doc_id||'/'||kind_id||'/'||field_key=?",
        (handle,),
    ).fetchone()[0]
    con.execute(
        "INSERT INTO entity_summary VALUES (?,?,?,?,?,?,?)",
        (handle, "임계값 85%, 초과 시 RUN 기록.", digest, 12, "llm", "ai-claude", NOW),
    )
    assert con.execute("PRAGMA foreign_key_check").fetchall() == []


def test_t12_28_call_log_aggregates_all_six_tools(token_scenario):
    con, docs, client = token_scenario
    client.ctx("WORK-2026-0001")
    client.get([_field_handle(con, docs[0][0])])
    client.find("임계값")
    client.trace("i:REQ-HW-001")
    client.since(0)
    client.put([{"h": _field_handle(con, docs[0][0]), "op": "append", "text": "집계"}])
    tools = {
        row[0]
        for row in con.execute("SELECT tool, count(*) FROM call_log GROUP BY tool")
    }
    assert tools >= {"ctx", "get", "find", "trace", "since", "put"}
