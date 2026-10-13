"""docdb MCP 서버 — docctl_llm 의 얇은 래퍼. 도구 6개만 노출 (승인·카탈로그 변경·render 는 노출하지 않음)
실행: DOCDB_PATH=docdb.sqlite DOCDB_ACTOR=ai-codex python3 docdb_mcp.py
도구 설명은 짧게, 중요한 것을 앞에 (Claude Code는 설명을 2,048자에서 자름)
"""
import os, json
try:                                              # mcp>=2
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:                               # mcp<2
    from mcp.server.fastmcp import FastMCP as Server
from docctl_llm import DocDB

INSTRUCTIONS = ("프로젝트 문서 DB(요구사항·Runbook·서버·테스트·작업 이력). 작업을 시작·재개할 때 docdb_ctx 를 먼저 호출. "
                "핸들: d:문서 f:문서/종류/필드 i:항목 w:작업. 응답 첫 줄의 as_of=c:N 을 기억했다가 다음엔 docdb_since 로 변경분만 읽기.")

mcp = Server('docdb', instructions=INSTRUCTIONS)

def db():
    return DocDB(os.environ.get('DOCDB_PATH', 'docdb.sqlite'), os.environ.get('DOCDB_ACTOR', 'ai-agent'),
                 os.environ.get('DOCDB_WORK'), os.environ.get('DOCDB_SESSION'))

def tool(fn):
    try:
        return mcp.tool(structured_output=False)(fn)   # text 하나만 반환 (같은 내용을 두 번 싣지 않기 위해)
    except TypeError:
        return mcp.tool()(fn)

@tool
def docdb_ctx(work_id: str, budget: int = 1500) -> str:
    """작업 시작·재개 묶음: 상태·다음 행동·차단 요인·관련 문서/필드 목록. 예산(tok) 안에서만 반환."""
    return db().ctx(work_id, budget)

@tool
def docdb_get(handles: list[str], level: int = 1, known: dict[str, str] | None = None, budget: int = 2000) -> str:
    """핸들 조회. level 0=한 줄, 1=요약, 2=전문(f: 핸들만). known={handle: dg} 이면 안 바뀐 값은 UNCHANGED."""
    return db().get(handles, level, known, budget)

@tool
def docdb_find(query: str, limit: int = 10) -> str:
    """본문·항목 검색. 핸들과 짧은 발췌만 반환."""
    return db().find(query, limit)

@tool
def docdb_trace(handle: str, rel: list[str] | None = None, depth: int = 1) -> str:
    """관계 추적(ID만). rel 예: verified_by, implements, depends_on."""
    return db().trace(handle, rel, depth)

@tool
def docdb_since(cursor: int, work_id: str | None = None) -> str:
    """커서(c:N) 이후 바뀐 것만, 짧은 diff로."""
    return db().since(cursor, work_id)

@tool
def docdb_put(patches: list[dict], reason: str | None = None) -> str:
    """필드 쓰기(원자적). 항목: {h, op:set|append|replace_section, text, expect:dg, anchor, state, why}. 본문은 되돌려주지 않음."""
    return db().put(patches, reason)

if __name__ == '__main__':
    mcp.run()
