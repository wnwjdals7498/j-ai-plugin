---
name: handoff
description: 작업을 하위 모델(Codex, Antigravity, 개인 Claude)에 우선순위대로 나눠 병렬로 넘길 때 사용한다. "하위 모델로 병렬 작업하자", "하위 모델로 돌려", "핸드오프로 병렬 처리" 요청이 대상이다. 사용량이 다 찬 실행자는 갱신 시각까지 건너뛰고 다음 순위로 넘어간다.
---

# model-handoff: 우선순위 전환 병렬 핸드오프

메인 세션(Claude)은 분할·계획·검증·반입만 한다. 코드 생성은 `handoff run`이
고른 실행자가 한다. 실행자 선택과 사용량 소진 시 전환은 `handoff` CLI가
담당한다 — 이 스킬에서 순위를 직접 판단하지 않는다.

## 1. 제안 (실행 전 반드시 승인받는다)

1. `handoff status`로 순위와 상태를 읽는다. 모두 `cooldown`·`login_required`·`disabled`면
   가장 빠른 해제 시각을 알리고 멈춘다.
2. 요청을 태스크로 나눈다. 형식은 `references/plan-format.md`:
   - 태스크마다 `대상:`(파일 단위 전부)과 `검증:`(실행 가능한 커맨드 → 기대 결과)
   - `검증:`을 쓸 수 없는 일은 넘기지 않고 메인에 남긴다
   - `대상:`이 겹치지 않는 태스크끼리만 같은 병렬 묶음에 넣는다. 한 묶음은 최대 3개
3. 아래 표로 제안하고 사용자 승인을 받는다. 승인 전에는 worktree도 만들지 않는다.

   | 태스크 | 대상 | 검증 | 묶음 | 예상 실행자 |
   |---|---|---|---|---|

   예상 실행자는 현재 `ready`인 첫 순위다. 실행 중 사용량이 차면 바뀔 수 있다고 적는다.
   순위를 바꾸고 싶다는 답이 오면 `/model-handoff:setting`으로 안내한다.

## 2. 실행 (승인된 묶음마다)

계획 파일을 `docs/superpowers/plans/YYYY-MM-DD-<주제>-handoff.md`에 저장한 뒤
묶음 안의 태스크를 병렬로 처리한다.

1. **worktree — 반드시 저장소 밖**:

   ```bash
   git -C <프로젝트> worktree add -b mh-t<N> /tmp/model-handoff/<프로젝트명>/t<N>
   ```

   저장소 안에 두면 agy가 본체를 워크스페이스로 잡는다(2026-08-03 실측).
   `검증:`에 의존성이 필요하면 메인이 worktree에서 먼저 설치한다.

2. **프롬프트 파일** `/tmp/model-handoff/<프로젝트명>/t<N>.prompt`:

   ```
   다음 실행자 규약에 따라 <계획 파일 경로>의 T<N>을 수행하라.
   작업 범위는 <대상 경로>뿐이다. 그 외 파일·디렉토리는 절대 건드리지 마라.

   <references/executor-protocol.md의 "너는 실행자다..." 줄 + 인라인 모드 섹션 전문>
   ```

   둘째 줄 가드레일은 생략하지 않는다. 계획 파일은 worktree 안 경로로 적는다
   (worktree 생성 전에 계획을 커밋하거나 worktree에 복사한다).

3. **디스패치** — 태스크마다 Bash `run_in_background`로 실행:

   ```bash
   handoff run --cwd /tmp/model-handoff/<프로젝트명>/t<N> \
     --prompt-file /tmp/model-handoff/<프로젝트명>/t<N>.prompt \
     --log /tmp/model-handoff/<프로젝트명>/t<N>.log
   ```

   stdout은 JSON이다. 종료 코드: `0` 완료, `1` 실행자 실패, `3` 모든 실행자 사용 불가.

## 3. 판정과 반입

센티널(`TASK_DONE`/`TASK_FAIL`)은 참고값이다. 실행자는 자동 승인 모드로 돌기
때문에 셸 명령이 막혀 스스로 검증하지 못할 수 있다(agy는 print 모드에서 셸
명령이 기본 거부된다). 그래서 판정은 항상 메인이 worktree에서 직접 한다.

- `done` 또는 `failed` → worktree에서 `git status --short -uall`과 `diff`로 대상 외 변경을
  목록화하고(버릴 노이즈), `검증:` 커맨드를 worktree에서 직접 실행한다.
  통과하면 `대상:` 경로만 반입한다:

  ```bash
  git -C <worktree> add -A -- <대상 경로들> && \
  git -C <worktree> diff --cached -- <대상 경로들> | git -C <프로젝트> apply
  ```

  본체에서 `검증:`을 다시 실행하고 통과하면 계획 체크박스를 `[x]`로 바꾼다.
- 검증 실패 → diff에서 본 문제를 짚는 재지시 프롬프트로
  `handoff run --only <같은 실행자> ...`를 **1회만** 재시도한다. 재실패면 그 태스크를
  멈추고 worktree를 남긴 채 경로·diff 요약·사유를 보고한다.
- `unavailable` → `next_available` 시각을 보고하고 태스크를 미완료로 둔다.
  남은 묶음도 시작하지 않는다.

`attempts`에 `limit`이 있으면 어느 실행자가 언제까지 대기로 바뀌었는지 보고에 넣는다.

## 4. 정리와 보고

반입이 끝난 worktree는 지운다:

```bash
git -C <프로젝트> worktree remove --force /tmp/model-handoff/<프로젝트명>/t<N> && \
git -C <프로젝트> branch -D mh-t<N>
```

마지막에 태스크별 한 줄(실행자, 결과, 검증)을 보고한다. 커밋은 사용자가
제안 단계에서 승인한 경우에만 한다.

## 하지 말 것

- 실행자 권한을 `bypass`로 바꾸는 것 — 사용자가 `/model-handoff:setting`으로 직접 지정한 경우만 허용

- 승인 전에 실행자를 호출하는 것
- 본체 저장소에서 실행자를 직접 돌리는 것, worktree를 저장소 안에 만드는 것
- 메인이 구현 코드를 직접 쓰는 것 (검증 커맨드·시그니처·한 줄 수정 지시는 가능)
- 태스크당 실행자 호출 3회 이상
- 사용량 오류가 아닌 실패를 다른 실행자로 돌리는 것 — 원인을 먼저 보고한다
- 대상 프로젝트의 AGENTS.md·GEMINI.md에 실행자 규약을 넣는 것
