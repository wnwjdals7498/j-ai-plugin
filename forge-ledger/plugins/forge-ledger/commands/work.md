---
description: 작업 항목의 착수·인계·검증·종료를 Plane에 기록한다
argument-hint: "start|note|verify|end <코드> [옵션]"
---

아래 명령을 실행하고 결과를 간결히 보고한다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" work $ARGUMENTS
```

- `start <코드> [--take]`: 착수. `--take`는 30분 넘게 기록이 없는 다른 사람의 점유를 넘겨받는다.
- `note <코드> --did "<한 일>" --next "<다음 할 일>"`: 인계 메모. 각 400자 이하.
- `verify <코드> --cmd "<명령>" (--exit N | --run) [--limits "<확인하지 못한 범위>"]`: 검증 기록. `--run`은 명령을 실행하고, 명령이 실패하면 기록한 뒤 종료 코드 1로 끝난다. 실행 중에 HEAD가 바뀌면 기록하지 않는다.
- `end <코드> --done --result "…" [--unverified "…"]` · `--pause` · `--fail --cause "…" --fix "…"` · `--skip --reason "…"`: 종료. `--done`은 현재 HEAD의 검증 기록이 없으면 `--unverified`가 필요하다. `[종료]` 댓글을 남기지 못했다는 오류가 나오면 같은 `end` 명령을 다시 실행한다. 이 컴퓨터의 착수를 같은 방식으로 이미 끝냈으면 아무것도 바꾸지 않고 기존 `[종료]` 줄을 알려 준다. 다른 방식이면 거부되며 `start`부터 다시 한다. 그 사이에 다른 컴퓨터가 착수했으면 거부된다.

`end`는 현재 유효한 착수를 남긴 사용자와 컴퓨터에서만 실행할 수 있다.

점유 거부나 착수 취소가 나오면 우회하지 말고 사용자에게 그대로 알린다. 기록에는 IP, 접속 주소, 토큰, 계정, 홈 경로를 넣지 않는다.
