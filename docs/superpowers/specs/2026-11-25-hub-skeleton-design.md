# 허브 골격(로컬 방식) 설계 (forge-ledger 2부)

- 작성일: 2026-11-25
- 상태: 초안, 사용자 승인 대기
- 대상 저장소: 로컬 Gitea의 `forge-ledger`, 브랜치 `hub-skeleton`
- 상위 설계: `docs/design/forge-ledger-design.html`(설계서 v4.2). 이 문서는 v4.2의 "프로토타입 2부 골격"을 2026-11-25 결정 D22–D27에 맞게 고친 것이다. 둘이 다르면 이 문서가 우선한다.

## 1. 목적과 이유

플러그인을 설치한 사용자가 토큰만 입력하면 프로젝트 관리 도구(이하 PM 도구)와 git 호스팅(이하 포지)의 상태를 한 화면에서 보게 한다. 최종 목표는 작업하는 컴퓨터나 작업자가 바뀌어도 PM 도구와 git에 남은 내용만으로 작업을 이어 가는 것이다(D24). 이번 골격은 그 화면과 수집 기반을 만든다. 기존 PMT 프로젝트를 옮기는 기능(D23)은 다음 계획이다(D27).

## 2. 사용자가 정한 것 (PMT 결정)

| 결정 | 내용 |
|---|---|
| D22 (D4 대체) | 대시보드는 로컬 기본 + 허브 선택. 같은 `ledger/` 엔진이 로컬 방식(사용자 토큰으로 주기 조회, 로컬 SQLite는 다시 만들 수 있는 사본)과 허브 방식(허브 주소 설정 시)으로 동작한다. 골격은 로컬 방식부터 |
| D23 | 기존 PMT 형식 프로젝트 이전은 정리 프롬프트(스킬)로 모델이 PMT·PM 도구·git·로컬을 조사해 대신 작성한다. 쓰기는 플러그인 CLI로 한다(다음 계획) |
| D24 (D21 대체) | 작업 목록·상태·인계 메모·점유는 PM 도구 작업 항목, 결정·사실·환경 이름은 git `.ledger/`, 실제 주소·토큰은 작업자별 로컬 설정. 토큰은 공유하지 않는다 |
| D25 | 골격은 Plane과 Gitea만 구현한다. 다른 서비스(Linear, GitHub, GitLab 등)는 연결 규격만 정의하고 배포 전에 연결·시험한다 |
| D26 | 첫 화면은 설계서 S1 그대로(진행, 결함, PR, CI, 수집 상태). 실제 페이지를 본 뒤 사용자가 추가 피드백한다 |
| D27 | 이번 계획은 허브 골격만. 실제 페이지는 `forge-ledger` 저장소(Gitea 실데이터)와 `ledger init`으로 만든 Plane 프로젝트로 확인한다 |
| 방식 A | 페이지는 필요할 때 띄우는 로컬 웹 서버(`ledger dashboard`)로 제공한다. 안에서 10분마다 대조하고, 실행마다 새 접속 토큰을 쓴다 |
| D28 | 대시보드는 에이전트 명령 `/forge-ledger:dashboard`로 세션 안에서 띄운다. 접속 토큰이 든 주소가 대화 기록에 남는 것을 받아들인다(서버가 도는 동안에만 유효) |
| D29 | 실서버 확인용 cycle 데이터는 이 계획의 작업들이다. `forge-ledger` 유지보수 Plane 프로젝트에 작업 항목으로 만들고 날짜 있는 cycle "허브 골격"에 넣어 작업이 끝날 때마다 상태를 바꾼다. `ledger init`이 만든 `.ledger/`는 `hub-skeleton` 브랜치에 커밋한다 |

## 3. 목표, 비목표, 반목표

목표
1. `ledger sync`가 저장소 하나를 등록하고 Gitea와 Plane을 API로 대조해 로컬 SQLite에 담는다.
2. `ledger dashboard`가 S1 개요 페이지와 읽기 전용 조회 API를 제공하고, 띄워 둔 동안 10분마다 다시 대조한다.
3. `ledger status`가 같은 수치를 터미널 글로 보여 준다.
4. SessionStart 훅이 로컬 SQLite에서 1,500자 이하 요약을 출력한다.
5. 플러그인 설정에서 Gitea 주소와 토큰을 입력받는다.
6. PM 도구와 포지 어댑터의 연결 규격(Python Protocol)을 정의하고 Plane·Gitea가 그것을 구현한다.

비목표(이번에 하지 않음)
- 웹훅 수신, 허브 컨테이너 배포, 허브 방식 클라이언트
- `event`, `entity_change`, `link`, `alert`, `rule`, `ledger_entry`, `plan_doc` 테이블과 어긋남 규칙 M1–M13
- S2–S10 화면, Gitea 로그인(OAuth), Telegram 알림, Penpot 어댑터
- 보관 기간에 따른 삭제(D10)
- Linear, GitHub, GitLab 구현
- PMT 이전과 인계 메모 쓰기 명령
- 설계서 v4.2 HTML 개정(D22–D27 반영은 다음 설계서 개정에서)

반목표(이렇게 되면 실패)
- 대시보드나 수집이 Plane·Gitea에 쓰기를 한다. 골격의 수집·조회 경로는 전부 읽기 전용이다.
- 토큰이 SQLite, 로그, 페이지, 저장소 파일에 남는다.
- 수집이나 훅 실패가 Claude Code 세션 시작을 막는다(fail-open 유지).
- S1 수치가 Plane·Gitea 화면 수치와 다르다.

## 4. 제약

- Python 3.9 이상 표준 라이브러리만 쓴다(기존 원칙). 웹은 `http.server.ThreadingHTTPServer` + 정적 HTML/JS, 빌드 단계 없음.
- 호출 한도: Plane 분당 50회(기존 `make_shared_limiter`, 공식 60회), Gitea 분당 120회(같은 공유 제한기, 다른 버킷).
- 주소·IP·토큰은 코드, 문서, 기록에 쓰지 않는다. 바인드 주소는 실행 옵션으로만 받는다.
- 기존 PR 검사 7개(lint, test, secrets, sast, deps, matrix, install)를 모두 통과해야 한다. 커버리지 하한 81%.
- 상태 파일은 기존 상태 폴더 규칙(`FORGE_LEDGER_STATE_DIR` → `$XDG_STATE_HOME/forge-ledger` → `~/.local/state/forge-ledger`)을 따른다.

## 5. 고른 방식 (Approaches)

| 안 | 내용 | 결과 |
|---|---|---|
| A 로컬 웹 서버 | `ledger dashboard`가 S1과 조회 API를 제공하고 안에서 주기 대조 | 채택. 설계서 기술 선택과 같고, 같은 코드를 나중에 허브 컨테이너로 띄울 수 있다(D22) |
| B 정적 HTML 파일 | `ledger dashboard --out`으로 데이터 내장 파일 생성 | 미채택. 갱신이 수동이고 원격 서버 파일을 내려받아야 하며 허브 방식과 코드 경로가 갈라진다 |
| C A + 내보내기 | A에 파일 내보내기 추가 | 미채택. 지금 필요 없는 시험 범위가 늘어난다 |

## 6. 구성

### 6.1 설정과 토큰

- `plugin.json` `userConfig`에 `gitea_url`(문자열, 필수), `gitea_token`(문자열, 필수, `sensitive: true`), `dashboard_bind`(문자열, 기본 `127.0.0.1`)을 더한다. 바인드 주소는 작업자별 로컬 설정이므로 저장소에 들어가지 않는다(D24).
- SessionStart 훅이 기존 Plane 값처럼 `CLAUDE_ENV_FILE`에 `FORGE_LEDGER_GITEA_URL`, `FORGE_LEDGER_GITEA_TOKEN`을 내보낸다.
- CLI는 환경 변수에서만 토큰을 읽는다. 토큰이 없으면 그 소스만 건너뛰고 "토큰 없음"으로 기록한다.

### 6.2 연결 규격 (`ledger/adapters.py`)

PM 도구와 포지를 서비스 종류와 무관하게 다루는 Protocol 두 개를 정의한다. 골격에 필요한 읽기 메서드만 둔다.

- `PmAdapter`: `whoami()`, `list_projects()`, `current_cycle(project_id)`(이름, 시작·종료일, 전체·완료 수)
- `ForgeAdapter`: `whoami()`, `get_repo(owner, name)`, `list_open_issues(owner, name)`, `list_open_pulls(owner, name)`, `list_runs(owner, name, limit)`

확인된 응답 근거 [검증]
- Plane v1.5.0: `GET .../cycles/?cycle_view=current`는 `start_date <= 지금 <= end_date`인 cycle을 돌려준다(`plane/api/views/cycle.py` 203줄). `CycleSerializer`에 `total_issues`, `completed_issues`가 있다(`plane/api/serializers/cycle.py` 124–126줄). 실행 중인 `plane-api` 컨테이너 소스로 확인했다.
- Gitea 1.27.2: `GET /repos/{owner}/{repo}/actions/runs`는 `total_count`와 `workflow_runs`를 주고, 실행마다 `path`(`<워크플로 파일>@<ref>`, 예: `pr-check.yml@refs/pull/5/head`), `head_sha`, `event`, `status`, `conclusion`, `run_number`, `pull_requests[].number`가 온다. `head_branch`는 비어 있다. 이슈·PR 목록은 `X-Total-Count` 헤더를 준다. 이 서버의 API로 확인했다.

Plane 구현은 기존 `PlaneClient`에 읽기 메서드를 더해 만든다. Gitea 구현은 새 `ledger/gitea.py`의 `GiteaClient`다(`Authorization: token` 헤더, 목록 수는 `X-Total-Count` 헤더 사용, 페이지 크기 50). 다른 서비스는 이 규격만 지키면 되고 구현은 배포 전에 붙인다(D25).

### 6.3 저장소 등록과 대조 (`ledger sync`)

1. `ledger sync [--repo PATH]`(기본 현재 폴더)는 그 저장소를 등록한다.
   - Gitea 소유자/이름은 `git remote get-url origin`의 경로 부분에서 얻는다. 호스트는 비교하지 않고 API는 `FORGE_LEDGER_GITEA_URL`로 부른다.
   - `.ledger/ledger.yml`이 있으면 `plane.workspace`와 붙은 Plane 프로젝트 키를 읽는다. 없으면 Plane 줄 없이 Gitea만 모은다.
   - 등록 목록은 DB 밖 `<상태 폴더>/hub-repos.json`(권한 0600)에 로컬 경로로 남는다. DB를 다시 만들어도 이 파일로 다시 등록한다. 같은 원격을 두 곳에 clone하면 저장소는 하나(`owner/name` 기준)이고 로컬 경로만 여러 개다.
2. 등록된 모든 저장소를 대조한다(`--repo`를 주면 그 저장소만). 대조할 때마다 `ledger.yml`을 다시 읽는다. 경로가 사라진 저장소는 건너뛰고 "경로 없음"으로 기록한다.
3. (소스, 저장소)마다 `sync_run` 한 줄을 남긴다: 시작·끝 시각, 결과(`ok`, `partial`, `failed`, `skipped`), API 호출 수, 실패 종류.
4. 엔터티는 `(source_id, kind, ext_key)`로 upsert한다. 조회는 종류(kind)별로 따로 하고 종류마다 하나의 트랜잭션으로 반영한다. 성공한 종류는 반영하고, 이번 "열린 목록"에 없는 그 종류의 열린 엔터티는 `state='closed'`로 바꾼다. 실패한 종류의 기존 행은 건드리지 않는다. 일부 종류만 실패하면 결과는 `partial`이다.
5. 동시에 대조가 두 번 돌지 않게 상태 폴더의 잠금 파일(`fcntl`, 기존 `ratelimit.py` 방식)로 막는다. 잠금을 못 얻으면 "다른 대조가 진행 중" 한 줄을 출력하고 종료 코드 0으로 끝낸다.

모으는 엔터티(`entity.kind`)

| kind | 원천 | attrs에 두는 값 |
|---|---|---|
| `repo` | Gitea 저장소 | full_name, default_branch, html_url |
| `issue` | 열린 Gitea 이슈(`type=issues`) | number, title, labels, created_at |
| `pull` | 열린 Gitea PR | number, title, draft, created_at |
| `ci_run` | 최근 Actions 실행 50건 | run_number, event, status, conclusion, workflow(`path`의 `@` 앞), ref(`path`의 `@` 뒤), head_sha, pull_numbers, started_at |
| `plane_project` | `ledger.yml`에 붙은 Plane 프로젝트 | key, title, default, id |
| `cycle` | 각 Plane 프로젝트의 현재 cycle(`cycle_view=current`) | name, start_date, end_date, total_issues, completed_issues |

### 6.4 S1 수치 규칙

화면과 API, `status`, SessionStart는 모두 SQLite만 읽는 같은 계산 함수(`ledger/hubview.py`)를 쓴다.

| 수치 | 규칙 |
|---|---|
| 현재 cycle 진행 | Plane cycle API의 `completed_issues / total_issues`를 그대로 쓴다. Plane 화면과 같은 값이 되게 하기 위함이다. 현재 cycle이 없으면 "cycle 없음" |
| 게이트 1 예정 | 현재 cycle `end_date`까지 남은 날 수. cycle이 없으면 `-` |
| 열린 결함 (Gitea) | 열린 이슈 수(PR 제외). D2에 따라 Gitea 이슈는 실제 문제만 담는다 |
| 리뷰 대기 PR | 열린 PR 중 draft가 아닌 것의 수와 가장 오래된 것의 경과 일수 |
| CI 실패 | 단위는 (대상, 워크플로)다. 대상은 열린 PR(`pull_numbers`에 그 번호가 있는 실행) 또는 기본 브랜치(`ref = refs/heads/<default_branch>`이고 `event=push`). 단위마다 완료된(`status=completed`) 실행 중 `run_number`가 가장 큰 것을 보고, 그 `conclusion`이 `failure`면 1로 센다. 진행 중인 실행은 무시하고 직전 완료 결과를 쓴다. 최근 50건 밖의 실행은 보지 않으며, 화면에 "최근 실행 50건 기준"을 적는다 |
| 수집 상태 | (소스, 저장소)마다 결과가 `ok`나 `partial`인 마지막 `sync_run`의 끝 시각과 그 결과. 그 시각이 (대조 주기 + 5분)보다 오래됐으면 "수집 지연"(기본 15분), 마지막 실행이 `failed`면 "연결 실패", `partial`이면 "일부 실패" |
| 빠진 문서, Penpot 변경, 열린 알림, 미분류 | 이번에는 계산하지 않고 "1차에서 제공"으로 표시 |

### 6.5 로컬 웹 서버 (`ledger dashboard`)

- 진입점: 슬래시 명령 `/forge-ledger:dashboard`(`commands/dashboard.md`). 에이전트가 `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" dashboard`를 백그라운드로 실행하고, 출력된 접속 주소를 사용자에게 알린다. 서버는 그 세션이 끝나거나 사용자가 멈추라고 할 때까지 돈다. 같은 CLI는 `ledger.py dashboard`로 직접 실행할 수도 있다.
- 옵션: `--bind`(기본은 환경 변수 `FORGE_LEDGER_DASHBOARD_BIND`, 없으면 `127.0.0.1`. SessionStart가 `dashboard_bind` 설정을 이 변수로 내보낸다), `--port`(기본 0이면 빈 포트), `--interval`(기본 600초, 최소 60초).
- 시작 시 한 번 대조하고, 이후 `--interval`마다 백그라운드 스레드가 대조한다. 대조 실패는 로그만 남기고 서버는 계속 돈다.
- 접속 보호: 실행마다 `secrets.token_urlsafe(32)` 접속 토큰을 만들어 터미널에 접속 주소와 함께 한 번 출력한다. 첫 방문 `/?token=...`이 맞으면 `HttpOnly; SameSite=Strict` 쿠키를 주고 토큰 없는 `/`로 리다이렉트한다. API는 같은 쿠키나 `Authorization: Bearer <토큰>`을 받는다. 없거나 틀리면 401. 비교는 `hmac.compare_digest`.
- 경로: `GET /`(S1 HTML), `GET /static/<허용 목록 파일>`, `GET /api/v1/summary`, `GET /api/v1/repos/{owner}/{name}`, `GET /api/v1/health`. 그 밖의 경로는 404, GET 외 메서드는 405.
- 모든 응답에 `Cache-Control: no-store`, `X-Content-Type-Options: nosniff`, `Content-Security-Policy: default-src 'self'`를 붙인다.
- 페이지는 정적 HTML/JS가 API를 불러 그린다. 와이어프레임은 설계서 S1을 따르되, 6.4의 "1차에서 제공" 칸은 회색으로 둔다. 공통 상태(데이터 없음, 수집 지연, 소스 연결 실패)를 표시한다.

### 6.6 SessionStart 요약

- `CLAUDE_PROJECT_DIR` 저장소가 SQLite에 등록돼 있으면 그 저장소 요약을 1,500자 이하로 출력한다: Plane 프로젝트별 cycle 진행, 열린 결함, 리뷰 대기 PR, CI 실패, 수집 상태와 마지막 수집 시각.
- 마지막 수집이 10분보다 오래됐고 토큰이 있으면 `ledger sync --repo <저장소>`를 분리된 백그라운드 프로세스로 시작한다(잠금으로 중복 방지). 이번 출력은 기존 값을 쓴다.
- 등록되지 않았거나 SQLite가 없으면 지금의 한 줄 요약을 그대로 출력한다.
- 어떤 오류든 종료 코드 0(fail-open).

### 6.7 `ledger status`

등록된 저장소마다 6.4 수치를 글로 출력한다. `--json`이면 `/api/v1/summary`와 같은 JSON을 출력한다.

### 6.8 저장소

- 파일: `<상태 폴더>/hub.sqlite3`, 권한 0600(상태 폴더 0700). WAL 모드.
- 테이블: `source`(id, kind, base_url_digest), `repo`(id, owner, name, default_branch, local_paths JSON, plane_workspace, registered_at), `entity`(id, source_id, repo_id, kind, ext_key, state, parent_id, attrs JSON, first_seen_at, updated_at), `sync_run`(id, source_id, repo_id, started_at, finished_at, result, api_calls, error_kind). `base_url_digest`는 `bucket_name` 방식의 해시이며 주소 원문과 토큰은 저장하지 않는다.
- 스키마 버전은 `PRAGMA user_version`으로 둔다. 버전이 다르면 파일을 지우고 다시 만든 뒤 `hub-repos.json`으로 다시 등록한다(사본이므로 안전, NFR-06).

### 6.9 로그

대조 로그는 설계서 구현 4의 형식을 따른다. 한 줄에 JSON 하나를 stderr로 쓴다: `ts`, `level`, `component`(`sync`, `dashboard`), `source`, `action`, `result`, `duration_ms`, `correlation_id`(sync_run id). 토큰, 이슈 제목 같은 본문은 쓰지 않는다.

## 7. 완료 기준

1. 단위 시험: 가짜 Plane·Gitea 서버(`tests/fakes.py` 방식)에 고정 데이터를 두고 6.4 수치가 기대값과 같다. 접속 토큰 없는 요청은 401, POST는 405, 허용 목록 밖 정적 파일은 404.
2. SessionStart 출력이 1,500자 이하이고, SQLite가 깨졌거나 없을 때도 종료 코드 0이다.
3. SQLite 파일, 로그, API 응답에 시험용 토큰 문자열이 없다(시험으로 확인).
4. `scripts/ci-local.sh all` 7개 검사 통과, 커버리지 81% 이상.
5. 실서버 확인(D29): `forge-ledger` 저장소에 `ledger init`을 실행하고, 이 계획의 작업들을 유지보수 Plane 프로젝트의 작업 항목으로 만들어 날짜 있는 cycle "허브 골격"에 넣는다. 작업이 끝날 때마다 항목 상태를 바꾼다. `ledger sync` 뒤 `ledger status --json` 수치(cycle 진행 포함)가 Plane·Gitea API를 따로 조회한 값과 같다. 사용자가 `/forge-ledger:dashboard`로 띄운 페이지를 브라우저로 열어 확인한다.

## 8. 작성자가 정한 것 (사용자 확인 대상)

1. 열린 결함은 라벨과 무관하게 열린 Gitea 이슈 전체로 센다(D2 근거).
2. cycle 진행은 Plane이 주는 수치를 그대로 쓴다. 계획서 항목(D20)은 cycle에 넣지 않으므로 따로 빼지 않는다.
3. 미분류 수는 1차로 미룬다(D15 흐름이 1차 범위).
4. SessionStart가 오래된 데이터를 보면 백그라운드 대조를 시작한다.
5. 바인드 기본값은 `127.0.0.1`이고, 다른 주소는 `dashboard_bind` 설정이나 `--bind`로만 연다.
6. 스키마가 바뀌면 SQLite를 지우고 다시 만든다. 등록 목록은 DB 밖 `hub-repos.json`에 둬서 잃지 않는다.
7. CI 실패는 (대상, 워크플로) 단위로, 완료된 가장 최근 실행만 본다. 최근 50건 밖은 보지 않는다.
8. "수집 지연" 기준은 대조 주기 + 5분(기본 15분)이다. 설계서 v4.2의 10분은 웹훅을 받는 허브 기준이라 주기 조회와 맞지 않는다. SessionStart의 재대조 기준은 10분 그대로 둔다.
9. 부분 실패는 종류별로 반영한다. 성공한 종류는 쓰고 실패한 종류는 기존 값을 둔다.
10. cycle이 없는 Plane 프로젝트는 "cycle 없음"으로만 보여 준다. 와이어프레임의 "상시 5 / 9" 표시는 1차로 미룬다.

## 9. 빌더에게 맡기는 것

- 모듈 나누기와 함수 이름(위에 적은 파일·명령 이름은 지킨다)
- S1 HTML/CSS 세부 모양(와이어프레임 구성은 지킨다)
- 가짜 서버 시험 데이터의 구체 값
- 페이지 크기·재시도 같은 호출 세부(한도는 지킨다)
