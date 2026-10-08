"""Shared Claude/Codex hook entry that prepares PMT before the bridge."""
from __future__ import annotations

import io
import json
import os
import sys

from . import easy_setup
from .client_setup.credentials import load_credential
from .client_setup.client import is_legacy_root, read_client_metadata, write_client_metadata
from .errors import PmtError
from .storage_config import _read_profile
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
    product = "claude"
    supplied_product = _argument_value(argv, "--product")
    if "--product" in argv:
        if supplied_product is None:
            _write_setup_error(stdout, _argument_value(argv, "--event"), "hook_arguments_invalid")
            return 0
        product = supplied_product
    remainder = []
    skip = False
    for index, arg in enumerate(argv):
        if skip:
            skip = False
            continue
        if arg == "--product":
            skip = True
        else:
            remainder.append(arg)
    if product not in {"claude", "codex"}:
        _write_setup_error(stdout, _argument_value(remainder, "--event"), "hook_product_unsupported")
        return 0
    event = _argument_value(remainder, "--event")
    # Core's parser requires an explicit product flag. Preserve every other native
    # hook flag, including --with-context and --read-context.
    bridge_argv = ["--product", product, *remainder]
    if event is None:
        _write_setup_error(stdout, None, "hook_arguments_invalid")
        return 0
    try:
        native = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        native = {}
    cwd = native.get("cwd") if isinstance(native, dict) and isinstance(native.get("cwd"), str) else os.getcwd()

    try:
        options, missing = easy_setup.client_mode.read_options(environ, product=product)
    except PmtError as error:
        _write_setup_error(stdout, event, error.code)
        return 0
    config_root = environ.get("PMT_CONFIG_ROOT")
    legacy = bool(missing) and config_root and is_legacy_root(config_root)
    message = None
    if legacy:
        try:
            profile, _digest = _read_profile(config_root)
            if profile and profile.get("mode") == "hosted":
                load_credential(config_root, environ)
            previous_metadata = read_client_metadata(config_root)
            legacy_mode = profile.get("mode") if profile else (previous_metadata or {}).get("last_mode", "local")
            write_client_metadata(config_root, source="legacy",
                                  python_path=environ.get("PMT_PYTHON") or sys.executable,
                                  mode=legacy_mode)
        except PmtError as error:
            _write_setup_error(stdout, event, error.code)
            return 0
    if not legacy:
        state = prepare(environ, cwd, product=product)
        if state["status"] != "ready":
            if event == "SessionStart":
                stdout.write(json.dumps({"systemMessage": state.get("message") or "PMT setup is unavailable."}, ensure_ascii=False) + "\n")
            return 0
        environ.update(state["env"])
        if event == "SessionStart":
            env_file = environ.get("CLAUDE_ENV_FILE")
            if env_file and product == "claude":
                easy_setup.write_env_file(env_file, state["env"])
            message = LINK_HINTS.get(state["link"])
            if state.get("configured_now"):
                setup_message = ("PMT: connected to the Host and saved the profile."
                                 if state.get("mode") == "hosted" else "PMT: initialized local storage and saved the profile.")
                message = (setup_message + " " + (message or "")).strip()

    saved_stdin, saved_stdout = sys.stdin, sys.stdout
    captured = io.StringIO()
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    sys.stdout = captured
    try:
        code = bridge_main(bridge_argv)
    finally:
        sys.stdin, sys.stdout = saved_stdin, saved_stdout
    merged = _merge_output(captured.getvalue(), message)
    if merged:
        stdout.write(merged + "\n")
    return code


def _write_setup_error(stdout, event, code):
    if event == "SessionStart":
        if str(code).startswith("handoff"):
            message = f"PMT handoff setup failed ({code}). Check the handoff file."
        elif str(code).startswith("credential"):
            message = f"PMT credential setup failed ({code}). Check the credential store."
        else:
            message = f"PMT setup failed ({code}). Check the PMT configuration."
        stdout.write(json.dumps({"systemMessage": message}, ensure_ascii=False) + "\n")


def _argument_value(argv, name):
    try:
        index = argv.index(name)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) and not argv[index + 1].startswith("--") else None
