# 03. 관계·항목을 쌓는 방식 (결정 D10)

## 1. 결정 요약

원 패키지에는 관계(`relation`)와 항목(`item`)을 쓰는 경로가 없다(문제 B1). `put`은 필드(`f:` 핸들)만 쓰고, 그 밖의 입력은 테스트의 SQL 직접 실행뿐이다. 2026-10-13 결정 D10으로 아래 방식을 정했다.

| 경로 | 주체 | 결과 상태 |
|---|---|---|
| 결정적 추출 | 스크립트(규칙 기반, 같은 입력이면 같은 결과) | `confirmed`(확정) |
| 에이전트 쓰기 | AI 에이전트(`put` 확장) | 관계는 `proposed`(제안), 항목은 바로 기록 |
| 사람 확정 | 사람(CLI) | 제안을 `confirmed` 또는 `rejected`(반려)로 |

근거: 원문 분담표(`docdb-design.md` 5.3) "I 항목·관계 — 스크립트: 추출 / LLM: 추출하지 못한 관계 후보 제안 / 사람: 확정". MCP 도구는 6개를 유지한다(토큰 원칙 TOK-P7, 도구 정의는 매 요청 고정 비용).

## 2. 스키마 변경

`relation`에 컬럼을 추가한다.

| 컬럼 | 값 | 용도 |
|---|---|---|
| `status` | `proposed` / `confirmed` / `rejected` | 확정 여부 |
| `source` | `rule:<규칙 ID>` / `agent` / `human` | 누가 어떤 근거로 만들었나 |
| `proposed_by` | `actor_id` | 제안자 |
| `decided_by`, `decided_at` | `actor_id`, 시각 | 확정·반려한 사람과 시각 |

DB 차원 강제(트리거, 원문 DEC-08 방식):

| 규칙 | 강제 방법 |
|---|---|
| 출처별 상태·행위자 일치 | 삽입 트리거 `relation_source_status`: `source='agent'`이면 `proposed`이고 제안자는 AI, `source='human'`이면 제안자는 사람, `source='rule:*'`이면 제안자 없이 `confirmed`. 그 밖은 거부 |
| `proposed` → `confirmed`·`rejected` 전이는 사람만 | 갱신 트리거 `relation_decide_human_only`(`decided_by`가 사람이 아니거나 `proposed`가 아니면 거부). 기존 `approval_human_only`와 같은 방식 |
| `confirmed` 관계 삭제는 사람만 | docctl이 강제(DELETE 문에는 행위자 정보가 없어 DB가 판단할 수 없음). AI 경로는 자기 `proposed`만 철회 |
| 모든 생성·전이·삭제 기록 | `change_log`(entity_type `relation`) |

규칙 추출은 에이전트의 `put`이 계기가 되어도 `source='rule:<ID>'`로 기록한다. `source`는 docctl이 정하고 에이전트 입력으로 받지 않는다. SQL 직접 실행으로는 위장할 수 있으므로 이 트리거는 실수 방지 장치이며 보안 경계가 아니다. 시제품 SQL과 검증 결과(10/10)는 [11-schema.md](11-schema.md) 7.7절에 있다.

조회 규칙: `v_rtm`, `trace`, `ctx`, 필수 연결 검사는 **`confirmed`만** 쓴다. `ctx` 첫 부분에 확정 대기 관계 수를 표시해 사람이 처리하게 한다.

## 3. 생성 경로

### 3.1 스크립트 결정적 추출 (`confirmed`)

| 규칙 | 입력 | 결과 |
|---|---|---|
| R1 정의 추출 | 대장 문서 필드(예: D01 `requirement_ids`)의 ID 패턴(`cat_item_type.id_prefix`) | `item` 생성, `item.defined_in` = 그 문서 |
| R2 언급 추출 | 다른 문서 필드 본문에 나온 기존 항목 ID | `relation(document → item, references)` |
| R3 필드 추출 규칙 | 카탈로그 필드의 `extract_rel`: 그 필드 본문의 **같은 줄**에 함께 나온 두 종류 ID(예: L02 `test_ids`의 한 줄에 `TC-…`와 `REQ-…`) | 정의된 관계(예: `REQ verified_by TC`) |
| R4 실행 결과 적재 | CI·점검 결과(`run`)의 대상 테스트 ID·요구사항 ID | `relation(run → item, evidence_for)` |
| R5 작업 변경 | 작업 중 `put`이 바꾼 문서·항목 | `relation(work → document|item, changes)` |

R3은 카탈로그 필드에 `extract_rel` 속성(`{"rel":"verified_by","from_prefix":"REQ-","to_prefix":"TC-"}`)을 추가해 정의한다. 원 카탈로그에 없는 확장이다(설계 판단, 형식은 [11-schema.md](11-schema.md) 7.5절). 규칙 ID와 버전은 `source`에 남겨 같은 입력이면 같은 결과가 나오게 한다(S-13).

### 3.2 에이전트 쓰기 (`put` 확장)

기존 `put`의 패치 형식에 `i:` 핸들과 `link`·`unlink` 연산을 추가한다.

```json
[
  {"h": "i:REQ-HW-002", "op": "upsert", "title": "메모리 사용률 경고", "defined_in": "d:DOC-REQ-HWCHECK",
   "attrs": {"mem_alert": 85}, "expect": "<읽을 때 받은 dg 또는 생략(신규)>"},
  {"op": "link", "from": "i:REQ-HW-002", "rel": "verified_by", "to": "i:TC-HW-002",
   "why": "TC-HW-002 절차가 메모리 경고 발생을 확인함"},
  {"op": "unlink", "rel_id": 41, "why": "중복 제안 철회"}
]
```

| 동작 | 결과 |
|---|---|
| `upsert`(항목) | 바로 기록. ID 접두어 트리거(`item_id_prefix`)와 `change_log` 적용. `expect`가 다르면 `CONFLICT` |
| `link` | `relation(status=proposed, source=agent, why)`. 응답은 `OK rel=<id> proposed` 한 줄 |
| `unlink` | 자기가 만든 `proposed`만 철회. `confirmed`는 `ERR NEEDS_HUMAN` |
| 묶음 처리 | 기존 `put`과 같이 전부 성공 아니면 전부 취소, 본문을 되돌려주지 않음 |

### 3.3 사람 확정 (CLI)

```text
docctl link review [--work W]            확정 대기 관계 목록 (출발 → 종류 → 도착, 제안자, 이유)
docctl link confirm <rel_id...>          proposed → confirmed
docctl link reject <rel_id> --why "..."  proposed → rejected
docctl link remove <rel_id> --why "..."  confirmed 삭제 (사람만)
```

반려된 관계는 같은 (출발, 종류, 도착) 조합을 다시 제안할 때 `ERR REJECTED_BEFORE rel=<id>`로 알려 같은 제안이 반복되지 않게 한다.

## 4. 항목(`item`) 쓰기 정리

| 경로 | 동작 |
|---|---|
| 스크립트 R1 | 대장 문서에서 ID를 추출해 생성 |
| 에이전트 `put` `upsert` | 생성·수정 |
| import | [05-import.md](05-import.md)의 적용 단계에서 R1·R2 실행 |
| 삭제 | 허용(D7). 검색 색인에서도 제거(`item_fts_ad`), `change_log`에 `delete` 기록 |

## 5. 필수 연결 검사 (문제 B2)

새 뷰 `v_missing_required_links`: 적용 대상 문서마다 `cat_kind_link(is_required=1)`의 대상 종류와 확정된 연결이 하나도 없고, `link_exemption`도 없는 경우를 찾는다.

"종류 Y와 연결됨"의 판정: 확정된 `relation`의 상대가 (a) 종류 Y의 문서이거나, (b) 종류 Y 문서에서 정의된 항목(`item.defined_in`)인 경우.

결과는 `ctx`의 차단 요인(`!`)과 문서 gate 검사(승인 전 검사)에 쓴다.

## 6. 시작 카탈로그의 연결 규칙 (확정 D12)

원 카탈로그의 `cat_kind_link`가 없어 새로 정의했다(D12). 원문에 나온 추적 사슬(요구사항 → 테스트 → 실행 증거)과 운영 관리(U-09)를 기준으로 했다.

| 종류 | 대상 | 구분 | 관계 예 |
|---|---|---|---|
| D01 요구사항 | L02 테스트 케이스 | 필수 | `REQ verified_by TC` |
| L02 테스트 케이스 | D01 요구사항 | 필수 | 위와 같은 관계의 반대 방향 확인 |
| L03 테스트 결과 | L02 테스트 케이스 | 필수 | `RUN evidence_for TC` |
| P06 Runbook | K02 서버 목록 | 필수 | 절차가 다루는 자원 |
| K07 드리프트 점검 | K02 서버 목록 | 필수 | 점검 대상 자원 |
| R04 백업 증거 | K02 서버 목록 | 필수 | 백업 대상 자원 |
| K02 서버 목록 | K01 환경 | 선택 | 자원이 속한 환경 |
| K02 서버 목록 | S03 자산 | 선택 | 자원과 자산 대장 |
| B04 RAID | D01 요구사항 | 선택 | 위험이 걸린 요구사항 |
| L07 결함 | L02 테스트 케이스 | 선택 | 결함을 찾은 테스트 |
| F07 기술 부채 | D01 요구사항 | 선택 | 부채가 걸린 요구사항 |

## 7. 토큰 영향

| 항목 | 영향 |
|---|---|
| 도구 정의 | 변화 없음(6개 유지). `put` 설명에 연산 2개 추가 |
| 에이전트 출력 | 관계 1건 = `link` 패치 한 줄. 본문 출력 없음 |
| 응답 | `OK rel=<id> proposed` 한 줄 |
| 조회 | `trace`는 기존대로 ID만 반환 |
