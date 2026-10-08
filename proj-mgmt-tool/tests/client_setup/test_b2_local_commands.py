import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from pmt import easy_cli, easy_setup, hooks as core_hooks, storage_config
from pmt.client_setup.local_commands import _parse_command
from pmt.client_setup.credentials import store_credential
from pmt.client_setup.client import write_client_metadata
from pmt.db import Database
from pmt.errors import PmtError
from pmt import easy_hook


@pytest.fixture
def local_checkout(tmp_path, monkeypatch):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "B2 Test"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "b2@example.invalid"], check=True)
    (root / "tracked.txt").write_text("clean source\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "tracked.txt"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "baseline"], check=True)
    config, data = tmp_path / "config root", tmp_path / "data root"
    monkeypatch.setenv("PMT_CONFIG_ROOT", str(config))
    monkeypatch.setenv("PMT_DATA_ROOT", str(data))
    monkeypatch.delenv("PMT_SCOPE_ID", raising=False)
    monkeypatch.delenv("PMT_HOST_CREDENTIAL", raising=False)
    monkeypatch.chdir(root)
    return root, config, data


def _project(root):
    easy_cli.main(["link", "--new", "Project B2"])
    config_root, _data_root, profile, _digest = easy_cli._roots()
    mapping = next(item for item in profile["workspace_mappings"] if item["local_root"] == str(root))
    return config_root, profile, mapping


def _add_item(title="Verification item"):
    work = easy_cli.main(["add", "work", "B2 Work"])
    assert work == 0
    records = easy_cli._records()
    parent = next(item for item in records if item.get("title") == "B2 Work")
    item = easy_cli.main(["add", "item", title, "--parent", parent["record_id"],
                          "--criteria", "actual test passed"])
    assert item == 0
    return next(item for item in easy_cli._records() if item.get("title") == title)


def test_c06_local_new_project_creates_core_scope_hierarchy_and_reuses_repository(local_checkout, capsys):
    root, config, _data = local_checkout
    assert easy_cli.main(["link", "--new", "First Project"]) == 0
    _config, _profile, first = _project_info(config, root)
    assert easy_cli.main(["projects"]) == 0
    output = capsys.readouterr().out
    assert "First Project" in output
    subprocess.run(["git", "-C", str(root), "checkout", "-qb", "second"], check=True)
    assert easy_cli.main(["link"]) == 0
    profile = easy_cli._roots()[2]
    mappings = [item for item in profile["workspace_mappings"] if item["local_root"] == str(root)]
    assert len(mappings) == 2
    assert {item["repository_id"] for item in mappings} == {first["repository_id"]}
    assert {item["project_id"] for item in mappings} == {first["project_id"]}
    db = Database(_data, config)
    with db.connect() as conn:
        project = conn.execute("SELECT kind,parent_id FROM scopes WHERE id=?", (first["project_id"],)).fetchone()
        repository = conn.execute("SELECT kind,parent_id FROM scopes WHERE id=?", (project["parent_id"],)).fetchone()
        environment = conn.execute("SELECT kind,parent_id FROM scopes WHERE id=?", (repository["parent_id"],)).fetchone()
    assert (project["kind"], repository["kind"], environment["kind"]) == ("project", "repository", "environment")
    assert environment["parent_id"] is None


def _project_info(config, root):
    registry = json.loads((config / "projects.json").read_text(encoding="utf-8"))
    return config, None, next(item for item in registry["projects"] if item["local_root"] == str(root))


def test_c06_link_name_and_unlink_current_branch(local_checkout):
    root, config, _data = local_checkout
    assert easy_cli.main(["link", "--new", "Named Project"]) == 0
    subprocess.run(["git", "-C", str(root), "checkout", "-qb", "feature"], check=True)
    assert easy_cli.main(["link", "Named Project"]) == 0
    assert len(easy_cli._roots()[2]["workspace_mappings"]) == 2
    assert easy_cli.main(["unlink"]) == 0
    mappings = easy_cli._roots()[2]["workspace_mappings"]
    assert [(m["local_root"], m["branch"]) for m in mappings] == [(str(root), "main")]


def test_mode_is_readonly_and_first_local_link_prepares_explicit_roots(local_checkout, capsys):
    _root, config, data = local_checkout
    assert not (config / "storage.json").exists()
    assert easy_cli.main(["mode"]) == 0
    assert "mode: unconfigured" in capsys.readouterr().out
    assert not (config / "storage.json").exists() and not (data / "pmt.sqlite3").exists()
    assert easy_cli.main(["link", "--new", "Mode Test Project"]) == 0
    assert json.loads((config / "storage.json").read_text(encoding="utf-8"))["mode"] == "local"
    assert (data / "pmt.sqlite3").is_file()
    assert json.loads((config / "client.json").read_text(encoding="utf-8"))["source"] == "plugin"


def test_mode_reads_hosted_metadata_without_loading_credential_or_network(monkeypatch, tmp_path, capsys):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    (config / "storage.json").write_text(json.dumps({
        "schema_version": 1, "revision": 1, "mode": "hosted",
        "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
        "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
        "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"
    }), encoding="utf-8")
    monkeypatch.setenv("PMT_CONFIG_ROOT", str(config))
    monkeypatch.setenv("PMT_DATA_ROOT", str(data))
    monkeypatch.delenv("PMT_HOST_CREDENTIAL", raising=False)
    monkeypatch.setattr(easy_cli, "probe_storage", lambda *_args: (_ for _ in ()).throw(
        AssertionError("mode must not probe")))
    assert easy_cli.main(["mode"]) == 0
    output = capsys.readouterr().out
    assert "mode: hosted" in output and "fixture" in output and "host.invalid" in output
    assert "credential" not in output.casefold()
    assert not (data / "pmt.sqlite3").exists()


def test_c02_partial_host_options_fail_with_missing_names_and_no_side_effects(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data),
           "CLAUDE_PLUGIN_OPTION_HOST_URL": "https://host.invalid",
           "CLAUDE_PLUGIN_OPTION_DEVICE_ID": "acd66a06-5995-40f0-a637-e96ffaf52b04",
           "CLAUDE_PLUGIN_OPTION_ACTOR": "fixture",
           "CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL": "injected-secret"}
    prior_process_credential = os.environ.get("PMT_HOST_CREDENTIAL")
    result = easy_setup.client_mode.prepare(env, tmp_path, product="claude")
    assert result["status"] == "error" and result["error_code"] == "handoff_invalid"
    assert result["missing"] == ["namespace_id"]
    assert "namespace_id" in result["message"] and "injected-secret" not in result["message"]
    assert not config.exists() and not data.exists()
    assert os.environ.get("PMT_HOST_CREDENTIAL") == prior_process_credential


def test_c02_local_profile_stays_local_when_host_options_are_partial(tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    env = {"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data), "HOME": str(tmp_path)}
    assert easy_setup.client_mode.prepare(env, tmp_path, product="claude")["mode"] == "local"
    profile_path = config / "storage.json"
    before = profile_path.read_bytes()
    env["CLAUDE_PLUGIN_OPTION_HOST_URL"] = "https://host.invalid"
    env["CLAUDE_PLUGIN_OPTION_DEVICE_ID"] = "acd66a06-5995-40f0-a637-e96ffaf52b04"
    result = easy_setup.client_mode.prepare(env, tmp_path, product="claude")
    assert result["status"] == "ready" and result["mode"] == "local"
    assert "storage switch --to hosted" in result["message"]
    assert profile_path.read_bytes() == before
    assert not (config / "secrets" / "host-credential.dpapi").exists()


def test_c07_local_add_start_done_records_actual_output_and_finishes(local_checkout, capsys):
    root, config, data = local_checkout
    _project(root)
    item = _add_item()
    assert easy_cli.main(["start", item["record_id"][:8]]) == 0
    token_state = json.loads((data / "easy-claims.json").read_text(encoding="utf-8"))
    assert item["record_id"] in token_state and token_state[item["record_id"]]["claim_token"]
    original_argv = [sys.executable, "-c", "import sys; print('actual stdout proof'); sys.exit(0)"]
    command = subprocess.list2cmdline(original_argv) if os.name == "nt" else __import__("shlex").join(original_argv)
    assert _parse_command(command) == original_argv
    assert easy_cli.main(["done", item["record_id"], "--test", command, "--result", "verified", "--timeout", "20"]) == 0
    output = capsys.readouterr().out
    assert "actual stdout proof" in output and "Completed" in output
    latest = next(record for record in easy_cli._records() if record["record_id"] == item["record_id"])
    assert latest["state"] == "Done"
    assert item["record_id"] not in json.loads((data / "easy-claims.json").read_text(encoding="utf-8"))
    lookup = easy_cli.execute("lookup_verification", {"definition_id": "pmt.item.test", "definition_version": "1",
                                  "target_id": item["record_id"], "command": original_argv,
                                  "inputs": {"workspace": str(root)}}, record_id=item["record_id"])
    assert lookup["reusable"] and lookup["evidence_ids"]
    evidence = data / "resources" / "objects" / lookup["evidence_ids"][0]
    assert evidence.read_bytes().replace(b"\r\n", b"\n") == b"actual stdout proof\n"


@pytest.mark.parametrize("exit_code", [1])
def test_c07_failed_local_test_keeps_claim_and_item_in_progress(local_checkout, exit_code):
    root, _config, data = local_checkout
    _project(root)
    item = _add_item("Failing test item")
    assert easy_cli.main(["start", item["record_id"]]) == 0
    argv = [sys.executable, "-c", f"import sys; print('exit {exit_code}'); sys.exit({exit_code})"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else __import__("shlex").join(argv)
    assert easy_cli.main(["done", item["record_id"], "--test", command, "--result", "must fail", "--timeout", "20"]) == 1
    latest = next(record for record in easy_cli._records() if record["record_id"] == item["record_id"])
    assert latest["state"] == "In Progress"
    assert item["record_id"] in json.loads((data / "easy-claims.json").read_text(encoding="utf-8"))


def test_c07_dirty_checkout_rejects_before_launch_or_finish(local_checkout):
    root, _config, data = local_checkout
    _project(root)
    item = _add_item("Dirty checkout item")
    assert easy_cli.main(["start", item["record_id"]]) == 0
    dirty_input = root / "preexisting-untracked.txt"
    dirty_input.write_text("uncommitted\n", encoding="utf-8")
    marker = root / "untracked.txt"
    argv = [sys.executable, "-c", f"from pathlib import Path; Path({str(marker)!r}).write_text('ran')"]
    command = subprocess.list2cmdline(argv) if os.name == "nt" else __import__("shlex").join(argv)
    assert easy_cli.main(["done", item["record_id"], "--test", command, "--result", "dirty", "--timeout", "20"]) == 3
    assert not marker.exists()
    latest = next(record for record in easy_cli._records() if record["record_id"] == item["record_id"])
    assert latest["state"] == "In Progress"
    assert item["record_id"] in json.loads((data / "easy-claims.json").read_text(encoding="utf-8"))
    dirty_input.unlink()


def test_c07_local_pause_releases_claim_and_keeps_item_paused(local_checkout):
    _root, _config, data = local_checkout
    _project(_root)
    item = _add_item("Paused item")
    assert easy_cli.main(["start", item["record_id"]]) == 0
    assert easy_cli.main(["pause", item["record_id"], "--next", "resume later"]) == 0
    latest = next(record for record in easy_cli._records() if record["record_id"] == item["record_id"])
    assert latest["state"] == "Paused" and latest["body"]["next"] == "resume later"
    assert item["record_id"] not in json.loads((data / "easy-claims.json").read_text(encoding="utf-8"))


def test_c08_local_mode_and_check_write_read_replay(local_checkout):
    root, config, data = local_checkout
    _project(root)
    assert easy_cli.main(["mode"]) == 0
    assert easy_cli.main(["check"]) == 0
    facts = [record for record in easy_cli._records() if record["title"] == "pmt check local"]
    assert len(facts) == 1
    assert (data / "pmt.sqlite3").is_file()


def test_c08_hosted_unavailable_does_not_open_local_database(monkeypatch, tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    (config / "storage.json").write_text(json.dumps({
        "schema_version": 1, "revision": 1, "mode": "hosted",
        "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
        "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
        "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"
    }), encoding="utf-8")
    store_credential(config, "test-credential")
    write_client_metadata(config, source="connect", python_path=sys.executable, mode="hosted")
    monkeypatch.setenv("PMT_CONFIG_ROOT", str(config))
    monkeypatch.setenv("PMT_DATA_ROOT", str(data))
    monkeypatch.setenv("PMT_HOST_CREDENTIAL", "test-credential")
    monkeypatch.setenv("PMT_SCOPE_ID", "4c3d4d19-28fa-4727-9895-e0fee0f43d6a")
    monkeypatch.setattr(easy_cli, "probe_storage", lambda *_args: (_ for _ in ()).throw(
        PmtError("remote_unavailable", "offline fixture")))
    assert easy_cli.main(["check"]) == 1
    assert not (data / "pmt.sqlite3").exists()


def test_c08_hosted_check_uses_protected_credential_and_replays_one_fact(monkeypatch, tmp_path, capsys):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    (config / "storage.json").write_text(json.dumps({
        "schema_version": 1, "revision": 1, "mode": "hosted",
        "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
        "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
        "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"
    }), encoding="utf-8")
    store_credential(config, "hosted-check-credential")
    monkeypatch.setenv("PMT_CONFIG_ROOT", str(config))
    monkeypatch.setenv("PMT_DATA_ROOT", str(data))
    monkeypatch.delenv("PMT_HOST_CREDENTIAL", raising=False)
    monkeypatch.setenv("PMT_SCOPE_ID", "4c3d4d19-28fa-4727-9895-e0fee0f43d6a")
    monkeypatch.setattr(easy_setup, "git_checkout", lambda _cwd: (None, None))
    monkeypatch.setattr(easy_cli, "probe_storage", lambda *_args: {"host_preflight": {
        "core_version": "0.4.1", "db_schema": 5, "graph_schema": 1, "protocol_versions": [1]}})
    requests = []
    class RemoteFacade:
        def execute(self, request):
            requests.append(dict(request))
            if request["operation"] == "read_context":
                return {"ok": True, "result": {"records": []}}, 0
            return {"ok": True, "result": {"record_id": "diagnostic-fact"}}, 0
    monkeypatch.setattr(storage_config, "select_store", lambda *_args, **_kwargs: RemoteFacade())
    assert easy_cli.main(["check"]) == 0
    saves = [request for request in requests if request["operation"] == "save_change"]
    assert len(saves) == 2 and saves[0] == saves[1]
    assert os.environ["PMT_HOST_CREDENTIAL"] == "hosted-check-credential"
    assert "hosted-check-credential" not in capsys.readouterr().out
    assert not (data / "pmt.sqlite3").exists()


def test_hosted_commands_keep_using_remote_store_without_local_fallback(monkeypatch, tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    project_id = "4c3d4d19-28fa-4727-9895-e0fee0f43d6a"
    repository_id = "bdd8eaf4-cb0a-4971-90d7-8d7965f41fc7"
    (config / "storage.json").write_text(json.dumps({
        "schema_version": 1, "revision": 1, "mode": "hosted",
        "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
        "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
        "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"
    }), encoding="utf-8")
    (config / "projects.json").write_text(json.dumps({"projects": [{"name": "Hosted Project",
        "project_id": project_id, "repositories": [{"name": "repo", "repository_id": repository_id}]}]}),
        encoding="utf-8")
    store_credential(config, "protected-fixture-credential")
    monkeypatch.setenv("PMT_CONFIG_ROOT", str(config))
    monkeypatch.setenv("PMT_DATA_ROOT", str(data))
    monkeypatch.delenv("PMT_HOST_CREDENTIAL", raising=False)
    monkeypatch.delenv("PMT_SCOPE_ID", raising=False)
    monkeypatch.setattr(easy_setup, "git_checkout", lambda _cwd: (str(tmp_path), "main"))
    requests = []
    profile_updates = []
    class RemoteFacade:
        def execute(self, request):
            requests.append(request)
            return {"ok": True, "result": {"records": []}}, 0
    monkeypatch.setattr(storage_config, "select_store", lambda *_args, **_kwargs: RemoteFacade())
    monkeypatch.setattr(easy_cli, "configure_storage", lambda _root, request: profile_updates.append(request))
    assert easy_cli.main(["link", "Hosted Project"]) == 0
    assert profile_updates[0]["workspace_mappings"][0]["project_id"] == project_id
    assert easy_cli.execute("read_context", {"limit": 200}, scope_id=project_id)["records"] == []
    assert requests and requests[0]["operation"] == "read_context"
    assert requests[0]["scope_id"] == project_id
    assert "protected-fixture-credential" not in json.dumps(requests)
    assert not (data / "pmt.sqlite3").exists()


def test_cli_replays_same_request_id_after_ambiguous_remote_failure(monkeypatch, tmp_path):
    config, data = tmp_path / "config", tmp_path / "data"
    config.mkdir()
    (config / "storage.json").write_text(json.dumps({
        "schema_version": 1, "revision": 1, "mode": "hosted",
        "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [],
        "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
        "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"
    }), encoding="utf-8")
    store_credential(config, "replay-fixture-credential")
    monkeypatch.setenv("PMT_CONFIG_ROOT", str(config))
    monkeypatch.setenv("PMT_DATA_ROOT", str(data))
    calls, lookups = [], []
    class RetryStore:
        def execute(self, request):
            calls.append(dict(request))
            if len(calls) == 1:
                raise PmtError("remote_unavailable", "fixture timeout", 3, True)
            return {"ok": True, "result": {"record_id": "request-result"}}, 0
        def get_request_result(self, request_id, actor, session_id, *, expected_request=None):
            lookups.append((request_id, actor, session_id, expected_request))
            return None
    monkeypatch.setattr(storage_config, "select_store", lambda *_args, **_kwargs: RetryStore())
    result = easy_cli.execute("save_change", {"kind": "fact", "title": "retry", "reason": "test", "body": {}},
                             scope_id="4c3d4d19-28fa-4727-9895-e0fee0f43d6a")
    assert result["record_id"] == "request-result"
    assert len(calls) == 2 and calls[0] == calls[1]
    assert lookups[0][0] == calls[0]["request_id"] and lookups[0][3] == calls[0]
    assert "replay-fixture-credential" not in json.dumps(calls)


def test_c07_hosted_start_keeps_existing_claim_ref_storage(local_checkout, monkeypatch):
    root, config, data = local_checkout
    project_id = "4c3d4d19-28fa-4727-9895-e0fee0f43d6a"
    repository_id = "bdd8eaf4-cb0a-4971-90d7-8d7965f41fc7"
    branch_hash = __import__("hashlib").sha256(b"main").hexdigest()
    profile = {"schema_version": 1, "revision": 1, "mode": "hosted",
        "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": [{
            "repository_id": repository_id, "project_id": project_id, "branch": "main",
            "branch_key_sha256": branch_hash, "local_root": str(root),
            "relative_graph_path": "docs/pmt-docs/graph.json"}],
        "endpoint": "https://host.invalid", "credential_env": "PMT_HOST_CREDENTIAL",
        "device_id": "acd66a06-5995-40f0-a637-e96ffaf52b04",
        "namespace_id": "73032707-440f-4123-9189-1ff7f046f043", "actor": "fixture"}
    config.mkdir()
    (config / "storage.json").write_text(json.dumps(profile), encoding="utf-8")
    store_credential(config, "host-start-credential")
    item = {"record_id": "f0b36d36-1ef2-4eb1-a0da-b66484acfd0b", "id": "f0b36d36-1ef2-4eb1-a0da-b66484acfd0b",
            "kind": "item", "title": "remote item", "state": "Planned", "revision": 1, "scope_id": project_id}
    monkeypatch.setenv("PMT_HOST_CREDENTIAL", "host-start-credential")
    monkeypatch.setenv("PMT_SCOPE_ID", project_id)
    class RemoteFacade:
        def execute(self, request):
            if request["operation"] == "read_context":
                return {"ok": True, "result": {"records": [item]}}, 0
            return {"ok": True, "result": {"claim_ref": "remote-claim-ref", "revision": 2,
                                              "owner_session": request["session_id"]}}, 0
    monkeypatch.setattr(storage_config, "select_store", lambda *_args, **_kwargs: RemoteFacade())
    assert easy_cli.main(["start", item["record_id"]]) == 0
    hosted_claims = json.loads((data / "easy-cli" / "claims.json").read_text(encoding="utf-8"))
    assert hosted_claims[item["record_id"]]["claim_ref"] == "remote-claim-ref"
    assert not (data / "easy-claims.json").exists()
    assert not (data / "pmt.sqlite3").exists()


@pytest.mark.parametrize("corrupt", [b"{broken", b'"wrong type"'])
def test_c06_corrupt_project_registry_fails_before_creating_scopes(local_checkout, corrupt):
    root, config, data = local_checkout
    easy_cli._roots()
    registry = config / "projects.json"
    registry.write_bytes(corrupt)
    before = registry.read_bytes()
    assert easy_cli.main(["link", "--new", "Must not create scopes"]) == 2
    db = Database(data, config)
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM scopes").fetchone()[0] == 0
    assert registry.read_bytes() == before


def test_c07_corrupt_claim_file_fails_before_claim_mutation(local_checkout):
    root, config, data = local_checkout
    _project(root)
    item = _add_item("Corrupt claim store item")
    claim_file = data / "easy-claims.json"
    corrupt = b"{broken"
    claim_file.write_bytes(corrupt)
    assert easy_cli.main(["start", item["record_id"]]) == 2
    latest = next(record for record in easy_cli._records() if record["record_id"] == item["record_id"])
    assert latest["state"] == "Planned"
    with Database(data, config).connect() as conn:
        assert conn.execute("SELECT count(*) FROM claims WHERE record_id=?", (item["record_id"],)).fetchone()[0] == 0
    assert claim_file.read_bytes() == corrupt


def test_c07_parallel_item_starts_preserve_both_tokens(local_checkout):
    root, config, data = local_checkout
    _project(root)
    easy_cli.main(["add", "work", "Parallel Work"])
    work = next(item for item in easy_cli._records() if item.get("title") == "Parallel Work")
    item_ids = []
    for title in ("Parallel Item A", "Parallel Item B"):
        easy_cli.main(["add", "item", title, "--parent", work["record_id"], "--criteria", "done"])
        item_ids.append(next(item["record_id"] for item in easy_cli._records() if item.get("title") == title))
    src = str(Path(__file__).resolve().parents[2] / "src")
    child = ("import sys; sys.path.insert(0, sys.argv[1]); from pmt.easy_cli import main; "
             "raise SystemExit(main(['start', sys.argv[2]]))")
    env = os.environ.copy()
    processes = [subprocess.Popen([sys.executable, "-c", child, src, item_id], cwd=root, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for item_id in item_ids]
    outputs = [process.communicate(timeout=30) + (process.returncode,) for process in processes]
    assert [result[2] for result in outputs] == [0, 0], outputs
    claims = json.loads((data / "easy-claims.json").read_text(encoding="utf-8"))
    assert set(item_ids) <= set(claims)
    assert all(claims[item_id].get("claim_token") for item_id in item_ids)
