# A1 공통 인터페이스 인계

기준: D1~D8 사용자 확정. plugin release 0.5.0 / Core 0.4.1 / SQLite5 / graph1 / protocol1 / HostAPIv1 유지.

## 소유권

메인: pyproject.toml, src/pmt/__init__.py, handoff.py, host/cli.py, host/auth.py, root plugin/marketplace manifests, build_plugins.py, 공통 문서/시험.
C1 시작 이후 server_admin/의 초기 __init__.py, __main__.py, cli.py 소유권을 서버 작업자에게 넘긴다. 기존 version 명령과 신규 console script를 보존하고 확장한다.

## 인계 JSON

- pmt.handoff.validate_handoff(dict, allow_loopback_http=False) -> 검증된 독립 복사 dict.
- load_handoff(path, allow_loopback_http=False) -> 1MiB bounded strict JSON 로드/검증.
- build_handoff(host_url=..., namespace_id=..., device=..., projects=None, ca_pem=None, ca_sha256=None, issuer_version="0.5.0", issued_at=None, allow_loopback_http=False) -> dict. 함수는 파일을 쓰지 않는다.
- device는 device_id/actor/permissions/scopes/credential만 가진다. 발급 결과의 plaintext credential/revision/state를 그대로 넘기지 않는다. credential={delivery:"separate",env:"PMT_HOST_CREDENTIAL"}는 참조 정보다.
- root issued_at/issuer/projects는 선택 필드다. 생성 함수는 세 필드를 채운다. 프로젝트/repository ID는 canonical UUID, graph_path는 repository 상대 경로, remote는 credential 없는 URL.
- Core호환은 major.minor 문자열 0.4, db5, graph1, protocol[1]. CA digest는 ca_pem의 UTF-8 **PEM bytes SHA-256**, DER 지문 아님. CA와 digest는 함께 있거나 함께 없다.
- client 생성 문서는 PMT_HOST_CREDENTIAL 이름을 사용한다. 인계 문서의 env 이름으로 임의의 프로세스 환경변수를 덮어쓰지 않는다. C-04 저장소/기존 credential_env 계약을 사용한다.
- unknown 필드/inline secret/credential 포함 URL/잘못된 ID/privkey/unsafe graph path는 handoff_invalid. 미지원 format/version은 handoff_version_unsupported, CA hash 불일치는 handoff_ca_mismatch, 미지원 compatibility는 incompatible.
- temporary bootstrap의 wildcard scope는 export 불가. project 생성 후 bootstrap 폐기, 일반 device의 실제 UUID scopes로 인계한다.
- plain HTTP는 explicit allow_loopback_http와 literal loopback일 때만 시험 허용. 운영은 TLS 검증 유지.

## Host 런처·기기 조회

- host.cli.serve_host(db,args,claim_keys=None,log_config=None) -> 기존 uvicorn single-worker 실행. args는 기존 serve namespace 필드(host,port,ssl_certfile,ssl_keyfile,allow_loopback_http,behind_proxy,trusted_proxy,claim_key_id,claim_key_env,retained_key)를 제공한다.
- claim_keys=None은 기존 env-reference 로딩. dict이면 decoded bytes keyring을 주입하며 primary는 args.claim_key_id다. log_config=None이면 legacy uvicorn kwargs와 동일, dict는 caller의 회전 로그 설정을 전달한다.
- 기존 pmt-host serve/_serve는 그대로 위 함수를 호출한다. Host API/application/allowlist/claim 인증 로직 변경 없음.
- AuthRegistry.list_devices() -> device_id,actor,scopes,permissions,revision,state,created_at,updated_at 리스트(읽기 전용). credential/hash 없음, revoked 포함.

## manifest·빌드

루트 proj-mgmt-tool/.claude-plugin/plugin.json이 단일 userConfig 원본. integrations/claude/.claude-plugin/plugin.json 삭제. Python만 필수, hosted 값은 조건부 검증 대상으로 선택화.
Claude displayName PMT, Codex compatibility interface.displayName PMT. 두 client manifest 0.5.0.
빌더는 두 root manifest를 복사하고 bundle hooks 경로를 ./hooks/hooks.json으로 재작성한다. server 대상·완전한 X-03/X-04·버전 자동 선택은 E3에서 마무리한다.
pmt-server console script는 실행 가능한 version 진입점에 연결됐다. 나머지 서버 기능은 C1 이후 구현.

## 검증

최신52 tests pass, 실제격리 HTTPS health 본문과 인증 compatibility 포함. 설치 pmt-server.exe version--json exit0. Claude plugin validate exit0. 독립 리뷰49개 focused tests pass, 추가 blocker 없음.
운영 Host/사용자 DB/설정/기기/비밀 변경 없음. A0의 기존 실패는 별도 baseline으로 보존.

## D1 등록에서 지킬 기존 scope 계약

S-11의 repository 논리 UUID 가정은 현재 Core와 다르므로 상세 명세를 정정했다.
create_scope의 payload는 kind/slug와 parent_id/body, 부모 관계는 environment→repository→project다. 실제 HTTPS write-only bootstrap으로 세 종류 생성이 통과했다. 임의 repository UUID만 mapping에 넣는 workspace 요청은 repository_scope_mismatch로 거부됐다.
project add는 정식 operation으로 environment/repository/project 관계를 마련하고 registry project를 게시한다. 첫 repo add는 그 실제 parent repository ID에 remote/name/graph_path metadata를 연결한다. 현재 한 project는 한 repository에만 bound된다; 두 번째 다른 repository를 같은 project에 붙이는 것은 거부하고 별도 project를 안내한다. API/schema/claim/auth 변경 없음.

## D2 인계와 E2 전환 준비

connect(config_root, handoff_path, *, credential=None, dry_run=False, environ=None, configure=configure_storage)는 HTTPS-only 검증·probe·CAS·projects/client metadata를 연결하고 비밀 없는 connection_summary를 반환한다. disconnect는 저장credential만 삭제한다. setup_lock(config_root)는 관리 CLI/Hook 연결을 직렬화하며 Core config-lock과 별도다.
merge_handoff_projects는 프로젝트 lock 안에서 실제 before/after bytes를 반환한다. write_client_metadata_snapshot은 Root lock 안에서 실제 before/after bytes를 반환한다. rollback은 projects→ConfigRoot 순서, credential restore의 _lock_held는 같은 Root config-lock을 보유할 때만 사용한다.
E2 source: MigrationCoordinator export는 maintenance meta를 쓰므로 원본 DB에 그대로 호출하면 file hash 보존을 보장하지 않는다. 격리 probe에서 writable BEGIN IMMEDIATE(no SQLwrites)로 다른 writer를 막고 별도 RO connection의 SQLitebackup이 성공했으며 rollback 후 원본mainfileSHA가 불변이었다. E2는 검증된 snapshot에 기존 migration을 적용하는 방식을 사용한다. Core/API/schema 변경 없음.
