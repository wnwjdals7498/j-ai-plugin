# Linux 설정

[개요](../05-plugin-split.md) · [Windows 설정](setup-windows.md) · [통신·인계 기준](communication.md)

1장은 Linux 개발 서버(pmt, Rocky Linux 8.9·Claude Code 2.1.292에서 0.4.1 hosted 실측), 2장은 Ubuntu 등 다른 배포판의 차이, 3장은 Linux를 Host로 쓰는 경우(선택)다. 값의 예는 실제 값으로 바꾼다.

## 0. 기준값

| 항목 | 개발 서버(pmt) | Linux Host(pmt-server) |
|---|---|---|
| Python | 3.13 이상, 절대 경로(예 `/opt/python3.13/bin/python3.13`) | 같음 |
| 코드 | 플러그인 캐시(제품이 관리) | `/opt/pmt/<version>/venv` |
| 설정 | `${XDG_CONFIG_HOME:-~/.config}/pmt` | `/etc/pmt-host` |
| 데이터 | `${XDG_DATA_HOME:-~/.local/share}/pmt/data` | `/var/lib/pmt-host` |
| 비밀 | `~/.config/pmt/secrets/` (0700/0600) | `/etc/pmt-host/secrets/` (root 0700, systemd `LoadCredential`) |
| 로그 | 제품 Hook 출력 | `/var/log/pmt-host` |
| 구동 계정 | 로그인 사용자 | 시스템 계정 `pmt` |

---

## 1. Linux 개발 서버 (pmt)

### 1.1 Python 3.13

Rocky Linux 8 기본 저장소의 Python은 3.13보다 낮을 수 있다. 다음 중 하나로 준비하고 절대 경로를 기록한다.

```bash
python3.13 --version || ls /opt/python3.13/bin/python3.13
```

- 이미 실측에 쓴 `/opt/python3.13/bin/python3.13`이 있으면 그대로 쓴다.
- 없으면 배포판 패키지(있는 경우), 소스 빌드(`--prefix=/opt/python3.13`), 또는 `uv python install 3.13` 중 조직 정책에 맞는 방법을 쓴다. 어느 쪽이든 Hook이 실행되는 계정이 읽고 실행할 수 있어야 한다.

### 1.2 플러그인 설치

```bash
claude plugin marketplace add wnwjdals7498/j-ai-plugin
claude plugin install pmt-lifecycle@j-ai-plugins
```

0.4.1을 `--plugin-dir`이나 로컬 marketplace로 설치했다면 먼저 그 등록을 제거한다. `~/.config/pmt`, `~/.local/share/pmt`는 지우지 않는다(기존 hosted 프로필·mapping을 그대로 쓴다).

### 1.3 local로 시작

`/plugin` → PMT → `python_path` = `/opt/python3.13/bin/python3.13`. 새 세션:

```bash
pmt mode            # local, ~/.config/pmt, ~/.local/share/pmt/data
cd ~/work/j-messenger && pmt link --new "J Messenger"
pmt check
```

### 1.4 Host에 연결

1. 관리자에게 받은 인계 파일을 둔다.
   ```bash
   install -d -m 700 ~/.config/pmt/handoff
   install -m 600 yss-claude.handoff.json ~/.config/pmt/handoff/
   ```
2. `/plugin` → PMT → `handoff_file` = `/home/<user>/.config/pmt/handoff/yss-claude.handoff.json`, `device_credential` = 받은 값. Claude Code는 sensitive 값을 `~/.claude/.credentials.json`(0600)의 `pluginSecrets`에 저장한다(실측).
3. 새 세션 → SessionStart: probe → hosted 게시 → credential을 `~/.config/pmt/secrets/host-credential`(0600)에 저장 → `CLAUDE_ENV_FILE`에 비밀 없는 값만 기록.
4. `cd ~/work/j-messenger && pmt link j-messenger && pmt check`.

0.4.1에서 이미 hosted로 쓰던 기기는 설정 화면의 개별 값(`host_url` 등)이 그대로 동작한다(C-02 호환). 인계 파일로 바꾸려면 개별 값을 비우고 `handoff_file`만 넣는다. 프로필 내용이 같으면 재게시하지 않는다.

### 1.5 이전 방식 정리

0.4.1 이전 수동 설정을 썼다면 다음을 제거한다. 남겨 두면 `PMT_HOST_CREDENTIAL` 환경변수가 저장소보다 우선한다(C-04).

```bash
grep -n "PMT_" ~/.bashrc ~/.bash_profile 2>/dev/null
grep -rln "PMT_" ~/work/*/.claude/settings.local.json 2>/dev/null
```

해당 줄을 지운 뒤 새 로그인 셸과 새 Claude 세션으로 `pmt check`.

### 1.6 Codex (같은 서버)

```bash
codex plugin marketplace add wnwjdals7498/j-ai-plugin
codex plugin add pmt-lifecycle@j-ai-plugins
/opt/python3.13/bin/python3.13 "<플러그인 경로>/scripts/pmt_easy.py" connect \
  --handoff ~/.config/pmt/handoff/yss-codex.handoff.json --credential-file ./yss-codex.credential
rm ./yss-codex.credential
echo 'export PMT_PYTHON=/opt/python3.13/bin/python3.13' >> ~/.profile
```

Codex Hook은 `python`을 호출하므로 Codex를 시작하는 환경의 `python`이 3.13 이상인지 확인한다(`command -v python && python --version`). 아니면 C-10의 안내 메시지가 SessionStart에 표시되고 기록은 건너뛴다. `/hooks`에서 PMT 명령을 검토·신뢰한다.

### 1.7 원격 접속 형태별 주의

| 형태 | Hook·`pmt`가 실행되는 곳 | 설정할 곳 |
|---|---|---|
| 서버에 SSH 접속해 `claude` 실행 | 서버 | 서버의 `/plugin` |
| Windows Desktop에서 원격(SSH) 세션 | 서버 | 서버 쪽 플러그인 설정. Windows PC 설정은 쓰이지 않음 |
| Remote Control / 클라우드 세션 | 해당 실행 환경 | 실행 환경마다 확인. 미실측 |

---

## 2. 다른 배포판 차이

| 항목 | RHEL/Rocky | Ubuntu/Debian |
|---|---|---|
| 방화벽 | firewalld | ufw (또는 nftables 직접) |
| SELinux | 기본 Enforcing | 해당 없음(AppArmor) |
| Python 3.13 | 소스 빌드/uv 등 | deadsnakes PPA 또는 uv |
| 서비스 계정 생성 | `useradd --system --home-dir /var/lib/pmt-host --shell /sbin/nologin pmt` | `adduser --system --group --home /var/lib/pmt-host pmt` |

---

## 3. Linux Host (선택)

Windows Host 대신 Linux를 쓰는 경우다. 기능·명령은 Windows와 같고 자동 시작·방화벽·비밀 보관 방식만 다르다.

### 3.1 설치

```bash
sudo useradd --system --home-dir /var/lib/pmt-host --shell /sbin/nologin pmt
sudo install -d -m 755 /opt/pmt/0.5.0
sudo /opt/python3.13/bin/python3.13 -m venv /opt/pmt/0.5.0/venv
sudo /opt/pmt/0.5.0/venv/bin/python -m pip install \
  "proj-mgmt-tool[host] @ git+https://github.com/wnwjdals7498/j-ai-plugin@v0.5.0#subdirectory=proj-mgmt-tool"
sudo ln -sfn /opt/pmt/0.5.0/venv/bin/pmt-server /usr/local/bin/pmt-server
pmt-server version
```

### 3.2 TLS (내부 CA 예)

```bash
w=$(mktemp -d)
openssl req -x509 -newkey rsa:3072 -sha256 -days 3650 -nodes -keyout $w/ca.key -out $w/ca.crt \
  -subj "/CN=PMT Internal CA" -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign"
openssl req -newkey rsa:3072 -sha256 -nodes -keyout $w/host.key -out $w/host.csr -subj "/CN=pmt-host"
printf 'subjectAltName=IP:10.8.0.10\nextendedKeyUsage=serverAuth\nkeyUsage=critical,digitalSignature,keyEncipherment\n' > $w/ext.cnf
openssl x509 -req -in $w/host.csr -CA $w/ca.crt -CAkey $w/ca.key -CAcreateserial -days 397 -sha256 -extfile $w/ext.cnf -out $w/host.crt
```

`ca.key`는 서버 밖으로 옮기고 `$w`를 지운다.

### 3.3 init

```bash
sudo pmt-server --config-root /etc/pmt-host init \
  --public-url https://10.8.0.10:8765 --listen 0.0.0.0:8765 \
  --data-root /var/lib/pmt-host --log-dir /var/log/pmt-host --backup-dir /srv/pmt-backup \
  --service systemd --account pmt --allow 10.8.0.0/24 \
  --tls-cert $w/host.crt --tls-key $w/host.key --tls-ca $w/ca.crt --apply
```

수동 동등:

| 작업 | 명령 |
|---|---|
| 폴더 | `install -d -m 700 -o pmt -g pmt /var/lib/pmt-host /var/log/pmt-host /srv/pmt-backup`, `install -d -m 750 -o root -g pmt /etc/pmt-host`, `install -d -m 700 -o root -g root /etc/pmt-host/secrets` |
| TLS | `install -m 644 host.crt ca.crt /etc/pmt-host/tls/`, `install -m 600 -o root host.key /etc/pmt-host/tls/` (systemd `LoadCredential`로 전달하므로 root 소유 가능) |
| claim key | `head -c 48 /dev/urandom | base64 -w0 > /etc/pmt-host/secrets/claim-primary` (root 0600) |
| 설정 | `claim_key.source = {"kind":"file","path":"${CREDENTIALS_DIRECTORY}/claim-primary"}` |

### 3.4 systemd unit

`pmt-server service install --apply`가 만드는 `/etc/systemd/system/pmt-host.service`:

```ini
[Unit]
Description=PMT Host
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pmt
Group=pmt
WorkingDirectory=/opt/pmt/0.5.0
Environment=PMT_HOST_CONFIG_ROOT=/etc/pmt-host
ExecStart=/opt/pmt/0.5.0/venv/bin/python -m pmt.server_admin --config-root /etc/pmt-host serve
LoadCredential=claim-primary:/etc/pmt-host/secrets/claim-primary
LoadCredential=tls-key:/etc/pmt-host/tls/host.key
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=/var/lib/pmt-host /var/log/pmt-host /srv/pmt-backup
UMask=0077

[Install]
WantedBy=multi-user.target
```

TLS 키도 `LoadCredential`로 받으므로 설정의 `tls.key_file`은 `${CREDENTIALS_DIRECTORY}/tls-key`가 된다. 적용:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pmt-host
systemctl status pmt-host
```

자동 백업(`--with-backup-timer`)은 `pmt-host-backup.service` + `pmt-host-backup.timer`(`OnCalendar=*-*-* 03:00:00`, `Persistent=true`)를 만든다.

### 3.5 방화벽

```bash
# firewalld
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" source address="10.8.0.0/24" port port="8765" protocol="tcp" accept'
sudo firewall-cmd --reload
# ufw
sudo ufw allow from 10.8.0.0/24 to any port 8765 proto tcp
```

SELinux Enforcing에서 `doctor`가 AVC 거부를 보고하면 `ausearch -m avc -ts recent`로 원인을 확인한다. 데이터 경로를 `/var/lib`, `/var/log` 아래에 두면 추가 정책 없이 동작하는 경우가 많지만 실측으로 확인한다.

### 3.6 프로젝트·기기·인계

Windows와 같다([Windows 1.6~1.7](setup-windows.md#16-프로젝트-등록)). 인계 파일·credential 파일은 `install -m 600`으로 만들고 전달 후 `shred -u`로 지운다.

### 3.7 운영 확인

`sudo reboot` 후 `pmt-server status`, `sudo systemctl kill -s KILL pmt-host` 후 5초 안에 재시작, `pmt-server backup --apply && pmt-server restore-check --bundle …`, 허용 대역 밖 접속 실패, 개발 기기 `pmt check` 통과.
