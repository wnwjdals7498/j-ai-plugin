# PMT 사용

이 패키지는 로컬 SQLite PMT, 작업 스킬과 Codex·Claude Code·OpenCode 연결부를 제공합니다. 아래 순서로 패키지를 만들고 제품별로 등록합니다. 실측 성공·미확인 범위는 [구현·검증 결과](phase1/implementation-status.md)를 확인합니다.

## 패키지 만들기와 등록

이 폴더에서 Python 3.13 이상으로 실행합니다. 본체의 런타임 의존성은 표준 라이브러리뿐입니다.

```powershell
py -3.13 scripts/build_plugins.py --output-dir dist/plugins --version 0.1.0
```

각 제품 디렉터리에 독립 실행 본체·스킬·훅과 파일 hash manifest가 생깁니다. 같은 출력 버전은 덮어쓰지 않습니다. 사용자 DB·리소스·설정은 패키지에 넣지 않습니다.

- Codex: `codex plugin marketplace add <절대경로>/dist/plugins/0.1.0/codex` → `codex plugin add pmt-lifecycle@pmt-local`. 새 세션의 `/hooks`에서 설치된 실행 명령을 검토해 활성화합니다. 0.156.1용 legacy manifest와 기본 hooks 경로를 함께 제공합니다.
- Claude Code: `claude plugin marketplace add <절대경로>/dist/plugins/0.1.0/claude` → `claude plugin install pmt-lifecycle@pmt-local`. 범위는 제품의 user/project/local 선택을 따릅니다.
- OpenCode: 프로젝트 `.opencode/plugins/pmt-loader.js`에서 설치한 번들을 참조합니다. 아래 경로는 복사해 보존한 OpenCode 번들 안의 실제 파일 URI로 바꿉니다. 번들의 `skills/proj-mgmt-tool`도 공식 스킬 검색 경로 `.opencode/skills/proj-mgmt-tool`에 등록합니다.

```javascript
export { PmtPlugin as default } from "file:///D:/pmt-plugins/0.1.0/opencode/integrations/opencode/pmt.js"
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
