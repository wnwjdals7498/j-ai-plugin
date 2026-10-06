# 4단계 세션 재개 절차

Core 0.4.0 / DB 5 / graph 1 / protocol 1. 기본 호출 형식은 [CLI 절차](cli-workflow.md), hosted 설정·인증·실행 경계는 [Host 절차](host-workflow.md)를 따른다. 이 참조는 설치 패키지 안에 포함된다.

모든 요청에 현재 `actor`, `session_id`, 명시 project `scope_id`, UUID `request_id`를 전달한다. 같은 논리 요청 재전송은 같은 ID/본문을 유지하고 새 행동은 새 ID를 쓴다. 반환된 실제 refs·hash·revision을 이어 사용한다. 조회 결과의 현재성·coverage·unknown·`complete`를 확인하며 모델의 완료 주장이나 caller 승인 Boolean을 근거로 삼지 않는다.

## 먼저 읽는 개요

`compose_resume_overview`의 선택 입력은 `task_id`, `selector`, `role`, `budget`이다. selector는 `purpose`와 선택한 repository/branch/workspace/task/environment dimension이며 잘 모르는 dimension은 추정하지 않는다. 작업/source를 선택하지 않은 기본 개요는 계획 문맥이고 source 현재성은 unknown이다. `read_current_facts`는 명시 scope의 현재 records/runs/claims/pending·결정·참조를 읽는다. 실제 보존 basis를 `basis_ref` 또는 명확한 current checkpoint로 선택해야 해당 basis의 구현/검증 관찰을 연결한다.

기본 budget은 UTF-8 16384 bytes/160줄이다. `budget.max_bytes`는 256~262144, `max_lines`는 8~2000, unit은 `utf8`이다. 응답 전체가 제한을 넘거나 필수 내용이 부족하면 incomplete/추가 조회이며 실행 준비 성공이 아니다. token usage는 제공된 실측값이 없으면 unknown이다.

기존 run/pending이 있으면 actual `read_execution`/원 요청 결과/current owner부터 확인한다. 모든 next-action은 조건부 제안이고 `executable=false`다. 요약·checkpoint·session link는 claim·소유권 이전·실행 완료를 만들지 않는다.

## 점유 후 source와 작업 문맥

| operation | 핵심 입력 의미 | 반환/소비 조건 |
|---|---|---|
| `capture_work_basis` | 실제 own `run_id`, `repository_id`, mapped `workspace`, `relative_graph_path`, 선택 `inventory_paths`, 선택 `task_id/condition_refs` | 현재 source/계약/DB revision·coverage의 immutable `basis_ref/hash`. 실제 run과 각 path claim 필요 |
| `validate_basis` | `basis_ref`와 같은 source/run/graph/inventory 선택 | actual recapture 후 unchanged/changed/unknown. ref가 있다는 이유로 현재성을 인정하지 않음 |
| `collect_changes` | own `run_id`, 명시 `paths`, `before_basis_ref`, `after_basis_ref`, `expected_pointer_revision` | raw Git/dirty 관찰의 change ref·hash·coverage와 private detail. before는 출발점, after는 현재 기준 |
| `build_implementation_links` | 현재 `basis_ref`, run/선택 paths, 명시 mappings와 실제 decision refs | 같은 basis의 code/graph index. Python 최상위 선언·명시 refs만 처리; dynamic/미지원은 unknown |
| `read_change_slice` | `change_ref`, budget, 선택 offset; private detail이면 own run/paths + `include_detail` | shared 요약 또는 현재 owner의 bounded local 상세. 전체 diff를 개요/로그로 옮기지 않음 |
| `assess_alignment` | `change_ref`, current basis/index refs, own run/paths, 선택 actual `typed_graph_preview` | 영향·대전제/위임·unknown·필요 검토. raw Git delta 자체를 typed graph 변경으로 보지 않음 |
| `read_applicability` | 현재 basis/run/scope와 기존 F6 정의·P2 target/criteria/evidence 선택 | applicable/불가/unknown 별도 참조. 기존 verification outcome/state를 다시 쓰지 않음 |
| `propose_semantic_resolution` | `assessment_ref`, `resolution_kind=method/premise/unclear`, 필요한 actual `decision_ref` | delegated 후보/실제 결정 참조/awaiting_user. caller approval은 승인 아님 |
| `apply_alignment` | actual assessment/resolution·own run/source/paths·완료 F1 graph/F3 document effect refs·선택 Step 영수증·기대 pointer revision | readback·결정/event·현재 source·quiescence 후 alignment receipt/CAS. 실행 중/미확정 효과는 먼저 조정 |
| `compose_task_resume` | current `basis_ref`, own run, repository/workspace/graph, 현재 F5 `task_ref`·requirements/plan/directive versions·role/budget, actual change/assessment/applicability/resolution refs | 기존 F5를 사용하는 private `bundle_ref`. stale/미반영/unknown은 incomplete; source pin이 바뀌면 F5 graph index부터 갱신 |
| `read_resume_detail` | `bundle_ref`, 같은 source/run/workspace/graph, 선택 cursor·예산 | 현재 basis/owner/변경 근거를 다시 확인한 실제 F5 상세. 예전 cursor/ref는 권한 아님 |

위 표는 필드 의미 요약이다. 기존 F1/F3/F5/F6 계약의 required versions/source/budget을 생략하지 않는다. 요청 불일치는 현재 사실을 재조회해 해결하고 원 요청 ID/본문을 바꾸어 replay conflict를 우회하지 않는다.

Hosted 모드에서는 같은 논리 operation을 client composite가 수행한다. 현재 Host 인증/grant → mapped client Git/파일 관찰 → shared refs/hash 게시 순서다. 서버에서 Git/모델/프로세스를 실행하거나 local SQLite로 fallback하지 않는다. private inventory/diff는 client spool에 둔다. server 확인을 거친 `read_decision_receipt`와 실제 완료 효과의 `apply_alignment_receipt`를 사용하며 `host_git_verified=false`와 client provenance를 유지한다.

## 확정 사건·보존

`create_checkpoint`는 실제 저장된 `boundary_event_id`, current `basis_ref`, `purpose`, nonnegative `expected_pointer_revision`을 사용한다. planning/direction checkpoint는 source/run 없는 DB 기준을 남길 수 있으나 incomplete/unknown이다. 실제 적용·검토·결정 등 현재 사건과 기준을 대조한 뒤 확정한다. `read_checkpoint`는 `object_ref` 또는 exact selector로 읽는다. `link_session`은 `checkpoint_ref`에 현재 actor/session metadata를 연결하며 선택 `event_id`로 중복을 구별한다. event_id를 실제 lifecycle 사건으로 검증하는 기능은 아니며 작업 owner를 이전하지 않는다.

SessionStart 자동 개요는 명시 `PMT_SCOPE_ID`와 현재 profile을 사용한다. Stop/idle/SessionEnd는 관찰이며 Done/unlock/checkpoint를 추정하지 않는다. Hook 오류/timeout은 안전한 경고·추가 조회 대상이다.

`prune_continuity`는 기본 dry-run이다. 명시 `dry_run=false`만 90일이 지난 미참조 metadata를 정리한다. 현재 pointer 근거·활성 작업/실행·미완료 효과·다른 actor private 자료를 보호하며 손상된 참조/hash면 중단한다. 만료된 optional 과거 checkpoint는 unavailable이고 현재 원본/소유권/실행을 삭제하지 않는다.
