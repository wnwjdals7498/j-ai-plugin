# R1 현재 사실·체크포인트와 R4 재개 문맥 상세 계획

2026-10-06. 상태: **계획 제안**. 대상 범위는 R1 CurrentFactsReader/CheckpointStore와 R4 ResumeComposer/공통 BasisVector 참조다. 아래 논리 port와 객체는 설계를 설명하는 이름이며 CLI operation, HTTP endpoint, DB schema/DDL, public API 이름을 확정하지 않는다. 공통 wire·저장 규격은 R0 메인 결정 및 [공유 연결 설계안](implementation-interfaces.md)의 승인된 의미를 따른다. 현재 구현 동작과 신규 요구를 구분하며, 이 문서는 구현·시험·PMT 상태 변경을 뜻하지 않는다.

## 목표와 경계

R1은 프로젝트의 현재 방향, 실제 구현 확인 수준, 업무·실행 상태를 원본 참조에 묶어 읽고, 명시적으로 확정된 업무 경계에서만 checkpoint metadata와 pointer를 기록한다. R4는 그 사실을 먼저 제한된 metadata 개요로 제시하고, 현재 점유와 source를 확인한 뒤에만 작업에 필요한 상세 문맥을 구성한다. 두 기능은 같은 BasisVector의 관련 부분을 참조해 혼합 snapshot이나 stale 행동 제안을 현재 사실로 바꾸지 않는다.

| 기능 | 추가 | 수정·연계 | 금지 범위 |
|---|---|---|---|
| R1 CurrentFactsReader | 방향·구현 확인 수준·결정·Work/Item/Step·run/claim/pending을 출처에 묶는 조회 projection | 기존 `read_context`, Step/claim/run 조회와 SourcePin·resource·verification ref 활용 | 요구 문서, graph, private Step 지시, 시험 결과를 복제한 별도 원본으로 만들지 않음. 조회 사실만으로 완료·정지·점유를 추론하지 않음 |
| R1 CheckpointStore | 확정된 의미 경계에 대한 immutable checkpoint ref와 current pointer CAS receipt | 기존 request/event replay, DB revision/transaction, resource/journal 보호 규칙과 연계 | 현재 schema에 미확정 DDL을 삽입하지 않음. 조회·Hook·heartbeat마다 checkpoint 생성 금지. checkpoint를 권한/소유권 증서로 사용 금지 |
| 공통 BasisVector | source, 계약, graph, 관련 업무 revision, 적용 조건, capture completeness를 묶는 버전형 참조 | R0가 정한 SourcePin·업무 revision·resource hash 의미를 사용 | Git HEAD, 업무 revision 또는 사용자 제공 hash 하나를 전체 snapshot으로 주장하지 않음 |
| R4 ResumeComposer | metadata-only ResumeOverview, owner-bound ResumeBundle, refs/cursor, 조건부 NextAction | `read_context`, `build_task_context`/`resume_task_context`, detail/alias 검증, Hook의 명시 scope 조회와 연계 | 이전 대화·session link·cache를 grant로 사용 금지. Step 지시 본문을 일반 관리 개요나 Host metadata에 포함 금지. 필수 필드 삭제로 성공 처리 금지 |

**Goal:** 새 session이 이전 대화 없이도 현재 사실과 출처, 누락·불일치, 진행 중인 실행, 상세 내용을 읽을 수 있는 조건, 다음에 허용되는 조회/대기/선택을 구분한다. 반복 본문을 저장하지 않고 안정된 ref와 제한된 요약만 사용한다.

**Non-goal:** 자동으로 사용자 의도나 대전제를 승인하는 일, 다른 owner의 작업 탈취, 미확정 실행 재실행, Step 완료/검증 pass 생성, 모든 저장소/이력의 통째 복제, 제품 설치·Host 배포·새 모델 호출을 이 범위만으로 완료 선언하는 일.

## 기반 조사: 현재 기능과 공백

아래는 현재 코드의 제한된 동작을 확인한 것이다. 4단계 적합성·수용 성공이라는 뜻은 아니다.

| 현재 기반 | 확인된 동작 | R1/R4에서 남는 공백 |
|---|---|---|
| `src/pmt/queries.py` `read_context` | 지정 scope 및 선택 record의 하위 records, 비종료 결정 일부, revision/state와 제한 Markdown을 읽는다. cursor binding은 scope/record/query/limit, 출력 budget은 문자 길이 기준이다. | 업무 변경 이후 일관된 다원 source basis를 만들지 않는다. source/hash·run/claim/pending coherence, metadata/detail 권한 층, UTF-8 전체 envelope 예산, mandatory 부족 표시가 재개 계약 수준으로 연결되지 않았다. record body의 일반 metadata 출력이므로 private directive 전문을 조회하는 수단으로 사용해서는 안 되며 별도 명시적 차단 검증이 필요하다. |
| `src/pmt/efficiency/context.py` task context | 현재 run owner·Step directive version·workspace claim/lock을 먼저 확인하고, 기존 private directive resource는 그 실행 권한 아래에서 읽는다. SourcePin, graph index, evidence를 검증하고 생성 전후 run/lock/source/index가 변했는지 재확인한다. 결과 projection은 제한 예산, alias, detail cursor, resource ref로 저장하며 원문 directive 대신 ref/hash를 기록한다. | 이는 실행 중인 특정 Step을 위한 projection이다. 프로젝트/Work 재개 개요, checkpoint, 독립 SessionLink, pending/전체 작업 현황 통합, R1 current-facts와 공통 BasisVector는 제공하지 않는다. |
| task context resume/detail | 기존 context를 현재 session·owner/run·directive·scope lock·source·graph index·evidence에 대조한다. 이전 alias를 재활성화하지 않으며, stale이면 재생성을 요구한다. | 이를 project-level R4 재개 성공으로 간주하지 않는다. 다른 session이나 checkpoint를 소유권/읽기 권한으로 인정하지 않는다. |
| Hook lifecycle | `process_session_start`는 Codex/Claude SessionStart event 저장과 명시 scope가 설정된 `read_context`를 병렬 수행한다. 조회 timeout·CLI 실패를 unavailable로 알리고, event 확인 실패 시 pending을 보존한다. `replay_pending`은 event 저장 응답이 성공한 뒤 pending을 제거한다. | 현재 context는 간단한 record 개요다. 현재 direction/source/진행 run/checkpoint의 통합, OpenCode native output, owner-bound 작업 bundle 생성은 확인된 지원이 아니다. Hook event 저장 성공은 task 점유/실행/완료가 아니다. |
| 실행·claim 경계 | `execution/service.py`는 run revision CAS, owner 검증, active/review states, durable result·stop confirmation·main review를 구분한다. `lifecycle.py`의 claim/recover는 기록 revision 및 owner를 다룬다. Step private directive는 현재 run과 version이 일치할 때 읽는다. | 새 checkpoint/summary가 기존 query/controller보다 강한 상태 판정 규칙을 만들 수 없다. run의 `starting`, `running`, `review_pending`, `reconciling`, `cancel_requested`, pending 및 불명확한 상태는 각 실제 원본 조회가 필요하다. |
| Host/storage | 인증된 device/session/scope·revision 및 SourcePin 기반 write가 일부 metadata/resource 기능에 존재한다. DB request 처리와 파일 effect는 journal/recovery 경계를 분리한다. | R0가 정할 checkpoint/overview 저장 port, Host allowlist·schema version·backup/import 지원은 현재 기능으로 가정하지 않는다. Host에 Git/working tree 또는 private directive 읽기 권한을 추가하지 않는다. |

근거 범위는 `queries.py`, `efficiency/context.py`, `hooks.py`, `steps.py`, `execution/service.py`, `lifecycle.py`, `host/auth.py`, `host/data.py`, `http_store.py`의 조사로 한정한다. 본 계획은 이 조사만으로 실행하지 않은 제품/Host 경로까지 지원한다고 서술하지 않는다.

## 공통 논리 타입 제안: BasisVector와 참조

공유 타입의 실제 필수 필드/직렬화는 R0 메인 결정 사항이다. 아래는 생산자·소비자 간 의미 제안이다.

| 논리 타입 | 의미·필수 구성 | 제약 |
|---|---|---|
| `BasisVectorRef` | 불투명 ref, contract/schema version, 범위 identity, content/manifest hash | 본문 전체를 복제하지 않고 immutable metadata/resource ref를 가리킨다. ref만으로 열람 권한을 부여하지 않는다. |
| `SourceBasis` | repo/project/workspace ref, observed HEAD, 분석·반영 기준의 구분, dirty/non-Git 상태, 관련 코드·문서 inventory ref/hash와 coverage, 검토한 범위 지문과 source capture ref | 관련 inventory는 선택 범위와 coverage를 함께 기록하며 전체 code 검증 완료를 뜻하지 않는다. working tree 내용은 client 권한에서 수집한다. Git HEAD만으로 dirty/document/업무 상태까지 같다고 하지 않는다. |
| `ContractBasis` | 요구/계획/Step directive version ref, 결정 refs, graph schema/revision/hash | 지시 본문은 private resource에 남기며 개요에는 허가된 metadata·ref만 전달한다. |
| `WorkBasis` | 관련 record IDs·revisions, run/claim/pending 관찰 refs, 관찰 시각과 query/capture ref | 한 SQLite read transaction/snapshot의 식별 가능한 업무 관찰을 표시한다. run·lock 등의 변화는 전후 확인한다. |
| `ConditionBasis` | 선택된 verification/tool/environment/config definition refs 및 확인하지 못한 조건 | 전체 환경 변수/환경 dump는 제외하며, 확인하지 않은 조건은 unknown으로 남긴다. |
| `CaptureManifest` | 구성 요소별 source/authority/version/time/completeness, 전후 coherence 판정과 누락/변경 이유 | 서로 다른 capture 시점을 무리하게 원자 snapshot 하나로 가장하지 않는다. |
| `BasisVector` | 위 ref/manifest를 연결한 기준 집합, 자체 version/hash | canonical serialization/hash 포맷과 적용 범위는 R0 결정 전 제안 상태다. |

R1은 capture 시작·끝의 Git/계약/업무 참조가 일치하는지 비교한다. 재확인이 가능한 요소는 제한 재수집한다. 변동이 반복되거나 비교하지 못한 요소가 남으면 `incomplete`/`source_changed`/`unknown`을 반환하고 마지막 확정 pointer를 유지한다. 다른 시점의 업무 snapshot과 source를 같은 시점의 일관된 기준인 것처럼 엮지 않는다. 분석 완료 기준과 사용자/메인이 실제 반영을 확정한 기준은 별도 ref로 유지한다.

## R1 논리 단계

### R1-S1 — 현재 사실의 범위와 읽기 등급을 확정해 조회

**목적:** 선택된 namespace/project 및 선택적 Work/Item/Step의 방향, 결정, 실제 진행 상태를 출처와 확인 수준을 함께 반환한다.

**의미 입력:** `scope selector`는 명시 project ID 또는 허가된 상위 범위와 선택 record ID를 담는다. `MetadataAccess`는 현재 호출자·session/device·namespace/project 범위의 metadata read 권한 및 auth revision ref를 뜻한다. 선택적 `basis hint`는 이전 checkpoint/cursor ref일 뿐 현재 기준이 아니다. 선택적 role은 표시 필드 선택에만 쓰며 인증 권한을 높이지 않는다. 포트 의미와 필드는 [공유 연결 설계안 §3·§4.1](implementation-interfaces.md)에 맞춘다.

**필수·제약:** project/scope는 명시되어야 한다. 미지정이면 자동 선택 대신 허가된 후보 metadata 또는 `selection_required`를 반환한다. UUID·scope 상하 관계와 caller의 metadata read authority를 기존 trusted adapter로 검증한다. 공유 checkpoint/facts metadata의 권한은 session-bound private context 권한과 별개다. 공유 저장소 adapter가 필요하면 project/namespace 권한을 먼저 확인하고 저장 주체와 함께 원래 actor/session을 감사 provenance로 남긴다. 조회 결과는 records의 ID/kind/title/state/revision와 허가된 요약 metadata, 현재/폐기 결정 refs, known/unknown origin을 구분한다. run/claim의 허용된 metadata 관찰은 WorkAccess와 구별하며, source·private 지시·working tree 내용 읽기에는 [공유 연결 설계안의 WorkAccess](implementation-interfaces.md)가 필요하다. task kind `step`의 directive artifact ID/version/hash는 허가된 metadata일 수 있으나 directive 본문은 이 단계의 일반 조회 결과에서 제외한다. 특히 관리툴/Hook/Host 개요에는 private Step 본문이나 transcript를 실어 보내지 않는다.

**출력:** `CurrentFactsProjection`(사실 항목 + source ref + observed revision/time + confidence/evidence class), 관련 진행 record refs, 현재 run/claim/pending 조회 결과 refs, 누락/불완전 사유, 다음에 읽을 수 있는 metadata ref. `implemented`, `verified`, `planned`, `unknown`은 관찰 출처를 가진 분류 제안이며 verification 원본 state를 대신하지 않는다. 구현이 있다는 사실은 `implemented` 후보일 뿐 시험 통과를 뜻하지 않는다.

**방법·연계:** 우선 `queries.handle`의 bounded record/decision projection과 현재 Step/run/claim/pending 조회 서비스를 재사용한다. 독립 SQL 상태 로직을 consumer마다 복제하지 않는다. 실제 실행·claim detail 조회는 기존 execution/lifecycle 경계를 사용하며 session link나 Hook event로 소유자를 바꾸지 않는다. 구현 사실은 R2 mapping/index와 실제 verification/evidence ref가 준비되기 전에는 coverage-limited/unknown으로 전달한다.

**선행/소비자:** R0가 정한 scope/read authority, fact class, 오류·요청 replay 의미가 선행한다. R2 구현 mapping과 R3 적용성 정보는 확장 입력이지만 R1 기본 사실 조회는 이를 사실보다 강하게 표시하지 않는다. R4 overview와 R1-S2가 소비한다.

**상태·CAS·중단 복구:** 이 단계는 read-only다. 동일 범위에서 source/record revision이 query 전후 바뀌면 새 snapshot을 재조회하거나 `incomplete`로 끝낸다. 중단은 pointer/원본 상태를 변경하지 않는다. timeout/DB 오류는 명시 오류로 남긴다.

**검증 제안:** P4-R1-01의 세부 case를 (a) 계획만 있는 feature, (b) 코드 존재·미검증, (c) 실제 verified pass, (d) 실제 fail/not_run/blocked, (e) superseded/폐기 결정으로 나누어 실제 refs와 분류를 대조한다. 모델의 문구만 있는 입력은 verified가 되지 않는지 확인한다. P4-R1-03으로 metadata read와 private resource read 분리를 scope/role 조합별 확인한다. 순수 fixture 외에 격리 DB/실제 graph·artifact 참조를 확인하되 테스트를 아직 수행한 것으로 기록하지 않는다.

**로그·증거:** scope/project와 authority revision ref, 관련 record revision/count, component capture time/completeness, 사실 분류·근거 ref/hash, 접근 거부/누락 사유·duration·bytes를 기록한다. 제목 등 안전한 metadata와 지문만 일반 trace에 남기고 directive/transcript, raw path, credential은 남기지 않는다.

**완료/인계:** 각 항목에서 planned/implemented/verified/unknown 의미가 source/evidence ref로 설명 가능하고, 조회 권한만으로 private content가 열리지 않으며, consumer가 current basis 생성을 위해 필요한 refs 및 known unknown을 받는다.

### R1-S2 — BasisVector 수집과 coherence 판정

**목적:** source와 업무 데이터가 다른 시점의 혼합 snapshot으로 성공 게시되는 것을 막는다.

**의미 입력:** R1-S1의 명시 scope와 capture refs, client가 허가 범위에서 확인한 SourcePin, 관련 코드·문서 inventory ref/hash·coverage, 계약·결정·graph refs, 한 업무 read snapshot의 revision set, 적용 조건 refs, 수집 시작/끝 manifest. `CaptureWorkBasis`/`ValidateBasis`의 경계는 [공유 연결 설계안 §4.1](implementation-interfaces.md)을 따른다.

**제약/실패:** Project/repository/workspace/branch binding 충돌, 읽기 권한 철회, Git 변경/dirty delta 변경, graph/계약 version 변화, run/record/claim/pending revision 변화, capture/hash 누락은 각각 이유를 보존한다. Git 기준과 분석/반영 기준을 합치지 않는다. 끝 시 확인 불가도 `unknown`이다. 무한 재수집 금지; R0가 정할 제한 횟수/시간을 따른다.

**출력:** BasisVector 제안 + component refs/hash + coherence enum/result + incomplete reasons + 확인 시각. 완전한 coherence는 확인한 구성 요소만 대상으로 선언한다.

**방법·연계:** SourcePin 및 graph의 기존 source verification을 client/Host adapter로 사용하고, 관련 code/doc inventory의 hash와 coverage를 별도 source component로 수집한다. SourcePin만으로 inventory 전체 의미를 대표하지 않는다. SQLite 기록은 기존 DB transaction read snapshot으로 모은다. 장시간 Git 작업 중 DB write lock을 잡지 않는다. 조합 전후에는 저비용 version/ref를 재대조한다. 두 소스의 atomicity가 보장되지 않으면 동시 capture 불가 상태를 솔직히 표현한다.

**선행/소비자:** R0 BasisVector/SourcePin·capture 의미와 R1-S1. R1-S3, R4 개요/작업 문맥이 basis ref를 사용한다. R2가 실제 change의 before/after source refs를 소비한다.

**상태·CAS·복구:** 이 단계는 제안 basis만 만든다. 확정 checkpoint pointer를 전진시키지 않는다. source 변동 시 bounded recapture 후에도 불일치면 incomplete. 실패 전후 원 source·DB는 유지한다.

**검증 제안:** P4-R1-03을 (a) 업무 record revision 변경, (b) HEAD/dirty 변화, (c) graph index revision 변화, (d) 하나의 요소 capture 실패, (e) pointer CAS 경쟁과 조합해 실제 source/DB 대조한다. 혼합 정보가 `ready/coherent`로 저장되지 않고 이전 pointer가 남는지 확인한다.

**로그·증거:** 각 basis component의 ref/hash/version, capture start/end, 재수집 횟수, coherence outcome/reason, prior pointer revision을 기록한다. 전체 파일/DB hash 대신 관련 범위 지문을 사용한다.

**완료/인계:** consumer가 기준 구성 요소별 시점·완전성·확인 범위를 복원할 수 있고 불일치를 정상 성공으로 소비하지 않는다.

### R1-S3 — 확정 경계 checkpoint와 pointer CAS

**목적:** 확정된 업무 경계를 불변 기록으로 남기되 조회나 세션 생성이 현재 상태를 전진시키지 않게 한다.

**의미 입력:** 확정 업무 event/request ID, explicit boundary kind와 결정 actor/source ref, 완전성 조건을 통과한 BasisVector, 관련 사실/현재 실행/근거/다음 조건 refs, expected current pointer revision. Hook 관찰만 온 경우에는 경계 확정 event로 승격하지 않는다.

**필수·제약:** 같은 event/request retry는 같은 결과로 수렴하고, 내용이 같더라도 다른 사용자 행동 event는 각각 구분한다. checkpoint는 direction/fact/evidence refs, basis, parent checkpoint, checkpoint schema/writer version, current pointer revision, completeness와 의미 metadata hash를 기록한다. public 함수명/DDL은 R0 결정 전 비확정이다. checkpoint는 task claim, execution grant, completion receipt가 아니다.

**출력:** immutable checkpoint ref 및 CAS receipt(current/expected revision, created/replayed/conflict, event/request ref, basis ref, stored hash), 필요하면 `pointer_unchanged`/`incomplete` 이유. 조회만으로 만들 새 checkpoint가 없으면 no-op 결과.

**방법·연계:** 기존 `db.run_request`/request replay·transaction·event 기록 패턴을 우선 재사용한다. 현재 `Phase3Storage`의 object 접근은 owner_actor+owner_session에 묶여 있으므로, project/namespace에 공유할 checkpoint/current pointer는 메인이 확정할 권한 검사를 수행하는 shared metadata adapter를 거친다. adapter는 저장/조회 principal과 original actor/session audit provenance를 구별한다. 공유 metadata 권한에서 private task context나 execution 권한을 유추하지 않는다. object 저장/포인터 갱신 및 업무 event/replay receipt는 R0가 지정한 동일 write boundary에서 일관되게 처리한다. immutable object 생성과 mutable pointer update 충돌 시 candidate object는 미참조 보존/정리 정책을 따르고 current pointer는 건드리지 않는다. DB와 파일 publish가 함께 필요하면 existing journal/recovery 규칙으로 분리한다. 저장 port는 [공유 연결 설계안 §4.2](implementation-interfaces.md)의 의미에 맞추며, 실제 operation·schema는 R0에서 정한다.

**선행/소비자:** R0 저장·idempotency/CAS·보존/backup 결정, R1-S2 완전한 basis, main/user가 확정한 boundary. R2는 before checkpoint를, R4는 latest confirmed checkpoint를 조회한다. R6는 Host parity·backup/import/recovery를 확인한다.

**상태·CAS·중단 복구:** `expected_pointer_revision`가 현재와 다르면 stale CAS로 거부하고 새 current 사실을 조회한 뒤 재평가한다. 재시도는 같은 request/event의 receipt를 회수한다. commit 응답 유실은 효과 조회 후 수렴한다. timeout·session 종료·Hook replay만으로 임의 retry에 의해 다른 pointer로 덮지 않는다. pointer 이전 revision 및 unresolved journal은 보존한다.

**검증 제안:** P4-R1-02를 (a) 확정 event 후 다른 session metadata lookup, (b) 같은 request replay, (c) 같은 본문·다른 event ID, (d) stale expected pointer, (e) DB commit 뒤 응답 유실, (f) Hook 미실행/중복으로 분리한다. 실제 DB row/revision/request ledger/event/ref/hash 전후를 검증하며 checkpoint 생성만으로 claim/session ownership이 바뀌지 않는지 확인한다. P4-R1-03과 별도 process 두 writer 경쟁도 확인한다.

**로그·증거:** event/request/correlation ref, parent/current checkpoint ref, basis/hash, expected/current/new pointer revision, idempotency outcome, commit/readback/journal outcome, 실패 이유를 기록한다. raw content는 resource ref로 남기고 일반 trace에서 반복 복제하지 않는다.

**완료/인계:** 확정 경계만 immutable checkpoint를 만들고 current pointer는 CAS 성공 때만 변경되며 retry/응답 유실 복구가 같은 effect로 귀결된다. R2/R4/R6가 소비할 ref와 unresolved recovery 조건을 전달한다.

### R1-S4 — 회귀 경계·이전 상태와 재개 검증 연결

**목적:** 기존 execution/verification state를 checkpoint 요약으로 덮지 않는지 확인한다.

**사례:** active run, review_pending 결과, cancel_requested/reconciling, queued/pending, lock 유지, 오래된 checkpoint, verification pass 후 조건 변경, branch/workspace 전환, 권한 revoke, session close/새 session을 각각 실제 state와 비교한다.

**규칙:** 조용함·idle·session end는 stop confirmation이 아니다. result 저장은 업무 review/Done이 아니다. stop confirmation은 실제 controller/receipt와 기존 execution service가 인정한 값에 따르고, Step Done은 기존 main review + current criteria/evidence/integration 경계만 결정한다. 오래된 결과·pointer는 설명·이력으로 보존할 수 있으나 새 계획의 완료가 되지 않는다.

**산출·완료:** 결과 evidence manifest는 확인 결과와 unknown을 나누고, 실재 테스트가 없는 경우에는 계획으로 남긴다. R4 NextAction이 기존 Query/Queue/controller/main review에 전달할 조건과 이를 생성한 basis를 보유한다.

## R4 논리 단계

### R4-S1 — metadata 전용 ResumeOverview 생성

**목적:** SessionStart 또는 명시 조회에서 현재 방향과 진행 사실, 기존 실행, 읽기 다음 단계만 빠르게 전달한다.

**의미 입력:** 명시 project/scope와 선택 Work/Item ref, current `MetadataAccess` ref, 선택된 latest checkpoint/BasisVector ref, bounded current-facts projection, role·configured byte/line budget·renderer policy version. SessionLink가 있다면 참고 사실 ref로만 받는다. Overview 포트는 [공유 연결 설계안 §4.5](implementation-interfaces.md)의 `ComposeResumeOverview` 의미를 따른다.

**출력:** 제한된 `ResumeOverview`: 현재 목표/대전제/금지/위임 ref, 계획·구현·검증 수준의 사실 요약, 마지막 확정 checkpoint 및 현재와의 차이/미확인, 진행·pending run/claim 관찰, 사용자 선택 또는 대기 필요, detail 조회 가능 경로(ref/cursor)와 권한 획득 조건, 조건부 NextAction 후보, 전체 serialized response byte/line 측정과 `complete`/`incomplete` 이유.

**필수·권한:** 목표·금지/위임·현재 basis·기존 실행/미확정·unknown은 mandatory 그룹이다. 예산에 들어가지 않으면 성공 요약으로 줄이지 말고 incomplete/추가 조회 필요를 명시한다. 개요는 current `MetadataAccess`만 사용하며 private directive 본문, source working tree, 다른 owner의 local pending bytes, 상세 transcript를 읽지 않는다. 공유 metadata 권한 어댑터가 필요해도 private task/execution read를 허용하지 않는다. 선택 scope가 없으면 자동 최근 project를 고르지 않는다. 새 SessionLink/새 session ID는 ownership을 승계하지 않는다.

**방법·연계:** 결정적 Python projection으로 필수 우선순위를 정하고 동일 원문 body를 섹션별로 반복 복제하지 않는다. 원본은 refs로 유지하고 개요에는 한 번만 짧게 인용/요약한다. SessionStart adapter는 현재 Codex/Claude의 명시 scope `read_context` 기반 fallback과 연계하고, R4 overview 지원이 연결되기 전에는 현재 Hook 출력을 새 기능으로 표현하지 않는다. 전체 envelope UTF-8 budget은 기존 `read_context`의 문자 budget과 별도 계산한다.

**선행/소비자:** R0 policy/budget/scope와 R1-S1~S3의 facts/basis/checkpoint. R3 current alignment/applicability가 준비되면 그 ref를 반영한다. R5 Hook/skills/제품이 consumer다.

**상태·중단 복구:** 조회 전후 관련 업무 revision과 basis coherence가 달라지면 bounded 재구성 또는 incomplete 반환. 요약 cache는 authority/basis/policy/role/budget key가 모두 현재일 때만 재사용. 요청 중단은 업무 pointer나 실행 상태 변경 없이 끝난다.

**검증 제안:** P4-R4-01을 scope/role/예산별로 (a) 필수 내용 모두 수용, (b) byte 한도 초과, (c) line 한도 초과, (d) mandatory 자체가 한도 초과, (e) 많은 history/이력 중복 refs, (f) unknown/진행 run 조합으로 분해한다. 응답 envelope 실제 bytes·lines와 필수 의미 보존을 확인한다. P4-R4-02는 캐시가 authority revoke, checkpoint 변경, basis stale, old alias/cursor에서 거절/재검증되는지 확인한다. Hook fixture pass는 native 제품 세션 주입 통과로 계산하지 않는다.

**로그·증거:** project/scope·session/event ref, selected checkpoint/basis/policy/role refs, cache hit/miss/stale reason, response byte/line, omitted ref count, mandatory incomplete, existing execution status refs·조회 outcome을 남긴다. 본문 전문·비밀·raw path는 기록하지 않는다.

**완료/인계:** 새 세션이 어디까지 metadata만 읽었고 어떤 실제 권한/선행 뒤 상세를 볼 수 있는지 알며, incomplete 상태를 완전한 방향 판단으로 오해하지 않는다.

### R4-S2 — 실제 점유 뒤 owner-bound ResumeBundle 구성

**목적:** 작업 상세 지시/관련 코드·근거를 읽을 권한을 획득한 뒤 현재 기준에서 필요한 작은 문맥만 제공한다.

**의미 입력:** 현재 `WorkAccess`(actual run/claim/scope union/owner/revision/source mapping) 확인 ref, record/directive expected revisions, 현재 SourcePin·관련 code/doc inventory ref/hash·coverage·workspace/repository mapping·graph/contract/evidence refs, R1 BasisVector 및 R3 검토된 assessment/applicability refs, role, explicit UTF-8 byte/line budget, 허용 scope/필드 정책. Port 의미는 [공유 연결 설계안 §3·§4.5](implementation-interfaces.md)에 맞춘다.

**필수·제약:** content read 직전에 current authority와 owner/active run을 확인해야 한다. project metadata read 또는 새 SessionLink만 가진 session은 task body를 읽을 수 없다. private Step directive는 현재 점유/run·directive version·resource integrity 검증 뒤 필요한 작업자에게만 보인다. 관리툴·일반 log·Host metadata·SessionStart overview에는 directive 본문을 포함하지 않고 내부 resource ref/hash로 연결한다. role projection이 제한되어도 directive 원문 접근 정책은 역할 필터와 별도로 검사한다. stale previous bundle·alias·cursor는 grant가 아니다.

**출력:** task bundle의 목표·방법·필수 input/output·non-goal/금지·current source/contract refs, 관련 change/alignment/evidence applicability refs, 실행/pending/unknown 및 next-action 조건, detail refs/cursor, source/access binding, completeness·byte/line·policy/writer version. 오래된 이전 내용은 현재 ref가 검증될 때만 비교 근거로 요약하고 반복 전문은 넣지 않는다.

**방법·연계:** `efficiency.context`의 `_actual_authorized_snapshot`, private directive/resource 검증, fresh graph slice, evidence verification, 전후 run/lock/SourcePin/index 검사를 재사용한다. 관련 source inventory hash/coverage도 BasisVector의 해당 component로 재검증한다. 기존 `build_task_context`/`resume_task_context`는 specific Step/run projection으로 취급한다. R4가 이를 project-level bundle로 확장하는 설계는 R0에서 현재 public operation registry와 호환을 확정한 후 수행한다. API 명칭 또는 필드 변경을 이 문서가 정하지 않는다.

**선행/소비자:** R0 authority/content-read/ref/budget·Host 경계, R1 current facts+basis, R3 assessment/applicability, 실제 현재 owner/run/source. R5 main/runner가 소비한다. R6는 Local/Host 의미 동일성과 여러 환경의 실제 복구를 확인한다.

**상태·CAS·중단 복구:** 입력 expected run/record/directive/source/graph revisions와 조합 후 다시 읽은 값을 비교한다. 하나라도 변하면 저장/응답을 current success로 확정하지 않고 stale/incomplete와 재조회 조건을 반환한다. 저장된 bundle은 재구성 가능한 파생 cache다. 저장 receipt만 성공하고 response 유실된 경우 원 request/ref를 회수하며 작업 자체를 재실행하지 않는다. 다른 session owner가 되면 새 권한을 가진 뒤 재구성한다.

**검증 제안:** P4-R4-01에서 여러 budget·role·큰 필수 지시를 시험하고 private directive가 authorized task path에서만 접근되는지 DB/resource/응답을 확인한다. P4-R4-02는 권한 revoke, owner change, run revision, source/graph/evidence hash, cursor/page change 중 각각을 bundle 구성 전/중/후에 주입한다. stale old bundle은 반환되지 않고 기존 active execution은 유지되어야 한다. R1-R4 scenario의 실제 소비자 검증으로 표현한다.

**로그·증거:** owner/run/directive/source/graph/evidence ref hash, current revisions, authorization decision, bundle/cache ref/hash, role·budget·actual bytes/lines, detail count, stale check/rebuild result, request/effect receipt. directive body는 diagnostics에서 제외한다.

**완료/인계:** 점유 뒤 현재 읽기 권한으로만 task detail이 제공되고 구성 전후 basis가 유효하며, owner 밖 session은 metadata와 명시적인 선택/대기 이유만 받는다.

### R4-S3 — 조건부 NextAction을 기존 실행 경계에 연결

**목적:** 다음 행동을 자동 실행 명령이 아닌 근거와 선행 조건을 가진 제안으로 표현한다.

**입력/출력:** 현재 facts, checkpoint/basis, alignment/applicability, run/claim/queue/pending 상태와 current authority를 받아 action 후보·reason·required refs/revisions·재확인 시점·owner/stop/review 조건·unknown을 반환한다. 기존 의미 후보는 조회 결과 확인, 기존 실행 관찰, 대기, 변경 분석, 계획/근거 갱신, 확정 Step 착수, 검증/통합 검토, 선택 요청이다. enum/string public 이름과 CLI command mapping은 R0/main 확정 전 비확정이다.

**금지·조건:** active/미확정 run, review_pending result, cancel_requested, reconciliation/pending에는 확인/관찰/대기 경계를 우선한다. 상태가 바뀌었거나 stop 미확정이면 rerun/cancel/unlock을 제시하지 않는다. run 결과가 있어도 main review·현재 criteria·evidence·integration 확인 없이 Step/Item/Work Done을 추천 완료 상태로 만들지 않는다. old `next` natural language, model claim 또는 session link만으로 execute permission을 만들지 않는다. 안전 조건을 판정할 정보가 부족하면 `unknown`/사용자 선택 필요를 유지한다.

**연계:** Query/Queue/control/execution controller/claim/main review가 실제 실행 여부·stop·Done의 단일 경계다. R4는 refs와 사용자/runner에게 제안만 전달한다. Host unavailable이면 offline overview를 실행 grant로 사용하지 않는다. 다른 기기의 pending bytes 없이는 제출·재전송 완료를 제시하지 않는다.

**검증 제안:** P4-R4-03을 조합별로 (a) active run, (b) review_pending durable result, (c) pending/reconciling/cancel_requested, (d) same source no change, (e) safe changed source, (f) delegate-scope auto-modification, (g) premise conflict, (h) owner/revision unknown으로 나눈다. 각 후보가 기존 controller condition과 일치하는지 실제 Query/Queue/control call receipt 및 DB state로 검증한다. 이 시험은 실제 실행을 재실행하지 않고 제안/승격 거부를 상태 관찰로 확인한다.

**완료/인계:** 모든 action에 basis·authority·required condition·reason이 있어야 하며, 후보 계산이 상태 전이/claim takeover/Step Done을 직접 일으키지 않는다. main/runner에 unresolved 조건을 포함해 전달한다.

### R4-S4 — stale refs·cache·detail·예산의 재구성

**목적:** 재개 경로 어디에서든 오래된 summary를 현재 사실로 재사용하지 않게 한다.

**처리:** cache key 후보는 scope, 관련 BasisVector refs/hash, authority/owner revision, contract/renderer/policy version, role, budget을 포함한다. R0에서 key/TTL/invalidity를 확정한다. detail cursor는 현재 bundle/source/scope/authorization/budget binding과 맞는지 검증한다. old alias는 새 bundle에서 활성화하지 않는다. cache가 invalid이면 관련 데이터만 재수집하고, 권한이 없거나 원본이 보존되지 않았다면 재구성 불가/unknown을 명시한다.

**실패·복구:** 권한 revoke/change는 detail read 전에 거부, source/work revision stale는 새 bundle 생성 필요, 삭제된 과거 detail은 unavailable, 증거 access/hash 실패는 not_applicable 또는 unknown 판정 ref, 예산 부족은 mandatory coverage와 상세 페이지 방법을 명시한다. pointer/checkpoint를 임의 전진시키지 않는다.

**검증 제안:** P4-R4-02를 old cache, authority revoked, record revision changed, branch/environment changed, old alias, mismatched cursor, retained detail removed, cache corruption으로 나누고 실제 store/ref/hash·detail access 결과를 확인한다. P4-R4-01 mandatory-too-large case와 결합해 missing 필수를 생략하고 complete로 보고하는지 확인한다.

**완료/인계:** stale/invalid/unknown/retained detail 부족이 구분되고, 재구성은 현재 권한·source 기준에서만 동작한다. 이전 성공은 증거로 보존해도 현재 사실로 자동 승격되지 않는다.

### R1-S4·R4-S3/S4 추적 보완

아래 단계는 위의 공통 범위·권한·실패·증거 규칙을 상속한다. 용어와 pointer/run/evidence 의미는 [공통 계약의 Checkpoint·Resume·NextAction](contracts.md#3-공통-정보-객체) 및 [공유 연결 설계안의 R4 포트](implementation-interfaces.md#45-재개-개요와-작업-문맥--r4--r5mainrunner)를 따른다. 이 표는 누락된 작업 경계만 보충하며 실제 API/schema 이름을 정하지 않는다.

| 단계 | 추가·수정·삭제·금지 범위 | Goal / Non-goal | 의미 입력 → 출력 | 방법·선행·소비자 | 진단·증거 필드 |
|---|---|---|---|---|---|
| R1-S4 | 추가: 실행/검증의 재개 상태 대조와 회귀 증거. 수정: current-facts/checkpoint의 상태 설명을 기존 execution/verification 근거에 결속. 삭제: 없음. 금지: run·Step·검증 원본 상태 변경, 완료/정지/claim 회수 추정. | Goal: 요약이 기존 실행·검토 의미를 보존하는지 확인. Non-goal: 재개 자체에서 작업을 실행/종료하거나 Step Done 처리. | checkpoint/BasisVector + 실제 run/claim/lock/result/verification/evidence refs → 보존 여부·stale/unknown 사유·R4가 지킬 선행조건. | 기존 execution/controller, claim 및 verification 판정을 읽어 대조. R1-S1~S3 선행, R4-S3와 R6 통합 시험에 소비. | run/record revision·state, owner/lock ref, stop/result/review ref, verification definition/result/evidence hash, before/after pointer와 판정 이유. |
| R4-S3 | 추가: 조건부 NextAction projection. 수정: overview/bundle의 자연어 next를 현재 권한·실행 상태·basis가 붙은 후보로 제한. 삭제: 없음. 금지: 후보를 직접 실행하거나 claim/run/Step state를 전이. | Goal: 기존 authoritative controller가 재검증할 구체 조건·근거 제공. Non-goal: 실행 위임, 자동 retry/takeover, autoDone. | 현재 facts/basis/authority + run/claim/queue/pending/receipt + policy → 후보 action·required refs/revisions·reason·unknown 및 재확인 조건. | 결정적 조건 평가 후 Query/Queue/control/execution/main-review 경계에 전달. R1 사실 조회 및 R3 assessment 선행, R5/main/runner 소비. | basis/action/policy refs, 선택·배제 이유, observed state/revision, required precondition 결과, owner/stop/review 확인, unknown·duration. |
| R4-S4 | 추가: stale cache/ref 검증과 제한 재구성 경로. 수정: cache/detail cursor/alias의 현재성 판단. 삭제: 없음. 오래된 alias/reference는 원본을 삭제하지 않고 현재 효력만 무효화한다. 금지: stale summary·권한 없는 detail 사용, 삭제된 본문 복구 주장, pointer 전진. | Goal: 보존된 현재 자료만 재사용하고, 부족/폐기를 정확히 반환. Non-goal: 전 이력 복원 또는 예산·권한 검증 우회. | 이전 bundle/cache/cursor + 현재 auth/basis/source/ref/hash·retention 상태 → valid/stale/unavailable/unknown·재구성 가능 범위와 incomplete reason. | 기존 owner/source/evidence 재검증 및 bound cursor/cache key 정책 적용. R4-S1/S2 출력 이후, R5 조회·R6 보존/복구가 소비·검증. | old/new basis·authority revision, cache key version/outcome, cursor/detail ref/hash·page, retention availability, rebuild/denial reason·bytes. |

## R1·R4 통합 시나리오와 시험 ID 맵

아래는 [verification.md](verification.md)의 21개 논리 ID 중 R1/R4 사례를 세분화한 제안이다. test name/명령은 구현 시 별도 등록하며 현재 통과 기록이 아니다. R0/R6는 공유 계약과 실제 저장/Host 경계를 입증하는 의존 case로 묶는다.

| 상위 ID | 분할할 실제 의미 시나리오 | 시스템 상태에서 확인할 것 | 연결 의존 |
|---|---|---|---|
| P4-R1-01a~e | 계획뿐인 기능 / 코드 존재·미검증 / 정의된 실제 pass / fail·not_run·blocked / superseded 결정 | 원 records/docs/code/verification refs와 projection 분류, 실제 evidence hash와 조건, 폐기 이유가 일치. 모델의 성공 서술만으로 pass가 되지 않음 | R0-01 wire/version, R2 mapping, R3 evidence applicability |
| P4-R1-02a~f | 확정 checkpoint→새 session metadata read / 같은 event replay / 같은 body 별도 event / Hook 누락 / response 유실 recovery / stale pointer CAS | pointer revision이 정해진 이벤트만큼 증가, session link는 owner/claim에 영향 없음, original request/effect 확인 후 같은 receipt, stale writer는 rollback/no advance | R0-03 idempotency/CAS, R5 Hook, R6 actual storage/restore |
| P4-R1-03a~e | source capture 중 HEAD/dirty 변경 / 업무 revision 변화 / graph revision 변화 / 하나의 component 실패 / DB pointer 경쟁 | BasisVector coherence/incomplete, old current pointer 보존, source·DB의 실제 before/after hash와 revisions 일치. 혼합 snapshot이 complete로 게시되지 않음 | R0-02 authority/basis, R0-03 CAS, R6 concurrent actual DB/client |
| P4-R4-01a~f | 작은 정상 예산 / byte 초과 / line 초과 / 필수 묶음 자체가 예산 초과 / 이력 많고 반복 refs / mandatory+unknown+active run 조합 | response 전체 serialized bytes/lines, mandatory coverage, incomplete reason, omission refs, 중복 본문 count, old execution state. 필수 부족을 complete로 위장하지 않음 | R0-01 canonical/budget envelope, R6-03 실제 비용 비교 |
| P4-R4-02a~h | old summary / 권한 revoke / record·run revision 변경 / branch·env 변경 / 오래된 alias / cursor binding 불일치 / detail retention 후 삭제 / cache 손상 | fresh authorization/source/basis, actual resource availability/hash, detail denial/rebuild, pointer/owner/실행 불변. 없음은 unavailable로 표시 | R0-02 current auth, R0-03 store/journal, R6-01 devices/offline/restore |
| P4-R4-03a~h | active run / review_pending result / pending/reconciling/cancel_requested / no change / safe delegated change / premise conflict / owner unknown / source stale | 후보 action과 existing query/controller precondition 일치. run count/state/lock/owner/Step state 실 DB 비교; 자동 실행/완료/claim 회수 없음 | R0-02 role/scope/status, R1 current facts, R3 assessment, R6-02 integrated resume |

### 결합 통합 시나리오

1. **무변화 재개:** 확정 checkpoint를 만든 뒤 다른 새 session이 metadata만 읽는다. source/업무 사실을 현재 조회하고 같은 basis이면 same-change 분석을 제안하지 않는다. 별도 task 점유 전에는 private directive·working tree detail에 접근할 수 없다. 확인 대상은 checkpoint pointer, record/run/claim state, detail access log와 overview bytes다.
2. **capture 중 변경:** Git working tree 수집과 업무 snapshot 사이에 코드/record revision을 바꾼다. 결과는 불일치 또는 limited recapture 후 `incomplete`; 마지막 checkpoint pointer와 원 실행은 유지한다. DB와 실제 파일 hash를 별도로 확인한다.
3. **중복/유실 event:** Hook의 SessionStart event 저장 응답을 잃고 replay한다. event request ledger가 같은 결과로 귀결되며 session link 기록이 생겨도 이전 작업 owner는 그대로다. Hook이 호출되지 않아도 마지막 확정 checkpoint와 현재 실제 query로 재구성 가능해야 한다.
4. **실행 미확정:** 새 개요의 이전 run이 `running`, `review_pending`, `reconciling`, `cancel_requested` 또는 pending이다. R4는 기존 결과 확인/실행 관찰/대기 조건을 제시하고 기존 handle·lock·result를 읽는다. elapsed time, idle, session end를 근거로 재실행·정지·unlock하지 않는다.
5. **예산·보존 경계:** mandatory direction/forbidden/active run/unknown refs만으로 예산 초과시키고, 이후 과거 상세 retention을 수행한다. overview/bundle은 incomplete와 누락된 ref를 알리고, 삭제한 body를 복원 가능한 것처럼 말하지 않는다. 현재 필수 증거와 미해결 journal은 retention으로 삭제되지 않았는지 확인한다.
6. **local/Host 복구:** 별도 client/device가 현재 Host metadata를 확인한다. current auth와 canonical source/workspace/branch를 다시 검증하며 같은 repository라도 branch/source가 다르면 cache를 공유하지 않는다. Host 불통 또는 다른 장치 local pending은 실행 권한/bytes가 없다고 표시한다. 실제 HTTPS·DB·파일·backup/import 상태를 확인한다.

## R0·R6 공통 계약 의존 및 시험 경계

메인 R0의 확정이 필요한 공유 사항은 BasisVector canonical form/version, `incomplete`/coherence 분류, facts/verification class, metadata vs private-content authority, Checkpoint pointer/idempotency/CAS, SessionLink semantics, budget 측정 단위, new storage/Host registry·schema migration, event/error/trace names다. 여기서는 논리 제안만 제공하며 public operation 이름/DDL/endpoint를 고정하지 않는다.

R6는 새 checkpoint/overview 객체의 local/Host read/write parity, current auth와 scope 폐기, 두 client 간 CAS, response loss/replay, backup/import/restore의 미해결 object 보존, offline pending의 unknown, 실제 loopback HTTPS/local fixture와 package/version 매칭을 확인한다. R1/R4 fixture 통과, Python unit check, Hook output 검사만으로 Host·제품·새 AI session 의미 검증을 통과 처리하지 않는다. 외부 정보성 데이터는 이 계획의 입력으로 사용하지 않았으며, 조사 사실은 지정 저장소의 현재 문서·코드에 한정한다.

시험 증거는 source commit/dirty 상태, test definition/version, 실제 명령·exit/action, namespace/project/repo/branch, role/device/session, DB 및 파일 before/after revision/hash, BasisVector/checkpoint/bundle refs, 조건/제품/tool 환경, 관찰 결과를 포함한다. 각 결과는 pass/fail/blocked/not_run/skip/이후 무효를 분리한다. 아직 실행하지 않은 시험은 예상 수용 조건으로만 기록한다.

## 완료·인계

R1 담당자는 실제 CurrentFactsReader/CheckpointStore 구현 경계와 결정된 공통 계약 version, 조회·쓰기 권한 분리, 사용한 source/DB snapshot 범위, CAS/replay/복구 증거, 미확인/실패 및 R2/R4 소비 조건을 반환한다. R4 담당자는 overview와 owner-bound bundle의 실제 입력/출력·authority/basis 검증·예산/필수 coverage·NextAction의 기존 controller 연결, cache/detail invalidation, 제품별 연결 범위와 증거를 반환한다.

메인은 R0와의 계약 차이를 조정하고 R6 실제 저장/복구 및 21개 ID 전체와 대조해 전체 수용을 판정한다. 문서 완료만으로 PMT Work/Item/Step의 상태, checkpoint pointer, 구현/시험 pass, commit/push를 만들지 않는다. 상세 설계 변경 시에는 확정된 인터페이스 및 evidence를 반영해 이 문서의 제안 상태와 의존을 갱신한다.
