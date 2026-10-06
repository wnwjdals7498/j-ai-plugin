"""Hosted R4 context through two real clients and loopback HTTPS Host storage."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

pytest_plugins = ["test_phase3_host_network"]

from pmt.hosted_context import HostedContextClient
from pmt.storage_config import _read_profile, configure_storage
from pmt.util import canonical_json, new_id
from test_phase3_hosted_cli import _cli, _configure
from test_phase3_hosted_runtime import _host_request, _seed_hosted_git_checkout


def _client(env, tmp_path, side="a", monkeypatch=None):
    config = tmp_path / f"hosted-context-config-{side}"
    data = tmp_path / f"hosted-context-data-{side}"
    if side == "a":
        _configure(env, config)
        store, device, session = env["store_a"], env["headers"]["x-pmt-device"], env["session_a"]
        actor = env["actor"]
        token_name = "PMT_CLIENT_A_TOKEN"
    else:
        config.mkdir()
        (config / "profile.json").write_text(canonical_json({
            "environment_id": env["environment_b"]}), encoding="utf-8")
        configure_storage(config, {"mode": "hosted", "expected_config_sha256": None,
            "endpoint": env["base"], "credential_env": "PMT_CLIENT_B_TOKEN",
            "device_id": env["device_b"]["device_id"], "namespace_id": env["headers"]["x-pmt-namespace"],
            "ca_file": str(env["cert"]), "workspace_mappings": [{
                "repository_id": env["repo"], "project_id": env["project"], "branch": "main",
                "branch_key_sha256": hashlib.sha256(b"main").hexdigest(),
                "local_root": str(env["checkout"]), "relative_graph_path": env["relative"]}]})
        store, device, session = env["store_b"], env["device_b"]["device_id"], env["session_b"]
        actor = "network-client-b"
        token_name = "PMT_CLIENT_B_TOKEN"
    profile, _ = _read_profile(config)
    return {"client": HostedContextClient(profile, store, data), "config": config, "data": data,
            "store": store, "device": device, "session": session, "actor": actor,
            "token_name": token_name}


def _capture(env, client_info, *, request_id=None, paths=None, run_revision=3):
    payload = {"repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"],
        "expected_run_revision": run_revision, "inventory_paths": paths or [env["relative"]]}
    req = _host_request(env, "capture_work_basis", payload, request_id=request_id)
    req["actor"] = client_info["actor"]
    req["session_id"] = client_info["session"]
    return _cli(client_info["config"], client_info["data"], req)


def _context_request(env, client_info, operation, *, basis_ref, context_ref=None,
                     request_id=None, budget=None, cursor=None, max_bytes=1024, max_lines=40,
                     inventory_paths=None, run_revision=3, change_ref=None,
                     assessment_ref=None, resolution_ref=None, applicability_ref=None):
    payload = {"basis_ref": basis_ref, "repository_id": env["repo"],
        "workspace": str(env["checkout"]), "relative_graph_path": env["relative"],
        "run_id": env["run"], "expected_run_revision": run_revision,
        "task_ref": {"task_id": env["item"], "step_id": env["step"], "run_id": env["run"]},
        "role": "lower", "node_ids": [node["id"] for node in env["graph"]["nodes"]],
        "inventory_paths": inventory_paths or [env["relative"]],
        "budget": {"max_bytes": 12000, "max_lines": 160},
        "f5_budget": budget or {"max_bytes": 512, "max_lines": 40, "unit": "utf8"},
        "max_bytes": max_bytes, "max_lines": max_lines}
    if context_ref is not None:
        payload["context_ref"] = context_ref
    for key, value in (("change_ref", change_ref), ("assessment_ref", assessment_ref),
                       ("resolution_ref", resolution_ref), ("applicability_ref", applicability_ref)):
        if value is not None:
            payload[key] = value
    if cursor is not None:
        payload["cursor"] = cursor
    req = _host_request(env, operation, payload, request_id=request_id)
    req["actor"] = client_info["actor"]
    req["session_id"] = client_info["session"]
    return req


def test_hosted_r4_bundle_links_actual_change_assessment_and_unapplied_state(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    client_info = _client(env, tmp_path, "a")
    before_response = _capture(env, client_info, paths=[env["relative"]])
    assert before_response.returncode == 0, before_response.stdout + before_response.stderr
    before = json.loads(before_response.stdout)["result"]

    graph = json.loads(env["graph_path"].read_text(encoding="utf-8"))
    graph["nodes"][0]["summary"] += " (R4 actual source change)"
    env["graph_path"].write_text(canonical_json(graph) + "\n", encoding="utf-8")
    after_response = _capture(env, client_info, paths=[env["relative"]])
    assert after_response.returncode == 0, after_response.stdout + after_response.stderr
    after = json.loads(after_response.stdout)["result"]

    changed = _cli(client_info["config"], client_info["data"], _host_request(env, "collect_changes", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"],
        "paths": [env["relative"]], "before_basis_ref": before["basis_ref"],
        "after_basis_ref": after["basis_ref"], "expected_pointer_revision": 0}))
    assert changed.returncode == 0, changed.stdout + changed.stderr
    change = json.loads(changed.stdout)["result"]

    links = _cli(client_info["config"], client_info["data"], _host_request(env, "build_implementation_links", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"],
        "paths": [env["relative"]], "basis_ref": after["basis_ref"],
        "expected_pointer_revision": 0}))
    assert links.returncode == 0, links.stdout + links.stderr
    index = json.loads(links.stdout)["result"]
    assessment_request = _host_request(env, "assess_alignment", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"], "task_id": env["item"],
        "paths": [env["relative"]], "basis_ref": after["basis_ref"],
        "change_ref": change["change_ref"], "index_ref": index["index_ref"]})
    assessed = _cli(client_info["config"], client_info["data"], assessment_request)
    assert assessed.returncode == 0, assessed.stdout + assessed.stderr
    assessment = json.loads(assessed.stdout)["result"]
    current_response = _capture(env, client_info, paths=[env["relative"]])
    assert current_response.returncode == 0, current_response.stdout + current_response.stderr
    current = json.loads(current_response.stdout)["result"]
    indexed = _cli(client_info["config"], client_info["data"], _host_request(env, "rebuild_graph_index", {
        "repository_id": env["repo"], "workspace": str(env["checkout"]),
        "relative_graph_path": env["relative"], "run_id": env["run"],
        "expected_source": current["source_pin"]}))
    assert indexed.returncode == 0, indexed.stdout + indexed.stderr

    request = _context_request(env, client_info, "compose_task_resume", basis_ref=current["basis_ref"],
        inventory_paths=[env["relative"]], change_ref=change["change_ref"],
        assessment_ref=assessment["assessment_ref"])
    composed, code = _run(client_info, request)
    assert code == 0, composed.get("error")
    evidence = composed["result"]["change_evidence"]
    assert evidence["change_ref"] == change["change_ref"]
    assert evidence["assessment_ref"] == assessment["assessment_ref"]
    assert evidence["applicability_status"] == "unknown"
    assert evidence["applied"] is False
    assert evidence["status"] == "change_requires_review"
    assert composed["result"]["complete"] is False
    assert composed["result"]["next_action"]["executable"] is False
    assert not list(client_info["data"].rglob("*.sqlite3"))


def _run(client_info, request):
    return client_info["client"].execute(request)


def test_hosted_context_build_and_detail_reuse_host_f5_and_current_source(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    client_info = _client(env, tmp_path, "a")
    decision = _host_request(env, "save_decision", {"decision_kind": "custom",
        "decider": "measurement fixture", "content": "Preserve the current source direction",
        "reason": "Actual hosted current checkpoint fixture", "confirmation_source": "user_selected"})
    decision["record_id"] = env["item"]
    decision["expected_revision"] = 1
    decision_result = _cli(client_info["config"], client_info["data"], decision)
    assert decision_result.returncode == 0, decision_result.stdout + decision_result.stderr
    with env["db"].connect() as conn:
        boundary = conn.execute("SELECT event_id FROM events WHERE event_type='decision_saved' "
            "AND scope_id=? AND record_id=? ORDER BY recorded_at DESC LIMIT 1",
            (env["project"], env["item"])).fetchone()[0]
    captured = _capture(env, client_info)
    assert captured.returncode == 0, captured.stdout + captured.stderr
    captured_result = json.loads(captured.stdout)["result"]
    basis_ref = captured_result["basis_ref"]
    checkpoint = _host_request(env, "create_checkpoint", {"basis_ref": basis_ref,
        "boundary_event_id": boundary, "expected_pointer_revision": 0})
    checkpoint_response = _cli(client_info["config"], client_info["data"], checkpoint)
    assert checkpoint_response.returncode == 0, checkpoint_response.stdout + checkpoint_response.stderr
    compose = _context_request(env, client_info, "compose_task_resume", basis_ref=basis_ref)
    composed = _cli(client_info["config"], client_info["data"], compose)
    assert composed.returncode == 0, composed.stdout + composed.stderr
    result = json.loads(composed.stdout)
    context_ref = result["result"].get("context_ref")
    assert context_ref and context_ref["kind"] == "task_context"
    assert result["result"]["owner_current"] is True
    assert result["result"]["source_provenance"] == "client_attested"
    assert result["result"]["host_git_verified"] is False
    assert result["result"]["change_evidence"]["status"] == "no_change_confirmed"
    assert result["result"]["change_evidence"]["checkpoint_ref"]
    assert result["result"]["next_action"]["executable"] is False
    assert env["checkout"].as_posix() not in canonical_json(result)
    assert not list(client_info["data"].rglob("*.sqlite3"))

    cursor = result["result"].get("detail_cursor")
    assert cursor
    detail_request = _context_request(env, client_info, "read_resume_detail", basis_ref=basis_ref,
                                      context_ref=context_ref, cursor=cursor)
    detail_response = _cli(client_info["config"], client_info["data"], detail_request)
    assert detail_response.returncode == 0, detail_response.stdout + detail_response.stderr
    detail = json.loads(detail_response.stdout)
    assert detail["result"]["owner_revalidated"] is True
    assert detail["result"]["source_revalidated"] is True
    assert detail["result"]["detail"]["content"]

    malformed = _context_request(env, client_info, "read_resume_detail", basis_ref=basis_ref,
        context_ref=context_ref, cursor=cursor[:-1] + ("A" if cursor[-1] != "A" else "B"))
    invalid_response = _cli(client_info["config"], client_info["data"], malformed)
    assert invalid_response.returncode in {2, 3}
    invalid = json.loads(invalid_response.stdout)
    assert invalid["error"]["code"] in {"context_cursor_invalid", "context_cursor_stale"}


def test_hosted_context_invalidates_old_basis_after_local_source_change(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    client_info = _client(env, tmp_path, "a")
    extra = env["checkout"] / "README.md"
    extra.write_text("initial selected inventory\n", encoding="utf-8")
    captured = _capture(env, client_info, paths=[env["relative"], "README.md"])
    assert captured.returncode == 0, captured.stdout + captured.stderr
    basis_ref = json.loads(captured.stdout)["result"]["basis_ref"]
    valid_request = _context_request(env, client_info, "validate_basis", basis_ref=basis_ref,
                                     budget={"max_bytes": 8000, "max_lines": 100},
                                     inventory_paths=[env["relative"], "README.md"])
    valid_response = _cli(client_info["config"], client_info["data"], valid_request)
    assert valid_response.returncode == 0, valid_response.stdout + valid_response.stderr
    valid = json.loads(valid_response.stdout)
    assert valid["result"]["status"] == "unchanged", valid["result"]

    extra.write_text("source changed after capture\n", encoding="utf-8")
    stale_response = _cli(client_info["config"], client_info["data"], valid_request)
    assert stale_response.returncode == 0, stale_response.stdout + stale_response.stderr
    stale = json.loads(stale_response.stdout)
    assert stale["result"]["status"] == "changed"
    assert "source" in stale["result"]["changed_components"]

    compose = _context_request(env, client_info, "compose_task_resume", basis_ref=basis_ref,
                               inventory_paths=[env["relative"], "README.md"])
    rejected_response = _cli(client_info["config"], client_info["data"], compose)
    assert rejected_response.returncode == 0, rejected_response.stdout + rejected_response.stderr
    rejected = json.loads(rejected_response.stdout)
    assert rejected["result"]["complete"] is False
    assert rejected["result"].get("context_ref") is None
    assert rejected["result"]["basis_status"] == "changed"


def test_new_host_device_and_session_must_build_a_new_private_context(live_host, tmp_path):
    env = _seed_hosted_git_checkout(live_host, tmp_path)
    client_a = _client(env, tmp_path, "a")
    captured_a = _capture(env, client_a)
    assert captured_a.returncode == 0, captured_a.stdout + captured_a.stderr
    basis_a = json.loads(captured_a.stdout)["result"]["basis_ref"]
    compose_a, code = _run(client_a, _context_request(env, client_a, "compose_task_resume", basis_ref=basis_a))
    assert code == 0, compose_a.get("error")
    context_a = compose_a["result"]["context_ref"]

    # This isolated fixture represents a new current Host run owner. All
    # subsequent client calls use the actual HTTPS auth/session of device B.
    with env["db"].write() as conn:
        row = conn.execute("SELECT revision FROM execution_runs WHERE id=?", (env["run"],)).fetchone()
        revision_b = row["revision"] + 1
        conn.execute("UPDATE execution_runs SET owner_session=?,revision=?,updated_at=? WHERE id=?",
                     (env["session_b"], revision_b, "2026-10-06T00:00:01Z", env["run"]))
        conn.execute("UPDATE scope_locks SET owner_session=? WHERE run_id=?",
                     (env["session_b"], env["run"]))

    client_b = _client(env, tmp_path, "b")
    captured_b = _capture(env, client_b, run_revision=revision_b)
    assert captured_b.returncode == 0, captured_b.stdout + captured_b.stderr
    basis_b = json.loads(captured_b.stdout)["result"]["basis_ref"]
    old_context_req = _context_request(env, client_b, "read_resume_detail", basis_ref=basis_b,
                                       context_ref=context_a,
                                       cursor=compose_a["result"]["detail_cursor"], run_revision=revision_b)
    denied, code = _run(client_b, old_context_req)
    assert code == 3
    assert denied["error"]["code"] in {"context_not_found", "ownership_conflict"}

    compose_b, code = _run(client_b, _context_request(env, client_b,
                                    "compose_task_resume", basis_ref=basis_b, run_revision=revision_b))
    assert code == 0, compose_b.get("error")
    context_b = compose_b["result"]["context_ref"]
    assert context_b["id"] != context_a["id"]
    assert context_b["scope_id"] == env["project"]
    assert context_b["source_hash"] == context_a["source_hash"]
    assert not list(client_a["data"].rglob("*.sqlite3"))
    assert not list(client_b["data"].rglob("*.sqlite3"))

    with env["db"].write() as conn:
        revision_revoked = revision_b + 1
        conn.execute("UPDATE execution_runs SET owner_session=?,revision=?,updated_at=? WHERE id=?",
                     (env["session_a"], revision_revoked, "2026-10-06T00:00:02Z", env["run"]))
        conn.execute("UPDATE scope_locks SET owner_session=? WHERE run_id=?",
                     (env["session_a"], env["run"]))
    revoked_request = _context_request(env, client_b, "read_resume_detail", basis_ref=basis_b,
        context_ref=context_b, cursor=compose_b["result"]["detail_cursor"],
        run_revision=revision_revoked)
    revoked, code = _run(client_b, revoked_request)
    assert code == 3
    assert revoked["error"]["code"] in {"ownership_conflict", "workspace_authority_stale"}
