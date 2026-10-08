import hashlib
import json
import os
import subprocess
import shlex
import shutil
from pathlib import Path

import pytest

from pmt import easy_setup
from pmt.storage_config import _read_profile

DEVICE = "acd66a06-5995-40f0-a637-e96ffaf52b04"
NAMESPACE = "73032707-440f-4123-9189-1ff7f046f043"
REPOSITORY = "bdd8eaf4-cb0a-4971-90d7-8d7965f41fc7"
PROJECT = "4c3d4d19-28fa-4727-9895-e0fee0f43d6a"


def options_env(tmp_path, **extra):
    env = {"HOME": str(tmp_path / "home"),
           "PMT_CONFIG_ROOT": str(tmp_path / "config"), "PMT_DATA_ROOT": str(tmp_path / "data"),
           "CLAUDE_PLUGIN_OPTION_HOST_URL": "https://203.0.113.10:8765",
           "CLAUDE_PLUGIN_OPTION_DEVICE_ID": DEVICE,
           "CLAUDE_PLUGIN_OPTION_NAMESPACE_ID": NAMESPACE,
           "CLAUDE_PLUGIN_OPTION_ACTOR": "dev-test",
           "CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL": "secret-value"}
    env.update(extra)
    return env


class FakeConfigure:
    """Stand-in for configure_storage that writes the same stored shape."""

    def __init__(self):
        self.calls = []

    def __call__(self, config_root, request, *, seed=False):
        if not seed:
            assert os.environ.get("PMT_HOST_CREDENTIAL") == "secret-value"
            self.calls.append(request)
        _profile, current = _read_profile(config_root)
        assert request["expected_config_sha256"] == current
        body = {"schema_version": 1, "revision": len(self.calls) + (1 if seed else 0), "mode": "hosted",
                "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836",
                "workspace_mappings": request["workspace_mappings"],
                "endpoint": request["endpoint"], "credential_env": request["credential_env"],
                "device_id": request["device_id"], "namespace_id": request["namespace_id"],
                "actor": request["expected_actor"]}
        if "ca_file" in request:
            body["ca_file"] = request["ca_file"]
        path = os.path.join(config_root, "storage.json")
        with open(path, "w", encoding="utf-8") as stream:
            json.dump(body, stream)
        return {"mode": "hosted"}


def git_repo(path, branch="main"):
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    return path


def mapping(root, branch="main"):
    return {"repository_id": REPOSITORY, "project_id": PROJECT, "branch": branch,
            "branch_key_sha256": hashlib.sha256(branch.encode()).hexdigest(),
            "local_root": str(root), "relative_graph_path": "docs/pmt-docs/graph.json"}


def test_missing_options_change_nothing(tmp_path):
    env = options_env(tmp_path)
    del env["CLAUDE_PLUGIN_OPTION_DEVICE_CREDENTIAL"]
    configure = FakeConfigure()
    result = easy_setup.prepare(env, tmp_path, configure=configure)
    assert result["status"] == "error" and result["error_code"] == "handoff_invalid"
    assert result["missing"] == ["device_credential"]
    assert configure.calls == []
    assert not (tmp_path / "home").exists()
    assert not (tmp_path / "config").exists() and not (tmp_path / "data").exists()


def test_first_run_publishes_profile_and_keeps_credential_out_of_process_env(tmp_path, monkeypatch):
    monkeypatch.delenv("PMT_HOST_CREDENTIAL", raising=False)
    configure = FakeConfigure()
    result = easy_setup.prepare(options_env(tmp_path), tmp_path, configure=configure, python="/opt/py")
    assert result["status"] == "ready" and result["configured_now"] is True
    assert len(configure.calls) == 1
    assert configure.calls[0]["expected_config_sha256"] is None
    assert configure.calls[0]["credential_env"] == "PMT_HOST_CREDENTIAL"
    assert "secret-value" not in json.dumps(configure.calls[0])
    assert "PMT_HOST_CREDENTIAL" not in os.environ
    config_root = tmp_path / "config"
    assert result["env"]["PMT_CONFIG_ROOT"] == str(config_root)
    assert result["env"]["PMT_DATA_ROOT"] == str(tmp_path / "data")
    assert result["env"]["PMT_PYTHON"] == "/opt/py"
    assert "PMT_SCOPE_ID" not in result["env"]
    assert result["link"] == "not_git"


def test_matching_profile_is_not_republished(tmp_path):
    configure = FakeConfigure()
    env = options_env(tmp_path)
    easy_setup.prepare(env, tmp_path, configure=configure)
    result = easy_setup.prepare(env, tmp_path, configure=configure)
    assert result["configured_now"] is False
    assert len(configure.calls) == 1


def test_changed_option_republishes_with_current_hash_and_keeps_mappings(tmp_path):
    configure = FakeConfigure()
    repo = git_repo(tmp_path / "repo")
    env = options_env(tmp_path)
    easy_setup.prepare(env, tmp_path, configure=configure)
    config_root = tmp_path / "config"
    profile, current = _read_profile(config_root)
    configure(str(config_root), {**configure.calls[0], "expected_config_sha256": current,
                                 "workspace_mappings": [mapping(repo)]}, seed=True)
    _profile, before = _read_profile(config_root)
    env["CLAUDE_PLUGIN_OPTION_HOST_URL"] = "https://203.0.113.11:8765"
    result = easy_setup.prepare(env, repo, configure=configure)
    assert result["configured_now"] is True
    last = configure.calls[-1]
    assert last["expected_config_sha256"] == before
    assert last["endpoint"] == "https://203.0.113.11:8765"
    assert [item["local_root"] for item in last["workspace_mappings"]] == [str(repo)]


def test_linked_checkout_exports_scope_and_unlinked_branch_does_not(tmp_path):
    configure = FakeConfigure()
    repo = git_repo(tmp_path / "repo")
    env = options_env(tmp_path)
    easy_setup.prepare(env, tmp_path, configure=configure)
    config_root = tmp_path / "config"
    _profile, current = _read_profile(config_root)
    configure(str(config_root), {**configure.calls[0], "expected_config_sha256": current,
                                 "workspace_mappings": [mapping(repo)]}, seed=True)
    linked = easy_setup.prepare(env, repo, configure=configure)
    assert linked["link"] == "linked" and linked["env"]["PMT_SCOPE_ID"] == PROJECT
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feature"], check=True)
    other = easy_setup.prepare(env, repo, configure=configure)
    assert other["link"] == "branch_not_linked" and "PMT_SCOPE_ID" not in other["env"]


def test_local_profile_is_never_replaced(tmp_path):
    config_root = tmp_path / "config"
    config_root.mkdir(parents=True)
    (config_root / "storage.json").write_text(json.dumps(
        {"schema_version": 1, "revision": 1, "mode": "local",
         "environment_id": "c5eb84ae-aeab-4eb1-90b9-a339bb4c0836", "workspace_mappings": []}), encoding="utf-8")
    configure = FakeConfigure()
    before = (config_root / "storage.json").read_bytes()
    result = easy_setup.prepare(options_env(tmp_path), tmp_path, configure=configure)
    assert result["status"] == "ready" and result["mode"] == "local"
    assert "storage switch" in result["message"]
    assert (config_root / "storage.json").read_bytes() == before
    assert configure.calls == []


def test_env_file_lines_are_shell_quoted(tmp_path):
    path = tmp_path / "env file"
    easy_setup.write_env_file(path, {"PMT_CONFIG_ROOT": "/a b/c", "PMT_HOST_CREDENTIAL": "x'y"})
    script = f". {shlex.quote(path.as_posix())}; printf '%s|%s' \"$PMT_CONFIG_ROOT\" \"$PMT_HOST_CREDENTIAL\""
    env = {key: value for key, value in os.environ.items() if key != "PMT_HOST_CREDENTIAL"}
    bash = "bash"
    if os.name == "nt":
        git = shutil.which("git")
        candidate = Path(git).resolve().parents[1] / "bin" / "bash.exe" if git else None
        if candidate is None or not candidate.is_file():
            pytest.skip("Git Bash is not installed on this Windows host")
        bash = str(candidate)
    out = subprocess.run([bash, "-c", script], env=env,
                         stdout=subprocess.PIPE, check=True).stdout.decode()
    assert out == "/a b/c|"
    assert "PMT_HOST_CREDENTIAL" not in path.read_text(encoding="utf-8")
    if os.name != "nt":
        assert oct(path.stat().st_mode & 0o777) == "0o600"
