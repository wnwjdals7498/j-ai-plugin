#!/usr/bin/env bash
set -e

.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
python3 -m unittest discover -s tests -t . -v
claude plugin validate .
