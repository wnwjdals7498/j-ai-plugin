# forge-ledger 1부 작성방식 프로토타입 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Claude Code 플러그인 forge-ledger의 1부(작성방식) 프로토타입을 만든다. 저장소에 `init`과 `plan new` 두 번을 실행하면 Plane 프로젝트 3개(유지보수 1 + 단위 2)가 붙고, 계획서 항목을 채운 뒤 `plan check`가 통과한다.

**Architecture:** 플러그인 안의 Python 패키지 `ledger/`가 원천 로직이다(나중에 허브가 같은 패키지를 쓴다). CLI `scripts/ledger.py`가 Plane CE 공개 API v1과 저장소 `.ledger/`를 읽고 쓴다. 단위 프로젝트 계획서는 Plane 작업 항목 본문(HTML)에 절(`<h2>`) 단위로 두고, 저장소 범위 문서는 `.ledger/` Markdown에 둔다. 스킬 `ledger-plan`이 질문 순서와 CLI 사용법을 Claude에게 알려 주고, 훅이 비밀값 쓰기를 막는다.

**Tech Stack:** Python 3.9 표준 라이브러리(런타임), unittest, 개발용 ruff 0.6.9·mypy 1.11.2(.venv), Plane CE v1.5.0, Claude Code 플러그인(skills·commands·hooks·.mcp.json·userConfig).

**Spec:** `docs/design/forge-ledger-design.html` (원본 `~/docs/projects/forge-wiki-plugin/resources/derived/v4/forge-ledger-design-v4.html`, 설계서 v4.2). 이 계획의 범위는 설계서 구현 7 "프로토타입" 행의 1부와 비밀값 차단 훅이다. 허브 골격(2부)은 다음 계획에서 다룬다.

## Global Constraints

- 런타임은 Python 3.9 표준 라이브러리만 쓴다. 모든 모듈 첫 줄 다음에 `from __future__ import annotations`. `match`, 런타임 `X | Y` 타입, `tomllib`는 쓰지 않는다.
- Plane 호출은 CE v1.5.0 공개 API `/api/v1/workspaces/{slug}/...`만, 인증 헤더 `X-API-Key`. 자체 제한 분당 50회 이하(NFR-04).
- 계획서 항목은 작업 항목이다. module 이름 `계획서`, 라벨 이름 `계획서`, 라벨 색 `#6b7280`. 항목 이름은 `ledger/spec.py`의 `UNIT_ITEMS` 값 그대로(D20).
- 계획서 항목 본문을 바꿀 때는 쓰기 직전에 `updated_at`을 다시 읽어, 읽은 뒤 바뀌었으면 쓰지 않는다(NFR-08).
- 토큰·IP 주소를 `.ledger/`, Plane, 출력, 로그에 쓰지 않는다(G5, NFR-01). 비밀값 판정은 `ledger/secrets.py` 하나로 한다.
- 훅은 실패해도 통과한다(fail-open). 훅이 막는 것은 비밀값 쓰기 하나뿐이다(D7).
- `.ledger/history.md` 한 줄 형식: `날짜 | 종류 | ID | Plane 프로젝트 | 제목 | 상세@수정 시각`, 종류는 `decision|phase|release|override`, 결정 ID는 `D-YYMMDD-xxxx`(소문자 16진수 4자리).
- 작성방식 규격 버전 `method_version: 1`.
- 사용자에게 보이는 메시지·문서는 한국어. 코드 식별자는 영어.
- Plane 프로젝트 identifier는 영문 대문자·숫자만, 최대 12자(Plane `FORBIDDEN_IDENTIFIER_CHARS_PATTERN`, `max_length=12`).
- push·배포는 사용자가 따로 요청하기 전에는 하지 않는다. 커밋만 한다.
- 각 Task의 "Commit" 단계는 `git add -A && git commit -m "<적힌 메시지>"`를 뜻한다. 커밋 전에 `bash scripts/dev-check.sh`가 통과해야 한다(Task 1 이후).

## Review Focus

1. Plane이 저장할 때 본문 HTML을 정리해 `<h2>`에 속성을 붙이거나 `<p>`로 감싸도 절 나누기가 깨지지 않아야 한다 → Task 6 `test_split_survives_plane_sanitizing`.
2. 저장소 이름이 한글·기호뿐이면 유지보수 프로젝트 키가 빈 문자열이 되면 안 된다 → Task 10 `test_maintenance_key_fallback`.
3. 이미 붙은 키로 `plan new`를 다시 실행하면 Plane에 아무것도 만들지 않고 명확한 오류를 내야 하며 `ledger.yml`에 중복이 없어야 한다 → Task 10 `test_plan_new_rejects_existing_key`.
4. 사람이 `ledger.yml`에 모르는 키를 넣으면, 저장할 때 조용히 사라지지 않고 불러올 때 그 키 이름으로 오류가 나야 한다 → Task 2 `test_unknown_key_is_error`.
5. 토큰이 만료되거나 틀리면(401·403) 계획서 쓰기가 반쯤 된 상태로 끝나지 않고 종료 코드 2와 "토큰 확인" 안내가 나와야 한다 → Task 11 `test_cli_auth_error_exit_2`.

---

## File Structure

```
forge-ledger/                                   저장소 루트 = 마켓플레이스 루트
  .claude-plugin/marketplace.json
  .gitignore
  pyproject.toml                                ruff·mypy 설정만(런타임 의존성 없음)
  scripts/dev-check.sh                          lint·typecheck·test 한 번에
  docs/design/forge-ledger-design.html          설계서 사본(spec)
  docs/superpowers/plans/                       이 계획
  plugins/forge-ledger/
    .claude-plugin/plugin.json                  userConfig 4개
    .mcp.json                                   Plane MCP(http, PAT 헤더)
    hooks/hooks.json
    hooks/session_start.py                      userConfig → CLAUDE_ENV_FILE, 한 줄 요약
    hooks/pre_tool_use.py                       비밀값 쓰기 차단
    commands/plan.md · commands/decide.md
    skills/ledger-plan/SKILL.md
    skills/ledger-plan/references/docs.md       spec.py에서 생성
    skills/ledger-plan/references/flows.md · gates.md · review-prompt.md
    skills/ledger-plan/templates/plane/*.html   계획서 항목 본문 6종
    skills/ledger-plan/templates/ledger/*       .ledger/ 뼈대
    skills/ledger-plan/examples/forge-ledger-design.html
    ledger/__init__.py
    ledger/miniyaml.py      ledger.yml 하위 집합 파서
    ledger/config.py        LedgerConfig 읽기·쓰기
    ledger/history.py       history.md 읽기·한 줄 추가·결정 ID
    ledger/secrets.py       비밀값 판정
    ledger/spec.py          문서 18종 규격(데이터)
    ledger/sections.py      HTML·Markdown 절 나누기·바꾸기·상태
    ledger/check.py         형식 검사기
    ledger/plane.py         Plane API 클라이언트
    ledger/flows.py         init · plan new · get · put · decide
    ledger/cli.py           argparse 진입점
    scripts/ledger.py       CLI 실행 파일
  tests/
    fakes.py                FakePlane(메모리), FakePlaneServer(로컬 HTTP)
    test_*.py
    e2e/run_p1_e2e.py       실서버 끝단 시험(Task 14)
```

---

### Task 1: 저장소 뼈대, 개발 도구, 플러그인 매니페스트

**Files:**
- Create: `.gitignore`, `pyproject.toml`, `scripts/dev-check.sh`, `.claude-plugin/marketplace.json`, `plugins/forge-ledger/.claude-plugin/plugin.json`, `plugins/forge-ledger/ledger/__init__.py`, `docs/design/forge-ledger-design.html`
- Test: `tests/__init__.py`, `tests/test_layout.py`

**Interfaces:**
- Produces: 패키지 경로 `plugins/forge-ledger/ledger`. 테스트는 `tests/__init__.py`에서 `sys.path`에 `plugins/forge-ledger`를 넣어 `import ledger`로 쓴다.
- Produces: userConfig 키 `plane_url`, `plane_workspace`, `plane_api_key`, `plane_mcp_url`.

- [ ] **Step 1: 저장소 만들기**

```bash
cd /data/workspace/github/forge-ledger && git init -b main
cp ~/docs/projects/forge-wiki-plugin/resources/derived/v4/forge-ledger-design-v4.html docs/design/forge-ledger-design.html
python3 -m venv .venv && .venv/bin/pip install ruff==0.6.9 mypy==1.11.2
```
`.gitignore`: `.venv/`, `__pycache__/`, `*.pyc`, `.mypy_cache/`, `.ruff_cache/`.

- [ ] **Step 2: 실패하는 테스트 작성** `tests/test_layout.py`

```python
def test_plugin_manifest_user_config(self):
    m = json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text())
    self.assertEqual(m["name"], "forge-ledger")
    uc = m["userConfig"]
    self.assertEqual(set(uc), {"plane_url", "plane_workspace", "plane_api_key", "plane_mcp_url"})
    self.assertTrue(uc["plane_api_key"]["sensitive"])
    self.assertEqual(uc["plane_workspace"]["default"], "forge")
    for v in uc.values():
        self.assertTrue({"type", "title", "description"} <= set(v))

def test_marketplace_points_to_plugin(self):
    mk = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    self.assertEqual(mk["plugins"][0]["source"], "./plugins/forge-ledger")
```

- [ ] **Step 3: 실행해 실패 확인**

Run: `python3 -m unittest tests.test_layout -v` → FAIL(파일 없음)

- [ ] **Step 4: 매니페스트와 설정 작성**

`plugin.json`: `name` `forge-ledger`, `version` `0.1.0`, `description` "작업 계획서 작성방식으로 프로젝트를 이끌고 Plane·Gitea·Penpot을 잇는 플러그인". userConfig는 모두 `type: "string"`: `plane_url`(required, title "Plane 주소"), `plane_workspace`(default `"forge"`), `plane_api_key`(required, sensitive), `plane_mcp_url`(required, title "Plane MCP 주소", description에 `/http/api-key/mcp`로 끝난다고 적음).
`marketplace.json`: `name` `forge`, `owner.name` `forge`, plugins 1개(`name` `forge-ledger`, `source` `./plugins/forge-ledger`).
`pyproject.toml`: `[tool.ruff] target-version = "py39"`, `line-length = 120`; `[tool.mypy] python_version = "3.9"`, `strict = true`, `mypy_path = "plugins/forge-ledger"`, `files = ["plugins/forge-ledger/ledger", "plugins/forge-ledger/hooks"]`.
`scripts/dev-check.sh`: `set -e`, `.venv/bin/ruff check .`, `.venv/bin/mypy`, `python3 -m unittest discover -s tests -t . -v`, 마지막에 `claude plugin validate .`.

- [ ] **Step 5: 통과 확인**

Run: `bash scripts/dev-check.sh` → 테스트 OK, 마지막 줄 `✔ Validation passed`

- [ ] **Step 6: Commit**

```bash
git add -A && git commit -m "chore: 저장소 뼈대, 개발 도구, 플러그인 매니페스트"
```

---

### Task 2: ledger.yml 읽기·쓰기

**Files:**
- Create: `plugins/forge-ledger/ledger/miniyaml.py`, `plugins/forge-ledger/ledger/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `miniyaml.loads(text: str) -> dict[str, Any]`, `class MiniYamlError(ValueError)`.
- Produces: `@dataclass PlaneProject(key: str, title: str, default: bool = False)`, `@dataclass LedgerConfig(method_version: int, workspace: str, projects: list[PlaneProject], screens_tool: str = "penpot", screens_team: str = "", screens_files: list[str] = [], hub: str = "")`, `LedgerConfig.default_project() -> PlaneProject`(없으면 `LookupError`), `load_config(path: Path) -> LedgerConfig`, `render_config(cfg: LedgerConfig) -> str`, `save_config(cfg: LedgerConfig, path: Path) -> None`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
SAMPLE = """# 비밀값과 접속 주소는 넣지 않는다
method_version: 1              # 작성방식 규격 버전
plane:
  workspace: forge
  projects:
    - key: SAMPM
      title: sample-app 유지보수
      default: true
    - key: APP
      title: 앱 첫 출시
screens:
  tool: penpot
  team: design
  files: ["f1", "f2"]
hub: dev-hub
"""
def test_load_sample(self):
    cfg = load_config_text(SAMPLE)
    self.assertEqual(cfg.workspace, "forge")
    self.assertEqual([p.key for p in cfg.projects], ["SAMPM", "APP"])
    self.assertEqual(cfg.default_project().key, "SAMPM")
    self.assertEqual(cfg.screens_files, ["f1", "f2"])

def test_round_trip(self):
    cfg = load_config_text(SAMPLE)
    self.assertEqual(load_config_text(render_config(cfg)), cfg)
    self.assertIn("# 비밀값과 접속 주소는 넣지 않는다", render_config(cfg))

def test_unknown_key_is_error(self):
    with self.assertRaisesRegex(MiniYamlError, "plane.colour"):
        load_config_text(SAMPLE.replace("  workspace: forge", "  workspace: forge\n  colour: red"))
```
`load_config_text(text)`는 테스트 도우미로 `config.py`에 둔다(`load_config`가 이것을 부른다).

- [ ] **Step 2: 실행해 실패 확인** — Run: `python3 -m unittest tests.test_config -v` → FAIL(import 오류)

- [ ] **Step 3: 구현**

`miniyaml.loads`: 줄 단위. `#` 뒤 주석 제거(따옴표 안 `#`은 유지), 2칸 들여쓰기 맵, `- key: v` 목록 안 맵, `["a", "b"]` 흐름 목록, 스칼라는 `true/false`→bool, 정수→int, 나머지 문자열(감싼 따옴표 제거). 탭·홀수 들여쓰기는 줄 번호를 담아 `MiniYamlError`.
`config.py`: 허용 키 집합(`method_version`, `plane.workspace`, `plane.projects[].key|title|default`, `screens.tool|team|files`, `hub`) 밖의 키는 점 경로(`plane.colour`)를 담아 `MiniYamlError`. `render_config`는 설계서 구현 1 예시와 같은 순서·주석으로 고정 출력한다.

- [ ] **Step 4: 통과 확인** — Run: `python3 -m unittest tests.test_config -v` → PASS

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat: ledger.yml 읽기·쓰기"`

---

### Task 3: history.md 읽기·추가·결정 ID

**Files:**
- Create: `plugins/forge-ledger/ledger/history.py`
- Test: `tests/test_history.py`

**Interfaces:**
- Produces: `KINDS = ("decision", "phase", "release", "override")`, `HEADER = "날짜 | 종류 | ID | Plane 프로젝트 | 제목 | 상세@수정 시각"`, `@dataclass(frozen=True) HistoryEntry(date: str, kind: str, ref_id: str, projects: str, title: str, detail: str)`, `parse_history(text: str) -> list[HistoryEntry]`, `format_entry(e: HistoryEntry) -> str`, `append_entry(path: Path, e: HistoryEntry) -> None`, `new_decision_id(today: datetime.date, existing: set[str], rand4: Callable[[], str]) -> str`, `empty_history() -> str`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_format_and_parse(self):
    e = HistoryEntry("2026-11-23", "decision", "D-261123-7f3a", "APP", "위키는 생성 구역만 쓴다", "plane:APP-3@2026-11-23T14:02Z")
    line = format_entry(e)
    self.assertEqual(line, "2026-11-23 | decision | D-261123-7f3a | APP | 위키는 생성 구역만 쓴다 | plane:APP-3@2026-11-23T14:02Z")
    self.assertEqual(parse_history(empty_history() + line + "\n"), [e])

def test_crlf_and_header_skipped(self):
    text = empty_history().replace("\n", "\r\n") + "2026-12-05 | phase | proto | APP | 프로토타입 종료 | -\r\n"
    self.assertEqual(parse_history(text)[0].kind, "phase")

def test_pipe_in_title_rejected(self):
    with self.assertRaises(ValueError):
        format_entry(HistoryEntry("2026-11-23", "decision", "D-261123-0000", "*", "a | b", "-"))

def test_unknown_kind_rejected(self):
    with self.assertRaises(ValueError):
        format_entry(HistoryEntry("2026-11-23", "note", "x", "*", "t", "-"))

def test_decision_id_retries_on_collision(self):
    seq = iter(["7f3a", "7f3a", "0b1c"])
    got = new_decision_id(date(2026, 11, 23), {"D-261123-7f3a"}, lambda: next(seq))
    self.assertEqual(got, "D-261123-0b1c")
```

- [ ] **Step 2: 실행해 실패 확인** — `python3 -m unittest tests.test_history -v` → FAIL

- [ ] **Step 3: 구현** — `empty_history()`는 `"# 이력\n\n" + HEADER + "\n"`. 파싱은 ` | `로 나눠 칸이 6개인 줄만, 머리줄 제외, 각 칸 strip. `append_entry`는 파일이 없으면 `empty_history()`로 만든 뒤 한 줄 추가. 기본 `rand4`는 `secrets.token_hex(2)`.

- [ ] **Step 4: 통과 확인** — PASS

- [ ] **Step 5: Commit** — `feat: history.md 형식과 결정 ID`

---

### Task 4: 비밀값 판정

**Files:**
- Create: `plugins/forge-ledger/ledger/secrets.py`
- Test: `tests/test_secrets.py`

**Interfaces:**
- Produces: `PATTERNS: dict[str, str]`, `find_secrets(text: str) -> list[str]`(걸린 패턴 이름, 정렬·중복 제거).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_detects_each_pattern(self):
    cases = {
        "ipv4": "server 10.1.2.3 down",
        "plane_token": "plane_api_" + "a" * 32,
        "github_token": "ghp_" + "A" * 36,
        "gitlab_token": "glpat-" + "x" * 20,
        "figma_token": "figd_" + "y" * 40,
        "telegram_bot_token": "123456789:" + "Z" * 35,
    }
    for name, text in cases.items():
        self.assertEqual(find_secrets(text), [name], name)

def test_loopback_and_any_allowed(self):
    self.assertEqual(find_secrets("http://127.0.0.1:18090 0.0.0.0"), [])

def test_plain_text_clean(self):
    self.assertEqual(find_secrets("D-261123-7f3a 버전 1.5.0 APP-12"), [])
```

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 구현** — 패턴 값: `ipv4` `\b(?:\d{1,3}\.){3}\d{1,3}\b`(127.0.0.1, 0.0.0.0 제외), `plane_token` `plane_api_[0-9a-f]{32}`, `github_token` `ghp_[A-Za-z0-9]{20,}`, `gitlab_token` `glpat-[A-Za-z0-9_-]{20,}`, `figma_token` `figd_[A-Za-z0-9_-]{20,}`, `telegram_bot_token` `\b\d{8,10}:[A-Za-z0-9_-]{35}\b`.
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `feat: 비밀값 판정`

---

### Task 5: 문서 18종 규격 데이터와 references/docs.md

**Files:**
- Create: `plugins/forge-ledger/ledger/spec.py`, `plugins/forge-ledger/skills/ledger-plan/references/docs.md`
- Test: `tests/test_spec.py`

**Interfaces:**
- Produces: `METHOD_VERSION = 1`, `@dataclass(frozen=True) DocSpec(code: str, title: str, scope: str, container: str, na_allowed: bool, first_question: str, check: str)`, `DOCS: tuple[DocSpec, ...]`(PRD1…PRD9, IMPL1…IMPL9 순서), `DOC_BY_CODE: dict[str, DocSpec]`, `UNIT_ITEMS: dict[str, str]`, `REPO_FILES: dict[str, str]`, `DRAWIO: dict[str, str]`, `FEATURE_PRD_REQUIRED = ("PRD1", "PRD2", "PRD6", "PRD8", "PRD9")`, `section_heading(code: str) -> str`, `render_docs_reference() -> str`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_eighteen_docs_in_order(self):
    self.assertEqual([d.code for d in DOCS], [f"PRD{i}" for i in range(1, 10)] + [f"IMPL{i}" for i in range(1, 10)])

def test_scopes_and_na(self):
    unit = {d.code for d in DOCS if d.scope == "unit"}
    self.assertEqual(unit, {f"PRD{i}" for i in range(1, 10)} | {"IMPL2", "IMPL3", "IMPL5", "IMPL7"})
    na = {d.code for d in DOCS if d.na_allowed}
    self.assertEqual(na, {"PRD7", "PRD8", "IMPL1", "IMPL2", "IMPL3", "IMPL4", "IMPL5", "IMPL8", "IMPL9"})

def test_containers(self):
    self.assertEqual(UNIT_ITEMS, {
        "PRD": "[계획서] PRD", "IMPL2": "[계획서] 구현 2 기능 레퍼런스", "IMPL3": "[계획서] 구현 3 화면 레퍼런스",
        "IMPL5": "[계획서] 구현 5 알림 처리", "IMPL7": "[계획서] 구현 7 단계별 범위", "DECISION": "[계획서] 결정"})
    self.assertEqual(REPO_FILES["IMPL6"], ".ledger/design/security.md")
    self.assertEqual(DRAWIO, {"IMPL1": ".ledger/design/data-model.drawio", "IMPL4": ".ledger/design/logging-flow.drawio"})

def test_headings(self):
    self.assertEqual(section_heading("PRD2"), "PRD 2 목표 · 성공 지표")
    self.assertEqual(section_heading("IMPL6"), "구현 6 인증 · 보안 처리")

def test_reference_in_sync(self):
    self.assertEqual(REF_DOCS.read_text(encoding="utf-8"), render_docs_reference())
```

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 구현** — 제목·첫 질문·검사 문장은 설계서 규격 1 표를 그대로 옮긴다(제목: PRD 1 문서 머리, PRD 2 목표 · 성공 지표, PRD 3 배경 · 환경 · 지원 범위, PRD 4 전략 · 선정 이유, PRD 5 가정, PRD 6 사용자 스토리 · 요구사항, PRD 7 목업 · 다이어그램, PRD 8 미결 질문, PRD 9 제외 범위, 구현 1 데이터 보관 설계, 구현 2 기능 레퍼런스, 구현 3 화면 레퍼런스, 구현 4 로깅 처리 레퍼런스, 구현 5 알림 처리, 구현 6 인증 · 보안 처리, 구현 7 단계별 범위, 구현 8 유지보수, 구현 9 배포). `container`는 PRD1–9 → `"PRD"`, IMPL2/3/5/7 → 자기 코드, 저장소 문서 → `REPO_FILES` 경로. `REPO_FILES`: IMPL1 `.ledger/design/data.md`, IMPL4 `.ledger/design/logging.md`, IMPL6 `.ledger/design/security.md`, IMPL8 `.ledger/ops/maintenance.md`, IMPL9 `.ledger/ops/deploy.md`. `render_docs_reference()`는 Markdown 표(문서 | 담는 것 | 첫 질문 | 해당 없음 | 위치 | 검사) 하나를 내고, 그 출력을 `references/docs.md`에 저장한다.
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `feat: 문서 18종 규격 데이터`

---

### Task 6: 절 나누기·바꾸기·상태 판정

**Files:**
- Create: `plugins/forge-ledger/ledger/sections.py`
- Test: `tests/test_sections.py`

**Interfaces:**
- Consumes: `spec.section_heading`, `spec.DOC_BY_CODE`.
- Produces: `split_html_sections(html: str) -> dict[str, str]`(키 = 문서 코드 또는 결정 ID, 값 = 다음 `<h2>` 전까지의 본문 HTML), `split_md_sections(md: str) -> dict[str, str]`(`## ` 머리), `replace_html_section(html: str, code: str, body: str) -> str`(없으면 `KeyError`), `append_html_section(html: str, heading: str, body: str) -> str`, `section_state(body: str, na_allowed: bool) -> tuple[str, list[str]]`(상태 `written|na|missing`), `html_table_rows(body: str) -> list[list[str]]`(머리 행 제외 셀 텍스트).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_split_and_replace(self):
    html = "<h2>PRD 1 문서 머리</h2><p>a</p><h2>PRD 2 목표 · 성공 지표</h2><p>b</p>"
    self.assertEqual(split_html_sections(html), {"PRD1": "<p>a</p>", "PRD2": "<p>b</p>"})
    out = replace_html_section(html, "PRD2", "<p>c</p>")
    self.assertEqual(split_html_sections(out)["PRD2"], "<p>c</p>")
    self.assertEqual(split_html_sections(out)["PRD1"], "<p>a</p>")

def test_split_survives_plane_sanitizing(self):
    html = '<h2 class="editor-heading-block">PRD 9 제외 범위</h2><ul class="list"><li><p>x</p></li></ul>'
    self.assertIn("PRD9", split_html_sections(html))

def test_decision_heading_key(self):
    html = append_html_section("", "D-261123-7f3a 위키는 생성 구역만", "<p>본문</p>")
    self.assertIn("D-261123-7f3a", split_html_sections(html))

def test_states(self):
    self.assertEqual(section_state("<p>{{목표}}</p>", True)[0], "missing")
    self.assertEqual(section_state("", True)[0], "missing")
    self.assertEqual(section_state("<p>해당 없음: 화면 없는 일</p>", True), ("na", []))
    self.assertEqual(section_state("<p>해당 없음: </p>", True)[0], "missing")
    self.assertEqual(section_state("<p>해당 없음: 이유</p>", False)[0], "missing")
    self.assertEqual(section_state("<p>내용</p>", False), ("written", []))

def test_md_sections(self):
    md = "# 데이터\n\n## 구현 1 데이터 보관 설계\n본문\n"
    self.assertEqual(split_md_sections(md)["IMPL1"].strip(), "본문")
```

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 구현** — `<h2[^>]*>(.*?)</h2>`로 머리를 찾고, 태그를 뗀 머리 글자가 `^(PRD|구현) (\d)`면 `PRD{n}`/`IMPL{n}`, `^(D-\d{6}-[0-9a-f]{4})`면 결정 ID를 키로 쓴다. 상태 판정은 태그를 뗀 글자로 한다: 비었거나 `{{` 포함 → missing, `^해당 없음:\s*(\S.*)$` → 허용이면 na, 아니면 missing("해당 없음 불가"), 이유가 비면 missing("이유 없음").
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `feat: 계획서 절 나누기와 상태 판정`

---

### Task 7: 계획서 템플릿

**Files:**
- Create: `skills/ledger-plan/templates/plane/prd.html`, `ref-feature.html`, `ref-screen.html`, `ref-alert.html`, `phases.html`, `decision.html`; `skills/ledger-plan/templates/ledger/design/data.md`, `design/logging.md`, `design/security.md`, `design/data-model.drawio`, `design/logging-flow.drawio`, `ops/maintenance.md`, `ops/deploy.md` (모두 `plugins/forge-ledger/` 아래. 하위 경로는 `.ledger/` 안 경로와 같다. `ledger.yml`·`history.md`는 템플릿 없이 코드가 만든다)
- Modify: `plugins/forge-ledger/ledger/spec.py`(`TEMPLATE_FOR` 추가)
- Test: `tests/test_templates.py`

**Interfaces:**
- Consumes: `spec.DOCS`, `spec.UNIT_ITEMS`, `sections.split_html_sections`, `sections.split_md_sections`, `sections.section_state`.
- Produces: 템플릿 파일 이름 ↔ 컨테이너 대응 `TEMPLATE_FOR = {"PRD": "prd.html", "IMPL2": "ref-feature.html", "IMPL3": "ref-screen.html", "IMPL5": "ref-alert.html", "IMPL7": "phases.html", "DECISION": "decision.html"}`(`spec.py`에 둔다).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
ALLOWED = {"h1", "h2", "h3", "p", "strong", "em", "code", "pre", "ul", "ol", "li", "table", "tbody", "thead", "tr", "td", "th", "a", "hr", "blockquote", "br"}

def test_every_unit_doc_has_a_section(self):
    for d in DOCS:
        if d.scope == "unit":
            html = (PLANE_T / TEMPLATE_FOR[d.container]).read_text(encoding="utf-8")
            self.assertIn(d.code, split_html_sections(html), d.code)

def test_every_repo_doc_has_a_section(self):
    for code, rel in REPO_FILES.items():
        md = (LEDGER_T / Path(rel).relative_to(".ledger")).read_text(encoding="utf-8")
        self.assertIn(code, split_md_sections(md), code)

def test_templates_start_missing(self):
    html = (PLANE_T / "prd.html").read_text(encoding="utf-8")
    for code, body in split_html_sections(html).items():
        self.assertEqual(section_state(body, True)[0], "missing", code)

def test_only_plane_tags(self):
    for f in PLANE_T.glob("*.html"):
        tags = set(re.findall(r"</?([a-z0-9]+)", f.read_text(encoding="utf-8")))
        self.assertLessEqual(tags, ALLOWED, f.name)

def test_drawio_templates_parse(self):
    for name in ("design/data-model.drawio", "design/logging-flow.drawio"):
        ET.parse(LEDGER_T / name)
```

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 템플릿 작성** — 절마다 `<h2>{section_heading(code)}</h2>` 다음에 `{{…}}` 자리표시자를 둔다. 검사기가 읽는 구조를 고정한다: PRD1은 표 행 `결정권자`·`진행 상태`·`목표 출시일`(2칸), PRD2는 표 머리 `목표 | 성공 지표 | 확인 방법`, PRD8은 표 머리 `ID | 질문 | 영향 | 기본값 | 필요 시점`, PRD9는 `<ul>`, 구현 7은 표 머리 `단계 | 만드는 것 | 완료 기준 | 갱신할 구현 문서`. `decision.html`은 `<h1>결정 상세</h1>`만. drawio 두 개는 빈 `<mxfile>`에 셀 2개(`id="0"`, `id="1"`).
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `feat: 계획서 템플릿`

---

### Task 8: 형식 검사기

**Files:**
- Create: `plugins/forge-ledger/ledger/check.py`, `tests/fakes.py`(`DictSource`, `RaisingSource`), `tests/fixtures/filled/`(채운 계획서 항목 본문 5개 `PRD.html`·`IMPL2.html`·`IMPL3.html`·`IMPL5.html`·`IMPL7.html`, 채운 저장소 문서 `.ledger/` 사본)
- Test: `tests/test_check.py`

**Interfaces:**
- Consumes: `spec`, `sections`, `secrets.find_secrets`.
- Produces: `@dataclass DocResult(scope: str, code: str, state: str, reasons: list[str])`(scope는 프로젝트 키 또는 `"repo"`), `@dataclass CheckReport(results: list[DocResult], exit_code: int)`, `CheckReport.to_json() -> str`(`{"method_version": 1, "exit_code": n, "results": [{"scope", "code", "state", "reasons"}]}`), `class UnitDocSource(Protocol): def get_container_html(self, project_key: str, container: str) -> Optional[str]`, `class SourceUnavailable(Exception)`, `doc_rules(code: str, body: str, repo_root: Optional[Path]) -> list[str]`, `check_unit(project_key: str, source: UnitDocSource) -> list[DocResult]`, `check_repo(repo_root: Path) -> list[DocResult]`, `run_check(repo_root: Path, project_keys: list[str], source: UnitDocSource) -> CheckReport`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_filled_project_passes(self):
    src = DictSource({("APP", c): FILLED[c] for c in ("PRD", "IMPL2", "IMPL3", "IMPL5", "IMPL7")})
    rep = run_check(self.filled_repo, ["APP"], src)
    self.assertEqual(rep.exit_code, 0, rep.to_json())
    self.assertEqual(len(rep.results), 18)

def test_template_fails(self):
    rep = run_check(self.template_repo, ["APP"], DictSource.from_templates("APP"))
    self.assertEqual(rep.exit_code, 1)
    self.assertTrue(all(r.state == "missing" for r in rep.results))

def test_missing_container_is_missing(self):
    rep = run_check(self.filled_repo, ["APP"], DictSource({}))
    self.assertEqual({r.state for r in rep.results if r.scope == "APP"}, {"missing"})

def test_rules(self):
    self.assertIn("확인 방법 빈칸", doc_rules("PRD2", "<table><tr><th>목표</th><th>지표</th><th>확인 방법</th></tr><tr><td>G1</td><td>x</td><td></td></tr></table>", None))
    self.assertIn("진행 상태 값", doc_rules("PRD1", "<table><tr><td>결정권자</td><td>a</td></tr><tr><td>진행 상태</td><td>모름</td></tr><tr><td>목표 출시일</td><td>미정</td></tr></table>", None))
    self.assertIn("제외 항목 없음", doc_rules("PRD9", "<p>없다</p>", None))
    self.assertIn("고르지 않은 안 없음", doc_rules("PRD4", "<p>A안</p>", None))

def test_secret_fails(self):
    src = DictSource({("APP", "PRD"): FILLED["PRD"].replace("미정", "10.1.2.3")})
    rep = run_check(self.filled_repo, ["APP"], src)
    self.assertIn("비밀값: ipv4", rep.to_json())

def test_unavailable_exit_2(self):
    rep = run_check(self.filled_repo, ["APP"], RaisingSource())
    self.assertEqual(rep.exit_code, 2)
```
`FILLED`는 `tests/fixtures/filled/`의 채운 본문(설계서 v4.2의 해당 절을 줄여 옮긴 것), `filled_repo`·`template_repo`는 `setUp`에서 임시 폴더에 만든 `.ledger/`다. `DictSource`·`RaisingSource`는 `tests/fakes.py`.

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 구현** — 문서별 규칙(메시지 고정): PRD1 `"진행 상태 값"`(초안·검토·승인·진행·종료 밖), `"필드 없음: <이름>"`; PRD2 `"확인 방법 빈칸"`; PRD4 `"고르지 않은 안 없음"`; PRD5 `"구분 없음: <사용자|기술|사업>"`; PRD8(na가 아닐 때) `"기본값 빈칸"`; PRD9 `"제외 항목 없음"`; IMPL1·IMPL4 `"drawio 파싱 실패"`(`spec.DRAWIO` 파일); IMPL6 `"위협 없음"`; IMPL7 `"완료 기준 빈칸"`; IMPL8 `"결함 흐름 없음"`; IMPL9 `"되돌리기 없음"`. 모든 절 공통으로 `find_secrets` 결과가 있으면 `"비밀값: <이름들>"`. 종료 코드: 원천을 못 읽으면 2, 상태가 missing이거나 reasons가 있는 결과가 하나라도 있으면 1, 아니면 0. `check_repo`는 `.ledger/` 파일이 없으면 그 문서를 missing으로 낸다.
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `feat: 계획서 형식 검사기`

---

### Task 9: Plane API 클라이언트

**Files:**
- Create: `plugins/forge-ledger/ledger/plane.py`
- Modify: `tests/fakes.py`(FakePlaneServer 추가)
- Test: `tests/test_plane.py`

**Interfaces:**
- Produces: `class PlaneError(Exception)`, `class PlaneUnavailable(PlaneError)`, `class PlaneAuthError(PlaneError)`, `class PlaneConflict(PlaneError)`, `@dataclass PlaneSettings(base_url: str, workspace: str, token: str)`, `PlaneSettings.from_env(env: Mapping[str, str]) -> PlaneSettings`(키 `FORGE_LEDGER_PLANE_URL`, `FORGE_LEDGER_PLANE_WORKSPACE`, `FORGE_LEDGER_PLANE_TOKEN`; 빠지면 `PlaneError("설정 없음: <키>")`).
- Produces: `class PlaneClient`: `__init__(settings, per_minute: int = 50, clock=time.monotonic, sleep=time.sleep)`, `list_projects() -> list[dict]`, `create_project(name: str, identifier: str) -> dict`, `delete_project(project_id: str) -> None`, `create_label(project_id, name, color) -> dict`, `create_module(project_id, name) -> dict`, `add_to_module(project_id, module_id, item_ids: list[str]) -> None`, `create_cycle(project_id, name) -> dict`, `create_item(project_id, name, description_html, label_ids: list[str]) -> dict`, `list_items(project_id) -> list[dict]`, `get_item(project_id, item_id) -> dict`, `update_item_description(project_id, item_id, html, expected_updated_at: str) -> dict`.
- 이후 Task는 이 메서드 이름을 쓰는 `PlaneLike` Protocol(같은 파일)에 기댄다. `tests/fakes.py`의 `FakePlane`도 이 Protocol을 따른다.

- [ ] **Step 1: 실패하는 테스트 작성** (FakePlaneServer는 `http.server`를 스레드로 띄워 받은 요청을 기록)

```python
def test_create_project_request(self):
    self.client.create_project("앱 첫 출시", "APP")
    req = self.server.requests[-1]
    self.assertEqual((req.method, req.path), ("POST", "/api/v1/workspaces/forge/projects/"))
    self.assertEqual(req.headers["X-API-Key"], "plane_api_" + "0" * 32)
    self.assertEqual(req.json, {"name": "앱 첫 출시", "identifier": "APP", "cycle_view": True, "module_view": True})

def test_add_to_module_payload(self):
    self.client.add_to_module("p1", "m1", ["i1", "i2"])
    req = self.server.requests[-1]
    self.assertEqual(req.path, "/api/v1/workspaces/forge/projects/p1/modules/m1/module-issues/")
    self.assertEqual(req.json, {"issues": ["i1", "i2"]})

def test_list_items_follows_cursor(self):
    self.server.pages = [{"results": [{"id": "a"}], "next_cursor": "c2", "next_page_results": True},
                         {"results": [{"id": "b"}], "next_cursor": None, "next_page_results": False}]
    self.assertEqual([i["id"] for i in self.client.list_items("p1")], ["a", "b"])

def test_update_conflict(self):
    self.server.item = {"id": "i1", "updated_at": "2026-11-23T10:00:00Z", "description_html": "<p>x</p>"}
    with self.assertRaises(PlaneConflict):
        self.client.update_item_description("p1", "i1", "<p>y</p>", expected_updated_at="2026-11-23T09:00:00Z")
    self.assertNotIn("PATCH", [r.method for r in self.server.requests])

def test_rate_limit_waits(self):
    t = [0.0]; slept = []
    c = PlaneClient(self.settings, per_minute=2, clock=lambda: t[0], sleep=lambda s: slept.append(s))
    c.list_projects(); c.list_projects(); c.list_projects()
    self.assertTrue(slept and slept[0] > 0)

def test_status_mapping(self):
    for status, exc in ((401, PlaneAuthError), (403, PlaneAuthError), (503, PlaneUnavailable), (400, PlaneError)):
        self.server.force_status = status
        with self.assertRaises(exc):
            self.client.list_projects()
```

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 구현** — `urllib.request`. 경로는 모두 `/api/v1/workspaces/{workspace}/projects/...`: 프로젝트 `projects/`, 라벨 `projects/{pid}/labels/`(`{"name", "color"}`), module `projects/{pid}/modules/`(`{"name"}`), module 추가 `modules/{mid}/module-issues/`, cycle `projects/{pid}/cycles/`(`{"name"}`), 작업 항목 `projects/{pid}/work-items/`(`{"name", "description_html", "labels"}`), 목록은 `?per_page=100&cursor=…`를 `next_page_results`가 거짓일 때까지. 속도 제한은 60초 창 안 호출 수가 `per_minute`에 닿으면 가장 오래된 호출 뒤 60초까지 `sleep`. 연결 실패·5xx·429는 3회까지 1·2·4초 대기 뒤 재시도, 그래도 실패면 `PlaneUnavailable`. 401·403은 `PlaneAuthError("Plane 토큰 확인 필요")`. `update_item_description`은 `get_item`의 `updated_at`이 `expected_updated_at`과 다르면 PATCH 없이 `PlaneConflict`. 응답·오류 메시지에 토큰을 넣지 않는다.
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `feat: Plane CE API 클라이언트`

---

### Task 10: 작업 흐름(init, plan new, get, put, decide)

**Files:**
- Create: `plugins/forge-ledger/ledger/flows.py`
- Modify: `tests/fakes.py`(FakePlane: 메모리 안 프로젝트·항목, `updated_at` 증가)
- Test: `tests/test_flows.py`

**Interfaces:**
- Consumes: `config`, `history`, `spec`, `sections`, `secrets`, `plane.PlaneLike`.
- Produces: `@dataclass Repo(root: Path, name: str)`, `Repo.discover(start: Path) -> Repo`(`git rev-parse --show-toplevel`, 실패하면 `start`), `maintenance_key(repo_name: str, taken: set[str]) -> str`, `class FlowError(Exception)`, `class SecretFound(FlowError)`, `init_repo(repo: Repo, client: PlaneLike, workspace: str, templates: Path, today: date) -> LedgerConfig`, `@dataclass PlanNewResult(project_id: str, item_ids: dict[str, str], decision_id: str)`, `plan_new(repo, client, title: str, key: str, templates: Path, today: date) -> PlanNewResult`, `plan_get(repo, client, key: str, code: str) -> str`, `plan_put(repo, client, key: str, code: str, body_html: str) -> str`(새 `updated_at`), `decide(repo, client, title: str, project: str, body_html: str, today: date) -> HistoryEntry`, `PlaneDocSource(client, cfg)`(check의 `UnitDocSource` 구현).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_maintenance_key(self):
    self.assertEqual(maintenance_key("sample-app", set()), "SAMPM")
    self.assertEqual(maintenance_key("sample-app", {"SAMPM"}), "SAMPM2")

def test_maintenance_key_fallback(self):
    self.assertEqual(maintenance_key("한글저장소", set()), "REPOM")

def test_init_creates_skeleton_and_maintenance_project(self):
    cfg = init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
    self.assertTrue((self.repo.root / ".ledger/design/security.md").exists())
    self.assertEqual([(p.key, p.title, p.default) for p in cfg.projects], [("SAMPM", "sample-app 유지보수", True)])
    self.assertEqual(self.plane.item_names("SAMPM"), ["[계획서] 결정"])
    self.assertEqual(self.plane.labels("SAMPM"), ["계획서"])

def test_init_does_not_overwrite(self):
    (self.repo.root / ".ledger").mkdir(); (self.repo.root / ".ledger/design").mkdir()
    (self.repo.root / ".ledger/design/data.md").write_text("사람이 쓴 내용")
    init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
    self.assertEqual((self.repo.root / ".ledger/design/data.md").read_text(), "사람이 쓴 내용")

def test_plan_new_twice(self):
    init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
    plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
    plan_new(self.repo, self.plane, "알림 기능", "NOTI", TEMPLATES, date(2026, 11, 24))
    cfg = load_config(self.repo.root / ".ledger/ledger.yml")
    self.assertEqual([p.key for p in cfg.projects], ["SAMPM", "APP", "NOTI"])
    self.assertEqual(sorted(self.plane.item_names("APP")), sorted(UNIT_ITEMS.values()))
    self.assertEqual(self.plane.module_members("APP", "계획서"), self.plane.item_ids("APP"))
    self.assertEqual(self.plane.cycles("APP"), ["프로토타입"])
    entries = parse_history((self.repo.root / ".ledger/history.md").read_text())
    self.assertEqual([(e.kind, e.projects, e.title) for e in entries],
                     [("decision", "APP", "단위 프로젝트 APP 시작"), ("decision", "NOTI", "단위 프로젝트 NOTI 시작")])
    self.assertRegex(entries[0].detail, r"^plane:APP-\d+@")

def test_plan_new_rejects_existing_key(self):
    init_repo(self.repo, self.plane, "forge", TEMPLATES, date(2026, 11, 23))
    plan_new(self.repo, self.plane, "앱 첫 출시", "APP", TEMPLATES, date(2026, 11, 23))
    calls = len(self.plane.calls)
    with self.assertRaisesRegex(FlowError, "이미 붙은 키: APP"):
        plan_new(self.repo, self.plane, "다시", "APP", TEMPLATES, date(2026, 11, 23))
    self.assertEqual(len(self.plane.calls), calls)

def test_put_get_and_secret(self):
    ...  # init + plan_new APP 뒤
    plan_put(self.repo, self.plane, "APP", "PRD9", "<ul><li>모바일 앱</li></ul>")
    self.assertIn("모바일 앱", plan_get(self.repo, self.plane, "APP", "PRD9"))
    with self.assertRaises(SecretFound):
        plan_put(self.repo, self.plane, "APP", "PRD3", "<p>10.1.2.3</p>")
    with self.assertRaisesRegex(FlowError, "저장소 문서"):
        plan_put(self.repo, self.plane, "APP", "IMPL1", "<p>x</p>")

def test_put_conflict_when_changed_between_read_and_write(self):
    ...  # plane.touch_on_next_get("APP", "[계획서] PRD") 로 읽은 뒤 바뀐 상황을 만든다
    with self.assertRaises(PlaneConflict):
        plan_put(self.repo, self.plane, "APP", "PRD9", "<ul><li>x</li></ul>")

def test_decide_star_goes_to_default_project(self):
    ...  # init 뒤
    e = decide(self.repo, self.plane, "로그 보관 180일", "*", "<p>근거</p>", date(2026, 12, 8))
    self.assertRegex(e.ref_id, r"^D-261208-[0-9a-f]{4}$")
    self.assertEqual(e.projects, "*")
    self.assertRegex(e.detail, r"^plane:SAMPM-\d+@")
    self.assertIn(e.ref_id, split_html_sections(self.plane.item_html("SAMPM", "[계획서] 결정")))
```

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 구현** — `maintenance_key`: 이름을 대문자로 바꿔 A–Z·0–9만 남긴 앞 4자(없으면 `REPO`) + `M`, 이미 있으면 뒤에 2, 3, …(12자 넘으면 앞부분을 줄임). `init_repo`: `templates/ledger/` 아래 파일을 같은 하위 경로로 `.ledger/`에 복사(있는 파일은 건드리지 않음), `history.md`가 없으면 `empty_history()`로 만든다, 워크스페이스 프로젝트 목록(`list_projects`)의 identifier로 `taken`을 채워 유지보수 키를 정한다, 유지보수 프로젝트 생성 → 라벨·module `계획서` → `[계획서] 결정` 항목(`decision.html`) → `save_config`로 `ledger.yml`을 쓰고 유지보수 프로젝트를 `default: true`로 기록. `plan_new`: 키 규칙(`^[A-Z0-9]{1,12}$`)·중복 확인을 Plane 호출 전에 한다 → 프로젝트 → 라벨 → module → `TEMPLATE_FOR` 6개 항목 → module에 추가 → cycle `프로토타입` → `ledger.yml` 추가 → history `decision` 한 줄(제목 `단위 프로젝트 {KEY} 시작`, 상세 `plane:{KEY}-{PRD 항목 sequence_id}@{updated_at}`). 항목 찾기는 `list_items`에서 이름이 `UNIT_ITEMS` 값과 같은 것. `plan_put`: 저장소 문서 코드면 `FlowError("저장소 문서는 .ledger/ 파일을 직접 고친다: <경로>")`, `find_secrets`가 있으면 `SecretFound`, `get_item`의 `updated_at`을 기억 → 절 바꾸기 → `update_item_description(..., expected_updated_at=기억한 값)`. `decide`: `*`이면 기본 프로젝트, `[계획서] 결정`에 `append_html_section("{id} {title}", body)`, history 한 줄.
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `feat: init·plan new·get·put·decide 흐름`

---

### Task 11: CLI

**Files:**
- Create: `plugins/forge-ledger/ledger/cli.py`, `plugins/forge-ledger/scripts/ledger.py`
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `flows.*`, `check.run_check`, `plane.PlaneClient`, `plane.PlaneSettings.from_env`.
- Produces: `main(argv: Optional[list[str]] = None, env: Optional[Mapping[str, str]] = None) -> int`. 명령: `init`, `plan new <제목> --key KEY`, `plan check [--project KEY] [--json]`, `plan get KEY DOC`, `plan put KEY DOC --from FILE`, `decide <제목> --project KEY|* --body-file FILE`. 공통 `--repo PATH`. 종료 코드: 0 성공, 1 검사 실패·입력 오류, 2 Plane 연결·인증·설정 문제, 3 수정 충돌, 4 비밀값.
- Produces: `scripts/ledger.py`는 자기 위치의 부모(`plugins/forge-ledger`)를 `sys.path`에 넣고 `sys.exit(cli.main())`.

- [ ] **Step 1: 실패하는 테스트 작성** (FakePlaneServer를 띄우고 `subprocess`로 `scripts/ledger.py` 실행, env에 `FORGE_LEDGER_PLANE_*`)

```python
def test_cli_init_new_check(self):
    self.assertEqual(self.run_cli("init").returncode, 0)
    self.assertEqual(self.run_cli("plan", "new", "앱 첫 출시", "--key", "APP").returncode, 0)
    r = self.run_cli("plan", "check", "--project", "APP", "--json")
    self.assertEqual(r.returncode, 1)
    self.assertEqual(json.loads(r.stdout)["exit_code"], 1)

def test_cli_missing_settings_exit_2(self):
    r = self.run_cli("init", env={})
    self.assertEqual(r.returncode, 2)
    self.assertIn("설정 없음: FORGE_LEDGER_PLANE_URL", r.stderr)

def test_cli_auth_error_exit_2(self):
    self.server.force_status = 401
    r = self.run_cli("init")
    self.assertEqual(r.returncode, 2)
    self.assertIn("토큰 확인", r.stderr)
    self.assertFalse((self.repo / ".ledger/ledger.yml").exists())

def test_cli_secret_exit_4(self):
    ...  # init, plan new 뒤
    (self.tmp / "b.html").write_text("<p>plane_api_" + "a" * 32 + "</p>")
    r = self.run_cli("plan", "put", "APP", "PRD3", "--from", str(self.tmp / "b.html"))
    self.assertEqual(r.returncode, 4)
    self.assertNotIn("plane_api_", r.stdout + r.stderr)

def test_cli_never_prints_token(self):
    for args in (("init",), ("plan", "check")):
        r = self.run_cli(*args)
        self.assertNotIn(self.token, r.stdout + r.stderr)
```

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 구현** — `argparse`, 하위 명령. `init`은 Plane 쪽을 먼저 끝낸 뒤에만 `.ledger/ledger.yml`을 쓴다(인증 실패 시 파일이 남지 않게, Review Focus 5). `plan check`는 `--project`가 없으면 기본 프로젝트를 뺀 모든 키를 검사하고, 사람용 표(문서 · 상태 · 이유)를 출력하거나 `--json`이면 `CheckReport.to_json()`. 오류 메시지는 stderr 한 줄, 토큰은 출력하지 않는다.
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `feat: ledger CLI`

---

### Task 12: 훅(세션 시작, 비밀값 차단)

**Files:**
- Create: `plugins/forge-ledger/hooks/hooks.json`, `plugins/forge-ledger/hooks/session_start.py`, `plugins/forge-ledger/hooks/pre_tool_use.py`
- Test: `tests/test_hooks.py`

**Interfaces:**
- Consumes: `secrets.find_secrets`, `config.load_config`, `history.parse_history`.
- Produces: `session_start.build_exports(env: Mapping[str, str]) -> list[str]`, `session_start.summary(repo_root: Path) -> str`, `pre_tool_use.should_block(payload: dict) -> Optional[str]`.

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_exports_from_plugin_options(self):
    env = {"CLAUDE_PLUGIN_OPTION_PLANE_URL": "http://h:1", "CLAUDE_PLUGIN_OPTION_PLANE_WORKSPACE": "forge",
           "CLAUDE_PLUGIN_OPTION_PLANE_API_KEY": "plane_api_" + "a" * 32}
    self.assertEqual(build_exports(env), [
        "export FORGE_LEDGER_PLANE_URL=http://h:1", "export FORGE_LEDGER_PLANE_WORKSPACE=forge",
        "export FORGE_LEDGER_PLANE_TOKEN=plane_api_" + "a" * 32])

def test_session_start_writes_env_file_and_prints_summary(self):
    r = run_hook("session_start.py", {}, env={**self.opts, "CLAUDE_ENV_FILE": str(self.envfile), "CLAUDE_PROJECT_DIR": str(self.repo)})
    self.assertEqual(r.returncode, 0)
    self.assertIn("FORGE_LEDGER_PLANE_TOKEN", self.envfile.read_text())
    self.assertIn("forge-ledger: Plane 프로젝트 3개", r.stdout)
    self.assertNotIn("plane_api_", r.stdout)
    self.assertLessEqual(len(r.stdout), 1500)

def test_session_start_fail_open(self):
    r = run_hook("session_start.py", {}, env={"CLAUDE_PROJECT_DIR": "/nonexistent"})
    self.assertEqual(r.returncode, 0)

def test_block_secret_in_ledger_write(self):
    p = {"tool_name": "Write", "tool_input": {"file_path": "/r/.ledger/design/data.md", "content": "10.1.2.3"}}
    self.assertIn("ipv4", should_block(p))
    r = run_hook("pre_tool_use.py", p)
    self.assertEqual(r.returncode, 2)

def test_allow_other_paths_and_clean(self):
    self.assertIsNone(should_block({"tool_name": "Write", "tool_input": {"file_path": "/r/src/a.py", "content": "10.1.2.3"}}))
    self.assertIsNone(should_block({"tool_name": "Edit", "tool_input": {"file_path": "/r/.ledger/x.md", "new_string": "깨끗"}}))

def test_block_plane_mcp_input(self):
    p = {"tool_name": "mcp__plugin_forge-ledger_plane__workitem", "tool_input": {"description_html": "plane_api_" + "b" * 32}}
    self.assertIsNotNone(should_block(p))

def test_pre_tool_use_fail_open_on_bad_json(self):
    r = run_hook("pre_tool_use.py", raw_stdin="not json")
    self.assertEqual(r.returncode, 0)
```

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 구현** — `build_exports`는 `shlex.quote`로 값을 감싸고 값이 빈 옵션은 뺀다. `session_start.main`: `CLAUDE_ENV_FILE`이 있으면 줄들을 덧붙이고, `CLAUDE_PROJECT_DIR/.ledger/`가 있으면 요약 한 줄(`forge-ledger: Plane 프로젝트 N개(기본 KEY), 결정 M건, 마지막 <ID>`)을 출력한다. 모든 예외는 삼키고 0. `should_block`: `Write|Edit|MultiEdit`이면서 `file_path`에 `/.ledger/`가 있을 때 `content`·`new_string`·`edits[].new_string`을, 도구 이름이 `mcp__plugin_forge-ledger_plane__`로 시작하면 `json.dumps(tool_input)`을 `find_secrets`로 본다. 막을 때 stderr에 `"forge-ledger: 비밀값(<이름들>)이 있어 쓰기를 막았습니다"`, 종료 코드 2. `hooks.json`(최상위 `"hooks"` 감싸기): `SessionStart` → `python3 "${CLAUDE_PLUGIN_ROOT}"/hooks/session_start.py`, `PreToolUse` matcher `Write|Edit|MultiEdit|mcp__plugin_forge-ledger_plane__.*` → `python3 "${CLAUDE_PLUGIN_ROOT}"/hooks/pre_tool_use.py`.
- [ ] **Step 4: 통과 확인** — PASS
- [ ] **Step 5: Commit** — `feat: 세션 시작·비밀값 차단 훅`

---

### Task 13: 스킬, 명령, Plane MCP 연결, 예시

**Files:**
- Create: `plugins/forge-ledger/skills/ledger-plan/SKILL.md`, `references/flows.md`, `references/gates.md`, `references/review-prompt.md`, `examples/forge-ledger-design.html`, `plugins/forge-ledger/commands/plan.md`, `plugins/forge-ledger/commands/decide.md`, `plugins/forge-ledger/.mcp.json`
- Test: `tests/test_skill.py`

**Interfaces:**
- Consumes: CLI 명령 문법(Task 11), `references/docs.md`(Task 5).

- [ ] **Step 1: 실패하는 테스트 작성**

```python
def test_skill_frontmatter(self):
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    fm = text.split("---")[1]
    self.assertIn("name: ledger-plan", fm)
    self.assertIn("description:", fm)
    self.assertLess(len(text.splitlines()), 120)

def test_skill_mentions_each_command_and_reference(self):
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    for s in ('${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py', "plan new", "plan get", "plan put", "plan check", "decide",
              "references/docs.md", "references/flows.md", "references/gates.md", "references/review-prompt.md",
              "examples/forge-ledger-design.html"):
        self.assertIn(s, text)

def test_question_order(self):
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    self.assertIn("PRD 1 → 2 → 9 → 3 → 5 → 4 → 6 → 7", text)

def test_example_section_ids_match_doc_codes(self):
    html = (SKILL / "examples/forge-ledger-design.html").read_text(encoding="utf-8")
    for sid in ("prd-goals", "prd-out", "impl-data", "impl-security", "impl-phases"):
        self.assertIn(f'id="{sid}"', html)

def test_mcp_config_uses_user_config(self):
    mcp = json.loads((PLUGIN / ".mcp.json").read_text())
    s = json.dumps(mcp)
    for k in ("${user_config.plane_mcp_url}", "Bearer ${user_config.plane_api_key}", "${user_config.plane_workspace}"):
        self.assertIn(k, s)
```

- [ ] **Step 2: 실행해 실패 확인** — FAIL
- [ ] **Step 3: 작성** — `SKILL.md`(120줄 안): 언제 쓰는지(신규 착수·큰 기능), 질문 순서 `PRD 1 → 2 → 9 → 3 → 5 → 4 → 6 → 7`(8은 대화 내내), 각 절을 `plan put`으로 쓰는 법, 저장소 문서는 `.ledger/` 파일을 직접 고친다는 것, 다 쓰면 `plan check`, 독립 검토(`references/review-prompt.md`)는 하위 에이전트에게 문서만 주고 질문을 세 갈래(이미 답 있음·작은 것·사용자 몫)로 나누는 것, 항목마다 예시 절을 `examples/forge-ledger-design.html`에서 보여 주는 것. `plan feature·fix·phase close·release`는 1차라고 적는다. `flows.md`·`gates.md`는 설계서 규격 2·3 표를 Markdown으로 옮긴다. `review-prompt.md`는 brainstorming 스킬의 builder check 문구를 계획서용으로 고친 것. `commands/plan.md`: frontmatter `description`, `argument-hint: "new <제목> --key KEY | check [--project KEY] | get KEY DOC | put KEY DOC --from FILE"`, 본문은 ledger-plan 스킬을 따르고 `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" plan $ARGUMENTS`를 실행하라는 지시. `commands/decide.md`도 같은 방식. `.mcp.json`: `{"mcpServers": {"plane": {"type": "http", "url": "${user_config.plane_mcp_url}", "headers": {"Authorization": "Bearer ${user_config.plane_api_key}", "X-Workspace-slug": "${user_config.plane_workspace}"}}}}`. 예시 파일은 `docs/design/forge-ledger-design.html` 사본.
- [ ] **Step 4: 통과 확인** — Run: `bash scripts/dev-check.sh` → 테스트 PASS, `✔ Validation passed`(형식 오류가 `.mcp.json`을 가리키면 `mcpServers` 감싸기 없이 서버 맵만 두는 형식으로 바꾼 뒤 다시 확인)
- [ ] **Step 5: Commit** — `feat: ledger-plan 스킬, 명령, Plane MCP 연결`

---

### Task 14: 실서버 끝단 시험(프로토타입 완료 기준)

**Files:**
- Create: `tests/e2e/run_p1_e2e.py`, `tests/e2e/README.md`
- Uses: `tests/fixtures/filled/`(Task 8)

**Interfaces:**
- Consumes: CLI 전체, `plane.PlaneClient.delete_project`.

- [ ] **Step 1: 시험 스크립트 작성** — 환경 변수 `FORGE_LEDGER_PLANE_URL`(`http://127.0.0.1:18090`), `FORGE_LEDGER_PLANE_WORKSPACE`(`forge`), `FORGE_LEDGER_PLANE_TOKEN`이 없으면 종료 코드 2로 안내만 하고 끝난다. 임시 git 저장소 `forge-e2e-<난수>`를 만들고 순서대로: `init` → `plan new "시험 1" --key E2EA` → `plan new "시험 2" --key E2EB` → E2EA의 단위 절을 `tests/fixtures/filled/`로 `plan put` → 저장소 문서를 채운 fixture로 덮기 → `plan check --project E2EA --json`. 확인: `ledger.yml` 프로젝트 3개(첫째 `default: true`), history 줄 2개, Plane E2EA에 계획서 항목 6개와 `계획서` module·라벨, check 종료 코드 0. 마지막에 `finally`로 만든 Plane 프로젝트 3개를 `delete_project`로 지우고 임시 폴더를 지운다.
- [ ] **Step 2: 시험 토큰 준비** — 관리자 계정(`~/docs/.env`의 `PLANE_ADMIN_*`)으로 Plane에 로그인해 2시간 만료 API 토큰을 만든다. 토큰은 셸 변수로만 쓰고 파일에 저장하지 않는다.
- [ ] **Step 3: 실행** — Run: `python3 tests/e2e/run_p1_e2e.py` → 마지막 줄 `E2E OK`, 종료 코드 0
- [ ] **Step 4: 정리 확인** — Plane `forge` 워크스페이스에 `E2EA`·`E2EB`·유지보수 프로젝트가 남지 않았는지 목록으로 확인하고, 시험 토큰을 삭제한다.
- [ ] **Step 5: 전체 검사** — Run: `bash scripts/dev-check.sh` → ruff·mypy 오류 0, 테스트 전부 PASS, `✔ Validation passed`
- [ ] **Step 6: Commit** — `test: 실서버 끝단 시험`

---

## 이 계획에서 하지 않는 것(다음 계획)

- 허브 골격(수집·SQLite·S1·SessionStart 허브 요약): 다음 계획 "허브 골격"
- `plan feature`(규모 추천·유지보수 프로젝트 미분류)·`plan fix`·`phase close`·`release`, 교차 검사(스토리↔작업 항목, 이력 수정 시각), 독립 검토 자동화: 1차 계획
- Gitea 원격 저장소 등록과 push: 사용자가 저장소 주소를 정하고 요청할 때
