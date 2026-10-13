# 01. 배경 — 원 요구, 원본 패키지, 측정 결과, 발견한 문제

## 1. 출발점

2026-10-08 대화에서 문서 관리 체계를 DB 기반으로 설계했고, 그 결과물이 `docdb-package.zip`이다([reference/docdb-package/](reference/docdb-package/)). 입력 자료는 문서 표준 `project-documentation-standard.md`(DOC-STD-001, 이하 STD)와 카탈로그 `project-documentation-catalogue.yaml`(문서 종류 194개, 이하 CAT)이었으나, **두 파일 모두 현재 없다**(서버 전체 검색 0건, 사용자 확인 2026-10-13).

## 2. 원 요구 (U)

출처: 원문 `docdb-design.md` 1.1절.

| ID | 요구 | 이 구상에서 반영한 곳 |
|---|---|---|
| U-01 | 누가 업무를 받아도 진행할 수 있어야 함 | 인계(`handoff`), 작업 재개 조회(`v_work_resume`), [04](04-llm-access.md) `ctx` |
| U-02 | 지금은 데이터를 쌓고, 팀이 커지면 문서를 한 번에 생성 | 문서는 생성물([02](02-data-model.md) 1절) |
| U-03 | 최대한 스크립트로 처리 | 쓰기 경로 단일화, 트리거, 결정적 추출([03](03-relations.md)) |
| U-04 | 중복 없이 한 곳에 두고 링크로 참조 | 필드 단위 저장, 관계 1테이블([02](02-data-model.md)) |
| U-05 | DB에 저장하고 엮어서 문서를 만드는 방식 | 전체 구조 |
| U-06 | 이력은 링크드 리스트로 관리(가설) | 링크드 리스트+순번+해시 체인([02](02-data-model.md) 6절) |
| U-07 | 많은 문서를 고려한 데이터셋+검수 | 원 카탈로그 부재로 시작 카탈로그([07](07-catalogue.md)) |
| U-08 | LLM 담당 / 스크립트 담당 구분 | [03](03-relations.md) 분담, [05](05-import.md) |
| U-09 | 실제 하드웨어·운영 테스트 관리 | `item(infra_resource)`, `run(hw_check)`, 기대값·관측값 분리 |
| U-10 | 모든 문서를 만들고 관리하려는 것은 아님 | 적용 판정(`kind_applicability`) |
| U-11 | 기준을 스킬 md로 남기기 | 플러그인 스킬([06](06-runtime-plugin.md) 6절) |

## 3. 이어받는 제약 (S)

출처: 원문 `docdb-design.md` 1.2절. 괄호 안 줄 번호는 STD 원문 위치이며, STD는 현재 없다.

| ID | 제약 |
|---|---|
| S-01 | living(현재 기준)과 work(작업 당시 증거)를 분리하고 한 문서에 섞지 않는다 (L36–46, L383) |
| S-02 | 코드·테스트·계약·Migration·IaC는 복제하지 않고 원본 경로를 참조한다 (L184, L336) |
| S-03 | 적용되지 않는 문서를 빈 문서로 일괄 생성하지 않는다 (L188) |
| S-04 | 세 값 논리: 미확인(unknown)을 비적용으로 처리하지 않는다 (L118, L403) |
| S-05 | 원본은 하나, 요약은 원본에 종속된 파생물이다 (L614) |
| S-06 | 종료된 기록은 고치지 않고 정정 기록을 추가한다 (L845) |
| S-07 | AI는 최종 책임자나 승인자가 될 수 없다 (L390, L1104) |
| S-08 | 규칙 해석은 제한된 연산만(eval 금지), 경로는 저장소 상대 POSIX만 (L1020) |
| S-09 | legal_hold(법적 보존) 자료는 삭제하지 않는다 (L416, L847) |
| S-10 | 필수 연결 대상이 비적용이면 N/A+이유+근거를 기록한다 (L527) |
| S-11 | 비밀번호·토큰·키 같은 민감 원문은 저장하지 않는다 (L418) |
| S-12 | 실행 결과는 PASS/FAIL/NOT_RUN/ERROR/UNKNOWN 5가지로 구분한다 (L834) |
| S-13 | 모델이나 담당자가 바뀌어도 같은 기준·변경·검증·잔여 사항을 재현할 수 있어야 한다 (L1243) |

## 4. 원본 패키지 구성

| 파일 | 내용 |
|---|---|
| `docdb-design.md` | 저장 구조 설계(요구·결정·데이터 모델·이력·분담·검수) |
| `docdb-token-spec.md` | LLM 읽기·쓰기 토큰 최소화 명세 |
| `docdb_schema.sql` | 테이블 36, 뷰 8, 트리거 12 |
| `docdb_schema_token_ext.sql` | digest·토큰 추정 컬럼, 요약, 전문 검색 색인, 호출 로그 |
| `docdb_seed_item_types.sql` | 고정 ID 항목 종류 39개(문서 종류 36개를 참조) |
| `docdb_tools/load_catalogue.py` | 카탈로그 적재 |
| `docdb_tools/audit.py` | 표준 항목↔컬럼 매핑 검수 |
| `docdb_tools/test_scenarios.py` | 동작 검증 33건 |
| `docdb_tools/docctl_llm.py` | LLM용 읽기·쓰기 CLI(ctx/get/find/trace/since/put) |
| `docdb_tools/docdb_mcp.py` | 위 6기능의 MCP 서버 래퍼 |
| `docdb_tools/test_token.py`, `test_mcp.py` | 토큰 명세 검증 28건, MCP 검증 |

## 5. 측정 결과 (2026-10-13, 개발 서버)

| 테스트 | 환경 | 결과 |
|---|---|---|
| `test_token.py` | Python 3.12, SQLite 3.26.0(서버 기본) | 실패: `no such tokenizer: trigram` |
| `test_token.py` | Python 3.13.16, SQLite 3.53.1 | 28/28 통과. 원문 수치(작업 시작 7.3%, 재개 1.4% 등) 재현 |
| `test_mcp.py` | 위 + `mcp` 2.3.0 | 통과(도구 6개, 결과 text 1블록) |
| `test_scenarios.py` | 미니 카탈로그, Python 3.6·3.12·3.13 | 32/33. 실패 1건은 "194종 집계"(미니 카탈로그 45종이라 발생, 원문 기재와 같음) |
| `audit.py` | 미니 카탈로그 | 매핑 94/94. 카탈로그 의존 수치(필드 897, 별칭 768, 종류 194)는 재현 불가 |
| 기본 스키마 | SQLite 3.26.0 | 동작. 3.34 이상이 필요한 것은 `trigram` 하나 |
| `pysqlite3` 0.6.1 | Python 3.12 venv | 내장 SQLite 3.53.4, trigram·JSON 동작 |

미니 카탈로그는 `test_token.py`가 만드는 검증용 가짜 데이터다(종류 이름 "D01 문서", 필드 `overview/details/owners` 등).

## 6. 발견한 문제

| # | 문제 | 근거 | 해결 방식 |
|---|---|---|---|
| P1 | 패키지 테스트 미실행 | 압축본 상태 | 5절에서 실행 완료. 플러그인 테스트로 상시 실행([08](08-verification.md)) |
| P2 | 입력 자료(STD·CAT) 없음 | 1절 | 시작 카탈로그([07](07-catalogue.md)), 194종 단정 테스트 일반화 |
| P3 | 원문 설계 문서의 요약이 낡음 | `docdb-design.md:9`, `:21`이 Q1~Q4를 미결로, `:344`는 확정으로 기록 | 결정 상태를 [09-decisions.md](09-decisions.md) 한 곳에만 둔다. 구현 시 원문 0장을 "결정 현황은 7장" 링크로 교체 |
| P4 | 항목 검색 색인의 삭제 트리거 없음 | `docdb_schema_token_ext.sql:78,83`에 추가·수정 트리거만 | `item_fts_ad` 추가(D7) |
| P5 | `export_policy` 미적용 | 컬럼은 있으나(기본 `local_only`) `docctl_llm.py`·`docdb_mcp.py`가 읽지 않음 | 읽기 경로 필터(D8, [04](04-llm-access.md) 6절) |
| P6 | 기존 문서 적재(import) 기능 없음 | 원문 토큰 명세 5.1 스크립트 목록에 없음 | [05-import.md](05-import.md) |
| P7 | 서버 기본 Python으로 실행 불가 | 서버 기본 SQLite 3.26.0에 trigram 없음 | `pysqlite3` 필수(D3) |
| B1 | 관계·항목 쓰기 경로 없음 | `put`은 필드(`f:`)만 지원. `item`·`relation`·`run` 등의 입력 코드는 테스트의 SQL 직접 실행뿐 | [03-relations.md](03-relations.md)(D10) |
| B2 | 필수 연결 검사 없음 | `cat_kind_link`·`link_exemption`은 있으나 누락 연결을 찾는 뷰·검사 없음 | [03-relations.md](03-relations.md) 5절 |
