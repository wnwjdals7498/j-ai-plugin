"""F15 isolated package, local setup/hook, and HTTPS client acceptance fixtures."""
from __future__ import annotations

import hashlib
import base64
import json
import os
from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
import uuid
from urllib.request import urlopen

import pytest

from pmt import __version__ as CORE_VERSION
from pmt.db import SCHEMA_VERSION

pytest_plugins = ["test_phase3_host_network"]

ROOT = Path(__file__).resolve().parents[1]
BASE_PYTHON = Path(sys.executable).resolve()


def _portable_env(additions=None):
    env = {key: os.environ[key] for key in
        ("SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH") if key in os.environ}
    if additions:
        env.update({key: str(value) for key, value in additions.items()})
    return env


def _copy_python_package(stage: Path, *, distribution_version: str | None = None):
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(ROOT / "src" / "pmt", stage / "src" / "pmt", ignore=ignore)
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if distribution_version:
        old = f'version = "{CORE_VERSION}"'
        if old not in project:
            raise AssertionError("the package revision fixture expected the current source distribution")
        project = project.replace(old, f'version = "{distribution_version}"', 1)
    (stage / "pyproject.toml").write_text(project, encoding="utf-8", newline="\n")


def _build_wheel(stage: Path, wheelhouse: Path):
    completed = subprocess.run([str(BASE_PYTHON), "-m", "pip", "wheel", "--no-deps",
        "--no-build-isolation", "--no-index", "--wheel-dir", str(wheelhouse), str(stage)],
        cwd=wheelhouse.parent, env=_portable_env(), capture_output=True, text=True,
        encoding="utf-8", timeout=120, check=False)
    assert completed.returncode == 0, "isolated wheel build failed"
    wheels = sorted(wheelhouse.glob("proj_mgmt_tool-*.whl"), key=lambda path: path.stat().st_mtime_ns)
    assert wheels, "wheel builder did not publish a package"
    return wheels[-1]


@pytest.fixture
def installed_package(tmp_path):
    stage = tmp_path / ("package-source-" + CORE_VERSION)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    _copy_python_package(stage)
    wheel = _build_wheel(stage, wheelhouse)
    venv = tmp_path / "install-venv"
    created = subprocess.run([str(BASE_PYTHON), "-m", "venv", str(venv)],
        cwd=tmp_path, env=_portable_env(), capture_output=True, text=True,
        encoding="utf-8", timeout=90, check=False)
    assert created.returncode == 0, "isolated Python environment creation failed"
    python = venv / "Scripts" / "python.exe"
    installed = subprocess.run([str(python), "-m", "pip", "install", "--no-deps",
        "--no-index", str(wheel)], cwd=tmp_path, env=_portable_env(), capture_output=True,
        text=True, encoding="utf-8", timeout=120, check=False)
    assert installed.returncode == 0, "offline isolated package install failed"
    cwd = tmp_path / "unrelated-working-directory"
    cwd.mkdir()
    return {"stage": stage, "wheelhouse": wheelhouse, "wheel": wheel,
            "venv": venv, "python": python, "cwd": cwd, "tmp": tmp_path}


def _run_protocol(package, data_root, config_root, request, *, environ=None):
    command = [str(package["python"]), "-m", "pmt", "--data-root", str(data_root),
        "--config-root", str(config_root)]
    completed = subprocess.run(command, input=json.dumps(request, separators=(",", ":")),
        cwd=package["cwd"], env=_portable_env(environ), capture_output=True, text=True,
        encoding="utf-8", timeout=30, check=False)
    assert completed.stdout.count("\n") == 1, "PMT CLI did not emit one JSON response line"
    return json.loads(completed.stdout), completed.returncode


def _setup(package, data_root, config_root):
    request = {"protocol_version": 1, "operation": "setup", "request_id": str(uuid.uuid4()),
        "actor": "main", "session_id": "f15-package-session", "payload": {"product": "cli"}}
    response, code = _run_protocol(package, data_root, config_root, request)
    assert code == 0 and response["ok"], response.get("error")
    assert response["result"]["core_version"] == CORE_VERSION
    assert response["result"]["schema_version"] == SCHEMA_VERSION
    return response["result"]


def _installed_version(package):
    completed = subprocess.run([str(package["python"]), "-m", "pip", "show", "proj-mgmt-tool"],
        cwd=package["cwd"], env=_portable_env(), capture_output=True, text=True,
        encoding="utf-8", timeout=20, check=False)
    assert completed.returncode == 0
    return next(line.split(":", 1)[1].strip() for line in completed.stdout.splitlines()
                if line.startswith("Version:"))


def _restart_fixture_host(env, output_dir):
    prior = env["process"]
    command = list(prior.args)
    prior.terminate()
    prior.wait(timeout=10)
    old_logs = env["stdout_path"].read_bytes() + env["stderr_path"].read_bytes()
    assert env["token_a"].encode() not in old_logs
    assert env["token_b"].encode() not in old_logs
    env["stdout"].close()
    env["stderr"].close()

    output_dir.mkdir(parents=True, exist_ok=True)
    stdout_path, stderr_path = output_dir / "restarted-host.stdout", output_dir / "restarted-host.stderr"
    stdout, stderr = stdout_path.open("wb"), stderr_path.open("wb")
    claim_key_name = "PMT_TEST_HOST_CLAIM_KEY"
    process_env = _portable_env({
        "PYTHONPATH": str(ROOT / "src"),
        claim_key_name: base64.b64encode(env["app"]._keys["fixture"]).decode("ascii"),
    })
    process = subprocess.Popen(command, cwd=ROOT, env=process_env, stdout=stdout, stderr=stderr)
    context = ssl.create_default_context(cafile=str(env["cert"]))
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and process.poll() is None:
        try:
            with urlopen(env["base"] + "/health", context=context, timeout=0.5) as response:
                if response.status == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        process.terminate()
        process.wait(timeout=5)
        stdout.close(); stderr.close()
        raise AssertionError("isolated HTTPS Host did not recover from restart")
    env.update(process=process, stdout=stdout, stderr=stderr,
               stdout_path=stdout_path, stderr_path=stderr_path)


def test_f15_installed_package_setup_hook_update_and_code_restore(installed_package, tmp_path):
    package = installed_package
    data_root, config_root = tmp_path / "user-data", tmp_path / "user-config"
    initial = _setup(package, data_root, config_root)
    assert _installed_version(package) == CORE_VERSION
    assert initial["storage_ready"] is True

    hook_driver = ("import json,sys; from pmt.hooks import process_hook; "
        "raw=json.load(sys.stdin); "
        "r=process_hook('codex','UserPromptSubmit',raw,environ=__import__('os').environ); "
        "print(json.dumps({'ok':r.get('ok'),'request_id':r.get('request_id')}))")
    event_input = {"hook_event_name": "UserPromptSubmit", "session_id": "f15-native-session",
        "turn_id": "f15-turn-1", "prompt": "F15_PRIVATE_PROMPT_SENTINEL_NOT_FOR_STORAGE"}
    event_env = _portable_env({"PMT_DATA_ROOT": data_root, "PMT_CONFIG_ROOT": config_root,
        "PMT_PYTHON": package["python"], "PMT_INSTALLATION_ID": initial["environment_id"]})
    first_hook = subprocess.run([str(package["python"]), "-c", hook_driver], input=json.dumps(event_input),
        cwd=package["cwd"], env=event_env, capture_output=True, text=True, encoding="utf-8",
        timeout=30, check=False)
    assert first_hook.returncode == 0
    hook_response = json.loads(first_hook.stdout)
    assert hook_response["ok"] is True and hook_response["request_id"]

    database = data_root / "pmt.sqlite3"
    with sqlite3.connect(database) as conn:
        db_id_before = conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()[0]
        schema_before = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
        event_count_before = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        event_payload = conn.execute("SELECT payload_json FROM events WHERE event_type='prompt_submitted'").fetchone()[0]
    assert event_count_before == 1 and int(schema_before) == SCHEMA_VERSION
    assert "F15_PRIVATE_PROMPT_SENTINEL_NOT_FOR_STORAGE" not in event_payload
    assert not list((data_root / "hook-pending").glob("*.json"))

    # Only the isolated wheel metadata advances; runtime code/version stay fixed.
    updated_version = CORE_VERSION + ".post1"
    updated_stage = package["tmp"] / ("package-source-" + updated_version)
    _copy_python_package(updated_stage, distribution_version=updated_version)
    updated_wheel = _build_wheel(updated_stage, package["wheelhouse"])
    update = subprocess.run([str(package["python"]), "-m", "pip", "install", "--upgrade",
        "--no-deps", "--no-index", str(updated_wheel)], cwd=package["cwd"], env=_portable_env(),
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert update.returncode == 0 and _installed_version(package) == updated_version
    after_update = _setup(package, data_root, config_root)
    assert after_update["db_id"] == initial["db_id"]
    with sqlite3.connect(database) as conn:
        assert conn.execute("SELECT value FROM meta WHERE key='db_id'").fetchone()[0] == db_id_before
        assert int(conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]) == SCHEMA_VERSION
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == event_count_before

    restore = subprocess.run([str(package["python"]), "-m", "pip", "install", "--force-reinstall",
        "--no-deps", "--no-index", str(package["wheel"])], cwd=package["cwd"], env=_portable_env(),
        capture_output=True, text=True, encoding="utf-8", timeout=120, check=False)
    assert restore.returncode == 0 and _installed_version(package) == CORE_VERSION
    restored = _setup(package, data_root, config_root)
    assert restored["db_id"] == db_id_before and restored["schema_version"] == SCHEMA_VERSION
    assert hashlib.sha256(package["wheel"].read_bytes()).hexdigest()


def _configure_installed_hosted(package, *, config_root, data_root, environment_id,
                                endpoint, credential_env, credential, device_id, namespace_id,
                                ca_file, actor, mapping):
    config_root.mkdir(parents=True)
    (config_root / "profile.json").write_text(
        json.dumps({"environment_id": environment_id}) + "\n", encoding="utf-8")
    body = {"request_id": str(uuid.uuid4()), "mode": "hosted", "environment_id": environment_id,
        "expected_config_sha256": None, "endpoint": endpoint, "credential_env": credential_env,
        "device_id": device_id, "namespace_id": namespace_id, "expected_actor": actor,
        "ca_file": str(ca_file), "workspace_mappings": [mapping]}
    completed = subprocess.run([str(package["python"]), "-m", "pmt", "--data-root", str(data_root),
        "--config-root", str(config_root), "storage", "configure"],
        input=json.dumps(body, separators=(",", ":")), cwd=package["cwd"],
        env=_portable_env({credential_env: credential}), capture_output=True, text=True,
        encoding="utf-8", timeout=30, check=False)
    assert completed.stdout.count("\n") == 1, "storage configure did not emit one JSON response line"
    response, code = json.loads(completed.stdout), completed.returncode
    assert code == 0 and response["ok"], response.get("error")
    assert response["result"]["mode"] == "hosted"
    assert not (data_root / "pmt.sqlite3").exists()
    return response["result"]


def test_f15_installed_hosted_cli_two_clients_real_https_and_native_hook(installed_package,
                                                                          live_host, tmp_path):
    from pmt.util import canonical_json
    from pmt.workspace import canonical_workspace

    env = live_host
    branch = env["pin"].selected_ref
    assert branch == "main"
    branch_hash = hashlib.sha256(branch.encode("utf-8")).hexdigest()
    canonical = canonical_workspace(env["repo"], branch)
    client_a_root, client_b_root = tmp_path / "checkout-a", tmp_path / "checkout-b"
    for root in (client_a_root, client_b_root):
        graph = root / env["relative"]
        graph.parent.mkdir(parents=True)
        graph.write_text(canonical_json(env["graph"]) + "\n", encoding="utf-8")

    config_a, data_a = tmp_path / "client-a-config", tmp_path / "client-a-data"
    config_b, data_b = tmp_path / "client-b-config", tmp_path / "client-b-data"
    profile_a = _configure_installed_hosted(installed_package, config_root=config_a, data_root=data_a,
        environment_id=env["headers"]["x-pmt-environment"], endpoint=env["base"],
        credential_env="PMT_F15_CLIENT_A_TOKEN", credential=env["token_a"],
        device_id=env["headers"]["x-pmt-device"], namespace_id=env["app"].auth.namespace_id,
        ca_file=env["cert"], actor=env["actor"], mapping={"repository_id": env["repo"],
            "project_id": env["project"], "branch": branch, "branch_key_sha256": branch_hash,
            "local_root": str(client_a_root), "relative_graph_path": env["relative"]})
    profile_b = _configure_installed_hosted(installed_package, config_root=config_b, data_root=data_b,
        environment_id=env["environment_b"], endpoint=env["base"],
        credential_env="PMT_F15_CLIENT_B_TOKEN", credential=env["token_b"],
        device_id=env["device_b"]["device_id"], namespace_id=env["app"].auth.namespace_id,
        ca_file=env["cert"], actor="network-client-b", mapping={"repository_id": env["repo"],
            "project_id": env["foreign_project"], "branch": branch, "branch_key_sha256": branch_hash,
            "local_root": str(client_b_root), "relative_graph_path": env["relative"]})
    assert profile_a["environment_id"] != profile_b["environment_id"]

    graph_bytes = canonical_json(env["graph"]).encode("utf-8")
    graph_ref = env["store_a"].publish_resource({"request_id": str(uuid.uuid4()),
        "scope_id": env["project"], "purpose": "graph_snapshot"}, graph_bytes,
        session_id=env["session_a"])["artifact_ref"]
    publish = {"protocol_version": 1, "operation": "publish_source_snapshot",
        "request_id": str(uuid.uuid4()), "actor": env["actor"], "session_id": env["session_a"],
        "scope_id": env["project"], "source": {"product": "cli"},
        "payload": {"project_id": env["project"], "repository_id": env["repo"],
            "workspace": str(client_a_root), "relative_graph_path": env["relative"],
            "run_id": env["run"], "expected_run_revision": 3,
            "expected_source_revision": 0, "branch_key": branch,
            "source_pin": env["pin"].to_dict(), "graph_resource_ref": graph_ref}}
    published, publish_code = _run_protocol(installed_package, data_a, config_a, publish,
        environ={"PMT_F15_CLIENT_A_TOKEN": env["token_a"]})
    assert publish_code == 0 and published["ok"], published.get("error")
    assert published["result"]["source_pin"]["source_hash"] == env["pin"].source_hash

    read = {"protocol_version": 1, "operation": "read_source_snapshot", "request_id": str(uuid.uuid4()),
        "actor": env["actor"], "session_id": env["session_a"], "scope_id": env["project"],
        "source": {"product": "cli"}, "payload": {"project_id": env["project"],
            "repository_id": env["repo"], "workspace": str(client_a_root),
            "relative_graph_path": env["relative"], "run_id": env["run"],
            "expected_run_revision": 3, "expected_source": env["pin"].to_dict()}}
    read_result, read_code = _run_protocol(installed_package, data_a, config_a, read,
        environ={"PMT_F15_CLIENT_A_TOKEN": env["token_a"]})
    assert read_code == 0 and read_result["ok"], read_result.get("error")
    assert read_result["result"]["source_pin"]["source_hash"] == env["pin"].source_hash
    assert read_result["result"]["canonical_workspace"] == canonical
    assert str(client_a_root) not in canonical_json(read_result)

    denied, denied_code = _run_protocol(installed_package, data_b, config_b,
        {**read, "request_id": str(uuid.uuid4()), "actor": "network-client-b",
         "session_id": env["session_b"], "scope_id": env["foreign_project"],
         "payload": {**read["payload"], "project_id": env["foreign_project"],
             "workspace": str(client_b_root)}}, environ={"PMT_F15_CLIENT_B_TOKEN": env["token_b"]})
    assert denied_code != 0 and denied["ok"] is False
    assert denied["error"]["code"] in {"scope_forbidden", "workspace_authority_stale"}
    assert not (data_a / "pmt.sqlite3").exists() and not (data_b / "pmt.sqlite3").exists()

    hook_driver = ("import json,sys,os; from pmt.hooks import process_hook; "
        "r=process_hook('codex','UserPromptSubmit',json.load(sys.stdin),environ=os.environ); "
        "print(json.dumps({'ok':r.get('ok'),'request_id':r.get('request_id')}))")
    hook_input = {"hook_event_name": "UserPromptSubmit", "session_id": "f15-hosted-native-session",
        "turn_id": "f15-hosted-turn", "prompt": "F15_PRIVATE_PROMPT_SENTINEL_NOT_FOR_HOST"}
    hook_env = {"PMT_DATA_ROOT": str(data_a), "PMT_CONFIG_ROOT": str(config_a),
        "PMT_INSTALLATION_ID": profile_a["environment_id"], "PMT_PYTHON": str(installed_package["python"]),
        "PMT_SCOPE_ID": env["project"], "PMT_F15_CLIENT_A_TOKEN": env["token_a"]}
    hook = subprocess.run([str(installed_package["python"]), "-c", hook_driver],
        input=json.dumps(hook_input), cwd=installed_package["cwd"], env=_portable_env(hook_env),
        capture_output=True, text=True, encoding="utf-8", timeout=30, check=False)
    assert hook.returncode == 0 and json.loads(hook.stdout)["ok"] is True
    assert not list((data_a / "hook-pending").glob("*.json"))
    with closing(env["db"].connect()) as conn:
        event = conn.execute("SELECT scope_id,payload_json FROM events WHERE event_type='prompt_submitted' "
            "AND scope_id=? ORDER BY recorded_at DESC LIMIT 1", (env["project"],)).fetchone()
    assert event and "F15_PRIVATE_PROMPT_SENTINEL_NOT_FOR_HOST" not in event["payload_json"]

    _restart_fixture_host(env, tmp_path / "host-restart")
    persisted_resource = env["store_a"].read_resource(graph_ref["id"], session_id=env["session_a"],
        expected_sha256=graph_ref["sha256"], scope_id=env["project"])
    assert persisted_resource["content"] == graph_bytes
    after_restart, restart_code = _run_protocol(installed_package, data_a, config_a,
        {**read, "request_id": str(uuid.uuid4())},
        environ={"PMT_F15_CLIENT_A_TOKEN": env["token_a"]})
    assert restart_code == 0 and after_restart["ok"], after_restart.get("error")
    assert after_restart["result"]["source_pin"]["source_hash"] == env["pin"].source_hash


def test_p3_f13_03_https_restore_then_current_client_source_reindex_and_query(tmp_path, monkeypatch):
    from pmt.db import Database
    from pmt.efficiency.source import SourcePin
    from pmt.migration import MigrationCoordinator
    from pmt.planning.graph import validate_graph
    from pmt.util import canonical_json, new_id, utc_now
    from pmt.workspace import canonical_workspace
    from test_phase3_host_data import _node
    from test_phase3_migration import _source_case
    from test_phase3_transfer_http import _pack_bundle, _request, _start_host, _stop_host
    from pmt.http_store import HttpStore

    source = _source_case(tmp_path / "restore-source")
    archive_root = tmp_path / "restore-bundle"
    manifest = MigrationCoordinator().create_backup(source["db"], archive_root, [source["mapping"]])
    bundle = _pack_bundle(archive_root, manifest)
    target = _start_host(tmp_path / "restored-host", "f15-restore-target")
    store = None
    try:
        transfer_metadata = {"bundle_id": manifest["bundle_id"],
            "manifest_sha256": manifest["manifest_sha256"]}
        status, _, raw = _request(target, "POST", "/api/v1/transfers/import", bundle,
            {**target["headers"], "X-PMT-Transfer-Metadata": canonical_json(transfer_metadata),
             "Content-Type": "application/vnd.pmt.migration+zip"})
        assert status == 200
        imported = json.loads(raw)
        assert imported["receipt"]["state"] == "imported"

        branch = source["branch"]
        workspace = source["workspace"]
        graph_path = workspace / "graph.json"
        requirement_id, implementation_id, relation_id = (new_id(), new_id(), new_id())
        graph = {"schema_version": 1, "project_id": source["project_id"], "graph_version": 1,
            "nodes": [_node(requirement_id, "requirement"), _node(implementation_id, "implementation")],
            "relations": [{"id": relation_id, "kind": "implements", "from": requirement_id,
                           "to": implementation_id}],
            "provenance": {"source_ref": "F15 restored-client snapshot fixture"}}
        graph_wire = canonical_json(graph) + "\n"
        graph_path.write_text(graph_wire, encoding="utf-8")
        subprocess.run(["git", "-C", str(workspace), "add", "graph.json"], check=True,
            cwd=workspace, capture_output=True)
        subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "F15 current source fixture"],
            check=True, cwd=workspace, capture_output=True)
        head = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, cwd=workspace).stdout.strip()
        checked = validate_graph(graph, source["project_id"], complete=False)
        pin = SourcePin(source["repo_id"], source["project_id"], branch, head,
            checked["schema_version"], checked["graph_version"], checked["sha256"], "clean")
        uri = canonical_workspace(source["repo_id"], branch)

        session_id = target["headers"]["x-pmt-session"]
        step_id, job_id, run_id = new_id(), new_id(), new_id()
        relative_path = "graph.json"
        scopes = [{"kind": "path", "workspace": uri, "resource": relative_path}]
        now = utc_now()
        intent = {"run_id": run_id, "job_id": job_id, "step_id": step_id,
            "scope_id": source["project_id"], "workspace": uri, "scopes": scopes,
            "criteria": [{"id": "current-source", "meaning": "SourcePin matches the restored checkout"}],
            "route": {"mode": "native", "adapter_kind": "native", "actual_support": "verified_supported"},
            "task": {"task_id": source["item_id"], "step_id": step_id},
            "requirements_version": "f15-restore", "plan_version": "f15-restore",
            "role": "lower", "directive_version": 1}
        with target["db"].write() as conn:
            conn.execute("INSERT INTO records(id,kind,scope_id,parent_id,title,state,body_json,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,'InProgress',?,1,?,?)", (step_id, "step", source["project_id"],
                source["item_id"], "Restored source verification Step",
                canonical_json({"directive_id": source["directive_id"], "directive_version": 1}), now, now))
            conn.execute("INSERT INTO step_specs(step_id,directive_id,directive_version,requirements_version,plan_version,plan_id,role,product_stage,workspace,scopes_json,criteria_json,dependencies_json,created_at,updated_at) "
                "VALUES(?,?,1,'f15-restore','f15-restore',NULL,'lower','prototype',?,?,?, '[]',?,?)",
                (step_id, source["directive_id"], uri, canonical_json(scopes),
                 canonical_json(intent["criteria"]), now, now))
            conn.execute("INSERT INTO execution_jobs(id,step_id,state,policy_json,created_at,updated_at) VALUES(?,?,'running','{}',?,?)",
                (job_id, step_id, now, now))
            conn.execute("INSERT INTO execution_runs(id,job_id,step_id,attempt,state,revision,owner_session,directive_version,workspace,scopes_json,route_json,intent_json,created_at,updated_at) "
                "VALUES(?,?,?,1,'running',1,?,1,?,?,?,?,?,?)", (run_id, job_id, step_id, session_id,
                 uri, canonical_json(scopes), canonical_json(intent["route"]), canonical_json(intent), now, now))
            conn.execute("INSERT INTO scope_locks(lock_key,run_id,owner_session,kind,workspace,resource,created_at) VALUES(?,?,?,?,?,?,?)",
                ("f15-restore-graph-lock", run_id, session_id, "path", uri, relative_path, now))

        token_env_name = "PMT_F15_RESTORE_TOKEN"
        monkeypatch.setenv(token_env_name, target["headers"]["authorization"][7:])
        host_command = list(target["process"].args)
        ca_path = Path(host_command[host_command.index("--ssl-certfile") + 1])
        store = HttpStore(target["base"], token_env_name, target["headers"]["x-pmt-device"],
            target["headers"]["x-pmt-environment"], target["headers"]["x-pmt-namespace"],
            ca_file=str(ca_path))
        store.register_session(session_id)
        resource_ref = store.publish_resource({"request_id": new_id(), "scope_id": source["project_id"],
            "purpose": "graph_snapshot"}, graph_wire.encode("utf-8"), session_id=session_id)["artifact_ref"]
        publish = {"protocol_version": 1, "operation": "publish_source_snapshot", "request_id": new_id(),
            "actor": "transfer-admin", "session_id": session_id, "scope_id": source["project_id"],
            "payload": {"project_id": source["project_id"], "repository_id": source["repo_id"],
                "canonical_workspace": uri, "relative_graph_path": relative_path, "run_id": run_id,
                "expected_run_revision": 1, "expected_source_revision": 0, "branch_key": branch,
                "source_pin": pin.to_dict(), "graph_resource_ref": resource_ref}}
        saved, code = store.execute(publish)
        assert code == 0 and saved["ok"], saved.get("error")
        indexed, code = store.execute({**publish, "operation": "rebuild_graph_index",
            "request_id": new_id(), "payload": {"project_id": source["project_id"],
                "repository_id": source["repo_id"], "canonical_workspace": uri,
                "relative_graph_path": relative_path, "run_id": run_id,
                "expected_run_revision": 1, "expected_source": pin.to_dict()}})
        assert code == 0 and indexed["ok"], indexed.get("error")
        queried, code = store.execute({**publish, "operation": "query_graph", "request_id": new_id(),
            "payload": {"project_id": source["project_id"], "repository_id": source["repo_id"],
                "canonical_workspace": uri, "relative_graph_path": relative_path,
                "run_id": run_id, "expected_run_revision": 1, "expected_source": pin.to_dict(),
                "query": {"node_ids": [requirement_id, implementation_id], "max_depth": 2,
                          "page_size": 50}}})
        assert code == 0 and queried["ok"], queried.get("error")
        assert queried["result"]["source_pin"]["source_hash"] == pin.source_hash
        assert set(queried["result"]["graph_slice"]["items"][i]["value"]["id"]
                   for i in range(len(queried["result"]["graph_slice"]["items"]))
                   if queried["result"]["graph_slice"]["items"][i]["entity"] == "node") == {
                       requirement_id, implementation_id}
        restored_resource = store.read_resource(resource_ref["id"], session_id=session_id,
            expected_sha256=resource_ref["sha256"], scope_id=source["project_id"])
        assert restored_resource["content"] == graph_wire.encode("utf-8")
    finally:
        if store is not None:
            store._opener.close() if hasattr(store._opener, "close") else None
        _stop_host(target)
