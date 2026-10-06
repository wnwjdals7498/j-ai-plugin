# R2 변경 수집·기능 연결과 R3 영향·근거 적용성·작업 방향 반영 계획

2026-10-06. 상태: **실행 설계 제안**. 이 문서는 R2/R3의 실행 가능한 논리 작업과 검증 기준이다. 코드/API/schema/PMT 업무 상태를 변경하거나 시험을 실행한 기록이 아니다. 공통 정보 의미는 [4단계 계약](contracts.md), 전체 단계 관문은 [상세계획](implementation-plan.md), 시험 ID와 계층 기준은 [검증 명세](verification.md)를 따른다. 실제 기능 추가 및 계약 확정은 R0 이후다.

## 조사 기준과 설계 결론

현재 기준은 Core 0.3.0, SQLite schema 4, graph schema 1이며 문서 기준 commit `ac7aded`다. 실제 착수 시 메인은 checkout의 HEAD·dirty 지문·문서/graph·DB schema를 다시 확인해야 한다. 계획 문서의 기준 SHA만으로 구현 결과나 시험 적용성을 주장하지 않는다.

현재 `pmt.reconciliation.service`는 project scope와 실제 run/workspace 소유권을 확인한 뒤 Git commit 범위, path 단위 dirty 내용, 기존 baseline, graph `file_refs` 후보 영향, 명시 검토 응답을 수집한다. 관찰/검토 전에는 baseline을 전진시키지 않으며 dirty owner·dirty fingerprint·확정 commit이 없거나 branch 이력이 갈라지면 중단/재조정한다. 현재 경로 영향은 path-to-graph-node 후보이며 함수·코드 의미·요구 영향의 완전한 index가 아니다. 이 서비스의 `reviewed_changes`·`impact_set_reconciled` 입력은 경계 확인 값이다. 이를 AI나 caller가 넣었다는 이유로 user intent 승인·실제 변경 검토 증거로 삼지 않는다.

현재 `SourcePin`은 repository/project/ref/commit, graph schema/revision/hash와 **graph 대상** dirty 상태·fingerprint를 묶는다. `verify_source_pin`은 이 전체 지문의 동일성을 확인한다. 코드 및 문서 파일 전체의 범위별 기준을 표현하지 않으므로 R2 변경 receipt의 기준으로 단독 사용하면 안 된다. 이번 메인 제안은 선택된 코드/문서/결정/검증 정의 범위의 actual inventory ref/hash와 coverage를 `BasisVector` source component로 연결하고, 그 내부에 versioned `ScopeFingerprint`를 둘지 R0에서 확정하는 것이다. SourcePin은 graph authority와 상호 참조로 유지한다. 저장 위치·형식·버전·SourcePin 확장 또는 대체 여부는 R0에서 소비자 호환을 검토해 메인이 확정한다. 그 전에는 이를 공용 public API/schema로 구현하거나 확정하지 않는다.

F3 `calculate_impact`는 F1이 검증한 `ChangeSet`/preview와 현재 SourcePin/index를 대조해 field semantic, 관계 경로, 문서 segment manifest, Step와 verification 후보 및 unknown을 계산한다. 임의 `git diff`는 F3 typed graph delta가 아니며, path 목록만 F3에 넣어 graph 의미·요구 영향으로 승격할 수 없다. R2 ImplementationLinkIndex는 F3의 대체물이 아니라 실제 코드/문서 변경에서 검토 가능한 연결 후보를 구성하는 별도 version-bound index다. graph 관계/ID를 바꾸려면 F1 검증 변경 경로를 거쳐 F3 typed delta를 별도로 만든다.

근거 적용성에는 F6의 정의가 선택한 selectors를 재사용한다. 현재 F6는 workspace 파일의 선택 경로, `SourcePin` 선택 필드, criteria, dependency manifest, runtime/tool, model provenance 등을 정의에 따라 지문화한다. selector가 고른 조건만 비교하고, 선택되지 않은 무관 조건을 key에 추가해 기존 재사용 범위를 넓히거나 좁히지 않는다. P2 verification lookup은 실제 snapshot, 정의/명령/환경/criteria, workspace·dependency·config·runtime 지문, ready/hash-valid evidence와 뒤이은 non-pass를 확인한다. R3는 기존 `pass`·verification 상태를 덮지 않고 `applicable / not_applicable / unknown`을 별도 판단 receipt로 연결한다.

Host 파일 경계는 `HostedFiles`/`HostedLocalRuntime`이 current run과 local mapping/source 권한을 확인하고 client에서 파일/Git 작업을 수행한 뒤 Host의 effect journal·resource·SourcePin/index metadata에 실제 effect를 연결하는 형태다. `HttpStore`/Host allowlist에 R2의 Git diff·source file 읽기나 R3 해석/문서 편집을 노출하지 않는다. Host 확대가 필요하면 저장 전용 metadata receipt로 제한하고 R0에서 scope·CAS·request replay를 검토한다.

## 판정 용어·공통 불변식

| 수준 | 기록 기준 | 허용되는 결론 |
|---|---|---|
| observed | 현재 권한으로 읽은 실제 Git/파일/문서/결정 기록/DB 상태와 capture 주체·시간·범위·hash·완전성이 확인됨 | 경로·bytes·ref·상태 등 관찰 사실만 확정 |
| analyzed | observed receipt와 명시적 mapping/조건/rule version을 사용해 영향·근거 조건을 비교함 | 후보·known·unknown과 근거 경로를 제시. 원 방향/문서 미변경 |
| applied | 현재 owner·권한·fresh basis·사용자 결정 또는 명시 위임이 일치하고 게시 효과 및 after-state가 확인됨 | 적용 receipt를 만들고 그 변경분의 반영 기준만 갱신 |

모든 기준은 `before_basis`와 `after_basis`를 구분한다. 수집 중 source, path 내용, owner, 업무 revision 또는 관련 mapping이 바뀌면 성공 receipt 대신 `incomplete/source_changed`를 반환하며 이전 확정 pointer를 유지한다. 관찰 지문과 분석 지문은 다르며, 분석 완료는 반영 완료가 아니다. CAS 불일치 때 무관한 최신 항목을 덮어쓰지 않고 current 상태 재조회·재평가 또는 확인 요청으로 멈춘다. request 재전송은 같은 request fingerprint/effect/journal을 조회해 수렴하며 서로 다른 event ID를 hash 같다는 이유로 합치지 않는다. 반영 전후 불일치나 응답 유실은 원 effect와 실제 Git/파일/DB/Host 상태를 대조하기 전 다시 게시하지 않는다.

권한 경계는 shared metadata와 실제 source/업무 write로 나눈다. `MetadataAccess`는 현재 auth 아래 공개 가능한 checkpoint/change/alignment ref와 제한된 metadata를 조회할 뿐, Git·private 본문·working tree를 읽거나 실행할 권한을 주지 않는다. source/path 상세 읽기 전에는 실제 현재 `WorkAccess`(run/claim·scope union·owner·revision·current source/mapping)를 다시 확인하고 client mapping으로 읽는다. 반영은 적용 순간의 WorkAccess와 source/업무/plan/pointer 기대 revision을 재검증한 뒤에만 한다. Host에는 권한 있는 metadata/receipt와 provenance만 저장하며 클라이언트의 Git 검사나 물리 적용을 Host가 수행한 것처럼 표현하지 않는다. 원래 작업 owner의 dirty path 지문은 현재 `reconciliation.service`처럼 client 실행 중 scope claim을 확인한 뒤 해당 client에서만 읽는다. 일반 개요·Host metadata·다른 device에는 dirty 본문이나 절대경로를 보내지 않는다. Host에서 받은 변경 설명, completion 주장, caller Boolean, commit message, 모델 답변은 대조할 후보/입력이지 증거 또는 intent approval이 아니다.

## Step별 목표와 비목표 추적

| Step | Goal | Non-goal |
|---|---|---|
| R2-S1 | 허가된 실제 commit/dirty 변경을 before/observed basis와 재현 가능한 receipt로 수집 | dirty 수정, baseline/apply 기준 전진, 타 owner 내용 읽기 |
| R2-S2 | 코드·기능·요구·작업·검증의 source-bound 연결과 누락 coverage를 산출 | 모든 언어·동적 경로의 완전한 의미 분석, graph schema 임의 확장 |
| R2-S3 | 실제 문서/결정 근거와 변경 사실을 연결하고 intent known/unknown을 분리 | commit/model/외부 completion 주장으로 사용자 승인 추정 |
| R2-S4 | 분기·불일치·replay를 조정하고 R3에 complete 또는 명시적 unknown을 인계 | Git branch 조작, 미확정 상태를 no-change나 최신 기준으로 축소 |
| R3-S1 | 실제 관계와 검토 mapping의 영향 경로·coverage·unknown을 산출 | raw Git diff를 F3 typed delta로 승격, 무관계를 무영향으로 확정 |
| R3-S2 | F6 selector와 실제 verification/evidence 조건으로 기존 근거의 적용성을 별도 판정 | 기존 pass/state 덮기, 선택되지 않은 조건으로 reuse key 변경 |
| R3-S3 | 위임 내 방법 조정과 대전제 변경에 필요한 사용자 판단을 구분 | AI가 사용자 의도·대전제를 독립 승인 |
| R3-S4 | 승인 범위의 관련 항목만 CAS/journal로 반영하고 물리 결과를 receipt에 증명 | 전체 자동 재작성, 미승인 방향 변경, 무관 가지/유효 근거 수정 |
| R3-S5 | 현재 source·업무·권한 상태를 재검증해 정렬 결과 또는 미해결 사유를 인계 | 시간 경과/늦은 응답/오프라인 receipt만으로 완료·기준 전진 |

## R2 — 실제 변경 수집과 구현 연결 index

### R2-S1 — 권한·기준 고정 후 실제 Git/dirty 변경 수집

**목적·이유.** 재개 기준 이후의 실제 commit 및 현재 owner의 미커밋 변경을 재현 가능한 receipt로 만들고, Git baseline의 관찰·분석·반영 경계를 분명히 한다.

**범위·goal.** 추가: Git ref/HEAD 비교, rename/delete 포함 path 상태, 실제 dirty 파일 hash 또는 제한된 content evidence ref, 범위 fingerprint, collected/unmapped receipt. 수정: 기존 `read_project_baseline`/`sync_project_baseline` 흐름을 checkpoint의 before-basis 및 R2 receipt 소비 방식으로 연결. 삭제: 없음. 금지: 변경 파일 수정/merge/reset/stage, 타 owner path 읽기, baseline·반영 pointer 자동 전진, Git 전체 내용 Host 전송.

**입력·출력.** 입력은 명시 repository/project/workspace/ref, 현재 actor/session/run 및 path/workspace scope claim, checkpoint/baseline before-basis, client mapping, 실제 Git inspection 결과다. Git SHA/ref와 canonical relative path는 검증된 문자열, path collection은 정렬·중복 제거된 상대 경로 집합, content는 ref/hash/size로 제한한다. dirty 지문은 status만이 아니라 읽은 실제 내용의 hash와 index state를 구분한다. 출력은 `ObservedChange` 초안: origin(`commit_range`, `working_tree`, `non_git`), before/observed-after basis, 추가/수정/삭제/이동의 path 사실, dirty owner/범위, capture completeness, 수집 시각·도구 version, diff/resource ref/hash, `observed` 상태다. Git 불가·역사 분기·workspace 이동·scope 부족·owner 미확인·capture 중 변동은 실패 또는 incomplete이며 빈 변경으로 처리하지 않는다.

**방법·연계.** 논리 포트 `CollectChanges`를 기존 shell=False Git 호출, `_git_context`, `_status_paths`, `_status_and_dirty`, `_read_scope_status`, `_require_any_run_scope`, journal/reconciliation receipt의 trusted adapter로 연결한다. 실제 Git diff는 byte-level 변경 자료만 생산한다. 현재 `SourcePin`은 graph 중심이므로 코드/문서 경로 전체의 scope basis라고 가정하지 않는다. R0에 제안한 selected source inventory/hash/coverage를 BasisVector에 연결하는 의미가 확정되기 전에는 `SourcePin` 원본의 의미를 조용히 바꾸지 말고 receipt에 해당 제한을 `coverage=unknown`으로 남긴다. CollectChanges의 저장은 `RegisterObservedChange` 논리 경계에 넘기며 실제 source 수집과 Host metadata write를 구별한다.

**선행·소비자.** R0 scope/CAS/read-authority 계약과 R1 확정 checkpoint/basis가 선행한다. mapping fixture는 병렬 준비할 수 있다. R2-S2/S3, 이후 R3가 실제 receipt를 소비한다. R6의 client/Host 통합은 이미 수집된 제한 metadata만 저장한다.

**전후 기준·CAS·중단·복구.** `before_basis`는 이전 확정 checkpoint/source pointer이며 수집 결과는 별도 `observed_basis`다. 분석 완료/반영 완료 revision은 유지한다. path 접근 전 현재 claim과 겹치는 다른 run owner를 확인한다. 2회 capture 사이 HEAD/ref/status/content hash가 다르면 incomplete로 고정하고 다시 읽을 때도 소유권을 재확인한다. 같은 request는 operation journal/effect를 찾아 재개하고, 결과가 유실되면 Git/파일을 다시 읽어 existing effect를 조정한다. 기록 실패 때 dirty 파일은 그대로 두고 마지막 확정 pointer와 미해결 journal을 보존한다.

**검증·실제 대조.** P4-R2-01: 동일 baseline 무변화 및 새 commit·문서 변경에 대해 Git `rev-parse`, 실제 `diff --raw`/`diff --name-status` 및 파일 SHA와 receipt를 비교하고 무변화 중복 분석이 없는지 DB의 journal/revision으로 확인한다. P4-R2-02: rename/delete, branch 전환/분기, dirty, non-Git, 타 owner/겹치는 scope에서 Git path·bytes·파일 유무, SQLite `scope_locks`/owner, baseline fingerprint와 실제 수정 여부를 비교한다. 기준 불가 시 pointer가 전진하지 않아야 한다. P4-R2-03: 수집 중 파일·HEAD 변동 및 실제 path 누락은 `incomplete`/unmapped으로 남고, 입력에 넣은 외부 completion 문자열과 실제 Git/파일 상태가 불일치하면 거부되는지 실제 receipt·파일 hash로 대조한다. 관련 기반은 P4-R0-02(범위/권한), R0-03(replay/CAS), R6-01(다른 device의 bytes/Host metadata 경계)다. 예정 ID이며 이 문서 작성 중 실행하지 않았다.

**기록·완료·인계.** 진단에는 project/repository ref, run/owner의 비민감 ID ref, before/observed basis hash, dirty fingerprint, path 개수·coverage·시간·원인 코드·journal/replay 상태를 기록한다. 업무 이력은 실제 변경 관찰/불완전 전이만 남긴다. 시험 증거는 격리 Git repository의 전후 `HEAD`, porcelain/status, path/hash manifest, SQLite owner/baseline/journal 전후 dump를 포함한다. 절대경로·원문 diff는 일반 로그에서 제외한다. 완료는 읽기 권한·owner·전후 지문·coverage가 실제 확인되고 receipt가 재처리 가능한 상태일 때다. 분석/반영 기준 미전진, unmapped 및 unknown 범위를 R2-S2/R3에 인계한다.

### R2-S2 — 코드·기능·요구·시험 mapping 및 version-bound index

**목적·이유.** 실제 바뀐 경로가 어느 기능/모듈/요구/작업/시험에 연결되는지 설명 가능한 범위로 좁히되, 파일 경로 일치와 의미 연결을 혼동하지 않는다.

**범위·goal.** 추가: `ImplementationLinkIndex` 논리 projection, 안정 node/function/module/interface ref, path/symbol과 project graph/계약/Work/Step/verification 간 사람이 검토한 link, 추출 후보, unmapped 및 parser coverage. 수정: graph의 `file_refs`, node IDs·관계, F2 index 및 F3 영향 계산을 조회 가능한 후보 원천으로 사용. 삭제: 폐기 관계는 새 index에서 current 연결로 노출하지 않고 이전 receipt 참조는 보존. 금지: graph schema 1에 임의 relation 종류 삽입, raw diff를 typed graph delta 취급, 미지원 언어/동적 호출을 완전 분석으로 간주.

**입력·출력.** 입력은 R2-S1 actual path/change receipt, 현재 owner 권한이 허용한 코드·문서 경로, graph/index SourcePin, BasisVector 안의 선택 source inventory/hash/coverage, mapping registry·추출기 version, F3/F6/verification 정의 refs다. 각 ref는 안정 ID/종류/버전, 연결은 `verified_mapping`, `extracted_candidate`, `reviewed_interpretation` 중 하나와 근거를 가진다. 출력 index는 source/basis/index/mapping version/hash, path→symbol→기능/graph/요구/Step/verification 후보, 방향·근거 경로·정확도 수준, mapped/unmapped count 및 누락 이유다. 논리 포트는 `BuildImplementationLinks`이며 version-bound `LinkIndexRef`를 반환한다. 실제 세부 코드를 읽으려면 MetadataAccess가 아니라 WorkAccess가 필요하고, MetadataAccess만 있는 소비자에는 허가된 ref와 coverage만 공개한다. 동일 문자열·basename만 맞는 후보는 semantic verified가 아니며, 동적 dispatch·reflection·generated code·외부 dependency·미지원 언어는 unknown으로 유지한다.

**방법·연계.** 논리 포트 `BuildImplementationLinks`를 기존 `planning.graph`의 schema validation/traversal과 `efficiency.graph`의 source-pinned graph index/manifest 조회에 연결한다. 처음에는 등록된 명시 `file_refs`와 사람이 확정한 mapping을 우선하고, 제한 parser는 지원 언어/문법의 증거 후보만 낸다. parser 출력이 graph 관계를 변경하지 않는다. `SourcePin`은 graph hash를 확정하지만 소스 코드 파일 mapping은 BasisVector의 선택 source inventory/hash/coverage로 별도 확인해야 한다는 조건을 index key와 증거에 반영한다. 단일 checkout에서 최신 후보를 계산하더라도 기능 명세/문서 본문은 파생 복제본으로 저장하지 않고 stable refs를 가리킨다.

**선행·소비자.** R0에서 mapping ref/unknown/범위 fingerprint 저장 의미가 정리되고 R2-S1 receipt가 필요하다. seed mapping은 fixture 준비 가능하나 실 source 검증은 R1/R2 basis 후. R3-S1은 후보 경로와 coverage를 사용하며 F3 graph delta와 별도 칸으로 연결한다. R4는 최종 mapping 상태와 ref를 소비한다.

**전후 기준·CAS·중단·복구.** 이전 index는 immutable version/hash로 참조하고 새 index는 current observed source/scope fingerprint 및 규칙 버전에 묶는다. 증분 재사용은 선택된 실제 path/hash와 mapping version이 동일한 부분에 한한다. 계산 중 source/mapping/graph revision 변경, 검토 권한 상실, parser 미지원은 새 확정 index를 publish하지 않고 후보/unknown으로 돌린다. index 재구축 실패 시 기존 last-known index는 stale 표시와 함께 보존하고 최신으로 대체하지 않는다. request 재처리는 같은 version-bound index receipt 조회로 귀결되어야 한다.

**검증·실제 대조.** P4-R2-01에서는 매핑한 변경 path의 실제 bytes와 index source hash/version을 비교하고 무변화 재계산의 cache/index hash가 동일한지 확인한다. P4-R2-02에서는 경로 이동·rename/delete 뒤 안정 기능/요구 ID와 old/new ref 관계를 실제 graph JSON·파일 목록·index row/파일로 확인하며 ID가 사라지거나 임의 재발급되지 않아야 한다. P4-R2-03에서는 의도적 누락/동적 symbol/미지원 언어를 `unmapped/unknown`으로 표시하는지, AI가 제시한 mapping은 검토 상태 이전에 확정되지 않는지 실제 index/receipt에서 확인한다. 관련 P4-R0-01(호환·실제 버전), R0-02(권한/범위), R6-02(새 session이 bundle의 mapping 수준을 오해하는지), 기존 P3-F2/F3 검증은 regression ref로 연결하되 신규 P4 pass로 재표기하지 않는다.

**기록·완료·인계.** 진단은 index/mapping/parser version, source/scope hash, mapped/unmapped 수, depth/coverage, 실패 원인·시간만 기록한다. 업무 이력은 사람의 mapping 확정/폐기 같은 의미 행동을 event로 구별한다. 증거는 격리 source 경로·실제 symbol 또는 graph ref·매핑 근거·동일 source 전후 index hash와 DB 상태를 포함한다. 완료는 모든 확정 link의 실제 원본 ref와 version을 재검증하고 비지원 범위가 명시됐을 때다. R3에는 typed graph delta가 아닌 path/index 후보 및 coverage를 보낸다.

### R2-S3 — 문서·결정·외부 변화 후보의 원본 대조와 통합 receipt

**목적·이유.** 변경 이유와 사용자 방향을 파일 수정 자체에서 추정하지 않고, 문서/결정의 출처·주체·버전을 actual change와 연결한다.

**범위·goal.** 추가: source-bound document/decision event refs, 실제 전후 section/content hash, 외부 변경 주장 대조 결과, 사실/intent known-unknown 연결을 `ObservedChange`에 병합. 수정: `docs/pmt-docs`와 PMT 결정·계획·Step 지시의 버전/authoritative source refs를 current baseline에 연결. 삭제: 없음. 금지: commit message, 모델 제안, imported summary, Host 응답을 사용자 승인으로 승격하거나 다른 원본에서 임의 정보를 가져오기.

**입력·출력.** 입력은 R2-S1 실제 파일/commit receipt, 허가된 문서·결정 event ID와 현재 내용 hash, 사용자 주체/명시 위임 scope, R2-S2 mapping 후보다. 출력은 변경 사실과 별도로 `intent_evidence_refs`(현재 결정/문서의 실제 ref/version), `intent_state=known|unknown`, 각각의 근거 source 및 판정 주체를 가진 통합 receipt다. 변경이 document-only/non-Git인 경우 commit SHA 대신 명시 문서 fingerprint·capture 범위·업무 event revision을 사용한다. 원본에 접근 못 하면 unknown이다.

**방법·연계.** 논리 포트 `ReadChangeSlice`로 retained receipt/detail을 조회하고, 이를 기존 project Git docs/graph baseline 및 SQLite의 결정/업무 event·resource hash에 read-only 연결한다. `MetadataAccess`는 retained, 공개 가능한 change summary/ref 조회에만 충분하다. 문서 내용/hash 또는 private detail을 읽을 때는 현재 WorkAccess와 resource 권한을 확인한다. 수집 receipt 등록은 `RegisterObservedChange`의 current scope authorization을 거친다. baseline reconciliation은 현재 Git docs/pmt-docs 중 graph와 plan 문서 일부를 인지하므로 이를 문서 전체/모든 결정으로 확대하지 않고 coverage를 표시한다. 외부 출처가 필요한 사용자 의견·시장 평가가 아니라 정보성 주장의 사실 근거는 공식 페이지/원 코드/재현 가능한 시험으로 제한한다. 임의 네트워크 수집이나 타 user file 경로 접근은 하지 않는다.

**선행·소비자.** R0의 source/event/version/ref 체계, R2-S1 actual change, (있다면) R1 checkpoint가 선행한다. R2-S2 mapping 후보와 서로 교차 링크하되 어느 한쪽의 미확인이 다른 쪽의 증거가 되지 않는다. R3-S3이 위임·사용자 판단 경계를 평가한다.

**전후 기준·CAS·중단·복구.** 문서 hash/event revision은 R2 관찰 기준으로 기록하며 아직 applied baseline은 고정한다. 읽기 전후 변경 또는 parent decision 갱신이 있으면 receipt는 stale/incomplete가 되고 재수집한다. 하나의 서로 다른 user decision event는 본문이 동일해도 별도 event ID로 보존한다. 중단 후 request replay는 기존 수집 receipt를 조회하며 사용자 event를 중복 생성하지 않는다.

**검증·실제 대조.** P4-R2-01은 commit 기반 문서 변경과 같은 기간의 실제 decision event를 source Git blob/hash와 SQLite event/version으로 각각 대조한다. P4-R2-02는 rename/delete·branch divergence에서 문서 stable ref 및 현재 파일 상태와 이력 분기를 확인한다. P4-R2-03은 미확인 외부 완료 claim, 존재하지 않는 event, 오래된 summary, 동일 본문/다른 event ID를 주입해 actual source와 불일치 claim은 거절·unknown, 독립 사건은 보존되는지 확인한다. 관련 P4-R0-02(명시 authority와 scope), R0-03(event/request 의미), R6-01(다른 기기 pending 실제 bytes 접근 제한), R6-02(fresh session source refs 이해)를 연결한다.

**기록·완료·인계.** 업무 이력에는 결정 event 자체와 변경 관찰 event를 분리해 기록한다. 진단은 ref/hash/outcome/reason, 실제 권한 identity ref만 저장하고 문서 본문/대화는 복제하지 않는다. 증거는 source Git blob/file hash, DB decision event/version, 접속 가능한 원본 위치 ref를 포함한다. 완료는 changed fact와 intent evidence/unknown이 분리되고 실제 주체·revision과 연결된 때다. 의미 결정이 필요한 항목은 R3의 대전제 판단으로 넘긴다.

### R2-S4 — 경계 조건·중복·분기·non-Git 통합 확인과 인계

**목적·이유.** 정상 path 외의 상태가 영향 없음이나 최신 기준으로 조용히 축소되지 않게 한다.

**범위·goal.** 추가: receipt completeness/unknown taxonomy, rescan·replay 복구 ref. 수정: S1~S3의 통합 상태로 전달하고, 이름 변경은 delete+add와 rename 후보를 원본 비교로 구별. 삭제: 임시 후보는 보존 정책에 따라 만료 가능하되 활성 checkpoint/effect를 가리키는 ref는 제거 금지. 금지: branch 자동 checkout/merge, dirty reset, 무관 path 전진, 현재 소유 확인 없는 cross-device 재구성.

**입력·출력.** 입력은 모든 origin receipt, 관련 checkpoint/source/mapping/decision versions, 동일 request/event 재호출 결과다. 출력은 최종 `ObservedChange` 상태(`complete`, `incomplete`, `reconciliation_required`, `no_change`), origin별 actual basis와 충돌·누락 목록, 다음 rescan/owner 확인 조건, coverage다. `no_change`는 선택된 scope와 입력 목록 전체가 실제 검사된 경우만 가능하다.

**방법·연계.** reconciliation journal과 request cache, `SourcePin` 검증, R0 prospective Basis/Scope fingerprint, 실제 Git refs/working tree, SQLite 업무 이력을 조정한다. F3가 요구하는 `ChangeSet/preview`가 별도로 존재할 때만 이를 typed graph delta 후보로 참조한다. 여기서 만든 raw git observation을 F3 delta로 변환하지 않는다.

**선행·소비자.** R2-S1~S3와 R0 request/effect semantics. R3에 평가 가능한 receipt만 complete로, 그 외는 명확한 unknown/incomplete로 넘긴다. R6가 local/Host 저장·복구의 parity를 확인한다.

**전후 기준·CAS·중단·복구.** 각각의 receipt는 원래 `before_basis`와 capture source, 각 component capture 시각, 확인된 `after_basis`를 갖는다. 단일 원자 snapshot을 꾸며내지 않는다. latest observed pointer 갱신도 R0에서 정한 expected revision CAS 뒤에만 수행하고, applied pointer는 유지한다. stale CAS·Host 응답 유실·미확정 journal은 current status와 실제 effect ref를 조회해 조정한 뒤 재평가하며 동일 수집 요청을 새 user event로 만들지 않는다.

**검증·실제 대조.** P4-R2-01~03의 종료 조건을 합쳐 git status/path/hash, 실제 SQLite baseline/journal/pointer/request 결과, scope owner 및 resource bytes를 대조한다. R0-01은 현재 schema/API가 없는 계획 단계임을 확인해 legacy operation/envelope가 변하지 않았음을 기준으로 삼고; 구현 이후에만 새 version migration을 수용한다. R0-02/03 및 R6-01의 DB/CAS/Host 시나리오를 통합한다. 여러 번 실행해도 같은 request effect는 하나, 서로 다른 decision event는 각 하나로 확인한다. 시험 명령·종료 코드는 시험 실행자만 실제 결과를 받은 뒤 manifest에 기록한다.

**기록·완료·인계.** 업무 이력은 바뀐 의미 상태만, 진단 로그는 반복 observation 요약만, 증거 manifest는 actual git/sql/file/owner 전후 스냅샷을 담는다. 완료 조건은 R2 receipt의 origin·scope·coverage·owner·source basis·replay ID가 실제 자료와 일치하며 unknown이 표면화된 것이다. R3 인계에는 receipt ref/hash, current unadvanced pointer revision, 검증 가능한 세부 읽기 ref, 기존 실행/pending, 무관 path 보존 근거를 포함한다.

## R3 — 영향·근거 적용성·작업 방향 정렬

### R3-S1 — 검토된 관계에 따른 영향 경로와 coverage 평가

**목적·이유.** 수집한 변경 후보가 요구·기능·계획·작업·문서·검증에 미친 영향을 실제로 연결된 관계만으로 산출하고, mapping 부재를 영향 없음으로 오해하지 않게 한다.

**범위·goal.** 추가: version-bound `AlignmentAssessment` 영향 projection, direct/transitive path·confidence·coverage·unknown 원인. 수정: F3의 검증된 typed graph delta 영향 결과와 F2 index, R2 ImplementationLinkIndex를 별도 증거 계층으로 결합. 삭제: 없음. 금지: raw Git diff를 F3 delta라 부르기, node `file_refs` 경로 hit만 의미 영향 확정, unknown 제거를 위한 임의 relation 추가, 문서/Step 편집.

**입력·출력.** 입력은 complete R2 receipt 또는 부분 receipt(불완전 표식 필수), current goal/premise/exclusions/delegation refs, current graph/index/change preview와 source fingerprint, 문서 dependency manifests, active run/claim/pending 및 verification selectors다. 출력은 영향 node/ref 후보, 실제 relation path와 mapping provenance, direct/indirect/possible 구별, changed/unchanged/unknown condition, 누락 coverage 및 관련 시험/근거 후보를 포함한다. `unknown`이면 결과 `complete=false`; 관계 없음은 `no_known_path`이지 의미상 무영향 확정이 아니다.

**방법·연계.** 논리 포트 `AssessAlignment`에서 F3 `calculate_impact`가 받는 실제 F1 change preview/change set, SourcePin, index hash, field rule version, graph relation traversal, document segment manifest를 사용한다. R2 raw path mapping은 별도 후보 목록으로 제공하고 F3 known 결과와 혼합하지 않는다. 기능·코드 연결은 `verified_mapping`만 확정 경로, extracted/reviewed candidate는 review 필요로 유지한다. SourcePin에 묶이지 않은 소스 code/document 범위는 BasisVector의 source inventory ref/hash/coverage로 확인하고, 이 연결이 없으면 해당 범위를 unknown으로 표시한다.

**선행·소비자.** R0 value/authority, R1 current basis/active execution, R2 actual change/index가 선행이다. 사전 영향 분석에는 F1이 검증한 `ChangeSet`과 source-bound preview면 충분하며, 이때 F3 결과는 후보 영향 assessment다. F1 apply receipt는 실제 graph 변경이 게시된 뒤 후속 문서 segment 게시·변경 적용 확인·최종 applied 기준 전진에 필요하다. 따라서 사전 assessment가 F1 apply를 선행 조건으로 요구하지 않으며, apply receipt가 아직 없으면 후속 게시/적용은 대기한다. S2/S3, R4 요약이 소비한다.

**전후 기준·CAS·중단·복구.** analysis는 before/observed source·graph/index/change rule hash·basis에 bound된 불변 assessment다. 분석 중 mapping, graph, run status, user direction이 달라지면 assessment stale 표시 후 재조회한다. 분석만으로 plan/doc/graph/version pointer를 바꾸지 않는다. 재처리는 동일 assessment hash를 재사용할 수 있으나 source/조건 바뀌면 새 평가가 필요하다.

**검증·실제 대조.** P4-R3-01은 위임 범위의 code method 변경·premise/goal 변경·의도 미확인 케이스에서 실제 graph/path refs와 업무 direction source를 대조해 자율 범위 경계를 본다. P4-R3-02는 영향 graph의 related/unrelated node, 누락 mapping, prior/current index를 DB/graph/file hash로 대조하고 무관 가지가 assessment에서 보존되는지 확인한다. P4-R3-03은 분석 중 source/revision/run 변동 시 기존 receipt는 stale로 남고 최신 pointer는 안 움직이는지 SQLite CAS/journal 및 actual graph/file 비교를 한다. 관련 R0-02(scope), R0-03(CAS/replay), R6-02(fresh session), 기존 P3-F3-01~03 regression refs를 연결한다.

**기록·완료·인계.** 진단은 source/index/rule/basis hash, known·unknown·coverage 개수, relation path/ref IDs, 시간·reason만 둔다. 업무 이력은 평가 사건을 반영 결정과 구분한다. 시험 증거는 actual graph file, F1/F3 receipt, F2 index SQL row/hash, 등록 segment manifest와 mapping ref를 포함한다. 완료는 각 영향 결론이 source ref와 경로·근거를 갖고, 누락이 명시된 것이다. S2에는 적용성 확인이 필요한 원리/검증 refs를 전달한다.

### R3-S2 — 기존 원리와 검증 증거의 현재 적용성 판정

**목적·이유.** 유효한 기존 근거는 같은 조건에서 재사용하고 관련 조건 변화·후속 실패·증거 손상 때는 과거 pass를 현재 pass처럼 쓰지 않는다.

**범위·goal.** 추가: 별도 `EvidenceApplicability` receipt와 `applicable/not_applicable/unknown` 이유·selector/basis. 수정: F6 reuse definition selectors와 P2 `lookup_verification`의 실제 snapshot/evidence 판정을 소비한다. 삭제: 없음. 금지: 기존 verification outcome/state를 변경, 선택하지 않은 조건을 reuse key에 넣기, 모델이 pass라고 서술한 것을 시험 outcome으로 기록, evidence ref/hash 접근 확인 없이 applicable로 선택.

**입력·출력.** 입력은 assessment에서 영향 후보로 분류된 prior principle/verification refs, 각 F6 definition/version/selector와 실제 관련 조건, verification 대상/command/environment/criteria, 실제 before/after snapshot, 후속 fail/non-pass, ready artifact IDs·scope/hash/access. 출력은 source artifact refs·적용성 상태·각 선택된 selector 조건의 stored/current hash·실제 조회 결과·무효/unknown 사유·필요한 재검증 후보다. 전제 조건 하나라도 선택됐는데 지문이나 권한이 없으면 `unknown`; 선택된 관련 조건의 mismatch, 후속 실패, hash/ready/scope 손상 확인 시 `not_applicable`다.

**방법·연계.** F6 `build_reuse_key`, selector resolver의 `workspace_files`, `source_pin_fields`, `criteria`, `dependency_manifests`, `runtime_fields`, verification model provenance를 재사용한다. P2 `verification._snapshot/_lookup`으로 현재 스냅샷과 definition/command/environment/scope/criteria/evidence hashes 및 rowid 뒤 non-pass를 확인한다. F6 reusable status와 P2 verification pass/state는 각자 authority를 유지하며 R3 적용성은 별도 파생값이다. 사용자가 정의에 고른 선택 조건만 selector를 통해 지문화하고, 정의에 없는 원리의 적용 여부를 새 기계 규칙으로 발명하지 않는다.

**선행·소비자.** R0 evidence provenance/retention semantics, R1 fresh current basis, R2-S1..S3 receipt 및 R3-S1 후보 경로가 선행한다. 기존 F6/P2 저장 의미가 source fingerprint와 실제 evidence를 조회 가능해야 한다. R3-S3/R4는 상태와 이유만 사용한다.

**전후 기준·CAS·중단·복구.** applicability는 정의 version·selector set·current condition fingerprint·evidence hash·후속 event watermark를 함께 고정한다. 판정 도중 조건/definition/evidence revision이 바뀌면 unknown/stale로 멈춘다. 새 판정은 원 `verification`을 rewrite하지 않고 append-only metadata/revision으로 보존한다. 증거가 일시적으로 접근 불가하면 파괴적 무효 처리로 단정하지 않고 unknown; 실제 hash mismatch/삭제/후속 non-pass는 무효 사유로 보존한다. retry는 read-only lookup부터 수행한다.

**검증·실제 대조.** P4-R3-02의 selector 일치, 관련 조건 변화, 무관 조건 변화, definition 변경, 후속 fail/non-pass, evidence delete/corrupt/wrong-scope를 실제 SQLite `verifications`·F6 reuse records·artifact DB/files/hash와 대조한다. 같은 selector가 선택한 조건·증거·후속 event가 일치할 때만 applicable; 관련 변경은 not_applicable; 선택 조건 미지/권한 없음은 unknown이어야 한다. 무관 path 변경이 적용성 key를 바꾸지 않는 것은 definition이 그 조건을 선택하지 않았을 때만 확인한다. 관련 R0-01(기존 schema/state 호환), R0-02(evidence read authority), R6-02(새 session 사실 인식), R6-03(재사용으로 실제 생략한 재시험/전체 조회 비용 측정)를 연결한다. P3-F6/P2 verification 기존 결과는 회귀 참조이며 P4 통과로 복제하지 않는다.

**기록·완료·인계.** 진단에는 selector/definition/policy version, condition/evidence hash, outcome/reason, follow-up watermark와 실제 check duration만 둔다. 업무 이력은 명시적 적용성 전이/재검토 요청을 기록한다. 증거는 DB row refs/revision, 실제 artifact hash/ready/scope 검사, 대상 조건 snapshot manifest를 포함한다. 완료는 applicable이 기존 검증 상태와 구별되고, invalidation은 이유가 소스 조건에 고정되며 unknown 경로를 숨기지 않는 때다. S3와 R4에 기존 evidence ref와 선택 조건/보류 이유를 전달한다.

### R3-S3 — 위임·대전제 경계를 적용한 방법 판단과 확인 요청

**목적·이유.** 새 사실에서 작업 방법을 조정할지, 방향/대전제를 변경할지 분리해 적법한 수준에서 판단한다.

**범위·goal.** 추가: interpretation proposal, method alternatives, delegation evidence, 선택 주체/범위/이유, 사용자 확인 필요 condition. 수정: 기존 AGENTS.md, 사용자 scope decisions, 프로젝트 baseline requirements/goal, 위임된 작업의 우선순위. 삭제: 낡거나 폐기된 해석 후보의 active 사용만 중단하고 기록 ref는 보존. 금지: AI가 premise/acceptance/exclusion/user intent를 임의 승인, 작성자/commit message/model response만으로 동의 추정, 조용한 전체 계획 변경.

**입력·출력.** 입력은 S1 impact evidence, S2 applicability receipts, 현재 authoritative requirement/decision/delegation version, 실행 owner/status/pending 및 unresolved receipts다. 출력은 관찰된 사실과 분리된 `AlignmentAssessment`의 interpretation, delegated method change candidate 또는 사용자 판단 요청, 대안과 각 영향/ref/unknown/진행 run 충돌, 거부/중단/추가 자료의 필요 이유다. “사용자 선택이 필요한 대전제 변경”은 applied 상태가 될 수 없고, “위임 범위 안 구현 방법 변경”은 명시 위임 ref가 현행이고 그 방법이 상위 acceptance/금지를 건드리지 않는 경우에만 적용 후보가 된다.

**방법·연계.** Python은 원문을 요약해 정책화하지 않고 explicit delegation/premise refs·source revisions·관계·선택된 evidence condition의 비교 및 상태를 제공한다. 의미 판단은 기존 main/native AI 경로에서 수행하고 판단 모델/범위·evidence ref를 기록한다. 모델은 제안자이며 승인은 사용자 결정 또는 기존에 기록된 위임이어야 한다. 재검증에서 원리의 근거가 없거나 F3의 unknown이 있으면 안전한 행동 후보를 낮추고, 사용자 선택 또는 추가 확인을 요구한다.

**선행·소비자.** S1의 영향/coverage와 S2의 적용성, R1 current facts, R0 role/authority contract가 선행한다. S4만 assessment에 기대어 반영 작업을 수행한다. R4는 선택 요청과 제안의 범위를 표시한다.

**전후 기준·CAS·중단·복구.** 판단은 원 delegation, goal/premise, change/evidence receipt hash와 현재 source를 고정한다. 대화 응답이 나중에 오더라도 선택/승인 event를 실제 decision source에 기록하고 current revision을 CAS로 검증해야 한다. assessment 중 상위 목표·owner·run 상태가 바뀌면 stale로 멈춘다. 진행 중인 run이 변경 대상의 옛 지시를 사용하면 기존 control/cancel 요청·실제 handle 확인 규칙으로 중단/대기하며 idle/time-out/Stop만으로 정지 확인하지 않는다. 늦은 결과는 old instruction basis에 귀속한다.

**검증·실제 대조.** P4-R3-01에 구현 세부의 방법 개선(명시 위임 안), 대전제 변경(명시 승인 필요), 의도 불명확 변경을 각각 두고 실제 decision/delegation resource·주체·version과 assessment/action event를 대조한다. 모델 출력만 제시한 경우 applied receipt가 없어야 한다. 진행 run/pending이 있는 premise change는 execution DB handle/claim/current instruction ref와 실제 process receipt로 확인한다. P4-R3-02는 전제/시험 결과의 provenance와 조건 refs, P4-R3-03은 오래된 결정/늦은 run 결과가 current plan을 갱신하지 않는지 검증한다. 관련 R0-02 권한/role, R6-02 독립 AI가 불확실성을 유지하는지 연결한다.

**기록·완료·인계.** 업무 이력은 사람 결정/위임 변경/확인 요청을 actor·event·scope·revision과 기록한다. 진단에 private prompt/transcript는 담지 않으며 판단의 참조 ref와 선택 결과만 둔다. 시험 evidence에는 현재 사용자 기준/위임 ref, 모델 제안 원문이 아니라 실제 적용/비적용 state, 관련 run status와 proof가 포함된다. 완료는 대전제 변경에 필요한 주체의 decision event가 실제로 존재하거나 판단 보류 사유가 명확한 경우다. S4에는 allowed action 및 required human choice를 따로 보낸다.

### R3-S4 — 필요한 범위만 계획·문서·graph·근거 상태에 반영

**목적·이유.** 검토한 관련 범위만 수정하고 재사용 가능한 근거와 무관한 가지를 그대로 보존한다.

**범위·goal.** 추가: 반영 대상별 변경 목록과 immutable `AlignmentReceipt`, before/after refs·revision·부분 범위, 문서 게시/effect ref. 수정: 계획·요구·graph/문서 구간·Step 지시·검증 기준의 선택된 부분. 삭제: 반영 승인에 의해 명시적으로 폐기된 active mapping만 retire하고 과거 receipt는 보존. 금지: 광범위 자동 rewrite, P3 F3가 확인하지 않은 graph relation 생성, 사용자 대전제 미승인 수정, 무관 branch/evidence 재작성·삭제, 편집 레시피식 기계 적용.

**입력·출력.** 입력은 current non-stale assessment hash, explicit applied decision/delegation ref, affected refs·unaffected refs·unknown, current owner/workspace/source/basis/expected document/graph/plan revisions, active run state다. 출력 receipt는 대상별 원본 ref·before hash/revision·선택/승인·after hash/revision·게시/DB outcome, 남은 unknown, preserved unrelated refs, evidence applicability 갱신 ref를 담는다. 실제 변경 0건도 성공 적용으로 가장하지 않고 analyzed/unchanged 상태로 반환한다.

**방법·연계.** 논리 포트 `ApplyAlignment`는 `AssessAlignment`/`ProposeSemanticResolution`의 실제 ref/hash와 검토 근거를 받는다. 의미 선택은 S3 후 결정된다. `MetadataAccess`나 공유 receipt 조회는 apply 권한이 아니다. 실제 graph/doc/Step/plan source를 게시하거나 업무 SQL pointer를 반영하기 직전에 WorkAccess, current auth, expected source hash와 expected SQL/plan/pointer revision을 다시 검증한다. graph 영향은 F1 `preview_graph_change`의 검증된 ChangeSet/SourcePin으로 F3에서 사전 계산할 수 있다. graph 게시에는 별도 F1 `apply_graph_change` 실제 receipt가 필요하고, 그 receipt가 확정된 뒤에만 동일 F3 impact와 F1 receipt를 묶어 `efficiency.documents`의 segment manifest·generated/manual 구분·target hash CAS·stage/publish/recover를 진행한다. 미적용 preview로 document publish 또는 applied 기준을 확정하지 않는다. HostedFiles는 client effect/journal/Host resource 경계와 연결한다. generic prose/decision/Step은 기존 resource/version/owner-controlled API로 부분 반영하고 PMT 문서 원본을 임의로 직접 덮지 않는다. 새로운 Host operation·schema/public dispatcher는 R0 확정 전 계약화하지 않는다.

**선행·소비자.** S1/S2/S3 완료 및 current owner/source/basis, R0 게시/replay/CAS 포트와 권한, R1 checkpoint가 선행한다. 영향 후보 계산에는 검증된 F1 preview와 이에 고정된 F3 typed impact가 필요하다. graph 변경을 실제 적용할 경우 F1 apply receipt는 그 뒤의 document segment publish 및 반영 확정에 필요하며, F1 receipt가 없는 preview는 실제 게시/적용 근거가 될 수 없다. R4/R5는 반영 receipt만 최신 계획으로 투영한다. R6는 실제 local/Host publication parity를 확인한다.

**전후 기준·CAS·중단·복구.** 시작 전에 old assessment basis와 각 target hash/revision을 고정한다. 적용 직전 원 source, 사용자 결정/위임, run status, current workspace owner, graph/문서 target hash 및 SQL/plan/pointer expected revision을 current WorkAccess 아래 재검증한다. 파일 publication과 DB/Host metadata를 단일 원자 transaction으로 주장하지 않고 단계별 journal/effect/source/resource hash로 연결한다. alignment 또는 checkpoint 반영 journal이 미완료/충돌 상태이면 evidence와 원본 refs를 보존하며 backup/export/import/retention에서도 해결 전 삭제하지 않는다. target conflict 시 원본·candidate·manual span을 보존하고 retry 전 actual target을 읽는다. 일부 게시 성공 후 failure면 관련 효과만 recover, receipt의 불완전 단계를 유지한다. latest `applied_basis` CAS가 성공하기 전 applied pointer를 바꾸지 않는다. old effect 응답 유실은 request/effect ID 및 원 파일/DB state를 조회하고 중복 게시를 피한다.

**검증·실제 대조.** P4-R3-01은 승인/위임 scope 내 수정과 대전제 선택 보류/확정 후 실제 문서·graph/Step bytes/hash 및 decision source를 대조한다. P4-R3-02는 applicable/unrelated 근거가 그대로 유지되고 invalid/unknown 근거만 상태 metadata로 설명되는지 prior/current F6/P2 records·evidence resources로 대조한다. P4-R3-03은 crash at stage/publish/Host ack loss·target concurrent edit·새 source·활성 run/late result를 주입하고 SQLite journals/effects, Git/file hashes, Host current effect revision/resource와 old/candidate/unrelated contents를 비교한다. old result가 new plan 완료나 applied pointer로 쓰이지 않아야 한다. 관련 P4-R0-01 schema/호환·R0-03 CAS/replay·R6-01 다른 장치 metadata 경계·R6-02 통합 source 및 실제 fresh-session 인식을 연결한다.

**기록·완료·인계.** 업무 이력은 선택된 방향/계획 반영과 실물 effect 완료를 구분한다. 진단은 assessment/basis/decision refs, target hash, CAS/effect/journal phase, failure reason 및 actual outcome을 남긴다. test manifest는 Git status/HEAD, before/current/candidate file bytes hash, SQLite pointer/decision/journal/effect rows, Host resource/effect revision을 포함한다. 완료는 각 target의 물리 after-state와 DB/Host receipt가 다시 읽혀 일치하며 applied basis CAS가 성공한 경우뿐이다. partial/unknown/recovery-needed를 S5에 넘기고 이전 확정 pointer는 보존한다.

### R3-S5 — 기준 확정·늦은 결과 조정·R4/R5 인계

**목적·이유.** 정렬 반영을 현재 기준에 확정하는 마지막 경계에서 혼합 source와 미확정 효과가 다음 session의 방향으로 퍼지지 않도록 한다.

**범위·goal.** 추가: final coherence check, applied pointer CAS receipt, unresolved effect/run link, next action precondition. 수정: R1 checkpoint와 R3 applied basis 연결 및 기존 reconciliation/F6/Host effect observation을 소비. 삭제: 완료 증거가 확인된 임시 stage만 journal 정책으로 정리. 금지: 기준만 advance하고 원본 미확인, 시간 경과로 run 점유 해제/완료, old result로 신규 계획 완료, Host offline receipt만으로 반영 확정.

**입력·출력.** 입력은 R3-S4 대상별 receipts, 최신 source/decision/evidence fingerprints, DB 업무 revision/owner/active run/pending, Host effect revision/request result 및 unresolved local journal. 출력은 `applied`, `reconciliation_required`, `incomplete`, `awaiting_user` 중 의미 상태, 확정 checkpoint/pointer와 old/new revision 또는 유지한 old pointer, late result 분류, 후속 action·unknown·사용자 선택 조건이다.

**방법·연계.** 논리 포트 `ReadApplicability`로 기존 근거의 현재 적용 조건을 확인하고 `ReadChangeSlice`로 change/detail ref를 접근한다. 기존 request replay, journal/effect recovery, query/Queue/control/run state, `verify_source_pin`, F6/P2 evidence recheck와 R1 pointer CAS를 통해 상태를 조정한다. metadata pointer/checkpoint를 읽을 MetadataAccess와 실제 Git/source/evidence/SQL 변경에 필요한 WorkAccess를 분리한다. Git/SQLite/Host capture는 component별 시간/ref로 기록하며 원자 snapshot으로 포장하지 않는다. 조건 맞는 prior applicable evidence는 refs로 재사용하되 해당 시험을 새 pass event로 중복 기록하지 않는다. unresolved alignment/checkpoint/effect journal과 이를 복구하는 데 필요한 retained source/evidence refs는 완료 확인 전 보존한다.

**선행·소비자.** R0 event/replay/CAS, R1 checkpoint, R2 actual receipt, R3-S1~S4. R4 overview/bundle과 R5 경계 hook이 소비한다. R6가 양 환경·중단 복구를 통합 확인한다.

**전후 기준·CAS·중단·복구.** final capture에서 source/decision/evidence/DB/Host revision이 시작 지문과 다르면 혼합 결과 승격 금지. pointer CAS에 실패하면 old pointer 및 new assessment/effect receipts를 보존하고 current head에 대해 재분석한다. 늦은 결과는 originating run/instruction basis에 기록하고 현재 target/pointer와 별도 비교한다. request replay는 같은 effect ID를 조회하고 second execution/decision event를 만들지 않는다. journal의 actual outcome이 ambiguous면 `reconciliation_required`로 남기고 effect 조회 전 재게시하지 않는다.

**검증·실제 대조.** P4-R3-03 통합에서는 commit/file/resource/SQLite revisions/owner/Host effect/request result를 시나리오별 전후 비교한다. 새 source가 생기거나 owner가 바뀌면 stale assessment와 old basis pointer 유지, 작업자에게 재조회가 제시되는지 확인한다. 진행 중 run/늦은 결과는 실제 handle·state를 조회해 보존한다. 관련 P4-R0-03(다중 process CAS/응답 유실), R6-01(두 device/branch/offline), R6-02(fresh session/final source), R6-03(total read/review/rework 비용 측정)을 연결한다. 실행 결과는 actual SQL/files/Git/resource 및 독립 세션의 응답으로 각각 검증한다.

**기록·완료·인계.** 업무 event는 확정 pointer, 정렬 receipt, 확인 요청을 구분한다. 진단은 coherence pass/fail·old/new revision·effect/request correlation·late outcome·reason을 기록한다. 증거는 canonical applied basis, Git/파일/resource/DB/Host 관찰 및 최신 소유권 결과와 전체 시나리오 manifest다. 완료는 실제 상태가 일치하고 outstanding journal·run·unknown이 표면화되며 R4가 무관 가지·유효 근거·미확정 실행을 유지하는 것이다. 전체 P4 수용 또는 user PMT 완료는 메인이 별도로 판정한다.

## 21개 시험 ID에서 R2/R3 실행 단계로 추적

| 필수 ID | 주 단계 | 분할해 입증할 실제 사실 |
|---|---|---|
| P4-R0-01 | R2-S4, R3-S4 | 구현 착수 전 기존 schema/API 기준, 구현 뒤 actual version/migration/backup/error/envelope와 기존 ID·원본 보존 |
| P4-R0-02 | R2-S1/S3, R3-S1/S3 | 명시/누락 scope, foreign identity/role/branch, stale revision과 caller 주장 아래 권한 없는 Git/file/DB read·checkpoint 승격 없음 |
| P4-R0-03 | R2-S4, R3-S4/S5 | 같은 request replay, 다른 event 구별, 실제 별도 process pointer CAS·부분 파일/DB/Host 실패 복구 |
| P4-R1-01 | R3-S1/S2/S3 | 예정/구현존재/실제 검증 pass-fail-not_run 및 현재/폐기 decision 분리 |
| P4-R1-02 | R2-S4, R3-S5 | 마지막 확정 ref 재조회, Hook/replay에도 의도 추정·중복 checkpoint 없음 |
| P4-R1-03 | R2-S1, R3-S5 | source/업무 revision capture 중 변동 때 incomplete와 old pointer 유지 |
| P4-R2-01 | R2-S1/S2/S3 | 실제 Git/doc/event 변화와 dirty hash; 동일 basis 반복 분석 생략 |
| P4-R2-02 | R2-S1/S2/S4 | owner dirty·rename/delete·branch divergence·non-Git의 실제 bytes/refs/owner/기준 보호 |
| P4-R2-03 | R2-S2/S3/S4 | missing mapping·dynamic/unsupported code·외부 claim·중간 변화의 unknown 및 거부 |
| P4-R3-01 | R3-S1/S3/S4 | delegated method 조정 대 premise/user decision 선택을 decision/delegation 원본으로 구분 |
| P4-R3-02 | R3-S1/S2/S4 | 실제 selector와 정의 조건에 대한 적용성, 필요한 시험만 선별, 기존 pass/무관 근거 보존 |
| P4-R3-03 | R3-S1/S4/S5 | crash/ack loss/new source/active run/late result에서 CAS·effect·old/무관 가지 복구 |
| P4-R4-01 | R3-S2/S5 | mandatory evidence·unknown refs 부족 시 incomplete 인계; budget 축소가 평가된 필수 근거를 숨기지 않음 |
| P4-R4-02 | R3-S2/S5 | 이전 alias/cache·권한 폐기·retention 후 현재 basis/evidence actual recheck |
| P4-R4-03 | R3-S3/S5 | run/pending/review/변경 조합에 따른 안전 action 및 no duplicate execution |
| P4-R5-01 | R3-S5 | 명시 project/work와 제한 receipt 전달, 미지정 scope 자동 선택 없음 |
| P4-R5-02 | R3-S5 | 중복/역순/누락 callback 뒤 receipt recovery; Stop/idle 완료 금지 |
| P4-R5-03 | R2-S1/S3, R3-S3/S4 | 실제 prompt/env/path sentinel이 metadata/log에 없고 업무 failure·진단 sink failure 구분 |
| P4-R6-01 | R2-S4, R3-S4/S5 | 다른 경로/device/branch에서 local client evidence와 Host metadata/auth/CAS/recovery 경계 |
| P4-R6-02 | R3-S5 | 다른 fresh native context가 source/intent/evidence 수준을 올바르게 판단하고 수행 상태는 별도 actual evidence |
| P4-R6-03 | R3-S2/S5 | 동일 정의·조건의 실제 reads/generation/reuse/review/rework 비용과 unknown 포함 비교 |

R0/R1/R4/R5/R6 담당자는 해당 ID의 전체 scenario owner다. 여기서 매핑한 ID를 R2/R3 단독 완료로 처리하지 않는다. 실제 시험자만 명령/action·종료 코드·환경·source hash·SQL/Git/files/evidence·pass/fail/blocked/not_run/skip/이후 무효를 manifest에 기록한다. 모델 답변은 의미 이해 시험의 한 결과이며 실제 source 적용·owner·파일/DB/Host 상태를 대신하지 않는다.

## 완료 판정과 인계 묶음

R2 인계 묶음은 (1) source-bound change receipt 및 수집 scope/completeness, (2) client에서 확인한 owner와 dirty fingerprint, (3) graph와 별개인 implementation link index version/coverage, (4) document/decision refs 및 intent known/unknown, (5) 현재 확정/관찰/분석/반영 basis의 각각의 revision, (6) Git/SQL/files 실제 검증 manifest다. 수집 자체는 baseline 적용이나 user intent 승인으로 보고하지 않는다.

R3 인계 묶음은 (1) 원 observed change와 mapping coverage, (2) impact/evidence assessment hash 및 관련/무관/unknown 경로, (3) F6 selector와 P2 evidence 적용성 ref, (4) 적용에 쓰인 실제 위임/사용자 decision source, (5) 변경 target별 before/after·journal/effect/owner·CAS receipt, (6) 미확정 run/late result/unknown 및 복구 action이다. 반영 완료는 target 물리 상태, 저장 receipt, applied pointer, 최신 source 및 권한이 다시 일치할 때만 주장한다.

이 문서는 기존 구현과 `CollectChanges`·`BuildImplementationLinks`·`ReadChangeSlice`·`RegisterObservedChange`·`AssessAlignment`·`ProposeSemanticResolution`·`ApplyAlignment`·`ReadApplicability` 논리 포트를 연결한다. 이 이름은 현재 제공 API가 아니며 실제 타입·저장/schema/public interface/version은 [공통 구현 인터페이스 문서](implementation-interfaces.md)의 R0 관문에서 확정한다. SourcePin의 graph 중심 구조를 BasisVector의 선택 source inventory/ref/hash/coverage와 연결하고 그 내부 `ScopeFingerprint` 표현을 확정하는 안을 주 제안으로 전달한다. 파일별 수정 레시피, 실제 PMT status 변경, code implementation, 테스트 실행, commit/push는 이 산출물의 범위가 아니다.
