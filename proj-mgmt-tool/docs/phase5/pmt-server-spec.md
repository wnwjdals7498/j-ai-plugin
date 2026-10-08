# pmt-server 기능 명세 (S-01 ~ S-18)

[개요](../05-plugin-split.md) · [통신·인계 기준](communication.md) · [pmt 명세](pmt-spec.md)

공통: 명령 이름은 `pmt-server [--config-root <HostConfigRoot>] <command>`. `--config-root` 생략 시 `PMT_HOST_CONFIG_ROOT` → OS 기본값(Windows `C:\ProgramData\PMT\host-config`, Linux `/etc/pmt-host`). stdout은 사람이 읽는 표, `--json`이면 계약 JSON 한 줄. 시스템을 바꾸는 명령은 기본 dry-run이며 `--apply`가 있어야 실행한다. 비밀값은 stdout에 쓰지 않는다(예외: `device issue`/`rotate`의 credential 1회 출력, `--credential-out` 지정 시 파일로만).

---

## S-01 설치 기준·버전 확인

**목적**: Host는 항상 고정된 release를 전용 venv에 설치하고, 코드와 데이터를 분리한다.

**설치 기준**
- 코드: `<AppRoot>/<version>/venv` (Windows `C:\PMT\app\0.5.0\venv`, Linux `/opt/pmt/0.5.0/venv`). 버전마다 새 venv. 이전 버전 폴더는 지우지 않는다(롤백용).
- 설치: `python -m pip install "proj-mgmt-tool[host] @ git+https://github.com/wnwjdals7498/j-ai-plugin@v0.5.0#subdirectory=proj-mgmt-tool"`.
- 데이터·설정·로그·백업은 AppRoot 밖(S-02).

**명령**: `pmt-server version` → package version, Core, DB schema, graph schema, protocol, Host schema, Python 경로·버전, FastAPI/Uvicorn/Pydantic 설치 여부.

**완료 기준**: T-S01-1 새 venv 설치 후 `version`이 0.5.0/5/1/1/1, T-S01-2 extras 없는 venv에서 `host_dependency_missing`.

---

## S-02 Host 설정 파일 `host-config.json`

**목적**: Host 실행·운영에 필요한 모든 비밀 아닌 값을 한 파일에 둔다.

**위치**: `<HostConfigRoot>/host-config.json`. 같은 폴더에 기존 Host `Database`의 config root 내용이 함께 있다.

**스키마 v1**
```json
{
  "schema_version": 1,
  "revision": 3,
  "paths": {
    "data_root": "C:\\ProgramData\\PMT\\host-data",
    "log_dir": "C:\\ProgramData\\PMT\\logs",
    "backup_dir": "D:\\PMT-backup"
  },
  "listen": {"host": "0.0.0.0", "port": 8765},
  "public_url": "https://222.234.220.199:8765",
  "tls": {
    "cert_file": "C:\\ProgramData\\PMT\\tls\\host.crt",
    "key_file": "C:\\ProgramData\\PMT\\tls\\host.key",
    "ca_file": "C:\\ProgramData\\PMT\\tls\\ca.crt"
  },
  "claim_key": {
    "key_id": "primary",
    "source": {"kind": "dpapi", "path": "C:\\ProgramData\\PMT\\secrets\\claim-primary.dpapi"},
    "retained": [{"key_id": "k2026a", "source": {"kind": "dpapi", "path": "...\\claim-k2026a.dpapi"}}]
  },
  "proxy": {"enabled": false, "trusted": []},
  "access": {"allowed_sources": ["10.8.0.0/24", "192.168.0.0/24"]},
  "service": {
    "kind": "windows-task",
    "name": "PMT Host",
    "account": "pmt-host",
    "app_root": "C:\\PMT\\app\\0.5.0"
  },
  "logging": {"level": "info", "retain_days": 30},
  "registry": {"projects": []}
}
```

| 필드 | 규칙 |
|---|---|
| `listen.host` | literal IP(기존 `_serve` 규칙). `0.0.0.0`/특정 NIC IP/`127.0.0.1` |
| `public_url` | 클라이언트가 접속할 주소. https 필수(loopback 시험 제외). 인증서 SAN과 일치해야 함(S-06) |
| `tls` | `cert_file`·`key_file`은 함께 있거나 함께 없어야 함. 없으면 `proxy.enabled` 또는 loopback 시험 모드만 허용 |
| `tls.ca_file` | 클라이언트에 배포할 **공개** Root CA. 공개 CA 인증서면 생략 |
| `claim_key.source.kind` | `env`(값: 환경변수 이름) / `file`(0600 파일, systemd `${CREDENTIALS_DIRECTORY}` 허용) / `dpapi`(Windows LocalMachine DPAPI + ACL) |
| `access.allowed_sources` | 방화벽 규칙의 원본(S-10). 빈 배열이면 방화벽 apply 거부 |
| `service.kind` | `windows-task` / `systemd` / `none` |
| `registry.projects` | S-11이 관리. `{name, project_id, repositories:[{name, repository_id, remote, graph_path}]}` |
| 경로 값의 변수 | 경로 필드(`tls.*`, `claim_key.source.path`)는 `${CREDENTIALS_DIRECTORY}`만 확장한다(systemd `LoadCredential`). 다른 변수·`~`는 거부 |
| Host DB 프로필 | 기존 `Database`가 HostConfigRoot에 `profile.json`을 만든다. `init --apply`가 생성하고 이후 `serve`는 읽기만 하므로 HostConfigRoot는 구동 계정에 읽기 권한만 준다. `doctor`가 존재·읽기를 확인한다 |

**갱신 규칙**: `revision` + 파일 sha256 CAS. 쓰기는 임시 파일 → `os.replace`. 모르는 필드는 거부한다.

**명령**
```
pmt-server config show [--json]
pmt-server config set <dotted.key> <value> --expected-sha256 <hash>
pmt-server config validate
```

**완료 기준**: T-S02-1 스키마 검증(필수·형식·모르는 필드), T-S02-2 동시 `set` 두 개 중 하나만 성공, T-S02-3 비밀값 형태 문자열(예: base64 32바이트 이상)이 들어오면 `config_secret_rejected`.

---

## S-03 `init`

**목적**: 빈 서버에서 설정 파일·폴더·claim key를 한 번에 준비한다.

```
pmt-server init --public-url https://<IP>:8765 [--listen 0.0.0.0:8765]
               [--data-root ...] [--log-dir ...] [--backup-dir ...]
               [--service windows-task|systemd|none] [--account pmt-host]
               [--allow <CIDR> ...] [--tls-cert <crt> --tls-key <key> [--tls-ca <ca>]]
               [--interactive] [--apply]
```

**동작**
1. 이미 `host-config.json`이 있으면 거부(`config_exists`). 덮어쓰기 없음.
2. 입력값으로 설정 초안을 만들고 `config validate`.
3. `--apply`: 폴더 생성·ACL(S-08의 `dirs` 컴포넌트), claim key 생성·보관(S-04), 설정 파일 게시, Host DB 초기화(`AuthRegistry` 생성 → namespace_id 생성).
4. 결과: 설정 경로, namespace_id, 다음 할 일(`doctor` → `serve` 수동 확인 → `project add`).

**기존 Host 인수 (`init --adopt`)**: 이미 `pmt-host serve`로 운영 중인 Host(0.4.x)를 pmt-server 관리로 옮긴다.

```
pmt-server init --adopt --data-root <기존 --data-root> --config-root <기존 --config-root>
               --public-url ... --listen <기존 bind:port> --claim-key-env <기존 환경변수 이름>
               [--claim-key-id primary] [--retained KEY_ID=ENV ...]
               --tls-cert <기존 cert> --tls-key <기존 key> [--tls-ca <공개 CA>] [--service ...] [--allow ...] [--apply]
```

- 기존 DB·`profile.json`·namespace_id·기기·claim key를 **새로 만들지 않는다**. DB를 열어 namespace_id·Host schema를 읽기만 하고 config에 기록한다.
- claim key source는 기존 값 그대로 `env`로 기록한다. `file`/`dpapi`로 옮기는 것은 S-04 `secret migrate-claim-key --to dpapi|file`(같은 key_id·같은 값, 원본 환경변수는 확인 후 사용자가 제거)로 따로 한다.
- 기존 자동 시작 등록(작업·서비스)은 건드리지 않고 `plan`에 "교체 대상"으로만 보고한다. 교체는 S-09 `service install`을 사용자가 승인했을 때 한다.
- 경로가 Git/동기화 폴더 안이면 경고하고, 이전은 S-15 backup → 새 경로 restore-check 통과 후 사용자 결정으로 한다.

**완료 기준**: T-S03-1 빈 경로 init → doctor의 경로·키 항목 ok, T-S03-2 두 번째 init 거부, T-S03-3 dry-run은 파일을 만들지 않음, T-S03-4 운영 중인 Host 사본(backup→restore한 별도 경로)에 `--adopt` → namespace_id·기기 목록·DB 파일 hash 불변, `serve` 후 기존 기기 credential로 compat 성공.

---

## S-04 비밀 관리 (claim key)

**목적**: claim key를 재시작 후에도 같은 값으로, 구동 계정만 읽게 보관한다.

**현재**: `pmt-host serve --claim-key-env NAME`만 지원. 키는 base64 32바이트 이상(`generate-claim-key`는 48바이트).

**변경**
- 런처(`serve`)가 `claim_key.source`에서 값을 읽어 **프로세스 내부** 환경변수(`PMT_HOST_CLAIM_KEY__<key_id>`)에 넣고 기존 `_key_reference` 검사를 그대로 통과시킨다. Host 코드·계약 변경 없음.
- source별 보관
  - `env`: 기존 방식. 운영 문서에서는 비권장(머신 환경변수는 다른 프로세스에 노출될 수 있음).
  - `file`: 소유자 = 구동 계정, 0600, 상위 디렉터리 0700. 그룹·기타 권한이 있으면 `host_key_insecure`로 실행 거부.
  - `dpapi`: `CryptProtectData(CRYPTPROTECT_LOCAL_MACHINE)` + 파일 ACL(Administrators, SYSTEM, 구동 계정만). ACL에 다른 주체가 있으면 실행 거부.

**명령**
```
pmt-server secret init-claim-key [--key-id primary] --apply
pmt-server secret rotate-claim-key --new-key-id k2026b --apply   # 새 키를 primary로, 이전 키는 retained로 이동
pmt-server secret retire-claim-key --key-id k2026a --apply       # 해당 key_id 활성 lease가 있으면 거부
pmt-server secret migrate-claim-key --key-id primary --to dpapi|file --apply   # 같은 key_id·같은 값을 다른 source로 이전(adopt 후)
pmt-server secret check
```

**완료 기준**: T-S04-1 재시작 후 이전 claim의 release/finish 성공, T-S04-2 rotate 후 이전 lease 처리 가능·새 lease는 새 key_id, T-S04-3 활성 lease가 있는 키 retire 거부, T-S04-4 권한이 넓은 키 파일 거부.

---

## S-05 TLS 준비

**목적**: 직접 TLS로 운영하고 클라이언트가 검증할 수 있는 CA를 배포한다.

**기본 기능 (의존성 추가 없음)**
```
pmt-server tls register --cert <crt> --key <key> [--ca <ca>] --apply   # 복사·ACL·설정 반영
pmt-server tls check                                                   # S-06의 TLS 항목만
```
검사: cert/key 쌍 일치(`ssl.SSLContext.load_cert_chain`), 만료일(30일 이내 경고), SAN에 `public_url`의 host(IP는 iPAddress SAN) 포함, chain이 `ca_file`로 검증됨.

**선택 기능 (결정 D5, `cryptography`를 `host` extra에 추가할 때)**
```
pmt-server tls create-ca --name "PMT Internal CA" --apply
pmt-server tls issue --san 222.234.220.199 --san pmt.local --days 397 --apply
```
CA 개인키는 Host 밖(오프라인 보관)으로 옮기도록 안내한다.

**완료 기준**: T-S05-1 SAN 불일치 감지, T-S05-2 키 쌍 불일치 감지, T-S05-3 클라이언트 `HttpStore`가 `ca_file`로 접속 성공.

---

## S-06 `doctor`

**목적**: 실행 전후 문제를 한 번에 찾는다. 읽기 전용.

| 항목 | 검사 | 실패 code |
|---|---|---|
| python | 3.13 이상, venv 경로 = `service.app_root` | `python_unsupported` |
| deps | fastapi·pydantic·uvicorn import | `host_dependency_missing` |
| version | Core/DB/graph/protocol 값, 기존 DB schema와 일치 | `host_schema_unsupported` |
| config | S-02 검증 | `config_invalid` |
| paths | 존재·쓰기(구동 계정 기준 ACL/모드), Git·동기화 폴더·네트워크 드라이브 아님 | `path_unsafe` |
| claim_key | source 읽기·길이·권한, retained 포함 | `host_key_unavailable`, `host_key_insecure` |
| tls | S-05 검사 | `host_tls_invalid` |
| port | listen 포트 점유 여부(서비스 중지 상태일 때만 실패 처리) | `port_in_use` |
| firewall | 규칙 존재·허용 대역 일치(S-10) | `firewall_mismatch` |
| service | 자동 시작 등록·설정 일치(S-09) | `service_mismatch` |
| runtime | 실행 중이면 `/health`·인증 compatibility(관리 진단 기기 사용) | `host_unreachable` |
| duplicates | 같은 data_root를 쓰는 Host 프로세스 1개 이하 | `host_duplicate` |
| selinux (Linux) | `getenforce`, 최근 AVC 거부 기록 유무 | 경고만 |

출력: 항목별 ok/warn/fail + 해결 안내 한 줄. 종료 코드: fail이 있으면 1.

**완료 기준**: 항목별 실패 주입 시험(T-S06-*).

---

## S-07 `serve`

```
pmt-server serve [--config-root ...] [--listen <ip:port>] [--allow-loopback-http]
```
- `host-config.json`을 읽어 기존 `pmt.host.cli._serve`와 같은 인자로 실행한다(Uvicorn workers=1, access_log off).
- 시작 전 `doctor`의 config·claim_key·tls·duplicates 항목을 실행하고 실패면 시작하지 않는다.
- 로그 설정은 S-16.
- `pmt-host serve`(기존)는 호환용으로 유지한다.

**완료 기준**: T-S07-1 기존 `pmt-host serve` 인자와 같은 동작(health/compat 동일), T-S07-2 중복 실행 거부.

---

## S-08 `plan` / `apply`

**목적**: 설정 파일을 기준으로 서버 상태를 맞춘다(“설정화”의 중심).

```
pmt-server plan  [--only dirs,acl,tls,claim_key,firewall,service]
pmt-server apply [--only ...] --apply
```

| 컴포넌트 | 원하는 상태 | Windows 실행 | Linux 실행 |
|---|---|---|---|
| dirs | data/log/backup/secrets/tls 폴더 존재 | `New-Item` | `install -d -m 0700 -o pmt` |
| acl | 구동 계정+관리자만 접근 | `icacls /inheritance:r /grant:r` | `chown`, `chmod` |
| tls | 설정 경로에 cert/key 존재·검사 통과 | 확인만(생성 안 함) | 확인만 |
| claim_key | primary 존재 | S-04 | S-04 |
| firewall | S-10 | `New-NetFirewallRule` | `firewall-cmd`/`ufw` |
| service | S-09 | `Register-ScheduledTask` | systemd unit 설치 |

- `plan`은 현재 상태와 차이, 실행할 명령을 그대로 출력한다(관리자가 직접 실행해도 같은 결과).
- `apply`는 컴포넌트별로 실행하고 각 결과를 기록한다. 실패하면 거기서 멈춘다. 이미 맞는 컴포넌트는 건너뛴다(멱등).
- 관리자 권한이 필요한 컴포넌트는 권한이 없으면 `admin_required`.

**완료 기준**: T-S08-1 두 번 apply 시 두 번째는 변경 0, T-S08-2 plan 출력 명령을 수동 실행한 결과와 apply 결과가 같음.

---

## S-09 자동 시작

**Windows (`service.kind = windows-task`)**
- 작업 이름 `PMT Host`, 트리거 시스템 시작, 실행 계정 `service.account`(로그온 여부와 관계없이 실행), 권한 수준 Limited.
- 동작: `<app_root>\venv\Scripts\python.exe -m pmt.server_admin --config-root <HostConfigRoot> serve`, 작업 폴더 `<app_root>`.
- 설정: `MultipleInstances=IgnoreNew`, `RestartCount=3`, `RestartInterval=1분`, `ExecutionTimeLimit=0`(무제한), `StartWhenAvailable`, 배터리 조건 해제.
- `serve`가 Uvicorn을 같은 프로세스에서 실행하므로 작업 종료 = Host 종료. 별도 wrapper 없음.

**Linux (`service.kind = systemd`)**: `/etc/systemd/system/pmt-host.service` 생성. 내용은 [Linux 설정](setup-linux.md#3-linux-host-선택)의 unit과 같다. `LoadCredential`로 claim key 전달.

**명령**
```
pmt-server service install --apply | remove --apply | status | start | stop | restart
```

**완료 기준**: T-S09-1 재부팅 후 같은 namespace·claim key로 health/compat, T-S09-2 프로세스 강제 종료 후 자동 재시작, T-S09-3 수동 두 번째 시작 시 하나만 실행.

---

## S-10 방화벽

- 원천: `listen.port`, `access.allowed_sources`.
- Windows: 규칙 이름 `PMT Host <port>`, Inbound TCP, `RemoteAddress` = allowed_sources, Profile Domain·Private. Public 프로필 허용은 `--allow-public` 명시 시만.
- Linux: firewalld면 rich rule(대역별), ufw면 `allow from <CIDR> to any port <port> proto tcp`. 둘 다 없으면 plan에 수동 안내만 출력.
- 기존 다른 규칙은 건드리지 않는다. 이름이 같은 규칙만 관리한다.

**완료 기준**: T-S10-1 허용 대역 밖에서 접속 실패, 안에서 성공(실측), T-S10-2 규칙 재적용 멱등.

---

## S-11 프로젝트·저장소 등록

**목적**: Host에 project scope를 만들고 저장소 식별자를 관리해 인계에 넣는다.

**현재**: `issue_device`는 존재하는 scope만 허용한다. project 생성은 `"*"` scope 기기의 `create_scope` operation으로만 가능하고, 이를 위한 bootstrap 절차가 문서로만 있다. 현재 Host workspace 계약은 실제 repository scope와 project→repository 관계를 검사한다. 논리 UUID만 만들어 registry에 넣으면 repository_scope_mismatch로 거부된다. 이 차이는 격리된 실제 HTTPS 시험으로 확인했다.

**명령**
```
pmt-server project add --name j-messenger [--title "..."] --apply
pmt-server project repo add --project j-messenger --name j-messenger --remote https://github.com/... [--graph-path docs/pmt-docs/graph.json] --apply
pmt-server project list
```

**동작 (`project add`)**: 실행 중인 Host에 대해
1. 임시 관리 기기 발급(`issue_device(actor="pmt-server-bootstrap", scopes=["*"], permissions=["write"])`, 로컬 DB 관리 경로).
2. 루프백 또는 `public_url`로 HTTPS 세션 등록 → 기존 kind/parent 계약에 맞게 environment → repository → project를 `create_scope(kind, slug, parent_id)`로 생성한다. project는 실제 repository scope를 부모로 가진다. title이 있으면 기존 body metadata에 담는다(정식 operation, 직접 SQL 아님).
3. 성공·실패와 관계없이 임시 기기 `revoke_device`.
4. `registry.projects`에 `{name, project_id}` CAS 추가.

`repo add`는 project 생성 시 마련된 실제 repository scope ID를 확인해 registry의 이름·remote·graph_path와 연결한다(이 단계의 업무 DB 쓰기는 없음). 현재 Core에서 한 project scope는 한 repository에 속한다. 다른 repository를 같은 project에 임의로 연결하지 않고 별도 PMT project를 만들도록 안내한다. Host API/schema/parent 규칙은 변경하지 않는다.

**오류**: `host_unreachable`, `project_exists`(같은 name), `bootstrap_revoke_failed`(이 경우 기기 ID를 출력하고 수동 revoke 안내).

**완료 기준**: T-S11-1 project 생성 후 임시 기기 state=revoked, T-S11-2 revoke 실패 주입 시 경고·ID 출력.

---

## S-12 기기 발급·회전·폐기·권한

```
pmt-server device issue --actor yss-claude --project j-messenger [--project ...] [--permission read,write,runtime,review]
                        [--handoff-out <file>] [--credential-out <file>] --apply
pmt-server device list
pmt-server device rotate --device <id> [--credential-out <file>] --apply
pmt-server device grants --device <id> --project ... --permission ... --apply
pmt-server device revoke --device <id> --apply
```
- 내부: 기존 `AuthRegistry.issue_device/rotate_device/update_grants/revoke_device`. `expected_revision`은 명령이 현재 값을 읽어 채운다.
- 기본 권한: `read,write,runtime,review`. `admin`과 `"*"` scope는 `--allow-admin` 명시 시만.
- `device list`: `host_devices`의 id·actor·scopes·permissions·state·revision·updated_at 읽기 전용 조회(신규 `AuthRegistry.list_devices`). credential hash는 출력하지 않는다.
- credential: `--credential-out`이면 0600/ACL 파일로만 쓰고 stdout에는 "저장함"만. 없으면 stdout에 1회 출력.
- `--handoff-out`이면 S-13 인계 파일을 함께 만든다.

**완료 기준**: T-S12-1 발급 기기로 `pmt check` 성공, T-S12-2 revoke 후 `unauthenticated`, T-S12-3 rotate 후 이전 credential 거부, T-S12-4 권한 밖 project 접근 `scope_forbidden`.

---

## S-13 인계 파일 생성

```
pmt-server handoff create --device <id> --out <file> [--include-ca]
```
- 형식: [X-02 `pmt-handoff/v1`](communication.md#3-인계-형식-pmt-handoffv1-x-02).
- 포함: public_url, 호환 버전, namespace_id, device_id, actor, scopes/permissions, 대상 project·repository registry, (선택) 공개 CA PEM과 sha256.
- 제외: credential, claim key, TLS 개인키, 내부 경로.
- 파일은 서명하지 않는다. 클라이언트는 CA sha256을 관리자와 별도 채널로 대조할 수 있다(`pmt connect`가 sha256을 출력).

**완료 기준**: T-S13-1 생성 파일에 비밀 패턴 없음(시험에서 credential 문자열 검색), T-S13-2 `pmt connect`로 바로 연결.

---

## S-14 `status`

출력: 서비스 상태(실행 여부·PID·시작 시각), `/health`, compatibility(관리 진단 기기 또는 로컬 DB 읽기), namespace_id, 기기 수(active/revoked), 활성 session 수, 활성 claim lease 수, 마지막 백업 시각, 로그 위치. 네트워크 연결 실패 시에도 로컬 정보는 출력.

---

## S-15 백업·복원 확인·가져오기

```
pmt-server backup --apply                  # quiescent manifest + sanitized SQLite + resources → backup_dir/<UTC>/
pmt-server restore-check --bundle <dir|zip> # 빈 임시 경로에 복원 → hash/FK/개수 검증 → 임시 경로 삭제
pmt-server import --bundle <zip> --apply    # 빈 namespace 확인 후 기존 transfer import (클라이언트 local 이관용)
pmt-server backup prune --keep 14 --apply
```
- 내부: 기존 `MigrationCoordinator`·Host transfer 경계. 업무 데이터가 있는 namespace에 import하면 거부(기존 규칙).
- credential·claim key·TLS 키는 백업에 포함하지 않는다. 별도 보관 안내만 출력.
- 자동 백업은 S-09와 같은 방식의 예약 작업(Windows 작업 스케줄러 매일, Linux systemd timer)으로 `service install --with-backup-timer` 옵션 제공.

**완료 기준**: T-S15-1 backup → restore-check 통과, T-S15-2 데이터 있는 namespace import 거부, T-S15-3 백업에 비밀 없음.

---

## S-16 로그

- 위치: `paths.log_dir/host.log`, 일 단위 회전(`TimedRotatingFileHandler`), `logging.retain_days` 보관.
- 내용: 기존 Host 진단 이벤트(JSON 한 줄), Uvicorn 시작/종료. access log는 끔(기존). 헤더·credential·본문 원문 없음.
- `pmt-server logs [--tail 200] [--since 1h]`.

---

## S-17 업그레이드

```
pmt-server upgrade --version 0.5.1 [--app-root C:\PMT\app\0.5.1] --apply
```
절차: 새 venv 설치(S-01) → 새 venv로 `doctor`(DB schema 호환 확인) → `backup` → 서비스 중지 → `service.app_root` 변경(CAS) → 서비스 등록 갱신 → 시작 → health/compat 확인. compat 실패면 이전 app_root로 되돌리고 시작한다. DB schema 변경이 있는 release는 별도 migration 문서가 있을 때만 진행한다.

**완료 기준**: T-S17-1 0.5.0→0.5.1 업그레이드 후 같은 namespace·기기 인증, T-S17-2 compat 실패 주입 시 롤백.

---

## S-18 pmt-server 플러그인·스킬

**plugin.json (Claude)**
```json
{
  "name": "pmt-server",
  "version": "0.5.0",
  "description": "Install, configure and operate the PMT storage Host.",
  "hooks": "./hooks/hooks.json",
  "userConfig": {
    "server_python": {"type": "file", "title": "Host venv Python", "description": "Absolute path of the Python inside the PMT Host venv.", "required": true},
    "host_config_root": {"type": "directory", "title": "Host ConfigRoot", "description": "Folder that holds host-config.json.", "required": true}
  }
}
```
- Hook: SessionStart 하나. `CLAUDE_ENV_FILE`에 `PMT_SERVER_PYTHON`, `PMT_HOST_CONFIG_ROOT`만 쓴다. lifecycle 이벤트 기록 없음, Host 호출 없음.
- `bin/pmt-server`(sh), `bin/pmt-server.cmd`: `"$PMT_SERVER_PYTHON" -m pmt.server_admin --config-root "$PMT_HOST_CONFIG_ROOT" "$@"`.
- Codex: 같은 skill, Hook 없음. `PMT_SERVER_PYTHON`은 사용자가 설정하거나 skill이 venv 경로를 묻는다.
- 스킬 `pmt-server`: 구축 순서(설치 → init → tls → doctor → serve 수동 확인 → project/device → handoff → plan/apply(firewall, service) → 재시작 검증 → backup/restore-check), 비밀 취급 규칙, 실패 시 확인 순서. references: Windows/Linux 절차(본 문서의 setup 문서 요약), 기존 `windows-host.md`의 운영 원칙.

**완료 기준**: T-S18-1 `claude plugin validate`, T-S18-2 Host 서버의 새 세션에서 `pmt-server doctor` 실행, T-S18-3 Codex skill 발견.
