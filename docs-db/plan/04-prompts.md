# 04. 지시문 틀과 오케스트레이터 절차

Sonnet 구현자와 리뷰어에게 보내는 지시문은 이 틀을 **그대로** 쓰고 `{…}`만 채운다. 틀을 바꾸면 작업마다 지시가 달라져 결과가 흔들린다.

## 1. 오케스트레이터 절차

```text
준비(1회)
  1. 사용자 승인 확인 → superpowers:using-git-worktrees로 작업 트리와 브랜치 feat/docdb 생성
  2. PMT: docdb 프로젝트에 Work "구현" 추가, 작업 T01~T30을 Item으로 등록
  3. T00a·T00b 실측 → 결과를 08-verification·C-26에 반영 (T00c는 push 승인 시)

작업마다(T01부터 순서대로)
  1. pmt start <Item>
  2. 구현자 지시문(2절)으로 Agent 호출: model="sonnet"
  3. 완료 보고 확인 → 오케스트레이터가 직접 전체 테스트 실행(보고를 믿지 않고 재실행)
  4. 리뷰어 지시문(3절)으로 새 Agent 호출: model="sonnet"
  5. 판정
     - 통과: pmt verify <Item> --cmd "<테스트 명령>" --run → pmt end <Item> --done --result "<요약>"
     - 수정 필요: 4절 지시문으로 같은 구현자에게 SendMessage (최대 2회)
     - 멈춤 또는 2회 초과: 사용자에게 보고(예상·실제·근거·원인·수정 방향·영향)
  6. 계약·결정 변경이 생기면 01-contracts·09-decisions를 먼저 고치고 다음 작업으로

마무리
  1. 전체 브랜치 리뷰(03-review 5절): model="opus"
  2. T27~T30
  3. 커밋 이력 정리 없이 그대로 두고, 병합·push는 사용자 지시 때만
```

- 작업은 **순서대로 하나씩** 진행한다. 같은 파일(`cli.py`, `llm.py`, `conftest.py`)을 여러 작업이 고치므로 병렬로 돌리지 않는다.
- 구현자 서브에이전트는 작업마다 새로 시작한다(앞 작업 맥락이 섞이지 않게).
- 오케스트레이터는 구현자 대신 코드를 크게 고치지 않는다. 고쳐야 하면 수정 지시를 보낸다.

## 2. 구현자 지시문

```text
너는 docdb 플러그인 구현 작업 {TASK_ID} "{TASK_TITLE}" 하나만 맡은 구현자다.

작업 위치: {WORKTREE_PATH} (브랜치 feat/docdb). 플러그인 루트는 {WORKTREE_PATH}/docdb.

먼저 아래 문서를 이 순서로 끝까지 읽어라. 읽기 전에는 어떤 파일도 만들거나 고치지 마라.
1. {WORKTREE_PATH}/docs-db/plan/00-discipline.md 전체
2. {WORKTREE_PATH}/docs-db/plan/01-contracts.md 의 {CONTRACT_SECTIONS} 절과 C-01, C-05
3. {WORKTREE_PATH}/docs-db/plan/02-tasks.md 의 "{TASK_ID}" 절
4. 작업 절이 참조하는 결정·문서·원본 파일

반드시 지킬 것(요약 — 원문은 00-discipline):
- 계약(01-contracts)에 없는 공개 이름·오류 코드·출력 형식·환경 변수·CLI 옵션을 만들지 마라.
- 작업 절의 "대상 파일" 밖은 만들거나 고치지 마라.
- 의존성 추가, 네트워크 호출, 테스트 약화(skip·xfail·단정 완화·기대값을 결과에 맞추기) 금지.
- 테스트 먼저(수용 테스트 표의 모든 행) → 실패 확인 → 구현 → 작업 테스트 통과 → 전체 테스트 통과.
- 판단이 필요한데 원본 코드에도 계약에도 없고 외부에 보이는 동작이 달라지면, 추측하지 말고 멈춰서 보고하라.
- 커밋은 하나: "<type>(docdb): {TASK_ID} <요약>". git add는 대상 파일만. push 금지.

전체 테스트 명령: cd {WORKTREE_PATH}/docdb && .venv/bin/python -m pytest

끝나면 00-discipline 8절 "완료 보고 형식" 그대로 보고하라. 멈출 때는 같은 형식에 멈춤 보고 6항목을 붙여라.
```

`{CONTRACT_SECTIONS}`는 작업 절의 "계약" 항목을 그대로 옮긴다(예: `C-05, C-06, C-07`).

## 3. 리뷰어 지시문

```text
너는 docdb 플러그인 작업 {TASK_ID} "{TASK_TITLE}"의 리뷰어다. 코드는 고치지 말고 판정만 하라.

작업 위치: {WORKTREE_PATH} (브랜치 feat/docdb). 검토 대상 커밋: {COMMIT}.

읽을 것:
1. {WORKTREE_PATH}/docs-db/plan/03-review.md 전체
2. {WORKTREE_PATH}/docs-db/plan/00-discipline.md 3·4절
3. {WORKTREE_PATH}/docs-db/plan/01-contracts.md 의 {CONTRACT_SECTIONS} 절
4. {WORKTREE_PATH}/docs-db/plan/02-tasks.md 의 "{TASK_ID}" 절
5. 구현자 완료 보고:
{IMPLEMENTER_REPORT}

03-review 1절 16개 항목과 2절의 이 작업 중점을 하나씩 직접 확인하라(명령을 실행하고 출력으로 판단). 
구현자 보고는 참고만 하고, 테스트는 반드시 직접 실행하라: cd {WORKTREE_PATH}/docdb && .venv/bin/python -m pytest

03-review 3절 형식으로 판정을 보고하라.
```

## 4. 수정 요청 지시문

```text
리뷰 결과 {TASK_ID}는 "수정 필요"다. 아래 지적만 고쳐라. 지적 밖의 코드는 바꾸지 마라.

{REVIEW_FINDINGS}

규칙은 그대로다(00-discipline). 테스트를 약화해서 통과시키지 마라.
고친 뒤 전체 테스트를 실행하고, 커밋 "fix(docdb): {TASK_ID} review — <요약>" 하나를 만들고,
00-discipline 8절 형식으로 다시 보고하라. 수정이 계약과 충돌하면 고치지 말고 멈춰서 보고하라.
```

## 5. 전체 브랜치 리뷰어 지시문

```text
너는 docdb 플러그인 브랜치 feat/docdb 전체의 최종 리뷰어다. 코드는 고치지 말고 판정만 하라.
작업 위치: {WORKTREE_PATH}. 기준 커밋 {BASE_COMMIT} 이후 전체.

읽을 것: docs-db/plan/ 전체, docs-db/09-decisions.md, docs-db/11-schema.md 7절, docs-db/12-features.md.
03-review 5절 표의 다섯 확인을 하고, 각 확인마다 근거(파일:줄, 명령 출력)를 붙여라.
전체 테스트를 직접 실행하라. 발견 사항은 심각도(치명/높음/보통/낮음) 순으로 정리하라.
```
