"""T26 플러그인 패키징 통합 테스트."""
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import sysconfig
import venv as venv_module
from pathlib import Path

import pytest


def _plugin_root() -> Path:
    """플러그인 루트 경로를 반환한다."""
    return Path(__file__).resolve().parents[2]


def _repository_root() -> Path:
    """저장소 루트 경로를 반환한다."""
    return _plugin_root().parent


def _json(path: Path) -> dict:
    """UTF-8 JSON 파일을 읽는다."""
    return json.loads(path.read_text(encoding="utf-8"))


def _environment(home: Path) -> dict[str, str]:
    """래퍼 테스트에 필요한 데이터 폴더 환경을 만든다."""
    env = os.environ.copy()
    for key in ("DOCDB_HOME", "DOCDB_PATH", "DOCDB_START_DIR", "XDG_DATA_HOME"):
        env.pop(key, None)
    env["DOCDB_HOME"] = str(home)
    return env


def _install_test_venv(home: Path) -> None:
    """T24 방식으로 현재 개발 환경을 임시 데이터 폴더에 연결한다."""
    venv = home / "venv"
    venv_module.EnvBuilder(with_pip=False, symlinks=True).create(venv)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site_packages = venv / "lib" / version / "site-packages"
    shutil.rmtree(site_packages)
    site_packages.symlink_to(
        Path(sysconfig.get_paths()["purelib"]), target_is_directory=True
    )
    constraints = _plugin_root() / "constraints.txt"
    stamp = {
        "python": platform.python_version(),
        "docdb": "0.1.0",
        "constraints_sha256": hashlib.sha256(constraints.read_bytes()).hexdigest(),
    }
    (venv / "docdb-stamp.json").write_text(
        json.dumps(stamp), encoding="utf-8"
    )


def test_t26_1_manifests_are_valid_json_with_matching_versions_and_actors():
    """행 1: 매니페스트 JSON과 버전, Claude·Codex 행위자를 확인한다."""
    from docdb import __version__

    root = _plugin_root()
    claude = _json(root / ".claude-plugin" / "plugin.json")
    codex = _json(root / ".codex-plugin" / "plugin.json")
    codex_mcp = _json(root / "codex.mcp.json")

    assert claude["version"] == __version__
    assert codex["version"] == __version__
    assert claude["mcpServers"]["docdb"]["env"]["DOCDB_ACTOR"] == "ai-claude"
    assert codex_mcp["mcpServers"]["docdb"]["env"]["DOCDB_ACTOR"] == "ai-codex"


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI 없음(C-27)")
def test_t26_2_claude_plugin_validator_passes_without_warnings():
    """행 2: 저장소 루트에서 Claude 플러그인 검증이 경고 없이 통과한다."""
    result = subprocess.run(
        ["claude", "plugin", "validate", "docdb"],
        cwd=_repository_root(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Validation passed" in output
    assert re.search(r"\bwarning\b", output, flags=re.IGNORECASE) is None


@pytest.mark.skipif(sys.platform == "win32", reason="bin/docctl은 POSIX sh(C-26), Windows는 docdb_launch.py cli")
def test_t26_3_docctl_runs_actor_list_with_configured_data_venv(tmp_path, project_db):
    """행 3: data venv를 설정하면 docctl actor list가 성공한다."""
    root = _plugin_root()
    home = tmp_path / "home"
    home.mkdir()
    _install_test_venv(home)
    result = subprocess.run(
        [str(root / "bin" / "docctl"), "--db", str(project_db), "actor", "list"],
        cwd=root,
        env=_environment(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "u-tester" in result.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="bin/docctl은 POSIX sh(C-26), Windows는 docdb_launch.py cli")
def test_t26_4_docctl_without_data_venv_reports_no_venv(tmp_path):
    """행 4: venv가 없으면 docctl이 종료 코드 2와 NO_VENV를 출력한다."""
    home = tmp_path / "empty-home"
    home.mkdir()
    result = subprocess.run(
        [str(_plugin_root() / "bin" / "docctl"), "actor", "list"],
        cwd=_plugin_root(),
        env=_environment(home),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 2
    assert "ERROR NO_VENV" in result.stderr


def test_t26_5_skill_has_required_sections_in_order_and_early_usage():
    """행 5: 필수 스킬 절 순서와 앞부분의 사용 시점을 확인한다."""
    skill = (_plugin_root() / "skills" / "docdb" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    headings = [
        "## 언제 쓰나",
        "## 설치",
        "## 사용 규칙",
        "## 관계 제안 규칙",
        "## 작업·인계",
        "## import 절차",
        "## 오류 코드 대응표",
    ]
    positions = [skill.index(heading) for heading in headings]
    assert positions == sorted(positions)
    assert "언제 쓰나" in skill[:2000]


def test_t26_6_both_marketplaces_list_docdb_at_plugin_version():
    """행 6: 두 마켓플레이스의 docdb 항목 버전이 패키지와 일치한다."""
    from docdb import __version__

    root = _repository_root()
    claude = _json(root / ".claude-plugin" / "marketplace.json")
    codex = _json(root / ".agents" / "plugins" / "marketplace.json")
    claude_item = next(item for item in claude["plugins"] if item["name"] == "docdb")
    codex_item = next(item for item in codex["plugins"] if item["name"] == "docdb")
    assert claude_item["version"] == __version__
    assert codex_item["version"] == __version__


def test_t26_7_codex_mcp_matches_measured_plugin_configuration():
    """행 7: Codex MCP 설정이 C-26의 실측 설정과 일치한다."""
    config = _json(_plugin_root() / "codex.mcp.json")
    assert config["mcpServers"]["docdb"] == {
        "command": "python3",
        "args": ["./src/docdb_launch.py", "mcp"],
        "cwd": ".",
        "env_vars": ["PWD", "DOCDB_HOME", "XDG_DATA_HOME"],
        "env": {"DOCDB_ACTOR": "ai-codex"},
    }
