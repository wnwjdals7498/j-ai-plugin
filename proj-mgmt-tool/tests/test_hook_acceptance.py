"""HOOK-01~03 and STOP-01 tests against real PMT CLI child processes and SQLite."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

from pmt.db import Database
from pmt.hooks import process_hook, replay_pending
from pmt.util import new_id, utc_now

FIXTURES = Path(__file__).parent / "hook-fixtures"
ROOT = Path(__file__).resolve().parents[1]


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def environment(data_root: Path, config_root: Path):
    src = str(ROOT / "src")
    existing = os.environ.get("PYTHONPATH", "")
    return {**os.environ, "PMT_DATA_ROOT": str(data_root), "PMT_CONFIG_ROOT": str(config_root),
            "PMT_PYTHON": sys.executable, "PMT_INSTALLATION_ID": str(uuid.uuid4()),
            "PYTHONPATH": src + (os.pathsep + existing if existing else "")}


def create_db(tmp_path):
    data_root, config_root = tmp_path / "data", tmp_path / "config"
    db = Database(data_root, config_root, busy_timeout_ms=500)
    return db, environment(data_root, config_root)


@pytest.mark.parametrize(("product", "event", "fixture_name", "expected_type"), [
    ("codex", "UserPromptSubmit", "codex-user-prompt.json", "prompt_submitted"),
    ("claude", "Stop", "claude-stop.json", "turn_stopped"),
    ("opencode", "session.idle", "opencode-session-idle.json", "session_idle"),
])
def test_hook_01_all_product_fixtures_use_real_cli_and_keep_only_minimal_event(
    tmp_path, product, event, fixture_name, expected_type
):
    db, env = create_db(tmp_path)
    response = process_hook(product, event, fixture(fixture_name), environ=env, timeout=3)
    assert response["ok"] is True
    with db.connect() as conn:
        rows = conn.execute("SELECT event_id,event_type,payload_json FROM events").fetchall()
        assert len(rows) == 1
        assert rows[0]["event_type"] == expected_type
        assert str(uuid.UUID(rows[0]["event_id"])) == rows[0]["event_id"]
        assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == 1
    assert not list((db.root / "hook-pending").glob("*.json"))
    event_text = rows[0]["payload_json"].lower()
    for raw_secret in ("must never be copied", "private/transcript", "last_assistant_message"):
        assert raw_secret not in event_text


@pytest.mark.parametrize(("product", "event", "raw"), [
    ("codex", "AssistantMessage", {"hook_event_name": "AssistantMessage", "session_id": "s"}),
    ("claude", "PreToolUse", {"hook_event_name": "PreToolUse", "session_id": "s"}),
    ("opencode", "session.prompt", {"properties": {"sessionID": "s"}}),
])
def test_hook_03_unsupported_native_event_warns_without_storage(tmp_path, product, event, raw):
    db, env = create_db(tmp_path)
    script = {
        "codex": ROOT / "integrations" / "codex" / "hook.py",
        "claude": ROOT / "integrations" / "claude" / "hook.py",
        "opencode": ROOT / "integrations" / "opencode" / "bridge.py",
    }[product]
    args = [sys.executable, str(script)]
    if product == "opencode":
        args += ["--product", product]
    args += ["--event", event]
    completed = subprocess.run(args, input=json.dumps(raw), text=True, capture_output=True,
                               env=env, timeout=5, check=False)
    assert completed.returncode == 0
    assert "PMT could not confirm event storage" in (completed.stdout + completed.stderr)
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == 0
    assert not (db.root / "hook-pending").exists()


def test_hook_03_pending_directory_write_failure_never_calls_core_or_claims_storage(tmp_path):
    db, env = create_db(tmp_path)
    (db.root / "hook-pending").write_text("blocking file", encoding="utf-8")
    hook_script = ROOT / "integrations" / "codex" / "hook.py"
    completed = subprocess.run([sys.executable, str(hook_script), "--event", "UserPromptSubmit"],
                               input=json.dumps(fixture("codex-user-prompt.json")), text=True,
                               capture_output=True, env=env, timeout=5, check=False)
    assert completed.returncode == 0
    warning = json.loads(completed.stdout)
    assert "systemMessage" in warning and "could not confirm" in warning["systemMessage"]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == 0


def test_hook_01_same_stable_native_occurrence_replays_one_committed_result(tmp_path):
    db, env = create_db(tmp_path)
    cases = [
        ("codex", "UserPromptSubmit", fixture("codex-user-prompt.json")),
        ("claude", "Stop", fixture("claude-stop.json")),
        ("opencode", "session.idle", {
            "properties": {"sessionID": "open-session", "eventID": "idle-occurrence-1",
                           "status": "idle", "time": "2026-10-01T00:00:00Z"},
        }),
    ]
    expected_ids = []
    for product, event, raw in cases:
        first = process_hook(product, event, raw, environ=env, timeout=3)
        second = process_hook(product, event, raw, environ=env, timeout=3)
        assert first["result"]["event_id"] == second["result"]["event_id"]
        expected_ids.append(first["result"]["event_id"])
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 3
        for event_id in expected_ids:
            assert conn.execute("SELECT count(*) FROM events WHERE event_id=?", (event_id,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM requests").fetchone()[0] == 3


def test_hook_01_open_code_without_occurrence_id_only_deduplicates_pending_replay(tmp_path):
    db, env = create_db(tmp_path)
    raw = fixture("opencode-session-idle.json")
    # No eventID is present. Separate native invocations must remain separate events.
    one = process_hook("opencode", "session.idle", raw, environ=env, timeout=3)
    two = process_hook("opencode", "session.idle", raw, environ=env, timeout=3)
    assert one["result"]["event_id"] != two["result"]["event_id"]
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='session_idle'").fetchone()[0] == 2


def _seed_item(db):
    scope_id, record_id, now = new_id(), new_id(), utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,'project',NULL,?,'{}',?,?)",
                     (scope_id, "stop-scope", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
                     "VALUES(?,'item',?,NULL,?,'Planned','{}',1,?,?)",
                     (record_id, scope_id, "Stop must not finish work", now, now))
    return scope_id, record_id


def test_stop_01_reverse_stop_session_end_and_idle_never_change_item_state(tmp_path):
    db, env = create_db(tmp_path)
    _scope_id, record_id = _seed_item(db)
    native = [
        ("codex", "Stop", {"hook_event_name": "Stop", "session_id": "codex-s",
                            "turn_id": "turn-earlier", "stop_hook_active": False}),
        ("codex", "SubagentStop", {"hook_event_name": "SubagentStop", "session_id": "codex-s",
                                    "turn_id": "turn-sub", "agent_id": "agent-a", "agent_type": "worker"}),
        ("claude", "Stop", fixture("claude-stop.json")),
        ("claude", "SessionEnd", {"hook_event_name": "SessionEnd", "session_id": "claude-s",
                                    "source": "other"}),
        ("opencode", "session.idle", {"properties": {"sessionID": "open-s", "eventID": "idle-late",
                                                       "status": "idle", "time": "2026-10-01T00:00:00Z"}}),
    ]
    results = []
    for product, event, raw in reversed(native):
        results.append(process_hook(product, event, raw, environ=env, timeout=3))
    assert {result["result"]["event_type"] for result in results} == {
        "turn_stopped", "subagent_stopped", "session_ended", "session_idle"
    }
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM records WHERE id=?", (record_id,)).fetchone()[0] == "Planned"
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='task_finished'").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM records WHERE kind='decision'").fetchone()[0] == 0


def test_hook_02_real_database_busy_keeps_pending_and_replay_commits_once(tmp_path):
    db, env = create_db(tmp_path)
    raw = fixture("codex-user-prompt.json")
    blocker = db.connect()
    blocker.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(RuntimeError, match="not confirmed"):
            process_hook("codex", "UserPromptSubmit", raw, environ=env, timeout=2)
        pending = list((db.root / "hook-pending").glob("*.json"))
        assert len(pending) == 1
        saved = json.loads(pending[0].read_text(encoding="utf-8"))
        with db.connect() as conn:
            assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (saved["request_id"],)).fetchone()[0] == 0
    finally:
        blocker.rollback()
        blocker.close()
    assert replay_pending(environ=env, timeout=4) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_id=?",
                            (saved["normalized_event"]["event_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (saved["request_id"],)).fetchone()[0] == 1
    assert not list((db.root / "hook-pending").glob("*.json"))


def test_hook_02_actual_cli_timeout_preserves_original_pending_ids_for_replay(tmp_path):
    db, env = create_db(tmp_path)
    shim = tmp_path / "python-startup-delay"
    shim.mkdir()
    marker = tmp_path / "sleep-once.marker"
    (shim / "sitecustomize.py").write_text(
        "import os, time\n"
        "marker = os.environ.get('PMT_TEST_SLEEP_MARKER')\n"
        "if marker and not os.path.exists(marker):\n"
        "    open(marker, 'w', encoding='utf-8').close()\n"
        "    time.sleep(1.0)\n",
        encoding="utf-8",
    )
    env["PYTHONPATH"] = str(shim) + os.pathsep + env["PYTHONPATH"]
    env["PMT_TEST_SLEEP_MARKER"] = str(marker)
    raw = fixture("codex-user-prompt.json")
    with pytest.raises(subprocess.TimeoutExpired):
        process_hook("codex", "UserPromptSubmit", raw, environ=env, timeout=0.2)
    assert marker.exists()
    pending = list((db.root / "hook-pending").glob("*.json"))
    assert len(pending) == 1
    saved = json.loads(pending[0].read_text(encoding="utf-8"))
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events").fetchone()[0] == 0
    assert replay_pending(environ=env, timeout=4) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_id=?",
                            (saved["normalized_event"]["event_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (saved["request_id"],)).fetchone()[0] == 1


def test_hook_02_actual_database_io_failure_keeps_pending_until_repair(tmp_path):
    data_root, config_root = tmp_path / "io-data", tmp_path / "io-config"
    data_root.mkdir()
    # A directory at the DB filename is a real filesystem/open failure.
    blocked_path = data_root / "pmt.sqlite3"
    blocked_path.mkdir()
    env = environment(data_root, config_root)
    with pytest.raises(RuntimeError, match="not confirmed"):
        process_hook("codex", "UserPromptSubmit", fixture("codex-user-prompt.json"), environ=env, timeout=3)
    pending = list((data_root / "hook-pending").glob("*.json"))
    assert len(pending) == 1
    envelope = json.loads(pending[0].read_text(encoding="utf-8"))
    blocked_path.rmdir()  # The empty test obstruction only; no user data is removed.
    db = Database(data_root, config_root)
    assert replay_pending(environ=env, timeout=3) == 1
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_id=?", (envelope["normalized_event"]["event_id"],)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM requests WHERE request_id=?", (envelope["request_id"],)).fetchone()[0] == 1


def test_hook_03_database_and_pending_root_failure_is_visible_without_false_save(tmp_path):
    data_root, config_root = tmp_path / "blocked-data", tmp_path / "config"
    data_root.write_text("both DB and pending are obstructed", encoding="utf-8")
    env = environment(data_root, config_root)
    native = subprocess.run([sys.executable, str(ROOT / "integrations/codex/hook.py"), "--event", "UserPromptSubmit"],
                            input=json.dumps(fixture("codex-user-prompt.json")), text=True,
                            capture_output=True, env=env, timeout=5)
    assert native.returncode == 0 and "could not confirm" in json.loads(native.stdout)["systemMessage"]
    core_request = {"protocol_version": 1, "operation": "setup", "request_id": new_id(),
                    "actor": "main", "session_id": "io-observation", "payload": {}}
    core = subprocess.run([sys.executable, "-m", "pmt"], input=json.dumps(core_request),
                          text=True, capture_output=True, env=env, timeout=5)
    assert core.returncode == 4 and json.loads(core.stdout)["ok"] is False
    assert data_root.read_text(encoding="utf-8") == "both DB and pending are obstructed"
