# 3단계 구현 연결 규격

2026-10-02. [기능 명세](contracts.md)를 병렬 작업자가 연결하기 위한 **계획 규격**이다. 아래 이름은 논리 인터페이스이며 아직 제공되는 API/스키마가 아니다. 이 문서의 의미와 호출 경계는 메인이 소유한다.

## 기존 구현에서 이어받을 것

현재 본체는 Python ≥3.13·표준 라이브러리·SQLite schema 3, graph schema 1, protocol-v1 CLI다. 현재 `service.execute`·handlers는 Database/connection을 직접 사용하므로 완성된 LocalStore/HttpStore가 있다고 가정하지 않는다. F0에서 저장 포트를 먼저 감싸고 소비자를 점진적으로 연결한다.

graph schema 1의 `tree_kind=requirement/implementation`을 보존한다. 요구와 구현은 의미가 달라 각각 고정 ID를 갖는다. 두 트리는 해당 종류의 parent/refines 투영이고, 연관 graph는 같은 canonical node ID에 implements/depends_on/evidence 관계를 연결한다. 화면별 사본이나 한 노드의 필수 다중 tree membership은 만들지 않는다.

2단계의 UUID·canonical JSON/hash·요청 재처리·짧은 transaction·Scope claim·verification·리소스/journal·run 상태를 재사용한다. 새 상태/제약/인덱스가 필요한 경우 F0 저장 담당이 실제 schema와 이관을 제공하고 소비자는 승인된 경계를 사용한다.

## 공통 값 객체

| 객체 | 필수 의미·검증 |
|---|---|
| SourcePin | repository/project ID, 선택 ref·검토 commit 또는 명시 non-Git, graph schema/revision/hash, 검토한 dirty 지문/clean 상태. 미확인과 clean을 null 하나로 혼동하지 않음 |
| ChangeSet | request ID·기대 SourcePin·changes 배열·이유/근거·소유 범위. create/update/link/unlink/clear/retire 의미를 구분 |
| NodeRef | stable UUID 또는 해당 batch/context에 묶인 temp/alias. 생성 temp ID는 Python UUID 발급 후 한 번 매핑; F5 이전에는 canonical UUID로 동작 |
| IndexRef / GraphSlice | source hash/revision·인덱스 종류/버전·조회 기준·nodes/relations·경로·완전성·unknown/다음 cursor |
| ImpactSet | change/rule/source 버전, 표시/의미 영향, 문서 구간/Step/검증 후보·이유/경로·known/unknown·누락 범위 |
| SegmentManifest | document/segment 안정 ID, node/field/relation 의존·template version, 생성/수기 경계, 이전/생성 hash·SourcePin |
| ContextBundle | 역할·Task/Step·SourcePin·접근/점유 검증 참조, 필수/추가 항목, 예산 단위·크기/측정 수준, 생략·불완전 이유·cursor·alias map/version |
| ReuseDecision | 정의/key schema/조건 지문, reusable/active/miss/invalid, 근거/원 run·owner/현재 상태·새 claim receipt·판정 이유 |
| ToolObservation | 물리 실행/Step/run·실제 상태/exit·시간·정제 요약·근거 hash/ref·생략/조회 가능 범위. 모델 주장과 실측 구분 |
| ControlAction | wait/poll/local-cli/main-native-call/read-result/review-needed 등의 다음 행동, 원 handle/run·receipt·사유·허용 조건·명령/알림 nonce |
| BatchPlan / BatchBinding | batch·parent/child logical runs·각 Step/지시/criteria refs, 공유 context·충돌/의존·scope union·물리 handle/slot·개별 결과 mapping |
| StoreRequest / StoreReply | operation·protocol/request ID·검증된 호출 주체·namespace·expected revision·payload refs → 기존 envelope/안정 오류·재처리 결과·receipt |
| MeasurementManifest | 정의/기준·환경/모델/정책·실제/추정/미상·호출/규모/시간·품질/재작업·실측 근거·비교 가능 여부 |

필수 의미는 고정한다. 내부 함수/데이터 배치는 구현자가 선택하되 공개 타입·필드 제약·신규 오류/호환 변경은 소비자 착수 전에 메인이 확정한다. 역할 등급은 접근 권한이 아니며 UUID/alias만으로 권한을 만들지 않는다.

## 서비스 경계·생산자/소비자

| 경계 | 논리 호출 | 생산·소비 |
|---|---|---|
| SourceRepository | load_verified(pin, scope), apply_changes(change_set, owner) → 변경/기준 receipt | F1 생산, F2/F4 소비 |
| GraphProjection | query(pin, query), register_manifest(manifest), rebuild(pin) → IndexRef/GraphSlice | F2 생산, F3/F5/F4 소비 |
| ImpactService | calculate(change, slice, rules) → ImpactSet | F3 생산, F4/F5/F6 소비 |
| DocumentRenderer | prepare(pin, impact, template, manifest), publish(stage, expected_hashes, owner) → 생성/게시 receipt | F4 생산, F2에 manifest 등록·F10 확인 |
| ContextBuilder | build(task, role, pin, budget, access), detail(cursor), resume(task), resolve_alias(map_ref, alias) | F5 생산, F8/F9 소비 |
| ReuseCoordinator | resolve(key, conditions, access), record_result(original_run, evidence) → ReuseDecision | F6 생산, F8 소비 |
| ResultPresenter | compact(report, policy), detail(result_ref, range, access) → ToolObservation | F7 생산, F8/F10 소비 |
| ExecutionController | advance(run, context_ref, reuse_decision, observation) → ControlAction/notice | F8 생산, 실제 제품/메인·F9 소비 |
| BatchPlanner | prepare(step_refs, context, capability), bind(plan, actual_handle), collect(binding, results) | F9 생산, F8의 그룹 확장·F10 소비 |
| MeasurementRunner | capture_baseline(case, condition), compare(baseline, measured) → MeasurementManifest | F0/F10 생산, F11/F15 소비 |
| StoragePort | execute(operation, request), get_request_result(id), check_compatibility() → StoreReply | F0 로컬·F11 Host 구현, F12/F14 소비 |
| MigrationCoordinator | quiesce(namespace), stage_import(manifest), verify(stage), switch_primary(verified), restore(version_set) | F13 생산, F14/F15 소비 |

이 호출을 파일별 수정 절차로 인계하지 않는다. 기능은 승인된 값 객체와 서비스 경계로 연결하고, 상세 원문은 Ref로 읽는다. 의미가 같은 함수라도 제품별 DB 상태 판정을 복제하지 않는다.

## 선행 순환을 막는 연결 순서

1. F2는 node/relation 조회와 F0 형식의 manifest 등록/읽기부터 제공한다. 아직 manifest가 없는 문서 구간은 absent/unknown으로 표시한다.
2. F3는 확인된 관계로 후보를 계산하고 누락 의존을 별도로 반환한다. 누락을 의미상 영향 없음으로 만들지 않는다.
3. F4의 최초 전체 렌더가 baseline manifest를 만들고 F2에 등록한다. 이후 부분 생성의 의존 목록을 사용한다. F2↔F4 실제 연계는 F10에서 확인한다.
4. F1~F3는 F5 alias 없이 UUID로 완료한다. F5가 나중에 입력 alias를 검증·변환하고 기존 ID 경계로 전달한다.
5. F8은 단일 Step부터 제어한다. F9가 나중에 그룹/물리 handle binding을 확장한다. vendor batch API는 필수 전제가 아니다.

## 저장 포트와 Host 허용 범위

클라이언트에는 원본/Git·working tree 확인/편집·문서 게시·검증 전후 대상 지문 수집·모델/명령 실행이 남는다. Host 포트에는 공유 상태/Queue/점유·그룹 binding·결과/근거·권한 있는 자료/파생 snapshot 조회와 재처리만 허용한다. 현재 dispatcher 전체를 원격으로 노출하지 않는다.

단일 작업 변경은 짧은 DB transaction 안의 상태·revision·업무 이력·재처리 결과로 확정한다. Git/파일 게시·resource upload·원격 반영은 operation journal 단계로 연결한다. 원본/기준이 바뀌면 해당 effect를 재검토하며 무조건 재실행하지 않는다.

Host 검증 지문은 인증된 클라이언트의 실제 작업 환경에서 수집한 전후 자료·기준·근거와 연결한다. Host의 Python/경로/환경으로 대체하지 않는다. 기기 인증은 주체를 확인하며, 그 자체가 모델의 성공 주장이나 시험의 진실성을 증명하지는 않는다.

HTTP API version과 CLI protocol version은 별개다. 기존 envelope·안정 error code·request fingerprint/replay 의미를 보존하고, HTTP의 input/auth/scope/conflict/transient 오류를 기존 CLI 의미로 변환한다. device/session/namespace 권한은 서버에서 확인하고 credential/claim token 원문은 일반 로그·metadata에 넣지 않는다.

## 그룹·재사용·충돌 처리

- 재사용 lookup와 새 조사 claim은 동일 ReuseKey에 대해 원자적으로 조정한다. 실행한 모델은 provenance이며 모델이 검증 대상일 때만 key의 필수 조건이다. 기존 사용자 event는 별개로 보존한다.
- batch의 scope union은 한 번에 확보하고 child run의 접근은 명시 parent/child binding으로 검증한다. 다른 실행의 소유권을 같은 session이라는 이유로 우회하지 않는다.
- 물리 handle 하나는 실행 slot 하나다. child run마다 지시 버전·결과·검증 상태를 유지하고, 누락 결과/취소/불명확한 handle은 개별 성공으로 추측하지 않는다.
- source/version/owner 충돌, 필요한 문맥 부족, 근거 무효, 실행 unknown, transient I/O, 미지원 capability를 구별한다. 반복 관찰·시간 초과만으로 점유 회수·다른 runner 실행을 하지 않는다.
- 텍스트/byte 상한으로 버린 원본 구간에는 상세 조회가 가능하다고 표시하지 않는다. 보존한 chunk/hash와 조회 범위, 복구 불가 생략 구간을 함께 반환한다.

이 규격의 실제 schema/operation/제품 capability·시험 결과는 구현 착수/실측 후 별도 기록한다. 계획만으로 호환이나 설치 성공을 주장하지 않는다.
