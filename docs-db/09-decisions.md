# 09. 결정 기록 (정본)

결정 상태는 이 문서 한 곳에만 기록한다. 다른 문서의 요약은 이 문서를 따른다. 2026-10-13 결정은 PMT 프로젝트 `docdb`에 D1~D10으로도 기록되어 있다(PMT 결정은 삭제하지 않고 대체·폐기로만 바꾼다).

## 1. 원 설계 결정 (2026-10-08)

출처: 원문 `docdb-design.md` 2장·7장, `docdb-token-spec.md` 머리표.

### 1.1 설계 결정 DEC

| # | 결정 | 현재 상태 |
|---|---|---|
| DEC-01 | 원본 위치 하이브리드: 구조·서술·관계·이력은 DB, 코드·OpenAPI·Migration·IaC는 Git(DB엔 경로·커밋·digest만) | 유지 |
| DEC-02 | 문서는 생성물. 저장 방식 4분류(view·register·run·narrative) | 유지 |
| DEC-03 | 데이터 계층 6개(R·P·I·W·H·D), 테이블 36·뷰 8·트리거 12 | 유지. 2026-10-13 추가 테이블·컬럼은 D6·D10 |
| DEC-04 | 본문은 필드 단위 저장(`field_value`) | 유지 |
| DEC-05 | 고정 ID 항목(`item`)과 관계 1테이블(`relation`) | 유지. 관계 상태 추가(D10) |
| DEC-06 | 이력: 링크드 리스트+순번+해시 체인, append-only | 유지 |
| DEC-07 | DB 엔진: SQLite로 시작, 다중 동시 쓰기·행 단위 접근 통제가 필요해지면 PostgreSQL | 유지(이전은 범위 밖) |
| DEC-08 | 쓰기 경로 단일화(docctl) + DB 제약·트리거로도 강제 | 유지. 관계 트리거 추가(D10) |
| DEC-09 | 중복 저장 금지(기본값 계산, 파일 내용 미저장, 생성물은 기록만) | 유지 |

### 1.2 확정 사항 (원문 7.1)

| # | 결정 | 현재 상태 |
|---|---|---|
| Q1 | SQLite로 시작, DEC-07 조건이 생기면 PostgreSQL | 유지 |
| Q2 | 초기에는 DB만 사용(승인은 `approval`), 팀이 커지면 YAML 내보내기 추가 | 유지. D5(git 제외·로컬 전용)와 일치. 내보내기 형식은 구현 시 다시 정한다(PyYAML 제거로 JSON 또는 의존성 없는 YAML 출력) |
| Q3 | 별칭 대표 종류: 리소스 가용성 계획=B03, 작업 환경=G01, 제품 성공 기준=C01, 환경별 설정 비교표=S04 | 유지(해당 별칭이 있을 때 적용) |
| Q4 | 항목 종류 39개와 접두어 승인 | 유지. 적재는 시작 카탈로그 종류만(시드 필터) |
| D-A03 | 템플릿·스키마는 `cat_*`에 정의만 하고 필요할 때 생성(view) | 유지 |
| D-F06 | 의존성·결정 색인은 `relation`에 값으로만 관리(view) | 유지 |
| D-V05 | 컨텍스트 잠금은 스크립트가 자동 기록(`context_read`), AI 협업 조회 강조 | 유지 |
| 연결 방식 | MCP 서버 + 코딩 에이전트(Claude Code, Codex) | 유지(D1) |

### 1.3 원 설계 미결 (원문 7.2)

| # | 내용 | 현재 |
|---|---|---|
| Q5 | 프로젝트 사실 `cap.*` 37개: 문서관리 도구 자체에 대한 답과 관리 대상 저장소에 대한 답이 섞여 범위 확인 중 | 미결 O5. 시작 카탈로그에 필요한 사실만 먼저 정의 |
| Q6 | 종류별 `item.attrs` 스키마 | 미결 O6. K02·S03·L02부터 |

## 2. 2026-10-13 결정 (D1~D10)

결정자: 주정민. 선택지는 사용자 지침(방향이 다른 2안 + 융합 1안)에 따라 제시했다.

| # | 주제 | 결정 | 근거 | 버린 대안 |
|---|---|---|---|---|
| D1 | 작업 위치 | `j-ai-plugin` 안에 Claude Code·Codex 플러그인으로 둔다 | 기존 플러그인 마켓플레이스 관례 | 새 저장소, 새 저장소+MCP만 플러그인 |
| D2 | 카탈로그 원천·형식 | 시작 카탈로그 18종, JSON만(PyYAML 제거) | 원 카탈로그 없음(서버 검색·사용자 확인). 의존성 축소 | 미니 카탈로그 테스트 전용, 시작 카탈로그+종류 제안 기능, YAML 유지 |
| D3 | 런타임 | Python 3.10 이상 공통, `pysqlite3` 필수, 공식 `mcp` SDK | `mcp` 2.3.0 `Requires-Python>=3.10`. 서버 SQLite 3.26.0에 trigram 없음. `pysqlite3` 0.6.1: 3개 OS 휠, SQLite 3.53.4, 의존성 0 | 하한 3.6+기능 감지, 구성 요소별 하한 분리, 표준 라이브러리 MCP 서버 |
| D4 | 의존성 설치·인터프리터 | 명시적 `setup` 명령이 venv에 설치. 인터프리터는 설정 우선(Claude `userConfig.python_path`, Codex `command`), 기본 `python3` | 시작 지연·숨은 네트워크 사용 방지 | MCP 첫 시작 자동 설치, 직접 설치 안내, `uv run` |
| D5 | DB 위치·git | 프로젝트별 `.docdb/docdb.sqlite`, git 제외·로컬 전용 | SQLite 바이너리는 diff·병합 불가. Q2와 일치 | 단일 공용 DB, SQLite 커밋, 텍스트 덤프 커밋 |
| D6 | import | 스크립트가 분할·규칙 매핑·원문 복사, 에이전트는 CLI로 라벨만. LLM API 직접 호출 없음. 기본 `export_policy=approved_providers` | 기존 데이터 없음, 외부 전송 허용(사용자). 출력 토큰 최소화 | 헤드리스 CLI 호출, Batch API, MCP 도구 추가 |
| D7 | 항목 삭제 | 삭제 허용, `item_fts_ad` 트리거 추가 | 원본에 항목 검색 색인 삭제 트리거 없음 | draft만 삭제 허용, 삭제 금지·retired |
| D8 | 외부 전송 정책 | `local_only` 문서 본문은 에이전트 응답에 싣지 않음 | 원본이 `export_policy`를 읽지 않음 | — |
| D9 | 검증 | 로컬 Linux Python 3.10~3.14, GitHub Actions(Windows·macOS), push는 승인 후 | 여러 환경 요구 | 로컬만, 사용자 수동 확인 |
| D10 | 관계·항목 생성 | 결정적 추출은 스크립트가 확정. 에이전트는 `put` 확장(`i:`, `link`·`unlink`)으로 쓰되 관계는 `proposed`, 사람이 CLI로 확정·반려. 조회는 확정만. MCP 도구 6개 유지 | 원문 5.3 분담(관계 확정은 사람), 원본에 쓰기 경로 없음(B1) | 에이전트 즉시 확정+사후 검토, 관계 전용 MCP 도구 추가 |

결정 과정에서 바뀐 것: D3는 처음 제시한 "Python 3.6 이상 + 기능 감지"에서, 사용자 선택(MCP는 공식 SDK, SQLite는 번들 패키지)과의 충돌을 확인한 뒤 "3.10 이상 공통"으로 확정했다.

### 2.1 2026-10-13 2차 결정 — A 등급 권장안 일괄 채택

사용자가 4절의 A 등급 권장안 전부 채택을 선택했다(O7은 결정이 아닌 실측 작업이라 제외). PMT D11~D25.

| # | 원 번호 | 결정 |
|---|---|---|
| D11 | O1 | 관계 12종 방향은 [02](02-data-model.md) 5절 제안안 |
| D12 | O2 | 시작 카탈로그 연결 규칙은 [03](03-relations.md) 6절 제안안 |
| D13 | O3 | 의존성 venv는 도구 공용 데이터 폴더(`DOCDB_HOME` → OS 사용자 데이터 폴더) |
| D14 | O4 | 플러그인 코드는 `j-ai-plugin/docdb/`, 구상·결정·계획 문서는 `j-ai-plugin/docs-db/` |
| D15 | O8 | 작업 생성·상태 변경과 인계 작성은 `put` 확장. 사실 확정, 수신 확인, 승인, 실행 적재, gate, 렌더는 CLI |
| D16 | O9 | 구조 값 요약은 스크립트 템플릿, 서술 필드 요약은 `put` 때 에이전트가 `sum`으로 동봉 |
| D17 | O10 | 문서·항목은 의미 있는 이름(형식 검사), 실행·인계·승인·파일은 연도별 자동 순번 |
| D18 | O11 | 도구 단위 actor(`ai-claude`, `ai-codex`, MCP 설정 환경 변수로 고정), 모델·세션은 별도 기록, 사람은 `u-<계정>` |
| D19 | O12 | 저장 전 LF·NFC 정규화, 정규화 값으로 저장·digest |
| D20 | O13 | WAL + `busy_timeout` + 쓰기는 `BEGIN IMMEDIATE` |
| D21 | O15 | `PRAGMA user_version`, 시작 시 불일치 감지·안내, `docctl migrate`가 백업 후 적용 |
| D22 | O17 | `docctl backup`(SQLite 온라인 백업) 날짜별 보관, migrate·import 전 자동 |
| D23 | O19 | 쓰기 시 패턴 검사로 거부, 사람이 승인한 `policy_exception`만 허용 |
| D24 | O23 | AI는 planned·active·blocked·review까지, verified·closed·cancelled는 사람 |
| D25 | O26 | 에이전트 작업 진행은 PMT, 문서 변경 묶음 `work`는 docdb. `work.base_refs`에 PMT Item ID, `project_id`는 PMT slug |

### 2.2 2026-10-13 3차 결정 — 실측 반영

T00b 실측([08](08-verification.md) 3.1절) 뒤 사용자가 선택했다.

| # | 원 번호 | 결정 |
|---|---|---|
| D26 | O7(S2) | Codex MCP 서버는 `"cwd": "."`(플러그인 루트)와 상대 `args`로 실행하고, 프로젝트 폴더는 `env_vars`로 받은 `PWD`를 `docdb_launch.py mcp`가 `DOCDB_START_DIR`가 없을 때만 옮겨 쓴다. `paths.find_db`는 바꾸지 않는다. 대안(`find_db`에 `PWD` 추가, Codex 호출 메타의 workspaces 사용)은 채택하지 않음. 한계: `PWD`는 Codex를 띄운 셸의 폴더라 `codex -C`·데스크톱 앱에서는 다를 수 있고 그때는 `NO_DB`로 드러난다 |

## 3. 원 설계와 달라진 점

| 원 설계 | 지금 | 이유 |
|---|---|---|
| 카탈로그 YAML(`catalogue.yaml`) | JSON | D2 의존성 축소 |
| SQLite 3.45.1·Python 3.13에서 검증 | `pysqlite3` 내장 SQLite 3.53.4, Python 3.10~3.14 | D3 환경 독립 |
| `put`은 필드만 | 항목·관계까지 | D10 |
| 관계에 상태 없음 | `proposed`·`confirmed`·`rejected` | D10 |
| `export_policy` 미적용 | 읽기 경로 필터 | D8 |
| import 없음 | 스크립트+에이전트 라벨 | D6 |

## 4. 결정 대기 목록

정형적으로 결정해 기록해야 할 항목 전체다(2026-10-13 조사). 결정되면 이 표에서 2절(D 번호)로 옮기고 PMT에 기록한다. 선택지는 (a)·(b)가 방향이 다른 안, (c)가 융합안이다. 권장안은 근거와 함께 적었다.

시점: **A** 구현 전 필수(이것 없이 구현 계획을 쓸 수 없음) · **B** 구현 중 해당 기능을 만들 때 · **C** 운영·확장 단계.

요약: 총 32건 — A 16건(15건 확정 D11~D25, O7은 실측 작업), B 12건, C 4건. 결정 대기는 B 12건, C 4건이다.

### 4.1 데이터 모델·식별

| # | 결정할 것 | 근거 | 선택지 | 권장 | 시점 |
|---|---|---|---|---|---|
| O1 | 관계 12종의 방향 정의 | 원 표준(STD L508–523) 없음 | 제안안 채택 / 수정 / 일부 종류만 채택 | 제안안 채택([02](02-data-model.md) 5절) | A → **D11 확정** |
| O2 | 시작 카탈로그 종류 간 연결 규칙 | 원 카탈로그 `cat_kind_link` 없음 | 제안안 채택 / 수정 / 필수 없이 선택만 | 제안안 채택([03](03-relations.md) 6절) | A → **D12 확정** |
| O5 | 프로젝트 사실 `cap.*` 범위 | 원 Q5: 도구 자체와 관리 대상 저장소에 대한 답이 섞임 | (a) 관리 대상 저장소 기준만 (b) 도구·저장소 둘 다 별도 키 (c) 시작 카탈로그 종류의 적용 조건에 쓰이는 사실만 먼저 | (c) | B |
| O6 | 종류별 `item.attrs` 스키마 | 원 Q6. 기대값 검증(DRIFT 판정)의 기준 | (a) 종류마다 JSON Schema 정의·검사 (b) 자유 JSON (c) K02·S03·L02만 스키마, 나머지 자유 | (c) | B |
| O10 | ID 체계와 발급 주체 | `work_id`만 형식 검사(GLOB)가 있고 `doc_id`·`run_id`·`handoff_id`·`approval_id`·`artifact_id`는 자유 텍스트. 원 주석 "doc_id 불변, 재사용 금지" | (a) 모두 종류 접두어+순번 자동 발급(`DOC-D01-0001`, `RUN-2026-0001`)과 형식 검사 (b) 사람이 의미 있는 이름 지정(`DOC-REQ-ORDERS`), 스크립트는 형식·중복만 검사 (c) 문서·항목은 의미 있는 이름, 실행·인계·승인·파일은 자동 순번 | (c) | A → **D17 확정** |
| O11 | 행위자(actor) 식별 단위 | 감사(`change_log`·`context_read`)와 권한(사람/AI 구분)의 기준. 원 코드 기본값은 `ai-agent` 하나 | (a) 도구 단위(`ai-claude`, `ai-codex`) (b) 모델 단위(`ai-claude-opus-5-5`) (c) 도구 단위 actor, 모델·세션은 `call_log`·`context_read.session_ref`에 기록. MCP 설정의 환경 변수로 고정. 사람은 `u-<계정>` | (c) | A → **D18 확정** |

### 4.2 저장·일관성

| # | 결정할 것 | 근거 | 선택지 | 권장 | 시점 |
|---|---|---|---|---|---|
| O12 | 텍스트 정규화 | `digest()`가 원문 그대로 해시함. Windows 줄바꿈(CRLF)이나 macOS 파일명 유니코드 분해형(NFD)이면 같은 내용도 digest가 달라져 불필요한 `CONFLICT`·`STALE` 발생 | (a) 저장 전 LF·NFC로 정규화하고 그 값으로 저장·digest (b) 원문 저장, digest만 정규화 값으로 (c) 정규화 안 함 | (a) | A → **D19 확정** |
| O13 | 동시 쓰기 | 원본은 `PRAGMA foreign_keys`만 설정, `journal_mode`·`busy_timeout` 없음. Claude와 Codex가 같은 프로젝트 DB를 동시에 쓸 수 있음 | (a) WAL 모드 + `busy_timeout` + 쓰기는 `BEGIN IMMEDIATE` (b) 기본 저널 + 실패 시 재시도 (c) 파일 잠금으로 쓰는 프로세스를 하나로 제한 | (a) | A → **D20 확정** |
| O14 | 이력 저장 범위 | `put`이 `before_json`·`after_json`에 필드 전문을 모두 저장. 수정마다 전문 2벌(중복 금지 DEC-09와 긴장) | (a) 전문 2벌 유지 (b) `after`만 전문, `before`는 직전 행 `after`로 복원 (c) 차이(diff)만 저장 | (b) | B |
| O15 | DB 구조 버전과 마이그레이션 | `cat_version.schema_version`은 카탈로그용이며 DB 구조 버전 기록(`PRAGMA user_version`)이 없음. 11-schema 7절 변경부터 기존 `.docdb` 갱신 필요 | (a) 시작할 때 자동 적용 (b) 수동 `docctl migrate`만 (c) 시작할 때 버전 불일치를 감지해 안내, `docctl migrate`가 백업 후 적용 | (c) | A → **D21 확정** |
| O16 | 카탈로그 변경 호환성 | `field_value`가 FK로 `cat_kind_field`를 참조. 필드 삭제·이름 변경 시 기존 값이 FK 위반 | (a) 삭제 금지, 폐기 표시만 (b) 이름 변경 시 매핑표로 값 이전 (c) (a)+(b) | (c) | B |
| O17 | 백업·복구 | D5로 `.docdb`는 git 제외·로컬 전용. 디스크 손실 시 복구 수단 없음 | (a) `docctl backup`(SQLite 온라인 백업)을 날짜별 보관, 마이그레이션·import 전 자동 실행 (b) 사용자가 수동 복사 (c) 내보내기(O29)를 git에 커밋 | (a) | A → **D22 확정** |
| O18 | 보존·정리 | `call_log`·`context_read`·`change_log`가 계속 증가. `retention_policy_ref` 값 미정 | (a) `call_log`만 기간(예: 90일) 정리, 나머지 영구 (b) 모두 영구 (c) 크기 기준 경고만 | (a) | C |

### 4.3 보안·정책

| # | 결정할 것 | 근거 | 선택지 | 권장 | 시점 |
|---|---|---|---|---|---|
| O19 | 민감 정보 검사 | 제약 S-11(비밀번호·토큰·키 저장 금지). 원본 코드에 검사 없음 | (a) 쓰기 시 형식 패턴(키·토큰·비밀번호)을 검사해 거부 (b) 경고만 하고 저장 (c) 거부하되 사람이 예외 승인(`policy_exception`) | (c) | A → **D23 확정** |
| O20 | `classification`과 `export_policy`의 관계 | 두 컬럼이 따로 있음. `restricted`는 원 설계상 PostgreSQL 행 보안이 필요한 조건(DEC-07) | (a) `confidential`·`restricted`면 `local_only` 강제 (b) 독립 유지 (c) `restricted`만 `local_only` 강제 | (c) | B |

### 4.4 에이전트 접근

| # | 결정할 것 | 근거 | 선택지 | 권장 | 시점 |
|---|---|---|---|---|---|
| O8 | docctl 본체 명령 형식과 에이전트 노출 범위 | [10](10-operation.md) 5.1절 | 작업·인계는 `put` 확장, 사람·CI 전용은 CLI / 전부 CLI / 전용 MCP 도구 추가 | 첫째 안 | A → **D15 확정** |
| O9 | 요약(L1) 생성 경로 | [10](10-operation.md) 5.2절 | 구조 값은 템플릿, 서술 필드는 `put` 때 동봉 / CLI 반복 / 요약 없이 발췌 | 첫째 안 | A → **D16 확정** |
| O21 | 도구 설명 언어 | 원문 측정: 도구 6개 정의 2,411자·약 769 tok, "설명이 한국어라 크게 잡힘". 매 요청 고정 비용(T1) | (a) 영어 (b) 한국어 유지 (c) 도구 설명은 영어, 서버 지침·스킬·응답 본문은 한국어 | (c) | B |
| O22 | 토큰 추정 계수 보정 | 원문 N2: `count_tokens` API로 보정 필요. D6(API 직접 호출 없음), API 키 없음 | (a) 휴리스틱 유지, 비율로만 사용 (b) API 키를 받아 한 번 측정 후 계수 고정 (c) (a)로 시작, 키가 생기면 (b) | (c) | B |

### 4.5 작업 흐름·거버넌스

| # | 결정할 것 | 근거 | 선택지 | 권장 | 시점 |
|---|---|---|---|---|---|
| O23 | 작업 상태 전이 권한 | `work.status` 7종(planned~cancelled). 제약 S-07(AI는 최종 책임·승인 불가) | (a) AI는 planned·active·blocked·review까지, verified·closed·cancelled는 사람 (b) 제한 없음, 이력으로 추적 (c) closed만 사람 | (a) | A → **D24 확정** |
| O24 | 문서 상태 전이 | `document.status` 5종. `approved`가 승인 기록 없이도 설정 가능 | (a) `approved`는 승인된 기준선이 있을 때만(트리거) (b) 스크립트 검사만 (c) 상태는 자유, 승인 여부는 별도 표시 | (a) | B |
| O25 | gate 종류와 통과 기준 | `approval.gate` 예(design-approval, pre-merge, pre-release, work-close)만 있고 기준 없음 | (a) 문서 승인 gate 하나 (b) 예 4종 전부 (c) 문서 승인 + 작업 종료 2종 먼저 | (c) | B |
| O26 | PMT·ai-memory와의 경계, `project_id` 연결 | 사용 환경에서 PMT가 작업·결정·인계를, ai-memory가 팀 작업 기록을 관리. docdb에도 `work`·`handoff`·결정 색인(F06)이 있어 같은 사실이 여러 곳에 생길 위험(U-04) | (a) docdb는 문서 데이터만, 작업·인계 원천은 PMT (b) docdb가 작업·인계 원천, PMT는 참조 (c) 에이전트 작업 진행은 PMT, 문서 변경을 묶는 작업 단위는 docdb `work`. `work.base_refs`에 PMT Item ID, `project_id`는 PMT slug | (c) | A → **D25 확정** |

### 4.6 산출물

| # | 결정할 것 | 근거 | 선택지 | 권장 | 시점 |
|---|---|---|---|---|---|
| O27 | 렌더 산출물 형식·위치·git | DB는 로컬 전용(D5)이라 공유 수단은 생성 문서뿐 | (a) md를 저장소에 출력·커밋 (b) 출력만, 커밋 안 함 (c) 승인된 기준선만 md로 커밋 | (c) | B |
| O28 | 영문 brief 생성 범위 | 스키마에 `brief_preserve`·`brief_exemption_*` 필드 있음(원 표준 5.2). 생성에는 LLM 필요(D6 제약) | (a) 범위 밖, 필드만 유지 (b) 에이전트가 작성(O9 경로) (c) 고위험 문서만 | (a) | C |
| O29 | 내보내기 형식 | 원 Q2 "YAML 내보내기"이나 PyYAML 제거(D2) | (a) 정렬된 JSON(결정적 출력) (b) 의존성 없는 YAML 출력기 (c) SQL 덤프 | (a) | C |

### 4.7 배포·운영

| # | 결정할 것 | 근거 | 선택지 | 권장 | 시점 |
|---|---|---|---|---|---|
| O3 | 의존성 설치 폴더 | [06](06-runtime-plugin.md) 4절 | 도구 공용 데이터 폴더 / `CLAUDE_PLUGIN_DATA` / 도구별 폴더 | 도구 공용 | A → **D13 확정** |
| O4 | 플러그인 폴더 이름 | D1은 `docdb/`, 구상 폴더는 `docs-db/` | `docdb/` / `docs-db/` / 구현은 `docdb/`, 구상 문서는 `docdb/docs/`로 이동 | 사용자 결정 | A → **D14 확정** |
| O7 | 실측 S1~S5 | 결정이 아닌 확인 작업. 결과로 O3·O30과 Codex 설정 확정 | — | 구현 계획 첫 단계 | A |
| O30 | 공식 지원 플랫폼 | `pysqlite3` 휠 없음: Linux aarch64, macOS Intel. 실측 S5 후 | (a) 휠 있는 플랫폼만 지원 (b) 소스 빌드 안내 포함 전부 지원 (c) 휠 플랫폼은 공식, 그 외는 최선 노력 | (c) | B |
| O31 | 오프라인 설치 | `setup`은 PyPI 접속 필요 | (a) 온라인만 (b) `setup --wheels <폴더>`로 미리 받은 휠 사용 (c) 플러그인에 휠 동봉 | (b) | C |
| O32 | 버전 정책 | 플러그인·마켓플레이스·DB 구조 버전을 함께 관리해야 함(저장소 README 규칙: 버전 함께 갱신) | (a) 의미적 버전(semver): 스키마 변경은 minor, 호환 깨짐은 major (b) 날짜 버전 (c) 플러그인과 DB 구조 버전을 따로 두고 호환표 유지 | (a), DB 구조 버전은 O15의 `user_version` | B |
