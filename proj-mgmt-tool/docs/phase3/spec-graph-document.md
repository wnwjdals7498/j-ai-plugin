# F1–F4 정형 graph·영향·부분 생성 기능 명세

상태: 예정 명세. 구현 존재·시험 수행·통과를 의미하지 않는다.
기준: [3단계 순서](README.md), [문서·문맥 효율화 계약](01-document-context-efficiency.md), [저장 계획](../03-hosted-storage.md).
공통 원본·버전·ID·변경·오류·저장 원자성·로그 규칙은 [F0 공통 계약](contracts.md)을 따른다. 아래는 F1–F4의 기능별 의미와 추가 제약이다.

## 공통 불변식

- F1–F4는 Git 정규 dataset과 SourcePin, 업무 revision을 분리한 F0 공통 값 계약을 소비한다.
- SQLite 조회 인덱스는 Git 원본의 파생물이며 source hash/version이 다르면 최신 조회 결과로 사용하지 않는다.
- request 재전송 dedup과 동일 질문·조사의 중복 제거는 다른 계약이다. 후자는 F6 소유다.
- 별도 데이터 원자성·게시 계약은 F0를 따른다. 각 기능은 기능 고유의 staging·journal·복구 근거를 정의한다.

## F1 — 정형 노드·관계 변경


**목적·이유.** 전체 graph 재제출 없이 의미 있는 변경분만 저장해 고정 ID와 기존 연결을 보존한다.

**범위.** 추가: 생성, 필드 갱신, 관계 연결·해제, 명시 폐기. 수정: planning graph 검증·저장 경계가 변경분과 기대 기준을 처리한다. 삭제: 누락 필드를 삭제로 해석하거나 전체 교체를 기본으로 하는 입력.

**Goal.** 검증된 변경만 정규 Git 데이터셋에 반영하고 각 성공 결과를 새 graph 버전·변경 ID와 조회 가능하게 연결한다.

**Non-goal.** 새 graph DB, 의미 타당성의 자동 추론, 파일 편집 레시피, Host API 구현, F5 alias 발급.

**입력.** `change_set`: 타입 있는 operation 배열(create/update/relate/unrelate/deprecate), 각 대상 ID·종류·선택 필드·관계 종류·근거 참조를 가진다. 식별자는 dataset/project 범위에 한정한다. create ID는 시스템 발급 또는 승인된 고정 ID이며 중복 금지.

**입력 제약·실패.** `expected_source`: 기준 commit, dirty 지문, graph version/hash와 별도 업무 revision. 불일치·오래된 dirty 상태는 conflict로 거부하고 최신 조회를 요구한다. 누락 참조·중복 ID·잘못된 종류/필드·불허 관계·권한/점유 부족·폐기 대상 재사용은 부분 반영 없이 오류로 반환한다.

**출력.** `change_result`: 성공한 변경 식별자, 보존된/발급된 node·relation ID, 이전/새 graph version·dataset hash, 적용한 기본값/상속 요약, journal 상태를 반환한다. 원문 전체를 복제하지 않는다.

**방법·원리.** Python planning 책임이 F0 NodeDelta를 검증해 관계 의미·참조와 허용된 순환 규칙을 적용한다. batch 내부 임시 ID를 참조할 수 있고, 검증이 끝나면 안정 UUID를 발급해 참조 전체를 한 번에 대체한다. 단일/복수 node 변경 모두 전체 graph를 재생성하지 않는다. staging·게시·파생 인덱스 갱신은 F0 저장 경계를 따른다.

**경계·선행.** F0 저장/버전 기준 및 2단계 graph validator 선행. planning은 데이터 의미와 Git 변경, db/queries는 revision·인덱스 참조, lifecycle은 점유·이력에 한정한다. 제품 연결부는 변환만 한다.

**시험.** `P3-F1-01`: create/inherit와 update 생략·clear가 고정 ID·기존 값을 구분해 저장되고 재조회된다. `P3-F1-02`: 기대 기준 충돌·동시 변경에서 데이터 일부 반영 없이 conflict가 나며 dirty 변경이 보존된다. `P3-F1-03`: 잘못된 참조/중복 ID 및 게시 중단을 검증하고 오류·journal 복구 후 원본과 파생 인덱스가 일치한다.

**로그.** `planning.graph_change_validated`, `planning.graph_change_published`, `planning.graph_change_rejected`; 추적: event_id, request_id, project/dataset, actor, op 종류·대상 ID, 기준 commit·dirty hash, 업무 revision, graph version/hash, 결과·오류 분류, journal ID. 필드 값·원문·비밀은 제외.

**완료·인계.** 위 시험 증거와 원본/인덱스 일치, 충돌·복구 동작을 메인이 확인해야 완료다. 작업자는 공개 필드/오류/이관 제안과 실행 증거·dirty 상태를 인계한다. 필드 생략 의미·검증 규칙의 국소 선택은 자율. 스키마 호환성·공유 revision/저장 경계 변경은 메인 판단이다.

## F2 — graph 조회·역참조


**목적·이유.** 같은 dataset의 두 트리와 연관 graph를 필요한 범위로 조회하고 변경 영향 계산을 위한 역방향 참조를 제공한다. 부분 생성의 실제 segment manifest 생산은 F4 소유다.

**범위.** 추가: 노드·관계·경로 조회, 정방향/역방향 참조, 문서 구간 의존 참조, 인덱스 상태와 재구축. 수정: queries/context 경계가 source version이 확인된 선택 조회를 제공한다. 삭제: SQLite를 graph 원본으로 취급하거나 오래된 인덱스를 최신처럼 반환하는 동작.

**Goal.** 조회 결과가 Git source의 동일 버전과 일치하고, 역참조로 연결 소비자를 빠짐없이 찾거나 미확인 사유를 드러낸다.

**Non-goal.** 의미 영향 판정(F3), 짧은 alias 발급(F5), 별도 graph 서비스/DB, 전체 원문 상시 반환.

**입력.** `graph_query`: project/dataset 참조, F0 SourcePin, node/relation UUID 또는 종류·방향·depth·페이지 cursor·요청 범위를 받는다. AliasMap이 입력된 경우 F5 소유 매핑을 우선 검증한다. AliasMap 없는 canonical UUID 조회는 이 범위에서 독립 완료 가능하다.

**입력 제약·실패.** depth/page는 양의 제한된 정수, 관계 종류는 허용 목록, cursor는 동일 source 버전에 종속된다. 버전 불일치·alias 만료/다른 프로젝트·누락 인덱스·순환 제한·잘못된 cursor는 오류 또는 명시적 재구축 필요로 반환하며 빈 결과로 위장하지 않는다.

**출력.** `graph_slice`: 요청 노드/관계, 방향·경로와 출처, 페이지 cursor, SourcePin·index version/hash 및 완전성 상태를 가진다. 구간 의존 조회는 F0가 정한 index 형식과 등록/조회 기반까지만 제공한다. F4 manifest가 아직 없는 구간은 absent/unknown으로 명시한다. 원문은 요청한 필드만 반환한다.

**방법·원리.** planning의 graph 의미를 기준으로 queries가 인접 목록과 역참조를 제공한다. 초기 F2는 F0 구간 의존 형식의 등록/조회 기능을 갖추되 실제 generated segment manifest는 만들지 않는다. SQLite 인덱스는 source hash/version에 묶고 stale이면 차단 후 Git dataset에서 재구축한다. 허용/금지 순환을 구별하고 방문 집합으로 탐색을 종료한다.

**경계·선행.** F1 정형 원본·관계 의미 선행. planning은 validator/관계 의미, queries는 읽기·역참조, reconciliation은 Git 기준 감지 소유. F3는 이 결과를 소비한다.

**시험.** `P3-F2-01`: 두 트리·연관 관계의 양방향 조회가 단일 원본 ID와 출처 버전을 유지한다. `P3-F2-02`: 역참조·페이지·순환 경계와 독립 fixture manifest의 등록/조회 계약을 검증하고, 실제 F4 manifest 연계는 F10 통합에서 확인한다. `P3-F2-03`: 다른 SourcePin·손상 인덱스·입력된 만료 AliasMap을 거부 또는 재구축 필요로 반환한다. AliasMap 자체는 선택 입력이며 F5가 소비를 구현한다.

**로그.** `planning.graph_slice_read`, `planning.graph_index_rebuilt`, `planning.graph_query_stale`; 추적: event/request ID, scope/project, source commit·dirty hash/version, index hash, query kind·방향·깊이·결과 수, cursor 상태, 결과·오류 분류. 본문 내용 제외.

**완료·인계.** 정상·경계·stale 재구축 증거와 F3에 필요한 반환 의미를 메인이 확인한다. 인계 시 query 의미·성능 측정 조건·미해결 index 호환성을 보고한다. 조회 투영은 자율, ID/관계 의미 변경은 메인 판단이다.

## F3 — 변경 영향 계산


**목적·이유.** 변경 필드와 관계 방향에 따라 다시 검토할 문서 구간·Step·검증 대상을 계산해 무관한 가지의 재작업을 줄인다. F4 segment manifest가 없는 초기 상태의 공백도 드러낸다.

**범위.** 추가: 영향 후보와 이유·확정도·미확인 경계 산출. 수정: reconciliation 영향 분석이 graph 역참조·의미 필드 규칙을 사용한다. 삭제: 관계 단절/미지원 유형을 영향 없음으로 간주하거나 모든 성공 검증을 일괄 무효화하는 처리.

**Goal.** 영향 집합은 재현 가능하고 출처 버전에 고정되며, 모호하거나 누락된 연결은 `unknown`으로 보존한다.

**Non-goal.** 모델을 대신한 의미 판단, 실행 중지·완료 상태 자동 전이, 무관한 결과 삭제, 파일별 수정 지시 생성.

**입력.** `impact_request`: 변경 ID와 node/field/relation 변경 종류, 이전·새 값의 안전한 fingerprint, F1 source 기준, F2 graph slice/index 기준, 판정 규칙 버전을 받는다. 값 원문은 꼭 필요한 비교에만 내부 사용한다.

**입력 제약·실패.** 방향과 대상이 확인된 관계만 경로로 사용한다. 누락·동적 의존·소유 불명·stale 기준은 unknown으로 포함한다. 변경/관계 종류 미지원, source 불일치, 순환 제한 초과는 부분 후보를 확정 결과로 가장하지 않고 오류·불확실 상태를 반환한다.

**출력.** `impact_set`: 문서·구간·Step·검증 ID 후보, 도달 경로/변경 원인, 의미 필드 분류, certainty(known/unknown), 검토 필요 이유, source·rule version을 제공한다. F4 manifest 부재 또는 미등록 구간은 unknown/보수적 검토 대상으로 포함한다. 완전성 증거가 없으면 empty나 all-covered로 판정할 수 없다.

**방법·원리.** 관계 방향에 따라 소비자 방향으로 역탐색한다. 관계 기반 영향과 field semantic 규칙을 적용하며, 제목/표현 변경은 확인된 표시 구간에 한정한다. 대전제·계약·방법·근거/시험 환경을 구분하고 중복 경로를 합친다. manifest가 없으면 알 수 없는 문서 소비자를 unknown으로 남겨 전체 영향 커버리지를 주장하지 않는다. 의미가 불확실하면 AI 판단 대상으로 남긴다.

**경계·선행.** F2 slice/reverse lookup 및 F1 변경 종류 선행. reconciliation은 후보 계산·근거, verification은 후보가 된 증거의 적용성 재평가, planning은 규칙 의미를 소유한다. 결과를 상태 변경 명령으로 해석하지 않는다.

**시험.** `P3-F3-01`: 소비자 역방향 탐색과 field semantic 규칙이 각기 다른 영향 경로·검토 후보를 낸다. `P3-F3-02`: 무관 가지는 제외하고 누락/동적/소유 불명 관계 및 F4 manifest 미등록 구간은 empty가 아닌 unknown으로 남긴다. `P3-F3-03`: 같은 source/rule 입력은 동일 결과를 내며 stale graph·순환 제한·지원하지 않는 변경은 확정 결과 없이 식별 가능한 실패로 반환한다.

**로그.** `reconciliation.graph_impact_calculated`, `reconciliation.graph_impact_incomplete`, `reconciliation.graph_impact_rejected`; 추적: event/request ID, change ID, source commit·dirty hash·graph/index/rule version, 탐색 방향·관계 종류, known/unknown 개수, 후보 ID, 이유 코드, 오류 분류. 값 원문은 제외.

**완료·인계.** 방향·의미·unknown 사례의 시험 증거와 F4 소비 계약을 메인이 확인한다. 영향 규칙 변경안과 재검토 필요 범위를 인계한다. 확정된 규칙의 후보 계산은 자율, 사용자 대전제와 공통 무효화 경계는 메인 판단이다.

## F4 — 부분 문서 생성


**목적·이유.** F3에서 영향받은 구간만 재생성해 무관 문서와 수기 영역을 보존하면서 전체 생성과 같은 결과를 유지한다.

**범위.** 추가: 최초 전체 렌더와 구간별 의존 manifest 등록, 이후 결정적 부분 렌더·staging·게시·복구. 수정: planning 문서 생성 경계가 변경 구간과 Git 충돌을 관리한다. 삭제: 전체 문서 덮어쓰기, 수기 변경 자동 흡수/삭제, 무관 구간의 시각·순서 변화.

**Goal.** 같은 dataset/template 입력은 전체 생성과 동등한 구간 내용을 만들고, 직접 편집 충돌·부분 게시 실패를 보존·복구할 수 있다.

**Non-goal.** AI의 의미 작성 자동화, 전체 문서 서식 개편, Git commit 수행, 수기 문서를 구조 원본으로 자동 역변환.

**입력.** `render_request`: F0 SourcePin, F3 영향 구간 및 완전성, template version, 기존 segment manifest(있을 때), 대상 문서의 생성/수기 경계와 기대 hash를 받는다. 첫 실행은 전체 렌더로 baseline manifest를 등록한다. 그 전까지 F3가 알 수 없는 구간은 임의로 좁히지 않고 전체 baseline 생성 대상으로 보수 처리한다.

**입력 제약·실패.** manifest는 node/field/relation 의존과 template version을 포함한다. renderer·템플릿 불일치, 누락 의존, 원본 변화, 기대 hash 불일치, 사용자 편집과 generated span 충돌은 게시하지 않고 conflict/stale로 반환한다. 다른 게시 영역 소유권은 변경 요청에서 확인한다.

**출력.** `render_result`: 변경 segment ID·이전/신규 hash·source/template version, staging/journal 및 게시 상태, 충돌/미확인 경계를 반환한다. 생성 문서 본문은 파일 산출물이며 API 추적 응답에 불필요하게 복제하지 않는다.

**방법·원리.** 첫 full render는 전체 baseline을 생성하고 segment ID·node/field/relation 의존성·template version·hash를 manifest에 등록한다. 후속 incremental render만 F3 영향 구간을 사용한다. 부분 렌더가 전체 렌더와 동등한지, 전체 baseline 원본과 수기 영역이 보존되는지, hash CAS·게시 범위 소유권이 유효한지 확인한 뒤 게시한다. Git 파일·SQLite manifest는 F0 journal 절차로 재개/복구하며 동시 수정은 거부한다.

**경계·선행.** F3 impact_set 및 F1 source 기준 선행. F2는 manifest 형식·등록/조회 기반만 먼저 제공하고 F4가 실제 manifest를 공급한다. planning은 renderer/manifest 의미, resources는 임시 산출물, reconciliation은 Git dirty/conflict, DB는 파생 manifest/index를 맡는다. 실 manifest 연계와 전체 흐름은 F10에서 확인한다.

**시험.** `P3-F4-01`: 첫 전체 렌더가 baseline manifest를 등록하고, 그 뒤 incremental partial 결과가 전체 생성과 동등하며 baseline 원본·수기 영역·고정 ID를 보존한다. `P3-F4-02`: 동시 사용자 편집, 다른 영역 소유권, dirty 변경을 hash CAS로 탐지해 conflict를 반환한다. `P3-F4-03`: staging/게시 중단을 journal로 복구하고 manifest·파일 hash를 대조하며 잘못된 template/manifest는 게시 전에 실패한다.

**로그.** `planning.document_segments_staged`, `planning.document_segments_published`, `planning.document_publish_conflict`, `planning.document_publish_recovered`; 추적: event/request ID, project/document/segment ID, source commit·dirty hash, template/manifest version, old/new hash, 소유권 기준, journal ID, 게시 결과·오류 분류. 문서 전문·비밀 제외.

**완료·인계.** 동등성·보존·충돌·복구 증거와 기존 수기 자료 호환을 메인이 확인한다. renderer 계약, manifest 전환/재구축 및 실제 미검증 환경을 인계한다. 확정 template 내부 결정성은 자율, 사용자 편집을 덮을 의미 결정·공유 게시 영역 변경은 메인 판단이다.

## 공통 완료·계약 제안

이 문서의 모든 시험은 `planned`다. 실행 전 실제 대상·환경·명령·종료 코드·증거와 commit/dirty 상태를 기록하며, 미실행은 통과가 아니다.

공통 계약은 [F0](contracts.md)를 참조한다. F2의 초기 index는 구간 의존 형식과 등록/조회 기반까지만 완성하고, F4의 최초 전체 렌더가 실제 manifest를 제공해야 F3가 구간 영향 완전성을 높일 수 있다. F2 fixture 시험은 이 연계와 독립이며 실제 F2/F4 연결 검증은 F10이 소유한다.

작업자는 기능 구현·자체 증거·미해결 계약을 인계한다. 통합 완료, 공통 계약 승인, 전체 상태 반영은 메인이 맡는다. 구현 중 schema/API 호환, 관계 의미, 원자성·복구 보존 정책 변경이 필요하면 근거·영향·대안을 제안하고 확정 전 공통 계약을 임의 변경하지 않는다.
