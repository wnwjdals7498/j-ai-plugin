"""T24 표준 라이브러리 부트스트랩 통합 테스트."""
import hashlib
import json
import os
import platform
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest


def _plugin_root() -> Path:
    """플러그인 루트 경로를 반환한다."""
    return Path(__file__).resolve().parents[2]


def _base_python() -> str:
    """현재 테스트 가상 환경의 Python 실행 파일을 반환한다."""
    return sys.executable


def _environment(home: Path) -> dict[str, str]:
    """부트스트랩 테스트용 환경을 만든다."""
    env = os.environ.copy()
    for key in ("DOCDB_HOME", "DOCDB_PATH", "DOCDB_START_DIR", "PWD"):
        env.pop(key, None)
    env["DOCDB_HOME"] = str(home)
    return env


def _stamp(home: Path, python_version: str = None) -> Path:
    """임시 venv를 만들고 계약 스탬프를 쓴다."""
    plugin_root = _plugin_root()
    target = home / "venv-target"
    target.mkdir(parents=True)
    scheme = "nt" if os.name == "nt" else "posix_prefix"
    paths = sysconfig.get_paths(
        scheme=scheme, vars={"base": str(target), "platbase": str(target)}
    )
    target_site = Path(paths["purelib"])
    target_site.mkdir(parents=True, exist_ok=True)
    dev_site = Path(sysconfig.get_paths()["purelib"])
    if dev_site is not None and os.name != "nt":
        target_site.rmdir()
        target_site.symlink_to(dev_site, target_is_directory=True)
    venv = home / "venv"
    venv.symlink_to(target, target_is_directory=True)
    constraints = plugin_root / "constraints.txt"
    stamp = {
        "python": python_version or platform.python_version(),
        "docdb": "0.1.0",
        "constraints_sha256": hashlib.sha256(constraints.read_bytes()).hexdigest(),
    }
    (target / "docdb-stamp.json").write_text(
        json.dumps(stamp), encoding="utf-8"
    )
    return target


def _run_script(args, env, python=None, python_args=None):
    """부트스트랩 파일을 별도 프로세스로 실행한다."""
    command = [python or _base_python()] + list(python_args or [])
    command.extend([str(_plugin_root() / "src" / "docdb_launch.py")] + args)
    return subprocess.run(
        command,
        cwd=str(_plugin_root()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )


def test_t24_1_python36_reports_too_old():
    """행 1: Python 3.6 실행 시 종료 코드 2와 PY_TOO_OLD를 출력한다."""
    old_python = Path("/usr/bin/python3.6")
    if not old_python.is_file():
        pytest.skip("python3.6 없음")
    result = _run_script(
        ["cli", "actor", "list"], os.environ.copy(), python=str(old_python)
    )
    assert result.returncode == 2
    assert "ERROR PY_TOO_OLD" in result.stderr


def test_t24_2_cli_without_venv_reports_no_venv(tmp_path):
    """행 2: 데이터 폴더에 venv가 없으면 NO_VENV를 반환한다."""
    home = tmp_path / "home"
    home.mkdir()
    result = _run_script(["cli", "actor", "list"], _environment(home))
    assert result.returncode == 2
    assert "ERROR NO_VENV" in result.stderr


def test_t24_3_python_version_mismatch_reports_error(tmp_path):
    """행 3: 스탬프의 Python major.minor가 다르면 VENV_MISMATCH다."""
    home = tmp_path / "home"
    home.mkdir()
    _stamp(home, "2.7.0")
    result = _run_script(["cli", "actor", "list"], _environment(home))
    assert result.returncode == 2
    assert "ERROR VENV_MISMATCH" in result.stderr


def test_t24_4_setup_runs_commands_and_writes_hash(tmp_path, monkeypatch):
    """행 4: setup 설치 순서와 제약 파일 SHA256을 확인한다."""
    import docdb_launch
    from docdb.paths import stamp_path, venv_dir, venv_python

    home = tmp_path / "home"
    monkeypatch.setenv("DOCDB_HOME", str(home))
    commands = []

    def fake_run(cmd):
        commands.append(cmd)
        if len(commands) == 1:
            Path(cmd[-1]).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(docdb_launch, "_run", fake_run)
    assert docdb_launch.main(["setup"]) == 0
    constraints = _plugin_root() / "constraints.txt"
    assert commands == [
        [sys.executable, "-m", "venv", str(venv_dir())],
        [
            str(venv_python()),
            "-m",
            "pip",
            "install",
            "-c",
            str(constraints),
            "pysqlite3",
            "mcp",
        ],
    ]
    stamp = json.loads(stamp_path().read_text(encoding="utf-8"))
    assert stamp["constraints_sha256"] == hashlib.sha256(
        constraints.read_bytes()
    ).hexdigest()


def test_t24_5_setup_remove_keeps_backups(tmp_path, monkeypatch):
    """행 5: setup --remove는 venv만 지우고 백업은 보존한다."""
    import docdb_launch
    from docdb.paths import backups_dir, venv_dir

    monkeypatch.setenv("DOCDB_HOME", str(tmp_path / "home"))
    venv_dir().mkdir(parents=True)
    backup = backups_dir("project") / "backup.sqlite"
    backup.parent.mkdir(parents=True)
    backup.write_text("backup", encoding="utf-8")
    assert docdb_launch.main(["setup", "--remove"]) == 0
    assert not venv_dir().exists()
    assert backup.read_text(encoding="utf-8") == "backup"


def test_t24_6_cli_uses_venv_and_lists_actors(tmp_path, project_db):
    """행 6: 유효한 venv와 스탬프가 있으면 CLI가 DB를 연다."""
    home = tmp_path / "home"
    home.mkdir()
    _stamp(home)
    result = _run_script(
        ["cli", "--db", str(project_db), "actor", "list"], _environment(home)
    )
    assert result.returncode == 0, result.stderr
    assert "ai-claude" in result.stdout
    assert "u-tester" in result.stdout


def test_t24_7_empty_venv_reports_missing_dependency(tmp_path):
    """행 7: 의존성 없는 venv는 DEP_MISSING을 반환한다."""
    home = tmp_path / "home"
    home.mkdir()
    venv = home / "venv"
    venv.mkdir()
    stamp = {
        "python": platform.python_version(),
        "docdb": "0.1.0",
        "constraints_sha256": hashlib.sha256(
            (_plugin_root() / "constraints.txt").read_bytes()
        ).hexdigest(),
    }
    (venv / "docdb-stamp.json").write_text(json.dumps(stamp), encoding="utf-8")
    result = _run_script(
        ["cli", "actor", "list"], _environment(home), python_args=["-S"]
    )
    assert result.returncode == 2
    assert "ERROR DEP_MISSING" in result.stderr


def test_t24_8_mcp_sets_start_dir_from_pwd(tmp_path):
    """행 8: DOCDB_START_DIR가 비어 있으면 MCP 시작 시 PWD를 복사한다."""
    home = tmp_path / "home"
    home.mkdir()
    _stamp(home)
    project = tmp_path / "proj"
    project.mkdir()
    env = _environment(home)
    env["PWD"] = str(project)
    code = (
        "import os, runpy, sys, types\n"
        "fake = types.ModuleType('docdb.mcp_server')\n"
        "fake.main = lambda: print(os.environ.get('DOCDB_START_DIR', ''))\n"
        "sys.modules['docdb.mcp_server'] = fake\n"
        "script = sys.argv[1]\n"
        "sys.argv = [script, 'mcp']\n"
        "runpy.run_path(script, run_name='__main__')\n"
    )
    result = subprocess.run(
        [_base_python(), "-c", code, str(_plugin_root() / "src" / "docdb_launch.py")],
        cwd=str(_plugin_root()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(project)


def test_t24_9_mcp_preserves_existing_start_dir(tmp_path):
    """행 9: 설정된 DOCDB_START_DIR는 PWD로 덮어쓰지 않는다."""
    home = tmp_path / "home"
    home.mkdir()
    _stamp(home)
    other = tmp_path / "other"
    other.mkdir()
    env = _environment(home)
    env["PWD"] = str(tmp_path / "proj")
    env["DOCDB_START_DIR"] = str(other)
    code = (
        "import os, runpy, sys, types\n"
        "fake = types.ModuleType('docdb.mcp_server')\n"
        "fake.main = lambda: print(os.environ.get('DOCDB_START_DIR', ''))\n"
        "sys.modules['docdb.mcp_server'] = fake\n"
        "script = sys.argv[1]\n"
        "sys.argv = [script, 'mcp']\n"
        "runpy.run_path(script, run_name='__main__')\n"
    )
    result = subprocess.run(
        [_base_python(), "-c", code, str(_plugin_root() / "src" / "docdb_launch.py")],
        cwd=str(_plugin_root()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(other)


def test_t24_10_venv_paths_precede_user_site(tmp_path, project_db):
    """행 10: venv 의존성이 PYTHONUSERBASE보다 먼저 import된다."""
    home = tmp_path / "home"
    home.mkdir()
    _stamp(home)
    user_base = tmp_path / "ub"
    env = _environment(home)
    env["PYTHONUSERBASE"] = str(user_base)
    user_site = Path(
        subprocess.check_output(
            [_base_python(), "-c", "import site; print(site.getusersitepackages())"],
            env=env,
            universal_newlines=True,
        ).strip()
    )
    user_site.mkdir(parents=True, exist_ok=True)
    (user_site / "typing_extensions.py").write_text(
        "raise RuntimeError('user site shadowed venv')\n", encoding="utf-8"
    )
    code = (
        "import sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "import docdb_launch\n"
        "status = docdb_launch.main(['cli', '--db', sys.argv[2], 'actor', 'list'])\n"
        "import typing_extensions\n"
        "print('T24_TYPING=' + typing_extensions.__file__)\n"
        "sys.exit(status)\n"
    )
    result = subprocess.run(
        [
            _base_python(),
            "-c",
            code,
            str(_plugin_root() / "src"),
            str(project_db),
        ],
        cwd=str(_plugin_root()),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "ai-claude" in result.stdout
    typing_path = Path(next(
        line.split("=", 1)[1]
        for line in result.stdout.splitlines()
        if line.startswith("T24_TYPING=")
    )).resolve()
    assert Path(sysconfig.get_paths()["purelib"]).resolve() in typing_path.parents
