# 4단계 공통 데이터·저장·동작 계약

2026-10-06. **계획 계약**. 논리 이름은 설명용이며 신규 CLI operation·HTTP endpoint·DDL·schema version은 R0에서 확정한다. 현재 runtime은 Core 0.3.0/SQLite 4/graph 1이다.

## 1. 원본과 판단 수준

| 수준 | 의미 | 판정 규칙 |
|---|---|---|
| 사용자 기준 | 목표·대전제·허용/금지·사용자 선택·위임 | 사용자 결정 참조와 현재 문서/graph 버전을 연결 |
| 관찰 사실 | 실제 Git diff·현재 업무 상태·handle/receipt·리소스 hash | 수집 주체·시각·범위·버전·완전성을 기록 |
| 해석 제안 | 변화의 의도·요구 영향·다음 행동 추론 | 이유·사실 참조·unknown·판단한 모델/정책을 남김 |
| 반영된 결정 | 현재 계획/문서/graph에 적용한 선택 | 권한·자율 범위·CAS·반영 receipt 확인 |
| 검증된 결과 | 정의한 대상과 조건에서 실제 실행한 결과 | 정의/조건/전후 지문·exit·증거와 적용성을 확인 |

commit 메시지는 변경 의도의 후보 근거다. 코드·테스트·문서 diff와 사용자/결정 기록에 연결하기 전에는 대전제 변경 승인으로 처리하지 않는다. 파일이 존재하면 `구현 존재 관찰`, 기능이 시험을 통과하면 `확인한 구현`, 계획에만 있으면 `예정`으로 구별한다.

## 2. 공통 식별·범위

- 기존 canonical UUID, request/event ID, 업무 revision, 요구/계획/Step 지시 버전, SourcePin과 resource hash 의미를 유지한다.
- namespace·repository·project·branch/workspace·Work/Item/Step·run·actor/device/environment/session을 분리한다.
- 논리 workspace와 클라이언트 경로 mapping을 재사용한다. PC 경로 차이가 같은 repository/branch/resource의 충돌을 없애지 않는다.
- AI 역할은 문맥 투영·판단 책임이며 인증 권한이 아니다. 이전 session ID나 checkpoint를 아는 사실도 현재 권한을 부여하지 않는다.
- 새 관계 종류를 임의로 graph schema 1에 넣지 않는다. 기존 관계/참조로 표현 가능한지 검토하고 필요한 별도 연결 index·버전/이관은 메인이 확정한다.
- 삭제·이름 변경·모듈 이동 후에도 요구·작업·결정 ID는 유지한다. 폐기된 관계는 현재 사용에서 제외하고 이유·이전 버전 참조를 보존한다.

## 3. 공통 정보 객체

### 3.1 BasisVector — 재개 기준 벡터

현재 판단이 어떤 자료를 기준으로 하는지 나타낸다. 모든 자료가 하나의 원자 snapshot이라고 주장하지 않는다.

| 그룹 | 필요한 의미 |
|---|---|
| 선택 범위 | namespace/repository/project, ref·canonical workspace, 선택 Work/Item/Step |
| Git/원본 | 관찰한 HEAD, 분석 완료 기준, 반영 완료 기준, dirty 상태와 검토한 변경 지문; non-Git 여부·대상 지문 |
| 계약 | 요구/계획/지시 버전, 문서 구간 hash, graph schema/revision/hash, 관련 결정 refs |
| 업무 | 조회 snapshot 또는 관련 records/run/claim/pending revision 목록·hash·capture ref |
| 적용 조건 | environment/tool/dependency/config/verification definition 중 실제 선택한 지문과 unknown |
| 수집 | component별 capture 시각·주체·완전성, coherence 체크 결과·불일치 이유 |

Git HEAD를 확인했다고 `반영 완료 기준`을 올리지 않는다. Git SHA만 같아도 dirty·관련 문서·업무 revision·근거가 달라지면 동일 기준으로 처리하지 않는다. 환경 전체를 dump하거나 매번 전체 repository/SQLite 파일을 hash하지 않는다. 관련 범위와 실제 선택된 조건을 사용한다.

문맥 조합 전후 source·관련 업무 revision을 대조한다. 불일치면 제한적으로 재수집하거나 `incomplete/source_changed`로 반환한다. checkpoint·alias·제안을 새 기준에 그대로 재활성화하지 않는다.

### 3.2 Checkpoint — 확정한 작업 경계

- ID·schema/작성기 version·parent checkpoint·선택 범위·BasisVector.
- 방향/요구/계획/결정의 refs와 실제 완료/진행/미확인 작업 refs.
- run/claim/handle/결과/pending의 안전한 refs와 마지막 관찰 상태.
- 유효 근거·무효/unknown 근거 refs와 재검토 사유.
- 다음 행동 후보·선행 조건·대기 대상·사용자 확인 필요 이유.
- 발생 원 이벤트·요청 ID, 확정 경계와 작성 시각, 의미 metadata hash.

checkpoint는 실행 권한·완료 판정·점유 승계 증서가 아니다. immutable 기록과 최신 pointer를 분리하고 동일 event/request의 재처리는 같은 결과로 수렴한다. parent/current pointer revision은 CAS로 비교한다. 변경 없는 조회를 새 checkpoint로 만들지 않는다.

### 3.3 ImplementationFactIndex — 현재 구현 연결

- 안정적인 기능/모듈/인터페이스 ID와 요구·계약·작업·코드 영역 refs.
- 관찰한 구현·호환/제약·미완성·현재 확인 수준과 source 범위/hash.
- 실제 확인한 시험 정의·결과·증거·검증 조건 refs.
- 사람이 확인한 mapping, Python이 추출한 후보, AI가 검토한 해석을 구분.
- 동적 연결·미지원 언어/생성 코드·삭제 경로·coverage 누락은 unknown.

index는 재생성 가능한 파생물이다. 코드 본문이나 기능 명세의 독립 편집본을 만들지 않는다. 특정 언어 parser로 파악한 사실을 전체 기술 stack의 완전한 의미 분석으로 확대하지 않는다.

### 3.4 ObservedChange — 관찰된 변경 receipt

- origin: Git commit/working-tree diff/문서·결정 event/명시적 외부 입력.
- before/after basis refs, 추가·수정·삭제·이동 경로와 실제 내용 diff의 refs/hash.
- 관련 계약·모듈·노드·Work/Item/Step 후보, 근거 경로, mapped/unmapped 범위.
- 수집 identity·scope·시각·분석 규칙 version, 사용 가능한 이유/의도 refs와 unknown.
- 물리 변경과 의미 행동의 ID를 분리. 같은 bytes라는 이유로 독립 사용자 사건을 합치지 않음.

외부 도구가 전달한 `완료`, commit ID, 성능 수치, 변경 설명은 검증 전 후보다. 현재 접근 가능한 실제 repository/record/evidence와 대조한다. arbitrary 원격 주소 fetching, 임의 명령 실행, 다른 기기의 절대 경로 읽기 권한을 제공하지 않는다.

### 3.5 AlignmentAssessment / AlignmentReceipt — 방향·근거 정렬

평가에는 요구·구현·결정·시험의 영향 경로, 바뀐/불변/unknown 조건, 기존 원리·검증의 적용성, 자율 범위, 대안·권고·필요 확인을 넣는다. 결론마다 관찰/결정/증거 refs를 갖는다.

반영 receipt에는 원 assessment hash·basis, 승인한 선택/위임 근거, 변경한 문서/graph/계획/지시 refs와 before/after revision, 증거 적용성 갱신, 미해결 범위, 게시·DB outcome을 넣는다. 반영 전후 source가 달라지면 old 평가를 현재 반영으로 확정하지 않는다.

진행 실행의 지시/대전제를 바꿀 때는 영향 run의 중지 요청·실제 정지·미확정 상태를 기존 execution 규칙으로 처리한다. stale 결과는 보존하되 새 계획의 성공으로 사용하지 않는다. 무관한 가지·유효 근거는 유지한다.

### 3.6 EvidenceApplicability — 현재 근거 적용성

| 값 | 의미 |
|---|---|
| applicable | 실제 선택된 정의·대상·조건과 현재가 일치하며 증거 접근/hash·후속 실패 조건을 통과 |
| not_applicable | 관련 조건 변화·정의 변경·후속 실패·증거 손상/권한 변화로 현재 사용할 수 없음 |
| unknown | 필수 지문·관계·실제 결과·접근 여부를 확인하지 못함 |

위 값은 **계획상 적용성 분류**이며 existing verification outcome/state를 임의로 교체하지 않는다. 참고 가능한 설명과 현재 pass를 구별한다. 모델 provenance는 모델이 시험 대상이거나 정의가 필요 조건으로 선택했을 때 조건에 포함한다.

### 3.7 ResumeOverview / ResumeBundle — 두 단계 문맥

| 단계 | 입력 → 출력 | 권한 경계 |
|---|---|---|
| 재개 개요 | 명시 scope·session·metadata 읽기 권한·예산 → 방향/진행 refs·기존 실행·주의/대기·선택 후보·실제 상세 읽기 방법 | 현재 허가된 metadata만; private 지시/working tree 내용/다른 owner spool은 읽지 않음 |
| 작업 문맥 | 실제 현재 점유/run·새 basis·정렬/근거 refs·role·예산 → 현재 목표/제약/방법·관련 변경·적용 근거·다음 행동·상세 refs | 새로운 내용 접근 직전 현재 권한/점유/source 확인 |

필수 내용은 목표·대전제·금지/자율 경계·현재 기준·기존 실행/pending·누락/unknown·다음 행동 조건이다. 부족한 예산에서 이를 누락한 완전한 성공을 반환하지 않는다. optional 과거 설명은 ref로 축약한다. 실제 유지한 리소스의 허가된 범위만 detail/cursor로 제공한다.

cache key는 요청 범위·관련 BasisVector·권한 범위/revision·policy/renderer version·role/예산 조건을 포함한다. 다른 branch/environment/새 session의 오래된 private 문맥·aliases를 무검증 재사용하지 않는다. 응답 전체 UTF-8 bytes/lines를 계산하며 provider token이 없으면 unknown이다.

### 3.8 NextAction / SessionLink — 다음 행동과 이력 연결

행동 후보는 `기존 결과 확인`, `기존 실행 관찰`, `대기`, `변경 분석`, `계획/근거 갱신`, `확정 Step 착수`, `검증/통합 검토`, `선택 요청` 의미를 갖는다. 이름은 현재 CLI 명령이 아니다. 후보마다 필요한 ref·basis·권한·점유·실제 정지 조건·이유·unknown을 반환한다.

SessionLink는 현재 session이 어떤 프로젝트·checkpoint·기존 결과를 참고했는지 기록한다. 이전 actor/device/session 소유권을 이 link로 승계하지 않는다. 닫힌 세션의 실행이 실제로 정지했는지, 다른 기기의 미반영 bytes를 가져올 수 있는지는 별도 조회·복구 조건이다.

## 4. 처리와 저장 포트

논리 포트의 의미를 먼저 확정하고 실제 함수/operation/endpoint 이름은 구현 때 registry와 함께 정한다.

| 포트 | 입력 → 결과 | 생산/소비 |
|---|---|---|
| CurrentFactsReader | scope/basis 선택·read authority → 현재 상태/ref·불완전성 | R1 → R4 |
| CheckpointStore | 확정 event·기대 pointer revision·basis/ref → immutable checkpoint·CAS receipt | R1/R5 → R2/R4 |
| ChangeCollector | 현재 owner/mapping·before/after basis → actual change refs·coverage | R2 → R3 |
| ImplementationLinkIndex | 현재 source·기능/모듈/코드 연결 → version-bound candidates·unknown | R2 → R3 |
| AlignmentCoordinator | 관찰·현재 계약·위임·근거 → assessment, 검토된 적용 → receipt | R3 → R4/R5 |
| ResumeComposer | 개요 또는 owner-bound task basis·refs·예산 → bundle/detail refs·조건부 next action | R4 → main/제품 |
| SessionAdapter | 제품 event·명시 scope·bounded result → 제품 native 주입 또는 명시 조회 참조 | R5 → 사용 세션 |
| ObservationRecorder | 실제 경계·상태·측정 → 업무 event/진단/증거를 각각 저장 | R0/R6 공통 |

role별 AI 의미 판단은 main/session의 기존 native/CLI 경로를 사용한다. Python이 user 의도·대전제 승인을 사실인 것처럼 생성하지 않는다. 변화가 없고 현재 근거가 유효한 구간에 불필요한 모델 분석을 다시 요청하지 않는다.

## 5. local/Host 경계

- Git/working tree·원본 검사/게시·모델·실행기·지역 spool은 클라이언트에 남는다.
- Host는 current auth/scope 아래 checkpoint pointer·관찰/정렬 metadata·증거 ref·권위 업무 상태·현재 원본 ref를 제공한다. SQLite 파일을 기기끼리 공유하지 않는다.
- `sync_project_baseline`은 현재 Host allowlist에 없다. CLI LocalStore 기반 동작을 원격으로 노출하는 방식 대신 client 수집 + 승인된 receipt 저장 경계를 설계한다.
- mutable pointer/반영은 current identity·expected revision·request fingerprint·fresh basis를 같은 write transaction에서 확인한다. replay보다 앞서 current 권한을 확인한다.
- metadata 조회는 작업 점유 권한을 주지 않는다. 원본/private 지시/working tree 접근은 별도 current owner/run/source 확인을 유지한다.
- DB와 Git/파일 게시의 효과를 하나의 원자 transaction으로 표현하지 않는다. 원 요청·effect 단계·hash·실제 파일 상태로 journal/recovery를 연결한다.
- Host 불통 중 저장된 개요를 offline 실행 권한으로 사용하지 않는다. 이미 생성한 결과 보존만 기존 pending 규칙으로 수행한다.
- 다른 장치의 local pending은 접근 가능한 Host metadata로 대기/미확인만 표시할 수 있다. 그 장치의 실제 bytes·receipt 없이 완료·재전송을 추측하지 않는다.

## 6. 호환·이관·실패

R0에서 additive metadata와 기존 generic storage/port의 재사용 여부를 비교한다. 필요한 DDL·schema/API version·구버전 거부·backup/import/restore·index rebuild·rollback을 함께 명세한다. 새 checkpoint를 기존 version에 몰래 끼워 넣거나 코드 작성 전 schema를 올리지 않는다.

| 실패 의미 | 필요한 처리 |
|---|---|
| scope/권한/프로젝트 미지정 | 거부 또는 허가된 선택 필요 metadata 반환 |
| stale source/revision/summary/cursor | 실제 변경을 조회하고 새 basis로 재구성; 이전 행동 실행 금지 |
| 내용/증거 부족 | incomplete/unknown과 상세 조회 ref·복구 불가 누락을 명시 |
| 지시·대전제 변경 | 영향 배정 중지/조정, 오래된 반환 보존, 현재 계획의 완료 금지 |
| 다른 owner/기존 실행 미확정 | 기존 실제 상태 조회·대기·기존 복구 절차; 시간 기반 takeover 금지 |
| 원본/게시 중단 | 원 request/effect/hash로 조정, old/candidate/user 변경 보존 |
| 통신·DB·파일 unknown | 확정 checkpoint pointer 유지, 원 효과 조회 전 재실행 금지 |
| 해석 이유 미확정 | 변경 사실은 남기고 intent unknown; 대전제를 임의 변경하지 않음 |

기존 CLI envelope와 exit 의미 0~5를 유지한다. HTTP 상태 숫자를 core exit로 넣지 않는다. 신규 attention/next-action 분류는 기존 run/Step/Work 상태와 별개다.

## 7. 이벤트·보존·경량화

같은 실제 행동은 request/event 재처리로 한 번 반영한다. 별개 사용자 선택은 내용이 같아도 별개 사건으로 보존한다. 반복 조회는 최근 관찰과 cache 상태를 갱신하고 의미 있는 전이만 업무 이력으로 저장한다.

종료된 일반 이력은 기존 3개월 정책, 미참조 임시 handoff/이미지는 1주일 정책을 적용한다. 현재 기준·유효 원리/결정·활성 checkpoint의 원본 refs·미해결 실행/pending/journal·재개 필수 증거는 참조 검토 전 삭제하지 않는다. summary/cache는 무효화·재생성 가능하게 관리하며 오래된 전체 본문을 session별로 복제하지 않는다.

원본 이력이 정리된 경우 재개에 필요한 **현재 확정 사실과 유지한 증거 refs**를 남기고 상세 과거가 사라진 사실을 표시한다. 삭제한 상세를 읽을 수 있다고 주장하지 않는다. 보존·backup/import에서 새 미해결 정렬/반영 journal을 버리지 않도록 R6에 연결한다.
