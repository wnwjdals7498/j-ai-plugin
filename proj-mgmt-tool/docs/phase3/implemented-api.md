# 3단계 구현 API

현재 공개 연결은 CLI protocol-v1과 `LocalStore`/인증 `HttpStore`다. 최종 검증 범위는 [구현 상태](implementation-status.md)를 따른다. 이 문서의 로컬 기능명은 Host 허용 목록이 아니다. 실제 저장 endpoint·권한·source/resource 계약은 [Host 연결](host-api-contract.md)을 따른다.

## 요청·응답

요청은 `protocol_version`, UUID `request_id`, `operation`, `actor`, `session_id`, 객체 `payload`를 받는다. 해당 작업의 `scope_id`·`record_id`·`expected_revision`은 최상위 필드다. 같은 request ID와 같은 입력은 재처리 결과를 반환하고 다른 입력은 충돌한다. event ID는 별도 사용자 행동을 식별한다.

응답은 `ok`, `result`, `error`, `warnings`를 포함한다. 오류나 unknown 결과를 완료·검증 성공으로 바꾸지 않는다. 추가 상세 조회는 실제로 보존된 리소스 범위만 제공한다.

## 로컬 기능

| 기능 | operation | 입력·결과의 핵심 |
|---|---|---|
| 원본 확인 | `capture_source_pin` | 프로젝트·저장소·점유 run·workspace·graph 경로 → 실제 Git/graph/dirty 지문 |
| 정형 변경 | `preview_graph_change`, `apply_graph_change`, `recover_graph_change` | 기대 SourcePin·동일 ChangeSet → delta·ID 매핑·게시/복구 receipt |
| 조회·인덱스 | `query_graph`, `rebuild_graph_index` | 현재 원본·조회 범위·cursor → source에 묶인 부분 graph·완전성 |
| 영향 | `calculate_graph_impact` | 변경 전 source·preview·원 ChangeSet → 재검증한 문서/Step/검증 영향·unknown |
| 문서 | `prepare_document_segments`, `publish_document_segments`, `recover_document_segments` | 현재 source·검증된 영향·apply receipt → 생성 구간 delta·게시/복구 참조 |
| 구간 등록 | `register_segment_manifest` | 구간·의존 필드·hash·source·선택적 coverage certificate → CAS 등록 |
| 문맥 | `build_task_context`, `read_task_context`, `read_context_detail` | 현재 Step/run·역할·예산 → 검증된 projection·생략/불완전성·보존 상세 |
| alias·재개 | `resolve_context_alias`, `resume_task_context` | 원 context/map/source 또는 새 세션의 현재 권한 → UUID 또는 새 문맥 |
| 재사용 | `resolve_reuse`, `record_reuse_result`, `invalidate_reuse`, `read_reuse_decision` | 정의가 선택한 실제 조건·근거 → 재사용/진행 중/신규 점유/무효 참조 |
| 도구 결과 | `compact_tool_result`, `read_tool_result_detail` | 실제 run 결과·정제 정책 → 짧은 관찰·리소스 hash·실제 상세 범위 |
| 실행 제어 | `advance_execution_control`, `read_execution_control`, `acknowledge_execution_action` | 현재 run·문맥/재사용 refs·실제 실행 handle → 다음 행동·통지 nonce·확인 상태 |
| 묶음 | `prepare_step_batch`, `bind_step_batch`, `collect_step_batch` | Step별 고정 지시/기준·검증된 실행 능력 → 배정/분리·binding·개별 결과 |
| 측정 | `capture_measurement`, `compare_measurements` | 같은 목표/수용/원본/환경/역할/정책/정의 → 실제·추정·미상 구분 비교 |

묶음 기능의 실제 연결 상태는 구현 상태에서 확인한다. 기능 등록만으로 실행 가능성을 주장하지 않는다.

## 저장 선택·Host

`pmt storage configure|probe|status`는 하나의 UTF-8 JSON 입력/응답을 사용한다. configure는 `mode`, 기대 config hash, 선택적 Host endpoint/credential 환경변수 이름/device/namespace/CA와 workspace mapping을 검증한다. 기기별 environment UUID는 유지하며 secret 값은 기록하지 않는다. hosted 선택 뒤에는 일반 저장 operation을 같은 namespace에 보내고 로컬 실행 operation은 현재 Host run/source를 확인하는 client adapter에 보낸다. 연결 실패는 local primary를 생성하지 않는다.

Host 전용 저장 RPC는 source publish/read·workspace authorize, verification snapshot publish, private control CAS/read·batch metadata read다. graph/verification bytes는 hash-bound immutable resource로 업로드하며 Git·모델·프로세스 실행 endpoint는 없다. HttpStore는 resource·transfer import/backup/download도 제공한다.

새 hosted 프로젝트의 구현 Step은 F4 완료 후 `publish_client_plan`으로 완전한 graph·coverage·현재 source를 확인한 plan metadata를 먼저 게시한다. `read_client_plan`은 버전/hash/참조만 읽는다. Host의 기존 Step·Queue 검증이 이 published plan을 사용한다. 모델 정책·가용 능력·`select_execution_route` 다섯 operation은 ConfigRoot의 독립 client settings 포트에 남고, Host에는 `route_for_enqueue`로 추린 실행 식별값만 전달한다. 이전 local 정책은 원 DB를 읽기만 하여 보존하고 환경에 묶인 가용성은 재관찰한다.

`MigrationCoordinator`는 quiescent sanitized backup·검증·빈 target import/restore를 제공한다. `PendingOutbox`는 이미 종료한 결과·resource bytes의 durable 보관과 동일 요청 조회/재전송을 제공한다. 이는 새로운 offline 점유/구현을 허용하는 저장소가 아니다. 상세 증거·사용 계약은 [이관](evidence/2026-10-02/migration/README.md), [전송](evidence/2026-10-02/transfer/README.md), [pending](evidence/2026-10-02/pending/README.md)을 따른다.

`pmt pending status|capture|reconcile`은 현재 hosted profile과 기존 native session을 사용한다. capture는 기존 run/context/source/dispatch ref로 own spool의 실제 종료 bytes만 보관한다. status는 owner별 결과/resource metadata를 반환한다. reconcile은 fresh TLS/auth/source/run 확인 및 원 요청 조회 후 반영한다. `session_id`, 선택적 `request_id`와 capture/reconcile의 `scope_id/run_id/context_ref/source_hash/dispatch_ref`를 입력하며 원문 결과·credential·임의 파일 경로는 받지 않는다.

## 적용 순서

1. 현재 run의 범위를 점유한 뒤 source를 확인하고 인덱스를 재구축한다.
2. 변경 전 원본에서 preview와 영향 계산을 수행한다. apply에는 동일 ChangeSet을 전달한다.
3. apply 후 source를 다시 확인한다. 부분 문서 생성은 이전 영향과 실제 apply receipt, 현재 source가 일치해야 한다.
4. 현재 Step·기준·권한으로 문맥과 재사용 여부를 만든 뒤 실행 제어에 참조를 전달한다.
5. 네이티브 호출은 메인 세션이 수행하고 실제 handle을 ACK한다. Python이 실행했다고 추측하지 않는다.
6. 결과·종료 근거·독립 검증·메인 검토를 구분한다. 관찰 종료·idle·시간 경과는 Done이나 잠금 해제 근거가 아니다.

SourcePin은 commit뿐 아니라 graph revision/hash와 dirty fingerprint를 포함한다. 미확인 환경은 clean으로 간주하지 않는다. graph·구간·context·reuse의 참조는 소유자·범위·source·version/hash 검증 없이 다른 작업에 재사용할 수 없다.

변경의 상세 계약은 [데이터·문서 명세](spec-graph-document.md), 실행 연결은 [문맥·실행 명세](spec-context-execution.md), 실제 오류/복구 근거는 [검증 자료](evidence/2026-10-02/)를 함께 확인한다.
