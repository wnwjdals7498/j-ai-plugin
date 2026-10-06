# R2/R3 구현·검증 증거

2026-10-06. B 작업자가 실행한 로컬 격리 시험 기록이다. 사용자 데이터·외부 Host·모델 API는 사용하지 않았다.

## 구현 연결

- `collect_changes`는 현재 실행 소유권과 선택 경로 claim, 설정된 repository/branch/workspace mapping, before·after BasisVector를 확인한 뒤 Git commit/working-tree 차이를 관찰한다. `before_basis_ref`는 변경 전 경계, `after_basis_ref/hash`는 재수집한 현재 source/work 경계다. raw diff와 상대경로 상세는 같은 actor/session의 private `detail` 객체에만 두며, 공유 `change`에는 path hash·내용 hash·coverage·basis ref만 저장한다. 분석·반영 pointer는 바꾸지 않는다. 수집 중 HEAD/status/bytes 변화는 `incomplete`와 미확정 effect로 남긴다.
- `build_implementation_links`는 실제 선택 범위 파일과 graph를 읽고, Python 최상위 함수/class 선언만 후보로 추출한다. dynamic/unsupported/parse 실패는 unknown이다. 확정 mapping은 현행 decision record와 `decision_saved` event를 대조한다. caller의 `reviewed_by`/`verified_mapping_ref` 값은 권한으로 쓰지 않는다.
- `read_change_slice`는 metadata를 bounded 응답으로 제공하고, detail 요청은 현재 run의 실제 path claim과 private owner를 확인한다. diff와 file 목록은 별도 offset으로 나눈다.
- `assess_alignment`는 observed change의 `after_basis_ref/hash`, 그 기준으로 만든 link index, fresh graph, 실제 delegation record를 연결한다. 선택된 F1 typed preview가 있을 때만 F1 preview를 현재 graph/ChangeSet과 대조하고 F3 impact를 계산한다. raw Git change 자체는 typed graph delta로 승격하지 않는다.
- `read_applicability`는 기존 F6 selector·P2 verification/evidence 검사를 실행하고 별도 applicability object로 보존한다. P2 outcome/state를 바꾸지 않는다. 기존 snapshot 구현이 workspace 전체를 읽으므로 현재 전체 workspace claim을 요구한다.
- `apply_alignment`는 실제 완료된 F1 graph journal, F3 document publication journal, 현행 delegation 또는 사용자 decision, current owner/source와 실제 target hash를 다시 확인한다. 같은 workspace의 다른 active run, 관련 현재 Step 실행, 미해결 effect가 있으면 대기한다. 검증 후 alignment receipt와 applied pointer를 CAS로 기록한다. 선택적인 `step_effect_refs`는 실제 완료된 `save_step_directive` request, 현재 Step revision, event, resource hash를 확인한다.

실제 작업 경계는 [변경 수집·정렬 계획](plan-change-alignment.md), API 유형과 권한은 [런타임 계약](runtime-contract.md)을 따른다.

## B가 실행한 시험

격리 명령:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_phase4_changes_core.py tests/test_phase4_r2_r3_actual.py -q --basetemp=.pmt-test/p4-b-local-final2
```

최신 로컬 결과: **19 passed**, exit 0, 105.14초. 포함한 실제 상태 확인은 다음과 같다.

- 실제 임시 Git 저장소의 dirty 변경, path/content fingerprint, immutable receipt, observed pointer CAS, 동일 request 재생 시 pointer revision 불변.
- capture 사이 파일이 바뀌면 `incomplete`; actual pointer는 revision 0에 유지되고 effect는 `partial`이다.
- 실제 commit의 rename/delete와 파일 전후 상태; 수집기는 원본 파일을 수정하지 않았다.
- 사용자 결정 record/event로 승인한 명시 mapping은 `verified_mapping`; caller reviewer flag 또는 존재하지 않는 decision ref는 승격되지 않는다.
- current F6 selector와 실제 P2 passing verification/evidence를 대조해 `applicable`을 기록하고, 원 P2 row를 `pass/valid`로 보존한다.
- 실제 F1 SourcePin→문서 baseline→F1 typed preview→F3 impact→alignment assessment→현재 위임 기반 방법 제안→F1 graph 적용→F3 문서 prepare/publish→물리 hash readback→alignment receipt/applied pointer CAS 전체 흐름.
- 실제 user decision이 없는 premise 제안은 caller의 `approved=true`에도 `awaiting_user`; 같은 workspace에 다른 active run이 있으면 pointer를 옮기지 않는다. 동일 apply request 재생은 기존 응답을 반환하고 pointer를 다시 CAS하지 않는다.
- bounded raw name-status parser, unusual/non-ASCII path 처리, path traversal 거부, Python 제한 parser의 dynamic/unsupported unknown, source-scope-bound index hash.

메인 담당 공통시험도 별도로 실행했다.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_phase4_foundation.py --basetemp=.pmt-test/p4-b
```

결과는 **7 passed**다. 그 외 전체 PMT 회귀, 실제 Host, 새 제품 session, 두 device/branch 통합 및 독립 AI 판단은 이 시험의 결과가 아니다.

격리 loopback HTTPS Hosted R2 시험:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_phase4_hosted_changes.py -q --basetemp=.pmt-test/p4-b-hosted-final
```

결과: **1 passed**, exit 0, 29.73초. actual TLS Host 기준 수집·원본 request replay·stale CAS 거부·implementation index 저장/읽기와 path 비노출·현재 owner 아래 private diff 조회를 확인했다. C의 route/기존 Hosted CLI smoke는 별도 **6 passed**다.

전체-workspace Hosted applicability/P2 보존 실제 TLS 확인(격리 임시 runner):

```powershell
.\.venv\Scripts\python.exe -m pytest .pmt-test/test_p4_host_applicability_proof.py -q --basetemp=.pmt-test/p4-host-app-proof-run2
```

결과: **1 passed**, exit 0, 9.76초. 현재 root-scope claim으로 `read_applicability(paths=['.'])`을 호출해 actual `applicable` object를 읽었고, Host P2 `record_verification`의 원 verification ID/outcome/state/input fingerprint가 호출 전후 동일한 `pass/valid`임을 확인했다. 이 proof는 통합 중 production freeze 요구에 따라 ignored `.pmt-test` runner에서 실행했으며 tracked pytest 추가는 하지 않았다.

격리 Host source/readback 식별자는 project `99f512e1-87cc-4eae-90a0-2e682e212392`, repository `d81b26a9-f952-4710-a668-cbcf081ed8a9`, branch `main`, HEAD `e83cf348ad4b31f1c2c923f22e548e111c468835`, graph hash `93c40f2be9855a0480348422e29ed1b700b721bdc94ae8c701eca4a9864c0242`, SourcePin hash `6e2a5ee88f2ab250ba2059b7c1e4b867f1028eba92bb0e83415f948cefbc3457`다. `publish_verification_snapshot` request `cd9a1fab-59ca-4f13-887f-ac7d714ce2ba`와 `record_verification` request `b4fb30dd-ac21-4451-96ea-6ec38a090248`로 actual P2 row `41c68d40-31cd-4dd3-8921-ff150b39d59d`를 만들었다. 전후 SQL readback은 동일하게 outcome `pass`, state `valid`, input fingerprint `dc076494f410d9d668eedb78acf4ee6354c2082b8656fce1aac107f90f783e99`였다. F6 `resolve_reuse` request `148a17cf-7d0a-5a5f-b7cb-b3dcf449100e`가 재사용 가능성을 반환했고 Phase 4 applicability object `411f8cdb-21a7-5ee3-a619-8b376f2c578d` / body hash `57c25f22b9a81d7ed1584600f578dbd73a9e20dc386ccf0de27984de79db559d`가 `applicable` 및 동일 verification ref를 보존했다.

이전 combined run 한 번은 rename fixture의 임시 Git `.git/config` 쓰기가 Windows `Permission denied`로 실패했다. 새 basetemp의 해당 단독 재시도는 **1 passed**였고, 뒤이어 격리한 전체 local R2/R3 19개 시험도 통과했다. 실패는 업무 코드 실행 전 fixture Git 설정에서 발생했다.

이후 변경에 대한 focused actual 회귀:

- `reviewed_method_alignment_applies_actual_graph_and_document_effects`는 설정된 `spec/current-project-graph.json`을 사용해 **1 passed**, exit 0, 84.29초. R2 link/assessment, F1 graph apply, F3 publication, actual receipt와 pointer까지 같은 비표준 graph mapping을 유지했다.
- branch switch·foreign session owner·non-Git 사례는 **3 passed**, exit 0, 12.76초. branch/profile 충돌과 non-owner는 읽기/포인터 쓰기 전에 거부됐고 non-Git은 `unknown` 및 pointer rev 0으로 남았다.
- 같은 `main` 이름에 parent 없는 root commit을 만든 실제 history divergence는 `history_diverged`/pointer rev 0을 반환해 **1 passed**, exit 0, 4.73초.
- Git root 아래 `monorepo/packages/component`에 mapped workspace를 둔 actual Git fixture는 root-prefixed name-status paths를 `src/feature.py`로 정규화해 inventory/content hash와 verified link path ref를 일치시켜 **1 passed**, exit 0, 10.99초.
- capture 중 파일 변동 뒤 같은 request를 재생하는 partial journal 사례는 원 `incomplete` 응답을 돌려주고 pointer rev 0을 유지해 **1 passed**, exit 0, 14.36초.
- Hosted R3 delegated-method full TLS flow는 Host가 확인한 `decision_saved` event ref, 실제 F1/F3 effects, typed receipt/applied pointer, physical post-readback과 exact request replay까지 **1 passed**(Root/C가 검증한 64.70초)다.

B source-freeze 회귀 묶음:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_phase4_changes_core.py tests/test_phase4_r2_r3_actual.py tests/test_phase4_hosted_changes.py tests/test_phase4_hosted_alignment_actual.py -q --basetemp=.pmt-test/p4-b-final-source-freeze
```

결과: **25 passed**, exit 0, 221.60초. 이는 B R2/R3 범위의 fixture/local Git·SQLite·loopback HTTPS 묶음이며 전체 PMT 회귀나 새 native session을 뜻하지 않는다.

## 미완료·제한

- 실제 local Git + SQLite/파일 환경과 격리 loopback HTTPS Hosted `collect_changes`/`build_implementation_links`/private detail 읽기/replay/stale CAS를 검증했다. Hosted adapter는 Host 메타데이터와 typed receipt만 쓰며 Host는 Git이나 local private detail을 읽지 않는다. Hosted R3 전체 actual positive도 별도 loopback TLS 시험을 통과했다.
- implementation link extractor는 Python 선언과 명시 graph refs만 다룬다. 다른 언어, runtime dispatch, 생성 코드, 자동 영향 밖의 관련성은 unknown이다.
- source detail 보존은 256 KiB까지의 private patch와 상대경로/hash이며, 더 크거나 Git이 아닌 상세는 `incomplete/unknown` ref로 남는다.
- graph와 generated document effect를 완료 영수증으로 연결했다. 임의 prose/제품 데이터나 선택되지 않은 Step directive의 자동 생성·반영은 하지 않는다. 실제 Step 지시 변경이 필요하면 기존 `save_step_directive` 실제 effect를 명시 제공해야 한다.
- Hosted의 실제 method 위임 경로는 Host가 확인한 현재 decision record와 정확한 `decision_saved` event ref를 사용해 전체 F1/F3 effect를 alignment receipt와 applied pointer까지 연결했다. Hosted premise 변경에 대한 사용자의 새 결정 후보 수집은 아직 `awaiting_user`로 제한한다.
- 외부 운영 Host, 새 native AI session 및 full PMT regression은 이 B 시험에 포함되지 않았다.
