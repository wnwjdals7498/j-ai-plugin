# 3단계 공통 계약·기반 명세 — F0

2026-10-02. **구현할 기능 명세**이며 제공 중인 operation·실측 성공을 뜻하지 않는다. [기능/의존 목록](README.md), [구현 방향](01-document-context-efficiency.md), [3단계 전체 범위](../03-hosted-storage.md)를 기준으로 한다. 2단계 형식은 재사용하되 [직접 모델 API 제외](../phase2/scope-decisions.md) 등 최신 결정을 우선한다.

## 명세를 읽는 순서

| 담당 기능 | 명세 |
|---|---|
| F0 공통 계약·저장 경계·최적화 전 기준 | 이 문서 |
| F1~F4 노드/관계·조회/영향·문서 | [데이터·문서 명세](spec-graph-document.md) |
| F5~F9 문맥·재사용·출력·제어·묶음 | [문맥·실행 명세](spec-context-execution.md) |
| F10 로컬 통합·효율 판정 | [로컬 효율 판정](spec-local-measurement.md) |
| F11~F15 Host·설정·이관·복구·전체 통합 | [Host·통합 명세](spec-host-integration.md) |
| 예정 시험·증거·관문·작업 반환 | [시험·인계 명세](verification.md) |

## F0 기능 명세

- **목적·이유:** 모든 작업자가 원본·변경·영향·문맥·저장의 같은 의미를 사용하게 하고, 최적화 전후 비교 기준을 구현 전에 확보한다.
- **추가:** 정형 변경/참조/오류의 공통 계약, 로컬/Host 저장 포트, 버전에 묶인 파생 인덱스·문맥·short ID 계약, 공통 측정/로그 정의.
- **수정:** 기존 저장·CLI·graph·Step·검증/백업 규약을 확장하고 기존 schema 3·graph schema 1 자료와 이관/거부 경계를 정의한다.
- **삭제/축소:** 상태·문서·인덱스의 독립 수정본, 소비자별 envelope/멱등성/상태 판정 중복을 제거한다. 기존 사용자 자료를 삭제하는 기능은 아니다.
- **goal:** F1~F15가 승인된 계약·소유자·필수 시험을 참조하고, 로컬 동작으로 먼저 실현한 기능을 같은 의미의 Host 저장 경계로 연결할 수 있다.
- **non-goal:** 실제 Host 배포, 모델 제공자 API·SDK, 공통 계약을 특정 파일 수정 목록으로 만들기, 기존 시험 결과를 신규 시험의 성공으로 표시하기.

| 방향 | 값의 의미·타입·제약 |
|---|---|
| input 기존 기준 | 사용자 결정·단계 문서·현재 코드/실측 참조의 배열; 확인된 사실/설계 선택/미확인 구별 |
| input 호환 대상 | 기존 protocol·DB/graph schema·core/제품 버전 식별자; 실제 지원 여부는 코드/시험으로 확인 |
| input 비교 작업 | 작업 유형·동일 요구/기준·환경/모델·실행 정책·측정 정의 참조; 운영 비밀값 제외 |
| output 계약 | 계약 버전·공통 타입/전이/오류·권위 원본·소유 경계·이관 정책·시험 정의 |
| output 초기 비교 기준 | 재현 가능한 최적화 전 사례·실행 조건/결과 지문·실측/미상/추정 구분·증거 참조 |
| failure | 규약 충돌·이관 미정·측정 조건 불명은 해당 기능 착수 보류 사유와 필요한 결정으로 반환 |

**구현 방식·기술·원리:** 현재 Python 표준 라이브러리·SQLite·UTF-8 JSON 서비스 경계를 재사용한다. 값 검증·저장 원자성·파일 게시·제품 변환을 분리한다. 로컬/Host는 DB connection 공유가 아닌 업무 operation 단위 포트로 연결하며, Host 웹 의존성은 선택 설치에 둔다.

**선행·소비자·소유:** 기존 2단계 구현·제약·미확인 범위가 선행. 모든 F 기능이 소비한다. 메인은 의미/공개 계약·연결·이관 결정을 소유하며 공유 스키마·공통 fixture의 변경 소유자를 지정한다. 개별 작업자가 동시에 공통 저장 구조를 바꾸지 않는다.

**코드동작 Test:** P3-F0-01 기존 요청·자료의 호환/의도된 거부·원본/파생 구분; P3-F0-02 LocalStore 계약 대역과 실제 로컬 저장의 envelope/충돌/멱등성 결과 일치; P3-F0-03 기준 사례의 환경/요구/모델/측정 지문과 이전 증거·미실행 구분. F0의 Host 대역 통과는 F11/F14의 실제 연결 통과가 아니다.

**로깅:** `contract.accepted/compatibility_rejected`, `measurement.baseline_captured`. 계약/측정 정의 버전·source refs·지원/거부 이유·지문·시험 ID·실행 계층만 기록하고 실제 저장 결과/manifest와 대조한다.

**완료·인계:** 공통 타입/오류·소유·소비 경계와 비교 기준이 확인되고 기능별 착수에 필요한 산출물이 전달된다. **자율:** 내부 코드 배치·값 객체/인덱스 표현. **메인 판단:** 계약 의미·원본/권한·새 의존성·호환성·완료 기준 변경.

## 공통 값·버전·권위

| 값 | 의미·규칙 |
|---|---|
| stable ID | 저장소/프로젝트/Work/Item/Step/node/relation의 UUID. 생성은 Python, 재분류/경로 변경에도 유지 |
| request/event/job/run ID | 같은 호출 재전송 / 원 행동 / 논리 실행 / 시도. 같은 질문의 재사용 키와 별개 |
| SourcePin | repository/project·선택 Git ref·검토 commit·graph revision/hash·필요시 검토한 dirty 지문. 브랜치/환경 혼용 금지 |
| semantic versions | 요구·계획·지시·관계 규칙·템플릿·문맥 정책의 의미 버전. 업무 revision 증가와 구별 |
| NodeDelta | 동작 종류·대상/임시 생성 참조·변경 필드·명시 clear·연결/해제·이유/근거. 생략은 유지, 생성만 기본값/상속 |
| Ref | ID·용도·version/hash·원본 종류·필요 권한. 긴 본문을 반복 복제하지 않음 |
| ContextBundle | 역할/Step·SourcePin·권한/점유·필수/추가 항목·예산·누락/후속 cursor·묶음/매핑 버전 |
| AliasMap | 짧은 ID→고정 UUID의 묶음별 매핑. 다른 project/버전/권한에서 사용하면 거부·재조회 |
| ReuseKey | 조사/검증 정의·목표/질문 의미·입력·환경/도구/의존/기준 지문·정의 버전. 유사한 문장이 곧 같은 작업은 아님 |
| FeatureResult | goal 기준별 pass/fail/blocked/not_run·산출물/실측·근거·적용성·미해결·다음 행동. 도구 성공과 업무 완료 구별 |

Git의 구조화 데이터셋은 정형 프로젝트 내용의 원본이다. 생성 문서·graph 화면·역참조/구간/문맥 인덱스는 파생이고 버전/hash로 재생성 가능하다. 수기 자료·기존 문서는 소유 구간을 유지하며 조정한 내용만 정형 원본으로 확정한다.

local 모드의 실행 상태·점유·Queue는 로컬 SQLite가, hosted 모드의 공유 상태·점유는 Host SQLite가 권위 원본이다. 클라이언트 보류함·journal은 미반영 결과/파일 작업용이며 새 업무 원본이 아니다. private Step 지시와 증거는 기존 내부 리소스 규칙을 따른다.

## 호출·저장·원자성

- CLI는 기존 UTF-8 protocol-v1 envelope·응답/경고·입력 크기·종료 코드 의미를 보존한다. 새 정보는 버전형 payload/리소스 참조로 정의하고 구버전 필드 검사를 우회하지 않는다.
- LocalStore/HttpStore의 공통 포트는 `execute(operation, request) → response/exit 의미`, 결과 조회·호환 확인이다. 업무 계약을 보존하며 원격 SQL·DB 파일 공유·클라이언트 독자 Done 판정을 제공하지 않는다. 공개 operation별 필드/이관은 F0 구현 착수 시 공유 산출물로 고정한다.
- 원격 포트에는 공유 저장·상태·점유·결과·허용된 자료 조회만 노출한다. Git/working tree 읽기·편집·문서 게시·명령/모델 실행은 클라이언트에 남기고 Host operation 허용 목록에서 제외한다. 현재 dispatcher 전체를 그대로 HTTP로 노출하지 않는다.
- 검증의 대상/환경/명령 지문은 실제 실행한 클라이언트가 전후에 수집해 증거와 제출한다. Host는 권한·출처·현재 기준·저장된 근거/실패·hash/참조를 검증한다. Host의 로컬 runtime/경로로 클라이언트 지문을 대체하거나 실행하지 않은 시험 결과를 만들지 않는다.
- 변경·실행을 위한 실제 상세 읽기/쓰기는 원자적 점유 성공 후 수행한다. 상태/ID 메타 조회와 immutable 기준 조회를 구별하고, 다른 소유자의 변경 중 내용을 실행 기준으로 삼지 않는다.
- 같은 DB 안의 상태·revision·업무 이벤트·재처리 결과는 함께 commit/rollback한다. DB·Git/파일·Host 효과는 단계별 intent/journal/receipt로 연결하며 외부 작업 동안 DB transaction을 유지하지 않는다.
- 예상 원본/문서 hash와 현재 값이 다르면 충돌로 반환한다. 부분 생성/게시·서버 반영의 중간 장애는 원 요청·이전/다음 hash·소유 범위로 완료/복구/재검토를 구별한다.
- 모든 노드/관계 batch가 유효할 때 변경을 게시한다. 부분 실패를 전체 성공으로 표현하지 않는다. 의도된 개별 Step 결과의 부분 실패와 데이터 변경의 원자 적용은 구별한다.

## 상태·실행 규칙

기존 Work/Item/Step 업무 상태와 run의 queued/starting/running/review_pending/succeeded·blocked/failed/cancel_requested/canceled/reconciling을 보존한다. 캐시·요약·영향 후보·묶음 준비 상태는 실행/업무 상태와 별개다.

- 문맥 부족/기준 불명은 상세 조회 또는 판단 요청, stale 지시/점유 충돌은 재검토/대기다. 누락을 숨긴 성공 응답을 하지 않는다.
- Python의 자동 재시도는 허용된 transient·확인된 미시작/종료 조건에만 적용한다. 기본 최대 2회, 동시 실행은 3과 실행기 한도 중 작은 값이다. 관찰 시간 초과는 종료·점유 해제 근거가 아니다.
- 동시성은 물리 실행/handle 기준이다. F9가 한 handle에 묶은 논리 child run은 Step별 상태·증거·점유 binding을 유지하되 각각 새 실행 슬롯으로 중복 계산하지 않는다.
- native 도구 호출은 실제 메인 세션이 수행한다. Python이 intent를 만들었다고 호출·종료를 기록하지 않는다. 현재 실제 제품의 자동 polling/UI/callback 지원 범위를 확인한다.
- 조사 재사용 조회와 새 점유는 함께 조정하며 실제 검증 범위/근거 유효성을 확인한다. 취소/변경 후 늦은 결과는 사실로 보존하고 현재 계획을 완료시키지 않는다.
- AI 제어를 줄여도 메인 판단이 필요한 구조/목표 변경·검토는 유지한다. 사용자 위임 범위의 방법 선택은 자율 처리한다.

## 오류·로그·호환

오류 범주: input_invalid, version/ownership_conflict, context_incomplete, evidence_not_applicable, unsupported, transient_io, execution_unknown, internal_error. HTTP 코드는 F11의 wire 계약에서 정의하고, 기존 CLI 의미로 변환한다. `retryable`은 근거로 지정한다.

공통 로그는 UTC·event·request/correlation·범위/Step/job/run·계약/원본/묶음/매핑 버전·상태/결과/이유·건수/byte/시간·증거 참조다. 업무 이력·진단·실측 증거를 분리한다. 필수 이력 실패는 rollback, 진단 실패는 경고/fallback, 필수 증거 누락은 완료 거부다.

전체 문맥·원문 대화·환경/인증 값·점유 token을 로그에 복제하지 않는다. ID·source refs·정제한 오류/사용 가능한 추가 조회로 반환한다. 시간·비용 추정과 실제 값, 모델 주장과 실측, 기존/신규·fixture/실제품을 구별한다.

이관 대상과 새 schema/graph 계약 버전은 실제 설계/시험으로 확정한다. 기존 자료를 현재 노드 모델로 읽는 호환 경계와 파생 인덱스 재구축 경로를 제공하며, 지원 밖 자료·미확인 제품은 명확히 거부/차단한다.
