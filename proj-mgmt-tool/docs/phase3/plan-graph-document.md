# F1–F4 graph/document 구현 계획

상태: 계획 초안. 이 문서는 기능 명세를 구현 가능한 단계와 인계 조건으로 연결한다. 공개 필드명은 제안이며 현재 API를 설명하지 않는다. 공통 저장 포트·오류·권한·이력의 계획 의미는 [3단계 구현 연결 규격](implementation-interfaces.md)을 따른다. 해당 문서는 논리 계약이며 구현 API/스키마가 아니다.

`logical plan ID`는 이 계획 내 추적용 이름이며 PMT의 UUID가 아니다. 실제 node/relation 식별자는 기존 UUID 정책을 따른다. 코드·스키마 변경은 이 계획의 범위가 아니다.

## 근거와 현재 경계

현재 `src/pmt/planning/graph.py`는 graph schema 1의 입력 검증, UUID/관계/순환·완료 조건 검사, canonical JSON 기반 hash, 결정적 문서 렌더를 제공한다. `src/pmt/planning/service.py`는 draft 저장, plan 조회, publish 흐름을 담당한다. 기존 publish는 `plan.graph.json`, `plan.md`, `AGENTS.md`에 걸친 staging/dirty 검사와 manifest hash 등록을 포함한다. 이 경로는 F4 부분 구간 manifest와 같다고 간주하지 않는다.

`src/pmt/phase2_schema.py`에는 plan metadata가 있고, SQLite 공통 schema와 `src/pmt/db.py`의 짧은 transaction 경계, `src/pmt/util.py`의 canonical JSON/hash helper를 각각 재사용할 기반이 있다. 기존 Git/파일 작업은 reconciliation/resources 경계에 걸쳐 있다. 구체적인 공유 인터페이스와 migration은 현재 문서만으로 확정되지 않았다.

기존 시험은 `tests/test_phase2_planning.py` 중심이며 현행 그래프 검증·draft·게시 동작의 근거다. 3단계 확인은 [verification.md](verification.md)의 예정 ID만 쓴다. F1~F4의 열두 필수 ID는 새 시험 정의가 아니라 기존 명세의 계획된 수용 시나리오다. 현행 통과를 3단계 통과로 승격하지 않는다.

## 의존 순서와 소유 경계

| 계획 ID | 기능 / 선행 | 핵심 산출 의미 | 소비자 |
|---|---|---|---|
| F1-S1..S3 | F1 / F0·기존 validator | 변경 계약, 원본 patch, 재개 가능한 게시 journal | F2, F3 |
| F2-S1..S3 | F2 / F1-S3 완료·CAS/복구 인계 | 동일 dataset 내 두 node-kind tree와 연관 관계 조회, source 고정 역참조 | F3 |
| F3-S1..S3 | F3 / F2-S3 완료·index pin/재구축 확인 | typed field 의미를 반영한 영향 후보와 unknown | F4 |
| F4-S1..S4 | F4 / F3-S3 완료 및 F2-S1 source 조회 | 전체 기준 manifest 이후의 안전한 부분 생성 | F10 통합 |

F1은 데이터 의미와 변경 검증, F2는 읽기 projection/index, F3는 후보와 불확실성 계산, F4는 문서 파일 게시의 주 소유자다. F0/main은 공통 schema·저장 포트·공개 operation·이벤트 registry를 소유한다. F4/F5/F6/F12/F13의 병렬 구조 중 F4는 이 작업 범위만 소유하며 다른 기능 파일·공통 schema를 수정하지 않는다. 공유 계약이 비어 있으면 이름을 확정하기보다 제안과 차이를 메인에게 인계한다.

## F1 — 정형 변경

### F1-S1 — 변경 명령 검증기

목적은 전체 graph 재전송을 요구하지 않는 타입 있는 변경 명령을 기존 graph 규칙에 안전하게 연결하는 것이다. 생성/갱신/관계 연결·해제/폐기를 검증하되 파일 게시나 인덱스 소유권은 맡지 않는다. 선행은 F0의 NodeDelta·SourcePin 의미와 기존 `validate_graph`다.

제안 입력은 `expected_source`(commit, dirty 지문, graph hash/version, 업무 revision)와 `change_set`이다. 입력 누락은 명시 계약대로만 기본값 적용하며 갱신에서 필드 생략은 보존, 명시 `clear`만 비우기로 해석한다. 생성에 한해 검증된 기본값/상속을 계산한다. 출력 제안 `change_preview`는 검증된 대상·필드 의미·관계·예상 변경 집합·기준 지문을 반환하고 원문 graph 복제를 피한다.

알고리즘은 snapshot을 읽고 기대 pin을 비교한 뒤 operation을 정규화하고 임시 참조를 batch 내부에서 해석한다. 모든 참조·권한·관계 종류·순환·중복 stable ID를 검증한 뒤 전체 candidate graph에 기존 validator를 적용한다. 불일치, 알 수 없는 필드/관계, 폐기 노드 재사용은 부분 저장 없이 conflict/invalid로 중단한다. 같은 입력의 재전송 식별은 request 계약에 맡기며 조사 재사용으로 합치지 않는다.

필수 확인은 `P3-F1-01`: 생략/clear/상속 의미와 재조회; `P3-F1-02`: 기대 기준 충돌·동시 변경 및 dirty 보존이다. 로그 제안 `planning.graph_change_validated/rejected`의 request/event ID, 기준 pin, 업무 revision, 대상 ID·operation 유형, 결과/오류를 실제 graph/dirty hash와 대조한다. 원문 필드값은 기록하지 않는다. 완료 조건은 preview가 명시 변경만 나타내고 소비자가 stable target과 기준 pin을 재사용할 수 있는 것이다.

### F1-S2 — 정규 데이터셋 patch 적용

이 단계는 검증된 변경을 Git 구조화 원본에 적용하고 변경 결과를 반환한다. 입력은 S1의 preview, 소유권/점유 증명, 여전히 현재인 SourcePin이다. 출력 제안 `change_result`는 발급·보존 ID, 이전/신규 graph version/hash, 상속 적용 요약, journal 참조를 제공한다.

ID는 안정 UUID로 한 번 발급하고 batch 임시 ID는 검증 완료 후 모든 참조에서 원자적으로 대체한다. graph JSON은 canonical JSON으로 직렬화한다. 검토한 현재 소유자의 dirty 기준은 pin/hash CAS로 보존하며 승인 변경만 적용한다. 미확인 dirty 또는 다른 소유자의 변경은 충돌로 반환하고 덮어쓰지 않는다. 같은 의미 입력에서 무관한 값/순서를 바꾸지 않는다. SQLite 변경과 Git 파일 쓰기를 단일 transaction인 것처럼 다루지 않는다. DB intent/journal과 단계별 old/new hash를 기록하고, 외부 파일 게시 동안 DB transaction을 열어 두지 않는다.

필수 확인은 `P3-F1-03`의 잘못된 참조/중복 ID, 부분 반영 방지와 journal 복구이며 F1-S1의 F1-01/02도 통합 확인한다. `planning.graph_change_published`를 DB revision·event와 Git 원본 hash/내용 및 index source pin과 대조한다. 완료 시 F2가 동일 source를 읽을 수 있고 journal의 상태가 재개/검토 필요를 구분해야 한다.

### F1-S3 — CAS·재개·오류 인계

이 단계는 두 writer의 경합과 파일/DB 사이 중단을 안전하게 종결한다. 입력은 저장 intent, request ID, 대상 범위 소유자, 예상 old hash와 journal 상태다. 출력은 적용 완료, 재개 가능, conflict, 또는 수동 검토 필요 중 하나와 증거 참조다.

게시 직전에 현재 commit/dirty hash와 대상 파일 hash를 재조회한다. 기대 값과 다르면 덮어쓰지 않고 충돌로 끝낸다. journal 단계 전이는 허용된 순서만 따르며 재시작 시 실제 원본 hash로 각 단계의 완료 여부를 판별한다. 보상은 이전 사용자의 편집을 역으로 덮지 않는다. 이미 다른 값이 게시된 경우 자동 롤백 대신 conflict/검토로 중단한다.

필수 ID는 `P3-F1-02`, `P3-F1-03` 재사용이다. 로그의 journal ID·old/new hash·owner·오류 분류를 실제 Git status/diff 및 DB journal/event와 대조한다. F2 착수 조건은 이 단계의 검증된 원본, 일관된 업무 revision, 재개/검토 구분 가능한 journal 인계다.

## F2 — graph 조회·역참조

### F2-S1 — 공통 node 읽기 projection

단일 정규 dataset의 requirement/implementation node를 각각 고정 ID와 tree_kind로 읽고, cross-kind relation은 같은 canonical IDs를 잇는다. 화면별 데이터셋 복제는 두지 않는다. 실제 F2 착수 선행은 F1-S3의 CAS·복구 완료 인계와 graph 관계 의미다. 앞 단계에서 pure projection 설계는 준비할 수 있지만 F1 검증 전에는 F2 완료로 처리할 수 없다. 입력 제안 `graph_query`는 SourcePin, node/relation 선택자, 종류·방향, 제한된 깊이/페이지 cursor를 받는다. 출력 `graph_slice`는 canonical UUID, 경로/관계 종류, 원본 pin, 완전성, 다음 cursor를 담는다.

원본 또는 source pin과 일치가 확인된 SQLite index에서 인접 관계를 순회한다. 허용 순환·금지 순환의 validator 의미를 구분하고 방문 집합·깊이·페이지 상한으로 제한한다. cursor는 source hash/version과 결속한다. stale/손상 인덱스는 빈 결과처럼 보이지 않게 stale/rebuild-needed로 반환한다.

필수 `P3-F2-01`: 두 트리와 연관 관계가 같은 ID·출처 버전을 보존한다. 로그 `planning.graph_slice_read`의 방향·깊이·수·pin을 실제 source row와 대조한다. 완료 조건은 F3가 관계 방향과 누락 범위를 구분할 수 있는 것이다.

### F2-S2 — 역참조 및 의미 있는 범위 조회

이 단계는 변경 대상의 소비자를 찾는 인덱스 조회를 제공한다. 입력은 validated source pin, relation 종류/field scope, cursor다. 출력은 역방향 소비자, 경로, index hash/version, coverage 상태다. 제안 필드명은 F0 인터페이스 확정 전까지 비공개 가정이다.

`depends_on`, `implements`, `evidence`, 부모·정제 관계의 방향별 소비 의미를 적용하고 인덱스 결과를 source 기준과 검증한다. 결과 수와 traversal 범위를 제한한다. 잘린 조회·index hole·동적 관계는 coverage unknown으로 반환한다. 문서 구간 의존 등록/조회 형식은 마련할 수 있지만 F4가 실제 segment manifest를 생성하기 전에는 구간 완전성을 주장하지 않는다.

필수 `P3-F2-02`: 역참조·페이지·순환 제한 및 fixture manifest 기반을 확인한다. 로그의 cursor/결과 수와 실제 역참조 행을 비교한다. F3는 source pin, 경로, 범위 완전성만 소비한다.

### F2-S3 — source 일치·재구축

기존 SQLite 인덱스가 현재 Git source의 파생물인지 판별하고, 아니면 검증 후 재구축한다. 입력 pin과 index source hash/version, 인덱스 무결성 지문을 받고 출력은 usable/stale/rebuild-required와 새 index pin을 반환한다.

원본을 읽어 graph validator를 적용하고 파생 index를 staging한 뒤 한 DB transaction에서 교체한다. 빌드 중 pin이 바뀌면 결과를 폐기하고 재시도 가능한 stale을 반환한다. 외부 Git snapshot은 immutable 기준으로 고정한다. 잘못된 schema는 무리하게 승격하지 않는다.

필수 `P3-F2-03`: 다른 SourcePin·손상 index 거부/재구축을 확인한다. `planning.graph_index_rebuilt/stale`를 원본 commit/dirty hash, index hash, 실제 DB 행과 대조한다. F3 착수 조건은 “결과 없음”과 “coverage 불명”을 구별할 수 있는 것이다.

## F3 — 변경 영향 계산

### F3-S1 — typed field 의미 규칙

목적은 필드 차이를 표시 변경, 계약/대전제, 구현 방법, 근거/시험 정의·환경으로 구분해 후보 생성 입력을 만드는 것이다. 규칙 표·pure classifier는 준비 가능하다. 실제 F3 계산 착수는 F2-S3의 source pin 확인·재구축 완료와 F1 change preview 인계를 기다린다. 입력은 변경 전후 fingerprint, field semantic version, source pin이며 출력은 변화 분류와 규칙 버전이다.

필드별 분류표를 적용하고 알려지지 않은 필드·타입은 영향 없음으로 축소하지 않고 unknown으로 분류한다. 민감 본문을 로그·결과에 싣지 않는다. 같은 pin·fingerprint·규칙 입력은 안정된 결과를 내며 제목/표현만으로 검증을 일괄 무효화하지 않는다.

필수 `P3-F3-01` 중 typed field 경로를 확인한다. 로그 `reconciliation.graph_impact_calculated`의 field 분류/rule version을 입력 fingerprint와 대조한다. 다음 단계는 이 분류 결과를 역방향 소비자 traversal에 전달한다.

### F3-S2 — 역방향 traversal 및 후보 산출

영향 후보를 소비자 방향으로 계산하되 파일 수정 지시나 상태 전이를 만들지 않는다. 실제 후보 계산 입력은 완료된 F2-S3가 확인한 source/index pin, slice/reverse lookup, 변경 분류, 제한값이다. 사전 알고리즘 설계는 완료 인계로 간주하지 않는다. 출력 제안 `impact_set`은 문서/구간/Step/검증 후보, 도달 경로, 원인, known/unknown, rule/source version을 제공한다.

방문 집합으로 중복 경로를 병합하고, 관계 타입별 역방향을 따라가되 unrelated branch를 제외한다. 알려진 관계 단절·미등록 manifest 구간·소유 불명·동적 dependency는 `unknown`으로 남긴다. 범위/깊이 제한 초과, stale pin, 미지원 변경은 partial 후보를 확정 결과로 둔갑시키지 않고 오류/unknown으로 중단한다. 결과는 후보이며 F4/F10 및 검토자가 소비한다.

필수 `P3-F3-01`, `P3-F3-02`를 적용한다. `reconciliation.graph_impact_incomplete`의 후보 수·unknown 이유·관계 경로를 실제 graph와 비교한다. 완료 조건은 무관 가지의 제외 근거와 미확인 범위가 함께 전달되는 것이다.

### F3-S3 — 결정성·미확인 경계

이 단계는 stale·순환 한계·지원되지 않는 변경의 반환을 안정화한다. 동일 원본/rule 입력을 재실행해 결과 fingerprint가 같음을 확인한다. unknown을 빈 배열로 바꾸지 않고 reason code, 제한 지점, 후속 조회/판단 필요를 명시한다.

필수 `P3-F3-03`을 확인한다. source/rule pin, known/unknown 개수와 오류 유형을 DB/source 조회 및 실제 traversal 기록과 대조한다. F4 소비 조건은 후보 구간뿐 아니라 영향 완전성 여부와 unknown reason을 받는 것이다.

## F4 — 부분 문서 생성

### F4-S1 — 렌더 계약과 첫 baseline 준비

기존 전체 렌더를 결정적으로 유지하면서 생성 구간과 수기 구간의 경계를 정의한다. 실제 F4 착수 선행은 F3-S3 완료의 ImpactSet/unknown 경계, F2-S1 source 조회, F1-S3 게시된 원본이다. baseline 절차 설계는 선행 검토 중 준비할 수 있으나 검증 전 렌더/부분 생성 기능 완료로 세지 않는다. 최초 생성에는 기존 segment manifest가 없으므로 입력 영향 범위만으로 축소하지 않고 전체 렌더를 baseline으로 삼는다.

각 generated segment는 안정 segment key, 참조 node/field/relation 의미, template semantic version, 출력 hash를 갖는 manifest를 제안한다. 수기 구간은 별도 소유·경계로 보존한다. F4 첫 전체 render가 실제 manifest의 최초 생산자이며 F2의 manifest index는 그 뒤 등록·조회한다. 따라서 F2→F4 cycle을 만들지 않는다.

필수 `P3-F4-01` baseline 부분집합 확인을 준비한다. 출력 manifest와 문서 hash를 실제 파일/원본 graph·template pin과 대조한다. 완료 시 다음 F3 실행은 등록된 segment 의존을 후보 범위로 사용할 수 있다.

### F4-S2 — 영향 구간만 결정적으로 생성

입력은 F3 impact_set과 completeness, 검증된 manifest, SourcePin, template semantic version, 파일별 예상 hash다. 출력 제안 `render_result`는 바뀐 segment ID/hash, 무관 segment 불변 여부, staging/journal 상태를 제공한다. 먼저 manifest 의존성·template/source pin을 검증하고 필요한 generated span만 만든 뒤 전체 렌더 결과와 해당 span을 비교한다.

세그먼트 순서·직렬화는 deterministic하며 시각값을 넣지 않는다. 부모 상속 필드가 바뀌면 manifest의 소비 segment를 영향 범위에 포함한다. manifest 누락/오래됨은 제한된 부분 렌더 근거가 아니므로 baseline 재작성 또는 unknown으로 중단한다. user-edited generated span은 자동 흡수·덮어쓰기 없이 conflict다.

필수 `P3-F4-01`: 부분/전체 동등성, 무관·수기 영역·고정 ID 보존. 로그 `planning.document_segments_staged`의 template/manifest version·segment hash를 실제 산출 파일과 대조한다. F10은 이 흐름의 연계와 실제 효과를 소비한다.

### F4-S3 — 소유 범위 CAS 및 원자 게시 계획

이 단계는 staging 산출물을 사용자의 현재 파일에 안전하게 반영할 수 있는지 판정한다. 입력에 파일 old hash, user dirty fingerprint, 대상 segment owner·기대 scope, DB manifest revision을 포함한다. 출력은 게시 가능 또는 conflict와 충돌 대상을 반환한다.

게시 직전 파일·Git dirty 상태와 scope owner를 다시 읽고 hash CAS를 수행한다. 사용자 dirty 영역은 보존한다. 다른 작업자가 소유한 게시 영역이 겹치면 게시를 취소한다. 파일 및 DB는 하나의 transaction이 아니므로 intent journal에 요청 ID, source/template pin, old/new hash, 단계와 receipt를 먼저 남긴다. stage 파일은 목적지와 같은 파일시스템의 임시 경로에 쓰고 flush한 뒤 교체한다. DB manifest는 게시 완료 receipt 확인 후 갱신한다.

필수 `P3-F4-02`: 동시 사용자 편집·다른 소유권·dirty 충돌을 시험한다. 파일 hash, Git status/diff, owner binding, DB revision/journal을 직접 대조한다. 완료 기준은 충돌 시 원본 보존과 명시적 조정 인계다.

### F4-S4 — journal 재개와 초기 의존 인덱스 등록

게시 중단을 재개 또는 수동 검토로 정리하고, 성공한 첫 baseline/후속 segment manifest를 F2 조회 기반에 등록한다. 입력은 F4-S3 journal, 실제 대상 hash, manifest/source pin이다. 출력은 recovered/published/conflict와 index pin이다.

재시작 시 journal 단계와 실제 파일 hash를 비교한다. 목적지가 old hash면 게시를 재시도할 수 있고 new hash면 DB receipt를 완성한다. 둘 다 아니면 덮지 않고 conflict로 중단한다. 복구는 임의 rollback으로 사용자 편집을 지우지 않는다. F4가 실 manifest를 만든 뒤 F2 등록 기능을 호출하므로 F2는 manifest 생성에 의존하지 않는다.

필수 `P3-F4-03`: 게시 단계 강제 종료 후 파일/manifest hash 대조, 잘못된 template/manifest의 사전 실패. 로그 `planning.document_segments_published/recovered/conflict`를 journal·파일·DB manifest와 교차 확인한다. 완료/인계 조건은 전체 baseline부터 부분 재생성, 충돌, 복구 경로와 미검증 수기 자료 호환을 메인이 확인하는 것이다.

## 필수 ID 및 관찰 요약

| 기능 | 필수 기존 예정 ID | 직접 관찰 |
|---|---|---|
| F1 | P3-F1-01, -02, -03 | canonical source/hash, IDs·관계, dirty diff, DB revision/event/journal |
| F2 | P3-F2-01, -02, -03 | source pin 대 index version/hash/행, 방향·cursor·coverage |
| F3 | P3-F3-01, -02, -03 | 변경 fingerprint/rule version, 경로·후보·unknown reason |
| F4 | P3-F4-01, -02, -03 | 전체/부분 파일 hash, user diff/owner, manifest·journal 복구 |

이 ID들은 명세와 [시험 인계](verification.md)에 이미 예정된 48개 전체 정의 중 기능별 세 개를 재사용한다. 이 계획은 추가 시험 ID를 만들지 않는다. 실제 실행이 필요한 시점에는 격리 Git/SQLite/파일과 독립 프로세스를 사용하고 command/action·종료 코드·commit/dirty 상태·증거를 남긴다. 로그의 성공 이벤트만으로 통과를 판정하지 않는다.

## 인터페이스 공백과 결정 인계

현재 코드에서 확인한 재사용 기반은 graph validator/renderer, draft/publish service, common canonical JSON/hash, SQLite plan metadata, 기존 journal/atomic-file 사례다. 연결 규격에 logical interface와 NodeRef/SourcePin 의미는 있으나 구현 완료가 아닌 영역은 NodeDelta의 승인된 공개 operation·필드 오류표, SourcePin dirty 지문 산출/재확인, batch 임시 ID의 일회 UUID 치환 수명, graph index의 실제 저장·stale 재구축 응답, typed field semantic registry/version, segment manifest 및 수기 경계의 구체 구현, Git file과 SQLite 간 journal 상태/복구 소유자다.

`implementation-interfaces.md`는 존재하는 계획 규격으로서 NodeRef·SourcePin과 서비스 생산자/소비자 순서를 제안하지만, 실제 API·schema·migration·operation·event registry·DB 구현은 아직 완료 계약으로 간주하지 않는다. 그 문서와 F0에서 승인되기 전 field names, migration strategy, operation/event 이름을 이 계획에서 고정하지 않는다. 남은 메인 결정은 dirty 지문 산출·재확인 규칙, 기능 간 공통 journal 상태 전이 소유, 기존 published 문서의 generated/manual 영역 판별·초기 baseline 전환, 손상 인덱스 재구축 동안 stale 독자를 차단하는 방법이다. SourcePin·NodeRef, segment 의존 필드, 서비스 순서는 연결 규격에 제안돼 있으나 구현/승인은 완료되지 않았다. 미결 상태에서는 구현을 공통 계약 변경으로 넓히지 말고 해당 연결을 보류해 보고한다.

새 dependency는 제안하지 않는다. Python 표준 라이브러리 JSON/hash/path/temp-file 기능, SQLite, 기존 Git 작업 경계, 기존 renderer/index/journal 기반을 우선 재사용한다. 추가 패키지가 필요하다는 근거가 생기면 목적·표준 라이브러리 대안·설치/업데이트 영향을 별도 제안하고 메인 결정을 기다린다.
