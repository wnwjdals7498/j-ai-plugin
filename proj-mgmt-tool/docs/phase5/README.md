# 5단계 작업 안내: 기능 차이와 작업 계획

[개요·목표·결정](../05-plugin-split.md) · [pmt 명세](pmt-spec.md) · [pmt-server 명세](pmt-server-spec.md) · [통신·인계 기준](communication.md) · [Windows 설정](setup-windows.md) · [Linux 설정](setup-linux.md)

이 문서는 2026-10-08 코드(`8bd698c` + 커밋되지 않은 마켓플레이스 재구성)를 명세와 대조해, **없는 기능**, **수정해야 할 기능**, **그대로 쓰는 기능**을 나누고 작업 순서를 정한다.

## 1. 기능 차이

### 1.1 없는 기능 (신규 구현)

| 명세 | 없는 것 | 위치(예정) | 크기 |
|---|---|---|---|
| C-01 | local 자동 준비(프로필 생성 → `setup`)와 판정표 전체 | `src/pmt/client_setup/mode.py` | M |
| C-03 | 인계 해석, `pmt connect`/`disconnect`, CA 저장, `projects.json` | `client_setup/connect.py` | M |
| C-04 | 클라이언트 credential 저장소(Linux 0600 파일, Windows DPAPI `ctypes`) | `client_setup/credentials.py` | M |
| C-06 | local `pmt link --new`, `pmt unlink`, `pmt projects` | `easy_cli.py` | S |
| C-07 | 짧은 명령 local 어댑터(`claim_token` 저장, local `done` 절차) | `client_setup/local_commands.py` | M |
| C-08 | `pmt mode`, local `pmt check` | `easy_cli.py` | S |
| C-09 | `pmt storage switch` | `client_setup/switch.py` | M |
| C-11 | `bin/pmt.cmd`, `scripts/pmt_easy.py`, `client.json` | `bin/`, `scripts/` | S |
| X-02 | 인계 형식 검증·생성 공용 모듈 | `src/pmt/handoff.py` | S |
| S-01 | `pmt-server version` | `src/pmt/server_admin/` | S |
| S-02 | `host-config.json` 스키마·CAS·`config show/set/validate` | `server_admin/config.py` | M |
| S-03 | `init`, 기존 Host 인수 `init --adopt`, `secret migrate-claim-key` | `server_admin/init.py` | M |
| S-04 | claim key 보관 source(`file`, `dpapi`), 생성·회전·retire | `server_admin/secrets.py` | M |
| S-05 | `tls register/check`(+선택 `create-ca/issue`) | `server_admin/tls.py` | M |
| S-06 | `doctor` 13개 항목 | `server_admin/doctor.py` | L |
| S-07 | 설정 기반 `serve` 런처(사전 검사 포함) | `server_admin/serve.py` | S |
| S-08 | `plan`/`apply` 컴포넌트 엔진 | `server_admin/apply.py` | L |
| S-09 | 작업 스케줄러·systemd 등록, `service` 명령 | `server_admin/service_win.py`, `service_systemd.py` | L |
| S-10 | 방화벽 규칙(Windows/firewalld/ufw) | `server_admin/firewall.py` | M |
| S-11 | `project add`(bootstrap 기기→`create_scope`→폐기), `repo add`, registry | `server_admin/projects.py` | M |
| S-12 | `device` 명령, `AuthRegistry.list_devices`(신규 읽기 함수) | `server_admin/devices.py`, `host/auth.py` | M |
| S-13 | `handoff create` | `server_admin/handoff.py` | S |
| S-14 | `status` | `server_admin/status.py` | S |
| S-15 | `backup`/`restore-check`/`import`/`prune` 래퍼, 백업 예약 | `server_admin/backup.py` | M |
| S-16 | 회전 로그 설정, `logs` | `server_admin/logging.py` | S |
| S-17 | `upgrade` | `server_admin/upgrade.py` | M |
| S-18 | `pmt-server/` 플러그인 폴더, 스킬, 템플릿, SessionStart Hook | `j-ai-plugin/pmt-server/` | M |
| X-01 | 마켓플레이스 `pmt-server` 항목(Claude·Codex) | `.claude-plugin/`, `.agents/plugins/` | S |
| X-03 | 빌드 server 대상 | `scripts/build_plugins.py` | S |

### 1.2 수정해야 할 기능

| 대상(현재 위치) | 현재 동작 | 고칠 내용 | 명세 |
|---|---|---|---|
| `proj-mgmt-tool/.claude-plugin/plugin.json` vs `integrations/claude/.claude-plugin/plugin.json` | 두 파일의 userConfig가 다름(`python_path`만 / Host 값 필수) | 루트 하나로 통합, Host 값 선택화, `handoff_file` 추가, integrations 쪽 삭제 | C-02 |
| `easy_setup.read_options` | Host 5개 값 필수, 없으면 `unconfigured` | 조건부 필수(`host_url` 있을 때), `handoff_file` 우선 | C-01, C-02 |
| `easy_setup.prepare` | hosted만 준비, local 프로필이면 `easy_setup_local_profile` 오류 | 판정표 구현, local 경로 추가, 오류 code 정리 | C-01 |
| `easy_setup.default_roots` | 모든 OS에서 XDG 경로 | Windows `%APPDATA%`/`%LOCALAPPDATA%`, 기존 경로 호환 | C-05 |
| `easy_setup.session_environment` / `write_env_file` | `PMT_HOST_CREDENTIAL`을 `CLAUDE_ENV_FILE`에 평문 기록 | credential 제외, 저장소 사용 | C-04 |
| `easy_hook.run` | Claude 전용, legacy 판정이 `PMT_CONFIG_ROOT` 유무뿐 | 제품 인자 일반화, Codex 공용, 판정표 사용 | C-10 |
| `easy_cli._roots` 및 각 `cmd_*` | hosted 아니면 `not_configured` | 저장 어댑터 분리(local/hosted), credential 로드 | C-07, C-08 |
| `easy_cli.cmd_link` | hosted mapping만, `--project/--repository` UUID 필수 | local 생성·이름 검색(`projects.json`) | C-06 |
| `integrations/claude/bin/pmt` | `PYTHONPATH=…:…` 사용, `PMT_PYTHON` 없으면 실패 | `pmt_easy.py` 호출, `client.json` 대체 경로 | C-11 |
| `integrations/codex/hooks/hooks.json`, `hook.py` | `hooks.main` 직접, `PMT_CONFIG_ROOT` 필수 | `easy_hook.run --product codex`, 3.13 미만 안내 | C-10 |
| `host/cli.py` `_serve` | 인자 객체 기반, 키는 환경변수 이름으로만 | 키 dict·로그 설정을 받는 내부 함수로 분리(동작 동일), `pmt-host`는 그대로 유지 | S-04, S-07, S-16 |
| `host/auth.py` | 기기 목록 조회 없음 | `list_devices()` 읽기 전용 추가(계약 변경 없음) | S-12 |
| `pyproject.toml` | scripts: `pmt`, `pmt-host` / version 0.4.0 | `pmt-server` 추가, 0.4.1(Core) 결정, (D5 채택 시) `cryptography` | S-01 |
| `scripts/build_plugins.py` | 3제품, integrations manifest 복사, `bin/pmt`만 | 루트 manifest, `bin/pmt.cmd`, `scripts/pmt_easy.py`, server 대상 | X-03 |
| `tests/test_easy_setup.py`, `test_packaging.py`, `test_phase4_packaging_actual.py` | 현재 hosted 전용·단일 번들 기대 | 판정표·local·두 번들 기대값 | X-04 |
| `skills/proj-mgmt-tool/SKILL.md`, `references/host-workflow.md` | hosted 짧은 명령만, Host 운영 설명 혼재 | local/hosted 공통, Host 구축 제거 | C-12 |
| `docs/usage.md` | 0.3.0/schema4 예제 잔존 | local 시작·hosted 연결·전환 순서로 재작성 | C-12 |
| `docs/handoff/windows-host.md`, `deployment-order.md`, `development-plugin.md` | AI용 수동 프롬프트 | pmt-server 스킬 references로 이동, 명령 기반 절차로 대체 | C-12, S-18 |
| `j-ai-plugin/README.md` | 플러그인 1개 설치 안내 | 두 플러그인·역할·설치 위치 | X-01 |

### 1.3 그대로 쓰는 기능 (변경 금지)

| 기능 | 이유 |
|---|---|
| Host API v1 경로·헤더·상태 코드·크기 상한 (`host/server.py`, `host/application.py`) | 클라이언트·서버 호환의 기준 |
| `HttpStore` 전송·TLS 검증·redirect 거부 | 운영 통신 계약 |
| `AuthRegistry`의 발급·회전·폐기·인증 로직, claim HMAC·lease | 보안 계약. 운영 도구는 감싸기만 한다 |
| `storage_config.configure_storage`/프로필 형식/`credential_env` | 클라이언트 설정 계약. 새 저장소는 프로세스 환경변수로 주입 |
| `hooks.py` 이벤트 정규화·`record_event`·재개 문맥 조회 | Hook 계약 |
| 2~4단계 operation, pending, migration/transfer, JSON CLI(`pmt.cli`) | 기존 기능 |
| hosted `pmt done`의 보조 Item/run 절차 | 실측 확정 절차 |

## 2. 작업 계획

### 2.1 작업 영역(소유)

| 영역 | 소유 파일 | 담당 |
|---|---|---|
| 공통 | 두 plugin.json·marketplace 2개·`pyproject.toml`·`src/pmt/handoff.py`·`build_plugins.py`·`host/cli.py`·`host/auth.py`·문서 | 메인 |
| 클라이언트 | `easy_setup.py`, `easy_hook.py`, `easy_cli.py`, `client_setup/`, `integrations/`, `bin/`, `scripts/pmt_easy.py`, `tests/client_setup/` | 작업자 1 |
| 서버 | `server_admin/`, `j-ai-plugin/pmt-server/`, `tests/server_admin/` | 작업자 2 |

같은 파일을 두 영역이 고치지 않는다. 공통 파일 변경이 필요하면 메인에게 요청한다.

### 2.2 작업 묶음

| ID | 내용 | 선행 | 영역 | 완료 기준(시험) |
|---|---|---|---|---|
| **A0** | 커밋되지 않은 마켓플레이스 재구성(`.claude-plugin/`, `.codex-plugin/`, `.agents/`, `scripts/pmt.py`, `docs/usage.md`) 검토·커밋. 결정 D1~D8 확정 | 없음 | 메인 | 기존 전체 시험 통과, `claude plugin validate` |
| **A1** | plugin.json 통합(C-02 manifest 부분), `pyproject` script 자리(`pmt-server`), `handoff.py`(X-02) 검증·생성 함수와 시험, `_serve` 내부 함수 분리, `list_devices` | A0 | 메인 | X-02 스키마 시험, `pmt-host serve` 회귀(health/compat 동일) |
| **B1** | C-05 경로, C-04 저장소, C-01 판정표(local·hosted), `easy_hook` 일반화 | A1 | 클라이언트 | T-C01-1~4, T-C04-1~3, T-C05-1~2 |
| **B2** | C-07 local 어댑터, C-06 link, C-08 mode/check | B1 | 클라이언트 | T-C06-1·3, T-C07-1~4, T-C08-1·3 |
| **B3** | C-11 진입점, C-10 Codex Hook | B1 | 클라이언트 | T-C10-3, T-C11-1 (Windows 항목은 F에서) |
| **C1** | S-01 version, S-02 config, S-03 init, S-04 secret(file·dpapi·env) | A1 | 서버 | T-S01-1~2, T-S02-1~3, T-S03-1~4, T-S04-4 |
| **C2** | S-05 tls register/check, S-06 doctor, S-07 serve, S-16 logs, S-14 status | C1 | 서버 | T-S05-1~3, T-S06-*, T-S07-1~2 |
| **D1** | S-11 project, S-12 device, S-13 handoff | C2 | 서버 | T-S11-1~2, T-S12-1~4, T-S13-1 |
| **D2** | C-03 connect, C-06 hosted 이름 연결 | B2, D1의 인계 샘플 | 클라이언트 | T-C03-1~4, T-C06-2, T-S13-2 |
| **E1** | S-08 plan/apply, S-09 service, S-10 firewall(명령 생성 시험만) | C2 | 서버 | T-S08-1~2 (명령 출력 비교) |
| **E2** | S-15 backup·restore-check·import, S-17 upgrade, C-09 switch | D1, D2 | 서버+클라이언트 | T-S15-1~3, T-S17-2, T-C09-1~3 |
| **E3** | S-18 플러그인·스킬·템플릿, X-01 마켓플레이스, X-03 빌드, X-04 포장 시험, C-12 문서 | E1, E2 | 메인+서버 | T-S18-1·3, T-C02-1, `test_packaging` |
| **F1** | 실측: 운영 중인 Windows Host를 `init --adopt`로 인수 → 서비스·방화벽 교체 → 운영 확인([Windows 1장](setup-windows.md#1-windows-host-서버)) | E3 | 메인 | 1.9 운영 확인 5항목, T-S04-1~3, T-S09-1~3, T-S10-1, T-S17-1 |
| **F2** | 실측: Linux 개발 서버 연결(0.4.1 → 0.5.0 업데이트 포함) | F1 | 메인 | T-C02-3·4, T-C04-4, T-C08-2, 기존 hosted 실측 표 재현 |
| **F3** | 실측: Windows 개발 PC(local 시작 → hosted 전환), Codex 1개 | F1 | 메인 | T-C02-2, T-C10-1·2, T-C11-2·3, T-C09 실제 |

### 2.3 순서와 병렬

```
A0 → A1 ─┬─ B1 ─┬─ B2 ───────────┐
         │      └─ B3            ├─ D2 ─┐
         └─ C1 → C2 ─┬─ D1 ──────┘      ├─ E2 ─┐
                     └─ E1 ─────────────┼──────┴─ E3 → F1 → F2
                                        │                └→ F3
```

- B 계열과 C 계열은 A1 이후 동시에 진행한다(기본 동시 작업 3개 이내).
- D2는 D1이 만든 실제 인계 샘플(시험용 Host)로 시험한다.
- F 단계는 순서대로 실제 환경에서 한다. F1이 끝나야 개발 기기가 붙을 Host가 생긴다.

### 2.4 미확인 사항 (해당 작업에서 먼저 확인)

| 사항 | 확인 작업 | 확인 방법 |
|---|---|---|
| Windows Git Bash에서 `PYTHONPATH`·Windows 경로 전달 | B3/F3 | Claude Code Bash 도구에서 `pmt mode` |
| `CLAUDE_ENV_FILE` 값이 서브에이전트 Bash에도 적용되는지 | B1/F2 | 서브에이전트에서 `pmt mode` |
| Codex Hook의 Python 선택(`python`/`py -3`)과 3.13 이상 보장 방법 | B3/F3 | 실제 Codex 새 세션 |
| LocalService 작업 스케줄러 실행에서 LocalMachine DPAPI 복호화·파일 ACL 읽기 | C1/F1 | 실제 작업으로 `serve` 시작 |
| Uvicorn이 `${CREDENTIALS_DIRECTORY}`의 TLS 키를 읽는지(systemd) | C2 | Linux 시험 VM |
| Claude Code userConfig `directory` 타입 지원 | E3 | `claude plugin validate` + `/plugin` 화면 |
| Rocky 8 Python 3.13 설치 방법의 조직 표준 | F2 | 서버 관리자 확인 |
| Windows 한국어 환경의 `New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\LOCALSERVICE"` 이름 처리 | F1 | 실제 등록 결과 |

### 2.5 보고 형식

작업자는 [AGENTS.md](../../AGENTS.md)의 인계 형식(작업 ID, 결과 요약, 변경 영역, 계약 변경 제안, 시험·종료 코드·증거, commit·dirty, 미해결)을 따른다. 증거는 `docs/phase5/evidence/<YYYY-MM-DD>/<작업 ID>/`에 둔다.

## 구현 상태

| 작업 | 상태 | 근거 |
|---|---|---|
| A0~F3 | 미착수 | 설계 문서만 작성(2026-10-08) |
