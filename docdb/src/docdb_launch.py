"""docdb 명령의 표준 라이브러리 부트스트랩."""
import hashlib
import importlib
import json
import os
import platform
import shutil
import site
import subprocess
import sys
import sysconfig
from pathlib import Path
from typing import List, Optional


def _run(cmd: list) -> None:
    """외부 명령을 검사하며 실행한다."""
    subprocess.run(cmd, check=True)


def _load_paths(source_dir):
    """소스 경로를 앞에 두고 docdb 경로 함수를 반환한다."""
    source = str(source_dir)
    if source in sys.path:
        sys.path.remove(source)
    sys.path.insert(0, source)
    from docdb.errors import DocdbError, format_error
    from docdb.paths import stamp_path, venv_dir, venv_python

    return DocdbError, format_error, stamp_path, venv_dir, venv_python


def _setup(remove, stamp_path, venv_dir, venv_python):
    """공용 데이터 폴더에 의존성 venv를 설치하거나 제거한다."""
    directory = venv_dir()
    if remove:
        if directory.is_symlink() or directory.is_file():
            directory.unlink()
        elif directory.exists():
            shutil.rmtree(str(directory))
        return

    directory.parent.mkdir(parents=True, exist_ok=True)
    _run([sys.executable, "-m", "venv", str(directory)])
    constraints = Path(__file__).resolve().parent.parent / "constraints.txt"
    _run(
        [
            str(venv_python()),
            "-m",
            "pip",
            "install",
            "-c",
            str(constraints),
            "pysqlite3",
            "mcp",
        ]
    )
    stamp = {
        "python": platform.python_version(),
        "docdb": __import__("docdb").__version__,
        "constraints_sha256": hashlib.sha256(constraints.read_bytes()).hexdigest(),
    }
    stamp_path().write_text(json.dumps(stamp, ensure_ascii=False) + "\n", encoding="utf-8")


def _check_venv(DocdbError, stamp_path, venv_dir):
    """설치 스탬프와 실행 Python 버전을 확인한다."""
    stamp_file = stamp_path()
    if not venv_dir().is_dir() or not stamp_file.is_file():
        raise DocdbError("NO_VENV", "docdb_launch.py setup 실행 필요")
    try:
        stamp = json.loads(stamp_file.read_text(encoding="utf-8"))
        stamp_version = stamp["python"].split(".")
        stamped = (int(stamp_version[0]), int(stamp_version[1]))
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise DocdbError("NO_VENV", "docdb_launch.py setup 실행 필요")
    if stamped != tuple(sys.version_info[:2]):
        raise DocdbError("VENV_MISMATCH", "현재 Python으로 docdb_launch.py setup 재실행 필요")


def _add_venv_paths(venv_dir):
    """venv 라이브러리 경로를 기존 패키지보다 앞에 둔다."""
    venv = str(venv_dir())
    scheme = "nt" if os.name == "nt" else "posix_prefix"
    paths = sysconfig.get_paths(
        scheme=scheme, vars={"base": venv, "platbase": venv}
    )
    before = list(sys.path)
    seen = set()
    for key in ("purelib", "platlib"):
        path = paths.get(key)
        if path and path not in seen:
            site.addsitedir(path)
            seen.add(path)
    added = [path for path in sys.path if path not in before]
    if added:
        sys.path[:] = added + [path for path in sys.path if path not in added]


def _load_runtime(DocdbError, venv_dir, source_dir):
    """venv 경로를 구성하고 런타임 의존성을 확인한다."""
    _add_venv_paths(venv_dir)
    source = str(source_dir)
    if source in sys.path:
        sys.path.remove(source)
    sys.path.insert(0, source)
    try:
        importlib.import_module("pysqlite3")
        importlib.import_module("mcp")
    except ImportError as exc:
        raise DocdbError("DEP_MISSING", "docdb_launch.py setup으로 의존성을 설치해야 함") from exc


def _dispatch(argv, DocdbError, stamp_path, venv_dir, venv_python, source_dir):
    """요청한 setup, MCP 또는 CLI 명령을 실행한다."""
    if not argv:
        raise DocdbError("BAD_ARGS", "명령이 필요함")
    command = argv[0]
    if command == "setup":
        if len(argv) == 1:
            remove = False
        elif len(argv) == 2 and argv[1] == "--remove":
            remove = True
        else:
            raise DocdbError("BAD_ARGS", "setup 인자가 올바르지 않음")
        _setup(remove, stamp_path, venv_dir, venv_python)
        return 0
    if command not in ("mcp", "cli"):
        raise DocdbError("BAD_ARGS", "지원하지 않는 docdb_launch 명령")
    if command == "mcp" and len(argv) != 1:
        raise DocdbError("BAD_ARGS", "mcp는 추가 인자를 받지 않음")

    _check_venv(DocdbError, stamp_path, venv_dir)
    _load_runtime(DocdbError, venv_dir, source_dir)
    if command == "mcp":
        if not os.environ.get("DOCDB_START_DIR") and os.environ.get("PWD"):
            os.environ["DOCDB_START_DIR"] = os.environ["PWD"]
        importlib.import_module("docdb.mcp_server").main()
        return 0
    return importlib.import_module("docdb.cli").main(argv[1:])


def main(argv: Optional[List[str]] = None) -> int:
    """부트스트랩 명령을 실행하고 계약 종료 코드를 반환한다."""
    if sys.version_info < (3, 10):
        sys.stderr.write("ERROR PY_TOO_OLD Python 3.10 이상 필요\n")
        return 2

    # Windows에서 파이프로 연결된 표준 입출력은 로캘 인코딩(cp1252 등)이라 한글 출력이 깨지거나
    # UnicodeEncodeError가 난다. 버전 검사 뒤에는 reconfigure가 항상 있다(C-24).
    for stream in (sys.stdout, sys.stderr):
        stream.reconfigure(encoding="utf-8")

    source_dir = Path(__file__).resolve().parent
    (
        DocdbError,
        format_error,
        stamp_path,
        venv_dir,
        venv_python,
    ) = _load_paths(source_dir)
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        return _dispatch(args, DocdbError, stamp_path, venv_dir, venv_python, source_dir)
    except DocdbError as exc:
        sys.stderr.write(format_error(exc) + "\n")
        return exc.exit_code


if __name__ == "__main__":
    sys.exit(main())
