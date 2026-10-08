"""Import a server-issued handoff into the protected client configuration."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from contextlib import nullcontext
from pathlib import Path

from .. import handoff
from ..errors import PmtError
from ..storage_config import (_config_lock, _read_profile, _sha, configure_storage,
                              profile_environment_id, storage_path)
from . import client, local_commands
from .credentials import (ENV_NAME, has_credential_store, load_credential,
                          restore_credential, snapshot_credential, stage_credential)


def _atomic_file(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".pmt-connect-", suffix=".tmp", dir=str(path.parent))
    try:
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _restore_file_locked(path, before, *, expected_after):
    """Compare-and-restore a file while its transaction lock is held."""
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    except OSError:
        return False
    if current != expected_after:
        return False
    if before is None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    else:
        try:
            _atomic_file(path, before)
        except OSError:
            return False
    return True


def _rollback_connect(root, *, prior_storage, owned_profile, profile_file, prior_profile_file,
                      profile_created_bytes, projects_file, prior_projects, projects_after,
                      client_file, prior_client, client_after, ca_path, prior_ca, installed_ca,
                      prior_credential, staged_credential):
    """Rollback only under both sidecar locks, aborting if storage ownership changed."""
    project_lock = local_commands._state_lock(root, "projects") if projects_after is not None else nullcontext()
    with project_lock:  # B2 order: project registry lock before ConfigRoot lock.
        with _config_lock(root):
            storage_file = storage_path(root)
            try:
                current_storage = storage_file.read_bytes()
            except FileNotFoundError:
                current_storage = None
            except OSError:
                return False
            current_hash = _sha(current_storage) if current_storage is not None else None
            prior_hash = _sha(prior_storage) if prior_storage is not None else None
            if current_hash not in {prior_hash, owned_profile[0] if owned_profile else None}:
                return False
            if owned_profile is not None and current_hash == owned_profile[0]:
                if not _restore_file_locked(storage_file, prior_storage, expected_after=owned_profile[1]):
                    return False
                # Recheck at the boundary before touching any dependent credential or CA.
                try:
                    restored_storage = storage_file.read_bytes()
                except FileNotFoundError:
                    restored_storage = None
                if restored_storage != prior_storage:
                    return False

            conflict = False
            if prior_profile_file is None and profile_created_bytes is not None:
                conflict |= not _restore_file_locked(profile_file, None, expected_after=profile_created_bytes)
            if projects_after is not None:
                conflict |= not _restore_file_locked(projects_file, prior_projects, expected_after=projects_after)
            if client_after is not None:
                conflict |= not _restore_file_locked(client_file, prior_client, expected_after=client_after)
            if installed_ca is not None:
                conflict |= not _restore_file_locked(ca_path, prior_ca, expected_after=installed_ca)
            if staged_credential is not None:
                conflict |= not restore_credential(root, prior_credential,
                                                   expected_current=staged_credential, _lock_held=True)
            return not conflict


def _handoff_project_rows(document):
    rows = []
    for project in document.get("projects", []):
        for repository in project["repositories"]:
            row = {"name": project["name"], "project_id": project["project_id"],
                   "repository_id": repository["repository_id"],
                   "repository_name": repository["name"]}
            row.update({key: repository[key] for key in ("remote", "graph_path") if key in repository})
            rows.append(row)
    return rows


def _connect_summary(document, result=None, *, dry_run=False):
    host = document["host"]
    compatibility = (result or {}).get("host_preflight") or host["compatibility"]
    return {"endpoint": host["url"], "namespace_id": document["namespace_id"],
            "actor": document["device"]["actor"], "scopes": list(document["device"]["scopes"]),
            "permissions": list(document["device"]["permissions"]), "compatibility": compatibility,
            "compatibility_source": "handoff" if dry_run or not (result or {}).get("host_preflight") else "Host probe",
            "ca_sha256": host.get("ca_sha256")}


def _scope_metadata(document, result):
    preflight = result.get("host_preflight") if isinstance(result, dict) else None
    authenticated = preflight.get("scopes") if isinstance(preflight, dict) else None
    if preflight is not None:
        if (result.get("device_id") != document["device"]["device_id"]
                or result.get("namespace_id") != document["namespace_id"]
                or result.get("actor") != document["device"]["actor"]
                or not isinstance(authenticated, list) or not authenticated):
            raise PmtError("host_scope_unverifiable", "Authenticated Host identity or scope grants are incomplete", 3)
        for scope in authenticated:
            try:
                if not isinstance(scope, str) or str(uuid.UUID(scope)) != scope:
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as error:
                raise PmtError("host_scope_unverifiable", "Authenticated Host scopes are not canonical", 3) from error
        if not set(document["device"]["scopes"]) <= set(authenticated):
            raise PmtError("host_scope_unverifiable", "Handoff scopes differ from authenticated Host grants", 3)
    if not isinstance(authenticated, list):
        authenticated = []
    return {"schema_version": 1, "actor": document["device"]["actor"],
            "device_id": document["device"]["device_id"], "namespace_id": document["namespace_id"],
            "handoff_scopes": sorted(document["device"]["scopes"]),
            "authenticated_scopes": sorted(authenticated)}


def _merge_client_metadata_extra(config_root, expected_current, extra):
    """Merge only internal nonsecret client metadata under the shared Root lock."""
    def check(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if any(marker in str(key).casefold() for marker in ("credential", "token", "secret", "password")):
                    raise PmtError("client_metadata_invalid", "Switch metadata cannot contain secret fields")
                check(child)
        elif isinstance(value, list):
            for child in value:
                check(child)
        elif not isinstance(value, (str, int, float, bool, type(None))):
            raise PmtError("client_metadata_invalid", "Switch metadata contains an unsupported value")

    if not isinstance(extra, dict):
        raise PmtError("client_metadata_invalid", "Switch metadata must be an object")
    check(extra)
    path = Path(config_root) / "client.json"
    with _config_lock(Path(config_root)):
        try:
            current = path.read_bytes()
        except FileNotFoundError:
            current = None
        if current != expected_current:
            raise PmtError("storage_config_conflict", "Client metadata changed during switch")
        try:
            metadata = json.loads(current.decode("utf-8")) if current else {}
        except (UnicodeError, ValueError) as error:
            raise PmtError("client_metadata_invalid", "Client metadata is invalid; switch metadata was not saved") from error
        if not isinstance(metadata, dict):
            raise PmtError("client_metadata_invalid", "Client metadata must be an object")
        metadata.update(extra)
        wire = (json.dumps(metadata, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        if len(wire) > 4096:
            raise PmtError("client_metadata_invalid", "Switch metadata exceeds its size limit")
        _atomic_file(path, wire)
        return wire


def setup_lock(config_root):
    """Lock managed setup shared by CLI connect and Claude SessionStart."""
    return local_commands._state_lock(config_root, "client-setup")


def connect(config_root, handoff_path, *, credential=None, dry_run=False, environ=None,
            configure=configure_storage, _allow_local_switch=False, _setup_locked=False,
            _client_metadata_extra=None):
    """Validate, authenticate, and atomically adopt one D1 handoff.

    ``credential`` is already read from the CLI's bounded file or stdin input;
    it is never returned, printed, or written outside the protected store.
    """
    environ = os.environ if environ is None else environ
    root = Path(config_root).expanduser().resolve()
    document = handoff.load_handoff(handoff_path)
    if document["device"]["credential"]["env"] != ENV_NAME:
        raise PmtError("handoff_invalid", "Handoff credential reference must be PMT_HOST_CREDENTIAL")

    prior_profile, config_hash = _read_profile(root)
    if prior_profile and prior_profile["mode"] == "local" and not _allow_local_switch:
        raise PmtError("local_profile_exists", "Local PMT data exists; use `pmt storage switch` to change modes")
    # Parse both sidecars before any new directories or files are published.
    local_commands._read_projects(root)
    metadata = client.read_client_metadata(root)
    if client.has_client_metadata(root) and metadata is None:
        raise PmtError("client_metadata_invalid", "Existing PMT client metadata is invalid; it was preserved")
    profile_environment_id(root, create=False)

    explicit = environ.get(ENV_NAME)
    if credential is not None and explicit and credential != explicit:
        raise PmtError("credential_conflict", "CLI credential differs from PMT_HOST_CREDENTIAL; unset the variable or use the same credential")
    selected = credential if credential is not None else explicit
    if selected is None and has_credential_store(root):
        selected = load_credential(root, environ={})
    if not isinstance(selected, str) or not selected:
        raise PmtError("credential_unavailable", "Provide the Host credential with --credential-file, --credential-stdin, or PMT_HOST_CREDENTIAL")

    ca_bytes = None
    ca_path = None
    if "ca_pem" in document["host"]:
        ca_bytes = document["host"]["ca_pem"].encode("utf-8")
        digest = hashlib.sha256(ca_bytes).hexdigest()
        if digest != document["host"]["ca_sha256"]:
            raise PmtError("handoff_ca_mismatch", "Handoff CA certificate digest does not match")
        import ssl
        try:
            ssl.create_default_context(cadata=ca_bytes.decode("utf-8"))
        except (ssl.SSLError, ValueError, UnicodeError) as error:
            raise PmtError("handoff_invalid", "Handoff CA certificate is invalid") from error
        ca_path = root / "host-ca" / f"{document['namespace_id']}.pem"
        try:
            ca_directory_info = ca_path.parent.lstat()
        except FileNotFoundError:
            ca_directory_info = None
        if (ca_directory_info is not None
                and (not stat.S_ISDIR(ca_directory_info.st_mode) or stat.S_ISLNK(ca_directory_info.st_mode)
                     or getattr(ca_directory_info, "st_file_attributes", 0) & 0x400)):
            raise PmtError("host_ca_insecure", "Configured Host CA directory is not a regular directory")

    rows = _handoff_project_rows(document)
    # Validate imported rows against the same strict registry rules before writes.
    for row in rows:
        if not isinstance(row.get("name"), str) or not isinstance(row.get("repository_name"), str):
            raise PmtError("handoff_invalid", "Handoff project metadata is invalid")

    if dry_run:
        return {"dry_run": True, "project_count": len(document.get("projects", [])),
                "connect_summary": _connect_summary(document, dry_run=True)}

    setup_guard = nullcontext() if _setup_locked else setup_lock(root)
    with setup_guard:
        current_profile, current_hash = _read_profile(root)
        if current_profile != prior_profile or current_hash != config_hash:
            raise PmtError("storage_config_conflict", "Storage settings changed while connect was preparing")
        local_commands._read_projects(root)
        current_metadata = client.read_client_metadata(root)
        if client.has_client_metadata(root) and current_metadata is None:
            raise PmtError("client_metadata_invalid", "Existing PMT client metadata is invalid; it was preserved")
        return _persist_connect(root, prior_profile, config_hash, document, rows, selected,
                                ca_bytes, ca_path, configure, _client_metadata_extra)


def _persist_connect(root, prior_profile, config_hash, document, rows, selected,
                     ca_bytes, ca_path, configure, client_metadata_extra=None):
    """Commit validated client state while the shared client-setup lock is held."""
    prior_credential = snapshot_credential(root)
    storage_file = storage_path(root)
    with _config_lock(root):
        try:
            prior_storage = storage_file.read_bytes()
        except FileNotFoundError:
            prior_storage = None
        current_hash = _sha(prior_storage) if prior_storage is not None else None
        if current_hash != config_hash:
            raise PmtError("storage_config_conflict", "Storage settings changed before connect could stage credentials")
    profile_file = root / "profile.json"
    prior_profile_file = profile_file.read_bytes() if profile_file.exists() else None
    prior_projects_file = root / "projects.json"
    prior_projects = None
    prior_client_file = root / "client.json"
    prior_client = None
    prior_ca = None
    if ca_path is not None:
        try:
            ca_info = ca_path.lstat()
        except FileNotFoundError:
            ca_info = None
        if ca_info is not None:
            if (not stat.S_ISREG(ca_info.st_mode) or stat.S_ISLNK(ca_info.st_mode)
                    or getattr(ca_info, "st_file_attributes", 0) & 0x400):
                raise PmtError("host_ca_insecure", "Configured Host CA path is not a regular file")
            prior_ca = ca_path.read_bytes()

    staged_credential = None
    installed_ca = None
    published_profile = None
    projects_after = None
    client_after = None
    profile_created_bytes = None
    configure_result = None
    existing_mappings = list(prior_profile.get("workspace_mappings", [])) if prior_profile else []
    if prior_profile and prior_profile.get("mode") == "local":
        valid_pairs = {(row["project_id"], row["repository_id"]) for row in rows}
        project_data = [mapping for mapping in existing_mappings
                        if (mapping["project_id"], mapping["repository_id"]) in valid_pairs]
    else:
        project_data = existing_mappings
    request = {"mode": "hosted", "expected_config_sha256": config_hash,
               "endpoint": document["host"]["url"], "credential_env": ENV_NAME,
               "device_id": document["device"]["device_id"], "namespace_id": document["namespace_id"],
               "expected_actor": document["device"]["actor"], "workspace_mappings": project_data}
    if ca_path is not None:
        request["ca_file"] = str(ca_path)

    original_env = os.environ.get(ENV_NAME)
    try:
        with _config_lock(root):
            try:
                current_profile_file = profile_file.read_bytes()
            except FileNotFoundError:
                current_profile_file = None
            if current_profile_file != prior_profile_file:
                raise PmtError("storage_config_conflict", "Core profile changed while connect was preparing")
            if current_profile_file is None:
                profile_environment_id(root, create=True)
                profile_created_bytes = profile_file.read_bytes()
        _, staged_credential, _ = stage_credential(root, selected)
        os.environ[ENV_NAME] = selected
        if ca_path is not None:
            with _config_lock(root):
                try:
                    current_ca = ca_path.read_bytes()
                except FileNotFoundError:
                    current_ca = None
                if current_ca != prior_ca:
                    raise PmtError("storage_config_conflict", "Host CA file changed while connect was preparing")
                _atomic_file(ca_path, ca_bytes)
            installed_ca = ca_bytes
        result = configure(str(root), request)
        configure_result = result
        published_profile = storage_file.read_bytes()
        if not isinstance(result, dict) or _sha(published_profile) != result.get("config_sha256"):
            published_profile = None
            raise PmtError("storage_config_conflict", "Storage settings changed during connect publication")
        prior_projects, projects_after = local_commands.merge_handoff_projects(root, rows)
        _written_client_file, prior_client, client_after = client.write_client_metadata_snapshot(
            root, source="connect", python_path=os.path.realpath(os.sys.executable), mode="hosted")
        metadata_extra = dict(client_metadata_extra or {})
        metadata_extra["host_scope_snapshot"] = _scope_metadata(document, result)
        client_after = _merge_client_metadata_extra(root, client_after, metadata_extra)
        if _sha(storage_file.read_bytes()) != result.get("config_sha256"):
            published_profile = None
            raise PmtError("storage_config_conflict", "Storage settings changed before connect completed")
        if ca_path is not None:
            with _config_lock(root):
                if ca_path.read_bytes() != ca_bytes:
                    raise PmtError("storage_config_conflict", "Host CA changed before connect completed")
        return {**result, "connect_summary": _connect_summary(document, result)}
    except Exception as error:
        try:
            current_wire = storage_file.read_bytes()
        except FileNotFoundError:
            current_wire = None
        except OSError:
            current_wire = None
        owned_hash = configure_result.get("config_sha256") if isinstance(configure_result, dict) else None
        owned_profile = (owned_hash, published_profile) if owned_hash is not None and published_profile is not None else None
        if not _rollback_connect(
                root, prior_storage=prior_storage, owned_profile=owned_profile,
                profile_file=profile_file, prior_profile_file=prior_profile_file,
                profile_created_bytes=profile_created_bytes, projects_file=prior_projects_file,
                prior_projects=prior_projects, projects_after=projects_after,
                client_file=prior_client_file, prior_client=prior_client, client_after=client_after,
                ca_path=ca_path, prior_ca=prior_ca, installed_ca=installed_ca,
                prior_credential=prior_credential, staged_credential=staged_credential):
            raise PmtError("storage_config_conflict", "Connect failed and a later client change prevented complete rollback") from error
        raise
    finally:
        if original_env is None:
            os.environ.pop(ENV_NAME, None)
        else:
            os.environ[ENV_NAME] = original_env


def disconnect(config_root):
    """Remove only the saved credential; leave profile, CA, and project data intact."""
    if not has_credential_store(config_root):
        return False
    before = snapshot_credential(config_root)
    if before is None:
        return False
    if not restore_credential(config_root, None, expected_current=before):
        raise PmtError("credential_store_conflict", "Credential changed during disconnect")
    return True
