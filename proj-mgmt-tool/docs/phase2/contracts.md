# 2단계 공통 구현 계약 — Q0

기존 [CLI·상태 계약](../phase1/contracts.md)과 [구현 인터페이스](../phase1/implementation-interfaces.md)를 확장한다. 아래는 병렬 구현의 기준이며 아직 제공되는 API 목록이 아니다. 명칭·형식을 변경하면 소비자·시험·이관을 함께 갱신한다.

## Q0 작업 범위

- **목적:** 작업자가 서로 다른 실행·점유·완료 의미를 구현하지 않도록 공통 기준을 만든다.
- **추가:** 두 트리/Step/job/run/범위 점유/runner/진행의 값 객체·오류·버전 계약, capability 확인·수용 시험 기준.
- **수정:** 기존 상태·검증·리소스 계약의 Step 적용 범위와 이관 규칙. **삭제:** 승인 근거 없는 중복 상태 원본·묵시적 성공 판단을 새 설계에서 제거; 기존 사용자 자료 삭제는 없음.
- **goal:** 모든 소비자의 input/output·소유자·선행·오류·시험 연결이 확정된다. **non-goal:** 특정 파일 수정 목록, 모든 제품의 지원 능력 추정, Host 구현.
- **input:** 사용자 합의, 기존 코드/계약/실측 기록, 제품별 공식 규약과 실제 확인 결과. **output:** 아래 계약·작업 배정·지원/미확인 목록·시험 정의 버전.
- **기술:** 기존 Python/SQLite/JSON 재사용. 새로운 의존성은 필요성이 입증된 연결부에 한정한다.
- **Test/로그:** Q0-CONTRACT 검토에서 필드 의미·전이·오류·소유·시험의 정합성을 확인한다. 결정/변경 이유·근거 참조·계약 버전을 남긴다. 문서 확인을 제품 실행 성공으로 기록하지 않는다.

## 공통 타입·원본

| 값 | 의미·제약 |
|---|---|
| Project/Work/Item/Step ID | 고정 UUID; 부모 종류·프로젝트 범위를 검증 |
| request_id / source event ID | 호출 재전송 / 원 행동 식별. 서로 대체하지 않음 |
| job_id / run_id / parent_run_id | 논리 요청 / 시도 / 호출 계보. 재시도는 새 run |
| requirements_version / plan_version / directive_version | 각각 요구·방법·실행 지시 의미 버전; 업무 revision과 구분 |
| baseline / workspace_fingerprint | 확인한 Git 기준 / 현재 commit·dirty·설정·대상 지문 |
| context_ref / instruction_ref | ID·버전·hash·용도의 참조; 본문 중복 저장 금지 |
| allowed_scope | 프로젝트/작업 공간 + 변경할 기능/논리 리소스·경로 경계; 허용 도구/권한과 별도 |
| capability_snapshot | agent/provider/model·실행 경로·도구·권한·동시성·취소/조회 지원·확인 시각/근거 |
| CriterionResult | 기준 ID·pass/fail/blocked/not_run·실측/재사용 구분·증거/검증 ID·불충족 이유 |

Git 문서/graph가 구조·대전제·기준 결정의 원본, SQLite가 실행 상태/Queue/점유의 원본, 내부 리소스가 Step 지시 원문의 원본이다. graph 초안은 내부 리소스에 보관하며 확정 graph의 독립 DB 편집본은 만들지 않는다.

graph schema 초안은 UTF-8 JSON `schema_version/project_id/graph_version/nodes/relations/provenance`다. 각 node에 ID·tree/node kind·summary·premises/method refs·제품 범위·종료 이유/위임 범위·Work/Item/Step refs를 둔다. 관계는 parent/refines/implements/depends_on/evidence를 구분한다. 선택·도구·권한 등 세부 구조는 이 의미를 유지해 구현자가 결정한다.

구현 Step은 확정 구현 계획이 필요하다. 트리 확정에 필요한 조사·실험은 승인된 탐색 목표·범위·판정 기준·권한·초안 버전으로 Step을 생성할 수 있다. 그 결과는 선택/계획의 입력이며 프로젝트 구현 완료로 집계하지 않는다. Q2는 이 경계를 공통 계약으로 소비하고 Q9에서 실제 Queue/runner를 연결해 확인한다.

## 호출 경계와 payload

기존 `protocol_version=1` 응답 envelope·종료 코드·1MiB 입력 제한을 유지한다. 2단계 정보는 검증된 동작별 `payload`/참조로 전달한다. 공개 operation은 아래 기능군을 구분해 Q0에서 명명하며, 대용량 원문은 리소스 참조다. 기존 동작의 payload를 바꿀 경우 구버전 입력 호환 시험을 포함한다.

| 기능군 | input → output | 변경 판정 |
|---|---|---|
| 계획 작성/검증/게시/조회 | 요구·결정·자원·초안 버전 → validator 결과·확정 문서/graph refs | 작성과 게시 분리; 두 트리 종료·근거 확인 후 게시 |
| Step 지시 확정/조회/검토 | Item·방법·기준·권한·버전 → 불변 지시 ref·기준별 판단 | 지시 원문은 runner 전달 경계에서 읽음; 관리 조회 제외 |
| 설정/capability/선정 | 정책·실제 가용 정보·역할/작업 필요 → 선택·이유·policy 버전 | 확인되지 않은 기능은 지원으로 승격하지 않음 |
| 실행 등록/준비 | Step ref·의존성·범위·정책 → job/run·점유·실행 intent 또는 대기 이유 | 활성 Step·범위 충돌·지시 버전 검증 |
| 실행 receipt/관찰/결과 | run·실행 handle·실측 관찰/결과 → 현재 상태·저장 receipt | run 전용 변경 버전으로 CAS; 중복 의미 입력 허용 |
| 취소/복구/검토 | run·중단/조회 증거·기준별 검토 → 상태·점유 처리·다음 조치 | 종료/미시작 확인 없이 해제·재배정 금지 |
| Git 반영/진행/정리 | 기준·현재 대상·확인 정책 → 영향/기준·진행·정리 후보/결과 | 반영 완료 후 기준 갱신, 보존 예외 적용 |

전체 operation 이름·필수 payload·오류 코드는 Q0 구현 착수 시 소비자가 공유하는 인터페이스 산출물로 고정한다. 내부 함수 이름을 작업자가 정할 수 있어도 동일 의미를 별도 envelope/트랜잭션으로 복제하지 않는다.

## runner와 네이티브 호출

`start(ExecutionSpec) → LaunchReceipt`, `status(handle) → Observation`, `result(handle) → ExecutionResult`, `cancel(handle) → CancelReceipt`가 공통 경계다. 값에는 run ID·실제 경로/모델·지원 여부·native handle·확인 시각·근거 참조가 포함된다. 취소 요청 receipt와 종료 확인은 별도다.

native 경로에서는 Python 본체가 메인 세션의 도구를 직접 소유하지 않는다. PMT가 영속 실행 intent를 반환 → 메인이 지정된 네이티브 도구 호출 → handle receipt 저장 → 관찰/반환 영속화 순서다. 직접 제어 가능한 CLI/SDK/API runner는 동일 의미를 실행 연결부에서 수행한다. 훅은 이벤트 정규화·문맥/관찰 수집이며 명시 실행 지시를 대신하지 않는다.

handle 저장 전 중단 가능성을 고려해 run 식별자/호출 계보로 원 실행 조회를 시도한다. 조회 기능이 없으면 `reconciling/blocked`로 보고하고 종료/미시작 증거를 얻기 전 대체 실행하지 않는다. native tool의 exactly-once를 보장한다고 주장하지 않는다.

## 상태·순서·완료

- Step 업무 상태는 기존 Planned/InProgress/Paused/Blocked/Done/Canceled 의미를 확장한다. job 대기와 run 상태는 별도이며 Item/Work 완료는 별도 판단이다.
- run 정상 흐름은 queued → starting → running → review_pending → succeeded. blocked/failed, cancel_requested → canceled, 시작/종료 불명 reconciling을 구별한다. 종료된 run의 결과는 불변이며 새 시도는 새 run이다.
- 결과가 상태 관찰보다 먼저 와도 유효한 run 결과를 보존·검토 대기로 처리한다. 순서가 늦은 running 관찰이 종료 상태를 되돌리지 못한다. 재전송과 다른 결과 충돌을 구별한다.
- 같은 Step의 겹치는 활성 실행을 금지한다. `prepare`는 의존성·용량·버전·범위 점유와 starting 기록을 짧은 DB 트랜잭션으로 확정한다. Git 최신화/실행 준비가 완료된 후 외부 호출한다.
- 실제 작업 내용 읽기/쓰기 전에 점유 성공 응답이 필요하다. 사전 배정은 ID·정적 필요 능력·선언된 범위 등 최소 메타데이터만 사용한다. 점유 후 범위 추가가 필요하면 원자적으로 확장하거나 안전하게 중단·재준비하며 새 영역부터 읽지 않는다.
- 다중 scope 전부 확보 또는 전부 취소, 논리 리소스·경로 겹침 검사, 부모 집계와 자식 소유권 분리, 시간만으로 회수 금지.
- transient 오류이고 이전 실행이 미시작/종료 확인된 경우 최대 2회 재시도. 시간/무응답 한도는 관찰·중단 판단의 입력이며 종료 근거가 아니다.
- 지시/요구/계획이 바뀐 반환은 이전 버전의 사실로 보존하되 현재 완료에 사용하지 않는다. 늦은 결과·취소 후 결과도 소실시키지 않는다.
- Step 실행 기준 → Item 요구 → Work 통합 기준 순서로 검토한다. 명시 기준별 검증·유효 증거가 필요하며 모든 자식 Done만으로 완료시키지 않는다.

## 오류·부작용 경계

오류 종류는 입력/지원 밖, revision·소유·버전 충돌, 일시 I/O/DB busy, 실행 불명, 기준/증거 미충족, 내부 오류다. 기존 CLI 종료 코드에 매핑하고 details에는 안전한 추적값·다음 행동만 둔다.

DB 상태·업무 이벤트·요청 성공 재처리는 같은 트랜잭션이다. 외부 실행·Git/파일 게시·파일 파기는 별도 intent/journal과 단계별 receipt를 둔다. 부분 실패를 진단·재개하며 DB와 외부 효과를 한 원자 작업으로 표현하지 않는다. 진단 쓰기 실패는 warnings/fallback, 업무 이력 실패는 rollback, 필수 증거 실패는 성공 판정 금지다.
