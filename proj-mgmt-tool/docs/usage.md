# PMT 사용

이 패키지는 PMT Core 0.3.0, 작업 스킬과 Codex·Claude Code·OpenCode 연결부를 제공합니다. SQLite schema는 4이며 local/hosted 저장을 선택합니다. 실측 범위와 미실행은 [3단계 구현·검증 결과](https://github.com/wnwjdals7498/j-ai-plugin/blob/master/proj-mgmt-tool/docs/phase3/implementation-status.md)를 확인합니다.

## 패키지 만들기와 등록

이 폴더에서 Python 3.13 이상으로 실행합니다. 클라이언트 런타임은 표준 라이브러리뿐이며 Host는 별도 `host` extra를 설치합니다.

```powershell
py -3.13 scripts/build_plugins.py --output-dir dist/plugins --version 0.3.0
```

각 제품 디렉터리에 독립 실행 본체·스킬·훅과 파일 hash manifest가 생깁니다. 같은 출력 버전은 덮어쓰지 않습니다. 사용자 DB·리소스·설정은 패키지에 넣지 않습니다.

- Codex: `codex plugin marketplace add <절대경로>/dist/plugins/0.3.0/codex` → `codex plugin add pmt-lifecycle@pmt-local`. 새 세션의 `/hooks`에서 설치된 실행 명령을 검토해 활성화합니다. 검증한 제품 버전의 legacy manifest와 기본 hooks 경로를 함께 제공합니다.
- Claude Code: `claude plugin marketplace add <절대경로>/dist/plugins/0.3.0/claude` → `claude plugin install pmt-lifecycle@pmt-local`. 범위는 제품의 user/project/local 선택을 따릅니다.
- OpenCode: 프로젝트 `.opencode/plugins/pmt-loader.js`에서 설치한 번들을 참조합니다. 아래 경로는 복사해 보존한 OpenCode 번들 안의 실제 파일 URI로 바꿉니다. 번들의 `skills/proj-mgmt-tool`도 공식 스킬 검색 경로 `.opencode/skills/proj-mgmt-tool`에 등록합니다.

```javascript
export { PmtPlugin as default } from "file:///D:/pmt-plugins/0.3.0/opencode/integrations/opencode/pmt.js"
```

플러그인 제거는 등록·코드 캐시를 제거하는 제품 명령으로 수행합니다. 별도 PMT data/config root는 유지하며, 재설치할 때 같은 경로를 연결합니다.

## 데이터 경로와 Python

제품 코드와 사용자 데이터는 분리합니다. 여러 제품에서 같은 PMT를 쓸 때 같은 두 경로를 설정합니다.

```powershell
$env:PMT_DATA_ROOT = 'D:\pmt-data'
$env:PMT_CONFIG_ROOT = 'D:\pmt-config'
```

이 변수들은 **제품 프로세스를 시작할 때** 전달해야 훅에도 적용됩니다. 다른 터미널이나 이미 실행 중인 앱에 자동 반영되지 않습니다. 실제 설치 시험에서는 사용자 설정을 변경하지 않고 별도 프로필·데이터 경로를 사용했습니다.

번들 CLI는 Python 3.13으로 실행합니다. 직접 호출에는 제품 번들의 `scripts/pmt.py`를 사용합니다.

```powershell
py -3.13 .\scripts\pmt.py --data-root $env:PMT_DATA_ROOT --config-root $env:PMT_CONFIG_ROOT
```

제품 훅에서 사용할 Python 경로가 `python`과 다르면 `PMT_PYTHON`을 설정합니다. OpenCode의 `pmt.js`는 같은 번들의 `integrations/opencode/bridge.py`를 실행하며, bridge가 번들 `src`를 직접 추가합니다.

```powershell
$env:PMT_PYTHON = (Get-Command python).Source
```

Python 실행 파일은 절대 경로로 지정합니다. 제품은 다른 작업 디렉터리에서 bridge를 시작할 수 있습니다. 제품별 실제 설치·callback 검증 결과는 구현·검증 결과 문서를 따릅니다.

## 최초 확인

로컬에서는 아래 `setup`을 사용합니다. hosted는 `pmt storage configure`에 endpoint·등록 device/namespace·credential 환경변수 이름·기기별 checkout mapping과 기대 config hash를 JSON으로 전달합니다. TLS/권한/호환 확인 후 단일 저장소를 선택하며 hosted 실패를 local DB로 전환하지 않습니다. `storage probe|status`로 연결/설정을 확인합니다. 모델 설정은 ConfigRoot에 남고 새 프로젝트의 구현 Step은 완료 F4 baseline 뒤 `publish_client_plan`으로 게시한 plan을 사용합니다. 사용 절차는 번들 스킬의 `references/host-workflow.md`를 따릅니다.

서버 중단 중 이미 생성된 결과는 `pmt pending capture`, 재연결 후 `pending reconcile`로 같은 원 요청을 조정합니다. `pending status`는 해당 기존 session의 결과/resource 상태만 조회합니다. 새 offline 작업·점유·완료는 허용하지 않습니다.

CLI는 UTF-8 JSON 한 요청을 stdin으로 받고, JSON 응답 한 줄과 계약 종료 코드를 반환합니다. 진단 메시지는 stderr에 기록됩니다.

```powershell
$request = '{"protocol_version":1,"operation":"setup","request_id":"550e8400-e29b-41d4-a716-446655440000","actor":"main","session_id":"main","payload":{"product":"cli"}}'
$request | py -3.13 .\scripts\pmt.py --data-root $env:PMT_DATA_ROOT --config-root $env:PMT_CONFIG_ROOT
```

응답의 `db_id`, `environment_id`, `schema_version`, `storage_ready`를 확인합니다. 다른 제품의 최초 setup에는 `product` 값을 `codex`, `claude`, `opencode` 중 해당 값으로 지정합니다. 경로를 바꿀 때 PMT는 이전 DB를 자동 이동하거나 합치지 않습니다.

새 세션에 문맥을 전달하려면 생성한 project의 UUID를 `PMT_SCOPE_ID`, 선택 Item의 UUID를 선택적으로 `PMT_RECORD_ID`에 설정하고 제품을 시작합니다. scope를 지정하지 않으면 훅은 이벤트만 수집하고 프로젝트를 추측하지 않습니다.

## 작업 흐름

1. `create_scope`로 project 범위를 만들고 반환된 `scope_id`를 보관합니다.
2. `save_change`의 `kind: "item"`으로 제목·완료 기준·workspace·다음 조치를 등록합니다.
3. `read_context`에 top-level `scope_id`와 선택적인 `record_id`를 넣어 현재 상태와 관련 문맥을 읽습니다. `payload.query`, `limit`, `cursor`, `budget`으로 결과를 좁힙니다.
4. 선택이 있으면 `save_decision`으로 선택 내용·선택자·이유를 명시해 저장합니다. 자연어로 작업 결과를 보고하더라도 PMT가 이를 자동으로 상태 변경으로 간주하지 않습니다.
5. `claim_task`가 성공해 반환한 `claim_token`이 있을 때만 작업을 착수합니다. 충돌이면 다시 `read_context`로 현재 상태를 확인합니다.
6. 실행 전 `lookup_verification`의 `input_fingerprint`를 보관합니다. 기존 성공의 조건·증거가 유효하면 그 ID를 재사용합니다. 새 검증이 필요하면 실제 명령을 실행하고 증거를 `register_resource`로 등록한 뒤, `record_verification`에 `before_fingerprint`·실제 outcome/exit_code·criterion/evidence ID를 전달합니다. 실행 전후 지문이 달라지면 pass를 거부합니다.
7. 마치지 못하면 현재 token으로 `release_claim`을 호출하고 `Paused` 또는 `Blocked` 이유와 다음 조치를 기록합니다. Stop·idle·session 종료는 완료를 뜻하지 않습니다.

모든 변경 요청은 top-level `request_id`, `actor`, `session_id`를 갖습니다. 같은 논리 요청의 재시도에는 같은 UUID를 쓰고, 새 사용자 행동에는 새 UUID를 사용합니다. 기존 기록을 변경할 때 `record_id`와 현재 `expected_revision`을 top-level에 둡니다.

## 기록 예시

새 Item은 `scope_id`를 top-level, 상세 내용은 `payload`에 둡니다.

```json
{
  "protocol_version": 1,
  "operation": "save_change",
  "request_id": "550e8400-e29b-41d4-a716-446655440001",
  "actor": "main",
  "session_id": "main",
  "scope_id": "반환된-project-uuid",
  "payload": {
    "kind": "item",
    "title": "문맥 조회를 연결한다",
    "body": {
      "criteria": ["READ-01", "CLI-01"],
      "workspace": "D:/workspace/example",
      "next": "격리 환경에서 실제 CLI 응답을 확인한다"
    },
    "reason": "재개 가능한 검증 단위를 만든다"
  }
}
```

`workspace`에는 실제 존재하는 검증 대상의 절대 경로를 넣습니다. 위 경로·UUID는 사용자 값으로 교체합니다. 기준은 문자열 ID 또는 `{"id":"READ-01","description":"검증할 내용"}` 객체로 기록하며, 설명 변경도 기존 검증을 무효화합니다.

현재 Item을 착수할 때 요청에는 top-level `record_id`와 `expected_revision`을 추가하고, `claim_task`를 호출합니다. 성공 응답의 token은 finish/release 요청에서만 사용합니다.

명시 선택은 `save_decision`으로 남깁니다. 예를 들어 `payload.decision_kind`는 `select`, `custom`, `delegate` 중 하나이고, 직접 선택한 내용·decider·이유·근거를 입력합니다. `Stop`, 도구 완료, 모델의 제안만으로 사용자의 선택을 만들지 않습니다.

완료 요청의 모양은 다음과 같습니다. PMT는 token·revision·결과와 기준별 현재 검증·evidence를 확인한 뒤에만 Done을 기록합니다.

```json
{
  "protocol_version": 1,
  "operation": "finish_task",
  "request_id": "550e8400-e29b-41d4-a716-446655440002",
  "actor": "main",
  "session_id": "main",
  "record_id": "반환된-item-uuid",
  "expected_revision": 2,
  "payload": {
    "claim_token": "현재 소유 token",
    "result": "READ-01과 CLI-01 검증을 마쳤다",
    "verification_ids": ["유효한-verification-uuid"]
  }
}
```

선택·실행 결과를 사용자에게 보고할 때는 무엇을 선택했는지, 이유, 기준별 pass/fail/blocked, evidence ID, 아직 남은 다음 조치를 구분해 전달합니다. PMT 호출이 실패하거나 timeout이면 저장됐다고 보고하지 않습니다.

## 제품별 공식 설치 참고

- [Codex plugin 패키징과 local marketplace](https://developers.openai.com/plugins/build/plugins): package 구조·marketplace 항목과 hook trust 절차를 확인합니다.
- [Claude Code plugin 설치](https://code.claude.com/docs/en/plugins) 및 [manifest/marketplace reference](https://code.claude.com/docs/en/plugins-reference), [marketplace schema](https://code.claude.com/docs/en/plugins/marketplace-reference): `.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `claude plugin validate`를 확인합니다.
- [OpenCode stable plugin 문서](https://opencode.ai/docs/plugins/): local JavaScript plugin과 `package.json`의 의존성 설치 동작을 확인합니다.

marketplace 등록·설치·신뢰 설정은 제품의 공식 명령을 따릅니다. 설치 시험은 본체·hook fixture 시험과 구별하며, 모델 응답 fixture를 사용한 시험을 실제 LLM의 성능이나 모델 분배 검증으로 표현하지 않습니다.

## 4단계 새 세션에서 이어가기

먼저 `compose_resume_overview`에 명시 project `scope_id`, 현재 `actor/session_id`, 새 `request_id`를 전달합니다. 기본 예산은 UTF-8 16KiB/160줄이며 `payload.budget`으로 제한합니다. 이 조회는 현재 방향·결정 참조·기존 실행·unknown을 전달하고 점유나 실행을 만들지 않습니다. 제품 SessionStart에서 자동 조회하려면 기존 data/config 설정과 `PMT_SCOPE_ID`를 지정합니다.

1. 개요에 기존 run/pending이 있으면 실제 상태·원 요청 결과부터 조회합니다. 같은 작업을 새로 실행하거나 idle만으로 잠금을 해제하지 않습니다.
2. 작업을 선택하고 현재 run/범위 claim을 확인한 뒤 `capture_work_basis`로 실제 source와 업무 기준을 수집합니다. Hosted는 설정된 클라이언트 checkout을 읽고 Host에 ref/hash/coverage만 저장합니다.
3. 이전 확정 basis와 차이가 있으면 `collect_changes` → `build_implementation_links` → `assess_alignment`로 영향과 미확인을 확인합니다. `read_applicability`는 기존 근거의 현재 적용 가능성을 별도 기록합니다.
4. 기준 변경은 `propose_semantic_resolution`으로 위임·사용자 검토 경계를 확인합니다. 실제 F1 graph/F3 문서 반영과 readback 뒤 `apply_alignment` 영수증을 확정합니다. caller의 승인 주장만으로 반영하지 않습니다.
5. 현재 기준으로 `compose_task_resume`를 만들고 필요한 상세만 `read_resume_detail`로 읽습니다. stale source·권한·불완전한 변경 근거는 추가 조회/검토 대상이며 실행 준비 완료가 아닙니다.
6. 실제 결정·검토·게시 등 확정 사건을 참조해 `create_checkpoint`를 호출합니다. `link_session`은 참조 연결이며 owner 이전이나 Done 처리가 아닙니다.

실제 입력 계약은 [4단계 runtime contract](phase4/runtime-contract.md), 실행된 범위와 제한은 [4단계 구현 상태](phase4/implementation-status.md)를 따릅니다. `prune_continuity`는 기본 dry-run입니다. 명시 apply로 90일이 지난 미참조 metadata를 정리하되 현재 근거·미해결 효과·실행 소유권은 보존합니다.
