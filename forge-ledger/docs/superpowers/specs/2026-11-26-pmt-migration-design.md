# PMT 이전과 작업 이어받기 설계

- 작성일: 2026-11-26
- 상태: 사용자 승인 대기
- 앞선 설계: `2026-11-25-hub-skeleton-design.md` (D22~D29)

## 1. 목적과 이유

PMT(proj-mgmt-tool, 로컬 마크다운 폴더로 작업을 관리하는 스킬) 형식 프로젝트를 forge-ledger로 옮긴다. 옮긴 뒤에는 작업 기록을 PM 도구(지금은 Plane)와 git에만 남긴다. 목표는 작업하는 컴퓨터나 작업자가 바뀌어도, 저장소를 클론하고 자기 토큰만 넣으면 남은 일·결정 이유·지난 결과를 그대로 이어받는 것이다.

첫 이전 대상은 이 플러그인 자신의 PMT 프로젝트 `~/docs/projects/forge-wiki-plugin`이다. 옮길 git 저장소는 `j-ai-plugin`의 `forge-ledger/`이고, Plane 프로젝트는 `forge-ledger init`이 만든 유지보수 프로젝트다.

이전 대상 규모(2026-11-26 확인): Work·Item 파일 24개, 백로그 11행, 결정 32행, 사실 33행, `resources/derived/` 파일 14개(440K, drawio 6·py 4·html 3·md 1).

## 2. 결정

| ID | 내용 | 결정 주체 |
|---|---|---|
| D30 | 옮긴 뒤 작업 기록의 원천은 forge-ledger 명령이다. 명령이 Plane과 git에 직접 쓴다. 옮긴 프로젝트의 PMT 폴더는 읽기 전용 보관본이 된다 | 사용자 |
| D31 | 끝난 기록까지 전부 옮긴다. 끝난 Item의 인계 메모·검증·결과와 `resources/`를 포함한다 | 사용자 |
| D32 | 공개 저장소에 들어갈 민감값은 모델이 바꿔 쓴다. 커밋 전에 검사하고, push는 사용자가 diff를 본 뒤에만 한다 | 사용자 |
| D33 | 노출 검사 스크립트를 만들어 코드·문서 규칙의 가드로 건다 | 사용자 |
| D34 | 이전은 "모델이 매니페스트 작성 → 사용자 미리 보기 → `ledger migrate apply`가 정해진 규칙대로 쓰기" 순서로 한다 | 사용자 |
| D35 | 과거 git 이력은 새 패턴으로 한 번 검사해 보고만 한다. 실제 민감값이 나오면 이력 수정·강제 push 여부를 그때 사용자에게 묻는다. 앞으로의 변경은 가드로 막는다 | 사용자 |
| D36 | 동시 착수는 최선 보장이다. 착수를 기록한 뒤 다시 읽어 먼저 착수한 사람이 있으면 내 착수를 되돌리고 알린다 | 사용자 |

D23(모델이 조사해 대신 작성, 쓰기는 플러그인 CLI)과 D24(작업 단위 정보는 PM 도구, 결정·사실·환경 이름은 git `.ledger/`, 주소·토큰은 로컬 설정)는 그대로 따른다.

## 3. 목표 · 비목표 · 반목표

목표
- `forge-wiki-plugin`의 모든 기록이 Plane·git으로 옮겨지고, 개수가 원본과 맞는다.
- 같은 매니페스트로 `apply`를 다시 실행하면 새로 만드는 것이 0건이다.
- 다른 컴퓨터에서 저장소를 클론하고 토큰을 넣은 뒤 `ledger next`를 실행하면, 이 서버와 같은 남은 일·최근 결정이 나온다.
- 저장소 전체가 노출 검사를 통과하고, 이 검사가 커밋·PR·CLI 쓰기마다 돈다.

비목표
- 다른 PMT 프로젝트 이전. 같은 도구로 할 수 있게 만들지만 이번에 실행하지는 않는다.
- Linear·GitHub·GitLab 어댑터. 출시 전 별도 작업이다(D25).
- 대시보드 변경. 전면 재작성(B11)은 따로 한다.
- PMT 스킬 자체의 변경.

반목표 (기술적으로 돌아가도 실패로 본다)
- 원래 값이 하나라도 공개 저장소나 GitHub에 올라간다. 치환 기록에도 원래 값을 남기지 않는다.
- 이전 뒤 PMT와 Plane·git 양쪽에 계속 기록해 원천이 둘이 된다.
- 결정의 대체·폐기 계보가 끊겨 "왜 이렇게 됐는지"를 따라가지 못한다.
- 다시 실행할 때 항목이나 댓글이 중복된다.

## 4. 접근 방식

| 방식 | 내용 | 장점 | 단점 |
|---|---|---|---|
| A (선택) | 모델이 조사해 매니페스트 파일을 쓰고, 사용자가 미리 본 뒤 `ledger migrate apply`가 정해진 규칙으로 쓴다 | 쓰기 전에 전체를 본다. 원천 키로 다시 실행해도 중복되지 않는다. 가드를 한 파일에 한 번에 돌린다 | 매니페스트 형식과 apply 명령을 새로 만든다 |
| B | 모델이 `work new`·`decide` 같은 명령을 하나씩 호출한다 | 새 코드가 가장 적다 | 미리 볼 수 없고, 중간 실패 시 반쯤 옮겨진 상태가 남는다 |
| C | Python 변환기가 PMT 파일을 정해진 규칙으로 바로 옮긴다 | 빠르고 결과가 매번 같다 | D23(모델이 조사해 작성)과 어긋나고, PMT v1·v2 형식 차이에 깨진다 |

A를 고른 이유: 옮길 양이 많고, D32에 따라 공개 저장소에 쓰기 전에 사용자가 확인해야 한다.

## 5. 대응표: PMT → Plane · git

| PMT | 옮길 곳 | 규칙 |
|---|---|---|
| `project.md` (Goal·Non-Goal 등) | git `.ledger/project.md` | 본문을 그대로 옮긴다. 저장소 단위 문서이기 때문이다 |
| Work (`WorkN.md`) | Plane 부모 작업 항목, 라벨 `work` | 모듈 대신 부모 항목을 쓴다. Linear 등 다른 PM 도구에도 하위 항목 개념이 있어 어댑터로 옮기기 쉽다 [설계 판단] |
| Item (`WorkN-M.md`) | Plane 하위 작업 항목(`parent`), 라벨 `kind:job`·`kind:test` 등 | 깊이 3까지 그대로 둔다 |
| Item 본문(기본 내용·범위·완료 기준·결과·증거) | 작업 항목 설명(`description_html`)의 같은 이름 절 | |
| Item 상태 | Plane 상태 그룹 | Planned→unstarted, In Progress→started, Done→completed, Paused→unstarted + 라벨 `paused`, Failed·Skipped→cancelled + 라벨 `failed`·`skipped` |
| `blocked_by` | 설명 맨 위 "차단: <코드 목록>" 줄과 라벨 `blocked` | Plane 관계 API를 쓸지는 구현 때 실서버로 확인해 정한다 [빌더 몫] |
| 재개(did·next), worklog 행 | 작업 항목 댓글, 원래 시각을 첫 줄에 적는다 | 시간순으로 만든다 |
| 검증 표 행 | 작업 항목 댓글 "검증: 명령·종료 코드·커밋·제한" | |
| 백로그 행 | Plane 작업 항목, 라벨 `backlog`·`kind:<req…>` | Active→backlog 그룹, Done→completed, 폐기→cancelled |
| 결정 행 | git `.ledger/history.md` + Plane `[계획서] 결정` (기존 `decide` 경로) | 새 ID `D-YYMMDD-XXXX`의 날짜는 원래 생성일이다. 원래 ID(`forge-wiki-plugin D5`)와 상태(승인·대체·폐기)를 남기고, 대체 관계는 새 ID로 다시 잇는다 |
| 사실 행 | git `.ledger/facts.md` (새 파일: ID·상태·생성·내용·출처) | 원래 ID를 유지한다 |
| `resources/` | git `.ledger/resources/pmt/<원래 경로>` | 가드를 통과한 파일만 옮긴다 |
| `RESUME.md` | 옮기지 않는다 | `ledger next`가 Plane·git에서 다시 만든다 |
| 점유(lock) | 옮기지 않는다 | 점유가 살아 있으면 이전을 거부한다 |

## 6. 매니페스트

- 모델이 만드는 JSON 파일 하나다. 커밋하지 않는 로컬 경로(`<XDG_STATE_HOME>/forge-ledger/migrations/<slug>.json`)에 둔다.
- 레코드마다 `key`(원천 키, 예: `pmt:forge-wiki-plugin/_default/Work5-1`), `kind`(project·work·item·comment·backlog·decision·fact·resource), 대상 필드, `redactions`를 가진다.
- `redactions`에는 무엇을 왜 바꿨는지만 적는다(예: "홈 경로 → `~`"). 원래 값은 적지 않는다.
- 명령은 셋이다.
  - `ledger migrate check <파일>`: 형식과 노출 검사를 돌린다. 문제가 있으면 위치를 알리고 0이 아닌 값으로 끝낸다.
  - `ledger migrate preview <파일>`: 종류별 개수, 새로 만들 것·바꿀 것·그대로 둘 것의 개수, 치환 목록, 레코드 견본 몇 개를 보여 준다. 무엇도 쓰지 않는다.
  - `ledger migrate apply <파일>`: check를 다시 통과해야 쓴다.

## 7. 다시 실행해도 중복되지 않게 하기

- Plane 작업 항목은 `external_source=forge-ledger-pmt`, `external_id=<원천 키>`를 단다. 두 필드가 작업 항목에 있음은 실서버 응답으로 확인했다(2026-11-26). 키로 조회하는 방법은 구현 때 실서버로 확인한다.
- 댓글은 본문 끝에 원천 키 표식을 넣어 같은 키가 있으면 건너뛴다. HTML 주석이 저장되지 않으면 눈에 보이는 짧은 표식 줄을 쓴다 [빌더 몫].
- git 쪽은 행의 원래 ID나 파일 경로를 키로 쓴다. 같은 키가 있으면 내용이 다를 때만 고친다.
- 쓰는 순서: project → 결정 → 사실 → resources → Work → Item → 댓글 → 백로그. 중간에 실패하면 다시 실행해 이어 간다.
- git 변경은 브랜치 `migrate/<slug>`에 커밋 하나로 묶는다. 이후 흐름은 지금과 같다. Gitea PR을 열고 검사 7개를 통과한 뒤 병합한다. GitHub push는 사용자가 diff를 본 뒤 요청할 때만 한다.

## 8. 이전 스킬 (`/forge-ledger:migrate`)

모델이 따르는 절차다.

1. PMT 점유가 없는지 확인한다. 있으면 멈추고 알린다.
2. PMT 파일을 읽고, Plane의 기존 항목, git의 `.ledger/`와 로그, 로컬 환경 변수 이름을 조사한다.
3. 대응표(5절)대로 매니페스트를 쓴다. 민감값은 바꿔 쓴다. 주소·토큰은 환경 변수 이름으로, 홈 경로는 `~`로 바꾼다.
4. `migrate check`를 통과할 때까지 고친다.
5. `migrate preview` 결과를 사용자에게 보이고 승인을 받는다.
6. `migrate apply`를 실행한다. 이어서 개수 대조와 재실행 0건을 확인한다.
7. Gitea PR을 연다. 병합과 GitHub push는 사용자 요청 때만 한다.
8. 사용자가 동의하면 PMT 프로젝트를 종료·보관한다(`pmt end <slug> --done --confirm`). 보관본은 읽기 전용이다.

## 9. 작업 명령 (이어받기)

모든 명령은 PM 도구 어댑터를 거친다. 작업 항목은 Plane 항목 코드(예: `FL-12`)로 가리킨다.

| 명령 | 하는 일 |
|---|---|
| `ledger work start <코드>` | 담당자를 나로 정하고 상태를 started로 바꾼다. "착수: <호스트 이름> <시각>" 댓글을 남긴다. 다른 사람이 점유 중이고 마지막 기록이 30분 이내면 거부한다. 30분이 지났으면 `--take`로 넘겨받는다. 기록 뒤 댓글을 다시 읽어 30분 안에 나보다 먼저 남은 다른 착수 댓글이 있으면 담당자를 되돌리고 "착수 취소" 댓글을 남긴 뒤 실패로 끝낸다(D36) |
| `ledger work note <코드> --did … --next …` | 인계 댓글을 남긴다. 항목마다 400자 이하다. 마지막 기록 시각이 점유 신호(heartbeat)가 된다 |
| `ledger work verify <코드> --cmd … (--exit N \| --run)` | 현재 HEAD, 명령, 종료 코드를 댓글로 남긴다 |
| `ledger work end <코드>` | `--done --result …`, `--pause`, `--fail --cause … --fix …`, `--skip --reason …` 중 하나로 끝낸다. done은 현재 HEAD의 verify 기록이 없으면 `--unverified <사유>`가 필요하다 |
| `ledger next` | 점유 중인 일과 마지막 인계, 착수 가능한 일, 최근 결정 3개·사실 3개를 정해진 길이 안에서 보여 준다. SessionStart 요약이 이 출력을 쓴다 |
| `ledger fact add "<내용>" --ref …` | `.ledger/facts.md`에 행을 더한다 |

모든 쓰기는 노출 검사(10절)를 먼저 통과해야 한다.

## 10. 노출 검사 가드

- 패턴은 `ledger/secrets.py` 한 곳에서만 관리한다. 지금 패턴은 IPv4·IPv6와 토큰 5종(Plane·GitHub·GitLab·Figma·Telegram)이다. 다음을 더한다.
  - 홈 경로: `/home/<이름>`, `/data/home/`, `/Users/<이름>`, `C:\Users\`
  - 이메일 주소. `example.com`·`noreply` 주소는 허용한다
  - 내부 호스트 이름: `.local`, `.internal`, `.lan`
  - 개인 키 블록, JWT(로그인 토큰 형식)
  - `TOKEN=`·`PASSWORD:`처럼 비밀값을 대입하는 줄
- 스크립트 `ci/scan_exposure.py`가 지금의 `ci/scan_changed_secrets.py`를 대신한다. 실행 방식은 넷이다.
  - `--staged`: 커밋 직전
  - `--range BASE..HEAD`: PR 검사
  - `--all`: 저장소 전체 점검
  - `--stdin`: Plane에 쓰기 직전 본문 검사
- 검사 범위는 `j-ai-plugin` 저장소 전체다. `model-handoff`, `pmt-server`, `proj-mgmt-tool`도 같은 공개 저장소에 있기 때문이다.
- 예외는 저장소 루트의 `.exposure-allow`에 경로·패턴 이름·사유를 적어야만 허용한다. 지금 `tests/` 폴더 전체를 검사에서 빼는 규칙은 없애고, 테스트용 가짜 값만 허용 목록에 올린다.
- 가드를 거는 곳은 다섯이다.
  1. git pre-commit 훅. `.githooks/pre-commit`과 설치 스크립트를 두고, 클론한 뒤 한 번 실행하면 `core.hooksPath`가 설정된다.
  2. Gitea CI 필수 검사 `secrets`. 범위를 저장소 전체 변경으로 넓힌다.
  3. forge-ledger CLI의 모든 Plane·git 쓰기. 지금 있는 `_check_secrets`를 같은 패턴으로 맞춘다.
  4. `migrate check`·`apply`.
  5. 루트 `AGENTS.md`의 규칙 문장: "민감값 금지, 커밋 전 검사 통과 필수, 예외는 사유와 함께 `.exposure-allow`에".
- 과거 이력 검사(D35): `--history` 실행 방식으로 모든 커밋의 추가 줄을 검사해 커밋·경로·패턴 이름만 보고한다. 값은 출력하지 않는다. 이력을 고치지는 않는다.
- 처음 켤 때 저장소 전체를 `--all`로 한 번 돌린다. 걸린 줄은 고치거나 사유와 함께 허용 목록에 올린 뒤에 CI 범위를 넓힌다.

## 11. 제약

- IP, 접속 주소, 토큰, 계정 같은 민감값은 계획·코드·기록에 넣지 않는다.
- 외부 패키지를 더하지 않는다. 지금 코드처럼 Python 표준 라이브러리만 쓴다. Python 3.10~3.13 매트릭스와 CI 검사 7개를 통과해야 한다.
- push·배포는 사용자가 명시적으로 요청할 때만 한다.
- 결정은 지우지 않는다. 대체·폐기로만 남긴다.

## 12. 성공 확인

1. `migrate preview`의 개수가 원본과 같다(Work·Item 24, 백로그 11, 결정 32, 사실 33, resources 14, 댓글은 원본의 재개·worklog·검증 행 수).
2. `apply`를 두 번째 실행하면 새로 만드는 것이 0건이다.
3. `scan_exposure.py --all`이 저장소 전체에서 통과한다.
4. 임시 폴더에 새로 클론하고 토큰을 넣은 뒤 `ledger next`를 실행하면 이 서버와 같은 남은 일과 최근 결정이 나온다.
5. `work start → note → verify → end` 한 바퀴를 실서버 Plane에서 돌리면 상태와 댓글이 남는다.

## 13. 검토 후 정한 것 (설계 판단, 사용자 확인 대상)

- 기존 Plane 항목과 겹칠 때: 모델이 조사 단계에서 같은 일을 하는 기존 항목을 찾으면, 매니페스트 레코드에 `link_to: <기존 항목 ID>`를 적는다. `apply`는 새로 만들지 않고 기존 항목에 원천 키만 단다. 기존 댓글과 연결은 그대로 둔다. 연결 목록은 `preview`에 따로 보여 사용자가 승인한다. 개수 대조는 "새로 만든 것 + 연결한 것"으로 한다. (예: 허브 골격 T1~T8 항목과 `Work5-1`)
- 이전 뒤 다시 적용할 때: 원천은 Plane·git이다(D30). `apply`는 빠진 레코드만 만든다. 마지막 적용 뒤 Plane·git에서 바뀐 레코드는 덮어쓰지 않고 충돌로 보고한 뒤 건너뛴다.
- Work의 상태와 착수 가능 여부: Work도 Item과 같은 상태 대응표를 쓴다. 자식이 없는 Planned Work는 `ledger next`에서 착수 가능한 일로 보인다.

## 14. 빌더에게 맡기는 것

- Plane에서 `external_id`로 조회하는 방법, 관계(차단) API 사용 여부, 댓글 표식 방식. 모두 실서버로 확인한 뒤 정한다.
- 매니페스트 JSON의 세부 필드 이름.
- `ledger next` 출력의 정확한 길이 상한. 지금 SessionStart 요약 상한 1500자를 기준으로 한다.
