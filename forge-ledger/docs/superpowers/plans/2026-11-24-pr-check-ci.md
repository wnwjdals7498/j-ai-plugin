# PR 자동 검사 CI 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `forge-ledger` 저장소의 `main` 대상 PR마다 격리 컨테이너에서 코드·보안 검사 5개 작업을 돌리고, 하나라도 실패하면 병합을 막는다.

**Architecture:** 호스트에 저장소 전용 Gitea 러너 1개를 docker compose로 띄운다. 작업은 로컬에서 빌드한 검사 이미지 `forge-ledger-ci:<TAG>`(바이너리 도구 포함) 안에서 돌고, Python 도구는 해시 고정 파일로 실행 때 venv에 설치한다. 실제 검사 명령은 `ci/run-checks.sh` 한 곳에 두어 CI와 로컬(`scripts/ci-local.sh`)이 같은 명령을 쓴다. `main` 브랜치 보호 규칙이 상태 5개를 필수로 요구한다.

**Tech Stack:** Gitea 1.27.2 Actions, `gitea/runner` 6.0.1, Docker, Python 3.9(시험·린트)·3.12(보안 도구), uv, bash, unittest

**Spec:** `docs/superpowers/specs/2026-11-24-pr-check-ci-design.md`

## Global Constraints

- 민감값(IP, 접속 주소, 토큰, 계정)은 저장소 파일·커밋 메시지·보고서에 넣지 않는다. Gitea 주소와 토큰은 `~/docs/.env`(`GITEA_URL_LOCAL`, `GITEA_USERNAME`, `GITEA_TOKEN`)에만 있고, 하위 에이전트에게 넘기지 않는다. Gitea API·설정 변경은 Task 7에서 메인 세션(controller)만 한다.
- 커밋 이력은 다시 쓰지 않는다. force push 금지.
- 저장소 코드 규칙: 표준 라이브러리만, `from __future__ import annotations`, Python 3.9 호환, ruff line-length 120, mypy strict, 시험은 `unittest`(`python3 -m unittest discover -s tests -t .`).
- 버전 고정 값(자동 갱신 금지):

| 대상 | 값 |
|---|---|
| venv py3.9 | `ruff==0.6.9`, `mypy==1.11.2`, `coverage==7.10.7` |
| venv py3.12 | `bandit==1.9.4`, `pip-audit==2.10.1` |
| 이미지 기반 | `python:3.12-slim-bookworm` |
| 이미지 안 도구 | uv 0.13.7, Python 3.9.25(uv로 설치), Node v26.13.1, gitleaks 8.30.1, trivy 0.76.2, shellcheck 0.11.0, actionlint 1.7.12, semgrep 1.186.0, `@anthropic-ai/claude-code@2.1.331` |
| 러너 이미지 | `gitea/runner:6.0.1` |
| 액션 | `actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1`(v7.0.1), `actions/cache@caa296126883cff596d87d8935842f9db880ef25`(v5.1.0) |

- 이미지 태그의 단일 원천은 `ci/image/TAG` 파일(내용 `1`)이다. 워크플로와 러너 라벨의 태그는 이 값과 같아야 한다.
- 이미지 입력 해시 기록 위치: 이미지 안 `/etc/forge-ci/inputs.sha256`. 불일치 메시지: `검사 이미지 재빌드 필요: bash scripts/ci-image-build.sh`
- 워크플로: 파일 `.gitea/workflows/pr-check.yml`, `name: pr-check`, 트리거 `pull_request` `types: [opened, synchronize, reopened]` `branches: [main]`, 최상위 `permissions: contents: read`, 작업 ID `lint`, `test`, `secrets`, `sast`, `deps`, 모두 `runs-on: forge-ci`, `container: forge-ledger-ci:<TAG>`.
- 러너 설정: `container.docker_host: "-"`, `container.privileged: false`, `runner.capacity: 1`, `container.options: "--memory=3g --cpus=4"`, `container.network: forge-ci-net`, `container.force_pull: false`, 러너 이름 `forge-ledger-ci`, 라벨 `forge-ci:docker://forge-ledger-ci:<TAG>`.
- 브랜치 보호(`main`): `enable_push: false`, `enable_status_check: true`, `status_check_contexts`는 Task 7에서 읽은 실제 상태 이름 5개, `block_admin_merge_override: true`, 필수 승인 0.
- 시험 고정 데이터(`tests/`)의 가짜 비밀값은 비밀값 검사 대상에서 경로로만 제외한다. 새 시험의 가짜 비밀값은 `"plane_api_" + "a" * 32`처럼 문자열 이어 붙이기로 만든다.
- 커밋 메시지는 한국어, 접두어 `feat:`/`fix:`/`test:`/`chore:`/`style:`/`docs:`.

## Review Focus

1. 러너 컨테이너가 재시작·서버 재부팅된 뒤에도 다시 온라인이 되어야 한다(아니면 모든 PR이 영원히 대기) → Task 7 Step 9.
2. Dockerfile이나 `ci/image/` 파일만 바꾸고 이미지를 다시 빌드하지 않은 PR은 옛 이미지로 통과하면 안 되고 `lint`가 재빌드 메시지로 실패해야 한다 → Task 2 시험, Task 4 Step 6.
3. 삭제·이름 변경·바이너리 파일이 섞인 PR에서 자체 비밀값 검사가 멈추지 않고, 삭제 파일은 건너뛰고 새 경로는 검사해야 한다 → Task 2 시험.
4. PyPI·GitHub 등 외부 네트워크 장애 때 검사는 통과가 아니라 실패로 끝나야 한다(fail closed) → Task 4 Step 6.
5. 보호 규칙의 필수 상태 이름이 실제 이름과 한 글자라도 다르면 모든 병합이 영원히 막힌다. 관리자 계정도 실패한 PR을 병합할 수 없어야 한다 → Task 7 Step 6–8.

---

### Task 1: Python 도구 고정, 로컬 준비 스크립트, 형식 정리

**Files:**
- Create: `ci/requirements-py39.in`, `ci/requirements-py39.txt`, `ci/requirements-py3x.in`, `ci/requirements-py3x.txt`, `scripts/dev-setup.sh`
- Modify: `scripts/dev-check.sh`, 그리고 `ruff format`이 바꾸는 파일 7개

**Interfaces:**
- Produces: `ci/requirements-py39.txt`, `ci/requirements-py3x.txt`(해시 고정, `--require-hashes`로 설치 가능). `scripts/dev-setup.sh`(인자 없음, `.venv`를 만들고 py39 고정 파일 설치). `scripts/dev-check.sh`는 `ruff format --check .`를 포함한다.

- [ ] **Step 1: 입력 파일 작성** — `ci/requirements-py39.in`에 `ruff==0.6.9`, `mypy==1.11.2`, `coverage==7.10.7`. `ci/requirements-py3x.in`에 `bandit==1.9.4`, `pip-audit==2.10.1`.
- [ ] **Step 2: 해시 고정 파일 생성**

Run:
```bash
uv pip compile --generate-hashes --python-version 3.9 ci/requirements-py39.in -o ci/requirements-py39.txt
uv pip compile --generate-hashes --python-version 3.12 ci/requirements-py3x.in -o ci/requirements-py3x.txt
```
Expected: 두 파일 모두 모든 줄에 `--hash=sha256:`가 있다.

- [ ] **Step 3: `scripts/dev-setup.sh` 작성** — `set -euo pipefail`. `.venv`가 없으면 `python3 -m venv .venv`, 그다음 `.venv/bin/pip install --require-hashes -r ci/requirements-py39.txt`. 끝에 `.venv/bin/ruff --version`과 `.venv/bin/mypy --version`을 출력한다.
- [ ] **Step 4: 실행해 버전 확인**

Run: `bash scripts/dev-setup.sh`
Expected: `ruff 0.6.9`, `mypy 1.11.2` 출력. `.venv/bin/coverage --version`은 `7.10.7`.

- [ ] **Step 5: 형식 정리 커밋 분리**

Run: `.venv/bin/ruff format . && .venv/bin/ruff format --check .`
Expected: `30 files already formatted`(또는 전체 파일 수). 형식만 바뀐 파일을 따로 커밋한다.
```bash
git add -u && git commit -m "style: ruff format 적용"
```

- [ ] **Step 6: `scripts/dev-check.sh`에 `.venv/bin/ruff format --check .`를 `ruff check` 다음 줄에 추가하고 전체 확인**

Run: `bash scripts/dev-check.sh`
Expected: 종료 코드 0, `Ran 132 tests` 이상과 `OK`.

- [ ] **Step 7: Commit**
```bash
git add ci/requirements-py39.in ci/requirements-py39.txt ci/requirements-py3x.in ci/requirements-py3x.txt scripts/dev-setup.sh scripts/dev-check.sh
git commit -m "chore: CI Python 도구 버전 고정과 로컬 준비 스크립트"
```

### Task 2: 이미지 입력 해시와 변경 파일 비밀값 검사 스크립트

**Files:**
- Create: `ci/inputs_hash.py`, `ci/scan_exposure.py`, `tests/test_ci_scripts.py`
- Modify: `pyproject.toml` (`[tool.mypy] files`에 `"ci"` 추가)

**Interfaces:**
- Consumes: `ledger.secrets.find_secrets(text: str) -> list[str]` (패턴 이름 목록, `plugins/forge-ledger/ledger/secrets.py:33`)
- Produces:
  - `ci/inputs_hash.py`: `image_input_paths(root: Path) -> list[Path]`(`ci/Dockerfile` + `ci/image/` 아래 모든 파일, 상대 POSIX 경로 순 정렬), `inputs_digest(root: Path) -> str`(sha256 hex 64자, 파일마다 상대경로 바이트 + `\0` + 내용 + `\0`을 순서대로 넣음), `main(argv: Optional[Sequence[str]] = None) -> int`. CLI: 인자 없음 → 해시 출력, 0. `--check FILE` → 같으면 0, 다르거나 파일 없으면 stderr에 `검사 이미지 재빌드 필요: bash scripts/ci-image-build.sh`, 1. `--root DIR`(기본: 이 파일의 부모의 부모).
  - `ci/scan_exposure.py`: 변경 파일 비밀값 검사. 2026-11-26에 이 이름의 저장소 전체 노출 검사로 대체했다. 인터페이스와 시험(`tests/test_scan_exposure.py`)은 `2026-11-26-pmt-migration.md` Task 1을 따른다.

- [ ] **Step 1: 실패하는 시험 작성** — `tests/test_ci_scripts.py`는 `ROOT/ci`를 `sys.path`에 넣고 `import inputs_hash`. 임시 폴더(`tempfile.TemporaryDirectory`)로 가짜 루트를 만든다.

```python
class TestInputsHash(unittest.TestCase):
    def test_digest_is_hex64(self): ...                     # re.fullmatch(r"[0-9a-f]{64}", d)
    def test_digest_ignores_creation_order(self): ...       # 같은 파일을 다른 순서로 만든 두 루트 → 같은 값
    def test_digest_changes_on_content_change(self): ...    # ci/image/TAG "1" → "2" → 값 다름
    def test_digest_changes_on_rename(self): ...            # ci/image/a → ci/image/b(내용 같음) → 값 다름
    def test_check_match_returns_0(self): ...               # 기록 파일 = 현재 해시 → main(["--root", r, "--check", f]) == 0
    def test_check_mismatch_returns_1_with_message(self): ...  # == 1, stderr에 "검사 이미지 재빌드 필요"
    def test_check_missing_record_returns_1(self): ...      # 없는 기록 파일 → 1
```

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_ci_scripts -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'inputs_hash'`)

- [ ] **Step 3: `ci/inputs_hash.py`, `ci/scan_exposure.py` 구현** (위 Interfaces 그대로). `pyproject.toml`의 mypy `files`에 `"ci"`를 추가한다.
- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_ci_scripts -v && bash scripts/dev-check.sh`
Expected: 새 시험 14개 PASS, dev-check 종료 코드 0(mypy 대상 파일 수가 늘어남).

- [ ] **Step 5: Commit**
```bash
git add ci/inputs_hash.py ci/scan_exposure.py tests/test_ci_scripts.py pyproject.toml
git commit -m "feat: 검사 이미지 입력 해시와 변경 파일 비밀값 검사"
```

### Task 3: 검사 전용 이미지

**Files:**
- Create: `ci/Dockerfile`, `ci/image/TAG`(내용 `1`), `ci/image/semgrep-packs.txt`, `scripts/ci-image-build.sh`

**Interfaces:**
- Consumes: `python3 ci/inputs_hash.py`(Task 2)
- Produces: 로컬 이미지 `forge-ledger-ci:1`. PATH에 `python3.9`, `python3.12`, `uv`, `node`, `git`, `gitleaks`, `trivy`, `shellcheck`, `actionlint`, `semgrep`, `claude`. semgrep 규칙 폴더 `/opt/semgrep-rules`, trivy 캐시 `/opt/trivy-cache`, 기록 파일 `/etc/forge-ci/inputs.sha256`. 빌드 명령 `bash scripts/ci-image-build.sh`(인자 없음).

- [ ] **Step 1: `ci/image/semgrep-packs.txt` 작성** — semgrep 레지스트리 규칙 묶음 이름을 한 줄에 하나씩 적는다. Python, 보안 감사, 셸 범위에서 고르고(설계서 9절 빌더 재량), 빌드 때 모두 내려받혀야 한다.
- [ ] **Step 2: `ci/Dockerfile` 작성** — Global Constraints 표의 버전을 `ARG`로 고정한다.
  - uv는 `COPY --from=ghcr.io/astral-sh/uv:0.13.7`.
  - Python 3.9.25는 `uv python install 3.9.25`로 설치하고 `python3.9`로 연결한다.
  - Node, gitleaks, trivy, shellcheck, actionlint는 공식 릴리스 파일을 받는다. 배포처 체크섬 파일로 확인하고, 체크섬 파일이 없는 도구는 sha256을 `ARG`로 고정해 `sha256sum -c`로 확인한다.
  - semgrep은 `/opt/semgrep` venv(python3.12)에 설치하고, 규칙 묶음마다 `https://semgrep.dev/c/<pack>`을 `/opt/semgrep-rules/`에 받는다.
  - 빈 폴더에 `trivy fs --scanners misconfig,secret --cache-dir /opt/trivy-cache`를 한 번 실행해 점검 규칙을 받아 둔다.
  - 마지막에 `ARG INPUTS_SHA256`이 비어 있으면 빌드를 실패시키고, 값을 `/etc/forge-ci/inputs.sha256`에 쓴다.
- [ ] **Step 3: `scripts/ci-image-build.sh` 작성** — `set -euo pipefail`. 저장소 루트로 이동한 뒤 `tag=$(cat ci/image/TAG)`, `digest=$(python3 ci/inputs_hash.py)`를 구하고 `docker build -f ci/Dockerfile --build-arg INPUTS_SHA256="$digest" -t "forge-ledger-ci:$tag" ci/image`를 실행한다.
- [ ] **Step 4: 빌드와 버전 확인**

Run:
```bash
bash scripts/ci-image-build.sh
docker run --rm forge-ledger-ci:1 sh -c 'python3.9 --version; python3.12 --version; uv --version; node --version; gitleaks version; trivy --version | head -1; shellcheck --version | sed -n 2p; actionlint -version | head -1; semgrep --version; claude --version'
```
Expected: 표의 버전과 모두 일치(`Python 3.9.25`, `v26.13.1`, `8.30.1`, `0.76.2`, `0.11.0`, `1.7.12`, `1.186.0`, `2.1.331`).

- [ ] **Step 5: 해시 기록과 오프라인 실행 확인**

Run:
```bash
test "$(docker run --rm forge-ledger-ci:1 cat /etc/forge-ci/inputs.sha256)" = "$(python3 ci/inputs_hash.py)" && echo HASH_OK
docker run --rm --network none forge-ledger-ci:1 sh -c 'trivy fs --scanners misconfig,secret --skip-check-update --skip-db-update --cache-dir /opt/trivy-cache /etc/forge-ci && semgrep scan --config /opt/semgrep-rules --metrics=off --disable-version-check /etc/forge-ci && echo OFFLINE_OK'
docker run --rm -v "$PWD":/src:ro -w /src forge-ledger-ci:1 shellcheck scripts/ci-image-build.sh scripts/dev-setup.sh scripts/dev-check.sh
```
Expected: `HASH_OK`, `OFFLINE_OK`, shellcheck 출력 없음(종료 코드 0). report에 이미지 크기(`docker image ls forge-ledger-ci:1`)를 적는다.

- [ ] **Step 6: Commit**
```bash
git add ci/Dockerfile ci/image/TAG ci/image/semgrep-packs.txt scripts/ci-image-build.sh
git commit -m "feat: 검사 전용 이미지와 빌드 스크립트"
```

### Task 4: 검사 실행 스크립트와 기존 코드 점검 결과 정리

**Files:**
- Create: `ci/run-checks.sh`, `scripts/ci-local.sh`, `.gitleaks.toml`, 필요하면 `.gitleaksignore`, `.semgrepignore`, `.trivyignore`
- Modify: `pyproject.toml` (`[tool.coverage.run]`, `[tool.coverage.report] fail_under`, `[tool.bandit]`), 점검에서 실제 문제로 판정된 코드

**Interfaces:**
- Consumes: Task 1 고정 파일, Task 2 스크립트, Task 3 이미지
- Produces: `ci/run-checks.sh <lint|test|secrets|sast|deps|all>`.
  - 환경 변수: `BASE_SHA`, `HEAD_SHA`(없으면 `git merge-base origin/main HEAD`와 `HEAD`), `UV_CACHE_DIR`(선택), `CI_VENV_DIR`(기본 `/tmp/forge-ci-venv`).
  - 작업 하나는 첫 실패에서 0이 아닌 값으로 끝난다. `all`은 다섯 작업을 모두 돌린 뒤 작업별 `통과`/`실패`를 출력하고, 하나라도 실패하면 1을 돌려준다.
  - `scripts/ci-local.sh [작업]`(기본 `all`): 커밋된 `HEAD`를 임시 폴더에 `git clone --no-local`하고, `docker run --rm -v <clone>:/src -w /src forge-ledger-ci:$(cat ci/image/TAG) ci/run-checks.sh <작업>`을 실행한 뒤 임시 폴더를 지운다. 커밋하지 않은 변경은 검사하지 않는다.

- [ ] **Step 1: `ci/run-checks.sh` 작성** — `set -euo pipefail`. venv 준비 함수: `uv venv $CI_VENV_DIR/py39 --python 3.9` + `uv pip install --require-hashes -r ci/requirements-py39.txt`, py3x(3.12)도 같은 방식. 작업별 명령은 아래 순서 그대로다.

| 작업 | 명령 |
|---|---|
| `lint` | `python3.9 ci/inputs_hash.py --check /etc/forge-ci/inputs.sha256`, `ruff check .`, `ruff format --check .`, `mypy`, `git ls-files -z '*.sh' \| xargs -0 shellcheck`, `.gitea/workflows/*.yml`이 있으면 `actionlint -config-file ci/actionlint.yaml <파일들>` |
| `test` | `coverage run -m unittest discover -s tests -t .`, `coverage report`, `claude plugin validate .` |
| `secrets` | `gitleaks git --config .gitleaks.toml --redact --no-banner --exit-code 1 .`, `python3.9 ci/scan_exposure.py --range "$BASE..$HEAD"` |
| `sast` | `semgrep scan --config /opt/semgrep-rules --error --metrics=off --disable-version-check .`, `bandit -c pyproject.toml -r plugins ci scripts` |
| `deps` | 고정 파일마다 `pip-audit --disable-pip --no-deps --require-hashes -r <파일>`, `trivy fs --scanners misconfig,secret --exit-code 1 --skip-check-update --skip-db-update --cache-dir /opt/trivy-cache --skip-dirs tests .` |

- [ ] **Step 2: `scripts/ci-local.sh`, `.gitleaks.toml` 작성** — `.gitleaks.toml`은 기본 규칙을 확장하고, 허용 경로는 `tests/test_secrets.py`, `tests/fixtures/`, `tests/test_ci_scripts.py`만 둔다.
- [ ] **Step 3: 첫 실행으로 발견 목록 확보**

Run: `bash scripts/ci-local.sh all`
Expected: 작업별 결과 출력. 실패한 도구의 발견을 report에 표로 적는다(도구, 규칙, 파일:줄, 판정).

- [ ] **Step 4: 발견 처리**
  - 실제 문제는 코드를 고친다. 동작이 바뀌면 시험을 추가한다.
  - 오탐은 줄 단위 예외(`# nosec <ID>`, `# nosemgrep: <rule-id>`)나 경로 예외로 처리하고, 같은 줄에 사유를 적는다. 경로 예외는 `tests/` 고정 데이터에만 쓴다.
  - gitleaks 과거 커밋 발견은 이력을 다시 쓰지 않는다. `.gitleaksignore`에 지문과 사유를 적는다. 실제 비밀값으로 보이면 멈추고 controller에게 보고한다(`BLOCKED`).
  - Dockerfile 점검 발견(예: root 사용자)도 같은 규칙으로 처리한다. 작업 컨테이너는 checkout 쓰기 권한 때문에 root로 돈다는 사유를 쓸 수 있다.
  - `coverage report`의 전체 비율을 소수점 아래 버림해 `fail_under`에 넣는다. `[tool.coverage.run]`의 `source`는 `["plugins/forge-ledger", "ci"]`, `branch = true`.
- [ ] **Step 5: 전체 통과 확인**

Run: `bash scripts/ci-local.sh all && bash scripts/dev-check.sh`
Expected: 다섯 작업 모두 `통과`, dev-check 종료 코드 0.

- [ ] **Step 6: 실패해야 하는 경우 확인 (커밋하지 않는 임시 복제본에서만)**

Run (각 줄은 임시 `git clone --no-local` 폴더에서 변경을 커밋한 뒤 `docker run ... ci/run-checks.sh <작업>`):
```text
docs/probe.md에 "plane_api_" + "a"*32 값을 쓴 커밋        → secrets 0이 아닌 종료, 출력에 토큰 값 없음
plugins/forge-ledger/ledger/spec.py에 사용하지 않는 import 추가  → lint 0이 아닌 종료
ci/image/TAG를 2로 바꾼 커밋                               → lint 0이 아닌 종료, "검사 이미지 재빌드 필요"
docker run --network none ... ci/run-checks.sh test        → 0이 아닌 종료(venv 설치 실패, 통과로 끝나지 않음)
```
Expected: 네 경우 모두 0이 아닌 종료 코드. 결과를 report에 적는다. 저장소 작업 트리에는 아무것도 남지 않는다(`git status --short` 비어 있음).

- [ ] **Step 7: Commit** (실제 문제 수정은 수정마다 별도 `fix:` 커밋)
```bash
git add ci/run-checks.sh scripts/ci-local.sh .gitleaks.toml pyproject.toml
git add .gitleaksignore .semgrepignore .trivyignore 2>/dev/null || true
git commit -m "feat: 검사 실행 스크립트와 도구 설정"
```

### Task 5: PR 검사 워크플로

**Files:**
- Create: `.gitea/workflows/pr-check.yml`, `ci/actionlint.yaml`, `tests/test_ci_workflow.py`

**Interfaces:**
- Consumes: `ci/run-checks.sh <작업>`(Task 4), `ci/image/TAG`(Task 3)
- Produces: 상태 이름(예상) `pr-check / <작업> (pull_request)` 5개. Task 7이 실제 값을 확인한다.

- [ ] **Step 1: 실패하는 시험 작성** — `tests/test_ci_workflow.py`, 표준 라이브러리 정규식으로 워크플로 텍스트를 검사한다.

```python
class TestWorkflow(unittest.TestCase):
    def test_name_and_trigger(self): ...          # "name: pr-check", pull_request, types [opened, synchronize, reopened], branches [main]
    def test_permissions_read_only(self): ...     # 최상위 "contents: read", 파일 어디에도 "write" 없음
    def test_five_jobs(self): ...                 # jobs 아래 ID 집합 == {"lint","test","secrets","sast","deps"}
    def test_container_tag_matches_tag_file(self): ...  # 모든 container 이미지 == "forge-ledger-ci:" + TAG 파일 내용
    def test_runs_on_label(self): ...             # "runs-on: forge-ci" 5번
    def test_actions_pinned_by_sha(self): ...     # uses 값 집합 == {"actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
                                                  #                  "actions/cache@caa296126883cff596d87d8935842f9db880ef25"}
    def test_each_job_runs_its_check(self): ...   # 작업 X 본문에 "ci/run-checks.sh X"
    def test_no_secret_literals(self): ...        # find_secrets(워크플로 전체) == []
```

- [ ] **Step 2: 실패 확인**

Run: `python3 -m unittest tests.test_ci_workflow -v`
Expected: FAIL(파일 없음)

- [ ] **Step 3: 워크플로와 `ci/actionlint.yaml` 작성**
  - 모든 작업: checkout(`fetch-depth: 0`) → cache(`path: /root/.cache/uv`, `key: uv-${{ hashFiles('ci/requirements-py39.txt', 'ci/requirements-py3x.txt') }}`) → `UV_CACHE_DIR=/root/.cache/uv`로 `ci/run-checks.sh <작업>`.
  - `secrets` 작업에는 `BASE_SHA: ${{ github.event.pull_request.base.sha }}`, `HEAD_SHA: ${{ github.event.pull_request.head.sha }}`를 준다.
  - `ci/actionlint.yaml`에 `self-hosted-runner: labels: [forge-ci]`를 둔다.
- [ ] **Step 4: 통과 확인**

Run: `python3 -m unittest tests.test_ci_workflow -v && bash scripts/dev-check.sh && git add -A .gitea ci/actionlint.yaml tests/test_ci_workflow.py && git commit -m "feat: PR 검사 워크플로" && bash scripts/ci-local.sh all`
Expected: 시험 8개 PASS, dev-check 0, ci-local 다섯 작업 `통과`(actionlint가 이제 워크플로를 검사함).

### Task 6: 러너 실행 파일과 운영 문서

**Files:**
- Create: `ci/runner/compose.yml`, `ci/runner/config.yaml`, `ci/runner/README.md`, `tests/test_ci_runner.py`

**Interfaces:**
- Consumes: `ci/image/TAG`
- Produces: `docker compose -f ci/runner/compose.yml up -d`.
  - 실행할 때만 환경 변수 `GITEA_INSTANCE_URL`, `GITEA_RUNNER_REGISTRATION_TOKEN`, `FORGE_RUNNER_DATA`(호스트 데이터 경로, 저장소 밖)를 받는다.
  - 서비스 이름은 `runner`, 컨테이너 이름은 `forge-ledger-runner`, 네트워크는 `forge-ci-net`.

- [ ] **Step 1: 실패하는 시험 작성**

```python
class TestRunnerFiles(unittest.TestCase):
    def test_config_isolation_values(self): ...   # config.yaml에 'docker_host: "-"', "privileged: false", "capacity: 1",
                                                  # "force_pull: false", "network: forge-ci-net", '--memory=3g --cpus=4'
    def test_compose_runner_image_pinned(self): ...  # "image: gitea/runner:6.0.1", "restart: unless-stopped"
    def test_label_tag_matches_tag_file(self): ...   # "forge-ci:docker://forge-ledger-ci:" + TAG
    def test_secrets_only_from_env(self): ...     # GITEA_INSTANCE_URL·GITEA_RUNNER_REGISTRATION_TOKEN 값은 "${...}" 형태만
    def test_no_secret_literals(self): ...        # find_secrets(compose·config·README 각각) == []
```

- [ ] **Step 2: 실패 확인** — Run: `python3 -m unittest tests.test_ci_runner -v` → FAIL(파일 없음)
- [ ] **Step 3: 파일 작성**
  - `config.yaml`의 키 이름은 `gitea/runner` v6.0.1 `internal/pkg/config/config.example.yaml`과 대조한다. 대조한 줄 번호는 report에 적는다.
  - `runner.file: /data/.runner`, `cache.enabled: true`, `cache.dir: /data/cache`로 둔다.
  - compose는 `/var/run/docker.sock`(러너 전용), `./config.yaml:/config.yaml:ro`, `${FORGE_RUNNER_DATA:?FORGE_RUNNER_DATA 필요}:/data`를 마운트한다. `CONFIG_FILE=/config.yaml`, `GITEA_RUNNER_NAME=forge-ledger-ci`, `GITEA_RUNNER_LABELS`도 넣는다. 네트워크 `forge-ci-net`은 `name: forge-ci-net`으로 만든다.
  - README에 적을 것(주소와 토큰은 변수 이름으로만):
    - 등록 절차(메인 세션 전용, 등록 토큰은 등록 직후 버림)
    - 상태 확인과 재시작(`docker compose ... ps`, `logs --tail 50`, `restart`)
    - 이미지 재빌드 절차
    - 외부 네트워크 장애 때 검사가 실패로 끝난다는 점
    - 비상 우회(보호 규칙 일시 해제와 기록)
- [ ] **Step 4: 통과 확인**

Run:
```bash
python3 -m unittest tests.test_ci_runner -v
FORGE_RUNNER_DATA=/tmp/forge-runner-probe GITEA_INSTANCE_URL=http://example.invalid docker compose -f ci/runner/compose.yml config -q && echo COMPOSE_OK
bash scripts/dev-check.sh
```
Expected: 시험 5개 PASS, `COMPOSE_OK`, dev-check 0.

- [ ] **Step 5: Commit 후 전체 검사**
```bash
git add ci/runner tests/test_ci_runner.py && git commit -m "feat: 저장소 전용 러너 실행 파일과 운영 문서"
bash scripts/ci-local.sh all
```
Expected: 다섯 작업 `통과`(trivy가 compose·Dockerfile 설정을 점검함).

### Task 7: 적용과 확인 (controller 전용, 위임하지 않음)

토큰과 Gitea 주소를 쓰므로 메인 세션이 직접 한다. 출력에서 토큰과 주소를 가린다.

- [ ] **Step 1:** 저장소 Actions 켜기: `PATCH /api/v1/repos/{owner}/forge-ledger` `{"has_actions": true}` → 다시 읽어 `has_actions: true` 확인.
- [ ] **Step 2:** 등록 토큰 발급(`POST .../actions/runners/registration-token`)을 셸 변수에만 담아 `docker compose -f ci/runner/compose.yml up -d`를 실행한다. 토큰 변수는 바로 지운다. `GET .../actions/runners`에서 `forge-ledger-ci`가 online인지 확인한다.
- [ ] **Step 3:** `ci-pr-check`를 push하고 PR #2(`ci-pr-check` → `main`)를 만든다.
- [ ] **Step 4:** 다섯 작업이 끝날 때까지 기다린다. `GET .../commits/{head_sha}/statuses`로 상태 이름 5개와 결과를 읽는다. 모두 `success`여야 한다. 실패하면 로그를 읽고 Task 4 규칙대로 고친다.
- [ ] **Step 5:** 시험 PR: `ci-probe-fail` 브랜치(`ci-pr-check`에서 시작)에 `plugins/forge-ledger/ledger/spec.py` 사용하지 않는 import 한 줄 커밋 → push → PR → `pr-check / lint`가 `failure`인지 확인한다.
- [ ] **Step 6:** Step 4에서 읽은 실제 이름 5개로 `main` 보호 규칙을 만든다(Global Constraints 값). 다시 읽어 값이 같은지 확인한다.
- [ ] **Step 7:** 관리자 토큰으로 시험 PR 병합 API를 호출한다 → 거부(2xx가 아님)되는지 확인한다. 그다음 시험 PR을 닫는다(`state: closed`).
- [ ] **Step 8:** 브랜치를 만들지 않고 빈 커밋 객체만 만든다: `sha=$(git commit-tree 'main^{tree}' -p main -m "chore: 보호 규칙 확인용 빈 커밋")`. `git push origin "$sha:refs/heads/main"`이 거부되는지 확인한다. 로컬 브랜치는 바뀌지 않는다.
- [ ] **Step 9:** `docker restart forge-ledger-runner` 뒤 60초 안에 러너가 다시 online인지 확인한다.
- [ ] **Step 10:** PR #2의 다섯 상태가 `success`이고 병합 가능인지 확인한 뒤, 병합 여부는 사용자에게 묻는다. PMT `verify`와 `note`를 남긴다.
