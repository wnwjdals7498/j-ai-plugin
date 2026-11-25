#!/usr/bin/env bash
set -euo pipefail

if [ ! -d ".venv" ]; then
    python3 -m venv .venv
fi

.venv/bin/pip install --require-hashes -r ci/requirements-py39.txt
.venv/bin/ruff --version
.venv/bin/mypy --version
