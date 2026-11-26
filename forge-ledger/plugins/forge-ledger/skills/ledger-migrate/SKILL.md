---
name: ledger-migrate
description: PMT 프로젝트의 작업과 기록을 매니페스트로 검토한 뒤 forge-ledger의 Plane과 git으로 이전할 때 사용한다.
---

# ledger-migrate

PMT(proj-mgmt-tool)의 완료 기록까지 Plane과 저장소 `.ledger/`로 옮긴다. `slug`는 원본 프로젝트 이름이며, 사용자가 승인하기 전에는 적용하지 않는다. 적용 후 PMT 원본은 읽기 전용 보관본으로 둔다.

## 진행 순서

1. **점유 확인**: PMT 프로젝트를 `pmt --project <slug> resume <slug>`와 `pmt --project <slug> lock list`로 살펴 점유가 없는지 확인한다. 점유가 있으면 멈추고 사용자에게 알린다. 점유를 임의 해제하지 않는다.
2. **원본과 대상 조사**: PMT 프로젝트와 Work·Item·worklog·검증·백로그·결정·사실·resources를 읽는다. Plane의 기존 항목, git의 `.ledger/`와 이력, 로컬 환경 변수 이름을 조사하고 원본 종류별 개수를 기록한다. 환경 변수의 값은 출력하거나 기록하지 않는다.
3. **매니페스트 작성**: 아래 대응표와 형식에 따라 모든 기록을 매니페스트로 만든다. 주소·토큰은 환경 변수 이름으로, 홈 경로는 `~`로 바꾼다. 계정과 그 밖의 민감값도 안전한 설명으로 바꾼다. `redactions`에는 바꾼 위치와 이유만 적고 원래 값은 절대 넣지 않는다. resources도 검사 가능한 안전한 내용으로 준비한다.
4. **검사**: `ledger migrate check <file>`를 실행한다. 실패하면 적용을 멈추고 위치와 패턴 이름만 확인해 원래 값 없이 고친 뒤 다시 검사한다. 통과하기 전에는 다음 단계로 가지 않는다.
5. **미리 보기와 승인**: `ledger migrate preview <file>`의 종류별 개수, 생성·변경·그대로·충돌, 연결, 치환, 견본을 사용자에게 보여 준다. 충돌을 해결하고 변경 사항을 다시 검사·미리 보기 한다. 사용자가 미리 보기를 승인하지 않으면 멈춘다. 승인한 매니페스트와 대상이 바뀌면 다시 승인받는다.
6. **적용과 대조**: 대상 저장소에서 `migrate/<slug>` 브랜치를 사용한다. `apply`는 다른 브랜치를 거부하고 커밋하지 않는다. 승인 후 `ledger migrate apply <file>`을 실행하고, 오류나 충돌이 있으면 멈추어 알린다. 원본과 대상의 종류별 개수를 대조하고 같은 매니페스트로 재실행해 새로 생성된 항목이 0건인지 확인한다. 이후 생성된 `.ledger/` 변경을 검토하고 노출 검사를 통과한 파일만 지정해 하나의 커밋으로 묶는다. 적용 중 실패한 경우 이미 기록된 내용을 덮거나 적용 기록을 지우지 말고 결과를 확인한 뒤 같은 매니페스트로 이어서 실행한다.
7. **검토 요청**: Gitea PR(변경 검토 요청)을 준비한다. 원격에 쓰기, PR 생성, 병합과 push는 각각 사용자 요청 또는 동의를 받은 뒤에만 수행한다. push 전 사용자가 diff(변경 내용)를 확인하도록 한다.
8. **원본 종료**: 사용자가 동의한 경우에만 `pmt end <slug> --done --confirm`으로 프로젝트를 종료·보관한다. 보관본은 읽기 전용이다.

실행 명령(`<file>`은 아래 매니페스트 파일의 경로):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" migrate check <file>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" migrate preview <file>
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" migrate apply <file>
```

## PMT 대응표

| PMT 원본 | 대상과 규칙 |
|---|---|
| `project.md` | 본문을 git `.ledger/project.md`에 옮긴다. |
| Work | Plane 부모 작업 항목. `labels`에 `work`를 넣는다. |
| Item | Plane 하위 작업 항목. `parent`로 Work 또는 Item을 가리키고, Work 아래 최대 3단계를 유지한다. `labels`에 `kind:job`, `kind:test` 등 원래 종류를 넣는다. 기본 내용·범위·완료 기준·결과·증거는 `description_html`의 같은 이름 절에 담는다. |
| Work·Item 상태 | Planned → unstarted, In Progress → started, Done → completed, Paused → unstarted + `paused`, Failed → cancelled + `failed`, Skipped → cancelled + `skipped`. `blocked_by`가 있으면 적용기가 설명 맨 앞에 차단 목록을 쓰고 `blocked` 라벨을 단다. |
| 재개 메모·worklog·검증 | 각 항목의 댓글. 원래 시각은 `at`에만 넣고 `body_html`에 반복하지 않는다. 적용기가 원래 시각을 출력한다. 시간순으로 기록하고 검증에는 명령·종료 코드·커밋·제한을 담는다. |
| 백로그 | Plane 작업 항목. `labels`에 `backlog`와 `kind:req` 등 원래 종류를 넣는다. Active → backlog, Done → completed, 폐기 → cancelled. |
| 결정 | git `.ledger/history.md`와 Plane `[계획서] 결정`. 원래 ID·상태·생성일을 보존하고 새 ID의 날짜는 원래 생성일로 하며 `replaced_by`로 대체 관계를 잇는다. 결정을 삭제하지 않는다. |
| 사실 | git `.ledger/facts.md`. 원래 ID·상태·생성일·내용·출처를 보존한다. |
| `resources/` | 검사 통과한 파일을 git `.ledger/resources/pmt/<원래 경로>`로 옮긴다. |
| `RESUME.md`, 점유 | 옮기지 않는다. 이어받기 요약은 `ledger next`가 다시 만든다. |

`work`, `backlog`, `kind:*`는 반드시 매니페스트의 `labels`에 직접 적는다. 적용기는 상태 라벨 `paused`·`failed`·`skipped`와 차단 라벨 `blocked`만 추가한다.

## 매니페스트 v1

매니페스트는 UTF-8 JSON 파일이며 `${XDG_STATE_HOME:-~/.local/state}/forge-ledger/migrations/<slug>.json`에 둔다. 적용 기록은 같은 폴더의 `<slug>.applied.json`이다. 둘 다 커밋하지 않는다. 최상위 필드는 `version`(정수 `1`), `slug`, `source_dir`(원본 PMT 프로젝트 폴더), `records`(레코드 객체 목록)다. 알 수 없는 필드와 중복 키는 금지한다.

모든 레코드에 `key`, `kind`, `redactions`(문자열 목록, 없으면 빈 목록)를 넣는다. `key`는 `pmt:<slug>/<PMT 안 상대 위치>`이며 댓글은 `<항목 키>#c<순번>`이다. 중복 없이 안정적으로 유지한다. Plane 항목에는 적용기가 `external_source = "forge-ledger-pmt"`, `external_id = <원천 키>`를 쓴다.

| `kind` | 공통 필드 외 필드 |
|---|---|
| `project` | `body_md` 문자열 |
| `work` | `name`, `status`, `labels`, `description_html`; 선택 `link_to` |
| `item` | `work`의 필드와 `parent`, `blocked_by` 문자열 목록 |
| `comment` | `item` 원천 키, `at` 시각, `body_html` 문자열 |
| `backlog` | `work`와 같은 필드 |
| `decision` | `date`, `status`, `title`, `detail`; 선택 `replaced_by` |
| `fact` | `date`, `status`, `text`, `ref` |
| `resource` | `path`: 원본 `resources/` 아래 상대 경로 |

`name`, `status`, `parent`, `item`, `at`, `date`, `title`, `text`, `path`는 비어 있지 않은 문자열이다. `body_md`, `description_html`, `body_html`, `detail`, `ref`는 빈 문자열도 허용한다. `labels`, `blocked_by`는 문자열 목록이다. `link_to`는 선택 사항이며 기존 Plane 항목 ID다. 연결 시 적용기는 원천 표식만 쓰며 기존 내용은 덮어쓰지 않는다. `replaced_by`는 선택 사항이며 다른 결정 레코드의 원천 키다. 선택 필드는 생략하거나 null을 쓸 수 있다. 날짜는 `YYYY-MM-DD`, 댓글 시각은 `YYYY-MM-DDTHH:MM` 형식이며 시간대도 적을 수 있다.

`status` 허용값은 Work·Item `Planned`, `In Progress`, `Done`, `Paused`, `Failed`, `Skipped`; 백로그 `Active`, `Done`, `폐기`; 결정 `승인`, `대체`, `폐기`; 사실 `Active`, `폐기`다. `parent`, `item`, `replaced_by`는 매니페스트에 존재하는 올바른 종류의 원천 키를 가리켜야 한다. `path`는 `..` 없는 상대 경로이며 이진 파일·UTF-8이 아닌 파일·검사할 수 없는 drawio는 통과하지 않는다.
