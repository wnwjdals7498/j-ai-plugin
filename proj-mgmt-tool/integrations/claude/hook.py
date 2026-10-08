"""Portable thin entry point for the Claude Code command hook."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from pmt.easy_hook import run

if __name__ == "__main__":
    raise SystemExit(run(["--product", "claude", *sys.argv[1:]]))
