#!/usr/bin/env python3
"""1부 프로토타입 실서버 끝단 시험.

임시 git 저장소에서 CLI를 subprocess로 실행하고 Plane 결과를 확인한 뒤,
이 실행이 만든 Plane 프로젝트만 지운다. 토큰과 주소는 출력하지 않는다.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, Optional

ROOT = Path(__file__).resolve().parent.parent.parent
PLUGIN_ROOT = ROOT / "plugins" / "forge-ledger"
CLI = PLUGIN_ROOT / "scripts" / "ledger.py"
FILLED_DIR = ROOT / "tests" / "fixtures" / "filled"
ENV_NAMES = ("FORGE_LEDGER_PLANE_URL", "FORGE_LEDGER_PLANE_WORKSPACE", "FORGE_LEDGER_PLANE_TOKEN")
KEY_A = "E2EA"
KEY_B = "E2EB"
TITLE_A = "시험 1"
TITLE_B = "시험 2"
UNIT_CONTAINERS = {
    "PRD": tuple(f"PRD{n}" for n in range(1, 10)),
    "IMPL2": ("IMPL2",),
    "IMPL3": ("IMPL3",),
    "IMPL5": ("IMPL5",),
    "IMPL7": ("IMPL7",),
}

sys.path[:] = [str(PLUGIN_ROOT), *(p for p in sys.path if p != str(PLUGIN_ROOT))]

from ledger.config import load_config  # noqa: E402
from ledger.flows import plane_project_name  # noqa: E402
from ledger.history import parse_history  # noqa: E402
from ledger.plane import PlaneClient, PlaneSettings  # noqa: E402
from ledger.sections import split_html_sections  # noqa: E402
from ledger.spec import UNIT_ITEMS  # noqa: E402

PLANE_TITLE_A = plane_project_name(TITLE_A)
PLANE_TITLE_B = plane_project_name(TITLE_B)


class E2EFailure(Exception):
    """시험 단계 실패. 메시지는 단계 이름과 종료 코드만 담는다."""


def _fail(step: str, detail: str = "") -> E2EFailure:
    return E2EFailure(f"{step}: {detail}" if detail else step)


def _run_cli(repo: Path, step: str, *args: str, expect: int = 0) -> str:
    proc = subprocess.run(
        [sys.executable, str(CLI), "--repo", str(repo), *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ.copy(),
    )
    if proc.returncode != expect:
        raise _fail(step, f"종료 코드 {proc.returncode}")
    return proc.stdout


def _expect(condition: bool, step: str) -> None:
    if not condition:
        raise _fail(step, "기대 불일치")


def _safe_delete(client: PlaneClient, key: str, name: str, before_ids: set[str]) -> bool:
    """이번 실행이 만든 프로젝트(식별자·이름 일치, 시작 전 목록에 없음)만 지운다."""
    for proj in client.list_projects():
        pid = str(proj.get("id", ""))
        if proj.get("identifier") == key and proj.get("name") == name and pid not in before_ids:
            client.delete_project(pid)
            return True
    return False


def _cleanup(
    client: Optional[PlaneClient],
    before_ids: Optional[set[str]],
    repo: Path,
    maint_title: str,
) -> list[str]:
    problems: list[str] = []
    if client is not None and before_ids is not None:
        keys = {KEY_A: PLANE_TITLE_A, KEY_B: PLANE_TITLE_B}
        cfg_path = repo / ".ledger" / "ledger.yml"
        try:
            cfg = load_config(cfg_path)
            keys[cfg.default_project().key] = maint_title
        except Exception:
            try:
                for proj in client.list_projects():
                    if proj.get("name") == maint_title and str(proj.get("id", "")) not in before_ids:
                        keys[str(proj.get("identifier", ""))] = maint_title
            except Exception:
                problems.append("정리: 유지보수 프로젝트 조회 실패")
        for key, name in keys.items():
            if not key:
                continue
            try:
                _safe_delete(client, key, name, before_ids)
            except Exception:
                problems.append(f"정리: 프로젝트 삭제 실패 {key}")
    shutil.rmtree(repo, ignore_errors=True)
    return problems


def _put_fixtures(repo: Path, step_prefix: str) -> None:
    work = repo.parent / f"{repo.name}-bodies"
    work.mkdir()
    try:
        for container, codes in UNIT_CONTAINERS.items():
            sections = split_html_sections((FILLED_DIR / f"{container}.html").read_text(encoding="utf-8"))
            for code in codes:
                _expect(code in sections, f"{step_prefix} {code} fixture")
                body_path = work / f"{code}.html"
                body_path.write_text(sections[code], encoding="utf-8")
                _run_cli(repo, f"{step_prefix} {code}", "plan", "put", KEY_A, code, "--from", str(body_path))
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _verify_plane(client: PlaneClient) -> None:
    projects = {str(p.get("identifier")): p for p in client.list_projects()}
    _expect(KEY_A in projects, "Plane E2EA 존재")
    pid = str(projects[KEY_A]["id"])
    names = sorted(str(i.get("name")) for i in client.list_items(pid))
    _expect(names == sorted(UNIT_ITEMS.values()), "Plane 계획서 항목 6개")
    list_paginated: Callable[[str], list[dict[str, Any]]] = client._list_paginated
    path = client._projects_path
    labels = [str(x.get("name")) for x in list_paginated(path(pid, "labels"))]
    modules = [str(x.get("name")) for x in list_paginated(path(pid, "modules"))]
    _expect("계획서" in labels, "Plane 계획서 라벨")
    _expect("계획서" in modules, "Plane 계획서 module")


def _main() -> int:
    missing = [n for n in ENV_NAMES if not os.environ.get(n, "").strip()]
    if missing:
        print(f"환경 변수 필요: {', '.join(ENV_NAMES)} (README.md 참고)")
        return 2

    repo = Path(tempfile.gettempdir()) / f"forge-e2e-{secrets.token_hex(4)}"
    maint_title = plane_project_name(f"{repo.name} 유지보수")
    client: Optional[PlaneClient] = None
    before_ids: Optional[set[str]] = None
    try:
        client = PlaneClient(PlaneSettings.from_env(os.environ))
        existing = client.list_projects()
        if any(p.get("identifier") in (KEY_A, KEY_B) for p in existing):
            client = None  # 선재 프로젝트를 지우지 않도록 정리를 막는다.
            raise _fail("사전 점검", "E2EA 또는 E2EB가 이미 있음")
        before_ids = {str(p.get("id", "")) for p in existing}

        repo.mkdir()
        proc = subprocess.run(["git", "init", "-q", str(repo)], capture_output=True, check=False)
        if proc.returncode != 0:
            raise _fail("git init", f"종료 코드 {proc.returncode}")

        _run_cli(repo, "init", "init")
        _run_cli(repo, "plan new E2EA", "plan", "new", TITLE_A, "--key", KEY_A)
        _run_cli(repo, "plan new E2EB", "plan", "new", TITLE_B, "--key", KEY_B)
        _put_fixtures(repo, "plan put")
        shutil.copytree(FILLED_DIR / ".ledger", repo / ".ledger", dirs_exist_ok=True)

        out = _run_cli(repo, "plan check", "plan", "check", "--project", KEY_A, "--json")
        try:
            report = json.loads(out)
        except json.JSONDecodeError:
            raise _fail("plan check", "JSON 파싱 실패") from None
        _expect(report.get("exit_code") == 0, "plan check 보고 종료 코드 0")

        cfg = load_config(repo / ".ledger" / "ledger.yml")
        _expect(len(cfg.projects) == 3, "ledger.yml 프로젝트 3개")
        _expect(cfg.projects[0].default is True, "ledger.yml 첫째 default")
        _expect({p.key for p in cfg.projects[1:]} == {KEY_A, KEY_B}, "ledger.yml 키 목록")
        history = parse_history((repo / ".ledger" / "history.md").read_text(encoding="utf-8"))
        _expect(len(history) == 2, "history 줄 2개")
        _verify_plane(client)
        result = 0
    except E2EFailure as exc:
        print(f"E2E FAIL {exc}")
        result = 1
    except Exception as exc:
        print(f"E2E FAIL 예외 {type(exc).__name__}")
        result = 1
    finally:
        problems = _cleanup(client, before_ids, repo, maint_title)
        for problem in problems:
            print(problem)
        if problems:
            result = 1
    if result == 0:
        print("E2E OK")
    return result


if __name__ == "__main__":
    sys.exit(_main())
