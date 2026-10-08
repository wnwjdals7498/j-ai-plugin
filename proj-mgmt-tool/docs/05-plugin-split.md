# 5단계: pmt / pmt-server 플러그인 분리

- 작성일: 2026-10-08
- 상태: 설계. 구현·실측 결과는 [5단계 구현 상태](phase5/README.md#구현-상태)에 별도로 기록한다.
- 기준 코드: Core 0.4.0 / SQLite schema 5 / graph schema 1 / protocol 1 / Host API v1

## 1. 왜 나누는가

현재 `pmt-lifecycle` 플러그인 하나가 두 역할을 함께 맡는다.

| 역할 | 실행 위치 | 지금의 문제 |
|---|---|---|
| 개발 환경에서 프로젝트 기록·재개 | 개발 PC·개발 서버의 Claude Code/Codex | 마켓플레이스 설치본은 Host 값이 없으면 안내만 하고 멈춘다. local 모드로 쓸 진입점이 없다. 짧은 명령(`pmt check/add/start/done`)은 hosted 전용이다. |
| 저장 Host 구축·운영 | Windows(또는 Linux) Host 서버 | `pmt-host serve` 인자 8개 이상, claim key 환경변수, 작업 스케줄러·방화벽·기기 발급·인계서를 사람이 AI용 프롬프트([windows-host.md](handoff/windows-host.md))를 보며 손으로 맞춘다. 같은 설정을 다시 만들 수 없다. |

두 역할은 설치 대상 컴퓨터, 사용자, 권한, 의존성(FastAPI/Uvicorn)이 모두 다르다. 그래서 **사용자 경험과 설치 단위는 둘로 나누고, 저장 계약과 파이썬 본체는 하나로 유지한다.**

## 2. 두 플러그인

### pmt (plugin id `pmt-lifecycle`, 표시 이름 PMT)

> 개발 환경의 AI 세션이 프로젝트 상태·결정·근거를 남기고 다음 세션에서 이어가게 한다. 저장 위치는 **로컬 SQLite** 또는 **PMT Host** 중 하나다.

- 대상: 코드를 작성하는 모든 기기(Windows 개발 PC, Linux 개발 서버).
- 목표
  1. 설치 직후 추가 설정 없이 local 모드로 바로 쓸 수 있다.
  2. Host 연결은 `pmt-server`가 발급한 인계 파일 하나와 credential 하나로 끝난다.
  3. local·hosted에서 같은 짧은 명령(`pmt link/status/add/start/done/pause/check`)을 쓴다.
  4. hosted 실패를 local로 대체하지 않는다. 모드 변경은 명시 명령으로만 한다.
  5. 기존 JSON CLI·2~4단계 operation·Hook 계약은 그대로 유지한다.

### pmt-server (plugin id `pmt-server`)

> PMT Host를 **선언형 설정 파일 하나**로 설치·실행·운영하고, 개발 기기에 줄 인계 파일을 만든다.

- 대상: Host 서버 1대(Windows 우선, Linux 지원). 관리자만 사용한다.
- 목표
  1. Host 설정이 `host-config.json` 한 파일에 모이고 `plan → apply`로 같은 상태를 반복해서 만들 수 있다.
  2. `doctor` 하나로 Python·의존성·경로 권한·비밀·TLS·포트·방화벽·자동 시작을 점검한다.
  3. 프로젝트·저장소 등록, 기기 발급·회전·폐기, 인계 파일 생성을 명령으로 끝낸다.
  4. 자동 시작(Windows 작업 스케줄러, Linux systemd), 백업·복원 확인, 업그레이드 절차를 명령으로 제공한다.
  5. Host API·DB·claim 계약은 바꾸지 않는다. 운영 계층만 추가한다.

## 3. 기준(원칙)

| 구분 | 기준 |
|---|---|
| 상태 원천 | hosted: Host SQLite 하나가 공유 이력·Queue·점유·리소스의 원천. local: 기기의 `pmt.sqlite3`가 원천. 두 개의 shared primary를 동시에 쓰지 않는다. Git 문서·graph는 그대로 기준 문서다. |
| 대체 금지 | hosted 프로필에서 연결·인증 실패는 오류다. local DB를 만들거나 열지 않는다. 기존 hosted 프로필을 local로 자동 전환하지 않는다. |
| 설정 원천 | 클라이언트: ConfigRoot의 storage 프로필(기존, CAS). 서버: Host ConfigRoot의 `host-config.json`(신규, CAS). 둘 다 비밀값을 담지 않고 비밀의 **위치**만 담는다. |
| 비밀 | claim key·TLS 개인키·기기 credential은 출력·로그·Git·인계 JSON·shared metadata에 넣지 않는다. credential은 발급 시 한 번만 보여 주고 별도 경로로 전달한다. |
| 계약 | Host API v1, protocol-v1 envelope, 오류 code/exit 의미, `request_id` 재전송·지문, claim HMAC 규칙은 변경하지 않는다. |
| 버전 | 두 플러그인은 같은 release 번호로 함께 배포한다. 호환 판정은 Core major.minor·DB schema·graph schema·protocol 일치다(기존 규칙). |
| 코드 위치 | 파이썬 패키지 `pmt` 하나. Host 운영 도구는 `pmt.server_admin`에 추가한다. pmt-server 플러그인은 런타임을 복사하지 않고 Host venv의 Python을 호출한다. |
| 검증 | 단위 시험·fixture·실제 제품 설치·실제 서버 운영을 구분해 기록한다. 실행하지 않은 경로를 완료로 쓰지 않는다. |

## 4. 구조

```
j-ai-plugin/
├─ .claude-plugin/marketplace.json      # pmt-lifecycle, pmt-server
├─ .agents/plugins/marketplace.json     # Codex: 같은 두 항목
├─ proj-mgmt-tool/                      # pmt 플러그인 루트 + 파이썬 패키지 원천
│   ├─ .claude-plugin/plugin.json       # 단일 userConfig (C-02)
│   ├─ .codex-plugin/plugin.json
│   ├─ bin/pmt, bin/pmt.cmd             # 짧은 명령 (C-11)
│   ├─ integrations/{claude,codex,opencode}/
│   ├─ skills/proj-mgmt-tool/
│   ├─ src/pmt/                         # 기존 본체 (host/ 포함)
│   │   ├─ client_setup/                # 신규: 모드 결정·인계 가져오기·credential 저장소
│   │   └─ server_admin/                # 신규: pmt-server CLI
│   └─ pyproject.toml                   # scripts: pmt, pmt-host(호환), pmt-server
└─ pmt-server/                          # pmt-server 플러그인 루트 (런타임 없음)
    ├─ .claude-plugin/plugin.json
    ├─ .codex-plugin/plugin.json
    ├─ hooks/hooks.json                 # SessionStart: 관리 환경변수 주입만
    ├─ bin/pmt-server, bin/pmt-server.cmd
    ├─ skills/pmt-server/ (SKILL.md, references/)
    └─ templates/                       # host-config 예시, systemd unit, 작업 스케줄러 XML
```

Host 서버에는 `pip install "proj-mgmt-tool[host] @ git+…"`로 같은 release의 본체를 venv에 설치하고, pmt-server 플러그인은 그 venv의 Python만 가리킨다. 개발 기기는 지금처럼 플러그인 폴더 안의 `src/`를 사용한다(서버 의존성 없음).

## 5. 기능 목록

상세 명세는 [pmt 명세](phase5/pmt-spec.md), [pmt-server 명세](phase5/pmt-server-spec.md), 공통 항목은 [통신·인계 기준](phase5/communication.md)에 있다.

### pmt

| ID | 기능 | 해결하는 목표 |
|---|---|---|
| C-01 | 저장 모드 결정·자동 준비 | 1, 4 |
| C-02 | 단일 플러그인 설정(userConfig) | 1, 2 |
| C-03 | 인계 가져오기 `pmt connect` | 2 |
| C-04 | 클라이언트 credential 저장소 | 2, 보안 |
| C-05 | OS별 기본 경로 | 1 |
| C-06 | 프로젝트 연결 `pmt link` (local/hosted) | 3 |
| C-07 | 짧은 명령 local 지원 | 3 |
| C-08 | 진단 `pmt check`, `pmt mode` | 2, 3 |
| C-09 | 모드 전환 `pmt storage switch` | 4 |
| C-10 | Hook 진입 통합 (Claude/Codex) | 1, 5 |
| C-11 | 명령 진입점 (Linux/Windows) | 3 |
| C-12 | 스킬·문서 정리 | 전체 |

### pmt-server

| ID | 기능 | 해결하는 목표 |
|---|---|---|
| S-01 | 설치 기준·버전 확인 | 1 |
| S-02 | Host 설정 파일 `host-config.json` | 1 |
| S-03 | `init` | 1 |
| S-04 | 비밀 관리 (claim key) | 1, 보안 |
| S-05 | TLS 준비 | 1, 2 |
| S-06 | `doctor` | 2 |
| S-07 | `serve` | 1 |
| S-08 | `plan` / `apply` | 1, 4 |
| S-09 | 자동 시작 (작업 스케줄러 / systemd) | 4 |
| S-10 | 방화벽 | 4 |
| S-11 | 프로젝트·저장소 등록 | 3 |
| S-12 | 기기 발급·회전·폐기·권한 | 3 |
| S-13 | 인계 파일 생성 | 3 |
| S-14 | `status` | 2 |
| S-15 | 백업·복원 확인·가져오기 | 4 |
| S-16 | 로그 | 4 |
| S-17 | 업그레이드 | 4 |
| S-18 | pmt-server 플러그인·스킬 | 전체 |

### 공통

| ID | 기능 |
|---|---|
| X-01 | 마켓플레이스 2개 항목·버전 정책 |
| X-02 | 인계 형식 `pmt-handoff/v1` |
| X-03 | 빌드 스크립트 server 대상 |
| X-04 | 시험 분리·포장 시험 갱신 |

## 6. 환경별 설정

- [Windows 설정](phase5/setup-windows.md): Windows Host 구축, Windows 개발 PC 연결.
- [Linux 설정](phase5/setup-linux.md): Linux 개발 서버(Rocky Linux 8.9 실측 환경) 연결, Linux Host 구축(선택).

## 7. 진행 순서

| 단계 | 내용 | 선행 |
|---|---|---|
| A | 커밋되지 않은 마켓플레이스 재구성 정리, plugin.json 단일화(C-02 일부), X-01 | 없음 |
| B | pmt local 경로: C-01, C-05, C-06, C-07, C-08, C-10, C-11 | A |
| C | pmt-server 기초: S-01~S-07, S-14, S-16 | A |
| D | 인계: X-02, S-11~S-13, C-03, C-04 | C |
| E | 운영: S-08~S-10, S-15, S-17, S-18, C-09, C-12, X-03, X-04 | D |
| F | 실측: Windows Host + Linux 개발 서버 + Windows 개발 PC | E |

B와 C는 독립이라 병렬로 진행한다. 단계별 완료 기준과 시험은 [5단계 작업 안내](phase5/README.md)를 따른다.

## 8. 확정이 필요한 결정

설계는 아래 제안값을 전제로 썼다. 다르게 정하면 해당 명세를 고친다.

| # | 결정 | 제안값 | 영향 명세 |
|---|---|---|---|
| D1 | 클라이언트 플러그인 id | `pmt-lifecycle` 유지, 표시 이름만 PMT | X-01 |
| D2 | 모드 판정 | `handoff_file` 또는 `host_url`이 있으면 hosted, 없으면 local | C-01, C-02 |
| D3 | claim key 보관 | `env`(기존) + `file`(Linux 0600, systemd credential) + `dpapi`(Windows) 허용. 런처가 프로세스 내부 환경변수로 주입하므로 Host 계약 불변 | S-04 |
| D4 | 클라이언트 credential | Claude는 plugin secret → SessionStart에서 OS 저장소로 복사, `CLAUDE_ENV_FILE`에는 넣지 않음 | C-04 |
| D5 | 내부 CA 발급 기능 | 기본은 기존 인증서 등록. `tls create-ca`는 `cryptography`를 `host` extra에 추가할 때만 제공 | S-05 |
| D6 | local 짧은 명령 범위 | B 단계에 `done`까지 포함 | C-07 |
| D7 | Codex 설정 | `pmt connect`(C-03) + 같은 Hook 진입(C-10)으로 B·D 단계에 포함 | C-03, C-10 |
| D8 | Windows 클라이언트 기본 경로 | `%APPDATA%\pmt`, `%LOCALAPPDATA%\pmt\data`. 기존 `~/.config/pmt`가 있으면 그 경로 유지 | C-05 |
