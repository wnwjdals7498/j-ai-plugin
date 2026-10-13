# 02. 작업 목록

모든 작업은 [00-discipline.md](00-discipline.md)의 절차(5절)와 [01-contracts.md](01-contracts.md)를 따른다. 작업 하나는 커밋 하나다. 표의 "수용 테스트"는 **행마다 테스트 함수 하나 이상**으로 만든다(함수 이름 규칙: 00-discipline 5절 3번).

공통 검증 명령(모든 Sonnet 작업의 마지막 단계):

```bash
cd /home/wnwjdals7498/j-ai-plugin/docdb && .venv/bin/python -m pytest
```

## 단계와 순서

| 단계 | 작업 | 실행자 | 선행 |
|---|---|---|---|
| 0 실측 | T00a Claude Code MCP 실측, T00b Codex MCP 실측, T00c Windows·휠 없는 플랫폼 실측 | 오케스트레이터 | — |
| 1 기반 | T01 골격 → T02 오류·시간·텍스트 → T03 경로 → T04 DB 연결 → T05 마이그레이션 실행기 → T06 마이그레이션 0003~0008 → T07 이력 → T08 ID·행위자 → T09 정책 → T10 카탈로그 → T11 백업·CLI 골격·init | Sonnet | 앞 작업 |
| 2 이식 | T12 LLM 읽기·쓰기 이식 → T13 원본 시나리오·검수 이식 | Sonnet | T11 |
| 3 관계 | T14 항목 쓰기 → T15 관계 제안·확정 → T16 규칙 추출 → T17 ctx·trace 확장 | Sonnet | T13 |
| 4 작업 흐름 | T18 작업·인계 → T19 프로필·적용 판정 | Sonnet | T17 |
| 5 증거 | T20 실행 결과 적재 | Sonnet | T19 |
| 6 import | T21 분할·scan → T22 next·label·report | Sonnet | T20 |
| 7 요약 | T23 요약 | Sonnet | T22 |
| 8 진입점 | T24 부트스트랩 → T25 MCP 서버 → T26 플러그인 패키징 | Sonnet(T26은 T00b 결과 필요) | T23, T00b |
| 9 검증 | T27 매트릭스, T28 GitHub Actions, T29 실사용 확인, T30 문서 반영 | T27 Sonnet, 나머지 오케스트레이터 | T26 |
| 보류 | T31 gate·기준선·승인, T32 렌더, T33 내보내기, T34 보존 정리 | — | 해당 B·C 결정 |

병렬로 돌릴 수 있는 쌍(대상 파일이 겹치지 않고 선행이 충족됨): T09∥T10, T21∥T23, T24∥T27. 실행자별 분배는 [handoff/README.md](handoff/README.md).

---

## 0단계 — 실측 (오케스트레이터)

### T00a Claude Code MCP 실측 (S1, S3)

- **목적**: Claude Code가 플러그인 MCP 서버를 어떻게 실행하는지 확인한다.
- **방법**: 작업 공간에 임시 플러그인을 만든다. `.claude-plugin/plugin.json`의 `mcpServers.spike`는 개발 venv Python으로 `spike_server.py`를 실행하고, `env`에 `{"P_ROOT": "${CLAUDE_PLUGIN_ROOT}", "P_PROJ": "${CLAUDE_PROJECT_DIR}", "P_DATA": "${CLAUDE_PLUGIN_DATA}"}`를 둔다. `spike_server.py`는 `mcp` SDK로 도구 `ping` 하나를 노출하고, 시작 시 `os.getcwd()`, 위 환경 변수, 받은 첫 요청의 메서드·프로토콜 버전을 stderr와 로그 파일에 쓴다. `claude --plugin-dir <임시 플러그인> -p "ping 도구를 한 번 호출해"`로 실행한다.
- **기록할 것**: 작업 폴더, 세 변수의 치환 결과, 첫 요청 메서드(`initialize` 또는 `server/discover`)와 버전, 도구 호출 성공 여부.
- **반영**: [../08-verification.md](../08-verification.md) 3절 S1·S3 결과, 필요하면 C-04 `find_db` 시작 폴더와 C-26 `env` 수정.
- **완료 조건**: 로그에 다섯 값이 모두 있다.

### T00b Codex MCP 실측 (S1, S2)

- **방법**: 같은 `spike_server.py`를 `.codex-plugin/plugin.json` + `codex.mcp.json`으로 등록한다. 세 가지 변형을 시험한다: `args`에 ① `./spike_server.py`(상대 경로) ② `${PLUGIN_ROOT}/spike_server.py` ③ 절대 경로. `env`에 `{"P_ROOT": "${PLUGIN_ROOT}"}`. 로컬 마켓플레이스로 설치하고 `codex exec "ping 도구를 한 번 호출해"`로 실행한다.
- **기록할 것**: 변형별 실행 성공 여부, 작업 폴더, 환경 변수 치환 결과, 첫 요청 메서드·버전.
- **반영**: [01-contracts.md](01-contracts.md) C-26의 Codex 절을 채운다(경로 방식, `command`, `env`). Windows 명령은 T00c 결과로.
- **완료 조건**: C-26 Codex 절이 채워져 T26을 시작할 수 있다.

### T00c Windows·휠 없는 플랫폼 실측 (S4, S5)

- **전제**: GitHub에 push해야 한다. **사용자 승인을 받은 경우에만** 진행한다. 승인이 없으면 이 작업을 "미검증"으로 기록하고 건너뛴다.
- **방법**: 임시 브랜치에 GitHub Actions 작업을 둔다. ① Windows: `py -3 -m venv`, `pip install pysqlite3==0.6.1 mcp==2.3.0`, SDK 클라이언트로 `spike_server.py`와 한글 문자열을 주고받기. ② Intel macOS와 Linux arm64 러너(실행 시점에 GitHub 문서에서 러너 이름 확인): `pip install pysqlite3==0.6.1` 후 `check_capabilities`와 같은 세 검사.
- **반영**: 08 문서 S4·S5 결과, README 지원 플랫폼(O30 기본값).

---

## 1단계 — 기반

### T01 골격과 개발 환경

- **선행**: 없음. **계약**: C-01, C-02, C-27. **결정**: D3, D14.
- **대상 파일**: `docdb/src/docdb/__init__.py`, `docdb/pytest.ini`, `docdb/constraints.txt`, `docdb/constraints-dev.txt`, `docdb/tests/conftest.py`(이 작업에서는 `frozen_now`, `plugin_root`만), `docdb/tests/unit/test_t01_skeleton.py`
- **규칙**:
  - 개발 venv: `uv venv docdb/.venv --python /usr/bin/python3.12`.
  - `constraints.txt`: 빈 venv에 `pysqlite3==0.6.1 mcp==2.3.0`을 설치하고 `uv pip freeze --python <venv python>` 결과에서 `pip`, `setuptools`, `wheel` 줄을 뺀 것.
  - `constraints-dev.txt`: 첫 줄 `-c constraints.txt`, 다음 줄부터 `pytest`와 그 하위 의존성 고정 버전.
  - 설치: `uv pip install --python docdb/.venv/bin/python -c docdb/constraints-dev.txt pytest pysqlite3 mcp`.

| # | 상황 | 기대 |
|---|---|---|
| 1 | `import docdb` | `docdb.__version__ == "0.1.0"` |
| 2 | `constraints.txt` 읽기 | `pysqlite3==0.6.1`과 `mcp==2.3.0` 줄이 있다 |
| 3 | `constraints-dev.txt` 읽기 | 첫 줄이 `-c constraints.txt`, `pytest==` 줄이 있다 |
| 4 | `from pysqlite3 import dbapi2` | `sqlite_version_info >= (3, 34, 0)` |
| 5 | `plugin_root` 픽스처 | `docdb/` 폴더를 가리키고 그 안에 `src/docdb/__init__.py`가 있다 |

### T02 오류·시간·텍스트 규칙

- **선행**: T01. **계약**: C-05, C-06, C-07. **결정**: D19.
- **대상 파일**: `src/docdb/errors.py`, `src/docdb/clock.py`, `src/docdb/textnorm.py`, `tests/conftest.py`(`frozen_now` 완성), `tests/unit/test_t02_text.py`

| # | 입력 | 기대 |
|---|---|---|
| 1 | `DocdbError("NO_DB", "x")` | `exit_code == 3`, `format_error(...) == "ERROR NO_DB x"` |
| 2 | C-05 표의 모든 코드 | `ERROR_CODES[code]`가 표의 종료 코드와 같다(테스트에 표를 그대로 옮긴 dict로 비교) |
| 3 | `DocdbError("NOPE")` | `ValueError` |
| 4 | `normalize_text("a\r\nb\rc")` | `"a\nb\nc"` |
| 5 | `normalize_text(unicodedata.normalize("NFD", "한글"))` | `"한글"`(NFC) |
| 6 | `normalize_text(None)`, `digest16(None)` | `None`, `digest16("")`와 같음 |
| 7 | `digest16("a\r\nb")` | `digest16("a\nb")`와 같음, 16자리 소문자 16진수 |
| 8 | `canonical_json({"b": 1, "a": "x\r\n"})` | `'{"a":"x\\n","b":1}'` |
| 9 | `tok_est("")`, `tok_est("abcdefg")`, `tok_est("가나")`, `tok_est("ab가")` | `0`, `2`, `2`, `2` |
| 10 | `q("a b")`, `q("")`, `q("x\ny")`, `q("ab")` | `'"a b"'`, `'""'`, `'"x ⏎ y"'`, `'ab'` |
| 11 | `norm_key("Behavior_Spec (요구)")` | `"behaviorspec요구"` |
| 12 | `clock.now_iso()` | 정규식 `^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00$` |
| 13 | `frozen_now` 적용 | `now_iso() == "2026-10-13T00:00:00+00:00"`, `utc_year() == 2026` |
| 14 | 하위 프로세스에서 `import docdb.errors, docdb.clock, docdb.textnorm` 후 `sys.modules` 검사 | `pysqlite3`, `mcp`, `docdb.db`가 없다 |

### T03 경로

- **선행**: T02. **계약**: C-03, C-04. **결정**: D5, D13.
- **대상 파일**: `src/docdb/paths.py`, `tests/unit/test_t03_paths.py`
- **규칙**: `sys.platform`, 환경 변수, `Path.home`을 monkeypatch해 OS별 분기를 모두 시험한다.

| # | 상황 | 기대 |
|---|---|---|
| 1 | `DOCDB_HOME=/x` | `data_dir() == Path("/x")` |
| 2 | linux, `XDG_DATA_HOME=/xdg` | `Path("/xdg/docdb")` |
| 3 | linux, XDG 없음, home=`/h` | `Path("/h/.local/share/docdb")` |
| 4 | darwin, home=`/h` | `Path("/h/Library/Application Support/docdb")` |
| 5 | win32, `LOCALAPPDATA=/L` | `Path("/L/docdb")`; `LOCALAPPDATA` 없으면 `/h/AppData/Local/docdb` |
| 6 | `venv_python()` win32 / linux | `.../venv/Scripts/python.exe` / `.../venv/bin/python` |
| 7 | `DOCDB_PATH`가 있는 파일 / 없는 파일 | 그 경로 / `NO_DB` |
| 8 | `tmp/a/b/c`에서 시작, `tmp/a/.docdb/docdb.sqlite` 존재 | 그 파일 |
| 9 | `start=None`, `DOCDB_START_DIR=tmp/a/b` | 위와 같은 파일 |
| 10 | 어디에도 없음 | `DocdbError` 코드 `NO_DB`, 종료 코드 3 |
| 11 | `project_root(tmp/a/.docdb/docdb.sqlite)` | `tmp/a` |
| 12 | 하위 프로세스 `import docdb.paths` | `pysqlite3`, `docdb.db`가 `sys.modules`에 없다 |

### T04 DB 연결과 트랜잭션

- **선행**: T03. **계약**: C-01(import 규칙), C-08. **결정**: D20.
- **대상 파일**: `src/docdb/db.py`(`open_project_db`는 T05에서 추가하므로 이 작업에서는 만들지 않는다), `tests/unit/test_t04_db.py`, `tests/unit/test_import_guard.py`

| # | 상황 | 기대 |
|---|---|---|
| 1 | `connect(없는 경로)` | `NO_DB` |
| 2 | `connect(경로, create=True)` | `foreign_keys=1`, `busy_timeout=5000`, `journal_mode=wal` |
| 3 | 연결 직후 | `con.in_transaction is False` |
| 4 | `check_capabilities(con)` | 예외 없음 |
| 5 | `sqlite_version`을 `(3, 26, 0)`으로 monkeypatch 후 `check_capabilities` | `SQLITE_CAPABILITY` |
| 6 | `write_tx` 안에서 INSERT 후 정상 종료 | 다른 연결에서 행이 보인다 |
| 7 | `write_tx` 안에서 INSERT 후 예외 | 행 없음, 예외가 그대로 전달 |
| 8 | 바깥 `write_tx`의 INSERT 성공, 안쪽 `write_tx`에서 INSERT 후 예외(바깥에서 잡음) | 바깥 행만 남는다 |
| 9 | 스레드 A가 `write_tx`를 1초 잡고 있는 동안 스레드 B가 다른 연결로 `write_tx` | B가 기다렸다가 성공, `database is locked` 없음 |
| 10 | FK 위반 INSERT | `docdb.db.IntegrityError`로 잡힌다 |
| 11 | (`test_import_guard.py`) `src/` 아래 `.py` 전체 검사 | `pysqlite3` 문자열은 `db.py`에만, `mcp` import는 `mcp_server.py`에만, 표준 `sqlite3` import 줄(정규식은 표 아래)은 어디에도 없음 |


#11의 표준 `sqlite3` import 검사 정규식:

```python
STD_SQLITE_IMPORT = r"^\s*(import|from)\s+sqlite3\b"
```

### T05 마이그레이션 실행기

- **선행**: T04. **계약**: C-09(마이그레이션 파일·`migrate.py`), C-08(`open_project_db`), C-27(`schema_con`). **결정**: D21.
- **대상 파일**: `src/docdb/migrate.py`, `src/docdb/schema/0001_base.sql`, `src/docdb/schema/0002_token_ext.sql`, `src/docdb/db.py`(`open_project_db` 추가), `tests/conftest.py`(`schema_con` 추가), `tests/unit/test_t05_migrate.py`
- **규칙**: 0001·0002는 `cp`로 복사한다(편집기로 다시 쓰지 않는다). `schema_con` 픽스처 = `tmp_path/"s.sqlite"`에 `connect(create=True)` 후 `apply_pending`.

| # | 상황 | 기대 |
|---|---|---|
| 1 | `schema_files()` 번호 | `[1, 2, …, n]` 빈틈 없음 |
| 2 | 0001·0002 sha256 | 원본 `docdb_schema.sql`·`docdb_schema_token_ext.sql`과 같다 |
| 3 | 새 DB에 `apply_pending` | 반환 `[1, …, latest]`, `user_version == latest_version()`, 0001의 `CREATE TABLE` 이름(정규식으로 추출, 36개)이 모두 있다 |
| 4 | 같은 DB에 다시 `apply_pending` | `[]` |
| 5 | `PRAGMA user_version = 1` 후 `ensure_current` | `MIGRATION_REQUIRED`, detail `1->latest` |
| 6 | `PRAGMA user_version = 99` 후 `ensure_current` | `DB_NEWER` |
| 7 | 임시 폴더에 정상 문장 + 오류 문장이 든 `000N_bad.sql`을 넣고 `schema_files`를 그 폴더로 monkeypatch | `DocdbError` 코드 `DB`, `user_version` 그대로, 정상 문장의 테이블도 없음 |
| 8 | `open_project_db(db_path=…)` 최신 DB / 오래된 DB | 연결 반환 / `MIGRATION_REQUIRED` |

### T06 마이그레이션 0003~0008

- **선행**: T05. **계약**: C-09 표, C-10 표(트리거 형식). **결정**: D6, D7, D10, D17, D18. **참조**: [../11-schema.md](../11-schema.md) 7.1~7.9, 7.7 시제품 SQL.
- **대상 파일**: `src/docdb/schema/0003_relation_status.sql` ~ `0008_call_log_session.sql`, `tests/helpers.py`(`seed_min` 추가), `tests/unit/test_t06_schema.py`
- **규칙**:
  - 0003·0004는 11-schema 7.7의 시제품 SQL을 출발점으로 쓴다(그 SQL은 SQLite 3.53.1에서 10/10 검증됨). 0003에 `DROP VIEW v_rtm;`과 `rel.status = 'confirmed'` 조건을 넣은 `CREATE VIEW v_rtm`을 더한다.
  - 0007 트리거는 C-10 정규식과 같은 결과를 내는 `GLOB`·`length` 조건으로 쓴다. 트리거 이름 `<table>_id_format`, 메시지 `<column> format invalid`.
  - `tests/helpers.py`의 `seed_min(con)`: `cat_version`, `cat_group`(`D`, `L`), `cat_kind`(D01 living narrative, L02 living narrative), `cat_kind_field`(D01 `behavior`, L02 `test_ids`), `cat_item_type`(`requirement` REQ- D01, `test_case` TC- L02), `cat_kind_link`(D01→L02 필수), `project`(`t`), `actor`(`u-tester` human, `ai-claude` ai)를 SQL로 넣는다. 이후 작업의 단위 테스트가 재사용한다.

| # | 상황 | 기대 |
|---|---|---|
| 1 | `latest_version()` | 8 |
| 2 | `relation` 컬럼 | `status`(기본 `confirmed`), `source`, `proposed_by`, `decided_by`, `decided_at` |
| 3 | AI가 `source='agent', status='confirmed'` 삽입 | 거부 |
| 4 | AI가 `source='rule:R3', proposed_by='ai-claude'` 삽입 | 거부 |
| 5 | AI가 `source='agent', status='proposed'` 삽입 | 허용 |
| 6 | 위 관계를 `decided_by='ai-claude'`로 확정 | 거부 |
| 7 | `decided_by='u-tester'`로 확정 | 허용 |
| 8 | 확정된 관계를 `rejected`로 | 거부 |
| 9 | `source='rule:R2', proposed_by NULL, status='confirmed'` | 허용 |
| 10 | 같은 (출발, 종류, 도착) 재삽입 | UNIQUE 거부 |
| 11 | `v_missing_required_links` | 연결 없음 1행 → 제안만 있음 1행 → 확정 후 0행 |
| 12 | `v_rtm` | `proposed` 관계는 `verified_by`에 안 나오고 `confirmed`만 나온다 |
| 13 | 항목 삽입 → FTS로 찾기 → 삭제 → 다시 찾기 | 1건 → 0건, `INSERT INTO fts_text(fts_text) VALUES('integrity-check')` 통과 |
| 14 | `import_segment`에 `status='done'` | CHECK 거부 |
| 15 | `cat_kind_field.extract_rel`에 `'{bad'` / `cat_item_type.defined_by_field` 존재 | 거부 / 컬럼 있음 |
| 16 | `run_id` `RUN-0001` / `RUN-2026-0001`, `doc_id` `doc-x` / `DOC-A--B` / 65자, `handoff_id` `HO-2026-0001`, `approval_id` `APR-2026-0001`, `artifact_id` `ART-2026-0001` | 거부 / 허용, 거부 / 거부 / 거부, 허용, 허용, 허용 |
| 17 | `call_log.session_ref` | 컬럼 있음 |

### T07 이력

- **선행**: T06. **계약**: C-15. **결정**: D19, 원 설계 DEC-06.
- **대상 파일**: `src/docdb/history.py`, `tests/unit/test_t07_history.py`

| # | 상황 | 기대 |
|---|---|---|
| 1 | 트랜잭션 밖에서 `append_change` | `DocdbError` 코드 `DB` |
| 2 | 첫 변경 | `seq=1`, `prev_change_id IS NULL`, `row_digest`가 `^sha256:[0-9a-f]{64}$` |
| 3 | 같은 대상 두 번째 변경 | `seq=2`, `prev_change_id`=첫 행, `prev_digest`=첫 행 `row_digest` |
| 4 | 다른 대상 | 각자 `seq=1`부터 |
| 5 | `row_digest(...)` 직접 계산 | 저장된 값과 같음(C-15 공식) |
| 6 | `after={"value_text": "a\r\nb"}` | `after_json == canonical_json({"value_text": "a\nb"})` |
| 7 | `work_id`, `reason` 인자 | 저장됨 |
| 8 | 정상 체인 `verify_chain` | `[]` |
| 9 | `change_log_no_update` 트리거를 지우고 첫 행 `after_json` 변조 | `verify_chain` 결과에 C-15 형식의 `seq=1 digest mismatch` 문자열(대상 이름 포함) |
| 10 | `RECORDED_TABLES` | C-15 목록과 정확히 같음 |

### T08 ID와 행위자

- **선행**: T07. **계약**: C-10, C-11. **결정**: D17, D18, D24.
- **대상 파일**: `src/docdb/ids.py`, `src/docdb/actors.py`, `tests/unit/test_t08_ids_actors.py`

| # | 상황 | 기대 |
|---|---|---|
| 1 | `validate("doc", …)`: `DOC-REQ-ORDERS` / `DOC-` / `doc-x` / `DOC-A--B` / 65자 | 통과 / `ID_FORMAT` ×4 |
| 2 | `validate("item", …)`: `REQ-HW-001` / `REQ` / `req-1` | 통과 / `ID_FORMAT` ×2 |
| 3 | 트랜잭션 밖 `next_id` | `DocdbError` 코드 `DB` |
| 4 | 빈 DB `next_id("run")`(frozen_now) | `RUN-2026-0001` |
| 5 | `RUN-2026-0007`, `RUN-2025-0100` 존재 | `RUN-2026-0008` |
| 6 | `RUN-2026-9999` 존재 | `RUN-2026-10000` |
| 7 | `next_id("work", year=2030)` | `WORK-2030-0001` |
| 8 | 두 스레드가 각각 `write_tx` 안에서 `next_id("run")` + INSERT | 서로 다른 ID |
| 9 | `change_log`에 `document`/`DOC-X` `delete` 행이 있을 때 `ensure_not_reused` | `ID_FORMAT` |
| 10 | `add_actor`: `u-jjm` human / `U-JJM` / `ai-x`를 human으로 | 성공 / `ID_FORMAT` / `ID_FORMAT` |
| 11 | `add_actor` 후 | `change_log`에 `actor` insert 1건 |
| 12 | `resolve_actor`: explicit 우선 / env만 / 둘 다 없음 / 없는 ID / `active=0` | explicit / env / `BAD_ARGS` / `NOT_FOUND` / `NOT_FOUND` |
| 13 | `require_human`: ai / human 비대화형 / human 대화형 | `NEEDS_HUMAN` / `NEEDS_HUMAN` / 통과 |
| 14 | `require_ai`: human | `BAD_ARGS` |

### T09 정책

- **선행**: T08. **계약**: C-16. **결정**: D8, D23.
- **대상 파일**: `src/docdb/policy.py`, `tests/unit/test_t09_policy.py`
- **규칙**: 테스트의 비밀 값은 반복 문자로 만든 가짜다(00-discipline 3절 12번).

| # | 입력 | 기대(`scan_secrets` 결과) |
|---|---|---|
| 1 | `"AKIA" + "A"*16` | `["aws_access_key"]` |
| 2 | `"ghp_" + "a"*36` | `["github_token"]` |
| 3 | `"sk-ant-" + "a"*20` | `["anthropic_key"]`(`openai_key` 아님) |
| 4 | `"sk-" + "a"*20` | `["openai_key"]` |
| 5 | `"xoxb-" + "1"*10` | `["slack_token"]` |
| 6 | `"123456789:" + "A"*35` | `["telegram_bot_token"]` |
| 7 | `"eyJ" + "a"*10 + "." + "b"*10 + "." + "c"*10` | `["jwt"]` |
| 8 | `"-----BEGIN RSA PRIVATE KEY-----"` | `["private_key"]` |
| 9 | `"password = " + "x"*8` | `["assignment"]` |
| 10 | `"password: 없음"`, `"sk-ant-"`, `"일반 문장"` | `[]` |
| 11 | `assert_no_secrets`에 #1 값 | `SECRET_DETECTED`, detail에 `aws_access_key`가 있고 비밀 값 문자열은 없다 |
| 12 | 사람·대화형 `allow_secret(handle, days=7)` | `PEX-2026-0001`, `expires_at`은 7일 뒤, 그 handle은 통과, 다른 handle은 거부, 이력 1건 |
| 13 | `frozen_now`를 8일 뒤로 바꿈 | 같은 handle 다시 거부 |
| 14 | AI가 `allow_secret` | `NEEDS_HUMAN` |
| 15 | `can_export`: `local_only` / `approved_providers` / `public` | `False` / `True` / `True` |

### T10 카탈로그

- **선행**: T08(T09와 대상 파일이 겹치지 않아 병렬 가능). **계약**: C-23. **결정**: D2, D11, D12, O5 기본값. **참조**: [../07-catalogue.md](../07-catalogue.md), [../03-relations.md](../03-relations.md) 6절, 원본 `test_token.py`의 `mini_catalogue()`, 원본 `docdb_seed_item_types.sql`, 원본 `load_catalogue.py`.
- **대상 파일**: `src/docdb/catalogue.py`, `catalogue/starter.json`, `catalogue/item_types.json`, `tests/fixtures/mini_catalogue.json`, `tests/conftest.py`(`starter_paths`, `mini_paths` 추가), `tests/unit/test_t10_catalogue.py`
- **`starter.json` 작성 규칙**:
  - 07 문서 3절 표의 18종. `title_ko`는 표의 "이름", 그룹은 `kind_id` 첫 글자.
  - 표의 "문서에 나온 필드"는 그 키로 필수 필드에 넣고 설명은 07·03 문서 문구로. 그 밖에 각 narrative 종류에 `overview`(필수), `owners`(필수), `notes`(선택)를 넣고 설명 끝에 `[설계 판단]`.
  - 저장 방식이 07 표와 같게 나오도록 `lifecycle`과 `content_files`를 정한다: view는 `VIEW_KINDS`, register는 `detail.md` 없음, run은 `lifecycle: "work"` + `run.json`, narrative는 `detail.md` 포함.
  - `required_when`·`applies_when`: K02, K07, P06, R04는 `{"fact": "cap.production"}`, 나머지는 `{"always": true}`. `facts`에는 `cap.production` 하나만(O5 기본값).
  - 연결 규칙: 03 문서 6절 표(필수 6, 선택 5)를 `required_link_kinds`·`optional_link_kinds`로.
  - L02 `test_ids` 필드: `{"description": "테스트 케이스 ID 목록. 한 줄에 TC ID와 검증하는 REQ ID", "extract_rel": {"rel": "verified_by", "from_prefix": "REQ-", "to_prefix": "TC-"}}`.
  - `covered_names`는 `[title_ko]`.
- **`item_types.json` 작성 규칙**: 원본 시드 39행을 같은 순서로 옮긴다. `defined_by_field`는 C-23 규칙.
- **`mini_catalogue.json`**: 원본 `mini_catalogue()` 반환 구조를 JSON으로 그대로(YAML 직렬화 단계만 생략).

| # | 상황 | 기대 |
|---|---|---|
| 1 | starter 적재 | 종류 18, 반환 dict의 `kinds == 18` |
| 2 | 종류별 `storage_class` | 07 표와 같음(A01·D08·F06 view, A04·B04·F07·L07·S03 register, K07·L03·R04 run, 나머지 narrative) |
| 3 | 항목 종류 | 적재 11, `item_types_skipped == 28` |
| 4 | `defined_by_field` | requirement→`requirement_ids`, acceptance_criterion→`acceptance`, test_case→`test_ids`, infra_resource→`resources`, known_error→`null` |
| 5 | `item_types.json` | 39행, (item_type, 접두어, 종류, 설명)이 원본 시드와 같다(원본 SQL을 정규식으로 읽어 비교) |
| 6 | `cat_kind_link` | 11행(필수 6: D01→L02, L02→D01, L03→L02, P06→K02, K07→K02, R04→K02 / 선택 5: K02→K01, K02→S03, B04→D01, L07→L02, F07→D01) |
| 7 | L02 `test_ids`의 `extract_rel` | 위 JSON과 같음 |
| 8 | 07 표에 없는 필드의 설명 | 모두 `[설계 판단]`을 포함 |
| 9 | `cat_fact` | `cap.production`만 |
| 10 | mini 적재 | 종류 45 |
| 11 | 같은 파일 재적재 | 오류 없음, 행 수 그대로 |
| 12 | 문서가 있는 종류를 뺀 카탈로그 재적재 | `CATALOGUE_INCOMPATIBLE` |
| 13 | 별칭 `작업 환경`을 G01·K01 둘 다 가진 카탈로그 | G01 `is_primary=1`, K01 `0` |
| 14 | 적재 후 | `cat_version.source_digest`=파일 sha256, `change_log`에 `catalogue` 1건 |

### T11 백업·CLI 골격·init

- **선행**: T10. **계약**: C-09(백업), C-24(`init`, `migrate`, `backup`, `restore`, `actor`, `verify-history`, 전역 옵션), C-27(픽스처). **결정**: D5, D21, D22, D25.
- **대상 파일**: `src/docdb/backup.py`, `src/docdb/cli.py`(이 작업 명령 + `catalogue load`, `policy allow-secret` 등록), `src/docdb/__main__.py`, `tests/conftest.py`(`project_db`, `mini_db`, `con`, `human`, `agent`, `ci`, `run_cli`), `tests/integration/test_t11_cli_init.py`
- **규칙**: `init` 출력은 `OK init project=<slug> db=<path>` 한 줄. `policy allow-secret` 출력은 `OK <exception_id>`. `--project`는 `^[a-z0-9][a-z0-9-]{0,62}$`(아니면 `BAD_ARGS`). `--owner`는 사람 형식(아니면 `ID_FORMAT`). CLI는 하위 명령 구조를 `argparse` 하위 파서로 만들되, 이 작업에서는 이 작업의 명령만 등록한다.

| # | 상황 | 기대 |
|---|---|---|
| 1 | `init` | 종료 0, `.docdb/docdb.sqlite`, `.docdb/.gitignore` 내용 `"*\n"`, `user_version == latest`, 행위자 4명, `project_id='t'`, 종류 18 |
| 2 | 같은 폴더에 다시 `init` | 종료 3, stderr `ERROR DB_EXISTS …` |
| 3 | `--project T_1` | 종료 1, `BAD_ARGS` |
| 4 | `--owner ai-x` | 종료 1, `ID_FORMAT` |
| 5 | `DOCDB_HOME=tmp/h`, `backup` 두 번(frozen_now) | `tmp/h/backups/t/20261013T000000Z-manual.sqlite`, `…-manual-2.sqlite` |
| 6 | 백업 파일 열기 | `user_version` 같음 |
| 7 | `restore` 비대화형 / 사람·대화형 | 종료 1 `NEEDS_HUMAN` / 교체됨(표시 행으로 확인), `pre-restore` 백업 생성 |
| 8 | 0001·0002만 적용한 DB에 `migrate` | 3~8 적용, `pre-migrate` 백업 생성 |
| 9 | 위 DB(마이그레이션 전)에 `actor list` | 종료 3, `MIGRATION_REQUIRED` |
| 10 | `actor add` 비대화형 / 사람·대화형(`--actor u-tester`) | `NEEDS_HUMAN` / 성공, `actor list`에 보임 |
| 11 | `verify-history` | stdout `OK history`, 종료 0 |
| 12 | 하위 프로세스 `python -m docdb --help`(PYTHONPATH=src) | 종료 0 |
| 13 | 오류 시 | stdout 비어 있음, stderr 한 줄 `ERROR <CODE> <detail>` |
| 14 | `--db <경로>`와 다른 cwd | `--db`가 우선 |
| 15 | `catalogue load <starter.json>` 비대화형 / 사람·대화형 | `NEEDS_HUMAN` / 성공, 행 수 그대로 |
| 16 | `policy allow-secret f:… --reason r --days 7` 비대화형 / 사람·대화형 | `NEEDS_HUMAN` / `OK PEX-2026-0001` |

---

## 2단계 — 원본 이식

### T12 LLM 읽기·쓰기 이식 (`ctx`·`get`·`find`·`trace`·`since`·`put` 필드)

- **선행**: T11. **계약**: C-12, C-13, C-14(필드 쓰기, `DocDB` 생성자), C-15(`session_ref`), C-16(정책 차단), C-24(해당 명령). **결정**: D8, D18, D19, D23. **원본**: `reference/docdb-package/docdb_tools/docctl_llm.py`, `test_token.py`.
- **대상 파일**: `src/docdb/llm.py`, `src/docdb/cli.py`(6개 명령 등록), `tests/legacy/test_token_port.py`, `tests/unit/test_t12_llm.py`
- **규칙**:
  - 원본 `docctl_llm.py`를 **복사한 뒤** 바꾼다. 바꾸는 것은 이 목록뿐이다: ① DB 연결은 받은 `con` 사용(`DocDB(con, actor, work, session, model)`) ② 핸들 파싱은 `parse_handle`(C-12) ③ `digest`·`tok_est`·`q`·`now`는 `textnorm`·`clock` ④ 쓰기는 `write_tx`(원본 SAVEPOINT 대체) ⑤ 이력은 `history.append_change` ⑥ 쓰기 전 `normalize_text`·`assert_no_secrets` ⑦ 정책 차단 줄(C-13) ⑧ `call_log.session_ref` ⑨ 오류 줄 코드는 C-05 이름.
  - 출력 형식은 한 글자도 바꾸지 않는다(C-13).
  - 원본 `test_token.py`의 28개 검사를 `tests/legacy/test_token_port.py`로 옮긴다. 데이터 생성은 `mini_db`. 바꿔도 되는 것은 C-27 이식 규칙뿐이다.

| # | 상황 | 기대 |
|---|---|---|
| 1~28 | 원본 `test_token.py` S1~S11의 28개 검사 | 원본과 같은 조건으로 모두 통과(비율 수치: 7.3%, 1.4%, 21 tok, 58 tok, 71% 포함) |
| 29 | `put` 텍스트 `"a\r\nb"` | 응답 `dg`가 `digest16("a\nb")`, 그 `dg`를 `expect`로 다시 `put` → `CONFLICT` 없음 |
| 30 | `put` 텍스트에 #T09-1 가짜 키 | `ERR … SECRET_DETECTED aws_access_key`, `NOTHING_APPLIED`, `change_log` 행 수 그대로 |
| 31 | `local_only` 문서의 `get level=2` | 줄 끝 ` policy=local_only`, 본문 없음 |
| 32 | `local_only` 문서 본문에만 있는 단어로 `find` | 핸들만, 발췌 없음, ` policy=local_only` |
| 33 | `local_only` 문서가 관련된 `ctx` / 바뀐 뒤 `since` | 요약 대신 `policy=local_only` / diff 없이 변경 사실·글자 수 |
| 34 | `DocDB(..., session="s1", model="m1")`로 `get` | `call_log.session_ref == "s1/m1"` |
| 35 | `put` 후 `change_log` | `row_digest` 형식 C-15, `verify_chain == []` |
| 36 | `get(["x:1"])` | 줄 `ERR x:1 BAD_HANDLE` |
| 37 | CLI `get`·`put`(run_cli) | `DocDB` 메서드 출력과 같은 텍스트 |

### T13 원본 시나리오·검수 이식

- **선행**: T12. **계약**: C-27(이식 규칙). **원본**: `test_scenarios.py`, `audit.py`.
- **대상 파일**: `tests/legacy/test_scenarios_port.py`, `tests/legacy/test_audit_port.py`. 원본 시나리오가 `src/docdb/`의 결함을 드러내면 고치지 말고 멈춤 보고한다(오케스트레이터가 별도 수정 작업으로 만든다)
- **규칙**: 원본 검사 조건을 바꾸지 않는다. 원본의 `ev()` 평가기는 테스트 안에 그대로 둔다(T19에서 `profile.evaluate`로 교체 확인).

| # | 상황 | 기대 |
|---|---|---|
| 1~33 | 원본 33개 검사(적용 판정 3, 종류 수 집계, 문서·필드·경로·증거·이력·승인·보존) | 모두 통과. "194종 집계"는 "적재한 종류 수와 같음" |
| 34 | 원본 `audit.py`의 매핑 검사 5묶음 | 55/55, 14/14, 11/11, 6/6, 8/8 |

---

## 3단계 — 관계·항목

### T14 항목 쓰기 (`put` `i:` `upsert`)

- **선행**: T13. **계약**: C-10, C-14(항목 쓰기), C-16. **결정**: D10, D17.
- **대상 파일**: `src/docdb/llm.py`, `tests/unit/test_t14_items.py`

| # | 상황 | 기대 |
|---|---|---|
| 1 | `{"h":"i:REQ-HW-009","op":"upsert","title":"디스크","defined_in":"d:<문서>"}` | `OK i:REQ-HW-009 dg=<16> tok=<n>`, 행 생성, `item_type='requirement'`, 이력 insert |
| 2 | 새 항목인데 `title` 없음 | `ERR … BAD_OP` |
| 3 | `i:ZZZ-1`(없는 접두어) | `ERR … ID_FORMAT` |
| 4 | `attrs`와 `attrs_patch` 함께 | `ERR … BAD_OP` |
| 5 | `attrs={"a":1,"b":2}` 후 `attrs_patch={"b":null,"c":3}` | `attrs == {"a":1,"c":3}` |
| 6 | 틀린 `expect` | `ERR … CONFLICT now=<dg>` |
| 7 | 제목에 가짜 키 | `ERR … SECRET_DETECTED` |
| 8 | 삭제 이력이 있는 ID로 생성 | `ERR … ID_FORMAT` |
| 9 | 항목 수정 후 `get i:` level 0 | 새 `dg` 표시 |
| 10 | 항목 패치 + 오류 필드 패치를 한 `put`에 | 둘 다 적용 안 됨(`NOTHING_APPLIED`) |

### T15 관계 제안·확정

- **선행**: T14. **계약**: C-13(관계 목록 줄), C-14(관계 제안·철회), C-17(`propose`~`remove`, `pending_count`), C-24(`link`). **결정**: D10, D11.
- **대상 파일**: `src/docdb/relations.py`, `src/docdb/llm.py`, `src/docdb/cli.py`, `tests/unit/test_t15_relations.py`
- **규칙**: `pending_count(con, work_id)`와 `review(con, work_id)`의 작업 필터는 "그 관계의 insert 이력(`change_log`)의 `work_id`가 같은 것"이다.

| # | 상황 | 기대 |
|---|---|---|
| 1 | AI `link`(정상) | `OK rel=<id> proposed`, `source='agent'`, `proposed_by='ai-claude'` |
| 2 | `why` 없음 / 빈 문자열 | `ERR … BAD_OP` |
| 3 | `rel="owns"` | `ERR … BAD_OP` |
| 4 | 없는 끝점 | `ERR … NOT_FOUND` |
| 5 | 끝점이 `f:` | `ERR … UNSUPPORTED_HANDLE` |
| 6 | 같은 조합 재제안 | `ERR … DUPLICATE rel=<id> status=proposed` |
| 7 | 반려 후 재제안 | `ERR … REJECTED_BEFORE rel=<id>` |
| 8 | 자기 제안 `unlink` / 남의 제안 / 확정된 관계 | `OK unlink rel=<id>` / `NEEDS_HUMAN` / `NEEDS_HUMAN` |
| 9 | `link confirm` 비대화형 / 사람·대화형 | 종료 1 `NEEDS_HUMAN` / `confirmed`, `decided_by`, 이력 update |
| 10 | `link reject` `--why` 없음 / 있음 | `BAD_ARGS` / `rejected` |
| 11 | `link remove` 확정 관계 / 규칙 관계 | 삭제·이력 delete / `BAD_OP` |
| 12 | `link review` | C-13 형식 줄, `rel` 순 |
| 13 | `pending_count` | 제안 수와 같음, `--work` 필터 적용 |
| 14 | 한 `put`에 필드 쓰기 + 잘못된 `link` | 필드도 적용 안 됨 |
| 15 | `changes`를 `i:`에서 출발 / `supersedes`를 `d:` → `i:` | `ERR … BAD_OP` ×2(허용 끝점 표) |
| 16 | 사람 행위자(`--actor u-tester`)가 CLI `put`으로 `link` | `OK rel=<id> proposed`, `source='human'`, 상태는 `proposed` |
| 17 | `team-ci`가 `link` | `ERR … BAD_ARGS` |

### T16 규칙 추출 R1·R2·R3·R5

- **선행**: T15. **계약**: C-17 표·정규식, C-13(`NOTE`). **결정**: D10, D11, D12.
- **대상 파일**: `src/docdb/relations.py`, `src/docdb/llm.py`(필드·항목 쓰기 뒤 `after_*` 호출), `tests/integration/test_t16_rules.py`
- **데이터**: `project_db`(시작 카탈로그). D01 문서 `DOC-REQ-HW`, L02 문서 `DOC-TC-HW`, K02 문서 `DOC-K02`를 테스트에서 만든다.

| # | 상황 | 기대 |
|---|---|---|
| 1 | D01 `requirement_ids`에 `"REQ-HW-001: 디스크 경고\nREQ-HW-002 - 메모리 경고"` | 항목 2개, 제목 `디스크 경고`·`메모리 경고`, `defined_in=DOC-REQ-HW`, 이력 `reason='rule:R1'` |
| 2 | 같은 필드를 `"REQ-HW-001: 디스크 사용률 경고"`로 교체 | REQ-HW-001 제목 갱신, REQ-HW-002는 남음 |
| 3 | 다른 D01 문서에 `REQ-HW-001` 정의 | 응답에 `NOTE R1 skip REQ-HW-001 defined_in=DOC-REQ-HW`, 변경 없음 |
| 4 | K02 문서 `overview`에 `REQ-HW-001` 언급 | `relation(document DOC-K02 → item REQ-HW-001, references, rule:R2, anchor d:DOC-K02)` |
| 5 | 언급 삭제 | 그 R2 관계 삭제, 이력 delete |
| 6 | 없는 ID `REQ-HW-999`·자기 문서 정의 항목 언급 | R2 관계 없음 |
| 7 | L02 `test_ids`에 `"TC-HW-001 REQ-HW-001 디스크 경고 확인"` | TC-HW-001 생성(R1), `REQ-HW-001 verified_by TC-HW-001`(rule:R3, anchor `f:DOC-TC-HW/L02/test_ids`), `v_rtm`에 보임 |
| 8 | 그 줄에서 REQ 제거 | R3 관계 삭제 |
| 9 | `DocDB(work=WORK-…)`로 필드 쓰기 두 번 | `work → document changes rule:R5` 1건 |
| 10 | 같은 조합이 반려된 에이전트 제안으로 있음 | 규칙이 새로 만들지 않음 |
| 11 | 같은 내용으로 `put` 두 번 | 두 번째에 관계 변화 없음, 관계 이력 추가 없음 |

### T17 `ctx`·`trace` 확장

- **선행**: T16. **계약**: C-13(새 줄), C-17(`pending_count`, `missing_links`). **결정**: D10.
- **대상 파일**: `src/docdb/llm.py`, `tests/integration/test_t17_ctx.py`
- **규칙**: `ctx`의 `! pending_links=<n>`은 프로젝트 전체 수(작업 필터 없음), 강제 줄. `! missing_link`는 `ctx`가 고른 관련 문서만, 원본 P2 우선순위·예산 안.

| # | 상황 | 기대 |
|---|---|---|
| 1 | 제안 2건 존재 | `ctx`에 `! pending_links=2` |
| 2 | 제안 0건 | 그 줄 없음 |
| 3 | 관련 D01 문서에 L02 연결 없음 | `! missing_link d:<문서> k:L02` |
| 4 | 연결 확정 후 | 그 줄 사라짐 |
| 5 | `trace` | `proposed`·`rejected` 관계 안 나옴 |
| 6 | 예산을 아주 작게 | `missing_link` 줄이 빠지고 `omitted` 증가, `pending_links` 줄은 남음 |

---

## 4단계 — 작업 흐름

### T18 작업·인계

- **선행**: T17. **계약**: C-14(작업·인계 패치), C-18, C-24(`work status`, `handoff ack`). **결정**: D15, D24, D25.
- **대상 파일**: `src/docdb/workflow.py`, `src/docdb/llm.py`, `src/docdb/cli.py`, `tests/integration/test_t18_workflow.py`
- **규칙**: `owner`가 사람이 아니면 `BAD_ARGS`. `base_refs` 형식 위반은 `BAD_ARGS`.

| # | 상황 | 기대 |
|---|---|---|
| 1 | `{"h":"w:new","op":"work","title":"서버 점검","work_type":"feature","owner":"u-tester","base_refs":["pmt:docdb/_default/Work1-1"]}` | `OK w:WORK-2026-0001 created`, `status='planned'`, `base_refs` 저장 |
| 2 | `owner="ai-claude"` / `base_refs=["x:1"]` | `ERR … BAD_ARGS` ×2 |
| 3 | AI: planned→active, active→review | 둘 다 OK |
| 4 | AI: review→verified | `ERR … NEEDS_HUMAN` |
| 5 | planned→closed | `ERR … STATUS_FORBIDDEN` |
| 6 | CLI `work status <W> verified` 사람·대화형 → `closed` | 성공, `closed_at` 기록 |
| 7 | 인계 `put`(다섯 목록 + `as_of`) | `OK handoff=HO-2026-0001`, `v_work_resume.next_actions` 반영 |
| 8 | `payload`에 `risks` 없음 / `as_of="x"` | `ERR … BAD_OP` ×2 |
| 9 | `handoff ack` 비대화형 / `--baseline` 없음 / 정상 / `--reject` | `NEEDS_HUMAN` / `BAD_ARGS` / `accepted` / `rejected` |
| 10 | 위 동작의 이력 | `work`·`handoff` insert·update 기록 |
| 11 | 만든 작업으로 `ctx` | 작업 카드 줄 `W WORK-2026-0001 …` |

### T19 프로필·적용 판정

- **선행**: T18. **계약**: C-19, C-24(`fact`, `applicability`). **결정**: O5 기본값.
- **대상 파일**: `src/docdb/profile.py`, `src/docdb/cli.py`, `tests/legacy/test_scenarios_port.py`(원본 `ev()`를 `profile.evaluate` 호출로 교체), `tests/unit/test_t19_profile.py`

| # | 상황 | 기대 |
|---|---|---|
| 1 | `evaluate`: always / fact 1·0·None / all(True,None) / all(True,False) / any(None,True) / any(None,False) / selected_kind | True / True·False·None / None / False / True / None / 선택 여부 |
| 2 | `{"eval": "1"}` | `BAD_ARGS` |
| 3 | 시작 카탈로그 `fact list` | `cap.production unknown` |
| 4 | `applicability` | D01 `required=true`, K02 `unknown`, 개수 출력 |
| 5 | `fact set cap.production true` 비대화형 / 사람·대화형 | `NEEDS_HUMAN` / 값 1, `profile_revision=2`, 이력, 재계산 후 K02 `true` |
| 6 | `fact set evt.release true` | `BAD_ARGS` |
| 7 | 이식 시나리오 AT-01~03 | `profile.evaluate`로 바꿔도 통과 |

---

## 5단계 — 실행 증거

### T20 실행 결과 적재

- **선행**: T19. **계약**: C-20, C-17(R4), C-24(`run ingest`). **결정**: D15.
- **대상 파일**: `src/docdb/runs.py`, `src/docdb/relations.py`(`apply_run_coverage`), `src/docdb/cli.py`, `tests/integration/test_t20_runs.py`
- **규칙**: `result`가 5종(PASS, FAIL, NOT_RUN, ERROR, UNKNOWN)이 아니면 INSERT 전에 `BAD_ARGS`.

| # | 상황 | 기대 |
|---|---|---|
| 1 | 필수 5키만 | `OK r:RUN-2026-0001` |
| 2 | `result="OK"` / 필수 키 누락 | `BAD_ARGS` |
| 3 | 행위자 `ai-claude` | `trust_source='ai_reported'` |
| 4 | `raw_log` 포함 | `ART-2026-0001`(role evidence), `run.raw_artifact_id` 연결 |
| 5 | `covers=["TC-HW-001"]` | `run → item evidence_for rule:R4` |
| 6 | `covers`에 없는 항목 | `NOT_FOUND`, 아무것도 저장 안 됨 |
| 7 | `findings` 1건 / 메시지에 가짜 키 | 저장 / `SECRET_DETECTED`, 전체 취소 |
| 8 | 같은 내용 두 번 | `RUN-2026-0002` |
| 9 | `work_id` 있는 FAIL 실행 후 `ctx` | `! r:RUN-… <type> FAIL` |
| 10 | `run ingest -`(표준 입력) | 파일과 같은 결과 |

---

## 6단계 — import

### T21 분할과 scan

- **선행**: T20. **계약**: C-21(scan·분할·규칙·적용·백업), C-14(필드 쓰기 경로). **결정**: D6, D22.
- **대상 파일**: `src/docdb/importer.py`, `src/docdb/cli.py`(`import scan`), `tests/fixtures/import/`(아래 파일), `tests/integration/test_t21_import_scan.py`
- **픽스처 파일**(테스트가 프로젝트 폴더 안으로 복사): `요구사항.md`(`# 요구사항`, `## behavior` 본문 2줄, ```` ``` ```` 블록 안에 `# 코드 제목`, `## 기타 메모`, 빈 `## scope`), `서버 목록.md`(`# 서버 목록`, `## resources` 본문에 `RES-DB01 운영 DB`), `notes.txt`(제목 없음 3줄), `bad.bin`(UTF-8 아닌 바이트), `bom.md`(UTF-8 BOM + `# 요구사항`), `crlf.md`(CRLF 줄바꿈, `# 요구사항`, `## behavior`)

| # | 상황 | 기대 |
|---|---|---|
| 1 | `split_segments(요구사항.md 바이트)` | 코드 블록 안 `#`는 구간 아님, 구간 제목 `요구사항`·`behavior`·`기타 메모`·`scope` |
| 2 | 각 구간의 `raw[start_off:end_off]` 디코드 | 구간 `text`와 같음 |
| 3 | `crlf.md` | 오프셋이 원본 바이트 기준, `text_digest`가 LF 버전과 같음 |
| 4 | 프로젝트 밖 폴더 scan | `BAD_ARGS` |
| 5 | scan | artifact 6건(`uri`는 POSIX 상대 경로), `bad.bin`은 구간 없음, `bom.md` 첫 제목 정상 |
| 6 | 종류 규칙 | `요구사항.md`·`bom.md`·`crlf.md`→D01, `서버 목록.md`→K02, `notes.txt` 미정 |
| 7 | `## behavior` | `ruled` → `applied`, `field_value(D01, behavior)` = 본문 2줄(제목 줄 없음) |
| 8 | 새 문서 | `doc_id`가 `DOC-D01-IMP-<4자리>` 형식(한글 파일 이름), `owner_id='u-tester'`, `export_policy='approved_providers'` |
| 9 | 빈 `## scope` | `skipped`, `why='empty'` |
| 10 | `## resources`(K02) 적용 | R1으로 `RES-DB01` 항목 생성 |
| 11 | 같은 폴더 다시 scan | 새 artifact·구간 0 |
| 12 | scan 후 | `pre-import-scan` 백업 파일 존재 |

### T22 next·label·report

- **선행**: T21. **계약**: C-21(`next_batch`, `label`, `report` 형식). **결정**: D6, D8, D22.
- **대상 파일**: `src/docdb/importer.py`, `src/docdb/cli.py`(`import next|label|report`), `tests/integration/test_t22_import_label.py`

| # | 상황 | 기대 |
|---|---|---|
| 1 | `import next` | 머리줄 형식, `d?:ART-… "notes.txt" heads=` 줄이 먼저, 그다음 `s:` 줄(`기타 메모`) |
| 2 | `cand` | 최대 5개, 같은 입력이면 같은 순서 |
| 3 | `--budget 30` | `omitted>0` |
| 4 | `--export-policy local_only`로 scan한 파일의 구간 | 발췌 없이 ` policy=local_only` |
| 5 | `[{"doc":"d?:<notes 파일>","kind":"G01"}]` | `OK d?:<ART> kind=G01 ruled=<n>` |
| 6 | `[{"seg":"s:<기타 메모>","kind":"D01","field":"behavior"}]` | `OK s:<id> applied`, `behavior`에 기존 본문 + `"\n\n"` + 새 본문(seq 순) |
| 7 | `skip` 라벨 | `OK s:<id> skipped`, `why` 저장 |
| 8 | scan 뒤 원본 파일 수정 후 label | `ERR s:<id> IMPORT_STALE`, `status='stale'`, 같은 호출의 다른 라벨도 적용 안 됨 |
| 9 | 알 수 없는 키 / 문서 종류와 다른 `kind` | `ERR … BAD_OP` / `ERR … BAD_ARGS` |
| 10 | `import report` | C-21 형식, 상태별 수 정확 |
| 11 | label 후 | `pre-import-label` 백업 존재 |

---

## 7단계 — 요약

### T23 요약

- **선행**: T20(T21·T22와 대상 파일이 겹치지 않아 T21과 병렬 가능). **계약**: C-22, C-14(`sum`), C-13(`sum=` 표기). **결정**: D16.
- **대상 파일**: `src/docdb/summaries.py`, `src/docdb/llm.py`, `tests/integration/test_t23_summaries.py`

| # | 상황 | 기대 |
|---|---|---|
| 1 | `template`: 항목 / JSON 객체 필드 / JSON 배열 필드 / 실행 | `"REQ-HW-001 - 디스크 경고"` 형식 / `"json keys=a,b"` / `"json list n=3"` / `"RUN-2026-0001 test PASS"` |
| 2 | `put` + `sum`(60 tok 이하) | 저장, `get level=1`에 `sum=<요약>` |
| 3 | `sum` 61 tok 이상 | `ERR … SUMMARY_TOO_LONG`, 필드도 적용 안 됨 |
| 4 | 요약 후 `sum` 없이 필드 수정 | `get level=1`에 `sum=stale` |
| 5 | `sum`에 가짜 키 | `SECRET_DETECTED` |
| 6 | `ctx`의 항목·실행 줄 | 템플릿 요약 사용 |
| 7 | 이식 S9 검사 | 계속 통과 |

---

## 8단계 — 진입점·MCP·플러그인

### T24 부트스트랩 `docdb_launch.py`

- **선행**: T23. **계약**: C-04(venv·스탬프), C-05(환경 오류), C-24(`docdb_launch.py`). **결정**: D3, D4, D13, D26.
- **대상 파일**: `src/docdb_launch.py`, `tests/integration/test_t24_launch.py`
- **규칙**: `setup`의 외부 명령은 모듈 수준 함수 `_run(cmd: list) -> None`(내부에서 `subprocess.run(cmd, check=True)`)으로만 부른다. 테스트는 `_run`을 monkeypatch한다.
- **테스트 건너뛰기 허용**: `/usr/bin/python3.6`이 없으면 #1만 `pytest.skip("python3.6 없음")`(C-27).

| # | 상황 | 기대 |
|---|---|---|
| 1 | `/usr/bin/python3.6 src/docdb_launch.py cli actor list` | 종료 2, stderr `ERROR PY_TOO_OLD …` |
| 2 | 빈 `DOCDB_HOME`에서 `cli` | 종료 2, `NO_VENV` |
| 3 | 스탬프 Python이 `2.7.0` | 종료 2, `VENV_MISMATCH` |
| 4 | `setup`(`_run` 가짜) | 호출 순서 ①`[sys.executable, "-m", "venv", <venv_dir>]` ②`[<venv_python>, "-m", "pip", "install", "-c", <constraints.txt>, "pysqlite3", "mcp"]`, 스탬프의 `constraints_sha256` 정확 |
| 5 | `setup --remove` | venv 폴더 삭제, `backups/`는 남음 |
| 6 | `DOCDB_HOME/venv`를 개발 venv에 심볼릭 링크 + 맞는 스탬프, `cli --db <DB> actor list` | 종료 0, 행위자 목록 |
| 7 | 패키지 없는 빈 venv(`uv venv`) + 맞는 스탬프 | 종료 2, `DEP_MISSING` |
| 8 | #6 방식 venv, `DOCDB_START_DIR` 없음, `PWD=<tmp>/proj`, `mcp`(가짜 `docdb.mcp_server`를 `sys.modules`에 주입해 `main` 호출 시점의 환경을 기록) | 기록된 `DOCDB_START_DIR == "<tmp>/proj"` (C-24, D26) |
| 9 | #8과 같고 `DOCDB_START_DIR=<tmp>/other` | 기록된 값이 `<tmp>/other` 그대로 |
| 10 | #6 방식 venv + `PYTHONUSERBASE=<tmp>/ub`의 사용자 site-packages에 import 시 예외를 던지는 가짜 `typing_extensions.py`, `cli --db <DB> actor list` | 종료 0(venv 경로가 사용자 site-packages보다 앞, C-24) |

### T25 MCP 서버

- **선행**: T24. **계약**: C-25, C-03. **결정**: D3, D18, O21 기본값. **원본**: `docdb_mcp.py`, `test_mcp.py`.
- **대상 파일**: `src/docdb/mcp_server.py`, `tests/integration/test_t25_mcp.py`
- **규칙**: 원본 `docdb_mcp.py`를 복사한 뒤 바꾼다. 바꾸는 것: ① DB는 `open_project_db` ② `DocDB` 생성자(C-14) ③ 오류는 `format_error` 문자열 반환 ④ 행위자 검사 ⑤ `docdb_put` 설명(C-25 문자열) ⑥ `DOCDB_SESSION`·`DOCDB_MODEL` 전달. 테스트 클라이언트는 원본 `test_mcp.py`처럼 SDK stdio 클라이언트로 `docdb_launch.py mcp`를 하위 프로세스로 띄운다(T24 #6의 venv 링크 방식).
- **행위자 검사**: 각 도구 호출 시작에서 `DOCDB_ACTOR`를 `resolve_actor` → `require_ai`. 실패하면 그 호출은 `format_error` 문자열을 반환한다.

| # | 상황 | 기대 |
|---|---|---|
| 1 | `list_tools` | 이름 6개(원본과 같음), `docdb_put` 외 설명은 원본 문자열, `docdb_put`은 C-25 문자열 |
| 2 | `docdb_ctx` 호출 | 결과 content가 text 1블록, `structuredContent` 없음 |
| 3 | DB 없는 폴더(`DOCDB_START_DIR`)에서 서버 시작 후 호출 | 서버 정상, 결과 텍스트가 `ERROR NO_DB`로 시작 |
| 4 | `DOCDB_ACTOR=u-tester` | 결과 `ERROR BAD_ARGS …` |
| 5 | `docdb_put` 후 DB | 값 저장, `call_log.session_ref`가 환경 변수 반영 |
| 6 | 서버 `INSTRUCTIONS` | 원본 문자열과 같음 |

### T26 플러그인 패키징

- **선행**: T25, **T00b 반영 완료**(C-26 Codex 절이 채워져 있어야 시작). **계약**: C-26. **결정**: D1, D14, D18, D26.
- **대상 파일**: `docdb/.claude-plugin/plugin.json`, `docdb/.codex-plugin/plugin.json`, `docdb/codex.mcp.json`, `docdb/bin/docctl`(실행 권한), `docdb/skills/docdb/SKILL.md`, `j-ai-plugin/.claude-plugin/marketplace.json`, `j-ai-plugin/.agents/plugins/marketplace.json`, `j-ai-plugin/README.md`(docdb 절 추가), `docdb/tests/integration/test_t26_plugin.py`

| # | 상황 | 기대 |
|---|---|---|
| 1 | 두 `plugin.json`과 `codex.mcp.json` | 올바른 JSON, `version == __version__`, `DOCDB_ACTOR`가 각각 `ai-claude`·`ai-codex` |
| 2 | `claude plugin validate docdb`(저장소 루트에서) | `Validation passed`(경고 없음) |
| 3 | `bin/docctl --db <DB> actor list`(DOCDB_HOME = T24 #6 방식) | 종료 0 |
| 4 | venv 없는 `DOCDB_HOME`에서 `bin/docctl` | 종료 2, stderr `ERROR NO_VENV` |
| 5 | `SKILL.md` | C-26 필수 절이 그 순서로 있고 앞 2,000자 안에 "언제 쓰나" |
| 6 | 두 마켓플레이스 | `docdb` 항목, 버전 일치 |
| 7 | `codex.mcp.json`의 `docdb` 서버 | C-26 Codex 절과 같다: `cwd == "."`, `args == ["./src/docdb_launch.py", "mcp"]`, `env_vars`에 `PWD` 포함 |

---

## 9단계 — 검증·반영

### T27 매트릭스 스크립트 (Sonnet 작성, 오케스트레이터 실행)

- **대상 파일**: `docdb/tools/matrix.sh`. **계약**: C-27. **결정**: D9.
- **동작**: 3.10~3.14마다 `uv venv <임시>/.venv-<ver> --python <ver>` → `uv pip install -c constraints-dev.txt pytest pysqlite3 mcp` → `python -m pytest -q` → 버전별 `PASS`/`FAIL`/`SKIP(인터프리터 없음)` 한 줄. 하나라도 FAIL이면 종료 코드 1.
- **완료 조건**: 오케스트레이터가 실행해 결과를 [../08-verification.md](../08-verification.md)에 기록.

### T28 GitHub Actions (오케스트레이터, push 승인 필요)

- 워크플로: `ubuntu-latest`, `macos-latest`, `windows-latest` × Python 3.10, 3.13. 승인 전에는 파일만 만들고 "미검증"으로 기록.

### T29 실사용 확인 (오케스트레이터)

- 로컬 마켓플레이스로 Claude Code와 Codex에 설치 → `setup` → 임시 프로젝트에서 `init` → 에이전트로 `ctx` → `put`(필드·항목·`link`) → 사람 `link confirm` → `since`. 각 단계 출력 기록.

### T30 문서 반영 (오케스트레이터)

- [../11-schema.md](../11-schema.md)를 실제 마이그레이션 적용 DB로 다시 생성(7절 "계획된 변경"은 "적용됨"으로).
- [../12-features.md](../12-features.md) 상태 열 갱신, [../08-verification.md](../08-verification.md) 결과, 원본 설계 문서 요약 문제(P3)는 docs-db README의 안내로 갈음했음을 확인.
- PMT 기록, 사용자 보고.

---

## 보류 작업 (B·C 결정 후 계획 추가)

| 작업 | 막는 결정 | 결정 후 할 일 |
|---|---|---|
| T31 gate 검사·기준선 고정·승인 CLI | O24 문서 상태 전이, O25 gate 종류 | 계약 C-28 추가 후 작업 절 작성 |
| T32 렌더 | O27 산출물 형식·위치 | 계약 C-29 추가 |
| T33 내보내기 | O29 형식 | 계약 C-30 추가 |
| T34 보존 정리 | O18 | 계약 C-31 추가 |
