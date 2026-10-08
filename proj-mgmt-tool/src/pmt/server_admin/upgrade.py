"""Guarded S17 release upgrade orchestration with an injectable system adapter."""
from __future__ import annotations

import base64
import copy
import json
import os
import re
import socket
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

from .. import __version__
from ..db import SCHEMA_VERSION
from ..errors import PmtError
from ..handoff import PLUGIN_VERSION
from ..host.auth import AuthRegistry, HOST_SCHEMA_VERSION
from ..http_store import HttpStore
from ..migration import GRAPH_SCHEMA_VERSION
from .backup import _backup_root, _clean_scratch, _issue_local_admin, _revoke_local_admin, create_backup
from .config import _process_lock, _publish_config_locked, config_path, load_config_snapshot
from .operations import NativeOperationsAdapter
from .registry import _host
from .runtime_paths import resolve_runtime_config
from .service import execute_service_plan, service_plan
from .serve import _runtime_path_safe

_SOURCE_REPO = "https://github.com/wnwjdals7498/j-ai-plugin"
_SHA = re.compile(r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$")
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def _version_tuple(value):
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        raise PmtError("host_input_invalid", "Upgrade version must be a stable x.y.z release")
    return tuple(int(part) for part in value.split("."))


def _source_sha(value):
    if not isinstance(value, str) or not _SHA.fullmatch(value):
        raise PmtError("upgrade_source_invalid", "Installing a release requires a full immutable Git commit SHA")
    return value.lower()


def _python_path(app_root):
    suffix = Path("venv") / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    return Path(app_root) / suffix


def _candidate_install_commands(app_root, source_ref, *, bootstrap_python=None):
    """Construct pinned candidate venv/install commands without executing them."""
    app_root = Path(app_root)
    python = _python_path(app_root)
    source_url = f"proj-mgmt-tool[host] @ git+{_SOURCE_REPO}@{_source_sha(source_ref)}#subdirectory=proj-mgmt-tool"
    return [
        [str(bootstrap_python or sys.executable), "-m", "venv", str(python.parent.parent)],
        [str(python), "-m", "pip", "install", source_url],
    ]


def _app_root(config, target_version, supplied, config_root):
    current = Path(config["service"]["app_root"])
    target = Path(supplied) if supplied else current.parent / target_version
    if not target.is_absolute():
        raise PmtError("path_unsafe", "Upgrade AppRoot must be an absolute path")
    if target.resolve(strict=False) == current.resolve(strict=False):
        raise PmtError("upgrade_target_invalid", "Upgrade AppRoot must preserve the current release directory")
    for value in (config_root, *config["paths"].values()):
        path = Path(value).resolve(strict=False)
        dest = target.resolve(strict=False)
        if dest == path or dest in path.parents or path in dest.parents:
            raise PmtError("path_unsafe", "Upgrade AppRoot must remain separate from Host data, logs and backup")
    if not _runtime_path_safe(target):
        raise PmtError("path_unsafe", "Upgrade AppRoot cannot be a symlink or reparse point")
    if target.exists() and (target.is_symlink() or bool(getattr(target.lstat(), "st_file_attributes", 0) & 0x400)):
        raise PmtError("path_unsafe", "Upgrade AppRoot cannot be a symlink or reparse point")
    return target.resolve(strict=False)


def _check_release_info(info, requested):
    if not isinstance(info, dict) or info.get("version") != requested:
        raise PmtError("upgrade_version_mismatch", "Candidate pmt-server release does not match --version")
    core = info.get("core_version")
    if (not isinstance(core, str) or tuple(core.split(".")[:2]) != tuple(__version__.split(".")[:2])
            or info.get("db_schema") != SCHEMA_VERSION or info.get("graph_schema") != GRAPH_SCHEMA_VERSION
            or info.get("protocol") != 1 or info.get("host_schema") != HOST_SCHEMA_VERSION):
        raise PmtError("upgrade_incompatible", "Candidate release changes an unsupported Core, DB, graph, protocol or Host schema")
    py = info.get("python_version", "")
    try: supported_python = tuple(int(part) for part in py.split(".")[:2]) >= (3, 13)
    except (ValueError, AttributeError): supported_python = False
    if not supported_python:
        raise PmtError("python_unsupported", "Candidate release requires Python 3.13 or newer")
    missing = [name for name in ("fastapi", "pydantic", "uvicorn") if not info.get("dependencies", {}).get(name)]
    if missing: raise PmtError("host_dependency_missing", "Candidate venv is missing a required Host dependency")


def _temporary_doctor_config(config, app_root, live_config_root, backup_root):
    """Build a scratch config/metadata DB; never copy or modify the live Host DB."""
    scratch = Path(tempfile.mkdtemp(prefix=".pmt-upgrade-doctor-", dir=backup_root))
    if os.name != "nt": os.chmod(scratch, 0o700)
    config_root, data_root, log_root, temp_backup = (scratch / name for name in ("config", "data", "logs", "backup"))
    for path in (config_root, data_root, log_root, temp_backup): path.mkdir(mode=0o700)
    probe_port = 0
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0)); probe_port = probe.getsockname()[1]
    copy_config = copy.deepcopy(resolve_runtime_config(config, live_config_root, environ={}))
    copy_config["service"]["app_root"] = str(app_root)
    # Candidate Doctor is an isolated diagnostic; it must not resolve systemd
    # credential aliases after claim-key references are replaced with scratch env vars.
    copy_config["service"]["kind"] = "none"
    copy_config["paths"] = {"data_root": str(data_root), "log_dir": str(log_root), "backup_dir": str(temp_backup)}
    public_host = urlparse(config["public_url"]).hostname
    if not public_host: raise PmtError("config_invalid", "Configured public URL host is unavailable")
    copy_config["listen"] = {"host": "127.0.0.1", "port": probe_port}
    authority = f"[{public_host}]" if ":" in public_host else public_host
    copy_config["public_url"] = f"https://{authority}:{probe_port}"
    key_name = "PMT_UPGRADE_PREFLIGHT_" + uuid.uuid4().hex.upper()
    copy_config["claim_key"]["source"] = {"kind": "env", "name": key_name}
    for index, item in enumerate(copy_config["claim_key"].get("retained", [])):
        item["source"] = {"kind": "env", "name": f"{key_name}_R{index}"}
    # The doctor reads only these version/identity meta fields; no user rows/resources are copied.
    conn = sqlite3.connect(str(data_root / "pmt.sqlite3"))
    try:
        conn.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.executemany("INSERT INTO meta(key,value) VALUES(?,?)", [
            ("schema_version", str(SCHEMA_VERSION)), ("host_schema_version", str(HOST_SCHEMA_VERSION)),
            ("host_namespace_id", str(uuid.uuid4()))])
        conn.commit()
    finally:
        conn.close()
    (config_root / "host-config.json").write_text(json.dumps(copy_config), encoding="utf-8")
    environment = os.environ.copy()
    value = base64.b64encode(os.urandom(48)).decode("ascii")
    environment[key_name] = value
    for item in copy_config["claim_key"].get("retained", []):
        environment[item["source"]["name"]] = value
    return scratch, config_root, environment


def _cas_app_root(config_root, expected_app_root, new_app_root):
    root = Path(config_root)
    with _process_lock(root / ".host-config.lock"):
        config, digest = load_config_snapshot(config_path(root))
        if Path(config["service"]["app_root"]).resolve(strict=False) != Path(expected_app_root).resolve(strict=False):
            raise PmtError("config_conflict", "service.app_root changed during the upgrade; preserved")
        updated = copy.deepcopy(config)
        updated["service"]["app_root"] = str(new_app_root)
        updated["revision"] += 1
        from .config import validate_config
        validate_config(updated)
        _publish_config_locked(root, updated, digest)
        return updated


class NativeUpgradeAdapter:
    """Real upgrade effects are isolated here; tests inject a fake adapter."""

    def __init__(self):
        self.operations = NativeOperationsAdapter()
        self.platform = self.operations.platform

    def is_admin(self): return self.operations.is_admin()

    def install_candidate(self, app_root, source_ref):
        app_root = Path(app_root)
        if app_root.exists() or app_root.is_symlink():
            raise PmtError("upgrade_target_exists", "Candidate AppRoot already exists; it will not be overwritten")
        if not app_root.parent.is_dir(): raise PmtError("path_unsafe", "Candidate AppRoot parent must already exist")
        commands = _candidate_install_commands(app_root, source_ref)
        try:
            created = subprocess.run(commands[0], capture_output=True, timeout=300, check=False)
            if created.returncode: raise PmtError("upgrade_install_failed", "Candidate virtual environment creation failed")
            install = subprocess.run(commands[1], capture_output=True, timeout=1800, check=False)
            if install.returncode: raise PmtError("upgrade_install_failed", "Pinned candidate install failed")
            return {"ok": True, "python": str(_python_path(app_root)), "source_ref": _source_sha(source_ref)}
        except (OSError, subprocess.SubprocessError) as exc:
            raise PmtError("upgrade_install_failed", "Pinned candidate install could not run") from exc

    def version(self, python):
        try:
            result = subprocess.run([str(python), "-m", "pmt.server_admin", "version", "--json"],
                                    capture_output=True, text=True, encoding="utf-8", errors="replace",
                                    timeout=30, check=False)
            if result.returncode: raise PmtError("upgrade_candidate_invalid", "Candidate version command failed")
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            if payload.get("ok") is False: payload = payload.get("result", {})
            return payload
        except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
            raise PmtError("upgrade_candidate_invalid", "Candidate version output is invalid") from exc

    def doctor(self, python, config_root, environment):
        try:
            result = subprocess.run([str(python), "-m", "pmt.server_admin", "--config-root", str(config_root),
                                     "doctor", "--json"], cwd=Path(python).parent.parent.parent,
                                    env=environment, capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=90, check=False)
            payload = json.loads(result.stdout.strip().splitlines()[-1])
            return payload if isinstance(payload, dict) else {"ok": False, "checks": []}
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return {"ok": False, "checks": []}

    def service_state(self, config, config_root):
        return self.operations.service_state(config, config_root,
            current_account=self.operations.current_account() if self.platform == "windows" and config["service"]["account"] == "current" else None)

    def _service_action(self, action, config, config_root):
        from .service import service_commands
        current_account = self.operations.current_account() if self.platform == "windows" and config["service"]["account"] == "current" else None
        results = []
        for command in service_commands(action, config, config_root, platform=self.platform, current_account=current_account):
            outcome = self.operations.execute(command); results.append(outcome)
            if not outcome.get("ok"): raise PmtError("service_apply_failed", f"Service {action} failed")
        return {"ok": True, "results": results}

    def stop_service(self, config, config_root): return self._service_action("stop", config, config_root)

    def start_service(self, config, config_root): return self._service_action("start", config, config_root)

    def install_and_start_service(self, config, config_root):
        current = self.service_state(config, config_root)
        plan = service_plan(config, config_root, current=current, action="install", platform=self.platform,
                            current_account=self.operations.current_account() if self.platform == "windows" and config["service"]["account"] == "current" else None)
        return execute_service_plan(plan, self.operations, apply=True)

    def create_backup(self, config_root): return create_backup(config_root, apply=True)

    def health_compatibility(self, config_root, expected_namespace):
        config, _digest, _db, auth = _host(config_root)
        from .tls import check_tls
        config = resolve_runtime_config(config, config_root, environ={})
        check_tls(config)
        issued, headers = _issue_local_admin(auth, "pmt-server-upgrade-check")
        env_name = "PMT_SERVER_UPGRADE_CHECK_" + uuid.uuid4().hex.upper()
        prior = os.environ.get(env_name)
        os.environ[env_name] = issued["credential"]
        failure = None
        cleanup_failure = None
        result = None
        try:
            parsed = urlparse(config["public_url"])
            context = ssl.create_default_context(cafile=config["tls"].get("ca_file"))
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=context))
            with opener.open(config["public_url"].rstrip("/") + "/health", timeout=5) as response:
                if response.status != 200: raise PmtError("host_unreachable", "Upgraded Host health check failed")
            client = HttpStore(config["public_url"], env_name, issued["device_id"],
                               headers["x-pmt-environment"], auth.namespace_id,
                               ca_file=config["tls"].get("ca_file"), timeout=8)
            result = client.check_compatibility()
            if result["namespace_id"] != expected_namespace:
                raise PmtError("upgrade_namespace_changed", "Host namespace changed during upgrade")
        except Exception as exc:
            failure = exc
        finally:
            if prior is None: os.environ.pop(env_name, None)
            else: os.environ[env_name] = prior
        try: _revoke_local_admin(auth, issued)
        except PmtError as exc: cleanup_failure = exc
        if cleanup_failure:
            raise PmtError("upgrade_check_device_revoke_failed", str(cleanup_failure)) from cleanup_failure
        if failure: raise PmtError("upgrade_health_failed", "Upgraded Host health or authenticated compatibility check failed") from failure
        return {"health": "ok", "compatibility": "ok", "namespace_id": result["namespace_id"],
                "device_id": issued["device_id"]}


def _candidate_doctor(adapter, config, config_root, app_root, backup_root):
    scratch, diagnostic_config, environment = _temporary_doctor_config(config, app_root, config_root, backup_root)
    try:
        python = _python_path(app_root)
        info = adapter.version(python)
        _check_release_info(info, info.get("version"))
        result = adapter.doctor(python, diagnostic_config, environment)
        failed = [item for item in result.get("checks", []) if item.get("status") == "fail"]
        if not result.get("ok") or failed:
            raise PmtError("upgrade_doctor_failed", "Candidate release failed isolated read-only doctor checks")
        return info
    finally:
        _clean_scratch(scratch, backup_root, ".pmt-upgrade-doctor-")


def _switch_app_root(config_root, expected, target):
    return _cas_app_root(config_root, expected, target)


def _rollback_upgrade(config_root, old_config, new_app_root, adapter):
    result = {"app_root_restored": False, "service_restored": False, "errors": []}
    candidate_config = {**old_config, "service": {**old_config["service"], "app_root": str(new_app_root)}}
    try:
        stopped = adapter.stop_service(candidate_config, config_root)
        if isinstance(stopped, dict) and stopped.get("ok") is False:
            raise PmtError("service_apply_failed", "Could not stop the candidate Host service")
    except Exception as exc: result["errors"].append("candidate service stop failed")
    try:
        restored = _cas_app_root(config_root, new_app_root, old_config["service"]["app_root"])
        result["app_root_restored"] = True
        try:
            started = adapter.install_and_start_service(restored, config_root)
            if isinstance(started, dict) and started.get("ok") is False:
                raise PmtError("service_apply_failed", "Could not start the restored Host service")
            result["service_restored"] = True
        except Exception: result["errors"].append("old service could not be re-registered and started")
    except Exception: result["errors"].append("app_root rollback conflicted with a later config change")
    return result


def upgrade_host(config_root, target_version, *, app_root=None, source_ref=None, apply=False, adapter=None):
    adapter = adapter or NativeUpgradeAdapter()
    root = Path(config_root)
    config, digest = load_config_snapshot(config_path(root))
    requested = _version_tuple(target_version)
    current = _version_tuple(PLUGIN_VERSION)
    if requested <= current: raise PmtError("upgrade_version_invalid", "Target server release must be newer than the installed release")
    target_root = _app_root(config, target_version, app_root, root)
    current_root = Path(config["service"]["app_root"]).resolve(strict=False)
    if config["service"]["kind"] == "none":
        raise PmtError("upgrade_service_unmanaged", "Automatic upgrade requires a configured Windows task or systemd service")
    if source_ref is not None: source_ref = _source_sha(source_ref)
    target_python = _python_path(target_root)
    install_needed = not target_python.is_file()
    if install_needed and target_root.exists():
        raise PmtError("upgrade_target_exists", "Candidate AppRoot exists without a complete venv; it will not be overwritten")
    blocked = "upgrade_source_required" if install_needed and source_ref is None else None
    if blocked:
        return {"ok": False, "applied": False, "error_code": blocked,
                "message": "No candidate venv exists; provide a full immutable --source-ref SHA to install safely",
                "target_app_root": str(target_root), "steps": ["install separate venv", "doctor", "backup", "stop", "CAS app_root", "service start", "health + compatibility"]}
    if not target_root.parent.is_dir():
        raise PmtError("path_unsafe", "Candidate AppRoot parent must already exist")
    original_namespace = _host(root)[3].namespace_id
    state = adapter.service_state(config, root)
    if not state.get("exists") or not state.get("matches"):
        raise PmtError("service_mismatch", "The configured PMT Host service must be present and running before upgrade")
    steps = ["install separate target venv" if install_needed else "validate prepared target venv",
             "candidate version + isolated read-only doctor", "quiescent Host backup", "stop service",
             "CAS service.app_root", "re-register and start service", "health + authenticated compatibility"]
    if not apply:
        if install_needed:
            return {"ok": True, "applied": False, "plan": steps, "target_app_root": str(target_root),
                    "source_ref": source_ref, "service_state": state}
        info = _candidate_doctor(adapter, config, root, target_root, _backup_root(config))
        _check_release_info(info, target_version)
        return {"ok": True, "applied": False, "plan": steps, "target_app_root": str(target_root),
                "candidate": info, "service_state": state}

    if not adapter.is_admin(): raise PmtError("admin_required", "Upgrade requires administrator privileges")

    installed = False
    switched = False
    stop_attempted = False
    backup = None
    try:
        if install_needed:
            installed = True
            installed_result = adapter.install_candidate(target_root, source_ref)
            if isinstance(installed_result, dict) and installed_result.get("ok") is False:
                raise PmtError("upgrade_install_failed", "Candidate install adapter reported failure")
        candidate = _candidate_doctor(adapter, config, root, target_root, _backup_root(config))
        _check_release_info(candidate, target_version)
        backup = adapter.create_backup(root)
        if not backup.get("ok"):
            raise PmtError(backup.get("error_code", "migration_source_not_quiescent"), "Quiescent Host backup failed; upgrade stopped")
        stop_attempted = True
        stopped_result = adapter.stop_service(config, root)
        if isinstance(stopped_result, dict) and stopped_result.get("ok") is False:
            raise PmtError("service_apply_failed", "Could not stop the current Host service")
        switched_config = _switch_app_root(root, current_root, target_root)
        switched = True
        service_result = adapter.install_and_start_service(switched_config, root)
        if isinstance(service_result, dict) and service_result.get("ok") is False:
            raise PmtError("service_apply_failed", "Candidate Host service could not be started")
        check = adapter.health_compatibility(root, original_namespace)
        if (not isinstance(check, dict) or check.get("health") != "ok" or check.get("compatibility") != "ok"
                or check.get("namespace_id") != original_namespace):
            raise PmtError("upgrade_health_failed", "Upgraded Host health or authenticated compatibility result is invalid")
        return {"ok": True, "applied": True, "target_version": target_version,
                "target_app_root": str(target_root), "backup": backup,
                "candidate": {key: candidate[key] for key in ("version", "core_version", "db_schema", "graph_schema", "protocol", "host_schema")},
                "health_compatibility": check, "old_release_preserved": True}
    except Exception as exc:
        rollback = {"app_root_restored": False, "service_restored": False, "errors": []}
        if switched:
            rollback = _rollback_upgrade(root, config, target_root, adapter)
        elif stop_attempted:
            try:
                if state.get("matches"):
                    started = adapter.start_service(config, root)
                    if isinstance(started, dict) and started.get("ok") is False:
                        raise PmtError("service_apply_failed", "Could not restart the prior Host service")
                rollback["service_restored"] = True
            except Exception: rollback["errors"].append("prior service could not be restarted")
        else:
            rollback["app_root_restored"] = True
            rollback["service_restored"] = True
        return {"ok": False, "applied": True, "target_version": target_version,
                "backup": backup, "rolled_back": rollback["app_root_restored"] and rollback["service_restored"], "rollback": rollback,
                "error_code": exc.code if isinstance(exc, PmtError) else "upgrade_failed",
                "failure_type": type(exc).__name__,
                "message": str(exc) if isinstance(exc, PmtError) else "Upgrade failed; prior release recovery was attempted",
                "candidate_app_root": str(target_root), "candidate_install_preserved": installed}
