"""Preflight and run the existing Host entry point under a data-root lock."""
from __future__ import annotations

import contextlib
import json
import ipaddress
import os
from datetime import datetime, timezone
from pathlib import Path

from ..db import Database
from ..errors import PmtError
from .config import load_config_snapshot
from .logging import server_log_config
from .secrets import _account_sid, _account_uid, _path_reparse, read_key, validate_service_account
from .tls import check_tls


def _try_lock(path, *, create=True):
    path = Path(path)
    if not create and not path.exists(): return None
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, "a+b" if create else "r+b")
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if create and stream.tell() == 0: stream.write(b"0"); stream.flush()
            stream.seek(0)
            try: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                stream.close(); return None
        else:
            import fcntl
            try: fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                stream.close(); return None
        return stream
    except Exception:
        stream.close()
        raise


def instance_available(lock_path):
    if not Path(lock_path).exists(): return True
    stream = _try_lock(lock_path, create=False)
    if stream is None: return not Path(lock_path).exists()
    _unlock(stream)
    return True


def _unlock(stream):
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally: stream.close()


def _write_status_marker(path):
    path = Path(path)
    payload = json.dumps({"pid": os.getpid(), "started_at": datetime.now(timezone.utc).isoformat()}).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try: temporary.unlink()
        except OSError: pass
        raise


@contextlib.contextmanager
def instance_lock(lock_path):
    stream = _try_lock(lock_path)
    if stream is None:
        raise PmtError("host_duplicate", "Another Host process holds the configured data-root lock", 1)
    status_path = Path(lock_path).with_name(".pmt-server.status.json")
    try:
        _write_status_marker(status_path)
        yield
    finally:
        try:
            marker = json.loads(status_path.read_text(encoding="utf-8"))
            if marker.get("pid") == os.getpid(): status_path.unlink()
        except (OSError, ValueError, AttributeError): pass
        _unlock(stream)


def _parse_listen(value):
    try:
        host, port = value.rsplit(":", 1)
        ipaddress.ip_address(host)
        port = int(port)
        if not 1 <= port <= 65535: raise ValueError
        return host, port
    except ValueError as exc:
        raise PmtError("host_bind_invalid", "--listen must be a literal IP:port") from exc


def _runtime_path_safe(path):
    target = Path(path)
    lowered = str(target).lower().replace("/", "\\")
    if _path_reparse(target) or lowered.startswith("\\\\") or any(
            marker in lowered for marker in ("\\.git\\", "\\onedrive\\", "\\dropbox\\", "\\google drive\\", "\\icloud\\")):
        return False
    if os.name == "nt" and len(lowered) >= 2 and lowered[1] == ":":
        try:
            import ctypes
            if ctypes.windll.kernel32.GetDriveTypeW(str(target.anchor)) == 4: return False
        except Exception: pass
    return True


def serve_host(config_root, *, listen=None, allow_loopback_http=False):
    from .config import config_path
    config, _ = load_config_snapshot(config_path(config_root))
    data_root = Path(config["paths"]["data_root"])
    configured_account = config["service"]["account"]
    expected_account = validate_service_account(configured_account)
    current_account = _account_sid("current") if os.name == "nt" else os.getuid()
    if expected_account != current_account:
        raise PmtError("service_account_mismatch", "Run the Host as the configured service account")
    if not (data_root / "pmt.sqlite3").is_file() or not (Path(config_root) / "profile.json").is_file():
        raise PmtError("host_schema_unsupported", "Initialized Host database and profile are required before serve")
    for directory in (Path(config_root), *map(Path, config["paths"].values())):
        if not directory.is_dir() or not _runtime_path_safe(directory) or not os.access(directory, os.R_OK | os.W_OK):
            raise PmtError("path_unsafe", "Configured Host path is unavailable or unsafe")
    keys = {}
    primary_id = config["claim_key"]["key_id"]
    keys[primary_id] = read_key(config["claim_key"]["source"], account=configured_account)
    for item in config["claim_key"].get("retained", []):
        keys[item["key_id"]] = read_key(item["source"], account=configured_account)
    if listen:
        host, port = _parse_listen(listen)
    else:
        host, port = config["listen"]["host"], config["listen"]["port"]
    address = ipaddress.ip_address(host)
    proxy_enabled = config["proxy"]["enabled"]
    trusted = config["proxy"]["trusted"]
    tls_configured = bool(config["tls"].get("cert_file"))
    if tls_configured:
        check_tls(config)
    elif not (address.is_loopback and (allow_loopback_http or proxy_enabled and trusted)):
        raise PmtError("host_tls_required", "TLS is required; plain HTTP needs explicit loopback test mode")
    if proxy_enabled and (not address.is_loopback or not trusted):
        raise PmtError("host_proxy_invalid", "Proxy mode requires a loopback bind and trusted proxy addresses")

    lock = data_root / ".pmt-server.lock"
    with instance_lock(lock):
        # The database and Host classes are constructed only after read-only preflight and lock.
        db = Database(root=data_root, config_root=config_root)
        from ..host.cli import serve_host as legacy_serve
        from argparse import Namespace
        args = Namespace(host=host, port=port, ssl_certfile=config["tls"].get("cert_file"),
                         ssl_keyfile=config["tls"].get("key_file"), allow_loopback_http=allow_loopback_http,
                         behind_proxy=proxy_enabled, trusted_proxy=trusted, claim_key_id=primary_id,
                         claim_key_env="PMT_SERVER_CLAIM_KEY", retained_key=[])
        log_config = server_log_config(config["paths"]["log_dir"], config["logging"]["retain_days"], config["logging"]["level"])
        return legacy_serve(db, args, claim_keys=keys, log_config=log_config)
