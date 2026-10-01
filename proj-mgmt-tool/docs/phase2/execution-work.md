# 2단계 실행 상세 작업

이 문서는 2단계 공통 요구를 Q4~Q6 실행 작업으로 나눈 계획이다.
모델 routing, Queue, lock, runner는 아직 구현 대상이며 완료 기능이 아니다.
실제 입출력은 공통 계약에서 정하며 아래 예시는 의미와 제약만 정의한다.
공통 상태·명령·스키마·오류·로그의 구체 계약 소유자는 메인이다.

## Q4 — 모델 설정·능력 확인·절약형 routing

### 목적과 범위

- 목적: 역할에 필요한 능력과 허용 경로를 만족하는 실제 후보를 고르고 이유를 남긴다.
- 추가: 선택형 초기 설정, 역할별 선호, 작업별 사용자 지정, capability 확인, 후보·선정 이력.
- 수정: `auto` 경로 선택은 같은 코딩 에이전트의 native subagent 지원 확인을 첫 기준으로 삼는다.
- 삭제: 확인되지 않은 모델·가격·지원 기능을 전제로 둔 후보와 선택 결과를 제거한다.
- Goal: 설정을 하지 않아도 안전한 기본 선택이 가능하고, 실제 선택을 재현할 수 있다.
- Non-goal: 전체 제공자 카탈로그 수집, 확인되지 않은 가격 비교, 모델 품질의 자동 단정.

### 입력·출력

- 입력은 역할, 필요한 능력, 사용자 선호/경로 지정, 연결 상태, 권한, 지원 정보의 출처·시각이다.
- 설정값은 선택 사항이며 비밀값은 입력 요약·로그·DB 일반 필드에 복사하지 않는다.
- 능력은 제품 세션의 실제 호출 가능 여부와 대상 모델·도구·권한의 조합으로 판정한다.
- 비용/사용량은 제공자 공식 자료 또는 계정 응답과 기준 시각이 있을 때만 비교한다.
- 출력은 후보 적합/부적합 사유, 선택된 역할·모델·runner 경로, 설정 버전과 이유다.
- 비용을 알 수 없으면 `unknown` 의미로 남기고 능력·선호로 선택한다.
- 요청 모델과 실제 모델이 다르면 둘을 각각 기록하며, 묵시적으로 이름을 바꾸지 않는다.

### 기술·의존·경계

- Python 표준 라이브러리의 명시적 capability 값과 SQLite 설정/선택 이력을 우선한다.
- 이유: 현재 본체와 호환되고 별도 서비스·ORM·제공자 SDK를 필수화하지 않는다.
- 제품별 capability adapter가 공식 제품 기능 또는 실제 세션 결과를 정규 값으로 변환한다.
- 인증정보는 OS/제품이 제공하는 안전한 인증 경계를 참조하고, PMT가 토큰을 출력하지 않는다.
- 선행: 메인의 capability·설정·오류·선택 결과 공통 계약 확정.
- 연관: Q6 runner가 실제 호출 경로를 수행하고, Q5가 대기·재시도를 관리한다.
- 내재 지원이 확인되지 않으면 지원으로 추정하지 않는다.
- native 실행 슬롯이 차면 같은 에이전트의 native 대기열에서 기다린다.
- 시작 여부가 불명확하거나 이미 시작한 요청은 CLI/SDK/API로 대체하지 않는다.
- 명시 경로는 사용자 선택을 우선하되 허용되지 않거나 인증이 없으면 `blocked`로 남긴다.

### 동작·시험·로그

- `P2-ROUTE-01`: 설정 없는 auto에서 실제 capability로 적격 후보만 고른다.
- `P2-ROUTE-02`: 사용자 모델/경로 지정은 기본값보다 우선한다.
- `P2-ROUTE-03`: 가격 미확인 후보에 비용 우위를 부여하지 않고 미상으로 남긴다.
- `P2-ROUTE-04`: 명시 subagent 미지원은 blocked이고, auto에서는 미시작이 확인된 경우 허용된 CLI/SDK/API 후보를 선택한다. 허용 외 호출은 없다.
- `P2-ROUTE-05`: 슬롯 부족은 대기하고 시작/시작불명 후 다른 경로를 열지 않는다.
- `P2-ROUTE-06`: 재시작 뒤 실제 모델·경로·선정 사유를 설정 버전으로 조회한다.
- `P2-ROUTE-07`: 여러 경로가 동시에 가능하면 auto는 native → CLI/SDK → 허용 API 순서다. 사용자 명시 경로와 native 슬롯 대기는 이 순서의 자동 fallback과 구별한다.
- 시험은 capability fixture와 설정 조합을 우선하며 실제 제품 시험 결과와 구분한다.
- 예정 시험은 실행 전까지 `not_run`; 제품 미설치·권한 부족은 `blocked`다.
- 로그 지점: 후보 수집, capability 판정, 후보 제외, 최종 경로 선정, 대기/차단.
- 이벤트 초안: `routing.candidates_checked`, `routing.selected`, `routing.blocked`.
- 필드: role, capability_ref, settings_revision, requested_route/model, actual_route/model,
  selection_reason_code, source_ref, observed_at, outcome, correlation_id.
- 전체 후보 응답·인증값·가격 원문·대화 본문은 로그에 싣지 않는다.

### 완료·인계·자율 경계

- 완료: 설정 없음/사용자 지정/미지원/가격 미상/슬롯 부족 사례가 판정되고 이력이 조회된다.
- 인계: Q5/Q6에 고정 routing 결과와 capability 근거 참조, 재선정 필요 조건을 전달한다.
- 자율: 공통 계약 안의 후보 정렬·검증 순서·명시된 대안 비교는 근거를 남겨 선택한다.
- 보고: 공통 필드·상태·스키마 변경, 새 인증 저장 정책, 허용 경로 확대는 메인에 제안한다.

## Q5 — Queue·scope lock·상태·중단 복구

### 목적과 범위

- 목적: 승인된 Step 실행을 순서화하고 겹치는 변경 범위를 원자적으로 보호한다.
- 추가: job/run/attempt 상태, 대기 사유, 다중 scope 점유, 취소 확인, 재조정 절차.
- 수정: SQLite의 짧은 트랜잭션으로 실행 의도·상태·점유·업무 이력을 함께 기록한다.
- 삭제: idle·시간 만료·사용자 Stop만으로 lock을 회수하는 동작을 금지한다.
- Goal: 동시 실행에도 중복 배정·부분 점유·불명확한 실행의 재호출을 막고 복구한다.
- Non-goal: SQLite가 제품 native 도구를 직접 호출하거나 장시간 트랜잭션으로 감싸는 것.

### 입력·출력

- 입력은 승인된 Step 참조, 의존성, scope 집합, 정책, routing 결과, 기대 revision이다.
- scope는 Step 소유 범위와 파일/저장소 등 공유 변경 대상의 불투명 참조로 표현한다.
- 경로·논리 자원은 정규화한 충돌 키를 사용하되 세부 키 문법은 메인이 확정한다.
- 다중 scope는 정렬된 순서로 모두 확보하거나 모두 취소한다.
- 조상/하위 또는 논리적으로 겹치는 scope는 충돌로 간주한다.
- 출력은 queue 위치·run/attempt ID·상태·점유 결과 또는 충돌/대기 이유다.
- 작업 본문은 lock 성공 전 읽거나 쓰지 않으며, 성공 뒤 기준 commit·문서·지시 버전을 고정한다.

### 상태·기술·복구

- 표준 진행 상태는 `queued → starting → running → review_pending → succeeded`다.
- 종료/시작 여부 불명 시 `reconciling`; 취소 흐름은 `cancel_requested → canceled` 확인을 요구한다.
- 실행 결과 실패·차단은 `failed` 또는 `blocked`이며 Step의 업무 완료와 분리한다.
- 동시성 기본 상한은 `min(3, 실제 실행기 한도)`다.
- 자동 일시 실패 재시도는 최대 2회이며, 미시작 또는 종전 실행 종료 확인 뒤에만 허용한다.
- 외부 실행 호출 전 `starting`과 attempt ID를 짧은 DB transaction으로 저장한다.
- DB transaction 밖에서 제품/native/CLI/SDK/API를 호출하고 반환 handle을 별도 transaction으로 저장한다.
- 시작 직후 종료되면 재호출 대신 `reconciling`에서 원 실행기 handle/state를 조회한다.
- 결과는 callback 또는 메인 통지보다 먼저 영속 저장한다.
- 취소 요청 수신은 종료 증거가 아니다. 확인 전에는 scope lock을 해제하지 않는다.
- 결과 종료는 `review_pending`; 메인이 결과·증거를 판정한 후에만 완료 및 필요한 해제를 확정한다.
- 프로세스형 실행의 취소는 소유 프로세스/자식 종료 범위를 확인하고 결과·종료 코드를 수집한다. 임의 외부 프로세스 종료나 PID만으로 원 실행 동일성을 단정하지 않는다.
- 기술은 Python 표준 라이브러리와 SQLite 짧은 트랜잭션을 쓴다.
- 이유: 기존 로컬 저장 구조의 멱등성·revision·이력 원칙을 재사용하고 브로커를 강제하지 않는다.
- 선행: Q4 routing 결과 계약, 메인의 상태 전이·scope·이벤트·스키마 계약.
- 연관: Q6가 실제 실행·상태 조회·취소를 제공하고 결과 receipt를 반환한다.

### 동작·시험·로그

- `P2-LOCK-01`: 독립 프로세스가 같은 scope를 요청하면 한쪽만 확보하고 나머지는 대기한다.
- `P2-LOCK-02`: 겹치는 조상/하위 scope 충돌 및 독립 scope 병렬 실행을 확인한다.
- `P2-LOCK-03`: 다중 scope 일부 실패 시 획득분을 전부 되돌리고 교착이 없어야 한다.
- `P2-LOCK-04`: lock 성공 전에 작업 입력을 읽거나 변경하지 않는 경계를 확인한다.
- `P2-LOCK-05`: 실행 중 범위 확장은 새 영역 접근 전에 전부 원자 확보한다. 실패 시 새 영역 미접근·부분 신규 점유 없음·기존 소유권 유지 또는 종료 확인 후 안전 재준비를 확인한다. 기존 점유를 보유한 채 충돌 범위를 기다려 교착시키지 않는다.
- `P2-RUN-01`: queued/starting/running/review_pending/succeeded 상태와 허용 전이를 검증한다.
- `P2-RUN-02`: 시작 전 장애·handle 저장 전 장애·결과 저장 후 통지 실패를 재현한다.
- `P2-RUN-03`: 중복 callback/result는 한 실행 완료와 한 업무 이벤트로 수렴한다.
- `P2-RUN-04`: 재시도 0~2회, 세 번째 재시도 거부 및 종전 실행 미확인 차단을 확인한다.
- `P2-RUN-05`: handle 복구/종료 확인 전 대체 경로 호출이 없음을 확인한다.
- `P2-CANCEL-01`: 실행 전·중·종료 후 취소 요청의 구분과 종료 확인을 검증한다.
- `P2-CANCEL-02`: runner 미응답 또는 메인 종료 중 lock 유지, 재개 세션 결과 조회를 검증한다.
- 통합 검증은 격리된 실제 SQLite와 독립 프로세스를 사용한다.
- 로그 지점: Queue 등록/승격, 다중 점유 시작/결과, 상태 전이, 재시도, cancel, reconcile.
- 이벤트 초안: `execution.queued`, `scope_lock.acquired`, `scope_lock.conflict`,
  `execution.transitioned`, `execution.reconciling`, `execution.cancel_confirmed`.
- 필드: job_id, run_id, attempt, step_ref, scope_ref_set, transition_from/to,
  owner_ref, wait_reason, native_handle_ref, retryable, transaction_outcome, evidence_ref.
- 지시 본문·전체 scope 경로·토큰·환경변수 전체는 기록하지 않는다.

### 완료·인계·자율 경계

- 완료: 충돌·병렬·다중 원자 점유, 장애·중복 결과·취소·재조정 시험 증거가 확보된다.
- 인계: Q6에 승인 run ID·scope 소유권·routing 결과·시도 제한·취소 계약을 전달한다.
- 자율: 계약 내 backoff·공정 Queue 순서·충돌 설명 방식은 관찰 가능한 규칙으로 정한다.
- 보고: 새 상태/전이, DB 이관, 다중 저장소 원자성 보장 범위는 메인에 올린다.

## Q6 — 제품 runner·skill·CLI/API·메인 결과 전달

### 목적과 범위

- 목적: execution이 생성한 run intent를 제품의 실제 실행 수단에 연결하고 증거 receipt를 회수한다.
- 추가: 제품별 runner/skill 연결, CLI·SDK 또는 허용 API adapter, polling/callback 수신.
- 수정: 출력 포맷은 공통 결과로 정규화하되 제품 capability·인증·취소 한계를 명시한다.
- 삭제: SQLite가 native 도구를 호출한다고 가장하거나 미지원 기능을 성공으로 변환하는 동작.
- Goal: 메인 세션이 실행을 조정하고 실제 도구 결과를 저장·재조회해 검토할 수 있다.
- Non-goal: PMT 자체 native subagent 엔진, 무승인 API 접근, 제품 독립 원격 실행 서버.

### 실행 경계와 입력·출력

- SQLite는 실행 요청·상태·scope·receipt의 영속 기준이며 native 제품 도구 호출자는 아니다.
- 메인 세션이 실제 제품 도구로 하위 작업을 호출하고 부모/자식 실행 식별자를 연결한다.
- runner는 `start/status/result/cancel` 의미를 제품 기능이 실제 제공하는 범위에서 구현한다.
- 입력 intent는 job/run/Step/parent run ID, 역할·경로, 지시 참조/version/hash,
  요구·계획·skill 버전, workspace·허용 범위·권한, 완료 기준·시간 제한을 가리킨다.
- 지시 원문은 내부 리소스에서 필요한 범위로 읽으며 일반 DB·관리 UI·로그에 복제하지 않는다.
- 출력 receipt는 실행 handle 참조, 실제 경로/모델, 결과 참조, 기준별 상태,
  실행 시험·종료 코드·환경/대상 지문·증거·미해결 항목을 포함한다.
- callback을 지원하면 먼저 저장 후 통지하고, 지원하지 않거나 유실되면 polling으로 회수한다.
- polling 응답은 동일 run/receipt key로 멱등 처리하고 이미 저장한 결과를 덮어쓰지 않는다.
- 외부 CLI/SDK/API는 연결·인증·허용 정책을 먼저 확인한다.
- 없는 인증·허용은 `blocked`; fixture는 계약 시험에만 쓰고 실제 제품 실행으로 표시하지 않는다.
- 사용자 명시 경로와 모델을 우선하며 불가한 경로를 자동 우회하지 않는다.

### 기술·의존 관계와 native 지원 확인

- 제품 adapter는 기존 Python/JSON 본체 계약에 맞추고 선택 SDK는 해당 adapter 의존성으로 격리한다.
- 이유: 제품 출력·호출 차이를 국소화하면서 운영 런타임 의존성 증가를 막는다.
- skill은 배정·필수 문맥 참조·반환 형식 안내를 맡고 공통 상태 판정은 PMT 서비스에 둔다.
- CLI/SDK/API는 제품 실행 경계를 구현하되 DB나 업무 규칙을 복제하지 않는다.
- 선행: 메인의 공통 runner/result/error 계약, Q4 선택 capability, Q5 run intent와 lock 확보.
- 연관: 결과 저장 성공 후 메인 검토·Step/Item 판정 및 통합 증거에 연결된다.
- Codex/Claude/OpenCode의 native subagent 가능 여부는 설치·현재 세션·대상 모델·권한을 실제 확인한다.
- 같은 에이전트 native 경로가 확인되면 우선 사용하며 슬롯이 부족하면 그 경로에서 대기한다.
- 시작 여부 불명 또는 이미 시작한 native 작업은 외부 경로로 대체하지 않는다.
- native 실행의 지원 미확인은 unknown, 확인된 미지원은 unsupported로 구분한다. 승인된 사용 가능 경로가 없으면 blocked다.
- 실제 제품별 시험은 제품 호출 경계·취소·재개를 확인한다. fixture 통과와 분리 보고한다.

### 동작·시험·로그

- `P2-NATIVE-01`: Codex 현재 세션의 대상 모델/도구/권한별 native 지원을 확인한다.
- `P2-NATIVE-02`: Claude Code 지원 설치에서 실제 호출·부모/자식 ID·결과 회수를 확인한다.
- `P2-NATIVE-03`: OpenCode 지원 설치에서 실제 호출·결과 회수·취소/재개를 확인한다.
- `P2-NATIVE-04`: 미설치·미지원·권한 부족은 blocked/not_run으로 남기고 성공 처리하지 않는다.
- `P2-NATIVE-05`: native 슬롯 대기와 시작 상태 불명의 무대체 규칙을 확인한다.
- `P2-API-01`: 직접 API 비허용/미인증은 호출 없이 blocked이며 에이전트 자체 모델 통신 설정과 혼동하지 않는다.
- `P2-API-02`: 허용된 실제 제공자에서 조사·선택지·근거 형식 결과를 수신한다. 제공자별 호환 규약·버전·모델·인증을 기록한다.
- `P2-API-03`: 직접 API 코딩은 도구 실행 경계·권한·workspace 격리 검증 전 활성화되지 않는다.
- `P2-API-04`: HTTP 실패·rate limit·형식 불일치·응답 유실은 원 호출 종료 여부와 재시도 정책에 따라 처리된다.
- `P2-RUN-06`: 외부 CLI/SDK/API 인증·허용 분기 및 fixture/실물 결과 분리를 확인한다.
- `P2-RUN-07`: callback 중복·유실과 polling 복구, 결과 우선 저장, 다음 세션 재조회.
- `P2-RUN-08`: 결과가 시작/상태 관찰보다 먼저 도착하거나 취소와 경쟁해도 사실을 보존하고 종료 상태를 늦은 관찰로 되돌리지 않는다.
- `P2-CANCEL-03`: 제품이 취소를 미지원/확인 불가할 때 lock 유지와 조정 상태를 확인한다.
- 로그 지점: intent 전달 직전/응답 직후, handle 저장, polling/callback, 결과 저장/메인 통지.
- 이벤트 초안: `runner.intent_dispatched`, `runner.handle_recorded`,
  `runner.poll_observed`, `runner.callback_received`, `runner.receipt_persisted`,
  `runner.main_notified`, `runner.cancel_observed`.
- 필드: run_id, parent_run_id, runner_kind, requested/actual route/model,
  product_capability_ref, auth_state, permission_state, handle_ref, receipt_ref,
  delivery_method, dedupe_key, outcome, error_code, evidence_ref.
- command line 인수·인증정보·대화/지시 본문·비밀 응답은 로그에서 정제한다.

### 완료·인계·자율 경계

- 완료: 지원 선언한 각 제품 경로에서 실제 실행 receipt·결과 조회·취소 또는 제한 증거가 있다.
- 지원 불가 제품은 사유·환경·검증 자료와 함께 blocked이며 단계 전체 성공으로 숨기지 않는다.
- 인계: 메인에 실제 경로/모델, handle/receipt 참조, 기준별 시험·증거와 미해결 사안을 반환한다.
- 자율: 기존 계약 내 polling 간격·adapter 내부 변환·정제 규칙은 실제 제품 근거로 정한다.
- 보고: 새 제품 지원 선언, 새 외부 인증/권한 요구, 공통 receipt 필드나 공개 CLI 변경은 메인 소유다.
- 상위 완료 판정은 Step 증거 검토, Item 요구 충족, Work 통합을 각각 따로 확인한다.

## 공통 인계와 판정

- 모든 시험 ID는 예정 수용 기준이다. 실행 전 성공 표시를 하지 않는다.
- 각 결과에는 실행 여부, 실제 제품/환경, 명령·종료 코드, commit/dirty 지문, 증거 참조를 기록한다.
- `pass`, `fail`, `blocked`, `not_run`을 구분하며 모델의 성공 서술만으로 통과시키지 않는다.
- 외부 경로에서 callback을 쓰든 polling을 쓰든 결과 영속화가 통지보다 앞선다.
- 자율 구현은 승인 계약 내부에 한정하며 목표·권한·공통 계약을 바꾸지 않는다.
- 공통 상태·명령·필드·스키마·오류·event 명 최종 확정은 메인이 한다.
- 묶음별 작업자는 변경 범위 제안, 사용한 근거, 증거, 미해결/연관 영향을 메인에게 인계한다.
