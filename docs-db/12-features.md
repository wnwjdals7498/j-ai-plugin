# 12. 기능 목록

docs-db의 모든 기능을 사용 경로별로 정리한다. 테이블 정의는 [11-schema.md](11-schema.md), 동작 흐름은 [10-operation.md](10-operation.md)에 있다.

## 0. 상태 표기

원본 코드(`reference/docdb-package/docdb_tools/`)를 확인해 매겼다(2026-10-13).

| 표기 | 뜻 |
|---|---|
| **구현** | 원본 코드에 있고 원본 테스트로 동작 확인됨 |
| **시제품** | 테스트 코드나 2026-10-13 시제품에만 있음. 제품 코드로 옮겨야 함 |
| **확정** | 결정됨, 아직 코드 없음 |
| **제안** | 미결 사항의 권장안. 확정 전(2026-10-13 2차 결정 후 해당 없음) |
| **미정** | 필요하다는 것만 정해짐. 명령 형식 미정 |

## 1. 기능 지도

| 영역 | 기능 | 주 사용자 | 경로 | 상태 | 관련 |
|---|---|---|---|---|---|
| 설치 | 의존성 설치·제거 | 사람 | CLI `setup` | 확정 | D4 |
| 초기화 | 프로젝트 DB 생성 | 사람·에이전트 | CLI `init` | 확정 | D5 |
| 카탈로그 | 카탈로그 적재 | 사람 | CLI `catalogue load` | 구현(YAML) → JSON으로 변경 확정 | D2 |
| 프로필 | 사실 입력, 적용 판정 | 사람·스크립트 | CLI | 평가기 시제품, 명령은 계획 계약 | D15 |
| 읽기 | 작업 재개, 핸들 조회, 검색, 관계 추적, 변경분 | 에이전트 | MCP 5개 + CLI | 구현 | 04 |
| 쓰기 | 필드 패치 | 에이전트 | MCP `put` + CLI | 구현 | 04 |
| 쓰기 | 항목 작성, 관계 제안 | 에이전트 | MCP `put` 확장 | 확정 | D10 |
| 관계 | 관계 확정·반려·삭제 | 사람 | CLI `link` | 확정 | D10 |
| 관계 | 결정적 추출 R1~R5, 필수 연결 검사 | 스크립트 | 내부 | 확정(검사 뷰 시제품) | D10 |
| import | 분할·규칙 매핑·라벨·적용 | 에이전트·스크립트 | CLI `import` | 확정 | D6 |
| 작업 | 작업 생성·상태 변경 | 에이전트·사람 | `put` 확장 또는 CLI | 확정 | D15 |
| 증거 | 실행 결과·지적 적재 | CI | CLI | 확정(형식은 계획 계약) | D15 |
| 인계 | 인계 작성 | 에이전트·사람 | `put` 확장 | 확정 | D15 |
| 인계 | 수신 확인 | 사람 | CLI | 확정(형식은 계획 계약) | D15 |
| 검토 | gate 검사, 기준선 고정, 승인 | 스크립트·사람 | CLI | 확정(형식은 계획 계약) | D15 |
| 생성 | 문서·RTM·등록부 렌더링 | 사람·스크립트 | CLI | 확정(형식은 계획 계약) | D15 |
| 요약 | L1 요약 생성 | 스크립트·에이전트 | 템플릿 + `put` 동봉 | 확정 | D16 |
| 내보내기 | 텍스트 내보내기 | 사람 | CLI | 시기 미정 | 원 결정 Q2 |
| 검수 | 표준 항목↔컬럼 매핑 | 개발자 | `audit.py` | 구현 | — |

## 2. MCP 도구 6개

에이전트에 노출하는 도구는 6개로 고정한다(토큰 원칙 TOK-P7). 같은 기능을 CLI(`docctl <명령>`)로도 쓸 수 있다.

| 도구 | 입력 | 출력 | 예산 | 읽는 테이블 | 쓰는 테이블 | 상태 |
|---|---|---|---:|---|---|---|
| `docdb_ctx` | `work_id`, `budget` | 작업 카드, 다음 행동, 차단 요인, 관련 문서 L0, 항목 개수, 필드 L1 | 1,500 | `work` `handoff` `run` `finding` `document` `document_kind` `field_value` `item` `entity_summary` `relation` `context_read` `change_log` | `context_read`, `call_log` | 구현. 확정 추가: 차단 요인에 필수 연결 누락·확정 대기 관계(D10), 정책 필터(D8) |
| `docdb_get` | `handles[]`, `level`, `known{h:dg}`, `budget` | 핸들별 L0/L1/L2, 같은 dg면 `UNCHANGED` | 2,000 | `document` `field_value` `item` `entity_summary` | `call_log` | 구현. 정책 필터 확정(D8) |
| `docdb_find` | `query`, `limit` | 핸들 + 발췌 | 800 | `fts_text` `v_text_unit` `field_value` `item` | `call_log` | 구현. 정책 필터 확정(D8) |
| `docdb_trace` | `handle`, `rel[]`, `depth` | `a -rel-> b` 간선(ID만) | 800 | `relation` | `call_log` | 구현. 확정된 관계만 조회로 변경(D10) |
| `docdb_since` | `cursor`, `work_id` | 바뀐 대상별 1줄 + diff 최대 6줄 | 1,200 | `change_log` | `call_log` | 구현. 정책 필터 확정(D8) |
| `docdb_put` | `patches[]`, `reason` | `APPLIED/NOTHING_APPLIED` + 핸들별 `OK dg tok` 또는 `ERR` | — | `field_value` `item` `relation` | `field_value` `change_log` `text_unit` `fts_text`(트리거), 확장 후 `item` `relation` | 구현(필드). 확장은 아래 표 |

### 2.1 `put` 연산

| 연산 | 대상 핸들 | 동작 | 상태 |
|---|---|---|---|
| `set` | `f:` | 필드 전체 교체 | 구현 |
| `append` | `f:` | 필드 끝에 추가 | 구현 |
| `replace_section` | `f:` + `anchor` | 제목 줄 기준 절 교체(다른 절 보존) | 구현 |
| `state` + `why` | `f:` | `tbd`·`unknown`·`not_applicable` 기록(사유 필수) | 구현 |
| `expect` | 모든 쓰기 | 읽을 때 받은 dg와 다르면 `CONFLICT` | 구현 |
| `upsert` | `i:` | 항목 생성·수정(제목, 상태, 기대 속성, 정의 문서) | 확정(D10) |
| `link` | 관계 | `proposed` 관계 생성 | 확정(D10) |
| `unlink` | 관계 | 자기 `proposed` 철회 | 확정(D10) |
| 작업 생성·상태 | `w:` | `work` 생성·상태 전이 | 확정(D15) |
| `handoff` | `w:` | 인계 작성(완료·미완료·위험·다음 행동·커서) | 확정(D15) |
| `sum` | `f:` | 필드 쓰기와 함께 L1 요약 제출 | 확정(D16) |

묶음 규칙(구현): 전부 성공하지 않으면 전부 취소한다. 응답에 본문을 되돌려주지 않는다.

## 3. CLI 명령

`docctl`은 `bin/docctl`(Claude Bash PATH) 또는 `python <plugin>/src/docdb_launch.py cli <명령>`로 실행한다([06](06-runtime-plugin.md) 3절).

### 3.1 설치·초기화·카탈로그

| 명령 | 하는 일 | 쓰는 대상 | 상태 |
|---|---|---|---|
| `setup [--remove]` | 데이터 폴더에 venv 생성, `mcp`·`pysqlite3` 설치(`constraints.txt` 적용) / 제거 | 데이터 폴더 | 확정(D4) |
| `init` | `.docdb/` 생성, 스키마·토큰 확장·카탈로그·시드 적재, `.docdb/.gitignore` 생성 | DB 전체 | 확정(D5) |
| `catalogue load <json>` | 카탈로그 적재·교체, 시드 필터, 저장 방식 분류 | `cat_*` | 원본은 YAML 로더(구현) → JSON 변경 확정(D2) |

### 3.2 에이전트 공용 (MCP와 같은 기능)

`ctx`, `get`, `find`, `trace`, `since`, `put` — 2절과 같다. 원본 인자: `--db`, `--actor`, `--work`(구현). 플러그인에서는 DB 경로를 탐색 규칙으로 정한다([06](06-runtime-plugin.md) 5절).

### 3.3 관계 (D10, 확정)

| 명령 | 하는 일 | 쓰는 대상 |
|---|---|---|
| `link review [--work W]` | 확정 대기 관계 목록 | — |
| `link confirm <rel_id...>` | `proposed` → `confirmed` | `relation`, `change_log` |
| `link reject <rel_id> --why` | `proposed` → `rejected` | `relation`, `change_log` |
| `link remove <rel_id> --why` | 확정 관계 삭제(사람만) | `relation`, `change_log` |

### 3.4 import (D6, 확정)

| 명령 | 하는 일 | 쓰는 대상 |
|---|---|---|
| `import scan <폴더> [--export-policy]` | 파일 등록, 구간 분할, 규칙 매핑, 확정분 적용 | `artifact` `import_segment` `run` `document` `document_kind` `field_value` `change_log` |
| `import next [--budget]` | 미분류 구간을 한 줄씩(제목, 앞 200자, 후보 필드) | — |
| `import label '<json>'` | 라벨 적용, digest 재확인, 원문 복사, R1·R2 추출 | 위와 같음 + `item` `relation` |
| `import report` | 상태별 구간 수, 매핑 비율 | — |

### 3.5 형식 미정 명령 (O8)

원문이 "docctl 기능 목록"을 다음 단계로 남겼다. 필요한 기능과 쓰는 테이블만 정해져 있다.

| 기능 | 쓰는 대상 | 주체 | 비고 |
|---|---|---|---|
| 사실 목록·입력 | `fact_value` | 사람 | 미확인 사실 질문 목록 출력 포함 |
| 적용 판정 | `kind_applicability` | 스크립트 | 평가기는 시제품(아래 4절) |
| 작업 생성·상태 | `work`, `work_event` | 에이전트·사람 | 제안: `put` 확장 |
| 실행 결과 적재 | `run`, `finding`, `artifact`, `relation`(R4) | CI | 결과 5종, 실행 기록 불변 |
| 인계 작성·수신 확인 | `handoff` | 에이전트·사람 | 제안: 작성은 `put` 확장, 수신 확인은 사람 CLI(기준 커밋 확인 필수) |
| gate 검사 | — | 스크립트 | 필수 필드·필수 연결 누락, 미해결 지적 |
| 기준선 고정 | `doc_revision` | 스크립트 | 체인·digest 트리거 |
| 승인 | `approval` | 사람 | AI는 트리거가 거부 |
| 렌더링 | `generation` + 파일 | 사람·스크립트 | 원본 digest 기록, 이후 STALE 판정 |
| 요약 처리 | `entity_summary` | 스크립트·에이전트 | 확정(D16) |
| 내보내기 | 파일 | 사람 | 원 결정 Q2. PyYAML 제거로 형식 재결정 |

## 4. 스크립트 내부 기능

| 기능 | 내용 | 상태 | 위치 |
|---|---|---|---|
| digest 계산 | sha256 앞 16자리(값), 전체 해시(이력 행) | 구현 | `docctl_llm.digest`, `_append_change` |
| 토큰 추정 | ASCII 3.5자=1 tok, 그 외 1자=1 tok(보정 전) | 구현 | `docctl_llm.tok_est` |
| 이력 기록 | `change_log` 체인(순번, 직전 digest) | 구현 | `docctl_llm._append_change` |
| 예산 채우기 | `ctx` 우선순위(작업 카드·차단 요인은 예산과 무관하게 포함, 넘친 수는 `omitted`) | 구현 | `docctl_llm.ctx`, `Out` |
| 절 교체 | 제목 줄 기준 부분 교체 | 구현 | `docctl_llm.replace_section` |
| 저장 방식 분류 | view·register·run·narrative 판정 | 구현 | `load_catalogue.storage_class` |
| 적용 판정 평가기 | `always`·`fact`·`all`·`any`·`selected_kind`만 허용(eval 금지), 3값 결과 | 시제품 | `test_scenarios.py` `ev()` |
| 필수 연결 검사 | `v_missing_required_links` | 시제품 | [11](11-schema.md) 7.7 |
| 관계 상태 트리거 | 출처별 상태·행위자 검사, 사람만 확정 | 시제품 | [11](11-schema.md) 7.7 |
| 결정적 추출 R1~R5 | 정의·언급·필드 규칙·실행 증거·작업 변경 | 확정 | [03](03-relations.md) 3.1 |
| 정책 필터 | `local_only` 본문 차단 | 확정 | [04](04-llm-access.md) 6절 |
| 시작 검사 | Python 버전, venv, trigram·JSON | 확정 | [06](06-runtime-plugin.md) 3절 |
| 순환 검사 | `supersedes`·`generated_from` 순환 금지(재귀 쿼리) | 미구현(원 설계 DEC-08) | — |
| 이력 해시 재검증 | 행 내용으로 `row_digest`를 다시 계산해 위변조 확인 | 미구현(원 설계 4장) | — |

## 5. 테이블별 쓰기 주체

어떤 기능이 어느 테이블에 쓰는지 테이블 기준으로 본다.

| 테이블 | 쓰는 기능 | 주체 |
|---|---|---|
| `cat_*` | `catalogue load` | 사람(승인) |
| `project`, `fact_value` | `init`, 사실 입력 | 사람 |
| `kind_applicability` | 적용 판정 | 스크립트 |
| `actor` | `init`, 행위자 등록 | 사람 |
| `document`, `document_kind` | `import`, 문서 생성 | 에이전트·스크립트 |
| `field_value` | `put`, `import label` | 에이전트·스크립트 |
| `item` | `put upsert`, 추출 R1 | 에이전트·스크립트 |
| `relation` | `put link`(proposed), 추출 R1~R5(confirmed), `link`(확정·반려·삭제) | 에이전트·스크립트·사람 |
| `artifact` | `import scan`, 실행 적재 | 스크립트·CI |
| `work`, `work_event` | 작업 생성·상태 | 에이전트·사람 |
| `run`, `finding` | 실행 적재, `import scan`(run) | CI·스크립트 |
| `handoff` | 인계 작성·수신 확인 | 에이전트·사람 |
| `approval` | 승인 | 사람만 |
| `context_read` | `ctx` 자동 기록 | 스크립트 |
| `change_log` | 모든 쓰기 | 스크립트(자동) |
| `doc_revision` | 기준선 고정 | 스크립트 |
| `generation` | 렌더링 | 스크립트 |
| `entity_summary` | 요약 처리 | 스크립트·에이전트 |
| `import_segment` | `import` | 스크립트 |
| `call_log` | 모든 에이전트 호출 | 스크립트(자동) |

## 6. 오류와 종료 코드

| 코드 | 의미 | 대응 | 상태 |
|---|---|---|---|
| `CONFLICT now=<dg>` | 읽은 뒤 값이 바뀜 | `since` 후 다시 패치 | 구현 |
| `ANCHOR_NOT_FOUND` | 교체할 절 제목 없음 | 제목 확인 | 구현 |
| `UNSUPPORTED_HANDLE` | 쓸 수 없는 핸들 | — | 구현(확장 후 `i:` 허용) |
| `BAD_OP` | 알 수 없는 연산 | — | 구현 |
| `DB <메시지>` | DB 제약 위반(카탈로그에 없는 필드, 사유 없는 tbd 등) | 메시지대로 수정 | 구현 |
| `NEEDS_HUMAN` | 확정 관계 철회 등 사람 권한 필요 | 사람에게 요청 | 확정 |
| `REJECTED_BEFORE rel=<id>` | 반려된 관계 재제안 | 다른 근거로 제안하거나 중단 | 확정 |
| `omitted=N` | 예산 초과로 생략 | 필요한 핸들만 추가 조회 | 구현 |
| 종료 코드 2 | 시작 검사 실패(Python·venv·SQLite 기능) | 안내 메시지대로 `setup` | 확정 |

`finding.code`에는 원 표준의 검사 코드가 들어간다(예: `FIELD_MISSING`, `DOC_MISSING`, `BRIEF_STALE`, `DRIFT`, `VULN`).

## 7. 권한 경계

| AI가 할 수 없는 것 | 막는 곳 |
|---|---|
| 승인 | 트리거 `approval_human_only` |
| 관계 확정·반려 | 트리거 `relation_decide_human_only`(확정) |
| 관계를 `confirmed`로 직접 생성 | 트리거 `relation_source_status`(확정) |
| 확정 관계 삭제 | docctl(DB는 삭제 주체를 알 수 없음) |
| 카탈로그 변경, legal_hold 해제 | 도구를 노출하지 않음 |
| `local_only` 문서 본문 읽기 | 읽기 경로 정책 필터 |
| 이력 수정·삭제, 실행 증거 덮어쓰기 | 트리거(사람에게도 금지) |
