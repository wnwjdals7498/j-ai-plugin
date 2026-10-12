---
name: setting
description: model-handoff 실행자 순위·활성화·모델·대기 시간 설정을 보거나 바꾼다. "핸드오프 순위 바꿔", "codex 끄기", "하위 모델 상태" 요청이나 /model-handoff:setting 호출이 대상이다.
argument-hint: "[status | order codex,agy,claude | enable|disable <실행자> | set <키> <값> | clear <실행자|all>]"
---

# model-handoff 설정

설정은 `handoff` CLI로만 바꾼다. 파일을 직접 고치지 않는다.
인자: `$ARGUMENTS` — 비어 있으면 `handoff status`와 `handoff config show`를 보여 준다.
자연어 요청은 아래 명령으로 옮겨 실행하고, 실행한 명령과 결과를 함께 보여 준다.

| 할 일 | 명령 |
|---|---|
| 순위와 상태 보기 | `handoff status` |
| 전체 설정 보기 | `handoff config show` |
| 순위 바꾸기 | `handoff config order agy,codex,claude` |
| 실행자 켜기·끄기 | `handoff config enable claude`, `handoff config disable agy` |
| 실행자 모델 고정 | `handoff config set codex.model <모델>` (해제는 값 `null`). 목록: `agy models` |
| codex 추론 강도 | `handoff config set codex.effort xhigh` (`low`·`medium`·`high`·`xhigh`·`max`·`ultra`, 해제는 `null`). 모델별 지원 단계는 `~/.codex/models_cache.json` |
| 실행자 승인 방식 | `handoff config set codex.permission auto` (기본) 또는 `bypass` |
| agy 샌드박스 자동 실행 설정 | `handoff setup agy` |
| Codex 샌드박스 네트워크 | `handoff config set codex.network false` (기본 `true`) |
| 개인 Claude 계정 연결 | `handoff config set claude.config_dir ~/.claude-personal` 후 `handoff config enable claude` |
| 개인 Codex 계정 분리 | `handoff config set codex.codex_home ~/.codex-personal` |
| 기본 대기 시간(분) | `handoff config set default_cooldown_minutes 300` |
| 실행 제한 시간(분) | `handoff config set timeout_minutes 12` |
| 대기 수동 지정·해제 | `handoff cooldown codex --minutes 60`, `handoff clear codex`, `handoff clear all` |
| 기본값 복원 | `handoff config reset` |

실행자: `codex`, `agy`, `claude`. 사용량 초과를 감지하면 CLI가 해제 시각까지
자동으로 `cooldown`을 건다. 해제 시각을 못 읽으면 `default_cooldown_minutes`를 쓴다.

승인 방식 `auto`(기본)는 각 CLI에서 안전장치를 유지하는 가장 넓은 자동 승인이다.

| 실행자 | `auto` | `bypass` |
|---|---|---|
| codex | `--approve-for-me`: 검토 에이전트가 요청을 자동 승인, 작업 폴더만 쓰기 가능한 샌드박스, 외부 네트워크 허용(`codex.network`) | `--dangerously-bypass-approvals-and-sandbox` |
| agy | `--mode accept-edits --sandbox` + 설정 `toolPermission: proceed-in-sandbox`: 샌드박스 안 셸 명령 자동 실행, 밖으로 나가려는 명령은 거부 | `--dangerously-skip-permissions` |
| claude | `--permission-mode auto`: 분류기가 동작마다 검토, 위험 동작 거부 | `--dangerously-skip-permissions` |

agy의 `proceed-in-sandbox`는 실행 플래그가 없어 `~/.gemini/antigravity-cli/settings.json`에
넣어야 한다. `handoff status`에서 agy에 "run `handoff setup agy`"가 보이면 사용자에게
알리고 승인받은 뒤 `handoff setup agy`를 실행한다(기존 파일은 백업). 이 설정은
대화형 agy에도 적용된다.

`bypass`는 모든 검사를 끈다. 사용자가 명시적으로 요청할 때만 설정하고, 설정 전에
"컨테이너·VM처럼 격리된 환경에서만 안전하다"는 점을 알린다.

실행 중 로그인 요구가 감지되면 그 실행자는 `login_required`가 되어 건너뛴다.
사용자가 직접 로그인한 뒤 `handoff clear <실행자>`로 해제한다. agy 로그인은
`! agy` 실행 후 안내되는 URL로 진행한다.

계정 분리 로그인은 사용자가 직접 한다. 안내만 한다:

- 개인 Claude: `! CLAUDE_CONFIG_DIR=~/.claude-personal claude` 실행 후 `/login`
- 개인 Codex: `! CODEX_HOME=~/.codex-personal codex login`

Antigravity(`agy`)를 처음 켜거나 순위에 넣을 때 한 번 알린다: Antigravity 공식
FAQ는 소비자 요금제에서 agy를 다른 코딩 에이전트 안에 감싸 쓰는 것을 지원하지
않으며 계정 정지 사유가 될 수 있다고 적고 있다
(https://antigravity.google/docs/faq/).
