from __future__ import annotations

from dataclasses import dataclass

METHOD_VERSION = 1


@dataclass(frozen=True)
class DocSpec:
    """단일 계획서 문서 규격."""

    code: str
    title: str
    scope: str
    container: str
    na_allowed: bool
    first_question: str
    check: str


UNIT_ITEMS: dict[str, str] = {
    "PRD": "[계획서] PRD",
    "IMPL2": "[계획서] 구현 2 기능 레퍼런스",
    "IMPL3": "[계획서] 구현 3 화면 레퍼런스",
    "IMPL5": "[계획서] 구현 5 알림 처리",
    "IMPL7": "[계획서] 구현 7 단계별 범위",
    "DECISION": "[계획서] 결정",
}

REPO_FILES: dict[str, str] = {
    "IMPL1": ".ledger/design/data.md",
    "IMPL4": ".ledger/design/logging.md",
    "IMPL6": ".ledger/design/security.md",
    "IMPL8": ".ledger/ops/maintenance.md",
    "IMPL9": ".ledger/ops/deploy.md",
}

DRAWIO: dict[str, str] = {
    "IMPL1": ".ledger/design/data-model.drawio",
    "IMPL4": ".ledger/design/logging-flow.drawio",
}

FEATURE_PRD_REQUIRED = ("PRD1", "PRD2", "PRD6", "PRD8", "PRD9")

DOCS: tuple[DocSpec, ...] = (
    DocSpec(
        code="PRD1",
        title="PRD 1 문서 머리",
        scope="unit",
        container="PRD",
        na_allowed=False,
        first_question="누가 결정하고, 언제까지 무엇을 내놓아야 합니까?",
        check="필드가 모두 있음. 진행 상태는 정해진 값(초안·검토·승인·진행·종료)",
    ),
    DocSpec(
        code="PRD2",
        title="PRD 2 목표 · 성공 지표",
        scope="unit",
        container="PRD",
        na_allowed=False,
        first_question="끝났다는 것을 무엇으로 확인합니까?",
        check="목표마다 확인 방법이 있음",
    ),
    DocSpec(
        code="PRD3",
        title="PRD 3 배경 · 환경 · 지원 범위",
        scope="unit",
        container="PRD",
        na_allowed=False,
        first_question="지금 무엇을 쓰고 있고, 어디까지 지원해야 합니까?",
        check="환경 행마다 확인 근거",
    ),
    DocSpec(
        code="PRD4",
        title="PRD 4 전략 · 선정 이유",
        scope="unit",
        container="PRD",
        na_allowed=False,
        first_question="(스킬이 안 2–3개를 먼저 보이고 고르게 함)",
        check="고르지 않은 안 1개 이상",
    ),
    DocSpec(
        code="PRD5",
        title="PRD 5 가정",
        scope="unit",
        container="PRD",
        na_allowed=False,
        first_question="확인하지 않았지만 사실로 두고 진행하는 것은 무엇입니까?",
        check="세 구분이 모두 있음. 확인 시점이 지난 가정 0건",
    ),
    DocSpec(
        code="PRD6",
        title="PRD 6 사용자 스토리 · 요구사항",
        scope="unit",
        container="PRD",
        na_allowed=False,
        first_question="누가 무엇을 하려고 합니까?",
        check="단위 분리 뒤 모든 스토리에 작업 항목",
    ),
    DocSpec(
        code="PRD7",
        title="PRD 7 목업 · 다이어그램",
        scope="unit",
        container="PRD",
        na_allowed=True,
        first_question="보여 줄 화면이나 흐름이 있습니까?",
        check="링크 대상이 있음",
    ),
    DocSpec(
        code="PRD8",
        title="PRD 8 미결 질문",
        scope="unit",
        container="PRD",
        na_allowed=True,
        first_question="(스킬이 대화 내내 채움)",
        check="기본값이 비지 않음. 필요 시점이 지난 미결 질문 0건",
    ),
    DocSpec(
        code="PRD9",
        title="PRD 9 제외 범위",
        scope="unit",
        container="PRD",
        na_allowed=False,
        first_question="이번에 하지 않을 것은 무엇입니까?",
        check="1건 이상",
    ),
    DocSpec(
        code="IMPL1",
        title="구현 1 데이터 보관 설계",
        scope="repo",
        container=REPO_FILES["IMPL1"],
        na_allowed=True,
        first_question="새로 저장하거나 바꾸는 데이터가 있습니까?",
        check="drawio 파싱. 테이블마다 보관 기간",
    ),
    DocSpec(
        code="IMPL2",
        title="구현 2 기능 레퍼런스",
        scope="unit",
        container="IMPL2",
        na_allowed=True,
        first_question="기능마다 입력과 결과는 무엇입니까?",
        check="기능마다 스토리 ID",
    ),
    DocSpec(
        code="IMPL3",
        title="구현 3 화면 레퍼런스",
        scope="unit",
        container="IMPL3",
        na_allowed=True,
        first_question="화면마다 누가 무엇을 봅니까?",
        check="화면마다 프레임 링크 또는 와이어프레임",
    ),
    DocSpec(
        code="IMPL4",
        title="구현 4 로깅 처리 레퍼런스",
        scope="repo",
        container=REPO_FILES["IMPL4"],
        na_allowed=True,
        first_question="문제가 생기면 무엇을 보고 원인을 찾습니까?",
        check="추적 순서가 있음",
    ),
    DocSpec(
        code="IMPL5",
        title="구현 5 알림 처리",
        scope="unit",
        container="IMPL5",
        na_allowed=True,
        first_question="누가 어떤 일을 알아야 합니까?",
        check="심각도마다 채널",
    ),
    DocSpec(
        code="IMPL6",
        title="구현 6 인증 · 보안 처리",
        scope="repo",
        container=REPO_FILES["IMPL6"],
        na_allowed=False,
        first_question="누가 어떤 수단으로 접근하고, 무엇이 새면 안 됩니까?",
        check="접근 경로마다 수단과 보관 위치. 위협마다 대응",
    ),
    DocSpec(
        code="IMPL7",
        title="구현 7 단계별 범위",
        scope="unit",
        container="IMPL7",
        na_allowed=False,
        first_question="첫 단계에서 무엇이 동작하면 됩니까?",
        check="단계마다 완료 기준과 cycle",
    ),
    DocSpec(
        code="IMPL8",
        title="구현 8 유지보수",
        scope="repo",
        container=REPO_FILES["IMPL8"],
        na_allowed=True,
        first_question="고장 나면 누가 어떤 순서로 고칩니까?",
        check="결함 흐름이 있음",
    ),
    DocSpec(
        code="IMPL9",
        title="구현 9 배포",
        scope="repo",
        container=REPO_FILES["IMPL9"],
        na_allowed=True,
        first_question="어디에 어떻게 올리고, 실패하면 어떻게 되돌립니까?",
        check="되돌리기가 있음",
    ),
)

DOC_BY_CODE: dict[str, DocSpec] = {doc.code: doc for doc in DOCS}

_CONTAINS: dict[str, str] = {
    "PRD1": "참여자(결정권자·작성·검토), 진행 상태, 목표 출시일, 저장소와 Plane 프로젝트 키",
    "PRD2": "목표마다 지표와 확인 방법, 비목표, 반목표",
    "PRD3": "왜 지금인지, 현재 작업 환경과 확인 근거, 지원·미지원 범위",
    "PRD4": "고른 안, 고르지 않은 안과 이유",
    "PRD5": "사용자·기술·사업 가정, 틀리면 바뀌는 것, 확인 시점",
    "PRD6": "스토리, 기능·비기능 요구사항, 단계, Plane 작업 항목",
    "PRD7": "화면 도구 프레임 링크, drawio 원본 링크",
    "PRD8": "질문, 영향, 기본값, 필요 시점, 답과 결정 번호",
    "PRD9": "이번에 하지 않을 것",
    "IMPL1": "무엇을 어디에, 테이블·필드, 보관 기간, drawio",
    "IMPL2": "기능 목록, 동작 규칙, API, 기능 흐름 프레임",
    "IMPL3": "화면 목록, 공통 상태(없음·지연·오류), 프레임",
    "IMPL4": "로그 흐름, 형식, 보관, 추적 순서, drawio",
    "IMPL5": "채널, 상태, 중복 방지, 심각도, 알림 화면 프레임",
    "IMPL6": "인증 주체와 수단, 토큰 보관, 외부 노출, 위협과 대응, 보안 화면 프레임",
    "IMPL7": '단계별 만드는 것, 완료 기준, 갱신할 구현 문서, 단계별 "변경 없음" 사유',
    "IMPL8": "결함 흐름, 정기 점검, 업그레이드, 스키마 변경",
    "IMPL9": "대상, 절차, 되돌리기, 버전 규칙",
}


def section_heading(code: str) -> str:
    """문서 코드에 해당하는 섹션 제목을 돌려준다."""
    return DOC_BY_CODE[code].title


def _format_location(doc: DocSpec) -> str:
    if doc.scope == "unit":
        return f"단위 · {UNIT_ITEMS[doc.container]}"
    drawio = DRAWIO.get(doc.code)
    if drawio is not None:
        return f"저장소 · `{doc.container}` + `{drawio}`"
    return f"저장소 · `{doc.container}`"


def render_docs_reference() -> str:
    """문서 18종 규격 Markdown 표를 생성한다."""
    lines = [
        "| 문서 | 담는 것 | 첫 질문 | 해당 없음 | 위치 | 검사 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for doc in DOCS:
        na_text = "허용" if doc.na_allowed else "불가"
        location = _format_location(doc)
        lines.append(
            f"| {doc.title} | {_CONTAINS[doc.code]} | {doc.first_question} | {na_text} | {location} | {doc.check} |"
        )
    return "\n".join(lines) + "\n"
