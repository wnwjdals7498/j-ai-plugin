---
name: docdb
description: Use docdb for project document context, change history, decisions, and controlled updates.
---

# docdb

## 언제 쓰나

프로젝트 문서의 기준, 변경 이력, 결정 근거를 docdb로 조회하거나 수정할 때 사용한다. 작업을 시작하거나 이어받을 때 먼저 `docdb_ctx(work_id)`를 호출한다. 승인과 관계 확정은 사람만 한다.

## 설치 (`setup`, `init`)

사용자 데이터 폴더에 실행 환경(venv, 가상 환경)과 의존성을 한 번 설치한다.

```bash
python3 <플러그인>/src/docdb_launch.py setup
docctl init --project <PMT-slug> --name <프로젝트명> --owner u-<계정> --owner-name <이름> --dir <프로젝트 폴더>
```

`setup`은 `docdb_launch.py`에만 있다(`docctl`은 설치된 venv가 있어야 동작하므로 `docctl setup`은 쓸 수 없다). `docctl`을 쓸 수 없는 환경에서는 `<플러그인>/src/docdb_launch.py cli init ...`을 실행한다. Windows는 `py -3 <플러그인>/src/docdb_launch.py setup` 및 `py -3 <플러그인>/src/docdb_launch.py cli init ...`을 쓴다.

## 사용 규칙

# docdb 사용 규칙
- 작업을 시작하거나 이어받으면 먼저 docdb_ctx(work_id). 응답 첫 줄의 as_of=c:N 을 기억한다.
- 다시 돌아왔을 때는 docdb_since(N) 으로 바뀐 것만 본다. 전체를 다시 읽지 않는다.
- 내용이 필요하면 level=1(요약)부터. 고칠 필드만 docdb_get([f:...], level=2).
- 이미 본 필드는 known={handle: dg} 로 물어본다. UNCHANGED 면 기억한 내용을 쓴다.
- 쓰기는 docdb_put 한 번에 묶는다. expect 에 읽을 때 받은 dg 를 넣는다.
- 긴 필드의 일부만 바꿀 때는 op=replace_section(anchor=제목 줄) 또는 append.
- 쓰기 후 확인하려고 다시 읽지 않는다. OK dg=... 가 확인이다.
- CONFLICT 가 나면 docdb_since 로 변경분을 보고 다시 패치한다.
- omitted=N 이 있으면 필요한 것만 핸들로 더 가져온다.
- 승인은 하지 않는다(사람만). 모르는 사실은 tbd/unknown + why 로 남긴다.

## 관계 제안 규칙

관계를 제안할 때 `link`에 근거를 `why`로 적는다. 에이전트는 관계를 확정하지 않는다. `link confirm`과 `link reject`는 사람이 실행한다.

## 작업·인계

에이전트 작업의 진행 상태와 의사 결정은 PMT에서 관리한다. docdb `work`는 문서 변경 묶음이며 `project_id`에는 PMT 프로젝트 slug, `base_refs`에는 PMT Item ID를 기록한다. 필요한 경우 `handoff`를 작성하고 `as_of=c:N`을 포함한다. 수신 확인은 사람이 한다.

## import 절차

1. `docctl import scan <폴더>`로 Markdown·텍스트 파일을 등록하고 제목 기준으로 나눈다.
2. `docctl import next`에서 미분류 구간과 후보를 확인한다. `local_only` 구간은 본문을 요청하지 않는다.
3. `docctl import label '<JSON 배열>'`에 종류·필드 라벨만 기록한다. 원문은 다시 쓰지 않는다.
4. `docctl import report`로 결과를 확인한다. 큰 import는 새 세션이나 서브에이전트로 나눈다.

## 오류 코드 대응표

| 코드 | 대응 |
|---|---|
| `PY_TOO_OLD`, `NO_VENV`, `VENV_MISMATCH`, `DEP_MISSING`, `SQLITE_CAPABILITY` | Python 3.10 이상을 선택하고 `python3 <플러그인>/src/docdb_launch.py setup`(Windows `py -3 …`)을 실행한다. |
| `NO_DB` | 프로젝트 폴더에서 `docctl init`을 실행하거나 `--db` 경로를 지정한다. |
| `DB_EXISTS` | 기존 DB를 덮어쓰지 말고 사용할 DB 경로를 확인한다. |
| `MIGRATION_REQUIRED`, `DB_NEWER` | 사람에게 플러그인과 DB 버전 확인을 요청한다. |
| `BAD_ARGS`, `BAD_HANDLE`, `BAD_OP`, `UNSUPPORTED_HANDLE` | 계약에 맞는 인자와 핸들을 확인해 다시 요청한다. |
| `NOT_FOUND`, `DUPLICATE`, `REJECTED_BEFORE`, `ID_FORMAT`, `CATALOGUE_INCOMPATIBLE`, `IMPORT_STALE` | 대상·ID·카탈로그 또는 import 원본을 다시 확인한다. |
| `NEEDS_HUMAN`, `STATUS_FORBIDDEN` | 사람에게 확정 또는 상태 변경을 요청한다. |
| `SECRET_DETECTED` | 민감 정보를 제거하고 다시 쓴다. |
| `CONFLICT` | `docdb_since(N)`으로 변경을 확인한 뒤 다시 패치한다. |
| `ANCHOR_NOT_FOUND` | 제목 줄과 `anchor`를 확인한다. |
| `SUMMARY_TOO_LONG` | `sum`을 60 tok 이하로 줄인다. |
| `DB` | 입력과 DB 제약을 확인하고, 해결되지 않으면 사람에게 전달한다. |
