"""docdb-handoff.md와 prompts/T*.prompt를 작업 표 하나에서 생성한다.

사용: python gen_handoff.py <저장소 루트> <model-handoff executor-protocol.md 경로>
표(T)를 고치고 다시 실행한다. 생성물은 손으로 고치지 않는다.
"""
import re
import sys
from pathlib import Path

REPO = Path(sys.argv[1])
PROTOCOL = Path(sys.argv[2]).read_text(encoding="utf-8")
OUT = REPO / "docs-db/plan/handoff"
PLAN_REL = "docs-db/plan/handoff/docdb-handoff.md"
WT = "/tmp/model-handoff/j-ai-plugin/t{n}"

D = "docdb/"
T = [
    # id, goal, executor, batch, deps, contracts, decisions, targets, test files, rows
    ("T01", "docdb 패키지 골격, 개발 venv, 고정 버전 파일, pytest 설정을 만든다", "codex", "B01", "없음",
     "C-01, C-02, C-27", "D3, D14",
     ["src/docdb/__init__.py", "pytest.ini", "constraints.txt", "constraints-dev.txt", "tests/conftest.py",
      "tests/unit/test_t01_skeleton.py"], ["tests/unit/test_t01_skeleton.py"], 5),
    ("T02", "오류 모델, 시간, 텍스트 정규화 모듈을 만든다", "agy", "B02", "T01",
     "C-05, C-06, C-07", "D19",
     ["src/docdb/errors.py", "src/docdb/clock.py", "src/docdb/textnorm.py", "tests/conftest.py",
      "tests/unit/test_t02_text.py"], ["tests/unit/test_t02_text.py"], 14),
    ("T03", "데이터 폴더, venv, DB 탐색 경로 모듈을 만든다", "agy", "B03", "T02",
     "C-03, C-04", "D5, D13",
     ["src/docdb/paths.py", "tests/unit/test_t03_paths.py"], ["tests/unit/test_t03_paths.py"], 12),
    ("T04", "pysqlite3 연결, 트랜잭션, 기능 검사, import 경계 테스트를 만든다", "codex", "B04", "T03",
     "C-01, C-08", "D20",
     ["src/docdb/db.py", "tests/unit/test_t04_db.py", "tests/unit/test_import_guard.py"],
     ["tests/unit/test_t04_db.py", "tests/unit/test_import_guard.py"], 11),
    ("T05", "마이그레이션 실행기와 원본 스키마 0001·0002를 만든다", "codex", "B05", "T04",
     "C-08, C-09, C-27", "D21",
     ["src/docdb/migrate.py", "src/docdb/schema/0001_base.sql", "src/docdb/schema/0002_token_ext.sql",
      "src/docdb/db.py", "tests/conftest.py", "tests/unit/test_t05_migrate.py"],
     ["tests/unit/test_t05_migrate.py"], 8),
    ("T06", "마이그레이션 0003~0008과 스키마 동작 테스트를 만든다", "codex", "B06", "T05",
     "C-09, C-10", "D6, D7, D10, D17, D18",
     ["src/docdb/schema/0003_relation_status.sql", "src/docdb/schema/0004_item_fts_ad.sql",
      "src/docdb/schema/0005_import_segment.sql", "src/docdb/schema/0006_catalogue_ext.sql",
      "src/docdb/schema/0007_id_formats.sql", "src/docdb/schema/0008_call_log_session.sql",
      "tests/helpers.py", "tests/unit/test_t06_schema.py"], ["tests/unit/test_t06_schema.py"], 17),
    ("T07", "change_log 이력 기록·검증 모듈을 만든다", "codex", "B07", "T06",
     "C-15", "D19",
     ["src/docdb/history.py", "tests/unit/test_t07_history.py"], ["tests/unit/test_t07_history.py"], 10),
    ("T08", "ID 형식·발급과 행위자 모듈을 만든다", "agy", "B08", "T07",
     "C-10, C-11", "D17, D18, D24",
     ["src/docdb/ids.py", "src/docdb/actors.py", "tests/unit/test_t08_ids_actors.py"],
     ["tests/unit/test_t08_ids_actors.py"], 14),
    ("T09", "민감 정보 검사, 예외, 외부 전송 정책 모듈을 만든다", "agy", "B09", "T08",
     "C-16", "D8, D23",
     ["src/docdb/policy.py", "tests/unit/test_t09_policy.py"], ["tests/unit/test_t09_policy.py"], 15),
    ("T10", "JSON 카탈로그 로더, 시작 카탈로그, 항목 종류, 미니 카탈로그 픽스처를 만든다", "codex", "B09", "T08",
     "C-23, C-27", "D2, D11, D12",
     ["src/docdb/catalogue.py", "catalogue/starter.json", "catalogue/item_types.json",
      "tests/fixtures/mini_catalogue.json", "tests/conftest.py", "tests/unit/test_t10_catalogue.py"],
     ["tests/unit/test_t10_catalogue.py"], 14),
    ("T11", "백업, CLI 골격, init 등 기반 명령과 통합 테스트 픽스처를 만든다", "codex", "B10", "T09, T10",
     "C-09, C-24, C-27", "D5, D21, D22, D25",
     ["src/docdb/backup.py", "src/docdb/cli.py", "src/docdb/__main__.py", "tests/conftest.py",
      "tests/integration/test_t11_cli_init.py"], ["tests/integration/test_t11_cli_init.py"], 16),
    ("T12", "원본 docctl_llm을 이식해 읽기 5종과 필드 쓰기를 구현하고 원본 토큰 테스트 28건을 옮긴다", "codex", "B11", "T11",
     "C-12, C-13, C-14, C-15, C-16, C-24", "D8, D18, D19, D23",
     ["src/docdb/llm.py", "src/docdb/cli.py", "tests/legacy/test_token_port.py", "tests/unit/test_t12_llm.py"],
     ["tests/legacy/test_token_port.py", "tests/unit/test_t12_llm.py"], 37),
    ("T13", "원본 시나리오 33건과 매핑 검수를 pytest로 옮긴다", "codex", "B12", "T12",
     "C-27", "—",
     ["tests/legacy/test_scenarios_port.py", "tests/legacy/test_audit_port.py"],
     ["tests/legacy/test_scenarios_port.py", "tests/legacy/test_audit_port.py"], 34),
    ("T14", "put에 항목(i:) upsert를 추가한다", "agy", "B13", "T13",
     "C-10, C-14, C-16", "D10, D17",
     ["src/docdb/llm.py", "tests/unit/test_t14_items.py"], ["tests/unit/test_t14_items.py"], 10),
    ("T15", "관계 제안·철회와 사람 확정 CLI를 만든다", "codex", "B14", "T14",
     "C-13, C-14, C-17, C-24", "D10, D11",
     ["src/docdb/relations.py", "src/docdb/llm.py", "src/docdb/cli.py", "tests/unit/test_t15_relations.py"],
     ["tests/unit/test_t15_relations.py"], 17),
    ("T16", "규칙 추출 R1·R2·R3·R5를 쓰기 경로에 연결한다", "codex", "B15", "T15",
     "C-13, C-17", "D10, D11, D12",
     ["src/docdb/relations.py", "src/docdb/llm.py", "tests/integration/test_t16_rules.py"],
     ["tests/integration/test_t16_rules.py"], 11),
    ("T17", "ctx에 확정 대기·필수 연결 누락 줄을 추가하고 trace를 확정 관계로 제한한다", "codex", "B16", "T16",
     "C-13, C-17", "D10",
     ["src/docdb/llm.py", "tests/integration/test_t17_ctx.py"], ["tests/integration/test_t17_ctx.py"], 6),
    ("T18", "작업 생성·상태 변경·인계 작성과 수신 확인을 만든다", "codex", "B17", "T17",
     "C-14, C-18, C-24", "D15, D24, D25",
     ["src/docdb/workflow.py", "src/docdb/llm.py", "src/docdb/cli.py", "tests/integration/test_t18_workflow.py"],
     ["tests/integration/test_t18_workflow.py"], 11),
    ("T19", "사실 입력과 적용 판정을 만든다", "agy", "B18", "T18",
     "C-19, C-24", "O5 기본값",
     ["src/docdb/profile.py", "src/docdb/cli.py", "tests/legacy/test_scenarios_port.py",
      "tests/unit/test_t19_profile.py"], ["tests/unit/test_t19_profile.py", "tests/legacy/test_scenarios_port.py"], 7),
    ("T20", "실행 결과 적재와 규칙 R4를 만든다", "agy", "B19", "T19",
     "C-17, C-20, C-24", "D15",
     ["src/docdb/runs.py", "src/docdb/relations.py", "src/docdb/cli.py", "tests/integration/test_t20_runs.py"],
     ["tests/integration/test_t20_runs.py"], 10),
    ("T21", "import 분할, scan, 규칙 적용을 만든다", "codex", "B20", "T20",
     "C-14, C-21", "D6, D22",
     ["src/docdb/importer.py", "src/docdb/cli.py", "tests/fixtures/import/요구사항.md",
      "tests/fixtures/import/서버 목록.md", "tests/fixtures/import/notes.txt", "tests/fixtures/import/bad.bin",
      "tests/fixtures/import/bom.md", "tests/fixtures/import/crlf.md", "tests/integration/test_t21_import_scan.py"],
     ["tests/integration/test_t21_import_scan.py"], 12),
    ("T22", "import next, label, report를 만든다", "codex", "B21", "T21",
     "C-21", "D6, D8, D22",
     ["src/docdb/importer.py", "src/docdb/cli.py", "tests/integration/test_t22_import_label.py"],
     ["tests/integration/test_t22_import_label.py"], 11),
    ("T23", "템플릿 요약과 put의 sum을 만든다", "agy", "B20", "T20",
     "C-13, C-14, C-22", "D16",
     ["src/docdb/summaries.py", "src/docdb/llm.py", "tests/integration/test_t23_summaries.py"],
     ["tests/integration/test_t23_summaries.py"], 7),
    ("T24", "표준 라이브러리 부트스트랩 docdb_launch.py를 만든다", "codex", "B22", "T22, T23",
     "C-04, C-05, C-24", "D3, D4, D13, D26",
     ["src/docdb_launch.py", "tests/integration/test_t24_launch.py"], ["tests/integration/test_t24_launch.py"], 10),
    ("T25", "MCP 서버를 이식한다", "codex", "B23", "T24",
     "C-03, C-14, C-25", "D3, D18, O21 기본값",
     ["src/docdb/mcp_server.py", "tests/integration/test_t25_mcp.py"], ["tests/integration/test_t25_mcp.py"], 6),
    ("T26", "플러그인 매니페스트, 실행 래퍼, 스킬, 마켓플레이스 항목을 만든다", "codex", "B24", "T25, T00b(메인 실측)",
     "C-26", "D1, D14, D18, D26",
     [".claude-plugin/plugin.json", ".codex-plugin/plugin.json", "codex.mcp.json", "bin/docctl",
      "skills/docdb/SKILL.md", "@.claude-plugin/marketplace.json", "@.agents/plugins/marketplace.json",
      "@README.md", "tests/integration/test_t26_plugin.py"], ["tests/integration/test_t26_plugin.py"], 7),
    ("T27", "Python 3.10~3.14 매트릭스 실행 스크립트를 만든다", "agy", "B22", "T22, T23",
     "C-27", "D9",
     ["tools/matrix.sh"], [], 0),
]


def full(p: str) -> str:
    return p[1:] if p.startswith("@") else D + p


def verify(task) -> str:
    tid, rows, tests = task[0], task[9], task[8]
    if tid == "T27":
        return "`bash -n docdb/tools/matrix.sh && echo syntax-ok` → `syntax-ok`(메인이 별도로 실제 실행)"
    cmd = f"cd docdb && .venv/bin/python ../docs-db/plan/handoff/check_rows.py {tid} {rows} {' '.join(tests)} && .venv/bin/python -m pytest -q"
    expect = f"`rows ok {tid} {rows}/{rows}` 출력, pytest 실패 0"
    if tid == "T26":
        cmd += " && cd .. && claude plugin validate docdb"
        expect += ", `Validation passed`"
    return f"`{cmd}` → {expect}"


COMMON = """# docdb 구현 핸드오프 계획

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

## 태스크
"""

_prev = OUT / "docdb-handoff.md"
DONE = set(re.findall(r"^- \[x\] (T\d+):", _prev.read_text(encoding="utf-8"), re.M)) if _prev.exists() else set()
lines = [COMMON]
for t in T:
    tid, goal, ex, batch, deps, contracts, decisions, targets, tests, rows = t
    tl = ", ".join(f"`{full(p)}`" for p in targets)
    lines.append(f"- [{'x' if tid in DONE else ' '}] {tid}: {goal}")
    lines.append(f"  - 대상: {tl}")
    lines.append(f"  - 배경: 공통 규칙을 따른다. 읽을 것: 02-tasks.md \"{tid}\" 절, 01-contracts.md {contracts}. 결정: {decisions}. "
                 f"의존: {deps}(반입 완료 후 시작). 묶음 {batch}, 지정 실행자 {ex}.")
    lines.append(f"  - 검증: {verify(t)}")
    lines.append("")
(OUT / "docdb-handoff.md").write_text("\n".join(lines), encoding="utf-8")

# executor protocol: "너는 실행자다" line + inline mode section, verbatim
start = PROTOCOL.index("너는 실행자다.")
end = PROTOCOL.index("### 큐 모드")
proto = PROTOCOL[start:end].rstrip() + "\n"

(OUT / "prompts").mkdir(parents=True, exist_ok=True)
for t in T:
    tid, targets = t[0], t[7]
    n = tid[1:].lower()
    wt = WT.format(n=n)
    tl = ", ".join(full(p) for p in targets)
    body = (f"다음 실행자 규약에 따라 {wt}/{PLAN_REL}의 {tid}을 수행하라.\n"
            f"작업 범위는 {tl}뿐이다. 그 외 파일·디렉토리는 절대 건드리지 마라.\n\n" + proto)
    (OUT / "prompts" / f"{tid}.prompt").write_text(body, encoding="utf-8")

# batches summary for README
batches = {}
for t in T:
    batches.setdefault(t[3], []).append(t)
rows = ["| 묶음 | 태스크 | 실행자 | 대상 파일 수 | 수용 행 | 의존 |", "|---|---|---|---:|---:|---|"]
for b in sorted(batches):
    for t in batches[b]:
        rows.append(f"| {b} | {t[0]} {t[1]} | {t[2]} | {len(t[7])} | {t[9]} | {t[4]} |")
print("\n".join(rows))  # README 2절 표에 붙여 넣는다
print("tasks", len(T), "batches", len(batches), "codex", sum(t[2] == "codex" for t in T), "agy", sum(t[2] == "agy" for t in T))
