"""T25 MCP 서버 통합 수용 테스트."""
import asyncio
import hashlib
import json
import os
import platform
import sys
import sysconfig
from pathlib import Path
from typing import Any, Callable

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def _plugin_root() -> Path:
    """플러그인 루트 경로를 반환한다."""
    return Path(__file__).resolve().parents[2]


def _prepare_home(home: Path) -> None:
    """테스트 데이터 폴더에 개발 venv를 가리키는 링크를 만든다."""
    home.mkdir(parents=True)
    target = home / "venv-target"
    target.mkdir()
    scheme = "nt" if os.name == "nt" else "posix_prefix"
    paths = sysconfig.get_paths(
        scheme=scheme, vars={"base": str(target), "platbase": str(target)}
    )
    target_site = Path(paths["purelib"])
    target_site.mkdir(parents=True)
    dev_site = Path(sysconfig.get_paths()["purelib"])
    if dev_site.is_dir() and os.name != "nt":
        target_site.rmdir()
        target_site.symlink_to(dev_site, target_is_directory=True)
    (home / "venv").symlink_to(target, target_is_directory=True)
    constraints = _plugin_root() / "constraints.txt"
    stamp = {
        "python": platform.python_version(),
        "docdb": "0.1.0",
        "constraints_sha256": hashlib.sha256(constraints.read_bytes()).hexdigest(),
    }
    (target / "docdb-stamp.json").write_text(
        json.dumps(stamp), encoding="utf-8"
    )


def _environment(home: Path, start: Path, actor: str) -> dict[str, str]:
    """MCP 하위 프로세스에 필요한 환경만 지정한다."""
    env = os.environ.copy()
    for key in (
        "DOCDB_HOME",
        "DOCDB_PATH",
        "DOCDB_START_DIR",
        "DOCDB_ACTOR",
        "DOCDB_WORK",
        "DOCDB_SESSION",
        "DOCDB_MODEL",
        "PWD",
        "PYTHONPATH",
    ):
        env.pop(key, None)
    env.update(
        {
            "DOCDB_HOME": str(home),
            "DOCDB_START_DIR": str(start),
            "DOCDB_ACTOR": actor,
        }
    )
    return env


def _run_server(
    home: Path,
    start: Path,
    actor: str,
    action: Callable[[ClientSession, Any], Any],
    extra_env: dict[str, str] | None = None,
) -> Any:
    """SDK stdio 클라이언트로 docdb_launch.py mcp를 실행한다."""
    _prepare_home(home)
    env = _environment(home, start, actor)
    env.update(extra_env or {})
    plugin_root = _plugin_root()
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(plugin_root / "src" / "docdb_launch.py"), "mcp"],
        env=env,
        cwd=plugin_root,
    )

    async def run() -> Any:
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                return await action(session, initialized)

    return asyncio.run(run())


def _text(result: Any) -> str:
    """MCP 결과의 첫 텍스트 블록을 반환한다."""
    blocks = [block.text for block in result.content if block.type == "text"]
    assert len(blocks) == 1
    return blocks[0]


def test_t25_1_tools_keep_original_definitions(tmp_path: Path) -> None:
    """행 1: 도구 6개와 원본 설명, docdb_put 계약 설명을 확인한다."""

    async def inspect(session: ClientSession, _initialized: Any) -> list[Any]:
        return (await session.list_tools()).tools

    tools = _run_server(tmp_path / "home", tmp_path, "ai-claude", inspect)
    expected = {
        "docdb_ctx": "작업 시작·재개 묶음: 상태·다음 행동·차단 요인·관련 문서/필드 목록. 예산(tok) 안에서만 반환.",
        "docdb_get": "핸들 조회. level 0=한 줄, 1=요약, 2=전문(f: 핸들만). known={handle: dg} 이면 안 바뀐 값은 UNCHANGED.",
        "docdb_find": "본문·항목 검색. 핸들과 짧은 발췌만 반환.",
        "docdb_trace": "관계 추적(ID만). rel 예: verified_by, implements, depends_on.",
        "docdb_since": "커서(c:N) 이후 바뀐 것만, 짧은 diff로.",
        "docdb_put": (
            "쓰기(원자적, 본문 미반환). 패치 키: op 필수, f:·i:·w: 대상은 h(핸들). "
            "필드 f: set|append|replace_section(text, expect, "
            "anchor, state, why, sum). 항목 i: upsert. 관계 link(from, rel, to, why)·"
            "unlink(rel_id). 작업 w: work·handoff."
        ),
    }
    assert len(tools) == 6
    assert {tool.name for tool in tools} == set(expected)
    assert {tool.name: tool.description for tool in tools} == expected


def test_t25_2_ctx_returns_one_text_block_without_structured_content(
    tmp_path: Path, project_db: Path
) -> None:
    """행 2: docdb_ctx는 text 한 블록만 반환한다."""

    async def call(session: ClientSession, _initialized: Any) -> Any:
        return await session.call_tool(
            "docdb_ctx", {"work_id": "WORK-2026-0001", "budget": 800}
        )

    result = _run_server(
        tmp_path / "home", project_db.parent.parent, "ai-claude", call
    )
    assert len(result.content) == 1
    assert result.content[0].type == "text"
    assert getattr(result, "structuredContent", None) is None
    assert getattr(result, "structured_content", None) is None


def test_t25_3_missing_db_is_reported_by_tool_call(
    tmp_path: Path,
) -> None:
    """행 3: DB가 없어도 서버는 시작하고 호출에서 NO_DB를 반환한다."""

    async def call(session: ClientSession, _initialized: Any) -> Any:
        return await session.call_tool(
            "docdb_ctx", {"work_id": "WORK-2026-0001", "budget": 800}
        )

    result = _run_server(tmp_path / "home", tmp_path / "empty", "ai-claude", call)
    assert _text(result).startswith("ERROR NO_DB")


def test_t25_4_non_ai_actor_returns_bad_args(
    tmp_path: Path, project_db: Path
) -> None:
    """행 4: 사람 행위자의 도구 호출은 BAD_ARGS를 반환한다."""

    async def call(session: ClientSession, _initialized: Any) -> Any:
        return await session.call_tool(
            "docdb_ctx", {"work_id": "WORK-2026-0001", "budget": 800}
        )

    result = _run_server(
        tmp_path / "home", project_db.parent.parent, "u-tester", call
    )
    assert _text(result).startswith("ERROR BAD_ARGS ")


def test_t25_5_put_persists_value_and_session_ref(
    tmp_path: Path, project_db: Path
) -> None:
    """행 5: docdb_put 저장 값과 환경 세션·모델 기록을 확인한다."""

    async def call(session: ClientSession, _initialized: Any) -> Any:
        return await session.call_tool(
            "docdb_put",
            {
                "patches": [
                    {
                        "h": "i:REQ-HW-009",
                        "op": "upsert",
                        "title": "MCP 저장 확인",
                    }
                ]
            },
        )

    result = _run_server(
        tmp_path / "home",
        project_db.parent.parent,
        "ai-claude",
        call,
        {"DOCDB_SESSION": "session-t25", "DOCDB_MODEL": "model-t25"},
    )
    assert "APPLIED ok=1 err=0" in _text(result)

    from docdb import db

    con = db.connect(project_db)
    try:
        item = con.execute(
            "SELECT title FROM item WHERE item_id=?", ("REQ-HW-009",)
        ).fetchone()
        call_log = con.execute(
            "SELECT session_ref FROM call_log WHERE tool='put' ORDER BY call_id DESC LIMIT 1"
        ).fetchone()
        assert item == ("MCP 저장 확인",)
        assert call_log == ("session-t25/model-t25",)
    finally:
        con.close()


def test_t25_6_server_instructions_match_original(
    tmp_path: Path,
) -> None:
    """행 6: MCP 초기화 안내 문자열을 원본과 비교한다."""

    async def inspect(_session: ClientSession, initialized: Any) -> str:
        return initialized.instructions

    instructions = _run_server(tmp_path / "home", tmp_path, "ai-claude", inspect)
    assert instructions == (
        "프로젝트 문서 DB(요구사항·Runbook·서버·테스트·작업 이력). "
        "작업을 시작·재개할 때 docdb_ctx 를 먼저 호출. "
        "핸들: d:문서 f:문서/종류/필드 i:항목 w:작업. "
        "응답 첫 줄의 as_of=c:N 을 기억했다가 다음엔 docdb_since 로 변경분만 읽기."
    )
