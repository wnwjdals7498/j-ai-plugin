# pi 헤드리스 핸드오프 (freellmapi 경유)

[pi](https://pi.dev) 코딩 에이전트를 `pi -p`로 비대화형 실행해 태스크를 넘기는 방법.
2026-10-14 실측: pi 1.2.0, Windows 네이티브, Git Bash, freellmapi `http://127.0.0.1:3001/v1`.
`handoff` CLI 실행자(`codex`·`agy`·`claude`)에는 포함되지 않는다. 아래는 **수동 디스패치**
절차이고 판정·반입은 SKILL.md 3절을 그대로 따른다. opencode는 `opencode-headless.md`.

## 1회 설정 — freellmapi 연결

`~/.pi/agent/models.json` (Windows: `C:\Users\<사용자>\.pi\agent\models.json`):

```json
{
  "$schema": "https://pi.dev/schemas/models.schema.json",
  "providers": {
    "freellmapi": {
      "baseUrl": "http://127.0.0.1:3001/v1",
      "api": "openai-completions",
      "apiKey": "$FREELLMAPI_API_KEY",
      "models": [
        { "id": "auto:smart", "name": "FreeLLMAPI auto:smart", "contextWindow": 128000, "maxTokens": 16384 },
        { "id": "auto:balanced", "name": "FreeLLMAPI auto:balanced", "contextWindow": 128000, "maxTokens": 16384 },
        { "id": "auto:fast", "name": "FreeLLMAPI auto:fast", "contextWindow": 128000, "maxTokens": 8192 },
        { "id": "auto", "name": "FreeLLMAPI auto (fallback chain)", "contextWindow": 128000, "maxTokens": 16384 }
      ]
    }
  }
}
```

- `apiKey`는 `$NAME` 환경변수 치환이다. 키 값을 파일·프롬프트·로그에 쓰지 않는다.
  실행 전에 `FREELLMAPI_API_KEY`를 환경변수로 내보낸다.
  환경변수 대신 `models.json`의 `apiKey`에 키를 직접 적어도 동작한다(2026-10-14 환경변수 없이 실측).
  `models.json`은 `~/.pi/agent/`에 있어 저장소 밖이지만, 저장소 안 문서·예시에는 키를 적지 않는다.
- 확인: `pi --offline --list-models freellmapi`가 위 4개 모델을 보여 주면 연결된 것이다.
  모델 ID는 서버 `GET /v1/models`(Bearer 키 필요)에 있는 값이다. 없는 ID를 쓰면 서버가
  `404 model_not_found`를 돌려준다.
- 설치 직후 `pi`가 PATH에 없을 수 있다. 실행 파일은 `~/.pi/agent/bin/pi`(`pi.cmd`·`pi.ps1` 동봉).

## 핵심 명령

```bash
cd /tmp/model-handoff/<프로젝트명>/t<N>        # worktree (저장소 밖)
pi --offline -p --mode json --no-session --model freellmapi/auto:smart \
  "$(cat /tmp/model-handoff/<프로젝트명>/t<N>.prompt)" \
  < /dev/null > /tmp/model-handoff/<프로젝트명>/t<N>.log 2>&1
```

| 플래그 | 역할 |
|---|---|
| `-p` | 프롬프트를 처리하고 종료 |
| `--mode json` | 이벤트를 JSON 줄로 출력. 판정 파싱용 |
| `--model provider/id` | `freellmapi/auto:smart`(핸드오프용), `freellmapi/auto:fast`(가벼운 일) |
| `--no-session` | 세션 파일을 남기지 않는다 |
| `--offline` | 시작 시 네트워크 작업(업데이트 확인 등)을 끈다 |
| `-t <목록>` / `-xt <목록>` | 도구 허용/제외 목록. 읽기 전용 검토는 `-t read,grep,find,ls` |

- **`< /dev/null`은 필수다.** stdin이 열린 채(파이프 등)로 `pi -p`를 실행하면 출력 0줄로 멈춘다.
  같은 명령이 `</dev/null`이면 5~7초에 끝났고 빼면 60초 타임아웃까지 멈췄다(각 3회 이상 재현).
  `bin/handoff`가 이미 `stdin=DEVNULL`로 실행자를 띄우는 것과 같은 이유다.
- 작업 디렉토리 = 호출 셸의 cwd. 반드시 worktree로 `cd`한 뒤 실행한다.
- 프롬프트는 SKILL.md 2절 형식 그대로(가드레일 줄 + `executor-protocol.md` 인라인 모드 전문).

## 출력 해석 — 종료 코드를 믿지 않는다

**pi는 모델·API 오류에도 종료 코드 `0`을 낸다.** 성공 여부는 JSON 이벤트로 판정한다.

이벤트 종류: `session`, `agent_start`, `turn_start`, `message_start/update/end`,
`tool_execution_start/update/end`, `turn_end`, `agent_end`, `agent_settled`.

- 마지막 assistant `message_end`의 `message.stopReason`:
  - `stop` → 정상 종료. `message.content`의 `type:"text"` 마지막 줄에서 센티널(`TASK_DONE`) 확인
  - `error` → `message.errorMessage`에 서버 응답이 들어 있다. 예:
    `404: {"message":"Model 'nonexistent' is not in the catalog...","code":"model_not_found"}`
  - `toolUse` → 도구 호출 중간 턴. 마지막이 이 값이면 비정상 종료다
- 도구 사용 이력: `tool_execution_start`의 `toolName` (`write`, `bash`, `edit`, `read`...)

```bash
python - t<N>.log <<'EOF'
import json, sys
last = None
for line in open(sys.argv[1], encoding="utf-8"):
    try: o = json.loads(line)
    except ValueError: continue          # 경고 등 JSON이 아닌 줄은 건너뛴다
    if o.get("type") == "message_end" and o["message"].get("role") == "assistant":
        last = o["message"]
text = "".join(c.get("text", "") for c in (last or {}).get("content", []) if c.get("type") == "text")
print(last and last.get("stopReason"), (last or {}).get("errorMessage"), text.strip().splitlines()[-1:] )
EOF
```

로그를 `2>&1`로 합치면 `Warning: Model "..." not found ... Using custom model id.` 같은
비-JSON 줄이 섞인다. 위처럼 파싱 시 건너뛰거나, 로그와 stderr를 분리한다.
`stopReason`이 `stop`이고 센티널이 있어도 판정은 메인이 worktree에서 `검증:`을 직접 돌려 내린다.

## 사용량 소진·전환

freellmapi가 전 프로바이더 소진을 알리면 `stopReason: "error"`에 서버 오류 본문이
`errorMessage`로 온다. 서버 소스 기준 코드: `rate_limit_exceeded`, `routing_exhausted`
(응답에 `retryAtMs`·`Retry-After`가 해제 시각), `no_providers_configured`(키 미등록 — 대기가 아님).
실제 소진 상태는 재현하지 못했다. `errorMessage`에 위 코드가 보이면 소진으로 보고
다음 순위나 서브에이전트 대체로 넘긴다. 그 외 오류는 원인을 먼저 보고한다.

## 실측 결과와 주의

- `freellmapi/auto:smart`, 기본 설정(확장·MCP 포함), `</dev/null`: "hello.txt 작성 → `cat`으로 확인 → `TASK_DONE`"
  태스크가 7초에 완료. 도구 `write`·`bash` 사용, 마지막 `stopReason: "stop"`, 마지막 줄 `TASK_DONE`, 파일 내용 `hi`.
- 센티널 준수는 모델 몫이라 불안정하다. 재검증 실행에서 `auto:smart`가 `hello.txt` 안에 `TASK_DONE`까지 쓰고
  마지막 줄을 `TASK_DONE` 단독이 아닌 설명 문장으로 끝냈다(`stopReason`은 `stop`). 센티널은 참고값이고
  `검증:` 직접 실행이 판정이다.
- 라우터 지연은 들쭉날쭉하다(같은 요청이 3초~수십 초). 제한 시간은 넉넉히 잡는다.
- **pi는 권한 승인 단계도 샌드박스도 없다.** `bash`·`write`가 확인 없이 실행된다(위 실측에서
  승인 프롬프트 없이 실행됨). codex·claude `auto`보다 안전장치가 약하다. worktree 밖 쓰기를 막는 건
  프롬프트의 범위 가드레일뿐이므로 반입 전 `git status --short -uall`과 diff 확인을 생략하지 않는다.
- 사용자 환경의 확장(예: `~/.pi/agent/extensions/`의 Orca 상태 확장)이 헤드리스 실행에도 로드된다.
  문제가 생기면 `--no-extensions --no-mcp`로 격리해 본다(이번 실측에서는 stdin이 원인이었고 확장은 무관).
- `bin/handoff`는 `fcntl`을 써서 Windows 네이티브에서는 돌지 않는다. 그래서 이 문서는
  `handoff run` 대신 Bash 도구로 직접 디스패치하는 절차다.

## 후속 (원하면)

`bin/handoff`에 `pi` 실행자를 추가하면 순위·대기 상태를 자동화할 수 있다: `EXECUTORS`·`DEFAULT_CONFIG`에 항목,
`build_command`에 위 명령, 결과 판정을 종료 코드 대신 `stopReason`으로, `QUOTA_PATTERNS`에
`rate_limit_exceeded|routing_exhausted`, 테스트 추가. 이번 변경에는 포함하지 않았다.
