"""TLS certificate validation and non-destructive registration."""
from __future__ import annotations

import hashlib
import os
import ssl
import stat
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from ..errors import PmtError
from .config import _process_lock, _publish_config_locked, config_path, load_config_snapshot
from .secrets import _account_uid, _check_file_acl, _check_windows_acl, _path_reparse, _read_key_file, _set_windows_acl


def _read_regular(path, *, private=False):
    path = Path(path)
    try:
        info = path.lstat()
        if _path_reparse(path) or path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400 or not stat.S_ISREG(info.st_mode):
            raise PmtError("host_tls_invalid", "TLS source must be a regular non-reparse file")
        if info.st_size <= 0 or info.st_size > 1024 * 1024:
            raise PmtError("host_tls_invalid", "TLS source file has an invalid size")
        if private and os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise PmtError("host_tls_invalid", "TLS private-key source permissions are too broad")
        return _read_key_file(path)
    except PmtError:
        raise
    except OSError as exc:
        raise PmtError("host_tls_invalid", "TLS source file is unavailable") from exc


def _validate_pair(cert_path, key_path, ca_path, public_url):
    """Use OpenSSL through Python's SSL implementation for pair, chain and SAN checks."""
    cert = Path(cert_path)
    key = Path(key_path)
    try:
        server = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server.load_cert_chain(str(cert), str(key))
        client = ssl.create_default_context(cafile=str(ca_path) if ca_path else None)
        client.check_hostname = True
        hostname = urlparse(public_url).hostname
        if not hostname:
            raise ValueError("missing public hostname")
        client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
        client_side = client.wrap_bio(client_in, client_out, server_side=False, server_hostname=hostname)
        server_side = server.wrap_bio(server_in, server_out, server_side=True)
        client_done = server_done = False
        for _ in range(100):
            for endpoint, done_name in ((client_side, "client"), (server_side, "server")):
                try:
                    endpoint.do_handshake()
                    if done_name == "client": client_done = True
                    else: server_done = True
                except (ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    pass
            outgoing = client_out.read()
            if outgoing: server_in.write(outgoing)
            outgoing = server_out.read()
            if outgoing: client_in.write(outgoing)
            if client_done and server_done:
                break
        else:
            raise ssl.SSLError("TLS validation handshake did not complete")
        peer = client_side.getpeercert()
        not_after = ssl.cert_time_to_seconds(peer["notAfter"])
        expires = datetime.fromtimestamp(not_after, timezone.utc)
        remaining_days = (expires - datetime.now(timezone.utc)).total_seconds() / 86400
        return {"ok": True, "expires_at": expires.isoformat(), "days_remaining": round(remaining_days, 2),
                "warning": "Certificate expires within 30 days" if remaining_days <= 30 else None}
    except PmtError:
        raise
    except (OSError, ValueError, ssl.SSLError, KeyError, TypeError) as exc:
        raise PmtError("host_tls_invalid", "TLS certificate, private key, chain, or public URL SAN is invalid") from exc


def check_tls(config):
    tls = config["tls"]
    if not tls.get("cert_file"):
        proxy_ok = (config["proxy"]["enabled"] and
                    config["listen"]["host"] in {"127.0.0.1", "::1"} and bool(config["proxy"]["trusted"]))
        loopback_only = config["listen"]["host"] in {"127.0.0.1", "::1"}
        if proxy_ok or loopback_only:
            return {"ok": True, "configured": False, "warning": "TLS is terminated by a trusted proxy or omitted for loopback-only use"}
        raise PmtError("host_tls_invalid", "TLS certificate and private key are required for this listener")
    cert, key, ca = tls["cert_file"], tls["key_file"], tls.get("ca_file")
    for path in (cert, key, ca):
        if path and _path_reparse(path):
            raise PmtError("host_tls_invalid", "TLS paths cannot contain symlinks or reparse points")
    if os.name == "nt":
        _check_windows_acl(key, config["service"]["account"])
        _check_windows_acl(Path(key).parent, config["service"]["account"])
    else:
        _check_file_acl(key, config["service"]["account"])
    result = _validate_pair(cert, key, ca, config["public_url"])
    result["configured"] = True
    return result


def register_tls(config_root, cert_source, key_source, ca_source=None, *, apply=False):
    cert_bytes = _read_regular(cert_source)
    key_bytes = _read_regular(key_source, private=True)
    ca_bytes = _read_regular(ca_source) if ca_source else None
    cert_hash, key_hash = hashlib.sha256(cert_bytes).hexdigest(), hashlib.sha256(key_bytes).hexdigest()
    ca_hash = hashlib.sha256(ca_bytes).hexdigest() if ca_bytes else None
    root = Path(config_root)
    if not apply:
        config, _ = load_config_snapshot(config_path(root))
        targets = _tls_targets(root, cert_hash, key_hash, ca_hash)
        # Validate the caller's material before presenting a dry-run plan.
        _validate_pair(cert_source, key_source, ca_source, config["public_url"])
        return {"ok": True, "applied": False, "paths": {key: str(value) for key, value in targets.items()}}

    # Config writers and TLS registration share this lock from snapshot through publish.
    with _process_lock(root / ".host-config.lock"):
        config, digest = load_config_snapshot(config_path(root))
        return _apply_tls_locked(root, config, digest, cert_bytes, key_bytes, ca_bytes,
                                 cert_hash, key_hash, ca_hash)


def _tls_targets(root, cert_hash, key_hash, ca_hash):
    tls_dir = Path(root) / "tls"
    stem = f"host-{cert_hash}-{key_hash}"
    targets = {"cert_file": tls_dir / f"{stem}.crt", "key_file": tls_dir / f"{stem}.key"}
    if ca_hash is not None: targets["ca_file"] = tls_dir / f"ca-{ca_hash}.crt"
    return targets


def _apply_tls_locked(root, config, digest, cert_bytes, key_bytes, ca_bytes, cert_hash, key_hash, ca_hash):
    tls_dir = Path(root) / "tls"
    targets = _tls_targets(root, cert_hash, key_hash, ca_hash)
    staged = {"cert_file": cert_bytes, "key_file": key_bytes}
    if ca_bytes is not None: staged["ca_file"] = ca_bytes

    account = config["service"]["account"]
    created = []
    tls_dir_created = False
    try:
        if not tls_dir.exists():
            tls_dir.mkdir(parents=True, exist_ok=False)
            tls_dir_created = True
            if os.name == "nt": _set_windows_acl(tls_dir, account)
            else:
                os.chmod(tls_dir, 0o700)
                uid = _account_uid(account)
                if uid != os.getuid():
                    if os.geteuid() != 0: raise PmtError("host_account_invalid", "TLS directory requires service-account ownership")
                    os.chown(tls_dir, uid, -1)
        elif os.name == "nt":
            _check_windows_acl(tls_dir, account)
        else:
            info = tls_dir.stat()
            if info.st_uid != _account_uid(account) or stat.S_IMODE(info.st_mode) & 0o077:
                raise PmtError("host_tls_invalid", "TLS directory must be private to the service account")
        for name, content in staged.items():
            target = targets[name]
            if target.exists():
                if hashlib.sha256(_read_regular(target, private=name == "key_file")).hexdigest() != hashlib.sha256(content).hexdigest():
                    raise PmtError("host_tls_invalid", "TLS destination collision; existing file preserved")
                if name == "key_file":
                    if os.name == "nt": _check_windows_acl(target, account)
                    else: _check_file_acl(target, account)
                continue
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600 if name == "key_file" else 0o644)
            info = os.fstat(fd)
            created.append((target, hashlib.sha256(content).hexdigest(), (info.st_dev, info.st_ino)))
            with os.fdopen(fd, "wb") as stream:
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
            if os.name == "nt":
                _set_windows_acl(target, account)
            elif name == "key_file":
                os.chmod(target, 0o600)
                uid = _account_uid(account)
                if uid != os.getuid():
                    if os.geteuid() != 0: raise PmtError("host_account_invalid", "TLS key requires service-account ownership")
                    os.chown(target, uid, -1)
        tls_values = {name: str(path) for name, path in targets.items()}
        candidate = {**config, "tls": tls_values, "revision": config["revision"] + 1}
        _validate_pair(targets["cert_file"], targets["key_file"], targets.get("ca_file"), config["public_url"])
        if config["tls"] == tls_values:
            return {"ok": True, "applied": True, "unchanged": True, "revision": config["revision"], "paths": tls_values}
        _publish_config_locked(root, candidate, digest)
        return {"ok": True, "applied": True, "revision": candidate["revision"], "paths": tls_values}
    except Exception:
        for path, expected, identity in reversed(created):
            try:
                info = path.lstat()
                if (stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == identity
                        and hashlib.sha256(_read_key_file(path)).hexdigest() == expected):
                    path.unlink()
            except OSError:
                pass
        if tls_dir_created:
            try: tls_dir.rmdir()
            except OSError: pass
        raise
