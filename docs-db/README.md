# docs-db — 프로젝트 문서 DB 구상

| 항목 | 내용 |
|---|---|
| 상태 | 설계(방식 확정) 단계. 구현 전 |
| 최종 갱신 | 2026-10-13 (Asia/Seoul) |
| 결정 정본 | [09-decisions.md](09-decisions.md) (결정 상태는 이 문서 한 곳에만 기록) |
| 작업 기록 | PMT(프로젝트 관리 도구) 프로젝트 `docdb` — `~/docs/projects/docdb/`, 결정 D1~D10, 백로그 B1·B2 |
| 원본 | [reference/docdb-package/](reference/docdb-package/) — 2026-10-08 작성 원문 그대로 보존 |

## 1. 무엇인가

docs-db는 프로젝트 문서를 파일로 쓰지 않고 DB(데이터베이스)에 **데이터 단위로 쌓은 뒤, 필요할 때 문서를 생성**하는 체계다. DB는 SQLite(파일 하나로 동작하는 DB)를 쓴다. AI 에이전트(Claude Code, Codex)는 플러그인으로 연결해 적은 토큰(LLM이 처리하는 글자 단위, 비용과 직결)으로 읽고 쓴다.

```text
 입력                              처리                         원본(DB)                   출력(생성물)
 사람: 사실 확인·승인·관계 확정 ─┐                           ┌ R 규칙(카탈로그)      ┐
 에이전트: 본문·항목·관계 제안 ──┼─▶ docctl ──────────────▶ │ P 프로필(프로젝트 사실) │ ─▶ 문서 md·RTM·상태
 CI: 테스트·배포·점검 결과 ──────┤   (유일한 쓰기 경로:       │ I 문서·필드·항목·관계   │     (필요할 때 생성)
 Git: 코드·계약 파일 참조 ───────┘    검증·이력 기록)        │ W 작업·실행·승인·인계   │
                                                             │ H 변경 이력·기준선      │
 에이전트 읽기: MCP 도구 6개 + CLI ◀── 토큰 최소 응답 ─────── └ D 생성 기록           ┘
```

## 2. 목표

원 요구(2026-10-08 대화에서 정리, 상세는 [01-background.md](01-background.md)):

1. 누가 업무를 받아도 이어서 진행할 수 있다.
2. 지금은 데이터를 쌓고, 팀이 커지면 문서를 한 번에 생성한다.
3. 최대한 스크립트로 처리하고, 스크립트·LLM(대형 언어 모델)·사람의 분담을 나눈다.
4. 중복 없이 한 곳에 저장하고 링크로 참조한다.
5. 모든 문서가 아니라 필요한 문서만 관리한다.

## 3. 문서 지도

| 문서 | 내용 |
|---|---|
| [01-background.md](01-background.md) | 원 요구·제약, 원본 패키지 구성, 측정 결과, 발견한 문제 |
| [02-data-model.md](02-data-model.md) | 무엇을 적재하는가(6계층), 데이터끼리 어떻게 연결되는가(4방식), 이력 구조 |
| [03-relations.md](03-relations.md) | 관계·항목을 쌓는 방식(스크립트 추출·에이전트 제안·사람 확정), 필수 연결 검사 |
| [04-llm-access.md](04-llm-access.md) | 에이전트가 적은 토큰으로 읽고 쓰는 방식(MCP 도구 6개, 핸들, 단계적 공개) |
| [05-import.md](05-import.md) | 기존 문서를 정형 데이터로 옮기는 방식 |
| [06-runtime-plugin.md](06-runtime-plugin.md) | 실행 환경·의존성·플러그인 연결·DB 위치 |
| [07-catalogue.md](07-catalogue.md) | 문서 종류 규칙(카탈로그)과 시작 카탈로그 |
| [08-verification.md](08-verification.md) | 테스트·검증 매트릭스·실측 항목·완료 기준·위험 |
| [09-decisions.md](09-decisions.md) | 결정 기록 전체(원 설계 결정 + 2026-10-13 결정 D1~D10)와 미결 사항 |
| [10-operation.md](10-operation.md) | 예상 동작 방식(설치부터 인계·승인·생성까지 시나리오), 드러난 공백 |
| [11-schema.md](11-schema.md) | DB 스키마 참조(테이블 40·뷰 11·트리거 17·시드, FK 관계, 계획된 변경과 검증된 SQL). 원본 SQL을 적재해 조회한 결과로 생성 |
| [plan/](plan/README.md) | 구현 계획: 실행자 규율, 계약(이름·형식·오류 코드), 단계별 작업, 리뷰 기준 |
| [12-features.md](12-features.md) | 기능 목록(MCP 도구, `put` 연산, CLI 명령, 내부 기능, 테이블별 쓰기 주체, 오류 코드, 권한 경계)과 구현 상태 |

## 4. 핵심 결정 요약

정본은 [09-decisions.md](09-decisions.md)다. 아래는 요약이다.

| # | 결정 |
|---|---|
| D1 | `j-ai-plugin` 저장소 안에 Claude Code·Codex 플러그인으로 둔다 |
| D2 | 원 카탈로그(194종)가 없으므로 시작 카탈로그 18종을 JSON으로 만든다 |
| D3 | Python 3.10 이상, `pysqlite3`(SQLite 3.53.4 내장) 필수, MCP는 공식 `mcp` SDK |
| D4 | 의존성은 `setup` 명령이 venv(격리된 Python 실행 환경)에 설치. 인터프리터는 설정 우선 |
| D5 | DB는 프로젝트별 `.docdb/docdb.sqlite`, git 제외·로컬 전용 |
| D6 | import는 스크립트가 분할·매핑·복사, 에이전트는 라벨만 작성. LLM API 직접 호출 없음 |
| D7 | 항목 삭제 허용, 검색 색인 삭제 트리거 추가 |
| D8 | `export_policy=local_only` 문서 본문은 에이전트에 보내지 않음 |
| D9 | 로컬 Linux Python 3.10~3.14 + GitHub Actions(Windows·macOS) 검증 |
| D10 | 관계: 결정적 추출은 자동 확정, 에이전트가 쓴 관계는 `proposed`로 저장 후 사람이 확정 |
| D11~D25 | A 등급 권장안 일괄 채택(관계 방향, 연결 규칙, 공용 데이터 폴더, 폴더 이름, docctl 범위, 요약 경로, ID 체계, 행위자, 정규화, WAL, 마이그레이션, 백업, 민감 정보, 작업 상태 권한, PMT 경계) |

## 5. 결정 대기 사항

정형적으로 결정할 항목 32건을 [09-decisions.md](09-decisions.md) 4절에 근거·선택지·권장안과 함께 정리했다. 2026-10-13에 A 등급(구현 전 필수) 16건 중 15건을 권장안으로 확정했고(D11~D25), O7은 실측 작업이다. 남은 결정은 B 등급 12건(구현 중 해당 기능을 만들 때)과 C 등급 4건(운영 단계)이다. 이 README에는 목록을 복사하지 않는다.

## 6. 다음 단계

1. 구현 계획 [plan/](plan/README.md) 검토·승인.
2. 계획 0단계에서 실측 S1~S5 수행, 결과로 Codex 설정과 지원 플랫폼 확정.
3. 계획에 따라 구현하고 [08-verification.md](08-verification.md)의 완료 기준 검증.
4. B 등급 결정은 해당 작업 착수 전에 확정.

## 7. 원본 보존 안내

`reference/docdb-package/`는 `~/docs/_tmp/docdb-package.zip`(sha256 `9889792564e2218d0f41e19c84a7a071461d92ecf9454ae8f8a33f318e648abc`)을 그대로 풀어 둔 것이다. 원문 `docdb-design.md`의 0장 요약과 머리표 상태는 결정 상태가 낡았다(원문 7장과 불일치). 결정 현황은 [09-decisions.md](09-decisions.md)를 따른다. 원문의 `[STD]`·`[CAT]` 줄 번호 인용은 입력 자료(`project-documentation-standard.md`, `project-documentation-catalogue.yaml`)를 가리키는데, 이 자료는 현재 없다.

## 8. 용어

| 용어 | 설명 |
|---|---|
| DB / SQLite | 데이터베이스 / 서버 없이 파일 하나로 동작하는 DB 엔진 |
| FK | Foreign Key. 다른 테이블의 행을 가리키는 값. DB가 존재하지 않는 대상을 가리키는 입력을 거부한다 |
| 트리거 | 행을 넣거나 바꿀 때 DB가 자동으로 실행하는 규칙. 이 설계는 규칙 위반을 DB 차원에서 막는 데 쓴다 |
| append-only | 추가만 허용하고 수정·삭제를 막는 방식. 정정은 새 기록으로 남긴다 |
| digest | 내용으로 계산한 짧은 지문(sha256 해시). 내용이 바뀌었는지 본문 없이 비교한다 |
| 카탈로그 | 문서 종류·필드·연결 규칙을 정의한 규칙 데이터 |
| living / work | 현재 기준을 담는 문서 / 특정 작업 당시의 증거를 담는 문서 |
| 항목(item) | REQ-001처럼 고정 ID를 가진 대상(요구사항, 테스트 케이스, 서버 자원 등) |
| RTM | Requirements Traceability Matrix. 요구사항과 그것을 구현·검증하는 대상의 대응표 |
| MCP | Model Context Protocol. AI 에이전트가 외부 도구를 호출하는 표준 규격 |
| LLM / 토큰 | 대형 언어 모델 / 모델이 처리하는 글자 단위. 요청에 실린 양만큼 비용과 문맥 공간을 쓴다 |
| 핸들 | `d:문서`, `f:필드`, `i:항목`처럼 대상을 가리키는 짧은 주소. 내용 대신 주고받는다 |
| FTS / trigram | Full-Text Search(전문 검색) 색인 / 3글자 단위 부분 문자열 검색 방식 |
| venv | 패키지를 시스템과 분리해 설치하는 Python 가상 환경 |
| CI | Continuous Integration. 커밋마다 테스트·빌드를 자동 실행하는 체계 |
| IaC | Infrastructure as Code. 서버·네트워크 구성을 코드로 관리하는 방식 |
| OpenAPI / Migration | API 명세 표준 형식 / DB 구조 변경 스크립트 |
| export_policy | 문서 본문을 외부(LLM 제공자)로 보내도 되는지 정한 값: `public`, `approved_providers`, `local_only` |
