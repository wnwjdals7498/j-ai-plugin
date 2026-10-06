# 4단계 검증·로그·효율·완료 명세

2026-10-06. **예정 수용 기준**. 아래 ID는 아직 실제 시험 이름이나 통과 기록이 아니다. [상세계획](implementation-plan.md)의 각 기능을 실제 코드/Git/DB/파일/handle/모델 판단으로 확인하기 위한 기준이다.

## 1. 기능별 시험 ID

21개 ID를 구현의 실제 pytest/제품 action/증거 manifest에 연결한다. 한 ID는 여러 정상·부정·실패 주입 사례를 포함할 수 있다. 함수 개수로 수용 여부를 판정하지 않는다.

| ID | 사례·입력 | 확인할 실제 상태 |
|---|---|---|
| P4-R0-01 | 기존 schema/request·4단계 metadata·local/Host 포트의 정상/거부 | 기존 ID/의미/원본 보존, 실제 version·이관/backup·error envelope/exit 0~5 |
| P4-R0-02 | 명시/미지정 scope, foreign device/role/branch, stale revision/caller Boolean | 권한 없는 데이터 접근·원본 읽기·checkpoint/next-action 승격이 없음 |
| P4-R0-03 | 같은 event/request/body·다른 body·두 process pointer CAS·부분 실패 | 한 번 반영, 독립 사용자 사건 구별, stale CAS rollback·미확정 ref 보존 |
| P4-R1-01 | 계획뿐인 기능·구현 존재·실제 검증 pass/fail/not_run, 현재 결정과 폐기 | 사실 수준이 구별되고 코드/계약/시험 refs가 올바름; 모델 문구만으로 verified 없음 |
| P4-R1-02 | 확정 경계 checkpoint→새 session→조회 재처리/Hook 없음 | 마지막 확정 refs로 현재 상태 재조회, 중복 checkpoint/상태·의도 추정 없음 |
| P4-R1-03 | source/업무 revision이 조합 중 변함·pointer/capture 실패 | 혼합 basis 성공 없음, 제한 재수집/incomplete와 마지막 확정 pointer 유지 |
| P4-R2-01 | 기준 동일·관련/무관 새 commit·문서/user event 변화 | 실제 diff·지문 일치, 같을 때 반복 분석 없음, 변경 의도 근거/unknown 구별 |
| P4-R2-02 | dirty·rename/delete·branch 전환·이력 분기·non-Git | 실제 파일과 owner 보존, 코드 경로 이동 ref 유지, 분석/반영 기준 무단 전진 없음 |
| P4-R2-03 | 누락 mapping·동적/미지원 코드·외부 완료 주장·수집 중 변경 | coverage와 unmapped 명시, actual source/receipt 미일치 거절, 의미상 무영향으로 추측하지 않음 |
| P4-R3-01 | 위임 범위의 방법 변경·대전제 변경·이유 미확인 변경 | 자율 수정/검토 요청 구별, 결정 이유/선택과 현재 적용 범위 추적 |
| P4-R3-02 | 유효 원리/검증·관련/무관 조건 변화·후속 실패·손상 증거·통합 source | applicable/불가/unknown 실제 조건 대조, 필요한 재확인만 선택; 기존 pass 재해석 없음 |
| P4-R3-03 | 반영 중 crash·게시 응답 유실·새 source·진행 run·늦은 결과 | same effect/revision/source 조정, old/candidate/무관 가지 보존, old 결과로 새 계획 Done 없음 |
| P4-R4-01 | 역할·범위·여러 budget·많은 이력·필수 내용 너무 큼 | 응답 전체 bytes/lines 제한, 목표/금지/기존 실행/unknown 보존, 필수 부족은 incomplete |
| P4-R4-02 | 오래된 summary/cache·권한 폐기·이력 정리·옛 alias/cursor | 실제 current auth/basis recheck, 보존 detail만 조회, 이전 ref가 실행 권한으로 쓰이지 않음 |
| P4-R4-03 | running/review_pending/pending/무변화/자율 수정/충돌 조합 | next-action 조건이 현재 Query/Queue/control/review 경계와 일치; 중복 실행·auto unlock 없음 |
| P4-R5-01 | 제품별 stable event/session·명시 Project/Work·미지정 scope·새 환경 | 제품 출력/명시 조회 형식 적합, 프로젝트 임의 선택 없음, 허가된 bounded metadata 주입 |
| P4-R5-02 | 중복/역순/누락 Hook·timeout·main 종료·late 결과 | Hook 실패 안전 표시·pending 재처리·확정 checkpoint 복구, Stop/idle를 Done으로 사용하지 않음 |
| P4-R5-03 | prompt/transcript·credential/env/PID/path sentinel·log 저장 실패 | 일반 개요·업무/진단 log·관리툴에 민감 원문 없음, 진단 실패와 업무 rollback 구별 |
| P4-R6-01 | 두 device/env·같은 repo branch 다른 경로·다른 branch·Host 불통/restart·이관 | canonical 충돌/격리·current auth·exact replay, offline 신규 작업 없음, 미해결 refs/원본 보호 |
| P4-R6-02 | 실제 격리 local/HTTPS 전체 flow·새 native AI session·제품 update/reinstall | 구현/문서/source/기준·다음 행동 정합, bundle만 읽은 새 AI의 판단 확인; 제품 tier 구별 |
| P4-R6-03 | 동일 목표·수용·source·환경·역할/모델·정의의 이전/개선 경로 | 전체 읽기/생성/전송/상세/실패/검토/재작업 비용·시간·품질과 비교 불가 이유 |

## 2. 필수 통합 시나리오

| 시나리오 | 재현할 흐름 | 성공과 실패의 경계 |
|---|---|---|
| 무변화 재개 | 확정 checkpoint→새 session→동일 source와 현재 실행 조회→문맥 | 같은 변경/검증/작업을 다시 실행하지 않음; metadata 확인 비용은 측정 |
| 외부 개발 반영 | 다른 작업자의 실제 commit→새 session→actual diff/mapping→영향/정렬 | 관련 계약·계획·근거만 갱신; 알려지지 않은 이유는 미확인 |
| 미커밋 이어가기 | 기존 owner dirty 작업→session 중단→같은/다른 environment 조회 | bytes/진행 보존, 현재 권한 없는 session takeover·reset 금지 |
| 큰 기준 변경 | 사용자 대전제 수정→영향 가지/run 확인→정지/확인→재계획 | old 지시/결과로 새 목표 완료 없음; 무관한 가지 유지 |
| 결과 유실 | 실제 실행 종료→result POST commit·응답 폐기→session 재개 | 원 request/fingerprint 조회 후 회수, 두 번째 실행/완료 event 없음 |
| 다른 기기의 local pending | 첫 장치 offline spool→두 번째 장치에서 Host 상태 조회 | 실제 bytes 없이 submit/완료하지 않고 대기/미확인 이유 표시 |
| 근거 불가 | 이전 pass 후 관련 정의/환경 변화 또는 evidence 손상 | old 원리/시험은 설명 refs로만 참고 가능, 현재 pass로 사용하지 않음 |
| 혼합 snapshot | bundle 구성 중 source/record version 변경 | 사용했던 기준·누락/불일치 명시, 조합된 내용을 현재 확정으로 게시하지 않음 |
| 보존 정책 이후 재개 | 오래된 상세 정리·현재 refs/근거 유지→새 session | 현재 작업에 필수인 증거/미해결 상태 보존, 삭제한 상세는 unavailable |

## 3. 실행 계층

1. 순수 규칙: typed input·ref/version/hash·budget·coverage·오류·행동 조건 fixture.
2. 본체: 실제 격리 SQLite/Git/문서/resource, 별도 process CAS, 실제 local fixture runner/receipt.
3. Host: 실제 loopback HTTPS·독립 client/device/env, scope/replay/복구·원본과 Host SQL/file 비교.
4. 모델: **새로운 독립 native AI context**에 재개 bundle과 허용된 detail만 전달해 의미/행동 판단 확인.
5. 제품: 실제 설치/등록·새 제품 세션·Hook/native 출력·권한을 확인. adapter fixture는 이 계층 성공이 아니다.

현재 허용 범위는 기존 native 서브에이전트와 local CLI fixture·격리 localhost다. 직접 API/Claude 실서비스 전송·외부 운영 Host·실제 user profile/data 전환을 시험 때문에 추가하지 않는다. 필수 제품/권한이 없으면 blocked/not_run을 기록한다.

모델 시험에서 이전 대화·전체 source·다른 변형의 답안이 새 context에 유출되면 순수 재개 이해 비교로 사용할 수 없다. 전달한 실제 bytes/ref/hash와 사용한 detail 호출, 수행 모델/능력/경로를 기록한다. 모르면 추가 조회 또는 미확인 판정을 해야 하는 사례도 포함한다.

### 새 AI session 판정 항목

- 현재 목표/대전제/금지/위임을 올바르게 추출한다.
- 현재 구현과 계획·불확실한 상태를 구별한다.
- 적용할 원리/증거의 조건과 반례·손상/변경 사유를 구별한다.
- 이전 실행/pending을 재실행하지 않고 실제 상태를 확인한다.
- 현재 기준 이후 변경과 관련 범위를 파악한다.
- 허용된 다음 행동을 선택하거나 정확한 unknown/선행 조건을 제시한다.
- unavailable한 상세나 permission을 확보했다고 추측하지 않는다.

golden 기준과 실제 refs는 해당 scenario 작성/관찰 주체가 별도로 고정한다. 평가에 쓴 기대 답안을 대상 모델에게 알려주는 방식은 사용하지 않는다. 모델 출력 성공과 시스템 상태 성공은 따로 확인한다.

## 4. 운영·측정 기준

성능/예산 수치는 기존값을 조사한 뒤 R0에서 benchmark별로 확정한다. 실제 수치 없이 향상·절감률을 약속하지 않는다.

| 항목 | 기록할 의미 |
|---|---|
| 규모/환경 | 관련 graph/node·문서/코드/이력 규모, 동시 요청 수, CPU/memory·OS·Python·dependency/tool/config·namespace/mode 조건 |
| 경계 | Hook 추가 지연, overview 생성, source 수집, 영향/정렬, task 문맥 생성·상세 조회, 실제 이어가기 wall time |
| 시간 | 반복 수·cold/warm·분포·평균/선택 percentile·timeout/오류율, 측정 시작/끝 범위 |
| 입력/전송 | source/문서 읽기 bytes·호출 수, bundle/상세 bytes, Host request/response bytes, 필수 보존·생략 |
| AI 비용 | 의미 분석/재분석/질문/검토/재작업 호출과 실제 usage; 제공 안 됨은 unknown |
| 재사용 | 조건 확인 비용·hit/miss/invalid/unknown·실제로 생략한 동일 정의의 재실행 여부 |
| 품질 | 요구·금지/위임·유효 증거 보존, 잘못된 행동/혼합 basis/unknown 누락, retry/rework와 실제 결과 |
| 비교 | 같은 goal/acceptance/source/environment/role/model/policy/definition인지와 불일치 이유 |

무변화 재개의 개선 목표는 반복 전체 분석·같은 검증/실행을 줄이는 것이다. 이를 위해 하는 coherence/auth·재사용 조건 조회 비용도 합산한다. 요약이 짧아져도 detail·재작업·질문이 늘면 전체 비용을 따로 평가한다.

Hook은 configured deadline 안에 metadata 결과·명시적 추가 조회 또는 안전한 오류를 반환해야 한다. 현재 1.5초 timeout 구현은 조사 기준이며 모든 환경의 성능 성공값으로 간주하지 않는다. mandatory 내용이 budget보다 크면 incomplete를 반환한다. 앱 종료 후 자동으로 계속 실행/깨어남을 보장하는 항목은 제외한다.

## 5. 로그·업무 이력·증거

| 종류 | 저장하는 내용 | 확인할 실제 상태 |
|---|---|---|
| 업무 event | 의미 있는 결정·checkpoint 확정·변경 반영·실행 상태 전이 | DB revision·원 event/request·적용 receipt |
| 최근 관찰 | 현재 실행/상태·마지막 실제 확인·대기/오류 | 같은 handle/run·known/unknown·조회 outcome |
| 진단 | 안전한 코드·크기·시간·정책·scope/basis/ref/hash·요청 결과 | SQL/파일/전송 실제 outcome, log sink 실패 신호 |
| 시험 증거 | 대상/정의/환경·실제 command/action·exit·전후 state/hash·관찰 자료 | Git/DB/파일/물리 receipt 또는 새 모델 실제 응답 |

제안 event 범주는 `현재 사실 수집`, `checkpoint 생성/충돌`, `변경 관찰/불완전`, `정렬 평가/반영/확인 필요`, `재개 bundle 생성/stale`, `session 문맥 전달/실패`, `근거 적용성 확인`, `다음 행동 제안`이다. 실제 event 문자열과 허용 trace field는 R0에서 기존 registry와 함께 확정한다.

공통 trace: UTC 시각·component·contract/policy/rule version·request/event/correlation·namespace/project/task/run·허가된 identity refs·basis/checkpoint/change/alignment/bundle·source/범위/manifest/evidence hash·old/new revision·outcome/reason·duration/bytes/count/cache/incomplete·다음 행동/unknown 이유.

credential/claim token·전체 환경변수·전체 대화/지시·raw argv/PID·로컬 절대경로·diff/source 본문은 일반 log·관리 개요에 넣지 않는다. 별도 source/evidence 읽기는 실제 권한과 retained ref에 한정한다. 비밀 제외는 field 이름뿐 아니라 실제 값/오류 문자열 sentinel로 확인한다.

업무 write/필수 근거 저장 실패는 완료 성공을 반환하지 않는다. 진단 log 실패는 신호를 남기며 이미 반영한 업무를 재실행하지 않는다. 현재 확정 pointer와 미해결 journal/resource를 보존하고 실제 hash/outcome으로 조정한다.

## 6. 증거 manifest

각 실행은 test/scenario/정의 version·수용 조건·계층·actual command/action/exit·UTC 시각·current commit/dirty/source 파일 SHA·BasisVector·제품/tool/environment/config/모델/policy·request/event/job/run/effect IDs·DB/파일 관찰·resource/hash refs·결과·source 변경 중 여부를 기록한다.

상태는 pass/fail/blocked/not_run/skip/이후 무효를 구별한다. 전체 실행 실패를 삭제하거나 현재 선택 재검증을 전부 다시 통과한 결과로 합산하지 않는다. 동일 source/조건의 증거만 관문에서 재사용한다. source가 변했으면 실제 영향을 받은 가족과 namespace/포트/스키마의 필수 회귀를 다시 확인한다.

## 7. 최종 수용과 인계

메인은 21개 ID를 실제 사례/증거에 연결하고 R0~R6 목표·호환·현 권한/원본·복구·모델 의미·제품 계층·측정을 확인한다. 일부 fixture만으로 전체 수용을 선언하지 않는다.

최종 보고에는 구현/API·schema/이관 version, actual 지원 제품/local/Host 범위, 최신 source/evidence/package hash, 통과/실패/미실행/제한, 예상하지 못한 현재 위험과 복구 방법, 토큰/비용/속도에서 확인한 사실·unknown을 넣는다. 실제 설치·배포·user primary 전환은 별도 허용 범위를 따른다.

4단계 문서 작성 시점에는 시험을 실행하지 않았다. 이 파일은 구현자와 검증자가 사용할 정의이며 통과 횟수를 제공하지 않는다.
