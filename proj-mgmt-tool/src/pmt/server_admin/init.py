"""Dry-run-first Host initialization and read-only adoption planning."""
from __future__ import annotations

import ipaddress
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from urllib.parse import urlparse

from ..db import Database
from ..errors import PmtError
from ..host.auth import AuthRegistry, HOST_SCHEMA_VERSION
from ..util import canonical_json, new_id
from .config import _process_lock, _publish_config_locked, config_path, publish_config, validate_config
from .secrets import _path_reparse, create_key_reference, validate_service_account


def _listen(value):
    try:
        host, port_text = value.rsplit(":", 1)
        ipaddress.ip_address(host)
        port = int(port_text)
        if not 1 <= port <= 65535:
            raise ValueError
        return host, port
    except ValueError as exc:
        raise PmtError("config_invalid", "listen must be a literal IP:port") from exc


def _validate_public_url(value):
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError as exc:
        raise PmtError("config_invalid", "public-url is invalid") from exc
    try:
        _ = parsed.port
    except ValueError as exc:
        raise PmtError("config_invalid", "public-url port is invalid") from exc
    if parsed.scheme != "https" or not hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise PmtError("config_invalid", "public-url must be an HTTPS origin without credentials or path")


def _paths(args):
    data = args.data_root or str(Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "PMT" / "host-data")
    log = args.log_dir or str(Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "PMT" / "logs")
    backup = args.backup_dir or str(Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "PMT" / "backup")
    return {"data_root": data, "log_dir": log, "backup_dir": backup}


def _base_config(args, source):
    _validate_public_url(args.public_url)
    host, port = _listen(args.listen)
    service_kind = args.service or "none"
    service_account = args.account or ("current" if service_kind == "none" else "pmt-host")
    app_root = args.app_root or _default_app_root()
    config = {
        "schema_version": 1, "revision": 1,
        "paths": _paths(args), "listen": {"host": host, "port": port},
        "public_url": args.public_url,
        "tls": {key: value for key, value in (("cert_file", args.tls_cert), ("key_file", args.tls_key), ("ca_file", args.tls_ca)) if value},
        "claim_key": {"key_id": args.claim_key_id, "source": source, "retained": []},
        "proxy": {"enabled": False, "trusted": []},
        "access": {"allowed_sources": args.allow or []},
        "service": {"kind": service_kind, "name": "PMT Host", "account": service_account, "app_root": str(app_root)},
        "logging": {"level": "info", "retain_days": 30},
        "registry": {"projects": []},
    }
    if args.retained:
        for item in args.retained:
            try:
                key_id, env_name = item.split("=", 1)
            except ValueError as exc:
                raise PmtError("config_invalid", "retained references must be KEY_ID=ENV_NAME") from exc
            config["claim_key"]["retained"].append({"key_id": key_id, "source": {"kind": "env", "name": env_name}})
    return validate_config(config)


def _default_app_root():
    executable_dir = Path(sys.executable).parent
    if executable_dir.name.lower() in {"scripts", "bin"} and executable_dir.parent.name.lower() == "venv":
        return executable_dir.parent.parent
    return executable_dir


def _assert_fresh(config_root, data_root, other_roots=()):
    config_root, data_root = Path(config_root), Path(data_root)
    roots = (config_root, data_root, *map(Path, other_roots))
    if any(_path_reparse(root) or _path_reparse(root.parent) for root in roots):
        raise PmtError("path_unsafe", "Initialization paths cannot contain symlinks or reparse points")
    if config_path(config_root).exists():
        raise PmtError("config_exists", "Host configuration already exists")
    for root in roots:
        entries = [item for item in root.iterdir() if item.name != ".host-config.lock"] if root.exists() else []
        if entries:
            raise PmtError("init_state_exists", "Initialization target contains existing files; preserved")
    if (data_root / "pmt.sqlite3").exists():
        raise PmtError("init_state_exists", "Initialization target already contains Host state; preserved")


def _adopt_metadata(data_root, db_config_root):
    data_root, db_config_root = Path(data_root), Path(db_config_root)
    profile_path = db_config_root / "profile.json"
    db_path = data_root / "pmt.sqlite3"
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        import uuid
        environment_id = profile["environment_id"]
        if str(uuid.UUID(environment_id)) != environment_id:
            raise ValueError
        with sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True) as conn:
            meta = dict(conn.execute("SELECT key, value FROM meta WHERE key IN ('host_schema_version','host_namespace_id','schema_version')"))
            schema = int(meta["schema_version"])
            if schema != 5 or meta.get("host_schema_version") != str(HOST_SCHEMA_VERSION):
                raise PmtError("host_schema_unsupported", "Existing Host database schema is unsupported")
            namespace_id = meta["host_namespace_id"]
            if str(uuid.UUID(namespace_id)) != namespace_id:
                raise ValueError
        return {"environment_id": environment_id, "namespace_id": namespace_id}
    except PmtError:
        raise
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, json.JSONDecodeError) as exc:
        raise PmtError("adopt_state_invalid", "Existing Host profile or database metadata is invalid; preserved") from exc


def init_host(args, config_root):
    config_root = Path(config_root)
    paths = _paths(args)
    if args.adopt:
        if not args.claim_key_env:
            raise PmtError("config_invalid", "--adopt requires --claim-key-env")
        _validate_public_url(args.public_url)
        metadata = _adopt_metadata(paths["data_root"], args.host_config_root or config_root)
        source = {"kind": "env", "name": args.claim_key_env}
        config = _base_config(args, source)
        if args.apply:
            # Only publish the declaration; existing DB, profile and referenced key are untouched.
            publish_config(config_root, config, create=True)
        return {"ok": True, "mode": "adopt", "applied": bool(args.apply), "config_path": str(config_path(config_root)), **metadata,
                "preserved": ["database", "profile", "namespace", "devices", "claim-key references"]}

    key_kind = "dpapi" if os.name == "nt" else "file"
    suffix = ".dpapi" if key_kind == "dpapi" else ".key"
    config = _base_config(args, {"kind": key_kind, "path": str(Path(config_root) / "secrets" / f"claim-{args.claim_key_id}{suffix}")})
    roots = {"config": config_root, **paths}
    resolved = {name: Path(value).resolve() for name, value in roots.items()}
    if len(set(resolved.values())) != len(resolved) or any(a in b.parents or b in a.parents for i, a in enumerate(resolved.values()) for b in list(resolved.values())[i+1:]):
        raise PmtError("config_invalid", "Initialization paths must be separate")
    result = {"ok": True, "mode": "init", "applied": False, "config_path": str(config_path(config_root)), "planned_paths": [str(config_root), *paths.values()]}
    if not args.apply:
        return result
    namespace_id = _apply_init(config_root, paths, config, args.claim_key_id)
    result.update({"applied": True, "namespace_id": namespace_id, "config_path": str(config_path(config_root)), "next": ["pmt-server doctor", "pmt-server serve"]})
    return result


def _apply_init(config_root, paths, config, key_id):
    config_root = Path(config_root)
    service_account = config["service"]["account"]
    lock_path = config_root / ".host-config.lock"
    created_paths = []
    key_path = None
    key_digest = None
    profile_path = config_root / "profile.json"
    profile_bytes = None
    db_path = Path(paths["data_root"]) / "pmt.sqlite3"
    db_digest = None
    db_reserved = False
    secrets_dir = config_root / "secrets"
    secrets_dir_preexisting = secrets_dir.exists()
    with _process_lock(lock_path):
        try:
            _assert_fresh(config_root, paths["data_root"], (paths["log_dir"], paths["backup_dir"]))
            if (config["service"]["kind"] == "windows-task") != (os.name == "nt") and config["service"]["kind"] != "none":
                raise PmtError("service_unsupported", "Configured service kind is unsupported on this operating system")
            validate_service_account(service_account)
            for directory in (config_root, *paths.values()):
                directory = Path(directory)
                if not directory.exists():
                    directory.mkdir(parents=True, exist_ok=False)
                    created_paths.append(directory)
            if not secrets_dir_preexisting:
                # Claim ownership before the helper runs: it may create this directory and
                # then fail while applying account ACLs before returning a key reference.
                created_paths.append(secrets_dir)
            key = create_key_reference(config_root, key_id, account=service_account)
            config["claim_key"]["source"] = key
            key_path = Path(key["path"])
            key_digest = _sha256_file(key_path)
            environment_id = new_id()
            profile_bytes = canonical_json({"environment_id": environment_id}).encode("utf-8")
            fd = os.open(profile_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(profile_bytes); stream.flush(); os.fsync(stream.fileno())
            fd = os.open(db_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            db_reserved = True
            db = Database(root=paths["data_root"], config_root=config_root)
            registry = AuthRegistry(db)
            db_digest = _sha256_file(db_path)
            _publish_config_locked(config_root, config, create=True)
            return registry.namespace_id
        except Exception as exc:
            _rollback_init(key_path, key_digest, profile_path, profile_bytes, db_path, db_digest, db_reserved, created_paths)
            if isinstance(exc, PmtError):
                raise
            raise PmtError("init_failed", "Host initialization failed; newly created state was rolled back where ownership was verified") from exc


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rollback_init(key_path, key_digest, profile_path, profile_bytes, db_path, db_digest, db_reserved, created_paths):
    if key_path is not None and key_digest is not None:
        try:
            if _sha256_file(key_path) == key_digest: key_path.unlink()
        except OSError: pass
    if profile_bytes is not None:
        try:
            if profile_path.read_bytes() == profile_bytes: profile_path.unlink()
        except OSError: pass
    if db_reserved and db_digest is not None:
        try:
            if _sha256_file(db_path) == db_digest: db_path.unlink()
        except OSError: pass
    if db_reserved and db_digest is not None:
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db_path) + suffix)
            try:
                if sidecar.exists() and sidecar.stat().st_size == 0: sidecar.unlink()
            except OSError: pass
    for directory in reversed(created_paths):
        try: directory.rmdir()
        except OSError: pass
