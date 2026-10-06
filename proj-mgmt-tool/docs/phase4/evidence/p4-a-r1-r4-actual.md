# P4 A/R1·R4 실제 구현·검증 증거

2026-10-06 UTC. 이 기록은 작업자 A의 격리 로컬 시험 결과다. 전체 4단계·Host·제품 수용 판정은 포함하지 않는다.

## 대상과 실행

- 시험 정의: `p4-a-r1-r4-1` — `tests/test_phase4_current_context.py`의 R1/R4 순수 규칙, 실제 SQLite/Git/파일, 실제 R2/R4 연결 사례.
- 기준 HEAD: `ac7adedc1e7d2464e0023e70a01515c8d70cb878`; 소스는 이 commit 이후 수정 중인 공유 작업 디렉터리.
- 명령: `.venv/Scripts/python.exe -m pytest tests/test_phase4_current_context.py --basetemp=.pmt-test/p4-a-r1r4-facts-final -q --tb=short`.
- 실제 결과: **exit 0, 12 passed, 138.78s**. 추가로 current checkpoint pointer/unchanged-basis 회귀와 R1 receipt 분류의 두 target test는 `.pmt-test/p4-a-r1-cp-final`에서 **exit 0, 2 passed, 13.44s** (R1 facts test는 full suite와 중복 실행). 각 fixture는 격리 DB/Git/config/runner 경계를 사용했다.
- 회귀: 다음 기존 F5 테스트 **exit 0, 3 passed, 12.23s** — `test_f5_s2_small_budget_omits_whole_required_sections_and_offers_only_saved_detail`, `test_p3_f5_01_detail_cursor_and_alias_are_current_source_bound`, `test_p3_f5_02_claim_directive_and_source_changes_block_detail`.
- 구문 확인: `.venv/Scripts/python.exe -m compileall -q src/pmt/continuity` — **exit 0**.

## 실제로 확인한 내용

- `read_current_facts`는 격리 CLI/SQLite에서 project metadata를 읽고, private Step 지시를 반환하지 않는다. local pending spool은 조회하지 않아 `local_pending_spool_not_checked` unknown을 남긴다.
- `capture_work_basis`는 실제 현재 run과 workspace claim 아래 Git HEAD, graph 및 선택 파일 hash를 읽고, 업무 snapshot을 전후 비교한다. 파일 경로/hash 상세는 actor/session 소유 private `detail` object에 저장하고 shared BasisVector에는 canonical `workspace_ref`, hash, coverage, opaque detail ref를 둔다. 같은 request replay는 원 receipt를 반환한다.
- capture 직후 source bytes가 달라져도 같은 request ID는 원 basis receipt로 수렴한다. 그 receipt 자체를 현재 기준으로 자동 승격하지 않으며, `validate_basis`/R4는 새 source capture로 현재성을 확인한다.
- 실 Git/SQLite basis의 canonical workspace ref는 기존 `pmt.workspace.canonical_workspace` 결과와 같다. 실제 선택 graph 변경 후 새 after Basis를 수집했고, B `collect_changes`의 immutable change ref를 R3 implementation-link index/assessment 및 R4 bundle로 전달했다. R4는 current after Basis·change hash·assessment graph/work snapshot을 비교했다. applicability가 없고 현재 `applied_alignment` pointer가 없어 `complete=false`, 실행 불가 검토 action을 반환했다.
- `create_checkpoint`는 저장된 실제 `decision_saved` event, 현재 record revision, BasisVector와 pointer revision을 확인한다. 같은 event 재처리는 같은 checkpoint를 돌려주고, 새 boundary는 pointer revision을 전진시킨다. 이후 Work revision과 pointer가 바뀐 뒤 이전 event를 다시 보내도 이전 checkpoint를 돌려주며 최신 pointer를 유지한다.
- 실제 run/Step 없이 project direction과 저장된 decision event로 planning checkpoint를 만들 수 있다. 저장 결과는 `basis_complete=false`, `source_currentness=unknown`, source HEAD 없음으로 명시되고, project overview는 metadata direction과 미확인을 읽는다.
- `compose_task_resume`는 기존 F5 owner-bound task context 생성을 호출한다. private bundle에는 F5 context ref와 제한된 change/assessment/applicability/alignment 상태 요약을 저장한다. `read_resume_detail`은 owner/claim, retained inventory, 현 source/basis 및 요약에 연결된 evidence refs를 다시 확인하고 기존 F5 detail cursor 경계를 통과한다.
- NextAction 순수 규칙은 queued/starting/running/review/pending 경계를 `wait`/관찰 후보로 남기며 `executable=false`다. Basis currentness가 unknown이면 source 확인이 필요하다고 반환한다.
- `read_current_facts`와 `compose_resume_overview`의 `implementation` 요약은 같은 project에 실제 저장된 basis/link-index/change/assessment/P2 verification refs만 연결한다. `planned`, `implementation_observed_at_basis`, `verification_at_basis` 수준을 구분하며, metadata 조회는 source currentness와 applicability를 별도 미확인으로 둔다. R1 fixture는 격리 SQLite에 ready artifact와 valid/pass verification row를 직접 seed해 same-basis projection을 확인했다(독립 `record_verification` 서비스 호출은 이 시험에서 하지 않음). 후속 valid/fail row와 caller-forged ref/Boolean도 확인했다.
- R4 no-change checkpoint test는 actual `decision_saved` event에서 current pointer를 만든 뒤 source revalidation이 의미상 같은 basis를 선택하고 `no_change_confirmed`를 반환하는지 확인했다.
- R4 hosted applied-receipt positive는 B fixture의 actual Host `save_decision`/R3 assessment/resolution, F1 graph effect, F3 document publication, `apply_alignment_receipt`, post-readback 및 current pointer rev1을 사용한다. R4가 fresh client Basis/private inventory와 current-owner F1/F3 effect readback를 대조해 실제 applied receipt를 표시하고 detail cursor owner/source 검사를 통과했다. 해당 applicability ref에는 원래 P2 valid/pass receipt가 없어 Host 측 P2 pass/state 보존은 미확인이다.
- `compose_task_resume`가 현재 checkpoint pointer를 선택할 때 `purpose=current`의 canonical selector에서 environment dimension을 생략하는 실제 LocalStore/Git/SQLite 시험이 통과했다. same-basis flow는 `no_change_confirmed`를 반환하며 source detail을 불필요하게 재분석하지 않는다.

## 미확인 범위와 인계

- 이 R4 fixture는 related-change가 검토 미완료일 때 bundle과 next-action을 불완전/실행불가로 유지함을 확인했다. 실제 applicability 생성 및 alignment 반영 후 current pointer를 따라 `applied_current`가 되는 positive 경로는 아직 실행하지 않았다.
- capture 도중 실제 HEAD/file/record 변경을 주입한 시험, 별도 프로세스의 pointer CAS 경쟁, crash/response loss 복구는 전체 R1-03/R0-03 수용에 남아 있다.
- 새 native AI 세션의 bundle 이해, Host의 client-attested work basis publish, 실제 제품 hook, 전체 R6 백업/복구는 이 증거가 다루지 않는다.
- source-free planning checkpoint는 명시된 `planning`/`direction` purpose와 실제 DB-derived incomplete basis 모양만 허용한다. 실행/verified 권한을 부여하지 않는다.
- 다음 소비자: R2는 `basis.source.repository_id/branch/workspace_ref/observed_head/inventory_hash/inventory_coverage`를 사용한다. R4는 `read_current_facts`, checkpoint selector/pointer, owner-bound private bundle/detail ref를 사용한다. C/Host는 `checkpoint_boundary(db, conn, req, basis_body)`의 현재 승인된 helper를 공유한다.

검증한 소스 SHA-256:

| 파일 | SHA-256 |
|---|---|
| `src/pmt/continuity/current.py` | `F11469BB646B274D3D1756F4785754B2F289A7B1D22D6F949D3145BAF946B938` |
| `src/pmt/continuity/context.py` | `443048C7E73ED68ECED2BFF4781D19595C741F6D231CFD56748F645D71DB25E2` |
| `tests/test_phase4_current_context.py` | `62901B80CF25D7B8F8DAE1EE20773AA081582A41057D181D8B5F1C832E7F0AC1` |
