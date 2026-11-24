---
description: 프로젝트 결정 항목을 Plane과 이력에 기록한다
argument-hint: "<제목> --project KEY|* --body-file FILE"
---

`ledger-plan` 스킬의 절차와 규칙을 따른다.

아래 명령을 실행해 결정 항목을 기록하고 결과를 간결히 보고한다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" decide $ARGUMENTS
```
