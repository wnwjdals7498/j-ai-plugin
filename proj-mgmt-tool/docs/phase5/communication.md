# 통신·인계·기준 (X-01 ~ X-04)

[개요](../05-plugin-split.md) · [pmt 명세](pmt-spec.md) · [pmt-server 명세](pmt-server-spec.md)

## 1. 두 플러그인의 관계

두 플러그인은 서로를 직접 호출하지 않는다. 연결은 세 갈래뿐이다.

```
 [개발 기기]                                   [Host 서버]
 Claude Code / Codex                           Claude Code / Codex (관리자)
   └ pmt 플러그인                                 └ pmt-server 플러그인
       Hook · bin/pmt                                 bin/pmt-server
       └ pmt 본체 (src/, 표준 라이브러리)              └ Host venv의 pmt 본체 + [host] extras
           └ HttpStore ──(1) 운영: HTTPS API v1 ──▶ Uvicorn/FastAPI (pmt-server serve)
                                                      └ Host SQLite + resources
           ◀──(2) 인계: handoff JSON + credential ── pmt-server handoff/device
                (사람이 별도 경로로 전달)
                                                   (3) 관리: pmt-server ──▶ 로컬 Host DB (기기 발급)
                                                                       └─▶ HTTPS 루프백 (project 생성)
```

| 갈래 | 방향 | 수단 | 내용 |
|---|---|---|---|
| (1) 운영 | pmt → Host | HTTPS, Host API v1 | 기록·조회·점유·완료·리소스·재전송 조회 |
| (2) 인계 | pmt-server → 사람 → pmt | 파일 2개(인계 JSON, credential) | 접속 주소·CA·식별자·권한·project/repository |
| (3) 관리 | pmt-server → Host | 로컬 DB(인증 관리만), HTTPS 루프백(업무 operation) | 기기 발급·회전·폐기, project 생성 |

pmt 플러그인은 Host의 DB·파일에 접근하지 않고, pmt-server는 개발 기기의 checkout·Git·모델 실행에 관여하지 않는다(기존 역할 경계).

## 2. 운영 통신 (1) — 현재 계약 그대로

자세한 원문: [Host 연결 계약 v1](../phase3/host-api-contract.md), [4단계 runtime contract](../phase4/runtime-contract.md). 이번 단계에서 바꾸지 않는다.

### 2.1 연결

- 클라이언트: `HttpStore`(표준 라이브러리 `urllib`/`ssl`). `ssl.create_default_context(cafile=<프로필 ca_file>)`로 인증서·hostname을 검증한다. redirect는 따르지 않는다. timeout 기본 10초(0~300초).
- 서버: Uvicorn 단일 worker, 직접 TLS 또는 loopback 뒤 신뢰 proxy.
- 요청·응답 JSON 상한 1MiB, 리소스 업로드 8MiB(metadata 헤더 4KiB), import 번들 64MiB.

### 2.2 인증 헤더

| 헤더 | 값 | 출처 |
|---|---|---|
| `Authorization` | `Bearer <device credential>` | 클라이언트 credential 저장소 → `PMT_HOST_CREDENTIAL` |
| `X-PMT-Device` | device_id | 프로필 |
| `X-PMT-Namespace` | namespace_id | 프로필 |
| `X-PMT-Environment` | environment_id | ConfigRoot가 만든 기기별 UUID(복사 금지) |
| `X-PMT-Session` | session_id | 등록 session을 요구하는 요청 |
| `X-PMT-Request-Fingerprint` | 요청 본문 SHA-256 | 결과 조회 시 |

Host는 credential hash·기기 상태·namespace·session/environment 소속을 매 요청 확인하고, 변경 transaction 안에서 scope·permission을 다시 확인한다. body의 `actor`는 등록 actor와 같아야 한다.

### 2.3 경로

| 경로 | 용도 |
|---|---|
| `GET /health` | 비밀 없는 가용 상태 |
| `GET /api/v1/compatibility` | 인증 후 core/db/graph/protocol·actor·scope·permission |
| `POST /api/v1/sessions` | session 등록 |
| `POST /api/v1/operations` | protocol-v1 envelope 실행 → `{api_version, envelope, exit_code}` |
| `GET /api/v1/requests/{request_id}` | 원 요청 결과 회수 |
| `POST/GET /api/v1/resources[/{id}]` | 리소스 업로드·다운로드 |
| `POST /api/v1/transfers/import`, `/backup`, `GET /download/{ref}` | 관리자 이관 |

### 2.4 호출 순서 (SessionStart, hosted)

```
Hook(easy_hook) ─ C-01 판정 ─ credential 로드(C-04)
  └ HttpStore.check_compatibility()          GET /api/v1/compatibility
  └ HttpStore.register_session(session_id)   POST /api/v1/sessions
  └ record_event / compose_resume_overview   POST /api/v1/operations
  └ systemMessage + additionalContext 출력
```

### 2.5 오류와 재시도

| HTTP | 의미 | 클라이언트 동작 |
|---|---|---|
| 400 | 입력·계약 오류 | 중단, code 표시 |
| 401 | 인증·session 불일치 | 중단, credential/인계 확인 안내 |
| 403 | scope/permission 부족 | 중단, 관리자에게 grants 요청 안내 |
| 409 | revision·소유권·source·동일 request ID 충돌 | 최신 상태 조회 후 중단(자동 덮어쓰기 없음) |
| 503 / 연결 실패 | 일시 불가 | 신규 공유 작업 중단. 이미 생성된 결과만 pending 보존 |

응답 유실은 effect unknown이다. 같은 `request_id`로 결과를 조회하고, 없을 때만 같은 본문으로 재전송한다. 새 ID로 바꾸지 않는다.

### 2.6 보내지 않는 것

credential(헤더 외), claim 내부 토큰, 환경변수 전체, argv, PID, 절대 경로, spool 경로, 전체 대화·지시 원문, provider 설정. 로그에는 헤더 원문을 남기지 않는다.

## 3. 인계 형식 `pmt-handoff/v1` (X-02)

pmt-server가 만들고(S-13) pmt가 읽는다(C-02, C-03). **비밀을 담지 않는다.**

```json
{
  "format": "pmt-handoff",
  "version": 1,
  "issued_at": "2026-10-08T06:00:00Z",
  "issuer": {"tool": "pmt-server", "version": "0.5.0"},
  "host": {
    "url": "https://222.234.220.199:8765",
    "compatibility": {"core": "0.4", "db_schema": 5, "graph_schema": 1, "protocol": [1]},
    "ca_pem": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----\n",
    "ca_sha256": "3f1c...e9"
  },
  "namespace_id": "6b1e...-....",
  "device": {
    "device_id": "0c8a...-....",
    "actor": "yss-claude",
    "permissions": ["read", "review", "runtime", "write"],
    "scopes": ["9d2f...-...."],
    "credential": {"delivery": "separate", "env": "PMT_HOST_CREDENTIAL"}
  },
  "projects": [
    {
      "name": "j-messenger",
      "project_id": "9d2f...-....",
      "repositories": [
        {"name": "j-messenger", "repository_id": "a41b...-....",
         "remote": "https://github.com/wnwjdals7498/j-messenger", "graph_path": "docs/pmt-docs/graph.json"}
      ]
    }
  ]
}
```

| 필드 | 필수 | 규칙 |
|---|---|---|
| `format`, `version` | 예 | 정확히 `pmt-handoff`, `1`. 다르면 `handoff_version_unsupported` |
| `host.url` | 예 | https(loopback 시험 제외), userinfo 없음 |
| `host.compatibility` | 예 | 클라이언트가 지원하지 않으면 연결 전 거부 |
| `host.ca_pem`/`ca_sha256` | 함께 | 공개 CA일 때 생략. sha256 불일치면 `handoff_ca_mismatch` |
| `namespace_id`, `device.device_id` | 예 | canonical UUID |
| `device.actor` | 예 | Host 등록 actor |
| `device.credential.delivery` | 예 | 항상 `separate`. 값 필드가 있으면 파일 전체 거부 |
| `projects[].repositories[].remote` | | credential 포함 URL 거부(기존 mapping 규칙) |

**credential 전달**: `device issue --credential-out`이 만든 파일(0600/ACL) 또는 1회 출력값을 사내 비밀 공유 수단·직접 입력으로 전달한다. 채팅·메일·Git·인계 JSON에 넣지 않는다. 전달 후 원본 파일은 삭제한다.

## 4. 관리 통신 (3)

| 작업 | 경로 | 이유 |
|---|---|---|
| 기기 발급·회전·폐기·권한 | 로컬 `AuthRegistry`(Host DB 직접, 관리자 계정) | 기존 설계: credential은 로컬 관리자만 발급 |
| project 생성 | 임시 bootstrap 기기 → HTTPS 루프백 → `create_scope` → 임시 기기 폐기 | 업무 데이터는 정식 operation으로만 만든다 |
| 백업·복원 확인 | 로컬 migration 경계(quiescent) | 기존 |
| 클라이언트 이관 import | 로컬 transfer import(빈 namespace) | 기존 |

pmt-server는 Host가 실행 중이 아닐 때 기기 발급은 가능하지만 project 생성은 `host_unreachable`로 거부한다.

## 5. 기준

### 5.1 버전·호환 (X-01)

| 항목 | 규칙 |
|---|---|
| 플러그인 버전 | `pmt-lifecycle`·`pmt-server` 같은 번호(0.5.0부터). 마켓플레이스 두 항목·plugin.json 4개·`pyproject.toml`을 함께 올린다 |
| Core 버전 | 계약 변경이 없으면 patch만(0.4.x). Host·클라이언트 major.minor 일치가 호환 조건 |
| DB/graph/protocol | 5/1/1 유지. 바뀌면 별도 단계와 migration 문서 필요 |
| 업그레이드 순서 | Host 먼저(S-17) → compat 확인 → 개발 기기 플러그인 업데이트 |
| 인계 형식 | `pmt-handoff` v1. 필드 추가는 v2로 하고 클라이언트는 모르는 버전을 거부 |

### 5.2 설정 원천

| 대상 | 파일 | 갱신 |
|---|---|---|
| 클라이언트 저장 프로필 | `<ConfigRoot>/storage.json` 계열(기존 `storage_path`) | `configure_storage` CAS |
| 클라이언트 project 목록 | `<ConfigRoot>/projects.json`(신규, 비밀 없음) | `pmt connect`/`pmt link` |
| 클라이언트 실행 정보 | `<ConfigRoot>/client.json`(신규: python 경로, 마지막 모드) | SessionStart |
| 클라이언트 credential | `<ConfigRoot>/secrets/` (C-04) | `pmt connect`, SessionStart |
| Host 설정 | `<HostConfigRoot>/host-config.json` | `pmt-server config`/`init` CAS |
| Host 비밀 | `<secrets>/claim-*.{key,dpapi}`, TLS key | `pmt-server secret`/`tls` |

### 5.3 보안 기준

- TLS 필수, 내부 CA면 공개 CA만 배포. Host bind는 literal IP.
- 방화벽은 허용 대역만 열고 인터넷 전체 공개를 하지 않는다. 원격이면 VPN을 우선한다.
- 기기별 credential·actor 분리. 기본 권한 `read,write,runtime,review`, `admin`·`"*"`는 임시 bootstrap에만.
- 비밀 파일: Linux 0600/0700, Windows ACL(관리자·SYSTEM·구동 계정)과 DPAPI.
- 비밀이 들어갈 수 있는 출력은 `--credential-out` 파일 우선.

### 5.4 검증 기준

- 각 기능의 T-ID 시험을 수행하고 명령·종료 코드·환경·증거를 `docs/phase5/evidence/<날짜>/`에 남긴다.
- 단위/fixture(격리 ConfigRoot·DataRoot, loopback TLS), 실제 제품 설치(새 세션), 실제 서버 운영(재부팅·방화벽)을 구분한다.
- 실제 사용자 설정·DB를 시험에 쓰지 않는다.

## 6. 빌드·시험 (X-03, X-04)

- `scripts/build_plugins.py`: `PRODUCTS`에 server 대상을 추가한다. server 번들은 `.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`, `hooks/`, `bin/`, `skills/pmt-server/`, `templates/`만 포함하고 `src/`를 넣지 않는다. 클라이언트 번들은 루트 manifest(C-02)와 `bin/pmt`, `bin/pmt.cmd`, `scripts/pmt_easy.py`를 포함한다.
- 시험 분리: `tests/client_setup/`(C-01~C-11), `tests/server_admin/`(S-01~S-17, 시스템 변경은 명령 생성 결과만 검증), `test_packaging.py` 두 번들 기준 갱신. 실제 작업 스케줄러·systemd·방화벽 적용은 실측 단계(F)에서만 한다.
