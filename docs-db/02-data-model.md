# 02. 데이터 모델 — 무엇을 적재하고 어떻게 연결하는가

출처: 원문 `docdb-design.md` 2~4장, `docdb_schema.sql`, `docdb_schema_token_ext.sql`. 2026-10-13 결정으로 바뀐 부분은 해당 문서를 링크한다.

## 1. 원칙

| 원칙 | 내용 | 근거 |
|---|---|---|
| DB가 원본 | 구조·서술·관계·이력은 DB가 원본이다 | 원 결정 DEC-01 |
| Git 원본은 참조만 | 코드·OpenAPI·Migration·IaC 내용은 넣지 않고 경로·커밋·digest만 `artifact`에 둔다 | DEC-01, S-02 |
| 문서는 생성물 | md·RTM·등록부는 DB에서 필요할 때 생성하고, 생성 사실만 `generation`에 남긴다 | DEC-02 |
| 필드 단위 저장 | 본문은 카탈로그 필드 하나를 행 하나로 저장한다. 누락 필드를 쿼리로 찾을 수 있다 | DEC-04 |
| 중복 저장 금지 | 기본값은 카탈로그에서 계산하고 다르게 정한 값만 저장한다 | DEC-09, U-04 |
| 기대값과 관측값 분리 | `item.attrs`에는 기대값만, 실제 관측값은 `run`(실행)과 `finding`(지적)에 둔다 | DEC-05 |
| 민감 원문 금지 | 비밀번호·토큰·키는 저장하지 않는다 | S-11 |

## 2. 적재 데이터 — 6계층

| 계층 | 테이블 | 담는 데이터 | 예 | 넣는 주체 |
|---|---|---|---|---|
| R 규칙 | `cat_version` `cat_group` `cat_fact` `cat_kind` `cat_kind_field` `cat_kind_link` `cat_kind_alias` `cat_item_type` | 문서 종류, 종류별 필드, 종류 간 연결 규칙, 별칭, 항목 ID 접두어 | D01 요구사항의 필수 필드 `behavior`, `REQ-` 접두어 | 스크립트(카탈로그 적재) |
| P 프로필 | `project` `fact_value` `kind_selection` `policy_exception` `kind_applicability` | 프로젝트 사실(참·거짓·미확인), 선택·예외, 종류별 필수 여부 판정 | `cap.database=1`이면 I01 필수 | 사람(사실 확정), 스크립트(판정) |
| I 실체 | `actor` `document` `document_kind` `doc_reviewer` `doc_tag` `doc_scope` `field_value` `item` `role_assignment` `artifact` `doc_artifact` `relation` `link_exemption` | 사람·AI 행위자, 문서 메타, 필드 본문, 고정 ID 항목, 파일 참조, 관계 | `REQ-HW-001`, `RES-DB01`(서버 자원), `TC-HW-001` | 에이전트(본문·항목·관계 제안), 스크립트(추출), 사람(소유자·관계 확정) |
| W 작업·증거 | `work` `work_event` `run` `finding` `approval` `handoff` `context_read` | 작업, 작업 사건, 실행 결과, 지적, 승인, 인계, AI가 읽은 근거 | `RUN-0001` 하드웨어 점검 PASS | CI·스크립트(실행), 사람(승인·인계 확인) |
| H 이력 | `change_log` `doc_revision` | 모든 변경(누가·언제·무엇을), 승인 대상 기준선 | `DOC-X|D01|behavior` seq 1→2→3 | 스크립트(자동) |
| D 파생 | `generation` | 생성물의 원본 digest, 생성기, 최신 여부 | md 문서 FRESH/STALE | 스크립트 |

토큰 확장(원문 `docdb-token-spec.md` 3장): `field_value`·`item`에 `digest`·`tok_est`(토큰 추정) 컬럼, `entity_summary`(요약, 파생), `text_unit`+`fts_text`(전문 검색 색인), `call_log`(도구별 전송량 기록).

2026-10-13 추가: `relation`의 상태 컬럼([03-relations.md](03-relations.md) 2절), `import_segment`([05-import.md](05-import.md) 2절).

## 3. 문서의 저장 방식 4분류

원문 DEC-02. 원 카탈로그 194종 기준 수치이며, 시작 카탈로그의 분류는 [07-catalogue.md](07-catalogue.md)에 있다.

| 저장 방식 | 원 카탈로그 수 | 저장 위치 | 예 |
|---|---:|---|---|
| `view` | 4 | 직접 쓰지 않고 다른 테이블을 조회해 생성 | A01 문서 목록, D08 RTM, F06 의존성·결정 색인 |
| `register` | 8 | `item` 행(항목 대장) | A04 용어, B04 RAID, L07 결함 |
| `run` | 27 | `run`+`finding`+`artifact` | L03 테스트 결과, R04 백업 증거, K07 드리프트 점검 |
| `narrative` | 155 | `field_value`(필드 단위 서술) | D01 요구사항, K02 서버 목록, P06 Runbook |

## 4. 연결 방식 4가지

### 4.1 구조 연결 (FK, 자동)

입력 순간 FK(다른 행을 가리키는 값)로 묶이고, DB가 위반을 거부한다.

| 연결 | 의미 |
|---|---|
| `field_value` → `document_kind` → `cat_kind_field` | 카탈로그에 없는 필드는 저장 불가 |
| `item.defined_in` → `document` | 항목을 정의한 문서(대장) |
| `doc_scope(doc, item)` | 문서가 다루는 대상(서비스·기능·환경·자산) |
| `doc_artifact(doc, artifact)` | 문서와 Git 원본 파일 |
| `role_assignment(role_item, actor, raci)` | 역할과 실제 담당자. 최종 책임(A)은 1명만(부분 UNIQUE 인덱스) |
| `run.work_id`, `run.env_item_id` | 실행이 속한 작업, 실행한 서버·환경 |
| `finding.run_id`, `finding.target_*` | 지적이 나온 실행과 지적 대상 |
| `approval.revision_id` / `work_id` | 승인 대상 기준선 또는 작업 |
| `handoff.work_id`, `context_read.last_change_id` | 인계 작업, AI가 읽은 시점의 마지막 변경 |

### 4.2 의미 연결 (`relation` 1테이블)

문서·항목·파일·작업·실행(`document|item|artifact|work|run`) 사이의 관계를 방향과 종류를 붙여 한 테이블에 저장한다. RTM, 의존성 지도, 영향 분석은 이 테이블을 조회해서 만든다. 같은 (출발, 종류, 도착)은 한 번만 저장된다(UNIQUE). 쌓는 방식은 [03-relations.md](03-relations.md).

### 4.3 규칙 연결 (카탈로그)

`cat_kind_link(kind, target_kind, is_required)`가 "종류 X의 문서는 종류 Y와 연결되어야 한다"를 정한다. 대상이 이 프로젝트에 해당하지 않으면 `link_exemption`에 N/A·이유·프로필 근거를 남긴다(S-10). 검사는 [03-relations.md](03-relations.md) 5절.

### 4.4 시간 연결 (이력)

| 테이블 | 연결 |
|---|---|
| `change_log` | 대상별 변경 체인. `since`로 커서 이후 변경만 조회 |
| `doc_revision` | 문서별 승인 기준선 체인 |
| `context_read` | AI가 작업 중 읽은 대상과 그 시점의 마지막 변경. 이후 바뀌면 `v_context_stale`에 표시 |

## 5. 관계 12종 — 방향 정의 (확정 D11)

원 표준(STD L508–523)이 없어 아래 방향 정의를 새로 정했다(D11). "근거"는 코드·원문에서 용도가 확인된 것만 적었다.

| 관계 | 방향(제안) | 근거 |
|---|---|---|
| `verified_by` | 요구사항·완료 조건 → 테스트 케이스 또는 실행 | `v_rtm` 뷰 |
| `implements` | API·모듈·코드 → 요구사항 | `v_rtm` 뷰 |
| `changes` | 작업 → 문서·항목 | 토큰 명세 7.1(`ctx`가 관련 문서 선정에 사용) |
| `supersedes` | 새 문서·결정 → 대체된 것 | 원문 4장(순환 금지) |
| `generated_from` | 생성물 → 원천 | 원문 4장(순환 금지) |
| `evidence_for` | 실행·파일 → 요구사항·작업·문서 | — |
| `depends_on` | 서비스·모듈 → 의존 대상 | — |
| `constrained_by` | 대상 → 비기능 요구·업무 규칙·정책 | — |
| `uses_data` | 기능·서비스 → 데이터셋 | — |
| `visualizes` | 대시보드·화면 → 지표·데이터 | — |
| `references` | 일반 참조(종류를 정하지 못한 언급) | — |
| `handoff_to` | 작업 → 인계 대상 작업 | — |

`supersedes`·`generated_from`은 순환이 생기지 않도록 스크립트가 재귀 쿼리로 검사한다(원문 DEC-08).

## 6. 이력 구조

원 요구 U-06 "이력은 링크드 리스트로"에 대한 원문 판정은 "부분적으로 맞음"이다. 링크만으로 남는 문제를 아래처럼 보완했다(원문 4장).

| 문제 | 보완 |
|---|---|
| 전체 이력 조회 | 재귀 쿼리(`WITH RECURSIVE`) |
| N번째 버전 조회 | `seq` 순번 + `UNIQUE(entity, seq)` |
| 최신값 조회 | 현재 값은 `document`·`field_value`에 따로 두고, 이력은 감사·재현용 |
| 동시 수정으로 분기 | `UNIQUE(prev_change_id)` |
| 위변조 | 각 행에 직전 행 digest 저장, 삽입 트리거가 체인 확인 |
| 수정·삭제 | append-only 트리거. 정정은 `op='correct'` 행 추가 |

이력은 두 층이다. `change_log`는 "누가, 언제, 어떤 필드를 무엇에서 무엇으로 바꿨나", `doc_revision`은 "승인된 버전 N의 내용은 무엇이었나"에 답한다. 행 내용의 해시 재계산은 스크립트가 맡는다(SQLite에 sha256 함수가 기본으로 없음).

## 7. 예시 — 서버 점검 작업

```text
d:DOC-REQ-HWCHECK (D01 요구사항)
   └ defined_in ── i:REQ-HW-001 "디스크 사용률 80% 경고"
                      │ verified_by
d:DOC-TC-HW (L02 테스트 케이스) ── i:TC-HW-001
d:DOC-K02 (서버 목록) ── i:RES-DB01   attrs={disk_alert: 80}         ← 기대값

w:WORK-2026-0001 ──changes──▶ d:DOC-REQ-HWCHECK
r:RUN-0001 hw_check  env=RES-DB01  work=WORK-2026-0001  PASS
   └ evidence_for ──▶ i:REQ-HW-001
finding DRIFT → i:RES-DB01 "관측값 90 ≠ 기대값 80"                    ← 관측값

change_log  DOC-REQ-HWCHECK|D01|behavior  c:12 → c:31 → c:49 (사람/AI, 시각)
context_read  AI가 WORK-2026-0001에서 behavior를 c:49 시점에 읽음
```

조회 예:

| 질문 | 조회 |
|---|---|
| 이 요구사항을 검증하는 테스트가 있는가 | `v_rtm` |
| 이 서버를 바꾸면 영향받는 요구사항은 | `trace i:RES-DB01` |
| 이 작업을 넘겨받으면 무엇이 막혀 있나 | `v_work_resume`(마지막 인계, 다음 행동, 미해결 지적, 실패·미실행 수, 낡은 근거 수) |
| AI가 읽은 근거가 그 뒤 바뀌었나 | `v_context_stale` |
| 필수 필드가 빠진 문서는 | `v_missing_required_fields` |

## 8. 저장하지 않는 것

| 하지 않는 것 | 이유 |
|---|---|
| 렌더링한 문서를 DB에 다시 저장 | 생성물이다(DEC-02) |
| 실행 원시 로그 전문 | `artifact`로 참조만, 모델에는 `finding`만 |
| 요약을 원본처럼 수정 | 원본이 바뀌면 요약은 자동으로 낡음 처리(S-05) |
| 같은 내용을 JSON 열과 텍스트 열에 동시 저장 | 중복(U-04) |
| import 대상 파일의 구간 원문 사본 | 오프셋과 digest만 보관하고, 원문은 `field_value` 한 곳에만 저장([05](05-import.md)) |
