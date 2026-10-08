"""Portable PMT CLI launcher for the repository plugin."""
from pathlib import Path
import sys

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PACKAGE_ROOT / "src"))
from pmt.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
