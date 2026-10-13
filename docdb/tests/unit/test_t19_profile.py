"""T19 프로필 사실과 적용 판정 테스트."""
import pytest
from docdb.errors import DocdbError
from docdb.profile import evaluate, unknown_facts


def test_t19_1_evaluate_logic():
    """always, fact(1/0/None), all, any, selected_kind 3값 논리 평가를 검증한다."""
    # always
    assert evaluate({"always": True}, {}, set()) is True
    assert evaluate({"always": False}, {}, set()) is True

    # fact 1, 0, None, missing
    assert evaluate({"fact": "cap.db"}, {"cap.db": 1}, set()) is True
    assert evaluate({"fact": "cap.ui"}, {"cap.ui": 0}, set()) is False
    assert evaluate({"fact": "cap.prod"}, {"cap.prod": None}, set()) is None
    assert evaluate({"fact": "cap.missing"}, {}, set()) is None

    # all(True, None) -> None
    assert (
        evaluate(
            {"all": [{"always": True}, {"fact": "cap.prod"}]},
            {"cap.prod": None},
            set(),
        )
        is None
    )

    # all(True, False) -> False
    assert (
        evaluate(
            {"all": [{"always": True}, {"fact": "cap.ui"}]},
            {"cap.ui": 0},
            set(),
        )
        is False
    )

    # any(None, True) -> True
    assert (
        evaluate(
            {"any": [{"fact": "cap.prod"}, {"always": True}]},
            {"cap.prod": None},
            set(),
        )
        is True
    )

    # any(None, False) -> None
    assert (
        evaluate(
            {"any": [{"fact": "cap.prod"}, {"fact": "cap.ui"}]},
            {"cap.prod": None, "cap.ui": 0},
            set(),
        )
        is None
    )

    # selected_kind
    assert evaluate({"selected_kind": "D01"}, {}, {"D01"}) is True
    assert evaluate({"selected_kind": "D01"}, {}, {"K01"}) is False
    assert evaluate({"selected_kind": "D01"}, {}, set()) is False


def test_t19_2_evaluate_rejects_eval_key():
    """eval 등 지원하지 않는 규칙 키는 BAD_ARGS로 거부한다."""
    with pytest.raises(DocdbError) as exc_info:
        evaluate({"eval": "1"}, {}, set())
    assert exc_info.value.code == "BAD_ARGS"

    with pytest.raises(DocdbError) as exc_info:
        evaluate({}, {}, set())
    assert exc_info.value.code == "BAD_ARGS"

    with pytest.raises(DocdbError) as exc_info:
        evaluate({"always": True, "eval": "2"}, {}, set())
    assert exc_info.value.code == "BAD_ARGS"


def test_t19_3_fact_list_starter_catalogue(project_db, con, run_cli):
    """시작 카탈로그 적재 후 fact list는 cap.production unknown을 출력한다."""
    code, out, err = run_cli(["--db", str(project_db), "fact", "list"])
    assert code == 0
    assert out == "cap.production unknown\n"
    assert err == ""

    assert unknown_facts(con, "t") == ["cap.production"]


def test_t19_4_applicability_recompute_and_cli(project_db, con, run_cli):
    """applicability 실행 시 D01은 required=true, K02는 unknown이고 개수를 출력한다."""
    code, out, err = run_cli(["--db", str(project_db), "applicability"])
    assert code == 0
    assert err == ""
    assert out == "true=14 false=0 unknown=4\n"

    d01_req = con.execute(
        "SELECT required FROM kind_applicability WHERE project_id='t' AND kind_id='D01'"
    ).fetchone()[0]
    assert d01_req == "true"

    k02_req = con.execute(
        "SELECT required FROM kind_applicability WHERE project_id='t' AND kind_id='K02'"
    ).fetchone()[0]
    assert k02_req == "unknown"


def test_t19_5_fact_set_requires_human_and_recomputes(project_db, con, run_cli):
    """fact set은 사람이 대화형으로만 실행 가능하며 profile_revision과 K02를 갱신한다."""
    # 비대화형 실행 시 거부
    code, out, err = run_cli(
        ["--db", str(project_db), "fact", "set", "cap.production", "true", "--reason", "운영 환경 대상"],
        interactive=False,
    )
    assert code == 1
    assert err == "ERROR NEEDS_HUMAN 사람 및 대화형 터미널(TTY)이 필요함\n"
    assert con.execute("SELECT count(*) FROM fact_value").fetchone()[0] == 0
    assert con.execute("SELECT profile_revision FROM project WHERE project_id='t'").fetchone()[0] == 1

    # AI 행위자 대화형 실행 시 거부
    code, out, err = run_cli(
        [
            "--db",
            str(project_db),
            "--actor",
            "ai-claude",
            "fact",
            "set",
            "cap.production",
            "true",
            "--reason",
            "운영 환경 대상",
        ],
        interactive=True,
    )
    assert code == 1
    assert err == "ERROR NEEDS_HUMAN 사람 및 대화형 터미널(TTY)이 필요함\n"
    assert con.execute("SELECT count(*) FROM fact_value").fetchone()[0] == 0
    assert con.execute("SELECT profile_revision FROM project WHERE project_id='t'").fetchone()[0] == 1

    # 사람 행위자 대화형 실행 성공
    code, out, err = run_cli(
        [
            "--db",
            str(project_db),
            "--actor",
            "u-tester",
            "fact",
            "set",
            "cap.production",
            "true",
            "--reason",
            "운영 환경 대상",
        ],
        interactive=True,
    )
    assert code == 0
    assert err == ""

    # DB 검증: 값 1
    val = con.execute(
        "SELECT value FROM fact_value WHERE project_id='t' AND fact_key='cap.production'"
    ).fetchone()[0]
    assert val == 1

    # profile_revision == 2
    rev = con.execute(
        "SELECT profile_revision FROM project WHERE project_id='t'"
    ).fetchone()[0]
    assert rev == 2

    # change_log에 fact_value 이력 행 단정
    fact_change = con.execute(
        "SELECT op, entity_id, after_json FROM change_log WHERE entity_type='fact_value' AND entity_id='t|cap.production'"
    ).fetchone()
    assert fact_change is not None
    assert fact_change[0] == "insert"
    assert fact_change[1] == "t|cap.production"
    import json
    fact_after = json.loads(fact_change[2])
    assert fact_after["value"] == 1
    assert fact_after["reason"] == "운영 환경 대상"
    assert fact_after["confirmed_by"] == "u-tester"

    # change_log에 project update 이력 행(profile_revision 1 -> 2) 단정
    proj_change = con.execute(
        "SELECT op, entity_id, before_json, after_json FROM change_log WHERE entity_type='project' AND entity_id='t' AND op='update'"
    ).fetchone()
    assert proj_change is not None
    assert json.loads(proj_change[2]) == {"profile_revision": 1}
    assert json.loads(proj_change[3]) == {"profile_revision": 2}

    # 재계산 후 K02 required == 'true'
    k02_req = con.execute(
        "SELECT required FROM kind_applicability WHERE project_id='t' AND kind_id='K02'"
    ).fetchone()[0]
    assert k02_req == "true"


def test_t19_6_fact_set_rejects_non_cap_prefix(project_db, run_cli):
    """cap.* 외의 사실 키(evt.release 등)는 BAD_ARGS로 거부한다."""
    code, out, err = run_cli(
        [
            "--db",
            str(project_db),
            "--actor",
            "u-tester",
            "fact",
            "set",
            "evt.release",
            "true",
            "--reason",
            "릴리스 이벤트",
        ],
        interactive=True,
    )
    assert code == 1
    assert err == "ERROR BAD_ARGS 사실 키는 cap.* 형식이어야 함\n"


def test_t19_7_scenario_port_at01_to_at03_with_evaluate(mini_db):
    """이식 시나리오 AT-01~03의 조건을 profile.evaluate로 평가해 통과함을 검증한다."""
    from docdb import db, profile

    connection = db.connect(mini_db)
    try:
        with db.write_tx(connection):
            connection.execute(
                "INSERT INTO fact_value (project_id, fact_key, value) VALUES "
                "('t', 'cap.database', 1), ('t', 'cap.ui', 0), ('t', 'cap.production', NULL)"
            )
            profile.recompute(connection, "t")

        assert connection.execute(
            "SELECT required FROM kind_applicability WHERE kind_id='I01'"
        ).fetchone()[0] == "true"
        assert connection.execute(
            "SELECT required FROM kind_applicability WHERE kind_id='E01'"
        ).fetchone()[0] == "false"
        assert connection.execute(
            "SELECT required FROM kind_applicability WHERE kind_id='F04'"
        ).fetchone()[0] == "unknown"
    finally:
        connection.close()
