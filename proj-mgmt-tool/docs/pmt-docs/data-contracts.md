# 데이터·문서 계약

## 원본과 저장 위치

| 내용 | 원본 | 다른 곳에 남길 값 |
|---|---|---|
| 프로젝트 대전제·구조·현재 결정·두 트리 graph | Git의 프로젝트 문서 | 기준 commit·문서/노드 ID·버전·hash |
| 상태·이력·점유·Queue·실행 시도 | 선택한 local/Host의 단일 SQLite | 사용자에게 필요한 요약·추적 ID |
| 하위 모델 Step 지시 원문 | PMT 내부 리소스 | Step ID·지시 버전/참조·hash·결과/증거 참조 |
| 테스트 로그·이미지 등 근거 | PMT 리소스 + manifest | 검증 ID·대상/환경 지문·참조 |
| 에이전트·모델·연결 설정 | 로컬 설정 | 비밀을 제외한 실제 실행 식별 정보 |

Step 지시 원문은 프로젝트 관리툴 화면·외부 동기화 대상에서 제외한다. runner가 실행에 필요한 범위만 읽어 전달한다. SQLite나 외부 도구에 Git 문서/graph의 독립 수정본을 만들지 않는다. 인증 비밀은 문서·graph·일반 리소스·로그에 넣지 않는다.

## 계층·태그

`Project → 분류 → Work → Item(재귀) → Step → 실행 시도(run)`를 사용한다. 분류는 프로젝트에 맞는 기능·URL·모듈 기준으로 정한다. run은 업무 트리의 추가 요구 노드가 아니라 Step을 실행한 기록이다.

| 단위 | 의미 | 기본 종류 태그 |
|---|---|---|
| Work | 사용자에게 전달할 작업 목표 | feature / bugfix / improvement / ops / research |
| Item | 분해된 요구·제약 | functional / quality / constraint / compatibility |
| Step | 방법과 경계가 정해진 실행 단위 | investigate / experiment / implement / test / review / docs / integrate |

상태·우선순위·owner·모델·의존성·revision·claim은 태그와 별도 필드다. 우선순위는 상속하며 예외에 이유를 남긴다. 제품 범위 `prototype / expansion / production`은 PMT 개발 1/2/3단계와 구별한다. ID는 이름·분류·경로 변경에도 유지한다.

## 두 트리와 graph — 2단계 계약

1. 자연어 트리: 최초 요구 → 분리·대전제 → 부족한 부분 확인 → prototype·확장·production 범위. 요약은 한 줄 50자 이내이며 원문·근거를 참조한다.
2. 구현 트리: 가용 자원 → 프레임워크·기능 배치 → 제품 작업 단계 → 전체 구조 → 기능 명세 → 세부 방법. 요구 노드와 구현 노드를 다대다로 연결한다.
3. 사용자가 이후 결정을 위임한 노드는 자율 범위를 남기고 분해를 종료한다. 자연어 트리는 다음 분해가 구현 방법이면, 구현 트리는 다음 분해가 직접 파일 수정 지시이면 종료한다.
4. 두 트리 종료 후 프로젝트의 대전제·구현 방식을 문서화하고, 해당 프로젝트 `docs/pmt-docs/` 안에 UTF-8 JSON graph를 저장한다. 작성 중인 초안은 확정본과 구분한다.

AI가 데이터셋의 세부 표현을 선택하되 다음 의미를 보존한다. 저장 형식의 공개 버전은 구현 시 확정하고 validator·migration과 함께 관리한다. 아래 필드 의미는 현재 구현된 API가 아니다.

| 그룹 | 필수 의미 |
|---|---|
| dataset | schema_version, project_id, graph_version, 문서 기준 참조 |
| node | 고정 ID, tree_kind, node_kind, summary, 대전제/방법, 제품 범위, 종료 이유·자율 범위, Work/Item/Step 참조 |
| relation | 관계 ID, 출발/도착 ID, parent/refines/implements/depends_on/evidence 종류 |
| provenance | 사용자 결정·문서·근거 참조, 작성 이유·변경 버전 |

중복 ID·누락 참조·부모/실행 의존 순환·필수 종료 정보 누락을 거부한다. 모든 관계를 한꺼번에 비순환으로 강제하지 않는다. graph 파일 안에 자신의 최종 Git commit을 포함시켜 무한 갱신하지 않으며, 적용한 commit은 SQLite 기준 정보에 기록한다.

대전제 변경 시 영향 가지를 활성 계획에서 제외하고 영향 Step 중지·재계획·검증 재사용 무효화를 처리한다. 폐기 이유·근거·이전 버전은 보존한다. 다른 가지와 유효 증거까지 일괄 삭제하지 않는다.

## Git 최신화

- **진행 중:** 선택한 작업 기준 브랜치에 미확인 commit이 있을 때만 분석한다.
- **Planned/backlog/wait 등 비진행:** 시작 직전 최신화한다. 같은 확인 commit은 반복 분석하지 않는다.
- 점유 획득 → 기준 commit 이후 변경 파악 → 관련 문서·graph·Step·근거 영향 반영 → 검토한 기준 저장 → 실행 순서다. 단순 SHA 교체로 끝내지 않는다.
- 이력 단절·브랜치 변경도 감지해 비교 기준을 재설정한다. 미커밋 변경은 별도 지문과 owner로 관리하며 Git 우선 원칙으로 덮어쓰지 않는다.
- 활성 작업 소유자와 충돌하는 변경은 조정 후 반영한다. 관련 기준이 바뀌면 기존 지시 버전으로 계속 실행하지 않는다.
- Git 없는 프로젝트는 그 사실과 문서/대상 지문을 기록한다.

업무 `revision`, 요구 버전, 계획 버전, Step 지시 버전을 구분한다. 점유·상태만 바뀌었다고 같은 계획의 검증을 모두 무효화하지 않는다. 1단계 schema 2에 Step·Queue·범위 lock을 추가할 때는 이관·복원·구버전 요청 시험이 필요하다.

## 현재 3단계 데이터 계약

현재 코드는 package `0.4.0`, SQLite schema 5를 사용한다. schema 0–4와 기존 2·3단계 의미를 보존하며 schema 3→4, 4→5 이관 때 백업·자료 보존을 검증한다. `LocalStore`/`HttpStore.execute(request)`는 `(envelope, exit_code)`를 반환한다. 결과 조회는 actor/session·현재 권한과 선택적 기대 요청 지문을 확인하며 미존재는 `None`이다. `check_compatibility()`는 core·DB·graph·protocol을 대조한다. Host auth schema와 HTTP API version은 별개다.

Phase 3 SQLite에는 graph/document 원문 복제본 대신 stable ID, SourcePin, revision, hash, 상태, manifest·이력·intent·outbox 같은 파생 메타데이터와 실행 상태를 둔다. Git 문서와 graph가 업무 원본이다. `request_id` 재전송은 같은 논리 요청만 재생하고, 새 행동은 새 요청·이벤트 식별자를 사용한다. 변경은 기대 revision/source hash에 대한 CAS로 적용하며, 불일치는 성공처럼 합치지 않고 conflict로 돌려준다.

SourcePin은 project/repository/workspace와 원본 지문을 묶는다. Git source는 commit/ref와 dirty 상태를 구별하고, 실제 Git이 없다고 확인된 경우에만 non-Git pin을 쓴다. 검사 실패를 non-Git으로 바꾸지 않는다. graph index와 문서 manifest는 캡처한 source hash/version에 고정된다. 현재 작업에서 소비하려면 실제 source와 권한·claim을 다시 확인한다. 미등록·불완전한 coverage는 “영향 없음”을 뜻하지 않으며 unknown/검토로 남긴다.

문서 기준선은 결정적 segment ID·template/dependency 버전·의존 node/field/relation·generated/manual 표시·hash manifest로 추적한다. 부분 갱신은 검증된 F3 영향과 적용 receipt가 같은 change ID, before/after pin 및 예상 graph hash/revision을 가리킬 때만 허용한다. 수기 구간과 영향 없는 segment는 보존하고, 손으로 변경된 generated 구간이나 불완전한 manifest는 덮어쓰지 않고 conflict/unknown으로 처리한다. 파일 게시와 복구는 공통 guarded publication을 사용해 이전본·후보를 보존하고 현재 실제 hash를 대조한다.

구조화 JSON의 Git clean 여부는 canonical graph 내용과 실제 Git status를 대조하므로 clean LF/CRLF checkout은 같은 SourcePin을 가진다. dirty 상태는 실제 working bytes/status 지문을 보존한다. 관리 Markdown은 UTF-8/LF로 생성하고 CRLF 입력도 해석한다. 준비 전에 사용자가 바꾼 수기 내용은 반영·보존하며 준비 후 변경은 raw target hash CAS로 거절한다. 생성 구간의 실제 내용 변경은 계속 conflict다.

F5 문맥은 권한과 현재 run/source를 재검증한 bounded projection 참조다. 필수 항목 누락·불확실성은 숨기지 않는다. F6 재사용은 소유·scope·source 및 evidence 참조가 맞는 경우만 재사용 가능성을 돌려준다. F7은 결과 리소스의 실제 hash와 제한된 상세를 제공하고, 기준 충족 여부를 모델의 성공 주장으로 판정하지 않는다. 문맥 예산은 bytes/lines로 제한하며 token 수·비용 절감은 측정되지 않았다.

Host의 graph/검증 snapshot은 인증된 클라이언트가 제출한 immutable hash 리소스와 현재 pointer다. Git 원본의 독립 수정본이 아니며 `provenance=client_snapshot`, `host_git_verified=false`를 구별한다. 실제 checkout은 클라이언트에서 검사한다. private Step/context는 현재 run·지시 버전·source·scope·기기/세션 소유권으로 제한한다.

Host 논리 workspace는 repository UUID와 branch key SHA-256으로 만든다. 클라이언트 절대 경로는 profile에만 보관한다. backup은 업무 ID·관계·증거를 보존하고 기기 인증·모델 설정·claim/handle·live replay·derived context/cache를 제외한다. 새 target은 별도 인증/namespace를 유지하고 이관된 검증은 current pass로 재사용하지 않는다. pending은 실제 생성된 종료 결과만 원 request/body/source/owner와 함께 보관하며 동일 요청 조회·현재 기준 확인 후 재조정한다.

## 4단계 세션 연속성 데이터

`continuity_objects`는 짧은 사실·basis·checkpoint·변경·정렬·재개 참조를 immutable JSON/hash로 저장한다. `continuity_pointers`는 선택한 project/repository/branch/workspace/task/environment의 확정 객체를 CAS로 가리킨다. `continuity_events`는 실제 사건과 객체를 연결하고 `continuity_journal`은 아직 확정되지 않은 효과를 보존한다. shared는 안전한 요약·ref/hash, private bundle/detail은 현재 actor/session의 점유·기준 검증을 거친 조회다.

basis는 실제 DB revision과 source·계약·조건·수집 범위를 함께 기록한다. graph SourcePin만으로 전체 코드가 확인됐다고 보지 않는다. 파일 inventory/diff 상세는 client-local private 자료이며 공유하는 값은 opaque ref·hash·개수·coverage다. 변경의 `before_basis_ref`는 출발점, `after_basis_ref/hash`는 현재 관찰 기준이다. mapping·assessment는 after 기준과 일치해야 한다.

계획 단계의 checkpoint는 실행 없이 만들 수 있다. source/환경을 확인하지 않았다면 incomplete/unknown을 유지한다. session link·checkpoint·summary는 점유 이전이나 실행 완료를 만들지 않는다. 현재 사실·변경/정렬·적용성·F5 상세를 연결해 다음 조회/검토를 제안하며, 오래된 ref를 새 실행 권한으로 사용하지 않는다. 실제 operation과 Host 경계는 [연결 계약](../phase4/runtime-contract.md), 확인 수준은 [4단계 상태](../phase4/implementation-status.md)를 따른다.
