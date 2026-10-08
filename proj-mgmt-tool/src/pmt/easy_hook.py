"""Claude Code hook entry that applies /plugin options before the PMT bridge."""
from __future__ import annotations

import io
import json
import os
import sys

from . import easy_setup
from .hooks import main as bridge_main

LINK_HINTS = {
    "not_linked": "PMT: this checkout is not linked to a PMT project. Run `pmt link` to connect it.",
    "branch_not_linked": "PMT: this branch is not linked yet. Run `pmt link` to add it.",
    "detached": "PMT: detached HEAD; check out a branch to use PMT project context.",
}


def _merge_output(captured, extra_message):
    """Combine the bridge's single JSON output with an extra system message."""
    text = captured.strip()
    output = {}
    if text:
        try:
            output = json.loads(text)
        except ValueError:
            output = {}
    if extra_message:
        output["systemMessage"] = (output.get("systemMessage", "") + " " + extra_message).strip()
    return json.dumps(output, ensure_ascii=False) if output else ""


def run(argv, *, stdin=None, stdout=None, environ=None, prepare=easy_setup.prepare):
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    environ = os.environ if environ is None else environ
    raw = stdin.buffer.read(64 * 1024 + 1)
    event = argv[argv.index("--event") + 1] if "--event" in argv else None
    try:
        native = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        native = {}
    cwd = native.get("cwd") if isinstance(native, dict) and isinstance(native.get("cwd"), str) else os.getcwd()

    options, missing = easy_setup.read_options(environ)
    legacy = bool(missing) and environ.get("PMT_CONFIG_ROOT")
    message = None
    if not legacy:
        state = prepare(environ, cwd)
        if state["status"] != "ready":
            if event == "SessionStart":
                stdout.write(json.dumps({"systemMessage": state["message"]}, ensure_ascii=False) + "\n")
            return 0
        environ.update(state["env"])
        if event == "SessionStart":
            env_file = environ.get("CLAUDE_ENV_FILE")
            if env_file:
                easy_setup.write_env_file(env_file, state["env"])
            message = LINK_HINTS.get(state["link"])
            if state.get("configured_now"):
                message = ("PMT: connected to the Host and saved the profile. " + (message or "")).strip()

    saved_stdin, saved_stdout = sys.stdin, sys.stdout
    captured = io.StringIO()
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    sys.stdout = captured
    try:
        code = bridge_main(argv)
    finally:
        sys.stdin, sys.stdout = saved_stdin, saved_stdout
    merged = _merge_output(captured.getvalue(), message)
    if merged:
        stdout.write(merged + "\n")
    return code
