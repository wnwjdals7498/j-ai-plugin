"""T04 import 경계 정적 검사."""
import re
from pathlib import Path


STD_SQLITE_IMPORT = r"^\s*(import|from)\s+sqlite3\b"
MCP_IMPORT = re.compile(r"^\s*(?:import|from)\s+mcp\b", re.MULTILINE)


def test_t04_11_import_guard() -> None:
    """src의 pysqlite3, mcp, sqlite3 import 위치가 계약을 지킨다."""
    src_dir = Path(__file__).resolve().parents[2] / "src"
    python_files = sorted(src_dir.rglob("*.py"))
    pysqlite_files: set[str] = set()
    mcp_files: set[str] = set()
    sqlite_imports: list[str] = []

    for path in python_files:
        source = path.read_text(encoding="utf-8")
        relative_path = path.relative_to(src_dir).as_posix()
        if "pysqlite3" in source:
            pysqlite_files.add(relative_path)
        if MCP_IMPORT.search(source):
            mcp_files.add(relative_path)
        if re.search(STD_SQLITE_IMPORT, source, re.MULTILINE):
            sqlite_imports.append(relative_path)

    assert pysqlite_files == {"docdb/db.py"}
    assert mcp_files <= {"docdb/mcp_server.py"}
    assert sqlite_imports == []
