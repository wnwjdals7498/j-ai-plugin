# 06. 실행 환경·의존성·플러그인 연결

원칙: 여러 환경(Python 버전, OS)에서 동작하고 의존성을 낮춘다(2026-10-13 사용자 요구).

## 1. 실행 환경 (D3)

| 항목 | 결정 | 근거 |
|---|---|---|
| Python | 3.10 이상 공통 | `mcp` SDK 2.3.0의 `Requires-Python >=3.10` |
| SQLite | `pysqlite3` 필수(내장 SQLite 3.53.4). OS의 SQLite를 쓰지 않음 | 서버 기본 SQLite 3.26.0은 trigram 없음(1장 P7). `pysqlite3` 설치 후 trigram·JSON 동작 확인 |
| MCP | 공식 `mcp` SDK | MCP 규격 두 세대(`2026-07-28` 요청별 버전 방식, `2025-11-25` 이전 `initialize` 방식)를 SDK가 처리. 2.3.0: `LATEST_PROTOCOL_VERSION=2026-07-28` |
| OS | Linux, macOS, Windows | — |

### 1.1 `pysqlite3` 휠(미리 빌드된 설치 파일) 범위 — PyPI 0.6.1

| 플랫폼 | 휠 |
|---|---|
| Linux x86_64(glibc 2.28 이상, musl) | 있음 |
| macOS arm64(Apple Silicon) | 있음 |
| Windows 32·64비트 | 있음 |
| Linux aarch64, macOS Intel | **없음** → 소스 빌드 필요(실측 S5) |

## 2. 의존성

| 의존성 | 범위 | 비고 |
|---|---|---|
| `pysqlite3` | 0.6.x | 추가 의존성 0개 |
| `mcp` | 2.x | 직접 의존성 19개 |
| PyYAML | 제거 | 카탈로그는 JSON만(D2) |
| 개발 전용 | `uv` | 검증 매트릭스용 Python 설치에만 사용 |

설치 재현성: `setup`은 `constraints.txt`(CI에서 검증한 venv의 `pip freeze` 결과)를 적용한다(S-13 재현).

## 3. 설치와 시작 (D4)

```text
[설치] python <plugin>/src/docdb_launch.py setup [--remove]
         데이터 폴더(4절)에 venv 생성 → pip install -c constraints.txt mcp pysqlite3
         docdb_launch.py는 표준 라이브러리만 사용(설치 전에도 실행 가능)

[MCP]  설정된 인터프리터 → src/docdb_launch.py mcp
         ├ Python < 3.10              → stderr 안내, 종료 코드 2
         ├ venv 없음                  → stderr "setup 실행" 안내, 종료 코드 2
         ├ venv Python 버전 ≠ 현재    → stderr "setup 다시 실행", 종료 코드 2
         └ venv site-packages를 현재 프로세스에 추가(sysconfig로 OS별 경로 계산)
             → 기능 검사(FTS5 trigram, JSON) → MCP 서버 시작

[CLI]  bin/docctl                                   데이터 폴더 venv의 Python으로 -m docdb 실행
       python <plugin>/src/docdb_launch.py cli <명령>  bin/이 없는 환경(Codex, Windows)용
```

venv 실행 파일을 직접 띄우지 않고 현재 프로세스에 site-packages를 붙이는 이유: MCP 설정의 `command`는 OS별로 분기할 수 없고(venv 실행 파일이 `bin/python`과 `Scripts\python.exe`로 다름), Windows에서 자식 프로세스를 두면 stdio 중계와 종료 처리가 복잡해진다.

인터프리터 설정(설정 우선):

| 도구 | 설정 위치 | 기본값 |
|---|---|---|
| Claude Code | `userConfig.python_path` → MCP `command`에 `${user_config.python_path}` | `python3`. Windows는 `py` 또는 절대 경로 지정 |
| Codex | `codex.mcp.json`의 `command` | `python3`. `"cwd": "."`로 플러그인 루트에서 실행(S2 실측). Windows 처리는 S4 |

## 4. 데이터 폴더 (D4 구체화, 확정 D13)

D4는 "플러그인 데이터 폴더"로 합의했으나 아래 사실 때문에 **도구 공용 docdb 데이터 폴더**로 구체화했다(D13).

- Claude Code의 `${CLAUDE_PLUGIN_DATA}`는 MCP 서버에는 전달되지만 Bash 도구 환경에는 없다(공식 plugins-reference "Environment variables"). 에이전트가 셸에서 `setup`을 실행하면 MCP가 찾는 곳과 다른 위치에 설치된다.
- Codex 문서는 `PLUGIN_DATA`를 훅에 대해서만 설명한다.

해석 순서: `DOCDB_HOME` → OS 사용자 데이터 폴더.

| OS | 기본 위치 |
|---|---|
| Linux | `$XDG_DATA_HOME/docdb`, 없으면 `~/.local/share/docdb` |
| macOS | `~/Library/Application Support/docdb` |
| Windows | `%LOCALAPPDATA%\docdb` |

Claude와 Codex가 venv 하나를 공유한다. 플러그인을 제거해도 자동으로 지워지지 않으므로 `setup --remove`를 둔다.

## 5. DB 위치 (D5)

| 항목 | 내용 |
|---|---|
| 위치 | 프로젝트 폴더의 `.docdb/docdb.sqlite` |
| git | 제외, 로컬 전용. `docctl init`이 `.docdb/.gitignore`(내용 `*`)를 만들어 프로젝트 `.gitignore`를 고치지 않고 제외 |
| 근거 | SQLite 파일은 바이너리라 diff·병합이 안 됨. 원 결정 Q2(초기에는 DB만, 팀이 커지면 내보내기 추가)와 일치 |
| 탐색 | `DOCDB_PATH` → `DOCDB_START_DIR`(없으면 현재 폴더)에서 위로 올라가며 `.docdb/` 탐색 → 없으면 "`docctl init` 실행" 오류 |
| 초기화 | `docctl init`: 스키마·토큰 확장·카탈로그·시드 적재 |

## 6. 플러그인 연결 (D1)

### 6.1 구조 (안)

```text
j-ai-plugin/
  .claude-plugin/marketplace.json     docdb 항목 추가
  .agents/plugins/marketplace.json    docdb 항목 추가
  docdb/                              (D14: 코드는 docdb/, 문서는 docs-db/)
    .claude-plugin/plugin.json        mcpServers(내장) + userConfig.python_path
    .codex-plugin/plugin.json         skills + mcpServers → ./codex.mcp.json
    codex.mcp.json                    Claude가 루트 .mcp.json을 자동으로 읽지 않도록 이름 분리
    constraints.txt
    bin/docctl                        Claude Bash PATH용 sh 래퍼
    skills/docdb/SKILL.md
    src/docdb_launch.py               부트스트랩(표준 라이브러리만)
    src/docdb/                        db.py, cli.py, schema/*.sql, catalogue.py, llm.py,
                                      importer.py, relations.py, mcp_server.py
    catalogue/starter.json, catalogue/item_types.json
    tests/
```

| 단위 | 하는 일 | 의존 |
|---|---|---|
| `docdb_launch.py` | venv 탐색·생성, 버전 검사, 서버·CLI 시작 | 표준 라이브러리 |
| `db.py` | 유일한 DB 연결 지점(`pysqlite3`), `PRAGMA foreign_keys=ON`, 기능 검사 | `pysqlite3` |
| `llm.py` | 읽기·쓰기 6기능, 예산·digest·패치, 정책 필터 | `db.py` |
| `relations.py` | 추출 규칙 R1~R5, 확정 CLI, 필수 연결 검사 | `db.py` |
| `importer.py` | scan·next·label·report | `db.py`, `llm.py`, `relations.py` |
| `mcp_server.py` | 6기능을 MCP 도구로 노출 | `mcp`, `llm.py` |

CLI 명령: `setup [--remove]`, `init`, `catalogue load <json>`, `ctx|get|find|trace|since|put`, `link review|confirm|reject|remove`, `import scan|next|label|report`.

### 6.2 Claude Code 설정 (안)

```json
{
  "name": "docdb",
  "version": "0.1.0",
  "description": "Project document DB with token-minimal MCP tools.",
  "author": { "name": "PMT" },
  "mcpServers": {
    "docdb": {
      "command": "${user_config.python_path}",
      "args": ["${CLAUDE_PLUGIN_ROOT}/src/docdb_launch.py", "mcp"],
      "env": { "DOCDB_START_DIR": "${CLAUDE_PROJECT_DIR}" }
    }
  },
  "userConfig": {
    "python_path": {
      "type": "string",
      "title": "Python 3.10+ executable",
      "description": "Command or absolute path of a Python 3.10 or later interpreter. On Windows use py or an absolute path.",
      "default": "python3"
    }
  }
}
```

근거: 공식 plugins-reference의 `mcpServers` 내장 정의, `${CLAUDE_PLUGIN_ROOT}`·`${user_config.KEY}` 치환. `env`에서 `${CLAUDE_PROJECT_DIR}` 치환 여부는 실측 S3로 확인한다.

### 6.3 Codex 설정

`.codex-plugin/plugin.json`에 `skills: "./skills/"`, `mcpServers: "./codex.mcp.json"`. MCP 설정 안의 경로·작업 폴더·환경 변수 해석은 공식 문서에 없어 실측 S2([08](08-verification.md) 3.1절)로 확정했다: `"cwd": "."` + 상대 `args`, 프로젝트 폴더는 `env_vars`의 `PWD`로 받아 부트스트랩이 `DOCDB_START_DIR`로 옮긴다(D26). 정확한 JSON은 구현 계획 계약 C-26. Windows 명령은 S4로 확정한다.

### 6.4 스킬 (U-11)

`skills/docdb/SKILL.md` = 사용 규칙(원문 토큰 명세 부록 A) + 관계 제안 규칙([03](03-relations.md)) + import 절차([05](05-import.md)) + 설치 안내. MCP 서버 `instructions`는 원문 문구를 유지한다(Claude Code는 2,048자에서 자름).

## 7. 고정 규칙

- 파일 입출력은 항상 `encoding='utf-8'`. 저장 경로는 저장소 상대 POSIX만(S-08).
- 로그는 stderr로만. stdout은 MCP 프로토콜 전용.
- 시간은 UTC ISO 8601로 저장.
