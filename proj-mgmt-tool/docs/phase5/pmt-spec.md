# pmt 기능 명세 (C-01 ~ C-12)

[개요](../05-plugin-split.md) · [통신·인계 기준](communication.md) · [pmt-server 명세](pmt-server-spec.md)

공통 표기: **현재**는 2026-10-08 코드 기준 사실, **변경**은 이번 단계의 구현 대상이다. 오류는 기존 envelope(`{"ok":false,"error":{"code","message","retryable"}}`)·exit code 의미를 따른다. 짧은 명령의 사람용 출력은 표, `--json`이면 원 응답이다.

---

## C-01 저장 모드 결정·자동 준비

**목적**: 설치 직후 local로 바로 쓰고, Host 설정이 들어오면 hosted로 연결한다. 잘못된 상태에서 조용히 다른 저장소로 넘어가지 않는다.

**현재**: `easy_setup.prepare`는 Host 필수값 5개가 없으면 `unconfigured`를 돌려주고 끝난다. local 프로필·DB를 만드는 경로가 없다. `PMT_CONFIG_ROOT`가 이미 설정된 경우에만 기존 hooks 경로(legacy)로 동작한다.

**입력**: 플러그인 설정(C-02), ConfigRoot의 storage 프로필, 클라이언트 credential 저장소(C-04), Hook 이벤트의 `cwd`.

**판정표** (SessionStart마다 평가, 다른 이벤트는 결과 캐시 사용)

| 프로필 | Host 설정(`handoff_file` 또는 `host_url`) | 동작 | 상태 |
|---|---|---|---|
| 없음 | 없음 | `configure_storage(mode=local, expected=null)` → `setup(product=claude|codex)` | `ready/local` |
| 없음 | 있음 | 인계 해석 → credential 확보 → `configure_storage(mode=hosted)`(probe 후 게시) | `ready/hosted` 또는 `error` |
| local | 없음 | 그대로 사용. DB 없으면 `setup` | `ready/local` |
| local | 있음 | 바꾸지 않는다. "`pmt storage switch --to hosted` 필요" 안내 | `ready/local` + 경고 |
| hosted | 없음 | 오류. local DB를 만들지 않는다 | `error: hosted_settings_missing` |
| hosted | 있음, 같음 | 그대로 사용(재게시 없음) | `ready/hosted` |
| hosted | 있음, 다름 | 현재 hash로 CAS 재게시(probe 성공 시에만). mapping 보존 | `ready/hosted` 또는 `error` |

**출력**: `{status, mode, env, link, scope_id, message}`. `env`에는 `PMT_CONFIG_ROOT`, `PMT_DATA_ROOT`, `PMT_PYTHON`, 연결된 경우 `PMT_SCOPE_ID`. **credential은 포함하지 않는다**(C-04).

**동작 규칙**
- Hook은 차단하지 않는다. 설정 오류는 SessionStart에 `systemMessage` 한 줄만 남기고 exit 0.
- local `setup`은 이미 같은 DB가 있으면 아무것도 바꾸지 않는다(기존 setup 멱등성).
- hosted probe 실패 시 프로필을 쓰지 않는다(기존 `configure_storage` 규칙).

**오류 code (신규)**: `hosted_settings_missing`, `local_profile_exists`(기존 `easy_setup_local_profile` 대체), `handoff_invalid`, `credential_unavailable`.

**완료 기준**
- T-C01-1: 빈 ConfigRoot + Host 설정 없음 → 새 세션 SessionStart 후 `pmt mode`가 `local`, `pmt.sqlite3` 존재.
- T-C01-2: hosted 프로필 + Host 설정 제거 → `hosted_settings_missing`, DataRoot에 `pmt.sqlite3`가 생기지 않음.
- T-C01-3: local 프로필 + Host 설정 입력 → 프로필 불변, 경고 표시.
- T-C01-4: hosted 설정값 변경 → CAS 재게시, 기존 mapping 유지. probe 실패면 이전 프로필 유지.

---

## C-02 단일 플러그인 설정 (Claude userConfig)

**목적**: 설정 화면을 하나로 정리하고 local은 Python 경로만으로 쓰게 한다.

**현재**: 루트 `.claude-plugin/plugin.json`은 `python_path`만, `integrations/claude/.claude-plugin/plugin.json`은 Host 값 6개가 필수다. 두 파일이 다르고 빌드 결과도 다르다.

**변경**: 루트 `proj-mgmt-tool/.claude-plugin/plugin.json` 하나만 원본으로 둔다. `integrations/claude/.claude-plugin/`은 삭제하고 빌드는 루트 manifest를 복사한다.

| 키 | type | required | sensitive | 설명 |
|---|---|---|---|---|
| `python_path` | file | 예 | | Python 3.13+ 절대 경로. Hook exec 명령 |
| `handoff_file` | file | | | `pmt-server`가 만든 인계 JSON 절대 경로(X-02). 있으면 아래 Host 개별 값보다 우선 |
| `device_credential` | string | | 예 | Host 기기 credential. hosted일 때 필요 |
| `host_url` | string | | | (개별 입력용) `https://<IP 또는 DNS>:<port>` |
| `host_ca_file` | file | | | (개별 입력용) Host 공개 Root CA PEM |
| `device_id` | string | | | (개별 입력용) |
| `namespace_id` | string | | | (개별 입력용) |
| `actor` | string | | | (개별 입력용) |

**검증**: `handoff_file`이 없고 `host_url`이 있으면 `device_id`, `namespace_id`, `actor`, `device_credential`이 모두 필요하다. 빠지면 `handoff_invalid`와 빠진 키 이름을 표시한다. Claude Code는 `default`를 Hook 명령 치환에 적용하지 않으므로(실측) 선택값의 기본값에 의존하지 않는다.

**완료 기준**
- T-C02-1: `claude plugin validate` 통과.
- T-C02-2: `python_path`만 입력 → local 준비(T-C01-1과 동일 결과).
- T-C02-3: `handoff_file` + `device_credential` → hosted 준비, `pmt check` 통과.
- T-C02-4: 개별 값 입력(기존 방식) → hosted 준비. 기존 0.4.1 실측 설정이 그대로 동작.

---

## C-03 인계 가져오기 `pmt connect`

**목적**: Codex·CLI·다른 Python 진입점도 Claude 설정 화면 없이 Host에 연결한다. Claude에서도 같은 해석 코드를 쓴다.

**명령**
```
pmt connect --handoff <file> [--credential-file <file> | --credential-stdin] [--dry-run]
pmt disconnect            # hosted 프로필 제거 아님. 저장된 credential만 삭제
```

**동작**
1. 인계 JSON을 X-02 스키마로 검증한다. 모르는 필드·버전이면 거부한다.
2. `host.ca_pem`이 있으면 `<ConfigRoot>/host-ca/<namespace_id>.pem`에 저장하고 sha256을 대조한다.
3. credential을 C-04 저장소에 넣는다(값은 stdout에 쓰지 않는다).
4. `configure_storage(mode=hosted, expected=<현재 hash>)`로 probe 후 게시한다. 기존 local 프로필이면 거부하고 C-09를 안내한다.
5. 인계의 `projects[]`를 `<ConfigRoot>/projects.json`(비밀 없음)에 병합 저장한다. `pmt link`가 이름으로 찾는다.
6. 결과 표: endpoint, namespace, actor, scopes, permissions, 호환 버전.

**오류**: `handoff_invalid`, `handoff_version_unsupported`, `handoff_ca_mismatch`, `credential_unavailable`, `local_profile_exists`, 기존 `storage_config_conflict`·`unauthenticated`·`incompatible`.

**완료 기준**
- T-C03-1: 정상 인계 → `pmt storage status`가 hosted, `projects.json`에 project 등록.
- T-C03-2: CA hash 불일치 → 거부, 파일·프로필 불변.
- T-C03-3: 잘못된 credential → probe 실패, 프로필 불변, credential 저장소에 남기지 않음.
- T-C03-4: `--dry-run` → 쓰기 없음, 검증 결과만 출력.

---

## C-04 클라이언트 credential 저장소

**목적**: credential을 셸 설정·세션 환경 파일에 평문으로 남기지 않고, Hook·짧은 명령·서브 프로세스가 같은 방식으로 읽는다.

**현재**: SessionStart가 `PMT_HOST_CREDENTIAL`을 `CLAUDE_ENV_FILE`에 `export`로 쓴다. 이후 Bash 명령은 그 값을 쓴다.

**변경**
- 저장 위치
  - Linux/macOS: `<ConfigRoot>/secrets/host-credential` (디렉터리 0700, 파일 0600, 소유자 확인).
  - Windows: `<ConfigRoot>\secrets\host-credential.dpapi` (DPAPI CurrentUser 보호, `ctypes`로 `CryptProtectData`/`CryptUnprotectData` 호출, 표준 라이브러리만 사용).
- 기록 경로
  - Claude: SessionStart에서 `CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL`이 있으면 저장소 값과 비교해 다를 때만 덮어쓴다.
  - Codex/CLI: `pmt connect`(C-03).
- 읽기: `load_credential()`이 프로세스 내부 `os.environ["PMT_HOST_CREDENTIAL"]`에 넣는다. storage 프로필의 `credential_env` 계약(`PMT_HOST_CREDENTIAL`)은 그대로다.
- `CLAUDE_ENV_FILE`에는 비밀 없는 값만 쓴다.
- 명시적 환경변수 `PMT_HOST_CREDENTIAL`이 이미 있으면 그것을 우선한다(기존 운영 호환).

**오류**: `credential_unavailable`(없음), `credential_store_insecure`(권한이 넓음 — 사용 거부), `credential_store_unreadable`.

**완료 기준**
- T-C04-1: Linux 저장 파일 권한 0600, 0644로 바꾸면 `credential_store_insecure`.
- T-C04-2: Windows DPAPI 파일을 다른 사용자 계정에서 읽으면 실패.
- T-C04-3: SessionStart 후 `CLAUDE_ENV_FILE` 내용에 credential 문자열이 없음.
- T-C04-4: 같은 세션 Bash에서 `pmt check` 통과.

---

## C-05 OS별 기본 경로

**현재**: 모든 OS에서 `${XDG_CONFIG_HOME:-~/.config}/pmt`, `${XDG_DATA_HOME:-~/.local/share}/pmt/data`.

**변경**

| OS | ConfigRoot | DataRoot |
|---|---|---|
| Linux | `${XDG_CONFIG_HOME:-~/.config}/pmt` | `${XDG_DATA_HOME:-~/.local/share}/pmt/data` |
| macOS | 위와 같음 | 위와 같음 |
| Windows | `%APPDATA%\pmt` | `%LOCALAPPDATA%\pmt\data` |

우선순위: `PMT_CONFIG_ROOT`/`PMT_DATA_ROOT` 환경변수 → Windows에서 기존 `~/.config/pmt/storage.json`이 있으면 그 경로(호환) → 표의 기본값. 경로를 바꿀 때 DB를 옮기거나 합치지 않는다(기존 원칙).

**완료 기준**: T-C05-1 OS별 기본값 단위 시험, T-C05-2 Windows 기존 경로 호환 시험.

---

## C-06 프로젝트 연결 `pmt link`

**목적**: 현재 Git checkout·branch를 PMT project에 연결해 SessionStart가 `PMT_SCOPE_ID`를 자동 선택하게 한다.

**명령**
```
pmt link [<project 이름|UUID 접두어>] [--repository <이름|UUID>] [--graph-path docs/pmt-docs/graph.json]
pmt link --new "<project 제목>"     # local 전용
pmt unlink                         # 현재 root+branch mapping 제거
pmt projects                       # 알려진 project 목록
```

**동작**
- 공통: Git root·branch 확인(분리 HEAD·non-Git이면 거부). mapping은 기존 형식(`repository_id, project_id, branch, branch_key_sha256, local_root, relative_graph_path`)으로 CAS 추가한다.
- hosted: project/repository는 `projects.json`(C-03) 또는 UUID 직접 입력에서 찾는다. 같은 root의 다른 branch가 이미 있으면 그 project/repository를 재사용한다(현재 동작 유지). 추정 연결은 하지 않는다.
- local:
  - `--new`: `create_scope(kind=project, title)` → 새 `repository_id`(uuid4) 생성 → mapping 추가 → `projects.json`에 기록.
  - 이름/UUID: local DB의 project scope에서 찾는다.
  - 인자 없음 + local project 0개: `--new`를 안내한다. 1개: 그 project로 연결 제안 후 `--yes`로 확정.

**오류**: `not_a_branch_checkout`, `project_not_found`, `project_ambiguous`, `link_ids_required`, `storage_config_conflict`.

**완료 기준**
- T-C06-1: local `pmt link --new` → 새 세션 SessionStart에 overview 표시.
- T-C06-2: hosted 이름 연결 → 기존 실측(canary mapping)과 같은 결과.
- T-C06-3: 같은 checkout 두 번째 branch → 같은 project로 추가.

---

## C-07 짧은 명령 local 지원

**현재**: `easy_cli._roots()`가 hosted가 아니면 `not_configured`로 끝난다.

**변경**: 명령 구현을 저장소 어댑터 두 개로 나눈다. 명령 표면은 같다.

| 명령 | local 내부 절차 | hosted 내부 절차 |
|---|---|---|
| `status` | `read_context(limit=200)` | 같음 |
| `add work|item` | `save_change(kind=work|item)` | 같음 |
| `start <item>` | `claim_task` → 반환 `claim_token`을 DataRoot `easy-claims.json`(0600)에 저장 | 기존(claim_ref) |
| `pause <item> --next` | `release_claim(claim_token, state=Paused, next)` | 기존 |
| `done <item> --test --result` | 미커밋 변경 거부 → `lookup_verification`(before fingerprint) → 테스트 실행 → 출력 `register_resource(evidence)` → `record_verification(outcome, exit_code, criteria, evidence)` → `finish_task(claim_token, result, verification_ids)` | 기존 보조 Item/run 절차 유지 |

규칙(공통): 명령마다 새 `request_id`, 응답 유실 시 같은 ID로 결과 조회 후 재전송, revision 충돌이면 최신 상태 출력 후 중단, 테스트 실패면 완료하지 않음.

**완료 기준**
- T-C07-1: local `add → start → done` → Item Done, verification pass, evidence 존재.
- T-C07-2: local 테스트 실패 → Item In Progress 유지, 점유 유지.
- T-C07-3: local 미커밋 변경 → `uncommitted_changes`.
- T-C07-4: hosted 명령 회귀(기존 실측 표 전체).

---

## C-08 진단 `pmt check`, `pmt mode`

- `pmt mode`: 모드, ConfigRoot/DataRoot, 연결 project, (hosted) endpoint·actor·namespace 한 줄씩. 네트워크 호출 없음.
- `pmt check`
  - local: DB 열기·schema 버전·`setup` 상태 → 진단 fact 쓰기·읽기·같은 요청 재전송.
  - hosted: 기존(인증 호환·조회·기록·재전송) + CA 파일 존재·만료일·credential 저장소 상태.
  - 종료 코드: 전부 ok면 0, 하나라도 실패면 1.

완료 기준: T-C08-1 local, T-C08-2 hosted, T-C08-3 Host 중지 시 `remote_unavailable`로 실패하고 local로 넘어가지 않음.

---

## C-09 모드 전환 `pmt storage switch`

**목적**: local로 쓰던 기기를 Host로 옮기거나 반대로 되돌리는 일을 명시 명령으로만 한다.

```
pmt storage switch --to hosted --handoff <file> [--credential-file <file>] [--export <bundle.zip>]
pmt storage switch --to local                     # hosted 프로필을 local로. 기존 local DB가 있으면 그대로 다시 사용
```

**동작 (`--to hosted`)**
1. 미처리 pending·활성 claim이 있으면 거부(`switch_busy`).
2. `--export`가 있으면 기존 migration의 quiescent backup 번들을 만든다. Host import는 관리자가 `pmt-server import`(S-15)로 **빈 namespace**에 한다. 이 명령은 import하지 않는다.
3. C-03 절차로 hosted 게시(CAS). local DB는 삭제하지 않고 그대로 둔다.

**완료 기준**: T-C09-1 전환 후 local DB 파일 hash 불변, T-C09-2 export 번들 manifest 검증, T-C09-3 claim 보유 중 거부.

---

## C-10 Hook 진입 통합 (Claude/Codex)

**현재**: Claude는 `easy_hook.run`, Codex는 `hooks.main`을 직접 호출하고 `PMT_CONFIG_ROOT`가 없으면 실패한다. Codex hooks.json은 `python` / `py -3`를 호출한다.

**변경**
- 두 제품 모두 `easy_hook.run(--product <p>)`을 쓴다. 제품별 차이는 설정 출처(Claude: plugin option, Codex: C-03 결과)와 출력 형식뿐이다.
- Codex Hook의 Python: Codex는 plugin option이 없으므로 `PMT_PYTHON`이 있으면 사용하고, 없으면 hooks.json의 `python`/`py -3`가 3.13 미만일 때 SessionStart에 "`pmt connect` 후 PMT_PYTHON 설정" 한 줄 안내 후 exit 0.
- legacy 경로(`PMT_CONFIG_ROOT` 직접 설정)는 계속 지원한다.

**완료 기준**: T-C10-1 Claude 실제 새 세션, T-C10-2 Codex `/hooks` 신뢰 후 새 세션, T-C10-3 Hook fixture 회귀(`tests/hook-fixtures`).

---

## C-11 명령 진입점 (Linux/Windows)

**현재**: `integrations/claude/bin/pmt`는 `PYTHONPATH="$root/src:..."`를 쓰는 sh 스크립트다. Windows Git Bash에서 `:` 구분자가 Windows Python에 그대로 전달되는지 실측되지 않았다.

**변경**
- `bin/pmt`(sh): `exec "$PMT_PYTHON" -B "$root/scripts/pmt_easy.py" "$@"`. `pmt_easy.py`가 스스로 `sys.path`에 `src`를 넣는다(PYTHONPATH 미사용).
- `bin/pmt.cmd`(Windows cmd/PowerShell): `"%PMT_PYTHON%" -B "%~dp0..\scripts\pmt_easy.py" %*`.
- `PMT_PYTHON`이 없으면 ConfigRoot의 `client.json`(SessionStart가 기록한 비밀 없는 python 경로)을 읽는다. 둘 다 없으면 exit 3 + 안내.

**완료 기준**: T-C11-1 Linux Bash, T-C11-2 Windows Git Bash(Claude Code Bash 도구), T-C11-3 Windows PowerShell.

---

## C-12 스킬·문서 정리

- `skills/proj-mgmt-tool/SKILL.md`: 짧은 명령 표를 local/hosted 공통으로 고친다. Host 구축 내용 제거, "Host 연결은 인계 파일과 credential로 `pmt connect` 또는 `/plugin`"만 남긴다.
- `references/host-workflow.md`: 클라이언트 관점(연결·오류·pending)만 유지.
- `docs/usage.md`: 0.3.0/schema4 예제 제거, local 시작·hosted 연결·전환 순서로 재작성.
- `docs/handoff/windows-host.md`, `deployment-order.md`: pmt-server 스킬 references로 이동하고 여기에는 링크만 둔다.

완료 기준: 문서 링크 검사, 스킬에 Host 관리 명령(`pmt-host`, `issue-device`)이 남지 않음.
