# opencode 헤드리스 핸드오프 (freellmapi 경유)

로컬 freellmapi(OpenAI 호환 라우터)에 붙은 opencode를 `opencode run`으로 비대화형
실행해 태스크를 넘기는 방법. 2026-10-14 실측(opencode v2.0.14, WSL Ubuntu에서 검증).
`handoff` CLI의 실행자(`codex`·`agy`·`claude`)에는 아직 포함되지 않았다 —
아래는 **수동 디스패치** 절차이고, 판정·반입은 SKILL.md 3절을 그대로 따른다.

## 지원 범위

**`opencode`가 핸드오프를 실행하는 셸의 PATH에 있을 때만 쓴다.** `which opencode`(또는
PowerShell `Get-Command opencode`)가 실패하면 대상이 아니다.

WSL 안에만 설치된 opencode는 지원하지 않는다(2026-10-14 결정). 이 PC가 그 경우다:
opencode(`serve :4096`)와 freellmapi(`:3001`)가 WSL Ubuntu에만 있고, PowerShell에서
`opencode run ...`은 `CommandNotFoundException`으로 실패한다. `wsl -e bash -lc`로 감싸
부르는 우회는 문서화하지 않는다. 아래 명령은 WSL 안에서 직접 검증한 것이라
네이티브 설치에서도 같은 플래그가 동작한다는 뜻일 뿐, Windows 쪽 검증은 아니다.

필요 구성: freellmapi 같은 OpenAI 호환 프로바이더를 `opencode.json`에 모델 별칭
(`freellmapi/{balanced,smart,fast,chain}`)으로 등록해 둔다. API 키·서버 비밀번호는
환경변수로만 주입하고 프롬프트·로그·저장소에 쓰지 않는다.

## 핵심 명령

```bash
# FREELLMAPI_API_KEY(, OPENCODE_PASSWORD)는 환경변수로 미리 내보낸다 (값을 출력하지 않는다)
cd /tmp/model-handoff/<프로젝트명>/t<N>             # worktree (저장소 밖)
opencode run --auto --format json -m freellmapi/smart \
  "$(cat /tmp/model-handoff/<프로젝트명>/t<N>.prompt)" \
  > /tmp/model-handoff/<프로젝트명>/t<N>.log
```

| 플래그 | 역할 |
|---|---|
| `--auto` | 명시적으로 deny되지 않은 권한 요청을 자동 승인. 이게 없으면 헤드리스에서 승인 대기로 멈출 수 있다 |
| `--format json` | 줄 단위 JSON 이벤트 스트림. 판정 파싱용 |
| `-m provider/model` | `freellmapi/smart`(핸드오프용), `freellmapi/fast`(가벼운 일). 값은 opencode.json의 별칭 |
| `--server <URL>` | 이미 떠 있는 서버(4096)에 붙는다. 생략하면 기본 백그라운드 서비스, `--standalone`은 일회용 비공개 서버 |
| `--agent`, `--file`, `--title` | 필요할 때만. `--continue`/`--session`은 재시도에 쓰지 않는다(새 세션이 더 예측 가능) |

- **작업 디렉토리 = 호출한 셸의 cwd.** `--server`로 붙어도 서버 cwd(`~/apps/freellmapi`)가 아니라
  클라이언트 cwd에 파일이 생긴다(실측). 그래도 반드시 worktree에서 `cd` 후 실행한다.
- 프롬프트는 SKILL.md 2절 형식 그대로(가드레일 줄 + `executor-protocol.md` 인라인 모드 전문).
  마지막 줄에 `TASK_DONE T<N>` / `TASK_FAIL T<N>: ...`을 내도록 시킨다.
- 서버 인증이 필요한 경우 `OPENCODE_PASSWORD`를 환경변수로 내보낸다(위 `server.env` 로드로 충족).

## 출력 해석

`--format json`은 줄마다 `{"type": ...}` 이벤트다: `step_start`, `tool_use`, `text`, `step_finish`, `error`.

- 센티널: 마지막 `type:"text"` 이벤트의 `part.text`에서 찾는다.

  ```bash
  jq -r 'select(.type=="text") | .part.text' t<N>.log | tail -1
  ```

- 실패: `{"type":"error","error":{"type":...,"message":...}}`가 나오고 종료 코드 `1`.
  예: 없는 모델 → `provider.no-route` / `Model unavailable: freellmapi/nonexistent`.
- 종료 코드 `0`이어도 센티널이 없으면 `failed`다. 판정은 항상 메인이 worktree에서
  `검증:`을 직접 돌려 내린다(SKILL.md 3절 동일).

## 사용량 소진·전환

freellmapi는 키 여러 개를 폴백 체인으로 돌리므로 "한 계정이 찼다"가 아니라
"라우팅 전체가 소진됐다"일 때만 오류가 올라온다. 서버 쪽 신호
(`server/src/lib/fallback-loop.ts`, 소스 확인 — 실제 소진 상태는 재현하지 못했다):

| 오류 `code` | 의미 | 처리 |
|---|---|---|
| `rate_limit_exceeded`, `routing_exhausted` | 전 프로바이더 소진. 응답 `retryAtMs`·`Retry-After`가 해제 시각 | 해제 시각까지 대기로 보고, 다음 순위로 |
| `no_providers_configured` | 키 미등록 | 사용자에게 알림 (대기 아님) |

`error.message`에 위 코드가 보이면 소진으로 취급한다. 이 경우 `handoff`의 다른 실행자나
서브에이전트 대체로 넘긴다. 그 외 오류는 원인을 먼저 보고한다.

## 실측 결과와 주의

- `freellmapi/smart`로 "파일 만들고 TASK_DONE 출력" 태스크: 정상 완료(약 80초, 무료 라우터라 지연이 크다).
  `--server` 경로(`freellmapi/fast`)도 정상. 두 경우 모두 worktree에 파일이 생기고 마지막 text가 `TASK_DONE`.
- 전역 `~/.config/opencode/AGENTS.md`(이 PC 기준)가 매 세션 시작 시 `.opencode/state/STATE.md`를
  찾게 한다(`glob` 호출 1회). 실행자 규약과 충돌하지 않지만, 핸드오프 worktree에
  `.opencode/state/`가 생길 수 있으니 반입 전 `git status --short -uall`에서 걸러낸다.
- `opencode.json`의 `permissions`는 `*` 전체 allow에 `sudo`·`rm -rf /*`·`git push --force*`만 deny다.
  `--auto`는 이 위에서 동작하므로 codex `auto`·claude `auto`보다 **샌드박스가 없다**.
  worktree 밖 쓰기를 막는 장치는 프롬프트의 범위 가드레일뿐이니 반입 전 diff 확인을 생략하지 않는다.
- 무료 라우터 모델은 도구 호출 품질이 들쭉날쭉하다. 한 태스크당 재시도 1회 규칙(SKILL.md)을 유지한다.

## 후속 (원하면)

`bin/handoff`에 `opencode` 실행자를 추가하면 순위·대기·로그인 감지를 자동화할 수 있다:
`EXECUTORS`·`DEFAULT_CONFIG`에 항목, `build_command`에 위 명령, `QUOTA_PATTERNS`에
`rate_limit_exceeded|routing_exhausted`, 테스트 추가. 이번 변경에는 포함하지 않았다.
