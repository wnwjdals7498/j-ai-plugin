"""묶음 FD 최종 리뷰 후속 지적 회귀 테스트."""
from pathlib import Path

import pytest

from docdb import actors, db, ids, importer, paths, policy, runs, workflow
from docdb.actors import Actor
from docdb.errors import DocdbError


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """테스트 백업을 임시 디렉터리에 저장한다."""
    monkeypatch.setattr(paths, "data_dir", lambda: tmp_path / "data")


def _secret() -> str:
    """형식만 맞춘 AWS 키 예시를 돌려준다."""
    return "AKIA" + "A" * 16


def _create_work(con, human: Actor) -> str:
    """테스트용 작업을 만들고 ID를 반환한다."""
    return workflow.create_work(
        con,
        human,
        "검수 작업",
        "review",
        "u-tester",
        ["pmt:docdb/test"],
    )


def test_fix_l2_workflow_invalid_status_is_redacted(con, human: Actor) -> None:
    """허용 목록 밖 상태가 오류 상세에 나타나지 않는다."""
    secret = _secret()
    work_id = _create_work(con, human)

    with pytest.raises(DocdbError) as exc_info:
        workflow.set_status(con, human, work_id, secret, True)

    assert exc_info.value.code == "STATUS_FORBIDDEN"
    assert secret not in exc_info.value.detail


def test_fix_l2_profile_unregistered_fact_key_is_redacted(con, human: Actor) -> None:
    """등록되지 않은 사실 키가 오류 상세에 나타나지 않는다."""
    secret = _secret()

    with pytest.raises(DocdbError) as exc_info:
        from docdb import profile

        profile.set_fact(con, human, "t", "cap." + secret, True, "검수", True)

    assert exc_info.value.code == "BAD_ARGS"
    assert secret not in exc_info.value.detail


def test_fix_l2_ids_invalid_handoff_is_redacted() -> None:
    """형식이 틀린 인계 ID가 오류 상세에 나타나지 않는다."""
    secret = _secret()

    with pytest.raises(DocdbError) as exc_info:
        ids.validate("handoff", secret)

    assert exc_info.value.code == "ID_FORMAT"
    assert secret not in exc_info.value.detail


def test_fix_l2_actor_invalid_id_is_redacted(con) -> None:
    """형식이 틀린 행위자 ID가 오류 상세에 나타나지 않는다."""
    secret = _secret()

    with pytest.raises(DocdbError) as exc_info:
        actors.add_actor(con, secret, "human", "검수자", None)

    assert exc_info.value.code == "ID_FORMAT"
    assert secret not in exc_info.value.detail


def test_fix_l2_cli_invalid_owner_id_is_redacted(tmp_path: Path, run_cli) -> None:
    """형식이 틀린 init 소유자 ID가 오류 출력에 나타나지 않는다."""
    secret = _secret()
    project_dir = tmp_path / "invalid-owner"

    code, out, err = run_cli(
        [
            "init",
            "--project",
            "t",
            "--name",
            "테스트",
            "--owner",
            secret,
            "--owner-name",
            "Tester",
            "--dir",
            str(project_dir),
        ]
    )

    assert code == 1
    assert out == ""
    assert err.startswith("ERROR ID_FORMAT ")
    assert secret not in err


def test_fix_l2_import_invalid_segment_target_is_hidden(con, human: Actor) -> None:
    """형식이 틀린 import 대상은 물음표로 표시한다."""
    secret = _secret()

    result = importer.label(
        con,
        human,
        [{"seg": "s:" + secret, "skip": True, "why": "제외"}],
    )

    assert result == "ERR ? BAD_HANDLE"
    assert secret not in result


@pytest.mark.parametrize(
    ("object_name", "object_value"),
    [
        ("env_snapshot", {"nested": {_secret(): "x"}}),
        ("target_versions", {_secret(): "x"}),
    ],
)
def test_fix_n1_run_freeform_object_keys_are_secret_checked(
    con, human: Actor, object_name: str, object_value: dict
) -> None:
    """실행의 자유 형식 객체 키도 중첩을 포함해 검사한다."""
    secret = _secret()
    payload = {
        "run_type": "test",
        "target_ref": "c1234567",
        "result": "PASS",
        "runner": "pytest",
        "trust_source": "ci",
        object_name: object_value,
    }

    with pytest.raises(DocdbError) as exc_info:
        runs.ingest(con, human, payload)

    assert exc_info.value.code == "SECRET_DETECTED"
    assert secret not in exc_info.value.detail


def test_fix_n3_allow_secret_reason_ignores_target_exception(
    con, human: Actor
) -> None:
    """대상 예외가 있어도 예외 사유의 민감 정보는 거부한다."""
    handle = "f:DOC-X/D01/behavior"
    policy.allow_secret(con, handle, "검토 완료", 7, human, True)
    secret = _secret()

    with pytest.raises(DocdbError) as exc_info:
        policy.allow_secret(con, handle, "승인 사유 " + secret, 7, human, True)

    assert exc_info.value.code == "SECRET_DETECTED"
    assert secret not in exc_info.value.detail
    assert con.execute("SELECT count(*) FROM policy_exception").fetchone()[0] == 1


def test_fix_l11_init_owner_name_is_normalized_and_scanned(
    tmp_path: Path, run_cli
) -> None:
    """init이 소유자 이름을 정규화하고 민감 정보를 저장 전에 거부한다."""
    normalized_dir = tmp_path / "normalized-owner-name"
    code, _out, err = run_cli(
        [
            "init",
            "--project",
            "t",
            "--name",
            "테스트",
            "--owner",
            "u-tester",
            "--owner-name",
            "Cafe\u0301\r\nreview",
            "--dir",
            str(normalized_dir),
        ]
    )
    assert code == 0, err
    con = db.connect(normalized_dir / ".docdb" / "docdb.sqlite")
    try:
        name = con.execute(
            "SELECT name FROM actor WHERE actor_id='u-tester'"
        ).fetchone()[0]
        assert name == "Café\nreview"
    finally:
        con.close()

    secret_dir = tmp_path / "secret-owner-name"
    secret = _secret()
    code, out, err = run_cli(
        [
            "init",
            "--project",
            "t",
            "--name",
            "테스트",
            "--owner",
            "u-tester",
            "--owner-name",
            "Reviewer " + secret,
            "--dir",
            str(secret_dir),
        ]
    )

    assert code == 1
    assert out == ""
    assert err.startswith("ERROR SECRET_DETECTED ")
    assert secret not in err
    assert not (secret_dir / ".docdb" / "docdb.sqlite").exists()
