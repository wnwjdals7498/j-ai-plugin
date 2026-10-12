"""Strict Host configuration parsing and cross-process compare-and-swap."""
from __future__ import annotations

import base64
import contextlib
import hashlib
import os
import re
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from ..errors import PmtError
from ..util import canonical_json, strict_json_loads


def _keys(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        raise PmtError("config_invalid", "Host configuration fields are invalid")


def _string(value, name):
    if not isinstance(value, str) or not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise PmtError("config_invalid", f"{name} must be a non-empty string")
    if re.search(r"(?i)(password|token|credential|secret|private[_ -]?key)\s*[:=]", value):
        raise PmtError("config_secret_rejected", "Secret values cannot be stored in host-config.json")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        decoded = b""
    if len(decoded) >= 32:
        raise PmtError("config_secret_rejected", "Secret values cannot be stored in host-config.json")
    return value


def validate_config(config):
    _keys(config, {"schema_version", "revision", "paths", "listen", "public_url", "tls", "claim_key", "proxy", "access", "service", "logging", "registry"})
    if type(config["schema_version"]) is not int or config["schema_version"] != 1:
        raise PmtError("config_invalid", "Unsupported host configuration schema")
    if type(config["revision"]) is not int or config["revision"] < 1:
        raise PmtError("config_invalid", "revision must be a positive integer")
    _keys(config["paths"], {"data_root", "log_dir", "backup_dir"})
    for key, value in config["paths"].items():
        _string(value, f"paths.{key}")
        _validate_path_reference(value, f"paths.{key}")
        _require_absolute_path(value, f"paths.{key}")
    _keys(config["listen"], {"host", "port"})
    host = _string(config["listen"]["host"], "listen.host")
    import ipaddress
    try:
        ipaddress.ip_address(host)
    except ValueError as exc:
        raise PmtError("config_invalid", "listen.host must be a literal IP address") from exc
    if type(config["listen"]["port"]) is not int or not 1 <= config["listen"]["port"] <= 65535:
        raise PmtError("config_invalid", "listen.port must be between 1 and 65535")
    public_url = _string(config["public_url"], "public_url")
    try:
        parsed_url = urlparse(public_url)
        hostname = parsed_url.hostname
        _ = parsed_url.port
    except ValueError as exc:
        raise PmtError("config_invalid", "public_url port is invalid") from exc
    if parsed_url.scheme != "https" or not hostname or parsed_url.username or parsed_url.password or parsed_url.path not in ("", "/") or parsed_url.query or parsed_url.fragment:
        raise PmtError("config_invalid", "public_url must be a credential-free HTTPS origin")
    _keys(config["tls"], set(), {"cert_file", "key_file", "ca_file"})
    if ("cert_file" in config["tls"]) != ("key_file" in config["tls"]):
        raise PmtError("config_invalid", "TLS certificate and key must be configured together")
    for key, value in config["tls"].items():
        _string(value, f"tls.{key}")
        _validate_path_reference(value, f"tls.{key}", allow_credentials=True)
        if not value.startswith("${CREDENTIALS_DIRECTORY}"):
            _require_absolute_path(value, f"tls.{key}")
    _keys(config["claim_key"], {"key_id", "source"}, {"retained"})
    key_id = _string(config["claim_key"]["key_id"], "claim_key.key_id")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_id):
        raise PmtError("config_invalid", "claim_key.key_id has an unsafe format")
    _validate_source(config["claim_key"]["source"])
    retained = config["claim_key"].get("retained", [])
    if not isinstance(retained, list):
        raise PmtError("config_invalid", "claim_key.retained must be a list")
    ids = {config["claim_key"]["key_id"]}
    for item in retained:
        _keys(item, {"key_id", "source"})
        key_id = _string(item["key_id"], "claim_key.retained.key_id")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_id):
            raise PmtError("config_invalid", "retained claim key id has an unsafe format")
        if key_id in ids:
            raise PmtError("config_invalid", "Claim key ids must be unique")
        ids.add(key_id)
        _validate_source(item["source"])
    _keys(config["proxy"], {"enabled", "trusted"})
    if type(config["proxy"]["enabled"]) is not bool or not isinstance(config["proxy"]["trusted"], list):
        raise PmtError("config_invalid", "proxy fields are invalid")
    for item in config["proxy"]["trusted"]:
        _string(item, "proxy.trusted")
        _validate_network(item, "proxy.trusted")
    _keys(config["access"], {"allowed_sources"})
    if not isinstance(config["access"]["allowed_sources"], list):
        raise PmtError("config_invalid", "access.allowed_sources must be a list")
    for item in config["access"]["allowed_sources"]:
        _string(item, "access.allowed_sources")
        _validate_network(item, "access.allowed_sources")
    _keys(config["service"], {"kind", "name", "account", "app_root"})
    if not isinstance(config["service"]["kind"], str) or config["service"]["kind"] not in {"windows-task", "systemd", "none"}:
        raise PmtError("config_invalid", "service.kind is invalid")
    for key in ("name", "account", "app_root"):
        _string(config["service"][key], f"service.{key}")
    _validate_path_reference(config["service"]["app_root"])
    _require_absolute_path(config["service"]["app_root"], "service.app_root")
    _keys(config["logging"], {"level", "retain_days"})
    if not isinstance(config["logging"]["level"], str) or config["logging"]["level"] not in {"debug", "info", "warning", "error"} or type(config["logging"]["retain_days"]) is not int or config["logging"]["retain_days"] < 1:
        raise PmtError("config_invalid", "logging fields are invalid")
    _keys(config["registry"], {"projects"})
    if not isinstance(config["registry"]["projects"], list):
        raise PmtError("config_invalid", "registry.projects must be a list")
    for project in config["registry"]["projects"]:
        _keys(project, {"name", "project_id", "repositories"})
        _string(project["name"], "registry project name")
        _string(project["project_id"], "registry project_id")
        _canonical_uuid(project["project_id"], "project_id")
        if not isinstance(project["repositories"], list):
            raise PmtError("config_invalid", "project.repositories must be a list")
        for repo in project["repositories"]:
            _keys(repo, {"name", "repository_id", "remote", "graph_path"})
            for key, value in repo.items():
                _string(value, f"repository.{key}")
            _canonical_uuid(repo["repository_id"], "repository_id")
            _validate_remote(repo["remote"])
            _validate_graph_path(repo["graph_path"])
    return config


def _validate_source(source):
    if not isinstance(source, dict) or not isinstance(source.get("kind"), str) or source.get("kind") not in {"env", "file", "dpapi"}:
        raise PmtError("config_invalid", "claim key source kind is invalid")
    kind = source["kind"]
    _keys(source, {"kind", "name"} if kind == "env" else {"kind", "path"})
    _string(source["name" if kind == "env" else "path"], "claim key source reference")
    if kind != "env":
        _validate_path_reference(source["path"], "claim key source", allow_credentials=True)
    if kind == "env" and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", source["name"]):
        raise PmtError("config_invalid", "claim key env source must be an environment variable name")


def _validate_path_reference(value, label="path", *, allow_credentials=False):
    remainder = value.replace("${CREDENTIALS_DIRECTORY}", "") if allow_credentials else value
    if "~" in value or "$" in remainder:
        raise PmtError("config_invalid", f"{label} contains an unsupported path variable")
    if allow_credentials and value.count("${CREDENTIALS_DIRECTORY}") > 1:
        raise PmtError("config_invalid", f"{label} contains an invalid path variable")
    if not allow_credentials and "${" in value:
        raise PmtError("config_invalid", f"{label} contains an unsupported path variable")


def _require_absolute_path(value, label):
    from pathlib import PureWindowsPath
    if not (Path(value).is_absolute() or PureWindowsPath(value).is_absolute()):
        raise PmtError("config_invalid", f"{label} must be absolute")


def _validate_network(value, label):
    import ipaddress
    try:
        ipaddress.ip_network(value, strict=False) if "/" in value else ipaddress.ip_address(value)
    except ValueError as exc:
        raise PmtError("config_invalid", f"{label} must contain an IP address or CIDR") from exc


def _canonical_uuid(value, label):
    import uuid
    try:
        if str(uuid.UUID(value)) != value: raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("config_invalid", f"{label} must be a canonical UUID") from exc


def _validate_remote(value):
    parsed = urlparse(value)
    try:
        _ = parsed.port
        hostname = parsed.hostname
    except ValueError as exc: raise PmtError("config_invalid", "Repository remote port is invalid") from exc
    if parsed.scheme not in {"ssh", "http", "https", "git"} or not hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise PmtError("config_invalid", "Repository remote must be an absolute credential-free URL")


def _validate_graph_path(value):
    from pathlib import PurePosixPath, PureWindowsPath
    path = PurePosixPath(value)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or ":" in value or any(part in {"..", "."} for part in value.split("/")) or "\\" in value or not value:
        raise PmtError("config_invalid", "graph_path must be a safe repository-relative path")


def load_config(path):
    try:
        value, _ = load_config_snapshot(path)
        return value
    except PmtError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise PmtError("config_invalid", "Host configuration cannot be read") from exc


def load_config_snapshot(path):
    try:
        path = Path(path)
        if _path_has_reparse(path.parent):
            raise PmtError("config_invalid", "Host config root cannot contain symlinks or reparse points")
        info = path.lstat()
        if path.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise PmtError("config_invalid", "Host configuration cannot use symlinks or reparse points")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        value = strict_json_loads(raw, max_bytes=1024 * 1024)
        return validate_config(value), hashlib.sha256(raw).hexdigest()
    except PmtError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise PmtError("config_invalid", "Host configuration cannot be read") from exc


def _path_has_reparse(path):
    for candidate in (Path(path), *Path(path).parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if candidate.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            return True
    return False


@contextlib.contextmanager
def _process_lock(lock_path):
    """Exclusive byte/file lock, shared by processes on Windows and POSIX."""
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = open(path, "a+b")
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"0"); stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            locked = True
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            locked = True
        yield
    finally:
        if locked and os.name == "nt":
            import msvcrt
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        elif locked:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        stream.close()


def config_path(root):
    return Path(root) / "host-config.json"


def config_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def publish_config(root, config, expected_sha256=None, *, create=False):
    root = Path(root)
    if _path_has_reparse(root):
        raise PmtError("config_invalid", "Host config root cannot contain symlinks or reparse points")
    root.mkdir(parents=True, exist_ok=True)
    path = config_path(root)
    validate_config(config)
    with _process_lock(root / ".host-config.lock"):
        return _publish_config_locked(root, config, expected_sha256, create=create)


def _publish_config_locked(root, config, expected_sha256=None, *, create=False):
    """Publish while caller holds the root lock (used by transactional init)."""
    root = Path(root)
    path = config_path(root)
    exists = path.exists()
    if create and exists:
        raise PmtError("config_exists", "Host configuration already exists")
    if expected_sha256 is not None:
        if not exists:
            raise PmtError("config_conflict", "Host configuration changed; reload and retry")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise PmtError("config_invalid", "Host configuration exceeds the size limit")
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise PmtError("config_conflict", "Host configuration changed; reload and retry")
        current = validate_config(strict_json_loads(raw, max_bytes=1024 * 1024))
        if config["revision"] != current["revision"] + 1:
            raise PmtError("config_conflict", "Host configuration revision changed")
    elif exists and not create:
        raise PmtError("config_exists", "Host configuration already exists")
    fd, name = tempfile.mkstemp(prefix="host-config-", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical_json(config).encode("utf-8")); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != "nt": os.chmod(path, 0o600)
    finally:
        try: os.unlink(name)
        except FileNotFoundError: pass
    return path
