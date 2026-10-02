# F11–F15 Host 연결 기능 명세

상태: 계획 명세. 구현·배포·실운영 시험 결과를 뜻하지 않는다.
기준일: 2026-10-02
적용 기준: [3단계 계획](../03-hosted-storage.md), [기능 목록](README.md), [문맥 효율화](01-document-context-efficiency.md), [2단계 공통 계약](../phase2/contracts.md), [통합 작업](../phase2/integration-work.md).

## 공통 Host 경계

Host는 PMT 상태·공유 점유·결과·근거·버전으로 식별되는 파생 인덱스의 저장 서버다.
Host는 모델 실행, 모델 선택, provider API 호출, 모델 라우팅을 수행하지 않는다.
Codex/Claude CLI, 네이티브 서브에이전트 및 작업 실행기는 각 클라이언트에 남는다.
승인된 Step directive는 관리 UI/일반 관리 조회에 복제하지 않는다.
Host는 승인된 Runtime 리소스를 권한 있는 runner에 전달할 수 있지만 PMT 관리 UI가 아니다.
로컬 SQLite는 계속 동작해야 하며 Host 사용 때문에 모든 클라이언트에 웹 프레임워크를 설치하지 않는다.
`LocalStore`와 `HttpStore`는 새로 설계할 연결 경계 이름이며 현재 구현 사실이 아니다.
클라이언트 업무 계약은 로컬/Host에서 같은 의미를 유지한다.
Host 통신은 HTTPS JSON `/api/v1`; DB·리소스 파일은 Host 로컬 디스크에 둔다.
초기 배포는 단일 Linux Host와 단일 SQLite 파일을 전제한다.
SQLite 파일을 여러 Host/기기에서 직접 공유하거나 다중 Host DB를 구성하지 않는다.
각 저장 namespace에는 권위 원본이 하나만 있다: local 또는 hosted 중 선택한 쪽이다.
Git의 구조화 데이터셋은 내용·graph의 권위 원본이며 Host 인덱스는 파생 캐시다.
Host 인덱스 키는 repository/project, actor 권한 범위, branch, commit/hash, graph·템플릿 버전을 포함한다.
Git snapshot은 승인된 읽기 기준으로만 받는다; Host가 Git을 쓰거나 최신 버전을 임의 선택하지 않는다.
클라이언트 source commit/dirty fingerprint가 불일치하면 거부 또는 재검토를 반환한다.
`environment_id`는 클라이언트 환경/프로필 식별자이고 `device_id`는 서버 등록·인증된 기기 주체다. 둘은 서로 다른 안정 ID이며 환경 프로필 복사는 기기 인증을 복사·공유하지 않는다. hostname/IP는 연결 진단용 변경 가능 힌트일 뿐 식별/권한 키가 아니다.
물리적 checkout 경로는 환경별 workspace mapping에 두며 canonical resource ID와 분리한다.
서로 겹치지 않는 checkout/work scope만 병렬 변경을 허용한다.
서버는 인증된 actor/device/session을 검증하고 서버 측 identity의 scope로 모든 조회·변경을 제한한다.
호출자가 보낸 capability 주장은 권한 근거가 아니다; 서버 검증된 권한만 사용한다.
토큰·비밀·전체 directive·전체 대화·환경변수는 로그에 남기지 않는다.
`request_id` 재전송은 같은 입력만 멱등 적용한다; 같은 ID의 다른 본문은 충돌이다.
revision·claim·owner 검증과 기록은 서버에서 원자 처리한다.
원격 저장이 끊기면 신규 shared claim/write를 시작하지 않는다.
이미 생성된 결과는 해당 namespace의 로컬 pending outbox에 보관하고 owner가 재연결 후 조정한다.
재시도는 원 `request_id`로 하고 현재 revision·owner를 다시 확인한 뒤 반영한다.
오래된 owner나 stale claim의 finish는 거부한다; callback 소실도 성공 처리하지 않는다.
파일 게시/업로드와 DB 트랜잭션은 하나의 원자 동작이 아니다; journal/outcome으로 복구한다.
백업은 코드·DB schema·resources의 호환 버전 세트를 고정한다.
서명 도입은 결정되지 않았다; manifest hash는 무결성 확인이며 출처 인증을 주장하지 않는다.
용어 `blocked`, `not_run`, 실패, 통과는 실제 실행 증거에 따라 구분한다.
세 기능군의 상세 공통 계약 파일은 별도 작성하지 않는다; 이 문서와 상위 계약을 참조한다.

## F11 — Host 저장 API·서버 운영

목적·이유: 동일 저장 서비스 계약을 HTTPS 경계로 노출해 승인된 환경들이 공유 상태와 점유를 일관되게 읽고 쓴다.
추가: FastAPI/Pydantic 요청 경계, Uvicorn 서비스, 인증/scope, 호환성, 멱등 변경, 원자 claim/finish, 리소스·인덱스 API.
수정: 2단계 저장 연결부를 LocalStore/HttpStore 선택으로 연결하고 오류·revision·증거 참조를 공통 의미로 반환한다.
삭제: Host에서 provider/model 호출, 임의 Git 쓰기, caller capability를 신뢰하는 흐름, 다중 Host SQLite 공유를 금지한다.
Goal: Host 저장/조회와 인증·점유·재처리가 로컬 계약의 의미를 보존하고 운영 가능한 단일 서버 경계를 제공한다.
Non-goal: 모델 라우팅/실행, Git checkout 편집, 다중 Host, 공개 무인 가입, 기존 데이터의 자동 병합.
입력: schema/API version, auth가 붙은 actor/device/session, repository/project/scope, request_id, expected revision, 기준 commit/hash, 구조화 delta 또는 claim/finish 자료, 리소스 bytes/hash.
입력 제약: 허용된 field/type/size와 scope만 받는다; 기준 commit을 확인할 수 없으면 저장/캐시 적중을 거부한다.
출력: versioned JSON 결과, 상태·새 revision·canonical ID, 결과/evidence/resource 참조, 오류 코드와 재조회/재시도 가능성.
출력 제약: secret·resource 원문은 권한 밖으로 노출하지 않는다; stale revision/owner는 성공 응답이 아니다.
타입: CLI protocol-v1은 기존 stdin/stdout envelope, exit 의미이고 HTTP API-v1은 독립된 wire version이다. `ActorId`, `DeviceId`, `EnvironmentId`, `SessionId`, `ActorScope`, `RepositoryRef`, `BaselineFingerprint`, `RequestId`, `Revision`, `ClaimRef`, `ResourceRef`, `Outcome`을 구별한다.
타입 의미: ID·계약 version은 식별값, revision은 양의 정수로 서버가 비교하며 hash는 알고리즘을 포함한다; Outcome은 pass/fail/blocked/not_run을 대체하지 않는다.
기본 auth 계약: TLS 인증서/peer 검증, 등록 device credential 발급·폐기·회전, 매 요청 actor/device/session 서버 검증, 최소 scope와 권한 축소/폐기 반영. credential 원문은 안전 전달하고 로그/일반 설정에 두지 않는다. 업로드는 크기 상한·hash·scope 확인 후 게시한다. 수치 상한·회전 주기·issuer/backend는 배포값이며 기본 보안 의미는 기능 요건이다.
API-v1 상태/code 계획: 400 input_invalid, 401 unauthenticated, 403 scope_forbidden, 409 version_or_owner_conflict, 503 transient_unavailable. 안정 `error_code`/request_id/retryability를 body에 반환하며 CLI protocol-v1은 기존 envelope/exit 의미로 변환한다. 이 HTTP wire mapping은 공통 계약 승인 시 확정한다.
추가 실패: API/schema 불일치, 중복 request_id의 다른 본문, busy claim, resource hash/size 오류, DB/파일 I/O, callback 결과 미확인.
실패 처리: 업무 write 실패는 rollback/error; 진단 로그 실패는 별도 진단하며 감추지 않는다; callback 유실은 조회로 실제 outcome을 확인한다.
기술: Python + FastAPI/Pydantic + Uvicorn, Host SQLite, Host disk resources, HTTPS reverse proxy(Caddy 또는 승인된 기존 proxy).
선정 이유: Python/SQLite 재사용, 타입 경계와 OpenAPI, 공식 ASGI 실행 모델, 파일/DB의 단일 Host 로컬 저장, TLS 종단을 명확히 한다.
클라이언트 선택 의존성은 Python 표준 라이브러리 우선이며 HTTP 구현을 위해 서버 전용 dependency가 클라이언트 설치에 강제되지 않게 한다.
모듈 소유: Host API/auth/protocol 경계가 저장 operation 허용 목록으로 입력을 검증하고 application service를 호출; 공통 service가 상태 판정; SQLite/resource adapter가 영속화한다. Git 편집·문서 게시·runner 명령은 HTTP 허용 목록에 넣지 않는다. 클라이언트가 실제 대상/환경의 전후 검증 지문을 수집하고 Host는 그 권한/출처·기준·근거를 검증한다.
의존성: F10 완료, 기존 저장·graph·Queue/lock·resource 계약, 메인이 승인한 API/schema 및 log/event 공통 계약.
P3-F11-01: 격리 Host를 실제 실행해 API/OpenAPI schema·호환 응답·잘못된 요청 거부와 local/HTTP 계약 일치 확인.
P3-F11-02: 독립 프로세스 동시 claim, 같은 request_id 재전송/다른 본문, stale revision/owner 및 callback 응답 유실을 실제 Host DB로 확인.
P3-F11-03: reverse proxy HTTPS 경로에서 인증·scope 격리, 리소스 해시/권한, secret 비기록, 백업 가능한 DB/resource 게시/복구를 확인.

제안 event: `host.request.accepted`, `host.request.rejected`, `host.claim.changed`, `host.resource.published`, `host.callback.unknown`.
trace 필드: correlation/request/actor/device/session ID, API/schema version, scope ID, baseline hash, claim/revision, resource hash/ref, result code, duration, retryable.
로그 필드에서 bearer token·비밀·전체 payload를 제외하고 오류도 안전한 코드/정제 요약으로 기록한다.
완료 인계: API/schema 및 module owner, 시험 결과별 실제 evidence, DB/resource backup 호환성, 실패·blocked·not_run, dirty/commit 상태.
자율 경계: API 표준 내부 오류·안전한 retry 처리 자율 수정; auth 범위, 공통 schema, 보존/운영 정책은 영향과 근거를 메인에게 전달.

## F12 — 플러그인 연결·환경 설정

목적·이유: 기존 제품 연결부가 저장소 위치를 선택하고 기기별 연결 정보를 안전하게 사용하도록 한다.
추가: local/hosted selector, Host URL/TLS/auth reference, environment UUID, repository/workspace mapping, API compatibility/health check.
수정: 제품별 setup에 hosted 저장 경계와 저장소 mapping을 연결하되 로컬 runner·현재 모델 정책은 유지한다.
삭제: setup이 host에 model/provider 설정을 보내거나 클라이언트의 물리 경로를 공통 resource ID로 취급하는 동작은 금지한다.
Goal: 선택한 단일 namespace로 동일 저장 API를 사용하며 다른 기기의 checkout은 명시 mapping으로 식별된다.
Non-goal: 사용자 비밀을 설정 화면/로그/저장소에 출력, 자동 이관, host가 기기의 Git을 대신 수정.
입력: `storage_mode`(local|hosted), HTTPS base URL, credential reference, stable `environment_id`, repository ID, branch/workspace mapping, API/schema version.
입력 제약: hosted는 HTTPS와 등록 device 인증 필수; URL 검증, credential 비노출, 환경/profile UUID와 device 인증 ID를 별도 관리한다.
출력: 선택된 storage adapter, connection/compatibility 상태, 권한 scope 요약, repository/workspace canonical mapping과 경고.
실패: URL/TLS/auth 실패, API 호환 불가, scope 부족, repository 미등록, mapping 불일치, duplicate environment UUID.
기술: 제품 adapter + 공통 storage interface, HTTP 표준 라이브러리 우선; 기존 setup/config 구조와 secret store를 재사용한다.
선정 이유: 제품별 실행 경계는 다르지만 업무 계약·저장 의미를 복제하지 않고 기존 설치 영향을 최소화한다.
모듈 소유: 제품 연결부는 입력·credential 참조·저장 adapter 선택; shared service는 연결 뒤 업무 규칙을 소유한다.
의존성: F11 승인된 API/schema/auth, F0 storage boundary, 제품별 공식 packaging/setup contract.
P3-F12-01: 격리 설정에서 local→hosted 선택, 재시작 후 유지, 동일 API 사용 및 로컬 실행기/모델 선택 불변 확인.
P3-F12-02: URL/TLS/API-version/auth/scope 오류 각 경로를 실제 격리 Host에서 실행하고 명확한 실패 상태와 비밀 비노출을 확인.
P3-F12-03: 두 environment UUID와 같은 repository의 서로 다른 workspace/branch mapping을 설정해 canonical IDs와 scope 충돌 검출 확인.
각 제품은 실제 설치·새 세션 검증과 mock/fixture 결과를 별도 evidence tier로 기록한다; 제품 미설치·권한 부족은 blocked다.
제안 event: `storage.connection.checked`, `storage.mode.selected`, `workspace.mapping.updated`, `storage.compatibility.failed`.
trace 필드: product/adapter version, environment UUID, repository/canonical scope refs, storage mode, API/schema, TLS/auth 결과 코드, mapping version, evidence ref.
완료 인계: 제품별 적용 범위·setup 입력/출력, secret 처리, 시험 tier/증거, 지원되지 않은 setup 경로.
자율 경계: mapping validation 오류는 자율 수정; 제품 권한 확대·제품 공통 설정 변경은 메인 판단.

## F13 — 데이터 이관·백업·복원

목적·이유: 로컬 원본을 손상 없이 선택한 Host namespace로 옮기고 장애/업데이트에서 호환 세트를 복구한다.
추가: quiesce/maintenance, manifest export/import, 빈 Host namespace import, ID·관계·resource/hash 검증, backup/restore, index rebuild.
수정: storage primary 전환은 검증 후 한 번에 수행하고 code/schema/resource version을 함께 고정한다.
삭제: 다중 로컬 DB 자동 병합, 기존 Host 자료에 대한 자동 DB merge, 실행 중 쓰기 snapshot, manifest hash를 서명/출처 인증으로 표현하는 행위.
Goal: 빈 Host namespace로의 이관과 복원에서 고정 ID·관계·리소스 바이트/hash를 검증하고 원본을 보존한다.
Non-goal: 양방향 동시 primary, 의미 충돌 자동 해결, 이관 단계에 포함된 원본 삭제.
입력: source namespace/DB, repository baseline/dirty fingerprint, resource manifest, schema/app/resource version set, maintenance 확인, target namespace.
입력 제약: 쓰기와 실행을 정지하고 in-flight execution 종료/회수를 확인; 대상 Host namespace가 비어 있어야 하며 다른 namespace를 임의 덮어쓰지 않는다. 공유 상태/증거/원본 참조와 클라이언트 설정·기기 인증·모델 정책을 구분해 로컬 설정을 Host 업무 원본으로 옮기지 않는다.
출력: migration manifest/hash, imported ID/count/reference summary, verified resource list, index rebuild status, selected primary 권고/상태.
출력 제약: hash는 전송 후 무결성 확인만 의미하며 저자·진위 보증이 아니다; 미검증 데이터는 primary로 승격하지 않는다.
실패: active write/run, 미종료 claim, schema incompatibility, nonempty target, ID/reference/hash/count 불일치, disk/DB failure.
기술: 일관된 SQLite backup API/복사 절차, manifest 기반 resource copy, hash 검증, staging→검증→publish journal; version pin/rollback. 전환 순서는 실행/쓰기 정지·종료 확인→빈 Host target import→ID/관계/resource 검증→인덱스 재생성→메인이 source·권한·실제 값을 확인하고 단일 primary 전환을 결정한다. 원본 삭제는 이 작업 기본 동작이 아니다.
선정 이유: SQLite 및 파일은 공동 원자 트랜잭션이 아니므로 staged publish와 재개 가능한 journal이 부분 실패를 드러낸다.
모듈 소유: migration coordinator는 순서/journal; storage adapter는 export/import; index builder는 기준에 묶인 파생물 재구성.
의존성: F11 resource/API/schema, F0 origin/version contract, 실행·claim quiescence 계약, 승인된 backup/restore 운영 절차.
P3-F13-01: 격리 local DB를 quiesce 후 빈 Host에 import하고 IDs/count/relations/resource hashes 및 migration manifest를 실제 비교.
P3-F13-02: 각 publish 단계에서 강제 중단/재개, 불일치 hash·중복 ID·비어있지 않은 target 입력을 시험해 원본 보존과 안전 중단 확인.
P3-F13-03: 호환 version set의 백업을 별도 격리 위치에 복원해 DB integrity·고정 ID·관계·resource hash·index rebuild 확인.
실제 명령·종료 코드·원본/대상 fingerprint·백업 세트 버전·evidence ref를 기록; fixture로 의미 검증 후 권한 있는 이관 경로의 전환 결정은 메인이 수행한다.
제안 event: `migration.quiesced`, `migration.manifest.created`, `migration.import.staged`, `migration.verified`, `backup.restored`, `index.rebuilt`.
trace 필드: migration/request ID, source/target namespace, baseline/schema/app/resource version, manifest hash, record/resource counts, 단계/outcome, journal ref.
완료 인계: 원본/대상 상태, 복구 가능 지점, 실제 검증 결과, manifest·backup evidence, 잔여 journal/미해결 불일치.
자율 경계: staging 경로 손상은 원본을 보존하고 중단·보고; 메인은 실제 값·권한과 primary 전환 여부를 판단한다. source 삭제·기존 Host merge는 이 작업 범위에 없다.

## F14 — 단절 복구·다중 환경 조정

목적·이유: 네트워크 단절 후 stale 결과가 공유 상태를 덮지 않게 하고 여러 기기·branch 작업을 조정한다.
추가: pending outbox, 재연결 reconciliation, owner/revision 재검증, environment/workspace-aware cache keys, claim/result conflict response.
수정: remote 결과 반영은 현재 서버 권한·revision·baseline을 재확인한 뒤 수행; 승인된 로컬 결과만 같은 request_id로 조정한다.
삭제: offline 공유 claim/write, offline 신규 owner 작업, blind replay, stale cache 재사용, 충돌 시 자동 Host 최신 복사.
Goal: 동일 namespace 재연결 후 안전한 재전송/충돌 노출과 환경·branch별 캐시 격리가 보장된다.
Non-goal: Host가 Git merge/checkout, 작업 의미 판정, 자동 owner takeover 또는 여러 Host DB 동기화.
입력: pending request/result IDs, source environment/workspace, base commit/hash, base revision, observed claim, result/resource references.
입력 제약: 결과 원 소유자가 재전송; scope와 authenticated actor 확인; base version이 변했으면 사용자/메인 판단 대상으로 반환.
출력: applied/already-applied/conflict/stale-owner/needs-rebase 상태, 최신 revision/owner 정보, 안전한 다음 행동, 유지된 pending 참조.
실패: 연결 재시도 소진, request body mismatch, revision/claim 변경, branch/commit 불일치, 권한 변경, outbox 손상.
기술: SQLite durable outbox, idempotent request ID, server-side atomic reconciliation; actor+repo+branch+commit+graph-version keyed index.
선정 이유: 동일 요청의 반복은 멱등하게 회수할 수 있지만 의미가 바뀐 기준/owner는 자동 병합할 수 없다.
모듈 소유: client adapter는 pending·재시도·관찰; server application은 인증·원자 판정; graph index는 기준 지문 검증.
의존성: F12 environment/workspace mapping, F13 versioned resource/backup and migration, F11 claim/revision API.
P3-F14-01: 실제 격리 네트워크 차단에서 신규 shared claim/write가 멈추고 생성된 결과만 pending으로 보존되는지 확인.
P3-F14-02: 복구 후 동일 request_id 재전송·응답 유실·중복 수신을 실제 Host에서 수행해 한 번 반영과 현재 owner/revision 확인.
P3-F14-03: 두 환경·branch·commit의 cache/scope 및 동시 claim을 교차 조회하고 mismatch/stale owner를 거부하며 pending을 보존하는지 확인.
시험은 네트워크 차단/복원 방법, 입력/환경 fingerprint, 관찰된 DB 상태, 종료 코드와 evidence ref를 남긴다; 모의 결과를 실제 네트워크 증거라 부르지 않는다.
제안 event: `storage.disconnected`, `storage.reconnected`, `outbox.pending`, `outbox.reconciled`, `outbox.conflicted`, `claim.stale_rejected`.
trace 필드: correlation/request/event IDs, actor/environment/workspace, baseline/hash, base/current revision, claim owner ref, outbox age/state, retry count, outcome.
완료 인계: 재연결 결과·충돌 목록·무손실 증거·scope/cache key 검증과 미해결 판단 항목.
자율 경계: 동일 요청 replay와 명시적 충돌 표시는 자율; 자동 merge, owner 교체, 오래된 결과 승격은 메인 판단.

## F15 — 전체 통합·배포 확인

목적·이유: local→Host 연결, 제품 setup, migration, 단절 복구와 다중 환경을 하나의 실제 승인 흐름에서 수용 판정한다.
추가: 의존성 연결, 배포 구성/업데이트·복구 확인, 제품별 실제 hosted session, 전체 acceptance/evidence manifest.
수정: 시험을 fixture, loopback/simulator, 실제 remote Host, 실제 제품 세션 단계로 구별해 결과를 집계한다.
삭제: 계획 시험/모의 성공으로 실운영 성공 판정, 실제품 미설치를 통과 처리, 전체 완료를 위해 미해결 범위를 숨기는 처리.
Goal: 승인된 전체 범위가 설치·저장·복원·재연결·권한 경계에서 실제 증거로 확인되고 지원 범위/차단 조건이 명확하다.
Non-goal: 외부 모델 API, 모델/provider 라우팅, 인터넷 공개 배포나 사용자가 승인하지 않은 실제 데이터 이전.
입력: 승인 API/schema, F11–F14 evidence, 배포/제품 version, 격리 credentials/scopes, 시험 data, native session 및 remote environment 접근 가능성.
입력 제약: 외부 모델 호출 없음; loopback/Host simulator 결과와 remote 실제 Host 결과, native product 결과는 다른 증거 계층.
출력: 항목별 passed/failed/blocked/not_run, 실제 환경·제품·버전, evidence manifest, 결함/제약, 전체 완료 판정.
출력 제약: 실행하지 못한 remote/native test는 not_run/blocked; 성공으로 간주하지 않고 품질·비용 실측도 없는 절감률을 주장하지 않는다.
실패: packaging/setup/update, HTTPS/auth/scope, API incompatibility, migration/restore, split-brain, stale cache/claim, 제품 호출·새 세션·권한 확인 실패.
기술: 기존 Python/SQLite/FastAPI/ASGI + reverse proxy 계획을 사용하고, Docker Compose는 반복 배포 필요가 확인될 때만 승인 계획의 선택지로 평가한다.
선정 이유: 현재 계획의 단일 Host 운영을 유지하며 격리 설치·업데이트·복구의 재현성으로 배포 위험을 확인한다.
모듈 소유: 메인이 통합·공통 계약·전체 scope 판정; 각 연결부 소유자는 제품/저장 경계 결과와 결함 증거를 제출한다.
의존성: F14 완료, F11–F13 승인 및 격리 검증; 제품별 설치·인증·실제 Host 접근은 native acceptance 수행 조건.
P3-F15-01: clean isolated deployment에서 API health/TLS/auth/schema, resource persistence, restart/update 및 pinned backup restore를 실제 확인. F10 효율 판정은 그 전 로컬 사용자 작업에서 비용/품질 기준을 완료해야 하며, F15는 동일 기준의 Host 연결 영향과 전체 통합 evidence를 보탠다.
P3-F15-02: 두 승인 환경에서 같은 프로젝트의 조회·동시 claim·변경·재전송·disconnect/reconnect를 실행해 ID/revision/owner/cache isolation 확인.
P3-F15-03: 사용 가능한 각 제품의 실제 설치·새 세션 hosted storage flow를 확인; 외부 모델 API 없이 사용자 선택의 로컬 fixture/native 경로만 사용하고 미설치는 blocked로 기록. 실제 remote Host 준비가 안 되면 remote acceptance는 blocked다. 격리 물리 Host와 두 client가 실제 HTTP·동일 Host DB로 조정한 pass는 별도 환경 증거로 기록할 수 있지만 remote/native pass를 대체하지 않는다.
P3-F15 test 결과는 시험 ID, 실제 단계, 명령/행동, 종료 코드, 제품·Host·schema 버전, env/commit fingerprint, evidence ref, 재사용 근거를 포함한다.
제안 event: `deployment.started`, `deployment.health_checked`, `integration.acceptance.recorded`, `integration.blocked`, `integration.completed`.
trace 필드: release/config/schema version, host/environment/product/session IDs, API compatibility, scope, baseline, test ID/tier/status, evidence manifest, exception/blocked reason.
완료 인계: 기능별 증거 상태, 제품별 범위, remote/native 확인 한계, 미해결 결함, 변경/dirty 상태와 전체 판정 제안.
자율 경계: 범위 내 배포 결함 수정·재시험은 자율; 공통 계약·권한·배포 노출 범위·미통과 예외는 메인이 판단한다.

## 인계 기준 및 미결정점

이 작업은 F11–F15의 설계 분해를 제공한다; 구현 착수나 전체 3단계 완료를 선언하지 않는다.
각 시험 ID는 예정 기준이다. 종료 코드·환경·증거가 아직 없으므로 모두 실측 결과로 표현하지 않는다.
운영값 미정: 실제 DNS/주소, proxy 선택·배포 설정, credential issuer/회전 주기, 숫자 upload 상한, retention/outbox 기간, 제품별 실제 접근 가능성. 기능 명세의 최소 발급·폐기·회전·TLS 검증·scope 축소·upload cap semantics는 구현 완료에 필요하며 운영값 미정이 기능 미정의 사유는 아니다.
TLS proxy 운영 선택(Caddy/기존 proxy), DNS/주소, 실제 remote Host 및 native 제품 접근은 운영 환경에서 확인한다.
F15 접근 조건은 격리 Host/API, TLS/auth credential, 저장소·기기 scope, 복원 가능한 테스트 자료, 실제 제품별 설치/새 세션 권한, 필요 시 remote Host endpoint다. 외부 모델 provider 호출은 시험 요구가 아니다.
Host의 callback 기능은 필요성이 확정되지 않았다; callback 실패를 성공으로 단정할 수 없다는 안전 규칙만 적용한다.
공유 schema·event 이름은 제안으로 남기며 확정 계약처럼 consumer에 요구하지 않는다.
마지막 판정 및 사용자 PMT 상태 반영, Git 통합·commit은 메인 소유다.
공통 header, cursor/revision, replay, scope filtering, resource journal 및 backup 규칙은 [F0 공통 계약](contracts.md)을 따른다; 아래에서는 Host 고유 세부만 정의한다.
