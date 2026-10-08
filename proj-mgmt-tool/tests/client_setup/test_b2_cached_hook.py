import io
import json
import sqlite3
import sys

from pmt import easy_hook, easy_setup, hooks
from pmt.client_setup.mode import prepare
from pmt.client_setup.client import write_client_metadata
from pmt.client_setup.credentials import store_credential
from pmt.db import Database


def _stdin(value):
    return io.TextIOWrapper(io.BytesIO(json.dumps(value).encode("utf-8")), encoding="utf-8")


def test_non_session_event_without_cached_profile_warns_without_initializing(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)}
    output = io.StringIO()
    assert easy_hook.run(["--product", "codex", "--event", "UserPromptSubmit"],
                         stdin=_stdin({"session_id": "fixture"}), stdout=output, environ=env) == 0
    assert "not_configured" in output.getvalue()
    assert not (config / "storage.json").exists()
    assert not (data / "pmt.sqlite3").exists()


def test_c02_partial_host_session_start_lists_missing_keys_without_creating_roots(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data),
           "CLAUDE_PLUGIN_OPTION_HOST_URL": "https://host.invalid",
           "CLAUDE_PLUGIN_OPTION_DEVICE_ID": "acd66a06-5995-40f0-a637-e96ffaf52b04",
           "CLAUDE_PLUGIN_OPTION_ACTOR": "fixture",
           "CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL": "secret-fixture"}
    output = io.StringIO()
    native = {"hook_event_name": "SessionStart", "session_id": "incomplete-host"}
    assert easy_hook.run(["--event", "SessionStart"], stdin=_stdin(native), stdout=output, environ=env) == 0
    assert "namespace_id" in output.getvalue()
    assert "secret-fixture" not in output.getvalue()
    assert not config.exists() and not data.exists()


def test_unsupported_event_does_not_run_managed_setup_or_write_event_state(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    output = io.StringIO()
    assert easy_hook.run(["--product", "codex", "--event", "AssistantMessage"],
                         stdin=_stdin({"session_id": "fixture"}), stdout=output,
                         environ={"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)}) == 0
    assert "PMT could not confirm event storage" in output.getvalue()
    assert not (config / "storage.json").exists()
    assert not (data / "pmt.sqlite3").exists()
    assert not (data / "hook-pending").exists()


def test_malformed_session_start_input_does_not_trigger_managed_setup(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    output = io.StringIO()
    assert easy_hook.run(["--product", "codex", "--event", "SessionStart"],
                         stdin=_stdin({"cwd": str(tmp_path)}), stdout=output,
                         environ={"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)}) == 0
    assert output.getvalue()
    assert not (config / "storage.json").exists()
    assert not (data / "pmt.sqlite3").exists()


def test_cached_local_event_calls_core_without_another_setup_write(monkeypatch, tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)}
    prepared = prepare(env, tmp_path, product="claude")
    assert prepared["status"] == "ready" and prepared["mode"] == "local"
    with sqlite3.connect(data / "pmt.sqlite3") as db:
        before = db.execute("SELECT count(*) FROM requests").fetchone()[0]
    observed = []
    monkeypatch.setattr(hooks, "process_hook", lambda product, event, raw, **_kwargs:
                        observed.append((product, event, raw.get("session_id"))))
    output = io.StringIO()
    raw = {"hook_event_name": "Stop", "session_id": "cached-session", "stop_hook_active": False}
    assert easy_hook.run(["--product", "claude", "--event", "Stop"], stdin=_stdin(raw),
                         stdout=output, environ=env) == 0
    assert observed == [("claude", "Stop", "cached-session")]
    with sqlite3.connect(data / "pmt.sqlite3") as db:
        after = db.execute("SELECT count(*) FROM requests").fetchone()[0]
    assert after == before


def test_known_direct_legacy_database_without_storage_profile_uses_synthetic_cache(monkeypatch, tmp_path):
    config, data = tmp_path / "legacy-config", tmp_path / "legacy-data"
    db = Database(data, config)
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data), "PMT_PYTHON": sys.executable}
    assert not (config / "storage.json").exists() and not (config / "client.json").exists()
    with db.connect() as conn:
        before = conn.execute("SELECT count(*) FROM requests").fetchone()[0]
    observed = []
    monkeypatch.setattr(hooks, "process_hook", lambda product, event, _raw, **_kwargs:
                        observed.append((product, event)))
    output = io.StringIO()
    native = {"hook_event_name": "Stop", "session_id": "old-cli-session", "stop_hook_active": False}
    assert easy_hook.run(["--event", "Stop"], stdin=_stdin(native), stdout=output, environ=env) == 0
    assert observed == [("claude", "Stop")]
    assert not (config / "storage.json").exists() and not (config / "client.json").exists()
    with db.connect() as conn:
        after = conn.execute("SELECT count(*) FROM requests").fetchone()[0]
    assert after == before


def test_default_root_without_explicit_config_is_not_claimed_as_legacy(monkeypatch, tmp_path):
    config, data = tmp_path / "default-config", tmp_path / "default-data"
    Database(data, config)
    monkeypatch.setattr(easy_setup, "default_roots", lambda _env: (config, data))
    observed = []
    monkeypatch.setattr(hooks, "process_hook", lambda *_args, **_kwargs: observed.append(True))
    output = io.StringIO()
    raw = {"hook_event_name": "Stop", "session_id": "default-root-session"}
    assert easy_hook.run(["--event", "Stop"], stdin=_stdin(raw), stdout=output, environ={}) == 0
    assert not observed and "not_configured" in output.getvalue()
    assert not (config / "storage.json").exists()
    assert not (config / "client.json").exists()


def test_cached_hosted_event_loads_protected_credential_without_prepare(monkeypatch, tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    profile = {"schema_version": 1, "revision": 1, "mode": "hosted",
        "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
        "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
        "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"}
    (config / "storage.json").write_text(json.dumps(profile), encoding="utf-8")
    store_credential(config, "cached-host-secret")
    write_client_metadata(config, source="plugin", python_path="fixture-python", mode="hosted")
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)}
    observed = []
    monkeypatch.setattr(hooks, "process_hook", lambda product, event, _raw, **_kwargs:
                        observed.append((product, event, env.get("PMT_HOST_CREDENTIAL"))))
    def forbidden_prepare(*_args, **_kwargs):
        raise AssertionError("non-SessionStart hook must consume the cached profile")
    output = io.StringIO()
    raw = {"hook_event_name": "Stop", "session_id": "cached-host-session"}
    assert easy_hook.run(["--product", "claude", "--event", "Stop"], stdin=_stdin(raw),
                         stdout=output, environ=env, prepare=forbidden_prepare) == 0
    assert observed == [("claude", "Stop", "cached-host-secret")]
    assert "cached-host-secret" not in output.getvalue()
    assert not (data / "pmt.sqlite3").exists()


def test_replay_pending_and_read_context_keep_their_no_event_core_paths(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "replay_pending", lambda: calls.append("replay") or 0)
    replay_out = io.StringIO()
    assert easy_hook.run(["--product", "codex", "--replay-pending"], stdin=_stdin({}),
                         stdout=replay_out, environ={}) == 0
    monkeypatch.setattr(hooks, "lookup_context", lambda session, **kwargs:
                        calls.append((session, kwargs["product"])) or {"status": "empty"})
    context_out = io.StringIO()
    native = {"session_id": "context-session", "native_event": "SessionStart"}
    assert easy_hook.run(["--product", "claude", "--read-context"], stdin=_stdin(native),
                         stdout=context_out, environ={}) == 0
    assert calls == ["replay", ("context-session", "claude")]
    assert json.loads(context_out.getvalue())["status"] == "empty"
