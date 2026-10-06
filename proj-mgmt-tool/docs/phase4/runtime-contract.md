# 4단계 구현 연결 계약

2026-10-06. 이 계약은 논리 계획을 현재 Python/CLI 경계에 연결한다. Core 0.4.0, SQLite 5, graph 1, protocol 1이다. 구현·시험 결과는 별도 status/evidence에 기록한다.

## 공통 런타임

- `pmt.continuity.contracts`: `CONTRACT_VERSION="phase4-1"`, `authorize(db, conn, req)`, `work_access(db, conn, req, paths=(".",), workspace=None)`, `digest(value)`, `validate_metadata(value)`, `budget(payload)`, `bounded_result(req, result)`.
- `pmt.continuity.storage.ContinuityStore(db)`: 아래 함수는 caller가 연 transaction의 `conn`을 받는다. 자체 작업 상태·권한을 새로 만들지 않는다.
- `pmt.phase4`는 명시 registry로 handler를 호출한다. `handle(db, conn, req)`는 read/write, `execute_file(db, req)`는 Git/resource/파일 effect이며 `(envelope, exit_code)`를 반환한다. 파일 작업은 DB transaction 밖에서 현재 권한을 확인한 뒤 수행하고 원 요청/journal로 확정한다.
- SQLite schema 5는 additive `continuity_objects`, `continuity_pointers`, `continuity_events`, `continuity_journal`을 추가한다. Core 배포 version은 메인이 최종 고정한다. 기존 graph schema 1·protocol-v1·core exit 0~5를 유지한다.
- Local metadata 권한은 현재 OS 사용자에게 허가된 PMT 데이터와 명시 project scope다. Host는 등록 principal·namespace/scope/read/write 권한을 write/replay 직전에 다시 검사한다. source/private 접근은 실제 run owner+workspace scope claim이 별도로 필요하다.

## 저장 포트

| 함수 | 의미 입력 → 출력 |
|---|---|
| `put(conn, req, kind, body, *, object_id=None, visibility="shared", basis_hash=None, event_id=None)` | 구조화 객체 → immutable `{id,kind,scope_id,contract_version,body_hash,basis_hash,visibility,owner_actor,owner_session,body,revision,created_at}`. 같은 ID·kind·scope·body는 같은 결과, 다른 body는 충돌. event는 별개 실제 사건 식별이며 kind/scope/event 단위로 body를 대조한다. |
| `get(conn, req, object_id, *, kind=None)` | 명시 scope·current 권한 → 보존 객체 또는 not_found. private은 원 actor/session에 제한, shared은 project 권한; ref만으로 접근 grant 없음. |
| `list(conn, req, kind, *, limit=100)` | bounded 종류 조회 → 허용된 같은 project 객체. limit 1~200. |
| `read_pointer(conn, req, selector)` | project·repository/branch/workspace/task/purpose/environment selector → `{pointer_key,object_id,revision}` 또는 revision 0. |
| `advance_pointer(conn, req, selector, object_id, expected_revision)` | 실제 객체 ref·payload의 nonnegative 기대 revision → CAS receipt. object/ref/scope와 selector를 대조하며 원 객체를 수정하지 않음. |
| `begin_effect(conn, req, kind, body, *, basis_hash=None)` | original request·effect 단계 metadata → journal row. body에 actual private file bytes/절대경로/명령을 넣지 않음. |
| `update_effect(conn, req, effect_id, state, outcome=None)` | current owner+scope·실제 effect outcome → journal row. `prepared/applying/partial/unknown/conflict/completed`; completed 이전 refs 보호. |
| `get_effect(conn, req, effect_id)` | original owner·scope → journal row. 다른 세션은 공유 개요의 안전한 pending ref만 볼 수 있음. |

저장 kind는 `basis/facts/checkpoint/session_link/change/link_index/assessment/resolution/alignment/applicability/overview/bundle/detail/measurement`로 제한한다. shared DTO는 refs/hash/짧은 의미 요약이며 raw transcript·native prompt·credential/env/argv/PID·절대경로를 금지한다. private 본문도 Step 원본은 기존 resource 한 곳에 유지한다. `object_id`는 canonical UUID, body hash는 canonical JSON SHA256이다. object 본문 수정 대신 새 immutable ID를 발급한다.

pointer selector는 `repository_id,branch,workspace_ref,task_id,purpose,environment_id` 중 의미적으로 선택한 dimension이다. `purpose` 필수. canonical workspace ref와 branch/task를 명시하고 서로 다른 branch/environment를 최신 시각만으로 합치지 않는다. payload의 `expected_pointer_revision`은 새 pointer에서 0, 기존 envelope 최상위 `expected_revision`은 양수 규칙을 유지한다.

`current/planning/direction` checkpoint는 environment dimension을 생략해 같은 선택 범위의 확정 이력을 공유한다. `observation/evidence` checkpoint는 관찰 환경을 포함한다. 변경·적용성·applied alignment pointer의 관찰 환경과 checkpoint selector를 구별하며 모든 consumer가 실제 producer의 규격을 따른다. pointer dimension에서 환경을 생략해도 인증의 device/environment/namespace·basis 조건·새 환경 재검증은 유지한다.

## 업무 서비스 배정과 operation

- A `current`: `read_current_facts`, `capture_work_basis`, `validate_basis`, `create_checkpoint`, `read_checkpoint`, `link_session`.
- A `context`: `compose_resume_overview`, `compose_task_resume`, `read_resume_detail`, `propose_next_action`.
- B `changes`: `collect_changes`, `build_implementation_links`, `read_change_slice`, `register_observed_change`.
- B `alignment`: `assess_alignment`, `propose_semantic_resolution`, `apply_alignment`, `read_applicability`.
- 메인 `retention`: `prune_continuity`. 기본 dry-run; 90일이 지난 미참조 metadata만 명시 apply로 정리한다. 현재 pointer·활성 업무/실행·미완료 journal의 직접/간접 원본 refs·다른 actor의 private 객체를 보호한다. 선택된 작업이 종료된 지 90일이 지나고 미해결 효과가 없을 때만 해당 과거 task pointer를 정리한다. 이전 checkpoint parent는 현재 근거 의존과 구별된 optional audit이며 만료 후 unavailable이다.

각 모듈은 `READ_OPERATIONS`, `WRITE_OPERATIONS`, `FILE_OPERATIONS`의 실제 분류를 명시한다. source 읽기·Git·모델·파일 게시가 필요한 것은 FILE로 분류한다. 메인은 registry를 모듈 분류에 맞추되 Host와 별도로 관리한다. source inventory 필드와 basis 본문은 A/B가 합의해 공유 계약에 추가한다.

## 실제 BasisVector 교환

`source`는 `repository_id/branch/workspace_ref/observed_head/analyzed_ref/applied_ref/dirty_state/dirty_fingerprint/inventory_ref/inventory_hash/inventory_coverage`다. coverage는 선택/확인/미확인 개수·complete·reason_codes를 포함한다. inventory의 상대경로별 hash/diff 상세는 client-local private ref로 유지하고 shared에는 opaque refs/hash·범위 digest·count/coverage만 둔다. SourcePin의 graph 권위와 별개 component다.

`contract`는 현재 요구/계획/지시/결정/graph version/ref/hash, `work`는 실제 records/run/claim/pending revision capture, `conditions`는 검증 정의가 선택한 조건과 unknown, `manifest`는 component별 주체·시각·확인 범위·전후 coherence다. `complete`는 실제 확인한 구성 요소의 기준이며 새 환경 조건을 알고 있다고 단정하지 않는다. A가 생산하고 B/R4가 같은 body/ref/hash를 소비한다.

변경의 `before_basis_ref/hash`는 출발점, `after_basis_ref/hash`는 현재 관찰 기준이다. link index·assessment는 after 기준과 일치해야 한다. 현재와 다른 출발 basis를 현재 근거로 승격하지 않는다. R4는 actual change·assessment·applicability·applied alignment pointer를 연결하고 미확인/미반영/stale를 incomplete로 반환한다. graph SourcePin이 바뀌면 기존 F5 graph index도 현재 source에 맞게 갱신해야 한다.

`validate_basis`와 `read_resume_detail`은 현재 client source/private 권한 재검증이 필요하므로 FILE/WorkAccess 경계다. 일반 metadata 읽기로 해당 기능을 대체하지 않는다.

## Host 연결과 제품 규격

Host는 위 operation 중 metadata-only로 입증된 것만 공개한다. 직접 Git·code/private 본문 read·baseline sync·모델·process·파일 게시 operation 공개 금지다. client source 작업의 실제 관찰은 ref/hash·provenance receipt로 저장하며 Host가 source를 검사했다고 하지 않는다. 필요 저장 operation은 C가 메인에 인계해 registry에 함께 반영한다.

공통 저장 함수는 Host 인증 전 호출하지 않는다. trusted adapter가 principal을 검증하고 transaction 내 authorization hook으로 current grant를 replay보다 먼저 확인한다. 설치 Hook 원 source와 transport principal은 분리하며 native attribution은 allowlist로 검증한다.

| 실제 Host/클라이언트 포트 | 책임 |
|---|---|
| `get/list/put_continuity_object`, `read/advance_continuity_pointer`, `begin/get/update_continuity_effect` | 인증된 shared metadata·CAS·미완료 효과. generic private bundle/detail, 확정 checkpoint/alignment 직접 게시, completed 효과 직접 주장은 거부 |
| `read_current_facts`, `read_checkpoint`, `compose_resume_overview`, `create_checkpoint`, `link_session` | 실제 DB metadata·확정 사건 서비스. 현재 Host principal/scope·권한을 검사하며 checkpoint는 저장된 실제 사건 필요 |
| `publish_work_basis` | client의 현재 run/source grant와 관찰 hash를 대조하고 Host SQL work snapshot을 서버에서 구성. `client_attested`, `host_git_verified=false` |
| `read_decision_receipt` | 현재 run/revision·Current 결정/revision/kind·target ancestry와 SQL의 실제 `decision_saved` event를 확인. bounded ref/hash만 반환; 원문/추정 승인 없음 |
| `apply_alignment_receipt` | 실제 F1/F3 효과·현재 source/run·확정 결정/위임·Step 영수증·물리/manifest 근거를 대조한 뒤 applied pointer CAS. Host가 client Git/문서 bytes를 검사했다고 하지 않음 |
| `HostedContinuityClient` | 현재 Host 권한 → client Git/선택 파일 before/after 관찰 → dedicated basis 게시. private inventory는 client spool |
| `HostedChangesClient` | client 변경/graph 분석·private detail → Host shared refs; 실제 HostedFiles 효과 뒤 typed alignment receipt. 서버 Git/모델 실행이나 local DB fallback 없음 |
| `HostedContextClient` | 현재 권한/source 재검사 → 기존 F5 context/detail 호출 → bounded 재개 결과. Step 지시 원문은 기존 리소스를 사용 |

통신은 기존 인증 HTTPS operation port와 UTF-8 protocol-v1 JSON envelope를 사용한다. dedicated 서비스가 generic 저장소 객체를 믿고 승인을 대신하지 않는다. client 내부 composite 이름과 Host에 직접 허용된 operation을 구별한다. 실제 제품 SessionStart 자동 조회는 명시 `PMT_SCOPE_ID`와 기존 profile만 사용하며 애매한 branch/project를 추정하지 않는다.

## 재처리·측정·시험

모든 WRITE는 `db.run_request(..., authorize=...)`를 사용한다. FILE은 actual current authority → 원 request receipt 확인 → journal/effect → 물리 readback → 동일 request transaction으로 확정한다. private 예전 response를 다른 owner에게 공유하지 않는다.

전체 response envelope UTF-8 byte/line 예산과 mandatory coverage를 검사한다. mandatory 자체가 넘으면 incomplete이며 detail/ref로 연결하고 실행 준비 성공으로 표시하지 않는다. token actual이 없으면 unknown. 기본 bounded budget은 16KiB/160 lines, explicit 값은 256~262144 bytes와 8~2000 lines 안에서 검증한다. 제품 output/deadline은 제품 tier에서 별도 측정한다.

P4-R0-01~03은 메인이 actual SQLite migration/read/write/ref/CAS/replay/private 권한·별도process 경쟁으로 확인한다. 각 작업자는 자기 모듈 시험을 `--basetemp=.pmt-test/p4-a|p4-b|p4-c`로 격리한다. 현재 사용자 데이터·설정·외부 서비스는 사용하지 않는다.
