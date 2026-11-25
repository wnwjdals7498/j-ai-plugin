---
description: 로컬 대시보드(S1)를 띄우고 접속 주소를 알린다
argument-hint: "[--bind 주소] [--port 번호] [--interval 초]"
---

아래 명령을 백그라운드로 실행한다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" dashboard $ARGUMENTS
```

출력의 `대시보드:` 줄을 사용자에게 그대로 알린다. `[!]`로 시작하는 줄이 있으면 함께 알린다. 멈추라는 요청이 오면 그 프로세스를 끝낸다.
