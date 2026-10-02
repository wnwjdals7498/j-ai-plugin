"""Package-relative Python bridge entry point for OpenCode's Node plugin."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from pmt.hooks import main

if __name__ == "__main__":
    raise SystemExit(main())
