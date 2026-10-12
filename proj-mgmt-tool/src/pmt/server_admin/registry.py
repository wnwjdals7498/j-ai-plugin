"""S11 project/repository registry and S13 credential-free handoffs."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from pathlib import Path

from ..db import Database, SCHEMA_VERSION
from ..errors import PmtError
from ..handoff import build_handoff
from ..host.auth import AuthRegistry, HOST_SCHEMA_VERSION
from ..http_store import HttpStore
from ..util import canonical_json
from .config import (_path_has_reparse, _process_lock, _publish_config_locked,
                     config_path, load_config_snapshot, validate_config)
from .secrets import _set_windows_acl
from .tls import check_tls

_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_HANDOFF_CREDENTIAL_ENV = "PMT_HOST_CREDENTIAL"


def _host(config_root):
    root = Path(config_root)
    config, digest = load_config_snapshot(config_path(root))
    profile = root / "profile.json"
    database_path = Path(config["paths"]["data_root"]) / "pmt.sqlite3"
    if not profile.is_file() or not database_path.is_file():
        raise PmtError("host_schema_unsupported", "Initialized Host profile and database are required")
    try:
        with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True, timeout=1) as connection:
            meta = dict(connection.execute("SELECT key,value FROM meta WHERE key IN ('schema_version','host_schema_version','host_namespace_id')"))
        if (meta.get("schema_version") != str(SCHEMA_VERSION)
                or meta.get("host_schema_version") != str(HOST_SCHEMA_VERSION)
                or not meta.get("host_namespace_id")):
            raise PmtError("host_schema_unsupported", "Host database schema or namespace is unsupported")
    except PmtError:
        raise
    except sqlite3.Error as exc:
        raise PmtError("host_schema_unsupported", "Host database metadata is unavailable") from exc
    db = Database(root=config["paths"]["data_root"], config_root=root)
    auth = AuthRegistry(db)
    return config, digest, db, auth


def _slug(value, label):
    if not isinstance(value, str) or not _SLUG.fullmatch(value):
        raise PmtError("config_invalid", f"{label} must be a short identifier using letters, numbers, dot, underscore or hyphen")
    return value


def _create_remote_scope(store, actor, session_id, kind, slug, parent_id=None, body=None):
    payload = {"kind": kind, "slug": slug}
    if parent_id is not None: payload["parent_id"] = parent_id
    if body: payload["body"] = body
    request = {"protocol_version": 1, "operation": "create_scope", "request_id": str(uuid.uuid4()),
               "actor": actor, "session_id": session_id, "payload": payload}
    try:
        envelope, code = store.execute(request)
    except PmtError as exc:
        raise PmtError("host_unreachable", "Configured Host could not complete project setup") from exc
    if code != 0 or not envelope.get("ok"):
        error = envelope.get("error") or {}
        code_value = error.get("code") if isinstance(error.get("code"), str) else "host_operation_failed"
        message = error.get("message") if isinstance(error.get("message"), str) else "Host rejected project setup"
        raise PmtError(code_value, message, code or 3)
    result = envelope.get("result")
    try:
        scope_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(scope_id, str) or str(uuid.UUID(scope_id)) != scope_id: raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise PmtError("remote_response_invalid", "Host returned an invalid scope result")
    if result.get("kind") != kind:
        raise PmtError("remote_response_invalid", "Host returned an invalid scope result")
    return scope_id


def project_add(config_root, name, *, title=None, apply=False):
    name = _slug(name, "Project name")
    if title is not None and (not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(c) < 32 for c in title)):
        raise PmtError("config_invalid", "Project title must be bounded text without control characters")
    root = Path(config_root)
    if not apply:
        config, _ = load_config_snapshot(config_path(root))
        if any(item["name"] == name for item in config["registry"]["projects"]):
            raise PmtError("project_exists", "Project is already registered")
        return {"ok": True, "applied": False, "operation": "project_add", "name": name,
                "scope_chain": ["environment", "repository", "project"]}

    with _process_lock(root / ".host-config.lock"):
        config, digest, _db, auth = _host(root)
        if any(item["name"] == name for item in config["registry"]["projects"]):
            raise PmtError("project_exists", "Project is already registered")
        # Validate all config-only fields before issuing the temporary wildcard device.
        placeholder = copy.deepcopy(config)
        placeholder["registry"]["projects"].append({"name": name, "project_id": str(uuid.uuid4()), "repositories": []})
        placeholder["revision"] += 1
        validate_config(placeholder)
        check_tls(config)

        actor = "pmt-server-bootstrap"
        issued = auth.issue_device(actor, ["*"], ["write"])
        bootstrap_id = issued["device_id"]
        env_name = "PMT_SERVER_BOOTSTRAP_" + uuid.uuid4().hex.upper()
        prior = os.environ.get(env_name)
        operation_error = None
        scope_ids = None
        try:
            os.environ[env_name] = issued["credential"]
            store = HttpStore(config["public_url"], env_name, bootstrap_id, str(uuid.uuid4()), auth.namespace_id,
                              ca_file=config["tls"].get("ca_file"), timeout=10)
            session_id = str(uuid.uuid4())
            store.register_session(session_id)
            environment_id = _create_remote_scope(store, actor, session_id, "environment",
                                                  "pmt-server-" + uuid.uuid4().hex[:16])
            repository_id = _create_remote_scope(store, actor, session_id, "repository",
                                                 "pmt-server-" + uuid.uuid4().hex[:16], environment_id)
            project_id = _create_remote_scope(store, actor, session_id, "project", name, repository_id,
                                              {"title": title} if title else None)
            scope_ids = (environment_id, repository_id, project_id)
        except Exception as exc:
            operation_error = exc
        finally:
            if prior is None: os.environ.pop(env_name, None)
            else: os.environ[env_name] = prior

        try:
            auth.revoke_device(bootstrap_id, 1)
        except Exception as exc:
            raise PmtError("bootstrap_revoke_failed",
                           f"Bootstrap device {bootstrap_id} could not be revoked; revoke it manually") from exc
        if operation_error is not None:
            raise operation_error

        updated = copy.deepcopy(config)
        updated["registry"]["projects"].append({"name": name, "project_id": scope_ids[2], "repositories": []})
        updated["revision"] += 1
        validate_config(updated)
        _publish_config_locked(root, updated, digest)
        return {"ok": True, "applied": True, "name": name, "project_id": scope_ids[2],
                "repository_id": scope_ids[1], "environment_id": scope_ids[0],
                "bootstrap_device_id": bootstrap_id, "bootstrap_state": "revoked", "revision": updated["revision"]}


def _project_repository_scope(config, project):
    database_path = Path(config["paths"]["data_root"]) / "pmt.sqlite3"
    try:
        with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True, timeout=1) as connection:
            row = connection.execute("SELECT p.parent_id,r.kind FROM scopes p LEFT JOIN scopes r ON r.id=p.parent_id "
                                     "WHERE p.id=? AND p.kind='project'", (project["project_id"],)).fetchone()
        if not row or not row[0] or row[1] != "repository":
            raise PmtError("host_scope_invalid", "Registered project is not bound to a real repository scope")
        return row[0]
    except PmtError:
        raise
    except sqlite3.Error as exc:
        raise PmtError("host_schema_unsupported", "Could not read Host project scope metadata") from exc


def project_repo_add(config_root, project_name, repository_name, remote, graph_path, *, apply=False):
    project_name = _slug(project_name, "Project name")
    repository_name = _slug(repository_name, "Repository name")
    root = Path(config_root)
    if not apply:
        config, _ = load_config_snapshot(config_path(root))
        project = next((item for item in config["registry"]["projects"] if item["name"] == project_name), None)
        if project is None: raise PmtError("project_not_found", "Project is not registered")
        repository_id = _project_repository_scope(config, project)
        if project["repositories"]:
            previous = project["repositories"][0]
            if previous["repository_id"] != repository_id:
                raise PmtError("project_repository_conflict", "A project can map to only its actual parent repository; create another project")
            if previous["name"] == repository_name and previous["remote"] == remote and previous["graph_path"] == graph_path:
                return {"ok": True, "applied": False, "unchanged": True, "repository_id": repository_id}
            raise PmtError("project_repository_conflict", "A project can map to only one repository; create another project for a different repository")
        candidate = copy.deepcopy(config)
        target = next(item for item in candidate["registry"]["projects"] if item["name"] == project_name)
        target["repositories"] = [{"name": repository_name, "repository_id": repository_id,
                                    "remote": remote, "graph_path": graph_path}]
        target_config = copy.deepcopy(candidate); target_config["revision"] += 1
        validate_config(target_config)
        return {"ok": True, "applied": False, "operation": "project_repo_add", "project": project_name,
                "repository_id": repository_id, "name": repository_name, "remote": remote, "graph_path": graph_path}

    with _process_lock(root / ".host-config.lock"):
        config, digest = load_config_snapshot(config_path(root))
        project = next((item for item in config["registry"]["projects"] if item["name"] == project_name), None)
        if project is None: raise PmtError("project_not_found", "Project is not registered")
        repository_id = _project_repository_scope(config, project)
        if project["repositories"]:
            previous = project["repositories"][0]
            if previous["repository_id"] != repository_id:
                raise PmtError("project_repository_conflict", "A project can map to only its actual parent repository; create another project")
            if previous["name"] == repository_name and previous["remote"] == remote and previous["graph_path"] == graph_path:
                return {"ok": True, "applied": True, "unchanged": True, "repository_id": repository_id}
            raise PmtError("project_repository_conflict", "A project can map to only one repository; create another project for a different repository")
        updated = copy.deepcopy(config)
        target = next(item for item in updated["registry"]["projects"] if item["name"] == project_name)
        target["repositories"] = [{"name": repository_name, "repository_id": repository_id,
                                    "remote": remote, "graph_path": graph_path}]
        updated["revision"] += 1
        validate_config(updated)
        _publish_config_locked(root, updated, digest)
        return {"ok": True, "applied": True, "project": project_name, "repository_id": repository_id,
                "name": repository_name, "revision": updated["revision"]}


def list_projects(config_root):
    config, _ = load_config_snapshot(config_path(config_root))
    return {"ok": True, "projects": copy.deepcopy(config["registry"]["projects"])}


def projects_for_scopes(config, scopes):
    if "*" in scopes:
        raise PmtError("handoff_invalid", "Wildcard devices cannot be included in a handoff")
    return [{"name": project["name"], "project_id": project["project_id"],
             "repositories": copy.deepcopy(project["repositories"])}
            for project in config["registry"]["projects"] if project["project_id"] in scopes]


def build_device_handoff(config, auth, device, *, include_ca=False):
    if device["state"] != "active": raise PmtError("device_not_active", "Only active devices can be handed off")
    if "*" in device["scopes"]:
        raise PmtError("handoff_invalid", "Wildcard devices cannot be included in a handoff")
    ca_pem = None
    if include_ca:
        ca_path = config["tls"].get("ca_file")
        if not ca_path: raise PmtError("host_tls_invalid", "No public CA certificate is configured")
        if _path_has_reparse(ca_path): raise PmtError("host_tls_invalid", "CA path cannot contain symlinks or reparse points")
        path = Path(ca_path)
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > 65536:
                raise PmtError("host_tls_invalid", "Public CA certificate file has an invalid size")
            ca_pem = path.read_text(encoding="utf-8")
        except PmtError: raise
        except (OSError, UnicodeError) as exc: raise PmtError("host_tls_invalid", "Public CA certificate is unavailable") from exc
        if "PRIVATE KEY" in ca_pem: raise PmtError("host_tls_invalid", "Handoff can include public certificates only")
    device_doc = {key: device[key] for key in ("device_id", "actor", "permissions", "scopes")}
    device_doc["credential"] = {"delivery": "separate", "env": _HANDOFF_CREDENTIAL_ENV}
    return build_handoff(host_url=config["public_url"], namespace_id=auth.namespace_id, device=device_doc,
                         projects=projects_for_scopes(config, device["scopes"]), ca_pem=ca_pem)


def write_new_file(path, content, *, sensitive=False):
    target = _validate_new_file_path(path)
    data = content if isinstance(content, bytes) else content.encode("utf-8")
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    info = os.fstat(fd)
    digest = hashlib.sha256(data).hexdigest()
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        if os.name == "nt": _set_windows_acl(target, "current")
        elif sensitive:
            os.chmod(target, 0o600)
        return {"path": str(target), "identity": (info.st_dev, info.st_ino), "sha256": digest}
    except Exception:
        try:
            current = target.lstat()
            if (stat.S_ISREG(current.st_mode) and (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino)
                    and hashlib.sha256(target.read_bytes()).hexdigest() == digest): target.unlink()
        except OSError: pass
        raise


def _validate_new_file_path(path):
    target = Path(path)
    if not target.is_absolute() or not target.parent.is_dir() or _path_has_reparse(target.parent):
        raise PmtError("path_unsafe", "Output path must be absolute in an existing local directory")
    if target.exists() or target.is_symlink(): raise PmtError("output_exists", "Output already exists; preserved")
    return target


def remove_owned_file(output):
    if not output: return
    path = Path(output["path"])
    try:
        info = path.lstat()
        if (stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == tuple(output["identity"])
                and hashlib.sha256(path.read_bytes()).hexdigest() == output["sha256"]):
            path.unlink()
    except OSError: pass
