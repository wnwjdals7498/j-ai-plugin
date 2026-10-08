"""Phase5 registration follows the unchanged Host's real scope relationships."""
import uuid
import pytest

from pmt.http_store import HttpStore
from test_phase3_host_network import live_host

def test_write_only_bootstrap_registers_real_repository_and_project(live_host, monkeypatch):
    env = live_host
    device = env["app"].auth.issue_device("phase5-bootstrap", ["*"], ["write"])
    session = "phase5-bootstrap-session"
    monkeypatch.setenv("PMT_PHASE5_BOOTSTRAP_TEST", device["credential"])
    store = HttpStore(env["base"], "PMT_PHASE5_BOOTSTRAP_TEST", device["device_id"],
                      str(uuid.uuid4()), env["app"].auth.namespace_id, ca_file=str(env["cert"]), timeout=2)
    store.register_session(session)
    def create(kind, slug, parent=None):
        payload = {"kind": kind, "slug": slug}
        if parent:
            payload["parent_id"] = parent
        result, code = store.execute({"protocol_version": 1, "operation": "create_scope",
            "request_id": str(uuid.uuid4()), "actor": "phase5-bootstrap", "session_id": session,
            "payload": payload})
        assert code == 0, result.get("error")
        return result["result"]["id"]
    try:
        environment = create("environment", "phase5-environment")
        repository = create("repository", "phase5-repository", environment)
        project = create("project", "phase5-project", repository)
        with env["db"].connect() as connection:
            row = connection.execute("SELECT kind,parent_id FROM scopes WHERE id=?", (project,)).fetchone()
            assert tuple(row) == ("project", repository)
            assert connection.execute("SELECT kind FROM scopes WHERE id=?", (repository,)).fetchone()[0] == "repository"
    finally:
        env["app"].auth.revoke_device(device["device_id"], 1)
    assert next(d for d in env["app"].auth.list_devices() if d["device_id"] == device["device_id"])["state"] == "revoked"

def test_logical_only_repository_id_rejected_by_existing_workspace_contract(live_host):
    env = live_host
    fake = str(uuid.uuid4())
    with env["db"].connect() as connection:
        revision = connection.execute("SELECT revision FROM execution_runs WHERE id=?", (env["run"],)).fetchone()[0]
    result, code = env["store_a"].execute({"protocol_version": 1, "request_id": str(uuid.uuid4()),
        "operation": "authorize_workspace", "actor": env["actor"], "session_id": env["session_a"],
        "scope_id": env["project"], "payload": {"project_id": env["project"], "repository_id": fake,
            "run_id": env["run"], "expected_run_revision": revision, "canonical_workspace": env["canonical"],
            "relative_graph_path": env["relative"]}})
    assert code != 0 and result["ok"] is False
    assert result["error"]["code"] == "repository_scope_mismatch"
