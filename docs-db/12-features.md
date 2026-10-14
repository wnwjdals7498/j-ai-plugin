# 12. 기능 목록

docs-db의 모든 기능을 사용 경로별로 정리한다. 테이블 정의는 [11-schema.md](11-schema.md), 동작 흐름은 [10-operation.md](10-operation.md)에 있다.

## 0. 상태 표기

`docdb/src`와 `docdb/tests`의 실제 코드·테스트를 확인해 매겼다(2026-10-14, T30). 작업 번호(T00~T34)는 [구현 계획](plan/02-tasks.md)의 작업이고, 테스트 이름은 `test_t<작업 번호>_<순번>_…` 형식이다.

| 표기 | 뜻 |
|---|---|
| **구현됨(T<번호>)** | `docdb/src`에 코드가 있고 해당 작업의 테스트(`docdb/tests`)가 있다 |
| **보류(T31~T34)** | 결정 대기(B·C 등급)라 코드가 없다. 구현 계획에는 작업 번호만 잡혀 있다. gate는 T31, 렌더는 T32, 내보내기는 T33, 보존 정리는 T34 |
| **미구현** | 원 설계나 결정에는 있으나 코드와 구현 작업이 모두 없다 |
| **미확인** | 코드는 있으나 직접 확인하는 테스트를 찾지 못했다 |

## 1. 기능 지도

| 영역 | 기능 | 주 사용자 | 경로 | 상태 | 관련 |
|---|---|---|---|---|---|
| 설치 | 의존성 설치·제거 | 사람 | `docdb_launch.py setup` | 구현됨(T24) | D4 |
| 초기화 | 프로젝트 DB 생성 | 사람·에이전트 | CLI `init` | 구현됨(T11) | D5 |
| 카탈로그 | 카탈로그 적재 | 사람 | CLI `catalogue load`(JSON) | 구현됨(T10, T11) | D2 |
| 프로필 | 사실 입력, 적용 판정 | 사람·스크립트 | CLI `fact list`·`fact set`·`applicability` | 구현됨(T19) | D15 |
| 읽기 | 작업 재개, 핸들 조회, 검색, 관계 추적, 변경분 | 에이전트 | MCP 5개 + CLI | 구현됨(T12, T17, T25) | 04 |
| 쓰기 | 필드 패치 | 에이전트 | MCP `put` + CLI | 구현됨(T12) | 04 |
| 쓰기 | 항목 작성, 관계 제안 | 에이전트 | MCP `put` 확장 | 구현됨(T14, T15) | D10 |
| 관계 | 관계 확정·반려·삭제 | 사람 | CLI `link` | 구현됨(T15) | D10 |
| 관계 | 결정적 추출 R1~R5, 필수 연결 검사 | 스크립트 | 내부 | 구현됨(R1·R2·R3·R5 T16, R4 T20, 검사 뷰 T06, `ctx` 표시 T17) | D10 |
| import | 분할·규칙 매핑·라벨·적용 | 에이전트·스크립트 | CLI `import` | 구현됨(T21, T22) | D6 |
| 작업 | 작업 생성·상태 변경 | 에이전트·사람 | `put` 확장 + CLI `work status` | 구현됨(T18) | D15 |
| 증거 | 실행 결과·지적 적재 | CI | CLI `run ingest` | 구현됨(T20) | D15 |
| 인계 | 인계 작성 | 에이전트·사람 | `put` 확장 | 구현됨(T18) | D15 |
| 인계 | 수신 확인 | 사람 | CLI `handoff ack` | 구현됨(T18) | D15 |
| 검토 | gate 검사, 기준선 고정, 승인 | 스크립트·사람 | CLI | 보류(T31, 결정 O24·O25 대기) | D15 |
| 생성 | 문서·RTM(요구사항 추적표)·등록부 렌더링 | 사람·스크립트 | CLI | 보류(T32, 결정 O27 대기) | D15 |
| 요약 | L1 요약 생성 | 스크립트·에이전트 | 템플릿 + `put` 동봉 | 구현됨(T23) | D16 |
| 정책 | 민감 정보 거부, 예외 승인 | 스크립트·사람 | `put` 검사 + CLI `policy allow-secret` | 구현됨(T09, T11) | D23 |
| 운영 | 마이그레이션, 백업·복구, 이력 검증, 행위자 등록 | 사람 | CLI `migrate`·`backup`·`restore`·`verify-history`·`actor` | 구현됨(T05, T07, T08, T11) | D21, D22 |
| 내보내기 | 텍스트 내보내기 | 사람 | CLI | 보류(T33, 결정 O29 대기) | 원 결정 Q2 |
| 보존 | 오래된 기록 정리 | 사람 | CLI | 보류(T34, 결정 O18 대기) | O18 |
| 검수 | 표준 항목↔컬럼 매핑 | 개발자 | 테스트 `tests/legacy/test_audit_port.py` | 구현됨(T13) | — |

지원 플랫폼([08](08-verification.md) 3.1~3.4): Linux x86_64·Windows·macOS arm64는 GitHub Actions에서 Python 3.10·3.13·3.14가 통과했고, 로컬 Linux x86_64에서 3.10~3.13이 통과했다(3.14는 로컬 인터프리터가 없어 CI로만 확인). Linux arm64는 `pysqlite3` 휠이 없어 소스 빌드(시스템 SQLite에 링크)로 동작을 확인했다. Intel Mac은 소스 빌드가 실패해 지원하지 않는다. Windows에서 Claude Code·Codex가 MCP `command`로 `py`를 해석하는지는 미확인이다.

## 2. MCP 도구 6개

에이전트에 노출하는 도구는 6개로 고정한다(토큰 원칙 TOK-P7, 테스트 `test_t25_1`). 같은 기능을 CLI(`docctl <명령>`)로도 쓸 수 있다.

| 도구 | 입력 | 출력 | 예산 | 읽는 테이블 | 쓰는 테이블 | 상태 |
|---|---|---|---:|---|---|---|
| `docdb_ctx` | `work_id`, `budget` | 작업 카드, 다음 행동, 차단 요인, 관련 문서 L0, 항목 개수, 필드 L1 | 1,500 | `work` `handoff` `run` `finding` `document` `document_kind` `field_value` `item` `entity_summary` `relation` `context_read` `change_log` | `context_read`, `call_log` | 구현됨(T12). 차단 요인에 필수 연결 누락·확정 대기 관계 수(T17, D10), 정책 필터(T12, D8) |
| `docdb_get` | `handles[]`, `level`, `known{h:dg}`, `budget` | 핸들별 L0/L1/L2, 같은 dg면 `UNCHANGED` | 2,000 | `document` `field_value` `item` `entity_summary` | `call_log` | 구현됨(T12). 정책 필터(`test_t12_31`, D8) |
| `docdb_find` | `query`, `limit` | 핸들 + 발췌 | 800 | `fts_text` `v_text_unit` `field_value` `item` | `call_log` | 구현됨(T12). 정책 필터(`test_t12_32`, D8) |
| `docdb_trace` | `handle`, `rel[]`, `depth` | `a -rel-> b` 간선(ID만) | 800 | `relation` | `call_log` | 구현됨(T12). 확정된 관계만 조회(`test_t17_5`, D10) |
| `docdb_since` | `cursor`, `work_id` | 바뀐 대상별 1줄 + diff 최대 6줄 | 1,200 | `change_log` | `call_log` | 구현됨(T12). 정책 필터(`test_t12_33`, D8) |
| `docdb_put` | `patches[]`, `reason` | `APPLIED/NOTHING_APPLIED` + 핸들별 `OK dg tok` 또는 `ERR` | — | `field_value` `item` `relation` | `field_value` `change_log` `text_unit` `fts_text`(트리거), `item` `relation` `work` `handoff` `entity_summary` | 구현됨(T12 필드, T14 항목, T15 관계, T18 작업·인계, T23 요약). 연산별 상태는 아래 표 |

### 2.1 `put` 연산

| 연산 | 대상 핸들 | 동작 | 상태 |
|---|---|---|---|
| `set` | `f:` | 필드 전체 교체 | 구현됨(T12) |
| `append` | `f:` | 필드 끝에 추가 | 구현됨(T12) |
| `replace_section` | `f:` + `anchor` | 제목 줄 기준 절 교체(다른 절 보존) | 구현됨(T12, `test_t12_10`) |
| `state` + `why` | `f:` | `tbd`·`unknown`·`not_applicable` 기록(사유 필수) | 구현됨(T12). DB 수준 사유 제약은 `test_t13_8`로 확인. `put`의 `state` 키를 직접 쓰는 테스트는 찾지 못함(미확인) |
| `expect` | 모든 쓰기 | 읽을 때 받은 dg와 다르면 `CONFLICT` | 구현됨(T12 `test_t12_13`, T14 `test_t14_6`) |
| `upsert` | `i:` | 항목 생성·수정(제목, 상태, 기대 속성, 정의 문서) | 구현됨(T14, D10) |
| `link` | 관계 | `proposed` 관계 생성 | 구현됨(T15, D10) |
| `unlink` | 관계 | 자기 `proposed` 철회 | 구현됨(T15, D10) |
| 작업 생성·상태 | `w:` | `work` 생성·상태 전이 | 구현됨(T18, D15) |
| `handoff` | `w:` | 인계 작성(완료·미완료·위험·다음 행동·커서) | 구현됨(T18, D15) |
| `sum` | `f:` | 필드 쓰기와 함께 L1 요약 제출 | 구현됨(T23, D16) |

묶음 규칙(구현됨, `test_t12_14`·`test_t14_10`): 전부 성공하지 않으면 전부 취소한다. 응답에 본문을 되돌려주지 않는다(`test_t12_12`).

## 3. CLI 명령

`docctl`은 `bin/docctl`(Claude Bash PATH) 또는 `python <plugin>/src/docdb_launch.py cli <명령>`로 실행한다([06](06-runtime-plugin.md) 3절, 테스트 T24·T26). 전역 옵션: `--db`, `--actor`, `--work`, `--session`, `--model`. 명령 목록과 인자는 계약 C-24, 파서는 `docdb/src/docdb/cli.py`다.

### 3.1 설치·초기화·카탈로그·운영

| 명령 | 하는 일 | 쓰는 대상 | 상태 |
|---|---|---|---|
| `setup [--remove]` | 데이터 폴더에 venv 생성, `mcp`·`pysqlite3` 설치(`constraints.txt` 적용) / 제거. `docdb_launch.py` 명령 | 데이터 폴더 | 구현됨(T24, D4) |
| `init` | `.docdb/` 생성, 스키마(마이그레이션 0001~0008)·카탈로그·시드 적재, `.docdb/.gitignore` 생성 | DB 전체 | 구현됨(T11, D5) |
| `catalogue load <json>` | 카탈로그 적재·교체, 시드 필터, 저장 방식 분류. JSON 형식 | `cat_*` | 구현됨(T10, T11, D2) |
| `migrate`, `backup`, `restore` | 백업 후 마이그레이션 적용, 온라인 백업, 백업 파일로 복구(사람·TTY) | DB 파일, 백업 폴더 | 구현됨(T05, T11, D21, D22) |
| `actor add`, `actor list` | 행위자 등록(사람·TTY)·목록 | `actor` | 구현됨(T08, T11) |
| `policy allow-secret` | 민감 정보 검사 예외 승인(사람·TTY) | `policy_exception` | 구현됨(T09, T11, D23) |
| `verify-history [--entity TYPE ID]` | `change_log`의 해시 체인을 다시 계산해 위변조 확인 | — | 구현됨(T07, T11) |

### 3.2 에이전트 공용 (MCP와 같은 기능)

`ctx`, `get`, `find`, `trace`, `since`, `put` — 2절과 같다(`test_t12_37`: CLI와 `DocDB` 출력 일치). 플러그인에서는 DB 경로를 탐색 규칙으로 정한다([06](06-runtime-plugin.md) 5절, T03).

### 3.3 관계 (D10)

| 명령 | 하는 일 | 쓰는 대상 | 상태 |
|---|---|---|---|
| `link review [--work W]` | 확정 대기 관계 목록 | — | 구현됨(T15) |
| `link confirm <rel_id...>` | `proposed` → `confirmed` | `relation`, `change_log` | 구현됨(T15) |
| `link reject <rel_id> --why` | `proposed` → `rejected` | `relation`, `change_log` | 구현됨(T15) |
| `link remove <rel_id> --why` | 확정 관계 삭제(사람만) | `relation`, `change_log` | 구현됨(T15) |

### 3.4 import (D6)

| 명령 | 하는 일 | 쓰는 대상 | 상태 |
|---|---|---|---|
| `import scan <폴더> --owner <u-…> [--export-policy]` | 파일 등록, 구간 분할, 규칙 매핑, 확정분 적용. scan마다 백업 | `artifact` `import_segment` `run` `document` `document_kind` `field_value` `change_log` | 구현됨(T21) |
| `import next [--budget]` | 미분류 구간을 한 줄씩(제목, 앞 200자, 후보 필드) | — | 구현됨(T22) |
| `import label '<json>'` | 라벨 적용, digest 재확인, 원문 복사, R1·R2 추출 | 위와 같음 + `item` `relation` | 구현됨(T22) |
| `import report` | 상태별 구간 수, 매핑 비율 | — | 구현됨(T22) |

### 3.5 작업·프로필·증거·인계 명령 (D15, 계약 C-24로 형식 확정)

원문이 "docctl 기능 목록"으로 남겼던 항목이다. 형식은 계약 C-24에서 정해졌고 구현되어 있다. 작업 생성과 인계 작성은 별도 명령 없이 `put` 확장(2.1절)을 쓴다.

| 명령 또는 기능 | 하는 일 | 쓰는 대상 | 주체 | 상태 |
|---|---|---|---|---|
| `fact list`, `fact set <cap.key> <true\|false\|unknown> --reason` | 사실 목록(미확인 사실 포함)·입력 | `fact_value` | 목록은 누구나, 입력은 사람·TTY | 구현됨(T19) |
| `applicability` | 적용 판정 재계산 후 개수 출력 | `kind_applicability` | 스크립트 | 구현됨(T19). 평가기 `docdb.profile.evaluate`(`always`·`fact`·`all`·`any`·`selected_kind`만 허용, `eval` 키 거부 `test_t19_2`) |
| `work status <WORK-ID> <status>` | 작업 상태 전이(AI는 `planned`·`active`·`blocked`·`review`까지, D24) | `work`, `change_log` | 에이전트·사람 | 구현됨(T18) |
| `run ingest <file\|->` | 실행 결과 적재(결과 5종, 실행 기록 불변), 증거 관계 R4 | `run`, `finding`, `artifact`, `relation` | CI | 구현됨(T20) |
| `handoff ack <HO-ID> --baseline REF` | 인계 수신 확인(기준 커밋 필수, `--reject` 가능) | `handoff` | 사람·TTY | 구현됨(T18) |
| 요약 처리 | 템플릿 요약, `put`의 `sum` 동봉, 낡은 요약 표시 | `entity_summary` | 스크립트·에이전트 | 구현됨(T23, D16) |

보류 명령(결정 대기, 코드 없음):

| 기능 | 쓰는 대상 | 주체 | 상태 | 막는 결정 |
|---|---|---|---|---|
| gate 검사 | — | 스크립트 | 보류(T31) | O25 gate 종류 |
| 기준선 고정 | `doc_revision` | 스크립트 | 보류(T31) | O24 문서 상태 전이 |
| 승인 | `approval` | 사람 | 보류(T31) | O24, O25 |
| 렌더링 | `generation` + 파일 | 사람·스크립트 | 보류(T32) | O27 산출물 형식·위치 |
| 내보내기 | 파일 | 사람 | 보류(T33) | O29 형식(PyYAML 제거로 재결정) |
| 보존 정리 | `call_log` 등 | 사람 | 보류(T34) | O18 |

## 4. 스크립트 내부 기능

| 기능 | 내용 | 상태 | 위치 |
|---|---|---|---|
| digest 계산 | sha256 앞 16자리(값), 전체 해시(이력 행) | 구현됨(T02, T07) | `docdb.textnorm.digest16`, `docdb.history.row_digest` |
| 텍스트 정규화 | LF·NFC로 저장 전 정규화(D19) | 구현됨(T02) | `docdb.textnorm.normalize_text` |
| 토큰 추정 | ASCII 3.5자=1 tok, 그 외 1자=1 tok(보정 전) | 구현됨(T02) | `docdb.textnorm.tok_est` |
| 이력 기록 | `change_log` 체인(순번, 직전 digest) | 구현됨(T07) | `docdb.history.append_change` |
| 이력 해시 재검증 | 행 내용으로 `row_digest`를 다시 계산해 위변조 확인 | 구현됨(T07 `test_t07_9`, CLI `verify-history` T11) | `docdb.history.verify_chain` |
| 예산 채우기 | `ctx` 우선순위(작업 카드·차단 요인은 예산과 무관하게 포함, 넘친 수는 `omitted`) | 구현됨(T12) | `docdb.llm.DocDB.ctx`, `Out` |
| 절 교체 | 제목 줄 기준 부분 교체 | 구현됨(T12) | `docdb.llm.replace_section` |
| 저장 방식 분류 | view·register·run·narrative 판정 | 구현됨(T10) | `docdb.catalogue.storage_class` |
| 적용 판정 평가기 | `always`·`fact`·`all`·`any`·`selected_kind`만 허용(eval 금지), 3값 결과 | 구현됨(T19) | `docdb.profile.evaluate` |
| 필수 연결 검사 | 확정 관계가 없는 필수 연결을 목록화 | 구현됨(T06 뷰, T17 `ctx` 표시) | 뷰 `v_missing_required_links`, `docdb.relations.missing_links` |
| 관계 상태 트리거 | 출처별 상태·행위자 검사, 사람만 확정 | 구현됨(T06) | [11](11-schema.md) 7.1, 트리거 `relation_source_status`·`relation_decide_human_only` |
| 결정적 추출 R1~R5 | 정의·언급·필드 규칙·실행 증거·작업 변경 | 구현됨(R1·R2·R3·R5 T16, R4 T20) | `docdb.relations`, [03](03-relations.md) 3.1 |
| 정책 필터 | `local_only` 본문 차단 | 구현됨(T09 `can_export`, T12 읽기 경로) | `docdb.policy`, `docdb.llm`, [04](04-llm-access.md) 6절 |
| 민감 정보 검사 | 쓰기 시 패턴 검사로 거부, 승인된 예외만 허용 | 구현됨(T09) | `docdb.policy.scan_secrets`·`assert_no_secrets` |
| 시작 검사 | Python 버전, venv, 의존성(`docdb_launch.py`)과 SQLite 기능(trigram·JSON) | 구현됨(T24, T04) | `docdb_launch.py`, `docdb.db.check_capabilities` |
| 순환 검사 | `supersedes`·`generated_from` 순환 금지(재귀 쿼리) | 미구현(원 설계 DEC-08. `docdb/src`에 순환 검사 코드가 없고 구현 작업도 없다) | — |

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

오류 코드와 종료 코드는 `docdb/src/docdb/errors.py`의 `ERROR_CODES`가 정본이다(`test_t02_2`).

| 코드 | 의미 | 대응 | 상태 |
|---|---|---|---|
| `CONFLICT now=<dg>` | 읽은 뒤 값이 바뀜 | `since` 후 다시 패치 | 구현됨(T12) |
| `ANCHOR_NOT_FOUND` | 교체할 절 제목 없음 | 제목 확인 | 구현됨(T12) |
| `UNSUPPORTED_HANDLE` | 쓸 수 없는 핸들 | — | 구현됨(T12, `i:`는 T14부터 허용) |
| `BAD_OP` | 알 수 없는 연산 | — | 구현됨(T12) |
| `DB <메시지>` | DB 제약 위반(카탈로그에 없는 필드, 사유 없는 tbd 등) | 메시지대로 수정 | 구현됨(T04) |
| `NEEDS_HUMAN` | 확정 관계 철회 등 사람 권한 필요 | 사람에게 요청 | 구현됨(T11, T15, T18) |
| `REJECTED_BEFORE rel=<id>` | 반려된 관계 재제안 | 다른 근거로 제안하거나 중단 | 구현됨(T15) |
| `STATUS_FORBIDDEN` | 허용되지 않은 작업 상태 전이(예: AI의 `verified`, `planned`에서 바로 `closed`) | 허용된 순서로 전이하거나 사람에게 요청 | 구현됨(T18, `test_t18_4`·`test_t18_5`) |
| `SECRET_DETECTED` | 민감 정보 패턴 검출 | 값을 빼거나 사람의 예외 승인 | 구현됨(T09) |
| `SUMMARY_TOO_LONG` | `sum` 길이 초과 | 요약을 줄임 | 구현됨(T23) |
| `ID_FORMAT`, `DUPLICATE`, `NOT_FOUND`, `BAD_ARGS`, `BAD_HANDLE` | ID 형식 위반, 중복, 대상 없음, 인자 오류, 핸들 오류 | 메시지대로 수정 | 구현됨(T08, T14, T15) |
| `CATALOGUE_INCOMPATIBLE`, `IMPORT_STALE` | 카탈로그 교체가 기존 값과 충돌, import 구간 원문 변경 | 메시지대로 처리 | 구현됨(T10, T22) |
| `omitted=N` | 예산 초과로 생략 | 필요한 핸들만 추가 조회 | 구현됨(T12) |
| 종료 코드 2 | 환경 실패(`PY_TOO_OLD` `NO_VENV` `VENV_MISMATCH` `DEP_MISSING` `SQLITE_CAPABILITY`) | 안내 메시지대로 `setup` | 구현됨(T24, T04) |
| 종료 코드 3 | DB 파일·구조 문제(`NO_DB` `DB_EXISTS` `MIGRATION_REQUIRED` `DB_NEWER`) | `init` 또는 `migrate` | 구현됨(T05, T11) |
| 종료 코드 1 | 그 밖의 오류 | 메시지대로 수정 | 구현됨 |

`finding.code`에는 원 표준의 검사 코드가 들어간다(예: `FIELD_MISSING`, `DOC_MISSING`, `BRIEF_STALE`, `DRIFT`, `VULN`).

## 7. 권한 경계

| AI가 할 수 없는 것 | 막는 곳 |
|---|---|
| 승인 | 트리거 `approval_human_only` |
| 관계 확정·반려 | 트리거 `relation_decide_human_only` |
| 관계를 `confirmed`로 직접 생성 | 트리거 `relation_source_status` |
| 확정 관계 삭제 | docctl(DB는 삭제 주체를 알 수 없음) |
| 카탈로그 변경, legal_hold 해제 | 도구를 노출하지 않음 |
| `local_only` 문서 본문 읽기 | 읽기 경로 정책 필터 |
| 이력 수정·삭제, 실행 증거 덮어쓰기 | 트리거(사람에게도 금지) |
