# 4단계 R5/R6 세션·Host 연결 상세 계획

2026-10-06. 상태: **계획 전용**. 코드는 Core 0.3.0 / SQLite 4 / graph 1 기준으로 읽었다. 아래 객체·operation은 R0 확정 전의 의미 초안이다. 구현·PMT 상태 변경·실서비스 호출·시험 실행 기록이 아니다. 상위 범위는 [4단계 방향](../04-session-continuity.md), 공통 데이터/권한/저장 의미는 [공통 계약](contracts.md), 시험 기준은 [검증 명세](verification.md)를 따른다.

## 코드에서 확인한 연결점과 미확정 계약

- `src/pmt/hooks.py`는 native event를 `actor="hook"`으로 정규화하고, 발생 UUID를 포함한 작은 pending envelope를 먼저 기록한 뒤 공유 CLI에 전송한다. `process_session_start`는 `read_context`를 병렬 조회하지만 `PMT_SCOPE_ID`가 명시된 경우만 요청한다. `PMT_RECORD_ID`는 선택적 Item 제한이다. 현재 결과는 metadata 개요가 아니라 기존 `context_markdown`이며, native 출력 구현은 Codex와 Claude만 허용한다.
- OpenCode는 `integrations/opencode/pmt.js`에서 `session.created`에 별도 bridge 조회를 준비하고 `experimental.chat.system.transform`에서 문맥을 주입한다. Python의 `process_session_start`에는 OpenCode 지원이 없다. 해당 제품의 hook/transform/output 계약은 이 adapter 경로를 기준으로 따로 판정한다.
- 현재 hook 조회 요청은 `actor="hook"`을 보내고, hosted `adapt_host_actor`는 등록 설치 principal로의 변환을 provenance가 일치하는 `record_event`에만 허용한다. 따라서 그대로 된 hosted SessionStart 조회 연결은 현재 코드에서 확인되지 않았다. 임의 caller flag로 인증을 우회하지 않고 native 출처를 검증한 뒤 등록 설치 principal의 정상 인증/범위 검사로 연결하는 경계가 필요하다. 원 actor/session provenance는 감사 참조로 보존한다.
- Host operation 경계는 `host/application.py`의 legacy `ALLOWLIST`와 확장 registry인 `host/host_contract.py`·`host/data.py`에 나뉘어 있다. `storage_config.select_store`는 Hosted mode에서 해당 registry를 거부/분기한다. Phase 3 CLI operation 집합은 Host 공개 목록이 아니다. R6 신규 포트는 저장 전용으로 검토해 각 경계 registry·인증을 함께 확정해야 한다.
- OpenCode native `session.idle`/Codex `Stop`/Claude `Stop`·`SessionEnd`는 관찰 이벤트다. 현재 pending은 hook 전달 재시도 자료다. 이를 checkpoint 확정·업무 종료·점유 해제로 승격할 근거는 없다. R5 checkpoint는 실제 결정/착수/결과/변경 반영 receipt만 소비한다.
- `pending.py`는 완료 결과와 evidence bytes의 owner-bound outbox, fingerprint/hash 검증, 재시작 후 `checking→unknown`, current Host/source/owner 재확인을 제공한다. 이는 지역 private spool/outbox다. R6의 공유 checkpoint나 alignment 저장을 그 outbox에 섞거나 타 기기의 local pending을 읽는 경로로 확장하지 않는다.
- `migration.py`는 현재 알려진 미완료 execution, claim, operation/Phase 3 journal/outbox, Host resource journal을 검사해 quiescence를 요구하고, Host transfer는 backup/import receipt를 둔다. 신규 checkpoint/alignment journal의 보호 범위·schema 목록은 아직 없으므로 백업·이관 경로별 포함/거부 규칙이 R0에서 결정돼야 한다. unresolved 효과를 조용히 누락하거나 정리하지 않는다.
- 패키지는 공통 Python/CLI와 Codex·Claude plugin, OpenCode Node adapter로 구성된다. 각 제품의 event·출력·설치 경계가 다르므로 adapter fixture, 격리 local 통합, 독립 모델 의미시험, 실제 제품 설치를 서로 다른 증거 tier로 기록한다.

## 공통 실행·인계 규칙

R5/R6 소비자는 R0/Main이 승인한 공통 request/response·Source/Basis·권한·오류·재처리 계약을 사용한다. 제안 타입은 공통 계약의 `ResumeOverview`, `SessionLink`, `Checkpoint`, `BasisVector`, `AlignmentReceipt` 의미를 참조하며 구체 필드와 operation/endpoint 이름은 R0 registry 확정 항목이다. R0에서 별도 지정하지 않는 한 CLI envelope의 protocol version, canonical UUID, `request_id` 재사용 의미, JSON stdout 전용, exit code 0~5, request fingerprint, 명시 scope/read authority, fresh auth, CAS·unknown 규칙을 유지한다.

각 작업은 기존 Session/Work/Item/Step/run에 연결되는 refs를 주고받으며 새 업무 depth를 만들지 않는다. 세션 ID, checkpoint ID, Hook occurrence ID는 참고·중복 억제·이력 연결일 뿐 claim 소유권을 바꾸지 않는다. 쓰기 경계는 안정된 event ID와 동일 의미 fingerprint를 묶고 current principal/scope·expected pointer revision·현재 source basis를 재확인한다. pointer CAS 충돌이면 현재값을 조회하고 후보/immutable record를 보존한다. 분리된 파일/DB 효과는 journal/ref/hash로 조정하며 실제 결과 unknown이면 같은 효과를 조회하기 전 재적용하지 않는다.

완료 Hook/Stop/idle/세션 종료/timeout/사용자 prompt 존재만으로 semantic checkpoint를 만들거나 점유를 해제하지 않는다. 의미 있는 checkpoint 트리거는 실제 사용자 결정, 명시적 claim/run 착수 receipt, 사용자가 검토했거나 위임받은 메인 AI가 실제 판정한 결과 receipt, 승인된 계획/문서 반영 receipt다. 매 결과를 사람에게 검토시키는 조건을 추가하지 않는다. Hook이 빠져도 마지막 확정 checkpoint와 현재 허가된 DB/Git/파일 상태에서 다시 찾을 수 있어야 한다.

## R5 — Hook·스킬·세션 연결

### R5-S1 — 제품별 이벤트와 조회·출력 계약 확인

**목적·이유.** SessionStart 입력의 안정된 session/event/profile 식별자와 제품이 실제 허용하는 출력 면을 확인해, 새 세션에 전달한 개요와 hook 관찰을 섞지 않는다.

**범위.** 추가·보완 후보는 제품별 capability matrix와 검증 증거다. 현재 adapter 및 hook 설정/스킬 안내는 새 공통 포트와 맞는지 조정 대상으로 한다. 제품 출력면을 공통 형식으로 강제하지 않고, 불필요해진 경로만 R0/Main 승인 뒤 제거한다. 파일 단위 수정·삭제 지시는 이 계획에 두지 않는다.

**Goal / non-goal.** Goal은 Codex·Claude·OpenCode의 native event, session/profile 안정성, explicit project/record selection, output injection 또는 명시 조회 경로를 제품별로 확인하는 것이다. Non-goal은 암묵적 프로젝트 선택, 전체 transcript/prompt 분석, 실제 사용자 설정 교체, 제품 지원을 문서만으로 통과 처리하는 것이다.

**의미 입출력.** 입력은 제품·adapter/version, native event 객체에서 allowlist된 작은 식별 metadata, 설치/profile identity, 환경 설정된 PMT scope/record, 제품 공식 output contract다. session/event ID가 없거나 scope가 없으면 제품·설정 오류/미지정 선택 상태를 반환한다. 출력은 `SessionCapability` 의미의 제품별 event/session/profile, 명시 scope, output channel, bounded input/output, 실제 확인 tier, 미지원·unknown 사유다. raw input은 출력·로그에 보존하지 않는다.

**기술·서비스 연결.** `normalize_event`, `_native_parts`, `process_session_start`, `lookup_context`, 두 native hook JSON, OpenCode plugin의 `event`·system transform, shared CLI `read_context`, hook pending/replay를 기준으로 fixture를 설계한다. SessionStart 1.5초 process timeout은 조사 기준일 뿐 성공 SLA가 아니다. native hook의 제품 설정상 timeout과 내부 조회 timeout을 하나의 예산으로 확인한다.

**선행·소비자.** R0/R4 의미·budget/overview 포트. 제품 대역 조사만은 G0 이후 준비 가능하다. 산출은 R5-S2와 제품별 검증 작업에 전달한다.

**관찰·시험·완료.** P4-R5-01: 세 제품의 stable identity·explicit/missing scope·출력 형식, 새로운 환경/기존 profile, Codex·Claude SessionStart 주입과 OpenCode transform을 각 tier로 연결한다. 실제 현재 auth로 해당 scope를 조회했는지 포함한다. 제품별 연결 범위와 untested/blocked를 matrix에 남기면 완료다.

### R5-S2 — metadata 개요 전달과 안전한 명시 조회

**목적·이유.** 명시 프로젝트를 가진 새 session이 최소한 현재 목표/상태/기존 실행/unknown을 확인하고, 추가 detail의 현재 권한 확인 경로를 받도록 한다.

**범위.** SessionStart를 기존 `read_context` 마크다운과 구별된 bounded metadata overview로 R4 composer에 연결하고, 제품 주입 또는 명시 조회 참조를 전달한다. 스킬 안내는 개요를 받았어도 실제 작업 전 source/권한·점유 재확인 및 필요한 단계별 reference 조회 순서를 설명한다. 기존 fallback/warning 의미를 보완한다. 이름/포맷과 actor provenance 변경은 Root 계약이다.

**Goal / non-goal.** Goal은 explicit PMT scope에 대해 authorization 아래 제한된 사실·참조·incomplete/unknown을 전달하는 것이다. Non-goal은 overview를 작업 선택·실제 변경 분석·private Step 지시·작업 권한으로 간주, 목록에서 자동 scope 선택, 타 owner의 local pending 조회, private detail을 일반 session context에 넣는 것이다.

**의미 입출력.** 입력은 제품 session, 명시 scope 및 선택적 record, 정상 인증된 설치 principal과 현재 scope read permission, 검증된 native 출처, Overview budget, 요청 ID/correlation이다. 필수 scope 누락이면 조회하지 않고 `not_configured`와 explicit setup 안내를 반환한다. 성공 출력은 공유 metadata 권한 범위의 Overview 목표/대전제/금지·진행/기존 run/pending refs·주의/unknown·last-confirmed basis·상세 조회 경로·UTF-8 byte/line/budget·source coherence다. private Step 지시와 원문은 이 권한으로 읽지 않는다. 현재 정보를 다 못 모으면 `incomplete`/`source_changed`, 권한·제품 출력 오류는 안전한 warning/조회 방법이다. 사용자 본문/인증정보/절대경로를 포함하지 않는다.

**기술·서비스 연결.** R4 `ResumeComposer` metadata projection → `SessionAdapter` → shared CLI operation → 제품 native output. Codex/Claude output은 `hookSpecificOutput.additionalContext` 경로, OpenCode는 `experimental.chat.system.transform`; OpenCode Python `process_session_start` 재사용 가능성을 가정하지 않는다. Hosted 조회는 등록된 설치 principal의 정상 인증/범위 검증 경로에 매핑하고, source provenance를 확인한다. 기존 코드에서 이 mapping은 아직 구현 확인되지 않았으므로 R0가 실제 read adapter를 정하고 검증하기 전 제품 연결 완료로 판정하지 않는다. shared metadata 접근과 private instruction 접근은 별도 권한 경계다.

**선행·소비자.** R0 request·actor·scope/read auth 계약과 R4 Overview 필수 필드·budget. 결과 소비자는 새 native AI session과 이후 실제 착수 경로다. 사용자 설치/config 변경 대신 격리 fixture가 기본이다.

**관찰·시험·완료.** P4-R5-01·03: explicit scope만 조회하고 identity/profile mismatch, revoked auth, 제품별 주입 실패를 검사한다. 필수 내용 누락은 incomplete, optional detail은 reference, transcript/prompt/secret sentinel은 제품 출력·CLI·일반 로그에서 없어야 한다. 독립 새 session이 overview를 작업 권한으로 오해하지 않고 detail read 절차를 아는지 실제 tier별로 확인한다.

### R5-S3 — 실제 업무 receipt 경계의 checkpoint 요청 연결

**목적·이유.** Hook 관찰을 실제 업무 결과라고 오해하지 않으면서, 결정·착수·결과 검토·변경 반영 같은 확정 경계에서 새 checkpoint의 기록 기회를 제공한다.

**범위.** 스킬/제품 연결이 shared service에 checkpoint 요청을 전달하는 계기와 SessionLink 참조를 추가한다. 이 요청은 이미 발생한 실제 decision/claim/run/review/alignment receipt에서만 파생한다. 스킬은 명시 사용자 의도 및 본체 operation의 반환 receipt를 확인한다. 이벤트 종류/업무 상태·write port는 R0가 정한다. 기존 lifecycle 이벤트의 의미는 관찰로 유지한다.

**Goal / non-goal.** Goal은 동일 업무 event 재처리, checkpoint pointer CAS, 결정·receipt·source basis 추적이다. Non-goal은 hook 자체의 의미 판정, 매 prompt/tool/heartbeat마다 write, Stop/idle/end/응답 끝으로 checkpoint/Done/해제, 성공 문구를 저장하는 것이다.

**의미 입출력.** 입력은 확정 event ID 및 request ID, 현재 owner/session/scope, 관련 실제 receipt refs, expected checkpoint pointer revision, coherent BasisVector다. 필수 receipt·authority·revision이 없으면 기록 없이 `not_eligible/unknown`류 실패를 돌려보내야 한다. 출력은 immutable checkpoint ref와 CAS receipt 또는 충돌/current pointer, duplicate replay 결과, source 변경/필수 자료 부족 사유다. 동일 request ID에 다른 fingerprint는 conflict다.

**기술·서비스 연결.** Lifecycle/claim/execute/result/review/alignment의 본체 receipts와 CheckpointStore; Hook은 event/correlation만 연결하고 실제 checkpoint 직렬화는 공유 service에 위임한다. local transaction 안에서 업무 상태/event/request/pointer가 같이 확정 가능한지는 R0에서 메인이 계약 산출물로 결정한다. Git/file effect는 effect journal의 실제 완료 receipt가 난 뒤에만 반영 checkpoint를 허용한다.

**선행·소비자.** R0 CheckpointStore·request/CAS 규약, R1 Checkpoint/Basis, R3 alignment receipt, R4 refs. R5-S4의 재처리 시험 및 R6 저장 연결이 소비한다.

**관찰·시험·완료.** P4-R5-02는 여러 경계의 hook 누락/중복, 동일 request replay/다른 body, CAS 경합, 사용자 선택과 기록 순서, effect 중단 전후를 확인한다. Pointer 이전 값과 미해결 journal이 보호되고, callback 누락 시 actual state 재조회로 복구한다. Stop/idle-only case에서는 업무 checkpoint/Done/claim 해제 전이가 0이어야 한다.

### R5-S4 — 제품 실패·재처리·비밀 경계와 스킬 반환

**목적·이유.** 응답이 늦거나 중복되거나 누락되어도 기존 확정 상태를 덮지 않고, 다음 session이 미확정 hook/checkpoint를 실제 상태와 조정하게 한다.

**범위.** 재처리/명시 fallback, event ordering/source change 경고, `not_run`/`blocked` 반환을 제품 guide와 관찰 계층에 연결한다. configured deadline 초과 후에도 이미 저장된 pending의 동일 request/event ref는 유지한다. 새 event가 이전 event를 대체하지 않는다.

**Goal / non-goal.** Goal은 실제 응답/저장 결과를 확인해 재개 경로와 안전한 경고를 제공하는 것이다. Non-goal은 제품을 강제로 깨우거나 종료된 세션을 이어 실행, 무제한 retry, orphan event를 합쳐 삭제하는 것이다.

**의미 입출력.** 입력은 제품·adapter·event/request/session/profile IDs, 제한 시간/size, 이전 pending ref 및 replay outcome, 진단 sink 상태다. 출력은 전달/저장/개요 조회 각각의 outcome, 유지된 원 ID·pending ref·replay/current read 방법 또는 failure code, 안전한 제품 메시지다. 시간 초과/응답 손실은 unknown으로 남긴다.

**기술·서비스 연결.** `hook-pending`, `_atomic_json`, `replay_pending`, `get_request_result`, diagnostic logger, OpenCode app.log를 실제 레코드와 연결한다. 다른 Host 장치의 result/resource outbox는 현재 device가 소유하지 않으므로 보이지 않는 사실을 안내하되 읽지 않는다.

**선행·소비자.** R5-S1~S3 및 R0 event/fingerprint/diagnostic 규약. 소비자는 SessionStart와 후속 작업자, 제품 설치 수용이다.

**관찰·시험·완료.** P4-R5-02·03: 중복/역순/누락/timeout/late output, pending 재시작, 제품 profile 변경, 로그 저장 실패와 credential/prompt/transcript/env/argv/PID/path sentinel을 확인한다. 업무 저장 실패는 성공이 아니며, 이미 업무 반영된 뒤 diagnostic 실패는 같은 effect 재실행을 유도하지 않아야 한다. native event 관찰·metadata 주입·checkpoint 반영 증거를 별도 tier로 전달한다.

## R6 — Host 저장·복구·통합·효율·패키징

### R6-S1 — Host 저장 전용 metadata 연결

**목적·이유.** 실제 Git/파일/모델 실행을 client에 두면서 승인된 checkpoint·change/alignment refs를 다른 환경에서 현재 권한 아래 읽고 같은 의미로 기록한다.

**범위.** R0 메인이 확정한 storage-only operation, Host metadata/auth adapter, client storage selector와 version compatibility, fresh current scope/actor/device/environment/session checks, request fingerprint/replay, pointer CAS를 추가/연결한다. Host에는 Git 실행, baseline sync/수집, 모델·프로세스 실행을 노출하지 않는다. 정적 Git diff bytes가 꼭 필요하다고 R0 메인이 계약에 명시하지 않으면 Host metadata에 넣지 않는다. source proof는 client가 actual source 확인 후 ref/hash/receipt로 제출한다.

**Goal / non-goal.** Goal은 local/Host에서 동일 의미의 version-bound shared metadata reference와 오류를 얻는 것이다. Non-goal은 Host가 Git/파일/baseline sync/runner/model/process를 실행, network-shared SQLite, 일반 dispatcher 전체 공개, 미승인 endpoint, private instruction 권한의 metadata 권한 승격이다.

**의미 입출력.** read는 명시 scope + 현재 등록 principal/auth + shared metadata read permission + checkpoint/change/alignment selector + expected/current basis 요구조건을 받아 허용된 metadata/ref + completeness/revision을 반환한다. private Step 지시·본문과 local source detail은 별도 current WorkAccess/owner 경계를 통과해야 한다. write는 source-bound immutable record, `request_id`, semantic fingerprint, expected pointer revision, actual receipt refs를 받아 append/ref + current pointer CAS receipt 또는 current revision conflict를 준다. principal/permission/scope mismatch, stale revision/source, unsupported contract는 기존 pointer를 그대로 두고 분류 가능한 code를 준다. 원 diff/transcript/credential은 wire/log에서 금지다.

**기술·서비스 연결.** 기존 `HttpStore` HTTP 제한/인증·redirect 거부·CA 검증·response envelope·observer, `HostApplication` authorization·request ledger, `HostDataExtension` registry 및 `_HostedOperationFacade`, `select_store` hosted 분기를 재사용 검토한다. 현재 `read_context` allowlist는 참고 기능이고 새 metadata authorization을 자동 충족하지 않는다. Client adapter는 등록된 설치 principal로 인증하고 source attribution과 명시 scope를 검증한다. shared metadata 권한과 private instruction/work-content 권한은 분리한다. checkpoint/정렬 write는 CAS·semantic fingerprint와 실제 transaction을 함께 검증한다.

**선행·소비자.** R0의 public interfaces와 scope/auth semantics 산출물, R1/R3 record definitions. R5 product read와 R6-S2 복구가 소비한다. 신규 operation/endpoints/registry는 R0 메인 산출물에서 확정한다.

**관찰·시험·완료.** P4-R6-01: local StorePort 대비 Host의 정상·거부·stale·same replay·다른 body/revision conflict parity를 실제 SQL 상태/ledger/ref로 확인한다. user local validation scope 안에서만 격리 환경 사용. R0 wire/auth 정의가 누락되면 메인이 선행 산출물을 보완할 때까지 구현 관문을 통과하지 못한 것으로 반환한다.

### R6-S2 — schema·이관·백업·미해결 효과 복구

**목적·이유.** schema upgrade/backup/restore/import가 진행 중이거나 미확정인 checkpoint, alignment, publication을 삭제·덮어쓰기·완료 오인하지 않게 한다.

**범위.** DB/Host schema compatibility, additive migration 및 backup/import의 허용/거부, 신규 refs/journal 보존, inactive resource 처리, rollback/recovery receipt를 정의하고 격리 데이터에 연결한다. 기존 schema를 수정/거부하는 조건·backup 경계는 R0 메인이 선행 설계 산출물로 확정한다.

**Goal / non-goal.** Goal은 정상 quiescent DB 이관의 hash/count 일관성과, non-quiescent 상태에서 원본 보존을 증명하는 것이다. Non-goal은 실제 사용자 primary를 바꾸거나 unresolved effect를 취소/덮어씀, 간편화 명목으로 권한/원래 owner/session을 옮기는 것이다.

**의미 입출력.** 입력은 old/new Core·SQLite/Host schema versions, migration manifest, backup/import bundle hash, 상태별 active run/claim/journal/outbox/pointer refs, 대상 namespace/current identity, current auth다. 출력은 accepted/blocked/precondition/receipt, 이전·새 count/hash/version, unresolved list의 안전한 식별·이유, 재개/복구 가능 단계다. unknown journal은 quiescent로 간주하지 않는다.

**기술·서비스 연결.** `Database._initialize`의 명시 schema migration/version reject, SQLite online backup, `MigrationCoordinator._assert_quiescent`, migration bundle table allowlist, `TransferService` owner-bound receipt, content-addressed resources, `host_resource_journal`, Phase 3 operation journal/outbox를 분석한다. 신규 checkpoint/alignment effect journal이 확정되면 source snapshot/include list와 target import guards에서 보존 또는 안전한 명시 거부가 필요하다. 현재 backup가 새 PMT R5/R6 저장 객체를 지원한다고 가정하지 않는다.

**선행·소비자.** R0 schema/version/journal 분류, R6-S1 schema/records. 업데이트·패키지·이관 소비자는 R6-S6.

**관찰·시험·완료.** P4-R6-01: 정상·busy·crash 직전/후·기존 schema·unknown journal의 격리 DB를 backup/import해 source/target SQL/hash/resource 목록 비교. unresolved Host effect는 backup 불가/회수 불가 상태를 명시하고 기존 포인터/바이트가 그대로임을 입증해야 한다. 복구 완료 receipt가 있으면 그 효과에 한해 다음 수용으로 진행한다.

### R6-S3 — end-to-end local/Host·Git·SQL·파일·독립 프로세스 수용

**목적·이유.** 개별 fixture 통과가 실제 수집/Host 공유/재개 중의 원본 상태 일관성이나 경쟁 조건을 보증하지 않으므로 독립 실행 조건에서 다시 확인한다.

**범위.** 무변화/외부 commit/dirty owner/branch 변경/Host 단절/응답 유실/두 기기·다중 프로세스/checkpoint 경합/증거 불가/부분 반영 시나리오와 각 tier를 통합한다. local checkout의 Git 변경은 별도 격리 fixture에서 만들고 Host는 오직 metadata 수신·현재 상태 조회에 사용한다.

**Goal / non-goal.** Goal은 원 실행/owner/미커밋 변경·무관 가지·근거 ref 보존, current authorization 및 CAS에 의한 동일 결과를 확인하는 것이다. Non-goal은 user data, 실제 user branch, 외부 운영 Host를 이용하는 것, Hook 부재/Host offline 중 새로운 shared write를 큐잉·추정하는 것이다.

**의미 입출력.** 입력은 current source/DB/schema/config fingerprints, 실제 실행 receipt와 SessionLink/checkpoint, role/device/environment/session 분리, 지정 scenario 및 fault point다. 출력은 각 경계의 actual before/after Git HEAD/dirty diff/파일 bytes·SQL row/pointer/revision/request/result·resource hashes, 실제 동시 process 결과, outcome/unknown/recovery ref다. GET/status 성공은 효과 성공이 아니다.

**기술·서비스 연결.** 실제 local Git repository, SQLite, 임시 리소스 directory, 실제 loopback HTTPS 서버(HttpStore→HostApplication), 서로 별도 프로세스의 동시 request 및 중단/재시작을 사용한다. pytest 자체 수준·통합 runner·사용자 제품을 증거에서 분리한다. 테스트 fixture만으로 제품 계층을 통과시키지 않는다.

**선행·소비자.** R5, R6-S1/S2 및 R1~R4 scenario/receipt. 그 결과가 독립 model session(S4)와 최종 비용/통합 보고(S5/S6)를 제공한다.

**관찰·시험·완료.** P4-R6-01·02 및 R0/R1/R2/R3/R5의 영향 회귀. two process CAS/SQLite lock에서 단일 승인된 pointer/effect와 보존한 충돌 후보를 확인한다. HTTP/Host는 실제 loopback TLS, 검증 가능한 CA, 두 device/environment/session identity를 쓰고 auth 회수·scope deny를 포함한다. source 변경/response loss 때 원 request 조회 전에 재실행하지 않는다.

### R6-S4 — 독립 새 GPT-6 Luna session 의미 품질 확인

**목적·이유.** bundle이 짧아진 사실만으로 세션 재개가 됐다고 보지 않고, 이전 task 대화가 없는 실제 별도 context가 현재 판단을 복원하는지 본다.

**범위.** 독립된 새 `gpt-6-luna` session/context에 실제 생성된 resume bundle과 해당 권한이 허용한 detail만 전달한다. 모델 실행은 반드시 허가된 native 경로에 한정하며 직접 provider/API 호출과 실서비스 Claude 전송은 제외한다. fixture/golden 규칙 측정과 실제 제품/bundle 결과를 분리한다.

**Goal / non-goal.** Goal은 목표·금지·위임, 구현/계획/검증의 수준, 유효 근거 조건, 기존 run/pending, 현재 변화, unknown·다음 행동이 정확한지 독립 판단을 기록하는 것이다. Non-goal은 평가 정답을 prompt에 제공, history/source를 몰래 context에 포함, 모델 성공을 DB/제품 상태 성공으로 간주, 단일 응답으로 일반 정확도를 주장하는 것이다.

**의미 입출력.** 입력은 source-bound bundle bytes/hash, 세션 identity와 모델/version/native 실행 경로, 지정 role·scenario·allowed detail refs, 분리 보관된 평가 기준/fixture ID다. 모델 결과는 해석된 goal/constraints/status/evidence/current execution/next action/unknown 및 실제 추가 조회 refs다. 기대와 실제 불일치는 independent reviewer가 criteria와 근거 refs로 채점하고 model response outcome과 system state를 별도로 보존한다.

**기술·서비스 연결.** R4 composer와 actual native session; measured bytes/detail reads; fixture simulation 결과는 `fixture`, 실제 local pipeline는 `local_integration`, 실제 독립 모델 응답은 `model_actual`, 설치/제품 hook은 `product`로 tagging한다. fresh GPT-6 Luna는 같은 모델·role/환경조건을 유지하는 비교에 사용하고 토큰 제공 정보가 없으면 actual usage 대신 unknown으로 둔다.

**선행·소비자.** 안정된 R4 bundle·R6-S3 source fixture 및 User-authorized native model path. 결과는 품질/비용 분석과 final acceptance다.

**관찰·시험·완료.** P4-R6-02: 위 판정 항목을 positive·unknown/unavailable·stale checkpoint 사례에서 모두 확인한다. 과거 transcript 없음, 평가 정답 누출 없음, model session ID·model version·prompt/bundle/detail 실제 bytes/ref hash·추가 조회수·응답/evidence 별도 기록이 있어야 한다. 호출 허용 경로가 없으면 blocked/not_run으로 남기며 fixture 결과와 합치지 않는다.

### R6-S5 — 전체 비용·성능·재작업 비교

**목적·이유.** Hook 조회·snapshot 확인·필요 detail 비용을 제외한 비교는 전체 재개 비용을 과소평가하므로 기존/새 경로를 같은 조건으로 비교한다.

**범위.** 동일 goal·acceptance·source·repository state/fixture·OS/tool/dependency/config·device/environment/role·model·model policy·verification definition의 비교 manifest와 모든 실제 작업 비용을 모은다. 비교 가능성이 없는 값은 산출/절감 주장 대신 불일치 또는 unknown이다.

**Goal / non-goal.** Goal은 무변화에서 반복 분석/동일 시험을 실제 줄이는지, 새 조회·생성/전송·상세 읽기·오류/재검토·질문/재작업이 얼마나 추가되는지, 품질이 유지되는지 확인하는 것이다. Non-goal은 현재 코드만으로 절감률·provider token usage를 지어내거나 fixture 수치를 실제 제품으로 외삽하는 것이다.

**의미 입출력.** 입력은 before/after Source/Basis, 시작·끝 경계 정의, iteration/warm/cold, local/Host/model/evidence tier, byte/count/time/token/cost availability, quality criteria다. 출력은 총조회·읽은 bytes·API request/response bytes·bundle/detail size·host transfer count·wall time 분포·actual/estimated/unknown token·사용자 또는 위임받은 메인 AI의 재검토와 실제 재실행 수·quality failures·비교 적격성이다. 실패/blocked·invalidated re-run 비용도 계산에 포함한다.

**기술·서비스 연결.** 기존 `efficiency.measurement`의 actual/estimate/unknown schema, context delivery measurements, `HttpStore` observer, hook diagnostic와 per-operation source/model/evidence metadata를 이용할 수 있는지 R0 manifest로 연결한다. 전체 대화/비밀 로그를 켜서 측정하지 않는다.

**선행·소비자.** R0 baseline manifest, R6-S3 actual integration, R6-S4 model quality 결과. R6-S6 acceptance report가 소비한다.

**관찰·시험·완료.** P4-R6-03: 적어도 무변화 재개와 관련 변경 재개를 같은 조건으로 비교한다. 같은 해석/검증이 생략된 실제 증거, 그 판단을 위해 수행한 current auth/source/evidence 조회 비용, 실패/재검토 비용을 같이 기록한다. 토큰은 제공자 실제 정보만 actual, tokenizer/바이트 근사만 estimate로 명시한다. 증거 부족 시 claim은 unknown이다.

### R6-S6 — source 고정·격리 패키지/설치 tier와 최종 인계

**목적·이유.** 통합 결과가 package의 실제 배포 자산/제품 동작과 일치하는지 현재 source를 고정한 후 확인하고 지원 범위를 정확히 전달한다.

**범위.** 현재 build/package contents·adapter/skill references·API/schema/protocol docs·사용 흐름·offline/error fallback을 비교하고, 격리 설치/업데이트/재설치/new session smoke를 각 허용 tier로 기록한다. 사용자 profile, credential, actual primary data는 건드리지 않는다. 설치 변경은 테스트 전용 위치/fixture에 한정한다.

**Goal / non-goal.** Goal은 각 실제 포함 제품에서 올바른 스킬/adapter/binary와 설정안내가 package snapshot에 있는지, native SessionStart와 fallback을 해당 격리 환경에서 관찰하는 것이다. Non-goal은 외부/Linux 배포, 직접 Claude 실서비스 호출, primary 전환, package 빌드 자체를 제품 acceptance로 주장하는 것이다.

**의미 입출력.** 입력은 final commit + dirty state, package artifact/hash/file manifest, API/schema version, install test root/product/profile, receipt/ref, 새 session ID, 인증 없는 sentinel/synthetic project fixture다. 출력은 package completeness/hash, 설치·업데이트·제거 후 재설치 단계별 실제 event/output, 제품 tier별 pass/fail/blocked/not_run, 회귀/미지원 범위와 rollback/recovery 방법, 최신 source/evidence hash다.

**기술·서비스 연결.** 현재 Codex/Claude package manifest와 OpenCode Node/Python bridge, skill workflow docs, package snapshots, 설치 smoke를 비교한다. fixture bridge·격리 local 제품 install·독립 model·실제 사용 profile은 별도 tier다. 스킬 수정 후 source hash가 바뀌면 동일 source를 요구하는 증거는 재검토한다.

**선행·소비자.** R5 완료; R6-S1~S5 actual 증거 또는 분명한 미해결/blocked 상태; clean source/package capture. 완료 결과는 main의 통합·전체 21 ID 판정과 사용자의 local-validation 보고를 소비한다.

**관찰·시험·완료.** P4-R6-02·관련 회귀·패키징 smoke. 반환 파일 목록/해시, 이벤트·제품 output 증거, 제품별 실지원 범위 및 향후 실행이 필요한 확인점을 기록한다. 실제품 미설치/제한된 권한은 blocked/not_run이며 adapter fixture를 pass로 바꾸지 않는다.

## 필수 증거와 보고 양식

각 P4-R5/R6 결과는 ID, 실제 구현/source commit·dirty 여부 및 관련 파일 hash, contract/schema/package version, test definition/scenario, 실행 tier, command/action과 실제 exit/status, UTC, OS/Python/SQLite/HTTP/TLS CA·제품·adapter/model/policy 버전, namespace/project·scope와 식별자의 비밀 아닌 digest/ref, current auth/scope outcome, 이전/이후 SQL revision·pointer/ledger/journal state, Git HEAD/dirty/paths digest, 실제 파일/resource sha/size, request/event/effect/session IDs, byte/count/time, actual/estimate/unknown token과 quality state, 로그/증거 ref, 실패/blocked/not_run/skip/invalidated 사유를 갖는다. 민감 값을 scrub한 실패 코드만 log 한다. P4-R5-03 sentinel 검증은 실제 안전한 synthetic value로 수행한다.

21개 ID 중 이 계획의 직접 소유는 P4-R5-01~03과 P4-R6-01~03이다. 각 ID를 fixture(unit/protocol), 본체(actual local Git/SQLite/files), loopback HTTPS·독립 process, model_actual, product 중 실제 tier에 매핑하고 더 낮은 tier의 성공은 상위 tier 성공으로 올리지 않는다. 전체 비용 산출에는 Hook의 bounded lookup, overview 구성, Host request/response 전송, 상세 조회, Source/auth 확인, 오류 재검토, 재작업, 재실행, 사람 질문까지 포함한다.

### 최종 인계 필수값

- 구현 변경 범위, 공통 registry/DTO/API/schema·호환 결정과 소비자 영향; actual 지원 제품·local/Host 경계.
- P4-R5-01~03/P4-R6-01~03별 pass/fail/blocked/not_run/skip/invalidated, 실행 계층·실제 command/action/exit·현재 source 및 환경·evidence hash.
- R5 Hook 수신 / 개요 조회 / 제품 주입 / semantic checkpoint receipt 성공의 구별; Stop/idle의 비승격 검증.
- R6 Host current auth·CAS/replay, local Git/SQL/file 상태, 독립 HTTPS/proc, backup/restore 미해결 journal 보호와 recovery refs.
- 새 GPT-6 Luna 독립 context의 quality result와 실제 추가 detail calls, fixture와 product/model_actual 분리.
- 전체 접근/생성/전송/검토/재작업 비용·token provenance·unknown, 성능 한계, 미해결·복구 지점, package/source/evidence hash.
- 사용자 local validation 및 명시된 제외 scope 준수; 실제 primary 전환·직접 API·Claude 실서비스 전달·외부/Linux 배포가 포함되지 않았는지 사실 기록.

## R0 메인 선행 산출물

아래 항목은 추가 사용자 승인 대기가 아니다. R0 메인이 별도 `implementation-interfaces.md`와 registry/verification 연결에서 구현 착수 전 확정·시험해 R5/R6 소비자에게 전달할 선행 산출물이다. 본 계획은 공통 wire/schema 이름을 미리 확정하지 않는다.

1. Hook의 native source attribution과 설치 principal identity를 실제 request에서 연결하는 source provenance 규칙·지원 제품·검증 자료. `read_context`는 등록 principal의 정상 인증·명시 scope 검사를 거치고 shared metadata와 private instruction/read 권한을 분리하며, 임의 caller flag로 인증을 우회하지 않는다.
2. `ResumeOverview`와 기존 `read_context`의 관계, SessionStart가 반환할 명시적 scope/unknown/incomplete/detail refs, 제품별 bounded output/deadline 및 metadata-only contract.
3. 의미 checkpoint를 유발하는 actual receipts와 발생 작업 경계 목록, event/request identity, checkpoint 포인터 CAS·request fingerprint·중복 처리·source coherence의 transaction/journal 경계.
4. Phase 4 local/Host data ownership, storage-only operations와 exact allowlist/read/write/file classifications, Host registry 위치·current auth/session/role/device/environment/source requirements. Host에 Git 실행/baseline sync/model/process 실행을 공개하지 않는다. 기존 R3 Host selector가 해당 source operation을 허용한다는 전제 금지.
5. SQLite/Host schema/API/protocol version 확장·migration/reject·backup/import/restore 포함 목록. unresolved alignment/checkpoint/publication journal의 quiescence 기준, 보존·복구·rollback 절차 및 old process와의 compatibility.
6. 신규 error/attention/outcome trace names, generic `request_id`와 native `event_id`/effect ID의 관계, 어떤 로그 실패가 업무 rollback이고 어떤 실패가 진단 신호인지.
7. 비교 baseline/quality fixture와 user local validation에서 사용할 수 있는 실제 native `gpt-6-luna` independent-context 경로·tier 기준. model/API/direct Claude/production use 금지 범위는 기존 사용자 결정 유지.

R0 메인은 위 사항을 확정하고 R0 검증 결과와 함께 R5/R6 작업자가 소비할 수 있게 인계한다. 계약이나 현재 권한 범위에서 지원 불가능한 연결은 선택지·영향·blocked 원인을 같은 산출물에 남긴다.
