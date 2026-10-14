# 08. 검증 — 테스트, 매트릭스, 실측, 완료 기준, 위험

## 1. 테스트 목록

| 테스트 | 내용 | 상태 |
|---|---|---|
| `test_scenarios.py` | 원 33건(적용 판정, 문서·필드·경로·증거·이력·승인·보존 규칙). 카탈로그 픽스처를 JSON으로 바꾸고 "194종 집계" 단정을 "적재한 종류 수와 일치"로 일반화(P2) | 원본 있음, 수정 필요 |
| `test_token.py` | 원 28건(ctx·get·put·since·find·trace·call_log) | 원본 있음 |
| `test_mcp.py` | 공식 SDK 클라이언트로 도구 6개·결과 text 1블록 확인 | 원본 있음 |
| `audit.py` | 표준 항목↔컬럼 매핑 94/94 | 원본 있음 |
| `test_fixes.py` | P4: 항목 삭제 후 `find`에 나오지 않음. P5: `local_only` 문서의 `get` L2·`ctx` 요약·`find` 발췌·`since` diff에 본문 없음 | 신규 |
| `test_relations.py` | D10: AI가 넣은 관계는 `proposed`만, 사람만 확정·반려, `confirmed` 삭제는 사람만, 반려 재제안 `REJECTED_BEFORE`, 조회 뷰는 `confirmed`만, 추출 규칙 R1~R5, `v_missing_required_links`, `change_log` 기록 | 신규 |
| `test_import.py` | 합성 md 3개: 규칙 매핑, `next` 예산, `label` 적용, digest 변경 시 `stale`, 적용 후 R1·R2 추출, `change_log` 체인 유효, `report` 수치 | 신규 |
| `test_env.py` | venv 없음, venv 버전 불일치, Python 3.9에서 안내 메시지와 종료 코드 2 | 신규 |

## 2. 검증 매트릭스 (D9)

| 위치 | 대상 |
|---|---|
| 로컬 Linux(개발 서버) | Python 3.10, 3.11, 3.12, 3.13, 3.14(`uv`로 설치) × 전체 테스트 |
| GitHub Actions | `ubuntu-latest`, `macos-latest`(arm64), `windows-latest` × Python 3.10, 3.13. **push는 사용자 승인 후** |
| 도구 실측 | `claude plugin validate`, Claude Code `/mcp`에서 도구 6개 확인, Codex에서 도구 노출 확인 |

## 3. 실측 항목 (구현 계획의 첫 단계, 미결 O7)

| # | 확인 | 결과에 따라 바뀌는 것 |
|---|---|---|
| S1 | Claude Code·Codex가 SDK 서버에 실제로 연결되는지(MCP 세대 포함) | 실패 시 SDK 버전 범위 |
| S2 | Codex `codex.mcp.json`의 상대 경로·작업 폴더·환경 변수·Windows 명령 해석 | [06](06-runtime-plugin.md) 6.3, 3절 Codex 행 |
| S3 | Claude MCP `env`에서 `${CLAUDE_PROJECT_DIR}` 치환 여부, MCP 서버 작업 폴더 | [06](06-runtime-plugin.md) 5절 탐색 시작점 |
| S4 | Windows: `py` 실행, venv site-packages 붙이기, 한글 stdio | [06](06-runtime-plugin.md) 3절 |
| S5 | 휠 없는 플랫폼(Linux aarch64, Intel Mac)에서 `pysqlite3` 소스 빌드 | [06](06-runtime-plugin.md) 1.1절 지원 범위 표기 |

### 3.1 실측 결과 (2026-10-13, 개발 서버 Linux x86_64, `mcp` 2.3.0 SDK 서버)

T00a(Claude Code 2.1.296, `claude --plugin-dir`)와 T00b(codex-cli 0.162.1, 로컬 마켓플레이스 설치 후 `codex exec`). 도구 `ping` 하나를 노출하는 시험 서버로 작업 폴더·환경 변수·원시 JSON-RPC 요청을 기록했다.

| # | 결과 |
|---|---|
| S1 | 둘 다 연결·도구 호출 성공. Claude Code 첫 요청은 `server/discover`(`_meta` 프로토콜 `2026-07-28`, 이어서 `subscriptions/listen`), Codex 첫 요청은 `initialize`(`protocolVersion` `2025-06-18`). `mcp` 2.3.0이 두 세대를 모두 처리한다 |
| S2 | `args`의 상대 경로(`./x.py`)와 `${PLUGIN_ROOT}`는 실패(서버 미시작, `${PLUGIN_ROOT}`는 치환 없이 문자 그대로). 절대 경로는 성공. `"cwd": "."`를 주면 작업 폴더가 설치된 플러그인 루트(`$CODEX_HOME/plugins/cache/<마켓>/<플러그인>/<버전>`)가 되어 상대 경로가 성공. `cwd`가 없으면 작업 폴더는 Codex 작업 폴더(프로젝트). 서버 환경은 `HOME LANG LOGNAME PATH SHELL TERM USER`만 전달되고 `env_vars`에 적은 변수(`PWD` 등, 미설정 변수가 있어도 무방)와 `env`의 고정값이 추가된다. MCP roots는 미지원(빈 목록). 상대 `command`(`./x.sh`)는 `cwd` 없이 실패. Windows는 S4(T00c) |
| S3 | `env`의 `${CLAUDE_PLUGIN_ROOT}`·`${CLAUDE_PROJECT_DIR}`·`${CLAUDE_PLUGIN_DATA}` 모두 치환됨(`CLAUDE_PLUGIN_DATA` = `~/.claude/plugins/data/<플러그인>-<마켓>`). MCP 서버 작업 폴더는 Claude Code를 시작한 프로젝트 폴더 |

반영: [06](06-runtime-plugin.md) 3절·6.3절, 계약 C-24(`mcp`의 `PWD` → `DOCDB_START_DIR`)·C-26 Codex 절, 결정 D26.

T00c(2026-10-14, GitHub Actions run 38484842771, 임시 orphan 브랜치 `spike/docdb-t00c`, 확인 뒤 삭제):

| # | 결과 |
|---|---|
| S4 | `windows-2025`: `py` 실행기 있음(3.10~3.14, 기본 3.14.7). `py -3 -m venv` + `pip install pysqlite3==0.6.1 mcp==2.3.0` 성공(휠). SDK stdio 클라이언트로 한글 문자열 왕복 성공(`KOREAN_ROUNDTRIP OK`). `py -3`에 venv site-packages를 C-24 방식(`scheme="nt"`, purelib=platlib=`v\Lib\site-packages`, 맨 앞 배치)으로 붙여 `pysqlite3`·`mcp` import와 기능 검사 통과(SQLite 3.53.4). 미측정: Claude Code·Codex가 Windows에서 MCP `command`로 `py`를 해석하는지 |
| S5 | `ubuntu-24.04-arm`(Python 3.12.15): 소스 빌드 성공, 시스템 SQLite 3.45.1에 링크, 기능 검사 통과. `macos-15-intel`(Python 3.12.10): 소스 빌드 실패 — Apple 시스템 SQLite에 `sqlite3_load_extension`이 없어 컴파일 오류 2건. Intel Mac은 지원 범위 밖 |

반영: [06](06-runtime-plugin.md) 1.1절 지원 범위 표, 저장소 README 지원 플랫폼(T26).

### 3.2 로컬 매트릭스 (T27, 2026-10-14, 개발 서버 Linux x86_64)

`docdb/tools/matrix.sh`(uv, `UV_PYTHON_DOWNLOADS=never`, `PYTHONNOUSERSITE=1`) 결과: `py3.10 PASS`, `py3.11 PASS`, `py3.12 PASS`, `py3.13 PASS`, `py3.14 SKIP(인터프리터 없음)` — 종료 코드 0, 버전마다 전체 pytest. 3.14는 GitHub Actions 매트릭스(T28)에서 확인한다. 이 실행 전 두 결함을 고쳤다: 3.12에서 뽑은 `constraints.txt`가 3.10 설치를 막음(범용 해석으로 교체), T24 테스트가 `docdb/.venv`에 묶여 다른 인터프리터에서 실패함(실행 중 venv 사용).


### 3.3 실사용 확인 (T29, 2026-10-14, 개발 서버, 격리된 `DOCDB_HOME`·프로젝트 폴더)

| 단계 | 결과 |
|---|---|
| `python3 src/docdb_launch.py setup` | 데이터 폴더에 venv 생성·의존성 설치 13초(범용 `constraints.txt`가 pip에서도 동작), 스탬프 기록 |
| `docctl init` | `.docdb/docdb.sqlite`·`.gitignore`, 행위자 4명 |
| `put` 작업 생성 → `import scan docs` | `WORK-2026-0001`, `요구사항.md`가 `DOC-D01-IMP-0001`로 적용(scan마다 `run(run_type='import')`, artifact `access_policy_ref='r:RUN-…'`) |
| Claude Code(`claude --plugin-dir docdb -p`) | MCP `docdb_ctx`→`docdb_get`(level 1, `sum=none`)→`docdb_put`(필드 append·항목 upsert·`link` 한 번에 `APPLIED ok=3`, 관계 `proposed`)→`docdb_since`(규칙 관계 제외) 성공. `DOCDB_HOME` 상속, `${CLAUDE_PROJECT_DIR}` 치환 확인 |
| 사람 `link confirm` | 비대화형 `ERROR NEEDS_HUMAN`(종료 1), TTY(`script`)에서 `OK rel=3 confirmed`, `since`에 확정 기록 |
| Codex(격리 `CODEX_HOME`, 저장소 마켓플레이스 설치) | `docdb_ctx`가 `PWD` 매핑(D26)으로 프로젝트 DB를 찾음, `docdb_get i:`의 `dg=`, `docdb_trace` 확정 관계 정상. `docdb_put`은 처음 `ERR ? BAD_HANDLE` — 에이전트가 `h` 대신 `handle` 키를 보냄. 도구 설명에 패치 키를 명시(C-25 보정)한 뒤 `APPLIED ok=1`, 행위자 `ai-codex` |

참고: 작업 트리에서 로컬 설치하면 Codex가 미추적 개발 venv까지 복사한다(67MB). GitHub 설치에는 해당 없음.
## 4. 완료 기준

1. 로컬 매트릭스(Python 3.10~3.14) 전체 통과.
2. GitHub Actions 3개 OS 통과. push 승인 전에는 "미검증"으로 표기한다.
3. `claude plugin validate` 통과, Claude Code와 Codex에서 도구 6개 확인.
4. `test_relations.py`, `test_import.py` 통과(합성 데이터 기준).
5. venv가 없거나 Python이 3.10 미만이면 안내 메시지와 함께 종료 코드 2로 끝난다.
6. 원문 설계 문서 0장 수정(P3), 마켓플레이스 2곳 등록, 저장소 README에 설치 절차 추가.

## 5. 위험

| 위험 | 대응 |
|---|---|
| `pysqlite3` 휠이 없는 플랫폼 | S5 결과로 지원 범위를 README에 명시 |
| `mcp` SDK 변경 | `constraints.txt` 고정, 매트릭스로 갱신 확인 |
| 데이터 폴더 venv가 플러그인 제거 후 남음 | `setup --remove` 제공, README에 명시 |
| 시작 카탈로그는 원 194종의 일부 | 변경 절차([07](07-catalogue.md) 5절)로 확장. 194종 관련 수치는 "재현 불가"로 표기 |
| 확정 대기 관계가 쌓임 | `ctx`에 대기 수 표시, `link review`로 일괄 처리 |
| PostgreSQL 이전(원 결정 DEC-07) | 범위 밖. SQLite 유지 |
