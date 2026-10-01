# Q1 저장·이관 기반

## 목적·범위

- **목적:** 1단계의 Work/Item·점유·증거를 보존하면서 두 트리 참조·Step·Queue·범위 lock·실행 시도를 일관되게 관리한다. 이후 작업자가 같은 저장 계약을 소비하게 한다.
- **추가:** Step 관계·지시 참조/버전, 요구/계획 버전·Git 기준 참조, job/run·실행 intent/receipt·결과, 범위 점유, 운영 최근 관찰·파일/정리 journal의 저장 경계.
- **수정:** schema 2의 버전형 이관, 관계·소유권·완료 조회, 검증 대상 매핑·백업/복원, 기록 삭제 시 참조 처리. **삭제:** 중복 원본·무효 참조를 만드는 경로를 새 동작에서 차단; 기존 기록의 임의 삭제 없음.
- **goal:** 기존 데이터·ID·현재 claim·검증이 보존되고 새 실행의 경쟁/재전송/부분 실패가 저장 제약으로 검출된다.
- **non-goal:** LLM 계획 생성, 모델 선정·실제 호출, 제품 훅 형식, Git 변경 해석, 사용자 운영 데이터 파기 실행.

## input / output

| 방향 | 값의 의미·제약 |
|---|---|
| input 기존 저장 | schema 버전·기존 records/events/claims/artifacts/verifications와 ID/참조; 이관 전 backup manifest |
| input 업무 변경 | 대상·부모·기대 revision·상태/태그/우선순위/제품 범위·변경 이유; 종류와 프로젝트 관계 일치 |
| input 실행 저장 | 논리 job·시도 run·선행·불변 지시 참조·범위 키·소유자·변경 버전·정규 결과; 동일 run 결과는 덮어쓰기 금지 |
| input 운영 | 최근 관찰·journal 단계·정리 분류·참조 보호; 비밀/원문 중복 저장 금지 |
| output | 초기화/이관 결과, 공개 저장 함수 계약, 관계·고유 제약, 원자적 변경 결과/충돌, 재처리 결과, 진단/복원 비교 |

## 기술·관련 계약

Python `sqlite3`·현재 `Database.write/run_request`와 foreign key·짧은 쓰기 transaction·CAS·고유 제약을 재사용한다. 관계/상태/버전/점유는 제약·조회 가능한 필드이며 상세 구조는 검증된 JSON으로 둔다. [Q0 계약](contracts.md)이 선행이고 모든 묶음이 이 기반을 소비한다.

새 schema 번호와 물리 테이블은 Q1이 제안하고 메인이 확정한다. 신규/구버전 이관/재초기화/지원 밖 미래 버전을 구분한다. maintenance 중 신규 작업 시작을 차단하고, 외부 실행이 있으면 먼저 안전한 유지보수 조건을 확보한다. 이관 실패 시 기존 상태와 복구 가능한 백업을 유지한다.

기존 Work/Item의 활성 claim을 Step 생성만으로 해제·재귀 변경하지 않는다. 이전 단위의 공유 소유권을 존중하는 호환 규칙을 제공하고 전환은 명시 변경으로 기록한다. Step parent·동일 프로젝트, DAG 의존, 같은 Step의 활성 run, 범위 중복 판정 자료를 일관되게 조회할 수 있어야 한다. 실 scope 충돌 알고리즘은 Q5가 소유한다.

graph는 문서 ref/hash/version만 저장하고 지시 원문은 리소스 하나만 참조한다. 결과와 이벤트의 ID 참조는 보존 정책에도 무결성을 유지해야 한다. 과거 이력 정리로 재시작/중복 처리가 깨지지 않도록 unresolved run·현재 참조·필요 최소 중복 처리 식별자는 보호한다.

## 코드동작 Test 방식

| 시험 ID | 실행 방식 | 확인 결과 |
|---|---|---|
| P2-DATA-01 | 실제 schema 2 격리 DB에 Work/Item·현재 claim·검증·리소스 준비 후 이관·재이관 | ID/관계/점유/결과 보존, 두 번째 이관은 추가 변경 없음 |
| P2-DATA-02 | 이관 중 단계별 실패 주입·미래 schema 열기·maintenance 경합 | 부분 schema 성공 표시 없음, 기존 DB/백업 보존, 신규 실행 차단 |
| P2-DATA-03 | Step 부모·프로젝트/의존/버전 위반과 같은 run 중복 제출 | 불법 관계/전이 거부; 성공 재전송 1회, 다른 결과 충돌 |
| P2-DATA-04 | 독립 프로세스가 같은 기대 버전으로 변경·run 준비 | 한 변경만 유효, 이벤트·요청 결과까지 함께 commit/rollback |
| P2-DATA-05 | 새 데이터가 포함된 DB+resources 백업·빈 root 복원 | Step/job/run/지시 ref·claim·증거·hash·journal 보존 |

실제 SQLite와 파일을 사용하고 mock 저장소만으로 통과시키지 않는다. 의미가 바뀐 1단계 상태/claim/완료/검증/백업 시험을 관련 회귀로 함께 확인한다. 로그 필드의 실제 저장값과 DB 상태를 대조한다.

## 코드동작 로깅·완료·인계

이벤트: `schema_migration_started/completed/failed`, `state_write_committed/rejected`, `request_replayed`, `execution_record_conflict`. 필드: 계약/schema 버전, request/correlation ID, 대상/Step/job/run ID, 이전/새 revision·run 버전, transaction 결과·오류 단계·참조 개수. 점유 token·본문·개인 절대 경로 제외.

**완료:** 위 시험과 관련 회귀가 실측 통과하고 Q2~Q8에 실제 저장 호출·관계·필드·오류·transaction 사용 제약을 인계한다. 이관/복원의 안전 조건·보존 정책도 문서화한다. **자율 범위:** SQL 배치·인덱스·내부 함수 구성. **메인 요청:** 계약 의미·지원 버전·새 의존성·기존 점유/완료 의미를 바꾸는 제안. 스키마 변경은 소비자가 각각 구현하지 않는다.
