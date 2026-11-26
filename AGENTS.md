## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, invoke the `skill` tool with `skill: "graphify"` before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).

## 민감값 노출 검사

이 저장소는 공개 저장소다. IP, 접속 주소, 토큰, 계정 같은 민감값은 코드·문서·테스트·커밋 메시지에 넣지 않는다.

- 민감값 금지, 커밋 전 검사 통과 필수, 예외는 사유와 함께 .exposure-allow에
- 클론한 뒤 한 번 `bash scripts/install-hooks.sh`를 실행한다. 커밋할 때 `forge-ledger/ci/scan_exposure.py --staged`가 돈다.
- 저장소 전체 점검: `python3 forge-ledger/ci/scan_exposure.py --all`
- 실제 값은 허용 목록에 올리지 않는다. 자리표시(`<PMT_HOST_ADDRESS>`), 환경 변수 이름, `~`로 바꾼다.
