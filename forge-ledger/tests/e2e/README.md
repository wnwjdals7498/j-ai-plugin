# 실서버 끝단 시험 (E2E, 처음부터 끝까지 시험)

`run_p1_e2e.py`는 임시 git 저장소에서 CLI 전체 흐름을 실제 Plane 서버에 실행한다.
`unittest discover`는 `test_*` 파일만 찾으므로 이 스크립트는 자동 테스트에 포함되지 않는다.

## 실행 방법

필요한 환경 변수 3개 (값은 파일에 적지 않는다):

- `FORGE_LEDGER_PLANE_URL`: Plane 주소
- `FORGE_LEDGER_PLANE_WORKSPACE`: 워크스페이스 이름
- `FORGE_LEDGER_PLANE_TOKEN`: API 토큰

토큰은 짧은 만료(예: 2시간)로 만들어 셸 변수로만 쓰고 파일에 저장하지 않는다. 시험 뒤 토큰을 삭제한다.

```bash
export FORGE_LEDGER_PLANE_URL=...        # 값은 환경 문서에서 확인
export FORGE_LEDGER_PLANE_WORKSPACE=...
export FORGE_LEDGER_PLANE_TOKEN=...      # 짧은 만료 토큰
python3 tests/e2e/run_p1_e2e.py
```

- 성공: 마지막 줄 `E2E OK`, 종료 코드 0
- 환경 변수 누락: 안내 한 줄, 종료 코드 2
- 실패: `E2E FAIL <단계 이름>: <종료 코드>`, 종료 코드 1

## 동작

1. 시작 전 `E2EA`·`E2EB` 프로젝트가 이미 있으면 중단한다(기존 프로젝트를 지우지 않기 위함).
2. `init` → `plan new` 2회 → `tests/fixtures/filled/` 절을 `plan put` → 저장소 문서 덮기 → `plan check --project E2EA --json`.
3. `ledger.yml`, `history.md`, Plane 항목·라벨·module을 확인한다.
4. `finally`에서 이 실행이 만든 프로젝트(`E2EA`, `E2EB`, 유지보수 프로젝트)와 임시 폴더만 지운다.
