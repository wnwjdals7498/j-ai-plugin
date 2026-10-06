# P4-R5 제품 연결 capability 관찰

2026-10-06 기준. 공식 제품 문서와 현재 저장소 adapter 코드를 대조한 설계 조사다. 네이티브 설치·제품 세션 실행 증거는 아직 없다.

| 제품 | 현재 코드 경로 | 제품이 제공하는 출력면 | 판정과 제한 |
|---|---|---|---|
| Codex | `integrations/codex/hooks/hooks.json` → `integrations/codex/hook.py` → `pmt.hooks.process_session_start` | SessionStart JSON의 `hookSpecificOutput.additionalContext`; 입력에는 `session_id`와 `source`가 있다. | 코드는 lifecycle receipt와 bounded `compose_resume_overview` 조회를 연결한다. 명시 scope가 없으면 조회를 하지 않는다. Hosted path는 등록 profile principal/provenance/scope를 검사한다. Adapter/local/loopback tiers가 시험됐고 native plugin install/new session output은 미시험이다. |
| Claude Code | `integrations/claude/hooks/hooks.json` → `integrations/claude/hook.py` → `pmt.hooks.process_session_start` | SessionStart는 새 세션 시작·기존 세션 재개 때 실행되며 JSON `additionalContext`를 모델 문맥에 추가한다. | 코드 경로는 Codex와 동일한 bounded overview operation을 사용한다. Adapter/local/loopback tiers가 시험됐고 native plugin install/new session output은 미시험이다. |
| OpenCode | `integrations/opencode/pmt.js` → `integrations/opencode/bridge.py` | 저장소는 `session.created` 최소 metadata 조회와 `experimental.chat.system.transform`의 system context 주입을 사용한다. | 현재 공식 plugin docs에는 session/tool event가 있지만 V2 migration 문서는 이전 experimental transform이 `ctx.session.hook("context", ...)`로 재평가돼야 하며 1:1 대응이 아님을 알린다. Bridge fixture와 Hosted HTTPS metadata path는 시험됐으나 OpenCode가 설치되지 않아 native session/output 호환성은 blocked/not_run이다. |

Codex 공식 자료는 [Hooks](https://learn.chatgpt.com/docs/hooks)와 [plugin hooks](https://developers.openai.com/plugins/build/plugins), Claude 자료는 [Hooks reference](https://code.claude.com/docs/en/hooks), OpenCode 자료는 [Plugins](https://opencode.ai/docs/plugins/)와 [V1-to-V2 migration](https://opencode.ai/v2/docs/build/plugins/migrate-v1/)이다. OpenCode V2 문서는 현재 저장소가 사용하는 transform API의 지원 여부를 설치된 제품으로 입증하지 않는다.

현재 환경 조회에서 `codex`와 `claude` 실행 파일이 PATH에 있었고 `opencode`는 없었다. 실행 파일의 버전이나 사용자 profile 설정은 조사하지 않았다. 사용자 profile을 변경하는 제품 설치·새 세션 실험은 수행하지 않았다.

R5 tier 분리:

- Adapter fixture: `tests/test_hooks.py`, `tests/test_hook_acceptance.py`, `tests/test_storage_config.py`가 검증하는 입력 선별, output envelope, pending replay, credential/scope selector 경계.
- Local/Host integration: PMT 격리 roots, SQLite, 현재 허용된 실제 loopback HTTPS Host를 통과한 동작만 해당 tier로 기록한다.
- Native product: Codex/Claude/OpenCode의 격리 설치·새 세션 output 관찰이 필요하다. 이 파일의 코드 조사나 fixture 통과로 승격할 수 없다.

Hook에서의 `additionalContext`는 metadata overview JSON과 명시적 “reference only” 안내다. Hook/세션 이벤트는 checkpoint를 만들지 않는다. Checkpoint는 public service가 actual persisted decision/run/review/publication receipt를 검증한 뒤 생성한다. private directive/detail은 generic Host metadata read 경로에서 제외되고 별도 현재 WorkAccess에 남는다.
