# docdb 핸드오프 실행 안내

[구현 계획](../README.md)의 작업 T01~T27을 model-handoff 플러그인으로 Codex와 Antigravity(agy)에 나눠 넘기기 위한 파일 모음이다. 실행 전 승인이 필요하다(5절).

## 1. 구성

| 파일 | 내용 |
|---|---|
| [docdb-handoff.md](docdb-handoff.md) | model-handoff `plan-format` 계획 파일. 공통 규칙 + 태스크 27개(대상·배경·검증) |
| `prompts/T01.prompt` ~ `T27.prompt` | 실행자에게 넘길 프롬프트. model-handoff 스킬 틀(첫 두 줄 + 실행자 규약 인라인 모드 전문) 그대로, worktree 경로 고정 |
| [check_rows.py](check_rows.py) | 검증 보조: 수용 테스트 표의 모든 행에 `test_<ID>_<행>_` 함수가 있는지 검사 |
| [gen_handoff.py](gen_handoff.py) | 위 계획 파일과 프롬프트를 작업 표 하나에서 생성. 고칠 때는 표를 고치고 다시 생성 |

설계 원칙: 실행자에게는 "무엇을 어떻게 짤지"를 넘기지 않는다. 계획 파일의 공통 규칙이 `00-discipline`·`01-contracts`·`02-tasks`를 읽게 하고, 판정은 검증 명령(행 누락 검사 + 전체 테스트)과 메인의 직접 재실행으로만 한다.

## 2. 실행자 분배 (제안)

2026-10-13 `handoff status`: `agy ready`(`handoff setup agy` 적용), `codex ready`, `claude disabled`. 설정: codex `gpt-6-luna`(effort xhigh), agy `gemini-3.8-flash-medium`, 실행 시간 제한 30분.

분배 기준:

- **codex 18건**: 원본 이식(T12·T13), 스키마·트리거(T05·T06), 이력·CLI·관계 규칙·import·부트스트랩·MCP·패키징처럼 테스트를 여러 번 돌려 맞춰야 하는 작업.
- **agy 9건**: 계약이 함수 서명·규칙을 거의 다 정해 둔 좁은 모듈(T02·T03·T08·T09·T14·T19·T20·T23)과 스크립트(T27).
- **병렬 묶음 3개**: B09(T09 agy ∥ T10 codex), B20(T21 codex ∥ T23 agy), B22(T24 codex ∥ T27 agy). 대상 파일이 겹치지 않고 선행 작업이 끝난 쌍만 묶었다. 나머지는 같은 파일(`cli.py`, `llm.py`, `conftest.py`)을 고치므로 순서대로 진행한다.
- 실행자는 `--only`로 지정한다. 지정 실행자가 사용 한도에 걸리면(`unavailable`) 다른 CLI 실행자로 자동 전환하지 않고, 보고 후 같은 프롬프트를 서브에이전트로 넘긴다(스킬 3절 "서브에이전트 대체").

| 묶음 | 태스크 | 실행자 | 대상 파일 수 | 수용 행 | 의존 |
|---|---|---|---:|---:|---|
| B01 | T01 docdb 패키지 골격, 개발 venv, 고정 버전 파일, pytest 설정을 만든다 | codex | 6 | 5 | 없음 |
| B02 | T02 오류 모델, 시간, 텍스트 정규화 모듈을 만든다 | agy | 5 | 14 | T01 |
| B03 | T03 데이터 폴더, venv, DB 탐색 경로 모듈을 만든다 | agy | 2 | 12 | T02 |
| B04 | T04 pysqlite3 연결, 트랜잭션, 기능 검사, import 경계 테스트를 만든다 | codex | 3 | 11 | T03 |
| B05 | T05 마이그레이션 실행기와 원본 스키마 0001·0002를 만든다 | codex | 6 | 8 | T04 |
| B06 | T06 마이그레이션 0003~0008과 스키마 동작 테스트를 만든다 | codex | 8 | 17 | T05 |
| B07 | T07 change_log 이력 기록·검증 모듈을 만든다 | codex | 2 | 10 | T06 |
| B08 | T08 ID 형식·발급과 행위자 모듈을 만든다 | agy | 3 | 14 | T07 |
| B09 | T09 민감 정보 검사, 예외, 외부 전송 정책 모듈을 만든다 | agy | 2 | 15 | T08 |
| B09 | T10 JSON 카탈로그 로더, 시작 카탈로그, 항목 종류, 미니 카탈로그 픽스처를 만든다 | codex | 6 | 14 | T08 |
| B10 | T11 백업, CLI 골격, init 등 기반 명령과 통합 테스트 픽스처를 만든다 | codex | 5 | 16 | T09, T10 |
| B11 | T12 원본 docctl_llm을 이식해 읽기 5종과 필드 쓰기를 구현하고 원본 토큰 테스트 28건을 옮긴다 | codex | 4 | 37 | T11 |
| B12 | T13 원본 시나리오 33건과 매핑 검수를 pytest로 옮긴다 | codex | 2 | 34 | T12 |
| B13 | T14 put에 항목(i:) upsert를 추가한다 | agy | 2 | 10 | T13 |
| B14 | T15 관계 제안·철회와 사람 확정 CLI를 만든다 | codex | 4 | 17 | T14 |
| B15 | T16 규칙 추출 R1·R2·R3·R5를 쓰기 경로에 연결한다 | codex | 3 | 11 | T15 |
| B16 | T17 ctx에 확정 대기·필수 연결 누락 줄을 추가하고 trace를 확정 관계로 제한한다 | codex | 2 | 6 | T16 |
| B17 | T18 작업 생성·상태 변경·인계 작성과 수신 확인을 만든다 | codex | 4 | 11 | T17 |
| B18 | T19 사실 입력과 적용 판정을 만든다 | agy | 4 | 7 | T18 |
| B19 | T20 실행 결과 적재와 규칙 R4를 만든다 | agy | 4 | 10 | T19 |
| B20 | T21 import 분할, scan, 규칙 적용을 만든다 | codex | 9 | 12 | T20 |
| B20 | T23 템플릿 요약과 put의 sum을 만든다 | agy | 3 | 7 | T20 |
| B21 | T22 import next, label, report를 만든다 | codex | 3 | 11 | T21 |
| B22 | T24 표준 라이브러리 부트스트랩 docdb_launch.py를 만든다 | codex | 2 | 7 | T22, T23 |
| B22 | T27 Python 3.10~3.14 매트릭스 실행 스크립트를 만든다 | agy | 1 | 0 | T22, T23 |
| B23 | T25 MCP 서버를 이식한다 | codex | 2 | 6 | T24 |
| B24 | T26 플러그인 매니페스트, 실행 래퍼, 스킬, 마켓플레이스 항목을 만든다 | codex | 9 | 6 | T25, T00b(메인 실측) |

## 3. 실행 절차 (메인 세션)

### 3.1 준비 (1회)

```bash
P=/home/wnwjdals7498/j-ai-plugin
git -C $P switch -c feat/docdb docs/docdb-design      # 이 handoff 폴더가 커밋된 상태여야 함
mkdir -p /tmp/model-handoff/j-ai-plugin
```

T26 전에 T00a·T00b 실측(02-tasks 0단계)을 메인이 끝내고 계약 C-26 Codex 절을 채운다.

### 3.2 묶음마다

```bash
N=02; T=T02; EX=agy                                    # 2절 표 값
W=/tmp/model-handoff/j-ai-plugin/t$N
git -C $P worktree add -b mh-t$N $W feat/docdb         # 반드시 저장소 밖(/tmp)
# T01 제외: 개발 venv 준비 (실행자는 설치하지 않음)
uv venv $W/docdb/.venv --python /usr/bin/python3.12
uv pip install -p $W/docdb/.venv/bin/python -c $W/docdb/constraints-dev.txt pytest pysqlite3 mcp
cp $P/docs-db/plan/handoff/prompts/$T.prompt $W.prompt
handoff run --only $EX --cwd $W --prompt-file $W.prompt --log $W.log    # Bash run_in_background
```

stdout은 JSON, 종료 코드 `0` 완료 · `1` 실행자 실패 · `3` 모든 실행자 사용 불가.

### 3.3 판정과 반입

센티널(`TASK_DONE`/`TASK_FAIL`)은 참고값이다. 판정은 메인이 직접 한다.

1. `git -C $W status --short -uall` — 대상 밖 변경을 목록화(반입하지 않을 노이즈).
2. worktree에서 계획 파일의 `검증:` 명령을 직접 실행.
3. 통과하면 대상 경로만 반입: `git -C $W add -A -- <대상> && git -C $W diff --cached --binary -- <대상> | git -C $P apply` (`--binary`가 없으면 T21 `bad.bin` 같은 바이너리 파일 반입이 실패한다)
4. 본체에서 `검증:` 재실행(본체 `docdb/.venv`는 T01 반입 직후 3.2와 같은 방법으로 1회 생성).
5. 리뷰: [03-review.md](../03-review.md) 기준으로 [04-prompts.md](../04-prompts.md) 3절 리뷰어 지시문을 Sonnet 서브에이전트에 보낸다. "수정 필요"면 3.4의 재지시로.
6. 통과하면 계획 체크박스를 `[x]`로 바꾸고, 승인된 경우 커밋: `feat(docdb): <ID> <요약>`(대상 경로 + 계획 파일만 `git add`).

### 3.4 실패 처리

- 검증 실패 또는 리뷰 "수정 필요": diff에서 본 문제를 짚는 재지시 프롬프트로 `handoff run --only <같은 실행자>` **1회만**. 재실패면 멈추고 worktree를 남긴 채 경로·diff 요약·사유를 보고한다.
- `unavailable`: `next_available` 시각을 보고하고, 같은 프롬프트를 general-purpose 서브에이전트(Sonnet)에 넘긴다. 첫 줄에 "작업 디렉토리는 /tmp/model-handoff/j-ai-plugin/t<N> 이다"를 덧붙인다. 보고의 실행자 칸은 `claude-subagent`.
- 실행자 실패를 다른 실행자로 돌리지 않는다(사용량 오류만 예외). 태스크당 실행자 호출은 최대 2회.

### 3.5 정리

```bash
git -C $P worktree remove --force $W && git -C $P branch -D mh-t$N
```

마지막에 태스크별 한 줄(실행자, 결과, 검증)을 보고하고 PMT에 기록한다.

## 4. 위험과 대응

| 위험 | 대응 |
|---|---|
| agy는 셸 명령이 거부되어 테스트를 스스로 못 돌림 → 테스트 없이 작성한 코드가 많아짐 | 실행 전 `handoff setup agy`(샌드박스 안 명령 허용) 권장. 안 하면 agy 태스크는 `TASK_FAIL … shell denied`로 끝나고 메인 검증·재지시에 의존 |
| 실행 시간 제한 12분 | 큰 작업(T06, T10, T11, T12, T13, T21, T25)은 시간이 부족할 수 있음. `/model-handoff:setting`으로 30분 권장 |
| worktree를 저장소 안에 두면 agy가 본체를 워크스페이스로 잡음 | `/tmp/model-handoff/` 아래에만 만든다(스킬 2026-08-03 실측) |
| 실행자가 대상 밖을 수정 | 반입은 대상 경로만, 나머지는 버림 |
| 병렬 묶음 반입 충돌 | 대상이 겹치지 않는 쌍만 묶음(생성 시 확인) |
| **Antigravity 약관** | 저장소 README: Antigravity 공식 FAQ는 소비자 요금제에서 agy를 다른 코딩 에이전트 안에서 감싸 쓰는 것을 지원하지 않으며 **계정 정지 사유가 될 수 있다**고 안내. 사용 여부는 사용자 판단. 쓰지 않으려면 agy 9건을 codex로 바꿔 다시 생성(`gen_handoff.py`의 표에서 실행자 값 변경) |

## 5. 실행 전 승인 (2026-10-13 승인 완료)

1. 분배: codex 18 · agy 9, 병렬 3묶음 그대로. Antigravity 약관 위험은 사용자가 감수.
2. `handoff setup agy` 실행(agy 셸 허용): 적용.
3. 실행 시간 제한: 12 → 30분 적용.
4. 커밋: 태스크마다 반입 후 커밋.
5. 브랜치 `feat/docdb` 생성과 이 handoff 폴더 커밋: 승인.
6. T00c·T28(GitHub push 필요): 진행. push 직전에 대상 브랜치를 다시 확인받는다.
