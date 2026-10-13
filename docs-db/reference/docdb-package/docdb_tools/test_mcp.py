"""MCP 래퍼 검증: 도구 정의 크기(매 요청에 실리는 고정 비용)와 호출 결과
사용: python3 test_mcp.py <test_token.py 가 만든 db>   (pip install mcp 필요)
"""
import os, sys, json, asyncio
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ['DOCDB_PATH'] = sys.argv[1]
os.environ['DOCDB_ACTOR'] = 'ai-claude'
import docdb_mcp
from docctl_llm import tok_est

async def main():
    try:
        from mcp.client.client import Client          # mcp>=2
        async with Client(docdb_mcp.mcp) as c:
            tools = (await c.list_tools()).tools
            res = await c.call_tool('docdb_ctx', {'work_id': 'WORK-2026-0001', 'budget': 800})
    except ImportError:                                # mcp<2
        from mcp.shared.memory import create_connected_server_and_client_session as connect
        async with connect(docdb_mcp.mcp._mcp_server) as c:
            tools = (await c.list_tools()).tools
            res = await c.call_tool('docdb_ctx', {'work_id': 'WORK-2026-0001', 'budget': 800})
    defs = [{'name': t.name, 'description': t.description, 'input_schema': getattr(t, 'input_schema', None) or getattr(t, 'inputSchema', None)} for t in tools]
    s = json.dumps(defs, ensure_ascii=False)
    print(f'도구 {len(defs)}개 정의: {len(s):,}자 / tok_est {tok_est(s):,}')
    for t in defs:
        print(f"  {t['name']:<12} {tok_est(json.dumps(t, ensure_ascii=False)):>4} tok")
    texts = [b.text for b in res.content if getattr(b, 'type', '') == 'text']
    print('결과 블록 수:', len(res.content), '/ structuredContent:', bool(getattr(res, 'structured_content', None) or getattr(res, 'structuredContent', None)))
    print('\n'.join(texts[0].splitlines()[:6]))
    ok = len(defs) == 6 and len(res.content) == 1 and texts[0].startswith('#ctx')
    print('MCP TEST', 'OK' if ok else 'FAIL')

asyncio.run(main())
