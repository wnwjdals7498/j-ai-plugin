# F5–F9 상세 구현 계획
2026-10-02 작성. 이 문서는 구현 순서와 기능 경계를 제안한다. 구현 완료·계약 확정·시험 통과를 뜻하지 않는다. 공통 계약 승인 전에는 공유 저장 schema, operation/event registry, 공개 API를 확정하지 않는다.
## 계획 적용 기준

- 기준 문서: [Phase 3 기능 목록](README.md), [F0 공통 계약](contracts.md), [문맥·실행 명세](spec-context-execution.md), [시험·인계](verification.md), [문서·문맥 효율화](01-document-context-efficiency.md).
- 2단계 기존 계약·코드의 재사용 여부는 실제 저장소와 시험으로 확인한다. 여기서 기능이 예정됐다는 사실은 구현 또는 지원 증거가 아니다.
- 기존 Phase 2 Operations와 `src-read-only` 경로는 현 구현에서 가능한 동작으로 제한해 관찰한다. 신규 F5~F9 동작을 이미 제공한다고 추정하지 않는다.
- 부모 계획의 `implementation-interfaces.md`에서 공유한 연결 규격을 따른다. 아래 키는 의미 키 수준이며 실제 API/필드/schema 선언이 아니다.
- 제안 의미 키: `source_pin`, `context_ref`, `scope_ref`, `budget`, `omitted_required`, `cursor_ref`, `alias_map_version`, `reuse_key`, `claim_ref`, `evidence_ref`, `receipt_ref`, `run_ref`, `handle_ref`, `child_step_ref`, `capability_ref`, `result_status`.
- SourcePin에는 project/repository, 선택 ref, 검토 commit, graph revision/hash, 적용 가능한 경우 검토 dirty 지문이 필요하다. 모르는 값은 unknown으로 남긴다.
- F5~F9 전반에서 역할은 문맥 projection이다. 역할 선택, short ID, 재개 요약은 인증·권한·scope를 만들거나 넓히지 않는다.
- 유한 budget과 단위는 필수다. 조건·금지·미해결·불확실성을 예산 때문에 조용히 자르지 않는다. 넘치면 누락 manifest와 안전한 다음 cursor/판단 요청을 돌린다.
- 원문 대화, Step 전체 지시문, 자격 증명·환경 비밀은 일반 로그·receipt에 저장하지 않는다. 승인된 실행 전달의 private artifact 경계를 따른다.
- 재사용은 정의·대상·입력·환경·의존성 provenance가 일치해야 한다. 모델 provenance는 검증 대상이 모델일 때만 key 요소다.
- `request_id` 재전송과 독립 행동 `event_id`를 구별한다. 같은 내용의 별개 사용자 행동을 합치지 않는다.
- 기존 제한은 최대 재시도 2회, 물리 실행 동시 slot 최대 3개 및 실행기 한도 중 작은 값이다. 기능별 시험은 예정 상태이며 기존 시험 통과를 신규 pass로 가져오지 않는다.

기능의 실제 구현 착수는 직접 선행 기능의 완료와 계약 인계를 기다린다. 그 전에는 중간 객체를 이용한 읽기 전용 순수 기술 준비만 가능하며, 목표 완료나 기능 pass로 판정하지 않는다.

## 구현 순서와 의존성

1. F5 context projection·재개 기반을 만들고 F6의 조건 키와 F7의 출력 보존을 독립 경로로 준비한다.
2. F8은 F5/F6/F7 결과를 받아 단일 실행을 관찰·제어한다. Python controller가 기존 native intent의 메인 호출 또는 확인된 CLI 프로세스 경계를 연결한다.
3. F9는 F8 단일 Step 경로가 실제 검증된 뒤에만 논리 child 여러 개를 기존 runner 메시지 하나에 묶는다. F8이 F9에 의존하지 않는다.
4. 각 단계 인계는 의미 키의 source/version/scope/run/receipt 연결을 보존하고, 공통 상태 전이는 메인 소유로 둔다.

## F5 — 문맥 구성·재개·별칭

### F5-S1 — SourcePin과 projection 입력 확정

- 목적/경계: 요청 시점에 사용할 원본 기준과 읽을 scope를 고정한다. 의미 원본은 Git 구조화 데이터이며 SQLite/문맥 인덱스는 파생 조회다.
- 선행: F3 영향 후보와 기존 project/revision·권한/점유 조회. 선행 정보가 없거나 검증되지 않으면 추측하지 않는다.
- 입력→출력: task/role/Step 참조, SourcePin, 승인 scope 참조 → 고정된 projection 계획, 사용 가능/미확인 참조 목록. 처리: commit·graph revision/hash를 대조하고 dirty 상태가 요구되면 검토 지문을 연결한다. 읽을 항목은 F3 관계 및 소비자를 따라 모은다.
- 권한 판단은 기존 공통 권한/lock 경계에 위임한다. role은 어떤 항목을 요약할지 정하는 projection hint다.
- 실패·복구: 기준 불일치, scope 거부, 참조 누락은 `stale`/`blocked` 의미로 반환하고 최신 기준 재조회나 권한 검토를 요청한다.
- 시험: `P3-F5-01` 기준 pin과 영향 참조가 일치하는지, `P3-F5-02` scope 차단·기준 변경에서 누출/오래된 사용이 없는지 확인한다.
- 관찰: 제안 `context.requested`/`context.built`를 DB 원본·Git commit/hash·실제 권한/점유 조회와 비교한다. 실제 event 이름은 미확정이다.
- 완료/소비자: F8은 고정 context/version/scope를, F9는 공유 가능 context 후보를 받는다. SourcePin 검증 전에는 실행 입력으로 소비할 수 없다.

### F5-S2 — 필수 문맥과 유한 budget 구성

- 목적/경계: 소비자가 판단에 필요한 조건을 작은 단위로 읽도록 순서화하고, 기준의 의미를 보존한다.
- 선행: F5-S1 projection, 해당 종류의 필수 입력 정의와 F3 미확인 범위.
- 입력→출력: projection 항목·유한 budget과 단위·형식 버전 → 필수/추가 참조, 포함·생략 manifest, 측정 수준. 처리: 필수 조건, 금지 범위, 완료 기준, 근거, 불확실성을 우선 분류하고 선택 항목만 예산에 맞춰 순서화한다.
- 토큰 측정 능력이 없으면 token 수는 unknown, byte/line 관찰은 별도 추정치로 낸다. 0을 무제한으로 해석하지 않는다.
- 실패·복구: 필수 항목이 예산에 못 들면 incomplete 상태와 필요한 항목 목록을 낸다. 필수 의미를 축약해 완전한 응답처럼 처리하지 않는다.
- 시험: `P3-F5-01` 작은 budget에서 기준·근거 보존 및 상세 추가 조회, `P3-F5-02` 과소 budget에서 필수 누락 명시를 본다.
- 관찰: manifest의 항목 수·단위·누락 이유를 실제 전달 payload 및 원문 ref와 대조한다. 본문 전체는 로그에 복제하지 않는다.
- 완료/소비자: F8/F9는 manifest를 수용해 실행할지 보류할지 판단한다. 조건 누락이면 둘 다 실행 입력으로 사용할 수 없다.

### F5-S3 — 추가 조회 cursor와 short ID

- 목적/경계: 상세 조회를 필요한 만큼 이어가고, 묶음 안에서 짧은 참조를 쓰되 canonical ID는 보존한다.
- 선행: F5-S1의 같은 SourcePin/scope 및 F5-S2 manifest. cursor는 해당 결과 집합의 버전에 결합한다.
- 입력→출력: context ref·추가 항목 요청·유효 cursor 또는 alias → 제한된 상세 조각·다음 cursor·alias mapping 참조. 처리: cursor는 실제 조회 경계·정렬·버전·scope에 결합한다. 이미 폐기한 byte/line 뒤를 가리키는 가짜 cursor는 만들지 않는다.
- short ID는 context 내부 immutable mapping이다. UUID/DB key는 바꾸지 않고 project·revision·mapping version·scope가 다르면 재조회한다.
- 실패·복구: 만료 cursor/alias, 기준 변경, 권한 변화는 거부하고 새 projection을 발급한다. 임의로 과거 alias를 새 ID에 매핑하지 않는다.
- 시험: `P3-F5-02` 만료 alias·scope 변화의 거부, `P3-F5-03` alias 재발급과 token unknown/byte 분리 확인.
- 관찰: 제안 `context.cursor_issued`/`context.alias_resolved`를 조회 범위·반환 byte/line·mapping 원본과 대조한다.
- 완료/소비자: F8/F9는 해당 context 안에서만 alias를 역참조한다. 재개 또는 다른 scope에서 alias 단독 사용은 허용하지 않는다.

### F5-S4 — 재개 상태 재검증 및 handoff

- 목적/경계: 다음 세션에 진행 메타와 다음 행동을 전달하되 요약을 권위 원본으로 승격하지 않는다.
- 선행: 이전 task/run/receipt 참조와 현재 F5-S1 projection. 요약은 선택적 파생 입력이다.
- 입력→출력: 기존 진행/미해결·차단·artifact 참조 → 현재 기준 재검증 결과, 다음 행동·필요 재조회 목록. 처리: source, 권한·owner, lock, receipt/evidence 적용성을 재확인한다. 이전 요약과 달라진 항목은 차이와 확인 필요성을 표시한다.
- 기록에는 전체 conversation이나 credential을 넣지 않고 의미상 결정·목표·미해결·다음 행동의 안전한 참조만 둔다.
- 실패·복구: 원본/evidence 접근 불가 또는 기준 변화는 unknown/stale로 남기고 필요한 부분만 재조회한다. 요약으로 pass를 복구하지 않는다.
- 시험: `P3-F5-03` context/alias 버전 호환과 실제 재검증, 이전 receipt 유효성의 fixture 및 통합 관찰.
- 관찰: 제안 `context.resume_revalidated`의 결과를 실제 Git 기준·점유·evidence hash/receipt와 대조한다.
- 완료/소비자: F8에 현재 유효한 context와 재개 상태를 전달한다. 기존 Phase 2 경로에서 확인되지 않은 재개 capability는 미확인으로 인계한다.

## F6 — 조사·검증 재사용 및 atomic claim

### F6-S1 — 재사용 기준과 provenance 정의

- 목적/경계: 문장 유사성이 아닌 유효 조건으로 조사·검증 결과의 재사용 가능성을 판정한다.
- 선행: F3 영향 결과, F0 version/source·기존 evidence/verification 계약.
- 입력→출력: 의미 정의·대상·입력·환경·도구/의존·baseline·근거 정보 → 버전된 재사용 조건과 누락/unknown 차원. 처리: 정규화 가능한 안정 식별자만 키에 포함하고 각 차원의 의미 버전을 보존한다. 모델 provenance는 모델이 시험 대상일 때에만 포함한다.
- 필수 차원이 미상인 상태를 wildcard 또는 일치로 간주하지 않는다. 무관 차원을 키에 임의 추가하지 않는다.
- 실패·복구: 조건 정의가 불완전하거나 버전이 불명확하면 재사용 불가로 판정하고 기존 기능 경계에서 신규 조사 또는 판단 요청을 한다.
- 시험: `P3-F6-01` 같은 조건의 적합성, `P3-F6-03` 이전 key version·unknown dimension의 silent hit 거부.
- 관찰: 제안 `reuse.lookup`을 저장된 정의/version·환경 및 도구 지문·evidence applicability와 대조한다.
- 완료/소비자: F8/F9는 reusable 여부와 조건/근거 참조를 받는다. key 정의 자체는 메인과 공유 계약 승인을 거친다.

### F6-S2 — 조회와 claim의 원자 경쟁 제어

- 목적/경계: 두 독립 실행기가 같은 조건을 동시에 조사해 중복 실행하는 경쟁을 막는다.
- 선행: F6-S1 key 의미와 기존 SQLite/Queue/lock 및 event/request 규칙.
- 입력→출력: 재사용 조건·독립 event·현재 owner/scope → reusable/active/miss/invalid 결과 또는 단일 신규 claim 결과. 처리: 권한/scope 확인 후 lookup과 reusable claim 또는 신규 claim을 하나의 transaction/CAS 경계에서 조정한다.
- 경쟁 패자는 active 원 run/owner 참조와 대기 조건을 받거나 직렬 대기한다. 독립 사용자 event는 각각 이력에 남는다.
- 실패·복구: transaction 충돌/DB 오류는 재조회 가능한 오류로 반환한다. 중복 신규 claim이나 자동 두 번째 실행으로 복구하지 않는다.
- 시험: `P3-F6-02` 독립 프로세스 barrier 경쟁에서 한 active claim, `P3-F6-03` request 재전송과 독립 event 보존 확인.
- 관찰: `reuse.claimed`/`reuse.shared_active` 제안 이벤트를 실제 SQLite transaction 결과·Queue/lock·물리 실행 횟수와 대조한다.
- 완료/소비자: F8은 claim 소유 또는 active 원 run 상태를 분기 입력으로 받는다. F9는 Step별 key/run 연결을 유지한다.

### F6-S3 — 근거 적용성·만료 판정

- 목적/경계: 과거 성공·진행 작업을 안전하게 사용할 수 있는지, 만료/불일치/미상 조건별로 보수적으로 판정한다.
- 선행: F6-S1 조건과 기존 evidence/receipt, F7 artifact hash 참조가 있을 경우 이를 대조한다.
- 입력→출력: 기존 결과·정의/대상/환경 provenance·evidence/receipt 상태 → reusable/active/miss/invalid/unknown 및 사유. 처리: 정의·대상·baseline·도구/의존성·환경을 각각 비교한다. 후속 실패, 삭제·손상·접근불가 증거는 무효다.
- receipt 존재만으로 pass를 만들지 않는다. expired/unknown/조건 불충족은 hit가 아니다. active는 완료 근거가 아니다.
- 실패·복구: 증거 조회 실패·적용 조건 unknown은 재사용을 거절하고 필요한 재검증 항목을 반환한다.
- 시험: `P3-F6-01` 유효 근거의 원 receipt 추적, `P3-F6-02` 환경 변경·후속 실패·증거 훼손 시 무효화.
- 관찰: `reuse.invalidated` 제안을 실제 파일/resource hash·원 run·criteria verdict 근거와 대조한다.
- 완료/소비자: F8은 miss/invalid 시 신규 실행을 검토하고 unknown은 메인 판단으로 올린다. 자동 완료 승격은 금지한다.

### F6-S4 — 완료 결과와 독립 행동 이력 연결

- 목적/경계: 실제 완료 근거를 후속 조회에 제공하고 사용자 행동의 감사 가능성을 보존한다.
- 선행: F6-S2 claim, F6-S3 적용성 판정, 기존 2단계의 정규 receipt/evidence 및 runner 결과. F7의 축약 결과 연계는 F8/F10에서 후속 확인하며 F6의 독립 완료 선행으로 강제하지 않는다.
- 입력→출력: 원 run/claim·Step·receipt·정의별 verdict → 재사용 가능한 결과 참조 또는 invalidation/미해결 참조. 처리: 상태/결과/근거 참조를 공통 저장 경계에 연결하고, 이력은 event 단위로 유지한다. 실패 이후 이전 성공의 적용 범위를 재평가한다.
- 결과 본문을 재사용 index에 복제하지 않는다. 문장·결과만으로 evidence applicability를 생략하지 않는다.
- 실패·복구: 필수 receipt/evidence 저장이 실패하면 재사용 가능으로 게시하지 않고 reconcile/review 상태를 남긴다.
- 시험: `P3-F6-01` 재사용 ref가 원 receipt와 일치, `P3-F6-02` 후속 실패 및 별도 event 보존, `P3-F6-03` 이력/정의 버전 대조.
- 관찰: `reuse.completed` 제안과 DB 이력·원 evidence hash·실제 완료 상태를 대조한다.
- 완료/소비자: F8/F9에 재사용 범위와 기준별 제한을 전달하고, F10은 중복 실행 감소와 정확성을 함께 측정한다.

## F7 — 도구 출력 요약·증거 참조

### F7-S1 — 출력 수집 및 실제 artifact 보존

- 목적/경계: 도구 결과를 축약하기 전에 실제 출력을 안전한 artifact 경계에 보존하고 식별한다.
- 선행: F0 resource/evidence 경계, 기존 runner/CLI 출력 수집 계약.
- 입력→출력: 실제 stream/file 결과·tool/run refs·exit/status·기준 ref → 저장 artifact와 hash, 크기/형식/보존 상태 참조. 처리: 실제 수신한 artifact를 안전 정책에 따라 저장하고 hash를 계산한다. 저장 성공 전에는 artifact 존재로 간주하지 않는다.
- 전체 대화·credential·전체 환경변수를 증거/로그에 복제하지 않는다. 원본 보존 정책은 메인 소유 계약을 따른다.
- 실패·복구: 저장 오류는 명시하고 receipt 완료를 막는다. partial file이면 실제 길이/hash를 보존하거나 불완전 상태로 정리한다.
- 시험: `P3-F7-01` 정상 출력의 hash/참조, `P3-F7-02` 저장·정제 오류 및 민감 fixture 격리를 확인한다.
- 관찰: 제안 `tool_result.received`/`tool_result.persisted`를 실제 파일/resource hash·길이와 대조한다.
- 완료/소비자: F8/F9는 실물 artifact ref와 저장 상태를 받아야 요약 결과를 receipt에 연결할 수 있다.

### F7-S2 — 정제 요약과 기준 판정 분리

- 목적/경계: 호출자에게 짧은 결과를 주면서 도구의 성공 주장과 독립 acceptance 판정을 구분한다.
- 선행: F7-S1 저장된 artifact와 실제 exit/status, 검증 기준 참조.
- 입력→출력: artifact metadata·기준 refs·redaction 정책 → 사실 요약·실제 상태·관찰 증거·기준 판정 출처. 처리: plain success summary와 증거의 관찰 가능한 사실을 구분한다. 기준 판정이 없거나 근거가 없으면 pass 대신 미상/검토 필요로 전달한다.
- 위험 문자열/비밀은 승인된 정제 정책으로 제거하거나 해당 요약 자체를 보류한다. 정제 실패를 숨기지 않는다.
- 실패·복구: 정제/criteria mapping 실패는 요약 불완전으로 남기고 안전한 원 ref 또는 수동 검토를 요구한다.
- 시험: `P3-F7-01` 요약과 원본/exit 일치, 성공 summary만으로 pass가 아닌지, `P3-F7-02` 민감 fixture 처리 확인.
- 관찰: `tool_result.redacted`/`tool_result.summarized` 제안과 실제 전달 payload·원 artifact·criteria verdict source를 비교한다.
- 완료/소비자: F8/F9는 사실·status·evidence ref를 소비하며 criteria pass는 독립 검증이 있을 때만 소비한다.

### F7-S3 — 제한된 line/byte 상세 조회

- 목적/경계: 큰 출력에서도 저장된 원본의 실제 부분을 제한해 읽고, 조회 이어가기를 정확히 표현한다.
- 선행: F7-S1의 실제 artifact/hash와 안전한 권한 확인.
- 입력→출력: evidence ref·line/byte 한도·유효 위치 → 실제 저장 구간·경계 metadata·가능한 다음 위치. 처리: 각 조각의 시작/끝 offset을 실제 보존 artifact의 byte/line과 연결하고 content ref/hash를 보존한다. chunk는 제한 크기 안에서 경계가 명확해야 한다. 조회 가능 범위는 보존되고 권한상 읽을 수 있는 실제 범위로만 한정한다.
- 버린 원본 구간을 나중에 재조회할 수 있다고 가정하지 않는다. 보존되지 않은 구간은 복구 불가 생략으로 명시하고 cursor를 발급하지 않는다. cursor는 보존된 chunk/hash의 실제 available range에만 발급하며 텍스트 인코딩 경계는 원본 offset으로 확인한다.
- 실패·복구: artifact 변경/hash 불일치·권한 불가·범위 초과는 cursor 거부와 최신 artifact 재확인으로 처리한다.
- 시험: `P3-F7-02` 큰 출력/부분 실패 cursor, `P3-F7-03` text/json/binary 및 unsupported 형식 구별 확인.
- 관찰: `tool_result.detail_requested`와 요청 범위·실제 byte/line·반환 content ref/hash를 대조한다.
- 완료/소비자: F8/F9는 후속 진단이 필요할 때 상세 조각 참조를 요청한다. 요약만으로 원문 내용이 입증되었다고 간주하지 않는다.

### F7-S4 — receipt 연결 및 형식 미지원 보존

- 목적/경계: 요약·artifact·실행 상태를 해당 Step receipt에 연결하고 불완전/opaque 결과를 숨기지 않는다.
- 선행: F7-S1~S3와 기존 receipt schema/runner result. 공통 receipt 변경은 메인 승인 대상이다.
- 입력→출력: run/Step·요약·artifact hash/ref·exit/status·format capability → receipt 연결 결과와 누락/미지원 사유. 처리: staging artifact를 실제 저장소에 게시한 뒤 hash를 재계산하고 재생(replay) 가능한 참조로 receipt에 연결한다.
- 형식·정제·조회 capability가 unknown/unsupported이면 결과도 unknown/unsupported다. 빈 결과나 opaque 응답을 성공으로 채우지 않는다.
- 실패·복구: artifact 게시와 DB receipt 사이 장애는 기존 journal/요청 ID로 재조정한다. 원본/해시 확인 전 완료를 선언하지 않는다.
- 시험: `P3-F7-03` capability 호환, `P3-F7-02` 저장 오류 복구, `P3-F7-01` receipt와 실제 종료 코드/hash 대조.
- 관찰: 실제 staging/최종 artifact·재계산 hash·SQLite receipt·프로세스 exit code를 event 제안과 대조한다.
- 완료/소비자: F8/F9는 receipt ref 및 criteria 출처를 전달받는다. F10은 원본/요약 전달비용과 기준 정확성을 함께 평가한다.

## F8 — Python 단일 실행 제어 및 진행 관찰

### F8-S1 — intent 분류와 실행 경계 선택

- 목적/경계: 실제 native intent를 main이 호출할지, 확인된 CLI 프로세스를 Python이 구동할지 경계를 분명히 선택한다.
- 선행: F5 유효 context/scope, F6 lookup 결과, F7 result 경계, 기존 Phase 2 runner 계약.
- 입력→출력: 단일 Step intent·role/context·scope·재사용 상태·runner capability → 실행/대기/재사용/판단 필요 분기와 로컬 handoff 참조. 처리: native 경로는 Python이 intent·기록을 준비해 main 실제 도구 호출을 기다린다. CLI 경로는 확인된 로컬 실행기만 대상으로 한다.
- Python이 실행하지 않은 native 호출을 시작/완료로 기록하지 않는다. 직접 모델 API와 Host coding 실행은 금지한다.
- 실패·복구: capability 불명확·실행기 미설치·권한 부족은 unsupported/blocked로 표시하고 개별 판단 경로를 제공한다.
- 시험: `P3-F8-01` 단일 정상 intent, `P3-F8-03` 기존 native/CLI 실제 capability snapshot과 현 Phase 2 한계 비교.
- 관찰: 제안 `control.observation_started`를 실제 intent, 사용자/main tool action 또는 로컬 process handle과 대조한다.
- 완료/소비자: F9는 확인된 F8 단일 실행 경로와 실제 capability만 확장 입력으로 받는다.

### F8-S2 — 상태 관찰과 60초 표시

- 목적/경계: Python이 반복 관찰을 처리하고 UI 표시 또는 명시 조회를 60초 내 제공한다. 매 poll마다 모델 turn을 시작하지 않는다.
- 선행: F8-S1의 실제 handle 또는 native 결과 참조, 기존 run/attempt/Queue/lock 권위 상태.
- 입력→출력: 실행/handle refs·관찰 시각·현재 DB/runner 상태 → 상태 변화·다음 관찰 정보·표시/query 결과. 처리: 로컬 controller가 DB/runner를 주기적으로 읽고 차이를 저장한다. 화면 갱신을 지원하면 표시하고, 미지원이면 로컬 기록과 explicit query fallback을 제공한다.
- UI unsupported는 명시 기록한다. Python 내부 poll이 모델 turn이나 사용자에게 보이는 가짜 자동 갱신인 것처럼 표현하지 않는다.
- 실패·복구: 관찰 timeout/세션 닫힘은 완료/취소/lock 해제 근거가 아니다. 다음 query/reopen에서 최신 원본 상태를 재조정한다.
- 시험: `P3-F8-01` controlled clock에서 60초 내 표시 또는 query 경로와 AI 호출 계측, `P3-F8-03` UI capability 확인.
- 관찰: `control.state_observed`/`control.progress_published` 제안과 DB row·프로세스 status·화면/query 응답 시각을 대조한다.
- 완료/소비자: main에는 의미 있는 변화·오류·판단/완료 때만 알린다. F9는 handle 및 마지막 관찰을 받는다.

### F8-S3 — 알림·취소·제한 retry

- 목적/경계: 의미 있는 변화만 main 판단으로 올리고 취소/재시도를 확인된 사실로 처리한다.
- 선행: F8-S2 관찰과 공통 retry·run state·lock 계약.
- 입력→출력: 상태 변화·오류 분류·취소 요청·retry policy → 알림 필요/허용된 retry/취소 미확인·재조정 상태. 처리: 알림은 상태 변화·오류·완료·판단 요청에만 발생한다. 허용 retry는 총 최대 2회 및 실행기 한도 규칙을 지킨다.
- 재시도 전 미시작/종료와 오류 종류를 확인한다. 시간 경과, timeout, 전달 실패만으로 retry/cancel/lock release하지 않는다.
- 실패·복구: 실행 시작 결과 unknown, cancel 경쟁, 알림 실패는 reconcile/review를 요구하고 물리 handle의 현재 상태를 다시 읽는다.
- 시험: `P3-F8-02` 미응답·시작 불명·cancel·통지 실패에서 blind retry/조기 해제 방지, `P3-F8-01` poll당 AI 호출이 없음을 계측.
- 관찰: `control.ai_notified`/`control.retry_decided` 제안과 호출 이력·attempt row·handle·lock owner를 대조한다.
- 완료/소비자: F9에 terminal/review state와 취소 영향이 전달된다. 미확인 실행은 batch 재사용/완료 근거가 아니다.

### F8-S4 — receipt 조정·안전한 handoff

- 목적/경계: 실제 runner 결과를 F7 receipt와 공통 상태에 맞추고 다음 동작에 넘긴다.
- 선행: F8-S1~S3의 단일 실행, F7 evidence/receipt 참조, 기존 owner/lock 계약.
- 입력→출력: 실제 process/native 결과·status·exit·receipt → 확인된 상태 변화·재조정 필요성·F9 후보 인계. 처리: 외부 결과를 확인한 뒤 상태/receipt 연결을 저장하고 공통 전이 규칙을 호출한다. 상태 기록 전에 Done으로 판단하지 않는다.
- handle 미확인·receipt 저장 실패면 실행 결과를 사실로 보존하면서 scope를 보호한다. 별도 재실행으로 덮지 않는다.
- 실패·복구: 늦은 결과는 원 run에 연결하고 현재 plan/status의 자동 완료로 승격하지 않는다. journal 기반 reconcile을 요청한다.
- 시험: `P3-F8-02` receipt/상태 저장 경계 장애, `P3-F8-03` native/CLI 관찰 가능성, `P3-F8-01` 정상 종료 대조.
- 관찰: DB run/attempt·Queue/lock·실제 handle 종료·F7 receipt/hash 및 제안 `control.reconcile_required`를 비교한다.
- 완료/소비자: F9는 검증된 single-Step capability/handle 수명만 받는다. F10은 실제 AI turn·시간·비용과 안전 판정을 받는다.

## F9 — 관련 Step 논리 묶음

### F9-S1 — 후보 관계·cycle·실행 대상 평가

- 목적/경계: 공유 context·모듈을 가진 비충돌 Step을 찾고 안 되는 묶음은 실행 전에 분리한다.
- 선행: F5 공유 context/version/scope 후보, F8의 검증된 단일 경로/capability, 기존 의존 관계.
- 입력→출력: Step refs·각 directive version/hash ref·완료기준·dependency/conflict 관계 → eligible/split/blocked 판정과 이유. 처리: dependency cycle, 선행 미완료, 충돌, 권한 차이, scope 불일치를 검사한다. 순환은 거부하며 애매하면 개별 실행으로 분리한다.
- 같은 프롬프트에 여러 logical Step을 두는 앱 수준 묶음은 가능하다. 별도 vendor batch API를 전제하지 않는다.
- 실패·복구: 결과를 Step별로 받지 못할 capability는 실행 전 분리하거나 명시적인 수동 검토로 보낸다.
- 시험: `P3-F9-01` 비충돌 공유 context 후보, `P3-F9-02` cycle/conflict 거부, `P3-F9-03` 실제 runner capability 확인.
- 관찰: `batch.evaluated` 제안과 실제 dependency graph·context/scope·capability snapshot을 대조한다.
- 완료/소비자: F9-S2는 승인된 후보만 받는다. F8 구현에 F9를 선행으로 추가하지 않는다.

### F9-S2 — group nonce와 per-Step directive 고정

- 목적/경계: 실행 시점에 그룹·멤버·공유 문맥과 각 Step의 고유 지시를 서로 바뀌지 않게 묶는다.
- 선행: F9-S1 eligible 결과, 최신 F5 context와 각 Step별 승인 지시 참조.
- 입력→출력: group 결정·멤버 refs·context pin·Step directives → 새 group nonce, 고정된 멤버/directive 참조 및 실행 payload 참조. 처리: 매 독립 행동은 새 request/event 식별을 따른다. per-Step 지시 hash/version 및 공유 context pin을 그룹 구성 시 고정한다.
- 같은 요청 재전송만 idempotent하다. stage 중 route가 바뀌거나 멤버를 재사용해 다른 그룹으로 합치지 않는다.
- 실패·복구: payload 구성/전달 실패는 실행 전 상태로 남기고 새 구성은 새 nonce로 시작한다. partial 전달 여부 unknown이면 재전달 전에 reconcile한다.
- 시험: `P3-F9-01` 실제 한 메시지의 per-Step 지시/고정 context 대조, `P3-F9-03` native/CLI 수용 형식 확인.
- 관찰: `batch.created` 제안과 전달 payload ref/hash, member/directive/context pin을 실제 실행 기록과 비교한다.
- 완료/소비자: F9-S3에 고정 group만 전달한다. 공통 operation/event 필드와 DB 표현은 `implementation-interfaces.md`의 논리 의미를 따르며, 실제 schema/API 확정은 메인 승인을 기다린다.

### F9-S3 — 원자 소유권·단일 물리 handle

- 목적/경계: 여러 논리 child를 물리 실행 슬롯과 scope/owner 소유권을 우회하지 않고 한 실제 실행으로 전달한다.
- 선행: F9-S2 고정 그룹, F8 단일 runner 경로, 공통 runner·owner group/scope lock 계약.
- 입력→출력: group nonce·child Step refs·scope union·runner capability → 원자 확보 결과, parent/child binding 및 실제 handle ref. 처리: owner group과 관련 scope union을 실행 직전 원자적으로 확보한다. 일부만 확보되면 실행하지 않고 전체를 되돌린다.
- 물리 handle 1개는 실제 slot 1개다. logical child가 여럿이어도 별도 slot/동시성/소유권 우회가 아니다.
- 취소·상태는 실제 handle과 child binding에 연결한다. stage 중 재사용되거나 route를 바꾸는 실행은 허용하지 않는다.
- 실패·복구: bind/소유권/실행 사이 결과가 unknown이면 lock을 보존하고 공통 reconcile로 실제 handle 유무를 확인한다.
- 시험: `P3-F9-02` scope union 경쟁/부분 확보에서 실행 차단, `P3-F9-01` handle 하나·slot 하나 대조.
- 관찰: `batch.step_bound` 제안과 DB lock owner·실행기 process/native handle·child mapping·물리 slot 계측을 비교한다.
- 완료/소비자: F9-S4에 확인된 binding/handle만 인계한다. 실제 소유권·완료 판정은 공통 runner와 메인 경계다.

### F9-S4 — per-Step 결과·부분 실패·whole cancel

- 목적/경계: 개별 logical Step 결과와 증거를 보존하고 group 상태를 Step들의 단일 성공으로 축약하지 않는다.
- 선행: F9-S3 handle/child binding 및 F7 per-Step artifact/receipt 경계.
- 입력→출력: 실제 구조화 결과·각 child ref·취소 범위·evidence → Step별 status/result/receipt 및 group 미해결/부분 결과. 처리: 응답을 member identity와 매칭하고 criteria/evidence를 Step 단위로 검증한다. 한 child의 누락/unknown은 다른 확인된 결과를 지우지 않는다.
- opaque response, 누락된 child result, evidence 불일치면 해당 Step을 unknown/review로 보존한다. group 전체 Done으로 올리지 않는다.
- whole-group 취소는 실제 active child/handle의 영향 범위와 다른 main 판단 대상의 취소 필요를 구분하고 결과를 기록한다.
- 결과를 Step별로 가져올 수 없으면 다음부터 별도 실행으로 분리한다. 자동으로 새 tool call을 만들지 않는다.
- 시험: `P3-F9-01` per-Step 결과·취소 범위, `P3-F9-02` 부분/누락/opaque 및 whole Done 금지, `P3-F9-03` 구조화 회수 미지원 시 분리.
- 관찰: `batch.step_result_recorded`/`batch.review_pending` 제안과 실제 runner response·F7 artifact/hash·각 SQLite receipt/status·취소 handle을 대조한다.
- 완료/소비자: F10은 child별 비용·품질·결과와 split 이유를 받는다. 각 Step의 terminal 상태가 개별 확인될 때만 기존 공통 완료 판단에 넘긴다.

## 시험·인계 매핑과 완료 판단

| 기능 | 예정 시험 ID | 이 문서의 관찰 초점 |
|---|---|---|
| F5 | `P3-F5-01`, `P3-F5-02`, `P3-F5-03` | source/scope, 필수 manifest/cursor, alias·재개 재검증 |
| F6 | `P3-F6-01`, `P3-F6-02`, `P3-F6-03` | 실제 조건/key provenance, 독립 프로세스 atomic claim, receipt·event |
| F7 | `P3-F7-01`, `P3-F7-02`, `P3-F7-03` | artifact/hash replay, 정제/line-byte 조회, exit/status/capability |
| F8 | `P3-F8-01`, `P3-F8-02`, `P3-F8-03` | native vs CLI 경계, 60초 화면/query, AI 호출·retry·handle/lock |
| F9 | `P3-F9-01`, `P3-F9-02`, `P3-F9-03` | 앱 수준 다중 지시, 원자 scope, physical handle, per-Step 회수 |

- 표의 모든 시험은 예정 ID다. 실제 실행 전후의 계층(fixture/core/제품), 기준 commit/dirty, 환경·실행기/model provenance, 종료 코드, 증거 ref, SQLite/Git/프로세스/응답 대조를 기록한다.
- 이벤트 이름은 명세의 제안이다. 공통 schema, 공개 operation, 소유권 전이, 오류 envelope의 최종 이름과 호환성은 메인이 확정한다.
- 각 기능은 pass/fail/blocked/not_run으로 보고하고 capability·근거·조건이 미확인인 값은 unknown으로 보존한다. fixture는 제품 실측 통과를 대체하지 않는다.
- F5→F8→F9는 versioned context/scope/run/receipt ref로 이어진다. F6 재사용 판단과 F7 증거도 같은 독립 event 및 Step에 연결한다.
- F8 단일 Step이 먼저 끝나고 검증되어야 F9 group 확장에 착수한다. 의존 cycle, 부분 scope 확보, 재사용 stage 변경, 물리 slot 중복 계산을 허용하지 않는다.
- 메인 인계에는 기능별 결과, 실제 수정/시험 범위, 저장소 dirty/commit, 수행하지 못한 시험, 공통 계약/구현 인터페이스 이견을 포함한다.
- 구현 전체 완료 판정은 메인 소유다. 이 계획은 구현 착수의 제안이며 현재 operation의 설명이나 성공 기록이 아니다.
