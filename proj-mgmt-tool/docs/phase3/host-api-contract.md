# Host 연결 계약 v1

이 문서는 구현 연결 기준이다. Host 전체 구현·실제 HTTP/HTTPS 수용 완료는 [구현 상태](implementation-status.md)와 실제 증거로 판단한다. 현재 사용자 수용 범위는 격리된 로컬 서버와 두 클라이언트다.

## 저장·인증

Host 하나가 SQLite와 resources를 소유한다. 클라이언트는 DB 파일을 공유하지 않고 저장 operation을 호출한다. Git·문서 게시·모델 선정·native/CLI 실행·프로세스 스풀은 클라이언트에 남는다. HTTP 서버는 기존 전체 dispatcher를 공개하지 않는다.

`namespace_id`, 등록 `device_id`, 환경 프로필 `environment_id`, 작업 `session_id`를 분리한다. credential은 로컬 Host 관리자만 발급·회전·폐기하며 서버에는 hash만 저장한다. 클라이언트 설정에는 환경변수 이름을 저장하고 값은 요청 시 읽는다. 환경 UUID가 같다고 기기 인증이 같아지지 않는다. IP·hostname은 권한 키가 아니다.

서버는 매 요청의 credential·device·namespace와 등록 session/environment를 확인한다. 변경 transaction 안에서도 현재 기기 상태·scope·permission을 다시 확인한다. 요청의 actor 문자열이나 capability Boolean은 인증 근거가 아니다. body actor는 서버 등록 actor와 일치해야 한다. 허용 scope의 자손만 조회·변경할 수 있다. Step 원문은 관리 조회에서 제공하지 않으며 현재 run의 권한 있는 runtime 조회로만 제공한다.

점유 `claim_ref`는 locator다. 내부 lease nonce·generation·fingerprint·record/scope·actor/device/session·server key ID로 HMAC 토큰을 만들고 hash만 claims에 저장한다. 내부 토큰은 공개 응답/requests cache/outbox/log에 기록하지 않는다. release/finish는 인증과 현재 lease를 확인한 뒤 transaction 내부에서 토큰을 재생성한다. 키 값은 Host의 보안 환경 설정에 둔다. 활성 lease가 있을 때 해당 key version을 폐기하지 않는다.

## HTTP 경계

일반 연결은 인증서를 검증하는 HTTPS다. redirect는 따르지 않는다. local fixture에서만 명시 옵션과 literal loopback 주소의 HTTP를 허용한다. 기본 JSON 요청·응답 상한은 각각 1MiB이며 리소스 전송은 별도 크기 상한·hash 검증을 사용한다. CLI protocol-v1과 HTTP API-v1은 독립된 version이다.

| 경로 | 입력 → 출력 |
|---|---|
| `GET /health` | 비밀 없는 가용 상태 |
| `GET /api/v1/compatibility` | 인증된 device/namespace → core/db/graph/protocol version·actor·scope·permission |
| `POST /api/v1/sessions` | session ID·environment UUID → 해당 기기의 등록 session |
| `POST /api/v1/operations` | 기존 protocol-v1 요청 → `{api_version:1,envelope,exit_code}` |
| `GET /api/v1/requests/{request_id}` | 현재 소유 session → `{api_version,actor,session_id,envelope,exit_code}`; 미존재는 envelope/exit 둘 다 null |
| `POST /api/v1/resources` | 최대 4KiB의 `X-PMT-Resource-Metadata` JSON·최대 8MiB의 raw bytes → hash/size/scope/purpose에 묶인 artifact/게시 receipt |
| `GET /api/v1/resources/{resource_id}` | 권한 있는 scope·선택적 현재 private run → 검증한 bytes·hash/scope/purpose/resource 헤더 |
| `POST /api/v1/transfers/import` | 관리자 권한·bundle ID/manifest hash 헤더·최대 64MiB ZIP → 빈 namespace 검증/import receipt |
| `POST /api/v1/transfers/backup` | 관리자 권한·UUID request ID → version/manifest에 묶인 download ref |
| `GET /api/v1/transfers/download/{ref}` | 원 device/actor/session의 현재 관리자 권한 → ZIP bytes·bundle/manifest/ref 헤더 |

인증 헤더는 `Authorization: Bearer …`, `X-PMT-Device`, `X-PMT-Environment`, `X-PMT-Namespace`, 등록 session을 요구하는 요청의 `X-PMT-Session`이다. header 원문은 로그에 남기지 않는다.

기대 요청 본문을 확인하는 결과 조회는 `X-PMT-Request-Fingerprint` SHA-256을 함께 보낸다. 소유자·현재 권한·요청 지문이 모두 일치해야 저장된 응답을 회수한다. 일반 리소스 전송으로 Step 원문이나 private context를 공개하지 않는다. 같은 bytes를 다른 purpose로 재등록해 private 접근 규칙을 낮출 수 없다.

호환 조건은 같은 core major/minor, DB schema, graph schema, 지원 protocol이다. 0.3.x patch 차이는 같은 계약일 때 허용한다. HTTP 성공·core `ok`·exit 0은 일치해야 하며 오류 상태와 nonzero exit도 일치해야 한다. 잘못된 envelope·UTF-8·version·크기는 실패다.

| HTTP | 의미 |
|---|---|
| 400 | 입력/계약 오류 |
| 401 | 인증/등록 session 불일치 |
| 403 | scope/permission 부족 |
| 409 | revision·소유권·source·동일 request ID의 본문 충돌 |
| 503 | 일시적 저장/통신 불가 |

업무 응답의 `error.code`와 core exit 의미를 보존한다. 응답 유실은 effect unknown이다. 같은 request ID를 조회한 뒤 같은 입력으로 조정하며 새 ID로 바꾸거나 완료로 추측하지 않는다. `HttpStore.get_request_result`는 미존재면 `None`, 존재하면 `(envelope, exit_code)`로 LocalStore와 같다.

## 원본·실행 경계

논리 workspace는 `pmt://<repository UUID>/<branch key SHA-256>`이다. 로컬 절대 경로와 environment는 이 키에 포함하지 않는다. 같은 저장소/branch/상대 경로의 두 PC는 같은 lock 범위를 사용한다. 별도 branch scope여도 같은 Step 점유는 독립적으로 중복 실행할 수 없다.

클라이언트 mapping은 논리 저장소/branch를 로컬 checkout·상대 graph 경로에 연결한다. 파일 접근 전 현재 Host run/claim/source/directive 권한을 조회한다. 로컬에서 실제 SourcePin·path containment·링크/reparse·원격 저장소를 확인한다. 연결 실패나 source 차이는 새 공유 작업과 파일 접근을 막는다. Host의 Python·경로·환경을 클라이언트 검증 지문으로 대신하지 않는다.

Source pointer는 Project·논리 workspace·상대 graph 경로를 함께 구별한다. 같은 저장소/branch의 다른 프로젝트가 서로의 source를 덮지 않는다. 기존 pointer는 같은 Project/path에만 호환 읽기·CAS 갱신을 허용한다. `read_source_metadata`는 정확한 repository/project/workspace/graph mapping과 현재 scope 권한으로 관리 리소스 hash·SourcePin을 확인한 metadata만 반환한다. 실행 lock이 없어도 이력 확인에 사용할 수 있지만 `execution_authorized=false`이며 파일 접근/실행 권한을 주지 않는다.

Queue·scope union·opaque handle·결과·현재 source/근거 refs는 Host 상태 포트로 저장한다. 실행 argv·provider 설정·환경변수·PID·스풀은 로컬이다. 취소 요청은 종료 확인이 아니다. 실제 종료 receipt·근거·독립 검증·메인 통합 검토를 구분한다.

전송되는 route는 agent/provider/model·선정 이유·지원/인증 상태·capability ref·실행 mode/한도 같은 식별 metadata다. command/argv/env/PID/credential/spool 등 실행 설정은 클라이언트와 서버 양쪽에서 거부한다. Host에 저장된 route는 선택한 실행의 기준이며 역할 등급이나 caller의 capability 표현이 접근 권한을 부여하지 않는다.

`read_execution_control`/`write_execution_control`은 현재 source·run/lock·등록 소유자를 확인하는 저장 RPC다. control body는 ref/hash/nonce/stage만 보관한다. bounded native action 원문은 원 요청의 private 응답 cache 한 곳에 둔다. 원 요청 응답을 보관할 때도 source/context·nonce·control ref·prompt hash·현재 run state를 검증하며 ACK는 실제 handle과 semantic fingerprint를 묶는다. private pending action 읽기에도 같은 기준을 재확인한다.

`read_step_batch`는 parent·ordered member·각 지시/context/criteria ref·source·scope union hash·물리 slot/handle·report ref를 반환한다. private 지시/prompt·로컬 경로는 포함하지 않는다. 메인 native action은 현재 권한 아래 각 child의 F5 문맥을 별도로 읽고 검증한다. 하나의 parent 물리 handle에 연결된 child의 결과는 개별 검토하며 누락/미확인 결과는 union을 유지한다.

로컬 graph/문서 effect는 `begin_local_file_effect`/`complete_local_file_effect`/`read_local_file_effect`로 소유자·원 입력/hash·현재 source·CAS 단계·receipt를 기록한다. before graph ref는 서버의 실제 current pointer에서 유도한다. 부분 문서는 그 immutable 원본과 실제 manifest/coverage로 F3를 재계산해 원 변경·preview·apply·현재 pin을 연결한다. Host는 클라이언트 파일을 직접 확인했다고 주장하지 않으며 `host_document_verified=false`를 유지한다. 중단 복구는 같은 effect의 old/candidate/recovery hash와 현재 권한으로 조정하고 다른 사용자 변경을 덮지 않는다.

새 프로젝트의 구현 Step을 위해 `publish_client_plan`은 현재 source·완전한 두 트리·완료 F4 effect/coverage·원 artifact를 검증해 기존 `plans` metadata를 게시한다. review/write/runtime 권한과 현재 점유를 확인하고 기대 prior plan hash로 CAS하며 활성 run이 사용하는 버전을 임의 교체하지 않는다. `read_client_plan`은 권한 있는 Project의 version/hash/refs만 반환한다. Step 생성 시에도 plan의 현재 workspace·artifact·graph version/hash·commit을 확인한다.

모델 정책·가용 능력은 ConfigRoot의 `routing-client.sqlite3`에 보관한다. 기존 policy/capability 입력·선택 규칙을 재사용하며 원 business DB는 읽기만 하여 이전 선호를 보존한다. 과거 가용성은 unknown으로 재관찰한다. 클라이언트 설정 요청의 로컬 actor/scope bookkeeping은 Host 권한이 아니며, Host에는 제한된 실행 route만 전달한다.

전송 bundle은 정해진 manifest/SQLite/resource 파일만 허용한다. 링크·중복·경로 탈출·추가 항목·압축 상한·CRC/hash 오류를 거부한다. import는 Host 인증·namespace·claim key를 유지하며 업무 ID/FK/resource hash를 검증한다. live 요청 cache·credential·프로세스/기기/모델 설정·derived context는 이관하지 않는다. backup/restore 성공은 실제 사용자의 primary 전환을 의미하지 않는다.

원격이 끊겨도 local을 두 번째 shared primary로 만들지 않는다. 이미 생성된 결과만 원 owner/request ID/body hash/base revision/source와 함께 pending으로 보존한다. 재연결 시 서버 기록·현재 owner/source/revision을 확인하고 충돌을 유지한다. 신규 claim/작업은 연결 실패로 중단한다.

기술 선택은 기존 Python/SQLite 재사용과 단일 상태 규칙 유지다. FastAPI/Pydantic은 HTTP 입력·OpenAPI 경계, Uvicorn은 ASGI 실행, 표준 라이브러리 HttpStore는 서버 의존성 없는 클라이언트 전송을 맡는다. [FastAPI 실행 문서](https://fastapi.tiangolo.com/deployment/manually/), [proxy 연결 문서](https://fastapi.tiangolo.com/advanced/behind-a-proxy/), [Python urllib](https://docs.python.org/3/library/urllib.request.html), [SQLite backup API](https://www.sqlite.org/backup.html)를 근거로 삼는다. 직접 로컬 TLS와 실제 reverse proxy/Linux 배포의 수용 결과는 구별한다.
