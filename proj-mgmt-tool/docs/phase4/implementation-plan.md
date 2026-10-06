# 4단계 상세 작업 계획

2026-10-06. 상태: **배정에 사용할 상세계획**. `gpt-6-luna` 세 작업자가 병렬로 작성하고 메인이 연결 계약과 의존성을 통합했다. 구현·시험·제품 수용은 미착수다. 코드 조사 기준은 `ac7aded`/Core 0.3.0/SQLite 4/graph 1이다. [전체 범위](../04-session-continuity.md), [공통 계약](contracts.md), [연결 규격](implementation-interfaces.md), [검증](verification.md)을 함께 읽는다.

각 작업은 기능 책임·의미 입출력·기술·선행·관찰 가능한 완료를 지정한다. 실제 파일 소유권은 착수 때 협의하며 계획을 특정 파일의 내용 추가/수정/삭제 지시로 분해하지 않는다.

## 배정 문서와 최소 읽기 범위

| 담당 | 상세계획·논리 Step | 기능 책임·인수할 결과 |
|---|---|---|
| 메인 | 이 문서 R0-S1~S4 + [공유 연결 규격](implementation-interfaces.md) | 값·권한·저장·호환·시험 기준, producer/consumer 계약, 통합·최종 판정 |
| A — `gpt-6-luna` | [현재 사실·재개 문맥](plan-current-context.md): R1-S1~S4, R4-S1~S4 | 사실 수준·basis·checkpoint·개요/점유 후 문맥·상세/조건부 행동 |
| B — `gpt-6-luna` | [변경·방향 반영](plan-change-alignment.md): R2-S1~S4, R3-S1~S5 | 실제 변화·코드/기능 연결·적용성·의미 제안·부분 반영 receipt |
| C — `gpt-6-luna` | [세션·Host·통합](plan-session-host-integration.md): R5-S1~S4, R6-S1~S6 | 제품 연결·저장 전용 Host·복구/이관·실제 새 세션·전체 비용·패키지 |

총 31개 논리 Step은 PMT 업무 ID와 별개다. 배정 시 실제 Work/Item/Step에 연결한다. 작업자는 공통 계약·연결 규격·자기 계획·직접 선행의 인계만 읽고, 다른 영역의 전체 문서를 반복 통독하지 않는다. 아래 R별 요약은 범위 안내이며 실제 실행에는 담당 상세계획의 실패·복구·시험 조건까지 포함한다.

추가/수정은 해당 기능 책임으로 제한한다. 삭제는 원본 삭제와 파생물 사용 중단을 구별한다. 기본 원본 삭제는 없으며, superseded mapping·cache·임시 stage만 명시된 보존/무효화 조건 아래 정리한다. 현재 checkpoint·필수 근거·미해결 journal이 참조한 데이터는 삭제 대상이 아니다.

## R0 — 공통 연결·호환·판정 계약

- 목적/이유: 과거 요약·현재 Git·업무 상태·AI 해석을 서로 다른 원본으로 처리하거나 소유권을 혼동하지 않도록 공통 기준을 확정한다.
- 추가: BasisVector·Checkpoint·ObservedChange·AlignmentAssessment/Receipt·ResumeOverview/Bundle·NextAction/SessionLink의 의미와 버전형 저장·read/write 포트.
- 수정: 기존 SourcePin·baseline·context·claim/run·verification·host allowlist와의 연결 의미, 실패와 attention의 구별.
- 금지: 신규 업무 depth, 역할에 따른 인증 우회, 전체 대화/환경 dump, 기존 run 상태 임의 대체.
- Goal: 생산자와 소비자가 같은 basis/ref/권한/CAS/unknown/재처리 의미를 사용한다.
- Non-goal: 일반 agent 플랫폼, 새 모델 API, 기존 전체 dispatcher의 Host 공개.
- 입력: 현재 실제 operation·schema·제품 callback·source/runtime 권한 경계, 사용자 결정, 기존 성공/미실행 증거.
- 출력: 호환/확장 표, 공통 값 객체·ports·오류·event/trace 규칙, 저장/이관/backup 영향, 신규 기능의 논리 registry.
- 기술: Python 표준 라이브러리 구조화 값·canonical JSON/hash·SQLite CAS·기존 리소스/journal/request cache. 새 의존성이 꼭 필요하면 목적/대안/공식 근거/설치 영향을 메인에게 제출한다.
- 선행: 1~3단계 실제 코드/계약 확인. 소비자의 fixture 준비는 가능하지만 독립 wire/schema를 먼저 확정하지 않는다.
- 시험: P4-R0-01~03. 로그에는 계약 version·source/ref·scope/identity·오류/충돌 코드와 실제 outcome을 남긴다.
- 완료/인계: 공통 계약과 소비자가 사용할 실제 경계, 호환/미지원 표가 확정되고 source·권한·요청 재처리 부정 시험이 연결된다.

| 단계 | 처리와 산출 | 순서 |
|---|---|---|
| R0-S1 | 기존 local/Host·개요/작업 문맥·제품 callback의 실제 지원과 보완점을 조사 | 먼저 |
| R0-S2 | 값 객체·권한·역할·source coherence·상태/attention·read/write 의미 확정 | S1 뒤 |
| R0-S3 | 필요한 additive 저장·schema/포트·이관·journal/ref 보호 설계와 계약 시험 | S2 뒤 |
| R0-S4 | 동일 목표/환경/모델의 기존 재개 사례·측정 기준 manifest 고정 | S2 뒤 독립 가능 |

### R0-S1 — 실제 지원·권한·호환 경계 목록

- 목적/이유·Goal: 작업자가 기존 CLI 기능을 Host 지원으로 오해하거나 metadata 권한을 실행 권한으로 높이지 않도록 현재 경계를 고정한다.
- 범위: local/hosted dispatcher·StorePort·제품·source/업무 읽기 등급의 지원표와 미지원 사유 추가. 기존 경계의 실제 소비 관계 보완. 삭제 없음. Non-goal: 이 조사만으로 신규 operation 공개·제품 설치·사용자 설정 변경.
- 의미 입력: 실제 registry/handler·현재 schema/프로토콜·설치 자산·기존 시험 근거·사용자 범위 결정의 refs/version.
- 의미 출력: operation별 read/write/client-file/metadata/private 분류, 현재 authorization 및 provenance, 기존 이름/의미 보존 범위와 신규 연결 공백 목록.
- 방법/기술: 원본 Python/Node adapter 및 실제 registry를 따라 producer→service→store를 추적한다. 이름 검색 결과만으로 지원을 판정하지 않는다.
- 선행/소비자: 기존 단계 계약 확인 → R0-S2/S3, A/B/C capability fixture.
- 시험/로그: P4-R0-01/02. 기존 경로와 신규 경로의 허용/거부, source read 전 권한 검사를 실제 handler 호출에 연결할 fixture·기준 refs를 인계한다. 지원 판정의 code/version/ref와 미확인 이유를 기록한다.
- 완료: 모든 신규 논리 포트에 구현할 위치가 아닌 책임 서비스·권한 등급·local/Host 경계·호환 상대가 지정된다.

### R0-S2 — 공통 값·포트·오류·인계 계약

- 목적/이유·Goal: A/B/C가 독립적으로 같은 객체를 다르게 해석하지 않도록 입력의 의미·필수성·실패·응답을 확정한다.
- 범위: [연결 규격](implementation-interfaces.md)의 18개 논리 호출·4개 adapter에 필수/선택/type·정규화·상호제약·version·visibility·호환표를 부여한다. 기존 권한/SourcePin/request 의미와 연결한다. 삭제 없음. Non-goal: 새 역할 인증·새 업무 계층·별도 실행 상태 머신.
- 의미 입력: R0-S1 지원표, 현재 UUID/ref/revision/SourcePin·요구/위임·scope 규칙, producer/consumer 요구와 실패 사례.
- 의미 출력: 소비자가 공유하는 구조화 계약·canonical hash/fingerprint 의미, current/stale/incomplete/unknown·attention·core exit 0~5 대응, source-bound cursor/cache/next-action 조건.
- 방법/기술: Python 값 객체·입출력 검증·canonical JSON/hash, 명시 registry와 기존 envelope. 의미적 제안 이름은 이 단계에서 실제 operation/함수와 연결하고 공유한다. wire 권한 면에 caller 지정 system owner를 두지 않는다.
- 선행/소비자: S1 → S3/S4와 A/B/C. 구현체 없이 계약 fixture는 준비할 수 있으나 실제 연결 성공으로 사용하지 않는다.
- 시험/로그: P4-R0-01/02/03의 정상·누락·타 범위·stale·같은 request/다른 body fixture. 필드/version/분류·source/권한·오류와 실제 outcome만 기록한다.
- 완료: 모든 producer/consumer가 같은 DTO/실패 의미를 사용하고, contract 변경 제안의 소비자·호환·필수 시험 영향이 추적된다.

### R0-S3 — 공유 저장·CAS·journal·보존 기반

- 목적/이유·Goal: session-bound 사적 저장과 프로젝트 공유 metadata를 분리하고 중복·충돌·부분 효과를 복구 가능하게 만든다.
- 범위: 권한 어댑터·immutable objects/latest pointers·원 request/event receipt·reference protection 추가. 기존 storage/transaction/replay·이관/backup 목록을 연결한다. 삭제는 검증된 보존 대상 정리만. Non-goal: caller 권한 우회·network SQLite·실제 사용자 DB 전환·파일/DB 전체 원자성 주장.
- 의미 입력: S2 계약, 기존 owner_actor/owner_session·scope 검사, 원 업무 event와 현재 auth/WorkAccess, 기대 pointer/revision·source hash, 현재 schema/backup/export/quiescence 목록.
- 의미 출력: 공유/사적 객체 접근·CAS/replay 포트, additive 저장 및 실제 version/이관 판정, effect별 journal 단계·unknown 복구, 보존/backup 포함 또는 명시 거부 규칙.
- 방법/기술: 기존 SQLite·짧은 transaction·request ledger·resources/journal을 우선 재사용한다. 기존 generic 객체로 표현해도 허용 kind·namespace 접근·export·quiescence 검사를 실제로 확장한다. 새 DDL이 필요하면 additive migration/version reject를 함께 제공한다. graph schema 1·CLI protocol-v1은 호환 가능한 한 유지한다.
- 선행/소비자: S2 → A checkpoint/Basis, B receipt/apply, C Host/이관. 저장 객체·pointer·업무 event/재처리는 가능한 같은 DB transaction, 실제 파일/graph는 journal 이후 readback으로 연결한다.
- 시험/로그: P4-R0-01~03. 실제 별도 프로세스 DB CAS·commit 뒤 응답 유실·current auth 회수 후 replay·부분 resource 실패·old/new DB와 backup/import 전후 refs/hash를 대조한다. request/event/effect·기대/현재 revision·journal 단계·원 결과 조회와 복구 outcome을 기록한다.
- 완료: 현재 권한 검사가 replay보다 먼저이며 원 요청의 결과가 한 효과로 수렴한다. 미해결 refs를 지우는 backup/retention 경로가 없고, 구버전에서 읽을 수 없는 데이터는 명시 거부된다.

### R0-S4 — 재개 품질·효율 비교 기준

- 목적/이유·Goal: 더 짧은 요약만으로 효율 성공을 주장하지 않고 현재 방향을 정확히 이어가는지와 전체 비용을 비교한다.
- 범위: 이전/개선 경로의 격리 scenario·평가 기준·측정 manifest·실행 tier 추가. 기존 measurement provenance와 연결. 삭제 없음. Non-goal: 미측정 절감률·모델 token 추정의 actual 표기·fixture의 제품 성공 승격.
- 의미 입력: 같은 목표/수용/source/환경/역할/모델·정의, 무변화/관련 변경/미확정 실행/근거 불가 사례, 실제 허가된 실행 면·지표 가용성.
- 의미 출력: 비교 가능한 기준 사례와 품질 항목·전체 비용 경계, 실제/추정/미상 판정, independent-context 모델에 공개할 문맥 및 분리 보관할 평가 근거.
- 방법/기술: 기존 measurement recorder·HttpStore observer와 actual bytes/lines/count/time. 이전 경로는 당시 기능을 그대로 실행하며 새 경로 자료를 몰래 제공하지 않는다. 품질은 목표/금지/위임·사실 수준·기존 실행·근거 적용성·unknown·행동 조건의 정합으로 판정한다.
- 선행/소비자: S2 뒤 독립 준비 → C R6-S3~S5 및 A/B 회귀. 실제 모델 시험은 과거 대화 없는 native `gpt-6-luna`를 사용한다.
- 시험/로그: P4-R6-02/03의 기준 정의와 R0-01 측정 호환. 모든 detail/재검토/실패/Host 전송을 포함하고 필수 의미 누락·중복 실행 유도가 있으면 품질 불합격이다. iteration·warm/cold·source/model/정의·가용 지표와 비교 불가 이유를 기록한다.
- 완료: 실행 전 품질/비용의 시작·끝과 비교 조건이 고정되고, 결과를 보고 기준을 낮추지 않는다. 실제 절감 여부는 R6 실측 뒤에만 보고한다.

## R1 — 현재 사실·구현 상태·체크포인트

- 목적/이유: 새 AI가 계획·실현·검증·미확인·현재 실행을 구별하고 마지막 확정 지점을 찾도록 한다.
- 추가: CurrentFactsReader, implementation fact projection, immutable checkpoint와 최신 pointer, session의 참조 이력.
- 수정: 기존 Work/Item/Step·결정·run·claim·progress·pending·artifact refs를 재개 목적의 연결 정보로 조회.
- 범위: 관련 프로젝트/작업의 현재 상태와 최소 의미 있는 경계. private Step 원문은 일반 개요에서 제외한다.
- Goal: 다른 session이 대화 기록 없이 현재 목표·현황·원 실행과 다음 행동 조건의 근거를 찾는다.
- Non-goal: 전체 코드에서 의도를 자동 확정, session마다 프로젝트 본문 복제, 관찰만으로 완료.
- 입력: 명시 scope·read authority·선택 작업·현재 records/decisions/run/claim/pending와 검토된 계약·코드/증거 refs.
- 출력: 구현 확인 수준이 구별된 현재 사실, component capture refs와 coherence 결과, 원 이벤트에 연결된 checkpoint/CAS receipt.
- 기술: 짧은 SQLite read snapshot/write transaction, deterministic JSON/hash, version형 immutable metadata와 pointer; 상세는 resources/ref. 오래 걸리는 Git·AI 작업을 DB write 안에서 기다리지 않는다.
- 선행: R0. 관련 코드 연결 index의 초기 대역을 사용해 준비할 수 있으나 실제 R2 mapping으로 재검증한다.
- 시험/로그: P4-R1-01~03, 현재·계획·검증 구별, checkpoint 재처리/CAS, capture 중 변화/실패. checkpoint/basis/hash/이유·old/new revision·재처리 여부를 기록.
- 완료/인계: stale/혼합 snapshot을 성공으로 반환하지 않고 현재 상태를 다시 조회해 복구한다. R2/R4에는 원본과 파생 정보의 실제 읽기 경계를 제공.

| 단계 | 처리와 산출 |
|---|---|
| R1-S1 | 방향/결정·현재 구현·미확인·실행/pending을 source refs로 구분해 조회 |
| R1-S2 | 관련 문서/graph와 업무 snapshot을 전후 검증하는 BasisVector 구성 |
| R1-S3 | 확정 event에 immutable checkpoint를 생성하고 최신 pointer를 CAS 갱신 |
| R1-S4 | 근거 없는 실행/완료·pointer 유실·중복 callback·읽기 중 변화에서 복구 확인 |

## R2 — 변경 수집과 코드·계약 연결

- 목적/이유: 다른 개발자·AI 세션·기기의 실제 변경을 현재 프로젝트 목표와 연결한다.
- 추가: 실제 Git/working-tree/doc/decision 변경 수집, implementation link index, unmapped 영역과 수집 receipt.
- 수정: 기존 Git baseline 분석·SourcePin·graph 조회를 관찰/분석/반영 단계와 연결.
- 범위: branch/history·관련 코드/문서/요구/모듈/시험 mapping. 필요한 코드 읽기는 현재 작업 범위 확보 후 수행한다.
- Goal: before/after와 관련 범위가 재현 가능한 변경 자료를 제공하고 의미 미확인 영역을 유지한다.
- Non-goal: commit 메시지로 user 의도 승인, working-tree 수정/merge/reset, 모든 언어의 완전한 static 분석.
- 입력: 명시 mapping·owner/run·이전 확정 basis·현재 실제 source, 검토된 기능/모듈/경로 연결과 외부 변화 후보.
- 출력: 추가/수정/삭제/이동 사실·diff ref/hash·coverage·후보 영향 경로·intent known/unknown·중복 수집 조정 receipt.
- 기술: shell=False의 기존 Git 호출·표준 파일/JSON 검증·기존 graph traversal. 코드 parser는 확인한 언어·표현의 한정된 추출자로 사용하며 미지원/동적 영역을 unknown으로 남긴다.
- 선행: R0; 실제 checkpoint/basis 연결은 R1 뒤. mapping 대역·순수 변경 fixture는 R1과 병렬 준비 가능.
- 시험/로그: P4-R2-01~03. commit/ref/dirty/change/index hash·추출 규칙 version·mapped/unmapped count·이유를 기록하고 raw diff/대화는 일반 로그에 복제하지 않는다.
- 완료/인계: 변경이 없으면 같은 범위를 반복 분석하지 않고, branch/이력 단절·dirty 소유 변경·증거 누락은 미확인으로 반환한다. R3에 actual before/after와 coverage를 전달.

| 단계 | 처리와 산출 |
|---|---|
| R2-S1 | 명시 mapping·범위·source authority 아래 Git commit와 dirty 차이 수집 |
| R2-S2 | 코드 경로/심볼·기능/모듈·요구/Step/시험의 stable ref 연결과 version-bound index |
| R2-S3 | 문서·결정 event·외부 변화 후보를 실제 원본과 대조해 change receipt로 통합 |
| R2-S4 | branch 변경·rename/delete·동적 연결·수집 중 변화·중복 요청·non-Git을 확인 |

## R3 — 영향·근거 적용성·방향 반영

- 목적/이유: 실제 변화가 작업 방향과 이전 근거에 미친 영향을 확인하고 허용된 수정으로 이어간다.
- 추가: AlignmentAssessment/Receipt, 원리·검증의 적용성 표, 위임/확인 이유와 조건부 다음 행동.
- 수정: 기존 graph 영향·F6·verification·문서 부분 게시·계획/지시 version 갱신과 연결.
- 범위: 관련 요구·구현 방법·문서·Step·근거; 불명확한 의미·대전제·충돌의 전달.
- Goal: 어떤 사실/원리/시험 때문에 어떤 변경을 반영했는지 추적하고, 유효한 무관 근거는 재사용한다.
- Non-goal: Python의 자동 의도 결정, 관련성 미확인인데 pass 재사용, 임의 대전제 변경, 전체 검증 무조건 반복.
- 입력: actual change와 mapping coverage, 현재 목표/결정/위임, 기능 계약/graph, 선택된 증거·조건, 현재 owner/source/revision.
- 출력: 변경 영향·불변/unknown·근거 적용성·위임 안의 수정 또는 확인할 선택지, applied refs/revision·반영 receipt.
- 기술: Python은 조건·지문·관계와 검증 가능성을 비교한다. 상위/main 모델이 의미·방법을 판단하고, 구조/큰 기준 변경이 필요하면 최상위 역할로 검토한다. 직접 API를 필수 전제로 두지 않는다.
- 선행: R1, R2. 새 code diff를 그대로 기존 F3 graph delta라고 간주하지 않고 검토된 graph 변경으로 연결한다.
- 시험/로그: P4-R3-01~03. 판단 규칙·basis·위임 ref·candidate/selected action·이유/unknown·원/현재 evidence·적용 outcome과 게시 단계 기록.
- 완료/인계: 적용 뒤 basis 재검증과 receipt 완료 시에만 `반영 완료` 기준을 올린다. unknown·현재 실행·지시 변경을 R4가 볼 수 있도록 보존.

| 단계 | 처리와 산출 |
|---|---|
| R3-S1 | 검토된 관계로 실제 영향·coverage 부족과 관련 검증/원리 후보 계산 |
| R3-S2 | 정의가 선택한 현재 조건·증거/후속 실패로 applicable/not_applicable/unknown 판정 |
| R3-S3 | 위임 범위에 맞는 의미 해석·대안·방법을 결정하거나 이유와 선택지를 요청 |
| R3-S4 | 원 source/revision을 확인해 계획·문서·graph·지시/근거 상태를 부분 갱신 |
| R3-S5 | 진행 실행·늦은 결과·게시 중단을 조정하고 receipt로 기준 갱신 |

## R4 — 재개 개요·작업 문맥·다음 행동

- 목적/이유: AI가 모든 문서/이력을 다시 읽지 않고 필요한 기준과 현재 변화만 파악한다.
- 추가: ResumeOverview, ResumeBundle, 조건을 명시한 NextAction, source-bound detail/cursor·cache 정책.
- 수정: `read_context`와 F5의 필수 내용·재개·권한·alias·예산 검증, F7의 실제 결과 상세 읽기.
- 범위: metadata 개요와 실제 점유 뒤 작업 문맥을 두 단계로 나눠 제공한다.
- Goal: 제한된 문맥으로 방향·구현·변경·근거·기존 실행·다음 행동을 정확히 파악한다.
- Non-goal: 요약을 독립 원본/실행 grant로 사용, 삭제된 내용의 상세 읽기, 부족한 필수 내용 숨기기.
- 입력: scope/선택 작업·role·현재 authority, basis/checkpoint/assessment/evidence refs, configured budget·제품 전달 한도.
- 출력: 짧은 사실과 출처·현재성/완전성·주의/대기·행동 조건·실제 보존 상세 refs; owner-bound 작업 지시/변경 bundle.
- 기술: Python 결정적 투영·선택 우선순위·UTF-8 bytes/lines·ref pagination·cache. 의미 요약이 필요한 구간만 AI에 요청하고 동일 basis/조건에서는 재사용한다.
- 선행: R1, R3. 생성기 skeleton은 R0/R1 뒤 fixture로 준비 가능하지만 실제 방향 판단 성공은 R3 뒤다.
- 시험/로그: P4-R4-01~03. mandatory 누락·size·role·source/scope hash·cache hit/miss·detail count·next-action reason·incomplete 기록.
- 완료/인계: 필수 기준을 보존하고 예산 부족·stale를 명시한다. action은 현재실행 재확인 조건을 갖고 기존 controller/Queue 밖의 독자 실행을 하지 않는다.

| 단계 | 처리와 산출 |
|---|---|
| R4-S1 | 방향·현황·현재 원 실행/pending·확인/선택 필요를 개요로 조합 |
| R4-S2 | 점유 뒤 관련 변경·현재 지시·근거 적용성·제약을 task 문맥에 구성 |
| R4-S3 | 현재 status와 action 선행 조건으로 기존 조회/대기/검토/실행 경계에 제안 연결 |
| R4-S4 | stale/cache/alias/detail/예산 부족·불일치 basis를 검증하고 제한적으로 재구성 |

## R5 — Hook·스킬·세션 연결

- 목적/이유: 새 세션에 재개 개요를 전달하고 작업의 의미 있는 경계를 놓치지 않도록 한다.
- 추가: 제품별 overview injection/명시 조회 안내, SessionLink, 경계별 checkpoint 갱신 연결.
- 수정: 기존 lifecycle adapter·pending events·SessionStart lookup·스킬의 착수/재개 순서.
- 범위: Codex·Claude·OpenCode의 실제 지원 입력/출력·기기 profile, user 선택·실제 결과·변경 receipt. 설치/사용자 설정은 격리해서 시험한다.
- Goal: 프로젝트가 명시되면 제한된 시간 안에 현재 개요 또는 실패/추가 조회 방법을 전달한다. Hook 누락 후에도 현재 실제 상태로 복구한다.
- Non-goal: 전체 transcript 수집, Hook 내부의 긴 Git/모델 분석, 닫힌 앱을 자동으로 깨운다는 보장, Stop을 Done으로 저장.
- 입력: 제품/native event identity·명시 scope/profile, read authority·configured deadline·공통 overview 또는 실제 boundary receipt.
- 출력: 제품에 맞는 제한된 문맥/조회 ref·안전한 오류·원 event 재처리·checkpoint/SessionLink refs.
- 기술: 기존 제품 adapter·CLI/subprocess·native output·structured JSON. 현재 Codex/Claude의 1.5초 timeout 구현을 시작 기준으로 조사하고 timeout/size 정책을 실제 측정으로 확정한다. OpenCode에 같은 event/output 형식을 강제하지 않는다.
- 선행: R4. 제품 fixture 조사/Hook output 대역 준비는 R0 뒤 독립 가능.
- 시험/로그: P4-R5-01~03. product/adapter version·event/request/session·scope·lookup outcome·deadline/size·pending/replay 기록. 사용자 prompt/인증/argv/env 원문 제외.
- 완료/인계: 이벤트 수신·metadata 전달·실제 제품 세션 주입을 다른 계층으로 보고한다. 무응답/미설치면 안전한 명시 조회 fallback·blocked/not_run과 범위를 반환.

| 단계 | 처리와 산출 |
|---|---|
| R5-S1 | 제품의 stable session/event·현재 profile/scope와 출력 형식 적합성 확인 |
| R5-S2 | SessionStart에 metadata 개요만 주입하고 work 선택·실제 분석은 main에 연결 |
| R5-S3 | 사용자 결정·착수 확정·결과 검토·변경 반영 경계에 checkpoint 요청 연결 |
| R5-S4 | 중복/역순/누락 Hook·timeout·profile 변경·민감 입력·새 세션을 확인 |

## R6 — Host·복구·통합·효율·패키징

- 목적/이유: 여러 세션·환경에서 같은 업무 의미로 이어가고 실제 품질/비용과 한계를 확인한다.
- 추가: checkpoint/변경/정렬 저장·현재 조회의 인증된 Host metadata 경계, 실제 재개와 비교 manifest.
- 수정: storage selector·호환/schema 이관·backup/import·미해결 journal 보호·package/usage/skilled workflow 연결.
- 범위: 로컬 Git/파일/client proof와 Host 공유 metadata의 연결, 두 device/env·branch, local/hosted 중단·업데이트/새 session.
- Goal: 현재 source/권한의 동일 의미를 유지하고 user 데이터/원 실행을 보존하며 중복 작업·조회·재작업을 감소시키는지 실제 확인한다.
- Non-goal: Host Git/모델 실행, network-shared SQLite, 실제 user primary 전환, 직접 API·외부/Linux 배포를 fixture로 통과 선언.
- 입력: R0 포트와 R1~R5 구현·현재 코드/정의/조건·승인된 격리 source/제품/모델 경로·기준 실행 manifest.
- 출력: 동작/오류의 local/Host parity·호환/이관/복구 receipts·시험 결과·전체 비용/품질·현재 source hash·패키지/실제품 지원표.
- 기술: 기존 HttpStore/Host allowlist·current auth/Scope/CAS·SQLite backup·immutable resources/journal·설정 mapping, pytest의 실제 별도 프로세스/loopback HTTPS. 새로운 scope/endpoint는 저장 전용 의미를 검토 후 registry에 등록한다.
- 선행: R0 뒤 저장·Host 대역 준비 가능; 최종 통합은 R5 및 실제 연결 구현 완료 뒤.
- 시험/로그: P4-R6-01~03과 관련 기존 회귀. BasisVector·client/Host identity·원 request FP·revision·resource/hash·model/환경/정의·실제 비용과 미상을 기록.
- 완료/인계: 현재 source에서 자체/실제 연계와 fresh AI session 판단을 구별해 확인한다. 실패·skip·not_run·blocked를 유지하고 승인된 local 범위와 native/remote 미수용을 분리한다.

| 단계 | 처리와 산출 |
|---|---|
| R6-S1 | 새로운 저장 metadata만 Host에 노출, client Git/원본/모델 경계·재처리 연결 |
| R6-S2 | 이관/backup·미해결 checkpoint/정렬/게시 refs·권한 폐기·단절 재개 확인 |
| R6-S3 | 같은 목표/조건의 신규·무변화·외부 변화·동시/중단·근거 무효 사례 통합 |
| R6-S4 | 다른 실제 native AI session에 제한된 bundle만 전달해 의미/행동 판단 검증 |
| R6-S5 | 모든 읽기·생성·Host 전송·상세 조회·실패/재검토를 포함한 비용 비교 |
| R6-S6 | 최종 source 고정 뒤 격리 패키지 업데이트·새 세션·반환 smoke·doc/API 상태 확인 |

## 구현 관문과 병렬 배정

| 관문 | 작업과 가능한 병렬 | 다음 조건 |
|---|---|---|
| G0 공통 확정 | 메인 R0, 필요한 저장/측정 준비 | 실제 포트·호환·권한·기준 사례 인계 |
| G1 기반 | A=R1, B=R2, C=제품/Host/시험 대역 준비 | checkpoint/source·actual change·mapping coverage 검증 |
| G2 판단 | B+메인=R3, A=R4 생성기 준비, C=Host metadata 구현 | 실제 근거 적용성과 검토된 반영 receipt |
| G3 재개 | A=R4, 준비된 부분의 C=R5, B=실제 scenario/negative 시험 | 현재 source·권한·내용/예산·행동 조건이 연결 |
| G4 실제 통합 | C=R6/제품·Host, A/B=담당 영향 회귀·품질/측정 | 전체 actual 결과·미해결·package/current hash 인계 |
| G5 완료 판정 | 메인이 전체 계약·21개 시험 ID·현재 증거·scope 확인 | 구현 상태/미실행/효율 한계를 구분한 최종 기록 |

관문 이름 G0~G5는 전체 구현 순서이며, [D0~D5](implementation-interfaces.md)의 producer/consumer 인계 묶음과 연결한다. D0 없이 서로 다른 schema/port를 구현해 나중에 맞추는 작업은 배정하지 않는다.

| 실행 wave | A — 사실/문맥 | B — 변경/반영 | C — 제품/Host/통합 | 종료·착수 조건 |
|---|---|---|---|---|
| W0 | 계약 검토·필요 fixture만 | 계약 검토·mapping fixture만 | 제품 capability·기존 Host 시험 자료 | 메인 R0-S1~S3 실제 계약/기반·D0; S4 기준 준비 |
| W1 | R1-S1→S2→S3→S4 | R2 source/mapping 대역 준비; 실제 R2-S1은 D1 뒤 | R5-S1 조사, R6-S1 저장 포트 대역 준비 | D1 actual basis/checkpoint 인계; 준비 결과는 연결 통과가 아님 |
| W2 | R4-S1 준비, S2/S3는 assessment 대역 사용 | R2-S1→S2/S3→S4, D2 뒤 R3-S1→S2/S3 | R6-S1 실제 metadata·R6-S2 복구/호환 | D2 actual change·mapping, R3 적용성/의미 제안. 판단은 메인 책임 |
| W3 | D3 뒤 R4-S1→S2/S3→S4 actual 통합 | R3-S4→S5, source/effect readback 및 D3 | D4 뒤 R5-S2→S3→S4 실제 연결 | D3 없이 최종 행동 의미 확정 금지; D4 재개 인계 |
| W4 | 담당 회귀·품질 문제 수정 | 담당 회귀·적용성/부분 효과 수정 | R6-S3→S4→S5→S6 및 제품 계층 확인 | D5 actual Local/Host, 실제 새 문맥·실행 tier·비용·패키지 |

R2-S2와 S3는 같은 source/receipt를 읽되 별도 산출물 소유일 때 병렬 가능하다. R3-S2 조건 비교와 S3 선택지 초안은 준비할 수 있으나 최종 방법 판단은 S2 결과 후 확정한다. R4-S1의 metadata 개요는 R3 적용이 없으면 `정렬 미완료`를 명시해 조회할 수 있고, 실제 착수에 필요한 S2/S3는 해당 반영 상태·기존 실행을 재확인한다. 사전 typed graph preview로 영향 분석을 하고, 실제 적용 receipt 뒤 after-state를 확정하므로 적용을 분석의 선행으로 만들지 않는다.

공유 영역은 메인이 직렬화한다. DB schema/migration·공통 DTO/operation registry·Host allowlist/auth·source 판정·package/전체 status가 공유 영역이다. 담당 서비스 구현은 A/B/C가 소유한다. 공유 영역 변경이 필요하면 producer/consumer·변경 이유·호환·부정 시험을 메인에게 인계하고, 확정 계약 뒤 소비자만 병렬로 연결한다.

기본 worker는 사용자 지정 `gpt-6-luna`, 최대 3명과 실제 runner 한도 안이다. 같은 소유 영역을 병렬 편집하지 않는다. 스키마·public dispatcher·Host allowlist·공통 BasisVector·scope 판정은 메인이 조정한다. 논리 계획·대역 통과를 선행 기능의 실제 성공으로 사용하지 않는다.

## Step 배정·반환 규격

배정에는 R/S ID·목적·goal/non-goal·기능 추가/수정/금지 범위·부모/계약 refs·의미 입력/출력·current basis/권한·기술/선택 근거·선행·시험/로그·자율 범위·완료 기준을 넣는다. operational secret·원본 전체·임의 파일 수정 레시피는 넣지 않는다.

작업자는 실제 수정 영역/충돌 가능성을 먼저 알린다. 반환에는 실제 구현 경계/함수·객체 version, 수행 시험·종료 코드·source/환경/증거, 현재 실패/미해결/미지원·복구 지점, 다음 소비자가 사용할 입출력과 확인 조건을 넣는다. 전체 완료·user PMT Done·원본 기준 변경·Git 통합은 메인이 판정한다.

### 배정할 때 고정하는 필드

| 필드 | 지정할 내용 |
|---|---|
| identity/references | R/S ID, 실제 부모 Work/Item/Step, 담당 상세계획·공통 계약의 해당 절·선행 인계 refs |
| purpose/goal/non-goal | 해결할 문제·이유, 관찰 가능한 성공, 하지 않는 기능/범위 |
| scope | 기능 책임으로 추가/수정/삭제/금지 구분; 실제 수정 영역은 작업자가 착수 보고 |
| input/output | 각 값의 의미·type·필수/선택·범위/version·현재성·실패; 예제 operational 값이나 secret 없음 |
| basis/access | 메인이 착수 시 조회한 현재 source/업무 기준 ref와 필요한 권한·claim·지시 version; static 계획 SHA는 권한 아님 |
| method/technology | 기존 서비스/port를 어떻게 연결하며 어떤 사실·원리/증거를 재사용하는지; 자기 범위 안 선택은 기록하고 수행 |
| dependencies | producer의 실제 output·시험·revision을 소비; fixture만 준비 가능한 구간과 실제 착수 조건 구별 |
| verification | 해당 P4 ID의 정상·부정·실패 주입, 실제 Git/DB/files/process/Host/product/model 확인 면과 기대 상태 |
| logging/evidence | 진단·업무 이력·시험 증거, request/event/effect/source/조건·실제 exit·원/후 revision·hash와 민감 제외 |
| finish/handoff | 실제 readback·필수 시험·현재 한계/복구 지점·소비 포트. 적용·업무 Done·Git 통합은 메인 판정 |

### 바로 배정할 첫 작업 묶음

- **A/R1-S1~S2:** 명시 project/task의 현재 사실과 source/업무 basis를 읽기 책임으로 구현한다. D0 계약·현재 scope/WorkAccess·관련 source 선택을 입력으로 받고, 사실 수준·capture refs·coverage/coherence를 출력한다. 원본 변경·checkpoint pointer 전진·실행 권한 승격은 범위 밖이다. P4-R1-01/03의 실제 원본·권한·capture 변화 검증 후 B와 R4에 D1 전반부를 인계한다.
- **B/R2 준비→R2-S1:** D0 아래 명시 mapping·추출 후보·변경 fixture를 준비한다. A의 D1 actual basis가 오면 current WorkAccess로 실제 before/after diff·dirty inventory를 수집한다. 코드 수정·Git checkout/merge·applied 기준 갱신은 범위 밖이다. 실제 coverage/unknown·receipt와 P4-R2-01~03 증거를 R3에 인계한다.
- **C/R5-S1 + R6-S1 준비:** 기존 제품 이벤트/출력과 current Host 저장 허용 면을 조사하고 D0 포트에 대한 제품 fixture·격리 Local/HTTPS fixture를 준비한다. 실제 checkpoint/overview 연결은 A/B 생산물 뒤, storage-only Host는 R0 계약/권한 뒤에 수행한다. 사용자 profile·외부 서비스·primary 전환은 범위 밖이다. 제품별 tier/identity·미지원와 P4-R5-01/R6-01 준비 결과를 메인에 인계한다.

그 뒤 작업은 wave 표의 실제 인계 결과를 받아 같은 규격으로 재배정한다. 계약 조정은 메인이 해결하는 구현 선행이며 사용자에게 매번 재확인할 사항이 아니다. 기존 사용자 위임·공통 규칙을 바꾸거나 허가 범위를 넓혀야 할 경우에만 이유와 영향을 제시한다.

### 반환·중단·최종 수용

작업자는 완료 기준별 구현 결과, 실제 변경 영역·계약 version, 수행한 시험 ID와 tier·exit/source/환경/증거 hash, 원/후 상태·불변 원본, 미해결·blocked/not_run·복구 ref를 반환한다. 실패 시 같은 시험을 무작정 반복하지 않고 실제 원인/조건을 기록하며 관련 기능과 필수 회귀를 다시 확인한다.

현재 범위는 격리된 실제 로컬 Git/SQLite/files·별도 프로세스·loopback HTTPS·허가된 native 서브에이전트까지다. 실제 제품 미설치/권한 부재와 외부/Linux Host는 해당 tier의 미수용으로 남긴다. 21개 상위 ID를 여러 사례로 분할하되 낮은 tier 통과를 높은 tier 성공으로 합산하지 않는다. 이번 문서 작성에서는 코드 동작 시험·실제 PMT 상태 변경·커밋/푸시를 수행하지 않았다.
