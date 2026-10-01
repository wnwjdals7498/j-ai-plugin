# 2단계 사용·재개

이 문서는 실제 구현된 operation과 의미를 연결한다. 요청은 [기존 protocol-v1](../phase1/contracts.md)의 envelope를 유지하고 동작별 정보는 `payload`에 둔다. 각 새 행동에 request ID를 발급하며 재전송에는 같은 ID·같은 입력을 사용한다. 모든 시험·실제 작업은 명시 data/config root로 구별한다.

## 초기 설정과 계획

1. `setup`: 로컬 DB·환경 ID·runtime 확인. 0.1.x schema 2는 schema 3으로 이관하며 변경 전 DB 백업을 남긴다.
2. `register_capabilities`: 실제 세션/공식 규약/제품 확인으로 얻은 agent/provider/model·native/cli 지원·능력·동시성·인증 상태·관찰 시각/근거 등록. API/SDK 실행은 현재 지원하지 않는다.
3. `save_routing_policy`(선택): 역할별 model/agent/provider와 모델 `priority` 목록·economy 설정. 기본 메인은 현재 세션이다.
4. `validate_plan_graph`·`save_plan_draft`: 메인이 만든 요구/구현 트리 구조를 검사·초안 저장. 작성 중 노드는 `complete=false`로 구조 검사하고 게시 시 완전한 두 트리·종료 조건을 검사한다.
5. `publish_project_docs`: 점유한 run과 workspace, plan ID·기존 파일의 관찰 hash를 전달한다. Git 프로젝트 문서·graph·AGENTS managed 참조를 생성한다. 기존 본문을 덮어쓰지 않는다.

## Step·배정·실행

| operation | 필요한 input의 의미 → output |
|---|---|
| `save_step_directive` | 부모 Item·확정 plan 또는 승인된 탐색·목적/경계/의미 I/O/방법/Test/로그·기준/범위/의존·버전 → Step ID·지시 ref/hash/버전 |
| `read_step` | Step ID → 상태·현재 metadata; 지시 원문 제외 |
| `set_task_metadata` | 현재 record revision·단계에 맞는 태그/분류·우선순위와 변경 이유 → 새 revision |
| `select_execution_route` | 역할·필요 능력·현재 agent·사용자 지정·미시작 상태 → 선택 경로/모델·이유·설정/capability 버전 또는 대기/차단 |
| `enqueue_execution` | Step·선택 route·정책 → job/run·시도·queued |
| `prepare_execution` | run ID·기대 run revision → 의존/용량/범위 점유 결과·starting intent 또는 대기 이유 |
| `sync_project_baseline` | 소유 run/workspace·기준 ref·변경 영향 확인 → 검토 기준·state pin 또는 메인 조정 요청 |
| `read_step_directive` | 소유 run·Step → 실행에 필요한 불변 지시. 점유/현재 버전 없으면 거부 |
| `dispatch_execution` | 소유 starting run → native 메인 호출 action 또는 고정 CLI 실행 handle |
| `attach_execution_handle` | 실제 native 호출 ID·현재 run revision → running. action 응답을 실제 실행으로 간주하지 않음 |
| `poll_execution` | 원 run → 관찰·저장 receipt·정제된 model report ref. 모델 자기 성공은 검증 통과와 구별 |
| `submit_execution_result` | pinned 지시 버전·실제 route·기준별 결과/증거·종료 근거 → 영속 결과·검토 대기/조정 |

workspace scope는 `kind=path/workspace/resource`, 절대 workspace와 상대 경로/논리 자원 의미를 가진다. `.`는 전체 작업 공간이다. 다중 범위는 전부 확보 또는 전부 실패이며 겹치는 범위는 대기한다. 내용 읽기/쓰기 전 점유 성공 응답을 받고, 추가 범위는 먼저 `extend_execution_scopes`로 확보한다.

신규 호출과 원 실행 조정을 구분한다. 이미 전달된 native action에 다른 request ID를 보내도 새 호출을 만들지 않는다. CLI의 `starting` 이후 답이 유실됐으면 원 supervisor/journal을 조회한다. 미확인 실행에는 새 runner를 배정하지 않는다.

## 검토·중단·운영

- 실측 시험 전에 `lookup_verification`으로 현재 대상/명령/입력의 지문을 잡는다. 시험 증거는 workspace 밖 PMT 리소스에 등록하고 `record_verification`의 before/after 조건을 확인한다. 모델 보고는 별도 내부 리소스다.
- `review_step`은 현재 Step·run·기대 record revision·유효 verification ID·실행 종료·통합 확인을 받는다. `review_execution`은 동일 실행 검토 경계이며 Step/Item/Work 목표 완료는 별도다. Item/Work는 현재 기준과 자식 종료를 확인한 뒤 명시적으로 완료한다.
- `request_execution_cancel` → 실제 native 도구 중단 또는 `cancel_runner` → `reconcile_execution` 종료/미시작 근거 → 필요 시 `cancel_step` 순으로 처리한다. 시간 경과만으로 끝내지 않는다.
- `read_execution/list_execution_queue`, `observe_progress/read_progress`, `diagnose_execution`으로 원 실행·대기·최근 관찰을 재개한다. 장기 실행은 60초 내 관찰하며 변화 없음을 진행률로 꾸미지 않는다.
- `configure_diagnostics`는 다음 invocation부터 사용자 data root의 파일 로그·회전·보존을 적용한다. `plan_retention` 후 `execute_retention`으로 만료 후보를 정리한다. 참조 중인 자료·미처리 결과는 보존하고 ID tombstone은 중복 방지에 남긴다.

완료된 모델 결과를 재사용해도 현재 목표·환경·입력·정의·증거 적용성은 확인한다. 대전제 변경은 `invalidate_plan_branch`로 영향 Step을 중지·재계획하고 구 반환은 보존한다. 자세한 메인 판단/전달 순서는 [플러그인 스킬](../../skills/proj-mgmt-tool/references/model-workflow.md)을 따른다.
