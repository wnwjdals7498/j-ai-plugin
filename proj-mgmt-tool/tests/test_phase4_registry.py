"""Public continuity operations preserve protocol-v1 even on incomplete inputs."""
import pytest

from pmt.db import Database
from pmt.phase4 import OPERATIONS
from pmt.service import execute
from pmt.util import new_id


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_registered_operation_has_a_structured_public_boundary(tmp_path, operation):
    db = Database(tmp_path / "data", tmp_path / "config")
    base = {"request_id": new_id(), "protocol_version": 1, "actor": "main", "session_id": "test-main"}
    project, code = execute(db, base | {"operation": "create_scope", "payload": {"kind": "project", "slug": "test"}})
    assert code == 0
    req = base | {"request_id": new_id(), "operation": operation,
                  "scope_id": project["result"]["scope_id"], "payload": {}}
    result, code = execute(db, req)
    assert code in range(6)
    assert set(result) == {"protocol_version", "request_id", "ok", "result", "error", "warnings"}
    assert result["protocol_version"] == 1 and result["request_id"] == req["request_id"]
    if code:
        assert result["ok"] is False and isinstance(result["error"]["code"], str)
    else:
        assert result["ok"] is True and isinstance(result["result"], dict)


@pytest.mark.parametrize("operation", sorted(OPERATIONS))
def test_missing_scope_never_reads_work_content(tmp_path, operation):
    db = Database(tmp_path / "data", tmp_path / "config")
    result, code = execute(db, {"request_id": new_id(), "protocol_version": 1, "actor": "main",
                              "session_id": "test-main", "operation": operation, "payload": {}})
    assert code == 2 and not result["ok"]
    assert result["error"]["code"] == "invalid_identifier"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM continuity_objects").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM continuity_pointers").fetchone()[0] == 0
