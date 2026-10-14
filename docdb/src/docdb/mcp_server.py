"""docdb MCP 서버 — docctl_llm의 얇은 래퍼. 도구 6개만 노출한다."""
import os
from typing import Any, Callable

try:  # mcp>=2
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp<2
    from mcp.server.fastmcp import FastMCP as Server

from docdb import actors, db
from docdb.actors import Actor
from docdb.errors import DocdbError, format_error
from docdb.llm import DocDB

INSTRUCTIONS = (
    "프로젝트 문서 DB(요구사항·Runbook·서버·테스트·작업 이력). "
    "작업을 시작·재개할 때 docdb_ctx 를 먼저 호출. "
    "핸들: d:문서 f:문서/종류/필드 i:항목 w:작업. "
    "응답 첫 줄의 as_of=c:N 을 기억했다가 다음엔 docdb_since 로 변경분만 읽기."
)
_PUT_DESCRIPTION = (
    "쓰기(원자적, 본문 미반환). 패치 키: op 필수, f:·i:·w: 대상은 h(핸들). "
    "필드 f: set|append|replace_section(text, expect, anchor, state, why, sum). "
    "항목 i: upsert. 관계 link(from, rel, to, why)·unlink(rel_id). 작업 w: work·handoff."
)


def _tool(server: Any, description: str | None = None) -> Callable:
    """구조화 출력 없이 MCP 도구를 등록하는 장식자를 반환한다."""

    def register(function: Callable) -> Callable:
        if description is not None:
            function.__doc__ = description
        try:
            return server.tool(structured_output=False)(function)
        except TypeError:
            return server.tool()(function)

    return register


def _invoke(method: str, *args: Any) -> str:
    """행위자를 검사하고 DB를 열어 DocDB 메서드를 호출한다."""
    con = None
    try:
        con = db.open_project_db()
        actor: Actor = actors.resolve_actor(con, None)
        actors.require_ai(actor)
        client = DocDB(
            con,
            actor,
            os.environ.get("DOCDB_WORK"),
            os.environ.get("DOCDB_SESSION"),
            os.environ.get("DOCDB_MODEL"),
        )
        return getattr(client, method)(*args)
    except DocdbError as exc:
        return format_error(exc)
    finally:
        if con is not None:
            con.close()


def build_server() -> Any:
    """여섯 도구를 등록한 MCP 서버를 만든다."""
    server = Server("docdb", instructions=INSTRUCTIONS)

    @_tool(server)
    def docdb_ctx(work_id: str, budget: int = 1500) -> str:
        """작업 시작·재개 묶음: 상태·다음 행동·차단 요인·관련 문서/필드 목록. 예산(tok) 안에서만 반환."""
        return _invoke("ctx", work_id, budget)

    @_tool(server)
    def docdb_get(
        handles: list[str],
        level: int = 1,
        known: dict[str, str] | None = None,
        budget: int = 2000,
    ) -> str:
        """핸들 조회. level 0=한 줄, 1=요약, 2=전문(f: 핸들만). known={handle: dg} 이면 안 바뀐 값은 UNCHANGED."""
        return _invoke("get", handles, level, known, budget)

    @_tool(server)
    def docdb_find(query: str, limit: int = 10) -> str:
        """본문·항목 검색. 핸들과 짧은 발췌만 반환."""
        return _invoke("find", query, limit)

    @_tool(server)
    def docdb_trace(
        handle: str, rel: list[str] | None = None, depth: int = 1
    ) -> str:
        """관계 추적(ID만). rel 예: verified_by, implements, depends_on."""
        return _invoke("trace", handle, rel, depth)

    @_tool(server)
    def docdb_since(cursor: int, work_id: str | None = None) -> str:
        """커서(c:N) 이후 바뀐 것만, 짧은 diff로."""
        return _invoke("since", cursor, work_id)

    @_tool(server, _PUT_DESCRIPTION)
    def docdb_put(patches: list[dict], reason: str | None = None) -> str:
        """put 패치를 저장한다."""
        return _invoke("put", patches, reason)

    return server


def main() -> None:
    """표준 입력·출력으로 MCP 서버를 실행한다."""
    build_server().run()
