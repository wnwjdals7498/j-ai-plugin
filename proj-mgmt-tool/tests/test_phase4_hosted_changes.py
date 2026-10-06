"""R2 hosted change collection uses client Git/local detail and the TLS Host store."""
from __future__ import annotations

import json

import pytest

pytest_plugins = ["test_phase3_host_network"]

from test_phase3_hosted_cli import _cli, _configure
from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout
from pmt.util import canonical_json


def _capture(env, config, data, paths):
    request = _host_request(env, "capture_work_basis", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "inventory_paths": paths})
    result = _cli(config, data, request)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)["result"]


def test_hosted_collect_changes_reads_client_git_and_keeps_detail_local(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    config, data = tmp_path / "changes-config", tmp_path / "changes-data"
    _configure(env, config)
    paths = [env["relative"]]
    before = _capture(env, config, data, paths)
    graph = json.loads(env["graph_path"].read_text(encoding="utf-8"))
    graph["nodes"][0]["summary"] += " (observed fixture edit)"
    env["graph_path"].write_text(canonical_json(graph) + "\n", encoding="utf-8")
    after = _capture(env, config, data, paths)
    request = _host_request(env, "collect_changes", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "paths": paths,
        "before_basis_ref": before["basis_ref"], "after_basis_ref": after["basis_ref"],
        "expected_pointer_revision": 0})
    captured = _cli(config, data, request)
    assert captured.returncode == 0, captured.stdout + captured.stderr
    result = json.loads(captured.stdout)["result"]
    assert result["coverage"] == "complete"
    assert result["after_basis_ref"] == after["basis_ref"]
    assert result["pointer"]["revision"] == 1
    assert str(env["checkout"]) not in canonical_json(result)
    assert not list(data.rglob("*.sqlite3")), "Hosted changes must not create a local primary DB"

    replay = _cli(config, data, request)
    assert replay.returncode == 0, replay.stdout + replay.stderr
    replay_result = json.loads(replay.stdout)["result"]
    assert replay_result["replayed"] is True
    assert replay_result["change_ref"] == result["change_ref"]
    stale = _host_request(env, "collect_changes", request["payload"] | {"expected_pointer_revision": 0})
    rejected = _cli(config, data, stale)
    assert rejected.returncode == 3
    assert json.loads(rejected.stdout)["error"]["code"] == "revision_conflict"

    links = _host_request(env, "build_implementation_links", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "task_id": env["item"], "paths": paths, "basis_ref": after["basis_ref"],
        "expected_pointer_revision": 0})
    linked = _cli(config, data, links)
    assert linked.returncode == 0, linked.stdout + linked.stderr
    index = json.loads(linked.stdout)["result"]
    fetched = _cli(config, data, _host_request(env, "get_continuity_object", {
        "object_id": index["index_ref"], "kind": "link_index"}))
    assert fetched.returncode == 0, fetched.stdout + fetched.stderr
    index_body = json.loads(fetched.stdout)["result"]["body"]
    assert index_body["basis_ref"] == after["basis_ref"]
    assert str(env["checkout"]) not in canonical_json(index_body)
    assert env["relative"] not in canonical_json(index_body)

    detail_request = _host_request(env, "read_change_slice", {
        "change_ref": result["change_ref"], "include_detail": True,
        "paths": paths, "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"]})
    read = _cli(config, data, detail_request)
    assert read.returncode == 0, read.stdout + read.stderr
    detail = json.loads(read.stdout)["result"]["detail"]
    assert detail["available"] and detail["complete"]
    assert detail["diff_sha256"]
    assert "observed fixture edit" in __import__("base64").b64decode(detail["diff_base64"]).decode()
    assert not list(data.rglob("*.sqlite3"))
