# Windows 설정

[개요](../05-plugin-split.md) · [Linux 설정](setup-linux.md) · [통신·인계 기준](communication.md)

1장은 Windows Host 서버(pmt-server), 2장은 Windows 개발 PC(pmt)다. 각 단계는 **pmt-server/pmt 명령**과 그 명령이 내부에서 하는 **수동 동등 작업**을 함께 적는다. 수동 동등 작업은 점검과 장애 대응용이다. 값의 예(`222.234.220.199`, `10.8.0.0/24`)는 실제 값으로 바꾼다.

## 0. 기준값

| 항목 | 기본값 |
|---|---|
| Python | 3.13 이상, **모든 사용자용 설치**(`C:\Program Files\Python313\python.exe`). 사용자별 설치(`%LOCALAPPDATA%\Programs\Python`)는 서비스 계정이 읽지 못한다 |
| 코드 | `C:\PMT\app\<version>\venv` |
| Host 설정 | `C:\ProgramData\PMT\host-config` (`host-config.json`, Host config root) |
| Host 데이터 | `C:\ProgramData\PMT\host-data` |
| 비밀 | `C:\ProgramData\PMT\secrets` |
| TLS | `C:\ProgramData\PMT\tls` |
| 로그 | `C:\ProgramData\PMT\logs` |
| 백업 | 다른 디스크 권장, 예 `D:\PMT-backup` |
| 구동 계정 | `NT AUTHORITY\LOCAL SERVICE`(비밀번호 없음, 최소 권한). 기존 관리 정책이 있으면 전용 로컬 계정 `pmt-host` |
| 포트 | 8765/TCP |

데이터·설정은 Git 작업 폴더, OneDrive 등 동기화 폴더, 네트워크 드라이브에 두지 않는다.

---

## 1. Windows Host 서버

### 1.1 Python과 Host 본체 설치

관리자 PowerShell:

```powershell
py -3.13 --version
New-Item -ItemType Directory -Force C:\PMT\app\0.5.0 | Out-Null
py -3.13 -m venv C:\PMT\app\0.5.0\venv
C:\PMT\app\0.5.0\venv\Scripts\python.exe -m pip install --upgrade pip
C:\PMT\app\0.5.0\venv\Scripts\python.exe -m pip install "proj-mgmt-tool[host] @ git+https://github.com/wnwjdals7498/j-ai-plugin@v0.5.0#subdirectory=proj-mgmt-tool"
C:\PMT\app\0.5.0\venv\Scripts\pmt-server.exe version
```

`version` 결과가 Core 0.4.x / DB 5 / graph 1 / protocol 1이고 FastAPI·Uvicorn·Pydantic이 설치됨으로 나와야 한다. Git이 없으면 release ZIP을 내려받아 `pip install "<압축 해제 경로>\proj-mgmt-tool[host]"`로 설치한다.

### 1.2 pmt-server 플러그인 설치 (Claude Code로 관리할 때)

```powershell
claude plugin marketplace add wnwjdals7498/j-ai-plugin
claude plugin install pmt-server@j-ai-plugins
```

`/plugin` → pmt-server 설정:
- `server_python` = `C:\PMT\app\0.5.0\venv\Scripts\python.exe`
- `host_config_root` = `C:\ProgramData\PMT\host-config`

새 세션에서 `pmt-server version`이 실행되면 준비가 끝난 것이다. 플러그인 없이 PowerShell에서 `C:\PMT\app\0.5.0\venv\Scripts\pmt-server.exe`를 직접 써도 결과는 같다.

### 1.3 TLS 인증서

선택 1: 사내 CA나 기존 인증서가 있으면 그대로 등록한다. SAN에 클라이언트가 접속할 IP(iPAddress SAN) 또는 DNS 이름이 있어야 한다.

선택 2: 내부 CA를 직접 만든다(Git for Windows에 포함된 openssl 사용 예, 한 번만).

```powershell
$o = "C:\Program Files\Git\usr\bin\openssl.exe"
$w = "$env:TEMP\pmt-ca"; New-Item -ItemType Directory -Force $w | Out-Null
& $o req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes -keyout "$w\ca.key" -out "$w\ca.crt" -subj "/CN=PMT Internal CA" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
& $o req -newkey rsa:3072 -sha256 -nodes -keyout "$w\host.key" -out "$w\host.csr" -subj "/CN=pmt-host"
Set-Content "$w\ext.cnf" "subjectAltName=IP:222.234.220.199`nextendedKeyUsage=serverAuth`nkeyUsage=critical,digitalSignature,keyEncipherment" -Encoding ascii
& $o x509 -req -in "$w\host.csr" -CA "$w\ca.crt" -CAkey "$w\ca.key" -CAcreateserial -days 397 -sha256 -extfile "$w\ext.cnf" -out "$w\host.crt"
```

`ca.key`는 Host에 두지 말고 오프라인 보관 매체로 옮긴다. 결정 D5가 채택되면 `pmt-server tls create-ca`/`tls issue`로 대신한다.

### 1.4 init

```powershell
pmt-server --config-root C:\ProgramData\PMT\host-config init `
  --public-url https://222.234.220.199:8765 --listen 0.0.0.0:8765 `
  --data-root C:\ProgramData\PMT\host-data --log-dir C:\ProgramData\PMT\logs --backup-dir D:\PMT-backup `
  --service windows-task --account "NT AUTHORITY\LOCAL SERVICE" `
  --allow 10.8.0.0/24 --allow 192.168.0.0/24 `
  --tls-cert $w\host.crt --tls-key $w\host.key --tls-ca $w\ca.crt
# 출력 계획 확인 후
pmt-server init ... --apply
```

`init --apply`가 하는 일과 수동 동등 작업:

| 작업 | 수동 동등 |
|---|---|
| 폴더 생성 | `New-Item -ItemType Directory -Force C:\ProgramData\PMT\{host-config,host-data,secrets,tls,logs}` |
| 권한 | `icacls C:\ProgramData\PMT /inheritance:r /grant:r "*S-1-5-32-544:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-19:(OI)(CI)M"` 후 `secrets`, `tls`는 `*S-1-5-19:(OI)(CI)R`. 한국어 Windows는 계정 이름이 현지화되므로 SID(Administrators, SYSTEM, LOCAL SERVICE)로 지정한다 |
| TLS 복사 | `host.crt`, `host.key`, `ca.crt` → `C:\ProgramData\PMT\tls\` |
| claim key | 48바이트 난수 → LocalMachine DPAPI 보호 → `secrets\claim-primary.dpapi` |
| 설정 | `host-config.json` 게시(S-02) |
| DB | Host DB 초기화, namespace_id 생성 |

복사 후 `$env:TEMP\pmt-ca`의 `host.key`와 `ca.key`는 지운다(`ca.key`는 오프라인 보관본만 남긴다).

### 1.5 점검과 수동 실행

```powershell
pmt-server doctor
pmt-server serve      # 콘솔에서 수동 실행. 다른 창에서 아래 확인 후 Ctrl+C
curl.exe --cacert C:\ProgramData\PMT\tls\ca.crt https://222.234.220.199:8765/health
```

`doctor`의 fail이 0이어야 다음으로 간다. health는 인증 없는 확인이고, 인증 compatibility는 1.7의 기기로 확인한다.

### 1.6 프로젝트 등록

`serve`가 실행 중인 상태에서:

```powershell
pmt-server project add --name j-messenger --title "J Messenger" --apply
pmt-server project repo add --project j-messenger --name j-messenger --remote https://github.com/wnwjdals7498/j-messenger --apply
pmt-server project list
```

임시 bootstrap 기기가 폐기됐는지 `pmt-server device list`에서 `pmt-server-bootstrap` state=revoked로 확인한다.

### 1.7 개발 기기 발급과 인계

개발 기기마다 따로 발급한다(같은 credential을 여러 기기에 쓰지 않는다).

```powershell
New-Item -ItemType Directory -Force C:\ProgramData\PMT\handoff | Out-Null
pmt-server device issue --actor yss-claude --project j-messenger `
  --handoff-out C:\ProgramData\PMT\handoff\yss-claude.handoff.json `
  --credential-out C:\ProgramData\PMT\handoff\yss-claude.credential --apply
```

- `yss-claude.handoff.json`: 비밀 없음. 개발 기기로 복사해도 된다.
- `yss-claude.credential`: 비밀. 사내 비밀 공유 수단이나 직접 입력으로 전달하고, 전달 후 `Remove-Item`으로 지운다.

### 1.8 방화벽·자동 시작

```powershell
pmt-server plan --only firewall,service
pmt-server apply --only firewall,service --apply
```

수동 동등:

```powershell
New-NetFirewallRule -DisplayName "PMT Host 8765" -Direction Inbound -Protocol TCP -LocalPort 8765 `
  -RemoteAddress 10.8.0.0/24,192.168.0.0/24 -Action Allow -Profile Domain,Private

$py = "C:\PMT\app\0.5.0\venv\Scripts\python.exe"
$action = New-ScheduledTaskAction -Execute $py -Argument "-m pmt.server_admin --config-root C:\ProgramData\PMT\host-config serve" -WorkingDirectory "C:\PMT\app\0.5.0"
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "NT AUTHORITY\LOCALSERVICE" -LogonType ServiceAccount -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "PMT Host" -Action $action -Trigger $trigger -Principal $principal -Settings $settings
Start-ScheduledTask -TaskName "PMT Host"
```

자동 백업은 `pmt-server service install --with-backup-timer --apply`로 매일 03:00 `pmt-server backup --apply` 작업(`PMT Host Backup`)을 추가한다.

### 1.9 운영 확인 (완료 조건)

1. 개발 기기에서 `pmt check` 통과(2장).
2. `Restart-Computer` 후 `pmt-server status`: 같은 namespace, 서비스 실행 중, 개발 기기 `pmt check` 재통과.
3. 작업 관리자에서 Host python 프로세스 강제 종료 → 1분 안에 재시작.
4. `pmt-server backup --apply` → `pmt-server restore-check --bundle <생성 경로>` 통과.
5. 허용 대역 밖 PC에서 접속 실패.

### 1.10 일상 운영 명령

| 목적 | 명령 |
|---|---|
| 상태 | `pmt-server status` |
| 로그 | `pmt-server logs --tail 200` |
| 재시작 | `pmt-server service restart` |
| 기기 추가·회수 | `pmt-server device issue … --apply`, `device revoke --device <id> --apply` |
| credential 유출 의심 | `pmt-server device rotate --device <id> --credential-out … --apply` → 새 값 전달 |
| claim key 회전 | `pmt-server secret rotate-claim-key --new-key-id k2027a --apply` → `service restart` |
| 인증서 갱신 | 새 cert/key로 `pmt-server tls register … --apply` → `service restart` |
| 업그레이드 | `pmt-server upgrade --version 0.5.1 --apply` |

---

## 2. Windows 개발 PC (pmt)

### 2.1 준비

- Python 3.13 이상(사용자별 설치도 됨), Git for Windows(Claude Code의 Bash 도구가 Git Bash를 사용).
- Claude Code CLI 또는 Desktop, 필요하면 Codex.

### 2.2 설치

```powershell
claude plugin marketplace add wnwjdals7498/j-ai-plugin
claude plugin install pmt-lifecycle@j-ai-plugins
```

### 2.3 local로 시작

`/plugin` → PMT → `python_path` = `C:\Users\<사용자>\AppData\Local\Programs\Python\Python313\python.exe`(실제 경로는 `py -3.13 -c "import sys;print(sys.executable)"`로 확인). 새 세션을 열면:

- `%APPDATA%\pmt\storage.json`(mode local)과 `%LOCALAPPDATA%\pmt\data\pmt.sqlite3`가 생긴다.
- 저장소 폴더에서 `pmt link --new "프로젝트 이름"` → 다음 세션부터 overview 표시.

### 2.4 Host에 연결

1. 관리자에게 받은 `*.handoff.json`을 `%APPDATA%\pmt\handoff\`에 둔다.
2. `/plugin` → PMT → `handoff_file` = 그 파일 경로, `device_credential` = 받은 credential.
3. 새 세션 → SessionStart가 probe 후 hosted 프로필 게시, credential을 `%APPDATA%\pmt\secrets\host-credential.dpapi`(DPAPI CurrentUser)에 저장.
4. 저장소 폴더에서 `pmt link j-messenger` → `pmt check`.

이미 local로 쓰던 PC라면 3단계에서 "local 프로필이 있음" 경고가 나온다. 기존 기록을 Host로 옮길지 결정한 뒤 `pmt storage switch --to hosted --handoff <file> [--export <bundle.zip>]`을 실행한다. 번들 import는 Host 관리자가 `pmt-server import`로 빈 namespace에 한다.

### 2.5 Codex (같은 PC)

```powershell
codex plugin marketplace add wnwjdals7498/j-ai-plugin
codex plugin add pmt-lifecycle@j-ai-plugins
py -3.13 "<플러그인 경로>\scripts\pmt_easy.py" connect --handoff "$env:APPDATA\pmt\handoff\yss-codex.handoff.json" --credential-file <전달받은 파일>
[Environment]::SetEnvironmentVariable("PMT_PYTHON", (py -3.13 -c "import sys;print(sys.executable)"), "User")
```

Codex를 다시 시작하고 `/hooks`에서 PMT 명령을 검토·신뢰한다. Codex Hook은 `py -3`로 시작하므로 `py -3`가 3.13 이상을 가리키는지 `py -3 --version`으로 확인한다. Claude와 Codex가 같은 PC면 같은 ConfigRoot·DataRoot·credential 저장소를 공유하되, Host에는 제품별로 다른 기기(actor)를 발급받는 것을 권장한다.

### 2.6 확인

| 확인 | 명령 |
|---|---|
| 모드·경로 | `pmt mode` |
| 연결 전체 | `pmt check` |
| PowerShell에서 | `pmt.cmd check` (C-11) |
| 비밀 노출 없음 | 세션 환경 파일·`storage.json`에 credential 문자열이 없음 |
