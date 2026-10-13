"""T02 수용 테스트: 오류 모델, 시간, 텍스트 정규화 규칙."""
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path

import pytest

import docdb.clock as clock
from docdb.errors import ERROR_CODES, DocdbError, format_error
from docdb.textnorm import (
    canonical_json,
    digest16,
    norm_key,
    normalize_text,
    q,
    sha256_hex,
    tok_est,
)

EXPECTED_C05_CODES = {
    "PY_TOO_OLD": 2,
    "NO_VENV": 2,
    "VENV_MISMATCH": 2,
    "DEP_MISSING": 2,
    "SQLITE_CAPABILITY": 2,
    "NO_DB": 3,
    "DB_EXISTS": 3,
    "MIGRATION_REQUIRED": 3,
    "DB_NEWER": 3,
    "BAD_ARGS": 1,
    "NOT_FOUND": 1,
    "NEEDS_HUMAN": 1,
    "ID_FORMAT": 1,
    "SECRET_DETECTED": 1,
    "CONFLICT": 1,
    "ANCHOR_NOT_FOUND": 1,
    "UNSUPPORTED_HANDLE": 1,
    "BAD_HANDLE": 1,
    "BAD_OP": 1,
    "DB": 1,
    "DUPLICATE": 1,
    "REJECTED_BEFORE": 1,
    "STATUS_FORBIDDEN": 1,
    "SUMMARY_TOO_LONG": 1,
    "CATALOGUE_INCOMPATIBLE": 1,
    "IMPORT_STALE": 1,
}


def test_t02_1_error_code_and_format() -> None:
    """DocdbError 초기화와 format_error 출력 형식을 검증한다."""
    err = DocdbError("NO_DB", "x")
    assert err.code == "NO_DB"
    assert err.detail == "x"
    assert err.exit_code == 3
    assert format_error(err) == "ERROR NO_DB x"


def test_t02_2_all_c05_error_codes() -> None:
    """C-05 표의 모든 오류 코드와 종료 코드를 검증한다."""
    assert ERROR_CODES == EXPECTED_C05_CODES


def test_t02_3_unknown_error_code_raises_value_error() -> None:
    """ERROR_CODES에 없는 코드로 DocdbError 생성 시 ValueError가 발생함을 검증한다."""
    with pytest.raises(ValueError):
        DocdbError("NOPE")


def test_t02_4_normalize_text_crlf_and_cr() -> None:
    """normalize_text가 CRLF와 CR을 모두 LF로 변환함을 검증한다."""
    assert normalize_text("a\r\nb\rc") == "a\nb\nc"


def test_t02_5_normalize_text_nfc() -> None:
    """normalize_text가 NFD 문자열을 NFC로 정규화함을 검증한다."""
    nfd_hangul = unicodedata.normalize("NFD", "한글")
    normalized = normalize_text(nfd_hangul)
    assert normalized == "한글"
    assert unicodedata.is_normalized("NFC", normalized)


def test_t02_6_normalize_text_none_and_digest16_none() -> None:
    """normalize_text(None)과 digest16(None) 동작을 검증한다."""
    assert normalize_text(None) is None
    assert digest16(None) == digest16("")


def test_t02_7_digest16_crlf_and_format() -> None:
    """digest16이 개행 정규화 후 16자리 소문자 16진수를 반환함을 검증한다."""
    d1 = digest16("a\r\nb")
    d2 = digest16("a\nb")
    assert d1 == d2
    assert len(d1) == 16
    assert bool(re.match(r"^[0-9a-f]{16}$", d1))


def test_t02_8_canonical_json() -> None:
    """canonical_json이 키 정렬, LF 정규화, 공백 없는 압축 JSON을 생성함을 검증한다."""
    result = canonical_json({"b": 1, "a": "x\r\n"})
    assert result == '{"a":"x\\n","b":1}'


def test_t02_9_tok_est() -> None:
    """tok_est의 토큰 추정 수치를 검증한다."""
    assert tok_est("") == 0
    assert tok_est("abcdefg") == 2
    assert tok_est("가나") == 2
    assert tok_est("ab가") == 2


def test_t02_10_q_quoting() -> None:
    """q()의 공백, 빈 문자열, 줄바꿈 인용 처리를 검증한다."""
    assert q("a b") == '"a b"'
    assert q("") == '""'
    assert q("x\ny") == '"x ⏎ y"'
    assert q("ab") == "ab"


def test_t02_11_norm_key() -> None:
    """norm_key의 소문자화 및 공백·특수문자 제거를 검증한다."""
    assert norm_key("Behavior_Spec (요구)") == "behaviorspec요구"


def test_t02_12_now_iso_format() -> None:
    """clock.now_iso()의 ISO-8601 UTC 형식을 검증한다."""
    iso = clock.now_iso()
    assert bool(re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$", iso))


def test_t02_13_frozen_now_fixture(frozen_now: None) -> None:
    """frozen_now 픽스처 적용 시 시각 및 연도 고정을 검증한다."""
    assert clock.now_iso() == "2026-10-13T00:00:00+00:00"
    assert clock.utc_year() == 2026


def test_t02_14_bootstrap_safe_modules_isolation() -> None:
    """errors, clock, textnorm 임포트 시 무거운 모듈이 로드되지 않음을 검증한다."""
    src_dir = str(Path(__file__).resolve().parents[2] / "src")
    env = dict(os.environ)
    env["PYTHONPATH"] = f"{src_dir}:{env.get('PYTHONPATH', '')}".rstrip(":")
    code = (
        "import docdb.errors, docdb.clock, docdb.textnorm;"
        "import sys;"
        "forbidden = {'pysqlite3', 'mcp', 'docdb.db'};"
        "loaded = forbidden.intersection(sys.modules);"
        "assert not loaded, f'forbidden modules loaded: {loaded}';"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0, res.stderr
