# docdb 구현 핸드오프 계획

[docs-db/plan/](../README.md)의 구현 계획을 실행자(Codex·Antigravity)에게 나눠 넘기기 위한 model-handoff 계획 파일이다. 형식은 model-handoff `plan-format`을 따른다. 정본은 `docs-db/plan/00-discipline.md`, `01-contracts.md`, `02-tasks.md`이며 이 파일은 그 작업을 실행 단위로 묶은 것이다.

## 공통 규칙 (모든 태스크의 배경)

1. 태스크를 시작하기 전에 `docs-db/plan/00-discipline.md` 전체, `docs-db/plan/01-contracts.md`의 C-01·C-05와 태스크 배경에 적힌 계약 절, `docs-db/plan/02-tasks.md`의 같은 ID 절을 끝까지 읽는다. 구현 내용은 그 문서들이 정한다.
2. `00-discipline.md`를 그대로 따르되, 핸드오프에서는 아래 네 가지만 다르다.
   - a. **커밋하지 않는다**(00-discipline 5절 9번 생략). 메인이 대상 경로만 반입해 커밋한다.
   - b. 개발 venv `docdb/.venv`는 메인이 미리 만들어 둔다(T01 제외). 패키지를 설치·삭제하지 않는다.
   - c. 셸 명령이 거부되어 테스트를 실행할 수 없으면 코드와 테스트를 끝까지 작성한 뒤 마지막 줄에 `TASK_FAIL T<N>: shell denied — 검증 미실행`을 쓴다.
   - d. 완료 보고는 00-discipline 8절 형식(커밋 줄 제외)으로 쓰고, 그 다음 마지막 줄에 `TASK_DONE T<N>` 또는 `TASK_FAIL T<N>: <사유>`를 쓴다.
3. 멈춤 조건(00-discipline 7절)에 걸리면 대상 파일을 더 고치지 말고 멈춤 보고 6항목을 쓴 뒤 `TASK_FAIL T<N>: <한 줄 사유>`.
4. 이 계획 파일, 체크박스, `docs-db/` 아래 문서는 수정하지 않는다.
5. 테스트 함수 이름은 `test_<태스크ID 소문자>_<행 번호>_<설명>`이다(원본 이식 테스트 포함). 검증의 `check_rows.py`가 이 이름으로 행 누락을 검사한다.
6. 경로는 저장소 루트 기준이다. 플러그인 루트는 `docdb/`다.
7. PMT(proj-mgmt-tool) 기록·점유·`start`는 메인이 이미 했다. 실행자는 전역 지침에 PMT 사용 규칙이 있어도 PMT 명령을 실행하지 않고 `~/docs`를 읽거나 쓰지 않는다(PMT 스킬 규칙: 서브에이전트는 PMT 문서·lock을 만지지 않는다).

## 태스크

- [x] T01: docdb 패키지 골격, 개발 venv, 고정 버전 파일, pytest 설정을 만든다
  - 대상: `docdb/src/docdb/__init__.py`, `docdb/pytest.ini`, `docdb/constraints.txt`, `docdb/constraints-dev.txt`, `docdb/tests/conftest.py`, `docdb/tests/unit/test_t01_skeleton.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T01" 절, 01-contracts.md C-01, C-02, C-27. 결정: D3, D14. 의존: 없음(반입 완료 후 시작). 묶음 B01, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T01 5 tests/unit/test_t01_skeleton.py && .venv/bin/python -m pytest -q` → `rows ok T01 5/5` 출력, pytest 실패 0

- [x] T02: 오류 모델, 시간, 텍스트 정규화 모듈을 만든다
  - 대상: `docdb/src/docdb/errors.py`, `docdb/src/docdb/clock.py`, `docdb/src/docdb/textnorm.py`, `docdb/tests/conftest.py`, `docdb/tests/unit/test_t02_text.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T02" 절, 01-contracts.md C-05, C-06, C-07. 결정: D19. 의존: T01(반입 완료 후 시작). 묶음 B02, 지정 실행자 agy.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T02 14 tests/unit/test_t02_text.py && .venv/bin/python -m pytest -q` → `rows ok T02 14/14` 출력, pytest 실패 0

- [x] T03: 데이터 폴더, venv, DB 탐색 경로 모듈을 만든다
  - 대상: `docdb/src/docdb/paths.py`, `docdb/tests/unit/test_t03_paths.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T03" 절, 01-contracts.md C-03, C-04. 결정: D5, D13. 의존: T02(반입 완료 후 시작). 묶음 B03, 지정 실행자 agy.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T03 12 tests/unit/test_t03_paths.py && .venv/bin/python -m pytest -q` → `rows ok T03 12/12` 출력, pytest 실패 0

- [x] T04: pysqlite3 연결, 트랜잭션, 기능 검사, import 경계 테스트를 만든다
  - 대상: `docdb/src/docdb/db.py`, `docdb/tests/unit/test_t04_db.py`, `docdb/tests/unit/test_import_guard.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T04" 절, 01-contracts.md C-01, C-08. 결정: D20. 의존: T03(반입 완료 후 시작). 묶음 B04, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T04 11 tests/unit/test_t04_db.py tests/unit/test_import_guard.py && .venv/bin/python -m pytest -q` → `rows ok T04 11/11` 출력, pytest 실패 0

- [x] T05: 마이그레이션 실행기와 원본 스키마 0001·0002를 만든다
  - 대상: `docdb/src/docdb/migrate.py`, `docdb/src/docdb/schema/0001_base.sql`, `docdb/src/docdb/schema/0002_token_ext.sql`, `docdb/src/docdb/db.py`, `docdb/tests/conftest.py`, `docdb/tests/unit/test_t05_migrate.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T05" 절, 01-contracts.md C-08, C-09, C-27. 결정: D21. 의존: T04(반입 완료 후 시작). 묶음 B05, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T05 8 tests/unit/test_t05_migrate.py && .venv/bin/python -m pytest -q` → `rows ok T05 8/8` 출력, pytest 실패 0

- [x] T06: 마이그레이션 0003~0008과 스키마 동작 테스트를 만든다
  - 대상: `docdb/src/docdb/schema/0003_relation_status.sql`, `docdb/src/docdb/schema/0004_item_fts_ad.sql`, `docdb/src/docdb/schema/0005_import_segment.sql`, `docdb/src/docdb/schema/0006_catalogue_ext.sql`, `docdb/src/docdb/schema/0007_id_formats.sql`, `docdb/src/docdb/schema/0008_call_log_session.sql`, `docdb/tests/helpers.py`, `docdb/tests/unit/test_t06_schema.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T06" 절, 01-contracts.md C-09, C-10. 결정: D6, D7, D10, D17, D18. 의존: T05(반입 완료 후 시작). 묶음 B06, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T06 17 tests/unit/test_t06_schema.py && .venv/bin/python -m pytest -q` → `rows ok T06 17/17` 출력, pytest 실패 0

- [x] T07: change_log 이력 기록·검증 모듈을 만든다
  - 대상: `docdb/src/docdb/history.py`, `docdb/tests/unit/test_t07_history.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T07" 절, 01-contracts.md C-15. 결정: D19. 의존: T06(반입 완료 후 시작). 묶음 B07, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T07 10 tests/unit/test_t07_history.py && .venv/bin/python -m pytest -q` → `rows ok T07 10/10` 출력, pytest 실패 0

- [x] T08: ID 형식·발급과 행위자 모듈을 만든다
  - 대상: `docdb/src/docdb/ids.py`, `docdb/src/docdb/actors.py`, `docdb/tests/unit/test_t08_ids_actors.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T08" 절, 01-contracts.md C-10, C-11. 결정: D17, D18, D24. 의존: T07(반입 완료 후 시작). 묶음 B08, 지정 실행자 agy.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T08 14 tests/unit/test_t08_ids_actors.py && .venv/bin/python -m pytest -q` → `rows ok T08 14/14` 출력, pytest 실패 0

- [x] T09: 민감 정보 검사, 예외, 외부 전송 정책 모듈을 만든다
  - 대상: `docdb/src/docdb/policy.py`, `docdb/tests/unit/test_t09_policy.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T09" 절, 01-contracts.md C-16. 결정: D8, D23. 의존: T08(반입 완료 후 시작). 묶음 B09, 지정 실행자 agy.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T09 15 tests/unit/test_t09_policy.py && .venv/bin/python -m pytest -q` → `rows ok T09 15/15` 출력, pytest 실패 0

- [x] T10: JSON 카탈로그 로더, 시작 카탈로그, 항목 종류, 미니 카탈로그 픽스처를 만든다
  - 대상: `docdb/src/docdb/catalogue.py`, `docdb/catalogue/starter.json`, `docdb/catalogue/item_types.json`, `docdb/tests/fixtures/mini_catalogue.json`, `docdb/tests/conftest.py`, `docdb/tests/unit/test_t10_catalogue.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T10" 절, 01-contracts.md C-23, C-27. 결정: D2, D11, D12. 의존: T08(반입 완료 후 시작). 묶음 B09, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T10 14 tests/unit/test_t10_catalogue.py && .venv/bin/python -m pytest -q` → `rows ok T10 14/14` 출력, pytest 실패 0

- [ ] T11: 백업, CLI 골격, init 등 기반 명령과 통합 테스트 픽스처를 만든다
  - 대상: `docdb/src/docdb/backup.py`, `docdb/src/docdb/cli.py`, `docdb/src/docdb/__main__.py`, `docdb/tests/conftest.py`, `docdb/tests/integration/test_t11_cli_init.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T11" 절, 01-contracts.md C-09, C-24, C-27. 결정: D5, D21, D22, D25. 의존: T09, T10(반입 완료 후 시작). 묶음 B10, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T11 16 tests/integration/test_t11_cli_init.py && .venv/bin/python -m pytest -q` → `rows ok T11 16/16` 출력, pytest 실패 0

- [ ] T12: 원본 docctl_llm을 이식해 읽기 5종과 필드 쓰기를 구현하고 원본 토큰 테스트 28건을 옮긴다
  - 대상: `docdb/src/docdb/llm.py`, `docdb/src/docdb/cli.py`, `docdb/tests/legacy/test_token_port.py`, `docdb/tests/unit/test_t12_llm.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T12" 절, 01-contracts.md C-12, C-13, C-14, C-15, C-16, C-24. 결정: D8, D18, D19, D23. 의존: T11(반입 완료 후 시작). 묶음 B11, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T12 37 tests/legacy/test_token_port.py tests/unit/test_t12_llm.py && .venv/bin/python -m pytest -q` → `rows ok T12 37/37` 출력, pytest 실패 0

- [ ] T13: 원본 시나리오 33건과 매핑 검수를 pytest로 옮긴다
  - 대상: `docdb/tests/legacy/test_scenarios_port.py`, `docdb/tests/legacy/test_audit_port.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T13" 절, 01-contracts.md C-27. 결정: —. 의존: T12(반입 완료 후 시작). 묶음 B12, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T13 34 tests/legacy/test_scenarios_port.py tests/legacy/test_audit_port.py && .venv/bin/python -m pytest -q` → `rows ok T13 34/34` 출력, pytest 실패 0

- [ ] T14: put에 항목(i:) upsert를 추가한다
  - 대상: `docdb/src/docdb/llm.py`, `docdb/tests/unit/test_t14_items.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T14" 절, 01-contracts.md C-10, C-14, C-16. 결정: D10, D17. 의존: T13(반입 완료 후 시작). 묶음 B13, 지정 실행자 agy.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T14 10 tests/unit/test_t14_items.py && .venv/bin/python -m pytest -q` → `rows ok T14 10/10` 출력, pytest 실패 0

- [ ] T15: 관계 제안·철회와 사람 확정 CLI를 만든다
  - 대상: `docdb/src/docdb/relations.py`, `docdb/src/docdb/llm.py`, `docdb/src/docdb/cli.py`, `docdb/tests/unit/test_t15_relations.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T15" 절, 01-contracts.md C-13, C-14, C-17, C-24. 결정: D10, D11. 의존: T14(반입 완료 후 시작). 묶음 B14, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T15 17 tests/unit/test_t15_relations.py && .venv/bin/python -m pytest -q` → `rows ok T15 17/17` 출력, pytest 실패 0

- [ ] T16: 규칙 추출 R1·R2·R3·R5를 쓰기 경로에 연결한다
  - 대상: `docdb/src/docdb/relations.py`, `docdb/src/docdb/llm.py`, `docdb/tests/integration/test_t16_rules.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T16" 절, 01-contracts.md C-13, C-17. 결정: D10, D11, D12. 의존: T15(반입 완료 후 시작). 묶음 B15, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T16 11 tests/integration/test_t16_rules.py && .venv/bin/python -m pytest -q` → `rows ok T16 11/11` 출력, pytest 실패 0

- [ ] T17: ctx에 확정 대기·필수 연결 누락 줄을 추가하고 trace를 확정 관계로 제한한다
  - 대상: `docdb/src/docdb/llm.py`, `docdb/tests/integration/test_t17_ctx.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T17" 절, 01-contracts.md C-13, C-17. 결정: D10. 의존: T16(반입 완료 후 시작). 묶음 B16, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T17 6 tests/integration/test_t17_ctx.py && .venv/bin/python -m pytest -q` → `rows ok T17 6/6` 출력, pytest 실패 0

- [ ] T18: 작업 생성·상태 변경·인계 작성과 수신 확인을 만든다
  - 대상: `docdb/src/docdb/workflow.py`, `docdb/src/docdb/llm.py`, `docdb/src/docdb/cli.py`, `docdb/tests/integration/test_t18_workflow.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T18" 절, 01-contracts.md C-14, C-18, C-24. 결정: D15, D24, D25. 의존: T17(반입 완료 후 시작). 묶음 B17, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T18 11 tests/integration/test_t18_workflow.py && .venv/bin/python -m pytest -q` → `rows ok T18 11/11` 출력, pytest 실패 0

- [ ] T19: 사실 입력과 적용 판정을 만든다
  - 대상: `docdb/src/docdb/profile.py`, `docdb/src/docdb/cli.py`, `docdb/tests/legacy/test_scenarios_port.py`, `docdb/tests/unit/test_t19_profile.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T19" 절, 01-contracts.md C-19, C-24. 결정: O5 기본값. 의존: T18(반입 완료 후 시작). 묶음 B18, 지정 실행자 agy.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T19 7 tests/unit/test_t19_profile.py tests/legacy/test_scenarios_port.py && .venv/bin/python -m pytest -q` → `rows ok T19 7/7` 출력, pytest 실패 0

- [ ] T20: 실행 결과 적재와 규칙 R4를 만든다
  - 대상: `docdb/src/docdb/runs.py`, `docdb/src/docdb/relations.py`, `docdb/src/docdb/cli.py`, `docdb/tests/integration/test_t20_runs.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T20" 절, 01-contracts.md C-17, C-20, C-24. 결정: D15. 의존: T19(반입 완료 후 시작). 묶음 B19, 지정 실행자 agy.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T20 10 tests/integration/test_t20_runs.py && .venv/bin/python -m pytest -q` → `rows ok T20 10/10` 출력, pytest 실패 0

- [ ] T21: import 분할, scan, 규칙 적용을 만든다
  - 대상: `docdb/src/docdb/importer.py`, `docdb/src/docdb/cli.py`, `docdb/tests/fixtures/import/요구사항.md`, `docdb/tests/fixtures/import/서버 목록.md`, `docdb/tests/fixtures/import/notes.txt`, `docdb/tests/fixtures/import/bad.bin`, `docdb/tests/fixtures/import/bom.md`, `docdb/tests/fixtures/import/crlf.md`, `docdb/tests/integration/test_t21_import_scan.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T21" 절, 01-contracts.md C-14, C-21. 결정: D6, D22. 의존: T20(반입 완료 후 시작). 묶음 B20, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T21 12 tests/integration/test_t21_import_scan.py && .venv/bin/python -m pytest -q` → `rows ok T21 12/12` 출력, pytest 실패 0

- [ ] T22: import next, label, report를 만든다
  - 대상: `docdb/src/docdb/importer.py`, `docdb/src/docdb/cli.py`, `docdb/tests/integration/test_t22_import_label.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T22" 절, 01-contracts.md C-21. 결정: D6, D8, D22. 의존: T21(반입 완료 후 시작). 묶음 B21, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T22 11 tests/integration/test_t22_import_label.py && .venv/bin/python -m pytest -q` → `rows ok T22 11/11` 출력, pytest 실패 0

- [ ] T23: 템플릿 요약과 put의 sum을 만든다
  - 대상: `docdb/src/docdb/summaries.py`, `docdb/src/docdb/llm.py`, `docdb/tests/integration/test_t23_summaries.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T23" 절, 01-contracts.md C-13, C-14, C-22. 결정: D16. 의존: T20(반입 완료 후 시작). 묶음 B20, 지정 실행자 agy.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T23 7 tests/integration/test_t23_summaries.py && .venv/bin/python -m pytest -q` → `rows ok T23 7/7` 출력, pytest 실패 0

- [ ] T24: 표준 라이브러리 부트스트랩 docdb_launch.py를 만든다
  - 대상: `docdb/src/docdb_launch.py`, `docdb/tests/integration/test_t24_launch.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T24" 절, 01-contracts.md C-04, C-05, C-24. 결정: D3, D4, D13, D26. 의존: T22, T23(반입 완료 후 시작). 묶음 B22, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T24 10 tests/integration/test_t24_launch.py && .venv/bin/python -m pytest -q` → `rows ok T24 10/10` 출력, pytest 실패 0

- [ ] T25: MCP 서버를 이식한다
  - 대상: `docdb/src/docdb/mcp_server.py`, `docdb/tests/integration/test_t25_mcp.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T25" 절, 01-contracts.md C-03, C-14, C-25. 결정: D3, D18, O21 기본값. 의존: T24(반입 완료 후 시작). 묶음 B23, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T25 6 tests/integration/test_t25_mcp.py && .venv/bin/python -m pytest -q` → `rows ok T25 6/6` 출력, pytest 실패 0

- [ ] T26: 플러그인 매니페스트, 실행 래퍼, 스킬, 마켓플레이스 항목을 만든다
  - 대상: `docdb/.claude-plugin/plugin.json`, `docdb/.codex-plugin/plugin.json`, `docdb/codex.mcp.json`, `docdb/bin/docctl`, `docdb/skills/docdb/SKILL.md`, `.claude-plugin/marketplace.json`, `.agents/plugins/marketplace.json`, `README.md`, `docdb/tests/integration/test_t26_plugin.py`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T26" 절, 01-contracts.md C-26. 결정: D1, D14, D18, D26. 의존: T25, T00b(메인 실측)(반입 완료 후 시작). 묶음 B24, 지정 실행자 codex.
  - 검증: `cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py T26 7 tests/integration/test_t26_plugin.py && .venv/bin/python -m pytest -q && cd .. && claude plugin validate docdb` → `rows ok T26 7/7` 출력, pytest 실패 0, `Validation passed`

- [ ] T27: Python 3.10~3.14 매트릭스 실행 스크립트를 만든다
  - 대상: `docdb/tools/matrix.sh`
  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md "T27" 절, 01-contracts.md C-27. 결정: D9. 의존: T22, T23(반입 완료 후 시작). 묶음 B22, 지정 실행자 agy.
  - 검증: `bash -n docdb/tools/matrix.sh && echo syntax-ok` → `syntax-ok`(메인이 별도로 실제 실행)
