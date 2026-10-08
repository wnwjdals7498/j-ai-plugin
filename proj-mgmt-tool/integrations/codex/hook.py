"""Portable Codex Hook entry with a stdlib-only interpreter guard."""
from pathlib import Path
import json
import os
import shutil
import sys


def _same_interpreter(candidate):
    try:
        resolved = shutil.which(candidate) or candidate
        return os.path.normcase(os.path.realpath(resolved)) == os.path.normcase(os.path.realpath(sys.executable))
    except (OSError, TypeError, ValueError):
        return False


def _dispatch():
    candidate = os.environ.get("PMT_PYTHON")
    if candidate and not _same_interpreter(candidate):
        # This file deliberately uses only the standard library until after the
        # selected interpreter has taken over. stdin/stdout remain the product
        # Hook's native JSON channel across exec.
        try:
            os.execvpe(candidate, [candidate, __file__, *sys.argv[1:]], os.environ)
        except OSError:
            if "--event" in sys.argv and sys.argv[sys.argv.index("--event") + 1:][:1] == ["SessionStart"]:
                sys.stdout.write(json.dumps({"systemMessage": "PMT could not start the configured Python. Check PMT_PYTHON."}) + "\n")
            return 0

    if sys.version_info < (3, 13):
        if "--event" in sys.argv and sys.argv[sys.argv.index("--event") + 1:][:1] == ["SessionStart"]:
            sys.stdout.write(json.dumps({"systemMessage": "PMT needs Python 3.13 or later. Run `pmt connect`, set PMT_PYTHON, then restart Codex."}) + "\n")
        return 0

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    from pmt.easy_hook import run
    return run(["--product", "codex", *sys.argv[1:]])

if __name__ == "__main__":
    raise SystemExit(_dispatch())
