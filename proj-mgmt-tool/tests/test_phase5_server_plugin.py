"""Prepared E3 server plugin asset checks; never operates a Host."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
PLUGIN = REPO / "pmt-server"
SESSION_START = PLUGIN / "hooks" / "session_start.py"
PYTHON = Path(r"C:\PMT\src\venv-dev\Scripts\python.exe")
GIT_BASH = Path(r"D:\Application\Git\bin\bash.exe")


def _minimal_config():
    return {
        "schema_version": 1,
        "revision": 1,
        "paths": {
            "data_root": "C:/PMT/work/phase5-test/E3/data",
            "log_dir": "C:/PMT/work/phase5-test/E3/logs",
            "backup_dir": "C:/PMT/work/phase5-test/E3/backups",
        },
        "listen": {"host": "127.0.0.1", "port": 18765},
        "public_url": "https://host.example.invalid",
        "tls": {},
        "claim_key": {"key_id": "test", "source": {"kind": "file", "path": "C:/PMT/work/phase5-test/E3/key"}},
        "proxy": {"enabled": False, "trusted": []},
        "access": {"allowed_sources": ["127.0.0.1"]},
        "service": {"kind": "none", "name": "PMT Host E3 Fixture", "account": "current", "app_root": "C:/PMT/work/phase5-test/E3/app"},
        "logging": {"level": "info", "retain_days": 1},
        "registry": {"projects": []},
    }


def _write_config(root: Path):
    root.mkdir(parents=True)
    (root / "host-config.json").write_text(json.dumps(_minimal_config()), encoding="utf-8")


def test_server_plugin_declares_claude_only_hook_asset():
    claude_manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
    codex_manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
    claude_hooks_path = claude_manifest["hooks"]
    assert claude_hooks_path == "./hooks/claude.json"
    assert json.loads((PLUGIN / claude_hooks_path.removeprefix("./")).read_text(encoding="utf-8"))["hooks"].keys() == {"SessionStart"}
    assert codex_manifest["hooks"] == []
    assert not (PLUGIN / "hooks" / "hooks.json").exists()


def _env(**extra):
    result = os.environ.copy()
    result.pop("PYTHONPATH", None)
    result.update(extra)
    return result


def test_session_start_exports_only_server_paths_without_host_setup(tmp_path):
    config_root = tmp_path / "host config 한글"
    _write_config(config_root)
    env_file = tmp_path / "claude env file"
    env = _env(CLAUDE_ENV_FILE=str(env_file))
    run = subprocess.run(
        [sys.executable, str(SESSION_START), "--config-root", str(config_root)],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {}
    content = env_file.read_text(encoding="utf-8")
    assert content.splitlines() == [
        f"export PMT_SERVER_PYTHON={_shell_quote(sys.executable)}",
        f"export PMT_HOST_CONFIG_ROOT={_shell_quote(str(config_root))}",
    ]
    assert set(line.split("=", 1)[0] for line in content.splitlines()) == {
        "export PMT_SERVER_PYTHON", "export PMT_HOST_CONFIG_ROOT",
    }
    assert sorted(path.name for path in tmp_path.iterdir()) == ["claude env file", "host config 한글"]
    assert sorted(path.name for path in config_root.iterdir()) == ["host-config.json"]


def test_session_start_exports_fresh_absolute_root_without_creating_it(tmp_path):
    config_root = tmp_path / "fresh host config 한글"
    env_file = tmp_path / "claude env file"
    run = subprocess.run(
        [sys.executable, str(SESSION_START), "--config-root", str(config_root)],
        cwd=tmp_path, env=_env(CLAUDE_ENV_FILE=str(env_file)), text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0, run.stderr
    assert "not initialized" in json.loads(run.stdout)["systemMessage"]
    assert env_file.read_text(encoding="utf-8").splitlines() == [
        f"export PMT_SERVER_PYTHON={_shell_quote(sys.executable)}",
        f"export PMT_HOST_CONFIG_ROOT={_shell_quote(str(config_root))}",
    ]
    assert not config_root.exists()


def _shell_quote(value):
    import shlex
    return shlex.quote(value)


@pytest.mark.parametrize("args", [
    [],
    ["--config-root", "relative"],
])
def test_session_start_invalid_input_is_native_nonblocking_json(tmp_path, args):
    root = tmp_path / "missing config"
    actual = [arg.format(root=str(root)) for arg in args]
    env_file = tmp_path / "should not be created"
    run = subprocess.run(
        [sys.executable, str(SESSION_START), *actual], cwd=tmp_path,
        env=_env(CLAUDE_ENV_FILE=str(env_file)), text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0
    payload = json.loads(run.stdout)
    assert set(payload) == {"systemMessage"}
    assert payload["systemMessage"]
    assert "Traceback" not in run.stderr
    assert not env_file.exists()
    assert not root.exists()


def test_session_start_exports_paths_even_when_existing_config_is_malformed(tmp_path):
    config_root = tmp_path / "existing config"
    config_root.mkdir()
    (config_root / "host-config.json").write_text("{bad json", encoding="utf-8")
    env_file = tmp_path / "claude env file"
    run = subprocess.run(
        [sys.executable, str(SESSION_START), "--config-root", str(config_root)],
        cwd=tmp_path, env=_env(CLAUDE_ENV_FILE=str(env_file)), text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0, run.stderr
    assert json.loads(run.stdout) == {}
    assert len(env_file.read_text(encoding="utf-8").splitlines()) == 2
    assert sorted(path.name for path in config_root.iterdir()) == ["host-config.json"]


def test_session_start_bad_env_file_is_nonblocking_native_json(tmp_path):
    config_root = tmp_path / "valid config"
    _write_config(config_root)
    env_dir = tmp_path / "missing parent"
    run = subprocess.run(
        [sys.executable, str(SESSION_START), "--config-root", str(config_root)],
        cwd=tmp_path, env=_env(CLAUDE_ENV_FILE=str(env_dir / "env.sh")),
        text=True, capture_output=True, check=False,
    )
    assert run.returncode == 0
    assert json.loads(run.stdout)["systemMessage"]
    assert not env_dir.exists()


def _assert_version_result(run):
    assert run.returncode == 0, (run.stdout, run.stderr)
    result = json.loads(run.stdout)
    assert result["python"].casefold() == str(PYTHON).casefold()
    assert result["version"]
    return result


def _write_powershell_cmd_runner(path: Path):
    path.write_text(
        'param([string]$Wrapper)\n'
        '$command = \'call "\' + $Wrapper + \'" version --json\'\n'
        '& $env:ComSpec /d /c $command\n'
        'exit $LASTEXITCODE\n',
        encoding="utf-8",
    )


@pytest.mark.skipif(os.name != "nt" or not PYTHON.is_file(), reason="Windows installed-dev-venv wrapper check")
def test_windows_cmd_wrapper_uses_venv_and_preserves_paths_and_arguments(tmp_path):
    config_root = tmp_path / "host config 한글"
    _write_config(config_root)
    ps = tmp_path / "invoke wrapper.ps1"
    _write_powershell_cmd_runner(ps)
    env = _env(PMT_SERVER_PYTHON=str(PYTHON), PMT_HOST_CONFIG_ROOT=str(config_root))
    env.pop("CLAUDE_ENV_FILE", None)
    run = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ps), str(PLUGIN / "bin" / "pmt-server.cmd")],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
    )
    _assert_version_result(run)
    assert sorted(path.name for path in config_root.iterdir()) == ["host-config.json"]


@pytest.mark.skipif(os.name != "nt" or not PYTHON.is_file() or not GIT_BASH.is_file(), reason="Windows Git Bash wrapper check")
def test_git_bash_wrapper_uses_venv_from_unrelated_cwd(tmp_path):
    config_root = tmp_path / "host config 한글"
    _write_config(config_root)
    env = _env(PMT_SERVER_PYTHON=str(PYTHON), PMT_HOST_CONFIG_ROOT=str(config_root))
    env.pop("CLAUDE_ENV_FILE", None)
    wrapper_path = PLUGIN / "bin" / "pmt-server"
    wrapper = f"/{wrapper_path.drive[0].lower()}/{str(wrapper_path)[3:].replace(chr(92), '/')}"
    run = subprocess.run(
        [str(GIT_BASH), "-c", '"$1" version --json', "_", wrapper],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
    )
    _assert_version_result(run)
    assert sorted(path.name for path in config_root.iterdir()) == ["host-config.json"]


@pytest.mark.parametrize("unset_name", ["PMT_SERVER_PYTHON", "PMT_HOST_CONFIG_ROOT"])
@pytest.mark.skipif(os.name != "nt" or not PYTHON.is_file(), reason="Windows command wrapper check")
def test_windows_cmd_missing_setting_exits_three(tmp_path, unset_name):
    env = _env(PMT_SERVER_PYTHON=str(PYTHON), PMT_HOST_CONFIG_ROOT=str(tmp_path / "config"))
    env.pop(unset_name, None)
    ps = tmp_path / "invoke missing.ps1"
    _write_powershell_cmd_runner(ps)
    run = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ps), str(PLUGIN / "bin" / "pmt-server.cmd")],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
    )
    assert run.returncode == 3
    assert "Set PMT_" in run.stderr
    assert "Traceback" not in run.stderr


@pytest.mark.skipif(os.name != "nt", reason="Windows command wrapper check")
def test_windows_cmd_invalid_python_exits_three(tmp_path):
    env = _env(PMT_SERVER_PYTHON=str(tmp_path / "missing python.exe"), PMT_HOST_CONFIG_ROOT=str(tmp_path / "config"))
    ps = tmp_path / "invoke invalid python.ps1"
    _write_powershell_cmd_runner(ps)
    run = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-File", str(ps), str(PLUGIN / "bin" / "pmt-server.cmd")],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
    )
    assert run.returncode == 3
    assert "PMT_SERVER_PYTHON" in run.stderr
    assert "Traceback" not in run.stderr


@pytest.mark.skipif(os.name != "nt" or not GIT_BASH.is_file(), reason="Windows Git Bash missing-setting check")
@pytest.mark.parametrize("unset_name", ["PMT_SERVER_PYTHON", "PMT_HOST_CONFIG_ROOT"])
def test_git_bash_missing_setting_exits_three(tmp_path, unset_name):
    env = _env(PMT_SERVER_PYTHON=str(PYTHON), PMT_HOST_CONFIG_ROOT=str(tmp_path / "config"))
    env.pop(unset_name, None)
    wrapper_path = PLUGIN / "bin" / "pmt-server"
    wrapper = f"/{wrapper_path.drive[0].lower()}/{str(wrapper_path)[3:].replace(chr(92), '/')}"
    run = subprocess.run(
        [str(GIT_BASH), "-c", '"$1" version --json', "_", wrapper],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
    )
    assert run.returncode == 3
    assert "Set PMT_SERVER_PYTHON" in run.stderr
    assert "Traceback" not in run.stderr


@pytest.mark.skipif(os.name != "nt" or not GIT_BASH.is_file(), reason="Windows Git Bash wrapper check")
def test_git_bash_invalid_python_exits_three(tmp_path):
    env = _env(PMT_SERVER_PYTHON=str(tmp_path / "missing python.exe"), PMT_HOST_CONFIG_ROOT=str(tmp_path / "config"))
    wrapper_path = PLUGIN / "bin" / "pmt-server"
    wrapper = f"/{wrapper_path.drive[0].lower()}/{str(wrapper_path)[3:].replace(chr(92), '/')}"
    run = subprocess.run(
        [str(GIT_BASH), "-c", '"$1" version --json', "_", wrapper],
        cwd=tmp_path, env=env, text=True, capture_output=True, check=False,
    )
    assert run.returncode == 3
    assert "PMT_SERVER_PYTHON" in run.stderr
    assert "Traceback" not in run.stderr
