"""docdb 오류 모델 및 오류 코드 정의."""

ERROR_CODES: dict[str, int] = {
    # 2: 환경·버전·의존성
    "PY_TOO_OLD": 2,
    "NO_VENV": 2,
    "VENV_MISMATCH": 2,
    "DEP_MISSING": 2,
    "SQLITE_CAPABILITY": 2,
    # 3: DB 파일·구조
    "NO_DB": 3,
    "DB_EXISTS": 3,
    "MIGRATION_REQUIRED": 3,
    "DB_NEWER": 3,
    # 1: 일반 오류
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


class DocdbError(Exception):
    """docdb 전용 예외 클래스."""

    def __init__(self, code: str, detail: str = "") -> None:
        """오류 코드와 부가 정보를 받아 예외를 초기화한다."""
        if code not in ERROR_CODES:
            raise ValueError(f"Unknown error code: {code}")
        self.code = code
        self.detail = detail
        self.exit_code = ERROR_CODES[code]
        msg = f"ERROR {code} {detail}" if detail else f"ERROR {code}"
        super().__init__(msg)


def format_error(err: DocdbError) -> str:
    """오류를 CLI 및 MCP 규격에 맞는 한 줄 문자열로 반환한다."""
    if err.detail:
        return f"ERROR {err.code} {err.detail}"
    return f"ERROR {err.code}"
