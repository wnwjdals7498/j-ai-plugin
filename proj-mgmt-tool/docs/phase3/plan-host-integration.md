# F11–F15 Host 연결 단계별 계획

상태: 실행 계획. 아래 Step과 `P3-F11-01`~`P3-F15-03`은 예정 기준이며 구현·실측 완료를 뜻하지 않는다.
범위: Host 저장 API·클라이언트 설정·이관/복원·단절 복구·전체 통합. 공통 의미는 [Host 명세](spec-host-integration.md), [F0 계약](contracts.md), [전체 시험 기준](verification.md)에 따른다.
계획 전제: 로컬 SQLite·리소스·검증 전후 snapshot·실행기는 현재 로컬 경계로 취급한다. 이 계획은 이를 Host HTTP 기능으로 바꾸거나 Git/runner를 원격화하지 않는다.
공통 인터페이스: [구현 연결 규격](implementation-interfaces.md)의 SourcePin/source-root fingerprint·저장 포트·HTTP envelope·Host 허용 범위를 소비한다. operation 이름과 wire 필드는 그 문서에서 메인이 확정하며, 여기서 제안한 포트 의미를 독립 계약으로 구현하지 않는다.
외부 정보 근거는 공식 프로젝트 문서와 저장소 코드/시험만 사용한다. 운영 주소·인증 발급자·회전 기간·업로드 수치·보존 기간·제품 접근성은 확인되지 않은 환경 입력이다.

## 전체 경계와 완료 흐름

| 기능 | 산출 흐름 | 단계 완료 조건 |
|---|---|---|
| F11 | 저장 포트 → HTTPS Host API → Host SQLite/리소스 | 저장 전용 allowlist, 권한·버전·재처리 확인 |
| F12 | 제품 설정 → 연결 검증 → LocalStore/HttpStore 선택 | 안정적인 환경/저장소 매핑, 로컬 실행 유지 |
| F13 | quiesce → 빈 namespace stage → 검증 → 단일 primary 결정 | 원본 보존, manifest·관계·해시·복원 증거 |
| F14 | 생성된 결과 pending → 같은 요청 재조정 | 새 offline 공유 write 금지, stale 거부 |
| F15 | 계층별 실제 통합 증거 → 수용 판정 | remote·native·격리 HTTP 결과를 별도 기록 |

HTTP API-v1과 CLI protocol-v1은 독립 wire version이다. HTTP 결과는 클라이언트 저장 포트에서 기존 CLI envelope·exit 의미로 변환한다. HTTP 요청은 서버 발급 opaque claim과 재처리를 식별할 `request_id`를 전달하고, 서버는 인증된 actor/device/session·scope·현재 revision을 검증한다. client가 보낸 `device_id`나 capability 주장은 인증 사실이 아니다. `environment_id`는 작업 프로필 식별자라 기기 인증 ID와 분리한다.

기술·선정 근거는 [3단계 기술 선택](../03-hosted-storage.md#기술-선택)을 따른다. Host만 FastAPI/Pydantic·Uvicorn을 선택 설치하며, 로컬 모드의 표준 라이브러리 의존 경계를 유지한다.

| 경계 | 기술·작업 이유 |
|---|---|
| Host 요청·실행 | Python/FastAPI/Pydantic으로 허용 operation·형식·OpenAPI를 연결하고 Uvicorn으로 서비스 실행. 초기 단일 Linux Host/프로세스에서 실제 저장 경합을 확인 |
| 저장·복구 | Host 로컬 SQLite·디스크 리소스, 짧은 transaction/CAS·backup API·hash/journal. 기존 업무 의미를 보존하고 DB/파일 중단을 따로 복구 |
| 클라이언트·배포 | Python 표준 HTTP/TLS 연결·SQLite outbox·기존 제품 adapter. HTTPS는 Caddy/기존 proxy, Docker Compose는 반복 배포 필요 시 선택. 서버 의존성을 클라이언트에 강제하지 않음 |

허용 HTTP 기능은 scope가 검증된 저장 상태 읽기/변경, claim/result, 리소스, 권한 제한된 파생 조회뿐이다. 기존 전체 phase-2 dispatcher, Git 명령·파일 게시·runner 실행·일반 명령 실행은 API로 노출하지 않는다. 검증 snapshot은 실제 대상 환경에서 클라이언트가 전후 수집하고 Host는 주체·resource/source 및 현재 버전을 검증한다.

Scope는 canonical repository 및 논리 resource 이름, checkout/branch/work/run 관계로 결정한다. 서로 다른 장치의 로컬 절대 경로를 비교해 scope를 정의하지 않는다. 경로 문자열 차이로 겹치는 논리 작업을 우회할 수 없어야 한다. Git 구조화 데이터는 권위 원본이며 Host index는 권한 필터와 source cursor를 가진 재생성 가능한 읽기 전용 캐시다.

모든 미지 응답은 먼저 원 request ID와 body fingerprint로 서버 결과를 조회한다. 결과가 확인되기 전 다른 request ID로 바꾸어 재실행하지 않는다. pending은 이미 만들어진 결과만 담으며 오프라인 중 새 공유 점유/변경을 만들지 않는다. 파일 게시·SQLite transaction은 journal/outcome으로 잇고 하나의 원자 효과라고 가정하지 않는다.

## F11 — Host 저장 API·서버 운영

### F11-S1 — 저장 전용 서비스 경계 확정

- 목적: 로컬 업무 저장 의미를 보존하며 Host에 노출할 최소 호출 면을 고정한다.
- 기능: 승인된 저장 operation만 allowlist로 표현하고 CLI protocol-v1과 별개의 HTTP API/schema version을 선언한다.
- 범위: 저장 포트 의미, 요청 주체/버전/범위 envelope, 거부 응답과 호환 조회. 기존 전 dispatcher 공개는 제외한다.
- 선행: F10 완료; 기존 로컬 저장·검증·리소스 동작과 공통 interface 문서 초안 확인.
- 입력 → 출력: operation intent, authenticated principal, canonical scope, schema version → 검증된 저장 command 또는 명시적 거부.
- 처리: operation별 field allowlist/type/size를 검증하고 principal에서 actor/device/session 권한을 도출한다. caller가 넣은 권한·물리 경로·명령은 무시/거부한다.
- 실패·복구: 미지원 버전·필드·scope는 부작용 전에 거부한다. 공통 wire 변경은 소비자/시험 영향과 함께 메인 계약으로 되돌린다.
- 확인: `P3-F11-01`에서 schema 호환·잘못된 요청 거부, local operation 의미와 HTTP operation의 일치·차이를 확인한다.
- 증거·완료: 실제 HTTP 요청/응답, OpenAPI/version, 거부 기록과 contract version을 남긴다. 인터페이스 변경 제안·미확인 호환을 인계한다.

### F11-S2 — 인증·scope와 원자 상태 전이

- 목적: 서로 다른 사용자·장치가 같은 권위 namespace에 접근해도 범위 밖 읽기·중복 반영을 막는다.
- 기능: 서버 검증 principal, opaque claim hash, body fingerprint, replay `request_id`, revision/owner CAS 및 충돌 결과.
- 범위: 저장·claim·result 원자 판정과 허가된 조회. Git/runner/임의 명령의 원격 수행은 제외한다.
- 선행: F11-S1 저장 operation 계약 및 기존 SQLite 상태 전이 의미.
- 입력 → 출력: 인증 credential reference, request/`request_id`, body fingerprint, source version, claim → 단일 결과 또는 충돌/미확인 상태.
- 처리: claim 원문은 opaque로 취급하고 서버에는 hash만 보관한다. 인증 principal의 scope를 검증한 뒤 상태·revision·요청 재처리 기록을 한 SQLite transaction에서 판정한다.
- 실패·복구: 동일 `request_id`/동일 fingerprint는 기존 결과를 반환하고 다른 fingerprint는 conflict. 연결 끊김은 `request_id` 결과 조회로 복구하며 확인 전 대체 요청을 금지한다.
- 확인: `P3-F11-02` 독립 프로세스 동시 claim, replay, 본문 불일치, stale revision/owner, 응답 유실을 실제 Host DB와 대조한다.
- 증거·완료: 프로세스/HTTP 관찰, DB revision·owner·요청 재처리 행, 정제 로그를 묶어 원자성과 1회 반영을 인계한다.

### F11-S3 — 리소스·TLS·운영 기준 연결

- 목적: 저장된 리소스를 scope·hash 기준으로 공개하고 실제 HTTPS 배치에서 보안 경계를 확인한다.
- 기능: 크기 제한 업로드, 임시→게시 journal, hash·권한 검사, TLS reverse proxy, 백업 가능한 버전 세트.
- 범위: Host 로컬 디스크/SQLite와 HTTPS 경로. 모델/provider API, public signup, 여러 Host 공유 SQLite는 제외한다.
- 선행: F11-S1/S2 및 승인된 proxy·auth 설정 인터페이스.
- 입력 → 출력: resource bytes/hash, canonical resource scope, upload bound, principal → 게시 ref 또는 실패·복구 가능한 journal 상태.
- 처리: Host는 업로드 hash/size/scope를 확인하고 제한된 ref만 반환한다. URL/proxy는 Caddy 또는 기존 승인 proxy 선택으로 종단하며 서버 의존성은 클라이언트 설치에 강제하지 않는다.
- 실패·복구: DB/파일 중간 실패는 게시 안 됨/미확정으로 노출하고 journal 대조 후 재개·정리한다. 비밀은 설정 값이나 로그에 쓰지 않는다.
- 확인: `P3-F11-03` 실제 HTTPS·인증·scope 격리, 리소스 hash·읽기 권한, DB/파일 상태 및 복구 가능한 backup set을 확인한다.
- 증거·완료: HTTP/TLS 관찰, Host 파일 hash·권한, DB/journal·backup version, 비밀 제외 로그를 남긴다. proxy, issuer, 숫자 한도 등 배포값은 환경 미결정으로 인계한다.

## F12 — 플러그인 연결·환경 설정

### F12-S1 — 로컬 설정 의미와 저장소 선택

- 목적: 사용자가 선택한 namespace를 명시적으로 고정하고 기존 실행 정책과 분리한다.
- 기능: local/hosted 선택, HTTPS endpoint 및 secret-store credential reference, environment profile ID.
- 범위: 사용자 설정 입력·검증·저장. secret 값 출력/복제·모델 정책 변경은 제외한다.
- 선행: F11 API/schema/auth 계약 승인, 제품별 공식 setup 규약 확인.
- 입력 → 출력: storage mode, URL, credential reference, environment UUID → 검증 결과와 선택할 adapter 정보.
- 처리: endpoint/TLS 요구와 UUID 형식을 확인하고 비밀은 기존 안전한 참조만 저장한다. 프로필 복사는 device 인증을 복제하지 않는다.
- 실패·복구: 설정 손상·미지원 mode는 local 동작으로 조용히 전환하지 않고 오류를 제시한다. hosted 전환 실패 시 설정 원본을 유지해 명시적 재시도한다.
- 확인: `P3-F12-01` 격리 설정 저장·재시작 후 선택 유지, 동일 저장 업무 호출 및 로컬 실행기/모델 설정 불변을 확인한다.
- 증거·완료: 설정 전후 fingerprint, adapter 선택, 실제 local/HTTP 호출, 비밀 없는 진단 결과를 인계한다.
### F12-S2 — 인증된 연결·호환 확인

- 목적: 연결 시점에 TLS·인증·권한·wire 지원을 확인해 잘못된 Host에 상태를 보내지 않는다.
- 기능: health/version 확인과 principal scope 요약, 오류를 구분한 연결 결과.
- 범위: 읽기 전용 연결 점검과 제품별 setup feedback; 사용자 자격 증명 발급/회전 정책 운영은 제외.
- 선행: F12-S1, F11의 실제 허용 endpoint와 표준 오류 응답.
- 입력 → 출력: endpoint·credential reference·요구 schema → TLS/auth/schema/scope 상태와 다음 조치.
- 처리: TLS peer 확인 후 최소 scope 상태를 요청한다. credential 원문을 응답·환경 전체 출력·일반 로그에 포함하지 않는다.
- 실패·복구: TLS/auth/version/scope 오류를 구분하고 자동으로 권한을 넓히지 않는다. credential 갱신 후 같은 설정으로 다시 검사한다.
- 확인: `P3-F12-02` 실제 격리 Host에서 URL/TLS/API/auth/scope 오류를 각각 일으켜 안전한 거부와 비밀 비노출을 확인한다.
- 증거·완료: 각 결과 코드, Host/API 버전, 실제 HTTP 관찰, 설정 fingerprint와 정제 로그를 인계한다.
### F12-S3 — 논리 저장소·workspace 매핑

- 목적: 장치별 checkout 차이가 canonical 저장 범위와 권한 격리를 바꾸지 않도록 한다.
- 기능: repository, branch, checkout/workspace, work/run을 canonical resource 범위로 묶는 명시 매핑.
- 범위: 저장소/환경 매핑과 scope 충돌 검증. 로컬 절대 경로를 서버 resource identity로 저장하는 것은 제외한다.
- 선행: F12-S1/S2, F11 canonical scope와 overlap 기준.
- 입력 → 출력: environment profile, repository ID, branch/workspace 관계 → canonical mapping 및 겹침/누락 진단.
- 처리: 실제 checkout의 source fingerprint는 로컬에서 수집하고 승인된 commit/dirty 지문을 Host가 검증한다. scope 겹침은 모든 관련 논리 리소스에 대해 판정한다.
- 실패·복구: 다른 branch/commit, duplicate environment, 불완전 mapping은 shared write를 막고 재매핑 근거를 반환한다.
- 확인: `P3-F12-03` 실제 두 환경·같은 repository·다른 workspace/branch mapping에서 canonical ID와 겹침 거부를 교차 확인한다.
- 증거·완료: 두 환경의 mapping/version, local source snapshot과 Host 판정, HTTP scope 결과를 인계한다.

## F13 — 데이터 이관·백업·복원

### F13-S1 — Quiesce와 공유 데이터 export

- 목적: 진행 중 변경을 섞지 않고 Host 공유 상태로 옮길 일관된 자료 세트를 만든다.
- 기능: 신규 쓰기/실행 중지, active write·execution 종료 확인, schema/app/resource version 고정, manifest export.
- 범위: 공유 상태·증거 참조·원본 리소스 export. 로컬 모델 설정·기기 인증·credential·사용자 로컬 경로는 export에서 제외한다.
- 선행: F0 source namespace 및 environment/repository canonical refs, F11 target resource/schema API, 기존 execution/claim 정지 계약. F12 제품 hosted 선택·workspace mapping은 이관 준비의 선행이 아니며 F14에서 결합한다.
- 입력 → 출력: source namespace, baseline/dirty fingerprint, 대상 버전 → manifest·고정 ID/count·resource hash 목록.
- 처리: quiesce를 확인하고 DB의 일관된 backup 경로와 resource snapshot을 만든다. manifest hash는 전송 무결성만 뜻하며 출처 인증을 주장하지 않는다.
- 실패·복구: active run/write·미해결 claim·불일치 snapshot이면 중단하고 원본을 보존한다. 정지/재개 상태와 실제 이유를 명시한다.
- 확인: `P3-F13-01` 격리 로컬 DB에서 quiesce 후 빈 Host namespace로 전달할 IDs/count/relations/resource hash를 실제 비교한다.
- 증거·완료: 실행/쓰기 정지 관찰, source fingerprint, manifest/version/hash·명령 종료 코드와 resource 증거를 인계한다.
### F13-S2 — 빈 namespace staged import·검증

- 목적: 일부만 반영된 데이터를 primary로 선택하지 않도록 안전하게 stage한다.
- 기능: 빈 target 검증, staged import, 원자 namespace publish, 재시작 가능한 migration journal.
- 범위: 한 로컬 원본에서 빈 Host namespace로 단방향 이관. 비어 있지 않은 target 병합·자동 전체 효과 rollback은 제외한다.
- 선행: F13-S1 유효 manifest, F11 업로드/저장 API.
- 입력 → 출력: manifest 및 고정 버전 resource set → staged target, 검증 결과, 게시 상태/journal.
- 처리: ID·관계·count·hash를 전부 대조한 후 target namespace를 verified-ready로 게시한다. 클라이언트의 단일 primary 전환은 F13-S3 확인과 메인 판단 뒤에 수행한다. 사용자 데이터와 전체 부작용을 되감는다고 가정하지 않고 안전한 이전/이후 상태를 기록한다.
- 실패·복구: 중단·중복 ID·hash 오류·비어 있지 않은 target은 publish 금지. 같은 migration ID와 fingerprint를 조회해 이어가거나 격리된 stage를 정리한다.
- 확인: `P3-F13-02` 각 단계 강제 중단/재개와 mismatch·중복·nonempty target 거부를 실제 Host DB/files/journal로 확인한다.
- 증거·완료: stage/publish 단계, 고정 ID/count/관계/hash, DB 및 파일 관찰, 실제 프로세스 종료 코드와 원본 보존 증거를 인계한다.

### F13-S3 — 백업 복원·인덱스 재생성·전환 제안

- 목적: 호환 버전 세트를 복원하고 재생성 자료를 검증한 뒤 안전한 단일 primary 결정을 돕는다.
- 기능: 격리 위치 restore, DB integrity, resource 검증, read-only index 재구축과 migration summary.
- 범위: 복원·비교 및 전환 권고. 원본 삭제·양방향 primary·메인 소유 전환 판단은 제외한다.
- 선행: F13-S2, Host backup version set 및 F11 index/source cursor 규칙.
- 입력 → 출력: pinned code/schema/DB/resource backup → 복원 상태, 고정 ID·hash 비교, index build/source cursor.
- 처리: 별도 격리 namespace에 복원하고 Git source baseline을 고정해 허용된 snapshot과 비교한다. 파생 index만 재생성하고 권위 Git 자료를 갱신하지 않는다.
- 실패·복구: version 불일치·DB 무결성/리소스 손상은 복원을 실패로 표시한다. backup을 수정하지 않고 다른 유효 세트로 반복한다.
- 확인: `P3-F13-03` 실제 격리 restore의 DB integrity, ID/관계/resource hash와 index rebuild를 대조한다.
- 증거·완료: backup set version, 복원 위치 식별자, 실제 DB/files 관찰·종료 코드·index source cursor를 남기고 primary 전환 결정에 필요한 미해결을 인계한다.

## F14 — 단절 복구·다중 환경 조정

### F14-S1 — 단절 판정과 pending 보존

- 목적: 네트워크가 끊겼을 때 새 shared 작업을 소유한 것처럼 처리하지 않는다.
- 기능: 연결 상태 구분, 새 shared claim/write 차단, 이미 만들어진 결과만 durable pending으로 보관.
- 범위: 동일 namespace의 결과 보류. 오프라인 신규 shared write/claim, 다른 저장소로 자동 대체는 제외한다.
- 선행: F12 environment/mapping, F13 versioned namespace, F11 `request_id`/revision/claim 계약.
- 입력 → 출력: 실제로 완료된 로컬 결과·원 `request_id`/base revision/source fingerprint → pending ref 또는 연결 오류.
- 처리: HTTP 불통 시 기존 실행 결과를 보존하되 새 shared claim 및 변경 실행을 멈춘다. pending에 원 scope와 body fingerprint를 고정한다.
- 실패·복구: pending 영속 실패는 완료 성공으로 보고하지 않고 원 결과를 보존 가능한 로컬 증거로 반환한다. 무응답만으로 작업 종료/claim 해제하지 않는다.
- 확인: `P3-F14-01` 실제 격리 네트워크 차단에서 새 shared 요청이 중단되고 기존 생성 결과만 보류되는지 확인한다.
- 증거·완료: 차단/복원 방법, HTTP 관찰, pending DB row·결과 참조, 실행 실제 종료/미확정 상태를 인계한다.

### F14-S2 — 동일 요청 재연결·응답 불명 조정

- 목적: 서버가 반영했으나 응답만 유실된 요청을 중복 실행하지 않고 실제 결과를 회수한다.
- 기능: 동일 `request_id`/fingerprint replay, 결과 조회, owner/revision 재검증, 제한 재시도.
- 범위: pending 요청 결과 판정. body 변경·새 ID 교체·blind replay는 제외한다.
- 선행: F14-S1 pending, F11 원자 replay/claim endpoint.
- 입력 → 출력: pending 원 `request_id`/body fingerprint/base revision/claim → applied/already-applied/conflict/stale/unknown과 현재 참조.
- 처리: 동일 원 요청의 서버 기록을 조회한다. 기록이 없고 재전송 조건이 유효하면 같은 CID로 수행하고, 현재 owner/source version을 다시 대조한다.
- 실패·복구: 연결 지속 실패는 pending 유지. 본문 hash 변경·권한 폐기·stale owner는 충돌/차단으로 보존하며 새 실행을 하지 않는다.
- 확인: `P3-F14-02` 실제 응답 유실·같은 요청 재전송·중복 수신을 Host DB와 HTTP 관찰로 비교해 한 번 반영을 확인한다.
- 증거·완료: 원/현재 `request_id`·fingerprint·revision·owner, Host 요청 이력/DB, 재전송 시도·결과와 종료 코드를 인계한다.

### F14-S3 — 환경·branch 격리 및 충돌 전달

- 목적: 다른 checkout의 캐시나 점유가 같은 것처럼 사용되지 않도록 한다.
- 기능: canonical scope, actor/device/environment 구분, branch/commit/graph version 포함 cache/index key, 충돌 제시.
- 범위: 겹침·stale 판정과 evidence 연결. Git merge·자동 owner takeover·의미 판단은 제외한다.
- 선행: F12-S3 canonical mapping, F13-S3 source baseline, F14-S2 replay.
- 입력 → 출력: 두 환경의 repo/checkout/branch/work/run 및 baseline → 분리된 lookup·claim 상태 또는 정확한 겹침/stale 결과.
- 처리: scope 비교는 논리 리소스와 작업 관계로 수행한다. 서로 겹치지 않는 영역만 병렬 허용하며 cache 응답은 허가된 source cursor와 권한으로 필터링한다.
- 실패·복구: commit/graph/source 차이는 재조회 또는 사람 판단 대상으로 전달한다. 물리 경로가 다르다는 이유로 충돌을 해제하지 않는다.
- 확인: `P3-F14-03` 실제 두 환경/branch/commit에서 scope·cache 교차 조회 및 동시 claim을 수행해 겹침/stale 거부를 확인한다.
- 증거·완료: 두 환경의 입력 fingerprint, HTTP/DB owner·revision·index cursor 관찰, 충돌 사유를 인계한다.

## F15 — 전체 통합·배포 확인

### F15-S1 — 격리 배포·업데이트·복구 경로

- 목적: 저장 Host를 반복 설치·재시작·업데이트·복원할 수 있는지 격리 환경에서 확인한다.
- 기능: 단일 Host 배포, HTTPS/auth/schema health, DB/resource persistence, pinned backup restore manifest.
- 범위: 승인된 격리 Host 운영. 인터넷 공개 배포·실제 사용자 데이터 이전은 제외한다.
- 선행: F14 완료(공식 F15 착수·수용 관문), F11 실제 저장/보안, F13 backup restore, 승인된 배포 설정과 버전 세트. F14 전에는 격리 배포 준비를 앞서 수행할 수 있으나 결과는 F15 수용 완료로 집계하지 않는다.
- 입력 → 출력: release/config/schema version과 격리 credential/scope → 배포 단계별 결과·호환/복구 evidence.
- 처리: 선택된 reverse proxy와 Host 로컬 SQLite/files를 설치하고 health 이후 실제 요청으로 영속성·재시작·업데이트·복원 순서를 확인한다.
- 실패·복구: TLS/auth/schema/persistence/restore 실패를 각각 분류하고 수용 판정을 막는다. 미설치나 미실행을 pass로 대체하지 않는다.
- 확인: `P3-F15-01` 실제 격리 배포에서 TLS/auth/schema, 리소스 지속성, restart/update 및 pinned backup restore를 확인한다.
- 증거·완료: 명령/실행 종료 코드, Host·schema 버전, HTTP/DB/files 관찰, restore 결과 및 로그 비밀 점검을 인계한다.

### F15-S2 — 두 실제 HTTP client 통합

- 목적: 같은 Host 권위 namespace를 두 실제 환경이 올바른 revision/owner로 공유하는지 수용한다.
- 기능: 조회, 동시 claim, 변경, replay, 단절/재연결, cache/scope 분리의 통합 관찰.
- 범위: 격리 물리 Host와 두 client의 실제 HTTP 경로. remote Host와 native product 수용은 별도다.
- 선행: F14 완료, F12/F13 통합 가능, 실제 client identity/scope와 격리 데이터.
- 입력 → 출력: 두 환경의 mapping·허가 scope·source version → 동작별 판정, 최신 revision/owner 및 충돌 목록.
- 처리: 실제 HTTP 요청 두 개와 Host SQLite를 함께 관찰한다. Git snapshot과 검증 전후 지문은 각 대상 환경에서 얻고 Host는 해당 기준/근거의 권한을 검증한다.
- 실패·복구: 불명 요청은 원 `request_id` 조회, stale 충돌은 안전한 재조회로 제시한다. 새 `request_id`/자동 merge/owner 변경은 허용하지 않는다.
- 확인: `P3-F15-02` 조회·경합·변경·재전송·disconnect/reconnect 및 ID/revision/owner/cache 격리를 확인한다.
- 증거·완료: 두 client의 실제 동작/종료 코드, HTTP trace, Host DB·resource 관찰, source fingerprint를 기록하고 환경 등급을 표시한다.

### F15-S3 — 제품·remote 수용 판정과 최종 인계

- 목적: fixture, 실제 local HTTP Host, remote Host, 실제 제품 세션의 증거를 구분해 지원 범위를 판정한다.
- 기능: F11~F14 evidence manifest 집계, 제품 설치·새 세션 hosted flow, F10과 연결된 비용/품질 측정 참조.
- 범위: 가능한 제품과 승인된 환경의 수용. 외부 모델 API 호출, 승인되지 않은 배포/자료 이전은 제외한다.
- 선행: F15-S1/S2, 각 제품 실제 설치·새 세션·권한, remote endpoint 접근은 별도 조건.
- 입력 → 출력: 기능/시험 증거, 제품·Host/API 버전, environment/source fingerprint → passed/failed/blocked/not_run과 잔여 조건.
- 처리: 기존 ID `P3-F15-03`으로 실제 설치 제품별 저장 흐름을 확인한다. Claude 실서비스 시험·직접 모델 API 호출은 제외하고 사용자가 선택한 local fixture/native 실행 경로를 사용한다.
- 실패·복구: 제품 미설치·권한 부족은 blocked, 미실행은 not_run이다. 물리 Host+두 client의 pass는 remote/native 결과를 대체하지 않는다.
- 확인: 각 결과의 실제 계층·요청/행동·종료 코드·evidence ref를 검토한다. 비용·품질 절감 주장은 기존 동일 조건 F10 실측에만 연결한다.
- 증거·완료: 기능별 결과, 실제 제품·Host·API·schema 버전, 환경 지문, 한계·결함, dirty/commit 상태를 메인에게 인계한다. 전체 판정·Git 통합은 메인 소유다.

## 작업 인계 공통 형식

각 Step 작업 지시는 Step ID·목적·기능 경계·선행 결과·입력/출력 의미·허용 interface·금지 부작용·시험 ID·실제 증거·완료/인계 기준을 포함한다. 특정 파일의 줄 단위 수정 레시피를 만들지 않는다. Host 요청의 actor/device 인증 주체와 environment profile은 분리하고, source-root 기준 및 전후 fingerprint는 연결 규격대로 실제 client 대상에서 얻는다.

각 구현 인계는 Step ID, 결과, 변경 영역, 확정/제안 계약 차이, 수행 시험/종료 코드, 실행 tier, 실제 Host HTTP·SQLite/filesystem·프로세스 관찰, evidence ref, baseline·환경 fingerprint, 실패/blocked/not_run 및 복구 상태, dirty/commit 상태를 기록한다. 비밀·전체 payload·전체 환경변수는 로그/인계 자료에서 제외한다.

정확한 operation 이름·HTTP field·이벤트 이름은 공통 연결 규격이 정하는 계약값이다. 주소, proxy 선택, credential issuer/회전 기간, 업로드 한도 수치, pending 보존 기간, remote/native 접근 권한은 별도의 운영 미결정값이며 계약 미정과 혼동하지 않는다. 확인되지 않은 값은 실측으로 쓰지 않는다.
