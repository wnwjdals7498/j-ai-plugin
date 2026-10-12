"""Portable launcher for the short, user-facing PMT command."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


MINIMUM_PYTHON = (3, 13)
_CLIENT_SOURCES = {"plugin", "connect", "legacy"}


def _config_root(environ):
    explicit = environ.get("PMT_CONFIG_ROOT")
    if explicit:
        return Path(explicit)
    home = Path(environ.get("HOME") or Path.home())
    if os.name == "nt":
        legacy = home / ".config" / "pmt"
        if (legacy / "storage.json").is_file():
            return legacy
        roaming = Path(environ.get("APPDATA") or home / "AppData" / "Roaming")
        return roaming / "pmt"
    config = Path(environ.get("XDG_CONFIG_HOME") or home / ".config")
    return config / "pmt"


def _metadata_python(environ):
    path = _config_root(environ) / "client.json"
    try:
        wire = path.read_bytes()
        if len(wire) > 4096:
            return None
        value = json.loads(wire.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("source") not in _CLIENT_SOURCES
            or value.get("last_mode") not in {"local", "hosted"}
            or not isinstance(value.get("python_path"), str)
            or not value["python_path"].strip()):
        return None
    return value["python_path"]


def _same_interpreter(candidate):
    try:
        resolved = shutil.which(candidate) or candidate
        return os.path.normcase(os.path.realpath(resolved)) == os.path.normcase(os.path.realpath(sys.executable))
    except (OSError, TypeError, ValueError):
        return False


def _start_selected_python(environ):
    candidate = environ.get("PMT_PYTHON") or _metadata_python(environ)
    if not candidate:
        return "PMT is not ready in this session. Start PMT setup or set PMT_PYTHON, then retry."
    if _same_interpreter(candidate):
        return None
    try:
        completed = subprocess.run(
            [candidate, "-B", str(Path(__file__).resolve()), *sys.argv[1:]],
            env=environ, check=False,
        )
    except OSError:
        return "PMT could not start the saved Python. Check PMT_PYTHON or run setup again."
    return completed.returncode


def main():
    environ = os.environ
    selected = _start_selected_python(environ)
    if isinstance(selected, str):
        print(selected, file=sys.stderr)
        return 3
    if selected is not None:
        return selected
    if sys.version_info < MINIMUM_PYTHON:
        print("PMT needs Python 3.13 or later. Finish setup with a supported Python, then retry.", file=sys.stderr)
        return 3

    package_root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(package_root / "src"))
    from pmt.easy_cli import main as easy_main
    return easy_main()


if __name__ == "__main__":
    raise SystemExit(main())
