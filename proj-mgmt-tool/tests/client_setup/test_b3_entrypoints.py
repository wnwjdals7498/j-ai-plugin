from __future__ import annotations

import json
import io
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import builtins

import pytest


PROJECT = Path(__file__).resolve().parents[2]


def _bundle(tmp_path):
    root = tmp_path / "PMT bundle with spaces"
    bin_dir, scripts_dir = root / "bin", root / "scripts"
    bin_dir.mkdir(parents=True)
    scripts_dir.mkdir()
    shutil.copy2(PROJECT / "bin" / "pmt", bin_dir / "pmt")
    shutil.copy2(PROJECT / "bin" / "pmt.cmd", bin_dir / "pmt.cmd")
    # A recorder stands in for easy_cli.main so these checks prove that the
    # platform launcher preserves argv exactly without invoking Core semantics.
    (scripts_dir / "pmt_easy.py").write_text(
        "import json, sys\nprint(json.dumps(sys.argv[1:], ensure_ascii=False))\n",
        encoding="utf-8",
    )
    return root


def _python_with_spaces(tmp_path):
    if os.name != "nt":
        return sys.executable
    source = Path(sys.executable).resolve()
    target_root = tmp_path / "python runtime with spaces"
    target_scripts = target_root / "Scripts"
    target_scripts.mkdir(parents=True)
    target = target_scripts / source.name
    try:
        os.link(source, target)
    except OSError as error:
        pytest.skip(f"Could not create a test-only Python path containing spaces: {error}")
    source_venv = source.parent.parent / "pyvenv.cfg"
    if source_venv.is_file():
        shutil.copy2(source_venv, target_root / "pyvenv.cfg")
    probe = subprocess.run([str(target), "-c", "import sys; print(sys.version_info[:2])"],
                           text=True, capture_output=True, timeout=5, check=False)
    if probe.returncode:
        pytest.skip("The test-only Python path with spaces did not start")
    return str(target)


def test_c11_pmt_easy_reads_client_metadata_before_importing_the_package(tmp_path):
    config = tmp_path / "config root"
    config.mkdir()
    (config / "client.json").write_text(json.dumps({
        "schema_version": 1, "source": "connect", "last_mode": "hosted",
        "python_path": "C:/Python With Spaces/python.exe",
    }), encoding="utf-8")
    script = PROJECT / "scripts" / "pmt_easy.py"
    code = (
        f"import importlib.util; s=importlib.util.spec_from_file_location('pmt_easy', {str(script)!r}); "
        "m=importlib.util.module_from_spec(s); s.loader.exec_module(m); "
        f"print(m._metadata_python({{'PMT_CONFIG_ROOT': {str(config)!r}}}))"
    )
    result = subprocess.run([sys.executable, "-c", code], text=True, capture_output=True, timeout=5)
    assert result.returncode == 0
    assert result.stdout.strip() == "C:/Python With Spaces/python.exe"


def test_c11_missing_python_and_client_metadata_exits_with_guidance_before_package_import(tmp_path):
    config = tmp_path / "empty config"
    data = tmp_path / "empty data"
    env = {key: value for key, value in os.environ.items()
           if key not in {"PMT_PYTHON", "PYTHONPATH", "PMT_CONFIG_ROOT", "PMT_DATA_ROOT"}}
    env.update({"PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)})
    result = subprocess.run([sys.executable, str(PROJECT / "scripts" / "pmt_easy.py"), "mode"],
                            text=True, capture_output=True, env=env, timeout=5)
    assert result.returncode == 3
    assert "Start PMT setup or set PMT_PYTHON" in result.stderr
    assert result.stdout == ""
    assert not config.exists() and not data.exists()


def test_c10_codex_hook_old_python_guard_is_nonblocking_without_importing_pmt(monkeypatch):
    hook = PROJECT / "integrations" / "codex" / "hook.py"
    spec = importlib.util.spec_from_file_location("pmt_codex_hook_guard", hook)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    output = io.StringIO()
    monkeypatch.delenv("PMT_PYTHON", raising=False)
    monkeypatch.setattr(sys, "version_info", (3, 12, 0))
    monkeypatch.setattr(sys, "argv", [str(hook), "--event", "SessionStart"])
    monkeypatch.setattr(sys, "stdout", output)
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == "pmt" or name.startswith("pmt."):
            raise AssertionError("The older-interpreter path must not import PMT modules")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert module._dispatch() == 0
    assert json.loads(output.getvalue()) == {
        "systemMessage": "PMT needs Python 3.13 or later. Run `pmt connect`, set PMT_PYTHON, then restart Codex."
    }


@pytest.mark.skipif(os.name != "nt", reason="Codex Windows Hook command")
def test_c10_codex_windows_command_uses_pmt_python_and_common_hook(tmp_path):
    bundle = tmp_path / "Codex plugin with spaces"
    codex = bundle / "integrations" / "codex"
    codex.mkdir(parents=True)
    shutil.copy2(PROJECT / "integrations" / "codex" / "hook.py", codex / "hook.py")
    shutil.copytree(PROJECT / "src" / "pmt", bundle / "src" / "pmt")
    interpreter = _python_with_spaces(tmp_path)
    manifest = json.loads((PROJECT / "integrations" / "codex" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    command = manifest["hooks"]["SessionStart"][0]["hooks"][0]["commandWindows"]
    command = command.replace("${PLUGIN_ROOT}", str(bundle))
    command_file = tmp_path / "Codex SessionStart command.cmd"
    command_file.write_text("@echo off\n" + command + "\n", encoding="utf-8")
    config, data = tmp_path / "hook config", tmp_path / "hook data"
    env = {key: value for key, value in os.environ.items() if key != "PYTHONPATH"}
    env.update({"PMT_PYTHON": interpreter, "PMT_CONFIG_ROOT": str(config), "PMT_DATA_ROOT": str(data)})
    fixture_path = PROJECT / "tests" / "hook-fixtures" / "codex-user-prompt.json"
    payload = fixture_path.read_text(encoding="utf-8")
    env["PMT_COMMAND_FILE"] = str(command_file)
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                             "& $env:PMT_COMMAND_FILE; exit $LASTEXITCODE"], input=payload, text=True,
                            capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert isinstance(json.loads(result.stdout), dict)


@pytest.mark.skipif(os.name != "nt", reason="Windows cmd frontdoor")
def test_c11_windows_cmd_frontdoor_preserves_quoted_arguments(tmp_path):
    root = _bundle(tmp_path)
    interpreter = _python_with_spaces(tmp_path)
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
               "& $env:PMT_LAUNCHER --mode 'two words' ; exit $LASTEXITCODE"]
    env = {**os.environ, "PMT_PYTHON": interpreter,
           "PMT_LAUNCHER": str(root / "bin" / "pmt.cmd"), "PYTHONPATH": ""}
    result = subprocess.run(command, text=True, capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == ["--mode", "two words"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Git Bash frontdoor")
def test_c11_git_bash_frontdoor_uses_windows_python_path_and_preserves_arguments(tmp_path):
    bash = Path(r"D:\Application\Git\bin\bash.exe")
    if not bash.is_file():
        pytest.skip("Git for Windows Bash is unavailable")
    root = _bundle(tmp_path)
    interpreter = _python_with_spaces(tmp_path)
    launcher = str(root / "bin" / "pmt").replace("\\", "/")
    env = dict(os.environ)
    env.pop("PMT_CONFIG_ROOT", None)
    env.pop("PMT_DATA_ROOT", None)
    env.pop("PYTHONPATH", None)
    env["PMT_PYTHON"] = interpreter
    result = subprocess.run(
        [str(bash), "-c", 'bash "$1" "$2" "$3"', "b3", launcher, "--mode", "two words"],
        text=True, capture_output=True, env=env, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == ["--mode", "two words"]


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher entrypoint")
def test_c11_cmd_falls_back_to_client_metadata_when_pmt_python_is_absent(tmp_path):
    root = _bundle(tmp_path)
    interpreter = _python_with_spaces(tmp_path)
    config = tmp_path / "config"
    config.mkdir()
    (config / "client.json").write_text(json.dumps({
        "schema_version": 1, "source": "plugin", "last_mode": "local",
        "python_path": interpreter,
    }), encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if key != "PMT_PYTHON" and key != "PYTHONPATH"}
    env["PMT_CONFIG_ROOT"] = str(config)
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
         "& $env:PMT_LAUNCHER --mode 'metadata fallback'; exit $LASTEXITCODE"],
        text=True, capture_output=True, env={**env, "PMT_LAUNCHER": str(root / "bin" / "pmt.cmd")}, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == ["--mode", "metadata fallback"]


@pytest.mark.skipif(os.name != "nt", reason="Windows Git Bash metadata interpreter fallback")
def test_c11_git_bash_metadata_fallback_preserves_spaced_paths_and_quoted_args(tmp_path):
    bash = Path(r"D:\Application\Git\bin\bash.exe")
    if not bash.is_file():
        pytest.skip("Git for Windows Bash is unavailable")
    root = tmp_path / "PMT bundle with spaces"
    (root / "bin").mkdir(parents=True)
    (root / "scripts").mkdir()
    package = root / "src" / "pmt"
    package.mkdir(parents=True)
    shutil.copy2(PROJECT / "bin" / "pmt", root / "bin" / "pmt")
    shutil.copy2(PROJECT / "scripts" / "pmt_easy.py", root / "scripts" / "pmt_easy.py")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "easy_cli.py").write_text(
        "import json, sys\ndef main():\n    print(json.dumps(sys.argv[1:]))\n    return 0\n",
        encoding="utf-8",
    )
    interpreter = _python_with_spaces(tmp_path)
    config = tmp_path / "config root with spaces"
    config.mkdir()
    (config / "client.json").write_text(json.dumps({
        "schema_version": 1, "source": "connect", "last_mode": "local",
        "python_path": interpreter,
    }), encoding="utf-8")
    env = {key: value for key, value in os.environ.items()
           if key not in {"PMT_PYTHON", "PYTHONPATH", "PMT_DATA_ROOT"}}
    env["PMT_CONFIG_ROOT"] = str(config)
    launcher = str(root / "bin" / "pmt").replace("\\", "/")
    result = subprocess.run(
        [str(bash), "-c", 'bash "$1" "$2" "$3" "$4"', "b3", launcher,
         "--test", "a title with spaces", 'quote "inside"'],
        text=True, capture_output=True, env=env, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip()) == ["--test", "a title with spaces", 'quote "inside"']
