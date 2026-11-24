# PR 자동 검사 설계 (forge-ledger)

- 작성일: 2026-11-24
- 상태: 초안, 사용자 승인 대기
- 대상 저장소: 로컬 Gitea의 `forge-ledger` (비공개)

## 1. 목적

`forge-ledger` 저장소에 PR이 열리거나 새 커밋이 올라오면, 격리된 컨테이너에서 코드 검사와 보안 검사를 자동으로 돌린다. 검사가 하나라도 실패하면 `main` 병합을 막는다. 사람이 만든 변경이든 에이전트가 만든 변경이든 검사를 거치지 않고는 `main`에 들어가지 않게 하는 것이 목적이다.

## 2. 사용자가 정한 것

1. 정해진 도구(린트, 시험, 비밀값 검사)에 더해, 가상환경을 만들고 더 많은 도구를 연결해 점검한다.
2. 검사가 실패하면 병합을 막는다.
3. 이 저장소에만 적용한다.
4. 도구 준비 방식은 C: 무거운 바이너리 도구는 검사 전용 이미지에 넣고, Python 도구는 실행할 때 venv(Python 가상환경)에 설치한다.
5. 도구 목록은 아래 표를 그대로 쓴다.

| 분류 | 도구 | 준비 위치 |
|---|---|---|
| 린트·형식 | `ruff check`, `ruff format --check` | venv |
| 타입 | `mypy` (strict) | venv |
| 시험 | `unittest` + `coverage` | venv |
| 플러그인 규격 | `claude plugin validate` | 이미지 |
| 비밀값 | `gitleaks`(커밋 이력 포함), forge-ledger 자체 판정기(`ledger.secrets`) | 이미지, 저장소 코드 |
| 정적 보안 분석(SAST, 소스 코드에서 위험한 패턴을 찾는 검사) | `semgrep`, `bandit` | 이미지, venv |
| 의존성 취약점 | `pip-audit` | venv |
| 셸 스크립트 | `shellcheck` | 이미지 |
| 워크플로 파일 | `actionlint` | 이미지 |
| 파일 전체 점검 | `trivy fs` | 이미지 |

## 3. 목표, 비목표, 반목표

목표
- PR 화면에 검사 작업별 성공·실패가 표시되고, 실패하면 병합 버튼이 막힌다.
- 관리자 계정(저장소 소유자)도 실패한 검사를 무시하고 병합할 수 없다.
- `main`에 직접 push해서 검사를 우회할 수 없다.

비목표
- 다른 저장소나 Gitea 전체 적용
- AI가 변경 내용을 읽고 리뷰 의견을 다는 기능
- 웹훅 수신 서버 제작(설계서 v4 비목표 "코드 리뷰 도구를 만들지 않습니다"와 충돌)
- 배포(CD), 허브 대시보드 연동

반목표(기술적으로 돌아가도 이러면 실패)
- PR 코드가 서버 본체, 호스트 docker 소켓, `~/docs/.env`에 닿는다.
- 러너 등록 토큰, Gitea 주소, IP 같은 민감값이 저장소 파일에 들어간다.
- 도구 버전이 자동으로 바뀌어, 같은 코드가 어느 날 갑자기 실패한다.
- 검사 결과가 로컬 `scripts/dev-check.sh` 결과와 다르다(같은 도구, 같은 버전이어야 한다).

## 4. 제약과 확인한 사실 (2026-11-24)

- Gitea 1.27.2, 호스트에서 systemd로 실행 중이다. Actions는 전역 기본값으로 켜져 있다(소스 `modules/setting/actions.go` 41줄). 하지만 이 저장소는 Actions가 꺼져 있고(`has_actions: false`), 등록된 러너는 0개다.
- Gitea Actions는 `pull_request` 이벤트로 실행되고(소스 `modules/actions/github.go` 11·93줄), 작업마다 커밋 상태를 만든다(소스 `services/actions/commit_status.go` 29–31줄). 상태 이름 형식은 `<워크플로 이름> / <작업 이름> (<이벤트>)`이다(소스 `WorkflowStatusContextName`).
- 브랜치 보호 API는 `enable_status_check`, `status_check_contexts`, `block_admin_merge_override`를 지원한다(소스 `modules/structs/repo_branch.go` 56–70줄).
- 저장소 범위 러너 등록 토큰: `POST /api/v1/repos/{owner}/{repo}/actions/runners/registration-token` (소스 `routers/api/v1/api.go` 1004·1262줄). 저장소 Actions를 켜야 쓸 수 있다.
- 러너: `gitea/runner`(이전 이름 `act_runner`). 2026-11-24 기준 최신 v6.0.1(2026-11-20 출시), v6.0.0(2026-11-11).
  - `container.docker_host: "-"`이면 작업 컨테이너에 docker 소켓을 넘기지 않는다.
  - `privileged`는 기본값이 `false`다.
  - 이미지가 로컬에 있으면 기본적으로 다시 받지 않는다.
  - 출처: `config.example.yaml` 246·283–290줄.
- 서버: 8코어, RAM 15GB 중 가용 약 6GB다. Plane, Penpot 등 다른 서비스가 함께 돌고 있다.
- 플러그인 실행 환경: 호스트 `python3`는 3.9.25이고, ruff와 mypy 대상도 py39다. 로컬 `.venv`는 Python 3.9.25, ruff 0.6.9, mypy 1.11.2다. 버전 고정 파일은 아직 없다.
- 최신 semgrep 1.186.0, bandit 1.9.4, pip-audit 2.10.1, mypy 2.4.1은 Python 3.10 이상을 요구한다. coverage 7.17.5는 3.11 이상을 요구한다.
- 실행 의존성은 0개다(표준 라이브러리만 씀). 의존성 취약점 검사의 대상은 CI 도구 자신이다.
- 현재 코드에서 `ruff format --check`를 돌리면 7개 파일이 실패한다.
- `claude plugin validate .`는 자격 증명이 없는 빈 HOME에서도 통과한다(Claude Code 2.1.331, 종료 코드 0). CI에 계정 정보를 넘길 필요가 없다.
- Gitea의 기본 액션 주소는 github.com이다(소스 `modules/setting/actions.go` 42줄). 서버 설정이라 저장소 단위로 바꿀 수 없다.
- 서버에서 Docker Hub, github.com, PyPI에 접속된다. 컨테이너 안에서 Gitea 내부 주소로 HTTP 200을 받았다.
- 민감값(IP, 접속 주소, 토큰, 계정)은 계획, 코드, 기록에 넣지 않는다. Gitea 주소와 토큰은 `~/docs/.env`의 `GITEA_URL_LOCAL`, `GITEA_USERNAME`, `GITEA_TOKEN`에서 읽는다.

## 5. 방식 비교

| 방식 | 내용 | 결과 |
|---|---|---|
| A | 매번 작업 컨테이너에서 venv를 만들고 모든 도구를 설치 | 이미지 관리는 없지만 실행마다 설치 시간과 네트워크가 든다 |
| B | 모든 도구를 넣은 검사 전용 이미지 | 빠르고 재현되지만 도구를 바꿀 때마다 재빌드해야 한다 |
| C (선택) | 바이너리 도구는 이미지, Python 도구는 실행 때 venv | 속도와 관리 부담의 중간. 사용자 선택 |

제외한 방식
- 웹훅 수신 서버 직접 제작: 수신, 실행, 결과 회신을 새로 만들어야 하고 설계서 v4의 비목표와 충돌한다.
- 러너 호스트 모드: PR 코드가 서버 본체에서 바로 실행된다.

## 6. 구성

### 6.1 러너

- `gitea/runner`를 버전 고정 태그로 docker compose를 써서 호스트에 1개 띄운다. 재시작 정책은 `unless-stopped`다.
- 저장소 범위로 등록한다. 이 저장소의 작업만 받는다.
- 러너 설정에서 고정할 값
  - `container.docker_host: "-"`: 작업 컨테이너에 docker 소켓을 넘기지 않는다.
  - `container.privileged: false`
  - `runner.capacity: 1`: 메모리를 아끼려고 작업을 한 번에 하나씩 돌린다.
  - 작업 컨테이너 자원 제한(`container.options`의 `--memory`, `--cpus`)
- 러너 자신은 작업 컨테이너를 만들기 위해 호스트 docker 소켓을 쓴다. 이 위험은 러너 컨테이너에만 있고, PR 코드에는 넘어가지 않는다.
- 저장소에 두는 파일: `ci/runner/compose.yml`, `ci/runner/config.yaml`. 비밀값과 주소는 넣지 않는다.
  - Gitea 주소와 등록 토큰은 등록할 때 환경 변수로만 넘긴다.
  - 등록 결과 파일(`.runner`)은 저장소 밖 호스트 경로에 둔다.
  - 등록 토큰은 등록 직후 버린다.

### 6.2 검사 전용 이미지

- `ci/Dockerfile`로 빌드하고 태그는 `forge-ledger-ci:<정수>`로 붙인다. 레지스트리에는 올리지 않는다. 러너와 같은 호스트 docker에서 빌드하므로 작업이 로컬 이미지를 바로 쓴다.
- 이미지에 들어가는 것
  - Python 3.9(시험, ruff, mypy, coverage용; 호스트 실행 환경과 같게)
  - Python 3.10 이상(semgrep, bandit, pip-audit용)
  - Node LTS(`actions/checkout`, `actions/cache`, Claude CLI 실행용), git
  - 버전 고정 바이너리: gitleaks, trivy, shellcheck, actionlint. 내려받을 때 체크섬을 확인한다.
  - semgrep은 이미지 안 별도 venv에 둔다. 규칙 묶음은 빌드할 때 받아 이미지에 넣고, 실행 중에는 외부에서 받지 않는다.
  - trivy가 쓰는 점검 규칙도 빌드할 때 넣고, 실행 중에는 갱신하지 않는다.
  - Claude CLI(`@anthropic-ai/claude-code`)는 버전을 고정하고 `claude plugin validate`에만 쓴다.
- 빌드 스크립트 `scripts/ci-image-build.sh`
  - `ci/Dockerfile`과 이미지에 들어가는 파일들의 해시를 계산해, 빌드 인자로 이미지 안 파일(`/etc/forge-ci/inputs.sha256`)에 기록한다.
  - `lint` 작업 첫 단계가 저장소 쪽 해시와 이 파일을 비교한다. 다르면 "검사 이미지 재빌드 필요"로 실패한다. Dockerfile만 바꾸고 이미지를 다시 빌드하지 않은 채 검사가 옛 이미지로 통과하는 일을 막기 위해서다.

### 6.3 Python 도구 (실행 때 venv)

- 버전 고정 파일은 `--require-hashes`로 해시까지 고정한다.
  - `ci/requirements-py39.txt`: ruff 0.6.9, mypy 1.11.2(로컬 `.venv`와 같은 버전), coverage(3.9를 지원하는 마지막 버전)
  - `ci/requirements-py3x.txt`: bandit, pip-audit
- 설치 파일은 러너 캐시(`actions/cache`)에 보관한다.
- 로컬 `scripts/dev-check.sh`도 같은 고정 파일로 `.venv`를 만들 수 있게 한다. 그래야 로컬과 CI 결과가 같다.

### 6.4 워크플로 `.gitea/workflows/pr-check.yml`

- 이름: `pr-check`
- 실행 조건: `main`을 대상으로 하는 `pull_request`의 `opened`, `synchronize`, `reopened`
- 작업 토큰 권한은 읽기만 준다(`contents: read`).
- 모든 작업은 `container: forge-ledger-ci:<정수>`에서 돈다. 저장소는 `fetch-depth: 0`으로 받는다.
- `actions/checkout`과 `actions/cache`는 Gitea 기본 액션 주소(github.com)에서 받는다. 태그(`@v4`)가 아니라 커밋 해시로 고정해 버전이 저절로 바뀌지 않게 한다.

| 작업 | 단계 |
|---|---|
| `lint` | 이미지 해시 비교, `ruff check`, `ruff format --check`, `mypy`, `shellcheck`(저장소의 `*.sh`), `actionlint` |
| `test` | `coverage run -m unittest discover -s tests -t .`, 최소 비율 확인, `claude plugin validate .` |
| `secrets` | `gitleaks`(전체 커밋 이력), 자체 판정기를 PR 변경 파일에 적용 |
| `sast` | `semgrep`(이미지에 넣은 규칙, 발견 시 실패), `bandit` |
| `deps` | `pip-audit`(고정 파일 전부), `trivy fs`(설정·비밀값 점검) |

- 상태 이름 5개는 `pr-check / lint (pull_request)`, `pr-check / test (pull_request)`, `pr-check / secrets (pull_request)`, `pr-check / sast (pull_request)`, `pr-check / deps (pull_request)`다.
- 시험 고정 데이터(`tests/test_secrets.py`와 `tests/fixtures/`)에는 일부러 넣은 가짜 비밀값이 있다. gitleaks 허용 목록(`.gitleaks.toml`)에 경로를 등록한다. 자체 판정기는 `tests/` 밖의 변경 파일에만 적용한다.
- 커밋 이력은 다시 쓰지 않는다(강제 push 금지). 경로 허용으로 덮이지 않는 과거 발견은 `.gitleaksignore`에 지문(fingerprint)과 사유를 적어 하나씩 예외 처리한다. 실제 비밀값으로 판단되면 예외 처리하지 않고 사용자에게 보고한다. 토큰 폐기는 사용자가 정한다.

### 6.5 병합 차단

`main` 브랜치 보호 규칙
- `enable_push: false`: 직접 push를 금지한다.
- `enable_status_check: true`
- `status_check_contexts`: 위의 상태 이름 5개
- `block_admin_merge_override: true`: 관리자 우회를 금지한다.
- 필수 승인 수는 0이다. 혼자 작업하는 저장소라서다.

## 7. 도입 순서

1. 기존 코드 정리
   - `ruff format`으로 7개 파일을 형식만 바꾸는 커밋을 따로 만든다.
   - bandit, semgrep, gitleaks, trivy, shellcheck를 로컬에서 한 번 돌린다. 실제 문제는 고치고, 오탐은 줄 단위 예외에 사유를 붙인다.
2. 버전 고정 파일, `ci/Dockerfile`, 빌드 스크립트, 워크플로, `.gitleaks.toml`, 러너 파일을 쓰고 이미지를 빌드한다.
3. 저장소 Actions를 켜고(`has_actions: true`) 러너를 등록해 띄운다.
4. 이 작업을 PR로 올려 5개 작업이 모두 통과하는지 확인한다. 그 PR 커밋의 실제 상태 이름을 API(`GET /api/v1/repos/{owner}/{repo}/commits/{sha}/statuses`)로 읽어 둔다. 6단계 보호 규칙에는 설계서의 문자열이 아니라 이 실제 값을 넣는다. 이름이 한 글자라도 다르면 규칙이 끝없이 기다려서 모든 병합이 막히기 때문이다.
5. 일부러 실패하는 시험 PR을 만든다. 작업이 실패로 표시되는지 확인한 뒤 그 PR은 닫는다.
6. 브랜치 보호를 적용한다. 관리자 계정으로 실패한 PR을 병합하려 하면 거부되는지, `main`에 직접 push하면 거부되는지 확인한다.

Gitea 설정 변경(Actions 켜기, 러너 등록 토큰, 보호 규칙)은 토큰이 필요하므로 메인 세션이 직접 한다. 하위 에이전트에게는 토큰을 넘기지 않는다.

## 8. 운영

- 러너가 멈추면 PR을 병합할 수 없다. 상태 확인과 재시작 명령은 `ci/runner/README.md`에 적는다.
- 도구를 바꾸려면 고정 파일이나 Dockerfile의 버전을 올리는 PR을 만들고, 이미지 태그 정수를 올린 뒤 재빌드한다. 해시 비교 단계가 재빌드를 빠뜨리지 않게 막는다.
- 비상시에는 사용자가 Gitea 웹에서 보호 규칙을 잠시 끈다. 끈 사실은 `.ledger/history.md`나 PMT에 남긴다.

## 9. 빌더 재량

- 이미지 안에서 Python 3.9와 3.10 이상을 함께 두는 방법(uv 등)
- semgrep 규칙 묶음 선택(Python, 보안 감사, 셸 범위에서)
- coverage 최소 비율 값(현재 측정값을 내림해서 쓴다)
- trivy와 semgrep을 외부 다운로드 없이 돌리는 구체 방법
- 작업 컨테이너 자원 제한 값(가용 메모리 6GB 안에서)
- 러너 버전(v6 계열 중 안정판)

## 10. 정한 순서

- PR #1(`p1-method-core` → `main`)을 먼저 병합했다(2026-11-24, 사용자 선택 A, 병합 커밋 `c4c5eb0`). 병합 결과에서 `scripts/dev-check.sh`가 통과했다.
- 이 작업은 `main` 위로 옮긴 `ci-pr-check` 브랜치에서 하고, 별도 PR로 올린다. CI 검사는 저장소 전체를 대상으로 하므로, 1부 코드도 이 PR에서 처음 검사를 받는다.
