"""Secret-free client provenance and launcher metadata."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from ..errors import PmtError
from ..storage_config import _read_profile
from .credentials import has_credential_store

_SOURCES = {"plugin", "connect", "legacy"}


def read_client_metadata(config_root):
    path = Path(config_root) / "client.json"
    try:
        raw = path.read_bytes()
        if len(raw) > 4096:
            return None
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("source") not in _SOURCES
            or value.get("last_mode") not in {"local", "hosted"}
            or not isinstance(value.get("python_path"), str)):
        return None
    return value


def has_client_metadata(config_root):
    return (Path(config_root) / "client.json").exists()


def is_legacy_root(config_root):
    """Return true only for a marked legacy root or an existing unmarked profile."""
    metadata = read_client_metadata(config_root)
    if metadata is not None:
        return metadata["source"] == "legacy"
    if has_client_metadata(config_root):
        return False
    try:
        profile, _digest = _read_profile(config_root)
    except PmtError:
        return False
    if profile and profile.get("mode") == "hosted" and has_credential_store(config_root):
        return False
    return profile is not None


def write_client_metadata(config_root, *, source, python_path, mode):
    if source not in _SOURCES or mode not in {"local", "hosted"}:
        raise PmtError("client_metadata_invalid", "Client metadata values are invalid")
    if not isinstance(python_path, str) or not python_path:
        raise PmtError("client_metadata_invalid", "Client interpreter path is missing")
    root = Path(config_root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / "client.json"
    metadata = {}
    try:
        prior_wire = path.read_bytes()
        if len(prior_wire) > 4096:
            prior_wire = b""
        prior = json.loads(prior_wire.decode("utf-8"))
        if isinstance(prior, dict):
            metadata = {key: value for key, value in prior.items()
                        if key not in {"schema_version", "source", "python_path", "last_mode"}
                        and not any(word in key.casefold() for word in ("credential", "token", "secret", "password"))}
    except (OSError, UnicodeError, ValueError):
        pass
    metadata.update({"schema_version": 1, "source": source, "python_path": python_path, "last_mode": mode})
    wire = (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    fd, temporary = tempfile.mkstemp(prefix=".client-", suffix=".tmp", dir=str(root))
    try:
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(wire)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    return path
