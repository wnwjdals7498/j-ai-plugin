---
description: 작업 계획서 생성·조회·갱신·검사를 실행한다
argument-hint: "new <제목> --key KEY | check [--project KEY] | get KEY DOC | put KEY DOC --from FILE"
---

`ledger-plan` 스킬의 절차와 규칙을 따른다.

아래 명령을 실행해 요청된 계획서 작업을 수행하고 결과를 간결히 보고한다.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/ledger.py" plan $ARGUMENTS
```
