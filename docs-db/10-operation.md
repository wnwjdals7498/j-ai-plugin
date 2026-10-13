# 10. 예상 동작 방식

설계 기준으로 예상하는 동작이다. 구현 전이다. `[형식 미정]` 표시는 작성 당시 기준이며, 명령 형식은 D15와 구현 계획([plan/](plan/README.md)) 계약에서 정했다.

## 1. 참여자

| 참여자 | 하는 일 | 접근 경로 |
|---|---|---|
| 사람 | 프로젝트 사실 확정, 관계 확정, 승인, 인계 수신 확인, 카탈로그 변경 | CLI(`docctl`) |
| 에이전트(Claude Code, Codex) | 작업 재개, 필드 본문 작성, 항목 작성, 관계 제안, import 라벨 | MCP 도구 6개, CLI |
| 스크립트 | 검증, 이력 기록, digest 계산, 결정적 추출, 적용 판정, 렌더링 | docctl 내부 |
| CI(자동 테스트·배포 체계) | 테스트·점검 결과 적재 | CLI `[형식 미정]` |

모든 쓰기는 docctl 한 경로를 지나고, `change_log`에 체인으로 남는다. 핵심 규칙은 DB 트리거도 막는다(AI 승인, AI의 관계 확정, 이력 수정, 실행 증거 덮어쓰기, legal_hold 삭제, 경로 탈출).

## 2. 시나리오

### 2.1 설치 (기기당 1회)

```text
1 사람   claude plugin install docdb@j-ai-plugins   (Codex: codex plugin add …)
2 사람   /plugin 에서 python_path 지정 (기본 python3, Windows는 py 또는 절대 경로)
3 사람   python <plugin>/src/docdb_launch.py setup
          → 데이터 폴더(~/.local/share/docdb 등)에 venv 생성, mcp·pysqlite3 설치
4 새 세션 → MCP 서버 시작: Python ≥3.10, venv, trigram·JSON 검사 통과 → 도구 6개 등록
```

| 실패 상황 | 동작 |
|---|---|
| Python 3.10 미만 | stderr 안내, 종료 코드 2. `/mcp`에 서버 실패로 표시 |
| `setup` 안 함 | "setup 실행" 안내, 종료 코드 2 |
| Python을 바꿔 venv 버전 불일치 | "setup 다시 실행" 안내, 종료 코드 2 |

### 2.2 프로젝트 시작 (프로젝트당 1회)

```text
1 사람/에이전트  docctl init
                 → .docdb/docdb.sqlite 생성, 스키마·토큰 확장 적재
                 → 시작 카탈로그 18종 적재, 시드는 적재된 종류만(건너뛴 수 출력)
                 → .docdb/.gitignore(*) 생성 → git에서 자동 제외
2 스크립트       미확인 사실(cap.*) 질문 목록 출력                     [형식 미정]
3 사람           사실 답변(참/거짓/미확인)                              [형식 미정]
4 스크립트       적용 판정 → 종류별 필수·비필수·미확인 목록            [형식 미정]
```

미확인 사실은 비적용으로 처리하지 않는다(S-04). 판정이 `unknown`인 종류는 필수 문서 목록에 "미확인"으로 남는다.

### 2.3 작업 시작·재개 (매 작업)

```text
1 사람/에이전트  작업 생성 WORK-2026-0001 "서버 점검 자동화"           [형식 미정]
2 에이전트       docdb_ctx("WORK-2026-0001")
   ← #ctx w:WORK-2026-0001 docs=4 items=40 as_of=c:49 tok=1514/1500 omitted=13
     W 작업 카드, > 다음 행동, ! 차단 요인(실패·미실행 실행, 미해결 지적, 낡은 근거, 필수 연결 누락, 확정 대기 관계)
     D 관련 문서 L0(누락 필드 표시), I 항목 종류별 개수, f: 필드 L1
3 스크립트       읽은 대상을 context_read에 자동 기록(시점 c:49)
```

다음 세션에서는 `docdb_since(49)`로 그 사이 바뀐 것만 받는다(측정: 전체 재독 대비 1.4%).

### 2.4 문서 작성·수정

```text
1 에이전트  docdb_get(["f:DOC-REQ-HWCHECK/D01/behavior"], level=2)   ← 전문 + dg=1bf8…
2 에이전트  docdb_put([{h, op:"replace_section", anchor:"## 임계값", text, expect:"1bf8…"}])
   ← APPLIED ok=1 / OK f:… dg=7c2a… tok=331                         (본문 되돌려주지 않음)
3 스크립트  field_value 갱신 → change_log 체인 추가 → 검색 색인 갱신 → 요약은 낡음 표시
            → 작업-문서 관계(R5 changes) 확정 기록
            → 바뀐 필드에서 ID 정의(R1)·언급(R2) 추출 → 항목·관계 확정 기록
```

| 상황 | 동작 |
|---|---|
| 그 사이 다른 사람이 수정 | `ERR CONFLICT now=<dg>` → `since`로 변경분 확인 후 다시 패치 |
| 묶음 중 1건 실패 | 전부 취소(`NOTHING_APPLIED`) |
| 카탈로그에 없는 필드 | `ERR DB` (FK 거부) |
| 모르는 값 | `state: "tbd"` + `why` 필수(사유 없으면 거부) |
| `local_only` 문서 | `get`·`find`·`ctx`에 본문 없이 핸들·제목만 |

### 2.5 관계 쌓기 (D10)

```text
1 에이전트  docdb_put([{op:"link", from:"i:REQ-HW-002", rel:"verified_by", to:"i:TC-HW-002", why:"…"}])
   ← OK rel=41 proposed
2 다음 ctx  ! 확정 대기 관계 1건
3 사람      docctl link review → docctl link confirm 41   (또는 reject 41 --why)
4 이후      v_rtm·trace·필수 연결 검사에 반영(확정된 관계만)
```

에이전트가 확정을 시도하면 트리거가 거부한다. 반려된 조합을 다시 제안하면 `ERR REJECTED_BEFORE rel=41`.

### 2.6 실행 증거 적재 (CI·점검)

```text
1 CI       테스트·하드웨어 점검 실행
2 CI       결과 적재 → run(result=PASS|FAIL|NOT_RUN|ERROR|UNKNOWN, 대상 커밋, 환경 스냅샷)
           + finding(지적) + artifact(원시 로그는 경로·digest만)              [형식 미정]
3 스크립트 R4: run evidence_for 대상 테스트·요구사항 (확정)
4 이후     실패·미실행 run과 미해결 finding은 ctx 차단 요인으로 표시
```

같은 실행 기록은 고칠 수 없다(트리거). 다시 실행하면 새 `run`을 쌓는다. 서버 점검에서 관측값이 `item.attrs`의 기대값과 다르면 `finding(DRIFT)`가 생긴다.

### 2.7 인계·수신

```text
1 에이전트/사람  인계 작성: 완료·미완료·위험·다음 행동·미실행 + 마지막 커서 as_of    [형식 미정]
2 받는 사람      docdb_ctx 또는 docdb_since(커서)부터 시작
3 받는 사람      수신 확인: 기준 커밋을 직접 확인해야 accepted 가능(CHECK 제약)     [형식 미정]
```

`v_work_resume`이 마지막 인계, 다음 행동, 미해결 지적 수, 실패·미실행 수, 낡은 근거 수를 한 행으로 보여 준다(U-01).

### 2.8 검토·승인

```text
1 스크립트  gate 검사: 필수 필드 누락, 필수 연결 누락, 미해결 지적                  [형식 미정]
2 스크립트  통과 시 doc_revision 생성(메타·필드·관계 고정 사본, 직전 기준선과 체인)
3 사람      approval(approved|rejected) — AI 행위자는 트리거가 거부
```

### 2.9 문서 생성

```text
1 사람/스크립트  render: 문서 md, RTM, 등록부, 상태 파일 출력                       [형식 미정]
2 스크립트       generation에 원본 digest·생성기 버전 기록
3 이후           원본이 바뀌면 해당 생성물은 STALE
```

생성물은 직접 고치지 않는다. DB를 고치고 다시 생성한다(DEC-02).

### 2.10 기존 문서 import

```text
1 사람/에이전트  docctl import scan ./old-docs
                 → 규칙으로 문서 종류·필드를 정한 구간은 바로 적용
2 에이전트       docctl import next  → 미분류 구간 한 줄씩(제목, 앞 200자, 후보 필드)
3 에이전트       docctl import label '[{"seg":"s:12","kind":"D01","field":"behavior"}]'
                 → 스크립트가 원본에서 구간을 다시 읽어 digest 확인 후 복사, R1·R2 추출
4 반복           next가 비거나 omitted=0이 될 때까지. 큰 묶음은 서브에이전트·새 세션으로
5 사람/에이전트  docctl import report  → 매핑 비율, 남은 수
```

### 2.11 이력 조회·감사

| 질문 | 동작 |
|---|---|
| 이 필드는 어떻게 바뀌어 왔나 | `change_log` 재귀 순회 또는 `seq`로 N번째 직접 조회 |
| 최근 누가(사람/AI) 바꿨나 | `v_recent_changes` |
| 기록이 위조되었나 | 체인 삽입 트리거가 깨진 연결을 거부. 행 내용 해시 재계산은 스크립트가 검사 |
| 잘못 기록했다 | 수정·삭제 불가. `op='correct'` 정정 행 추가 |

## 3. 토큰 사용 감각

| 항목 | 크기 | 근거 |
|---|---|---|
| 도구 6개 정의 | 약 769 tok(추정) | `test_mcp.py` 측정. Claude Code는 도구 검색 기본값으로 필요할 때만 실림 |
| 작업 시작 `ctx` | 예산 1,500 tok | 원시 덤프의 7.3% |
| 재개 `since` | 수백 tok 이하 | 재독의 1.4% |
| 바뀌지 않은 필드 재조회 | 21 tok | `UNCHANGED` |
| 쓰기 응답 | 한 줄 | 본문 미반환 |

## 4. 하루 흐름 예

```text
09:00 에이전트 ctx(WORK-2026-0001)       → 차단 요인: RUN-0002 NOT_RUN, 확정 대기 관계 1건
09:05 사람     link confirm 41
09:10 에이전트 get L2 → put(replace_section) → OK
09:30 CI       부하 테스트 적재 → RUN-0003 PASS → R4 evidence_for
10:00 에이전트 인계 작성(as_of=c:57)
14:00 다른 사람 since(57) → 바뀐 3건만 확인 → 수신 확인(기준 커밋 확인)
```

## 5. 이 정리에서 드러난 공백

### 5.1 docctl 본체 명령 형식 (확정 D15, PMT 백로그 B3)

1. 예상: 작업 생성, 사실 입력, 적용 판정, 실행 적재, 인계 작성·수신, gate·기준선·승인, 렌더 명령이 있어야 2.2~2.9 흐름이 동작한다.
2. 실제: 이 테이블들(`work`, `fact_value`, `run`, `handoff`, `approval`, `doc_revision`)에 쓰는 코드는 원본 테스트(`test_scenarios.py`, `test_token.py`)의 SQL 직접 실행뿐이다. 원문은 "docctl 기능 목록"을 다음 단계로 남겼다(`docdb-design.md:376`).
3. 근거: 원본 도구 코드 grep 결과. 원문 토큰 명세 5.1은 이 기능들을 "스크립트 전용"으로 이름만 나열한다.
4. 원인: 원 설계가 스키마와 LLM 읽기 경로까지만 구현되었다.
5. 수정 방향(결정 필요):
   - (a) 권장: 에이전트 작업 흐름에 필요한 쓰기(작업 생성·상태 변경, 인계 작성)는 `put` 확장(`w:` 핸들, `handoff` 연산)으로 하고, 사람·CI 전용(사실 확정, 수신 확인, 승인, 실행 적재, gate, 렌더)은 CLI로 한다. 도구 6개를 유지한다.
   - (b) 전부 CLI로 하고, 에이전트는 셸로 CLI를 실행한다(MCP는 읽기와 필드·항목·관계 쓰기만).
   - (c) 작업·인계 전용 MCP 도구를 추가한다(7개 이상).
6. 영향도: 정하지 않으면 에이전트가 `ctx`에 넘길 작업을 만들 수 없어 2.3부터 시작할 수 없다.

### 5.2 요약(L1) 생성 경로 충돌 (확정 D16, PMT 백로그 B4)

1. 예상: 서술 필드의 L1 요약(1~3문장)이 만들어져 `ctx`·`get level=1`에 쓰인다.
2. 실제: 원문은 "값싼 모델로 배치 생성"(`docdb-token-spec.md:292` N3)인데, D6은 LLM API 직접 호출을 금지한다.
3. 근거: 두 문서의 문구가 서로 맞지 않는다.
4. 원인: D6을 import 기준으로 정하면서 요약 생성은 검토하지 않았다.
5. 수정 방향(결정 필요):
   - (a) 권장: 구조 값(항목, JSON 필드, 실행)의 요약은 스크립트 템플릿으로 만들고, 서술 필드는 에이전트가 `put`할 때 요약(`sum`)을 함께 제출한다. 요약이 없거나 낡으면 `ctx`는 `sum=none/stale`로 표시한다.
   - (b) 에이전트가 CLI 반복(`summarize next/put`, import와 같은 방식)으로 낡은 요약을 채운다.
   - (c) 요약 없이 스크립트 발췌(앞부분)만 쓴다.
6. 영향도: 요약이 없어도 동작은 한다(측정 데이터도 `sum=none` 상태였다). 다만 L1 단계의 정보량이 줄어 L2 전문 조회가 늘어난다.
