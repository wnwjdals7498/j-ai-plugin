"""Isolated request and subprocess fixtures for the PMT CLI acceptance tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest


@dataclass(frozen=True)
class Cli:
    cwd: Path
    data_root: Path
    config_root: Path
    env: dict[str, str]

    def call(self, request: dict[str, Any] | str, *, cwd: Path | None = None,
             timeout: float = 20) -> subprocess.CompletedProcess[str]:
        wire = request if isinstance(request, str) else json.dumps(
            request, ensure_ascii=False, separators=(",", ":")
        )
        return subprocess.run(
            [sys.executable, "-m", "pmt", "--data-root", str(self.data_root),
             "--config-root", str(self.config_root)],
            input=wire, text=True, encoding="utf-8", capture_output=True,
            cwd=cwd or self.cwd, env=self.env, timeout=timeout, check=False,
        )


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    """Per-test paths: no test can touch a developer's configured PMT data."""
    data, config = tmp_path / "사용자 데이터", tmp_path / "설정 공간"
    data.mkdir()
    config.mkdir()
    return data, config


@pytest.fixture
def cli(roots: tuple[Path, Path]) -> Cli:
    data, config = roots
    checkout = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PMT_DATA_ROOT"] = str(data)
    env["PMT_CONFIG_ROOT"] = str(config)
    src = str(checkout / "src")
    env["PYTHONPATH"] = src + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    # Suppress Python startup noise without changing the machine protocol under test.
    env["PYTHONUTF8"] = "1"
    return Cli(checkout, data, config, env)


@pytest.fixture
def request_factory():
    """Build protocol-v1 envelopes with stable session identity by default."""
    stable_session = "test-main"

    def make(operation: str, payload: dict[str, Any] | None = None, **overrides: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "protocol_version": 1,
            "operation": operation,
            "request_id": str(uuid.uuid4()),
            "actor": "main",
            "session_id": stable_session,
            "payload": payload or {},
        }
        request.update(overrides)
        return request

    return make


@pytest.fixture
def create_project(cli: Cli, request_factory):
    def create(slug: str = "시험 프로젝트") -> str:
        result = cli.call(request_factory("create_scope", {
            "kind": "project", "slug": slug,
        }))
        assert result.returncode == 0, result.stderr
        envelope = _one_json_line(result.stdout)
        assert envelope["ok"] is True
        scope_id = envelope["result"].get("scope_id") or envelope["result"].get("id")
        assert scope_id
        return scope_id
    return create


def _one_json_line(stdout: str) -> dict[str, Any]:
    lines = stdout.splitlines()
    assert len(lines) == 1, f"expected one stdout JSON line; got {len(lines)}"
    value = json.loads(lines[0])
    assert isinstance(value, dict)
    return value


@pytest.fixture
def parse_cli_response():
    return _one_json_line
