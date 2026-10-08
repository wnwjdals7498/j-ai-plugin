"""PMT server entry point; administrative commands extend this boundary."""
from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import sys

from .. import __version__
from ..db import SCHEMA_VERSION
from ..handoff import PLUGIN_VERSION
from ..host.auth import HOST_SCHEMA_VERSION
from ..util import canonical_json


def version_info():
    dependencies = {}
    for module in ("fastapi", "pydantic", "uvicorn"):
        try:
            available = importlib.util.find_spec(module) is not None
            dependencies[module] = importlib.metadata.version(module) if available else None
        except (ImportError, importlib.metadata.PackageNotFoundError):
            dependencies[module] = None
    return {"version": PLUGIN_VERSION, "core_version": __version__,
            "db_schema": SCHEMA_VERSION, "graph_schema": 1, "protocol": 1,
            "host_schema": HOST_SCHEMA_VERSION, "python": sys.executable,
            "python_version": sys.version.split()[0], "dependencies": dependencies}


def main(argv=None):
    parser = argparse.ArgumentParser(prog="pmt-server", description="PMT Host administration")
    parser.add_argument("--config-root")
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("version")
    version.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    result = version_info()
    missing = [name for name, installed in result["dependencies"].items() if installed is None]
    if missing:
        value = {"ok": False, "result": result,
                 "error": {"code": "host_dependency_missing", "message": "Install proj-mgmt-tool[host]", "retryable": False}}
        print(canonical_json(value) if args.json else "Install proj-mgmt-tool[host]")
        return 5
    if args.json:
        print(canonical_json(result))
    else:
        print(f"PMT Server {result['version']} | Core {result['core_version']}")
        print(f"SQLite {result['db_schema']} | graph {result['graph_schema']} | protocol {result['protocol']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
