from pathlib import Path

import docdb
from pysqlite3 import dbapi2


def test_t01_1_package_version() -> None:
    assert docdb.__version__ == "0.1.0"


def test_t01_2_runtime_constraints(plugin_root: Path) -> None:
    constraints = (plugin_root / "constraints.txt").read_text(encoding="utf-8").splitlines()

    assert "pysqlite3==0.6.1" in constraints
    assert "mcp==2.3.0" in constraints


def test_t01_3_dev_constraints(plugin_root: Path) -> None:
    constraints = (plugin_root / "constraints-dev.txt").read_text(encoding="utf-8").splitlines()

    assert constraints[0] == "-c constraints.txt"
    assert any(line.startswith("pytest==") for line in constraints[1:])


def test_t01_4_sqlite_minimum_version() -> None:
    assert dbapi2.sqlite_version_info >= (3, 34, 0)


def test_t01_5_plugin_root_fixture(plugin_root: Path) -> None:
    assert plugin_root.name == "docdb"
    assert (plugin_root / "src/docdb/__init__.py").is_file()
