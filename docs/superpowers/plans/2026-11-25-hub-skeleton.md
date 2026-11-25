# 허브 골격(로컬 방식) 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 사용자 토큰으로 Gitea와 Plane을 주기 조회해 로컬 SQLite에 담고, S1 개요 페이지(`ledger dashboard`), `ledger status`, SessionStart 요약으로 보여 준다.

**Architecture:** 연결 규격(`adapters.py`의 Protocol) 뒤에 Gitea 클라이언트(새 `gitea.py`)와 Plane 클라이언트(기존 `plane.py`에 읽기 메서드 추가)를 둔다. `hubstore.py`가 SQLite와 등록 목록을, `hubsync.py`가 대조를, `hubview.py`가 S1 수치 계산과 글 출력을 맡는다. CLI(`hubcli.py`), SessionStart 훅, 로컬 웹 서버(`dashboard.py` + 정적 HTML/JS)는 모두 `hubview`의 같은 계산을 읽기만 한다.

**Tech Stack:** Python 3.9+ 표준 라이브러리(`sqlite3`, `http.server.ThreadingHTTPServer`, `urllib`, `fcntl`), 정적 HTML/JS(빌드 없음), unittest

**Spec:** `docs/superpowers/specs/2026-11-25-hub-skeleton-design.md`(결정 D22–D29 포함). 상위 설계는 `docs/design/forge-ledger-design.html`(v4.2)이며 둘이 다르면 spec이 우선한다.

## Global Constraints

- 저장소 코드: 표준 라이브러리만, `from __future__ import annotations`, Python 3.9 호환, ruff line-length 120, mypy strict, 시험은 `unittest`. 새 모듈은 `plugins/forge-ledger/ledger/`에 둔다.
- 민감값(IP, 접속 주소, 토큰, 계정)을 코드·문서·커밋·보고서에 넣지 않는다. 시험용 가짜 비밀값은 문자열 이어 붙이기로 만든다(예: `"tok" + "e" * 30`). 루프백 `127.0.0.1`은 써도 된다.
- 수집·조회 경로는 Plane·Gitea에 읽기만 한다(GET만). 토큰은 SQLite, 등록 파일, 로그, API 응답, 페이지에 남기지 않는다.
- 호출 한도: Plane 분당 50회(기존 `make_shared_limiter(base_url, token, env, per_minute=50)`), Gitea 분당 120회(같은 함수, `per_minute=120`).
- 환경 변수 이름(그대로): `FORGE_LEDGER_GITEA_URL`, `FORGE_LEDGER_GITEA_TOKEN`, `FORGE_LEDGER_DASHBOARD_BIND`, 기존 `FORGE_LEDGER_PLANE_URL`, `FORGE_LEDGER_PLANE_TOKEN`, `FORGE_LEDGER_STATE_DIR`.
- 상태 폴더는 `ledger.ratelimit.default_state_dir(env)`를 쓴다. 파일 이름(그대로): `hub.sqlite3`(0600), `hub-repos.json`(0600), `hub-sync.lock`. 폴더는 `mkdir(mode=0o700)`으로 만든다(`os.chmod` 금지: semgrep 규칙).
- 시각은 UTC ISO 문자열 `YYYY-MM-DDTHH:MM:SSZ`로 저장하고, 계산 함수는 `now`(tz-aware UTC `datetime`)를 인자로 받는다.
- 수치 규칙과 상수(그대로): 대조 주기 기본 600초(최소 60초), 수집 지연 기준 = 주기 + 300초, SessionStart 재대조 기준 600초, CI 실행 조회 50건, SessionStart 출력 1,500자 이하.
- 훅은 어떤 오류에도 종료 코드 0(fail-open).
- 사용자에게 보이는 문구는 한국어. 커밋 메시지 한국어, 접두어 `feat:`/`fix:`/`test:`/`chore:`/`docs:`.
- 브랜치 `hub-skeleton`. push, Plane 쓰기, Gitea 설정 변경은 controller만 한다. 이력 재작성 금지.
- 완료 확인 명령: `bash scripts/dev-check.sh`(ruff, ruff format, mypy, unittest, plugin validate). 커버리지 하한 81%는 controller가 `bash scripts/ci-local.sh all`로 확인한다.

## Review Focus

1. 원격 주소에 계정이 들어 있는 경우(`http://user:pass@host/owner/name.git`): 소유자/이름만 얻고 계정 부분은 DB·등록 파일·로그 어디에도 남지 않아야 한다 → Task 4 `test_remote_credentials_never_stored`.
2. `ledger.yml`의 Plane 키가 Plane에 없는 경우(지워짐·이름 바뀜): 그 줄만 "프로젝트 없음"이고 다른 프로젝트와 Gitea 수치는 정상이어야 한다 → Task 4 `test_missing_plane_project_is_partial`, Task 5 `test_render_project_not_found`.
3. `hub.sqlite3`가 깨졌거나 SQLite가 아니거나 스키마 버전이 다른 경우: 지우고 다시 만들어 `status`와 SessionStart가 멈추지 않아야 한다 → Task 3 `test_corrupt_db_is_rebuilt`, `test_schema_mismatch_rebuilds`.
4. 프로젝트 이름·cycle 이름·저장소 이름에 HTML이 든 경우: 페이지가 글자로만 보여야 한다(XSS 없음) → Task 7 `test_app_js_never_uses_html_sinks`, `test_api_is_json_with_nosniff`.
5. 접속 토큰이 서버 로그·API 응답에 남는 경우: 터미널에 접속 주소를 처음 한 번 출력하는 것 말고는 어디에도 나오면 안 된다 → Task 7 `test_token_not_in_logs_or_responses`.

---

## 파일 구조

| 파일 | 책임 |
|---|---|
| `plugins/forge-ledger/ledger/adapters.py` (새) | 서비스 중립 데이터형과 `ForgeAdapter`·`PmAdapter` Protocol, `parse_run_path` |
| `plugins/forge-ledger/ledger/gitea.py` (새) | Gitea 읽기 클라이언트(`ForgeAdapter` 구현) |
| `plugins/forge-ledger/ledger/plane.py` (수정) | `PlaneClient`에 `calls`, `whoami`, `list_project_infos`, `current_cycle` 추가(`PmAdapter` 구현) |
| `plugins/forge-ledger/ledger/hubstore.py` (새) | SQLite 스키마·재생성·upsert/닫힘, 등록 파일 |
| `plugins/forge-ledger/ledger/hubsync.py` (새) | 원격 주소 해석, 저장소 대조, 잠금, JSON 로그 |
| `plugins/forge-ledger/ledger/hubview.py` (새) | S1 수치 계산, `status`·SessionStart 글 |
| `plugins/forge-ledger/ledger/hubcli.py` (새) | `sync`·`status`·`dashboard` 명령 |
| `plugins/forge-ledger/ledger/dashboard.py` (새) | 로컬 웹 서버, 접속 토큰, 주기 대조 스레드 |
| `plugins/forge-ledger/dashboard/{index.html,app.js,style.css}` (새) | S1 화면 |
| `plugins/forge-ledger/commands/dashboard.md` (새) | `/forge-ledger:dashboard` |
| `plugins/forge-ledger/ledger/cli.py`, `hooks/session_start.py`, `.claude-plugin/plugin.json` (수정) | 연결 |
| `tests/hub_fakes.py` (새), `tests/fakes.py` (수정) | 가짜 Gitea 서버, 가짜 Plane에 cycle·users/me |

---

### Task 0: Plane 실데이터 준비 (controller 전용)

D29. 구현 전에 controller가 직접 한다.

- [ ] `~/docs/.env`의 `PLANE_URL_LOCAL`, `PLANE_API_KEY`로 `FORGE_LEDGER_PLANE_URL`, `FORGE_LEDGER_PLANE_TOKEN`, `FORGE_LEDGER_PLANE_WORKSPACE=forge`를 셸에만 설정하고 저장소 루트에서 `python3 plugins/forge-ledger/scripts/ledger.py init` 실행. 기대: `초기화 완료: <키> (forge)`, `.ledger/` 생성.
- [ ] 그 유지보수 프로젝트에 cycle "허브 골격"(시작 2026-11-25, 끝 2026-11-29)과 작업 항목 8개("허브 골격 T1"…"T7", "허브 골격 T8 실서버 확인")를 Plane API로 만들고 cycle에 넣는다.
- [ ] `.ledger/` 커밋: `chore: forge-ledger 저장소에 ledger init 적용`.
- [ ] 이후 각 Task가 리뷰를 통과하면 그 항목 상태를 completed 그룹 상태로 바꾼다.

### Task 1: 연결 규격과 Gitea 클라이언트

**Files:**
- Create: `plugins/forge-ledger/ledger/adapters.py`, `plugins/forge-ledger/ledger/gitea.py`, `tests/hub_fakes.py`
- Test: `tests/test_gitea.py`

**Interfaces:**
- Produces (`adapters.py`, 모두 `@dataclass(frozen=True)`):
  - `RepoInfo(full_name: str, default_branch: str, html_url: str)`
  - `IssueInfo(number: int, title: str, labels: tuple[str, ...], created_at: str)`
  - `PullInfo(number: int, title: str, draft: bool, created_at: str)`
  - `RunInfo(run_id: int, run_number: int, event: str, status: str, conclusion: str, workflow: str, ref: str, head_sha: str, pull_numbers: tuple[int, ...], started_at: str)`
  - `ProjectInfo(id: str, identifier: str, name: str)`
  - `CycleInfo(id: str, name: str, start_date: str, end_date: str, total_issues: int, completed_issues: int)`
  - `class ForgeAdapter(Protocol)`: `calls: int`; `whoami() -> str`; `get_repo(owner: str, name: str) -> RepoInfo`; `list_open_issues(owner: str, name: str) -> list[IssueInfo]`; `list_open_pulls(owner: str, name: str) -> list[PullInfo]`; `list_runs(owner: str, name: str, limit: int = 50) -> list[RunInfo]`
  - `class PmAdapter(Protocol)`: `calls: int`; `whoami() -> str`; `list_project_infos() -> list[ProjectInfo]`; `current_cycle(project_id: str) -> Optional[CycleInfo]`
  - `parse_run_path(path: str) -> tuple[str, str]`: `"pr-check.yml@refs/pull/5/head"` → `("pr-check.yml", "refs/pull/5/head")`; `@`이 없으면 `(path, "")`. 마지막 `@` 기준이 아니라 첫 `@` 기준으로 나눈다.
- Produces (`gitea.py`): `GiteaError(Exception)`, `GiteaAuthError(GiteaError)`, `GiteaUnavailable(GiteaError)`; `GiteaSettings(base_url: str, token: str = field(repr=False))` + `GiteaSettings.from_env(env: Mapping[str, str]) -> GiteaSettings`(없으면 `GiteaError("설정 없음: FORGE_LEDGER_GITEA_URL")` 형식); `GiteaClient(settings, *, limiter: Optional[RateLimiter] = None, sleep: Callable[[float], None] = time.sleep, notify: Optional[Callable[[str], None]] = None)`는 `ForgeAdapter`를 구현한다. 기본 제한기는 `LocalRateLimiter(per_minute=120)`.
- Produces (`tests/hub_fakes.py`): `FakeGiteaServer`(127.0.0.1 임의 포트, `.base_url`, `.close()`, 속성 `repo: dict`, `issues: list[dict]`, `pulls: list[dict]`, `runs: list[dict]`, `status_queue: list[int]`, `requests: list[tuple[str, str, dict[str, str]]]`). 경로: `GET /api/v1/user`, `/api/v1/repos/{o}/{n}`, `/issues?state=open&type=issues&page&limit`, `/pulls?state=open&page&limit`, `/actions/runs?limit` — 목록은 `X-Total-Count` 헤더를 붙인다. 응답 모양은 spec 6.2의 Gitea 1.27.2 필드를 따른다.

- [ ] **Step 1: 실패하는 시험 작성** (`tests/test_gitea.py`)
  - `test_parse_run_path`: 위 두 예와 `"a@b@c"` → `("a", "b@c")`.
  - `test_settings_from_env_missing`: 빈 env → `GiteaError`, 메시지에 `FORGE_LEDGER_GITEA_URL`.
  - `test_get_repo_and_whoami`: 가짜 서버 → `RepoInfo(full_name="o/n", default_branch="main", html_url=...)`, `whoami()`가 login, 요청 헤더 `Authorization`이 `"token " + 토큰`.
  - `test_list_open_issues_paginates`: 이슈 120개(limit 50) → 120개, 요청 3번, `calls == 3`.
  - `test_list_open_pulls_maps_draft`: draft true/false 매핑.
  - `test_list_runs_parses_path_and_prs`: `path="pr-check.yml@refs/pull/5/head"`, `pull_requests=[{"number": 5}]` → `workflow="pr-check.yml"`, `ref="refs/pull/5/head"`, `pull_numbers=(5,)`; `limit=50` 쿼리.
  - `test_auth_error`: 401 → `GiteaAuthError`; `test_retry_5xx_then_ok`: `status_queue=[502]` → 성공, sleep 1회.
  - `test_errors_never_contain_token`: 연결 실패 메시지에 토큰 문자열이 없다.
- [ ] **Step 2: 실행해 실패 확인** — `python3 -m unittest tests.test_gitea -v` → FAIL(`ModuleNotFoundError: ledger.gitea`)
- [ ] **Step 3: 구현** — `PlaneClient._request`와 같은 재시도 규칙(429는 `Retry-After` 최대 3회, 5xx·연결 오류는 1·2·4초), 루프백이면 프록시 끔, 목록 쪽은 `page`/`limit=50`로 받은 수가 50 미만이거나 `X-Total-Count`에 닿으면 멈춘다. HTTP 요청마다 `calls += 1`.
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, `bash scripts/dev-check.sh` 통과
- [ ] **Step 5: 커밋** — `feat: 연결 규격과 Gitea 읽기 클라이언트`

### Task 2: Plane 읽기 메서드

**Files:**
- Modify: `plugins/forge-ledger/ledger/plane.py`, `tests/fakes.py`(FakePlaneServer)
- Test: `tests/test_plane.py`

**Interfaces:**
- Consumes: `ProjectInfo`, `CycleInfo` (Task 1)
- Produces: `PlaneClient.calls: int`(HTTP 요청마다 1 증가), `whoami() -> str`(`GET /api/v1/users/me/`의 `email`), `list_project_infos() -> list[ProjectInfo]`(기존 `list_projects()` 결과 변환), `current_cycle(project_id: str) -> Optional[CycleInfo]`(`GET .../projects/{id}/cycles/?cycle_view=current`, 응답이 목록이든 `results` 딕셔너리든 첫 항목, 없으면 `None`; `total_issues`·`completed_issues`가 없으면 0). `PlaneClient`는 `PmAdapter`를 만족한다.
- Produces (`FakePlaneServer`): `cycles: dict[str, list[dict[str, Any]]]`(프로젝트 id → 현재 cycle 목록), `me: dict[str, Any]`.

- [ ] **Step 1: 실패하는 시험 작성**
  - `test_current_cycle_maps_counts`: cycle `{"id":"c1","name":"허브 골격","start_date":"2026-11-25","end_date":"2026-11-29","total_issues":8,"completed_issues":3}` → `CycleInfo(...)`, 요청 쿼리에 `cycle_view=current`.
  - `test_current_cycle_none_when_empty`: `[]` → `None`.
  - `test_whoami_and_calls_counter`: `whoami()` 뒤 `calls == 1`.
  - `test_list_project_infos`: `identifier`·`name`·`id` 매핑.
- [ ] **Step 2: 실패 확인** — `python3 -m unittest tests.test_plane -v`
- [ ] **Step 3: 구현** — 기존 메서드와 동작은 바꾸지 않는다.
- [ ] **Step 4: 통과 확인** — 같은 명령, `bash scripts/dev-check.sh`
- [ ] **Step 5: 커밋** — `feat: Plane 현재 cycle·사용자 조회`

### Task 3: 허브 저장소(SQLite)

**Files:**
- Create: `plugins/forge-ledger/ledger/hubstore.py`
- Test: `tests/test_hubstore.py`

**Interfaces:**
- Produces:
  - 상수 `SCHEMA_VERSION = 1`, `DB_NAME = "hub.sqlite3"`, `REGISTRY_NAME = "hub-repos.json"`, `LOCK_NAME = "hub-sync.lock"`
  - `url_digest(base_url: str) -> str`: `sha256(base_url.rstrip("/"))` hex 앞 16자
  - `EntityRow(kind: str, ext_key: str, state: str, attrs: dict[str, Any], parent: Optional[tuple[str, str]] = None)` — `parent`는 (부모 kind, 부모 ext_key)
  - `EntityView(ext_key: str, state: str, attrs: dict[str, Any], parent_ext_key: Optional[str])`
  - `RepoRow(id: int, owner: str, name: str, default_branch: str, html_url: str, local_paths: tuple[str, ...], plane_workspace: str)`
  - `RunRow(source_kind: str, repo_id: int, started_at: str, finished_at: Optional[str], result: str, api_calls: int, error_kind: str)`
  - `class HubStore(state_dir: Path)`: `.path`; `source_id(kind: str, base_url: str) -> int`; `upsert_repo(owner: str, name: str, local_path: str, plane_workspace: str) -> int`; `set_repo_meta(repo_id: int, default_branch: str, html_url: str) -> None`; `repos() -> list[RepoRow]`; `find_repo_by_path(local_path: str) -> Optional[RepoRow]`; `start_run(source_id: int, repo_id: int, now: str) -> int`; `finish_run(run_id: int, result: str, api_calls: int, error_kind: str, now: str) -> None`; `apply_kind(source_id: int, repo_id: int, kind: str, rows: Sequence[EntityRow], now: str, close_missing: bool = True) -> None`; `entities(repo_id: int, kind: str, states: Optional[Sequence[str]] = ("open",)) -> list[EntityView]`(`None`이면 모든 상태); `last_run(repo_id: int, source_kind: str) -> Optional[RunRow]`; `last_good_run(repo_id: int, source_kind: str) -> Optional[RunRow]`(결과 `ok`·`partial` 중 마지막)
  - `load_registry(state_dir: Path) -> list[str]`, `save_registry(state_dir: Path, paths: Sequence[str]) -> None`(중복 제거·정렬, 0600, 형식 `{"version": 1, "paths": [...]}`)
- 스키마는 spec 6.8 그대로. 연결은 작업마다 새로 열고(`timeout=10`), `PRAGMA journal_mode=WAL`. `apply_kind`는 한 트랜잭션이다. `close_missing=True`면 이번 rows에 없는 같은 (source, repo, kind)의 `open` 행을 `closed`로 바꾼다.

- [ ] **Step 1: 실패하는 시험 작성**
  - `test_creates_files_with_private_modes`: 폴더 0700, DB 0600, `PRAGMA user_version == 1`.
  - `test_apply_kind_upserts_and_closes_missing`: 이슈 1·2 → 2·3 반영 → `entities(kind="issue")` ext_key `{"2","3"}`, 1은 `closed`.
  - `test_apply_kind_close_missing_false_keeps_rows`: `ci_run`은 남는다.
  - `test_parent_link`: cycle의 `parent=("plane_project","HUB")` → `parent_ext_key == "HUB"`.
  - `test_last_good_run_skips_failed`: ok → failed 순 → `last_run.result == "failed"`, `last_good_run.result == "ok"`.
  - `test_registry_roundtrip_and_mode`: 저장·읽기, 중복 제거, 0600.
  - `test_corrupt_db_is_rebuilt`: `hub.sqlite3`에 `b"not sqlite"` → `HubStore` 생성 성공, `repos() == []`.
  - `test_schema_mismatch_rebuilds`: `user_version=99`로 만든 파일 → 다시 1.
  - `test_concurrent_read_during_write`: 쓰기 트랜잭션 중인 다른 연결이 있어도 `entities()`가 예외 없이 끝난다.
  - `test_no_raw_url_stored`: `source_id("gitea", "http://h.example/")` 뒤 DB 파일 바이트에 `h.example`가 없다.
- [ ] **Step 2: 실패 확인** — `python3 -m unittest tests.test_hubstore -v`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령, `bash scripts/dev-check.sh`
- [ ] **Step 5: 커밋** — `feat: 허브 SQLite 저장소와 등록 목록`

### Task 4: 대조(sync)

**Files:**
- Create: `plugins/forge-ledger/ledger/hubsync.py`
- Test: `tests/test_hubsync.py`

**Interfaces:**
- Consumes: Task 1–3의 형과 `HubStore`; `ledger.config.load_config`
- Produces:
  - `RepoResolveError(Exception)`; `RepoRef(path: str, owner: str, name: str, plane_workspace: str, plane_keys: tuple[str, ...])`
  - `parse_remote(url: str) -> tuple[str, str]`: `https://host/o/n(.git)`, `http://user:pw@host:3000/o/n.git`, `ssh://git@host:2222/o/n.git`, `git@host:o/n.git`를 받는다. 끝의 `.git`·`/`를 뗀다. 경로가 `o/n` 두 부분이 아니면 `RepoResolveError`.
  - `resolve_repo(path: Path, git_remote: Callable[[Path], str] = <git remote get-url origin>) -> RepoRef`: `.ledger/ledger.yml`이 없으면 `plane_workspace=""`, `plane_keys=()`.
  - `SyncOutcome(repo: str, source: str, result: str, api_calls: int, error_kind: str)`
  - `sync_repo(store: HubStore, ref: RepoRef, forge: Optional[ForgeAdapter], forge_url: str, pm: Optional[PmAdapter], pm_url: str, now: Callable[[], datetime], log: Callable[[dict[str, Any]], None]) -> list[SyncOutcome]`
  - `sync_paths(store: HubStore, paths: Sequence[str], make_forge: Callable[[], tuple[Optional[ForgeAdapter], str]], make_pm: Callable[[str], tuple[Optional[PmAdapter], str]], now: Callable[[], datetime], log: Callable[[dict[str, Any]], None]) -> list[SyncOutcome]`: 경로가 없으면 그 저장소는 `error_kind="path_missing"`, `result="skipped"`로 남기고 다음으로 간다.
  - `sync_lock(state_dir: Path) -> ContextManager[bool]`: `fcntl.flock(LOCK_EX | LOCK_NB)`, 잠겨 있으면 `False`를 낸다.
  - `json_logger(stream: TextIO) -> Callable[[dict[str, Any]], None]`: 한 줄 JSON, 키 `ts, level, component, source, action, result, duration_ms, correlation_id`(`"run-<sync_run id>"`), `api_calls`.
- 규칙(spec 6.3): 소스마다 `sync_run` 한 줄. forge가 `None`이면 결과 `skipped`, `error_kind="no_token"`. Gitea 종류는 `repo`(close_missing), `issue`, `pull`, `ci_run`(close_missing=False) 순서로 각각 따로 조회·반영하고, 한 종류의 실패는 나머지를 막지 않는다(`partial`). 모두 실패면 `failed`. 오류 종류: `GiteaAuthError`/`PlaneAuthError` → `auth`, `*Unavailable` → `unavailable`, 그 밖 → `error`. Plane은 `list_project_infos()`로 키→id를 찾고, `ledger.yml`의 키마다 `plane_project` 행(attrs: `key`, `title`, `default`, `found`)을 쓴다. 찾은 프로젝트는 `current_cycle` → `cycle` 행(parent=("plane_project", 키)). 찾지 못한 키가 있으면 결과 `partial`, `error_kind="not_found"`. 토큰·원격 주소 원문은 store·log에 넘기지 않는다.

- [ ] **Step 1: 실패하는 시험 작성** (가짜 서버 또는 Protocol을 만족하는 가짜 객체)
  - `test_parse_remote_forms`: 위 네 형식 → `("o", "n")`; `"https://host/only"` → `RepoResolveError`.
  - `test_remote_credentials_never_stored`: `http://user:` + `"s3" + "cret"` + `@host/o/n.git`로 sync → DB 파일·등록 파일·로그 출력 어디에도 비밀 문자열과 `user:`가 없다.
  - `test_sync_writes_entities_and_runs`: Gitea 이슈 2, PR 1, 실행 3, Plane 키 2개(cycle 1개) → 엔터티 수와 `sync_run` 2줄(`ok`).
  - `test_partial_when_runs_fail`: 실행 목록 조회만 실패 → 이슈·PR 반영, 결과 `partial`, 이전 `ci_run` 행 유지.
  - `test_missing_plane_project_is_partial`: 키 `GONE`이 Plane에 없음 → `plane_project` GONE `found=False`, 다른 키의 cycle 반영, 결과 `partial`, `error_kind="not_found"`.
  - `test_no_token_is_skipped`: forge `None` → `skipped`/`no_token`, Plane은 정상.
  - `test_path_missing_skipped`: 사라진 경로 → `skipped`/`path_missing`, 다른 경로는 대조.
  - `test_lock_busy_returns_false`: 잠금을 잡은 상태에서 다시 들어가면 `False`.
  - `test_log_lines_are_json_without_secrets`: 로그 줄마다 `json.loads` 가능, 필수 키 존재.
- [ ] **Step 2: 실패 확인** — `python3 -m unittest tests.test_hubsync -v`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령, `bash scripts/dev-check.sh`
- [ ] **Step 5: 커밋** — `feat: 허브 대조(Gitea·Plane)`

### Task 5: S1 수치와 글 출력

**Files:**
- Create: `plugins/forge-ledger/ledger/hubview.py`
- Test: `tests/test_hubview.py`

**Interfaces:**
- Consumes: `HubStore`, `RepoRow`, `EntityView`, `RunRow`(Task 3)
- Produces:
  - `CI_WINDOW = 50`, `PENDING_1ST = ("missing_docs", "penpot_changes", "open_alerts", "unclassified")`
  - `count_ci_failures(runs: Sequence[dict[str, Any]], open_pulls: set[int], default_branch: str) -> int` — spec 6.4 규칙 그대로. `runs`는 `ci_run` attrs이고 `run_number` 큰 순으로 최근 50건만 본다.
  - `source_state(last: Optional[RunRow], last_good: Optional[RunRow], now: datetime, interval_s: int) -> str` — 결과는 `never`, `failed`, `skipped`, `stale`, `partial`, `ok` 중 하나. 우선순위: 실행 없음 → `never`; 마지막이 `failed` → `failed`; `skipped` → `skipped`; 마지막 성공(ok·partial)이 `interval_s + 300`초보다 오래됨 → `stale`; 마지막이 `partial` → `partial`; 그 밖 `ok`.
  - `STATE_LABELS = {"ok": "정상", "stale": "수집 지연", "failed": "연결 실패", "partial": "일부 실패", "skipped": "토큰 없음", "never": "수집 전"}`
  - `repo_summary(store: HubStore, repo: RepoRow, now: datetime, interval_s: int = 600) -> dict[str, Any]` 반환 모양(키 그대로):
    ```
    {"repo": "o/n", "html_url": str,
     "projects": [{"key", "title", "default": bool, "found": bool,
                   "cycle": None | {"name", "start_date", "end_date", "completed", "total", "gate_days"}}],
     "open_issues": int, "review_prs": {"count": int, "oldest_days": Optional[int]},
     "ci_failures": int, "ci_window": 50,
     "sources": {"gitea": {"state", "label", "last_ok_at"}, "plane": {...}},
     "last_sync_at": Optional[str], "pending": ["missing_docs", ...]}
    ```
    `gate_days`는 `end_date` 앞 10자 날짜와 `now` 날짜의 차(일). `oldest_days`는 draft 아닌 열린 PR 중 가장 오래된 `created_at`부터의 일수(내림).
  - `summary(store, now, interval_s=600) -> dict[str, Any]`: `{"generated_at": str, "interval_s": int, "repos": [repo_summary...]}`
  - `health(store, now, interval_s=600) -> dict[str, Any]`: `{"generated_at": str, "sources": [{"repo", "source", "state", "label", "last_ok_at", "last_result", "api_calls"}]}`
  - `render_text(summary: dict[str, Any]) -> str`(저장소 없으면 `등록된 저장소 없음 — ledger sync로 등록하세요`)
  - `render_session(repo_sum: dict[str, Any], now: datetime, limit: int = 1500) -> str`(첫 줄 `forge-ledger 허브: <o/n> (마지막 수집 N분 전)`, 넘치면 `limit-1`자에서 자르고 `…`)

- [ ] **Step 1: 실패하는 시험 작성** (`now`를 고정해 store에 직접 행을 넣는다)
  - `test_ci_failures_per_target_and_workflow`: PR 5의 `pr-check.yml` 실행 #1 실패·#2 성공 → 0; PR 5의 `lint.yml` 최신 실패 → 1; 진행 중인 #3은 무시; 닫힌 PR 4의 실패는 0; 기본 브랜치 push 최신 실패 → +1.
  - `test_ci_failures_only_latest_50`: 51번째로 오래된 실패는 세지 않는다.
  - `test_review_prs_excludes_draft`: draft 1, 일반 2(5일·2일 전) → `count=2`, `oldest_days=5`.
  - `test_cycle_progress_and_gate_days`: completed 3/total 8, end 2026-11-29, now 2026-11-26 → `gate_days=3`.
  - `test_source_state_rules`: 위 우선순위 여섯 경우, 지연 경계는 `interval_s + 300`초 초과에서 `stale`.
  - `test_render_project_not_found`: `found=False` 줄에 `프로젝트 없음`.
  - `test_render_session_limit`: 저장소·프로젝트가 많아도 길이 ≤ 1500.
  - `test_render_text_empty`: 저장소 없음 문구 그대로.
- [ ] **Step 2: 실패 확인** — `python3 -m unittest tests.test_hubview -v`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령, `bash scripts/dev-check.sh`
- [ ] **Step 5: 커밋** — `feat: 허브 S1 수치와 요약 출력`

### Task 6: CLI(`sync`·`status`)와 설정·SessionStart

**Files:**
- Create: `plugins/forge-ledger/ledger/hubcli.py`
- Modify: `plugins/forge-ledger/ledger/cli.py`, `plugins/forge-ledger/hooks/session_start.py`, `plugins/forge-ledger/.claude-plugin/plugin.json`, `tests/test_layout.py`, `tests/test_hooks.py`
- Test: `tests/test_hubcli.py`

**Interfaces:**
- Consumes: Task 1–5
- Produces:
  - `hubcli.HUB_COMMANDS = ("sync", "status", "dashboard")`; `add_hub_parsers(sub: argparse._SubParsersAction, repo_parent: argparse.ArgumentParser) -> None`; `run_hub(args: argparse.Namespace, env: Mapping[str, str], *, now: Callable[[], datetime] = <UTC now>, out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int`
  - `sync`: `--repo` 있으면 그 저장소만 등록·대조, 없으면 현재 폴더가 git 저장소일 때 등록하고 등록된 전체를 대조. 등록은 `resolve_repo` 성공 뒤 `save_registry(load_registry() + [절대 경로])`와 `store.upsert_repo`로 한다. `--quiet`면 stdout 없음. JSON 로그는 stderr. 종료 코드: 잠금 중이면 `다른 대조가 진행 중입니다` 출력 후 0; 결과에 `failed`가 있으면 2; 그 밖 0. 원격 해석 실패 → 1.
  - `status`: `--json`이면 `hubview.summary` JSON, 아니면 `render_text`. 항상 0.
  - `cli.main`은 `args.command in HUB_COMMANDS`면 Plane 설정을 요구하기 전에 `run_hub`로 넘긴다. 기존 명령 동작은 그대로.
  - `plugin.json` `userConfig`에 `gitea_url`(string, required true, title "Gitea 주소"), `gitea_token`(string, required true, sensitive true, title "Gitea 토큰"), `dashboard_bind`(string, default "127.0.0.1", title "대시보드 바인드 주소") 추가. 각 항목에 `description`.
  - `session_start._OPTION_EXPORTS`에 `("CLAUDE_PLUGIN_OPTION_GITEA_URL", "FORGE_LEDGER_GITEA_URL")`, `("CLAUDE_PLUGIN_OPTION_GITEA_TOKEN", "FORGE_LEDGER_GITEA_TOKEN")`, `("CLAUDE_PLUGIN_OPTION_DASHBOARD_BIND", "FORGE_LEDGER_DASHBOARD_BIND")` 추가.
  - `session_start.hub_summary(repo_root: Path, env: Mapping[str, str], now: datetime) -> Optional[str]`: 상태 폴더에 DB가 있고 `repo_root`가 등록돼 있으면 `render_session`, 아니면 `None`(→ 기존 한 줄 요약).
  - `session_start.maybe_refresh(repo_root: Path, env: Mapping[str, str], now: datetime, spawn: Callable[[list[str], dict[str, str]], None]) -> bool`: 등록된 저장소의 마지막 대조 시작이 600초보다 오래됐고 Gitea 또는 Plane 토큰 값이 있으면 `[sys.executable, <plugin>/scripts/ledger.py, "sync", "--repo", <repo_root>, "--quiet"]`를 실행한다. 토큰은 argv가 아니라 env로 넘긴다(`CLAUDE_PLUGIN_OPTION_*` → `FORGE_LEDGER_*` 변환). 기본 spawn은 `subprocess.Popen(..., start_new_session=True, stdin/stdout/stderr=DEVNULL)`.

- [ ] **Step 1: 실패하는 시험 작성**
  - `test_hubcli.py`: `test_status_empty_message`(빈 상태 폴더 → 문구, 0); `test_sync_then_status_json`(가짜 Gitea·Plane, `--repo` 임시 git 저장소 → 0, `status --json`의 `open_issues` 기대값); `test_sync_lock_busy_exit_zero`; `test_sync_failed_exit_two`(Gitea 401); `test_hub_commands_do_not_require_plane_env`(Plane 환경 변수 없이 `status` 0).
  - `test_layout.py`: `userConfig` 키 집합에 세 키 추가, `gitea_token.sensitive is True`, `dashboard_bind.default == "127.0.0.1"`.
  - `test_hooks.py`: `test_exports_include_gitea_and_bind`; `test_session_start_prints_hub_summary_when_registered`(≤1500자); `test_maybe_refresh_spawns_without_token_in_argv`(argv에 토큰 없음, env에 있음); `test_maybe_refresh_skips_when_fresh`; `test_session_start_fail_open_with_corrupt_db`.
- [ ] **Step 2: 실패 확인** — `python3 -m unittest tests.test_hubcli tests.test_layout tests.test_hooks -v`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령, `bash scripts/dev-check.sh`(`claude plugin validate .` 포함)
- [ ] **Step 5: 커밋** — `feat: 허브 sync·status 명령과 SessionStart 요약`

### Task 7: 로컬 대시보드

**Files:**
- Create: `plugins/forge-ledger/ledger/dashboard.py`, `plugins/forge-ledger/dashboard/index.html`, `plugins/forge-ledger/dashboard/app.js`, `plugins/forge-ledger/dashboard/style.css`, `plugins/forge-ledger/commands/dashboard.md`
- Modify: `plugins/forge-ledger/ledger/hubcli.py`(`dashboard` 실행)
- Test: `tests/test_dashboard.py`

**Interfaces:**
- Consumes: `HubStore`, `hubview.summary/repo_summary/health`, `hubsync.sync_paths`, `hubstore.load_registry`
- Produces:
  - `DashboardApp(store: HubStore, token: str, interval_s: int, static_dir: Path, now: Callable[[], datetime])`; `make_server(app: DashboardApp, bind: str, port: int) -> ThreadingHTTPServer`; `serve(app: DashboardApp, bind: str, port: int, sync_once: Callable[[], None], out: TextIO, stop: threading.Event) -> int`
  - 경로와 응답(spec 6.5): `GET /`(index.html), `GET /static/app.js`, `GET /static/style.css`, `GET /api/v1/summary`, `GET /api/v1/repos/{owner}/{name}`(없으면 404), `GET /api/v1/health`. 그 밖 404, GET 외 405. 인증: `/?token=<맞는 값>` → 302 `Location: /` + `Set-Cookie: fl_dash=<토큰>; HttpOnly; SameSite=Strict; Path=/`; 그 뒤 쿠키나 `Authorization: Bearer <토큰>`. 없거나 틀리면 401. 비교는 `hmac.compare_digest`. 모든 응답 헤더: `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'self'`. API는 `Content-Type: application/json; charset=utf-8`. `log_message`는 경로에서 쿼리 문자열을 지우고 stderr JSON 로그(component `dashboard`)로 쓴다.
  - `serve`: 시작 시 `sync_once()` 1회, 이후 `interval_s`마다 데몬 스레드에서 호출(예외는 로그만). 터미널(`out`)에 `대시보드: http://<bind>:<port>/?token=<토큰>`을 한 번 출력. `bind`가 루프백이 아니면 `[!] 사내망에 열린 주소입니다. 접속 토큰을 공유하지 마세요` 한 줄 추가.
  - `hubcli`의 `dashboard`: `--bind`(기본 `env["FORGE_LEDGER_DASHBOARD_BIND"]` 또는 `127.0.0.1`), `--port`(기본 0), `--interval`(기본 600, 60 미만이면 오류 1). 토큰은 `secrets.token_urlsafe(32)`.
  - 화면(`app.js`): `/api/v1/summary`를 60초마다 불러 S1을 그린다. 저장소 카드 안에 Plane 프로젝트별 줄(현재 cycle, 진행 `completed / total`, 게이트 1 `N일 뒤`, 빠진 문서는 회색 `1차에서 제공`, `found=false`면 `프로젝트 없음`), 저장소 공통 칸(열린 결함, 리뷰 대기 PR과 가장 오래된 일수, CI 실패와 `최근 실행 50건 기준`, Penpot 변경·열린 알림은 회색 `1차에서 제공`, 수집 상태 라벨). 공통 상태: 저장소 없음(안내 문구), `stale`(수집 지연 표시), `failed`(연결 실패 표시). DOM은 `createElement`/`textContent`로만 만든다.
  - `commands/dashboard.md`: frontmatter `description: 로컬 대시보드(S1)를 띄우고 접속 주소를 알린다`. 본문: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" dashboard $ARGUMENTS`를 백그라운드로 실행하고, 출력의 `대시보드:` 줄을 사용자에게 그대로 알리며, 멈추라는 요청이 오면 그 프로세스를 끝낸다.

- [ ] **Step 1: 실패하는 시험 작성** (서버를 127.0.0.1 임의 포트로 띄우고 `urllib`로 요청)
  - `test_requires_token`: 토큰 없이 `/api/v1/summary` → 401; Bearer 맞으면 200.
  - `test_token_query_sets_cookie_and_redirects`: 302, `Location: /`, `Set-Cookie`에 `HttpOnly`·`SameSite=Strict`, 쿠키로 다음 요청 200.
  - `test_methods_and_unknown_paths`: POST → 405, `/static/../hubstore.py` → 404, `/nope` → 404.
  - `test_api_is_json_with_nosniff`: 세 보안 헤더와 JSON 형식.
  - `test_repo_endpoint_404_for_unknown`.
  - `test_token_not_in_logs_or_responses`: `?token=` 요청 포함 여러 요청 뒤 로그 출력과 모든 응답 본문에 토큰이 없다(터미널 첫 줄 제외).
  - `test_app_js_never_uses_html_sinks`: `app.js`에 `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval(`가 없다.
  - `test_sync_loop_runs_and_survives_errors`: `interval_s`를 작게 주입(시험 전용 인자)하고 `sync_once`가 처음에 예외를 내도 다음 호출이 일어난다.
  - `test_dashboard_interval_below_minimum`: `--interval 30` → 1.
- [ ] **Step 2: 실패 확인** — `python3 -m unittest tests.test_dashboard -v`
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — 같은 명령, `bash scripts/dev-check.sh`
- [ ] **Step 5: 커밋** — `feat: 로컬 대시보드 S1과 /forge-ledger:dashboard`

### Task 8: 실서버 확인 (controller 전용)

- [ ] `bash scripts/ci-local.sh all` 7개 통과(커버리지 81% 이상).
- [ ] `~/docs/.env`에서 Gitea·Plane 값을 셸에만 설정하고 `ledger.py sync --repo .` → 종료 0.
- [ ] `ledger.py status --json`의 열린 결함, 리뷰 대기 PR, CI 실패, cycle `completed/total`이 Plane·Gitea API를 따로 조회한 값과 같다.
- [ ] `ledger.py dashboard --bind <VPN 주소>`를 띄워 사용자에게 접속 주소를 알리고 페이지 피드백을 받는다(D26).
- [ ] 마지막 작업 항목을 완료로 바꾸고 cycle 진행 8/8이 S1에 보이는지 확인.
