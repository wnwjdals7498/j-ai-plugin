# Codex handoff: 5단계(pmt / pmt-server 분리)를 Host 서버에서 끝까지 진행

작성: 2026-10-08. 아래 `---` 이후 본문을 PMT Host가 될 Windows 서버의 Codex 새 세션에 그대로 붙여 넣는다. 세션이 끊기면 같은 본문을 다시 붙여 넣고 마지막 줄에 `이어서 진행`을 덧붙인다.

**붙여 넣기 전 (원래 개발 PC에서)**: 5단계 문서(`docs/05-plugin-split.md`, `docs/phase5/`, 이 파일)와 커밋되지 않은 마켓플레이스 재구성(`.claude-plugin/`, `.agents/`, `README.md`, `proj-mgmt-tool/.claude-plugin/`, `.codex-plugin/`, `scripts/pmt.py`, `docs/usage.md`)이 `origin`에 push되어 있어야 한다. Host의 Codex는 GitHub에 있는 내용만 볼 수 있다.

---

너는 이 컴퓨터에서 PMT 5단계 작업의 **메인**이다. 이 Windows 컴퓨터는 이미 PMT Host(0.4.x)를 운영하고 있으며, 5단계가 끝나면 `pmt-server`로 관리되는 Host가 된다. 작업 묶음 A0 → A1 → B/C → D → E → F1을 순서대로 끝까지 진행하라. 다른 컴퓨터에서 해야 하는 F2(Linux 개발 서버)와 F3(Windows 개발 PC)는 실행하지 말고, 그곳에서 쓸 인계 프롬프트를 만들어라.

## 1. 기준

- 저장소: https://github.com/wnwjdals7498/j-ai-plugin (`origin/master`)
- 컴포넌트: `proj-mgmt-tool`(pmt 본체·클라이언트 플러그인), 신규 `pmt-server/`(서버 플러그인)
- 유지할 계약: Core 0.4.x / SQLite schema 5 / graph schema 1 / protocol 1 / Host API v1. 이 계약을 바꿔야 할 것 같으면 **멈추고 이유·영향·대안을 보고**한다.
- 사용자 지시가 문서보다 우선한다. 문서끼리 다르면 `docs/phase5/*` 상세 명세 → `docs/05-plugin-split.md` → 기존 단계 문서 순으로 따르고, 차이를 기록한다.

## 2. 먼저 읽을 것 (전부 통독하지 말고 작업 묶음에 필요한 부분만)

1. `proj-mgmt-tool/AGENTS.md`: 작업·병렬·검증·인계 규칙. 반드시 따른다.
2. `proj-mgmt-tool/docs/05-plugin-split.md`: 목표, 원칙, 구조, 기능 목록, 결정 D1~D8.
3. `proj-mgmt-tool/docs/phase5/README.md`: 없는 기능·수정할 기능·변경 금지 목록, 작업 묶음 A0~F3, 소유 영역, 미확인 사항.
4. 작업 묶음별 명세: `phase5/pmt-spec.md`(C-*), `phase5/pmt-server-spec.md`(S-*), `phase5/communication.md`(X-*, 통신·인계 형식).
5. 환경 절차: `phase5/setup-windows.md`(이 컴퓨터), `phase5/setup-linux.md`(F2 인계 작성 시).
6. 기존 계약: `docs/phase3/host-api-contract.md`, `docs/phase4/runtime-contract.md`, `docs/handoff/simple-setup-design.md`(0.4.1 hosted 실측).

## 3. 시작할 때 할 일 (첫 세션)

### 3.1 현재 Host 조사 (읽기 전용)

운영 중인 Host를 찾아 다음을 기록한다. **값을 바꾸지 않는다. 비밀값은 읽거나 출력하지 않는다.**

- 실행 형태: 작업 스케줄러 작업(`Get-ScheduledTask | Where-Object {$_.Actions.Arguments -match 'pmt'}`), Windows 서비스, 실행 중 프로세스(`Get-CimInstance Win32_Process | Where-Object CommandLine -match 'pmt'`의 CommandLine).
- `pmt-host serve` 인자: `--data-root`, `--config-root`, `--host`, `--port`, `--claim-key-env`(환경변수 **이름**만), `--ssl-certfile`, `--ssl-keyfile`, `--retained-key`.
- Python·venv 경로, 설치된 `proj-mgmt-tool` 버전, 구동 계정, 방화벽 규칙(`Get-NetFirewallRule | Where-Object DisplayName -match 'PMT|8765'`), 인증서 만료일·SAN.
- `/health` 응답(인증 없음).

결과는 `docs/phase5/host-survey.md`에 비밀 없이 쓴다. 기존 Host의 경로·포트는 이후 개발·시험에서 **절대 사용하지 않는다.**

### 3.2 작업 공간

- 개발 checkout: `C:\PMT\src\j-ai-plugin`. 이미 있으면 dirty 변경을 보존하고 그 상태를 보고한다. 없으면 clone한다.
- 브랜치: `origin/master`에서 `feat/phase5-plugin-split`. 이미 있으면 그 브랜치를 이어 쓴다.
- 개발용 venv: `C:\PMT\src\venv-dev`(Python 3.13 이상, `pip install -e "proj-mgmt-tool[test,host,host-test]"`). 운영 Host의 venv와 분리한다.
- 시험 데이터: 저장소의 `proj-mgmt-tool/.pmt-test/` 또는 `C:\PMT\work\phase5-test\` 아래 격리된 ConfigRoot/DataRoot. 시험용 Host는 loopback과 다른 포트(예 `127.0.0.1:18765`)만 사용한다.
- 증거: `proj-mgmt-tool/docs/phase5/evidence/<YYYY-MM-DD>/<작업 ID>/`. 명령, 실제 종료 코드, 환경(OS/Python/commit), pytest 출력·junit. 비밀은 넣지 않는다.

### 3.3 결정 D1~D8 확정

`05-plugin-split.md` §8의 결정 8개를 **한 표로 사용자에게 보여 주고 한 번에 확인**받는다. "제안대로"라고 하면 제안값을 채택한다. 결과를 `docs/phase5/decisions.md`에 기록한다. 확정 전에는 A1 이후로 진행하지 않는다. 구현 중 자기 범위 안에서 정한 선택도 같은 파일의 "구현 선택" 절에 근거와 함께 남긴다.

## 4. 진행 기록과 재개

- `proj-mgmt-tool/docs/phase5/progress.md`를 만들고 작업 묶음마다 한 행을 둔다: `ID | 상태(미착수/진행/완료/blocked) | commit | 시험(명령·결과) | 증거 경로 | 미해결·다음 할 일`.
- 작업 묶음을 **시작할 때와 끝날 때** 갱신하고, 끝날 때 코드·문서·progress를 한 커밋으로 묶는다. 커밋 메시지는 `feat(pmt): …`/`feat(pmt-server): …`/`docs(pmt): …` 형식이고 작업 ID를 포함한다.
- **새 세션(재개)**: `git fetch` → `git status`(dirty 보존) → `progress.md`와 `git log --oneline -20` 확인 → 첫 번째 미완료 묶음부터 이어 간다. 완료된 묶음은 그 결과를 무효로 만드는 변경이 없으면 다시 하지 않는다. 진행 중이던 묶음은 dirty 변경과 progress의 "다음 할 일"부터 재개한다.
- 문맥이 길어지면 묶음 경계에서 progress를 갱신·커밋한 뒤 계속한다.

## 5. 작업 묶음별 지시

각 묶음의 범위·완료 시험 ID는 `phase5/README.md` §2.2 표를 따른다. 아래는 이 컴퓨터에서 지켜야 할 추가 지시다.

**A0 기준선**
- push된 재구성 파일(루트 `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json`, `proj-mgmt-tool/.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `scripts/pmt.py`)과 5단계 문서가 있는지 확인한다. 없으면 멈추고 보고한다.
- 전체 `pytest`를 실행해 기준선(통과/실패/skip 수, 실패 목록)을 기록한다. 원래 실패하던 시험은 고치지 말고 기록만 한다.
- `claude`·`codex` CLI가 있으면 버전을 기록하고 `claude plugin validate`를 실행한다. 없으면 해당 시험은 `blocked`로 둔다.
- `docs/phase3/evidence/local-acceptance/**/package-snapshot` 같은 과거 증거 파일은 추가·삭제하지 않는다.

**A1 공통 계약**(메인 직접): plugin.json 통합, `pyproject` script `pmt-server` 자리, `src/pmt/handoff.py`(X-02 검증·생성), `host/cli.py`의 `_serve`를 동작 그대로 내부 함수로 분리, `AuthRegistry.list_devices`. `pmt-host serve` 회귀(health·compat 동일)를 확인한다.

**B1~B3 클라이언트 / C1~C2 서버**
- 서로 독립이므로 병렬로 진행할 수 있다. AGENTS.md대로 네이티브 서브에이전트(`gpt-6-luna`)가 가능하면 클라이언트 영역과 서버 영역에 하나씩 배정하고, 소유 파일을 `phase5/README.md` §2.1로 나눈다. 같은 파일을 동시에 고치지 않는다. 서브에이전트를 쓸 수 없으면 B1 → B2 → B3 → C1 → C2 순으로 직접 한다.
- 서브에이전트 지시에는 작업 ID, 목표, 소유 범위, 입출력 계약, 필수 시험 ID, 증거 위치, 제외 범위를 넣는다. 결과 통합·커밋은 메인이 한다.
- Windows 특이 사항(DPAPI `ctypes`, `%APPDATA%` 경로, `pmt.cmd`)은 이 컴퓨터에서 실제로 시험한다. Linux 전용 항목(0600 파일 권한 등)은 단위 시험으로 검증하고, 실제 Linux 확인은 F2 인계로 넘긴다.
- C1에는 기존 Host 인수 `init --adopt`(S-03)를 포함한다. T-S03-4는 **운영 DB 사본**으로만 시험한다(§6 F1의 백업 사본 또는 시험용 Host로 만든 DB). 운영 경로에 adopt를 실행하지 않는다.

**D1 → D2 인계**: D1은 시험용 Host(18765)에 project·기기·인계 파일을 만들고, D2는 그 인계 파일로 격리된 클라이언트 ConfigRoot에서 `pmt connect`·`pmt link`·`pmt check`를 확인한다.

**E1~E3 운영·패키징**
- 작업 스케줄러·방화벽·systemd는 **명령 생성 결과만 시험**한다(dry-run, `plan` 출력과 기대 명령 비교). 이 컴퓨터에 실제로 등록하는 것은 F1에서만 한다.
- `scripts/build_plugins.py`로 두 번들을 만들고 manifest·파일 목록을 확인한다. 빌드 결과(`dist/`)는 커밋하지 않는다.
- 문서 정리(C-12)까지 끝나면 전체 `pytest`를 다시 실행해 A0 기준선과 비교한다. 새 실패가 0이어야 한다.

**E3 종료 관문 (사용자 확인)**: 결과 요약(묶음별 상태, 시험 비교, 남은 미확인)을 보고하고 **branch push와 PR 생성 승인**을 받는다. 승인 후 `git push -u origin feat/phase5-plugin-split` → PR(본문 끝에 사용한 도구·모델 표기). master 직접 push·force push는 하지 않는다. F1은 push된 commit 해시로 설치한다.

## 6. F1: 운영 중인 Host 인수 (실제 서버 작업)

**각 단계는 실행 전 무엇을 바꾸는지 보여 주고 사용자 승인을 받는다.** 개발 서버 사용자에게 작업 시간 공지가 필요하면 사용자에게 알린다.

1. **새 venv 설치**: `C:\PMT\app\<version>\venv`에 push된 commit으로 `proj-mgmt-tool[host]`를 설치한다(`setup-windows.md` §1.1, `@<commit>` 사용). 기존 Host venv는 건드리지 않는다. `pmt-server version`을 확인한다.
2. **adopt 계획**: 3.1 조사 결과로 `pmt-server init --adopt …`를 dry-run하고 출력(만들 파일, 바뀌지 않는 것)을 보여 준다.
3. **정지·백업 (승인 필요)**: 활성 claim·run이 없음을 확인한다. 있으면 멈추고 보고한다. 기존 Host를 멈추기 전 기존 자동 시작 정의를 보존한다(`Export-ScheduledTask` XML 또는 서비스 설정을 `C:\PMT\rollback\<UTC>\`에 저장). 기존 Host를 정지한다. 데이터·설정 폴더를 그대로 복사해 `C:\PMT\rollback\<UTC>\copy\`에 둔다(파일 복사, hash 기록).
4. **adopt 적용**: `init --adopt --apply` → `pmt-server backup --apply` → `pmt-server restore-check` → `doctor`. fail이 있으면 6.9 롤백.
5. **수동 기동**: `pmt-server serve`를 콘솔로 실행하고 `/health`, 진단용 기기(`device issue --actor host-diagnostic`, 대상 canary project, credential은 파일로만)로 compat·조회를 확인한 뒤 그 기기를 revoke한다. 기존 기기 목록·namespace_id가 조사 결과와 같아야 한다.
6. **자동 시작·방화벽 교체 (승인 필요)**: `plan --only service,firewall`을 보여 주고 승인 후 기존 작업은 **사용 안 함(Disable)**으로 바꾸고(삭제하지 않음) `apply`로 새 작업·규칙을 등록한다. 기존 방화벽 규칙도 삭제하지 말고 내용을 기록한 뒤 새 규칙과 중복이면 사용 안 함으로 바꾼다.
7. **재부팅 확인 (승인 필요)**: 재부팅 후 `pmt-server status`(같은 namespace, 서비스 실행), 프로세스 강제 종료 후 재시작 확인. 재부팅이 승인되지 않으면 해당 항목은 `blocked`로 기록한다.
8. **claim key 보관 이전 (D3 채택 시, 승인 필요)**: `secret migrate-claim-key --to dpapi`로 같은 key_id·같은 값을 옮기고 재시작 확인. 기존 환경변수 제거는 사용자가 직접 하도록 안내한다.
9. **롤백**: 어느 단계든 실패하면 새 작업을 정지·사용 안 함 → 보존한 기존 작업 정의를 다시 사용으로 바꿈 → 기존 Host 기동 → `/health`. DB·설정이 바뀌었으면 3단계 복사본과 hash를 비교하고 사용자 결정 없이 덮어쓰지 않는다.
10. **개발 기기 발급 (승인 필요)**: 사용자가 원하는 기기(예 Linux 개발 서버의 Claude, Windows 개발 PC의 Claude/Codex)마다 `device issue --handoff-out … --credential-out …`. credential 파일은 `C:\ProgramData\PMT\handoff\`에 ACL로 보호하고, **채팅·로그·Git에 값을 쓰지 않는다**. 전달 방법은 사용자가 정한다. 기존 0.4.1 기기(yss)는 회수하지 않는다. 인계 파일 방식으로 바꿀지는 사용자가 정한다.

## 7. F2·F3 인계 프롬프트 작성

이 컴퓨터에서 실행하지 않는다. F1 결과의 **비밀 없는 실제 값**(endpoint, 공개 CA sha256, namespace_id, 발급한 device_id·actor, project/repository 이름, 설치할 commit·버전, 인계 파일 이름)을 넣어 다음 두 파일을 만들고 커밋한다.

- `proj-mgmt-tool/docs/phase5/handoff-f2-linux-dev.md`: Linux 개발 서버의 Codex 또는 Claude용. `setup-linux.md` 1장 절차(0.4.1 설정 정리 포함), 시험 T-C02-3·4, T-C04-1·4, T-C08-2, 기존 hosted 실측 표 재현, 미확인 사항(서브에이전트 `CLAUDE_ENV_FILE`), 결과 보고 형식.
- `proj-mgmt-tool/docs/phase5/handoff-f3-windows-dev.md`: Windows 개발 PC용. `setup-windows.md` 2장 절차(local 시작 → hosted 전환), 시험 T-C02-2, T-C10-1·2, T-C11-2·3, T-C09-1~3, 미확인 사항(Git Bash 경로 전달, Codex `py -3`).

두 프롬프트 모두 "credential은 사용자가 별도로 전달한다", "기존 설정·DB 보존", "결과를 이 저장소 progress에 반영할 수 있게 보고"를 포함한다.

## 8. 반드시 멈추고 사용자에게 확인받을 때

- D1~D8 확정(3.3).
- E3 종료 관문: push·PR.
- F1의 3, 6, 7, 8, 10단계와 롤백 실행.
- Host API·DB schema·claim·인증 계약을 바꿔야 할 때.
- 새 외부 의존성 추가(예 D5의 `cryptography`)를 제안할 때.
- 관리자 권한·네트워크·설치 권한이 없어 진행할 수 없을 때(필요한 권한을 구체적으로 요청).

나머지 구현 선택은 직접 결정하고 `decisions.md`에 근거를 남긴 뒤 계속 진행한다. 하나가 `blocked`여도 의존하지 않는 다른 묶음은 계속한다.

## 9. 하지 말 것

- 운영 중인 Host의 정지·재시작·설정·데이터·claim key·기기 변경을 F1 승인 단계 밖에서 하지 않는다.
- claim key, 기기 credential, TLS 개인키 값을 읽어 출력하거나 로그·Git·채팅·증거에 넣지 않는다. 환경변수 이름과 파일 경로로만 다룬다.
- 기존 데이터·venv·작업 정의·방화벽 규칙을 삭제하지 않는다(사용 안 함, 보존 복사 사용).
- 업무 테이블에 직접 SQL로 쓰지 않는다. 업무 데이터는 정식 operation으로만 만든다.
- fixture·dry-run 성공을 실제 서버 성공으로 기록하지 않는다. 실행하지 않은 시험은 `미실행` 또는 `blocked`로 쓴다.
- master 직접 push, force push, hook 우회(`--no-verify`)를 하지 않는다.
- 다른 컴퓨터(개발 서버·개발 PC)에 접속해 작업하지 않는다.

## 10. 보고

**묶음이 끝날 때마다** AGENTS.md 형식으로 짧게 보고한다: 작업 ID, 결과 요약, 변경 영역, 계약 변경 제안, 시험·종료 코드·증거, commit·dirty, 미해결·다음 조치.

**최종 보고**에는 다음을 넣는다.
- 묶음별 상태표(A0~F1, F2·F3는 "인계 작성").
- A0 대비 시험 비교.
- 운영 Host의 현재 상태: 버전, namespace(동일 여부), 서비스·방화벽, 백업 위치, 롤백 자료 위치.
- 발급한 기기 목록(비밀 없음)과 credential 전달 대기 항목.
- F2·F3 인계 파일 경로.
- 남은 미확인 사항(`phase5/README.md` §2.4)의 실제 확인 결과 또는 미확인 사유.
