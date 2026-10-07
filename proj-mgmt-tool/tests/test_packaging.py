"""PKG-01 preparation checks; no product install or plugin registration is performed."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from pmt import __version__ as CORE_VERSION
from pmt.db import SCHEMA_VERSION


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("pmt_build_plugins", ROOT / "scripts" / "build_plugins.py")
BUILDER = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(BUILDER)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def tree_hashes(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): sha256(path)
            for path in sorted(root.rglob("*")) if path.is_file()}


def build(tmp_path: Path, version=None):
    return BUILDER.build_plugins(tmp_path / "distribution", version, ROOT)


def test_schema_version_metadata_resolves_static_phase3_alias_without_executing_source(tmp_path):
    root = tmp_path / "fixture"
    package_dir = root / "src" / "pmt"
    package_dir.mkdir(parents=True)
    marker = tmp_path / "source-was-executed"
    (package_dir / "phase3_schema.py").write_text(
        f"SCHEMA_VERSION = 4\nfrom pathlib import Path\nPath({str(marker)!r}).touch()\n", encoding="utf-8")
    db_source = ("from .phase3_schema import SCHEMA as PHASE3_SCHEMA, "
                 "SCHEMA_VERSION as PHASE3_SCHEMA_VERSION\n"
                 "SCHEMA_VERSION = PHASE3_SCHEMA_VERSION\n")
    assert BUILDER._static_schema_version(root, db_source) == 4
    assert not marker.exists()


def test_pkg_01_three_separate_bundles_manifest_and_zip_hashes(tmp_path):
    built = build(tmp_path, "0.1.1")
    assert built["version"] == "0.1.1"
    assert built["core_version"] == CORE_VERSION
    assert built["schema_version"] == SCHEMA_VERSION
    for product in ("codex", "claude", "opencode"):
        package = Path(built["products"][product]["directory"])
        archive = Path(built["products"][product]["zip"])
        manifest = json.loads((package / "pmt-package.json").read_text(encoding="utf-8"))
        assert manifest["plugin_version"] == "0.1.1"
        assert manifest["core_version"] == CORE_VERSION
        assert manifest["protocol_version"] == 1
        assert manifest["schema_version"] == SCHEMA_VERSION
        assert set(manifest["files"]) == set(tree_hashes(package)) - {"pmt-package.json"}
        for relative, digest in manifest["files"].items():
            assert sha256(package / Path(relative)) == digest
        assert sha256(archive) == built["products"][product]["zip_sha256"]
        with zipfile.ZipFile(archive) as zipped:
            names = zipped.namelist()
            assert set(names) == set(tree_hashes(package))
            assert all(not PurePosixPath(name).is_absolute() and ".." not in PurePosixPath(name).parts for name in names)
        assert (package / "src" / "pmt" / "cli.py").is_file()
        assert (package / "scripts" / "pmt.py").is_file()
        assert (package / "skills" / "proj-mgmt-tool" / "SKILL.md").is_file()
        assert (package / "skills" / "proj-mgmt-tool" / "references" / "cli-workflow.md").is_file()
        assert not any(part in {".venv", "__pycache__", "tests", ".git"} for path in package.rglob("*") for part in path.parts)
        for path in package.rglob("*"):
            if path.is_file() and path.suffix in {".json", ".md", ".py", ".js"}:
                assert str(ROOT).lower() not in path.read_text(encoding="utf-8").lower()

    codex = Path(built["products"]["codex"]["directory"])
    assert (codex / "plugin.json").is_file()
    assert (codex / ".codex-plugin" / "plugin.json").is_file()
    codex_portable = json.loads((codex / "plugin.json").read_text(encoding="utf-8"))
    assert {"name", "version", "description"} == set(codex_portable)
    codex_compat = json.loads((codex / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert codex_compat["hooks"] == "./hooks/hooks.json"
    assert (codex / "integrations" / "codex" / "hook.py").is_file()
    assert sha256(codex / "integrations" / "codex" / "hook.py") == sha256(ROOT / "integrations" / "codex" / "hook.py")
    codex_market = json.loads((codex / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8"))
    entry = codex_market["plugins"][0]
    assert entry["source"] == {"source": "local", "path": "./"}
    assert {"installation", "authentication"} <= entry["policy"].keys()

    claude = Path(built["products"]["claude"]["directory"])
    assert (claude / "integrations" / "claude" / "hook.py").is_file()
    assert sha256(claude / "integrations" / "claude" / "hook.py") == sha256(ROOT / "integrations" / "claude" / "hook.py")
    claude_market = json.loads((claude / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
    assert {"name", "owner", "plugins"} <= claude_market.keys()
    assert claude_market["plugins"][0]["source"] == "./"
    claude_plugin = json.loads((claude / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    python_option = claude_plugin["userConfig"]["python_path"]
    assert python_option["type"] == "file" and python_option["required"] is True
    assert "default" not in python_option
    claude_hooks = json.loads((claude / "hooks" / "hooks.json").read_text(encoding="utf-8"))["hooks"]
    for groups in claude_hooks.values():
        for hook in (item for group in groups for item in group["hooks"]):
            # Exec form keeps the configured absolute interpreter a single argv entry.
            assert hook["command"] == "${user_config.python_path}"
            assert hook["args"][0] == "${CLAUDE_PLUGIN_ROOT}/integrations/claude/hook.py"

    opencode = Path(built["products"]["opencode"]["directory"])
    package_json = json.loads((opencode / "package.json").read_text(encoding="utf-8"))
    assert package_json["type"] == "module"
    assert package_json["main"] == "./integrations/opencode/pmt.js"
    assert (opencode / package_json["main"][2:]).is_file()
    assert (opencode / "integrations" / "opencode" / "bridge.py").is_file()


def test_pkg_01_standalone_cli_works_from_unrelated_cwd(tmp_path):
    built = build(tmp_path, "0.1.1")
    package = Path(built["products"]["codex"]["directory"])
    cwd = tmp_path / "fresh cwd with spaces 한글"
    data, config = tmp_path / "isolated data", tmp_path / "isolated config"
    cwd.mkdir()
    data.mkdir()
    config.mkdir()
    request = {
        "protocol_version": 1, "operation": "setup",
        "request_id": "550e8400-e29b-41d4-a716-446655440010",
        "actor": "main", "session_id": "package-smoke", "payload": {"product": "cli"},
    }
    env = os.environ.copy()
    env["PYTHONPATH"] = ""
    env["PMT_DATA_ROOT"] = str(data)
    env["PMT_CONFIG_ROOT"] = str(config)
    completed = subprocess.run(
        [sys.executable, str(package / "scripts" / "pmt.py"), "--data-root", str(data), "--config-root", str(config)],
        input=json.dumps(request, ensure_ascii=False), text=True, encoding="utf-8",
        capture_output=True, cwd=cwd, env=env, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    assert len(lines) == 1
    response = json.loads(lines[0])
    assert response["ok"] is True
    assert response["request_id"] == request["request_id"]
    assert response["result"]["schema_version"] == SCHEMA_VERSION


def test_pkg_01_second_version_does_not_replace_earlier_bundle(tmp_path):
    first = build(tmp_path, "0.1.0")
    old_dir = Path(first["products"]["claude"]["directory"])
    old_archive = Path(first["products"]["claude"]["zip"])
    before_dir, before_zip = tree_hashes(old_dir), sha256(old_archive)
    second = build(tmp_path, "0.1.1")
    assert Path(second["output"]).name == "0.1.1"
    assert tree_hashes(old_dir) == before_dir
    assert sha256(old_archive) == before_zip


def test_pkg_01_existing_version_is_preserved_and_refused(tmp_path):
    first = build(tmp_path, "0.1.0")
    old_dir = Path(first["products"]["codex"]["directory"])
    before = tree_hashes(old_dir)
    with pytest.raises(BUILDER.BuildError):
        build(tmp_path, "0.1.0")
    assert tree_hashes(old_dir) == before


def test_pkg_01_source_mutation_during_copy_aborts_publication(tmp_path, monkeypatch):
    output = tmp_path / "distribution"
    original_copy = BUILDER._copy_one
    changed = False

    def mutate_after_copy(source, destination):
        nonlocal changed
        original_copy(source, destination)
        if not changed and source.name == "cli.py":
            changed = True
            source.write_text(source.read_text(encoding="utf-8") + "\n# source changed during packaging test\n", encoding="utf-8")

    # Mutate only a private fixture source tree.
    source_root = tmp_path / "source"
    import shutil
    shutil.copytree(ROOT, source_root, ignore=shutil.ignore_patterns(".git", ".venv", ".pytest_cache", ".pytest-tmp", ".pmt-test", "__pycache__"))
    monkeypatch.setattr(BUILDER, "_copy_one", mutate_after_copy)
    with pytest.raises(BUILDER.BuildError, match="source changed"):
        BUILDER.build_plugins(output, "0.1.1", source_root)
    assert changed
    assert not (output / "0.1.1").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-violation retry policy")
def test_pkg_01_retries_one_temporary_windows_publish_denial(tmp_path, monkeypatch):
    output = tmp_path / "distribution"
    actual_replace = BUILDER.os.replace
    attempts = []

    def deny_once(source, destination):
        attempts.append((source, destination))
        if len(attempts) == 1:
            error = PermissionError(13, "simulated temporary publish denial")
            error.winerror = 5
            raise error
        return actual_replace(source, destination)

    monkeypatch.setattr(BUILDER.os, "replace", deny_once)
    result = BUILDER.build_plugins(output, "0.1.1", ROOT)
    assert len(attempts) == 2
    assert Path(result["output"]).is_dir()
    assert not list(output.glob(".pmt-build-*"))


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-violation retry policy")
def test_pkg_01_persistent_windows_publish_denial_is_bounded_and_structured(tmp_path, monkeypatch, capsys):
    output = tmp_path / "distribution"
    attempts = []
    delays = []

    def always_deny(_source, _destination):
        attempts.append(time.monotonic())
        error = PermissionError(13, "simulated persistent sharing violation")
        error.winerror = 32
        raise error

    actual_sleep = BUILDER.time.sleep

    def measure_sleep(delay):
        delays.append(delay)
        actual_sleep(delay)

    monkeypatch.setattr(BUILDER.os, "replace", always_deny)
    monkeypatch.setattr(BUILDER.time, "sleep", measure_sleep)
    code = BUILDER.main(["--output-dir", str(output), "--version", "0.1.1"])
    report = json.loads(capsys.readouterr().out)
    assert code == 2 and report["ok"] is False
    assert report["error"]["cause_type"] == "PermissionError"
    assert report["error"]["errno"] == 13 and report["error"]["winerror"] == 32
    assert len(attempts) == 5 and sum(delays) <= 1.0
    assert not (output / "0.1.1").exists()
    assert not list(output.glob(".pmt-build-*"))
    assert str(ROOT).lower() not in json.dumps(report).lower()


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing-violation retry policy")
def test_pkg_01_source_drift_during_publish_retry_aborts_and_cleans_stage(tmp_path, monkeypatch):
    output = tmp_path / "distribution"
    source_root = tmp_path / "private-source"
    import shutil
    shutil.copytree(ROOT, source_root, ignore=shutil.ignore_patterns(
        ".git", ".venv", ".pytest_cache", ".pytest-tmp", ".pmt-test", "__pycache__"))
    actual_replace = BUILDER.os.replace
    attempts = 0

    def mutate_then_deny(_source, _destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            source_file = source_root / "src" / "pmt" / "cli.py"
            source_file.write_text(source_file.read_text(encoding="utf-8") + "\n# changed during retry wait\n", encoding="utf-8")
            error = PermissionError(13, "simulated temporary publish denial")
            error.winerror = 5
            raise error
        return actual_replace(_source, _destination)

    monkeypatch.setattr(BUILDER.os, "replace", mutate_then_deny)
    with pytest.raises(BUILDER.BuildError, match="source changed"):
        BUILDER.build_plugins(output, "0.1.1", source_root)
    assert attempts == 1
    assert not (output / "0.1.1").exists()
    assert not list(output.glob(".pmt-build-*"))
