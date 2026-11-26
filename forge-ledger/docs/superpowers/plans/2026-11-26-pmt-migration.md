# PMT 이전과 작업 이어받기 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PMT 형식 프로젝트를 매니페스트 → 미리 보기 → apply로 Plane·git에 옮기고, `ledger work`·`ledger next`로 어느 컴퓨터에서든 작업을 이어받게 하며, 저장소 전체에 노출 검사 가드를 건다.

**Architecture:** 노출 패턴은 `ledger/secrets.py` 한 곳에 두고 CI 스크립트·pre-commit 훅·CLI 쓰기·매니페스트 검사가 모두 이를 쓴다. 이전은 `ledger/migrate.py`(매니페스트 형식·검사·미리 보기)와 `ledger/migrate_apply.py`(git 쪽·Plane 쪽 쓰기, 적용 기록)로 나눈다. 작업 명령은 `ledger/work.py`가 `adapters.WorkTracker` 규격만 보고 동작하며 `PlaneClient`가 그 규격을 구현한다.

**Tech Stack:** Python 표준 라이브러리만, pytest, Plane REST API v1, git CLI.

**Spec:** `forge-ledger/docs/superpowers/specs/2026-11-26-pmt-migration-design.md`

## Global Constraints

- IP, 접속 주소, 토큰, 계정 같은 민감값은 계획·코드·테스트·커밋 메시지에 넣지 않는다. 테스트의 가짜 값은 `.exposure-allow`에 사유와 함께 올린다.
- 외부 패키지를 더하지 않는다. 플러그인 코드는 Python 3.10~3.13, `ci/` 스크립트와 그것이 import하는 `ledger/secrets.py`는 `python3.9`로도 돈다(`ci/run-checks.sh`가 `python3.9`로 실행).
- 완료 조건: `bash ci/run-checks.sh all`(검사 7개 lint·test·secrets·sast·deps·matrix·install) 통과. 사용자 메시지·오류는 한국어.
- push·배포·GitHub 쓰기는 사용자가 명시적으로 요청할 때만 한다.
- Plane 원천 표식: `external_source = "forge-ledger-pmt"`, `external_id = <원천 키>`.
- 원천 키 형식: `pmt:<slug>/<PMT 안 상대 위치>` (예: `pmt:forge-wiki-plugin/_default/Work5-1`, `pmt:forge-wiki-plugin/D5`, `pmt:forge-wiki-plugin/resources/derived/a.drawio`, 댓글은 `<항목 키>#c<순번>`).
- 상수: `CLAIM_STALE_MINUTES = 30`, 인계 필드 최대 `400`자, `ledger next` 출력 최대 `1500`자.
- 매니페스트 위치: `${XDG_STATE_HOME:-~/.local/state}/forge-ledger/migrations/<slug>.json`, 적용 기록: 같은 폴더 `<slug>.applied.json`. 둘 다 커밋하지 않는다.
- 치환 기록(`redactions`)과 검사 출력에는 원래 값을 절대 넣지 않는다. 출력은 위치·패턴 이름만.
- 결정은 지우지 않는다. 상태는 `승인`·`대체`·`폐기`만 쓴다.

## Review Focus

1. 매니페스트 댓글 본문에 홈 경로가 섞인 경우 → `migrate check`가 레코드 키와 패턴 이름만 출력하고 실패한다(값 미출력). Task 4에서 고정.
2. `apply` 도중 Plane 5xx로 멈춘 뒤 다시 실행 → 중복 없이 나머지만 만든다. Task 6에서 고정.
3. 이전 뒤 Plane에서 고친 항목에 다시 `apply` → 덮어쓰지 않고 충돌로 보고한다. Task 6에서 고정.
4. 두 컴퓨터가 30분 안에 같은 항목을 `work start` → 뒤에 온 쪽은 거부, 경합으로 둘 다 기록되면 늦은 쪽이 되돌린다. Task 7에서 고정.
5. 저장소에 이진 파일·UTF-8이 아닌 파일이 있을 때 → 스캐너가 건너뛰고 죽지 않는다. Task 1에서 고정.

---

### Task 1: 노출 패턴 확장과 `ci/scan_exposure.py`

**Files:**
- Modify: `forge-ledger/plugins/forge-ledger/ledger/secrets.py`
- Create: `forge-ledger/ci/scan_exposure.py`
- Delete: `forge-ledger/ci/scan_changed_secrets.py` (기능을 `--range`로 흡수)
- Modify: `forge-ledger/ci/run-checks.sh` (`job_secrets`가 `python3.9 ci/scan_exposure.py --range "$base..$head"` 호출)
- Create: `.exposure-allow`(최상위) — 이 Task의 테스트 파일에 대한 가짜 값 허용 규칙과 사유만 담는다
- Test: `forge-ledger/tests/test_secrets.py`, `forge-ledger/tests/test_scan_exposure.py` (기존 `test_ci_scripts.py`의 scan_changed_secrets 테스트는 옮긴다)

**Interfaces:**
- Produces: `secrets.find_exposures(text: str) -> list[str]` (걸린 패턴 이름 목록, 중복 없음, 정의 순서). 기존 `find_secrets`는 `find_exposures`의 별칭으로 남긴다.
- Produces: `secrets.PATTERN_NAMES: tuple[str, ...]` = 기존 7개 + `home_path`, `email`, `internal_host`, `private_key`, `jwt`, `secret_assignment`.
- Produces: `scan_exposure.load_allow(path: Path) -> list[AllowRule]` (`AllowRule(path_glob: str, pattern: str, reason: str)`), `scan_exposure.scan_text(rel: str, text: str, rules) -> list[Finding]` (`Finding(path: str, line: int, pattern: str)`), CLI `--staged | --range A..B | --all | --history | --stdin [--label NAME]`, 종료 코드 0 통과·1 발견·2 사용 오류.
- `.exposure-allow` 형식: 저장소 최상위 파일, 한 줄에 `<경로 glob>\t<패턴 이름>\t<사유>`, `#` 주석, 사유 비면 사용 오류.

- [ ] **Step 1: 실패하는 테스트 작성** — `test_secrets.py`에 패턴별 양성·음성:
  - `home_path`: `"/home/alice/x"`, `"/data/home/u/a"`, `"/Users/bob/a"`, `"C:\\Users\\bob"` 양성, `"~/docs"`, `"/home"` 단독 음성.
  - `email`: `"a.b@corp.co.kr"` 양성, `"dev@example.com"`, `"noreply@github.com"`, `"x@noreply.example"` 음성.
  - `internal_host`: `"build.lan"`, `"svc.internal:8080"`, `"pc.local"` 양성, `"localhost"` 음성.
  - `private_key`: `"-----BEGIN OPENSSH PRIVATE KEY-----"` 양성.
  - `jwt`: `eyJ`로 시작하는 점 3분할 base64url 문자열 양성.
  - `secret_assignment`: `"API_TOKEN=abcd1234efgh"`, `"password: hunter2hunter2"` 양성, `"TOKEN=${PLANE_TOKEN}"`, `"token = os.environ[...]"`, `"PASSWORD="` 음성(값이 변수 참조이거나 8자 미만).
  - `test_scan_exposure.py`: 이진 파일(널 바이트 포함)·UTF-8 아닌 파일은 건너뜀, 허용 규칙(glob+패턴)이 맞으면 무시, 사유 빈 규칙은 종료 2, 출력에 값이 없고 `path:line:pattern` 형식만, `--stdin --label plane:FL-3`은 `plane:FL-3:<line>:<pattern>` 출력.
- [ ] **Step 2: 실패 확인** — `cd forge-ledger && python3 -m pytest tests/test_secrets.py tests/test_scan_exposure.py -q` → FAIL.
- [ ] **Step 3: 구현** — 패턴은 `secrets.py`의 `PATTERNS`에 더한다(3.9 호환 문법). 스캐너는 `git rev-parse --show-toplevel` 기준으로 경로를 다루고 `tests/` 일괄 제외를 없앤다. `--history`는 `git log -p --all --format=commit:%H` 의 추가 줄(`+`)만 보고 `commit:path:pattern`을 출력한다.
- [ ] **Step 4: 통과 확인** — 같은 명령 PASS, `bash ci/run-checks.sh lint` PASS, `bash ci/run-checks.sh secrets` PASS.
- [ ] **Step 5: 커밋** — `git commit -m "feat(ci): 노출 패턴 확장과 scan_exposure 스크립트"`

### Task 2: 가드 적용(저장소 전체·훅·규칙)과 이력 보고

**Files:**
- Modify: `.exposure-allow`
- Create: `.githooks/pre-commit`, `scripts/install-hooks.sh` (모두 `j-ai-plugin` 최상위)
- Modify: `AGENTS.md`(최상위) 규칙 절, `forge-ledger/ci/run-checks.sh` `job_secrets`(gitleaks `--relative -- .` 범위는 그대로, scan_exposure는 최상위 전체)
- Modify: 저장소 전체 `--all` 검사에서 걸린 파일(고치거나 허용 목록에 사유와 함께)
- Test: `forge-ledger/tests/test_ci_scripts.py`

**Interfaces:**
- Consumes: Task 1 CLI.
- Produces: `.githooks/pre-commit`은 `python3 forge-ledger/ci/scan_exposure.py --staged`를 실행하고 실패 시 커밋을 막는다. `scripts/install-hooks.sh`는 `git config core.hooksPath .githooks`만 한다.

- [ ] **Step 1: 실패하는 테스트** — `test_ci_scripts.py`: 임시 git 저장소에서 훅 설치 후 홈 경로가 든 파일을 커밋하면 실패(종료≠0), 허용 목록에 올리면 성공. `AGENTS.md`에 문장 `민감값 금지, 커밋 전 검사 통과 필수, 예외는 사유와 함께 .exposure-allow에`가 있는지.
- [ ] **Step 2: 실패 확인** — `python3 -m pytest tests/test_ci_scripts.py -q` FAIL.
- [ ] **Step 3: 구현과 전체 점검** — 훅·스크립트·규칙을 쓰고 `python3 forge-ledger/ci/scan_exposure.py --all`을 최상위에서 실행한다. 걸린 줄은 실제 값이면 고치고(환경 변수 이름·`~`로), 설명용 예시(설계서·테스트의 패턴 예시)는 파일 단위 허용 규칙과 사유를 단다. 실제 값을 허용 목록에 올리지 않는다.
- [ ] **Step 4: 통과 확인** — `--all` 종료 0, `bash ci/run-checks.sh secrets` PASS, 테스트 PASS.
- [ ] **Step 5: 이력 보고(controller 전용)** — `--history` 결과를 작업 폴더 밖 임시 파일에 저장해 사용자에게 개수·커밋·경로·패턴만 보고한다(D35). 이력 수정은 하지 않는다.
- [ ] **Step 6: 커밋** — `git commit -m "chore: 저장소 전체 노출 검사 가드와 pre-commit 훅"`

### Task 3: Plane 쓰기 확장과 `WorkTracker` 규격

**Files:**
- Modify: `forge-ledger/plugins/forge-ledger/ledger/plane.py`, `ledger/adapters.py`, `tests/fakes.py`(`FakePlane`, `FakePlaneServer`)
- Test: `forge-ledger/tests/test_plane.py`

**Interfaces:**
- Produces (`PlaneClient`와 `PlaneLike`에 추가):
  - `list_states(project_id) -> list[dict]` (각 `id`, `name`, `group`)
  - `list_labels(project_id) -> list[dict]`
  - `create_item(project_id, name, description_html, label_ids, *, parent: str | None = None, state: str | None = None, external_source: str | None = None, external_id: str | None = None) -> dict` (기존 호출과 호환)
  - `update_item(project_id, item_id, fields: dict[str, Any]) -> dict` (`state`·`assignees`·`labels`·`parent`·`description_html`·`external_source`·`external_id`만 허용, 그 밖은 `ValueError`)
  - `find_item_by_external(project_id, source, external_id) -> dict | None` — 조회 방식은 실서버로 확인해 정한다(필터 질의가 되면 그것, 아니면 `list_items` 후 거름). 확인 결과를 보고서에 적는다.
  - `list_comments(project_id, item_id) -> list[dict]` (각 `id`, `comment_html`, `created_at`, `actor`)
  - `create_comment(project_id, item_id, html) -> dict`
  - `resolve_code(code: str) -> tuple[str, dict]` (`"FL-12"` → (project_id, item); 프로젝트 `identifier`와 `sequence_id`로 찾음, 없으면 `PlaneError("항목 없음: FL-12")`)
- Produces (`adapters.py`): `class WorkTracker(Protocol)`: `whoami() -> str`, `resolve_code`, `update_item`, `list_comments`, `create_comment`, `list_states`, `list_items`, `list_labels`, `create_label`.

- [ ] **Step 1: 실패하는 테스트** — `FakePlaneServer` 대상: 각 메서드의 경로·메서드·본문(예: `create_item(..., parent="P", external_id="pmt:s/W1")` 요청 본문에 `parent`·`external_source`·`external_id` 포함), `update_item`에 허용 밖 키 → `ValueError`, `resolve_code("FL-12")` 성공·없음, 댓글 생성·목록.
- [ ] **Step 2: 실패 확인** — `python3 -m pytest tests/test_plane.py -q` FAIL.
- [ ] **Step 3: 구현** — 기존 요청·재시도·리다이렉트 거부 경로를 그대로 쓴다. `FakePlane`(메모리)도 같은 메서드를 갖춘다.
- [ ] **Step 4: 통과 확인** — PASS, `ci/run-checks.sh lint` PASS.
- [ ] **Step 5: 커밋** — `git commit -m "feat(plane): 상태·댓글·부모·원천 표식 쓰기와 WorkTracker 규격"`

### Task 4: 매니페스트 형식·`migrate check`·`migrate preview`

**Files:**
- Create: `forge-ledger/plugins/forge-ledger/ledger/migrate.py`
- Modify: `ledger/cli.py` (`migrate check|preview|apply <파일>` 하위 명령, apply는 Task 6에서 연결)
- Test: `forge-ledger/tests/test_migrate.py`

**Interfaces:**
- 매니페스트 JSON v1 (이 형식이 스킬·apply의 계약이다):

```json
{"version": 1, "slug": "forge-wiki-plugin", "source_dir": "~/docs/projects/forge-wiki-plugin",
 "records": [
  {"key": "pmt:forge-wiki-plugin/project", "kind": "project", "body_md": "...", "redactions": []},
  {"key": "pmt:forge-wiki-plugin/_default/Work5", "kind": "work", "name": "...", "status": "Done", "labels": ["work"], "description_html": "...", "link_to": null, "redactions": []},
  {"key": "pmt:forge-wiki-plugin/_default/Work5-1", "kind": "item", "parent": "pmt:forge-wiki-plugin/_default/Work5", "name": "...", "status": "Done", "labels": ["kind:job"], "blocked_by": [], "description_html": "...", "link_to": null, "redactions": ["홈 경로 → ~"]},
  {"key": "pmt:forge-wiki-plugin/_default/Work5-1#c1", "kind": "comment", "item": "pmt:forge-wiki-plugin/_default/Work5-1", "at": "2026-11-25T17:20", "body_html": "...", "redactions": []},
  {"key": "pmt:forge-wiki-plugin/B3", "kind": "backlog", "name": "...", "status": "Active", "labels": ["backlog", "kind:req"], "description_html": "...", "link_to": null, "redactions": []},
  {"key": "pmt:forge-wiki-plugin/D5", "kind": "decision", "date": "2026-11-23", "status": "대체", "title": "...", "detail": "...", "replaced_by": "pmt:forge-wiki-plugin/D9", "redactions": []},
  {"key": "pmt:forge-wiki-plugin/F1", "kind": "fact", "date": "2026-11-23", "status": "Active", "text": "...", "ref": "...", "redactions": []},
  {"key": "pmt:forge-wiki-plugin/resources/derived/a.drawio", "kind": "resource", "path": "derived/a.drawio", "redactions": []}
 ]}
```

- 허용 status: work·item `Planned|In Progress|Done|Paused|Failed|Skipped`, backlog `Active|Done|폐기`, decision `승인|대체|폐기`, fact `Active|폐기`.
- Produces: `load_manifest(path: Path) -> Manifest`, `check_manifest(m: Manifest, source_dir: Path) -> list[Problem]` (`Problem(key: str, message: str)`), `render_preview(m: Manifest, plan: ApplyPlan) -> str`, `manifest_path(slug: str, env: Mapping[str, str]) -> Path`.
- 검사 규칙: version=1, 키 유일, 키 접두 `pmt:<slug>/`, kind·status 허용값, `parent`·`item`·`replaced_by`·`link_to`가 가리키는 키 존재(`link_to`는 Plane ID 문자열이라 존재 검사 제외), Work 아래 깊이 ≤3, 모든 문자열 필드와 resource 파일 내용에 `find_exposures` 적용(파일은 `source_dir/resources/<path>`), resource 경로에 `..` 금지.

- [ ] **Step 1: 실패하는 테스트** — 정상 견본 통과, 각 규칙 위반마다 `Problem.key`가 해당 키, 댓글 본문의 `/home/alice` → 메시지에 `home_path` 포함·`alice` 미포함, `replaced_by`가 없는 키 → 문제, `render_preview`에 종류별 개수·`link_to` 목록·치환 목록·새로 만들 것/바꿀 것/그대로 둘 것 개수.
- [ ] **Step 2: 실패 확인** — `python3 -m pytest tests/test_migrate.py -q` FAIL.
- [ ] **Step 3: 구현** — `ApplyPlan`은 Task 6이 채우는 자료형(`create: list[str]`, `update: list[str]`, `skip: list[str]`, `conflict: list[str]`)으로 여기서 정의만 한다. preview는 Plane·git을 읽기만 한다.
- [ ] **Step 4: 통과 확인** — PASS.
- [ ] **Step 5: 커밋** — `git commit -m "feat(migrate): 매니페스트 형식과 check·preview"`

### Task 5: apply — git 쪽(project·결정·사실·resources)

**Files:**
- Create: `forge-ledger/plugins/forge-ledger/ledger/migrate_apply.py`, `ledger/facts.py`
- Modify: `ledger/flows.py` (`decide`의 Plane·history 쓰기를 `record_decision(repo, client, entry: HistoryEntry, body_html: str) -> HistoryEntry`로 빼고 `decide`가 이를 호출)
- Test: `forge-ledger/tests/test_migrate_apply_git.py`, `tests/test_facts.py`, `tests/test_flows.py`

**Interfaces:**
- Produces: `facts.py` — `.ledger/facts.md` 표 `| ID | 상태 | 생성 | 내용 | 출처 |`, `parse_facts(text) -> list[Fact]`, `upsert_fact(path, fact: Fact) -> str`("created"|"updated"|"same"), `next_fact_id(facts) -> str`(`F<n+1>`).
- Produces: `apply_git(m: Manifest, repo: Repo, client: PlaneLike, state: AppliedState) -> list[Outcome]` (`Outcome(key, action: "created"|"updated"|"same"|"conflict", target: str)`).
- Produces: `AppliedState` — `load(path)`, `save(path)`, `get(key) -> AppliedEntry | None`, `put(key, AppliedEntry(target: str, applied_at: str, digest: str))`.
- 규칙: project → `.ledger/project.md`. 결정 → 원래 날짜로 `new_decision_id`, `detail`에 `원래 ID: <slug> D5 · 상태: 대체 · 대체: <새 ID>` 포함, 대체 관계는 같은 실행 안에서 새 ID로 다시 잇는다(두 단계: ID 먼저 배정 후 기록). 사실 → 원래 ID 유지. resource → `.ledger/resources/pmt/<path>` 바이트 그대로. 같은 키가 이미 적용됐고 현재 파일 내용 digest가 적용 당시와 다르면 `conflict`로 건너뛴다.
- `apply`는 현재 브랜치가 `migrate/<slug>`가 아니면 `FlowError("브랜치 migrate/<slug>에서 실행하세요")`.

- [ ] **Step 1: 실패하는 테스트** — 임시 저장소+`FakePlane`: 결정 3개(D1 대체→D2, D3 폐기)의 새 ID 날짜가 원래 날짜, D1 detail에 D2의 새 ID, 두 번째 실행은 모두 `same`, 적용 뒤 `.ledger/facts.md`를 손으로 고치면 다음 실행에서 `conflict`, 다른 브랜치에서 실행 시 `FlowError`, 기존 `decide` 테스트 계속 통과.
- [ ] **Step 2: 실패 확인** — 관련 테스트 FAIL.
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — `python3 -m pytest tests -q` PASS.
- [ ] **Step 5: 커밋** — `git commit -m "feat(migrate): git 쪽 apply와 facts.md"`

### Task 6: apply — Plane 쪽(Work·Item·댓글·백로그)과 `migrate apply`

**Files:**
- Modify: `ledger/migrate_apply.py`, `ledger/cli.py`
- Test: `forge-ledger/tests/test_migrate_apply_plane.py`

**Interfaces:**
- Consumes: Task 3 메서드, Task 4 `Manifest`·`ApplyPlan`, Task 5 `AppliedState`·`apply_git`.
- Produces: `apply_plane(m, tracker: PlaneLike, project_id: str, state: AppliedState) -> list[Outcome]`, `plan_apply(m, repo, tracker, project_id, state) -> ApplyPlan`, `run_apply(path: Path, repo, tracker, env) -> int`.
- 상태 대응: Planned→`unstarted`, In Progress→`started`, Done→`completed`, Paused→`unstarted`+라벨 `paused`, Failed→`cancelled`+`failed`, Skipped→`cancelled`+`skipped`; 백로그 Active→`backlog`, Done→`completed`, 폐기→`cancelled`. 그룹 안 첫 상태를 쓴다. 없는 라벨은 만든다.
- `blocked_by`가 비어 있지 않으면 설명 맨 위에 `<p>차단: <원천 키 목록></p>`, 라벨 `blocked`.
- 순서: Work → Item(부모 먼저) → 댓글(시각순) → 백로그. 항목은 `find_item_by_external`로 먼저 찾고, `link_to`가 있으면 그 항목에 `update_item`으로 `external_source`·`external_id`만 단다.
- 댓글 표식: 본문 끝 `<p data-fl-key="<키>"></p>`; 저장 후 표식이 사라지면 `<p>fl:<키></p>`로 바꾼다(실서버 확인 결과를 보고서에 적음). 같은 표식 댓글이 있으면 건너뜀.
- 충돌: 적용 기록의 `applied_at`보다 Plane `updated_at`이 늦으면 `conflict`, 쓰지 않음.
- 매 레코드 성공 직후 `AppliedState.save` (중단 후 재개).
- `run_apply`: `check_manifest` 통과 필수 → `apply_git` → `apply_plane` → 종류별 결과 표 출력. 충돌이 있으면 종료 3.

- [ ] **Step 1: 실패하는 테스트** — `FakePlane`: 전체 견본 적용 후 개수·부모·상태·라벨·external 표식, 두 번째 실행 create 0, 5번째 항목에서 5xx를 던지게 한 뒤 재실행 시 중복 없음, 적용 후 Plane 항목 `updated_at`을 늦추면 conflict·종료 3, `link_to` 레코드는 새로 만들지 않고 기존 항목에 표식만.
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — `python3 -m pytest tests -q` PASS.
- [ ] **Step 5: 커밋** — `git commit -m "feat(migrate): Plane 쪽 apply와 migrate apply 명령"`

### Task 7: `ledger work start|note|verify|end`

**Files:**
- Create: `forge-ledger/plugins/forge-ledger/ledger/work.py`
- Modify: `ledger/cli.py`, `plugins/forge-ledger/commands/`(필요 시 `work.md`)
- Test: `forge-ledger/tests/test_work.py`

**Interfaces:**
- Consumes: `WorkTracker`(Task 3), `secrets.find_exposures`.
- Produces: `start(t: WorkTracker, code, host, now, take=False) -> str`, `note(t, code, did, next_, now) -> str`, `verify(t, code, cmd, exit_code, head, now, limits=None) -> str`, `end(t, code, mode: "done"|"pause"|"fail"|"skip", now, head, result=None, unverified=None, cause=None, fix=None, reason=None) -> str`.
- 댓글 첫 줄 형식(정확히): `[착수] <host> <ISO 시각>`, `[인계] did: … / next: …`, `[검증] head=<7자> exit=<N> cmd=<명령>`, `[종료] <done|pause|fail|skip> …`, `[착수 취소] <host> <ISO 시각>`.
- 점유 판정: 담당자가 나 아님 + 마지막 `[착수]`·`[인계]`·`[검증]` 댓글이 `CLAIM_STALE_MINUTES` 이내 → 거부(`WorkError("점유 중: <담당자> <분>분 전")`). 지났으면 `take=True`일 때만 넘겨받음.
- 경합(D36): `[착수]` 기록 후 댓글을 다시 읽어 30분 안에 다른 host의 `[착수]`가 내 것보다 먼저면, 담당자를 원래대로 되돌리고 `[착수 취소]`를 남긴 뒤 `WorkError`.
- `note` 각 필드 400자 초과 → `WorkError`. 모든 댓글 본문은 `find_exposures` 통과 필수.
- `end done`: 현재 HEAD와 같은 `[검증]` 댓글이 없으면 `unverified` 필수. 상태 대응은 Task 6과 같다.

- [ ] **Step 1: 실패하는 테스트** — 정상 한 바퀴(start→note→verify→end done) 상태·댓글 순서, 다른 host가 10분 전 착수 → 거부, 40분 전 → `take` 없으면 거부·있으면 성공, 경합 시뮬레이션(가짜 트래커가 내 기록 직후 더 이른 시각의 다른 `[착수]`를 끼워 넣음) → 되돌림·`[착수 취소]`, 401자 → 거부, 홈 경로 포함 note → 거부, HEAD 다른 검증만 있을 때 done → `unverified` 요구.
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — `python3 -m pytest tests -q` PASS.
- [ ] **Step 5: 커밋** — `git commit -m "feat(work): 착수·인계·검증·종료 명령"`

### Task 8: `ledger next`, `ledger fact add`, SessionStart 연결

**Files:**
- Modify: `ledger/work.py`, `ledger/cli.py`, `plugins/forge-ledger/hooks/session_start.py`
- Test: `forge-ledger/tests/test_work.py`, `tests/test_hooks.py`

**Interfaces:**
- Produces: `render_next(t: WorkTracker, project_id, repo: Repo, now) -> str` — 순서: 점유 중(담당·마지막 인계 한 줄), 착수 가능(Planned·차단 없음·자식 없는 Work 포함), 최근 결정 3(`history.md`), 최근 사실 3(`facts.md`). 최대 1500자, 넘으면 각 절 뒤쪽부터 줄이고 `… 외 N건`.
- Produces: `fact_add(repo, text, ref, today) -> str`(새 ID 반환, 노출 검사 통과 필수).
- SessionStart: `.ledger/`가 있고 Plane 토큰이 있으면 기존 요약 뒤에 `render_next` 결과를 붙인다. 전체 출력 상한은 기존 1500자 규칙을 지킨다. Plane 오류 시 한 줄 경고만.

- [ ] **Step 1: 실패하는 테스트** — 항목 30개 견본에서 출력 ≤1500자·`외 N건` 포함, 절 순서, 자식 없는 Planned Work 포함, `fact add`가 `F34` 반환(기존 33개일 때), Plane 오류 시 훅 종료 0.
- [ ] **Step 2: 실패 확인**
- [ ] **Step 3: 구현**
- [ ] **Step 4: 통과 확인** — `python3 -m pytest tests -q` PASS.
- [ ] **Step 5: 커밋** — `git commit -m "feat(work): next 요약·fact add·SessionStart 연결"`

### Task 9: 이전 스킬 `/forge-ledger:migrate`

**Files:**
- Create: `plugins/forge-ledger/skills/ledger-migrate/SKILL.md`, `plugins/forge-ledger/commands/migrate.md`
- Test: `forge-ledger/tests/test_skill.py`, `tests/test_layout.py`

**Interfaces:**
- 스킬 본문 필수 내용: 설계서 8절 절차 8단계(순서 그대로), 5절 대응표, Task 4 매니페스트 형식, 치환 규칙(주소·토큰 → 환경 변수 이름, 홈 경로 → `~`, `redactions`에 원래 값 금지), 멈춤 조건(점유 존재, check 실패, 사용자 미승인), push·PMT 종료는 사용자 동의 후에만.

- [ ] **Step 1: 실패하는 테스트** — 기존 스킬 테스트 방식으로 frontmatter(`name`, `description`) 존재, 본문에 `migrate check`·`migrate preview`·`migrate apply`·`redactions`·`pmt end` 문자열 포함, 명령 파일이 스킬을 가리킴.
- [ ] **Step 2~4: 작성·통과 확인** — `python3 -m pytest tests/test_skill.py tests/test_layout.py -q` PASS, `bash ci/run-checks.sh all` PASS.
- [ ] **Step 5: 커밋** — `git commit -m "feat(skill): PMT 이전 스킬 ledger-migrate"`

### Task 10: 실이전과 확인 (controller 전용)

- [ ] **Step 1:** PR 브랜치를 Gitea에 올리고 검사 7개 통과 후 병합(기존 흐름). GitHub push는 사용자 요청 때.
- [ ] **Step 2:** `migrate/forge-wiki-plugin` 브랜치에서 스킬 절차로 매니페스트 작성 → `migrate check` 통과.
- [ ] **Step 3:** `migrate preview`를 사용자에게 보이고 승인을 받는다. **승인 전에는 apply하지 않는다.**
- [ ] **Step 4:** `migrate apply` → 개수 대조(설계서 12절 1번), 재실행 create 0(2번), `scan_exposure.py --all` 통과(3번).
- [ ] **Step 5:** 임시 폴더에 새로 클론, 토큰 설정, `ledger next` 출력이 이 서버와 같은지 대조(4번).
- [ ] **Step 6:** 실서버 Plane에서 `work start → note → verify → end` 한 바퀴(5번).
- [ ] **Step 7:** Gitea PR 생성·검사 통과. 병합·GitHub push·`pmt end forge-wiki-plugin --done --confirm`은 사용자 동의 후.
