# docdb 플러그인 구현 계획

> **에이전트 실행자에게:** 이 계획은 superpowers:subagent-driven-development 방식(작업마다 새 Sonnet 구현자 + 새 리뷰어)으로 실행한다. 지시문은 [04-prompts.md](04-prompts.md)의 틀을 그대로 쓴다.

**목표:** `docs-db/` 구상과 결정(D1~D27)대로 `j-ai-plugin/docdb/`에 Claude Code·Codex용 문서 DB 플러그인을 만들고, Python 3.10~3.14·Linux에서 전체 테스트가 통과하게 한다.

**구조:** 원본 패키지(`docs-db/reference/docdb-package/`)의 스키마와 LLM 읽기·쓰기 코드를 그대로 이식하고, 그 위에 정규화·정책·이력·관계 규칙·작업 흐름·import·요약을 계약대로 더한다. DB는 `pysqlite3`로 OS SQLite와 무관하게 하고, 실행은 데이터 폴더 venv를 붙이는 표준 라이브러리 부트스트랩으로 시작한다. 에이전트 접근은 MCP 도구 6개와 같은 기능의 CLI다.

**기술:** Python 3.10+, `pysqlite3` 0.6.1(SQLite 3.53.4 내장), `mcp` 2.3.0, `pytest`(개발), `uv`(개발 환경·매트릭스).

**명세:** [../README.md](../README.md)와 `docs-db/01~12` 문서, 결정 정본 [../09-decisions.md](../09-decisions.md), 스키마 [../11-schema.md](../11-schema.md), 기능 [../12-features.md](../12-features.md).

## 이 계획의 원칙

"파일 X를 만들어 값 Y를 넣어라" 식으로 쓰지 않았다. 대신 실행자가 어떤 판단을 내려도 같은 결과가 나오도록 **판단의 근거를 고정**했다.

| 고정한 것 | 문서 | 왜 |
|---|---|---|
| 이름·형식·알고리즘 | [01-contracts.md](01-contracts.md) | 작업마다 다른 실행자가 같은 이름과 같은 출력을 쓰게 하려고 |
| 행동 규율·금지·절차·멈춤 조건 | [00-discipline.md](00-discipline.md) | 모호할 때 추측 대신 멈추고, 범위 밖을 건드리지 않게 하려고 |
| 작업 범위·순서·수용 테스트 | [02-tasks.md](02-tasks.md) | "완료"를 실행자가 아니라 표의 입력→기대로 판정하려고 |
| 리뷰 기준 | [03-review.md](03-review.md) | 리뷰 결과가 리뷰어에 따라 달라지지 않게 하려고 |
| 지시문 틀 | [04-prompts.md](04-prompts.md) | 작업마다 지시가 달라지지 않게 하려고 |

## 읽는 순서

| 역할 | 읽을 것 |
|---|---|
| 사용자(검토) | 이 README → 02-tasks 단계표 → 01-contracts 목차 |
| 오케스트레이터 | 전부. 특히 04-prompts 1절 |
| 구현자 | 00-discipline 전체 → 01-contracts의 해당 절 → 02-tasks의 자기 작업 절 |
| 리뷰어 | 03-review → 00-discipline 3·4절 → 해당 계약 절 → 해당 작업 절 |

## 전역 제약

모든 작업의 요구 사항에 암묵적으로 포함된다.

- Python 3.10 이상. 3.11 이상 전용 기능 금지(C-02).
- 런타임 의존성은 `pysqlite3==0.6.1`, `mcp==2.3.0`뿐. 개발은 `pytest`만 추가(C-02).
- `pysqlite3` import는 `db.py`에만, `mcp` import는 `mcp_server.py`에만, 표준 `sqlite3` import 금지(C-01).
- 모든 쓰기는 `write_tx` 안, 이력 대상 테이블의 모든 변경은 `append_change`(C-08, C-15).
- 모든 자유 텍스트는 LF·NFC 정규화 후 저장, digest는 정규화 값으로(C-06, D19).
- 모든 자유 텍스트 쓰기 전 민감 정보 검사(C-16, D23).
- `local_only` 문서 본문은 에이전트 응답에 싣지 않음(C-13, C-16, D8).
- 에이전트가 쓴 관계는 `proposed`, 확정·반려·확정 관계 삭제는 사람만(C-17, D10).
- AI가 바꿀 수 있는 작업 상태는 planned·active·blocked·review까지(C-18, D24).
- 원본 출력 형식은 한 글자도 바꾸지 않음. 새 줄은 C-13 표의 것만.
- 오류 코드와 종료 코드는 C-05 표의 것만(환경 2, DB 상태 3, 명령 1).
- 시각은 `clock.now_iso()`만(C-07). 네트워크 호출 금지(`setup`의 pip 제외).
- 커밋된 마이그레이션·`docs-db/reference/`·결정 문서는 실행자가 고치지 않음.
- push·원격 작업은 사용자 지시 때만.

## 고위험 입력 (Review Focus)

자세한 표는 [03-review.md](03-review.md) 4절.

1. Windows 줄바꿈·macOS 분해형 유니코드 → 같은 내용은 같은 digest(T02, T12, T21).
2. Claude와 Codex의 동시 쓰기 → 잠금 오류 없이 대기, ID 중복 없음(T04, T08).
3. 본문 속 토큰·키 → 저장 거부, 오류에 값 미노출(T09, T12, T14, T20, T23).
4. 오래된·더 새 DB → 종료 코드 3과 안내, 마이그레이션 실패 시 원상 복구(T05, T11).
5. 이상한 import 입력(코드 블록 속 `#`, BOM, 비 UTF-8, 원본 변경) → 올바른 분할·보고·전부 취소(T21, T22).

## 단계 요약

| 단계 | 작업 | 산출 |
|---|---|---|
| 0 실측 | T00a~T00c | Claude·Codex의 MCP 실행 방식, Windows·휠 없는 플랫폼 결과 |
| 1 기반 | T01~T11 | 패키지 골격, 텍스트·경로·DB·마이그레이션(0001~0008)·이력·ID·행위자·정책·카탈로그·CLI `init` |
| 2 이식 | T12~T13 | 원본 LLM 도구와 원본 테스트(28+33+94) 통과 |
| 3 관계 | T14~T17 | 항목 쓰기, 관계 제안·확정, 규칙 추출 R1·R2·R3·R5, `ctx` 확장 |
| 4 작업 흐름 | T18~T19 | 작업·인계, 사실·적용 판정 |
| 5 증거 | T20 | 실행 결과 적재, R4 |
| 6 import | T21~T22 | 분할·scan·next·label·report |
| 7 요약 | T23 | 템플릿 요약, `sum` |
| 8 진입점 | T24~T26 | 부트스트랩, MCP 서버, 플러그인 파일·마켓플레이스 |
| 9 검증 | T27~T30 | 매트릭스, GitHub Actions(승인 시), 실사용 확인, 문서 반영 |
| 보류 | T31~T34 | gate·승인, 렌더, 내보내기, 보존 정리 — 해당 B·C 결정 후 |

## 실행 전 확인 사항

1. 이 계획의 승인과 실행 방식 선택(04-prompts 1절의 subagent 방식 권장).
2. 작업 트리(브랜치 `feat/docdb`) 생성 허락.
3. T00c·T28은 GitHub push가 필요하다. 승인하지 않으면 Windows·macOS는 "미검증"으로 남는다.

## 결정이 바뀔 때

결정이 바뀌면 오케스트레이터가 ① `09-decisions.md` ② `01-contracts.md`의 해당 절 ③ 영향받는 작업 절의 수용 테스트를 이 순서로 고친 뒤 작업을 배정한다. 실행자는 결정 문서를 고치지 않는다.
