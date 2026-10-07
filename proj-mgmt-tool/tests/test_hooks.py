import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import uuid

import pytest

from pmt.db import Database
from pmt.hooks import (_NAMESPACE, lookup_context, normalize_event, process_hook,
                       process_session_start, replay_pending)
from pmt.util import new_id, utc_now

FIXTURES = Path(__file__).parent / "hook-fixtures"


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_codex_stable_occurrence_ids_and_no_prompt_copy():
    raw = fixture("codex-user-prompt.json")
    one = normalize_event("codex", "UserPromptSubmit", raw, environ={"PMT_INSTALLATION_ID": "install-a"})
    two = normalize_event("codex", "UserPromptSubmit", raw, environ={"PMT_INSTALLATION_ID": "install-a"})
    assert one["normalized_event"]["event_id"] == two["normalized_event"]["event_id"]
    assert one["request_id"] == two["request_id"]
    assert uuid.UUID(one["normalized_event"]["event_id"]).version == 5
    rendered = json.dumps(one).lower()
    assert raw["prompt"] not in rendered
    assert raw["transcript_path"].lower() not in rendered
    assert '"prompt"' not in rendered and '"transcript_path"' not in rendered
    assert one["normalized_event"]["type"] == "prompt_submitted"
    assert one["normalized_event"]["occurred_at"] is None


def test_native_occurrence_namespace_separates_product_and_session():
    raw = fixture("codex-user-prompt.json")
    event_id = normalize_event("codex", "UserPromptSubmit", raw,
                               environ={"PMT_INSTALLATION_ID": "installation-test"})["normalized_event"]["event_id"]
    expected = str(uuid.uuid5(_NAMESPACE, json.dumps(
        ["codex", "installation-test", "codex-session-fixture", "prompt_submitted", "codex-turn-fixture"],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )))
    assert event_id == expected
    assert event_id != normalize_event("claude", "Stop", fixture("claude-stop.json"),
                                      environ={"PMT_INSTALLATION_ID": "installation-test"})["normalized_event"]["event_id"]


def test_missing_occurrence_gets_new_uuid_with_replay_only_scope():
    raw = {"session_id": "codex-session", "hook_event_name": "SessionStart", "source": "startup"}
    a = normalize_event("codex", "SessionStart", raw, environ={"PMT_INSTALLATION_ID": "install-a"})
    b = normalize_event("codex", "SessionStart", raw, environ={"PMT_INSTALLATION_ID": "install-a"})
    assert a["normalized_event"]["event_id"] != b["normalized_event"]["event_id"]
    assert a["normalized_event"]["source_metadata"]["dedup_scope"] == "adapter_replay_only"


def test_opencode_keeps_only_allowlisted_metadata(tmp_path):
    envelope = normalize_event("opencode", "session.idle", fixture("opencode-session-idle.json"),
                              environ={"PMT_CONFIG_ROOT": str(tmp_path / "config")})
    rendered = json.dumps(envelope)
    assert envelope["normalized_event"]["type"] == "session_idle"
    assert envelope["session_id"] == "opencode-session-fixture"
    assert envelope["normalized_event"]["occurred_at"] == "2026-10-01T00:00:00Z"
    assert '"prompt"' not in rendered.lower()
    assert '"title"' not in rendered.lower()
    assert envelope["source"]["installation_id"] == json.loads((tmp_path / "config" / "profile.json").read_text())["environment_id"]


def test_profile_fallback_is_persistent_separate_and_rejects_corruption(tmp_path):
    raw = fixture("codex-user-prompt.json")
    config_a, config_b = tmp_path / "config-a", tmp_path / "config-b"
    env_a = {"PMT_CONFIG_ROOT": str(config_a)}
    first = normalize_event("codex", "UserPromptSubmit", raw, environ=env_a)
    again = normalize_event("codex", "UserPromptSubmit", raw, environ=env_a)
    other = normalize_event("codex", "UserPromptSubmit", raw, environ={"PMT_CONFIG_ROOT": str(config_b)})
    assert first["source"]["installation_id"] == again["source"]["installation_id"]
    assert first["normalized_event"]["event_id"] == again["normalized_event"]["event_id"]
    assert first["normalized_event"]["event_id"] != other["normalized_event"]["event_id"]
    profile = config_a / "profile.json"
    profile.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="left unchanged"):
        normalize_event("codex", "UserPromptSubmit", raw, environ=env_a)
    assert profile.read_text(encoding="utf-8") == "{corrupt"


def test_pending_saved_before_cli_and_removed_only_after_ok(tmp_path):
    raw = fixture("codex-user-prompt.json")
    seen = {}

    def successful(argv, **kwargs):
        seen["argv"] = argv
        seen["envelope"] = json.loads(kwargs["input"])
        pending = tmp_path / "hook-pending" / (seen["envelope"]["normalized_event"]["event_id"] + ".json")
        assert pending.exists()
        assert kwargs["shell"] is False
        assert str((Path(__file__).resolve().parents[1] / "src").resolve()) in kwargs["env"]["PYTHONPATH"].split(__import__("os").pathsep)
        assert argv == ["python-test", "-m", "pmt", "--data-root", str(tmp_path), "--config-root", str(tmp_path / "cfg")]
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true}', stderr=b"")

    process_hook("codex", "UserPromptSubmit", raw, environ={
        "PMT_DATA_ROOT": str(tmp_path), "PMT_CONFIG_ROOT": str(tmp_path / "cfg"),
        "PMT_PYTHON": "python-test", "PATH": "",
    }, run=successful)
    assert not list((tmp_path / "hook-pending").glob("*.json"))


def test_pending_retained_on_cli_failure(tmp_path):
    def failed(argv, **kwargs):
        return SimpleNamespace(returncode=4, stdout=b'{"ok":false}', stderr=b"private details")

    with pytest.raises(RuntimeError):
        process_hook("claude", "Stop", fixture("claude-stop.json"), environ={
            "PMT_DATA_ROOT": str(tmp_path), "PMT_CONFIG_ROOT": str(tmp_path / "cfg"),
            "PATH": "",
        }, run=failed)
    pending = list((tmp_path / "hook-pending").glob("*.json"))
    assert len(pending) == 1
    stored = pending[0].read_text(encoding="utf-8")
    assert "last_assistant_message" not in stored
    assert "private details" not in stored


def test_timeout_leaves_same_pending_envelope_for_replay(tmp_path):
    call_count = 0

    def timeout(argv, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        assert json.loads(kwargs["input"])["request_id"] == saved_request
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true}', stderr=b"")

    raw = fixture("codex-user-prompt.json")
    env = {"PMT_DATA_ROOT": str(tmp_path), "PMT_CONFIG_ROOT": str(tmp_path / "cfg"), "PATH": ""}
    with pytest.raises(subprocess.TimeoutExpired):
        process_hook("codex", "UserPromptSubmit", raw, environ=env, run=timeout)
    pending = list((tmp_path / "hook-pending").glob("*.json"))
    assert len(pending) == 1
    saved_request = json.loads(pending[0].read_text(encoding="utf-8"))["request_id"]
    process_hook("codex", "UserPromptSubmit", raw, environ=env, run=timeout)
    assert not list((tmp_path / "hook-pending").glob("*.json"))


def test_explicit_pending_replay_uses_original_ids(tmp_path):
    raw = {"session_id": "s", "hook_event_name": "SessionStart"}
    env = {"PMT_DATA_ROOT": str(tmp_path), "PMT_CONFIG_ROOT": str(tmp_path / "cfg"), "PATH": ""}
    with pytest.raises(RuntimeError):
        process_hook("codex", "SessionStart", raw, environ=env,
                     run=lambda *args, **kwargs: SimpleNamespace(returncode=4, stdout=b'{"ok":false}', stderr=b""))
    path = next((tmp_path / "hook-pending").glob("*.json"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    calls = []

    def success(argv, **kwargs):
        replayed = json.loads(kwargs["input"])
        calls.append(replayed)
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true}', stderr=b"")

    assert replay_pending(environ=env, run=success) == 1
    assert calls[0]["normalized_event"]["event_id"] == saved["normalized_event"]["event_id"]
    assert calls[0]["request_id"] == saved["request_id"]
    assert not path.exists()


def test_unsupported_native_event_rejected():
    with pytest.raises(ValueError):
        normalize_event("codex", "AssistantMessage", {"session_id": "s"})


def test_sensitive_values_are_excluded_without_banning_prompt_event_type():
    raw = fixture("codex-user-prompt.json")
    raw.update({"access_token": "test-secret-token", "transcript": "secret transcript bytes"})
    envelope = normalize_event("codex", "UserPromptSubmit", raw,
                               environ={"PMT_INSTALLATION_ID": "install-a"})
    rendered = json.dumps(envelope).lower()
    for secret_value in (raw["prompt"], raw["transcript_path"], raw["access_token"], raw["transcript"]):
        assert secret_value.lower() not in rendered
    assert '"prompt"' not in rendered
    assert envelope["normalized_event"]["type"] == "prompt_submitted"


def test_opencode_tool_completion_uses_documented_hook_and_packaged_bridge():
    entry = Path(__file__).resolve().parents[1] / "integrations" / "opencode" / "pmt.js"
    source = entry.read_text(encoding="utf-8")
    assert '"tool.execute.after": async (input)' in source
    assert "input.callID" in source and "input.sessionID" in source
    assert "output.output" not in source and "output.title" not in source
    assert '"-m", "pmt.hooks"' not in source
    assert '"integrations/opencode/bridge.py"' in source
    assert (entry.parent / "bridge.py").exists()


def test_codex_windows_commands_quote_resolved_plugin_root():
    manifest = json.loads((Path(__file__).resolve().parents[1] / "integrations" / "codex" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for handlers in manifest["hooks"].values():
        command = handlers[0]["hooks"][0]["commandWindows"]
        assert '"${PLUGIN_ROOT}/integrations/codex/hook.py"' in command
        assert "%PLUGIN_ROOT%" not in command


def test_claude_command_args_use_official_exec_form():
    root = Path(__file__).resolve().parents[1] / "integrations" / "claude"
    manifest = json.loads((root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    for groups in manifest["hooks"].values():
        hook = groups[0]["hooks"][0]
        assert hook["type"] == "command" and hook["command"] == "${user_config.python_path}"
        assert hook["args"][:2] == ["${CLAUDE_PLUGIN_ROOT}/integrations/claude/hook.py", "--event"]
        if "SessionStart" in hook["args"]:
            assert hook["args"][-1] == "--with-context"
        assert set(hook) == {"type", "command", "args", "timeout"}


def _seed_context(data_root, config_root):
    db = Database(data_root, config_root, busy_timeout_ms=500)
    scope_id, record_id, now = new_id(), new_id(), utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO scopes(id,kind,parent_id,slug,body_json,created_at,updated_at) VALUES(?,'project',NULL,?,'{}',?,?)",
                     (scope_id, "startup-context", now, now))
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
                     "VALUES(?,'work',?,NULL,?,'In Progress',?,1,?,?)",
                     (record_id, scope_id, "Saved PMT context", json.dumps({"content": "Persisted project direction; continue validation.",
                                                                            "next": "Run the acceptance checks."}), now, now))
    return db, scope_id, record_id


def _isolated_env(data_root, config_root):
    src = str(Path(__file__).resolve().parents[1] / "src")
    existing = os.environ.get("PYTHONPATH", "")
    return {**os.environ, "PMT_DATA_ROOT": str(data_root), "PMT_CONFIG_ROOT": str(config_root),
            "PMT_PYTHON": sys.executable,
            "PYTHONPATH": src + (os.pathsep + existing if existing else "")}


def test_session_start_injects_saved_context_from_real_isolated_cli(tmp_path):
    data_root, config_root = tmp_path / "data", tmp_path / "config"
    db, scope_id, _record_id = _seed_context(data_root, config_root)
    other_id, now = new_id(), utc_now()
    with db.write() as conn:
        conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
                     "VALUES(?,'work',?,NULL,?,'Planned',?,1,?,?)",
                     (other_id, scope_id, "Unselected Item", json.dumps({"content": "This must stay outside the selected context."}), now, now))
    env = _isolated_env(data_root, config_root)
    env["PMT_SCOPE_ID"] = scope_id
    env["PMT_RECORD_ID"] = _record_id
    raw = {"session_id": "fresh-codex-session", "hook_event_name": "SessionStart", "source": "startup"}
    hook_script = Path(__file__).resolve().parents[1] / "integrations" / "codex" / "hook.py"
    completed = subprocess.run([sys.executable, str(hook_script), "--event", "SessionStart", "--with-context"],
                               input=json.dumps(raw), text=True, capture_output=True, env=env,
                               timeout=8, check=False)
    assert completed.returncode == 0, completed.stderr
    native = json.loads(completed.stdout)
    assert native["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    context = native["hookSpecificOutput"]["additionalContext"]
    assert "metadata overview" in context
    assert '"current_status"' in context
    assert "Persisted project direction" not in context
    assert "Run the acceptance checks" not in context
    assert "This must stay outside the selected context." not in context
    assert "fresh-codex-session" not in context
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='session_started'").fetchone()[0] == 1
    assert not list((data_root / "hook-pending").glob("*.json"))


def test_session_start_without_explicit_scope_records_event_without_context_query(tmp_path):
    data_root, config_root = tmp_path / "data", tmp_path / "config"
    db, _, _ = _seed_context(data_root, config_root)
    calls = []
    real_run = subprocess.run
    def observe(argv, **kwargs):
        calls.append(json.loads(kwargs["input"]))
        return real_run(argv, **kwargs)
    output = process_session_start("claude", {"session_id": "fresh-claude-session",
                                "hook_event_name": "SessionStart", "source": "startup"},
                                environ=_isolated_env(data_root, config_root), timeout=5, run=observe)
    assert output is None
    assert len(calls) == 1 and calls[0]["operation"] == "record_event"
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM events WHERE event_type='session_started'").fetchone()[0] == 1


def test_session_start_context_lookup_failure_uses_warning_channel(tmp_path):
    data_root, config_root = tmp_path / "data", tmp_path / "config"
    _db, _scope_id, _record_id = _seed_context(data_root, config_root)
    env = _isolated_env(data_root, config_root)
    env["PMT_SCOPE_ID"] = new_id()
    native = process_session_start("claude", {"session_id": "fresh-claude-session",
                                "hook_event_name": "SessionStart", "source": "startup"},
                                environ=env, timeout=5)
    assert "hookSpecificOutput" not in native
    assert "scope_not_found" in native["systemMessage"]


def test_opencode_context_bridge_uses_core_cli_and_reports_configuration_errors(tmp_path):
    data_root, config_root = tmp_path / "data", tmp_path / "config"
    _db, scope_id, _record_id = _seed_context(data_root, config_root)
    env = _isolated_env(data_root, config_root)
    env["PMT_SCOPE_ID"] = scope_id
    bridge = Path(__file__).resolve().parents[1] / "integrations" / "opencode" / "bridge.py"
    result = subprocess.run([sys.executable, str(bridge), "--product", "opencode", "--read-context"],
                            input=json.dumps({"session_id": "fresh-opencode-session",
                                              "native_event": "session.created"}), text=True,
                            capture_output=True, env=env, timeout=5, check=False)
    assert result.returncode == 0
    response = json.loads(result.stdout)
    assert response["status"] == "ok"
    assert '"current_status"' in response["context_markdown"]
    assert "Persisted project direction" not in response["context_markdown"]
    env.pop("PMT_SCOPE_ID")
    missing = subprocess.run([sys.executable, str(bridge), "--product", "opencode", "--read-context"],
                             input=json.dumps({"session_id": "fresh-opencode-session",
                                               "native_event": "session.created"}), text=True,
                             capture_output=True, env=env, timeout=5, check=False)
    assert json.loads(missing.stdout)["status"] == "not_configured"


def test_invalid_context_ids_are_warnings_and_never_guess_a_scope(tmp_path):
    env = {"PMT_SCOPE_ID": "not-a-uuid", "PMT_DATA_ROOT": str(tmp_path / "data"),
           "PMT_CONFIG_ROOT": str(tmp_path / "config")}
    result = lookup_context("session", product="claude", environ=env,
                            run=lambda *_args, **_kwargs: pytest.fail("invalid ID must not invoke the CLI"))
    assert result == {"status": "invalid_configuration", "context_markdown": None,
                      "error_code": "invalid_scope_id"}
    output = process_session_start("claude", {"session_id": "session", "hook_event_name": "SessionStart"},
                                   environ=env, run=lambda *_args, **_kwargs: SimpleNamespace(
                                       returncode=0, stdout=b'{"ok":true}', stderr=b""))
    assert output["systemMessage"] == "PMT context unavailable (invalid_scope_id)."
    assert "hookSpecificOutput" not in output


def test_opencode_plugin_uses_official_system_transform_and_explicit_scope_gate():
    source = (Path(__file__).resolve().parents[1] / "integrations" / "opencode" / "pmt.js").read_text(encoding="utf-8")
    assert '"experimental.chat.system.transform": async (input, output)' in source
    assert "input.sessionID" in source and "output.system.push(context)" in source
    assert "PMT_SCOPE_ID" in source and "experimental.chat.system.transform" in source
