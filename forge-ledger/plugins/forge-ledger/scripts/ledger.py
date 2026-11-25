#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


def _run() -> int:
    plugin_root = str(Path(__file__).resolve().parent.parent)
    sys.path[:] = [plugin_root, *(p for p in sys.path if p != plugin_root)]
    from ledger import cli

    return cli.main()


if __name__ == "__main__":
    sys.exit(_run())
