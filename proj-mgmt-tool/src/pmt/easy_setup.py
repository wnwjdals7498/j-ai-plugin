"""Turn product plugin options into a ready hosted PMT environment.

Claude Code stores the options entered in its ``/plugin`` dialog and exports
them to hook processes as ``CLAUDE_PLUGIN_OPTION_<KEY>``.  This module maps
those values onto the existing storage contract: it publishes the hosted
profile through ``configure_storage`` (probe first, CAS on the current hash),
selects the project for the current checkout only from explicit mappings, and
returns the environment that hooks and the ``pmt`` command need.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

from .errors import PmtError
from .storage_config import _read_profile, configure_storage
from .client_setup import mode as client_mode

CREDENTIAL_ENV = "PMT_HOST_CREDENTIAL"
OPTION_PREFIX = "CLAUDE_PLUGIN_OPTION_"
REQUIRED_OPTIONS = ("host_url", "device_id", "namespace_id", "actor", "device_credential")


def default_roots(env):
    """Return the per-user ConfigRoot/DataRoot outside any checkout."""
    return client_mode.default_roots(env)


def read_options(env):
    """Return ``(options, missing)`` from the product's exported plugin options."""
    return client_mode.read_options(env, product="claude")


def _desired_connection(options):
    desired = {"endpoint": options["host_url"], "credential_env": CREDENTIAL_ENV,
               "device_id": options["device_id"], "namespace_id": options["namespace_id"],
               "actor": options["actor"]}
    if options.get("host_ca_file"):
        desired["ca_file"] = str(Path(options["host_ca_file"]).expanduser().resolve())
    return desired


def _matches(profile, desired):
    if not profile or profile.get("mode") != "hosted":
        return False
    if profile.get("ca_file") != desired.get("ca_file"):
        return False
    return all(profile.get(key) == value for key, value in desired.items() if key != "ca_file")


def ensure_profile(config_root, options, *, configure=configure_storage):
    """Publish the hosted profile when it is missing or differs from the options.

    The credential must already be present in ``os.environ[CREDENTIAL_ENV]``
    because the existing probe reads it from the process environment.
    Existing workspace mappings are preserved.  Returns ``(changed, profile)``.
    """
    desired = _desired_connection(options)
    profile, current_hash = _read_profile(config_root)
    if _matches(profile, desired):
        return False, profile
    if profile is not None and profile.get("mode") == "local":
        raise PmtError("easy_setup_local_profile", "A local PMT profile exists; it is not replaced automatically", 3)
    request = {"mode": "hosted", "expected_config_sha256": current_hash,
               "endpoint": desired["endpoint"], "credential_env": CREDENTIAL_ENV,
               "device_id": desired["device_id"], "namespace_id": desired["namespace_id"],
               "expected_actor": desired["actor"],
               "workspace_mappings": list(profile.get("workspace_mappings", [])) if profile else []}
    if desired.get("ca_file"):
        request["ca_file"] = desired["ca_file"]
    configure(str(config_root), request)
    profile, _ = _read_profile(config_root)
    return True, profile


def git_checkout(cwd):
    """Return ``(root, branch)`` for a Git checkout, branch None when detached."""
    def git(*args):
        completed = subprocess.run(["git", "-C", str(cwd), *args], stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, timeout=2, check=False)
        return completed.stdout.decode("utf-8", "replace").strip() if completed.returncode == 0 else None
    root = git("rev-parse", "--show-toplevel")
    if not root:
        return None, None
    return os.path.abspath(root), git("symbolic-ref", "--quiet", "--short", "HEAD")


def select_mapping(profile, root, branch):
    """Return ``(mapping, reason)`` using only configured mappings for this checkout."""
    if not profile or root is None:
        return None, "not_git" if profile else "not_configured"
    same_root = [item for item in profile.get("workspace_mappings", [])
                 if os.path.abspath(item["local_root"]) == root]
    if not same_root:
        return None, "not_linked"
    exact = [item for item in same_root if item.get("branch") == branch]
    if len(exact) == 1:
        return exact[0], "linked"
    return None, "branch_not_linked" if branch is not None else "detached"


def session_environment(config_root, data_root, python, options, mapping):
    """Variables the hook process and later Bash commands need."""
    env = {"PMT_CONFIG_ROOT": str(config_root), "PMT_DATA_ROOT": str(data_root),
           "PMT_PYTHON": python}
    if mapping is not None:
        env["PMT_SCOPE_ID"] = mapping["project_id"]
    return env


def write_env_file(path, variables):
    """Append ``export`` lines for Claude Code's ``CLAUDE_ENV_FILE``."""
    lines = "".join(f"export {key}={shlex.quote(value)}\n" for key, value in variables.items()
                     if key != CREDENTIAL_ENV)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as stream:
        stream.write(lines)


def prepare(env, cwd, *, configure=configure_storage, python=None, product="claude"):
    """Apply plugin options for one hook call.

    Returns a dict with ``status`` (``unconfigured``, ``ready`` or ``error``),
    the environment to apply, the selected mapping reason and a short message.
    Never raises for configuration problems; hooks must stay non-blocking.
    """
    return client_mode.prepare(env, cwd, product=product, configure=configure, python=python)
