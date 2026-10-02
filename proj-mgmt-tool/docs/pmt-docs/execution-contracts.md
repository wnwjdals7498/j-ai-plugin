# 모델·실행·통신 계약

Queue·runner·Step·범위 lock의 실제 상태는 [2단계 기록](../phase2/implementation-status.md), Host·제어·묶음의 실제 상태는 [3단계 기록](../phase3/implementation-status.md)을 따른다. 기본 CLI·claim의 의미는 [1단계 계약](../phase1/contracts.md)을 유지한다.

## 모델과 실행 경로

- 최상위: 불완전한 요구의 큰 기준, 구조·기술·공유 계약 판단. 상위: 기본 메인, 두 트리·Step 지시·배정·통합·검증·문서 반영. 하위: 확정 지시 실행과 실측 결과 반환.
- 초기 모델 설정은 선택 사항이다. 기본 절약형은 실제 가용 모델·능력·권한에서 AI가 선정한다. 현재 작업의 사용자 지정 모델은 이 기본값보다 우선한다.
- `auto`: 같은 코딩 에이전트의 대상 모델·도구·권한이 지원되면 **native subagent 우선**, 다음 CLI/SDK, 허용·연결된 경우 직접 API. 사용자가 지정한 경로를 우선한다.
- 에이전트, provider, model, 인증, 역할을 각각 기록한다. 존재하지 않는 모델·가격·지원 기능을 가정하지 않는다.
- 비용·사용량은 공식 제공자 자료/계정 응답과 확인 시점을 기록한다. 비용을 확인할 수 없으면 미상으로 표시하고 능력·명시된 선호로 선택하며, 절약형이라는 이유로 최저가를 확인했다고 주장하지 않는다.
- native 실행 슬롯이 부족하면 대기한다. 이미 시작했거나 시작 여부가 불명확한 작업을 다른 경로로 중복 실행하지 않는다.

## 경계별 통신

| 경계 | 방식 | 규칙 |
|---|---|---|
| 제품 훅/스킬 → PMT | 현재 CLI의 UTF-8 JSON 요청/응답 | stdout 응답 한 줄, 진단 stderr; native 출력은 adapter가 변환 |
| 본체 모듈 간 | Python 함수와 명시적 값 객체 | 순환 의존·제품별 상태 판정 복제 금지 |
| execution → runner | `start / status / result / cancel` | 실행 handle 보존; 공통 결과로 정규화 |
| runner → 제품/모델 | 지원된 subagent·CLI/SDK·API | 제품별 callback 지원을 공통 보장으로 가정하지 않음 |
| 결과 → 메인 | 먼저 영속 저장, 이후 callback 또는 조회 | 메인이 닫혀 있어도 다음 세션에 결과 재조회 가능 |
| 로컬 → Host | 3단계에서 정의할 저장 API | 2단계에 HTTP 서버·원격 실행 Queue를 필수로 추가하지 않음 |

현재 CLI 요청은 `protocol_version/operation/request_id/actor/session_id`와 동작별 대상·revision·payload·context_refs를 사용한다. 응답은 `ok/result/error/warnings`를 포함한다. 기존 strict 필드 검사를 우회해 2단계 필드를 임의 주입하지 말고 동작별 payload 또는 명시적인 프로토콜 확장으로 정의한다.

runner 요청에는 job/run/Step·parent run ID, 목표 역할·실제 경로/모델, 지시 참조·버전/hash, 요구/계획 버전, skill 버전, workspace·허용 범위·권한, 완료 기준·시간 제한을 넣는다. 결과는 변경/산출물 참조, 기준별 `pass/fail/blocked/not_run`, 실제 시험·종료 코드·환경/대상 지문·증거, 미해결 사항을 포함한다.

`request_id`는 같은 호출 재전송, `job_id`는 논리 실행 요청, `run_id`는 시도, native handle은 외부 실행 식별자다. 취소 요청 수신은 종료 확인과 다르다. runner가 미지원 동작을 성공으로 꾸미지 않는다.

## Queue·점유

```text
queued → starting → running → review_pending → succeeded
             └─ 시작/종료 불명확 → reconciling
실행 가능한 상태에서: blocked / failed / cancel_requested → canceled
```

상태 전이는 원인·증거·허용 전이 검사를 동반한다. `reconciling`은 외부 실행을 조회해 기존 handle을 복구하거나 종료 여부를 확인하는 상태다. 실행 상태와 Step 업무 상태는 별도로 관리한다.

- 기본 동시 실행은 `min(3, 실행기 한도)`. 일시 실패의 자동 재시도는 최대 2회이며, 미시작 또는 이전 실행 종료가 확인되어야 한다.
- Step와 공유 변경 범위의 lock을 **원자적으로 확보하고 성공 응답을 받은 후** 작업 내용을 읽고 쓴다. UI 상태 조회는 가능하나 변경 중인 내용을 새 실행 기준으로 삼지 않는다.
- 범위 여러 개는 정렬된 키와 전부 성공/전부 취소 방식으로 확보한다. 조상·하위 경로 또는 같은 논리 리소스의 겹침도 충돌로 판정한다. 일부만 점유한 채 대기하지 않는다.
- 점유 후 Git·문서·대상을 최신화하고 지시 버전을 고정한다. 부모 목표의 집계와 자식 Step의 작업 소유권을 구별해 독립 Step은 병렬로 실행한다.
- 점유는 필요한 결과 검증·통합까지 유지한다. 시간·idle·Stop만으로 해제하지 않는다. `.lock` 파일이 필요해도 표시/호환용이며 SQLite 점유가 권위 원본이다.
- 외부 실행을 DB 트랜잭션 안에서 기다리지 않는다. `starting`과 시도 ID를 먼저 저장하고 외부 호출 후 handle을 저장한다. 중간 장애 시 미확인 실행을 조정하며 무조건 재호출하지 않는다.
- 결과·callback은 중복 식별로 재처리한다. 실행 종료는 `review_pending`이며 상위의 결과 확인 후 성공을 확정한다.

## 동작 레퍼런스

| 상황 | 흐름 | 확인할 결과 |
|---|---|---|
| 새 프로젝트 | 요구 트리 → 구현 트리 → 문서/graph → Work/Item/Step → 배정 | 대전제와 구현 방법 추적 가능; 두 트리 종료 조건 충족 |
| 동일 에이전트의 두 독립 Step | 지시 확정 → Queue → 각 범위 점유 → Git 확인 → native subagents → 결과 저장 → 메인 검토/통합 | 실제 경로·지시 버전·증거 연결, 공유 파일 동시 변경 없음 |
| 같은 범위 요청 | 첫 Step 점유 → 두 번째 대기 → 첫 결과 검토/해제 → 두 번째 점유·최신화 | 대기 작업이 이전 기준으로 착수하지 않음 |
| 실행 직후 PMT 중단 | starting 기록 → 외부 시작 → handle 저장 전 중단 → reconciling | 외부 실행 확인 전 대체 실행 없음 |
| callback 재전송/메인 종료 | 결과 영속화 → 통지 실패/중복 → 다음 조회 | 결과 손실·중복 완료·중복 업무 이벤트 없음 |
| 테스트 실패 | 실측 결과 → 상위가 원인/변경 범위 판단 → 필요시 BUG/TEST 기록 → 수정 Step | 사용자 대전제 변경은 사용자, 구조 변경은 최상위 검토 |

Step 성공은 실행 기준 충족, Item 완료는 요구 충족, Work 완료는 통합 목표 충족이다. 하위 모델 성공 응답·프로세스 종료·prototype 완료를 전체 완료로 바꾸지 않는다.

## 현재 3단계 실행 경계

`LocalStore`/`HttpStore`는 operation 결과·소유자 제한 조회·호환 포트를 제공한다. Phase 3 operation registry는 Host API allowlist가 아니다. 저장 mode는 단일 profile로 선택하고 hosted 실패는 로컬 업무 DB로 전환하지 않는다. 모델·CLI·native·working tree는 클라이언트에서 동작한다. [Host 계약](../phase3/host-api-contract.md)의 현재 인증·source·run/lock 확인을 거쳐 로컬 mapping을 해석한다.

F8은 단일 run 또는 F9 parent를 제어한다. Phase 2 Queue·run revision·scope lock이 권위 원본이며 제어기는 현재 run·F5 문맥·F6 근거를 다시 검증한다. native 실행은 안정적인 nonce와 기대 revision을 가진 main-native-call action으로 요청한다. 실제 main 측 handle ACK 전에는 실행 중이라고 기록하지 않는다. 동일 nonce·동일 handle 재전송은 복구 가능하지만 다른 handle은 거부한다. 결과는 저장된 뒤 `review_pending`에서 검토한다. private native action 원문은 해당 원 요청의 응답 cache 하나에 보관하고 공유 control metadata에는 hash/ref/stub만 둔다.

로컬 CLI 경로는 기존 runners 경계와 실제 CLI handle을 사용한다. prompt에는 검증된 F5 bounded projection만 반영하며 hash를 기록한다. 호스트 경로·PID·spool을 원격 저장 계약으로 노출하지 않는다. 관찰은 같은 상태에서 업무 이력을 쌓지 않는 조회이며 다음 poll은 15–60초 범위다. 의미 있는 변경·오류·메인 조치 필요·완료에 한해 통지하고, 표시 UI를 쓸 수 없으면 조회 결과를 명시한다. 앱이 닫힌 상태에서도 깨어난다는 보장은 없다.

취소 요청이나 ACK는 실제 정지 확인이 아니다. 결과·정지 여부가 불명확하면 lock을 유지하고 기존 handle을 reconcile한다. 재시도는 허용된 일시 오류와 미시작/종료 확인을 모두 만족할 때 최대 두 번이며, 새 run은 현재 context/source/reuse 참조를 다시 받는다. 현재 CLI 결과에는 일시 오류 분류가 항상 포함되지 않으므로 그 경우 자동 재시도하지 않는다. 손실된 receipt나 모델의 성공 문구만으로 성공·완료를 기록하지 않는다.

F9는 queued Step별 고정 지시·context·criteria와 parent scope union을 검증하고 물리 실행 한 개를 child별 logical run에 명시적으로 연결한다. 누락 결과는 미확인으로 남기며 다른 child의 검토 전 parent union을 해제하지 않는다. batch 자체를 모든 Step 성공으로 판정하지 않는다.

F7 결과 receipt는 실제 리소스 hash·run·기준 참조에 묶인다. 기준별 pass에는 명시적 증거가 필요하다. Host 중단 중 이미 시작한 로컬 프로세스의 실제 종료 receipt는 기존 own spool에서만 수집할 수 있다. 신규 파일 접근·dispatch·claim을 허용하지 않는다. 이미 생성된 결과는 pending에 보관하고 재연결 시 같은 원 요청으로 조회·조정한다. 모델 token 사용량·비용·실제 모델 품질 효과와 fixture 실행 품질은 구분한다.
