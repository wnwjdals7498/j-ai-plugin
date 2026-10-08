"""Shared Claude/Codex hook entry that prepares PMT before the bridge."""
from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path

from . import easy_setup
from .client_setup.credentials import load_credential
from .client_setup.client import (is_legacy_root, read_client_metadata,
                                  has_client_metadata, write_client_metadata)
from .errors import PmtError
from .storage_config import _read_profile
from .hooks import EVENTS, HookInputError, main as bridge_main, normalize_event

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
            _write_setup_error(stdout, _argument_value(argv, "--event"), "hook_arguments_invalid", product)
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
        _write_setup_error(stdout, _argument_value(remainder, "--event"), "hook_product_unsupported", product)
        return 0
    event = _argument_value(remainder, "--event")
    # Core's parser requires an explicit product flag. Preserve every other native
    # hook flag, including --with-context and --read-context.
    bridge_argv = ["--product", product, *remainder]
    no_event_core_action = "--replay-pending" in remainder or "--read-context" in remainder
    if event is None and not no_event_core_action:
        return _invoke_bridge(bridge_argv, raw, stdout)
    if no_event_core_action:
        return _invoke_bridge(bridge_argv, raw, stdout)
    # Let Core report unsupported native events before any managed setup occurs.
    if event not in EVENTS.get(product, {}):
        return _invoke_bridge(bridge_argv, raw, stdout)
    try:
        native = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, ValueError):
        native = {}
    cwd = native.get("cwd") if isinstance(native, dict) and isinstance(native.get("cwd"), str) else os.getcwd()
    message = None
    explicit_config_root = bool(environ.get("PMT_CONFIG_ROOT"))
    config_root, data_root = easy_setup.default_roots(environ)
    environ.setdefault("PMT_CONFIG_ROOT", str(config_root))
    environ.setdefault("PMT_DATA_ROOT", str(data_root))
    if event != "SessionStart":
        try:
            state = _cached_state(environ, cwd, explicit_config_root=explicit_config_root)
        except PmtError as error:
            _write_setup_error(stdout, event, error.code, product)
            return 0
        environ.update(state["env"])
        try:
            normalize_event(product, event, native, environ=environ)
        except (HookInputError, PmtError, ValueError):
            # The unchanged Core owns native event warnings and validation.
            return _invoke_bridge(bridge_argv, raw, stdout)
    else:
        try:
            existing_profile, _digest = _read_profile(config_root)
        except PmtError as error:
            _write_setup_error(stdout, event, error.code, product)
            return 0
        try:
            options, missing = easy_setup.client_mode.read_options(environ, product=product)
        except PmtError as error:
            if not existing_profile or existing_profile.get("mode") != "local":
                _write_setup_error(stdout, event, error.code, product)
                return 0
            options, missing = {}, []
        host_settings_present = any(options.get(key) for key in
                                    ("handoff_file", "host_url", "device_id", "namespace_id", "actor", "host_ca_file"))
        if existing_profile is None and host_settings_present and missing:
            state = prepare(environ, cwd, product=product)
            _write_setup_error(stdout, event, state.get("error_code", "handoff_invalid"), product,
                               missing=state.get("missing"))
            return 0
        validation_env = dict(environ)
        validation_env.setdefault("PMT_INSTALLATION_ID", str(uuid.uuid4()))
        try:
            normalize_event(product, event, native, environ=validation_env)
        except (HookInputError, PmtError, ValueError):
            return _invoke_bridge(bridge_argv, raw, stdout)
        legacy = bool(missing) and environ.get("PMT_CONFIG_ROOT") and is_legacy_root(environ["PMT_CONFIG_ROOT"])
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
                state = _cached_state(environ, cwd)
                environ.update(state["env"])
            except PmtError as error:
                _write_setup_error(stdout, event, error.code, product)
                return 0
        else:
            state = prepare(environ, cwd, product=product)
            if state["status"] != "ready":
                _write_setup_error(stdout, event, state.get("error_code", "setup_unavailable"), product,
                                   missing=state.get("missing"))
                return 0
            environ.update(state["env"])
            env_file = environ.get("CLAUDE_ENV_FILE")
            if env_file and product == "claude":
                easy_setup.write_env_file(env_file, state["env"])
            message = LINK_HINTS.get(state["link"])
            if state.get("configured_now"):
                setup_message = ("PMT: connected to the Host and saved the profile."
                                 if state.get("mode") == "hosted" else "PMT: initialized local storage and saved the profile.")
                message = (setup_message + " " + (message or "")).strip()
    return _invoke_bridge(bridge_argv, raw, stdout, message)


def _cached_state(environ, cwd, *, explicit_config_root=None):
    config_root, data_root = easy_setup.default_roots(environ)
    profile, _digest = _read_profile(config_root)
    if profile is None:
        return _legacy_local_cache(environ, config_root, data_root,
                                   explicit_config_root=explicit_config_root)
    if profile["mode"] == "local" and not (data_root / "pmt.sqlite3").is_file():
        raise PmtError("local_storage_uninitialized", "Local PMT storage is not initialized; wait for SessionStart.")
    if profile["mode"] == "hosted":
        load_credential(config_root, environ)
    metadata = read_client_metadata(config_root)
    py = environ.get("PMT_PYTHON") or (metadata or {}).get("python_path") or sys.executable
    env = {"PMT_CONFIG_ROOT": str(config_root), "PMT_DATA_ROOT": str(data_root), "PMT_PYTHON": py}
    root, branch = easy_setup.git_checkout(cwd)
    mapping, link = easy_setup.select_mapping(profile, root, branch)
    if mapping:
        env["PMT_SCOPE_ID"] = mapping["project_id"]
    elif root is not None:
        environ.pop("PMT_SCOPE_ID", None)
    else:
        scope = environ.get("PMT_SCOPE_ID")
        if scope:
            env["PMT_SCOPE_ID"] = scope
    return {"status": "ready", "mode": profile["mode"], "env": env, "link": link}


def _legacy_local_cache(environ, config_root, data_root, *, explicit_config_root=None):
    """Accept an old direct local root only when its Core DB is already initialized."""
    if explicit_config_root is None:
        explicit_config_root = bool(environ.get("PMT_CONFIG_ROOT"))
    if not explicit_config_root:
        raise PmtError("not_configured", "PMT has no cached storage profile.")
    metadata = read_client_metadata(config_root)
    if metadata and metadata.get("source") != "legacy":
        raise PmtError("not_configured", "Managed PMT storage has no cached profile.")
    if has_client_metadata(config_root) and metadata is None:
        raise PmtError("client_metadata_invalid", "PMT client metadata is invalid.")
    core_profile = Path(config_root) / "profile.json"
    database = Path(data_root) / "pmt.sqlite3"
    try:
        value = json.loads(core_profile.read_text(encoding="utf-8"))
        environment_id = value.get("environment_id") if isinstance(value, dict) else None
        if not isinstance(environment_id, str) or str(uuid.UUID(environment_id)) != environment_id:
            raise ValueError("invalid Core environment profile")
        with sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            rows = dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('environment_id','schema_version')"))
        if rows.get("environment_id") != environment_id or rows.get("schema_version") != "5":
            raise ValueError("uninitialized Core database")
    except (OSError, sqlite3.Error, ValueError, TypeError, AttributeError) as error:
        raise PmtError("not_configured", "PMT has no initialized legacy local profile.") from error
    py = environ.get("PMT_PYTHON") or (metadata or {}).get("python_path") or sys.executable
    env = {"PMT_CONFIG_ROOT": str(config_root), "PMT_DATA_ROOT": str(data_root), "PMT_PYTHON": py}
    if environ.get("PMT_SCOPE_ID"):
        env["PMT_SCOPE_ID"] = environ["PMT_SCOPE_ID"]
    return {"status": "ready", "mode": "local", "env": env, "link": "not_linked"}


def _invoke_bridge(argv, raw, stdout, extra_message=None):
    saved_stdin, saved_stdout = sys.stdin, sys.stdout
    captured = io.StringIO()
    sys.stdin = io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8")
    sys.stdout = captured
    try:
        code = bridge_main(argv)
    finally:
        sys.stdin, sys.stdout = saved_stdin, saved_stdout
    merged = _merge_output(captured.getvalue(), extra_message)
    if merged:
        stdout.write(merged + "\n")
    return code


def _write_setup_error(stdout, event, code, product="claude", missing=None):
    if str(code).startswith("handoff"):
        if code == "handoff_invalid" and isinstance(missing, list) and missing:
            message = "PMT Host setup is missing: " + ", ".join(missing)
        else:
            message = f"PMT handoff setup failed ({code}). Check the handoff file."
    elif str(code).startswith("credential"):
        message = f"PMT credential setup failed ({code}). Check the credential store."
    elif code == "hosted_settings_missing":
        message = "Hosted PMT settings are missing or incomplete; local storage was not opened."
    else:
        message = f"PMT setup failed ({code}). Check the PMT configuration."
    if product in {"claude", "codex"} and event != "SessionEnd":
        stdout.write(json.dumps({"systemMessage": message}, ensure_ascii=False) + "\n")
    else:
        print(message, file=sys.stderr)


def _argument_value(argv, name):
    try:
        index = argv.index(name)
    except ValueError:
        return None
    return argv[index + 1] if index + 1 < len(argv) and not argv[index + 1].startswith("--") else None
