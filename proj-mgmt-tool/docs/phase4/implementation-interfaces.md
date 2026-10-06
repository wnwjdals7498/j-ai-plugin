# 4단계 병렬 구현 연결 규격

2026-10-06. 상태: **메인 연결 설계안**. [공통 계약](contracts.md)의 의미를 실제 작업자가 인계하기 위한 입력·출력·권한·재처리 경계다. 아래 호출은 논리 포트이며 현재 CLI/HTTP API로 제공되지 않는다. R0에서 실제 함수·operation·저장 구조·version을 확정하고 그 계약을 소비자가 공유한다.

## 1. 기술·연결 방향

`제품/스킬 → 재개 조정 서비스 → 기존 업무 서비스 + 변경/문맥 서비스 → LocalStore 또는 인증 HttpStore` 방향을 유지한다. Host에는 허가된 저장 metadata와 파생 조회만 보낸다. Git·working tree·실제 파일 게시·모델/실행기·local spool은 클라이언트에 남는다.

- Python 표준 라이브러리와 SQLite, 기존 canonical JSON/hash·UUID·짧은 transaction·request cache·resource/journal을 우선한다.
- source 권위·점유·업무 상태·검증 판정을 새 모듈에서 복제하지 않는다. 필요한 변경은 기존 서비스의 trusted adapter로 연결한다.
- graph schema 1과 public CLI protocol-v1/현재 Host envelope를 유지할 수 있는지 먼저 확인한다. 새로운 schema/API 번호는 DDL·이관·백업·구버전 입력 검증이 준비된 뒤 확정한다.
- 메인이 공통 registry·scope rules·DTO·storage revision 의미를 소유한다. 작업자는 소비자 수정 전에 변경 이유·호환·부정 시험을 인계한다.

## 2. 현재 코드에서 먼저 확인할 연결 공백

| 공백 | 확인한 근거 | 구현 전에 고정할 처리 |
|---|---|---|
| 공유 checkpoint와 session-bound storage | 현재 `Phase3Storage.get_object/put_object`는 scope와 owner_actor/owner_session을 함께 검사 | 허가된 공유 metadata는 namespace/project 저장 adapter가 접근 권한을 먼저 확인하고 관리 주체로 저장·조회; original actor/session은 audit 참조. private 문맥·지시는 owner-bound 유지 |
| 코드/문서 변화 지문 | 기존 SourcePin의 graph hash/dirty 검사는 graph 경로 중심 | 별도 relevant source inventory ref/hash와 coverage를 BasisVector에 연결. 전체 code 검증을 완료한 지문으로 오해하지 않음 |
| Host SessionStart 조회 identity | 현재 Hook 조회는 `actor=hook`의 `read_context`, hosted actor 변환은 normalized `record_event`에 제한 | 등록 profile principal에 맞춘 제한된 Hook metadata read 경계. scope 설정·현재 auth·provenance 검증 유지; `hook` 문자열을 인증 예외로 허용하지 않음 |
| Git baseline hosted 연결 | 현재 Host allowlist에 `sync_project_baseline/read_project_baseline`이 없음 | client의 현재 점유/mapping 아래 Git 수집 후 허가된 change/basis/alignment receipt를 Host에 저장 |
| 임의 code diff의 의미 | F3는 typed graph ChangeSet/preview를 실제 검증 | code/기능/요구 연결 후보를 R2에서 수집하고 의미 판단·검토된 graph delta를 R3에서 연결 |
| 제품 출력 동등성 | 현재 native SessionStart 추가 문맥은 Codex/Claude만 지원 | 각 adapter의 actual 입력/반환 형식과 명시 조회 면을 별도로 확인 |

위 표는 새 코드를 구현한 결과가 아니다. 메인이 R0 검토 후 소비자 작업의 실제 선행을 확정한다.

## 3. 공통 입력과 응답

| 값 | 의미와 제약 |
|---|---|
| RequestIdentity | 원 request/event/correlation ID, 요청 actor/session과 실제 transport principal. 등록 device/environment/namespace를 wire의 caller 주장으로 대체하지 않음 |
| ScopeSelection | namespace/repository/project·canonical workspace/branch, 선택 Work/Item/Step/run refs. 명시 scope와 task ancestry를 검증; 불명확하면 내용 읽기 없이 selection 필요 |
| MetadataAccess | 현재 auth/permission/scope 확인의 ref·revision. 목록·checkpoint/current source metadata 조회를 뜻하며 실행 grant가 아님 |
| WorkAccess | actual current run/claim·scope union·owner·revision·current source/mapping 확인 ref. source/지시/working tree 내용 읽기 전 요구 |
| BasisRef | 버전형 BasisVector의 immutable ref/hash, component capture refs와 coherence 결과. 단일 Boolean verified로 대체 불가 |
| ObjectRef | kind/id/scope/revision 또는 immutable content hash·schema/version·visibility·provenance. lookup하는 실제 객체와 비교 |
| RevisionExpectation | mutable pointer/업무 record/원본 별도의 기대 revision·hash. 신규 pointer 0을 쓰는 경우 payload의 해당 기대값으로 명시하며 기존 최상위 양수 expected_revision과 혼동하지 않음 |
| BudgetPolicy | 응답 전체 UTF-8 bytes/lines·optional detail/page limit·role/policy version. 실제 tokenizer/usage 없으면 token unknown |
| Outcome | success/error envelope와 실제 currentness/complete/unknown/attention·reason·적용 receipt. action의 제안과 실행 결과를 구별 |

private body·credential/env/command·PID·절대경로는 shared DTO에 넣지 않는다. 경로가 필요한 실제 검사/게시 함수에는 승인된 client mapping으로만 전달한다. 일반 DTO에는 workspace-relative resource와 content-addressed refs를 사용한다.

## 4. 포트별 의미적 인수인계

### 4.1 현재 사실과 기준 수집 — R1 → R2/R3/R4

| 호출 | 입력 의미 | 출력 의미·실패 |
|---|---|---|
| ReadCurrentFacts | ScopeSelection, 현재 MetadataAccess, 선택 상태/결정/실행 범위, 예산 | 현재 계약/방향 refs·관찰 수준·업무 capture ref·기존 실행/pending·unavailable. 작업 source/private 지시를 직접 읽지 않음 |
| CaptureWorkBasis | WorkAccess, 명시 repository/branch mapping, 관련 문서/graph/code inventory 선택 | 실제 client source component refs와 현재 DB 관련 revision, BasisRef·coverage·coherence. source/owner 변화면 stale/incomplete |
| ValidateBasis | BasisRef, 필요한 현재 접근 면과 component 선택 | unchanged/changed/unknown, 변경한 component·이유·재수집 조건. 전체 조건 검사로 확대하지 않음 |

SourcePin·관련 코드 inventory·문서 segment·업무 capture를 각각 관찰하고 조합 전후 비교한다. SQLite snapshot이 파일/Git까지 잠갔다고 주장하지 않는다. 새 환경의 실제 지문이 없으면 환경 유효성은 unknown이다.

### 4.2 체크포인트 — R1/R5 → R2/R4/R6

| 호출 | 입력 의미 | 출력 의미·실패 |
|---|---|---|
| CreateCheckpoint | 실제 확정 boundary event ref, BasisRef, 현재 facts/decision/run/evidence refs, purpose, 기대 latest pointer revision | immutable CheckpointRef와 current pointer CAS receipt; duplicate event/body는 같은 ref, 다른 body는 conflict |
| ReadCheckpoint | 명시 selected pointer 또는 immutable ref, MetadataAccess, 예산 | 현재 pointer/ref·checkpoint의 permitted metadata·basis version·missing. private 지시·실행 grant 제외 |
| LinkSession | native stable session identity, selected project/task, checkpoint/receipt 참조, current metadata access | 감사용 SessionLinkRef·조회 outcome. 원 owner/lease/handle을 바꾸지 않음 |

shared latest pointer의 key는 namespace/project/repository·branch/workspace·선택 task·checkpoint purpose를 분리한다. environment 의존 관찰은 그 dimension을 보존한다. 현재 방향의 동일 원본 참조와 장치별 관찰/근거를 합쳐 덮지 않는다. 서로 다른 branch/task 체크포인트의 최신 시각만 비교해 자동 선택하지 않는다.

### 4.3 실제 변화와 구현 연결 — R2 → R3

| 호출 | 입력 의미 | 출력 의미·실패 |
|---|---|---|
| CollectChanges | WorkAccess, previous analyzed/applied BasisRef, current capture, 수집 종류/범위 | 실제 before/after change refs/hash·commit/dirty 범위·의도 근거 후보·coverage; history/owner 분기면 확인 필요 |
| BuildImplementationLinks | current source refs, 검토된 기능/모듈/경로 계약 refs, 추출기/version/선택 범위 | source-bound LinkIndexRef·각 연결의 출처/확인 수준·unmapped/dynamic 범위. 무조건 graph schema에 새 relation을 넣지 않음 |
| ReadChangeSlice | retained Change/IndexRef, MetadataAccess 또는 필요한 WorkAccess, source/query-bound cursor·범위 | 실제 보존된 해당 변화/연결과 상세/누락. stale/권한 부족/폐기된 data는 거절 |
| RegisterObservedChange | client 실제 수집 refs·BasisRef·source metadata·request ID, current scope 권한 | Host 또는 local의 immutable change receipt. 수집 범위/주체의 provenance이며 Host가 Git을 검사했다는 뜻 아님 |

미지원 parser·추적하지 못한 rename·수집 중 dirty 변화·외부 provider가 주장한 결과는 unknown을 유지한다. 수집 내용이 같아도 별개 사용자 사건의 event ID는 보존한다.

### 4.4 적용성·의미 판단·반영 — R3 → R4/R5/R6

| 호출 | 입력 의미 | 출력 의미·실패 |
|---|---|---|
| AssessAlignment | actual Change/LinkIndex/BasisRef, 현재 계약/결정/위임 refs, evidence definition·현재 선택 조건 | impact/unknown·근거 적용성·정렬 필요·행동 후보·이유/refs. 모델 해석은 제안으로 표시 |
| ProposeSemanticResolution | 메인 역할에 전달할 제한된 영향/계약/근거 bundle, user 위임 범위 | 대안·선정 이유·확인 필요 또는 위임 범위의 변경 방법. provider 직접 호출 면을 새로 만들지 않음 |
| ApplyAlignment | 검토된 Assessment/Resolution refs/hash, user 선택 또는 실제 위임 ref, WorkAccess, 기대 source/업무/plan/pointer 기준 | 원 작업에 연결된 AlignmentReceipt·before/after refs와 상태/unknown. 실제 게시/반영 성공 전 applied 기준 전진 금지 |
| ReadApplicability | 기존 원리/검증/definition/evidence refs, 현재 selected condition refs와 접근 | applicable/not_applicable/unknown와 정확한 변화/후속 실패/손상 이유. 기존 시험 outcome의 수정이 아님 |

실행 중 지시/대전제 변경은 기존 execution cancel/reconcile·실제 stop·stale 결과 규칙을 사용한다. DB·문서/graph 게시를 journal로 연결하고 current auth를 write transaction 안에서 replay보다 먼저 확인한다. 반영 항목의 일부만 완료됐으면 완료한 effects와 미해결 effects를 따로 표시한다.

### 4.5 재개 개요와 작업 문맥 — R4 → R5/main/runner

| 호출 | 입력 의미 | 출력 의미·실패 |
|---|---|---|
| ComposeResumeOverview | 명시 project/task 선택, MetadataAccess, checkpoint/current facts refs, role·budget | 방향/현황/실행/pending refs·basis freshness·후보 work/attention/필요 조회. private 본문과 source 읽기 제외 |
| ComposeTaskResume | actual WorkAccess, fresh coherent BasisRef, 검토된 alignment/applicability refs, current Step/directive/criteria refs, role·budget | 현재 F5 연결 문맥·관련 변화·허용 방법·금지·근거·조건부 action·retained detail refs. 과거 aliases는 재활성화하지 않음 |
| ReadResumeDetail | current permitted BundleRef·scope/source-bound cursor·range·budget | 접근 가능한 보존 상세 slice·실제 범위/hash·새 cursor. discarded data를 재구성했다고 주장하지 않음 |
| ProposeNextAction | 현재 facts/basis/attention·권한·원 실행/receipt 정보와 policy | 읽기/대기/조정/검토/확정 Step 착수의 선택과 preconditions. 실제 행동 전에 기존 authoritative 서비스 재검증 |

권고 action의 currentness는 cache 시간만으로 판정하지 않는다. 현재 source/권한/업무 revision이 달라지면 action은 다시 계산하거나 거절한다. mandatory budget 초과는 `incomplete`이며 실행 준비 완료와 구별한다. 개요를 읽을 수 있다고 해당 Work 내용을 점유했다고 기록하지 않는다.

### 4.6 제품·Host·관찰 — R5/R6 공통

| 연결 | 입력 → 출력 | 실패/제한 |
|---|---|---|
| SessionAdapter | native stable event/session·명시 profile/scope·bounded overview → 제품 native 추가 문맥 또는 명시 조회 안내 | Hook timeout/미설치/미지원은 안전한 오류/추가 조회; 긴 분석을 Hook 안에서 수행하지 않음 |
| BoundaryRecorder | 실제 user 선택/착수/검토/반영 receipt와 checkpoint 조건 → 공통 저장 요청 | raw Hook event만으로 업무 boundary 승격 금지, 누락/중복/역순 재처리 |
| HostContinuityStore | current principal·scope·기대 pointer/revision·원 FP·retained source refs → permitted metadata/receipt | 일반 dispatcher·임의 명령·Git/client path·모델 설정 공개 금지 |
| MeasurementRecorder | scenario/정의·조건 지문·actual observation → evidence manifest·비교 가능/불가 이유 | missing usage unknown, 모델 주장으로 시스템/성능 pass 없음 |

## 5. 재처리·동시성·실패 인계

- 모든 mutable effect는 원 request ID/body semantic fingerprint·주체·scope·기대 revision을 유지한다. response unknown일 때는 원 결과 조회가 먼저다.
- checkpoint 본문과 latest pointer·업무 event/request receipt는 가능한 짧은 동일 DB transaction으로 확정한다. 외부 resource/문서/Git 효과는 separate journal에서 원 요청과 단계/hash로 잇는다.
- 동일 작업/겹치는 범위는 기존 Step/claim/scope union으로 직렬화한다. Role나 SessionLink·summary freshness를 owner 변경 조건으로 쓰지 않는다.
- private state와 공개 metadata의 조회·history·cache visibility를 분리한다. 공유 metadata는 current scope grant 아래 읽되 raw private original response/지시는 기존 current run owner 조건을 유지한다.
- Root가 기존 generic storage를 재사용한다면 namespace 관리 주체를 adapter 안에서만 선택한다. caller가 system owner/namespace 문자열을 지정해 우회하는 wire 필드를 만들지 않는다.
- 이관/보존/백업에는 미해결 checkpoint pointer·alignment effects·pending·원본 refs의 실제 보호 검사를 넣는다. 미래 DDL에만 보호 규칙을 적고 현재 exporter가 누락된 채 성공으로 표시하지 않는다.
- core exit는 0~5, HTTP와 분리한다. 새로운 logical attention 상태는 existing run/Step 상태의 대체가 아니다.

## 6. 소비자 착수용 계약 관문

| 인계 묶음 | 필요한 산출물 | 실제 검증과 다음 착수 |
|---|---|---|
| D0 공유 기반 | BasisVector/ref visibility·checkpoint pointer/CAS·포트 read/write·오류·schema/registry 계획 | R0 P4-R0-01~03; A/B/C가 같은 fixture/DTO 소비 |
| D1 현재 기준 | actual CurrentFacts/Basis/Checkpoint 읽기/쓰기와 원 event 재처리 | R1 P4-R1-01~03; R2 real basis 연결/R4 current facts 연결 |
| D2 실제 변경 | actual Change/LinkIndex coverage·before/after와 reader | R2 P4-R2-01~03; R3 의미/적용성 판단 |
| D3 반영 | actual assessment/applicability·검토된 receipt·applied 기준 CAS | R3 P4-R3-01~03; R4/R5의 현재 next action/업무 boundary |
| D4 재개 | overview와 actual owner-bound task bundle/detail/action 조건 | R4 P4-R4-01~03; 제품·fresh native session 판단 |
| D5 제품/Host | 실제 allowed endpoint/identity와 native/명시 조회·복구, local/hosted parity | R5/R6 해당 IDs; 최종 actual 통합·측정·package |

값·함수 이름을 작업자가 임의로 대체해 맞춘 결과를 관문 성공으로 삼지 않는다. 인터페이스 변경 제안은 producer/consumer·호환·재처리·권한·필수 시험 영향과 함께 메인에 인계한다.
