"""Mode selection and local/hosted client preparation."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from .. import handoff
from ..db import Database
from ..errors import PmtError
from ..storage_config import _read_profile, configure_storage, probe_storage
from .client import read_client_metadata, write_client_metadata
from .credentials import ENV_NAME, load_credential, restore_credential, stage_credential

CLAUDE_PREFIX = "CLAUDE_PLUGIN_OPTION_"


def default_roots(environ=None):
    environ = os.environ if environ is None else environ
    home = Path(environ.get("HOME") or Path.home())
    legacy = home / ".config" / "pmt"
    if os.name == "nt":
        config_default = Path(environ.get("APPDATA") or home / "AppData" / "Roaming") / "pmt"
        data_default = Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / "pmt" / "data"
        if (legacy / "storage.json").is_file():
            config_default = legacy
            legacy_data = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "pmt" / "data"
            if legacy_data.is_dir():
                data_default = legacy_data
    else:
        config_default = Path(environ.get("XDG_CONFIG_HOME") or home / ".config") / "pmt"
        data_default = Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share") / "pmt" / "data"
    return (Path(environ.get("PMT_CONFIG_ROOT") or config_default),
            Path(environ.get("PMT_DATA_ROOT") or data_default))


def read_options(environ=None, *, product="claude"):
    environ = os.environ if environ is None else environ
    prefix = CLAUDE_PREFIX if product == "claude" else "PMT_"
    def get(key):
        return environ.get(prefix + key.upper())
    options = {key: value.strip() for key in ("handoff_file", "host_url", "device_id", "namespace_id", "actor",
                                               "device_credential", "host_ca_file")
               if isinstance((value := get(key)), str) and value.strip()}
    explicit_credential = environ.get(ENV_NAME)
    if not options.get("device_credential") and isinstance(explicit_credential, str) and explicit_credential:
        options["device_credential"] = explicit_credential
    if options.get("handoff_file"):
        document = handoff.load_handoff(options["handoff_file"])
        host, device = document["host"], document["device"]
        options.pop("host_ca_file", None)
        options.update(host_url=host["url"], device_id=device["device_id"],
                       namespace_id=document["namespace_id"], actor=device["actor"])
        if "ca_pem" in host:
            options["handoff_ca_pem"] = host["ca_pem"]
        # Explicit device_credential comes from a separately delivered option or env.
        credential = get("device_credential") or explicit_credential
        if credential:
            options["device_credential"] = credential
    required = ("host_url", "device_id", "namespace_id", "actor", "device_credential")
    missing = [] if options.get("host_url") else list(required)
    if options.get("host_url"):
        missing = [key for key in required if not options.get(key)]
    return options, missing


def _local(config_root, data_root, profile, profile_hash, configure, product):
    changed = False
    if profile is None:
        configure(str(config_root), {"mode": "local", "expected_config_sha256": profile_hash,
                                     "workspace_mappings": []})
        changed = True
    db = Database(str(data_root), str(config_root))
    from ..service import execute
    setup_request = {"protocol_version": 1, "operation": "setup", "request_id": str(uuid.uuid4()),
                     "actor": "pmt-client-setup", "session_id": str(uuid.uuid4()),
                     "payload": {"product": product}}
    result, exit_code = execute(db, setup_request)
    if exit_code:
        error = result.get("error") or {}
        raise PmtError(error.get("code", "local_setup_failed"), "Local PMT setup failed", exit_code)
    return changed


def prepare(environ, cwd, *, product="claude", configure=configure_storage, probe=probe_storage, python=None):
    """Prepare selected storage; hook callers receive a credential-free environment."""
    profile = None
    try:
        options, missing = read_options(environ, product=product)
        config_root, data_root = default_roots(environ)
        config_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        profile, current_hash = _read_profile(config_root)
        hosted_requested = bool(options.get("host_url"))
        codex_profile = product == "codex" and profile and profile.get("mode") == "hosted" and not options.get("handoff_file")
        if profile and profile.get("mode") == "hosted" and not hosted_requested and not codex_profile:
            raise PmtError("hosted_settings_missing", "Hosted profile settings are missing; local storage was not opened")
        if profile and profile.get("mode") == "local":
            changed = _local(config_root, data_root, profile, current_hash, configure, product) if not (data_root / "pmt.sqlite3").exists() else False
            warning = "Host settings are present. Use pmt storage switch --to hosted to change modes." if hosted_requested else None
            mode = "local"
        elif codex_profile:
            load_credential(config_root, environ)
            changed, warning, mode = False, None, "hosted"
        elif not hosted_requested:
            changed = _local(config_root, data_root, profile, current_hash, configure, product)
            warning, mode = None, "local"
        else:
            if missing:
                raise PmtError("hosted_settings_missing", "Host settings are incomplete")
            credential = environ.get(ENV_NAME) or options["device_credential"]
            credential_before, staged_credential, credential_changed = stage_credential(config_root, credential)
            options["device_credential"] = credential
            previous = os.environ.get(ENV_NAME)
            os.environ[ENV_NAME] = credential
            try:
                from ..easy_setup import ensure_profile
                changed, profile = ensure_profile(config_root, options, configure=configure)
                if credential_changed and not changed:
                    probe(str(config_root))
            except Exception:
                if credential_changed:
                    restore_credential(config_root, credential_before, expected_current=staged_credential)
                raise
            finally:
                if previous is None:
                    os.environ.pop(ENV_NAME, None)
                else:
                    os.environ[ENV_NAME] = previous
            # Existing hosted profile without current settings is rejected above; matched/reconfigured profiles are usable.
            environ[ENV_NAME] = credential
            warning, mode = None, "hosted"
        py = python or environ.get("PMT_PYTHON") or sys.executable
        metadata = read_client_metadata(config_root)
        source = metadata["source"] if metadata and metadata["source"] in {"plugin", "connect"} else "plugin"
        write_client_metadata(config_root, source=source, python_path=py, mode=mode)
        result_env = {"PMT_CONFIG_ROOT": str(config_root), "PMT_DATA_ROOT": str(data_root), "PMT_PYTHON": py}
        if profile:
            from ..easy_setup import git_checkout, select_mapping
            root, branch = git_checkout(cwd)
            mapping, link = select_mapping(profile, root, branch)
            if mapping:
                result_env["PMT_SCOPE_ID"] = mapping["project_id"]
        else:
            root, branch, link = None, None, "not_configured"
        return {"status": "ready", "mode": mode, "env": result_env, "configured_now": changed,
                "link": link, "root": root, "branch": branch, "scope_id": result_env.get("PMT_SCOPE_ID"),
                "message": warning}
    except PmtError as error:
        return {"status": "error", "mode": profile.get("mode") if 'profile' in locals() and profile else None,
                "env": {}, "error_code": error.code, "message": _safe_message(error.code)}
    except OSError:
        return {"status": "error", "env": {}, "error_code": "easy_setup_io_error",
                "message": "PMT setup could not write its local configuration."}


def _safe_message(code):
    if code.startswith("handoff"):
        return f"PMT handoff setup failed ({code}). Check the handoff file."
    if code in {"credential_unavailable", "credential_store_unreadable", "credential_store_insecure"}:
        return f"PMT credential setup failed ({code}). Check the credential store."
    if code == "hosted_settings_missing":
        return "Hosted PMT settings are missing or incomplete; local storage was not opened."
    return f"PMT setup failed ({code}). Check the PMT configuration."
