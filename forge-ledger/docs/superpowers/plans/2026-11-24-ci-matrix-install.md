# PR 검사 파이프라인 확대: Python 버전 매트릭스와 설치 시험 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** PR 검사에 두 작업을 더한다. `matrix`는 단위 시험을 Python 3.10·3.11·3.12·3.13 각각의 새 가상환경에서 돌린다. `install`은 깨끗한 격리 환경에 플러그인을 실제로 설치하고 CLI와 훅을 실행해 본다. 둘 다 `main` 병합 필수 검사로 만든다.

**Architecture:** 검사 이미지에 Python 3.10·3.11·3.13을 넣어 TAG 3으로 다시 빌드한다(3.9·3.12는 이미 있음). `ci/run-checks.sh`에 `matrix`, `install` 작업을 더하고, 설치 뒤 확인은 시험 가능한 `ci/install_smoke.py`로 둔다. 워크플로에 두 작업을 더하고 보호 규칙의 필수 상태를 7개로 늘린다.

**Tech Stack:** Gitea Actions, Docker, uv, Python 3.9~3.13, Claude Code CLI 2.1.331(`claude plugin marketplace add`·`install`), bash, unittest

**Spec:** 없음. 사용자 요청 "파이프라인을 더 크게(가상환경 시험 확대)"를 controller가 해석한 범위다. 확인한 사실(2026-11-24):
- 네트워크를 끊고 로그인하지 않은 깨끗한 HOME에서 `claude plugin marketplace add <저장소>`와 `claude plugin install forge-ledger@forge`가 종료 0으로 성공했다. `claude plugin list`는 `Read from: <저장소>/plugins/forge-ledger`를 보였다.
- uv 내려받기 목록에 `cpython-3.10.22`, `cpython-3.11.18`, `cpython-3.13.18`(linux-x86_64-gnu)이 있다.
- 서버 가용 메모리 약 6GB, 러너 동시 작업 1개라 작업이 늘면 파이프라인 시간이 길어진다.

## Global Constraints

- 저장소 코드: 표준 라이브러리만, `from __future__ import annotations`, Python 3.9 호환, ruff line-length 120, mypy strict(`ci/`도 대상), 시험은 `unittest`.
- 민감값(IP, 접속 주소, 토큰, 계정)을 파일·커밋·보고서에 넣지 않는다. 시험·확인용 가짜 비밀값은 문자열 이어 붙이기로 만든다(예: `"10." + "1.2.3"`, `"plane_api_" + "a" * 32`). 저장소의 자체 비밀값 검사가 `tests/` 밖 변경 파일도 보기 때문이다.
- `main`은 보호돼 있다. 브랜치 `ci-matrix-install`에서 하고 PR로 병합한다. push·Gitea 설정·러너 재생성·보호 규칙 변경은 controller만 한다. 이력 재작성·force push 금지.
- 이미지 태그 단일 원천 `ci/image/TAG`를 `3`으로 올린다. 워크플로 `container:`와 러너 라벨은 이 값과 같아야 한다. `forge-ledger-ci:1`, `:2`는 지우지 않는다. `docker system prune`·`docker image prune` 금지.
- 이미지에 넣을 Python: `3.10.22`, `3.11.18`, `3.13.18`(uv 관리 배포본, `python3.10`·`python3.11`·`python3.13`으로 연결). 기존 `python3.9`(3.9.25), `python3.12`(기반 이미지) 유지.
- 새 작업 ID와 상태 이름: `matrix` → `pr-check / matrix (pull_request)`, `install` → `pr-check / install (pull_request)`. 기존 5개는 그대로.
- `matrix`는 버전마다 `uv venv --clear <CI_VENV_DIR>/m<버전> --python <버전>`으로 새 가상환경을 만들고(실행 중 내려받기 금지: `UV_PYTHON_DOWNLOADS=never`), 그 가상환경의 python으로 `-m unittest discover -s tests -t .`를 돌린다. 한 버전이라도 실패하면 작업 실패. 3.9는 기존 `test` 작업이 맡는다.
- `install`은 임시 HOME에서 `claude plugin marketplace add "$PWD"` → `claude plugin install forge-ledger@forge` → `ci/install_smoke.py`를 `python3.9`와 `python3.13` 두 해석기로 돌린다.
- 확인 문구(그대로): 훅 차단 메시지는 `forge-ledger: 비밀값(ipv4)이 있어 쓰기를 막았습니다`를 포함한다.
- 커밋 메시지는 한국어, 접두어 `feat:`/`fix:`/`test:`/`chore:`/`docs:`.

## Review Focus

1. Python 버전마다 다른 문법·표준 라이브러리 차이(예: 3.13에서 없어진 모듈, 3.9에 없는 문법)가 있으면 `matrix`가 실패해야 한다 → Task 2 Step 5 실패 시험.
2. 설치는 성공했는데 훅이 실제로 막지 못하면 `install`이 실패해야 한다 → Task 2 `install_smoke` 시험.
3. 이미지에 새 Python을 넣고 TAG를 올리지 않으면 `lint`의 해시 비교가 잡아야 한다 → Task 1 Step 4.
4. `install` 작업이 사용자 홈이나 컨테이너 밖에 흔적을 남기지 않아야 한다 → Task 2 Step 5.
5. 필수 상태가 7개로 늘어난 뒤에도 이름이 실제와 같아야 병합이 영원히 막히지 않는다 → Task 4 Step 4.

---

### Task 1: 검사 이미지에 Python 3.10·3.11·3.13 추가, TAG 3

**Files:**
- Modify: `ci/Dockerfile`(Python 3.9 설치 부분 34–37줄 근처), `ci/image/TAG`, `.gitea/workflows/pr-check.yml`(`container:` 5곳), `ci/runner/compose.yml`(`GITEA_RUNNER_LABELS`)

**Interfaces:**
- Produces: 로컬 이미지 `forge-ledger-ci:3`. PATH에 `python3.9`, `python3.10`, `python3.11`, `python3.12`, `python3.13`.

- [ ] **Step 1:** `ci/Dockerfile`에 `ARG PYTHON310_VERSION=3.10.22`, `ARG PYTHON311_VERSION=3.11.18`, `ARG PYTHON313_VERSION=3.13.18`을 두고, 기존 3.9와 같은 방식(`uv python install --no-bin` + `ln -sf "$(uv python find --no-project <버전>)" /usr/local/bin/python3.X`)으로 설치한다. 각 설치 뒤 `--version`을 출력한다.
- [ ] **Step 2:** `ci/image/TAG`를 `3`으로, 워크플로 `container:` 5곳과 compose 라벨을 `forge-ledger-ci:3`으로 바꾼다.
- [ ] **Step 3: 빌드와 확인**

Run:
```bash
bash scripts/ci-image-build.sh
docker run --rm forge-ledger-ci:3 sh -c 'for v in 3.9 3.10 3.11 3.12 3.13; do python$v --version; done'
test "$(docker run --rm forge-ledger-ci:3 cat /etc/forge-ci/inputs.sha256)" = "$(python3 ci/inputs_hash.py)" && echo HASH_OK
python3 -m unittest tests.test_ci_workflow tests.test_ci_runner -v
```
Expected: `Python 3.9.25`, `3.10.22`, `3.11.18`, `3.12.x`, `3.13.18`, `HASH_OK`, 시험 PASS.
- [ ] **Step 4:** 커밋 뒤 `bash scripts/ci-local.sh all` → 기존 다섯 작업 통과. `docker image ls forge-ledger-ci`에 `1`, `2`, `3`이 모두 있다.
- [ ] **Step 5: Commit** — `git commit -m "feat: 검사 이미지에 Python 3.10·3.11·3.13을 넣고 TAG를 3으로 올림"`

### Task 2: `matrix`·`install` 검사와 설치 확인 스크립트

**Files:**
- Create: `ci/install_smoke.py`, `tests/test_install_smoke.py`
- Modify: `ci/run-checks.sh`(`job_matrix`, `job_install`, `job_all`의 작업 목록, 인자 분기)

**Interfaces:**
- Produces (`ci/install_smoke.py`):
  - `class SmokeResult(NamedTuple): name: str; ok: bool; detail: str`
  - `run_smoke(plugin_root: Path, python: str) -> list[SmokeResult]` — 아래 5개 확인을 순서대로 돌려 결과를 모두 돌려준다(첫 실패에서 멈추지 않음).
    1. `cli_help`: `<python> <root>/scripts/ledger.py --help` 종료 0, 표준 출력에 `init`과 `plan`
    2. `hooks_json`: `<root>/hooks/hooks.json`의 각 명령에서 `${CLAUDE_PLUGIN_ROOT}`를 root로 바꾼 경로의 파일이 모두 있음
    3. `session_start`: 표준 입력 `{}`, 환경 `CLAUDE_PROJECT_DIR=/nonexistent`로 `hooks/session_start.py` 종료 0
    4. `pre_tool_use_block`: `{"tool_name": "Write", "tool_input": {"file_path": "/r/.ledger/design/data.md", "content": <"10." + "1.2.3">}}` → 종료 2, 표준 오류에 Global Constraints의 차단 문구, 표준 오류에 IP 문자열 없음
    5. `pre_tool_use_allow`: `{"tool_name": "Write", "tool_input": {"file_path": "/r/src/a.py", "content": "깨끗"}}` → 종료 0
  - `main(argv: Optional[Sequence[str]] = None) -> int` — `--root DIR --python EXE`. 결과를 한 줄씩 `<name>: 통과|실패 <detail>`로 출력하고, 하나라도 실패하면 1, 모두 통과면 0. 하위 프로세스는 시간 제한(30초)을 둔다.
- `job_matrix`: Global Constraints대로 3.10·3.11·3.12·3.13. 버전별 결과를 출력하고 하나라도 실패하면 0이 아닌 값.
- `job_install`: `HOME`을 `mktemp -d`로 바꾼 하위 셸에서 설치하고, `claude plugin list` 출력의 `Read from:` 경로가 `$PWD/plugins/forge-ledger`인지 확인한 뒤, `python3.9`와 `python3.13`으로 `ci/install_smoke.py --root "$PWD/plugins/forge-ledger" --python <해석기>`를 돌린다. 끝나면 임시 HOME을 지운다(trap).
- `job_all` 작업 목록: `lint test secrets sast deps matrix install`.

- [ ] **Step 1: 실패하는 시험 작성** — `tests/test_install_smoke.py`
```python
class TestInstallSmoke(unittest.TestCase):
    def test_repo_plugin_passes_all_checks(self): ...
        # run_smoke(ROOT / "plugins/forge-ledger", sys.executable) → 5개 모두 ok, 이름 순서 그대로
    def test_detects_hook_that_does_not_block(self): ...
        # 플러그인 폴더를 임시 폴더에 복사하고 hooks/pre_tool_use.py를 "import sys; sys.exit(0)"로 바꿈
        # → pre_tool_use_block만 ok=False, 나머지 ok
    def test_detects_missing_hook_file(self): ...
        # 임시 사본에서 hooks/session_start.py 삭제 → hooks_json과 session_start가 ok=False
    def test_main_exit_codes_and_output(self): ...
        # 정상 root → 0, 출력에 "pre_tool_use_block: 통과"; 망가진 root → 1, 출력에 "실패"
    def test_output_has_no_fake_secret(self): ...
        # main 출력과 detail 어디에도 검사용 가짜 IP 문자열("10." + "1.2.3")이 없음(값은 이어 붙여 만듦)
```
- [ ] **Step 2: 실패 확인** — Run: `python3 -m unittest tests.test_install_smoke -v` → FAIL(모듈 없음)
- [ ] **Step 3: 구현** — `ci/install_smoke.py`(위 Interfaces), `ci/run-checks.sh`의 `job_matrix`·`job_install`·`job_all`·인자 분기.
- [ ] **Step 4: 통과 확인** — Run: `python3 -m unittest tests.test_install_smoke -v && bash scripts/dev-check.sh` → PASS, 종료 0. 커밋 뒤 `bash scripts/ci-local.sh matrix`와 `bash scripts/ci-local.sh install` → 각각 통과, `bash scripts/ci-local.sh all` → 일곱 작업 통과.
- [ ] **Step 5: 실패해야 하는 경우(커밋하지 않는 임시 복제본에서)**
```text
tests/에 Python 3.10 이상에서만 깨지는 시험을 하나 넣은 커밋(예: 3.10+에서 제거된 동작에 기대는 단언)
  → matrix 0이 아닌 종료, 출력에 실패한 버전 표시
plugins/forge-ledger/hooks/pre_tool_use.py를 항상 0으로 끝나게 바꾼 커밋 → install 0이 아닌 종료
```
그리고 `install` 실행 전후 호스트 `~/.claude`와 컨테이너 밖 파일이 바뀌지 않음을 확인한다(ci-local은 컨테이너 안에서만 HOME을 바꾼다). 결과를 report에 적는다.
- [ ] **Step 6: Commit** — `git commit -m "feat: Python 버전 매트릭스와 플러그인 설치 확인 검사 추가"`

### Task 3: 워크플로에 `matrix`·`install` 작업 추가

**Files:**
- Modify: `.gitea/workflows/pr-check.yml`, `tests/test_ci_workflow.py`

**Interfaces:**
- Consumes: `ci/run-checks.sh matrix|install`(Task 2), TAG 3(Task 1)
- Produces: 상태 이름 7개(Global Constraints).

- [ ] **Step 1: 시험 갱신(먼저)** — `tests/test_ci_workflow.py`: 작업 ID 집합이 `{"lint","test","secrets","sast","deps","matrix","install"}`, `runs-on: forge-ci` 7번, 각 작업 본문에 `ci/run-checks.sh <ID>`, 모든 `container:`가 TAG 값, `uses:`는 기존 두 해시만, `install`·`matrix`에는 cache 단계가 없음.
- [ ] **Step 2: 실패 확인** — Run: `python3 -m unittest tests.test_ci_workflow -v` → FAIL
- [ ] **Step 3: 구현** — 두 작업 추가(checkout `fetch-depth: 0`, `container: forge-ledger-ci:3`, `runs-on: forge-ci`, 실행 `bash ci/run-checks.sh <ID>`).
- [ ] **Step 4: 통과 확인** — Run: `python3 -m unittest tests.test_ci_workflow -v && bash scripts/dev-check.sh`, 커밋 뒤 `bash scripts/ci-local.sh lint`(actionlint 통과)
- [ ] **Step 5: Commit** — `git commit -m "feat: PR 검사 워크플로에 matrix·install 작업 추가"`

### Task 4: 적용과 확인 (controller 전용)

- [ ] **Step 1:** 서버에 `forge-ledger-ci:3`이 있는지 확인한다.
- [ ] **Step 2:** `GITEA_INSTANCE_URL`·`FORGE_RUNNER_DATA`를 넣고 `docker compose -f ci/runner/compose.yml up -d`로 러너를 다시 만든다. 라벨 `forge-ci:docker://forge-ledger-ci:3`, online 확인.
- [ ] **Step 3:** 브랜치 push, PR 생성, 상태 7개 `success` 확인. 실제 상태 이름 7개를 API로 읽는다.
- [ ] **Step 4:** `main` 보호 규칙의 `status_check_contexts`를 실제 이름 7개로 바꾸고 다시 읽어 확인한다(다른 값은 유지).
- [ ] **Step 5:** 병합 여부를 사용자에게 묻는다.
