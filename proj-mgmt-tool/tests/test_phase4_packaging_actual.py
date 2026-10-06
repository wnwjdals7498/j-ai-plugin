"""Portable package upgrade/session smoke; never installs into user profiles."""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from pmt.util import new_id
from test_phase3_hosted_cli import _configure
from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout

pytest_plugins = ["test_phase3_host_network"]
ROOT = Path(__file__).resolve().parents[1]


def _build(destination):
    spec = importlib.util.spec_from_file_location("p4_package_builder", ROOT / "scripts/build_plugins.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    return builder.build_plugins(destination, "0.4.0", ROOT)


def _request(operation, payload=None, **fields):
    return {"protocol_version": 1, "operation": operation, "request_id": new_id(),
            "actor": "main", "session_id": "packaged-first-session", "payload": payload or {}, **fields}


def _call(package, data, config, request, cwd):
    process = subprocess.run([sys.executable, str(package / "scripts/pmt.py"),
        "--data-root", str(data), "--config-root", str(config)],
        input=json.dumps(request, ensure_ascii=False), capture_output=True, text=True, encoding="utf-8",
        env=dict(os.environ, PYTHONPATH=""), cwd=cwd, timeout=30, check=False)
    assert process.returncode == 0, process.stdout + process.stderr
    assert len(process.stdout.splitlines()) == 1
    envelope = json.loads(process.stdout)
    assert envelope["ok"] is True
    return envelope


@pytest.mark.parametrize("product", ["codex", "claude", "opencode"])
def test_portable_package_upgrade_reinstall_and_fresh_session_keep_schema4_data(tmp_path, product):
    historical = ROOT / "docs/phase3/evidence/local-acceptance/repaired-source/package-snapshot/0.3.0" / product
    assert (historical / "pmt-package.json").is_file(), "Actual pinned schema4 package fixture is required"
    assert json.loads((historical / "pmt-package.json").read_text(encoding="utf-8"))["core_version"] == "0.3.0"
    installed = tmp_path / "installed"
    old_package = installed / "0.3.0"
    shutil.copytree(historical, old_package)
    data, config = tmp_path / "data", tmp_path / "config"
    unrelated = tmp_path / "다른 작업 폴더"
    unrelated.mkdir()
    initial = _call(old_package, data, config, _request("setup", {"product": "cli"}), unrelated)
    assert initial["result"]["schema_version"] == 4
    created = _call(old_package, data, config, _request("create_scope", {
        "kind": "project", "slug": "portable-" + product, "body": {"goal": "기존 이력을 보존하고 세션을 재개한다"}}), unrelated)
    scope = created["result"]["scope_id"]
    saved = _call(old_package, data, config, _request("save_change", {
        "kind": "work", "title": "기존 작업", "body": {"goal": "재개 이력을 확인한다"},
        "reason": "격리된 업데이트 시험"}, scope_id=scope), unrelated)
    record = saved["result"]["record_id"]
    built = _build(tmp_path / "distribution")
    new_package = installed / "0.4.0"
    shutil.copytree(Path(built["products"][product]["directory"]), new_package)
    updated = _call(new_package, data, config, _request("setup", {"product": "cli"}), unrelated)
    assert updated["result"]["schema_version"] == 5
    assert len(list(data.glob("pmt-schema4-*.sqlite3"))) == 1
    overview_req = _request("compose_resume_overview", {"budget": {"max_bytes": 8192, "max_lines": 96}},
                            scope_id=scope, session_id="packaged-fresh-session")
    overview = _call(new_package, data, config, overview_req, unrelated)["result"]
    assert overview["metadata_only"] is True and overview["private_detail_read"] is False
    assert overview["overview"]["scope"]["project_ref"] == scope
    assert record in {item["record_ref"] for item in overview["overview"]["work_items"]}
    assert overview["next_action"]["executable"] is False
    before_files = {path.name: path.read_bytes() for path in config.iterdir() if path.is_file()}
    removed = installed / "uninstalled-0.4.0"
    assert new_package.resolve().is_relative_to(tmp_path.resolve()) and removed.resolve().is_relative_to(tmp_path.resolve())
    new_package.rename(removed)
    reinstalled = installed / "reinstalled-0.4.0"
    shutil.copytree(Path(built["products"][product]["directory"]), reinstalled)
    again = _call(reinstalled, data, config, overview_req | {"request_id": new_id()}, unrelated)["result"]
    assert again["overview"]["work_items"] == overview["overview"]["work_items"]
    assert before_files == {path.name: path.read_bytes() for path in config.iterdir() if path.is_file()}
    with sqlite3.connect(data / "pmt.sqlite3") as connection:
        assert connection.execute("SELECT revision,state FROM records WHERE id=?", (record,)).fetchone() == (1, "Planned")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    hook_env = dict(os.environ, PYTHONPATH="", PMT_PYTHON=sys.executable,
                    PMT_DATA_ROOT=str(data), PMT_CONFIG_ROOT=str(config), PMT_SCOPE_ID=scope)
    if product == "opencode":
        hook = reinstalled / "integrations/opencode/bridge.py"
        arguments = ["--product", "opencode", "--read-context"]
        raw = {"session_id": "packaged-opencode-fresh", "native_event": "session.created"}
    else:
        hook = reinstalled / ("integrations/" + product + "/hook.py")
        arguments = ["--event", "SessionStart", "--with-context"]
        raw = {"session_id": "packaged-" + product + "-fresh", "hook_event_name": "SessionStart", "source": "startup"}
    hooked = subprocess.run([sys.executable, str(hook), *arguments], input=json.dumps(raw),
                           capture_output=True, text=True, encoding="utf-8", env=hook_env, cwd=unrelated,
                           timeout=20, check=False)
    assert hooked.returncode == 0, hooked.stderr
    native = json.loads(hooked.stdout)
    delivered = native.get("context_markdown") if product == "opencode" else native["hookSpecificOutput"]["additionalContext"]
    assert scope in delivered and "metadata overview" in delivered
    assert not list((data / "hook-pending").glob("*.json"))


def test_portable_package_uses_actual_tls_host_without_local_database(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "config", tmp_path / "data"
    _configure(env, config)
    built = _build(tmp_path / "distribution")
    package = Path(built["products"]["codex"]["directory"])
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    request = _host_request(env, "capture_work_basis", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "inventory_paths": [env["relative"]]})
    result = _call(package, data, config, request, unrelated)["result"]
    assert result["complete"] is True and result["source_provenance"] == "client_attested"
    assert result["host_git_verified"] is False
    assert not list(data.rglob("*.sqlite3"))
    assert env["checkout"].as_posix() not in json.dumps(result)
    with env["db"].connect() as connection:
        assert connection.execute("SELECT id FROM continuity_objects WHERE id=? AND kind='basis'",
                                  (result["basis_ref"],)).fetchone()
