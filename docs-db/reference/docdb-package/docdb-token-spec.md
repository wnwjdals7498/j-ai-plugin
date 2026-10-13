> ⚠️ **【주의】 출처가 없는 정보가 포함되어 있음** — `[설계 판단]` 표시가 붙은 항목은 공식 문서에 근거가 없는 제 판단입니다. `[측정]` 표시는 이 패키지의 테스트로 잰 값이며, 토큰 수는 **보정 전 추정치(tok_est)**입니다. 출처가 있는 내용을 먼저 적고, 설계 판단은 따로 구분했습니다.

# 문서 DB 토큰 최소화 명세 — 저장·기능·구조·호출·조합

| 항목 | 내용 |
|---|---|
| 문서 ID | `DOCDB-TOKEN-001` (초안) |
| 작성일 | 2026-10-08 (Asia/Seoul) |
| 대상 | `docdb-design.md`(DOCDB-DESIGN-001), `docdb_schema.sql` |
| 연결 방식 | MCP 서버 + 코딩 에이전트(Claude Code, Codex) — 2026-10-08 확정 |
| 함께 제공 | `docdb_schema_token_ext.sql`, `docdb_tools/docctl_llm.py`, `docdb_tools/docdb_mcp.py`, `docdb_tools/test_token.py`, `docdb_tools/test_mcp.py` |

---

## 0. 요약

1. **저장량은 토큰과 상관없습니다.** 토큰은 요청에 실리는 것(도구 정의, 지침, 도구 결과, 대화 이력)과 모델이 쓰는 출력에서 생깁니다(1장). 그래서 목표는 **"저장은 세밀하게, 싣는 것은 최소로"**입니다.
2. **기존 스키마는 저장 쪽이 이미 잘 되어 있습니다.** 필드 단위 저장, 관계 1테이블, 고정 ID, change_log 구조를 그대로 씁니다. 바꿀 것은 **읽기 경로**입니다.
3. **추가하는 것은 4가지입니다.** 값 옆의 `digest`·`tok_est`, 파생 요약(`entity_summary`), 전문 검색 색인(`fts_text`), 호출 로그(`call_log`). 기존 테이블은 컬럼만 추가합니다.
4. **LLM에 주는 도구는 6개입니다.** `ctx`(작업 시작·재개) · `get`(핸들 조회) · `find`(검색) · `trace`(관계) · `since`(변경분) · `put`(쓰기). 승인·렌더·카탈로그 변경은 노출하지 않습니다.
5. **[측정] 같은 일을 할 때:** 작업 시작 묶음은 원시 덤프의 **7.3%**, 재개는 전체 재독의 **1.4%**, 바뀌지 않은 값 재조회는 413→**21** tok, 부분 수정 출력은 318→**58** tok였습니다(8장, 합성 데이터).

---

## 1. 전제 정정 — "저장 데이터가 많으면 토큰이 많이 든다"

**정정:** DB에 저장된 양은 토큰에 들어가지 않습니다. 모델이 한 번의 요청에서 받는 내용과 생성하는 내용만 계산됩니다.

- 근거: "Everything in the request counts toward the context window: the system prompt, every message in `messages` (including tool results, images, and documents), and your tool definitions. The output Claude generates for the turn, including its extended thinking, counts too." — Context windows, "How the context window works"
- **예:** `field_value`에 10만 행이 있어도, 에이전트가 `ctx`로 1,500 tok만 받으면 그 요청의 DB 관련 입력은 1,500 tok입니다. 반대로 행이 20개뿐이어도 `SELECT *` 결과를 통째로 받으면 그만큼 듭니다.

### 1.1 이 시스템에서 토큰이 드는 4곳

| # | 어디서 | 성격 | 근거 | 이 명세의 대응 |
|---|---|---|---|---|
| T1 | 도구 정의(이름·설명·스키마) + 서버 지침 | 매 요청 고정비 | Tool use "Pricing": "The `tools` parameter in API requests (tool names, descriptions, and schemas)" | 도구 6개, 짧은 설명 (6.3) |
| T2 | 도구 결과 | 한 번 실리면 이후 **매 턴 다시 전송** | Claude Code costs "Why usage climbs in a long session": "Claude Code sends your full conversation with every request, and each time Claude uses tools it sends another request carrying that batch of tool results." | 예산·단계적 공개·변경분만 (TOK-P3~P6) |
| T3 | 모델 출력 | 입력보다 단가가 높음 | Pricing "Model pricing": Opus 5.5 입력 $4 / 출력 $20 per MTok, Sonnet 5.5 입력 $2 / 출력 $10 | 패치로 쓰기 (TOK-P5) |
| T4 | 실패·재시도 | 오류 메시지 + 다시 읽기 + 다시 쓰기 | [설계 판단] | 원자적 쓰기, 짧은 오류 코드 (5.2) |

- 캐시는 비용을 줄이지만 자리를 줄이지는 않습니다: "Cached prompt prefixes still occupy the context window: prompt caching changes what you pay for those tokens, not whether they count." — Context windows
- 많이 싣는 것은 정확도에도 손해입니다: "As token count grows, accuracy and recall degrade, a phenomenon known as *context rot*." — Context windows

---

## 2. 설계 원칙 (TOK-P1 ~ P7)

| ID | 원칙 | 근거 |
|---|---|---|
| TOK-P1 | **저장은 세밀하게, 싣는 것은 최소로.** 정규화·필드 단위 저장은 유지 | 1장 |
| TOK-P2 | **조인·집계·필터는 스크립트(SQL)가 하고, 모델은 결과만 받는다.** 모델이 두 목록을 받아 맞춰 보게 하지 않음 | Agent Skills "Level 3": "Scripts run through bash, and only their output enters context" · Claude Code costs "Offload processing to hooks and skills": "a hook can grep for `ERROR` and return only matching lines, reducing context from tens of thousands of tokens to hundreds." |
| TOK-P3 | **단계적 공개(L0 → L1 → L2).** 목록은 한 줄, 필요하면 요약, 고칠 필드만 전문 | Agent Skills "How Skills work": "Claude loads information in stages as needed, rather than consuming context upfront." (Level 1 ~100 tokens / Level 2 under 5k / Level 3 none until accessed) |
| TOK-P4 | **바뀐 것만 다시 보낸다.** 커서(`as_of=c:N`)와 digest로 판단 | [설계 판단] — change_log의 seq·digest 체인(docdb-design.md DEC-06)을 재사용 |
| TOK-P5 | **출력은 패치로.** 문서 전체를 다시 쓰지 않고 필드·절 단위로 바꾼다. 쓰기 응답은 본문을 되돌려주지 않는다 | 1.1 T3 (출력 단가 5배) |
| TOK-P6 | **모든 응답에 예산을 두고, 자른 것은 숫자로 알린다(`omitted=N`).** 조용히 자르지 않는다 | Claude Code MCP "MCP output limits and warnings": "Claude Code displays a warning when MCP tool output exceeds 10,000 tokens and limits output to 25,000 tokens by default." |
| TOK-P7 | **고정비를 작고 일정하게.** 도구 수를 적게, 설명은 짧게, 도구 목록을 상태에 따라 바꾸지 않는다 | Prompt caching "What invalidates the cache": 도구 정의를 바꾸면 tools·system·messages 캐시가 모두 무효 · Claude Code MCP "For MCP server authors": "Claude Code truncates each tool description and each server's instructions at 2,048 characters by default. Keep them concise, and put critical details near the start." |

---

## 3. 저장 방식 — 무엇을 더 저장하나

기존 원칙(DB가 원본, Git 원본은 참조만, 문서는 생성물, 중복 금지 — docdb-design.md DEC-01·02·09)은 그대로입니다. 아래는 **읽기 비용을 줄이려고 추가하는 것**입니다. 모두 `docdb_schema_token_ext.sql`에 있습니다.

| ID | 추가 | 목적 | 원본/파생 |
|---|---|---|---|
| ST-1 | `field_value.digest`, `item.digest` | 내용을 보내지 않고 "바뀌었나"만 비교 → `UNCHANGED` 한 줄, 쓰기 충돌 검사 | 파생(스크립트 계산) |
| ST-2 | `field_value.tok_est`, `item.tok_est` | 본문을 읽지 않고 예산 계산 (ctx가 무엇을 넣을지 고를 때) | 파생 |
| ST-3 | `entity_summary` (handle, summary, source_digest) + `v_summary_stale` | L1 요약. 원본 digest가 바뀌면 자동으로 낡음 표시 → 낡은 요약은 쓰지 않음 | **파생.** 원본이 아님 (docdb-design.md L51 S-05 "요약은 원본에 종속된 파생물") |
| ST-4 | `text_unit` + `fts_text` (FTS5, trigram, external content) + 트리거 5개 | 검색 결과로 **핸들 + 짧은 발췌**만 반환 | 색인(본문 복사 안 함) |
| ST-5 | `call_log` | 도구별로 실은 양(out_tok_est)·생략 수를 기록 → 어디서 토큰이 새는지 측정 | 운영 기록 |
| ST-6 | `v_l0_document`, 인덱스 4개 | 한 줄 표현을 SQL에서 바로 생성, `since`·`ctx` 조회 속도 | 뷰·색인 |

### 3.1 검색 색인 설계 메모

- **본문 복사 안 함:** FTS5 external content는 색인만 갖고 값은 원본 테이블(여기서는 뷰 `v_text_unit`)에서 읽습니다 — FTS5 §4.4 "The "content" option may be used to create an FTS5 table that stores only FTS full-text index entries."
- **rowid 고정:** `field_value`는 명시적 INTEGER PRIMARY KEY가 없어서, VACUUM 후 암묵 rowid가 바뀔 수 있습니다 — SQLite VACUUM §3 "The VACUUM command may change the ROWIDs of entries in any tables that do not have an explicit INTEGER PRIMARY KEY." 그래서 `text_unit.unit_id`(명시적 키)를 색인 rowid로 씁니다. [측정] VACUUM 후에도 검색 결과 핸들 유지 확인(S8).
- **한국어:** trigram은 부분 문자열 검색이라 "임계값을"처럼 조사가 붙은 단어도 찾습니다 — FTS5 §4.3.4 "a query or phrase token may match any sequence of characters within a row". 단 **3글자 미만은 찾지 못합니다** — 같은 절 Notes "Substrings consisting of fewer than 3 unicode characters do not match any rows when used with a full-text query." [측정] `MATCH "장애"` = 0건. 그래서 `find`는 2글자 이하 검색어를 `LIKE`(전체 탐색)로 바꿉니다.
- **발췌 길이:** `snippet()`은 최대 64 토큰(FTS 토큰)까지 잘라 줍니다 — FTS5 §5.1.3. `find`는 8로 씁니다 [설계 판단].

### 3.2 L1 요약 작성 규칙 [설계 판단]

| 대상 | 누가 | 언제 | 형식 |
|---|---|---|---|
| 구조 값(item, json 필드, run) | 스크립트 (템플릿) | put 직후 | 한 줄: ID·상태·핵심 수치 |
| 서술 필드(md) | LLM (값싼 모델로 배치) | `v_summary_stale`을 주기적으로 처리 | 1~3문장, 60 tok 이내. 숫자·ID·조건은 그대로 남김 |
| 요약이 없거나 낡았을 때 | — | — | `ctx`는 요약 대신 `sum=none/stale`과 tok만 표시. 내용은 `get`으로 |

요약은 한 번 쓰고 여러 번 읽히므로, 읽는 횟수가 많은 필드부터 만듭니다(`call_log`·`context_read`로 판단).

### 3.3 저장하지 않을 것

| 하지 않을 것 | 이유 |
|---|---|
| 렌더링한 문서(md/html)를 DB나 대화에 다시 넣기 | 생성물이고, 필드 단위로 읽는 편이 작음 (DEC-02) |
| 실행 원시 로그 전문 | `artifact`로 참조만 (DEC-09). 모델에는 `finding`만 |
| 요약을 원본처럼 수정 | 원본을 고치면 요약은 자동으로 낡음 처리 |
| 같은 내용을 JSON 열과 텍스트 열에 동시 저장 | 중복 (U-04) |

---

## 4. 데이터 구조 — 모델이 보는 형식

### 4.1 핸들 (모든 도구의 공통 주소) [설계 판단]

| 핸들 | 가리키는 것 | 예 |
|---|---|---|
| `d:` | 문서 | `d:DOC-REQ-HWCHECK` |
| `f:` | 필드 (문서/종류/필드) | `f:DOC-REQ-HWCHECK/D01/behavior` |
| `i:` | 항목 | `i:REQ-HW-001` |
| `w:` `r:` | 작업, 실행 | `w:WORK-2026-0001`, `r:RUN-0002` |
| `k:` | 문서 종류(카탈로그) | `k:D01` |

- 모델은 내용 대신 **핸들을 주고받습니다.** 한 번 본 대상은 이후 핸들로만 언급합니다.
- 내부 `change_log.entity_id`(`DOC|D01|behavior`)와 1:1로 바뀝니다.

### 4.2 공개 단계 (L0 / L1 / L2)

| 단계 | 내용 | 크기 감 | 어디서 |
|---|---|---|---|
| L0 | 핸들·상태·제목·`dg`·`tok` 한 줄 | 15~40 tok | 목록, ctx |
| L1 | L0 + 최신 요약(`sum=`) | ~60~100 tok | ctx(요약이 최신일 때), get level=1 |
| L2 | 필드 전문 | `tok=` 값만큼 | get level=2, **f: 핸들만** |

- `d:` 핸들로는 전문을 주지 않습니다. 문서 하나를 통째로 싣는 경로를 없애는 것이 목적입니다 [설계 판단].

### 4.3 응답 형식 [설계 판단]

```text
#ctx w:WORK-2026-0001 docs=4 items=40 as_of=c:49 tok=1514/1500 omitted=13   ← 첫 줄: 커서·예산·생략 수
W WORK-2026-0001 active owner=u-jjm "서버 점검 자동화" handoff=HO-0001:pending
> 디스크 임계값 검토                                                           ← 다음 행동
! r:RUN-0002 load_test NOT_RUN                                                  ← 차단 요인
! F1 error FIELD_MISSING document:DOC-D01-00 "필수 필드 누락"
D DOC-D01-00 D01 draft "D01 문서 0" missing=owners                              ← 문서 L0
I requirement×40 REQ-HW-001…REQ-HW-040                                          ← 항목은 종류별 개수
f:DOC-A04-01/A04/details dg=d14b54c09119fef6 tok=342 sum=none                   ← 필드 L1(요약 없음)
… omitted → get <handle> / since <cursor>
```

| 규칙 | 이유 |
|---|---|
| 한 줄에 하나, 줄머리 기호(`W > ! D I f:`)로 종류 구분 | JSON 키를 행마다 반복하지 않음. [측정] 같은 40개 항목에서 JSON 대비 71% (S2) |
| 공백 있는 값만 따옴표, null·기본값은 생략 | 빈 칸을 보내지 않음 |
| `dg`는 sha256 앞 16자리 | 프로젝트 안 비교에 충분, 전체는 DB에 |
| 첫 줄에 `as_of=c:N` | 다음 호출에서 `since`·`known`에 그대로 씀 |
| MCP 결과는 **text 한 블록만** (`structuredContent` 안 씀) | MCP 규격상 구조화 결과를 쓰면 같은 JSON을 text로도 보내도록 권고됨 — Tools "Structured Content": "a tool that returns structured content SHOULD also return the serialized JSON in a TextContent block." 클라이언트가 둘 다 모델에 넣는지는 **확인하지 못했으므로** 한쪽만 보냄 |

---

## 5. 기능 목록 (docctl)

### 5.1 노출 범위

| 구분 | 기능 | 누가 호출 |
|---|---|---|
| **LLM 노출 (MCP 6개)** | ctx, get, find, trace, since, put | 에이전트 |
| 스크립트 전용 | load(카탈로그), applicability(규칙 평가), check(gate), render, run ingest(CI), summarize(배치), export | 사람·CI·스케줄 |
| **노출 금지** | approve, 카탈로그 변경, legal_hold 해제, 삭제 | 사람만 (DB 트리거로도 차단: DEC-08) |

노출 금지 기능은 도구 정의 자체를 만들지 않습니다. AI 승인은 어차피 트리거가 막으므로(`approval_human_only`) 도구를 두면 정의 비용(T1)과 실패 비용(T4)만 생깁니다 [설계 판단].

### 5.2 LLM 도구 명세

| 도구 | 입력 | 출력 | 기본 예산 | 토큰 장치 |
|---|---|---|---:|---|
| `docdb_ctx` | work_id, budget | 작업 카드·다음 행동·차단 요인·관련 문서 L0·항목 개수·필드 L1 | 1,500 | 우선순위 채우기(7.1), 읽은 대상 `context_read` 자동 기록 |
| `docdb_get` | handles[], level, known{h:dg}, budget | 핸들별 L0/L1/L2 | 2,000 | `known`이 같으면 `UNCHANGED`, `d:`는 전문 없음 |
| `docdb_find` | query, limit | 핸들 + 발췌 | 800 | 3글자 이상 FTS(bm25 정렬), 미만 LIKE |
| `docdb_trace` | handle, rel[], depth | `a -rel-> b` 간선 | 800 | ID만, 내용 없음 |
| `docdb_since` | cursor, work_id | 바뀐 엔터티별 1줄 + diff 최대 6줄 | 1,200 | 같은 엔터티의 여러 변경은 한 줄로(`x3`) |
| `docdb_put` | patches[{h, op, text, expect, anchor, state, why}], reason | `APPLIED/NOTHING_APPLIED` + 핸들별 `OK dg tok` 또는 `ERR 코드` | — | 본문 되돌려주지 않음, `replace_section`·`append`, 전부 성공 아니면 전부 취소 |

**put 오류 코드 (짧게, 고칠 방법 포함):** `CONFLICT now=<dg>`(그 사이 바뀜 → since 후 재시도) · `ANCHOR_NOT_FOUND` · `UNSUPPORTED_HANDLE` · `BAD_OP` · `DB <제약 메시지>`(카탈로그에 없는 필드, 사유 없는 tbd 등). `expect`는 읽을 때 받은 `dg`이며, 다르면 쓰지 않습니다. change_log 분기 금지(UNIQUE prev)와 같은 문제를 쓰기 전에 막습니다.

---

## 6. 호출 방식

### 6.1 표준 순서

| 단계 | 호출 | 받는 것 | 다음 판단 |
|---|---|---|---|
| ① 시작·재개 | `docdb_ctx(w)` | 카드·차단 요인·관련 핸들 목록 | 무엇을 더 볼지 |
| ② 좁히기 | `docdb_get([...], level=1)` / `docdb_find` / `docdb_trace` | 요약·발췌·간선 | 고칠 필드 결정 |
| ③ 상세 | `docdb_get([f:...], level=2)` | 고칠 필드 전문 + `dg` | 패치 작성 |
| ④ 쓰기 | `docdb_put([{h, op, text, expect: dg}])` | OK/ERR + 새 `dg` | 끝 (다시 읽지 않음) |
| ⑤ 다음 세션·인계 | `docdb_since(c:N)` | 그 사이 바뀐 것만 | ①로 |

- 인계(`handoff.payload`)에 마지막 커서 `as_of`를 넣으면, 받는 쪽은 ①을 생략하고 ⑤부터 할 수 있습니다 [설계 판단].

### 6.2 하지 말 것 [설계 판단]

| 패턴 | 대신 |
|---|---|
| 문서 전체를 읽고 필드 하나에 답하기 | `get f:... level=2` |
| 쓰고 나서 확인하려고 다시 읽기 | put 응답의 `OK dg`로 충분 |
| 같은 필드를 같은 작업에서 다시 전문으로 읽기 | `known={h: dg}` |
| 목록 두 개를 받아 모델이 맞춰 보기 | `trace` 또는 뷰(v_rtm 등) |
| 범용 `query(sql)` 도구 | 모델이 스키마를 알아야 함. [측정] `docdb_schema.sql`만 31,511자 / 약 9,950 tok |
| 테이블마다 도구 1개 | 도구 정의가 매 요청 고정비(T1) |

### 6.3 MCP 연결 설정

| 클라이언트 | 설정 | 근거 |
|---|---|---|
| Claude Code | 기본값 유지(도구 검색 켜짐). 도구 정의는 필요할 때만 실림 | MCP "Scale with MCP tool search": "Only tool names and server instructions load at session start" |
| Claude Code | 서버 지침(`instructions`)은 "언제 쓰는지"를 앞에 | 같은 절 "For MCP server authors" |
| Claude Code | `alwaysLoad`는 켜지 않음 | 같은 문서 "Exempt a server from deferral": "each upfront tool consumes context" |
| Claude Code | 도구 결과는 1만 tok 이하로 (예산 기본값 800~2,000) | "MCP output limits and warnings" |
| Codex | `enabled_tools`로 6개만 허용, `tools.<tool>.output_token_limit`으로 결과 상한 | Codex MCP "Configure with config.toml › Other configuration options" |
| 둘 다 | 도구 목록·설명을 자주 바꾸지 않음 (`listChanged` 알림을 쓰지 않음) | Prompt caching "What invalidates the cache" |

> Claude Code 비용 문서는 "Prefer CLI tools when available … more context-efficient than MCP servers because they don't add any per-tool listing"라고 합니다("Reduce MCP server overhead"). 이번에는 MCP로 정했으므로, **같은 기능을 `docctl_llm.py` CLI로도 쓸 수 있게** 두었습니다. 셸이 있는 에이전트는 CLI를 써도 됩니다.

[측정] 도구 6개 정의 = 2,411자 / 약 769 tok (설명이 한국어라 크게 잡힘). `test_mcp.py`.

---

## 7. 조합 방식 — 스크립트가 묶어서 주는 규칙

### 7.1 `ctx` 우선순위 채우기 [설계 판단]

| 순위 | 내용 | 예산 밖이어도 넣나 |
|---|---|---|
| P0 | 작업 카드 + 다음 행동 | 넣음 |
| P1 | 실패·미실행 run | 넣음 |
| P2 | 미해결 finding(심각도순), 낡은 근거(`! stale`) | 예산 안에서 |
| P3 | 관련 문서 L0 + 누락 필수 필드(`missing=`) | 예산 안에서 |
| P4 | 항목 종류별 개수·범위(`I requirement×40 …`) | 예산 안에서 |
| P5 | 필드 L1 (요약이 최신이면 요약, 아니면 핸들·dg·tok만) | 예산 안에서 |
| P6 | 개별 항목 L0 | 예산 안에서 |
| — | 넘친 것 | `omitted=N` + 다음 호출 안내 |

- **관련 문서:** `document.work_id`·`origin_work_id`가 이 작업 + `relation(work → changes → document)` + 이 작업에서 이미 읽은 문서(`context_read`).
- **정렬은 항상 같게**(순위 → ID). 같은 DB 상태면 같은 출력이 나와 재현할 수 있습니다(docdb-design.md S-13).

### 7.2 묶기 규칙 [설계 판단]

| 규칙 | 내용 |
|---|---|
| 중복 제거 | 한 응답에서 같은 대상은 한 번. 두 번째부터는 핸들만 |
| 변경 묶기 | `since`는 엔터티별로 묶어 마지막 값 기준 diff 최대 6줄, 나머지는 `… +N lines` |
| 요약 신선도 | `entity_summary.source_digest ≠ 현재 digest`이면 요약을 쓰지 않음 |
| 컨텍스트 잠금 | L1 이상을 실은 문서·항목은 `context_read`에 자동 기록 → 이후 바뀌면 다음 `ctx`에 `! stale` (V05, docdb-design.md 7.3) |
| 계산은 SQL | RTM·누락 필드·재개 카드는 기존 뷰(`v_rtm`, `v_missing_required_fields`, `v_work_resume`)를 그대로 씀 |

---

## 8. 검증 결과

`test_token.py`는 합성 데이터(문서 12개 중 작업 관련 4개, 필드 49개, 항목 80개, 실행·지적·인계)를 만들어 시나리오를 실행합니다. 카탈로그 원본이 패키지에 없어서 **검증용 미니 카탈로그(45종)**로 돌렸습니다.

### 8.1 동작 — 28건 전부 통과 (SQLite 3.45.1, Python 3.13, mcp 2.2.0 · 1.30.0)

| 구분 | 시나리오 | 결과 |
|---|---|---|
| ctx | 예산 준수 · 차단 요인 포함 · 관련 문서만 · 누락 필드 표시 · 항목 개수 요약 · context_read 자동 기록 | 통과 |
| get | 바뀌지 않은 값 `UNCHANGED` · 요약 최신/낡음 구분 · `v_summary_stale` | 통과 |
| put | 절 교체(다른 절 보존) · 응답에 본문 없음 · 낡은 dg → `CONFLICT` 미적용 · 배치 중 1건 실패 → 전부 취소 · change_log 체인 트리거 통과 | 통과 |
| since | 바뀐 3개만 반환 | 통과 |
| find | 3글자 이상 FTS · 2글자 LIKE 대체 · trigram 2글자 불가 확인 · 수정 즉시 색인 반영 · VACUUM 후 핸들 유지 · FTS 무결성 | 통과 |
| 기타 | trace ID만 · FK 무결성 · call_log 집계 | 통과 |
| MCP | 도구 6개 노출, 결과 text 1블록, structuredContent 없음 — mcp 2.2.0과 1.30.0 둘 다 (`test_mcp.py`) | 통과 |
| 호환성 | 기존 `test_scenarios.py`를 확장 없이/확장 적용 후 각각 실행 → **결과 동일**(32/33). 실패 1건은 "194종 집계"로, 미니 카탈로그가 45종이라 생긴 것 | 영향 없음 |

### 8.2 효과 [측정 — 글자 수와 tok_est(보정 전 추정)]

| 상황 | 기존 방식 | 이 명세 | 비율 |
|---|---|---|---:|
| 작업 시작 (S1) | 관련 행 원시 JSON 덤프 49,479자 / 20,031 tok | `ctx` 3,519자 / 1,467 tok | 7.3% |
| 재개 (S6) | 관련 문서 필드 전체 재독 11,796자 / 8,579 tok | `since` 291자 / 116 tok | 1.4% |
| 같은 필드 재조회 (S3) | 전문 413 tok | `UNCHANGED` 21 tok | 5.1% |
| 일부 수정 — 모델 출력 (S4) | 필드 전체 재작성 318 tok | `replace_section` 패치 58 tok | 18% |
| 항목 40개 목록 (S2) | JSON 2,062 tok | 한 줄 형식 1,466 tok | 71% |

### 8.3 한계

- **tok_est는 보정 전 휴리스틱**입니다(ASCII 3.5자 = 1 tok, 그 외 1자 = 1 tok, 과대추정 쪽). 절대값은 믿지 말고 비율만 보세요. 실제 값은 count_tokens API로 재야 합니다 — Token counting: "Token counting is **free to use**", "The token count is an **estimate**."
- 합성 데이터는 문장 10개를 섞은 것이라 실제 문서보다 반복이 많습니다.
- 실제 카탈로그(194종)로는 실행하지 않았습니다.
- 대규모 성능(수십만 행, LIKE 전체 탐색 비용)은 재지 않았습니다.
- Codex의 도구 지연 로딩 여부, 각 클라이언트가 structuredContent를 모델에 넣는지는 확인하지 못했습니다.

---

## 9. 다음 단계

| # | 할 일 | 비고 |
|---|---|---|
| N1 | 실제 카탈로그로 `test_token.py` 재실행 | `python3 docdb_tools/test_token.py out.db project-documentation-catalogue.yaml docdb_schema.sql` |
| N2 | `tok_est` 계수 보정 | 실제 필드 100개를 count_tokens API로 재서 `ASCII_PER_TOK`, `NONASCII_PER_TOK` 조정 |
| N3 | 요약 배치(`summarize`) 구현 | `v_summary_stale` → 값싼 모델 → `entity_summary` |
| N4 | `call_log`로 1~2주 측정 | 도구별 합계·최대값, 생략률, 같은 대상 재독률 |
| N5 | 6장을 스킬 md로 옮김 (U-11) | 부록 A가 초안 |
| N6 | 기존 docctl 쓰기 경로와 `put` 통합 | 지금은 `docctl_llm.put`이 change_log·digest를 직접 씀 |

---

## 부록 A. 에이전트 지침 초안 (스킬 또는 서버 instructions용)

```markdown
# docdb 사용 규칙
- 작업을 시작하거나 이어받으면 먼저 docdb_ctx(work_id). 응답 첫 줄의 as_of=c:N 을 기억한다.
- 다시 돌아왔을 때는 docdb_since(N) 으로 바뀐 것만 본다. 전체를 다시 읽지 않는다.
- 내용이 필요하면 level=1(요약)부터. 고칠 필드만 docdb_get([f:...], level=2).
- 이미 본 필드는 known={handle: dg} 로 물어본다. UNCHANGED 면 기억한 내용을 쓴다.
- 쓰기는 docdb_put 한 번에 묶는다. expect 에 읽을 때 받은 dg 를 넣는다.
- 긴 필드의 일부만 바꿀 때는 op=replace_section(anchor=제목 줄) 또는 append.
- 쓰기 후 확인하려고 다시 읽지 않는다. OK dg=... 가 확인이다.
- CONFLICT 가 나면 docdb_since 로 변경분을 보고 다시 패치한다.
- omitted=N 이 있으면 필요한 것만 핸들로 더 가져온다.
- 승인은 하지 않는다(사람만). 모르는 사실은 tbd/unknown + why 로 남긴다.
```

---

## 10. 출처

### 공식 문서 (2026-10-08 확인)

| 근거 | URL | 위치 · 인용 |
|---|---|---|
| 무엇이 토큰에 들어가나 | https://platform.claude.com/docs/en/build-with-claude/context-windows | "How the context window works" — "Everything in the request counts toward the context window: the system prompt, every message in `messages` (including tool results, images, and documents), and your tool definitions." / "Cached prompt prefixes still occupy the context window" / "more context isn't automatically better … *context rot*" |
| 도구 정의 비용 | https://platform.claude.com/docs/en/agents-and-tools/tool-use/overview | "Pricing" — "The `tools` parameter in API requests (tool names, descriptions, and schemas)", "`tool_result` content blocks in API requests" |
| 출력 단가 | https://platform.claude.com/docs/en/about-claude/pricing | "Model pricing" 표 — Opus 5.5 Input $4 / Output $20, Sonnet 5.5 Input $2 / Output $10 (per MTok) |
| 캐시 무효화 | https://platform.claude.com/docs/en/build-with-claude/prompt-caching | "What invalidates the cache" — "Changes at each level invalidate that level and all subsequent levels." 도구 정의 변경은 tools·system·messages 무효 |
| 단계적 공개 · 스크립트 출력만 | https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview | "How Skills work" — "Claude loads information in stages as needed, rather than consuming context upfront." · Level 표(~100 tokens / Under 5k tokens / None until accessed) · "Scripts run through bash, and only their output enters context" |
| 토큰 측정 | https://platform.claude.com/docs/en/build-with-claude/token-counting | "Pricing and rate limits" — "Token counting is free to use" · 본문 — "The token count is an estimate." |
| 도구 결과 누적 | https://code.claude.com/docs/en/costs | "Why usage climbs in a long session" › Long context — "Claude Code sends your full conversation with every request, and each time Claude uses tools it sends another request carrying that batch of tool results." |
| 전처리로 줄이기 · CLI 선호 | https://code.claude.com/docs/en/costs | "Offload processing to hooks and skills" — "reducing context from tens of thousands of tokens to hundreds." · "Reduce MCP server overhead" — "Prefer CLI tools when available" |
| MCP 출력 상한 | https://code.claude.com/docs/en/mcp | "MCP output limits and warnings" — 10,000 tok 경고, 기본 25,000 tok 상한, `MAX_MCP_OUTPUT_TOKENS` |
| MCP 도구 지연 로딩 · 설명 길이 | https://code.claude.com/docs/en/mcp | "Scale with MCP tool search" — "Only tool names and server instructions load at session start" · "For MCP server authors" — 2,048자에서 자름 · "Exempt a server from deferral" |
| MCP 결과 형식 | https://modelcontextprotocol.io/specification/2025-06-18/server/tools | "Tool Result › Structured Content" — "SHOULD also return the serialized JSON in a TextContent block." · "Resource Links" |
| Codex MCP 설정 | https://learn.chatgpt.com/docs/extend/mcp?surface=cli | "Configure with config.toml › Other configuration options" — `enabled_tools`, `disabled_tools`, `tools.<tool>.output_token_limit` |
| FTS5 external content | https://www.sqlite.org/fts5.html | §4.4 "External Content and Contentless Tables", §4.4.3 "External Content Tables" |
| FTS5 trigram 3글자 제한 | https://www.sqlite.org/fts5.html | §4.3.4 "The Trigram Tokenizer" › Notes — "Substrings consisting of fewer than 3 unicode characters do not match any rows" |
| FTS5 snippet · bm25 | https://www.sqlite.org/fts5.html | §5.1.3 "The snippet() function" (최대 64 토큰) · §5.1.1 "The bm25() function" |
| VACUUM rowid 변경 | https://www.sqlite.org/lang_vacuum.html | §3 "How VACUUM works" — "may change the ROWIDs of entries in any tables that do not have an explicit INTEGER PRIMARY KEY." |

### 입력 자료

| 표기 | 파일 | 인용 위치 |
|---|---|---|
| docdb-design.md | 패키지 내 | L51 S-05(요약은 파생물) · L59 S-13(재현) · DEC-01·02·06·08·09 · 7.3 AI 협업 조회 |

### 재현 방법

```bash
python3 docdb_tools/test_token.py out.db                       # 미니 카탈로그로 28건
python3 docdb_tools/test_token.py out.db project-documentation-catalogue.yaml docdb_schema.sql   # 실제 카탈로그
pip install mcp && python3 docdb_tools/test_mcp.py out.db      # MCP 래퍼 (mcp 1.30.0·2.2.0에서 확인, Python 3.10 이상)
# 실제 사용
python3 docdb_tools/docctl_llm.py --db docdb.sqlite --actor ai-codex ctx WORK-2026-0001 --budget 1500
DOCDB_PATH=docdb.sqlite DOCDB_ACTOR=ai-claude python3 docdb_tools/docdb_mcp.py
```
