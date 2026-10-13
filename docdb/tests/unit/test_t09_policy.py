"""T09 민감 정보 검사, 정책 예외, 외부 전송 정책 단위 테스트."""
import pytest

from docdb import clock, history
from docdb.actors import Actor
from docdb.errors import DocdbError
from docdb.policy import (
    SECRET_PATTERNS,
    allow_secret,
    assert_no_secrets,
    can_export,
    scan_secrets,
)
from tests.helpers import seed_min


def test_t09_1_scan_secrets_aws_access_key() -> None:
    secret = "AKIA" + "A" * 16
    assert scan_secrets(secret) == ["aws_access_key"]


def test_t09_2_scan_secrets_github_token() -> None:
    secret = "ghp_" + "a" * 36
    assert scan_secrets(secret) == ["github_token"]


def test_t09_3_scan_secrets_anthropic_key() -> None:
    secret = "sk-ant-" + "a" * 20
    assert scan_secrets(secret) == ["anthropic_key"]


def test_t09_4_scan_secrets_openai_key() -> None:
    secret = "sk-" + "a" * 20
    assert scan_secrets(secret) == ["openai_key"]


def test_t09_5_scan_secrets_slack_token() -> None:
    secret = "xoxb-" + "1" * 10
    assert scan_secrets(secret) == ["slack_token"]


def test_t09_6_scan_secrets_telegram_bot_token() -> None:
    secret = "123456789:" + "A" * 35
    assert scan_secrets(secret) == ["telegram_bot_token"]


def test_t09_7_scan_secrets_jwt() -> None:
    secret = "eyJ" + "a" * 10 + "." + "b" * 10 + "." + "c" * 10
    assert scan_secrets(secret) == ["jwt"]


def test_t09_8_scan_secrets_private_key() -> None:
    secret = "-----BEGIN RSA PRIVATE KEY-----"
    assert scan_secrets(secret) == ["private_key"]


def test_t09_9_scan_secrets_assignment() -> None:
    secret = "password = " + "x" * 8
    assert scan_secrets(secret) == ["assignment"]


def test_t09_10_scan_secrets_clean_text() -> None:
    assert scan_secrets("password: 없음") == []
    assert scan_secrets("sk-ant-") == []
    assert scan_secrets("일반 문장") == []
    assert scan_secrets("") == []
    assert scan_secrets(None) == []


def test_t09_11_assert_no_secrets_detected(schema_con: object) -> None:
    secret = "AKIA" + "A" * 16
    with pytest.raises(DocdbError) as exc_info:
        assert_no_secrets(schema_con, "f:DOC-X/D01/behavior", [secret])
    assert exc_info.value.code == "SECRET_DETECTED"
    assert "aws_access_key" in exc_info.value.detail
    assert secret not in exc_info.value.detail


def test_t09_12_allow_secret_human_interactive(schema_con: object, frozen_now: None) -> None:
    seed_min(schema_con)
    actor = Actor("u-tester", "human")
    handle = "f:DOC-X/D01/behavior"
    secret = "AKIA" + "A" * 16

    ex_id = allow_secret(schema_con, handle, "테스트 예외", 7, actor, interactive=True)
    assert ex_id == "PEX-2026-0001"

    row = schema_con.execute(
        "SELECT expires_at, scope, reason, approved_by FROM policy_exception WHERE exception_id = ?",
        (ex_id,),
    ).fetchone()
    assert row is not None
    assert row[0] == "2026-10-20T00:00:00+00:00"
    assert row[1] == f"secret:{handle}"
    assert row[2] == "테스트 예외"
    assert row[3] == "u-tester"

    # 해당 handle은 통과
    assert_no_secrets(schema_con, handle, [secret])

    # 다른 handle은 거부
    with pytest.raises(DocdbError) as exc_info:
        assert_no_secrets(schema_con, "f:DOC-OTHER/D01/behavior", [secret])
    assert exc_info.value.code == "SECRET_DETECTED"

    # 이력 1건 기록 확인
    cnt = schema_con.execute(
        "SELECT count(*) FROM change_log WHERE entity_type = 'policy_exception' AND entity_id = ?",
        (ex_id,),
    ).fetchone()[0]
    assert cnt == 1
    assert history.verify_chain(schema_con, "policy_exception", ex_id) == []


def test_t09_13_allow_secret_expired(
    schema_con: object, frozen_now: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_min(schema_con)
    actor = Actor("u-tester", "human")
    handle = "f:DOC-X/D01/behavior"
    secret = "AKIA" + "A" * 16

    allow_secret(schema_con, handle, "테스트 예외", 7, actor, interactive=True)
    assert_no_secrets(schema_con, handle, [secret])

    # 8일 뒤로 시각 이동
    monkeypatch.setattr(clock, "now_iso", lambda: "2026-10-21T00:00:00+00:00")

    with pytest.raises(DocdbError) as exc_info:
        assert_no_secrets(schema_con, handle, [secret])
    assert exc_info.value.code == "SECRET_DETECTED"


def test_t09_14_allow_secret_ai_forbidden(schema_con: object) -> None:
    seed_min(schema_con)
    ai_actor = Actor("ai-claude", "ai")
    human_actor = Actor("u-tester", "human")
    handle = "f:DOC-X/D01/behavior"

    with pytest.raises(DocdbError) as exc_info:
        allow_secret(schema_con, handle, "AI 허용 시도", 7, ai_actor, interactive=True)
    assert exc_info.value.code == "NEEDS_HUMAN"

    with pytest.raises(DocdbError) as exc_info:
        allow_secret(schema_con, handle, "비대화형 허용 시도", 7, human_actor, interactive=False)
    assert exc_info.value.code == "NEEDS_HUMAN"


def test_t09_15_can_export_policy(schema_con: object) -> None:
    seed_min(schema_con)
    now = "2026-10-13T00:00:00+00:00"
    for doc_id, pol in [
        ("DOC-LOCAL", "local_only"),
        ("DOC-APPROVED", "approved_providers"),
        ("DOC-PUBLIC", "public"),
    ]:
        schema_con.execute(
            "INSERT INTO document ("
            "doc_id, project_id, title, lifecycle, category, authority, "
            "baseline_mode, owner_id, export_policy, created_at, updated_at"
            ") VALUES (?, 't', '문서', 'living', 'D', 'normative', 'desired', 'u-tester', ?, ?, ?)",
            (doc_id, pol, now, now),
        )

    assert can_export(schema_con, "DOC-LOCAL") is False
    assert can_export(schema_con, "DOC-APPROVED") is True
    assert can_export(schema_con, "DOC-PUBLIC") is True

    with pytest.raises(DocdbError) as exc_info:
        can_export(schema_con, "DOC-NONEXISTENT")
    assert exc_info.value.code == "NOT_FOUND"
