# 공통 계약 v1

이 문서는 구현 형태가 달라도 유지할 의미와 입출력을 정한다. 현재 본체는 Python 표준 라이브러리와 SQLite schema 2로 구현했다. 실행·설치 결과는 별도 검증 기록으로 확인한다. 계약 변경은 이유·소비자 영향·이관·시험 변경을 함께 제시하고 메인이 확정한다.

## 식별·범위·시간

| 값 | 의미 |
|---|---|
| scope_id / record_id / artifact_id | 경로·slug가 바뀌어도 유지하는 UUID |
| session_id | PMT 호출자의 명시적인 안정 세션 ID. 프로세스 PID만으로 공유하지 않음 |
| request_id | 한 논리적 호출의 재전송 ID. 재시도에서도 유지 |
| event_id | 실제 사용자·제품 이벤트 ID. 호출 재전송 ID와 구분 |
| correlation_id / causation_id | 관련 작업 묶음과 직접 원인 이벤트를 추적 |
| revision | 기록별 성공 변경마다 증가하는 정수. stale 입력으로 덮어쓰기 방지 |
| claim_token | 현재 작업 소유권을 나타내는 불투명 토큰. 해제·복구 후 이전 토큰은 무효 |

`environment_id`는 사용자·장치의 PMT 프로필 ID로 별도 사용자 설정에 보존한다. IP·Hostname·data root 변경으로 자동 교체하지 않는다. `db_id`는 데이터 저장소의 ID, `installation_id`는 제품·등록 scope별 설치 ID다. 같은 프로필/DB를 쓰는 세 제품은 environment/db ID를 공유하고 installation ID는 다르다. 회사·집을 별도 환경으로 구분할 필요가 있으면 명시적인 프로필 선택으로 등록한다.

시간은 UTC로 저장하고 사용자에게는 설정 시간대로 보여준다. 이벤트의 발생 시각과 PMT의 수신·기록 시각을 구분한다. 상태 순서는 제품 시계 대신 기록 revision과 저장 순서로 정한다.

환경은 고정 UUID + 보조 Hostname·IP, 저장소는 등록된 UUID + 선택한 canonical remote로 구분한다. remote의 표현 차이를 정규화할 때 인증정보를 제거한다. fork·다중 remote·별칭을 URL 유사성만으로 자동 합치지 않는다.

## 논리 데이터

| 영역 | 필수 내용 | 관련 기능 |
|---|---|---|
| 범위 | 종류, 부모 ID, 표시용 slug, 환경별 경로 매핑 | 환경→저장소→프로젝트→분류 |
| 기록 | 종류, 범위·부모 ID, 제목, 상태, 본문, revision, 작성·갱신 시각 | Work·Item·사실·원리·결정·파기·backlog |
| 업무 이벤트 | event ID, 대상·작성자, 변경 종류·이유, 이전/이후 revision, 근거 참조 | 변경 추적·결정·파기 |
| 요청 결과 | request ID, canonical 요청 지문, 처리 결과와 생성 ID | 성공 호출 재처리 |
| 점유 | 대상, owner session, token, 점유·갱신 시각 | 착수·해제·완료·복구 |
| 리소스 | ID, hash, 크기, 상대 위치, 상태, 참조·보존 기한 | 이미지·로그·증거 |
| 검증 | 정의 버전, 대상·환경·입력 지문, 결과·종료 코드, 증거, 기준 포함 관계 | 재검증 후보·완료 근거 |
| 설정·운영 | 설치·환경 ID, 데이터 경로, 스키마·연결부 버전, maintenance 상태 | setup·백업·이관 |

업무상 관계·상태·revision은 조회·제약 가능한 필드로 관리한다. 종류별 상세 본문은 구조화된 JSON을 사용할 수 있다. 구현 작업자는 저장 구조를 정하되 위 의미와 검증 가능한 관계를 유지한다.

## 요청·응답

CLI의 machine 모드는 UTF-8 JSON 요청 하나를 받아 JSON 응답 한 줄을 내보내고 종료한다. 제품 연결부는 매 호출을 이 계약으로 감싼다. 처음부터 상시 서버나 스트리밍 세션을 만들 필요는 없다.

| 요청 필드 | 규칙 |
|---|---|
| protocol_version, operation | 지원 버전과 동작 이름 |
| request_id, actor, session_id | 변경·점유·이벤트 입력에 필수. actor는 사용자/메인/제품 이벤트 출처 구분 |
| scope_id, record_id | operation에 필요한 대상 |
| expected_revision | 기존 기록 변경에 필수 |
| payload, context_refs | operation별 허용 필드와 근거 ID |
| source | 제품·제품 버전·연결부 버전. 직접 CLI이면 CLI 출처 |
| normalized_event | 이벤트 수집 시 event_id, type, occurred_at, session/turn/source_event 식별 정보 |

응답은 `protocol_version, request_id, ok, result, error, warnings`를 사용한다. `error`에는 `code, message, retryable`을 둔다. 성공 결과에는 대상 ID·revision·발생 event ID 등 추적값을 넣는다. stdout에 진단 로그·진행 문구를 섞지 않는다.

JSON 자체를 해석할 수 없는 요청의 응답은 `request_id: null`과 입력 오류를 반환한다. 계약 v1은 요청 JSON 1MiB 이내, native 수집 payload 64KiB 이내를 기본 상한으로 한다. 더 긴 명시 업무 본문은 리소스 참조로 전달하고, native payload 축소는 생략 여부·길이를 표시한다. 비유한 숫자·중복 JSON key·지원 밖 필드는 거부한다. 역할별 필수 필드와 기본값은 operation 규약을 따른다.

| 종료 코드 | 의미 |
|---|---|
| 0 | 조회 또는 DB 반영 성공. 비필수 진단 로그 경고는 warnings에 표시 가능 |
| 2 | 잘못된 JSON·필수 입력·미지원 계약·상태/완료 기준 위반 |
| 3 | revision·점유·동일 요청 키의 다른 본문 충돌. 최신 상태 확인 필요 |
| 4 | DB busy·파일 I/O·runtime 등 일시적 장애. retryable은 오류별 지정 |
| 5 | 처리하지 못한 내부 오류. 정상 완료로 보고하지 않음 |

native hook의 종료·출력 규약은 위 코드와 별도다. 연결부가 제품별 규약으로 변환한다. PMT 실패 JSON을 native hook stdout에 그대로 전달하지 않는다.

## 주요 operation

| 동작 | 입력 | 출력·불변식 |
|---|---|---|
| setup | 데이터 경로·제품·runtime·계약 버전 | 동일 설치 설정의 재호출은 동일 DB·환경 ID 유지. 실제 점검 상태 반환 |
| read_context | 범위·선택 Item·query·limit/cursor·요약 예산 | 상태·현재 결정·다음 작업·관련 근거 + revision·다음 cursor·축소 표시 |
| save_change | 대상, expected_revision, 변경·이유·근거 | 새 revision + 업무 event. 결정/파기·다음 작업은 명시 입력 |
| save_decision | 대상·revision, decider, 선택 내용/option ID/직접 작성/AI 위임, 이유·근거, supersedes | 사용자 또는 위임 범위의 메인 결정을 명시 저장. 현재 결정과 파기/대체 관계를 함께 갱신 |
| record_event | 정규화 이벤트와 안정 event ID | 한 실제 이벤트에 하나의 반영 결과. Stop/idle은 업무 완료를 만들지 않음 |
| claim_task | 대상·expected_revision·owner session | 가능 여부 확인과 소유권 기록을 함께 수행. token을 받은 뒤만 작업 |
| release_claim | 대상·현재 token·owner·중지/실패 이유·재개 정보 | 현재 소유자만 해제. pause/blocked 의미와 후속 조치 반환 |
| recover_claim | 메인의 명시 복구, 대상·revision, 이전 소유자 종료/격리 근거, 이유 | 복구 event + 새 소유권/token. 확인 없이 오래된 점유를 자동 교체하지 않음 |
| finish_task | 현재 token·expected_revision·결과·기준별 검증 참조 | 결과·기준·증거·소유권이 유효할 때만 Done + event + 점유 해제 |
| register_resource | 파일·범위·hash·보존 분류 | 검증된 ready artifact ID. 설치 경로를 파일 정체성으로 쓰지 않음 |
| record_verification / lookup_verification | 정의·대상/환경/입력 지문·결과·증거 | 성공/실패 기록 또는 유효 후보와 이유. 조건 불완전은 재사용 불가 |
| backup / restore / diagnose | 데이터 범위·운영 상태·백업 ID | 일관된 DB·리소스 manifest 또는 이상·복원 결과 |
| get_request_result | request ID | 이미 commit된 원결과 또는 not_found. 미조회 결과를 완료/미실행으로 추정하지 않음 |

핵심 저장 경계는 기존 네 기능 `read_context / save_change / claim_task / finish_task`다. 해제·이벤트·리소스·운영 기능은 보조 operation으로 같은 호출 규약을 사용한다.

문맥 요약은 기본 4,500자 예산으로 생성하고 상세는 ID·cursor로 조회한다. 관련 기준·경고를 숨기며 임의 절단하지 않는다. 출력 축소 사실과 상세 조회 방법을 제공한다.

## 원자성·중복·오류

1. 성공 변경의 상태·revision·업무 이벤트·request 처리 결과를 한 DB 트랜잭션으로 commit한다. 하나라도 실패하면 전부 취소한다.
2. 같은 request ID + 같은 의미의 요청은 저장된 성공 결과를 반환한다. 같은 ID + 다른 요청은 충돌이다. 일시적 실패는 성공 반영으로 고정하지 않는다.
3. event ID는 실제 발생 단위다. 제품의 안정 session/turn/tool/event 식별자를 사용하거나, 연결부 재전송함에 최초 ID를 보존한다. 단순 prompt/body hash로 정당한 반복 입력을 삭제하지 않는다.
4. 동시 변경은 expected_revision을 조건으로 반영한다. 경쟁에서 패한 요청은 최신 상태·이력을 수정하지 않는다.
5. SQLite busy는 한정된 시간·횟수만 재시도한다. 확인 불가한 결과의 재시도는 같은 request ID로 조회·재처리한다.
6. 업무 이력 저장 실패는 변경 실패다. 진단 로그 실패는 별도 warnings/fallback이며 이미 commit된 업무 결과를 뒤집거나 재실행하지 않는다.

request ID는 해당 DB 전체에서 유일한 UUID다. actor·session·operation·protocol·대상 ID·expected_revision·operation별 기본값을 적용한 payload/context_refs/normalized event가 의미 지문에 포함된다. 수신 시각·재시도 횟수·진단 상관값은 제외한다. `fingerprint_version=1`, key 정렬·UTF-8 JSON·SHA-256으로 직렬화하며 문자열/숫자를 임의 변환하지 않는다. 표현상 JSON key 순서 차이는 같은 요청이다.

save_decision의 payload는 `decision_kind(select/custom/delegate)`, decider, 대상 요구/Item와 revision, 선택 option ID와 내용 또는 직접 작성 내용/위임 범위, 이유·evidence/context refs, 선택을 확인한 명시 입력 출처를 요구한다. supersedes가 있으면 이전 결정이 같은 대상의 유효 결정인지 확인하고 현재 결정/파기·대체 관계를 함께 반영한다. native 종료 메시지 자체를 선택 확인 출처로 쓰지 않는다.

동시 최초 호출은 DB 고유 제약과 트랜잭션으로 한 번만 반영하고 나머지는 commit된 원결과를 반환한다. 반환 revision은 현재 최신 revision이 아니라 최초 요청의 결과다. 입력/상태/충돌의 결정적 오류는 가능한 경우 요청 지문과 원오류만 기록하며 재시도는 같은 결과를 반환한다. 상태 변경 후 새 시도는 새 request ID를 사용한다. DB busy·I/O 등 미반영 일시 실패는 성공/결정적 결과로 고정하지 않으며 같은 본문·ID로 다시 시도한다.

유효 JSON도 DB가 unavailable이면 결과/키 기록을 보장할 수 없음을 응답으로 표시한다. parse 불가 요청은 키를 기록하지 않는다. commit 후 응답이 유실되면 get_request_result 또는 같은 ID·본문의 재전송으로 확인한다. not_found는 아직 commit된 결과가 없다는 의미이지 이전 호출·프로세스가 종료됐다는 의미가 아니다.

PMT event ID도 UUID다. native 이벤트는 `product + installation/source instance + native session + event 종류 + native occurrence ID`를 namespace로 하여 정규화한다. native 식별자의 전역 유일성을 가정하지 않는다. 식별 조합이 충분하지 않으면 발생마다 UUID를 만들고 pending에 먼저 보존한다. 이 경우 중복 방지는 연결부의 같은 pending 재전송에만 보장하며 native의 재호출 자체를 식별한다고 주장하지 않는다. 다른 request ID로 같은 event가 재전송되면 기존 반영 결과를 반환하고 추가 mutation은 없다.

SQLite는 DB 쓰기를 직렬화하지만 외부 파일 작업까지 하나의 트랜잭션으로 묶지 않는다. [공식 격리 설명](https://www.sqlite.org/isolation.html), [Python 트랜잭션 제어](https://docs.python.org/3.13/library/sqlite3.html#transaction-control).

## Item 상태와 점유

| 전이 | 조건 |
|---|---|
| Planned → In Progress | 차단 없음·현재 revision 일치·점유 없음·유효 scope |
| In Progress → Paused | 현재 소유자가 재개 정보와 이유를 남기고 해제 |
| Paused → In Progress | 다시 명시적 claim |
| In Progress → Blocked | 실패·대기 원인과 다음 조치를 남기고 해제 |
| Blocked → Planned | 명시적인 차단 해소 이벤트 |
| In Progress → Done | finish의 결과·기준별 유효 근거·소유권·revision 확인 |
| 비종료 상태 → Canceled | 명시 취소·이유. 진행 소유자가 있으면 중단 확인 후 처리 |

완료 기준 미충족·미확인은 Done이 아니라 Blocked/Paused와 사유다. 사용자가 기준을 바꾸면 revision을 변경하고 그 기준으로 다시 판단한다. 과거 기준의 성공을 새 기준의 증거로 간주하지 않는다.

Work의 자식 진행은 조회의 `aggregate_state`로 집계하고 Work 자신의 점유·결과를 나타내는 `state`는 명시적으로 관리한다. 자식 완료만으로 부모 점유를 해제하거나 완료 이벤트를 만들지 않는다. 초기 집계는 모든 자식 Item이 종료이면 Done(전부 취소이면 Canceled), 나머지는 In Progress → Blocked → Paused → Planned 순서다. 프로젝트 종료·보관 전용 operation은 초기 CLI에 포함하지 않았다. 필요한 경우 별도 계약·revision을 추가하며 미완료 자식을 가진 프로젝트 종료를 허용하지 않는다.

초기 점유는 명시적 해제 방식이다. heartbeat는 생존 참고값이며 오래됐다는 이유만으로 자동 회수하지 않는다. 회복 동작은 이전 작업자의 종료 또는 작업 공간 격리 확인, 이유·근거 기록, 새 token 발급을 요구한다. 이전 token의 결과 반영은 거부한다.

claim/release/finish/recover는 기록 revision을 올리고 이전·이후 revision을 event에 연결한다. heartbeat만 갱신하면 점유 메타데이터만 바꾸고 record revision·업무 이력을 늘리지 않는다. 소유권 token은 DB에서 hash로 비교하고 원문을 로그에 남기지 않는다. request 원응답에 포함하는 token은 소유 세션의 재처리를 위해 보호된 사용자 저장소에 보존하며 로그·문서·다른 actor의 조회에 노출하지 않는다. competing claim의 revision/점유 충돌은 exit 3이다.

recover_claim은 메인이 종료 확인 결과 또는 격리 workspace 근거를 제공하고 P2가 현재 revision·이전 owner를 확인한 같은 트랜잭션에서 event·revision·token을 교체한다. 초기 구현은 운영 범위가 정해진 수동 복구만 허용한다.

finish는 모든 필수 criterion ID에 대해 성공 검증·증거 참조가 있고, criteria revision·정의·대상 지문·현재 적용 범위가 일치하는지 확인한다. P3는 검증 ID·outcome·criteria 포함 관계·scope/fingerprint·valid/stale 이유·증거 ID를, P4는 해당 artifact의 ready·존재·hash 상태를 제공한다. P2가 이 결과와 현재 revision을 확인한 뒤 반영한다. 서로 다른 기준·대상의 검증 ID를 연결해 완료할 수 없다.

## 검증 지문의 비교 단위

- 대상은 repo UUID + 등록한 verification scope다. 영향 범위가 정해지지 않으면 전체 repo로 비교한다.
- tracked 파일 내용·dirty 변경·관련 untracked 파일을 포함한다. ignored 파일도 테스트 입력·설정·생성물로 쓰면 포함한다. submodule이 대상이면 실제 revision·dirty 상태를 포함한다. 어떤 대상도 수집하지 못했으면 unknown으로 처리한다.
- 환경은 OS/architecture, Python·SQLite·검증 도구 버전, 의존성 lock 또는 실제 설치 manifest, 필요한 설정·입력 fixture·명령을 포함한다. lock 파일 부재 자체를 동일성 근거로 삼지 않는다. 비밀 설정은 공개하지 않고 의미 있는 비밀 제외 식별값을 사용한다.
- `fingerprint_version`과 canonical JSON/SHA-256 규칙을 고정한다. 대상 밖 절대 경로·시각·PID 차이는 내용 지문에 넣지 않는다. fixture·scope·설치 대상 시험의 package hash·제품 버전은 의미 입력으로 포함한다.
- 원 검증 outcome은 pass/fail/blocked/aborted다. pass에는 실제 명령 또는 확인 절차와 증거를 요구한다. 재사용 판정 valid/stale/unknown 및 reused 보고는 원 실행을 수정하거나 새 pass를 만들지 않는다.
- 이후 실패·철회는 최소 `definition ID + repo/scope ID`로 기존 후보에 연결한다. 그 scope에 관련된 이후 실패·조건 변경·증거 손실이 있으면 보수적으로 stale로 표시한다. 다른 scope의 실패를 영향 추론 없이 무조건 적용하지 않는다.
- 여기서 ‘이후’는 SQLite에 저장된 행 순서다. 시계가 뒤로 이동하거나 같은 발생 시각을 가진 실패도 앞서 저장된 성공을 무효화한다. 시각은 표시·감사용이며 최신성 판단의 권위가 아니다.
- 실행 전후 대상 상태를 비교하고, finish 시에도 현재 조건과 기준을 확인한다. 변경·수집 실패·조건 누락은 재사용 불가 이유다.

실행자는 검증 전에 `lookup_verification`으로 현재 `input_fingerprint`를 받고, 실제 명령을 실행한 뒤 `record_verification.payload.before_fingerprint`로 전달한다. PMT가 현재 조건을 다시 수집해 비교하며 pass에는 이 값이 필수다. 외부 실행의 진실성은 호출자와 보존된 증거를 기준으로 한다. PMT가 명령을 대신 실행하거나 임의 문자열 지문을 현재 조건의 권위로 받아들이지 않는다. 같은 직접 등록 scope·환경·작업 공간·입력·전체 기준이 일치하면 다른 Item이 기존 ID를 참조할 수 있고, 기준 subset의 여러 검증을 합쳐 완료한다. 현재 구현은 작업 공간의 절대 경로를 저장하지 않고 그 식별 hash에 묶어 다른 worktree로의 재사용을 보수적으로 거부한다.

## 리소스·백업

- staging → 파일 내용·hash 확인 → 정식 파일 배치 → DB 참조 commit 순서로 처리한다.
- DB는 ready 파일만 참조한다. 파일 배치 후 DB 실패로 남은 고아 파일은 diagnose/재조정에서 찾는다.
- hash가 같아도 범위·권한·보존 참조를 섞지 않는다. 데이터 root 밖 경로·링크를 따라 임의 파일을 가져오거나 삭제하지 않는다.
- 참고용 임시 자료가 증거가 되면 정식 리소스로 승격한다. 참조 중인 근거는 기한만으로 파기하지 않는다.
- 백업은 maintenance 상태에서 새 쓰기·GC를 막고 진행 중 짧은 쓰기를 정리한 후 SQLite backup과 리소스 manifest·파일을 묶는다. DB 트랜잭션을 파일 복사 전체에 걸쳐 유지하지 않는다.
- 복원은 격리 경로에서 스키마·ID·관계·hash를 검증한 뒤 전환한다. 기존 데이터를 자동 덮어쓰지 않는다.

maintenance 진입은 DB의 조건부 운영 상태로 원자적으로 확정한다. 모든 mutation·리소스 등록·참조 변경·GC는 같은 쓰기 트랜잭션에서 maintenance를 검사한다. 이미 진행 중인 짧은 쓰기는 bounded 대기 후 완료시키며 진입 실패 시 백업을 시작하지 않는다. maintenance 중 읽기는 허용하고 다른 쓰기는 retryable maintenance 오류로 반환한다.

백업 소유자만 내부 snapshot/manifest를 확정한다. 쓰기·GC 차단이 유지된 상태에서 DB backup, ready 참조 목록, 파일 복사·hash 검증까지 끝내고 해제한다. 중간 오류는 해당 백업을 invalid/incomplete로 기록한다. 프로세스 종료로 남은 maintenance도 자동 해제하지 않고 소유자 종료·불완전 백업 확인 후 명시 복구한다. 복원 대상은 비어 있는 별도 data root여야 한다.

staging과 정식 배치는 같은 데이터 파일 시스템에서 수행하고, 임의 외부 경로를 정식 참조로 삼지 않는다. 작업 범위에서 허용한 원본 파일은 복사·검증하여 반입한다. link/reparse point와 상대 경로 탈출을 검사한다. 동시 같은 hash 등록은 최초 파일을 덮어쓰지 않고 최종 hash와 DB 고유 식별을 재확인해 참조한다. 초기에는 실제 GC를 자동 실행하지 않으며 파일 배치·백업·복원·정리의 소유/운영 상태를 직렬화한다.

리소스 파일 작업의 시작·소유를 maintenance와 같은 운영 계약으로 등록한다. maintenance는 진행 중 파일 등록이 끝나거나 명시 실패한 뒤 진입하며, 진입 후 새 등록·정리의 파일 작업도 시작하지 않는다. 정식 배치는 기존 파일을 교체하지 않는 소유권 있는 게시로 수행하고, 충돌자는 소유자의 ready 상태와 hash를 확인한 뒤 재사용한다. 단순 경로 존재만으로 완성된 파일이라고 판단하지 않는다.

[SQLite backup API](https://docs.python.org/3.13/library/sqlite3.html#sqlite3.Connection.backup)를 사용하거나 쓰기가 중지된 일관된 상태에서 백업한다. 열린 WAL DB 파일 하나를 복사한 것을 완전한 백업으로 주장하지 않는다.
