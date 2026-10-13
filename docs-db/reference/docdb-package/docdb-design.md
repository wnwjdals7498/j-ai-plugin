> ⚠️ **【주의】 출처가 없는 정보가 포함되어 있음** — `[설계 판단]` 표시가 붙은 항목은 공식 문서나 입력 자료에 근거가 없는 제 판단입니다. 출처가 있는 내용을 먼저 적고, 설계 판단은 따로 구분해 두었습니다.

# 문서 데이터 DB 설계 — 요구사항·선정 근거·검수 결과

| 항목 | 내용 |
|---|---|
| 문서 ID | `DOCDB-DESIGN-001` (초안) |
| 작성일 | 2026-10-08 (Asia/Seoul) |
| 상태 | draft — DB 엔진과 Git 리뷰 방식이 결정되기 전까지 확정하지 않음 |
| 입력 자료 | `project-documentation-standard.md` (DOC-STD-001, 이하 **[STD]**), `project-documentation-catalogue.yaml` (이하 **[CAT]**) |
| 함께 제공하는 파일 | `docdb_schema.sql`, `docdb_seed_item_types.sql`, `docdb_tools/` (적재·검수·동작 검증 스크립트) |

---

## 0. 요약

1. **원본 위치:** 구조·서술·관계·이력은 **DB가 원본**입니다. 코드·API 계약·Migration·IaC는 **Git이 원본**이며, DB에는 경로·커밋·digest만 참조로 둡니다.
2. **문서는 생성물입니다.** 194종을 저장 방식에 따라 4가지로 나눴습니다. 쿼리로 바로 만드는 것 4종, 항목 대장 8종, 실행·자동 기록 27종, 서술 문서 155종입니다.
3. **이력 구조:** 링크드 리스트는 그대로 쓰되 **순번(seq)과 해시 체인을 더한 append-only 구조**로 보완합니다. 링크드 리스트만으로는 조회, 동시 수정, 위변조 문제가 남기 때문입니다.
4. **검수 결과:** 1차 스키마(v0)에서 누락 24건을 찾았고 보완 후(v1) 점검 항목 94개를 모두 매핑했습니다. 동작 검증 33건도 모두 통과했습니다. 카탈로그 전체(194종, 필드 1,284개, 링크 769개, 별칭 768개)가 FK 위반 없이 적재됩니다.
5. **결정이 필요한 것:** DB 엔진(SQLite로 시작할지), Git PR 리뷰를 유지할지, 별칭 중복 4건, 프로젝트 사실 37개 확인 → [7장](#7-미결-사항--결정-필요)

---

## 1. 요구사항

### 1.1 사용자 요구 (대화에서 정리)

| ID | 요구 | 비고 |
|---|---|---|
| U-01 | 누가 업무를 받아도 진행할 수 있어야 함 | 목표 G1 |
| U-02 | 지금은 데이터를 쌓고, 팀이 커지면 문서를 한 번에 생성 | 목표 G2 |
| U-03 | 최대한 스크립트로 처리 | 목표 G3 |
| U-04 | 중복 없이, 데이터는 한 곳에 두고 링크로 참조 | 목표 G4 |
| U-05 | DB에 저장하고 엮어서 문서를 만드는 방식 | 아키텍처 |
| U-06 | 이력은 링크드 리스트로 관리 (가설) | 검증 대상 → [4장](#4-이력-구조--링크드-리스트-가설-검증) |
| U-07 | 최대한 많은 문서를 고려한 데이터셋 + 검수 | → [6장](#6-검수-결과) |
| U-08 | LLM 담당 / 스크립트 담당 구분 | → [5.3](#53-스크립트--llm--사람-분담) |
| U-09 | 프로덕션 환경: 실제 하드웨어·운영 테스트 관리 | 자원 항목 + 실행 증거 |
| U-10 | 모든 문서를 만들고 관리하려는 것은 아님 | 적용 판정으로 필요한 것만 |
| U-11 | 기준을 스킬 md로 남기기 | 다음 단계 |

### 1.2 입력 자료에서 이어받는 제약

| ID | 제약 | 근거 |
|---|---|---|
| S-01 | living(현재 기준)과 work(작업 당시 증거)를 분리하고, 한 묶음에 섞지 않음 | [STD] L36–46, L383 |
| S-02 | 코드·테스트·계약·Migration·IaC는 옮기거나 복제하지 않고 실제 원본 경로를 참조 | [STD] L184, L336 |
| S-03 | 적용되지 않거나 채택하지 않은 문서는 빈 문서로 일괄 생성하지 않음 | [STD] L188 |
| S-04 | 세 값 논리: 미확인(unknown)을 비적용으로 처리하지 않음 | [STD] L118, L403 |
| S-05 | 원본은 하나만 두고, 요약은 원본에 종속된 파생물 | [STD] L614 |
| S-06 | 종료된 기록은 고치지 않고 정정 기록을 추가 | [STD] L845 |
| S-07 | AI는 최종 책임자나 승인자가 될 수 없음 | [STD] L390, L1104 |
| S-08 | 규칙 해석은 제한된 연산만 허용(eval 금지), 경로는 저장소 상대 POSIX만 | [STD] L1020 |
| S-09 | legal_hold 자료 삭제 금지 | [STD] L416, L847 |
| S-10 | 필수 연결 대상이 비적용이면 N/A + 이유 + 프로필 근거를 기록 | [STD] L527 |
| S-11 | 비밀번호·토큰·키 같은 민감 원문은 저장하지 않음 | [STD] L418 |
| S-12 | 실행 결과는 PASS/FAIL/NOT_RUN/ERROR/UNKNOWN 5가지로 구분 | [STD] L834 |
| S-13 | 모델이나 담당자가 바뀌어도 같은 기준·변경·검증·잔여 사항을 재현할 수 있어야 함 | [STD] L1243 |

> **입력 자료와 다른 점:** 원안은 `meta.yaml` 파일을 단일 기준으로 삼고 색인은 그 파생물로 둡니다([CAT] L5, [STD] L195, L358). 이 설계는 U-05에 따라 **DB를 원본으로 뒤집었습니다.** 그 결과 사라지는 기능(Git PR 리뷰, 보호 브랜치 승인 [STD] L792)을 대신할 방법은 DEC-01과 Q2에 적었습니다.

### 1.3 요구 → 설계 → 검증 추적

| 요구 | 설계 결정 | 검증 (6.4절 시나리오) |
|---|---|---|
| U-01 이어받기 | DEC-05 항목·관계, DEC-06 이력, `handoff`, `role_assignment` | B03 최종 책임 단일화, 이력 순회·N번째 조회 |
| U-02 데이터 먼저 | DEC-01, DEC-02, `generation` | RTM·등록부 뷰 |
| U-03 스크립트화 | DEC-08 쓰기 경로 단일화, 규칙 평가기, 트리거 12개 | AT-01~03, AT-29, AT-14 |
| U-04 중복 제거 | DEC-05 관계 1테이블, DEC-09 기본값 계산 | RTM 쿼리 생성 |
| U-05 DB → 문서 | DEC-01, DEC-02 | 카탈로그 전체 적재 |
| U-06 링크드 리스트 | DEC-06 | 분기·위변조·수정·삭제 차단 |
| U-07 커버리지 | 6장 검수 | 매핑 94/94 |
| U-09 하드웨어·운영 테스트 | `item(infra_resource)`, `run(hw_check)`, `env_snapshot` | RUN-0001 덮어쓰기 차단 |
| U-10 필요한 것만 | `kind_applicability` 규칙 평가 | 194종 판정 집계 |

---

## 2. 설계 결정 — 목적·이유·근거

### DEC-01 원본 위치: DB와 Git을 함께 쓰는 하이브리드

- **결정:** 메타, 서술 필드, 항목, 관계, 작업, 증거, 이력은 DB에 둡니다. 코드, OpenAPI/AsyncAPI, Migration, IaC는 Git에 두고, DB의 `artifact` 테이블에서 경로·커밋·digest로 참조합니다.
- **목적:** U-04(중복 제거)와 U-05(DB 기반)를 지키면서, 실행 가능한 원본을 둘로 나누지 않기 위해서입니다.
- **이유:** 실행 파일을 문서용으로 복제하면 원본이 두 개가 됩니다. 이는 [STD] L184("원본과 별도 복사본을 각자 편집하지 않는다")와 L336에 어긋납니다.
- **버린 대안:** (a) 모두 DB에 넣는 방식은 Migration·IaC를 실행하는 도구 체인과 단절됩니다. (b) 모두 파일에 두는 방식(원안)은 194종의 메타가 파일마다 반복되고, 사용자 요구 U-05와 맞지 않습니다.
- **잃는 것과 보완:** Git PR 리뷰를 쓸 수 없게 되므로 `approval` 테이블과 `external_ref`(결재·PR ID)로 대신합니다. Git diff 리뷰를 유지할지는 Q2에서 정합니다.

### DEC-02 문서는 생성물로 다룸 — 종류별 저장 방식 4분류

| 저장 방식 | 종류 수 | 저장 위치 | 예 |
|---|---:|---|---|
| `view` 쿼리로 생성 | 4 | 다른 테이블을 조회해서 만듦. 직접 작성하지 않음 | A01 문서 마스터 목록, D08 RTM, F06 의존성·결정 색인, A03 템플릿·스키마(필요 시 생성) |
| `register` 항목 대장 | 8 | `item` 행 | A04 용어, B04 RAID, F07 부채, L07 결함, M09 취약점, S03 자산 등 |
| `run` 실행·자동 기록 | 27 | `run` + `finding` + `artifact` + `context_read` | L03 테스트 결과, R04 백업 증거, K07 드리프트 점검, O05 릴리스, V05 컨텍스트 잠금 등 |
| `narrative` 서술 | 155 | `field_value` (필드 단위). 서술 문서도 항목을 정의할 수 있음 | D01 요구사항, K02 서버 목록, P06 Runbook 등 |

- **근거:** [STD] L126("인덱스는 자동 생성 가능"), L127("대형 수기 엑셀 파일 자체가 필수인 것은 아니다").
- **분류 규칙 [설계 판단]:** A01·D08은 view로 지정했습니다. work 종류 중 `evidence.json`·`results.json`·`run.json`·`report.json`·`findings.json`이 있으면 run, `detail.md`가 없으면 register, 나머지는 narrative입니다. A03(템플릿·스키마)은 실제로는 DB의 `cat_*` 계층 자체이고, V05(컨텍스트 잠금)는 관계 + 생성 기록입니다. 이 두 종류는 분류가 애매하므로 확정이 필요합니다.

### DEC-03 데이터 계층 6개

| 계층 | 테이블 | 담는 것 | 대체하는 원안 파일 |
|---|---|---|---|
| R 규칙 | `cat_version` `cat_group` `cat_fact` `cat_kind` `cat_kind_field` `cat_kind_link` `cat_kind_alias` `cat_item_type` | 카탈로그 (종류·필드·연결·조건·별칭) | `catalogue.yaml` |
| P 프로필 | `project` `fact_value` `kind_selection` `policy_exception` `kind_applicability` | 프로젝트 사실(세 값), 선택·승격, 예외, 적용 판정 | `profile.yaml`, `fact-registry.yaml` |
| I 실체 | `actor` `document` `document_kind` `doc_reviewer` `doc_tag` `doc_scope` `field_value` `item` `role_assignment` `artifact` `doc_artifact` `relation` `link_exemption` | 문서 메타, 본문 필드, 고정 ID 항목, 파일 참조, 관계 | `meta.yaml`, `detail.md`, 각종 `*.yaml` 대장 |
| W 작업·증거 | `work` `work_event` `run` `finding` `approval` `handoff` | 작업, 사건 사실, 실행 증거, 지적, 승인, 인계 | `work.yaml`, `evidence.json`, `handoff.yaml` |
| H 이력 | `change_log` `doc_revision` | 세부 변경 이력, 문서 기준선 | Git 이력, `record.yaml` |
| D 파생 기록 | `generation` | 생성물의 원본 digest, 생성기, 최신 여부 | `brief.en.md` 헤더, `document-register.yaml` |

전체 테이블 36개, 뷰 8개, 트리거 12개입니다(`docdb_schema.sql`).

### DEC-04 본문은 필드 단위로 저장 (`field_value`)

- **결정:** 카탈로그의 `required_fields`/`optional_fields` 한 개를 행 하나로 저장합니다. 형식은 `md`(서술), `json`(구조), `ref`(Git 원본의 절·JSON Pointer 참조) 중 하나입니다.
- **목적:** 필드가 누락됐는지를 쿼리로 검사하기 위해서입니다(`v_missing_required_fields`). 생성 문서의 절 순서는 `cat_kind_field.sort_order`를 따릅니다.
- **이유:** 원안의 `field_bindings`([STD] L356, L397)는 파일 위치를 가리키는 간접 참조입니다. DB에서는 값 자체를 저장하면 위치를 검증하는 단계가 사라집니다.
- **강제:** 카탈로그에 없는 필드 키는 FK로 거부합니다. `tbd`·`unknown`·`not_applicable` 상태는 사유가 있어야 합니다([STD] L579 "TBD를 숨기지 않음").

### DEC-05 고정 ID 항목(`item`)과 관계 1테이블(`relation`)

- **결정:** REQ, AC, SCR, API, TC, ERR, RISK, 자원, 서비스 등 ID를 가진 대상은 `item` 행으로 저장합니다. 모든 연결은 `relation` 한 곳에 12개 관계 타입으로 저장합니다.
- **목적:** U-04입니다. 같은 사실을 여러 문서에 복사하지 않고 연결만 둡니다. RTM·의존성 맵·영향 분석은 이 테이블을 조회해서 만듭니다.
- **근거:** 관계 타입 12종과 최소 필드는 [STD] L508–523, 추적 사슬은 [STD] L531–541에 있습니다.
- **항목 종류 39개 [설계 판단]:** 카탈로그 필수 필드 중 ID를 요구하는 필드 65개에서 추출했습니다(`docdb_seed_item_types.sql`). 카탈로그에는 없는 확장이므로 확정이 필요합니다. 하드웨어는 `infra_resource`(`RES-`, K02)와 `asset`(`AST-`, S03)로 나눴습니다.
- **기대값과 관측값 분리:** `item.attrs`에는 기대값(desired)만 둡니다. 실제 관측값은 `run`(점검)과 `finding`(차이)에 둡니다. 근거는 [STD] L63의 `baseline_mode desired/observed`입니다.

### DEC-06 이력: 링크드 리스트 + 순번 + 해시 체인 → [4장](#4-이력-구조--링크드-리스트-가설-검증)

### DEC-07 DB 엔진: SQLite로 시작하고 PostgreSQL로 옮겨갈 경로를 열어 둠 (결정 필요)

| 관점 | SQLite | PostgreSQL | 근거 |
|---|---|---|---|
| 동시 쓰기 | 쓰기는 한 번에 하나 | 서버가 동시 쓰기를 조정 | SQLite whentouse §2·§3 |
| 행 단위 접근통제 | 없음 | Row Security Policy | PostgreSQL §5.9 |
| 재귀 쿼리(이력 순회) | 지원 | 지원 | SQLite lang_with §3, PostgreSQL §7.8.2 |
| JSON | 3.38.0부터 기본 내장 | 지원 | SQLite json1 §2 |
| 운영 부담 | 파일 하나 | 서버 필요 | — |

- **권장 [설계 판단]:** 지금은 1~소수 인원이 쓰고, 쓰기를 스크립트(docctl) 한 경로로 모으므로 **SQLite로 시작**합니다. 다음 둘 중 하나가 생기면 PostgreSQL로 옮깁니다. (a) 여러 사람·CI가 동시에 쓰기 시작할 때, (b) `classification=restricted` 문서를 사용자별로 숨겨야 할 때([STD] L751 "허용되지 않은 문서는 이름·요약·검색 점수도 노출하지 않는다").
- **이식성:** 테이블·CHECK·FK·UNIQUE는 표준 SQL 위주로 작성했습니다. SQLite 전용 부분은 `json_valid`, `group_concat`, `GLOB`, 트리거의 `RAISE` 구문이며, PostgreSQL로 옮길 때 트리거는 PL/pgSQL 함수로 다시 써야 합니다. **PostgreSQL에서는 실행해 보지 않았습니다.**
- **주의:** SQLite는 FK 제약이 기본으로 꺼져 있고 연결마다 켜야 합니다(SQLite foreignkeys §2). 따라서 docctl은 매 연결마다 `PRAGMA foreign_keys=ON`을 실행해야 합니다.

### DEC-08 쓰기 경로 단일화 + DB 제약으로 강제

- **결정:** 모든 쓰기는 docctl(스크립트)을 거칩니다. 스크립트가 `change_log`와 digest를 기록합니다. DB 제약으로 막을 수 있는 규칙(append-only, AI 승인 금지, 생명주기 혼합 금지, legal_hold, 경로 탈출, ID 접두어)은 트리거·CHECK로 **DB 차원에서도** 막습니다.
- **이유:** 스크립트에 버그가 있거나 누군가 SQL을 직접 실행해도 핵심 규칙이 깨지지 않게 하려는 것입니다. 원안의 오류 코드와 인수 테스트([STD] L974–987, L1202–1231) 중 DB로 강제할 수 있는 것을 옮겼습니다.
- **스크립트에 남는 것:** digest 계산, 규칙 평가(세 값 논리), supersedes 순환 검사([STD] L562), 의미 검사 요청. SQLite에는 기본 sha256 함수가 없어서 digest는 스크립트가 계산합니다.

### DEC-09 중복 저장 금지 규칙

- `owner_role`과 `review_days`는 문서마다 저장하지 않고 `cat_kind`/`cat_group` 기본값에서 계산합니다. 문서에는 다르게 정한 값(override)만 저장합니다.
- 파일 내용은 저장하지 않고 `artifact`(경로·커밋·digest)만 둡니다.
- 생성물(md, brief, 등록부)은 파일로 만들고, DB의 `generation`에는 "언제, 무엇으로, 어떤 원본 digest에서 만들었는지"만 둡니다.
- 문서 기준선의 `doc_revision.snapshot_json`은 중복처럼 보이지만, 승인 대상을 고정하려는 **불변 사본**입니다. 현재 값의 원본이 아닙니다.

---

## 3. 데이터 모델 개요

```text
cat_group ─< cat_kind ─< cat_kind_field          (R: 카탈로그)
               │  └─< cat_kind_link / cat_kind_alias
               └─< cat_item_type
project ─< fact_value (cap.*)   project ─< kind_applicability (파생 캐시)     (P)
document ─< document_kind >─ cat_kind                                      (I)
   │  ├─< field_value  (doc × kind × field)
   │  ├─< doc_scope >─ item ─< role_assignment >─ actor
   │  ├─< doc_artifact >─ artifact (Git·외부 참조)
   │  └─< link_exemption (필수 연결 N/A)
relation : (document|item|artifact|work|run) ──type──▶ (document|item|artifact|work|run)
work ─< work_event (evt.*) / run ─< finding / handoff / approval           (W)
change_log : entity별 prev → prev → ... (seq, digest 체인)                  (H)
doc_revision : doc별 rev1 ← rev2 ← rev3 (승인 대상) ── approval
generation : 생성물 기록 (원본 digest, 생성기, FRESH/STALE)                 (D)
```

---

## 4. 이력 구조 — 링크드 리스트 가설 검증

**가설(U-06):** "이력은 링크드 리스트로 만들면 관리될 것 같다."
**판정:** **부분적으로 맞습니다.** 순서를 표현하고 위변조를 막는 데는 링크가 유리합니다. 다만 링크드 리스트만 쓰면 아래 문제가 남아서 보완이 필요합니다.

| 관점 | 링크드 리스트만 쓸 때 | 보완 | 근거 |
|---|---|---|---|
| 전체 이력 조회 | 재귀 쿼리 필요 | `WITH RECURSIVE` 사용 (지원됨) | SQLite lang_with §3 "walks a tree or graph", PostgreSQL §7.8.2 |
| N번째·특정 버전 조회 | 처음부터 순회 | `seq` 번호 + `UNIQUE(entity, seq)` | [설계 판단] |
| 최신값 조회 | 꼬리를 탐색해야 함 | 현재 상태는 `document`·`field_value`에 따로 두고, 이력은 감사·재현용 | [설계 판단] |
| 동시 수정 | 두 행이 같은 prev를 가리켜 분기 | `UNIQUE(prev_change_id)`. 첫 행의 NULL은 여러 개 허용 | SQLite lang_createtable §3.6 "NULL values are considered distinct" |
| 위변조 | 감지 못함 | 각 행에 `prev_digest` 저장 + 삽입 트리거로 체인 검증 | [설계 판단] / 근거 개념 [STD] L495–499 |
| 수정·삭제 | — | append-only 트리거(`RAISE(ABORT)`), 정정은 `op='correct'` 행 추가 | SQLite lang_createtrigger §6, [STD] L845 |
| 순환 | — | supersedes·generated_from은 스크립트가 재귀 쿼리로 순환 검사 | [STD] L562, PostgreSQL §7.8.2.2 |

**이력은 두 층으로 둡니다 [설계 판단]**

| 층 | 테이블 | 답하는 질문 | 링크 |
|---|---|---|---|
| 세부 변경 | `change_log` | 누가, 언제, 어떤 필드를 무엇에서 무엇으로 바꿨나 | 엔터티별 prev 링크 |
| 기준선 | `doc_revision` | 승인된 버전 N의 내용은 무엇이었나 | 문서별 prev 링크 |
| 문서 간 대체 | `relation(supersedes)` | 이 결정·문서를 무엇이 대체했나 | 문서 간 링크 ([STD] L519) |

---

## 5. 아키텍처

### 5.1 구성

```text
 입력                         처리                    원본(SoT)                 출력(파생물, 직접 편집 금지)
 사람: 사실 확인·승인  ─┐                        ┌── DB ─────────────┐
 LLM : 서술 필드 초안  ─┼─▶ docctl (스크립트) ──▶│ R 카탈로그         │──▶ docctl render ─▶ 문서 md/html
 CI  : 테스트·배포·점검 ─┤   · 입력 검증          │ P 프로필           │                    brief.en.md
 Git : diff·커밋       ─┘   · 규칙 평가(3값)     │ I 문서·항목·관계   │                    등록부·RTM
                            · change_log·digest  │ W 작업·증거        │                    status.json
                                                 │ H 이력             │                    context package
                                                 └────────┬──────────┘
                                                          │ 경로+커밋+digest 참조만
                                                 Git: 코드·OpenAPI·Migration·IaC (원본 유지)
```

### 5.2 흐름

1. **적재:** `catalogue.yaml`을 R 계층에 적재합니다(`load_catalogue.py`, 구현·검증 완료).
2. **프로필:** 미확인 `cap.*` 사실 목록을 스크립트가 만들고, 사람이 답합니다. 그 결과로 `kind_applicability`를 다시 계산합니다.
3. **작업:** `work`를 생성하면 Git diff·선언을 근거로 `work_event`(evt.*)를 채우고, 필수 기록 종류를 판정합니다.
4. **작성:** 필드는 LLM 초안 또는 사람이 쓰고, 항목·관계는 원본에서 추출합니다. 모든 쓰기는 `change_log`에 기록됩니다.
5. **검증:** CI 결과는 `run`에, 지적은 `finding`에 적재합니다. 문서는 gate 검사 후 `doc_revision`을 고정하고 사람이 `approval`합니다.
6. **생성:** 필요할 때 `render`로 문서·brief·등록부를 출력하고 `generation`에 기록합니다. 원본 digest가 바뀌면 STALE로 표시합니다.

### 5.3 스크립트 / LLM / 사람 분담

근거: 구조·참조 검사는 자동, 의미·승인은 사람([STD] L599–607). docctl 필수 기능은 [STD] L860–875에 있습니다. 표의 분담 비율은 **[설계 판단]**입니다.

| 계층 | 스크립트 | LLM | 사람 |
|---|---|---|---|
| R 카탈로그 | 적재·버전 관리 전부 | — | 카탈로그 변경 승인 |
| P 프로필 | 미확인 사실 질문 목록, 규칙 평가 | 코드 스캔 결과로 근거 후보 제시 | 사실 확정 |
| I 메타 | 기본값 계산, ID 형식 검증, 생명주기 검사 | — | 소유자 지정 |
| I 서술 필드 | 누락 탐지, 템플릿 생성 | 초안 작성 | 의미 검토 |
| I 항목·관계 | OpenAPI operation, Migration 테이블, 인벤토리, 테스트 ID 추출 | 추출하지 못한 관계 후보 제안 | 확정 |
| W 실행 증거 | CI·점검 결과 자동 적재 | 결과 요약 | 수동 점검 기록 |
| W 승인 | gate 검사 | — (DB에서 차단) | 승인 |
| W 인계 | 증거·미완료 집계로 초안 생성 | 서술 보완 | 수신 확인 |
| H 이력 | 전부 | — | — |
| D 생성물 | 문서·RTM·등록부 렌더링 | brief.en 초안 (선택) | 고위험 brief 검토 |

---

## 6. 검수 결과

### 6.1 방법

1. **적재 검사:** [CAT] 전체를 스키마에 적재하고 FK 위반이 있는지 확인했습니다(`load_catalogue.py`).
2. **매핑 검사:** [STD]의 데이터 요구 항목을 `테이블.컬럼`에 대응시키고, 그 컬럼이 실제로 있는지 스크립트로 확인했습니다(`audit.py`).
3. **필드 분류:** 카탈로그 필수 필드 897개가 어느 테이블에 저장되는지 키워드 규칙으로 분류했습니다.
4. **동작 검증:** 인수 테스트([STD] L1202–1231) 중 DB로 강제할 수 있는 것과 이력 구조를 실제로 실행했습니다(`test_scenarios.py`, SQLite 3.45.1).

### 6.2 적재 결과 — 카탈로그 전체 수용

| 대상 | 수 | 결과 |
|---|---:|---|
| 영역(group) | 26 | 적재 |
| 사실(cap 38 + evt 53) | 91 | 적재 (`cap.integration`은 OR 파생 규칙) |
| 문서 종류 | 194 | 적재 (living 139 / work 55) |
| 필드(필수 897 + 선택 387) | 1,284 | 적재 |
| 종류 간 연결(필수 194 + 선택 575) | 769 | 적재 |
| 포함 명칭(별칭) | 768 | 적재 |
| FK 위반 | 0 | — |

### 6.3 매핑 검수 — v0에서 누락 24건 → v1에서 0건

| 점검 범위 | 근거 | v0 | v1 |
|---|---|---:|---:|
| 공통 메타데이터 5.1·5.2 | [STD] L379–416 | 39/55 | **55/55** |
| 실행 증거 최소 요건 9.4 | [STD] L831–837 | 10/14 | **14/14** |
| 인수인계 9.3 | [STD] L797–822 | 10/11 | **11/11** |
| status·check·오류 코드 10.3–10.5 | [STD] L917–987 | 3/6 | **6/6** |
| 추가 점검 (RACI·N/A·하드웨어·관측·프로필 버전·별칭) | [STD] L527, [CAT] B03·K02·K07·S04 | (v1에서 추가) | **8/8** |
| **합계** | | 62/86 | **94/94** |

**v0에서 빠졌다가 v1에서 추가한 것:**

- 메타: 문서↔파일 연결(`doc_artifact`), review 3필드, applicability 3필드, `work.result_refs`, `generation.input_digests`, `archive_reason`, brief 면제 2필드, `doc_tag`(tags·aliases), `origin_work_id`, `extensions`
- 증거: `target_versions`(데이터·스키마·프롬프트 버전), `env_snapshot`, 증거 보존 정책, `attestation_ref`
- 인계: `ack_checked_baseline`, `ack_checked_evidence`
- 검사: `finding` 테이블(코드·심각도·조치 상태)
- 추가: `link_exemption`, `kind_applicability`, `role_assignment`(단일 최종 책임), `cat_item_type`, `project.profile_revision`, `cat_kind_alias.is_primary`

### 6.4 카탈로그 필수 필드 897개의 저장 위치

| 저장 위치 | 필드 수 | 예 |
|---|---:|---|
| `field_value` (서술·구조 값) | 680 | D01.behavior, P06.steps |
| `item` (고정 ID 항목) | 65 | D01.requirement_ids, K02.resources, L02.test_ids |
| `run`/증거 | 73 | L03.execution, R04.verification |
| `actor`/`role_assignment` | 30 | B03.accountable, P01.owners |
| `approval` | 26 | B05.approval, D07.approval |
| `relation` | 23 | D08.edges, J05.edges, F02.dependencies |

> 분류는 필드 키와 설명의 키워드 규칙으로 만든 **근사치**입니다 [설계 판단]. `field_value`는 범용 테이블이어서 897개 모두 **저장은 가능**합니다. 이 분류는 링크와 검증이 가능한 전용 구조로 몇 개가 올라가는지를 보여 줍니다.

### 6.5 동작 검증 33건 — 전부 통과 (AI 협업 조회 7건은 7.3 결정 후 추가)

| 구분 | 시나리오 | 결과 |
|---|---|---|
| 적용 판정 | AT-01 DB=true → I01 필수 / AT-02 UI=false → E01 비필수 / AT-03 production=NULL → F04 미확인 | 통과 |
| 적용 판정 | 194종 판정 집계 (true 47 / false 24 / unknown 123) | 통과 |
| 문서 | AT-04 doc_id 중복 / AT-29 living 문서에 work 종류 혼합 | 차단 |
| 필드 | AT-05 필수 필드 누락 탐지 (behavior·acceptance·scope·owners) | 탐지 |
| 필드 | 사유 없는 tbd / 카탈로그에 없는 필드 키 | 차단 |
| 항목·관계 | ID 접두어 불일치 차단 / RTM을 쿼리로 생성 (REQ-HW-001 → TC-HW-001) | 통과 |
| 경로 | AT-17 `../` 탈출 / 절대경로 | 차단 |
| 증거 | AT-14 실행 결과 덮어쓰기 / 5종 외 결과값 | 차단 |
| 이력 | 재귀 역순 순회 (3→2→1) / seq로 N번째 직접 조회 | 통과 |
| 이력 | 분기(같은 prev) / 해시 체인 위조 / 이력 수정 / 이력 삭제 | 차단 |
| 승인 | AI 승인 차단 / 사람 승인 기록 | 통과 |
| 보존·책임 | AT-23 legal_hold 삭제 / B03 최종 책임(A) 중복 | 차단 |
| 무결성 | FK 무결성 검사 | 통과 |

### 6.6 검수 중 발견한 사항

| # | 발견 | 영향 | 조치 |
|---|---|---|---|
| F-1 | 별칭 중복 4건: '리소스 가용성 계획'(B02·B03), '작업 환경'(G01·K01), '제품 성공 기준'(C01·C06), '환경별 설정 비교표'(K07·S04) | 기존 문서 이름을 종류에 매핑할 때 모호해짐 | `is_primary`로 대표 1개 지정 (임시, Q3에서 확정) |
| F-2 | 필수 연결 194개 중 110개는 대상이 조건부·선택 종류 | 비적용일 때 N/A 기록이 없으면 gate가 막힘 | `link_exemption` 추가 |
| F-3 | 사실 3개만 입력한 상태에서 living 139종 판정: 필수 47 / 비필수 14 / 미확인 78. 미확인 78종은 `cap.*` 사실 36개(직접 답할 것 35개 + OR로 계산되는 `cap.integration`)에만 의존하고, living 종류 중 `evt.*`에 의존하는 것은 0개 | **질문 35개에 답하면 living 문서의 필수 여부가 모두 자동으로 정해짐** (선택 문서 채택 여부는 별도). ※ 35개는 테스트에서 2개를 가정 입력한 뒤의 수치. 실제로는 아무것도 확정되지 않았으므로 직접 답할 사실은 **37개**(cap 38개 − 파생 1개) | 질문 목록 자동 생성 (Q5) |
| F-4 | work 55종 중 45종은 프로젝트 단계에서 미확인 | 정상입니다. `evt.*`는 작업마다 판정 | 없음 |
| F-5 | 원안은 meta.yaml이 원본, 이 설계는 DB가 원본 | Git PR 리뷰를 쓸 수 없게 됨 | Q2 |
| F-6 | A03·V05·F06은 4분류 중 어디에 넣을지 애매 | 생성 방식이 달라짐 | DEC-02 주석, 확정 필요 |

### 6.7 검수의 한계

- 매핑 점검표 94개는 제가 [STD]에서 뽑은 항목입니다. **표준 전체를 한 문장씩 대조한 결과는 아닙니다.**
- 필드 분류(6.4)는 키워드 규칙에 의한 근사치입니다.
- 검증하지 않은 것: 의미 검사, 성능, PostgreSQL 실행, 실제 프로젝트 문서 적재, 종류별 `item.attrs` JSON Schema(아직 없음).
- 26개 영역의 문서 내용이 실무에 충분한지는 검증 범위 밖입니다.

---

## 7. 결정 기록 / 미결 사항

### 7.1 확정 (2026-10-08, 주정민)

| # | 결정 | 반영 |
|---|---|---|
| Q1 | SQLite로 시작하고, DEC-07 조건이 생기면 PostgreSQL로 이전 | — |
| Q2 | 초기에는 DB만 사용(승인은 `approval`), 팀이 커지면 YAML 내보내기 추가 | — |
| Q3 | 별칭 대표 종류: 리소스 가용성 계획=B03, 작업 환경=G01, 제품 성공 기준=C01, 환경별 설정 비교표=S04 | `docdb_seed_item_types.sql` |
| Q4 | 항목 종류 39개와 접두어를 초안대로 승인 | `docdb_seed_item_types.sql` |
| D-A03 | 템플릿·스키마는 **작성 양식과 담을 값의 정의만** `cat_*`에 관리하고, 필요할 때 생성 → `view` | 적재 스크립트 |
| D-F06 | 의존성·현재 결정 색인은 **`relation`에 값으로만 관리**하고 작성하지 않음 → `view` | 적재 스크립트 |
| D-V05 | 컨텍스트 잠금은 스크립트가 자동 기록(`context_read`)하고 작성하지 않음. **AI 협업용 조회(최근 값·이력·인계·재개)를 강조** → 7.3 | 스키마 |

변경 후 저장 방식 분류: view 4 / register 8 / run(자동 기록 포함) 27 / narrative 155. 스키마는 테이블 36, 뷰 8, 트리거 12이고, 동작 검증은 33건 모두 통과했습니다.

### 7.2 미결

| # | 결정 사항 | 상태 |
|---|---|---|
| Q5 | `cap.*` 사실 37개 | 답변을 받았으나 **범위 확인 중**: 문서관리 도구 자체에 대한 답과 관리 대상 레포에 대한 답이 섞여 있음 |
| Q6 | 종류별 `item.attrs` 스키마 | K02·S03·L02부터 정의 |

### 7.3 AI 협업 조회 (D-V05)

| 질문 | 조회 | 데이터 |
|---|---|---|
| 이 값의 최신은? | `v_entity_head` | `change_log` 링크드 리스트의 head |
| 최근에 누가(사람/AI) 무엇을 바꿨나? | `v_recent_changes` | `change_log` + `actor.actor_type` |
| 이 값은 어떻게 변해 왔나? | 재귀 순회 / seq 직접 조회 | `change_log` |
| 이 작업을 이어받으면 뭘 해야 하나? | `v_work_resume` | 마지막 인계·다음 행동·미해결 지적·미실행/실패 실행·낡은 근거 수 |
| AI가 읽은 근거가 그 뒤에 바뀌었나? | `v_context_stale` | `context_read.last_change_id` vs 최신 변경 |
| 인계를 확인했나? | `handoff.ack_*` | 기준 커밋을 확인하지 않으면 수신 확인 불가 (CHECK) |

**다음 단계:** docctl 기능 목록(스크립트 범위) → 스킬 md(U-11)

---

## 8. 출처

### 입력 자료 (업로드 파일)

| 표기 | 파일 | 인용 위치 |
|---|---|---|
| [STD] | `project-documentation-standard.md` | L36–46 living/work · L63 baseline_mode · L118 세 값 논리 · L126–127 등록부·RTM · L184 원본 복제 금지 · L188 빈 문서 금지 · L195 등록부 파생 · L336 실행 파일 복제 금지 · L356–358 field_bindings·meta.yaml 단일 기준 · L379–416 공통 메타 · L418 비밀 금지 · L495–499 digest · L508–527 관계·N/A · L531–541 추적 사슬 · L562 순환 금지 · L579 TBD · L599–607 검증 5단계 · L614 원본 1개 · L751 ACL · L792 보호 브랜치 · L797–837 인계·증거 · L845–847 정정·보존 · L860–875 docctl 기능 · L917–987 status·오류 코드 · L1020 제한 연산 · L1104 자동 승인 금지 · L1202–1231 인수 테스트 · L1243 완료 기준 |
| [CAT] | `project-documentation-catalogue.yaml` | L5 metadata_strategy · L7–163 groups · L164–257 facts · L258~ document_types (194종) |

### 외부 공식 문서 (2026-10-08 확인)

| 근거 | URL | 위치 |
|---|---|---|
| SQLite 쓰기 동시성 | https://www.sqlite.org/whentouse.html | §2 "High Concurrency" — "SQLite supports an unlimited number of simultaneous readers, but it will only allow one writer at any instant in time." · §3 item 2 "Many concurrent writers? → choose client/server" |
| SQLite 재귀 CTE | https://www.sqlite.org/lang_with.html | §3 "Recursive Common Table Expressions" — "A recursive common table expression can be used to write a query that walks a tree or graph." |
| SQLite UNIQUE와 NULL | https://www.sqlite.org/lang_createtable.html | §3.6 "UNIQUE constraints" — "NULL values are considered distinct from all other values, including other NULLs." |
| SQLite 트리거 RAISE | https://www.sqlite.org/lang_createtrigger.html | §6 "The RAISE() function" — RAISE(ABORT,...) 호출 시 현재 쿼리 종료, SQLITE_CONSTRAINT 반환 |
| SQLite JSON 기본 내장 | https://www.sqlite.org/json1.html | §2 "Compiling in JSON Support" — "built into SQLite by default, as of SQLite version 3.38.0" |
| SQLite FK 기본 꺼짐 | https://www.sqlite.org/foreignkeys.html | §2 "Enabling Foreign Key Support" — "Foreign key constraints are disabled by default ... must be enabled separately for each database connection." |
| PostgreSQL 행 보안 | https://www.postgresql.org/docs/current/ddl-rowsecurity.html | §5.9 "Row Security Policies" 첫 문단 (18.6 문서) |
| PostgreSQL 재귀 쿼리 | https://www.postgresql.org/docs/current/queries-with.html | §7.8.2 "Recursive Queries" 첫 문단 · §7.8.2.2 "Cycle Detection" |

### 재현 방법

```bash
# 카탈로그 적재 + 동작 검증 33건
python3 docdb_tools/test_scenarios.py test.db project-documentation-catalogue.yaml docdb_schema.sql
# 매핑 검수
python3 docdb_tools/audit.py audit.db project-documentation-catalogue.yaml docdb_schema.sql
```

`docdb_seed_item_types.sql`은 `docdb_schema.sql`과 같은 폴더에 있어야 자동으로 적재됩니다.
