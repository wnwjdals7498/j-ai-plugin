# F5–F9 작업 문맥·재사용·실행 명세

2026-10-02 작성. **예정 구현 규격**이며 구현·시험 성공을 뜻하지 않는다. 공통 필드·오류·revision·저장/실행 경계는 [F0 공통 계약](contracts.md)을 따른다. 공개 operation/event registry 최종 승인은 메인 소유다.

## 공통 기준

- 원본·상태·권한·시험 보고 원칙은 [PMT 작업 규칙](../../AGENTS.md), [문맥 효율화 계약](01-document-context-efficiency.md), [3단계 의존성](README.md), [전체 계획](../03-hosted-storage.md), [2단계 실행](../phase2/execution-work.md), [2단계 검증](../phase2/verification.md)을 따른다.
- F5/F6의 직접 선행은 F3 영향 계산이다. F7은 F0 이후 독립 가능하다. F8은 F5·F6·F7 이후, F9는 F5·F8 이후다.
- 공통 저장 상태/전이·요청 멱등·이벤트 최종 필드·잠금·runner receipt는 메인(F0/F10)이 소유한다. 이 명세는 확정 계약을 대신하지 않는다.
- Git 구조화 데이터셋은 의미 원본, SQLite는 실행/조회 상태의 원본이다. 문맥·요약·키·짧은 ID는 재생성 가능한 파생물이다.
- 기준별 판정은 `pass`, `fail`, `blocked`, `not_run`이다. `unknown`은 capability·문맥·적용성 등의 미확인 정보이며 판정 성공이나 별도 완료 상태로 사용하지 않는다.
- 로그는 안전한 참조·지문·계량만 남긴다. 전체 대화, Step 원문, 비밀, 전체 환경 변수를 복제하지 않는다.
- 아래 시험 ID는 실제 시험 예정이다. 구현 전 통과로 표시하지 않으며 fixture 결과와 실제 제품 결과를 분리한다.
- 필수 공통 시험/manifest는 [시험·인계 명세](verification.md)의 기능 ID로 연결한다. 세부 이벤트 이름은 아래 초안으로 제안하며 registry 변경은 메인이 확정한다.

## F5 — 역할 문맥·세션 재개·짧은 ID

### 목적과 범위

- 목적: 역할과 Step이 허용된 범위에서 필요한 판단 기준·관계·근거만 읽고, 새 세션도 현재 상태를 안전하게 재확인한다.
- 이유: 전체 graph·문서·대화를 매번 보내면 비용이 늘고 핵심 기준 누락이나 오래된 판단 혼용이 생긴다.
- 추가: scoped context 구성, 유한 예산, 추가 조회 cursor, 역할/Step short reference, 버전 결합 매핑, 재개용 메타/의미 요약.
- 수정: 기존 context 조회가 F3 영향 후보 및 현재 revision·scope·근거 상태를 포함하도록 확장한다.
- 삭제: 무제한 전체 전달, 임의 요약으로 필수 기준을 대체하는 동작.
- Goal: 작은 조회로도 필수 기준·금지 범위·불확실성·다음 행동을 보존하고, 더 필요하면 정확한 부분을 이어 읽는다.
- Non-goal: 의미 판단을 요약기로 대체하거나, 재개 요약을 근거·원문·현재 권한으로 취급하는 것.

### 입력과 출력 의미

- 입력 대상: project/repository, 현재 commit/dirty 지문·graph revision, task/role/Step 참조, 승인된 scope·authorization pins, 관련 F3 영향 후보.
- 입력 요청: finite `budget`; 지원되는 경우 토큰 단위, 미지원이면 `unknown` 토큰과 별도 byte estimate를 반환한다.
- 입력 상세: 필요한 기준/근거 종류, cursor, context/summary 형식 버전, 이전 묶음 참조는 선택적 힌트다.
- 타입/제약: budget은 단위가 명시된 유한 양의 수여야 한다. 숫자 0을 무제한으로 해석하지 않는다.
- 타입/제약: role은 문맥 투영용 모델 역할이고 scope는 검증된 접근/점유 범위다. 최상위 역할 선택만으로 접근 권한이 늘지 않는다. 단축 ID로 authorization을 만들거나 넓히지 않는다.
- 타입/제약: 저장된 짧은 ID는 project·graph/revision·mapping version에 묶인 immutable mapping이다.
- 출력: 필수 항목·관련 참조·불확실/미확인·생략 목록·다음 cursor·budget 측정 수준·context version을 반환한다.
- 출력: budget 부족 시 필수 항목을 자르거나 성공처럼 내보내지 않는다. 부족한 항목을 표시하고 `get more` cursor를 제공한다.
- 출력: 짧은 ID는 해당 context 묶음 안의 alias이며 고정 UUID·DB key를 대체하지 않는다.
- 실패: 기준 변경·mapping 만료·권한 변경·cursor 불일치면 stale/conflict로 재조회 요청. 예전 alias로 조용히 변환하지 않는다.
- 실패: 필수 기준을 수용 가능한 예산으로 제공할 수 없으면 incomplete/blocked를 반환하고 자동 축약 실행을 금한다.

### 방법과 기술 선택

- Python이 필요한 필드·참조·상속 관계와 역할별 필터를 정형화하고 예산을 적용한다.
- F3 후보에서 scoped context를 만든 뒤, 먼저 요약·ID·근거 지문을 보내고 추가 cursor로 필요한 원문/근거만 읽는다.
- 비용 이유: 토큰 수를 제공하는 제품은 실제 입력/출력·캐시 토큰을 기록하고, 미지원이면 토큰 `unknown` 및 byte estimate를 따로 기록한다.
- byte estimate는 실측 토큰 또는 요금으로 표시하지 않는다. context 생성·전달·추가 조회의 비용도 계측한다.
- alias mapping은 버전 경계가 있는 불변 레코드다. project/graph/role/scope가 달라지면 기존 alias를 재사용하지 않는다.
- 재개는 Python이 작업·실행·승인된 artifact 참조의 메타 요약을 만든다. AI 의미 요약은 실제 결정/목표/미해결/다음 행동에 필요한 경우만 증분 생성한다.
- 의미 요약 생성 비용을 입력/출력 규모·호출 수·시간으로 별도 계측하고 총 효율 측정에 포함한다.
- 재개 시 summary만 믿지 않고 원본/증거, 현재 Git 기준, revision, 점유자·승인 scope, 실행 receipt 유효성을 다시 확인한다.
- 기술은 Python/SQLite 파생 인덱스를 우선한다. 이유는 기존 로컬 저장·버전 원칙과 재구축 가능성을 유지하기 위해서다.

### 경계·시험·완료

- 선행: F3 영향·불확실 범위. 소비자: F8 실행 제어, F9 묶음 배정, F10 통합 측정.
- F5는 역할별 문맥 조립을 소유한다. 인증·scope 정책과 잠금 권한은 메인/공통 계약 소유다.
- `P3-F5-01` 정상: 작은 finite budget으로 기준/근거·요약 먼저, cursor로 상세 추가 조회; 실제 필수항목과 반환 순서를 대조한다.
- `P3-F5-02` 안전 실패: 부족 예산·scope 거부·기준 변경·만료 alias를 주입; 자르기/권한 확장/오래된 참조 허용이 없음을 확인한다.
- `P3-F5-03` 형식 호환: token capability 있음/없음, context 버전·alias 재발급을 확인; token unknown과 byte estimate를 분리한다.
- 대조 이벤트 초안: `context.requested`, `context.built`, `context.cursor_issued`, `context.alias_resolved`, `context.resume_revalidated`.
- 안전 추적값: request/task/role refs, scope pin ref, source revision/hash, context/mapping version, budget unit/limit, actual token source or unknown, byte estimate, omitted-required count, cursor ref, outcome.
- 로그를 실제 DB·Git 기준·권한 평가·전달 payload 크기 및 receipt와 대조한다. 본문은 기록하지 않는다.
- 완료: 필수 정보 보존, 추가 조회, 무효 alias 거부, 재개 시 원본/증거 재확인 및 생성 비용 계측 증거가 있다.
- 인계: F8에 scope-bound context ref·version·필수 항목/생략·cursor·재검증 결과를 준다. F9에는 동일 묶음에서 공유 가능한 context version을 준다.
- 자율 범위: 승인된 scope 내 필드 정렬·요약 길이·cursor 페이지 크기. 필수 기준 생략 규칙·권한·alias 안정성 변경은 메인에 보고한다.
- 완료 보고는 ID별 실행 상태·명령/도구·실제 환경·종료 코드·DB/Git/receipt 대조·증거 ref·미해결 사항을 포함한다.

## F6 — 조사·검증 재사용과 동시 중복 방지

### 목적과 범위

- 목적: 적용 조건이 정확히 같은 조사/검증은 유효 근거를 재사용하고, 동시에 시작된 동일 작업의 이중 실행을 방지한다.
- 이유: 반복 탐색은 모델 비용·시간을 늘리고, 서로 다른 실행 결과를 동일 근거처럼 혼합할 수 있다.
- 추가: 재사용키, 근거 applicability 판정, lookup 및 atomic claim, 진행 중 execution 참조 공유.
- 수정: 기존 지식/검증 cache와 Queue 연계를 대상·환경·기준·도구 provenance까지 구별한다.
- 삭제: 문장 유사성만으로 재사용, 실행 중인 원 작업을 취소해 시간을 회수한 것처럼 보고, 서로 다른 사용자 이벤트 병합.
- Goal: 정확한 조건의 완료 결과를 재사용하고, 동시에 조회/claim해도 한 active execution만 생성한다.
- Non-goal: 사용자 독립 요청을 합치거나, 결과 요약만으로 검증을 통과시키는 것.

### 키·입출력 의미

- 재사용키 정의: canonical 조사/검증 정의와 버전 + 대상 식별/버전 + 기준 commit/dirty 또는 데이터 지문 + 입력/범위 지문 + 환경 지문 + baseline/tool/dependency의 적용 조건과 관련 provenance. 실행한 모델은 출처 metadata로 남기고, 모델 자체가 조사/검증 대상일 때만 model 식별을 필수 재사용 조건에 넣는다.
- 키 구성은 순서·기본값·정규화 규칙이 고정된 버전형 tuple을 hash한다. 숨은 조건은 key 밖의 applicability 조건으로 저장하고 판정한다.
- baseline/tool/dependency 조건에는 검증을 바꿀 수 있는 실제 버전/설정/실행기 capability를 넣는다. 제공되지 않으면 `unknown`; 임의 호환 취급 금지.
- 입력: definition ref/version, 대상·환경·입력 지문, 도구/의존성/baseline 지문, 적용/무효화 조건, event/request ID, 기존 근거/실행 참조.
- 출력: exact key version, lookup 결과(reusable / active / miss / invalid / conflict), 근거/receipt ref, 재사용 사유 또는 새 atomic claim 결과.
- 출력의 reusable은 적용조건 일치와 증거 유효성을 의미할 뿐 상위 요구의 pass가 아니다.
- 같은 request 재전송은 공통 idempotency 계약을 따른다. 서로 다른 `event_id`의 독립 사용자 요청은 동일 문장·키여도 이벤트로 보존한다.
- active 결과를 공유할 때는 원 run 참조·상태·owner와 scope 조건을 반환한다. active 실행이 이미 소비한 시간을 회수했다고 표시하지 않는다.
- 실패/무효: 대상·환경·baseline·tool/dependency 변동, 정의 변경, 후속 실패, 증거 삭제/손상/접근 불가면 재사용 불가다.

### 처리·소유 경계

- 순서는 현재 권한/범위 검사 → 읽기 lookup → 재사용 claim 또는 새 claim의 단일 원자 동작 → 결과 저장 → 소비자 통지다.
- 읽기 lookup과 새 claim 사이 경쟁은 DB transaction/CAS 또는 확정된 공통 원자 claim으로 막는다. 경쟁 패자는 existing active ref를 받거나 직렬 대기한다.
- 서로 다른 키는 독립 실행 가능하되 공유 scope lock 규칙을 따른다. 공통 lock/Queue의 정합성은 F6가 재구현하지 않는다.
- 이유: exact provenance만 비교하고, 동시 경쟁도 기존 SQLite/Queue/lock 규칙으로 추적 가능하게 한다.
- 증거 본문은 원본 resource에 두며 F6는 지문·적용 조건·receipt 참조를 관리한다.
- 선행: F3 영향 결과 및 기존 2단계 evidence/verification·lock. 소비자: F8, F9, F10.
- F6는 재사용 key/applicability 정의 제안과 lookup/claim 사용을 소유한다. 공통 schema·lock 전이는 메인이 확정한다.

### 시험·로그·완료

- `P3-F6-01` 정상: 동일 정의·대상·환경·baseline/tool/deps·증거가 유효하면 재사용 ref를 돌려 실행 횟수와 원 receipt를 확인한다.
- `P3-F6-02` 안전 실패: 독립 프로세스에서 동시 exact claim, 조건 한 가지 변경, 후속 실패·증거 손상을 재현; 단일 active claim, 무효화, event 보존을 확인한다.
- `P3-F6-03` 형식 호환: provenance 필드 추가/이전 정의 버전·unknown capability를 확인; 버전 경계를 넘는 silent hit가 없어야 한다.
- 대조 이벤트 초안: `reuse.lookup`, `reuse.claimed`, `reuse.shared_active`, `reuse.invalidated`, `reuse.completed`.
- 추적값: key schema/version, safe dimension fingerprints, event/request refs, lookup outcome/reason, claim/run/owner refs, evidence/receipt ref, transaction result, wait reason.
- 실제 SQLite 독립 프로세스/경합 결과·Queue/lock 행·실제 실행 횟수·receipt와 이벤트를 대조한다. 로그 내용으로 단일 실행을 추정하지 않는다.
- 완료: 적용 조건 불일치/손상 시 재사용 거부, 동시 claim에서 단일 active run, 서로 다른 사용자 이벤트 보존, evidence 연결이 입증된다.
- 인계: F8에 miss/active/reusable·원 run/근거 ref·조건·검증 제한을 전달한다. F9는 Step별 key·run mapping을 유지한다.
- 자율 범위: 계약 내 key canonicalization 및 안전한 원자 claim 구현. 신규 provenance 기준·cross-scope 합침·자동 무효화 영향 확대는 메인 결정이다.
- 완료 보고는 시험별 상태·프로세스/환경·종료 코드·SQLite/receipt 대조·증거 ref·미해결 쟁점을 포함한다.

## F7 — 도구 결과·로그 축약

### 목적과 범위

- 목적: 실제 실행 증거를 보존하면서 AI/호출자에는 판단에 필요한 요약과 직접 조회 가능한 근거 참조만 전달한다.
- 이유: 큰 성공 출력·전체 로그를 반복 전달하면 비용과 민감정보 노출이 늘며, 결과 요약이 실제 결과와 혼동될 수 있다.
- 추가: 정제된 결과 요약, evidence/resource 보관 참조, 실패 구간·추가 조회 cursor.
- 수정: 기존 CLI/도구 출력 수집과 Step receipt가 요약·원본 참조·보존 정책을 갖도록 한다.
- 삭제: 성공 때 원문 전체 전달, 요약을 검증 기준 통과로 해석, 로그 실패 은폐.
- Goal: 짧은 반환과 원 증거 추적을 동시에 제공한다.
- Non-goal: 기준 판정, 원본 증거 삭제, 민감한 원응답의 무제한 보존.

### 계약·기술

- 입력: run/Step/tool refs, 원 출력 또는 stream/resource ref, 완료 기준 refs, 민감값 정제 정책·보존 class, 호출자의 상세 요청.
- 입력 타입: 성공/실패/부분 결과 구분, exit/status·시간·환경·대상 지문, evidence locator를 포함할 수 있다. 없는 값은 미상으로 둔다.
- 출력: 간결한 사실 요약, 실행 상태, 종료 코드/기준별 관찰, evidence ref/hash, 생략량/추가 조회 cursor, 정제/보존 결과.
- 성공 요약은 “실행 완료”만 전달할 수 있다. acceptance criteria 충족은 독립 검증 결과와 근거가 있어야 한다.
- 실패 출력은 관련 오류·로그 구간·재현/다음 관찰 ref만 우선 보이고, 원문은 권한 검증 후 cursor로 읽는다.
- 원본 대화·토큰·비밀·전체 환경 변수를 로그나 사용자 요약에 복제하지 않는다. 오류 정제 실패도 별도 기록한다.
- Python은 byte/line 상한과 safe redaction 후 evidence resource에 저장하고 receipt에 참조를 연결한다. 상한 초과는 명시적 truncation+cursor다.
- 기술 선택 이유: 기존 SQLite receipt·resource 및 로그 보존 원칙을 재사용하고 결과 전달과 보존을 분리한다.
- 선행: F0 저장/evidence 경계, 기존 runner 결과. 소비자: F8, F9, F10.
- F7은 payload 요약/정제와 증거 참조를 소유한다. resource 보존 기한·공통 receipt schema는 메인 소유다.

### 시험·로그·완료

- `P3-F7-01` 정상: 작은 성공·실패 결과 요약과 원문 evidence 조회가 일치하고, success summary만으로 기준 pass가 되지 않는지 확인한다.
- `P3-F7-02` 안전 실패: 대용량 출력, 토큰/비밀 fixture, 저장/정제 오류를 주입; 비밀 미노출·오류 가시성·cursor 및 원본 보존 상태를 확인한다.
- `P3-F7-03` 형식 호환: text/json/binary 또는 capability 미지원 도구 결과를 구분하고 unsupported/unknown을 성공으로 변환하지 않는지 확인한다.
- 대조 이벤트 초안: `tool_result.received`, `tool_result.redacted`, `tool_result.persisted`, `tool_result.summarized`, `tool_result.detail_requested`.
- 추적값: run/Step/tool refs, receipt/evidence refs, payload byte count, line count, truncation/cursor, redaction outcome, exit/status, criteria verdict source.
- 실제 파일/resource hash·SQLite receipt·도구 exit code·전달 payload와 event를 대조한다. fixture로 실제 도구 통합 통과를 주장하지 않는다.
- 완료: 요약은 짧고 참조 가능하며, 기준 판정과 원 증거가 분리되고 민감값/로그 오류 처리가 검증된다.
- 인계: F8에 상태·receipt·추가 detail cursor, F9에 Step별 result/evidence ref를 전달한다.
- 자율 범위: 승인된 정제 규칙과 표시 순서. 보존/삭제 정책, 공개 결과형 변경은 메인에게 보고한다.
- 보고는 시험 상태·실행환경/도구·exit code·증거/resource 대조·미해결을 포함한다.

## F8 — Python 실행 제어·진행 알림

### 목적과 범위

- 목적: 명시된 조건에서 재사용/대기/실행/관찰을 제어하고 의미 있는 변화만 메인 AI에 알린다.
- 이유: 같은 poll마다 AI를 호출하면 비용이 늘고, 시간 경과만으로 완료·취소·잠금 회수를 추정하면 상태가 손상된다.
- 추가: 결정적 Python control loop, Queue/runner 관찰, 제한된 재시도 판정, 60초 내 표시·변화 기반 알림.
- 수정: 기존 run/lock/polling이 F5 문맥, F6 reuse, F7 result receipt를 사용한다.
- 삭제: 맹목적 retry, poll마다 LLM 호출, 60초 고정 간격 AI 기동, 응답 없음으로 종료/점유 해제 처리.
- Goal: 재개 가능한 관찰과 정확한 상태 표시, 필요한 변화/판단만 AI에 전달한다.
- Non-goal: 직접 모델 API, 원격 coding host, 제품이 제공하지 않는 callback·wake 기능을 가장하는 것.

### 입출력·처리 제약

- 입력: F5 context ref/version/scope, F6 lookup/claim, run intent·Queue state, native/CLI handle/capability, F7 receipt/detail ref, retry policy·deadline·request/event refs.
- 출력: 실제 확인된 state transition, run/attempt/handle refs, next observation hint, 사용자/메인 알림 원인, retained locks, review-required 결과.
- Python은 매 poll에서 저장 상태와 runner status를 읽고 변화를 비교한다. 변화·오류·판단 요청·완료 시점에만 메인 AI를 깨운다.
- native/CLI 경로는 로컬 worker에서만 실행한다. Claude/Codex CLI 또는 확인된 local native worker를 사용하며 직접 제공자 API 호출 금지, Host coding 금지.
- 제품의 자동 UI 표시/refresh capability를 실측한다. 60초 내 표시를 지원하면 그 화면에 상태를 갱신한다.
- 화면 표시를 지원하지 않으면 조회/로컬 로그에 관찰 결과를 남기고, 명시적 query에서 최신 상태를 반환한다.
- 앱/세션이 닫힌 상태의 자동 wake를 보장하지 않는다. 닫힌 세션은 다음 재개 시 조회한다.
- retry는 오류 종류·명시 횟수 제한·이전 실행 종료/미확인 상태를 확인하고 허용될 때만 수행한다. blind retry 금지.
- 시간 경과, poll timeout, 알림 전달 실패는 run 완료·취소 확인·scope lock 해제 근거가 아니다.
- 저장/응답 순서는 공통 계약의 상태 journal→외부 호출→handle 기록→결과 저장→통지를 따른다.
- 이유: 상태/횟수/타이머는 결정적 Python으로 처리하고 AI는 의미 변화 판단을 맡아 불필요한 호출을 줄인다.

### 경계·시험·완료

- 선행: F5, F6, F7와 공통 run/lock/runner 계약. 소비자: F9, F10.
- F8은 poll/backoff·허용 retry·알림 trigger orchestration을 소유한다. run state/transition, lock, 실제 runner 호출은 각각 공통 core/adapter 소유다.
- `P3-F8-01` 정상: 진행 중·완료 runner 관찰을 제어 시계로 실행; 60초 이내 지원 화면 갱신 또는 unsupported query 경로, 상태 변화만 AI 알림인지 확인한다.
- `P3-F8-02` 안전 실패: runner 미응답/시작 결과 불명/통지 실패/cancel 요청을 주입; blind retry·종료 오판·lock 조기 해제가 없음을 확인한다.
- `P3-F8-03` 형식 호환: UI refresh·callback·poll capability 조합 및 local Codex/Claude CLI 경로 지원 여부를 실제 점검; 미지원은 unknown/unsupported/blocked로 남긴다.
- fixture 통과는 실제 native/CLI 통합이 아니다. 실제 설치·모델·권한 경로를 별도 증거로 기록한다.
- 대조 이벤트 초안: `control.observation_started`, `control.state_observed`, `control.progress_published`, `control.ai_notified`, `control.retry_decided`, `control.reconcile_required`.
- 추적값: task/run/attempt/Step refs, runner/handle refs, prior/current observed state, poll time, capability ref, retry count/reason, notice reason, scope lock owner, receipt ref.
- DB run/attempt·Queue/lock·프로세스/runner status·표시 시각·AI 호출 기록을 이벤트와 대조한다. poll당 AI 호출이 없음을 호출 계측으로 확인한다.
- 완료: stale/unconfirmed 실행 보호, 허용 retry만 수행, 알림 조건·60초 관찰 경로·닫힌 세션 한계가 실제 상태와 일치한다.
- 인계: F9에 실제 run/attempt·scope lock·Step별 terminal/review state 및 알림/재조정 상태를 준다.
- 자율 범위: 계약 내 poll interval/backoff 및 UI에 맞는 표시 주기. retry 최대치·wake 보장·실행기 경계 변경은 메인 결정이다.
- 보고는 시험 ID별 실제/fixture 계층, 명령/도구·exit code, 제품 capability, DB·프로세스·호출 계측 대조, 증거 ref를 포함한다.

## F9 — 관련 소작업 batch 배정

### 목적과 범위

- 목적: 문맥·계약을 공유하고 실행 간섭이 적은 Step을 묶어 배정하되 각 작업의 소유권·증거·독립 완료를 유지한다.
- 이유: 공통 문맥의 반복 전달을 줄일 수 있지만 큰 batch는 충돌·오류 전파·완료 오판을 만들 수 있다.
- 추가: batch 후보/적합성 판정, parent-child run mapping, Step별 상태·결과 결합, 부분 실패 보고. PMT가 공통 문맥과 여러 논리 Step 지시를 한 메시지에 구성하고 각 Step의 구조화 결과를 검증하는 방식도 batch다. 수정: 기존 native/CLI 실행에서 일괄 지시 수용, Step별 결과 분리, 취소 범위, handle binding capability를 실제 시험하며 별도 vendor batch API는 요구하지 않는다.
- 삭제: Step 상세를 뭉개 단일 완료로 치환, 확인되지 않은 capability를 가정, 자동 중복 tool call.
- Goal: feasible한 경우에만 공유 문맥을 이용해 실행 비용을 줄이고 Step별 독립 검토를 가능하게 한다.
- Non-goal: 모든 작은 작업의 batch 강제, 공유 scope lock 소유권 추정, 서버별 bulk API나 신규 외부 실행기 추가, 불가능한 모델 capability 생성.

### 입출력·결과

- 입력: Step refs·개별 지시 version/hash refs, F5 context version/scope, F8 run/runner capability, 의존·충돌 관계, 완료 기준·예상 공유 문맥.
- 입력 제약: 원 지시문은 승인된 runner에만 전달한다. 일반 관리·로그에는 Step ID/ref만 둔다.
- 출력: batch decision(eligible/split/blocked), 이유·공유 context ref, batch/run refs, Step별 child mapping, 실제 handle 및 개별 receipt/result/status.
- 각 Step은 자신의 논리 job/run과 directive/result refs를 가진다. batch parent는 correlation/공유문맥 식별자일 뿐 Step ID를 대체하지 않는다.
- 한 실제 물리 handle은 runner 실행 slot 하나로 센다. 여러 논리 child run과의 parent-child binding을 명시 저장해 별도 run으로 동시 실행 한도·점유를 우회하지 않는다. 실행 전 owner group과 관련 scope union을 원자 확보하며 일부만 확보되면 실행하지 않고, 소유권은 실제 handle 종료·결과 확인 규칙을 따른다.
- batch는 vendor-native 다중 작업 기능을 전제하지 않는다. 기존 native/CLI에 여러 Step 지시와 공통 context를 한 메시지로 전달하고 Step별 구조화 결과를 회수할 수 있으면 사용할 수 있다.
- 결과 분리·취소 범위·handle binding을 adapter별 실제 시험으로 확인한다. capability가 없거나 불명확하면 구조화 결과를 추측하지 않고 개별 Step 실행으로 분리한다.
- 개별 결과가 없는 단일 opaque response, 누락 결과, 부분 저장, unknown 결과는 해당 Step별 미확정으로 보존하고 복구 전까지 scope/결과 보호를 유지한다. 하나의 Step 실패/검토대기는 다른 Step의 확인된 결과를 보존하되 parent 전체 완료로 승격하지 않는다.
- 같은 원인/요청의 transport 재전송만 idempotency 처리한다. 추가 tool call은 명시된 다른 작업으로 자동 복제하지 않는다.

### 방법·소유 관계

- 먼저 F8의 single-Step 실행·관찰·복구를 완성하고, 그 뒤 F9가 group 실행으로 확장한다. 순환 의존을 만들지 않는다: F8은 F5/F6/F7에만 의존하며, F9는 F5/F8에 의존한다.
- Python은 의존·충돌·공유 context·실행기 capability를 평가해 함께 실행 가능할 때만 batch를 만든다. 애매하면 개별 실행으로 분리한다.
- 공유 문맥 ref는 F5 버전·scope에 고정한다. Step-specific 권한/지시/기준을 배치 편의로 생략하지 않는다.
- 실행 결과는 F7 receipt 경계를 통해 Step별로 저장하고 F8 상태 조정에 연결한다.
- 이유: 결정적 적합성 판정과 native/CLI 실제 capability 확인으로 문맥 절감과 추적성을 함께 보장한다.
- 선행: F5 context, F8 single-Step 실행 제어, 2단계 Step/runner/lock 계약. F9 group 확장은 F8 단일 경로 완료 뒤 순차 진행한다. 소비자: F10 통합·효율 판정.
- F9는 batch grouping 및 mapping 관계를 소유한다. 실제 실행/단계 완료·scope lock 최종 판정은 기존 공통 runner/메인 소유다.

### 시험·로그·완료

- `P3-F9-01` 정상: 공유 module/contract context와 비충돌 Step 묶음을 PMT가 한 메시지로 전달하는 기존 native/CLI 방식으로 시험한다. Step별 구조화 결과·handle binding·실제 취소 범위를 확인한다. 별도 제품 batch API는 요구하지 않는다.
- `P3-F9-02` 안전 실패: dependency cycle·scope union 경쟁·부분/누락 결과·opaque response·취소 경쟁을 주입; 원자 점유, child별 unknown 보존·복구 보호, 단일 handle=slot 1, whole batch Done 금지를 확인한다.
- `P3-F9-03` 형식 호환: 기존 runner의 일괄 지시 수용·개별 결과 분리·취소 범위·handle binding capability를 실제 snapshot/시험에 대조한다. 구조화 회수가 불가하거나 불명확하면 unsupported/unknown으로 기록하고 개별 실행을 기본 선택한다.
- 동일 suite에서 개별 실행과 batch 실행의 입력 기준·완료 기준·결과/증거를 비교한다. 근거 없는 절감·가짜 pass는 금지한다.
- 대조 이벤트 초안: `batch.evaluated`, `batch.created`, `batch.step_bound`, `batch.split`, `batch.step_result_recorded`, `batch.review_pending`.
- 추적값: batch/parent/child run refs, Step/directive/context refs, actual native/CLI handle ref, capability, owner/scope lock refs, per-Step result/evidence/status, split reason.
- 실제 실행 handle·child mapping·directive hash·원자 확보된 owner group/scope union·각 Step receipt/criteria 결과·취소 영향 범위와 이벤트를 대조한다.
- 완료: 가능한 묶음/불가능한 분리, native 지원 경계, 각 Step 독립 결과·부분 실패·scope 소유가 실제 상태로 검증된다.
- 인계: F10에 batch decision·비용·품질·Step별 receipt 및 전체 미해결/blocked를 전달한다.
- 자율 범위: 승인된 관계/기준 내 batch 크기·공유 context 선정. 의존 의미 변경, whole Done 승격, 새 multiStep capability 선언은 메인 결정이다.
- 보고는 ID별 상태·fixture/실제 계층·모델/실행기·exit code·handle/DB/Git 대조·증거 ref·미해결을 포함한다.

## 공통 완료와 메인 인계

- 각 항목은 목적·범위, 입력·출력, 선행·소비자, 안전 실패, 호환 형식, 로그 대조와 수용 시험을 모두 충족해야 한다.
- 테스트 안 한 기능은 미검증, 지원 확인 전은 unknown, 권한/설치 차단은 blocked다. 코드 존재·AI 자가보고만으로 pass하지 않는다.
- 실제 기준 commit/dirty, 제품·모델·실행기 capability, 정의 버전, 명령/도구 호출 ref·종료 코드, 증거 ref, 상태를 기록한다.
- F5→F8→F9의 전달은 context/version/scope/run/receipt 참조로 잇고, F6 reuse claim과 F7 증거도 해당 Step 및 사용자 event와 연결한다.
- 각 작업자는 실제 변경 범위·계약 제안·시험 증거·미해결/다음 조치·dirty/commit 상태를 메인에 보고한다. 이 문서 작성은 구현이나 계약 확정이 아니다.
- 메인이 공통 계약·상태·event/schema를 승인하고 기능 명세 전체를 통합한 뒤에만 구현을 배정한다.
