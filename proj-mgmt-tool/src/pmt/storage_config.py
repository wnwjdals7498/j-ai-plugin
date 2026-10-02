"""Client-side LocalStore/HttpStore configuration and fail-closed selection.

Storage settings contain endpoint and credential references only. Secret values,
models, Git paths and local runtime state stay on this client.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from .errors import PmtError
from .paths import resolve_roots
from .util import canonical_json, new_id, strict_json_loads

CONFIG_FILE = "storage.json"
PROFILE_FILE = "profile.json"
SCHEMA_VERSION = 1
MAX_CONFIG_BYTES = 64 * 1024
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _uuid(value, field):
    try:
        if not isinstance(value, str) or str(uuid.UUID(value)) != value:
            raise ValueError
    except (ValueError, TypeError, AttributeError) as exc:
        raise PmtError("storage_config_invalid", f"{field} must be a canonical UUID", 2) from exc
    return value


def _relative_path(value):
    if (not isinstance(value, str) or not value or "\\" in value or value.startswith("-")):
        raise PmtError("storage_mapping_invalid", "relative_graph_path must be a POSIX relative path", 2)
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise PmtError("storage_mapping_invalid", "relative_graph_path must remain inside the workspace", 2)
    return path.as_posix()


def _mapping(value):
    required = {"repository_id", "project_id", "branch", "branch_key_sha256",
                "local_root", "relative_graph_path"}
    allowed = required | {"remote", "canonical_workspace"}
    if not isinstance(value, Mapping) or required - set(value) or set(value) - allowed:
        raise PmtError("storage_mapping_invalid", "Workspace mapping fields are incomplete or unsupported", 2)
    repository_id = _uuid(value["repository_id"], "repository_id")
    project_id = _uuid(value["project_id"], "project_id")
    branch = value["branch"]
    if branch is not None and (not isinstance(branch, str) or not branch or len(branch) > 1024
                               or any(ord(char) < 0x20 or ord(char) == 0x7f for char in branch)):
        raise PmtError("storage_mapping_invalid", "branch must be null or bounded text", 2)
    branch_hash = value["branch_key_sha256"]
    if not isinstance(branch_hash, str) or not _HEX64.fullmatch(branch_hash):
        raise PmtError("storage_mapping_invalid", "branch_key_sha256 must be lowercase SHA-256", 2)
    if branch is not None and hashlib.sha256(branch.encode("utf-8")).hexdigest() != branch_hash:
        raise PmtError("storage_mapping_invalid", "branch_key_sha256 does not match the branch key", 2)
    root = value["local_root"]
    if not isinstance(root, str) or not root.strip() or not Path(root).expanduser().is_absolute():
        raise PmtError("storage_mapping_invalid", "local_root must be an absolute client path", 2)
    relative = _relative_path(value["relative_graph_path"])
    uri = f"pmt://{repository_id}/{branch_hash}"
    if value.get("canonical_workspace", uri) != uri:
        raise PmtError("storage_mapping_invalid", "canonical_workspace does not match repository and branch hash", 2)
    remote = value.get("remote")
    if remote is not None and (not isinstance(remote, str) or not remote.strip() or len(remote) > 2048):
        raise PmtError("storage_mapping_invalid", "remote must be null or bounded text", 2)
    if remote is not None:
        parts = urlsplit(remote)
        if parts.username is not None or parts.password is not None:
            raise PmtError("storage_mapping_invalid", "remote URLs cannot contain credentials", 2)
    result = {"repository_id": repository_id, "project_id": project_id, "branch": branch,
        "branch_key_sha256": branch_hash, "local_root": os.path.abspath(os.path.expanduser(root)),
        "relative_graph_path": relative, "canonical_workspace": uri}
    if remote is not None:
        result["remote"] = remote
    return result


def _mappings(value):
    if not isinstance(value, list) or len(value) > 100:
        raise PmtError("storage_mapping_invalid", "workspace_mappings must be a bounded array", 2)
    result, seen = [], set()
    for index, item in enumerate(value):
        try:
            mapping = _mapping(item)
        except PmtError as exc:
            raise PmtError(exc.code, f"workspace_mappings[{index}] is invalid", 2) from exc
        key = (mapping["repository_id"], mapping["project_id"], mapping["branch_key_sha256"])
        if key in seen:
            raise PmtError("storage_mapping_duplicate", "Workspace mapping key is duplicated", 2)
        seen.add(key)
        result.append(mapping)
    return result


def storage_path(config_root):
    return Path(config_root).expanduser().resolve() / CONFIG_FILE


def profile_environment_id(config_root, *, create=False):
    """Read the existing installation environment UUID, creating it only on explicit setup."""
    root = Path(config_root).expanduser().resolve()
    path = root / PROFILE_FILE
    if path.exists():
        try:
            body = strict_json_loads(path.read_bytes(), max_bytes=4096)
            return _uuid(body.get("environment_id") if isinstance(body, dict) else None, "environment_id")
        except (OSError, PmtError) as exc:
            raise PmtError("profile_config_invalid", "Existing profile configuration is invalid; it was preserved", 4) from exc
    if not create:
        return None
    root.mkdir(parents=True, exist_ok=True)
    candidate = new_id()
    created = _atomic_create(path, (canonical_json({"environment_id": candidate}) + "\n").encode("utf-8"))
    if created:
        return candidate
    return profile_environment_id(config_root, create=False)


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_profile(config_root):
    path = storage_path(config_root)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise PmtError("storage_config_read_failed", "Storage settings could not be read", 4, True) from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise PmtError("storage_config_invalid", "Storage settings exceed their bound", 2)
    try:
        body = strict_json_loads(raw, max_bytes=MAX_CONFIG_BYTES)
    except PmtError as exc:
        raise PmtError("storage_config_invalid", "Storage settings are invalid and were left unchanged", 2) from exc
    if not isinstance(body, dict):
        raise PmtError("storage_config_invalid", "Storage settings must be an object", 2)
    profile = _validate_stored_profile(body)
    return profile, _sha(raw)


def _validate_stored_profile(body):
    allowed = {"schema_version", "revision", "mode", "environment_id", "workspace_mappings",
               "endpoint", "credential_env", "device_id", "namespace_id", "ca_file", "actor"}
    if set(body) - allowed or not {"schema_version", "revision", "mode", "environment_id", "workspace_mappings"} <= set(body):
        raise PmtError("storage_config_invalid", "Storage settings contain unsupported or missing fields", 2)
    if body["schema_version"] != SCHEMA_VERSION or type(body["revision"]) is not int or body["revision"] < 1:
        raise PmtError("storage_config_invalid", "Storage settings version or revision is invalid", 2)
    mode = body["mode"]
    if mode not in {"local", "hosted"}:
        raise PmtError("storage_mode_invalid", "storage mode must be local or hosted", 2)
    environment_id = _uuid(body["environment_id"], "environment_id")
    mappings = _mappings(body["workspace_mappings"])
    result = {"schema_version": SCHEMA_VERSION, "revision": body["revision"], "mode": mode,
              "environment_id": environment_id, "workspace_mappings": mappings}
    if mode == "local":
        if set(body) - {"schema_version", "revision", "mode", "environment_id", "workspace_mappings"}:
            raise PmtError("storage_config_invalid", "Local storage settings cannot contain Host credentials", 2)
        return result
    required = {"endpoint", "credential_env", "device_id", "namespace_id", "actor"}
    if not required <= set(body):
        raise PmtError("storage_config_invalid", "Hosted storage settings are incomplete", 2)
    endpoint, credential_env, actor = body["endpoint"], body["credential_env"], body["actor"]
    if not isinstance(endpoint, str) or not endpoint.startswith("https://"):
        raise PmtError("storage_config_invalid", "Hosted endpoints must use HTTPS", 2)
    if not isinstance(credential_env, str) or not _ENV_NAME.fullmatch(credential_env):
        raise PmtError("credential_reference_invalid", "credential_env must be an environment variable name", 2)
    if not isinstance(actor, str) or not actor.strip() or len(actor) > 200:
        raise PmtError("storage_config_invalid", "Host principal actor is invalid", 2)
    result.update(endpoint=endpoint, credential_env=credential_env,
                  device_id=_uuid(body["device_id"], "device_id"),
                  namespace_id=_uuid(body["namespace_id"], "namespace_id"), actor=actor)
    if body.get("ca_file") is not None:
        ca_file = body["ca_file"]
        if not isinstance(ca_file, str) or not Path(ca_file).expanduser().is_absolute():
            raise PmtError("storage_config_invalid", "ca_file must be an absolute client path", 2)
        result["ca_file"] = str(Path(ca_file).expanduser().resolve())
    return result


def _atomic_create(path: Path, wire: bytes):
    fd, temporary = tempfile.mkstemp(prefix=".pmt-profile-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(wire); stream.flush(); os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
            return True
        except FileExistsError:
            return False
        except OSError as exc:
            raise PmtError("storage_config_write_failed", "Client profile could not be created atomically", 4, True) from exc
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass


@contextlib.contextmanager
def _config_lock(root: Path):
    lock_path = root / ".storage-config.lock"
    stream = lock_path.open("a+b")
    try:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0"); stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


def _atomic_replace(path: Path, wire: bytes, expected_hash):
    root = path.parent
    root.mkdir(parents=True, exist_ok=True)
    with _config_lock(root):
        current = None
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PmtError("storage_config_read_failed", "Storage settings could not be read", 4, True) from exc
        actual = _sha(current) if current is not None else None
        if actual != expected_hash:
            raise PmtError("storage_config_conflict", "Storage settings changed before publish", 3,
                           details={"expected_config_sha256": expected_hash, "current_config_sha256": actual})
        fd, temporary = tempfile.mkstemp(prefix=".storage-", suffix=".tmp", dir=str(root))
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(wire); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, path)
            try:
                dfd = os.open(root, os.O_RDONLY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
            except OSError:
                pass
        except OSError as exc:
            raise PmtError("storage_config_write_failed", "Storage settings could not be atomically updated", 4, True) from exc
        finally:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _probe_host(profile, *, store_factory=None, environ=None):
    if store_factory is None:
        from .http_store import HttpStore
        store_factory = HttpStore
    client = store_factory(profile["endpoint"], profile["credential_env"], profile["device_id"],
                           profile["environment_id"], profile["namespace_id"], profile.get("ca_file"))
    compatible = client.check_compatibility()
    if compatible.get("compatible") is not True:
        raise PmtError("remote_compatibility_mismatch", "Host compatibility check failed", 3,
                       details={"incompatibilities": compatible.get("incompatibilities", [])})
    if (compatible.get("actor") != profile["actor"]
            or compatible.get("device_id") != profile["device_id"]
            or compatible.get("namespace_id") != profile["namespace_id"]):
        raise PmtError("host_principal_mismatch", "Host principal does not match the requested storage profile", 3)
    session_id = "pmt-setup-" + profile["environment_id"]
    registered = client.register_session(session_id)
    if (registered.get("device_id") != profile["device_id"]
            or registered.get("environment_id") != profile["environment_id"]
            or registered.get("session_id") != session_id):
        raise PmtError("host_session_registration_invalid", "Host setup session registration did not match", 3)
    return client, {key: compatible.get(key) for key in (
        "core_version", "db_schema", "graph_schema", "protocol_versions", "actor", "scopes", "permissions")}, session_id


def configure_storage(config_root, request, *, store_factory=None):
    """Probe first, then publish settings using the caller's expected content hash."""
    if not isinstance(request, Mapping):
        raise PmtError("storage_config_invalid", "Configuration request must be an object", 2)
    allowed = {"request_id", "mode", "endpoint", "credential_env", "device_id", "namespace_id",
               "environment_id", "ca_file", "workspace_mappings", "expected_config_sha256", "expected_actor"}
    if set(request) - allowed or "mode" not in request:
        raise PmtError("storage_config_invalid", "Configuration request fields are unsupported or missing", 2)
    if "request_id" in request:
        _uuid(request["request_id"], "request_id")
    path = storage_path(config_root)
    old, old_hash = _read_profile(config_root)
    if "expected_config_sha256" not in request:
        raise PmtError("storage_config_hash_required", "expected_config_sha256 is required for a settings update", 2)
    expected_hash = request["expected_config_sha256"]
    if expected_hash is not None and (not isinstance(expected_hash, str) or not _HEX64.fullmatch(expected_hash)):
        raise PmtError("storage_config_hash_invalid", "expected_config_sha256 must be null or lowercase SHA-256", 2)
    if expected_hash != old_hash:
        raise PmtError("storage_config_conflict", "Storage settings changed before setup began", 3,
                       details={"expected_config_sha256": expected_hash, "current_config_sha256": old_hash})
    environment_id = profile_environment_id(config_root, create=True)
    requested_environment = request.get("environment_id")
    if requested_environment is not None and _uuid(requested_environment, "environment_id") != environment_id:
        raise PmtError("storage_environment_mismatch", "Configured environment UUID does not match this ConfigRoot", 3)
    mode = request["mode"]
    if mode not in {"local", "hosted"}:
        raise PmtError("storage_mode_invalid", "storage mode must be local or hosted", 2)
    mappings = _mappings(request.get("workspace_mappings", []))
    revision = old["revision"] + 1 if old else 1
    preflight = None
    body = {"schema_version": SCHEMA_VERSION, "revision": revision, "mode": mode,
            "environment_id": environment_id, "workspace_mappings": mappings}
    if mode == "hosted":
        hosted_required = {"endpoint", "credential_env", "device_id", "namespace_id"}
        if not hosted_required <= set(request):
            raise PmtError("storage_config_invalid", "Hosted configuration requires endpoint, credential ref, device and namespace", 2)
        draft = {**body, "endpoint": request["endpoint"], "credential_env": request["credential_env"],
                 "device_id": request["device_id"], "namespace_id": request["namespace_id"],
                 "actor": request.get("expected_actor") or "pending-probe"}
        if request.get("ca_file") is not None:
            draft["ca_file"] = request["ca_file"]
        draft = _validate_stored_profile(draft)
        if store_factory is None:
            from .http_store import HttpStore
            store_factory = HttpStore
        try:
            probe_client = store_factory(draft["endpoint"], draft["credential_env"], draft["device_id"],
                draft["environment_id"], draft["namespace_id"], draft.get("ca_file"))
        except (ValueError, PmtError) as exc:
            raise PmtError("storage_endpoint_invalid", "Host endpoint or client TLS configuration is invalid", 2) from exc
        compatibility = probe_client.check_compatibility()
        if compatibility.get("compatible") is not True:
            raise PmtError("remote_compatibility_mismatch", "Host compatibility check failed", 3,
                           details={"incompatibilities": compatibility.get("incompatibilities", [])})
        actor = compatibility.get("actor")
        if not isinstance(actor, str) or not actor.strip():
            raise PmtError("host_principal_mismatch", "Host compatibility response has no registered actor", 3)
        if request.get("expected_actor") is not None and request["expected_actor"] != actor:
            raise PmtError("host_principal_mismatch", "Registered Host actor differs from expected_actor", 3)
        body.update(endpoint=draft["endpoint"], credential_env=draft["credential_env"],
                    device_id=draft["device_id"], namespace_id=draft["namespace_id"], actor=actor)
        if draft.get("ca_file") is not None:
            body["ca_file"] = draft["ca_file"]
        verified = _validate_stored_profile(body)
        # Setup session registration is intentionally deterministic and scoped to this environment.
        setup_session = "pmt-setup-" + environment_id
        registered = probe_client.register_session(setup_session)
        if (registered.get("session_id") != setup_session
                or registered.get("environment_id") != environment_id
                or registered.get("device_id") != verified["device_id"]):
            raise PmtError("host_session_registration_invalid", "Host setup session registration did not match", 3)
        preflight = {key: compatibility.get(key) for key in (
            "core_version", "db_schema", "graph_schema", "protocol_versions", "actor", "scopes", "permissions")}
    elif set(request) & {"endpoint", "credential_env", "device_id", "namespace_id", "ca_file", "expected_actor"}:
        raise PmtError("storage_config_invalid", "Local mode cannot include Host connection fields", 2)

    normalized = _validate_stored_profile(body)
    wire = (canonical_json(normalized) + "\n").encode("utf-8")
    if len(wire) > MAX_CONFIG_BYTES:
        raise PmtError("storage_config_invalid", "Storage settings exceed their bound", 2)
    _atomic_replace(path, wire, expected_hash)
    return {"mode": normalized["mode"], "revision": normalized["revision"],
            "config_sha256": _sha(wire), "environment_id": environment_id,
            "actor": normalized.get("actor"), "device_id": normalized.get("device_id"),
            "namespace_id": normalized.get("namespace_id"),
            "workspace_mapping_count": len(normalized["workspace_mappings"]),
            "host_preflight": preflight}


def probe_storage(config_root, *, store_factory=None):
    profile, config_hash = _read_profile(config_root)
    if profile is None:
        return {"mode": "local", "configured": False, "host_preflight": None}
    if profile["mode"] == "local":
        return {"mode": "local", "configured": True, "revision": profile["revision"],
                "config_sha256": config_hash, "environment_id": profile["environment_id"]}
    client, compatibility, setup_session = _probe_host(profile, store_factory=store_factory)
    return {"mode": "hosted", "configured": True, "revision": profile["revision"],
            "config_sha256": config_hash, "environment_id": profile["environment_id"],
            "actor": profile["actor"], "device_id": profile["device_id"],
            "namespace_id": profile["namespace_id"], "workspace_mapping_count": len(profile["workspace_mappings"]),
            "setup_session_registered": bool(setup_session), "host_preflight": compatibility}


def storage_status(config_root):
    profile, config_hash = _read_profile(config_root)
    if profile is None:
        return {"mode": "local", "configured": False,
                "environment_id": profile_environment_id(config_root, create=False)}
    result = {"mode": profile["mode"], "configured": True, "revision": profile["revision"],
              "config_sha256": config_hash, "environment_id": profile["environment_id"],
              "workspace_mapping_count": len(profile["workspace_mappings"])}
    if profile["mode"] == "hosted":
        result.update({key: profile[key] for key in ("endpoint", "credential_env", "device_id", "namespace_id", "actor")})
        result["ca_configured"] = profile.get("ca_file") is not None
    return result


def mapping_for_request(profile, request):
    payload = request.get("payload", {}) if isinstance(request, Mapping) else {}
    repo_id = payload.get("repository_id")
    project_id = payload.get("project_id") or request.get("scope_id")
    source = payload.get("source_pin") or payload.get("expected_source")
    branch = payload.get("branch")
    explicit_branch_key = payload.get("branch_key")
    source_kind, reviewed_commit = None, None
    if isinstance(source, Mapping):
        repo_id = repo_id or source.get("repository_id")
        project_id = project_id or source.get("project_id")
        branch = source.get("selected_ref", branch)
        source_kind, reviewed_commit = source.get("source_kind"), source.get("reviewed_commit")
    if not isinstance(project_id, str):
        if isinstance(payload.get("workspace"), str) and Path(payload["workspace"]).is_absolute():
            raise PmtError("storage_mapping_missing", "A local workspace path requires an explicit canonical mapping", 3)
        return None
    candidates = [item for item in profile["workspace_mappings"]
                  if item["project_id"] == project_id
                  and (repo_id is None or item["repository_id"] == repo_id)]
    if isinstance(explicit_branch_key, str) and explicit_branch_key:
        branch_key = explicit_branch_key
    elif branch is not None:
        branch_key = branch
    elif source_kind == "git" and isinstance(reviewed_commit, str) and reviewed_commit:
        branch_key = "detached:" + reviewed_commit
    elif source_kind == "non_git":
        branch_key = "non-git"
    else:
        branch_key = None
    branch_hash = hashlib.sha256(branch_key.encode("utf-8")).hexdigest() if branch_key is not None else None
    if branch_hash is None:
        matched = candidates if len(candidates) == 1 else []
    else:
        matched = [item for item in candidates
                   if item["branch_key_sha256"] == branch_hash and item["branch"] == branch]
    if len(matched) != 1:
        raise PmtError("storage_mapping_missing", "No unique local checkout matches the canonical repository/project/branch", 3)
    mapping = matched[0]
    if branch_key is not None and mapping["branch_key_sha256"] != hashlib.sha256(branch_key.encode("utf-8")).hexdigest():
        raise PmtError("storage_mapping_stale", "Local branch mapping no longer matches SourcePin", 3)
    return mapping


def adapt_host_request(profile, request):
    """Translate a local workspace mapping to canonical identifiers without sending local paths."""
    value = json.loads(canonical_json(request))
    payload = value.get("payload")
    if not isinstance(payload, dict):
        return value
    if value.get("operation") == "read_client_plan":
        return value
    mapping = mapping_for_request(profile, value)
    if mapping is None:
        return value
    workspace = payload.get("workspace")
    if isinstance(workspace, str) and Path(workspace).is_absolute():
        resolved = Path(workspace).expanduser().resolve()
        local_root = Path(mapping["local_root"]).resolve()
        if resolved != local_root:
            raise PmtError("storage_mapping_conflict", "Request workspace does not match the configured local checkout", 3)
    elif workspace is not None and workspace != mapping["canonical_workspace"]:
        raise PmtError("storage_mapping_conflict", "Request workspace does not match its configured canonical mapping", 3)
    supplied_canonical = payload.get("canonical_workspace")
    if supplied_canonical is not None and supplied_canonical != mapping["canonical_workspace"]:
        raise PmtError("storage_mapping_conflict", "Request canonical workspace differs from its configured mapping", 3)
    supplied_graph = payload.get("relative_graph_path")
    if supplied_graph is not None and _relative_path(supplied_graph) != mapping["relative_graph_path"]:
        raise PmtError("storage_mapping_conflict", "Request graph path differs from its configured mapping", 3)
    payload["repository_id"] = mapping["repository_id"]
    payload["project_id"] = mapping["project_id"]
    payload["canonical_workspace"] = mapping["canonical_workspace"]
    payload["workspace"] = mapping["canonical_workspace"]
    payload["relative_graph_path"] = mapping["relative_graph_path"]
    branch_key = payload.get("branch_key")
    source = payload.get("source_pin") or payload.get("expected_source")
    if branch_key is None:
        if mapping["branch"] is not None:
            branch_key = mapping["branch"]
        elif isinstance(source, Mapping) and source.get("source_kind") == "git" and source.get("reviewed_commit"):
            branch_key = "detached:" + source["reviewed_commit"]
        elif isinstance(source, Mapping) and source.get("source_kind") == "non_git":
            branch_key = "non-git"
    if branch_key is not None:
        if not isinstance(branch_key, str) or not branch_key or len(branch_key) > 1024:
            raise PmtError("storage_mapping_invalid", "branch_key must be bounded text", 2)
        if hashlib.sha256(branch_key.encode("utf-8")).hexdigest() != mapping["branch_key_sha256"]:
            raise PmtError("storage_mapping_stale", "Current SourcePin branch key differs from the mapping", 3)
        payload["branch_key"] = branch_key
    payload.pop("local_root", None)
    payload.pop("local_workspace", None)
    if value.get("operation") in {"publish_source_snapshot", "publish_verification_snapshot", "read_source_metadata",
                                  "publish_client_plan"}:
        payload.pop("workspace", None)
        payload.pop("branch", None)
    if value.get("operation") in {"publish_verification_snapshot", "read_source_metadata", "publish_client_plan"}:
        payload.pop("branch_key", None)
    return value


def adapt_host_actor(profile, request):
    """Bind the one native hook actor to the authenticated Host principal only for normalized hook events."""
    if not isinstance(request, Mapping):
        raise PmtError("host_input_invalid", "Request must be an object", 2)
    value = json.loads(canonical_json(request))
    actor = value.get("actor")
    if actor == profile["actor"]:
        source = value.get("source")
        if isinstance(source, dict) and "original_actor" in source:
            from .hooks import ADAPTER_VERSION, EVENTS
            if (value.get("operation") != "record_event" or source.get("product") not in EVENTS
                    or source.get("adapter_version") != ADAPTER_VERSION
                    or source.get("installation_id") != profile["environment_id"]
                    or source.get("original_actor") != "hook"):
                raise PmtError("host_actor_mismatch", "Hook actor provenance is invalid", 3)
        return value
    source = value.get("source")
    from .hooks import ADAPTER_VERSION, EVENTS
    if (actor != "hook" or value.get("operation") != "record_event"
            or not isinstance(source, dict) or source.get("product") not in EVENTS
            or source.get("adapter_version") != ADAPTER_VERSION
            or source.get("installation_id") != profile["environment_id"]
            or not isinstance(value.get("normalized_event"), dict)):
        raise PmtError("host_actor_mismatch", "Request actor does not match the authenticated Host principal", 3)
    if "original_actor" in source:
        raise PmtError("host_actor_mismatch", "Native hook input cannot supply original_actor", 3)
    value["actor"] = profile["actor"]
    source["original_actor"] = "hook"
    return value


def _prepare_host_identity(profile, request, environ):
    value = adapt_host_actor(profile, request)
    if (value.get("operation") == "record_event"
            and (value.get("source") or {}).get("original_actor") == "hook"):
        selected_scope = environ.get("PMT_SCOPE_ID")
        try:
            selected_scope = _uuid(selected_scope, "PMT_SCOPE_ID")
        except PmtError as exc:
            raise PmtError("hook_scope_required", "Hosted native hook events require an explicit PMT_SCOPE_ID", 3) from exc
        if selected_scope not in {item["project_id"] for item in profile["workspace_mappings"]}:
            raise PmtError("hook_scope_forbidden", "PMT_SCOPE_ID must name a configured project mapping", 3)
        if value.get("scope_id") not in (None, selected_scope):
            raise PmtError("hook_scope_forbidden", "Hook request scope conflicts with the configured project", 3)
        value["scope_id"] = selected_scope
    return value


def select_store(data_root, config_root, request, *, environ=None, local_store_factory=None,
                 http_store_factory=None, hosted_runtime_factory=None, hosted_files_factory=None,
                 client_routing_factory=None):
    """Build the selected operation facade without a hosted-mode local DB fallback."""
    profile, _config_hash = _read_profile(config_root)
    routing_operations = {"save_routing_policy", "read_routing_policy", "register_capabilities",
                          "inspect_capabilities", "select_execution_route"}
    if (request.get("operation") in routing_operations
            and (profile is not None and profile["mode"] == "hosted"
                 or (Path(config_root) / "routing-client.sqlite3").is_file())):
        if client_routing_factory is None:
            from .routing.client_config import ClientRoutingConfig
            return ClientRoutingConfig(config_root, legacy_data_root=data_root)
        return client_routing_factory(config_root=config_root, legacy_data_root=data_root)
    if profile is None or profile["mode"] == "local":
        if local_store_factory is None:
            from .db import Database
            from .store import LocalStore
            db = Database(data_root, config_root)
            return LocalStore(db)
        return local_store_factory(data_root, config_root)
    runtime_env = os.environ if environ is None else environ
    request = _prepare_host_identity(profile, request, runtime_env)
    operation = request.get("operation")
    from .host.application import ALLOWLIST
    from .host.control_state import OPERATIONS as CONTROL_OPERATIONS
    from .host.host_contract import HOST_DATA_OPERATIONS, LOCAL_ONLY_OPERATIONS
    host_operations = ALLOWLIST | HOST_DATA_OPERATIONS | CONTROL_OPERATIONS | {"get_request_result"}
    runtime_operations = {"advance_execution_control", "acknowledge_execution_action",
                          "dispatch_execution", "poll_execution", "cancel_runner"}
    file_operations = {"preview_graph_change", "apply_graph_change", "recover_graph_change",
        "prepare_document_segments", "publish_document_segments", "recover_document_segments"}
    if operation not in host_operations and operation not in LOCAL_ONLY_OPERATIONS:
        raise PmtError("host_operation_forbidden", "Operation is not available in hosted storage mode", 3)
    if operation in LOCAL_ONLY_OPERATIONS and operation not in runtime_operations | file_operations:
        raise PmtError("hosted_local_runtime_unavailable",
            "This local filesystem operation has no hosted client adapter; local database fallback is disabled", 3)
    if http_store_factory is None:
        from .http_store import HttpStore
        http_store_factory = HttpStore
    try:
        http = http_store_factory(profile["endpoint"], profile["credential_env"], profile["device_id"],
            profile["environment_id"], profile["namespace_id"], profile.get("ca_file"))
    except (ValueError, PmtError) as exc:
        raise PmtError("storage_config_invalid", "Hosted client configuration is invalid", 2) from exc
    compatible = http.check_compatibility()
    if compatible.get("compatible") is not True:
        raise PmtError("remote_compatibility_mismatch", "Host compatibility check failed", 3,
                       details={"incompatibilities": compatible.get("incompatibilities", [])})
    if (compatible.get("actor") != profile["actor"]
            or compatible.get("device_id") != profile["device_id"]
            or compatible.get("namespace_id") != profile["namespace_id"]):
        raise PmtError("host_principal_mismatch", "Current Host principal differs from stored profile", 3)
    session_id = request.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise PmtError("host_session_required", "A native caller session ID is required for hosted storage", 3)
    http.register_session(session_id)
    if operation in host_operations or operation == "get_request_result":
        return _HostedOperationFacade(profile, http, runtime_env)
    if operation in file_operations:
        if hosted_files_factory is None:
            hosted_files_factory = _default_hosted_files
        return hosted_files_factory(profile=profile, state_port=http, request=request,
                                    data_root=data_root, environ=runtime_env)
    if operation in LOCAL_ONLY_OPERATIONS:
        if hosted_runtime_factory is None:
            hosted_runtime_factory = _default_hosted_runtime
        runtime = hosted_runtime_factory(profile=profile, state_port=http, request=request,
                                         data_root=data_root, environ=runtime_env)
        return _HostedRuntimeFacade(runtime, operation)
    raise PmtError("host_operation_forbidden", "Operation is neither Host-allowlisted nor a supported local runtime operation", 3)


def _default_hosted_files(*, profile, state_port, request, data_root, environ):
    from .hosted_files import HostedFiles
    # Validate a supplied physical path against the explicit mapping before
    # the adapter inspects its independently authorized local checkout.
    adapt_host_request(profile, request)
    session_hash = hashlib.sha256(request["session_id"].encode("utf-8")).hexdigest()
    spool = (Path(data_root).expanduser().absolute() / "hosted-file-effects" /
             profile["namespace_id"] / profile["device_id"] /
             profile["environment_id"] / session_hash)
    return HostedFiles(profile, state_port, spool)


def _default_hosted_runtime(*, profile, state_port, request, data_root, environ):
    """Construct the client effect adapter without opening a local business DB."""
    from .diagnostics import DiagnosticLogger
    from .hosted_runtime import HostedLocalRuntime
    from .pending import PendingOutbox

    def mapping_provider(run, source_pin):
        mapped = mapping_for_request(profile, {"scope_id": source_pin.get("project_id"),
            "payload": {"repository_id": source_pin.get("repository_id"),
                        "source_pin": source_pin}})
        if mapped is None or run.get("workspace") != mapped["canonical_workspace"]:
            raise PmtError("storage_mapping_conflict", "Host run does not match this client's checkout mapping", 3)
        return mapped

    session_hash = hashlib.sha256(request["session_id"].encode("utf-8")).hexdigest()
    spool = (Path(data_root).expanduser().absolute() / "hosted-spool" /
             profile["namespace_id"] / profile["device_id"] /
             profile["environment_id"] / session_hash)
    pending_root = (Path(data_root).expanduser().absolute() / "hosted-pending" /
                    profile["namespace_id"] / profile["device_id"] /
                    profile["environment_id"] / session_hash)
    return HostedLocalRuntime(state_port, mapping_provider, spool,
        diagnostics=DiagnosticLogger(), pending_outbox=lambda: PendingOutbox(pending_root,
            namespace_id=profile["namespace_id"], actor=profile["actor"],
            device_id=profile["device_id"], environment_id=profile["environment_id"],
            session_id=request["session_id"]))


def execute_pending_command(data_root, config_root, action, body):
    """Explicit client-only receipt preservation; never creates a shared offline job."""
    profile, _config_hash = _read_profile(config_root)
    if profile is None or profile["mode"] != "hosted":
        raise PmtError("hosted_profile_required", "Pending receipt commands require a hosted profile", 3)
    if not isinstance(body, dict):
        raise PmtError("pending_input_invalid", "Pending command input must be an object", 2)
    common = {"request_id", "session_id", "scope_id"}
    extras = ({"run_id", "context_ref", "source_hash", "dispatch_ref"} if action == "capture" else
              {"run_id", "context_ref", "source_hash", "dispatch_ref", "runtime_receipt_ref", "pending_request_id"}
              if action == "reconcile" else set())
    if action not in {"status", "capture", "reconcile"} or set(body) - common - extras:
        raise PmtError("pending_input_invalid", "Pending command fields are unsupported", 2)
    request_id = _uuid(body.get("request_id"), "request_id")
    session_id = body.get("session_id")
    if (not isinstance(session_id, str) or not session_id or len(session_id) > 200
            or any(ord(char) < 0x20 or ord(char) == 0x7f for char in session_id)):
        raise PmtError("pending_input_invalid", "An existing native session ID is required", 2)
    request = {"protocol_version": 1, "request_id": request_id, "operation": "poll_execution",
        "actor": profile["actor"], "session_id": session_id, "scope_id": body.get("scope_id"),
        "source": {"product": "cli"}, "payload": {key: value for key, value in body.items()
                                                   if key not in common}}
    from .http_store import HttpStore
    store = HttpStore(profile["endpoint"], profile["credential_env"], profile["device_id"],
        profile["environment_id"], profile["namespace_id"], profile.get("ca_file"))
    runtime = _default_hosted_runtime(profile=profile, state_port=store, request=request,
                                      data_root=data_root, environ=os.environ)
    if action == "status":
        from dataclasses import asdict
        outbox = runtime._pending_outbox()
        return {"pending_results": [asdict(item) for item in outbox.list_pending()],
                "pending_resources": [asdict(item) for item in outbox.list_resources()]}
    _uuid(request["scope_id"], "scope_id")
    if action == "capture":
        required = {"run_id", "context_ref", "source_hash", "dispatch_ref"}
        if set(request["payload"]) != required:
            raise PmtError("pending_input_invalid", "Capture needs the exact existing dispatch/context/source refs", 2)
        return runtime.capture_pending_terminal_receipt(request)
    compatibility = store.check_compatibility()
    if (compatibility.get("compatible") is not True
            or compatibility.get("actor") != profile["actor"]
            or compatibility.get("device_id") != profile["device_id"]
            or compatibility.get("namespace_id") != profile["namespace_id"]):
        raise PmtError("remote_compatibility_mismatch", "Current Host no longer matches the selected profile", 3)
    store.register_session(session_id)
    return runtime.reconcile_pending_result(request)


class _HostedOperationFacade:
    def __init__(self, profile, state_port, environ):
        self.profile, self.state_port, self.environ = profile, state_port, environ

    def execute(self, request):
        request = _prepare_host_identity(self.profile, request, self.environ)
        if request.get("operation") == "get_request_result":
            payload = request.get("payload", {})
            expected = payload.get("expected_request")
            if expected is not None:
                expected = adapt_host_request(self.profile,
                    _prepare_host_identity(self.profile, expected, self.environ))
            found = self.state_port.get_request_result(payload.get("request_id"),
                request.get("actor"), request.get("session_id"),
                expected_request=expected)
            from .service import response
            result = {"found": found is not None,
                      "response": found[0] if found else None,
                      "exit_code": found[1] if found else None}
            return response(request.get("request_id"), result=result), 0
        return self.state_port.execute(adapt_host_request(self.profile, request))

    def get_request_result(self, request_id, actor=None, session_id=None, *, expected_request=None):
        if expected_request is not None:
            expected_request = adapt_host_request(self.profile,
                _prepare_host_identity(self.profile, expected_request, self.environ))
        return self.state_port.get_request_result(request_id, actor, session_id,
                                                   expected_request=expected_request)

    def check_compatibility(self):
        return self.state_port.check_compatibility()


class _HostedRuntimeFacade:
    """Route only local runtime operations through an injected runtime adapter."""

    def __init__(self, runtime, operation):
        self.runtime, self.operation = runtime, operation

    def execute(self, request):
        method = ("execute_control" if self.operation in {"advance_execution_control", "acknowledge_execution_action"}
                  else "dispatch" if self.operation == "dispatch_execution"
                  else "observe" if self.operation == "poll_execution"
                  else "cancel" if self.operation == "cancel_runner" else None)
        if method is None or not callable(getattr(self.runtime, method, None)):
            raise PmtError("hosted_local_runtime_unavailable", "The client-local runtime does not support this operation", 3)
        result = getattr(self.runtime, method)(request)
        if isinstance(result, tuple):
            return result
        from .service import response
        return response(request.get("request_id"), result=result), 0
