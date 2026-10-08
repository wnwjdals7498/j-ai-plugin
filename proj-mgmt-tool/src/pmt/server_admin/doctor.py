"""Read-only launch diagnostics for an initialized PMT Host."""
from __future__ import annotations

import importlib
import http.client
import ipaddress
import os
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from .. import __version__
from ..db import SCHEMA_VERSION
from ..errors import PmtError
from ..handoff import PLUGIN_VERSION
from ..host.auth import HOST_SCHEMA_VERSION
from .config import load_config_snapshot, validate_config
from .secrets import _account_sid, _account_uid, check_source
from .tls import check_tls


def _result(name, status, code=None, message="", guidance=""):
    return {"name": name, "status": status, "code": code, "message": message, "guidance": guidance}


def _readonly_meta(config):
    path = Path(config["paths"]["data_root"]) / "pmt.sqlite3"
    if not path.is_file(): raise PmtError("host_schema_unsupported", "Host database is absent")
    with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=1) as conn:
        return dict(conn.execute("SELECT key,value FROM meta WHERE key IN ('schema_version','host_schema_version','host_namespace_id')"))


def _loopback(url):
    parsed = urlparse(url)
    try: return ipaddress.ip_address(parsed.hostname).is_loopback
    except (ValueError, TypeError): return False


def _health(config, timeout=1.5):
    parsed = urlparse(config["public_url"])
    if parsed.port == 8765: return None  # C2 deliberately never probes the production Host port.
    host = config["listen"]["host"]
    try: address = ipaddress.ip_address(host)
    except ValueError: return None
    connect_host = host if address.is_loopback else ("::1" if address.version == 6 else "127.0.0.1")
    try:
        if config["tls"].get("cert_file"):
            import ssl
            context = ssl.create_default_context(cafile=config["tls"].get("ca_file"))
            class LocalTLSConnection(http.client.HTTPSConnection):
                def connect(self):
                    sock = socket.create_connection((connect_host, parsed.port or 443), timeout=self.timeout)
                    self.sock = context.wrap_socket(sock, server_hostname=parsed.hostname)
            connection = LocalTLSConnection(parsed.hostname, parsed.port or 443, timeout=timeout, context=context)
        else:
            connection = http.client.HTTPConnection(connect_host, config["listen"]["port"], timeout=timeout)
        connection.request("GET", "/health", headers={"Host": parsed.netloc})
        response = connection.getresponse()
        result = response.status == 200
        response.read()
        connection.close()
        return result
    except (OSError, http.client.HTTPException, ValueError):
        return False


def _path_safety(path):
    target = Path(path)
    text = str(target).lower().replace("/", "\\")
    if target.is_symlink(): return False, "Path is a symlink"
    if any(marker in text for marker in ("\\.git\\", "\\onedrive\\", "\\dropbox\\", "\\google drive\\", "\\icloud\\")):
        return False, "Path is inside a Git or sync-managed directory"
    if text.startswith("\\\\"): return False, "Network paths are unsupported for Host state"
    if os.name == "nt" and len(text) >= 2 and text[1] == ":":
        try:
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(target.anchor)) == 4: return False, "Network drives are unsupported for Host state"
        except Exception: pass
    return True, "Path is local and outside known Git/sync roots"


def run_doctor(config_root):
    checks = []
    config = None
    try:
        config, _ = load_config_snapshot(Path(config_root) / "host-config.json")
        validate_config(config)
        checks.append(_result("config", "ok", message="Host configuration schema and revision are valid"))
    except PmtError as exc:
        checks.append(_result("config", "fail", exc.code, "Host configuration could not be validated", "Run pmt-server config validate and repair the reported fields"))

    python_ok = sys.version_info >= (3, 13)
    expected = Path(config["service"]["app_root"]) / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python") if config else None
    in_release_venv = expected is not None and Path(sys.executable).resolve() == expected.resolve()
    if not python_ok:
        checks.append(_result("python", "fail", "python_unsupported", "Python 3.13 or newer is required", "Run doctor with the configured release venv Python"))
    elif in_release_venv:
        checks.append(_result("python", "ok", message=f"Python {sys.version.split()[0]} is supported"))
    elif config and config["service"]["kind"] != "none":
        checks.append(_result("python", "fail", "python_unsupported", "Python or release venv does not match service.app_root", "Run doctor with the configured release venv Python"))
    else:
        checks.append(_result("python", "warn", "python_dev_mode", "service.kind=none uses a development interpreter", "Use the release venv before configuring an automatic service"))

    missing = []
    for name in ("fastapi", "pydantic", "uvicorn"):
        try: importlib.import_module(name)
        except ImportError: missing.append(name)
    checks.append(_result("deps", "fail" if missing else "ok", "host_dependency_missing" if missing else None,
                          "Required Host modules are unavailable" if missing else "Host modules import successfully",
                          "Install proj-mgmt-tool[host] into the configured venv" if missing else ""))

    version_ok = config is not None
    if config:
        try:
            meta = _readonly_meta(config)
            version_ok = (meta.get("schema_version") == str(SCHEMA_VERSION) and
                          meta.get("host_schema_version") == str(HOST_SCHEMA_VERSION) and bool(meta.get("host_namespace_id")))
        except (PmtError, sqlite3.Error, OSError):
            version_ok = False
    checks.append(_result("version", "ok" if version_ok else "fail", None if version_ok else "host_schema_unsupported",
                          f"PMT Server {PLUGIN_VERSION}, Core {__version__}, DB {SCHEMA_VERSION}, Host {HOST_SCHEMA_VERSION}" if version_ok else "Host DB schema or metadata is absent or incompatible",
                          "Restore or migrate the Host database using a supported release" if not version_ok else ""))

    paths_ok, path_message = True, "Configured data, log and backup paths are ready"
    account_uncertain = False
    if config:
        try:
            configured = config["service"]["account"]
            account_uncertain = (_account_sid(configured) != _account_sid("current") if os.name == "nt"
                                 else _account_uid(configured) != os.getuid())
        except PmtError:
            account_uncertain = True
    if config:
        for name, value in config["paths"].items():
            safe, reason = _path_safety(value)
            target = Path(value)
            if not target.is_dir() or not os.access(target, os.R_OK | os.W_OK) or not safe:
                paths_ok = False; path_message = f"Configured {name} path is unavailable or unsafe"; break
    checks.append(_result("paths", "fail" if not paths_ok else "warn" if account_uncertain else "ok",
                          "path_unsafe" if not paths_ok else None,
                          path_message if not account_uncertain else path_message + "; configured service-account access is not proven in this session",
                          "Use local, writable paths outside Git/sync/network roots and verify access as the service account" if not paths_ok or account_uncertain else ""))

    if config:
        try:
            check_source(config["claim_key"]["source"], account=config["service"]["account"])
            for item in config["claim_key"].get("retained", []): check_source(item["source"], account=config["service"]["account"])
            checks.append(_result("claim_key", "ok", message="Primary and retained key references are readable and protected"))
        except PmtError as exc:
            checks.append(_result("claim_key", "fail", exc.code, "A configured claim-key reference is unavailable or insecure", "Repair the key reference and ACL, then rerun secret check"))
    else: checks.append(_result("claim_key", "fail", "config_invalid", "Claim-key checks require valid configuration", "Repair host-config.json first"))

    if config:
        try:
            tls_result = check_tls(config)
            status = "warn" if tls_result.get("warning") or tls_result.get("warning") and not tls_result.get("configured") else "ok"
            checks.append(_result("tls", status, message=tls_result.get("warning") or "TLS chain, hostname, and expiry checks passed",
                                  guidance="Register a certificate with matching SAN and trusted chain" if status == "warn" else ""))
        except PmtError as exc:
            checks.append(_result("tls", "fail", exc.code, "TLS certificate, chain, hostname, or expiry check failed", "Run pmt-server tls register with a valid matching certificate/key pair"))
    else: checks.append(_result("tls", "fail", "config_invalid", "TLS check requires valid configuration", "Repair host-config.json first"))

    port_status, port_msg = "warn", "Port check requires valid configuration"
    if config:
        host, port = config["listen"]["host"], config["listen"]["port"]
        try:
            with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind((host, port)); port_status, port_msg = "ok", "Configured listener address is available"
        except OSError:
            health = _health(config)
            if health is True: port_status, port_msg = "warn", "Port is occupied by the responding configured Host"
            else: port_status, port_msg = "fail", "Configured listener port is occupied by an unverified listener"
    checks.append(_result("port", port_status, "port_in_use" if port_status == "fail" else None, port_msg,
                          "Stop the foreign listener or confirm the configured Host is already running" if port_status != "ok" else ""))

    # C2 permits only read-only service/firewall observations; registration is deferred.
    checks.append(_result("firewall", "warn", "firewall_unverified", "Firewall rule was not inspected during this offline diagnostic run", "Verify the allowed-source rule before exposing the listener"))
    service_status = "ok" if config and config["service"]["kind"] == "none" else "warn"
    checks.append(_result("service", service_status, "service_unverified" if service_status == "warn" else None,
                          "Automatic service is disabled" if service_status == "ok" else "Service registration is deferred to the service-management phase",
                          "Use the documented service install/status workflow after review" if service_status == "warn" else ""))

    health = _health(config) if config else None
    if health is True:
        checks.append(_result("runtime", "warn", "compat_unchecked", "Configured Host health endpoint responded; authenticated compatibility was not checked because no diagnostic device is configured", "Use an explicitly issued diagnostic device for compatibility verification"))
    elif health is False:
        checks.append(_result("runtime", "warn", "host_unreachable", "Loopback health endpoint did not respond", "Start the Host after fixing any failed preflight checks"))
    else:
        checks.append(_result("runtime", "warn", "compat_unchecked", "Runtime endpoint was not probed; no diagnostic device credential is configured", "Use an explicitly issued diagnostic device for compatibility verification"))

    if config:
        lock_path = Path(config["paths"]["data_root"]) / ".pmt-server.lock"
        if lock_path.exists():
            from .serve import instance_available
            available = instance_available(lock_path)
            duplicate_status = "ok" if available else "fail"
        else: duplicate_status = "ok"
        checks.append(_result("duplicates", duplicate_status, None if duplicate_status == "ok" else "host_duplicate",
                              "No server currently holds the data-root lock" if duplicate_status == "ok" else "Another Host holds the data-root lock",
                              "Stop the other Host process before starting this one" if duplicate_status == "fail" else ""))
    else: checks.append(_result("duplicates", "warn", "config_invalid", "Duplicate check requires valid configuration"))

    if sys.platform.startswith("linux"):
        try:
            proc = subprocess.run(["getenforce"], capture_output=True, text=True, timeout=2, check=False)
            selinux = proc.stdout.strip()
            checks.append(_result("selinux", "warn", "selinux_review", f"SELinux mode: {selinux or 'unknown'}; AVC log review not performed", "Review recent AVC denials if startup fails"))
        except (OSError, subprocess.SubprocessError): checks.append(_result("selinux", "warn", "selinux_unavailable", "SELinux state is unavailable", "Review SELinux AVC denials if startup fails"))
    else: checks.append(_result("selinux", "ok", message="SELinux check is not applicable on this platform"))

    return {"ok": not any(item["status"] == "fail" for item in checks), "checks": checks}
