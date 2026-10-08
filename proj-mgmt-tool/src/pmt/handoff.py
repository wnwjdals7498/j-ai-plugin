"""Strict, secret-free pmt-handoff/v1 documents shared by client and server."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
from pathlib import Path
import re
import ssl
from urllib.parse import urlsplit
import uuid

from .errors import PmtError
from .http_store import _validate_endpoint
from .util import canonical_json, strict_json_loads, utc_now

FORMAT = "pmt-handoff"
VERSION = 1
PLUGIN_VERSION = "0.5.0"
COMPATIBILITY = {"core": "0.4", "db_schema": 5, "graph_schema": 1, "protocol": [1]}
_PERMISSIONS = {"read", "write", "runtime", "review", "admin"}
_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


def _invalid(message="Handoff document is invalid"):
    raise PmtError("handoff_invalid", message)


def _object(value, required, optional=()):
    if not isinstance(value, dict) or set(value) - set(required) - set(optional) or set(required) - set(value):
        _invalid("Handoff fields do not match the supported schema")
    return value


def _text(value, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or any(ord(c) < 32 or ord(c) == 127 for c in value):
        _invalid("Handoff text must be bounded and contain no control characters")
    return value


def _uuid(value):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, AttributeError):
        _invalid("Handoff identifiers must be canonical UUIDs")


def _array(value, maximum=100):
    if not isinstance(value, list) or len(value) > maximum:
        _invalid("Handoff lists must be bounded")
    return value


def _remote(value):
    _text(value, 4096)
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        _invalid("Repository remote is invalid")
    if parts.scheme not in {"https", "http", "ssh", "git"} or not parts.hostname or parts.password is not None:
        _invalid("Repository remote must be a credential-free absolute URL")
    if parts.username is not None and not (parts.scheme == "ssh" and parts.username == "git"):
        _invalid("Repository remote must not contain credentials")
    if parts.query or parts.fragment or "\\" in value or (port is not None and not 1 <= port <= 65535):
        _invalid("Repository remote is invalid")


def validate_handoff(value, *, allow_loopback_http=False):
    """Return an isolated validated dict; errors never echo input values."""
    doc = _object(value, ("format", "version", "host", "namespace_id", "device"),
                  ("issued_at", "issuer", "projects"))
    if doc["format"] != FORMAT or type(doc["version"]) is not int or doc["version"] != VERSION:
        raise PmtError("handoff_version_unsupported", "Unsupported handoff format or version")
    if "issued_at" in doc:
        try:
            stamp = datetime.fromisoformat(_text(doc["issued_at"], 64).replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                _invalid("Handoff timestamp must include a timezone")
        except ValueError:
            _invalid("Handoff timestamp is invalid")
    if "issuer" in doc:
        issuer = _object(doc["issuer"], ("tool", "version"))
        if issuer["tool"] != "pmt-server":
            _invalid("Handoff issuer is invalid")
        _text(issuer["version"], 64)
    host = _object(doc["host"], ("url", "compatibility"), ("ca_pem", "ca_sha256"))
    try:
        _text(host["url"], 4096)
        _validate_endpoint(host["url"], allow_loopback_http)
    except ValueError:
        _invalid("Host endpoint must be a safe HTTPS URL")
    compatibility = _object(host["compatibility"], tuple(COMPATIBILITY))
    if (compatibility["core"] != "0.4" or type(compatibility["db_schema"]) is not int
            or compatibility["db_schema"] != 5 or type(compatibility["graph_schema"]) is not int
            or compatibility["graph_schema"] != 1 or compatibility["protocol"] != [1]
            or not isinstance(compatibility["protocol"], list)
            or any(type(v) is not int for v in compatibility["protocol"])):
        raise PmtError("incompatible", "Handoff requires unsupported Core, schema or protocol")
    if ("ca_pem" in host) != ("ca_sha256" in host):
        _invalid("CA certificate and digest must be supplied together")
    if "ca_pem" in host:
        pem = host["ca_pem"]
        digest = host["ca_sha256"]
        if (not isinstance(pem, str) or not pem or len(pem.encode("utf-8")) > 65536
                or not isinstance(digest, str) or not _HASH.fullmatch(digest)):
            _invalid("CA certificate or digest is invalid")
        if hashlib.sha256(pem.encode("utf-8")).hexdigest() != digest:
            raise PmtError("handoff_ca_mismatch", "Handoff CA certificate digest does not match")
        if "PRIVATE KEY" in pem:
            _invalid("Handoff must contain public certificates only")
        try:
            ssl.create_default_context(cadata=pem)
        except (ssl.SSLError, ValueError):
            _invalid("Handoff CA certificate is invalid")
    _uuid(doc["namespace_id"])
    device = _object(doc["device"], ("device_id", "actor", "permissions", "scopes", "credential"))
    _uuid(device["device_id"])
    _text(device["actor"])
    permissions = _array(device["permissions"])
    if (not permissions or any(not isinstance(p, str) or p not in _PERMISSIONS for p in permissions)
            or len(set(permissions)) != len(permissions)):
        _invalid("Device permissions are invalid")
    scopes = _array(device["scopes"])
    if not scopes:
        _invalid("Device needs at least one scope")
    for scope in scopes:
        _uuid(scope)
    if len(set(scopes)) != len(scopes):
        _invalid("Device scopes must be distinct")
    credential = _object(device["credential"], ("delivery", "env"))
    if credential["delivery"] != "separate" or not isinstance(credential["env"], str) or not _ENV.fullmatch(credential["env"]):
        _invalid("Credential must be delivered separately using an environment-variable reference")
    seen_projects = set()
    seen_repositories = set()
    for project in _array(doc.get("projects", [])):
        _object(project, ("name", "project_id", "repositories"))
        _text(project["name"])
        _uuid(project["project_id"])
        if project["project_id"] in seen_projects:
            _invalid("Project IDs must be distinct")
        seen_projects.add(project["project_id"])
        for repository in _array(project["repositories"]):
            _object(repository, ("name", "repository_id"), ("remote", "graph_path"))
            _text(repository["name"])
            _uuid(repository["repository_id"])
            if repository["repository_id"] in seen_repositories:
                _invalid("Repository IDs must be distinct")
            seen_repositories.add(repository["repository_id"])
            if "remote" in repository:
                _remote(repository["remote"])
            if "graph_path" in repository:
                path = _text(repository["graph_path"], 4096)
                if path.startswith(("/", "\\")) or "\\" in path or ":" in path or any(p in {"", ".", ".."} for p in path.split("/")):
                    _invalid("Graph path must be a safe repository-relative path")
    if len(canonical_json(doc).encode("utf-8")) > 1024 * 1024:
        _invalid("Handoff document exceeds the JSON size limit")
    return deepcopy(doc)


def load_handoff(path, *, allow_loopback_http=False):
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(1024 * 1024 + 1)
        value = strict_json_loads(raw)
    except (OSError, PmtError):
        _invalid("Handoff file is unavailable or is not bounded strict JSON")
    return validate_handoff(value, allow_loopback_http=allow_loopback_http)


def build_handoff(*, host_url, namespace_id, device, projects=None, ca_pem=None,
                  ca_sha256=None, issuer_version=PLUGIN_VERSION, issued_at=None,
                  allow_loopback_http=False):
    """Generate a validated, credential-free document without writing files."""
    host = {"url": host_url, "compatibility": deepcopy(COMPATIBILITY)}
    if ca_pem is not None:
        if not isinstance(ca_pem, str):
            _invalid("CA certificate must be PEM text")
        host.update(ca_pem=ca_pem, ca_sha256=(hashlib.sha256(ca_pem.encode("utf-8")).hexdigest()
                                          if ca_sha256 is None else ca_sha256))
    elif ca_sha256 is not None:
        _invalid("CA digest requires a certificate")
    value = {"format": FORMAT, "version": VERSION, "issued_at": utc_now() if issued_at is None else issued_at,
             "issuer": {"tool": "pmt-server", "version": issuer_version},
             "host": host, "namespace_id": namespace_id, "device": deepcopy(device),
             "projects": deepcopy([] if projects is None else projects)}
    return validate_handoff(value, allow_loopback_http=allow_loopback_http)
