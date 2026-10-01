"""Acceptance tests that exercise the public CLI through real child processes."""
from __future__ import annotations

import json
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from conftest import Cli


def envelope(completed: subprocess.CompletedProcess[str]) -> dict:
    lines = completed.stdout.splitlines()
    assert len(lines) == 1, f"stdout must contain exactly one JSON line: {completed.stdout!r}"
    decoded = json.loads(lines[0])
    assert isinstance(decoded, dict)
    assert {"protocol_version", "request_id", "ok", "result", "error", "warnings"} <= decoded.keys()
    return decoded


def scope(cli: Cli, request_factory, slug="alpha") -> str:
    cp = cli.call(request_factory("create_scope", {"kind": "project", "slug": slug}))
    assert cp.returncode == 0, cp.stderr
    response = envelope(cp)
    assert response["ok"] is True
    return response["result"].get("scope_id") or response["result"]["id"]


def item(cli: Cli, request_factory, scope_id: str, title="CLI test item") -> tuple[str, int]:
    cp = cli.call(request_factory("save_change", {
        "kind": "item", "title": title,
        "body": {"criteria": ["C1"], "workspace": ".", "next": "검증"},
        "reason": "CLI 통합 시험 fixture",
    }, scope_id=scope_id))
    assert cp.returncode == 0, cp.stderr
    result = envelope(cp)["result"]
    return result.get("record_id") or result["id"], result.get("revision", 1)


def test_cli_01_utf8_one_line_no_stdout_diagnostics(cli, request_factory):
    """CLI-01: UTF-8 request round trips with protocol-only stdout."""
    request = request_factory("create_scope", {"kind": "project", "slug": "한글 프로젝트"})
    cp = cli.call(request)
    assert cp.returncode == 0
    response = envelope(cp)
    assert response["protocol_version"] == 1
    assert response["request_id"] == request["request_id"]
    assert response["ok"] is True
    assert response["result"]


@pytest.mark.parametrize("wire", ["{", '{"protocol_version":1,"protocol_version":1}'])
def test_cli_02_parse_errors_are_json_and_exit_two(cli, wire):
    """CLI-02: malformed and duplicate-key JSON have parse-error envelopes."""
    cp = cli.call(wire)
    assert cp.returncode == 2
    response = envelope(cp)
    assert response["request_id"] is None
    assert response["ok"] is False
    assert response["error"]


@pytest.mark.parametrize("invalid_request", [
    {"protocol_version": 1, "operation": "read_context"},
    {"protocol_version": 999, "operation": "read_context", "request_id": str(uuid.uuid4()),
     "actor": "main", "session_id": "s", "payload": {}},
    {"protocol_version": 1, "operation": "not_an_operation", "request_id": str(uuid.uuid4()),
     "actor": "main", "session_id": "s", "payload": {}},
])
def test_cli_02_schema_version_and_operation_rejected(cli, invalid_request):
    cp = cli.call(invalid_request)
    assert cp.returncode == 2
    response = envelope(cp)
    assert response["ok"] is False
    assert response["error"]


def test_cli_02_oversized_json_rejected(cli):
    # Contract v1 caps request input at 1 MiB.
    wire = json.dumps({"protocol_version": 1, "operation": "read_context", "payload": {"x": "a" * (1024 * 1024)}})
    cp = cli.call(wire, timeout=30)
    assert cp.returncode == 2
    assert envelope(cp)["ok"] is False


def test_cli_03_cwd_with_spaces_and_korean_uses_configured_data_root(cli, request_factory, tmp_path):
    """CLI-03: invocation cwd does not choose or relocate the PMT database."""
    project_id = scope(cli, request_factory, "경로 유지")
    external_cwd = tmp_path / "외부 작업 공간 with spaces"
    external_cwd.mkdir()
    first_request = request_factory("read_context", {}, scope_id=project_id)
    second_request = request_factory("read_context", {}, scope_id=project_id)
    first = cli.call(first_request)
    second = cli.call(second_request, cwd=external_cwd)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    a, b = envelope(first), envelope(second)
    assert a["ok"] is b["ok"] is True
    assert a["result"] == b["result"]


def test_idem_01_same_request_key_replays_original_result(cli, request_factory):
    request = request_factory("create_scope", {"kind": "project", "slug": "once"})
    first = cli.call(request)
    reordered = dict(reversed(list(request.items())))
    second = cli.call(reordered)
    assert first.returncode == second.returncode == 0
    a, b = envelope(first), envelope(second)
    assert a["result"] == b["result"]


def test_idem_02_same_request_key_with_different_meaning_conflicts(cli, request_factory):
    request = request_factory("create_scope", {"kind": "project", "slug": "original"})
    first = cli.call(request)
    assert first.returncode == 0
    changed = json.loads(json.dumps(request))
    changed["payload"]["slug"] = "changed"
    second = cli.call(changed)
    assert second.returncode == 3
    assert envelope(second)["ok"] is False


def test_claim_01_two_independent_processes_claim_only_once(cli, request_factory):
    """CLAIM-01: concurrent child processes contend for one stored item."""
    project_id = scope(cli, request_factory, "claim-race")
    record_id, revision = item(cli, request_factory, project_id)
    gate = threading.Barrier(2)
    requests = [request_factory("claim_task", {
    }, record_id=record_id, expected_revision=revision, session_id=f"worker-{n}") for n in (1, 2)]

    def contender(req):
        gate.wait(timeout=10)
        return cli.call(req)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(contender, requests))
    responses = [envelope(cp) for cp in outcomes]
    assert sum(response["ok"] is True for response in responses) == 1
    assert sum(cp.returncode == 0 for cp in outcomes) == 1
    assert all(cp.returncode in (0, 3) for cp in outcomes)


def test_rev_01_two_processes_with_same_revision_have_one_winner(cli, request_factory):
    """REV-01: stale revision loses when independent CLI processes race."""
    project_id = scope(cli, request_factory, "revision-race")
    record_id, revision = item(cli, request_factory, project_id)
    gate = threading.Barrier(2)
    requests = [request_factory("save_change", {
        "title": f"revision winner {n}", "reason": "REV-01 경쟁 변경",
    }, record_id=record_id, expected_revision=revision) for n in (1, 2)]

    def contender(req):
        gate.wait(timeout=10)
        return cli.call(req)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(contender, requests))
    assert sorted(cp.returncode for cp in outcomes) == [0, 3]
    assert sum(envelope(cp)["ok"] is True for cp in outcomes) == 1


def test_cli_02_invalid_payload_does_not_claim_or_mutate(cli, request_factory):
    """Invalid lifecycle input is rejected before a successful state change."""
    project_id = scope(cli, request_factory, "invalid-payload")
    record_id, revision = item(cli, request_factory, project_id)
    request = request_factory("claim_task", {
        "unexpected": True,
    }, record_id=record_id, expected_revision=revision, claim_token="invalid-token-from-test")
    cp = cli.call(request)
    assert cp.returncode in (2, 3)
    assert envelope(cp)["ok"] is False
